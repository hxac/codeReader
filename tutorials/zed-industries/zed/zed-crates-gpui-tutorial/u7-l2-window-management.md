# 窗口管理：多窗口与 WindowOptions

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 `WindowOptions`、`WindowKind`、`WindowBounds`、`TitlebarOptions` 完整描述一个即将创建的窗口，并知道每个字段落在哪个平台、哪些字段只是「请求」而非「保证」。
2. 说清 `cx.open_window` 从预留 `WindowId` 到返回 `WindowHandle` 的完整流程，以及为什么它在返回前要「先画一帧」。
3. 掌握 `WindowHandle<V>` / `AnyWindowHandle` 的跨窗口读取与更新模式（`update` / `read` / `update_window`），理解窗口句柄与实体句柄的同构与差异。
4. 理解 GPUI 的核心设计——**实体不属于窗口**：同一个实体可以被多个窗口显示，也可以在窗口之间迁移，`_in` 系列回调永远派发到实体的「当前窗口」。
5. 掌握窗口生命周期：`remove_window` 的延迟拆除、`on_window_closed`、`on_window_should_close` 否决、`observe_window_bounds` / `observe_window_activation`，以及「最后一个窗口关闭是否退出」的平台差异。

## 2. 前置知识

本讲是高级单元（u7）的第二讲，默认你已完成以下认知（都来自前置讲义）：

- **u2-l1（应用生命周期）**：`Application::run` 把控制权交给平台事件循环；`App` 是全局状态容器；`QuitMode` 决定最后一个窗口关闭时的行为。本讲会用到 `QuitMode::LastWindowClosed` 与 `QuitMode::Default` 的语义。
- **u2-l2（实体所有权）**：一切实体住在 `App` 的 `EntityMap` 里，`Entity<T>` 只是「EntityId + 类型标签」的句柄。窗口句柄的设计与之同构，本讲反复对比。
- **u3-l1（Render 与视图）**：视图 = 实现了 `Render` 的实体；窗口必须有一个根视图（root view）。
- **u7-l1（Platform 抽象）**：`App` 持有 `Rc<dyn Platform>`，每个窗口持有一个 `Box<dyn PlatformWindow>`。本讲中 `WindowOptions` 最终会被「蒸馏」成平台层的 `WindowParams` 传给 `Platform::open_window`。

再补三个本讲要用的术语：

- **slotmap**：一种「插入即获得稳定键」的容器。`App` 用它存所有窗口，键类型是 `WindowId`；删除槽位后键不会被复用，因此 `WindowId` 在进程内唯一且稳定。
- **restore size（还原尺寸）**：窗口最大化/全屏时，记录的「还原到普通窗口态时应该多大」。`WindowBounds::Maximized(bounds)` 里的 `bounds` 指的就是它，不是当前尺寸。
- **级联偏移（cascade offset）**：不指定位置开窗时，新窗口相对上一个活动窗口右下偏移一小段距离，避免完全遮挡——这是几乎所有桌面系统的默认行为，GPUI 也自己实现了。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/platform.rs` | 定义 `WindowOptions`、`WindowKind`、`WindowBounds`、`TitlebarOptions`、`WindowParams`——全部是纯数据结构，是平台 trait 契约的一部分 |
| `src/window.rs` | `Window` 结构体本身、`Window::new`（消费 `WindowOptions`）、`WindowHandle` / `AnyWindowHandle` / `WindowId`、`remove_window` / `on_window_should_close` 等生命周期方法 |
| `src/app.rs` | `App` 持有的窗口表（`windows` slotmap + `window_handles` 哈希表）、`App::open_window`、`update_window_id`（跨窗口更新的真正实现与窗口拆除逻辑）、`on_window_closed` |
| `src/app/context.rs` | `Context<T>` 侧的窗口观察者注册口：`observe_window_bounds`、`observe_window_activation`、`observe_window_appearance` |
| `src/gpui.rs` | `AppContext` trait 中 `update_window` / `with_window` 的契约（「实体的当前窗口」这一定义就在这里的文档注释里） |
| `examples/window.rs` | `WindowOptions` 的试验台：十来个按钮逐一演示各种配置组合的效果 |
| `examples/window_positioning.rs` | 多显示器定位：遍历 `cx.displays()`，在每块屏幕的九个方位摆窗口 |
| `examples/move_entity_between_windows.rs` | 实体迁移演示：点击按钮把根视图实体搬到新窗口 |
| `examples/on_window_close_quit.rs` | 关窗退出联动：`on_window_closed` + `windows().is_empty()` 判断 |

## 4. 核心概念与源码讲解

### 4.1 cx.open_window：一个窗口的诞生

#### 4.1.1 概念说明

窗口是 **App 级资源**，不是视图级资源。一个 GPUI 应用可以开任意多个窗口，它们全部登记在 `App` 上，与实体表（`EntityMap`）平级。`cx.open_window` 是创建窗口的唯一入口（测试基础设施里另有内部通道），它接收一份纯配置 `WindowOptions` 和一个「构建根视图」的闭包，返回类型化的窗口句柄。

理解创建流程的关键是分清三样东西：

- `WindowOptions`：你写给 GPUI 的**期望**（位置、样式、行为开关）。
- `Window`：GPUI 的**窗口运行时**（元素树、绘制管线、焦点表、dispatch tree），对应 u4-l3 讲过的那 7500 行结构体。
- `Box<dyn PlatformWindow>`：`Window` 内部持有的**操作系统窗口**，`WindowOptions` 里真正需要平台处理的字段最终会传到这里。

#### 4.1.2 核心流程

`App::open_window` 的执行序列：

```text
cx.open_window(options, build_root_view)
  ├─ 1. cx.windows.insert(None)          # 在 slotmap 预留一个空槽，得到 WindowId
  ├─ 2. WindowHandle::new(id)            # 造出类型化句柄（此时窗口还不存在）
  ├─ 3. Window::new(handle, options, cx) # 消费 options：
  │      ├─ window_bounds 为 None → default_bounds() 级联推算
  │      ├─ 蒸馏出 WindowParams → cx.platform.open_window(...) 创建平台窗口
  │      └─ Fullscreen → toggle_fullscreen() / Maximized → zoom()
  ├─ 4. push window_update_stack
  │      build_root_view(&mut Window, &mut App) → Entity<V>   # 你的闭包在这里执行
  │      pop window_update_stack
  ├─ 5. window.root = Some(root_view)    # 根视图挂到窗口
  ├─ 6. defer(appearance_changed)        # 首帧前同步外观
  ├─ 7. window.draw(cx)                  # 立即画一帧（见下文解释）
  ├─ 8. window_handles.insert(id, ...)   # 双表登记，窗口正式可见可寻址
  └─ 返回 Ok(WindowHandle<V>)；第 3 步失败则移除预留槽并返回 Err
