# 悬浮层：Popover / Tooltip / HoverCard

## 1. 本讲目标

学完本讲，你应当能够：

- 用 `Popover` 创建一个「点击/右键触发」的弹出层，理解它是**非模态**（不遮罩、不独占焦点）的轻量浮层，并掌握它的受控（`open` + `on_open_change`）与非受控（`default_open`）两种用法。
- 为任意元素（尤其是 `Button` 这类组件，也包含普通 `div`）加上 `Tooltip`，理解 gpui-component 为什么用一个**每窗口唯一的 `TooltipOverlay`** 来统一托管所有 tooltip 的延迟、动效与定位。
- 用 `HoverCard` 实现「鼠标悬停一段时间后弹出富内容卡片」，理解它为什么能在鼠标从触发器移到卡片正文上时不被误关，以及它如何复用 Popover 的渲染管线。
- 在三种组件之间做出正确选型，并说清它们与 u5-1 的 `Dialog`（模态、抢焦点、遮罩）的本质差别。

## 2. 前置知识

本讲假设你已经掌握：

- **`init(cx)` 与 `Root` 管理壳**（u1-4）：`Root` 是窗口顶层视图，集中托管 Sheet / Dialog / Notification / **tooltip overlay** 等窗口级状态；`gpui_component::init(cx)` 负责初始化这些子系统。
- **无状态 `RenderOnce` 组件 + 状态外置**（u2-2、u3-1）：组件每帧 `render(self)` 重建，自身不持跨帧状态。本讲的 `Popover` / `HoverCard` 都是 `RenderOnce`，状态分别外置到 `PopoverState` / `HoverCardState`（经 `window.use_keyed_state` 托管）。
- **Styled 与 `popover_style`**（u2-2）：组件统一用 `cx.theme()` 取语义色；`popover_style(cx)` 一行套上「popover 背景 + 文字色 + 1px 边框 + 大阴影 + 圆角」的浮层默认外观。
- **`ElementExt::on_prepaint`**（u2-4）：`on_prepaint` 往容器塞一个铺满透明的探针 `canvas`，绘制后回调出元素的像素矩形 `Bounds<Pixels>`。本讲所有「定位浮层」都靠它先量出触发器的边界。
- **Button 家族**（u3-1）：实践环节会用 `Button::new(...).outline().label(...)` 当触发器。
- **GPUI 的 Action / KeyBinding**（u1-4、u2-4）：`actions!` 宏定义动作，`cx.bind_keys` 绑按键，`key_context(...)` 把绑定限定在某个上下文内。`Popover` 用这套实现「按 ESC 关闭」。

> 一个贯穿全讲的关键概念：**模态 vs 非模态**。u5-1 的 `Dialog` 是模态浮层——它抢走键盘焦点、盖一层遮罩、打断用户。本讲三个组件全是**非模态**：不盖遮罩、不抢焦点、点击外部（或鼠标移开）即消失。它们的复杂度集中在「**怎样把一块内容精确定位到触发器旁边、并在合适的时机显示/隐藏**」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [popover.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs) | `Popover` 组件主体与 `PopoverState`：触发器、锚点定位（`resolved_corner`）、受控/非受控开关、ESC 关闭、`DismissEvent`，以及被 HoverCard 复用的 `render_popover` / `render_popover_content`。 |
| [tooltip.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs) | `Tooltip` 视图（文本/自定义元素 + 快捷键）、每窗口唯一的 `TooltipOverlay`（延迟、宽限期、进出/横向滑动动画）、`TooltipOverlayPositioner` 自定义定位元素、`ManagedTooltipExt` 扩展 trait 与 `ComponentTooltip`。 |
| [hover_card.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/hover_card.rs) | `HoverCard` 组件与 `HoverCardState`：悬停触发、`open_delay`/`close_delay`、触发器与正文双悬停桥接、epoch 计时器取消，复用 Popover 的渲染管线。 |
| [root.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs) | `Root` 持有唯一的 `tooltip_overlay: Entity<TooltipOverlay>` 并在 `render` 里把它作为一层画出来；`tooltip_overlay(window, cx)` 访问器供 `ManagedTooltipExt` 回调。 |
| [styled.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs) | `popover_style(cx)`：Popover / HoverCard 共用的浮层默认外观（`bg=tokens.popover`、`text=popover_foreground`、1px 边框、`shadow_lg`、`radius`）。 |
| [popover_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/popover_story.rs) / [tooltip_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/tooltip_story.rs) / [hover_card_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/hover_card_story.rs) | Story Gallery 里的真实范例，是本讲实践与示例的依据。 |

## 4. 核心概念与源码讲解

三个组件要解决同一个问题——**把一块内容临时显示到触发器旁边**——但触发方式、生命周期和定位机制各不相同：

| 组件 | 触发 | 生命周期 | 定位机制 | 焦点 |
| --- | --- | --- | --- | --- |
| `Popover` | 鼠标按下（点击/右键） | 受控/非受控开关；点外部、ESC、`DismissEvent` 关闭 | `deferred(anchored())` + `resolved_corner`（触发器锚点角） | 可 `track_focus`，开关时保存/还原焦点 |
| `Tooltip` | 鼠标悬停 | 悬停延时显示、移开延时隐藏；同一窗口**只显示一个** | 自定义 `TooltipOverlayPositioner`（上方/下方自动翻转） | 不抢焦点 |
| `HoverCard` | 鼠标悬停 | 延时显示/隐藏，可悬停到正文上不关 | 复用 Popover 的 `render_popover` + `resolved_corner` | 不抢焦点 |

一个贯穿全讲的实现要点：**浮层内容不能直接画在触发器所在的普通布局流里**，否则会撑开父容器、影响布局。gpui-component 用两种手段把浮层「抽离」出正常布局流：

- Popover / HoverCard：用 GPUI 的 `deferred(anchored())`，把内容**延迟到本帧所有正常元素画完之后**再画，并用 `anchored` 让它脱离父容器、吸附到窗口坐标。
- Tooltip：由 `Root` 里唯一的 `TooltipOverlay` 实体统一渲染，同样用 `deferred` 延迟绘制，但定位用一个自写的 `TooltipOverlayPositioner` 元素算偏移。

下面按三个最小模块逐个精读。

### 4.1 Popover：点击触发的弹出层

#### 4.1.1 概念说明

`Popover` 是一个**无状态 `RenderOnce` 组件**（派生 `IntoElement`，并实现 `Styled` 与 `ParentElement`），自身不持跨帧状态。它的关键字段分几组：

[popover.rs:19-42](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L19-L42) `Popover` 结构体：`id`、`anchor`（锚点角）、`default_open` / `open`（两套开关）、`trigger`（触发器闭包）、`content`（正文闭包）、`mouse_button`、`appearance`、`overlay_closable`、`tracked_focus_handle`、`on_open_change`。

