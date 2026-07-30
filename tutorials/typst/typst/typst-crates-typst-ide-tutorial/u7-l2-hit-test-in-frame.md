# frame 内点击命中检测

## 1. 本讲目标

在 [u7-l1](u7-l1-jump-from-click.md) 中，我们已经看到公共入口 `jump_from_click` 只是一行分发，把点击位置交给 `document.resolve_position`，而后者最终会调用一个被我们当作「黑盒」的函数：`jump_from_click_in_frame`。本讲就打开这个黑盒。

学完本讲，你应当能够：

1. 说清楚「链接优先于源码 span 命中」这一设计的原因与实现位置。
2. 理解文本字形（glyph）命中时，如何用 `span_offset` 与「点击落在字符左半还是右半」的半宽判断，得到字符内的字节偏移。
3. 掌握图形（shape）的两类命中：填充命中（`Curve::contains`、`even-odd`、多边形自交）与描边命中（`stroke_contains`）。
4. 理解分组（group）的坐标变换：为什么要先对 `transform` 求逆、为什么用 `transform_inf`，以及 `clip` 裁剪如何递归地排除落点。
5. 看懂 `is_in_rect`、`Jump::from_span` 这两个被反复复用的底层小工具。

## 2. 前置知识

本讲假定你已学完 [u7-l1](u7-l1-jump-from-click.md)，知道 `Jump` 枚举有 `File` / `Url` / `Position` 三类终点、`jump_from_click` 经 `JumpFromDocument` sealed trait 分发到两种文档后端，并且 `PagedDocument::resolve_position` 会取出点击所在页的 `frame` 后调用本讲的 `jump_from_click_in_frame`。

此外需要一点关于「渲染产物 frame」的直觉：

- Typst 把排版结果组织成一棵 **frame 树**。每一页有一个根 `Frame`，frame 内是一组带位置（`Point`）的 **图元（frame item）**。
- 图元共有六类，本讲会用到其中五类：`Group`（子 frame + 变换 + 裁剪）、`Text`（一段排好版的字形）、`Shape`（几何图形 + 填充/描边）、`Image`（位图）、`Link`（超链接区域）。第六类 `Tag` 是内省标记，本讲不处理（命中检测里走 `_ => {}` 兜底）。
- 每个带源码语义的图元都携带一个 `Span`（源码位置）。命中检测的目标就是：在所有与点击点相交的图元里，挑出一个，把它的 `Span` 转成 `Jump::File`。

坐标系约定：frame 使用 **y 轴向下** 的点坐标系（`Point { x, y }`，单位为绝对长度 `Abs`）。文本的 `pos.y` 是 **基线**，字形向上延伸 `text.size`。

## 3. 本讲源码地图

本讲几乎只看一个文件：

| 文件 | 作用 |
| --- | --- |
| [src/jump.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs) | 命中检测的全部实现：`jump_from_click_in_frame`、`is_in_rect`、`Jump::from_span`，以及一组可直接运行的测试。 |

为了让图元结构与几何方法讲准确，本讲还会少量引用 typst-library 中以下真实定义（仅供对照，不是 typst-ide 的代码）：

| 文件 | 作用 |
| --- | --- |
| `crates/typst-library/src/layout/frame.rs` | `FrameItem` 枚举、`GroupItem` 结构体定义。 |
| `crates/typst-library/src/text/item.rs` | `Glyph` 结构体：`span: (Span, u16)`、`x_advance`、`range`。 |
| `crates/typst-library/src/visualize/shape.rs` | `Shape`、`Geometry`、`FillRule` 定义。 |
| `crates/typst-library/src/visualize/curve.rs` | `Curve::contains` / `Curve::stroke_contains`。 |
| `crates/typst-library/src/layout/point.rs` | `Point::transform` / `transform_inf`。 |

---

## 4. 核心概念与源码讲解

### 4.1 主流程：两阶段命中检测

#### 4.1.1 概念说明

`jump_from_click_in_frame` 解决的问题是：给定一个 frame、一个点击点 `click`，返回一个 `Jump`。它的总体策略可以浓缩成两句话：

1. **先找链接，再找 span。** 链接（`FrameItem::Link`）是「显式声明了跳转目标」的图元，语义最明确，必须优先于「根据源码 span 猜测」的命中。
2. **找不到链接时，逆序遍历图元找第一个被命中的。** frame 里的图元按「绘制顺序」排列，**后绘制的盖在前面**。因此从视觉上看，排在列表 **末尾** 的图元位于最上层，应当最先被命中——这正是 `.rev()` 逆序遍历的原因。

整段实现是一个纯函数：输入 `world`（取源码）、`output`（取 introspector，仅用于解析 `Location` 类链接）、`frame`、`click`，输出 `Option<Jump>`。

#### 4.1.2 核心流程

