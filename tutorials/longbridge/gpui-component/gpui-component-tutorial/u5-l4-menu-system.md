# 菜单系统：ContextMenu / DropdownMenu / PopupMenu

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清菜单模块里「三类菜单 + 一个核心引擎」的分工：右键菜单 `ContextMenu`、下拉菜单 `DropdownMenu`、以及它们共同依赖的弹出菜单引擎 `PopupMenu`。
- 理解 gpui-component 为什么把菜单项的行为**绑定到 GPUI 的 Action 上**，以及这样做带来的「自动显示快捷键」红利。
- 掌握用 `.context_menu(...)` 给任意元素挂右键菜单、用 `.dropdown_menu(...)` 给按钮挂下拉菜单的最小写法。
- 读懂 `PopupMenu` 的键盘导航（上下左右/回车/ESC）与「点外部关闭、关闭后把焦点还给来源」的状态机。
- 理解 `DropdownButton` 与 `DropdownMenu` 的协作关系：`DropdownButton` 本身不实现 `DropdownMenu` trait，而是内部拼了一个箭头按钮去用 `DropdownMenu`。

本讲承接 u5-l2（悬浮层）建立的「非模态浮层」认知——菜单也是一种浮层，但它多了「键盘导航 + Action 派发 + 焦点归还」三件套。

## 2. 前置知识

本讲假设你已经掌握：

- **GPUI 的 Action 体系**：动作（Action）是一种可以被派发、被快捷键绑定、被 `on_action` 监听的类型；通常用 `actions!(namespace, [Foo, Bar])` 宏生成。菜单项正是「点了它就派发某个 Action」。
- **`on_action` / `cx.bind_keys` / `key_context`**：u5-1 的 Dialog 用 `key_context("Dialog")` 让 ESC/Enter 只在弹窗上下文生效，菜单用的是同样的机制，只是 context 名字换成了 `"PopupMenu"`。
- **实体（Entity）与 `Render`**：u4-4 里 `InputState` 是一个有状态实体 `Entity<T>`；本讲的 `PopupMenu` 也是这样的实体，实现 `Render + Focusable + EventEmitter<DismissEvent>`。
- **`dismiss` / `DismissEvent`**：u5-2 的 Popover 通过监听子组件的 `DismissEvent` 来关闭自己。`PopupMenu` 也发 `DismissEvent`，`DropdownMenu` 和 `ContextMenu` 正是靠订阅它来收起菜单。
- **自定义 Element**：u2-4 提到 GPUI 元素可以实现 `Element` trait 自管布局/绘制。`ContextMenu` 就是这样一个**自定义 Element**（不是 `RenderOnce` 组件），这是它与 `DropdownMenu` 最大的实现差异。

## 3. 本讲源码地图

菜单代码全部在 [crates/ui/src/menu/](crates/ui/src/menu) 这一个目录下：

| 文件 | 作用 | 本讲是否精读 |
| --- | --- | --- |
| `mod.rs` | 模块入口：声明子模块、对外 re-export、提供 `init(cx)` 注册键盘绑定 | 是（快速过一遍） |
| `popup_menu.rs` | **核心引擎**：`PopupMenu` 实体与 `PopupMenuItem` 枚举，所有菜单的真正渲染与交互都在这里 | 是（重点） |
| `context_menu.rs` | `ContextMenuExt` 扩展 trait + `ContextMenu` 自定义 Element，负责「右键触发」 | 是 |
| `dropdown_menu.rs` | `DropdownMenu` 扩展 trait + `DropdownMenuPopover` 组件，负责「点击触发」 | 是 |
| `menu_item.rs` | `MenuItemElement`，单行菜单项的视觉实现（pub(crate)，内部用） | 略读 |
| `app_menu_bar.rs` | 顶部原生菜单栏（macOS 系统菜单等），属另一条线，本讲不展开 | 仅提及 |

