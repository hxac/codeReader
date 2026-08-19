# gpui_web 是什么：项目定位与目录结构

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `gpui_web` 在 Zed/GPUI 体系中的定位：它是 GPUI UI 框架面向 **WebAssembly（浏览器）** 目标的平台后端（Platform backend）。
2. 列出 crate 的 8 个源码模块（`platform` / `window` / `events` / `dispatcher` / `display` / `keyboard` / `logging` / `http_client`），并说出每个模块各自实现的 gpui trait 或承担的职责。
3. 读懂 `Cargo.toml` 中三处关键配置的含义：wasm 目标专属依赖、`multithreaded` feature、以及 `[lib] path` 指向 `src/gpui_web.rs`。
4. 解释为什么整个 crate 顶部有一行 `#![cfg(target_family = "wasm")]`，以及它和 `Cargo.toml` 中 `target.'cfg(target_family = "wasm")'` 的配合关系。

## 2. 前置知识

本讲是整个手册的第一讲，不假设你读过 GPUI 的任何代码，但需要几个通俗的基础概念：

- **Zed 与 GPUI 的关系**：Zed 是一个高性能代码编辑器，GPUI 是它自研的 UI 框架（一个独立 crate，位于 `crates/gpui`）。Zed 的界面全部用 GPUI 画出来。GPUI 自己不直接调用任何操作系统的 UI API，而是定义了一组「平台抽象接口」（trait），由各个平台后端 crate 去实现。
- **什么是「平台后端」**：可以类比「驱动程序」。GPUI 说「帮我开一个窗口」「帮我读剪贴板」「帮我调度一个任务」，不同平台（macOS / Windows / Linux / 浏览器）对这些请求有不同的实现方式。`gpui_web` 就是「浏览器这个平台」的实现：开窗口 = 创建一个 `<canvas>` 元素，读剪贴板 = 调 `navigator.clipboard`，调度任务 = 用 `setTimeout` / `requestAnimationFrame` / Web Worker。
- **Rust 的条件编译 `cfg`**：`#[cfg(条件)]` 表示「只有条件成立时才编译这段代码」。`target_family = "wasm"` 是 Rust 对 WebAssembly 目标提供的族标识。当用 `cargo build --target wasm32-unknown-unknown` 编译时它为真；编译 Windows/Linux/macOS 原生程序时为假。
- **crate 与 workspace**：Zed 仓库是一个 Cargo workspace，`crates/` 下有几百个 crate。`gpui_web` 是其中之一，它的兄弟 crate 包括 `gpui`（框架核心与平台 trait 定义）、`gpui_wgpu`（基于 wgpu 的 WebGPU/WebGL 渲染与文本系统，被 gpui_web 复用）、`gpui_platform`（对使用者的「门面」，帮你按目标平台选好后端）。
- **WebAssembly（wasm）**：一种可以在浏览器里运行的二进制格式。Rust 程序编译成 wasm 后跑在浏览器的沙箱里，不能直接碰操作系统，只能调用浏览器暴露的 JS API（通过 `web-sys` / `js-sys` 这类绑定库）。

## 3. 本讲源码地图

本讲涉及的关键文件（均位于 `crates/gpui_web/` 下）：

| 文件 | 作用 |
| --- | --- |
| [src/gpui_web.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/gpui_web.rs) | 库入口：只有 24 行，包含 crate 级 `cfg` 门、8 个私有模块声明和一组 `pub use` 再导出，定义了这个 crate 对外的全部 API 面 |
| [Cargo.toml](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/Cargo.toml) | 包定义：feature 开关、wasm 目标专属依赖、`[lib] path` 配置 |
| [src/platform.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs) | 最大的模块（约 785 行）：`WebPlatform` 实现 gpui 的 `Platform` trait，是整个 crate 的「总装车间」，把其余模块组合起来 |
| [examples/hello_web/](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs) | 可运行的浏览器示例工程（下一讲的主角，本讲只借用它确认调用关系） |

作为参照，还需要扫一眼两个「契约方」文件（下一讲会展开）：

- [crates/gpui/src/platform.rs:126](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L126)：`pub trait Platform` 的定义处，也就是 `gpui_web` 要满足的「接口合同」。
- [crates/gpui_platform/src/gpui_platform.rs:31-38](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L31-L38)：`application_with_web_backend`，应用作者实际使用的装配入口，它内部构造了 `gpui_web::WebPlatform`。

## 4. 核心概念与源码讲解

### 4.1 gpui_web 的定位：GPUI 的浏览器平台后端

#### 4.1.1 概念说明

GPUI 把「跨平台」这件事做成了 trait 契约：框架核心（元素树、布局、状态管理、实体系统）只依赖抽象接口，把「窗口、事件、调度、剪贴板、显示器」等能力交给平台 crate 实现。目前仓库里有四个「真平台」后端：

