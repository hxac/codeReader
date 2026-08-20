# WebDispatcher：浏览器主线程邮箱与 wasm 单线程世界

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 wasm 环境对线程与任务调度的天然限制：为什么「多线程 wasm」需要 `SharedArrayBuffer` + `Atomics` + 跨域隔离，为什么主线程不能阻塞等待。
2. 讲解 `MainThreadMailbox` 与 `MainThreadItem` 的设计：后台 worker 如何把「必须在主线程执行的任务」投进一个邮箱，再用 `Atomics` 唤醒协议把浏览器主线程叫醒。
3. 逐方法对照 `PlatformDispatcher` 契约与浏览器调度原语（`setTimeout`、`queueMicrotask`、`requestIdleCallback`、`Atomics.waitAsync`）的映射。
4. 区分 `application()`、`application_with_web_backend()`、`single_threaded_web()` 三个入口在多线程开关上的差别。

本讲承接 u4-l2 建立的 `PlatformDispatcher` 契约认知（五个必需方法、`RunnableVariant` 信封、60/30/10 加权抽签、`RealtimeAudio` 永不入队），并与 u4-l3 的 LinuxDispatcher、u4-l4 的 MacDispatcher / WindowsDispatcher 形成四方对照。

## 2. 前置知识

### 2.1 浏览器事件循环的三类任务

浏览器主线程在同一时刻只能做一件事，所有工作都要排队：

- **宏任务（macrotask）**：`setTimeout(fn, 0)`、事件回调、I/O 回调。每两个宏任务之间浏览器有机会渲染一帧。
- **微任务（microtask）**：`queueMicrotask(fn)`、Promise 的 `.then`。当前宏任务结束后、下一次渲染前**清空全部**微任务。
- **空闲任务**：`requestIdleCallback(fn)`。浏览器在一帧的渲染工作做完后、还有富余时间时才执行，回调收到一个 `IdleDeadline`，可用 `time_remaining()` 查询剩余额度。Safari 不支持该 API。

### 2.2 wasm 的「线程」是什么

标准 wasm 模块是单线程的。「多线程 wasm」的真实形态是：**多个 Web Worker 共享同一段 WebAssembly 线性内存**。要让浏览器允许共享，必须满足：

- 页面处于**跨域隔离**状态：响应头 `Cross-Origin-Opener-Policy: same-origin` + `Cross-Origin-Embedder-Policy: require-corp`；
- wasm 模块以 `--shared-memory` 链接，内存是 `SharedArrayBuffer`；
- 编译目标开启 `+atomics`。

跨域隔离之后，JS 侧才暴露 `SharedArrayBuffer` 与完整的 `Atomics` API。

### 2.3 Atomics 的四个操作与主线程禁令

`Atomics` 操作的是 `SharedArrayBuffer`（或 wasm 共享内存视图）上的整型单元：

- `Atomics.store(view, i, v)`：原子写入。
- `Atomics.notify(view, i)`：叫醒正在 `wait` 这个单元的等待者。
- `Atomics.wait(view, i, expected)`：**阻塞**等待，直到值变得不等于 `expected` 或被 notify。**在浏览器主线程上调用会直接抛异常**——主线程阻塞等于卡死整个页面。
- `Atomics.waitAsync(view, i, expected)`：`wait` 的非阻塞版本，立即返回一个结果对象；若值已经不等于 `expected`，其 `async` 属性为 `false`（无需等待）；否则 `async: true` 且 `value` 是一个 Promise，resolve 即「被唤醒」。

这条「主线程禁止阻塞」的禁令同样解释了本讲会反复看到的 spin 变体：**主线程上连互斥锁都不能阻塞获取**，只能自旋尝试。

### 2.4 其他背景

- `std::thread::ThreadId`：在启用线程的 wasm 目标上，主线程与每个 Web Worker 各有独立 `ThreadId`，所以「我是不是主线程」可以用 `ThreadId` 相等判断。
- `std::time::Instant` 在 `wasm32-unknown-unknown` 上不可用，web 侧用 `web_time::Instant` 替代。
- wasm-bindgen 的 `Closure::once_into_js(fn)` 把 Rust 闭包一次性转换成 JS 回调（消耗闭包、交给 JS GC），才能传给 `setTimeout` 等 API。
- `wasm_bindgen_futures::spawn_local(fut)` 在**当前线程**的本地 future 队列上驱动一个非 `Send` 的 async 块。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/gpui_web/src/dispatcher.rs` | 本讲主角：`WebDispatcher`、`MainThreadMailbox`、`MainThreadItem`，以及 `setTimeout` / `queueMicrotask` / `requestIdleCallback` 的封装函数 |
| `crates/gpui_web/src/gpui_web.rs` | gpui_web 的 crate 根：整文件被 `#![cfg(target_family = "wasm")]` 门控，再导出 `WebDispatcher` 等 |
| `crates/gpui_platform/src/gpui_platform.rs` | 门面入口：`application` / `application_with_web_backend` / `single_threaded_web` / `web_init` |
| `crates/gpui_web/src/platform.rs` | `WebPlatform::new_with_backend`：创建 dispatcher 并派生前后台两个执行器（辅助） |
| `crates/gpui/src/queue.rs` | `PriorityQueueSender/Receiver` 与 `spin_send` / `spin_try_pop` 变体（辅助，容器本身 u4-l2 已讲） |
| `crates/gpui_web/src/http_client.rs` | 邮箱的真实跨线程用户：`FetchHttpClient`（辅助） |
| `crates/gpui_web/Cargo.toml` | `multithreaded` feature 的定义与透传（辅助） |
| `crates/gpui_web/examples/hello_web/trunk.toml`、`.../.cargo/config.toml` | 跨域隔离响应头与原子链接参数（实践素材） |

