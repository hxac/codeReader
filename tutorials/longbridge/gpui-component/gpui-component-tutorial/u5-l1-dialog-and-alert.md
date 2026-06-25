# 弹窗：Dialog 与 Alert

## 1. 本讲目标

学完本讲，你应当能够：

- 用两种风格（声明式 `.trigger(...)` 与命令式 `window.open_dialog(...)`）创建并打开一个 `Dialog`，并理解 `Dialog` 本身是「无状态 `RenderOnce` 组件」。
- 用 `DialogHeader / DialogTitle / DialogDescription / DialogContent / DialogFooter` 这套声明式积木，以及 `DialogClose / DialogAction` 两个「语义按钮壳」，把 Header / Content / Footer 三段拼出来。
- 用 `AlertDialog` 实现一个「打断式」确认弹窗，理解它只是 `Dialog` 上的一组「带默认值的糖」。
- 说清楚弹窗在 gpui-component 里为什么必须由 `Root` 统一管理：弹窗不持状态，`Root` 只存「构建闭包 + 焦点句柄」，每帧重建，并负责焦点进出、遮罩、键盘、关闭动画。

## 2. 前置知识

本讲假设你已经掌握：

- **`init(cx)` 与 `Root` 管理壳**（u1-4）：每个窗口的第一层视图必须是 `Root`，它集中托管 Sheet / Dialog / Notification 等窗口级浮层；`gpui_component::init(cx)` 负责把它们注册到 `App`。
- **无状态 `RenderOnce` 组件 + 状态外置**（u2-2、u3-1）：组件每帧 `render(self)` 重建，自身不持跨帧状态，真实状态由外层 `Render` View 持有。
- **主题与语义色**（u2-1）：通过 `cx.theme()` 取 `overlay`、`border`、`radius_lg`、`muted_foreground`、`danger` 等语义色。
- **Button 家族**（u3-1）：本讲大量使用 `Button`、`ButtonVariant`、`ButtonVariants`（`.primary()` / `.outline()` / `.danger()`）。
- **焦点陷阱 `FocusTrapElement`**（u2-4）：`focus_trap(...)` 让模态容器的焦点不逃逸，Dialog 正是靠它实现「Tab 只在弹窗里循环」。
- **GPUI 的 Action / KeyBinding**（u1-4、u2-4 提到）：`actions!` 宏定义动作，`cx.bind_keys` 把按键绑定到动作，`key_context(...)` 把绑定限定在某个上下文范围内。

如果你对上面任何一点陌生，建议先回看 u1-4（入口与 Root）和 u2-4（事件与焦点陷阱）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [dialog.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/dialog.rs) | `Dialog` 组件主体：结构、`DialogProps`/`DialogButtonProps` 两份配置、声明式触发器、`RenderOnce` 渲染（遮罩、卡片、动画、键盘），以及 `CancelDialog`/`ConfirmDialog` 两个动作。 |
| [alert_dialog.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/alert_dialog.rs) | `AlertDialog`：在 `Dialog` 基础上加「打断式」默认值（不点遮罩关闭、无关闭叉），并提供 `title/description/icon/button_props` 等命令式糖，最终经 `into_dialog` 折叠回一个 `Dialog`。 |
| [mod.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/mod.rs) | 模块聚合与再导出，把 `Dialog`、`AlertDialog`、`DialogHeader/Title/Description/Content/Footer` 等统一 `pub use` 出来。 |
| [header.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/header.rs) / [title.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/title.rs) / [description.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/description.rs) / [content.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/content.rs) / [footer.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/footer.rs) | 五个声明式子组件，分别承担头部容器、标题、描述、正文容器、底部按钮栏；`footer.rs` 还定义了 `DialogClose`/`DialogAction` 两个语义按钮壳。 |
| [root.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs) | `Root` 对弹窗的统一管理：`active_dialogs` 栈、`render_dialog_layer`、`open_dialog`/`close_dialog`/`defer_close_dialog`/`close_all_dialogs`，以及焦点进出与关闭动画。 |
| [window_ext.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/window_ext.rs) | `WindowExt` trait：把 `open_dialog` / `open_alert_dialog` / `close_dialog` 等能力挂到 `Window` 上，内部都转发给 `Root`。 |
| [dialog_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/dialog_story.rs) / [alert_dialog_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/alert_dialog_story.rs) | Story Gallery 里的真实范例，是本讲实践与示例的依据。 |

## 4. 核心概念与源码讲解

弹窗要解决的问题是：在当前窗口之上临时弹出一块「模态」内容，**打断用户**、**遮住背景**、**独占键盘焦点**，直到用户确认或取消。gpui-component 把这件事拆成两层：

| 层 | 职责 | 关键源码 |
| --- | --- | --- |
| 组件层 | 定义「弹窗长什么样」：卡片、遮罩、动画、Header/Content/Footer 三段、OK/Cancel 按钮 | `Dialog` / `AlertDialog` |
| 管理层 | 定义「弹窗怎么活」：谁打开了、谁是栈顶、遮罩归谁、关闭后焦点还给谁、谁负责每帧把它画到屏幕上 | `Root` |

