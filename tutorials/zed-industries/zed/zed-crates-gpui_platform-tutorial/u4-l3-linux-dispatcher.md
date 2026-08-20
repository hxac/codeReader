# LinuxDispatcher 与 calloop：用事件循环库实现调度器

## 1. 本讲目标

上一讲（u4-l2）我们读完了平台无关的调度契约 `PlatformDispatcher`：五个必需方法、`RunnableVariant` 信封、`Send + Sync` 约束，以及只在测试中使用的通用参考实现 `ThreadedDispatcher`。本讲进入 Linux 的真实实现 `LinuxDispatcher`，学完后你应当能够：

1. 说明 `LinuxDispatcher` 的独特架构决策——它**不拥有主事件循环**，主循环归 Wayland/X11/headless 客户端所有，调度器只持有一个能「唤醒」主循环的发送端。
2. 理解 calloop 的 `ping`（eventfd 唤醒）、`channel`、`timer`、`insert_idle` 与 `LoopSignal` 五个原语，以及 `PriorityQueueCalloopSender`/`PriorityQueueCalloopReceiver` 如何把一个纯同步的优先级队列「适配」成合法的 calloop 事件源。
3. 跟踪三条投递路径的完整旅程：后台 `dispatch` 落到 Worker 线程池、`dispatch_on_main_thread` 经 ping 唤醒主循环、`dispatch_after` 经专用 Timer 线程延迟执行。
4. 解释为什么 X11/Wayland 把 runnable 塞进 `insert_idle` 而 headless 直接执行——输入事件优先于普通任务的关键机制。
5. 独立写出一个仿照 `LinuxDispatcher` 结构的、带两个优先级的最小 calloop 调度器。

## 2. 前置知识

### 2.1 事件循环库 calloop 是什么

GUI 程序的主线程典型工作方式是「事件循环」（event loop）：睡觉等事件 → 醒来处理一批事件 → 再睡觉。直接操作 epoll/kqueue 太底层，**calloop** 是一个跨平台的事件循环库（Linux 上基于 epoll），Zed 在 gpui_linux 中大量使用它。本讲涉及五个原语：

| 原语 | 作用 | 在本讲中的用途 |
|---|---|---|
| `EventLoop<D>` | 持有 epoll 与全部事件源；`run()` 阻塞驱动循环 | 主循环（客户端持有）与 Timer 线程的私有循环 |
| `EventSource` trait | 可被 poll 的事件源；实现 `process_events`/`register` 等 | 把优先级队列适配成事件源 |
| `ping` | `make_ping()` 返回 `(Ping, PingSource)`；任意线程调 `Ping::ping()`（写 eventfd）即可唤醒循环 | 唤醒主循环去排空任务队列 |
| `channel` | calloop 自带通道，接收端是事件源，回调收到 `Event::Msg(item)` 或 `Event::Closed` | Timer 线程接收 `TimerAfter`；系统唤醒信号 |
| `timer` | 定时器源；回调返回 `TimeoutAction::Drop`（一次性）或 `Reschedule`（周期） | 延迟任务的到期执行 |

另外两个概念：

- **`insert_idle`**：往循环里插入一个「空闲回调」。calloop 的一次 `dispatch` 迭代先把就绪的事件源回调全部跑完，之后、睡觉之前，会执行空闲队列里的回调。X11/Wayland 用它让 runnable 排在输入事件之后。
- **`LoopSignal`**：`event_loop.get_signal()` 得到的停止信号，任意线程调 `signal.stop()`，循环在当前迭代结束后退出。它正是 Linux `Platform::quit` 的实现（承接 u2-l2）。

### 2.2 从上一讲带来的结论（不重复展开）

- `RunnableVariant` 是携带 `RunnableMeta`（spawn 位置与时刻，供 profiler 消费）的任务信封。
- `Priority` 来自 scheduler crate：`High`/`Medium`/`Low` 权重 60/30/10，接收端用**加权随机抽签**而非严格排序来防饿死；`RealtimeAudio` 永不入队（权重 0），走专用实时线程。
- 契约要求 `PlatformDispatcher: Send + Sync`，因为它被 `Arc` 同时共享给前台、后台执行器，且会被任意线程调用。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [../gpui_linux/src/linux/dispatcher.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs) | 本讲主角：`LinuxDispatcher`、`TimerAfter`、`PriorityQueueCalloopSender/Receiver` 及其单元测试 |
| [../gpui_linux/src/linux/platform.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/platform.rs) | `LinuxCommon::new`：调度器、执行器、主循环接收端的装配车间；`run`/`quit` |
| [../gpui_linux/src/linux/headless/client.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/headless/client.rs) | headless 后端：创建事件循环并直跑 runnable（对照组之一） |
| [../gpui_linux/src/linux/x11/client.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs) | X11 后端：`insert_idle` 包一层再执行（对照组之二） |
| [../gpui_linux/src/linux/wayland/client.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs) | Wayland 后端：与 X11 同型的 `insert_idle` 处理 |
| [../gpui/src/queue.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/queue.rs) | 平台无关的三队列优先级通道（Mutex + Condvar + 抽签） |
| [../scheduler/src/scheduler.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../scheduler/src/scheduler.rs) | `Priority` 枚举与权重定义 |
| [../gpui/src/platform_scheduler.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/platform_scheduler.rs) | 执行器侧调用点：`dispatch`/`dispatch_on_main_thread`/`dispatch_after` 的上游 |

## 4. 核心概念与源码讲解