## 4. 核心概念与源码讲解

### 4.1 wasm 的线程世界：编译开关与运行期能力探测

#### 4.1.1 概念说明

WebDispatcher 面对的第一个问题是：**「我到底有没有线程可用？」**这个问题的答案由两层共同决定：

1. **编译期**：`gpui_web` 的 `multithreaded` feature 是否开启（默认开启）。关掉它，`wasm_thread` 依赖与 `scheduler` 的 wasm 线程支持根本不会参与编译。
2. **运行期**：即使编译期允许，当前浏览器页面也可能没有跨域隔离，`SharedArrayBuffer` / `Atomics.waitAsync` 不可用，此时必须优雅回退到单线程。

所以 WebDispatcher 是「一份代码、两种人格」：多线程人格（有 worker 池 + 邮箱唤醒）与单线程人格（一切任务都排到主线程浏览器事件循环）。

#### 4.1.2 核心流程

```text
WebDispatcher::new(browser_window, allow_threads):
    编译期开关 = cfg!(feature = "multithreaded")
    运行期探测 = shared_memory_supported() && wait_async_supported()
    supports_threads = 编译期开关 && allow_threads && 运行期探测

    if supports_threads:
        启动主线程 waker loop（邮箱的消费者，见 4.4）
        启动 N = max(navigator.hardwareConcurrency, 2) 个 wasm worker
    else if 编译期开关 && allow_threads:
        打警告日志，回退单线程人格
    else:
        静默使用单线程人格
```

#### 4.1.3 源码精读