| 平台 crate | 目标 | 
| --- | --- |
| `gpui_macos` | macOS（Metal / AppKit） |
| `gpui_windows` | Windows |
| `gpui_linux` | Linux / FreeBSD |
| `gpui_web` | 浏览器（WebAssembly + WebGPU/WebGL2） |

`gpui_web` 的特殊性在于：它面对的不是操作系统，而是浏览器沙箱。操作系统给你的东西（任意开多个顶层窗口、同步读剪贴板、文件选择对话框），浏览器要么不给、要么只给异步版本。所以这个 crate 的很多代码在做「能力降级」——能用浏览器 API 实现的就实现，不能实现的就返回错误或空操作。crate 顶部文档注释把这个边界说得很直白：

> 浏览器平台使用一个属于文档的 canvas，只支持一个顶层窗口；默认优先 WebGPU，自动回退到 WebGL2；打开第二个顶层窗口或关闭后重开，都会返回 `WebWindowError`。

和 `gpui_web` 配套的还有两个兄弟 crate，分工如下（本讲先建立印象，不必深读）：

- `gpui_wgpu`：提供 `WgpuContext` / `WgpuRenderer`（图形渲染）和 `CosmicTextSystem`（文本排版），是「图形后端」，不关心窗口和事件。
- `gpui_platform`：门面 crate，根据编译目标替你选择后端，让使用者不用写 `#[cfg]`。

#### 4.1.2 核心流程

从「一个 GPUI 应用在浏览器里跑起来」的视角看整体数据流：

```text
应用代码（examples/hello_web/main.rs）
    │  调用 gpui_platform::application_with_web_backend(...)
    ▼
gpui_platform（门面，按 target_family = "wasm" 分发）
    │  构造 Rc<gpui_web::WebPlatform> 并注入 FetchHttpClient
    ▼
gpui_web::WebPlatform（本 crate 的总装车间，platform.rs）
    │  run() 时异步初始化图形（WebGPU 优先，WebGL2 兜底）
    │  open_window() 时创建 WebWindow（canvas + 隐藏 input）
    ▼
浏览器原语（DOM / Canvas / requestAnimationFrame / Fetch / Web Worker）
```

对应到源码组织上，`platform.rs` 是唯一的 `Platform` 实现，它持有并组装其他所有模块的产物（调度器、显示器、文本系统、HTTP 客户端工厂），窗口与事件则下沉到 `window.rs` / `events.rs`。

#### 4.1.3 源码精读

先看「合同」：`Platform` trait 定义在 gpui 核心 crate 中 —— [crates/gpui/src/platform.rs:126](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L126)。这个 trait 的方法可以粗略分成几组：执行器（`background_executor` / `foreground_executor`）、窗口（`open_window` / `active_window`）、剪贴板、光标、外观（`window_appearance`）、菜单与回调注册等。同文件中还有 `PlatformWindow`（[L816](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L816)）和 `PlatformDispatcher`（[L1029](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L1029)）两个 trait，分别由本 crate 的 `window.rs` 和 `dispatcher.rs` 实现。

再看「履约方」：`gpui_web` 里对 `Platform` 的实现从 [src/platform.rs:267](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L267) 开始，一直延伸到文件结尾（L657），是整个 crate 最大的一个 `impl` 块。

`WebPlatform` 结构体本身在 [src/platform.rs:37-53](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L37-L53)。这段代码定义了平台的全量状态，几个关键字段的含义：

- `browser_window: web_sys::Window` —— 浏览器的全局 `window` 对象，是所有 Web API 的入口；
- `dispatcher: Arc<WebDispatcher>` —— 调度器，被前台/后台两个执行器共享；
- `text_system: Arc<dyn PlatformTextSystem>` —— 文本系统，实际来自 `gpui_wgpu::CosmicTextSystem`；
- `wgpu_context` / `prepared_window` / `window_lifecycle` —— 图形初始化与「只允许一个顶层窗口」的状态机（第二单元精读）；
- `_cursor_restore_listeners: Vec<EventListenerHandle>` —— 隐藏光标后用于恢复的 DOM 事件监听（以 `_` 前缀命名表示「只为持有其生命周期」）。

举个「能力降级」的直观例子——浏览器里没有「文件选择对话框」这个同步概念，于是 `prompt_for_paths` 直接通过 oneshot 通道返回一个错误（[src/platform.rs:427-437](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L427-L437)）：

```rust
fn prompt_for_paths(
    &self,
    _options: PathPromptOptions,
) -> oneshot::Receiver<Result<Option<Vec<PathBuf>>>> {
    let (tx, rx) = oneshot::channel();
    tx.send(Err(anyhow::anyhow!(
        "prompt_for_paths is not supported on the web"
    )))
    .ok();
    rx
}
```