### 4.1 LinuxDispatcher：一个不拥有主循环的调度器

#### 4.1.1 概念说明

回忆 u4-l2 的 `ThreadedDispatcher`：它自己拥有线程池和定时器线程，自成一体。`LinuxDispatcher` 走了另一条路——**主事件循环不属于它，而属于窗口系统客户端**（Wayland/X11/headless 三选一，见 u1-l4 的两层分发）。

为什么？因为 Linux 上 GPUI 的主线程必须同时伺候两类事件：窗口系统协议事件（X11 的 XCB 事件、Wayland 的 display 事件）和调度器投递的 runnable。与其让调度器和窗口系统各自跑一个循环再互相唤醒，不如**共用一个 calloop 事件循环**：客户端拥有循环，调度器只拿到一根「能唤醒这个循环的发送端引线」。

于是 `LinuxDispatcher` 自己创建的线程只有两组：后台 Worker 线程池和一条 Timer 线程；主线程的一切都发生在客户端的循环里。

#### 4.1.2 核心流程

构造期（发生在 `LinuxCommon::new`，主线程）：

```
LinuxCommon::new(signal)
  ├─ PriorityQueueCalloopReceiver::new()
  │    → (main_sender, main_receiver)     ← 配对：发送端给调度器，接收端给客户端循环
  ├─ LinuxDispatcher::new(main_sender)
  │    ├─ PriorityQueueReceiver::new() → 后台通道（Worker 线程池消费）
  │    ├─ spawn N = max(available_parallelism, 2) 个 "Worker-i" 线程
  │    └─ spawn 1 个 "Timer" 线程（内含一个私有 calloop 循环）
  ├─ BackgroundExecutor::new(Arc<LinuxDispatcher> 的克隆)
  └─ ForegroundExecutor::new(Arc<LinuxDispatcher>)
→ (common, main_receiver, wake_receiver)  ← 接收端交还给客户端注册进它自己的循环
```

#### 4.1.3 源码精读

先看结构体——四个字段对应四条通路：

- [../gpui_linux/src/linux/dispatcher.rs:L20-L26](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L20-L26) — `LinuxDispatcher`：`main_sender` 唤醒主循环执行前台任务；`timer_sender` 发延迟任务给 Timer 线程；`background_sender` 投后台任务给 Worker 池；`main_thread_id` 记录构造时的线程用于 `is_main_thread`。注意没有任何字段持有主事件循环——它真的不属于调度器。

- [../gpui_linux/src/linux/dispatcher.rs:L33-L34](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L33-L34) — 线程数取 `available_parallelism()` 且不低于 `MIN_THREADS = 2`（[L28](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L28)）。

- [../gpui_linux/src/linux/dispatcher.rs:L36-L52](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L36-L52) — Worker 线程池：每个线程命名 `Worker-{i}`，克隆一份后台通道接收端，进入**阻塞迭代** `for runnable in receiver.iter()`——队列空时线程在 Condvar 上睡眠，有任务时被唤醒，按抽签顺序取出执行。

- [../gpui_linux/src/linux/dispatcher.rs:L42-L48](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L42-L48) — Worker 执行体的固定三段式：先 `profiler::update_running_task` 登记、再 `runnable.run()`、最后 `profiler::save_task_timing()`。这套插桩贯穿 Linux 调度器的每条执行路径（细节留待 u4-l6）。

- [../gpui_linux/src/linux/dispatcher.rs:L92-L98](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L92-L98) — 构造收尾：`main_thread_id` 用 `thread::current().id()` 捕获。由于 `LinuxDispatcher::new` 只会被 `LinuxCommon::new` 在主线程调用，这个 id 就是主线程 id。

装配车间在平台侧：

- [../gpui_linux/src/linux/platform.rs:L139-L178](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L139-L178) — `LinuxCommon::new` 全景：L146 创建主通道配对；L156 构造 `LinuxDispatcher`（吃进 `main_sender`）；L158-L162 用**同一个** `Arc<LinuxDispatcher>` 分别构造后台、前台执行器——这正是 u4-l1 说过的「一个调度器实例喂两个执行器」。

- [../gpui_linux/src/linux/platform.rs:L126](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L126) — `LinuxCommon` 还存着 `signal: LoopSignal`。谁传进来的？客户端构造事件循环后用 `event_loop.get_signal()` 传入（见 4.4.3），于是 `Platform::quit` 只需 `signal.stop()`（[L280-L282](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L280-L282)）。

#### 4.1.4 代码实践：亲眼看到这些线程

1. **实践目标**：验证 `LinuxDispatcher` 创建的线程确实存在且命名符合源码。
2. **操作步骤**：
   - 在仓库根目录运行 gpui 自带的窗口示例（Linux 上默认启用 wayland+x11 feature）：
     ```bash
     cargo run -p gpui --example window
     ```
   - 另开终端：
     ```bash
     ps -T -p "$(pgrep -f 'example.*window' | head -1)" -o spid,comm
     ```
