# u4-l2 PlatformDispatcher 契约：跨线程投递 runnable 的统一接口

## 1. 本讲目标

上一讲（u4-l1）我们站在「使用者」视角看了 GPUI 的前台/后台执行器：`cx.spawn` 跑主线程、`cx.background_spawn` 跑后台线程。但有一个问题被刻意跳过了——**执行器本身是平台无关的，它凭什么知道怎么把任务送进某个线程？** macOS 要走 Grand Central Dispatch，Linux 要唤醒 calloop 事件循环，浏览器只能排队到主线程邮箱。把这些差异藏起来的那一层契约，就是本讲的主角 `PlatformDispatcher`。

学完本讲，你应该能够：

1. 列出 `PlatformDispatcher` 需要实现的核心方法（5 个必需 + 6 个默认），并说出每个方法的职责。
2. 解释为什么这个 trait 要求 `Send + Sync`，而 `Platform` trait 本身不是 `Send`。
3. 完整描述一个 runnable 从后台线程到主线程的旅程（包括定时器任务与 `!Send` future 的安全协议）。
4. 读懂 `ThreadedDispatcher` 这个可运行的通用参考实现，并能动手写测试验证「任务确实在前台线程执行」。
5. 说清 `Priority` 的加权随机调度语义，以及测试体系里「tick」一词的确切含义。

## 2. 前置知识

本讲假设你已学完 u4-l1（单前台线程模型、`spawn`/`background_spawn` 的分工）。在此之上，需要几个 Rust 语言层面的概念：

- **trait object 与动态分发**：`dyn Trait` 是编译期未知具体类型、运行期通过虚表调用的值。`Box<dyn Trait>`、`Rc<dyn Trait>`、`Arc<dyn Trait>` 都是「契约指针」——调用方只依赖方法签名，不依赖实现者。
- **`Send` 与 `Sync`**：两个自动 trait（auto trait）。`Send` 表示值可以安全地**移动**到另一个线程；`Sync` 表示值可以安全地被多个线程**共享引用**（`&T` 跨线程）。`Arc<T>` 只有在 `T: Send + Sync` 时自己才是 `Send + Sync`；而 `Rc<T>` 永远不是 `Send`。
- **`Arc` vs `Rc`**：引用计数智能指针。`Rc` 非线程安全但开销小；`Arc` 用原子操作计数，可跨线程共享。GPUI 用 `Rc<dyn Platform>` 表达「只在主线程用的平台对象」，用 `Arc<dyn PlatformDispatcher>` 表达「会被多个线程同时碰的调度器」。
- **`async_task::Runnable` 模型**：`async-task` 库的核心套路是 `spawn(future, schedule_fn, metadata) -> (Runnable, Task)`。`Runnable` 是任务的「可执行信封」——调用 `runnable.run()` 就 poll 一次里面的 future；调用 `runnable.schedule()` 则触发 `schedule_fn`，把信封交给某个执行队列。future 内部 `await` 被唤醒时，waker 最终也是再次调用 `runnable.schedule()`。**任务因此可以在「排队」和「执行」两种状态之间反复穿梭。**
- **mpsc 队列与条件变量（Condvar）**：生产者把元素推进队列后 `notify`，消费者 `wait` 到有活可干。`ThreadedDispatcher` 全靠这两件标准并发工具搭建。

术语对照：本讲反复出现的 **runnable** 指 `Runnable<RunnableMeta>` 这个信封；**投递（dispatch）** 指把信封放进某个队列的动作；**主循环/事件循环** 指平台上不断取活干活的那条循环（AppKit run loop、calloop、浏览器事件循环、或测试里的 `run_until_idle`）。

## 3. 本讲源码地图

| 文件（相对 `crates/gpui_platform/`） | 作用 |
| --- | --- |
| `../gpui/src/platform.rs` | 契约定义处：`PlatformDispatcher` trait、`RunnableVariant` 别名、`ThreadedDispatcher` 的条件编译门控与再导出 |
| `../gpui/src/platform/threaded_dispatcher.rs` | 通用参考实现：线程池 + 定时器线程 + 主线程队列，本讲精读对象（约 720 行，含测试） |
| `../scheduler/src/scheduler.rs` | `Priority` 枚举与 `RunnableMeta` 元数据的权威定义 |
| `../scheduler/src/executor.rs` | `RunnableMeta` 被附着到 runnable 上的地方（`LocalExecutor::spawn`） |
| `../gpui/src/platform_scheduler.rs` | 执行器层与调度器层之间的适配器 `PlatformScheduler`，追踪「投递旅程」的关键文件 |
| `../gpui/src/executor.rs` | 执行器如何持有 `Arc<dyn PlatformDispatcher>`（承接 u4-l1） |
| `../gpui/src/queue.rs` | `ThreadedDispatcher` 使用的优先级队列（三条 `VecDeque` + 加权随机弹出） |
| `../gpui_linux/src/linux/dispatcher.rs` | 生产实现对照：`LinuxDispatcher`（u4-l3 深入） |
| `../gpui/src/platform/test/dispatcher.rs` | `TestDispatcher`，看「tick」概念的定义处（u8-l4 深入） |

