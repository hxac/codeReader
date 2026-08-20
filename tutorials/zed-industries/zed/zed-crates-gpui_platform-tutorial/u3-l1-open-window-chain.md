# 窗口创建主链路：WindowOptions、WindowParams 与 Platform::open_window

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立画出从 `App::open_window` 到 `Platform::open_window` 的完整调用链，并说出链上每一层的职责。
2. 说清楚应用层的 `WindowOptions` 与平台层的 `WindowParams` 这两个「长得像但不一样」的结构各归谁管，以及哪些字段根本不进 `WindowParams`、而是走后置调用。
3. 解释 `AnyWindowHandle`（以及它背后的 `WindowId` + `TypeId`）如何让平台层在完全不知道视图类型 `V` 的情况下持有、引用、回报窗口，从而实现平台层与 GPUI 视图层的解耦。

本讲是 u2-l1（Platform trait 全景）在「窗口」这一组方法上的下钻，也是 u3 后续三讲（PlatformWindow、键盘、光标与拖放）的地基。

## 2. 前置知识

本讲默认你已读过 u1-l2（第一个应用）和 u2-l1（Platform trait 导览）。在此基础上补充三个概念：

- **两层选项结构**：GPUI 把「用户想要的窗口」拆成两层描述。应用层拿 `WindowOptions`（面向 GPUI 自己和业务代码，字段全平台可见），平台层收 `WindowParams`（面向操作系统封装，字段带 `cfg` 痕迹）。中间的翻译只发生在一个地方——`Window::new`。
- **类型擦除句柄（type-erased handle）**：Rust 的泛型在编译后消失。窗口的根视图类型 `V` 只存在于 GPUI 视图层；平台层需要一个不依赖 `V` 的窗口标识。`AnyWindowHandle = WindowId + TypeId` 就是这个标识，它能单向「降级」回具体类型的 `WindowHandle<V>`，反之可用 `From` 直接升级。
- **slotmap**：一个「带稳定 key 的 Vec」容器。插入元素时返回一个 `Key`，删除其他元素不会让旧 key 失效。GPUI 用它存所有窗口，key 即 `WindowId`。窗口在创建完成前就先占好坑位（先插入 `None` 占位），防止窗口创建过程中其他代码引用一个不存在的 id。

另外回顾 u2-l1 的结论：`Platform` 是 gpui 与操作系统之间的契约 trait，平台 crate（gpui_macos / gpui_linux / gpui_windows / gpui_web）是实现方，`gpui_platform` 门面只负责按编译目标挑出实现。本讲会反复用到这张地图。

## 3. 本讲源码地图

所有链接基于当前 HEAD `6e0a0835`。路径相对于 `crates/gpui_platform/`。

| 文件 | 角色 |
| --- | --- |
| [../gpui/src/app.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/app.rs) | 应用上下文。`App::open_window` 是主链路起点；`windows` slotmap 存放所有窗口。 |
| [../gpui/src/window.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/window.rs) | `Window`（窗口状态）、`Window::new`（选项翻译点）、`WindowHandle<V>` / `AnyWindowHandle` 定义。 |
| [../gpui/src/platform.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/platform.rs) | 契约层。`Platform::open_window`、`WindowOptions`、`WindowParams`、`WindowBounds`、`WindowKind`、`TitlebarOptions` 全在这里。 |
| [../gpui_linux/src/linux/platform.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui_linux/src/linux/platform.rs) | `LinuxPlatform`（外壳，实现 `Platform`）与 `LinuxClient`（内层契约，Wayland/X11/headless 三后端实现）。 |
| [../gpui_linux/src/linux/wayland/client.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs) | Wayland 后端的 `open_window` 实现。 |
| [../gpui_linux/src/linux/headless/client.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui_linux/src/linux/headless/client.rs) | headless 后端的 `open_window` 实现。 |
| [../gpui_macos/src/platform.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui_macos/src/platform.rs) | `MacPlatform` 的 `open_window` 实现。 |
| [../gpui/examples/window.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/examples/window.rs) | 官方窗口示例，一堆按钮各以一种 `WindowOptions` 组合开窗，是本讲实践的素材库。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**主链路起点**、**选项翻译**、**平台实现**、**桥接句柄**。

### 4.1 模块一：App::open_window——主链路的起点

#### 4.1.1 概念说明

用户代码里的 `cx.open_window(options, build_root_view)` 是整个窗口创建的入口。它要做四件事：

