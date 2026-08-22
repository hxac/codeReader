# 毕业实践：实现一个自定义 Platform

## 1. 本讲目标

这是本手册的最后一讲，也是唯一一讲「不看新机制、只造新东西」的讲义。学完后你应该能够：

1. 不看参考答案，独立判断 `Platform` trait 中哪些方法必须实现、哪些可以直接吃默认值、哪些会随编译目标消失或出现。
2. 复用 gpui 已经提供的通用构件——`ThreadedDispatcher`、`NoopTextSystem`、`DummyKeyboardMapper`——而不是从零造调度器和字体引擎。
3. 把一份自己写的 `FakePlatform` 通过 `Application::with_platform` 注入 gpui，跑起一个「窗口存在但不上屏」的完整应用，并用日志验证至少五个平台方法被真实调用。
4. 由此对一个真实问题建立量感：把 GPUI 移植到一个新操作系统或新窗口系统，工作量到底分布在哪里。

本讲的实践是全手册的综合毕业设计，会同时用到 u2（契约分组）、u3（PlatformWindow）、u4（调度器）、u8-l1（文本系统）的知识。

## 2. 前置知识

本讲不再引入新的平台机制，但要把前几讲散落的三个结论拧成一股绳：

1. **平台是注入的，不是选出来的。** gpui 主 crate 从头到尾不知道 macOS、Windows、Linux 的存在。它只认识 `Rc<dyn Platform>` 这根「插座」，谁实现 `Platform` trait，谁就是平台。`gpui_platform` 门面里的 `current_platform()` 只是官方提供的四个默认插座之一；`Application::with_platform` 才是真正的注入口——u1-l1、u1-l2 已建立这个模型，本讲我们第一次自己动手用这个注入口。

2. **调度器与平台是两个 trait、两种线程语义。** `Platform` 以 `Rc` 持有、只在主线程使用；`PlatformDispatcher` 被 `Arc` 共享、必须 `Send + Sync`（u4-l2）。这意味着一个自定义平台可以完全不管线程池怎么搭——把 `ThreadedDispatcher` 搬过来，用 `BackgroundExecutor::new` / `ForegroundExecutor::new` 包一层，执行器部分就完成了。

3. **默认实现是契约的一部分。** u2-l1 统计过：`Platform` 共 69 个方法，18 个带默认实现，4 个受平台 cfg 门控。带默认实现的方法有三种姿态——能力探测型（返回 `None`/`false`，调用方自行降级）、优雅降级 no-op 型（静默忽略）、通用回退型（默认体给出可用结果）。写自定义平台时，默认值不是「偷懒」，而是契约明说的「你可以不实现」。

另外两个术语提醒：

- **测试替身（test double）**：为测试目的造的假实现。gpui 内部就有一个完整的 `TestPlatform`，本讲把它当「参考答案」精读——但你不能直接 `use` 它，原因见 4.3。
- **`todo!()` / `unimplemented!()`**：Rust 的运行期恐慌宏。测试替身里没意义的方法常用它占位，表示「调用到这里说明测试写错了」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| `../gpui/src/platform.rs` | `Platform` / `PlatformWindow` / `PlatformDispatcher` / `PlatformTextSystem` 契约权威定义 | 数清楚「实现面」：哪些必需、哪些有默认 |
| `../gpui/src/platform/threaded_dispatcher.rs` | 测试与基准用的多线程调度器 | 唯一可直接复用的调度器构件 |
| `../gpui/src/platform/test/platform.rs` | gpui 内部测试替身 `TestPlatform` | 最小实现的参考答案（`pub(crate)`，不可直接引用） |
| `../gpui/src/app.rs` | `Application` / `App`，启动序列 | `with_platform` 注入点与 `run` 回调时机 |
| `src/gpui_platform.rs` | 门面 crate | 对照：官方四个平台如何走同一个注入口 |
| `../gpui/src/app/headless_app_context.rs` | 无头测试上下文 | 最佳「组装配方」：调度器→执行器→平台的完整顺序 |
| `../gpui/src/executor.rs` | 前后台执行器构造 | `BackgroundExecutor::new` / `ForegroundExecutor::new` |
| `../gpui/src/platform/keyboard.rs` | 键盘契约 | `DummyKeyboardMapper` 现成的透传实现 |
| `../gpui/src/platform/test/window.rs` | `TestWindow` / `TestAtlas` | 假窗口如何拒绝 raw-window-handle、最小图集长什么样 |

## 4. 核心概念与源码讲解

### 4.1 Platform 契约的实现面：必需、默认与 cfg 门控

#### 4.1.1 概念说明

实现 `Platform` 之前要先回答一个问题：**这份契约到底要我为它写多少行代码？** 答案由三张清单决定：

- **必需方法**：trait 里没有默认体的方法，不写编译不过。它们是「一个 GPUI 平台」的定义性特征——没有执行器就没有并发模型，没有 `run` 就没有事件循环，没有 `open_window` 就没有窗口。
- **默认方法**：trait 里带 `{ ... }` 默认体的方法。默认体要么返回「不支持」，要么给出通用结果；只要你的平台没有特别能力，一行都不用写。
- **cfg 门控方法**：用 `#[cfg(target_os = ...)]` 标注的方法，只在特定编译目标下存在于 trait 中。在 Linux 上编译，`read_from_primary` / `write_to_primary` 就是必需方法；在 macOS 上编译，换成 `read_from_find_pasteboard` / `write_to_find_pasteboard`；在两者之外的虚构目标上，这两组都消失。

#### 4.1.2 核心流程

给一个自定义平台分类方法的决策流程：

```text
对 Platform 的每个方法：
  ├── 是否被 cfg 门控且在我的编译目标上不存在？ → 跳过
  ├── 有没有默认实现？
  │     ├── 有，且属于「能力探测 / no-op」姿态 → 不实现（调用方会降级）
  │     └── 有，但默认值对我的平台是错的 → 覆盖
  └── 必需 → 必须实现，按三档取值：
        ├── 真实工作（执行器、text_system、run、open_window）
        ├── 合理假值（displays 返回一张假屏、is_active 返回 false）
        └── 显式失败（Task::ready(Err(...)) 或 unimplemented!()）
```

#### 4.1.3 源码精读

先看契约的地基——三个执行器/文本系统方法加生命周期组，全部必需、无默认：

[../gpui/src/platform.rs:125-137](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L125-L137)

这段是 `Platform` trait 的开头：`background_executor`、`foreground_executor`、`text_system` 三个「取设施」方法，以及 `run` / `quit` / `restart` / `activate` / `hide` 家族。没有任何默认体——这就是最小平台的骨架。

窗口与显示器组同样必需：

[../gpui/src/platform.rs:139-166](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L139-L166)

`displays`、`primary_display`、`active_window`、`open_window` 必须实现；紧接着的 `window_stack`（返回 `None`）、`is_screen_capture_supported`（返回 `false`）、`screen_capture_sources`（直接回 Err）就是「能力探测型」默认值的标本——默认实现替你宣布「本平台没有这个能力」，调用方据此降级。