```

两个值得咀嚼的细节：

- **步骤 7 的「先画一帧」**不是优化而是正确性修复。源码注释解释：在 Windows 上，`open_window` 返回前经常「跑输」平台的 `on_request_frame` 竞态，导致返回一个从未渲染过的窗口，后续 `DispatchTree::root_node_id` 会在空节点上断言崩溃。所以 GPUI 干脆同步画掉第一帧。
- **步骤 1 的「先插空槽」**让 `WindowId` 在平台窗口创建之前就确定下来——这样 `Window::new` 内部注册的平台回调（尺寸变化、激活变化等）从一开始就能用这个 id 找到窗口。

#### 4.1.3 源码精读

先看 `App` 上的两张表——窗口的「户籍」：

- [src/app.rs:696-697](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app.rs#L696-L697)：`windows: SlotMap<WindowId, Option<Box<Window>>>` 存窗口本体（`Option` 分层用于「更新期间把窗口搬出槽位」，见 4.3），`window_handles: FxHashMap<WindowId, AnyWindowHandle>` 存「id → 带类型信息句柄」的反查表，`cx.windows()` 就是靠它实现的。

然后是 `open_window` 本体：

- [src/app.rs:1248-1281](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app.rs#L1248-L1281)：`App::open_window` 的完整签名与流程。注意 `build_root_view` 的参数是 `(&mut Window, &mut App)`——你的闭包拿到的 `window` 就是刚创建、还没有根视图的窗口运行时，所以可以在这里提前注册窗口级回调（`observe_window_bounds`、初始 `focus_handle().focus(window, cx)` 等都发生在这一步，见示例）。

`WindowOptions` 是如何被消费的：

- [src/window.rs:1350-1404](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L1350-L1404)：`Window::new` 开头把 `WindowOptions` 整个解构，`window_bounds.unwrap_or_else(|| default_bounds(display_id, cx))` 处理「没给位置」的兜底，然后把布局无关之外的字段打包成 `WindowParams` 交给 `cx.platform.open_window`。平台窗口创建成功后，再从平台窗口读回真实状态（`content_size`、`scale_factor`、`appearance`、`sprite_atlas`……）——这体现了「选项是请求，读回的才是事实」。
- [src/window.rs:1433-1437](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L1433-L1437)：`WindowBounds` 三态的附加动作——`Fullscreen` 调 `toggle_fullscreen()`、`Maximized` 调 `zoom()`、`Windowed` 什么都不做（bounds 已在 `WindowParams` 里生效）。

#### 4.1.4 代码实践

**实践目标**：亲手跑通「开窗 → 观察」，并验证 `cx.open_window` 返回前确实完成了首次绘制与登记。

**操作步骤**：

1. 运行官方示例：
   ```bash
   cargo run -p gpui --example window
   ```
2. 拖动、缩放主窗口，观察终端输出。这个示例在 `build_root_view` 闭包里注册了 bounds 观察者（[examples/window.rs:320-325](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/window.rs#L320-L325)），每次尺寸变化都会打印 `Window bounds changed: ...`。
3. 复制一份 `examples/window.rs` 为 `examples/my_windows.rs`，在 `run_example` 的 `cx.open_window(...).unwrap();` 之后追加第二个窗口与一行打印（示例代码）：
   ```rust
   cx.open_window(
       WindowOptions {
           window_bounds: Some(WindowBounds::Windowed(
               Bounds::centered(None, size(px(300.0), px(200.0)), cx),
           )),
           ..Default::default()
       },
       |_, cx| cx.new(|_| WindowDemo {}),
   )
   .unwrap();
   println!("open windows: {:?}", cx.windows());
   ```
   运行 `cargo run -p gpui --example my_windows`（`Cargo.toml` 未关闭 `autoexamples`，新示例文件会被自动发现）。

**需要观察的现象**：第二个窗口出现；终端打印出包含两个 `AnyWindowHandle` 的列表。

**预期结果**：`cx.windows()` 返回长度为 2 的 `Vec`；关闭其中一个窗口后再从代码里查询（例如把它挂到某个按钮回调里打印），列表长度变为 1。

**待本地验证**：`WindowDemo` 是示例里的私有结构体，复制文件后可直接复用；若编译报「cannot borrow」类错误，确认两个 `open_window` 都在 `application().run` 的回调内顺序调用。

#### 4.1.5 小练习与答案

**练习 1**：`App::open_window` 为什么要在返回前调用 `window.draw(cx)` 画一帧？去掉这一步可能在哪个平台上出什么问题？

**答案**：在 Windows 上，`open_window` 返回后经常抢不到平台的 `on_request_frame` 回调时机，会返回一个从未渲染过的窗口；这种窗口的 `DispatchTree` 是空的，后续派发动作时 `DispatchTree::root_node_id` 会在空节点上触发断言崩溃。源码注释原文将其描述为「we quite frequently lose the race」。同步画一帧保证返回的窗口至少完成过一次完整渲染。

**练习 2**：`App` 为什么用 `SlotMap<WindowId, Option<Box<Window>>>` 而不是 `HashMap` + 计数器？

**答案**：slotmap 的键在删除后不复用，`WindowId` 全进程稳定唯一；同时 `Option<Box<Window>>` 的外层 `Option` 允许在「更新某个窗口」期间把窗口从槽位里整体搬出（置 `None`），既防止同窗口嵌套更新，又能把 `Box<Window>` 按值交给更新闭包——这是 4.3 节 `update_window_id` 的核心技巧。

### 4.2 WindowOptions 与 WindowKind：窗口的全部可配置项

#### 4.2.1 概念说明

`WindowOptions` 是一个**纯数据结构**：没有任何方法，只有字段，实现了 `Debug` 和 `Default`。它的定位是「创建窗口时的一次性描述」——创建之后想改窗口属性，要走 `Window` 上的运行时方法（如 `window.resize`、`request_decorations`），而不是改这份配置。

按语义把字段分成六组更好记：

| 分组 | 字段 | 说明 |
| --- | --- | --- |
| 位置与状态 | `window_bounds`、`display_id` | 开窗位置/状态（窗口态、最大化、全屏）与目标显示器 |
| 标题栏 | `titlebar`（`Option<TitlebarOptions>`） | 标题文字、是否隐藏系统标题栏、macOS 红绿灯按钮位置；`None` 表示完全不要标题栏 |
| 行为开关 | `focus`、`show`、`is_movable`、`is_resizable`、`is_minimizable`、`app_owns_titlebar_drag` | 创建时是否聚焦/显示；用户能否拖动、缩放、最小化；macOS 上自绘标题栏时是否由应用接管拖拽 |
| 种类 | `kind`（`WindowKind`） | 普通/弹出/浮动/对话框等平台语义（见下） |
| 外观 | `window_background`、`window_decorations`、`icon`、`window_min_size`、`inactive_frame_interval` | 背景透明度、X11/Wayland 客户端/服务端装饰、图标、最小尺寸、非激活时的帧率节流 |
| 平台标识 | `app_id`、`tabbing_identifier` | 桌面环境分组标识；macOS 原生标签页分组名 |

几个容易踩的点：

- `show: false` 创建的是「隐形窗口」——窗口存在、已渲染，只是不显示。适合「先建好、准备好内容再亮出来」的场景。
- `is_movable: false` 在 macOS 上等价于设置 `NSWindow.isMovable`，同时会禁用 Window 菜单里的平铺项；但**程序化移动仍然允许**（注释明说）。
- `window_decorations`、`app_owns_titlebar_drag`、`tabbing_identifier` 等字段只在特定平台有效，文档注释与 `WindowParams` 上成片的 `cfg_attr(..., allow(dead_code))` 标注了这一点——读代码时不要假设所有字段在所有平台都落地。

`WindowKind` 的五个（+一个条件编译）变体：

| 变体 | 语义 |
| --- | --- |
| `Normal` | 普通应用窗口（默认值） |
| `PopUp` | 悬在所有窗口之上的弹出窗口，源码注释特意加了「use sparingly!」——用于 alert/弹出层 |
| `AnchoredPopup(PopupOptions)` | 相对父窗口定位的原生弹出（菜单、组合框、右键菜单、tooltip）。与 `PopUp` 不同，它的位置由父窗口决定，`window_bounds` 的 origin 会被忽略；不支持原生实现的平台会返回 `PopupNotSupportedError` |
| `Floating` | 悬浮在父窗口之上的浮动窗口 |
| `LayerShell(LayerShellOptions)` | Wayland LayerShell 窗口（dock、通知中心、壁纸类），仅 `target_os = "linux"` 且 `feature = "wayland"` 时存在 |
| `Dialog` | 模态对话框：悬于父窗口之上并**阻塞与父窗口的交互**，直到关闭 |

#### 4.2.2 核心流程

**位置的三种给法**：

1. `window_bounds: Some(WindowBounds::Windowed(bounds))` —— 精确指定（bounds 是屏幕坐标）。
2. `Some(WindowBounds::Maximized(b))` / `Some(WindowBounds::Fullscreen(b))` —— 以最大化/全屏态打开，`b` 是**还原尺寸**（restore size），不是当前尺寸。`Window::new` 会额外调用 `zoom()` / `toggle_fullscreen()`。
3. `window_bounds: None` —— 兜底级联：取当前活动窗口的 bounds（无活动窗口则取目标显示器的 `default_bounds()`），沿对角线偏移 `CASCADE_OFFSET = 25px`，若越出显示器可见区域（排除任务栏/dock 的 `visible_bounds`）则贴边收拢。想要「跟随系统默认」就传 `None`，想要「叠在当前窗口旁边」也是传 `None`。

用伪代码概括 `default_bounds`：

```text
base = 当前活动窗口的 bounds（保持同样的 Window/Max/Full 变体）
     ?? 目标显示器（display_id ?? 主显示器）的 default_bounds()，变体取 Windowed
