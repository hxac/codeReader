# 文本展示：Label / Tag / Badge / Separator

## 1. 本讲目标

本讲讲解 gpui-component 中四类「轻量文本与状态展示」组件。学完后你应当能够：

- 用 `Label` 展示主文本、副文本、脱敏文本，并对匹配片段做高亮。
- 用 `Tag` 给内容打上分类/状态标签，并理解它的「变体 + outline + 尺寸 + 圆角」四套正交开关。
- 用 `Badge` 给头像/图标叠加数字、圆点或图标角标。
- 用 `Separator` 画水平/垂直、实线/虚线的分隔线，并能带文字标签。

这四个组件都很小，但它们是几乎所有复杂界面（卡片、列表、表头、导航）的「拼图零件」。理解它们，等于掌握了库中「无状态展示组件」的通用设计范式。

## 2. 前置知识

在进入源码前，先回顾几个来自前序讲义的关键概念（详见 [u2-l2 样式系统](./u2-l2-styled-and-sizable.md)）：

- **`RenderOnce` 与无状态组件**：派生 `IntoElement`、实现 `render(self, window, cx)`，每帧用 `self` 重建一棵元素树，不持有跨帧状态。本讲的四个组件都是无状态组件。
- **`Styled` trait**：提供 `div().flex().gap_2()…` 这类链式样式方法，内部把修改写进一个 `StyleRefinement`（样式增量记录）。组件只要把 `Styled` 暴露出来，用户就能用 `Styled` 的全部方法继续链式定制。
- **`Sizable` trait**：统一支持 `xs/sm/md/lg` 四档尺寸，靠 `Size` 枚举与 `with_size` 方法（以及 `small()/large()` 等便捷方法）实现。
- **`ParentElement` trait**：让一个元素能容纳子元素，从而支持 `.child(...)` 和 `.children(...)`。`Tag`、`Badge` 都实现了它。
- **`cx.theme()` 与语义化颜色**：通过 `ActiveTheme` trait 取主题色，如 `cx.theme().foreground`、`cx.theme().border`、`cx.theme().primary`（详见 [u2-l1 主题系统](./u2-l1-theme-system.md)）。

下面用到的几个 GPUI 原生类型也简单说明：

| 类型 | 含义 |
|------|------|
| `SharedString` | GPUI 的引用计数字符串，clone 廉价，被组件大量使用 |
| `Hsla` | 色彩类型（色相/饱和度/亮度/透明度），主题色的统一表示 |
| `AbsoluteLength` | GPUI 的绝对长度（像素），`rounded(px(4.))` 等接受它 |
| `StyledText` | 带「高亮区间」的文本元素，是 `Label` 的渲染底座 |

## 3. 本讲源码地图

四个组件各自一个文件，都是 `crates/ui/src/` 下的单文件模块，并在 [lib.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/lib.rs) 中以 `pub mod` 暴露：