对外暴露的公共 API（来自 [crates/ui/src/menu/mod.rs:9-12](crates/ui/src/menu/mod.rs#L9-L12)）：

```rust
pub use app_menu_bar::AppMenuBar;
pub use context_menu::{ContextMenu, ContextMenuExt, ContextMenuState};
pub use dropdown_menu::DropdownMenu;
pub use popup_menu::{PopupMenu, PopupMenuItem};
```

## 4. 核心概念与源码讲解

### 4.1 PopupMenu —— 菜单系统的核心引擎

#### 4.1.1 概念说明

虽然本讲标题把三种菜单并列，但它们**不是平级的三兄弟**。真实的关系是：

```
            ┌──────────────────────────┐
            │      PopupMenu           │  ← 唯一的「引擎」
            │  (Entity, 负责渲染+交互)  │
            └──────────────────────────┘
                 ▲            ▲
                 │ 谁来触发它？│
   ┌─────────────┴───┐   ┌────┴──────────────┐
   │  ContextMenu    │   │  DropdownMenu     │
   │  右键 → 弹出    │   │  点击 → 弹出      │
   │  (自定义 Element)│   │  (RenderOnce 组件) │
   └─────────────────┘   └───────────────────┘
```

也就是说，`ContextMenu` 和 `DropdownMenu` 本身**不画菜单**，它们只负责「在合适时机构造一个 `PopupMenu` 实体并把它摆到屏幕上」。真正的菜单画面、键盘导航、点击派发，全部由 `PopupMenu` 统一完成。

`PopupMenu` 是一个有状态实体 `Entity<PopupMenu>`，实现了三个关键 trait：

- `Render`：自己绘制菜单。
- `Focusable`：可以获得焦点，从而接收键盘事件。
- `EventEmitter<DismissEvent>`：关闭时发出 `DismissEvent`，让上层（`ContextMenu`/`DropdownMenu`）据此收起。

#### 4.1.2 核心流程

一个 `PopupMenu` 从构造到关闭的生命周期：

1. **构造**：通过 `PopupMenu::build(window, cx, |menu, ...| { ... })` 工厂创建实体；闭包里用链式方法（`.menu()`/`.separator()`/`.submenu()`...）往里塞菜单项。
2. **聚焦**：菜单弹出后调用 `menu.focus_handle(cx).focus(window, cx)` 拿走焦点，键盘才能命中。
3. **键盘导航**：焦点在菜单上时，按下方向键会被 `init` 注册的快捷键翻译成 `SelectUp/SelectDown/SelectLeft/SelectRight`、回车翻译成 `Confirm`、ESC 翻译成 `Cancel`，由菜单的 `on_action` 处理。
4. **确认**：点中某项 → 设置 `selected_index` → 触发 `confirm` → 派发该项绑定的 Action（或调用 `on_click` 回调）→ 调用 `dismiss` 关闭。
5. **关闭**：`dismiss` 发出 `DismissEvent`，把焦点归还给 `action_context`（通常是打开菜单的那个控件），若有父菜单则递归关闭。

键盘操作一览（来自官方文档）：

| 按键 | 动作 |
| --- | --- |
| ↑ / ↓ | 在菜单项之间移动 |
| ← / → | 进/出子菜单 |
| Enter / Space | 激活当前项 |
| Escape | 关闭菜单 |

#### 4.1.3 源码精读

**(1) 键盘绑定：菜单的「遥控器」是在 `init` 里装好的**

[crates/ui/src/menu/popup_menu.rs:19-28](crates/ui/src/menu/popup_menu.rs#L19-L28) 把方向键和回车/ESC 绑定到 `"PopupMenu"` 这个上下文：

```rust
pub fn init(cx: &mut App) {
    cx.bind_keys([
        KeyBinding::new("enter", Confirm { secondary: false }, Some(CONTEXT)),
        KeyBinding::new("escape", Cancel, Some(CONTEXT)),
        KeyBinding::new("up", SelectUp, Some(CONTEXT)),
        KeyBinding::new("down", SelectDown, Some(CONTEXT)),
        KeyBinding::new("left", SelectLeft, Some(CONTEXT)),
        KeyBinding::new("right", SelectRight, Some(CONTEXT)),
    ]);
}
```

> 这里 `CONTEXT = "PopupMenu"`（[popup_menu.rs:17](crates/ui/src/menu/popup_menu.rs#L17)）。这个 `init` 由 `menu::init`（[mod.rs:14-17](crates/ui/src/menu/mod.rs#L14-L17)）调用，而 `menu::init` 又在顶层 `gpui_component::init(cx)` 里被调用——所以只要你按规矩调了 `init(cx)`，键盘导航就自动可用。

**(2) 菜单项的统一表示：`PopupMenuItem` 枚举**

[crates/ui/src/menu/popup_menu.rs:31-65](crates/ui/src/menu/popup_menu.rs#L31-L65) 用一个枚举把所有种类的菜单项统一表达：

```rust
pub enum PopupMenuItem {
    Separator,              // 分割线
    Label(SharedString),    // 不可点击的小标题
    Item { icon, label, disabled, checked, is_link, action, handler }, // 普通项/链接项
    ElementItem { ... render, ... }, // 自定义任意元素渲染的项
    Submenu { icon, label, disabled, menu: Entity<PopupMenu> },        // 子菜单
}
```

关键设计：每个普通项都可以同时挂一个 `action: Option<Box<dyn Action>>` 和一个 `handler`（`on_click` 闭包）。这对应官方文档里那句提示——**Action 是推荐方式**（能自动显示快捷键），但若你不想定义 Action，也能用 `on_click` 直接写逻辑。

**(3) `PopupMenu` 的关键字段**

[crates/ui/src/menu/popup_menu.rs:273-295](crates/ui/src/menu/popup_menu.rs#L273-L295)：

```rust
pub struct PopupMenu {
    pub(crate) focus_handle: FocusHandle,
    pub(crate) menu_items: Vec<PopupMenuItem>,
    /// The focus handle of Entity to handle actions.
    pub(crate) action_context: Option<FocusHandle>,
    selected_index: Option<usize>,
    // ... min_width / max_width / max_height / scrollable ...
    parent_menu: Option<WeakEntity<Self>>, // 用于子菜单回溯父菜单
    // ...
}
```

- `selected_index`：当前高亮项的下标，键盘导航和鼠标悬停都会改它。
- `action_context`：菜单关闭后，焦点要还给谁、Action 要派发给谁的焦点上下文。
- `parent_menu`：子菜单持有的「弱引用」指向父菜单，让 ESC 关闭时能整条链一起关。

**(4) 工厂方法 `build`**

[crates/ui/src/menu/popup_menu.rs:319-325](crates/ui/src/menu/popup_menu.rs#L319-L325)：

```rust
pub fn build(window, cx, f: impl FnOnce(Self, &mut Window, &mut Context<PopupMenu>) -> Self) -> Entity<Self> {
    cx.new(|cx| f(Self::new(cx), window, cx))
}
```

所有上层（`ContextMenu`/`DropdownMenu`）都通过它拿到一个 `Entity<PopupMenu>`。

**(5) 点击确认：`on_click` → `confirm` → 派发 Action → `dismiss`**

点击某一项时（[popup_menu.rs:745-750](crates/ui/src/menu/popup_menu.rs#L745-L750)）：

```rust
fn on_click(&mut self, ix: usize, window, cx) {
    cx.stop_propagation();
    window.prevent_default();
    self.selected_index = Some(ix);
    self.confirm(&Confirm { secondary: false }, window, cx);
}
```

`confirm`（[popup_menu.rs:752-783](crates/ui/src/menu/popup_menu.rs#L752-L783)）会取出该项的 `handler` 优先调用，否则派发 `action`，最后无论如何都 `dismiss` 关闭菜单。

派发 Action 的细节在 [popup_menu.rs:785-796](crates/ui/src/menu/popup_menu.rs#L785-L796)：先把焦点给 `action_context`，再 `window.dispatch_action(...)`。这就是「菜单项的快捷键提示」与「Action 真正生效」能对上的原因——Action 派发到正确的焦点上下文里。

**(6) 关闭：`dismiss`**

[crates/ui/src/menu/popup_menu.rs:945-966](crates/ui/src/menu/popup_menu.rs#L945-L966)：

```rust
fn dismiss(&mut self, _: &Cancel, window, cx) {
    if self.active_submenu().is_some() { return; } // 有展开的子菜单就不关
    cx.emit(DismissEvent);                          // 通知上层收起
    if let Some(action_context) = self.action_context.as_ref() {
        window.focus(action_context, cx);           // 焦点还给来源
    }
    // 若存在父菜单，递归关闭整条链
    if let Some(parent_menu) = self.parent_menu.clone() { ... }
}
```

**(7) 键盘导航实现**

以「向下」为例（[popup_menu.rs:825-843](crates/ui/src/menu/popup_menu.rs#L825-L843)）：从当前位置往后找下一个**可点击**（`is_clickable`）的项；到末尾就循环回第一项——这正是菜单常见的「循环选择」行为。`is_clickable`（[popup_menu.rs:227-243](crates/ui/src/menu/popup_menu.rs#L227-L243)）会过滤掉分割线、Label 和 `disabled` 项。

**(8) 渲染入口**

[popup_menu.rs:1283-1350](crates/ui/src/menu/popup_menu.rs#L1283-L1350) 是 `render`，几个值得注意的点：

- `.key_context(CONTEXT)`：把自己声明为 `"PopupMenu"` 上下文，(1) 里绑定的快捷键才会在它聚焦时生效。
- `.on_action(...)` 一连串：把导航动作接到对应方法上。
- `.popover_style(cx)`：复用主题里的浮层样式（阴影、圆角、背景）。
- `.occlude()`：让菜单下方的区域不响应鼠标，保证「点菜单空白处不会穿透」。

> ⚠️ 一个重要限制（官方文档与源码注释都强调）：当菜单设置了 `scrollable(true)` 后，**子菜单不可用**。原因是子菜单靠 `overflow` 之外的绝对定位弹出，而可滚动容器会把它裁掉。源码注释见 [popup_menu.rs:1346](crates/ui/src/menu/popup_menu.rs#L1346) 附近的 TODO。

#### 4.1.4 代码实践

**实践目标**：用 `PopupMenu::build` 手动构造一个菜单实体，理解它是「一个能聚焦、能发事件的有状态实体」。

**操作步骤**：

1. 阅读 [crates/story/src/stories/menu_story.rs](crates/story/src/stories/menu_story.rs)，这是官方 Gallery 里完整的菜单示例，定义了 `Copy/Cut/Paste/SearchAll/ToggleCheck` 等 Action。
2. 定位到 [menu_story.rs:137-221](crates/story/src/stories/menu_story.rs#L137-L221) 的 `.dropdown_menu(move |this, window, cx| { ... })` 闭包，观察它如何用链式方法组装菜单：`.link()` → `.separator()` → `.item(PopupMenuItem::new(...).on_click(...))` → `.menu("Copy", Box::new(Copy))` → `.menu_with_check(...)` → `.menu_with_icon(...)` → `.submenu(...)`。
3. 在该文件里搜索 `PopupMenu::build`，对比「手动 build」与「`.dropdown_menu` 里隐式 build」两种用法的差异。

**需要观察的现象**：

- 注意闭包签名 `Fn(PopupMenu, &mut Window, &mut Context<PopupMenu>) -> PopupMenu`——第一个参数 `this` 已经是一个建好的 `PopupMenu` 值，你只需往里加项并返回它。
- 注意 `.submenu("Links", window, cx, |menu, _, _| { ... })`（[menu_story.rs:206](crates/story/src/stories/menu_story.rs#L206)）需要传入 `window` 和 `cx`，因为子菜单也要 `PopupMenu::build`，必须有这两个上下文。

**预期结果**：你能说清「`.dropdown_menu` 的闭包参数 `this` 从哪来」——它就是 `PopupMenu::build` 在内部 `Self::new(cx)` 后传进来的（见 (4)）。运行结果待本地验证（可 `cargo run` 打开 Gallery 的 Menu 页面查看效果）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `PopupMenu` 要实现 `EventEmitter<DismissEvent>`？谁在监听这个事件？

> **答**：为了让 `ContextMenu` 和 `DropdownMenu`（以及任何把 `PopupMenu` 当浮层用的上层）知道「菜单自己想关了」。例如 `ContextMenu` 在右键打开时会 `window.subscribe(&menu, ...)` 监听 `DismissEvent`，一旦收到就把 `open` 置为 `false`（见 4.2.3）。

**练习 2**：菜单项既可以挂 `action` 也可以挂 `on_click`，两者都设了会怎样？

> **答**：看 `confirm`（[popup_menu.rs:757-767](crates/ui/src/menu/popup_menu.rs#L757-L767)）：`handler` 优先，有 `handler` 就只调用 handler、不派发 action；只有没有 handler 时才 fallback 到派发 `action`。所以两者是「二选一」的实际效果。

**练习 3**：方向键导航时，`disabled` 的菜单项会被跳过吗？

> **答**：会。`select_down`/`select_up` 通过 `is_clickable()` 过滤候选项（[popup_menu.rs:227-243](crates/ui/src/menu/popup_menu.rs#L227-L243)），`disabled` 项 `is_clickable` 为 `false`，所以导航会自动跨过它。

---

### 4.2 ContextMenu —— 右键菜单

#### 4.2.1 概念说明

`ContextMenu` 解决的问题是：「我想在某个区域上**右键**时弹出一组操作」。它的使用方式极其轻量，靠一个扩展 trait 挂到任意元素上：

```rust
use gpui_component::menu::ContextMenuExt;

div()
    .id("my-area")
    .child("Right click me")
    .context_menu(|menu, window, cx| {
        menu.menu("Copy", Box::new(Copy))
            .separator()
            .menu("Delete", Box::new(Delete))
    })
```

注意：被挂载的元素**必须有 `.id(...)`**，因为 `ContextMenu` 是自定义 Element，需要稳定的元素 id 来托管跨帧状态。

#### 4.2.2 核心流程

1. `.context_menu(f)` 把原元素包成一个 `ContextMenu<E>` 自定义元素，并把菜单构造闭包 `f` 存起来。
2. 在 GPUI 的 `paint` 阶段，注册一个 `MouseDownEvent` 监听：当**右键**按下且鼠标落在该元素的 hitbox 内时，记录鼠标坐标、置 `open = true`。
3. 用 `window.defer(...)` 在下一帧真正构造 `PopupMenu`（闭包 `f` 此时才执行），并把它定位到鼠标坐标。
4. 订阅该 `PopupMenu` 的 `DismissEvent`，收到就置 `open = false`。
5. 下一帧 `request_layout` 时，若 `open` 为真且菜单非空，就把菜单作为 `deferred(anchored())` 子元素画出来。

#### 4.2.3 源码精读

**(1) 扩展 trait 与 blanket impl：让任何元素都能挂右键菜单**

[crates/ui/src/menu/context_menu.rs:13-37](crates/ui/src/menu/context_menu.rs#L13-L37)：

```rust
pub trait ContextMenuExt: InteractiveElement + ParentElement + Styled {
    fn context_menu(mut self, f: impl Fn(...) -> PopupMenu + 'static) -> ContextMenu<Self>
    where Self: Sized
    {
        // 用元素的 id 生成唯一 key，保证每个右键菜单有独立状态
        let id = self.interactivity().element_id.clone()
            .map(|id| format!("context-menu-{:?}", id))
            .unwrap_or_else(|| format!("context-menu-{:p}", &self as *const _));
        ContextMenu::new(id, self).menu(f)
    }
}
impl<E: InteractiveElement + ParentElement + Styled> ContextMenuExt for E {}
```

这与 u2-4 学过的「扩展 trait + blanket impl」手法完全一致：不动 GPUI 源码，让所有满足约束的 `div()` 都凭空多出 `.context_menu()` 方法。

**(2) `ContextMenu` 是自定义 Element，不是 `RenderOnce`**

这是它与 `DropdownMenu` 最大区别。看 [context_menu.rs:141](crates/ui/src/menu/context_menu.rs#L141)：

```rust
impl<E: ParentElement + Styled + IntoElement + 'static> Element for ContextMenu<E> {
    type RequestLayoutState = ContextMenuState;
    type PrepaintState = Hitbox;
    fn request_layout(...) { ... }
    fn prepaint(...) { ... }
    fn paint(...) { ... }   // 关键：监听右键在这里
}
```

为什么用自定义 Element 而不是组件？因为它需要在 `paint` 阶段拿到一个属于自己的 `Hitbox`，再用 `hitbox.is_hovered(window)` 判断「右键是不是落在我的地盘上」。这种「命中测试」需求用组件层很难干净地表达。

**(3) 跨帧状态：`ContextMenuState` + `Rc<RefCell<...>>`**

因为自定义 Element 的 `request_layout`/`paint` 每帧都会重新调用、且没有 `&mut self` 那种持久状态，`ContextMenu` 把「是否打开、菜单位置、菜单实体」放进 element state（[context_menu.rs:122-139](crates/ui/src/menu/context_menu.rs#L122-L139)）：

```rust
struct ContextMenuSharedState {
    menu_view: Option<Entity<PopupMenu>>,
    open: bool,
    position: Point<Pixels>,
    _subscription: Option<Subscription>,
}
pub struct ContextMenuState {
    element: Option<AnyElement>,
    shared_state: Rc<RefCell<ContextMenuSharedState>>,
}
```

用 `Rc<RefCell<...>>` 是因为这些状态要在事件闭包里被读写，而闭包只能捕获 `shared_state` 的克隆。

**(4) 右键检测与延迟构造**

`paint` 里（[context_menu.rs:275-288](crates/ui/src/menu/context_menu.rs#L275-L288)）注册鼠标事件：

```rust
window.on_mouse_event(move |event: &MouseDownEvent, phase, window, cx| {
    if phase.bubble() && event.button == MouseButton::Right && hitbox.is_hovered(window) {
        // 记录位置、打开
        shared_state.menu_view = None;
        shared_state.position = event.position;
        shared_state.open = true;
        // 下一帧再真正 build 菜单，避免竞态
        window.defer(cx, { ... PopupMenu::build(...) ... });
    }
});
```

随后订阅 `DismissEvent`（[context_menu.rs:303-309](crates/ui/src/menu/context_menu.rs#L303-L309)）把 `open` 复位为 `false`。

**(5) 弹出层的绘制**

`request_layout` 在 `open` 为真时，把菜单塞进 `deferred(anchored().child(...))`（[context_menu.rs:180-210](crates/ui/src/menu/context_menu.rs#L180-L210)）：外层一个铺满窗口的透明 div 用来吞掉滚动事件，内层 `anchored().position(position)` 把菜单钉在右键坐标，并 `snap_to_window_with_margin(px(8.))` 防止菜单贴边或溢出窗口。

#### 4.2.4 代码实践

**实践目标**：为一个列表项区域添加右键菜单，包含「复制 / 删除」。

**操作步骤**（基于 Gallery 已有的模式，参照 [menu_story.rs:241-271](crates/story/src/stories/menu_story.rs#L241-L271)）：

1. 在你的 view 里用 `actions!` 定义两个动作：`actions!(my_view, [Copy, Delete]);`
2. 给 view 加一个 `on_action` 处理：`fn on_copy(&mut self, _: &Copy, ...)` 和 `fn on_delete(&mut self, _: &Delete, ...)`。
3. 渲染一个列表项容器，挂上右键菜单：

   ```rust
   // 示例代码（仿 menu_story，非项目原文件）
   div()
       .id("list-item-1")
       .child("Project Alpha")
       .context_menu(move |menu, _, _| {
           menu.menu("复制", Box::new(Copy))
              .separator()
              .menu("删除", Box::new(Delete))
       })
   ```

4. 在 view 根 `div` 上 `.on_action(cx.listener(Self::on_copy))` 把动作接到处理函数。

**需要观察的现象**：

- 在该区域上右键应弹出菜单，菜单**出现在鼠标光标处**（因为 `position = event.position`）。
- 点击「删除」后，菜单自动消失（`confirm` 调用了 `dismiss`），且你的 `on_delete` 被触发。
- 在区域**外**右键或点击，菜单也会关闭（外层透明 div 与 `on_mouse_down_out` 共同作用）。

**预期结果**：右键弹菜单、点选项触发 Action 并自动收起。运行结果待本地验证（`cargo run` 打开 Gallery → Menu → 「Context Menu」区可看到同样效果）。

#### 4.2.5 小练习与答案

**练习 1**：为什么被挂 `.context_menu()` 的元素必须先 `.id(...)`？

> **答**：`ContextMenu` 是自定义 Element，需要一个稳定 `ElementId` 来通过 `window.with_optional_element_state` 托管 `ContextMenuState`（[context_menu.rs:78](crates/ui/src/menu/context_menu.rs#L78)）。没有 id，状态就无法跨帧存取，菜单的 open/position 会丢。

**练习 2**：源码里构造菜单用了 `window.defer(cx, ...)` 放到「下一帧」执行，为什么不直接当场 build？

> **答**：注释写明是「avoiding race conditions」（[context_menu.rs:290](crates/ui/src/menu/context_menu.rs#L290)）。在鼠标事件回调里当场修改 element state 并重建菜单，可能与当前帧的状态读写产生借用/时序冲突；defer 到下一帧让状态更新先落地，再安全地 build。

---

### 4.3 DropdownMenu —— 下拉菜单（及 DropdownButton 协作）

#### 4.3.1 概念说明

`DropdownMenu` 解决的问题是：「点击一个**触发器**（通常是按钮）时弹出菜单」。它与 `ContextMenu` 的区别是触发方式（点击 vs 右键）和实现方式（`RenderOnce` 组件 vs 自定义 Element）。

最典型的写法是把 `DropdownMenu` trait 用在 `Button` 上：

```rust
use gpui_component::{button::Button, menu::DropdownMenu as _};

Button::new("menu-btn")
    .label("Edit")
    .dropdown_menu(|menu, window, cx| {
        menu.menu("Copy", Box::new(Copy))
           .menu("Paste", Box::new(Paste))
    })
```

注意导入时写 `DropdownMenu as _`——因为我们要用它的方法但不需要这个 trait 名本身。

#### 4.3.2 核心流程

1. `.dropdown_menu(f)` 把按钮包成 `DropdownMenuPopover<Button>` 组件，存下触发器样式、锚点角和构造闭包 `f`。
2. 该组件复用 u5-2 的 `Popover` 作为浮层容器（`appearance(false)` 不要边框、`overlay_closable(false)` 不要点遮罩关闭）。
3. 用 `window.use_keyed_state(...)` 缓存构造好的 `PopupMenu` 实体——**只 build 一次**，后续每次 render 直接复用。
4. 订阅 `PopupMenu` 的 `DismissEvent`：收到就 dismiss 掉 Popover、并把缓存的菜单清空（以便下次重新动态构建）。
5. Popover 负责定位（按 `anchor` 锚点角）、点外部关闭等通用行为，菜单本身只管内容。

#### 4.3.3 源码精读

**(1) `DropdownMenu` trait：只对 `Selectable` 的交互元素开放**

[crates/ui/src/menu/dropdown_menu.rs:11-33](crates/ui/src/menu/dropdown_menu.rs#L11-L33)：

```rust
pub trait DropdownMenu: Styled + Selectable + InteractiveElement + IntoElement + 'static {
    fn dropdown_menu(self, f) -> DropdownMenuPopover<Self> {
        self.dropdown_menu_with_anchor(Anchor::TopLeft, f)
    }
    fn dropdown_menu_with_anchor(mut self, anchor, f) -> DropdownMenuPopover<Self> {
        let style = self.style().clone();
        let id = self.interactivity().element_id.clone();
        DropdownMenuPopover::new(id.unwrap_or(0.into()), anchor, self, f).trigger_style(style)
    }
}
impl DropdownMenu for Button {}
```

注意两点：

- 只给 `Button` 实现了 blanket 之外的 `impl DropdownMenu for Button {}`——所以 `.dropdown_menu()` 是按钮专属能力。
- trait 约束里有 `Selectable`，意味着触发器要能表达「选中态」（菜单打开时常高亮）。

**(2) `DropdownMenuPopover`：复用 Popover + 缓存菜单**

这是本模块最精巧的一段。看 [dropdown_menu.rs:81-138](crates/ui/src/menu/dropdown_menu.rs#L81-L138) 的 `render`：

```rust
fn render(self, window, cx) -> impl IntoElement {
    let menu_state = window.use_keyed_state(self.id.clone(), cx, |_, _| DropdownMenuState::default());

    Popover::new(...)
        .appearance(false)        // 不要 Popover 的默认边框
        .overlay_closable(false)  // 点遮罩不关（改由菜单自身的 dismiss 机制关）
        .trigger(self.trigger)
        .anchor(self.anchor)
        .content(move |_, window, cx| {
            // 只在第一次构建，之后复用同一个 Entity<PopupMenu>
            let menu = match menu_state.read(cx).menu.clone() {
                Some(menu) => menu,
                None => {
                    let menu = PopupMenu::build(window, cx, |m, w, c| builder(m, w, c));
                    menu_state.update(cx, |s, _| s.menu = Some(menu.clone()));
                    menu.focus_handle(cx).focus(window, cx);   // 打开即聚焦
                    // 监听菜单 dismiss，连带关闭 Popover 并清缓存
                    window.subscribe(&menu, cx, move |_, _: &DismissEvent, window, cx| {
                        popover_state.update(cx, |s, cx| s.dismiss(window, cx));
                        menu_state.update(cx, |s, _| s.menu = None);
                    }).detach();
                    menu.clone()
                }
            };
            menu.clone()
        })
}
```

注释（[dropdown_menu.rs:97-102](crates/ui/src/menu/dropdown_menu.rs#L97-L102)）解释了为什么要缓存：`content` 闭包**每次 render 都会被调用**，若每次都 `PopupMenu::build`，菜单的选中态、滚动位置都会重置。所以用 `use_keyed_state` 把 `Entity<PopupMenu>` 存下来只建一次；又在 dismiss 时清空，是为了「下次打开时按最新数据重新构建」（支持动态菜单项）。

**(3) `DropdownButton` 是如何与 `DropdownMenu` 协作的**

学习目标里提到「`DropdownMenu` 与 `DropdownButton` 配合」，但要注意：`DropdownButton` **本身并不实现 `DropdownMenu` trait**。它的做法是内部拼装一个箭头按钮，再对**那个箭头按钮**调用 `DropdownMenu`。看 [crates/ui/src/button/dropdown_button.rs:188-212](crates/ui/src/button/dropdown_button.rs#L188-L212)：

```rust
.when_some(self.menu, |this, menu| {
    this.child(
        Button::new("popup")
            .icon(IconName::ChevronDown)        // 右侧的箭头
            .border_edges(...)                  // 互抵圆角，和主按钮拼成一体
            .border_corners(...)
            .dropdown_menu_with_anchor(self.anchor, menu),  // ← 在这里用 DropdownMenu
    )
})
```

也就是说：`DropdownButton` = 「主按钮 + 箭头按钮」两段拼装（u3-1 讲过它互抵圆角合成连体外观），箭头按钮挂菜单。调用方只需：

```rust
// 示例代码
DropdownButton::new("ops")
    .button(Button::new("save").label("Save"))
    .dropdown_menu(|menu, _, _| menu.menu("Export", Box::new(Export)).menu("Print", Box::new(Print)))
```

主按钮负责常规 `on_click`，箭头负责展开菜单——两者各司其职，这是「DropdownMenu 与 DropdownButton 配合」的真实含义。

#### 4.3.4 代码实践

**实践目标**：实现一个 `DropdownButton`，主按钮点击提交、箭头展开一个「导出/打印」操作下拉菜单。

**操作步骤**（参照 Gallery [dropdown_button_story.rs](crates/story/src/stories/dropdown_button_story.rs) 与 [menu_story.rs:134-222](crates/story/src/stories/menu_story.rs#L134-L222)）：

1. 定义动作：`actions!(my_view, [Export, Print]);`
2. 渲染：

   ```rust
   // 示例代码
   DropdownButton::new("save-ops")
       .button(
           Button::new("save").label("保存").on_click(cx.listener(|this, _, _, cx| {
               this.message = "已保存".into(); cx.notify();
           }))
       )
       .dropdown_menu_with_anchor(Anchor::BottomLeft, move |menu, _, _| {
           menu.menu("导出", Box::new(Export))
              .separator()
              .menu("打印", Box::new(Print))
       })
   ```

3. 在 view 根上接 `on_action(cx.listener(Self::on_export))` 等。

**需要观察的现象**：

- 点击**主按钮**触发 `on_click`（保存），不弹菜单。
- 点击**右侧箭头**弹出菜单，菜单按 `BottomLeft` 锚点出现在按钮下方。
- 点「导出」后菜单关闭并触发 `Export` 动作；验证「关一次再开」时菜单仍能正常重建（因为 dismiss 时清了缓存）。

**预期结果**：主按钮与箭头各自独立响应。运行结果待本地验证（`cargo run` 打开 Gallery，分别看 DropdownButton 与 Menu 两个页面）。

#### 4.3.5 小练习与答案

**练习 1**：`DropdownMenuPopover` 为什么要用 `use_keyed_state` 缓存 `PopupMenu`，而不是每次 `content` 闭包里都新建？

> **答**：因为 `content` 闭包每次 render 都会调用（[dropdown_menu.rs:97-99](crates/ui/src/menu/dropdown_menu.rs#L97-L99) 注释）。若每次新建 `Entity<PopupMenu>`，菜单的高亮项、滚动位置等状态会被重置，体验异常。缓存后只在首次构建，保证状态连续。

**练习 2**：`DropdownButton` 自己能直接调用 `.dropdown_menu()` 吗？为什么？

> **答**：不能直接在 `DropdownButton` 上调 `.dropdown_menu()`（它没实现 `DropdownMenu` trait）。它是内部 new 了一个 `Button::new("popup").icon(ChevronDown)`，再对那个按钮调 `.dropdown_menu_with_anchor(...)`（[dropdown_button.rs:190-211](crates/ui/src/button/dropdown_button.rs#L190-L211)）。所以 `DropdownButton` 是「组装者」，真正用 trait 的是它内部的箭头按钮。

**练习 3**：`DropdownMenuPopover` 的 `Popover` 为什么设 `.appearance(false)` 和 `.overlay_closable(false)`？

> **答**：菜单自己已经有完整的边框/阴影样式（来自 `PopupMenu` 的 `popover_style`），不需要 Popover 再画一层外观，所以 `appearance(false)`；菜单的关闭统一走 `PopupMenu` 的 `DismissEvent` 机制（点菜单项、ESC、点外部都会触发），不需要 Popover 的「点遮罩关闭」，所以 `overlay_closable(false)`，避免两套关闭逻辑打架。

## 5. 综合实践

把本讲三个模块串起来，做一个「**带右键菜单与操作下拉的简易文件列表**」。要求：

1. 用一个 view 持有 `Vec<String>` 文件名列表，渲染成一个竖向列表。
2. 每个**列表项**挂 `ContextMenu`，右键可「复制 / 重命名 / 删除」；删除时从列表移除该项（用带下标的 Action，仿照 [menu_story.rs:16-18](crates/story/src/stories/menu_story.rs#L16-L18) 的 `Info(usize)` 把下标塞进 Action）。
3. 列表上方放一个 `DropdownButton`（或 `Button` + `.dropdown_menu()`），点击箭头弹出「全部复制 / 清空列表」操作下拉。
4. 给「复制」「删除」绑定快捷键（如 `ctrl-c` / `Delete`），观察菜单项右侧**是否自动显示快捷键提示**（这要求给菜单设 `action_context`，并让 view 根 `div` 处于该焦点上下文里——参考 [menu_story.rs:22-43](crates/story/src/stories/menu_story.rs#L22-L43) 的 `bind_keys`）。
5. 验证键盘操作：右键打开菜单后，用 ↑↓ 移动、Enter 确认、ESC 关闭。

**提示与预期**：

- 列表项需要各自的稳定 `id`（如 `format!("item-{i}")`），否则 `ContextMenu` 状态会互相串。
- 删除某项后，列表重渲染、下标变化——思考「带下标的 Action」在重渲染后是否还指向正确的项（这是一个值得记录的设计权衡点）。
- 若你给「全部复制」用了一组超过 20 项的子菜单，可对照 [popup_menu.rs:714-716](crates/ui/src/menu/popup_menu.rs#L714-L716) 体会「>20 项自动开启 scrollable」的行为。
- 运行结果待本地验证；建议先在 Gallery 的 Menu / DropdownButton 页面确认 API 行为，再迁移到自己的列表里。

## 6. 本讲小结

- **一个引擎，两个触发器**：`PopupMenu` 是唯一的菜单引擎（有状态 `Entity`，负责渲染/导航/派发），`ContextMenu`（右键）和 `DropdownMenu`（点击）只是不同的触发外壳。
- **菜单项绑 Action**：推荐用 `Box<dyn Action>` 定义菜单项行为，好处是能复用 GPUI 的快捷键系统、自动显示快捷键提示；不想定义 Action 时可用 `PopupMenuItem::new(...).on_click(...)` 回调方式。
- **`ContextMenu` 是自定义 Element**：靠 hitbox 命中测试捕获右键，状态存在 `ContextMenuState`（`Rc<RefCell<...>>`）里跨帧保持，被挂元素必须有 `.id()`。
- **`DropdownMenu` 复用 Popover 并缓存菜单**：用 `use_keyed_state` 只 build 一次 `PopupMenu`，靠监听 `DismissEvent` 关闭并清缓存以支持动态重建。
- **`DropdownButton` 是组装者**：它内部 new 一个箭头按钮、再对箭头按钮调 `DropdownMenu`，主按钮与箭头各司其职；它本身不实现 `DropdownMenu` trait。
- **键盘导航与焦点归还**：导航动作（上下左右/回车/ESC）在 `init` 里绑到 `"PopupMenu"` 上下文；`dismiss` 会发 `DismissEvent` 并把焦点还给 `action_context`。
- **限制**：开启 `scrollable(true)` 后子菜单不可用。

## 7. 下一步学习建议

- **往回巩固**：若你对本讲里的「Action 派发 / `key_context` / `DismissEvent`」仍觉抽象，建议重读 u5-1（Dialog 的 `key_context("Dialog")`）与 u5-2（Popover 的定位与 dismiss）。
- **横向扩展**：菜单的另一条线是 `AppMenuBar`（[crates/ui/src/menu/app_menu_bar.rs](crates/ui/src/menu/app_menu_bar.rs)），它把菜单接到了 macOS 等平台的**原生系统菜单栏**，以及 `crates/ui/src/native_menu/`。当你需要做「文件/编辑/视图」这种应用级顶栏时去读它。
- **实战串联**：下一单元 u6 进入 Dock 布局系统，Dock 的 Tab 面板右键、面板操作菜单都用到了本讲的 `PopupMenu`/`ContextMenu`——学完 Dock 后回头你会对菜单的「触发外壳 + 引擎」分层有更深的体会。
- **贡献向**：若想给菜单加新行为，先读 [COMPONENT_TEST_RULES.md](.claude/COMPONENT_TEST_RULES.md) 与 `menu_story.rs` 的写法，注意菜单涉及焦点与键盘，测试时要覆盖导航与 dismiss 路径。
