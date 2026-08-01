# 文本与字形渲染 text.rs

## 1. 本讲目标

本讲聚焦 typst-svg 里把一段文字翻译成 SVG 的全过程，全部位于 [`src/text.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs)。学完后你应当能够：

- 说清楚 `render_text` 为什么在进入文字渲染前要先做一次 Y 轴翻转，以及笔（pen）的 `x/y` 是如何随 `x_advance/y_advance` 累加推进的。
- 解释 `render_glyph` 如何用 `should_outline` 把字形分流为「轮廓字形」与「图像字形」两条完全不同的渲染路径。
- 说出 `RenderedGlyph` 枚举两种变体（`Path` / `Frame`）分别缓存什么，以及它们为何被存进 `Deduplicator<Option<RenderedGlyph>>`。
- 对比 `render_path_glyph`（去重时预缩放）与 `render_image_glyph`（使用处才缩放）这两条路径在「空间正确性」与「文件体积」上的取舍——这是本讲的核心实践任务。

本讲承接 [u3-l1 路径构建器](./u3-l1-path-builder.md)（`SvgPathBuilder::with_scale`）与 [u2-l1 渲染器与状态](./u2-l1-renderer-and-state.md)（`SVGRenderer`、`State`、`Deduplicator`）。`write_glyph_defs` 把去重结果写成 `<symbol>` 的细节留给下一讲 [u4-l2](./u4-l2-glyph-defs.md)，本讲只在结尾处点到。

## 2. 前置知识

- **坐标系方向**：SVG 画布是 Y-Down（原点在左上角，y 向下增长）；而字体内部的轮廓与度量（baseline、`y_advance`、`y_offset`）是 Y-Up（原点在 baseline，y 向上增长）。这两套坐标系方向相反，是本讲所有「翻转」操作的根源。
- **Em 与 Abs**：字体里的advance/offset 用 `Em`（相对单位，1 em = 1 个字号）表示；SVG 里用 `Abs`（绝对单位 pt）。`Em::at(size)` 把相对值乘以字号得到 pt：\(\text{advance}_{\text{pt}} = \text{advance}_{\text{em}} \times \text{size}_{\text{pt}}\)。
- **units_per_em（upem）**：字体的内部栅格分辨率，例如 1000 表示 1 em 被切成 1000 个字体单位。字号 pt 与字体单位的换算比例就是 \(\text{scale} = \text{size}_{\text{pt}} / \text{upem}\)。
- **Deduplicator**：typst-svg 的去重容器（见 [u2-l1](./u2-l1-renderer-and-state.md) / [u6-l3](./u6-l3-deduplicator.md)），用 `typst_utils::hash128(&key)` 做键，把相同资源只存一份并返回一个 `DedupId`。`render_glyph` 产出的字形就缓存在 `self.glyphs`（kind 字符 `'g'`）里。
- **FrameItem::Text**：排版产出的 `Frame` 树里，文字节点是 `TextItem`，它持有一串 `Glyph`，每个 `Glyph` 带 `id`（字形索引）与 `x_advance/x_offset/y_advance/y_offset`（均为 `Em`）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-svg/src/text.rs` | 本讲主角：文本/字形的渲染与去重入口。 |
| `crates/typst-svg/src/lib.rs` | 提供 `SVGRenderer`（含 `glyphs` 字段）、`State`、`Deduplicator`、`DedupId`、`finalize` 等基础设施。 |
| `crates/typst-svg/src/path.rs` | `SvgPathBuilder::with_scale`，用于在提取轮廓时直接把字体单位缩放到 pt（见 [u3-l1](./u3-l1-path-builder.md)）。 |
| `crates/typst-library/src/text/font/color.rs` | 上游定义 `should_outline`、`glyph_frame`、`GlyphFrame`、`GlyphFrameItem`，决定一个字形走轮廓还是图像路径。 |
| `crates/typst-library/src/text/item.rs` | 上游定义 `TextItem` 与 `Glyph` 结构（advance/offset 字段）。 |

## 4. 核心概念与源码讲解

### 4.1 文本入口 render_text：Y 轴翻转与笔推进

#### 4.1.1 概念说明

`render_text` 是文字渲染的总入口，被 `render_frame` 在遇到 `FrameItem::Text` 时调用（见 [u2-l2](./u2-l2-frame-traversal.md)）。它要做两件事：

1. **建立 Y-Up 的字形坐标系**：因为后续所有字形度量（`y_advance`、`y_offset`、轮廓坐标）都按 Y-Up 定义，而 SVG 是 Y-Down，所以先在文字外层套一个 `scale(1, -1)` 的翻转，让字体空间里的「向上」在屏幕上也表现为「向上」。
2. **逐字形推进笔位置**：维护两个累加器 `x`、`y`，对每个字形先在「当前笔位置 + 该字形 offset」处渲染，再用该字形的 advance 推进笔。

#### 4.1.2 核心流程

```
render_text(svg, state, text):
  打开外层 <g>
  state ← state.pre_concat(scale(1, -1))      # 翻转 Y，建立 Y-Up 空间
  <g transform = state.transform>
  x ← 0; y ← 0                                # 笔起点
  for glyph in text.glyphs:
      xo ← x + glyph.x_offset.at(text.size)   # 渲染位置（用推进前的笔位置）
      yo ← y + glyph.y_offset.at(text.size)
      render_glyph(... glyph_id, xo, yo)
      x ← x + glyph.x_advance.at(text.size)   # 渲染后才推进
      y ← y + glyph.y_advance.at(text.size)
```

注意一个易错点：**offset 用的是推进前的笔位置，advance 在渲染之后才累加**。这是标准排版笔模型——先在当前笔位画字形，笔再向前走一个 advance。

Y 翻转的几何含义：字体空间某点 \((x_f, y_f)\)（Y-Up）经外层 `scale(1,-1)` 后变为 \((x_f, -y_f)\)（Y-Down），正好把「向上」翻成屏幕上的「向上」，字形看起来是正立的。

#### 4.1.3 源码精读

入口与外层 `<g>`、Y 翻转（注意 `pre_concat` 把翻转垫在累积变换的内层，再写到 `transform` 属性上）：

[text.rs:29-53](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L29-L53) —— `render_text` 整体；其中 [text.rs:37-39](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L37-L39) 是关键的 Y 翻转与 `<g transform>` 写入。

笔推进循环：

[text.rs:41-52](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L41-L52) —— `x`/`y` 累加器；`x_offset`/`y_offset` 用 `.at(text.size)` 把 `Em` 换算成 `Abs`（pt），`render_glyph` 之后再做 `x += x_advance`、`y += y_advance`。

上游 `Glyph` 结构里 advance/offset 字段的定义（注意注释明确 `y_advance`/`y_offset` 是 Y-Up）：

[item.rs:94-106](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/item.rs#L94-L106) —— `Glyph { id, x_advance, x_offset, y_advance, y_offset, range }`。

#### 4.1.4 代码实践

**目标**：理解 Y 翻转对最终 SVG 的影响。

**操作步骤（源码阅读型）**：

1. 在 [text.rs:38](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L38) 处，把 `Transform::scale(Ratio::one(), -Ratio::one())` 临时改成 `Transform::identity()`（仅本地思考，不要提交）。
2. 推演：失去这次翻转后，一个 baseline 在 \(y=0\)、字身向 \(+y\) 延伸的轮廓字形，在 Y-Down 的 SVG 里会朝哪个方向画？

**需要观察的现象 / 预期结果**：没有翻转时，字体空间 \(+y\)（向上）会直接映射到 SVG 的 \(+y\)（向下），于是字形会**上下颠倒**地画在 baseline 下方。这解释了为什么这一行翻转不可或缺。本结论为源码推演，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `render_text` 把翻转放在外层 `<g>` 上，而不是每个字形各自翻转一次？
**答案**：一次外层翻转对整段文字生效，避免在每个字形重复写入 `scale(1,-1)`，既省体积又保证整段文字共用同一套 Y-Up 度量。

**练习 2**：水平排版时 `y_advance` 通常是 0，此时 `y` 累加器会怎样？
**答案**：`y` 始终保持 0，`y_offset.at(size)` 也通常为 0，所以水平文字每个字形只在 `x` 方向上排列；`y`/`y_advance` 主要服务竖排或堆叠脚本。

---

### 4.2 字形分发 render_glyph：轮廓 vs 图像

#### 4.2.1 概念说明

`render_glyph` 是单个字形的渲染入口，它的核心职责是**分流**：调用上游 `should_outline(font, glyph_id)` 判断这个字形应当

- **轮廓路径**（`should_outline == true`）：来自 `glyf`/`cff`/`cff2` 表的普通单色矢量字形，用 `render_path_glyph` 画；
- **图像路径**（`should_outline == false`）：彩色/位图/SVG 字形（COLR、PNG 位图、OpenType SVG）或兜底的 tofu 框，用 `render_image_glyph` 画。

两条路径都先把字形交给 `self.glyphs` 去重，拿到一个 `DedupId`，再用 `<use>` 引用之。区别在于**去重键**和**缩放时机**不同（见 4.4 / 4.5）。

#### 4.2.2 核心流程

```
render_glyph(svg, state, text, glyph_id, x_offset, y_offset):
  if should_outline(font, glyph_id):              # 轮廓字形
      scale ← size.pt / upem
      key ← (font, glyph_id, scale)               # ★ scale 进入键
      (id, path) ← glyphs.insert_with_val(key, || 提取轮廓并预缩放 → RenderedGlyph::Path)
      if path.is_some(): render_path_glyph(...)
  else:                                            # 图像字形
      key ← (font, glyph_id)                       # ★ 不含 scale
      (id, frame) ← glyphs.insert_with_val(key, || glyph_frame(...) → RenderedGlyph::Frame)
      if frame.is_some(): render_image_glyph(...)
```

`should_outline` 返回 `true` 当且仅当：字体有 `glyf`/`cff`/`cff2` 表，且该字形**既不是** PNG 位图、**也不是** COLR 彩色字形、**还没有** SVG 字形——即一个「朴素的单色轮廓字形」。

#### 4.2.3 源码精读

分流主体：

[text.rs:55-94](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L55-L94) —— `render_glyph`。其中 [text.rs:64](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L64) 是 `should_outline` 分流点；[text.rs:67-68](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L67-L68) 是轮廓分支的 `scale` 与含 `scale` 的键；[text.rs:84](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L84) 是图像分支**不含** `scale` 的键。

`insert_with_val` 的「按键哈希、惰性构造值」语义：只在键缺失时才调用闭包提取字形，已缓存则直接返回既有 `DedupId`：

[lib.rs:504-512](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L504-L512) —— `insert_with_val`，`typst_utils::hash128(&key)` 作键。

上游 `should_outline` 的判定逻辑（四个条件的合取）：

[color.rs:17-29](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/font/color.rs#L17-L29) —— `should_outline`。

#### 4.2.4 代码实践

**目标**：用真实字体观察分流结果。

**操作步骤**：

1. 准备一个最小 Typst 文档，混排普通文字与一个彩色 emoji，例如：

   ```typst
   Hello #emoji["😀"]
   ```

   （`Hello` 的字母走轮廓路径；emoji 走图像路径。）
2. 用 CLI 编译为 SVG：`typst compile doc.typ doc.svg`（**示例命令，待本地验证**；typst-cli 默认对 SVG 不开启 bleed，见 [u1-l3](./u1-l3-public-api-and-usage.md)）。
3. 打开 `doc.svg`，搜索 `<symbol` 与 `<use`。

**需要观察的现象 / 预期结果**：

- 普通字母 → `<symbol>` 内是 `<path d="..."/>`（轮廓），引用处是带 `x`/`y`/`fill` 的 `<use>`。
- emoji → `<symbol>` 内是 `<image xlink:href="data:...;base64,..."/>`（位图/COLR/SVG），引用处是带 `transform="matrix(...)"` 的 `<use>`，且**没有** `fill`/`x`/`y`。

这正是两条分流路径在产物上的差异。具体字符串形态待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：一个 COLR 彩色字形会走哪条分支？为什么？
**答案**：走图像分支。因为 `should_outline` 里含 `!ttf.is_color_glyph(glyph_id)`，COLR 彩色字形会使该条件为假，整体返回 `false`，进入 `render_image_glyph`。

**练习 2**：为什么 `render_glyph` 在调用 `render_path_glyph`/`render_image_glyph` 前要用 `if path.is_some()` / `if frame.is_some()` 守卫？
**答案**：去重容器存的是 `Option<RenderedGlyph>`。轮廓提取（`outline_glyph`）或 `glyph_frame` 可能返回 `None`（字形无可用数据），此时不应输出任何 `<use>`，故需先判 `is_some`。

---

### 4.3 去重值 RenderedGlyph：两种缓存形态

#### 4.3.1 概念说明

`RenderedGlyph` 是「一个字形被预处理后、等待去重缓存」的数据类型，也是 `self.glyphs` 里每个条目的值。它只有两种变体，正好对应 4.2 的两条分流：

- `RenderedGlyph::Path(EcoString)`：一段 **SVG path 数据字符串**（形如 `M x y L x y C ... Z`），代表轮廓字形，且**已经预缩放到目标 pt**。
- `RenderedGlyph::Frame(GlyphFrame)`：一个**字体单位（upem）空间下的 frame**，里面装着图像字形（位图/COLR/SVG）或 tofu 兜底框，**未缩放**。

#### 4.3.2 核心流程

两条分支各自构造 `RenderedGlyph`：

```
轮廓分支:  SvgPathBuilder::with_scale(scale) 提取轮廓 → Path(路径字符串)   # 已含 scale
图像分支:  glyph_frame(font, glyph_id)               → Frame(GlyphFrame)   # 字体单位
```

二者都被包进 `Option` 存入 `Deduplicator`；最终在 `finalize → write_glyph_defs` 阶段，每个 `RenderedGlyph` 被写成一个 `<symbol id="g...">`，供各处 `<use>` 引用。

#### 4.3.3 源码精读

枚举定义（注释说明 `Path` 即 `M x y L x y C x1 y1 x2 y2 x y Z` 格式的路径数据）：

[text.rs:14-23](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L14-L23) —— `enum RenderedGlyph { Frame(GlyphFrame), Path(EcoString) }`。

轮廓分支构造 `Path`（`with_scale(scale)` 即在提取时预缩放）：

[text.rs:69-73](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L69-L73) —— `SvgPathBuilder::with_scale(scale)` + `outline_glyph`，返回 `Some(RenderedGlyph::Path(...))`。

图像分支构造 `Frame`：

[text.rs:85-88](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L85-L88) —— `glyph_frame(&text.font, glyph_id.0)`，返回 `Some(RenderedGlyph::Frame(frame))`。

缓存它的字段（注意元素类型是 `Option<RenderedGlyph>`，kind 字符 `'g'`）：

[lib.rs:188-225](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L188-L225) —— `SVGRenderer` 结构；[lib.rs:192](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L192) 是 `glyphs: Deduplicator<Option<RenderedGlyph>>`；[lib.rs:275](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L275) 构造时 `Deduplicator::new('g')`。

上游 `GlyphFrame` 与 `GlyphFrameItem`（图像字形在字体单位空间下的两种形态：tofu 兜底框 / 真正的图像）：

[color.rs:31-67](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/font/color.rs#L31-L67) —— `GlyphFrame { upem, item }`，`item` 为 `Tofu(Point, Shape)` 或 `Image(Point, Image, Size)`。

#### 4.3.4 代码实践

**目标**：理解 `finalize` 如何把两种 `RenderedGlyph` 统一成 `<symbol>`。

**操作步骤（源码阅读型）**：

1. 阅读 [text.rs:184-219](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L184-L219) 的 `write_glyph_defs`（深入讲解见 [u4-l2](./u4-l2-glyph-defs.md)）。
2. 跟踪两种变体分别写出什么：`Path` → `<symbol><path d="..."/></symbol>`；`Frame` → 递归调用 `render_shape`（Tofu）或 `render_image`（Image）放进 `<symbol>`。

**需要观察的现象 / 预期结果**：不论 `Path` 还是 `Frame`，最终都被包进同一个 `<symbol id="g...">` 容器，差别只在内部是 `<path>` 还是 `<image>`/`<rect>`。统一的 `<symbol>` 抽象让 4.4 / 4.5 都能用 `<use>` 引用。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `glyphs` 存的是 `Option<RenderedGlyph>` 而不是 `RenderedGlyph`？
**答案**：因为轮廓/图像提取都可能失败（返回 `None`）。即便值为 `None` 也要记下这个键的哈希，避免下次对同一个无数据字形重复尝试提取——「失败也缓存」。

**练习 2**：`Frame` 变体里存的是「字体单位」还是「目标 pt」？
**答案**：字体单位（`GlyphFrame.upem` 决定其尺寸为 `Size::splat(upem)`）。缩放推迟到使用处（4.5），这正是它与 `Path`（已预缩放）的关键区别。

---

### 4.4 轮廓字形 render_path_glyph：去重时预缩放

#### 4.4.1 概念说明

对轮廓字形，typst-svg 选择**在去重时就完成缩放**：提取轮廓时用 `SvgPathBuilder::with_scale(scale)`，把字体单位直接换算成目标 pt，于是缓存里的 `Path` 字符串是「这个字号下的最终坐标」。因此：

- 去重键是 `(font, glyph_id, scale)`——**同一个字形在不同字号下会各存一份**；
- 使用处（`<use>`）**不需要再带 scale 变换**，只需 `x`/`y` 定位 + `fill`/`stroke` 涂料。

源码注释点明了动机：「Pre-scale outlined glyphs, so strokes and fill patterns don't need to consider text size glyph scaling.」——预缩放后，描边厚度、渐变/平铺等涂料都在最终 pt 空间里计算，无需再为「字形被缩放」做额外补偿。

#### 4.4.2 核心流程

```
render_path_glyph(svg, state, text, glyph_id, x_offset, y_offset, id):
  state ← state.pre_concat(translate(x_offset, y_offset))   # 仅用于涂料变换计算
  bbox ← font.glyph_bounding_box(glyph_id)                   # 取宽高
  aspect_ratio ← bbox 的宽高比                                # 给渐变角度修正用
  <use xlink:href="#id" x=x_offset y=y_offset>
      write_fill(fill, aspect_ratio, text_paint_transform)   # 填充/渐变/平铺
      if stroke: write_stroke(stroke, aspect_ratio, ...)
```

注意：`state` 在这里 `pre_concat` 了 `translate(x_offset, y_offset)`，但这**不会**输出成 `<g transform>`；它只喂给 `text_paint_transform`，用于 `RelativeTo::Parent` 的渐变/平铺定位。实际定位靠 `<use>` 的 `x`/`y` 属性（因为路径已预缩放，无需 scale）。

#### 4.4.3 源码精读

[text.rs:115-163](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L115-L163) —— `render_path_glyph` 整体。关键点：

- [text.rs:127-129](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L127-L129)：`state.pre_concat(translate(x_offset, y_offset))`，注释说明「state transform 用于绘制带渐变/平铺的描边与填充」。
- [text.rs:131-141](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L131-L141)：取字形包围盒并算 `aspect_ratio`（供 `write_fill`/`write_stroke` 修正渐变角度，参见 [u3-l2](./u3-l2-shape-rendering.md) 的 `shape_fill_size`）。
- [text.rs:143-146](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L143-L146)：`<use>` 带 `xlink:href`、`x`、`y`——**没有 scale**。
- [text.rs:148-162](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L148-L162)：在 `<use>` 上直接 `write_fill`/`write_stroke`，涂料在最终 pt 空间计算。

去重时构造 `Path` 的预缩放（`with_scale(scale)`）：

[text.rs:67-73](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L67-L73) —— `scale = size.pt / upem`，键 `(font, glyph_id, scale)`，`SvgPathBuilder::with_scale(scale)`。

产物示意（**示例代码**，非项目原产物，仅说明结构）：

```xml
<!-- 定义处（finalize 阶段写出）：路径已是目标 pt 坐标 -->
<symbol id="gXXXX" overflow="visible">
  <path d="M0 0 L10 0 ..."/>          <!-- 已按 scale 预缩放 -->
</symbol>
<!-- 使用处：只有 x/y 定位 + fill，无 scale -->
<use xlink:href="#gXXXX" x="0pt" y="0pt" fill="black"/>
```

#### 4.4.4 代码实践

**目标**：验证「同字形不同字号 → 不同 symbol」。

**操作步骤**：

1. 写一份 Typst 文档，让同一字母以两种字号出现：

   ```typst
   #text(size: 10pt)[A]
   #text(size: 20pt)[A]
   ```
2. 编译为 SVG（`typst compile ... doc.svg`，**示例命令，待本地验证**）。
3. 在产物里统计 `id="g...` 的 `<symbol>` 数量，确认字母 A 出现了几份。

**需要观察的现象 / 预期结果**：因为键含 `scale`，10pt 与 20pt 的 A 会有**两个不同的** `<symbol>`（两条预缩放后的路径），各自被对应字号的 `<use>` 引用。这体现了「以空间换涂料计算简单性」的取舍。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：假如把 `scale` 从去重键里去掉（即所有字号共用一份未缩放的 `Path`），`render_path_glyph` 需要怎样改？
**答案**：`<use>` 上必须额外带一个 `scale` 变换（如 `transform="scale(s)"`），并且 `write_fill`/`write_stroke` 的渐变/平铺计算也要把字形缩放纳入补偿——这正是源码注释想避免的复杂度。

**练习 2**：为什么包围盒取不到（`glyph_bounding_box` 返回 `None`）时直接 `return`？
**答案**：能走到 `render_path_glyph` 说明轮廓已成功提取并缓存，理论上必有包围盒；取不到属于不应发生的情况，故防御性地跳过该字形（见 [text.rs:131-135](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L131-L135) 的注释）。

---

### 4.5 图像字形 render_image_glyph：使用处才缩放

#### 4.5.1 概念说明

对图像字形（emoji 位图、COLR 彩色、SVG 字形），typst-svg 反其道而行：**去重时不缩放**，缓存的是字体单位（upem）空间下的 `Frame`；缩放推迟到**每次 `<use>` 引用时**才做。因此：

- 去重键是 `(font, glyph_id)`——**不含 scale**，同一字形在所有字号下共用一份定义；
- 使用处的 `<use>` 自带一个 `transform`，把字体单位缩放到当前字号。

源码注释说明动机：「colr, svg-, and bitmap glyph images are usually quite large, and having one glyph per text size is a bit of a waste.」——这些图像数据本身体积大（base64 的 PNG、内嵌 SVG），若每个字号都存一份会撑爆文件；而每个 `<use>` 多带一个 transform（区区几个数字）几乎不占空间。

此外，图像字形还需**第二次 Y 翻转**：外层 `render_text` 已为字体度量做了一次翻转，但图像像素本身是 Y-Down（像普通图片一样），所以在 `render_image_glyph` 里用 `scale(scale, -scale)` 把 Y 再翻一次，两次翻转相消，图像就以正立方向显示。

#### 4.5.2 核心流程

```
render_image_glyph(svg, x_offset, y_offset, text, id):
  scale ← size.pt / upem
  ts ← translate(x_offset, y_offset) ∘ scale(scale, -scale)   # 缩放 + 二次翻转
  <use xlink:href="#id" transform=ts>
```

双重翻转的几何含义：图像局部点 \((x_i, y_i)\)（Y-Down）→ 经 `scale(s,-s)` 得 \((s x_i, -s y_i)\)（Y-Up）→ 再经外层 `scale(1,-1)` 得 \((s x_i, s y_i)\)（Y-Down，正立）。净效果是 Y 方向缩放 \(+s\)，图像朝向正常。

#### 4.5.3 源码精读

[text.rs:96-113](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L96-L113) —— `render_image_glyph` 整体。关键点：

- [text.rs:105](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L105)：`scale = size.pt / upem`，**在使用处**才算。
- [text.rs:107-108](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L107-L108)：`Transform::translate(x_offset, y_offset).pre_concat(Transform::scale(scale, -scale))`，注释「Flip the transform again, since images are drawn Y-Down」。
- [text.rs:110-112](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L110-L112)：`<use>` 只有 `xlink:href` 与 `transform`，**没有** `fill`（图像自带颜色）也**没有** `x`/`y`（定位已并入 transform）。

去重键不含 scale：

[text.rs:84-88](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L84-L88) —— 键 `(font, glyph_id)`，值 `Frame(glyph_frame(...))`，字体单位空间。

产物示意（**示例代码**，非项目原产物，仅说明结构）：

```xml
<!-- 定义处：图像在字体单位（upem）空间，与字号无关 -->
<symbol id="gYYYY" overflow="visible">
  <image xlink:href="data:image/png;base64,...." width="..." height="..."/>
</symbol>
<!-- 使用处：transform 同时完成 缩放 + 二次翻转 + 定位 -->
<use xlink:href="#gYYYY" transform="matrix(...)"/>
```

#### 4.5.4 代码实践（本讲核心实践任务）

**目标**：对比 `render_path_glyph` 与 `render_image_glyph`，说清「预缩放 vs 使用处缩放」的取舍。

**操作步骤**：

1. 重新审视 4.2.4 的 SVG 产物，或再编译一份混排文档：

   ```typst
   #text(size: 20pt)[AB] #emoji["😀"] #text(size: 40pt)[AB] #emoji["😀"]
   ```
2. 分别对字母 `B`（轮廓）与 emoji（图像）统计：当同一字形以两种字号（20pt / 40pt）出现时，`<symbol>` 各有几份？`<use>` 各自带哪些属性？

**需要观察的现象 / 预期结果**：

| | 轮廓字形 `B` | 图像字形 emoji |
| --- | --- | --- |
| 去重键 | `(font, id, scale)` | `(font, id)` |
| 20pt 与 40pt 共用定义？ | 否（两份 `<symbol>`） | 是（一份 `<symbol>`） |
| `<use>` 自带 scale？ | 否（用 `x`/`y` 定位） | 是（`transform="matrix(...)"`） |
| 取舍 | 用「少量重复的小路径」换「涂料在最终 pt 空间的简单计算」 | 用「每个 `<use>` 多一个 transform」换「不重复存大体积图像数据」 |

**结论（取舍说明）**：

- **轮廓字形预缩放** = 以**空间（体积）**换**计算简单性/正确性**：路径数据很小，即便每个字号存一份也廉价；好处是描边与渐变/平铺涂料都在最终 pt 空间直接计算，`<use>` 无需带 scale，几何与涂料都对齐。
- **图像字形使用处缩放** = 以**每个引用多一个 transform（廉价）**换**体积**：彩色/位图/SVG 字形数据庞大，按字号重复存储不可接受；故共用一份字体单位定义，把缩放推迟到使用处。

具体统计数字待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `render_image_glyph` 的 `<use>` 上没有 `fill` 属性，而 `render_path_glyph` 的 `<use>` 上通常有？
**答案**：图像字形（位图/COLR/SVG）自身已携带颜色信息，不需要外部 `fill`；而轮廓字形只是单色矢量轮廓，必须由 `text.fill` 提供填充色。

**练习 2**：如果某 emoji 在 10pt、14pt、18pt 三种字号下各出现一次，按当前实现会产生几个 `<symbol>`、几个带 transform 的 `<use>`？
**答案**：1 个 `<symbol>`（键不含 scale，共用一份字体单位定义）+ 3 个 `<use>`，每个 `<use>` 带各自字号的 `transform`。

---

## 5. 综合实践

把本讲知识串起来：用 CLI 观察一条「文字 → emoji → 文字」的混排行，画出它的 SVG 结构草图。

1. 准备 `doc.typ`：

   ```typst
   #set text(size: 16pt)
   Hi #emoji["🚀"]!
   ```
2. 编译：`typst compile doc.typ doc.svg`（**示例命令，待本地验证**）。
3. 打开 `doc.svg`，按本讲所学回答：
   - 最外层包裹 `Hi 🚀!` 的 `<g>` 的 `transform` 里，为什么含一个负号的 Y 缩放？（对应 4.1 的 Y 翻转）
   - `H`、`i`、`!` 对应的 `<use>` 各自带哪些属性？它们的 `<symbol>` 内部是 `<path>` 还是 `<image>`？（对应 4.4）
   - `🚀` 的 `<use>` 是否带 `transform` 而非 `x`/`y`/`fill`？它的 `<symbol>` 内部是什么？（对应 4.5）
   - 假设把字号改成 32pt 再编译一次，`H` 的 `<symbol id>` 与 `🚀` 的 `<symbol id>` 分别会不会变？为什么？（对应 4.4 vs 4.5 的键差异）
4. 把你的观察整理成一张「轮廓字形 vs 图像字形」对照表。

**预期结果**：能清楚指认产物中 Y 翻转的外层 `<g>`、轮廓字形的 `<symbol><path>` + 带 `fill`/`x`/`y` 的 `<use>`、图像字形的 `<symbol><image>` + 带 `transform` 的 `<use>`；并能解释字号变化时两种字形的 `<symbol>` 复用行为不同。具体产物待本地验证。

## 6. 本讲小结

- `render_text` 先用 `scale(1,-1)` 翻转 Y 轴建立 Y-Up 字形空间，再用 `x`/`y` 累加器按 `x_advance`/`y_advance`（经 `.at(text.size)` 换算为 pt）逐字形推进笔——offset 用推进前的笔位，advance 在渲染后累加。
- `render_glyph` 用上游 `should_outline` 把字形二分流：普通单色轮廓走 `render_path_glyph`，彩色/位图/SVG 字形（或 tofu 兜底）走 `render_image_glyph`；二者都先经 `self.glyphs` 去重拿 `DedupId`。
- `RenderedGlyph` 有两变体：`Path(EcoString)`（已预缩放的轮廓路径数据）与 `Frame(GlyphFrame)`（字体单位空间下的图像/tofu 帧），统一缓存为 `Option`，最终都写成 `<symbol>` 供 `<use>` 引用。
- 轮廓字形**去重时预缩放**（键含 `scale`）——以少量重复的小路径换取描边/渐变/平铺涂料在最终 pt 空间的简单计算，`<use>` 只需 `x`/`y`/`fill`。
- 图像字形**使用处才缩放**（键不含 `scale`）——以每个 `<use>` 多带一个 `transform` 为代价，避免重复存储大体积图像数据；并因像素是 Y-Down 而做第二次 Y 翻转。
- 两条路径的取舍本质是「空间/体积 vs 计算简单性」的不同偏向，依据是「路径廉价、图像昂贵」这一事实。

## 7. 下一步学习建议

- 接着读 [u4-l2 字形定义与符号复用 write_glyph_defs](./u4-l2-glyph-defs.md)，看 `finalize` 如何把本讲产出的 `RenderedGlyph` 写成 `<defs><symbol>`，以及 `<use>` 的引用如何闭合整个复用链。
- 想深入涂料如何作用于字形，回到 [u3-l2 形状渲染与描边](./u3-l2-shape-rendering.md) 的 `write_fill`/`write_stroke`/`shape_paint_transform`，对照本讲的 `text_paint_transform`。
- 渐变/平铺本身的实现见第 5 单元（[u5-l2](./u5-l2-fill-stroke-dedup.md) 起），可印证「轮廓字形预缩放」为何让涂料计算更简单。
