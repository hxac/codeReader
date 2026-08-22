# 前台/后台执行器：GPUI 的单前台线程模型

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 GPUI 为什么把所有实体（Entity）更新与 UI 绘制都限制在**单一前台线程**上，以及这套约束在 Rust 类型系统层面是如何被强制的。
2. 读懂 `Platform` trait 中 `background_executor` / `foreground_executor` 两个契约方法，说明「平台层」在执行器创建中扮演的角色：平台负责构造一个 `PlatformDispatcher`，再用它同时造出前台、后台两个执行器。
3. 区分 `cx.spawn`（跑在主线程）与 `cx.background_spawn`（跑在后台线程池）的签名差异与适用场景。
4. 掌握 `Task` 的三种生命周期管理方式——**await、detach、存储在字段里**——并理解「Task 被 drop 即被取消」这一语义。

本讲是第 4 单元（调度与并发）的第一篇。u2-l2 已经讲过平台事件循环 `run/quit` 的生命周期；本讲把视角下沉一层，看事件循环里的「任务」是从哪些执行器里来的。下一讲（u4-l2）将继续深入 `PlatformDispatcher` 契约本身。

## 2. 前置知识

### 2.1 Rc、RefCell 与「非 Send」状态

- `Rc<T>` 是**单线程引用计数指针**：它没有原子操作、性能好，但编译器禁止它跨线程——`Rc` 不实现 `Send` trait。
- GPUI 的所有应用状态（`App`、实体表、窗口表）都装在 `Rc<RefCell<...>>` 里。这意味着：**只要状态不跨线程，就永远不需要锁**。
- 一个类型如果不实现 `Send`，就不能被 move 到别的线程。Rust 会在编译期拦住这种代码。本讲会看到 GPUI 如何利用这一点「锁死」前台状态。

### 2.2 Future 是惰性的

Rust 的 `async` 块本身不会执行，它只是构造出一个 `Future`。必须有「执行器」（executor）不断调用 `Future::poll`，任务才会推进。因此「任务跑在哪个线程」完全取决于「哪个执行器的 poll 发生在哪个线程」——这正是本讲的主题。

### 2.3 事件循环（复习 u2-l2）

平台事件循环（macOS 的 NSRunLoop、Linux 的 calloop、Windows 的消息循环）是主线程上那个「永不返回」的循环。GPUI 把待执行的任务排进这个循环，由循环驱动 poll。前台执行器的任务最终都回到这里执行。

### 2.4 优先级与线程池（直觉）

后台执行器背后通常是一个线程池，可以按 `Priority` 排队；前台只有一个线程，任务按入队顺序执行——后面源码会印证「前台优先级被忽略」这一细节。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [../gpui/src/platform.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/platform.rs) | `Platform` trait 契约：两个执行器方法（L127-L128）；`PlatformDispatcher` trait（L1029 起，下一讲主角） |
| [src/gpui_platform.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/src/gpui_platform.rs) | 门面 crate：便捷函数 `background_executor()`（L8-L11） |
| [../gpui/src/executor.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/executor.rs) | `BackgroundExecutor` / `ForegroundExecutor` 的定义与包装逻辑 |
| [../scheduler/src/executor.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../scheduler/src/executor.rs) | 底层 `scheduler` crate：`Task` 类型定义、`detach`、取消语义 |
| [../gpui/src/app.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/app.rs) | `App::new_app` 启动期提取执行器（L779 起）；`App::spawn`（L1939）；`background_spawn` 实现（L2860） |
| [../gpui/src/app/context.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/app/context.rs) | `Context<T>::spawn`——带 `WeakEntity<T>` 的前台 spawn（L237） |
| [../gpui/src/app/async_context.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/app/async_context.rs) | `AsyncApp` 上的 `spawn` / `background_spawn`——跨 await 点的上下文 |
| [../gpui_macos/src/platform.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_macos/src/platform.rs) | 平台侧样本一：`MacPlatform::new` 如何构造执行器 |
| [../gpui_linux/src/linux/platform.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/platform.rs) | 平台侧样本二：`LinuxCommon` 共享同一个 dispatcher 造两个执行器 |
| [../gpui_web/examples/hello_web/main.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_web/examples/hello_web/main.rs) | 实战范本：素数计数的前后台协作（本讲综合实践的参照） |

## 4. 核心概念与源码讲解

### 4.1 单前台线程模型：一个 dispatcher，两个执行器

#### 4.1.1 概念说明

GPUI 的并发模型可以概括成一句话：**「一个前台线程拥有全部 UI 状态；CPU 密集与 IO 工作丢到后台线程池，算完再把结果送回前台。」**

为什么这么设计？

1. **免锁**。所有实体状态（编辑器缓冲区、窗口树、焦点句柄……）都被 `Rc<RefCell<..>>` 包裹，只允许主线程触碰。既然只有一个线程能写，就不存在数据竞争，也就不需要一把锁。
2. **与平台事件循环天然对齐**。macOS AppKit、Windows UI、浏览器 DOM 都有「只能在主线程碰 UI」的硬性规定（u2-l2 已见过 macOS 主线程约束）。GPUI 把这条平台规则上升为自己的架构规则。
3. **渲染一致性**。绘制一帧需要读取大量实体状态；如果状态可以在别的线程被改，就必须在绘制期间加锁或做快照，复杂度爆炸。

于是执行器被一分为二：

- `ForegroundExecutor`（前台执行器）：在主线程 poll 任务。任务可以是**非 Send** 的（可以持有 `Rc`、可以 update 实体）。
- `BackgroundExecutor`（后台执行器）：在线程池 poll 任务。任务及其返回值都必须是 `Send`。

两者共享同一个 `PlatformDispatcher`——这是平台层提供的「投递通道」：前台任务靠它的 `dispatch_on_main_thread` 排进主线程事件循环，后台任务靠它的 `dispatch` 进入线程池。dispatcher 的完整契约留给下一讲，这里只需要记住它的四个核心方法。

#### 4.1.2 核心流程