| 文件 | 组件 | 作用 | `pub mod` 位置 |
|------|------|------|----------------|
| `crates/ui/src/label.rs` | `Label` | 文本展示，支持副文本/脱敏/高亮 | [lib.rs:47](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/lib.rs#L47) |
| `crates/ui/src/tag.rs` | `Tag` | 小型状态/分类标签 | [lib.rs:75](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/lib.rs#L75) |
| `crates/ui/src/badge.rs` | `Badge` | 角标（数字/圆点/图标） | [lib.rs:28](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/lib.rs#L28) |
| `crates/ui/src/separator.rs` | `Separator` | 水平/垂直分隔线 | [lib.rs:63](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/lib.rs#L63) |

引用路径分别为 `gpui_component::label::Label`、`gpui_component::tag::Tag`、`gpui_component::badge::Badge`、`gpui_component::separator::Separator`。运行期可对照查看 Story Gallery 中的 `Label`、`Tag`、`Badge`、`Separator` 四个演示页（分别对应 `crates/story/src/stories/label_story.rs` 等）。

---

## 4. 核心概念与源码讲解

### 4.1 Label：基础文本展示

#### 4.1.1 概念说明

`Label` 是库中最基础的文本元素。它解决的问题很简单——「在界面上画一段文字」，但又在此基础上叠加了三个常见需求：

1. **副文本（secondary）**：在主文本后面跟一段说明性小字，颜色更淡（如「公司地址」后面跟「(可选)」）。
2. **脱敏（masked）**：把敏感数字（如金额）替换成等长的 `•`，常见于隐藏余额的场景。
3. **高亮（highlights）**：把文本中匹配搜索词的片段染成蓝色，常用于搜索/过滤列表。

它是一个典型的无状态 `RenderOnce` 组件，本身不存跨帧状态，所有「状态」由外层持有它的 View 控制。

#### 4.1.2 核心流程

`Label` 的渲染主链路是「拼文本 → 算高亮 → 交给 StyledText」：

```
new(label)
  └─ render(self, cx)
       ├─ full_text()          // 主文本 + " " + 副文本（若有）
       ├─ 若 masked → 用 •×字符数 替换
       ├─ measure_highlights() // 计算需要染色的字节区间
       └─ div().text_color(foreground)
            .child(StyledText::new(text).with_highlights(ranges))
```

高亮算法的直觉是：在「整段拼接后的文本」上做**大小写不敏感**的子串查找，把每个命中片段记录成一段字节区间 `Range<usize>`，再由 GPUI 的 `StyledText` 按区间染色。匹配模式有两种（见 [label.rs:12-17](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/label.rs#L12-L17)）：

- `Full`：找出**所有**出现位置（含重叠）。
- `Prefix`：仅当整段文本以该词**开头**时，高亮开头那一段。

> 注意区间单位是**字节**（byte）而非字符。这对 ASCII 无影响，但对中文等多字节字符很关键——源码里用 `is_char_boundary` 来保证不会把一个 UTF-8 字符从中间切开（见 [label.rs:132](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/label.rs#L132)）。

#### 4.1.3 源码精读

`Label` 的字段定义揭示了它的全部能力——一个样式增量、主文本、可选副文本、脱敏开关、可选高亮匹配：

[label.rs:52-59](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/label.rs#L52-L59) 定义了 `Label` 结构体，注意它派生了 `IntoElement`（无状态组件标志）并持有一个 `style: StyleRefinement` 字段用于承接用户链式样式。

构造器只要求一段主文本，其余能力默认关闭：

[label.rs:63-72](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/label.rs#L63-L72) 是 `Label::new`，把传入的任意 `impl Into<SharedString>` 转成主文本。

三个核心能力各对应一个 builder 方法，都是经典的「修改字段后返回 self」：

- [label.rs:76-79](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/label.rs#L76-L79) `secondary`：设置副文本。
- [label.rs:82-85](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/label.rs#L82-L85) `masked`：开启/关闭脱敏。
- [label.rs:88-91](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/label.rs#L88-L91) `highlights`：设置要高亮的匹配词。

`full_text` 负责把主文本和副文本拼成一段连续字符串（中间加空格），高亮查找就发生在这段拼接文本上：

[label.rs:93-98](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/label.rs#L93-L98) 拼接主文本与副文本。

真正的高亮区间计算在 `highlight_ranges`，区分 Prefix/Full 两种模式：

[label.rs:100-147](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/label.rs#L100-L147) 计算高亮区间，关键点：把文本与搜索词都 `to_lowercase()` 后比较实现大小写不敏感；Full 模式用循环找出所有出现位置，并用 `is_char_boundary` 跳过 UTF-8 字符中间。

最后，`measure_highlights` 把「文本区间」翻译成「区间 + 颜色」的染色指令——副文本染 `muted_foreground`，匹配片段染 `blue`：

[label.rs:149-185](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/label.rs#L149-L185) 把区间映射为 `HighlightStyle`，副文本用 `cx.theme().muted_foreground`，高亮用 `cx.theme().blue`。

`RenderOnce::render` 把上面所有东西组装成一棵元素树：

[label.rs:194-213](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/label.rs#L194-L213) 渲染逻辑：masked 时用 `•` 重复「字符数」次（注意是 `chars().count()` 而非字节数），默认行高 `rems(1.25)`，文本色取 `cx.theme().foreground`，最后用 `StyledText` 承载高亮。

#### 4.1.4 代码实践

这是一个**源码阅读 + 现象观察**型实践，对照 Story Gallery 的 Label 演示页。

1. **目标**：验证 `secondary`、`masked`、`highlights` 三项能力的实际效果。
2. **操作步骤**：
   - 用 `cargo run` 启动 Story Gallery，在左侧找到 `Label` 页。
   - 该页顶部有一个输入框和一个 `Prefix` 勾选框（对应 [label_story.rs:98-110](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/label_story.rs#L98-L110)）。
   - 在输入框输入 `AA`，观察 `AAA中文BB` 这一行（见 [label_story.rs:118](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/label_story.rs#L118)）的高亮表现——这条注释特别说明它曾是一个 CJK + ASCII 混排导致崩溃的 bug。
   - 点击 `Masked Label` 区块里的眼睛按钮，观察金额 `9,182,1 USD` 变成一串 `•`。
3. **需要观察的现象**：输入 `AA` 时，两处 `AA` 都被染蓝（Full 模式）；切换为 `Prefix` 后只高亮开头；mask 开启后圆点数量与原数字位数一致。
4. **预期结果**：高亮仅作用于字母数字，中文边界不被切断；脱敏点数 = 原文本字符数。

如果你想自己写最小调用，下面是「示例代码」（非项目原有，仿照 hello_world 风格）：

```rust
use gpui_component::{label::Label, v_flex};

// 渲染三行 Label
v_flex()
    .gap_2()
    .child(Label::new("Company Address").secondary("(optional)"))
    .child(Label::new("9,182,1 USD").masked(true))
    .child(Label::new("Hello World").highlights("world"))
```

> 运行结果需在真实窗口中验证（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`masked` 替换出的 `•` 个数是按「字符数」还是「字节数」计算的？为什么这样选？

> **答案**：按字符数（`text.chars().count()`，见 [label.rs:197-201](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/label.rs#L197-L201)）。因为脱敏要保证「视觉长度」一致——一个中文字符在 UTF-8 里占 3 字节但屏幕上只占一个字宽，用字符数才能让圆点数与肉眼看到的字数相符。

**练习 2**：为什么高亮区间用字节 `Range<usize>` 而不是字符索引？

> **答案**：GPUI 的 `StyledText`/`combine_highlights` 按字节偏移定位，与 Rust 字符串的底层表示一致。因此源码在推进搜索起点时必须用 `is_char_boundary` 防止落在一个多字节字符的中间（[label.rs:132-136](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/label.rs#L132-L136)），否则会产生无效的字符串切片而 panic。

---

### 4.2 Tag：小型状态/分类标签

#### 4.2.1 概念说明

`Tag` 是一个带边框、带背景色的小标签，用于给内容打上「状态」或「分类」，例如「已通过」「危险」「Admin」。它的设计哲学是：外观由几套**正交开关**组合而成，互不干扰：

- **变体（variant）**：决定语义角色与配色，如 `Primary/Danger/Success/Warning/Info/Secondary/Color/Custom`。
- **outline**：是否换成「透明背景 + 彩色描边」的轻量风格。
- **尺寸（size）**：通过 `Sizable` 控制 padding 与圆角（注意 Tag 实际只面向 `Small` 与 `Medium` 两档）。
- **圆角（rounded）**：可自定义圆角或 `rounded_full` 变成药丸形。

`Tag` 同时实现了 `ParentElement`，所以它的内容是「子元素」而非固定字符串——你可以往里放文字、图标，甚至任意元素。

#### 4.2.2 核心流程

```
Tag::new() 或 Tag::primary()/danger()/...
  └─ render(self, cx)
       ├─ bg     = outline ? 透明 : variant.bg(cx)
       ├─ fg     = variant.fg(outline, cx)
       ├─ border = variant.border(cx)
       ├─ rounded = 自定义 或 按 size 取 theme.radius(/2)
       └─ div().flex.items_center.border_1.text_xs
            .(按 size 设 padding).bg(bg).text_color(fg).border_color(border)
            .rounded(rounded).hover(opacity 0.9).children(...)
```

变体到颜色的映射集中在 `TagVariant` 的三个方法里：`bg`/`border`/`fg`，分别返回背景、边框、文字色。`Color(ColorName)` 变体还会根据 `cx.theme().is_dark()` 在亮/暗模式下取不同明度（如 `scale(50)` vs `scale(950)`），保证对比度。

#### 4.2.3 源码精读

变体枚举是 Tag 的「语义层」核心，包含 6 个预设角色外加两种自定义方式：

[tag.rs:9-24](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tag.rs#L9-L24) `TagVariant` 枚举，默认值是 `Secondary`；`Color(ColorName)` 用主题色板取色，`Custom { color, foreground, border }` 允许完全自定义三色。

三个方法把变体翻译成具体颜色。以背景为例：

[tag.rs:27-44](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tag.rs#L27-L44) `bg`：预设角色直接读 `cx.theme()` 的语义色；`Color` 变体按明暗模式分别取 `scale(50)`（亮）或 `scale(950).opacity(0.5)`（暗）。

文字色 `fg` 还要看 `outline` 参数——outline 时用「角色色本身」当文字色（背景透明），非 outline 时用「角色色的前景色」（如 `primary_foreground`）：

[tag.rs:65-118](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tag.rs#L65-L118) `fg`，每个分支都有 `if outline { 角色色 } else { *_foreground }` 两套。

`Tag` 结构体本身收集了上述所有开关，外加 `children` 容纳子元素：

[tag.rs:124-132](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tag.rs#L124-L132) `Tag` 结构体，文档注释明确「Only support: Medium, Small」。

构造方面，库提供了两类入口：通用 `new()` + `with_variant`，以及每个变体的快捷构造器：

[tag.rs:133-213](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tag.rs#L133-L213) `Tag` 的 impl 块，包含 `new`、`primary/danger/success/...` 等便捷构造、`outline`、`rounded`、`rounded_full`。

`Tag` 同时实现 `Sizable` 与 `ParentElement`，让尺寸与内容都可控：

[tag.rs:215-226](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tag.rs#L215-L226) `Sizable`（存 `with_size`）与 `ParentElement`（`extend` 把子元素塞进 `children`）。

`render` 把四套开关翻译成最终样式，固定 `text_xs`（小字号），按 size 分两档 padding/圆角：

[tag.rs:234-269](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tag.rs#L234-L269) 渲染逻辑：outline 时背景取 `transparent_white()`；圆角默认对 Small 取 `theme.radius/2`、其余取 `theme.radius`；悬停时整体 `opacity(0.9)` 提供轻量反馈。

#### 4.2.4 代码实践

对照 Story Gallery 的 Tag 演示页（[tag_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/tag_story.rs)）。

1. **目标**：理解「变体 × outline × 尺寸 × 圆角」的组合效果。
2. **操作步骤**：
   - 启动 Gallery，打开 `Tag` 页。
   - 依次观察「Tag (default)」「Tag (outline)」「Tag (small)」「Tag (rounded full)」「Tag (rounded 0px)」「Color Tags」六个区块。
   - 重点对比同一变体在 default 与 outline 下的差异（背景透明、文字变角色色）。
3. **需要观察的现象**：outline 模式下背景透明、文字与边框同为角色色；`Color Tags` 区块用 `ColorName::all()` 遍历生成一排彩色标签（见 [tag_story.rs:130-139](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/tag_story.rs#L130-L139)）。
4. **预期结果**：六种预设变体 + Color + Custom 能覆盖绝大多数状态标记需求。

下面是「示例代码」（仿 tag_story，非项目原有）：

```rust
use gpui_component::{tag::Tag, h_flex, indigo_50, indigo_500};

h_flex()
    .gap_2()
    .child(Tag::success().child("已通过"))
    .child(Tag::danger().outline().child("已拒绝"))
    .child(Tag::custom(indigo_500(), indigo_50(), indigo_500()).child("自定义"))
```

> 运行结果需在真实窗口中验证（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`outline()` 为什么会让背景变透明？

> **答案**：`render` 中 `bg = if self.outline { transparent_white() } else { self.variant.bg(cx) }`（[tag.rs:236-240](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tag.rs#L236-L240)）。outline 是一种「轻量强调」风格：去掉背景、保留彩色边框，并把文字色改为角色色本身（见 `fg` 的 `if outline` 分支），让标签在密集列表里不抢视觉。

**练习 2**：如何让一个 Tag 用主题色板里的某个颜色，但又不写死 RGB？

> **答案**：用 `Tag::color(color: impl Into<ColorName>)`（[tag.rs:185-188](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tag.rs#L185-L188)）。它会走 `TagVariant::Color`，在 `bg`/`border`/`fg` 里根据明暗模式自动选合适的明度（`scale(50/200/300/600/800/950)`），比 `Custom` 更省心、且能跟随主题切换。

---

### 4.3 Badge：角标（数字 / 圆点 / 图标）

#### 4.3.1 概念说明

`Badge` 用于在某个元素（通常是头像或图标）的角落叠加一个「角标」——可能是未读消息数、在线状态圆点，或一个表示状态的小图标（如对勾、星标）。它的关键设计是：

- 它本身是一个**相对定位容器**（`relative`），被装饰的内容作为子元素放入，角标则用**绝对定位**叠在角落。
- 角标有三种形态（`BadgeVariant`）：`Number`（数字，超上限显示 `N+`）、`Dot`（小圆点）、`Icon`（带边框的小图标）。

#### 4.3.2 核心流程

```
Badge::new().count(3).child(Avatar / Icon)
  └─ render(self, cx)
       ├─ visible = Number 时 count>0 才显示；Dot/Icon 始终显示
       ├─ (size, text_size) = 按 Size 分 Large/Medium/Small 三档
       └─ div().relative().children(self.children)
            .when(visible, |this| this.child(
                h_flex().absolute().rounded_full().bg(默认 red)
                    .match variant {
                        Dot    => 右上 6px 小圆点
                        Number => 右上数字，count>max 显示 "max+"
                        Icon   => 右下带边框图标
                    }
            ))
```

要点：`count(0)` 的 `Number` 徽标会自动隐藏（`visible = false`）；数字徽标超过 `max`（默认 99）会显示成 `99+`；徽标默认红色，但可用 `.color(...)` 覆盖。

#### 4.3.3 源码精读

变体枚举区分三种角标形态：

[badge.rs:8-14](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/badge.rs#L8-L14) `BadgeVariant`，默认 `Number`；`Icon` 用 `Box<Icon>` 存储图标。

`Badge` 结构体持有计数、上限、变体、可选颜色、子元素列表与尺寸：

[badge.rs:30-39](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/badge.rs#L30-L39) `Badge` 结构体，`max` 默认 99。

配置方法对应三种变体与颜色：

[badge.rs:55-85](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/badge.rs#L55-L85) `dot()`/`count(n)`/`icon(...)`/`max(n)`/`color(...)`，注意 `count` 的文档注释说明「count 为 0 时徽标隐藏」。

`render` 的第一步是判断角标是否可见，这是「count=0 不显示」的实现来源：

[badge.rs:101-112](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/badge.rs#L101-L112) 计算 `visible`，并按 `size` 决定徽标盒子的尺寸与文字字号（Large=24px、Medium=16px、Small=10px）。

随后用 `relative` 容器 + 绝对定位角标完成叠加，三种变体定位各不相同：

[badge.rs:114-164](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/badge.rs#L114-L164) 渲染主体：外层 `div().relative().children(...)`，再 `.when(visible)` 追加一个 `absolute` 角标；`Number` 变体里 `if self.count > self.max { format!("{}+", self.max) }` 实现「99+」效果，并按 `count.len()` 反向偏移 `right` 让多位数字居中（[badge.rs:130-153](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/badge.rs#L130-L153)）；`Dot` 固定右上 6px 圆点；`Icon` 放右下并加一圈背景色边框。

#### 4.3.4 代码实践

对照 Story Gallery 的 Badge 演示页（[badge_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/badge_story.rs)）。

1. **目标**：验证三种角标形态与 `max` 上限行为。
2. **操作步骤**：
   - 启动 Gallery，打开 `Badge` 页。
   - 观察「Badge with count」区块：`count(3)` 与 `count(103)` 两个头像（见 [badge_story.rs:70-79](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/badge_story.rs#L70-L79)）。103 超过默认 max=99，应显示 `99+`。
   - 观察「Badge with icon」与「Badge with dot」区块，对比 Icon（右下、带边框）与 Dot（右上、小圆点）的定位差异。
3. **需要观察的现象**：`count(103)` 显示为 `99+` 而非 `103`；Icon 徽标在右下角且有一圈背景色描边；Dot 徽标是右上角的小红点。
4. **预期结果**：与源码 [badge.rs:130-161](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/badge.rs#L130-L161) 中三个 `match` 分支的定位完全一致。

下面是「示例代码」（仿 badge_story，非项目原有）：

```rust
use gpui_component::{avatar::Avatar, badge::Badge, IconName};

// 未读消息数（会显示 99+）
Badge::new().count(120).child(
    Avatar::new().src("https://avatars.githubusercontent.com/u/5518?v=4"),
)
// 在线状态圆点，用绿色
Badge::new().dot().color(cx.theme().green).child(
    Avatar::new().src("https://avatars.githubusercontent.com/u/5518?v=4"),
)
```

> 运行结果需在真实窗口中验证（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Badge::new().count(0)` 时看不到角标？

> **答案**：`render` 中 `visible = match self.variant { Number => self.count > 0, Dot|Icon => true }`（[badge.rs:103-106](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/badge.rs#L103-L106)），再用 `.when(visible, …)` 决定是否追加角标子元素。所以 `Number` 变体 count 为 0 时角标整个不渲染——这符合「没有未读就不显示红点」的直觉。

**练习 2**：要让 100 条未读显示成 `99+`，需要做什么？

> **答案**：什么都不用做。`max` 默认就是 99（[badge.rs:47](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/badge.rs#L47)），渲染时 `if self.count > self.max { format!("{}+", self.max) }`（[badge.rs:131-135](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/badge.rs#L131-L135)）会自动把超过上限的数字显示成 `99+`。若想改成 `999+`，调用 `.max(999)` 即可。

---

### 4.4 Separator：分隔线

#### 4.4.1 概念说明

`Separator` 是一条分隔线，用于在视觉上切分组内容。它支持两个维度的选择：

- **方向**：`horizontal()`（水平）或 `vertical()`（垂直）。
- **线型**：`Solid`（实线，默认）或 `Dashed`（虚线）。

此外还能 `.label("文字")` 在线上叠加一段文字，形成「带标题的分隔线」。实线实现很直接（一个 1px 的填充条），但虚线实现值得一看——它用 GPUI 的 `canvas` 在绘制阶段画一条 `dash_array` 路径。

#### 4.4.2 核心流程

```
Separator::horizontal()/vertical()  (+ 可选 .dashed()/.label())
  └─ render(self, cx)
       ├─ color = self.color 或 cx.theme().border
       └─ self.base(flex + shrink_0 + 居中)
            .child( match line_style {
                Solid  => render_solid: 1px 绝对定位条 + bg(color)
                Dashed => render_dashed: canvas 画 dash_array [4,2] 路径
            })
            .when_some(label, |this, lbl| this.child(背景色遮罩的文字))
```

实线（`render_solid`）通过 `render_base` 造一个绝对定位、宽/高为 1px 的 `div`，再用背景色填充。虚线（`render_dashed`）则用 `canvas` 在布局完成后拿到 `bounds`，沿水平/垂直方向画一条 `dash_array(&[4px, 2px])` 的描边路径。

#### 4.4.3 源码精读

`SeparatorStyle` 区分实线与虚线：

[separator.rs:8-13](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/separator.rs#L8-L13) `SeparatorStyle`，默认 `Solid`。

`Separator` 持有一个 `base: Div`（构造时预置方向相关样式，如垂直时 `h_full()`）、一个样式增量、可选 label、轴向、可选颜色与线型：

[separator.rs:16-24](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/separator.rs#L16-L24) `Separator` 结构体。

构造器按「方向 × 线型」组合，并提供 `dashed`/`label`/`color` 配置：

[separator.rs:26-77](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/separator.rs#L26-L77) `vertical`/`horizontal`/`vertical_dashed`/`horizontal_dashed` 四个构造器，以及 `label`/`color`/`dashed` 方法。注意 `vertical()` 时 base 是 `div().h_full()`，这保证垂直线能撑满父容器高度。

实线渲染依赖一个公共的 1px 基底：

[separator.rs:79-88](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/separator.rs#L79-L88) `render_base` 造绝对定位的 1px 条（垂直时宽 1px 高满，水平时高 1px 宽满），`render_solid` 直接 `bg(color)` 填充。

虚线渲染是本组件最有「技术含量」的部分，用 `canvas` 在 paint 阶段画路径：

[separator.rs:90-117](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/separator.rs#L90-L117) `render_dashed`：用 `PathBuilder::stroke(px(1.)).dash_array(&[px(4.), px(2.)])` 建一条「4px 实线 + 2px 间隔」的描边路径；`canvas` 的第二个回调在布局后拿到 `bounds`，按轴向算出起点/终点（水平沿 x、垂直沿 y，各偏移 0.5px 居中到像素），最后 `window.paint_path(line, color)` 绘制。

`render` 把颜色、线型、可选 label 组装起来，并保证分隔线不会在 flex 布局里被挤压：

[separator.rs:126-155](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/separator.rs#L126-L155) 渲染主体：颜色默认 `cx.theme().border`；外层 `flex().flex_shrink_0()`（关键：`shrink_0` 防止线被压缩）；按 `line_style` 选 solid/dashed；`when_some(label)` 用 `bg(theme.tokens.background)` 的文字块居中遮盖线的中段，实现「带文字分隔线」。

#### 4.4.4 代码实践

对照 Story Gallery 的 Separator 演示页（[separator_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/separator_story.rs)）。

1. **目标**：观察水平/垂直、实线/虚线、带 label 的各种组合。
2. **操作步骤**：
   - 启动 Gallery，打开 `Separator` 页。
   - 「Horizontal Separators」区块依次有：实线、带 label 实线、虚线、带 label 虚线（见 [separator_story.rs:46-57](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/separator_story.rs#L46-L57)）。
   - 「Vertical Separators」区块高度固定为 100px（`h(px(100.))`），用于让垂直线有可撑满的高度。
3. **需要观察的现象**：带 label 的分隔线，文字会用背景色「打断」线条形成居中标题；虚线是 4px 实/2px 空的均匀虚段；垂直线必须父容器有明确高度才能撑满。
4. **预期结果**：与 `render_dashed` 的 `dash_array(&[px(4.), px(2.)])` 一致。

下面是「示例代码」（仿 separator_story，非项目原有）：

```rust
use gpui::{px, ParentElement};
use gpui_component::{h_flex, separator::Separator, v_flex};

// 水平带标题分隔线
v_flex().child(Separator::horizontal().label("区块标题"))
// 两条垂直虚线分隔三个项目（需要父容器有高度）
h_flex()
    .h(px(100.))
    .child("Docs")
    .child(Separator::vertical().dashed())
    .child("Github")
```

> 运行结果需在真实窗口中验证（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么垂直分隔线需要父容器有明确高度？

> **答案**：`Separator::vertical()` 的 base 是 `div().h_full()`（[separator.rs:30](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/separator.rs#L30)），而 `render_base` 里垂直线又用 `h_full()` 撑满（[separator.rs:80-83](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/separator.rs#L80-L83)）。`h_full()` 的含义是「等于父容器高度」，若父容器高度未定（如默认 flex 行无固定高），垂直线高度就是 0、看不见。Story 里特意 `.h(px(100.))` 就是为此。

**练习 2**：虚线为什么用 `canvas` + `PathBuilder` 画，而不是用 CSS 风格的 `border` 虚线？

> **答案**：GPUI 的元素样式不直接支持「虚线边框」。要得到任意长度的均匀虚线，库选择在绘制阶段用 `canvas` 拿到实际 `bounds`，再沿轴向画一条带 `dash_array` 的描边路径（[separator.rs:90-117](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/separator.rs#L90-L117)）。这样虚段长度与容器尺寸无关，始终是「4px 实 + 2px 空」。

---

## 5. 综合实践

把本讲四个组件串起来，做一个「用户信息卡片」：

- 用 `Label` 显示用户名（带 `secondary` 显示工号）。
- 用 `Badge` 在头像右上角显示在线状态（绿色圆点）或未读消息数。
- 用 `Tag` 标记用户角色（如 `success` 变体的「管理员」）。
- 用 `Separator` 把「头像/名字区」与「角色标签区」分开。

下面是「示例代码」（非项目原有，综合本讲各组件，需嵌入到一个实现了 `Render` 的 View 中运行）：

```rust
use gpui::{IntoElement, ParentElement};
use gpui_component::{
    avatar::Avatar, badge::Badge, h_flex, label::Label, separator::Separator, tag::Tag,
    v_flex, ActiveTheme as _,
};

// 假设此函数返回的元素被某个 View 的 render 方法使用
pub fn user_card(cx: &gpui::App) -> impl IntoElement {
    h_flex()
        .gap_4()
        // 头像 + 在线状态圆点
        .child(
            Badge::new()
                .dot()
                .color(cx.theme().green)
                .child(Avatar::new().src("https://avatars.githubusercontent.com/u/5518?v=4")),
        )
        // 名字 + 工号
        .child(
            v_flex().child(
                Label::new("Alice Zhang").secondary("#1024"),
            ),
        )
        // 垂直分隔线（注意父行需有高度）
        .child(Separator::vertical())
        // 角色标签
        .child(
            h_flex()
                .gap_2()
                .child(Tag::success().child("管理员"))
                .child(Tag::info().outline().child("前端组")),
        )
}
```

**验收清单**：

1. 头像右上角有一个绿色小圆点（`Badge` dot）。
2. 名字后跟着较淡的 `#1024`（`Label` secondary，颜色为 `muted_foreground`）。
3. 角色标签中「管理员」是实心绿色，「前端组」是描边蓝色（outline）。
4. 名字区与角色区之间有一条垂直实线（`Separator::vertical`）；若看不到线，检查父容器是否给了固定高度。

> 若想看到真实效果，可把这段代码放入一个最小 View（参照 [u1-l4 应用入口](./u1-l4-entry-init-and-root.md) 的 hello_world 骨架，调用 `gpui_component::init(cx)` 并用 `Root` 包裹）。运行结果待本地验证。

## 6. 本讲小结

- `Label` 是基础文本元素，无状态 `RenderOnce` 组件，靠 `secondary`/`masked`/`highlights` 叠加副文本、脱敏、高亮；高亮基于字节区间且大小写不敏感，用 `is_char_boundary` 保护多字节字符。
- `Tag` 用「变体（`TagVariant`）× outline × 尺寸（`Sizable`）× 圆角」四套正交开关组合外观，变体到颜色映射集中在 `bg`/`border`/`fg` 三方法，`Color` 变体会按明暗模式自动取明度。
- `Badge` 是相对定位容器 + 绝对定位角标，三种形态 `Number`/`Dot`/`Icon`；`count(0)` 自动隐藏，超过 `max`（默认 99）显示 `N+`。
- `Separator` 支持水平/垂直、实线/虚线与带 label；实线是 1px 填充条，虚线用 `canvas` 在绘制阶段画 `dash_array [4,2]` 路径；外层 `flex_shrink_0` 防止被挤压，垂直线依赖父容器高度。
- 四者都遵循库的统一约定：无状态 `RenderOnce`、暴露 `Styled`/`Sizable`/`ParentElement`、颜色优先取 `cx.theme()` 语义色、状态由外层 View 持有。

## 7. 下一步学习建议

- 下一讲 [u3-l3 头像与进度](./u3-l3-avatar-progress.md) 会精读 `Avatar`/`Progress`/`Skeleton`/`Spinner`，本讲的 `Badge` 已经大量配合 `Avatar` 使用，正好衔接。
- 想看这四个组件的真实组合用法，可直接阅读 Story 源码：`crates/story/src/stories/` 下的 `label_story.rs`、`tag_story.rs`、`badge_story.rs`、`separator_story.rs`。
- 若你想自己造一个类似的展示组件，回顾 [u2-l2 样式系统](./u2-l2-styled-and-sizable.md) 中 `Styled`/`Sizable`/`RenderOnce` 的组合方式——本讲的 `Tag` 就是同时实现这三者的范例。
