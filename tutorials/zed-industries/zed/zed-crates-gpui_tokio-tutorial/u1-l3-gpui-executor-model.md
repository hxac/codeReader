# 前置知识一：GPUI 的执行器与 Task 模型

## 1. 本讲目标

上一讲（u1-l1）我们知道了 gpui_tokio 要解决的问题：GPUI 侧的任务句柄是 `Task`，Tokio 侧的任务句柄是 `JoinHandle`，两者不能直接互相 await。本讲先把 GPUI 这一侧的基础打牢。学完本讲，你应该能够：

1. 说出 GPUI 的「前台单线程 + 后台线程池」双执行器模型，以及各自适合什么工作。
2. 区分 `cx.spawn`（前台）与 `cx.background_spawn`（后台）在签名、`Send` 约束和使用场景上的差异。
3. 说明 `Task` 的三种处置方式（await / detach / drop），特别是 **drop 即取消** 这一贯穿整个 gpui_tokio 设计的语义。
4. 解释 `AppContext` trait 为什么能让 `gpui_tokio::Tokio::spawn` 接受几乎任意一种 GPUI 上下文。

本讲不涉及任何 Tokio 知识——那是下一讲（u1-l4）的内容。

## 2. 前置知识

用最通俗的语言补几个本讲会用到的概念：

- **Future（期物）**：Rust 异步的基本单位。一个 `Future` 是一段「可以暂停和恢复」的计算：执行器每次调用它的 `poll` 方法，它要么说「我完成了，结果是这个」，要么说「我还没好，等依赖的事件发生了再叫我」。
- **执行器（executor）**：负责不停地 `poll` 各种 `Future`、让它们前进的调度者。Rust 标准库不自带执行器，每个框架自己带：Tokio 有自己的运行时，GPUI 也有自己的执行器。**这就是「双运行时」问题的根源**——同一个进程里住着两个互不知晓的调度者。
- **`Send` 约束**：一个类型如果可以安全地在线程之间转移所有权，就实现（implement）了 `Send`。`Rc<T>`、`RefCell<T>` 等不是 `Send` 的。如果一段 future 会被丢到别的线程上去跑，它就必须是 `Send` 的；如果只在当前线程跑，就没这个要求。这个差异在本讲会反复出现。
- **RAII 与 `Drop`**：Rust 中资源的生命周期由值的生命周期管理。一个值离开作用域（或被显式 `drop`）时，它的 `Drop::drop` 方法会被调用。「drop 即取消」正是利用了这一点：任务句柄被丢弃时，顺带把任务本身也丢弃掉。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/gpui/src/gpui.rs` | gpui 的库根；`AppContext` trait 就定义在这里（L172 起） |
| `crates/gpui/src/app.rs` | `App` 结构体（根上下文）、`App::spawn`、`impl AppContext for App` |
| `crates/gpui/src/app/context.rs` | `Context<T>`（实体更新时的上下文）及其 `spawn` 方法 |
| `crates/gpui/src/app/async_context.rs` | `AsyncApp`：可以跨 await 点持有的上下文 |
| `crates/gpui/src/executor.rs` | `ForegroundExecutor` / `BackgroundExecutor`，以及从 scheduler crate 再导出的 `Task` |
| `crates/scheduler/src/executor.rs` | `Task<T>` 的真实定义、「drop 即取消」的文档承诺、`detach` / `ready` |
| `crates/gpui/src/app/test_context.rs` | `TestAppContext`：测试专用的确定性上下文 |
| `crates/gpui/src/test.rs` | `#[gpui::test]` 宏的文档与示例 |
| `crates/gpui_tokio/src/gpui_tokio.rs` | 本手册的主角，本讲只回看其中对 `AppContext` 的用法 |

> 提示：本讲引用的 `Task` 定义在 `scheduler` 这个独立 crate 里，gpui 只是通过 `pub use` 把它转手暴露成 `gpui::Task`。查源码时别在 gpui 目录里找不到定义而困惑。

## 4. 核心概念与源码讲解

### 4.1 gpui::App 与双执行器模型：前台一条线程，后台一组线程

#### 4.1.1 概念说明

GPUI 是 UI 框架，而 UI 有个铁律：**所有界面状态必须在同一个线程上访问**（macOS 上是主线程）。因此 GPUI 的并发模型是：

- **前台执行器（ForegroundExecutor）**：只有一条线程，负责所有实体（Entity）状态的读写、UI 渲染、事件处理。future 不需要 `Send`。
- **后台执行器（BackgroundExecutor）**：一组线程，承接纯计算型工作（解析文件、正则匹配、图片处理……）。future 必须 `Send`，因为它会被扔到不知道哪个线程上去。

典型协作方式是：前台任务发起工作 → 后台线程算完 → 前台任务 await 到结果并更新界面状态。gpui_tokio 的桥接代码（`cx.background_spawn`）正是站在后台执行器这一侧工作的。

#### 4.1.2 核心流程

