# 前置知识二：Tokio 运行时、Handle 与 JoinHandle

## 1. 本讲目标

本讲是阅读 `gpui_tokio` 源码前的第二块（也是最后一块）前置拼图。学完本讲，你应该能够：

1. 不依赖 `#[tokio::main]`，用 `tokio::runtime::Builder::new_multi_thread()` 手动构建一个 Tokio 运行时，并说清 `worker_threads(2)` 与 `enable_all()` 各自的作用。
2. 解释 `Runtime` 与 `Handle` 的所有权关系：`Runtime` 拥有线程池，`Handle` 是可以廉价克隆、可以在任意线程上驱动 `spawn` 的「遥控器」——这正是 `gpui_tokio` 能从 GPUI 线程操纵 Tokio 的关键。
3. 掌握 `JoinHandle` 的 await 语义（成功给 `Ok`，任务 panic 或被取消给 `Err(JoinError)`），以及 `abort_handle()` 的取消机制——这是后续理解取消联动（u2-l4）的基础。

上一讲（u1-l3）我们站在 GPUI 一侧看了它的前台/后台双执行器；本讲我们站到 Tokio 一侧。两讲合起来，你就具备了读懂 `gpui_tokio.rs` 全部 101 行所需的两边背景。

## 2. 前置知识

### 2.1 异步运行时是什么

`async fn` 本身只是编译器生成的状态机（一个 `Future`），它不会自己跑起来——必须有「人」反复调用它的 `poll` 方法推动它前进，直到产出结果。这个「人」就是**异步运行时（runtime）**。Tokio 运行时通常由三部分组成：

- **调度器（scheduler）**：决定哪个任务在哪个线程上被 poll；
- **线程池**：真正执行任务的一组操作系统线程；
- **I/O 驱动与定时器驱动**：监听网络事件、管理定时器，负责「事件没好就挂起、事件好了就唤醒」。

对比上一讲：GPUI 自带了一套执行器（前台单线程 + 后台线程池），它**不是** Tokio。一个进程里可以同时存在多个互不相识的运行时，这正是 `gpui_tokio` 存在的前提。

### 2.2 `#[tokio::main]` 帮你做了什么

大多数 Tokio 教程第一行就是：

```rust
#[tokio::main]
async fn main() { /* ... */ }
```

这个宏展开后本质上就是「手动建运行时 + 在运行时上 `block_on` 驱动 main 体」。它方便，但把运行时的创建过程藏了起来。`gpui_tokio::init` 恰恰要做宏背后那些事——因为 Zed 的主线程归 GPUI 管，不能交给 Tokio。所以本讲的实践任务要求**一律不用这个宏**，亲手做一遍宏替你做的事。

### 2.3 `Send + 'static` 是什么约束

你会在 `gpui_tokio` 的泛型约束里反复看到 `Fut: Future<Output = R> + Send + 'static`：

- `Send`：这个值可以安全地在线程间转移所有权。Tokio 的任务可能被调度到任意 worker 线程，所以必须 `Send`。
- `'static`：这个 future 不持有任何外部数据的借用，可以活任意久（任务的生命周期不由调用方作用域决定）。

对比上一讲的结论：GPUI **前台** future 刻意不要求 `Send`（单线程执行器，省去同步开销）。而一旦跨到 Tokio 侧，`Send + 'static` 就是硬性门槛。

### 2.4 与前几讲的衔接

- u1-l1 说过：`gpui_tokio` 是把 GPUI 任务（`Task`）与 Tokio 任务（`JoinHandle`）打通的桥。本讲讲清楚桥的「Tokio 端接口」长什么样。
- u1-l2 说过：`gpui_tokio` 对 tokio 只启用 `rt` 与 `rt-multi-thread` 两个 feature。本讲会看到为什么这几个 API 恰好只需要这两个 feature。

## 3. 本讲源码地图

本讲涉及的项目源码文件很少（这个 crate 本身只有一个源码文件）：