而 `run()` 方法则展示了浏览器平台的另一个特点——启动是异步的：图形初始化必须 `await`（探测 WebGPU 适配器是异步操作），所以 `on_finish_launching` 回调要等图形就绪后才被调用（[src/platform.rs:280-304](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L280-L304)）。这段在第二单元 u2-l1 会逐行精读，本讲只需记住结论：**浏览器的图形初始化是异步的，所以「应用启动完成」这个事件也被推迟了**。

#### 4.1.4 代码实践

**实践一：数一数 Platform 合同有多大，找出「降级实现」**

1. **实践目标**：对 `Platform` trait 的能力范围建立手感，并亲眼看到 `gpui_web` 里哪些能力是真实实现、哪些是空实现或错误返回。
2. **操作步骤**（在 Zed 仓库根目录执行，全部是只读命令）：

   ```bash
   # 1. 查看 Platform trait 有哪些方法（在契约方文件里）
   grep -n "    fn " crates/gpui/src/platform.rs | sed -n '1,60p'

   # 2. 查看 gpui_web 实现了哪些方法
   grep -n "    fn " crates/gpui_web/src/platform.rs

   # 3. 找出所有「not supported / not available」的降级点
   grep -n "not supported\|not available" crates/gpui_web/src/platform.rs
   ```

   也可以直接用编辑器打开 `crates/gpui_web/src/platform.rs`，跳到 L267 的 `impl Platform for WebPlatform`，从上往下扫一遍方法名。
3. **需要观察的现象**：第 2 条命令会列出几十个方法；第 3 条命令会命中 `prompt_for_paths`、`prompt_for_new_path`、`app_path`、`path_for_auxiliary_executable`、`write_credentials` 等若干处。
4. **预期结果**：你会看到大约 40+ 个方法实现，其中相当一部分是空函数体（如 `fn hide(&self) {}`）或返回错误的占位。这些就是「浏览器给不了的能力」清单。**（命令输出条数随仓库版本浮动，属正常现象；具体数字待本地验证。）**

#### 4.1.5 小练习与答案

**练习 1**：`gpui_web`、`gpui_wgpu`、`gpui_platform` 三个 crate 各自负责什么？为什么要把渲染拆到 `gpui_wgpu` 而不是直接放进 `gpui_web`？

<details>
<summary>参考答案</summary>

`gpui_web` 实现窗口、事件、调度、剪贴板等「平台抽象」；`gpui_wgpu` 提供 wgpu 图形上下文、渲染器和 Cosmic 文本系统；`gpui_platform` 是门面，按编译目标替使用者装配对应后端。渲染拆出去是因为 wgpu/WebGPU/WebGL 本身是跨平台的图形抽象，和「浏览器窗口/事件模型」是正交关注点——桌面平台同样可以用 wgpu 软件渲染或无头渲染，单独成 crate 才能复用。
</details>

**练习 2**：在 [src/platform.rs:427-437](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L427-L437) 中，`prompt_for_paths` 为什么不直接 `panic!`，而是返回一个携带错误的 `oneshot::Receiver`？

<details>
<summary>参考答案</summary>

因为 `Platform` 是通用接口，调用方（GPUI 框架和应用代码）默认「平台可能不支持」，错误应该沿正常的数据流向上传播，让 UI 层能给用户反馈（比如提示「网页版不支持选择文件」）。直接 panic 会把一次可恢复的功能缺失变成整个应用崩溃，这也符合 Zed 仓库 CLAUDE.md 中「避免 panic、用 `?`/Result 传播错误」的编码规范。
</details>

**练习 3**：打开 [src/platform.rs:280-304](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L280-L304)，`run()` 里为什么用 `wasm_bindgen_futures::spawn_local` 而不是 `std::thread::spawn`？

<details>
<summary>参考答案</summary>

浏览器的主线程不能被阻塞，也没有传统的线程创建 API 给 wasm 主线程用；`spawn_local` 把一个 async 块调度到当前（主）线程的事件循环上，允许内部 `await`（比如异步探测 WebGPU 适配器），这是 wasm 环境下「在主线程上跑异步任务」的标准做法。
</details>

### 4.2 库入口 gpui_web.rs：八个模块与公开 API 面

#### 4.2.1 概念说明

Rust crate 的库入口默认是 `src/lib.rs`，但 Zed 仓库的规范是给入口起一个和 crate 同名的文件（通过 `[lib] path` 指定），所以这里的入口是 `src/gpui_web.rs`。这个文件只有 24 行，却决定了两件大事：

1. **这个 crate 在什么条件下才有内容**（第 1 行的 crate 级 `cfg`）。
2. **这个 crate 对外暴露什么**（`mod` 声明 + `pub use` 再导出）。

注意一个细节：所有 8 个模块都是**私有** `mod`，对外只通过 `pub use` 暴露精选的类型。这是刻意的 API 面控制——使用方不需要也不应该 `use gpui_web::window::WebWindow`，只能拿到再导出的那几个名字。

#### 4.2.2 核心流程

模块间的依赖关系（谁被谁组装）可以画成：