```
jump_from_click_in_frame(world, output, frame, click):
  output = output.as_output()

  # 第一阶段：正向遍历，找链接
  for (pos, item) in frame.items():           # 注意：正向
    if item 是 Link(dest, size) 且 click 落在 (pos, size) 矩形内:
      按 dest 三种变体返回 Jump::Url / Position / Position(查 introspector)

  # 第二阶段：逆序遍历，找源码 span
  for (pos, item) in frame.items().rev():      # 注意：逆序
    match item:
      Group   → 扣除组原点 → 检查 clip → 求逆变换 → 递归
      Text    → 逐字形判断是否落在点击点的字符格内
      Shape   → 分别做 fill 命中与 stroke 命中
      Image   → 矩形命中
      其它    → 跳过

  None   # 两个阶段都没命中
```

关键设计点：

- **两阶段用两个独立的循环**，而不是合并成一个。因为链接必须在 **任何 span 命中之前** 被考虑，且链接之间按正向顺序（先声明者优先级低、后声明者……实际上链接很少重叠，顺序影响很小），而 span 命中需要 `.rev()`（上层优先）。
- 两个循环里凡是命中都 **立即 `return`**，体现「首个命中即止」。

#### 4.1.3 源码精读

函数签名与两个阶段的整体结构在 [src/jump.rs:208-237](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L208-L237)：先 `output.as_output()` 拿到内省器，再进入第一个正向循环找链接，找不到才进入 `frame.items().rev()` 的逆序循环。

被两个阶段反复复用的矩形判定函数 `is_in_rect` 在 [src/jump.rs:333-340](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L333-L340)：

```rust
fn is_in_rect(pos: Point, size: Size, click: Point) -> bool {
    pos.x <= click.x
        && pos.x + size.x >= click.x
        && pos.y <= click.y
        && pos.y + size.y >= click.y
}
```

它把 `pos` 当作矩形 **左上角**、`size` 当作宽高，判断 `click` 是否落在 \([pos.x,\ pos.x+size.x] \times [pos.y,\ pos.y+size.y]\) 内。注意 Typst 的 y 轴向下，所以「左上角」的 y 较小。

被多种图元（Shape/Image/Text 等）复用的「span → Jump」转换在 [src/jump.rs:25-31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L25-L31)：

```rust
fn from_span(world: &dyn IdeWorld, span: Span) -> Option<Self> {
    let id = span.id()?;
    let offset = world.range(span)?.start;
    Some(Self::File(id, offset))
}
```

它把一个 `Span` 拆成「文件 id（`span.id()?`）」与「字节起始偏移（`world.range(span)?.start`）」，包成 `Jump::File`。`world.range` 来自文件顶部 `use typst::WorldExt;` 提供的扩展方法；`span.id()` 为 `None` 表示该 span 是 detached（无源码归属，例如运行时生成的图元），此时直接放弃。

> 图元结构对照（真实定义，仅作参考）：`FrameItem` 枚举见 [`frame.rs:486-499`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L486-L499)，`GroupItem`（含 `frame`/`transform`/`clip`）见 [`frame.rs:514-530`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L514-L530)。

#### 4.1.4 代码实践

实践目标：直观感受「链接优先、span 逆序」这两条规则。

操作步骤：

1. 打开 [src/jump.rs:577-585](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L577-L585) 的 `test_jump_from_click` 测试，源码是 `"*Hello* #box[ABC] World"`。
2. 注意这几组断言：点击 `point(45.0, 15.0)` 期望 `cursor(14)`、点击 `point(48.0, 15.0)` 期望 `cursor(15)`、点击 `point(72.0, 10.0)` 期望 `cursor(20)`。`cursor(n)` 表示期望跳到主源码第 `n` 个字节。
3. 在 `typst-ide` 目录下运行：`cargo test -p typst-ide test_jump_from_click -- --nocapture`。

需要观察的现象：测试通过，说明不同点击点确实命中了不同的源码位置。

预期结果：测试全部通过。

> 如何知道该用哪个坐标点？测试模块顶部的文档注释 [src/jump.rs:476-483](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L476-L483) 给出了 typst 团队自己的「探针」技巧：用 `#set page(background: place(dx:..,dy:.., square(size: 2pt, fill: red)))` 在页面上画一个红色小方块，渲染后看它在预览里的坐标，就能反推点击点。

#### 4.1.5 小练习与答案

**练习 1**：为什么链接搜索用正向 `frame.items()`，而 span 搜索用 `.rev()`？

**参考答案**：链接是显式声明的跳转区域，相互之间几乎不重叠，顺序无关紧要，正向遍历即可；而普通图元按绘制顺序排列，后绘制的盖在前面（视觉最上层），要让最上层优先被命中，就必须从列表末尾开始逆序遍历。

**练习 2**：`Jump::from_span` 里为什么有两处 `?`（`span.id()?` 与 `world.range(span)?.start`）？

**参考答案**：第一处排除 detached span（运行时生成的、没有源码归属的图元，如某些内省标记产生的图形）；第二处处理 world 里查不到该 span 区间的情况。两者都让函数优雅地返回 `None`，而不是 panic。

---

### 4.2 链接与图片：最简单的命中

#### 4.2.1 概念说明

`FrameItem::Link(Destination, Size)` 表示一块可点击的链接区域，`Destination` 有三种：

