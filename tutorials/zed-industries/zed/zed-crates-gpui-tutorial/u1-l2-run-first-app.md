# 运行第一个 GPUI 应用：hello_world 逐行解读

## 1. 本讲目标

学完本讲，你应该能够：

1. 会用 `cargo run -p gpui --example <name>` 运行 GPUI 仓库中的任意示例。
2. 读懂启动一个 GPUI 应用所需的四个标准步骤：`application().run` → `cx.open_window` → `cx.new` → `impl Render`。
3. 能修改 `div` 的样式链式调用，并通过重新运行示例观察界面变化。
4. 说清楚 `Application`、`App`、`Window`、`Entity<HelloWorld>` 这几个角色在启动流程中各自出现的位置。

本讲是全手册第一个「动手」讲义：所有结论都来自真实源码，所有实践都只需要一台能编译 Rust 的机器。

## 2. 前置知识

本讲默认你已读完 u1-l1（GPUI 的定位与三层编程界面），此外还需要以下通俗概念：

- **事件循环（event loop）**：GUI 程序和命令行程序最大的区别是「程序不会跑完就退出」。操作系统把鼠标移动、按键、窗口重绘等事件源源不断地投递给程序，程序在一个循环里逐个处理，直到用户关闭窗口。这个循环由「平台层」（macOS 的 AppKit、Linux 的 Wayland/X11、Windows 的 Win32）驱动。
- **回调（callback）**：你不直接写这个循环，而是把一个闭包交给框架，框架在合适的时机调用它。GPUI 中最典型的就是 `application().run(|cx| { ... })`——闭包在「应用完全启动之后」被调用一次。
- **trait**：Rust 的接口概念。本讲会碰到三个 trait：`Render`（能把自己渲染成元素树）、`IntoElement`（能作为元素树中的一个节点）、`AppContext`（统一提供 `cx.new` 等实体操作）。
- **`RefCell` 与借用**：Rust 的运行期可变借用检查。GPUI 把整个应用状态放在一个 `RefCell<App>` 里，同一时刻只允许一个 `&mut App` 存在——这决定了「所有 UI 代码跑在单个前台线程上」这一基本事实（u2 会展开）。
- **逻辑像素（`px()`）**：GPUI 用 `Pixels` 表示逻辑像素，由框架按屏幕缩放系数换算成物理像素。写 `px(500.0)` 即可，不必关心用户是 1x 还是 2x 屏幕。

回顾 u1-l1 的关键结论：GPUI 是混合「立即模式 + 保留模式」的框架——元素树每帧重建（立即），应用状态存在跨帧存活的实体里（保留）。本讲的 `HelloWorld` 结构体就是那个「保留」的部分，`render()` 里的 `div()` 链就是「立即」的部分。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [examples/hello_world.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/hello_world.rs) | 官方最小 GPUI 示例，全篇 122 行 | 整个启动流程的唯一「脚本」 |
| [examples/README.md](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/README.md) | 示例目录导览 | 如何运行示例、新手该先看哪些 |
| [src/app.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs) | `Application`、`App`、`open_window` 等核心定义（约 3000 行） | `run`、`open_window`、`App::new` |
| [src/platform.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/platform.rs) | 平台抽象 trait 与窗口相关公共类型 | `WindowOptions`、`WindowBounds` |
| [src/geometry.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/geometry.rs) | 几何基础类型 | `Bounds::centered` |
| [src/element.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/element.rs) | 元素与 `Render` trait 定义 | `Render` trait 本体 |
| [src/gpui.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs) | crate 注册表 | `AppContext` trait 中的 `new` |
| [src/prelude.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/prelude.rs) | 预导入模块 | `use gpui::prelude::*` 到底导入了什么 |
| ../gpui_platform/src/gpui_platform.rs | 平台门面 crate | `application()` 如何按操作系统选平台 |

另外会顺带引用 [Cargo.toml](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml) 说明示例为何能用 `gpui_platform`。

## 4. 核心概念与源码讲解

本讲的三个最小模块对应启动流程的三个阶段：**启动应用**（`Application::run`）→ **打开窗口**（`App::open_window`）→ **渲染内容**（`Render`）。hello_world 的 `run_example()` 函数恰好按这个顺序书写：

