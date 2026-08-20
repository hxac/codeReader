# 光标、外观与拖放：set_cursor_style、window_appearance 与外部拖拽

## 1. 本讲目标

本讲是单元 3（窗口与输入）的收官篇，聚焦 `Platform` trait 上三个「小而精」的能力组：

1. **光标样式**：掌握从元素上的 `.cursor()` 声明，到 `Platform::set_cursor_style` 平台调用，再到四个操作系统各自画光标的完整链路；理解 `hide_cursor_until_mouse_moves` 与 `is_cursor_visible` 这对配套接口。
2. **窗口外观**：掌握 `window_appearance` 查询与 `set_window_appearance` 覆盖这对接口，并能在源码层面解释「为什么外观覆盖只在 macOS 上真正生效」。
3. **外部拖放**：理解 `ExternalDragPayload` 数据模型，以及一次 GPUI 内部拖拽如何在你把元素拖出窗口边缘的一瞬间被「晋升」为操作系统级的原生拖拽会话。

读完本讲，你应当能回答：光标样式为什么在 paint 阶段收集、在绘制结束时统一下发？Linux 的深色模式信息从哪里来？为什么 X11 上把文件拖出窗口不会有任何反应？

## 2. 前置知识

### 2.1 声明式样式与命中盒（Hitbox）

GPUI 是保留模式与立即模式的混合体：每帧都会重新执行各视图的 `render`，随后进入 **paint 阶段**。元素在 paint 阶段可以拿到自己的 `Hitbox`（命中盒，即该元素在窗口中的可见区域，含 stacking 上下文信息）。本讲的光标机制完全建立在 hitbox 之上：「鼠标悬停在谁的命中盒上，就显示谁声明的光标」。

### 2.2 trait 默认实现即「能力探测」

在 u2-l1 中我们总结过 `Platform` trait 默认实现的三种姿态。本讲会遇到两个典型案例：

- `set_window_appearance` 的默认实现是**空方法体**（优雅降级 no-op 型），全仓库只有 macOS 覆盖了它；
- `can_start_external_drag` / `start_external_drag` 的默认实现是**返回 false**（能力探测型），调用方据此判断该平台支不支持把拖拽交出去。

### 2.3 各操作系统的光标资源体系

| 平台 | 光标资源体系 | 关键点 |
| --- | --- | --- |
| macOS | AppKit `NSCursor` 类方法（`arrowCursor`、`pointingHandCursor`…） | 通过 cursor rect 机制按区域注册 |
| Linux/Wayland | `cursor-shape-v1` 协议扩展，或经典的 XDG 光标主题（按名字查位图） | 两套路径，前者优先 |
| Linux/X11 | X11 光标字体 / `Xcursor` 主题，经 XCB 装载为 `Cursor` id | 挂在窗口属性上 |
| Windows | `LoadCursor` 系列或等价封装，`HCURSOR` 句柄 | 经窗口消息应用到光标 |
| Web | CSS `cursor` 属性 | 直接映射到 `CursorStyle` 的文档注释里的 CSS 值 |

### 2.4 外观（Appearance）与窗口 chrome

「外观」指系统级的浅色/深色模式。关键区别在于**窗口 chrome（标题栏、边框）由谁绘制**：

- macOS 上，窗口标题栏由 AppKit 按 `NSApplication.appearance` 渲染，GPUI 无法自绘，所以需要平台级的覆盖接口；
- Linux/Windows 上，Zed/GPUI 客户端自绘标题栏，应用主题自己就能决定深浅，不需要平台层介入。

这个差异正是本讲学习目标「外观覆盖为何只在 macOS 上真正生效」的答案，下文用源码验证。

### 2.5 拖放（Drag & Drop）的两个世界

- **内部拖拽**：GPUI 自己管理。`on_drag` 启动，拖拽影像是一个 GPUI 视图，落点由 `on_drop` 接收——u3-l2 与 `drag_drop` 示例里已经见过。
- **原生拖拽（外部拖拽）**：操作系统级的 DnD 会话。拖出窗口后由系统接管，落点可以是 Finder、别的应用，本应用只提供一个「负载」（payload），例如文件 URI 列表。两个世界靠 MIME 类型（如 `text/uri-list`）交换数据。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `../gpui/src/platform.rs` | 契约层：`CursorStyle`、`WindowAppearance` 枚举与 `set_cursor_style` / `window_appearance` / `set_window_appearance` / `should_auto_hide_scrollbars` / `start_external_drag` 的签名 |
| `../gpui/src/window.rs` | `Window` 层：paint 期收集光标请求、绘制尾部统一下发；`promote_external_drag_to_platform` 晋升逻辑；外部拖拽测试 |
| `../gpui/src/style.rs`、`../gpui_macros/src/styles.rs` | 声明式入口：`Style.mouse_cursor` 字段与 `.cursor()` 系列方法（宏生成） |
| `../gpui/src/elements/div.rs` | 元素层：paint 期调用 `window.set_cursor_style`；`Interactivity::external_drag_payload` 注册外部负载 |
| `../gpui/src/interactive.rs` | `ExternalDragPayload` / `FileDragPaths` / `FileDropEvent` 数据模型 |
| `../gpui/src/app.rs` | `App` 转发层；`AnyDrag`、`PlatformOwnedDrag` 状态机 |
| `../gpui_macos/src/platform.rs`、`../gpui_macos/src/window_appearance.rs`、`../gpui_macos/src/window.rs` | macOS：`NSCursor`、`NSAppearance` 映射与外部拖拽 |
| `../gpui_linux/src/linux/platform.rs` | `LinuxPlatform` 转发、`LinuxClient` 契约、`cursor_style_to_icon_names` 通用映射表 |
| `../gpui_linux/src/linux/wayland/cursor.rs`、`../gpui_linux/src/linux/wayland.rs`、`../gpui_linux/src/linux/wayland/client.rs`、`../gpui_linux/src/linux/wayland/window.rs` | Wayland：光标主题装载、shape 协议、外部拖拽的 data_source |
| `../gpui_linux/src/linux/x11/client.rs`、`../gpui_linux/src/linux/x11/window.rs` | X11：光标字体装载与外观状态 |
| `../gpui_linux/src/linux/xdg_desktop_portal.rs` | Linux 外观来源：portal 的 `color-scheme` 设置 |
| `../gpui_windows/src/platform.rs`、`../gpui_web/src/platform.rs` | Windows / Web 实现 |
| `../gpui/examples/drag_drop.rs` | 官方拖拽示例，本讲代码实践的锚点 |