proposed = base.origin + (25px, 25px)，尺寸沿用 base.size
若 proposed 超出显示器可见区域的右/下边界 → 对应轴回贴到显示器边缘
返回 同变体的 WindowBounds(final_bounds)
```

#### 4.2.3 源码精读

- [src/platform.rs:1826-1897](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform.rs#L1826-L1897)：`WindowOptions` 全部字段及文档注释。这是「创建窗口能配置什么」的权威清单。
- [src/platform.rs:1999-2025](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform.rs#L1999-L2025)：`Default` 实现。默认值是：有标题栏、聚焦、显示、`Normal`、可移动/可缩放/可最小化、非激活帧间隔约 33ms（即非激活窗口动画帧率降到 ~30fps）。示例里大量出现的 `..Default::default()` 就是在这组默认值上做点状覆盖。
- [src/platform.rs:2041-2070](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform.rs#L2041-L2070)：`WindowKind` 枚举。注意 `AnchoredPopup` 与 `LayerShell` 两个新成员都带配置参数、且分别有「平台可拒绝」与「feature 门控」的限制。
- [src/platform.rs:1964-1997](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform.rs#L1964-L1997)：`WindowBounds` 三态、`get_bounds()` 与便捷构造器 `WindowBounds::centered(size, cx)`（在主显示器居中）。
- [src/platform.rs:2027-2039](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/platform.rs#L2027-L2039)：`TitlebarOptions` 三个字段。`appears_transparent: true` 是自绘标题栏的标准搭配（Linux 上改看 `window_decorations`）。
- [src/window.rs:1284-1347](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L1284-L1347)：`default_bounds` 级联算法本体。注意源码里留着一条自认的 TODO/BUG 注释：在当前活动窗口的更新栈上开窗时，活动窗口查询会错误地回落到 `None`——读代码时能看到框架作者自己标注的边角。
- [src/window.rs:1380-1404](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L1380-L1404)：`None` 兜底与 `WindowParams` 蒸馏的衔接处。

官方示例是最好的字段说明书：

- [examples/window.rs:107-137](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/window.rs#L107-L137)：`Normal` 与 `Popup` 两个按钮的开窗代码——除了 `kind` 字段，其余完全一致，方便对比行为差异。同文件后续按钮依次演示 `Floating`、`Dialog`、`titlebar: None`（自绘标题栏）、`show: false`（隐形）、`is_movable: false`、`is_resizable: false`、`is_minimizable: false`。
- [examples/window_positioning.rs:53-71](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/window_positioning.rs#L53-L71)：`build_window_options` 函数几乎给每个字段都赋了值：`display_id`（指定显示器）+ `WindowKind::PopUp` + `focus: false` + 透明背景 + `is_movable: false`，是「多字段组合」的完整范例。
- [examples/window_positioning.rs:82-95](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/window_positioning.rs#L82-L95)：遍历 `cx.displays()`，用 `screen.bounds().top_right()` 等几何方法把窗口摆到每块屏幕的九个方位——多显示器定位的标准写法。

#### 4.2.4 代码实践

**实践目标**：建立「字段 → 观感/行为」的直接映射，尤其是四种 `WindowKind` 的差异。

**操作步骤**：

1. 运行 `cargo run -p gpui --example window`。
2. 依次点击 `Normal`、`Popup`、`Floating`、`Dialog` 四个按钮，每次观察新窗口的层叠关系；点开一个 `Dialog` 后，尝试点击它的父窗口。
3. 点击 `Custom Titlebar`，对比 `titlebar: None` 的窗口与系统标题栏窗口的区别（示例在内容区自绘了一条蓝色标题栏，[examples/window.rs:40-58](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/window.rs#L40-L58)）。
4. 点击 `Invisible`——终端没有任何新窗口出现，但窗口确实创建了；再点击 `Resize` 按钮观察 `window.resize`（[examples/window.rs:266-269](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/window.rs#L266-L269)：把宽高互换，演示运行时改尺寸与 `WindowOptions` 无关）。

**需要观察的现象**：`Dialog` 打开后父窗口无法交互（模态）；`Popup` 悬浮于其他应用窗口之上；`Invisible` 无窗口出现但程序不报错。

**预期结果**：四种 kind 的层叠与阻塞行为符合上表描述。

**待本地验证**：`WindowKind` 的最终视觉效果依赖平台窗口管理器（尤其 Linux 上 WM 对 popup/dialog 的处理），不同平台观感可能不同；`AnchoredPopup` 在没有原生实现的平台会直接开窗失败（返回 `PopupNotSupportedError`）。

#### 4.2.5 小练习与答案

**练习 1**：`WindowBounds::Maximized(Bounds::new(origin, size))` 里的 `size` 是什么意思？窗口实际会有多大？

**答案**：`size` 是还原尺寸（restore size）——将来用户把窗口从最大化还原时使用的尺寸；窗口初始实际大小是整个屏幕（或显示器工作区）的可视范围。`Fullscreen` 变体同理。

**练习 2**：想让新窗口「叠在当前窗口右下方一点」和「由系统决定」分别怎么写？

**答案**：两种诉求其实都写 `window_bounds: None`。`default_bounds` 的级联逻辑会自动取当前活动窗口的 bounds 加 25px 对角偏移；没有活动窗口时退回目标显示器（`display_id` 指定，否则主显示器）的 `default_bounds()`。若要精确控制，则显式传 `Some(WindowBounds::Windowed(bounds))`。

**练习 3**：为什么 `WindowOptions` 上有 `titlebar: Option<TitlebarOptions>` 这种「双层 Option」？`None` 和 `Some(TitlebarOptions { title: None, .. })` 有什么区别？

**答案**：外层 `None` 表示完全去掉标题栏（典型于自绘标题栏的窗口，配合 `appears_transparent` 或 Linux 的 `window_decorations`）；内层 `title: None` 表示保留系统标题栏但不设置初始标题。两层 Option 表达的是「有没有标题栏」与「标题栏里写什么」两个正交决策。

### 4.3 WindowHandle 与 AnyWindowHandle：窗口的句柄体系

#### 4.3.1 概念说明

和实体句柄 `Entity<T>` / `AnyEntity` 完全同构，窗口句柄也分两层：

- `WindowHandle<V>`：**类型化**句柄，`V` 是根视图类型。由 `WindowId` + `PhantomData<fn(V) -> V>` 组成，零运行时开销，`Copy` 语义。
- `AnyWindowHandle`：**类型擦除**句柄，`WindowId` + `TypeId`（根视图的类型标签）。`App::windows()` 返回的就是它，任何「不知道根视图类型」的场合（包括 `Window::window_handle()` 拿自己的句柄）都用它。

务必记住文档注释里那句提醒：**「Note that this does not keep the window alive on its own」**——句柄不保活。窗口被关闭后句柄还在你手里，但所有更新方法会返回 `Err`。这与 `WeakEntity` 的失败模式类似，只不过窗口句柄天生就是「弱」的，没有强窗口句柄。

两层的转换关系：

- `WindowHandle<V>` 通过 `Deref` 到 `AnyWindowHandle`，也可用 `From`/`Into` 显式转换（[src/window.rs:6536-6540](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L6536-L6540)）。
- `AnyWindowHandle::downcast::<V>()` 按 `TypeId` 判断能否转回 `WindowHandle<V>`，类型不符返回 `None`（[src/window.rs:6555-6566](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L6555-L6566)）。

#### 4.3.2 核心流程

跨窗口更新的统一入口是 `AppContext::update_window`（`cx.update_window(any_handle, |root_view, window, cx| ...)`），其实现 `App::update_window_id` 的流程：

```text
update_window_id(id, update)
  ├─ 1. window = cx.windows.get_mut(id)?.take()?   # 把 Box<Window> 整体搬出，槽位置 None
  ├─ 2. root_view = window.root.clone().unwrap()
  ├─ 3. cx.window_update_stack.push(id)            # 标记「当前正在更新哪个窗口」
  ├─ 4. result = update(root_view, &mut window, cx)  # 你的闭包：AnyView + &mut Window + &mut App
  └─ 5. trail(id, window, cx):
         ├─ window.removed == false → 放回槽位（windows.get_mut(id).replace(window)）
         └─ window.removed == true  → 拆除窗口（见 4.5）