3. **需要观察的现象**：线程列表中应出现 `Worker-0`、`Worker-1`……（数量 = max(CPU 核数, 2)）和一个名为 `Timer` 的线程；此外还有主线程与 wgpu/渲染相关线程。
4. **预期结果**：线程名与 [dispatcher.rs:L40](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L40)（`Worker-{i}`）和 [L56](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L56)（`Timer`）一一对应。具体线程数取决于机器核数，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LinuxDispatcher` 不需要像 `ThreadedDispatcher` 那样自己创建主循环？
**答案**：Linux 上主线程的事件循环由窗口系统客户端（Wayland/X11/headless）创建并拥有，协议事件与调度任务共用一个循环更高效也更简单；调度器只通过 `main_sender`（队列 + ping）唤醒该循环，因此它自建的线程只剩后台 Worker 池与 Timer 线程。

**练习 2**：`is_main_thread` 是如何实现的？为什么不需要平台 API？
**答案**：构造时在主线程捕获 `thread::current().id()` 存入 `main_thread_id`，查询时比较当前线程 id（[dispatcher.rs:L103-L105](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L103-L105)）。因为构造必然发生在主线程（`LinuxCommon::new` 由客户端构造函数调用），纯 Rust 的线程 id 比较即可，无需 `gettid` 之类的系统调用。

### 4.2 PriorityQueueCalloopSender / Receiver：给优先级队列装上 calloop 唤醒

#### 4.2.1 概念说明

gpui 平台无关的 `PriorityQueueSender/Receiver`（定义在 gpui 主 crate 的 queue.rs）是三条 `VecDeque` + `Mutex` + `Condvar` 的同步通道，**没有任何文件描述符**——它无法直接注册进基于 epoll 的 calloop 循环。而主线程投递（`dispatch_on_main_thread`）恰恰可能来自任意后台线程，必须能「唤醒」阻塞中的主循环。

解法是一个经典的适配器模式：

- **发送端** `PriorityQueueCalloopSender<T>` = 原始队列发送端 + 一个 `Ping`。`send` 成功后立刻 `ping()`——写 eventfd，主循环立刻可读就绪。
- **接收端** `PriorityQueueCalloopReceiver<T>` = 原始队列接收端 + `PingSource`，并实现 calloop 的 `EventSource` trait：当 ping 就绪时，回调里用 `try_iter()` **非阻塞排空**队列，把每个元素以 `Event::Msg(item)` 转发给下游回调。

为什么下游事件类型选 `calloop::channel::Event<T>`？这样这个适配器对外看起来和 calloop 原生 channel 一模一样（`Msg`/`Closed`），客户端代码可以无差别地 `match`——三个客户端的回调写的都是 `calloop::channel::Event::Msg(runnable)`。

#### 4.2.2 核心流程

一次主线程投递的微观时序：

```
后台线程                              主线程（客户端的 calloop 循环）
────────                              ────────────────────────────
dispatch_on_main_thread(r, p)
  └─ PriorityQueueCalloopSender::send
      ├─ 队列锁内按 p 入队 (high/medium/low)
      └─ ping.ping()  ──写 eventfd──→  poll 返回就绪
                                      PriorityQueueCalloopReceiver::process_events
                                        ├─ PingSource::process_events（读掉 eventfd 计数）
                                        ├─ receiver.try_iter() 逐个抽出（抽签决定顺序）
                                        │    └─ callback(Event::Msg(r))
                                        │         └─ 客户端回调：insert_idle 或直接 run
                                        └─ 若本round取到过任务 → 再 ping 一次（防漏）