```rust
fn run_example() {
    application().run(|cx: &mut App| {          // ① 启动应用，拿到 &mut App
        let bounds = Bounds::centered(None, size(px(500.), px(500.0)), cx);
        cx.open_window(                          // ② 打开窗口，创建根视图
            WindowOptions { window_bounds: Some(WindowBounds::Windowed(bounds)), ..Default::default() },
            |_, cx| { cx.new(|_| HelloWorld { text: "World".into() }) }
        ).unwrap();
        cx.activate(true);                       // ③ 把应用带到前台
    });
}
```

上面是节选示意，完整源码见 [examples/hello_world.rs:L92-L109](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/hello_world.rs#L92-L109)。下面逐模块展开。

### 4.1 Application::run：应用是如何启动的

#### 4.1.1 概念说明

`Application` 是「尚未启动的应用」。它的定义只有一行——一个装在 `Rc` 里的 `AppCell`：

> [src/app.rs:L143-L145](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L143-L145)
> `pub struct Application(Rc<AppCell>);`——官方文档注释说明：通常在 `main` 函数里构造，除初始配置和启动外很少再接触它。

`AppCell` 则是 `RefCell<App>` 的包装（[src/app.rs:L81](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L81)）。也就是说：

- `App` 是真正的应用状态容器——文档注释称它「包含整个应用的状态，并以引用形式传给各种回调」（[src/app.rs:L672-L675](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L672-L675)），内部持有平台句柄、文本系统、实体表 `entities: EntityMap`、窗口表 `windows: SlotMap<WindowId, ...>`、执行器等几十个字段（[src/app.rs:L675-L689](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L675-L689)）。
- `Application::run` 是「交出控制权」的一步：调用后，你的 `main` 不再继续执行新代码，控制权进入平台事件循环，直到应用退出。

`hello_world.rs` 顶部那句 `use gpui_platform::application;` 值得注意：`application()` 不在 `gpui` 本体里，而在门面 crate `gpui_platform` 中，它按编译目标操作系统挑选平台实现。

#### 4.1.2 核心流程

启动一条链可以概括为：

```text
gpui_platform::application()
    │  按 target_os 选择 Platform（macOS / Windows / Linux / wasm）
    ▼
Application::with_platform(platform)
    │  内部调用 App::new_app(...)
    │  断言"必须主线程构造"、初始化 TextSystem / EntityMap / 执行器
    ▼
application().run(|cx: &mut App| { ... })
    │  取出 platform，调用 platform.run(callback)，随即阻塞
    ▼
平台事件循环启动完成 → 回调被调用一次 → 你的代码拿到 &mut App
    ▼
在回调里 open_window / 创建实体 / 注册全局…… 事件循环接管此后的一切
```

关键点：

1. `run` 的回调类型是 `FnOnce(&mut App)`，即启动完成后**只调用一次**；此后你的代码都以「事件处理器」的形式被事件循环调用（鼠标回调、每帧的 `render` 等）。
2. `Application::run` 的函数体只有三行：克隆 `AppCell`、克隆平台句柄、调用 `platform.run(...)`——阻塞发生在平台层，不在 GPUI 核心里。
3. `App::new_app` 里有一个断言 `background_executor.is_main_thread()`，即「必须在主线程构造 App」（[src/app.rs:L767-L779](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L767-L779)）。这是 u1-l1 所说「单前台线程」模型的第一个直接证据。

#### 4.1.3 源码精读

**① `application()`：按操作系统选择平台。**

> [../gpui_platform/src/gpui_platform.rs:L13-L21](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_platform/src/gpui_platform.rs#L13-L21)
> 非 wasm 目标时直接返回 `gpui::Application::with_platform(current_platform(false))`；wasm 目标则走 web 后端初始化。

> [../gpui_platform/src/gpui_platform.rs:L57-L81](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_platform/src/gpui_platform.rs#L57-L81)
> `current_platform(headless)` 用四组 `#[cfg]` 分别返回 `MacPlatform`、`WindowsPlatform`、`gpui_linux::current_platform(...)`（Linux/FreeBSD）或 `WebPlatform`。这就是 u1-l1 讲过的「gpui_platform 是按 target_os 挑选后端的约百行门面」的具体形态——你的应用代码因此完全不用写 `#[cfg(target_os = ...)]`。

**② `Application::run`：交出控制权。**

> [src/app.rs:L224-L236](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L224-L236)
> `run` 把闭包包进 `Box::new`，调用 `platform.run(...)`；平台在「应用完全启动」后 `borrow_mut` 出 `&mut App` 并调用你的闭包。文档注释明确写着：*Start the application. The provided callback will be called once the app is fully launched.*

顺带一提：`App` 内部还有一个包裹了效果冲刷（effect flushing）的 `update` 方法（[src/app.rs:L1045-L1050](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1045-L1050)），后面 `open_window` 的第一步就是钻进它——本讲只需知道「对 `App` 的一切修改都发生在这样的 update 作用域里」。

**③ hello_world 的入口与 wasm 分身。**

> [examples/hello_world.rs:L111-L114](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/hello_world.rs#L111-L114)
> 桌面平台上 `main` 只做一件事：调用 `run_example()`。

> [examples/hello_world.rs:L1](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/hello_world.rs#L1) 与 [examples/hello_world.rs:L116-L121](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/hello_world.rs#L116-L121)
> 文件第一行 `#![cfg_attr(target_family = "wasm", no_main)]` 加上底部的 `#[wasm_bindgen(start)]` 入口，让同一份代码既能 `cargo run` 也能编译成网页版。初学阶段可忽略 wasm 分支。

**④ 示例为什么能用 `gpui_platform`？**

> [Cargo.toml:L147-L151](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L147-L151)
> `gpui` crate 的 `[dev-dependencies]` 里声明了 `gpui_platform = { workspace = true, features = ["font-kit", "wayland", "x11"] }`，所以 examples（属于 dev 目标）能直接 `use gpui_platform::application;`。若你在自己的项目里使用 GPUI，需要在 `Cargo.toml` 同时依赖 `gpui` 与 `gpui_platform`。

#### 4.1.4 代码实践

**实践目标**：确认「run 回调只在启动完成时执行一次」，并验证 `Application` 的配置方法都是链式的。

**操作步骤**：

1. 从仓库根目录运行 hello_world（命令来自 [examples/README.md:L3-L7](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/README.md#L3-L7)）：

   ```sh
   cargo run -p gpui --example hello_world
   ```

2. 打开 `crates/gpui/examples/hello_world.rs`，在 `application().run(|cx: &mut App| {` 的闭包第一行插入一句日志（示例代码，非项目原有代码）：

   ```rust
   application().run(|cx: &mut App| {
       eprintln!("应用启动完成，收到 &mut App");
       let bounds = Bounds::centered(None, size(px(500.), px(500.0)), cx);
       // ……其余不变
   ```

3. 再次运行，观察终端输出；然后随便在窗口里移动鼠标、点击，观察日志是否重复打印。

**需要观察的现象**：终端恰好打印一次 `应用启动完成，收到 &mut App`；此后无论怎么操作窗口都不会再打印（除非重启程序）。

**预期结果**：证实 `run` 回调是「一次性启动钩子」，后续所有代码都由事件驱动。首次编译需要构建整个依赖树，耗时几分钟属正常现象。（显示效果依赖本机 GPU 与窗口系统，如遇环境问题待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：`Application` 和 `App` 是什么关系？为什么需要两个类型？

**答案**：`Application` 是 `Rc<AppCell>` 的薄包装（[src/app.rs:L143-L145](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L143-L145)），代表「配置期、尚未启动」的应用，只在 `main` 里短暂出现；`App` 是真正的全局状态容器，启动后以 `&mut App` 的形式在各类回调中传递。分开是为了让「配置」和「运行期使用」有不同的 API 面：配置期用 `with_assets`、`run` 等方法，运行期用 `open_window`、`new` 等方法。

**练习 2**：如果把 `Application::run` 的回调改成在启动前调用，会出什么问题？

**答案**：回调里立刻要 `cx.open_window(...)`、读显示器信息（`Bounds::centered` 内部会查 `cx.primary_display()`），这些都依赖平台已完成初始化、事件循环已就绪。GPUI 的实现是把回调交给 `platform.run`，由平台在「fully launched」后触发（[src/app.rs:L224-L236](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L224-L236)），保证回调拿到的 `&mut App` 一定可用。

**练习 3**：为什么 `App::new_app` 要断言主线程？

**答案**：见 [src/app.rs:L776-L779](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L776-L779)：`assert!(background_executor.is_main_thread(), "must construct App on main thread")`。因为平台窗口系统（以及 GPUI 的 `RefCell` 借用模型）都假设前台状态单线程访问；在非主线程构造会在运行期直接 panic。

### 4.2 App::open_window 与 WindowOptions：窗口从无到有

#### 4.2.1 概念说明

窗口（window）是操作系统管理的资源：位置、尺寸、标题栏、焦点都属于平台。GPUI 用一个纯数据结构 `WindowOptions` 描述「想要一个什么样的窗口」，再由 `App::open_window` 把它连同「根视图工厂闭包」一起变成真实窗口。

三个类型各司其职：

- **`WindowOptions`**：约 20 个字段的配置结构（[src/platform.rs:L1796-L1867](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/platform.rs#L1796-L1867)），实现了 `Default`，所以示例能用 `..Default::default()` 只覆盖关心的字段。
- **`WindowBounds`**：窗口打开时的状态，三选一——`Windowed(bounds)`（普通窗口）、`Maximized`、`Fullscreen`（[src/platform.rs:L1934-L1945](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/platform.rs#L1934-L1945)）。
- **`Bounds<Pixels>`**：原点 + 尺寸的矩形。`Bounds::centered(None, size, cx)` 会在指定显示器（`None` 表示主显示器）上算出居中矩形（[src/geometry.rs:L738-L751](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/geometry.rs#L738-L751)）。

`open_window` 的签名值得逐字读：它是泛型函数 `open_window<V: 'static + Render>`——**窗口的根必须是一个实现了 `Render` 的实体**，这是窗口与视图世界之间的约定。

#### 4.2.2 核心流程

`open_window` 内部步骤（对应源码 [src/app.rs:L1233-L1266](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1233-L1266)）：

```text
cx.open_window(options, build_root_view)
 1. 在 windows: SlotMap 中预留一个 WindowId
 2. Window::new(handle, options, cx)   → 构造 GPUI 侧的 Window 状态
 3. 把 id 压入 window_update_stack     → 让后续 cx 知道"当前正在更新哪个窗口"
 4. 调用你的闭包 build_root_view(&mut window, cx) → 返回 Entity<V>
 5. window.root.replace(root_view)     → 该实体成为窗口的根视图
 6. window.draw(cx)                    → 返回前先绘制至少一帧
 7. 注册 window_handles / windows      → 窗口正式"上线"
 8. 返回 anyhow::Result<WindowHandle<V>>
```

其中第 6 步有一段很能体现工程细节的注释：Windows 平台上经常「输掉与 on_request_frame 的竞速」，拿到一个从未渲染过的窗口会导致 `DispatchTree::root_node_id` 断言失败，所以干脆同步先画一帧。

最后 hello_world 调用的 `cx.activate(true)` 只是「把应用带到前台」（[src/app.rs:L1268-L1271](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1268-L1271)），直接转发给 `platform.activate`。

#### 4.2.3 源码精读

**① hello_world 中的调用现场。**

> [examples/hello_world.rs:L93-L108](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/hello_world.rs#L93-L108)
> 逐行做四件事：`Bounds::centered(None, size(px(500.), px(500.0)), cx)` 算出 500×500 的居中矩形；构造 `WindowOptions` 只设置 `window_bounds`；`open_window` 的第二个参数是根视图工厂闭包 `|_, cx| cx.new(|_| HelloWorld { text: "World".into() })`；最后 `cx.activate(true)`。

**② `WindowOptions` 的常用字段。**

> [src/platform.rs:L1796-L1814](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/platform.rs#L1796-L1814)
> 依次是 `window_bounds`（位置与状态）、`titlebar`（标题栏配置）、`focus`（创建时是否聚焦）、`show`（创建时是否显示）、`kind`（窗口种类）……其余字段如 `is_resizable`、`window_min_size`、`window_decorations`（X11/Wayland 客户端或服务端装饰）等见 [src/platform.rs:L1815-L1867](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/platform.rs#L1815-L1867)。示例用 `..Default::default()` 跳过了全部其余字段。

**③ 居中的两条捷径。**

> [src/geometry.rs:L738-L751](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/geometry.rs#L738-L751)
> `Bounds::centered` 先用 `display_id` 找目标显示器、找不到就退回主显示器，再以显示器中心为锚点生成矩形；连显示器都拿不到时退化为原点 (0,0)。

> [src/platform.rs:L1963-L1966](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/platform.rs#L1963-L1966)
> `WindowBounds::centered(size, cx)` 是更短的写法，等价于 `WindowBounds::Windowed(Bounds::centered(None, size, cx))`——hello_world 里两步并一步的展开版本。

**④ `open_window` 全文。**

> [src/app.rs:L1233-L1266](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1233-L1266)
> 签名 `pub fn open_window<V: 'static + Render>(&mut self, options: WindowOptions, build_root_view: impl FnOnce(&mut Window, &mut App) -> Entity<V>) -> anyhow::Result<WindowHandle<V>>`。注意闭包拿到的是 `(&mut Window, &mut App)` 两个参数——hello_world 用 `|_, cx|` 忽略了 `Window`。函数体严格按 4.2.2 的八步执行；`Window::new` 失败时会回滚刚预留的窗口槽位并返回 `Err`。

#### 4.2.4 代码实践

**实践目标**：通过修改 `WindowOptions` 亲眼验证「窗口的位置尺寸只是数据」。

**操作步骤**（示例代码，可直接改在 `examples/hello_world.rs`）：

1. 把 500×500 改为 800×600：

   ```rust
   let bounds = Bounds::centered(None, size(px(800.), px(600.0)), cx);
   ```

2. 用快捷构造替换两行：

   ```rust
   let bounds = WindowBounds::centered(size(px(800.), px(600.0)), cx).get_bounds();
   ```

3. 在 `WindowOptions` 里再显式加一个字段试验（`..Default::default()` 保留）：

   ```rust
   WindowOptions {
       window_bounds: Some(WindowBounds::Windowed(bounds)),
       focus: false,
       ..Default::default()
   }
   ```

4. 每改一步就重新 `cargo run -p gpui --example hello_world`。

**需要观察的现象**：窗口尺寸明显变化（步骤 1）；步骤 2 与步骤 1 效果一致；步骤 3 中窗口打开时不抢焦点（在其他应用里点击运行时，新窗口不自动置顶获得键盘焦点——具体行为随窗口管理器有差异）。

**预期结果**：确认 `WindowOptions` 是纯配置数据、`Bounds`/`WindowBounds` 只是几何描述。Linux 上部分 WM 会忽略程序请求的窗口位置，属平台差异，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`open_window` 的泛型约束 `V: 'static + Render` 说明了什么？

**答案**：窗口必须有一个「根视图」，而视图在 GPUI 里的定义就是「实现了 `Render` 的实体」（见 4.3.1）。`'static` 表示该类型不能借用短生命周期数据——因为实体要跨帧存活，这正是「保留模式」那一半。

**练习 2**：为什么 `open_window` 返回 `anyhow::Result<WindowHandle<V>>` 而不是直接返回句柄？

**答案**：创建窗口是平台操作，可能失败（例如没有可用的图形环境）。源码中 `Window::new` 返回 `Err` 时会 `cx.windows.remove(id)` 回滚预留的槽位并把错误向上传（[src/app.rs:L1260-L1263](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1260-L1263)）。hello_world 用 `.unwrap()` 是示例的偷懒写法，真实应用应当处理。

**练习 3**：`cx.activate(true)` 的参数是什么意思？

**答案**：`ignoring_other_apps`——是否无视其他应用强行激活。查看 [src/app.rs:L1269-L1271](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1269-L1271) 可见它只是转调 `platform.activate(ignoring_other_apps)`，语义沿袭 macOS AppKit 的同名概念。

### 4.3 Render 与根视图：实体如何变成界面

#### 4.3.1 概念说明

hello_world 的界面来自一个只有两个字段的结构体：

> [examples/hello_world.rs:L9-L11](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/hello_world.rs#L9-L11)
> `struct HelloWorld { text: SharedString }`——`text` 是「保留」下来的应用状态：窗口重绘多少次，它都在。`SharedString` 是 `&'static str` 或 `Arc<str>` 的免拷贝封装。

「视图」在 GPUI 中不是独立概念，而是一个 trait 约定：

> [src/element.rs:L161-L166](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/element.rs#L161-L166)
> `pub trait Render: 'static + Sized { fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement; }`——文档注释直译：「这是区分『视图』与其他实体的 trait。视图就是实现了 `Render` 的 `Entity`」。

于是链条闭合了：`cx.new(|_| HelloWorld { .. })` 创建实体并返回 `Entity<HelloWorld>`；因为 `HelloWorld: Render`，它可以充当窗口根视图；每次需要重绘时，GPUI 调用 `render(&mut self, ..)`，用**当时的** `self.text` 重新生成元素树。「改状态 → 下一帧重新 render」就是 GPUI 声明式 UI 的全部秘密（如何触发重绘的 `cx.notify()` 留到 u2-l3）。

#### 4.3.2 核心流程

从 `cx.new` 到屏幕像素：

```text
cx.new(|_| HelloWorld { text: "World".into() })
  │  ① entities.reserve()          在 EntityMap 预留槽位
  │  ② build_entity(&mut Context)  运行你的闭包构造 HelloWorld
  │  ③ push Effect::EntityCreated  记录"实体已创建"效果
  │  ④ entities.insert(slot, ..)   实体归 App 所有，返回 Entity<T> 句柄
  ▼
Entity<HelloWorld> 作为窗口根视图
  ▼
平台请求一帧 → Window::draw → 调根视图的 render(&mut self, window, cx)
  ▼
div().flex()... .child(format!("Hello, {}!", self.text))
  ▼
元素树经布局(layout) → 预绘制(prepaint) → 绘制(paint) 三阶段变成 GPU 指令
```

三阶段管线的内部机制属于 u4 的主题，本讲只需建立「`render()` 每帧重新执行、`div` 链描述的是『这一帧长什么样』」的直觉。

#### 4.3.3 源码精读

**① `cx.new` 的真身。**

> [src/gpui.rs:L170-L178](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L170-L178)
> `AppContext` trait 声明了 `fn new<T: 'static>(&mut self, build_entity: impl FnOnce(&mut Context<T>) -> T) -> Entity<T>`，并附一段 `#[expect]` 注解说明「`App::new` 是创建实体的惯例方法」——这就是所有上下文（`App`、`Context<T>`、`AsyncApp`……）都叫 `cx.new` 的原因：它们都实现了这个 trait。

> [src/app.rs:L2714-L2733](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L2714-L2733)
> `impl AppContext for App` 的 `new` 恰好四步：`entities.reserve()` → 用 `Context::new_context` 构造实体 → 推送 `Effect::EntityCreated` → `entities.insert`。注意实体本身被**移入** EntityMap——从此「App 拥有一切实体，`Entity<T>` 只是句柄」，这是 u2-l2 的核心预告。

**② `render()`：一棵用链式调用写出的元素树。**

> [examples/hello_world.rs:L13-L28](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/hello_world.rs#L13-L28)
> 外层 `div()`：`.flex().flex_col().gap_3()` 声明「纵向 flex 布局、子项间距 3 号」；`.bg(rgb(0x505050)).size(px(500.0)).shadow_lg().border_1().border_color(rgb(0x0000ff))` 设置背景、500×500 尺寸、大阴影、蓝色 1px 边框；`.justify_center().items_center()` 让子项居中；`.text_xl().text_color(rgb(0xffffff))` 设置文字字号与颜色；第一个 `.child(format!("Hello, {}!", self.text))` 把状态拼进界面。

> [examples/hello_world.rs:L29-L49](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/hello_world.rs#L29-L49)
> 第二个 child 又是一个 `div()`：横向 flex 排开一排 `size_8()` 的彩色小方块（红、绿……），每块都有虚线边框与圆角。`child` 可以无限嵌套——元素树就是这样「长」出来的。

这些方法名与 Tailwind CSS 高度同构（`gap_3` ↔ `gap-3`，`text_xl` ↔ `text-xl`），u1-l1 说过这是刻意设计。它们分别来自 `Styled`、`ParentElement` 等 trait，所以文件顶部需要：

> [examples/hello_world.rs:L3-L7](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/hello_world.rs#L3-L7)
> `use gpui::{... div, px, rgb, size, ...}` 显式导入函数与类型，`use gpui::prelude::*` 导入 trait。

> [src/prelude.rs:L1-L8](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/prelude.rs#L1-L8)
> prelude 一共导出十几个 trait：`AppContext`、`Context`、`Element`、`InteractiveElement`、`IntoElement`、`ParentElement`、`Refineable`、`Render`、`RenderOnce`、`StatefulInteractiveElement`、`Styled`、`StyledImage`、`VisualContext`、`FluentBuilder` 等。删掉这行 `use` 会让所有 `.flex()`、`.child()` 报「找不到方法」——这是新手最常踩的坑。

**③ 字符串为什么能当 child？**

> [src/elements/text.rs:L312](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/elements/text.rs#L312) 与 [src/elements/text.rs:L378](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/elements/text.rs#L378)
> gpui 为 `String` 和 `SharedString` 都实现了 `IntoElement`，所以 `format!(...)` 的产物可以直接塞给 `.child(...)`，框架会把它包成文本元素。

**④ 创建根视图的闭包。**

> [examples/hello_world.rs:L100-L104](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/hello_world.rs#L100-L104)
> `|_, cx| { cx.new(|_| HelloWorld { text: "World".into() }) }`——外层闭包是 `open_window` 要求的 `build_root_view(&mut Window, &mut App)`；内层闭包是 `cx.new` 要求的实体构造函数（拿到 `&mut Context<HelloWorld>`，本例用 `_` 忽略）。`"World".into()` 把 `&str` 转成 `SharedString`。返回值 `Entity<HelloWorld>` 随后被 `open_window` 塞进 `window.root`。

#### 4.3.4 代码实践

**实践目标**：验证「界面文字来自实体字段」——改状态即改界面，且无需触碰任何绘制代码。

**操作步骤**（示例代码）：

1. 修改 [examples/hello_world.rs:L9-L11](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/hello_world.rs#L9-L11) 的结构体，增加一个字段：

   ```rust
   struct HelloWorld {
       text: SharedString,
       subtitle: SharedString,
   }
   ```

2. 在 `render()` 的外层 div 上追加第二个文本 child（放在 `format!` 那行之后）：

   ```rust
   .child(format!("Hello, {}!", self.text))
   .child(self.subtitle.clone())
   ```

3. 修改构造处（[examples/hello_world.rs:L101-L103](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/hello_world.rs#L101-L103)）：

   ```rust
   cx.new(|_| HelloWorld {
       text: "World".into(),
       subtitle: "我的第一个 GPUI 视图".into(),
   })
   ```

4. 重新运行示例。

**需要观察的现象**：界面上出现两行文字（外层 div 本来就是 `flex_col`，新增 child 自动排到下一行）；把 `subtitle` 的值改成任意字符串再运行，文字随之变化。

**预期结果**：`render()` 完全没变，只有「状态」变了，界面就变了——这正是「声明式 UI」的含义：你声明「界面是状态的函数」，框架负责重绘。本实践为综合实践（4.4 之后的第 5 节）做好了铺垫。

#### 4.3.5 小练习与答案

**练习 1**：`Render::render` 为什么拿 `&mut self` 而不是 `&self`？

**答案**：render 允许在渲染期间修改实体状态（例如惰性计算并缓存布局数据），且返回值依赖当帧状态。配合 `&mut Context<Self>` 还能在 render 中读取全局、订阅其他实体等。见 [src/element.rs:L163-L166](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/element.rs#L163-L166)。

**练习 2**：`Entity<HelloWorld>` 和 `HelloWorld` 是什么关系？

**答案**：`HelloWorld` 是数据本体，被存进 `App` 的 `EntityMap`；`Entity<HelloWorld>` 是指向它的带类型句柄（类似 `Rc` 但访问必须经由上下文）。创建过程见 [src/app.rs:L2719-L2733](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L2719-L2733)：本体 insert 进 map，句柄返回给你。详细的所有权模型是 u2-l2 的主题。

**练习 3**：如果把 `render()` 里两个 `.child(...)` 的顺序对调，会发生什么？

**答案**：外层 div 是 `flex_col`（纵向排列），child 顺序即绘制顺序，所以两行文字上下互换；色块那行会排到标题文字上方。这验证了「元素树的结构完全由 render 代码描述」。

## 5. 综合实践

把三个模块串成一个完整任务：**制作你自己的第一个 GPUI 示例**。

### 5.1 任务描述

以 hello_world 为底板，新建示例 `my_first_app`：窗口内是一个「两行布局」的卡片——第一行是可配置的标题文字，第二行是副标题文字，下方保留一排彩色小方块；两行文字都必须来自实体字段。

### 5.2 操作步骤

1. **复制底板**：

   ```sh
   cp crates/gpui/examples/hello_world.rs crates/gpui/examples/my_first_app.rs
   ```

   cargo 会自动发现 `examples/` 下的新文件（[Cargo.toml:L1-L17](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L1-L17) 的 `[package]` 段未关闭 autoexamples；现有示例虽在 [Cargo.toml:L177-L179](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L177-L179) 等处显式登记，但那只是显式声明，不阻碍自动发现）。如果不想新建文件，直接修改 `hello_world.rs` 也可以完成本任务。

2. **改造状态**（示例代码）：

   ```rust
   struct MyFirstApp {
       title: SharedString,      // 第一行：标题
       subtitle: SharedString,   // 第二行：副标题
   }
   ```

3. **改造 render**：外层 div 保持 `.flex().flex_col()`，三个 child 依次为：

   ```rust
   .child(self.title.clone())     // 大字号：.text_xl() 已在外层设置
   .child(self.subtitle.clone())  // 可对外层再加 .gap_2() 调整行距
   .child(/* 原来的色块行 div 原样保留 */)
   ```

   进阶：把标题和副标题分别包进各自的 `div()`，用 `.text_2xl()` 与 `.text_sm().text_color(rgb(0xc0c0c0))` 区分层级；若还想加粗标题，`Styled` trait 提供的是 `font_weight(FontWeight::BOLD)`（见 [src/styled.rs:L522](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/styled.rs#L522)），并没有 `font_bold` 这样的快捷方法。

4. **改构造闭包与类型名**：`HelloWorld` → `MyFirstApp`，字段赋上你自己的值。

5. **运行**：

   ```sh
   cargo run -p gpui --example my_first_app
   ```

### 5.3 检查清单

- [ ] 窗口能打开且尺寸仍是 500×500（或你改过的值）。
- [ ] 界面呈现明显的两行文字 + 一行色块，纵向排列。
- [ ] 修改任一字段字符串后重新运行，对应文字变化，而 `render()` 结构代码无需改动。
- [ ] 终端无 panic；关闭窗口后进程退出。

### 5.4 预期结果与观察点

运行后你会看到一个居中的深灰卡片：两行文字在上（子项因 `.justify_center().items_center()` 而居中），六个彩色方块在下。这个练习覆盖了本讲全部三个模块：你在 `run` 回调里工作（4.1）、经过 `open_window` + `WindowOptions` 创建窗口（4.2）、用 `impl Render` + `div` 链描述界面并让内容来自实体字段（4.3）。渲染效果与 GPU/窗口系统相关，如遇无法打开窗口的情况待本地验证（Linux 无头环境下需要 Wayland/X11 会话）。

## 6. 本讲小结

- 运行 GPUI 示例的标准命令是 `cargo run -p gpui --example <name>`，在 Zed 仓库根目录执行。
- 启动四步曲：`application().run(|cx| ...)`（进入平台事件循环，回调启动后执行一次）→ `cx.open_window(options, |_, cx| ...)`（创建窗口）→ `cx.new(|_| View { .. })`（创建根视图实体）→ `impl Render`（声明界面）。
- `Application` 是 `Rc<AppCell>` 的配置期包装；`App` 是全局状态容器（实体表、窗口表、执行器……）；`AppCell = RefCell<App>` 带来的单借用约束贯穿整个框架。
- `open_window` 泛型于 `V: Render`，内部完成窗口槽位预留、根视图装配，并在返回前先绘制一帧。
- `WindowOptions` 是纯配置数据，`Bounds::centered` / `WindowBounds::centered` 负责几何计算，真实创建交给平台层。
- 「视图 = 实现了 `Render` 的实体」：`render()` 每帧重新执行，元素树是状态的函数——这就是 GPUI 声明式 UI 的核心。

## 7. 下一步学习建议

- **下一讲 u1-l3（gpui crate 目录结构导览）**：本讲我们反复在 `app.rs`、`element.rs`、`platform.rs`、`geometry.rs` 之间跳转；下一讲会系统地走一遍 `src/` 目录，讲清 `gpui.rs` 如何用 `mod` + `pub use` 组织 60 多个模块，让你能独立定位任何公开类型的定义文件。
- **提前预读两段源码**（各 100 行以内）：[src/gpui.rs:L170-L245](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L170-L245) 的 `AppContext` trait（本讲只用了 `new`，还有 `reserve_entity`、`update_entity`、`read_entity` 等），以及 [examples/README.md:L9-L17](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/README.md#L9-L17) 推荐的 `input`、`uniform_list`、`testing` 三个入门示例。
- **带着问题进入 u2**：本讲留下的两个钩子——「实体归谁所有、如何触发重绘（`cx.notify`）」将在 u2-l2（Entity 与所有权模型）和 u2-l3（Context 家族）中逐一回答。
