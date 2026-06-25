# 折叠与分组：Collapsible / Accordion / GroupBox

## 1. 本讲目标

本讲讲解 gpui-component 中「折叠」与「分组」两类容器组件。读完本讲，你应当能够：

- 用 `Collapsible` 实现一段内容的「展开 / 收起」，并理解它与普通 `div` 在「子元素显隐」上的本质区别。
- 用 `Accordion` / `AccordionItem` 把多个可折叠面板组织成一组，掌握「单展开 / 多展开」模式与「无状态组件如何配合 View 持久化展开状态」这一关键设计。
- 用 `GroupBox` 给一组相关内容套上一个带标题的分组外壳，区分 `Normal` / `Fill` / `Outline` 三种变体。
- 把三者组合起来，搭建一个真实的「设置面板」交互。

## 2. 前置知识

本讲依赖前面几讲已建立的概念，这里只做最简回顾：

- **无状态组件 `RenderOnce`**：派生 `IntoElement`、实现 `render(self)`，每帧重建、自身不持有跨帧状态（参见 u2-l2）。本讲的三个组件**全部**是无状态组件——它们把「展开到第几个」「是否折叠」这类状态完全外置给你的 View 持有。
- **`Sizable` 与 `Size`**：`Size` 枚举（`XSmall`/`Small`/`Medium`/`Large`），`Sizable` 的 `with_size` 只存档位，真正翻译成像素发生在组件 `render` 内（参见 u2-l2）。
- **`cx.theme()` 取色**：颜色统一是 `Hsla`（参见 u2-l1）。本讲会用到几个组件专属的语义色：`cx.theme().tokens.accordion`、`cx.theme().tokens.accordion_hover`、`cx.theme().tokens.group_box`。
- **`ParentElement` 与 `Styled`**：实现 `ParentElement` 才能用 `.child(...)` 挂子元素；实现 `Styled` 才能像 `div()` 一样链式设置宽高、间距、背景等样式（参见 u2-l2）。
- **GPUI 事件冒泡**：子元素的 `on_click` 触发后，事件会继续冒泡到父元素的同名处理器。这一机制是 Accordion「子项点击 → 容器汇报」的关键，下面会细讲。

此外先建立两个直觉：

- **「显隐」≠「条件渲染」的语义差别**：`Collapsible` 关闭时是把内容元素**从渲染树里摘掉**（而不是用 `opacity:0` 或高度塌缩藏起来），所以关闭状态下内容完全不占布局、完全不参与绘制——这是它最朴素的实现方式。
- **「无状态 + 双向回写」的状态同步范式**：当组件本身不存状态，但又需要响应用户点击改变自身外观时，库的常见做法是「组件把用户操作的结果通过回调告诉你，由你存进自己的 View，再在下一次 `render` 时把新状态喂回去」。Acccordon 就是这个范式的典型。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/ui/src/collapsible.rs` | `Collapsible` 组件：通过区分 `Element` / `Content` 两类子节点，按 `open` 开关控制 `Content` 的显隐 |
| `crates/ui/src/accordion.rs` | `Accordion` 容器 + `AccordionItem` 单项；含「单/多展开」模式与「无状态 ↔ View」状态同步机制 |
| `crates/ui/src/group_box.rs` | `GroupBox` 分组容器 + `GroupBoxVariant`（Normal/Fill/Outline）变体 |
| `crates/ui/src/theme/theme_color.rs` | `accordion` / `accordion_hover` / `group_box` / `group_box_foreground` 等语义色定义 |
| `crates/story/src/stories/accordion_story.rs` 等 | 三个组件在 Gallery 中的真实用法示范 |

## 4. 核心概念与源码讲解

### 4.1 Collapsible（展开 / 收起容器）

#### 4.1.1 概念说明

`Collapsible` 是最朴素的「展开 / 收起」容器：它内部有一份子节点列表，当 `open = false` 时，其中被标记为「内容」的子节点**不渲染**，其余子节点（比如标题、切换按钮）始终渲染。典型用法是「一段摘要 + 一个『展开更多』按钮 + 被折叠的正文」。

它故意做得极简——没有内置的标题栏、没有内置的箭头图标、没有展开动画。**「点哪里触发折叠」「折叠按钮长什么样」完全由你决定**，`Collapsible` 只负责「按开关显示或隐藏内容」这一件事。这种「只给机制、不给皮肤」的设计，让它能嵌进任何自定义布局（卡片、列表项、段落……），这一点在 Gallery 里体现得很清楚。

#### 4.1.2 核心流程

```
Collapsible 持有 children: Vec<CollapsibleChild>
其中每个 child 是两种之一：
  Element(el)  —— 普通⼦元素，永远渲染（如标题、按钮）
  Content(el)  —— 内容⼦元素，仅当 open=true 时渲染
        │
        ▼