再看三类默认姿态的代表作：

[../gpui/src/platform.rs:216-229](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L216-L229)

移动端专属的 `on_app_lifecycle`、`on_memory_warning` 与 `gestures` 全是默认空实现，文档明确写着「桌面平台永远不会触发」——你的自定义桌面平台一行都不用写。

[../gpui/src/platform.rs:259-291](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L259-L291)

`set_app_identity`、`show_system_notification`、`dismiss_system_notification`、`on_system_notification_response` 也全是 no-op 默认（参数用 `_ =` 丢弃）。这意味着**一个只会日志的平台可以免费获得「通知静默忽略」的语义**，与应用层预期完全一致。

[../gpui/src/platform.rs:320-322](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L320-L322)

`read_from_clipboard_async` 是「通用回退型」默认：默认体转调同步的 `read_from_clipboard` 并包成 `Task::ready`。只有浏览器（权限门控的异步剪贴板）才需要覆盖。

[../gpui/src/platform.rs:324-332](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L324-L332)

最后是 cfg 门控组：Linux/FreeBSD 的主选区两方法与 macOS 的查找板两方法。**在 Linux 上写自定义平台，主选区读写是逃不掉的必需方法**——这是「最小实现量随目标浮动」的直接原因（u2-l1 的统计：47～49 个）。

文本系统方面还有一个「零成本答案」：

[../gpui/src/platform.rs:1106-1114](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L1106-L1114)