```

#### 4.2.3 源码精读

- [../gpui_linux/src/linux/dispatcher.rs:L161-L178](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L161-L178) — 发送端：`send` 先走底层队列，成功后 `ping()`；注释外的关键点是 ping 只在 `res.is_ok()` 时发——入队失败（对端已死）就不必唤醒。

- [../gpui_linux/src/linux/dispatcher.rs:L180-L184](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L180-L184) — 发送端 `Drop` 时也 ping 一次：让循环醒来发现「所有发送端已消失」，从而触发 `Event::Closed` 并移除事件源（与 calloop channel 的关闭语义对齐）。

- [../gpui_linux/src/linux/dispatcher.rs:L193-L206](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L193-L206) — 配对构造：`make_ping()` 拿到 `(Ping, PingSource)`，再建底层优先级通道；Ping 克隆给发送端，PingSource 留在接收端。

- [../gpui_linux/src/linux/dispatcher.rs:L228-L243](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L228-L243) — `EventSource` 实现的关联类型：`Event = calloop::channel::Event<T>`、`Error = ChannelError`（[L211-L226](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L211-L226) 是对 `PingError` 的薄包装）。

- [../gpui_linux/src/linux/dispatcher.rs:L246-L272](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L246-L272) — 核心回调：借 `PingSource::process_events` 拿到唤醒时机，在闭包里克隆接收端并 `try_iter()` 排空。`Ok(r)` 逐个上抛 `Event::Msg`；`Err(_)` 说明所有发送端已掉线，标记 `disconnected` 并在末尾补发一次 `Event::Closed`。

- [../gpui_linux/src/linux/dispatcher.rs:L274-L282](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L274-L282) — 三个出口的决策树：`disconnected` → `PostAction::Remove`（把自己从循环移除）；本轮一个任务都没取到（`clear_readiness`）→ 沿用 ping 源的动作；本轮取到过任务 → **手动再 ping 一次**并 `PostAction::Continue`。最后这个再唤醒是防御性的：eventfd 的计数会把多次 ping 合并成一次就绪，「只要本轮有产出就再约一轮」确保排空期间新入队的任务不会被唤醒合并吞掉。

- [../gpui_linux/src/linux/dispatcher.rs:L285-L303](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L285-L303) — `register/reregister/unregister` 原样委托给内部的 `PingSource`：注册进 epoll 的始终是那一个 eventfd，队列本身对 poll 不可见。

顺带看两眼底层队列，理解「优先级」在这里的真实含义：

- [../gpui/src/queue.rs:L69-L78](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/queue.rs#L69-L78) — 入队：按 `Priority` 挑三条 `VecDeque` 之一；`RealtimeAudio` 直接 `unreachable!`——呼应契约「实时任务永不入队」。
- [../gpui/src/queue.rs:L328-L351](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/queue.rs#L328-L351) — 出队抽签：权重来自 [../scheduler/src/scheduler.rs:L47-L55](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../scheduler/src/scheduler.rs#L47-L55)（High=60、Medium=30、Low=10）。也就是说**主线程通道里消息的处理顺序同样是抽签的**，High 只是概率更高，不保证绝对先于 Low。

#### 4.2.4 代码实践：跑通并魔改 calloop_works 测试

dispatcher.rs 自带一个不依赖任何窗口系统的单元测试，是理解适配器行为的最佳入口：

1. **实践目标**：验证 ping 唤醒、消息传递、Closed 通知三个行为。
2. **操作步骤**：
   ```bash
   cargo test -p gpui_linux calloop_works
   ```
   （gpui_linux 默认启用 wayland+x11 feature，需要系统装有 wayland/xkbcommon/xcb 开发库；缺库时先补依赖，或待本地验证。）
   然后在自己的克隆里把测试改成跨优先级版本（示例代码）：
   ```rust
   // 在 tests 模块内仿照 calloop_works 新增：
   #[test]
   fn calloop_two_priorities() {
       let mut event_loop = calloop::EventLoop::try_new().unwrap();
       let handle = event_loop.handle();
       let (tx, rx) = PriorityQueueCalloopReceiver::new();

       let received = std::rc::Rc::new(std::cell::RefCell::new(Vec::new()));
       let sink = received.clone();
       handle
           .insert_source(rx, move |evt, &mut (), _: &mut ()| {
               if let Event::Msg(p) = evt {
                   sink.borrow_mut().push(p);
               }
           })
           .unwrap();

       for i in 0..50 {
           tx.send(Priority::Low, format!("low-{i}")).unwrap();
           tx.send(Priority::High, format!("high-{i}")).unwrap();
       }
       for _ in 0..10 {
           event_loop.dispatch(Some(Duration::ZERO), &mut ()).unwrap();
       }
       assert_eq!(received.borrow().len(), 100); // 全部送达
   }
   ```
3. **需要观察的现象**：所有消息在若干次 `dispatch` 后全部送达；打印 `received` 时 High 与 Low **交错出现**（抽签所致），并非 50 条 High 全部在前。
4. **预期结果**：数量断言通过；顺序断言若写成「High 全在前」会失败——这正是加权抽签与严格优先队列的区别。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接把 `PriorityQueueReceiver` 注册进 calloop 循环？
**答案**：calloop 的事件源必须对应一个可 poll 的对象（Linux 上是 epoll 感知的 fd）。优先级队列只是 `Mutex` + `Condvar` 的内存结构，没有 fd，epoll 感知不到它的变化；所以用 ping 的 eventfd 作「门外铃」：入队后按铃，铃声（而不是队列本身）唤醒循环，醒来后再排空队列。

**练习 2**：接收端为什么在「本轮取到过任务」时要再 ping 一次自己？
**答案**：eventfd 唤醒会被合并——两次 `ping()` 若都发生在循环睡眠期间，poll 只就绪一次。排空过程中或紧接着的新入队虽然自己也会 ping，但为不依赖对底层唤醒合并语义的假设，「本轮有产出就再约一轮核查」以少量冗余唤醒换取不丢任务（[dispatcher.rs:L279-L282](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L279-L282)）。

**练习 3**：`Event::Closed` 什么时候触发？
**答案**：当所有 `PriorityQueueCalloopSender` 被 drop（其 `Drop` 会 ping 最后一次），接收端排空时从底层通道收到 `Err`，标记 disconnected、上抛一次 `Event::Closed`，并以 `PostAction::Remove` 把自己从循环移除（[dispatcher.rs:L180-L184](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L180-L184)、[L258-L260](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L258-L260)、[L274-L275](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L274-L275)）。

### 4.3 TimerAfter：专用计时器线程与一次性定时器

#### 4.3.1 概念说明

`dispatch_after(duration, runnable)` 是契约中的延迟投递方法，也是 GPUI 所有 `timer()` 的底层（`cx.background_executor().timer(d)`、测试里的时钟推进最终都汇到这里）。Linux 的实现没有用任何定时器轮（timer wheel）数据结构，而是「一个线程 + 一个私有 calloop 循环 + 每次延迟插入一个一次性 timer 源」：

- `TimerAfter` 是最朴素的信封：到期时长 + 要执行的 runnable。
- Timer 线程跑一个只干一件事的 `EventLoop<()>`：从 channel 收 `TimerAfter`，为每个信封插入 `calloop::timer::Timer::from_duration(duration)` 源，到期回调里执行 runnable 并返回 `TimeoutAction::Drop`（一次性，执行后自毁）。

一个容易忽略的事实：**到期后 runnable 在 Timer 线程上执行，不在主线程**。所以它只适合「触发」而不该做 UI 工作——实际链路中它通常只负责点亮一个 oneshot，把等待中的 future 重新唤醒回正确的执行器（见 4.3.3 最后一处引用）。

#### 4.3.2 核心流程

```
任意线程: dispatch_after(duration, runnable)
  └─ timer_sender.send(TimerAfter { duration, runnable })   ← calloop channel
       └─ Timer 线程的私有循环被唤醒
            └─ channel 回调: timer_handle.insert_source(
                  Timer::from_duration(duration),
                  |_,_,_| { 执行 runnable; TimeoutAction::Drop })
                 到期 → 回调执行 → 源自动移除
