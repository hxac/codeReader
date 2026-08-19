# 从零跑起第一个应用：application() 与一个跨平台窗口

## 1. 本讲目标

上一讲（u1-l1）我们知道了 `gpui_platform` 是一个"门面 crate"：它把 gpui 的平台 trait 和 `current_platform` 构造器再导出，让使用者免写 `#[cfg]` 条件编译。本讲我们把这条结论变成**能跑起来的代码**。学完本讲，你应该能够：

1. 用 `gpui_platform::application()` 写出一个最小可运行的 GPUI 窗口程序，并知道它为什么在 macOS / Windows / Linux / 浏览器上都能编译运行。
2. 说清 `Application::with_platform` 做了什么：它如何把一个 `Rc<dyn Platform>` 实例"注入"应用，并从平台层抽取执行器、文本系统、键盘布局等系统能力。
3. 画出从 `Application::run` 的启动回调，到 `App::open_window`，再到平台层 `Platform::open_window` 的完整调用链。
4. 区分 `application()` 与 `headless()` 两个入口，说明 `headless = true` 时应用的行为差异（无窗口、事件循环照常运转）。

## 2. 前置知识

本讲需要一点 Rust 基础概念，我们先用大白话过一遍：

- **trait 与 trait 对象（`dyn Trait`）**：trait 类似其他语言的"接口"。`dyn Platform` 表示"某个实现了 Platform 接口的类型"，编译期不关心具体是谁，运行期动态分发。GPUI 用 `Rc<dyn Platform>`（一个引用计数的智能指针，指向"某个平台实现"）来装当前操作系统的平台对象。
- **回调（callback）**：把一个闭包（匿名函数）作为参数交给框架，框架在合适的时机替你调用它。本讲会见到 `run(|cx| ...)` 这种"启动完成后回调"。
- **事件循环（event loop）**：GUI 程序的心脏。它是一个不停转的循环：取事件（鼠标、键盘、重绘请求）→ 分发处理 → 再取下一个。`run()` 通常是**阻塞**的——程序会一直停在事件循环里，直到退出。
- **`Entity<T>` 与 `Render`**（只需大概了解，后续单元细讲）：GPUI 里界面状态放在 `Entity<T>` 中；给 `T` 实现了 `Render` trait（提供一个 `render` 方法返回界面描述），GPUI 就知道怎么把它画到窗口里。
- **条件编译 `#[cfg]`**：Rust 编译期开关。`#[cfg(target_os = "macos")]` 标注的代码只在编译目标是 macOS 时才存在，其他平台上这段代码根本不会进入编译产物。这是上一讲的核心，也是本讲 `application()` 的实现方式。

承接 u1-l1 的结论：依赖方向是「平台 crate（gpui_macos / gpui_windows / gpui_linux / gpui_web）→ gpui」，gpui 主 crate 定义契约（`Platform` 等 trait），`gpui_platform` 负责按编译目标挑一个实现并再导出。本讲就看这个"挑选"发生在哪、以及挑完之后应用如何启动。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/gpui_platform/src/gpui_platform.rs` | 门面 crate 全部内容：`application()`、`headless()`、`current_platform()` 等入口函数，不足百行 |
| `crates/gpui_platform/Cargo.toml` | 声明按目标操作系统分组的依赖（macOS 才依赖 gpui_macos 等），这是条件编译的"另一半" |
| `crates/gpui/examples/window.rs` | 官方窗口示例：演示 `application().run(...)`、`cx.open_window(...)` 与各种 `WindowOptions` |
| `crates/gpui/examples/hello_world.rs` | 官方最小示例：比 window.rs 更简短，适合作为第一个运行的程序 |
| `crates/gpui/src/app.rs` | `Application`（应用外壳）与 `App`（应用上下文）的定义：`with_platform`、`run`、`open_window` 都在这里 |
| `crates/gpui/src/window.rs` | `Window::new`：把 `WindowOptions` 翻译成 `WindowParams` 并调用平台层 `open_window` 的桥接点 |
| `crates/gpui_linux/src/linux.rs` | Linux 侧 `current_platform(headless)`：headless 分支与 Wayland/X11 探测分发 |
| `crates/gpui_linux/src/linux/headless/client.rs` | `HeadlessClient`：无显示环境下的平台实现，本讲看它的 `run` 与 `open_window` |
| `crates/gpui/src/platform.rs` | 平台契约所在地；本讲只用到其中的 `guess_compositor()`（环境变量探测） |

> 提示：gpui 相关 crate 均**不发布到 crates.io**（zed 仓库工作区声明了 `publish = false`，见根目录 `Cargo.toml` 第 268 行），所以本讲所有"独立程序"都要在 zed 仓库工作区内部构建，或使用 git 依赖。

## 4. 核心概念与源码讲解

### 4.1 `application()`：一行代码背后的平台选择

#### 4.1.1 概念说明

写一个跨平台 GUI 程序，最烦的事情之一是：创建窗口、读剪贴板、弹文件对话框……这些能力在每个操作系统上 API 完全不同。GPUI 的解法是把"所有系统能力"抽象成一个 `Platform` trait，再为每个操作系统写一个实现（上一讲的架构图）。

但对使用者来说还有个次生烦恼：**"当前操作系统该用哪个实现"这个判断写在哪？** 如果每个下游 crate 都自己写一遍 `#[cfg(target_os = ...)]`，条件编译会蔓延得到处都是。`gpui_platform::application()` 就是把这个判断收拢到一处——你只管调用它，它在编译期就已经"知道"自己在哪个平台上，返回一个配置好的 `Application`。