真正持有「开/关」等跨帧状态的是 `PopoverState`，它通过 `window.use_keyed_state(id, ...)` 按 id 缓存在窗口上（同一个 id 的 Popover 复用同一个 state）：

[popover.rs:207-217](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L207-L217) `PopoverState`：`focus_handle`、`previous_focus_handle`（关闭时还原焦点）、`trigger_bounds`（触发器像素边界）、`open` 开关、`on_open_change` 回调、以及一个 `DismissEvent` 订阅。

> 术语：**锚点角（anchor corner）**。`Anchor` 枚举有 `TopLeft / TopCenter / TopRight / BottomLeft / BottomCenter / BottomRight / LeftCenter / RightCenter` 等取值。它的作用是决定「浮层以触发器边界的哪个角作为定位原点」。默认 `TopLeft`：浮层出现在触发器的左上角附近（即正下方偏左）。

#### 4.1.2 核心流程

一次「点击触发器 → 弹出 → 点外部关闭」的全流程：

```
鼠标按下触发器
   │  on_mouse_down(mouse_button)
   ▼
toggle_open
   │  开启时：
   │    1) previous_focus_handle = window.focused()   # 记下当前焦点
   │    2) set_open(true)
   │       └─ GlobalState::register_deferred_popover  # 登记「我用了 deferred 渲染」
   │    3) 把焦点移到 tracked 或自身 focus_handle
   │    4) 订阅 DismissEvent（别人 emit 时关掉自己）
   │    5) 调 on_open_change(true)
   ▼
每帧 RenderOnce::render
   │  on_prepaint 量出 trigger_bounds，写入共享 Cell
   │  首次量到边界时 request_animation_frame（修正首帧定位）
   │  若 open && 已量到边界：
   │    render_popover_content（popover_style + tab_group + key_context("Popover")）
   │      ├─ track_focus(&focus_handle)
   │      ├─ on_action(Cancel)        # ESC → dismiss
   │      ├─ overlay_closable 时 on_mouse_down_out → dismiss
   │      └─ content(state,...) / children
   │    render_popover: deferred(anchored().position(resolved_corner(anchor, bounds)))
   ▼
关闭（三种来源之一）：
   ESC → on_action_cancel → dismiss
   点外部 → on_mouse_down_out → dismiss
   正文 emit DismissEvent → 订阅回调 → dismiss
        │
        ▼
toggle_open(false)
   取消 DismissEvent 订阅；unregister_deferred_popover
   若焦点还在自身 → 还原到 previous_focus_handle
   调 on_open_change(false)
```

**关键结论**：Popover 的「开/关」是 `PopoverState.open` 一个布尔值；定位依赖 `on_prepaint` 先量出触发器边界，再用 `resolved_corner` 算出浮层原点，最后交给 `deferred(anchored())` 延迟绘制。

#### 4.1.3 源码精读

**(1) 锚点定位：`resolved_corner`。** 给定触发器边界和一个锚点角，算出浮层的定位原点：

[popover.rs:172-192](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L172-L192) `resolved_corner`：`TopLeft` 取 `origin`，`TopCenter` 取 `top_center`，`BottomLeft/BottomCenter/BottomRight` 把 `y` 减去触发器高度（折到触发器上方）。`LeftCenter/RightCenter` 暂回退到 `origin`。

`(Bottom*)` 的 y 坐标减去 `trigger_bounds.size.height`，是因为这些锚点要让浮层出现在触发器**上方**，而锚点是以「触发器左上角 + 尺寸」描述的，需要把原点向上平移一个触发器高度。

**(2) 受控 vs 非受控。** 两种开关写法：

[popover.rs:97-100](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L97-L100) `default_open`：仅初始化 `PopoverState.open`，之后由组件内部自行翻转（非受控）。

[popover.rs:107-110](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L107-L110) `open`：强制设定开关（受控）。文档明确要求**必须配合 `on_open_change`**，否则内部翻转后的状态无法同步回你的 View，会出现「点开后又自动弹回」的死循环。

[popover.rs:117-123](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L117-L123) `on_open_change`：回调第一个参数是**新开关状态**。受控模式下用它把状态写回 View，再下帧喂回 `.open(...)`，形成闭环（和 u3-1 Button、u4-2 Switch 的「状态外置 + 回调同步」范式一致）。

**(3) 正文闭包每帧执行。** `content` 的签名是把 `&mut PopoverState, &mut Window, &mut Context<PopoverState>` 交给你的闭包：

[popover.rs:141-150](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L141-L150) `content`：闭包在**每次渲染**都会被调用。

> 这条注释很重要：因为每帧都跑，所以**不要在闭包里 `cx.new(...)` 新建实体**——那会每帧创建一个新实体，导致状态丢失、内存泄漏。要用的实体应该在 Popover 之外（外层 View）建好，闭包只负责「引用」。

**(4) 触发器要求实现 `Selectable`。** `trigger` 的类型约束是 `Selectable + IntoElement`：

[popover.rs:81-90](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L81-L90) `trigger`：把触发器包成闭包，渲染时把 `trigger.is_selected() || is_open` 作为选中态传回去。

`Button`、`DropdownButton` 等组件都实现了 `Selectable`（u3-1），所以可以直接当触发器。这样 Popover 打开时，触发按钮会自动呈现「选中高亮」，视觉上提示「这个弹出层是由我打开的」。这也是为什么 `Popover` 的 `mouse_button` 默认是 `Left`，但可改成 `Right` 当右键菜单用（[popover.rs:75-78](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L75-L78)）。

**(5) 焦点保存与还原。** 这是 Popover 比 Tooltip「重」的地方：

[popover.rs:261-301](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L261-L301) `toggle_open`：开启时记下 `previous_focus_handle = window.focused(cx)`，并把焦点移到 `tracked_focus_handle`（若设了 `track_focus`）或自身 `focus_handle`；关闭时若焦点还在自身（`contains_focused`），就还原到 `previous_focus_handle`。

`track_focus` 让你可以把 Popover 的焦点绑到一个**你自己的实体**上（比如里面放了一个表单 View），这样 Popover 打开时 Tab 能进到你的表单里：

[popover.rs:167-170](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L167-L170) `track_focus`：把指定 `FocusHandle` 绑定为 Popover 的聚焦目标。

**(6) ESC 关闭与 DismissEvent。** Popover 在 `init` 里把 ESC 绑到 `Cancel` 动作，作用域是 `"Popover"` 这个 key_context：

[popover.rs:13-16](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L13-L16) `init`：`cx.bind_keys([escape → Cancel, Some("Popover"))])`。

