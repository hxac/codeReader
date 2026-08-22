# 前台工作日志与 hang 检测：profiler 对调度链路的插桩

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `profiler` feature（编译期门控）与 `set_trace_enabled` / `trace_enabled`（运行期开关）各自管住哪些数据通道：前台工作日志 `ForegroundJournal` 只要编译进来就常开，而旧的帧事件环与每线程任务环还需要运行期开关。
2. 跟踪三类前台工作——任务轮询、输入/动作分发、绘制与呈现——分别在哪一行源码被写进日志，以及 `ForegroundRunnableCounter` 如何用「排队计数」防止 idle 边界把一段连续工作错误地切成两段。
3. 讲解 `journal.rs` 的数据模型：六种 `ForegroundEvent`、小轮询折叠（`TASK_POLL_FLOOR`）、两种 `IntervalBoundary`，以及纯状态机 `IntervalSealer` 如何把事件流封口成 `FrameSnapshot`（含 `occupancy` 与 `busy_fraction` 的计算方式）。
4. 掌握 `HangDetector` 判定 hang 的两条标准——**单事件时长 ≥ threshold** 与 **区间前台总占用 ≥ frame_budget**——以及日志出现断点（`Discontinuity`）时第二条标准为何被抑制。
5. 通过 `App::foreground_journal()` 拿到日志句柄，写一个能人为制造 hang、再用极小阈值检测出 `HangIncident` 的探针程序。
6. 说明 7316cf7745（Report foreground executor work in bench reports）之后，`gpui::bench` 如何通过 `ForegroundJournalCollector` 把**不产生任何绘制**的前台任务也汇入 `BenchReport::foreground_work`——journal 的第二个内建消费者。

本讲是第 4 单元「调度与并发」的收官：u4-l1 讲了单前台线程模型，u4-l2 讲了 `PlatformDispatcher` 契约（`RunnableVariant` 信封、spawn 位置元数据），u4-l3～u4-l5 依次拆了 Linux/macOS/Windows/Web 四个调度器实现。本讲要看的是：**这些调度器在执行每个 runnable 前后额外多做的那两件事**（`update_running_task` / `save_task_timing`），如何汇成一条可事后审计的主线程工作日志，并支撑 Zed 的线上卡顿报告；最后（4.5）再看同一条日志如何反过来喂给 gpui 的 benchmark 基建。

## 2. 前置知识

### 2.1 为什么卡顿检测必须是「事后」的

主线程卡住时，任何运行在主线程上的检测代码自己也被卡住。所以可行的方案只有两种：

- **看门狗**：另一个线程定期检查主线程是否心跳（Zed 也有一条这样的兜底日志线，见 `crates/zed/src/reliability/` 下的 logging 模块）。
- **事后审计**：主线程把自己做过的每件事快速记下来（只记时间戳与位置，不做分析），由别的线程慢慢消费。

本讲的 `ForegroundJournal` + `HangDetector` 属于第二种。关键约束是**记录必须极廉价**：主线程永远不能为了等一个读日志的人而停下（后面会看到无锁槽位设计正是为此）。

### 2.2 环形日志与「序号防混淆」

环形缓冲（ring buffer）是固定大小的数组，写满后覆盖最旧的槽位。gpui 的日志环在每个槽位上额外存一个**单调递增的序号**（sequence）：读侧只有当槽内序号等于自己期待的逻辑序号时才认这次读取。这样即便写侧转了一圈回来复用同一槽位，读侧也不会把「第 1024 条」误当成「第 0 条」——这是无锁环形队列防 ABA 问题的标准手法。

### 2.3 一帧的生命周期：dirty → draw → present

回顾 u3-l2/u4-l1 的内容，GPUI 窗口的一帧分三步：

1. **dirty（失效）**：某视图调用 `cx.notify()`，窗口记下「需要重画」，并记录**首次失效时刻** `dirty_at`；
2. **draw（绘制）**：在主线程上跑布局与渲染，产出 `Scene`；
3. **present（呈现）**：把 `Scene` 提交给平台窗口系统上屏。

从 `dirty_at` 到 `present_end` 的时长就是用户感知的「这一帧等了多久」。日志里帧的三步各占一类事件，`FrameSnapshot` 用呈现或空闲作为区间的**封口边界**。

### 2.4 直方图的「桶化」与精确总量

`hdrhistogram` 这类记录型直方图不保存每个样本，而是把样本丢进**按对数分布的桶**里。gpui 用的是 3 位有效数字精度（`Histogram::new(3)`）：记录 1.234ms，桶里存的是它所属区间的代表值。这对分位数（p50/p99）完全够用，但**把桶值相加求总量会有累积误差**。4.5 会看到 gpui 为此专门做了「直方图 + 精确 `total_nanos`」的组合结构——先记住这个精度差异，就能理解那个设计为什么存在。

### 2.5 术语表

| 术语 | 含义 |
| --- | --- |
| 前台（foreground） | GPUI 的主线程：所有实体更新、UI 绘制都发生在这里（u4-l1） |
| turn（回合） | 一次「前台被唤醒干活→干完回到事件循环」的括号，可嵌套（输入分发里同步触发一次绘制就是两层） |
| runnable | 平台调度器投递的可执行任务信封，即 u4-l2 的 `RunnableVariant` |
| poll（轮询） | 一次驱动 async 任务的过程；任务不一次性跑完，每次 poll 推进到下一个 await 点 |
| occupancy（占用） | 一个区间内前台真正在干活的时长（事件区间的并集 + 折叠小轮询的分摊） |
| hang（卡顿） | 单个前台工作阻塞超过阈值，或一个区间总占用超过帧预算 |
| collector（收集器） | journal 的读取游标，只观测**创建之后**记录的条目，多个 collector 互不干扰 |

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/gpui/src/profiler.rs` | profiler 模块根：feature 门控、运行期开关 `set_trace_enabled`、`WindowProfiler`（把输入/动作/绘制/呈现包成 turn 并写入日志）、旧的帧事件环 `FRAME_TIMINGS` |
| `crates/gpui/src/profiler/journal.rs` | 本讲主角一：`ForegroundJournal`（无锁环形日志）、`ForegroundEvent` / `ForegroundJournalEntry` 数据模型、`ForegroundRunnableCounter`、`IntervalSealer` 与 `FrameSnapshot`，以及测试专用的 `install_test_foreground_journal` |
| `crates/gpui/src/profiler/hang.rs` | 本讲主角二：`HangDetector`（轮询日志、产出 `HangIncident`）与 `SerializedHangIncident`（telemetry 友好的序列化形态） |
| `crates/gpui/src/platform_scheduler.rs` | `PlatformScheduler`：前台任务入队时给 `ForegroundRunnableCounter` 加计数的那个环节 |
| `crates/gpui/src/executor.rs` | `ForegroundExecutor`：持有 counter 的克隆；`spawn_when_idle` 的入队计数 |
| `crates/gpui/src/app.rs` | `App` 构造时安装日志（`install_foreground_journal`）并提供 `App::foreground_journal()` 访问器 |
| `crates/gpui/src/app/bench_context.rs` | 本讲主角三（本轮新增）：`gpui::bench` 的测量基建 `BenchAppContext`/`BenchReport`；7316cf7745 之后它消费 journal，把前台工作汇入 `foreground_work` 直方图 |
| `crates/gpui_macros/src/bench.rs` | `#[gpui::bench]` 宏展开：创建 `BenchAppContext` 并在结束时 `report.print()`（辅助） |
| `crates/gpui/src/window.rs` | 窗口侧插桩：`FrameDirtyAccumulator` 记录首次失效、`Window::draw`/`present` 的括号（辅助） |
| `crates/gpui/Cargo.toml` | `profiler = ["dep:hdrhistogram"]` 与 `bench = ["test-support", "profiler", "dep:criterion"]` feature 定义（辅助） |
| `crates/zed/src/reliability/hang_detection.rs` | 生产接线示范：Zed 如何用极小代码把 `HangDetector` 挂到独立线程（拓展阅读） |
| `crates/benchmarks/benches/editor_render.rs` | 真实的 `#[gpui::bench]` 使用者：`bench_iter`/`bench_renderer` 的调用样例（拓展阅读） |

## 4. 核心概念与源码讲解

### 4.1 开关与入口：profiler feature、trace_enabled 与 App::foreground_journal

#### 4.1.1 概念说明

gpui 的性能插桩有**两层开关**，分工很容易混淆：

- **编译期**：Cargo feature `profiler`。它决定 `profiler::journal`、`profiler::hang` 两个模块乃至 `hdrhistogram` 依赖是否存在。不开这个 feature，本讲的一切类型都不存在。
- **运行期**：`set_trace_enabled(bool)` 与 `trace_enabled()`。它只控制**旧的聚合计数通道**——每线程任务计时环与帧事件环 `FRAME_TIMINGS`。**前台工作日志 `ForegroundJournal` 不受它控制**：只要编译进来就一直在记录。

为什么日志要常开？因为 hang 是小概率事件，等用户（或开发者）想起打开开关时，卡顿早过去了。而旧的计时环保留开关，是因为它体量大（每线程 16MiB 上限），长开有内存与写入成本。Zed 主程序**无条件启用** `profiler` feature（`crates/zed/Cargo.toml` 中 `gpui = { workspace = true, features = ["profiler"] }`），所以线上每个 Zed 都带着这条日志。另一个常开收益要在 4.5 才显现：benchmark 也因此**无需任何开关**就能读到前台工作日志。