```

第 1 步的「搬出再放回」一举三得：闭包拿到**按值独占**的 `&mut Window`；期间槽位是 `None`，同窗口嵌套 `update_window` 会因 `take()?` 失败而返回 `Err("window not found")`——物理上杜绝同一窗口的嵌套可变借用（与 u2-l2 实体租约同思路）；窗口若在更新中被标记删除，`trail` 顺势完成拆除。

#### 4.3.3 源码精读

- [src/window.rs:6381-6391](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L6381-L6391)：`WindowId` 由 `slotmap::new_key_type!` 宏生成，附带 `as_u64()` 与 `From<u64>`——`move_entity_between_windows` 示例就是用 `window_id().as_u64()` 在日志里区分窗口的。
- [src/window.rs:6399-6412](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L6399-L6412)：`WindowHandle<V>` 结构体与「不保活」的文档注释。
- [src/window.rs:6445-6463](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L6445-L6463)：`WindowHandle::update`——先 `cx.update_window` 进入窗口，再把 `AnyView` downcast 成 `Entity<V>` 并 `view.update(cx, ...)`。两层失败源：窗口已关闭（外层 `?`）、根视图类型变了（downcast 报错）。同文件还有 `read` / `read_with` / `entity` / `is_active`（[src/window.rs:6465-6511](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L6465-L6511)），以及仅测试构建可用的 `root()`。
- [src/window.rs:6542-6580](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L6542-L6580)：`AnyWindowHandle` 及其 `downcast` / `update` / `read`。注意 `AnyWindowHandle::update` 的闭包拿 `AnyView`，**不做**类型检查，适合「只管调用窗口方法」的场合（例如 `old_window.update(cx, |_, window, _| window.remove_window())`——根本不关心根视图）。
- [src/gpui.rs:212-215](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/gpui.rs#L212-L215)：`AppContext` trait 的 `update_window` 契约——五种上下文（`App`、`Context<T>`、`AsyncApp`、`AsyncWindowContext`、各测试上下文）都实现了它，所以在任何地方都能跨窗口更新。
- [src/app.rs:1849-1904](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app.rs#L1849-L1904)：`update_window_id` 本体，即上面流程图的源码。`trail` 内部函数（1860-1898 行）负责「放回或拆除」。
- [src/app.rs:1224-1243](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app.rs#L1224-L1243)：窗口枚举三兄弟——`windows()` 返回全部句柄、`window_stack()` 返回**按屏幕前后关系排序**的句柄列表（第一个是当前最上层窗口，平台未实现则返回 `None`）、`active_window()` 返回平台层面当前聚焦的窗口。

#### 4.3.4 代码实践

**实践目标**：验证「句柄不保活」与跨窗口更新的 `Result` 语义。

**操作步骤**：

1. 在 4.1.4 里创建的 `examples/my_windows.rs` 基础上，把第二个 `open_window` 的返回值存进主窗口视图的字段（示例代码）：
   ```rust
   // WindowDemo 结构体加一个字段：
   // sub_window: Option<WindowHandle<WindowDemo>>,
   let sub = cx
       .open_window(
           WindowOptions { window_bounds: Some(window_bounds), ..Default::default() },
           |_, cx| cx.new(|_| WindowDemo { sub_window: None }),
       )
       .unwrap();
   // 主窗口创建后（或通过一个按钮回调）：
   println!("windows now: {:?}", cx.windows());
   ```
2. 给主窗口加一个「检查子窗口」按钮，回调里执行：
   ```rust
   match sub.update(cx, |sub, _window, _cx| sub.sub_window.is_some()) {
       Ok(alive) => println!("子窗口存活: {alive}"),
       Err(err) => println!("子窗口更新失败: {err}"),
   }
   ```
3. 运行示例，先点「检查子窗口」按钮，然后手动关闭子窗口，再点一次按钮。

**需要观察的现象**：第一次点击打印 `子窗口存活: true`；关闭子窗口后再点，打印 `子窗口更新失败: ...`。

**预期结果**：关闭后的窗口句柄调用 `update` 返回 `Err`（错误信息是 `"window not found"` 上下文），程序不 panic——这正是句柄弱引用语义的意义：持有旧句柄不会让窗口复活，也不会崩溃。

**待本地验证**：按钮的具体绑定写法可参考 `examples/window.rs` 的 `button` 辅助函数（[examples/window.rs:14-27](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/window.rs#L14-L27)）；字段存句柄需要处理 `WindowDemo` 的构造顺序，若嫌繁琐可把句柄存进一个 `Global`（u2-l4）来简化。

#### 4.3.5 小练习与答案

**练习 1**：`WindowHandle::update` 与 `AnyWindowHandle::update` 的失败条件有何不同？

**答案**：两者都会在窗口已关闭时失败。此外 `WindowHandle<V>::update` 还要求根视图类型必须是 `V`（内部 downcast，类型变了报「the type of the window's root view has changed」）；`AnyWindowHandle::update` 不做类型检查，闭包直接拿 `AnyView`。需要读写根视图状态用前者，只操作窗口本身（如 `remove_window`）用后者。

**练习 2**：为什么 `App` 里除了 `windows` slotmap 还要维护一张 `window_handles: FxHashMap<WindowId, AnyWindowHandle>`？

**答案**：slotmap 只能「凭 id 拿窗口」，而 `AnyWindowHandle` 还携带根视图的 `TypeId`。`cx.windows()` 需要返回带类型标签的句柄（这样才能 `downcast::<V>()`），窗口被搬出槽位更新期间（槽位为 `None`）`windows()` 依然要能列出它，因此单独维护一张「id → AnyWindowHandle」的登记表，在 `open_window` 成功后写入、窗口拆除时删除。

**练习 3**：`window_stack()` 与 `active_window()` 的区别是什么？

**答案**：`active_window()` 是平台层面「当前聚焦的那一个」窗口（可能为 `None`，比如焦点在别的应用）；`window_stack()` 返回全部窗口按屏幕层叠前后排序的完整列表（第一个即最上层），依赖平台实现 `Platform::window_stack`，未实现返回 `None`。

### 4.4 实体不属于窗口：共享与迁移

#### 4.4.1 概念说明

这是 GPUI 窗口设计中最有特点的一条：**窗口不拥有实体，实体也不隶属于窗口**。回顾 u2-l2：实体住在 `App` 的 `EntityMap` 里，窗口只是「引用」实体——每个窗口有一个根视图，元素树里还可以引用任意其他实体。由此推出三个重要结论：

1. **同一份状态可以被多个窗口同时显示**。两个窗口的根视图（或子视图）可以持有同一个 `Entity<T>` 句柄，各自 `read` 渲染。`cx.notify()` 时，u4-l3 讲过的倒排表（`window_invalidators_by_entity`：实体 → 显示它的窗口集合）会把**所有**正在显示该实体的窗口标脏——一处修改、处处刷新是结构天然支持的。
2. **「视图在哪个窗口」是运行时属性，不是注册时属性**。`_in` 系列 API（`update_in`、`spawn_in`、`subscribe_in`、`defer_in`、`observe_*_in`）注册回调时需要传一个 `&Window`，但派发时走的是实体的**当前窗口**（current window）——定义为「最近一次渲染时引用过该实体的窗口」（见 `AppContext::with_window` 的文档注释）。
3. **实体可以在窗口间迁移**。既然窗口只是引用实体，那么「把实体搬到新窗口」=「开一个新窗口并以该实体为根视图」+「关掉旧窗口」。没有任何「重新绑定」 ceremony。

官方示例 `move_entity_between_windows` 的文档注释把实验目的说得很直白（[examples/move_entity_between_windows.rs:1-7](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/move_entity_between_windows.rs#L1-L7)）：实体用 `_in` 系列注册回调，然后通过点击被「重新托管」（re-hosted）到新窗口，要点在于**迁移之后派发的回调正确地指向实体的当前窗口，而不是注册时的窗口**。

#### 4.4.2 核心流程

迁移的标准四步（全部来自官方示例）：

```text
用户点击「Move me to a new window」
  └─ cx.listener 里 cx.emit(MoveToNewWindow)
       └─ subscribe_in 处理器（在实体的当前窗口上下文中执行）：
            1. cx.defer(...)                    # 推迟到本轮效果循环结束，避免在订阅回调里重入
            2. cx.open_window(options, move |_, _| entity)   # 关键：根视图构建器直接返回已有实体
            3. old_window.update(cx, |_, window, _| window.remove_window())  # 关闭旧窗口
            4. 后续 tick 任务、订阅回调继续触发，但 window 参数已经是新窗口
