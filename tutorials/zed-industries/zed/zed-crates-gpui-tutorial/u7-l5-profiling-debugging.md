# u7-l5 性能剖析与调试工具

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 GPUI 在 `profiler` feature 下采集哪些性能数据（每线程任务计时、每窗口帧直方图、可选的逐帧事件环形缓冲），以及它们分别埋点在哪里。
2. 会用 `#[gpui::bench]` 宏写一个真正的 Criterion 基准：理解宏展开后发生了什么、`BenchAppContext` 提供了哪些测量入口（`bench_iter` / `bench_task` / `bench_renderer`）、`BenchReport` 报告里各列的含义。
3. 会用 `debug()` / `debug_below()` 两个调试样式给元素树画红框，并利用 hover 显示的元素 ID 与源码位置定位「这个 div 到底是哪行代码画出来的」。
4. 会打开并使用帧率悬浮面板（`DebugFrameOverlay`）与元素检查器（`Inspector`），理解它们为什么必须「绕过常规 UI 管线」来实现。

本讲是「专家层」的性能与调试专题。前置依赖是 u4-l3（窗口绘制管线：`cx.notify` → 标脏 → `Window::draw` → `present` 这条链路），因为本讲所有工具的埋点都挂在这条链路上。

## 2. 前置知识

- **绘制管线回顾（u4-l3）**：视图被 `cx.notify()` 标脏后，平台帧回调进入 `Window::draw`（重建元素树、走三阶段、生成 `Scene`），随后 `present` 把 `Scene` 提交给平台窗口（`platform_window.draw`）。本讲的帧计时正是围绕「draw 开始/结束」「present 开始/结束」这两对时间点展开的。
- **HDR 直方图（hdrhistogram）**：一种以对数分桶记录数值分布的结构，查询分位数（p50/p99）开销极低且内存有界。GPUI 用它统计「每帧 draw 耗时」「呈现间隔」等，避免为每个样本存原始数据。
- **分位数**：p99 表示「99% 的样本都不超过这个值」。性能分析关注高分位数（p99、max），因为掉帧恰恰由长尾样本造成。
- **Criterion**：Rust 最常用的基准测试框架，负责迭代次数校准、统计与对比。GPUI 不重造这部分，而是提供 `#[gpui::bench]` 把「一个带真实 GPUI 运行时的环境」接入 Criterion。
- **`debug_assertions` 与 feature 门控**：本讲的调试工具多数只在 `#[cfg(debug_assertions)]` 或 `profiler` / `inspector` feature 下编译。debug 构建（默认 `cargo run` / `cargo test`）自动满足 `debug_assertions`；release 构建需显式开 feature。
- **`#[track_caller]` 与 `panic::Location`**：Rust 的机制，让函数拿到「调用者的源码位置」。GPUI 用它在 debug 模式下记录每个 div 的构造位置，这是检查器能跳转源码的基础。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/profiler.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs) | 性能采集核心：每线程任务计时与 top-5 统计、`WindowProfiler` 帧直方图、trace 开关、帧事件环形缓冲。`journal` / `hang` 两个子模块（u7-l6 详解）也挂在这里 |
| [src/app/bench_context.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/bench_context.rs) | 基准测试上下文：`bench_platform`、`BenchAppContext`、`BenchReport` |
| [src/debug_overlay.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/debug_overlay.rs) | 帧率悬浮面板：自己用点阵字模画统计数字，绕过文本系统 |
| [src/inspector.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/inspector.rs) | 元素检查器状态与注册表 |
| [src/style.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/style.rs) | `DebugBelow` 全局标记、`StyleRefinement` 的 debug 字段、`Style::paint` 里的红框绘制 |
| [src/styled.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/styled.rs) | `debug()` / `debug_below()` 链式方法 |
| [src/elements/div.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/elements/div.rs) | `Interactivity` 捕获构造位置；hover 时显示元素 ID 与源码位置 |
| [src/window.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs) | 全部接线：`window_profiler` 字段、draw/present/输入/动作的埋点、overlay 与 inspector 的公开 API |
| [src/element.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/element.rs) | `Element` trait 三阶段签名中的 `inspector_id` 参数与 `source_location` 方法 |
| crates/gpui_macros/src/bench.rs | `#[gpui::bench]` 过程宏的实现 |
| crates/benchmarks/benches/editor_render.rs | 仓库中真实使用 `#[gpui::bench]` 的范本 |

## 4. 核心概念与源码讲解

### 4.1 profiler：任务与帧的采样统计

#### 4.1.1 概念说明

`profiler.rs` 解决的问题是：**UI 卡顿时，到底是谁在主线程上耗时间？** 它采集三类互补的数据：

1. **每线程任务计时（task timings）**：前台/后台执行器每次轮询一个 future，都记下「在哪轮询、何时 spawn、何时开始、何时 yield」。既保留 top-5 最差任务的统计（常态开启），也可保留完整环形缓冲（开 trace 后）。
2. **每窗口帧直方图（`WindowProfiler`）**：只要编译了 `profiler` feature 就持续累计——draw 耗时直方图、动画呈现间隔直方图、输入到成帧的延迟直方图。
3. **逐帧事件缓冲（`FRAME_TIMINGS`）**：仅在 `set_trace_enabled(true)` 期间记录的 `FrameEvent::Draw / Present` 环形缓冲，供基准测试与外部工具增量拉取。

一个关键设计：**统计与明细分层**。top-5 与直方图是「总是收集、极便宜」的；完整事件流是「按需开启」的，由一个原子变量 `TRACE_STATE` 控制，热路径上用 `std::hint::cold_path()` 提示分支预测器优化「未开启」的常见情形。

`journal` / `hang` 两个子模块（[src/profiler.rs:19-23](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L19-L23)）是后来加入的「前台工作日志 + 卡顿检测」子系统：`WindowProfiler` 的各埋点现在会**同时**向 journal 双写（例如 `begin_input` 里调用 `journal::begin_foreground_turn()` 与 `journal::record_input(...)`）。这属于 u7-l6 的主题，本讲只需知道「直方图统计之外还有一路日志输出」。

#### 4.1.2 核心流程

任务计时链路（挂在执行器轮询路径上）：

```text
executor 轮询一个 future
  → profiler::update_running_task(spawned, location)   # 记录 ActiveTiming（开始）
  → future 真正执行（耗时发生在这里）
  → profiler::save_task_timing()                        # 结算成 TaskTiming
      ├─ stats.add_yield_timing / add_runtime           # top-5 统计（feature 下常态收集）
      └─ trace_enabled() 时 push 进线程局部环形缓冲      # 明细（默认不收集）
```

