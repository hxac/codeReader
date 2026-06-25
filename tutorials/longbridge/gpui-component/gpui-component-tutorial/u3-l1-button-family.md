# 按钮家族：Button / ButtonGroup / DropdownButton / Toggle

## 1. 本讲目标

学完本讲，你应该能够：

- 用 `Button` 创建一个可点击按钮，并理解它的「变体（variant）」「轮廓（outline）」「尺寸（size）」三套样式开关如何组合。
- 看懂 `Button` 内部「五态样式（normal / hovered / active / selected / disabled）」的计算模型，知道按钮颜色从哪里来。
- 用 `ButtonGroup` 把多个按钮拼成一组（横向 / 纵向、单选 / 多选），并理解它如何「裁剪相邻按钮的圆角与边框」让按钮视觉上连成一片。
- 用 `DropdownButton` 组合「主按钮 + 下拉箭头按钮」，理解它是两个 `Button` 的拼装。
- 用 `Toggle` 做一个「可勾选」的二态按钮，并区分它与普通 `Button` 的关系。

本讲是第 3 单元「基础展示组件」的第一篇，也是后续几乎所有交互组件的基石——菜单、对话框、表单里都充斥着 `Button`。

## 2. 前置知识

本讲默认你已经掌握前两个单元的内容，尤其是：

- **无状态组件与 RenderOnce**（u2-l2）：`Button` 派生 `IntoElement`，每帧由 `render(self)` 重建，自身不持有跨帧状态，状态由外层 View 持有。
- **主题系统**（u2-l1）：按钮颜色全部来自 `cx.theme()`，例如 `cx.theme().primary`、`cx.theme().tokens.button_primary`。本讲你会看到按钮如何「消费」主题色。
- **样式系统与 Sizable**（u2-l2）：`Sizable` trait 提供 `with_size` 与 `xsmall/small/large`，统一支持四档尺寸。
- **图标系统**（u2-l3）：`Button::icon(...)` 可接收 `IconName`、`Icon`，也可接收 `Spinner`、`ProgressCircle`。

此外，本讲会用到三个由库统一提供的「能力 trait」，它们都定义在 [crates/ui/src/styled.rs:369-389](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L369-L389)：

```rust
pub trait Selectable: Sized {
    fn selected(mut self, selected: bool) -> Self;
    fn is_selected(&self) -> bool;
    // ...
}

pub trait Disableable {
    fn disabled(mut self, disabled: bool) -> Self;
}
```

- `Sizable`：控制尺寸（`with_size`）。
- `Disableable`：控制禁用（`disabled(true)`）。
- `Selectable`：控制选中（`selected(true)`）。

`Button`、`ButtonGroup`、`DropdownButton`、`Toggle` 都实现了其中若干个，因此它们都长着一套相似的「开关式 API」。这是 gpui-component 的一个重要设计约定：**能力以 trait 暴露，组件按需实现**。

> 术语提示：本讲把 `Button` 这种「按一下触发动作」的元素叫**动作按钮**；把 `Toggle` 这种「按一下在 开/关 之间切换并保持状态」的元素叫**二态按钮**。

## 3. 本讲源码地图

