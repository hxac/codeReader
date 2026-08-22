# test-support 与可视化测试：TestPlatform、run_until_parked 与时钟推进

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 gpui 的测试平台设施由哪几块组成：`platform/test` 目录下的 `TestPlatform` / `TestWindow` / `TestDispatcher`、`app/test_context.rs` 的 `TestAppContext` / `VisualTestContext`、以及 `platform/visual_test.rs` 的 `VisualTestPlatform`。
2. 理解 `#[gpui::test]` 宏如何把这些零件组装起来：种子（seed）、迭代次数、teardown 时的泄漏检测都由谁负责。
3. 掌握 `run_until_parked`（排空就绪任务）与 `advance_clock`（推进虚拟时钟、触发定时器）的分工，能写出「断言延时任务在时钟推进前未执行、推进后已执行」的确定性测试。
4. 解释 gpui_platform crate 里那三个 `#[ignore]` 的 macOS 测试为什么必须 `--ignored --test-threads=1` 运行。
5. 会用 `TestWindow` 新增的 `schedule_frame` 实现与 `simulate_scheduled_frame` / `frame_scheduled` 模拟 API，确定性地验证「停泊（Parked）渲染循环的唤醒」——这正是 eb354c8d50 把渲染循环改为按需驱动后配套引入的测试设施。

## 2. 前置知识

- **测试替身（test double）**：在测试里顶替真实依赖的构件。本讲的替身是一个完整的假 `Platform`：不开真窗口、不碰真 GPU，窗口几何、剪贴板、文件对话框全部变成可断言的内存状态。
- **虚拟时钟（virtual clock）**：测试调度器里的时间是一个可以被测试代码手动拨动的数值，`Duration::from_millis(500)` 的定时器不需要真的等 500ms，调用 `advance_clock(600ms)` 即可让它「到期」。
- **确定性调度**：真实执行器里任务何时运行取决于线程与操作系统；测试调度器把所有任务收进单线程队列，用种子化的加权随机决定顺序——同一 `SEED` 必然复现同一交错。
- **PlatformDispatcher 契约**（承接 u4-l2）：`is_main_thread` / `dispatch` / `dispatch_on_main_thread` / `dispatch_after` 等方法是执行器与平台事件循环之间的桥。本讲的 `TestDispatcher` 就是这个契约的测试实现，且 `PlatformDispatcher::as_test()` 提供了运行期下转入口。
- **headless 后端**（承接 u5-l2）：`HeadlessClient` 证明「窗口逻辑存在但不上屏」是可行的；本讲的 `TestWindow` 走得更远——连布局绘制都只更新内存标志位。
- **schedule_frame 语义**（承接 u3-l2 与 u5-l4）：`PlatformWindow::schedule_frame` 的含义是「请求平台调度下一帧」，默认空实现代表平台自有帧驱动；Wayland 覆写它以唤醒停泊在 Parked 状态的按需渲染循环。本讲的 `TestWindow` 是第二个覆写者，用于在测试里模拟这种按需驱动平台。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/gpui_platform/src/gpui_platform.rs` | 门面 crate；末尾 `#[cfg(all(test, target_os = "macos"))] mod tests` 里是本讲的主角之一：三个被 ignore 的 VisualTestAppContext 测试 |
| `crates/gpui/src/platform/test.rs` | 测试平台模块的入口与再导出：`TestDispatcher` 对外公开，`TestPlatform` / `TestWindow` / `TestDisplay` 仅为 `pub(crate)` |
| `crates/gpui/src/platform/test/dispatcher.rs` | `TestDispatcher`：把调度委托给 scheduler crate 的 `TestScheduler`，提供 `tick` / `run_until_parked` / `advance_clock` |
| `crates/gpui/src/platform/test/platform.rs` | `TestPlatform`：`Platform` 契约的测试实现，`open_window` 产出 `TestWindow`，提示框/通知等变成可查询的队列 |
| `crates/gpui/src/platform/test/window.rs` | `TestWindow`：假窗口，含 `schedule_frame` / `simulate_scheduled_frame` / `frame_scheduled` / `frame_waker` 等帧调度模拟设施 |
| `crates/gpui/src/platform/visual_test.rs` | `VisualTestPlatform`：装饰器——把真实平台（如 MacPlatform）的渲染能力和 `TestDispatcher` 的确定性调度拼在一起 |
| `crates/gpui/src/app/test_context.rs` | `TestAppContext` 与 `VisualTestContext`：`#[gpui::test]` 传给测试函数的上下文类型 |
| `crates/gpui/src/app/visual_test_context.rs` | `VisualTestAppContext`：离屏窗口 + 截图 + `run_until_parked` / `advance_clock` |
| `crates/gpui/src/test.rs` | `gpui::run_test`：按种子迭代构造 `TestDispatcher` 并运行测试闭包 |
| `crates/gpui/src/window.rs` | gpui 主窗口逻辑；`on_request_frame` 闭包是帧调度的心脏，文件末尾 tests 模块里有三个停泊渲染循环测试 |
| `crates/gpui/examples/testing.rs` | 官方测试示范：`#[gpui::test]` 的六种用法（基础、窗口、异步、allow_parking、属性测试、双上下文） |
| `crates/scheduler/src/test_scheduler.rs` | `TestScheduler`：虚拟时钟与任务队列的真正实现（`run` / `step` / `tick` / `advance_clock`） |
| `crates/gpui_macros/src/test.rs` | `#[gpui::test]` 过程宏：生成 `#[test]` 壳、按参数个数组装 `TestAppContext`、注入 teardown |
| `crates/gpui/src/platform/threaded_dispatcher.rs` | `ThreadedDispatcher`：真实多线程调度器（测试/基准用），与 `TestDispatcher` 形成对照 |

## 4. 核心概念与源码讲解

### 4.1 测试平台替身全景：TestPlatform、TestWindow 与 #[gpui::test] 组装线

#### 4.1.1 概念说明

gpui 的所有 UI 状态都活在单个前台线程、靠平台事件循环驱动（u4-l1）。这意味着「跑一段 UI 代码」离不开一个 `Platform`。为了不让测试依赖真实的窗口系统，gpui 内建了一整套平台替身，全部放在 `platform/test` 目录，由 `platform.rs` 统一再导出：

[../gpui/src/platform.rs:L16](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L16) 声明 `mod test;`（受 `test` / `test-support` feature 门控）。