```

#### 4.3.3 源码精读

- [../gpui_linux/src/linux/dispatcher.rs:L15-L18](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L15-L18) — `TimerAfter` 信封定义。

- [../gpui_linux/src/linux/dispatcher.rs:L54-L88](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L54-L88) — Timer 线程全貌：L58-L59 建私有循环；L63-L84 把 `timer_channel` 注册进该循环，每收到一个 `TimerAfter` 就插入一个新的一次性 timer 源；L86 `event_loop.run(None, ...)` 无限阻塞驱动。注意 L62-L67 拿了两个相同的 handle（`handle` 收消息、`timer_handle` 插定时器），纯为闭包捕获方便。

- [../gpui_linux/src/linux/dispatcher.rs:L66-L79](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L66-L79) — 到期回调：runnable 被包在 `Option` 里，回调内 `take()` 保证**至多执行一次**；执行前后照例做 profiler 登记；返回 `TimeoutAction::Drop` 让源执行后自移除。

- [../gpui_linux/src/linux/dispatcher.rs:L129-L136](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L129-L136) — `dispatch_after` 的错误处理：发送失败说明 Timer 线程已关停（应用退出中），此时对返回的 `TimerAfter` 调 `std::mem::forget`。注释讲明了理由——drop 一个已调度的 runnable 等于取消其任务，会让任何等待者的下一次 poll 直接 panic；泄漏则让任务永远挂起，在关机阶段可接受。

上游是谁？执行器的 `timer` 一路接到这里：

- [../gpui/src/executor.rs:L187-L192](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/executor.rs#L187-L192) — `BackgroundExecutor::timer(duration)` 就是 `spawn(scheduler().timer(duration))`：把「等闹钟」包装成一个后台任务。
- [../gpui/src/platform_scheduler.rs:L138-L156](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/platform_scheduler.rs#L138-L156) — `timer` 的真身：用 `async_task` 造一个 runnable，其 future 只做 `tx.send(())`（点亮 oneshot），而它的 **schedule 函数**是 `dispatcher.dispatch_after(duration, runnable)`。于是到期的瞬间，Timer 线程执行 runnable → oneshot 被点亮 → 等待这个 oneshot 的 future（通常在前台执行器上）被 waker 重新调度回主线程。延迟与唤醒的职责就这样切开。

#### 4.3.4 代码实践：跟踪一条 timer 调用链（源码阅读型）

1. **实践目标**：把 `timer()` 到 `TimerAfter` 的静态调用链走一遍，填出路径表。
2. **操作步骤**：从 [executor.rs:L187](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/executor.rs#L187) 出发，依次打开 [platform_scheduler.rs:L138-L156](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/platform_scheduler.rs#L138-L156) 与 [dispatcher.rs:L129-L136](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L129-L136)，记录每一步的函数与线程。
3. **需要观察的现象**（可选实验，示例代码）：
   ```rust
   // 在任意 GPUI 示例的 run 回调里：
   let start = std::time::Instant::now();
   cx.background_spawn(async move {
       cx.background_executor().timer(std::time::Duration::from_millis(300)).await;
       log::info!("timer fired after {:?}", start.elapsed());
   }).detach();
   ```
4. **预期结果**：路径表为 `timer() → PlatformScheduler::timer（造 oneshot + async_task）→ dispatch_after → timer_sender.send(TimerAfter) → Timer 线程插入一次性 timer 源 → 到期在 Timer 线程执行 runnable → tx.send(()) → oneshot 唤醒后台 future`；日志显示约 300ms（毫秒级精度受调度影响，待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么每个 `dispatch_after` 都新插一个 timer 源，而不是用一个统一的时间轮？
**答案**：这是一种用吞吐换简单的取舍——calloop 的 timer 源自带到期管理，每次插入/自毁的成本可接受，换来的是零维护的 correctness（`TimeoutAction::Drop` 即一次性语义）；对编辑器场景的延迟任务量而言足够。统一时间轮（如 `ThreadedDispatcher` 的做法，见 u4-l2）是另一种工程选择。

**练习 2**：`TimerAfter` 到期时 runnable 在哪个线程执行？这带来什么约束？
**答案**：Timer 线程（[dispatcher.rs:L70-L78](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L70-L78)）。因此 runnable 必须是 `Send`（契约本身就要求），且不应在回调里做任何主线程限定的工作——真实链路中它只点亮 oneshot，由 waker 把后续工作带回正确的执行器。

### 4.4 一次投递的完整路径：三条通路与输入优先级

#### 4.4.1 概念说明

现在把三个契约方法接上各自的终点，并回答本讲的最后一个问题：**为什么主循环里输入事件比普通任务「优先」？**

Wayland/X11 客户端把协议事件源和 `main_receiver` 注册在**同一个**循环里。若 runnable 在事件源回调里立即执行，一轮 dispatch 中 runnable 会与输入事件按注册/就绪顺序竞争；而 `insert_idle` 把 runnable 挪到「本轮所有事件回调跑完之后、睡觉之前」的空闲时段——用户输入（按键、鼠标）永远先于任务被处理，剩余时间才用来消化任务队列。headless 没有用户输入，也就不需要这层缓冲，直接执行。

#### 4.4.2 核心流程

三条通路总览：

```
① 后台：dispatch(runnable, priority)
     └→ background_sender.send → 三队列之一 + condvar.notify_one
     └→ 某 Worker-N 线程从 iter() 醒来 → 抽签取任务 → profiler 登记 → run()