帧计时链路（挂在 window.rs 的 draw/present 上）：

```text
Window::draw   → window_profiler.begin_draw()            # 记录 draw 起点
  ... 元素树三阶段 ...
              → window_profiler.end_draw(dirty_at, invalidations)
                  ├─ draw_duration_histogram.record()    # 常态
                  ├─ record_frame_event(Draw)            # 仅 trace 开启时
                  └─ journal 双写
Window::present → window_profiler.record_present(start, end, ...)
                  ├─ 输入延迟直方图（首个输入 → present 结束）
                  ├─ 动画间隔直方图（连续动画帧之间的 present 间隔）
                  └─ record_frame_event(Present)         # 仅 trace 开启时
```

帧预算超支数的计算（`BenchReport` 用它把「耗时」翻译成「丢了几帧」）：

\[ \text{overruns}(t) = \left\lceil \frac{t - B}{B} \right\rceil \quad (t > B) \]

其中 \( B \) 是单帧预算，默认 \( B = 1\,\text{s} / 120 \approx 8.33\,\text{ms} \)。

#### 4.1.3 源码精读

**任务计时的数据结构**。`TaskTiming` 用 `core::panic::Location` 携带「哪一行代码 spawn 的任务」，四个时间点区分了「单次轮询耗时」（poll_duration）与「从 spawn 到结束的总时长」（since_spawn）：

- [src/profiler.rs:76-83](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L76-L83) 定义 `TaskTiming`；[src/profiler.rs:116-124](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L116-L124) 的 `poll_duration` / `since_spawn` 是两个口径。

每个线程一块 `ThreadTimings`（线程局部 + 全局弱引用注册表，[src/profiler.rs:533-563](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L533-L563)），内部装着环形缓冲、正在运行的任务和 top-5 统计。

**top-5 统计的「准入门槛」设计**。[src/profiler.rs:455-466](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L455-L466) 中 `TaskStatistics::default` 把 `poll_time_to_beat` / `runtime_to_beat` 初始化为 100 微秒——比它慢的任务才有资格进入 top-5 榜单：

- [src/profiler.rs:468-509](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L468-L509) 的 `add_yield_timing` / `add_runtime`：先比较门槛，超过才进冷路径替换榜单中最差者，并抬高门槛到当前榜单最小值。绝大多数任务一次比较就被挡在热路径外。

**trace 开关**是一个原子状态字，最高位是「开关」，低 63 位是嵌套的 `trace_scope()` 计数（基准测试用它保证嵌套测量不会提前关掉 tracing）：

- [src/profiler.rs:696-735](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L696-L735) `set_trace_enabled`：从开到关时会清空各线程缓冲与帧缓冲，避免陈旧数据在下次开启后被当作新数据读出。
- [src/profiler.rs:613-620](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L613-L620) `ThreadTimings::save_task_timing`：只有 `trace_enabled()` 时才 push 进环形缓冲；缓冲上限 16MiB（[src/profiler.rs:404-408](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L404-L408)），满了就弹掉最老的。

**帧计时的数据结构**。[src/profiler.rs:786-800](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L786-L800) 的 `FrameTiming` 记录单帧 draw：`dirty_at`（首次标脏时刻）、`invalidations`（合并了几次失效）、`draw_start` / `draw_end`。配合 [src/profiler.rs:804-815](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L804-L815) 的两个口径：`draw_duration`（纯绘制耗时）与 `dirty_to_draw_duration`（从数据变化到画完的完整延迟）。

[src/profiler.rs:818-838](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L818-L838) 的 `PresentTiming` 现在记录 `present_start` / `present_end` 两个时刻（此前只有单个时刻），并提供 `present_duration()`；`animation_interval` 只在「前后两帧都属于连续动画」时才有值——静止窗口的重复 present 不会污染帧率统计。

**`WindowProfiler` 的埋点方法**（每个窗口一个实例，存在 [src/window.rs:1187](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L1187) 的 `window_profiler` 字段）：

- [src/profiler.rs:1009-1015](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L1009-L1015) `begin_draw` 与 [src/profiler.rs:1018-1040](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L1018-L1040) `end_draw`：结算出 `FrameTiming`，写入直方图、journal，并在 trace 开启时发出 `FrameEvent::Draw`。
- [src/profiler.rs:1046-1059](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L1046-L1059) `record_present` 与 [src/profiler.rs:1078-1125](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L1078-L1125) `record_present_at`：结算输入延迟（`first_input_at` → `present_end`）、每帧合并的输入事件数、动画呈现间隔。
- [src/profiler.rs:942-980](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L942-L980) `begin_input` / `end_input`：输入派发的计时，`kind` 参数来自 `PlatformInput::kind_name()`（如 `"key_down"`、`"scroll_wheel"`，u5-l1 介绍过）。绘制中途到达的输入不计入延迟统计（`mid_draw_events_dropped`），因为它们无法归因到本帧。
- [src/profiler.rs:983-1006](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L983-L1006) `begin_action_handler` / `end_action_handler`：动作处理器计时。注意 `end_action_handler` 里的注释——legacy 聚合存储只有一个全局 running 槽位，在测试并发跑动作时会失真，所以 journal 条目改由窗口侧跟踪。

**window.rs 侧的接线**：

- [src/window.rs:2860](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L2860) `Window::draw` 开头调用 `begin_draw()`；[src/window.rs:2980-2986](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L2980-L2986) 帧末 `end_draw(...)` 的返回值同时喂给了 `debug_frame_overlay.record_frame`（4.3 节）。
- [src/window.rs:3015-3031](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L3015-L3031) `present`：在 `platform_window.draw(&scene)` 前后取 `present_start` / `present_end`，交给 `record_present`。
- [src/window.rs:5009-5011](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L5009-L5011) `dispatch_event` 开头的 `begin_input(event.kind_name())`。

**帧事件的读取侧**：[src/profiler.rs:1164-1180](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L1164-L1180) `record_frame_event`（未开 trace 直接返回）与 [src/profiler.rs:1185-1222](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L1185-L1222) `FrameTimingCollector`——后者带游标增量拉取，环形缓冲绕回时被弹出的条目会丢失（文档明确说明）。任务计时也有对应的 `ProfilingCollector`（[src/profiler.rs:337-402](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L337-L402)），供 Zed 编辑器的调试面板使用。

