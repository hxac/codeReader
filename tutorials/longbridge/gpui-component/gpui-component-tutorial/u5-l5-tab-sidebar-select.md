# 导航与下拉：Tab / Sidebar / Breadcrumb / Select / Combobox

## 1. 本讲目标

本讲是「浮层与导航组件」单元的收尾篇，集中讲解五类用于**组织内容、引导用户**的组件。学完后你应当能够：

1. 用 `TabBar` + `Tab` 搭建多面板切换，并说出五种 `TabVariant`（Tab/Outline/Pill/Segmented/Underline）的区别与滑动指示器动画原理。
2. 用 `Sidebar` + `SidebarGroup`/`SidebarMenu`/`SidebarMenuItem` 组织侧边导航，区分三种折叠模式（Icon/Offcanvas/None）。
3. 用 `Breadcrumb` 画面包屑，理解它的「无状态 + 最后一项高亮 + 自动插入分隔符」机制。
4. 用 `Select`（单选下拉）与 `Combobox`（单/多选 + 搜索 + 自定义触发器）做带搜索过滤的下拉选择，理解它们共享的 `SearchableListDelegate` 数据代理模型。

## 2. 前置知识

在继续之前，请确保你已经掌握以下概念（本讲会直接复用，不再重复解释）：

- **无状态组件 + 状态外置**（u3/u4 的反复范式）：组件本身是 `RenderOnce`/`IntoElement`，真正的选中值、开关等状态存在外层 `View`（实现 `Render` 的实体）里，通过 builder 方法下发、通过回调上报，回调里必须 `cx.notify()` 触发重绘。本讲的 `TabBar`、`Sidebar`、`Breadcrumb` 都遵循这一范式。
- **Popover 的延迟锚定定位**（u5-l2）：`Select`/`Combobox` 的下拉层与 `Popover` 一样，用 `deferred(anchored())` 延迟到普通图层之上绘制，并把触发器矩形记录下来做对齐。
- **Button 家族**（u3-l1）：`SidebarToggleButton`、`TabBar` 的 `menu` 按钮、`SidebarMenuItem` 的折叠箭头，底层都是一个 `Button`。
- **`Sizable` / `Styled` / `ActiveTheme`**（u2-l1、u2-l2）：四档尺寸、链式样式、`cx.theme()` 取主题色。
- **`gpui_component::init(cx)`**（u1-l4）：本讲的 `Select`/`Combobox` 依赖键盘快捷键绑定，它们的初始化正是在 `init` 里完成的，不调用 `init` 则方向键/回车/Esc 全部失效。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/ui/src/tab/mod.rs` | Tab 模块入口，再导出 `Tab`/`TabBar`/`TabVariant`。 |
| `crates/ui/src/tab/tab.rs` | 单个 `Tab` 元素与 `TabVariant`（含五种变体的样式表）。 |
| `crates/ui/src/tab/tab_bar.rs` | `TabBar` 容器：负责排列 Tab、滑动指示器动画、溢出菜单。 |
| `crates/ui/src/sidebar/mod.rs` | `Sidebar` 容器、`SidebarCollapsible` 折叠模式、`SidebarToggleButton`、`SidebarItem` trait。 |
| `crates/ui/src/sidebar/menu.rs` | `SidebarMenu` 与 `SidebarMenuItem`（含子菜单、右键菜单）。 |
| `crates/ui/src/sidebar/group.rs` | `SidebarGroup` 分组容器。 |
| `crates/ui/src/breadcrumb.rs` | `Breadcrumb` 与 `BreadcrumbItem`。 |
| `crates/ui/src/select.rs` | `Select`/`SelectState`（单选下拉）。 |
| `crates/ui/src/combobox.rs` | `Combobox`/`ComboboxState`（单/多选 + 搜索 + 自定义触发器）。 |
| `crates/ui/src/searchable_list/vec.rs` | `SearchableVec`/`SearchableGroup`——Select 与 Combobox 开箱即用的数据代理。 |
| `crates/story/src/stories/{tabs,sidebar,breadcrumb,select,combobox}_story.rs` | 五个组件在 Story Gallery 中的真实用法示范。 |

## 4. 核心概念与源码讲解

本讲按「导航三件套（Tab/Sidebar/Breadcrumb）→ 下拉二选（Select/Combobox）」的顺序拆为五个最小模块。

### 4.1 Tab 与 TabBar：多面板切换

#### 4.1.1 概念说明

`Tab` 是单个标签页，`TabBar` 是装着若干 `Tab` 的横向容器。它们解决的问题是：**在有限的同一块屏幕区域里，用顶部一排标签把多块内容“叠放”起来，点击切换显示哪一块。**

注意它和后面 Dock 里的 `TabPanel`（u6）不是一回事——本讲的 `Tab`/`TabBar` 是纯展示用的「无状态标签条」，它本身不托管面板内容，只负责“告诉外层当前选中第几个”。把第几号面板渲染到下方，是外层 View 的职责。这是一种典型的**关注点分离**：标签条管切换语义，View 管内容。

#### 4.1.2 核心流程

`TabBar` 的使用闭环是：

1. 外层 View 持有 `active_tab_ix: usize`。
2. `TabBar::new("id").selected_index(self.active_tab_ix).on_click(...)` 把当前选中号下发。
3. 用户点击某个 Tab → `TabBar` 回调上报被点击的索引 `&usize`。
4. View 在回调里更新 `active_tab_ix` 并 `cx.notify()`，下一帧新的 `selected_index` 生效。
5. View 根据 `active_tab_ix` 渲染对应面板。

`Tab` 的外观由两套**正交开关**组合决定：

- **变体 `TabVariant`**：Tab（默认卡片）、Outline（描边）、Pill（药丸）、Segmented（分段控件）、Underline（下划线）。
- **状态**：normal / hovered / selected / disabled。

每个变体针对每种状态都返回一个 `TabStyle { fg, bg, borders, border_color, inner_bg, shadow }`，把「这个变体在这态下长什么样」完全数据化。

对于 Pill/Segmented/Underline 这三种变体，选中态不是画在单个 Tab 上，而是用一个**滑动指示器（indicator）**——一块会从上一个选中位置滑到新位置的色块，配合文字颜色的渐变，做出顺滑的切换动画。

#### 4.1.3 源码精读

`TabVariant` 枚举定义了五种风格，是整个外观系统的总开关：

[crates/ui/src/tab/tab.rs:13-21](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tab/tab.rs#L13-L21) —— `TabVariant` 五个变体（Tab/Outline/Pill/Segmented/Underline）。

每个变体通过一组方法返回各状态样式，例如 `selected(cx)` 给出「选中态」的颜色与边框：

[crates/ui/src/tab/tab.rs:221-264](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tab/tab.rs#L221-L264) —— `TabVariant::selected`：Pill 用 `primary` 实色背景、Underline 用底部 2px 主色边框、Segmented 用带阴影的 `background` 内层色块、Outline 用主色描边。

`Tab` 本身是一个无状态的 `IntoElement`（注意它派生了 `IntoElement` 而非 `Render`），实现 `Selectable`、`Sizable`、`Styled`、`InteractiveElement` 等能力 trait：

[crates/ui/src/tab/tab.rs:393-414](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tab/tab.rs#L393-L414) —— `Tab` 结构体字段：`ix`（在 TabBar 中的下标）、`label`/`icon`、`variant`、`selected`、以及一组 `indicator_*` 字段（由 TabBar 注入，用于和滑动指示器同步）。

`TabBar` 的关键状态全部外置——它只持有 `selected_index: Option<usize>` 和 `on_click`，不自己记“当前选中谁”：

[crates/ui/src/tab/tab_bar.rs:144-164](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tab/tab_bar.rs#L144-L164) —— `selected_index` 下发当前选中号、`on_click` 回调上报被点击的下标（注释明确说明：设了它之后子 Tab 自身的 `on_click` 会被忽略）。

滑动指示器的动画是 `TabBar` 最精巧的部分。它先用 `on_prepaint`（u2-l4 讲过的隐形探针）测量容器和每个 Tab 的像素矩形，存进 `Rc<RefCell<TabIndicatorBounds>>`（用 `RefCell` 是因为 prepaint 回调里写值不该触发重渲染），再据此算出“从哪滑到哪”：

[crates/ui/src/tab/tab_bar.rs:172-257](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tab/tab_bar.rs#L172-L257) —— `render_indicator`：用 `use_keyed_state` 跨帧保存 `(from_left, from_width, to_left, to_width, epoch)`，再用 `with_animation` 配 `ease_in_out_cubic` 让色块平滑滑过。其中 `epoch` 是关键——每次切换自增，既作为动画 id，又让选中 Tab 的文字颜色淡入“重启”并与色块同步。

色块位置的插值就是线性插值（lerp）：

\[
\text{left}(\delta) = \text{from\_left} + (\text{to\_left} - \text{from\_left})\cdot \delta,\quad \delta \in [0,1]
\]

最后看 `TabBar::render` 如何把这一切拼起来：它给每个 Tab 包一层 `on_prepaint` 把矩形写进 `bounds_rc`，把 indicator 作为第一个子元素插在最前面（保证被 Tab 文字盖在上面），并在溢出时挂一个“更多”下拉菜单按钮：

[crates/ui/src/tab/tab_bar.rs:340-534](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tab/tab_bar.rs#L340-L534) —— `RenderOnce for TabBar`：变体决定背景/间距/padding；为每个子 Tab 注入 `ix`、`variant`、`size`、`selected`、`indicator_*`；`menu(true)` 时构造一个用 `DropdownMenu` 实现的“更多”按钮，列出放不下的标签。

#### 4.1.4 代码实践

**实践目标**：实现一个三标签面板切换，体会“状态外置 + 回调上报”闭环。

**操作步骤**（参考 `crates/story/src/stories/tabs_story.rs`）：

1. 在你的 View 里加一个字段 `active_tab_ix: usize`，初值 `0`。
2. 写一个更新方法：

```rust
// 示例代码
fn set_active_tab(&mut self, ix: usize, _: &mut Window, cx: &mut Context<Self>) {
    self.active_tab_ix = ix;
    cx.notify();
}
```

3. 在 `render` 里放一个 `TabBar`，并按 `active_tab_ix` 渲染对应面板：

```rust
// 示例代码
v_flex()
    .child(
        TabBar::new("main-tabs")
            .selected_index(self.active_tab_ix)
            .on_click(cx.listener(|this, ix: &usize, w, cx| this.set_active_tab(*ix, w, cx)))
            .child("概览")
            .child("成员")
            .child("设置"),
    )
    .child(match self.active_tab_ix {
        0 => div().child("概览内容"),
        1 => div().child("成员内容"),
        _ => div().child("设置内容"),
    })