② 主线程：dispatch_on_main_thread(runnable, priority)
     └→ main_sender.send（入队 + ping）
     └→ 主循环 poll 就绪 → PriorityQueueCalloopReceiver::process_events
     └→ Event::Msg(runnable)
          ├─ Wayland/X11: handle.insert_idle(run + profiler)   ← 让输入先行
          └─ headless:  直接 runnable.run()

③ 延迟：dispatch_after(duration, runnable)  → 见 4.3

④ 实时：spawn_realtime(f)  → 新 OS 线程 + SCHED_FIFO(65) → f()
```

#### 4.4.3 源码精读

- [../gpui_linux/src/linux/dispatcher.rs:L107-L111](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L107-L111) — 通路①：后台投递直接进优先级通道；失败即 panic（fail-fast：Worker 池在正常运行期不可能先于发送端消失，出现即初始化顺序 bug）。

- [../gpui_linux/src/linux/dispatcher.rs:L113-L127](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L113-L127) — 通路②：投主线程。失败分支的注释是全文件最值得咀嚼的一段——runnable 可能包着 `!Send` 的 future（u4-l1 讲过：前台任务允许非 Send），平时只会在主线程 poll/drop；若发送失败说明主循环接收端已死且当前在后台线程，**在错误线程 drop 一个 `!Send` 值是未定义行为**，所以必须 `mem::forget`（泄漏它），反正进程即将退出。这正呼应 u4-l2 说的「!Send future 仅在主线程执行与销毁」。

- [../gpui_linux/src/linux/dispatcher.rs:L138-L158](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L138-L158) — 通路④：实时任务不复用线程池，直接 `std::thread::spawn` 并用 `pthread_setschedparam` 设 `SCHED_FIFO`、优先级 65；失败仅告警降级为普通线程。这就是契约里「RealtimeAudio 永不入队」在 Linux 侧的兑现。

接收端注册（同一模式的三个变体）：

- [../gpui_linux/src/linux/headless/client.rs:L26-L38](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/headless/client.rs#L26-L38) — headless：先建 `EventLoop`，把 `get_signal()` 交给 `LinuxCommon::new`，再把 `main_receiver` 注册进**自己的**循环；回调里 `runnable.run()` 直跑，无 idle 缓冲。

- [../gpui_linux/src/linux/x11/client.rs:L316-L333](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L316-L333) — X11：同样的注册，但回调里是 `handle.insert_idle(...)`。[L321-L323](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L321-L323) 的注释就是本节论点的原始出处：把 runnable 排成 idle 回调，确保用户输入与 X11 事件更高优先、任务在事件回调之后处理。

- [../gpui_linux/src/linux/wayland/client.rs:L746-L761](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L746-L761) — Wayland：与 X11 逐行同型（insert_idle + profiler 登记），佐证这是 Linux 有头后端的统一模式。

- [../gpui_linux/src/linux/headless/client.rs:L133-L142](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/headless/client.rs#L133-L142) — 主循环本体在客户端的 `run` 里：`event_loop.run(None, ...)` 永久阻塞。配合 [platform.rs:L267-L278](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L267-L278)（`Platform::run` 先同步执行启动回调再进循环）与 [L280-L282](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L280-L282)（`quit` = `signal.stop()`），u2-l2 讲过的生命周期在这里全部落到 calloop 的 `LoopSignal` 上。

顺带留意 [platform.rs:L147](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L147)：`LinuxCommon::new` 还创建了第二条 calloop channel（`wake_receiver`），专供 login1 的 PrepareForSleep 系统唤醒信号（u2-l2 讲过）接入同一个主循环。「多个事件源共享一个循环」是贯穿 gpui_linux 的组织原则。

#### 4.4.4 代码实践：三个后端对照阅读（源码阅读型）

1. **实践目标**：说清同一份 `main_receiver` 在三个后端的注册差异及原因。
2. **操作步骤**：并排打开 4.4.3 列出的三处 `insert_source`，逐行比较回调体。
3. **需要观察的现象**：headless 是两行直跑；X11/Wayland 多了 `handle.insert_idle` 与 profiler 三段式。
4. **预期结果**：能回答「为什么 headless 不需要 insert_idle」——它没有窗口系统事件源，循环里除了 wake channel 外没有需要优先保障的输入，idle 缓冲没有意义；而 X11/Wayland 的循环里同时挂着协议事件，必须让位。

#### 4.4.5 小练习与答案

**练习 1**：`dispatch` 失败时 panic，`dispatch_on_main_thread` 失败时 forget，两种策略为何不同？
**答案**：后台通道的失败只可能源于 Worker 接收端先死，属于初始化顺序 bug，fail-fast 暴露问题更合理；主线程通道的失败发生在关机期，且 runnable 可能含 `!Send` future，在后台线程 drop 有 UB 风险，forget（泄漏）是唯一安全选择——进程将退，泄漏无妨（[dispatcher.rs:L107-L127](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/dispatcher.rs#L107-L127)）。

**练习 2**：X11 后端把 runnable 包进 `insert_idle`，这对「一轮 dispatch」内的执行顺序意味着什么？
**答案**：calloop 每轮先执行所有就绪事件源的回调（X11 输入、ping 排空等），再执行空闲回调。包进 idle 后，即使任务与按键事件在同一轮就绪，按键的处理也先于任务，任务只在当轮剩余时间里消化——用吞吐换取输入响应的确定性。

**练习 3**：`Platform::quit` 如何让阻塞在 `event_loop.run` 的主线程返回？
**答案**：客户端构造循环时把 `event_loop.get_signal()` 传入 `LinuxCommon`，`quit` 调 `signal.stop()`（[platform.rs:L280-L282](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L280-L282)），calloop 在当前迭代结束后退出 `run`，随后 `Platform::run` 触发 on_quit 回调（[L267-L278](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L267-L278)）。

## 5. 综合实践

**任务：写一个仿照 LinuxDispatcher 结构的最小 calloop 调度器（独立 crate）**。要求复刻本讲的四个骨架件：ping 适配的优先级通道、Worker 线程池、`insert_idle` 执行、延迟投递，让你在没有 gpui 的情况下体会这套结构。

在仓库外新建 `mini-dispatcher`（workspace 里 calloop 是 git fork 依赖，见 [Cargo.toml:L971](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/Cargo.toml#L971)，独立 crate 直接用 crates.io 版本即可；本讲用到的 API 两边一致，具体版本兼容性待本地验证）：

```toml
# Cargo.toml
[package]
name = "mini-dispatcher"
version = "0.1.0"
edition = "2021"