#### 4.1.2 核心流程

```text
gpui_platform::application()
        │
        ├─ 编译目标是 wasm？
        │     └─ 是 → application_with_web_backend(WebBackendPreference::Auto)   （第 7 单元细讲）
        └─ 否（桌面平台）
              └─ gpui::Application::with_platform( current_platform(false) )
                                    │
                                    └─ current_platform(false) 按编译目标四选一：
                                         macOS   → Rc::new(gpui_macos::MacPlatform::new(false))
                                         Windows → Rc::new(gpui_windows::WindowsPlatform::new(false).expect(...))
                                         Linux/FreeBSD → gpui_linux::current_platform(false)
                                         wasm    → Rc::new(gpui_web::WebPlatform::new(true))
```

注意两条隐藏的编译期规则：

1. `current_platform` 函数体内四段 `#[cfg]` 在**任何一次编译中只有一段存活**——你在 Linux 上编译时，macOS 分支的代码根本不存在，所以 `gpui_macos` 这个依赖也不会被链接。
2. 依赖也是按 target 声明的（`Cargo.toml` 的 `[target...]` 段），`#[cfg]` 与依赖表两者配合，才让"一份代码处处可编译"成立。

#### 4.1.3 源码精读

先看门面本身。`application()` 总共只有 8 行：

- [crates/gpui_platform/src/gpui_platform.rs:13-21](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L13-L21)：`application()` 的完整实现。wasm 目标走 `application_with_web_backend`（自动选择 WebGPU/WebGL 后端）；非 wasm 目标调用 `gpui::Application::with_platform(current_platform(false))`——注意传入的 `headless` 参数是 `false`，即"我要一个有真实窗口的应用"。

再看 `current_platform` 的开头两个分支（完整四分支的精读留给 u1-l4）：

- [crates/gpui_platform/src/gpui_platform.rs:57-69](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L57-L69)：`current_platform(headless)` 签名返回 `Rc<dyn Platform>`。macOS 分支直接 `Rc::new(MacPlatform::new(headless))`；Windows 分支多了 `.expect("failed to initialize Windows platform")`——因为 Windows 平台初始化可能失败（返回 `Result`），而门面层选择直接 panic。

依赖侧的"另一半条件编译"：