- `Url`：外链，跳到一个网址。
- `Position`：直接给出一个分页文档内的坐标，跳过去即可。
- `Location`：一个内省用的逻辑位置标识符，需要借助 `output.introspector()` 反查出它对应的 `PagedPosition`。

`FrameItem::Image(Image, Size, Span)` 是位图图元，命中后用它的 `Span` 跳回源码里的 `#image(...)` 调用。

#### 4.2.2 核心流程

```
# 链接（第一阶段，正向）
for (pos, item) in frame.items():
  if item 是 Link(dest, size) 且 is_in_rect(pos, size, click):
    match dest:
      Url(url)        → Jump::Url(url)
      Position(pos)   → Jump::Position(pos)
      Location(loc)   → introspector.position(loc) 若为 Paged → Jump::Position

# 图片（第二阶段，逆序）
for (pos, item) in frame.items().rev():
  if item 是 Image(_, size, span) 且 is_in_rect(pos, size, click):
    Jump::from_span(world, span)
```

#### 4.2.3 源码精读

链接分支在 [src/jump.rs:217-234](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L217-L234)：

```rust
for (pos, item) in frame.items() {
    if let FrameItem::Link(dest, size) = item
        && is_in_rect(*pos, *size, click)
    {
        match dest {
            Destination::Url(url) => return Some(Jump::Url(url.clone())),
            Destination::Position(pos) => return Some(Jump::Position(*pos)),
            Destination::Location(loc) => {
                if let Some(DocumentPosition::Paged(pos)) =
                    output.introspector().position(*loc)
                {
                    return Some(Jump::Position(pos));
                }
            }
        }
    }
}
```

注意 `Location` 分支：它用 `output.introspector().position(*loc)` 查询，并且用 `if let ... = DocumentPosition::Paged(pos)` 只接受分页文档的位置——这是 typst-ide 目前只支持「点击跳到分页文档坐标」的体现（HTML 坐标不在 `Jump` 枚举里，见 u7-l1）。

图片分支在 [src/jump.rs:322-324](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L322-L324)，是一个带 `if` 守卫的 match 臂：

```rust
FrameItem::Image(_, size, span) if is_in_rect(pos, *size, click) => {
    return Jump::from_span(world, *span);
}
```

这里 `pos` 是逆序循环解构出来的 `mut pos`（图元左上角），命中矩形后直接交给 `Jump::from_span`。

#### 4.2.4 代码实践

实践目标：体会「`Location` 链接依赖 introspector」这一最依赖编译产物的命中路径。

操作步骤：

1. 阅读 [src/jump.rs:782-786](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L782-L786) 的 `test_footnote_links`，源码是 `"#footnote[Hi]"`。
2. 注意：点击 `point(10.0, 10.0)`（正文里的脚注标记）期望跳到 `pos(1, 10.0, 31.58)`（页脚条目）；点击 `point(19.0, 33.0)`（页脚条目）期望跳回 `pos(1, 10.0, 16.58)`（正文标记）。

需要观察的现象：脚注标记与脚注条目之间是 **双向** 跳转，二者都通过 `Location` 解析。

预期结果：理解脚注之所以能「点标记到条目、点条目回标记」，正是因为两个位置都注册成了 `Location`，由 introspector 完成双向反查。待本地运行 `cargo test -p typst-ide test_footnote_links` 验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Destination::Location` 分支里要写 `if let ... DocumentPosition::Paged(pos)`，而不是直接 `return Some(Jump::Position(...))`？

**参考答案**：`introspector().position(loc)` 返回的是 `Option<DocumentPosition>`，而 `DocumentPosition` 是个枚举（分页 `Paged` 与 HTML 等）。`Jump::Position` 只接受 `PagedPosition`，所以必须先模式匹配出 `Paged` 变体；若返回的是其它形态则不跳转（落空继续找）。

**练习 2**：图片命中为什么不需要像 shape 那样做几何判断，只用 `is_in_rect`？

**参考答案**：图片总是绘制在一个轴对齐的矩形包围盒内（即便内容本身是透明/不规则），用矩形判定就够了；而 shape 的可见区域可能是圆、多边形、自交曲线，必须用真正的几何包含测试。

---

### 4.3 文本字形命中与字符内偏移

#### 4.3.1 概念说明

文本图元 `FrameItem::Text(TextItem)` 携带一组 **字形（Glyph）**。一个字形是字体里的一个图形（一个「字」），但它和源码里的字符 **不是一一对应** 的：

- 连字（ligature）：源码里的 `fi` 两个字母可能被排成一个字形。
- 多字节字符、组合字符：一个字形可能对应源码里多个字节。
- 反过来，一个源码字符也可能拆成多个字形。

因此 typst 给每个字形挂了两个字段：

- `span: (Span, u16)` —— 该字形所属的源码 `Span`，外加一个 **`span_offset`**，表示「在 span 节点覆盖的文本里，这个字形从第几个字符开始」。
- `range: Range<u16>` —— 该字形覆盖的 **字符区间长度**（连字时长度 > 1）。

命中检测不仅要找到被点击的字形，还要回答：**点击落在字符的左边还是右边？** 这决定了光标应放在字符前还是字符后。

#### 4.3.2 核心流程

```
for glyph in &text.glyphs:
    width = glyph.x_advance.at(text.size)       # 字形推进宽度（绝对长度）
    box = 矩形(pos.x, pos.y - text.size, width, text.size)   # 左上角、宽、高
    if is_in_rect(box, click):
        (span, span_offset) = glyph.span
        id = span.id()  否则 continue
        node = source.find(span)
        if node.kind() 是 Text 或 MathText:
            range = node.range()
            offset = range.start + span_offset          # 字形起点
            if (click.x - pos.x) > width / 2.0:         # 点击在右半
                offset += glyph.range().len()           # 推进到字形末尾
            offset = offset.min(range.end)              # 钳制不越界
        else:
            offset = node.offset()                      # 非文本节点：取节点起点
        return Jump::File(source.id(), offset)
    pos.x += width        # 前进到下一个字形的左边缘