1. **分配窗口身份**：在 `App` 的 slotmap 里占一个坑，得到 `WindowId`，包成 `WindowHandle<V>`。
2. **构造 GPUI 窗口**：调用 `Window::new`，这一步内部会触达平台层（见 4.2）。
3. **构建根视图**：执行你传入的 `build_root_view` 闭包，把返回的 `Entity<V>` 塞进窗口。
4. **强制绘制第一帧**：在返回句柄前先画一帧，随后把窗口登记进 `windows` 与 `window_handles`。

为什么第 4 步必要？源码注释写得很直白：Windows 平台上经常「输给 `on_request_frame` 的竞速」，返回一个从未渲染过的窗口会在 `DispatchTree::root_node_id` 上断言崩溃。所以 GPUI 用「返回前必画一帧」把三个平台的行为拉齐。

#### 4.1.2 核心流程

```text
用户代码: cx.open_window(WindowOptions, build_root_view)      ← V 是具体视图类型
   │
   ├─ App::open_window                    (app.rs L1248)
   │    ├─ cx.windows.insert(None)        ← slotmap 占位，得 WindowId
   │    ├─ WindowHandle::<V>::new(id)     ← 带 V 的类型化句柄
   │    ├─ Window::new(handle.into(), options, cx)   ← 进入 4.2 的翻译点
   │    │    └─ （内部调用 cx.platform.open_window，见 4.3）
   │    ├─ build_root_view(&mut window, cx)           ← 构建 Entity<V>
   │    ├─ window.draw(cx)               ← 返回前强制绘制第一帧
   │    └─ 登记: window_handles[id] / windows[id] = window
   ▼
返回 WindowHandle<V>（Result）
```

异步上下文中还有一个孪生入口 `AsyncApp::open_window`，它只是加了「应用正在退出时拒绝开窗」的保护，最终仍转发给 `App::open_window`。

#### 4.1.3 源码精读

主入口（返回类型是带类型的 `WindowHandle<V>`）：

