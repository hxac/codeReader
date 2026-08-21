# GPUI 并发模型：executor 与 Task

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 GPUI 的双执行器架构：`ForegroundExecutor` 只在主线程轮询 future，`BackgroundExecutor` 把 future 交给平台线程池。
2. 区分 `cx.spawn` / `cx.background_spawn` 两条派发路径，并解释为什么后者的 future 必须满足 `Send + 'static` 而前者不需要。
3. 掌握 `Task` 的三种归宿：被 `await`、被 `detach`、被 Drop（取消），以及「取消发生在轮询边界」这一关键语义。
4. 理解 `PlatformDispatcher` 如何把 GPUI 的调度请求翻译成操作系统原语（主线程派发、后台线程池、定时器），从而让同一套 executor 代码跑在 macOS / Linux / Windows / Web 上。
5. 知道 `profiler` feature 在调度路径上埋了哪些计数点（`ForegroundRunnableCounter`），并明白它们只做观测、不改变任何调度语义——这是 u7-l6 前台工作日志的入口。

本讲承接 u2-l3 建立的上下文体系：那一讲我们知道了 `Context::spawn` 会把 `WeakEntity<T>` 递进异步闭包，本讲回答「这些异步闭包到底被谁、在哪个线程、按什么规则执行」。

## 2. 前置知识

### 2.1 Rust async 基础回顾

- **future 是惰性的**：`async` 块本身不执行任何代码，只有被 `poll` 时才推进到下一个 `await` 点。
- **executor（执行器）**：负责反复调用 `poll` 的调度者。Rust 标准库不提供执行器，每个框架自带。
- **Waker**：future 在 `poll` 返回 `Pending` 前可以登记一个唤醒器，事件就绪时执行器被唤醒、再次 poll。
- **`Send`**：类型可以安全地跨线程移动。`Rc`、`RefCell` 等「非线程安全」类型是 `!Send` 的。

### 2.2 GPUI 的单前台线程模型（回顾 u2-l1/u2-l2）

GPUI 的全部实体状态与 UI 渲染都发生在**单一前台线程**（通常就是主线程），`AppCell` 是 `RefCell<App>`，同一时刻只允许一个可变借用。这意味着：

- 任何想更新实体状态的异步代码，最终必须**回到前台线程**执行。
- 耗时计算绝不能放在前台，否则事件循环被卡住、界面冻结。

于是自然产生了两个问题：去哪里跑耗时计算（后台执行器）？算完怎么回去（前台执行器）？本讲就是回答这两个问题。

### 2.3 一个容易混淆的点：executor 不是 GPUI 发明的

`Task`、`Priority`、底层 `LocalExecutor` 等类型定义在工作区的 `scheduler` crate 里，gpui 在 `src/executor.rs` 顶部只是**重新导出**（这承接 u1-l3 的「反查定义文件」技能——在 gpui 源码里 grep `Task` 找到的都是用法，不是定义）。本讲只精读 gpui 这一侧的包装层与平台桥接层，这已经足以理解全部使用语义。

## 3. 本讲源码地图

| 文件 | 行数规模 | 作用 |
|---|---|---|
| `src/executor.rs` | 约 570 行 | `ForegroundExecutor` / `BackgroundExecutor` 两个公开执行器，`TaskExt` 扩展 trait，以及测试 |
| `src/platform_scheduler.rs` | 约 465 行 | `PlatformScheduler`：把 scheduler crate 的调度需求翻译成 `PlatformDispatcher` 调用；`PlatformClock`；一组验证线程语义的测试 |
| `src/platform.rs` | 约 3000 行（本讲只看其中一段） | `Platform` trait 与 `PlatformDispatcher` trait——平台差异的隔离层 |
| `src/profiler/journal.rs` | 约 2760 行（本讲只看一小段） | `profiler` feature 下的前台工作日志；本讲只精读其中 `ForegroundRunnableCounter` 计数器，完整机制留给 u7-l6 |
| `src/app.rs` | 节选 | `App` 如何持有两个执行器、`App::spawn` 的定义 |
| `src/app/context.rs` / `src/app/async_context.rs` | 节选 | `Context<T>::spawn`、`background_spawn` 在各上下文上的入口 |
| `examples/move_entity_between_windows.rs` | 155 行 | 最佳学习样本：timer + 前台任务 + `WeakEntity` 回写的完整闭环 |
| `examples/window.rs` | 节选 L251-L265 | 「3 秒后恢复应用」：timer 的最小真实用例 |

## 4. 核心概念与源码讲解

### 4.1 ForegroundExecutor：主线程上的前台执行器

#### 4.1.1 概念说明

`ForegroundExecutor` 是「跑在主线程上的执行器」。你在 GPUI 里写的绝大多数异步代码——`cx.spawn(...)`、`cx.spawn_in(...)`——最终都落到它身上。它有两个鲜明特征：

1. **不要求 `Send`**：future 永远只在主线程被轮询，所以闭包里可以放心使用 `Rc`、`RefCell`、`Entity<T>` 这些非线程安全类型，也可以随意借用 `&mut AsyncApp` 访问实体状态。
2. **它自己是 `!Send` 的**：结构体里放了一个 `PhantomData<Rc<()>>`，从类型系统层面禁止把这个执行器本身移动到其他线程。

#### 4.1.2 核心流程

```
cx.spawn(f)                          (App / Context<T> / AsyncApp)
  └─ ForegroundExecutor::spawn       把 future 装箱为 Box<dyn Future>（boxed_local，不要求 Send）
       └─ LocalExecutor 登记 future，返回 Task<R>
            └─ 需要再次轮询时，通过构造时注入的回调 schedule_local
                 └─ Scheduler::schedule_local        (platform_scheduler.rs)
                      └─ PlatformDispatcher::dispatch_on_main_thread(runnable, priority)
                           └─ 主线程事件循环空闲时 poll 该 future
```

注意「装箱」这一步的差异：前台用 `boxed_local()`，后台用 `boxed()`——前者不需要 vtable 的线程安全版本，是「不要求 Send」在实现上的直接体现。