[dependencies]
calloop = "0.13"
```

```rust
// src/main.rs —— 示例代码：仿 LinuxDispatcher 的最小调度器
use std::{
    collections::VecDeque,
    sync::{Arc, Mutex},
    thread,
    time::Duration,
};

use calloop::{channel, timer::TimeoutAction, EventLoop, PostAction};

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Priority {
    High,
    Low,
}

// ① 朴素的严格优先级队列（gpui 用 60/30/10 加权抽签防饿死，这里从简，见练习）
struct SharedQueue {
    queues: Mutex<(VecDeque<String>, VecDeque<String>)>, // (high, low)
}

impl SharedQueue {
    fn push(&self, priority: Priority, job: String) {
        let mut queues = self.queues.lock().unwrap();
        match priority {
            Priority::High => queues.0.push_back(job),
            Priority::Low => queues.1.push_back(job),
        }
    }

    fn drain(&self) -> Vec<String> {
        let mut queues = self.queues.lock().unwrap();
        let mut jobs = queues.0.drain(..).collect::<Vec<_>>();
        jobs.extend(queues.1.drain(..));
        jobs
    }
}

// ② PriorityQueueCalloopReceiver 的迷你版：ping 事件源 + 排空 + 再唤醒
struct PingQueueSource {
    queue: Arc<SharedQueue>,
    ping: calloop::ping::Ping,
    source: calloop::ping::PingSource,
}

impl calloop::EventSource for PingQueueSource {
    type Event = channel::Event<String>;
    type Metadata = ();
    type Ret = ();
    type Error = calloop::Error;

    fn process_events<F>(
        &mut self,
        readiness: calloop::Readiness,
        token: calloop::Token,
        mut callback: F,
    ) -> Result<PostAction, Self::Error>
    where
        F: FnMut(Self::Event, &mut Self::Metadata) -> Self::Ret,
    {
        let mut got_any = false;
        let action = self
            .source
            .process_events(readiness, token, |(), &mut ()| {
                for job in self.queue.drain() {
                    callback(channel::Event::Msg(job), &mut ());
                    got_any = true;
                }
            })
            .map_err(|e| calloop::Error::Other(Arc::new(e)))?;

        if got_any {
            // 本轮有产出：再 ping 一轮，防止唤醒合并吞掉排空期间的新任务
            self.ping.ping();
            Ok(PostAction::Continue)
        } else {
            Ok(action)
        }
    }

    fn register(
        &mut self,
        poll: &mut calloop::Poll,
        token_factory: &mut calloop::TokenFactory,
    ) -> calloop::Result<()> {
        self.source.register(poll, token_factory)
    }

    fn reregister(
        &mut self,
        poll: &mut calloop::Poll,
        token_factory: &mut calloop::TokenFactory,
    ) -> calloop::Result<()> {
        self.source.reregister(poll, token_factory)
    }

    fn unregister(&mut self, poll: &mut calloop::Poll) -> calloop::Result<()> {
        self.source.unregister(poll)
    }
}

// ③ PriorityQueueCalloopSender 的迷你版：入队 + ping
#[derive(Clone)]
struct PingQueueSender {
    queue: Arc<SharedQueue>,
    ping: calloop::ping::Ping,
}

impl PingQueueSender {
    fn send(&self, priority: Priority, job: String) {
        self.queue.push(priority, job);
        self.ping.ping();
    }
}

