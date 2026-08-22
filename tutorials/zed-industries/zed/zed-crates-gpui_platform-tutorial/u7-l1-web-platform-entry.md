# u7-l1 WebPlatform 与 wasm 入口：web_init、后端偏好与 http client

## 1. 本讲目标

前六个单元我们一直在桌面世界（macOS、Windows、Linux）里读 Platform 契约。本讲进入第四个平台——浏览器。学完本讲，你应该能够：

1. 写出一个 wasm 应用的标准启动序列：`web_init()` → `application_with_web_backend(...)` → `run(...)`，并说清每一步各自负责什么。
2. 区分 gpui_platform 在 wasm 上的三个专属入口 `application()`、`application_with_web_backend()`、`single_threaded_web()` 的差别，以及它们与 `current_platform` wasm 分支的关系。
3. 讲清 `WebBackendPreference` 三种取值（Auto/WebGpu/WebGl）在 `initialize_graphics` 里的回退逻辑：Auto 是「先 WebGPU、失败换画布再试 WebGL2」，显式指定则是「只试一种、失败即放弃」。
4. 解释为什么 web 平台要在入口处注入 `FetchHttpClient`，而桌面平台默认只装一个只会报错的 `NullHttpClient`。

## 2. 前置知识

- **wasm 与 `target_family = "wasm"`**：WebAssembly 是一种可运行在浏览器里的字节码。Rust 用 `wasm32-unknown-unknown` 目标编译出 `.wasm`，再由 wasm-bindgen 生成 JS 胶水。注意 GPUI 判断浏览器目标用的是 `target_family = "wasm"` 而不是 `target_os`（回顾 u1-l4）。
- **浏览器主线程**：浏览器里 JS（以及 wasm）默认运行在唯一的主线程上，页面绘制、DOM 操作、`fetch` 都发生在它身上。wasm 里即使开了多线程（SharedArrayBuffer + Worker），**浏览器 API 仍然只能在主线程调用**——这是本讲 FetchHttpClient 设计的根因。
- **wasm-bindgen 与 web-sys**：wasm-bindgen 是 Rust 与 JS 的互操作桥；web-sys 是它生成的一整套浏览器 API 绑定（`window`、`navigator`、`document`……）。`wasm_bindgen_futures::spawn_local` 用来在主线程上启动一个不跨线程的 async 任务。
- **HttpClient trait**：来自 `http_client` crate，gpui 对外的统一网络接口，核心方法是 `send(request) -> BoxFuture<Response>`。`App` 持有一个 `Arc<dyn HttpClient>`，应用代码用 `cx.http_client()` 取用。
- **u2-l1 的结论**：`Platform` 是 gpui 与操作系统之间的契约，`gpui_platform` 门面按编译目标构造 `Rc<dyn Platform>` 注入 `Application::with_platform`。本讲的 `WebPlatform` 就是该契约在浏览器上的实现。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/gpui_platform/src/gpui_platform.rs` | 门面 crate：`web_init`、`application_with_web_backend`、`single_threaded_web` 三个 wasm 专属入口都在这里 |
| `crates/gpui_web/src/gpui_web.rs` | gpui_web crate 的库根：`#![cfg(target_family = "wasm")]` 门控与全部公开类型的再导出 |
| `crates/gpui_web/src/platform.rs` | `WebPlatform` 结构体、构造流水线、`initialize_graphics` 回退逻辑、`Platform` trait 的 web 实现 |
| `crates/gpui_web/src/logging.rs` | `ConsoleLogger`：把 Rust `log` 日志映射到浏览器控制台，并安装 panic hook |
| `crates/gpui_web/src/http_client.rs` | `FetchHttpClient`：把 `HttpClient` trait 落到浏览器 Fetch 上 |
| `crates/gpui_web/src/dispatcher.rs` | `WebDispatcher`（u4-l5 已精读），本讲只借用它的 `dispatch_function_on_main_thread` |
| `crates/gpui_wgpu/src/wgpu_context.rs` | `WebBackendPreference` 枚举的真正定义处（gpui_web 只是再导出） |
| `crates/gpui/src/app.rs` | `Application::with_platform` 装入默认 `NullHttpClient`、`with_http_client` 替换之 |
| `crates/gpui_web/examples/hello_web/main.rs` | 官方示例：`requested_backend()` 读 URL 查询参数选后端偏好，是本讲综合实践的样板 |

## 4. 核心概念与源码讲解

### 4.1 web_init：panic hook 与控制台日志先行

#### 4.1.1 概念说明

wasm 程序跑在浏览器里，一旦出了问题，你看到的是浏览器控制台而不是终端。Rust 默认的 panic 输出和日志宏在 wasm 里**什么都不显示**：panic 会变成一句莫名其妙的 "unreachable executed"，`log::error!` 则根本没有人消费。所以任何 GPUI wasm 应用的第一件事都不是建平台，而是把「错误可见性」接通——这正是 `web_init` 做的两件事：装 panic hook、初始化日志。

#### 4.1.2 核心流程

```text
web_init()
 ├── console_error_panic_hook::set_once()   // panic → console.error + 调用栈
 └── gpui_web::init_logging()
      ├── console_error_panic_hook::set_once()   // 再装一次，set_once 幂等，无害
      ├── log::set_logger(&ConsoleLogger)       // 接管 log 宏的输出目的地
      └── log::set_max_level(debug ? Debug : Info)
```