最容易踩坑的一点：**`Dialog` 组件自己不会出现在屏幕上**。它只是一个「描述」，真正把它渲染出来的是 `Root::render_dialog_layer`。所以你在自己的根视图里必须手动 `.children(Root::render_dialog_layer(window, cx))`，否则 `window.open_dialog(...)` 调了也看不到任何东西。这一点我们在 4.3 会反复强调。

下面按三个最小模块逐个精读：`Dialog`（4.1）、`AlertDialog`（4.2）、`Root` 的弹窗管理（4.3）。

### 4.1 Dialog：模态对话框主体

#### 4.1.1 概念说明

`Dialog` 是一个**无状态 `RenderOnce` 组件**（派生 `IntoElement`，并实现 `Styled` 与 `ParentElement`），它本身不持有跨帧状态——这一点和 Button、Switch 等组件一脉相承。它的字段分三类：

- **外观配置 `DialogProps`**（私有）：宽度、最大宽度、顶部偏移、是否带关闭叉、是否带遮罩、点遮罩是否可关、是否支持 ESC 键。
- **按钮配置 `DialogButtonProps`**：OK/Cancel 按钮的文案、变体、是否显示 Cancel，以及 `on_ok` / `on_cancel` / `on_close` 三个回调。
- **内容插槽**：`title`、`header`、`footer`、`content_builder`，以及通过 `ParentElement` 直接 `.child(...)` 塞进去的「正文」。

它提供两套等价的用法：

- **声明式（触发器）**：给一个 `.trigger(Button...)`，`Dialog` 就渲染成那个触发按钮，点击时自动调用 `window.open_dialog` 把自己打开。适合「这个按钮就是用来开弹窗的」场景。
- **命令式**：你自己在任意回调里 `window.open_dialog(cx, |dialog, _, _| { dialog... })`，闭包收到一个全新的 `Dialog` 让你配置。适合「弹窗的触发时机由业务逻辑决定」（比如收到一条远程消息后弹窗）的场景。

> 术语：**模态（modal）** 指弹窗会拦截它背后所有交互——背景点不动、键盘焦点进不去——直到它被关闭。这正是 `overlay`（遮罩）+ `focus_trap`（焦点陷阱）两件事一起做到的。

#### 4.1.2 核心流程

一次「点按钮 → 弹窗出现 → 点确认 → 弹窗消失」的全流程：

```
用户点击触发按钮
   │
   ▼
window.open_dialog(cx, build)            # 命令式入口（WindowExt）
   │   内部转发给 Root::open_dialog
   ▼
Root::open_dialog                        # 见 4.3
   │  1) 记录当前焦点为 previous_focused_handle（关闭时要还回去）
   │  2) 新建 focus_handle 并立刻 focus，把焦点抢进弹窗
   │  3) 把 build 闭包 + 焦点句柄包成 ActiveDialog 压栈 active_dialogs
   │  4) cx.notify() 触发 Root 重绘
   ▼
Root 重绘 → render_dialog_layer          # 见 4.3
   │  遍历 active_dialogs，对每个用 build 闭包重建一个 Dialog，
   │  把焦点句柄和 layer_ix（栈位）塞回去，只给栈顶弹窗画遮罩，
   │  最后 div().children(dialogs) 画出来
   ▼
Dialog::render                           # 本节重点
   │  anchored + snap_to_window 铺满视口；栈顶弹窗画 overlay 色；
   │  内层卡片套 focus_trap、绑 key_context("Dialog")；
   │  slide-down + fade-in 动画；右下角可选关闭叉
   ▼
用户点 OK 按钮 → render_ok 的 on_click
   │  调 on_ok 回调；若返回 true
   ▼
window.close_dialog(cx) → Root::close_dialog
   │  弹出栈顶 ActiveDialog，把 previous_focused_handle 还原焦点
   │  cx.notify() 重绘 → 弹窗从 render_dialog_layer 消失
```

关键结论：**弹窗的「存在与否」由 `Root` 的 `active_dialogs` 栈决定，`Dialog` 组件只是把栈里的描述画出来。** 关闭弹窗本质上是「从栈里弹出一个条目」。

#### 4.1.3 源码精读

**(1) 两个动作与键盘绑定。** Dialog 用 GPUI 的 `actions!` 宏定义了 `CancelDialog`、`ConfirmDialog` 两个动作，并在 `init` 里把 ESC 绑到取消、Enter 绑到确认，作用域限定为 `"Dialog"` 这个 key_context：