先看 feature 定义。[gpui_web/Cargo.toml:12-14](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/Cargo.toml#L12-L14)：`multithreaded` 默认开启，拉起 `wasm_thread`（Zed fork 的、支持新版 wasm-bindgen 参数的版本）并给 `scheduler` 打开 `wasm-threads`。

两个运行期探测函数。[dispatcher.rs:14-23](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L14-L23)：`shared_memory_supported` 检查三件事——全局存在 `SharedArrayBuffer`、全局存在 `Atomics`、**当前模块的 `WebAssembly.Memory.buffer()` 确实是 `SharedArrayBuffer` 实例**。第三个检查最关键：即使 API 存在，若模块不是用 `--shared-memory` 链接的，内存在 worker 之间依然是各拷贝一份，「共享」无从谈起。

[dispatcher.rs:25-35](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L25-L35)：`wait_async_supported` 探测 `Atomics.waitAsync` 是否为函数——它是邮箱唤醒协议（4.4）的必要原语，部分旧浏览器没有。

探测结果的消费点在构造函数里。[dispatcher.rs:164-175](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L164-L175)：`supports_threads` 是四个条件的与；条件不满足且「本想多线程」时打一条 warn 日志说明回退。

工程上这两个探测对应的搭建要求可以在 hello_web 示例里看到。[trunk.toml:6-7](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/examples/hello_web/trunk.toml#L6-L7)：dev 服务器显式发送 COOP/COEP 两个响应头，注释直说是「WebGPU / SharedArrayBuffer 支持」所需；[.cargo/config.toml:2-10](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/examples/hello_web/.cargo/config.toml#L2-L10)：rustflags 打开 `+atomics` 并传 `--shared-memory` 等链接参数、导出 TLS 初始化符号（wasm 线程的栈上 TLS 需要）。

#### 4.1.4 代码实践

1. **实践目标**：搞清「让多线程 wasm 跑起来」需要哪几件配套，并能把每件配套对应到代码里的一个探测条件。
2. **操作步骤**：
   - 打开 `crates/gpui_web/examples/hello_web/trunk.toml` 与 `.cargo/config.toml`，逐行写出每个 header / rustflag 的作用；
   - 对照 `shared_memory_supported()` 的三个条件，标注「哪个配置项决定哪个条件为真」；
   - 若本地能跑 `trunk serve`（见 u7-l3），用浏览器 DevTools 的 Network 面板查看文档响应头中是否有 COOP/COEP，再在控制台执行 `typeof SharedArrayBuffer`。
3. **需要观察的现象**：带隔离头时 `typeof SharedArrayBuffer === "function"`；去掉 trunk.toml 里那两行 header 重启后，页面控制台应出现 dispatcher.rs:172-174 的回退警告（「Required WebAssembly threading APIs are unavailable...」）。
4. **预期结果**：三张配置（feature、链接参数、响应头）与三个探测条件一一对应；缺任何一个，应用仍能运行但落入单线程人格。
5. 本地无 wasm 工具链时，前两步（纯阅读）即可完成；运行部分**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`shared_memory_supported` 为什么除了检查全局 API，还要确认 `memory.buffer()` 是 `SharedArrayBuffer` 的实例？
**答案**：前两个检查只说明「浏览器支持这套 API」；第三个检查确认**本模块的内存真的以共享方式链接**。若模块没用 `--shared-memory` 链接，每个 worker 拿到的是内存的独立副本，写互不可见，线程协作无从谈起。

**练习 2**：把 `multithreaded` feature 关掉（`default-features = false`）后，`MIN_BACKGROUND_THREADS` 常量与 `wasm_thread` 依赖还存在吗？
**答案**：都不参与编译。常量带 `#[cfg(feature = "multithreaded")]`（dispatcher.rs:11-12），`wasm_thread` 在 Cargo.toml 中是 `optional = true` 且只被该 feature 拉起。

**练习 3**：为什么 `wait_async_supported` 要单独探测，而不并入 `shared_memory_supported`？
**答案**：两者独立演进——页面可能跨域隔离了（SAB 可用）但浏览器没实现 `waitAsync`。缺了 `waitAsync`，4.4 的主线程唤醒协议无法工作，所以它单独构成 `supports_threads` 的一个条件。

### 4.2 WebDispatcher：一份代码、两种人格

#### 4.2.1 概念说明

`WebDispatcher` 是 `PlatformDispatcher` 契约在浏览器上的实现。它的全部状态是：

- `main_thread_id`：构造时记下主线程的 `ThreadId`，用于判断「我现在在哪」；
- `background_sender`：投递后台任务的优先级队列发送端（复用 gpui 的 `PriorityQueue`，即 u4-l2 讲过的 60/30/10 加权抽签容器）；
- `main_thread_mailbox`：跨线程发往主线程的邮箱（4.4 详述）；
- `supports_threads`：运行期人格开关；
- `_background_threads`（仅 multithreaded 编译）：持有的 worker 句柄，字段名带下划线表示「只为了保活」。

#### 4.2.2 核心流程

构造流程（worker 池部分）：

```text
if supports_threads:
    N = max(navigator.hardwareConcurrency, 2)
    for i in 0..N:
        克隆一份后台队列接收端
        wasm_thread::Builder.spawn("background-worker-{i}", || loop {
            runnable = receiver.pop()   // 阻塞等待，worker 上允许阻塞
            match runnable { Ok(r) => r.run(), Err(_) => break }  // 断开则退出
        })
```

五个必需契约方法在「两种人格 × 两个线程」下的完整决策表：

| 契约方法 | 单线程人格 | 多线程人格 · 主线程调用 | 多线程人格 · worker 调用 |
| --- | --- | --- | --- |
| `dispatch`（后台任务） | 改道 `dispatch_on_main_thread` | `spin_send` 入后台队列 | `send` 入后台队列 |
| `dispatch_on_main_thread` | `schedule_runnable` → `setTimeout(0)` | 同左 | 邮箱投 `Runnable` |
| `dispatch_after`（延迟） | `setTimeout(ms)` | `setTimeout(ms)` | 邮箱投 `Delayed`（High） |
| `spawn_realtime` | `queueMicrotask` | `queueMicrotask` | 邮箱投 `RealtimeFunction`（High） |
| `dispatch_on_main_thread_when_idle` | `requestIdleCallback` | 同左 | 邮箱投 `Idle`（Low） |

注意两处不对称：主线程入队用 `spin_send`（不能阻塞等锁）而 worker 用普通 `send`；跨线程延迟任务的 `millis` 要等信件到达主线程后才交给 `setTimeout`——**计时起点是「到站」而非「寄出」**。

#### 4.2.3 源码精读

结构体字段。[dispatcher.rs:146-153](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L146-L153)：注意 `main_thread_id` 用 `std::thread::ThreadId`（wasm 线程目标上每个 worker 都有独立 id），`_background_threads` 受 feature 门控。

构造函数的 worker 池。[dispatcher.rs:177-210](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L177-L210)：worker 数量取 `navigator.hardware_concurrency()` 与 `MIN_BACKGROUND_THREADS = 2` 的较大值；每个 worker 克隆一个接收端（`PriorityQueueReceiver` 的 `Clone` 会递增接收计数），然后进入「阻塞 pop → run」的死循环，通道断开时打日志退出。源码里的 TODO 注释坦承一个隐忧：让 web worker 长期阻塞是否合适。

线程判断与后台投递。[dispatcher.rs:222-224](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L222-L224) 是 `ThreadId` 相等比较；[dispatcher.rs:245-260](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L245-L260) 是 `dispatch`：第一行就是单线程人格的改道——`if !self.supports_threads { self.dispatch_on_main_thread(...); return; }`，随后按「在不在主线程」选 `spin_send` 或 `send`，失败只打日志（与 u4-l2 讲过的「投递失败用 forget/日志兜底」一致：wasm 版本选择记日志后丢弃）。

单线程人格的连带效果：构造函数在非 multithreaded 编译时直接丢弃后台队列接收端（[dispatcher.rs:159-160](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L159-L160)），而运行期回退时接收端同样无人消费——但这不构成问题，因为 `dispatch` 在 `supports_threads == false` 时永远不会碰 `background_sender`。

主线程直呼路径的代表：`dispatch_after`。[dispatcher.rs:271-287](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L271-L287)：毫秒数先钳制到 `i32::MAX`；主线程上把 runnable 包进 `Closure::once_into_js` 直接交给 `window.setTimeout`；否则投邮箱的 `Delayed` 变体（优先级写死 High）。

`now()`。[dispatcher.rs:326-328](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L326-L328)：用 `web_time::Instant`（文件头 [dispatcher.rs:9](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L9) 引入），因为 `std::time::Instant` 在该目标上不存在。

#### 4.2.4 代码实践

1. **实践目标**：把 4.2.2 的决策表内化成能默写的肌肉记忆。
2. **操作步骤**：遮住决策表右三列，只看「契约方法」列，对着 `dispatcher.rs` 的 `impl PlatformDispatcher for WebDispatcher`（240 行起）逐行还原每个分支；特别核对每条 worker 路径投递 `MainThreadItem` 时写的优先级（`Runnable` 用原优先级、`Delayed`/`RealtimeFunction` 用 High、`Idle` 用 Low）。
3. **需要观察的现象**：还原过程中会发现「单线程人格」一列其实是「主线程」一列的复制——所有方法的第一层判断都是 `on_main_thread()`，单线程人格只是让这个判断永远为真。
4. **预期结果**：与 4.2.2 表完全一致；能说出两处不对称（spin 与否、延迟计时起点）。
5. 纯阅读型实践，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：单线程人格下，`cx.background_spawn(cpu_heavy)` 的任务在哪执行？优先级还生效吗？
**答案**：在主线程执行。`dispatch` 改道 `dispatch_on_main_thread` → `schedule_runnable` → `setTimeout(0)` 宏任务。优先级被平铺（`schedule_runnable` 对所有非 RealtimeAudio 优先级一视同仁），这正是 dispatcher.rs:429 TODO 注释承认的缺陷： ought to enqueue 以便按优先级出队。

**练习 2**：为什么主线程调用 `dispatch` 用 `spin_send` 而 worker 用 `send`？
**答案**：`send` 内部要阻塞获取队列互斥锁（[queue.rs:43](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/queue.rs#L43)），而主线程不允许阻塞（见 2.3）；`spin_send`（[queue.rs:49-67](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/queue.rs#L49-L67)）用 `try_lock` + `spin_loop` 自旋获取，永不睡眠。

**练习 3**：worker 数量公式里 `.max(MIN_BACKGROUND_THREADS as f64)` 防的是什么？
**答案**：`navigator.hardwareConcurrency` 在无法探知核数时可能返回很小甚至未定义的值，取 max 保证至少有 2 个 worker，避免后台通道完全失去消费者（那样 `dispatch` 的发送会全部失败并刷日志）。

### 4.3 MainThreadItem：主线程任务的五种信封与浏览器调度原语

#### 4.3.1 概念说明

`MainThreadItem` 是「必须到主线程执行的东西」的统一信封。设计动机：worker 与主线程之间只有一条邮箱通道（4.4），但契约里有五种语义不同的投递——立即执行、延迟执行、空闲执行、普通函数、实时函数。把它们做成 enum 变体塞进同一个通道，比开五条通道简单得多，而且延迟/空闲的**语义可以在到站后重新表达**（到主线程后再真正去 `setTimeout` / `requestIdleCallback`）。

五种变体：

| 变体 | 谁投递 | 到站后的落点（浏览器 API） |
| --- | --- | --- |
| `Runnable(RunnableVariant)` | `dispatch_on_main_thread`（worker 上） | 直接 `run()`（在 drain 的调用栈里同步执行） |
| `Delayed { runnable, millis }` | `dispatch_after`（worker 上） | `setTimeout(fn, millis)` |
| `Idle { runnable, timeout }` | `dispatch_on_main_thread_when_idle`（worker 上） | `requestIdleCallback`（可带 timeout 选项） |
| `Function(Box<dyn FnOnce() + Send>)` | `dispatch_function_on_main_thread`（worker 上） | 直接调用 |
| `RealtimeFunction(...)` | `spawn_realtime`（worker 上） | 直接调用（源码 TODO：理想情况应有专属线程） |

主线程直呼时则完全不经过信封，直接调用三个封装函数：`schedule_runnable`、`schedule_idle_runnable`、或裸 `setTimeout` / `queueMicrotask`。

#### 4.3.2 核心流程

主线程直呼路径的三条封装：

```text
schedule_runnable(runnable, priority):
    RealtimeAudio → window.queueMicrotask(fn)      // 微任务，最快
    其他         → window.setTimeout(fn, 0)         // 宏任务，让出渲染机会

schedule_idle_runnable(runnable, timeout):
    浏览器没有 requestIdleCallback（Safari）→ 降级 schedule_runnable(Low)
    有 → requestIdleCallback(fn [, {timeout}])
         回调里：IDLE_DEADLINE ← deadline；runnable.run()；IDLE_DEADLINE ← None

dispatch_after / spawn_realtime 的主线程分支:
    setTimeout(fn, millis) / queueMicrotask(fn)
```

`execute_on_main_thread` 是信封的「拆封器」：邮箱 drain 出一条 `MainThreadItem` 后按变体分发到上表第三列的落点。

#### 4.3.3 源码精读

信封定义。[dispatcher.rs:37-50](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L37-L50)：注意 `RealtimeFunction` 上方的 TODO——源码自己承认实时函数 ideally 应跑在专属线程而非与普通函数同样对待；这与 u4-l2 讲过的「`RealtimeAudio` 永不入优先级队列」是同一原则的两个侧面：优先级队列的 `push` 对 `RealtimeAudio` 直接 `unreachable!`（[queue.rs:69-78](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/queue.rs#L69-L78)），所以 `spawn_realtime` 在 worker 上投邮箱时改用 `Priority::High`（[dispatcher.rs:296-297](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L296-L297)）。

拆封器。[dispatcher.rs:335-358](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L335-L358)：`execute_on_main_thread` 按变体分发——`Runnable` 与两种函数变体直接同步执行，`Idle` 与 `Delayed` 在主线程**重新排队**到对应的浏览器 API。这就是「语义到站后重新表达」：延迟与空闲的计时/调度权始终在浏览器手里。

`schedule_runnable`。[dispatcher.rs:418-435](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L418-L435)：`Closure::once_into_js` 把消耗式闭包变成 JS 函数；`RealtimeAudio` 走微任务、其余走 `setTimeout(0)`。TODO 注释再次承认主线程路径没有优先级化。

空闲回调封装与 Safari 降级。[dispatcher.rs:375-403](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L375-L403)：文档注释说清了设计——**每个 runnable 注册一个独立的 `requestIdleCallback`**（与 `dispatch_after` 每个定时器一个 `setTimeout` 对齐），因为浏览器本身就提供了队列语义：空闲回调按注册顺序执行、一个空闲期按 deadline 尽量多执行、超时的回调会被当作普通任务补投。回调闭包在运行前后维护 `IDLE_DEADLINE` 线程局部变量。Safari 分支把空闲任务当普通宏任务跑，且因为没有受度量的 deadline，`idle_time_remaining` 恒为 `None`，空闲任务需要自己约束时间片。

能力探测与缓存。[dispatcher.rs:405-416](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L405-L416)：用 `Reflect::has(window, "requestIdleCallback")` 探测一次并缓存进 `IDLE_CALLBACK_SUPPORTED` 线程局部 `Cell`。

契约方法 `idle_time_remaining`。[dispatcher.rs:314-324](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L314-L324)：非主线程返回 `None`；主线程读 `IDLE_DEADLINE`，把 JS 的毫秒换算成 `Duration`。

信封的第四种变体还有一个真实用户——HTTP 客户端。[http_client.rs:87-101](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/http_client.rs#L87-L101)：`FetchHttpClient::send` 的 future 可能被 poll 在 worker 上，而浏览器的 `fetch` 与 `spawn_local` 必须在主线程的 JS 环境发起，于是先 `dispatch_function_on_main_thread`（[dispatcher.rs:226-237](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L226-L237)，主线程直接 `queueMicrotask`、worker 投邮箱 `Function` 变体）跳回主线程再 `spawn_local(fetch)`，结果经 oneshot 通道送回等待方。这是 4.5 三个入口都要注入 `FetchHttpClient` 的底层原因。

#### 4.3.4 代码实践

1. **实践目标**：验证「契约方法 → 浏览器 API」映射没有遗漏。
2. **操作步骤**：通读 [dispatcher.rs:240-329](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L240-L329) 的 `impl PlatformDispatcher for WebDispatcher`，为每个方法标注它最终触达的浏览器 API；再反向从三个封装函数（`schedule_runnable` / `schedule_idle_runnable` / 裸 `setTimeout`）出发，列出各自的全部调用者。
3. **需要观察的现象**：`is_main_thread` 与 `now` 两个方法不触达任何浏览器调度 API；其余每个方法的主线程分支与 worker 分支落点可以不同（如 `spawn_realtime`：微任务 vs 邮箱）。
4. **预期结果**：得到一张与 4.3.1 表等价的双向索引；额外发现 `dispatch_function_on_main_thread` 这个 `pub(crate)` 方法不在契约里、却撑起了 HTTP 客户端。
5. 纯阅读型实践，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：worker 上调用 `dispatch_after(10s, r)`，从调用到执行实际经过多久？
**答案**：≥ 10 秒，且通常略多。`millis` 信封先在邮箱里排队，被 drain 到主线程后才交给 `setTimeout(millis)`——计时从「到站」开始，邮路上的排队时间不计入延时但会叠加在总时长上。

**练习 2**：Safari 上 `dispatch_on_main_thread_when_idle(r, Some(1s))` 的 `timeout` 参数去哪了？
**答案**：被丢弃。Safari 分支直接降级为 `schedule_runnable(window, runnable, Priority::Low)` 即 `setTimeout(0)`，`timeout` 选项只在真正的 `requestIdleCallback` 路径上通过 `IdleRequestOptions` 传递。

**练习 3**：为什么 `IDLE_DEADLINE` 必须是 thread_local 而不是 `WebDispatcher` 的字段？
**答案**：它只在主线程的 `requestIdleCallback` 回调里有意义（回调设置、runnable 读取、回调清除），`idle_time_remaining` 也只在主线程返回 `Some`。放进 `WebDispatcher`（它是 `Send + Sync`、被 worker 共享）反而要加锁且语义错误。

### 4.4 MainThreadMailbox：Atomics 唤醒协议与丢失唤醒

#### 4.4.1 概念说明

`MainThreadMailbox` 解决的问题是：**worker 怎么把任务交给主线程，并让主线程「及时」知道？**

- 「交给」不难：共享内存里放一个优先级队列（复用 gpui 的 `PriorityQueue`）。
- 「及时知道」才是难点：主线程不能阻塞在队列上等（2.3 的禁令），浏览器也没有 `recv` 这种系统调用。主线程需要一个「可以被异步唤醒」的睡眠点。

答案是经典的「条件变量 + 谓词」模式的 Atomics 版：

- **队列**是数据，**signal（一个 `AtomicI32` 单元格）**是谓词：「1 = 有未处理工作」。
- worker 投递后 `store(1)` + `notify`——notify 负责叫醒正在睡的人，值负责让「还没睡下的人」也能发现工作。
- 主线程 waker loop 反复执行：把 signal 清零（重新布防）→ 排空队列 → `Atomics.waitAsync`（期待值为 0）异步等待。

#### 4.4.2 核心流程

```text
worker 侧 post(priority, item):
    spin_send 入优先级队列          # 主线程不能阻塞，但 worker 自旋 OK
    Atomics.store(signal, 1)
    Atomics.notify(signal)

主线程 waker loop（spawn_local 的 async 循环）:
    loop:
        Atomics.store(signal, 0)      # 重新布防
        drain()                      # 关键：布防后再排空一次，补丢失唤醒窗口
        result = Atomics.waitAsync(signal, expect=0)
        if result.async == false:    # 值已经 ≠ 0：等都不用等，活已经到了
            （直接进入下一轮 drain）
        else:
            await result.value       # 睡到被 notify（或值变化）
        drain()

drain():
    lock 接收端
    while let item = spin_try_pop():  # 60/30/10 加权抽签
        execute_on_main_thread(item)  # 按信封变体分发（见 4.3）
```

三队皆非空时，drain 每次弹出一条队的概率按权重分配：

\[ P(\text{High}) = \frac{60}{60+30+10} = 0.6,\quad P(\text{Medium}) = 0.3,\quad P(\text{Low}) = 0.1 \]

这与 u4-l2/u4-l4 讲过的后台抽签完全同构——因为容器是同一个（[queue.rs:248-281](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/queue.rs#L248-L281) 的 `spin_try_pop`，权重定义在 [scheduler.rs:44-56](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/scheduler/src/scheduler.rs#L44-L56)：High 60 / Medium 30 / Low 10 / RealtimeAudio 0）。

**丢失唤醒分析**（本模块最值得吃透的点）：

- 窗口一：post 发生在「上一轮 drain 结束」与「本轮 store(0)」之间。此时 notify 落空（主线程没在等）。兜底：store(0) 之后**立刻再 drain 一次**，队列里滞留的条目被取走；即便此刻又有新 post 把值写回 1，接下来的 `waitAsync(expect=0)` 会走「不相等」路径立即返回。
- 窗口二：post 发生在 store(0) 之后、waitAsync 注册之前。notify 可能在等待者登记前到达而丢失。兜底：signal 值已是 1，`waitAsync(expect=0)` 以 `async: false` 立即返回，循环直接 drain。

两条兜底合起来给出不变式：**只要队列非空过，signal 就曾是 1，而任何一次 `waitAsync(expect=0)` 都不会在「有未处理工作」时睡死**。

#### 4.4.3 源码精读

结构体与构造。[dispatcher.rs:52-66](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L52-L66)：`sender` 是普通优先级队列发送端；`receiver` 被包进 `parking_lot::Mutex`（drain 需要 `&mut self`，而邮箱整体在 `Arc` 里被 waker loop 持有）；`signal` 就是那个 `AtomicI32` 谓词单元。

post。[dispatcher.rs:68-77](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L68-L77)：`spin_send` 失败（接收端断开）只打日志；随后对 signal 视图先 `store(1)` 再 `notify`。

drain。[dispatcher.rs:79-90](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L79-L90)：持接收端锁循环 `spin_try_pop`，逐条 `execute_on_main_thread`。代码注释点明必须用 spin 变体的原因——**主线程不能阻塞获取锁**。另一个值得注意的推论：所有 `post` 调用点都在 `!on_main_thread()` 分支里（见 4.2.3、4.3.3 的各方法），主线程从不向邮箱投递，所以「持锁执行条目」不会自死锁；worker 若在 drain 期间 post，会在 `spin_send` 里自旋等锁。

signal 视图——Rust 原子与 JS Atomics 握手的地方。[dispatcher.rs:92-96](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L92-L96)：把 `AtomicI32` 的地址换算成字节偏移，在 `wasm_bindgen::memory()` 的 buffer 上构造一个长度为 1 的 JS `Int32Array`。此后 Rust 的 `AtomicI32` 与 JS 的 `Atomics` 操作的是**同一块 4 字节线性内存**——这就是跨语言共享原子的全部秘密。

waker loop 主体。[dispatcher.rs:98-143](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L98-L143)：`run_waker_loop` 以 `self: &Arc<Self>` 接收（Arc 方法语法，便于 clone 进 async 块）；无共享内存时打警告直接返回（单线程人格根本不启动它，邮箱因此处于休眠——但如 4.2.3 所述那时也不会有人投递）。循环体严格按 4.4.2 的伪代码展开，两条注释分别解释「布防后立刻 drain」与「async:false 即跳过等待」，与我们的丢失唤醒分析一一对应。

#### 4.4.4 代码实践

1. **实践目标**：亲手推演唤醒协议的时间线，确认两个丢失唤醒窗口都被兜住。
2. **操作步骤**：画一条竖直时间线，左侧标 worker 事件（post 的三步：入队/store/notify），右侧标主线程事件（store(0)/drain/waitAsync/await/drain）。分别摆放三组交错：
   - A：post 完整发生在主线程 `await` 期间；
   - B：post 发生在主线程上一轮 drain 结束后、store(0) 前；
   - C：post 的 notify 恰在 store(0) 后、waitAsync 注册前到达。
3. **需要观察的现象**：A 走「promise resolve → drain」；B 走「布防后 drain 兜住」；C 走「async:false 立即返回 → drain」。
4. **预期结果**：三组交错无一导致主线程睡死或任务滞留；写出每组的兜底机制对应的源码行。
5. 纸上推演即可，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：`post` 里 `store(1)` 和 `notify` 能不能只留一个？
**答案**：不能。只留 `notify`：窗口二里 notify 落空后主线程永远睡着。只留 `store`：主线程已在 `waitAsync` 中时，值变化本身能让 `waitAsync(expect=0)` 以「不相等」返回——看起来可行，但 `waitAsync` 的语义保证依赖值比较，且窗口二中「注册前」的值变化若发生在检查之后仍有竞态；notify+值的组合是标准的「唤醒 + 谓词」双保险，源码两者都做。

**练习 2**：waker loop 里 `Atomics.store(&view, 0, 0)` 之后为什么必须紧跟一次 `drain`，而不是直接 `waitAsync`？
**答案**：store(0) 会清掉「有工作」的标记。若此前（上一轮 drain 之后、本轮 store 之前）有 post 把值写成 1 并 notify 落空，清零后这个信息就没了——队列里有活但 signal 为 0，`waitAsync(expect=0)` 会安心睡去。紧跟的 drain 把这类滞留条目取走，才恢复不变式。

**练习 3**：单线程人格下 `MainThreadMailbox` 还工作吗？
**答案**：结构上存在但完全休眠：`run_waker_loop` 不会被启动（构造函数只在 `supports_threads` 时调用它），而所有契约方法在主线程路径（单线程人格下是唯一路径）都直接走浏览器 API，不投邮箱。邮箱是专为「存在其他线程」准备的器官。

### 4.5 三个入口：application、application_with_web_backend 与 single_threaded_web

#### 4.5.1 概念说明

u1-l1/u1-l2 已建立「门面入口」的概念。本讲聚焦 wasm 上三个入口在**多线程开关**上的分野：

| 入口 | 平台构造 | `allow_multi_threading` 实参 | 额外注入 |
| --- | --- | --- | --- |
| `application()` | wasm 上转调 `application_with_web_backend(Auto)` | `true` | `FetchHttpClient` |
| `application_with_web_backend(pref)` | `WebPlatform::new_with_backend(true, pref)` | `true` | `FetchHttpClient` |
| `single_threaded_web()` | `WebPlatform::new(false)` | `false` | `FetchHttpClient` |
| （参照）`current_platform` 的 wasm 分支 | `WebPlatform::new(true)` | `true` | 无 |

也就是说：**三个 web 入口的差别不在渲染后端，而在传给 `WebDispatcher::new` 的那个布尔值**。`single_threaded_web` 的文档注释直说：「与 `application` 不同，本函数返回单线程 web 应用」。

#### 4.5.2 核心流程

```text
application()                       （wasm 目标）
  └─ application_with_web_backend(Auto)
       ├─ WebPlatform::new_with_backend(true, Auto)
       │    ├─ WebDispatcher::new(browser_window, allow=true)
       │    │    └─ supports_threads 探测（4.1）→ 决定人格
       │    ├─ BackgroundExecutor::new(dispatcher.clone())
       │    └─ ForegroundExecutor::new(dispatcher.clone())   # 一个 dispatcher，两个执行器（u4-l1）
       └─ with_platform(platform).with_http_client(FetchHttpClient)

single_threaded_web()
  └─ WebPlatform::new(false) → WebDispatcher::new(window, false)
       └─ supports_threads 恒为 false → 单线程人格
```

#### 4.5.3 源码精读

`application()` 的 wasm 分支。[gpui_platform.rs:13-21](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/src/gpui_platform.rs#L13-L21)：wasm 目标上转调 `application_with_web_backend(WebBackendPreference::Auto)`，桌面目标走 `with_platform(current_platform(false))`——与 u1-l4 的条件编译分发同构。

两个 wasm 专属入口。[gpui_platform.rs:30-38](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/src/gpui_platform.rs#L30-L38)：`application_with_web_backend` 用 `new_with_backend(true, pref)` 构造平台并注入 `FetchHttpClient`；[gpui_platform.rs:41-46](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/src/gpui_platform.rs#L41-L46)：`single_threaded_web` 唯一的区别是实参 `false`。

布尔值抵达 dispatcher 的路径。[platform.rs:119-134](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/platform.rs#L119-L134)：`WebPlatform::new(allow_multi_threading)` 转调 `new_with_backend(allow_multi_threading, Auto)`；后者创建 `WebDispatcher::new(browser_window.clone(), allow_multi_threading)`，再用**同一个** `Arc<WebDispatcher>` 构造前后台两个执行器——这正是 u4-l1 讲过的「平台只造一个 dispatcher，两个执行器共享」模型在 web 上的实例。

`current_platform` 的 wasm 分支。[gpui_platform.rs:76-80](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/src/gpui_platform.rs#L76-L80)：`let _ = headless;` 丢弃 headless 参数（浏览器环境里「无头」没有意义），`WebPlatform::new(true)` 里的 `true` 是 **allow_multi_threading**，不是 headless——读这段代码时最容易看错的两处形参。

实际用法参照 hello_web。[hello_web/main.rs:429-431](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/examples/hello_web/main.rs#L429-L431)：`web_init()`（设 panic hook + 日志，见 [gpui_platform.rs:50-54](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_platform/src/gpui_platform.rs#L50-L54)）之后用 `application_with_web_backend(requested_backend())` 启动，后端偏好来自 URL 查询参数。

#### 4.5.4 代码实践

1. **实践目标**：体感区分多线程与单线程人格的运行期差异。
2. **操作步骤**：本地跑通 hello_web（构建方式见 u7-l3 的 trunk 流程）；观察素数计数期间进度条与页面是否流畅；然后把 main.rs 第 431 行的 `application_with_web_backend(requested_backend())` 改为 `single_threaded_web()` 重新构建再跑（**改的是示例文件的本地副本，仅作实验，不要提交**）。
3. **需要观察的现象**：多线程版计算在 worker 上、UI 保持响应；单线程版计算以 `setTimeout(0)` 宏任务形式穿插在主线程，长计算期间交互明显变钝。
4. **预期结果**：与 4.2 的决策表一致——`background_spawn` 在单线程人格下改道主线程宏任务。若本地无 wasm 工具链，改为在纸上写出两条链路的每一站（参考下一节综合实践的答案）。**待本地验证**。
5. 实验结束后把示例文件恢复原状。

#### 4.5.5 小练习与答案

**练习 1**：`single_threaded_web()` 与「关闭 `multithreaded` feature」是一回事吗？
**答案**：不是。前者是**运行期**开关：feature 照常编译，只是 `allow_threads = false` 让 `supports_threads` 恒为假。后者是**编译期**开关，`wasm_thread` 与 `scheduler/wasm-threads` 根本不参与编译（还影响链接产物）。两者最终都落入单线程人格，但产物与依赖面不同。

**练习 2**：为什么三个 web 入口都注入 `FetchHttpClient`，而桌面 `application()` 不需要？
**答案**：浏览器里发请求的 `fetch` 必须在主线程 JS 环境发起（见 4.3.3 的 http_client 链路），且 web 没有系统级 HTTP 客户端可抽象；桌面平台上 gpui 默认装空的 HTTP 客户端，由上层应用（如 Zed）自行注入。

**练习 3**：`current_platform(headless)` 在 wasm 上对 `headless` 做了什么？`WebPlatform::new(true)` 的 `true` 又是什么？
**答案**：headless 被 `let _ = headless;` 显式丢弃——浏览器环境没有「无头」概念。`true` 是 `allow_multi_threading` 实参，所以经 `current_platform` 走 web 平台时默认允许多线程（是否真用还取决于 4.1 的运行期探测）。

## 5. 综合实践

**任务**（本讲核心实践）：阅读 `MainThreadMailbox` 实现后，写出「一个任务从 spawn 到被执行」的全程伪代码/流程图，标注每一站对应的浏览器 API。

**参考追踪 A —— 多线程人格：worker 上完成的后台任务唤醒前台 continuation**

以 hello_web 的素数计数为背景（`cx.background_spawn` 的 chunk 完成后唤醒等待进度的前台 future）：

```text
① 主线程：cx.background_spawn(future)
   └─ scheduler 把 future 包成 RunnableVariant（信封带 spawn 元数据，u4-l2）
② 主线程：WebDispatcher::dispatch(runnable, priority)              [dispatcher.rs:245]
   └─ spin_send 入 PriorityQueue 三条 VecDeque 之一                 [gpui/src/queue.rs]
③ worker：阻塞 receiver.pop()（60/30/10 抽签）→ runnable.run()
   └─ 任务在 Web Worker 里执行                    ← 浏览器 API: Web Worker（wasm_thread）
④ worker：future 完成，唤醒 await 它的前台 continuation
   └─ WebDispatcher::dispatch_on_main_thread(r2)                    [dispatcher.rs:262]
⑤ worker：mailbox.post(priority, Runnable(r2))                     [dispatcher.rs:68]
   ├─ spin_send 入邮箱队列
   ├─ Atomics.store(signal, 1)     ← 浏览器 API: SharedArrayBuffer 上的 Atomics.store
   └─ Atomics.notify(signal)       ← 浏览器 API: Atomics.notify
⑥ 主线程 waker loop：waitAsync 的 promise resolve（或 async:false 立即返回）
                                    ← 浏览器 API: Atomics.waitAsync + Promise
   └─ drain() → spin_try_pop → execute_on_main_thread               [dispatcher.rs:79,335]
⑦ 主线程：schedule_runnable(r2, priority)
   ├─ RealtimeAudio → queueMicrotask  ← 浏览器 API: queueMicrotask
   └─ 其他        → setTimeout(0)     ← 浏览器 API: setTimeout
⑧ 浏览器事件循环执行该宏任务 → r2.run()
   └─ 实体更新 / cx.notify() → 下一帧绘制
```

**参考追踪 B —— 单线程人格：同一份代码**

①② 之后不再有 worker：`dispatch` 因 `supports_threads == false` 直接改道 `dispatch_on_main_thread`（[dispatcher.rs:246-249](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/dispatcher.rs#L246-L249)）→ `schedule_runnable` → `setTimeout(0)` → 事件循环执行。邮箱、Atomics、waker loop 全程不参与。

**加分追踪 C —— 一次 fetch 请求**：worker 上 poll 的 `FetchHttpClient::send` → `dispatch_function_on_main_thread`（邮箱 `Function` 变体）→ drain → `queueMicrotask` → `spawn_local(fetch)` → oneshot 回到等待方（[http_client.rs:87-101](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_web/src/http_client.rs#L87-L101)）。

**验收标准**：图中每一站都能标出源码文件与行号；能回答「哪几步依赖跨域隔离」（⑤⑥ 的 Atomics 与 ③ 的 worker）。

## 6. 本讲小结

- **能力探测决定人格**：`supports_threads = multithreaded feature && allow_threads && SharedArrayBuffer 可用 && Atomics.waitAsync 可用`；不满足则整份代码退化为「一切任务排主线程」的单线程人格，后台优先级被 `setTimeout(0)` 平铺。
- **MainThreadItem 是五种信封**（Runnable / Delayed / Idle / Function / RealtimeFunction）：worker 与主线程只走一条邮箱通道，延迟与空闲的语义「到站后重新表达」——到主线程才真正去 `setTimeout` / `requestIdleCallback`。
- **MainThreadMailbox = 优先级队列 + AtomicI32 谓词**：`signal_view` 把 Rust 原子变量映射成 wasm 线性内存上的 JS `Int32Array`，让 `Atomics.store/notify/waitAsync` 与 Rust 原子操作同一块内存；waker loop 用「布防后先 drain、值不等即不睡」双保险堵死两个丢失唤醒窗口。
- **主线程禁止阻塞无处不在**：主线程入队用 `spin_send`、取件用 `spin_try_pop`、等待用 `waitAsync` 而非 `wait`，全部是 2.3 那条浏览器禁令的推论。
- **三个入口只差一个布尔**：`application()`（wasm 上 = Auto 后端 + 多线程）、`application_with_web_backend(pref)`（显式后端 + 多线程）、`single_threaded_web()`（多线程开关关死），三者都注入 `FetchHttpClient`。
- **与 u4-l3/u4-l4 的四方对照**：宿主事件循环分别是 calloop / NSRunLoop+GCD / Win32 消息循环 / 浏览器事件循环；唤醒机制分别是 ping 事件源 / dispatch_async / PostMessageW / `Atomics.notify`——容器（优先级队列 + 加权抽签）与「唤醒即再投递」「!Send future 主线程专属」「RealtimeAudio 拒绝入队」这些契约级共识保持一致。

## 7. 下一步学习建议

- **u4-l6（前台工作日志与 hang 检测）**：本单元收官讲，看 `RunnableVariant` 携带的元数据如何被 profiler 消费、`HangDetector` 如何从日志流里事后发现主线程卡顿——单线程人格下「后台任务挤占主线程」正是最典型的 hang 来源。
- **横向复读**：把本讲的 WebDispatcher 与 u4-l3 的 LinuxDispatcher、u4-l4 的 Mac/Windows dispatcher 并排再读一遍，亲手完成四方对比表（唤醒机制、优先级、延迟、空闲、线程安全），你会对「契约不变、宿主原语各显神通」有肌肉级理解。
- **向前铺垫**：u7 单元将深入 WebPlatform 全貌（`web_init` 初始化顺序、WebWindow 与浏览器事件桥接、hello_web 的 trunk 构建），本讲的 dispatcher 是其中「任务从哪来、到哪去」的骨架。