render 时 filter_map 遍历 children：
  是 Content 且 !open → 丢弃（None）
  否则                 → 保留（Some(el)）
        │
        ▼
用一个 v_flex() 把保留下来的元素纵向排起来
```

关键点：`.child(x)` 把 `x` 当作 `Element`（永远显示）；`.content(x)` 把 `x` 当作 `Content`（受 `open` 控制）。两者在 `render` 之前共存于同一个 `children` 列表里，**顺序就是你在代码里书写的顺序**。

#### 4.1.3 源码精读

`Collapsible` 用一个内部枚举区分两类子节点：

[crates/ui/src/collapsible.rs:7-16](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/collapsible.rs#L7-L16) — `CollapsibleChild` 有 `Element` 与 `Content` 两个变体，`is_content()` 用来在 `render` 时判断是否受开关控制。

组件本体只持有三个字段，没有任何跨帧状态：

[crates/ui/src/collapsible.rs:19-24](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/collapsible.rs#L19-L24) — `style`（样式 refinement）、`children`（子节点列表）、`open`（开关，默认 `false`）。

`.content()` 把元素包成 `Content` 变体入列：

[crates/ui/src/collapsible.rs:45-49](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/collapsible.rs#L45-L49) — 注意是 `push(Content(...))`，与下面的 `extend` 区分开。

而 `ParentElement::extend`（即 `.child(...)` 走的入口）则把元素包成 `Element` 变体：

[crates/ui/src/collapsible.rs:58-63](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/collapsible.rs#L58-L63) — `extend` 映射成 `CollapsibleChild::Element`，所以普通 `.child()` 挂的元素永远显示。

整个「折叠」逻辑浓缩在 `render` 的一个 `filter_map` 里：

[crates/ui/src/collapsible.rs:65-80](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/collapsible.rs#L65-L80) — 用 `v_flex()` 承载，遍历 `children`：若是 `Content` 且 `!self.open` 就返回 `None`（摘掉），否则返回该元素。**这就是「关闭即不渲染」的全部实现**，没有动画、没有过渡。

Gallery 的 `CollapsibleStory` 给出了标准用法——标题与切换按钮用 `.child()`（永远显示），正文用 `.content()`（受控），开关状态 `item1_open` 存在 View 里，按钮点击翻转它并 `cx.notify()` 触发重绘：

[crates/story/src/stories/collapsible_story.rs:72-105](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/collapsible_story.rs#L72-L105) — `.open(self.item1_open)` 喂回状态、`.content(...)` 放正文、按钮 `on_click` 里 `this.item1_open = !this.item1_open; cx.notify();`。

#### 4.1.4 代码实践

这是一个「源码阅读 + 断言验证」型实践（不需要运行）：

1. **目标**：验证你理解了「`.child()` 与 `.content()` 的区别」。
2. **步骤**：阅读上面的 `render` 实现，回答：如果把切换按钮（`Button`）误写成 `.content(button)` 而不是 `.child(button)`，当 `open=false` 时会发生什么？
3. **需要观察的现象**：`filter_map` 会因为该 child 是 `Content` 且 `!open` 而返回 `None`——**按钮也会被一起摘掉**，于是用户再也看不到、也点不到展开按钮，组件「锁死」在收起态。
4. **预期结果**：你能在代码层面解释「为什么触发折叠的控件必须用 `.child()`、被折叠的内容才用 `.content()`」——前者是常驻控件，后者是受控内容。

#### 4.1.5 小练习与答案

**练习 1**：`Collapsible` 关闭时，内容是「看不见但占位置」还是「完全不占位置」？
**答案**：完全不占位置。关闭时 `Content` 子节点在 `filter_map` 阶段就被丢弃，根本不会进入渲染树，因此既不绘制也不参与布局计算（高度为 0）。

**练习 2**：为什么 `Collapsible` 没有内置展开动画？
**答案**：因为它走的是「从渲染树摘除/挂回」的路径，元素要么在、要么不在，没有中间态可插值。若想要高度过渡动画，需要在内容容器外层自行用 GPUI 的 `with_animation` 包一层（参见 u3-l3 中 `Progress` 的动画写法）。

---

### 4.2 Accordion / AccordionItem（手风琴）

#### 4.2.1 概念说明

`Accordion`（手风琴）是「一列可折叠面板」：每个面板有自己的标题栏，点击标题栏展开或收起其内容。相比 `Collapsible`，它**内置了**标题栏、箭头图标（`ChevronDown`/`ChevronUp`）、悬停高亮、分隔线，并额外提供两个重要能力：

- **单展开 vs 多展开**：`.multiple(true)` 允许同时打开多个面板；默认 `false` 时打开一个会自动收起其他（经典手风琴行为）。
- **状态汇报回调**：`.on_toggle_click(|open_ixs: &[usize], ...|)` 在每次切换后把「当前打开面板的下标列表」回调给你。

它由两部分组成：`Accordion`（容器，管理一组面板与展开逻辑）与 `AccordionItem`（单个面板，含图标、标题、内容）。两者都实现 `Sizable`，因此整组手风琴可以统一调成 `XSmall`/`Small`/`Large` 等尺寸。

#### 4.2.2 核心流程

`Accordion` 自身是无状态 `RenderOnce`，但「哪些面板开着」必须跨帧保留。它用了一个精巧的「单帧状态 + 事件冒泡」机制把状态回写到你的 View：

```
render 开始 → 新建一个临时的 open_ixs: Rc<RefCell<HashSet<usize>>>（仅本帧有效）
        │
        ▼