```

第 2 步是精髓：`open_window` 的 `build_root_view` 闭包**不一定要 `cx.new` 创建新实体**，直接返回一个已存在的 `Entity<V>` 即可——新窗口以它为根视图。配合第 3 步关旧窗，宏观效果就是「窗口内容搬家」。

而持续运行的 `spawn_in` 任务则持续证明「当前窗口」语义：它每秒 `update_in` 一次实体，日志里打印的 `window_id` 在迁移前后会从旧 id 变成新 id。

#### 4.4.3 源码精读

- [examples/move_entity_between_windows.rs:35-51](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/move_entity_between_windows.rs#L35-L51)：`cx.spawn_in(window, async move |this, cx| ...)` 启动的周期任务，内部 `this.update_in(cx, |this, window, _cx| ...)` 拿到的 `window` 就是**执行时刻**的当前窗口，日志打印 `window.window_handle().window_id().as_u64()`。
- [examples/move_entity_between_windows.rs:53-82](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/move_entity_between_windows.rs#L53-L82)：迁移的核心 handler。`cx.subscribe_in::<_, MoveToNewWindow>(&self_entity, window, ...)` 注册带窗口的订阅；处理器里 `cx.defer` 后 `cx.open_window(WindowOptions { ... }, move |_, _| entity)` 以已有实体为新窗口根视图，再 `old_window.update(cx, |_, window, _| window.remove_window()).ok()` 关闭旧窗口（`.ok()` 静默容忍旧窗口此刻已不存在的边角情况）。
- [examples/move_entity_between_windows.rs:94-126](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/move_entity_between_windows.rs#L94-L126)：`render` 里用 `window.window_handle().window_id().as_u64()` 把「当前渲染发生在哪个窗口」「tick 计数」「迁移计数」直接显示在界面上，配合终端日志可以肉眼对照。
- [src/gpui.rs:217-225](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/gpui.rs#L217-L225)：`AppContext::with_window` 的契约注释——「Run `f` against the entity's *current* window — the most recently rendered window that referenced the entity」。这是 `_in` 系列回调定位窗口的底层依据；实体没有当前窗口或该窗口不可用时返回 `None`。
- [src/app/entity_map.rs:502-508](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/entity_map.rs#L502-L508)：`Entity::update_in` 的签名与文档——「within a visual context that has a window. Returns an error if the window has been closed」，与 `update` 的差异一目了然。
- [src/app/context.rs:676-690](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/context.rs#L676-L690)：`Context::spawn_in`——把异步任务绑定到窗口上下文（`AsyncWindowContext`），任务里的 `update_in` 即按当前窗口解析。

#### 4.4.4 代码实践

**实践目标**：亲眼确认「回调派发到当前窗口而非注册窗口」。

**操作步骤**：

1. 运行：
   ```bash
   cargo run -p gpui --example move_entity_between_windows
   ```
2. 静置观察终端 5 秒：每秒一条 `tick #N fired in entity's current window <id>`。
3. 点击窗口中的「Move me to a new window」按钮。
4. 继续观察终端与新窗口界面。