```

半宽判断的直觉：字形的命中框是一条横向区间 \([pos.x,\ pos.x+width]\)。若点击点的 x 坐标落在前半段（\(\le pos.x + width/2\)），视为点在「字符前」；落在后半段（\(> pos.x + width/2\)），视为点在「字符后」，于是把字节偏移再推进一个字形的字符长度。这就是本讲标题里「区分点击落在字符前还是字符后」的实现。

#### 4.3.3 源码精读

文本分支在 [src/jump.rs:260-290](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L260-L290)：

```rust
FrameItem::Text(text) => {
    for glyph in &text.glyphs {
        let width = glyph.x_advance.at(text.size);
        if is_in_rect(
            Point::new(pos.x, pos.y - text.size),
            Size::new(width, text.size),
            click,
        ) {
            let (span, span_offset) = glyph.span;
            let Some(id) = span.id() else { continue };
            let source = world.source(id).ok()?;
            let node = source.find(span)?;
            let pos = if matches!(
                node.kind(),
                SyntaxKind::Text | SyntaxKind::MathText
            ) {
                let range = node.range();
                let mut offset = range.start + usize::from(span_offset);
                if (click.x - pos.x) > width / 2.0 {
                    offset += glyph.range().len();
                }
                offset.min(range.end)
            } else {
                node.offset()
            };
            return Some(Jump::File(source.id(), pos));
        }

        pos.x += width;
    }
}
```

阅读这段要特别注意两个细节：

1. **命中框的 y 坐标是 `pos.y - text.size`**：因为 `pos.y` 是文本 **基线**，字形向上延伸 `text.size`，所以矩形的「上边」要减去 `text.size`。
2. **变量 `pos` 被刻意遮蔽（shadow）**：循环里 `pos` 一开始是字形的左上角 `Point`（由外层 `for &(mut pos, ref item)` 解构而来，每轮 `pos.x += width` 前进）；但在 `if` 内部又写了 `let pos = if matches!(...) { ... }`。在 Rust 里，新绑定要等右边表达式 **整体求值完毕** 才生效，所以右边的 `(click.x - pos.x)` 里的 `pos.x` 仍指 **外层那个字形左上角的 x**。这正是半宽判断能拿到「字形左边缘」的原因——这是一个容易读错的地方。

> 字段来源（真实定义）：`Glyph` 的 `span: (Span, u16)`、`x_advance: Em`、`range: Range<u16>` 与 `Glyph::range()` 见 [`text/item.rs:94-116`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/item.rs#L94-L116)。注意 `range()` 把内部的 `u16` 区间转成 `usize` 区间，描述的是「该字形在所属文本项里的字符范围」，连字时长度大于 1。

#### 4.3.4 代码实践

实践目标：用真实测试验证「点字符左半 vs 右半」会得到相邻的两个不同偏移。

操作步骤：

1. 阅读 [src/jump.rs:582-584](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L582-L584)：对源码 `"*Hello* #box[ABC] World"`，点击 `point(45.0, 15.0)` 期望 `cursor(14)`，点击 `point(48.0, 15.0)` 期望 `cursor(15)`。
2. 在 `typst-ide` 目录下运行：`cargo test -p typst-ide test_jump_from_click -- --nocapture`。

需要观察的现象：两个点击点的 x 坐标只差 3pt，却分别跳到第 14、15 个字节——说明半宽判断把光标分别放在了「某字符前」和「某字符后」。

预期结果：测试通过。如果想看得更清楚，可在 `let (span, span_offset) = glyph.span;` 之后临时加一行 `eprintln!("hit glyph span_offset={span_offset} range={:?}", glyph.range());`（**示例代码**，非项目原有），重新运行观察每个命中字形的内部偏移与字符长度。

> 待本地验证：具体坐标点会随字体/版本微调，但「相邻点击点产生相邻字节偏移」这一行为是稳定的。

#### 4.3.5 小练习与答案

**练习 1**：如果用户在源码里写了连字 `fi`，被排版成一个字形，点击它的右半区会跳到哪里？

**参考答案**：该字形的 `range().len()` 为 2（覆盖 `f`、`i` 两个字符）。点击右半区时 `offset` 会从字形起点 `range.start + span_offset` 再 `+= 2`，即跳过整个 `fi`，落到连字之后的位置。

**练习 2**：为什么对非 `Text`/`MathText` 节点，代码直接用 `node.offset()` 而不做半宽判断？

**参考答案**：`Text`/`MathText` 是「一段文字」，有字符粒度的偏移意义；而其它节点（如一个标点 token、一个内联元素）通常只对应一个不可分的源码位置，没有「字符内偏移」的概念，取节点起点即可。

---

### 4.4 图形命中：fill 与 stroke

#### 4.4.1 概念说明

图形图元 `FrameItem::Shape(Shape, Span)` 的命中分两套独立检测：

- **填充命中（fill）**：点击点是否落在 shape 的 **填充区域** 内。只有 `shape.fill.is_some()` 时才做。
- **描边命中（stroke）**：点击点是否落在 shape 的 **边线** 上。只有 `shape.stroke.is_some()` 且描边厚度非零时才做。

shape 的几何（`Geometry`）有三种：

- `Line(Point)`：一条线段，**没有内部**，所以填充命中恒为 `false`。
- `Rect(Size)`：轴对齐矩形，填充命中退化成 `is_in_rect`。
- `Curve(Curve)`：由移动、直线、贝塞尔段组成的曲线，填充命中用真正的几何包含测试 `Curve::contains(fill_rule, point)`。

填充区域由 **填充规则** `FillRule` 决定，typst 支持两种（用环绕数 \(w\) 判定）：

- `NonZero`（默认）：点在内部当且仅当 \(w \neq 0\)。
- `EvenOdd`：点在内部当且仅当 \(w \bmod 2 \neq 0\)。

二者在「自交多边形」上表现不同：默认的 NonZero 会让同向环绕的区域「叠加」，而 even-odd 会在奇偶交叠处挖洞。

#### 4.4.2 核心流程

```
if shape.fill.is_some():
    within = match shape.geometry:
        Line(_)   → false
        Rect(s)   → is_in_rect(pos, s, click)
        Curve(c)  → c.contains(shape.fill_rule, click - pos)   # 注意：曲线原点在 (0,0)
    if within: return Jump::from_span(span)