```

**需要观察的现象**：把 `TabBar` 的变体分别换成 `.pill()`、`.segmented()`、`.underline()`、`.outline()`，观察选中态的滑动/高亮差异；尤其注意 Pill/Segmented 切换时色块是否平滑滑动、文字颜色是否同步淡入。

**预期结果**：五种变体各有一个明显的选中样式；切换瞬间无闪烁（因为 `selected_index` 由 View 下发，每帧重算）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TabBar` 要用 `Rc<RefCell<TabIndicatorBounds>>` 而不是普通的 `Entity` 来存各 Tab 的像素矩形？

**参考答案**：因为写入发生在 `on_prepaint` 回调里，那是绘制阶段而非事件阶段。如果用 `Entity` 的 `update` 写入会触发 `cx.notify()` 引起重渲染，形成绘制→重渲染的循环；而 `RefCell` 只是普通可变借用，不触发任何 GPUI 的状态变更。

**练习 2**：`indicator_epoch` 自增的作用是什么？如果去掉它会怎样？

**参考答案**：`epoch` 同时充当动画的 `ElementId` 后缀和「这次是一次新切换」的标记。GPUI 的 `with_animation` 用 `ElementId` 区分不同动画实例；如果 id 不变，连续两次切换可能被当成“同一个动画”而不重启。用自增 epoch 保证每次切换都启动一段全新的滑动与文字淡入。