窗口还对外暴露直方图快照：[src/window.rs:3046-3055](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L3046-L3055) 的 `input_latency_snapshot` / `frame_duration_snapshot`。

#### 4.1.4 代码实践

**实践目标**：亲手读出「输入到成帧」的延迟分布，验证 4.1.2 的链路描述。

操作步骤（源码阅读 + 小实验，示例代码为读者自写）：

1. 用 `profiler` feature 跑一个带交互的示例：`cargo run -p gpui --example opacity --features profiler`。
2. 在该示例的根视图 `render` 里（或任何能拿到 `window` 的回调里）周期性打印两个直方图快照的分位数（**示例代码**）：

   ```rust
   // 示例代码：放在某个定时回调里，例如每 5 秒触发一次
   let snapshot = window.frame_duration_snapshot();
   println!(
       "draw p50={:?} p99={:?} max={:?}",
       Duration::from_nanos(snapshot.draw_duration_histogram.value_at_quantile(0.50)),
       Duration::from_nanos(snapshot.draw_duration_histogram.value_at_quantile(0.99)),
       Duration::from_nanos(snapshot.draw_duration_histogram.max()),
   );
   ```

3. 连续拖动滑块制造重绘，观察打印输出的 p50 与 max 的差距。

需要观察的现象：静止时样本几乎不增长；快速交互时 p99/max 明显抬升。

预期结果：能读到非空的 draw 直方图样本；`input_latency_snapshot` 在发生过鼠标交互后也有样本。具体数值因机器而异——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TaskStatistics` 要设 100µs 的「准入门槛」，而不是把所有任务计时都存下来？

答案：热路径优化。绝大多数任务轮询极快，不值得记录；用 `runtime_to_beat` 先做一次比较，把「维护榜单」的工作放进 `std::hint::cold_path()` 标注的冷路径，采集开销对正常任务接近一次比较。同时 top-5 榜单是有界的，不会随运行时间增长。

**练习 2**：`FrameTiming::draw_duration` 与 `dirty_to_draw_duration` 有什么区别？各自回答什么问题？

答案：`draw_duration = draw_end - draw_start`，回答「绘制这一帧本身花了多久」（绘制管线效率）；`dirty_to_draw_duration = draw_end - dirty_at`，回答「从数据第一次变化到画完等多久」（含调度延迟、合并等待）。前者大说明绘制慢，后者大但前者小说明帧被推迟了（比如别的任务占着主线程）。

**练习 3**：`PresentTiming::animation_interval` 为什么是 `Option`，而不是每次 present 都记录间隔？

答案：见 [src/profiler.rs:1096-1103](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/profiler.rs#L1096-L1103)——只有「上一帧 present 时窗口在动画中、本帧是新绘制的帧、窗口活跃」三个条件同时满足才计算间隔。否则静止窗口的重复 present（内容未变也会 present）会把「无意义的呈现间隔」混进帧率统计。

### 4.2 `#[gpui::bench]` 与 BenchAppContext

#### 4.2.1 概念说明

GPUI 的基准测试建立在 Criterion 之上，补齐的是「运行环境」：普通 Criterion 基准只有裸函数，而 GPUI 基准需要 App、窗口、真实文本系统、甚至真实 GPU 渲染。三个组件分工：

- `#[gpui::bench]`（过程宏）：把一个 `fn xxx(cx: &mut BenchAppContext)` 改写成 Criterion 需要的 `fn xxx(criterion: &mut Criterion)`，自动搭建/拆除 GPUI 环境。
- `bench_platform`：用当前线程共享的 `ThreadedDispatcher` 造一个「真实时间、真实并发」的测试平台——不同于 u7-l4 测试平台的假时钟，基准要测真实耗时。
- `BenchAppContext`：持有独立的 App 实例，提供 `bench_iter`（测闭包）、`bench_task`（测异步任务）、`bench_renderer`（测「更新实体 → 同步画一帧 → present」的帧延迟）三种测量入口，并把测量期间产生的帧事件汇入 `BenchReport`。

