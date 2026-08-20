# gpui_tokio 是什么：为什么 Zed 需要桥接两个异步运行时

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `gpui_tokio` 这个 crate 在 Zed 工作区中的位置，以及它解决的「双运行时桥接」问题是什么。
2. 浏览它的唯一源码文件 `src/gpui_tokio.rs`（只有 100 行），并对 `init` 函数和 `Tokio` 结构体的公共方法签名形成整体印象。
3. 在 workspace 的 `Cargo.toml` 中找到 `gpui_tokio` 的注册条目，理解它是如何被其他 crate 引用的。
4. 列出依赖它的下游 crate（`zed`、`client`、`cloud_api_client`、`extension_host`、`livekit_client` 等），并在本地亲手统计出一份调用方清单。

本讲是整本手册的第一讲，不要求你已经读过 Zed 或 GPUI 的任何代码。我们只建立一个正确的「地图感」，细节留给后续讲义。

## 2. 前置知识

### 2.1 什么是异步运行时（async runtime）

Rust 语言本身只定义了 `async/await` 的语法，不提供调度器。你写一个 `async fn`，它只是一个「可以被暂停和恢复的计算描述」，必须有一个人负责调度它——这个调度者就是**异步运行时**。

一个运行时通常包含：

- **调度器**：决定哪个任务在哪个线程上、什么时候被推进。
- **I/O 多路复用**：监听网络、定时器等事件，事件就绪时唤醒对应的任务。
- **spawn 接口**：把一个 future 丢进运行时里执行，并返回一个可以 await 结果的句柄（handle）。

### 2.2 Zed 里同时存在两个运行时

- **GPUI 自带的执行器**：Zed 的界面和大部分业务代码跑在 GPUI 提供的执行器上。它的特点是所有 UI 和实体状态都在**一个前台线程**上推进（保证无数据竞争），耗时工作则通过后台线程池完成。GPUI 侧的任务句柄叫 `Task<T>`。
- **Tokio 运行时**：Rust 生态中最流行的异步运行时。大量第三方库——HTTP 客户端 `reqwest`、WebSocket 库、LiveKit 实时音视频库等——内部直接依赖 Tokio，甚至要求「必须在 Tokio 运行时上下文里」才能初始化。Tokio 侧的任务句柄叫 `JoinHandle<T>`。

问题来了：Zed 的业务代码在 GPUI 的世界里，想用的库却在 Tokio 的世界里。两个世界的任务句柄互不认识，不能直接互相 await。

`gpui_tokio` 就是这两个世界之间的一座小桥：它在 GPUI 应用里创建（或接纳）一个 Tokio 运行时，把它存成全局单例，并提供几个简单的静态方法，让你把一个 Tokio 任务包装成 GPUI 的 `Task`。整座桥只有约 100 行代码。

### 2.3 几个会用到的 Rust/Cargo 术语

| 术语 | 含义 |
| --- | --- |
| crate | Rust 的编译单元， roughly 等于「一个库或一个可执行程序」。 |
| workspace | 多个 crate 共享一套锁文件和依赖版本的仓库组织方式，Zed 是一个巨大 的 workspace。 |
| feature | 依赖的可选功能开关，例如 tokio 的 `rt-multi-thread`。 |
| 全局（Global） | GPUI 提供的「应用级单例」机制，后续讲义会精读。 |
| `pub use` | 把别的 crate 里的类型重新导出，让用户不必直接依赖那个 crate。 |

## 3. 本讲源码地图

本讲涉及的关键文件一共三个（外加两个「现场参观」用的调用方文件）：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `crates/gpui_tokio/src/gpui_tokio.rs` | 100 行 | **全部核心源码**：初始化函数、全局单例、桥接方法都在这一个文件里。 |
| `crates/gpui_tokio/Cargo.toml` | 19 行 | crate 的构建配置：库名、依赖、feature。 |
| `Cargo.toml`（仓库根目录） | — | workspace 配置，注册了 `gpui_tokio` 这个成员和依赖条目。 |
| `crates/zed/src/main.rs` | — | Zed 主程序，第 499 行调用了 `gpui_tokio::init(cx)`，是理解「谁在启动这座桥」的最好现场。 |

> 提示：这是一个「小而横跨两个世界」的 crate。读它的正确姿势不是「文件很多，逐个看」，而是「文件只有一个，但每一行都同时牵扯 GPUI 和 Tokio 两边的概念」。

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. `gpui_tokio::init` —— 桥接的起点。
2. `Tokio` 结构体的公共方法签名 —— 桥上有哪些通道。
3. workspace `Cargo.toml` 中的 `gpui_tokio` 条目 —— 这座桥如何挂进 Zed 大厦。