```text
应用启动
  └─ Application::run —— 进入平台事件循环（主线程）
       └─ 每个事件循环周期，主线程驱动 ForegroundExecutor 里排队的 future
            ├─ 遇到纯计算 → 调 background_spawn 丢给线程池 → 拿到 Task
            ├─ 遇到 await 那个 Task → 主线程挂起等待（不阻塞事件循环）
            └─ 结果回来 → 更新实体状态 → cx.notify() → 重新渲染
```

#### 4.1.3 源码精读

先看两个执行器结构体。它们本质上都只是「调度器 + 平台分发器」的轻量句柄（`Clone` 成本极低）：

[crates/gpui/src/executor.rs:L16-L19](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/executor.rs#L16-L19)
后台执行器：一个指向当前运行中的执行器的指针，用于在后台线程上派生（spawn）任务。

[crates/gpui/src/executor.rs:L21-L30](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/executor.rs#L21-L30)
前台执行器：同样是指针，但注意第 29 行的 `not_send: PhantomData<Rc<()>>`——这是一个刻意的类型标记，用「伪装持有一个 `Rc`」让整个结构体**不是** `Send`，从类型系统层面保证前台执行器不可能被拿到别的线程去用。

两者的 `spawn` 签名差异浓缩了整个模型：

[crates/gpui/src/executor.rs:L112-L119](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/executor.rs#L112-L119)
后台派生：future 需要 `Send + 'static`，因为要跨线程转移；内部再装箱（box）后交给带优先级的调度。

[crates/gpui/src/executor.rs:L347-L354](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/executor.rs#L347-L354)
前台派生：future 只要求 `'static`，**不要求 `Send`**（`boxed_local` 装箱成单线程 future），因为永远只在主线程上轮询（poll）。

再看 `App` 结构体如何同时持有这两者：

[crates/gpui/src/app.rs:L679-L691](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L679-L691)
`App` 是整个应用的状态容器，第 690–691 行可以看到它内嵌（embed）了 `background_executor` 和 `foreground_executor` 两个执行器字段——所谓「上下文（cx）」，最终都是从这个结构体里取执行器、取全局状态、取实体表的。

最后是入口：桌面应用由 `Application::run` 启动平台事件循环：

[crates/gpui/src/app.rs:L231-L243](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L231-L243)
启动应用：回调会在应用完全启动后被调用一次，之后主线程进入平台事件循环。学习 gpui_tokio 时不需要真的跑起一个窗口——第 4.5 节会讲 headless（无界面）的测试用法。

#### 4.1.4 代码实践

**实践目标**：用编译器亲身体验两个执行器的 `Send` 约束差异。

**操作步骤**（示例代码，可放在任意一个能引用 gpui 的临时练习 crate 里）：

1. 写一个持有 `Rc<()>` 的 async 块（`Rc` 不是 `Send`）。
2. 分别尝试传给 `cx.background_spawn(...)` 和 `cx.foreground_executor().spawn(...)`。
3. 观察哪一个报编译错误、错误信息提到了哪个 trait 约束。

```rust
// 示例代码：仅用于观察编译行为
use std::rc::Rc;

let not_send = Rc::new(());
let fut = async move {
    let _keep = &not_send; // 让 future 持有 Rc，从而不是 Send
};

// cx.background_spawn(fut);              // 预期：编译失败，要求 Send
// cx.foreground_executor().spawn(fut);   // 预期：可以编译（不需要 Send）
```

**需要观察的现象**：`background_spawn` 一行报错，大意是 `Rc<()>` cannot be sent between threads safely / 未满足 `Send`；前台 `spawn` 一行不报这个错。

**预期结果**：报错发生在**编译期**而非运行期——这正是 Rust 把「哪段代码能跑在哪条线程」做成类型约束的意义。具体错误文案随编译器版本变化，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ForegroundExecutor` 要用 `PhantomData<Rc<()>>` 故意让自己不是 `Send`？

**参考答案**：前台执行器调度的工作全部依赖主线程上的状态（实体表、UI、非 `Send` 的 future）。如果执行器句柄本身是 `Send` 的，就可能被传到后台线程再被用来 spawn「应该在主线程跑的任务」，破坏「单线程访问 UI 状态」的铁律。用类型系统直接禁止这种可能性，比靠运行时检查和文档约定可靠。

**练习 2**：Zed 里「用正则在大文件里搜索」应该交给哪个执行器？为什么？

**参考答案**：后台执行器。它是纯 CPU 计算、耗时可能很长，若在主线程跑会卡住事件循环，界面掉帧；计算只需 `Send` 的输入输出，天然适合 `background_spawn`。算完后由前台任务 await 结果并更新 UI。

**练习 3**：`App` 结构体里 `globals_by_type` 字段的注释（app.rs L725-L729）提到「Drop globals last」，这与 gpui_tokio 有什么关系？

**参考答案**：全局状态（包括 gpui_tokio 存进去的 `GlobalTokio`）是在 `App` 销毁时最后才 drop 的。此时所有实体和任务都已被释放、任务已标记取消，之后才轮到 Tokio runtime 关停，避免有任务在 runtime 已关停后还想在其上 spawn 而导致 panic。这正是 u2-l2 将详细分析的 `GlobalTokio::drop` 时机。

### 4.2 gpui::Task：await、detach 与「drop 即取消」

#### 4.2.1 概念说明

`Task<T>` 是 GPUI 侧统一的任务句柄，地位等同于 Tokio 的 `JoinHandle<T>`。它的三条核心规则：

1. 它实现了 `Future`，可以直接 `.await`，得到任务的输出 `T`。
2. **丢弃（drop）它，任务就被立即取消**——未完成的 future 会被丢弃，里面还没执行的代码不再执行。
3. 如果你不想要结果、只想要它「在后台自己跑完」，调用 `detach()` 放手。

规则 2 是整本手册最重要的语义：gpui_tokio 文档里那句「the Tokio task will be cancelled if the GPUI task is dropped」，靠的就是 GPUI Task 的 drop 会级联触发取消。u2-l4 会看到完整的联动机制。

#### 4.2.2 核心流程

```text
spawn 得到 Task<T>
  ├── .await      → 挂起等待，拿到 T；任务完成后 Task 自然消亡
  ├── .detach()   → 放手，任务继续跑到结束，但再也无法拿到结果
  └── drop / 离开作用域 → 任务被取消，future 连同其状态一起被丢弃
                      （已完成的任务 drop 则无任何副作用）

另有便捷构造：
  Task::ready(value) → 一个立即就绪、await 必得 value 的任务
```

#### 4.2.3 源码精读

`Task` 的真实定义在 scheduler crate：

[crates/scheduler/src/executor.rs:L373-L380](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/scheduler/src/executor.rs#L373-L380)
`Task` 的官方文档承诺，一字千金：实现了 `Future` 可以 `.await`；**如果丢弃（drop）一个任务，它会被立即取消**；调用 `detach` 可以让任务继续运行。注意结构体上的 `#[must_use]`——如果你写下 `cx.spawn(...);` 却不使用返回值，编译器会警告，因为语句结束时临时 `Task` 被丢弃，任务还没开始就结束了。

[crates/scheduler/src/executor.rs:L515-L519](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/scheduler/src/executor.rs#L515-L519)
`Task::ready`：构造一个「已经完成」的任务。gpui_tokio 之外的很多测试和合成（synthesize）路径都用它来模拟立即就绪的异步结果。

[crates/scheduler/src/executor.rs:L549-L557](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/scheduler/src/executor.rs#L549-L557)
`detach`：按内部状态分派（dispatch）——已就绪的无事可做，运行中的交给底层 async_task 的 detach，之后任务与句柄脱钩，跑到完成为止。

gpui 只是把 `Task` 从 scheduler crate 转手导出：

[crates/gpui/src/executor.rs:L9-L11](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/executor.rs#L9-L11)
gpui 暴露 `scheduler::{DedicatedExecutor, FallibleTask, LocalExecutor, Priority, Task}`——所以 `gpui::Task` 与 `scheduler::Task` 是同一个类型。gpui_tokio 第 3 行 `use gpui::{App, AppContext, Global, ReadGlobal, Task};` 引入的就是它。

对返回 `Result` 的任务，gpui 还提供了一个常用的扩展 trait：

[crates/gpui/src/executor.rs:L32-L41](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/executor.rs#L32-L41)
`TaskExt::detach_and_log_err`：对 `Task<Result<T, E>>` 「放手并记日志」——任务失败时把错误打出来而不是无声吞掉。Zed 工程规范明确反对 `let _ =` 丢弃可失败操作，这个方法就是「想 detach 又不想丢错误」的标准答案。

#### 4.2.4 代码实践

**实践目标**：通过阅读确认「drop 即取消」是文档承诺而非实现巧合，并理解 `#[must_use]` 的保护作用。

**操作步骤**：

1. 打开 [crates/scheduler/src/executor.rs:L373-L380](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/scheduler/src/executor.rs#L373-L380)，抄下文档原文。
2. 在 Zed 仓库里搜索 `#[must_use]` 加在 `Task` 上的位置（同上）。
3. 在任意已有 GPUI 测试里加一行 `cx.background_spawn(async { println!("会被打印吗"); });`（不接收返回值），编译并运行。

**需要观察的现象**：第 3 步会先得到一个 `unused` 警告（`#[must_use]` 生效）；即便忽略警告运行，该打印**大概率也不会出现**——语句结束时临时 `Task` 被 drop，任务被取消。

**预期结果**：打印不出现；警告信息原文待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`Task` drop 时，future 内部已经执行过的部分会回滚吗？

**参考答案**：不会。Rust 的取消是「停止前进」而不是「撤销已做的事」：future 在上一个 await 点之后、下一个 await 点之前已产生的副作用（写文件、发请求、打印）都会保留。所以写任务时要自己保证「做到一半被取消」是安全的，必要时在 drop 时做清理——u2-l4 的 `defer` 守卫正是这种思路的镜像（GPUI 侧 drop 触发 Tokio 侧 abort）。

**练习 2**：`task.detach()` 之后还能拿到任务的输出吗？

**参考答案**：不能。detach 的语义是「句柄与任务脱钩，任务跑完即弃」，文档原话是 "allows the task to continue running, but with no way to return a value"。需要结果就必须 await；既不要结果又可能出错就用 `detach_and_log_err`。

**练习 3**：`Task::ready(v)` 和直接写 `v` 有什么区别？什么时候用前者？

**参考答案**：值完全一样，类型不同：前者是「立即完成的任务」，可以出现在任何需要 `Task<T>` 的位置，比如某个函数签名规定必须返回 `Task<R>`，而你手头的结果是同步算出来的。测试里也常用它构造「假装异步完成」的桩（stub）。

### 4.3 cx.spawn 与 cx.background_spawn：两种任务的入口

#### 4.3.1 概念说明

日常代码里我们不直接碰执行器，而是通过上下文（cx）上的两个方法：

- `cx.spawn(...)`：**前台任务**。闭包拿到一个 `AsyncApp`（可跨 await 点访问应用状态的上下文），整个任务在主线程上跑，适合「调度工作、等结果、改实体状态」的协调型逻辑。
- `cx.background_spawn(...)`：**后台任务**。把一个 `Send` 的 future 直接丢进线程池，适合纯计算。它返回普通 `Task<R>`，通常由某个前台任务 await。

一个关键细节：**前台 `spawn` 是各种上下文类型各自的固有（inherent）方法，而 `background_spawn` 是 `AppContext` trait 的方法**。这个不对称不是偶然——下一节会看到它直接决定了 gpui_tokio 的泛型写法。

#### 4.3.2 核心流程

```text
cx.spawn(async move |cx: &mut AsyncApp| { ... })
  └─ App::spawn
       └─ ForegroundExecutor::spawn（boxed_local，主线程轮询）
            返回 Task<R>

cx.background_spawn(async move { ... })
  └─ AppContext::background_spawn（trait 方法）
       └─ BackgroundExecutor::spawn（Send + 线程池）
            返回 Task<R>

常见组合：
cx.spawn(async move |cx| {
    let data = cx.background_spawn(heavy_parse(file)).await; // 前台等后台
    entity.update(cx, |item, cx| { item.set(data); cx.notify(); }) // 回到主线程改状态
})
```

#### 4.3.3 源码精读

前台入口，以 `App::spawn` 为例：

[crates/gpui/src/app.rs:L1936-L1952](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L1936-L1952)
`App::spawn`：接收一个异步闭包，闭包参数是 `&mut AsyncApp`，允许跨 await 点访问应用状态；内部转手给前台执行器。注意它只是 `App` 的固有方法，不在任何 trait 上。

更新实体时用的是 `Context<T>::spawn`，形态多了一个弱句柄：

[crates/gpui/src/app/context.rs:L233-L245](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app/context.rs#L233-L245)
`Context<T>::spawn`：闭包签名是 `AsyncFnOnce(WeakEntity<T>, &mut AsyncApp)`——先取实体的**弱句柄**再转交给 `App::spawn`。弱句柄保证异步任务不会强行续命实体：实体若已销毁，`update` 会失败返回而不是悬垂访问。这就是 Zed 代码里随处可见的 `cx.spawn(async move |this, cx| ...)` 中 `this` 的来历。

后台入口则是 trait 方法，`App` 的实现只有一行：

[crates/gpui/src/app.rs:L2860-L2865](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L2860-L2865)
`impl AppContext for App` 中的 `background_spawn`：直接委托（delegate）给 `background_executor.spawn(future)`。签名上 `future: impl Future<Output = R> + Send + 'static`、`R: Send + 'static`——future 和输出都必须能跨线程。

真实调用形态可以看 vim 插件里的一行：

[crates/vim/src/state.rs:L747](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/vim/src/state.rs#L747)
一个典型的后台任务：`cx.background_spawn(async move { db.delete_mark(workspace_id, path, mark_name).await })`，把数据库删除操作交给线程池，返回的 `Task` 由调用方持有或 await。「发起在主线程、执行在线程池、结果回主线程」的三角关系在此一目了然。

#### 4.3.4 代码实践

**实践目标**：用搜索统计感受两种 spawn 在 Zed 代码库中的分工。

**操作步骤**：

1. 在 Zed 仓库根目录执行（只读操作）：
   - `git grep -c "cx.spawn(" -- "crates/*/src/**/*.rs" | sort -t: -k2 -rn | head -20`
   - `git grep -c "background_spawn" -- "crates/*/src/**/*.rs" | sort -t: -k2 -rn | head -20`
2. 从第二份清单里任选一个文件，读它的 `background_spawn` 调用点前后各 10 行，回答：这个后台任务的结果被谁 await？await 发生在前台还是后台？

**需要观察的现象**：两种调用都大量存在；`background_spawn` 的结果几乎总被某个 `cx.spawn(...)` 内的 async 块 await，或者被存进结构体字段。

**预期结果**：能找到至少一处「前台任务 await 后台任务」的完整闭环（例如上面引用的 vim/state.rs，或 repl 模块）。各文件的精确计数随代码演进变化，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`cx.spawn(async move |this, cx| ...)` 里的 `this` 是什么？为什么给弱句柄而不是 `Entity<T>`？

**参考答案**：`this: WeakEntity<T>`，即当前实体的弱引用（weak handle）。强句柄 `Entity<T>` 会维持实体存活，异步任务一旦被遗忘（比如被 detach）就会让实体永远无法释放；弱句柄不续命，实体销毁后通过它 `update` 只会得到 `Err`。这是 GPUI 防止「实体与任务互相引用导致内存泄漏」的标准手段。

**练习 2**：把「读取大文件内容」拆成前后台两段，各用哪个 spawn？

**参考答案**：读文件本身（阻塞 I/O、无需访问实体状态）用 `cx.background_spawn`；「拿到内容后更新编辑器实体并刷新界面」用 `cx.spawn`，在其中 await 前一个任务。反过来是错的：阻塞 I/O 放前台会卡住事件循环；改实体状态放后台会违反单线程访问规则（而且 `AsyncApp` 也无法在后台 future 里使用）。

**练习 3**：`background_spawn` 的 future 里能使用 `cx` 去更新实体吗？

**参考答案**：不能。`AppContext::background_spawn` 的闭包根本拿不到任何上下文参数（签名只有一个 future），而且后台 future 必须 `Send`，上下文句柄多为非 `Send`。一切状态更新必须回到前台任务里做——这正是「协调在前台、计算在后台」的分界线。

### 4.4 AppContext trait：为什么 gpui_tokio 只要求 `C: AppContext`

#### 4.4.1 概念说明

GPUI 的上下文种类很多：`App`、`Context<T>`、`AsyncApp`、测试里的 `TestAppContext`……如果 gpui_tokio 的 `Tokio::spawn` 写死 `cx: &mut App`，那么在 `Context<T>` 或测试上下文里就用不了。解决办法是面向 trait 编程：`AppContext` 把「各种上下文都能做的操作」抽象出来，`Tokio::spawn` 只依赖这个最小接口。

回看 u1-l1 读过的桥接函数签名：`pub fn spawn<C, Fut, R>(cx: &C, f: Fut) -> Task<Result<R, JoinError>> where C: AppContext`——注意它内部只用了 `read_global` 和 `background_spawn` 两个操作，**恰好都是 `AppContext` 的 trait 方法**，所以任何实现了该 trait 的上下文都能直接传入。

#### 4.4.2 核心流程

```text
gpui_tokio::Tokio::spawn(cx, future)
  │  约束：C: AppContext
  │
  ├─ cx.read_global(|tokio: &GlobalTokio, cx| {   })   ← trait 方法：读全局单例
  │     ├─ tokio.handle.spawn(f)                       ← 交给 Tokio 运行时（下一讲）
  │     └─ cx.background_spawn(async move { ... })     ← trait 方法：后台等待结果
  └─ 返回 Task<Result<R, JoinError>>
```

实现这个 trait 的上下文（部分）：`App`、`Context<T>`、`AsyncApp`、`TestAppContext`、`VisualTestContext` 等——同一个 `Tokio::spawn` 对它们全部可用。

#### 4.4.3 源码精读

[crates/gpui/src/gpui.rs:L170-L245](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/gpui.rs#L170-L245)
`AppContext` trait 完整定义。文档注释点明设计意图：**让 GPUI 中不同的上下文可以被互换使用**。接口包括建实体、更新实体、更新/读取窗口，以及本讲关心的两个方法。

[crates/gpui/src/gpui.rs:L236-L239](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/gpui.rs#L236-L239)
trait 方法 `background_spawn`：在后台线程上派生（spawn）一个 future。这是 gpui_tokio 唯一用到的「执行」能力。

[crates/gpui/src/gpui.rs:L241-L244](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/gpui.rs#L241-L244)
trait 方法 `read_global`：带回调地读取全局状态。gpui_tokio 靠它从全局里拿到 `GlobalTokio`（读取时还会把 `&App` 一并传进回调，方便二次操作）。

对照两个实现，确认它们行为一致：

[crates/gpui/src/app.rs:L2867-L2873](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L2867-L2873)
`App` 的 `read_global` 实现：从全局表按类型取出 `&G` 后调用回调。

[crates/gpui/src/app/test_context.rs:L109-L114](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app/test_context.rs#L109-L114)
`TestAppContext` 的 `background_spawn` 实现：同样委托给后台执行器。测试上下文因此天然满足 gpui_tokio 的约束。

最后回到主角，看它如何只靠这两个方法完成桥接：

[crates/gpui_tokio/src/gpui_tokio.rs:L55-L73](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L55-L73)
`Tokio::spawn` 全文：`cx.read_global` 拿到 Tokio 句柄并派生（spawn） future，取出 `abort_handle` 做**取消守卫（cancel guard）**（u2-l4 专题），最后 `cx.background_spawn` 等待 JoinHandle 的结果并包装成 GPUI `Task` 返回。整段代码没有出现任何具体上下文类型——`C: AppContext` 四个字让它对 `App`、`Context<T>`、`TestAppContext`……全体通用。

#### 4.4.4 代码实践

**实践目标**：体会「面向 trait 的接口」在泛型代码中的约束传导。

**操作步骤**：

1. 阅读 [crates/gpui_tokio/src/gpui_tokio.rs:L55-L73](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L55-L73)，列出函数体用到的全部 `cx.` 方法。
2. 在 [crates/gpui/src/gpui.rs:L170-L245](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/gpui.rs#L170-L245) 里逐一核对这些方法是否都在 `AppContext` trait 上。
3. 思考实验（不必运行）：若把函数体里加一行 `cx.spawn(...)`（前台 spawn，固有方法），泛型约束 `C: AppContext` 还够用吗？

**需要观察的现象**：第 1 步只能找到 `read_global` 和 `background_spawn` 两个调用，均在 trait 上。

**预期结果**：第 3 步的答案是不够——前台 `spawn` 不在 `AppContext` trait 上，`C: AppContext` 的泛型上下文不一定有这个方法，代码将无法编译（除非收紧为具体类型）。这解释了桥接代码「只用后台」的形态：一方面语义上只需要等待，另一方面类型上也只允许等待。

#### 4.4.5 小练习与答案

**练习 1**：为什么 gpui_tokio 不把签名写成 `cx: &mut App` 更简单直接？

**参考答案**：写成 `&mut App` 后，调用方若手头是 `Context<T>`（最常见的事件回调环境）或测试里的 `TestAppContext`，就必须先「升级」上下文再调用，处处转换。`C: AppContext` 让函数与具体上下文解耦，任何实现该 trait 的现在和未来的上下文都能直接使用——这是 Rust API 设计里「接受窄接口，服务宽受众」的典型做法。

**练习 2**：`read_global` 的回调签名是 `FnOnce(&G, &App) -> R`，为什么同时传 `&App`？

**参考答案**：回调里往往还要做后续操作（比如 gpui_tokio 里紧接着的 `cx.background_spawn`）。把 `&App` 一并传入，避免回调方再去想方设法获取应用引用，一次闭包搞定「读全局 + 用应用上下文做后续事」。

**练习 3**：`Tokio::spawn` 为什么用后台 `background_spawn` 而不是前台 `spawn` 去 await Tokio 的 `JoinHandle`？

**参考答案**：等待外部运行时的任务本质是「睡觉等结果」，没有界面协调工作，放在后台线程池不占用宝贵的主线程时间片；同时它要求 future 是 `Send` 的（`JoinHandle` 满足），与 `background_spawn` 的约束天然匹配。如果放前台，大量这样的等待任务会挤占 UI 事件循环的吞吐。

### 4.5 测试上下文 TestAppContext：headless 跑 GPUI 任务

#### 4.5.1 概念说明

想验证并发行为，不必启动真窗口。gpui 提供 `test-support` 特性（feature）：`#[gpui::test]` 宏会给测试函数注入一个 `TestAppContext`，它同样实现 `AppContext`，但底层接的是**确定性测试调度器**——所有任务（前台后台）都由同一个假的调度队列驱动，定时器用假时钟，测试因此可复现。驱动队列的把手是 `run_until_parked()`：把当前能跑的任务全部跑完（必要时推进假时钟）再返回。

这也是 u3-l3 的伏笔：Zed 各 crate 的测试里那句 `gpui_tokio::init(cx)` 之所以必要，正因为**每个测试的 `TestAppContext` 都有自己独立的全局状态**——不在本测试里 init，全局里就没有 `GlobalTokio`。

#### 4.5.2 核心流程

```text
#[gpui::test]
async fn my_test(cx: &TestAppContext) {
    cx.spawn(...) / cx.background_spawn(...)   // 任务入队，但不会立刻执行
    cx.run_until_parked();                      // 驱动：能跑的跑完，定时器推进假时钟
    // 断言结果
}

假时钟规则（要点）：
  - tick() 一次只跑一个任务轮次，不推进时钟
  - run_until_parked() 在没有可跑任务时会推进时钟到下一个定时器再继续
```

#### 4.5.3 源码精读

[crates/gpui/src/test.rs:L1-L27](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/test.rs#L1-L27)
测试模块文档：GPUI 提供一等公民的测试支持，包含运行依赖上下文之测试的宏，以及一个**保证测试在任意并行度下都确定可复现**的执行器实现；示例即 `async fn test_example(cx: &TestAppContext)`。

[crates/gpui/src/app/test_context.rs:L18-L34](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app/test_context.rs#L18-L34)
`TestAppContext` 结构体：同样持有 `background_executor` 与 `foreground_executor` 两个**公开**字段（练习里会直接用 `cx.background_executor` 创建定时器），以及内部共享的 `app: Rc<AppCell>`——测试上下文与它构造出的 `App` 共享同一份状态。

[crates/gpui/src/app/test_context.rs:L424-L432](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app/test_context.rs#L424-L432)
测试版 `spawn`：在主线程（测试线程）上运行给定任务，闭包拿到 `AsyncApp`。

[crates/gpui/src/app/test_context.rs:L475-L478](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app/test_context.rs#L475-L478)
`run_until_parked`：等到没有任何待处理任务为止——测试中最常用的「让子弹飞一会儿」。

[crates/gpui/src/executor.rs:L183-L192](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/executor.rs#L183-L192)
`BackgroundExecutor::timer`：返回一个在指定时长后完成的任务；注释说明测试中使用假时钟。**注意它返回的也是 `Task<()>`**——定时器本身就是任务，同样受 drop 取消语义约束。Zed 的项目规范明确建议测试用 GPUI 的这个 timer 而不是 `smol::Timer`，原因见 [crates/gpui/src/executor.rs:L212-L222](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/executor.rs#L212-L222) 的 `run_until_parked` 文档：测试调度器会配合假时钟推进，外来定时器不被它追踪，可能让测试「无事可跑」而提前结束。

[crates/gpui/src/executor.rs:L206-L210](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/executor.rs#L206-L210)
`tick`：测试中**只运行一个任务**。综合实践里用它制造「任务跑到一半停下」的精确状态。

#### 4.5.4 代码实践（本讲主实践·上篇）

**实践目标**：跑通第一个 headless GPUI 测试，观察两种 spawn 的行为。

**操作步骤**：

1. 在练习分支上给 `crates/gpui_tokio/Cargo.toml` 追加测试依赖（勿提交主分支）：

   ```toml
   [dev-dependencies]
   gpui = { workspace = true, features = ["test-support"] }
   ```

2. 新建 `crates/gpui_tokio/tests/executor_practice.rs`：

   ```rust
   use gpui::TestAppContext;

   #[gpui::test]
   async fn test_two_spawns_print(cx: &TestAppContext) {
       // 任务存进 Vec 持有，避免语句结束时临时 Task 被 drop 而取消
       let tasks = vec![
           cx.spawn(|_cx| async {
               println!("[1] 前台任务：运行在主（测试）线程");
           }),
           cx.background_spawn(async {
               println!("[2] 后台任务：由测试调度器驱动");
           }),
       ];

       cx.run_until_parked(); // 驱动所有任务
       drop(tasks);           // 任务已完成，此时 drop 无副作用
   }
   ```

3. 运行（`--nocapture` 让 println 可见）：

   ```bash
   cargo test -p gpui_tokio --test executor_practice -- --nocapture
   ```

**需要观察的现象**：两行打印都出现。顺序由确定性测试调度器决定（同一队列按派生顺序执行，先派生的先打印），但真实应用中后台任务跑在线程池上**没有顺序保证**，不应依赖此顺序。

**预期结果**：测试通过、两行打印出现；具体先后顺序待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：把第 2 步里的 `let tasks = vec![...]` 改成两条独立的 `cx.spawn(...);` 语句（不接收返回值），会发生什么？

**参考答案**：编译器给出「未使用的必须使用（must_use）值」警告；运行时两行打印都不会出现——每条语句结束时临时 `Task` 被 drop，任务随即被取消。这是「drop 即取消」最直接的实验证据，也解释了为什么实践中 spawn 的返回值要么被 await、要么 `detach`、要么存进字段。

**练习 2**：为什么测试里推荐 `cx.background_executor.timer(...)` 而不是 `smol::Timer::after(...)`？

**参考答案**：GPUI 的定时器注册在测试调度器的假时钟上，`run_until_parked` 能推进它让任务按时完成；`smol` 定时器走真实时间、不被调度器追踪，`run_until_parked` 看到没有可跑任务就返回，定时任务永远等不到触发，测试表现为「卡住然后失败」。

**练习 3**：两个不同的 `#[gpui::test]` 测试函数各自 `cx.set_global(...)`，会互相覆盖吗？

**参考答案**：不会。每个测试拿到独立的 `TestAppContext`，背后是各自的 `App` 与全局表（test_context.rs 的 `app: Rc<AppCell>` 每个测试一份）。这也是为什么每个需要 Tokio 的测试都要自己调用一次 `gpui_tokio::init(cx)`——全局不跨测试共享（详见 u3-l3）。

## 5. 综合实践

把本讲的三个知识点（双执行器、drop 即取消、测试驱动）串成一个实验。**在练习分支上**继续编辑第 4.5 节的 `crates/gpui_tokio/tests/executor_practice.rs`，追加一个取消实验：

```rust
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

#[gpui::test]
async fn test_drop_cancels_a_parked_task(cx: &gpui::TestAppContext) {
    // —— 实验组：任务挂在定时器上时被 drop ——
    let finished = Arc::new(AtomicBool::new(false));
    let timer = cx.background_executor.timer(Duration::from_secs(60)); // 假时钟的 60 秒

    let task = cx.background_spawn({
        let finished = finished.clone();
        async move {
            println!("[bg] 第一步：任务启动，即将等待 60 秒");
            timer.await; // 任务在这里挂起（假时钟不会自己走）
            println!("[bg] 第二步：定时器到期，任务完成");
            finished.store(true, Ordering::SeqCst);
        }
    });

    // 只推进一个轮次：第一步打印，任务停在 timer 上
    assert!(cx.background_executor.tick());

    drop(task);            // 关键操作：句柄丢弃 → 任务取消
    cx.run_until_parked(); // 时钟再怎么推进，第二步也不会执行

    assert!(!finished.load(Ordering::SeqCst), "被 drop 的任务不应完成");
    println!("实验组验证通过：drop 即取消");

    // —— 对照组：不 drop，任务应正常完成 ——
    let finished2 = Arc::new(AtomicBool::new(false));
    let timer2 = cx.background_executor.timer(Duration::from_secs(60));
    let task2 = cx.background_spawn({
        let finished = finished2.clone();
        async move {
            timer2.await;
            finished.store(true, Ordering::SeqCst);
        }
    });
    cx.run_until_parked(); // 推进假时钟 60 秒，任务完成
    assert!(task2.is_ready());
    assert!(finished2.load(Ordering::SeqCst), "未被 drop 的任务应完成");
    println!("对照组验证通过：正常完成");
}
```

运行：

```bash
cargo test -p gpui_tokio --test executor_practice -- --nocapture
```

**验收标准**：

1. 实验组只打印「第一步」，断言 `!finished` 通过——任务在 await 点被取消。
2. 对照组 `finished` 为真，且 `task2.is_ready()`（[Task::is_ready](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/scheduler/src/executor.rs#L540-L547) 可查询任务是否已结束）。
3. 能用自己的话解释：取消发生在哪个 await 点、为什么第二步的 println 永远没有机会执行。

> 注：`tick()` 的「一轮」具体覆盖到 future 的哪个暂停点、测试输出中两组日志的交错顺序，依赖测试调度器实现细节，**待本地验证**；若断言行为与预期不符，请对照 [crates/gpui/src/executor.rs:L206-L222](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/executor.rs#L206-L222) 的文档修正理解，这正是练习的一部分。

## 6. 本讲小结

- GPUI 采用**双执行器**：前台 `ForegroundExecutor` 单线程跑所有状态与 UI（future 免 `Send`，结构体用 `PhantomData<Rc<()>>` 禁止跨线程）；后台 `BackgroundExecutor` 是线程池（future 必须 `Send`）。
- `Task<T>` 是 GPUI 的任务句柄：可 await、可 `detach()`、可 `Task::ready` 构造；**drop 即立即取消**，且带 `#[must_use]` 防止误丢。
- `cx.spawn`（前台、拿 `AsyncApp`、`Context<T>` 版还带 `WeakEntity` 弱句柄）负责协调与状态更新；`cx.background_spawn`（`AppContext` trait 方法）负责纯计算；标准形态是「前台 await 后台」。
- 前台 `spawn` 是各上下文的固有方法，`background_spawn` / `read_global` 是 `AppContext` trait 方法——gpui_tokio 的 `Tokio::spawn<C: AppContext>` 正是只依赖这两个 trait 方法，才做到对 `App`、`Context<T>`、`TestAppContext` 全体通用。
- 测试用 `#[gpui::test]` + `TestAppContext` headless 运行：确定性调度、假时钟定时器（`cx.background_executor.timer`）、`run_until_parked` 驱动；每个测试的全局状态独立，为 u3-l3 的 `gpui_tokio::init` 测试模式埋下伏笔。

## 7. 下一步学习建议

本讲补齐了「河的这一岸」：GPUI 的任务如何派生、等待、取消。下一讲 **u1-l4（Tokio 运行时、Handle 与 JoinHandle 入门）** 补齐另一岸：`Builder::new_multi_thread` 如何建运行时、`Handle` 为何能脱离 `Runtime` 独立派生、`JoinHandle::abort_handle` 的取消机制。学完 u1-l4 后，`Tokio::spawn` 里那句「先 `handle.spawn`、再取 `abort_handle`、最后 `background_spawn` 等待」的四步就完全透明了，届时可进入第二单元（u2-l1 初始化流程）逐段精读。

继续阅读建议：

- [crates/scheduler/src/executor.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/scheduler/src/executor.rs) 的 `FallibleTask` 与 `Scope`——`Task` 家族的其他成员。
- [crates/gpui/src/app/async_context.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app/async_context.rs)——`AsyncApp` 如何安全地跨 await 点访问应用状态。
- 仓库根 [CLAUDE.md](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/CLAUDE.md) 的 Concurrency 一节——Zed 团队自己总结的任务语义规范。