正文容器挂 `.key_context("Popover")`（见 4.1.3(8)），所以只有「焦点在 Popover 内」时 ESC 才生效，不会误伤界面其他部分。`on_action_cancel` 收到 `Cancel` 就 `dismiss`：

[popover.rs:303-305](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L303-L305) `on_action_cancel`：`self.dismiss(...)`。

`PopoverState` 还实现了 `EventEmitter<DismissEvent>`，于是**正文里的任意元素**都能 `cx.emit(DismissEvent)` 让 Popover 自己关掉——非常适合「弹层里点提交按钮后自动关闭」：

[popover.rs:320](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L320) `impl EventEmitter<DismissEvent> for PopoverState`。

开启时 Popover 会订阅这个事件，收到就调 `dismiss`（见 [popover.rs:278-286](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L278-L286)）。

**(7) deferred 渲染与 deferred 上下文登记。** 浮层主体用 `deferred(anchored())` 画：

[popover.rs:323-341](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L323-L341) `render_popover`：`deferred(anchored().snap_to_window_with_margin(px(8.)).anchor(anchor).position(position.get()))`，优先级 1。`snap_to_window_with_margin(8px)` 让浮层贴住窗口边缘时留 8px 安全边距。

因为用了 deferred，Popover 开启时会在 `GlobalState` 里登记自己，避免「deferred 里再套 deferred」导致 GPUI panic：

[popover.rs:252-259](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L252-L259) `set_open`：开则 `register_deferred_popover`，关则 `unregister_deferred_popover`。