```text
gpui_web.rs（入口：声明 + 再导出）
 ├── platform.rs   ── WebPlatform：总装，impl Platform
 │     ├── 引用 dispatcher.rs  ── WebDispatcher：impl PlatformDispatcher
 │     ├── 引用 display.rs     ── WebDisplay：impl PlatformDisplay
 │     ├── 引用 keyboard.rs    ── WebKeyboardLayout：impl PlatformKeyboardLayout
 │     ├── 引用 http_client.rs ── FetchHttpClient：impl HttpClient（工厂方法 fetch_http_client）
 │     └── 引用 window.rs      ── WebWindow：impl PlatformWindow
 │           └── 引用 events.rs ── DOM 事件 → PlatformInput 翻译层（无 gpui trait impl）
 └── logging.rs   ── ConsoleLogger：impl log::Log（独立，被入口直接再导出）
```

读源码的推荐顺序就沿这张图：先 `platform.rs` 看总装，再按需下钻。

#### 4.2.3 源码精读

入口文件全文只有 24 行（[src/gpui_web.rs:1-24](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/gpui_web.rs#L1-L24)）：

第 1 行是全 crate 最重要的一个「开关」——整份源码只在 wasm 目标下存在：

```rust
#![cfg(target_family = "wasm")]
```

当这行 crate 级属性的条件为假（即编译原生目标）时，整个 crate 会编译成**一个空库**：没有模块、没有类型、没有函数。它和 `Cargo.toml` 中仅作用于 wasm 目标的依赖声明（见 4.3）是一对双保险。

第 3-6 行是 crate 文档注释，一句话概括了浏览器平台的四条硬约束/策略（[src/gpui_web.rs:3-6](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/gpui_web.rs#L3-L6)）：

- 一个 document 拥有的 canvas，只支持**一个顶层窗口**；
- 默认优先 **WebGPU**，自动回退 **WebGL2**；
- 应用可以通过 `WebBackendPreference` 强制指定后端；
- 开第二个顶层窗口、或窗口关闭后重开，返回 `WebWindowError`。

第 8-15 行声明 8 个私有模块（[src/gpui_web.rs:8-15](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/gpui_web.rs#L8-L15)）：

```rust
mod dispatcher;
mod display;
mod events;
mod http_client;
mod keyboard;
mod logging;
mod platform;
mod window;
```

第 17-24 行是对外的全部 API（[src/gpui_web.rs:17-24](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/gpui_web.rs#L17-L24)）：

```rust
pub use dispatcher::WebDispatcher;
pub use display::WebDisplay;
pub use gpui_wgpu::WebBackendPreference;
pub use http_client::{FetchCredentials, FetchHttpClient};
pub use keyboard::WebKeyboardLayout;
pub use logging::init_logging;
pub use platform::{WebPlatform, WebWindowError};
pub use window::WebWindow;
```

注意第三行：`WebBackendPreference` 不是本 crate 定义的，而是从 `gpui_wgpu` **转手再导出**的——使用方因此不必直接依赖 `gpui_wgpu` 就能指定图形后端。

把 8 个模块和它们实现/服务的抽象对应起来，得到下面这张职责表（impl 行号均已核对）：

| 模块 | 主类型 | 实现的抽象 | impl 位置 | 一句话职责 |
| --- | --- | --- | --- | --- |
| `platform.rs` | `WebPlatform` | gpui 的 `Platform` | [src/platform.rs:267](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L267) | 平台总装：执行器、文本系统、剪贴板、光标、外观、图形初始化与单窗口状态机 |
| `window.rs` | `WebWindow` / `WebWindowInner` | gpui 的 `PlatformWindow` | [src/window.rs:601](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/window.rs#L601) | 一个顶层窗口 = 一个 canvas + 隐藏 input；rAF 帧循环、ResizeObserver、绘制 |
| `dispatcher.rs` | `WebDispatcher` | gpui 的 `PlatformDispatcher` | [src/dispatcher.rs:240](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/dispatcher.rs#L240) | 任务调度：主线程邮箱（Atomics 信号）、`setTimeout`/`requestIdleCallback`、wasm 后台线程池 |
| `display.rs` | `WebDisplay` | gpui 的 `PlatformDisplay` | [src/display.rs:65](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/display.rs#L65) | 用 `window.screen` / 视口尺寸回答「显示器有多大」 |
| `keyboard.rs` | `WebKeyboardLayout` | gpui 的 `PlatformKeyboardLayout` | [src/keyboard.rs:5](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/keyboard.rs#L5) | 占位实现：永远报告 "us" 布局（仅 13 行） |
| `events.rs` | `WebEventListeners` / `EventListenerHandle` | （不实现 gpui trait） | — | 把 DOM 事件（pointer/key/composition/paste/wheel）翻译成 gpui 的 `PlatformInput`，供 `window.rs` 派发 |
| `http_client.rs` | `FetchHttpClient` | `http_client` crate 的 `HttpClient` | [src/http_client.rs:69](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/http_client.rs#L69) | 用浏览器 Fetch API 实现 HTTP 客户端（含流式响应与凭证策略） |
| `logging.rs` | `ConsoleLogger` / `init_logging` | `log` crate 的 `Log` | [src/logging.rs:5](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/logging.rs#L5) | 把 Rust 日志按级别转发到浏览器控制台，并安装 panic hook |

其中 `events.rs` 是唯一不直接实现某个 gpui trait 的模块——它实现的是「DOM → GPUI 事件模型」的翻译层，产出 `PlatformInput` 枚举值后交给 `window.rs` 的回调派发。而 `logging.rs` 实现的 `Log` 来自通用的 `log` crate，不是 gpui 的抽象。

#### 4.2.4 代码实践

**实践二（本讲核心实践）：画一张模块职责表并回答 cfg 问题**

1. **实践目标**：亲手把 4.2.3 的职责表重建一遍，并用自己的话解释 `#![cfg(target_family = "wasm")]` 的作用。
2. **操作步骤**：
   1. 在仓库根目录列出 crate 的全部源码文件，确认只有 8 个模块加 1 个入口：

      ```bash
      ls crates/gpui_web/src/
      wc -l crates/gpui_web/src/*.rs
      ```

   2. 用 grep 找到每个模块里「为谁实现了抽象」的证据：

      ```bash
      grep -rn "^impl .* for " crates/gpui_web/src/
      ```

   3. 打开 [src/gpui_web.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/gpui_web.rs)，把 8 个 `mod` 和 8 行 `pub use` 抄到你的笔记里，逐行标注「来自哪个模块 / 实现什么抽象」，形成你自己的职责表。
   4. 打开 [Cargo.toml:19](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/Cargo.toml#L19)，观察依赖表头是 `[target.'cfg(target_family = "wasm")'.dependencies]`，与入口第 1 行对照。
3. **需要观察的现象**：
   - `ls` 应列出 9 个文件（8 模块 + `gpui_web.rs` 入口）；注意入口**不叫** `lib.rs`。
   - `grep "^impl .* for"` 的输出应包含 `impl Platform for WebPlatform`、`impl PlatformWindow for WebWindow`、`impl PlatformDispatcher for WebDispatcher`、`impl PlatformDisplay for WebDisplay`、`impl PlatformKeyboardLayout for WebKeyboardLayout`、`impl HttpClient for FetchHttpClient`、`impl Log for ConsoleLogger`，以及 `WebWindow`/`WebDispatcher`/`WebPlatform` 的固有 `impl` 块。
4. **预期结果**：你得到一张与 4.2.3 相同结构的表，并且发现 `events.rs` 在 `impl ... for ...` 的结果里只有 `impl WebWindowInner`（[src/events.rs:120](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/events.rs#L120)）——它实现的是事件翻译辅助逻辑而非平台 trait。**（grep 输出内容待本地验证。）**

**思考题（实践的一部分）**：为什么整个 crate 顶部要有 `#![cfg(target_family = "wasm")]`，既然 `Cargo.toml` 已经把依赖限制在 wasm 目标上了？

参考要点（答案会在 4.3.3 之后完整给出，建议先自己想 2 分钟）：
- 依赖表只决定「编译 wasm 目标时才引入 web-sys 等依赖」，但代码本身仍会被解析；
- 如果某段代码无条件 `use web_sys::...`，在原生目标上（没有这些依赖）会直接编译失败；
- crate 级 `cfg` 让整个 crate 在原生目标上变成空库，于是其他 crate（如 `gpui_platform`）可以无条件声明 `gpui_web.workspace = true` 依赖而不会连坐编译错误——配合消费侧自己的 `#[cfg(target_family = "wasm")]` 使用点，双方各自防御。

#### 4.2.5 小练习与答案

**练习 1**：入口文件里 `mod platform;` 是私有的，外部还能拿到 `WebPlatform` 吗？

<details>
<summary>参考答案</summary>

能。`mod platform;` 只是模块路径私有（外部无法写 `gpui_web::platform::WebPlatform`），但 `pub use platform::{WebPlatform, WebWindowError};` 把类型重新导出到了 crate 根，外部写 `gpui_web::WebPlatform` 即可。这是 Rust 常见的「隐藏模块结构、收敛 API 面」手法。
</details>

**练习 2**：`pub use gpui_wgpu::WebBackendPreference;` 这行转手再导出带来了什么好处？

<details>
<summary>参考答案</summary>

使用方（比如 hello_web 示例）想指定 `WebGpu` 或 `WebGl` 后端时，只需要依赖 `gpui_web`（或门面 `gpui_platform`），不必再在自己的 `Cargo.toml` 里加 `gpui_wgpu` 依赖。再导出充当了「API 汇聚点」，降低依赖图的暴露面。
</details>

**练习 3**：如果把你自己的某个工具函数放进这个 crate 并对外提供，按入口现在的风格应该怎么做？

<details>
<summary>参考答案</summary>

在某个现有模块（按 Zed 规范优先放进职责匹配的现有文件，而不是新建小文件）里实现 `pub fn`/`pub struct`，然后在 `src/gpui_web.rs` 的再导出区加一行 `pub use 模块::名字;`。对外 API 始终收敛在入口的 `pub use` 列表中，模块保持私有。
</details>

### 4.3 Cargo.toml：wasm 目标依赖、multithreaded feature 与 lib 路径

#### 4.3.1 概念说明

这个 `Cargo.toml` 里有三个值得初学者注意的机制：

- **目标专属依赖（target-specific dependencies）**：`[target.'cfg(...)'.dependencies]` 表里的依赖只在匹配的编译目标上生效。对 `gpui_web` 来说，全部依赖都挂在 `cfg(target_family = "wasm")` 下——编译原生目标时这个 crate 几乎没有依赖。
- **feature 开关**：`[features]` 定义可选功能。这里的 `multithreaded` 默认开启，控制「是否启用基于 `SharedArrayBuffer` 的 wasm 多线程」；关掉它，调度器退化为单线程模式。
- **非默认入口名**：`[lib] path = "src/gpui_web.rs"` 显式指定库入口（这也是 Zed 仓库的统一规范：入口文件与 crate 同名，便于在几百个 crate 里按名找人）。

#### 4.3.2 核心流程

编译期的决策链可以这样理解：

```text
cargo build --target wasm32-unknown-unknown
  ├── target_family = "wasm" 为真
  │     ├── [target.'cfg(wasm)'.dependencies] 生效 → 引入 gpui / web-sys / wasm_thread...
  │     └── #![cfg(target_family = "wasm")] 为真 → crate 内容正常编译
  │           └── feature multithreaded（默认开）→ 启用 wasm_thread 与 scheduler/wasm-threads
  ▼
cargo build --target x86_64-unknown-linux-gnu（原生）
  ├── 目标专属依赖不生效 → 本 crate 无第三方依赖
  └── crate 级 cfg 为假 → 整个 crate 编译为空库（不产生任何符号）
```

#### 4.3.3 源码精读

**feature 定义**（[Cargo.toml:12-14](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/Cargo.toml#L12-L14)）：

```toml
[features]
default = ["multithreaded"]
multithreaded = ["dep:wasm_thread", "scheduler/wasm-threads"]
```

`multithreaded` 是**默认开启**的：它拉起可选依赖 `wasm_thread`，并打开 `scheduler` crate 的 `wasm-threads` feature（调度器必须两端同时开开关，这是 Cargo 里典型的「feature 联动」）。注意：开启 feature 只表示「编译进多线程支持」，运行时还要浏览器满足 `SharedArrayBuffer` + `Atomics.waitAsync` 等条件才会真正用后台线程（探测逻辑在 [src/dispatcher.rs:14-35](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/dispatcher.rs#L14-L35)，第二单元 u2-l7 精读）。

**lib 路径**（[Cargo.toml:16-17](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/Cargo.toml#L16-L17)）：

```toml
[lib]
path = "src/gpui_web.rs"
```

**wasm 目标专属依赖**（[Cargo.toml:19-39](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/Cargo.toml#L19-L39)）的开头：

```toml
[target.'cfg(target_family = "wasm")'.dependencies]
gpui.workspace = true
scheduler.workspace = true
...
wasm_thread = { git = "https://github.com/zed-industries/wasm_thread", rev = "...", version = "0.3", features = ["es_modules"], optional = true }
```

几个依赖值得点名：

- `gpui`：平台 trait 的定义方，是编译期的「合同文本」；
- `gpui_wgpu`：渲染与文本系统的实际提供者（`WebBackendPreference`、`WgpuContext`、`CosmicTextSystem` 都来自它）；
- `wasm-bindgen` / `js-sys` / `web-sys`：Rust ↔ JS 互操作三件套；
- `wasm_thread`：一个 fork 版本，Cargo.toml 里的注释解释了为什么 fork——上游无人维护、其 worker 引导代码还在用 0.2.93 版本起弃用的位置参数调用 wasm-bindgen 初始化函数（[Cargo.toml:35-39](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/Cargo.toml#L35-L39)）。这是读依赖注释能学到工程决策的好例子。

**web-sys 的按需 feature**（[Cargo.toml:40-88](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/Cargo.toml#L40-L88)）：`web-sys` 把整个浏览器 API 面按类型拆成了几百个 feature，必须逐个显式开启。这份列表本身就是一张「本 crate 用到哪些浏览器 API」的清单——`HtmlCanvasElement`（canvas）、`KeyboardEvent` / `CompositionEvent`（键盘与输入法）、`Clipboard` / `ClipboardEvent`（剪贴板）、`ResizeObserver`（尺寸监听）、`ReadableStream`（流式 HTTP 响应）、`Request` / `Response` / `Headers`（Fetch）……对照 4.2.3 的模块职责表，几乎一一呼应。

现在可以完整回答 4.2.4 的思考题了：

> **为什么 crate 顶部要有 `#![cfg(target_family = "wasm")]`？**
>
> `Cargo.toml` 的目标专属依赖表只保证「编译原生目标时不下载/不链接 web-sys 等依赖」，但它不阻止 Cargo 在原生目标上**解析并编译本 crate 的源码**。源码里大量 `use web_sys::...` 在原生目标上会因为依赖不存在而编译失败。加上 crate 级 `cfg` 之后，原生目标上整个 crate 被编译成空库，于是 `gpui_platform` 这类消费方可以放心地在 `Cargo.toml` 里无条件声明 `gpui_web.workspace = true`（见 [crates/gpui_platform/Cargo.toml:37](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/Cargo.toml#L37)），只在真正 `use gpui_web::...` 的代码处套一层 `#[cfg(target_family = "wasm")]`（见 [crates/gpui_platform/src/gpui_platform.rs:27-38](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L27-L38)）。两端各守一道门，任一端的疏忽都不会把编译错误传染给整个 workspace。

#### 4.3.4 代码实践

**实践三：观察 feature 开关对依赖图的影响**

1. **实践目标**：直观看到 `multithreaded` feature 如何改变依赖图，验证「feature 联动」。
2. **操作步骤**（需要本机安装 Rust 工具链；以下命令为只读查询）：

   ```bash
   # 1. 查看本 crate 在默认 feature 下的依赖树（限定 wasm 目标）
   cargo tree -p gpui_web --target wasm32-unknown-unknown -e normal 2>/dev/null | head -40

   # 2. 关掉默认 feature 再看一次，对比 wasm_thread 是否消失
   cargo tree -p gpui_web --target wasm32-unknown-unknown --no-default-features -e normal 2>/dev/null | head -40

   # 3. 查看 cargo 读到的 feature 定义
   cargo metadata --format-version 1 2>/dev/null | python3 -c "
   import json,sys
   meta = json.load(sys.stdin)
   for p in meta['packages']:
       if p['name'] == 'gpui_web':
           print(json.dumps(p['features'], indent=2))
   "
   ```

   如果本机没有 wasm32 目标，命令 1/2 可能报错；此时可以直接对比阅读 Cargo.toml 的 L12-L14 与 L35-L39 完成等价分析。
3. **需要观察的现象**：命令 1 的输出里应出现 `wasm_thread`（以及 git 源）；命令 2 的输出里它应该消失；命令 3 应打印 `{"default": ["multithreaded"], "multithreaded": ["dep:wasm_thread", "scheduler/wasm-threads"]}`。
4. **预期结果**：证明 feature 只是一层「编译期条件依赖」，`scheduler/wasm-threads` 这种写法还能跨 crate 打开对方的 feature。**（命令输出待本地验证；未安装 wasm 目标时以阅读 Cargo.toml 的推演为准。）**

#### 4.3.5 小练习与答案

**练习 1**：如果浏览器不支持 `SharedArrayBuffer`，把 `multithreaded` 设为默认开启会不会导致运行时崩溃？

<details>
<summary>参考答案</summary>

不会崩溃。feature 开启只代表「多线程代码被编译进来」；运行时 [src/dispatcher.rs:14-23](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/dispatcher.rs#L14-L23) 的 `shared_memory_supported()` 会先探测全局对象上有没有 `SharedArrayBuffer`、`Atomics`，并检查 wasm 内存是不是真的共享缓冲，不满足就自动走单线程调度路径。这是「编译期能力 + 运行时探测 + 优雅降级」的组合拳。
</details>

**练习 2**：为什么 Zed 仓库要坚持 `[lib] path = "src/gpui_web.rs"` 而不用默认的 `lib.rs`？

<details>
<summary>参考答案</summary>

这是仓库 CLAUDE.md 明确的规范：入口文件与 crate 同名，在几百个 crate 的大 workspace 里，按名字直接定位（`gpui_web` → `src/gpui_web.rs`）比在 `lib.rs` / `mod.rs` 命名之间猜测更可靠，也让「一个 crate 一个门面文件」的约定保持一致。
</details>

**练习 3**：从 web-sys feature 列表（[Cargo.toml:40-88](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/Cargo.toml#L40-L88)）里挑出三个类型，分别说出它们对应本 crate 的哪个功能模块。

<details>
<summary>参考答案</summary>

示例（任选三个即可）：
- `HtmlCanvasElement` → `window.rs`：顶层窗口就是一个 canvas 元素；
- `ResizeObserver` / `ResizeObserverEntry` → `window.rs`：监听画布尺寸与物理像素变化，驱动 GPUI 重排；
- `CompositionEvent` → `events.rs`：中文/日文等输入法的组合输入事件；
- `Clipboard` / `ClipboardItem` → `platform.rs`：异步剪贴板读取；
- `ReadableStream` / `ReadableStreamDefaultReader` → `http_client.rs`：流式读取 Fetch 响应体。
</details>

## 5. 综合实践

把本讲三块知识（定位、模块地图、构建配置）串成一个输出物——**一页纸的 gpui_web 心智地图**：

1. **画职责表**：不看本讲正文，只借助下面两条命令，独立重建 4.2.3 的「模块 × 实现抽象 × 行号」八行表格：

   ```bash
   ls crates/gpui_web/src/
   grep -rn "^impl .* for " crates/gpui_web/src/
   grep -n "pub use" crates/gpui_web/src/gpui_web.rs
   ```

2. **标注装配关系**：打开 [src/platform.rs:37-53](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L37-L53) 的 `WebPlatform` 结构体，在表格旁边写下它的每个字段来自哪个模块（`dispatcher` ← dispatcher.rs，`active_display` ← display.rs，`wgpu_context` ← gpui_wgpu ……）。
3. **回答 cfg 双保险问题**：用自己的话写 3-5 句话，解释 `#![cfg(target_family = "wasm")]`（[src/gpui_web.rs:1](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/gpui_web.rs#L1)）和 `[target.'cfg(target_family = "wasm")'.dependencies]`（[Cargo.toml:19](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/Cargo.toml#L19)）各挡住了什么、为什么需要两道门。
4. **验证消费链**（为下一讲热身）：打开 [examples/hello_web/main.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs) 和 [crates/gpui_platform/src/gpui_platform.rs:31-38](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L31-L38)，找到示例代码里调用 `application_with_web_backend` 的那一行，确认你的地图两端（应用侧入口 ↔ gpui_web 总装）接上了。

完成标志：不看任何资料，能在白纸上默写出 8 个模块名 + 各自实现的抽象 + `#![cfg(...)]` 的作用。全部命令均为只读，不需要修改任何源码。

## 6. 本讲小结

- `gpui_web` 是 GPUI 的**浏览器（WebAssembly）平台后端**：实现 gpui 定义的 `Platform` / `PlatformWindow` / `PlatformDispatcher` 等抽象，把「开窗口」映射为创建 canvas，把「事件」映射为 DOM 事件监听，把「调度」映射为 `setTimeout`/`requestIdleCallback`/wasm 线程。
- crate 只有 8 个源码模块约 3300 行：`platform`（总装，`impl Platform`，platform.rs:267）、`window`（`impl PlatformWindow`，window.rs:601）、`dispatcher`（`impl PlatformDispatcher`，dispatcher.rs:240）、`display`（`impl PlatformDisplay`，display.rs:65）、`keyboard`（占位布局）、`events`（DOM→`PlatformInput` 翻译层，无 trait impl）、`http_client`（`impl HttpClient`）、`logging`（`impl Log`）。
- 入口 [src/gpui_web.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/gpui_web.rs) 只有 24 行：私有 `mod` + 精选 `pub use` 收敛 API 面，并转手再导出 `gpui_wgpu::WebBackendPreference`。
- `Cargo.toml` 三处关键配置：`[lib] path = "src/gpui_web.rs"`（仓库规范）；全部依赖挂在 `cfg(target_family = "wasm")` 目标下；默认开启的 `multithreaded` feature 联动 `dep:wasm_thread` 与 `scheduler/wasm-threads`。
- `#![cfg(target_family = "wasm")]` 与目标专属依赖是**双保险**：前者让原生目标编译出空库，后者让原生目标不引入 web 依赖；二者配合使 `gpui_platform` 可以无条件依赖本 crate。
- web-sys 的 feature 列表（Cargo.toml:40-88）本身就是一份「本 crate 使用了哪些浏览器 API」的清单，可与模块职责表互相印证。

## 7. 下一步学习建议

下一讲（u1-l2《跑起来：hello_web 示例与 wasm 构建链路》）将把本讲建立的静态地图变成动态体验：用 `trunk serve` 在浏览器里跑起 [examples/hello_web](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs)，观察 `?backend=webgl` 查询参数如何落到本讲提到的 `WebBackendPreference`，以及 COOP/COEP 响应头如何决定多线程是否可用。

在进入下一讲之前，建议先自主通读两个文件（各不超过 100 行）：

1. [examples/hello_web/main.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs) —— 从使用者视角看 `web_init` → `application_with_web_backend` → `open_window` → `Render` 的完整流程；
2. [src/logging.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/logging.rs)（45 行）—— crate 里最小的完整模块，适合作为「一个平台能力从 trait 到实现」的最小样本。

之后按大纲顺序进入 u1-l3（Platform trait 与装配方式），再开始第二单元的逐模块精读。