注意一个容易混淆的点：`ThreadedDispatcher` 虽然定义在 gpui 主 crate 里，但**只在测试场景编译**——[../gpui/src/platform.rs:12-16](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform.rs#L12-L16) 用 `#[cfg(any(test, feature = "test-support"))]` 门控了模块，[../gpui/src/platform.rs:84-88](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform.rs#L84-L88) 在同样条件下再导出 `ThreadedDispatcher` 与 `TestDispatcher`。生产路径上真正干活的是各平台自己的实现（Linux/macOS/Windows/Web）。

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：`RunnableVariant`（信封）、`PlatformDispatcher`（契约本体）、投递旅程、优先级队列与 tick、`ThreadedDispatcher`（参考实现）。

### 4.1 RunnableVariant：带元数据的任务信封

#### 4.1.1 概念说明

执行器每接到一个 future 要运行，都得先把它变成可以「排队、转移、执行」的实体。`async-task` 库把这个实体叫做 `Runnable`。GPUI 在此之上定义了一个类型别名：

> `RunnableVariant = Runnable<RunnableMeta>`

为什么需要别名？因为这个泛型参数 `M`（元数据）在 GPUI 里到处都是同一个东西，别名让签名短一点。更重要的历史信息写在注释里：它**曾经是一个单变体枚举**，后来简化成了直接别名——契约在演化中做减法。

元数据 `RunnableMeta` 只有两个字段，都是为诊断与性能分析服务的：

- `location`：任务是在源码哪一行 spawn 的（`#[track_caller]` 捕获的 `&'static Location`）。
- `spawned`：任务被 spawn 的时刻（`SpawnTime`，包装了 `Instant`）。

#### 4.1.2 核心流程

一个信封的诞生：

```text
future（用户代码）
   │  async_task::spawn(future, schedule_fn, metadata)
   ▼
┌──────────────────────────────┐
│ Runnable<RunnableMeta>（信封）│ ←—— Task（持有 JoinHandle，可 await/取消）
│  • run():    poll 一次 future │
│  • schedule(): 触发 schedule_fn│
│  • metadata: 位置 + 时刻      │
└──────────────────────────────┘
```

关键点：`runnable.schedule()` 与「future 被唤醒」是同一件事的两个视角。第一次 `schedule()` 让任务进队；此后任何一次 waker 触发（比如后台线程的 oneshot 发送了数据）都会再次 `schedule()`，把同一个信封重新送回队列。**调度器搬运的永远是这个信封，而不是 future 本身。**

#### 4.1.3 源码精读

类型别名与元数据的定义：

- [../gpui/src/platform.rs:1012-1015](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform.rs#L1015-L1015) —— `pub type RunnableVariant = Runnable<RunnableMeta>;`，即所有投递接口的通用「货币」。注释说明了它曾是枚举、如今是别名。
- [../scheduler/src/scheduler.rs:61-78](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/scheduler/src/scheduler.rs#L61-L78) —— `RunnableMeta` 结构体：`location` 记录 spawn 源码位置，`spawned` 记录时刻；`new_with_callers_location()` 用 `#[track_caller]` 自动采集。

元数据被附着的真实位置（scheduler crate 的本地执行器）：

- [../scheduler/src/executor.rs:62-80](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/scheduler/src/executor.rs#L62-L80) —— `LocalExecutor::spawn` 用 `Location::caller()` 拿到调用点，构造 `RunnableMeta { location, spawned }`，交给 `spawn_local_with_source_location` 产出 `(runnable, task)`，随后立刻 `runnable.schedule()` 让任务进队。

在消费端，元数据被 profiler 读取。`ThreadedDispatcher` 的 worker 线程在执行每个信封前都会取出这两项：

```rust
let location = runnable.metadata().location;
let spawned = runnable.metadata().spawned;
profiler::update_running_task(spawned, location);
runnable.run();
```

见 [../gpui/src/platform/threaded_dispatcher.rs:138-145](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/threaded_dispatcher.rs#L138-L145)，这段 worker 循环展示了「先记录元数据、再 run」的标准姿势（`LinuxDispatcher` 的 worker 也是同样四行，见 u4-l3）。

#### 4.1.4 代码实践

**实践目标**：亲眼看到每个信封都背着 spawn 位置。

**操作步骤**：

1. 用 Grep 在 `crates/gpui` 与 `crates/gpui_linux` 中搜索 `runnable.metadata()`，统计出现次数与所在文件。
2. 再搜索 `RunnableMeta {`，观察元数据在哪几处被构造（提示：`scheduler/src/executor.rs` 与 `gpui/src/platform_scheduler.rs` 的 `timer` 方法）。
3. 阅读其中任意两处，确认构造点的 `location` 都来自 `Location::caller()` 或 `#[track_caller]`。

**需要观察的现象**：元数据的**构造**集中在 scheduler 层，**读取**集中在各调度器的 worker 循环——即「写一次、处处可追踪」。

**预期结果**：构造点很少（两三个），读取点较多；这正说明元数据是基础设施性质的字段，业务代码不碰它。完整统计数字待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`Runnable` 和 `Task` 是同一时间产出的_pair_，它们各自的职责是什么？

**答案**：`Runnable` 是给调度器搬运的「信封」，持有任务状态与执行入口（`run`/`schedule`）；`Task` 是给用户的「凭据」，可以 await 结果、clone、或 drop 以取消任务。二者分离使得调度器不需要知道任务的输出类型。

**练习 2**：为什么 `RunnableMeta` 里要存 `&'static Location<'static>` 而不是 `String` 格式的位置文本？

**答案**：`Location` 来自编译期 `#[track_caller]`，本身是 `&'static` 的静态数据，零分配、零运行期开销；转成 `String` 会给每个任务增加一次堆分配，违背「元数据必须便宜」的设计前提。仅在 profiler 真正需要展示时才格式化。

**练习 3**：future 被 waker 唤醒时，是谁调用了 `runnable.schedule()`？

**答案**：`async-task` 在构建 waker 时已经把 `schedule_fn`（即 spawn 时传入的那个闭包）烘焙进 waker。唤醒 = 再次执行该闭包 = 把信封重新投递进队列。所以「唤醒」最终一定落到某个 `PlatformDispatcher` 方法上——这正是下一节的内容。

### 4.2 PlatformDispatcher trait 本体：五个必需方法与六个默认方法

#### 4.2.1 概念说明

现在回答本讲开篇的问题：**执行器是平台无关的，投递机制是平台相关的，二者之间需要一道契约。** `PlatformDispatcher` 就是这道契约。它的定位可以用一张三层图表达：

```text
┌────────────────────────────────────────────┐
│ 执行器层（平台无关）                         │
│ BackgroundExecutor / ForegroundExecutor     │
└──────────────┬─────────────────────────────┘
               │ 通过 PlatformScheduler 适配（schedule_local / schedule_background）
┌──────────────▼─────────────────────────────┐
│ 契约层：PlatformDispatcher（Send + Sync）    │
│ dispatch / dispatch_on_main_thread / ...    │
└──────────────┬─────────────────────────────┘
               │ 各平台/测试各自实现
┌──────────────▼─────────────────────────────┐
│ 实现层：LinuxDispatcher / MacDispatcher /   │
│ Windows 调度器 / WebDispatcher /            │
│ ThreadedDispatcher / TestDispatcher         │
└────────────────────────────────────────────┘
```

trait 声明本身只有一个约束：`pub trait PlatformDispatcher: Send + Sync`。它被标记为 `#[doc(hidden)]`——作者明说这不是给外部用户的公共 API，而是给平台实现者与测试宏的接口。它**不要求** `'static` 之外的任何东西，但 `Send + Sync` 是硬性的，原因见 4.2.3。

#### 4.2.2 核心流程：方法清单

按「必需 / 默认」两组归类（行号见 4.2.3 的引用）：

| 组 | 方法 | 必需? | 职责 |
| --- | --- | --- | --- |
| 线程身份 | `is_main_thread(&self) -> bool` | 必需 | 当前线程是否主线程（各实现存一个 `ThreadId` 对比） |
| 后台投递 | `dispatch(&self, runnable, priority)` | 必需 | 把信封送进后台线程池 |
| 主线程投递 | `dispatch_on_main_thread(&self, runnable, priority)` | 必需 | 把信封送进主线程队列并唤醒事件循环 |
| 延迟投递 | `dispatch_after(&self, duration, runnable)` | 必需 | 定时器：duration 到期后在**某处**执行信封 |
| 实时线程 | `spawn_realtime(&self, f: Box<dyn FnOnce() + Send>)` | 必需 | 为音频等场景开独立线程立即执行 |
| 空闲投递 | `dispatch_on_main_thread_when_idle(&self, runnable, timeout)` | 默认 | 回退为 `dispatch_on_main_thread(runnable, Priority::Low)`；仅个别平台有真「空闲」语义 |
| 空闲度量 | `idle_time_remaining(&self) -> Option<Duration>` | 默认 `None` | 平台能给出空闲切片剩余时长时覆盖 |
| 时钟 | `now(&self) -> Instant` | 默认 `Instant::now()` | 测试实现可换成虚拟时钟 |
| 定时器精度 | `increase_timer_resolution(&self) -> TimerResolutionGuard` | 默认空守卫 | Windows 默认定时器粒度约 15.6ms，需要时提精度 |
| 测试下转 | `as_test(&self) -> Option<&TestDispatcher>` | 默认 `None` | 把契约指针下转回具体测试类型 |
| 测试下转 | `as_threaded(&self) -> Option<&ThreadedDispatcher>` | 默认 `None` | 同上，转 `ThreadedDispatcher`（cfg 门控） |

对比 u2-l1 学过的 `Platform` trait（69 个方法、18 个默认实现）：`PlatformDispatcher` 小得多——5 个必需方法就是实现一个调度器的全部硬性工作量，其余都可以先吃默认值。

#### 4.2.3 源码精读

trait 的完整定义：

- [../gpui/src/platform.rs:1026-1069](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform.rs#L1026-L1069) —— `PlatformDispatcher` 全文。注意 4 个必需投递方法的参数都是**值传递** `RunnableVariant`（信封所有权随投递转移）；两个 `as_test`/`as_threaded` 下转方法带 `#[cfg(any(test, feature = "test-support"))]` 门控，注释特别说明该 cfg 必须与 `threaded_dispatcher` 模块的门控一致。
- [../gpui/src/platform.rs:1035-1042](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform.rs#L1035-L1042) —— `dispatch_on_main_thread_when_idle` 的默认实现：直接降级为低优先级主线程投递。这是 u2-l1 讲过的「优雅降级型默认实现」在调度器上的翻版：没有空闲语义的平台不必写一行代码就获得可用行为。
- [../gpui/src/platform.rs:1044-1056](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform.rs#L1044-L1056) —— 三个「能力探测型」默认：`idle_time_remaining` 返回 `None`、`now` 返回真实时钟、`increase_timer_resolution` 返回什么都不做的守卫。

一个「最小实现」长什么样？gpui 自己的测试里就有一个故意在每个方法里 panic 的 `SmokeDispatcher`：

- [../gpui/src/platform_scheduler.rs:210-228](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform_scheduler.rs#L210-L228) —— 只实现了 5 个必需方法，且全部 `panic!`。它存在的意义是证明「专线程执行器根本不碰平台调度器」——如果某个测试路径意外调用了任何投递方法，测试会立刻爆炸。这从反面印证了 5 个必需方法就是契约的全部硬边界。

实现者们分布在：

- [../gpui/src/platform/threaded_dispatcher.rs:414-463](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/threaded_dispatcher.rs#L414-L463) —— `ThreadedDispatcher`（本讲 4.5 精读）
- [../gpui_linux/src/linux/dispatcher.rs:102-141](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_linux/src/linux/dispatcher.rs#L102-L141) —— `LinuxDispatcher`（u4-l3 精读）
- `../gpui_macos/src/dispatcher.rs` 与 `../gpui_windows/src/dispatcher.rs` —— u4-l4 精读
- `../gpui_web/src/dispatcher.rs` —— u4-l5 精读
- `../gpui/src/platform/test/dispatcher.rs` —— `TestDispatcher`（u8-l4 精读）

#### 4.2.4 为什么是 `Send + Sync`，而 `Platform` 不是

这是本讲的核心论证，证据链如下：

1. **`Platform` 活在主线程。** u1-l1/u1-l2 讲过，平台对象以 `Rc<dyn Platform>` 构造和持有（[../src/gpui_platform.rs:57](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/src/gpui_platform.rs#L57-L57)），`Rc` 天生 `!Send`。`App::new_app` 还会断言构造发生在主线程：[../gpui/src/app.rs:786-789](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L786-L789) 用 `background_executor.is_main_thread()` 断言「must construct App on main thread」。
2. **执行器必须跨线程持有调度器。** 平台在构造时创建**一个**调度器实例，然后分别用它构造前台、后台两个执行器——以 macOS 为例：[../gpui_macos/src/platform.rs:196-220](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_macos/src/platform.rs#L196-L220) 中 `let dispatcher = Arc::new(MacDispatcher::new());` 随后 `BackgroundExecutor::new(dispatcher.clone())` 与 `ForegroundExecutor::new(dispatcher)`。执行器结构体里存的正是这个 `Arc<dyn PlatformDispatcher>`：[../gpui/src/executor.rs:21-30](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/executor.rs#L21-L30)。
3. **`dispatch` 的调用点在后台线程。** 后台执行器把 future 交给 scheduler crate，后者调用 `PlatformScheduler::schedule_background_with_priority`，进而调用 `dispatcher.dispatch(...)`——这一刻的调用线程就是 spawn 任务的任意业务线程或 worker 线程。多个后台线程会**同时**通过各自的执行器克隆触碰同一个调度器。若调度器不是 `Send + Sync`，`Arc<dyn PlatformDispatcher>` 就无法跨线程传递，整条链编译不过。
4. **对照：前台执行器故意 `!Send`。** `ForegroundExecutor` 的字段 `not_send: PhantomData<Rc<()>>`（[../gpui/src/executor.rs:29](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/executor.rs#L29-L29)）在编译期禁止它离开主线程——但它内部的 `dispatcher` 字段仍可被**主线程上的代码**用来投递。于是分工清晰：**平台对象与前台执行器被锁在主线程；调度器作为两者之间唯一需要跨线程的部件，被要求 `Send + Sync`。**

一句话总结：`Send + Sync` 不是为调度器「加分」的约束，而是它的**工作内容本身**——它就是被设计成从任意线程安全地敲主线程门的门铃。

#### 4.2.5 代码实践

**实践目标**：亲手验证「5 个必需方法」清单，并找到所有实现者。

**操作步骤**：

1. 打开 [../gpui/src/platform.rs:1026-1069](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform.rs#L1026-L1069)，把每个方法抄进 4.2.2 的表格，标注必需/默认。
2. 用 Grep 在整个仓库搜索 `impl PlatformDispatcher for`，列出所有实现者与文件行号。
3. 任选一个非测试实现（推荐 `LinuxDispatcher`），对照表格确认它覆盖了哪些默认方法、跳过了哪些。

**需要观察的现象**：实现者的数量比想象多（生产平台 + 测试替身 + 通用实现），但每个实现者真正「重写」的方法都很少。

**预期结果**：搜到的实现者应包括 `ThreadedDispatcher`、`TestDispatcher`、`LinuxDispatcher`、macOS/Windows/Web 各一，以及测试里的 `SmokeDispatcher`。每个实现者至少覆盖 5 个必需方法。具体命中清单待本地验证。

#### 4.2.6 小练习与答案

**练习 1**：如果给 `PlatformDispatcher` 新增一个带默认实现的方法，会不会破坏现有实现者？

**答案**：不会。trait 的默认实现意味着实现者不覆盖也能编译，这是 GPUI 敢于给契约「做加法」的方式（`as_threaded` 就是后加的、带默认 `None` 与 cfg 门控的方法）。反之，新增**必需**方法会同时打破所有实现者，代价高得多。

**练习 2**：`now()` 明明就是 `Instant::now()`，为什么要放在契约上？

**答案**：为了让测试实现能换成**虚拟时钟**。`TestDispatcher` 覆盖 `now()` 后，`advance_clock` 可以把时间「快进」而不真实等待——u8-l4 的时钟推进测试全靠这个钩子。生产平台吃默认值即可。`PlatformClock` 把 `dispatcher.now()` 暴露给整个 scheduler 体系，见 [../gpui/src/platform_scheduler.rs:186-199](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform_scheduler.rs#L186-L199)。

**练习 3**：`as_test`/`as_threaded` 这种「下转」方法为什么必须由 trait 自己提供，而不是让调用方用 `Any` 做类型检查？

**答案**：`dyn PlatformDispatcher` 的具体类型被擦除了，调用方手里的 `Arc<dyn PlatformDispatcher>` 无法直接 downcast（除非 trait 声明 `Any` 作为 supertrait，那会污染所有实现者）。在 trait 上放两个返回 `Option<&Concrete>` 的默认方法，是用最小成本实现「知道自己是测试替身」的自省——执行器构造时正是靠 `as_test()` 判断该不该走确定性测试调度器。

### 4.3 一次投递的完整旅程：从 `background_spawn` 到主线程

#### 4.3.1 概念说明

这是本讲三条学习目标中的第三条。把 4.1 的信封、4.2 的契约串起来，追踪一个典型场景：

> 你在主线程写了 `cx.background_spawn(算一下) .await 结果更新 UI`（实际上这是一个前台任务 await 一个后台 Task）。

此时发生了什么？关键认知是：**存在两类投递**——后台任务第一次进队（`dispatch`）与任何任务回到主线程（`dispatch_on_main_thread`）。定时器是第三条支线（`dispatch_after`）。

#### 4.3.2 核心流程

```text
【主线程】cx.spawn(async {                          ── 用户 future A（可 !Send）
   let data = cx.background_spawn(async { 重计算 }).await;   ── 派生 future B（必须 Send）
   label.set(data);                                 ── 更新 UI
})

 1. spawn(A)            → LocalExecutor::spawn：构造信封 Envelope_A
                          runnable.schedule() → dispatch 闭包
 2. dispatch 闭包        → PlatformScheduler::schedule_local
 3. 契约                 → dispatcher.dispatch_on_main_thread(Envelope_A, Medium)
 4. [主队列] Envelope_A 等待；主循环取出 → Envelope_A.run() → poll A
    A 在 .await 处挂起，注册 waker；同时 spawn(B) 走:
       schedule_background_with_priority → dispatcher.dispatch(Envelope_B, priority)
 5. [后台队列] worker 线程 pop Envelope_B → Envelope_B.run() → B 算完
    Task 完成 → 唤醒 A 的 waker → Envelope_A.schedule()
 6. 回到第 3 步：Envelope_A 再次进主队列 → 主循环取出 → A 从 await 恢复 → 更新 UI
```

注意第 5→6 步：唤醒 A 的动作发生在**后台 worker 线程**上，但投递接口保证 Envelope_A 永远只在主线程被 `run()`。「哪个线程调用 dispatch」与「哪个线程执行 runnable」是两回事——契约存在的意义就是让前者任意、后者确定。

定时器支线：`cx.background_executor().timer(d)` 会构造一个只发一次 oneshot 的信封，其 schedule 闭包固定调用 `dispatch_after(d, envelope)`——到期前信封住在平台定时器结构里，到期后被执行并发送完成信号。见 [../gpui/src/platform_scheduler.rs:137-160](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform_scheduler.rs#L137-L160)。

#### 4.3.3 源码精读

适配器层（执行器 → 调度器的三个跳板）：

- [../gpui/src/platform_scheduler.rs:118-123](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform_scheduler.rs#L118-L123) —— `schedule_local`：前台任务的唯一入口，调用 `dispatch_on_main_thread(runnable, Priority::default())`。注意：**前台投递的优先级恒为默认值**。
- [../gpui/src/platform_scheduler.rs:125-131](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform_scheduler.rs#L125-L131) —— `schedule_background_with_priority`：后台任务入口，优先级原样透传给 `dispatch`。
- [../gpui/src/platform_scheduler.rs:46-54](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform_scheduler.rs#L46-L54) —— `foreground_executor()` 构造 `LocalExecutor` 时传入的 dispatch 闭包：每次信封被 schedule 就走一遍 `schedule_local`。这就是 4.3.2 流程图里「dispatch 闭包」的真身。
- [../gpui/src/platform_scheduler.rs:27-33](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform_scheduler.rs#L27-L33) —— `PlatformScheduler` 结构体：把 `Arc<dyn PlatformDispatcher`> 包装成 scheduler crate 的 `Scheduler` trait 实现。文档注释点明它让 GPUI 复用 scheduler crate 的执行器类型而接入平台原生派发机制（如 macOS 的 GCD）。

执行器侧的组装（承接 u4-l1）：

- [../gpui/src/executor.rs:65-82](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/executor.rs#L65-L82) —— `BackgroundExecutor::new(dispatcher)`：先用 `as_test()` 判断是否测试替身（决定用确定性调度器还是 `PlatformScheduler`），再把调度器装进 scheduler crate 的执行器外壳。
- [../gpui/src/executor.rs:347-354](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/executor.rs#L347-L354) —— `ForegroundExecutor::spawn`：注意签名没有 `Send` 约束（future 可 `!Send`），因为它只会在主线程被 poll。

`!Send` future 的安全协议——本旅程中最微妙的一环：

- [../gpui_linux/src/linux/dispatcher.rs:113-127](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_linux/src/linux/dispatcher.rs#L113-L127) —— `LinuxDispatcher::dispatch_on_main_thread` 的注释是权威解释：`Runnable` 可能包着 `!Send` 的 future；平时安全是因为只在主线程 poll；但若 `send` 失败（说明主循环已拆除、应用正在退出），此时必然身处后台线程，**在错误线程 drop 一个 `!Send` 的东西可能未定义**，而进程马上也要退了——所以选择 `std::mem::forget(runnable)` 泄漏而非 drop。`ThreadedDispatcher` 的同款处理见 [../gpui/src/platform/threaded_dispatcher.rs:426-437](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/threaded_dispatcher.rs#L426-L437)，注释里写明「mirrors LinuxDispatcher」。

这也解释了 `RunnableVariant` 为何在类型上是 `Send` 的（否则跨线程队列装不下它）：**信封的搬运是线程安全的，信封里 future 的执行与销毁被协议约束在主线程**。类型系统管前一半，注释与纪律管后一半。

#### 4.3.4 代码实践

**实践目标**：把 4.3.2 的流程图锚定到真实源码行。

**操作步骤**：

1. 从 `cx.background_spawn` 的定义出发（u4-l1 已定位），一路用 rust-analyzer 的「Go to Definition」追到 `dispatch`，记录途经的每个函数名与文件。
2. 单独追一条唤醒链：在 gpui 中找一处 `.await` 后更新 UI 的代码（例如任意 `cx.spawn` 内 `background_spawn(...).await`），画出「后台完成 → waker → schedule_local → dispatch_on_main_thread」的链条。
3. 追定时器链：`BackgroundExecutor::timer`（[../gpui/src/executor.rs:186-192](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/executor.rs#L186-L192)）→ `PlatformScheduler::timer` → `dispatch_after`。

**需要观察的现象**：三条链（前台、后台、定时器）在 `PlatformScheduler` 汇聚后各奔一个 `dispatch_*` 方法，再往下就进入平台特定代码。

**预期结果**：得到一张三行对照表，每行以一个 `dispatch_*` 方法收尾。此练习为源码阅读型，不涉及运行；链路终点行号待本地按当前工作区确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `schedule_local` 投递主线程任务时固定用 `Priority::default()`，而不是透传优先级？

**答案**：主线程只有一个队列、一个执行顺序，本质是 FIFO；给主线程任务分优先级没有意义。`ForegroundExecutor::spawn_with_priority` 的实现干脆忽略优先级参数，注释写明「前台任务按序运行」，见 [../gpui/src/executor.rs:356-368](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/executor.rs#L356-L368)。优先级只影响后台线程池的取活顺序（见 4.4）。

**练习 2**：如果 `dispatch_on_main_thread` 在 send 失败时直接 `unwrap()`，最坏会发生什么？

**答案**：在应用退出的竞态窗口里，一个 `!Send` future 的 `Drop` 会在后台线程上执行，对 `Rc`/`RefCell` 这类类型属于未定义行为（可能崩溃或内存损坏）。所以实现者一致选择 `forget`（泄漏），用小的内存代价换线程安全。

**练习 3**：`dispatch_after` 到期后，信封在哪个线程执行？各实现一致吗？

**答案**：契约不规定。`ThreadedDispatcher` 的定时器线程亲自执行（见 4.5）；`LinuxDispatcher` 也是独立定时器线程执行。因此定时器回调若要碰 UI，仍需通过 oneshot + 前台任务回到主线程，而不能假设自己在主线程。

### 4.4 优先级队列与 tick：优先级如何真正生效

#### 4.4.1 概念说明

`dispatch(runnable, priority)` 的 `priority` 参数进了队列之后如何影响执行顺序？直觉答案是「高优先级先跑」，真实答案更精妙：**加权随机，而非严格排序**。`Priority` 的文档注释直言不讳：更高优先级**更有可能**先被调度，但不是保证——调度器会在不同优先级之间交错，以防低优先级任务饿死。

另一个必须澄清的词是 **tick**。大纲主题里提到的 tick 概念来自测试体系：一次 tick = 执行**恰好一个**任务。它是确定性测试的原子操作，`run_until_parked`/`run_until_idle` 都是 tick/drain 的循环封装。生产调度器没有 tick 这个词，但有对应的「事件循环转一圈」。

#### 4.4.2 核心流程

优先级枚举与权重：

- `RealtimeAudio`：权重 0，**永不进入优先级队列**——`spawn_realtime` 直接开独立线程。
- `High`：权重 60
- `Medium`：权重 30（默认）
- `Low`：权重 10

队列弹出时做加权抽签。当三个队列都非空时，选中各队列的概率为：

\[ P(\text{High}) = \frac{60}{60+30+10} = 0.6, \quad P(\text{Medium}) = 0.3, \quad P(\text{Low}) = 0.1 \]

即每弹一个任务掷一次「灌了铅的骰子」（代码注释里给出了算法出处：Keith Schwarz 的 darts-dice-coins）。低优先级任务不会被无限期搁置——只要它的队列非空，每次抽签都有 10% 的**边缘概率**胜出（当高/中队列耗尽时概率升至 100%）。

tick 的层级关系：

```text
tick()                执行一个任务（原子操作，TestDispatcher）
  └─ run_until_parked()  循环 tick 直到调度器「停车」（测试常用）
       └─ run_until_idle()  真实时间版本：drain 主队列 + 等后台/定时器在飞数归零（ThreadedDispatcher）
```

#### 4.4.3 源码精读

- [../scheduler/src/scheduler.rs:23-42](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/scheduler/src/scheduler.rs#L23-L42) —— `Priority` 枚举：四档 + 文档注释明示「非严格保证、交错防饿死」。
- [../scheduler/src/scheduler.rs:44-56](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/scheduler/src/scheduler.rs#L44-L56) —— `weight()`：High 60 / Medium 30 / Low 10 / RealtimeAudio 0（实时优先级不参与概率调度）。
- [../gpui/src/queue.rs:12-16](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/queue.rs#L12-L16) —— 队列的真实形态：不是一个大堆，而是**三条 `VecDeque`**（high/medium/low 各一条），同优先级内 FIFO。
- [../gpui/src/queue.rs:69-78](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/queue.rs#L69-L78) —— `push`：按优先级进对应队列；`RealtimeAudio` 分支直接 `unreachable!`——实时任务在更上层已被 `spawn_realtime` 截走，永远不该走到这里（这是 4.3 里 `spawn_with_priority` 先判断 `RealtimeAudio` 的原因，见 [../gpui/src/executor.rs:134-138](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/executor.rs#L134-L138)）。
- [../gpui/src/queue.rs:313-357](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/queue.rs#L313-L357) —— `pop_inner`：加权抽签的完整实现。先算三个非空队列的权重质量，再依次以 `weight/mass` 的概率尝试 High、Medium、Low；注释标明算法是 loaded die / biased coin（darts-dice-coins）。
- [../gpui/src/queue.rs:145-148](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/queue.rs#L145-L148) —— `send(priority, item)`：投递入口，内部 push 后 `notify_one` 唤醒一个等待中的接收者。

tick 的定义处：

- [../gpui/src/platform/test/dispatcher.rs:68-78](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/test/dispatcher.rs#L68-L78) —— `TestDispatcher::tick(background_only)`：执行一个任务；紧随其后的 `run_until_parked` 就是 `while self.tick(false) {}`。
- [../gpui/src/executor.rs:206-210](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/executor.rs#L206-L210) —— `BackgroundExecutor::tick()`：测试辅助，内部经 `as_test()` 下转后调用 scheduler 的 tick。

#### 4.4.4 代码实践

**实践目标**：直观感受「加权随机」而非「严格优先」。

**操作步骤**：

1. 阅读 [../gpui/src/queue.rs:313-357](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/queue.rs#L313-L357)，手推一个场景：队列里有 1 个 High、1 个 Medium、1 个 Low 任务，写出第一次 pop 选中每个的概率。
2. 运行 gpui 已有的队列/调度器测试：在仓库根执行 `cargo test -p gpui threaded_dispatcher`（该模块在 `cfg(test)` 下编译，无需额外 feature）。
3. 阅读测试输出中被执行的用例名，与 4.5.3 列出的测试一一对应。

**需要观察的现象**：所有测试通过；注意其中没有断言「High 一定先于 Low 执行」的用例——测试作者也知道顺序是概率性的。

**预期结果**：`is_idle_tracks_queued_work_but_ignores_undue_timers`、`timers_fire_in_real_time` 等 8 个测试通过。具体输出待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么不用一个 `BinaryHeap` 按 `(priority, seq)` 排序实现严格优先级？

**答案**：严格优先级下，只要 High 队列持续有新任务，Low 队列会永久饥饿。GPUI 选择了统计意义上的偏向：High 任务期望上获得 60% 的服务率，Low 仍有 10%，任何任务最终都能被调度。对 UI 场景，「几乎总是先响应高优」与「绝不饿死」的折中比绝对顺序更健康。

**练习 2**：`dispatch_on_main_thread` 收到的 `priority` 参数在 `ThreadedDispatcher` 里去了哪里？

**答案**：随 `main_sender.send(priority, runnable)` 进入主队列的三条 `VecDeque`，但主队列由 `drain_main_queue` 逐个弹出执行且从未按优先级抽签（见 4.5.3）——所以该参数目前对主线程队列实际不起排序作用，只是被存储了。这与 `schedule_local` 恒用默认优先级相呼应。

**练习 3**：`tick()` 与 `run_until_parked()` 各适合什么测试场景？

**答案**：`tick()` 适合断言「执行一个任务后的精确中间状态」（例如验证某任务让出后队列里还剩什么）；`run_until_parked()` 适合「跑到没有可推进的任务为止」的终态断言。需要跨虚拟时间时再配合 `advance_clock`。

### 4.5 ThreadedDispatcher：一个可运行的通用参考实现

#### 4.5.1 概念说明

`PlatformDispatcher` 有六个实现者，其中五个绑定平台或测试框架，唯有 `ThreadedDispatcher` 是**不依赖任何操作系统的通用实现**：线程池 + 优先级队列 + 定时器线程，全部用标准库原语搭成。它的文档注释给自己的定位是「供测试与基准测试使用的多线程调度器」——后台任务在 worker 池并行、定时器实时触发，模仿生产行为；主线程任务因为没有平台事件循环，靠创建者手动调用 `run_until_idle` 来驱动。与 `TestDispatcher`（单线程、虚拟时钟、确定性）形成互补。

对学习者而言，它是**最小可运行范本**：读懂它，就知道了实现 `PlatformDispatcher` 到底需要什么。

#### 4.5.2 核心流程

`ThreadedDispatcher::new()` 在**调用线程**（即「主线程」）上完成装配：

```text
调用 new() 的线程 ─── 记下 main_thread_id ──┐
                                            │
 ├── 后台通道 PriorityQueueSender/Receiver ─┤
 ├── 主线程通道 PriorityQueueSender         │   main_receiver 交还给主线程
 │   （receiver 用 Mutex 包着留给主线程）    │   供 run_until_idle 弹任务
 ├── N 个 worker 线程（N = max(CPU 数, 2)） ─┤   循环 receiver.pop() → runnable.run()
 ├── 1 个定时器线程                          │   BinaryHeap 按 due 时间等待/触发
 └── IdleTracker（在飞计数 + Condvar）       ┘   run_until_idle 等它归零
```

主线程驱动循环 `run_until_idle` 的逻辑（伪代码）：

```text
loop:
    若主队列非空 → 全部弹出执行，continue
    若有到期定时器 → 短等 1ms 后 continue（定时器线程正在触发它）
    加在飞锁：
        若主队列又有活 → continue
        若在飞数 == 0  → return（真正空闲）
        否则 wait（被 notify_under_lock 或归零唤醒）
```

#### 4.5.3 源码精读

结构与状态：

- [../gpui/src/platform/threaded_dispatcher.rs:17-35](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/threaded_dispatcher.rs#L17-L35) —— 结构体五个字段：后台/主线程两个 sender、被 `Mutex` 保护的主线程 receiver、定时器队列、空闲追踪器、主线程 id。文档注释说明了它与 `TestDispatcher` 的分工。
- [../gpui/src/platform/threaded_dispatcher.rs:38-71](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/threaded_dispatcher.rs#L38-L71) —— `IdleTracker`：一个计数器 + 条件变量。`increment`/`decrement` 维护「在飞」任务数；`decrement_on_drop` 返回一个 RAII 守卫，即使 runnable panic 也能正确减计数；`notify_under_lock` 解释了一个经典并发陷阱——必须在持有同一把锁时 notify，否则唤醒信号可能恰好落在「检查完但还没 wait」的缝隙里丢失。
- [../gpui/src/platform/threaded_dispatcher.rs:73-112](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/threaded_dispatcher.rs#L73-L112) —— `TimerQueue`：`BinaryHeap<TimerEntry>`，`TimerEntry` 带单调递增的 `seq` 打破同刻平局；`Ord` 实现刻意**反转**比较方向，因为 `BinaryHeap` 是最大堆而我们需要最早到期者在堆顶——这是 Rust 二叉堆的惯用小技巧。

装配：

- [../gpui/src/platform/threaded_dispatcher.rs:120-149](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/threaded_dispatcher.rs#L120-L149) —— `new()`：线程数取 `max(available_parallelism, 2)`；每个 worker 拿一份 `receiver.clone()` 进入 `while let Ok(runnable) = receiver.pop()` 循环，执行前 `idle.decrement_on_drop()` 登记在飞、读取元数据喂 profiler，然后 `runnable.run()`。
- [../gpui/src/platform/threaded_dispatcher.rs:151-208](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/threaded_dispatcher.rs#L151-L208) —— 定时器线程：锁内 `peek` 堆顶，未到期则 `wait_until(due)`，到期则先 `increment` 在飞数再放锁执行（注释解释了锁序：永远「定时器状态锁 → 在飞锁」，`run_until_idle` 不得反序，否则死锁）。
- [../gpui/src/platform/threaded_dispatcher.rs:211-258](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/threaded_dispatcher.rs#L211-L258) —— `run_until_idle`：文档特别强调「未到期的定时器**不**等待」——它跑在真实时间上，不像 `TestDispatcher` 可以把时钟快进；睡在未来定时器上等于真实阻塞整个时长。
- [../gpui/src/platform/threaded_dispatcher.rs:393-411](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/threaded_dispatcher.rs#L393-L411) —— `drain_main_queue`：只在 `pop` 的瞬间持锁，让正在执行的 runnable 可以重入地再投递主线程任务（对应 4.4.5 练习 2 的观察：主队列按弹出顺序执行）。

契约实现（对照 4.2.2 的表格逐个看）：

- [../gpui/src/platform/threaded_dispatcher.rs:415-417](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/threaded_dispatcher.rs#L415-L417) —— `is_main_thread`：比较当前线程 id 与构造时记录的 id。
- [../gpui/src/platform/threaded_dispatcher.rs:419-424](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/threaded_dispatcher.rs#L419-L424) —— `dispatch`：先 `increment` 在飞数，再 `send` 进后台通道；失败则 panic（worker 线程不该消亡）。
- [../gpui/src/platform/threaded_dispatcher.rs:426-437](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/threaded_dispatcher.rs#L426-L437) —— `dispatch_on_main_thread`：投进主通道；失败走 4.3.3 讲过的 `forget` 协议；成功后 `notify_under_lock` 叫醒可能正等待的 `run_until_idle`。**注意 `increment` 的缺席**——在飞计数只统计后台与定时器任务，主队列是否有活由 `main_queue_has_work` 单独判断。
- [../gpui/src/platform/threaded_dispatcher.rs:439-449](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/threaded_dispatcher.rs#L439-L449) —— `dispatch_after`：打包成 `TimerEntry` 入堆，`notify_one` 让定时器线程重新检查最近的到期时间。
- [../gpui/src/platform/threaded_dispatcher.rs:451-458](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/threaded_dispatcher.rs#L451-L458) —— `spawn_realtime`：注释坦白「不需要真实实时优先级，普通线程保便携性」。
- [../gpui/src/platform/threaded_dispatcher.rs:460-462](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/threaded_dispatcher.rs#L460-L462) —— `as_threaded` 返回 `Some(self)`，其余默认方法（`as_test`、`now`、`idle_time_remaining` 等）一律吃默认值。

与 `LinuxDispatcher` 的快速对照（为 u4-l3 预热）：

| 维度 | ThreadedDispatcher | LinuxDispatcher |
| --- | --- | --- |
| 主队列唤醒 | `Condvar::notify_under_lock` | calloop 的 `PriorityQueueCalloopSender` 直接注入事件循环 |
| 后台线程池 | `max(并行度, 2)` 个 worker | 同样 `max(并行度, 2)`（[../gpui_linux/src/linux/dispatcher.rs:33-34](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_linux/src/linux/dispatcher.rs#L33-L34)） |
| 定时器 | 独立线程 + `BinaryHeap` | 独立线程 + calloop `EventLoop` 的 timer source |
| send 失败处理 | `std::mem::forget` | `std::mem::forget`（原文注释的出处） |

#### 4.5.4 代码实践（本讲主实践）

**实践目标**：写一段测试，从**普通 `std::thread`** 唤醒一个主线程任务，并用线程 id 证明任务确实在前台（主）线程恢复执行。

**背景阅读**：先精读 gpui 自带的两个现成测试——[../gpui/src/platform/threaded_dispatcher.rs:532-558](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/threaded_dispatcher.rs#L532-L558)（后台→主线程交接）与 [../gpui/src/platform/threaded_dispatcher.rs:561-588](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/threaded_dispatcher.rs#L561-L588)（`std::thread` 外部唤醒——正是本实践的官方范本）。

**操作步骤**：

1. **先跑零改动路线**：在仓库根目录执行 `cargo test -p gpui threaded_dispatcher`，确认 8 个现有测试全绿（模块在 `cfg(test)` 下编译，无需 feature）。
2. **再走动手路线**：在仓库外新建一个独立小 crate（`cargo new dispatcher-lab`），添加依赖：

   ```toml
   # 示例代码：dispatcher-lab/Cargo.toml
   [dependencies]
   gpui = { path = "/path/to/zed/crates/gpui", features = ["test-support"] }
   futures = "0.3"
   ```

   > `ThreadedDispatcher` 只在 `test-support` feature（或 gpui 自身测试）下导出（[../gpui/src/platform.rs:87-88](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform.rs#L87-L88)），所以外部使用必须开这个 feature。

3. 写入下面的测试（示例代码，逻辑仿照 `run_until_waits_for_untracked_external_wakes`，但只用公开 API，用「轮询 `run_until_idle`」替代 crate 私有的 `run_until`）：

   ```rust
   // 示例代码：dispatcher-lab/src/lib.rs
   use std::sync::{Arc, Mutex};
   use std::thread;
   use std::time::{Duration, Instant};

   use futures::channel::oneshot;
   use gpui::{ForegroundExecutor, ThreadedDispatcher};

   #[test]
   fn task_woken_from_std_thread_resumes_on_main_thread() {
       let dispatcher = Arc::new(ThreadedDispatcher::new());
       let foreground = ForegroundExecutor::new(dispatcher.clone());
       let main_thread_id = thread::current().id();

       let (wake_tx, wake_rx) = oneshot::channel::<()>();
       let ran_on: Arc<Mutex<Option<thread::ThreadId>>> = Arc::new(Mutex::new(None));

       // 1. 主线程注册前台任务：挂起等 oneshot，恢复后记录自己的线程 id
       foreground
           .spawn({
               let ran_on = ran_on.clone();
               async move {
                   let _ = wake_rx.await;
                   *ran_on.lock().unwrap() = Some(thread::current().id());
               }
           })
           .detach();

       // 2. 普通 std::thread 稍后唤醒它。
       //    oneshot 发送会触发前台任务的 waker → schedule_local
       //    → dispatch_on_main_thread → 主队列
       let waker_thread = thread::spawn(move || {
           thread::sleep(Duration::from_millis(10));
           let _ = wake_tx.send(());
       });

       // 3. 主线程反复驱动事件循环直到任务完成。
       //    （不用 crate 私有的 run_until，只能轮询 run_until_idle）
       let deadline = Instant::now() + Duration::from_secs(5);
       while ran_on.lock().unwrap().is_none() && Instant::now() < deadline {
           dispatcher.run_until_idle();
           thread::sleep(Duration::from_millis(5));
       }

       waker_thread.join().unwrap();
       assert_eq!(*ran_on.lock().unwrap(), Some(main_thread_id));
   }
   ```

4. 运行 `cargo test`。

**需要观察的现象**：

- 断言通过——`ran_on` 里记录的线程 id 等于测试主线程的 id，而不是 `waker_thread` 或某个 worker 的 id。
- 若把第 3 步换成「只调用一次 `dispatcher.run_until_idle()`」，测试可能**间歇性失败**：任务尚未被唤醒时 `run_until_idle` 就已返回（外部 `std::thread` 的唤醒不在调度器的在飞计数内）。这正是官方测试要用 `run_until`「跨临时静默等待」的原因，也是文档注释（[../gpui/src/platform/threaded_dispatcher.rs:260-270](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/threaded_dispatcher.rs#L260-L270)）专门解释过的坑。

**预期结果**：轮询版稳定通过；单次 `run_until_idle` 版本存在时序竞态。两种行为的差异待本地验证（结果可能与机器负载相关，失败应为偶发而非必然）。

**备选路径**：若不想新建 crate，也可以临时把测试追加到 `crates/gpui/src/platform/threaded_dispatcher.rs` 的 `tests` 模块里跑（该路径可用 `run_until`，写法更贴近官方测试），验证完删掉即可。注意不要把临时测试提交进仓库。

#### 4.5.5 小练习与答案

**练习 1**：`run_until_idle` 为什么不等待「尚未到期」的定时器？`TestDispatcher` 为什么可以？

**答案**：`ThreadedDispatcher` 用真实时间，等待未来定时器 = 真实阻塞（一个 60 秒的定时器会让测试卡 60 秒）；`TestDispatcher` 持有虚拟时钟，`advance_clock` 可以瞬间把时间推到到期点。前者只能把「睡在未来定时器上的任务」视为空闲（[../gpui/src/platform/threaded_dispatcher.rs:211-219](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/threaded_dispatcher.rs#L211-L219)）。

**练习 2**：`dispatch_on_main_thread` 成功后为何要 `notify_under_lock`？去掉「持锁 notify」会怎样？

**答案**：`run_until_idle` 在决定 `wait` 之前会最后检查一次主队列，若投递方在「检查之后、wait 之前」入队并 notify，无锁的 notify 会落空，主线程就会睡过头。把 notify 放在同一个 `inflight` 锁内，保证投递与等待在锁的序列化下互斥，信号不可能插入缝隙（[../gpui/src/platform/threaded_dispatcher.rs:64-70](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/platform/threaded_dispatcher.rs#L64-L70) 的注释即为此而写）。

**练习 3**：`TimerEntry` 的 `Ord` 为什么把比较方向反过来？

**答案**：`std::collections::BinaryHeap` 是**最大**堆，堆顶是「最大」元素；我们想要的是最早到期者在顶。把 `cmp` 反转（拿 `other` 和 `self` 比而不是 `self` 和 `other`），就让「最早到期」成为堆意义上的「最大」。`seq` 字段则以入队序打破同刻平局，保证 FIFO 稳定性。

## 5. 综合实践

**任务：给一条「跨线程流水线」办线程护照。**

把本讲所有知识串起来：写一个独立示例（基于 4.5.4 的 `dispatcher-lab` crate 扩展），模拟「主线程派活 → 后台并行计算 → 结果回主线程汇总」的完整旅程，并在每个阶段记录线程 id，最后核验护照。

要求：

1. 主线程 `spawn` 一个前台任务 A：创建两个 `background_spawn` 子任务（各睡 10ms 后返回各自执行线程的 `ThreadId`），`await` 两者结果。
2. 两个子任务返回后，任务 A 在**自身继续执行的代码**里再取一次 `thread::current().id()`，连同两个子任务的 id 一起存入 `Arc<Mutex<Passport>>`。
3. 主线程循环 `run_until_idle` 直到护照就绪（参照 4.5.4 的轮询模式）。
4. 最后断言：两个子任务的 id 各不相同（不同 worker 线程）、都不等于主线程 id；任务 A 恢复执行后的 id 等于主线程 id。
5. 在笔记里回答：任务 A 第二段代码之所以在主线程执行，中间依次经过了哪三个函数？（答案参照 4.3.2 的第 5→6 步：waker → `schedule_local` → `dispatch_on_main_thread` → 主队列 → `drain_main_queue` 中的 `runnable.run()`。）

**预期结果**：三个断言全部通过；流水线各阶段的线程 id 构成一份清晰的「护照」，直观印证「后台并行、前台单线程」。完整运行输出待本地验证。

**延伸（可选）**：把其中一个子任务换成 `background.timer(Duration::from_millis(50))` 后再执行，观察护照里定时器唤醒路径的线程 id——它会指向哪个线程？先预测（提示：4.3.5 练习 3），再验证。

## 6. 本讲小结

- `RunnableVariant` 是 `Runnable<RunnableMeta>` 的别名——所有投递接口流通的「信封」，元数据（spawn 位置 + 时刻）由 scheduler 层在 spawn 时自动附着，供 profiler 消费。
- `PlatformDispatcher` 是执行器层与平台事件循环之间的契约：5 个必需方法（`is_main_thread`、`dispatch`、`dispatch_on_main_thread`、`dispatch_after`、`spawn_realtime`）+ 6 个默认方法（空闲投递、空闲度量、时钟、定时器精度、两个测试下转）。
- 它要求 `Send + Sync` 是由工作内容决定的：`dispatch` 会被任意后台线程调用、调度器被 `Arc` 共享给前后台两个执行器；而 `Platform` 以 `Rc` 持有、被锁在主线程，前台执行器更用 `PhantomData<Rc<()>>` 在编译期禁止跨线程。
- 一次完整旅程：spawn 构造信封 → `schedule_local`/`schedule_background_with_priority` → 对应 `dispatch_*` → 平台队列 → worker 或主循环取出 → `runnable.run()`；唤醒即再投递。`!Send` future 的安全协议是「只在主线程 poll/drop，失败时 `forget` 而非 drop」。
- 优先级队列是三条 FIFO `VecDeque` + 60/30/10 加权随机抽签，防饿死而非严格排序；`RealtimeAudio` 永不入队；主线程队列不按优先级排序；测试体系里一次 tick = 执行一个任务。
- `ThreadedDispatcher` 是不依赖操作系统的通用实现（线程池 + `BinaryHeap` 定时器线程 + Condvar 空闲追踪），只在 `test-support`/测试下编译，是理解契约的最小可运行范本，也是 u8-l4 测试设施与 u8-l5 毕业实践（`FakePlatform`）的基石。

## 7. 下一步学习建议

本讲学完了**契约**，下一讲 u4-l3《LinuxDispatcher 与 calloop》进入**生产实现**：你会看到 `dispatch_on_main_thread` 如何借 calloop 的 channel source 把信封直接注入真实事件循环、`PriorityQueueCalloopSender/Receiver` 与本讲 `queue.rs` 的异同、以及 `LoopSignal` 如何扮演 `IdleTracker` 的角色。阅读时建议把 u4-l3 的每一节与本讲 4.5.3 的对照表互相印证。

若你想先横向对比，可跳到 u4-l4（macOS/Windows 调度器）看 GCD 与 Windows 消息循环如何映射到同一契约；想深入 tick 与虚拟时钟，则直接前往 u8-l4（test-support 与可视化测试）。无论走哪条线，把本讲 4.3.2 的旅程图放在手边——所有调度器的差异，都只是这张图里「队列」和「唤醒」两格的不同填法。