按钮家族位于 [crates/ui/src/button/](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button) 目录，由 [crates/ui/src/button/mod.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/mod.rs#L1-L12) 统一组织并再导出：

| 文件 | 作用 |
| --- | --- |
| `button.rs` | 核心 `Button` 组件、`ButtonVariant` 枚举、`ButtonVariants` trait、五态样式计算 |
| `button_group.rs` | `ButtonGroup`：把多个 `Button` 拼成一组 |
| `dropdown_button.rs` | `DropdownButton`：主按钮 + 下拉箭头按钮的组合 |
| `toggle.rs` | `Toggle` 二态按钮，以及 `ToggleGroup` |
| `button_icon.rs` | `ButtonIcon`：让 `Button::icon` 既能放图标也能放 `Spinner` / `ProgressCircle` |

`mod.rs` 的再导出策略值得注意：`pub use button::*` 把 `Button` 及其变体类型全部导出到 `button` 命名空间下，因此使用时写 `gpui_component::button::{Button, ButtonCustomVariant, ButtonGroup, ButtonVariants}`，见 [crates/story/src/stories/button_story.rs:6-13](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/button_story.rs#L6-L13)。

## 4. 核心概念与源码讲解

### 4.1 Button：一个会算颜色的无状态组件

#### 4.1.1 概念说明

`Button` 是整个库最基础的交互组件。它有三个相互独立的「样式维度」：

1. **变体（variant）**：决定按钮的「语义角色」，例如主操作 `Primary`、危险操作 `Danger`、幽灵按钮 `Ghost`。由 `ButtonVariant` 枚举表达。
2. **轮廓（outline）**：一个布尔开关，把按钮变成「浅色描边」版本（背景半透明、文字使用主题强调色）。由 `outline()` 开启。
3. **尺寸（size）**：`xs/sm/md/lg` 四档，来自 `Sizable`。

这三者**正交组合**：你可以要一个「大号、危险、轮廓」按钮，也可以要一个「小号、主色、实心」按钮。理解这一点是掌握 `Button` 的关键。

此外，`Button` 还有几个状态修饰：

- `disabled(true)`：禁用，不可点击，颜色变灰。
- `selected(true)`：选中，使用「激活态」配色（常用于工具栏里被按下的按钮）。
- `loading(true)`：加载中，图标位置换成 `Spinner`，且按钮不可点击。

#### 4.1.2 核心流程

`Button` 是无状态组件（派生 `IntoElement`），每帧的渲染流程可以概括为：

```text
Button::new(id)
  → 链式设置：variant / outline / size / label / icon / on_click ...
  → 渲染时 RenderOnce::render(self)
      1. 由 variant + outline 计算出 normal 样式 {bg, border, fg, underline, shadow}
      2. 构造可交互的 base: Stateful<Div>
      3. 应用尺寸（区分「图标按钮」与「普通按钮」两种内边距/尺寸策略）
      4. 应用圆角、边框（按 border_corners / border_edges 精细控制每个角和每条边）
      5. 按 disabled / selected / 普通 分别套用 normal / selected / disabled / hover / active 样式
      6. 挂载 on_mouse_down、on_click、on_hover、tooltip、focus ring
      7. 渲染内部 h_flex：图标 + 文本 + 自定义 children + 下拉箭头
```

其中第 1 步和第 5 步是「按钮颜色从哪里来」的核心。`ButtonVariant` 上挂着一组方法 `normal / hovered / active / selected / disabled`，每个方法返回一个 `ButtonVariantStyle { bg, border, fg, underline, shadow }`，即一个完整的「视觉状态」。`render` 根据按钮当前是普通/悬停/按下/选中/禁用，选择对应方法算出的样式套到 `base` 上。

> 直觉：可以把 `ButtonVariant` 想成「一个配色函数集」，输入是「状态 + 是否 outline」，输出是「一组颜色」。`render` 只负责把输出涂到 div 上。

`outline` 的本质是把「实心背景」替换为「主题色按低透明度稀释的背景」。以 `Primary` 为例，实心态背景是 `tokens.button_primary`（通常是一条渐变），而 outline 态背景是 `tokens.primary.background.opacity(0.1)`，透明度近似为：

\[
\alpha_{\text{outline}}(s) = 0.1 + 0.1 \cdot k_s,\quad k_s \in \{0,1,2,3\}\ \text{对应 normal/hover/active 的递进}
\]

即 normal=0.1、hover=0.2、active=0.4（见下文源码），颜色越「深」表示交互越强。

#### 4.1.3 源码精读

**结构体定义**：`Button` 持有一个可交互的 `base: Stateful<Div>` 作为渲染底座，其余字段都是样式开关与回调，见 [crates/ui/src/button/button.rs:183-213](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L183-L213)。注意 `base` 在 `new` 时就带上了元素 id，因为 `dropdown_menu` 要复用这个 id 来创建弹出菜单。

**构造与链式 API**：`Button::new` 给出全部默认值，见 [crates/ui/src/button/button.rs:222-258](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L222-L258)。默认 `variant` 是 `Default`、`size` 是 `Medium`、`rounded` 是 `Medium`、所有角和边都为 `true`（即完整圆角与四边边框）。典型的几个 setter：

- `outline()` 仅翻转一个布尔位，见 [crates/ui/src/button/button.rs:260-264](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L260-L264)。
- `label(...)` 与 `icon(...)`：当没有 label 且没有 children 时，按钮进入「图标按钮模式」（正方形），见下文。
- `on_click(...)`：把闭包包进 `Rc` 存起来，见 [crates/ui/src/button/button.rs:332-338](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L332-L338)。

**变体枚举与快捷方法**：`ButtonVariant` 列出全部语义角色，见 [crates/ui/src/button/button.rs:138-153](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L138-L153)。配套的 `ButtonVariants` trait 把 `primary()`、`danger()`、`ghost()`、`link()`、`text()` 等都实现成 `with_variant(...)` 的语法糖，见 [crates/ui/src/button/button.rs:42-94](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L42-L94)。这个 trait 也被 `ButtonGroup`、`DropdownButton` 实现，所以「给一组按钮统一设主色」只需在 group 上调 `.primary()`。

**能力 trait 的实现**：`Button` 实现了 `Disableable`、`Selectable`、`Sizable`、`ButtonVariants`、`Styled`、`ParentElement`、`InteractiveElement`，见 [crates/ui/src/button/button.rs:387-435](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L387-L435)。其中 `InteractiveElement` 直接委托给 `self.base.interactivity()`，意味着 GPUI 的 `on_click`/`id`/`hover` 等原生能力也适用于 `Button`。

**可点击判定**：一个按钮「可点击」当且仅当「未禁用、未加载、且设置了 `on_click`」，见 [crates/ui/src/button/button.rs:376-379](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L376-L379)：

```rust
fn clickable(&self) -> bool {
    !(self.disabled || self.loading) && self.on_click.is_some()
}
```

这意味着「没有 `on_click` 的按钮不会响应点击」，但也仍可被 `selected` 高亮（用作纯展示态）。

**渲染主体（精简版）**：`RenderOnce::render` 先算出 `normal_style`，再用一长串 `.when(...)` 链按状态套色，见 [crates/ui/src/button/button.rs:437-645](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L437-L645)。几个关键片段：

- 计算当前状态样式：[crates/ui/src/button/button.rs:443](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L443) `let normal_style = style.normal(self.outline, cx);`
- 圆角按主题 `radius` 缩放（Small=0.5×, Medium=1×, Large=2×），见 [crates/ui/src/button/button.rs:455-461](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L455-L461)。
- 尺寸分两支：「图标按钮」走 `size_5/6/8` 的正方形；「普通按钮」走 `h_8().px_4()` 之类的高度+内边距，见 [crates/ui/src/button/button.rs:481-505](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L481-L505)。
- 普通态（非禁用非选中）套 `normal` 色，并注册 `.hover(...)` / `.active(...)`，见 [crates/ui/src/button/button.rs:531-547](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L531-L547)。
- 鼠标按下时禁止窗口级文本选择，见 [crates/ui/src/button/button.rs:556-569](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L556-L569)（`GlobalState::suppress_text_selection`）。
- 内部内容区是一个 `h_flex`，依次放 图标 / 文本 / children / 下拉箭头，见 [crates/ui/src/button/button.rs:587-620](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L587-L620)。

**五态样式计算**：`ButtonVariant` 实现了 `normal/hovered/active/selected/disabled` 五个方法，每个返回 `ButtonVariantStyle`。`bg_color` 决定实心底色，例如 `Primary` 取 `tokens.button_primary`，`Ghost/Link/Text` 取透明，见 [crates/ui/src/button/button.rs:736-752](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L736-L752)。`outline_background` 则是 outline 态的半透明配色，能看到前面提到的 0.1/0.2/0.4 透明度递进，见 [crates/ui/src/button/button.rs:664-734](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L664-L734)。

**图标可以是 Spinner/进度圈**：`Button::icon` 接收的是 `ButtonIcon`，而 `ButtonIcon` 能从 `Icon`、`Spinner`、`ProgressCircle` 三者转换而来，见 [crates/ui/src/button/button_icon.rs:52-80](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button_icon.rs#L52-L80)。当 `loading=true` 且图标本身不是 spinner/progress 时，会自动替换成 `Spinner`，见 [crates/ui/src/button/button_icon.rs:116-131](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button_icon.rs#L116-L131)。Story 里「Installing…」按钮直接把 `ProgressCircle` 当图标塞进去，见 [crates/story/src/stories/button_story.rs:358-386](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/button_story.rs#L358-L386)。

#### 4.1.4 代码实践

**实践目标**：亲手构造几种变体的 `Button`，并验证「变体 / outline / 禁用 / 加载」的组合效果。

**操作步骤**（参考 `hello_world` 示例的入口骨架，u1-l4 已讲）：

1. 在你的 View 的 `render` 里放一个垂直布局，依次创建：Default、Primary、Danger、Ghost 四个按钮。
2. 给 Primary 按钮接 `on_click`，在回调里 `println!` 打印一行。
3. 复制 Primary 按钮，再加 `.outline()`，对比实心与轮廓的差异。
4. 再做一个 `.disabled(true)` 和一个 `.loading(true)` 的按钮。

参考写法（示例代码，基于 [crates/story/src/stories/button_story.rs:160-267](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/button_story.rs#L160-L267) 的风格简化）：

```rust
// 示例代码
use gpui_component::{
    button::{Button, ButtonVariants as _},
    Disableable as _, v_flex,
};

v_flex().gap_2()
    .child(Button::new("btn-default").label("Default").on_click(|_, _, _| {
        println!("default clicked");
    }))
    .child(Button::new("btn-primary").primary().label("Primary"))
    .child(Button::new("btn-danger").danger().label("Danger"))
    .child(Button::new("btn-primary-outline").primary().outline().label("Primary Outline"))
    .child(Button::new("btn-disabled").primary().label("Disabled").disabled(true))
    .child(Button::new("btn-loading").primary().label("Loading").loading(true))
```

**需要观察的现象**：

- Default 按钮是浅灰描边；Primary 是主题主色实心；Danger 是红色实心；Ghost 无背景无边框，悬停才出现浅底色。
- Primary Outline 与 Primary 文字同为主色，但背景几乎透明、带主色描边。
- Disabled 按钮颜色变灰、不响应点击；Loading 按钮左侧出现旋转图标、同样不响应点击。

**预期结果**：点击 Default 按钮在终端打印 `default clicked`；其余无 `on_click` 的按钮点击无反应但仍正常渲染。运行结果待本地验证（需在带显示的环境执行 `cargo run`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么「没有设置 `on_click` 的 `Button`」点击没有反应，却仍能被 `selected(true)` 高亮？

**参考答案**：`clickable()` 要求 `on_click.is_some()`，缺回调即不可点击（见 [button.rs:376-379](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L376-L379)）；而 `selected` 是独立的样式状态，在 `render` 里通过 `.when(self.selected, ...)` 单独套用选中配色（见 [button.rs:525-530](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L525-L530)），与是否可点击无关。这种按钮常用于「纯展示当前状态」。

**练习 2**：`Ghost`、`Link`、`Text` 三种变体的背景色是什么？它们的 `no_padding` 行为如何区分？

**参考答案**：三者底色都是 `cx.theme().transparent`（见 [button.rs:749](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L749)）。但只有 `Link` 和 `Text` 满足 `no_padding()`（见 [button.rs:171-174](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L171-L174)），即没有内边距、看起来像普通文字；`Ghost` 仍保留按钮内边距。此外 `Link` 会在文本下加下划线（`underline()` 返回 true，见 [button.rs:850-855](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L850-L855)）。

---

### 4.2 ButtonGroup：把按钮拼成连体一组

#### 4.2.1 概念说明

当你需要「一组语义相关的按钮」时（例如字号选择「小/中/大」、对齐方式「左/中/右」），用 `ButtonGroup` 把多个 `Button` 包起来，它会：

- 让相邻按钮**视觉上连成一片**：去掉中间的圆角与重复边框。
- 统一应用 `variant / outline / size / compact / disabled` 到组内每个按钮（不用逐个设）。
- 提供**单选 / 多选**语义：点击某按钮时，`on_click` 回调返回被选中按钮的下标列表 `&Vec<usize>`。

`ButtonGroup` 只接收 `Button` 作为子元素（`child(child: Button)`），不支持任意元素。

#### 4.2.2 核心流程

`ButtonGroup` 本身也是无状态组件，它的 `render` 做了三件巧妙的事：

```text
ButtonGroup::new(id)
  → .child(Button...) × N，并记录每个按钮初始 selected
  → RenderOnce::render(self)
      1. 用 Rc<Cell<Option<usize>>> 作为「哪个子按钮被点」的共享记号
      2. 给每个子按钮注入一个内部 on_click：被点时把自己的下标写进 Cell
      3. 按下标位置裁剪每个子按钮的 border_corners / border_edges：
           - 第一个：保留左上/左下圆角，去掉右上/右下（横向时）
           - 中间：四个角全去圆角
           - 最后一个：保留右上/右下圆角
      4. 在最外层 div 上挂 on_click：读出 Cell 里的下标，
         按 multiple 模式更新 selected_ixs（单选: clear+push；多选: toggle）
         再调用用户的 on_click(&selected_ixs, ...)
```

注意：`ButtonGroup` 自身**不维护选中状态**，它每次渲染都从子按钮的 `selected` 字段重新收集当前选中下标（见下文源码）。真正的「选中态」要由外层 View 持有，再通过 `Button::new(...).selected(...)` 回填——这是 gpui-component「状态外置」的典型体现。

#### 4.2.3 源码精读

**结构体**：`ButtonGroup` 持有 `children: Vec<Button>` 以及一组「待下发给子按钮」的属性，见 [crates/ui/src/button/button_group.rs:17-33](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button_group.rs#L17-L33)。

**child 自动继承 disabled**：`child` 在加入子按钮时，会用组级别的 `disabled` 覆盖子按钮，见 [crates/ui/src/button/button_group.rs:61-64](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button_group.rs#L61-L64)：

```rust
pub fn child(mut self, child: Button) -> Self {
    self.children.push(child.disabled(self.disabled));
    self
}
```

**单选 / 多选与方向**：`multiple(true)` 开多选，`layout(Axis::Vertical)` 改纵向，见 [crates/ui/src/button/button_group.rs:73-82](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button_group.rs#L73-L82)。

**on_click 契约**：回调第一个参数是被选中按钮的下标列表，文档里直接给了字号选择的示例，见 [crates/ui/src/button/button_group.rs:100-129](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button_group.rs#L100-L129)。

**render：收集初始选中 + 共享 Cell**：见 [crates/ui/src/button/button_group.rs:152-162](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button_group.rs#L152-L162)。它遍历子按钮收集 `selected_ixs`，并创建 `state: Rc<Cell<Option<usize>>>`。

**render：裁剪相邻按钮圆角/边框**：按下标分首/中/尾三种情况，用 `border_corners` / `border_edges` 精细控制；纵向时角的方向相应旋转，见 [crates/ui/src/button/button_group.rs:176-225](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button_group.rs#L176-L225)。这里也把组级别的 `size/variant/compact/outline` 下发给每个子按钮。

**render：注入内部 on_click + 最外层聚合**：每个子按钮被注入一个写 Cell 的 on_click，最外层 div 再读取 Cell 并按 multiple 模式更新选中集合后回调用户，见 [crates/ui/src/button/button_group.rs:230-260](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button_group.rs#L230-L260)。

**真实使用**：Story 的「Toggle Button Group」用 `ButtonGroup` 做了一个多选开关组，把 `disabled/loading/selected/compact` 四个布尔状态映射到四个按钮的选中态，并在 `on_click` 里根据下标回写状态，见 [crates/story/src/stories/button_story.rs:775-807](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/button_story.rs#L775-L807)。

#### 4.2.4 代码实践

**实践目标**：用 `ButtonGroup` 实现一个「字号选择器」，体会单选语义与「选中态外置」。

**操作步骤**：

1. View 里持有一个 `font_size: Size` 字段（`Size::Small/Medium/Large`）。
2. 渲染一个 `ButtonGroup`，三个按钮分别 `.selected(self.font_size == Size::Small)` 等。
3. `on_click` 回调里根据 `&Vec<usize>` 把对应下标翻译回 `Size`，并 `cx.notify()`。

参考写法（示例代码，简化自 [button_group.rs:107-122](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button_group.rs#L107-L122) 的文档示例）：

```rust
// 示例代码
ButtonGroup::new("size-group").outline()
    .child(Button::new("sm").label("Small").selected(self.font_size == Size::Small))
    .child(Button::new("md").label("Medium").selected(self.font_size == Size::Medium))
    .child(Button::new("lg").label("Large").selected(self.font_size == Size::Large))
    .on_click(cx.listener(|view, clicks: &Vec<usize>, _, cx| {
        view.font_size = match clicks.first() {
            Some(0) => Size::Small,
            Some(2) => Size::Large,
            _ => Size::Medium,
        };
        cx.notify();
    }))
```

**需要观察的现象**：三个按钮连成一片，中间无圆角；点击任一按钮后只有它高亮（单选），其余恢复普通态。

**预期结果**：点击 Large 后，`view.font_size` 变为 `Size::Large`，且只有 Large 按钮显示选中色。运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果想让组内按钮支持「同时选中多个」（例如多选粗体/斜体/下划线），应该怎么做？

**参考答案**：在 `ButtonGroup` 上调用 `.multiple(true)`，此时 `render` 里的更新逻辑会走 `toggle` 分支（见 [button_group.rs:245-250](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button_group.rs#L245-L250)），`on_click` 返回的 `&Vec<usize>` 会包含所有当前选中的下标。

**练习 2**：`ButtonGroup` 的「选中态」存在哪里？为什么点击后 UI 能更新？

**参考答案**：选中态存在外层 View 的字段里，`ButtonGroup` 每帧从子按钮的 `selected` 字段重新收集。点击时回调更新 View 字段并 `cx.notify()` 触发重渲染，重渲染时 `selected(self.xxx == ...)` 把新状态回填到对应按钮，于是 UI 更新。`ButtonGroup` 自己不存任何跨帧选中状态。

---

### 4.3 DropdownButton：主按钮 + 下拉箭头的拼装

#### 4.3.1 概念说明

`DropdownButton` 解决「一个按钮既要触发主操作、又要展开一个下拉菜单」的场景（例如「保存」+ 旁边的小箭头展开「另存为/导出」）。它的实现非常直白：**它就是两个 `Button` 的拼装**——左边一个用户给定的主按钮，右边一个固定带 `ChevronDown` 图标的「弹出按钮」，后者挂载一个 `DropdownMenu`。

> 前置提示：`DropdownMenu` 与 `PopupMenu` 的内部机制属于第 5 单元 u5-l4「菜单系统」，本讲只关注 `DropdownButton` 如何把它们拼起来，不展开菜单本身。

#### 4.3.2 核心流程

```text
DropdownButton::new(id)
  → .button(主 Button)         // 左半部分
  → .dropdown_menu(|menu, ..| menu.menu(...))   // 右半部分要弹的菜单
  → RenderOnce::render(self)
      1. 渲染主按钮：应用 variant/size/outline 等，并把右侧圆角去掉（与箭头按钮拼接）
      2. 若设置了 menu：再渲染一个 icon=ChevronDown 的 Button，
         用 .dropdown_menu_with_anchor(anchor, menu) 把菜单绑到它上面
      3. 两按钮放在一个 h_flex 里，看起来像一个整体
```

关键技巧：`DropdownButton` 通过设置两个子按钮各自的 `border_corners`，让左按钮「右边圆角失效」、右按钮「左边圆角失效」，于是中间拼接处是直角，外观浑然一体。当变体是 `Ghost` 且未选中时，它还会保留分隔处的圆角（见源码 `let rounded = self.variant.is_ghost() && !self.selected;`）。

#### 4.3.3 源码精读

**结构体**：`DropdownButton` 持有一个可选的 `button: Option<Button>`（主按钮）和一个可选的 `menu` 构造闭包，见 [crates/ui/src/button/dropdown_button.rs:16-34](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/dropdown_button.rs#L16-L34)。注意 `menu` 的类型是 `Fn(PopupMenu, &mut Window, &mut Context<PopupMenu>) -> PopupMenu`，即「给我一个空菜单，我返回配置好的菜单」。

**setter**：`button(...)` 设置主按钮，`dropdown_menu(...)` / `dropdown_menu_with_anchor(...)` 设置菜单及弹出锚点（默认 `Anchor::TopRight`），见 [crates/ui/src/button/dropdown_button.rs:63-87](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/dropdown_button.rs#L63-L87)。

**render：拼装两个 Button**：先用 `rounded` 决定拼接处是否保留圆角，再分别渲染主按钮（去右侧圆角）和箭头按钮（`Button::new("popup").icon(IconName::ChevronDown)`，去左侧圆角并把菜单绑上去），见 [crates/ui/src/button/dropdown_button.rs:156-216](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/dropdown_button.rs#L156-L216)。

可以看到，`DropdownButton` 几乎所有样式属性（`variant/size/outline/compact/loading/disabled/selected`）都被**下发给两个内部 `Button`**，这就是它能「看起来像一个按钮」的原因。

#### 4.3.4 代码实践

**实践目标**：构造一个「保存 + 下拉」按钮，验证它由两段拼成，且点击箭头能弹出菜单。

**操作步骤**：

1. 创建 `DropdownButton::new("save")`，`.primary().large()`。
2. 用 `.button(Button::new("save-main").label("Save").on_click(...))` 设主按钮。
3. 用 `.dropdown_menu(|menu, window, cx| menu.menu("Save As", Box::new(...)).menu("Export", Box::new(...)))` 配菜单项（菜单项 action 的具体写法见 u5-l4，此处先用最简结构）。

参考写法（示例代码）：

```rust
// 示例代码（菜单 action 的完整写法见 u5-l4 菜单系统）
DropdownButton::new("save-dd")
    .primary()
    .button(Button::new("save-main").label("Save").on_click(|_, _, _| {
        println!("save clicked");
    }))
    .dropdown_menu(|menu, _window, _cx| {
        menu.menu("Save As", Box::new(SaveAsAction)).menu("Export", Box::new(ExportAction))
    })
```

**需要观察的现象**：整个按钮外观是一个整体（主色实心），右侧有一个朝下小箭头；点左侧文字区触发「Save」打印；点右侧箭头弹出一个含「Save As / Export」的菜单。

**预期结果**：点击主按钮区终端打印 `save clicked`；点击箭头弹出菜单。运行结果待本地验证（菜单 action 需自定义，可先用 `button_story` 之外、含 `DropdownMenu` 的 story 对照）。

#### 4.3.5 小练习与答案

**练习 1**：`DropdownButton` 的两个子按钮是如何做到「拼接处没有重复圆角/边框」的？

**参考答案**：在 `render` 里，主按钮通过 `border_corners` 把 `top_right/bottom_right` 设为 `rounded`（Ghost 未选中时为 true，否则 false，即去掉右侧圆角），箭头按钮则把 `top_left/bottom_left` 设为 `rounded`，并各自用 `border_edges` 控制边线（见 [dropdown_button.rs:164-213](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/dropdown_button.rs#L164-L213)）。这复用了 `Button` 暴露的 `pub(crate)` 的 `border_corners/border_edges` 方法。

**练习 2**：为什么 `DropdownButton` 也要实现 `ButtonVariants` 和 `Sizable`？

**参考答案**：因为它内部是两个 `Button`，统一在 `DropdownButton` 这一层调 `.primary()` / `.large()`，可以把变体和尺寸**一次性下发给两个子按钮**（见 [dropdown_button.rs:138-143](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/dropdown_button.rs#L138-L143) 与 render 内的 `.with_size(self.size).with_variant(self.variant)`），避免用户分别配置两个按钮，保持 API 一致性。

---

### 4.4 Toggle：可勾选的二态按钮

#### 4.4.1 概念说明

`Toggle` 是一个「按一下在 开/关 之间切换」的按钮。与普通 `Button` 的区别在于：

- 它持有 `checked: bool` 状态语义，选中时使用「强调色」背景。
- 它的 `on_click` 回调参数是 `&bool`（新的选中态），而不是 `&ClickEvent`。
- 它只有两种变体：`Ghost`（默认，无背景描边）和 `Outline`（带描边）。

`Toggle` 适合做「可独立开关的功能按钮」，如工具栏里的「字数统计开关」。如果需要一组互斥或联动的开关，用配套的 `ToggleGroup`。

#### 4.4.2 核心流程

```text
Toggle::new(id)
  → .checked(bool) .on_click(|checked: &bool, ..|)
  → RenderOnce::render(self)
      1. 计算 hoverable = !disabled && !checked（选中后不再 hover 变色）
      2. 按尺寸设最小宽/高/内边距（与 Button 类似）
      3. Outline 变体时画边框 + 背景；Ghost 变体不画边框
      4. 若 checked：套强调色背景（tokens.accent）+ 强调前景色
      5. 挂 on_click：回调里把 !checked（翻转后的新值）传给用户
```

注意第 5 步：`Toggle` 在内部把「翻转」做好了——回调收到的是 `&!checked`，即点击后的新状态。但 `Toggle` 自身**不保存**这个新状态，仍需外层 View 在回调里更新 `checked` 字段并回填。

`ToggleGroup` 的逻辑与 `ButtonGroup` 高度同构：用 `Rc<Cell<Option<usize>>>` 记录被点下标，`on_click` 回调返回 `&Vec<bool>`（每个 Toggle 的新选中态）；`segmented()` 可让组内 Toggle 拼成分段控件（segmented control）。

#### 4.4.3 源码精读

**ToggleVariant**：只有 `Ghost`（默认）和 `Outline`，配套 `ToggleVariants` trait，见 [crates/ui/src/button/toggle.rs:14-32](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/toggle.rs#L14-L32)。注意它和 `Button` 的 `ButtonVariants` 是**两套独立的 trait**（变体空间不同），所以 `Toggle` 没有 `.primary()`。

**结构体**：`Toggle` 持有 `checked`、`variant`、`on_click: Fn(&bool, ..)` 等，见 [crates/ui/src/button/toggle.rs:34-47](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/toggle.rs#L34-L47)。

**核心 setter**：`checked(...)` 设状态，`on_click(handler)` 设回调（参数 `&bool`），`label`/`icon` 走 children，见 [crates/ui/src/button/toggle.rs:78-104](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/toggle.rs#L78-L104)。

**render**：`hoverable = !disabled && !checked`；选中态套 `tokens.accent` 背景 + `accent_foreground` 文字；点击时回调传入翻转后的 `&!checked`，见 [crates/ui/src/button/toggle.rs:150-208](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/toggle.rs#L150-L208)。关键两段：

- 选中套色：[toggle.rs:196-199](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/toggle.rs#L196-L199)
- 翻转回调：[toggle.rs:202-205](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/toggle.rs#L202-L205) `this.on_click(move |_, window, cx| on_click(&!checked, window, cx))`

**ToggleGroup + segmented**：`ToggleGroup::on_click` 回调返回 `&Vec<bool>`；`segmented()` 去掉默认间距并把相邻 Toggle 的边框拼成一条整体描边，见 [crates/ui/src/button/toggle.rs:299-379](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/toggle.rs#L299-L379)。

#### 4.4.4 代码实践

**实践目标**：实现一个「夜间模式」开关 `Toggle`，验证二态切换与状态外置。

**操作步骤**：

1. View 持有 `dark_mode: bool`。
2. 渲染 `Toggle::new("dark").label("Night Mode").checked(self.dark_mode).outline()`。
3. `on_click` 回调里 `view.dark_mode = *checked; cx.notify();`（`checked` 即新状态）。

参考写法（示例代码）：

```rust
// 示例代码
Toggle::new("dark-mode")
    .label("Night Mode")
    .outline()
    .checked(self.dark_mode)
    .on_click(cx.listener(|view, checked: &bool, _, cx| {
        view.dark_mode = *checked;
        cx.notify();
    }))
```

**需要观察的现象**：未选中时是描边浅色；点击后变为强调色实心并显示「已开启」视觉；再次点击回到描边态。

**预期结果**：每点击一次，`view.dark_mode` 在 `true/false` 间翻转，UI 同步切换选中配色。运行结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`Toggle::on_click` 的回调参数为什么是 `&bool` 而不是 `&ClickEvent`？这个 `bool` 代表「点击前」还是「点击后」的状态？

**参考答案**：因为 `Toggle` 的语义是「切换状态」，回调最关心的是「切换成了什么」，所以直接给新状态。这个 `bool` 是**点击后**的值——`render` 里在挂 `on_click` 时传的是 `&!checked`（见 [toggle.rs:202-205](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/toggle.rs#L202-L205)），即把旧值取反后传出。

**练习 2**：`Toggle` 和「设了 `selected(true)` 的 `Button`」外观上都能高亮，本质区别是什么？

**参考答案**：`Button::selected` 只是一个「展示态」，按钮不会自动在选中/未选中间翻转，需外层手动维护；而 `Toggle` 把「点击即翻转」内建进了 `on_click`（自动传 `!checked`），并且选中色固定用 `tokens.accent`（见 [toggle.rs:196-199](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/toggle.rs#L196-L199)），变体空间也只有 Ghost/Outline 两档。简言之：`Toggle` 是「自带翻转语义的二态控件」，`Button::selected` 是「被动高亮的动作按钮」。

## 5. 综合实践

把本讲四个组件串起来，实现一个「操作栏」：

> 场景：一个内容编辑器的底部操作栏，包含一组排版按钮、一个保存下拉按钮、一个夜间模式开关。

要求：

1. 用 `ButtonGroup`（`outline().compact()`）放一组单选的「字号 小/中/大」按钮（用 4.2 的写法），选中态外置到 View。
2. 用 `DropdownButton`（`primary()`）做「保存」按钮：主按钮区打印 `saving...`，下拉菜单含 `Save As` 和 `Export` 两项（菜单 action 可先用空 `Box::new(...)` 占位，完整写法见 u5-l4）。
3. 用 `Toggle`（`outline()`）做「夜间模式」开关，切换时打印新状态。
4. 把三者放进一个 `h_flex().gap_3()`，观察整体排版。

参考骨架（示例代码，省略 action 定义与 View 实现）：

```rust
// 示例代码
h_flex().gap_3()
    .child(
        ButtonGroup::new("size-group").outline().compact()
            .child(Button::new("sm").label("S").selected(self.font_size == Size::Small))
            .child(Button::new("md").label("M").selected(self.font_size == Size::Medium))
            .child(Button::new("lg").label("L").selected(self.font_size == Size::Large))
            .on_click(cx.listener(|v, c: &Vec<usize>, _, cx| {
                v.font_size = match c.first() { Some(0) => Size::Small, Some(2) => Size::Large, _ => Size::Medium };
                cx.notify();
            })),
    )
    .child(
        DropdownButton::new("save-dd").primary()
            .button(Button::new("save-main").label("Save").on_click(|_, _, _| println!("saving...")))
            .dropdown_menu(|menu, _, _| menu.menu("Save As", Box::new(SaveAsAction)).menu("Export", Box::new(ExportAction))),
    )
    .child(
        Toggle::new("dark").label("Night").outline().checked(self.dark)
            .on_click(cx.listener(|v, on: &bool, _, cx| { v.dark = *on; println!("dark={on}"); cx.notify(); })),
    )
```

**验收**：

- 字号组三个按钮连体且单选，点击切换高亮。
- 保存按钮主区点击打印 `saving...`，右侧箭头弹出菜单。
- 夜间开关点击在两态间切换并打印 `dark=true/false`。

运行结果待本地验证（需在带显示环境执行 `cargo run`，并补全 action 类型）。也可先运行 `cargo run` 打开 Story Gallery，搜索 `Button` 页面，对照 [crates/story/src/stories/button_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/button_story.rs) 观察每个开关（Disabled/Loading/Selected/Compact/Shadow 复选框）对各种按钮的影响。

## 6. 本讲小结

- `Button` 是无状态组件，三套正交样式开关：**变体**（`ButtonVariant` 决定语义角色）、**outline**（浅色描边版）、**size**（`Sizable` 四档）。
- 按钮颜色由 `ButtonVariant` 上的 `normal/hovered/active/selected/disabled` 五个方法计算，每个返回一组 `{bg, border, fg, underline, shadow}`；`outline` 态用主题色低透明度（0.1/0.2/0.4）稀释背景。
- `clickable()` 要求「未禁用、未加载、且设了 `on_click`」；没有 `on_click` 的按钮不响应点击但仍可被 `selected` 高亮。
- `ButtonGroup` 把多个 `Button` 拼成连体一组，通过裁剪相邻按钮的 `border_corners/border_edges` 去掉中间圆角，并提供单选/多选语义（`on_click` 返回 `&Vec<usize>`）；选中态由外层 View 持有。
- `DropdownButton` 是「主按钮 + ChevronDown 箭头按钮」的拼装，两段通过互抵的圆角拼接成整体，箭头按钮挂载 `DropdownMenu`。
- `Toggle` 是自带「点击翻转」语义的二态按钮，`on_click` 回调直接给点击后的新状态 `&bool`；配套 `ToggleGroup`（可 `segmented()`）管理一组开关。
- 四个组件都遵循「能力以 trait 暴露（`Sizable/Disableable/Selectable/ButtonVariants`）、状态外置到 View」的库约定。

## 7. 下一步学习建议

- **横向扩展展示组件**：继续第 3 单元，下一篇 u3-l2「文本展示：Label / Tag / Badge / Separator」会讲轻量展示组件，它们常与按钮搭配出现在卡片、工具栏里。
- **进入表单**：u4-l2「Switch / Checkbox / Radio」会讲另一类二态/多态控件，可与本讲的 `Toggle`、`ButtonGroup`（多选）对比，理解「按钮型选择」与「表单型选择」的差异。
- **深入菜单**：本讲 `DropdownButton` 用到的 `DropdownMenu` / `PopupMenu` 在 u5-l4「菜单系统」详讲，学完后可回头把综合实践里的菜单 action 补全。
- **源码延伸阅读**：想看「五态样式」的完整配色表，可精读 [crates/ui/src/button/button.rs:663-1136](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L663-L1136)；想看 `ButtonIcon` 如何让图标位置支持 Spinner/进度，可读 [crates/ui/src/button/button_icon.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button_icon.rs)。