**练习 3**：`Tab` 为什么实现 `Selectable` trait？

**参考答案**：`Selectable::selected(bool)` 是 gpui-component 里通用的「选中态」入口（u3-l1 按钮家族、u4 的选择控件都实现它）。`TabBar` 在渲染时用 `.selected(selected_ix == ix)` 给每个 Tab 打上选中标记，这样既可以在 TabBar 层统一控制，也允许把 `Tab` 单独拿出来用。

---

### 4.2 Sidebar：侧边导航与分组

#### 4.2.1 概念说明

`Sidebar` 是侧边栏导航容器，仿照 shadcn/ui 的 Sidebar 设计，负责把 Logo（header）、可滚动的分组菜单（content）、底部区域（footer）组织起来，并提供**三种折叠方式**。它的子元素必须是实现了 `SidebarItem` trait 的类型——这个 trait 要求同时实现 `Collapsible`（u3-l4 讲过的折叠能力），因为侧边栏整体折叠时，内部每一项都要知道“我现在是不是折叠态”来调整自己的渲染（例如只显示图标）。

#### 4.2.2 核心流程

典型结构是三层嵌套：

```
Sidebar
 └─ SidebarGroup("平台")        // 带标题的分组
     └─ SidebarMenu             // 一个菜单容器
         └─ SidebarMenuItem     // 叶子菜单项（可带子菜单）
              └─ SidebarMenuItem ...  // 子菜单
```

三种折叠模式 `SidebarCollapsible` 决定折叠后长什么样：

- **Icon**（默认）：收窄成一条 48px 的图标栏（`COLLAPSED_WIDTH`），仍占布局。
- **Offcanvas**：完全滑出布局，宽度动画到 0，折叠后不占位。
- **None**：忽略 `collapsed`，永远展开。

折叠/展开过程有 200ms 的宽度过渡动画。由于 GPUI 每个动画帧都会重建整棵元素树，动画状态必须存在 `use_keyed_state` 里（u3-3 讲过的模式）才能跨帧保持，否则动画会被不断重置。

`SidebarMenuItem` 的“子菜单展开”也是同样的 `use_keyed_state` 状态外置思路：它本身不记展开与否，而是用元素的 id 作 key 申请一个 `bool` 状态，点击折叠箭头时翻转它。

#### 4.2.3 源码精读

折叠模式枚举与默认宽度常量：