if let Some(stroke) = shape.stroke:
    if not stroke.thickness.approx_empty():      # 厚度近似为 0 则不算
        base_curve = match shape.geometry:        # 统一转成 Curve
            Line(to) → Curve([Line(*to)])
            Rect(s)  → Curve::rect(s)
            Curve(c) → c
        within = base_curve.stroke_contains(stroke, click - pos)
        if within: return Jump::from_span(span)
```

注意两个关键点：

- **`click - pos`**：curve/geometry 都是以 `(0,0)` 为原点定义的，所以要把点击点先平移到 shape 的局部坐标（减去图元位置 `pos`）。
- **把 `Line`/`Rect` 也转成 `Curve` 再做 stroke_contains**：这样描边命中只依赖 `Curve::stroke_contains` 一套实现，避免为每种几何各写一遍。

#### 4.4.3 源码精读

shape 分支在 [src/jump.rs:292-320](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L292-L320)：

```rust
FrameItem::Shape(shape, span) => {
    if shape.fill.is_some() {
        let within = match &shape.geometry {
            Geometry::Line(..) => false,
            Geometry::Rect(size) => is_in_rect(pos, *size, click),
            Geometry::Curve(curve) => {
                curve.contains(shape.fill_rule, click - pos)
            }
        };
        if within {
            return Jump::from_span(world, *span);
        }
    }

    if let Some(stroke) = &shape.stroke {
        let within = !stroke.thickness.approx_empty() && {
            let base_curve = match &shape.geometry {
                Geometry::Line(to) => &Curve(vec![CurveItem::Line(*to)]),
                Geometry::Rect(size) => &Curve::rect(*size),
                Geometry::Curve(curve) => curve,
            };
            base_curve.stroke_contains(stroke, click - pos)
        };
        if within {
            return Jump::from_span(world, *span);
        }
    }
}
```

底层几何方法的实现（真实定义，对照阅读）：

- `Curve::contains` 在 [`curve.rs:510-517`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/curve.rs#L510-L517)：先把曲线转成 kurbo 的 `BezPath`，算出环绕数 `windings`，再按 `NonZero`（`!= 0`）或 `EvenOdd`（`% 2 != 0`）判定。
- `Curve::stroke_contains` 在 [`curve.rs:521-523`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/curve.rs#L521-L523)：把曲线按描边参数（厚度、端点、连接、虚线）扩张成一条带状的 `BezPath`，再用 `kurbo::Shape::contains` 判断点是否落在带状区域内。
- `Shape` 结构体（`geometry`/`fill`/`fill_rule`/`stroke`）见 [`shape.rs:332-343`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/shape.rs#L332-L343)，`Geometry`/`FillRule` 见 [`shape.rs:356-373`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/shape.rs#L356-L373)。

#### 4.4.4 代码实践

实践目标：用 `test_jump_from_click_shapes` 与 `test_jump_from_click_shapes_stroke` 两组测试，验证 fill 与 stroke 的不同命中行为。

操作步骤：

1. 阅读 [src/jump.rs:646-673](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L646-L673)。关注三组用例：
   - **圆**：`#circle(...)`，点击圆心 `point(15,15)` 命中（`cursor(1)`），点击圆外 `point(1,1)` 不命中（`None`）——证明 `Curve::contains` 做了真正的圆形判定，而不是用包围盒。
   - **蝴蝶结（bowtie）自交多边形**（默认 NonZero）：`#polygon(fill: black, (0pt,0pt),(20pt,20pt),(20pt,0pt),(0pt,20pt))`。点 `(1,2)` 命中、点 `(2,1)` 不命中、点 `(19,10)` 命中——展示环绕数如何决定自交区域的内外。
   - **even-odd 规则**：一个带 `fill-rule: "even-odd"` 的复杂多边形，点击中心 `point(15,15)` 不命中（被挖洞），点击外围 `(5,15)`、`(15,5)` 命中——展示 even-odd 与默认 NonZero 的差异。