### 4.1 模块一：`gpui_tokio::init` —— 桥接的起点

#### 4.1.1 概念说明

要使用桥，得先把 Tokio 运行时「建起来并放到一个大家都能找到的地方」。这就是 `init` 的职责：

- 创建一个 Tokio 多线程运行时（很小，只有 2 个工作线程）。
- 把运行时的 `Handle`（可以理解为「遥控器」，克隆一份也能远程控制运行时）连同运行时本体一起，存进 GPUI 的全局状态。
- 之后任何代码只要拿到 GPUI 上下文 `cx`，就能取到这个 Handle，往 Tokio 线程池里投放任务。

为什么只有 2 个工作线程？源码里的注释写得很直白：既然现在有两个执行器了（GPUI 自己的 + Tokio 的），尽量把额外占用控制到最小。Zed 的重活优先走 GPUI 的后台线程池，Tokio 这边主要是为了满足那些「非 Tokio 不可」的生态库。

#### 4.1.2 核心流程

`init(cx)` 的执行过程可以概括为四步：

```text
gpui_tokio::init(cx)
  ├─ 1. tokio::runtime::Builder::new_multi_thread()   声明要建多线程运行时
  ├─ 2. .worker_threads(2).enable_all()               限定 2 个工作线程；启用 IO/定时器
  ├─ 3. .build()                                      真正创建 Runtime
  ├─ 4. runtime.handle().clone()                      克隆出可自由复制的 Handle
  └─ 5. cx.set_global(GlobalTokio { owned_runtime: Some(runtime), handle })
                                                       存入 GPUI 全局单例
```

配套还有一个孪生函数 `init_from_handle(cx, handle)`：它不创建运行时，只保存外部传入的 Handle（`owned_runtime` 字段为 `None`）。适用场景是「调用方自己已经建好了运行时，或者想自定义线程数」。

存进全局之后，谁负责在应用退出时关掉 Tokio 运行时？`GlobalTokio` 实现了 `Drop`：当 GPUI 应用销毁、全局被清理时，它会对自持有的运行时调用 `shutdown_background()`（非阻塞地关停）。这套生命周期机制是第二单元第 2 讲的主题，本讲只需知道「有人善后」即可。

#### 4.1.3 源码精读

先看文件开头的导入与再导出（第 1–6 行）：

> [crates/gpui_tokio/src/gpui_tokio.rs:1-6](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L1-L6) —— 引入 GPUI 的 `App`/`AppContext`/`Global`/`ReadGlobal`/`Task` 与 `gpui_util::defer`，并把 `tokio::task::JoinError` 直接再导出，这样下游 crate 处理取消/panic 错误时不必再显式依赖 tokio。

接着是本模块的主角 `init`（第 8–25 行）：

> [crates/gpui_tokio/src/gpui_tokio.rs:8-25](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L8-L25) —— `init` 用 `Builder::new_multi_thread()` 创建一个 `worker_threads(2)`、`enable_all()` 的 Tokio 运行时；`.expect("Failed to initialize Tokio")` 表示创建失败直接 panic（启动期致命错误）；随后克隆 `handle`，把「运行时本体 + Handle」一起装进 `GlobalTokio` 并 `cx.set_global` 写入全局。

文档注释（第 8–11 行）同时告诉了你逃生门：如果需要更多线程或在 GPUI 之外访问运行时，可以自己创建运行时，然后走 `init_from_handle`。

孪生函数与全局类型（第 27–48 行）：

> [crates/gpui_tokio/src/gpui_tokio.rs:27-33](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L27-L33) —— `init_from_handle` 只保存外部 Handle，`owned_runtime` 为 `None`，因此 Drop 时也不会去关停别人的运行时。

> [crates/gpui_tokio/src/gpui_tokio.rs:35-48](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L35-L48) —— `GlobalTokio` 结构体持有 `owned_runtime: Option<Runtime>` 与 `handle: Handle`；`impl Global for GlobalTokio {}` 让它具备 GPUI 全局单例资格；`Drop` 实现在全局销毁时调用 `runtime.shutdown_background()` 非阻塞关停。

最后看看真实的调用现场——Zed 主程序启动时：