[dialog.rs:24-31](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/dialog.rs#L24-L31) 定义两个动作并绑定 ESC/Enter。

> 这个 `init` 由 `gpui_component::init(cx)` 统一调起，见 [lib.rs:122](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/lib.rs#L122)。绑定只在 `key_context("Dialog")` 的元素树内生效，所以普通界面的 ESC/Enter 不会误触发弹窗。

**(2) 两份配置。** `DialogProps` 承载外观默认值，注意默认宽度是 448px、遮罩与键盘默认都开：

[dialog.rs:184-197](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/dialog.rs#L184-L197) `DialogProps::default`：宽度 448px、`overlay=true`、`overlay_closable=true`、`keyboard=true`、`close_button=true`。

`DialogButtonProps` 承载按钮，三个回调里 `on_ok` / `on_cancel` 的**返回值是 `bool`：返回 `true` 才关闭弹窗**，返回 `false` 就保持打开（这是实现「表单校验未通过则不关」的关键）：

[dialog.rs:92-112](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/dialog.rs#L92-L112) `on_ok` / `on_cancel` 的文档：返回 `true` 关闭，`false` 不关。

**(3) 声明式触发器。** 当你调 `.trigger(...)`，`RenderOnce::render` 走的不是画弹窗的分支，而是 `render_trigger`：它把触发元素包进一个 `div`，挂上 `on_mouse_down`，按下时调用 `window.open_dialog` 把自己打开，并 `cx.stop_propagation()` 防止冒泡：

[dialog.rs:402-436](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/dialog.rs#L402-L436) `render_trigger`：触发元素被点击时通过 `window.open_dialog` 打开弹窗。

注意它把 `content_builder`、`style`、`props`、`button_props` 都克隆进了闭包——因为这些值要等到「真正打开的那一刻」才被喂给一个新 `Dialog`。

**(4) 渲染主体。** 真正画弹窗的 `RenderOnce::render` 是本组件最复杂的地方，重点看几段：

[dialog.rs:438-466](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/dialog.rs#L438-L466) 入口：若有 trigger 走触发器分支；否则计算视口可用尺寸、居中坐标 `x`、顶部 `y`（默认视口高度的 1/10），并构造一段 0.25 秒、缓动 `cubic_bezier(0.32,0.72,0,1)` 的动画。

[dialog.rs:483-521](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/dialog.rs#L483-L521) 外层用 `anchored().snap_to_window()` 铺满视口；**只有栈顶弹窗**（`layer_ix + 1 == active_dialogs.len()`）才会挂上「点遮罩关闭」的事件，并把该区域标记为 `WindowControlArea::Drag`（拖拽窗口时不会被遮罩吃掉）；点击位置在标题栏以上（`< TITLE_BAR_HEIGHT`）时忽略，避免和拖窗冲突。

[dialog.rs:522-564](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/dialog.rs#L522-L564) 内层卡片：`.track_focus(&focus_handle)` 接入焦点系统，`.focus_trap(format!("dialog-{}", layer_ix), &focus_handle)`（来自 u2-4 的 `FocusTrapElement`）让 Tab 只在弹窗内循环；`.key_context(CONTEXT)` 让 ESC/Enter 绑定生效；`on_action::<CancelDialog>` 走取消、`on_action::<ConfirmDialog>` 走确认。

[dialog.rs:642-663](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/dialog.rs#L642-L663) 两段 `with_animation`：`slide-down` 让卡片从顶部 `y*delta` 滑下并带阴影，`fade-in` 让整体透明度从 0 淡入。`delta` 是 0→1 的动画进度。

**(5) 默认 OK/Cancel 按钮。** 如果你没有自定义 footer，`DialogButtonProps::render_ok` / `render_cancel` 会帮你生成两个按钮（文案走 i18n 的 `Dialog.ok` / `Dialog.cancel`），点击时调对应回调，返回 `true` 才 `window.close_dialog`：

[dialog.rs:114-139](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/dialog.rs#L114-L139) `render_ok`：OK 按钮点击 → `on_ok` 返回 `true` → `window.close_dialog` + `on_close`。

默认按钮文案的多语言来自 [ui.yml:224-236](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/locales/ui.yml#L224-L236)：中文环境下 OK 显示「确定」、Cancel 显示「取消」。

> 注意 `on_close` 与 `on_ok`/`on_cancel` 的关系：`on_close` 是「弹窗已经决定要关了」之后触发的收尾回调，在 `on_ok`/`on_cancel` 之后执行，无论确认还是取消都会调它。适合做「无论结果如何都要清理」的逻辑。

#### 4.1.4 代码实践

**实践目标**：用命令式 API 在 Story Gallery 风格的视图里打开一个带滚动正文的最简 `Dialog`，观察遮罩、ESC 关闭、Enter 确认。

**操作步骤**：

1. 打开 [dialog_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/dialog_story.rs)，定位 `render_dialog_without_title`（第 258 行起），这是命令式用法最短的范例：

```rust
// 示例代码（节选自 dialog_story.rs:262-274，已简化）
Button::new("dialog-no-title")
    .outline()
    .label("Dialog without Title")
    .on_click(cx.listener(move |_, _, window, cx| {
        window.open_dialog(cx, move |dialog, _, _| {
            dialog
                .overlay(dialog_overlay)         // 是否带遮罩
                .overlay_closable(overlay_closable) // 点遮罩能否关闭
                .child("This is a dialog without title.")
        });
    }))
```

2. 在你的应用里，先确保根视图 `render` 中挂上了弹窗图层（**这是新手最常漏的一步**）：

```rust
// 示例代码（节选自 crates/story/src/lib.rs:693-715 的 StoryRoot 写法）
let dialog_layer = Root::render_dialog_layer(window, cx);
div()
    .size_full()
    .child(/* 你的业务视图 */)
    .children(dialog_layer)   // 不加这行，open_dialog 什么都看不到
```

3. 运行 `cargo run`，在 Gallery 里搜索 "Dialog" 进入对应页面，点击 "Dialog without Title"。

**需要观察的现象**：

- 弹窗从顶部滑下、淡入出现；背景被半透明遮罩盖住。
- 按 ESC，弹窗消失（因为 `keyboard` 默认 true）。
- 点击弹窗外的遮罩区域，弹窗消失（因为 `overlay_closable` 默认 true）。
- 在弹窗里反复按 Tab，焦点只在弹窗内部元素间循环，不会跑到背景去（`focus_trap` 生效）。

**预期结果**：四种关闭方式（关闭叉、ESC、点遮罩、自定义按钮调 `window.close_dialog`）都能让弹窗消失；关闭后焦点回到触发它的按钮上。

> 待本地验证：不同平台的 `anchored + snap_to_window` 在自定义窗口装饰（Linux CSD）下表现是否一致。

#### 4.1.5 小练习与答案

**练习 1**：想做一个「表单校验失败就不让关」的弹窗，应该用哪个回调、返回什么？

**答案**：用 `on_ok` 回调，校验通过返回 `true`（关闭），不通过返回 `false`（保持打开）。参见 [dialog.rs:92-98](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/dialog.rs#L92-L98)。`on_cancel` 同理。

**练习 2**：为什么 `render` 里要判断 `(self.layer_ix + 1) != Root::read(window, cx).active_dialogs.len()` 才决定是否挂「点遮罩关闭」？

**答案**：因为多个弹窗可以同时打开（弹窗里再开弹窗），但「点遮罩关闭」只能挂给**栈顶**那一个，否则点一下会同时关闭好几个弹窗。`layer_ix` 是该弹窗在栈里的下标，`+1 == len()` 表示它就是栈顶。

**练习 3**：`.trigger(...)` 和直接 `window.open_dialog(...)` 两种写法，分别适合什么场景？

**答案**：`.trigger(...)` 适合「点击某个固定按钮就开弹窗」——它把触发与打开绑定在一起，声明式、零额外代码；`window.open_dialog` 适合「开弹窗的时机由业务逻辑决定」（如收到网络消息、定时器到期、另一段流程结束时），需要在任意回调里命令式触发。

### 4.2 AlertDialog：打断式确认弹窗

#### 4.2.1 概念说明

`AlertDialog` 是 `Dialog` 上的一层「带默认值的糖」。同样是模态弹窗，但 `AlertDialog` 的语义是「**打断用户、要求必须回应**」——典型场景是「确定删除吗？」「会话已过期」「需要授权」。它的源码定义很直白：内部就是一个 `base: Dialog`：

[alert_dialog.rs:63-72](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/alert_dialog.rs#L63-L72) `AlertDialog` 结构体：`base: Dialog` 加上 `trigger / icon / title / description / button_props / children`。

它的「糖」体现在构造函数里给 `Dialog` 灌了两条更「打断」的默认值：

[alert_dialog.rs:80-90](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/alert_dialog.rs#L80-L90) `AlertDialog::new`：默认 `overlay_closable(false)`（点遮罩**不能**关）、`close_button(false)`（**没有**右上角关闭叉）。

这两条默认值的意图很明确：**Alert 必须由用户明确点按钮才能关**，不能被随手点外面关掉。这正好对应「打断式」语义。

> 一处代码注释与实现的小出入：`.width(...)` 的文档注释写「defaults to 420px」([alert_dialog.rs:204-205](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/alert_dialog.rs#L204-L205))，但 `AlertDialog::new` 并没有设置宽度，所以实际继承的是 `Dialog` 的 448px 默认值。以代码为准：默认 448px，需要更窄请显式 `.width(px(420.))`。

#### 4.2.2 核心流程

`AlertDialog` 本身不实现「画弹窗」的逻辑，它最终都要折叠回一个 `Dialog`：

```
AlertDialog::new(cx)                       # 1. 带「打断式」默认值创建
   .title(...) / .description(...)         # 2. 命令式 API：填充标题/描述/图标/按钮
   .icon(...) / .button_props(...)
   .on_ok(...) / .show_cancel(true)
        │
        ├─ 声明式用法：RenderOnce::render 时若有 trigger → render_trigger（同 Dialog）
        │
        └─ 否则：into_dialog(window, cx)    # 3. 折叠成一个配置好的 Dialog
                │   - 用 DialogHeader 把 icon+title+description 组成头部
                │   - 没自定义 footer 就生成默认 footer（cancel? + ok）
                ▼
              一个普通 Dialog               # 4. 之后的行为完全复用 4.1 的 Dialog
```

注意命令式 API 里的 `.title()` / `.description()` / `.icon()` / `.button_props()` 这些便捷方法，**不能和 `.trigger()` / `.content()` 同时用**——源码用 `debug_assert_no_trigger` 在 debug 构建里强制检查：

[alert_dialog.rs:150-156](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/alert_dialog.rs#L150-L156) `debug_assert_no_trigger`：用了 trigger/content 后再调 title/description 等会 panic（仅 debug）。

原因：`.title()` 这些方法走的是「由 `into_dialog` 帮你拼好头部」的捷径；而 `.trigger()+.content()` 走的是「你自己用 `DialogHeader` 等积木声明头部」的声明式路线。两条路不能混走。

#### 4.2.3 源码精读

**(1) `confirm()` 加取消按钮。** 默认 `AlertDialog` 只有一个 OK 按钮；调 `.confirm()` 会把 `show_cancel` 置真，从而多出一个 Cancel 按钮，变成「确认/取消」二选一：

[alert_dialog.rs:95-98](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/alert_dialog.rs#L95-L98) `confirm()`：`show_cancel = true`。

**(2) `into_dialog`：折叠回 Dialog。** 这是 `AlertDialog` 的核心方法，看它如何把命令式字段组装成声明式的 `Dialog`：

[alert_dialog.rs:270-312](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/alert_dialog.rs#L270-L312) `into_dialog`：判断是否有标题/描述（`has_header`），若有就用 `DialogHeader` 套一层 `h_flex`（图标在左、标题描述竖排在右）作为 `.header(...)`；若用户没自定义 footer，就生成默认 `DialogFooter`，按 `show_cancel` 决定要不要 cancel，再加 ok。

读这段能学到一件事：**`AlertDialog` 的「便利」完全建立在 `Dialog` 的「声明式子组件」之上**——它只是替你把 `DialogHeader / DialogTitle / DialogDescription / DialogFooter` 拼了一遍。所以理解了 4.1 的 Dialog，`AlertDialog` 就没什么神秘的了。

**(3) 命令式入口 `open_alert_dialog`。** `WindowExt` 提供的便捷方法，内部其实就是 `open_dialog` + `AlertDialog::new` + `into_dialog`：

[window_ext.rs:144-152](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/window_ext.rs#L144-L152) `open_alert_dialog`：把你的闭包收到的 `AlertDialog` 经 `into_dialog` 折叠后，交给 `open_dialog`。

这也解释了为什么 `has_active_dialog` / `close_dialog` 不分 `Dialog`/`AlertDialog`——**对 `Root` 来说它们都是 `Dialog`**，`AlertDialog` 只是个构造期的糖。

**(4) 真实用法范例。** Story 里展示了命令式 `open_alert_dialog` 的典型写法，正是本讲综合实践的依据：

[alert_dialog_story.rs:99-126](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/alert_dialog_story.rs#L99-L126) `open_alert_dialog` + `.icon/.title/.description/.button_props/.on_ok`，OK 按钮用 `Danger` 变体、文案 "Delete"。

#### 4.2.4 代码实践

**实践目标**：用 `open_alert_dialog` 弹出一个「删除文件」确认框，验证 Alert 的三条「打断式」默认行为。

**操作步骤**：

1. 阅读上面的 [alert_dialog_story.rs:99-126](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/alert_dialog_story.rs#L99-L126) 范例。
2. 在你的视图里加一个「删除」按钮，点击触发下面的逻辑（示例代码，基于该范例简化）：

```rust
// 示例代码
use gpui_component::{
    Icon, IconName, WindowExt as _,
    button::{ButtonVariant, ButtonVariants as _},
    dialog::DialogButtonProps,
};

Button::new("delete").danger().label("Delete").on_click(cx.listener(|_, _, window, cx| {
    window.open_alert_dialog(cx, |alert, _, cx| {
        alert
            .icon(Icon::new(IconName::Trash).text_color(cx.theme().danger))
            .title("Delete File")
            .description("Are you sure? This action cannot be undone.")
            .button_props(
                DialogButtonProps::default()
                    .ok_variant(ButtonVariant::Danger)
                    .ok_text("Delete")
                    .cancel_text("Cancel")
                    .show_cancel(true),
            )
            .on_ok(|_, window, cx| {
                window.push_notification("File deleted", cx);
                true // 返回 true 才会关闭
            })
    });
}))
```

**需要观察的现象**：

- 弹窗出现后，**点遮罩不会关闭**（区别于普通 Dialog 的默认行为）。
- 弹窗**右上角没有关闭叉**。
- 只有点 "Cancel" 或按 ESC（`keyboard` 默认 true）才会取消；点 "Delete" 触发 `on_ok`、弹通知、并关闭。
- 把 `on_ok` 的返回值改成 `false`，点 "Delete" 后**弹窗不关**，模拟「删除失败」。

**预期结果**：能清楚区分 `Dialog`（可被随手关）与 `AlertDialog`（必须明确回应）两种模态的体感差异。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AlertDialog::new` 要把 `overlay_closable` 和 `close_button` 默认设成 `false`？

**答案**：因为 Alert 的语义是「打断、必须回应」。如果允许点遮罩或点叉随手关掉，用户可能在没看清警告的情况下误关，违背「打断式」目的。普通 `Dialog` 不强制，所以默认都开。

**练习 2**：下面这段代码在 debug 构建里会怎样？为什么？
```rust
AlertDialog::new(cx)
    .trigger(Button::new("b").label("Open"))
    .title("Hi")   // 这一行
```

**答案**：会触发 `debug_assert_no_trigger` 的 panic（见 [alert_dialog.rs:150-156](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/alert_dialog.rs#L150-L156)）。因为 `.trigger()` 走声明式路线（头部要由 `.content()` 里的 `DialogHeader` 等积木给出），不能再混用 `.title()` 这种命令式捷径。应改成在 `.content()` 里用 `DialogHeader`/`DialogTitle`。

**练习 3**：`open_alert_dialog` 内部最终调的是 `open_dialog`，那 `close_dialog` 能不能关掉一个 AlertDialog？

**答案**：能。对 `Root` 而言 `AlertDialog` 折叠后就是一个普通 `Dialog`，栈里没有「Alert」的概念。所以 `window.close_dialog(cx)`、ESC、点自定义按钮调 `close_dialog` 都能关它。

### 4.3 Root 的弹窗管理：栈、焦点与关闭动画

#### 4.3.1 概念说明

前两节都在讲「弹窗长什么样」，这一节讲「弹窗怎么活」。这套机制全部住在 `Root` 里，理解了它，你才能解释这些现象：

- 为什么 `Dialog` 是无状态组件，却能在屏幕上「持续存在」直到被关掉？
- 为什么弹窗里再开弹窗，关掉内层后焦点能正确回到外层？
- 为什么必须在根视图里手动 `.children(Root::render_dialog_layer(...))`？

核心答案：**`Root` 维护一个 `active_dialogs: Vec<ActiveDialog>` 栈，但它不存「弹窗对象」，只存「构建闭包 + 焦点句柄」。每帧重建。**

看一眼 `ActiveDialog` 长什么样就明白了：

[root.rs:71-91](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L71-L91) `ActiveDialog`：只有 `focus_handle`、`previous_focused_handle`（关闭时还原的焦点）和 `builder`（一个 `Fn(Dialog, &mut Window, &mut App) -> Dialog` 闭包）三个字段。

> 为什么只存闭包不存对象？因为 `Dialog` 是 `RenderOnce`，它「渲染一次就消费掉」，没法长期持有。`Root` 转而保存「怎么造一个 Dialog」的配方（闭包），每帧造一个新的来画。这样闭包里捕获的状态（你 `.title()`/`.child(...)` 写进去的东西）天然是「最新」的，因为每帧都用最新闭包重造。

#### 4.3.2 核心流程

```
                        ┌─────────────────────────────┐
   open_dialog ────────▶│  Root.active_dialogs (栈)    │
                        │  [ ActiveDialog{             │
                        │      focus_handle,           │
                        │      previous_focused_handle,│
                        │      builder } , ... ]       │
                        └─────────────┬───────────────┘
                                      │ 每帧 render
                                      ▼
                        render_dialog_layer(window, cx)
                          遍历栈，对每个 ActiveDialog：
                          1) dialog = Dialog::new(cx)
                          2) dialog = builder(dialog, ...)   # 跑你写的配置
                          3) dialog.focus_handle = active.focus_handle
                          4) dialog.layer_ix = i             # 栈位
                          5) 给「最高层且带遮罩」的那个画 overlay
                          div().children([dialog, ...])      # 一次性全画出来

   close_dialog ───────▶ active_dialogs.pop()
                         把 previous_focused_handle 还原焦点
                         cx.notify() → 下帧 render_dialog_layer 少画一个
```

#### 4.3.3 源码精读

**(1) `render_dialog_layer`：每帧重建所有弹窗。** 这是把弹窗「画出来」的唯一入口，是一个返回 `Option<impl IntoElement>` 的关联函数：

[root.rs:228-273](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L228-L273) `render_dialog_layer`：克隆 `active_dialogs`，逐个跑 builder 重建 `Dialog`，回填 `focus_handle` 与 `layer_ix`，找出「需要画遮罩」的那一个（遍历时不断覆盖 `show_overlay_ix`，最终是**最上层带遮罩**的弹窗），置 `overlay_visible = true`，最后 `div().children(dialogs)`。若栈空返回 `None`。

注意第 250-254 行的注释：因为 `dialog` 是临时值，没法在它身上长期保存 `focus_handle`，所以焦点句柄存在 `ActiveDialog` 里（归 `Root` 所有），每帧再「借」给临时 `Dialog`。

**(2) 必须手动挂载图层。** 这也是为什么 Story 的根视图要这么写：

[crates/story/src/lib.rs:693-715](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/lib.rs#L693-L715) `StoryRoot::render` 里 `.children(dialog_layer)`，把弹窗层叠在业务视图之上。

**这是初学者最常见的「弹窗不显示」原因**：调了 `open_dialog` 却忘了在根视图渲染 `render_dialog_layer`。`Root` 自己的 `render`（[root.rs:539-568](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L539-L568)）只画业务视图 + tooltip + 文本选择控制器，**并不**画弹窗——弹窗图层是留给外层（通常是你的根视图）显式挂的。这种「数据由 Root 管、渲染由调用方挂」的分工，和 u5-3 将要讲的 Sheet/Notification 一致。

**(3) `open_dialog`：记焦点、抢焦点、压栈。**

[root.rs:275-296](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L275-L296) `open_dialog`：先记下当前焦点为 `previous_focused_handle`（若有 `pending_focus_restore` 则用它，处理「关一个立刻开一个」的焦点衔接），新建 `focus_handle` 并立刻 `focus`（把焦点抢进弹窗），把三者包成 `ActiveDialog` 压栈，`cx.notify()`。

**(4) 关闭：即时关 vs 延迟关。** 有两个关闭路径，区别在于「关闭动画期间焦点怎么还」：

[root.rs:298-311](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L298-L311) `close_dialog_internal` / `close_dialog`：弹出栈顶，把它的 `previous_focused_handle` 升级并立刻 `window.focus` 还原焦点。点按钮（`render_ok`/`render_cancel`）和点遮罩都走这条**即时**路径。

[root.rs:313-334](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L313-L334) `defer_close_dialog`：弹出栈顶，但把焦点句柄存进 `pending_focus_restore`，然后 `spawn_in` 一个等 `ANIMATION_DURATION`（0.25s，与弹窗动画一致）的定时器，**动画结束后**再还原焦点；若期间又开了新弹窗（`current_dialogs_count != dialogs_count`）就不还原。这条**延迟**路径只给 Enter 键确认（`ConfirmDialog`）用，见 [dialog.rs:554-563](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/dialog.rs#L554-L563)。

为什么要分两条？因为按 Enter 确认时，弹窗要播完淡出动画才彻底消失，期间若立刻把焦点还给背景按钮，会出现「动画还没完，背景已经被聚焦/可点」的割裂感；延迟到动画结束再还焦点更顺滑，同时用 `pending_focus_restore` 兼容「动画期间又开了新弹窗」的边角情况。

**(5) `WindowExt`：对外只暴露 `Window` 方法。** 业务代码调用的 `window.open_dialog` / `window.close_dialog` 都在 `WindowExt` 里，它们只是转发给 `Root::update(...)`：

[window_ext.rs:135-164](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/window_ext.rs#L135-L164) `open_dialog` / `close_dialog` 都通过 `Root::update(self, cx, |root, window, cx| root.xxx(...))` 把操作落到唯一的 `Root` 实例上。

这也是 u1-4 讲过的 `window.root::<Root>()` 机制的体现：整个窗口只有一个 `Root`，所有弹窗操作最终都汇聚到它。

#### 4.3.4 代码实践

**实践目标**：通过源码阅读，验证「弹窗里再开弹窗」时焦点栈的正确性，并理解 `render_dialog_layer` 的栈重建行为。

**操作步骤**：

1. 阅读 [dialog_story.rs:206-235](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/dialog_story.rs#L206-L235) 的 "Basic Dialog"——它的 footer 里有一个 "Open Other Dialog" 按钮，点击会 `window.open_dialog` 再开一个弹窗，构成「弹窗套弹窗」。
2. 运行 Gallery → Dialog 页面，点 "Open Dialog" 打开第一个，再点 "Open Other Dialog" 打开第二个。
3. 在第二个弹窗里按 ESC 关闭它。

**需要观察的现象**：

- 第二个弹窗出现时，**只有它带遮罩**（栈顶才画遮罩），第一个弹窗的遮罩被「让位」。
- 关掉第二个后，焦点回到**第一个弹窗**（而不是背景），因为第二个的 `previous_focused_handle` 指向第一个弹窗。
- 再关掉第一个，焦点才回到最初的触发按钮。
- 打开「Focus back test」旁的输入框聚焦、再开 Dialog、再关，输入框重新获得焦点。

**预期结果**：焦点栈与弹窗栈严格对应，逐层弹出、逐层还原，验证 `previous_focused_handle` 的链式保存。

**源码阅读延伸**：在 `render_dialog_layer`（[root.rs:228-273](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L228-L273)）里，给 `dialog.layer_ix = i`。结合 `Dialog::render` 里「只有 `layer_ix + 1 == len()` 的弹窗才挂点遮罩关闭」（[dialog.rs:495-499](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/dialog.rs#L495-L499)），思考：如果两层弹窗都各自挂「点遮罩关闭」，会发生什么？（答案见下方练习 3。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Root` 不直接 `Entity<Dialog>` 把弹窗存成持久实体，而要存「闭包」？

**答案**：因为 `Dialog` 是 `RenderOnce`（设计上每帧 `render(self)` 消费 self），不适合长期持有；而且弹窗内容往往捕获了调用方的最新状态。存「构建闭包」让 `Root` 每帧用最新闭包重造一个临时 `Dialog`，既绕开了生命周期问题，又保证内容永远是最新的。

**练习 2**：用户调了 `window.open_dialog(...)`，但屏幕上什么都没出现。最可能的原因是什么？

**答案**：根视图的 `render` 里忘了 `.children(Root::render_dialog_layer(window, cx))`。`Root` 的数据层（栈）已经更新，但渲染层需要调用方显式挂载。`Root::render` 自己不画弹窗。参见 [crates/story/src/lib.rs:693-715](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/lib.rs#L693-L715)。

**练习 3**：假如「点遮罩关闭」不限制在栈顶弹窗，而是每个弹窗都挂，会有什么问题？

**答案**：当弹窗套弹窗时，点一下最外层遮罩区域，会同时触发多个弹窗的关闭事件（事件冒泡 + 多个监听者），可能一次关闭多层、或关闭顺序错乱，导致焦点栈（`previous_focused_handle`）错位。所以源码用 `layer_ix + 1 == len()` 把该事件**只**挂给栈顶弹窗。

## 5. 综合实践

把本讲三块知识串起来，做一个**「删除确认」完整流程**：

**需求**：一个用户列表视图，每行右侧有「删除」按钮。点击后：

1. 弹出 `AlertDialog`（命令式 `open_alert_dialog`），标题「删除用户」、描述该用户名、带一个红色垃圾桶图标。
2. OK 按钮是 `Danger` 变体、文案「删除」，并有 Cancel。
3. 点「删除」：在 `on_ok` 里把该用户从你的数据里移除、推一条成功 `Notification`（提示：用 `window.push_notification(...)`，下讲 u5-3 会讲），返回 `true` 关闭弹窗。
4. 关闭后焦点回到该行的「删除」按钮（验证 `Root` 的焦点还原）。
5. **额外要求**：在根视图的 `render` 里正确挂载 `Root::render_dialog_layer(window, cx)`，否则第 1 步的弹窗看不见。

**实现要点提示**（基于本讲已读源码）：

- 命令式 Alert 模板照抄 [alert_dialog_story.rs:99-126](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/alert_dialog_story.rs#L99-L126)，把用户名通过闭包捕获传进 `.description(...)`。
- 按钮配置用 `DialogButtonProps::default().ok_variant(ButtonVariant::Danger).ok_text("删除").show_cancel(true)`。
- `on_ok` 里返回 `true`（关闭）；想模拟「删除失败」就返回 `false`，观察弹窗不关。
- 根视图挂载图层照抄 [crates/story/src/lib.rs:693-715](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/lib.rs#L693-L715) 的 `.children(dialog_layer)`。

**验收**：删除按钮 → 弹窗（点遮罩关不掉、无关闭叉，符合 Alert 默认）→ 点删除 → 数据减少 + 成功通知 + 弹窗消失 + 焦点回到删除按钮。

> 待本地验证：`Notification` 的具体 API 与 `push_notification` 的表现将在 u5-3 讲义确认；本实践暂用 `window.push_notification("...", cx)` 即可跑通。

## 6. 本讲小结

- `Dialog` 是无状态 `RenderOnce` 组件，提供**声明式**（`.trigger(...)`）和**命令式**（`window.open_dialog`）两套等价用法；外观默认遮罩、ESC、点遮罩关闭都开，宽度 448px。
- 弹窗内容由 `DialogHeader / DialogTitle / DialogDescription / DialogContent / DialogFooter` 这套声明式积木拼装；`DialogClose` / `DialogAction` 是会分别派发 `CancelDialog` / `ConfirmDialog` 动作的「语义按钮壳」，让 footer 按钮天然支持 ESC/Enter。
- `on_ok` / `on_cancel` **返回 `bool`**：`true` 才关闭，`false` 保持打开——这是做「校验通过才关」的关键；`on_close` 是无论确认/取消都会触发的收尾回调。
- `AlertDialog` 是 `Dialog` 的「打断式」糖：默认 `overlay_closable(false)` + `close_button(false)`，必须明确点按钮才能关；它的 `.title/.description/.icon/.button_props` 命令式捷径最终经 `into_dialog` 折叠回一个普通 `Dialog`，且不能与 `.trigger()/.content()` 声明式路线混用。
- 弹窗的「存在与否」由 `Root.active_dialogs` 栈决定；`Root` 只存「构建闭包 + 焦点句柄」，每帧由 `render_dialog_layer` 重建——所以**必须在根视图手动挂载 `Root::render_dialog_layer`**，否则弹窗不显示。
- 焦点管理分两条关闭路径：点按钮/遮罩走即时 `close_dialog`，Enter 确认走延迟 `defer_close_dialog`（等动画结束再还焦点），`previous_focused_handle` 逐层保存焦点栈，弹窗套弹窗时焦点能正确逐层还原。

## 7. 下一步学习建议

- **u5-2 悬浮层（Popover / Tooltip / HoverCard）**：Dialog 是「模态」弹窗（抢焦点、遮罩），而 Popover 系列是「非模态」轻量浮层（不抢焦点、点击外部即消）。对比学习能让你彻底分清两类浮层的取舍。
- **u5-3 抽屉与通知（Sheet / Notification）**：Sheet 和 Dialog 一样由 `Root` 统一管理、一样需要 `render_sheet_layer` / `render_notification_layer` 挂载图层，机制高度同构，正好巩固本讲的「Root 栈 + 闭包重建 + 焦点还原」范式；本讲综合实践用到的 `push_notification` 也在那里详讲。
- **深读建议**：想彻底吃透焦点系统，可回看 u2-4 的 `FocusTrapElement` 与 `FocusTrapManager`，再对照本讲 `Dialog::render` 里 `.focus_trap(...)` 与 `Root` 的 `Tab/TabPrev` 拦截（[root.rs:453-514](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L453-L514)），把「模态焦点循环」从底层到组件层串通。
