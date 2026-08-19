# u2-l2 Label 家族与排版系统

## 1. 本讲目标

学完本讲，你应该能够：

1. 熟练使用 `Label` 的常用 builder 方法：`size`、`weight`、`color`、`truncate` 系列、`line_clamp` 等。
2. 说清 `Label`、`LabelCommon` trait、`LabelLike` 三者的委托结构：为什么 API 定义在 trait 上、状态存在 `LabelLike` 里、`Label` 只是一个薄门面。
3. 理解 `TextSize` 档位与 `StyledTypography` 扩展方法（`text_ui`、`text_ui_sm`、`font_buffer` 等）如何把「像素字号」换算成 rem，并随用户的 `ui_font_size` 设置整体缩放。

## 2. 前置知识

本讲建立在前两讲的基础上，先用两段话把要承接的结论摆出来：

- **u2-l1 语义颜色**：`ui::Color` 是按「意图」命名的枚举，组件只在构建期存下这个语义键，真正的 `Hsla` 颜色值延迟到渲染期由 `Color::color(cx)` 查询当前主题获得。本讲会看到 `LabelLike` 对文字颜色用的是完全相同的套路。
- **u1-l3 RenderOnce**：无状态组件的标准骨架是 `#[derive(IntoElement)]` + `impl RenderOnce`，组件结构体只是「一次渲染输入的配方」，每帧由父视图重建、在 `render(self, ...)` 中被按值消费，builder 方法统一按值接收 `self`。`Label` 和 `LabelLike` 都是这个模型。

在此基础上，补充三个本讲要用的排版术语：

- **字重（FontWeight）**：笔画的粗细。`FontWeight` 是 gpui 提供的类型，常用值如 `FontWeight::BOLD`、`FontWeight::MEDIUM`、`FontWeight::THIN`，内部以 0–1000 的数值刻画。
- **rem**：CSS 借来的相对单位。gpui 中 `Rems` 表示「当前窗口 rem 基准的倍数」，渲染时按

  \[ \text{实际像素} = \text{rem 值} \times \text{窗口 rem 基准（px）} \]

  换算。Zed 把窗口 rem 基准设为用户的 UI 字体大小（默认 16px），因此调大 UI 字体会让**所有**以 rem 表达的尺寸（文字、间距、图标）一起变大——这就是 Zed 界面整体缩放的实现方式。
- **文本截断（truncation）**：文字超出可用宽度时的处理策略。Zed 支持尾部截断（`…` 在末尾）、头部截断（`…` 在开头，保留结尾）、中部截断（`…` 在中间，首尾都保留，适合文件路径）以及按行数截断（`line_clamp`）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/components/label/label.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label.rs) | `Label` 组件：门面 + 文本子元素 + 反引号代码片段解析 |
| [src/components/label/label_like.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label_like.rs) | `LabelCommon` trait、`LabelSize`、`LineHeightStyle`、`LabelLike` 基座及其 `RenderOnce` 实现 |
| [src/styles/typography.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/typography.rs) | `TextSize` 档位、`StyledTypography` 扩展 trait、`Headline` 标题组件 |
| [src/styles/units.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/units.rs) | `BASE_REM_SIZE_IN_PX` 常量与 `rems_from_px` 换算函数 |
| [src/prelude.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/prelude.rs) | 本讲涉及的类型（`Label`、`LabelCommon`、`LabelSize`、`LineHeightStyle`、`TextSize`、`StyledTypography`、`Headline` 等）全部经它导出 |