feature 关系（[Cargo.toml:29-30](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/Cargo.toml#L29-L30)、[Cargo.toml:40](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/Cargo.toml#L40)）：`bench = ["test-support", "profiler", "dep:criterion"]`，`profiler = ["dep:hdrhistogram"]`。即基准天然带着帧 profiler，`BenchReport` 才有数据可收。

#### 4.2.2 核心流程

宏展开（无 `inputs` 的简单形式）：

```text
#[gpui::bench] fn foo(cx: &mut BenchAppContext) { ... }
  ↓ 展开为
fn __gpui_bench_foo(cx: &mut BenchAppContext) { ... }     # 原函数体改名
fn foo(criterion: &mut criterion::Criterion) {
    let report = BenchReport::default();                   # 或 with_fps(N)
    criterion.bench_function("foo", move |bencher| {
        let mut cx = BenchAppContext::new_with_platform_and_report(
            bench_platform(当前平台 headless 渲染器, 当前平台文本系统),
            Some("foo"), bencher, report.clone());
        __gpui_bench_foo(&mut cx);                         # 你的 setup + 测量
        cx.teardown();                                     # 取消残留定时器
    });
    report.print(Some("foo"));                              # stderr 打印帧报告
}
```

测量期间的 tracing 由 `TraceScope`（RAII）管理：进入时 `trace_scope()` 计数 +1（开启收集），Drop 时计数 -1，归零自动清缓冲——即使基准 panic 也不会把 tracing 留给同进程的下一个基准。

#### 4.2.3 源码精读

**过程宏**。[gpui_macros/src/bench.rs:131-153](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_macros/src/bench.rs#L131-L153) 是无 `inputs` 路径的展开模板；[gpui_macros/src/bench.rs:11-49](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_macros/src/bench.rs#L11-L49) 解析五个可选参数：`fps`（帧预算）、`inputs`（多组输入）、`input_name` / `group`（分组命名）、`sample_size`。宏只支持同步函数（async 直接报错，[gpui_macros/src/bench.rs:63-68](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui_macros/src/bench.rs#L63-L68)）。

入口配套宏在 [src/gpui.rs:114-136](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/gpui.rs#L114-L136)：`bench_group!` / `bench_main!` 只是 `criterion_group!` / `criterion_main!` 的别名，保持文件形状与普通 Criterion 一致。

**`bench_platform`**。[src/app/bench_context.rs:40-59](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/bench_context.rs#L40-L59)：dispatch 用线程局部缓存复用（worker/timer 线程跨基准存活，不被 Criterion 校准阶段反复重建）；文本系统用当前平台的真实实现，文本密集型基准包含真实的整形与光栅化；headless 渲染器目前只有 macOS 提供（Metal），其他平台上 present 会丢弃场景——**GPU 提交在非 macOS 平台不计入测量**，读跨平台基准数据时要记住这一点（文档注释 [src/app/bench_context.rs:33-39](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/bench_context.rs#L33-L39)）。

**`BenchReport`**。[src/app/bench_context.rs:96-129](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/bench_context.rs#L96-L129) `record_frame_timings` 把 `FrameEvent` 流分拣进四张直方图（dirty→draw、draw、present 间隔、每帧失效次数）；[src/app/bench_context.rs:145-153](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/bench_context.rs#L145-L153) `budget_overruns` 用 4.1.2 的公式换算丢帧数——基准环境没有 vsync，所以这是「假设按 120fps 消耗帧预算会漏几帧」的合成指标；[src/app/bench_context.rs:156-214](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/bench_context.rs#L156-L214) `print` 输出到 stderr，逐直方图打印 samples / mean / p50 / p90 / p95 / p99 / max / 超支数。

**三个测量入口**：

- [src/app/bench_context.rs:461-475](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/bench_context.rs#L461-L475) `bench_iter`：最通用，闭包每个 Criterion 迭代调用一次，触发到的窗口绘制自动进报告。
- [src/app/bench_context.rs:486-510](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/bench_context.rs#L486-L510) `bench_task` / `bench_batched_task`：测异步任务到完成；每次迭代独立成批并先 `settle()`（[src/app/bench_context.rs:435-447](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/bench_context.rs#L435-L447)），把上一迭代丢弃的实体彻底释放，防止跨迭代累积。
- [src/app/bench_context.rs:552-607](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/bench_context.rs#L552-L607) `bench_renderer`：测「更新视图 → effect 冲刷时同步画脏窗口 → `present_if_needed` 提交」的完整帧路径，只统计目标窗口的帧事件。`present_if_needed`（[src/window.rs:3038-3043](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L3038-L3043)）就是为这种「不走平台帧循环、同步驱动」的场景准备的。

**真实范本**。[crates/benchmarks/benches/editor_render.rs:11-65](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/benchmarks/benches/editor_render.rs#L11-L65) 展示了带 `inputs` / `group` / `sample_size` 的完整用法：setup 建编辑器与窗口，`cx.bench_iter(...)` 里做输入处理；同文件 [crates/benchmarks/benches/editor_render.rs:88-110](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/benchmarks/benches/editor_render.rs#L88-L110) 的 `editor_render` 则用 `window.replace_root` 把编辑器设为根视图后测渲染。benchmarks crate 的声明见 [crates/benchmarks/Cargo.toml:26](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/benchmarks/Cargo.toml#L26)（`gpui` 开 `bench` feature）与 [crates/benchmarks/Cargo.toml:48-50](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/benchmarks/Cargo.toml#L48-L50)（`harness = false`，Criterion 自带 main）。

#### 4.2.4 代码实践

**实践目标**：写一个基准测量「渲染一万个 div 子元素」的帧耗时，为综合实践做铺垫。

操作步骤（**示例代码**，读者可在本地仓库的 `crates/benchmarks/benches/` 下新建文件并在其 `Cargo.toml` 加一个 `[[bench]]` 条目来运行）：

1. 新建 `crates/benchmarks/benches/div_render.rs`（示例代码）：

   ```rust
   use gpui::{AppContext as _, BenchAppContext, Context, ElementId, Render, Window, div, px, rgb};

   struct Grid {
       count: usize,
       generation: usize,
   }

   impl Render for Grid {
       fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
           div()
               .id("root")
               .size_full()
               .flex_wrap()
               .children((0..self.count).map(|i| {
                   // generation 参与 id，保证每次重绘子元素 id 变化，
                   // 缓存与 hitbox 都无法复用上一帧结果
                   div()
                       .id(ElementId::NamedInteger(
                           format!("cell-{}", self.generation).into(),
                           i as u64,
                       ))
                       .size_px(px(8.))
                       .bg(rgb(0x888888))
               }))
       }
   }

   #[gpui::bench]
   fn render_ten_thousand_divs(cx: &mut BenchAppContext) {
       let mut window = cx.add_empty_window();
       // 把 Grid 设为根视图，保存返回的句柄用于驱动重绘
       let grid = window.update(|window, cx| {
           window.replace_root(cx, |_, _| Grid { count: 10_000, generation: 0 })
       });
       cx.bench_iter(|_| {
           window.update(|_, cx| {
               grid.update(cx, |grid, cx| {
                   grid.generation += 1;
                   cx.notify();
               });
           });
       });
   }
   ```

   注：`Window::replace_root` 的签名为 `(&mut self, cx: &mut App, build_view) -> Entity<E>`（[src/window.rs:1987-1994](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L1987-L1994)）；bench 闭包里的 `window.update(...)` 借用的是 `BenchWindowContext::update`（闭包拿到 `(&mut Window, &mut App)`）。

2. 在 `crates/benchmarks/Cargo.toml` 追加：

   ```toml
   [[bench]]
   name = "div_render"
   harness = false
   ```

3. 运行：`cargo bench -p benchmarks --bench div_render`。

需要观察的现象：终端先输出 Criterion 的常规统计，随后 stderr 打印 `GPUI bench report (all observed iterations): render_ten_thousand_divs`，列出 window dirty-to-draw / window draw 两张直方图与 frame budget overruns。

预期结果：draw 直方图的 p99/max 显著高于 p50；`frame budget overruns` 大于 0，说明一万个 div 的整树重建超出了 120fps 的 8.33ms 帧预算。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `BenchAppContext` 要与 `TestAppContext` 分开，而不是给测试上下文加测量方法？

答案：见 [src/app/bench_context.rs:313-318](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/bench_context.rs#L313-L318) 的类型文档——两者需求相反：测试要确定性（假时钟、可控调度），基准要真实耗时（`ThreadedDispatcher` 实时并发）。混在一起会让「确定性调度」的桩污染计时。

**练习 2**：`bench_renderer` 每次迭代里为什么要显式调用 `present_if_needed`？

答案：基准不跑平台帧请求循环，effect 冲刷只会同步画出脏窗口，不会触发 present；不主动 present 的话（a）帧事件流里缺 Present 半边、（b）有 headless 渲染器时场景不会真正提交到 GPU，测量就漏掉了这部分真实成本（[src/app/bench_context.rs:590-597](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/bench_context.rs#L590-L597) 的注释明确了这是「镜像生产环境每帧必 present」）。

**练习 3**：`teardown` 里为什么要循环 `cancel_pending_timers`（[src/app/bench_context.rs:656-681](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/bench_context.rs#L656-L681)）？

答案：dispatch 是跨基准复用的线程局部共享对象。上一个基准遗留的定时器若不取消，会在后续基准测量中途醒来执行无关工作，污染别人的计时；取消本身会唤醒被它阻塞的任务，所以要循环「取消 + 排空」直到没有残留，超过 100 轮直接 panic 暴露问题。

### 4.3 debug_overlay：帧率悬浮面板

#### 4.3.1 概念说明

`DebugFrameOverlay` 是画在窗口右上角的绿色帧耗时读数。它的特殊之处写在模块文档第一行：**完全绕过布局、文本与视图失效，直接往 `Scene` 里塞图元**。原因很直接——如果它用 div + 文本实现，那么「显示上一帧耗时」这个动作本身就会标脏视图、触发新帧，形成无限自激励的绘制循环。所以它自带一套 5×7 点阵字模，用纯 `Quad` 拼 数字。

三种模式循环切换：`Hidden → Minimal（只显示当前帧耗时）→ Full（CUR/1%/10%/MAX/FRAMES 五行）`。

#### 4.3.2 核心流程

```text
每帧 Window::draw 结束
  → end_draw 返回 draw_duration
  → overlay.record_frame(draw_duration)          # 滚动窗口保留最近 1000 个样本
  → draw_roots 之后、finish 之前：
    overlay.paint(&mut next_frame.scene, ...)    # 直接向场景插入面板与文字 quad
用户切换模式
  → window.cycle_debug_frame_overlay_mode()      # set_mode + refresh()
```

Full 模式各行的含义：`CUR` 最新一帧；`1%` / `10%` 是把样本升序排序后的高分位数（下标 \((n-1) \times 99/100\) 与 \((n-1) \times 90/100\)）；`MAX` 最差样本；`FRAMES` 自面板存在以来的总帧数（超过 99999 显示 `LOTS`）。

#### 4.3.3 源码精读

- [src/debug_overlay.rs:1-3](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/debug_overlay.rs#L1-L3) 模块文档说明「绕过一切常规管线」的动机。
- [src/debug_overlay.rs:12-29](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/debug_overlay.rs#L12-L29) `DebugFrameOverlayMode` 与 `next()` 的三态循环。
- [src/debug_overlay.rs:31-44](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/debug_overlay.rs#L31-L44) 常量：`MAX_SAMPLES = 1000` 滚动窗口；字形尺寸与步进以「字格」为单位，乘 `CELL_SIZE`（2 逻辑像素）换算成实际大小。
- [src/debug_overlay.rs:87-93](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/debug_overlay.rs#L87-L93) `record_frame`：总帧数累加不受模式影响（隐藏时也在数），样本队列超限弹出最老的。
- [src/debug_overlay.rs:95-161](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/debug_overlay.rs#L95-L161) `paint`：先按最长行算面板尺寸、锚到视口右上角，画半透明黑色底板，然后逐字符查字模、把每行点阵中**连续的点合并成一个 quad**（减少图元数量）；`cell` 尺寸对 `scale_factor` 取 `max(1.0)` 保证小数缩放下每格至少一个物理像素。
- [src/debug_overlay.rs:163-192](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/debug_overlay.rs#L163-L192) `lines()` 生成各行文本；[src/debug_overlay.rs:195-205](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/debug_overlay.rs#L195-L205) `format_ms` 固定宽度右对齐，保证多行数字对齐。
- [src/debug_overlay.rs:231-316](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/debug_overlay.rs#L231-L316) `glyph()`：每个字符一个 `[u8; 7]`，每行 5 个 bit。配套测试 [src/debug_overlay.rs:325-357](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/debug_overlay.rs#L325-L357) 保证「读数能产生的每个字符都有字模」，否则会静默渲染成空白。

接线（均在 `profiler` feature 下）：

- [src/window.rs:2889-2900](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L2889-L2900)：`draw_roots` 之后把 overlay 画进 `next_frame.scene`。
- [src/window.rs:2980-2986](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L2980-L2986)：`end_draw` 的返回值喂给 `record_frame`。
- [src/window.rs:3057-3083](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L3057-L3083)：公开 API——查询/设置模式、`cycle_debug_frame_overlay_mode`、`reset_debug_frame_overlay_stats`（清样本但保留总帧数）。
- Zed 侧把它绑到动作：[crates/zed/src/zed.rs:1040-1047](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/zed/src/zed.rs#L1040-L1047) `dev::ToggleFpsOverlay` / `dev::ResetFrameOverlayStats`。

#### 4.3.4 代码实践

**实践目标**：让一个 gpui 示例持续重绘并打开帧面板，肉眼验证「帧率-耗时」关系。

操作步骤（示例代码为读者自写）：

1. 复制 `examples/hello_world.rs` 为自己的示例（或在本地修改），在根视图上加一个每 16ms `cx.notify()` 的定时器，制造约 60fps 的连续动画（可参照 u2-l5 的 spawn + timer 写法）。
2. 在 `open_window` 拿到 `window_handle` 后，调用一次：

   ```rust
   // 示例代码
   window_handle
       .update(cx, |_, window, _cx| window.cycle_debug_frame_overlay_mode())
       .unwrap();
   ```

3. 用 `cargo run -p gpui --example <你的示例名> --features profiler` 运行（面板代码在 `profiler` feature 下编译）。

需要观察的现象：右上角出现绿色读数；再触发一次 cycle 会从 `Minimal`（一行）变 `Full`（五行 CUR/1%/10%/MAX/FRAMES）。

预期结果：连续动画期间 CUR 稳定在某个值附近，MAX 明显更大（偶发长帧）；停止动画后读数不再更新但仍显示。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `record_frame` 在 `Hidden` 模式下也要继续累加 `total_frame_count`？

答案：模式切换是即时生效的查询，而统计窗口是历史数据。隐藏期间也计数，用户切到 Full 时看到的 `FRAMES` 才是「自创建以来的真实总帧数」，而不是「开启显示后的帧数」。测试 [src/debug_overlay.rs:392-407](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/debug_overlay.rs#L392-L407) 明确验证了跨模式切换的累计行为。

**练习 2**：面板文字为什么用点阵字模而不是 `window.text_system()`？

答案：模块文档（[src/debug_overlay.rs:1-3](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/debug_overlay.rs#L1-L3)）说明动机：使用文本系统会牵扯布局、字形缓存乃至视图失效，任何一步都可能触发新帧，而这个 overlay 的目的就是观察帧——必须避免「观察行为改变被观察对象」。直接插 quad 则完全不进入 GPUI 的常规绘制管线。

### 4.4 `debug` / `debug_below` 调试样式

#### 4.4.1 概念说明

这是最轻量的元素级调试手段：`Styled` trait 上的两个链式方法（仅 debug 构建存在）。`debug()` 给单个元素画红框；`debug_below()` 给它**以及所有走 `Style::paint` 绘制背景的子元素**画红框。后者借助了一个巧妙的机制：一个只在「进入子树绘制」期间存在的 `DebugBelow` **全局标记**。

注意精确语义：红框来自 `Style::paint`，只有「conforming」（用 `Style::paint` 画背景）的元素才会被覆盖；`canvas`、直接 `paint_quad` 的自定义元素不会自动出现红框。这也是 `DebugBelow` 的文档要求「从你自己的元素里配合该全局」的原因——自定义元素可以主动查询这个全局来决定是否画自己的调试框。

#### 4.4.2 核心流程

```text
.debug_below() 写入 StyleRefinement.debug_below = Some(true)
  ↓ 每帧样式合成进 Style
Style::paint(bounds, window, cx, continuation):
  if style.debug_below { cx.set_global(DebugBelow) }     # 进入子树前挂全局
  if style.debug || cx.has_global::<DebugBelow>() {
      window.paint_quad(outline(bounds, red()))           # 画红框
  }
  ... 正常绘制背景/边框 ...
  continuation(window, cx)                                # 递归绘制孩子
  if style.debug_below { cx.remove_global::<DebugBelow>() } # 离开子树时摘除
```

全局的「挂上/摘除」严格包裹 `continuation`（孩子绘制），因此标记的作用域恰好是这棵子树——这就是「below」的实现方式，不需要向样式继承体系里塞字段。

另一个配套行为在 div 上：当元素处于 debug 状态且被 hover 时，div 会在自己左上角画出 `GlobalElementId` 文本；按住辅助修饰键（macOS 为 ⌘，其他平台为 Ctrl，见 [src/platform/keystroke.rs:483-493](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform/keystroke.rs#L483-L493) 的 `Modifiers::secondary`）再点击该文本，stderr 会打印这个元素的构造位置 `文件:行:列`。

#### 4.4.3 源码精读

- [src/styled.rs:891-903](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/styled.rs#L891-L903) 两个链式方法，写入补丁字段。
- [src/style.rs:315-321](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/style.rs#L315-L321) `StyleRefinement` 的 `debug` / `debug_below` 字段（`#[cfg(debug_assertions)]`）。
- [src/style.rs:19-26](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/style.rs#L19-L26) `DebugBelow` 空标记类型与 `impl Global`——文档写明「供你自己的元素对接 debug_below」。
- [src/style.rs:695-703](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/style.rs#L695-L703) `Style::paint` 开头：挂全局 + 画 `outline(bounds, red())`；[src/style.rs:758-761](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/style.rs#L758-L761) 孩子绘完后的摘除。
- [src/elements/div.rs:2519-2536](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/elements/div.rs#L2519-L2536) `paint_debug_info`：hover 且处于 debug 状态时，用文本系统整形 `GlobalElementId` 并画在 hitbox 左上角。
- [src/elements/div.rs:2558-2604](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/elements/div.rs#L2558-L2604) 按住辅助修饰键点击 ID 文本时 `eprintln!` 出构造位置。

#### 4.4.4 代码实践

**实践目标**：给一个含大量 div 的界面开 `debug_below`，用红框 + hover ID + 源码位置三件套定位元素。

操作步骤（示例代码为读者自写）：

1. 基于 `examples/hello_world.rs`（或 4.2.4 的万格界面）在根 div 上加 `.debug_below()`：

   ```rust
   // 示例代码
   div()
       .debug_below()
       .flex_col()
       .child(div().w_full().h(px(40.)).bg(rgb(0x505050)))
       .child(div().w_full().flex_1().bg(rgb(0x202020)))
   ```

2. 直接 `cargo run -p gpui --example <示例名>`（debug 构建即满足 `debug_assertions`，无需 feature）。
3. 鼠标 hover 到任意子块上；再按住 Ctrl（macOS 为 ⌘）点击左上角的 ID 文本，观察终端。

需要观察的现象：整个子树每个走 `Style::paint` 的元素都套红框；hover 某块时其左上角出现一串形如 `Name("...")` / `NamedInteger(...)` 的元素 ID；修饰键点击后 stderr 打印 `This element was created at: .../xxx.rs:行:列`。

预期结果：红框覆盖所有 div 子元素；能从打印的源码位置反查到你在示例里写的那行 `.child(...)`。若某个子元素是 canvas 或自定义元素，则不会有红框——验证 4.4.1 的「conforming」限制。

#### 4.4.5 小练习与答案

**练习 1**：`debug_below` 为什么用「临时 Global」而不是像文本样式那样沿 `text_style_stack` 继承？

答案：调试标记只在绘制阶段消费、且需要覆盖「所有类型」的子元素（不限于解析样式的元素）。样式栈继承要求每个元素都参与合成与读取；而全局标记对任意在子树内执行 `Style::paint`（或主动查询全局）的代码都生效，实现只需一对 set/remove，代价最小。副作用是它不是「继承」语义：只看 `has_global::<DebugBelow>()`，与元素深度无关。

**练习 2**：release 构建下 `.debug_below()` 还存在吗？

答案：不存在。方法、补丁字段与 `DebugBelow` 全局都在 `#[cfg(debug_assertions)]` 下（[src/styled.rs:898-903](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/styled.rs#L898-L903)、[src/style.rs:22-26](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/style.rs#L22-L26)），release 编译直接没有这些 API，零运行时开销。

### 4.5 Inspector 元素检查器

#### 4.5.1 概念说明

Inspector 是「GPUI 版的浏览器 DevTools 元素面板」：开启后窗口右侧出现检查器面板，进入拾取模式（pick mode）后点击界面任意元素，面板显示它的元素 ID、构造位置，以及**该元素类型的自定义检查状态**（Zed 中用于展示 div 的样式详情）。

它的启用条件是 `#[cfg(any(feature = "inspector", debug_assertions))]`——debug 构建开箱即用，release 构建需开 `inspector` feature（[Cargo.toml:30](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/Cargo.toml#L30)，该 feature 会把 `gpui_macros` 的 inspector 支持也打开）。

三个角色分工：

- **gpui（本讲范围）**：`Inspector` 实体（哪个元素被选中、是否在拾取）、`InspectorElementId`（元素身份）、hitbox 登记、鼠标事件路由。
- **宿主应用**：通过 `App::set_inspector_renderer` 提供面板 UI，通过 `App::register_inspector_element::<T>` 注册「类型 T 的检查状态如何渲染」。
- **元素**：在 prepaint 阶段把 hitbox 与 inspector id 关联起来，供拾取命中。

#### 4.5.2 核心流程

元素身份的构成：

```text
InspectorElementId = { path: InspectorElementPath, instance_id: usize }
InspectorElementPath = { global_id: GlobalElementId,          # 最近带 id 祖先的路径（u4-l1）
                         source_location: 构造处源码位置 }      # #[track_caller] 捕获
instance_id：同一路径在一帧内出现多次时递增区分
```

拾取流程：

```text
window.toggle_inspector(cx)              # 创建/移除 Inspector 实体并 refresh
用户点「开始拾取」 inspector.start_picking()
  ↓ 下一帧 prepaint：每个有 inspector_id 的元素调 window.insert_inspector_hitbox
    （仅在 picking 时登记到 next_frame.inspector_hitboxes）
鼠标移动 → hovered_inspector_hitbox 按命中栈 + pick_depth 选中元素 → inspector.hover
鼠标点击 → inspector.select(id) → window.refresh()
  ↓ 之后每帧：路径匹配活动元素的元素通过 with_inspector_state 存取检查状态
Inspector::render → 应用注册的 InspectorRenderer 画面板 → render_inspector_states
                    列出该元素所有已注册状态的渲染结果
```

`pick_depth` 是滚轮控制的「穿透深度」：命中栈里同一位置的元素可能层层叠叠（子元素、父容器、浮层），滚动滚轮在深度轴上移动，就能选中「鼠标下面第二层」的元素。

#### 4.5.3 源码精读

**元素身份**。[src/inspector.rs:2-10](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/inspector.rs#L2-L10) `InspectorElementId`；[src/inspector.rs:28-37](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/inspector.rs#L28-L37) `InspectorElementPath` = 全局元素 ID + 源码位置。

**元素侧的配合**。`Element` trait 的三阶段签名都带 `inspector_id` 参数，并有 `source_location` 方法（[src/element.rs:60-69](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/element.rs#L60-L69)、[src/element.rs:73-104](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/element.rs#L73-L104)）。div 的 `Interactivity::new` 用 `#[track_caller]` 捕获构造位置（[src/elements/div.rs:88-97](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/elements/div.rs#L88-L97)）——这就是检查器能显示「这行代码」的来源。

**窗口侧**：

- [src/window.rs:6170-6178](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L6170-L6178) `toggle_inspector`：创建 `Inspector` 实体并 `refresh()`——下一帧整树重绘，元素才有机会登记 inspector hitbox。
- [src/window.rs:6213-6228](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L6213-L6228) `build_inspector_element_id`：为同一路径分配递增的 `instance_id`（存在 `next_frame` 上，每帧重置）。
- [src/window.rs:6254-6272](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L6254-L6272) `insert_inspector_hitbox`：只在拾取模式登记；div 在 prepaint 里调用它（[src/elements/div.rs:2495-2501](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/elements/div.rs#L2495-L2501)）。
- [src/window.rs:6290-6338](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L6290-L6338) `handle_inspector_mouse_event`：MouseMove → hover；MouseDown → select；ScrollWheel → 调整 `pick_depth`（钳制在 `[0, 命中数-0.5]`）后重新解析悬停元素。
- [src/window.rs:6340-6357](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L6340-L6357) `hovered_inspector_hitbox`：按 `pick_depth` 跳过命中栈前 N 层后找第一个登记过 inspector id 的 hitbox。
- [src/window.rs:6230-6244](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L6230-L6244) `prepaint_inspector`：把 `Inspector` 视图作为一个附加根，铺在窗口右侧 `inspector_width` 宽的列上。
- [src/window.rs:6191-6211](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L6191-L6211) `with_inspector_state`：元素在 paint 阶段经它存取「自己类型对应的活动检查状态」——仅当自己的 id 恰是当前选中元素时闭包才拿到 `Some`。

**检查器状态与渲染**。[src/inspector.rs:59-112](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/inspector.rs#L59-L112)：`Inspector` 持有活动元素及其 `TypeIdHashMap` 状态表；`set_active_element_id` 变化时 `window.refresh()`。[src/inspector.rs:145-153](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/inspector.rs#L145-L153) 拾取开关；[src/inspector.rs:156-186](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/inspector.rs#L156-L186) `render_inspector_states` 遍历状态表、查注册表逐类型渲染。

**应用侧注册**。[src/app.rs:2729-2742](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app.rs#L2729-L2742) `set_inspector_renderer` / `register_inspector_element`；[src/inspector.rs:189-199](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/inspector.rs#L189-L199) `Inspector::render` 优先调用应用注册的面板渲染器，否则渲染空。

**真实用法**。Zed 把它接成动作：[crates/inspector_ui/src/inspector.rs:10-25](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/inspector_ui/src/inspector.rs#L10-L25) `dev::ToggleInspector` 动作（注意用 `cx.defer` 避免窗口已在更新中造成的双重租约）；[crates/inspector_ui/src/inspector.rs:42-52](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/inspector_ui/src/inspector.rs#L42-L52) 注册 `DivInspectorState` 的渲染器并设置面板渲染器。注意这层在 zed 仓库的 `inspector_ui` crate，不在 gpui 内（HEAD 提交 91bf967e27 的标题 "Implement inspector flag" 指的就是 zed 侧的 `inspector` 构建开关，与 gpui 的 `src/inspector.rs` 是两回事）。

#### 4.5.4 代码实践

**实践目标**：在纯 gpui 示例中打开检查器，走一遍「拾取 → 选中 → 面板显示」。

操作步骤（示例代码为读者自写，源码阅读型为主）：

1. 基于 hello_world 写一个含嵌套 div 的视图，在窗口创建后开启检查器（debug 构建即可）：

   ```rust
   // 示例代码：open_window 之后
   window_handle
       .update(cx, |_, window, _cx| window.toggle_inspector(_cx))
       .unwrap();
   ```

   注意 `toggle_inspector` 需要 `&mut App`；在 `update` 闭包里可直接拿到。
2. 运行示例，观察窗口右侧出现的面板区域。
3. 阅读并对照源码走查：`toggle_inspector`（创建实体 + refresh）→ 下一帧 `insert_inspector_hitbox`（若在拾取模式）→ `handle_inspector_mouse_event` 的 hover/select 分支 → `set_active_element_id` 的 `window.refresh()`。

需要观察的现象：右侧出现检查器面板；由于 gpui 示例没有注册 `InspectorRenderer`，`Inspector::render` 走 `Empty` 分支（[src/inspector.rs:189-199](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/inspector.rs#L189-L199)），面板内容为空——这正是「gpui 提供机制、宿主提供 UI」分工的直接体现。

预期结果：理解「没有注册渲染器时检查器退化为空面板」；若想看到完整体验，需在应用侧 `set_inspector_renderer`（可参照 inspector_ui 的实现自己写一个最简面板）。交互细节**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`InspectorElementId` 为什么要在 `GlobalElementId` 之外再加 `instance_id`？

答案：`GlobalElementId` 是「最近带 id 祖先路径」的快照，同一段代码在循环里生成的多个元素可能共享同一路径（例如兄弟节点都没显式 id）。`build_inspector_element_id` 为同路径按出现顺序递增 `instance_id`，才能把「同一帧里同路径的第 1 个和第 3 个」区分开。

**练习 2**：为什么 `insert_inspector_hitbox` 在非拾取模式直接返回，而不是始终登记？

答案：性能。登记发生在每个元素的 prepaint 热路径上，每帧每个元素都要调一次；检查器关闭时这是纯浪费。用 `is_inspector_picking` 短路后，关闭状态的开销只剩一次布尔判断。

**练习 3**：`with_inspector_state` 如何避免「所有元素都往 Inspector 里塞自己的状态」？

答案：它只对「id 恰好等于当前活动元素」的元素生效（[src/window.rs:6199-6208](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L6199-L6208) 的比较），其余元素拿到 `&mut None`。状态表按 `TypeId` 键存在活动元素自己的 `InspectedElement` 里，随选中切换而重建，而不是全局累积。

## 5. 综合实践

把本讲工具串成一个完整的性能调查任务：

**任务：诊断「一万个格子的界面为什么掉帧」，并给出虚拟化前后对比数据。**

1. **搭建被测界面**：按 4.4.4 写一个 10_000 个 8px 方块的网格视图（每个子 div 用 `(name, generation, i)` 组合出唯一 id，确保重绘不可复用缓存）。
2. **肉眼观察**：`.debug_below()` 开红框确认元素树结构符合预期（没有意外的包裹层）；开 `DebugFrameOverlay`（Full 模式）读 CUR/MAX。
3. **量化**：按 4.2.4 把它写成 `#[gpui::bench]` 基准，记录 window draw 的 p50/p99/max 与 frame budget overruns。
4. **虚拟化改造**：把网格换成 u6-l1 的 `uniform_list`（行高固定、只渲染可见行），保持总数据量不变，重复第 3 步测量。
5. **对比结论**：写出两份报告的差异——draw 直方图整体下移多少、超支数是否归零；用一句话解释原因（等高虚拟化用算术代替了整树重建，每帧只为可见区间构建元素）。
6. **（延伸）定位长任务**：在 `--features profiler` 下触发一次明显卡顿，结合 `take_all_stats` 的 top-5 任务统计（文件:列 定位）验证瓶颈确实在渲染而非别处。

产物：一份包含两组基准数字与一句结论的对比笔记。所有运行结果**待本地验证**。

## 6. 本讲小结

- `profiler` feature 提供三层采集：每线程任务计时（100µs 准入门槛的 top-5 统计 + 可选明细环形缓冲）、每窗口帧直方图（draw 耗时、动画呈现间隔、输入延迟）、trace 开启时的逐帧事件流（`FrameTimingCollector` 游标增量拉取）；`WindowProfiler` 的埋点同时向 journal 双写，那是 u7-l6 的主题。
- `PresentTiming` 记录 `present_start`/`present_end` 并提供 `present_duration()`；`animation_interval` 只在连续动画帧之间有意义。
- `#[gpui::bench]` = Criterion + 真实 GPUI 环境：`bench_platform` 提供真实时间的 `ThreadedDispatcher` 与平台文本系统（GPU 提交仅 macOS 计入）；`BenchAppContext` 有 `bench_iter` / `bench_task` / `bench_renderer` 三种入口；`BenchReport` 把帧事件汇成直方图并按帧预算换算超支数。
- `debug()` / `debug_below()` 是零依赖的元素级调试：红框来自 `Style::paint`，「below」由严格包裹子树绘制的临时 `DebugBelow` Global 实现；hover 可看元素 ID，辅助修饰键点击可打印构造位置。
- `DebugFrameOverlay` 用自带点阵字模直接向 `Scene` 插 quad，绕过布局/文本/失效以避免「观察帧的行为本身触发帧」。
- `Inspector` 是「机制在 gpui、UI 在宿主」的元素检查器：`InspectorElementId`（全局元素 ID + 源码位置 + 实例序号）标识元素，拾取模式登记 inspector hitbox，滚轮调 `pick_depth` 穿透叠层，面板与各类型检查状态由应用注册。

## 7. 下一步学习建议

下一讲（u7-l6）深入 `profiler/journal.rs` 与 `profiler/hang.rs`：本讲反复出现的 `journal::begin_foreground_turn` / `record_input` / `record_present` 双写到底写去了哪里、固定大小环形缓冲如何划分活动区间、`HangDetector` 如何按「单事件阈值 / 帧预算累计」两类条件产出卡顿报告。建议在此之前先把本讲的 `set_trace_enabled` / `TraceGuard` / `FrameTimingCollector` 三者的关系想清楚——journal 与帧事件缓冲共享同一套「便宜到可常开」的设计哲学。若想继续读工程化主题，u7-l4（测试体系）与本讲的 `BenchAppContext` 是同一家族的「受控 GPUI 环境」，对照阅读收获最大。