```text
平台启动（如 MacPlatform::new）
        │
        ├─ 构造一个 PlatformDispatcher（MacDispatcher / LinuxDispatcher / ...）
        │
        ├─ BackgroundExecutor::new(dispatcher.clone())   ──► 后台线程池
        └─ ForegroundExecutor::new(dispatcher)           ──► 主线程事件循环
                    │
App::new_app(platform)
        │  platform.background_executor() / platform.foreground_executor()
        │  assert!(background_executor.is_main_thread())   ← 启动期断言：App 必须在主线程构造
        ▼
App 持有两个执行器 → 应用代码经 cx.spawn / cx.background_spawn 使用它们
```

#### 4.1.3 源码精读

先看契约。`Platform` trait 的头两个方法就是两个执行器的访问器，它们**没有默认实现**——每个平台必须提供：

- [../gpui/src/platform.rs:125-129](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/platform.rs#L125-L129) — `Platform` trait 开头：`fn background_executor(&self) -> BackgroundExecutor;` 与 `fn foreground_executor(&self) -> ForegroundExecutor;` 是契约的前两行，紧随其后的才是 `text_system` 与 `run`。执行器被排在 trait 最前面，可见其基础性。

再看两个执行器结构体如何用类型系统「锁死」线程约束：

- [../gpui/src/executor.rs:13-30](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/executor.rs#L13-L30) — `BackgroundExecutor` 与 `ForegroundExecutor` 的定义。两者都只是「内层 scheduler 执行器 + `Arc<dyn PlatformDispatcher>`」的包装。关键在 `ForegroundExecutor` 的最后一个字段：`not_send: PhantomData<Rc<()>>`——`PhantomData<Rc<()>>` 本身不占空间，却让整个类型**编译期不满足 `Send`**，你无法把它（以及持有它的 `App`、`Context`）move 到其他线程。
- [../gpui/src/executor.rs:283-286](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/executor.rs#L283-L286) — `BackgroundExecutor::is_main_thread()` 直接转发给 `dispatcher.is_main_thread()`。这个方法也解释了 `PlatformDispatcher` 为什么把 `is_main_thread` 列为第一个必需方法。

启动期的主线程断言在 `new_app`：

- [../gpui/src/app.rs:779-789](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/app.rs#L779-L789) — `App::new_app` 的开头三步：调用 `platform.background_executor()` 与 `platform.foreground_executor()` 取出两个执行器（u1-l2 讲过的「平台注入点」），随即 `assert!(background_executor.is_main_thread(), "must construct App on main thread")`。执行器来自平台、App 必须生在主线程——单前台线程模型从第一行代码就开始生效。

`PlatformDispatcher` 长什么样（只看签名，细节下一讲）：

- [../gpui/src/platform.rs:1029-1033](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/platform.rs#L1029-L1033) — `pub trait PlatformDispatcher: Send + Sync`：`is_main_thread`、`dispatch`（后台执行）、`dispatch_on_main_thread`（前台执行）、`dispatch_after`（延时）。注意它与 `Platform` 相反，**要求 `Send + Sync`**——因为它要被后台线程拿来向主线程投递任务。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「App 必须在主线程构造」这条断言，而不是只听文档说。

**操作步骤**：

1. 打开 [../gpui/src/app.rs:786-789](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/app.rs#L786-L789)，确认断言文本。
2. 阅读仓库现成测试 [../gpui_platform/src/gpui_platform.rs:113-140](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/src/gpui_platform.rs#L113-L140)（`test_foreground_tasks_run_with_run_until_parked`）：注意它被 `#[ignore]` 标注，注释写着「Standard Rust tests run on worker threads, which causes SIGABRT when interacting with macOS AppKit/Cocoa APIs」——标准 Rust 测试跑在 worker 线程上，而 GPUI 平台要求主线程，这正是断言的现实来源。
3. 用 rust-analyzer（或 `cargo doc`）查看 `ForegroundExecutor` 的 trait 实现（搜索 "impl Send for ForegroundEncoder" 之类），确认**不存在** `unsafe impl Send for ForegroundExecutor`。

**需要观察的现象**：`PhantomData<Rc<()>>` 字段存在但零开销；macOS 测试必须 `--ignored --test-threads=1` 才能跑。

**预期结果**：你会确认单前台线程不是「口头约定」，而是「断言 + 类型系统」双重强制。断言部分在 macOS 上最严格（AppKit 会直接 SIGABRT），类型系统部分全平台一致。

### 4.2 执行器从哪里来：平台层的角色与门面捷径

#### 4.2.1 概念说明

u1-l1 说过 gpui 主 crate 定义契约、四个平台 crate 负责实现。执行器也不例外，但有个精妙之处：**gpui 已经写好了 `BackgroundExecutor::new(dispatcher)` 和 `ForegroundExecutor::new(dispatcher)` 这两个通用构造器**（毕竟「包一个 dispatcher」这件事没有平台差异），平台层唯一要做的是：

1. 造一个属于自己的 `PlatformDispatcher` 实现（MacDispatcher、LinuxDispatcher、WebDispatcher……）；
2. 用**同一个** dispatcher 实例调用上面两个构造器；
3. 把结果存起来，在 `Platform::background_executor/foreground_executor` 里克隆返回。

两个执行器共享同一个 dispatcher 非常重要：前台任务与后台任务最终走的是同一条「投递通道」，只是出口不同（主线程事件循环 vs 线程池）。

另外，门面 crate 提供了一个独立入口 `gpui_platform::background_executor()`，让你**不启动完整应用**也能拿到当前平台的后台执行器——代价是它会临时构造一个 headless 平台。

#### 4.2.2 核心流程

```text
macOS: MacPlatform::new
  dispatcher = Arc::new(MacDispatcher::new())
  background_executor  = BackgroundExecutor::new(dispatcher.clone())
  foreground_executor  = ForegroundExecutor::new(dispatcher)
  → 存入 MacPlatformState，每次 background_executor() 调用都 clone 同一份

Linux: LinuxCommon::new（三种后端共享）
  dispatcher = Arc::new(LinuxDispatcher::new(main_sender))
  common.background_executor = BackgroundExecutor::new(dispatcher.clone())
  common.foreground_executor = ForegroundExecutor::new(dispatcher)
  → LinuxPlatform<P> 的 trait 实现转发到 inner.with_common(...)
```

#### 4.2.3 源码精读

- [../gpui_macos/src/platform.rs:196-221](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_macos/src/platform.rs#L196-L221) — `MacPlatform::new` 的开头：第 198 行先造 `MacDispatcher`，第 219-220 行用同一个 dispatcher 分别构造 `BackgroundExecutor` 与 `ForegroundExecutor`，存进平台状态。文本系统、键盘映射等也在这里初始化（u2-l1 讲过的注入流程）。
- [../gpui_macos/src/platform.rs:478-485](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_macos/src/platform.rs#L478-L485) — `impl Platform for MacPlatform` 的前两个方法：加锁、clone、返回。执行器是启动时一次性造好的共享句柄。
- [../gpui_linux/src/linux/platform.rs:156-162](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L156-L162) — Linux 侧同款剧本：`LinuxDispatcher::new(main_sender)` 之后一行造后台执行器、一行造前台执行器。这段代码在 `LinuxCommon::new` 里，意味着 Wayland/X11/headless 三种后端**共享同一对执行器实现**（u5-l1 会展开 LinuxClient 分发结构）。
- [../gpui_linux/src/linux/platform.rs:233-242](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L233-L242) — `LinuxPlatform<P>` 的 trait 实现：`self.inner.with_common(|common| common.background_executor.clone())`。外壳转发到公共状态，与 macOS 直存直取殊途同归。
- [src/gpui_platform.rs:8-11](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/src/gpui_platform.rs#L8-L11) — 门面捷径：`pub fn background_executor() -> gpui::BackgroundExecutor { current_platform(true).background_executor() }`。注意它传入 `true`（headless）——只是要个后台线程池，不需要真实窗口系统；且**每次调用都新建一个平台实例**，适合工具脚本，别在应用主路径里反复调用。
- [../gpui/src/app.rs:1916-1927](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/app.rs#L1916-L1927) — App 暴露的两个访问器。注意 `foreground_executor()` 在应用退出阶段（`quitting`）会 panic：`"Can't spawn on main thread after on_app_quit"`——退出流程开始后不再接收新的前台任务，这是 u2-l2 优雅退出链路的配套防御。

#### 4.2.4 代码实践

**实践目标**：验证「两个执行器共享同一个 dispatcher」这一结构事实。

**操作步骤**：

1. 阅读 [../gpui/src/executor.rs:65-82](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/executor.rs#L65-L82)（`BackgroundExecutor::new`）与 [../gpui/src/executor.rs:294-345](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/executor.rs#L294-L345)（`ForegroundExecutor::new`），数一数两者各自保存了几份 `dispatcher`。
2. 用 Grep 在四个平台 crate 里搜索 `BackgroundExecutor::new`，记录每个平台构造执行器的位置（macOS 在 platform.rs:219、Linux 在 linux/platform.rs:158，gpui_windows 与 gpui_web 留给你找）。
3. 回答：如果平台用两个**不同**的 dispatcher 分别构造两个执行器，会发生什么？（见下面练习答案）

**需要观察的现象**：每个平台 crate 恰好有一处 `BackgroundExecutor::new` 与一处 `ForegroundExecutor::new`，且相邻、共享同一 dispatcher 变量。

**预期结果**：你会得到一张「平台 → dispatcher → 两个执行器」的对应表，这正是综合实践要画的图的一半。（gpui_windows / gpui_web 的具体行号：待本地确认，用 Grep 搜 `BackgroundExecutor::new` 即可。）

#### 4.2.5 小练习与答案

**练习 1**：`gpui_platform::background_executor()` 为什么传 `true`（headless）而不是 `false`？

**答案**：这个函数的用途是「不启动 GUI 应用也要个后台线程池」（比如写个小工具、benchmark）。headless 平台不需要连接 Wayland/X11/窗口系统，构造更快、依赖更少、在无显示环境（CI）也能工作；而它产出的后台执行器行为与带窗口的完全一致。

**练习 2**：如果某平台错误地用两个不同的 dispatcher 分别构造前台、后台执行器，最直接的症状会是什么？

**答案**：两个执行器的任务走两条互不相通的投递通道，`is_main_thread()` 的判断依据、队列甚至时钟可能不一致；更严重的是后台任务完成后的「回前台」唤醒可能送不到前台 dispatcher 对应的事件循环，导致后台结果永远不被 poll（典型表现：界面卡住不更新）。现有实现全部共享同一 dispatcher 正是为了保证「后台算完能唤醒前台」。

**练习 3**：`App::foreground_executor()` 为什么在 quitting 时 panic，而 `background_executor()` 不做这个检查？

**答案**：前台执行器把任务排进主线程事件循环，而退出流程意味着事件循环即将结束、on_app_quit 观察者已执行，此时再排任务要么永远不跑要么在半关闭状态跑，属编程错误，fail-fast 最安全；后台线程池与主线程生命周期解耦，退出期间仍允许收尾工作（如写盘）继续在后台进行。

### 4.3 两个执行器的分工：Send 与非 Send、优先级与计时器

#### 4.3.1 概念说明

把两个执行器的 `spawn` 签名并排放着看，差异一目了然：

| 维度 | `BackgroundExecutor::spawn` | `ForegroundExecutor::spawn` |
| --- | --- | --- |
| future 约束 | `Future + Send + 'static` | `Future + 'static`（**可以非 Send**） |
| 输出约束 | `R: Send + 'static` | `R: 'static` |
| 内部装箱 | `future.boxed()` | `future.boxed_local()` |
| 运行位置 | 平台线程池（GCD worker、web worker 等） | 主线程事件循环 |
| 优先级 | 生效（`Priority`，还有 `spawn_dedicated` 专用线程） | **被忽略**（按序执行） |
| 典型用途 | 素数计数、文件 IO、索引构建 | 更新实体状态、刷新 UI、串行化状态变更 |

一句话记忆：**后台执行器要求一切可跨线程（Send），前台执行器允许持有 Rc 但必须乖乖排队。**

#### 4.3.2 核心流程

```text
cx.background_spawn(fut)                    cx.spawn(fut)
        │                                          │
BackgroundExecutor::spawn                  ForegroundExecutor::spawn
要求 fut: Send                            fut 可以 !Send（boxed_local）
        │                                          │
dispatcher.dispatch(runnable, priority)    dispatcher.dispatch_on_main_thread(...)
        │                                          │
   后台线程池 poll                          主线程事件循环 poll
```

#### 4.3.3 源码精读

- [../gpui/src/executor.rs:112-119](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/executor.rs#L112-L119) — `BackgroundExecutor::spawn`：签名要求 `Send + 'static`，内部直接转给 `spawn_with_priority(Priority::default(), ...)`。
- [../gpui/src/executor.rs:126-139](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/executor.rs#L126-L139) — `spawn_with_priority`：`Priority::RealtimeAudio` 走专用实时线程，其余进优先级队列。后台是「池 + 优先级」的世界。
- [../gpui/src/executor.rs:347-368](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/executor.rs#L347-L368) — `ForegroundExecutor::spawn` 与 `spawn_with_priority` 对照组：前者 `boxed_local()`（非 Send future），后者干脆用注释挑明「Priority is ignored for foreground tasks - they run in order on the main thread」——前台按入队顺序执行，没有插队。
- [../gpui/src/executor.rs:91-110](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/executor.rs#L91-L110) — `spawn_dedicated`：为深递归/大栈 future 开独立 OS 线程。文档注释给出了一个精彩理由：macOS GCD worker 线程栈被内核固定在 512 KiB，是所有平台里最紧的，栈不够的任务必须用专用线程（标准 2 MiB）。这是「平台差异如何影响执行器 API」的直接例证。
- [../gpui/src/executor.rs:175-192](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/executor.rs#L175-L192) — `now()` 与 `timer(duration)`：统一时钟入口，测试时可换成假时钟（CLAUDE.md 里「GPUI 测试优先用 GPUI 计时器」指的就是它，u8-l4 会展开）。
- [../gpui/src/executor.rs:370-399](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/executor.rs#L370-L399) — `spawn_when_idle`：把任务排到平台「空闲时间片」执行（无超时则可能无限期推迟）。适合低优先级的擦除性工作，是前台执行器里唯一近似「优先级」的机制。

顺带一提 wasm 的特殊性：浏览器里「后台线程」未必存在。[../gpui_web/src/dispatcher.rs:164-167](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_web/src/dispatcher.rs#L164-L167) 显示 `supports_threads` 要同时满足 multithreaded feature、允许线程与两组 WebAssembly API；不支持时后台任务也退化到主线程邮箱排队。代码写法不变，线程模型收窄——这是下一讲 u4-l5 的预告。

#### 4.3.4 代码实践

**实践目标**：用编译器当老师，亲手触发一次 Send 检查失败。

**操作步骤**：

1. 在任意能编译 gpui 的示例（如 `crates/gpui/examples/hello_world.rs` 的副本）里加一段**示例代码**：

   ```rust
   // 示例代码：故意在后台任务里持有 Rc，观察编译错误
   use std::rc::Rc;
   let rc = Rc::new(42);
   cx.background_spawn(async move {
       println!("{}", *rc); // 预期：Rc<i32> cannot be sent between threads safely
   })
   .detach();
   ```

2. 把同样的 `Rc` 移进 `cx.spawn(async move |cx| { ... })`，确认**可以编译**。
3. `cargo check -p gpui --example hello_world`（改的是副本，注意别提交）。

**需要观察的现象**：第 1 步报错信息形如 `Rc<i32> cannot be sent between threads safely`，并指出 `Send` 不满足；第 2 步通过。

**预期结果**：直观建立「background_spawn 的 Send 门槛、spawn 没有」的肌肉记忆。（具体报错文案随编译器版本略有差异：待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ForegroundExecutor::spawn_with_priority` 接受 `priority` 参数却在实现里忽略它？

**答案**：主线程只有一个执行流，任务天然按入队顺序串行执行，「优先级插队」没有意义；保留参数是为了与 `BackgroundExecutor` 的 API 对称，调用方可以用同一套代码面向两种执行器编程，不必为前台单独写分支。

**练习 2**：`spawn_dedicated` 的文档为什么要专门提到 macOS 的 512 KiB 栈？

**答案**：后台任务默认在平台 dispatcher 提供的线程上 poll，而 macOS 的 GCD worker 线程栈被内核固定为 512 KiB（`PTH_DEFAULT_STACKSIZE`），是所有平台里最小的；深递归或大局部变量的 future 在这里可能栈溢出，所以提供「新 OS 线程 + 标准 2 MiB 栈」的逃生门。这说明「后台执行器」的具体行为仍由平台决定。

**练习 3**：`background_executor().timer(Duration::from_secs(1))` 与直接 `std::thread::sleep` 有什么本质区别？

**答案**：`timer` 返回一个 `Task<()>`，它是**可组合的异步等待**——不阻塞当前线程，超时后由调度器唤醒后续逻辑，且测试中可被假时钟推进；`sleep` 同步阻塞所在线程，在主线程调用会直接冻结整个 UI 事件循环。

### 4.4 应用层入口：cx.spawn 与 cx.background_spawn

#### 4.4.1 概念说明

直接持有执行器对象的场景很少，应用代码几乎总是通过上下文（context）来 spawn。gpui 在多个层级提供了入口，签名各有侧重：

| 入口 | 所在上下文 | 任务跑在哪 | 闭包参数 | 典型场景 |
| --- | --- | --- | --- | --- |
| `App::spawn` | `&App`（同步） | 前台 | `&mut AsyncApp` | 应用级异步初始化 |
| `Context<T>::spawn` | `&Context<T>`（实体上下文） | 前台 | `WeakEntity<T>`, `&mut AsyncApp` | 视图/实体发起的任务（防内存泄漏） |
| `AsyncApp::spawn` | `&AsyncApp`（已异步） | 前台 | `&mut AsyncApp` | 异步链里再排队 |
| `App/AsyncApp::background_spawn` | 任意 cx（经 deref） | 后台 | 无闭包，直接吃 future | CPU/IO 密集工作 |

最常用也最有 GPUI 特色的是 `Context<T>::spawn`：它自动把「当前实体」降级为**弱句柄** `WeakEntity<T>` 传进闭包。任务完成后用 `this.update(cx, ...)` 回写状态；如果实体已被销毁，`update` 返回 `Err`，任务安静终止——这就是 CLAUDE.md 强调的避免 `Entity` 互相持有导致内存泄漏的机制。

#### 4.4.2 核心流程

```text
Context<T>::spawn(async move |this, cx| { ... })
        │  this = self.weak_entity()   ← 弱句柄，不延长实体寿命
        ▼
App::spawn(async move |cx| f(this, cx).await)
        │
ForegroundExecutor::spawn(... .boxed_local())      ← 排进主线程
        ▼
主线程 poll 闭包 → 遇到 await cx.background_spawn(fut)
        │
        ├─ 后台线程池执行 fut（Send）
        ▼ 完成后唤醒前台 future
this.update(cx, |this, cx| { 改状态; cx.notify(); })   ← 回到主线程改 UI
```

#### 4.4.3 源码精读

- [../gpui/src/app/context.rs:233-245](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/app/context.rs#L233-L245) — `Context<T>::spawn`：三行实现——取弱句柄、转交 `App::spawn`。文档点明「The returned task must be held or detached」（Task 必须被持有或 detach，直接丢弃等于取消）。
- [../gpui/src/app.rs:1936-1952](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/app.rs#L1936-L1952) — `App::spawn`：把闭包包一层 `boxed_local()` 后交给 `foreground_executor.spawn`。注意它同样有 quitting 防御（`debug_panic!`）。
- [../gpui/src/app/async_context.rs:204-212](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/app/async_context.rs#L204-L212) — `AsyncApp::spawn`：克隆自身作为闭包上下文，再走前台执行器。`AsyncApp` 可克隆、可跨 await 持有（CLAUDE.md「Contexts」一节）。
- [../gpui/src/app.rs:2860-2865](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/app.rs#L2860-L2865) — `AppContext` trait 的 `background_spawn` 在 `App` 上的实现：一行 `self.background_executor.spawn(future)`。`Context<T>` deref 到 `App`，所以在事件回调里写 `cx.background_spawn(...)` 走的就是这里。
- [../gpui/src/app/async_context.rs:126-132](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/app/async_context.rs#L126-L132) — `AsyncApp::background_spawn`：同样一行转交。hello_web 里「spawn 的闭包内部再 background_spawn」用的正是它。

对照范本（下一节详读）：[../gpui_web/examples/hello_web/main.rs:131-134](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_web/examples/hello_web/main.rs#L131-L134) 中 `cx.spawn(async move |this, cx| { let count = cx.background_spawn(...).await; ... })` 就是「前台任务里嵌后台任务」的标准句式。

#### 4.4.4 代码实践

**实践目标**：跟踪一条完整的「spawn → background_spawn → 回前台」调用链，把每个环节对应的源码函数抄下来。

**操作步骤**：

1. 从 [../gpui_web/examples/hello_web/main.rs:131](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_web/examples/hello_web/main.rs#L131) 的 `cx.spawn` 出发。
2. 用 rust-analyzer 的「Go to Definition」依次跳转：`Context::spawn` → `App::spawn` → `ForegroundExecutor::spawn`；再跳 `cx.background_spawn` → `AsyncApp::background_spawn` → `BackgroundExecutor::spawn`。
3. 把每一站的文件名、函数名、关键行（boxed_local / boxed 的分叉点）记成一张链路表。

**需要观察的现象**：两条链在「装箱方式」处分道——前台 `boxed_local()`（单线程 future），后台经 `spawn_with_priority` → `boxed()`（多线程 future）。

**预期结果**：得到一条可复述的链路：`Context::spawn → App::spawn → ForegroundExecutor::spawn(boxed_local) → 主线程`，分支 `AsyncApp::background_spawn → BackgroundExecutor::spawn(boxed) → 线程池`。

#### 4.4.5 小练习与答案

**练习 1**：`Context<T>::spawn` 为什么传 `WeakEntity<T>` 而不是 `Entity<T>`？

**答案**：`Entity<T>` 是强句柄，会延长实体寿命；如果视图被关闭但它发起的任务还持有强句柄，实体永远不释放，且任务回写时可能撞上「实体正在被 update」的借用冲突。弱句柄在实体销毁后 `update` 直接失败，任务自然终止，防止内存泄漏与悬垂更新。

**练习 2**：在 `cx.listener` 的同步回调里可以直接调用 `cx.background_spawn` 吗？为什么？

**答案**：可以。`listener` 回调拿到 `&mut Context<T>`，它 deref 到 `App`，而 `App` 实现了 `AppContext::background_spawn`。同步回调本身在主线程，把 future 丢给后台执行器后立即返回，不阻塞 UI——这正是「主线程只排队、后台做重活」的日常用法。

**练习 3**：`cx.spawn` 的闭包参数 `cx` 与外层的 `cx` 是同一个东西吗？

**答案**：不是。闭包里的 `cx` 是 `&mut AsyncApp`——可跨 await 点持有的异步上下文；外层是 `&mut Context<T>`（同步、带实体绑定）。CLAUDE.md 明确要求闭包内必须用内层 `cx`，避免多重借用问题。hello_web 第 131-136 行就是标准写法：外层 `cx.spawn(...)`，内层用 `cx.background_spawn` 与 `this.update(cx, ...)`。

### 4.5 Task 的生命周期：await、detach 与存储

#### 4.5.1 概念说明

`spawn` 与 `background_spawn` 都返回 `Task<T>`。它是 `Future`，但比普通 future 多了一条铁律：**`Task` 被 drop 就是被取消**。于是每个 Task 都面临「怎么活下来」的选择，共三种管理方式：

| 方式 | 写法 | 语义 | 适用场景 |
| --- | --- | --- | --- |
| await | `let v = task.await;` | 挂起当前异步流直到任务完成，取返回值 | 需要结果继续计算 |
| detach | `task.detach();` / `task.detach_and_log_err(cx);` | 放弃返回值，任务跑到完（错误打日志） | 「发射后不管」：自动保存、日志上报 |
| 存储 | `self._tasks.push(task);` | 存字段，实体活着任务就活着 | 生命周期应跟随视图的任务 |

第三种最容易被忽视：`Task` 存在局部变量里，回调一返回就被 drop，任务当场取消——代码「看起来启动了」却什么都没发生。Zed 的 hello_web 示例专门用 `_tasks: Vec<Task<()>>` 字段示范正确做法。

#### 4.5.2 核心流程

```text
spawn 返回 Task<T>
   │
   ├─ 被 drop（默认！） ──► 立即取消（async_task 语义）
   │
   ├─ .await             ──► 当前异步流等它，拿到 T
   ├─ .detach()          ──► 无人持有也跑完，丢弃 T
   ├─ .detach_and_log_err(cx)（Task<Result<T,E>> 专用）──► 跑完且错误自动打日志
   └─ 存进 self._tasks   ──► 与实体同寿命；再次 start_search 前先 clear() 取消旧任务
```

#### 4.5.3 源码精读

- [../scheduler/src/executor.rs:373-380](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../scheduler/src/executor.rs#L373-L380) — `Task` 的权威定义，文档写明两条规则：「It implements `Future` so you can `.await` on it」「If you drop a task it will be cancelled immediately. Calling `Task::detach` allows the task to continue running, but with no way to return a value」。`#[must_use]` 属性让编译器对「spawn 后无视返回值」发出警告。
- [../scheduler/src/executor.rs:549-557](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../scheduler/src/executor.rs#L549-L557) — `Task::detach` 的实现：把内部 `async_task::Task` 交给后台跑完，不保留任何联系。
- [../gpui/src/executor.rs:32-63](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/executor.rs#L32-L63) — gpui 的 `TaskExt` 扩展 trait：为 `Task<Result<T, E>>` 提供 `detach_and_log_err(cx)` 与 `detach_and_log_err_with_backtrace(cx)`。实现方式是把「记录错误」的 future 重新 spawn 到前台执行器再 detach——既跑完任务又不吞错误（CLAUDE.md「Never silently discard errors」的工程化落地）。
- [../gpui_web/examples/hello_web/main.rs:90-95](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_web/examples/hello_web/main.rs#L90-L95) — `HelloWeb` 状态字段里的 `_tasks: Vec<Task<()>>`：素数计算的 12 个任务全部存这里，与实体同寿命。
- [../gpui_web/examples/hello_web/main.rs:118](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_web/examples/hello_web/main.rs#L118) — `self._tasks.clear();`：开始新一轮计数前清空旧任务——被 clear 的 Task 走 drop，即「取消上一轮还在跑的任务」。存储方式同时兼任了取消机制。

#### 4.5.4 代码实践

**实践目标**：观察「drop 即取消」在真实代码里的运用。

**操作步骤**：

1. 读 [../gpui_web/examples/hello_web/main.rs:107-165](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_web/examples/hello_web/main.rs#L107-L165) 的 `start_search`：注意第 118 行 `clear()` 与第 163 行 `self._tasks.push(task)` 的呼应。
2. 做一个思想实验并写出答案：如果把第 163 行改成局部变量 `let _task = task;`（不存字段），连点两次「Count Primes」会发生什么？
3. 再思考：`this.update(cx, ...)` 在实体已销毁时返回 `Err`，示例代码用 `.ok()` 丢弃——这与「不吞错误」原则矛盾吗？

**需要观察的现象**：`clear()` 时机在「新 Run 覆盖 current_run 之后、新任务 spawn 之前」；旧任务即使被取消，已 `push` 进 `chunk_results` 的成果也不会被新 Run 读到（因为 `current_run` 已整体替换）。

**预期结果**：你能解释清楚——(2) 局部变量在迭代变量作用域结束后立即 drop，任务可能在第一步完成前就被取消，进度条停摆；(3) 不矛盾，实体销毁导致的 `Err` 是预期内的生命周期事件，不是错误；真正的 IO/计算错误在 `Result` 里另行处理。（结合运行验证：待本地验证。）

#### 4.5.5 小练习与答案

**练习 1**：`#[must_use]` 加在 `Task` 上解决什么问题？

**答案**：`spawn` 返回值如果被无视（既不 await 也不 detach 也不存储），任务会在语句结束后立刻 drop 取消，形成「看似启动实则没跑」的隐蔽 bug。`#[must_use]` 让编译器对 `let _ = cx.spawn(...)` 之外的忽略行为发出 `unused_must_use` 警告，把这类 bug 拦在编译期。

**练习 2**：`detach()` 与 `detach_and_log_err(cx)` 该怎么选？

**答案**：任务输出是 `Task<()>` 或调用方自己 await 处理错误时用 `detach()`；任务输出是 `Task<Result<T, E>>` 且「发射后不管」时用 `detach_and_log_err(cx)`，它保证失败信息进日志而不是无声消失（内部把日志逻辑重新 spawn 到前台执行器再 detach）。需要 anyhow 完整回溯时还有 `detach_and_log_err_with_backtrace` 变体。

**练习 3**：把任务存进 `self._tasks` 而不是 detach，除了「保活」还有什么好处？

**答案**：获得**取消权**。存字段意味着实体决定任务寿命：新一轮开始时 `clear()` 即可取消旧任务（hello_web 正是这么做的），视图销毁时任务随实体一起结束，不会出现「视图没了任务还在往里写」的竞态。detach 出去的任务则再也无法收回。

### 4.6 实战范本：hello_web 的素数计数流水线

#### 4.6.1 概念说明

hello_web 示例把本讲全部知识点串成了一条流水线：把 \(N=10^7 \sim 10^8\) 的素数计数拆成 12 块（`NUM_CHUNKS`），每块一个「前台任务包裹后台计算」的组合任务；后台算完逐块回前台更新进度，12 块全部到齐后汇总出结果。它虽然是 wasm 示例，但用的全是平台无关 API（`cx.spawn` / `cx.background_spawn` / `Task`），在桌面平台逐字可用——这正是综合实践的出发点。

#### 4.6.2 核心流程

```text
点击 Count Primes
  │ start_search(cx)
  │   current_run = Run{ chunks_done: 0, ... }; _tasks.clear()   ← 取消上一轮
  │   for i in 0..12:
  │       cx.spawn(async move |this, cx| {                        ← 前台任务
  │           let count = cx.background_spawn(                    ← 后台计算（Send）
  │               async move { count_primes_in_range(a, b) }).await;
  │           this.update(cx, |this, cx| {                        ← 回前台
  │               run.chunks_done += 1; ...; cx.notify();         ← 触发重绘
  │           }).ok();
  │       }) → 存入 self._tasks
  ▼
进度条 12 格逐格点亮 → 全部完成显示 "π(N) = ... (xxx ms)"
```

理想情况下 12 块并行，墙钟时间约为串行的 \( \frac{T_{\text{串行}}}{\min(12,\,P)} \)（\(P\) 为后台线程数）。

#### 4.6.3 源码精读

- [../gpui_web/examples/hello_web/main.rs:45](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_web/examples/hello_web/main.rs#L45) — `const NUM_CHUNKS: u64 = 12;`：切块数。切多的意义在于让进度条有颗粒度、也让少于 12 核的机器依然吃满线程池。
- [../gpui_web/examples/hello_web/main.rs:82-95](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_web/examples/hello_web/main.rs#L82-L95) — 状态模型：`Run` 记录本轮的 `chunks_done` / `chunk_results` / `total` / `elapsed`；`HelloWeb` 持有 `current_run`、`history` 与 `_tasks: Vec<Task<()>>`。
- [../gpui_web/examples/hello_web/main.rs:123-134](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_web/examples/hello_web/main.rs#L123-L134) — 流水线核心三行：循环里先算出本块的 `[range_start, range_end)`，然后 `cx.spawn` 前台任务、任务体内 `cx.background_spawn` 后台计算并 `.await`。注意两次 `async move`：外层捕获 `this`（弱句柄）与 `cx`，内层只捕获两个 `u64`，天然满足 Send。
- [../gpui_web/examples/hello_web/main.rs:136-160](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_web/examples/hello_web/main.rs#L136-L160) — 回前台收尾：`this.update(cx, ...)` 累加 `chunks_done`，第 12 块到达时求和、记录耗时、写入 `history`，最后 `cx.notify()` 触发重绘。`.ok()` 容忍实体已销毁的情况。
- [../gpui_web/examples/hello_web/main.rs:11-39](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_web/examples/hello_web/main.rs#L11-L39) — 故意用暴力法判素数（`is_prime` 逐个试除），注释写明「intentionally brute-force so it hammers the CPU」——示例的教学目的就是制造足够重的后台负载。

#### 4.6.4 代码实践

**实践目标**：纯阅读任务——给 hello_web 的数据流标注「线程」。

**操作步骤**：

1. 准备一张三列空表：`代码位置 | 跑在前台还是后台 | 依据`。
2. 逐行填入：`start_search` 主体（按钮回调）、`count_primes_in_range`、`this.update` 闭包、`format_number`、`render`。
3. 依据写源码证据，如「`cx.background_spawn` 的参数 future → 后台」「`cx.listener` 回调 → 前台」。

**需要观察的现象**：所有**写状态**的代码（`current_run`、`history`、`cx.notify`）都在前台；后台只有纯函数计算，输入输出全是 `u64`（Send）。

**预期结果**：得到一张体现「重活出主线程、状态变更进主线程」的对照表，它就是综合实践的施工蓝图。

#### 4.6.5 小练习与答案

**练习 1**：如果把 `count_primes_in_range(range_start, range_end).await` 直接写在 `cx.spawn` 的外层闭包里（不经过 `background_spawn`），会发生什么？

**答案**：计算会在主线程执行，事件循环被 100M 次试除占住数秒，进度条、按钮、整个页面完全冻结——这正是本讲要避免的反模式；也是验证「UI 不卡顿」实践时的对照组。

**练习 2**：示例用 `web_time::Instant::now()` 计时（第 121 行），移植到桌面时应该换成什么？

**答案**：`std::time::Instant::now()`。`web_time` 是 `std::time` 在 wasm 上的替身（浏览器环境的 time API 不同）；桌面平台直接用标准库。更讲究的做法是用 `cx.background_executor().now()`，测试时可被假时钟接管（见 4.3.3 的 `now()`）。

**练习 3**：为什么每块计算的结果要经 `this.update` 回前台累加，而不是各块算完直接 `std::sync::atomic` 原子累加？

**答案**：原子累加只能更新一个数字，而 UI 需要的是「更新 `Run` 状态 + `cx.notify()` 触发重绘」——实体更新与通知都必须发生在主线程（单前台线程模型）。此外逐块回前台还能驱动进度条逐格点亮，这是纯原子计数做不到的。

## 5. 综合实践

**任务**：把 hello_web 的素数计数流水线移植成一个**桌面示例**，验证「后台满载时 UI 依然流畅」。

**准备**：`crates/gpui/examples/` 目录下已有大量示例且 gpui 的 dev-dependencies 已包含 `gpui_platform`（见 [../gpui/Cargo.toml:131-136](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/Cargo.toml#L131-L136)），因此新示例可以直接 `use gpui_platform::application;`（范本：[../gpui/examples/hello_world.rs:7](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/examples/hello_world.rs#L7) 与 [L92-L109](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/examples/hello_world.rs#L92-L109) 的窗口骨架）。

**步骤**：

1. 新建 `crates/gpui/examples/prime_counter.rs`（示例代码，如下骨架可直接扩充）：

   ```rust
   // 示例代码：桌面版素数计数（骨架）
   use gpui::{App, Context, SharedString, Task, Window, WindowOptions, div, prelude::*, px, rgb, size, Bounds, WindowBounds};
   use gpui_platform::application;

   struct PrimeCounter {
       chunks_done: u64,
       total: Option<u64>,
       _tasks: Vec<Task<()>>,          // 存储：任务与实体同寿命
   }

   const NUM_CHUNKS: u64 = 12;

   impl PrimeCounter {
       fn start(&mut self, cx: &mut Context<Self>) {
           self.chunks_done = 0;
           self.total = None;
           self._tasks.clear();        // 取消上一轮
           cx.notify();

           let limit = 100_000_000u64;
           let chunk = limit / NUM_CHUNKS;
           for i in 0..NUM_CHUNKS {
               let (a, b) = (i * chunk, if i == NUM_CHUNKS - 1 { limit } else { (i + 1) * chunk });
               let task = cx.spawn(async move |this, cx| {
                   // 后台：Send 的纯计算
                   let count = cx.background_spawn(async move { count_primes_in_range(a, b) }).await;
                   // 前台：更新实体状态并触发重绘
                   this.update(cx, |this, cx| {
                       this.chunks_done += 1;
                       if this.chunks_done == NUM_CHUNKS { /* 记录 total */ }
                       cx.notify();
                   }).ok();
               });
               self._tasks.push(task);  // 关键：保活
           }
       }
   }
   // is_prime / count_primes_in_range 抄自 hello_web/main.rs:11-39
   // render：显示 "{chunks_done}/{NUM_CHUNKS}" 与进度条，按钮 on_click 调 start
   ```

2. 补全 `is_prime` / `count_primes_in_range`（从 hello_web 第 11-39 行复制）与 `Render` 实现（进度条可参考 hello_web 第 251-307 行的 `w(gpui::relative(fraction))` 写法），`main` 抄 hello_world 第 92-109 行。
3. 运行：`cargo run -p gpui --example prime_counter`（Linux 上 gpui 默认启用 `wayland`/`x11` feature，无需额外参数）。

**观察与验证**：

1. 点击开始后，进度条应当**逐格推进**——说明主线程在渲染帧，而不是被计算占住。
2. 计算进行中**持续晃动鼠标 / 反复点击按钮**：光标样式与按钮 hover 反馈应即时变化（对照组：把 `background_spawn` 改成直接在 `cx.spawn` 闭包里同步计算，UI 应立刻冻结数秒——验证 4.6 练习 1 的预言）。
3. 连点两次开始：第二次点击应 `clear()` 取消旧任务、进度从零重计（验证 4.5 的存储-取消机制）。
4. 终端运行 `time` 或观察窗口内耗时字段，对比 `NUM_CHUNKS = 1` 与 `12` 的总耗时，验证近似 \( \frac{T_1}{\min(12, P)} \) 的加速。

**预期结果**：UI 全程流畅、进度逐块推进、取消与重开行为正确。若你的机器核数少于 12，加速比按核数封顶。（各平台具体渲染表现：待本地验证。）

## 6. 本讲小结

- GPUI 采用**单前台线程模型**：全部实体状态与 UI 绘制都在主线程，`ForegroundExecutor` 用 `PhantomData<Rc<()>>` 在编译期强制 `!Send`，`App::new_app` 再用 `is_main_thread` 断言兜底。
- 执行器的**契约**是 `Platform` trait 的前两个方法；**实现材料**由平台提供——每个平台造一个 `PlatformDispatcher`，用同一个实例构造前台、后台两个执行器（macOS 与 Linux 的源码结构完全同构）。
- 分工口诀：`background_spawn` 吃 `Send` future 跑线程池（支持优先级、专用线程）；`cx.spawn` 允许非 Send future、按序跑主线程（优先级被忽略）。
- `Context<T>::spawn` 自动传 `WeakEntity<T>`，任务完成后经 `this.update` 回前台改状态，实体销毁则任务安静终止——防泄漏的关键设计。
- `Task` 被 drop 即被取消；三种管理方式是 **await**（要结果）、**detach / detach_and_log_err**（发射后不管）、**存字段**（与实体同寿命且保留取消权）。
- hello_web 示例是标准范本：切 12 块 → 前台任务包裹后台计算 → 逐块回前台 `cx.notify()` → `_tasks` 字段保活并支持整轮取消。

## 7. 下一步学习建议

本讲回答了「执行器是什么、怎么用」，但刻意绕开了底层通道：前台任务如何被「投递」进主线程事件循环、后台线程池如何被唤醒。下一讲 **u4-l2（PlatformDispatcher 契约）** 将精读 [../gpui/src/platform.rs:1029](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/platform.rs#L1029) 起的 trait 全文与 `RunnableVariant`、`ThreadedDispatcher` 通用实现。建议先行阅读：

1. [../gpui/src/platform.rs:1029-1060](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/platform.rs#L1029-L1060) — `PlatformDispatcher` 全部方法，数一数哪些有默认实现。
2. [../gpui/src/platform/threaded_dispatcher.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/platform/threaded_dispatcher.rs) — 「POSIX 线程 + 管道唤醒」的最小实现，是理解一切 dispatcher 的参照系。
3. 带着问题读：`dispatch` 与 `dispatch_on_main_thread` 在 `ThreadedDispatcher` 里最终是不是同一个队列？（答案将在 u4-l2 展开。）