2. 再阅读 [src/jump.rs:675-689](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L675-L689)：一个 `#place(dx:10,dy:10, rect(width:10,height:10, stroke:5pt))`，点中心 `(15,15)` 返回 `None`（无 fill、中心不在描边带上），点边线 `(10,15)` 命中（落在 5pt 描边带上）。
3. 运行：`cargo test -p typst-ide test_jump_from_click_shapes`、`cargo test -p typst-ide test_jump_from_click_shapes_stroke`。

需要观察的现象：fill 命中遵循真实几何边界（圆是圆、自交按环绕数），stroke 命中只在描边带内成立、内部空洞不命中。

预期结果：两组测试均通过。

> 待本地验证：bowtie 用例中 `(2,1)` 返回 `None` 的几何原因是该点落在环绕数相互抵消的区域；若你修改测试把它改成 `(1.0, 1.0)` 附近的不同点，可以观察到命中/不命中的边界。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Geometry::Line(..)` 的填充命中直接写 `false`？

**参考答案**：一条线段是一维的，没有面积、没有「内部」，填充对它无意义（即便 `shape.fill.is_some()`）。所以填充命中恒为假；线段的命中完全交给下面的 stroke 分支（描边带）。

**练习 2**：把 bowtie 多边形改成 `fill-rule: "even-odd"`，原来在 NonZero 下命中的 `(1,2)` 还会命中吗？

**参考答案**：不一定——even-odd 只看穿越次数的奇偶，不区分环绕方向。在自交区域，NonZero 可能因同向叠加而判定为「内」，even-odd 却可能因偶数次穿越而判定为「外」。这正是两种规则的差别，也是 `test_jump_from_click_shapes` 同时覆盖默认 NonZero 与 even-odd 两组用例的原因。待本地修改 `fill-rule` 后验证具体行为。

---

### 4.5 分组：逆变换与裁剪的递归

#### 4.5.1 概念说明

`FrameItem::Group(GroupItem)` 是最复杂的一类图元：它内嵌一个子 frame（`group.frame`），并且带了两个改变坐标系的属性：

- `transform: Transform` —— 一个仿射变换（旋转、缩放、平移、错切）。子 frame 里的所有内容都被这个变换「搬」到父 frame 里。
- `clip: Option<Curve>` —— 一条裁剪曲线，超出该曲线范围的子内容 **不可见、也不可命中**。

于是命中检测必须解决两个问题：

1. **坐标变换**：点击点 `click` 是在父 frame 坐标系里给出的，要递归到子 frame，必须把它 **变回** 子 frame 的局部坐标——也就是对 `transform` 求逆，再把逆变换作用到点上。
2. **裁剪**：如果点击点落在裁剪曲线之外，整个 group 都应被跳过（`continue`）。

这就是本讲实践任务里「点击一个带 `#rotate` 的 rect 仍能正确定位源码」的根本原因：rotate 产生了一个带旋转变换的 group，命中时通过 `transform.invert()` 把点击点旋回 rect 所在的局部坐标系，递归命中 rect。

#### 4.5.2 核心流程

仿射变换作用在点上的公式为：

\[
\begin{pmatrix} x' \\ y' \end{pmatrix}
=
\begin{pmatrix} sx & kx \\ ky & sy \end{pmatrix}
\begin{pmatrix} x \\ y \end{pmatrix}
+
\begin{pmatrix} tx \\ ty \end{pmatrix}
\]

它可逆当且仅当行列式 \(sx\cdot sy - kx\cdot ky \neq 0\)（例如 `scale(x: 0%)` 的行列式为 0，不可逆）。

