# 抽屉与通知：Sheet 与 Notification

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `Sheet`（侧边抽屉）与 `Notification`（全局通知）这两类「窗口级浮层」是如何被 `Root` 统一承载的，并理解它们和 u5-l1 的 `Dialog` 在管理模型上的**关键差异**。
- 掌握 `Sheet` 的打开 / 关闭、四个方向（Placement）、尺寸、遮罩、关闭回调的用法，并能解释它的滑入动画原理。
- 掌握 `Notification` 的推送、四种类型（Info/Success/Warning/Error）、自动消失、唯一 id 去重、操作按钮的用法。
- 能够在自己的根视图里正确**挂载** `render_sheet_layer` 与 `render_notification_layer`，否则这两者都「调了却看不到」。
- 完成一个综合实践：点按钮从右侧滑出设置抽屉，抽屉里点「保存」后弹一条成功通知。

## 2. 前置知识

本讲默认你已经学完 **u5-l1 弹窗：Dialog 与 Alert**，那里建立了两条贯穿本讲的核心认知，这里只做承接、不再展开：

1. **`Root` 是窗口的「管理壳」**：它不是业务视图，而是集中托管窗口级浮层（Sheet / Dialog / Notification / Tooltip）与焦点导航的顶层视图，每个窗口的第一层视图必须是 `Root`。
2. **「数据层 + 渲染层」分离**：`Root` 只在内存里保存浮层的「存在状态」（栈 / 字段 / 实体），而把浮层真正画出来的 `render_*_layer` 是一组**关联函数**，必须由你的根视图 `render` 手动 `.children(...)` 挂载。`Root::render` 自己**不画**这些浮层。

此外请回忆 u2-l1 的 `cx.theme()` 全局主题、u2-l2 的 `Styled` / `RenderOnce`、u2-l4 的 `focus_trap` / `on_prepaint` 探针、u4 的「状态外置 + 回调上报 + `cx.notify()`」闭环范式——它们在本讲都会反复出现。

> 一个直觉对比：`Dialog` 打断你、逼你回应；`Sheet` 是从屏幕边缘滑入的临时面板，通常承载表单 / 导航 / 设置；`Notification` 则是「不打断、自动消失」的轻量提示。三者都属于窗口级浮层，但「谁来存状态、状态是单个还是多个、要不要重建」各不相同，这正是本讲要讲透的地方。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/ui/src/sheet.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sheet.rs) | `Sheet` 组件：无状态 `RenderOnce`，定义抽屉的结构（标题栏 / 可滚动正文 / 页脚）、遮罩、滑入动画与关闭逻辑。 |
| [crates/ui/src/notification.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/notification.rs) | `Notification`（单条通知）与 `NotificationList`（通知列表实体）：有状态 `Render` 视图，管理 `VecDeque`、去重、订阅 `DismissEvent`、5 秒自动消失。 |
| [crates/ui/src/root.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs) | `Root` 管理壳：`active_sheet` 字段、`notification: Entity<NotificationList>`、`render_sheet_layer` / `render_notification_layer`、`open_sheet_at` / `close_sheet` / `push_notification` 等方法。 |
| [crates/ui/src/window_ext.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/window_ext.rs) | `WindowExt` trait：把 `window.open_sheet(...)` / `window.push_notification(...)` 等便捷方法转发给 `Root`，是日常使用的入口。 |
| [examples/dialog_overlay/src/main.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/examples/dialog_overlay/src/main.rs) | 一个最小可运行示例：演示 `open_sheet` 并在 `render` 末尾挂载 `render_dialog_layer` / `render_sheet_layer`。 |

## 4. 核心概念与源码讲解

### 4.1 Root 承载：Sheet 与 Notification 如何挂在窗口上

#### 4.1.1 概念说明

`Sheet` 和 `Notification` 都不是「你 new 出来 add 到某个容器里」就完事的普通组件——它们是**窗口级浮层**，必须浮在所有业务内容之上、由窗口唯一的 `Root` 统一调度。这与 u5-l1 的 `Dialog` 一脉相承。

但三者被 `Root` 管理的方式并不相同，这是本讲最重要的结构认知：

| 浮层 | `Root` 里的存储字段 | 数量模型 | 渲染方式 |
| --- | --- | --- | --- |
| `Dialog` | `active_dialogs: Vec<ActiveDialog>` | **栈**，支持「弹窗里再开弹窗」 | 每帧由 `render_dialog_layer` 跑 builder 闭包重建 |
| `Sheet` | `active_sheet: Option<ActiveSheet>` | **单个**，新开一个会替换旧的 | 每帧由 `render_sheet_layer` 跑 builder 闭包重建 |
| `Notification` | `notification: Entity<NotificationList>` | **可堆叠的列表** | 一个**常驻的有状态实体视图**，自己 `Render`，不靠闭包重建 |

可以看到：