`NoopTextSystem` 是 gpui 自带的空文本系统：不加载任何字体，但 `layout_line` 会用固定宽度的假度量把每个字符排开（见 [../gpui/src/platform.rs:1179-1221](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L1179-L1221)）。对「只要布局管线能转、不在乎字形真实」的假平台，它就是 `text_system()` 的现成返回值；要真实字形时再换成 `gpui_wgpu::CosmicTextSystem::new("fallback")`（见 [../gpui/src/app/headless_app_context.rs:31](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/headless_app_context.rs#L31) 的用法示例）。

#### 4.1.4 代码实践

**实践目标**：用编译器帮你数出当前目标上的必需方法清单——这比手数可靠得多。

1. 新建一个独立 crate，`Cargo.toml` 中依赖 `gpui`（path 指向本地 zed 检出的 `crates/gpui`，开启 `test-support` feature，理由见 4.2）。
2. 写一个空的 `struct FakePlatform;`，然后写 `impl Platform for FakePlatform {}`。
3. 运行 `cargo build`，把编译器报出的每个 `not all trait items were implemented` 错误里的方法名抄进一张表。
4. 对照 [../gpui/src/platform.rs:125-341](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L125-L341)，给表中每个方法标注你打算给它的三档取值（真实工作 / 合理假值 / 显式失败）。

**需要观察的现象**：编译错误列表就是你这个编译目标上的「必需方法全集」；在 Linux 上它比在 macOS 上多出主选区两项、少出查找板两项。

**预期结果**：得到一张与 u2-l1 分组一致的实现规划表（数量在 47～49 附近，随目标浮动）。编译器输出即证据，无需本地验证结论，但具体条数取决于你的编译目标，建议实际跑一次 `cargo build` 确认。

#### 4.1.5 小练习与答案

**练习 1**：你的假平台不打算支持屏幕捕获。需要实现 `screen_capture_sources` 吗？

**答案**：不需要。默认实现（[platform.rs:150-160](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L150-L160)）已经通过 oneshot 通道回 Err「gpui was compiled without the screen-capture feature」，且 `is_screen_capture_supported` 默认 `false`，调用方在 UI 上根本不会走到这里。

**练习 2**：`compositor_name()` 有默认实现返回空串（[platform.rs:293-295](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L293-L295)）。为什么 Linux 三后端仍然都覆盖它？

**答案**：它不是 no-op 语义而是「身份报告」——u1-l4 用 `compositor_name()` 作为观测出口区分 Wayland/X11/headless。返回空串意味着遥测里平台一栏为空。假平台覆盖成 `"Fake"` 便于日志区分，属于「默认值对我的平台是错的」这一档。

**练习 3**：`quit` 是必需方法，但假平台没有操作系统退出协议可调。它的正确实现是什么？

**答案**：让自己 `run` 循环退出的那个信号。`quit` 只该「发退出请求」（u2-l2 的结论：只发信号、不杀进程），所以典型实现是置一个 `AtomicBool`，`run` 循环每轮检查它。本讲综合实践就是这么写的。

### 4.2 ThreadedDispatcher：可以整体搬走的调度器

#### 4.2.1 概念说明

u4-l2 建立的结论：`PlatformDispatcher` 是平台无关执行器与宿主事件循环之间的桥，契约只有五个必需方法。u4-l3～u4-l5 看了四个平台各自实现它有多讲究（calloop、GCD、Windows 消息、浏览器邮箱）。好消息是：**gpui 把一个通用实现直接送给你了**——`ThreadedDispatcher`，随 `test-support` feature 编译并公开导出：

[../gpui/src/platform.rs:12-16](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L12-L16)（模块门控）与 [../gpui/src/platform.rs:84-88](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L84-L88)（公开导出 `ThreadedDispatcher`）。

它的工作方式与生产调度器同构：后台任务跑在真实线程池上、定时器按真实时间触发，唯一差别是「主线程任务没有宿主 run loop 帮你泵」，需要你在自己的 `run` 里手动排空。文档原话：「镜像生产调度器（参见 `LinuxDispatcher`）」。

#### 4.2.2 核心流程

`ThreadedDispatcher` 内部有四块状态、三类线程：

```text
ThreadedDispatcher
  ├── background_sender / main_sender   两条优先级队列通道
  ├── main_receiver（Mutex 包裹）        主线程任务收件箱，等你的 run 循环来取
  ├── TimerQueue（堆 + Condvar）        专职定时器线程按到期时间弹出
  ├── IdleTracker（in-flight 计数）     追踪"排队中/执行中"的后台与定时器任务
  └── 线程：N 个 Worker（N = max(CPU 数, 2)）+ 1 个 Timer + 按需 Realtime

任务旅程：
  cx.background_spawn(f) → dispatch() → Worker 线程池执行
  cx.spawn(f)            → dispatch_on_main_thread() → main_receiver 排队
                            ↑ 你在 run 循环里调 run_until_idle() 取出执行
  executor.timer(d)      → dispatch_after(d) → Timer 线程到点投递
```

#### 4.2.3 源码精读

[../gpui/src/platform/threaded_dispatcher.rs:28-35](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/threaded_dispatcher.rs#L28-L35)

结构体字段即上面的四块状态。注意 `main_thread_id: thread::ThreadId`——构造线程就是「主线程」，这正是自定义平台 `run` 必须被调用的线程。

[../gpui/src/platform/threaded_dispatcher.rs:120-209](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/threaded_dispatcher.rs#L120-L209)

`new()` 做全部重活：起线程池（L130-148，每个 Worker 在 `receiver.pop()` 上阻塞）、起定时器线程（L151-199，`wait_until` 到期弹出执行）。Worker 与定时器执行 runnable 前后都插了 profiler 钩子——u4-l6 讲过的对称插桩在这里也能看到。

[../gpui/src/platform/threaded_dispatcher.rs:220-258](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/threaded_dispatcher.rs#L220-L258)

`run_until_idle()` 是你的 `run` 循环要调的核心方法：排空主队列 → 检查到期定时器 → 在 `IdleTracker` 的 Condvar 上等新工作，直到「无主线程工作、无在途后台任务、无到期定时器」才返回。注意文档警告：**未到期的定时器不算工作**——它不会替你等 2 秒后的闹钟，所以外层要自己控制节奏（见综合实践里 `sleep` + 轮询的写法）。

[../gpui/src/platform/threaded_dispatcher.rs:414-463](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/threaded_dispatcher.rs#L414-L463)

`PlatformDispatcher` 契约实现：`dispatch`（L419-424，失败直接 panic，因为 Worker 线程应当与进程同寿）、`dispatch_on_main_thread`（L426-437，**发送失败时 `std::mem::forget` 防 `!Send` future 在错误线程 drop 的未定义行为**——与 u4-l3 LinuxDispatcher 同一戒律）、`dispatch_after`（L439-449）、`spawn_realtime`（L451-458，起普通线程即可）。

最后是把它变成执行器的两行：

[../gpui/src/executor.rs:65-70](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/executor.rs#L65-L70) 与 [../gpui/src/executor.rs:294-299](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/executor.rs#L294-L299)

`BackgroundExecutor::new(Arc<dyn PlatformDispatcher>)` 与 `ForegroundExecutor::new(Arc<dyn PlatformDispatcher>)` 都只要一个 `Arc` 化的调度器——前后台执行器共享同一个 `ThreadedDispatcher` 实例，这正是 u4-l1 说的「平台只造一个 dispatcher，构造两个执行器」。

#### 4.2.4 代码实践

**实践目标**：在写任何 `Platform` 之前，先单独验证「调度器 → 执行器」这对构件能转。

1. 在练习 crate 里写（示例代码）：

   ```rust
   use gpui::{BackgroundExecutor, ForegroundExecutor, ThreadedDispatcher};
   use std::sync::Arc;
   use std::time::Duration;

   fn main() {
       let dispatcher = Arc::new(ThreadedDispatcher::new());
       let background = BackgroundExecutor::new(dispatcher.clone());
       let foreground = ForegroundExecutor::new(dispatcher.clone());

       let main_id = std::thread::current().id();
       foreground
           .spawn(async move {
               println!("前台任务在线程 {:?}", std::thread::current().id());
               assert_eq!(std::thread::current().id(), main_id);
           })
           .detach();
       background
           .spawn(async move {
               println!("后台任务线程 id: {:?}", std::thread::current().id());
           })
           .detach();
       dispatcher.run_until_idle();
       std::thread::sleep(Duration::from_millis(50));
   }
   ```

2. 运行并对照两个打印的线程 id。

**需要观察的现象**：前台任务打印的线程 id 与 main 相同（它被 `run_until_idle` 在调用线程上执行）；后台任务打印的是 `ThreadedDispatcherWorker-N` 的 id。

**预期结果**：断言通过即证明执行器组装正确。此程序依赖 `gpui` 的 `test-support` feature（否则 `ThreadedDispatcher` 不存在），输出细节待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ThreadedDispatcher` 要 `Arc` 包一层再传给两个执行器？

**答案**：执行器只持有 `Arc<dyn PlatformDispatcher>`，且 `PlatformDispatcher: Send + Sync`（[platform.rs:1029](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L1029)）——后台线程也要通过它投递唤醒。同时你的平台结构体持有同一个 `Arc`，`run` 循环里才能调 `run_until_idle`。一份实例、三处共享。

**练习 2**：`run_until_idle` 返回了，但 3 秒后有个 `executor.timer(3s)` 会到期。程序直接退出会发生什么？怎么改？

**答案**：什么都不会发生——定时器还没入执行队列，进程一退线程全灭。所以自定义 `run` 循环不能只调一次 `run_until_idle`，要「排空 → 小睡 → 再排空」地轮询，让未来的定时器有机会到期；综合实践里用 15ms 睡眠模拟一个粗糙的 60Hz 泵。

**练习 3**：`dispatch_on_main_thread` 发送失败时为什么 `forget` 而不是返回错误？

**答案**：见 [threaded_dispatcher.rs:426-437](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/threaded_dispatcher.rs#L426-L437) 的注释：runnable 可能包着 `!Send` future，若在错误线程 drop 会触发未定义行为，宁可泄漏。这是 u4-l2/u4-l3 反复出现的平台戒律。

### 4.3 TestPlatform：一份「最小实现」的参考答案

#### 4.3.1 概念说明

gpui 内部早就写好了一个假平台——`TestPlatform`，供 `#[gpui::test]` 宏组装测试上下文。它是本讲最好的参考答案：**它把「哪些方法认真做、哪些方法潦草做、哪些方法直接 `unimplemented!()`」的三档取舍完整示范了一遍**。

但有一个关键限制：`TestPlatform` 是 `pub(crate)` 的——

[../gpui/src/platform.rs:81-85](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L81-L85)

平台模块对 test 目录整体 `pub(crate) use test::*`，只把 `TestDispatcher`、`TestScreenCaptureSource`、`TestScreenCaptureStream` 三个类型真正公开。所以外部 crate（也就是你的毕业设计）**不能** `use gpui::TestPlatform`，只能照着它抄。这恰恰是本练习的教育意义所在。

#### 4.3.2 核心流程

`TestPlatform` 的组装配方（也是 gpui 里所有「非官方平台」的标准组装顺序）：

```text
1. 造调度器            TestDispatcher::new(seed)          （虚拟时钟，单线程）
2. Arc 化              Arc::new(dispatcher.clone())
3. 造两个执行器        BackgroundExecutor::new(arc.clone())
                       ForegroundExecutor::new(arc)
4. 造平台              TestPlatform::with_platform(
                           background_executor, foreground_executor,
                           text_system, renderer_factory)
5. 注入应用            App::new_app(Rc 平台, asset_source, http_client)
```

#### 4.3.3 源码精读

[../gpui/src/app/headless_app_context.rs:65-101](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/headless_app_context.rs#L65-L101)

上面流程图的权威出处：`HeadlessAppContext::with_platform` 第 75-78 行依次构造 `TestDispatcher` 与两个执行器，第 82-87 行把它们连同文本系统、渲染器工厂交给 `TestPlatform::with_platform`，第 91 行注入 `App::new_app`。把 `TestDispatcher` 换成 `ThreadedDispatcher`、把 `TestPlatform` 换成你的 `FakePlatform`，就是本讲综合实践的装配图。

[../gpui/src/platform/test/platform.rs:21-42](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/platform.rs#L21-L42)

`TestPlatform` 的状态面：两个执行器直接存 clone（构造时注入，见 [test/platform.rs:124-152](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/platform.rs#L124-L152) 的 `with_platform`），剪贴板、光标、提示框、通知全是内存假状态。注意 Linux/macOS 的 cfg 字段也照门控出现——测试替身同样要遵守 cfg 规则。

再看它对三档取舍的示范：

[../gpui/src/platform/test/platform.rs:309-341](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/platform.rs#L309-L341)

「认真做」的五个方法：执行器两方法原样返回存的 clone，`text_system` 返回注入的 `Arc`，键盘三件套给假布局 + `DummyKeyboardMapper`（[keyboard.rs:26-41](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/keyboard.rs#L26-L41) 的透传实现）。而 `run` 直接 `unimplemented!()`——测试里 `run` 永不该被调用，调到即测试写错。

[../gpui/src/platform/test/platform.rs:399-413](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/platform.rs#L399-L413)

「合理假值」的样板：`open_window` 不创建任何真实窗口，只是 `TestWindow::new(handle, params, ...)` 包个假窗口返回。你的 `FakePlatform::open_window` 可以逐行照抄这个形状。

[../gpui/src/platform/test/platform.rs:538-544](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/platform.rs#L538-L544) 与 [../gpui/src/platform/test/platform.rs:566-576](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/platform.rs#L566-L576)

剪贴板用 `Mutex<Option<ClipboardItem>>` 存内存值（写进读出，测试可断言）；凭据三件套用 `Task::ready(Ok(()))` / `Task::ready(Ok(None))` 立即成功——假平台的异步方法用 `Task::ready` 给确定结果，是比 `unimplemented!()` 更常用的第三档写法。

最后看假窗口的两个细节，综合实践会用到：

[../gpui/src/platform/test/window.rs:51-69](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/window.rs#L51-L69)

`TestWindow` 对 raw-window-handle 两个 supertrait 的实现是返回 `Err(HandleError::NotSupported)`——没有真实 GPU 窗口的假窗口就该这样（u3-l2 的结论：headless 同款处理）。

[../gpui/src/platform/test/window.rs:460-527](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/window.rs#L460-L527)

`TestAtlas` 展示了 `PlatformAtlas` 契约的最小实现：一个 `HashMap<AtlasKey, AtlasTile>` 缓存加三个方法（`get_or_insert_with` / `remove` / `contains`），不碰任何 GPU。你的 `FakeWindow::sprite_atlas` 需要同款自绘图集（注意 `TestAtlas` 也是 `pub(crate)`，抄不能引）。

#### 4.3.4 代码实践

**实践目标**：把参考答案读成一张对照表，为综合实践定稿。

1. 通读 [test/platform.rs:309-585](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform/test/platform.rs#L309-L585) 的 `impl Platform for TestPlatform`。
2. 为每个方法打上三档标签之一：真实工作 / 内存假值 / `unimplemented!()`，统计三档数量。
3. 对你的 `FakePlatform` 逐行决定：照抄（如剪贴板内存值、`Task::ready`）、改造（如 `run`——你要真的能跑，不能 `unimplemented!()`）、还是依赖默认（如通知组整个删掉不写）。

**需要观察的现象**：`unimplemented!()` 集中在 `run`、`hide`、`reveal_path`、`app_path` 等与「真实操作系统交互」强相关的方法上；内存假值集中在可断言的状态（剪贴板、通知、提示框）上。

**预期结果**：一张三列对照表（TestPlatform 的做法 / 你是否照抄 / 理由）。这是纯阅读实践，结论可离线得出。

#### 4.3.5 小练习与答案

**练习 1**：`TestPlatform::run` 是 `unimplemented!()`，而 `FakePlatform::run` 必须真的跑起来。两者为什么不同？

**答案**：测试由 `#[gpui::test]` 宏驱动，任务用 `run_until_parked` 手动排空，永远不走 `Platform::run`（u8-l4）；而毕业设计要走 `Application::run` 的正式启动序列，`run` 是事件循环本体，必须实现「执行启动回调 + 泵主队列」的循环。

**练习 2**：为什么 gpui 只公开 `TestDispatcher` 却把 `TestPlatform` / `TestWindow` 留在 `pub(crate)`？

**答案**：`TestDispatcher` 公开是因为测试代码需要直接操作它（`run_until_parked`、`advance_clock`，u8-l4 的 VisualTest 体系）；而 `TestPlatform`/`TestWindow` 的假状态字段直接暴露给 gpui 内部测试断言（`pub(crate)` 字段），公开它们就得稳定整个内部状态布局，不值得。对使用者的启示是：想定制平台行为，正路是注入自己的实现，而不是复用测试替身。

**练习 3**：`TestPlatform` 的键盘布局用假 id `"zed.keyboard.example"`，mapper 用 `DummyKeyboardMapper`。你的 `FakePlatform` 照抄会有什么限制？

**答案**：按键的 `key` 字段不再反映真实布局（没有布局可反映），键位绑定按美式直觉工作即可；`DummyKeyboardMapper` 只是透传（`KeybindingKeystroke::from_keystroke`），等于声明「本平台没有 macOS key equivalents 那类改写」。对一个日志平台这完全够用——u3-l3 讲过，非 macOS 平台本来就该用 Dummy。

### 4.4 Application::with_platform：注入点与启动序列

#### 4.4.1 概念说明

`Application::with_platform(platform: Rc<dyn Platform>)` 是 gpui 对外公开的「换平台」正门——u1-l1 说过门面 crate 的 `application()` / `headless()` 内部也只是转调它。它接收一个 `Rc<dyn Platform>`，立即构造整个应用状态。理解两件事就够了：

1. **构造时平台被「抽取」了什么**：启动那一刻 gpui 从平台抽出执行器、文本系统、键盘布局/映射器存进 `App`，之后运行期逐方法转发——所以平台必须在构造前完全初始化好。
2. **`run` 的控制权移交**：`Application::run` 把启动回调装箱交给 `platform.run`，之后**线程的控制权完全交给你的平台**——什么时候执行回调、何时返回（即何时退出进程），都是平台说了算。

#### 4.4.2 核心流程

```text
FakePlatform::new()                     主线程：造 dispatcher + 执行器
        ↓
Application::with_platform(Rc 平台)
        ↓ App::new_app
        ├── platform.background_executor()   抽取
        ├── assert!(is_main_thread())        必须在主线程构造
        ├── TextSystem::new(platform.text_system())
        ├── platform.keyboard_layout() / keyboard_mapper()
        └── 构建 AppCell（RefCell 包着的全局状态）
        ↓
Application::run(on_finish_launching)
        └── platform.run(Box::new(回调))     ← 控制权移交，阻塞开始
                （你的 FakePlatform::run 实现）
                ├── on_finish_launching()    ← 此时 App 已可用：开窗、spawn
                └── 循环 { run_until_idle; 检查 quit; sleep }
```

#### 4.4.3 源码精读

[../gpui/src/app.rs:176-183](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app.rs#L176-L183)

`with_platform` 本体只有一行实质：用平台加空资产源加 `NullHttpClient` 调 `new_app`。要自定义资产（图标、SVG），链式调 `with_assets`；要真 HTTP，链式调 `with_http_client`（[app.rs:216-222](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app.rs#L216-L222)）——u7-l1 见过 wasm 入口就是这么补 FetchHttpClient 的。

[../gpui/src/app.rs:779-817](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app.rs#L779-L817)

`new_app` 的抽取序列：L784-785 取两个执行器；**L786-789 断言 `background_executor.is_main_thread()`——必须在主线程构造 App**（这个断言转发到你的 dispatcher 的 `is_main_thread`，`ThreadedDispatcher` 用构造线程 id 判断）；L794 把平台文本系统包进缓存层 `TextSystem`（u8-l1 讲过的回退字体栈就在这层）；L796-797 取键盘布局与映射器。

[../gpui/src/app.rs:233-243](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app.rs#L233-L243)

`run`：取 `Rc<AppCell>` 的 clone 塞进闭包，装箱交给 `platform.run`。你的 `FakePlatform::run` 执行这个闭包时，闭包内部 `borrow_mut()` 拿到的就是就绪的 `&mut App`——所以回调里能安全地开窗、注册 action、spawn 任务。

[../gpui/src/app.rs:254-265](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app.rs#L254-L265)

`run_embedded` 是控制权不移交的变体：平台的 `run` 调完回调立即返回，`ApplicationHandle` 让宿主（wasm guest、外部原生应用内嵌 GPUI 视图）持有 App 生存期并随时再进入。若你的假平台宿主是一个更大的程序（比如游戏引擎的某一帧），选这个入口而不是 `run`。

对照官方入口的门面写法：

[src/gpui_platform.rs:13-25](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui_platform/src/gpui_platform.rs#L13-L25)

`application()` 与 `headless()` 都汇聚到 `Application::with_platform(current_platform(...))`——你的 `FakePlatform::new()` 返回的 `Rc` 与 `current_platform` 返回的 `Rc` 在类型上无法区分（都是 `Rc<dyn Platform>`），这就是门面模式承诺的全部。

#### 4.4.4 代码实践

**实践目标**：在 `FakePlatform` 尚未完整实现前，先验证注入链路本身通不通。

1. 给 `FakePlatform` 只实现四个方法：两个执行器（用 4.2 的组装）、`text_system`（返回 `Arc::new(NoopTextSystem)`）、`run`（打印一行、执行回调、立即返回）。
2. 其余必需方法先全部写成 `todo!("{}")` 形式的占位（借助 4.1.4 得到的清单），让编译通过。
3. `main` 里写：

   ```rust
   fn main() {
       let platform = FakePlatform::new();
       Application::with_platform(platform).run(|_cx| {
           println!("on_finish_launching 已执行");
       });
       println!("run 已返回，进程即将结束");
   }
   ```

4. 运行观察三行输出的顺序。

**需要观察的现象**：三行按「run 进入 → 启动回调 → run 返回」顺序出现；因为你的 `run` 不循环，进程立即结束。

**预期结果**：证明「构造 App 时抽取执行器/文本系统 → run 移交控制权 → 回调拿到就绪 App」链路成立。若忘记在主线程构造（比如把 `FakePlatform::new` 挪进 `thread::spawn`），会命中 `new_app` 的断言 panic——可以故意试一次。运行输出待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`Application::run` 之后 `self`（`Application`）被消费了。App 状态为什么不会跟着被销毁？

**答案**：`run` 在装箱回调前克隆了 `Rc<AppCell>`（[app.rs:237](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app.rs#L237)），闭包持有强引用，闭包又被 `Box` 进平台存活——引用计数撑过 `Application` 的析构。`run_embedded` 则由返回的 `ApplicationHandle` 持有（[app.rs:264](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app.rs#L264)）。

**练习 2**：如果 `FakePlatform::run` 忘了调 `on_finish_launching`，程序表现是什么？

**答案**：没有任何窗口和任务被创建，`run` 循环空转（或按你的实现立即返回）——启动回调是应用侧唯一的初始化时机（u1-l2 的结论），跳过它 gpui 内部状态完好但什么都不会发生。这也是为什么综合实践把「回调必须被执行」列为验收第一条。

**练习 3**：`App::quit`（[app.rs:1014-1016](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app.rs#L1014-L1016)）只是转调 `platform.quit()`。谁负责收尾清理？

**答案**：平台负责让 `run` 返回；u2-l2 讲过事件循环退出后 gpui 才触发 `on_quit` 观察者与 `App::shutdown` 的收尾链。对 `FakePlatform` 而言，`quit` 置标志、`run` 循环检测到后退出循环即完成「优雅退出」的最小闭环（收尾观察者未注册时无额外动作）。

## 5. 综合实践

### 毕业设计：FakePlatform + FakeWindow + 日志验收

把 4.1～4.4 的积木全部拼起来。**目标**：一个不依赖任何操作系统的 gpui 应用——`FakePlatform` 把所有系统调用记录到日志，`FakeWindow` 让窗口逻辑（布局、实体、首帧绘制）真实运转但不上屏。运行后日志里应出现至少五个平台方法的调用记录，窗口标题被设置、一帧被绘制、两秒后优雅退出。

**第 1 步：crate 骨架**（示例代码）

```toml
# Cargo.toml —— path 指向你本地的 zed 检出
[package]
name = "fake-platform-demo"
version = "0.1.0"
edition = "2021"

[dependencies]
gpui = { path = "../zed/crates/gpui", features = ["test-support"] }
anyhow = "1"
uuid = { version = "1", features = ["v4"] }
```

`test-support` feature 是关键：它同时带来 `ThreadedDispatcher`（4.2）。

**第 2 步：FakePlatform**（示例代码，核心部分；完整方法清单以 4.1.4 编译器输出为准）

```rust
use anyhow::Result;
use gpui::*;
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

pub struct FakePlatform {
    dispatcher: Arc<ThreadedDispatcher>,
    background_executor: BackgroundExecutor,
    foreground_executor: ForegroundExecutor,
    quit_flag: Arc<AtomicBool>,
}

impl FakePlatform {
    pub fn new() -> Rc<Self> {
        let dispatcher = Arc::new(ThreadedDispatcher::new());
        let background_executor = BackgroundExecutor::new(dispatcher.clone());
        let foreground_executor = ForegroundExecutor::new(dispatcher.clone());
        Rc::new(Self {
            dispatcher,
            background_executor,
            foreground_executor,
            quit_flag: Arc::new(AtomicBool::new(false)),
        })
    }
}

impl Platform for FakePlatform {
    fn background_executor(&self) -> BackgroundExecutor {
        self.background_executor.clone()
    }
    fn foreground_executor(&self) -> ForegroundExecutor {
        self.foreground_executor.clone()
    }
    fn text_system(&self) -> Arc<dyn PlatformTextSystem> {
        eprintln!("[fake] text_system()");
        Arc::new(NoopTextSystem)   // 或 gpui_wgpu::CosmicTextSystem::new("fallback")
    }

    fn run(&self, on_finish_launching: Box<dyn FnOnce()>) {
        eprintln!("[fake] run() 进入事件循环");
        on_finish_launching();
        while !self.quit_flag.load(Ordering::SeqCst) {
            self.dispatcher.run_until_idle();               // 泵主队列 + 等后台
            std::thread::sleep(Duration::from_millis(15));  // 粗糙的 ~60Hz 心跳
        }
        eprintln!("[fake] run() 退出，进程收尾");
    }
    fn quit(&self) {
        eprintln!("[fake] quit()");
        self.quit_flag.store(true, Ordering::SeqCst);
    }

    fn restart(&self, _: Option<_>, _: Vec<_>) { eprintln!("[fake] restart()"); }
    fn activate(&self, _: bool) { eprintln!("[fake] activate()"); }
    fn hide(&self) { eprintln!("[fake] hide()"); }
    fn hide_other_apps(&self) { eprintln!("[fake] hide_other_apps()"); }
    fn unhide_other_apps(&self) { eprintln!("[fake] unhide_other_apps()"); }

    fn displays(&self) -> Vec<Rc<dyn PlatformDisplay>> {
        eprintln!("[fake] displays()");
        vec![Rc::new(FakeDisplay)]
    }
    fn primary_display(&self) -> Option<Rc<dyn PlatformDisplay>> {
        Some(Rc::new(FakeDisplay))
    }
    fn active_window(&self) -> Option<AnyWindowHandle> { None }

    fn open_window(
        &self,
        handle: AnyWindowHandle,
        options: WindowParams,
    ) -> Result<Box<dyn PlatformWindow>> {
        eprintln!(
            "[fake] open_window(bounds={:?}, titlebar={:?})",
            options.bounds,
            options.titlebar.as_ref().and_then(|t| t.title.clone())
        );
        Ok(Box::new(FakeWindow::new(handle, options.bounds)))
    }

    fn window_appearance(&self) -> WindowAppearance { WindowAppearance::Light }
    fn open_url(&self, url: &str) { eprintln!("[fake] open_url({url})"); }
    fn on_open_urls(&self, _: Box<dyn FnMut(Vec<String>)>) {}
    fn register_url_scheme(&self, url: &str) -> Task<Result<()>> {
        eprintln!("[fake] register_url_scheme({url})");
        Task::ready(Ok(()))
    }
    fn prompt_for_paths(&self, options: PathPromptOptions)
        -> futures::channel::oneshot::Receiver<Result<Option<Vec<std::path::PathBuf>>>> {
        eprintln!("[fake] prompt_for_paths(multiple={})", options.multiple);
        let (mut tx, rx) = futures::channel::oneshot::channel();
        tx.send(Ok(None)).ok();   // 用户「取消」：Ok(None) 不是错误（u5-l5 语义）
        rx
    }
    fn prompt_for_new_path(&self, dir: &std::path::Path, _: Option<&str>)
        -> futures::channel::oneshot::Receiver<Result<Option<std::path::PathBuf>>> {
        eprintln!("[fake] prompt_for_new_path({})", dir.display());
        let (mut tx, rx) = futures::channel::oneshot::channel();
        tx.send(Ok(None)).ok();
        rx
    }
    fn can_select_mixed_files_and_dirs(&self) -> bool { false }
    fn reveal_path(&self, path: &std::path::Path) {
        eprintln!("[fake] reveal_path({})", path.display())
    }
    fn open_with_system(&self, path: &std::path::Path) {
        eprintln!("[fake] open_with_system({})", path.display())
    }
    fn on_quit(&self, _: Box<dyn FnMut()>) {}
    fn on_reopen(&self, _: Box<dyn FnMut()>) {}
    fn on_system_wake(&self, _: Box<dyn FnMut()>) {}
    fn set_menus(&self, _: Vec<Menu>, _: &Keymap) { eprintln!("[fake] set_menus()"); }
    fn set_dock_menu(&self, _: Vec<MenuItem>, _: &Keymap) {}
    fn on_app_menu_action(&self, _: Box<dyn FnMut(&dyn Action)>) {}
    fn on_will_open_app_menu(&self, _: Box<dyn FnMut()>) {}
    fn on_validate_app_menu_command(&self, _: Box<dyn FnMut(&dyn Action) -> bool>) {}
    fn thermal_state(&self) -> ThermalState { ThermalState::Nominal }
    fn on_thermal_state_change(&self, _: Box<dyn FnMut()>) {}
    fn app_path(&self) -> Result<std::path::PathBuf> {
        Ok(std::env::current_exe()?)
    }
    fn path_for_auxiliary_executable(&self, name: &str) -> Result<std::path::PathBuf> {
        anyhow::bail!("no auxiliary executable: {name}")
    }
    fn set_cursor_style(&self, style: CursorStyle) {
        eprintln!("[fake] set_cursor_style({style:?})")
    }
    fn hide_cursor_until_mouse_moves(&self) {}
    fn is_cursor_visible(&self) -> bool { true }
    fn should_auto_hide_scrollbars(&self) -> bool { false }
    fn read_from_clipboard(&self) -> Option<ClipboardItem> { None }
    fn write_to_clipboard(&self, item: ClipboardItem) {
        eprintln!("[fake] write_to_clipboard({} bytes)", item.text().len())
    }
    fn write_credentials(&self, url: &str, _: &str, _: &[u8]) -> Task<Result<()>> {
        eprintln!("[fake] write_credentials({url})");
        Task::ready(Ok(()))
    }
    fn read_credentials(&self, url: &str) -> Task<Result<Option<(String, Vec<u8>)>>> {
        eprintln!("[fake] read_credentials({url})");
        Task::ready(Ok(None))
    }
    fn delete_credentials(&self, url: &str) -> Task<Result<()>> {
        Task::ready(Ok(()))
    }
    fn keyboard_layout(&self) -> Box<dyn PlatformKeyboardLayout> {
        Box::new(FakeKeyboardLayout)
    }
    fn keyboard_mapper(&self) -> Rc<dyn PlatformKeyboardMapper> {
        Rc::new(DummyKeyboardMapper)
    }
    fn on_keyboard_layout_change(&self, _: Box<dyn FnMut()>) {}
    fn compositor_name(&self) -> &'static str { "Fake" }

    // Linux 目标上额外必需（见 4.1.3 cfg 门控组）：
    // fn read_from_primary / write_from_primary —— 照剪贴板两方法写日志版即可
}

struct FakeKeyboardLayout;
impl PlatformKeyboardLayout for FakeKeyboardLayout {
    fn id(&self) -> &str { "fake.keyboard" }
    fn name(&self) -> &str { "Fake Layout" }
}

struct FakeDisplay;
impl PlatformDisplay for FakeDisplay {
    fn id(&self) -> DisplayId { DisplayId(0) }        // 运行期句柄，随便给
    fn uuid(&self) -> Result<uuid::Uuid> { Ok(uuid::Uuid::new_v4()) }
    fn bounds(&self) -> Bounds<Pixels> {              // 一张 1920x1080 假屏（同 u2-l3 headless 惯例）
        Bounds {
            origin: point(px(0.), px(0.)),
            size: size(px(1920.), px(1080.)),
        }
    }
}
```

**第 3 步：FakeWindow**（示例代码，骨架；无默认体的方法全列，默认体方法一律不写）

```rust
use raw_window_handle::{HasDisplayHandle, HasWindowHandle, HandleError};

pub struct FakeWindow {
    bounds: Bounds<Pixels>,
    scale_factor: f32,
    title: String,
    draw_count: usize,
    atlas: Arc<FakeAtlas>,
    on_request_frame: Option<Box<dyn FnMut(RequestFrameOptions)>>,
    on_close: Option<Box<dyn FnOnce()>>,
    // 其余 on_* 回调同样存起来即可（on_input、on_resize……），假平台永不触发
}

impl FakeWindow {
    fn new(_handle: AnyWindowHandle, bounds: Bounds<Pixels>) -> Self {
        Self {
            bounds,
            scale_factor: 1.0,
            title: String::new(),
            draw_count: 0,
            atlas: Arc::new(FakeAtlas::default()),
            on_request_frame: None,
            on_close: None,
        }
    }
}

// 没有 GPU 窗口，两个 supertrait 都返回 NotSupported（照抄 TestWindow，见 4.3.3）
impl HasWindowHandle for FakeWindow {
    fn window_handle(&self) -> Result<raw_window_handle::WindowHandle<'_>, HandleError> {
        Err(HandleError::NotSupported)
    }
}
impl HasDisplayHandle for FakeWindow {
    fn display_handle(&self) -> Result<raw_window_handle::DisplayHandle<'_>, HandleError> {
        Err(HandleError::NotSupported)
    }
}

impl PlatformWindow for FakeWindow {
    fn bounds(&self) -> Bounds<Pixels> { self.bounds }
    fn is_maximized(&self) -> bool { false }
    fn window_bounds(&self) -> WindowBounds { WindowBounds::Windowed(self.bounds) }
    fn content_size(&self) -> Size<Pixels> { self.bounds.size }
    fn resize(&mut self, size: Size<Pixels>) { self.bounds.size = size; }
    fn scale_factor(&self) -> f32 { self.scale_factor }
    fn appearance(&self) -> WindowAppearance { WindowAppearance::Light }
    fn display(&self) -> Option<Rc<dyn PlatformDisplay>> { Some(Rc::new(FakeDisplay)) }
    fn mouse_position(&self) -> Point<Pixels> { point(px(0.), px(0.)) }
    fn modifiers(&self) -> Modifiers { Modifiers::default() }
    fn capslock(&self) -> Capslock { Capslock::Off }
    fn set_input_handler(&mut self, _: PlatformInputHandler) {}
    fn take_input_handler(&mut self) -> Option<PlatformInputHandler> { None }
    fn prompt(&self, _: PromptLevel, msg: &str, _: Option<&str>, _: &[PromptButton])
        -> Option<futures::channel::oneshot::Receiver<usize>> {
        eprintln!("[fake-window] prompt({msg})");
        None
    }
    fn activate(&self) { eprintln!("[fake-window] activate()"); }
    fn is_active(&self) -> bool { false }
    fn is_hovered(&self) -> bool { false }
    fn background_appearance(&self) -> WindowBackgroundAppearance {
        WindowBackgroundAppearance::Opaque
    }
    fn set_title(&mut self, title: &str) {
        eprintln!("[fake-window] set_title({title})");
        self.title = title.to_string();
    }
    fn set_background_appearance(&mut self, _: WindowBackgroundAppearance) {}
    fn minimize(&self) { eprintln!("[fake-window] minimize()"); }
    fn zoom(&self) {}
    fn toggle_fullscreen(&self) {}
    fn is_fullscreen(&self) -> bool { false }
    fn on_request_frame(&self, callback: Box<dyn FnMut(RequestFrameOptions)>) {
        // 真实平台在此注册帧回调；假平台存下永不触发 → 只有首帧（u3-l2 的 schedule_frame 语义）
        let this = self as *const FakeWindow as *mut FakeWindow;
        unsafe { (*this).on_request_frame = Some(callback); }
    }
    fn on_input(&self, _: Box<dyn FnMut(PlatformInput) -> DispatchEventResult>) {}
    fn on_active_status_change(&self, _: Box<dyn FnMut(bool)>) {}
    fn on_hover_status_change(&self, _: Box<dyn FnMut(bool)>) {}
    fn on_resize(&self, _: Box<dyn FnMut(Size<Pixels>, f32)>) {}
    fn on_moved(&self, _: Box<dyn FnMut()>) {}
    fn on_should_close(&self, _: Box<dyn FnMut() -> bool>) {}
    fn on_hit_test_window_control(&self, _: Box<dyn FnMut() -> Option<WindowControlArea>>) {}
    fn on_close(&self, callback: Box<dyn FnOnce()>) {
        let this = self as *const FakeWindow as *mut FakeWindow;
        unsafe { (*this).on_close = Some(callback); }
    }
    fn on_appearance_changed(&self, _: Box<dyn FnMut()>) {}
    fn draw(&self, scene: &Scene) {
        self.draw_count += 1;
        eprintln!(
            "[fake-window] draw() 第 {} 帧：{} 个图元",
            self.draw_count,
            scene.layers().map(|l| l.batches.len()).sum::<usize>()
        );
    }
    fn sprite_atlas(&self) -> Arc<dyn PlatformAtlas> { self.atlas.clone() }
    fn is_subpixel_rendering_supported(&self) -> bool { false }
    fn update_ime_position(&self, _: Bounds<Pixels>) {}
    fn gpu_specs(&self) -> Option<GpuSpecs> { None }
}
```

两处诚实的说明：① `on_request_frame` / `on_close` 的签名只给 `&self` 却要存回调，示例用了 `*const Self` 强转的捷径——生产代码应像 `TestWindow` 那样用 `Rc<Mutex<State>>` 内部可变性，这里为省篇幅（也更提醒你：这是示例代码，非项目源码）；② `scene.layers()` 的具体迭代 API 以你本地 gpui 版本为准，若方法名对不上，删掉图元统计只留计数即可（待确认）。

`FakeAtlas` 直接照抄 4.3.3 精读过的 `TestAtlas` 形状：`HashMap<AtlasKey, AtlasTile>` + `get_or_insert_with` / `remove` / `contains` 三方法，约 60 行，此处不重复。

**第 4 步：main 与验收**（示例代码）

```rust
fn main() {
    let platform = FakePlatform::new();
    Application::with_platform(platform).run(|cx: &mut App| {
        let bounds = Bounds::centered(None, size(px(600.), px(400.)), cx);
        let options = WindowOptions {
            window_bounds: Some(WindowBounds::Windowed(bounds)),
            titlebar: Some(TitlebarOptions {
                title: Some("Fake Platform".into()),
                ..Default::default()
            }),
            ..Default::default()
        };
        let window = cx.open_window(options, |_window, cx| {
            cx.new(|_| FakeView)
        }).expect("开窗失败");
        window.update(cx, |_, window, cx| window.activate_window(cx)).ok();

        // 2 秒后优雅退出：后台计时 → 回前台调 quit（u4-l1 的两层任务结构）
        let executor = cx.background_executor().clone();
        cx.spawn(async move |cx| {
            executor.timer(Duration::from_secs(2)).await;
            let _ = cx.update(|cx| cx.quit());
        })
        .detach();
    });
}

struct FakeView;
impl Render for FakeView {
    fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        div()
            .size_full()
            .flex()
            .items_center()
            .justify_center()
            .bg(rgb(0x2e7d32))
            .child("Hello from FakePlatform")
    }
}
```

**第 5 步：观察与验收清单**

预期日志序列（顺序可能有交错）：

```text
[fake] run() 进入事件循环
[fake] displays()
[fake] open_window(bounds=..., titlebar=Some("Fake Platform"))
[fake-window] set_title(Fake Platform)
[fake-window] draw() 第 1 帧：N 个图元
[fake] quit()
[fake] run() 退出，进程收尾
```

逐条验收：

1. `text_system` / `displays` / `open_window` / `set_cursor_style`（若视图含交互元素）/ `quit` ——**至少五个平台方法出现在日志里**，达标。
2. `set_title` 被调用证明 `WindowOptions.titlebar` 经 `Window::new` 翻译成 `WindowParams` 后真的传到了平台窗口（u3-l1 的降维搬运链）。
3. `draw()` 恰好一次——`App::open_window` 强制绘制首帧（u1-l2），此后 `on_request_frame` 永不触发、无重绘，印证 u3-l2 的 schedule_frame 语义：默认空实现即「平台自有帧驱动」，而假平台的「自有驱动」就是「不驱动」。
4. 2 秒后 `quit()` → run 循环退出，证明 `executor.timer`（真实定时器线程）→ 前台任务 → `platform.quit` → `run` 返回 的完整闭环。

**预期结果**：以上全部满足即毕业设计通过。整个示例未在本讲义写作环境中编译运行，标注为示例代码、待本地验证；若编译报缺方法，按 4.1.4 的编译器清单补齐即可（尤其 Linux 目标上的主选区两方法）。

### 移植工作量分布：从这次实践反推真实移植

做完毕业设计，你可以凭手感回答「把 GPUI 移植到一个新操作系统要多少工作量」：

| 层次 | 占比感受 | 说明 |
| --- | --- | --- |
| `Platform` 契约的「能跑」级实现 | **小**（本讲一天量级） | 执行器可借 `ThreadedDispatcher`，文本可借 `NoopTextSystem`，一半方法吃默认或给假值 |
| `PlatformWindow` 的「能交互」级实现 | **中** | 事件翻译（u3-l3）、焦点与激活语义（u3-l2）、按需帧驱动（u5-l4 的 FrameLoop）都是真功夫 |
| 渲染与文本的「能看」级实现 | **大** | `PlatformAtlas` + `PlatformTextSystem` 要对接真实 GPU 与字体引擎（u8-l1/u8-l2），Windows/Mac 各上千行 |
| 桌面协议集成 | **大且零碎** | 剪贴板/文件对话框/通知/凭据/输入法（u2-l4、u5-l3、u5-l5、u6-l3），每个都是独立小项目 |

也就是说：**让 gpui 在新平台上「不 panic 地跑起来」很便宜，让它「看起来是原生应用」才是成本所在**——这正是契约把执行器和 run 循环定为必需、把系统集成大量留给默认实现的深层原因。

## 6. 本讲小结

- 实现 `Platform` 的第一步是数实现面：69 个方法中 18 个有默认、4 个随 cfg 门控增减，最小实现量随编译目标在 47～49 之间浮动；默认实现分能力探测、优雅降级、通用回退三种姿态，直接吃默认是契约许可的行为。
- `ThreadedDispatcher`（`test-support` feature）是可直接复用的调度器：线程池 + 定时器线程 + 主队列，配 `BackgroundExecutor::new` / `ForegroundExecutor::new` 两行组装出完整执行器；`run_until_idle` 不等未到期定时器，外层 `run` 循环要自己掌握节奏。
- `TestPlatform` 是最好的最小实现参考答案，但它是 `pub(crate)`——公开的只有 `TestDispatcher` 等，想定制平台只能自己写，这也正是本讲的意义。
- `Application::with_platform` 是换平台的正门：构造时从平台抽取执行器/文本系统/键盘并断言主线程，`run` 把控制权连同启动回调一起移交平台；`run_embedded` 是宿主自持循环的变体。
- 毕业设计 FakePlatform + FakeWindow 证明：一个「执行器真实、窗口逻辑真实、系统调用全记日志」的平台一天可成；真实移植的成本大头在 PlatformWindow 交互语义与渲染/文本/桌面协议集成。
- 验收锚点：日志中至少五个平台方法、`set_title` 证明 WindowOptions 翻译链、恰好一帧 `draw` 证明首帧强制绘制与按需帧语义、2 秒定时器触发优雅退出。

## 7. 下一步学习建议

本手册到此完结。三个方向的延伸阅读：

1. **重读契约，带着实现者的眼睛**：再走一遍 [../gpui/src/platform.rs:125-341](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L125-L341)，这次每读一个方法就问「我的 FakePlatform 给了哪一档实现、真实平台给了哪一档」——同一份契约在五套实现（macOS/Windows/Linux 三后端/Web/Test）之间的分叉是最有价值的学习材料。
2. **把毕业设计推向真实**：给 `FakeWindow` 接一个无头渲染器（回到 u8-l3 的 `PlatformHeadlessRenderer` 与 `HeadlessAppContext::capture_screenshot`），让你的假平台能产出真实像素截图；或把 `NoopTextSystem` 换成 `CosmicTextSystem`，观察文本布局变化。
3. **对照一个新生平台的历史**：用 `git log --follow crates/gpui_linux/src/linux/wayland/window.rs` 回看 Wayland 后端的演进（尤其 eb354c8d50 的按需渲染循环重写），体会「能跑 → 能交互 → 能看」三阶段在一个真实后端上的时间分布——那就是你的下一个平台要走完的路。