#### 4.1.2 核心流程

```text
编译期                          运行期
────────                        ────────
Cargo.toml:
  profiler = ["dep:hdrhistogram"]
        │
        ▼
#[cfg(feature = "profiler")]
  pub mod journal;  pub mod hang;
        │
        ▼                          App::new_app（主线程）
App 结构体持有                     │
  foreground_journal  ◄─────────── install_foreground_journal()
        ▲                          （幂等：同线程多个 App 共享一条日志）
        │
App::foreground_journal() ──► ForegroundJournal（可 Clone 的句柄）
                                     │ .collector()
                                     ▼
                          ForegroundJournalCollector（独立游标）
                          ├── 消费者一：HangDetector（4.4）
                          └── 消费者二：BenchReport（4.5，7316cf7745 起）

另一条受运行期开关控制的通道：
  set_trace_enabled(true/false) ──► TRACE_STATE 原子位
                                     │
  FRAME_TIMINGS（帧事件环）/ 每线程 timings 环
  仅当 trace_enabled() 时写入；关闭时被 clear_trace_buffers() 清空
```

#### 4.1.3 源码精读

feature 的定义只有一行，拉起 `hdrhistogram`（直方图库，供 `WindowProfiler` 的聚合直方图使用）：[gpui/Cargo.toml:40](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/Cargo.toml#L40)，可选依赖声明在 [gpui/Cargo.toml:97](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/Cargo.toml#L97)。另外还有一个聚合 feature 值得记住：`bench = ["test-support", "profiler", "dep:criterion"]`（[gpui/Cargo.toml:29](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/Cargo.toml#L29)）——它把 profiler 一起拉起，是 4.5 的前提。

模块层面，`hang` 与 `journal` 两个子模块整体被 feature 门控且是 `pub mod`（下游可以直接 `use gpui::profiler::journal::…`），`actions`（动作计时统计）则常开：[profiler.rs:19-24](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler.rs#L19-L24)。

运行期开关是一个原子整数 `TRACE_STATE`：最高位是「启用」标志，其余位是**作用域计数**（测试里可以叠加多个 trace 作用域，最后一个结束才真正关闭）：[profiler.rs:696-735](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler.rs#L696-L735)。`trace_enabled()` 只是读一下这个原子量：[profiler.rs:764-767](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler.rs#L764-L767)。注意 `clear_trace_buffers` 清理的对象：[profiler.rs:769-783](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler.rs#L769-L783) ——它只清每线程 `timings` 环和 `FRAME_TIMINGS`，**不含 journal 环**，这是「日志常开」的直接证据。

受运行期开关控制的两处写入点：每线程任务计时环在 [profiler.rs:613-620](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler.rs#L613-L620)（`if trace_enabled()` 之外还有 Top5 统计，那部分不受开关控制）；旧帧事件环的入口 `record_frame_event` 在 [profiler.rs:1167-1180](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler.rs#L1167-L1180) 开头就检查 `trace_enabled()`。

日志的安装点在 `App` 构造中：[app.rs:791](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app.rs#L791) 调用 `install_foreground_journal()`，结果存进字段：[app.rs:692-693](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app.rs#L692-L693)（注意字段本身也被 feature 门控）。安装函数是**线程局部**且**幂等**的：[journal.rs:533-554](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L533-L554)——同一线程构造多个 `App`（测试的常态）共享同一条日志，后到的拿到同一环的句柄。

对外的读取入口就是本讲实践要用的 `App::foreground_journal()`：[app.rs:1938-1943](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app.rs#L1938-L1943)。`ForegroundJournal` 是可 `Clone` 的轻句柄：[journal.rs:883-913](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L883-L913)。

#### 4.1.4 代码实践

1. **实践目标**：确认本讲讲解的两层开关在真实构建里的状态，并找到日志在自己平台上的安装路径。
2. **操作步骤**：
   - 在仓库根执行 `cargo tree -p zed -i hdrhistogram -e features 2>/dev/null | head -20`（或在 `crates/zed/Cargo.toml` 搜 `profiler`），确认 Zed 主程序确实通过 `gpui/profiler` 拉起了 `hdrhistogram`；
   - 用 Grep 在 `crates/gpui/src` 搜 `set_trace_enabled`，列出所有调用点，标注哪些在测试代码里；
   - 在 `crates/gpui/src/app.rs` 找到 `install_foreground_journal` 调用，向上看它所在的函数是不是只在主线程执行（对照 u4-l1 讲过的 `is_main_thread` 断言）。
3. **需要观察的现象**：`set_trace_enabled` 的生产调用点非常少（主要在测试与 bench 里），因为主通道（journal）不需要它。
4. **预期结果**：能画出 4.1.2 的流程图，并回答「journal 的写入受哪个开关控制」——答案是只受编译期 feature 控制。
5. 命令输出与具体调用点数量**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果不启用 `profiler` feature，`App::foreground_journal()` 还存在吗？

**答案**：不存在。字段（app.rs:692-693）与方法（app.rs:1940-1943）都带 `#[cfg(feature = "profiler")]`，feature 关闭时该方法从 `App` 的 API 面上消失，调用它的代码会编译失败——这是让下游「零成本不开通」的标准做法。

**练习 2**：`set_trace_enabled(false)` 之后，主线程上的 hang 还能被 `HangDetector` 检测到吗？

**答案**：能。`HangDetector` 消费的是 `ForegroundJournal` 环，它的写入路径（4.2 会逐一走到）不检查 `trace_enabled()`；被关闭开关清空的是每线程任务计时环与 `FRAME_TIMINGS`（profiler.rs:769-783）。

**练习 3**：为什么 `install_foreground_journal` 要设计成幂等的，而不是每次 `App` 构造都新建一条日志？

**答案**：测试常在同一线程先后（甚至同时）构造多个 `App`（journal.rs:533-535 的文档注释）。若每次新建，前一个 App 期间记录的条目就对后一个 App 的检测器不可见，且旧环会被无谓丢弃；共享一条流则保证「同线程所有 App 的前台工作」都在一个事件流里，边界语义不中断。4.5 还会看到它的反例：当测试**刻意**需要隔离时，用 `install_test_foreground_journal` 换一条全新的日志。

### 4.2 记录的三个来源与 ForegroundRunnableCounter：前台 turn 的嵌套与 idle 闸门

#### 4.2.1 概念说明

日志里的事件不会凭空出现。往前追溯，所有 `ForegroundEvent` 来自三类插桩点：

1. **任务轮询**：u4-l2 讲过，每个平台调度器在执行 runnable 前后各调一次 `profiler::update_running_task` / `profiler::save_task_timing`（Linux 的 worker 线程与主循环、macOS 的 trampoline、Windows 的消息泵、`ThreadedDispatcher` 都有这对括号）。这对括号同时服务于旧的每线程计时与本讲的 journal。
2. **输入与动作分发**：`WindowProfiler::begin_input`/`end_input`、`begin_action_handler`/`end_action_handler`，由窗口的事件分发路径调用。
3. **帧的三步**：`record_frame_pending`（首次失效）、`begin_draw`/`end_draw`、`record_present`。

除了事件本身，writer 还要维护两个**控制状态**，它们决定「一个区间什么时候结束」：

- **turn 深度**（`turn_depth`）：前台被唤醒时 +1，回到事件循环时 -1。嵌套括号（输入分发中同步绘制）会让深度大于 1，只有最外层退出才考虑封口。
- **`ForegroundRunnableCounter`**：一个 `Arc<AtomicUsize>` 计数器，记录「已投递到主线程但还没跑完的 runnable 数」。它存在的意义是：**任务 A 刚轮询完、任务 B 已经排队**的那一瞬间，前台看起来「闲」了，但工作其实没断——这时不能插入 idle 边界，否则一次连续的堆积会被切成多个小区间，第二条判定标准（区间总占用）就永远凑不满预算。

#### 4.2.2 核心流程

```text
【入队侧】                                    【完成侧】
PlatformScheduler::schedule_local            平台调度器执行 runnable：
  foreground_runnables.queued()  (+1)          update_running_task   → begin_foreground_turn
  dispatcher.dispatch_on_main_thread(...)      runnable.run()
                                               save_task_timing      → record_task_poll:
【另一个入队侧】                                 ├─ finished()  (-1)
ForegroundExecutor::spawn_when_idle            ├─ poll 时长 ≥ 100µs ? 记 TaskPoll 事件
  queued()  (+1)                               │   否则折叠进 SmallPollFlush
  dispatch_on_main_thread_when_idle(...)       └─ end_turn(now)

【end_turn 的四道闸门】（journal.rs:414-434）
turn_depth > 0 ?               ── 是 → 不封口（还在嵌套括号里）
has_runnables() ?              ── 是 → 不封口（还有任务排队）
有未过期 pending 帧 ?           ── 是 → 不封口（等这帧画完呈现）
本区间有保留事件 ?              ── 否 → 丢弃折叠的小轮询，不封口
全部通过 → 写入 IntervalBoundary::Idle
```

#### 4.2.3 源码精读

先看计数器本体：[journal.rs:357-380](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L357-L380)。`finished` 用 `fetch_update` 配 `checked_sub(1)`：减到 0 再减就返回 `None`、原地不动。这是刻意的——后台线程也会调用 `save_task_timing`（它们各自的线程局部计数器从未被加过），用「不许减到负」的原子操作避免下溢。计数器本体是线程局部的（`FOREGROUND_RUNNABLES`，[journal.rs:524-527](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L524-L527)），但克隆出来的是 `Arc`，任何线程调用 `queued()` 加的都是**主线程那个**计数器。

入队侧的 +1 有两处。普通前台任务：[platform_scheduler.rs:118-123](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform_scheduler.rs#L118-L123)——`schedule_local` 先 `queued()` 再投递给平台。空闲期任务：[executor.rs:379-399](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/executor.rs#L379-L399)——`spawn_when_idle` 的自定义投递闭包里同样先计数。`ForegroundExecutor` 持有 counter 的克隆（字段声明在 [executor.rs:27-28](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/executor.rs#L27-L28)，构造时从 `PlatformScheduler` 拿：[executor.rs:305-314](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/executor.rs#L305-L314)）。注意测试构建里它是 `None`：[executor.rs:333-336](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/executor.rs#L333-L336)——确定性测试调度器不调用 profiler 括号，加了计数就没有配对的减法，idle 判定会被永久卡死。

完成侧就是那对全局函数：[profiler.rs:669-689](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler.rs#L669-L689)。`update_running_task` 在 profiler 构建下先 `journal::begin_foreground_turn()`（开启一个 turn 括号），再更新旧的每线程计时；`save_task_timing` 则把计时交给 `journal::record_task_poll`。而 `record_task_poll` 是事件与控制状态的汇合点：[journal.rs:636-646](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L636-L646)——先 `finished()` 减计数，再按 100µs 地板决定记整事件还是折叠，最后 `end_turn` 走四道闸门。

四道闸门本体：[journal.rs:414-434](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L414-L434)。配套的 pending 帧过期逻辑（`FRAME_DEADLINE` 一秒）在 [journal.rs:436-443](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L436-L443)：一个从不呈现的窗口（隐藏或被遮挡，收不到帧回调）不能永远堵住 idle 边界。`retained_since_boundary` 的注释（[journal.rs:407-413](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L407-L413)）解释了为什么「只有折叠小轮询的零星唤醒」要在真正空闲时丢弃：定时器、文件监听这类一微秒就完事的唤醒每次都封边界的话，被折叠掉的轮询又会以边界条目的形式回到环里，几秒钟就能把环写穿。

调度器侧的括号以 Linux 为例（u4-l3 走读过结构）：worker 线程在 [gpui_linux/src/linux/dispatcher.rs:41-48](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui_linux/src/linux/dispatcher.rs#L41-L48) 包住每个 runnable；macOS 的 trampoline 在 [gpui_macos/src/dispatcher.rs:172-174](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui_macos/src/dispatcher.rs#L172-L174)；Windows 在 [gpui_windows/src/dispatcher.rs:95-97](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui_windows/src/dispatcher.rs#L95-L97)。这正对应 u4-l4 总结的「四平台对称插桩」。

窗口侧的三类插桩（u4 系列第一次真正走进 `window.rs` 的 profiler 部分）：

- 首次失效追踪：`FrameDirtyAccumulator`（[window.rs:131-141](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/window.rs#L131-L141)），`invalidate_view` 在窗口「由干净变脏」的那一刻调用 `journal::record_frame_pending`：[window.rs:178-181](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/window.rs#L178-L181)，累计器在 [window.rs:246-256](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/window.rs#L246-L256)。
- 输入分发括号：`begin_input` 在 [window.rs:5011](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/window.rs#L5011)，`end_input` 在 [window.rs:5143](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/window.rs#L5143)；动作分发括号在 [window.rs:5589-5592](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/window.rs#L5589-L5592) 等四处。
- 帧括号：`draw` 开头取走累计器并 `begin_draw`（[window.rs:2853-2860](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/window.rs#L2853-L2860)），结尾 `end_draw(dirty_at, invalidations)`（[window.rs:2980-2986](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/window.rs#L2980-L2986)），`present` 用 `foreground_turn` 守卫包住提交并上报 `record_present`（[window.rs:3015-3028](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/window.rs#L3015-L3028)）。

这些括号在 `WindowProfiler`（[profiler.rs:893-938](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler.rs#L893-L938) 构造，同时创建四张直方图）里汇聚，每个 `begin_*` 都先 `journal::begin_foreground_turn()`、每个 `end_*` 都对应 `end_foreground_turn()`，例如输入对：[profiler.rs:942-980](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler.rs#L942-L980)，呈现上报：[profiler.rs:1046-1059](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler.rs#L1046-L1059) 与 [profiler.rs:1078-1125](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler.rs#L1078-L1125)。窗口销毁时还要 `record_window_closed` 解除 pending 帧对 idle 的堵塞：[profiler.rs:1141-1146](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler.rs#L1141-L1146)。

一个值得知道的隐患被源码注释明说了：turn 括号目前是裸的 begin/end 调用对而非 RAII 守卫，若有人在括号中间 `catch_unwind` 接住 panic，`turn_depth` 与计数器会永久失衡，idle 边界从此静默失效——Zed 遇 panic 直接 abort 所以暂无症状，见 [journal.rs:608-614](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L608-L614) 的 TODO（守卫类型 `ForegroundTurnGuard` 已经写好，在 [journal.rs:615-626](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L615-L626)，`window.rs` 的 `present` 已经换用了它）。

#### 4.2.4 代码实践

1. **实践目标**：把「一次鼠标点击」从平台事件到 journal 条目的完整链路画出来，标出途中每一次 turn 深度的加减。
2. **操作步骤**：
   - 从 [window.rs:5011](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/window.rs#L5011) 的 `begin_input` 出发向下读，找到 `PlatformInput::MouseDown` 的分发路径，确认点击处理器在 `end_input`（window.rs:5143）之前同步执行；
   - 若处理器里调用了 `cx.notify()`，继续追 `notify` → `invalidate_view`（window.rs:165-190）→ `record_frame_pending`，再追这帧的 `draw`/`present`；
   - 把链路上每个 journal 写入点（`Input` 事件、`FrameState::Pending`、`Draw` 事件、`Presented` 边界）按时间顺序列表。
3. **需要观察的现象**：一次点击至少产生 1 个 `Input` 事件 + 1 个 `FrameState` 条目；若窗口重画，还有 `Draw` 事件与 `Presented` 边界。
4. **预期结果**：一张五步链路图；能指出「点击处理器里 `thread::sleep(300ms)`」会被记成一条约 300ms 的 `Input` 事件（这正是综合实践的原理）。
5. 纯源码阅读即可完成，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：任务 A 轮询结束、任务 B 已在队列里的瞬间，writer 为什么不封 idle 边界？如果不做这个检查会发生什么？

**答案**：`end_turn` 的第二道闸门 `has_runnables()`（journal.rs:421）拦住了它。没有这个检查时，「A 完 → idle 边界 → B 开始」会把一段连续的堆积切成两个区间；每段的总占用都低于帧预算，第二条判定标准永远触发不了，连续小任务把帧拖掉的情况就漏报了。对应测试 `an_immediately_ready_runnable_prevents_an_idle_boundary_between_polls`（[journal.rs:1238-1271](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L1238-L1271)）。

**练习 2**：`queued()` 在后台线程被调用时，加的是哪个线程的计数器？为什么不会加错？

**答案**：加的是**主线程**的计数器。`PlatformScheduler` 在主线程构造（`ForegroundExecutor::new` 时），`foreground_runnable_counter()` 克隆的是主线程那个 `Arc<AtomicUsize>`（journal.rs:529-531）；线程局部只是「每线程有一个取之处的入口」，克隆出来的 `Arc` 指向的永远是构造时那个实例，跨线程 `fetch_add` 落在同一个原子量上。

**练习 3**：`FRAME_DEADLINE` 到期后，「饿死这帧的 hang」和「这帧本身」会被拆进两个区间吗？

**答案**：不会。过期只是**解除对 idle 边界的堵塞**（journal.rs:436-443），并不产生边界；区间仍然要等真正的呈现或空闲才封口，所以超时帧最终呈现时，饿死它的工作和它自己一起封在同一个 `FrameSnapshot` 里，`dirty_to_present` 关联得以保留。对应测试 `a_presentation_after_the_deadline_seals_the_whole_interval`（[journal.rs:1327-1353](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L1327-L1353)）。

### 4.3 ForegroundJournal 事件流：折叠、无锁环、IntervalSealer 与 FrameSnapshot

#### 4.3.1 概念说明

`journal.rs` 开头的模块文档（[journal.rs:1-13](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L1-L13)）把设计一页讲完：前台线程把任务轮询、动作、输入、绘制、呈现记成有界环里的事件流；**呈现与回到空闲是显式的边界条目**；独立的消费者把边界之前的所有工作归组成 `FrameSnapshot`，且封口器**不靠流逝时间、也不靠「碰巧是绘制」之类的事件种类来猜边界**——只认显式边界。注意这句话里的「独立的消费者」是复数的：4.4 的 `HangDetector` 只是最出名的那个，4.5 会看到 `gpui::bench` 成为第二个——而且它**根本不用封口器**，直接吃裸事件。

三个关键设计决定值得单独记：

1. **小轮询折叠**：短于 `TASK_POLL_FLOOR`（100µs）的轮询不单独成条目，而是折叠进 `SmallPollFlush`（保留精确个数与总时长、以及首尾张成的最窄时间跨度），在下一个被保留的事件或边界之前一次性冲刷。这让事件量上界由「慢轮询数」而不是「轮询总数」决定。
2. **完成顺序**：事件按**结束时刻**入流。跨着帧边界进行的工作（包住一次绘制的大轮询）在它结束时才记录，时间戳可能早于此前已记录的事件——消费者不得假设流内时间单调。
3. **无锁槽位**：写侧（前台）永不为读侧停留。槽位被读侧短暂钉住的罕见冲突下，条目先进一个 64 深度的 pending 队列下次再发；实在发不出去就记为丢失，用 `Discontinuity` 条目如实告知消费者「这里缺了 n 条，不要跨缺口推断」。

#### 4.3.2 核心流程

```text
【写入侧：ForegroundJournalWriter（主线程线程局部）】
事件到达 ──► 先冲刷挂着的 SmallPolls ──► JournalPublisher
                                             │
                                             ▼ （序号 = 逻辑位置）
                              JournalRing：无锁定长槽位数组
                              每槽：users(写位+读者数) + sequence + entry
                                             │
【读取侧：每 collector 独立游标】             ▼
collector.collect_unseen()
  ├─ 游标落后于保留窗口 → 头部插 Discontinuity{lost}
  ├─ 逐槽 try_read（序号不匹配也计 lost）
  └─ 返回 DrainedEntries{entries, lost}

【消费侧一：IntervalSealer（纯状态机，可在任意线程）→ HangDetector】
push_entries(entries):
  Event(SmallPolls) → 累积进 small_polls
  Event(其他)       → 累积进 events；空区间时推进 interval_start
  Boundary(Presented) → 先把 Present 事件补进 events，再封口
  Boundary(Idle)      → 直接封口（若区间非空）
  FrameState(_)       → 忽略（门控在写入侧已完成）
  Discontinuity{lost} → 记入 dropped_events / journal_discontinuous
封口 = 产出 FrameSnapshot 并把 interval_start 推进到边界结束时刻

【消费侧二：BenchReport（4.5，只吃裸 Event，不看边界）】
```

`FrameSnapshot::occupancy`（区间占用）的数学定义：事件的 `[start, end]` 区间**裁剪到统计窗口后取并集长度**（嵌套工作如输入分发内的绘制不重复计），加上折叠轮询的**按跨度比例分摊**：

\[ \text{flush 贡献} = \text{total} \cdot \frac{\lvert [\max(s, w_s),\, \min(e, w_e)] \rvert}{e - s} \]

（折叠轮询没有逐条时间戳，只能假设耗地在跨度上均匀分布。）忙碌比例则是：

\[ \text{busy\_fraction} = \min\left(1,\ \frac{\text{occupancy}}{\text{interval\_end} - \text{interval\_start}}\right) \]

#### 4.3.3 源码精读

三个体量常量划定了最坏情况的资源上界：[journal.rs:29-56](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L29-L56)——100µs 折叠地板、1 秒帧截止线、单区间 16384 条事件上限（防事件风暴的兜底）、4MiB 固定环（按注释的估算能装下数秒的最坏流量）、64 条 pending 队列。

六种前台事件：[journal.rs:58-79](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L58-L79)，统一的起止/时长访问器在 [journal.rs:81-113](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L81-L113)（注意 `SmallPolls` 的 `duration()` 是跨度而非耗时，注释特意提醒用 `summary.total` 算占用——4.5 的 bench 代码会再次踩在这个约定上）。折叠的数据结构 `PollSummary`/`SmallPollFlush`：[journal.rs:129-154](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L129-L154)。

环里实际存的条目有五类：[journal.rs:225-240](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L225-L240)——事件、边界、pending 帧状态变更（元数据，不是工作）、`Discontinuity`。两种边界（呈现的附带 `PresentedFrame`，含绘制与提交两侧计时；空闲只带时刻）在 [journal.rs:174-203](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L174-L203)，其中 `PresentedFrame::dirty_to_present_duration` 正是「用户等的这帧花了多久」。

无锁槽位的实现：[journal.rs:673-752](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L673-L752)。`users` 的最高位是写者锁、低位数读者；`try_publish` 用 CAS 从 0 抢到写位、写入条目、存序号、释放；`try_read` 先把读者数 +1（写者持有或读者满则失败），再校验序号后拷贝。环与序号取模定位在 [journal.rs:774-803](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L774-L803)。发布器在直接发布失败时入 pending 队列、每次发布前后冲刷队尾：[journal.rs:810-881](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L810-L881)。这套环有一个用参考模型做穷举对照的属性测试（`ring_matches_reference_model_under_wrap_collisions_and_independent_cursors`），连带一个手写的 `ModelRing` 参考实现：[journal.rs:2348-2448](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L2348-L2448)。

读取侧：`ForegroundJournal::collector()` 把游标定在当前的 `offered` 上（只看此后新记录的条目）：[journal.rs:905-912](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L905-L912)；`collect_unseen` 把游标夹到保留窗口、聚合丢失数、把读不到的槽位折叠成合并的 `Discontinuity` 标记：[journal.rs:940-967](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L940-L967)。「只看创建之后的条目」这个语义是 4.5 里 bench 排除 setup 的全部原理。

封口状态机：[journal.rs:970-1093](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L970-L1093)。几个精妙处：空区间遇到边界时不产快照、只把 `interval_start` 推到边界时刻（[journal.rs:1034-1036](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L1034-L1036)，即「前一段的空闲不计入下一个区间」）；`Presented` 边界先把呈现工作补进事件列表再封（[journal.rs:1026-1033](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L1026-L1033)）；`FrameState` 被无视（[journal.rs:1040-1043](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L1040-L1043)，门控在写入侧做过了，条目留在流里是给想看 dirty 时序的其他消费者用的）；区间事件数超限时优雅降级——把小轮询冲刷并进最后一条而不是丢掉耗时（[journal.rs:1065-1078](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L1065-L1078)）。

`FrameSnapshot` 本体与三个查询方法：[journal.rs:242-264](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L242-L264)（字段）、[journal.rs:297-345](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L297-L345)（`occupancy_within` 的区间并集 + 分摊实现）、[journal.rs:347-355](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L347-L355)（`busy_fraction`）。

#### 4.3.4 代码实践

1. **实践目标**：用 gpui 自带的 journal 测试验证本节的三个行为（折叠、边界封口、消费者互不干扰），并读懂其中最巧妙的一个。
2. **操作步骤**：
   - 在仓库根执行 `cargo test -p gpui --features profiler profiler::journal 2>&1 | tail -20`；
   - 打开 `sparse_small_polls_are_discarded_when_the_foreground_returns_to_idle`（[journal.rs:1540-1571](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L1540-L1571)）：它模拟「每秒一次 50µs 的零星唤醒」共 160 秒，断言环里**一条都没写**，且之后真正的保留事件不会继承这些折叠轮询；
   - 再对照 `draw_waits_for_its_presentation_boundary`（[journal.rs:1103-1159](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L1103-L1159)）：输入 + 绘制之后**没有**快照，`Presented` 边界一到才产出一个含 3 个事件的快照。
3. **需要观察的现象**：journal 测试全部通过（含两个 proptest 属性测试，首次运行可能多花几秒）。
4. **预期结果**：能口头复述「为什么 160 秒的零星唤醒一条不记」——它们都低于地板被折叠，而每次 end_turn 时区间里没有任何保留事件，`retained_since_boundary` 为假，折叠结果被整体丢弃（journal.rs:426-428）。
5. 测试运行结果**待本地验证**（不同机器上 proptest 用例集一致，应当稳定通过）。

#### 4.3.5 小练习与答案

**练习 1**：为什么事件要按**完成顺序**而不是开始顺序入流？

**答案**：前台工作会嵌套：一次输入分发可能同步触发绘制，一个任务轮询可能横跨帧边界。写入点只在工作**结束**时才掌握完整的首尾时间戳（比如 `end_draw` 才知道 `draw_start` 与 `draw_end`），按完成顺序写最自然；代价是流内时间戳不单调，消费者（封口器）必须自己用 `interval_start` 的推进规则处理重叠，而不是假设有序（journal.rs:60-63 的文档）。

**练习 2**：`IntervalSealer` 是纯状态机、不持有任何线程资源。这个设计带来了什么好处？

**答案**：它可以在**任意线程**运行——Zed 的生产接线正是把 `HangDetector`（内含 sealer）放进一个独立线程轮询（见 4.4.3），主线程只管写环；测试也可以直接手工构造条目序列喂给它（hang.rs 的大量单测就是这么写的），不需要起真实窗口。

**练习 3**：两个 `collector` 同时读同一个环，会互相影响吗？读取失败会把写侧拖住吗？

**答案**：不会。每个 collector 有独立游标（journal.rs:928-934），读侧只在 `try_read` 的瞬间把槽的读者数 +1、读完即减（journal.rs:715-751）；写侧碰到被钉住的槽不等待，改走 pending 队列或最终记丢失（journal.rs:53-56 的注释：前台永不为读者等待）。这正是 4.5 里 HangDetector 与 bench 两个消费者能同时挂在一条日志上的前提。

### 4.4 HangDetector 与 HangIncident：两条判定标准与序列化报告

#### 4.4.1 概念说明

`hang.rs` 的模块文档（[hang.rs:1-11](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L1-L11)）给出定义——hang 是满足以下**任一**条件的情形：

- **标准一（单事件）**：任何一段前台工作（任务轮询、动作处理器、输入分发、绘制、平台提交）阻塞前台达到 `threshold`；
- **标准二（帧预算）**：一个区间的**总前台占用**（事件并集 + 折叠轮询）达到 `frame_budget`，即使没有任何单个事件越过阈值——许多小块工作照样能掉帧，或者饿死一个无头应用。

检测是**事后**的：hang 在完成它的区间边界（呈现或空闲）出现之后才被报告；一直不交还前台的工作在被交还之前观察不到。检测器把每个含 hang 的完成区间报成一个 `HangIncident`（快照 + 贡献者列表）。

两条配套规则同样重要：

- **日志断点抑制标准二**：区间里出现 `Discontinuity` 时，跨缺口的总占用不可信，标准二不再触发；但**直接观察到的**超阈值事件（标准一）依然有效。
- **纯调度延迟不算 hang**：一帧从 dirty 到 present 很久、但期间前台几乎没干活（占用很低），说明是节流/调度问题而非应用卡顿——预算度量的是**前台花费**，不是 dirty-to-present 时长。

#### 4.4.2 核心流程

```text
HangDetector::new(journal, threshold, frame_budget)
  ├─ collector = journal.collector()      // 只看此后的事件
  └─ sealer = IntervalSealer::new(now)

poll()  ←（外部线程周期调用，Zed 生产为每秒一次）
  ├─ drained = collector.collect_unseen()
  ├─ 首个 Presented 边界 → 锁存 first_present_at（区分 startup/steady）
  ├─ snapshots = sealer.push_entries(drained.entries)
  └─ 对每个 snapshot 调 HangIncident::detect：

detect(snapshot, threshold, frame_budget):
  contributors = [e ∈ events | dur(e) ≥ threshold]     ── 标准一
  若空：
    ├─ snapshot.journal_discontinuous ? → None（标准二被抑制）
    ├─ occupancy(interval) < frame_budget ? → None
    └─ contributors = 全部事件                        ── 标准二
  按 duration 降序排序 → HangIncident{snapshot, contributors}
```

判定式可以写成：

\[ \text{incident} \iff \underbrace{\exists\, e:\ \mathrm{dur}(e) \ge T}_{\text{标准一}} \ \lor\ \underbrace{\Big(\mathrm{occ}(I) \ge B\ \land\ \neg\,\mathrm{discontinuous}\Big)}_{\text{标准二}} \]

其中 \(T\) 是 threshold、\(B\) 是 frame_budget、\(\mathrm{occ}(I)\) 是 4.3 的区间占用。

#### 4.4.3 源码精读

检测器本体很小：[hang.rs:29-48](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L29-L48)（collector + sealer + 两个阈值 + 首帧时刻），构造与轮询在 [hang.rs:50-89](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L50-L89)：`poll` 先排水、锁存首个呈现边界（[hang.rs:74-81](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L74-L81)，此后不再移动，对应测试 `first_present_at_latches_on_the_first_observed_presentation`），再把每个封口快照交给 `detect`。

两条标准的实现就集中在这一个函数：[hang.rs:393-425](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L393-L425)。标准一先筛（L404-409）；空则查断点（L410-413）再查预算（L414-417），通过后**全部事件**成为贡献者（L418，因为没有单个停滞能解释这个忙碌区间，报告要展示是什么填满了它）；最后按时长降序（L420）。

配套的两个「反例测试」把边界钉死：一个脏了很久但前台几乎没干活的帧**不是** incident（[hang.rs:808-837](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L808-L837)）；日志断点抑制预算推断但保留直接观察到的 hang（[hang.rs:769-803](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L769-L803)）。标准二的两个正例：呈现封口（[hang.rs:723-767](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L723-L767)）与空闲封口（无头应用场景，[hang.rs:842-865](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L842-L865)）；甚至只有折叠轮询也能凑满预算（[hang.rs:923-966](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L923-L966)）——此时贡献者列表为空，故事由 `small_poll_count/total` 承载。两条标准在任意参数组合下的行为有一个 proptest 对照模型穷举验证：[hang.rs:975-1051](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L975-L1051)。

`HangIncident` 的报告窗口 `active_window`（[hang.rs:370-391](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L370-L391)）：从「最早成因」（封口帧的首次失效时刻与最早贡献者开始时刻的较小者）到封口，把上一帧之后的空闲剪掉。

序列化形态 `SerializedHangIncident`（[hang.rs:92-142](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L92-L142)）面向 telemetry：毫秒时间戳、`phase`（活动窗口早于首个新帧提交结束则标 `startup`，否则 `steady`）、`sealed_by`（present/idle）、`busy_fraction`（低占用 + 高 dirty-to-present 指向节流而非应用工作）、贡献者截断上限。贡献者的**嵌套深度**（[hang.rs:298-310](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L298-L310)）表达了前台工作的包含关系——输入分发里同步画了一次窗口，报告会写成深度 0 的 Input 跟着深度 1 的 Draw，而不是两段无关的等长时间（对应测试 [hang.rs:872-917](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L872-L917)）。转换器在 [hang.rs:224-296](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L224-L296)，贡献者枚举在 [hang.rs:144-216](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L144-L216)。

最有说服力的验证是一个**真实 UI 上的随机注入测试**：渲染真实的元素树，通过生产分发路径（视图渲染、平台输入分发、动作分发、前台任务轮询）随机注入 15～35ms 的阻塞，断言检测器一个不漏地全部报出：[hang.rs:1100-1182](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L1100-L1182)。注意它的辅助函数：确定性测试调度器不会给 runnable 套 profiler 括号，所以测试**手动调用生产调度器会调的那对公共钩子**来模拟一次阻塞的前台轮询：[hang.rs:1230-1239](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L1230-L1239)。

生产接线在 Zed 主程序里，总共几十行：阈值按构建模式选择（release：单事件 100ms、帧预算 8ms——一帧 120Hz；debug：5s/30s 与 100ms，防止未优化构建天天误报）：[zed/src/reliability/hang_detection.rs:31-59](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/zed/src/reliability/hang_detection.rs#L31-L59)。检测器拿的就是 `cx.foreground_journal()`：[zed/src/reliability/hang_detection.rs:103-107](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/zed/src/reliability/hang_detection.rs#L103-L107)，运行在名为 `HangDetection` 的独立线程上、每秒 `poll()` 一次并序列化上报：[zed/src/reliability/hang_detection.rs:139-177](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/zed/src/reliability/hang_detection.rs#L139-L177)，退出前还有一次最终排水：[zed/src/reliability/hang_detection.rs:112-135](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/zed/src/reliability/hang_detection.rs#L112-L135)。同一文件还注册了 `dev::HangAction` 等三个调试动作，可在真实 Zed 里主动制造前台阻塞来试验：[zed/src/reliability/hang_detection.rs:61-68](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/zed/src/reliability/hang_detection.rs#L61-L68)。

#### 4.4.4 代码实践

1. **实践目标**：用 gpui 自带的 hang 测试套件验证两条判定标准及两条抑制规则，并从测试名反读出行为规范。
2. **操作步骤**：
   - 在仓库根执行 `cargo test -p gpui --features profiler profiler::hang 2>&1 | tail -20`；
   - 按含义给测试分四组：标准一（`a_hang_outliving_the_frame_deadline_keeps_its_frame_association`，[hang.rs:466-511](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L466-L511)）、标准二（`an_interval_of_small_work_over_budget_is_an_incident` 与 `an_idle_sealed_interval_over_budget_is_an_incident`）、抑制（`a_journal_gap_suppresses_budget_inference_but_retains_observed_hangs`）、反例（`a_slow_frame_with_little_foreground_spend_is_not_an_incident`）；
   - 挑 `serialized_incident_reports_presented_seal_fields`（[hang.rs:513-596](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L513-L596)），手工核一遍它的 `busy_fraction = 0.72` 断言：poll 150ms + action 40ms + input 5ms + present 20ms + 折叠 1ms = 216ms，活动窗口 300ms。
3. **需要观察的现象**：全部通过，其中 `detects_randomly_placed_foreground_hangs` 是 `#[gpui::test(iterations = 5)]`，会跑 5 轮随机注入。
4. **预期结果**：能对任意一个测试名说出「它锁定了哪条规则、若删掉对应代码哪个断言会挂」。
5. 测试运行结果**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：把生产参数（release：threshold=100ms、frame_budget=8ms）套到一个「每 4ms 一次、每次 5ms 的输入风暴」上，两条标准各自会怎么判？

**答案**：单事件 5ms < 100ms，标准一不触发；但一个区间内这些分发的占用会迅速累过 8ms 预算，标准二触发，**所有**事件都成为贡献者。这正是标准二存在的意义——「很多小块工作掉帧与一个大停顿一样彻底」（hang.rs:3-9 文档）。

**练习 2**：`first_present_at` 为什么只锁存一次、之后不再更新？

**答案**：它用来给 incident 打 `startup`/`steady` 相位标签——首个新帧提交完成之前开始的卡顿算启动期（用户还没看到任何画面，容忍度与归因都不同）。若随观测推进而漂移，旧 incident 的相位会来回翻转；锁存首个之后不变（hang.rs:74-81 的 `if self.first_present_at.is_none()`，测试 [hang.rs:659-684](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L659-L684)）。

**练习 3**：贡献者列表为什么既要「按时长降序」（`HangIncident.contributors`）又要在序列化时改成「按开始时刻升序」（`SerializedHangIncident::convert`）？

**答案**：内部表示把**最长阻塞**放首位，方便调用方直接取 `contributors[0]` 当作用户感知到的冻结时长（`stall_ms` 就取它，hang.rs:255-259）；序列化面向人读与遥测分析，按发生顺序排列并附上嵌套深度，输入分发里嵌着绘制这种因果关系才读得懂（hang.rs:276-284 的排序与 [hang.rs:872-917](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/hang.rs#L872-L917) 的断言）。

### 4.5 journal 的第二个消费者：gpui::bench 的 foreground_work 上报

#### 4.5.1 概念说明

7316cf7745（Report foreground executor work in bench reports）之前，`gpui::bench` 的报告只走**帧事件通道**：`TraceScope` 打开 `profiler::trace_scope()`（运行期开关那个通道），收集 `FrameEvent`（dirty-to-draw、draw、present interval），全部需要窗口参与。这留下一个盲区：**一个不产生任何绘制的前台慢任务**——比如防抖后的后台计算回前台结算、一次纯数据结构的重算——在 bench 报告里完全不可见，报告甚至因为没有任何帧事件而一个字都不打印。

本次改动把 `ForegroundJournal` 变成 bench 的第二个数据源。理解它的钥匙是与 `HangDetector` 对照，看同一事件流支撑的两种消费姿势：

| | HangDetector（4.4） | BenchReport（本节） |
| --- | --- | --- |
| 消费什么 | 边界封口后的 `FrameSnapshot` | 裸的 `ForegroundJournalEntry::Event` |
| 需要边界吗 | 需要（封口才产快照） | **不需要**（逐条计时） |
| 关心什么 | 区间占用、跨事件聚合 | 单个工作项的时长分布（直方图） |
| Draw/Present 事件 | 是一等公民（帧的组成部分） | **跳过**（帧通道已统计，避免双计） |
| SmallPolls 的量 | 按跨度比例分摊进 occupancy | 直接取 `summary.total` |
| 在哪个线程 | 独立线程周期轮询 | 测量线程内同步采集 |

三个实现细节值得预先点出（源码精读逐一对号）：

1. **collector 在测量起点创建**：`TraceScope::start` 现在接收一个 `ForegroundJournalCollector`，它由 `BenchAppContext::foreground_journal_collector()` 在同一时刻创建。collector 只观测创建之后的条目（4.3.3 讲过游标定在当前 `offered` 上），因此**每轮迭代的 setup 天然落在测量窗口之外**——不需要任何显式的「扣除」逻辑。
2. **Draw/Present 跳过、SmallPolls 取 total**：Draw 与 Present 已经被帧通道的 `record_frame_timings` 统计过，再计入就是双计；而 `SmallPolls` 的 `duration()` 是**跨度**不是耗时（4.3.3 特意提醒过的约定），所以取 `flush.summary.total`。
3. **DurationHistogram = 桶化直方图 + 精确总量**：hdrhistogram 3 位有效数字的桶化值做分位数足够、做总和有累积误差（2.4 的前置知识），所以新结构在直方图旁记一个精确的 `total_nanos`。

#### 4.5.2 核心流程

```text
#[gpui::bench] 宏展开（gpui_macros/src/bench.rs:131-152）
  └─ BenchAppContext::new_with_platform_and_report(platform, …, report.clone())
       └─ 用户函数内调用三个测量入口之一：

【bench_iter / bench_renderer：同步包裹整个测量】
  collector = TraceScope::start(self.foreground_journal_collector())
                │ ├─ profiler::trace_scope()        ← 打开旧的帧事件通道
                │ └─ journal_collector 游标 = 当前 offered  ← 本节新增
                ▼
  …被测代码运行；调度器的 update_running_task/save_task_timing 括号
    照常把每个前台工作写进 journal（4.2 的三类来源不变）…
                │
  events = collector.finish()   → TracedEvents { frame_events, journal_entries }
                │
  ├─ record_frame_timings(events.frame_events …)     ← 帧通道（原有）
  └─ record_foreground_events(events.foreground_events())   ← 新增
        对每条 Event：
          Draw | Present   → 跳过（帧通道已统计）
          SmallPolls(flush)→ 记 flush.summary.total
          其他             → 记 event.duration()
        DurationHistogram::record：直方图记一份 + total_nanos 累加精确值

【bench_task / bench_batched_task：TraceScope 生命周期跨 Criterion 批次】
  setup 例程：settle → setup() → TraceScope::start(collector)   ← collector 晚于 setup 创建
  测量例程：benchmark() → run_task_to_completion
  收尾：MeasuredTaskOutput::drop → finish → 两个 record_*

【报告出口】
  report.print()  → stderr 单独一段 "foreground executor work (…)"
  report.foreground_work() → Option<ForegroundWorkSummary>
```

帧预算超支计数的语义与 4.4 的标准二同源但更粗粒度：benchmark 没有 vsync，`budget_overruns` 把每个样本的时长除以「一帧预算」（按报告配置的 FPS，默认 120Hz → 8.33ms）取整累加，作为**错失帧数的代理指标**：

\[ \text{overruns}(d) = \left\lceil \frac{d - B}{B} \right\rceil \quad (d > B)，\quad \text{overruns\_total} = \sum_{\text{样本}} \text{overruns}(d) \]

#### 4.5.3 源码精读

模块门控先记住：`mod bench_context` 整体在 `bench` feature 之后（[app.rs:63-64](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app.rs#L63-L64)），而 `bench = ["test-support", "profiler", "dep:criterion"]`（[gpui/Cargo.toml:29](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/Cargo.toml#L29)）——所以 bench 构建里 journal 必然存在且常开。

`TraceScope` 是本次改动的枢纽：[bench_context.rs:368-401](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/bench_context.rs#L368-L401)。它现在多持一个 `journal_collector`（L381）；`start` 的参数就是 collector（L386）；`finish` 不再返回裸的 `Vec<FrameEvent>`，而是返回 `TracedEvents`（L395-400）。文档注释（[bench_context.rs:375-378](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/bench_context.rs#L375-L378)）明说了排除 setup 的原理：「collector 只观测其创建之后记录的条目，所以 scope 开始前记录的前台工作（例如每轮迭代的 setup）不会进入 `finish` 的结果」。

`TracedEvents` 与它的过滤器：[bench_context.rs:403-419](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/bench_context.rs#L403-L419)——`foreground_events()` 把 `Boundary`、`FrameState`、`Discontinuity` 全部滤掉，只留描述已完成工作的 `Event` 变体。

汇入点 `record_foreground_events`：[bench_context.rs:160-176](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/bench_context.rs#L160-L176)。逐行对照 4.5.1 的三个细节：L168 跳过 Draw/Present；L169-171 的注释与取值解释了「flush 的跨度（`duration()` 用的）不是轮询耗时，`summary.total` 才是」；其余走 `event.duration()`。

存储结构 `DurationHistogram`：[bench_context.rs:340-362](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/bench_context.rs#L340-L362)。文档第一句就是 2.4 讲的精度问题：桶化直方图（3 位有效数字）对总和的近似不如直接累加精确，所以 `record` 同时做两件事（L356-361）。它挂在 `WindowFrameSnapshot` 的新字段 `foreground_work` 上（[bench_context.rs:313-319](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/bench_context.rs#L313-L319)），并且参与 `is_empty` 判定（[bench_context.rs:332-337](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/bench_context.rs#L332-L337)）——这就是「无窗口 bench 也能打印报告」的实现：只要记录过前台工作，报告就不为空。

查询出口 `BenchReport::foreground_work`：[bench_context.rs:202-230](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/bench_context.rs#L202-L230)。返回的 `ForegroundWorkSummary`（结构体定义在 [bench_context.rs:69-93](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/bench_context.rs#L69-L93)）包含 count、精确 total、max、p50/p90/p95/p99，以及两个帧预算超支计数（total 按全部样本累加、max 只算最长那条）。文档再次强调关键语义：通过 journal 采集、**不需要窗口**、「即使运行期间什么都没画，也能暴露一个慢或停摆的任务」；没有任何前台工作时返回 `None`。

打印出口：`print` 在帧统计之后追加一段（[bench_context.rs:252](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/bench_context.rs#L252)），`print_foreground_work`（[bench_context.rs:264-276](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/bench_context.rs#L264-L276)）打印标题「foreground executor work (task polls, actions, input dispatch)」、一行「excludes window draw/present, reported separately above」的注释、精确 total，然后把直方图主体交给与帧统计共用的 `print_histogram_body`（[bench_context.rs:278-310](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/bench_context.rs#L278-L310)，这次重构把原 `print_histogram` 的函数体抽成了它）。

三个测量入口的接线：

- `foreground_journal_collector` 辅助方法：[bench_context.rs:617-621](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/bench_context.rs#L617-L621)——在当前 App 上取 `foreground_journal().collector()`；
- `bench_iter`：[bench_context.rs:630-640](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/bench_context.rs#L630-L640)——start 与 finish 包住 `bencher.iter`，之后先记帧事件再记前台事件；
- `bench_task`/`bench_batched_task`（共用 `bench_batched_task_internal`）：collector 的创建点在 setup 例程里、**晚于** `setup()` 调用（[bench_context.rs:698-703](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/bench_context.rs#L698-L703)）；TraceScope 本体放在 `MeasuredTaskOutput` 里，靠 `Drop` 收尾（[bench_context.rs:432-443](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/bench_context.rs#L432-L443)）——L438-441 依次 `finish`、记帧事件、记前台事件；
- `bench_renderer`：帧计时仍按 `window_id` 过滤（[bench_context.rs:767-772](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/bench_context.rs#L767-L772)），而前台工作**不**按窗口过滤——L773-776 的注释给出理由：前台工作不归属任何窗口，且 benchmark 应用同时只承载一个窗口，不会混入无关窗口的工作（[bench_context.rs:773-778](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/bench_context.rs#L773-L778)）。

journal.rs 侧仅有的变化是测试可见性：`install_test_foreground_journal` 与 `TestForegroundJournalGuard` 从 `pub(super)` 放宽为 `pub(crate)` 并补了文档（[journal.rs:574-598](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L574-L598)、[journal.rs:556-563](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/profiler/journal.rs#L556-L563)）——它给当前线程换上一条**全新的隔离日志**并在 guard drop 时还原，专门服务「本模块之外的测试」（文档点名 `bench_context`）：bench 测试不想让同一测试线程上先前测试残留的条目（或共享日志里的并发噪声）混进自己的测量。数据模型与 hang 检测未动。

三个新测试把行为钉死，全部用 `install_test_foreground_journal` 起步：

- `foreground_work_reports_long_task_without_window_draw`（[bench_context.rs:1129-1173](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/bench_context.rs#L1129-L1173)）：不起任何窗口，让一个前台任务睡 60ms，断言帧事件为空、而 `foreground_work()` 的 max/total 都 ≥55ms——「无绘制也上报」的直接证明。注释还解释了一个微妙点：`run_task_to_completion` 里观察任务完成的那个微型包装 poll 会折叠成第二个近似为零的样本，而不是被丢掉；
- `foreground_work_excludes_setup_before_trace_scope_starts`（[bench_context.rs:1175-1213](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/bench_context.rs#L1175-L1213)）：scope 创建**之前**跑一个 80ms 的 setup 任务、之后跑一个 10ms 的被测任务，断言 summary 的 max 与 total 都 <40ms——setup 不得泄漏进测量；
- `bench_task_reports_long_task_without_window`（[bench_context.rs:1215-1250](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/bench_context.rs#L1215-L1250)）：走完整的 `#[gpui::bench]` 生产路径（真 Criterion、真 `BenchAppContext`、`bench_task`），无窗口 spawn 一个 20ms 任务，断言报告的 max ≥15ms。

最后看宏与真实使用者：`#[gpui::bench]` 展开成 criterion 的 `bench_function` 包裹（[gpui_macros/src/bench.rs:131-152](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui_macros/src/bench.rs#L131-L152)）：创建 `BenchAppContext`（headless 平台）、调用户函数、`teardown`，最后 L151 的 `report.print(...)` 把含前台工作段的报告打到 stderr。`crates/benchmarks` 里的 [editor_render.rs:11-16](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/benchmarks/benches/editor_render.rs#L11-L16) 是现成样例：`bench_iter` 驱动多光标编辑（L42），`bench_renderer` 驱动帧级更新（L117）。

#### 4.5.4 代码实践

1. **实践目标**：验证「不产生任何绘制的前台任务也会被 bench 计时上报」，并亲手跑通三个新测试。
2. **操作步骤**：
   - 在仓库根运行三个新测试（注意必须带 `bench` feature，模块才存在）：
     `cargo test -p gpui --features bench bench_context::tests::foreground_work -- --nocapture 2>&1 | tail -15`；
   - 跑一个现有 benchmark，观察 stderr 报告的新段落（需要本地能编译 benchmarks crate 的依赖）：
     `cargo bench -p benchmarks --bench editor_render 2>&1 | tail -40`，在输出里找 `foreground executor work (task polls, actions, input dispatch):` 一段；
   - （可选进阶）在 `crates/benchmarks/benches/` 下新建一个最小 bench 文件（需在 `crates/benchmarks/Cargo.toml` 注册 `[[bench]]`，并仿照该 crate 现有 bench 的写法），核心只有几行（示例代码）：

     ```rust
     #[gpui::bench(sample_size = 10)]
     fn foreground_sleep_without_window(cx: &mut gpui::BenchAppContext) {
         cx.bench_task(|cx| {
             cx.foreground_executor().spawn(async move {
                 std::thread::sleep(std::time::Duration::from_millis(20));
             })
         });
     }
     ```

     运行后在报告的 foreground executor work 段核对 max 是否落在 20ms 量级。
3. **需要观察的现象**：三个测试通过；bench 报告出现独立的前台工作段，且标题下注明「excludes window draw/present」；纯 sleep 的 bench 没有任何帧统计，但前台工作段非空。
4. **预期结果**：能回答「改动前这个 bench 会打印什么」——什么都没有（`WindowFrameSnapshot::is_empty` 为真，报告整体跳过）。
5. 命令输出**待本地验证**（benchmarks crate 依赖较重，编译时间可能较长）。

#### 4.5.5 小练习与答案

**练习 1**：`record_foreground_events` 为什么跳过 `Draw`/`Present`，却要把 `SmallPolls` 单独处理成 `flush.summary.total`？

**答案**：Draw/Present 已经被帧通道的 `record_frame_timings` 统计（draw 耗时、dirty-to-draw、present interval），再计入前台工作就是双计，所以 `continue` 跳过（bench_context.rs:167-168）。`SmallPolls` 则是取值口径问题：`ForegroundEvent::duration()` 对 flush 返回的是首尾**跨度**（since 到 until），不是实际轮询耗时；真正的耗地在 `summary.total` 里（bench_context.rs:169-171 的注释）——这正是 4.3.3 里「用 summary.total 算占用」同一条约定在另一个消费者处的再现。

**练习 2**：`bench_task` 的 TraceScope 在 Criterion 的 setup 例程里创建、在输出值 drop 时结束。为什么「setup 不泄漏进测量」不需要任何显式扣除逻辑？

**答案**：因为排除是 collector 语义自带的：`TraceScope::start` 接收的 `ForegroundJournalCollector` 创建于 setup 之后（bench_context.rs:698-703），而 collector 的游标定在创建时刻的 `offered` 上（journal.rs:905-912），先前的条目对它不可见。测试 `foreground_work_excludes_setup_before_trace_scope_starts`（bench_context.rs:1175-1213）用 80ms setup + 10ms 被测任务验证了这一点。

**练习 3**：`bench_renderer` 里帧计时按 `window_id` 过滤，前台工作却不过滤。两者口径为何不同？

**答案**：`FrameEvent` 天然携带窗口归属（一次 draw/present 明确属于某个窗口），benchmark 应用可能先后给不同实体建窗，过滤保证只统计目标窗口的帧；而 journal 里的前台事件（任务轮询、动作、输入）不属于任何特定窗口——它们就是「前台线程忙了多久」，没有可过滤的归属字段。安全前提写在注释里：benchmark 应用一次只承载一个窗口，所以不会混入无关窗口的工作（bench_context.rs:773-776）。

## 5. 综合实践

把整讲串起来：写一个**卡顿探针窗口程序**，它既能打印前台日志条目，又能人为制造两类 hang，并用极小阈值把两类 `HangIncident` 都检测出来。

**第一步：创建示例文件**（读者在本地仓库操作，本讲不改动源码）。新建 `crates/gpui/examples/journal_probe.rs`，仿照 `crates/gpui/examples/window.rs` 的入口习惯。以下为示例代码：

```rust
// 示例代码：crates/gpui/examples/journal_probe.rs
#![cfg_attr(target_family = "wasm", no_main)]

use gpui::{
    profiler::hang::HangDetector, profiler::journal::IntervalBoundary, App, Context,
    InteractiveElement, IntoElement, MouseButton, ParentElement, Render, Styled, Window, div,
    prelude::*, px, size,
};
use gpui_platform::application;
use std::time::{Duration, Instant};

struct Probe {
    // 阈值 50ms（标准一）；帧预算 100ms（标准二）
    detector: HangDetector,
    startup: Instant,
}

impl Probe {
    fn new(cx: &App) -> Self {
        Self {
            detector: HangDetector::new(
                cx.foreground_journal(),
                Duration::from_millis(50),
                Duration::from_millis(100),
            ),
            startup: Instant::now(),
        }
    }
}

impl Render for Probe {
    fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        div()
            .flex()
            .flex_col()
            .gap_2()
            .p_4()
            .size_full()
            // 标准一：点击处理器内阻塞 300ms → 记成一条 ≥300ms 的 Input 事件
            .child(
                div()
                    .id("block")
                    .cursor_pointer()
                    .child("block 300ms（标准一）")
                    .on_mouse_down(MouseButton::Left, |_, _, _| {
                        std::thread::sleep(Duration::from_millis(300));
                    }),
            )
            // 标准二：3 段各 40ms 的前台任务轮询，每段都低于阈值，总和超预算
            .child(
                div()
                    .id("grind")
                    .cursor_pointer()
                    .child("grind 3x40ms（标准二）")
                    .on_mouse_down(MouseButton::Left, cx.listener(|_, _, _, cx| {
                        for _ in 0..3 {
                            cx.spawn(async move |_| {
                                std::thread::sleep(Duration::from_millis(40));
                            })
                            .detach();
                        }
                    })),
            )
            // 探针：排水并打印 incident 与最近的前台日志条目
            .child(
                div()
                    .id("probe")
                    .cursor_pointer()
                    .child("probe（打印 incident 与日志）")
                    .on_mouse_down(MouseButton::Left, cx.listener(|this, _, _, cx| {
                        for incident in this.detector.poll() {
                            let (start, end) = incident.active_window();
                            let sealed_by = match incident.snapshot.boundary {
                                IntervalBoundary::Presented(_) => "present",
                                IntervalBoundary::Idle { .. } => "idle",
                            };
                            println!(
                                "incident: 活动窗口 {:?}(约{:?}), sealed_by={sealed_by}, \
                                 贡献者={}（首因时长 {:?}）",
                                start.duration_since(this.startup),
                                end.duration_since(start),
                                incident.contributors.len(),
                                incident.contributors.first().map(|e| e.duration()),
                            );
                        }
                        let mut collector = cx.foreground_journal().collector();
                        let drained = collector.collect_unseen();
                        println!("新日志条目 {} 条，丢失 {}", drained.entries.len(), drained.lost);
                        for entry in drained.entries.iter().take(8) {
                            println!("  {entry:?}");
                        }
                    })),
            )
    }
}

fn run_example() {
    application().run(|cx: &mut App| {
        let probe = Probe::new(cx);
        cx.open_window(
            gpui::WindowOptions::default(),
            |_, cx| cx.new(|_| probe),
        )
        .unwrap();
        cx.activate(true);
    });
}

#[cfg(not(target_family = "wasm"))]
fn main() {
    run_example();
}
```

**第二步：构建并运行**（在仓库根，Linux 上依赖默认 wayland/x11 feature）：

```bash
cargo run -p gpui --example journal_probe --features profiler
```

**第三步：按序操作并观察**：

1. 点 `block 300ms`：窗口会冻结约 0.3 秒。这次点击的输入分发被记成一条约 300ms 的 `Input` 事件；点击引发重画，区间最终由 `Presented` 边界封口（或无重画时由 `Idle` 封口）。
2. 点 `probe`：应打印 1 个 incident，贡献者 1 个（`Input`，时长 ≈300ms ≥ 50ms）——**标准一**触发；`sealed_by` 取决于平台与是否有重画。
3. 点 `grind 3x40ms`：三次 `cx.spawn` 的任务在同一前台线程先后轮询，每段 ≈40ms，彼此之间因 runnable 排队计数而不封 idle 边界（4.2 的第二道闸门），整个区间在最后一段完成后由 `Idle`（或下一次呈现）封口。
4. 再点 `probe`：应打印 1 个 incident，贡献者 3 个 `TaskPoll`、每个 ≈40ms 都低于 50ms 阈值，但区间占用 ≈120ms ≥ 100ms 预算——**标准二**触发；打印出的日志条目里应能看到 `TaskPoll` 事件（40ms 远高于 100µs 折叠地板，不会被折叠）。
5. 对照打印的 `Debug` 输出阅读 `ForegroundJournalEntry` 的五个变体；多次点击后还会看到 `FrameState::Pending`/`Closed` 与 `Boundary` 条目。

**第四步（可选延伸，对应 4.5）**：换到 bench 通道再验证一遍同样的工作。按 4.5.4 的步骤写一个 `#[gpui::bench]` 的 `bench_task`，工作负载里**不建任何窗口**、只 spawn 一个睡 20ms 的前台任务，然后：

- 读 `BenchReport::foreground_work()`（count 至少 1、max ≈20ms），或直接看 stderr 报告里的 `foreground executor work` 段；
- 与第三步对照：probe 程序要靠「incident 报告」看到这次阻塞，bench 报告则在**没有任何帧事件**的情况下把它计成直方图里的一个样本——同一份 journal 数据，两个消费者各自的表达。

**预期结果**：两张 incident 报告分别对应两条判定标准，且每张的首因时长（300ms / 40ms）与人为注入吻合；第四步的 bench 报告在前台工作段显示 ≈20ms 的 max。若 `probe` 打印出 `Discontinuity`，说明消费太慢导致环被写穿——试着更频繁地点 `probe`。

**注意事项**：

- 严格按时序「先制造、后探测」；`HangDetector::new` 只观察创建之后的事件。
- 本示例未在本讲环境中实际运行，行为描述基于源码分析，**待本地验证**；若 `cargo run` 报 feature 或平台依赖问题，可退回 4.3.4/4.4.4/4.5.4 的纯测试实践。
- 想看真实 Zed 上的效果：debug 构建里触发 `dev` 命名空间的 `HangAction`（zed/src/reliability/hang_detection.rs:61-68 注册），日志里会出现「This should trigger a report」的提示。

## 6. 本讲小结

- **两层开关分工**：`profiler` feature（编译期）决定 journal/hang 模块与 `hdrhistogram` 是否存在；`set_trace_enabled`（运行期）只管旧的每线程任务环与 `FRAME_TIMINGS` 帧事件环。前台工作日志**常开**，Zed 生产构建无条件启用该 feature——hang 检测与 4.5 的 bench 上报都因此零开关可用。
- **三类插桩来源**：调度器在每个 runnable 前后的 `update_running_task`/`save_task_timing` 括号（四平台对称）、窗口的输入/动作分发括号（`WindowProfiler`）、帧的 dirty/draw/present 三步；它们共同维护 turn 深度与 pending 帧状态。
- **`ForegroundRunnableCounter` 防误切**：「已排队未执行」的原子计数堵住了「任务 A 完、任务 B 排队」瞬间的假空闲，保证连续堆积留在同一区间；`end_turn` 的四道闸门（嵌套深度、排队计数、未过期 pending 帧、本区间有保留事件）共同决定何时写 `Idle` 边界。
- **事件流模型**：六种 `ForegroundEvent`、100µs 地板下的小轮询折叠、按完成顺序入流的固定 4MiB 无锁环；`IntervalSealer` 纯状态机只认显式边界（`Presented`/`Idle`），把区间封成 `FrameSnapshot`，`occupancy` 是事件区间裁剪取并集加折叠轮询按跨度比例分摊。
- **两条判定标准**：单事件时长 ≥ threshold，或（无断点时）区间占用 ≥ frame_budget——后者把「许多小块工作掉帧」与纯调度延迟（dirty 久但占用低）区分开；`SerializedHangIncident` 用 `first_present_at` 区分 startup/steady 相位、用嵌套深度表达贡献者的包含关系。
- **bench 成为第二个消费者（7316cf7745）**：`TraceScope` 在测量起点同时创建 `ForegroundJournalCollector`（setup 因此天然排除），`finish` 返回 `TracedEvents`，`bench_iter`/`bench_task`/`bench_renderer` 把裸 `Event` 条目经 `record_foreground_events` 写入 `foreground_work` 直方图（Draw/Present 跳过、SmallPolls 取 `summary.total`、`DurationHistogram` 精确 total 配桶化分位数）；`BenchReport::foreground_work()` 与报告新段落让**无窗口的前台慢任务**第一次可测量。

## 7. 下一步学习建议

本讲到此完成了第 4 单元「调度与并发」的全部内容。后续建议：

1. **u5-l1（LinuxPlatform 与 LinuxClient）**：顺着 u4-l3 看过的调度器进入 gpui_linux 的三后端架构——本讲反复出现的「调度器括号」在 Wayland/X11 客户端主循环里的具体位置（如 wayland/client.rs 中 runnable 执行处）将在那里得到完整上下文。
2. **u8-l4（test-support 与可视化测试）**：本讲多次出现 `install_test_foreground_journal`、确定性测试调度器「不套 profiler 括号」的注意事项，都指向 gpui 的测试平台设施；那一讲系统讲解 `TestDispatcher`、`run_until_parked` 与时钟推进。
3. **拓展阅读**：`crates/zed/src/reliability/hang_detection.rs` 及其 `telemetry.rs`/`logging.rs` 子模块——几十行就把检测器接到独立线程与遥测，是「gpui 契约 + 精薄接线」分层的好范例；`journal.rs` 尾部的 proptest 参考模型（`ModelRing`/`ModelWriter`）则示范了如何用穷举对照测试锁死无锁数据结构的语义；`crates/benchmarks/benches/` 下的 `#[gpui::bench]` 用例（editor_render、markdown_renderer）演示了 4.5 的报告在真实工作负载上的样子。