遍历每个 AccordionItem：
  ① 若该 item 标记 .open(true) → 把它的下标塞进 open_ixs（用你喂回的状态播种）
  ② 给它挂一个内部 on_toggle_click：
       若要打开 → 非多展开时先 open_ixs.clear()，再 insert(ix)
       若要关闭 → open_ixs.remove(&ix)
  ③ 把整组尺寸/border/disabled 透传给 item
        │
        ▼
用户点击某面板标题栏（子元素 on_click）
  → 触发该 item 的内部 toggle（更新临时 open_ixs）
  → 事件冒泡到容器 on_click
  → 容器把 open_ixs 读出为 Vec<usize>，调用你注册的 on_toggle_click(open_ixs, ...)
        │
        ▼
你的回调把 open_ixs 存进 View（如 self.open_ixs = ...）并 cx.notify()
  → 下一帧 render 用 .open(self.open_ixs.contains(&ix)) 把新状态播种回去
```

也就是说，**持久展开状态必须由你的 View 持有并通过 `.open()` 喂回**；`Accordion` 内部那个 `HashSet` 只是为了在「一次点击」内算出「单展开模式下该收起谁」、并把结果汇报给你。如果你不接 `on_toggle_click`、也不用 `.open()` 喂回，点击面板将没有任何持久效果——这是使用 Accordion 最容易踩的坑。

#### 4.2.3 源码精读

`Accordion` 容器的字段里，「单/多展开」「尺寸」「边框」「禁用」都是正交开关：

[crates/ui/src/accordion.rs:13-21](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/accordion.rs#L13-L21) — 注意 `on_toggle_click` 的签名 `Arc<dyn Fn(&[usize], &mut Window, &mut App) + Send + Sync>`，回调参数就是「打开面板的下标切片」。

三个开关的默认值在 `new` 里设定：

[crates/ui/src/accordion.rs:25-35](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/accordion.rs#L25-L35) — `multiple` 默认 `false`（单展开）、`bordered` 默认 `true`、`disabled` 默认 `false`。

「单帧状态」机制是 `render` 的核心，分三步看。第一步：新建临时集合并用各 item 的 `open` 播种：

[crates/ui/src/accordion.rs:86-101](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/accordion.rs#L86-L101) — `open_ixs` 是本帧临时的；`if accordion.open { insert(ix) }` 就是用你喂回的 `.open()` 值初始化它。

第二步：给每个 item 挂内部 toggle，实现「单展开自动收起其他」：

[crates/ui/src/accordion.rs:107-120](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/accordion.rs#L107-L120) — `if *open { if !is_multiple { clear() } insert(ix) } else { remove(&ix) }`——这正是「单展开模式打开新面板时清空旧面板」的逻辑所在。

第三步：把你的 `on_toggle_click` 接到容器的 `on_click` 上，靠事件冒泡汇报结果：

[crates/ui/src/accordion.rs:123-133](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/accordion.rs#L123-L133) — 容器的 `on_click` 把 `open_ixs` 收集成 `Vec<usize>` 再调用你的回调；`.filter(|_| !self.disabled)` 保证禁用时回调不触发。

单个面板 `AccordionItem` 的字段与构造：

[crates/ui/src/accordion.rs:139-149](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/accordion.rs#L139-L149) — `index`（由容器在 render 时通过私有 `.index(ix)` 赋值）、`icon`、`title`、`children`（面板正文）、`open` 等。

面板正文靠 `ParentElement` 收集（`.child(...)` 即正文内容）：

[crates/ui/src/accordion.rs:208-212](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/accordion.rs#L208-L212) — `extend` 直接 push 进 `children`，这些就是展开后显示的内容。

面板自身的 `render` 先按尺寸算字号，再用主题色铺背景与边框：

[crates/ui/src/accordion.rs:222-239](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/accordion.rs#L222-L239) — `xs`/`small` 用 `0.875rem`、其余 `1.0rem`；背景取 `cx.theme().tokens.accordion`，`bordered` 时加 1px 边框与圆角。

标题栏的悬停高亮、箭头方向、点击翻转都在「未禁用」分支里：

[crates/ui/src/accordion.rs:277-295](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/accordion.rs#L277-L295) — 悬停 `bg(accordion_hover)`；箭头 `ChevronUp`(开)/`ChevronDown`(收)；`on_click` 调用内部 `on_toggle_click(&!self.open, ...)`，注意传的是**翻转后的值** `!self.open`。

展开时才渲染正文（与 Collapsible 同样是「不渲染而非隐藏」）：

[crates/ui/src/accordion.rs:297-308](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/accordion.rs#L297-L308) — `.when(self.open, ...)` 包裹正文容器，收起时正文不进渲染树。

Gallery 的 `AccordionStory` 完整示范了「View 持久化展开状态」的闭环：

[crates/story/src/stories/accordion_story.rs:58-61](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/accordion_story.rs#L58-L61) — `toggle_accordion` 把回调收到的 `open_ixs` 存进 `self.open_ixs` 并 `cx.notify()`。

[crates/story/src/stories/accordion_story.rs:168-173](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/accordion_story.rs#L168-L173) — 每个 item 用 `.open(self.open_ixs.contains(&0))` 把状态喂回，三者（`.open` 喂回、`on_toggle_click` 接收、`cx.notify` 重绘）缺一不可。

#### 4.2.4 代码实践

1. **目标**：亲手验证「不持久化状态会发生什么」，从而理解双向同步的必要性。
2. **操作步骤**：在 Gallery 中打开 `Accordion` 页（`cargo run` 后在左侧列表选 Accordion，或 `cargo run -- accordion`）。先在默认（`Multiple` 勾选）状态下点开几个面板，再用顶部 `Multiple` 复选框关掉多展开，再点击不同面板。
3. **需要观察的现象**：`Multiple` 开启时，多个面板可同时展开；关闭后变成单展开——点开新面板时旧面板自动收起。这一切能生效，是因为 Story 的 View 里存了 `open_ixs` 并在每次点击后被更新。
4. **预期结果**：你能口头复述「点击 → 内部 toggle 更新本帧集合 → 事件冒泡到容器 → 容器回调 `on_toggle_click(open_ixs)` → View 存 `open_ixs` 并 `notify` → 下帧用 `.open()` 喂回」这条链路。> 若无法本地运行，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`Accordion` 内部那个 `open_ixs: Rc<RefCell<HashSet<usize>>>` 能不能跨帧保留状态？
**答案**：不能。它在每次 `render` 开头被重新 `new` 出来，只活一帧。它的作用是「在单次点击内算出单展开模式下该收起哪些面板，并把最终打开列表汇报给你的回调」。真正跨帧的状态必须由你的 View（如 `Vec<usize> open_ixs`）持有。

**练习 2**：如果我把 `Accordion::new("a").item(...)` 写了好几个 item，却既不调 `on_toggle_click`、也不在 item 上调 `.open(...)`，点击面板会怎样？
**答案**：点击的瞬间，内部 toggle 与容器 `on_click` 仍会执行，但因为没有 `cx.notify()`（你不接回调就不会触发），也不会有新的 `render`，更没有新的 `.open()` 喂回——所以**面板视觉上不会改变展开状态**。这印证了「Accordion 是无状态组件，展开状态必须由调用方持有」。

**练习 3**：单展开模式下，为什么点开第 3 个面板时第 1 个会自动收起？
**答案**：见 [accordion.rs:107-120](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/accordion.rs#L107-L120) 的内部 toggle：`!is_multiple` 时先 `open_ixs.clear()` 再 `insert(ix)`，把集合清空只留新打开的下标，下一帧只有第 3 个面板被 `.open(true)` 喂回，第 1 个自然收起。

---

### 4.3 GroupBox（带标题的分组容器）

#### 4.3.1 概念说明

`GroupBox` 是一个「带可选标题的分组容器」：它把一组相关的控件圈在一个有边框/背景的外壳里，并在顶部放一个标题（如「Appearance」「Subscriptions」）。它**不折叠**——内容永远显示，纯粹用于视觉上的「归类」。

它通过 `GroupBoxVariant` 提供三种外观：

- **`Normal`（默认）**：无背景、无边框，只有一个标题 + 内容，最朴素。
- **`Fill`**：内容区有 `group_box` 主题色背景、有内边距，适合「卡片内的设置组」。
- **`Outline`**：内容区有 1px 边框、有内边距，适合「轻量分组」。

变体通过 `GroupBoxVariants` trait 暴露，所以可以直接链式调用 `.fill()` / `.outline()`，与库中其他组件的「变体 trait」约定一致（参见 u3-l1 按钮家族的 `ButtonVariants`）。

#### 4.3.2 核心流程

```
根据 variant 计算三元组 (bg, border, has_paddings)：
  Normal  → (None,   None,         false)
  Fill    → (group_box 背景, None,  true)
  Outline → (None,   border 色,    true)
        │
        ▼