**需要观察的现象**：点击后终端先打印 `MoveToNewWindow handler fired in entity's current window <旧id>`；随后新窗口出现（界面显示 `Rendering in window: <新id>`、`Moves observed by entity: 1`），旧窗口消失；tick 日志继续输出，但 id 已变成新窗口的 id，计数不中断（`Ticks observed by entity` 持续累加）。

**预期结果**：实体的状态（tick 计数、move 计数）在迁移后完整保留——因为实体从未被重建，只是换了宿主窗口；`spawn_in` 任务的回调始终拿到当前窗口。

**待本地验证**：Linux/Wayland 上窗口 id 的具体数值无关紧要，关注的是「迁移前后日志里 id 发生切换且计数连续」。

#### 4.4.5 小练习与答案

**练习 1**：如果把迁移 handler 里的 `cx.defer(...)` 直接去掉、在订阅回调里同步调用 `cx.open_window`，可能出什么问题？

**答案**：订阅回调发生在效果派发（`flush_effects`）过程中，此时 `App` 正处于效果循环里；同步开窗会触发平台窗口创建、首轮绘制、外观回调等一系列再入操作，容易与正在进行的派发/借用发生冲突。`cx.defer` 把开窗与关窗动作推迟到本轮效果循环收尾，是官方示例给出的稳妥写法（同一模式也出现在 `examples/window.rs` 的嵌套对话框中可对照）。

**练习 2**：一个实体能否同时作为两个窗口的根视图？

**答案**：机制上没有禁止——两个窗口的 `build_root_view` 都返回同一实体即可，且 `cx.notify()` 会通过倒排表把两个窗口都标脏。但此时「实体的当前窗口」只有一个（最近渲染的那个），`_in` 系列回调只会派发到那一个窗口；官方迁移示例也采用「开新窗 + 立即关旧窗」的互斥模式。需要多窗口展示同一状态时，更常见的做法是多个根视图实体**共享同一个数据实体**（综合实践采用这种）。

**练习 3**：`Entity::update` 与 `Entity::update_in` 的适用场景分别是什么？

**答案**：`update` 只需要一个满足 `AppContext` 的上下文（包括 `&mut App`），闭包拿 `&mut T` + `Context<T>`，适合纯状态修改；`update_in` 需要 `VisualContext`（有窗口的上下文），闭包额外拿到 `&mut Window`，适合「修改状态的同时操作窗口」（如 `window.remove_window()`），且窗口已关闭时返回错误而不是 panic。

### 4.5 窗口生命周期：关闭、观察与退出联动

#### 4.5.1 概念说明

窗口的生命周期事件分布在三个层级：

1. **窗口级观察者**（`Context<T>` 上注册，返回 `Subscription`）：`observe_window_bounds`（尺寸/位置变化）、`observe_window_activation`（激活/失活）、`observe_window_appearance`（明暗外观变化）。它们存在 `Window` 的 `bounds_observers` / `activation_observers` 等 `SubscriberSet` 里。
2. **窗口级否决**：`Window::on_window_should_close` 注册一个返回 `bool` 的回调——用户点关闭按钮时平台会询问，返回 `false` 可以**阻止窗口关闭**（典型于「有未保存修改」的确认场景；u7-l3 的 prompt 是它的天然搭档）。
3. **App 级通知**：`cx.on_window_closed(|cx, window_id| ...)` 在窗口**完全拆除之后**触发，此时该窗口已不可寻址，适合做全局簿记（比如「最后一个窗口关了就退出」）。

关闭窗口本身只有一条 API：`window.remove_window()`。它的实现只有一行——`self.removed = true`。真正的拆除发生在**下一次**该窗口经过 `update_window_id` 时（`trail` 分支）：从两张表里删除、清理该窗口追踪的实体与倒排索引、触发 `window_closed_observers`，然后根据 `QuitMode` 决定是否退出应用。

#### 4.5.2 核心流程

关闭 → 拆除 →（可能）退出的完整链路：

```text
window.remove_window()            # 仅置 removed = true
  ↓（同一轮 update_window 的 trail 阶段）
trail(id, window, cx):
  ├─ cx.end_platform_drag(id)                      # 若该窗口是拖拽源，终止平台拖拽
  ├─ cx.window_handles.remove(&id)                 # 注销类型句柄表
  ├─ cx.windows.remove(id)                         # 注销窗口槽位（Window 在此 drop）
  ├─ 清理 tracked_entities / window_invalidators_by_entity / current_window_by_entity
  │    # 该窗口与实体世界的全部关联被切断，其他窗口不受影响
  ├─ 触发 window_closed_observers（即 cx.on_window_closed 的回调）
  └─ quit_on_empty 判定：
       QuitMode::Explicit        → 不退出（应用显式调 cx.quit 才退）
       QuitMode::LastWindowClosed → 窗口全空则 cx.quit()
       QuitMode::Default          → 非 macOS 上窗口全空则退出；macOS 不退（符合平台惯例）
```

bounds/activation 观察者的触发源头在平台回调：平台窗口尺寸变化 → `Window::bounds_changed`（更新 `scale_factor`、`viewport_size` 并 `refresh()`）→ 遍历 `bounds_observers`；激活状态变化 → `on_active_status_change` 回调 → 更新 `active` 单元 + 遍历 `activation_observers`，顺带刷新 modifiers/capslock 并 `bounds_changed` + `refresh()`。

#### 4.5.3 源码精读