```
FrameItem::Group(group):
    pos_local = click - pos                        # 扣除组原点
    if let Some(clip) = &group.clip:
        if not clip.contains(FillRule::NonZero, pos_local):
            continue                               # 落在裁剪区外，跳过整个组
    inv = group.transform.invert()  否则 continue   # 不可逆（如 scale 0）则跳过
    pos_local = pos_local.transform_inf(inv)       # 用逆变换旋/缩/移回子坐标系
    递归 jump_from_click_in_frame(world, output, &group.frame, pos_local)
```

三个要点：

- **clip 用 `NonZero` 规则**：无论 shape 自己的 `fill_rule` 是什么，group 的裁剪曲线总是按 NonZero 判定（裁剪关心的是「是否被完全包住」，NonZero 是更自然的「封闭区域」语义）。
- **clip 在逆变换之前判定**：即裁剪曲线定义在「扣除组原点、但尚未逆变换」的坐标空间里。
- **不可逆就 `continue`**：源码注释明确指出「现实中的变换几乎总是可逆的，不可逆的例子是 scale 0，而它本来也点不到」。

#### 4.5.3 源码精读

group 分支在 [src/jump.rs:239-258](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L239-L258)：

```rust
FrameItem::Group(group) => {
    let pos = click - pos;
    if let Some(clip) = &group.clip
        && !clip.contains(FillRule::NonZero, pos)
    {
        continue;
    }
    // Realistic transforms should always be invertible.
    // An example of one that isn't is a scale of 0, which would
    // not be clickable anyway.
    let Some(inv_transform) = group.transform.invert() else {
        continue;
    };
    let pos = pos.transform_inf(inv_transform);
    if let Some(span) =
        jump_from_click_in_frame(world, output, &group.frame, pos)
    {
        return Some(span);
    }
}
```

注意这里又一次出现了 `let pos = ...` 的遮蔽：`pos` 先是「扣除组原点后的点击点」，clip 判定用它；随后被遮蔽成「逆变换后的子坐标系点击点」，传给递归调用。

关于 `transform_inf` 与普通 `transform` 的差别（真实定义，对照阅读）：