外层 div（修复偶发的宽度未撑满问题）
  └─ v_flex
       ├─ has_paddings ? gap_3 : gap_4
       ├─ （可选）标题行：muted_foreground、行高 1.0
       └─ 内容容器 v_flex：
            bg（Fill 时）、border（Outline 时）
            text_color = group_box_foreground
            has_paddings ? p_4 : 无
            gap_4、rounded(theme.radius)
            → 渲染所有 children
```

`has_paddings` 同时影响「外层标题与内容的间距」和「内容容器是否有内边距」——也就是说，`Normal` 变体既无内边距、间距也更松（`gap_4`），整体更「透气」。

#### 4.3.3 源码精读

变体枚举与字符串互转（用于主题序列化等场景）：

[crates/ui/src/group_box.rs:10-16](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/group_box.rs#L10-L16) — `Normal` 为 `#[default]`；`from_str` 大小写不敏感，未知值回落 `Normal`。

`GroupBoxVariants` trait 提供 `.normal()`/`.fill()`/`.outline()` 便捷方法：

[crates/ui/src/group_box.rs:19-37](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/group_box.rs#L19-L37) — 与按钮家族同款的「变体 trait」风格，`GroupBox` 实现了它。

`GroupBox` 结构体把「整体样式」「标题样式」「内容样式」三套 refinement 分开存放，方便分别覆盖：

[crates/ui/src/group_box.rs:62-70](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/group_box.rs#L62-L70) — `style` / `title_style` / `content_style` 三套样式，加上 `title: Option<AnyElement>` 与 `children: SmallVec<[AnyElement; 1]>`（用 `SmallVec` 是因为绝大多数分组只有 1 个内容容器）。

`render` 的开头用模式匹配把变体翻译成 `(bg, border, has_paddings)`：

[crates/ui/src/group_box.rs:130-136](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/group_box.rs#L130-L136) — 一眼看清三种变体的视觉差异本质：Fill 给背景、Outline 给边框、两者都要内边距，Normal 啥都不给。

布局上先套一层 `div` 再放 `v_flex`，注释说明了原因：

[crates/ui/src/group_box.rs:139-145](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/group_box.rs#L139-L145) — 「Add `div` wrapper to avoid sometime width not full issue」——`v_flex` 直接做最外层有时宽度不撑满，多套一层 `div().child(...)` 规避。

标题行与内容容器分别构建：

[crates/ui/src/group_box.rs:146-165](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/group_box.rs#L146-L165) — 标题用 `muted_foreground`、`line_height(relative(1.))`；内容容器按变体加 `bg`/`border`，统一 `text_color(group_box_foreground)`、`gap_4`、`rounded(theme.radius)`，`has_paddings` 时 `p_4`。

`GroupBox` 既实现 `ParentElement`（挂内容）也实现 `Styled`（覆盖整体样式），还实现 `GroupBoxVariants`（选变体），三者让它的 API 与库中其他容器一致：

[crates/ui/src/group_box.rs:111-128](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/group_box.rs#L111-L128) — `extend`、`style`、`with_variant` 三个实现。

Gallery 用 `Fill` 与 `Outline` 两种变体分别承载「开关组」与「单选组」，是最直观的对照：

[crates/story/src/stories/group_box_story.rs:73-107](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/group_box_story.rs#L73-L107) — `.fill().title("...")` 里放若干 `Switch`，`.outline().title("Appearance")` 里放一个 `RadioGroup`，体现「GroupBox 只负责分组外壳，内容随意」。

`title_style` / `content_style` 允许精细覆盖，Gallery 的「Custom style」段演示了把标题加粗、内容区改圆角与双边框：

[crates/story/src/stories/group_box_story.rs:119-145](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/group_box_story.rs#L119-L145) — `title_style(StyleRefinement::default().font_semibold()...)` 与 `content_style(...rounded_xl().border_2()...)`。

#### 4.3.4 代码实践

1. **目标**：用同一组子内容，对比三种变体的视觉差异。
2. **操作步骤**：阅读 Gallery 的 `GroupBoxStory`（链接见上），它已经并排给出了 `Default`（Normal）、`Fill`、`Outline` 三段。在本地运行 Gallery 定位到 `GroupBox` 页观察。
3. **需要观察的现象**：`Normal` 无边无背景，只靠标题和间距分组；`Fill` 内容区有淡色背景与内边距，整体像「浅色卡片」；`Outline` 内容区只有边框，更「轻」。
4. **预期结果**：你能说出三者区别的本质来自 `render` 开头那个三元组——`Fill` 多了背景、`Outline` 多了边框、两者多了内边距，仅此而已。> 若无法本地运行，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`GroupBox` 与 `Collapsible` / `Accordion` 的本质区别是什么？
**答案**：`GroupBox` 不折叠——内容永远显示，只负责视觉分组（标题 + 外壳）；`Collapsible` / `Accordion` 能折叠——收起时内容不进渲染树。因此「需要收起」用后两者，「只需要归类」用 `GroupBox`。

**练习 2**：为什么 `GroupBox` 的 `children` 用 `SmallVec<[AnyElement; 1]>` 而不是 `Vec`？
**答案**：`SmallVec<[T; 1]>` 在元素数 ≤ 1 时直接内联存储（无堆分配），而一个 `GroupBox` 的内容容器几乎总是只有 1 个子元素（你的内容），用 `SmallVec` 能省掉一次堆分配，是针对实际使用模式的微型优化。

**练习 3**：如何让标题文字加粗、并把内容区圆角改大？
**答案**：用 `.title_style(StyleRefinement::default().font_semibold())` 覆盖标题样式，用 `.content_style(StyleRefinement::default().rounded_xl())` 覆盖内容样式——Gallery 的「Custom style」段正是范例。

---

## 5. 综合实践

把本讲三个组件串起来，搭建一个「设置面板」：

> 用 `Accordion` 把「通知」「外观」「高级」三组设置归类（单展开模式）；其中「高级」这一组用 `Collapsible` 实现「默认折叠」的二级展开（例如默认收起的危险操作区）；整组设置外面包一个 `GroupBox::new().outline().title("Preferences")` 作为分组外壳。「外观」面板里放一个 `RadioGroup` 选主题，「通知」面板里放几个 `Switch`。

下面是关键实现思路（**示例代码**，省略 import 与部分无关细节，聚焦状态同步）：

```rust
// 示例代码：演示「Accordion 状态外置 + Collapsible 默认折叠 + GroupBox 外壳」的组合，非项目原有代码
pub struct SettingsStory {
    focus_handle: gpui::FocusHandle,
    open_ixs: Vec<usize>,        // Accordion 当前打开的面板下标
    advanced_open: bool,         // Collapsible（高级区二级展开）默认 false
}

impl SettingsStory {
    fn new(_: &mut gpui::Window, cx: &mut gpui::Context<Self>) -> Self {
        Self {
            focus_handle: cx.focus_handle(),
            open_ixs: vec![0],   // 默认只展开第 0 个「通知」面板
            advanced_open: false, // Collapsible 默认折叠，满足「至少一个默认折叠」
        }
    }
}

impl Render for SettingsStory {
    fn render(&mut self, _window, cx) -> impl IntoElement {
        GroupBox::new()
            .outline()
            .title("Preferences")
            .child(
                Accordion::new("settings")
                    .multiple(false)                       // 单展开
                    .item(|this| {
                        this.open(self.open_ixs.contains(&0))
                            .title("Notifications")
                            .child(/* 若干 Switch ... */)
                    })
                    .item(|this| {
                        this.open(self.open_ixs.contains(&1))
                            .title("Appearance")
                            .child(/* RadioGroup 选主题 ... */)
                    })
                    .item(|this| {
                        // 第 2 个面板内部再用 Collapsible 做二级折叠
                        this.open(self.open_ixs.contains(&2))
                            .title("Advanced")
                            .child(
                                Collapsible::new()
                                    .open(self.advanced_open)
                                    .child(Label::new("Danger zone (click to reveal)"))
                                    .child(
                                        Button::new("reveal")
                                            .label(if self.advanced_open { "Hide" } else { "Show" })
                                            .on_click(cx.listener(|this, _, _, cx| {
                                                this.advanced_open = !this.advanced_open;
                                                cx.notify();
                                            })),
                                    )
                                    .content(/* 危险操作，如「删除账户」按钮 */),
                            )
                    })
                    // 关键：接住 Accordion 汇报的打开下标，存回 View 并重绘
                    .on_toggle_click(cx.listener(|this, open_ixs: &[usize], _, cx| {
                        this.open_ixs = open_ixs.to_vec();
                        cx.notify();
                    })),
            )
    }
}
```

**操作步骤**：

1. 仿照 `hello_world`（u1-l4）搭一个最小窗口与 `Root`，把上面的 `SettingsStory` 作为窗口内容。
2. 确保 `Accordion` 的三项闭环齐全：`.open(self.open_ixs.contains(&ix))` 喂回、`.on_toggle_click` 接收、回调内 `cx.notify()`。
3. 在「Advanced」面板里嵌入 `Collapsible`，并让 `advanced_open` 默认 `false`。

**需要观察的现象**：单展开模式下，点开「Appearance」时「Notifications」自动收起；「Advanced」面板内点「Show」才展开危险操作区，再点「Hide」收起——这是「Accordion 一级折叠 + Collapsible 二级折叠」的嵌套效果。

**预期结果**：你完成了一个把「Accordion 归类 + Collapsible 默认折叠 + GroupBox 分组外壳」三者协同的真实设置面板，并亲手验证了「无状态组件的状态必须由 View 持有并双向同步」这一贯穿本讲的核心约定。

> 如果无法本地运行，标注「待本地验证」。

## 6. 本讲小结

- 三个组件**全部是无状态 `RenderOnce` 组件**，跨帧状态都由外层 View 持有——这是 gpui-component 一贯的约定（承接 u2-l2）。
- `Collapsible` 用 `CollapsibleChild` 枚举区分两类子节点：`.child()` 挂的是 `Element`（永远显示），`.content()` 挂的是 `Content`（受 `open` 控制）；关闭时 `Content` 在 `render` 的 `filter_map` 阶段被直接摘除，**既不绘制也不占布局**。
- `Accordion` 自身不存展开状态，而是用「单帧临时 `HashSet` + 事件冒泡 + `on_toggle_click` 回调」把打开下标汇报给你的 View，由你存起来并用 `.open(...)` 下帧喂回——「`.open()` 喂回 + `on_toggle_click` 接收 + `cx.notify()`」三者缺一不可；它还内置了 `ChevronUp/Down` 箭头、悬停高亮、单/多展开模式。
- `GroupBox` 不折叠，只分组：`Normal`（无框无底）/`Fill`（有 `group_box` 背景与内边距）/`Outline`（有边框与内边距）三变体，本质区别全在 `render` 开头的 `(bg, border, has_paddings)` 三元组；并提供 `title_style` / `content_style` 精细覆盖。
- 选型原则：**需要折叠**用 `Collapsible`（单块）或 `Accordion`（成组、要标题栏/箭头/单展开模式）；**只需视觉归类**用 `GroupBox`。

## 7. 下一步学习建议

- 本讲的「无状态组件 + View 持有状态 + 双向回调同步」范式，在接下来的 **u4（表单与基础输入）** 中会反复出现——例如 `Switch` / `Checkbox` / `Radio` / `Input` 的 `checked` / `value` 同样要由你的 View 持有并通过回调更新。建议带着这个视角进入 u4-l2（开关选择组件）。
- 若你想深入 GPUI 的事件冒泡与 `cx.listener` / `cx.notify` 机制，可阅读 GPUI skill 中 events / context / entity state 的部分——它是本讲 Accordion 状态同步能成立的底层原因。
- `Accordion` / `GroupBox` 常作为「设置面板」「详情卡片」的骨架，后续学习 **u5（浮层与导航）** 的 `Tab` / `Sidebar` 时，可与本讲组件组合出更完整的应用主框架。