- [src/window.rs:2024-2027](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L2024-L2027)：`remove_window` 全文——一行置标志。对比紧邻其上的 `refresh`（把窗口标脏重绘）可以体会 GPUI「先登记意图、统一时机结算」的一贯风格。
- [src/app.rs:1860-1898](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app.rs#L1860-L1898)：`trail` 函数——拆除流程的源码，对照上面的链路图逐行读。特别注意它如何把该窗口追踪的实体从 `window_invalidators_by_entity`（倒排表）与 `current_window_by_entity` 里摘除：**关闭一个窗口不会影响其他窗口正在显示的实体**。
- [src/app.rs:1885-1893](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app.rs#L1885-L1893)：`quit_on_empty` 三态判定——`QuitMode::Default` 用 `cfg!(not(target_os = "macos"))` 表达「macOS 应用惯例是关完窗口进程仍在」。
- [src/app.rs:2358-2367](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app.rs#L2358-L2367)：`App::on_window_closed`——文档注释强调「窗口在此回调触发时已不可访问」。返回 `Subscription`，drop 即注销（与 u2-l1 的其他应用级钩子一致）。
- [src/window.rs:5952-5963](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L5952-L5963)：`on_window_should_close`——把返回 `bool` 的闭包装箱交给 `platform_window.on_should_close`；注意兜底逻辑 `unwrap_or(true)`：若回调执行时上下文已失效（比如窗口正在拆除），默认允许关闭。
- [src/app/context.rs:425-459](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/app/context.rs#L425-L459)：`observe_window_bounds` 与 `observe_window_activation` 的注册实现——把视图的弱句柄包进观察者（视图没了自动失效返回 `false` 注销），插入 `Window` 上对应的 `SubscriberSet`。`examples/window.rs:320-325` 是前者的使用样例（在 `build_root_view` 阶段注册、`detach()` 保活）。
- [src/window.rs:2427-2448](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L2427-L2448)：`Window::bounds_changed`（平台 resize 回调的汇聚点：同步 `scale_factor` / `viewport_size` / `display_id` / `mouse_position`，再 `refresh()` + 遍历观察者）与其后的 `bounds()` 查询。运行时改尺寸则用 `resize`（[src/window.rs:2468-2471](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L2468-L2471)）。
- [src/window.rs:1710-1730](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/src/window.rs#L1710-L1730)：`platform_window.on_active_status_change` 注册处——激活变化时更新 `active`、同步修饰键状态、遍历 `activation_observers`、调用 `bounds_changed` 并 `refresh()`。
- [examples/on_window_close_quit.rs:44-50](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/on_window_close_quit.rs#L44-L50)：`on_window_closed` 的标准用法——回调里查 `cx.windows().is_empty()`，空则 `cx.quit()`。该示例还展示了 `cmd-w` 绑定到自定义 `CloseWindow` 动作、动作处理器里调 `window.remove_window()` 的完整键盘关窗路径（[examples/on_window_close_quit.rs:15-21](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/on_window_close_quit.rs#L15-L21)）。

#### 4.5.4 代码实践

**实践目标**：体验「关闭 → 退出」联动与关闭否决。

**操作步骤**：

1. 运行：
   ```bash
   cargo run -p gpui --example on_window_close_quit
   ```
2. 示例开了两个并排窗口。用 `cmd-w`（macOS）/ 点击窗口关闭按钮关掉一个，再关掉另一个。
3. 复制该示例为 `examples/my_close_veto.rs`，在 `run_example` 里给其中一个窗口加关闭否决（示例代码）：
   ```rust
   cx.open_window(
       WindowOptions { window_bounds: Some(WindowBounds::Windowed(bounds)), ..Default::default() },
       |window, cx| {
           window.on_window_should_close(cx, |_window, _cx| {
               println!("拒绝关闭！");
               false
           });
           cx.new(|cx| {
               let focus_handle = cx.focus_handle();
               focus_handle.focus(window, cx);
               ExampleWindow { focus_handle }
           })
       },
   )
   .unwrap();
   ```

**需要观察的现象**：第 2 步中，两个窗口都关闭后进程退出（注意该示例用 `on_window_closed` 显式 `quit`，与 `QuitMode` 默认行为叠加）；第 3 步中，被否决的窗口点击关闭按钮时终端打印「拒绝关闭！」且窗口不消失，另一个窗口可正常关闭。

**预期结果**：`on_window_should_close` 返回 `false` 能拦下平台发起的关闭；返回 `true`（或不注册）则放行，随后走 `remove_window` → 拆除 → `on_window_closed` 链路。

**待本地验证**：Linux 上窗口关闭按钮的路径依赖 WM/X11/Wayland 实现，`on_window_should_close` 在各平台的触发时机以本地实测为准；`cmd-w` 键位在非 macOS 平台需自行改成 `ctrl-w` 之类的绑定才能触发。

#### 4.5.5 小练习与答案

**练习 1**：调用 `window.remove_window()` 之后窗口立刻消失吗？真正的拆除发生在什么时机？

**答案**：`remove_window` 只把 `Window::removed` 置为 `true`。拆除发生在同一轮 `update_window_id` 调用的收尾阶段（`trail` 函数）：从 `windows` / `window_handles` 移除、清理实体关联、触发 `on_window_closed`、按 `QuitMode` 判定是否退出。也就是说「请求关闭」与「完成关闭」在同一个 `update_window` 调用内先后发生。

**练习 2**：macOS 上用户关掉最后一个窗口，默认（`QuitMode::Default`）应用会退出吗？如何强制「关掉最后一个窗口就退出」？

**答案**：默认不退出——`Quit_on_empty` 对 `QuitMode::Default` 取 `cfg!(not(target_os = "macos"))`，这符合 macOS「关完窗口进程常驻」的惯例。要强制退出，用 `Application::with_quit_mode(QuitMode::LastWindowClosed)`（u2-l1 讲过该配置钩子），或像 `on_window_close_quit` 示例那样在 `on_window_closed` 里自己判断 `cx.windows().is_empty()` 后 `cx.quit()`。

**练习 3**：`observe_window_bounds` 与 `observe_window_activation` 注册在哪个对象上？窗口关闭后还需要手动注销吗？

**答案**：注册在 `Window` 的 `bounds_observers` / `activation_observers` 这两个 `SubscriberSet` 上（通过 `Context<T>` 的方法注册，观察者内部持有视图弱句柄）。窗口关闭时整个 `Window` 被 drop，其中的观察者集合随之消亡，无需手动注销；返回的 `Subscription` 在正常作用域结束时会自动注销。观察者内部用弱句柄升级，视图先于窗口释放时回调自动失效——这与 u2-l3 讲过的订阅防泄漏机制一致。

## 5. 综合实践

**任务**：实现「主窗口 + 两个子窗口」的多窗口应用——三个窗口展示**同一份计数器数据的不同视图**，在任意窗口修改数据后其余窗口实时刷新；再为子窗口实现「把本窗口迁移到新窗口」按钮。这综合了本讲全部四个最小模块：`cx.open_window`、`WindowOptions`、`WindowHandle`、实体与窗口解耦。

**设计要点**（先想清楚再动手）：

- 数据实体 `Counter` 与视图实体 `CounterPanel` 分离：`Counter` 是纯状态（不属于任何窗口），每个窗口的根视图是一个 `CounterPanel`，持有 `Entity<Counter>` 的克隆句柄——这就是「同一份数据、多个视图」。
- 同步刷新用 u2-l3 的 observe 模式：每个 `CounterPanel` 在构造时 `cx.observe(&counter, |this, _, cx| cx.notify())`，计数器一变，各窗口的根视图各自被标脏重绘。
- 迁移用 4.4 的官方模式：`cx.listener` 里 `cx.entity()` 拿到自身句柄，`cx.defer` 后 `open_window(..., move |_, _| panel)` + 旧窗口 `remove_window`。

**操作步骤**：

1. 新建 `examples/multi_window.rs`（`Cargo.toml` 未关闭 autoexamples，无需显式声明），写入以下**示例代码**：

   ```rust
   #![cfg_attr(target_family = "wasm", no_main)]

   use gpui::{
       App, Bounds, Context, Entity, Render, SharedString, Subscription, Window, WindowBounds,
       WindowOptions, div, prelude::*, px, rgb, size,
   };
   use gpui_platform::application;

   /// 共享数据：一个纯状态实体，不属于任何窗口。
   struct Counter {
       count: i64,
   }

   /// 每个窗口的根视图：持有共享实体的句柄，按 mode 展示同一份数据的不同视图。
   struct CounterPanel {
       counter: Entity<Counter>,
       label: SharedString,
       doubled: bool,
       _subscription: Subscription,
   }

   impl CounterPanel {
       fn new(
           label: &str,
           doubled: bool,
           counter: Entity<Counter>,
           cx: &mut Context<Self>,
       ) -> Self {
           // 观察共享实体：计数一变，本视图 cx.notify()，所在窗口随之重绘
           let _subscription = cx.observe(&counter, |this, _counter, cx| {
               cx.notify();
           });
           Self {
               counter,
               label: label.into(),
               doubled,
               _subscription,
           }
       }
   }

   impl Render for CounterPanel {
       fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
           let count = self.counter.read(cx).count;
           let shown = if self.doubled { count * 2 } else { count };
           let counter = self.counter.clone();
           let window_id = _window.window_handle().window_id().as_u64();

           div()
               .flex()
               .flex_col()
               .gap_3()
               .size_full()
               .justify_center()
               .items_center()
               .bg(rgb(0x282828))
               .text_color(rgb(0xffffff))
               .child(format!(
                   "{}（window {}）当前显示：{}",
                   self.label, window_id, shown
               ))
               .child(
                   div()
                       .id("increment")
                       .px_4()
                       .py_2()
                       .rounded_md()
                       .bg(rgb(0x4040ff))
                       .child("＋1（任意窗口点击，所有窗口同步刷新）")
                       .on_click(move |_, _window, cx| {
                           counter.update(cx, |counter, _cx| counter.count += 1);
                       }),
               )
               .child(
                   div()
                       .id("move")
                       .px_4()
                       .py_2()
                       .rounded_md()
                       .bg(rgb(0x7a3e9d))
                       .child("把本窗口迁移到新窗口")
                       .on_click(cx.listener(|_this, _, window, cx| {
                           let entity = cx.entity();
                           let old_window = window.window_handle();
                           cx.defer(move |cx| {
                               let bounds =
                                   Bounds::centered(None, size(px(400.0), px(300.0)), cx);
                               cx.open_window(
                                   WindowOptions {
                                       window_bounds: Some(WindowBounds::Windowed(bounds)),
                                       ..Default::default()
                                   },
                                   move |_, _| entity, // 关键：新窗口的根视图就是本实体
                               )
                               .unwrap();
                               old_window
                                   .update(cx, |_, window, _| window.remove_window())
                                   .ok();
                           });
                       })),
               )
       }
   }

   fn run_example() {
       application().run(|cx: &mut App| {
           let counter = cx.new(|_| Counter { count: 0 });

           let make = |label: &'static str, doubled: bool, offset: f32| {
               move |cx: &mut App| {
                   let bounds = Bounds::centered(None, size(px(400.0), px(300.0)), cx);
                   let Bounds { mut origin, size } = bounds;
                   origin.x += px(offset);
                   cx.open_window(
                       WindowOptions {
                           window_bounds: Some(WindowBounds::Windowed(Bounds { origin, size })),
                           ..Default::default()
                       },
                       move |_, cx| {
                           cx.new(|cx| CounterPanel::new(label, doubled, counter.clone(), cx))
                       },
                   )
                   .unwrap();
               }
           };

           (make("主窗口（原值视图）", false, -420.0))(cx);
           (make("子窗口 A（原值视图）", false, 0.0))(cx);
           (make("子窗口 B（二倍视图）", true, 420.0))(cx);

           cx.activate(true);
       });
   }

   #[cfg(not(target_family = "wasm"))]
   fn main() {
       run_example();
   }

   #[cfg(target_family = "wasm")]
   #[wasm_bindgen::prelude::wasm_bindgen(start)]
   pub fn start() {
       gpui_platform::web_init();
       run_example();
   }
   ```

2. 运行：
   ```bash
   cargo run -p gpui --example multi_window
   ```

**需要观察的现象（验收清单）**：

1. 三个窗口横向排开，`子窗口 B` 显示的数字始终是另外两个的 2 倍——同一实体、不同视图。
2. 在**任意**一个窗口点「＋1」，三个窗口的数字同时 +1（B 加 2）——`cx.notify()` 经倒排表标脏所有显示该实体的窗口，各面板的 observe 回调再各自触发重绘。
3. 在子窗口 A 点「迁移」：A 内容原封不动地出现在一个居中的新窗口里（`window` 编号变化），旧 A 窗口关闭；随后继续点「＋1」，新窗口的数据照常刷新——实体的状态与观察订阅在迁移后完整保留。
4. 关闭任意两个窗口后应用若仍在运行，再关最后一个窗口，进程退出（非 macOS 默认 `QuitMode` 行为）。

**预期结果**：以上四条全部成立，即验证了本讲的核心论断——窗口是实体的「宿主」而非「所有者」。

**待本地验证**：本示例代码未在本地编译运行过。若编译报错，优先检查：`cx.listener` 内 `window` 参数的命名（示例中用了 `_window` 以外的名字以便取 `window_handle()`，`cx.listener` 闭包签名是 `|this, event, window, cx|`）；`make` 闭包捕获 `counter` 需要三次调用共享一个句柄（`Entity` 是 `Clone` 的，`.clone()` 廉价）；如遇 `-window` 命名冲突可自行改名。

## 6. 本讲小结

- **窗口是 App 级资源**：`App` 用 `windows: SlotMap<WindowId, Option<Box<Window>>>` 与 `window_handles` 两张表管理全部窗口；`cx.open_window(options, build_root_view)` 走「预留 id → 创建平台窗口 → 构建根视图 → 先画一帧 → 双表登记」的流程，返回 `WindowHandle<V>`。
- **`WindowOptions` 是纯数据的创建期请求**：位置（`window_bounds` 三态 + `display_id`）、标题栏、行为开关、`WindowKind` 六种窗口语义、外观与平台标识；`None` 位置触发 25px 级联兜底；「请求」以平台窗口创建后读回的状态为准。
- **窗口句柄与实体句柄同构**：`WindowHandle<V>` = `WindowId` + 类型标签（`Copy`、不保活），`AnyWindowHandle` 是其类型擦除形态；跨窗口更新统一走 `AppContext::update_window` → `update_window_id` 的「搬出槽位 → 更新 → 放回/拆除」三段式，天然防同窗口嵌套更新。
- **实体不属于窗口**：同一实体可被多窗口显示（倒排表保证 notify 处处刷新），`_in` 系列回调派发到实体的**当前窗口**；「迁移」= 新窗口以已有实体为根视图 + 旧窗口 `remove_window`，无任何重新绑定。
- **关闭是延迟结算**：`remove_window` 只置标志，拆除（清表、清实体关联、触发 `on_window_closed`、按 `QuitMode` 判定退出）发生在同一轮 `update_window` 收尾；`on_window_should_close` 可否决平台关闭，`observe_window_bounds` / `observe_window_activation` 提供窗口级观察。

## 7. 下一步学习建议

本讲搞定了「窗口」这个容器本身，下一讲 **u7-l3（对话框、菜单与系统通知）** 将补齐窗口与操作系统的三类交互：`window.prompt` 的原生对话框流程（`src/window/prompts.rs` 的 `PromptBuilder`，本讲 `examples/window.rs` 里已经出现过调用样例）、应用菜单与动作绑定（`src/platform/app_menu.rs`）、系统通知。建议在继续之前：

1. 把综合实践的示例玩熟，尤其多次迁移、关闭再重开窗口，观察 observe 订阅与窗口句柄的生命周期是否如本讲所述。
2. 通读 [examples/window.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/gpui/examples/window.rs) 里还没试过的按钮（`Hide Application`、`Prompt`），它们分别对应 u2-l1 的应用级 API 与 u7-l3 的对话框主题。
3. 想深挖平台差异的读者可以带着本讲的 `WindowParams` 去 u7-l1 讲过的 `gpui_linux` / `gpui_macos` 里看 `Platform::open_window` 的实现如何逐字段消费这些请求。
