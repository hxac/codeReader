# 编辑器渲染:Element 与绘制管线

## 1. 本讲目标

学完本讲,你应该能够:

1. 说出 `EditorElement` 的三阶段生命周期(`request_layout` → `prepaint` → `paint`)各自完成什么工作,以及文本绘制的「两遍」结构(先 `prepaint` 定位、后 `draw` 上屏)。
2. 解释编辑器如何圈定「可见行范围」并配合内容掩码(content mask)做视口裁剪,理解为什么滚动十万行的文件依然流畅。
3. 理解高亮样式如何在 `highlighted_chunks` 中合成(语法 + 自定义高亮 + 诊断),并掌握 gutter(行号区)的绘制入口。
4. 掌握 `git_gutter_width` 设置的新形态:从 `Option<f32>` 重构为 `GitGutterWidth` 枚举(`Default` 随字号缩放 / `Custom` 固定像素),以及 `element.rs` 中两处宽度匹配逻辑的变化。
5. 描述鼠标像素坐标如何经 `PositionMap::point_for_position` 反查回 `DisplayPoint`,完成命中测试。
6. 定位「自定义绘制(如行高亮装饰)」应该修改的代码位置,并能动手实现一个不影响滚动性能的装饰效果。

## 2. 前置知识

本讲是编辑器单元(第五单元)的第 4 讲,默认你已读过:

- **u2-l3(Element 与 Render)**:GPUI 元素的三阶段上屏流程——`request_layout`(布局)、`prepaint`(预绘制)、`paint`(绘制)。本讲的 `EditorElement` 是这套机制迄今最复杂的实现,我们会不断回用这三个概念。
- **u5-l1(Editor 总览)**:`Editor` 是约 250 个字段的巨型实体,渲染统一读取不可变的 `EditorSnapshot`;`EditorSettings` 是实例级开关为 `None` 时的全局回落。
- **u5-l3(DisplayMap)**:Buffer 坐标要经过折叠、换行、制表符、inlay、块这多层变换才成为屏幕坐标;屏幕坐标体系是 `DisplayPoint`/`DisplayRow`。本讲只消费 `EditorSnapshot`,不再展开变换细节。

再补充两个本讲反复出现的术语:

- **像素与行高的换算**:编辑器是「等高行」布局,第 \( r \) 个屏幕行左上角的 y 坐标就是 \( r \times \text{line\_height} - \text{scroll\_top} \)。几乎所有装饰(行高亮、gutter 条、光标)的定位都基于这一个公式。
- **`paint_quad` / `paint_layer`**:GPUI 提供的底层绘制原语。`paint_quad` 画一个矩形(填充色、边框、圆角),`paint_layer` 开启一个合成层,让层内的绘制整体叠加。编辑器里几乎所有非文本装饰最终都落到这两个调用上。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/editor/src/element.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs) | 本讲主战场。`EditorElement` 实现 GPUI `Element` trait,包含布局、可见行圈定、全部 `paint_*` 绘制函数、`PositionMap` 命中测试,以及 `LineWithInvisibles` 文本行的两遍绘制。约 1.25 万行。 |
| [crates/editor/src/element/mouse.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element/mouse.rs) | 鼠标事件处理。`paint_mouse_listeners` 在绘制期注册鼠标回调,并把 `PositionMap` 克隆进闭包,供事件发生时做像素→文本反查。 |
| [crates/editor/src/display_map/invisibles.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/display_map/invisibles.rs) | 「不可见字符」分类器:判断哪些字符肉眼不可见(零宽空格、控制符等)以及用什么符号替换显示。是高亮样式合成链路的下游消费者。 |
| [crates/editor/src/display_map.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/display_map.rs) | 只读其中 `highlighted_chunks`:把语法高亮、自定义高亮、诊断样式合成为 `HighlightedChunk` 流,是 `layout_lines` 的输入。 |
| [crates/editor/src/editor_settings.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_settings.rs) | `EditorSettings` 定义。本讲关注 `Gutter` 结构中 `git_gutter_width` 字段的类型变化。 |
| [crates/settings_content/src/editor.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings_content/src/editor.rs) | 设置文件的内容模型,`GitGutterWidth` 枚举在此定义并被 re-export。 |
| [assets/settings/default.json](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/assets/settings/default.json) | 默认设置,`git_gutter_width` 默认值为 `"default"`。 |

## 4. 核心概念与源码讲解

### 4.1 两阶段渲染:布局、预绘制与绘制

#### 4.1.1 概念说明

`Editor` 实体(状态)如何变成屏幕上的像素?答案是 u2-l3 学过的 `Render` 循环:每帧调用 `Editor::render()` 构造一个 `EditorElement`,随后 GPUI 依次驱动它的 `request_layout`、`prepaint`、`paint` 三个阶段。

但编辑器比普通组件复杂得多:它要处理滚动、行号、gutter、粘性头部、minimap、滚动条、诊断块……所以 Zed 把工作进一步划分:

- **`request_layout`:只回答「我占多大空间」**。按 `EditorMode`(单行输入框 / 自适应高度 / minimap / 完整编辑器)给出尺寸约束,不算内容。
- **`prepaint`:做所有重活**。取快照、量字体、算 gutter 宽度、定换行宽度、处理自动滚动、圈定可见行、对所有可见行做文本整形(shaping)、布局光标/选区/诊断等一切子元素,产出一份 `EditorLayout`。
- **`paint`:按序上屏**。拿着 `EditorLayout` 逐层调用 `paint_background`、`paint_line_numbers`、`paint_text`……只画不算。

为什么要这样分?因为 GPUI 的契约是「布局信息在 prepaint 后固定,paint 只消费」——编辑器把全部中间结果(整好形的行、布局好的选区)存进 `EditorLayout`,paint 阶段就不再访问可变状态,逻辑清晰且可复用(例如鼠标命中测试复用的 `PositionMap` 就来自这份布局)。

文本行内部还有一次「小两阶段」:每个 `LineWithInvisibles` 先 `prepaint`(推进 x 坐标、预绘制行内嵌元素),再 `draw`(把文本片段 `ShapedLine::paint` 上屏)。

> 术语说明:本讲规格沿用了旧版代码里的说法「`paint_for_layout`/`paint` 两次遍历」。在当前 HEAD 中,GPUI 的 `ShapedLine` 上已没有 `paint_for_layout` 这个方法,对应机制演化为 `LineWithInvisibles::prepaint` + `LineWithInvisibles::draw`——先定位、后绘制的精神不变,命名已统一进三阶段模型。本讲按当前源码讲解。

#### 4.1.2 核心流程

