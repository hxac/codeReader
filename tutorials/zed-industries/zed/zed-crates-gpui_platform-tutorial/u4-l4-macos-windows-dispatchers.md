# macOS 与 Windows 的调度器：MacDispatcher 与 WindowsDispatcher

## 1. 本讲目标

学完本讲，你应该能够：

1. 说明 macOS 上 `MacDispatcher` 如何把调度完全「外包」给 Grand Central Dispatch（GCD）：主线程任务进 main queue、后台任务进全局队列、延迟任务用 `dispatch_after`，以及这个无状态单元结构体为什么能成立。
2. 说明 macOS 主线程 run loop（`NSApp.run()` / `CFRunLoopRun()`）与 GCD main queue 的关系：为什么往 main queue 提交任务就等于唤醒了主循环。
3. 说明 Windows 上 `WindowsDispatcher` 如何用「优先级队列 + `PostMessageW` 唤醒消息 + 消息专用窗口」把 Win32 消息循环改造成前台调度器，并理解 `wake_posted` 标志的唤醒合并与防丢失唤醒两个用途。
4. 总结 macOS、Windows、Linux（乃至 Web）四个平台调度器在唤醒机制上的共性：跨线程只投递一个极小的信号，任务本体放在共享队列里，主循环醒来后自行拉取。
5. 能独立画出/写出三平台调度器在「唤醒机制、线程安全、优先级支持、计时器实现」四个维度上的对比表，并为每一格附上源码行号。

## 2. 前置知识

### 2.1 契约回顾：PlatformDispatcher 是什么

在 u4-l2 中我们精读了这个契约：它是平台无关执行器（`ForegroundExecutor` / `BackgroundExecutor`）与平台事件循环之间的桥，被 `Arc` 共享、可从任意线程调用，因此要求 `Send + Sync`。契约共五个必需方法（`is_main_thread`、`dispatch`、`dispatch_on_main_thread`、`dispatch_after`、`spawn_realtime`）加若干默认方法（空闲投递、时钟、计时器分辨率、测试下转等）：