| 文件 | 作用 |
| --- | --- |
| [crates/gpui_tokio/src/gpui_tokio.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs) | 本讲唯一精读的项目源码：`init` 构建 runtime、`GlobalTokio` 持有 `Runtime` 与 `Handle`、`spawn` 中对 `JoinHandle`/`abort_handle` 的使用全在这里 |
| [crates/gpui_tokio/Cargo.toml](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/Cargo.toml) | tokio 依赖声明，只启用 `rt` 与 `rt-multi-thread` 两个 feature |
| [Cargo.toml（仓库根）](https://github.com/zed-industries-zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/Cargo.toml#L829) | workspace 统一的 tokio 版本声明 `tokio = { version = "1" }` |

延伸参考（非项目文件，Tokio 官方文档）：

- [Runtime](https://docs.rs/tokio/latest/tokio/runtime/struct.Runtime.html) / [Handle](https://docs.rs/tokio/latest/tokio/runtime/struct.Handle.html) / [Builder](https://docs.rs/tokio/latest/tokio/runtime/struct.Builder.html)
- [JoinHandle](https://docs.rs/tokio/latest/tokio/task/struct.JoinHandle.html) / [AbortHandle](https://docs.rs/tokio/latest/tokio/task/struct.AbortHandle.html) / [JoinError](https://docs.rs/tokio/latest/tokio/task/struct.JoinError.html)

## 4. 核心概念与源码讲解

### 4.1 用 `tokio::runtime::Builder` 手动构建运行时

#### 4.1.1 概念说明

Tokio 不允许你直接 `Runtime::new()` 拿到一个多线程运行时，而是要求通过**建造者（Builder）模式**逐项配置后再 `build()`。这样设计是因为运行时的形态差异很大——线程数、是否启用 I/O/定时器驱动、线程命名等都要在创建时定死。

几个关键配置项：

| 配置 | 含义 |
| --- | --- |
| `Builder::new_multi_thread()` | 创建「多线程调度器」的建造者。任务会被分发到一组 worker 线程上并发执行，支持工作窃取。对应的还有 `new_current_thread()`（单线程调度器） |
| `.worker_threads(n)` | 设置 worker 线程数量。不设置时默认等于 CPU 核心数。多线程调度器的 worker 线程默认线程名为 `tokio-runtime-worker` |
| `.enable_all()` | 同时启用 I/O 驱动与定时器驱动。**Builder 默认什么都不启用**——没有它，`tokio::net`、`tokio::time::sleep` 等都无法工作 |
| `.build()` | 真正创建运行时，返回 `Result<Runtime, Error>`（例如底层线程创建失败时会返回 `Err`），所以调用方要处理错误 |

`gpui_tokio::init` 正是用这四步创建了一个**只有 2 个 worker 线程**的运行时。

#### 4.1.2 核心流程

手动构建并使用一个运行时的通用流程：

```text
Builder::new_multi_thread()   # 选多线程调度器
    .worker_threads(2)        # 定线程数
    .enable_all()             # 启用 I/O + 定时器驱动
    .build()                  # 得到 Result<Runtime, Error>
    ?                         # 处理失败（gpui_tokio 用 expect）
# 此后：在 Runtime 上 block_on 驱动入口 future，
#       或用 Handle::spawn 把任务丢进去（见 4.2）
```

#### 4.1.3 源码精读

先看 `gpui_tokio.rs` 中 `init` 的前半段——也就是本模块的主角：

[crates/gpui_tokio/src/gpui_tokio.rs:12-18](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L12-L18)：`init` 函数用 Builder 手动构建 Tokio 运行时。逐行含义：

```rust
pub fn init(cx: &mut App) {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        // Since we now have two executors, let's try to keep our footprint small
        .worker_threads(2)
        .enable_all()
        .build()
        .expect("Failed to initialize Tokio");
```

- 第 13 行选了多线程调度器；
- 第 15 行 `worker_threads(2)` 把线程数压到 2——源码注释写明原因：进程里已经有两个执行器（GPUI 前台 + GPUI 后台），再开一大池 Tokio 线程会让资源占用膨胀，所以刻意保持小footprint；
- 第 16 行 `enable_all()` 启用 I/O 与定时器驱动，这样跑在它上面的 `reqwest`、websocket、`sleep` 才能工作；
- 第 17-18 行 `build().expect(...)`：`build` 返回 `Result`，启动期初始化失败没有恢复手段，直接 `expect` panic（符合「启动即校验」的惯例；仓库规范里禁止的 `unwrap` 针对的是可能出错的常规路径，这里是刻意的启动断言）。

再看依赖侧为什么恰好够用：

[crates/gpui_tokio/Cargo.toml:19](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/Cargo.toml#L19)：tokio 依赖只启用 `rt` 与 `rt-multi-thread` 两个 feature。`rt` 提供 `Builder`/`Runtime`/`Handle`/`JoinHandle` 这些运行时基本件，`rt-multi_thread` 额外解锁 `new_multi_thread()` 多线程调度器；本 crate 代码只用到这些，所以不必开 `time`、`net` 等——那些由真正使用这些 API 的下游 crate 开启，经 feature unification 取并集（承接 u1-l2 的结论）。版本声明则统一在 [Cargo.toml:829](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/Cargo.toml#L829)（`tokio = { version = "1" }`）。

#### 4.1.4 代码实践

**实践目标**：亲手做一遍 `#[tokio::main]` 背后的事，并观察 `enable_all` 被去掉后的后果。

**操作步骤**（以下均为「示例代码」，不是 Zed 仓库内容）：

1. 在 zed 仓库之外新建一个独立项目：

   ```bash
   cargo new tokio-primer
   cd tokio-primer
   ```

2. 把 `Cargo.toml` 的依赖改成（注意：独立项目里用 `sleep` 需要额外开 `time` feature，这正是 feature unification 在 Zed 里被下游 crate「代开」的部分）：

   ```toml
   [dependencies]
   tokio = { version = "1", features = ["rt", "rt-multi-thread", "time"] }
   ```

3. 把 `src/main.rs` 写成：

   ```rust
   use std::time::Duration;

   fn main() {
       // 与 gpui_tokio::init 相同的四步构建
       let runtime = tokio::runtime::Builder::new_multi_thread()
           .worker_threads(2)
           .enable_all()
           .build()
           .expect("Failed to initialize Tokio");

       // main 线程不在运行时里，用 block_on 进入并驱动一个 future
       let answer = runtime.block_on(async {
           tokio::time::sleep(Duration::from_millis(100)).await;
           42
       });
       println!("answer = {answer}");
   }
   ```

4. `cargo run`，确认输出 `answer = 42`。
5. **观察实验**：把 `.enable_all()` 那一行删掉再 `cargo run`。

**需要观察的现象**：步骤 4 正常打印；步骤 5 程序 panic，报错大意为定时器未启用（不同 tokio 版本措辞略有差异）。

**预期结果**：`Builder` 默认不启用任何驱动，`sleep` 依赖定时器驱动，没有 `enable_all` 就无法工作。`gpui_tokio::init` 里的 `.enable_all()` 因此必不可少。panic 的具体报错文本以本地输出为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`gpui_tokio` 为什么用 `new_multi_thread()` 而不是 `new_current_thread()`？

**答案**：桥接过去的任务多为网络 I/O（HTTP 请求、websocket、LiveKit），需要真正的并发与多核利用；`current_thread` 调度器只在 `block_on` 的调用线程上轮询任务，一个长任务会把其他任务全部饿住，无法承载 Zed 的网络负载。

**练习 2**：`worker_threads(2)` 不写的话默认是多少？`gpui_tokio` 为什么显式写 2？

**答案**：默认等于 CPU 核心数。显式写 2 是因为 Zed 进程里已有 GPUI 前台执行器与后台线程池两个执行者，Tokio 再按核数开线程会让总线程数膨胀，源码注释明确写了 "keep our footprint small"。

**练习 3**：`build()` 为什么返回 `Result`？`gpui_tokio` 用 `expect` 处理是否违反「避免 panic」的编码规范？

**答案**：创建运行时可能失败（例如系统资源不足、线程创建失败）。`expect` 在这里是启动期断言：初始化失败时应用无法工作，立刻 panic 是合理且信息清晰的失败方式；规范禁止的是在常规可恢复路径上用 `unwrap` 丢弃错误。

### 4.2 `Runtime` 与 `Handle`：所有权与「遥控器」

#### 4.2.1 概念说明

- **`Runtime`** 拥有整台机器：worker 线程、调度器、I/O 与定时器驱动都归它所有。它被 drop 时运行时关停。
- **`Handle`** 是指向某个运行时的**廉价句柄**：`Clone` 只是复制一个内部引用，克隆多少个都不产生新线程；drop 一个 Handle 也完全不影响运行时。但只要拿着 Handle，就能：

  - `handle.spawn(future)`：把任务投递到该运行时，**在任意线程上调用都可以，不要求当前线程处于 Tokio 运行时上下文**；
  - `handle.block_on(future)`：在该运行时上同步等待一个 future。

这里有一个极易混淆的对比：自由函数 `tokio::spawn(...)` 要求调用点**正在某个 Tokio 运行时上下文里**，否则 panic；而 `Handle::spawn(...)` 是方法调用，靠的是 Handle 自身携带的运行时信息，与调用线程无关。`gpui_tokio` 正是靠这一点，让运行在 GPUI 执行器上的代码（完全不在 Tokio 上下文中）也能把任务投进 Tokio 线程池。

类比：`Runtime` 是电视台（拥有发射塔和演播厅），`Handle` 是遥控器——遥控器可以复印很多份带在身上，按一下（`spawn`）节目就开播，但扔掉遥控器不会拆掉电视台。

#### 4.2.2 核心流程

`gpui_tokio::init` 中 Runtime 与 Handle 的流转：

```text
Builder...build() ──► runtime: Runtime      （拥有 2 个 worker 线程）
        │
        └─ runtime.handle().clone() ──► handle: Handle   （廉价克隆的"遥控器"）
                     │
                     ▼
     set_global(GlobalTokio { owned_runtime: Some(runtime), handle })
                     │
                     ▼
     之后任意 GPUI 代码：Tokio::spawn(cx, fut)
        └─ read_global 拿到 handle ──► handle.spawn(fut)   ← 从 GPUI 线程投递任务
```

两条初始化路径的所有权差异：

- `init`：自己 build，`Runtime` 存进全局（`owned_runtime: Some`），全局销毁时负责关停它；
- `init_from_handle`：只保存外部传入的 `Handle`（`owned_runtime: None`），运行时的生死由外部所有者负责。

（全局状态与 Drop 关停的完整生命周期在 u2-l2 展开，本讲只关注 Runtime/Handle 的关系。）

#### 4.2.3 源码精读

[crates/gpui_tokio/src/gpui_tokio.rs:20-24](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L20-L24)：从 `runtime` 取出 `Handle` 并克隆，然后把 **Runtime 本体**与 **Handle 克隆**一起存入全局——一个负责「拥有」，一个负责「使用」：

```rust
    let handle = runtime.handle().clone();
    cx.set_global(GlobalTokio {
        owned_runtime: Some(runtime),
        handle,
    });
```

注意顺序：先 `clone` 出 Handle，再把 `runtime` move 进全局。此后全局里的 `handle` 字段是全天候可用的任务入口，而 `runtime` 只在销毁时才被再次触碰：

[crates/gpui_tokio/src/gpui_tokio.rs:42-48](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L42-L48)：`Drop` 时用 `shutdown_background()` 非阻塞关停自己拥有的运行时（普通 drop 的关停是同步的，可能阻塞当前线程去等待收尾；`shutdown_background` 只通知关闭、不等待任务自然结束，能尽快返回。GPUI 的全局销毁发生在前台线程，阻塞它不可取——详细分析留给 u2-l2）：

```rust
impl Drop for GlobalTokio {
    fn drop(&mut self) {
        if let Some(runtime) = self.owned_runtime.take() {
            runtime.shutdown_background();
        }
    }
}
```

而两个字段的类型差异直观体现了「拥有 vs 引用」：

[crates/gpui_tokio/src/gpui_tokio.rs:35-38](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L35-L38)：`owned_runtime` 是 `Option`（`init_from_handle` 路径下没有所有权），`handle` 永远存在：

```rust
struct GlobalTokio {
    owned_runtime: Option<tokio::runtime::Runtime>,
    handle: tokio::runtime::Handle,
}
```

真正「按遥控器」的调用点在 `Tokio::spawn` 里：

[crates/gpui_tokio/src/gpui_tokio.rs:62](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L62)：用全局里存的 `handle` 调用 `Handle::spawn`——这行代码执行在 GPUI 的线程上，完全不在 Tokio 上下文中，`Handle::spawn` 却照常工作，这就是桥接得以成立的第一块基石：

```rust
            let join_handle = tokio.handle.spawn(f);
```

#### 4.2.4 代码实践

**实践目标**：完成规格指定的任务——不用 `#[tokio::main]`，构建运行时、克隆 Handle、在 Handle 上 spawn 一个 sleep 任务并 await 它的 JoinHandle；同时验证「Handle::spawn 不需要运行时上下文，`tokio::spawn` 需要」。

**操作步骤**（示例代码，接 4.1.4 的项目）：

把 `src/main.rs` 替换为：

```rust
use std::time::Duration;

fn main() {
    // 1. 手动构建运行时（与 gpui_tokio::init 一致）
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .expect("Failed to initialize Tokio");

    // 2. 克隆 Handle：对运行时的"遥控器"，可携带到任意线程
    let handle: tokio::runtime::Handle = runtime.handle().clone();

    // 3. main 线程并不在 Tokio 运行时上下文里，
    //    但 Handle::spawn 仍能把任务投进运行时
    let join_handle = handle.spawn(async {
        println!(
            "tokio task on thread {:?} ({:?})",
            std::thread::current().id(),
            std::thread::current().name()
        );
        tokio::time::sleep(Duration::from_millis(200)).await;
        "sleep done"
    });

    // 4. 回到 runtime 上 await 它的 JoinHandle
    let result = runtime.block_on(join_handle);
    println!("result = {result:?}");
}
```

然后做两个对照实验：

- 实验 A：原样 `cargo run`。
- 实验 B：把 `handle.spawn(async { ... })` 改成自由函数 `tokio::spawn(async { ... })`，再 `cargo run`。

**需要观察的现象**：实验 A 打印出任务所在线程（形如 `ThreadId(2)`，线程名 `Some("tokio-runtime-worker")`）和 `result = Ok("sleep done")`；实验 B 直接 panic，报错大意为「必须在 Tokio 运行时上下文中调用」。

**预期结果**：实验 A 证明 Handle 可在运行时之外驱动 spawn——`gpui_tokio.rs:62` 正是这么做的；实验 B 证明自由函数 `tokio::spawn` 没有这个能力，所以桥接必须先持有 Handle。线程 Id 的具体数值取决于调度，待本地验证；线程名 `tokio-runtime-worker` 是多线程调度器 worker 的默认名。

#### 4.2.5 小练习与答案

**练习 1**：drop 一个 `Handle` 会关停运行时吗？什么时候运行时才会真正关停？

**答案**：不会。Handle 只是引用，克隆和丢弃都不影响运行时；只有 `Runtime` 本体被 drop（或调用其 `shutdown_*` 方法）才触发关停。`gpui_tokio` 把两者分开存在 `GlobalTokio` 的两个字段里，就是这个关系。

**练习 2**：`tokio::spawn`（自由函数）与 `Handle::spawn`（方法）的核心区别是什么？`gpui_tokio` 为什么只能用后者？

**答案**：自由函数依赖「当前线程正处于某个 Tokio 运行时上下文」这一隐式环境，离开运行时调用会 panic；`Handle::spawn` 所需的运行时信息由 Handle 显式携带，任意线程可用。GPUI 代码不在 Tokio 上下文中，所以桥接必须走 Handle。

**练习 3**：`init` 与 `init_from_handle` 存进全局的内容有何不同？各自适合什么场景？

**答案**：`init` 存 `Runtime` + `Handle`（`owned_runtime: Some`），自己负责关停；`init_from_handle` 只存 `Handle`（`owned_runtime: None`），运行时归外部所有。前者是默认的「一站式」用法；后者适合宿主已经有自己的 Tokio 运行时（或想自定义线程数、想在 GPUI 之外也使用它）的场景。

### 4.3 `JoinHandle` 与 `abort_handle`：取消机制

#### 4.3.1 概念说明

任务投进 Tokio 后拿到的收据是 **`JoinHandle<T>`**，它同时是两样东西：

1. **任务句柄**：提供 `abort()`（取消任务）与 `abort_handle()`（生成一个独立的「取消按钮」）；
2. **一个 `Future`**：`Output = Result<T, JoinError>`。await 它会等到任务结束——

| 任务结局 | await `JoinHandle` 得到 | 判别方法 |
| --- | --- | --- |
| 正常完成，产出 `T` | `Ok(value)` | — |
| 任务内部 panic | `Err(JoinError)` | `JoinError::is_panic() == true` |
| 被取消（abort） | `Err(JoinError)` | `JoinError::is_cancelled() == true` |

**`AbortHandle`** 是从 `JoinHandle::abort_handle()` 分离出来的独立句柄，只保留「取消」能力：调用它的 `abort()` 会在**下一个 await 点**把任务对应的 future 直接 drop 掉（协作式取消，不是立刻中断线程）。把 abort 能力从 JoinHandle 上分离出来，意味着可以把「取消按钮」交给第三方保管，而 JoinHandle 仍留给等待结果的人——`gpui_tokio` 正是把这个分离玩成了取消协议。

另外注意 `gpui_tokio` 把 `JoinError` 作为自己的公共错误类型转发出来：

[crates/gpui_tokio/src/gpui_tokio.rs:6](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L6)：`pub use tokio::task::JoinError;`——因为 `Tokio::spawn` 返回 `Task<Result<R, JoinError>>`，下游处理错误时需要这个类型名。

#### 4.3.2 核心流程

abort 联动的两条时序（对照 `Tokio::spawn` 的实现）：

```text
正常完成路径：
  handle.spawn(f) ─► join_handle ─► background_spawn 中 await ─► Ok(R)
                                     └─ drop(cancel) 提前解除取消守卫（不触发 abort）

被取消路径（GPUI Task 被 drop，详见 u2-l4）：
  defer 的闭包在 Drop 时执行 ─► abort_handle.abort()
      ─► 任务 future 在下一个 await 点被丢弃
      ─► join_handle.await 返回 Err(JoinError)，is_cancelled() == true
```

本讲只需看清前半段（abort_handle 如何取得、abort 如何触发）；「谁在什么时机调用 abort」是 u2-l4 的主角。

#### 4.3.3 源码精读

[crates/gpui_tokio/src/gpui_tokio.rs:61-66](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L61-L66)：spawn 的核心三步——用 Handle 投递任务拿到 `join_handle`，立刻取 `abort_handle`，再把 abort 包进一个 `defer` 守卫（`defer` 返回的 `Deferred` 在被 drop 时执行闭包，即「到时取消」；本讲先记住「abort_handle 是那个取消按钮」即可）：

```rust
        cx.read_global(|tokio: &GlobalTokio, cx| {
            let join_handle = tokio.handle.spawn(f);
            let abort_handle = join_handle.abort_handle();
            let cancel = defer(move || {
                abort_handle.abort();
            });
```

[crates/gpui_tokio/src/gpui_tokio.rs:67-71](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L67-L71)：把 `join_handle` 交给 GPUI 的后台执行器 await，得到 `Result<R, JoinError>` 后**先 `drop(cancel)` 解除取消守卫再返回结果**——任务已正常结束，不能再让守卫误触发 abort：

```rust
            cx.background_spawn(async move {
                let result = join_handle.await;
                drop(cancel);
                result
            })
```

对比 [crates/gpui_tokio/src/gpui_tokio.rs:90](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L90)：`spawn_result` 里是 `let result = join_handle.await?;`——用 `?` 把 `JoinError` 也折进 `anyhow::Result`，错误分层的细节在 u2-l5 讲，本讲关注的是同一行 await 背后的 `Result<T, JoinError>` 语义。

#### 4.3.4 代码实践

**实践目标**：亲眼验证 `abort_handle().abort()` 的效果，以及 `JoinError::is_cancelled` 的判别方式——这就是 u2-l4 取消联动的原子操作。

**操作步骤**（示例代码，接前一个项目）：

```rust
use std::time::Duration;

fn main() {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .expect("Failed to initialize Tokio");
    let handle = runtime.handle().clone();

    // 任务 A：无限心跳的长任务
    let join_handle_a = handle.spawn(async {
        loop {
            println!("heartbeat");
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    });
    // 从 JoinHandle 上分离出"取消按钮"
    let abort_handle_a = join_handle_a.abort_handle();

    // 任务 B：正常完成的短任务
    let join_handle_b = handle.spawn(async { 1 + 1 });

    // 等一会儿让 A 打印几次心跳，然后取消它
    runtime.block_on(tokio::time::sleep(Duration::from_millis(350)));
    abort_handle_a.abort();

    let a = runtime.block_on(join_handle_a);
    let b = runtime.block_on(join_handle_b);
    println!("B = {b:?}");
    match a {
        Ok(_) => println!("A 完成（不符合预期）"),
        Err(join_error) => println!("A cancelled? {}", join_error.is_cancelled()),
    }
}
```

**需要观察的现象**：先打印约 3 次 `heartbeat`（次数取决于调度时序），`abort()` 之后心跳立即停止，最后输出 `B = Ok(2)` 与 `A cancelled? true`。

**预期结果**：被 abort 的任务，其 `JoinHandle` await 得到 `Err` 且 `is_cancelled() == true`；未受影响的任务 B 正常 `Ok(2)`。心跳次数的具体值待本地验证（调度相关），但「abort 后心跳停止」与「cancelled? true」是确定的。

#### 4.3.5 小练习与答案

**练习 1**：await 一个已被 abort 的任务的 `JoinHandle`，得到什么？如何区分「被取消」与「panic」？

**答案**：得到 `Err(JoinError)`。`join_error.is_cancelled() == true` 表示被取消；`is_panic() == true` 表示任务内部 panic（还可以用 `into_panic()` 取回 panic 载荷）。

**练习 2**：调用 `abort()` 后任务会立刻停在执行的半途中吗？

**答案**：不会。Tokio 的取消是协作式的：`abort()` 标记任务待取消，任务对应的 future 会在**下一个 await 点**被整体 drop，此后不再被调度。所以「停止」发生在 await 边界，而不是任意指令处。

**练习 3**：既然 `JoinHandle` 自己就有 `abort()` 方法，为什么 `gpui_tokio` 还要先 `abort_handle()` 再包进 `defer`？

**答案**：因为 `join_handle` 必须被 move 进后台任务里去 await（拿到结果），而取消能力要留在**等待结果的那一方**随时可触发。`abort_handle()` 把「取消按钮」从「等待结果的句柄」上分离出来，两者可以分别持有——守卫 `cancel` 拿按钮，`background_spawn` 的闭包拿 JoinHandle。若不分离，取消方手里没有 JoinHandle 就无从 abort。`defer` 如何在恰当时机按下按钮，是 u2-l4 的内容。

## 5. 综合实践

**任务：写一个「迷你 runtime 体检器」，把本讲三个模块串成一条线，并与 `gpui_tokio` 源码逐行对表。**

要求在独立项目里实现一个程序（示例代码）：

1. 用 `Builder::new_multi_thread().worker_threads(2).enable_all().build()` 创建运行时（对应 [gpui_tokio.rs:13-18](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L13-L18)）；
2. 克隆 `Handle`（对应 [gpui_tokio.rs:20](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L20)）；
3. 用 Handle spawn 3 个短任务，各自打印 `std::thread::current().id()` 与线程名，再 spawn 1 个每 100ms 打印心跳的长任务（对应 [gpui_tokio.rs:62](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L62)）；
4. 取长任务的 `abort_handle()`，350ms 后调用 `abort()`（对应 [gpui_tokio.rs:63-66](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L63-L66)）；
5. 用 `runtime.block_on` 依次 await 全部 4 个 `JoinHandle`，打印每个是 `Ok` 还是 `Err`，`Err` 的注明 `is_cancelled()`；
6. 程序结束前，把你的每个步骤与上表中的源码行号对照，回答两个问题：
   - 3 个短任务分别跑在哪几个线程上？2 个 worker 线程够不够分配，说明了 `worker_threads(2)` 的什么特点？（观察任务分布即可，具体分配待本地验证）
   - 在你的程序里，`gpui_tokio` 中 GPUI 的角色（`background_spawn` + await）由谁扮演？（答案：main 线程 + `runtime.block_on`——你的程序就是一座没有 GPUI 的「单侧桥」。）

## 6. 本讲小结

- Tokio 运行时要手动构建：`Builder::new_multi_thread().worker_threads(2).enable_all().build()`，`worker_threads` 控制线程池大小（默认取 CPU 核数），`enable_all` 启用 I/O 与定时器驱动，`build` 返回 `Result`。
- `Runtime` 拥有线程与驱动，drop 即关停；`Handle` 是可廉价克隆的「遥控器」，`Handle::spawn` **不要求调用线程处于 Tokio 上下文**，这是 `gpui_tokio` 能从 GPUI 线程投递任务的基石（自由函数 `tokio::spawn` 则要求上下文）。
- `JoinHandle<T>` 既是句柄又是 `Future<Output = Result<T, JoinError>>`：正常完成 `Ok`，panic 或被取消 `Err`，用 `is_panic()`/`is_cancelled()` 区分。
- `abort_handle()` 从 JoinHandle 分离出独立的「取消按钮」，`abort()` 在下一个 await 点协作式终止任务——`gpui_tokio` 用它构建「GPUI 任务被 drop 则 Tokio 任务被取消」的协议。
- `gpui_tokio` 只启用 tokio 的 `rt` + `rt-multi-thread` feature，与其「只建运行时、只 spawn、只转发 JoinHandle」的职责严格对齐；`time`/`net` 等能力由下游 crate 经 feature unification 补齐。

## 7. 下一步学习建议

前置知识到此补齐，可以进入第二单元的源码精读：

1. **u2-l1（初始化流程）**：把本讲 4.1/4.2 的知识代入 `init` 与 `init_from_handle` 的完整流程，并找到 Zed 主程序里的调用点；
2. **u2-l2（GlobalTokio 生命周期）**：深入 GPUI 的 `Global` 机制与 `Drop` 中 `shutdown_background` 的取舍——本讲 4.2 留下的悬念在那里展开；
3. **u2-l4（取消语义）**：本讲 4.3 的 `abort_handle` 加上 `gpui_util::defer` 守卫，完整解释「drop GPUI Task 为什么会取消 Tokio 任务」。

延伸阅读推荐 Tokio 官方文档的 [runtime 模块](https://docs.rs/tokio/latest/tokio/runtime/index.html)与 [task 模块](https://docs.rs/tokio/latest/tokio/task/index.html)，重点看 `Runtime`、`Handle`、`JoinHandle` 三个类型的文档注释——`gpui_tokio` 的 101 行里，每一行 Tokio API 的语义都在那里有权威定义。