## 4. 核心概念与源码讲解

### 4.1 光标样式：从 `.cursor()` 声明到 `Platform::set_cursor_style`

#### 4.1.1 概念说明

光标样式解决的问题是**悬停反馈**：鼠标停在一个可点击的元素上时应显示手形、停在文本上应显示 I 形。因为每个操作系统表达「换光标」的方式完全不同（NSCursor / X11 Cursor id / HCURSOR / CSS），GPUI 在平台契约里定义了一个语义枚举 `CursorStyle`，它的每个变体都在文档注释里标注了对应的 CSS `cursor` 值——也就是说，这个枚举的公共语言就是 CSS 光标语义。

注意一个容易混淆的点：`set_cursor_style` 定义在 **`Platform`**（应用级）而不是 `PlatformWindow`（窗口级）上。原因是光标本质上是全局输入设备的状态；但各平台实现内部通常**按窗口记录**当前样式（后面 X11 实现里会看到 `cursor_styles: HashMap<Window, CursorStyle>`）。

#### 4.1.2 核心流程

光标设置是一条「声明 → 收集 → 命中 → 下发」的延迟链路：

```text
元素声明: div().cursor_pointer()          ← 写入 Style.mouse_cursor
     │
     ▼ paint 阶段
div.rs: window.set_cursor_style(style, hitbox)
     │  （只是把 CursorStyleRequest { hitbox_id, style } 压入 next_frame.cursor_styles）
     ▼ 本帧绘制结束
window.rs: reset_cursor_style()
     │  （对 rendered_frame.cursor_style 做命中测试: 鼠标悬停在哪个 hitbox 上）
     ▼ 仅当本窗口被悬停时
cx.platform.set_cursor_style(style)       ← 真正调用平台实现
```

之所以不在 paint 时直接调平台，是因为 paint 阶段元素自底向上绘制，后绘制的元素在视觉层级更靠前；必须等整帧画完，才能用 hitbox 的 stacking 信息选出「视觉最顶层被悬停者」。

拖拽进行中是唯一例外：拖拽光标属于整个窗口，用 `set_window_cursor_style` 声明，优先级高于任何 hitbox 光标。

#### 4.1.3 源码精读

先看契约。`Platform` 上光标相关的四个方法中，前三个是必修，`should_auto_hide_scrollbars` 也是必修（放在 4.3 一起讲）：