[../gpui/src/platform.rs:1029-L1069](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui/src/platform.rs#L1029-L1069) 定义 `PlatformDispatcher` trait 的完整签名。

任务本体是 `RunnableVariant`——一个携带 `RunnableMeta`（spawn 位置与时刻）的 `Runnable` 类型别名：

[../gpui/src/platform.rs:1012-1015](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui/src/platform.rs#L1012-L1015) 给出 `RunnableVariant` 的定义。

优先级 `Priority`（含 `RealtimeAudio`/`High`/`Medium`/`Low` 四档）定义在 scheduler crate 中，权重是 0/60/30/10：

[../scheduler/src/scheduler.rs:30-55](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../scheduler/src/scheduler.rs#L30-L55) 定义 `Priority` 枚举与 `weight()` 方法，注释说明权重用于「任务被选中的概率」。

u4-l3 里我们已经看过 Linux 的 `LinuxDispatcher`（自建 Worker 线程池 + Timer 线程 + calloop ping）。本讲把视野补齐到 macOS 与 Windows，最后做四方对比。

### 2.2 macOS 侧需要的基础概念：Grand Central Dispatch（GCD）

- **GCD** 是 Apple 操作系统内建的 C 语言并发库，核心抽象是「队列（dispatch queue）」：你把闭包提交给队列，GCD 负责挑线程、调优先级、唤醒与执行。
- **串行队列 vs 并发队列**：串行队列保证任务按提交顺序一个接一个执行；并发队列可以同时跑多个任务。
- **main queue**：一个特殊的串行队列，绑定主线程。它的任务只会在主线程执行，前提是主线程正在运行 run loop（比如 `NSApplication.run()`）或调用了 `dispatch_main()`。
- **全局队列（global queue）**：一组系统共享的并发队列，按 QoS 优先级分级（High/Default/Low 等），适合丢后台工作。
- **`dispatch_async_f` / `dispatch_after_f`**：GCD 的 C 接口。它们不接收 Rust 闭包，只接收「一个 `void*` 上下文指针 + 一个 C 函数指针」，到期后用上下文指针回调该函数。Rust 侧要把闭包「装箱成裸指针、再在回调里还原」——这是本讲会反复看到的 trampoline（蹦床）手法。

### 2.3 Windows 侧需要的基础概念

- **消息循环（message loop）**：Win32 GUI 程序的心脏是 `GetMessageW`/`DispatchMessageW` 循环。线程拥有一个消息队列，系统与其他线程通过 `PostMessageW` 往里投递消息；循环取出消息、翻译、分发给对应窗口的窗口过程（window procedure）。
- **`PostMessageW` vs `SendMessageW`**：`PostMessageW` 异步——把消息放进队列立即返回，投递线程不等待；`SendMessageW` 同步——阻塞等待目标窗口处理完。跨线程唤醒必须用前者。
- **消息专用窗口（message-only window）**：以 `HWND_MESSAGE` 为父创建的窗口。它不可见、不被枚举、不接收广播，但拥有完整的窗口过程，是「借窗口消息机制但不做 GUI」的经典手法。
- **`WM_USER + n`**：窗口类私有消息号区间，语义由窗口过程自定义。
- **Windows 线程池（Vista 线程池 API）**：`TrySubmitThreadpoolCallback` 把一个回调提交给系统管理的线程池；`CreateThreadpoolTimer`/`SetThreadpoolTimer` 创建一次性定时器，到期在线程池线程上执行回调。回调优先级用 `TP_CALLBACK_PRIORITY` 三档表达。
- **`FILETIME`**：100 纳秒为单位的时间结构；负值表示「相对当前的延迟」。
- **`timeBeginPeriod(1)`**：把 Windows 系统计时器分辨率调到 1ms，否则默认约 15.6ms，短延时任务会明显偏晚。

### 2.4 术语速查

| 术语 | 一句话解释 |
| --- | --- |
| trampoline | 把 Rust 闭包经由 `void*` 上下文投给 C API，再在 C 回调里还原执行的「蹦床」函数 |
| 唤醒合并 | 多次投递任务只发一次唤醒信号，避免信号洪水 |
| 丢失唤醒 | 「入队方看到信号已在途而不再发，消费方却已经放弃检查」的竞态窗口 |
| 加权抽签 | 不严格按优先级排序，而是按权重随机挑队列取任务，防止低优先级饿死 |

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [../gpui_macos/src/dispatcher.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/dispatcher.rs) | `MacDispatcher` 实现：全部逻辑转发 GCD；文件后半是音频线程的 mach 线程策略 |
| [../gpui_macos/src/platform.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/platform.rs) | `MacPlatform`：创建 dispatcher 与两个执行器；`run()` 启动 AppKit 事件循环 |
| [../gpui_windows/src/dispatcher.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/dispatcher.rs) | `WindowsDispatcher` 实现：优先级队列 + `PostMessageW` 唤醒 + 线程池后台执行 |
| [../gpui_windows/src/platform.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/platform.rs) | `WindowsPlatform`：创建消息专用窗口、消息循环 `run()`、唤醒消息路由与 `run_foreground_task` |
| [../gpui_windows/src/events.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/events.rs) | `WM_GPUI_*` 私有消息号常量表 |
| [../gpui_windows/src/wrapper.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/wrapper.rs) | `SafeHwnd` 等把非 Send 的 Win32 句柄安全跨线程携带的包装类型 |
| [../gpui/src/queue.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui/src/queue.rs) | gpui 自带的 `PriorityQueueSender/Receiver`：三条 VecDeque + 加权抽签，Windows 主线程队列与 Linux 后台队列共用 |
| [../gpui_linux/src/linux/dispatcher.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs) | `LinuxDispatcher`（u4-l3 已精读，本讲作对比参照） |
| [../gpui/src/platform_scheduler.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui/src/platform_scheduler.rs) | 生产版 `Scheduler` 适配层：展示 `dispatch_after` 的真实用法（延迟只负责「点火」） |

## 4. 核心概念与源码讲解

### 4.1 MacDispatcher：把调度完全外包给 Grand Central Dispatch

#### 4.1.1 概念说明

回忆 u4-l3 的 `LinuxDispatcher`：它自己建 Worker 线程池、自己建 Timer 线程、自己操心怎么唤醒 calloop 主循环——因为 Linux 没有一个现成的「系统级任务调度运行时」。

macOS 有。GCD 就是操作系统内建的调度器，而且 Apple 自己的 AppKit 事件循环就跑在 GCD 之上。于是 `MacDispatcher` 做出了一个在其他平台不可能的选择：**自己不保存任何状态**。它是一个单元结构体（unit struct），没有队列、没有线程、没有标志位：

- 后台任务 → 提交给 GCD 全局并发队列（按优先级选一条）；
- 主线程任务 → 提交给 GCD main queue；
- 延迟任务 → 用 `dispatch_after` 在指定时刻提交到高优先级全局队列；
- 主线程身份 → 问 AppKit（`NSThread.isMainThread`）；
- 实时音频线程 → 自己 `std::thread::spawn`，再用 mach 线程策略 API 提升到实时调度。

队列的存储、线程的伸缩、唤醒的合并、优先级的仲裁，全部是操作系统的职责。这是「门面」思想在调度器上的重现：能外包给运行时的，绝不自己写。

#### 4.1.2 核心流程

一个后台任务在 macOS 上的旅程：

```text
cx.background_spawn(future)
  └─ BackgroundExecutor（Arc<MacDispatcher> 共享）
       └─ dispatcher.dispatch(runnable, priority)          # dispatch 后台
            └─ 按优先级选 GCD 全局队列（High/Default/Low）
                 └─ exec_async_f(context 指针, trampoline)
                      └─ GCD 挑一个池化线程执行 trampoline(context)
                           └─ Runnable::from_raw(context) → runnable.run()
```

一个主线程任务（例如 `cx.spawn` 的唤醒）的旅程：

```text
dispatcher.dispatch_on_main_thread(runnable, priority)
  └─ DispatchQueue::main().exec_async_f(context, trampoline)   # priority 被忽略
       └─ main queue（串行、FIFO、只在主线程执行）
            └─ 主线程 run loop（NSApp.run / CFRunLoopRun）取块执行
```

注意两个关键点：

1. **主线程任务严格 FIFO**。main queue 是串行队列，`dispatch_on_main_thread` 的 `priority` 参数直接被丢弃（形参名写作 `_priority`）。这和 Windows/Linux 的「三条队列加权抽签」形成鲜明对比（见 4.3 节）。
2. **「提交即唤醒」**。调用方不需要像 Windows 那样额外 `PostMessageW`——把块放进 main queue 本身就会让阻塞中的主 run loop 醒来。唤醒合并（wake 合并）也由 GCD 内部完成。

延迟任务的流程略有玄机：

```text
dispatcher.dispatch_after(duration, runnable)
  └─ 计算 DispatchTime::NOW + duration
       └─ exec_after_f(when, 高优先级全局队列, context, trampoline)
            └─ 到期后 runnable 在【后台线程】上执行 trampoline
```

**延迟到期的 runnable 跑在后台线程，不是主线程**。这是初学者最容易想错的地方。为什么这样是正确的？因为 `dispatch_after` 在 GPUI 里只被 `Scheduler::timer` 用来「点火」——到期时 runnable 只做一件事：往一个 oneshot channel 里发完成信号：

[../gpui/src/platform_scheduler.rs:138-160](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui/src/platform_scheduler.rs#L138-L160) `PlatformScheduler::timer` 用 `dispatch_after` 调度一个只发送 oneshot 完成信号的 runnable。

信号唤醒的是正在 `await` 这个 timer 的 future，而这个 future 属于哪个执行器，continuation 就在哪里继续跑。也就是说：**延迟机制只负责「几点钟叫醒你」，不负责「醒来后在哪个房间干活」**。三个平台都遵循这一分工（Linux 的 TimerAfter 在 Timer 线程上点火、Windows 的线程池定时器在线程池线程上点火）。

#### 4.1.3 源码精读

**① 无状态的单元结构体**

[../gpui_macos/src/dispatcher.rs:23-29](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/dispatcher.rs#L23-L29) `MacDispatcher` 只是一个单元结构体，`new()` 返回 `Self`，没有任何字段。

它的 `Send + Sync` 因此自动成立（单元结构体无数据），满足契约要求。所有「本该有的状态」都活在 GCD 里。

**② 主线程身份：问 AppKit**

[../gpui_macos/src/dispatcher.rs:32-35](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/dispatcher.rs#L32-L35) `is_main_thread` 通过 objc 消息发送调用 `[NSThread isMainThread]`。

这与其他平台的「缓存构造线程的 `ThreadId` 再比较」思路不同：macOS 上「主线程」是 AppKit 的语义概念（第一个调用 AppKit 的线程），直接询问系统最权威。对照 Windows 版（4.2.3 ①）与 Linux 版：

[../gpui_linux/src/linux/dispatcher.rs:103-105](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L103-L105) Linux 在构造时缓存 `thread::current().id()`，之后比较 ThreadId。

**③ 后台投递：优先级映射到全局队列**

[../gpui_macos/src/dispatcher.rs:37-53](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/dispatcher.rs#L37-L53) `dispatch` 把 `Priority` 翻译成 GCD 全局队列优先级，然后 `exec_async_f` 提交。

三段式：`runnable.into_raw()` 把 Rust 任务变成裸指针当上下文 → 选一条全局队列 → 提交 `trampoline`。注意 `RealtimeAudio` 分支直接 `panic!`——三平台一致的契约约定：实时优先级必须走 `spawn_realtime`，绝不能进队列（对照 queue.rs 的 `push` 对该档位 `unreachable!`）。

这里没有「投递失败」路径：`exec_async_f` 是不返回错误的 C 函数，提交即受理。对比 Windows/Linux 在 send 失败时的 `mem::forget` 兜底（见 4.2.3 ④），macOS 根本不需要这段防御。

**④ 主线程投递：一行核心**

[../gpui_macos/src/dispatcher.rs:55-60](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/dispatcher.rs#L55-L60) `dispatch_on_main_thread` 把任务提交到 `DispatchQueue::main()`，`_priority` 被显式忽略。

main queue 是串行队列：任务按提交顺序执行，天然满足 GPUI「主线程任务按序执行」的要求（u4-l1 讲过 `cx.spawn` 的非 Send future 必须顺序 poll）。

**⑤ 延迟投递**

[../gpui_macos/src/dispatcher.rs:62-71](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/dispatcher.rs#L62-L71) `dispatch_after` 用 `DispatchTime::NOW.time(纳秒)` 计算到期时刻，把任务排到**高优先级全局队列**。

到期执行发生在后台线程，如 4.1.2 所析，点火信号经由 oneshot 传回 awaiter。

**⑥ trampoline：Rust 与 C 边界上的类型还原**

[../gpui_macos/src/dispatcher.rs:166-175](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/dispatcher.rs#L166-L175) `extern "C" fn trampoline` 从裸指针还原 `Runnable`，执行前后更新 profiler 计时。

这个 8 行函数是理解全部三个平台调度器的钥匙：C API 只认 `void*` + 函数指针，Rust 闭包的世界只能以裸指针的形式「过境」，落地后再还原。Windows 的 `run_work_callback`（4.2.3 ②）是同一手法的 Win32 变体。还要注意 `update_running_task`/`save_task_timing` 的 profiler 插桩在 macOS、Windows、Linux 三边的执行回调中对称出现——这是 u4-l6 将要展开的前台日志机制的数据来源。

**⑦ 实时音频线程：mach 三连策略**

[../gpui_macos/src/dispatcher.rs:73-78](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/dispatcher.rs#L73-L78) `spawn_realtime` 起一个专用 `std::thread`，先设置线程策略再跑闭包。

[../gpui_macos/src/dispatcher.rs:91-109](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/dispatcher.rs#L91-L109) 前两步 mach 策略：`THREAD_EXTENDED_POLICY`（`timeshare = 0`，即 Fixed 固定优先级、不参与分时衰减）与 `THREAD_PRECEDENCE_POLICY`（`importance = 63`，相对重要度）。

[../gpui_macos/src/dispatcher.rs:126-146](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/dispatcher.rs#L126-L146) 第三步 `THREAD_TIME_CONSTRAINT_POLICY`，按音频节奏计算硬实时约束：以 44.1kHz 下约 128 帧一个量化周期（\( T = 2.9\,\text{ms} \)），保证占用与最大允许占用分别为

\[ \text{computation} = 0.75\,T, \qquad \text{constraint} = 0.85\,T \]

再经 `mach_timebase_info` 把毫秒换算成 mach 绝对时间。整个参数表抄自 Chromium 的音频线程设置（源码注释附了链接 [../gpui_macos/src/dispatcher.rs:82](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/dispatcher.rs#L82)）。对照另外两平台的同位实现：Windows 只调 `SetThreadPriority(THREAD_PRIORITY_TIME_CRITICAL)`，Linux 用 `pthread_setschedparam(SCHED_FIFO, 65)`——同一契约（「给音频一条独享的实时线程」）在三套操作系统 API 上的投影。

**⑧ 平台侧的配合：dispatcher 的诞生与 run loop**

[../gpui_macos/src/platform.rs:196-221](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/platform.rs#L196-L221) `MacPlatform::new` 创建唯一的 `MacDispatcher`，用同一个 `Arc` 分别构造后台与前台执行器。

这正是 u4-l1 讲过的模型：平台只造一个 dispatcher，前后台执行器是同一份调度能力的两个视图。

[../gpui_macos/src/platform.rs:491-518](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/platform.rs#L491-L518) `run()`：headless 模式直接 `CFRunLoopRun()` 驱动主 run loop；正常模式走 `app.run()`（AppKit 事件循环）。

这里回答了本讲目标 2 的问题：**main queue 的任务由主线程的 run loop 负责取出执行**。`NSApp.run()` 与 `CFRunLoopRun()` 都是运行主线程 run loop 的方式，GCD 把 main queue 挂在主 run loop 上，因此「往 main queue 提交块」与「唤醒正在 `GetMessage`/`select` 阻塞的主循环」在 macOS 上是同一件事的两个说法。

[../gpui_macos/src/platform.rs:520-538](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/platform.rs#L520-L538) `quit()` 反过来用调度器解决重入问题：为避免在持有 App 状态借用时同步关窗导致双重借用，退出动作本身被 `DispatchQueue::main().exec_async_f` 投递到主队列延迟执行。

这是「调度器作为架构工具」的实例——不只是跑任务，还能把一段敏感操作挪出当前调用栈。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：亲手验证「main queue ↔ 主 run loop」的绑定关系，并完整跟踪一次主线程任务在 macOS 上的投递路径。

**操作步骤**：

1. 打开 [../gpui_macos/src/platform.rs:491-518](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/platform.rs#L491-L518)，确认两个分支分别用什么 API 运行主线程 run loop。
2. 打开 [../gpui/src/platform_scheduler.rs:118-123](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui/src/platform_scheduler.rs#L118-L123)，确认前台执行器的 `schedule_local` 最终调用 `dispatch_on_main_thread(runnable, Priority::default())`。
3. 填写下面这张「六站旅程表」（把每站的函数名、所在文件、执行线程填进去）：

| 站点 | 函数/API | 文件 | 执行线程 |
| --- | --- | --- | --- |
| 1. 用户代码 | `cx.spawn(...)`（见 [../gpui/src/app.rs:1939-1957](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui/src/app.rs#L1939-L1957) 中 `App::spawn` 的定义） | gpui/src/app.rs | 主线程 |
| 2. 前台执行器 | （待填） | （待填） | （待填） |
| 3. 调度适配层 | （待填） | （待填） | （待填） |
| 4. 平台调度器 | （待填） | （待填） | （待填） |
| 5. 操作系统 | （待填） | — | （待填） |
| 6. 落地执行 | （待填） | （待填） | （待填） |

4. （可选，需 macOS 真机）在 gpui 的 window 示例（`crates/gpui/examples/window.rs`）的按钮回调里加一行 `log::info!("thread: {:?}", std::thread::current().id());`，再在 `trampoline` 里加同样的日志，运行后比对两条日志的线程 id 是否一致。**待本地验证**。

**需要观察的现象**：第 5 站应该是 `DispatchQueue::main().exec_async_f`，而第 6 站的 `trampoline` 与用户回调的线程 id 相同且都是主线程。

**预期结果**：旅程表能完整闭合——任何一条从后台线程发起的主线程任务，最终都经由 main queue 在主线程上被 `trampoline` 还原执行；`priority` 参数在这条链路上被丢弃。

#### 4.1.5 小练习与答案

**练习 1**：`MacDispatcher` 是单元结构体、没有任何字段，为什么它还能满足 `PlatformDispatcher: Send + Sync` 的约束？

**参考答案**：`Send`/`Sync` 是针对类型所持数据的自动 trait。单元结构体不持有任何数据，因此这两个 trait 自动成立。真正需要线程安全的状态（队列、唤醒、线程池）全部由 GCD 在操作系统层维护，GCD 的 C API 本身就是线程安全的。这也是「外包」策略的最大红利：Rust 侧无可争用之物。

**练习 2**：如果调用方误用 `dispatch(runnable, Priority::RealtimeAudio)`，会发生什么？为什么设计成 panic 而不是降级处理？

**参考答案**：会命中 [dispatcher.rs:41-43](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/dispatcher.rs#L41-L43) 的 `panic!`。因为实时音频任务的正确执行方式是「独占一条实时调度线程」（`spawn_realtime`），放进任何共享队列都会破坏其时限保证；这是一个编程错误而非运行时故障，fail-fast 比静默降级更安全。Windows 与 Linux 的 `dispatch` 对该档位同样 panic，queue.rs 的 `push` 则是 `unreachable!`——契约在四个层次上一致设防。

**练习 3**：`dispatch_after` 到期后 runnable 在后台线程执行。结合 [../gpui/src/platform_scheduler.rs:138-160](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui/src/platform_scheduler.rs#L138-L160) 说明：一个在 `cx.spawn`（前台）里 `await` 的 `background_executor().timer(1s)`，1 秒后 continuation 在哪个线程继续？

**参考答案**：在主线程。到期时后台线程上的 runnable 只向 oneshot channel 发送 `()`；该 channel 的 `Receiver` 被 await 的 future 持有，而这个 future 属于前台执行器，唤醒信号使它在主线程上被重新 poll，continuation 因此在主线程继续。延迟机制只点火，不决定 continuation 的归属。

### 4.2 WindowsDispatcher 与消息循环：从 PostMessageW 到 run_foreground_task

#### 4.2.1 概念说明

Windows 没有 GCD 这样的「提交即唤醒」运行时，但它有一样别的东西：**每个 GUI 线程都自带一个线程消息队列**，而且这个队列天生就是「主线程事件循环」——`Platform::run` 里的 `GetMessageW` 循环。u2-l2 讲过 Windows 的 `run()` 在进循环前同步执行启动回调，然后阻塞在消息循环上。

`WindowsDispatcher` 的策略因此是：

- **前台任务的容器**：一条 gpui 自带的 `PriorityQueueReceiver` 优先级队列（三条 VecDeque + 加权抽签，和 Linux 后台队列共用同一实现）。
- **唤醒信号**：一次 `PostMessageW(WM_GPUI_TASK_DISPATCHED_ON_MAIN_THREAD)`，投给一个进程私有的消息专用窗口。
- **唤醒合并**：`wake_posted: AtomicBool`——信号「在途」期间的新任务不再重复发消息，一条消息代表「队列里可能有任意多任务，醒来看看」。
- **后台任务**：直接扔给 Windows 系统线程池（`TrySubmitThreadpoolCallback`），优先级映射到回调优先级三档。
- **延迟任务**：线程池定时器（`CreateThreadpoolTimer` + 负 FILETIME 相对延迟）。

对比 macOS 的「零状态」，`WindowsDispatcher` 必须自己持有：优先级队列的 sender、主线程 id、消息窗口句柄（`SafeHwnd`）、验证号、唤醒标志。它把 Win32 消息循环当成 calloop 一样的「宿主事件循环」来寄生——这与 LinuxDispatcher「不拥有主循环、只负责唤醒它」的定位完全同构（u4-l3 的核心结论在 Windows 上原样成立）。

还有一个 Windows 独有的防御细节：**验证号（validation number）**。`WM_USER + 3` 是窗口类私有消息号，任何拿到 HWND 的代码都可以伪造这条消息。每次 `PostMessageW` 都把一个进程启动时生成的随机数放进 `wparam`，消息处理方先核对再执行，防止进程内其他组件误发/伪造唤醒消息触发任务排空逻辑。

#### 4.2.2 核心流程

**主线程任务的完整旅程**：

```text
任意线程: dispatch_on_main_thread(runnable, priority)
  ├─ main_sender.send(priority, runnable)        # 进三条优先级队列之一
  └─ 若 wake_posted 从 false 翻转为 true:        # swap(true)，只有第一个线程成功
        PostMessageW(消息窗口, WM_USER+3, 验证号, 0)   # 异步唤醒，不携带任务

主线程消息循环: GetMessageW → DispatchMessageW
  └─ window_procedure → handle_msg
       └─ handle_gpui_events: 核对 wparam == validation_number
            └─ run_foreground_task():
                 loop {
                   10ms 预算内: try_pop → execute_runnable   # 加权抽签选队列
                   预算耗尽: 泵一条 paint + 排空 input 消息,
                             重新 Post 一条唤醒消息, 退出本轮
                   队列空: wake_posted = false 后再查一次队列  # 防丢失唤醒
                 }
```

**为什么要「先清标志、再查队列」**（流程最后一行，代码在 4.2.3 ⑥）：存在这样的交错——

```text
时刻 t1: 入队方 send 成功，看到 wake_posted == true，于是不发 PostMessage
时刻 t2: 消费方排空队列，wake_posted.store(false)，准备退出
```

若消费方在 t2 之后直接返回，t1 的任务就永远无人处理（标志已 false，但没人再触发消息）。补救：清 false 之后**再** `try_pop` 一次，若有任务就把标志重新置 true 并当场执行，循环继续。用「多查一次」封死窗口期。

**后台任务的旅程**：

```text
dispatch(runnable, priority)
  └─ dispatch_on_threadpool(TP_CALLBACK_PRIORITY_*, runnable)
       └─ TrySubmitThreadpoolCallback(run_work_callback, context 指针)
            └─ 系统线程池某线程执行 run_work_callback(context)
                 └─ RunnableVariant::from_raw → execute_runnable
```

#### 4.2.3 源码精读

**① 结构体：五个字段各司其职**

[../gpui_windows/src/dispatcher.rs:28-34](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/dispatcher.rs#L28-L34) `WindowsDispatcher` 的字段表：唤醒标志、主线程队列 sender、主线程 id、消息窗口句柄、验证号。

`platform_window_handle: SafeHwnd` 之所以需要包装类型，是因为原始 `HWND` 不是 `Send`/`Sync`：

[../gpui_windows/src/wrapper.rs:27-39](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/wrapper.rs#L27-L39) `SafeHwnd` 用 `unsafe impl Send/Sync` 声明「这个句柄值可以跨线程复制」，使其能存进 `Send + Sync` 的 dispatcher。

`is_main_thread` 用缓存的线程 id 比较：

[../gpui_windows/src/dispatcher.rs:102-104](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/dispatcher.rs#L102-L104) 比较当前线程 id 与构造时（主线程）缓存的 id。

**② 创建时机：藏在 WM_NCCREATE 里**

[../gpui_windows/src/platform.rs:133-139](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/platform.rs#L133-L139) `WindowsPlatform::new` 先建优先级队列对（sender/receiver）并用 `rand::random` 生成验证号。

[../gpui_windows/src/platform.rs:151-166](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/platform.rs#L151-L166) 随后 `CreateWindowExW` 以 `HWND_MESSAGE` 为父创建消息专用窗口，把一个 `PlatformWindowCreateContext`（内含 sender/receiver/验证号）通过 `lpCreateParams` 传进去。

[../gpui_windows/src/platform.rs:1482-1509](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/platform.rs#L1482-L1509) 窗口过程在收到 `WM_NCCREATE`（窗口创建期第一条消息）时从上下文取出 sender，就地 `WindowsDispatcher::new(main_sender, hwnd, 验证号)`。

这是一个精巧的鸡生蛋问题的解法：dispatcher 需要 HWND 才能发唤醒消息，而 HWND 只有 `CreateWindowExW` 跑到一半（`WM_NCCREATE` 回调时）才存在。于是创建动作被塞进窗口过程第一次被调用的瞬间，`CreateWindowExW` 返回后再从 context 里把成品取出来。

[../gpui_windows/src/platform.rs:179-180](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/platform.rs#L179-L180) 与 macOS 完全同型：一个 dispatcher 的 `Arc` 构造出后台、前台两个执行器。

**③ 唤醒消息的定义与路由**

[../gpui_windows/src/events.rs:23-30](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/events.rs#L23-L30) `WM_GPUI_*` 私有消息号表，唤醒消息是 `WM_USER + 3`。

[../gpui_windows/src/platform.rs:447-465](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/platform.rs#L447-L465) `run()` 的标准 Win32 消息循环：`GetMessageW` 阻塞取消息 → `translate_accelerator`（键盘加速器特判）→ `TranslateMessage`/`DispatchMessageW` 分发。循环退出后（收到 `WM_QUIT`）才触发 `on_quit` 回调。

[../gpui_windows/src/platform.rs:988-1009](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/platform.rs#L988-L1009) `handle_msg` 把五条 `WM_GPUI_*` 消息路由到 `handle_gpui_events`，其余交给 `DefWindowProcW` 默认处理。

[../gpui_windows/src/platform.rs:1011-1027](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/platform.rs#L1011-L1027) `handle_gpui_events` 首先核对 `wparam` 与验证号，不匹配则记日志并放行给默认处理；匹配则把唤醒消息转给 `run_foreground_task`。

**④ 主线程投递与唤醒合并**

[../gpui_windows/src/dispatcher.rs:118-145](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/dispatcher.rs#L118-L145) `dispatch_on_main_thread` 全文，三步：send 进队列 → `swap(true)` 抢唤醒权 → 抢到者 `PostMessageW`。

注意 `swap` 的语义：原子地写入 true 并返回**旧值**。并发场景下只有「旧值为 false」的那个线程会发消息；其他线程知道已有信号在途，直接返回。这就是唤醒合并——一百个并发投递最多产生一条窗口消息。

send 失败的分支值得逐字读：

[../gpui_windows/src/dispatcher.rs:133-143](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/dispatcher.rs#L133-L143) 发送失败（意味着 receiver 已 drop、应用正在关闭、且当前在后台线程）时用 `std::mem::forget` 故意泄漏 runnable。

原因：runnable 可能包着 `!Send` 的 future，它只在主线程被 poll，也只应在主线程被 drop；在错误线程 drop 是未定义行为。既然进程即将退出，泄漏是两害相权取其轻。Linux 侧的 `dispatch_on_main_thread` 有一段逐字等价的防御（[../gpui_linux/src/linux/dispatcher.rs:116-126](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L116-L126)），而 macOS 因为 `exec_async_f` 无失败路径而无需此段——三平台对「!Send future 的线程归属」的执念是共性的重要一面。

**⑤ 后台投递与延迟投递**

[../gpui_windows/src/dispatcher.rs:54-71](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/dispatcher.rs#L54-L71) `dispatch_on_threadpool` 构造带回调优先级的 `TP_CALLBACK_ENVIRON_V3`，把 runnable 裸指针交给 `TrySubmitThreadpoolCallback`。

代码注释解释了另一处「接受泄漏」：若线程池从未执行回调，配对的 `from_raw` 不会发生，runnable 泄漏——但 drop 它反而会取消任务并让 awaiter 下次 poll 时 panic，关闭场景下泄漏更安全。

[../gpui_windows/src/dispatcher.rs:106-116](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/dispatcher.rs#L106-L116) `dispatch` 把 `Priority` 映射到 `TP_CALLBACK_PRIORITY_{HIGH,NORMAL,LOW}` 三档，`RealtimeAudio` 照例 panic。

[../gpui_windows/src/dispatcher.rs:73-89](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/dispatcher.rs#L73-L89) `dispatch_on_threadpool_after` 创建一次性线程池定时器：负 FILETIME 表示相对延迟（100ns 单位），`SetThreadpoolTimer` 的周期参数为 0 即只触发一次。

[../gpui_windows/src/dispatcher.rs:175-191](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/dispatcher.rs#L175-L191) 两个 `extern "system"` 回调：工作回调还原并执行 runnable；定时器回调额外 `CloseThreadpoolTimer` 释放定时器对象。与 macOS 的 `trampoline` 完全同构，只是调用约定换成 Win32 的 `system`。

[../gpui_windows/src/dispatcher.rs:91-98](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/dispatcher.rs#L91-L98) `execute_runnable` 把 profiler 前后插桩抽出为内联函数，供主线程路径与两个线程池回调共用——再次呼应三平台对称的插桩。

**⑥ run_foreground_task：10ms 预算、消息泵与防丢失唤醒**

[../gpui_windows/src/platform.rs:1044-1109](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/platform.rs#L1044-L1109) 消息处理侧的完整实现，是全文件最值得反复读的 60 行。

- [L1045-1051](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/platform.rs#L1045-L1051)：`MAIN_TASK_TIMEOUT = 10`（毫秒）。一次唤醒最多连续执行 10ms 的 GPUI 任务。
- [L1087-1091](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/platform.rs#L1087-L1091)：内层循环反复 `try_pop` → `execute_runnable`，队列空则跳出。
- [L1051-1085](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/platform.rs#L1051-L1085)：预算耗尽分支。先用 `PeekMessageW` 主动泵消息——**必须先处理一条 paint 消息再排空 input 消息**：注释解释 Windows 倾向优先分发应用自定义消息，若不先画，队列里还有剩余任务时会在重新进入本函数与绘制之间「饿死」绘制；input 排空则保证窗口关闭、键盘等系统事件不被 10ms 循环无限推迟。最后给自己再 Post 一条唤醒消息（让主循环先处理其他消息后再回来继续干活），仅当 re-post 失败才清 `wake_posted`。
- [L1094-1105](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/platform.rs#L1094-L1105)：防丢失唤醒（4.2.2 分析过的时序）。`store(false)` 之后再 `try_pop` 一次，若又弹出任务则把标志重置为 true 并继续循环。

这段代码是「寄生在宿主事件循环上」策略的代价清单：macOS 上 GCD 替你处理的预算控制、消息公平性、唤醒时序，在 Windows 上全都要手写。

**⑦ 两个 Windows 独有覆盖：计时器分辨率与实时线程**

[../gpui_windows/src/dispatcher.rs:165-172](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/dispatcher.rs#L165-L172) 覆盖契约默认实现的 `increase_timer_resolution`：调 `timeBeginPeriod(1)` 把系统计时器分辨率提到 1ms，返回的 guard 被 drop 时 `timeEndPeriod(1)` 复原。

这是三平台中唯一的覆盖（契约默认是 no-op 的 `defer`，见 [../gpui/src/platform.rs:1054-1056](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui/src/platform.rs#L1054-L1056)）。谁需要它？音频子系统——延时抖动直接劣化音频输出。

[../gpui_windows/src/dispatcher.rs:151-163](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/dispatcher.rs#L151-L163) `spawn_realtime`：起专用线程并 `SetThreadPriority(THREAD_PRIORITY_TIME_CRITICAL)`。

与 macOS 的 mach 三连策略、Linux 的 `SCHED_FIFO` 65 并置，正好构成「同一需求、三套系统 API」的教科书样本。

**⑧ 队列本体：三条 VecDeque 与加权抽签**

[../gpui/src/queue.rs:12-31](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui/src/queue.rs#L12-L31) 队列状态：high/medium/low 三条 `VecDeque` 藏在 `parking_lot::Mutex` 后，配一个 `Condvar` 与收发方计数。

[../gpui/src/queue.rs:69-78](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui/src/queue.rs#L69-L78) `push` 按优先级入对应条，`RealtimeAudio` 到达此处即 `unreachable!`。

[../gpui/src/queue.rs:313-357](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui/src/queue.rs#L313-L357) 取任务的「加权抽签」：设 \(h, m, l \in \{0,1\}\) 表示三条队列是否非空，则先尝试抽 High 的概率为

\[ P(\text{High}) = \frac{60}{60h + 30m + 10l} \]

未中再按剩余权重依次尝试 Medium、Low。算法注释标注出自「loaded die / biased coin」经典构造。设计意图是**防饿死**：低优先级任务也有非零概率被选中，而不是严格排队。

`run_foreground_task` 用的 `try_pop`（[queue.rs:244-246](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui/src/queue.rs#L244-L246)）就是非阻塞版 `pop_inner(false)`，同样走抽签。**因此 Windows 主线程任务的执行顺序是概率的，不是 FIFO**——与 macOS main queue 的严格串行形成本讲最重要的对照点。Linux 的主线程队列（`PriorityQueueCalloopReceiver` 内嵌同一 receiver）同理。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：把 `wake_posted` 的完整生命周期画成状态机，并能向别人解释「先清标志、再查队列」这一顺序为何不可颠倒。

**操作步骤**：

1. 通读 [../gpui_windows/src/dispatcher.rs:118-145](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/dispatcher.rs#L118-L145)（生产者侧）与 [../gpui_windows/src/platform.rs:1044-1109](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/platform.rs#L1044-L1109)（消费者侧）。
2. 在纸上画两个状态：`wake_posted = false`（无信号在途）与 `wake_posted = true`（信号在途）。把下列六个事件标注为状态转移边，每条边写清执行线程：
   - a. send 成功且 `swap(true)` 返回 false → Post 一条消息；
   - b. send 成功且 `swap(true)` 返回 true → 不发消息；
   - c. `run_foreground_task` 超时分支 re-post 成功；
   - d. 超时分支 re-post 失败 → `store(false)`；
   - e. 正常路径 `store(false)`；
   - f. 正常路径随后 `try_pop` 又取到任务 → `store(true)`。
3. 构造一个交错时间线（t1 < t2 < t3 …），说明：若把事件 e 改成「`store(false)` 后直接 break、不再 `try_pop`」，哪种交错会让某个 runnable 永远不被执行。
4. （可选，需 Windows 真机）写一个独立小 crate 依赖 `windows` crate，仿照 platform.rs 创建 `HWND_MESSAGE` 窗口并在回调里给自己 `PostMessageW(WM_USER + 3)`，观察消息循环收到自发自收的消息。此段为**示例代码**思路，完整 API 用法参照 [platform.rs:1467-1474](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/platform.rs#L1467-L1474) 的窗口类注册。**待本地验证**。

**需要观察的现象**：状态机中从 `true` 回到 `false` 的边只有 d 与 e 两条，且 e 之后必须紧跟一次队列检查（边 f 的条件分支）才能闭环。

**预期结果**：交错时间线形如「t1: 后台线程 A send 任务 X，看到标志为 true 不发消息；t2: 主线程恰好刚 `store(false)` 并 break」——若没有边 f，X 将滞留队列且无任何未来消息会触发检查。加上边 f 后，t2 时刻主线程在清标志后重查队列，X 被当场执行（或标志被重新置 true 进入下一轮）。

#### 4.2.5 小练习与答案

**练习 1**：为什么唤醒消息要发到一个 `HWND_MESSAGE` 消息专用窗口，而不是发给某个真实的 GPUI 应用窗口，或者干脆用 `PostThreadMessageW` 直接投给线程？

**参考答案**：发给真实窗口会把调度逻辑与某个窗口的生死耦合——窗口销毁后消息无处投递；而且窗口过程要额外区分调度消息与 UI 消息。`HWND_MESSAGE` 窗口不可见、不被枚举、不接收广播，是「纯控制通道」，与 UI 生命周期解耦，还能复用 `window_procedure` 里现成的验证号校验与 `GWLP_USERDATA` 存取机制。`PostThreadMessageW` 虽然更直接，但线程消息没有窗口过程承接、也拿不到 `WM_NCCREATE` 时机传递创建上下文，且在消息泵风格代码（如 `PeekMessageW` 带 `PM_QS_*` 过滤）里线程消息与窗口消息的过滤行为不同，不利于 `run_foreground_task` 里按类别泵消息。（源码可佐证的部分：消息窗口创建见 [platform.rs:151-166](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/platform.rs#L151-L166)，`PM_QS_PAINT`/`PM_QS_INPUT` 分类别泵见 [platform.rs:1063-1073](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/platform.rs#L1063-L1073)；关于 `PostThreadMessageW` 的比较属于设计权衡讨论，供思考。）

**练习 2**：`run_foreground_task` 的预算是 10ms。假设主线程队列里积压了 500ms 的任务，同时用户正在拖动窗口（产生大量 input 消息），描述系统的行为。

**参考答案**：每 10ms 一轮：先 `PeekMessageW` 处理一条 paint 消息（保证界面持续重绘），然后排空当前全部 input 消息（保证拖动等交互不被饿死），再给自己 Post 一条唤醒消息后返回主消息循环；主循环处理完其他消息后再次进入 `run_foreground_task` 继续消化任务。净效果是任务执行被切成 10ms 的片，与系统消息交替进行——用吞吐换响应性（[platform.rs:1051-1085](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/platform.rs#L1051-L1085) 的注释原文称「drain system events first to stay responsive」）。

**练习 3**：对比 macOS：为什么 `MacDispatcher` 不需要 `wake_posted`、不需要 10ms 预算、也不需要 `mem::forget` 兜底？

**参考答案**：三个问题共用一个答案——GCD 把这些都做了。main queue 的提交内建唤醒与合并（无需 `wake_posted`）；GCD 与 AppKit run loop 的集成由系统仲裁任务与事件的处理节奏（无需手写预算切片）；`exec_async_f` 提交即受理、无失败返回（无需 forget 兜底）。`WindowsDispatcher` 的额外复杂度全部来自「宿主系统没有提供等价抽象」。

### 4.3 PlatformDispatcher 契约的四面镜子：横向对比与共性

#### 4.3.1 概念说明

至此我们见过了三份实现（Linux 在 u4-l3，macOS 与 Windows 在本讲），第四份（Web 的 `MainThreadMailbox`）将在 u4-l5 展开。把它们并排放在一起看，会发现一个反复出现的**六步模式**：

1. **寄生而非拥有**：调度器从不拥有主事件循环。宿主分别是 calloop（Linux）、AppKit run loop（macOS）、Win32 消息循环（Windows）、浏览器事件循环（Web）。
2. **容器与信号分离**：任务放进某个跨线程队列（gpui 的 PriorityQueue / GCD 队列 / 线程池），唤醒只投递一个不含任务本体的极小信号（ping / main queue 提交本身 / 窗口消息 / 浏览器任务调度原语）。
3. **唤醒即再投递**：u4-l2 的结论——信号只负责让主循环「再进来一次」，进来后自行拉取任务。
4. **!Send future 的主线程专属性**：主线程任务可能在错误线程被 drop 的所有路径（send 失败、线程池不执行回调）都被显式用 `forget` 封死，宁可泄漏也不冒 UB。
5. **RealtimeAudio 不入队**：四档优先级中的实时档在 `dispatch`/`push` 两层都被 panic/unreachable 拦下，唯一合法路径是 `spawn_realtime` 的专用线程。
6. **profiler 对称插桩**：每份实现的执行回调都包裹 `update_running_task`/`save_task_timing`，为 u4-l6 的前台工作日志供给数据。

#### 4.3.2 核心流程

用伪代码概括三平台主线程投递的最小同构骨架：

```text
# 生产者（任意线程）
dispatch_on_main_thread(runnable, priority):
    queue.push(priority, runnable)          # ① 容器
    if wake_flag.swap(true) == false:       # ② 合并
        post_wakeup_signal()                # ③ 信号（不含 runnable）

# 消费者（主线程事件循环的一个分支）
on_wakeup_signal():
    while budget_left() and (runnable = queue.pop_weighted()):   # ④ 抽签/FIFO
        profiling_around(|| runnable.run())
    wake_flag = false
    if queue.pop_weighted() is Some(r):     # ⑤ 防丢失唤醒
        wake_flag = true; run(r)
```

macOS 是这个骨架的退化特例：①②③ 被 GCD 的 `DispatchQueue::main().exec_async_f` 一个调用合并，④ 退化为严格 FIFO，⑤ 由系统保证不存在，⑤ 的预算切片也交给系统。平台越「厚」，调度器越薄。

#### 4.3.3 源码精读（对比表）

**主线程投递路径对比**：

| 平台 | 任务容器 | 唤醒信号 | 顺序语义 | 关键源码 |
| --- | --- | --- | --- | --- |
| macOS | GCD main queue | 提交即唤醒（内建合并） | 严格 FIFO，priority 忽略 | [macos/dispatcher.rs:55-60](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/dispatcher.rs#L55-L60) |
| Windows | gpui PriorityQueue（三条 VecDeque） | `PostMessageW(WM_USER+3)` + `wake_posted` 合并 | 三队列 60/30/10 加权抽签 | [windows/dispatcher.rs:118-145](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/dispatcher.rs#L118-L145)，抽签 [queue.rs:313-357](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui/src/queue.rs#L313-L357) |
| Linux | 同一 PriorityQueue（经 calloop 适配） | calloop `ping.ping()`（事件 fd 写一字节） | 同上加权抽签 | [linux/dispatcher.rs:113-127](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L113-L127)、[linux/dispatcher.rs:171-178](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L171-L178) |

**后台投递与优先级对比**：

| 平台 | 后端 | 优先级映射 | 关键源码 |
| --- | --- | --- | --- |
| macOS | GCD 全局并发队列（系统线程池） | High/Default/Low 三条全局队列 | [macos/dispatcher.rs:37-53](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/dispatcher.rs#L37-L53) |
| Windows | Vista 线程池 `TrySubmitThreadpoolCallback` | `TP_CALLBACK_PRIORITY` 三档 | [windows/dispatcher.rs:54-71](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/dispatcher.rs#L54-L71)、[L106-116](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/dispatcher.rs#L106-L116) |
| Linux | 自建 Worker 线程池（≥2 条，`available_parallelism`） | 共享 PriorityQueue 加权抽签 | [linux/dispatcher.rs:31-52](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L31-L52) |

**延迟任务实现对比**（注意三者的到期执行都在后台线程，只负责点火）：

| 平台 | 机制 | 关键源码 |
| --- | --- | --- |
| macOS | `DispatchTime::NOW + 纳秒` → `exec_after_f` 到 High 全局队列 | [macos/dispatcher.rs:62-71](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/dispatcher.rs#L62-L71) |
| Windows | `CreateThreadpoolTimer` + 负 FILETIME 相对延迟（100ns 单位） | [windows/dispatcher.rs:73-89](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/dispatcher.rs#L73-L89) |
| Linux | Timer 线程内 calloop `Timer::from_duration` 一次性源 | [linux/dispatcher.rs:54-88](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L54-L88) |

**实时音频与杂项对比**：

| 维度 | macOS | Windows | Linux |
| --- | --- | --- | --- |
| `spawn_realtime` | mach 三连策略（Fixed + importance 63 + 时间约束）[L81-164](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/dispatcher.rs#L81-L164) | `SetThreadPriority(TIME_CRITICAL)` [L151-163](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/dispatcher.rs#L151-L163) | `SCHED_FIFO` 优先级 65 [L138-158](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L138-L158) |
| `is_main_thread` | `NSThread.isMainThread` | 缓存 ThreadId 比较 | 缓存 ThreadId 比较 |
| `increase_timer_resolution` | 默认 no-op | **唯一覆盖**：`timeBeginPeriod(1)` | 默认 no-op |
| 投递失败兜底 | 无失败路径 | `mem::forget` | `mem::forget` |
| 自有线程 | 0 | 0（用系统线程池） | Worker 池 + Timer 线程 |

最后一条值得强调：**macOS 与 Windows 的调度器都不拥有任何线程**（后台工作分别交给 GCD 与系统线程池，实时音频线程按需临时创建），而 Linux 必须自己养池子。这是「操作系统抽象厚度」的直接映射。

#### 4.3.4 代码实践（本机可运行型）

**实践目标**：用 gpui 自带队列的真实单元测试，验证「加权抽签」与「优先 FIFO」两种顺序语义的差异——这正是 macOS（FIFO）与 Windows/Linux（抽签）主线程队列的分野。

**操作步骤**：

1. 阅读两个现成测试：[../gpui/src/queue.rs:406-421](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui/src/queue.rs#L406-L421) 的 `all_tasks_get_yielded`（断言三种优先级的任务最终全部出队）与 [../gpui/src/queue.rs:423-434](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui/src/queue.rs#L423-L434) 的 `new_high_prio_task_get_scheduled_quickly`（100 个 Low 任务积压时，新插入的 High 任务在两次 pop 内被取出）。
2. 在仓库根目录运行（Linux 上需先装好构建依赖，见 `script/linux`）：

```bash
cargo test -p gpui --lib queue::tests
```

3. 手工推演一遍 `pop_inner` 的抽签：往三条队列各压 1 个任务，按 [queue.rs:328-331](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui/src/queue.rs#L328-L331) 的权重公式计算第一次 pop 选中各队列的概率，与公式 \[ P(H) = 60/100,\ P(M|未中H) = 30/40,\ P(L) = 10 \] 比对。

**需要观察的现象**：测试通过；`new_high_prio_task_get_scheduled_quickly` 表明 High 任务「很快」但不「立刻」被调度（第一次 pop 仍可能取到 Low）。

**预期结果**：两条测试断言的行为即「加权抽签」语义的直接证据；对照 macOS main queue 的严格 FIFO，可得出结论：同一份 GPUI 应用在两个平台上主线程任务的执行顺序保证并不相同，但都满足契约要求（按序 poll 的语义由「单线程执行」而非「入队顺序」保证——`cx.spawn` 的多个任务即便乱序执行，每个任务内部的 poll 顺序仍然严格串行）。若本机无法编译 gpui，步骤 3 的手工推演可独立完成，测试运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：三平台的 `is_main_thread` 有两种实现路线（问系统 vs 比对缓存 id）。各举一个平台的理由。

**参考答案**：macOS 走「问系统」（[macos/dispatcher.rs:32-35](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_macos/src/dispatcher.rs#L32-L35)），因为「主线程」在 AppKit 里是运行时语义（第一个触碰 AppKit 的线程），`NSThread.isMainThread` 是权威答案，且单元结构体本来也不想存字段。Windows/Linux 走「比对缓存 id」（[windows/dispatcher.rs:102-104](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_windows/src/dispatcher.rs#L102-L104)、[linux/dispatcher.rs:103-105](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L103-L105)），因为这两家的主线程就是「跑事件循环的那个线程」——即构造 platform/dispatcher 的线程，缓存其 `ThreadId` 既便宜又足够准确。

**练习 2**：`dispatch_on_main_thread_when_idle`（空闲时投递）在三平台分别如何实现？

**参考答案**：都没实现——三平台均使用契约默认实现「降级为 `dispatch_on_main_thread(runnable, Priority::Low)`」（[../gpui/src/platform.rs:1035-1042](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui/src/platform.rs#L1035-L1042)）。整个仓库里唯一覆盖它的实现是 Web 的 `MainThreadMailbox`（借用浏览器空闲回调原语，u4-l5 详述）。这体现了契约「默认实现兜底、按需覆盖」的设计：桌面平台尚无可靠的空闲语义，统一降级比各自发明要安全。

**练习 3**：如果让你给 `WindowsDispatcher` 增加 `now()` 的覆盖（伪造时钟，供测试），你会动哪个结构？为什么现有三平台都不覆盖？

**参考答案**：需要在 `WindowsDispatcher` 里加一个可替换的时钟源字段（如 `Arc<dyn Clock>`），并让 `now()` 读它——这会打破「无状态可变性」的简单性。现有三平台都不覆盖 `now()`（默认 `Instant::now()`，见 [../gpui/src/platform.rs:1050-1052](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/../gpui/src/platform.rs#L1050-L1052)），因为可伪造时钟只对确定性测试有价值，而测试走的是 `TestDispatcher`（u8-l4 会展开），生产平台没有动机为此增加状态。

## 5. 综合实践

**任务**：产出一份《三平台调度器对比报告》，核心是下面这张四维对比表，**每一格都必须附源码文件与行号引用**（格式如 `gpui_windows/src/dispatcher.rs:118-145`）：

| 维度 | macOS | Windows | Linux |
| --- | --- | --- | --- |
| 唤醒机制（主线程） | （待填） | （待填） | （待填） |
| 线程安全（状态与同步原语） | （待填） | （待填） | （待填） |
| 优先级支持（后台 + 主线程两侧） | （待填） | （待填） | （待填） |
| 计时器实现（含 timer resolution） | （待填） | （待填） | （待填） |

**操作步骤**：

1. 先独立填表，不看本讲 4.3.3 的表格，只读三份源码：`gpui_macos/src/dispatcher.rs`、`gpui_windows/src/dispatcher.rs`（连同 `gpui_windows/src/platform.rs` 的 `run_foreground_task`）、`gpui_linux/src/linux/dispatcher.rs`。
2. 填完后与 4.3.3 对照，补齐遗漏维度（尤其：投递失败兜底、`spawn_realtime`、自有线程数——它们经常被漏掉）。
3. 为每一格写一句「为什么这个平台长成这样」的归因（提示：往「操作系统提供了什么抽象」上归因——GCD / 系统线程池 / 什么都得自己来）。
4. 用 15 行以内的伪代码写出三平台共享的「容器 + 信号 + 合并 + 防丢失唤醒」骨架（参照 4.3.2，但用自己的话）。
5. （可选）在 Linux 本机完成 4.3.4 的 `cargo test -p gpui --lib queue::tests`，把真实输出贴进报告附录。

**验收标准**：表内 12 格全部有行号引用且行号真实可达；归因部分至少命中「macOS 零状态 vs Windows/Linux 自持状态」这一根本差异；伪代码骨架包含防丢失唤醒步骤。

## 6. 本讲小结

- `MacDispatcher` 是一个无状态的单元结构体：后台任务进 GCD 全局队列、主线程任务进 main queue、延迟任务用 `dispatch_after`，队列/唤醒/合并/线程伸缩全部外包给操作系统；主线程任务因此是严格 FIFO，`priority` 被忽略。
- macOS 上「往 main queue 提交」与「唤醒主循环」是同一件事：main queue 挂在主线程 run loop 上，`NSApp.run()`（或 headless 的 `CFRunLoopRun()`）驱动它。
- `WindowsDispatcher` 采用「容器 + 信号」结构：任务进 gpui 的三条 VecDeque 优先级队列，唤醒靠向 `HWND_MESSAGE` 消息专用窗口 `PostMessageW(WM_USER+3)`，`wake_posted: AtomicBool` 同时承担唤醒合并与（消费侧「先清标志再查队列」带来的）防丢失唤醒。
- Windows 主线程任务按 60/30/10 加权抽签执行（与 Linux 同一队列实现），且 `run_foreground_task` 以 10ms 预算切片并主动泵 paint/input 消息，防止任务洪水饿死系统事件；验证号随机数防御伪造的唤醒消息。
- 延迟任务在三平台都只负责「点火」：到期的 runnable 在后台线程向 oneshot 发信号，continuation 在 awaiter 所属执行器上继续；Windows 是唯一覆盖 `increase_timer_resolution`（`timeBeginPeriod(1)`）的平台。
- 四平台共享六步模式：寄生宿主事件循环、容器与信号分离、唤醒即再投递、!Send future 主线程专属性（失败路径 `forget`）、RealtimeAudio 拒绝入队、profiler 对称插桩。

## 7. 下一步学习建议

下一讲 **u4-l5《WebDispatcher：浏览器主线程邮箱与 wasm 单线程世界》** 是调度系列的最后一站：在 wasm 没有原生线程的世界里，`MainThreadMailbox` 如何用浏览器任务调度原语模拟前台调度器、如何成为全仓库唯一覆盖 `dispatch_on_main_thread_when_idle` 的实现，以及 `single_threaded_web` 与多线程 web 后端的差别。建议带着本讲的六步共性框架去读，重点找出「哪一步在 wasm 上被迫变形」。之后 **u4-l6** 会从 profiler 视角重返调度链路，解释本讲反复出现的 `update_running_task`/`save_task_timing` 插桩到底喂给了谁。若想先横向扩展，可跳读 **u8-l4**（测试平台 `TestDispatcher`，看伪造时钟与确定性调度如何实现）。