label 目录下还有三个兄弟组件（本讲只做认知定位，不精读）：`highlighted_label.rs`（多段高亮文字）、`spinner_label.rs` / `loading_label.rs`（加载态文字）。它们与 `Label` 一样，内部都持有一个 `LabelLike` 作为基座。整个 label 模块由 `src/components.rs` 声明并以 `pub use label::*;` 扁平导出（见 [src/components.rs:L24](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components.rs#L24) 与 [src/components.rs:L67](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components.rs#L67)），所以下游写 `ui::Label` 即可。

## 4. 核心概念与源码讲解

### 4.1 Label：文本组件的门面

#### 4.1.1 概念说明

`Label` 是 ui crate 里最常用的文本组件——界面上几乎所有静态文字最终都经过它。它解决的问题很朴素：**给一段文字套上「语义化的排版配置」**（多大、多粗、什么颜色、超宽怎么办），同时把「文字如何被渲染」的细节全部隐藏。

说它是「门面」是因为 `Label` 自己几乎不存排版状态：它的结构体只有三个字段——一个 `LabelLike` 基座、一段 `SharedString` 文本、一个「是否渲染反引号代码片段」的布尔值。所有 `size`/`color` 之类的调用都被转发给基座。这种设计让 `Label`、`HighlightedLabel` 等多个文字组件共享同一套排版引擎，API 却保持简短。

#### 4.1.2 核心流程

`Label` 从创建到上屏的路径：

1. `Label::new("文本")` 创建实例，内部初始化一个默认 `LabelLike`。
2. 链式调用 builder 方法（`.size(...)`、`.color(...)` 等），每个方法按值消费 self、修改后返回。
3. 布局阶段，`RenderOnce::render(self, ...)` 被调用，`self` 被拆解：
   - 若开启了 `render_code_spans` 且文本中含成对反引号 → 解析出代码片段区间，用 `StyledText` + 高亮 + 字体覆写渲染；
   - 否则 → 把 `SharedString` 直接作为 child 塞进 `self.base`（`LabelLike`），返回基座。
4. 基座 `LabelLike` 自己也是 `RenderOnce`（4.2 节精读），由它把字号、颜色、截断等配置翻译成 gpui 样式。

伪代码：

```text
Label::new(text)
  └─ render(self)
       ├─ render_code_spans 且有反引号？
       │    ├─ 是 → parse_backtick_spans → StyledText + highlights + 字体覆写
       │    └─ 否 → base.child(self.label)
       └─ 返回 self.base（LabelLike，等待它自己的 render）
```

#### 4.1.3 源码精读

先看结构体与构造函数。`Label` 派生 `IntoElement` 与 `RegisterComponent`（后者服务于组件预览体系，u8-l5 再展开）：

[crates/ui/src/components/label/label.rs:L34-L57](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label.rs#L34-L57)

这段代码定义了 `Label` 的三个字段并给出 `new` 构造器：`base` 是承载全部排版状态的 `LabelLike`，`label` 是文本（`impl Into<SharedString>` 让调用方可以直接传 `&str`、`String` 或 `SharedString`），`render_code_spans` 默认关闭。

`Label` 自己的固有方法不多，但都很有用。前两个控制反引号代码片段与运行期改文本，后三个是截断方法的「转发放大」：

[crates/ui/src/components/label/label.rs:L59-L88](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label.rs#L59-L88)

注意这些方法的统一形态：`fn xxx(mut self) -> Self`——按值拿 self、改字段、还回去，这正是 u1-l3 讲过的 builder 惯例。`truncate_start` / `truncate_middle` / `line_clamp` 并没有定义在 `LabelCommon` trait 里，而是 `Label`（和 `LabelLike`）的固有方法，随后逐一把调用转交给 `self.base`。

`Label` 还暴露了一组 flex 布局方法，直接改写基座内部的 `StyleRefinement`：

[crates/ui/src/components/label/label.rs:L93-L127](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label.rs#L93-L127)

这里有两个值得注意的细节：`style()` 私有方法穿透到 `self.base.base.style()`（两层 `base`：Label 的 base 是 LabelLike，LabelLike 的 base 是 `Div`）；`gpui::margin_style_methods!` 宏则批量生成 `mt_1`、`mx_2` 这类 margin 快捷方法，避免手写几十个雷同函数。`flex_1()` 常用于让标签在工具栏中占据弹性空间——例如按钮左侧的标签名随窗口伸缩。

最后是 `Label` 的渲染实现与代码片段解析：

[crates/ui/src/components/label/label.rs:L259-L289](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label.rs#L259-L289)

`render` 的主体逻辑：开启 `render_code_spans` 时，先尝试 `parse_backtick_spans` 解析反引号区间；成功则取 theme 设置里的 buffer（等宽）字体族和 `element_background` 主题色，为每个代码区间构造背景高亮与字体覆写，交给 gpui 的 `StyledText` 渲染；任何一步不成立都退回 `self.base.child(self.label)`。注意 `cx.theme()` 与 `theme::theme_settings(cx)` 再次体现了「渲染期才查主题」的解耦。

解析函数本身是一个教科书式的小状态机，并且自带 5 个单元测试：

[crates/ui/src/components/label/label.rs:L296-L324](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label.rs#L296-L324)

它把反引号剥掉、记录剥离后文本中代码片段的字节区间；没有成对反引号时返回 `None`。对应的测试在 [crates/ui/src/components/label/label.rs:L326-L361](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label.rs#L326-L361)，覆盖了无反引号、单个、多个、未闭合、空片段五种情况——读测试断言是理解边界行为最快的方式（如「未闭合反引号不产生代码区间」）。

#### 4.1.4 代码实践

**实践目标**：通过 doc test 验证 `Label` 的最小用法，并观察 `render_code_spans` 的输入输出。

**操作步骤**：

1. 在仓库根目录运行 label 相关的文档测试（这些测试就来自上面精读的 doc 注释）：

   ```bash
   cargo test -p ui --doc label
   ```

2. 阅读输出中被执行的用例名，对照 [crates/ui/src/components/label/label.rs:L11-L33](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label.rs#L11-L33) 的四个 doc 示例（新建、着色、删除线）。
3. 再运行纯逻辑单测，观察 `parse_backtick_spans` 的断言：

   ```bash
   cargo test -p ui parse_backtick_spans
   ```

**需要观察的现象**：doc test 全部通过；`parse_backtick_spans` 的 5 个用例名与 4.1.3 中列出的边界情况一一对应。

**预期结果**：两条命令均报告 passed。若 `--doc label` 过滤不到用例，可去掉 filter 直接 `cargo test -p ui --doc`（耗时更长）。「在当前环境能否完整编译并跑通」属于待本地验证事项——本讲不假设已经运行过。

#### 4.1.5 小练习与答案

**练习 1**：`Label::new` 为什么接受 `impl Into<SharedString>` 而不是 `&str`？

**答案**：`SharedString` 是 `&'static str` 与 `Arc<str>` 的二选一封装，能避免无谓拷贝；接受 `impl Into<SharedString>` 让调用方传 `&str`、`String`、`SharedString` 都能零成本或一次成本地转换，API 更通用（见 [label.rs:L51](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label.rs#L51)）。

**练习 2**：`Label::set_text`（[label.rs:L67-L69](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label.rs#L67-L69)）接收 `&mut self`，与其余 builder 方法风格不同，这暗示了什么使用场景？

**答案**：builder 方法服务于「构造后立即交给元素树」的无状态用法；`set_text` 服务于「把 Label 存在结构体字段里、随状态更新文本」的用法——持有者可在自己的 `render` 里先 `set_text` 再把这个 Label 放进元素树。

**练习 3**：想让提示文案里的 `` `Cargo.toml` `` 显示为等宽小代码块，应该怎么写？

**答案**：`Label::new("打开 `Cargo.toml` 继续").render_code_spans()`。渲染期会把反引号剥掉、给该区间套 buffer 字体与 `element_background` 底色（见 [label.rs:L259-L289](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label.rs#L259-L289)）。

### 4.2 LabelCommon 与 LabelLike：共享 API 的委托结构

#### 4.2.1 概念说明

打开 `label_like.rs` 会看到一个三层结构，这是 ui crate 复用组件 API 的标准手法：

- **`LabelCommon` trait**：定义「一个文字组件应该能做什么」——设字号、字重、颜色、删除线、截断……共 13 个方法。
- **`LabelLike`**：实现这个 trait 的「基座组件」，真正持有全部排版字段，并在自己的 `RenderOnce::render` 里把字段翻译成 gpui 样式。它同时实现 `ParentElement`，可以容纳任意子元素。
- **`Label` / `HighlightedLabel` 等**：包装 `LabelLike` 的门面，把 trait 方法一一委托给基座，自己只追加差异能力（`Label` 追加的是「纯文本 + 代码片段」）。

为什么这样拆？因为「设置文字样式」这件事在多个组件里重复出现，若各自实现会迅速漂移。用 trait 约束 API、用基座承载实现，下游组件只需一行 `self.base = self.base.size(size)` 就能获得一致行为。这与 u3-l4 将要讲的 `Clickable`/`Disableable` 能力 trait 是同一思想的两种应用：一个管「文字排版能力」，一个管「交互能力」。

什么时候直接用 `LabelLike`？源码文档说得很直白：预置标签不够用时才用，且要节制，因为它不受约束、容易破坏 UI 一致性（见 [label_like.rs:L72-L77](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label_like.rs#L72-L77)）。典型合法场景恰恰是 crate 内部——用它搭建预置标签。

#### 4.2.2 核心流程

`LabelLike::render` 是整个文字排版的汇总点，它把「构建期存的配置」逐项翻译成样式：

```text
render(self, cx):
  color = self.color.color(cx)          # 渲染期查主题（u2-l1 的模式）
  if alpha 有值: color.fade_out(1 - alpha)
  base = self.base (Div)
  按 LabelSize 选择 text_ui_lg / text_ui / text_ui_sm / text_ui_xs / text_size(Custom)
  when UiLabel 行高样式 → line_height(relative(1.))
  when italic / underline / strikethrough / single_line → 对应样式
  when truncate / truncate_start / truncate_middle → 截断三件套
  默认字重 ← theme 设置的 ui_font 字重
  返回 base.children(self.children)
```

三种截断的样式配方完全同构，只有省略号位置不同：

\[ \text{截断} = \texttt{min\_w\_0} + \texttt{overflow\_x\_hidden} + \texttt{whitespace\_nowrap} + \text{省略号位置} \]

其中省略号位置分别是 `text_ellipsis`（尾部）、`text_ellipsis_start`（头部）、`text_ellipsis_middle`（中部）。`min_w_0()` 尤其关键：flexbox 子项默认有最小内容宽度，不把它清零，文字永远「挤不小」，截断永远不会触发。

#### 4.2.3 源码精读

先看两个配置枚举。`LabelSize` 是语义化字号档位，外加一个 `Custom(Rems)` 逃生门：

[crates/ui/src/components/label/label_like.rs:L5-L31](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label_like.rs#L5-L31)

`LineHeightStyle` 区分两种行高策略：`TextLabel`（默认，用 UI 或开发者 buffer 的默认行高，适合正文）与 `UiLabel`（行高强制为 1，适合紧凑的控件内标签）。这在多行文字上差异明显。

然后是 trait 本体：

[crates/ui/src/components/label/label_like.rs:L33-L70](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label_like.rs#L33-L70)

13 个方法全部是 `fn xxx(self, ...) -> Self` 的按值签名。注意 `buffer_font` 与 `inline_code` 需要 `cx: &App`——因为它们要查 theme 设置里的字体，又一次「渲染期解析」。

`LabelLike` 的字段就是这份 API 的状态镜像：

[crates/ui/src/components/label/label_like.rs:L77-L93](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label_like.rs#L77-L93)

`base: Div` 承载布局与样式；其余布尔/Option 字段一一对应 trait 方法；`children: SmallVec<[AnyElement; 2]>` 说明它预期通常只有一两个子元素（`SmallVec` 在栈上内联两个元素，避免常见情形下的堆分配）。

委托的写法以 `Label` 为例（`HighlightedLabel` 同理）：

[crates/ui/src/components/label/label.rs:L130-L186](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label.rs#L130-L186)

每个方法体都是同一句 `self.base = self.base.xxx(...)`。看两个需要 `cx` 的特殊实现——`buffer_font` 与 `inline_code`：

[crates/ui/src/components/label/label_like.rs:L209-L224](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label_like.rs#L209-L224)

`buffer_font` 从 theme 设置取用户的 buffer 字体整体（家族+字重）套上；`inline_code` 在此之上加 `element_background` 底色、小圆角和 `px_0p5` 内边距，做出「行内代码」的观感。

`ParentElement` 的实现让 `LabelLike` 可以当容器用（`Label::render` 的 `self.base.child(...)` 正是走这条路）：

[crates/ui/src/components/label/label_like.rs:L227-L231](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label_like.rs#L227-L231)

`LabelLike` 的三个固有截断方法里，`line_clamp` 藏着一条重要注释：

[crates/ui/src/components/label/label_like.rs:L134-L154](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label_like.rs#L134-L154)

注释解释了为什么实现是 `line_clamp(lines).text_ellipsis()` 的组合：只设 `line_clamp` 会把文字**硬切**在行边界，最后一行末尾的省略号只有再叠加一个文本溢出样式才会渲染出来。

最后是汇总点 `render`：

[crates/ui/src/components/label/label_like.rs:L233-L287](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label_like.rs#L233-L287)

逐段读：

- L235–L238：`self.color.color(cx)` 查主题拿 `Hsla`（u2-l1 精读过的解析链），`alpha` 通过 `fade_out(1.0 - alpha)` 施加透明度。
- L241–L247：`LabelSize` → `text_ui_*` 的映射表，`Custom` 直接 `text_size(rem)`。这五行是本讲与 4.3 节的接缝。
- L248–L250：`UiLabel` 行高样式把行高设为 `relative(1.)`。
- L252–L259：下划线不是简单开关，而是构造 `UnderlineStyle`：1px 粗、颜色取主题 `text_muted` 再打 0.4 透明度、非波浪线——一个「弱化下划线」的精心取值。
- L260–L261：删除线与 `single_line`（后者强制不换行）。
- L262–L279：三种截断，配方如 4.2.2 所述。
- L280–L284：`text_color(color)` 收尾；字重取 `self.weight.unwrap_or(theme 的 ui_font 字重)`——**未显式设置字重时跟随用户 UI 字体设置**，这是很多人意外的默认行为。
- L285：装上 children 返回。

整段大量使用 u1-l3 提过的 `FluentBuilder::when`，把「字段 → 条件样式」写成一条流水线。

#### 4.2.4 代码实践

**实践目标**：亲手验证三种截断策略与 `line_clamp` 的视觉差异，理解各自适用场景。

**操作步骤**：

1. 参考 `Label` 已有的 doc 示例风格（[label.rs:L411-L418](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label.rs#L411-L418) 的 preview 里就有 `max_w_24` 容器 + `truncate` 的现成写法），在你自己的分支上给 `Label` 增加一个 doc 示例（示例代码，非项目原有）：

   ```rust
   /// ```
   /// use ui::prelude::*;
   ///
   /// let long_path = "zed/crates/ui/src/components/label/label.rs";
   /// let _ = div().max_w_24().child(Label::new(long_path).truncate());
   /// let _ = div().max_w_24().child(Label::new(long_path).truncate_start());
   /// let _ = div().max_w_24().child(Label::new(long_path).truncate_middle());
   /// let _ = div().max_w_40().child(Label::new("两行截断的长描述文字……").line_clamp(2));
   /// ```
   ```

2. 运行：

   ```bash
   cargo test -p ui --doc label
   ```

3. 若想看真实渲染效果，可对照组件预览：`Label` 的 `Component::preview` 中 "Special Cases" 组已经展示了 `truncate` 与 `truncate_start` 的并排对比（[label.rs:L411-L418](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label.rs#L411-L418)），Zed 的组件预览入口可打开该组观察。

**需要观察的现象**：同一段超宽路径，`truncate` 丢失结尾、`truncate_start` 丢失开头、`truncate_middle` 首尾都保留；`line_clamp(2)` 在第二行末尾出现省略号而非被硬切。

**预期结果**：doc test 编译通过即证明 API 组合合法；视觉效果待本地验证（需要图形环境）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `truncate` 的实现里必须带 `min_w_0()`？

**答案**：flex 布局中子项的最小宽度默认是其内容宽度（min-content），文字再长也会把容器撑开或溢出而不触发截断；`min_w_0()` 把最小宽度归零，允许容器把子项压到更窄，`overflow_x_hidden` + `text_ellipsis` 才有机会生效（见 [label_like.rs:L262-L267](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label_like.rs#L262-L267)）。

**练习 2**：`Label::underline()` 的下划线颜色从哪来？为什么不用文字同色？

**答案**：取 `cx.theme().colors().text_muted` 再乘 0.4 透明度（[label_like.rs:L252-L259](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label_like.rs#L252-L259)）。用弱化的中性色而非文字色，让下划线提示「可交互」又不与正文抢视觉重量。

**练习 3**：不给 `Label` 设置 `weight` 时，最终字重由什么决定？

**答案**：由用户的 UI 字体设置决定：`theme::theme_settings(cx).ui_font(cx).weight`（[label_like.rs:L281-L284](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label_like.rs#L281-L284)）。也就是说换一个 UI 字体，未显式设字重的标签会跟着变。

### 4.3 TextSize 与 StyledTypography：排版档位与 rem 缩放

#### 4.3.1 概念说明

4.2 节留下了一个问题：`text_ui`、`text_ui_sm` 这些方法从哪来、值是多少？答案在 `styles/typography.rs`：

- **`TextSize`** 是文字版的设计令牌（design token）：`Large` / `Default` / `Small` / `XSmall` 四个固定档位，外加 `Ui`（用户 `ui_font_size`）与 `Editor`（用户 `buffer_font_size`）两个「跟随用户设置」档。
- **`StyledTypography`** 是 gpui `Styled` 的扩展 trait，提供 `text_ui_lg` / `text_ui` / `text_ui_sm` / `text_ui_xs` / `text_buffer` / `font_ui` / `font_buffer` 等便捷方法——任何实现了 `Styled` 的元素都能用。

这与 u2-l1 的 `Color` 是同一个模式的又一次出现：**组件存语义键（`TextSize::Small`），渲染期解析成实际尺寸**。解析目标不是 `px` 而是 `rem`，而 rem 的换算基准是「窗口 rem 基准」——Zed 把它设成用户的 UI 字体大小。于是用户调大 UI 字体时，不止文字，间距、图标等一切 rem 尺寸同步放大，整个界面像被均匀缩放。**需要澄清一个容易误解的点：当前源码中并不存在名为 `ui_scale` 的设置**，承担「缩放整个 UI」职责的设置就是 theme 设置里的 `ui_font_size`（以及运行期的临时放大调整），机制见下文。

#### 4.3.2 核心流程

完整链路分四段：

```text
① 组件层：LabelSize::Small
② 扩展层：text_ui_sm(cx)  →  TextSize::Small.rems(cx)
③ 换算层：rems_from_px(12) = 12 / 16 = 0.75rem      （BASE_REM_SIZE_IN_PX = 16）
④ 渲染层：实际像素 = 0.75 × 窗口 rem 基准
```

窗口 rem 基准在应用启动/刷新时由 theme_settings 设置：

```text
setup_ui_font(window, cx):
  ui_font_size = ThemeSettings 的 ui_font_size(cx)
  window.set_rem_size(ui_font_size)      # 此后 1rem = ui_font_size 像素
```

数值上，设用户 UI 字体为 \( u \) px，则 `TextSize::Default` 的实际字号为：

\[ \text{px} = \frac{14}{16} \times u = 0.875u \]

默认 \( u = 16 \) 时得 14px；用户把 UI 字体调到 20px 时，同一段文字变成 17.5px——**组件代码一个字都不用改**，这就是「档位用 rem 表达」的收益。

各档位换算表（按代码实际计算）：

| LabelSize | StyledTypography 方法 | TextSize | 代码换算 | rem 值 | 默认基准 16px 下 |
| --- | --- | --- | --- | --- | --- |
| Large | `text_ui_lg` | Large | `rems_from_px(16)` | 1.000 | 16px |
| Default | `text_ui` | Default | `rems_from_px(14)` | 0.875 | 14px |
| Small | `text_ui_sm` | Small | `rems_from_px(12)` | 0.750 | 12px |
| XSmall | `text_ui_xs` | XSmall | `rems_from_px(10)` | 0.625 | 10px |
| Custom(r) | `text_size(r)` | — | 直接使用传入 rem | r | 16r px |

一个源码阅读的警示：`TextSize::Default` 的 doc 注释写的是「`0.825rem` or `14px`」（[typography.rs:L44-L53](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/typography.rs#L44-L53)），但代码是 `rems_from_px(14)`，即 14/16 = **0.875**rem（0.875 × 16px = 14px 自洽；而 0.825 × 16px = 13.2px 与注释自称的 14px 矛盾）。注释与代码不一致时，以代码为准——这是读源码的基本纪律，本表已按代码修正。

#### 4.3.3 源码精读

先看 `StyledTypography` trait 的核心方法：

[crates/ui/src/styles/typography.rs:L10-L53](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/typography.rs#L10-L53)

`font_buffer` / `font_ui` 分别把字体族切到用户的 buffer / UI 字体；`text_ui_size` 是「带 `cx` 的通用档位入口」；每个 `text_ui_*` 方法的 doc 注释都提醒同一件事：绝对大小会随用户 UI 字体设置变化。第 89 行的 blanket impl 让所有 `Styled` 元素免费获得这些方法：

[crates/ui/src/styles/typography.rs:L89](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/typography.rs#L89)

小字号两档与 `text_buffer`：

[crates/ui/src/styles/typography.rs:L62-L86](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/typography.rs#L62-L86)

`text_buffer` 值得单独记：它取 `buffer_font_size` 设置，**只应**用于缓冲区文本或需要与编辑器字号对齐的场景——UI 文字用它会造成两套字号体系混用。

`TextSize` 枚举与它的两个解析方法：

[crates/ui/src/styles/typography.rs:L91-L158](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/typography.rs#L91-L158)

`rems(cx)` 与 `pixels(cx)` 是同一档位的两种表达：前者供 `text_size`（样式里存 rem）用，后者供需要直接像素值的场合（如图标尺寸换算）用。固定四档的换算不依赖任何设置——`rems_from_px(14)` 永远是 0.875rem；`Ui` / `Editor` 两档则读 theme 设置。

换算函数与常量：

[crates/ui/src/styles/units.rs:L3-L15](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/units.rs#L3-L15)

`rems_from_px(px) = px / 16`。基准 16 写死在 `BASE_REM_SIZE_IN_PX`——注意它换算的是「设计像素 → rem」，与「窗口 rem 基准」是两回事：前者是静态的数学换算，后者是渲染期由窗口状态决定的动态乘数。

跨出 ui crate，看窗口 rem 基准从哪来：

[crates/theme_settings/src/settings.rs:L587-L596](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/theme_settings/src/settings.rs#L587-L596)

`setup_ui_font` 读出用户的 `ui_font_size`，调用 `window.set_rem_size(ui_font_size)`。gpui 侧的定义在 [crates/gpui/src/window.rs:L2632](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/window.rs#L2632)。这一行就是「调 UI 字体 = 缩放整个界面」的全部队列来源：rem 基准变了，所有 rem 尺寸跟着变。

最后顺带认识同文件的 `Headline`——排版系统的另一个成员，用于标题层级：

[crates/ui/src/styles/typography.rs:L161-L223](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/typography.rs#L161-L223)

`HeadlineSize` 五档采用「Major Second」比例尺（相邻档约 1.125 倍：0.88 / 1.0 / 1.125 / 1.27 / 1.43 rem），统一 1.6rem 行高；`render` 里套用户 UI 字体、设行高字号、文字颜色取主题 `text`。注意它的 `rems()` **不带 `cx`**——档位值是硬编码 rem，不经过 `TextSize`，但同样以 rem 表达，因此同样随窗口 rem 基准缩放。

#### 4.3.4 代码实践

**实践目标**：验证 `text_ui` / `text_ui_sm` 的 rem 值换算，并观察 rem 随用户 UI 字体设置的整体缩放。

**操作步骤**：

1. **纸面推演**：设用户 `ui_font_size = 20`，用公式 \[ \text{px} = \text{rem} \times u \] 计算四档字号的实际像素（Large 20、Default 17.5、Small 15、XSmall 12.5），写下来。
2. **验证 rem 值**：在你自己的分支上给 `StyledTypography` 写一个 doc 示例（示例代码，非项目原有）断言换算结果：

   ```rust
   /// ```
   /// use ui::prelude::*;
   ///
   /// // TextSize::Small 换算成 12/16 = 0.75rem
   /// assert_eq!(TextSize::Small.rems(cx), rems(0.75));
   /// ```
   ```

   然后运行 `cargo test -p ui --doc typography`。

3. **观察整体缩放**：在 Zed 的 settings.json 中把 `ui_font_size` 从默认调大一档（如 16 → 20），重启或等待设置生效，观察侧栏、标签页、按钮内的文字**连同图标与间距**一起变大。

**需要观察的现象**：第 2 步断言通过；第 3 步中不仅是文字，`Icon`、按钮内边距等所有 rem 尺寸同步放大，界面布局比例不变。

**预期结果**：rem 换算可确定性验证；`ui_font_size` 的视觉效果待本地验证（需要图形环境的 Zed）。若 doc test 的 `cx` 参数名与实际不符，参照 [typography.rs:L29-L31](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/typography.rs#L29-L31) 的签名修正。

#### 4.3.5 小练习与答案

**练习 1**：`TextSize::Ui` 档的 `rems(cx)` 是 `rems_from_px(settings.ui_font_size(cx))`，而窗口 rem 基准恰好也是 `ui_font_size`。设 `ui_font_size = 20`，这一档的实际像素是多少？

**答案**：rem 值 = 20/16 = 1.25rem；实际像素 = 1.25 × 20 = 25px。也就是说该档不是「还原成 20px」，而是「按比例放大一档」——固定档位与跟随档叠加基准缩放时，先除 16 再乘基准，乘除并不相消。这个例子提醒我们：读换算代码时要始终区分「换算用的静态 16」与「渲染期的动态基准」。

**练习 2**：为什么 `LabelLike::render` 里 `LabelSize::Custom(size)` 直接 `this.text_size(size)` 而其他档位要走 `text_ui_*`？

**答案**：`Custom(Rems)` 已经是 rem 值，无需换算，直接交给 `Styled::text_size`；而四档语义档位需要先经 `TextSize::rems(cx)` 把「设计像素」换算成 rem（见 [label_like.rs:L241-L247](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label_like.rs#L241-L247) 与 [typography.rs:L132-L145](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/typography.rs#L132-L145)）。

**练习 3**：`Headline` 的字号会随用户 UI 字体缩放吗？

**答案**：会。`HeadlineSize::rems()` 返回硬编码 rem（如 Medium = 1.125rem），看似与用户设置无关，但渲染像素 = rem × 窗口 rem 基准，而基准就是 `ui_font_size`（[typography.rs:L179-L189](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/typography.rs#L179-L189)、[theme_settings/src/settings.rs:L594](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/theme_settings/src/settings.rs#L594)）。这正是「一切尺寸以 rem 表达」的好处。

## 5. 综合实践

把本讲全部内容串成一个「项目概览卡片」的构造任务（示例代码，可在你自己的分支上以 doc 示例或临时视图的形式落地）：

```rust
use ui::prelude::*;

// 示例代码：文件详情卡片
v_flex()
    .gap_2()
    .max_w_80()
    .child(Headline::new("label.rs").size(HeadlineSize::Small))     // 标题层级：比例尺档位
    .child(
        h_flex().gap_2()
            .child(Label::new("zed/crates/ui/src/components/label/label.rs")
                .size(LabelSize::XSmall)
                .color(Color::Muted)
                .truncate_middle()),                                   // 文件路径：中部截断保首尾
            // （h_flex 内还可以放 CountBadge 之类的状态元素）
    )
    .child(Label::new("构建失败：类型不匹配")
        .color(Color::Error)
        .weight(FontWeight::BOLD))                                    // 语义色 + 显式字重
    .child(Label::new("该文件定义了 Label 组件……（此处为一段较长的说明文字，超出两行时应当被截断）")
        .line_clamp(2))                                               // 多行截断
    .child(Label::new("依赖 `gpui` 与 `theme` 两个 crate")
        .render_code_spans())                                         // 反引号代码片段
```

落地后逐项自检，每项都能答出「机制链路」才算过关：

1. `Headline` 的 16px 来自哪条链？（HeadlineSize::Small = 1.0rem × 窗口 rem 基准）
2. `truncate_middle` 为什么首尾都保留？（`text_ellipsis_middle` + nowrap + min_w_0 三件套）
3. `Color::Error` 何时变成真实颜色？（渲染期 `Color::color(cx)` 查主题，u2-l1）
4. 未设 `weight` 的那两行标签字重由谁决定？（用户 UI 字体的字重）
5. 用户把 `ui_font_size` 调到 20px 后，卡片里哪些尺寸变了？（全部——所有尺寸都以 rem 表达）

验证方式：`cargo test -p ui --doc` 保证编译与断言通过；真实渲染效果待本地验证。

## 6. 本讲小结

- `Label` 是薄门面：结构体只有 `LabelLike` 基座 + 文本 + 代码片段开关，13 个 `LabelCommon` 方法全部一行委托给基座；`HighlightedLabel` 等兄弟组件复用同一基座。
- `LabelLike::render` 是排版汇总点：查主题解析颜色、按 `LabelSize` 选 `text_ui_*` 档位、用一串 `when` 应用斜体/下划线/删除线/截断，默认字重跟随用户 UI 字体设置。
- 三种单行截断同构：`min_w_0 + overflow_x_hidden + whitespace_nowrap + 省略号位置`；`line_clamp` 必须叠加 `text_ellipsis` 才会在末行显示省略号。
- `TextSize` 是文字版设计令牌：固定四档经 `rems_from_px`（÷16）换算成 rem（1.0 / 0.875 / 0.75 / 0.625），`Ui`/`Editor` 两档跟随用户设置；doc 注释中的「0.825rem」与代码计算值 0.875 不一致，以代码为准。
- 缩放机制：源码中不存在 `ui_scale` 设置；`theme_settings::setup_ui_font` 把窗口 rem 基准设为 `ui_font_size`，所有 rem 尺寸（文字、间距、图标）随之整体缩放，组件代码无需任何改动。

## 7. 下一步学习建议

- 下一讲 **u2-l3 DynamicSpacing 与 UI 密度**：把「尺寸语义化」从字号推广到间距——三档 UI 密度如何生成 `DynamicSpacing` 枚举，与本讲的 `TextSize` 档位互为映照。
- 若想先横向看看「语义键 + 渲染期解析」在尺寸上的另一处应用，可预习 [src/components/icon.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/icon.rs) 中 `IconSize` 的定义（u4-l1 精读）。
- rem 缩放的更底层机制（`WithRemSize` 如何为局部子树覆写 rem 基准）留到 **u8-l3**；本讲只需记住换算式 \[ \text{px} = \text{rem} \times \text{窗口 rem 基准} \]。
- 建议顺手通读 [label.rs 的 preview 部分](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label.rs#L363-L421)，它是官方认可的用法清单，也为 u8-l5 的组件注册体系做好铺垫。