[../gpui/src/platform.rs:L85-L91](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L85-L91) 把 `TestDispatcher`、`ThreadedDispatcher`、`VisualTestPlatform` 设为公开导出。

[../gpui/src/platform/test.rs:L1-L11](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test.rs#L1-L11) 则把 `TestPlatform`、`TestWindow`、`TestDisplay` 以 `pub(crate)` 再导出——**这个可见性差异很重要**：下游 crate（包括 zed 主程序）能拿到 `TestDispatcher`，但拿不到 `TestWindow`；因此 4.4 节的停泊渲染循环测试只能写在 gpui crate 内部（那三个真实测试正是在 `crates/gpui/src/window.rs` 的 tests 模块里）。

替身的分工：

- `TestPlatform`：假平台。文本系统默认是 `NoopTextSystem`，剪贴板是内存字段，文件对话框把请求存进队列等测试用 `simulate_path_prompt_response` 应答。
- `TestWindow`：假窗口。没有操作系统句柄，`HasWindowHandle` / `HasDisplayHandle` 直接返回 `NotSupported`（u3-l2 讲过这是 raw-window-handle 对「无真实句柄」的标准表达）；布局与绘制管线照常运行，但「呈现」只是置两个布尔标志位。
- `TestDispatcher`：假调度器。所有前后台任务进同一个单线程队列，时间由虚拟时钟控制（4.2 详解）。

#### 4.1.2 核心流程

`#[gpui::test] fn foo(cx: &mut TestAppContext)` 的完整组装线：

```text
#[test] 壳（宏生成）
  └─ gpui::run_test(iterations, seeds, retries, |dispatcher, seed| { ... })
       ├─ TestDispatcher::new(seed)            ← 每个 seed 一次
       ├─ TestAppContext::build(dispatcher)
       │    ├─ BackgroundExecutor / ForegroundExecutor  ← 共享同一 Arc<TestDispatcher>
       │    ├─ TestPlatform::new(...)                   ← 假平台
       │    └─ App::new_app(...)  + GpuiMode::test()
       ├─ ForegroundExecutor::block_test( 测试函数体 )
       └─ teardown：run_until_parked → forbid_parking + quit → run_until_parked
                    → drop(cx) → dispatcher.drain_tasks()   ← 泄漏检测
```

teardown 的顺序有讲究：先排空任务，再 `forbid_parking()` 并触发 quit，再排空一次——如果还有任务在等一个永远不来的外部事件，`forbid_parking` 会让后续 park 直接 panic，把「测试泄漏了挂起任务」暴露成失败而不是无声挂起。

#### 4.1.3 源码精读

**宏展开的骨架**。[../gpui_macros/src/test.rs:L163-L176](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui_macros/src/test.rs#L163-L176)：每出现一个 `&mut TestAppContext` 参数，宏就生成一段 `TestAppContext::build(dispatcher.clone(), Some(函数名))` 与配套 teardown；因此一个测试函数要几个上下文就有几个（`testing.rs` 的分布式系统测试正是靠双上下文模拟两个进程）。

[../gpui_macros/src/test.rs:L185-L209](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui_macros/src/test.rs#L185-L209)：最终生成的 `#[test]` 函数调用 `gpui::run_test`，异步测试则先经 `ForegroundExecutor::block_test` 把 future 在前台执行器上阻塞跑完；末尾 `dispatcher.drain_tasks()` 清空残留任务供泄漏检测。

**种子的来源**。[../gpui/src/test.rs:L95-L142](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/test.rs#L95-L142)：`run_test` 对每个 seed 新建 `TestDispatcher::new(seed)`，失败时打印 `failing seed: {seed}`，并提示用环境变量 `SEED` 复现。`#[gpui::test(iterations = 10)]` 就是让同一段测试跑 10 个不同种子（`testing.rs:326` 有实例）。

**TestPlatform 的构造与开窗**。[../gpui/src/platform/test/platform.rs:L106-L152](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/platform.rs#L106-L152)：三个构造器（默认 NoopTextSystem / 注入真实文本系统 / 附带无头渲染器工厂），用 `Rc::new_cyclic` 拿到弱引用供 `TestWindow::activate` 回指平台。

[../gpui/src/platform/test/platform.rs:L399-L413](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/platform.rs#L399-L413)：`open_window` 直接 `TestWindow::new(...)` 装箱返回——对照 u3-l1 的开窗主链路，`Window::new` 在测试里拿到的就是这个假窗口。

[../gpui/src/platform/test/platform.rs:L338-L340](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/platform.rs#L338-L340)：`run` 是 `unimplemented!()`——测试从不进入平台事件循环，一切驱动都来自测试代码手动泵送。

**TestAppContext 的组装**。[../gpui/src/app/test_context.rs:L125-L137](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/test_context.rs#L125-L137)：`build` 用同一个 `Arc<TestDispatcher>` 构造前后台两个执行器（与 u4-l1 讲的「平台只造一个 dispatcher」完全同构），再装上 `TestPlatform` 和 `FakeHttpClient`，并把 `App` 的模式设为 `GpuiMode::test()`。

[../gpui/src/platform/test/window.rs:L55-L69](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/window.rs#L55-L69)：TestWindow 对 raw-window-handle 两个 trait 返回 `HandleError::NotSupported`，并附注释说明这正是 raw_window_handle 为「无真实窗口」准备的变体。

#### 4.1.4 代码实践

**实践目标**：把官方测试示范跑起来，亲眼看到「同步副作用立即执行、异步副作用要泵送才执行」。

1. 操作步骤：在仓库根目录执行（命令摘自示例文件自己的文档头）：

   ```bash
   cargo test -p gpui --example testing --features test-support
   ```

2. 需要观察的现象：六个测试全部通过；重点看 `test_async_operations`——`reload()` 产生的 detached 任务在 `cx.run_until_parked()` 之前 count 仍是 100，之后才变成 150。
3. 预期结果：全部通过。关键源码在 [../gpui/examples/testing.rs:L278-L299](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/examples/testing.rs#L278-L299)，注释写明了「side effects don't run until you yield control」。
4. 若想体会调度随机性，可用 `cargo test -p gpui --example testing --features test-support test_random_interleaving -- --nocapture` 观察 `spawned` 与 `ran` 两行执行顺序的差异（该测试用 `iterations = 10` 跑了 10 个种子）。
5. 本环境未执行上述命令，运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TestPlatform::run` 是 `unimplemented!()` 而不是空实现？

答案：`run` 的语义是「进入平台事件循环并阻塞」。测试里没有事件循环可进；若静默空实现，测试代码里误调 `cx.run(...)` 会无声通过而不是立刻失败。`unimplemented!()` 让契约违规在第一时间 panic——这与 u2-l1 讲的「默认实现三姿态」中显式报错姿态一致。

**练习 2**：`#[gpui::test] fn t(cx_a: &mut TestAppContext, cx_b: &mut TestAppContext)` 里两个上下文共享调度器吗？

答案：共享。宏把同一个 `dispatcher.clone()` 传给两个 `TestAppContext::build`，而 `TestDispatcher::clone`（[../gpui/src/platform/test/dispatcher.rs:L103-L112](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/dispatcher.rs#L103-L112)）克隆的是同一个 `Arc<TestScheduler>`、只是新分配 `session_id`。所以任一上下文 `run_until_parked` 都会把两个「进程」的待办混在一起按加权随机执行——这正是 `testing.rs` 分布式测试能检验不同交错鲁棒性的原理。

**练习 3**：下游 crate 为什么不能像 gpui 内部测试那样调用 `cx.test_window(handle)`？

答案：`TestAppContext::test_window` 是 `pub(crate)`（[../gpui/src/app/test_context.rs:L530-L543](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/test_context.rs#L530-L543)），且 `TestWindow` 本身经 `platform/test.rs` 的 `pub(crate) use window::*` 只在 crate 内可见。下游能拿到的是 `TestDispatcher`（`pub use`）。要把测试写到 `TestWindow` 这一层，只能把它放进 gpui crate 的测试模块。

### 4.2 TestDispatcher：单线程虚拟时钟与 run_until_parked / advance_clock

#### 4.2.1 概念说明

`TestDispatcher` 是 `PlatformDispatcher` 契约（u4-l2）的测试实现。它的注释开门见山：调度工作全部委托给 scheduler crate 的 `TestScheduler`，时钟、随机数、parking 控制都直接找 `scheduler()` 拿。核心设计有三个：

1. **一个 tick 至多跑一个任务**。这让测试可以像单步调试一样逐任务推进，也是 `dispatch_after` 直接 panic 的原因——测试要求一切延时都走调度器原生定时器，不许绕过虚拟时钟。
2. **tick 不推进时钟**。`step()` 只会「到期定时器出队 + 执行一个就绪任务」；时钟只被 `advance_clock` 拨动。因此 `run_until_parked` 排空的是**当前时刻**能做的所有事，未来的定时器保持挂起。
3. **确定性的随机**。`randomize_order: true` 时用优先级加权随机挑下一个任务，随机源由构造种子决定，同一 `SEED` 复现同一交错。

#### 4.2.2 核心流程

```text
run_until_parked()          advance_clock(d)
  while tick(false) {}        loop {
  ← 排空就绪任务               run()                    ← 先排空当前就绪任务
  ← 不触发未到期定时器          若最近的定时器 ≤ 目标时刻:
  ← tick 返回 false 即停         时钟拨到该定时器到期点   ← 触发它，唤醒等待的 future
                               }
                               时钟拨到 目标时刻
```

一个 tick 的内部（`step_filtered`）：先把 `expiration <= now` 的定时器出队并 drop（drop 会唤醒等待它的 future，future 醒来再把后续任务排回队列），再从候选任务中按权重随机挑一个执行；没有可做的事返回 false。

#### 4.2.3 源码精读

**TestDispatcher 本体**。[../gpui/src/platform/test/dispatcher.rs:L12-L34](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/dispatcher.rs#L12-L34)：结构体只有三个字段（session_id、调度器、CPU 数覆盖），`new(seed)` 用 `TestSchedulerConfig` 构造——`allow_parking: false`、`randomize_order: true`，还会读取 `PENDING_TRACES` 环境变量决定是否采集挂起任务的追踪。

**契约实现**。[../gpui/src/platform/test/dispatcher.rs:L114-L148](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/dispatcher.rs#L114-L148)：

- `dispatch_on_main_thread` → `scheduler.schedule_local(self.session_id, runnable)`——前台任务按 session 排队，同一 session 内保持顺序（这正是 u4-l1「cx.spawn 按序跑主线程」在测试里的落地）。
- `dispatch_after` → **panic**，错误信息明确指示改用 `BackgroundExecutor::timer()`。
- `as_test` → `Some(self)`，对应契约默认返回 `None` 的下转点（[../gpui/src/platform.rs:L1058-L1061](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L1058-L1061)），这是 `BackgroundExecutor` 在测试里拿到 `tick` / `run_until_parked` 的通道。
- `spawn_realtime` → 直接 `std::thread::spawn`，实时音频等真的需要独立线程的场景不受虚拟时钟管理。

**run_until_parked 与 tick**。[../gpui/src/platform/test/dispatcher.rs:L68-L78](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/dispatcher.rs#L68-L78)：`run_until_parked` 就是 `while self.tick(false) {}`。

[../gpui/src/platform/test/dispatcher.rs:L56-L66](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/dispatcher.rs#L56-L66)：`advance_clock` 与 `advance_clock_to_next_timer`（后者直接跳到下一个定时器，返回是否跳了）、`simulate_random_delay` 都只是调度器的薄封装。

**调度器侧**。[../scheduler/src/test_scheduler.rs:L186-L204](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/scheduler/src/test_scheduler.rs#L186-L204)：`run` = `while step() {}`，`tick` = `step_filtered(false)`——从实现看两者都不会拨动时钟。

[../scheduler/src/test_scheduler.rs:L239-L266](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/scheduler/src/test_scheduler.rs#L239-L266)：`step_filtered` 先按 `clock.now()` 切出已到期定时器并 drop 之（注释点明 drop 会唤醒等待的 future），再挑任务。

[../scheduler/src/test_scheduler.rs:L302-L331](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/scheduler/src/test_scheduler.rs#L302-L331)：候选筛选——前台任务每个 session 只取队首（保持会话内顺序）、被阻塞 session 排除；随后按 `priority.weight()` 加权随机抽取，与 u4-l2 讲的生产调度器「60/30/10 加权抽签」同构。

[../scheduler/src/test_scheduler.rs:L377-L413](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/scheduler/src/test_scheduler.rs#L377-L413)：`advance_clock` 的循环体——先 `run()` 排空，再把时钟拨到最近的未到期定时器，周而复始直到没有更早的定时器，最后拨到目标时刻。

**执行器侧的同名方法**。[../gpui/src/executor.rs:L205-L222](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/executor.rs#L205-L222)：`BackgroundExecutor::run_until_parked` 经 `as_test().unwrap().scheduler()` 走到 `scheduler.run()`——注意它对非测试调度器会直接 unwrap 失败，这两个方法都受 `test` / `test-support` 门控。

**三个 run_until_parked 的细微差别**：`TestAppContext::run_until_parked` 走 `dispatcher.run_until_parked()`（while tick），[../gpui/src/app/test_context.rs:L475-L478](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/test_context.rs#L475-L478)；`VisualTestContext::run_until_parked` 走 `background_executor.run_until_parked()`（scheduler.run），[../gpui/src/app/test_context.rs:L764-L767](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/test_context.rs#L764-L767)。两者殊途同归：都只排空就绪任务、不拨时钟——这一点被 gpui_platform 的第二个测试直接当作断言依据（见 4.3.3）。

**与 ThreadedDispatcher 的对照**。[../gpui/src/platform/threaded_dispatcher.rs:L17-L27](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/threaded_dispatcher.rs#L17-L27) 的文档一句话讲清分工：`TestDispatcher` 单线程 + 虚拟时钟，`ThreadedDispatcher` 真实线程池 + 实时定时器、以生产并发语义跑测试与基准。需要检验竞态时换用后者。

**写测试的计时器纪律**（也写在本仓库 CLAUDE.md）：需要超时/延时一律用 `cx.background_executor().timer(duration).await` 或 GPUI 执行器计时器，不要用 `smol::Timer::after(...)`——后者不被调度器追踪，`run_until_parked` 会在「还有事没做」时误判为无事可做；且如上所述，`dispatch_after` 在测试里是 panic 而非延时。

#### 4.2.4 代码实践

**实践目标**：亲手验证「延时任务在时钟推进前不执行、推进后执行」。这是 gpui_platform 第二个 macOS 测试的跨平台移植版，可在任何操作系统运行（`TestPlatform` 不依赖真实窗口系统）。

1. 操作步骤：在你自己的试验 crate（或直接在本地克隆中给 `crates/gpui/examples/testing.rs` 的 `mod tests` 追加）写入以下测试（**示例代码**，非仓库原有代码；若放独立 crate，需启用 gpui 的 `test-support` feature）：

   ```rust
   #[gpui::test]
   fn test_delayed_task_waits_for_clock(cx: &mut TestAppContext) {
       let flag = Rc::new(RefCell::new(false));
       let executor = cx.executor(); // TestAppContext::executor → BackgroundExecutor
       cx.update(|cx| {
           let flag = flag.clone();
           cx.spawn(async move |_| {
               // 测试纪律：用 GPUI 执行器计时器，而不是 smol::Timer
               executor.timer(Duration::from_millis(500)).await;
               *flag.borrow_mut() = true;
           })
           .detach();
       });

       // 排空就绪任务；定时器未到期，任务应仍在等待
       cx.run_until_parked();
       assert!(!*flag.borrow(), "时钟未推进，延时任务不应执行");

       // 推进虚拟时钟越过定时器时长，再排空
       cx.dispatcher.advance_clock(Duration::from_millis(600));
       cx.run_until_parked();
       assert!(*flag.borrow(), "时钟推进后延时任务应已执行");
   }
   ```

   要点：`cx.dispatcher` 是 `TestAppContext` 上的 `pub` 字段（[../gpui/src/app/test_context.rs:L26-L27](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/test_context.rs#L26-L27)），下游可直接 `advance_clock`；`cx.executor()` 见 [../gpui/src/app/test_context.rs:L197](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/test_context.rs#L197)。

2. 需要观察的现象：第一条断言在 `run_until_parked` 后依然成立（证明 run_until_parked 不拨时钟）；`advance_clock` 后任务完成。
3. 预期结果：测试通过。
4. 本讲义撰写环境未运行该测试，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：把上面的 `executor.timer(...)` 换成 `dispatch_after` 语义（比如通过某个平台的延时投递接口）会发生什么？

答案：`TestDispatcher::dispatch_after` 直接 panic，错误信息引导你改用 `BackgroundExecutor::timer()`（[../gpui/src/platform/test/dispatcher.rs:L132-L137](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/dispatcher.rs#L132-L137)）。设计意图：测试里一切时间都必须可由虚拟时钟控制，任何绕开调度器的延时都会破坏确定性。

**练习 2**：`run_until_parked` 之后调度器里还可能有「挂起」的东西吗？

答案：可能有——未到期的定时器。`tick` 只处理 `expiration <= now` 的定时器，时钟不动它们就永远躺着；`has_pending_tasks()` 会返回 true（[../scheduler/src/test_scheduler.rs:L212-L216](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/scheduler/src/test_scheduler.rs#L212-L216)）。这也是宏 teardown 里 `drain_tasks` 存在的原因之一：测试结束时调度器里可能还压着带执行器句柄的任务，与调度器形成引用环，不显式清空会干扰泄漏检测（[../scheduler/src/test_scheduler.rs:L346-L366](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/scheduler/src/test_scheduler.rs#L346-L366) 的注释详细解释了这条引用环）。

**练习 3**：`advance_clock_to_next_timer` 与 `advance_clock` 该选哪个？

答案：想「恰好让下一个定时器到期」用前者（返回 bool 表示是否真的跳了）；想模拟「过了一段真实时间，期间所有定时器按序触发」用后者——它会把区间内每个定时器的到期点依次兑现并运行被唤醒的任务（[../scheduler/src/test_scheduler.rs:L368-L413](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/scheduler/src/test_scheduler.rs#L368-L413)）。

### 4.3 VisualTestAppContext：真实渲染与可控调度的组合（gpui_platform 的三个 macOS ignore 测试）

#### 4.3.1 概念说明

`TestPlatform` 什么都是假的，适合逻辑测试；但有些测试（截图对比、像素级视觉断言）需要**真实渲染**。gpui 的答案是装饰器模式：`VisualTestPlatform` 包住一个真实平台（构造时传入），把「执行器」两个方法替换成基于 `TestDispatcher` 的实现，其余大量方法原样转发给内层真实平台。

[../gpui/src/platform/visual_test.rs:L1-L6](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/visual_test.rs#L1-L6) 的模块文档概括了它的三个卖点：真实渲染（目前仅 macOS Metal）、确定性任务调度、可拨动的时间。

`VisualTestAppContext` 则是使用它的门面：构造时读 `SEED` 环境变量做种子、打开「离屏窗口」（位置 -10000,-10000，屏幕外但合成器照常渲染）、暴露 `run_until_parked` / `advance_clock` / `capture_screenshot`。它专为「时间敏感的视觉行为」设计——文档里点名的例子就是 tooltip 延时。

#### 4.3.2 核心流程

```text
VisualTestAppContext::new(current_platform(false))     ← 真实平台（macOS 上是 MacPlatform）
  └─ VisualTestPlatform::new(platform, seed)           ← 装饰
       ├─ TestDispatcher::new(seed) → Arc 共享
       ├─ BackgroundExecutor / ForegroundExecutor      ← 替换执行器
       └─ platform 字段保留真实平台
  └─ App::new_app(装饰后的平台) + GpuiMode::test()

调用链路由：background_executor/foreground_executor → TestDispatcher（可控）
          text_system/displays/open_window → 内层真实平台（真实）
          run → panic（测试不进事件循环）
          剪贴板 → 内存字段（隔离，不污染系统剪贴板）
```

为什么这三个测试必须 `#[ignore]` 且 `--test-threads=1`？Rust 默认测试框架把每个测试放到工作线程上跑，而 macOS 的 AppKit/Cocoa API 只允许在进程主线程调用，从工作线程碰它们会触发 SIGABRT。`--test-threads=1` 让 libtest 直接在主线程顺序执行测试，测试体因此拿到主线程身份，才能安全构造 `MacPlatform` 并开真实窗口。源文件顶部的注释原文写明了这条因果链。

#### 4.3.3 源码精读

**测试文件头与运行说明**。[../gpui_platform/src/gpui_platform.rs:L99-L111](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui_platform/src/gpui_platform.rs#L99-L111)：`mod tests` 只在 `all(test, target_os = "macos")` 下编译；注释给出背景与命令。注意两点：注释中的命令写的是 `cargo test -p gpui visual_test_context -- --ignored --test-threads=1`，但本 crate 的测试要真正跑到，命令应为 `cargo test -p gpui_platform -- --ignored --test-threads=1`（注释里的过滤词 `visual_test_context` 与这些测试名并不匹配，疑似从别处复制的残留；**待本地验证**）。

**测试一：前台任务要泵送才执行**。[../gpui_platform/src/gpui_platform.rs:L113-L140](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui_platform/src/gpui_platform.rs#L113-L140)：`cx.update` 里 `cx.spawn(...).detach()` 一个只置位标志的任务；断言它**尚未**运行（spawn 只是把任务排进 TestDispatcher）；`cx.run_until_parked()` 之后才置位。注释强调「This should use our TestDispatcher, not the MacDispatcher」——验证装饰器确实接管了执行器。

**测试二：advance_clock 触发延时任务**。[../gpui_platform/src/gpui_platform.rs:L142-L171](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui_platform/src/gpui_platform.rs#L142-L171)：任务里 `executor.timer(500ms).await`；`run_until_parked` 后断言未完成（4.2 讲过：run_until_parked 不拨时钟）；`advance_clock(600ms)` 后断言完成。这是 4.2.4 实践的原型，也是「虚拟时钟」语义最浓缩的展示。

**测试三：window.spawn 路径同样走 TestDispatcher**。[../gpui_platform/src/gpui_platform.rs:L173-L206](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui_platform/src/gpui_platform.rs#L173-L206)：先 `open_offscreen_window_default` 开离屏窗口，再经 `window.spawn(cx, ...)` 在窗口上派生任务。注释点明这是 tooltip 行为的关键路径——tooltip 用 `window.spawn` 实现延时显示，若这条路径绕过了 TestDispatcher（比如落到 MacDispatcher 的主队列），延时类测试将不可控。

**装饰器本体**。[../gpui/src/platform/visual_test.rs:L31-L65](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/visual_test.rs#L31-L65)：结构体五个字段；`new` 里 `Arc::new(dispatcher.clone())` 同时喂给前后台执行器——与 `TestAppContext::build`、生产平台三方完全一致的「一个 dispatcher 造两个执行器」模式。

[../gpui/src/platform/visual_test.rs:L67-L98](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/visual_test.rs#L67-L98)：Platform impl 的方法分流——执行器返回自己的、`text_system`/`displays` 转发内层、`run` panic（`VisualTestPlatform::run should not be called in tests`）。

[../gpui/src/platform/visual_test.rs:L122-L128](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/visual_test.rs#L122-L128)：`open_window` 转发内层平台——所以这三个测试开的窗口是**真窗口**（只是摆在屏幕外），这正是「视觉测试要真实渲染」的来由。

[../gpui/src/platform/visual_test.rs:L219-L225](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/visual_test.rs#L219-L225)：剪贴板被替换为两个 `Mutex` 内存字段——测试写剪贴板不会污染系统状态。

**上下文门面**。[../gpui/src/app/visual_test_context.rs:L53-L87](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/visual_test_context.rs#L53-L87)：`with_asset_source` 读 `SEED` 环境变量（默认 0）→ 装饰平台 → 取 dispatcher 与执行器 → `FakeHttpClient` → `GpuiMode::test()`。文档提醒默认资产源是空 `()`，SVG 图标不渲染，需要时用 `with_asset_source` 传入真实 `Assets`。

[../gpui/src/app/visual_test_context.rs:L97-L119](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/visual_test_context.rs#L97-L119)：`open_offscreen_window` 把窗口摆到 `(-10000, -10000)`——任何显示器上都看不见，但合成器照常渲染。

[../gpui/src/app/visual_test_context.rs:L150-L161](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/visual_test_context.rs#L150-L161)：`run_until_parked` 与 `advance_clock` 一行转发给 dispatcher；注释点名它们的典型用途是 tooltip 延时类测试。

[../gpui/src/app/visual_test_context.rs:L383-L386](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/visual_test_context.rs#L383-L386)：`capture_screenshot` 走 `window.render_to_image()`——即 u8-l3 讲过的离屏回读链路，视觉断言由此拿到像素。

#### 4.3.4 代码实践

**实践目标**：在 macOS 上真实运行那三个被 ignore 的测试；在没有 macOS 的机器上完成「源码阅读型实践」。

1. macOS 路径：执行

   ```bash
   cargo test -p gpui_platform -- --ignored --test-threads=1
   ```

2. 需要观察的现象：三个测试按序通过；去掉 `--test-threads=1`（其他参数不变）则预期在 AppKit 线程检查处崩溃（SIGABRT）——这正是注释所警告的行为。
3. 非 macOS 路径（源码阅读型）：对照阅读测试一与 4.1.3 的宏展开骨架，回答「`cx.spawn` 之后任务在哪、谁把它搬上主线程」：任务经 `ForegroundExecutor` → `TestDispatcher::dispatch_on_main_thread` → `scheduler.schedule_local` 排队；只有 `run_until_parked` 泵送时才执行。把这条链路写成笔记。
4. 预期结果：macOS 上通过；并发运行时崩溃（**待本地验证**，本环境为 Linux，无法直接验证 macOS 行为）。

#### 4.3.5 小练习与答案

**练习 1**：`VisualTestPlatform` 为什么要拦截剪贴板却不拦截 `open_url`？

答案：剪贴板在测试里会被频繁读写（复制粘贴逻辑测试），若不拦截会污染系统状态且互相干扰，所以换成内存字段；`open_url` 转发给真实平台（[../gpui/src/platform/visual_test.rs:L134-L136](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/visual_test.rs#L134-L136)）——视觉测试的目标是渲染保真，打开 URL 属于「系统行为」，转发即可（对比 `TestPlatform` 则把它记录进 `opened_url` 字段供断言，两个替身对同一方法的取舍不同，取决于各自服务 的测试类型）。

**练习 2**：`VisualTestAppContext` 的 `wait_for` 方法为什么每轮循环先 `run_until_parked` 再 `timer(10ms).await`？

答案：见 [../gpui/src/app/visual_test_context.rs:L352-L377](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/visual_test_context.rs#L352-L377)。被等待的状态通常由异步任务推进：先泵送让已就绪的任务跑完，再用 10ms 计时器把控制权交还给调度器、让新到期的任务有机会执行；循环外用真实 `web_time::Instant` 做超时。这展示了「虚拟时钟泵送 + 真实超时兜底」的混合写法。

**练习 3**：这三个测试为什么写在 gpui_platform crate 而不是 gpui？

答案：它们验证的是「真实平台 + TestDispatcher 组合」这条集成链——需要一个会创建真实执行器的 `current_platform(false)`，而 gpui 主 crate 测试默认用纯替身 `TestPlatform`；gpui_platform 恰好同时依赖 gpui 与各平台 crate，是做这层集成验证的天然位置。门面 crate 不止再导出，还承担平台层的集成测试，这是本系列 u1-l1「门面定位」的一个补充侧面。

### 4.4 TestWindow 的 schedule_frame 模拟设施：确定性驱动停泊渲染循环

#### 4.4.1 概念说明

eb354c8d50 把渲染循环改成按需驱动后（u5-l4 详解了 Wayland 侧的 FrameLoop 八态状态机），「空闲窗口停泊在 Parked、零唤醒」成了需要专门测试的行为。真实平台不可入测试，于是 `TestWindow` 把这套语义压缩成**两个布尔 + 一个计数器**的模型：

- `frame_scheduled: bool`——「有帧待办」。gpui 调 `schedule_frame()` 置位，代表「请平台调度下一帧」。
- `frame_callback_pending: bool`——「已画完、正等合成器回调」。`draw()` 置位；模型了 present 之后到 frame callback 到达之前的窗口。
- `frame_wake_count`——`frame_waker()` 被调用的次数。gpui 的失效器（invalidator）通过 `wake_platform` 通知「窗口变脏了」，TestWindow 不真正投递帧、只计数，让测试能断言「唤醒协议」本身而不与帧率耦合。

配套的模拟 API：

- `simulate_scheduled_frame()`——扮演「平台的帧回调到达」：若有帧待办，就取出 gpui 注册的 `on_request_frame` 回调并执行一次，返回 true；若无帧待办（已停泊）返回 false。
- `frame_scheduled()`——查询当前是否有帧待办。
- `simulate_frame_request(options)`——不经过 frame_scheduled 闸门，直接投递一次帧请求。

三条通道各司其职：`schedule_frame`（显式请求下一帧）、`frame_waker`（失效唤醒）、`simulate_scheduled_frame`（测试扮演平台）。这个三通道模型与 Wayland 真实现的 `schedule_frame`/`frame_ping`/`wl_callback::Done` 一一对应。

#### 4.4.2 核心流程

一个窗口从创建到停泊、再被唤醒的完整时序：

```text
App::open_window → 强制绘制第一帧 → TestWindow::draw
  置 frame_callback_pending = true, frame_scheduled = true
测试：simulate_scheduled_frame()   ← 第 1 次：消费首帧请求
  gpui 的 on_request_frame 闭包运行 → draw/present
  → 画完仍置 frame_scheduled = true（等一次合成器回调）
测试：simulate_scheduled_frame()   ← 第 2 次：回调到达，无新工作
  → frame_scheduled == false       ← 渲染循环停泊（Parked）
唤醒路径 A：window.on_next_frame(cb)
  → push 回调 + platform_window.schedule_frame() → frame_scheduled = true
唤醒路径 B：cx.notify() / window.refresh()
  → invalidator.set_dirty(true) → 调 platform_waker（frame_waker）
  → TestWindow 只递增 frame_wake_count，不置 frame_scheduled
```

注意 gpui 侧帧尾的再武装逻辑：一帧跑完后若窗口仍脏或还有 next_frame 回调，会再调一次 `schedule_frame`——这保证「帧中排入的工作不会在循环停泊前被丢下」。

#### 4.4.3 源码精读

**TestWindow 的三个帧状态字段**。[../gpui/src/platform/test/window.rs:L40-L42](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/window.rs#L40-L42)：`frame_wake_count`、`frame_scheduled`、`frame_callback_pending`。

**schedule_frame 的实现**。[../gpui/src/platform/test/window.rs:L353-L358](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/window.rs#L353-L358)：`if !state.frame_callback_pending { state.frame_scheduled = true }`——已在等合成器回调时不再重复置位（幂等），与 Wayland 的 frame_ping 唤醒语义同型（u5-l4）。

**draw 的实现**。[../gpui/src/platform/test/window.rs:L394-L403](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/window.rs#L394-L403)：置 `frame_callback_pending = true` 且 `frame_scheduled = true`（呈现后必须再等一次合成器回调），若配置了无头渲染器则真正渲染场景。也就是说 TestWindow 的「present」= 标记「awaiting callback」。

**simulate_scheduled_frame 与 frame_scheduled**。[../gpui/src/platform/test/window.rs:L112-L133](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/window.rs#L112-L133)：`std::mem::take(&mut state.frame_scheduled)` 消费待办标志（没有则直接返回 false，即「已停泊」）；清掉 `frame_callback_pending`；取出 `request_frame_callback` 执行后放回。回调尚无人注册时把 `frame_scheduled` 还原并返回 false。`frame_scheduled()` 是一行查询。

**frame_waker 与 frame_wake_count**。[../gpui/src/platform/test/window.rs:L339-L347](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/window.rs#L339-L347)：注释明说设计意图——「记录调用而非投递帧，让测试断言唤醒协议而不与帧时序耦合；帧由测试经 simulate_frame_request 显式投递」。`frame_wake_count()`（L169-L172）与 `simulate_frame_request`（L176-L184）配套。

**gpui 侧：帧请求回调的注册与执行**。[../gpui/src/window.rs:L1533-L1541](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/window.rs#L1533-L1541)：窗口创建时把一整个闭包注册进 `platform_window.on_request_frame`——`simulate_scheduled_frame` 执行的就是它。

[../gpui/src/window.rs:L1613-L1622](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/window.rs#L1613-L1622)：帧回调先取出并运行全部 `next_frame_callbacks`。

[../gpui/src/window.rs:L1631-L1650](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/window.rs#L1631-L1650)：窗口脏或强制渲染时 `window.draw(cx)` + `window.present()`。

[../gpui/src/window.rs:L1652-L1669](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/window.rs#L1652-L1669)：帧尾再武装——仍脏或 next_frame 回调非空就再调 `platform_window.schedule_frame()`，并 `wake_platform()`；注释解释这是给「空闲窗口停止请求帧的平台」的显式补唤。

**两条唤醒通道的分岔点**。[../gpui/src/window.rs:L2355-L2357](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/window.rs#L2355-L2357)：`on_next_frame` 压栈回调后**立刻** `schedule_frame()`。

[../gpui/src/window.rs:L196-L212](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/window.rs#L196-L212)：`Invalidator::set_dirty(true)` 在「由净变脏」的瞬间调用 `platform_waker`——即 `cx.notify()` 走的是 frame_waker 通道，不会置 `frame_scheduled`（waker 在 [L1672](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/window.rs#L1672) 被设为 `platform_window.frame_waker()`）。

**三个真实测试**。

[../gpui/src/window.rs:L7092-L7118](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/window.rs#L7092-L7118) `queued_frame_callback_wakes_a_parked_render_loop`：先两次 `simulate_scheduled_frame` 把循环压到停泊（`frame_scheduled() == false`）；然后 `window.active.set(true)`（绕过非激活窗口的帧率节流）并 `on_next_frame(|_, _| {})`；断言 `frame_scheduled()` 为 true——「给停泊窗口排队工作必须唤醒渲染循环」。随后两次 simulate 验证呈现后仍要等一次合成器回调、再下一轮才真正停泊。

[../gpui/src/window.rs:L7120-L7136](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/window.rs#L7120-L7136) `pending_presentation_wakes_a_parked_render_loop`：停泊后直接 `window.draw(cx).clear(cx)` 造出一个「已渲染待呈现」的场景；断言 `frame_scheduled()` 为 true——TestWindow 的 draw 置位语义保证了这一点。

[../gpui/src/window.rs:L7138-L7164](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/window.rs#L7138-L7164) `callback_queued_during_a_frame_requests_a_follow_up`：注册的 next_frame 回调**内部再注册**一个 next_frame；注释说明非激活窗口会被节流、从而推迟测试手动驱动的 tick，所以先 `active.set(true)`。第一次 simulate 后断言 `frame_scheduled()` 为 true（「帧中排入的回调必须在循环停泊前请求后续帧」），第二次 simulate 后内层回调已运行。

#### 4.4.4 代码实践

**实践目标**：亲手用 `simulate_scheduled_frame` / `frame_scheduled` 验证唤醒行为，并区分两条唤醒通道。由于 `TestWindow` 与 `test_window()` 都是 `pub(crate)`（4.1.1），这个测试必须写进 gpui crate——仿照三个真实测试，追加到你本地克隆的 `crates/gpui/src/window.rs` 末尾 tests 模块中（**示例代码**，仅用于本地练习，不要提交）：

```rust
#[gpui::test]
fn refresh_wakes_a_parked_render_loop_through_the_frame_waker(cx: &mut TestAppContext) {
    let window = cx.add_window(|_, _| Empty);
    let test_window = cx.test_window(window.into());

    // 两次模拟帧回调：第一次消费首帧，第二次兑现呈现后的回调 → 停泊
    assert!(test_window.simulate_scheduled_frame());
    assert!(test_window.simulate_scheduled_frame());
    assert!(!test_window.frame_scheduled());

    let wakes_before = test_window.frame_wake_count();
    cx.update_window(window.into(), |_, window, _| window.refresh())
        .unwrap();

    // 通道 B：refresh → set_dirty → platform_waker（frame_waker）
    assert_eq!(
        test_window.frame_wake_count(),
        wakes_before + 1,
        "标脏应通过 frame_waker 通知平台"
    );
    assert!(
        !test_window.frame_scheduled(),
        "TestWindow 上标脏只唤醒、不直接安排帧；帧仍由下一次 schedule_frame 请求"
    );

    // 对照通道 A：on_next_frame 直接请求帧
    cx.update_window(window.into(), |_, window, _| {
        window.on_next_frame(|_, _| {});
    })
    .unwrap();
    assert!(test_window.frame_scheduled(), "on_next_frame 必须唤醒停泊的渲染循环");
    assert!(test_window.simulate_scheduled_frame());
}
```

1. 操作步骤：把测试加入本地 `crates/gpui/src/window.rs` 的 `mod tests`（该模块已有 `Empty`、`TestAppContext` 等 import），运行：

   ```bash
   cargo test -p gpui refresh_wakes_a_parked_render_loop
   ```

2. 需要观察的现象：`refresh` 之后 `frame_wake_count` 加一而 `frame_scheduled` 仍为 false；`on_next_frame` 之后 `frame_scheduled` 变 true。
3. 预期结果：测试通过。若把 `window.refresh()` 换成 `cx.notify()`（需要经根视图 `view.update(cx, |_, cx| cx.notify())` 触发），应观察到同样的通道 B 行为。
4. 本讲义撰写环境未修改源码、未运行该测试，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`queued_frame_callback_wakes_a_parked_render_loop` 开头为什么连续两次 `simulate_scheduled_frame` 而不是一次？

答案：第一次消费的是 `open_window` 强制首绘留下的待办帧；执行帧回调时 gpui 会 draw + present，而 TestWindow 的 `draw` 又置 `frame_scheduled = true`（呈现后须等一次合成器回调）；第二次 simulate 兑现这次回调后循环才真正无事可做、`frame_scheduled` 归 false——至此才到达「停泊」这一测试起点。

**练习 2**：`schedule_frame` 里 `if !state.frame_callback_pending` 这个守卫去掉会怎样？

答案：功能上多数场景仍工作（布尔重复置位无害），但模型就不再对应 Wayland 语义：真实平台在「已等回调」时重复请求帧是浪费甚至协议错误。这个守卫让 TestWindow 与 Wayland 实现的 frame_ping 幂等唤醒（u5-l4）保持同型，测试因此能检验 gpui 侧「不重复请求」的配合行为。

**练习 3**：为什么这三个测试都要 `window.active.set(true)` 或以停泊态为前提，而不直接开窗就断言？

答案：其一，非激活窗口受帧率节流（[../gpui/src/window.rs:L1574-L1609](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/window.rs#L1574-L1609) 的 `min_frame_interval` 分支会把帧请求推迟，测试手动驱动的 tick 会被节流逻辑劫持——第三个测试的注释明确点了这一点）；其二，这三个测试验证的正是「停泊态可被正确唤醒」，必须先把循环确定性压到停泊，断言才有明确含义。

## 5. 综合实践

综合实践把本讲四条线串成一个完整任务：**为「延时显示的 tooltip」写一套确定性测试**。

背景：u4-l3 与 gpui_platform 测试三都提到 tooltip 用 `window.spawn` + 计时器实现延时显示。请完成：

1. **逻辑层（任何平台可做）**：写一个最小组件 `DelayedBadge`——`cx.notify()` 触发后经 `window.spawn` 派生一个 300ms 计时任务，到期后把 `visible` 置 true。用 `#[gpui::test]` + `TestAppContext` 写两个断言：
   - `run_until_parked` 后仍不可见（对应 4.2 的时钟语义）；
   - `cx.dispatcher.advance_clock(400ms)` + `run_until_parked` 后可见。
   提示：完全照抄 4.2.4 的骨架，把 `flag` 换成实体字段，用 `counter.read_with(cx, |this, _| this.visible)` 断言（`read_with` 用法见 [../gpui/examples/testing.rs:L221-L246](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/examples/testing.rs#L221-L246)）。
2. **渲染调度层（gpui crate 内）**：把 4.4.4 的测试扩展成「帧循环版」：组件在 `on_next_frame` 里轮询自己的动画状态，验证（a）停泊后 `on_next_frame` 能唤醒循环；（b）回调内部再排队会请求后续帧（对照 `callback_queued_during_a_frame_requests_a_follow_up`）。
3. **视觉层（仅 macOS，可选）**：把组件放进 `VisualTestAppContext` 的离屏窗口，`advance_clock` 越过延时后 `capture_screenshot`，断言目标区域不再是背景色（像素断言思路参考 u8-l3 的 `render_to_image` 回读）。

完成标志：三组测试在你当前平台全部通过（视觉层仅 macOS），并能把每条断言对应到本讲引用的具体源码行。

## 6. 本讲小结

- gpui 的测试平台设施由 `TestPlatform`（假平台）/ `TestWindow`（假窗口）/ `TestDispatcher`（虚拟时钟调度器）组成，`#[gpui::test]` 宏按参数组装 `TestAppContext`、按种子迭代运行、并在 teardown 中用 `forbid_parking` + `drain_tasks` 做挂起任务与泄漏检测。
- `TestDispatcher` 把调度委托给 scheduler crate 的 `TestScheduler`：一个 tick 至多一个任务、加权随机挑序、`run_until_parked` 只排空就绪任务、`advance_clock` 才拨动虚拟时钟触发定时器；`dispatch_after` 在测试里直接 panic，延时必须走 `BackgroundExecutor::timer()`。
- `VisualTestPlatform` 是装饰器：执行器换成 TestDispatcher、`open_window`/`text_system` 转发真实平台、剪贴板换内存；`VisualTestAppContext` 再补上离屏窗口、`run_until_parked` / `advance_clock` / 截图。gpui_platform 的三个 `#[ignore]` 测试验证了这条集成链，且因 macOS 主线程约束必须 `--ignored --test-threads=1` 运行。
- `TestWindow` 用 `frame_scheduled` / `frame_callback_pending` 两个布尔加 `frame_wake_count` 计数器建模按需渲染循环：`schedule_frame` 显式请求下一帧、`frame_waker` 承接失效唤醒、`simulate_scheduled_frame` 让测试扮演平台投递帧回调；window.rs 的三个新测试（排队回调唤醒停泊循环、待呈现唤醒停泊循环、帧中排队请求后续帧）示范了确定性驱动这套状态机的标准写法。
- 可见性边界决定了测试的落点：`TestDispatcher` 对外公开（延时/调度测试可写在任何下游 crate），`TestWindow` 与 `test_window()` 仅 `pub(crate)`（帧调度测试必须写进 gpui crate）。

## 7. 下一步学习建议

本讲是高级单元的倒数第二讲。下一步：

- **u8-l5（毕业实践：实现一个自定义 Platform）**：本讲已让你从「消费者」视角看清 Platform 契约的边界与默认实现的减负作用，u8-l5 将切换到「实现者」视角，动手写一个 FakePlatform——`TestPlatform`（4.1.3）就是最好的最小实现参考。
- **延伸阅读 1**：`crates/scheduler/src/test_scheduler.rs` 全文——本讲只走了 `run` / `step` / `advance_clock` 主干，其中 `park` 的硬超时、`blocked_sessions`、`is_main_thread` 的切换（执行前台任务时临时置真，[L334-L341](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/scheduler/src/test_scheduler.rs#L334-L341)）值得细读。
- **延伸阅读 2**：`crates/gpui/examples/testing.rs` 的 `mod distributed_systems`——双 `TestAppContext` 共享调度器做随机交错测试，是「确定性调度」能力的极致展示。
- **延伸阅读 3**：对照 u5-l4 的 Wayland `FrameLoop` 源码重读 4.4，体会 `TestWindow` 两个布尔如何浓缩八态状态机的可测子集。