- `Point::transform` 在 [`point.rs:77-86`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/point.rs#L77-L86)：用 `Ratio::of` 做乘法，**遇到无穷大坐标时结果归零**（带保护）。
- `Point::transform_inf` 在 [`point.rs:88-95`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/point.rs#L88-L95)：直接用原始浮点乘法，**不做无穷大保护**。

命中检测用 `transform_inf`，因为这里只是把一个有限点击点做逆映射，不需要 `transform` 那套针对「无穷大尺寸」的保护逻辑，直接相乘即可。

`Transform::invert` 在 [`transform.rs:350-369`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L350-L369)：文档明确「当矩阵行列式为零时返回 `None`」，内部还为单位阵和「纯缩放+平移」给了快速路径。

#### 4.5.4 代码实践

实践目标：解释「点击带 `#rotate` 的 rect 仍能正确定位源码」，并观察 clip 如何让命中失效。

操作步骤：

1. 阅读 [src/jump.rs:600-644](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L600-L644) 的 `test_jump_from_click_transform_clip`，逐条对照下表：

| 源码 | 点击点 | 期望 | 解释 |
| --- | --- | --- | --- |
| `#rect(width:20pt,height:20pt,fill:black)` | `(10,10)+margin` | `cursor(1)` | 普通矩形，直接 fill 命中 |
| `#rect(width:60pt,height:10pt,fill:black)` | `(5,30)+margin` | `None` | 矩形不在该位置，未命中 |
| `#rotate(90deg, origin: bottom+left, rect(60×10,black))` | `(5,30)+margin` | `cursor(38)` | **rotate 产生 group，逆变换后命中 rect** |
| `#scale(x:300%,y:300%, origin: top+left, rect(10×10,black))` | `(20,20)+margin` | `cursor(45)` | scale 产生 group，逆变换（缩回 1/3）后命中 |
| `#box(10×10, clip:true, scale(300%, rect(10×10,black)))` | `(20,20)+margin` | `None` | **clip 把超出的部分裁掉，命中失败** |
| `#box(10×10, clip:false, rect(30×30,black))` | `(20,20)+margin` | `cursor(45)` | 不裁剪，rect 超出 box 仍可命中 |
| `#box(10×10, clip:true, rect(30×30,black))` | `(20,20)+margin` | `None` | **clip 裁掉，命中失败** |

2. 运行：`cargo test -p typst-ide test_jump_from_click_transform_clip -- --nocapture`。

需要观察的现象：

- 第三行（`#rotate(...)`）能在旋转后的视觉位置上命中 `cursor(38)`，正是因为 group 分支先 `click - pos`、再 `transform.invert()`、再 `transform_inf`，把屏幕上的点击点逆旋转回了 rect 的局部坐标系，递归命中了 rect 的 fill。
- 第五、七行（`clip:true`）即便子内容在视觉上「延伸」到了点击点，也因 `clip.contains(NonZero, pos)` 判定点击在裁剪曲线之外而被 `continue` 跳过。

预期结果：测试通过。

> 待本地验证：若把第三行的 `origin: bottom + left` 改成别的旋转原点，点击同一个点可能就不再命中——你可以借此体会「逆变换依赖变换参数」。

#### 4.5.5 小练习与答案

**练习 1**：为什么命中检测里用 `transform_inf` 而不是普通的 `transform`？

**参考答案**：普通 `Point::transform` 用 `Ratio::of` 做乘法，目的是在遇到「无穷大尺寸」（如某些无限长元素）时把结果归零以避免 NaN 污染；而命中检测里的点击点总是有限值，逆变换后也应是有限值，用更直接、不做特殊保护的 `transform_inf` 即可，省去多余的开销分支。

**练习 2**：`#scale(x: 0%, ...)` 包裹的内容，点击它会怎样？

**参考答案**：scale 0 的变换矩阵行列式为 0，`Transform::invert()` 返回 `None`，group 分支走 `let Some(inv_transform) = ... else { continue; }` 直接跳过该 group，命中返回 `None`。这与源码注释「scale 0 本来也点不到」一致。

---

## 5. 综合实践

把本讲五类图元串起来，做一次「调用链追踪 + 测试改写」的综合练习。

背景：你要为一个新场景写测试——`#rotate(90deg, origin: bottom + left)[hello world]`，即把一段文本旋转 90 度。这同时涉及 **group 逆变换**（4.5）与 **文本字形命中**（4.3），还可能涉及 **clip**。

任务：

1. **读测试**：打开 [src/jump.rs:639-643](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L639-L643)，typst 团队已经为这个场景写了断言：点击 `point(5.0, 15.0) + margin` 期望 `cursor(40)`。
2. **画调用链**：在纸上（或注释里）写出从 `jump_from_click` 到最终 `Jump::File` 的完整路径，至少包含：`resolve_position` → `jump_from_click_in_frame`（第一层 frame）→ 命中 `FrameItem::Group` → `click - pos` → `transform.invert()` → `transform_inf` → 递归 `jump_from_click_in_frame`（子 frame）→ 命中 `FrameItem::Text` → 逐字形 `is_in_rect` → `span_offset` + 半宽判断 → `Jump::File`。
3. **加测试（示例代码，待本地验证）**：仿照 `test_click` 的风格，新增一个用例验证「点击旋转后文本的不同位置得到不同字节偏移」。例如：

```rust
// 示例代码：非项目原有，仿 test_click 风格
test_click(
    "#rotate(90deg, origin: bottom + left)[hello world]",
    point(5.0, 10.0) + point(10.0, 10.0),  // 点击文本上不同 x
    cursor(39),                            // 期望偏移，待本地验证
);
```

4. **验证**：运行 `cargo test -p typst-ide test_jump_from_click_transform_clip`。由于旋转后字形的精确坐标依赖字体，若你的新断言坐标选不准，可以借助 [src/jump.rs:476-483](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L476-L483) 的「红色小方块探针」法先确定可命中坐标，再写期望值。

预期：你能说清楚「一个被旋转的文本，为什么点击它能落到 `hello world` 里的某个字符」——这同时串起了 group 逆变换与字形半宽判断两条主线。

## 6. 本讲小结

- `jump_from_click_in_frame` 采用 **两阶段** 策略：第一阶段正向遍历找 `Link`（链接优先），第二阶段 `.rev()` 逆序遍历找源码 span（上层优先）。
- `Jump::from_span` 与 `is_in_rect` 是被多类图元复用的两个底层小工具：前者把 `Span` 转成 `Jump::File(id, offset)`，后者做轴对齐矩形包含判定。
- 文本命中逐字形进行，用 `glyph.span` 的 `(Span, span_offset)` 定位源码节点，再用 **`(click.x - pos.x) > width/2`** 的半宽判断决定光标落在字符前还是字符后。
- 图形命中分 fill 与 stroke 两套：fill 按 `Geometry` 分派（`Curve` 走 `Curve::contains(fill_rule, ..)`），stroke 统一把几何转成 `Curve` 再 `stroke_contains`；多边形自交与 even-odd 由环绕数规则决定。
- 分组命中是递归的：先 `click - pos` 扣组原点，按 `NonZero` 规则做 `clip` 裁剪，再 `transform.invert()` + `transform_inf` 把点击点逆变换回子坐标系，最后递归。
- 整套实现是 **best-effort** 的：detached span、不可逆变换、落在 clip 外、厚度为零的描边，统统优雅地返回 `None` 或 `continue`。

## 7. 下一步学习建议

- 下一讲 [u7-l3](u7-l3-jump-from-cursor.md) 讲 **反向** 链路 `jump_from_cursor`：从源码光标出发，反查它在渲染产物里的位置（`find_in_frame` / `find_in_elem`），与本讲互为逆向，建议对照阅读。
- 若想巩固坐标变换的直觉，可顺带读 `Transform::invert`（`crates/typst-library/src/layout/transform.rs`）与 `kurbo` 的环绕数实现。
- 想看 HTML 后端如何复用本讲的 `jump_from_click_in_frame`，可回看 [src/jump.rs:182-186](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L182-L186)：HTML 命中一个内嵌 frame 时，最终也委托给同一个函数。
