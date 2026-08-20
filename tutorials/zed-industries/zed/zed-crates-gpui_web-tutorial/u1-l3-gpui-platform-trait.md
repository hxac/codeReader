# 平台抽象契约：Platform trait 与装配方式

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Platform` trait 是什么：它是 gpui 定义的一份「操作系统能力清单」，任何平台后端（macOS、Windows、Linux、浏览器）都要照着这份清单实现。
2. 按能力分组列举 `Platform` trait 的主要方法（执行器、窗口、剪贴板、光标、外观、菜单等）。
3. 手工追踪一条装配链路：`gpui_platform::application_with_web_backend` → `WebPlatform::new_with_backend` → `Application::with_platform` → `with_http_client`，并说出 `WebPlatform` 结构体每个字段的用途。
4. 对照 `impl Platform for WebPlatform`，把近 50 个方法分成四类：真实实现、静默空实现、礼貌性成功、显式错误，并解释浏览器版为什么在某些方法上只能返回 "not supported"。

本讲是整个手册的「契约课」：第 1 讲我们知道了 gpui_web 是谁，第 2 讲我们把它跑了起来，这一讲我们回答——**GPUI 是通过什么接口把浏览器当成一台「操作系统」来用的？**

## 2. 前置知识

本讲需要一点 Rust 基础概念，用通俗语言先解释清楚：

- **trait 与 trait 对象（`dyn Platform`）**：trait 是 Rust 的接口。`dyn Platform` 表示「某个实现了 Platform 的类型，运行时才知道具体是谁」。GPUI 不关心你在 macOS 还是浏览器上，它只拿着一个 `Rc<dyn Platform>` 调方法——具体执行到 `MacPlatform` 还是 `WebPlatform` 的代码，由装配阶段决定。这叫**动态分发**。
- **`Rc` 与 `Arc`**：都是引用计数的共享所有权指针。`Rc` 用于单线程（平台对象只在主线程使用），`Arc` 用于跨线程（调度器要被前台执行器和 HTTP 客户端共享）。浏览器版 GPUI 整体运行在单个主线程上，所以平台内部大量用 `Rc<RefCell<...>>` 做内部可变性。
- **`#[cfg(...)]` 条件编译**：同一段源码在不同编译目标下包含或排除某些代码。`cfg(target_family = "wasm")` 表示「编译到 WebAssembly 目标时才生效」。它既可以用在 `use`/依赖上，也可以用在函数上，还能像属性一样 gate 整个 crate（`#![cfg(...)]`）。
- **门面模式（facade）与装配（composition root）**：程序里总得有一个地方决定「new 哪个实现、注入哪些依赖」。`gpui_platform` 这个门面 crate 就是 GPUI 的装配点：它根据编译目标挑选平台实现，让应用代码不用写一堆 `#[cfg]`。
- **浏览器与桌面操作系统的能力差异**：桌面 App 可以弹文件选择框、访问钥匙串、开多个窗口；网页运行在沙箱里，这些要么没有对应 API，要么被权限门控。理解这个差异，就理解了 gpui_web 一半的设计决策。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [../gpui/src/platform.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L125-L341) | 定义 `Platform` trait 契约（也定义了 `PlatformWindow`、`PlatformDisplay` 等，本讲聚焦 `Platform`） |
| [../gpui_platform/src/gpui_platform.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L1-L97) | 门面 crate：按编译目标装配平台实现，提供 `application` / `application_with_web_backend` / `single_threaded_web` / `web_init` / `current_platform` |
| [../gpui_platform/Cargo.toml](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/Cargo.toml#L23-L38) | 用 `cfg` 目标门控各平台 crate 依赖，wasm 目标下依赖 `gpui_web` |
| [src/platform.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L37-L174) | `WebPlatform` 结构体、构造函数、`impl Platform for WebPlatform`（本讲主战场） |
| [src/gpui_web.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/gpui_web.rs#L1-L24) | gpui_web 库入口：模块声明与精选导出 |
| [../gpui/src/app.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L177-L222) | `Application::with_platform` 与 `with_http_client`，装配链路的终点 |
| [examples/hello_web/main.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs#L429-L443) | 示例入口：实践任务中我们要改造它 |

## 4. 核心概念与源码讲解

### 4.1 Platform trait：一份写给所有操作系统的契约

#### 4.1.1 概念说明

GPUI 想做到「一套 UI 代码，跑在 macOS、Windows、Linux 和浏览器上」。但窗口、键盘事件、剪贴板、光标……这些能力和操作系统（或浏览器）强绑定。GPUI 的解法是经典的**平台抽象层**：

- 在 `gpui` crate 里定义一份 trait——`Platform`，把「一个 GUI 运行环境应该会什么」逐条列成方法。
- 每个平台后端 crate 实现这份 trait：`gpui_macos`、`gpui_windows`、`gpui_linux`、`gpui_web`。
- GPUI 框架核心只依赖 `Rc<dyn Platform>`，对具体平台一无所知。

这带来两个直接后果：

1. **新增平台不改框架**：只要实现 trait，框架代码一行不动。
2. **能力差异必须在 trait 层面消化**：浏览器做不到的事，`WebPlatform` 也得给个交代——要么空实现、要么返回错误。这正是本讲 4.4 节的主题。

除了 `Platform`，这个文件里还有两个同族契约：`PlatformWindow`（单个窗口的行为，由 `gpui_web/src/window.rs` 的 `WebWindow` 实现）和 `PlatformDispatcher`（任务调度，由 `dispatcher.rs` 实现）。它们分别在后续讲义中精读，本讲聚焦 `Platform`。

#### 4.1.2 核心流程

`Platform` trait 有近 50 个方法，按能力可以分成 9 组：

| 能力分组 | 代表方法 | 浏览器上的对应物 |
| --- | --- | --- |
| 执行器与文本 | `background_executor` / `foreground_executor` / `text_system` | `WebDispatcher` 驱动的调度器 + CosmicText 文本系统 |
| 应用生命周期 | `run` / `quit` / `restart` / `activate` / `hide` | `run` 变成异步图形初始化；其余大多无意义 |
| 窗口 | `open_window` / `active_window` / `window_appearance` | 一个 document 一个 canvas，只支持一个顶层窗口 |
| 显示器 | `displays` / `primary_display` | 浏览器视口（viewport）伪装成一块屏幕 |
| URL 与文件 | `open_url` / `prompt_for_paths` / `reveal_path` | `window.open` 可用；文件对话框不可用 |
| 菜单 | `set_menus` / `set_dock_menu` / `on_app_menu_action` | 浏览器没有应用菜单，全部空实现 |
| 光标 | `set_cursor_style` / `hide_cursor_until_mouse_moves` | CSS `cursor` 属性 |
| 剪贴板 | `read_from_clipboard` / `write_to_clipboard` / `read_from_clipboard_async` | `navigator.clipboard`（异步、权限门控） |
| 键盘与凭据 | `keyboard_layout` / `write_credentials` | 键盘布局是占位实现；凭据存储不可用 |

trait 还有一批**带默认实现的方法**，这是 trait 设计里很实用的手法：默认实现通常返回「不支持」（如 `window_stack` 默认 `None`、`compositor_name` 默认空串）或者基于其他方法推导（如 `read_from_clipboard_async` 默认直接包装同步版 `read_from_clipboard`）。这样平台实现方只需要覆盖自己真正支持或真正异于默认的方法。

#### 4.1.3 源码精读

先看 trait 的开头，三个「必答题」——任何平台都必须提供两个执行器和一个文本系统：

[../gpui/src/platform.rs:L125-L137](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L125-L137)

```rust
pub trait Platform: 'static {
    fn background_executor(&self) -> BackgroundExecutor;
    fn foreground_executor(&self) -> ForegroundExecutor;
    fn text_system(&self) -> Arc<dyn PlatformTextSystem>;

    fn run(&self, on_finish_launching: Box<dyn 'static + FnOnce()>);
    fn quit(&self);
    ...
```

这段定义了 trait 的入口三件套：后台执行器（跑重活）、前台执行器（跑 UI 状态更新）、文本系统（字体与排版），以及启动应用的事件循环。

窗口创建的签名值得单独看——它是 GPUI 与平台之间最重要的握手：

[../gpui/src/platform.rs:L162-L166](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L162-L166)

```rust
fn open_window(
    &self,
    handle: AnyWindowHandle,
    options: WindowParams,
) -> anyhow::Result<Box<dyn PlatformWindow>>;
```

返回 `Result` 说明「开窗口可能失败」是一等公民——浏览器版正是靠这个返回值表达「只支持一个顶层窗口」。

再看两个「默认实现」的例子，体会 trait 如何为弱能力平台留后门：

[../gpui/src/platform.rs:L142-L144](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L142-L144) —— `window_stack` 默认返回 `None`，表示「本平台不提供窗口叠放顺序」：

```rust
fn window_stack(&self) -> Option<Vec<AnyWindowHandle>> {
    None
}
```

[../gpui/src/platform.rs:L310-L322](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L310-L322) —— 剪贴板部分：同步读取是必答题，异步读取默认包装同步版：

```rust
fn read_from_clipboard(&self) -> Option<ClipboardItem>;
fn write_to_clipboard(&self, item: ClipboardItem);
...
fn read_from_clipboard_async(&self) -> Task<Result<Option<ClipboardItem>, ClipboardReadError>> {
    Task::ready(Ok(self.read_from_clipboard()))
}
```

注意这里的精妙之处：**在浏览器上关系是反过来的**——同步读剪贴板做不到（API 异步且需要权限），所以 `WebPlatform` 把 `read_from_clipboard_async` 做成真实现（见 4.4.3），而 `read_from_clipboard` 返回 `None`。默认方法机制让两种世界都用同一份 trait 表达。

最后看契约的消费端：`Application` 如何持有平台对象。

[../gpui/src/app.rs:L177-L183](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L177-L183)

```rust
pub fn with_platform(platform: Rc<dyn Platform>) -> Self {
    Self(App::new_app(
        platform,
        Arc::new(()),
        Arc::new(NullHttpClient),
    ))
}
```

`Application` 接收一个 `Rc<dyn Platform>`——从这里开始，GPUI 框架对具体平台的认知就只剩这份 trait 了。另注意它默认塞了一个 **`NullHttpClient`**（所有请求都失败的空 HTTP 客户端），这解释了为什么门面 crate 装配时要紧接着调用 `with_http_client`（见 4.3.3）。

#### 4.1.4 代码实践

**实践目标**：通过通读 trait 定义，建立「能力清单」的直观感受，并找出所有默认实现。

**操作步骤**：

1. 打开 [../gpui/src/platform.rs:L125-L341](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L125-L341)，从 `pub trait Platform` 读到 trait 结束的大括号。
2. 准备一张三列表格：方法名 / 所在能力分组 / 是必答题（无默认体）还是默认实现（有方法体）。
3. 重点标记三个方法：`run`、`open_window`、`read_from_clipboard_async`，写下它们的签名。

**需要观察的现象**：你会注意到 trait 里方法分成视觉上很不一样的两种——一种只有分号结尾的签名，另一种带 `{ ... }` 方法体。

**预期结果**：能整理出 9 个能力分组；能确认 `compositor_name`、`window_stack`、`show_system_notification`、`read_from_clipboard_async` 等是默认实现（有方法体），而 `open_window`、`quit`、`set_cursor_style` 等是必答题。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Platform` trait 定义在 `gpui` crate 里，而不是定义在 `gpui_web` 这样的平台 crate 里？

**参考答案**：契约必须由「中立第三方」持有。`gpui` 是框架核心，它需要在不知道任何具体平台的情况下调用平台能力；如果把 trait 放在某个平台 crate 里，框架就得依赖那个平台 crate，造成依赖倒挂和循环依赖（平台 crate 反过来要实现 trait、必然依赖定义处）。定义在 `gpui` 里，平台 crate 单向依赖 `gpui`，框架核心只认 `dyn Platform`。

**练习 2**：`read_from_clipboard_async` 的默认实现是 `Task::ready(Ok(self.read_from_clipboard()))`。这个默认实现适用于什么平台？为什么浏览器版必须覆盖它？

**参考答案**：适用于剪贴板可以同步读取的桌面平台（macOS/Windows/Linux）——默认实现把同步结果包成立即就绪的 Task。浏览器版必须覆盖，因为 `navigator.clipboard.read()` 是异步 Promise 且受权限（user activation）门控，同步版 `read_from_clipboard` 根本拿不到内容（只能返回 `None`）；所以在 web 上真实现放在异步方法里，同步方法退化为返回 `None`。

**练习 3**：`Platform: 'static` 这个 trait 约束是什么意思？

**参考答案**：要求实现类型不包含非 `'static` 的借用（即不借用短生命周期的数据）。因为平台对象会被装进 `Application` 长期持有，并在各种延迟执行的回调（`Box<dyn 'static + FnOnce()>` 等）中使用，编译器必须保证这些类型可以安全地活过整个程序生命周期。

### 4.2 四个 crate 的分工：gpui、gpui_platform、gpui_web、gpui_wgpu

#### 4.2.1 概念说明

本手册的主角是 `gpui_web`，但它不是孤军作战。理解它在 crate 家族里的位置，才能理解装配为什么长那样：

- **`gpui`**：框架核心。元素树、布局、样式、实体系统，以及平台契约（`Platform` / `PlatformWindow` / `PlatformDispatcher`）。
- **`gpui_web`**：契约的浏览器实现。本手册的主角，把契约逐条翻译成 DOM/Canvas/Web API 调用。
- **`gpui_wgpu`**：渲染与文本基础设施。提供 `WgpuContext`（基于 wgpu 的图形上下文，可走 WebGPU 或 WebGL2）和 `CosmicTextSystem`（文本系统）。它不只服务 web——任何能用 wgpu 的地方都能用。
- **`gpui_platform`**：门面（facade）。按编译目标把对应的平台实现装配进 `Application`，让应用代码一行 `#[cfg]` 都不用写。

一个关键问题：**为什么 `gpui` 不直接依赖 `gpui_web`？** 两个原因：

1. **依赖方向**：`gpui_web` 实现契约，必须依赖 `gpui`；若 `gpui` 再依赖 `gpui_web` 就循环依赖了。
2. **按目标裁剪**：门面 crate 用 Cargo 的 `[target.'cfg(...)'.dependencies]` 只在对应目标下拉入对应平台 crate——编译 macOS 版时 `gpui_web` 根本不参与编译。

#### 4.2.2 核心流程

crate 之间的依赖与装配关系可以用下面这张图表示（箭头表示「依赖」）：

```text
             应用（examples/hello_web）
                     │ 只依赖门面
                     ▼
              ┌──────────────┐
              │ gpui_platform │  ← 装配点：按 cfg 挑实现
              └──────┬───────┘
        ┌────────────┼─────────────┬──────────────┐
        ▼            ▼             ▼              ▼
  [cfg macos]   [cfg windows]  [cfg linux]   [cfg wasm]
  gpui_macos    gpui_windows   gpui_linux    gpui_web ──▶ gpui_wgpu
        └────────────┴─────────────┴──────────────┘
                     ▼ （所有平台 crate 都依赖）
                    gpui   ← 契约与框架核心
```

以 wasm 目标为例，从应用视角的调用链是：

1. 应用调用 `gpui_platform::application_with_web_backend(...)`；
2. 门面内部 `use gpui_web::WebPlatform` 并构造它；
3. `WebPlatform` 构造时又 `use gpui_wgpu::{WgpuContext, CosmicTextSystem, ...}` 拿到渲染与文本设施；
4. 门面把 `Rc<WebPlatform>` 交给 `gpui::Application::with_platform`，框架从此只通过 `dyn Platform` 使用它。

一个有趣的细节是**再导出的接力**：`WebBackendPreference` 类型定义在 `gpui_wgpu`，被 `gpui_web` 再导出，又被 `gpui_platform` 再导出，所以 hello_web 示例里可以直接写 `gpui_platform::WebBackendPreference`（见 [examples/hello_web/main.rs:L408-L427](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs#L408-L427) 中的 `requested_backend` 函数）。应用因此不需要直接依赖 `gpui_wgpu`。

#### 4.2.3 源码精读

先看门面 crate 的自我介绍，第一行文档注释就说明了它的存在意义：

[../gpui_platform/src/gpui_platform.rs:L1-L4](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L1-L4)

```rust
//! Convenience crate that re-exports GPUI's platform traits and the
//! `current_platform` constructor so consumers don't need `#[cfg]` gating.

pub use gpui::Platform;
```

「让消费者不需要写 `#[cfg]`」——这就是门面的全部使命。

再看它是如何按目标拉依赖的：

[../gpui_platform/Cargo.toml:L23-L38](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/Cargo.toml#L23-L38)

```toml
[dependencies]
gpui.workspace = true

[target.'cfg(target_os = "macos")'.dependencies]
gpui_macos.workspace = true

[target.'cfg(any(target_os = "linux", target_os = "freebsd"))'.dependencies]
gpui_linux.workspace = true

[target.'cfg(target_family = "wasm")'.dependencies]
gpui_web.workspace = true
console_error_panic_hook.workspace = true
```

wasm 目标下拉入 `gpui_web` 和 `console_error_panic_hook`（后者用于把 Rust panic 转发到浏览器控制台，见 4.3.3 的 `web_init`）。非 wasm 目标编译时，`gpui_web` 完全不参与。

最后看 gpui_web 自己的入口，注意第 1 行的 crate 级 cfg：

[src/gpui_web.rs:L1-L24](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/gpui_web.rs#L1-L24)

```rust
#![cfg(target_family = "wasm")]

//! GPUI's browser platform uses one document-owned canvas and supports one top-level window. ...

mod dispatcher;
mod display;
mod events;
mod http_client;
...
pub use gpui_wgpu::WebBackendPreference;
pub use http_client::{FetchCredentials, FetchHttpClient};
pub use platform::{WebPlatform, WebWindowError};
```

这行 `#![cfg(target_family = "wasm")]` 是**双保险**：即使某个原生目标意外把 `gpui_web` 拉进依赖图，整个 crate 也会编译成空库，里面所有 `web_sys` 调用都不会引发编译错误。文档注释同时声明了这个平台的两条硬约束：单 document 单 canvas 单窗口、WebGPU 优先 WebGL2 兜底。

#### 4.2.4 代码实践

**实践目标**：亲手验证「依赖按编译目标裁剪」的机制。

**操作步骤**：

1. 阅读 [../gpui_platform/Cargo.toml:L23-L38](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/Cargo.toml#L23-L38)，数一数有几个 `[target.'cfg(...)'.dependencies]` 表。
2. 打开 [src/gpui_web.rs:L1](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/gpui_web.rs#L1)，确认 crate 级 cfg 属性存在。
3. （可选，待本地验证）在你的机器上分别执行 `cargo tree -p gpui_platform --target wasm32-unknown-unknown | head -30` 和 `cargo tree -p gpui_platform --target x86_64-unknown-linux-gnu | head -30`（在 Zed 仓库根目录），对比输出中是否出现 `gpui_web`。

**需要观察的现象**：两个 target 的依赖树不同——wasm 目标下能看到 `gpui_web`（以及它的 `web-sys`、`wasm-bindgen` 等传递依赖），Linux 目标下看不到。

**预期结果**：确认「原生目标编译时 gpui_web 不存在」这一论断。若无法运行 cargo tree，纯阅读 Cargo.toml 也能得出相同结论——cfg 表就是证据。

#### 4.2.5 小练习与答案

**练习 1**：如果去掉 `gpui_web.rs` 第 1 行的 `#![cfg(target_family = "wasm")]`，只保留 Cargo.toml 里 `gpui_platform` 的 cfg 依赖门控，会有问题吗？

**参考答案**：在正常的 Cargo 解析下不会出问题（因为门面只在 wasm 目标依赖它），但这是单保险：任何其他 crate 如果不加 cfg 地在自己的 `[dependencies]` 里写上 `gpui_web`，原生编译就会立刻失败——`web_sys`、`wasm_bindgen` 这些依赖在原生目标大量使用 wasm 专属 API。crate 级 cfg 把「我只能为 wasm 编译」变成了自身的硬约束，不依赖调用者的自觉，所以叫双保险。

**练习 2**：`WebBackendPreference` 定义在 `gpui_wgpu`，为什么 hello_web 示例可以写 `gpui_platform::WebBackendPreference`？

**参考答案**：再导出接力：`gpui_wgpu` 定义 → `gpui_web/src/gpui_web.rs` 第 19 行 `pub use gpui_wgpu::WebBackendPreference;` → `gpui_platform/src/gpui_platform.rs` 第 27-28 行 `#[cfg(target_family = "wasm")] pub use gpui_web::WebBackendPreference;`。层层再导出让应用只依赖门面就能用到深层类型。

**练习 3**：`gpui_web` 为什么不直接依赖 `gpui_platform`，反而要依赖更底层的 `gpui_wgpu`？

**参考答案**：依赖必须无环。`gpui_platform` 已经依赖 `gpui_web`（在 wasm 目标下），`gpui_web` 若再依赖 `gpui_platform` 就成环了。`gpui_wgpu` 位于环外，是被两个 crate 共用的底层设施，`gpui_web` 依赖它天经地义。

### 4.3 装配链路：从 application_with_web_backend 到 WebPlatform::new_with_backend

#### 4.3.1 概念说明

「装配」（assembly / composition root）指程序中**唯一**负责创建具体实现并注入依赖的地方。GPUI 在 web 上的装配点就是 `gpui_platform::application_with_web_backend`。它做三件事：

1. **构造平台**：`WebPlatform::new_with_backend(true, backend_preference)`——`true` 表示允许（探测到条件满足时）使用多线程。
2. **注入 HTTP 客户端**：调用 `platform.fetch_http_client()` 拿到基于浏览器 Fetch 的客户端，包成 `Arc` 塞进 `Application`。不做这一步，应用会一直用 `with_platform` 里默认的 `NullHttpClient`，所有网络请求直接失败。
3. **交给框架**：`Application::with_platform(platform)` 把 `Rc<WebPlatform>` 擦成 `Rc<dyn Platform>`。

门面 crate 一共提供四个入口函数，适用场景不同：

| 函数 | 多线程 | 后端偏好 | 典型用途 |
| --- | --- | --- | --- |
| `application()` | 允许 | `Auto` | 不关心细节的默认入口（wasm 下转发到 `application_with_web_backend`） |
| `application_with_web_backend(pref)` | 允许 | 调用者指定 | 需要强制 WebGPU/WebGL2 的应用（hello_web 用它） |
| `single_threaded_web()` | 禁止 | `Auto` | 部署环境无法提供 COOP/COEP 头（无 SharedArrayBuffer）时的保守选择 |
| `web_init()` | — | — | 前置初始化：panic hook + 日志，须在入口最先调用 |

#### 4.3.2 核心流程

以 hello_web 为例，从 `main()` 到平台对象就绪的完整流程：

```text
main()
 ├─ gpui_platform::web_init()
 │    ├─ console_error_panic_hook::set_once()   // panic 转发到浏览器控制台
 │    └─ gpui_web::init_logging()               // 日志转发到浏览器控制台
 │
 └─ gpui_platform::application_with_web_backend(backend_preference)
      ├─ ① Rc::new(WebPlatform::new_with_backend(true, pref))
      │       ├─ web_sys::window()               // 拿浏览器全局 window，非浏览器环境 panic
      │       ├─ WebDispatcher::new(window, allow_multi_threading)
      │       ├─ BackgroundExecutor / ForegroundExecutor（共享同一个 dispatcher）
      │       ├─ CosmicTextSystem + 内嵌字体（include_bytes）
      │       ├─ WebDisplay（把视口伪装成显示器）
      │       └─ 光标状态 + 光标恢复监听器
      ├─ ② http_client = Arc::new(platform.fetch_http_client())
      │       └─ FetchHttpClient::new(dispatcher.clone())   // 复用同一个调度器
      └─ ③ gpui::Application::with_platform(platform)      // 擦成 Rc<dyn Platform>
              .with_http_client(http_client)                // 替换掉默认的 NullHttpClient
```

`WebPlatform` 结构体（[src/platform.rs:L37-L53](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L37-L53)）共 14 个字段，逐个列出用途：

| 字段 | 类型 | 用途 |
| --- | --- | --- |
| `browser_window` | `web_sys::Window` | 浏览器全局 `window` 对象的句柄，后续一切 DOM 操作的入口 |
| `dispatcher` | `Arc<WebDispatcher>` | 调度器，被两个执行器和 HTTP 客户端共享（跨线程所以是 `Arc`） |
| `background_executor` | `BackgroundExecutor` | 后台执行器（重活），由 dispatcher 构造 |
| `foreground_executor` | `ForegroundExecutor` | 前台执行器（UI 状态更新），由同一个 dispatcher 构造 |
| `text_system` | `Arc<dyn PlatformTextSystem>` | 文本系统（实际是 `gpui_wgpu::CosmicTextSystem`），已加载内嵌字体 |
| `active_window` | `Rc<RefCell<Option<AnyWindowHandle>>>` | 当前活跃窗口句柄；`Rc` 共享给 `WebWindow` 读写（内部可变性） |
| `active_display` | `Rc<dyn PlatformDisplay>` | 唯一的「显示器」（浏览器视口） |
| `callbacks` | `RefCell<WebPlatformCallbacks>` | 各类平台回调（quit、菜单动作、键盘布局变化等 8 个槽位） |
| `backend_preference` | `WebBackendPreference` | 图形后端偏好（Auto/WebGpu/WebGl），`run()` 时使用 |
| `wgpu_context` | `Rc<RefCell<Option<WgpuContext>>>` | 图形上下文；构造时为 `None`，`run()` 异步初始化成功后才填入 |
| `prepared_window` | `Rc<RefCell<Option<PreparedWebWindow>>>` | 预备好的 canvas + wgpu surface，`open_window` 时被 `take()` 走 |
| `window_lifecycle` | `Rc<Cell<WebWindowLifecycle>>` | 窗口生命周期状态机（Available/Open/Closed/Unavailable），与 `WebWindow` 共享 |
| `cursor_visible` | `Rc<Cell<bool>>` | 光标是否可见（配合 hide_cursor_until_mouse_moves） |
| `last_cursor_css` | `Rc<Cell<&'static str>>` | 最近设置的 CSS cursor 值，光标恢复时用 |
| `_cursor_restore_listeners` | `Vec<EventListenerHandle>` | 4 个光标恢复事件监听器的存活凭证（下划线前缀表示仅为持有而存） |

注意模式：**「构造时能定」的字段直接初始化，「依赖异步图形初始化」的字段（`wgpu_context`、`prepared_window`、`window_lifecycle`）全部是 `Rc<RefCell<Option<..>>>` / `Rc<Cell<..>>`**，因为浏览器的 WebGPU 初始化是异步的，构造函数无法完成它们。

#### 4.3.3 源码精读

装配函数本体只有 8 行：

[../gpui_platform/src/gpui_platform.rs:L30-L38](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L30-L38)

```rust
#[cfg(target_family = "wasm")]
pub fn application_with_web_backend(backend_preference: WebBackendPreference) -> gpui::Application {
    let platform = Rc::new(gpui_web::WebPlatform::new_with_backend(
        true,
        backend_preference,
    ));
    let http_client = std::sync::Arc::new(platform.fetch_http_client());
    gpui::Application::with_platform(platform).with_http_client(http_client)
}
```

三步：new 平台 → 取 HTTP 客户端 → 交给 `Application` 并注入客户端。`true` 即 `allow_multi_threading`（运行时仍会探测 SharedArrayBuffer，探测不到自动退单线程，详见第 u2-l7 讲）。

单线程变体只把构造函数换成 `new(false)`：

[../gpui_platform/src/gpui_platform.rs:L40-L46](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L40-L46)

```rust
/// Unlike `application`, this function returns a single-threaded web application.
#[cfg(target_family = "wasm")]
pub fn single_threaded_web() -> gpui::Application {
    let platform = Rc::new(gpui_web::WebPlatform::new(false));
    let http_client = std::sync::Arc::new(platform.fetch_http_client());
    gpui::Application::with_platform(platform).with_http_client(http_client)
}
```

`web_init` 是 wasm 入口的第一句：

[../gpui_platform/src/gpui_platform.rs:L48-L54](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L48-L54)

```rust
/// Initializes panic hooks and logging for the web platform.
/// Call this before running the application in a wasm_bindgen entrypoint.
#[cfg(target_family = "wasm")]
pub fn web_init() {
    console_error_panic_hook::set_once();
    gpui_web::init_logging();
}
```

没有它，wasm 里 panic 和 `log::error!` 会静默吞掉，你在浏览器里什么都看不到——这是第 2 讲强调过的排障前提。

通用的 `application()` 在 wasm 目标下转发到我们关心的函数，非 wasm 目标走 `current_platform`：

[../gpui_platform/src/gpui_platform.rs:L13-L21](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L13-L21)

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

`current_platform` 则是按操作系统逐个 cfg 的总开关，wasm 分支在第 76-80 行：

[../gpui_platform/src/gpui_platform.rs:L56-L81](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L56-L81)

```rust
pub fn current_platform(headless: bool) -> Rc<dyn Platform> {
    #[cfg(target_os = "macos")]
    { Rc::new(gpui_macos::MacPlatform::new(headless)) }
    ...
    #[cfg(target_family = "wasm")]
    {
        let _ = headless;
        Rc::new(gpui_web::WebPlatform::new(true))
    }
}
```

注意 wasm 分支把 `headless` 参数显式丢弃（`let _ = headless;`）——浏览器上没有「无头」概念，永远允许尝试多线程。

现在进入被装配的 `WebPlatform::new_with_backend`（[src/platform.rs:L123-L174](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L123-L174)），按顺序看四个关键步骤。

第一步，拿浏览器全局对象并构造调度器与执行器（[src/platform.rs:L127-L134](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L127-L134)）：

```rust
let browser_window =
    web_sys::window().expect("must be running in a browser window context");
let dispatcher = Arc::new(WebDispatcher::new(
    browser_window.clone(),
    allow_multi_threading,
));
let background_executor = BackgroundExecutor::new(dispatcher.clone());
let foreground_executor = ForegroundExecutor::new(dispatcher.clone());
```

`web_sys::window()` 在非浏览器环境（如 Node）返回 `None`，这里直接 `expect` panic——平台对象本来就不该在浏览器外构造。两个执行器克隆的是同一个 `Arc<WebDispatcher>`，这是「前台后台共用一套调度协议」的关键。

第二步，文本系统与内嵌字体（[src/platform.rs:L26-L35](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L26-L35) 和 [L135-L145](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L135-L145)）：

```rust
static BUNDLED_FONTS: &[&[u8]] = &[
    include_bytes!("../../../assets/fonts/ibm-plex-sans/IBMPlexSans-Regular.ttf"),
    ...
];

let text_system = Arc::new(gpui_wgpu::CosmicTextSystem::new_without_system_fonts(
    "IBM Plex Sans",
));
let fonts = BUNDLED_FONTS.iter().map(|bytes| Cow::Borrowed(*bytes)).collect();
if let Err(error) = text_system.add_fonts(fonts) {
    log::error!("failed to load bundled fonts: {error:#}");
}
```

浏览器沙箱读不到系统字体，所以用 `include_bytes!` 把字体文件在**编译期**嵌进 wasm 二进制——这也解释了第 2 讲里 wasm 产物体积为什么可观。加载失败只打日志不 panic（界面还能画，只是字体回退）。

第三步，显示器与光标状态（[src/platform.rs:L146-L155](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L146-L155)）：

```rust
let active_display: Rc<dyn PlatformDisplay> =
    Rc::new(WebDisplay::new(browser_window.clone()));

let cursor_visible = Rc::new(Cell::new(true));
let last_cursor_css = Rc::new(Cell::new("default"));
let cursor_restore_listeners = cursor_restore_listeners(
    &browser_window,
    cursor_visible.clone(),
    last_cursor_css.clone(),
);
```

`cursor_restore_listeners`（实现在 [src/platform.rs:L726-L760](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L726-L760)）注册了 `mousemove`、`mouseenter`、`blur`、`visibilitychange` 四个监听器：一旦光标被 `hide_cursor_until_mouse_moves` 隐藏，这些事件会把 CSS cursor 恢复成 `last_cursor_css` 里记的旧值。

第四步，HTTP 客户端工厂（[src/platform.rs:L176-L179](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L176-L179)）：

```rust
/// Returns an HTTP client that runs browser Fetch operations on this platform's main thread.
pub fn fetch_http_client(&self) -> FetchHttpClient {
    FetchHttpClient::new(self.dispatcher.clone())
}
```

注释点明设计：Fetch 请求会被派发回主线程执行，客户端复用平台的 `dispatcher`（细节在第 u2-l8 讲）。

装配终点在 gpui 侧（[../gpui/src/app.rs:L177-L183](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L177-L183) 与 [L217-L222](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L217-L222)）：`with_platform` 先放入 `NullHttpClient`，`with_http_client` 再替换为 `FetchHttpClient`：

```rust
pub fn with_http_client(self, http_client: Arc<dyn HttpClient>) -> Self {
    let mut context_lock = self.0.borrow_mut();
    context_lock.http_client = http_client;
    drop(context_lock);
    self
}
```

#### 4.3.4 代码实践（本讲主实践一）

**实践目标**：手工追踪装配链路，验证 4.3.2 的字段表，体会「构造时初始化 vs 异步延迟填充」的差别。

**操作步骤**：

1. 从 [examples/hello_web/main.rs:L429-L443](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs#L429-L443) 的 `main()` 出发，用编辑器「跳转到定义」依次追踪：`gpui_platform::web_init` → `gpui_platform::application_with_web_backend` → `WebPlatform::new_with_backend` → `fetch_http_client` → `Application::with_platform` → `with_http_client`。
2. 对照 [src/platform.rs:L37-L53](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L37-L53) 的结构体定义和 [L157-L173](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L157-L173) 的构造收尾，不看本讲表格，自己写一遍 14 个字段的用途。
3. 在你自己的字段表里标出哪些字段构造时是「空」的（`None` 或初始枚举值），再在源码里搜索它们第一次被真正赋值的位置。

**需要观察的现象**：`wgpu_context`、`prepared_window` 构造时是 `None`，`window_lifecycle` 构造时是 `Available`；前三者的赋值点不在构造函数里，而在 `run()` 的异步回调中（[src/platform.rs:L286-L303](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L286-L303)）。

**预期结果**：你得到的字段表应与 4.3.2 的表格一致；能说出「图形相关字段为何必须是 `RefCell<Option<..>>`」——因为 WebGPU 适配器获取是异步的，且 `open_window` 要用 `take()` 一次性把预备窗口转移走。

**（可选运行实验，待本地验证）** 把 [examples/hello_web/main.rs:L431](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs#L431) 的 `application_with_web_backend(requested_backend())` 换成 `single_threaded_web()`，重新 `trunk serve`。预期应用仍能运行、素数计算仍会完成（后台任务退化为在主线程上分片调度），但计算期间界面响应可能变卡；恢复原样后重新编译验证差异消失。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `dispatcher` 用 `Arc` 而 `active_window` 用 `Rc`？

**参考答案**：`dispatcher`（`Arc<WebDispatcher>`）要被共享给可能运行在 wasm 后台线程的调度路径和 `FetchHttpClient`，必须线程安全，所以用 `Arc`；`active_window` 等字段只在主线程被平台和窗口对象读写，GPUI web 的 UI 状态全部在单主线程上，用更轻量的 `Rc<RefCell<...>>` 即可。

**练习 2**：如果 `application_with_web_backend` 忘了调用 `.with_http_client(http_client)`，应用会发生什么？

**参考答案**：`Application::with_platform` 默认装入 `NullHttpClient`（[../gpui/src/app.rs:L181](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L181)），之后应用发出的所有 HTTP 请求都会失败。hello_web 的 `fetch_http_client` 注入就是为了让网络走浏览器 Fetch。

**练习 3**：`WebPlatform::new_with_backend` 里 `web_sys::window().expect(...)` 会在什么场景下 panic？这个设计合理吗？

**参考答案**：在非浏览器宿主（如 Node.js 或 wasm 测试运行时没有 JS `window` 全局对象）里构造 `WebPlatform` 时 panic。合理——这个平台实现的存在前提就是浏览器环境，与其在后续每一步悄悄失败，不如在装配时快速失败（fail fast），并且 `web_init` 已提前装好 panic hook，错误信息会显示在浏览器控制台。

### 4.4 impl Platform for WebPlatform：真实实现、空实现与错误返回

#### 4.4.1 概念说明

`impl Platform for WebPlatform`（[src/platform.rs:L267-L657](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L267-L657)）共有 49 个方法。面对浏览器「给不了」的能力，代码里有四种不同的交代方式，形成一个光谱：

| 类别 | 策略 | 典型方法 | 适用条件 |
| --- | --- | --- | --- |
| A. 真实实现 | 把请求映射到浏览器 API | `run`、`open_window`、`set_cursor_style`、`read_from_clipboard_async`、`write_to_clipboard`、`window_appearance`、`open_url` | 浏览器有对应能力 |
| B. 静默空实现 | 方法体为空或只打日志 | `set_menus`、`hide`、`restart`、`activate`、`reveal_path` | 桌面专属概念，浏览器无对应物，调用方无须知情 |
| C. 礼貌性成功 | 返回「成功但什么都没做」 | `register_url_scheme` → `Ok(())`、`read_credentials` → `Ok(None)` | 能力无意义但报错会造成噪音 |
| D. 显式错误 | 返回 "not supported" 错误 | `prompt_for_paths`、`prompt_for_new_path`、`app_path`、`path_for_auxiliary_executable`、`write_credentials`、`delete_credentials`、非 Normal 窗口的 `open_window` | 调用方（可能是最终用户）需要知道操作没有发生 |

分类的判断标准是**「沉默会不会造成伤害」**：隐藏应用窗口失败了没人受伤（B）；但用户点了「打开文件」按钮却毫无反应，是必须暴露的问题（D）。

#### 4.4.2 核心流程

给一个 trait 方法做分类的决策流程：

```text
浏览器有对应 API 吗？
 ├─ 有 ──────────────▶ A. 真实实现（翻译成 web_sys 调用）
 └─ 没有
     ├─ 调用方需要知道失败吗？
     │    ├─ 不需要（桌面专属概念）──▶ B. 空实现
     │    └─ 需要（用户可见操作）
     │         ├─ 报错只会制造噪音？──▶ C. 礼貌性成功
     │         └─ 否 ────────────────▶ D. 显式错误
```

以「生命周期」为例走一遍这个流程：`run` 是 A（映射成异步图形初始化）；`quit` 是 B 变体（打一条 warn 日志，因为浏览器页面无法真正「退出」）；`restart` 是 B（空实现）。

还有一个重要的浏览器特性影响 `run` 的形态：**桌面平台的 `run` 通常启动 OS 事件循环并阻塞当前线程，而浏览器的「事件循环」由浏览器自己掌管**——wasm 代码不能接管它。所以 `WebPlatform::run` 用 `spawn_local` 发起异步图形初始化后立刻返回，`on_finish_launching` 回调在初始化完成后才被调用。这就是为什么 hello_web 的 `open_window` 必须写在 `run` 的回调里：过早调用时图形上下文尚未就绪，会得到 `GraphicsInitializationPending` 错误。

#### 4.4.3 源码精读

**A 类：真实实现。** 先看整个平台最核心的 `run`：

[src/platform.rs:L280-L304](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L280-L304)

```rust
fn run(&self, on_finish_launching: Box<dyn 'static + FnOnce()>) {
    let wgpu_context = self.wgpu_context.clone();
    ...
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

这段正是 4.3.2 里那三个「延迟填充」字段的赋值点：初始化成功就填入 `wgpu_context` 和 `prepared_window` 并触发启动回调；失败则把生命周期置为 `Unavailable`，并用 `show_graphics_unavailable_message`（[src/platform.rs:L762-L776](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L762-L776)）往页面 body 里插一个 `<p>` 显示错误——用户至少能看到一句人话。

`open_window` 是第二核心（[src/platform.rs:L332-L397](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L332-L397)），开头两道关卡分别是窗口种类和生命周期：

[src/platform.rs:L337-L360](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L337-L360)

```rust
match &params.kind {
    WindowKind::Normal => {}
    WindowKind::AnchoredPopup(_) => return Err(PopupNotSupportedError.into()),
    WindowKind::PopUp => {
        return Err(WebWindowError::UnsupportedWindowKind("popup windows").into());
    }
    ...
}

match self.window_lifecycle.get() {
    WebWindowLifecycle::Open => return Err(WebWindowError::AlreadyOpen.into()),
    WebWindowLifecycle::Closed => {
        return Err(WebWindowError::ReopeningUnsupported.into());
    }
    WebWindowLifecycle::Unavailable => {
        return Err(WebWindowError::GraphicsUnavailable.into());
    }
    WebWindowLifecycle::Available => {}
}
```

非 `Normal` 窗口直接报错（D 类），`AnchoredPopup` 是特例——它返回专门的 `PopupNotSupportedError`，提示调用方改用 GPUI 的 in-window 弹层方案（`gpui::popup` 模块）。状态机四态的含义（[src/platform.rs:L60-L66](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L60-L66)）：`Available`（还没开过窗口）→ `Open`（窗口存活）→ `Closed`（窗口已关，不允许重开）→ / `Unavailable`（图形初始化失败或建窗失败，永久不可用）。转移图：

```text
Available ──open_window 成功──▶ Open ──窗口关闭──▶ Closed（终态）
    │                            │
    └─图形初始化失败──────────────┴──▶ Unavailable（终态）
```

错误类型本身定义在 [src/platform.rs:L68-L104](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L68-L104)，每个变体的 `Display` 文案都写着补救建议，例如 `GraphicsInitializationPending` 会提示「从 `Platform::run` 的回调里开窗口」：

```rust
/// Graphics initialization has not completed yet; retrying after it
/// finishes (e.g. from the `Platform::run` callback) can succeed.
GraphicsInitializationPending,
```

真实实现的另外三个漂亮样本：`window_appearance` 用媒体查询探测深色模式（[src/platform.rs:L399-L411](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L399-L411)，`match_media("(prefers-color-scheme: dark)")`）；`set_cursor_style` 把 21 种 GPUI 光标枚举逐一映射到 CSS cursor 关键字（[src/platform.rs:L509-L538](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L509-L538)，如 `CursorStyle::IBeam => "text"`、`CursorStyle::PointingHand => "pointer"`）；`write_to_clipboard` 在用户输入事件内同步调用 `navigator.clipboard.write_text` 以满足浏览器的 user-activation 要求（[src/platform.rs:L620-L628](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L620-L628)）。异步剪贴板读取（[L559-L618](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L559-L618)）在第 u3-l1 讲精读，这里只需知道它是「trait 默认方法被真实现覆盖」的教科书案例。

**B 类：空实现。** 桌面专属概念集中出现在这两段：

[src/platform.rs:L310-L318](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L310-L318)

```rust
fn restart(&self, _binary_path: Option<PathBuf>, _arguments: Vec<std::ffi::OsString>) {}

fn activate(&self, _ignoring_other_apps: bool) {}

fn hide(&self) {}

fn hide_other_apps(&self) {}

fn unhide_other_apps(&self) {}
```

[src/platform.rs:L471-L473](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L471-L473)

```rust
fn set_menus(&self, _menus: Vec<Menu>, _keymap: &Keymap) {}

fn set_dock_menu(&self, _menu: Vec<MenuItem>, _keymap: &Keymap) {}
```

「重启进程」「隐藏其他应用」「应用菜单栏」这些概念在浏览器里不存在，空实现即可——`quit`（[L306-L308](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L306-L308)）稍好一点，至少打一条 warn 日志承认自己收到了调用。

**C 类：礼貌性成功。** 两个代表：

[src/platform.rs:L423-L425](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L423-L425)

```rust
fn register_url_scheme(&self, _url: &str) -> Task<Result<()>> {
    Task::ready(Ok(()))
}
```

[src/platform.rs:L636-L638](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L636-L638)

```rust
fn read_credentials(&self, _url: &str) -> Task<Result<Option<(String, Vec<u8>)>>> {
    Task::ready(Ok(None))
}
```

注册 URL scheme 在网页里没有意义，报错只会让上层代码徒增分支，于是假装成功；读取凭据返回 `Ok(None)` 表示「没有存过凭据」——语义上完全合法的回答。

**D 类：显式错误。** 本讲最核心的一组：

[src/platform.rs:L427-L451](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L427-L451)

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

`prompt_for_new_path` 同构（返回 `"prompt_for_new_path is not supported on the web"`）。注意错误是**通过 oneshot 通道异步送达**的——签名决定了调用方本来就要 await 结果，在通道里塞一个立即就绪的 `Err` 既满足签名又不丢信息。

[src/platform.rs:L499-L507](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L499-L507)

```rust
fn app_path(&self) -> Result<PathBuf> {
    Err(anyhow::anyhow!("app_path is not available on the web"))
}

fn path_for_auxiliary_executable(&self, _name: &str) -> Result<PathBuf> {
    Err(anyhow::anyhow!(
        "path_for_auxiliary_executable is not available on the web"
    ))
}
```

凭据写入与删除同样报错（[src/platform.rs:L630-L644](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L630-L644)，`"credential storage is not available on the web"`）。

#### 4.4.4 代码实践（本讲主实践二）

**实践目标**：理解「为什么某些能力只能报错」，并用一个可运行实验验证单窗口约束。

**第一部分（源码阅读）**：从 `Platform` trait 中挑 3 个 D 类方法（建议 `prompt_for_paths`、`app_path`、`write_credentials`），对每个方法回答三个问题：

1. 这个方法在桌面平台上做什么？
2. 浏览器环境缺了什么使它做不到？
3. 如果改成静默空实现，用户会遭遇什么？

参考分析（以此格式写你的另外两个）：

- `prompt_for_paths`：桌面弹出文件/目录选择对话框并返回文件系统路径；浏览器沙箱里网页没有任意路径的同步枚举能力（File System Access API 有类似物但有权限弹窗、返回句柄而非裸路径、且不能同步返回）；若静默返回 `None`，用户点「打开文件」后界面毫无反应，且上层无法区分「用户取消了」和「根本不支持」。

**第二部分（可运行实验，待本地验证）**：验证「只支持一个顶层窗口」。修改 hello_web 的 `main()`（以下为示例代码，基于 [examples/hello_web/main.rs:L429-L443](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/examples/hello_web/main.rs#L429-L443) 修改）：

```rust
// 示例代码：在 run 回调中尝试打开第二个窗口
fn main() {
    gpui_platform::web_init();
    gpui_platform::application_with_web_backend(requested_backend()).run(|cx: &mut App| {
        let bounds = Bounds::centered(None, size(px(640.), px(560.)), cx);
        let options = WindowOptions {
            window_bounds: Some(WindowBounds::Windowed(bounds)),
            ..Default::default()
        };
        cx.open_window(options.clone(), |_, cx| cx.new(HelloWeb::new))
            .expect("first window should open");

        let second = cx.open_window(options, |_, cx| cx.new(HelloWeb::new));
        match second {
            Ok(_) => web_sys::console::log_1(&"unexpected: second window opened".into()),
            Err(error) => web_sys::console::log_1(
                &format!("second window failed as expected: {error:#}").into(),
            ),
        }
    });
}
```

**操作步骤**：

1. 保存上述修改，在 `examples/hello_web` 目录运行 `trunk serve`，打开浏览器。
2. 打开 DevTools 控制台，观察第二条 `console::log_1` 输出的错误文案。
3. 对照 [src/platform.rs:L351-L352](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L351-L352) 与 [L84-L86](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L84-L86)，确认控制台文案正是 `WebWindowError::AlreadyOpen` 的 `Display` 输出。
4. 实验完成后还原 `main.rs`。

**需要观察的现象**：界面正常显示第一个窗口（素数计算器可用）；控制台出现 `"second window failed as expected: GPUI web supports only one top-level window; a window is already open"` 字样。

**预期结果**：与 [src/platform.rs:L351-L352](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L351-L352) 的状态机分支一一对应：第一次 `open_window` 把生命周期置为 `Open`（[L386](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L386)），第二次命中 `AlreadyOpen` 分支返回错误。

#### 4.4.5 小练习与答案

**练习 1**：`quit()` 的实现是打一条 warn 日志，而 `prompt_for_paths()` 返回错误。为什么同为「做不到」，处理方式不同？

**参考答案**：`quit` 的调用方是框架自身（如窗口全部关闭后的退出流程），浏览器页面本来就无法被脚本「退出」，报错只会制造噪音，打日志留痕即可（B 类）；`prompt_for_paths` 通常由「打开文件」这类用户可见操作触发，静默失败会让用户困惑操作为何无效，必须显式报错让 UI 层能提示（D 类）。分界线是「沉默是否伤害用户」。

**练习 2**：`read_credentials` 返回 `Task::ready(Ok(None))` 而不是 `Err`，`write_credentials` 却返回 `Err`。为什么不对称？

**参考答案**：`Ok(None)` 的语义是「这里没有存储的凭据」，是一个完全合法且无害的回答——上层会当作「未登录/未保存」处理，逻辑照常运转；而写入凭据如果假装成功，用户以为密码已保存实际却丢了，属于数据丢失级别的伤害，必须报错。读取操作的无害默认值与写入操作的失败之间不对称是合理设计。

**练习 3**：在 `run` 回调之外（例如某个按钮点击回调里首次）调用 `cx.open_window` 会发生什么？阅读 `WebWindowError` 的文档注释给出答案。

**参考答案**：若图形初始化尚未完成，`open_window` 会在取 `wgpu_context`/`prepared_window` 时得到 `None`，返回 `GraphicsInitializationPending` 错误（[src/platform.rs:L362-L370](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L362-L370)）。该变体的文档（[L73-L75](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L73-L75)）明确说明：等初始化完成后再试可以成功，正确做法是把开窗逻辑放进 `Platform::run` 的回调。若初始化已经失败，则会得到 `GraphicsUnavailable`（重试也无济于事）。

## 5. 综合实践

把本讲知识串成一张「装配护照」。完成以下三件事：

1. **画完整装配图**：从 `hello_web` 的 `main()` 开始，画到 `WebPlatform` 的 14 个字段为止的调用与数据流图，标注每一跳所在的文件与行号（提示：`web_init` → `application_with_web_backend` → `new_with_backend` 四个步骤 → `fetch_http_client` → `with_platform`/`with_http_client`）。在图上用三种颜色区分：构造时同步初始化的字段、`run()` 中异步填充的字段、与其他对象（`WebWindow`）共享的字段。
2. **做方法四分类卡片**：把 `impl Platform for WebPlatform` 的 49 个方法按 A（真实实现）/ B（空实现）/ C（礼貌性成功）/ D（显式错误）归类成表。完成后与 4.4.1 的标准对照，检验你的分类标准是否与「沉默是否伤害用户」一致。
3. **运行验证**：完成 4.4.4 的双窗口实验，截下控制台里的 `AlreadyOpen` 错误文案，贴在你的装配图旁边——它就是「契约 + 状态机 + 错误返回」三者在运行时的合影。

## 6. 本讲小结

- `Platform` 是 gpui 定义的「操作系统能力清单」trait；框架核心只持有 `Rc<dyn Platform>`，对具体平台一无所知，新增平台不需要改动框架。
- crate 分工：`gpui` 持契约，`gpui_web` 做浏览器实现，`gpui_wgpu` 提供渲染与文本设施，`gpui_platform` 是按 `cfg` 目标挑选实现的门面（装配点）；`gpui_web` 顶部的 `#![cfg(target_family = "wasm")]` 与门面的 cfg 依赖构成双保险。
- 装配链路 `application_with_web_backend`：构造 `WebPlatform`（调度器、双执行器、内嵌字体的文本系统、显示器、光标状态）→ 用 `fetch_http_client()` 换掉默认的 `NullHttpClient` → 交给 `Application::with_platform`。
- `WebPlatform` 的图形相关字段（`wgpu_context`、`prepared_window`、`window_lifecycle`）在构造时是空的，因为浏览器图形初始化是异步的，它们在 `Platform::run` 的异步回调里才被填充——这也是开窗必须写在 `run` 回调内的原因。
- `impl Platform for WebPlatform` 的 49 个方法分为四类：真实实现、静默空实现、礼貌性成功、显式错误；分类标准是「沉默是否伤害用户」，浏览器给不了但用户可见的能力（文件对话框、路径、凭据写入）一律显式报错。
- 窗口生命周期是四态状态机（`Available`/`Open`/`Closed`/`Unavailable`），配合 `WebWindowError` 的五个变体，把「单窗口、不可重开、图形可用性」这三条浏览器硬约束表达为可恢复或不可恢复的错误。

## 7. 下一步学习建议

下一讲（u2-l2 之前的第一站）是 **u2-l1「WebPlatform 初始化与图形后端选择」**：本讲我们看到 `run()` 只是发起 `initialize_graphics` 的异步任务，下一讲将深入这个函数，拆解 `Auto` 策略下「先 WebGPU、失败移除 canvas、再换 WebGL2」的完整降级链，以及 `WebWindowLifecycle` 状态机的全部转移条件。

继续阅读建议：

- 通读 [src/platform.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_web/src/platform.rs#L267-L657) 的完整 `impl Platform` 块，重点补上本讲略过的 `read_from_clipboard_async`（为 u3-l1 做准备）。
- 对照阅读 `gpui` 的 [../gpui/src/platform.rs:L125-L341](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L125-L341)，找出 `PlatformWindow` 和 `PlatformDispatcher` 两个 trait 的定义位置，为 u2-l2（窗口）和 u2-l7（调度器）建立契约视角。