另外，`schedule_local` 这一步在启用 `profiler` feature 时还会顺手递增一个「已入队未轮询」计数（见 4.4.3 的埋点讲解）——它只被观测链路读取，对上面的派发流程没有任何影响。

#### 4.1.3 源码精读

先看结构体定义，注意两个细节——`PhantomData` 与 `cfg(profiler)` 下的新字段：

[src/executor.rs:23-L30](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/executor.rs#L23-L30) 定义 `ForegroundExecutor`：内部持有一个 `scheduler::LocalExecutor` 和 `Arc<dyn PlatformDispatcher>`，用 `not_send: PhantomData<Rc<()>>` 把自己标记为 `!Send`——编译期保证它不会逃逸出主线程。第三个字段 `foreground_runnables` 是提交 1861e58f98 新增的：仅在编译开启 `profiler` feature 时存在，类型是 `Option<journal::ForegroundRunnableCounter>`，用于向前台工作日志报告「有多少个 runnable 已入队」（详见 4.4.3）。

再看 spawn 方法：

[src/executor.rs:347-L354](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/executor.rs#L347-L354) 是 `ForegroundExecutor::spawn`：签名只要求 `Future<Output = R> + 'static`，**没有 `Send` 约束**；实现上用 `boxed_local()` 装箱后交给内部 `LocalExecutor`。

[src/executor.rs:356-L368](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/executor.rs#L356-L368) 是 `spawn_with_priority`：注释明说前台任务**忽略优先级**，按提交顺序在主线程依次执行——主线程只有一条执行流，天然是 FIFO。

用户侧的入口在 `App` 上：

[src/app.rs:1936-L1952](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L1936-L1952) 定义 `App::spawn`：接受 `AsyncFnOnce(&mut AsyncApp) -> R`，先把 `self.to_async()` 克隆出一份 `AsyncApp`，再把「调用该异步闭包」这件事情作为一个 future 交给前台执行器。这就是你在闭包里能拿到 `cx: &mut AsyncApp` 的原因。

[src/app/context.rs:237-L245](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app/context.rs#L237-L245) 定义 `Context<T>::spawn`：与 `App::spawn` 的唯一差别是闭包多收一个参数 `WeakEntity<T>`（`this`）。u2-l3 讲过，这是异步回写实体状态的标准入口，弱引用避免任务把实体寿命无限延长。

还有一个值得注意的细节——应用退出后的保护：

[src/app.rs:1921-L1927](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L1921-L1927) 中 `App::foreground_executor()` 在 `quitting` 为真时直接 panic（`App::spawn` 里则是 `debug_panic!`）。对照 u2-l1 讲过的「`on_app_quit` 之后前台调度被关闸」，这里就是闸门本身：退出流程开始后不允许再排新的前台工作。

#### 4.1.4 代码实践：用编译器体会 Send 约束差异

1. **实践目标**：亲眼看到「前台 future 可以 `!Send`，后台不行」是编译期约束而非运行时约定。
2. **操作步骤**（建议在本地学习分支上改 `examples/window.rs`）：
   - 在任意按钮回调里加一段：
     ```rust
     // 示例代码：前台任务持有 Rc，可以编译
     cx.spawn(async move |_cx| {
         let counter = std::rc::Rc::new(std::cell::RefCell::new(0u32));
         *counter.borrow_mut() += 1;
     })
     .detach();
     ```
   - 再把 `cx.spawn` 改成 `cx.background_spawn`，保持 `Rc` 不动，执行 `cargo check -p gpui --example window`。
3. **需要观察的现象**：第一段通过编译；第二段报错，错误信息形如 `Rc<RefCell<u32>> cannot be sent between threads safely`（具体措辞「待本地验证」）。
4. **预期结果**：理解 `Send` 约束写在方法签名里（见 4.2.3），编译器在你越过主线程边界前就拦住你。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ForegroundExecutor` 要用 `PhantomData<Rc<()>>` 把自己标记为 `!Send`，而不是靠开发者自觉？

**答案**：它内部持有绑定主线程的 `LocalExecutor`（内部有非线程安全的任务队列），若被移动到其他线程再调用 `spawn`，任务会被错误线程轮询，产生数据竞争。用 `PhantomData<Rc<()>>` 让「把执行器传过线程」直接无法编译，把运行时隐患前移到编译期——这是 Rust API 设计的常用手法。

**练习 2**：`App::spawn` 的闭包参数是 `&mut AsyncApp`，为什么不能是 `&mut App`？

**答案**：`&mut App` 意味着独占借用要跨越 `await` 点存活，而主线程上其他更新随时需要借用 `App`，会形成借用冲突（u2-l1 讲过的重入 panic）。`AsyncApp` 持有 `Weak<AppCell>`，每次方法调用内部短暂 `borrow_mut`，两次调用之间借用被释放，其他工作得以插队。

**练习 3**：前台任务调用 `spawn_with_priority(Priority::High, ...)` 会发生什么？

**答案**：优先级被忽略，任务仍按提交顺序在主线程执行。见 [src/executor.rs:366](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/executor.rs#L366) 的注释「Priority is ignored for foreground tasks」。

### 4.2 BackgroundExecutor：平台线程池里的后台执行器

#### 4.2.1 概念说明

`BackgroundExecutor` 是「把 future 送离主线程」的通道：文件 IO、网络请求、大计算、定时器都该走它。它把工作交给 `PlatformDispatcher` 的平台实现——macOS 上是 Grand Central Dispatch（GCD），Linux/Windows 上是各自的线程池。因此它的所有 spawn 方法都要求 future 与返回值满足 `Send + 'static`：future 会被**移动**到另一个线程上去轮询。

它还提供三个「周边能力」，本讲后面都会用到：

- `timer(duration)`：返回一个在指定时长后就绪的 `Task<()>`；
- `now()`：读当前时间（测试时可被假时钟替换）；
- `spawn_dedicated(...)`：为一次会话开一根独占 OS 线程。

#### 4.2.2 核心流程

```
cx.background_spawn(fut)
  └─ BackgroundExecutor::spawn                      (executor.rs)
       └─ spawn_with_priority(Priority::default(), fut.boxed())
            └─ scheduler::BackgroundExecutor → Scheduler::schedule_background_with_priority
                 └─ PlatformScheduler::schedule_background_with_priority   (platform_scheduler.rs)
                      └─ PlatformDispatcher::dispatch(runnable, priority)  (platform.rs)
                           └─ 平台线程池某个工作线程上轮询该 future
```

`timer` 的链路则是：

```
cx.background_executor().timer(d)
  └─ BackgroundExecutor::timer                      (executor.rs)（时长为 0 直接返回 Task::ready）
       └─ Scheduler::timer                           (platform_scheduler.rs)
            └─ 构造 async_task Runnable + oneshot channel
                 └─ PlatformDispatcher::dispatch_after(d, runnable)  (platform.rs)
                      └─ 平台定时器到点 → runnable 执行 → oneshot 发送 () → Timer future 就绪
```

#### 4.2.3 源码精读

[src/executor.rs:15-L19](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/executor.rs#L15-L19) 定义 `BackgroundExecutor`：同样持有 `Arc<dyn PlatformDispatcher>`，但没有 `PhantomData`——它是 `Clone` 且线程安全的，可以随意复制到任何地方使用。

[src/executor.rs:112-L119](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/executor.rs#L112-L119) 是 `BackgroundExecutor::spawn`：签名 `future: impl Future<Output = R> + Send + 'static` 且 `R: Send + 'static`——**这就是 Send 约束的源头**。实现上先 `boxed()` 再按默认优先级派发。

[src/executor.rs:121-L139](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/executor.rs#L121-L139) 是 `spawn_with_priority`：注意特例——`Priority::RealtimeAudio` 会走 `spawn_realtime`，在带实时调度优先级的专属线程上运行（音频处理需要确定性延迟）。

[src/executor.rs:183-L192](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/executor.rs#L183-L192) 是 `timer`：时长为零时直接返回 `Task::ready(())`（不产生任何调度）；否则把调度器提供的 `Timer` future 再 spawn 成后台任务。文档提醒：受其他任务影响，实际经过的时间**可能**比请求的长。

[src/executor.rs:91-L110](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/executor.rs#L91-L110) 是 `spawn_dedicated`：为闭包开一根新的专属 OS 线程（持有独立的 `LocalExecutor`）。文档注释解释了动机——共享后台线程的栈可能很小（macOS GCD 工作线程固定 512 KiB），深递归或大栈 future 应该用满 2 MiB 标准栈的专属线程。注意约束的巧妙之处：future 本身可以 `!Send`（独占线程内可以用 `Rc`），但**返回值**必须 `Send + Sync`（要送回发起线程）。

用户侧入口（与 4.1.3 的 `spawn` 对照）：

[src/app/context.rs:856-L863](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app/context.rs#L856-L863) 与 [src/app/async_context.rs:125-L131](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app/async_context.rs#L125-L131) 分别在 `Context<T>` 与 `AsyncApp` 上定义 `background_spawn`：实现都只有一行——`self.app.background_executor.spawn(future)`（后者经由 `self.background_executor`）。**`background_spawn` 不给你 `AsyncApp`**，因为后台线程根本不允许碰实体状态。

装配发生在 `App` 创建时：

[src/app.rs:779-L792](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L779-L792) 中 `App::new_app` 从 `Rc<dyn Platform>` 取出两个执行器存入 `App`，并断言 `background_executor.is_main_thread()`——App 必须在主线程构造（呼应 u2-l1）。同一段里还能看到 `#[cfg(feature = "profiler")]` 下调用 `install_foreground_journal()` 安装前台日志（见 4.4.3）。

一个真实用例（timer 的最小闭环）：

[src/examples/window.rs:251-L265](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/window.rs#L251-L265) 「Hide Application」按钮：先 `cx.hide()` 隐藏应用，然后 `window.spawn` 起一个前台任务，`await` 一个 3 秒 timer，再 `cx.activate(false)` 恢复应用，最后 `detach()` 让它自生自灭。这个 8 行片段浓缩了本讲全部概念：前台任务、后台 timer、await 串联、detach。

#### 4.2.4 代码实践：阅读 timer 驱动的真实闭环

1. **实践目标**：读懂一个「定时器 → 前台回写」的完整生产级循环。
2. **操作步骤**：
   - 打开 [examples/move_entity_between_windows.rs:35-L51](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/move_entity_between_windows.rs#L35-L51)。
   - 运行：`cargo run -p gpui --example move_entity_between_windows`。
   - 对照下面的调用链标注每一行：`cx.spawn_in` 把谁递给了闭包？`cx.background_executor().timer(...)` 在哪个线程计时？`this.update_in` 如何回到实体所在的窗口？
3. **需要观察的现象**：终端每秒打印一行 `tick #N fired in entity's current window ...`；点击按钮把实体搬进新窗口后，tick 打印的窗口编号随之改变。
4. **预期结果**：任务引用被存进 `_tasks: Vec<Task<()>>` 字段（[examples/move_entity_between_windows.rs:25](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/move_entity_between_windows.rs#L25)），实体存活期间循环不会被取消——这就是「存字段以延长任务寿命」的官方推荐写法（CLAUDE.md 中的约定）。

#### 4.2.5 小练习与答案

**练习 1**：`cx.background_spawn(async move { /* 这里能用 Entity<T> 吗 */ })` 里能否直接使用 `Entity<T>` 句柄？

**答案**：语法上 `Entity<T>` 本身是 `Send` 的（它只是 id 加类型标签，u2-l2），可以捕获进后台 future；但**没有任何上下文**可供 `read`/`update`——`background_spawn` 的闭包拿不到 `AsyncApp`。想在后台用数据，应在前台先 `entity.read(cx)` 把需要的值克隆出来带进去；算完再经前台任务回写。

**练习 2**：为什么 `BackgroundExecutor::timer(0)` 不派发任何任务？

**答案**：见 [src/executor.rs:188-L190](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/executor.rs#L188-L190)：零时长定时器语义上立即就绪，直接返回 `Task::ready(())`，避免一次无意义的「派发到平台 → 平台立刻唤醒」往返。

**练习 3**：你要在后台跑一个需要 8 MiB 栈的深递归解析器，该用 `spawn` 还是 `spawn_dedicated`？

**答案**：`spawn_dedicated`。`spawn` 的 future 由平台线程池轮询，栈大小不受你控制（macOS GCD 工作线程仅 512 KiB）；`spawn_dedicated` 为该会话开专属 OS 线程，享有标准库默认 2 MiB 栈，且独占线程内还允许 future 使用 `Rc` 等 `!Send` 类型（返回值仍需 `Send + Sync`）。见 [src/executor.rs:91-L110](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/executor.rs#L91-L110) 的文档注释。

### 4.3 Task：可等待、可分离、可取消的 future 句柄

#### 4.3.1 概念说明

`spawn` 的返回值 `Task<R>` 是一个**可 await 的取消凭证**。它是 future（`task.await` 拿结果），同时握着底层任务的生死：Drop 它就取消任务。GPUI 官方约定（CLAUDE.md）把任务的归宿概括为三条路：

1. **`await`**：在别的异步上下文里等它完成（例如 `scoped` 里逐个 `task.await`）。
2. **`detach()`**：与凭证脱钩，任务继续跑到自然结束——适合「发射后不管」。
3. **存进字段**：任务寿命绑定到某个实体/结构体，后者 Drop 时任务随之取消——适合「组件没了工作也该停」。

对 `Task<Result<T, E>>` 还有第四条捷径：`detach_and_log_err(cx)`，分离的同时把错误交给日志。

#### 4.3.2 核心流程

```
Task<R> 的生命周期：

  spawn 返回 Task<R>
    ├─ .await ────────── 挂起当前 future 直到任务完成，取出 R
    ├─ .detach() ─────── 凭证失效，任务后台自走，R 被丢弃
    ├─ 存入结构体字段 ── 结构体 Drop → Task Drop → 取消
    └─ 自然离开作用域 ── Drop → 取消

取消的精确语义（协作式）：
  Task 被 Drop
    → 底层 future 被丢弃
    → 若 future 正停在某个 await 点：后续代码永不执行
    → 若 future 正在被 poll（一段不含 await 的长计算）：本次 poll 会跑完，之后不再被轮询
```

关键认知：**取消发生在轮询边界，不能抢占正在执行的同步代码**。一个不含内部 `await` 的长计算（如朴素递归的斐波那契）一旦开始 poll 就会算完，只是结果随凭证一起被丢弃。

#### 4.3.3 源码精读

`Task` 类型本身来自 `scheduler` crate，gpui 重新导出：

[src/executor.rs:9-L11](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/executor.rs#L9-L11) 把 `Task`、`FallibleTask`、`Priority`、`DedicatedExecutor` 等从 `scheduler` crate 引入 gpui 命名空间——`Task` 的定义不在 gpui 源码内，但它的行为由 gpui 自己的测试约束。

取消语义有测试为证：

[src/platform_scheduler.rs:302-L350](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform_scheduler.rs#L302-L350) 测试 `spawn_dedicated_dropping_task_cancels_future`：future 先发信号宣布自己已启动，然后 `futures::future::pending::<()>().await` 永久挂起；测试随后 `drop(task)`，断言 await 点之后的代码**从未执行**（两个通道都收不到信号）。这就是「Drop 即取消、取消在 await 边界生效」的可执行证明。

`detach` 的真实用法：

[src/executor.rs:48-L54](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/executor.rs#L48-L54) 是 `TaskExt::detach_and_log_err` 的实现：把「记录错误」这一步包装成新的 future，再 `spawn` 到**前台**执行器并 `detach()`。注意 `#[track_caller]`——它捕获调用位置，让日志能指向任务诞生的源码行。

[src/examples/testing.rs:449-L455](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/testing.rs#L449-L455) 展示最朴素的后台发射模式：`cx.background_spawn({ let client = self.client.clone(); async move { client.send(delta); } }).detach();`——先克隆需要的数据，后台发送，凭证立即 detach。这里必须先 `clone`：`self.client` 是借来的，`async move` 块要 `'static`。

gpui 自己的单测里还能看到任务最基本的运行验证：

[src/executor.rs:548-L571](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/executor.rs#L548-L571) 测试 `sanity_test_tasks_run`：前台 spawn 一个改写 `Rc<RefCell<bool>>` 的任务并 detach，`run_until_parked` 推进调度后断言任务确实执行了。注意任务体里用的正是 `Rc`——前台任务 `!Send` 也无妨的活证据。

#### 4.3.4 代码实践：见证 Drop 取消

1. **实践目标**：亲手验证「丢弃 Task 会取消 future，且取消发生在 await 点」。
2. **操作步骤**：
   - 阅读 [src/platform_scheduler.rs:302-L350](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform_scheduler.rs#L302-L350) 的测试，画出时间线：`started` 信号 → `pending().await` 挂起 → `drop(task)` → 断言 `after_park` 信号永不出现。
   - 运行：`cargo test -p gpui --lib -- platform_scheduler`（测试名过滤方式「待本地验证」）。
3. **需要观察的现象**：该测试通过——说明 Drop 之后 future 在 `await` 点被取消、后续代码没有执行。做对比实验时要注意：单纯注释掉 `drop(task)` 并不能让断言失败（future 挂在 `pending()` 上，`after_park_rx.recv_timeout(...)` 依旧超时返回 `Err`），更有效的改法是把 `pending().await` 换成带超时的等待（例如 `futures::future::select` + `timer`），让「任务存活」与「任务被取消」产生可区分的信号；改造后的具体现象「待本地验证」。
4. **预期结果**：建立肌肉记忆——`Task` 不是「线程句柄」而是「取消凭证」；想要任务活着，要么 await、要么 detach、要么存起来。

#### 4.3.5 小练习与答案

**练习 1**：`cx.spawn(...)` 的返回值没有被绑定也没有 detach，这个任务会怎样？

**答案**：`Task` 在语句结束时被 Drop，任务随即取消——future 里尚未执行的部分永远不会跑。这是新手最常见的「我的异步代码没执行」陷阱。三个修法：`.detach()`、存入 `_tasks` 字段、或在另一个 future 里 `await` 它。

**练习 2**：`detach()` 与存字段，分别适合什么场景？

**答案**：`detach` 适合与界面状态无关的发射后不管工作（如打点上报），代价是无人能再取消它，且任务可能引用全局资源直到结束。存字段适合「任务结果服务于该实体 / 工作应随实体消亡而停止」的场景，如示例中把 tick 循环存进 `_tasks: Vec<Task<()>>`，实体析构时循环自动停止。

**练习 3**：为什么 `detach_and_log_err` 要把错误处理任务 spawn 到**前台**执行器而不是后台？

**答案**：见 [src/executor.rs:51-L53](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/executor.rs#L51-L53)：日志写入通常涉及共享的日志器状态，且 `log_tracked_err` 链路与前台基础设施协作；前台执行器保证按序执行且无需 `Send`。同时它借 `#[track_caller]` 把调用点位置带进日志，便于排查。

### 4.4 PlatformDispatcher 与 PlatformScheduler：平台调度器如何驱动 futures

#### 4.4.1 概念说明

前三个模块一直出现 `Arc<dyn PlatformDispatcher>`，现在是揭开它的时候。GPUI 要跨 macOS / Linux / Windows / Web 运行，而每个平台的「把一段工作安排到主线程 / 后台线程 / 定时器」原语完全不同（GCD、线程池、`setTimeout`……）。`PlatformDispatcher` 就是隔离这些差异的窄接口：

- `dispatch_on_main_thread`：安排到主线程；
- `dispatch`：安排到后台线程池（带优先级）；
- `dispatch_after`：定时器；
- `spawn_realtime`：实时优先级线程；
- `is_main_thread` / `now`：线程与时间的查询。

`PlatformScheduler` 则是 gpui 与 scheduler crate 之间的**适配器**：scheduler crate 的执行器只会喊「我要 `schedule_local` / `schedule_background_with_priority` / 要一个 `timer`」，`PlatformScheduler` 负责把这些喊话翻译成对 `PlatformDispatcher` 的调用。平台实现本身（gpui_macos、gpui_linux 等）在其他 crate 里（u1-l1 讲过的门面结构），本讲只见接口不见实现。

#### 4.4.2 核心流程

```
scheduler crate 执行器          PlatformScheduler(适配器)           平台实现 crate
─────────────────────          ──────────────────────           ─────────────────
LocalExecutor ──schedule_local──▶ dispatch_on_main_thread ───────▶ GCD main queue /
                                 （profiler 下先递增入队计数）       事件循环唤醒
BackgroundExecutor
 ──schedule_background_with_...─▶ dispatch(runnable, priority) ─▶ GCD work queue /
                                                                  线程池
timer 请求 ────────────────────▶ dispatch_after(duration, ...) ─▶ 平台定时器
block(同步阻塞) ───────────────▶ Parker + waker_fn 本地实现（不依赖平台）
```

组装关系（自底向上）：

```
平台实现提供 Arc<dyn PlatformDispatcher>
  → BackgroundExecutor::new / ForegroundExecutor::new 包装它（内部各建一个 PlatformScheduler）
    → Platform::background_executor() / foreground_executor() 暴露给框架
      → App::new_app 取出并存入字段（app.rs:784-785；profiler 下还安装前台日志）
        → 各上下文经 background_spawn / spawn 触达用户代码
```

#### 4.4.3 源码精读

先看平台契约：

[src/platform.rs:1029-L1033](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L1029-L1033) 定义 `PlatformDispatcher` trait 的四个核心方法：`is_main_thread`、`dispatch`、`dispatch_on_main_thread`、`dispatch_after`。注释明确说它「public 是为了测试宏，但不应视为公开 API」——这是内部契约。

[src/platform.rs:1035-L1056](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L1035-L1056) 是几个带默认实现的方法：`dispatch_on_main_thread_when_idle` 默认退化为「低优先级主线程派发」（支持空闲调度的平台可覆写，对应 `ForegroundExecutor::spawn_when_idle`）；`now()` 默认 `Instant::now()`；`increase_timer_resolution` 返回空 guard（Windows 平台可覆写，见下文 `block` 里的用法）。

而 `Platform` trait 对外只暴露两个工厂：

[src/platform.rs:127-L128](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L127-L128) `Platform::background_executor` / `foreground_executor`——平台实现负责构造绑定自己调度原语的执行器。

再看适配器：

[src/platform_scheduler.rs:27-L33](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform_scheduler.rs#L27-L33) 定义 `PlatformScheduler`：除了 `dispatcher`、`clock`、`next_session_id` 三个老字段，还多了一个 `#[cfg(feature = "profiler")]` 的 `foreground_runnables: ForegroundRunnableCounter`（L31-L32）——就是前文反复出现的那个计数器，宿主在这里。

[src/platform_scheduler.rs:46-L54](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform_scheduler.rs#L46-L54) 是 `foreground_executor` 工厂：为每个前台执行器分配一个 `SessionId`，并注入「需要调度时回调 `schedule_local`」的闭包（持有 `Weak` 引用避免环）。

[src/platform_scheduler.rs:118-L131](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform_scheduler.rs#L118-L131) 是两个翻译函数本体：`schedule_local` → `dispatch_on_main_thread`；`schedule_background_with_priority` → `dispatch`。注意 `schedule_local` 开头多出的两行 `#[cfg(feature = "profiler")] self.foreground_runnables.queued();`——每次把 runnable 排向主线程时先递增计数（见下文「可观测性埋点」）。

[src/platform_scheduler.rs:137-L160](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform_scheduler.rs#L137-L160) 实现 `timer`：用 `async_task::Builder` 造一个 runnable，其 schedule 回调把 runnable 交给 `dispatch_after(duration, ...)`；runnable 执行时往 oneshot channel 发 `()`，`Timer` future 因收到该信号而就绪。计时由此完全委托给平台定时器。

[src/platform_scheduler.rs:69-L116](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform_scheduler.rs#L69-L116) 实现 `block`（同步阻塞等 future）：用 `waker_fn` 把「唤醒」接到 `parking` crate 的 unparker 上，循环「poll → park 直到被唤醒或超时」。注意 L96-L101 的细节：在 Windows 上等超时时先调 `increase_timer_resolution()`——因为 Windows 默认定时器精度约 15.6 毫秒，不提高精度短超时会严重失真。这是平台差异渗入调度层的绝佳例子。

[src/platform_scheduler.rs:186-L199](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform_scheduler.rs#L186-L199) 是 `PlatformClock`：`now()` 委托给 `dispatcher.now()` 而不是直接 `Instant::now()`——这让测试平台可以注入假时钟（对应 `BackgroundExecutor::now()` 的文档「允许测试使用假计时器」）。

**可观测性埋点：ForegroundRunnableCounter（profiler feature）**

提交 1861e58f98（gpui: Journal foreground work between frames and report hang incidents）在这条调度链上插入了一组纯观测性的计数点。它们全部包在 `#[cfg(feature = "profiler")]` 里，默认构建（不开该 feature）连字段都不存在，调度语义分毫未动：

- [src/profiler/journal.rs:357-L378](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/profiler/journal.rs#L357-L378) 定义 `ForegroundRunnableCounter`：本质就是一个 `Arc<AtomicUsize>`。`queued()` 原子加一；`finished()` 原子减一（用 `fetch_update` + `checked_sub`，减到 0 不会下溢）；`has_runnables()` 询问队列里是否还有未完成的工作。
- 入队侧有两处调用 `queued()`：一是 [src/platform_scheduler.rs:118-L123](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform_scheduler.rs#L118-L123) 的 `schedule_local`（覆盖普通前台任务）；二是 [src/executor.rs:379-L399](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/executor.rs#L379-L399) 的 `spawn_when_idle`——它绕过 `schedule_local`、直接走 `dispatch_on_main_thread_when_idle`，所以要自己在派发闭包里补一次计数（L389-L396）。
- 计数器的所有权链：`PlatformScheduler` 构造时从线程局部存储取一份（[src/platform_scheduler.rs:36-L44](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform_scheduler.rs#L36-L44)），`ForegroundExecutor::new` 再经访问器 [src/platform_scheduler.rs:60-L65](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform_scheduler.rs#L60-L65) 克隆出 `Option<ForegroundRunnableCounter>` 存进执行器（[src/executor.rs:296-L345](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/executor.rs#L296-L345)）。注意 L333-L336 的注释：测试构建下计数器是 `None`——确定性测试调度器不会触发 GPUI 的任务 profiler 钩子，若照常递增就会出现「只加不减」的脏计数。
- 消费侧在 `App`：[src/app.rs:690-L693](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L690-L693) 给 `App` 增加了 `foreground_journal` 字段，[src/app.rs:790-L791](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L790-L791) 在 `new_app` 里安装，[src/app.rs:1929-L1934](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L1929-L1934) 暴露 `App::foreground_journal()` 访问器（同一前台线程上的多个 App 共享一条日志流）。

这组计数回答的问题是：「主线程此刻是真的空闲，还是只是暂时没有 runnable 排队？」前台日志（ForegroundJournal）据此把连续的事件流切分成一段段「活动区间」，并把低于 100µs（`TASK_POLL_FLOOR`）的零星任务轮询折叠成聚合计数而不是逐条记录——这正是本讲开头那个 `schedule_local` 里两行 `cfg` 代码的存在意义。日志如何被 HangDetector 消费、如何产出卡顿报告，是 u7-l6 的主题，本讲只需记住：**调度链上多了一双「只看不动手」的眼睛**。

#### 4.4.4 代码实践：画一条完整的派发链

1. **实践目标**：把本讲四条链路（前台派发、后台派发、定时器、阻塞等待）各自整理成「方法 → 方法 → 平台原语」的清单。
2. **操作步骤**：
   - 从 [src/app/context.rs:856-L863](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app/context.rs#L856-L863) 的 `background_spawn` 出发，手动展开到 `PlatformDispatcher::dispatch`，把途经的每个函数与行号记下来。
   - 再从 `App::spawn`（[src/app.rs:1936-L1952](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L1936-L1952)）展开到 `dispatch_on_main_thread`。
   - 用 `grep -n "dispatch_on_main_thread" src/platform.rs` 之类验证清单（源码阅读型实践，无需运行）。
3. **需要观察的现象**：两条链在中途分叉（`schedule_local` vs `schedule_background_with_priority`），最终汇合于同一个 `Arc<dyn PlatformDispatcher>`；前台那条链路上还能标出 `queued()` 计数点。
4. **预期结果**：得到一张可对照源码的派发链图；后续阅读任何 `cx.spawn` 代码都能立刻说出「这段代码将在哪个线程、被谁轮询」。

#### 4.4.5 小练习与答案

**练习 1**：`PlatformScheduler` 为什么持有 `Arc<dyn PlatformDispatcher>`，而 `foreground_executor` 工厂里却只给闭包一个 `Weak`？

**答案**：见 [src/platform_scheduler.rs:48-L53](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform_scheduler.rs#L48-L53)：`PlatformScheduler` 自身生命周期与平台一致，强引用没问题；但注入给 `LocalExecutor` 的调度闭包可能活得比 scheduler 更久（执行器被移动、scheduler 已析构），用 `Weak` + `upgrade()` 失败时静默丢弃，避免闭包反向吊住 scheduler 的寿命（u2-l2 讲过的强引用成环问题）。

**练习 2**：如果一个新的操作系统平台要接入 GPUI，最少要实现 `PlatformDispatcher` 的哪些方法？

**答案**：必须实现无默认体的四个：`is_main_thread`、`dispatch`、`dispatch_on_main_thread`、`dispatch_after`（[src/platform.rs:1030-L1033](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L1030-L1033)），外加 `spawn_realtime`（[src/platform.rs:1048](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L1048)，无默认体）。其余（`now`、`increase_timer_resolution`、`dispatch_on_main_thread_when_idle`、`idle_time_remaining`）都有可用的默认实现。这个短清单正说明该 trait 是精心收敛过的「平台最小调度契约」。

**练习 3**：测试平台上 `cx.background_executor().timer(...)` 会真的等一秒吗？

**答案**：不会。测试分发器携带假时钟：`BackgroundExecutor::advance_clock(duration)` 直接把时间拨 forward 使 timer 就绪（[src/executor.rs:200-L204](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/executor.rs#L200-L204)），`run_until_parked` 在无任务可跑时也会自动推进到下一个 timer。这就是 u1-l4 提过的 `run_until_parked` 测试范式的底层支撑，也是 `PlatformClock` 把 `now()` 委托给 dispatcher 的原因。

**练习 4**：为什么 `ForegroundRunnableCounter` 在测试构建下被置为 `None`，而不是照样计数？

**答案**：见 [src/executor.rs:333-L336](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/executor.rs#L333-L336) 的注释：计数器的增减必须成对——入队时 `queued()` 加一，轮询结束由 profiler 钩子 `finished()` 减一。确定性测试调度器不调用这些钩子，若照常递增，计数会只涨不降，`has_runnables()` 永远为真，前台日志会把「空闲」误判成「忙碌」。置 `None` 是让观测链路在无法自洽的环境里干脆闭嘴。

## 5. 综合实践

把本讲全部知识串成一个完整示例：**后台计算斐波那契数列第 40 项 + 定时器模拟耗时 + 回前台刷新界面 + 验证 Drop 取消**。

朴素递归斐波那契满足：

\[ F(n) = F(n-1) + F(n-2),\quad F(0)=0,\ F(1)=1 \]

其调用次数随 \( n \) 指数增长（约 \( O(\phi^n) \)，其中 \(\phi = \frac{1+\sqrt{5}}{2} \approx 1.618\)），\( F(40) \) 大约需要上亿次调用——正好是「必须放后台」的典型负载。

### 5.1 创建示例

在 gpui 仓库的学习分支上新建 `crates/gpui/examples/async_fib.rs`（示例代码，仿照 `move_entity_between_windows.rs` 的骨架）：

```rust
// 示例代码：crates/gpui/examples/async_fib.rs
//! To run: cargo run -p gpui --example async_fib
#![cfg_attr(target_family = "wasm", no_main)]

use std::time::Duration;

use gpui::{
    App, AppContext as _, Bounds, Context, Render, SharedString, Task, Window, WindowBounds,
    WindowOptions, div, prelude::*, px, rgb, size,
};
use gpui_platform::application;

/// 朴素递归：刻意保持指数复杂度，制造真实的后台负载。
fn fib(n: u64) -> u64 {
    if n < 2 {
        n
    } else {
        fib(n - 1) + fib(n - 2)
    }
}

struct FibApp {
    status: SharedString,
    started: bool,
    _task: Option<Task<()>>, // 任务凭证存字段：实体活着，任务才活着
}

impl FibApp {
    fn new(_window: &mut Window, _cx: &mut Context<Self>) -> Self {
        Self {
            status: "idle（点击 Start 开始计算）".into(),
            started: false,
            _task: None,
        }
    }

    fn start(&mut self, cx: &mut Context<Self>) {
        if self.started {
            return;
        }
        self.started = true;
        // 前台任务：闭包拿到 WeakEntity<FibApp> 与 &mut AsyncApp
        let task = cx.spawn(async move |this, cx| {
            this.update(cx, |this, cx| {
                this.status = "computing...（后台计算中）".into();
                cx.notify();
            })
            .ok();

            // ① 后台线程池里计算 F(40)：返回值 u64 满足 Send
            let result = cx.background_spawn(async move { fib(40) }).await;

            // ② timer 模拟额外的 IO 耗时（平台定时器，不占线程）
            cx.background_executor()
                .timer(Duration::from_secs(1))
                .await;

            // ③ 回到前台（本任务本就在前台）回写实体状态
            this.update(cx, |this, cx| {
                this.status = format!("fib(40) = {result}").into();
                this.started = false;
                cx.notify();
            })
            .ok();
        });
        self._task = Some(task);
    }

    fn cancel(&mut self, _cx: &mut Context<Self>) {
        // 丢弃 Task 凭证 → 取消前台任务 → 其内部 await 的后台任务一并被丢弃
        self._task = None;
        self.status = "cancelled（任务已取消）".into();
        self.started = false;
    }
}

impl Render for FibApp {
    fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        div()
            .flex()
            .flex_col()
            .gap_3()
            .size(px(400.0))
            .justify_center()
            .items_center()
            .bg(rgb(0x333333))
            .text_color(rgb(0xffffff))
            .child(self.status.clone())
            .child(
                div()
                    .px_4()
                    .py_2()
                    .rounded_md()
                    .bg(rgb(0x4040ff))
                    .child("Start")
                    .on_mouse_down(gpui::MouseButton::Left, cx.listener(|this, _, _, cx| this.start(cx))),
            )
            .child(
                div()
                    .px_4()
                    .py_2()
                    .rounded_md()
                    .bg(rgb(0xa04040))
                    .child("Cancel")
                    .on_mouse_down(gpui::MouseButton::Left, cx.listener(|this, _, _, cx| this.cancel(cx))),
            )
    }
}

fn run_example() {
    application().run(|cx: &mut App| {
        let bounds = Bounds::centered(None, size(px(400.0), px(400.0)), cx);
        cx.open_window(
            WindowOptions {
                window_bounds: Some(WindowBounds::Windowed(bounds)),
                ..Default::default()
            },
            |window, cx| cx.new(|cx| FibApp::new(window, cx)),
        )
        .unwrap();
        cx.activate(true);
    });
}

#[cfg(not(target_family = "wasm"))]
fn main() {
    run_example();
}

#[cfg(target_family = "wasm")]
#[wasm_bindgen::prelude::wasm_bindgen(start)]
pub fn start() {
    gpui_platform::web_init();
    run_example();
}
```

再在 `crates/gpui/Cargo.toml` 追加声明（参照现有条目格式，见 [Cargo.toml:181-L184](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/Cargo.toml#L181-L184) 的 `move_entity_between_windows`）：

```toml
[[example]]
name = "async_fib"
path = "examples/async_fib.rs"
```

### 5.2 运行与观察

1. **实践目标**：验证双执行器协作、前台回写、Drop 取消三件事。
2. **操作步骤**：
   - `cargo run -p gpui --example async_fib`（Linux 需要 `WAYLAND_DISPLAY` 或 `DISPLAY` 可用，u1-l2 讲过）。
   - 点击 **Start**：界面立刻显示 computing...，主线程不卡（窗口可以继续拖动、按钮仍可点击）。
   - 等待约 1~3 秒后显示 `fib(40) = 102334155`。
   - 再次点击 **Start**，并在一秒内点击 **Cancel**。
3. **需要观察的现象**：
   - Start 后 UI 立即响应且不冻结——计算在后台线程池。
   - 正常路径结果出现——后台结果经 `await` 回到前台任务、再 `this.update` 回写实体并 `cx.notify()` 触发重绘。
   - Cancel 后结果**永不出现**——`self._task = None` 丢弃凭证，前台任务在某个 `await` 点被取消，界面停留在 cancelled。
   - 进阶观察：在 `background_spawn` 闭包末尾加一行 `eprintln!("background done");` 再做 Cancel 实验。你会看到这行日志**仍然可能打印**——因为朴素 `fib` 是一次不含 `await` 的长 poll，正在执行的 poll 无法被抢占，只是结果被丢弃。这正印证 4.3.2 的「取消发生在轮询边界」。
4. **预期结果**：界面行为如上；具体耗时因机器而异（「待本地验证」）。

### 5.3 思考题

如果把这个示例里的 `cx.spawn(...)` 换成 `cx.background_spawn(...)` 包住整个流程，会出什么问题？

<details>
<summary>参考答案</summary>

两个层面：其一，`background_spawn` 的闭包拿不到 `&mut AsyncApp`，也没有 `WeakEntity` 传入——后台任务没有任何合法途径更新实体状态，`this.update`、`cx.background_executor()` 都无从谈起；其二，即便把实体句柄克隆进去，`Entity::update` 需要 `App` 上下文，而后台线程不允许碰 `RefCell<App>`。正确结构永远是「前台任务做骨架，重活 `background_spawn` 出去，结果 await 回来」。
</details>

## 6. 本讲小结

- GPUI 的并发是**双执行器**架构：`ForegroundExecutor` 只在主线程轮询、不要求 `Send`、忽略优先级；`BackgroundExecutor` 把 `Send + 'static` 的 future 送进平台线程池，`Send` 约束直接写在 [spawn 签名](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/executor.rs#L112-L119)里。
- `cx.spawn` 与 `cx.background_spawn` 是用户侧两条派发路径：前者给闭包 `&mut AsyncApp`（`Context<T>::spawn` 还额外给 `WeakEntity<T>`），后者什么都不给——后台线程不允许触碰实体状态。
- `Task` 是可 await 的**取消凭证**：Drop 即取消，且取消发生在轮询边界、不能抢占正在执行的同步计算；想让任务活下去，就 `await`、`detach()`（错误用 `detach_and_log_err`）或存进字段绑定实体寿命。
- `timer` 不占线程，完全委托平台定时器（`dispatch_after`），零时长直接 `Task::ready`；测试平台上由假时钟驱动。
- `PlatformDispatcher` 是约六个方法的平台最小调度契约，`PlatformScheduler` 把 scheduler crate 的调用翻译过去；`block` 里 Windows 定时器精度补偿（`increase_timer_resolution`）展示了平台差异被隔离在这一层的价值。
- 提交 1861e58f98 在这条调度链上插入了 `profiler` feature 下的 `ForegroundRunnableCounter` 埋点（`schedule_local` 与 `spawn_when_idle` 入队时 `queued()`，轮询结束 `finished()`）：它向前台工作日志回答「主线程是否真的空闲」，不改变任何调度语义；测试构建下置 `None` 以免计数只增不减。
- 执行器在 `App::new_app` 时从 `Platform` 取出并存入 `App`；应用进入 quitting 后 `App::foreground_executor()` 会 panic，前台调度被关闸（呼应 u2-l1 的退出链）。

## 7. 下一步学习建议

本讲结束后，你已经集齐了「状态 + 上下文 + 并发」三块基石，下一单元（u3）将进入声明式 UI：建议先学 **u3-l1（Render 与视图）**，看 `cx.notify()` 如何让修改过的实体在下一帧重绘——你会重新理解本讲实践中 `cx.notify()` 那一行到底触发了什么。

继续深挖并发可阅读：

- [src/app.rs:2635-L2656](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L2635-L2656) 的 `fetch_asset`——`.shared()` 让多个等待者共享同一个后台任务（旧名 `load_asset`，现已更名为 `fetch_asset` 并返回 `(Shared<Task>, bool)`）。
- [src/executor.rs:141-L173](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/executor.rs#L141-L173) 的 `scoped` / `scoped_priority`——用通道 + `Drop` 实现的「等待一组后台任务全部完成」作用域原语。
- 本讲 4.4.3 提到的 `ForegroundRunnableCounter` 只是前台工作日志的计数入口：想在运行时读到任务轮询、窗口绘制、帧呈现的完整事件流并理解卡顿（hang）如何被判定，请直接进入 **u7-l6（前台工作日志与卡顿检测）**——`ForegroundJournal` 的环形缓冲、`TASK_POLL_FLOOR` 折叠与 `HangDetector` 都在那里展开。
- u2-l4 讲过的 `Effect` 队列与本讲 `Task` 的关系：任务里 `this.update` 产生的 `Notify` 效果同样在更新收尾的 `flush_effects` 中派发。