- [../gpui/src/platform.rs:299-308](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/platform.rs#L299-L308) — `set_cursor_style(style)` 设置光标样式；`hide_cursor_until_mouse_moves()` 把光标藏到鼠标下次移动为止；`is_cursor_visible()` 查询可见性。三者都没有默认实现，每个平台必须落实。

`CursorStyle` 枚举（节选），每个变体都对齐一个 CSS cursor 值：

```rust
pub enum CursorStyle {
    #[default]
    Arrow,            // default
    IBeam,            // text
    Crosshair,        // crosshair
    ClosedHand,       // grabbing
    OpenHand,         // grab
    PointingHand,     // pointer
    ResizeLeftRight,  // ew-resize
    ...
}
```

见 [../gpui/src/platform.rs:2216-2274](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/platform.rs#L2216-L2274)（枚举还有 `ResizeColumn`、`OperationNotAllowed`、`DragLink`、`DragCopy`、`ContextualMenu` 等变体，完整清单可继续往下读）。

用户侧的声明式 API 由宏生成，最终只是写入样式字段：

- [../gpui_macros/src/styles.rs:159-188](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_macros/src/styles.rs#L159-L188) — 生成 `.cursor(CursorStyle)` 通用方法，以及 `.cursor_default()` / `.cursor_pointer()` / `.cursor_text()` / `.cursor_move()` 等 Tailwind 风格快捷方法；字段落点是 [../gpui/src/style.rs:299](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/style.rs#L299) 的 `pub mouse_cursor: Option<CursorStyle>`。

div 在 paint 阶段把样式翻译成光标请求，拖拽时改用窗口级请求：

```rust
if let Some(drag) = cx.active_drag.as_ref() {
    if let Some(mouse_cursor) = drag.cursor_style {
        window.set_window_cursor_style(mouse_cursor);   // 拖拽: 全窗口生效
    }
} else {
    if let Some(mouse_cursor) = style.mouse_cursor {
        window.set_cursor_style(mouse_cursor, hitbox);  // 常规: 绑定 hitbox
    }
}
```

见 [../gpui/src/elements/div.rs:2446-2454](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/elements/div.rs#L2446-L2454)。`Window::set_cursor_style` 有 `debug_assert_paint()` 守卫，只允许在 paint 阶段调用，且只做记录：

- [../gpui/src/window.rs:3517-3537](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/window.rs#L3517-L3537) — 两个方法都只是把 `CursorStyleRequest` 压进 `next_frame.cursor_styles`；`hitbox_id: None` 表示窗口级请求。

整帧绘制结束时统一结算：

- [../gpui/src/window.rs:2966-2969](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/window.rs#L2966-L2969) — 绘制收尾处调用 `reset_cursor_style`。
- [../gpui/src/window.rs:1066-1079](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/window.rs#L1066-L1079) — `cursor_style()` 对请求列表**倒序**折叠：`hitbox_id` 为 `None` 的窗口级请求立即短路生效（最后声明者优先）；否则取第一个「鼠标实际悬停」的 hitbox 请求。这就是顶层元素覆盖底层元素光标的机制。
- [../gpui/src/window.rs:4954-4963](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/window.rs#L4954-L4963) — `reset_cursor_style` 只在**本窗口被悬停**时才调用平台，避免后台窗口抢别人的光标；没有命中时回落到 `CursorStyle::Arrow`。

`hide_cursor_until_mouse_moves` 的典型触发者是键盘输入——编辑器场景打字时隐藏光标以免遮挡文字：

- [../gpui/src/window.rs:5282-5289](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/window.rs#L5282-L5289) — 按键产生字符且 `cursor_hide_mode` 为 `OnTyping`/`OnTypingAndAction` 时隐藏；
- [../gpui/src/window.rs:5565-5570](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/window.rs#L5565-L5570) — 动作被消费且模式为 `OnTypingAndAction` 时也隐藏。
- 查询侧：[../gpui/src/app.rs:1035-1036](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/app.rs#L1035-L1036) 的 `App::is_cursor_visible` 直接转发平台。

#### 4.1.4 代码实践

**实践目标**：验证「悬停不同区域 → 光标切换」这条链路，并亲手体会声明式 API。

**操作步骤**（示例代码，基于 u1-l2 的独立小 crate，可参考官方示例 [../gpui/examples/drag_drop.rs](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/examples/drag_drop.rs) 的骨架）：

```rust
// 示例代码：三个区域三种光标
struct CursorDemo;

impl Render for CursorDemo {
    fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        div()
            .size_full()
            .flex()
            .flex_col()
            .gap_4()
            .p_4()
            .child(div().id("hand").h_24().bg(gpui::blue()).cursor_pointer().child("手形"))
            .child(div().id("text").h_24().bg(gpui::green()).cursor_text().child("I 形"))
            .child(div().id("move").h_24().bg(gpui::red()).cursor_move().child("抓手"))
    }
}
```

1. 把 u1-l2 的窗口根视图换成上面的 `CursorDemo`。
2. 运行程序（在仓库内也可尝试 `cargo run -p gpui --example drag_drop` 观察官方示例里的 `.cursor_move()`，注意按 u1-l3 的结论可能需要开启 `wayland`/`x11` feature，具体命令待本地验证）。
3. 依次把鼠标悬停到三个色块和窗口空白处。

**需要观察的现象**：悬停蓝色块时手形、绿色块时 I 形、红色块时抓手；移到空白处回到箭头；把鼠标移出窗口，其他应用光标不受影响。

**预期结果**：光标随悬停区域即时切换；若在 div 上叠加两层（例如外层 `.cursor_pointer()`、内层 `.cursor_text()`），悬停在内层时内层样式胜出——对应 4.1.3 中倒序折叠 + hitbox 命中的语义。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Window::set_cursor_style` 用 `debug_assert_paint()` 限制只能在 paint 阶段调用，而不是随时可调？

**答案**：光标请求依赖当帧的 hitbox 布局结果；若在布局/paint 之外随时调用，请求无法与帧的命中盒关联，也无法参与「顶层优先」的折叠结算。收集-结算两段式设计保证了光标永远与最新一帧的视觉层级一致。

**练习 2**：`set_window_cursor_style` 与 `set_cursor_style` 同时存在时谁赢？为什么？

**答案**：窗口级赢。在 [../gpui/src/window.rs:1066-1079](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/window.rs#L1066-L1079) 的倒序折叠中，`hitbox_id == None` 的请求会立即 `Done(Some(...))` 短路返回。拖拽进行中整个窗口应当显示统一的光标（如抓手），不应被途经元素的悬停光标干扰。

**练习 3**：`hide_cursor_until_mouse_moves` 与 `is_cursor_visible` 为什么必须成对出现在契约里？

**答案**：隐藏是异步语义（「直到鼠标移动」），恢复时机由用户后续的鼠标事件决定，平台实现需要自己维护可见性镜像（macOS 用 `AtomicBool`，X11/Wayland 记录 `cursor_hidden_window`）。`is_cursor_visible` 让上层（如编辑器判断是否显示光标相关的 UI）能查询这个平台侧状态。

### 4.2 四个平台如何画同一只光标

#### 4.2.1 概念说明

`CursorStyle` 是唯一公共语言，往下每种窗口系统都有一套自己的资源装载与生效机制。这一节对照五份实现（macOS、Wayland、X11、Windows、Web，外加 headless 的空实现），重点看两件事：**样式如何映射到本地资源**，以及**实现里共同的防御性细节**（光标隐藏时不覆盖、弹出层打开时跳过更新）。

#### 4.2.2 核心流程

各平台生效路径一览：

```text
macOS:   set_cursor_style → 记录到窗口 state → invalidateCursorRectsForView
         → AppKit 回调 resetCursorRects → 映射为 NSCursor 类方法 → addCursorRect
Wayland: cursor-shape-v1 可用 → wp_cursor_shape_device_v1.set_shape(serial, shape)
         不可用 → 从光标主题按名字查位图 → attach 到 wl_surface + set_cursor(serial)
X11:     按名字装载 Xcursor → xcb change_window_attributes(cursor=...) 挂到窗口
Windows: load_cursor → 投递 WM_GPUI_CURSOR_STYLE_CHANGED 消息到窗口线程
Web:     映射为 CSS cursor 字符串 → set_body_cursor
headless: 空实现
```

#### 4.2.3 源码精读

**macOS**。平台方法把活儿转给一个自由函数：

- [../gpui_macos/src/platform.rs:1091-1107](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_macos/src/platform.rs#L1091-L1107) — `set_cursor_style` 调 `set_active_window_cursor_style`；`hide_cursor_until_mouse_moves` 先用 `AtomicBool` 镜像做幂等保护（AppKit 不暴露 `setHiddenUntilMouseMoves:` 的状态，见 [../gpui_macos/src/platform.rs:191](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_macos/src/platform.rs#L191) 的注释），再调 `[NSCursor setHiddenUntilMouseMoves: YES]`。
- [../gpui_macos/src/window.rs:333-362](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_macos/src/window.rs#L333-L362) — 找到 key window（其次 main window），样式变化时向原生窗口发 `invalidateCursorRectsForView:`，请求 AppKit 重算光标区域。
- [../gpui_macos/src/window.rs:2280-2328](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_macos/src/window.rs#L2280-L2328) — `resetCursorRects` 回调里把 `CursorStyle` 逐变体映射为 `NSCursor` 类方法（如 `pointingHandCursor`、`resizeLeftRightCursor`）。注意 L2306-2313 的注释：两个对角 resize 光标用的是 AppKit **未公开的私有类方法**，并附了出处链接——这是跨平台光标覆盖不全时的务实妥协。

**Wayland**。优先走 `cursor-shape-v1` 协议，退回光标主题：

- [../gpui_linux/src/linux/wayland/client.rs:1047-1085](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1047-L1085) — 先做 `need_update` 判断（样式相同或窗口被弹出层占用 `is_blocked` 时跳过），再检查 `cursor_hidden_window`（隐藏期间不覆盖，恢复时从记录读回），最后：有 `cursor_shape_device` 就 `set_shape(serial, shape)`；否则用光标主题位图 `set_icon`。`serial` 取 `SerialKind::MouseEnter`（Wayland 协议要求 set_cursor 携带触发它的输入序列号，详见 u5-l4 的 serial 模块）。
- [../gpui_linux/src/linux/wayland.rs:18-42](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/wayland.rs#L18-L42) — `to_shape` 把 `CursorStyle` 映射为 `cursor-shape-v1` 的 `Shape` 枚举。
- [../gpui_linux/src/linux/wayland/cursor.rs:94-118](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/wayland/cursor.rs#L94-L118) — `Cursor::set_icon` 按候选名字列表在主题里查位图（先专用名后 `left_ptr` 兜底），并按输出缩放重设主题尺寸（构造与主题装载见同文件 [L33-L74](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/wayland/cursor.rs#L33-L74)）。
- 名字列表是 Linux 两后端共享的：[../gpui_linux/src/linux/platform.rs:920-946](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L920-L946) — `cursor_style_to_icon_names` 注释标明命名参考 Chromium 的映射表，每个样式给出多个候选名以兼容不同主题。

**X11**。光标是装载出来的 X 资源 id，挂在窗口属性上：

- [../gpui_linux/src/linux/x11/client.rs:1667-1711](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1667-L1711) — 只对 `mouse_focused_window` 生效；`cursor_styles` 按 `Window` 分表记录；被弹出层阻塞（`is_blocked`，见 [../gpui_linux/src/linux/x11/window.rs:1151-1154](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/x11/window.rs#L1151-L1154)，判定为「存在子表面」）或已隐藏光标时跳过；最后经 XCB `change_window_attributes(cursor=...)` 下发。虽然契约在 `Platform` 上，X11 的实现鲜明地体现了「光标状态按窗口维护」。
- [../gpui_linux/src/linux/x11/client.rs:2014-2076](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L2014-L2076) — `get_cursor_icon` 带缓存，逐候选名装载，全部失败时回落默认 `left_ptr` 并打日志。
- [../gpui_linux/src/linux/x11/client.rs:2089-2098](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L2089-L2098) — 「隐藏」的实现是创建并挂上一个**全透明光标**，记录 `cursor_hidden_window`，鼠标移入事件（L254-255）再恢复。

**Windows**：

- [../gpui_windows/src/platform.rs:783-793](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_windows/src/platform.rs#L783-L793) — `load_cursor` 取得 `HCURSOR` 后通过自定义窗口消息 `WM_GPUI_CURSOR_STYLE_CHANGED` 投递到 owning 线程（Windows 的窗口有线程亲和性），并用比较避免重复设置。
- 隐藏同样有 `AtomicBool` 镜像与逐窗口处理，见 [../gpui_windows/src/platform.rs:795-810](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_windows/src/platform.rs#L795-L810)。

**Web**：

- [../gpui_web/src/platform.rs:509-538](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_web/src/platform.rs#L509-L538) — 逐变体映射成 CSS 字符串（和枚举文档注释一一对应），`set_body_cursor` 设到 body 上；隐藏就是把 cursor 设为 `"none"`（L540-545），可见性恢复时用记录的 `last_cursor_css` 设回。

**headless**：

- [../gpui_linux/src/linux/headless/client.rs:115](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/headless/client.rs#L115) — `set_cursor_style` 空实现；LinuxClient 契约里 `hide_cursor_until_mouse_moves` 与 `is_cursor_visible` 甚至有默认空/true 实现，见 [../gpui_linux/src/linux/platform.rs:83-87](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L83-L87)。

#### 4.2.4 代码实践

**实践目标**：观察 Linux 光标主题这条 fallback 链，或做一次纯源码阅读对照。

**操作步骤**：

1. 阅读 [../gpui_linux/src/linux/wayland/client.rs:1068-1084](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1068-L1084)，回答：`cursor_shape_device` 存在时为什么还需要 `wl_pointer` 与 serial？
2. （Linux 桌面环境可选）运行 4.1.4 的示例，用 `XCURSOR_PATH` 指向一个无效路径再运行一次，对比光标是否回落到默认样式——这正是 [../gpui_linux/src/linux/platform.rs:948-960](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L948-L960) `log_cursor_icon_warning` 专门检查该环境变量的原因。
3. 在 debug 构建下观察终端日志中的 `X11: error loading cursor icon ...` / `Wayland: Failed to load cursor theme` 类告警。

**需要观察的现象**：主题失效时光标仍可用（回落默认箭头），但样式不再随悬停变化或全部变成 `left_ptr`。

**预期结果**：候选名兜底 + 默认名兜底的两级降级生效；日志给出明确告警。非 Linux 平台做第 1、3 步的源码阅读即可（第 2 步待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：X11 实现中 `cursor_styles` 为什么要按窗口分表，而不是像 Wayland 那样只存一个 `Option<CursorStyle>`？

**答案**：X11 客户端在一个连接上管理多个窗口，`set_cursor_style` 只知道「当前鼠标聚焦的窗口」，把样式记在 `mouse_focused_window` 名下，鼠标移到另一个窗口时能查到该窗口自己的样式恢复光标；Wayland 后端的 `cursor_style` 是单值（[../gpui_linux/src/linux/wayland/client.rs:1050-1061](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1050-L1061)），因为 Wayland 的光标是按 seat/pointer 表面设置的，进入新窗口时 GPUI 会重新走 paint → reset 链路。这也再次说明契约放在 `Platform` 而实现按窗口维护的理由。

**练习 2**：三个桌面实现都有一句「Don't clobber the invisible cursor」式的检查（X11 在 [client.rs:1691](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1691)，Wayland 在 [client.rs:1064](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1064)）。它防的是什么竞态？

**答案**：防止「打字隐藏光标」期间一次普通的光标样式更新把隐藏状态顶掉。做法是：隐藏期间照常记录新样式到 `cursor_styles`/`cursor_style`，但不动实际光标；等鼠标移动恢复时再从记录读回最新样式。

### 4.3 窗口外观：查询人人有份，覆盖仅 macOS

#### 4.3.1 概念说明

`window_appearance()` 回答「应用窗口当前是什么外观」，主题系统据此选择浅色/深色主题；`set_window_appearance(Some(...))` 则允许应用**覆盖**系统设置，传 `None` 恢复跟随系统。

为什么覆盖只属于 macOS？看契约文档自己怎么说：

> Currently only implemented on macOS, where it sets `NSApplication.appearance` so the native window chrome (the window border and titlebar) of every window matches a dark app theme even when the system is in light mode (or vice versa). A no-op on other platforms.

原因在 2.4 节已经给出：macOS 的窗口 chrome（边框、标题栏）由 AppKit 渲染，应用主题切到深色而系统是浅色时，原生 chrome 仍是浅色，需要平台级覆盖把它们对齐；Linux/Windows 上 GPUI 客户端自绘标题栏，主题一改 chrome 自然跟着改，平台层无事可做，于是用默认空实现。

#### 4.3.2 核心流程

查询链（四个平台都实现了 `window_appearance`，数据来源各不相同）：

```text
App::window_appearance (app.rs:1371)
  └─ Platform::window_appearance (platform.rs:169, 必修)
       ├─ macOS:   [NSApplication effectiveAppearance] → 按名字映射（含覆盖后的值）
       ├─ Linux:   LinuxCommon.appearance ← xdg-desktop-portal 的 color-scheme 设置
       ├─ Windows: system_appearance()（系统深浅色设置）
       └─ Web:     matchMedia("(prefers-color-scheme: dark)")
```

Linux 的事件链（外观是「被推送」过来的）：

```text
XDPEventSource（后台任务读 portal Settings）
  → 初始 color-scheme + receive_color_scheme_changed 订阅变更
  → calloop channel 发 XDPEvent::WindowAppearance
  → client 更新 LinuxCommon.appearance，并对每个窗口 window.set_appearance(...)
  → 触发窗口的 appearance_changed 回调（gpui 的 Window 层据此通知观察者）
```

覆盖链（仅 macOS）：`App::set_window_appearance` → `[NSApp setAppearance:]`，`None` 传 nil 即清除覆盖；之后 `window_appearance()` 读到的 `effectiveAppearance` 就包含覆盖效果，形成闭环。

#### 4.3.3 源码精读

契约与模型：

- [../gpui/src/platform.rs:168-179](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/platform.rs#L168-L179) — `window_appearance` 必修、`set_window_appearance` 默认空体，文档明确「currently only implemented on macOS」。
- [../gpui/src/platform.rs:2072-2098](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/platform.rs#L2072-L2098) — `WindowAppearance` 四变体：`Light`/`Dark` 与带「vibrant（鲜活渲染）」的 `VibrantLight`/`VibrantDark`，后两者是 macOS 特有概念，其他平台不会产生。
- [../gpui/src/app.rs:1370-1387](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/app.rs#L1370-L1387) — `App` 层纯转发，文档再次解释了 macOS 的使用场景（应用深色主题 + 系统浅色时对齐窗口边缘）。

macOS 覆盖实现：

- [../gpui_macos/src/platform.rs:680-709](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_macos/src/platform.rs#L680-L709) — 查询读 `effectiveAppearance`；覆盖时按枚举变体取外观名字（`NSAppearanceNameAqua`/`DarkAqua`/Vibrant 两个）创建 `NSAppearance` 并 `setAppearance:`，`None` 传 nil 清除。注意 `NSAppearanceNameAqua`/`DarkAqua` 两个静态量是 crate 手工 extern 声明的（[../gpui_macos/src/window_appearance.rs:31-35](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_macos/src/window_appearance.rs#L31-L35)）。
- [../gpui_macos/src/window_appearance.rs:10-29](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_macos/src/window_appearance.rs#L10-L29) — 反向映射：拿 `NSAppearance` 的 `name` 与四个已知名字比对；未知名字打印日志并回落 `Light`——一个典型的「能力探测 + 降级」。

Linux 查询与事件源：

- [../gpui_linux/src/linux/platform.rs:723-725](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L723-L725) — 查询只是读 `LinuxCommon.appearance`；Linux **没有**覆盖 `set_window_appearance`，落到契约默认空体——这就是「非 macOS 上 no-op」的第一手证据。
- [../gpui_linux/src/linux/xdg_desktop_portal.rs:33-53](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/xdg_desktop_portal.rs#L33-L53) — 启动时读 portal 的 `color-scheme` 得到初始外观，同一来源还顺带读取 `cursor-theme`、`cursor-size`、`button-layout`（注意：Linux 的**光标主题与尺寸**也是从这里来的，呼应 4.2 的 Wayland 光标主题装载）；[L119-L122](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/xdg_desktop_portal.rs#L119-L122) 订阅后续变更。
- [../gpui_linux/src/linux/xdg_desktop_portal.rs:185-191](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/xdg_desktop_portal.rs#L185-L191) — `PreferDark → Dark`，`PreferLight`/`NoPreference → Light`：Linux 没有 vibrant 概念，永远只产生两个值。
- [../gpui_linux/src/linux/wayland/client.rs:798-810](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L798-L810) — 收到 `XDPEvent::WindowAppearance` 后更新 `common.appearance` 并逐窗口 `set_appearance`；X11 侧同构逻辑见 [../gpui_linux/src/linux/x11/client.rs:488-492](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L488-L492)。
- 窗口级外观状态与回调（承接 u3-l2 的 on_* 模式）：X11 的 `appearance` 字段与 `set_appearance` 见 [../gpui_linux/src/linux/x11/window.rs:1341-1351](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/x11/window.rs#L1341-L1351)，查询在 [L1444-L1445](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/x11/window.rs#L1444-L1445)，变化回调注册在 [L1716-L1717](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/x11/window.rs#L1716-L1717)。

其他平台的查询：

- Windows：[../gpui_windows/src/platform.rs:582-584](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_windows/src/platform.rs#L582-L584) — `system_appearance()` 失败时回落默认（Light）。
- Web：[../gpui_web/src/platform.rs:399-408](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_web/src/platform.rs#L399-L408) — `matchMedia("(prefers-color-scheme: dark)")`，查询失败也回落 Light。

顺带把 4.1 留下的 `should_auto_hide_scrollbars`（「是否应自动隐藏滚动条」，滚动条 UI 的降级依据）在这里一次看完，它与外观同属「系统观感查询」：

| 平台 | 实现 | 位置 |
| --- | --- | --- |
| macOS | `NSScroller preferredScrollerStyle == Overlay` | [../gpui_macos/src/platform.rs:1113-1121](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_macos/src/platform.rs#L1113-L1121) |
| Windows | `UISettings.AutoHideScrollBars` | [../gpui_windows/src/platform.rs:820-821](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_windows/src/platform.rs#L820-L821)、[L1394-L1397](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_windows/src/platform.rs#L1394-L1397) |
| Linux | `LinuxCommon.auto_hide_scrollbars`，初始化为 `false`，仓库内无其他写入点 | [../gpui_linux/src/linux/platform.rs:653-655](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L653-L655)、[L165](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L165) |
| Web | 直接返回 `true` | [../gpui_web/src/platform.rs:551-553](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_web/src/platform.rs#L551-L553) |

App 层转发在 [../gpui/src/app.rs:1596-1597](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/app.rs#L1596-L1597)。

#### 4.3.4 代码实践

**实践目标**：亲手验证「`set_window_appearance` 在非 macOS 上是 no-op」这一文档承诺。

**操作步骤**（示例代码，接 4.1.4 的独立 crate）：

```rust
// 示例代码：点击按钮切换外观覆盖，标签实时显示查询值
struct AppearanceDemo { dark: bool }

impl Render for AppearanceDemo {
    fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let appearance = cx.window_appearance();      // 查询: 转发 Platform::window_appearance
        div()
            .size_full().flex().flex_col().gap_4().p_4()
            .child(format!("当前外观: {:?}", appearance))
            .child(
                div().id("toggle").w_32().h_10().bg(gpui::blue())
                    .cursor_pointer()
                    .child("强制深色 / 恢复跟随系统")
                    .on_click(cx.listener(|this, _, _, cx| {
                        this.dark = !this.dark;
                        if this.dark {
                            cx.set_window_appearance(Some(gpui::WindowAppearance::Dark));
                        } else {
                            cx.set_window_appearance(None);
                        }
                        cx.notify();
                    })),
            )
    }
}
```

1. 在你的操作系统上编译运行。
2. 点击按钮前记录 `cx.window_appearance()` 的打印值；点击后再记录一次；再点一次（传 `None`）后记录第三次。
3. macOS 上额外观察原生标题栏与边框的颜色变化；非 macOS 平台观察程序行为与界面。

**需要观察的现象**：

- macOS：点击后查询值变为 `Dark`，且**原生标题栏**变深（这是该接口存在的意义）；传 `None` 后恢复。
- Linux/Windows：点击后查询值**保持不变**（Linux 仍返回 portal 推送的值，Windows 仍返回系统设置值），界面无任何反应——空实现不产生副作用。

**预期结果**：与契约文档一致——覆盖只在 macOS 生效，其他平台 no-op。若你在 Linux 上把系统切到深色主题，查询值会经 portal 事件链自动变成 `Dark`，与按钮无关，这正说明 Linux 的外观是「只读跟随」模型。

#### 4.3.5 小练习与答案

**练习 1**：Linux 的 `window_appearance()` 从不调用任何系统 API，它是怎么知道深色模式的？

**答案**：外观值存放在 `LinuxCommon.appearance`，来源是 `XDPEventSource` 后台任务通过 xdg-desktop-portal 的 Settings 接口读取/订阅的 `color-scheme`（[../gpui_linux/src/linux/xdg_desktop_portal.rs:33-39](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/xdg_desktop_portal.rs#L33-L39)），事件经 calloop channel 推回前台后写入 common 并逐窗口分发（[../gpui_linux/src/linux/wayland/client.rs:798-810](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L798-L810)）。查询只是读缓存。

**练习 2**：`WindowAppearance::VibrantDark` 在 Linux/Web 上会出现吗？

**答案**：不会。Linux 的映射函数只产生 `Light`/`Dark`（[xdg_desktop_portal.rs:185-191](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/xdg_desktop_portal.rs#L185-L191)），Web 的 matchMedia 同样只有两态；Vibrant 是 macOS 独有的渲染概念，主题代码在非 macOS 平台只需处理两个值。

**练习 3**：macOS 的 `set_window_appearance(None)` 为什么语义是「恢复跟随系统」而不是「强制浅色」？

**答案**：实现里 `None` 分支把 nil 传给 `setAppearance:`（[../gpui_macos/src/platform.rs:691-707](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_macos/src/platform.rs#L691-L707)），NSApplication 的 appearance 为 nil 时使用系统默认行为，之后 `effectiveAppearance` 会重新反映系统深浅设置——所以契约文档写「Pass None to clear the override and follow the system again」。

### 4.4 外部拖放：ExternalDragPayload 与拖拽晋升

#### 4.4.1 概念说明

GPUI 的 `on_drag`/`on_drop` 是**进程内**拖拽：拖拽影像是 GPUI 视图，鼠标移出窗口后拖拽就到了尽头。但真实应用需要把一个文件拖到桌面、 Finder 或别的编辑器里——这就必须把拖拽「晋升」为操作系统的 DnD 会话。

`ExternalDragPayload` 就是交给操作系统的负载模型。它目前只有一个变体 `Files(FileDragPaths)`：真实磁盘路径加上「是否目录」的标注。为什么目录信息要调用方提供？`FileDragPaths` 的文档注释说得直白：*Directory metadata is provided by the caller to avoid querying it when the platform drag starts*——晋升发生在鼠标移动的事件处理里，同步查文件系统会卡输入。

#### 4.4.2 核心流程

完整链路（四个前置条件缺一不可）：

```text
① 注册: div().on_drag(value, ...).external_drag_payload(resolver)
        （resolver 必须在 on_drag 之后调用, 且类型与 on_drag 一致）
② 启动: 鼠标按下并移动 → active_drag = AnyDrag {
            value, cursor_style,
            external_payload_source: 包装后的 resolver }
③ 晋升（每次 MouseMove 末尾检查 promote_external_drag_to_platform）:
    a. 事件是 MouseMove 且左键按下
    b. 鼠标位置已在视口之外
    c. platform_window.can_start_external_drag() == true
    d. active_drag 里确有 external_payload_source, 且 resolver 返回 Some(payload)
④ 移交: platform_window.start_external_drag(&payload) 成功
        → hand_active_drag_to_platform: active_drag 变为 PlatformOwnedDrag::Suspended
        （之后若拖回源窗口, 还能经 restore_platform_drag 恢复内部拖拽）
```

反向（拖入）由 `FileDropEvent`（`Entered`/`Pending`/`Exited`/`Ended`）承载，与本讲 outbound 链路互为镜像，这里只指路不展开：[../gpui/src/interactive.rs:726-739](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/interactive.rs#L726-L739)。

#### 4.4.3 源码精读

数据模型：

- [../gpui/src/interactive.rs:694-717](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/interactive.rs#L694-L717) — `ExternalDragPayload::Files(FileDragPaths)`；`FileDragPaths::new([(PathBuf, bool); N])` 以「路径 + 是否目录」二元组列表构造。

注册侧的防御性检查：

- [../gpui/src/elements/div.rs:617-643](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/elements/div.rs#L617-L643) — `external_drag_payload` 要求先有 `on_drag`（否则 debug_assert 报错）、拖拽值类型必须与 `on_drag` 一致、同一元素只能注册一次；fluent 等价方法在 [L1576-L1585](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/elements/div.rs#L1576-L1585)。
- [../gpui/src/elements/div.rs:2855-2882](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/elements/div.rs#L2855-L2882) — 内部拖拽启动时，把 resolver 连同被拖值一起捕获进 `AnyDrag.external_payload_source`（类型定义见 [../gpui/src/app.rs:2946-2968](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/app.rs#L2946-L2968)）。payload 是**懒解析**的：只有真的拖出窗口才会调用 resolver。

晋升闸门：

- [../gpui/src/window.rs:5134-5136](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/window.rs#L5134-L5136) — 注释点明时序：必须在 move 事件分发**之后**检查，「平台随后拥有这个手势，这是拖拽监听器看到指针离开并复位状态的最后机会」。
- [../gpui/src/window.rs:5151-5179](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/window.rs#L5151-L5179) — 依次校验 ③ 的 a/b/c/d 四关；`start_external_drag` 成功后调 `hand_active_drag_to_platform` 并刷新。
- [../gpui/src/app.rs:2526-2535](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/app.rs#L2526-L2535) — 移交：`active_drag` 挪进 `PlatformOwnedDrag::Suspended`；拖回源窗口时由 `restore_platform_drag`（[L2537-L2554](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/app.rs#L2537-L2554)）原样恢复。

平台契约（注意它们声明在 platform.rs 的「Linux specific methods」注释段内，但**没有 cfg 门控**，任何平台都可覆盖；默认全 false，即默认不支持）：

- [../gpui/src/platform.rs:908-913](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/platform.rs#L908-L913) — `can_start_external_drag` 默认 `false`、`start_external_drag` 默认 `false`。

两份真实实现：

- **macOS**：[../gpui_macos/src/window.rs:2009-2059](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_macos/src/window.rs#L2009-L2059) — 空列表与「没有留存最近的左键按下事件」都拒绝（AppKit 的拖拽影像要锚定在该事件上，见 L2042-2048 注释）；随后逐路径构造 `NSURL` 塞进 `NSMutableArray` 发起 `NSDraggingItem` 拖拽，非 UTF-8 路径跳过并告警。
- **Wayland**：[../gpui_linux/src/linux/wayland/window.rs:1777-1784](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/wayland/window.rs#L1777-L1784) 转发到 client，[../gpui_linux/src/linux/wayland/client.rs:444-482](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L444-L482) — 把路径列表转成 `text/uri-list`，创建 `wl_data_source` 并 offer 该 MIME 类型、声明 Copy|Move 动作，最后 `data_device.start_drag(..., serial)`；serial 取 `SerialKind::MousePress`（Wayland 要求 start_drag 携带触发它的按键序列号）。
- **X11 / Windows / Web**：均未覆盖 → `can_start_external_drag()` 用默认值 `false`，晋升闸门在 c 关就被拦下：在这些平台上拖出窗口不会产生原生拖拽。

测试设施（如何在没有操作系统的情况下验证这条链路）：

- [../gpui/src/platform/test/window.rs:404-416](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/platform/test/window.rs#L404-L416) — 测试窗口的 `start_external_drag` 把文件记进 `external_drag_files`，返回值可由 `set_start_external_drag_result` 预置，用来模拟平台成功/失败两种结局。
- [../gpui/src/window.rs:7157-7186](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/window.rs#L7157-L7186) — `FileDragView`：`.on_drag(PathBuf)` + `.external_drag_payload(...)` 的标准用法；随后 [L7188](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/window.rs#L7188) 起的测试 `file_drag_is_promoted_once_and_restored_in_source_window` 用它验证「只晋升一次 + 拖回源窗口可恢复」。

#### 4.4.4 代码实践

**实践目标**：把一次内部拖拽变成真正的系统级文件拖拽，并观察平台差异。

**操作步骤**：

1. 先跑测试（源码阅读型）：在仓库根执行 `cargo test -p gpui file_drag_is_promoted_once_and_restored_in_source_window`，对照 [../gpui/src/window.rs:7188-7245](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/window.rs#L7188-L7245) 阅读断言（运行耗时与环境依赖待本地验证）。
2. 再动手（示例代码，替换 4.1.4 demo 的根视图；需要一个真实存在的文件路径）：

```rust
// 示例代码：可拖出到系统的文件卡片
struct FileCard { path: PathBuf }

impl Render for FileCard {
    fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        let path = self.path.clone();
        div()
            .id("file-card")
            .w_64().h_16()
            .bg(gpui::gray())
            .cursor_move()                       // 4.1: 悬停抓手
            .child(format!("拖我: {}", path.display()))
            .on_drag(path.clone(), |_, _, _, cx| cx.new(|_| gpui::Empty))
            .external_drag_payload(move |p: &PathBuf, _, _| {
                Some(gpui::ExternalDragPayload::Files(
                    gpui::FileDragPaths::new([(p.clone(), false)]),
                ))
            })
    }
}
```

3. 运行程序，按住卡片拖出窗口边缘，一直到桌面或一个文件管理器窗口上松手。

**需要观察的现象**：

- macOS / Wayland（带 DnD 的合成器）：出窗口瞬间拖拽影像变为系统绘制，松手后目标位置出现文件（或复制副本）；
- X11 / Windows / Web：拖出窗口后拖拽直接结束，系统无反应——因为 `can_start_external_drag()` 是默认 `false`。

**预期结果**：与 4.4.3 的平台矩阵一致。另外可在 Wayland 上验证 serial 的必要性：若合成器拒绝无有效 serial 的 start_drag，拖拽不会开始（这属于协议侧约束，具体表现待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `external_drag_payload` 的 resolver 要到晋升那一刻才执行，而不是拖拽开始时就解析好？

**答案**：两个原因。其一，绝大多数拖拽都在窗口内结束，永远不会需要平台负载，懒解析省掉无谓工作；其二，晋升点发生在 MouseMove 事件处理里，`FileDragPaths` 让调用方预先标好「是否目录」，避免在这条输入热路径上同步查文件系统（[../gpui/src/interactive.rs:702-705](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/interactive.rs#L702-L705) 的文档注释）。此外 `AnyDrag` 里 resolver 是 `FnOnce`，类型上就只允许消费一次。

**练习 2**：晋升成功后，GPUI 内部的拖拽状态去哪了？拖回源窗口会怎样？

**答案**：`hand_active_drag_to_platform` 把 `active_drag` 移进 `PlatformOwnedDrag::Suspended`（[../gpui/src/app.rs:2526-2535](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/app.rs#L2526-L2535)），此后手势归平台；指针带着拖拽回到源窗口时 `restore_platform_drag` 把内部拖拽原样恢复（[L2537-L2554](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/app.rs#L2537-L2554)），测试名 `file_drag_is_promoted_once_and_restored_in_source_window` 概括了这两个保证。

**练习 3**：X11 后端为什么「没实现」外部拖拽也能通过编译？

**答案**：`can_start_external_drag`/`start_external_drag` 在 `PlatformWindow` trait 上带默认实现（返回 false，[../gpui/src/platform.rs:908-913](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/platform.rs#L908-L913)），不覆盖就是「声明不支持」。这是 u2-l1 总结过的「能力探测型默认实现」：调用方（window.rs 的晋升闸门）先问 `can_start_external_drag()`，答案为否就安静地走内部拖拽的旧路，不需要任何条件编译。

## 5. 综合实践

把本讲三个模块串成一个「文件卡片面板」：

1. **面板骨架**：一个纵向列表，每张卡片代表一个磁盘文件（路径取自命令行参数或写死的几个临时文件）。
2. **光标（4.1/4.2）**：卡片本体 `.cursor_move()`，卡片右上角一个「重命名」小图标区域 `.cursor_pointer()`，卡片之间的间隔条 `.cursor_text()`；验证嵌套元素的光标覆盖关系。
3. **外观（4.3）**：面板顶部放一个「深色窗口 chrome」开关，点击调用 `cx.set_window_appearance(Some(WindowAppearance::Dark) / None)`，旁边标签实时显示 `cx.window_appearance()`；在 macOS 与非 macOS 各跑一遍，把两者的行为差异（原生标题栏是否变色、查询值是否变化）写成笔记。
4. **拖放（4.4）**：每张卡片 `.on_drag(PathBuf)` + `.external_drag_payload(...)`（目录卡片记得把「是否目录」标对），并给面板整体挂一个 `on_drop` 区域接收**别的应用拖进来的**文件（提示：走 `FileDropEvent` 方向，从 [../gpui/src/interactive.rs:726-739](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_platform/../gpui/src/interactive.rs#L726-L739) 入手查 `on_drop`/`FileDropEvent` 在 div 上的接线）。
5. **验收清单**：三种光标正确切换并符合层级；外观开关在非 macOS 上无副作用、在 macOS 上连标题栏一起变暗；卡片能拖到桌面生成文件（macOS/Wayland）或在 X11 上明确记录「不支持」的观察结论。

## 6. 本讲小结

- 光标样式是「paint 期声明、帧末结算」的延迟链路：`.cursor()` 写入样式 → div 在 paint 期借 hitbox 登记 `CursorStyleRequest` → 绘制收尾 `reset_cursor_style` 做倒序命中折叠 → 只有被悬停的窗口才调用 `Platform::set_cursor_style`；拖拽中的 `set_window_cursor_style` 一律优先。
- 同一个 `CursorStyle` 在五个平台各有一套落地：macOS NSCursor cursor-rect、Wayland cursor-shape-v1 或光标主题位图、X11 XCursor 资源挂窗口属性、Windows 窗口消息换 HCURSOR、Web 直接映射 CSS；桌面实现共享「隐藏期间不覆盖、弹出层期间跳过」两条防御性守卫。
- `hide_cursor_until_mouse_moves` / `is_cursor_visible` 服务「打字藏光标」场景，平台需自维护可见性镜像；`should_auto_hide_scrollbars` 是观感查询，四平台各有来源（overlay 滚动条样式 / UISettings / 固定 false / 固定 true）。
- `window_appearance` 查询四平台都实现（macOS effectiveAppearance、Linux portal color-scheme、Windows 系统设置、Web prefers-color-scheme）；`set_window_appearance` 只有 macOS 覆盖——因为只有 macOS 的窗口 chrome 由系统渲染、需要平台级对齐，其余平台落到默认空实现（no-op）。
- `ExternalDragPayload`（当前仅 `Files`）由 `.on_drag` + `.external_drag_payload` 懒注册；拖拽出视口时 `promote_external_drag_to_platform` 过四道闸门后把内部拖拽移交平台（`PlatformOwnedDrag::Suspended`），macOS 与 Wayland 支持，X11/Windows/Web 走默认 `can_start_external_drag() == false`。

## 7. 下一步学习建议

本讲结单元 3。接下来：

- **单元 4（调度与并发）**：光标与拖拽链路里反复出现的「事件循环投递」（Windows 的 `WM_GPUI_CURSOR_STYLE_CHANGED`、portal 的 calloop channel）将在 u4-l2 `PlatformDispatcher` 中系统展开。
- **u5-l4（Wayland 客户端）**：本讲多次出现的 `serial_tracker`、`data_device`、layer_shell 将在 Wayland 协议全景中讲解。
- **u5-l5（xdg-desktop-portal）**：Linux 外观/光标主题/按钮布局共同的 portal 事件源值得一篇完整讲义。
- **反向拖入**：若想继续拖放主题，可从 `FileDropEvent` 的平台侧实现（如 macOS 的 dragging destination、Wayland 的 data_offer）追踪「拖入」链路，作为自学小项目。