```text
Editor::render() 每帧构造 EditorElement { editor, style, split_side }
        │
        ▼
request_layout      按 EditorMode 决定尺寸:
                    SingleLine → 固定一行高
                    AutoHeight → request_measured_layout 按内容测高
                    Full       → 宽高各占 100%(父容器决定)
        │
        ▼
prepaint(重活)
    ├─ editor.snapshot() 取不可变快照
    ├─ 量字体:line_height / em_width / em_advance
    ├─ gutter_dimensions(行号位数、blame 宽度…)→ text_width → 换行宽度
    ├─ 插入 3 个 hitbox(editor / gutter / text)
    ├─ 自动滚动 + 圈定可见行 start_row..end_row(见 4.2)
    ├─ layout_lines:highlighted_chunks → shape_line → Vec<LineWithInvisibles>
    ├─ prepaint_lines:逐行推进坐标、预绘制行内元素
    ├─ layout_selections / layout_cursors / layout_blocks / …
    └─ 组装 EditorLayout(含 Rc<PositionMap>)
        │
        ▼
paint(按序上屏)
    ├─ paint_mouse_listeners(注册鼠标回调,克隆 PositionMap)
    ├─ paint_background        ← 行级背景、当前行高亮、换行参考线
    ├─ paint_indent_guides
    ├─ paint_blamed_display_rows + paint_line_numbers
    ├─ paint_text
    │     ├─ paint_lines_background   ← 行内背景段
    │     ├─ paint_highlights         ← 选区/高亮矩形
    │     ├─ paint_lines              ← 真正画文本(LineWithInvisibles::draw)
    │     ├─ paint_redactions / paint_cursors / 内联诊断 / 内联 blame …
    ├─ gutter 指示器、块元素、粘性头部、minimap、滚动条
    └─ 弹层(编辑预测、鼠标右键菜单)
```

#### 4.1.3 源码精读

**入口结构体**——`EditorElement` 只有三份只读数据:目标实体、样式、分屏侧(用于 diff 视图左右分栏时给删改条着不同颜色):

[crates/editor/src/element.rs:244-248](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L244-L248)

```rust
pub struct EditorElement {
    editor: Entity<Editor>,
    style: EditorStyle,
    split_side: Option<SplitSide>,
}
```

**request_layout:只管尺寸**。注意 `Full` 模式下若 `SizingBehavior::SizeByContent`,高度直接用「最大屏幕行数 × 行高」算出,这是把编辑器嵌进自动高度容器(如搜索结果列表)时的路径:

[crates/editor/src/element.rs:7909-7982](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L7909-L7982)

关键片段(`Full` 分支):

```rust
EditorMode::Full { sizing_behavior, .. } => {
    let mut style = Style::default();
    style.size.width = relative(1.).into();
    if sizing_behavior == SizingBehavior::SizeByContent {
        let snapshot = editor.snapshot(window, cx);
        let line_height = self.style.text.line_height_in_pixels(window.rem_size());
        let scroll_height =
            (snapshot.max_point().row().next_row().0 as f32) * line_height;
        style.size.height = scroll_height.into();
    } else {
        style.size.height = relative(1.).into();
    }
    window.request_layout(style, None, cx)
}
```

**prepaint:量字体与 gutter**。进入 `prepaint` 后首先套上文本样式与内容掩码,再解析字体、量出 `line_height`/`em_width`,并向快照询问 gutter 尺寸(行号宽度由文件最大行号位数决定,详见 4.3.3):

[crates/editor/src/element.rs:8009-8029](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L8009-L8029)

```rust
let rem_size = window.rem_size();
let font_id = window.text_system().resolve_font(&style.text.font());
let font_size = style.text.font_size.to_pixels(rem_size);
let line_height = style.text.line_height_in_pixels(rem_size);
let em_width = window.text_system().em_width(font_id, font_size).unwrap();
...
let gutter_dimensions =
    snapshot.gutter_dimensions(font_id, font_size, style, window, cx);
let text_width = bounds.size.width - gutter_dimensions.width;
```

接着根据是否软换行设置换行宽度,并把可见行数/列数写回 `Editor`(供分页滚动、视口相关逻辑使用):

[crates/editor/src/element.rs:8060-8088](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L8060-L8088)

**prepaint:文本整形**。可见行范围确定后(4.2 详解),`layout_lines` 把快照的 `highlighted_chunks`(带样式合成的文本块流)整形成 `LineWithInvisibles`——这就是「一行的布局结果」:

[crates/editor/src/element.rs:3064-3138](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L3064-L3138)

```rust
let chunks = snapshot.highlighted_chunks(rows.clone(), language_aware, style);
LineWithInvisibles::from_chunks(
    chunks, style, MAX_LINE_LEN, rows.len(),
    &snapshot.mode, editor_width,
    is_row_soft_wrapped, bg_segments_per_row, window, cx,
)
```

`LineWithInvisibles` 的结构——片段列表(整形后的文本或行内元素)、不可见字符标记、总宽:

[crates/editor/src/element.rs:7043-7049](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L7043-L7049)

```rust
pub(crate) struct LineWithInvisibles {
    fragments: SmallVec<[LineFragment; 1]>,
    invisibles: Vec<Invisible>,
    len: usize,
    pub(crate) width: Pixels,
    font_size: Pixels,
}
```

随后 `prepaint_lines` 逐行调用 `line.prepaint(...)`,用「行号 − 滚动位置」算出该行的 y,推进片段 x 坐标,并把行内嵌元素(如 inlay 图标)预绘制后收集到 `line_elements`:

[crates/editor/src/element.rs:3140-3166](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L3140-L3166)

[crates/editor/src/element.rs:7420-7452](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L7420-L7452)

```rust
fn prepaint_with_custom_offset(...) {
    let mut fragment_origin =
        content_origin + gpui::point(Pixels::from(-scroll_pixel_position.x), line_y);
    for fragment in &mut self.fragments {
        match fragment {
            LineFragment::Text(line) => { fragment_origin.x += line.width; }
            LineFragment::Element { element, size, .. } => {
                let mut element = element.take()
                    .expect("you can't prepaint LineWithInvisibles twice");
                element.prepaint_at(element_origin, window, cx);
                line_elements.push(element);
                fragment_origin.x += size.width;
            }
        }
    }
}
```

注意 `expect("you can't prepaint LineWithInvisibles twice")`——行内元素被 `take()` 走了,一行只能 prepaint 一次,这是防重复布局的哨兵。

**paint:按序上屏**。`paint` 先注册输入处理与全部 action(键盘交互的注册点就在这里,不是在别处),再按固定顺序层层绘制:

[crates/editor/src/element.rs:9469-9572](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L9469-L9572)

绘制顺序摘要(层从下到上):