- [crates/gpui_platform/Cargo.toml:23-37](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/Cargo.toml#L23-L37)：基础依赖只有 `gpui`；随后按 `[target.'cfg(target_os = "macos")']`、`windows`、`linux/freebsd`、`wasm` 四组分别引入对应平台 crate。在 Linux 上编译时，`gpui_macos`、`gpui_windows` 甚至不会出现在依赖树里。

官方最小示例长什么样：

- [crates/gpui/examples/hello_world.rs:92-109](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/hello_world.rs#L92-L109)：`run_example()`——`application().run(|cx| { ... cx.open_window(WindowOptions {...}, |_, cx| cx.new(|_| HelloWorld { .. })) ... cx.activate(true); })`。这就是"跨平台 GUI 程序"的全部骨架：拿应用 → 启动回调里开一个窗口 → 激活。
- [crates/gpui/examples/hello_world.rs:111-121](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/hello_world.rs#L111-L121)：文件末尾的双入口写法——桌面目标用 `fn main()`；wasm 目标用 `#[wasm_bindgen(start)] pub fn start()` 并先调用 `gpui_platform::web_init()` 初始化 panic 钩子与日志。配合文件第一行的 `#![cfg_attr(target_family = "wasm", no_main)]`，同一份示例既能在桌面跑也能在浏览器跑。这就是 `application()` 想带给使用者的体验。

为什么 gpui 的示例能直接 `use gpui_platform::application`：

- [crates/gpui/Cargo.toml:147-151](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/Cargo.toml#L147-L151)：`gpui` 的 `[dev-dependencies]` 里声明了 `gpui_platform = { workspace = true, features = ["font-kit", "wayland", "x11"] }`。示例（examples）属于 dev 依赖范畴，所以每个官方示例都能直接使用门面入口。

#### 4.1.4 代码实践：跑通官方示例

1. **实践目标**：不写一行代码，先把官方示例跑起来，建立"它能跑"的直观感受。
2. **操作步骤**：在 zed 仓库根目录执行：
   ```bash
   cargo run -p gpui --example hello_world
   cargo run -p gpui --example window
   ```
3. **需要观察的现象**：
   - `hello_world`：弹出一个 500×500 的灰色窗口，中间显示 "Hello, World!" 和一排彩色方块。
   - `window`：弹出 800×600 窗口，里面有一排按钮（Normal / Popup / Floating / Dialog / ...），点击可创建不同 `WindowOptions` 的子窗口；调整窗口大小时终端会打印 `Window bounds changed: ...`（来自示例里的 `observe_window_bounds` 回调）。
4. **预期结果**：两个示例都能正常启动、显示窗口、响应点击。Linux 上需要 Wayland（`WAYLAND_DISPLAY`）或 X11（`DISPLAY`）会话；无显示环境下会退化为 headless 行为（详见 4.4）。**待本地验证**（本讲义写作环境无图形会话，未实际运行）。

#### 4.1.5 小练习与答案

**练习 1**：`application()` 为什么不需要调用者告诉它"现在是什么操作系统"？

**参考答案**：因为平台选择发生在**编译期**。`current_platform` 内部用 `#[cfg(target_os = ...)]` 分支，每次编译只有一个分支存活；编译目标本身已经决定了用哪个平台 crate，运行期无需再判断。

**练习 2**：`application()` 与 `headless()` 都最终调用了 `Application::with_platform`，它们传入的关键差异是什么？

**参考答案**：唯一差异是 `current_platform` 的 `headless` 布尔参数——`application()` 传 `false`（真实窗口），`headless()` 传 `true`（无显示环境实现）。这个布尔值会一路传到具体平台构造器（如 `MacPlatform::new(headless)`、`gpui_linux::current_platform(true)`）。

### 4.2 `Application::with_platform`：把 Platform 实例注入应用

#### 4.2.1 概念说明

`Application` 是 GPUI 的"应用外壳"：你只在 `main` 里配置和启动它一次，之后很少再碰。它内部其实非常薄——就是一个引用计数的 `AppCell`（装着 `App` 状态的 `RefCell`）。

`with_platform` 是 `Application` 的**平台注入点**：把"谁来提供系统能力"（一个 `Rc<dyn Platform>`）塞进应用。这是典型的依赖注入——gpui 主 crate 定义契约但不含实现，实现由外部（这里是 `gpui_platform` 门面）递进来。正因如此，gpui 才能同时被四个平台 crate 和测试替身（`TestPlatform`）复用。

#### 4.2.2 核心流程

```text
Application::with_platform(platform: Rc<dyn Platform>)
        │
        └─ App::new_app(platform, 空资源, NullHttpClient)
               │  （App::new_app 内部从 platform 依次抽取：）
               ├─ background_executor / foreground_executor   ← 平台提供执行器
               ├─ text_system                                  ← 平台提供字体/文本系统
               ├─ keyboard_layout / keyboard_mapper            ← 平台提供键盘布局与映射
               └─ 断言"必须在主线程构造 App"
        │
        └─ 返回 Application(Rc<AppCell>)——只是一个句柄，还没有运行
```

一个容易忽略的细节：`with_platform` 默认装的是 `NullHttpClient`（空操作 HTTP 客户端）。桌面应用通常不发 HTTP 请求所以无所谓；而 web 入口 `application_with_web_backend` 会紧接着用 `.with_http_client(...)` 换上浏览器 `fetch` 实现。

#### 4.2.3 源码精读

- [crates/gpui/src/app.rs:144-146](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L144-L146)：`Application` 的定义——`pub struct Application(Rc<AppCell>)`。文档注释说明它"通常在 main 函数里构造，除初始配置外很少交互"。
- [crates/gpui/src/app.rs:176-183](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L176-L183)：`with_platform` 全文。调用 `App::new_app(platform, Arc::new(()), Arc::new(NullHttpClient))`——第三个参数就是默认的空 HTTP 客户端；第二个参数是空资源源（可用 `with_assets` 覆盖）。
- [crates/gpui/src/app.rs:777-793](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L777-L793)：`App::new_app` 的前半段，能清楚看到"应用的一切系统能力都从 platform 抽取"：`platform.background_executor()`、`platform.foreground_executor()`、`platform.text_system()`、`platform.keyboard_layout()`、`platform.keyboard_mapper()`；同时断言必须在主线程构造（macOS 上 AppKit 有严格的主线程约束）。
- [crates/zed/src/main.rs:86-93](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/zed/src/main.rs#L86-L93)：Zed 编辑器自己的用法——`build_application()` 先调 `gpui_platform::current_platform(false)` 拿平台，再按环境变量决定用 `Application::with_platform(platform)` 还是 `Application::new_inaccessible(platform)`（强制关闭无障碍集成）。注意 Zed 没有直接用 `application()`，因为它需要在注入前对平台做额外选择——这说明 `with_platform` 是更底层的通用入口，`application()` 是给不需要定制的多数人的便捷入口。

#### 4.2.4 代码实践：找出所有"注入点"的使用者

1. **实践目标**：通过真实调用方，体会 `with_platform` 是通用注入点、`application()` 是便捷糖。
2. **操作步骤**：在仓库根目录执行：
   ```bash
   grep -rn "Application::with_platform" crates --include="*.rs"
   grep -rn "gpui_platform::headless()" crates --include="*.rs"
   ```
3. **需要观察的现象**：第一个命令的命中集中在 `gpui_platform/src/gpui_platform.rs` 自身和 `zed/src/main.rs`；第二个命令命中一批"无界面"程序——`remote_server`、`editor_benchmarks`、`project_benchmarks`、`eval_cli`、`fs_benchmarks` 等。
4. **预期结果**：你会发现一条规律：**要窗口的程序用 `application()` 或 `current_platform(false)`；不要窗口的程序（远程服务器、基准测试、CLI）用 `headless()`**。这就是两个入口的真实分工。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `with_platform` 接收 `Rc<dyn Platform>` 而不是泛型 `impl Platform`？

**参考答案**：用 trait 对象意味着 `Application` 内部只需存一份固定类型 `Rc<dyn Platform>`，调用方可以运行期决定装哪个实现（例如测试时换 `TestPlatform`），也让 `App` 结构体不必泛型化——避免整个应用类型被平台类型参数"污染"。代价是每次方法调用有一次动态分发，但对平台级调用（开窗、剪贴板这类低频操作）完全可接受。

**练习 2**：`App::new_app` 为什么要断言"必须在主线程构造"？

**参考答案**：因为部分平台的实现（最典型是 macOS 的 AppKit/Cocoa）要求几乎所有 UI 操作发生在进程主线程，而 GPUI 的整体模型就是"单前台线程更新所有实体与 UI"（见 CLAUDE.md 与第 4 单元）。在构造期就断言，能把"在错误线程建应用"这类问题提前到启动瞬间暴露，而不是等到某个偶发的运行期崩溃。

### 4.3 `Application::run` 与 `App::open_window`：启动回调与开窗主链路

#### 4.3.1 概念说明

拿到 `Application` 后，程序还没真正"跑起来"——事件循环尚未启动。`Application::run(on_finish_launching)` 做的事是：把你的回调打包，交给**平台的事件循环**，然后阻塞在那里直到应用退出。回调会在"启动完成"后被调用一次，相当于 macOS 术语里的 `applicationDidFinishLaunching`。

开窗口则是 `App::open_window`：你给出 `WindowOptions`（窗口边界、标题栏样式、窗口种类等**应用层偏好**）和一个"构造根视图"的闭包；gpui 内部把 `WindowOptions` 翻译成更底层的 `WindowParams`，再调用平台层的 `Platform::open_window` 拿到真正的系统窗口（`Box<dyn PlatformWindow>`）。

#### 4.3.2 核心流程

```text
application().run(|cx| ...)
   │
   └─ platform.run( 打包后的 on_finish_launching )     ← 进入平台事件循环（阻塞）
          │
          └─ 启动完成 → 调用你的回调，参数是 &mut App（应用上下文 cx）
                 │
                 └─ cx.open_window(WindowOptions, build_root_view)
                        │
                        ├─ 注册窗口 id（cx.windows.insert）
                        ├─ Window::new(handle, options, cx)
                        │     ├─ 解构 WindowOptions → 组装 WindowParams
                        │     └─ cx.platform.open_window(handle, params)
                        │            └─ 返回 Box<dyn PlatformWindow>（真正的系统窗口）
                        ├─ 调用 build_root_view 构造根视图并挂到 window.root
                        ├─ window.draw(cx) —— 返回前至少绘制一帧（Windows 上的竞态修复）
                        └─ 返回 WindowHandle<V>
```

两个值得记住的细节：

- **`WindowOptions` vs `WindowParams`**：前者是应用层 API（带默认值、语义化字段），后者是平台层参数（解构后的裸参数集合）。这层翻译把"应用想怎样"与"平台需要什么"解耦。
- **返回前先画一帧**：`open_window` 在返回前会强制绘制一次，注释里写明这是为了修 Windows 上"返回了一个从未渲染过的窗口"导致的崩溃——跨平台抽象层里这类平台特调会以通用注释的形式沉淀下来。

#### 4.3.3 源码精读

- [crates/gpui/src/app.rs:233-243](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L233-L243)：`Application::run` 全文。它把 `on_finish_launching` 闭包装进 `Box`，交给 `platform.run(...)`；注释说明回调会在"应用完全启动后"被调用一次。注意 `run` 拿走 `self`——应用只能启动一次。
- [crates/gpui/examples/window.rs:311-337](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/window.rs#L311-L337)：官方 window 示例的入口 `run_example()`：`application().run(|cx| ...)` 里先 `Bounds::centered(...)` 计算居中边界，`cx.open_window(WindowOptions { window_bounds: Some(WindowBounds::Windowed(bounds)), ..Default::default() }, |window, cx| ...)` 开主窗口，最后 `cx.activate(true)` 把应用带到前台，并注册 `Quit` 动作与 `cmd-q` 快捷键。
- [crates/gpui/src/app.rs:1242-1275](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/app.rs#L1242-L1275)：`App::open_window` 全文。签名要求根视图类型 `V: 'static + Render`；流程为注册窗口 id → `Window::new` → 压栈调用 `build_root_view` → 存入 `window.root` → `window.draw(cx)`（L1258-1263 的注释解释了"至少绘制一帧"的 Windows 背景）→ 存回窗口表并返回 `WindowHandle<V>`。失败时（L1269-1272）会撤销刚注册的窗口 id，保持状态一致。
- [crates/gpui/src/window.rs:1334-1388](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L1334-L1388)：`Window::new` 中段——把 `WindowOptions` 逐字段解构（L1339-1362），在 L1368 计算默认边界，随后在 L1369 调用 `cx.platform.open_window(handle, WindowParams { ... })`，这就是**应用层与平台层的交界点**：从这里往下就进入各操作系统的实现了（第 5-7 单元）。

#### 4.3.4 代码实践：改造 WindowOptions 观察行为

1. **实践目标**：亲手改 `WindowOptions` 的字段，观察它们如何影响窗口形态，建立"应用层偏好 → 平台层窗口"的直观映射。
2. **操作步骤**：
   1. 打开 `crates/gpui/examples/hello_world.rs`，把 `Bounds::centered(None, size(px(500.), px(500.0)), cx)` 改成 `size(px(300.), px(150.0))`。
   2. 运行 `cargo run -p gpui --example hello_world`，观察窗口尺寸变化。
   3. 再把 `WindowOptions` 增加一个字段 `kind: WindowKind::PopUp`（可参照 window.rs 示例中 Popup 按钮的写法），重新运行观察窗口装饰与置顶行为的差异。
   4. （源码阅读部分）对照 [crates/gpui/src/window.rs:1371-1387](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L1371-L1387)，确认你改动的每个字段都出现在 `WindowParams` 的构造里。
3. **需要观察的现象**：窗口尺寸随第一步改变；改为 `PopUp` 后窗口呈现弹出样式（无常规装饰/不进任务栏，具体表现依平台而异）。
4. **预期结果**：字段改动直接反映到窗口形态；并能在源码里指出每个字段流入 `WindowParams` 的哪一项。**待本地验证**（窗口形态的平台差异需在你的操作系统上实际确认）。

#### 4.3.5 小练习与答案

**练习 1**：`Application::run` 的回调参数是 `&mut App`，而 `App::open_window` 定义在 `App` 上。请说明"为什么开窗口必须写在这个回调里（或回调触发的代码路径上）"。

**参考答案**：因为 `run` 之前事件循环还没启动、应用也未"完全启动"；`open_window` 最终要调用平台层创建系统窗口，这必须在平台事件循环就绪之后进行。把初始化逻辑放进 `on_finish_launching` 回调，保证它运行在正确的时间点（启动完成）和正确的线程（平台主线程）上。

**练习 2**：`open_window` 里的 `build_root_view` 闭包为什么接收 `&mut Window` 和 `&mut App` 两个参数？

**参考答案**：构造根视图时经常需要与"正在创建的这个窗口"交互——例如 window.rs 示例里在闭包内调用 `cx.observe_window_bounds(window, ...)` 监听本窗口的边界变化。把 `Window` 作为参数传入，让这类"针对新窗口"的初始化不必等 `open_window` 返回后再补做。

### 4.4 `headless()`：无显示环境的另一个入口

#### 4.4.1 概念说明

很多场景**没有屏幕**：CI 里的 UI 快照测试、远程服务器上跑的 `remote_server`、各种 benchmark、只想要 gpui 的布局/文本能力做离屏计算的 CLI。这些程序仍然需要事件循环、执行器、文本系统——只是不需要真实窗口。

`gpui_platform::headless()` 就是为此准备的入口：它调用 `with_platform(current_platform(true))`，把"无显示环境"的标志传给平台构造器。以 Linux 为例，这会直接构造 `HeadlessClient`，跳过 Wayland/X11 的初始化。

`headless = true` 的行为差异可以概括为三点：

1. **窗口不显示**：`open_window` 仍能成功，返回的是一个"逻辑窗口"（如 `HeadlessWindow`），有边界几何，但不会上屏。
2. **事件循环照常运转**：`run()` 依旧阻塞，任务调度、计时器一切正常——只是没有真实输入事件。
3. **系统能力静默降级**：光标设置、打开 URL 之类的接口变成空操作。

#### 4.4.2 核心流程

```text
gpui_platform::headless()
   └─ Application::with_platform( current_platform(true) )
          │  （以 Linux 为例）
          └─ gpui_linux::current_platform(true)
                 ├─ headless == true → 直接返回 LinuxPlatform { inner: HeadlessClient::new() }
                 │    （不探测 Wayland/X11，无需图形会话）
                 └─ headless == false → guess_compositor() 探测：
                        ZED_HEADLESS 环境变量存在        → "Headless" → HeadlessClient
                        WAYLAND_DISPLAY 非空              → "Wayland"  → WaylandClient
                        DISPLAY 非空                      → "X11"      → X11Client
                        都没有                            → "Headless" → HeadlessClient
```

注意一个微妙点：即使你用 `application()`（`headless=false`），Linux 上如果既没有 `WAYLAND_DISPLAY` 也没有 `DISPLAY`，`guess_compositor()` 也会返回 `"Headless"` 自动落入无头实现——这就是"在无图形环境的机器上跑 `application()` 示例不会立刻崩溃"的原因。

#### 4.4.3 源码精读

- [crates/gpui_platform/src/gpui_platform.rs:23-25](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_platform/src/gpui_platform.rs#L23-L25)：`headless()` 全文——就是 `with_platform(current_platform(true))`，与 `application()` 唯一的区别是那个布尔值。
- [crates/gpui_linux/src/linux.rs:30-60](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux.rs#L30-L60)：Linux 侧 `current_platform`。L34-38 是 headless 短路分支——`if headless { return ... HeadlessClient::new() }`，连 compositor 探测都不做；L40 起才按 `gpui::guess_compositor()` 的结果在 Wayland/X11/Headless 三者间选择。
- [crates/gpui/src/platform.rs:98-123](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/platform.rs#L98-L123)：`guess_compositor()`——先看 `ZED_HEADLESS` 环境变量（设置即强制无头），再看 `WAYLAND_DISPLAY` / `DISPLAY` 是否存在且非空，据此返回 `"Wayland"` / `"X11"` / `"Headless"`。
- [crates/gpui_linux/src/linux/headless/client.rs:100-113](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/headless/client.rs#L100-L113)：`HeadlessClient` 的 `open_window`——直接 `Ok(Box::new(HeadlessWindow::new(...)))` 成功返回一个逻辑窗口；`compositor_name()` 返回 `"headless"`（4.1 实践里打印这个名字就能确认当前后端）。紧随其后的 `set_cursor_style`、`open_uri`、`reveal_path` 都是空实现——这就是"系统能力静默降级"的实例。
- [crates/gpui_linux/src/linux/headless/client.rs:133-142](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_linux/src/linux/headless/client.rs#L133-L142)：`HeadlessClient::run`——取走事件循环（第二次调用会 panic："App is already running"），然后 `event_loop.run(None, ...)` 进入 **calloop** 事件循环。也就是说无头模式并不是"空转返回"，而是有一个真实（但没有窗口系统）的事件循环在驱动任务与计时器。
- 真实使用者佐证：[crates/remote_server/src/server.rs:570](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/remote_server/src/server.rs#L570) 与 [crates/editor_benchmarks/src/main.rs:113](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/editor_benchmarks/src/main.rs#L113)——远程服务器与基准测试都通过 `gpui_platform::headless().run(...)` 驱动 gpui。

#### 4.4.4 代码实践：观察 headless 与普通模式的差异

1. **实践目标**：验证"headless 下 `run` 回调照常执行、窗口逻辑存在但不上屏、进程不会自行退出"。
2. **操作步骤**（Linux，待本地验证）：
   1. 运行图形版：`cargo run -p gpui --example hello_world`，确认窗口出现后手动关闭，进程退出。
   2. 强制无头运行同一示例：`ZED_HEADLESS=1 cargo run -p gpui --example hello_world`（注意：这里走的仍是 `application()`，但 `guess_compositor()` 会因环境变量返回 `"Headless"`）。
   3. 观察终端：程序不报错也不弹窗；用 `Ctrl+C` 结束。
   4. （可选）在无图形环境的服务器/容器里重复第 2 步，不设任何环境变量，观察自动落入 headless。
3. **需要观察的现象**：第 2 步没有窗口出现；进程持续运行（事件循环在阻塞）；`Ctrl+C` 才能结束。若在 `run` 回调里加一句 `println!("launched, compositor: {:?}", std::env::var("ZED_HEADLESS"))`，能看到回调确实执行了一次。
4. **预期结果**：`run` 的 `on_finish_launching` 回调在两种模式下各执行一次；差别只在"窗口是否上屏"与"是否有真实输入事件"。headless 下程序不会因为"没有窗口可显示"而崩溃或立即退出。**待本地验证**（本讲义写作环境未实际运行）。

#### 4.4.5 小练习与答案

**练习 1**：在 Linux 图形会话中，`ZED_HEADLESS=1` 时调用 `application()` 与直接调用 `headless()`，最终得到的平台实现有何异同？

**参考答案**：最终都得到 `LinuxPlatform { inner: HeadlessClient }`。区别在路径：`application()` 走 `current_platform(false)` → `guess_compositor()` 读到 `ZED_HEADLESS` 返回 `"Headless"`（环境变量在**运行期**生效）；`headless()` 走 `current_platform(true)` 的短路分支（**参数**在构造期生效，不读环境变量）。

**练习 2**：为什么 `remote_server`（运行在无显示的远程机器上）选择 `headless()` 而不是 `application()`？

**参考答案**：远程服务器没有 Wayland/X11 会话，`application()` 的探测最终也会落到 Headless，但那是"碰巧的回退"；显式使用 `headless()` 表达了意图（我们本来就不要窗口），并且不依赖环境变量探测，行为确定。同时 headless 下事件循环、执行器、文本系统仍然完整，足以支撑远程协作的 UI 状态计算。

**练习 3**：`HeadlessClient::run` 里为什么用 `self.0.borrow_mut().event_loop.take().expect("App is already running")`，而不是直接持有事件循环？

**参考答案**：`take()` 把事件循环从 `RefCell` 中拿走所有权并转移到本次 `run` 调用的栈上；如果第二次调用 `run`，`take()` 返回 `None`，`expect` 立刻报出清晰的错误信息。这是用 `Option` + `expect` 实现"只能启动一次"语义的惯用写法（错误消息即文档）。

## 5. 综合实践

**任务**：新建一个独立小 crate，用 `gpui_platform::application()` 打开一个显示 "Hello Platform" 的窗口；再改成 `headless()` 入口，对比记录两种模式下 `run` 回调的执行情况。

由于 gpui 系列 crate 不发布到 crates.io，最省事的方式是把新 crate 放进 zed 仓库工作区（与 `examples`、`zed` 等 crate 同级）：

1. **创建 `crates/hello_platform/Cargo.toml`**（示例代码）：
   ```toml
   [package]
   name = "hello_platform"
   version = "0.1.0"
   edition.workspace = true
   publish.workspace = true
   license = "Apache-2.0"

   [lints]
   workspace = true

   [dependencies]
   gpui = { workspace = true }
   # Linux 上需要显式启用窗口系统 feature（macOS/Windows 不需要）：
   gpui_platform = { workspace = true, features = ["wayland", "x11"] }
   ```
2. **在仓库根 `Cargo.toml` 的 `members` 列表里加入 `"crates/hello_platform"`**（工作区成员是显式声明的）。
3. **创建 `crates/hello_platform/src/main.rs`**（示例代码，参照 `crates/gpui/examples/hello_world.rs` 的结构）：
   ```rust
   use gpui::{
       App, Bounds, Context, SharedString, Window, WindowBounds, WindowOptions,
       div, prelude::*, px, rgb, size,
   };
   use gpui_platform::application;

   struct HelloPlatform {
       text: SharedString,
   }

   impl Render for HelloPlatform {
       fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
           div()
               .flex()
               .size_full()
               .bg(rgb(0x1e1e2e))
               .justify_center()
               .items_center()
               .text_color(rgb(0xffffff))
               .child(format!("Hello, {}!", self.text))
       }
   }

   fn main() {
       application().run(|cx: &mut App| {
           println!("[run 回调] 应用启动完成");
           let bounds = Bounds::centered(None, size(px(400.), px(300.0)), cx);
           cx.open_window(
               WindowOptions {
                   window_bounds: Some(WindowBounds::Windowed(bounds)),
                   ..Default::default()
               },
               |_, cx| {
                   cx.new(|_| HelloPlatform {
                       text: "Platform".into(),
                   })
               },
           )
           .unwrap();
           cx.activate(true);
       });
   }
   ```
4. **运行图形版**：`cargo run -p hello_platform`。预期：窗口显示 "Hello, Platform"，终端打印一行 `[run 回调] 应用启动完成`。
5. **切换到 headless 版**：把 `use gpui_platform::application;` 与 `application()` 分别改为 `headless` 与 `headless()`，再次运行。预期：**没有窗口**，但终端仍打印那一行启动日志；进程不退出，需 `Ctrl+C` 结束。
6. **记录实验结论**（建议写进你的学习笔记）：两种模式下 `run` 回调是否都执行、各执行几次；`open_window` 是否都成功返回；进程生命周期有何不同；结合 4.4 的源码解释原因（提示：`HeadlessClient::open_window` 返回逻辑窗口、`HeadlessClient::run` 进入 calloop 循环）。

> 本综合实践在无图形环境下可只完成第 5、6 步（headless 部分）；图形部分**待本地验证**。

## 6. 本讲小结

- `gpui_platform::application()` = `Application::with_platform(current_platform(false))`：一行代码在编译期完成平台选择，桌面目标拿到真实窗口平台，wasm 目标自动走 web 后端。
- `Application::with_platform` 是平台注入点：应用的一切系统能力（前后台执行器、文本系统、键盘布局/映射）都在 `App::new_app` 里从 `Rc<dyn Platform>` 抽取；默认注入空 HTTP 客户端。
- `Application::run(on_finish_launching)` 把回调交给平台事件循环后**阻塞**；回调在启动完成后于主线程执行一次，是开窗口、注册动作等初始化的正确时机。
- `App::open_window` 的调用链是 `open_window → Window::new → Platform::open_window`：应用层 `WindowOptions` 被翻译成平台层 `WindowParams`，返回前还会强制绘制一帧（Windows 竞态修复）。
- `headless()` 与 `application()` 只差一个布尔：headless 下窗口逻辑存在但不上屏、系统能力静默空操作，而事件循环（Linux 上是 calloop）照常阻塞运转——这正是 CI、远程服务器与基准测试使用 gpui 的方式。

## 7. 下一步学习建议

- 下一讲（u1-l3）转到构建视角：逐段阅读 `gpui_platform/Cargo.toml` 的 feature 表与按 target 分组的依赖，理解 `wayland` / `x11` / `test-support` 等 feature 如何透传，并动手用不同的 feature 组合构建。
- 若想先巩固本讲的调用链，建议用 rust-analyzer 在 `App::open_window`（`crates/gpui/src/app.rs:1242`）上执行 Find All References / Go to Implementation，把 `cx.platform.open_window` 在四个平台 crate 中的实现各看一眼——这是第 5-7 单元的预习。
- 推荐顺带阅读 `crates/gpui/examples/on_window_close_quit.rs`，看"最后一个窗口关闭时如何退出应用"，加深对 `run` 阻塞语义的理解。