`GlobalState::is_in_deferred_context()`（[global_state.rs:59-61](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/global_state.rs#L59-L61)）据此判断「当前是否在某个打开的 Popover 内部」，供其他需要 deferred 的组件（如输入法的补全菜单）避让。

**(8) `RenderOnce::render` 全貌。** 这是把上面零碎拼起来的总入口，重点看四段：

[popover.rs:367-379](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L367-L379) 用 `use_keyed_state` 取/建 `PopoverState`，并把本帧的 `tracked_focus_handle`、`on_open_change`、受控 `open` 同步进去。

[popover.rs:399-433](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L399-L433) 触发器被 `on_mouse_down` 触发 `toggle_open`；`on_prepaint` 把真实边界写入共享 `Cell`，并在**首次**量到边界时 `request_animation_frame()`——因为首次渲染时边界还没量到，浮层会画在错误位置，需要主动请求新一帧来修正。

[popover.rs:435-437](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L435-L437) 早退：`!open || !trigger_bounds_captured` 时只画触发器、不画浮层。

[popover.rs:439-467](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L439-L467) 拼装浮层：`render_popover_content` 套 `popover_style` + `tab_group` + `.track_focus` + `.key_context("Popover")` + `on_action(Cancel)`；`overlay_closable` 时挂 `on_mouse_down_out → dismiss`；最后 `render_popover(...)` 延迟绘制。注意 `content` 在这里通过 `state.update(cx, |state, cx| (content)(state, window, cx))` 执行（即 4.1.3(3) 所说「每帧调用」）。

> `render_popover_content` 里的 `tab_group()`（[popover.rs:343-360](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L343-L360)）让浮层内形成一个 Tab 焦点组，配合 `track_focus` 实现「Tab 在浮层内循环」。这与 u5-1 Dialog 的 `focus_trap` 思路类似但更轻量（不强制陷阱、点外部即可逃出）。

#### 4.1.4 代码实践

**实践目标**：用受控模式实现一个「带表单的 Popover」，点提交后通过 `DismissEvent` 自动关闭，验证焦点进出与外部点击关闭。

**操作步骤**：

1. 打开 [popover_story.rs:254-269](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/popover_story.rs#L254-L269) 的 "Popover with Form" 与 [popover_story.rs:287-313](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/popover_story.rs#L287-L313) 的 "Right click to open Popover"，这是受控模式与 `DismissEvent` 的范例。
2. 在你的 View 里加一个受控 Popover，要点如下（示例代码，基于上述范例简化）：

```rust
// 示例代码（基于 popover_story.rs 简化）
use gpui_component::{
    button::Button,
    input::{Input, InputState},
    popover::Popover,
};

// View 字段里持有：form_open: bool，form_state: Entity<InputState>

Popover::new("my-popover")
    .p_0()
    .text_sm()
    .trigger(Button::new("open-btn").outline().label("Open Form"))
    .track_focus(&self.form_state.focus_handle(cx))   // Tab 能进到输入框
    .open(self.form_open)                              // 受控
    .on_open_change(cx.listener(|this, open, _, cx| {  // 同步回 View
        this.form_open = *open;
        cx.notify();
    }))
    .child(Input::new(&self.form_state))
    .child(
        Button::new("submit").primary().label("Submit").on_click(
            cx.listener(|_, _, _, cx| cx.emit(gpui::DismissEvent)), // 提交即关闭
        ),
    )
```

3. 运行 `cargo run`，在 Gallery 搜索 "Popover" 进入页面，点 "Popup Form"。

**需要观察的现象**：

- 点击按钮弹出表单，焦点自动进入输入框（`track_focus` 生效）；按钮自身高亮（`Selectable` 选中态）。
- 点 Popover **外部**任意位置，弹层消失（`overlay_closable` 默认 true）；关闭后焦点回到触发按钮。
- 在弹层内按 ESC，弹层关闭（`key_context("Popover")` + ESC 绑定）。
- 点 "Submit"，弹层关闭——这就是 `cx.emit(DismissEvent)` 的效果。

**预期结果**：三种关闭方式（ESC、点外部、`DismissEvent`）都能正确关闭，且焦点进出符合预期。

> 待本地验证：不同锚点（`Anchor::BottomCenter` 等）在触发器靠近窗口边缘时的翻转表现——`snap_to_window_with_margin(8px)` 会把浮层拉回窗口内，但不会自动换边。

#### 4.1.5 小练习与答案

**练习 1**：用 `.open(self.x)` 受控模式，但忘了写 `.on_open_change(...)`，会发生什么？

**答案**：组件内部 `toggle_open` 翻转后调了 `on_open_change`，但你没监听，View 里的 `self.x` 不变；下一帧 `.open(self.x)` 又把它改回去——表现为「点开瞬间弹出又立刻消失」或完全打不开。受控模式必须 `open` + `on_open_change` 成对使用（[popover.rs:102-123](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L102-L123)）。

**练习 2**：为什么 `RenderOnce::render` 在首次量到触发器边界时要 `request_animation_frame()`？

**答案**：首次渲染时 `on_prepaint` 还没跑过，`trigger_bounds` 是默认值（全 0），`resolved_corner` 算出的浮层位置是错的。`on_prepaint` 虽然在本帧补上了真实边界，但浮层已经按错误位置画了。主动请求新一帧，能确保下一帧用正确边界重新定位（[popover.rs:419-432](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L419-L432)）。

**练习 3**：`appearance(false)` 后会发生什么？什么场景需要它？

**答案**：浮层不再套 `popover_style`（没有背景、边框、阴影、内边距），且「点击外部关闭」也不再生效（参见 [popover.rs:152-161](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L152-L161) 与 story "Styling Popover" 用 `.appearance(false)` 再手动 `.bg().text_color()` 自定义外观的场景）。适合想要完全自定义浮层视觉、或浮层本身就是一个不需要边框的容器（如菜单）时。

### 4.2 Tooltip：悬停提示与窗口级 TooltipOverlay

#### 4.2.1 概念说明

`Tooltip` 和 Popover 有一处根本差别：**同一时间整个窗口只显示一个 Tooltip**。这是「提示」的语义决定的——鼠标移到一个按钮上，只该出现一条提示，而不是每个悬停过的元素都飘一条。

为实现这一点，gpui-component 没有像 Popover 那样「每个 Tooltip 自己 deferred 渲染」，而是把所有 tooltip 的生命周期集中到**一个每窗口唯一的 `TooltipOverlay` 实体**，它就住在 `Root` 里：

[tooltip.rs:363-373](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L363-L373) `TooltipOverlay`：`content`（当前要显示的内容）、`prev_trigger_bounds`（切换动画用）、`epoch`（计时器版本号）、`had_recent_tooltip`（宽限期标记）、`animation_epoch`、`is_switching`、以及 show/hide 两个定时器 `Task`。

[Root 在初始化时建好它并在 render 里作为一层画出](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L557)——注意这和 Dialog 不同：Dialog 的图层需要调用方手动挂 `render_dialog_layer`，而 **TooltipOverlay 是 `Root` 自己挂的**（[root.rs:557](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L557) `.child(self.tooltip_overlay.clone())`），所以你用 `.tooltip(...)` 不需要任何额外挂载。

`TooltipOverlay` 负责四件事：**延时显示**、**宽限期**（鼠标从一个提示快速移到另一个时不重新等待）、**进出/横向滑动动画**、**智能定位**（上方放不下就翻到下方）。

而 `Tooltip` 本身是一个会渲染成 `AnyView` 的小视图，描述「这条提示长什么样」：

[tooltip.rs:32-37](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L32-L37) `Tooltip`：`content`（文本 `Text` 或自定义元素）、`key_binding`（手动指定的快捷键）、`action`（用 Action 自动查快捷键）。

> 两条 tooltip 接入路径，别混淆：
> - **组件自带的 `.tooltip(text)`**（Button / Checkbox / Radio / Switch / Toggle / Clipboard 等）：内部存一个 `ComponentTooltip`，渲染时经 `ComponentTooltip::apply` 调 `managed_tooltip`，把悬停事件路由到 `TooltipOverlay`。这是推荐用法。
> - **GPUI 内置的 `.tooltip(builder)`**（任何 `StatefulInteractiveElement`，如 `div().id(...).tooltip(...)`）：这是 GPUI 自己的 tooltip 机制，**不走** `TooltipOverlay`。Story 的 "Default Tooltip" 一节特意用它来对比（[tooltip_story.rs:141-148](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/tooltip_story.rs#L141-L148)）。本讲主要讲 gpui-component 的托管机制。

#### 4.2.2 核心流程

```
鼠标悬停在带 .tooltip(...) 的元素上
   │  on_hover(true)  (由 ManagedTooltipExt 挂载)
   │  on_prepaint 已把 trigger_bounds 存进 Cell
   ▼
Root::tooltip_overlay → TooltipOverlay::request_show(content)
   │  取消任何 pending hide
   │  若「当前已显示」或「在宽限期内」：
   │      立即换内容，标记 is_switching，触发横向滑动动画
   │  否则：
   │      记 epoch，起 SHOW_DELAY(500ms) 定时器
   ▼
定时器到点（epoch 仍匹配）
   │  content = 新内容；animation_epoch++；notify
   ▼
TooltipOverlay::render
   │  deferred( TooltipOverlayPositioner(trigger_bounds) )
   │    positioner 量出 tooltip 尺寸，算出 above/below 位置，用 with_element_offset 平移
   │  新 tooltip：slideDown(y 4→0) + fadeIn(0→1)，150ms，ease_out_cubic
   │  切换 tooltip：slideX(从旧触发器横向移到新触发器)，200ms，ease_in_out_cubic
   │  priority 2（比 Popover 的 1 更高，盖在最上）
   ▼
鼠标移开 → on_hover(false) → request_hide
   │  取消 pending show；记 epoch；起 GRACE_PERIOD(300ms) 定时器
   │  到点（epoch 匹配）→ content = None；notify → 不再绘制
```

**关键结论**：Tooltip 的显示与否由**全局唯一**的 `TooltipOverlay.content` 决定；延时与取消靠 **epoch 版本号**；定位由 `TooltipOverlayPositioner` 在 prepaint 阶段根据 tooltip 实际尺寸算出。

#### 4.2.3 源码精读

**(1) 扩展 trait：`ManagedTooltipExt`。** 它让任何「带稳定 id 的元素（`StatefulInteractiveElement`）+ `ElementExt`」自动获得托管 tooltip 能力，靠的是 blanket impl：

[tooltip.rs:585-634](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L585-L634) `managed_tooltip`：`on_prepaint` 把元素边界写入共享 `Cell`；`on_hover(true)` 时用边界构造 `TooltipContent` 调 `Root::tooltip_overlay(...)` 的 `request_show`，`on_hover(false)` 调 `request_hide`；`on_mouse_down(Left)` 时调 `hide`（按下鼠标立刻收起 tooltip）。

`ComponentTooltip::apply`（[tooltip.rs:561-581](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L561-L581)）就是把文本/Action 配置包成一个 `Tooltip::new(...).build(...)` 的闭包，喂给 `managed_tooltip`——这就是 Button 等组件 `.tooltip("...")` 的落地处。

**(2) 延时与宽限期：`request_show` / `request_hide`。** 这是 Tooltip 体验的核心：

[tooltip.rs:396-435](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L396-L435) `request_show`：先取消 pending hide。若「当前已显示」或 `had_recent_tooltip`（宽限期内），**立即**换内容并标记切换动画；否则起一个 `SHOW_DELAY`（500ms）定时器，到点后若 `epoch` 仍匹配才真正显示。

[tooltip.rs:439-462](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L439-L462) `request_hide`：取消 pending show，置 `had_recent_tooltip = true`，起 `GRACE_PERIOD`（300ms）定时器，到点且 `epoch` 匹配才清空内容。

> **宽限期（grace period）解决一个体验问题**：鼠标从按钮 A 移到按钮 B 时，中间会短暂「离开 A」。如果没有宽限期，A 的 tooltip 会先消失、然后 B 重新等满 500ms 才显示，顿挫感明显。宽限期让「刚隐藏的 tooltip」在 300ms 内被新的 `request_show` 命中时跳过延时、立即切换，配合横向滑动动画，移动感很顺滑。

**(3) epoch：取消陈旧计时器。** 异步计时器有一个经典问题——定时器到期时，状态可能已经变了（比如用户在 500ms 内移开了鼠标，show 定时器却仍会触发）。gpui-component 用一个递增的 `epoch` 版本号解决：

每次 `request_show` / `request_hide` / `cancel_tasks` 都会 bump `epoch`（`next_epoch`）。定时器回调里**先比对 `this.epoch == epoch`**，不匹配就直接 return。`cancel_tasks` 更是一次性 bump epoch、让所有 pending 计时器全部失效（[tooltip.rs:447-448](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L447-L448) 与 `clear_state` [tooltip.rs:470-486](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L470-L486)）。

这是 gpui-component 异步状态机的通用手法——u4-3 的 Slider、本讲 4.3 的 HoverCard 都用同一套 `epoch` 思路。

**(4) 智能定位：上方优先，放不下翻下方。** `tooltip_overlay_position` 决定 tooltip 放在触发器的上方还是下方：

[tooltip.rs:167-198](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L167-L198) `tooltip_overlay_position`：先算水平居中的 `centered_x`；上方候选 `above` 的顶边是 `trigger.top − tooltip.height`，下方候选 `below` 的顶边是 `trigger.bottom`；优先选「上方且顶边 ≥ margin」的，否则「下方且底边不超出」的，否则选「空间更大的一侧」；最后 `clamp_tooltip_bounds` 把它拉回视口内。

水平居中坐标：

\[ x_{center} = \text{trigger.center}.x - \frac{\text{tooltip.width}}{2} \]

上下候选原点：

\[ y_{above} = \text{trigger.top} - \text{tooltip.height},\quad y_{below} = \text{trigger.bottom} \]

选择策略（`margin` 为到窗口边缘的安全距离）：

\[ \text{placement} = \begin{cases} \text{Above} & \text{若 } y_{above} \geq \text{margin} \\ \text{Below} & \text{若 } y_{below} + \text{tooltip.height} \leq \text{viewport}.h - \text{margin} \\ \text{空间更大的一侧} & \text{否则} \end{cases} \]

「上方优先」符合大多数桌面系统的习惯（提示一般出现在元素上方）。`clamp_tooltip_bounds`（[tooltip.rs:200-223](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L200-L223)）再保证它不会超出窗口左右下边界。这套逻辑有单测覆盖（[tooltip.rs:675-733](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L675-L733)）。

**(5) 自定义定位元素 `TooltipOverlayPositioner`。** 因为 tooltip 的尺寸要到子元素布局完才知道，而定位又依赖尺寸，gpui-component 写了一个自定义 `Element`：先做一次绝对定位的 flex 布局量出子元素总尺寸，再在 prepaint 阶段用 `with_element_offset` 把整体平移到目标位置：

[tooltip.rs:288-326](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L288-L326) `prepaint`：遍历子元素的 `layout_bounds` 求出 `tooltip_size`，调用 `tooltip_overlay_position` 算出目标原点，`offset = 目标原点 − 自身原点`，四舍五入后用 `with_element_offset` 平移所有子元素。

> 这个「先量尺寸再偏移」的套路和 u2-4 讲过的 `on_prepaint` 隐形探针是同源思想——都用一次额外的布局/绘制来获取像素信息。区别是这里需要一个能改写自身绘制偏移的自定义元素，而不是只回调一个矩形。

**(6) 进出/滑动动画。** `TooltipOverlay::render` 根据是否在「切换」套不同动画：

[tooltip.rs:489-543](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L489-L543) `render`：`deferred(positioner).child(...)`，优先级 2（盖在 Popover 之上）。新 tooltip 用 `Transition::new(ENTER_DURATION=150ms).ease(ease_out_cubic).slide_y(4px→0).fade(0→1)`；切换时若新旧触发器在同一水平线（`|Δy| < 10px`）才用 `slide_x(-dx→0)`（`SLIDE_DURATION=200ms`、`ease_in_out_cubic`），跨行则不横向滑动避免斜向移动。

常量都在 [tooltip.rs:146-153](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L146-L153)：`SHOW_DELAY 500ms`、`GRACE_PERIOD 300ms`、`ENTER_DURATION 150ms`、`SLIDE_DURATION 200ms`、窗口边距 `4px`。

**(7) 组件用法。** Story 展示了组件自带 tooltip 的写法，正是实践环节的依据：

[tooltip_story.rs:79-97](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/tooltip_story.rs#L79-L97) `Button::new("btn0").tooltip("This is a search Button.")`、`.tooltip_with_action("...", &Info, Some("Tooltip"))`（用 Action 自动显示其快捷键）。

`tooltip_with_action` 会通过 `Kbd::binding_for_action` 查到该 Action 绑定的按键，并在 tooltip 右侧渲染一个 `Kbd` 快捷键标签（见 [Tooltip::render](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L90-L141) 的 `when_some(key_binding, ...)` 分支）。

#### 4.2.4 代码实践

**实践目标**：为按钮加文本 tooltip，再用 `tooltip_with_action` 让 tooltip 自动显示一个绑定了快捷键的 Action 的按键，验证「窗口唯一、上方优先、切换无延时」。

**操作步骤**：

1. 阅读 [tooltip_story.rs:79-97](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/tooltip_story.rs#L79-L97) 与它的按键绑定 [tooltip_story.rs:24-26](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/tooltip_story.rs#L24-L26)（`ctrl-shift-delete → Info`，context `"Tooltip"`）。
2. 在你的视图里加两个相邻的按钮（示例代码）：

```rust
// 示例代码（基于 tooltip_story.rs 简化）
use gpui_component::button::{Button, ButtonVariants as _};
// 假设你已定义 actions!(my_view, [Info]); 并 cx.bind_keys(ctrl-shift-delete → Info)

h_flex()
    .gap_2()
    .child(Button::new("search").primary().label("Search").tooltip("开始搜索"))
    .child(
        Button::new("info")
            .label("Info")
            .tooltip_with_action("显示信息（带快捷键）", &MyInfoAction, Some("MyView")),
    )
```

3. 运行 Gallery → Tooltip 页面（或你自己的应用），鼠标分别悬停两个按钮。

**需要观察的现象**：

- 悬停第一个按钮约 500ms 后，提示从其上方滑下淡入。
- 鼠标**快速**移到第二个按钮：提示几乎立即切换（宽限期 + 横向滑动），不再等 500ms。
- 第二个按钮的提示右侧多出一个快捷键标签（`tooltip_with_action` 自动查到的按键）。
- 把窗口缩小、让按钮贴近顶部，提示自动翻到按钮**下方**显示（`tooltip_overlay_position` 的 above→below 翻转）。
- 按下任一按钮，提示立刻消失（`on_mouse_down → hide`）。

**预期结果**：能清楚看到「单窗口唯一 + 延时 + 宽限期 + 智能上下定位」四项行为，并区分 `.tooltip(text)` 与 `.tooltip_with_action(...)`。

> 待本地验证：自定义元素 tooltip（`Tooltip::element(builder)`）与 Action 快捷键在不同平台（macOS 显示 `⌘`、Windows/Linux 显示 `Ctrl`）的渲染差异。

#### 4.2.5 小练习与答案

**练习 1**：为什么 gpui-component 要把所有 tooltip 集中到一个 `TooltipOverlay`，而不是像 Popover 那样每个元素自己 deferred 渲染？

**答案**：因为 tooltip 语义上「同一时刻只该有一条」。集中托管能自然保证唯一性（后显示的覆盖先显示的），还能统一实现延时、宽限期、切换动画、智能定位这些跨元素的状态——若每个元素各自渲染，这些「窗口级」的状态（比如「上一条刚消失、现在该不该跳过延时」）无处安放。参见 [tooltip.rs:359-373](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L359-L373)。

**练习 2**：用户悬停按钮 A 出现 tooltip，500ms 内鼠标移出 A 又移入 A。tooltip 会重新等 500ms 吗？

**答案**：不会（大概率）。移出时 `request_hide` 置 `had_recent_tooltip = true` 并起 300ms 宽限定时器；若在 300ms 内移回（`request_show` 命中宽限期），会立即重新显示而不等 500ms。只有当移出超过 300ms（宽限期结束、`had_recent_tooltip` 被清）后再移入，才会重新等满 500ms。参见 [tooltip.rs:396-462](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L396-L462)。

**练习 3**：`epoch` 在 `TooltipOverlay` 里解决什么问题？如果不用它直接起定时器会怎样？

**答案**：它解决「定时器到期时状态已变」的竞态。比如 show 定时器 500ms 到期前用户已移开鼠标，若无 epoch 检查，到期仍会把 tooltip 显示出来（内容已过期）。epoch 让每次状态变更都让旧定时器「作废」——回调里比对版本号不匹配就 return。不用它就会出现「鼠标已经移走，tooltip 却延迟弹出」的幽灵提示。参见 [tooltip.rs:418-433](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L418-L433)。

### 4.3 HoverCard：悬停触发的富内容卡片

#### 4.3.1 概念说明

`HoverCard` 解决「鼠标悬停在某个元素上，过一会儿弹出一张**富内容**卡片」——典型场景是悬停 `@用户名` 弹出用户资料卡（头像、简介、关注按钮）。它和 Tooltip 都是悬停触发，但有三个关键区别：

- **内容更重**：Tooltip 通常是一行文字；HoverCard 是任意富内容（头像、按钮、列表）。
- **可悬停到正文上**：Tooltip 一旦鼠标移到正文位置就会消失（正文不响应 hover）；HoverCard 的正文也响应 hover，允许鼠标从触发器移到正文上操作（比如点卡片里的「关注」按钮）而不被关闭。
- **延时更可配**：`open_delay`（默认 600ms）/`close_delay`（默认 300ms）都可自定义。

实现上，`HoverCard` 是个 `RenderOnce` 组件，**直接复用了 Popover 的渲染管线**——`render_popover_content`、`render_popover`、`resolved_corner` 都是从 `Popover` 借来的：

[hover_card.rs:305-323](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/hover_card.rs#L305-L323) `RenderOnce::render` 末尾用 `Popover::render_popover_content(...)` 套外观、`Popover::render_popover(...)` 延迟绘制——和 Popover 同一套 `deferred(anchored())` + 锚点定位机制。

`HoverCardState` 负责悬停逻辑与计时器：

[hover_card.rs:121-138](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/hover_card.rs#L121-L138) `HoverCardState`：`open`、`trigger_bounds`、`open_delay`/`close_delay`、`open_task`/`close_task` 两个计时器、`epoch`、`is_hovering_trigger`/`is_hovering_content` 两个悬停标志、`on_open_change`。

#### 4.3.2 核心流程

```
鼠标进入触发器
   │  on_hover(true) → on_trigger_hover(true)
   ▼
schedule_open：cancel_tasks；epoch++；起 open_delay 定时器
   │  到点且 epoch 匹配 → set_open(true)
   ▼
render：open 为真 → 复用 Popover 管线画出卡片
   触发器 on_prepaint 持续更新 trigger_bounds → position
   卡片正文也挂 on_hover(on_content_hover)
   ▼
鼠标从触发器移到卡片正文（中间会短暂离开触发器）
   │  on_hover(false) on 触发器 → on_trigger_hover(false)
   │      若此时不在悬停正文 → schedule_close（起 close_delay 定时器）
   │  on_hover(true) on 正文 → on_content_hover(true)
   │      cancel_tasks！取消刚才的 close 定时器 → 卡片不关
   ▼
在正文里操作（点关注按钮）→ 卡片保持打开
   ▼
鼠标移出正文 → on_content_hover(false)
   │  若不在悬停触发器 → schedule_close
   │  到点且 epoch 匹配 且 不悬停触发器 且 不悬停正文 → set_open(false)
```

**关键结论**：HoverCard 用**两个悬停标志（trigger / content）**和**关闭延时**，让「触发器 ↔ 正文」之间的鼠标移动不会误关卡片——只要鼠标还在两者任一之上，就不真正关闭。

#### 4.3.3 源码精读

**(1) 默认配置。** 默认锚点 `TopCenter`（卡片在触发器正下方居中），开/关延时 600ms/300ms：

[hover_card.rs:36-49](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/hover_card.rs#L36-L49) `new`：`anchor = TopCenter`、`open_delay = 0.6s`、`close_delay = 0.3s`、`appearance = true`。

延时可用 `open_delay` / `close_delay` 调整（[hover_card.rs:81-89](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/hover_card.rs#L81-L89)），Story 的 "Custom Timing" 演示了 200ms/100ms 的「快开」与 1000ms 的「慢开」（[hover_card_story.rs:109-128](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/hover_card_story.rs#L109-L128)）。

**(2) 触发器不要求 `Selectable`。** 与 Popover 不同，HoverCard 的 `trigger` 约束只是 `IntoElement`：

[hover_card.rs:58-64](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/hover_card.rs#L58-L64) `trigger`：包成只接收 `&Window, &App` 的闭包（不需要 `is_open` 来切选中态）。

因为悬停触发不需要「按下高亮」的选中语义，触发器可以是任意元素（一段文字、一个头像）。

**(3) 双悬停桥接。** 这是 HoverCard 最精巧的部分：

[hover_card.rs:217-242](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/hover_card.rs#L217-L242) `on_trigger_hover` / `on_content_hover`：

- 触发器悬停进入 → `schedule_open`；触发器悬停离开 → 仅当**不在悬停正文**才 `schedule_close`。
- 正文悬停进入 → `cancel_tasks`（取消任何 pending 关闭）；正文悬停离开 → 仅当**不在悬停触发器**才 `schedule_close`。

这样，鼠标从触发器滑向正文时，即便中间瞬间离开触发器，只要及时进入正文（在 `close_delay` 内），`cancel_tasks` 就会把那次 pending close 取消掉，卡片保持打开。渲染时给触发器（[hover_card.rs:286-288](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/hover_card.rs#L286-L288)）和正文（[hover_card.rs:308-310](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/hover_card.rs#L308-L310)）分别挂了 `on_hover`。

**(4) 计时器与 epoch。** 与 Tooltip 完全同构：

[hover_card.rs:162-194](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/hover_card.rs#L162-L194) `schedule_open` / `schedule_close`：都先 `cancel_tasks`、bump `epoch`、起定时器；定时器回调里**先比对 `epoch`**，`schedule_close` 还要额外检查 `!is_hovering_trigger && !is_hovering_content`（延时期间用户可能又悬停回来了）。

[hover_card.rs:196-205](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/hover_card.rs#L196-L205) `cancel_tasks`：`epoch += 1` 让所有 pending 计时器失效，并清空两个 task。

**(5) 复用 Popover 渲染管线。** `RenderOnce::render` 的结构与 Popover 高度相似：用 `use_keyed_state` 取 `HoverCardState`、`on_prepaint` 量边界写 `position` Cell、`!open` 时早退只画触发器；`open` 时用 `Popover::render_popover_content` + `Popover::render_popover` 画出卡片：

[hover_card.rs:251-325](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/hover_card.rs#L251-L325) `render` 全貌：注意它在正文容器上额外加了 `.overflow_hidden()`，并把 `content` 闭包（接收 `&mut HoverCardState`）通过 `state.update(cx, |state, cx| (content)(state, window, cx))` 执行——和 Popover 一样「每帧调用」，所以同理**不要在闭包里新建实体**。

**(6) 真实用法范例。** Story 的 "User Profile Preview" 就是悬停 `@用户名` 弹资料卡，是综合实践的依据：

[hover_card_story.rs:73-103](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/hover_card_story.rs#L73-L103) `HoverCard::new("user-profile").trigger(...).content(|_, _, cx| { 头像 + 姓名 + 简介 })`。

注意它用的是 `.content(...)`（接收 state 的闭包）而不是 `.child(...)`——两者都行，`.content` 适合需要在闭包里读 `HoverCardState` 的场景，普通静态内容用 `.child(...)` 即可。

#### 4.3.4 代码实践

**实践目标**：实现悬停 `@用户名` 弹出资料卡，验证「鼠标可以从用户名滑到资料卡上而不被关闭」。

**操作步骤**：

1. 阅读 [hover_card_story.rs:68-106](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/hover_card_story.rs#L68-L106)。
2. 在你的视图里加一个 HoverCard（示例代码，基于该范例简化）：

```rust
// 示例代码（基于 hover_card_story.rs 简化）
use std::time::Duration;
use gpui::{Anchor, px, relative};
use gpui_component::{ActiveTheme, StyledExt as _, avatar::Avatar, h_flex, hover_card::HoverCard, v_flex};

HoverCard::new("user-profile")
    .trigger(
        div()
            .child("@huacnlee")
            .cursor_pointer()
            .text_color(cx.theme().link),
    )
    .content(|_, _, cx| {
        h_flex()
            .w(px(320.))
            .gap_3()
            .items_start()
            .child(Avatar::new().src("https://avatars.githubusercontent.com/u/5518?s=64"))
            .child(
                v_flex()
                    .gap_1()
                    .child(div().child("Jason Lee").font_semibold())
                    .child(div().child("@huacnlee").text_color(cx.theme().link).text_sm())
                    .child(div().child("The author of GPUI Component.")),
            )
    })
```

3. 运行 Gallery → HoverCard 页面，悬停 `@huacnlee`。

**需要观察的现象**：

- 悬停约 600ms 后资料卡在用户名正下方弹出。
- 鼠标从用户名**缓慢滑到资料卡上**：卡片保持打开（双悬停桥接 + `cancel_tasks` 生效），可以在卡片里停留。
- 鼠标完全移开（既不在用户名也不在卡片）约 300ms 后，卡片消失。
- 把 `open_delay` 改成 `Duration::from_millis(200)`，再次悬停，弹出明显变快。

**预期结果**：能稳定地把鼠标移到资料卡上进行阅读/操作而不被误关，验证「触发器 ↔ 正文」的双悬停桥接。

> 待本地验证：当触发器与卡片正文之间有较大间距（gap）时，`close_delay`（默认 300ms）是否足够让鼠标「跨过空隙」到达正文——间距过大可能需要调大 `close_delay`。

#### 4.3.5 小练习与答案

**练习 1**：HoverCard 和 Tooltip 都是悬停触发，为什么 HoverCard 能让鼠标移到正文上不被关，而 Tooltip 不能？

**答案**：因为 HoverCard 的正文容器也挂了 `on_hover`，并维护 `is_hovering_content` 标志；正文被悬停时 `cancel_tasks` 取消 pending 关闭。Tooltip 的内容由窗口唯一的 `TooltipOverlay` 渲染，它只响应触发器的 hover，正文本身不挂 hover，所以鼠标一离开触发器（无论是否碰到正文）就会 `request_hide`。参见 [hover_card.rs:217-242](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/hover_card.rs#L217-L242)。

**练习 2**：`schedule_close` 的定时器回调里为什么要额外检查 `!is_hovering_trigger && !is_hovering_content`？只检查 `epoch` 不够吗？

**答案**：`epoch` 只能取消「起定时器之后又起了新定时器」的陈旧回调，但无法应对「`close_delay` 期间用户又把鼠标悬停回来了」——此时没有新的 `cancel_tasks`（hover 回来走的是 `schedule_open` 或 `cancel_tasks`，但若走的是 `on_content_hover(true)` 的 `cancel_tasks` 会 bump epoch；而若是鼠标短暂离开又回到触发器，`on_trigger_hover(true)` 会调 `schedule_open` → `cancel_tasks` 也会 bump epoch）。这道额外检查是一道**双保险**：即便 epoch 恰好没变，只要鼠标还在触发器或正文上，也绝不关闭。参见 [hover_card.rs:184-193](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/hover_card.rs#L184-L193)。

**练习 3**：HoverCard 的定位和外观是怎么来的？它自己实现了 `deferred(anchored())` 吗？

**答案**：没有。HoverCard 直接调 `Popover::render_popover_content` 和 `Popover::render_popover`（[hover_card.rs:305-323](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/hover_card.rs#L305-L323)），定位（`resolved_corner` + `deferred(anchored())`）和外观（`popover_style`）全部复用 Popover。所以 HoverCard 与 Popover 的视觉、锚点行为完全一致，差别只在「点击触发 vs 悬停触发」和「悬停生命周期管理」。

## 5. 综合实践

把本讲三个组件串起来，做一个**「用户资料行」**交互，覆盖全部三种悬浮层：

**需求**：一行用户信息——左侧头像、中间 `@用户名`、右侧一个「操作」按钮。要求：

1. **HoverCard**：悬停头像，延时弹出资料卡（头像大图 + 姓名 + 简介），且鼠标能从头像滑到资料卡上阅读。
2. **Tooltip**：悬停「操作」按钮，显示简短提示「打开操作菜单」。
3. **Popover**：**点击**「操作」按钮，弹出一个操作菜单（至少含「复制用户名」「删除」两项）。点击「复制用户名」后通过 `DismissEvent` 关闭 Popover 并 `window.push_notification` 提示；点击「删除」也关闭 Popover。

**实现要点提示**（基于本讲已读源码）：

- HoverCard 部分照抄 4.3.4 的范例，把 `trigger` 换成头像 `Avatar`。
- Tooltip 部分：`Button::new("actions").outline().label("操作").tooltip("打开操作菜单")`。
- Popover 部分用受控模式（`open(self.menu_open)` + `on_open_change` 同步），`trigger` 用同一个「操作」按钮（注意 Popover 的 `trigger` 要求 `Selectable`，`Button` 满足）；`content` 里放一个 `v_flex` 列出菜单项，每项是 `Button`，点击时 `cx.emit(DismissEvent)` 关闭。可参考 [popover_story.rs:287-313](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/popover_story.rs#L287-L313) 的 `DismissEvent` 用法。
- 注意 Tooltip 与 Popover 共用同一个按钮：Popover 打开时按钮因 `Selectable` 自动高亮，此时 Tooltip 不应再显示——实际表现是按下鼠标即 `hide` tooltip（`ManagedTooltipExt` 的 `on_mouse_down → hide`），两者不冲突。

**验收**：

- 悬停头像 ≈600ms → 资料卡弹出，鼠标能滑到卡片上。
- 悬停「操作」按钮 ≈500ms → 提示出现（上方）。
- 点击「操作」→ 菜单弹出，按钮高亮；点菜单项 → 菜单关闭 + 通知。
- 点菜单外部 → 菜单关闭；菜单内按 ESC → 关闭。

> 待本地验证：综合实践中 `push_notification` 需要 `Root` 已挂通知图层（u5-3 详讲）；若你的根视图还没挂，可先用 `println!` 替代验证流程。

## 6. 本讲小结

- `Popover` 是**点击/右键触发的非模态弹出层**：`RenderOnce` + `PopoverState`（`use_keyed_state` 托管），支持受控（`open` + `on_open_change` 必须成对）与非受控（`default_open`）；定位靠 `on_prepaint` 量触发器边界 + `resolved_corner` 算锚点 + `deferred(anchored())` 延迟绘制；ESC、点外部、`DismissEvent` 三种关闭方式，开关时保存/还原焦点。
- `content` / `trigger` 闭包每帧执行——**不要在其中新建实体**；`trigger` 要求实现 `Selectable`（打开时自动高亮）；`track_focus` 可把焦点绑到你的表单实体上。
- `Tooltip` 由**每窗口唯一的 `TooltipOverlay`**（住在 `Root`，由 `Root` 自己挂载，无需手动加图层）统一托管：同一时刻只显示一条，带 `SHOW_DELAY(500ms)` 显示延时、`GRACE_PERIOD(300ms)` 宽限期、进出/横向滑动动画；组件经 `ComponentTooltip::apply` → `ManagedTooltipExt::managed_tooltip` 接入，`on_hover` 驱动 `request_show`/`request_hide`。
- Tooltip 用 **epoch 版本号**取消陈旧计时器（异步状态机通用手法），用自定义 `TooltipOverlayPositioner` 元素「先量尺寸再 `with_element_offset` 偏移」实现「上方优先、放不下翻下方、贴边回拉」的智能定位。
- `HoverCard` 是**悬停触发的富内容卡片**，**直接复用 Popover 的渲染管线**（`render_popover_content` / `render_popover` / `resolved_corner`），区别只在触发方式与生命周期：用 `open_delay`/`close_delay`（默认 600/300ms）+ 双悬停标志（`is_hovering_trigger` / `is_hovering_content`）+ `cancel_tasks`，让鼠标能在触发器与正文之间移动而不被误关。
- 三者都是**非模态**轻量浮层（不遮罩、不抢焦点，点外部/移开即消），与 u5-1 的模态 `Dialog`（遮罩 + 焦点陷阱 + 打断）形成对照；选型口诀：**提示用 Tooltip、悬停看详情用 HoverCard、点击展开操作用 Popover**。

## 7. 下一步学习建议

- **u5-3 抽屉与通知（Sheet / Notification）**：Sheet 和 Dialog 一样由 `Root` 统一管理、需要手动挂载图层；`Notification` 则补齐了本讲综合实践里用到的 `push_notification`。学完它你就能拼出完整的「弹层 + 提示 + 通知」交互闭环。
- **u5-4 菜单系统（ContextMenu / DropdownMenu / PopupMenu）**：本讲 Popover 的「右键触发」与「点按钮弹菜单」正是菜单系统的底层；下一讲会看到 `PopupMenu` / `DropdownMenu` 如何基于类似的浮层机制构建出带快捷键、分隔线、子菜单的完整菜单。
- **深读建议**：想彻底吃透「异步计时器取消」这一通用手法，可对比本讲 `TooltipOverlay`（[tooltip.rs:396-486](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L396-L486)）、`HoverCardState`（[hover_card.rs:162-205](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/hover_card.rs#L162-L205)）与 u4-3 的 Slider，三者都是「epoch 版本号 + 定时器回调内比对」的同构实现；再结合 u2-4 的 `on_prepaint` 探针，你就能独立实现任意「定位浮层 + 延时显示」的组件。