```rust
self.paint_mouse_listeners(layout, window, cx);   // 注册鼠标回调
...
self.paint_background(layout, window, cx);        // 当前行高亮、行级背景
self.paint_indent_guides(layout, window, cx);
self.paint_blamed_display_rows(layout, window, cx);
self.paint_line_numbers(layout, window, cx);
self.paint_text(layout, window, cx);              // 选区→文本→光标→内联元素
self.paint_spacer_blocks(...);                    // 块元素
self.paint_gutter_highlights(...);                // git diff 条等
self.paint_gutter_indicators(...);                // 折叠箭头、断点等
self.paint_non_spacer_blocks(...);
self.paint_sticky_headers(layout, window, cx);    // 粘性滚动头部
self.paint_minimap(layout, window, cx);
self.paint_scrollbars(layout, window, cx);
```

**paint_text:文本相关的总入口**。它先把绘制范围掩码到文本区 hitbox,再依次画行背景、高亮/选区、文本、遮蔽、光标和内联元素:

[crates/editor/src/element.rs:5533-5593](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L5533-L5593)

```rust
fn paint_text(&mut self, layout: &mut EditorLayout, window: &mut Window, cx: &mut App) {
    window.with_content_mask(Some(ContentMask {
        bounds: layout.position_map.text_hitbox.bounds,
    }), |window| {
        ...
        self.paint_lines_background(layout, window, cx);
        let invisible_display_ranges = self.paint_highlights(layout, window, cx);
        self.paint_document_colors(layout, window);
        self.paint_lines(&invisible_display_ranges, layout, window, cx);
        self.paint_redactions(layout, window);
        ...
        self.paint_cursors(layout, window, cx);
        ...
    })
}
```

顺序有讲究:选区高亮(`paint_highlights`)画在文本(`paint_lines`)**之下**,所以文字永远盖在选区色上;光标画在文本**之上**。

**第二遍:draw**。`paint_lines` 遍历 `line_layouts`,逐行调用 `draw`:

[crates/editor/src/element.rs:5643-5674](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L5643-L5674)

`draw_with_custom_offset` 是真正调用字体系统上屏的地方:

[crates/editor/src/element.rs:7477-7513](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L7477-L7513)

```rust
for fragment in &self.fragments {
    match fragment {
        LineFragment::Text(line) => {
            line.paint(fragment_origin, line_height, layout.text_align,
                       Some(layout.content_width), window, cx)
                .log_err();
            fragment_origin.x += line.width;
        }
        LineFragment::Element { size, .. } => { fragment_origin.x += size.width; }
    }
}
self.draw_invisibles(...);  // 画不可见字符替换符号
```

#### 4.1.4 代码实践

**实践:用日志观察一帧的绘制顺序**

1. **实践目标**:验证 `paint` 内各 `paint_*` 的调用顺序与本讲流程图一致,并直观感受「每帧只处理可见行」。
2. **操作步骤**(在你自己的克隆中做,本讲义不改动任何源码):
   - 打开 `crates/editor/src/element.rs`,定位 `paint` 方法([element.rs:9469](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L9469))。
   - 在 `paint_background`、`paint_text`、`paint_scrollbars` 三个调用前各临时加一条 `eprintln!`(示例代码):

     ```rust
     // 示例代码:临时调试日志,观察后删除
     eprintln!("[frame] visible rows: {:?}", layout.visible_display_row_range);
     ```

   - 重新 `cargo run`(debug 构建即可),打开一个几百行的文件,上下滚动几次。
3. **需要观察的现象**:终端持续输出可见行区间,且滚动时区间随滚动位置平移;区间长度约等于窗口能容纳的行数,与文件总行数无关。
4. **预期结果**:每次光标移动/滚动都会触发若干帧输出;`paint_background` 的日志总是先于 `paint_text`,`paint_text` 先于 `paint_scrollbars`。
5. 观察完成后删除日志。若你暂时无法本地构建,可改为纯阅读:对照 [element.rs:9528-9568](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L9528-L9568) 手绘调用顺序图,**运行现象待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `EditorElement` 要在 `prepaint` 里就把所有行整形(shape)完毕,而不是等到 `paint` 再做?

**答案**:三阶段契约要求布局结果在 prepaint 后固定。更重要的是,布局产物(`EditorLayout` 内的 `PositionMap`)要被随后的鼠标命中测试复用(4.4)——鼠标事件发生时元素已经销毁,只有提前算好的「像素↔文本」映射能回答点击落在哪个字符。此外把纯计算与绘制分离,也让 `paint` 不持有可变借用、逻辑单一。

**练习 2**:`LineWithInvisibles::prepaint_with_custom_offset` 里为什么会 `expect("you can't prepaint LineWithInvisibles twice")`?

**答案**:行内嵌元素(`LineFragment::Element`)存放的是 `Option<AnyElement>`,prepaint 时被 `take()` 走并交给 `line_elements` 统一管理。如果同一行被 prepaint 两次,第二次 `take()` 拿到 `None`,说明上游流程出现了重复布局——这里用 panic 快速暴露 bug,而不是静默画错。

**练习 3**:选区色和文本谁先画?依据哪段代码?

**答案**:选区先画、文本后画(文本盖在选区色上)。依据 [element.rs:5575-5578](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L5575-L5578):`paint_lines_background` → `paint_highlights`(选区)→ `paint_lines`(文本)的调用顺序。

### 4.2 可见行圈定与视口裁剪

#### 4.2.1 概念说明

编辑器经常要显示几十万行的文件,但屏幕一次只能显示几十行。如果每帧都对整个文档做文本整形,滚动会卡到不可用。Zed 的解法有两层:

1. **逻辑层圈定**:prepaint 时算出 `start_row..end_row`,只整形、只布局这个区间内的行。
2. **绘制层裁剪**:用 GPUI 的内容掩码(`ContentMask`)把超出自身边界的绘制裁掉。这一层解决的是「元素实际比父容器大」的场景——例如把内容尺寸(content-sized)的编辑器放进虚拟列表(`List`),列表只给它一个视口大的掩码,编辑器据此进一步缩小圈定范围。

两层结合,编辑器的每帧成本与「可见行数」成正比,与文件大小无关。

#### 4.2.2 核心流程

滚动位置 `scroll_position.y` 是以「行」为单位的浮点数,整数部分是窗口顶部的屏幕行号。可见行范围:

\[
\text{start\_row} = \min\big(\lfloor \text{scroll\_y} + \text{clipped\_top\_lines} \rfloor,\ \text{max\_row}\big)
\]

\[
\text{end\_row} = \min\big(\lceil \text{scroll\_y} + \text{clipped\_top\_lines} + \text{visible\_height\_lines} \rceil,\ \text{max\_row} + 1\big)
\]

其中 `clipped_top_lines` 是被父容器裁掉的顶部行数(处理编辑器嵌在虚拟列表里的情况),向下取整/向上取整保证半露出的行也被画到。

#### 4.2.3 源码精读

**prepaint 的内容掩码**——进入 prepaint 第一件事就是把自己限制在分配到的 `bounds` 内:

[crates/editor/src/element.rs:8010-8015](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L8010-L8015)

```rust
window.with_rem_size(rem_size, |window| {
    window.with_text_style(Some(text_style), |window| {
        window.with_content_mask(Some(ContentMask { bounds }), |window| {
            let (mut snapshot, is_read_only) = ...
```

**计算被裁剪的顶部与可见高度**——`window.content_mask().bounds` 是「祖先允许我画的区域」,与自己 bounds 求交集:

[crates/editor/src/element.rs:8111-8120](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L8111-L8120)

```rust
// Calculate how much of the editor is clipped by parent containers (e.g., List).
// This allows us to only render lines that are actually visible, which is
// critical for performance when large content-sized editors are inside Lists.
let visible_bounds = window.content_mask().bounds;
let visible_top = bounds.top().max(visible_bounds.top());
let visible_bottom = bounds.bottom().min(visible_bounds.bottom());
let clipped_top = (visible_top - bounds.top()).max(px(0.));
let visible_height = (visible_bottom - visible_top).max(px(0.));
let clipped_top_in_lines = f64::from(clipped_top / line_height);
let visible_height_in_lines = f64::from(visible_height / line_height);
```

注释原文就点明了动机:内容尺寸编辑器放进 `List` 时,必须只渲染真正可见的行。

**圈定 start_row / end_row**:

[crates/editor/src/element.rs:8169-8188](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L8169-L8188)

```rust
// The scroll position is a fractional point, the whole number of which represents
// the top of the window in terms of display rows.
let max_row = snapshot.max_point().row();
let start_row = cmp::min(
    DisplayRow((scroll_position.y + clipped_top_in_lines).floor() as u32),
    max_row,
);
let end_row = cmp::min(
    (scroll_position.y + clipped_top_in_lines + visible_height_in_lines).ceil()
        as u32,
    max_row.next_row().0,
);
let end_row = DisplayRow(end_row);

let row_infos = snapshot // note we only get the visual range
    .row_infos(start_row)
    .take((start_row..end_row).len())
    .collect::<Vec<RowInfo>>();
```

`row_infos` 只取可见区间的行信息(diff 状态、buffer 行号等),源码注释 `note we only get the visual range` 再次强调这一点。这两个行号最终存进 `EditorLayout.visible_display_row_range`([element.rs:9420](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L9420)),后续所有按行遍历的绘制都基于它。

**绘制侧的两处裁剪**:

- `paint` 整体再次掩码到 `bounds`([element.rs:9503](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L9503)),粘性头部之下还有一层额外掩码,保证粘性头部不与正文重叠([element.rs:9506-9528](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L9506-L9528))。
- `paint_text` 掩码到文本区 hitbox,行号与正文互不越界([element.rs:5534-5537](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L5534-L5537))。

**极端长行的保护**:超长行(如压缩后的 JS)按 `MAX_LINE_LEN` 截断整形([element.rs:3125-3136](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L3125-L3136) 传入 `from_chunks`),避免单行整形拖垮整帧——这是纵向裁剪之外横向的保险。

#### 4.2.4 代码实践

**实践:验证「只整形可见行」**

1. **实践目标**:用数据证明滚动大文件时,每帧参与布局的行数 ≈ 窗口行数,而非文件总行数。
2. **操作步骤**:
   - 在 [element.rs:8183](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L8183) 的 `let end_row = DisplayRow(end_row);` 之后临时加(示例代码):

     ```rust
     // 示例代码:临时调试日志,观察后删除
     eprintln!("[viewport] rows {}..={} (len {}), file max_row {}",
         start_row.0, end_row.0, (start_row..end_row).len(), max_row.0);
     ```

   - 准备一个大文件(例如 `wc -l` 不少于 5 万行),用你构建出的 Zed 打开。
   - 按住 `PageDown` 连续翻页,观察终端输出。
3. **需要观察的现象**:每帧输出的 `len` 恒定在一个小数字(约等于窗口高度 / 行高,可能 +1~2);`file max_row` 保持几万;`rows` 区间随滚动整体平移。
4. **预期结果**:翻到文件中部时,例如输出 `rows 30001..=30048 (len 48), file max_row 50000`——每帧成本与 48 行成正比。这解释了为什么大文件滚动依旧流畅。
5. 若无法本地运行,可改为跟踪 `visible_display_row_range` 的全部消费方(`Grep visible_display_row_range`)验证所有绘制路径都限制在该区间内,运行现象**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**:`start_row` 用 `floor`、`end_row` 用 `ceil`,换成反过来会发生什么?

**答案**:反过来(开头向上取整、结尾向下取整)会把半露出的行排除在外:滚动停在分数位置时,窗口顶部/底部会出现一条画不出内容的空白带。当前取法宁可多画一行,也不留缝。

**练习 2**:已经有了逻辑层的 `start_row..end_row` 圈定,为什么 `paint_text` 还要再套一层 `ContentMask`?

**答案**:两者覆盖的场景不同。行圈定针对「编辑器自身的滚动」;内容掩码针对「编辑器被祖先容器裁剪」——比如把内容尺寸编辑器放进虚拟列表,编辑器名义上高度是全文档,但真正可见的只有列表视口。此外掩码还能防止任何意外越界的 `paint_quad`(比如行尾超画)污染相邻 UI。双保险各管一层。

**练习 3**:编辑器如何知道「自己被父容器裁掉了多少」?

**答案**:读 `window.content_mask().bounds`。见 [element.rs:8114-8120](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L8114-L8120):用编辑器 bounds 与当前掩码求交,得出 `clipped_top_in_lines` 与 `visible_height_in_lines`,并把裁掉行数加进 `scroll_position.y` 参与行圈定(注释明确说明不修改 `scroll_position` 本身,因为定位由父容器负责)。

### 4.3 高亮样式合成与 gutter 绘制(GitGutterWidth 枚举)

#### 4.3.1 概念说明

**高亮样式合成**回答的问题是:某个字符该用什么颜色、什么下划线?答案来自三个独立来源的叠加:

1. **语法高亮**(tree-sitter / 语义 token):`chunk.syntax_highlight_id` 查主题得到。
2. **自定义高亮**(搜索匹配、bracket 匹配等编辑器叠加层):`chunk.highlight_style`。
3. **诊断**(错误波浪线、多余代码淡化):`chunk.diagnostic_severity`。

三者各自转成 `HighlightStyle` 后用 `reduce` 合并,最终交给 `layout_lines` 整形为带 `TextRun` 的行。这条链路横跨 prepaint(合成)与 paint(上屏)。

**gutter 绘制**则是行号区的全部视觉:行号、git diff 增删条、断点、折叠箭头、blame 条。本模块同时讲清本讲的「更新动因」:`git_gutter_width` 设置刚从 `Option<f32>`(旧:`Some(像素)` 自定义 / `None` 默认)重构为语义更清晰的枚举:

```rust
// crates/settings_content/src/editor.rs:499-505
pub enum GitGutterWidth {
    /// Width scales automatically with the buffer font size.
    #[default]
    Default,
    /// A fixed pixel width for the git diff indicators.
    Custom(crate::PixelSetting),
}
```

`Default` 随行高(字号)缩放——因为默认宽度本来就定义为「行高的 0.275 倍」,不是一个常量,旧版用 `None` 表达「非常量」容易误解为「未设置」;枚举让两种意图都显式化。设置文件里对应 `"default"` 或 `{"custom": <像素>}`,默认值为 `"default"`([assets/settings/default.json:708](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/assets/settings/default.json#L708))。

#### 4.3.2 核心流程

高亮合成(发生在 prepaint 的 `layout_lines` 内):

```text
snapshot.chunks(range)               →  原始文本块(带 syntax id / 诊断 / 自定义样式)
  ↓ highlighted_chunks()
每块:语法样式 ┐
     块样式   ├→ reduce(highlight 合并) → Option<HighlightStyle>
     诊断样式 ┘
  ↓ highlight_invisibles()
切分不可见字符,插入替换样式(HighlightedChunk 流)
  ↓ LineWithInvisibles::from_chunks()
shape_line(文本 + TextRun[]) → 每行的片段与宽度
```

gutter 宽度(发生在 prepaint,由快照方法计算):

```text
行号位数(min_line_number_digits 保底) → line_gutter_width
+ 左侧(断点/运行按钮/bookmark/blame 占位)
+ 右侧(折叠箭头占位)
= gutter_dimensions.width → text_width = 总宽 − gutter 宽
```

git diff 条宽度(发生在 paint,按设置取值):

\[
w = \begin{cases} \text{custom 像素} & \text{GitGutterWidth::Custom} \\ \lfloor 0.275 \times \text{line\_height} \rfloor & \text{GitGutterWidth::Default(增改条)} \\ \lfloor 0.35 \times \text{line\_height} \rfloor & \text{GitGutterWidth::Default(删除条)} \end{cases}
\]

#### 4.3.3 源码精读

**样式合流**——三种样式 `[syntax, chunk, diagnostic].into_iter().flatten().reduce(...)`:

[crates/editor/src/display_map.rs:1846-1944](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/display_map.rs#L1846-L1944)

核心片段:

```rust
let syntax_highlight_style = chunk.syntax_highlight_id
    .and_then(|id| editor_style.syntax.get(id).cloned());
let chunk_highlight = chunk.highlight_style.map(|chunk_highlight| { ... });
let diagnostic_highlight = ... // 按严重程度生成 fade_out / wavy underline

let style = [
    syntax_highlight_style,
    chunk_highlight,
    diagnostic_highlight,
]
    .into_iter()
    .flatten()
    .reduce(|acc, highlight| acc.highlight(highlight));

HighlightedChunk {
    text: chunk.text,
    style,
    ...
}
.highlight_invisibles(editor_style)
```

诊断下划线是波浪线,且「非必要代码」(is_unnecessary)会带 `fade_out` 淡化;行内 inlay 会继承当前诊断下划线(用一个跨块状态 `current_diagnostic_underline` 补齐 inlay 内的样式)。

**不可见字符的下游处理**——`highlight_invisibles` 在字符流里探测不可见字符并拆块:

[crates/editor/src/display_map.rs:1418-1459](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/display_map.rs#L1418-L1459)

```rust
for (offset, ch) in text.char_indices() {
    if !is_invisible(ch) { continue; }
    ...
    let invisible_highlight = HighlightStyle {
        background_color: Some(editor_style.status.hint_background),
        underline: Some(UnderlineStyle {
            color: Some(editor_style.status.hint),
            thickness: px(1.),
            wavy: false,
            ...
```

`is_invisible` / `replacement` 的判据在 [crates/editor/src/display_map/invisibles.rs:36-61](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/display_map/invisibles.rs#L36-L61):ASCII 控制字符(除 `\t` `\n` `\r`)、Unicode 格式字符(零宽连接符、BOM 等)与部分「看起来像空格」的字符被判定为不可见;控制字符替换为 `␀`-`␟` 系列、其余替换为定宽空格,少数参与组合字素的字符保留原样。文件头部长注释完整记录了这套启发式。

**gutter 总宽**——行号宽度按文件最大行号位数预留,且用 `min_line_number_digits` 设置保底以避免位数增长时 gutter 抖动:

[crates/editor/src/editor.rs:11709-11718](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor.rs#L11709-L11718)

```rust
let line_gutter_width = if show_line_numbers {
    // Avoid flicker-like gutter resizes when the line number gains another digit by
    // only resizing the gutter on files with > 10**min_line_number_digits lines.
    let min_width_for_number_on_gutter =
        ch_advance * gutter_settings.min_line_number_digits as f32;
    self.max_line_number_width(style, window)
        .max(min_width_for_number_on_gutter)
} else {
    0.0.into()
};
```

**git diff 条宽度——本次重构的核心变化点**。`gutter_strip_width` 现在按枚举匹配:

[crates/editor/src/element.rs:5322-5327](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L5322-L5327)

```rust
fn gutter_strip_width(line_height: Pixels, cx: &App) -> Pixels {
    match EditorSettings::get_global(cx).gutter.git_gutter_width {
        GitGutterWidth::Custom(width) => px(*width),
        GitGutterWidth::Default => (0.275 * line_height).floor(),
    }
}
```

第二处在 `diff_hunk_bounds` 的「空删除块」分支(删除行在屏幕上没有对应行,画一个居中的圆角小块,宽度系数更大):

[crates/editor/src/element.rs:5355-5370](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L5355-L5370)

```rust
if status.is_deleted() && display_row_range.is_empty() {
    ...
    let width = match EditorSettings::get_global(cx).gutter.git_gutter_width {
        GitGutterWidth::Custom(width) => px(*width),
        GitGutterWidth::Default => (0.35 * line_height).floor(),
    };
```

对比旧代码(提交 a7d74150ac 之前):

```rust
// 旧写法(已不存在):用 Option 表达「默认/自定义」
match EditorSettings::get_global(cx).gutter.git_gutter_width {
    Some(width) => px(width),
    None => (0.275 * line_height).floor(),
}
```

配套地,`EditorSettings::Gutter` 字段类型从 `Option<f32>` 改为 `settings::GitGutterWidth`,在设置合并时用 `gutter.git_gutter_width.unwrap()` 直接落到默认值([crates/editor/src/editor_settings.rs:149-153](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_settings.rs#L149-L153));内容模型中是 `Option<GitGutterWidth>` 以支持分层覆盖([crates/settings_content/src/editor.rs:539](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/settings_content/src/editor.rs#L539))。旧用户的数值型设置由 migrator 的 `m_2026_08_17` 迁移改写(设置迁移机制属 u7-l1 的范围,这里只需知道行为)。

**diff 条的绘制**:`paint_gutter_diff_hunks` 按 hunk 状态选色(新增绿/修改黄/删除红;左右分屏 diff 视图按 `split_side` 强制红/绿),并把颜色与编辑器背景混合以防透明主题下「穿透」:

[crates/editor/src/element.rs:5214-5320](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L5214-L5320)

```rust
let flattened_background_color = cx
    .theme()
    .colors()
    .editor_background
    .blend(background_color);
```

入口判断在 `paint_gutter_highlights`([element.rs:5466-5478](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L5466-L5478)):只有 `git_gutter` 设置开启(TrackedFiles)才调用。

#### 4.3.4 代码实践

**实践:亲手切换 `git_gutter_width` 的两种取值**

1. **实践目标**:直观感受 `GitGutterWidth::Default`(随字号缩放)与 `Custom`(固定像素)的差异,并理解设置如何流到 `element.rs` 的两处 `match`。
2. **操作步骤**:
   - 打开一个有未提交改动的 git 仓库文件,确保 gutter 出现增/删/改色条。
   - 打开用户设置文件(`~/.config/zed/settings.json`,macOS 为 `~/Library/Application Support/Zed/settings.json`),依次尝试:

     ```json
     // 取值一:默认,随字号缩放
     "gutter": { "git_gutter_width": "default" }

     // 取值二:固定 6 像素
     "gutter": { "git_gutter_width": { "custom": 6 } }
     ```

   - 保存设置后观察色条宽度变化(Zed 会热加载设置,无需重启)。
   - 再调整 `"buffer_font_size"`(如 12 → 20),分别在两种取值下观察:取值一的色条随行高变宽,取值二保持 6px。
   - 若你的设置文件里残留旧版数值写法(如 `"git_gutter_width": 3`),保存后会被 migrator 自动改写成新格式,可在迁移后再打开该文件确认。
3. **需要观察的现象**:取值一时色条宽度 ≈ 行高 × 0.275(删除块 ≈ 行高 × 0.35);取值二时恒为你填的像素值。
4. **预期结果**:两种取值切换立即生效;`Default` 模式下改字号,色条与行号同步缩放。若你使用的是已安装的官方发行版且其版本早于本提交,则只支持旧数值写法,新枚举写法**待本地验证**(以你本地构建的 HEAD 为准)。
5. 阅读延伸:用 `Grep 'git_gutter_width'` 全仓搜索,跟踪 `settings_content`(定义)→ `editor_settings`(落值)→ `element.rs`(消费)→ `migrator`(旧值迁移)四站。

#### 4.3.5 小练习与答案

**练习 1**:为什么删除行的默认宽度系数(0.35)比增改行(0.275)大?

**答案**:删除块出现在 `display_row_range.is_empty()` 的分支——被删的行在屏幕上没有任何行可占据,只能在两行之间画一个居中的小色块示意(见 [element.rs:5355-5370](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L5355-L5370) 的 `offset = line_height / 2`),稍宽一点让这个「孤儿块」在视觉上不至过于纤细。这属于视觉打磨,非协议约束。

**练习 2**:语法高亮、自定义高亮、诊断三者同时命中同一字符,最终样式怎么定?

**答案**:三者转为 `HighlightStyle` 后 `reduce(|acc, h| acc.highlight(h))` 逐项合并([display_map.rs:1925-1932](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/display_map.rs#L1925-L1932))——`highlight` 合并语义是「后者覆盖前者已设置的字段」,所以顺序上诊断(最后)优先于块样式优先于语法样式;未设置的字段沿用先前者。

**练习 3**:把 `git_gutter_width` 从 `Option<f32>` 改成枚举,为什么 `EditorSettings` 落值处从「直接透传」变成 `.unwrap()`?

**答案**:内容模型(`GutterContent`)仍用 `Option<GitGutterWidth>` 表达「用户是否覆盖」,分层合并后进入 `EditorSettings` 时应得到确定值——`unwrap()` 在 `#[derive(Default)]` 兜底(`GitGutterWidth::Default`)下取出最终枚举。两套类型的分工:外层 `Option` 管「设置是否提供」,内层枚举管「提供的是哪种模式」。

### 4.4 鼠标命中测试:像素反查文本坐标

#### 4.4.1 概念说明

鼠标事件回调发生时,`EditorElement` 和 `EditorLayout` 早已销毁——事件只带一个窗口像素坐标。怎么知道点击落在了第几行第几列?

答案是**绘制期留影**:prepaint 产出的 `EditorLayout` 里有一份 `Rc<PositionMap>`,paint 注册鼠标回调时把它克隆进闭包([mouse.rs:357-392](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element/mouse.rs#L357-L392))。事件到来时,用这份「当时」的布局做反查。这份数据结构也因此得名 PositionMap——坐标地图。

反查要处理三个不完美:

1. y 落在行高整除边界附近 → 用浮点除法自然取整。
2. x 落在行尾之后(点击行尾右侧空白)→ 列号钳到行尾,但记录「越界量」,虚拟列(俗称 euthanasia/overshoot)支持光标停在行尾之外。
3. 落点对应折叠/inlay 等非普通文本位置 → `clip_point` 按 `Bias::Left`/`Right` 给出前后两个合法点,由 inlay 偏好决定取哪个。

#### 4.4.2 核心流程

```text
MouseDownEvent { position(窗口像素) }
  → position_map.point_for_position(position)
      position − text_bounds.origin          (转为编辑器内坐标)
      row = floor(y / line_height + scroll_y) (行)
      x += scroll_x × em_layout_width         (水平滚动补偿)
      column = line.index_for_x(x)            (二分/线性查整形行,得字符列)
             失败 → 列 = 行长,记录 overshoot
      clip_point(Bias::Left/Right) → previous_valid / next_valid / nearest_valid
  → PointForPosition { previous_valid, next_valid, nearest_valid,
                        exact_unclipped, column_overshoot_after_line_end }
  → mouse_left_down / mouse_moved 等据此改选区
```

核心公式:

\[
\text{row} = \left\lfloor \frac{y}{\text{line\_height}} \right\rfloor + \text{scroll\_y}, \qquad
\text{column} = \text{index\_for\_x}(x + \text{scroll\_x} \cdot \text{em\_layout\_width})
\]

#### 4.4.3 源码精读

**PositionMap:布局的快照存档**——行布局、可见行范围、滚动位置、两个 hitbox、diff hunk 边界都在:

[crates/editor/src/element.rs:10113-10131](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L10113-L10131)

```rust
pub(crate) struct PositionMap {
    pub size: Size<Pixels>,
    pub line_height: Pixels,
    pub scroll_position: gpui::Point<ScrollOffset>,
    ...
    pub visible_row_range: Range<DisplayRow>,
    pub line_layouts: Vec<LineWithInvisibles>,
    pub snapshot: EditorSnapshot,
    ...
    pub text_hitbox: Hitbox,
    pub gutter_hitbox: Hitbox,
    ...
}
```

**point_for_position:反查主体**:

[crates/editor/src/element.rs:10179-10227](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L10179-L10227)

```rust
let position = position - text_bounds.origin;
let y = position.y.max(px(0.)).min(self.size.height);
let x = position.x + (scroll_position.x as f32 * self.em_layout_width);
let row = ((y / self.line_height) as f64 + scroll_position.y) as u32;

let (column, x_overshoot_after_line_end) =
    if let Some(line_index) = row.checked_sub(self.visible_row_range.start.0)
        && let Some(line) = self.line_layouts.get(line_index as usize)
    {
        let alignment_offset = line.alignment_offset(self.text_align, self.content_width);
        let x_relative_to_text = x - alignment_offset;
        if let Some(ix) = line.index_for_x(x_relative_to_text) {
            (ix as u32, px(0.))
        } else {
            (line.len as u32, px(0.).max(x_relative_to_text - line.width))
        }
    } else {
        (0, x)
    };

let mut exact_unclipped = DisplayPoint::new(DisplayRow(row), column);
let previous_valid = self.snapshot.clip_point(exact_unclipped, Bias::Left);
let next_valid = self.snapshot.clip_point(exact_unclipped, Bias::Right);
```

注意三个细节:

- 行号只对**可见范围**内的行反查(`checked_sub` + `line_layouts.get`),范围外退化为第 0 列——因为只有可见行才有整形结果。
- `index_for_x` 失败(点击超过行宽)时列号钳到 `line.len`,越界像素换算成 `column_overshoot_after_line_end`(以 em 为单位),供拖拽选区时保持虚拟列。
- `alignment_offset` 处理 `text_align`(如居中对齐)造成的整行偏移。

**PointForPosition:四个层次的答案**:

[crates/editor/src/element.rs:10133-10149](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L10133-L10149)

```rust
pub struct PointForPosition {
    pub previous_valid: DisplayPoint,
    pub next_valid: DisplayPoint,
    pub nearest_valid: DisplayPoint,
    pub exact_unclipped: DisplayPoint,
    pub column_overshoot_after_line_end: u32,
}
```

`as_valid()` 要求前后两个合法点相等才算「精确命中」;`intersects_selection` 则用 `nearest_valid` 判断点击是否落在某选区内(双击选词后拖拽的判定就靠它)。

**消费端:mouse_moved**——悬停 diff hunk、内联 blame 气泡、gutter 高亮等状态机都从 `point_for_position` 起步:

[crates/editor/src/element/mouse.rs:29-45](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element/mouse.rs#L29-L45)

```rust
let text_hovered = text_hitbox.is_hovered(window);
let gutter_hovered = gutter_hitbox.is_hovered(window);
editor.set_gutter_hovered(gutter_hovered, cx);

let point_for_position = position_map.point_for_position(event.position);
let valid_point = point_for_position.nearest_valid;
```

**hitbox 的注册**(反查的前提)发生在 prepaint 尾声,编辑器/gutter/文本区三块:

[crates/editor/src/element.rs:8090-8101](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L8090-L8101)

```rust
let hitbox = window.insert_hitbox(bounds, HitboxBehavior::Normal);
let gutter_hitbox = window.insert_hitbox(
    gutter_bounds(bounds, gutter_dimensions),
    HitboxBehavior::Normal,
);
let text_hitbox = window.insert_hitbox(
    Bounds { origin: gutter_hitbox.top_right(), size: size(text_width, bounds.size.height) },
    HitboxBehavior::Normal,
);
```

而把这些 hitbox 与事件连接起来的,是 `paint` 开头的 `paint_mouse_listeners`——每个鼠标回调闭包都克隆一份 `Rc<PositionMap>`:

[crates/editor/src/element/mouse.rs:345-392](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element/mouse.rs#L345-L392)

```rust
window.on_mouse_event({
    let position_map = layout.position_map.clone();
    let editor = self.editor.clone();
    ...
    move |event: &MouseDownEvent, phase, window, cx| {
        if phase == DispatchPhase::Bubble {
            match event.button {
                MouseButton::Left => editor.update(cx, |editor, cx| {
                    ...
                    Self::mouse_left_down(editor, event, &position_map, ...);
                }),
                ...
```

#### 4.4.4 代码实践

**实践:手算一次命中,与编辑器行为对照**

1. **实践目标**:用 `point_for_position` 的公式人工推算一个点击落点,验证你对坐标换算的理解。
2. **操作步骤**:
   - 设想(或实测)如下参数:行高 20px,无水平滚动,文本区左上角在窗口 (200, 100),第 3 行(0 基)文本为 `fn main() {`,该行整形宽度 96px。
   - 计算点击 (200 + 150, 100 + 3 × 20 + 5) = (350, 165) 的落点:
     - \( y = 165 - 100 = 65 \),\( \text{row} = \lfloor 65/20 \rfloor + 0 = 3 \);
     - \( x = 350 - 200 = 150 > 96 \),`index_for_x` 失败 → 列 = 行长,overshoot = 150 − 96 = 54px。
   - 在你本地构建的 Zed 中(可用开发者工具或临时日志打印 `text_hitbox.origin` 与 `line_height`)找一组真实参数,点击某行行尾右侧空白处,观察光标位置。
3. **需要观察的现象**:点击第 3 行行尾右侧任意位置,光标都落在该行行尾(列 = 行长),不会跳到下一行。
4. **预期结果**:手算与实际一致;随后向右拖动,选区不立即变长(列被钳住),直到重新进入文本区才变化——overshoot 机制在起作用。
5. 参数取值与实测**待本地验证**;若不做运行,可对照 [editor_tests.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/editor_tests.rs) 中与选区拖拽相关的既有测试阅读断言。

#### 4.4.5 小练习与答案

**练习 1**:为什么鼠标回调里能安全地使用 `line_layouts`,明明元素每帧重建?

**答案**:`PositionMap` 通过 `Rc` 与鼠标闭包共享,生命周期由引用计数托管,元素销毁不影响它。代价是它代表的是「注册回调那一帧」的布局;若两帧之间文档变化,反查结果属于旧布局——GPUI 的按需重绘保证状态变化会触发新帧、注册新回调,窗口期极短。

**练习 2**:`previous_valid`、`next_valid`、`nearest_valid` 三个值什么时候会不相等?

**答案**:当 `exact_unclipped` 落在 `clip_point` 需要移动的位置——典型是折叠区域内部或 inlay 边界:向左钳(Bias::Left)得到折叠前位置,向右钳得到折叠后位置。此时 `nearest_valid` 依 `inlay_bias_at` 的偏好二选一([element.rs:10207-10215](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L10207-L10215))。

**练习 3**:点击行号区(gutter)时,`point_for_position` 的 x 计算还正确吗?

**答案**:不经过同一条路。`point_for_position` 以 `text_hitbox.origin` 为原点,gutter 在其左侧,x 为负、y 仍有效;行号可点击逻辑(如断点、折叠)走的是 `paint_mouse_listeners` 里克隆的 `line_numbers` 布局与各 hitbox 的 `is_hovered` 判断([mouse.rs:357-361](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element/mouse.rs#L357-L361) 同时克隆了两者),文本反查与 gutter 反查是两套路径。

## 5. 综合实践

**任务:给编辑器加一个「光标行下缘强调线」装饰**

现有 `current_line_highlight` 画的是当前行**背景色块**。我们来加一个它没有的效果:在光标所在屏幕行的**下缘**画一条 2px 的主题色横线,并验证它不影响滚动性能。这个任务贯穿本讲三个知识点:装饰画在 `paint_background` 阶段(4.1 的绘制顺序)、坐标由行高公式计算(4.2 的视口换算)、只遍历 `active_rows` 而非全文档(性能边界)。

1. **实践目标**:在 [element.rs](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs) 的渲染链路中新增一个自定义装饰,并论证其每帧成本与文件大小无关。
2. **操作步骤**:
   - 定位 `paint_background`([element.rs:4902-4976](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L4902-L4976))中遍历 `layout.active_rows` 画当前行背景的 `while let Some((start_row, contains_non_empty_selection)) = active_rows.next()` 循环。
   - 在该循环内、背景 `paint_quad` 之后追加(示例代码,非项目原有代码):

     ```rust
     // 示例代码:在光标行下缘画一条强调线
     let line_bottom_y = layout.hitbox.origin.y
         + Pixels::from(
               (end_row.as_f64() + 1.0 - scroll_top)
                   * ScrollPixelOffset::from(layout.position_map.line_height),
           )
         - px(2.);
     window.paint_quad(fill(
         Bounds::new(
             point(layout.position_map.text_hitbox.bounds.left(), line_bottom_y),
             size(layout.position_map.text_hitbox.bounds.size.width, px(2.)),
         ),
         cx.theme().players().local().cursor,
     ));
     ```

     要点说明:`layout.active_rows` 来自 `layout_selections`([element.rs:816-823](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element.rs#L816-L823)),键是屏幕行号;`end_row` 是循环里合并连续行后的结束行;颜色复用主题的本地玩家光标色,无需新增主题键。
   - `cargo run` 重新构建运行,移动光标(上下键、点击、多光标)观察。
3. **需要观察的现象**:
   - 光标所在行(含多光标的多行)下缘出现 2px 横线,随光标移动即时刷新;
   - 软换行的长行中,每个被光标占据的屏幕行都有自己的横线(因为 `active_rows` 以屏幕行为键);
   - 打开数万行大文件持续滚动,横线只在可见的光标行出现,滚动帧率无可感知变化。
4. **预期结果**:功能生效且性能无损。性能论证:新代码在 `active_rows`(光标数量级,通常 ≤ 几十)上循环,每次一个 `paint_quad`,与文档总行数、与可见行数都无关;对比 4.2 实践中每帧仅有的几十行布局成本,可忽略。
   - 编译报错时优先检查:变量名是否与当前 HEAD 一致(`ScrollPixelOffset`、`scroll_top` 均取自 `paint_background` 现有代码)、`end_row` 类型是 `u32` 还是 `DisplayRow`(循环内为 `u32`,需按需转换)。
   - 实际渲染效果**待本地验证**。
5. **收尾**:按仓库规范(`CONTRIBUTING.md`),调试完删除该装饰或整理成正式提交;本讲义的使命是让你掌握「从 element 渲染处入手加装饰」的路径。

## 6. 本讲小结

- `EditorElement` 走 GPUI 三阶段:`request_layout` 只定尺寸,`prepaint` 做快照、量字体、圈可见行、整形文本、布局一切子元素并产出 `EditorLayout`,`paint` 按固定顺序上屏——重计算与绘制彻底分离。
- 文本行内部是「小两阶段」:`LineWithInvisibles::prepaint` 推进坐标并预绘制行内元素,`draw` 调 `ShapedLine::paint` 上屏;旧文档中的 `paint_for_layout` 在当前 HEAD 已并入这套命名。
- 视口裁剪是双层保险:逻辑层用 `start_row..end_row` 只整形可见行(公式含父容器裁剪补偿),绘制层用 `ContentMask` 裁掉越界像素;因此每帧成本与文件大小无关。
- 高亮样式由语法、自定义、诊断三路 `HighlightStyle` 经 `reduce` 合成,再经 `highlight_invisibles` 切分不可见字符(判据在 invisibles.rs 的 Unicode 启发式)。
- gutter 宽度 = 行号位数(带防抖保底)+ 左右功能占位;`git_gutter_width` 本提交起从 `Option<f32>` 变为 `GitGutterWidth` 枚举,`element.rs` 的 `gutter_strip_width`(0.275 系数)与删除块宽度(0.35 系数)两处 `match` 随之改写,`Default` 随行高缩放、`Custom` 固定像素。
- 鼠标命中靠绘制期留下的 `Rc<PositionMap>`:`point_for_position` 用行高公式算行、`index_for_x` 算列,越界钳到行尾并记录 overshoot,`clip_point` 双向钳位给出三层合法点。

## 7. 下一步学习建议

- **补齐第五单元收尾**:如果你是按序学习,接下来进入第六单元语言智能;若想继续深挖编辑器视觉,建议阅读 `paint_cursors`/`layout_visible_cursors`(光标动画与多光标渲染)与 [element.rs 中的 sticky headers 模块](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/editor/src/element/header.rs),它们是本讲两阶段模式的高级变体。
- **minimap 是绝佳复习材料**:`EditorMode::Minimap` 走了独立的 `layout_minimap`/`paint_minimap` 简化管线,对照本讲主管线阅读,能看清「哪些步骤是全量管线必需、哪些可裁剪」。
- **延伸到设置系统**:本讲遇到的三站式设置链(内容模型 → `EditorSettings` 落值 → 消费点)与 migrator 迁移,在 u7-l1(settings crate)中系统展开,建议接着读。
- **动手方向**:把综合实践的装饰升级为正式功能——走设置开关(`settings!` 宏)+ `EditorSettings` 字段 + `paint_background` 消费的完整链路,正好预习 u7-l1 与 u8-l6 的贡献闭环。