#### 4.1.3 源码精读

门面侧的 `web_init` 只有两行，且带明确的使用说明注释——「在 wasm_bindgen 入口点、运行应用之前调用」：

[crates/gpui_platform/src/gpui_platform.rs:48-54](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_platform/src/gpui_platform.rs#L48-L54)

```rust
#[cfg(target_family = "wasm")]
pub fn web_init() {
    console_error_panic_hook::set_once();
    gpui_web::init_logging();
}
```

真正的日志实现是一个 `ConsoleLogger`，把 Rust 的日志级别逐条映射到浏览器控制台的四个 API（error/warn/info/log）：

[crates/gpui_web/src/logging.rs:3-32](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/logging.rs#L3-L32)

```rust
match record.level() {
    Level::Error => web_sys::console::error_1(&js_string),
    Level::Warn => web_sys::console::warn_1(&js_string),
    Level::Info => web_sys::console::info_1(&js_string),
    Level::Debug | Level::Trace => web_sys::console::log_1(&js_string),
}
```

`init_logging` 本体在 [crates/gpui_web/src/logging.rs:34-45](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/logging.rs#L34-L45)：先装 panic hook（源码注释解释了原因——没有它，panic 在控制台里只剩一条不透明的 "unreachable executed"，拿不到消息和回溯），再注册 logger，并用 `cfg!(debug_assertions)` 区分构建形态：debug 构建放行到 `Debug` 级别，release 构建只保留 `Info` 及以上。

一个细节：`web_init` 与 `init_logging` **都调用了 `console_error_panic_hook::set_once()`**。`set_once` 顾名思义只生效一次，重复调用是幂等的，所以这处「双保险」是无害的冗余，先装的那次生效。

#### 4.1.4 代码实践

1. **实践目标**：体会「不调 `web_init` 会发生什么」。
2. **操作步骤**：阅读 [crates/gpui_web/examples/hello_web/main.rs:429-431](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/main.rs#L429-L431)，确认 `main()` 的第一行就是 `gpui_platform::web_init();`。然后在自己的 wasm 试验工程里故意注释掉这一行（或只注掉 `init_logging` 那一行），在某个按钮回调里写 `log::error!("test")` 与 `panic!("test")` 各一次。
3. **需要观察的现象**：控制台里 `log::error!` 的输出消失；panic 变成一条 "unreachable executed" 而非带消息的报错。
4. **预期结果**：恢复 `web_init()` 后两者都正常显示。本实践需要 trunk/浏览器环境，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `web_init` 要在 `application_with_web_backend` 之前调用？
**答案**：平台的构造与 `run` 回调里都会打日志（例如 4.3 节的 "Browser graphics initialized successfully" 与回退警告）；不先接通日志与 panic hook，这些早期诊断信息就会丢失。hello_web 的 `main()` 正是这个顺序。

**练习 2**：release 构建下 `log::debug!` 的输出去了哪里？
**答案**：被 `log::set_max_level` 的 `LevelFilter::Info` 过滤掉，`ConsoleLogger::log` 根本不会被调用；debug 构建下才会进 `console.log_1`。

**练习 3**：`web_init` 里的 `set_once` 和 `init_logging` 里的 `set_once` 冲突吗？
**答案**：不冲突。`set_once` 是幂等的一次性初始化，第二次调用是 no-op，先装的那次 hook 一直生效。

### 4.2 三个 wasm 专属入口：application() 的分叉、application_with_web_backend 与 single_threaded_web

#### 4.2.1 概念说明

桌面平台上，`application()` 只是 `Application::with_platform(current_platform(false))` 的包装。到了 wasm，事情变复杂了：平台需要两个桌面没有的自由度——**选哪个图形后端**（WebGPU 还是 WebGL2）与**允不允许多线程**。于是门面在 wasm 上多给了两个专属入口，并让通用的 `application()` 自动分叉到其中之一。三个入口的关系：

| 入口 | 后端偏好 | 多线程 | 注入 FetchHttpClient |
| --- | --- | --- | --- |
| `application()`（wasm 分支） | Auto | 允许 | 是 |
| `application_with_web_backend(p)` | 调用方指定 | 允许 | 是 |
| `single_threaded_web()` | Auto | **禁止** | 是 |
| `current_platform` 的 wasm 分支（供 `headless()` 等使用） | Auto | 允许 | **否** |

#### 4.2.2 核心流程

```text
application()  ──[wasm]──▶ application_with_web_backend(Auto)
                               │
                               ├─ WebPlatform::new_with_backend(true, preference)
                               ├─ http_client = Arc::new(platform.fetch_http_client())
                               └─ Application::with_platform(platform).with_http_client(http_client)

single_threaded_web() ──▶ WebPlatform::new(false)  ← 唯一差别：allow_multi_threading = false
                          其余（http client 注入）与上面完全相同
```

#### 4.2.3 源码精读

先看 `application()` 的分叉——同一个函数名，wasm 与非 wasm 编译出完全不同的身体：

[crates/gpui_platform/src/gpui_platform.rs:13-21](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_platform/src/gpui_platform.rs#L13-L21)

```rust
pub fn application() -> gpui::Application {
    #[cfg(target_family = "wasm")]
    {
        application_with_web_backend(gpui_web::WebBackendPreference::Auto)
    }

    #[cfg(not(target_family = "wasm"))]
    gpui::Application::with_platform(current_platform(false))
}
```

`WebBackendPreference` 本体定义在 gpui_wgpu（`Auto` 是默认值），gpui_web 再导出，门面再再导出一次供下游使用，形成一条三跳的再导出链：

- 定义：[crates/gpui_wgpu/src/wgpu_context.rs:29-34](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_wgpu/src/wgpu_context.rs#L29-L34)（`#[default] Auto, WebGpu, WebGl`）
- gpui_web 再导出：[crates/gpui_web/src/gpui_web.rs:19](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/gpui_web.rs#L19)
- 门面再导出：[crates/gpui_platform/src/gpui_platform.rs:27-28](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_platform/src/gpui_platform.rs#L27-L28)

核心入口 `application_with_web_backend`——注意它同时完成了「建平台」与「装网络」两件事：

[crates/gpui_platform/src/gpui_platform.rs:30-38](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_platform/src/gpui_platform.rs#L30-L38)

```rust
pub fn application_with_web_backend(backend_preference: WebBackendPreference) -> gpui::Application {
    let platform = Rc::new(gpui_web::WebPlatform::new_with_backend(
        true,                    // allow_multi_threading
        backend_preference,
    ));
    let http_client = std::sync::Arc::new(platform.fetch_http_client());
    gpui::Application::with_platform(platform).with_http_client(http_client)
}
```

`single_threaded_web` 与它逐行同型，只把第一个参数换成 `false`，文档注释直白地说明了用途：「与 `application` 不同，本函数返回单线程 web 应用」：

[crates/gpui_platform/src/gpui_platform.rs:40-46](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_platform/src/gpui_platform.rs#L40-L46)

这个布尔最终传给 `WebDispatcher::new(browser_window, allow_threads)`，参与 `supports_threads` 的四合一判定（multithreaded feature、入口布尔、SharedArrayBuffer、Atomics.waitAsync，详见 u4-l5）。

最后是与前两个入口平行、但**不装 http client** 的路径：`current_platform` 的 wasm 分支。回顾 u1-l4 的四段 `#[cfg]`，wasm 段长这样：

[crates/gpui_platform/src/gpui_platform.rs:76-80](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_platform/src/gpui_platform.rs#L76-L80)

```rust
#[cfg(target_family = "wasm")]
{
    let _ = headless;
    Rc::new(gpui_web::WebPlatform::new(true))
}
```

两个易错点：`headless` 参数被显式丢弃（浏览器里没有「无头」概念，`WebPlatform::new(true)` 里的 `true` 是 **allow_multi_threading**，不是 headless）；且这条路径只返回 `Rc<dyn Platform>`，走 `headless()` 或直接调 `current_platform` 的使用者拿不到 FetchHttpClient 注入——这也是为什么 wasm 应用应当优先用 `application()` / `application_with_web_backend()`。

再看注入的另一端。`Application::with_platform` 默认装入的是一个**只会报错的** `NullHttpClient`：

[crates/gpui/src/app.rs:177-183](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/app.rs#L177-L183)

```rust
pub fn with_platform(platform: Rc<dyn Platform>) -> Self {
    Self(App::new_app(platform, Arc::new(()), Arc::new(NullHttpClient)))
}
```

`NullHttpClient` 的 `send` 无条件 `anyhow::bail!("No HttpClient available")`（[crates/gpui/src/app.rs:3008-3022](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/app.rs#L3008-L3022)）。也就是说：**「谁来提供网络」在 gpui 里不是平台的义务，而是入口/应用层的决定**——桌面上的 Zed 应用自己构造真实客户端后替换；web 上的门面入口顺手替换成 FetchHttpClient。替换动作由链式方法 `with_http_client` 完成（[crates/gpui/src/app.rs:216-222](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/app.rs#L216-L222)），应用代码此后经 `App::http_client()` 取用（[crates/gpui/src/app.rs:1616-1619](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/app.rs#L1616-L1619)）。

#### 4.2.4 代码实践

1. **实践目标**：搞清「我在 wasm 上该用哪个入口」。
2. **操作步骤**：通读 [crates/gpui_platform/src/gpui_platform.rs:13-54](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_platform/src/gpui_platform.rs#L13-L54)，为四个调用路径（`application()`、`application_with_web_backend(WebGpu)`、`single_threaded_web()`、`headless()`）各写一行结论，标注：是否注入 FetchHttpClient、是否允许多线程、后端偏好是什么。
3. **需要观察的现象**：这是一次纯源码阅读实践，产出是你笔记里的四行表格。
4. **预期结果**：只有 `headless()` 路径（经 `current_platform`）不含 `with_http_client` 调用；`single_threaded_web()` 是唯一把 `allow_multi_threading` 置为 false 的入口。

#### 4.2.5 小练习与答案

**练习 1**：wasm 上调用 `gpui_platform::application()` 时，`current_platform` 被调用了吗？
**答案**：没有。`application()` 的 wasm 分支直接走 `application_with_web_backend(Auto)`，在函数体内自行构造 `WebPlatform`；`current_platform` 只被 `headless()` 等路径使用。

**练习 2**：为什么 `NullHttpClient` 的设计是「send 即报错」而不是返回空响应？
**答案**：把「没有网络能力」伪装成成功响应会让上层逻辑拿着假数据继续跑，错得更深；`bail!` 把缺失显式暴露给调用方，是快速失败（fail-fast）的姿态。

**练习 3**：`WebBackendPreference` 为什么定义在 gpui_wgpu 而不是 gpui_web？
**答案**：偏好最终由 `WgpuContext::new_web(canvas, preference)` 消费，属于 wgpu 上下文创建参数；gpui_web 与 gpui_platform 都只是再导出方，避免下游多依赖一个 crate。

### 4.3 WebPlatform 构造与 WebBackendPreference 的 Auto/WebGpu/WebGl 回退

#### 4.3.1 概念说明

`WebPlatform` 是 `Platform` 契约的浏览器实现（u2-l1 八大方法分组在它身上的落地：大量方法按「能力探测返回 None」或「显式报错」处理，如 `prompt_for_paths` 直接回 Err、`quit` 只打 warn）。它有两段式构造的关键特点：**构造函数只搭骨架，图形初始化被推迟到 `run()`**。因为创建 wgpu 的 WebGPU/WebGL 上下文是异步的浏览器操作，而 `WebPlatform::new_with_backend` 是同步函数。后端偏好 `backend_preference` 在构造期只是被**存起来**，等 `run` 时才消费——这就是「偏好」一词的含义：你表达意愿，回退逻辑替你兜底。

gpui_web 的库根文档注释一句话概括了整个平台的约束，值得先读：

[crates/gpui_web/src/gpui_web.rs:3-6](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/gpui_web.rs#L3-L6)

> 默认偏好浏览器 WebGPU，带自动 WebGL2 回退；应用可用 `WebBackendPreference` 强制指定。第二个顶层窗口、或关闭后重开窗口，会返回 `WebWindowError`。

#### 4.3.2 核心流程

**构造期（同步）**：

```text
WebPlatform::new_with_backend(allow_multi_threading, preference)
 ├── web_sys::window().expect(...)                    // 必须在浏览器窗口上下文里
 ├── WebDispatcher::new(browser_window, allow_multi_threading)
 ├── BackgroundExecutor::new(dispatcher.clone())      // 前后台执行器共享同一个 dispatcher
 ├── ForegroundExecutor::new(dispatcher.clone())
 ├── CosmicTextSystem::new_without_system_fonts("IBM Plex Sans")
 ├── text_system.add_fonts(BUNDLED_FONTS)             // 8 个内嵌 ttf 字体
 ├── WebDisplay::new(browser_window)
 ├── 注册光标恢复监听器（mousemove/blur/visibilitychange）
 └── 存下 backend_preference、wgpu_context = None、lifecycle = Available
```

**运行期（异步，`Platform::run` 内）**：

```text
initialize_graphics(browser_window, preference)
 ├── preference == Auto:
 │     ① 为 WebGPU 准备画布
 │     ② is_browser_webgpu_supported().await 探测
 │     ③ WgpuContext::new_web(canvas, WebGpu) 尝试
 │     ④ 失败 → 从 DOM 移除坏画布 → 重新准备画布 → 尝试 WebGL2
 │     ⑤ 再失败 → Err（错误信息同时携带两个后端的失败原因）
 ├── preference == WebGpu | WebGl:
 │     ① 为指定后端准备画布 → 尝试一次
 │     ② 失败 → 移除画布 → Err（错误信息说明「只因显式指定而只试了这一种」）
 └── Ok((canvas, context, surface))
成功 → 存入 wgpu_context / prepared_window → 调 on_finish_launching()
失败 → lifecycle = Unavailable → 页面上渲染一条错误消息
```

#### 4.3.3 源码精读

先看结构体，留意哪些字段是 `Rc<RefCell<Option<...>>>`——它们就是「两段式构造」留下的空槽，等着 `run` 来填：

[crates/gpui_web/src/platform.rs:37-53](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/platform.rs#L37-L53)

```rust
pub struct WebPlatform {
    browser_window: web_sys::Window,
    dispatcher: Arc<WebDispatcher>,
    // ...执行器、文本系统、回调槽位...
    backend_preference: WebBackendPreference,
    wgpu_context: Rc<RefCell<Option<WgpuContext>>>,
    prepared_window: Rc<RefCell<Option<PreparedWebWindow>>>,
    window_lifecycle: Rc<Cell<WebWindowLifecycle>>,
    // ...
}
```

构造函数 `new_with_backend`（`new` 是它固定 Auto 偏好的特例，见 [crates/gpui_web/src/platform.rs:119-121](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/platform.rs#L119-L121)）。注意 web 平台**不访问系统字体**，而是用 `include_bytes!` 把 8 个字体文件直接编进 wasm，喂给 cosmic-text 的文本系统（`new_without_system_fonts`）——浏览器沙箱里没有「枚举系统字体」这回事：

[crates/gpui_web/src/platform.rs:123-145](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/platform.rs#L123-L145)

```rust
pub fn new_with_backend(
    allow_multi_threading: bool,
    backend_preference: WebBackendPreference,
) -> Self {
    let browser_window =
        web_sys::window().expect("must be running in a browser window context");
    let dispatcher = Arc::new(WebDispatcher::new(
        browser_window.clone(),
        allow_multi_threading,
    ));
    // ...前后台执行器、文本系统、字体、显示器、光标监听...
```

字体常量表在 [crates/gpui_web/src/platform.rs:26-35](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/platform.rs#L26-L35)（IBM Plex Sans 与 Lilex 各四个字重/斜体）。

然后是本模块的主角——`initialize_graphics` 的 Auto 分支。读懂这段的关键是「**每次尝试都配一张新画布**」：WebGPU 初始化失败的画布可能已被污染，所以先 `canvas.remove()` 把它从 DOM 摘掉，再为 WebGL2 重新 `prepare_canvas`：

[crates/gpui_web/src/platform.rs:199-243](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/platform.rs#L199-L243)

```rust
WebBackendPreference::Auto => {
    let webgpu_canvas = WebWindow::prepare_canvas(browser_window)?;
    let webgpu_result = if wgpu::util::is_browser_webgpu_supported().await {
        WgpuContext::new_web(&webgpu_canvas, WebBackendPreference::WebGpu).await
    } else {
        Err(anyhow::anyhow!("browser WebGPU probe did not return a usable adapter"))
    };
    match webgpu_result {
        Ok(PreparedWebGraphics { context, surface }) => return Ok((webgpu_canvas, context, surface)),
        Err(webgpu_error) => {
            let canvas: &web_sys::Element = webgpu_canvas.as_ref();
            canvas.remove();
            log::warn!("WebGPU initialization failed; falling back to WebGL2: {webgpu_error:#}");
            // 重新准备画布 → WgpuContext::new_web(..., WebGl) → 再失败则汇总两个错误
        }
    }
}
```

显式指定分支则没有任何回退，且错误信息把责任说清楚——「只因应用显式指定，所以只试了这一种」：

[crates/gpui_web/src/platform.rs:244-263](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/platform.rs#L244-L263)

`Platform::run` 把这段异步逻辑接到启动回调上。与桌面平台「进事件循环前同步执行回调」不同（回顾 u2-l2），web 的 `run` 用 `spawn_local` 立即返回，回调被推迟到图形初始化完成后：

[crates/gpui_web/src/platform.rs:280-304](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/platform.rs#L280-L304)

```rust
fn run(&self, on_finish_launching: Box<dyn 'static + FnOnce()>) {
    // ...克隆各 Rc 字段...
    wasm_bindgen_futures::spawn_local(async move {
        match initialize_graphics(&browser_window, backend_preference).await {
            Ok((canvas, context, surface)) => {
                log::info!("Browser graphics initialized successfully with {:?}", context.backend());
                *wgpu_context.borrow_mut() = Some(context);
                *prepared_window.borrow_mut() = Some(PreparedWebWindow { canvas, surface });
                on_finish_launching();
            }
            Err(error) => {
                window_lifecycle.set(WebWindowLifecycle::Unavailable);
                log::error!("Failed to initialize browser graphics: {error:#}");
                show_graphics_unavailable_message(&browser_window, &error);
            }
        }
    });
}
```

这条「回调晚于 run 返回」的时间线直接解释了 `open_window` 的生命周期闸门：在图形就绪前开窗会拿到 `GraphicsInitializationPending`（其文档建议「从 `Platform::run` 的回调里重试可以成功」）；彻底失败后则是 `GraphicsUnavailable`；已开过窗是 `AlreadyOpen`；关了再开是 `ReopeningUnsupported`：

- 窗口生命周期枚举：[crates/gpui_web/src/platform.rs:60-66](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/platform.rs#L60-L66)（`Available / Open / Closed / Unavailable` 四态）
- 错误变体与释义：[crates/gpui_web/src/platform.rs:68-79](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/platform.rs#L68-L79)
- `open_window` 里按生命周期与空槽位判错的闸门：[crates/gpui_web/src/platform.rs:351-370](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/platform.rs#L351-L370)

最后，初始化彻底失败时用户看到的不是黑屏而是文字说明——`show_graphics_unavailable_message` 往 `body` 里追加一个 `<p>` 元素，把错误写出来：[crates/gpui_web/src/platform.rs:762-776](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/platform.rs#L762-L776)。观测出口方面，web 的 `compositor_name()` 固定返回 `"Web"`：[crates/gpui_web/src/platform.rs:495-497](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/platform.rs#L495-L497)。

#### 4.3.4 代码实践

1. **实践目标**：把回退逻辑整理成可查的决策表。
2. **操作步骤**：只读 [crates/gpui_web/src/platform.rs:190-265](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/platform.rs#L190-L265)，按「偏好 × 浏览器能力」填满下面的表（示例答案见练习 2）：

   | 偏好 | 浏览器支持 WebGPU | 结果 |
   | --- | --- | --- |
   | Auto | 是且初始化成功 | ？ |
   | Auto | 探测失败或初始化失败 | ？ |
   | WebGpu | 否 | ？ |
   | WebGl | WebGL2 不可用 | ？ |

3. **需要观察的现象**：填表过程中你会被迫回答「失败后画布去哪了」「错误信息里包含几次失败原因」这类细节问题。
4. **预期结果**：四行都有确定答案，且每行都能附上一个行号引用；无需运行浏览器即可完成（运行验证留给第 5 节综合实践）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `run` 里成功分支要打 `log::info!("Browser graphics initialized successfully with {:?}", ...)` 这条日志？它对回退行为的观测有什么价值？
**答案**：这是判断「最终落在哪个后端」最直接的观测点——Auto 偏好下你不指定后端，实际用上的是 WebGPU 还是 WebGL2 只能看 `context.backend()` 的输出；回退发生时还会先出现那条 "falling back to WebGL2" 的 warn。

**练习 2**：填 4.3.4 的决策表。
**答案**：依次为——① WebGPU 上下文+surface 成功返回；② 换新画布改试 WebGL2，成功则用 WebGL2，仍失败则 Err 且错误信息同时携带 WebGPU 与 WebGL2 两段失败原因（L230-239）；③ Err，信息说明只试了 WebGPU 这一种（L253-260）；④ 同上，只试了 WebGL2 这一种。

**练习 3**：在 `run` 回调执行之前调用 `cx.open_window`，用户会看到什么？
**答案**：`Platform::open_window` 里 `wgpu_context`/`prepared_window` 还是空，返回 `WebWindowError::GraphicsInitializationPending`；该变体的文档注释明确建议「从 `Platform::run` 的回调里重试可以成功」（[platform.rs:73-75](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/platform.rs#L73-L75)）。因为窗口创建发生在 `run(|cx| ...)` 回调内部，正常写法不会踩到。

### 4.4 FetchHttpClient：web 平台为什么自带 HTTP 客户端

#### 4.4.1 概念说明

桌面平台上「发 HTTP 请求」是普通系统调用，谁都能做，所以 gpui 只装 `NullHttpClient` 占位，真实客户端由应用层自选自装。浏览器世界里这件事被三条铁律改写：

1. **网络只能走浏览器 Fetch**：wasm 代码被沙箱包裹，没有裸 socket；所有请求经 `fetch()` 发出，自动受 CORS、Cookie、Service Worker 等浏览器规则约束。
2. **浏览器 API 只能在主线程调用**：即使 wasm 开了多线程，后台线程也碰不得 `fetch`，必须把调用投递回主线程——而这正是 `WebDispatcher` 的本职工作（u4-l5）。
3. **有些请求头改不了**：浏览器禁止 wasm 伪造 `User-Agent`，所以 `FetchHttpClient` 的 `user_agent` 只是「报告值」——给上层代码查的身份标识，并不随请求发送。

于是设计顺理成章：平台既然唯一拥有「回到主线程」的通道（dispatcher），就由平台顺手产出一个 HttpClient，入口函数替你装好——应用代码完全无感，直接 `cx.http_client()`。

#### 4.4.2 核心流程

一次 `FetchHttpClient::send(request)` 的旅程：

```text
调用方（可能在后台线程）
 └─ send(req)
     ├─ ① read_body_to_bytes：把请求体全部读进内存
     │     （注释：流式上传需要半双工 Fetch，浏览器普遍未实现）
     ├─ ② 建 oneshot 通道
     ├─ ③ dispatcher.dispatch_function_on_main_thread(|| spawn_local(async {
     │        fetch(parts, bytes, credentials).await → sender.send(result)
     │     }))
     │     ├─ 已在主线程 → queue_microtask 立即排入微任务队列
     │     └─ 在后台线程 → MainThreadMailbox::post 投回主线程
     └─ ④ receiver.await ← 拿到 http::Response（错误则带上下文向上冒泡）

fetch()（在主线程执行）
 └─ RequestInit：method、credentials、redirect（来自 RedirectPolicy 扩展）、body
     → global_fetch(request) → JsFuture 等 Promise
     → 响应：status + Headers 可迭代对 + body ReadableStream
     → ReadableStreamBody：泵任务把 chunk 灌进 mpsc 通道，drop 即取消
```

#### 4.4.3 源码精读

`fetch` 这个全局函数在 wasm-bindgen 里没有现成绑定，http_client.rs 手写了一段 extern 声明，用 `catch` 捕获同步抛错：

[crates/gpui_web/src/http_client.rs:17-21](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/http_client.rs#L17-L21)

`FetchHttpClient` 本体只抱三个字段：dispatcher（回家之路）、可选的 user_agent、credentials 策略。[crates/gpui_web/src/http_client.rs:23-39](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/http_client.rs#L23-L39)：

```rust
pub struct FetchHttpClient {
    dispatcher: Arc<WebDispatcher>,
    user_agent: Option<http_client::http::header::HeaderValue>,
    credentials: FetchCredentials,
}

pub enum FetchCredentials {
    Omit,        // 永不带凭据
    SameOrigin,  // 同源才带（默认）
    Include,     // 跨源也带（Cookie 等）
}
```

它不从 WebPlatform「注册」而来，而是 WebPlatform 用自己的 dispatcher 现场造一个——`new` 是 crate 私有构造器，只有平台能调用（[crates/gpui_web/src/http_client.rs:42-48](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/http_client.rs#L42-L48)）。平台侧的两个工厂方法：

[crates/gpui_web/src/platform.rs:176-187](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/platform.rs#L176-L187)

```rust
/// Returns an HTTP client that runs browser Fetch operations on this platform's main thread.
pub fn fetch_http_client(&self) -> FetchHttpClient {
    FetchHttpClient::new(self.dispatcher.clone())
}

/// Returns a browser Fetch HTTP client with the given reported user agent.
pub fn fetch_http_client_with_user_agent(&self, user_agent: &str) -> anyhow::Result<FetchHttpClient> { ... }
```

注意第二个方法的文档用词是 **reported** user agent：浏览器不允许设置该请求头，这个值只用于 `HttpClient::user_agent()` 的查询（[crates/gpui_web/src/http_client.rs:69-72](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/http_client.rs#L69-L72)），`fetch` 函数从头到尾没有 set 它——对照桌面平台自定义 UA 的自由，这是浏览器约束的直接体现。

`send` 的骨架是「读体 → 投主线程 → 等结果」三段：

[crates/gpui_web/src/http_client.rs:78-101](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/http_client.rs#L78-L101)

```rust
Box::pin(async move {
    let body_bytes = read_body_to_bytes(body).await?;
    let (sender, receiver) = oneshot::channel();

    dispatcher.dispatch_function_on_main_thread(move || {
        wasm_bindgen_futures::spawn_local(async move {
            let result = fetch(parts, body_bytes, credentials).await;
            if sender.send(result).is_err() {
                log::debug!("fetch response receiver was dropped");
            }
        });
    });

    receiver.await.context("browser fetch task was canceled")?
})
```

投递动作的两种姿态在 WebDispatcher 里（u4-l5 已精读，这里只看本讲用到的这段）：主线程上直接 `queue_microtask`，后台线程则进邮箱排队：

[crates/gpui_web/src/dispatcher.rs:226-237](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/dispatcher.rs#L226-L237)

```rust
pub(crate) fn dispatch_function_on_main_thread(&self, function: impl FnOnce() + Send + 'static) {
    if self.on_main_thread() {
        let callback = Closure::once_into_js(function);
        browser_window().queue_microtask(callback.unchecked_ref());
    } else {
        self.main_thread_mailbox
            .post(Priority::High, MainThreadItem::Function(Box::new(function)));
    }
}
```

主线程上的 `fetch` 本体把 `http::Request` 逐字段翻译成 `web_sys::RequestInit`——方法、凭据策略、重定向策略（从请求扩展里取 `RedirectPolicy`）、字节数组化的请求体（[crates/gpui_web/src/http_client.rs:110-136](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/http_client.rs#L110-L136)）；响应侧把 `web_sys::Response` 的 status、可迭代的 Headers、`ReadableStream` 身体重新拼装回 `http::Response<AsyncBody>`（[crates/gpui_web/src/http_client.rs:158-196](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/http_client.rs#L158-L196)）。响应体不是一次性读尽的，而是起一个 `spawn_local` 泵任务把流里的 chunk 逐个送进 mpsc 通道（容量 8），接收端被 drop 时泵任务感知取消并调用 `reader.cancel()` 停掉浏览器侧的流（[crates/gpui_web/src/http_client.rs:246-284](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/http_client.rs#L246-L284)）——这样上层中途放弃请求时，网络传输也随之停止，而不是白白下载完。

最后闭环回 4.2 的注入链：门面入口里 `let http_client = std::sync::Arc::new(platform.fetch_http_client());` + `.with_http_client(http_client)`（[crates/gpui_platform/src/gpui_platform.rs:36-37](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_platform/src/gpui_platform.rs#L36-L37)）。

#### 4.4.4 代码实践

1. **实践目标**：验证「send 可以从任何线程调用」这一设计承诺。
2. **操作步骤**：纯源码追踪。从 `FetchHttpClient::send`（[crates/gpui_web/src/http_client.rs:78-102](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/http_client.rs#L78-L102)）出发，跟着 `dispatch_function_on_main_thread` 的两个分支各走一遍，回答：若上层在 `background_spawn` 的任务里调 `cx.http_client().send(...)`，请求是在哪个线程、经哪条路径到达 `fetch()` 的？
3. **需要观察的现象**：你会看到两条路径在「主线程判定」处分岔，且函数签名要求闭包 `Send + 'static`（邮箱路径跨线程投递的必要条件）。
4. **预期结果**：能写出「后台线程 → MainThreadMailbox::post(High) → 主线程 waker loop 取出 → spawn_local → fetch」的完整链条，并指出主线程直呼路径走的是 `queue_microtask`。可运行验证（在 hello_web 里加一个按钮发请求）**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么不把 `FetchHttpClient::new` 设为 pub，让应用自己构造？
**答案**：`new` 需要 `Arc<WebDispatcher>`——平台的「回家之路」是内部设施；公开它等于让应用绕过平台直接摸 dispatcher。平台用 `fetch_http_client` 工厂方法控制发放，保证客户端与平台共享同一套主线程调度。

**练习 2**：`with_user_agent("MyApp/1.0")` 之后，发出的请求头里 `User-Agent` 是什么？
**答案**：仍是浏览器自己的 User-Agent。该值只被存起来供 `HttpClient::user_agent()` 查询（代码注释与平台方法的 "reported user agent" 措辞都点明了这一点），`fetch` 从不设置这个头——浏览器禁止脚本改写它。

**练习 3**：请求体为什么要先 `read_body_to_bytes` 整体读入内存？
**答案**：源码注释写明：流式上传需要半双工 Fetch 支持，浏览器普遍还没实现；所以请求体只能先缓冲成字节再一次性交给 `init.set_body`。响应体不受此限，走的是 ReadableStream 泵。

## 5. 综合实践

把本讲四条线索（入口、偏好、回退、观测）串成一个任务：**给 hello_web 增加显式的 `backend=auto` 查询参数，并追踪它如何影响 WebPlatform 的初始化分支**。

**任务描述**：hello_web 已支持 `?backend=webgpu` 与 `?backend=webgl`，缺省等价于 Auto，但 `backend=auto` 这个显式写法目前并未被识别（只是恰好落进 else 分支得到相同结果）。请让它成为一等公民，并用浏览器验证整条链路。

**步骤**：

1. 阅读现状：[crates/gpui_web/examples/hello_web/main.rs:408-427](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/main.rs#L408-L427) 的 `requested_backend()` 先取 `window.location().search()`（形如 `"?backend=webgl"`），剥掉前导 `?` 后按 `&` 切分，逐项精确匹配两个参数值，否则返回 `Auto`。
2. 修改为（**示例代码**，仅示意改法，未提交到仓库）：

   ```rust
   let parameters: Vec<&str> = search.trim_start_matches('?').split('&').collect();
   if parameters.contains(&"backend=webgpu") {
       gpui_platform::WebBackendPreference::WebGpu
   } else if parameters.contains(&"backend=webgl") {
       gpui_platform::WebBackendPreference::WebGl
   } else {
       // backend=auto 与不写参数都显式落入 Auto
       gpui_platform::WebBackendPreference::Auto
   }
   ```

3. 对照入口链路确认参数的去向：`main()` 里 [crates/gpui_web/examples/hello_web/main.rs:429-431](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/examples/hello_web/main.rs#L429-L431) 依次调用 `web_init()` 与 `application_with_web_backend(requested_backend())`——你改的返回值作为 `backend_preference` 传入 `WebPlatform::new_with_backend(true, ...)`，被**原样存进结构体字段**，直到 `run()` 才交给 `initialize_graphics` 消费。
4. 用 trunk 构建（具体构建流程是 u7-l3 的主题），分别用四个 URL 各打开一次：
   - 无参数 → Auto
   - `?backend=auto` → Auto（你的新分支）
   - `?backend=webgpu` → WebGpu
   - `?backend=webgl` → WebGl

**需要观察的现象与预期结果**（**待本地验证**，需要浏览器环境）：

- 打开控制台（`web_init` 已把日志接过去），看 [platform.rs:289-292](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/platform.rs#L289-L292) 那条 `Browser graphics initialized successfully with ...`：Auto 与 auto 应打印同一个后端名；在支持 WebGPU 的浏览器上通常是 WebGpu。
- `?backend=webgl` 在支持 WebGPU 的浏览器上应直接初始化 WebGL2，**不出现** "falling back to WebGL2" 的 warn——因为回退只属于 Auto 分支。
- 在两者都不支持的环境（可借浏览器 flags 关闭 WebGPU 模拟）下：Auto 出现两次失败的汇总错误并显示页面错误消息；显式指定则只报单后端错误、且信息中带「只因显式指定而只试了这一种」。
- 把结论整理成「URL 参数 → preference → initialize_graphics 分支 → console 证据」四列的表格，这就是本讲的交付物。

## 6. 本讲小结

- GPUI wasm 应用的标准启动序列是 `web_init()`（panic hook + 控制台日志）→ `application_with_web_backend(preference)`（建平台 + 注入 FetchHttpClient）→ `run(callback)`（异步初始化图形后执行回调）。
- 门面在 wasm 上有三个入口：`application()` 分叉到 `application_with_web_backend(Auto)`；`single_threaded_web()` 只是把 allow_multi_threading 置 false；`current_platform` 的 wasm 分支丢弃 headless 参数且不注入 http client。
- `WebPlatform` 是两段式构造：构造函数只搭骨架（dispatcher、双执行器、内嵌字体的 cosmic-text、显示器、光标监听），图形初始化推迟到 `run()` 的 `spawn_local` 里完成，启动回调因此晚于 `run` 返回。
- `WebBackendPreference` 的回退逻辑：Auto 先探测并尝试 WebGPU，失败则摘掉坏画布、换新画布再试 WebGL2，再失败才汇总报错；显式 WebGpu/WebGl 只试一种、失败即放弃，错误信息会说明原因。
- web 平台自带 HTTP 客户端是浏览器约束的必然：网络只能走 Fetch、浏览器 API 只能在主线程调用、UA 头不可伪造；`FetchHttpClient` 借平台独有的 dispatcher 把请求投回主线程，入口函数顺手 `with_http_client` 装好，替代默认的 `NullHttpClient`。
- 观测后端选择的最直接证据是 `run` 成功分支的 `context.backend()` 日志与回退时的 warn。

## 7. 下一步学习建议

- 下一讲 **u7-l2「WebWindow 与浏览器事件桥接」**：`run` 回调里 `open_window` 拿到的 `WebWindow` 如何在一张 canvas 上模拟窗口语义（尺寸、设备像素比、焦点），`WebEventListeners` 如何把 keydown/mousedown/wheel 等浏览器事件翻译成 `PlatformInput`。
- 之后 **u7-l3「hello_web 实战」**会把本讲综合实践里搁置的 trunk 构建流程完整走一遍，并精读素数计数的前后台协作。
- 若想温习两条被本讲反复借用的支线：主线程邮箱与 `supports_threads` 四合一判定回到 **u4-l5**；`Platform::run` 在四个平台上的回调时机差异回到 **u2-l2**。
- 源码层面建议按本讲源码地图把 [crates/gpui_web/src/platform.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/platform.rs) 的 `impl Platform for WebPlatform` 通读一遍，对照 u2-l1 的八大方法分组，数一数有多少方法落在「显式报错」与「返回 None」两种姿态上——这是检验你是否真正掌握契约默认实现语义的好练习。