[crates/ui/src/sidebar/mod.rs:27-46](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sidebar/mod.rs#L27-L46) —— `DEFAULT_WIDTH`(255px)、`COLLAPSED_WIDTH`(48px)，以及 `SidebarCollapsible` 三种模式。注意 `From<bool>` 让旧的 `.collapsible(true/false)` 用法保持向后兼容。

`SidebarItem` trait 定义了“可放进 Sidebar 的元素”需要满足什么——必须可克隆且可折叠：

[crates/ui/src/sidebar/mod.rs:211-218](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sidebar/mod.rs#L211-L218) —— `SidebarItem: Collapsible + Clone`，要求实现 `render(self, id, window, cx)`。

`Sidebar` 是泛型容器 `Sidebar<E: SidebarItem>`，提供 header/footer/child/side/collapsible/collapsed 等 builder：

[crates/ui/src/sidebar/mod.rs:236-298](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sidebar/mod.rs#L236-L298) —— builder 方法：`.side(Side::Left/Right)`、`.collapsible(...)`、`.collapsed(bool)`、`.header(...)`、`.footer(...)`、`.child(E)`。

`Sidebar::render` 是核心：它把 content 放进一个 GPUI `list`（虚拟化滚动，复用 u7 的 `ListState`），用 `overdraw` 按 30% 视口高度预渲染；折叠态时给每个子项调用 `.collapsed(layout.icon_collapsed)` 让它们切换成图标渲染；展开/折叠的宽度动画用 `Transition::new(...).ease(ease_in_out_cubic).width(from, to)`：

[crates/ui/src/sidebar/mod.rs:379-546](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sidebar/mod.rs#L379-L546) —— 整段 `render`：虚拟化 list + 宽度动画 + `SidebarAnimationState`（含 `hide_request` 版本号，避免展开/折叠来回切换时的竞态）。

`SidebarToggleButton` 是一个封装好的 ghost `Button`，根据 `collapsed` 和 `side` 自动选择 `PanelLeftOpen/Close` 图标，调用方只需在 `on_click` 里翻转 View 的 `collapsed` 字段：

[crates/ui/src/sidebar/mod.rs:301-371](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sidebar/mod.rs#L301-L371) —— `SidebarToggleButton`。

再看叶子项 `SidebarMenuItem`，它支持图标、active 高亮、`on_click`、`default_open`（子菜单初始展开）、`click_to_open`/`click_to_toggle`（点击整行是否展开子菜单）、`suffix`（右侧附加元素，如 Badge）、右键 `context_menu`：

[crates/ui/src/sidebar/menu.rs:91-216](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sidebar/menu.rs#L91-L216) —— `SidebarMenuItem` 字段与全部 builder 方法。

`SidebarMenuItem::render` 用 `use_keyed_state` 管理子菜单展开，用一个箭头 `Button`（带 `stop_propagation` 避免触发整行点击）来翻转：

[crates/ui/src/sidebar/menu.rs:231-385](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sidebar/menu.rs#L231-L385) —— `render`：active 项用 `tokens.sidebar_accent` 高亮；有 `children` 即为子菜单，展开时递归渲染并加左侧竖线；折叠态时居中只显示图标。

`SidebarGroup` 只是一个带标题的纵向分组，展开时显示标题、折叠时隐藏标题：

[crates/ui/src/sidebar/group.rs:8-86](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sidebar/group.rs#L8-L86) —— `SidebarGroup`。

#### 4.2.4 代码实践

**实践目标**：搭一个带分组的侧边栏，并体会 `active` 与 `collapsed` 两个外置状态。

**操作步骤**（参考 `crates/story/src/stories/sidebar_story.rs`）：

```rust
// 示例代码（节选自 sidebar_story 的渲染逻辑）
Sidebar::new("my-sidebar")
    .side(Side::Left)
    .collapsible(SidebarCollapsible::Icon)
    .collapsed(self.collapsed)
    .w(px(220.))
    .header(SidebarHeader::new().child(/* Logo */))
    .child(
        SidebarGroup::new("工作区").child(
            SidebarMenu::new().child(
                SidebarMenuItem::new("收件箱")
                    .icon(Icon::new(IconName::Inbox))
                    .active(self.active == "inbox")
                    .on_click(cx.listener(|this, _, _, cx| { this.active = "inbox".into(); cx.notify(); })),
            ),
        ),
    )
```

再加一个 `SidebarToggleButton`，`on_click` 里翻转 `self.collapsed`。

**需要观察的现象**：

1. 点击切换按钮，宽度用约 200ms 平滑收窄到 48px，文字消失只剩图标。
2. 把 `collapsible` 改成 `Offcanvas`，再折叠——这次侧边栏完全滑出、不再占布局宽度。
3. 把 `collapsible` 改成 `None`，折叠按钮点击无效。

**预期结果**：三种折叠模式行为如上；快速来回点折叠按钮不会出现“卡在半展开”的竞态（因为 `SidebarAnimationState` 用 `hide_request` 版本号做了取消）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Sidebar` 把内容放进 GPUI 的 `list`（虚拟化）而不是直接 `v_flex().children(...)`？

**参考答案**：侧边栏可能包含非常多的菜单项（例如文件树式导航）。`list` 只渲染可见项（u7-l1 的虚拟化原理），保证菜单项很多时也不卡顿；同时 `ListState` 还驱动了滚动条（`.vertical_scrollbar(&list_state)`）。

**练习 2**：`SidebarAnimationState` 里的 `hide_request` 版本号解决了什么问题？

**参考答案**：Offcanvas 模式折叠时需要“先播放收起动画，动画结束后才真正卸载子元素”（否则隐藏的控件仍占据 Tab 焦点顺序）。如果在动画进行中用户又点开、又点折叠，就会有多个延迟回调竞争。`hide_request` 自增让 `finish_hide` 只接受最新一次请求，旧请求被忽略，从而避免错误卸载。

**练习 3**：`SidebarMenuItem` 的子菜单展开状态存在哪里？刷新窗口会保留吗？

**参考答案**：存在 `window.use_keyed_state(id, ...)` 申请的 `bool` 里（以元素 id 为 key）。它是窗口级的内存状态，不落盘，所以刷新/重开窗口会回到 `default_open` 指定的初始值。

---

### 4.3 Breadcrumb：面包屑导航

#### 4.3.1 概念说明

`Breadcrumb`（面包屑）用来显示「当前位置的层级路径」，例如 `首页 / 文档 / 项目 A`。它是本讲里最简单的组件：纯无状态、无动画、无状态机。它的设计点在于**自动处理“最后一项高亮、其余项灰显、相邻项之间插分隔符”**这三件事，让你只需 `child()` 连续追加文本即可。

#### 4.3.2 核心流程

1. `Breadcrumb::new()` 创建空容器。
2. 连续 `.child("首页").child("文档").child(BreadcrumbItem::new("项目A").on_click(...))` 追加项。`child` 接受任何能 `Into<BreadcrumbItem>` 的类型——`&str`/`String`/`SharedString` 都行。
3. `render` 时遍历，把最后一项标记 `is_last = true`（用前景色），其余用 `muted_foreground`（灰）；非末项后面自动跟一个 `ChevronRight` 图标作分隔符。

#### 4.3.3 源码精读

`BreadcrumbItem` 是单个项，支持 `disabled`（不可点击且灰显）与 `on_click`：

[crates/ui/src/breadcrumb.rs:91-110](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/breadcrumb.rs#L91-L110) —— `BreadcrumbItem::render`：最后一项用 `foreground`，其余用 `muted_foreground`；非禁用且有 `on_click` 时才显示 `cursor_pointer` 并绑定点击。

分隔符是单独的 `BreadcrumbSeparator`，渲染一个 `ChevronRight`：

[crates/ui/src/breadcrumb.rs:134-143](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/breadcrumb.rs#L134-L143) —— `BreadcrumbSeparator`。

`Breadcrumb::render` 负责组装：给每个项设 `id(ix)` 和 `is_last`，并在非末项后插入分隔符：

[crates/ui/src/breadcrumb.rs:151-173](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/breadcrumb.rs#L151-L173) —— 整体用一个 `h_flex().gap_1p5()` 横向排列；`is_last = ix == items_count - 1` 决定高亮；`if !is_last` 时 `push` 一个分隔符。

#### 4.3.4 代码实践

**实践目标**：做一个可点击的面包屑，点击中间项能“跳回”该层级。

**操作步骤**（参考 `crates/story/src/stories/breadcrumb_story.rs` 与 sidebar_story 里的面包屑）：

```rust
// 示例代码
Breadcrumb::new()
    .child("Home")
    .child(BreadcrumbItem::new("Documents").on_click(cx.listener(|this, _, _, cx| {
        this.path = "Documents".into();
        cx.notify();
    })))
    .child("Projects") // 最后一项，自动高亮且不可点的只是“当前位置”
```

**需要观察的现象**：`Home`、`Documents` 呈灰色，`Projects`（最后一项）呈前景深色；`Documents` 上鼠标变手型可点击，`Home` 因为没绑 `on_click` 不可点。

**预期结果**：点击 `Documents` 回调被触发；末项始终高亮。

#### 4.3.5 小练习与答案

**练习 1**：如果不想要默认的 `ChevronRight` 分隔符（例如想换成 `/`），该改哪里？

**参考答案**：当前 `Breadcrumb::render` 硬编码插入 `BreadcrumbSeparator`（`ChevronRight` 图标），没有对外暴露自定义分隔符的 builder。若要换成 `/`，需要自己不用 `Breadcrumb` 而是用 `h_flex()` 手动 `child(项).child("/").child(项)` 拼装，或在项目里给 `Breadcrumb` 提交一个增加分隔符配置的特性。

**练习 2**：为什么 `Breadcrumb` 也能用 `.child("字符串")` 直接传字符串？

**参考答案**：因为 `Breadcrumb::child` 的参数是 `impl Into<BreadcrumbItem>`，而源码里为 `&'static str`/`String`/`SharedString` 都实现了 `From<...> for BreadcrumbItem`，直接 `Self::new(value)`。

---

### 4.4 Select：单选下拉

#### 4.4.1 概念说明

`Select` 是经典的单选下拉框：一个像输入框的触发器，点击展开一个可滚动的选项列表，选中一项后收起并把选中值回填到触发器。和 `Tab` 不同，`Select` 是**有状态**的（打开/关闭、当前光标位置），所以它采用 u4-l4 的「状态实体 + 无状态外壳」模式：真正的状态在 `Entity<SelectState<D>>` 里，`Select` 只是借给它的渲染外壳。

`Select` 的精髓在于一个泛型数据模型：状态泛型参数 `D: SearchableListDelegate`。你只要提供「数据在哪、第几项是什么、怎么按关键字过滤」这三件事，`Select` 就帮你把搜索、虚拟化列表、键盘导航、选中提交全部接好。库内置了两个开箱即用的 `D`：

- `Vec<T>`：最简单，不可搜索。
- `SearchableVec<T>`：可搜索（在内存里增量过滤）。
- `SearchableVec<SelectGroup<T>>`：分组 + 可搜索。

#### 4.4.2 核心流程

`Select` 的使用三步：

1. `cx.new(|cx| SelectState::new(delegate, Some(initial_index), window, cx).searchable(true))` 创建状态实体。
2. `cx.subscribe_in(&state, window, handler)` 订阅 `SelectEvent::Confirm(Option<Value>)`。
3. 在 `render` 里 `Select::new(&state).placeholder("...").cleanable(true)`。

内部运行时：

- 触发器渲染为一个类输入框（`input_style`），显示 `display_title`（选中项标题或 placeholder）。
- 点击触发器 → `toggle_menu` → `set_open(true)`，并通过 `GlobalState::global_mut(cx).register_deferred_popover(...)` 把这个弹层登记为「延迟浮层」。
- 展开时，渲染一个 `deferred(anchored())` 的下拉层，内含一个虚拟化的 `List`（复用 u7 的列表），顶部可选搜索框。
- 选中一项 → `on_confirm` 回调：更新 selection、`cx.emit(SelectEvent::Confirm(value))`、关闭、把焦点还给触发器。
- 键盘：上下键移动光标、回车确认、Esc 取消（恢复到上次提交的选中项）。这些键位在 `select::init` 里绑定到 key_context `"Select"`。

#### 4.4.3 源码精读

为了向后兼容，`select.rs` 把 `searchable_list` 模块的几个类型再导出成 Select 风格的名字：

[crates/ui/src/select.rs:25-34](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/select.rs#L25-L34) —— `SelectGroup`=`SearchableGroup`、`SelectItem`=`SearchableListItem`、`SelectDelegate`=`SearchableListDelegate` 等（注释提示新代码应直接用 `SearchableList*`）。

键盘快捷键绑定（注意这是在 `gpui_component::init` 里调用的）：

[crates/ui/src/select.rs:38-50](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/select.rs#L38-L50) —— `up/down/enter/secondary-enter/escape` 绑定到 context `"Select"`。

`SelectState::new` 把三个核心回调（确认 `on_confirm`、取消 `on_cancel`、空态渲染 `on_render_empty`、失焦 `on_blur`）传给底层的 `SearchableListState`。确认回调里会 `cx.emit(SelectEvent::Confirm(final_value))`、设 selection、关闭、还焦点：

[crates/ui/src/select.rs:127-245](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/select.rs#L127-L245) —— `SelectState::new`：用 `cx.defer_in` 把对底层 list 的写操作推迟，避免在持锁回调里再次取锁导致的重入死锁。

`set_open` 通过 `GlobalState` 登记延迟浮层——这是 Select/Combobox 与 Root 协作的关键：

[crates/ui/src/select.rs:394-404](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/select.rs#L394-L404) —— `set_open`：打开时 `register_deferred_popover`、关闭时 `unregister_deferred_popover`。

`SelectState::render` 同时画触发器和（展开时）下拉层。下拉层是 `deferred(anchored().snap_to_window_with_margin(8.).child(... List ...))`，宽度默认跟触发器一致（`bounds.size.width + 2px`）：

[crates/ui/src/select.rs:452-588](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/select.rs#L452-L588) —— `Render for SelectState`：触发器用 `input_size`/`focused_border`，右侧根据 `cleanable` 显示清空按钮或 `ChevronDown` 图标；展开时 `deferred(anchored())` 画 `List`，并 `on_mouse_down_out` 实现点外部关闭。

触发器上显示的内容由 `display_title` 决定，它会优先用数据项自定义的 `display_title()`（如「中国 (CN)」这种富文本），否则用标题：

[crates/ui/src/select.rs:412-449](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/select.rs#L412-L449) —— `display_title`。

数据代理 `SearchableVec` 是入门最常用的 delegate，它维护「全量列表」和「过滤后视图」两份，搜索时重算过滤视图：

[crates/ui/src/searchable_list/vec.rs:76-140](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/searchable_list/vec.rs#L76-L140) —— `SearchableVec`：`perform_search` 用 `item.matches(query)` 过滤全量列表得到 `matched_items`，`items_count`/`item` 都基于过滤视图，所以列表只显示匹配项。

#### 4.4.4 代码实践

**实践目标**：实现一个可搜索的国家选择器，监听选中事件。

**操作步骤**（参考 `crates/story/src/stories/select_story.rs`）：

```rust
// 示例代码
let fruits = SearchableVec::new(vec!["Apple", "Orange", "Banana", "Grape"]);
let fruit_select = cx.new(|cx| SelectState::new(fruits, None, window, cx).searchable(true));
cx.subscribe_in(&fruit_select, window, |_, event: &SelectEvent<_>, _, _| match event {
    SelectEvent::Confirm(v) => println!("selected: {:?}", v),
}).detach();
```

渲染：

```rust
// 示例代码
Select::new(&self.fruit_select)
    .placeholder("选择水果")
    .icon(IconName::Search)
    .cleanable(true)
    .menu_width(px(320.))
```

**需要观察的现象**：

1. 点击触发器，下拉出现；输入「ap」列表实时过滤成 Apple/Grape。
2. 选中后下拉收起，触发器显示选中项；控制台打印选中值。
3. 点下拉外部或按 Esc 关闭；Esc 取消时光标恢复到上次提交的项（不是当前悬停项）。

**预期结果**：如上。注意：如果忘了 `cx.subscribe_in`，选中后 `println` 不会执行（事件无人监听）。

#### 4.4.5 小练习与答案

**练习 1**：`Select` 的状态为什么是 `Entity<SelectState<D>>` 而不是直接存在 View 里？

**参考答案**：因为 Select 有大量内部状态（打开/关闭、光标位置、搜索词、滚动位置、焦点句柄），且这些状态需要跨多次渲染稳定持有、需要在事件回调里可变更新。把它做成独立 `Entity`（实现 `Render + Focusable + EventEmitter`）后，`Select` 外壳成为无状态 `RenderOnce`，只负责把 builder 配置同步进去并渲染——这正是 u4-l4 「视图与状态解耦」的模式。

**练习 2**：`Vec<T>` 和 `SearchableVec<T>` 都实现了 `SearchableListDelegate`，作为 `Select` 的数据源有什么区别？

**参考答案**：`Vec<T>` 实现的 delegate 不重写 `perform_search`，没有搜索能力，`.searchable(true)` 也无从过滤；`SearchableVec<T>` 维护 `matched_items` 过滤视图并实现了 `perform_search`，所以可搜索。`SearchableVec<SelectGroup<T>>` 还额外支持分组标题。

**练习 3**：`SelectEvent::Confirm` 携带的 `Option<Value>` 在什么情况下是 `None`？

**参考答案**：当用户清空选择（`cleanable(true)` 时点清空按钮）或初始化未选时，会 `cx.emit(SelectEvent::Confirm(None))`。订阅方据此区分“选了某项”和“清空”。

---

### 4.5 Combobox：可搜索的单/多选下拉

#### 4.5.1 概念说明

`Combobox` 是 `Select` 的增强版：底层引擎完全相同（同一个 `SearchableListState` + `SearchableListDelegate`），但在其之上增加了三件事——**多选**、**自定义触发器外观**、**下拉层底部的 footer 区域**。

单选模式下，点击一项就替换选中并关闭（和 `Select` 行为一致）；多选模式下（`.multiple(true)`），点击一项是“切换该项”，下拉层保持打开，可以连续勾选多个。

#### 4.5.2 核心流程

使用三步与 Select 类似：

1. `cx.new(|cx| ComboboxState::new(delegate, vec![初始 IndexPath...], window, cx).multiple(true).searchable(true))`。
2. 订阅 `ComboboxEvent`：`Change(Vec<Value>)`（每次勾选/取消都发）、`Confirm(Vec<Value>)`（关闭时发）。
3. 渲染 `Combobox::new(&state).placeholder("...").cleanable(true)`，可选 `.render_trigger(...)` 自定义触发器、`.footer(...)` 加底部区域。

内部关键差异：

- 点击项 → `selection_changes` 根据 `multiple` 计算 `Select`/`Deselect` 变更，先调用 delegate 的 `on_will_change`（**可以否决**变更），再提交。
- 多选时 `should_close = false`，保持打开；单选时 `changed && !multiple` 才关闭。
- 触发器内容：多选时把所有选中标题用「, 」拼接；单选时显示第一个。

#### 4.5.3 源码精读

两种事件：`Change`（每次切换）、`Confirm`（关闭时）：

[crates/ui/src/combobox.rs:112-120](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/combobox.rs#L112-L120) —— `ComboboxEvent::Change` / `Confirm`。

`selection_changes` 是单/多选行为分叉的核心——多选切换、单选替换：

[crates/ui/src/combobox.rs:347-371](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/combobox.rs#L347-L371) —— `selection_changes`：多选时若已选则 `Deselect` 否则 `Select`；单选时先把现有所有项 `Deselect` 再 `Select` 新项（即“替换”）。

确认回调（在 `ComboboxState::new` 里传给底层）计算变更、调 `on_will_change`、判断是否关闭、发事件：

[crates/ui/src/combobox.rs:142-214](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/combobox.rs#L142-L214) —— `on_confirm` 闭包：`should_close = changed && !multiple`，多选不关；变更时发 `Change`，关闭时发 `Confirm`。

默认触发器内容（多选拼接标题、单选取第一个）：

[crates/ui/src/combobox.rs:513-559](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/combobox.rs#L513-L559) —— `default_trigger_body`。

`ComboboxState::render` 与 Select 几乎同构（同样的 `deferred(anchored())` + `List`），但多了 `render_trigger`/`footer` 的可选注入，下拉层底部可加一条带分隔线的 footer：

[crates/ui/src/combobox.rs:561-679](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/combobox.rs#L561-L679) —— `Render for ComboboxState`。

`Combobox` 外壳的 builder：`placeholder`、`icon`（替换触发器箭头）、`check_icon`（选中项前的勾）、`cleanable`、`render_trigger`、`footer`：

[crates/ui/src/combobox.rs:733-828](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/combobox.rs#L733-L828) —— `Combobox` builder 方法。

> **Select vs Combobox 选型**：只需单选、用默认触发器 → `Select`；要多选、自定义触发器外观、或要在下拉层加底部操作区 → `Combobox`。

#### 4.5.4 代码实践

**实践目标**：实现一个可搜索的多选「技术栈」选择器，触发器显示已选项数量。

**操作步骤**（参考 `crates/story/src/stories/combobox_story.rs`）：

```rust
// 示例代码
let frameworks = SearchableVec::new(vec!["React", "Vue", "Angular", "Svelte"]);
let stack = cx.new(|cx| {
    ComboboxState::new(frameworks, vec![], window, cx).multiple(true).searchable(true)
});
cx.subscribe_in(&stack, window, |_, event: &ComboboxEvent<_>, _, _| match event {
    ComboboxEvent::Change(v) => println!("当前选择: {:?}", v),
    ComboboxEvent::Confirm(v) => println!("确认: {:?}", v),
}).detach();
```

渲染：

```rust
// 示例代码
Combobox::new(&self.stack)
    .placeholder("选择技术栈")
    .search_placeholder("搜索...")
    .cleanable(true)
```

**需要观察的现象**：

1. 展开后可连续勾选多个，下拉不关闭，每勾一个发一次 `Change`。
2. 触发器把所有选中项用「, 」拼接显示。
3. 输入搜索词过滤；按 Esc 或点外部关闭，关闭瞬间发一次 `Confirm`。

**预期结果**：如上。若把 `.multiple(true)` 去掉，则变回单选（点一项即关闭）。

#### 4.5.5 小练习与答案

**练习 1**：多选模式下，`Change` 和 `Confirm` 的触发时机有何区别？

**参考答案**：`Change` 在每次勾选/取消某项时立即发出（携带当前全部选中值）；`Confirm` 只在下拉关闭时（Esc、点外部）发出一次。所以实时联动用 `Change`，最终提交用 `Confirm`。

**练习 2**：`on_will_change` 在 `selection_changes` 之后、真正提交之前被调用，能用来做什么？

**参考答案**：delegate 的 `on_will_change` 收到 `&mut selection` 和 `&[changes]`，可以选择“忽略这些 changes”（保持 selection 不变），从而**否决**这次选择。例如限制最多选 2 项：当已达上限且要新增时，`on_will_change` 不把新项 push 进 selection，选择被静默拒绝（combobox_story 里的 `Max2Delegate` 正是这么做的）。

**练习 3**：为什么 `Combobox` 的 id 是 `("multi-combo-box", state.entity_id())` 而 `Select` 是 `("select", state.entity_id())`？

**参考答案**：为了让同页面上多个 Select/Combobox 各有独立、稳定的 GPUI 元素 id（用实体 id 做后缀避免冲突），同时区分两者的 key_context（`"Select"` vs `"Combobox"`），保证各自的键盘快捷键只在对应组件聚焦时生效、互不串扰。

---

## 5. 综合实践

把本讲五个组件串成一个「应用主框架」小任务。

**实践目标**：实现一个左侧 Sidebar 导航、顶部 Breadcrumb、主区域 Tab 切换多面板，其中一个面板放一个 Combobox 做搜索多选的完整界面。

**操作步骤**：

1. View 状态（外置所有跨帧状态）：

```rust
// 示例代码
pub struct AppShell {
    collapsed: bool,
    active_route: String,   // Sidebar 选中项
    active_tab: usize,      // TabBar 选中号
    stack: Entity<ComboboxState<SearchableVec<&'static str>>>,
}
```

2. `new` 里创建多选 Combobox 状态：`ComboboxState::new(SearchableVec::new(vec![...]), vec![], w, cx).multiple(true).searchable(true)`。

3. `render` 用 `h_flex` 横排 Sidebar 与主区，主区里先放 `Breadcrumb` 再放 `TabBar`：

```rust
// 示例代码（骨架）
h_flex().size_full()
    .child(
        Sidebar::new("nav").collapsed(self.collapsed)
            .child(SidebarGroup::new("导航").child(SidebarMenu::new().child(
                SidebarMenuItem::new("面板一").active(self.active_route == "p1")
                    .on_click(cx.listener(|this, _, _, cx| { this.active_route = "p1".into(); this.active_tab = 0; cx.notify(); })),
            )))
            .child(SidebarToggleButton::new().collapsed(self.collapsed)
                .on_click(cx.listener(|this, _, _, cx| { this.collapsed = !this.collapsed; cx.notify(); }))),
    )
    .child(v_flex().flex_1().child(
        Breadcrumb::new().child("首页").child("面板一"),
    ).child(
        TabBar::new("tabs").selected_index(self.active_tab)
            .on_click(cx.listener(|this, ix: &usize, _, cx| { this.active_tab = *ix; cx.notify(); }))
            .child("概览").child("设置"),
    ).child(match self.active_tab {
        0 => div().child("概览内容"),
        _ => v_flex().child("设置内容").child(Combobox::new(&self.stack).placeholder("选择技术栈")),
    }))
```

**需要观察的现象**：

1. 折叠按钮收窄 Sidebar，主区自动扩展占满。
2. Sidebar 项 active 高亮；Breadcrumb 末项深色。
3. Tab 切换面板，切换瞬间滑动指示器动画顺滑。
4. 「设置」面板里的 Combobox 可搜索多选，触发器显示已选项。

**预期结果**：四类组件协同工作，状态各自外置、互不干扰。

**如果无法本地运行**：以上为「源码阅读型实践」，可对照 `crates/story/src/stories/{sidebar,tabs,breadcrumb,combobox}_story.rs` 阅读真实实现，理解状态如何外置与回调如何闭环。

## 6. 本讲小结

- `TabBar`/`Tab` 是无状态标签条，选中索引外置到 View；五种 `TabVariant` 经 normal/hovered/selected/disabled 四态样式表生成外观，Pill/Segmented/Underline 靠 `on_prepaint` 测矩形 + `with_animation` 插值实现滑动指示器，`epoch` 保证动画与文字淡入同步。
- `Sidebar` 是泛型折叠容器，子项需实现 `SidebarItem: Collapsible + Clone`；三种 `SidebarCollapsible`（Icon/Offcanvas/None）控制折叠形态，宽度动画状态用 `use_keyed_state` 跨帧保持并用版本号防竞态；内容虚拟化在 GPUI `list` 中。
- `Breadcrumb` 最简单：无状态、自动给末项高亮、相邻项间插 `ChevronRight` 分隔符。
- `Select`/`Combobox` 共享 `SearchableListState` + `SearchableListDelegate` 引擎：状态是 `Entity<*State<D>>`，外壳是无状态 `RenderOnce`；下拉层是 `deferred(anchored())` 里的虚拟化 `List`，通过 `GlobalState::register_deferred_popover` 与 Root 协作；键盘快捷键在 `init` 里绑定到各自 key_context。
- `Select` 单选、`Combobox` 支持多选/自定义触发器/footer，靠 `selection_changes` 区分单选替换与多选切换，`on_will_change` 提供选择否决点。

## 7. 下一步学习建议

- 本讲的 `Sidebar` 折叠动画、`Tab` 滑动指示器都建立在 GPUI 的 `use_keyed_state` 与 `with_animation` 之上，下一单元 **u6 Dock 布局系统** 会把这些窗口级布局思想推广到完整的 `DockArea` + `DockItem` 树，并复用 `TabPanel`（注意它和本讲 `Tab` 的区别）。
- `Select`/`Combobox` 内部都依赖 `List` 与 `SearchableListDelegate`，这部分正是 **u7 高性能数据展示** 的虚拟化主题；学完 u7 你就能为它们写自定义 delegate（异步加载、海量数据）。
- 若想给 `Select`/`Combobox` 接入树形或带图标/描述的富选项，可结合 **u7-l3 Tree** 与 `SearchableListItem::render` 自定义渲染，做出像「带国旗的国家选择器」这类高级形态。