- `Sheet` 沿用了 `Dialog` 的「**描述 + 闭包重建**」范式——`Root` 只存一个构建闭包与焦点句柄，每帧重建一个临时的 `Sheet` 值。
- `Notification` 走的是另一条路——它是一个**真正存活的状态实体**（`Entity<NotificationList>`），内部用 `VecDeque` 维护多条通知，自己订阅关闭事件、自己跑自动消失计时器。`Root` 只是持有它的句柄。

> 为什么 `Notification` 不像 Dialog/Sheet 那样「每帧重建」？因为通知需要**累积、去重、各自倒计时**，这些是跨帧的持久状态，用「闭包描述 + 每帧重建」表达不了，所以它必须是常驻实体。这是一个很典型的「选型」：临时性、单次的浮层用描述式重建；持续累积的状态用实体视图。

#### 4.1.2 核心流程

以「点按钮 → 打开 Sheet / 推送 Notification」为例，整体调用链如下：

```
你的代码: window.open_sheet(cx, |sheet,_,_| { sheet.title(...) })      # 声明式
   └─ WindowExt::open_sheet  →  Root::open_sheet_at                     # window_ext.rs
        └─ 存 ActiveSheet{ builder, focus_handle, previous_focused_handle }
        └─ cx.notify() 触发 Root 重绘
              └─ 你的根视图 render 里早就挂了 .children(Root::render_sheet_layer(window, cx))
                     └─ render_sheet_layer 克隆 active_sheet，跑 builder 重建 Sheet 并渲染
```

```
你的代码: window.push_notification(Notification::success("已保存"), cx)
   └─ WindowExt::push_notification  →  Root::push_notification           # window_ext.rs
        └─ root.notification.update(|list| list.push(note, ...))         # 转给常驻实体
        └─ NotificationList::push: 去重 → subscribe(DismissEvent) → 倒计时 5s
              └─ cx.notify() 触发 NotificationList 重绘
                     └─ 你的根视图 render 里早就挂了 .children(Root::render_notification_layer(...))
                            └─ 直接渲染 root.notification 这个实体（它自己 Render）
```

两条链路最终的「画面出口」都是你根视图里手动挂载的 `render_*_layer`。**漏挂 = 调了也看不见。**

#### 4.1.3 源码精读

**`Root` 结构体里与 Sheet/Notification 相关的字段：**

[root.rs:38-60](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L38-L60)：注意 `active_sheet` 是 `Option<ActiveSheet>`（单个），而 `notification` 是 `Entity<NotificationList>`（常驻实体）。

```rust
pub struct Root {
    // ...
    pub(crate) active_sheet: Option<ActiveSheet>,
    pub(crate) active_dialogs: Vec<ActiveDialog>,   // 对照：Dialog 是栈
    pub notification: Entity<NotificationList>,      // 对照：Notification 是实体
    sheet_size: Option<DefiniteLength>,              // 记录当前 Sheet 尺寸，供通知避让
    // ...
}
```

`ActiveSheet` 只是一个「描述包」，里面有一个构建闭包 `builder`，没有任何 `Sheet` 实体：

[root.rs:62-69](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L62-L69)：

```rust
pub(crate) struct ActiveSheet {
    focus_handle: FocusHandle,
    previous_focused_handle: Option<WeakFocusHandle>, // 关闭后要把焦点还给它
    placement: Placement,
    builder: Rc<dyn Fn(Sheet, &mut Window, &mut App) -> Sheet + 'static>,
}
```

**`Root::new` 时就创建好通知列表实体：**

[root.rs:95-113](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L95-L113)：`notification` 在 `Root` 一诞生时就 `cx.new(|cx| NotificationList::new(...))` 创建，整个窗口生命周期内常驻。

**最关键的「画面出口」——两个 layer 关联函数：**

[root.rs:202-225](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L202-L225) `render_sheet_layer`：每帧克隆 `active_sheet`，**新建一个临时 `Sheet`**，跑 `builder` 闭包把 title/child/footer 配进去，再回填 `focus_handle` 与 `placement`，然后用 `on_prepaint` 探针把当前 Sheet 的 `size` 记到 `root.sheet_size`（供通知避让用，见 4.1 末）。`active_sheet` 为 `None` 时返回 `None`。

[root.rs:153-199](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L153-L199) `render_notification_layer`：根据主题里的 `notification.placement`（默认右上角）把一个 `div` 定位到对应角，然后直接 `.child(root.read(cx).notification.clone())` 把常驻实体渲染出来。注意它在开头会读取 `active_sheet` 的 placement/size，给通知加一段 margin——**这样 Sheet 打开时通知不会被抽屉盖住**，体现了 Root 对两类浮层的协调。

**`Root::render` 自己不画这两个浮层：**