> [crates/zed/src/main.rs:499](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/zed/src/main.rs#L499) —— 在 `release_channel::init` 之后、`settings::init` 之前的启动序列里，一行 `gpui_tokio::init(cx);` 就把 Tokio 运行时挂进了整个应用。

> [crates/zed/src/main.rs:515-520](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/zed/src/main.rs#L515-L520) —— 初始化后仅十几行，创建 HTTP 客户端 `ReqwestClient` 时就用 `let _guard = Tokio::handle(cx).enter();` 进入 Tokio 运行时上下文——这正是「reqwest 这类库需要 Tokio 在场」的实证。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：亲眼确认「这座桥的全部源码只有一个文件、100 行」，并找到它在 Zed 主程序中的安装位置。
2. **操作步骤**：
   - 打开 `crates/gpui_tokio/src/gpui_tokio.rs`，从头读到尾（不需要看懂每一行，本讲只关注 `init` 和方法签名）。
   - 数一数文件里所有 `pub` 开头的项：应该恰好是 4 个——`pub use tokio::task::JoinError`、`pub fn init`、`pub fn init_from_handle`、`pub struct Tokio`（其方法在下文 4.2 精读）。
   - 打开 `crates/zed/src/main.rs`，跳到第 499 行，向上向下各扫 10 行，观察 `gpui_tokio::init(cx)` 夹在哪些初始化步骤之间。
3. **需要观察的现象**：`init` 在启动序列里出现得非常早，早于 HTTP 客户端的创建（第 515 行附近）。
4. **预期结果**：你能用自己的话说出——「Zed 启动时先建好 Tokio 运行时存为全局，后面的功能模块随取随用」。具体的运行输出无需执行命令即可确认（纯阅读型实践）。

#### 4.1.5 小练习与答案

**练习 1**：`init` 为什么把 `worker_threads` 设为 2，而不是越多越好？

**参考答案**：源码第 14 行的注释写道：由于现在同时存在两个执行器，应当尽量控制自身的资源占用（footprint）。Zed 的耗时任务优先走 GPUI 自己的后台线程池；Tokio 运行时主要是为依赖 Tokio 的生态库服务的，2 个线程通常足够，多开只会白白占用线程资源。

**练习 2**：`init` 和 `init_from_handle` 的本质区别是什么？各适合什么场景？

**参考答案**：`init` 自己创建运行时并把 `Runtime` 本体存进 `owned_runtime`（`Some`），因此应用退出时要负责关停它；`init_from_handle` 只保存外部 Handle（`owned_runtime` 为 `None`），运行时的生死由外部负责。前者适合「应用里没有别的 Tokio、想一行代码搞定」的场景（如 Zed 主程序、测试）；后者适合「调用方已有运行时、或想自定义线程数/在 GPUI 之外访问运行时」的场景。

**练习 3**：如果 `Builder::build()` 失败了会发生什么？这样设计合理吗？

**参考答案**：代码用 `.expect("Failed to initialize Tokio")` 直接 panic。合理：这发生在应用启动阶段，运行时都建不起来意味着后续所有依赖 Tokio 的功能（网络请求、扩展系统、音视频）都无法工作，与其带病运行不如快速失败、暴露清晰错误。这符合「启动期致命错误用 panic、运行期错误用 Result」的常见取舍。

### 4.2 模块二：`Tokio` 结构体的公共方法签名 —— 桥上有哪些通道

#### 4.2.1 概念说明

`pub struct Tokio {}` 是一个**没有字段的结构体**，它唯一的用途是充当命名空间：所有桥接方法都以静态方法的形式挂在它上面，调用时写作 `gpui_tokio::Tokio::spawn(cx, future)`。这是 Rust 中「无状态工具类」的惯用写法（不需要实例化，也不持有任何数据——真正的数据在全局单例 `GlobalTokio` 里）。

桥上共有三条通道：

| 方法 | 签名要点 | 适用场景 |
| --- | --- | --- |
| `Tokio::spawn` | `fn spawn<C, Fut, R>(cx: &C, f: Fut) -> Task<Result<R, JoinError>>` | 把一个输出普通值 `R` 的 future 丢到 Tokio 线程池执行，结果以 GPUI `Task` 形式返回。 |
| `Tokio::spawn_result` | `fn spawn_result<C, Fut, R>(cx: &C, f: Fut) -> Task<anyhow::Result<R>>` | future 本身输出 `anyhow::Result<R>`（业务可能失败），返回类型也相应变成 `Task<anyhow::Result<R>>`。 |
| `Tokio::handle` | `fn handle(cx: &App) -> tokio::runtime::Handle` | 直接暴露原始 Tokio `Handle`，用于绕过封装的原生用法（如进入运行时上下文 `enter()`）。 |

两个 spawn 方法都有一条重要契约，写在文档注释里：**如果 GPUI 侧返回的 `Task` 被 drop，Tokio 那边的任务会被取消（abort）**。这条「drop 即取消」的联动是本 crate 最精巧的部分，由 `gpui_util::defer` 守卫实现，我们留到第二单元第 4 讲专门剖析；本讲先记住这条契约本身。

`spawn` 与 `spawn_result` 的返回类型差异体现了两层错误：

- `JoinError`：任务层面出了问题——被取消，或 panic 了（由 Tokio 报告）。
- `anyhow::Result` 里的错误：业务层面的失败（网络不通、响应格式错误等）。

`spawn_result` 通过 `join_handle.await?` 把 `JoinError` 也折叠进 `anyhow::Error`，于是调用方只需要处理一种错误类型，代价是丢失了「任务级失败」与「业务级失败」的类型区分。

#### 4.2.2 核心流程

`Tokio::spawn(cx, f)` 内部（第 61–72 行）可以拆成四步：

```text
Tokio::spawn(cx, f)
  ├─ 1. cx.read_global(|tokio: &GlobalTokio, cx| ...)   从全局取出 Tokio Handle
  ├─ 2. tokio.handle.spawn(f)                           future 投放到 Tokio 线程池，得 join_handle
  ├─ 3. join_handle.abort_handle() + defer(...)         取消句柄 + RAII 守卫：守卫被 drop 时 abort
  └─ 4. cx.background_spawn(async move { join_handle.await ... })
                                                        在 GPUI 后台等待 Tokio 结果，
                                                        包装成 GPUI Task 返回
```

一句话概括数据流向：**future 去 Tokio 那边跑，结果通过一个 GPUI 后台任务送回来，中途还有一根「GPUI 放弃 → Tokio 中止」的联动线**。

#### 4.2.3 源码精读

> [crates/gpui_tokio/src/gpui_tokio.rs:50-52](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L50-L52) —— `pub struct Tokio {}`：空结构体，纯静态方法的宿主，不持有任何状态。

> [crates/gpui_tokio/src/gpui_tokio.rs:53-73](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L53-L73) —— `Tokio::spawn` 的完整签名与实现。注意泛型约束 `C: AppContext`（任何 GPUI 上下文都能用，不限于 `App`）、`Fut: Future<Output = R> + Send + 'static`（future 必须能跨线程送到 Tokio 线程池），以及返回类型 `Task<Result<R, JoinError>>`。文档注释明确写了「GPUI 任务被 drop 时 Tokio 任务会被取消」。

> [crates/gpui_tokio/src/gpui_tokio.rs:75-95](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L75-L95) —— `Tokio::spawn_result` 与 `spawn` 结构完全相同，差别只有两处：future 的输出类型是 `anyhow::Result<R>`；第 90 行用 `join_handle.await?` 把 `JoinError` 也转成 `anyhow::Error`。

> [crates/gpui_tokio/src/gpui_tokio.rs:97-99](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L97-L99) —— `Tokio::handle` 只有两行：从全局取出 `GlobalTokio` 并克隆其 `handle` 字段返回。这是「逃生通道」：当你需要 Tokio 的原生能力（如 `enter()` 进入运行时上下文）时用它。

再看几个真实调用点，感受这条通道在 Zed 里的实际形态（每个都是一行调用）：

> [crates/client/src/client.rs:1357](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/client/src/client.rs#L1357) —— `client` crate 用 `Tokio::spawn_result(cx, {...})` 在 Tokio 线程池里建立代理 TCP 连接（连接失败是业务错误，所以选 `spawn_result`）。

> [crates/extension_host/src/wasm_host.rs:717](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/extension_host/src/wasm_host.rs#L717) —— 扩展系统用 `Arc::new(gpui_tokio::Tokio::spawn(cx, extension_task))` 驱动 WASM 扩展的加载/运行（结果可能是 `JoinError`，且需要多处共享同一个任务，所以包了 `Arc`）。

> [crates/livekit_client/src/livekit_client/linux.rs:153](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/livekit_client/src/livekit_client/linux.rs#L153) —— Linux 屏幕采集的常驻循环跑在 `Tokio::spawn` 上（LiveKit 生态依赖 Tokio）。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：把「三条通道」的签名与真实调用点一一对应起来。
2. **操作步骤**：
   - 在仓库根目录执行下面的搜索，找出所有直接调用点：

     ```bash
     grep -rn "gpui_tokio::Tokio::" --include="*.rs" crates/ | grep -v test
     ```

   - 再搜索 `Tokio::handle` 的用法（注意有些文件会 `use gpui_tokio::Tokio;` 之后直接写 `Tokio::handle`）：

     ```bash
     grep -rn "Tokio::handle" --include="*.rs" crates/ | head -20
     ```

3. **需要观察的现象**：调用点数量并不多（个位数到十几个），且集中在 `client`、`cloud_api_client`、`extension_host`、`livekit_client`、`call`、`language_models` 等与网络/扩展/音视频相关的 crate。
4. **预期结果**：本讲义编写时已在 HEAD `b0e37a6c` 上执行过同等搜索，确认的直接调用点包括（不限于）：
   - `Tokio::spawn_result`：`crates/client/src/client.rs:1357`、`crates/cloud_api_client/src/websocket/native.rs:79`
   - `Tokio::spawn`：`crates/livekit_client/src/livekit_client/playback.rs:444`、`crates/livekit_client/src/livekit_client/linux.rs:153`、`crates/extension_host/src/wasm_host.rs:713` 与 `:717`
   - `Tokio::handle`：`crates/zed/src/main.rs:516`、`crates/call/src/call_impl/room.rs:1740`、`crates/eval_cli/src/headless.rs:57`、`crates/remote_server/src/server.rs:686`、`crates/language_models/src/provider/bedrock.rs:571` 等

   你本地跑出的行号应与上述一致（同一 HEAD 下）；若你 checkout 了更新的提交，行号可能漂移，属正常现象。

#### 4.2.5 小练习与答案

**练习 1**：`Tokio` 结构体一个字段都没有，为什么不直接写成三个顶层自由函数 `tokio_spawn(cx, f)`？

**参考答案**：写成空结构体 + 静态方法有两个好处：一是命名空间更整洁，`Tokio::spawn` / `Tokio::spawn_result` / `Tokio::handle` 形成一组显式相关联的 API，顶层自由函数则会和其他 crate 的 `spawn` 混在一起、需要起更长的名字；二是将来若需要加入配置状态（例如多个运行时），可以在结构体上扩展而不破坏调用方写法。这是 Rust 里常见的「无状态命名空间宿主」模式。

**练习 2**：我有一个 future，它的输出是 `anyhow::Result<HttpResponse>`，应该用 `spawn` 还是 `spawn_result`？

**参考答案**：用 `spawn_result`。如果用 `spawn`，返回类型会变成 `Task<Result<anyhow::Result<HttpResponse>, JoinError>>`——双层嵌套的错误，调用方要拆两次。`spawn_result` 专门为「future 本身输出 `anyhow::Result`」设计，它把 `JoinError` 通过 `?` 折叠进 `anyhow::Error`，调用方只需处理一层 `anyhow::Result<HttpResponse>`。

**练习 3**：`Tokio::spawn` 的泛型参数 `C: AppContext` 意味着什么？

**参考答案**：意味着这个方法不要求你手里恰好是 `&mut App`，任何实现了 GPUI `AppContext` trait 的上下文（如 `&TestAppContext`、各种实体的 `Context<T>` 解引用得到的 `App` 视图等）都能调用，只要该上下文能访问全局状态并具备 spawn 能力。这让 `gpui_tokio` 在生产代码和测试代码里都以同样的方式使用。同理 `read_global` 也是 `AppContext` 提供的方法。

### 4.3 模块三：workspace `Cargo.toml` 中的 `gpui_tokio` 条目 —— 这座桥如何挂进 Zed 大厦

#### 4.3.1 概念说明

Zed 是一个拥有上百个 crate 的 Cargo workspace。workspace 根目录的 `Cargo.toml` 承担两件事，`gpui_tokio` 在其中各占一行：

1. **成员注册**（`members` 列表）：告诉 Cargo 「`crates/gpui_tokio` 是本 workspace 的一个成员」。
2. **依赖条目**（`[workspace.dependencies]` 表）：定义 `gpui_tokio = { path = "crates/gpui_tokio" }`，之后所有成员 crate 只要在自己的 `Cargo.toml` 里写 `gpui_tokio.workspace = true`，就能以统一版本引用它，避免每个 crate 重复写路径、也保证全仓库只有一份编译产物。

而 crate 自己的 `Cargo.toml` 也值得逐行读一遍——它一共只有 19 行，却包含三个信息点：

- `[lib] path = "src/gpui_tokio.rs"`：库名不叫默认的 `src/lib.rs` 而叫 `src/gpui_tokio.rs`。这是 Zed 仓库的统一规范（见仓库 `CLAUDE.md`：新 crate 应指定描述性的库文件名，如 `gpui.rs`、`main.rs`），好处是打开多个文件时标签页可读性更好。
- `doctest = false`：不为这个库跑文档测试（它的示例都需要完整运行时环境，doc-test 意义不大）。
- `tokio = { workspace = true, features = ["rt", "rt-multi-thread"] }`：只启用了 Tokio 的 `rt`（运行时核心）和 `rt-multi-thread`（多线程调度器）两个 feature。没有 `net`、没有 `time`、没有 `fs`、没有 `macros`——这个 crate 只需要「建运行时 + spawn」，依赖面被压到最小，编译更快、体积更小。

#### 4.3.2 核心流程

一个下游 crate（以 `client` 为例）使用这座桥的完整装配链：

```text
workspace 根 Cargo.toml
  ├─ members: "crates/gpui_tokio"                      （注册成员）
  └─ [workspace.dependencies]
        gpui_tokio = { path = "crates/gpui_tokio" }    （统一依赖条目）
              ↓  (client/Cargo.toml 写 gpui_tokio.workspace = true)
crates/client 依赖 gpui_tokio
              ↓  (代码里 use / 直接路径调用)
client/src/client.rs:1357  gpui_tokio::Tokio::spawn_result(cx, ...)
              ↓  (运行期：谁来 init？)
zed/src/main.rs:499  gpui_tokio::init(cx)              （应用启动时安装全局）
```

要特别注意：**依赖关系（Cargo.toml）和运行期可用性（谁调用了 init）是两回事**。`client` 的 `Cargo.toml` 依赖 `gpui_tokio` 只说明「能编译通过」；运行时 `Tokio::spawn` 之所以能取到 Handle，是因为应用入口（或测试初始化）已经调用过 `gpui_tokio::init(cx)`。这也解释了为什么 Zed 的很多测试文件里会出现一行 `gpui_tokio::init(cx);`——每个测试的 `App` 都是独立的，全局状态不共享（第三单元第 3 讲会专门讲这个模式）。

#### 4.3.3 源码精读

> [Cargo.toml:98](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/Cargo.toml#L98) —— workspace `members` 列表中的 `"crates/gpui_tokio"` 条目，按字母序夹在 `gpui_shared_string` 与 `gpui_util` 之间。

> [Cargo.toml:364](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/Cargo.toml#L364) —— `[workspace.dependencies]` 表中的 `gpui_tokio = { path = "crates/gpui_tokio" }`：所有下游 crate 通过 `.workspace = true` 继承这一条。

> [crates/gpui_tokio/Cargo.toml:1-13](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/Cargo.toml#L1-L13) —— 包名 `gpui_tokio`、版本 `0.1.0`、`edition`/`publish`/`lints` 均从 workspace 继承；`[lib] path = "src/gpui_tokio.rs"` 体现仓库「库文件用描述性命名」的规范；`doctest = false` 关闭文档测试。

> [crates/gpui_tokio/Cargo.toml:15-19](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/Cargo.toml#L15-L19) —— 依赖表：`anyhow`、`gpui`、`gpui_util` 走 workspace 继承；`tokio` 在 workspace 基础上只追加 `rt` 与 `rt-multi-thread` 两个 feature，依赖面刻意收窄。

#### 4.3.4 代码实践（源码阅读型 + 命令验证）

1. **实践目标**：亲眼看到「一行 workspace 条目 → 十几个下游 crate」的扩散方式。
2. **操作步骤**：
   - 在仓库根目录执行：

     ```bash
     grep -rn "gpui_tokio" --include="Cargo.toml" crates/ | grep -v "/zed-crates"
     ```

   - 对每一个命中的文件，打开看它把 `gpui_tokio` 放在了哪个依赖表里（`[dependencies]` 还是 `[dev-dependencies]`，是否 `optional = true`）。
   - 可选：执行 `cargo tree -p gpui_tokio --depth 1` 查看 `gpui_tokio` 自己的直接依赖树。
3. **需要观察的现象**：命中的都是各 crate 的 `Cargo.toml`，行内容几乎清一色是 `gpui_tokio.workspace = true`。
4. **预期结果**：本讲义编写时已在 HEAD `b0e37a6c` 上执行过同等搜索，确认以下 14 个 crate 的 `Cargo.toml` 引用了 `gpui_tokio`：

   | 下游 crate | 引用位置 | 形态 |
   | --- | --- | --- |
   | `agent` | `crates/agent/Cargo.toml:101` | 普通依赖 |
   | `agent_servers` | `crates/agent_servers/Cargo.toml:41` | 可选依赖（`optional = true`，由 feature `dep:gpui_tokio` 开启；第 72 行另有 dev 依赖） |
   | `agent_ui` | `crates/agent_ui/Cargo.toml:62` | 普通依赖 |
   | `call` | `crates/call/Cargo.toml:35` | 普通依赖 |
   | `client` | `crates/client/Cargo.toml:34` | 普通依赖 |
   | `cloud_api_client` | `crates/cloud_api_client/Cargo.toml:28` | 普通依赖 |
   | `collab` | `crates/collab/Cargo.toml:100` | dev 依赖（测试用，位于文件靠后的 dev-dependencies 段） |
   | `edit_prediction_cli` | `crates/edit_prediction_cli/Cargo.toml:35` | 普通依赖 |
   | `eval_cli` | `crates/eval_cli/Cargo.toml:33` | 普通依赖 |
   | `extension_host` | `crates/extension_host/Cargo.toml:31` | 普通依赖 |
   | `language_models` | `crates/language_models/Cargo.toml:41` | 普通依赖 |
   | `livekit_client` | `crates/livekit_client/Cargo.toml:30` | 普通依赖 |
   | `remote_server` | `crates/remote_server/Cargo.toml:44` | 普通依赖 |
   | `zed` | `crates/zed/Cargo.toml:124` | 普通依赖 |

   `cargo tree` 的具体输出**待本地验证**（本讲义未在该环境执行 cargo 命令）；预期直接依赖为 `anyhow`、`gpui`、`gpui_util`、`tokio` 四个。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `crates/client/Cargo.toml` 里只写 `gpui_tokio.workspace = true` 就够了，不用写版本号和路径？

**参考答案**：因为 workspace 根 `Cargo.toml` 的 `[workspace.dependencies]` 表（第 364 行）已经定义了 `gpui_tokio = { path = "crates/gpui_tokio" }`。成员 crate 用 `.workspace = true` 继承这份定义，Cargo 会保证整个 workspace 内所有人对 `gpui_tokio` 的引用解析到同一条路径、同一份编译产物，避免版本漂移和重复编译。

**练习 2**：`gpui_tokio` 对 tokio 只开启 `rt` 和 `rt-multi-thread` 两个 feature。如果某个下游 crate 需要 tokio 的 `net` feature（比如要用 `tokio::net::TcpStream`），该怎么办？

**参考答案**：由下游 crate 在自己的 `Cargo.toml` 里为 tokio 追加 feature（例如 `tokio = { workspace = true, features = ["net"] }`）。Cargo 的 feature 是跨 crate 求并集的：只要依赖图中任何一处启用了 `net`，最终编译出的 tokio 就带 `net`。`gpui_tokio` 自己只声明自己需要的最小集合，这是「每个 crate 声明自己实际用到的功能」的良好习惯。事实上 Zed 仓库 workspace 里 tokio 本身就带了很多 feature，`gpui_tokio` 的克制声明保证了「即使单独编译它也尽量轻」。

**练习 3**：`collab` crate 把 `gpui_tokio` 放在 dev-dependencies 里，这说明了什么？

**参考答案**：说明 `collab` 的正式产物（库代码）并不使用 `gpui_tokio`，只有它的测试代码使用——典型场景是集成测试里构造一个 `App`，调用 `gpui_tokio::init(cx)` 让被测代码在测试环境下具备 Tokio 运行时（`crates/collab/tests/integration/test_server.rs:179` 就有一例）。dev-dependencies 不会传染给下游用户，因此这个依赖不会进入 `collab` 使用者的依赖树。

## 5. 综合实践

**任务：构建 `gpui_tokio` 并产出一份「调用方清单」。**

这个任务把本讲的三个模块串起来：构建它（模块一、二的载体）、读懂它的 Cargo 配置（模块三）、统计谁在用它（三个模块的综合视角）。

1. **实践目标**：独立完成 `gpui_tokio` 的本地构建，并用搜索工具回答「Zed 里到底谁在依赖这座桥」。
2. **操作步骤**：
   1. 克隆仓库并进入仓库根目录：

      ```bash
      git clone https://github.com/zed-industries/zed.git
      cd zed
      ```

   2. 只构建这一个 crate（不必构建整个 Zed，那会久得多）：

      ```bash
      cargo build -p gpui_tokio
      ```

   3. 搜索所有声明依赖它的 crate（即 4.3.4 的命令）：

      ```bash
      grep -rln "gpui_tokio" --include="Cargo.toml" crates/
      ```

   4. 搜索所有调用 `init` / `Tokio::*` 的源码位置：

      ```bash
      grep -rn "gpui_tokio::init\|gpui_tokio::Tokio::" --include="*.rs" crates/ | sort
      ```

   5. 把结果整理成一张两栏清单：左栏「crate 名 + Cargo.toml 行号」，右栏「该 crate 中的代表性调用点（文件:行号 + 用的是 init / spawn / spawn_result / handle 中的哪个）」。
3. **需要观察的现象**：
   - 构建应迅速完成（依赖少），不产生警告或错误。
   - 依赖它的 crate 约 14 个，且主题高度集中在「网络通信、扩展系统、音视频、agent/评测工具」这些天然需要 Tokio 生态库的领域。
   - `gpui_tokio::init` 的调用点里，除了 `zed/src/main.rs:499` 这个应用入口，还有大量出现在 `tests`、`examples`、`headless` 初始化里。
4. **预期结果**：
   - 构建结果**待本地验证**（本讲义编写环境未执行 cargo；预期 `Finished ...` 且无 warning）。
   - 搜索部分的参考答案已在本讲义正文中给出（4.2.4 与 4.3.4 两节），你的清单应与之一致（同一 HEAD `b0e37a6c` 下）。
5. **思考题（选做）**：为什么 `gpui_tokio::init` 的调用点远多于 `Tokio::spawn` 的调用点？提示：想想「每个测试都有独立的全局状态」这句话，答案在第三单元第 3 讲。

## 6. 本讲小结

- `gpui_tokio` 是 Zed 工作区中一个仅约 100 行、单文件的桥接 crate：它让运行在 GPUI 执行器上的 Zed 代码，能够使用必须依赖 Tokio 运行时的生态库（reqwest、WebSocket、LiveKit 等）。
- 桥的起点是 [`init`](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L8-L25)：创建一个 2 工作线程的 Tokio 多线程运行时，连同其 Handle 一起存入 GPUI 全局单例 `GlobalTokio`；`init_from_handle` 则接纳外部运行时的 Handle。
- 桥上的通道是空结构体 `Tokio` 的三个静态方法：`spawn`（返回 `Task<Result<R, JoinError>>`）、`spawn_result`（业务错误折叠为 `Task<anyhow::Result<R>>`）、`handle`（暴露原始 Tokio Handle）。两个 spawn 都遵守「GPUI Task 被 drop 则 Tokio 任务被 abort」的取消契约。
- workspace 根 `Cargo.toml` 用两行挂起这座桥：`members` 里的 `"crates/gpui_tokio"`（第 98 行）与 `[workspace.dependencies]` 里的 `gpui_tokio = { path = "crates/gpui_tokio" }`（第 364 行）；crate 自身对 tokio 只启用 `rt` 与 `rt-multi-thread` 两个 feature。
- 本讲已在 HEAD `b0e37a6c` 上核实：共 14 个 crate 依赖它（zed、client、cloud_api_client、extension_host、livekit_client、call、language_models、agent、agent_ui、agent_servers、collab、eval_cli、edit_prediction_cli、remote_server），代表性调用点包括 `client/src/client.rs:1357`（spawn_result）、`extension_host/src/wasm_host.rs:717`（spawn）、`zed/src/main.rs:516`（handle + enter）。
- 「Cargo.toml 里依赖它」和「运行时能用它」是两回事：必须有人先调用 `gpui_tokio::init(cx)`，Zed 主程序在 `main.rs:499` 完成这一步，各测试则各自显式调用。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：精读 `crates/gpui_tokio/Cargo.toml` 的每一行，重点理解 `[lib] path` 命名规范、workspace 依赖继承机制，并动手用 `cargo tree` 观察依赖树——把本讲 4.3 节的粗读变成逐行精读。
- **前置补课（u1-l3 / u1-l4）**：如果你对 GPUI 的 `Task`、`cx.spawn`/`background_spawn`，或 Tokio 的 `Runtime`/`Handle`/`JoinHandle` 还没有手感，强烈建议先学这两讲再进入第二单元的源码精读——第二单元的每一行代码都同时踩在这两个世界的概念上。
- **源码延伸阅读**：带着本讲建立的地图，去读 `crates/zed/src/main.rs` 第 490–530 行（启动序列），感受 `gpui_tokio::init` 在整个应用装配流程中的位置。