fn main() -> anyhow::Result<()> {
    let mut event_loop: EventLoop<()> = EventLoop::try_new()?;
    let handle = event_loop.handle();
    let (ping, source) = calloop::ping::make_ping()?;
    let queue = Arc::new(SharedQueue {
        queues: Mutex::new((VecDeque::new(), VecDeque::new())),
    });

    let receiver = PingQueueSource {
        queue: queue.clone(),
        ping: ping.clone(),
        source,
    };
    let sender = PingQueueSender {
        queue: queue.clone(),
        ping,
    };

    // ④ 主循环注册接收端：任务经 insert_idle 执行（输入优先，仿 X11/Wayland）
    let main_sender = sender.clone();
    let idle_handle = handle.clone();
    handle.insert_source(receiver, move |event, _, ()| {
        if let channel::Event::Msg(job) = event {
            idle_handle.insert_idle(move |()| {
                println!("[main {:?}] {job}", thread::current().id());
            });
        }
    })?;

    // ⑤ TimerAfter 的迷你版：延迟消息经 channel 进主循环的一次性 timer
    let (timer_sender, timer_channel) = channel::channel::<(Duration, String)>();
    let timer_handle = handle.clone();
    handle.insert_source(timer_channel, move |event, _, ()| {
        if let channel::Event::Msg((duration, job)) = event {
            let mut job = Some(job);
            timer_handle
                .insert_source(
                    calloop::timer::Timer::from_duration(duration),
                    move |(), _, ()| {
                        if let Some(job) = job.take() {
                            println!("[timer-fired] {job}");
                        }
                        TimeoutAction::Drop
                    },
                )
                .ok();
        }
    })?;

    // ⑥ 后台 Worker 线程池：从其他线程向主循环投递两个优先级的任务
    let worker = thread::spawn(move || {
        for i in 0..5 {
            main_sender.send(Priority::Low, format!("low-{i}"));
            main_sender.send(Priority::High, format!("high-{i}"));
            thread::sleep(Duration::from_millis(20));
        }
        timer_sender
            .send((Duration::from_millis(100), "delayed-job".into()))
            .ok();
    });

    // 主循环跑 1 秒后退出（真实代码里由 LoopSignal.stop() 触发，仿 Platform::quit）
    let signal = event_loop.get_signal();
    thread::spawn(move || {
        thread::sleep(Duration::from_secs(1));
        signal.stop();
    });

    event_loop.run(Some(Duration::from_millis(100)), &mut (), |(), (), ()| {})?;
    worker.join().ok();
    Ok(())
}
```

**验收要点**：

1. 所有 `high-*`/`low-*` 都以 `[main ...]` 打印且线程 id 相同（证明它们都回到主循环执行）。
2. 由于队列是严格优先级，每轮排空时 high 组先于 low 组（与 gpui 的抽签不同，见练习）。
3. `delayed-job` 在最后约 100ms 后由 `[timer-fired]` 打印，且只出现一次（`TimeoutAction::Drop`）。
4. 把 `insert_idle` 换成直接执行，观察输出顺序不变但语义上失去了「输入优先」的保障——在本例没有其他事件源，差异不可见；这正是 headless 直跑的理由。
5. 进阶：把严格优先改成 60/10 加权抽签（参照 [queue.rs:L328-L351](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/queue.rs#L328-L351)），验证 Low 任务不再被无限挤压。

运行结果与 calloop crates.io 版本的 API 细节差异待本地验证。

## 6. 本讲小结

- `LinuxDispatcher` **不拥有主事件循环**：主循环归 Wayland/X11/headless 客户端，调度器只持有 `main_sender`（队列 + ping）这根引线；它自建的线程只有 Worker 池（`max(核数, 2)` 条）和一条 Timer 线程。
- `PriorityQueueCalloopSender/Receiver` 是适配器：给无 fd 的同步优先级队列配上 eventfd 唤醒（`send` 后 ping、`Drop` 时 ping），接收端实现 `EventSource`，在 ping 就绪时 `try_iter` 排空并以 `channel::Event::Msg/Closed` 对外呈现，取到过任务就再 ping 一轮防漏。
- `dispatch_after` 走 `TimerAfter` 信封进 Timer 线程的私有 calloop 循环，每次插入一个 `TimeoutAction::Drop` 的一次性 timer 源；到期在 Timer 线程执行 runnable，真实链路中它只点亮 oneshot，由 waker 把等待的 future 带回原执行器。
- 失败处理三种姿态：后台 `dispatch` 失败 panic（fail-fast）；主线程投递失败 `mem::forget`（`!Send` future 不能在错误线程 drop，宁泄漏不 UB）；延迟投递失败同样 forget（drop 会取消任务并令等待者 panic）。
- X11/Wayland 把 runnable 包进 `insert_idle`，让用户输入与协议事件在同轮 dispatch 中永远先于普通任务；headless 无输入事件源，直跑即可。`quit` 的实现就是客户端循环的 `LoopSignal.stop()`。
- 即便在主线程通道里，「优先级」也只是 60/30/10 的抽签概率，不是严格顺序——防饿死是全链路的一致选择。

## 7. 下一步学习建议

1. **u4-l4（macOS 与 Windows 的调度器）**：对比 `MacDispatcher`（NSRunLoop/dispatch）与 Windows 消息循环版调度器如何映射同一契约，重点看它们与 Linux 版在「谁拥有主循环」「如何唤醒」上的差异。
2. **u4-l6（前台工作日志与 hang 检测）**：本讲反复出现的 `profiler::update_running_task`/`save_task_timing` 三段式就是那里的插桩点，可顺势深读。
3. **u5-l1（LinuxPlatform 与 LinuxClient）**：本讲的 `LinuxCommon::new` 只是装配的一角，下一单元完整展开「一个外壳、三种后端」的二次分发结构。
4. 延伸阅读：calloop 文档中 `EventSource` 的 register/reregister 协议（理解为什么 4.2 的三个方法必须委托给 PingSource），以及 [../gpui/src/queue.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/queue.rs) 末尾的通道行为测试（`tx.send` 高中低混合后接收顺序的断言方式）。