[root.rs:539-568](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L539-L568)：`Render` 实现里只画了 `self.view`（业务视图）、`tooltip_overlay`、`native_menu_overlay`，**没有** `render_sheet_layer` / `render_notification_layer`。这就是为什么挂载责任落在调用方——和 u5-l1 的 Dialog 完全一样的「坑」。

#### 4.1.4 代码实践

**实践目标**：用源码阅读验证「漏挂 layer 就看不见」这一结论，并理解 `Root` 字段模型。

**操作步骤**：

1. 打开 [examples/dialog_overlay/src/main.rs:76-77](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/examples/dialog_overlay/src/main.rs#L76-L77)，确认它的 `render` 末尾有 `.children(Root::render_dialog_layer(window, cx))` 和 `.children(Root::render_sheet_layer(window, cx))`。
2. 在 [root.rs:539-568](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L539-L568) 确认 `Root::render` 里确实没有调用任何 `render_*_layer`。
3. 对照 [root.rs:38-60](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L38-L60)，把 `active_sheet`、`active_dialogs`、`notification` 三个字段的类型与「单/栈/实体」模型填入上面的表格。

**需要观察的现象**：如果你把示例 `render` 里 `.children(Root::render_sheet_layer(window, cx))` 注释掉再运行，点「Open Drawer」按钮，`Root.active_sheet` 确实被写入了（可以用 `window.has_active_sheet(cx)` 返回 `true` 验证），但屏幕上看不到抽屉。

**预期结果**：数据层已更新、渲染层缺失 → 浮层不显示。这正是「数据层 + 渲染层分离」的直接体现。**待本地验证**（本环境不运行 GUI）。

#### 4.1.5 小练习与答案

**练习 1**：`Sheet` 用 `Option<ActiveSheet>`（单个），而 `Dialog` 用 `Vec<ActiveDialog>`（栈）。如果你在 Sheet 里再调一次 `window.open_sheet(...)` 会发生什么？

**答案**：新 Sheet 会**替换**旧 Sheet。`open_sheet_at` 会先 `self.active_sheet.take()`，只回收旧 Sheet 的 `previous_focused_handle`，丢弃旧的 `builder`，再写入新的 `ActiveSheet`（见 [root.rs:358-371](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L358-L371)）。所以 Sheet 不支持「抽屉里再开抽屉」的嵌套，这一点与可嵌套的 Dialog 不同。

**练习 2**：为什么 `notification` 是 `Entity<NotificationList>` 常驻实体，而不是像 Sheet 那样存「构建闭包」？

**答案**：通知需要跨帧累积（多条并存）、按 id 去重、每条各自倒计时 5 秒自动消失，这些是**跨帧持久状态**，闭包「每帧重建」的描述式模型表达不了。只有常驻实体才能持有 `VecDeque`、`Subscription` 和后台计时器。

---

### 4.2 Sheet：从边缘滑入的侧边抽屉

#### 4.2.1 概念说明

`Sheet` 是一个**无状态**的 `RenderOnce` 组件（回忆 u2-l2：每帧重建、自身不持跨帧状态）。它本身不会自己出现在屏幕上——你从不直接 `Sheet::new(...)` 加到布局里，而是通过 `window.open_sheet(cx, |sheet, ...| { ...配置... })` 把「配置闭包」交给 `Root`，由 `render_sheet_layer` 在浮层阶段重建并绘制。

`Sheet` 提供：

- **标题栏**（内置关闭按钮）、**可滚动正文**、**可选页脚**三段式结构。
- **四个方向** `Placement::Left / Right / Top / Bottom`，默认 `Right`。
- **遮罩**（半透明背景，点一下可关闭）、**尺寸**（左右是宽度，上下是高度，默认 350px）。
- **滑入动画**（0.15 秒）、**ESC 关闭**、**焦点陷阱**（回忆 u2-l4，Tab 不会逃出抽屉）。

#### 4.2.2 核心流程

```
window.open_sheet(cx, |sheet, window, cx| sheet.title("设置").child(...))
  └─ 默认 placement = Right（open_sheet_at 可改方向）
  └─ 你在闭包里调用 .title / .child / .footer / .size / .overlay 等链式方法配置
        └─ 这些方法只是改 Sheet 结构体字段（builder 模式，返回 self）

每帧渲染时（render_sheet_layer 重建后调用 Sheet::render）:
  └─ anchored() 把整层钉到窗口左上角（含窗口 padding / CSD 边框补偿）
  └─ 遮罩层 div：铺满整窗，overlay 时点左键 → window.close_sheet + on_close
  └─ 抽屉本体 v_flex：
        ├─ focus_trap + key_context("Sheet") + on_action(Cancel) → ESC 关闭
        ├─ 按 placement 定位到对应边缘（top + 边距、right_0…）
        ├─ 标题栏（标题 + 关闭按钮，点关闭 → close_sheet + on_close）
        ├─ 正文（flex_1 + overflow_y_scrollbar，自动滚动）
        ├─ 页脚（可选）
        └─ with_animation("slide") 把抽屉从屏幕外平移到位
```

**滑入动画的数学**：动画进度 `delta` 从 0 → 1，抽屉用一个偏移量把自己从「屏幕外」平移到「贴边」：

\[
\text{offset} = -100\,\text{px} + \text{delta} \times 100\,\text{px}
\]

`delta = 0` 时 offset = −100px（在视口外），`delta = 1` 时 offset = 0（就位）。不同方向把这个 offset 套到 `top / right / bottom / left` 上。

#### 4.2.3 源码精读

**`Sheet` 结构体与默认值：**

[sheet.rs:44-58](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sheet.rs#L44-L58) 与 [sheet.rs:62-76](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sheet.rs#L62-L76)：注意默认 `placement = Right`、`size = 350px`、`overlay = true`、`overlay_closable = true`。

```rust
pub struct Sheet {
    pub(crate) focus_handle: FocusHandle,
    pub(crate) placement: Placement,
    pub(crate) size: DefiniteLength,
    resizable: bool,
    on_close: Rc<dyn Fn(&ClickEvent, &mut Window, &mut App) + 'static>,
    title: Option<AnyElement>,
    footer: Option<AnyElement>,
    // ...
    overlay: bool,
    overlay_closable: bool,
}
```

> 说明：`resizable` 字段与其 setter `resizable(bool)` 存在（见 [sheet.rs:96-100](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sheet.rs#L96-L100)），但当前 `render` 并未读取它来渲染拖拽手柄，所以它目前只是「存了值、暂未接线」的字段，不要误以为设了它就一定能拖拽改尺寸（待确认后续版本是否会启用）。

**ESC 关闭的绑定与处理：**

[sheet.rs:24-27](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sheet.rs#L24-L27) 在 `init` 里把 `escape` 绑定到 `Cancel` 动作（作用域 `"Sheet"`）：

```rust
const CONTEXT: &str = "Sheet";
pub(crate) fn init(cx: &mut App) {
    cx.bind_keys([KeyBinding::new("escape", Cancel, Some(CONTEXT))])
}
```

抽屉本体在渲染时打上 `.key_context(CONTEXT)` 并 `.on_action(|_: &Cancel, ...| { window.close_sheet(cx); on_close(...) })`（[sheet.rs:190-204](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sheet.rs#L190-L204)），于是焦点在抽屉内时按 ESC 就会关闭它——和 u5-l1 Dialog 用 `key_context("Dialog")` 是同一套机制。

**遮罩 + 点遮罩关闭：**

[sheet.rs:164-189](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sheet.rs#L164-L189)：遮罩是一个铺满整窗、`occlude()` 的 `div`。`overlay_closable && 鼠标左键` 时调用 `window.close_sheet(cx)` 并触发 `on_close`。这里有个细节：`if event.position.y < top { return; }`——点击落在标题栏高度以上的区域不关闭，避免误触窗口标题栏。

**抽屉本体的定位（按方向）：**

[sheet.rs:219-226](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sheet.rs#L219-L226)：用 `match self.placement` 决定贴哪条边、加哪条边框，`top` 统一用 `cx.theme().sheet.margin_top`（默认等于 `TITLE_BAR_HEIGHT`，避开标题栏）。

```rust
.map(|this| match self.placement {
    Placement::Top => this.top(top).left_0().right_0().border_b_1(),
    Placement::Right => this.top(top).right_0().bottom_0().border_l_1(),
    Placement::Bottom => this.bottom_0().left_0().right_0().border_t_1(),
    Placement::Left => this.top(top).left_0().bottom_0().border_r_1(),
})
```

**标题栏内置关闭按钮：**

[sheet.rs:227-247](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sheet.rs#L227-L247)：标题栏右侧固定有一个 `ghost` 风格的 `Close` 图标按钮，点击同样 `window.close_sheet(cx)` + `on_close`。

**正文与页脚：**

[sheet.rs:248-269](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sheet.rs#L248-L269)：正文是 `.flex_1().overflow_hidden()` 的容器，里面再放一个 `.overflow_y_scrollbar()` 的 `v_flex`，所以**内容超出会自动出滚动条**；`.footer(...)` 给了才渲染页脚（用 `when_some`）。

**滑入动画：**

[sheet.rs:270-282](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sheet.rs#L270-L282)：`with_animation("slide", Animation::new(0.15s), |this, delta| ...)`，按方向把前面讲的 offset 套到对应边。

**`on_close` 回调：**

[sheet.rs:115-122](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sheet.rs#L115-L122)：`on_close` 在三个「用户通过 UI 主动关闭」的地方被调用——点遮罩、按 ESC、点关闭按钮。注意：**它不会**在外部直接调用 `window.close_sheet(cx)` 时触发（后者只清空 `active_sheet`，见 [root.rs:375-387](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L375-L387)）。如果你需要「无论怎么关都通知一声」，要在程序化关闭处自己补调用。

**`open_sheet_at` 与 `close_sheet`：**

[root.rs:349-373](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L349-L373) `open_sheet_at`：保存「打开前的焦点句柄」`previous_focused_handle`，新建 `focus_handle` 并立刻 `focus`（把焦点抓进抽屉），存好 `ActiveSheet` 后 `cx.notify()`。

[root.rs:375-387](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L375-L387) `close_sheet`：把焦点还给 `previous_focused_handle`，清空 `active_sheet`，`cx.notify()`。

#### 4.2.4 代码实践

**实践目标**：在最小示例里打开一个右侧抽屉，验证四个方向与关闭方式。

**操作步骤**：

1. 参考 [examples/dialog_overlay/src/main.rs:16-20](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/examples/dialog_overlay/src/main.rs#L16-L20) 的 `show_drawer` 写法：

   ```rust
   // 示例代码
   fn show_drawer(&mut self, _: &ClickEvent, window: &mut Window, cx: &mut Context<Self>) {
       window.open_sheet(cx, |sheet, _, _| {
           sheet.title("Test Drawer").child("Hello from Drawer!")
       });
   }
   ```

2. 把 `open_sheet` 改成 `window.open_sheet_at(Placement::Left, cx, ...)`，再分别试 `Top` / `Bottom`（需要 `use gpui_component::Placement;`）。
3. 运行后分别用三种方式关闭：点遮罩、按 ESC、点标题栏关闭按钮。

**需要观察的现象**：每次切换方向，抽屉从对应边缘滑入；三种关闭方式都能让抽屉消失并把焦点还给打开前的控件。

**预期结果**：默认 `open_sheet` 从右侧滑入；改 `Placement` 后方向随之变化；关闭后焦点回到触发按钮。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：你给 Sheet 配了 `.on_close(|_, window, cx| { window.push_notification("关了", cx); })`。请问哪些关闭路径会触发这条通知？如果你在代码里直接 `window.close_sheet(cx)`（不是点按钮），通知会弹吗？

**答案**：点遮罩、按 ESC、点标题栏关闭按钮这三条「用户主动关闭」路径会触发 `on_close`（[sheet.rs:183-186](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sheet.rs#L183-L186)、[197-203](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sheet.rs#L197-L203)、[242-245](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sheet.rs#L242-L245)）。但程序化 `window.close_sheet(cx)`（[root.rs:375-387](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L375-L387)）**不会**触发——它只清字段、还焦点，不调用 `on_close`。

**练习 2**：`Sheet` 的 `placement` 默认是哪一边？依据是哪段源码？

**答案**：默认 `Placement::Right`。依据有二：`Sheet::new` 里 `placement: Placement::Right`（[sheet.rs:65](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sheet.rs#L65)），以及 `WindowExt::open_sheet` 内部直接调 `self.open_sheet_at(Placement::Right, cx, build)`（[window_ext.rs:105-110](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/window_ext.rs#L105-L110)）。

---

### 4.3 Notification：可堆叠、自动消失的全局通知

#### 4.3.1 概念说明

`Notification` 是「Toast」式的轻量提示：出现在窗口一角（默认右上角），不打断操作、不抢焦点，默认 5 秒后自动消失。它由两部分组成：

- **`Notification`**：单条通知，是一个**有状态**的 `Render` 实体（注意它不是 `RenderOnce`——因为它要播放关闭动画、跟踪 `closing` 状态）。
- **`NotificationList`**：通知列表，是 `Root` 常驻持有的实体，用 `VecDeque` 维护多条通知，负责去重、订阅每条的 `DismissEvent`、调度 5 秒自动消失计时器。

使用入口很简单：`window.push_notification("xxx", cx)`，或用 `Notification::success(...)` / `.error(...)` 等构造器精确控制类型、标题、操作按钮。

它支持：

- **四种类型** `Info / Success / Warning / Error`，每种带对应的图标与主题色。
- **自动消失**（默认开，5 秒；设了 `.action(...)` 操作按钮会自动关掉自动消失，等用户处理）。
- **唯一 id 去重**：默认每条用随机 UUID（所以总是追加到列表末尾）；用 `.id::<T>()` / `.id1::<T>(key)` 指定 id 后，再 push 同 id 会**替换**旧的那条，适合「上传中 → 上传完成」这种状态更新。
- **关闭路径**：右上角关闭按钮（悬停才显示）、中键点击、点通知本体（若设了 `on_click`）、操作按钮、自动消失、以及程序化 `remove_notification`。

#### 4.3.2 核心流程

```
window.push_notification(Notification::success("已保存"), cx)
  └─ Root::push_notification → NotificationList::push(note)
        ├─ 按 id 去重：retain 掉同 id 的旧通知
        ├─ cx.new(|_| notification)  创建单条实体
        ├─ subscribe(notification, DismissEvent) → 收到时从列表移除
        ├─ push_back 到 VecDeque，cx.notify()
        └─ 若 autohide：后台计时器 5s 后调用 note.dismiss()

单条关闭 dismiss():
  └─ closing = true，cx.notify()（开始播 0.15s 淡出动画）
  └─ 0.15s 后 emit(DismissEvent) + 触发 on_close
        └─ NotificationList 订阅收到 DismissEvent → 从 VecDeque 移除该条

渲染（NotificationList::render）:
  └─ 取最后 max_items（默认 10）条，按 placement 定位到窗口一角
  └─ 每条 Notification::render：图标 + 标题 + 正文 + 可选操作按钮 + 关闭按钮 + 滑入/淡出动画
```

#### 4.3.3 源码精读

**四种类型与图标：**

[notification.rs:23-41](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/notification.rs#L23-L41)：`NotificationType` 的 `icon` 方法按类型返回带主题色的图标（Info→`info` 色、Success→`success` 色、Warning→`warning` 色、Error→`danger` 色）。

**便捷构造器与随机 id：**

[notification.rs:116-138](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/notification.rs#L116-L138)：`Notification::new()` 默认用 `uuid::Uuid::new_v4()` 生成 id——这正是「不指定 id 时总是追加新条目」的原因。

[notification.rs:147-172](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/notification.rs#L147-L172)：`info/success/warning/error` 四个关联函数，等价于 `Self::new().message(...).with_type(...)`。

**唯一 id（去重的关键）：**

[notification.rs:180-189](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/notification.rs#L180-L189)：

```rust
/// 用一个空 struct 的 TypeId 当 id
pub fn id<T: Sized + 'static>(mut self) -> Self {
    self.id = TypeId::of::<T>().into();
    self
}
/// 用 TypeId + 额外 key，支持同类型下多条
pub fn id1<T: Sized + 'static>(mut self, key: impl Into<ElementId>) -> Self {
    self.id = (TypeId::of::<T>(), key.into()).into();
    self
}
```

**操作按钮会关闭自动消失：**

[notification.rs:240-247](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/notification.rs#L240-L247)：`.action(...)` 在存 `action_builder` 的同时**强制 `self.autohide = false`**——因为既然给了操作按钮，就该等用户处理，不能 5 秒后自己消失。

**关闭动画 `dismiss`：**

[notification.rs:250-273](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/notification.rs#L250-L273)：置 `closing = true` 触发重绘（渲染侧据此播淡出），用后台计时器等 0.15 秒，然后 `emit(DismissEvent)` 并触发 `on_close`。`emit` 出去后，列表的订阅会把它移除。

**单条渲染（容器 + 图标 + 文案 + 操作 + 关闭 + 动画）：**

[notification.rs:293-323](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/notification.rs#L293-L323)：固定宽 `w_112()`、圆角、阴影、`popover` 背景的卡片。

[notification.rs:327-372](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/notification.rs#L327-L372)：左侧绝对定位的图标、标题（`font_semibold`）、正文、可选 `action` 按钮；右上角有个 `invisible` 的关闭按钮，靠 `.group_hover("")` 在鼠标悬停整条通知时才 `visible`；`on_click`（若有）会在触发后先 `dismiss`；`on_aux_click` 处理**中键关闭**（`is_middle_click()`）。

[notification.rs:373-419](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/notification.rs#L373-L419)：滑入 / 淡出动画。出现时按 placement 从上方/下方平移 45px 进入并淡入；关闭（`closing`）时淡出并按方向滑出 45px，透明度低于 0.85 时去掉阴影避免拖影。

**通知设置（位置 / 边距 / 最大条数）：**

[notification.rs:423-448](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/notification.rs#L423-L448)：`NotificationSettings` 默认 `placement = TopRight`、`max_items = 10`，`margins.top` 加了 `TITLE_BAR_HEIGHT` 避开标题栏。它挂在主题上，通过 `cx.theme().notification` 访问。

**`NotificationList::push`：去重 + 订阅 + 5 秒自动消失：**

[notification.rs:467-505](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/notification.rs#L467-L505)：

```rust
// 1. 按 id 去重：同 id 的旧通知先移除
self.notifications.retain(|note| note.read(cx).id != id);
// 2. 创建实体并订阅它的 DismissEvent，收到就移除
let notification = cx.new(|_| notification);
self._subscriptions.insert(id.clone(),
    cx.subscribe(&notification, move |view, _, _: &DismissEvent, cx| {
        view.notifications.retain(|note| id != note.read(cx).id);
        view._subscriptions.remove(&id);
    }),
);
self.notifications.push_back(notification.clone());
// 3. 若 autohide，5 秒后 dismiss
if autohide {
    cx.spawn_in(window, async move |_, cx| {
        cx.background_executor().timer(Duration::from_secs(5)).await;
        notification.update_in(cx, |note, window, cx| note.dismiss(window, cx)).ok();
    }).detach();
}
```

这段集中体现了「常驻实体」的价值：它持有订阅 `_subscriptions` 和后台计时器，这些都是跨帧状态。

**列表渲染：只显示最后 N 条、按位置排版：**

[notification.rs:552-601](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/notification.rs#L552-L601)：用 `.rev().take(max_items).rev()` 取「最后 max_items 条但保持原顺序」，再按 `placement` 决定左/右/居中、是否 `flex_col_reverse`（底部位置时新通知往上堆）。

**`push_notification` 与 `remove_notification`：**

[root.rs:389-398](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L389-L398) `push_notification` 转给实体；[root.rs:400-431](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L400-L431) `remove_notification::<T>` / `remove_notification1::<T>(key)` / `clear_notifications` 分别按类型、按类型+key、全部清除。注意 `remove_notification::<T>` 用的是 `close_by_type`（[notification.rs:520-540](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/notification.rs#L520-L540)），会一并清掉该类型下用 `id` 和 `id1` 注册的所有通知。

#### 4.3.4 代码实践

**实践目标**：亲手验证「随机 id 追加」与「指定 id 替换」两种行为，并触发一次操作按钮通知。

**操作步骤**：

1. 连续三次调用 `window.push_notification("A", cx);`（默认随机 id）。
2. 再定义一个空 struct `struct UploadN;`，先 push：

   ```rust
   // 示例代码
   window.push_notification(
       Notification::info("上传中...").id::<UploadN>().title("文件上传").autohide(false),
       cx,
   );
   ```
   然后用**同样的** `id::<UploadN>()` 再 push 一条 `Notification::success("上传完成").title("完成")`。

3. 试一条带操作按钮的通知：

   ```rust
   // 示例代码
   window.push_notification(
       Notification::error("连接失败")
           .title("网络错误")
           .action(|_, _, cx| {
               Button::new("retry").primary().label("重试")
                   .on_click(cx.listener(|this, _, window, cx| {
                       println!("重试"); this.dismiss(window, cx);
                   }))
           }),
       cx,
   );
   ```

**需要观察的现象**：第 1 步会堆叠出三条独立的 "A"；第 2 步第二条会**替换**第一条（列表里始终只有一条 `UploadN`，内容从「上传中」变成「完成」）；第 3 步的通知**不会**自动消失（因为有 `.action`），且悬停时才显示关闭按钮。

**预期结果**：随机 id → 追加；相同 `id::<T>()` → 替换；`.action(...)` → 自动关闭自动消失。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么「设了 `.action(...)` 的通知不会自动消失」？用源码解释。

**答案**：`action` 方法在存 `action_builder` 后**显式置 `self.autohide = false`**（[notification.rs:240-247](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/notification.rs#L240-L247)）。而 `push` 里的 5 秒计时器只在 `if autohide` 分支才启动（[notification.rs:491-503](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/notification.rs#L491-L503)），所以带操作按钮的通知会一直等到用户处理或手动关闭。

**练习 2**：你想做一个「上传中 → 上传成功」的进度通知，应该用随机 id 还是 `.id::<T>()`？为什么？

**答案**：用 `.id::<T>()`。因为默认随机 id 每次 push 都追加新条目，会出现「上传中」和「上传完成」两条并存；用同一个 `id::<T>()` 时，`push` 会先 `retain` 移除同 id 的旧条目（[notification.rs:478](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/notification.rs#L478)），从而把「上传中」原位更新成「上传完成」。docs 也正是这样示范的（见 [docs/docs/components/notification.md:215-235](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/docs/docs/components/notification.md#L215-L235)）。

## 5. 综合实践

**任务**：实现一个「设置入口」——点按钮从右侧滑出 Sheet 设置抽屉，抽屉里放一个 Switch 开关 + 一个「保存」按钮；点「保存」后：① 弹一条成功通知，② 关闭抽屉。

下面是一份**示例代码**（基于 `examples/dialog_overlay` 的骨架改写，省略了 `Cargo.toml` 与 imports 的完整罗列，重点看流程）：

```rust
// 示例代码：src/main.rs
use gpui::*;
use gpui_component::{notification::Notification, *};

#[derive(Default)]
struct AppView {
    notify_enabled: bool, // Sheet 内 Switch 的状态外置到这里
}

impl AppView {
    fn open_settings(&mut self, _: &ClickEvent, window: &mut Window, cx: &mut Context<Self>) {
        let notify = self.notify_enabled; // 闭包捕获当前值
        window.open_sheet(cx, move |sheet, _, _| {
            sheet
                .title("设置")
                .size(px(380.))
                .child(
                    v_flex().gap_4().py_4().child(
                        Switch::new()
                            .label("开启通知")
                            .checked(notify), // 下发当前状态（见 u4-2 状态外置范式）
                    ),
                )
                // 页脚放「保存」按钮：注意它要能改 AppView，所以用 closure 捕获的事后逻辑放在 on_close 里
                .footer(
                    h_flex().justify_end().gap_2().child(
                        Button::new("save")
                            .primary()
                            .label("保存")
                            .on_click(move |_, window, cx| {
                                // ① 弹成功通知
                                window.push_notification(
                                    Notification::success("设置已保存"),
                                    cx,
                                );
                                // ② 程序化关闭抽屉（注意：这条路径不会触发 on_close）
                                window.close_sheet(cx);
                            }),
                    ),
                )
        });
    }
}

impl Render for AppView {
    fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        div()
            .size_full()
            .child(TitleBar::new().child("Sheet + Notification"))
            .child(
                div().p_8().child(
                    Button::new("open")
                        .primary()
                        .label("打开设置")
                        .on_click(cx.listener(Self::open_settings)),
                ),
            )
            // —— 关键：必须手动挂载这两个 layer，否则什么都看不见 ——
            .children(Root::render_sheet_layer(window, cx))
            .children(Root::render_notification_layer(window, cx))
    }
}

fn main() {
    let app = gpui_platform::application().with_assets(gpui_component_assets::Assets);
    app.run(move |cx| {
        gpui_component::init(cx); // 必须最先调用：注册 theme/root/sheet/notification 等
        cx.spawn(async move |cx| {
            cx.open_window(WindowOptions::default(), |window, cx| {
                let view = cx.new(|_| AppView::default());
                cx.new(|cx| Root::new(view, window, cx)) // 窗口第一层必须是 Root
            })
            .expect("Failed to open window");
        })
        .detach();
    });
}
```

**验证清单**：

1. 点「打开设置」→ 抽屉从右侧滑入，焦点进入抽屉（Tab 不会逃出去，因 `focus_trap`）。
2. 按 ESC / 点遮罩 / 点关闭按钮 都能关闭抽屉。
3. 点「保存」→ 右上角弹出绿色「设置已保存」通知，约 5 秒后自动消失，同时抽屉关闭。
4. 把 `render` 末尾两行 `.children(...)` 注释掉任意一行，对应浮层就消失——验证「挂载责任在调用方」。

> 进阶：把「保存」按钮的逻辑改成「保存后用 `Notification::success(...).id::<SaveN>()`」，再连续点几次保存，观察通知是**替换**而不是堆叠（结合 4.3 的去重机制）。**待本地验证**。

## 6. 本讲小结

- `Sheet` 与 `Notification` 都是**窗口级浮层**，由窗口唯一的 `Root` 统一承载；它们的「画面出口」是 `Root::render_sheet_layer` / `Root::render_notification_layer`，**必须由你的根视图 `render` 手动 `.children(...)` 挂载**，`Root::render` 自己不画它们（与 u5-l1 的 Dialog 同理）。
- 三类浮层管理模型不同：`Dialog` 是**栈**（`Vec`）、`Sheet` 是**单个**（`Option`，新开替换旧的，不能嵌套）、`Notification` 是**常驻实体列表**（`Entity<NotificationList>`，可堆叠、去重、各自倒计时）。
- `Sheet` 是无状态 `RenderOnce`，沿「描述 + 闭包重建」范式：`window.open_sheet(cx, |sheet, _| sheet.title(...).child(...))`；支持四方向（默认 Right）、尺寸、遮罩、0.15s 滑入动画、ESC / 点遮罩 / 关闭按钮三种关闭，`on_close` 仅在「用户主动关闭」时触发。
- `Notification` 是有状态 `Render` 实体 + 常驻 `NotificationList`：`window.push_notification(...)` 推送；四种类型；默认 5 秒自动消失；随机 id 追加、`id::<T>()` / `id1::<T>(key)` 去重替换；`.action(...)` 会自动关闭自动消失。
- `Root` 还会协调两类浮层：`render_notification_layer` 会读取当前 `active_sheet` 的方向与尺寸，给通知加 margin，避免通知被抽屉盖住。

## 7. 下一步学习建议

- **u5-l4 菜单系统**：`ContextMenu` / `DropdownMenu` / `PopupMenu` 同样是浮层，且常和本讲的 Sheet/Notification 配合（例如右键菜单里某项触发一条通知），可以对照它们的浮层管理方式。
- **u5-l5 导航与下拉**：`Sidebar` 是「常驻侧边栏」，和本讲的 `Sheet`（临时滑入）形成「常驻 vs 临时」的对照，理解何时该用哪个。
- **继续阅读源码**：精读 [crates/ui/src/notification.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/notification.rs) 末尾的 `tests` 模块（`close_by_type_removes_id_and_all_id1_of_same_type` 等用例），能帮你彻底搞清 `id` / `id1` / `close_by_type` 的去重与清除语义；再回头看 [crates/story/src/lib.rs:693-695](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/lib.rs#L693-L695) 三个 layer 一起挂载的真实范例。