[../gpui/src/app.rs:1248-1281](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/app.rs#L1248-L1281)——`App::open_window` 全体：先 `cx.windows.insert(None)` 占坑、`WindowHandle::new(id)` 造句柄，再进 `Window::new`；成功路径里依次执行 `build_root_view`、`appearance_changed` 的 defer、`window.draw(cx)`（注释解释了 Windows 上的竞速问题），最后把窗口装回 slotmap；失败路径则把占位的坑撤掉（`cx.windows.remove(id)`），保证 slotmap 不泄漏。

[../gpui/src/app.rs:696-697](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/app.rs#L696-L697)——两个关键字段：`windows: SlotMap<WindowId, Option<Box<Window>>>`（值先是 `None` 占位、创建成功后 `replace` 成真身）与 `window_handles: FxHashMap<WindowId, AnyWindowHandle>`（id → 擦除类型句柄的反查表）。

[../gpui/src/app/async_context.rs:185-200](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/app/async_context.rs#L185-L200)——`AsyncApp::open_window`：检查 `lock.quitting` 后直接转发给 `App::open_window`。

[../gpui/src/window.rs:2854-2854](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/window.rs#L2854-L2854)——`Window::draw` 的定义处，即上面「强制绘制第一帧」调用的函数本体。

#### 4.1.4 代码实践

**实践目标**：亲眼确认调用链的层与顺序。

**操作步骤**：

1. 打开 [../gpui/examples/window.rs:311-337](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/examples/window.rs#L311-L337)（`run_example`），这是最短的可运行入口：`application().run(...)` 里一次 `cx.open_window`。
2. 用 rust-analyzer 在 `cx.open_window` 上做「Go to Definition」，再依次对 `Window::new`、`cx.platform.open_window` 重复，把跳转路径记下来，和 4.1.2 的流程图对照。
3. （可选）运行示例：在 zed 仓库根目录执行 `cargo run -p gpui --example window`。该示例文件位于 `examples/` 标准路径，可被 cargo 自动发现（`Cargo.toml` 中显式登记的是路径非标准的那些示例），但整仓编译较重，具体耗时与能否直接跑通**待本地验证**。

**需要观察的现象**：示例主窗口出现后，点击窗口内的按钮会再次进入同一条链路（每个按钮开一个子窗口）。

**预期结果**：你手工画出的跳转链与 4.1.2 流程图一致；示例可交互地开出一批子窗口（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `App::open_window` 要先 `cx.windows.insert(None)` 占位，而不是等窗口完全建好再插入？

**答案**：`WindowId` 必须在调用 `Window::new` 之前就存在，因为它要被包进 `AnyWindowHandle` 传给平台层（平台窗口需要记住自己属于哪个 GPUI 窗口）。slotmap 占位保证了「id 已可用」与「窗口实体就绪」两件事解耦；创建失败时再 `remove` 撤坑（app.rs L1276）。

**练习 2**：`build_root_view` 闭包在 `Window::new` 之后、`window.draw` 之前执行，这个顺序意味着闭包里能拿到什么、不能假设什么？

**答案**：能拿到一个已经拥有真实平台窗口（`platform_window` 就绪）的 `&mut Window`，所以闭包里可以用 `window.window_handle()`、注册窗口级观察者（示例里正是这样用 `cx.observe_window_bounds`）；不能假设窗口已经渲染过——第一帧绘制发生在闭包返回之后。

**练习 3**：`App::open_window` 返回 `WindowHandle<V>`，而 `cx.active_window()` 返回 `Option<AnyWindowHandle>`。为什么不统一成一种？

**答案**：`open_window` 的调用方知道自己刚建的窗口根视图是 `V`，返回类型化句柄可直接 `update`/`read` 根视图；`active_window` 回报的是「任意窗口」，编译期不知道类型，只能给擦除版句柄，需要时再 `downcast`（见 4.4）。

### 4.2 模块二：WindowOptions → WindowParams——只在一个函数里发生的翻译

#### 4.2.1 概念说明

`WindowOptions` 是应用层契约：字段对全平台可见，描述「用户想要什么样的窗口」。`WindowParams` 是平台层契约：描述「操作系统封装需要知道什么」。两者高度重叠但**不是复制关系**。把 `WindowOptions` 的 17 个字段分类，只有三种命运：

1. **直接搬运**（改名或原样进 `WindowParams`）：如 `titlebar`、`kind`、`focus`、`show`、`is_movable`、`app_id`、`icon` 等。
2. **降维搬运**：`window_bounds: Option<WindowBounds>`（三态：窗口化/最大化/全屏 + 恢复尺寸）被压平成 `bounds: Bounds<Pixels>`，三态信息丢失后由**后置调用**补回（见下）。
3. **根本不进 `WindowParams`**：`window_background`、`window_decorations`、`inactive_frame_interval`——它们分别通过创建后的 `set_background_appearance` / `request_decorations` 方法调用、或由 `Window` 自己长期持有来生效。

为什么这样设计？因为 `PlatformWindow` 是一个**活的 trait 对象**，除了创建参数，还有一整套可在任意时刻调用的方法。「创建时定的」和「创建后可变的」分开放，避免 `WindowParams` 变成一个万能大杂烩。

#### 4.2.2 核心流程

翻译点在 `Window::new`，伪代码：

```text
解构 WindowOptions
window_bounds = options.window_bounds 或 default_bounds(display_id, cx)
                 （default_bounds: 活动窗口 bounds → 指定显示器 → 主显示器 → 兜底矩形，
                   逐级回退；新窗相对活动窗基准偏移 CASCADE_OFFSET = 25px）
platform_window = cx.platform.open_window(
    handle,
    WindowParams { bounds: window_bounds.get_bounds(), /* 直接搬运的字段… */ },
)?
后置补课:
    platform_window.request_decorations(window_decorations)      // 恢复 window_decorations
    platform_window.set_background_appearance(window_background) // 恢复 window_background
    match window_bounds {
        Fullscreen(_) => platform_window.toggle_fullscreen(),    // 恢复三态
        Maximized(_)  => platform_window.zoom(),
        Windowed(_)   => {}
    }
inactive_frame_interval → 存进 Window，后续帧调度时使用（不经过平台层）
```

关键洞察：`WindowBounds::get_bounds()` 把三态压成一个矩形，随后用两个 `PlatformWindow` 方法把丢失的状态「演」回来。新窗口的级联偏移可写成 \( \text{origin}_{new} = \text{origin}_{active} + 25\,\text{px} \)（沿活动窗口对角方向错开，避免完全遮挡）。

#### 4.2.3 源码精读

[../gpui/src/platform.rs:1826-1897](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/platform.rs#L1826-L1897)——`WindowOptions` 定义。注意几个字段的注释自带平台知识：`app_owns_titlebar_drag`「仅 macOS 生效」、`is_movable = false` 时 macOS 上会同时禁用 Window 菜单的平铺项、`tabbing_identifier` 是 macOS 10.12+ 原生标签页分组。

[../gpui/src/platform.rs:1899-1962](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/platform.rs#L1899-L1962)——`WindowParams` 定义。看两处细节：`bounds` 是压平后的普通矩形；大量字段挂着 `#[cfg_attr(any(target_os = "linux", target_os = "freebsd"), allow(dead_code))]` 之类的标记，说明在某些平台/feature 组合下这些字段根本不会被读——平台层契约允许实现方「收下但不看」。`tabbing_identifier` 则干脆 `#[cfg(target_os = "macos")]` 只在 macOS 存在。

[../gpui/src/platform.rs:1964-1997](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/platform.rs#L1964-L1997)——`WindowBounds` 三态枚举与 `get_bounds()`（压平点）、`WindowBounds::centered`（常用便捷构造）。

[../gpui/src/window.rs:1350-1404](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/window.rs#L1350-L1404)——**本讲最核心的一段**：`Window::new`。L1355-L1378 解构全部选项；L1384 `window_bounds.unwrap_or_else(|| default_bounds(display_id, cx))` 补默认值；L1385-L1404 构造 `WindowParams` 并调用 `cx.platform.open_window(handle, params)`——注意 `handle` 此时已是 `AnyWindowHandle`（`handle.into()` 发生在 app.rs L1256 的调用处），且 `tabbing_identifier` 带着 macOS 的 cfg 传入。

[../gpui/src/window.rs:1284-1305](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/window.rs#L1284-L1305)——`default_bounds` 的回退链开头：先尝试当前活动窗口的 bounds（级联基准），否则按 `display_id` 找显示器、再退到主显示器，并用 `visible_bounds()`（u2-l3 讲过：扣除任务栏/Dock）作为可用区域。

[../gpui/src/window.rs:1429-1437](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/window.rs#L1429-L1437)——「后置补课」三连：`request_decorations`（Linux 上选择客户端/服务端装饰）、`set_background_appearance`、以及按 `WindowBounds` 三态调 `toggle_fullscreen()` / `zoom()`。

[../gpui/src/window.rs:1582-1583](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/window.rs#L1582-L1583)——`inactive_frame_interval` 的归宿：存在 `Window` 里，在请求下一帧时用于「非激活窗口限帧省电」，从未进入 `WindowParams`。

[../gpui/src/platform.rs:2041-2070](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/platform.rs#L2041-L2070)——`WindowKind` 枚举：`Normal` / `PopUp` / `AnchoredPopup(PopupOptions)`（父窗口锚定的原生弹出，平台不支持时会报错回退到应用内 popover）/ `Floating` / `LayerShell`（cfg 门控在 linux+wayland）/ `Dialog`。

[../gpui/src/platform.rs:2029-2039](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/platform.rs#L2029-L2039)——`TitlebarOptions`：标题、是否透明标题栏（macOS/Windows 的自绘标题栏）、红绿灯按钮位置。

#### 4.2.4 代码实践

**实践目标**：验证「直接搬运 / 降维搬运 / 不搬运」三分类。

**操作步骤**：

1. 对照阅读 [../gpui/examples/window.rs:154-169](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/examples/window.rs#L154-L169)（Dialog 按钮：改 `kind`）、[L170-185](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/examples/window.rs#L170-L185)（Custom Titlebar 按钮：`titlebar: None`）、[L186-201](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/examples/window.rs#L186-L201)（Invisible 按钮：`show: false`）、[L202-218](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/examples/window.rs#L202-L218)（Unmovable：`is_movable: false`）。
2. 在自己的练习 crate（沿用 u1-l2 建的那个）里把主窗口的 `window_bounds` 改成 `Some(WindowBounds::Maximized(bounds))`，运行观察。

**需要观察的现象**：`Maximized` 版本启动即铺满屏幕；同时你在 4.2.3 里能预判：`bounds`（恢复尺寸）会传进 `WindowParams`，紧接着 `platform_window.zoom()` 被调用。

**预期结果**：窗口以最大化状态打开，还原后大小等于你给的 `bounds` 尺寸（行为待本地验证；`zoom()` 是 macOS 术语，Linux/Windows 上的对应行为见 u3-l2）。

#### 4.2.5 小练习与答案

**练习 1**：`WindowOptions.window_decorations` 为什么不放进 `WindowParams`，而是创建后调 `request_decorations`？

**答案**：装饰模式（客户端自绘 vs 服务端）在 Wayland/X11 上是**可协商、可变更**的运行期状态，不是一次性创建参数。走 `PlatformWindow` 方法还能承载「平台可以忽略无法满足的请求」的语义（字段文档明确写了 The platform may ignore requests it cannot satisfy），而构造参数失败了只能让整个开窗失败。

**练习 2**：`WindowParams.kind` 在 Linux 上标了 `allow(dead_code)`，但 Wayland 后端明显在读它（见 4.3.3）。矛盾吗？

**答案**：不矛盾。`allow(dead_code)` 是**允许**未被读取而不报警，不是禁止读取。Linux 的三个后端中 headless（以及部分 feature 组合）不读 `kind`，字段级 cfg_attr 为这些组合关掉编译警告；Wayland/X11 后端照常使用。

**练习 3**：如果把 `WindowBounds::Fullscreen(bounds)` 传给 `open_window`，`bounds` 里的尺寸还有意义吗？

**答案**：有。`get_bounds()` 会把它原样传给 `WindowParams.bounds`，字段文档称其为 restore size——全屏/最大化状态下用户「还原」窗口时回到的尺寸；随后 `toggle_fullscreen()` 负责进入全屏态。

### 4.3 模块三：Platform::open_window 与三份平台实现

#### 4.3.1 概念说明

契约只有一行签名：吃 `AnyWindowHandle + WindowParams`，吐 `Box<dyn PlatformWindow>`。注意两点：

- **句柄是「入参」而非「出参」**。窗口 id 由 GPUI 分配（4.1），平台层只是「代管」——把句柄存进自己创建的平台窗口里，之后事件回调、`active_window()` 回报都用它对账。
- **返回的是 trait 对象**。平台窗口的具体类型（`MacWindow`、`WaylandWindow`、`X11Window`、`HeadlessWindow`…）对 GPUI 完全不可见，后续一切交互走 `PlatformWindow` 的方法（u3-l2 的主题）。

#### 4.3.2 核心流程

以 Linux 为例（u1-l4 讲过的两层分发在开窗上的形态）：

```text
cx.platform.open_window(handle, params)
   │  cx.platform 实际是 Rc<dyn Platform> = LinuxPlatform（外壳）
   ▼
LinuxPlatform::open_window            (linux/platform.rs L384)
   │  一行转发: self.inner.open_window(handle, options)
   ▼
LinuxClient::open_window(handle, params)     ← 内层契约 trait (L78)
   ├─ Wayland 后端: 解析 kind→父窗口/popup grab → WaylandWindow::new → 登记 state.windows
   ├─ X11 后端:     找键盘焦点父窗口 → 创建 X 窗口
   └─ headless 后端: HeadlessWindow::new(假显示器) 直接返回
```

macOS 没有内层分发：`MacPlatform::open_window` 直接构造 `MacWindow`。

#### 4.3.3 源码精读

[../gpui/src/platform.rs:162-166](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/platform.rs#L162-L166)——契约本体：`fn open_window(&self, handle: AnyWindowHandle, options: WindowParams) -> anyhow::Result<Box<dyn PlatformWindow>>`，`Platform` trait 中少数返回 `Result` 的方法（开窗可能失败：显示器消失、弹出窗口找不到父窗口等）。

[../gpui/src/platform.rs:141-144](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/platform.rs#L141-L144)——平台层「回传」句柄的两个方法：`active_window() -> Option<AnyWindowHandle>` 与带默认实现的 `window_stack()`。平台层全程只操作擦除类型句柄。

[../gpui_linux/src/linux/platform.rs:384-390](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L384-L390)——`LinuxPlatform::open_window`：外壳的唯一动作是把调用转给 `self.inner`（`LinuxClient` trait 对象）。

[../gpui_linux/src/linux/platform.rs:78-82](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L78-L82)——内层契约 `LinuxClient::open_window` 的签名，与外层几乎同构。

[../gpui_linux/src/linux/wayland/client.rs:982-1012](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L982-L1012)——Wayland 实现前半：按 `params.kind` 分流——`AnchoredPopup` 用 `options.parent`（一个 `AnyWindowHandle`！）在 `state.windows` 里找父窗口并取 popup grab 序列号；其余种类父级取当前键盘焦点窗口。这是「句柄作为对账凭证」的最直接证据。

[../gpui_linux/src/linux/wayland/client.rs:1014-1045](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1014-L1045)——后半：`params.display_id` 换算成 Wayland 协议 output 对象（u2-l3 讲过 Linux 的 `DisplayId` 来源），`WaylandWindow::new` 拿着 handle、globals、GPU 上下文与 params 建窗，最后 `state.windows.insert(surface_id, ...)` 登记到以 surface id 为键的窗口表。

[../gpui_linux/src/linux/headless/client.rs:100-109](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui_linux/src/linux/headless/client.rs#L100-L109)——headless 实现：忽略 handle，直接 `HeadlessWindow::new(params, 假显示器)` 返回。整个开窗链路在无显示环境下依然走完全程，这就是 u1-l2 说的「无头模式下窗口逻辑存在但不上屏」。

[../gpui_linux/src/linux/x11/client.rs:1598-1607](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1598-L1607)——X11 实现开头：同样先解析键盘焦点窗口作为父窗口，再创建 X 窗口。

[../gpui_macos/src/platform.rs:649-678](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui_macos/src/platform.rs#L649-L678)——macOS 实现：两步走——先对 `WindowKind::AnchoredPopup` 做能力拒绝（返回 `PopupNotSupportedError`，让调用方回退到 GPUI 应用内 popover，这正是 `WindowKind` 文档承诺的行为）；再从锁住的状态里捞出执行器与渲染上下文，交给 `MacWindow::open` 构造。对比 Wayland 版本读到的「AnchoredPopup 是一等公民」，同一个 `kind` 字段在两平台的命运截然不同。

#### 4.3.4 代码实践

**实践目标**：确认「同一签名、四种实现」并能在自己的机器上指出哪份代码在被编译。

**操作步骤**：

1. 用 Grep 在四个平台 crate 里分别搜索 `fn open_window`，各记下文件与行号，做成四行对照表。
2. 借用 u1-l4 的结论做一次运行期验证：Linux 上设 `ZED_HEADLESS=1` 跑你的练习 crate，开窗必然落入 headless 分支；macOS/Windows 读者对应检查 `MacPlatform` / Windows 平台的实现位置（Windows 的 `open_window` 在 `gpui_windows` crate 中，本讲未展开，可自行搜索补全表格）。
3. 阅读中留意每个实现拿到 `handle` 后第一件事做什么（Wayland：找父窗口/登记；macOS：透传给 `MacWindow::open`；headless：丢弃）。

**需要观察的现象**：`ZED_HEADLESS=1` 时程序不弹窗但正常退出、无报错（开窗链路完整走完）。

**预期结果**：四平台对照表完成；headless 运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Platform::open_window` 返回 `Box<dyn PlatformWindow>` 而不是泛型 `impl PlatformWindow`？

**答案**：返回 `impl Trait` 要求编译期已知具体类型且调用方只能静态使用；而 GPUI 需要把平台窗口存进 `Window.platform_window` 字段（window.rs L1136）统一调度，且要在运行期按平台选择实现，trait 对象是唯一选择。这也意味着平台窗口方法全部是动态分发——窗口操作频率远低于逐帧绘制内层循环，代价可接受。

**练习 2**：macOS 对 `AnchoredPopup` 返回错误而不是静默降级，这个设计好在哪里？

**答案**：把「能力不支持」显式抛给调用方，调用方（gpui 的 popup 模块）可以捕获后改用应用内 popover 渲染，用户得到一致体验；若静默建成普通窗口，弹出菜单会变成一个乱飞的无父窗口，属于更糟的失败模式。契约注释（WindowKind 文档）预先声明了这一约定。

**练习 3**：Linux 为什么需要 `LinuxClient` 这第二层 trait，而 macOS 不需要？

**答案**：Linux 上「窗口系统」有三个可运行期选择的实现（Wayland/X11/headless，u1-l4 的环境变量探测），需要运行期多态；macOS 只有一个 AppKit，编译期就确定了，多一层 trait 只会增加间接性。

### 4.4 模块四：AnyWindowHandle——平台层与视图层的桥

#### 4.4.1 概念说明

`WindowHandle<V>` = `AnyWindowHandle` + `PhantomData<V>`：**数据上只多了一个编译期类型标记**，运行期尺寸完全相同。`AnyWindowHandle` 本体是 `WindowId`（slotmap key）加 `TypeId`（根视图类型的编译期指纹）。这个设计的精妙之处：

- 平台层只见过 `AnyWindowHandle`，从头到尾不知道 `V` 是谁——解耦。
- `TypeId` 让 `downcast::<V>()` 可以做**运行期类型检查**：拿错类型的句柄去 `update` 会得到错误而非未定义行为。
- 句柄是 `Copy` 的、不持有引用计数——它不维持窗口存活（文档明确说 does not keep the window alive on its own），用之前要接受「窗口可能已关」的 `Result`。

#### 4.4.2 核心流程

一个句柄的生命周期：

```text
cx.windows.insert(None) ─→ WindowId
        │
WindowHandle::<V>::new(id)          ← 打上 V 的 TypeId
        │  .into()  （From impl，丢弃 PhantomData）
        ▼
AnyWindowHandle ──传给──→ Platform::open_window ──存入──→ 平台窗口内部
        │                                              │
        │ ←──────────── active_window() / window_stack() ┘ 平台层回报
        ▼
需要具体类型时: handle.downcast::<V>() → Option<WindowHandle<V>>
```

两条对账通路：① 平台窗口自己存着句柄，事件发生时把「哪个窗口」报回 GPUI；② GPUI 侧 `App.window_handles` 表支持从 id 反查句柄。

#### 4.4.3 源码精读

[../gpui/src/window.rs:6399-6407](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/window.rs#L6399-L6407)——`WindowHandle<V>`：内嵌 `any_handle: AnyWindowHandle`（Deref 到它）加 `PhantomData`，注释说明它不独立维持窗口存活。

[../gpui/src/window.rs:6417-6428](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/window.rs#L6417-L6428)——`WindowHandle::new(id)`：用 `TypeId::of::<V>()` 指纹构造。这就是 app.rs L1255 那一行调用的函数。

[../gpui/src/window.rs:6536-6540](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/window.rs#L6536-L6540)——`From<WindowHandle<V>> for AnyWindowHandle`：免费升级（丢掉类型标记）；`app.rs` L1256 的 `handle.into()` 用的就是它。

[../gpui/src/window.rs:6542-6566](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/window.rs#L6542-L6566)——`AnyWindowHandle` 本体与 `downcast`：`TypeId` 相等才还原出 `WindowHandle<T>`，否则 `None`。窗口操作方法（`update` 等）统一返回 `Result`，把「窗口已关闭」当作正常错误传播——符合项目「不用 unwrap、错误显式处理」的规范。

[../gpui/src/app.rs:1224-1243](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/app.rs#L1224-L1243)——消费侧：`windows()` 从 slotmap+反查表收集所有句柄（文档提示可按根视图类型 downcast 过滤）；`window_stack()` 转发平台的层叠顺序回报（u2-l1 讲过它有默认 `None`）；`active_window()` 直接转发 `platform.active_window()`——平台层产出的正是 `AnyWindowHandle`。

[../gpui/src/window.rs:1131-1136](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/window.rs#L1131-L1136)——`Window` 结构开头：`handle: AnyWindowHandle`、`platform_window: Box<dyn PlatformWindow>` 并排存放——「GPUI 窗口」与「平台窗口」一对一互持对方的身份凭证。

#### 4.4.4 代码实践

**实践目标**：观察句柄的分配与类型擦除。

**操作步骤**（示例代码，加入你自己的练习 crate）：

```rust
// 示例代码：观察 WindowId 与 downcast
let handle = cx
    .open_window(WindowOptions::default(), |_, cx| {
        cx.new(|_| MyRootView {})
    })
    .unwrap();

let any = handle.into();                       // WindowHandle<V> → AnyWindowHandle
println!("window id: {:?}", any.window_id());
println!("all windows: {:?}", cx.windows().len());
let typed_again: Option<WindowHandle<MyRootView>> = any.downcast();
assert!(typed_again.is_some());
let wrong: Option<WindowHandle<OtherView>> = any.downcast(); // 换个类型
println!("wrong type downcast: {:?}", wrong.is_none());      // 预期 true
```

1. 把上述代码放进你的练习 crate（`MyRootView`/`OtherView` 需实现 `Render`）。
2. 连开三个窗口，打印每个 `window_id()`。

**需要观察的现象**：三次 id 输出互不相同且无规律（slotmap key 含版本位）；`wrong` 的 downcast 恒为 `None`。

**预期结果**：断言通过、id 各异（待本地验证——slotmap key 的 Debug 格式依实现而定，观察要点是「互不相同」）。

#### 4.4.5 小练习与答案

**练习 1**：`AnyWindowHandle` 里为什么要存 `TypeId`？只有 `WindowId` 不够吗？

**答案**：只有 id 的话，`downcast::<V>` 无法验证窗口根视图真是 `V`，类型错误的 `update` 会在解引用时才爆炸。`TypeId` 把类型检查提前到句柄转换处，返回 `Option` 让调用方显式处理「拿错类型」——用一点点内存换掉一整类 bug。

**练习 2**：平台层持有 `AnyWindowHandle` 而不是 `WindowId`，多带的 `TypeId` 对平台层有什么用？

**答案**：基本没用——平台层不 downcast。它拿到的是「同一枚硬币」的完整面值而已；真正的受益方是 GPUI 层：平台回报句柄后（如 `active_window`），视图层无需再查表就能直接尝试 downcast，省掉一次 id→类型的反查与失配风险。

**练习 3**：句柄是 `Copy` 且不维持窗口存活，这两点组合下，异步代码里持有一个旧句柄再 `update` 会怎样？

**答案**：窗口若已关闭，slotmap 中该 key 已被移除，`update` 走 `cx.update_window` 路径会返回 `Err`（「window not found」一类），不会 panic——这正是 CLAUDE.md 强调的错误传播风格在句柄 API 上的体现。持有方应当处理这个 `Result`（例如 `log_err` 或静默放弃）。

## 5. 综合实践

**任务：给窗口创建主链路做一次「带日志的全链路追踪」，并标注三个字段的传播路径。**

这是本讲规格里指定的实践，把四个模块串起来。

**第 1 步（准备）**：复制 u1-l2 的练习 crate（或 fork zed 仓库到本地学习分支——日志只加在你自己的学习分支上，绝不提交/不改动上游）。程序体直接借用 [../gpui/examples/window.rs:311-337](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/examples/window.rs#L311-L337) 的 `run_example` 骨架。

**第 2 步（加探针）**：在本地学习分支的四个位置各加一行临时 `eprintln!`（带统一前缀如 `[trace]` 便于过滤）：

| 位置 | 打印内容 |
| --- | --- |
| `App::open_window` 开头（app.rs L1248 函数体第一行） | `id`、`options.window_bounds`、`options.kind`、`options.titlebar` 是否为 None |
| `Window::new` 内、调用 `cx.platform.open_window` 之前（window.rs L1385 前） | 翻译后的 `WindowParams.bounds`、`kind`、`titlebar` |
| `LinuxPlatform::open_window`（或你平台对应实现）转发处 | 进入内层/实现的确认 |
| `Window::new` 的后置补课段（window.rs L1429-L1437） | 是否触发了 `toggle_fullscreen` / `zoom` / 装饰请求 |

**第 3 步（改字段）**：准备三个版本的 `WindowOptions`，分别只改一个字段：① `window_bounds: Some(WindowBounds::Maximized(...))`；② `titlebar: None`；③ `kind: WindowKind::PopUp`（或 `Dialog`）。各运行一次。

**第 4 步（产出）**：整理成两张表：

- **调用链表**：每行 = 一层函数名 + 该层收到的关键参数 + 所属文件与行号（应复现 4.1.2 的链）。
- **字段传播表**：三行（三个字段）× 四列（`WindowOptions` 值 → `WindowParams` 值/是否进入 → 后置调用 → 最终可见效果）。

**验收标准**：字段传播表能清楚显示——`window_bounds` 的三态如何被压平再由 `zoom()` 恢复；`titlebar` 如何原样进入 `WindowParams` 并在平台窗口上生效（自绘标题栏按钮 vs 普通按钮的可见差异）；`kind` 如何决定平台实现里的分流（如 macOS 对 `AnchoredPopup` 的拒绝路径）。

**预期结果**：两张表完整、与本章源码讲解吻合；具体输出（如 `WindowId` 的 Debug 格式、Linux 上 `zoom` 的实际行为）**待本地验证**。完成后 `git checkout` 恢复源码，保持工作区干净。

## 6. 本讲小结

- 窗口创建主链路：`App::open_window` →（分配 `WindowId`、造 `WindowHandle<V>`）→ `Window::new` → `Platform::open_window` → 平台实现返回 `Box<dyn PlatformWindow>` → 构建 `build_root_view` → 强制绘制第一帧 → 登记 slotmap。
- `WindowOptions`（应用层，全平台字段）与 `WindowParams`（平台层，带 cfg 痕迹）的翻译**只发生在 `Window::new` 一处**；字段有三种命运：直接搬运、降维搬运（`WindowBounds` 压平成 `bounds` + 后置 `toggle_fullscreen`/`zoom` 补课）、不搬运（`window_decorations`/`window_background`/`inactive_frame_interval` 走运行期方法或 `Window` 自持）。
- 句柄是**入参不是出参**：GPUI 先分配 id，平台层代管句柄并用于事件对账；`active_window()`/`window_stack()` 回报的也是句柄——平台层全程只见 `AnyWindowHandle`，不见视图类型 `V`。
- `AnyWindowHandle = WindowId + TypeId`，`downcast` 靠 `TypeId` 做运行期类型检查；句柄 `Copy` 且不维持窗口存活，一切操作返回 `Result`。
- Linux 上开窗经历两层分发：`LinuxPlatform`（外壳）→ `LinuxClient`（Wayland/X11/headless 三后端）；macOS 单层直达 `MacWindow::open`，并对不支持的原生弹出（`AnchoredPopup`）显式报错让调用方回退。

## 7. 下一步学习建议

- **下一讲（u3-l2）**：`PlatformWindow trait 详解`——本讲拿到的 `Box<dyn PlatformWindow>` 之后能做什么：标题、尺寸、焦点、全屏、窗口控制区等方法族，以及 raw-window-handle 两个约束 trait。
- **顺路复习**：u2-l1 的八大方法分组（本讲的 `open_window` 属「窗口与显示器」组）；u1-l4 的 Linux 两层分发（本讲在开窗链路上见到了它的具体形态）。
- **源码延伸**：带着「谁消费了我设置的字段」这个问题通读 [../gpui/src/platform.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_platform/../gpui/src/platform.rs) 中 `PlatformWindow` 的完整方法列表（L816 起），你会发现 `WindowOptions` 里那些「不搬运」的字段全部在这里重逢。
