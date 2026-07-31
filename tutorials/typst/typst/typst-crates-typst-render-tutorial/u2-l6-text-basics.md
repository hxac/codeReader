# 文本渲染基础

## 1. 本讲目标

学完本讲，你应该能够：

- 看懂 `render_text` 如何用一个「绘图笔」遍历整串字形，并用 `x_offset` / `y_offset` / `x_advance` / `y_advance` 计算每个字形的位置。
- 理解 `should_outline` 这个分流条件：它如何决定一个字形走「轮廓路径」还是「彩色字形子框架路径」。
- 理解 `upem` 与 `text_scale = size / upem` 的含义，以及为什么彩色字形需要预先 `pre_scale`，而轮廓字形不需要。

本讲只讲文本渲染的**骨架**（遍历、分流、缩放）。轮廓字形的快/慢光栅化细节（`pixglyph`、`write_bitmap`、`blend_src_over`）留到 **u3-l3**，渐变/平铺填充留到 **u3-l1 / u3-l2**。

## 2. 前置知识

### 2.1 字体的两种字形

字体里的一个「字」在内部叫**字形（glyph）**，用一个整数 ID（`glyph id`）表示。一个字形可以有两种存在形式：

- **轮廓字形（outline glyph）**：由数学曲线（贝塞尔）描述字形的外形，可以无损放大。普通黑白文字（拉丁字母、汉字轮廓）几乎都是这种，存在字体的 `glyf` / `cff` / `cff2` 表里。
- **彩色字形（color glyph）**：本身就带颜色和像素/矢量图层，常见于 emoji（😀）。它可能来自三种表：
  - `CBDT/CBLC`：PNG 位图字形（如 Noto Emoji、Apple Color Emoji）；
  - `COLR`：分层矢量彩色字形（如微软的 3D emoji）；
  - `SVG`：内嵌 SVG 的字形（如 Twitter Color Emoji）。

这两种字形需要**完全不同的渲染方式**，typst-render 就是用 `should_outline` 来决定走哪条路的。

### 2.2 em、units-per-em 与字形度量

- **em**：排版的相对长度单位，1 em 等于当前字号。
- **units-per-em（upem）**：字体设计者把 1 em 划分成多少个「字体单位」，例如 1000。字形的所有坐标、宽度都按这个字体单位给出。
- **advance（步进）**：画完这个字形后，绘图笔要前进多少。
- **offset（偏移）**：字形相对于当前笔位的额外平移（用于合字、复杂排版）。

在 Typst 里，`Glyph` 的 `x_advance` / `x_offset` 等是 `Em` 类型（相对值），需要乘以字号才能得到绝对长度。

### 2.3 承接前几讲的概念

本讲假设你已经了解（来自前置讲义）：

- **State 与坐标变换**（u2-l1）：`State` 是渲染递归中携带的不可变「状态背包」，`pre_translate` / `pre_scale` 会生成新的变换；`pre_*` 链式中「写在后者作用在先（更内层）」。
- **render_frame 派发**（u1-l3）：`FrameItem::Text` 会被 `render_frame` 派发给 `text::render_text`，派发前会先 `state.pre_translate(*pos)` 把整段文字定位到画布上。
- **Paint 转换**（u2-l3）：`paint::to_sk_paint` 把 Typst 的 `Paint` 转成 tiny-skia 画笔；本讲的轮廓填充就是用纯色 `Paint::Solid` 调它。
- **AbsExt**（u2-l1）：`Abs::to_f32()` 把 Typst 长度（基于 pt）转成 tiny-skia 需要的 `f32`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-render/src/text.rs` | 本讲主战场。`render_text` 遍历字形并分流；`render_outline_glyph` 处理轮廓字形（本讲只看入口，细节在 u3-l3）。 |
| `crates/typst-library/src/text/font/color.rs` | 提供 `should_outline`（分流条件）和 `glyph_frame`（把彩色字形变成一个可渲染的子 `Frame`）。 |
| `crates/typst-library/src/text/item.rs` | 定义 `TextItem`（一段排版好的文字）和 `Glyph`（单个字形及其度量）。 |

## 4. 核心概念与源码讲解

### 4.1 绘图笔模型：render_text 如何遍历字形串

#### 4.1.1 概念说明

一段排好版的文字在 Typst 里是一个 `TextItem`，它内部是一串 `Glyph`。要把这串字画到画布上，typst-render 用了最经典的**「绘图笔（pen）」模型**：

- 维护一个当前笔位 `(x, y)`，从 `(0, 0)` 开始。
- 对每个字形：
  1. 用「累积笔位 + 字形偏移」算出这个字形的实际位置；
  2. 把这个字形画出来；
  3. 把字形的步进（advance）累加回笔位，准备画下一个字。

注意 `x_advance` / `x_offset` 是 `Em`（相对值），必须先乘以字号 `text.size` 才是绝对长度——这正是 `.at(text.size)` 做的事。

#### 4.1.2 核心流程

`render_text` 的伪代码如下：

```
x = 0; y = 0                          # 笔位（绝对长度 Abs）
for glyph in text.glyphs:
    x_offset = x + glyph.x_offset.at(size)   # 这个字形的水平绝对位置
    y_offset = y + glyph.y_offset.at(size)   # 垂直绝对偏移

    if should_outline(font, glyph.id):
        state = state.pre_translate(x_offset, -y_offset)
        render_outline_glyph(...)            # 走轮廓路径
    else:
        text_scale = size / upem
        state = state.pre_translate(x_offset, -y_offset)
                       .pre_scale(text_scale)
        if let Some(frame) = glyph_frame(font, glyph.id):
            render_frame(canvas, state, &frame)   # 走彩色字形子框架路径

    x += glyph.x_advance.at(size)            # 笔位前进
    y += glyph.y_advance.at(size)
```

两个关键细节先记住，后面会展开：

1. **`-y_offset` 的负号**：字形的偏移/步进用的是**字体坐标系（Y 向上）**，而画布/Frame 用的是**屏幕坐标系（Y 向下）**，所以垂直方向要取负。
2. **两条路径的缩放位置不同**：轮廓路径只 `pre_translate` 不缩放（缩放交给 `render_outline_glyph` 内部）；彩色路径在前面就 `pre_scale(text_scale)`。

#### 4.1.3 源码精读

`render_text` 的完整实现（含字形遍历与笔位计算）：

[crates/typst-render/src/text.rs:15-41](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L15-L41) —— `render_text`：初始化笔位 `x`/`y`，遍历 `text.glyphs`，逐字计算 `x_offset`/`y_offset`、分流、最后累加 `x_advance`/`y_advance`。

逐行对应：

- L16–17：`let mut x = Abs::zero(); let mut y = Abs::zero();` —— 笔位从原点开始。
- L20：`let x_offset = x + glyph.x_offset.at(text.size);` —— 笔位 + 字形水平偏移 = 该字形原点的水平位置。`Em::at(size)` 就是 `size * em.get()`：
  \[
  x_{\text{offset}} = x_{\text{笔}} + x_{\text{offset,em}} \cdot \text{size}
  \]
- L23：`if should_outline(&text.font, id)` —— 分流，见 4.2。
- L24：`state.pre_translate(Point::new(x_offset, -y_offset))` —— 把笔位落到 state 变换里，注意 `y` 取负。
- L38–39：`x += glyph.x_advance.at(text.size); y += glyph.y_advance.at(text.size);` —— 笔位前进，循环到下一个字。

`TextItem` 与 `Glyph` 的字段定义在这里：

[crates/typst-library/src/text/item.rs:13-31](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/item.rs#L13-L31) —— `TextItem`：一段排好版的文字，含 `font`、`size: Abs`、`fill: Paint`、`stroke` 和 `glyphs: Vec<Glyph>`。

[crates/typst-library/src/text/item.rs:94-110](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/item.rs#L94-L110) —— `Glyph`：`id: u16`（字形索引），`x_advance / x_offset / y_advance / y_offset` 全是 `Em`（相对字号的比例）。

`Em::at` 把相对值换算成绝对长度：

[crates/typst-library/src/layout/em.rs:61-64](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/em.rs#L61-L64) —— `Em::at(font_size)` 返回 `font_size * self.get()`，即「相对值 × 字号」。

#### 4.1.4 代码实践：手算三个字形的笔位轨迹

**实践目标**：用具体数字体会绘图笔模型，验证「笔位 = 上一个字的累积步进」。

**操作步骤**：

1. 假设字号 `text.size = 10 pt`，一段文字含 3 个字形，它们的 `x_offset` 都是 `0 em`，`x_advance` 分别为 `0.5 em`、`0.5 em`、`0.4 em`。
2. 模拟 `render_text` 的循环，逐字写出笔位 `x` 和 `x_offset`（取 `.at(10pt)` 后的绝对值）。

**需要观察的现象**：每个字形被画在「前一个字形步进累加」出的位置上，字形自身的 `x_offset` 只在本字计算时叠加一次，不会进入下一字的笔位。

**预期结果**：

| 字形 # | 进入时 `x`（pt） | `x_offset`（pt） | 画图位置 | 画完后 `x`（pt） |
| --- | --- | --- | --- | --- |
| 1 | 0 | 0 | 0 | 0 + 0.5×10 = 5 |
| 2 | 5 | 0 | 5 | 5 + 5 = 10 |
| 3 | 10 | 0 | 10 | 10 + 4 = 14 |

（合字 / 复杂排版时 `x_offset` 非零，会把字形相对笔位再平移；笔位本身仍只由 `x_advance` 推进。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `render_text` 里用的是 `glyph.x_advance.at(text.size)`，而不能直接用 `glyph.x_advance`？

**答案**：`x_advance` 是 `Em`（相对字号的比值），必须乘以字号才是 pt 长度；`.at(text.size)` 就是做这个乘法。直接用 `Em` 没有量纲，无法和 `x`（`Abs`）相加。

**练习 2**：一个合字（ligature）把 "fi" 两个字符合并成一个字形，字形数和字符数还相等吗？

**答案**：不一定相等。`TextItem` 的注释明确指出：由于合字等原因，字形数可能与字符数不同（参见 [item.rs:28-30](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/item.rs#L28-L30)）。

---

### 4.2 分流条件 should_outline：画轮廓还是画彩色

#### 4.2.1 概念说明

每画一个字形，typst-render 都要做一个二选一的决定：

- **轮廓路径**：用字体的矢量轮廓（曲线）去画，再按 `text.fill` 上色。适用于所有普通黑白文字。
- **彩色字形路径**：字形本身带颜色图层（emoji 等），不能再当成「轮廓 + 单色填充」处理，要转成一个子 `Frame` 用通用渲染器画。

`should_outline` 就是这个分流器。它的判断依据是**字体表的结构**，而跟字号、颜色无关。

#### 4.2.2 核心流程

`should_outline(font, glyph_id)` 返回 `true`（走轮廓）的判定逻辑：

```
字体有轮廓表 (glyf / cff / cff2)
  且 该字形 没有 PNG 位图
  且 该字形 不是 COLR 彩色字形
  且 该字形 没有 SVG 图像
→ true（走 render_outline_glyph）
否则 → false（走 glyph_frame）
```

也就是：**只要这个字形带着任何「彩色/位图/SVG」图层，就不走轮廓；只有纯黑白矢量字形才走轮廓。** 此外，如果字体根本没有轮廓表（某些纯位图字体 CBDT），所有字形都只能走 `glyph_frame`。

#### 4.2.3 源码精读

分流条件本身定义在 typst-library（不在 render crate）：

[crates/typst-library/src/text/font/color.rs:19-29](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/font/color.rs#L19-L29) —— `should_outline`：检查字体是否有 `glyf/cff/cff2` 轮廓表，且字形不是 PNG 位图、不是 COLR 彩色字形、没有 SVG 图像。四个条件全满足才返回 `true`。

`render_text` 里调用它的 `if/else` 分支：

[crates/typst-render/src/text.rs:23-36](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L23-L36) —— `render_text` 的分流分支：`should_outline` 为真走 `render_outline_glyph`（只 `pre_translate`）；为假算 `text_scale` 并 `pre_scale`，再尝试 `glyph_frame` 递归渲染。

注意一个不对称：**轮廓分支一定能画出东西**（普通字形必有轮廓），所以 `render_outline_glyph` 返回 `Option<()>` 但调用处不 `?` 传播；而**彩色分支可能失败**——`glyph_frame` 返回 `Option`，若为 `None`（例如空格字形在某些位图字体里没有图），就什么都不画（`if let Some(frame)` 静默跳过）。

#### 4.2.4 代码实践：判断一个 emoji 的分流结果

**实践目标**：通过阅读 `should_outline` 的实现，预测给定字形走哪条路径。

**操作步骤**：

1. 想象用 Noto Color Emoji 字体渲染 "😀"。该字体的 "😀" 字形是一个 **PNG 位图字形**（来自 `CBDT` 表），且字体本身通常没有 `glyf` 表。
2. 对照 [color.rs:19-29](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/font/color.rs#L19-L29) 的四个条件逐项判断。

**需要观察的现象**：第一个条件「有 `glyf/cff/cff2`」对纯位图 emoji 字体为假，整个 `&&` 表达式短路为 `false`。

**预期结果**：`should_outline` 返回 `false` → "😀" 走 **彩色字形路径**（`glyph_frame`）。这正是 typst 能正确渲染彩色 emoji 的原因。该路径有集成测试参考图佐证：`tests/ref/render/shaping-emoji-basic.png` 等就是 emoji 渲染的回归基准图。

#### 4.2.5 小练习与答案

**练习 1**：一个普通拉丁字母 "A"（用 DejaVu Sans 这种常规字体），`should_outline` 会返回什么？为什么？

**答案**：返回 `true`。常规字体有 `glyf`/`cff` 表，"A" 是纯矢量轮廓字形，没有 PNG/COLR/SVG 图层，四个条件全满足。

**练习 2**：如果 `should_outline` 误把一个 emoji 判成 `true`，会发生什么？

**答案**：`render_outline_glyph` 会尝试取该字形的矢量轮廓（`outline_glyph`），但彩色字形通常没有传统轮廓（或轮廓为空），结果要么画不出、要么画出错误的黑白形状，丢失颜色。

---

### 4.3 轮廓路径 render_outline_glyph：普通字形的入口（概览）

#### 4.3.1 概念说明

`render_outline_glyph` 处理所有 `should_outline == true` 的字形——也就是绝大多数日常文字。本讲只讲它的**入口与缩放逻辑**，内部的光栅化（`pixglyph`）和像素混合留到 **u3-l3**。

它有两个值得现在就理解的设计：

1. **它自己负责字号缩放**。正因为如此，`render_text` 的轮廓分支只 `pre_translate`、**不** `pre_scale`——缩放被推迟到函数内部做。这与彩色分支形成鲜明对比。
2. **它要处理「字体坐标系 Y 向上、画布 Y 向下」的翻转**。字体轮廓的坐标是 Y 向上的，画到 tiny-skia（Y 向下）时必须垂直翻转。

#### 4.3.2 核心流程

```
ts = state.transform                       # 当前累积变换（含 pixel_per_pt、平移）
ppem = text.size * ts.sy                   # 一个 em 等于多少像素（像素字号）

if ppem > 100 或 有倾斜(kx/ky) 或 非均匀缩放(sx≠sy) 或 有描边:
    慢路径：把字形轮廓建成 sk::Path，
            scale = text.size / upem，
            ts' = ts.pre_scale(scale, -scale)   # 字体单位→pt，并翻转 Y
            fill_path / stroke_path
else:
    快路径：pixglyph 按 ppem 把字形光栅化成位图，
            write_bitmap 用 fill 颜色把位图画上去（细节见 u3-l3）
```

关键量 **ppem**（pixels per em）= 字号 × 当前垂直缩放，反映「这个字最终要画成多大（像素）」。它既是分流条件，也是快路径光栅化的尺寸参数。

#### 4.3.3 源码精读

[crates/typst-render/src/text.rs:44-62](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L44-L62) —— `render_outline_glyph` 入口：取 `ts = state.transform`、算 `ppem = text.size.to_f32() * ts.sy`，并给出慢路径触发条件（ppem 过大/为负、有 kx/ky、sx≠sy、有描边）。

慢路径里把字体单位换算成 pt 并翻转 Y 的两行：

[crates/typst-render/src/text.rs:69-78](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L69-L78) —— `scale = text.size.to_f32() / units_per_em`，随后 `ts.pre_scale(scale, -scale)`：先把字体单位缩到 pt，`-scale` 负号把字体坐标系的 Y 向上翻成画布的 Y 向下。注意 `scale` 与本讲 4.4 的 `text_scale` 是同一个比值。

> 这里 `units_per_em()` 返回 `f64`（见 [font/mod.rs:213-215](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/font/mod.rs#L213-L215)），所以 `scale = size.to_f32() / upem` 是一个无单位的 `f32` 比值。

填充用的画笔就是 u2-l3 讲过的 `to_sk_paint`，这里 `on_text = true`、`shape = None`：

[crates/typst-render/src/text.rs:79-81](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L79-L81) —— 慢路径用 `paint::to_sk_paint(&text.fill, state_ts, true, &mut pixmap, None, false)` 造画笔，再 `canvas.fill_path(...)`。描边（若有）紧随其后。

快路径（光栅化）的入口只作概览，细节在 u3-l3：

[crates/typst-render/src/text.rs:104-124](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L104-L124) —— 快路径：`#[comemo::memoize]` 的 `rasterize` 用 `pixglyph` 按 `ppem` 光栅化字形为 `Bitmap`，再据 `text.fill` 类型走 `write_bitmap`（Solid/Gradient/Tiling 三选一）。

#### 4.3.4 代码实践：找出快慢路径的分界

**实践目标**：理解什么情况下普通字形会「退化」成慢路径（路径绘制）。

**操作步骤**：

1. 阅读 [text.rs:56-62](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L56-L62) 的 `if` 条件。
2. 对下列三种情形，判断走快路径还是慢路径：(a) 12pt 普通正文、`pixel_per_pt=2`；(b) 一段被旋转/倾斜的文字；(c) 给文字加了描边（`stroke`）。

**需要观察的现象**：只有「中等字号 + 无倾斜 + 无描边 + 均匀缩放」才走快的位图光栅化；任何极端条件都退回路径绘制。

**预期结果**：

- (a) `ppem = 12 × 2 = 24`，无 kx/ky、无描边、sx==sy → **快路径**。
- (b) 有 `kx != 0` 或 `ky != 0` → **慢路径**。
- (c) `text.stroke.is_some()` → **慢路径**。

#### 4.3.5 小练习与答案

**练习 1**：为什么「字号特别大（ppem > 100）」要走慢路径？

**答案**：ppem 极大意味着要把字形光栅化成超大位图，内存和耗时都会暴涨；此时直接用矢量路径绘制（`fill_path`）更经济、质量也更好。注释 [text.rs:53-55](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L53-L55) 正是这么说的。

**练习 2**：`render_text` 的轮廓分支为什么只 `pre_translate` 不 `pre_scale`？

**答案**：因为字号→字体单位的缩放由 `render_outline_glyph` 内部统一完成（慢路径算 `scale`、快路径算 `ppem`），调用方不需要预先缩放。

---

### 4.4 彩色字形路径 glyph_frame：把 emoji 变成子框架

#### 4.4.1 概念说明

当一个字形 `should_outline == false`（emoji、COLR、SVG 字形等），它不能当轮廓画。typst 的做法很优雅：

1. `glyph_frame(font, glyph_id)` 把这个彩色字形转成一个 **`Frame`**——一个尺寸为 `upem × upem`（字体单位）的小框架，里面装着一个 `Image`（PNG 位图 / COLR 转出的 SVG / SVG 字形）或一个兜底的「豆腐块」`Tofu`。
2. `render_text` 算出 `text_scale = size / upem`，用它 `pre_scale` 把这个字体单位的框架放大到字号尺寸。
3. 直接调用通用的 `crate::render_frame(canvas, state, &frame.into())` 把它画出来——**复用整棵 Frame 渲染器**，彩色字形其实被当成「一张小图」来渲染。

`glyph_frame` 还标了 `#[comemo::memoize]`，同一个彩色字形只解码一次。

#### 4.4.2 核心流程

```
upem = font.units_per_em()           # f64，例如 1000
text_scale = text.size / upem        # Abs：1 字体单位 = (size/upem) pt

state = state
    .pre_translate(x_offset, -y_offset)   # 落到字形原点（Y 取负）
    .pre_scale(text_scale)                # 字体单位 → pt

if let Some(frame) = glyph_frame(font, glyph_id):   # 取字体单位下的子框架
    render_frame(canvas, state, &frame.into())      # 复用通用渲染器
```

`text_scale` 的几何含义：

\[
\text{text\_scale} = \frac{\text{size}}{\text{upem}}
\]

即「每个字体单位对应多少 pt」。因为 1 em = `upem` 个字体单位 = `size` pt，所以 1 字体单位 = `size / upem` pt。把 `upem × upem` 的框架按 `text_scale` 缩放后，正好变成 `size × size` pt，再由 state 里原有的 `pixel_per_pt` 缩成像素。

#### 4.4.3 源码精读

彩色分支的缩放链（本讲的核心）：

[crates/typst-render/src/text.rs:27-35](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L27-L35) —— `render_text` 彩色分支：算 `upem = text.font.units_per_em()`、`text_scale = text.size / upem`，`state.pre_translate(...).pre_scale(...)` 后，`glyph_frame` 返回 `Some(frame)` 时调 `crate::render_frame` 递归渲染。

`text_scale` 的类型推导：`text.size` 是 `Abs`、`upem` 是 `f64`，`Abs / f64 → Abs`，所以 `text_scale` 是 `Abs`，正好满足 `pre_scale(Axes<Abs>)` 的签名。

`glyph_frame` 把彩色字形变成 `Frame`：

[crates/typst-library/src/text/font/color.rs:88-103](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/font/color.rs#L88-L103) —— `#[comemo::memoize] glyph_frame`：按 PNG 位图 / COLR / SVG 三类尝试 `draw_glyph`，画不出来且不是空格时返回兜底「豆腐块」。

`GlyphFrame` → `Frame` 的转换，可以看到框架尺寸就是 `upem × upem`（字体单位）：

[crates/typst-library/src/text/font/color.rs:45-58](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/font/color.rs#L45-L58) —— `From<GlyphFrame> for Frame`：`Frame::soft(Size::splat(g.upem))` 建一个 `upem × upem` 的软框架，把 `Tofu` 当 Shape、把彩色字形当 Image 塞进去。

子框架里装的东西只有两种：

[crates/typst-library/src/text/font/color.rs:61-67](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/font/color.rs#L61-L67) —— `GlyphFrameItem`：`Tofu(Point, Shape)`（兜底矩形）或 `Image(Point, Image, Size)`（PNG/COLR/SVG 字形）。它们都会被 `render_frame` 当成普通的 Shape/Image 画出来——这正是「复用通用渲染器」的妙处。

#### 4.4.4 代码实践：画出 emoji 字形的 state 缩放链（本讲核心任务）

**实践目标**：把「绘图笔定位 → 字体单位缩放 → 递归渲染」这条链画清楚，彻底理解 `text_scale = size / upem`。

**操作步骤**：

1. 设想渲染一个 "😀"，字号 `text.size = 18 pt`，字体 `units_per_em() = 1000`，该字形 `should_outline == false` 且 `glyph_frame` 返回 `Some`。
2. 设进入这个字形时，`state.transform` 已是 `T0`（它至少包含 `pixel_per_pt = 2`，加上版面平移）。
3. 按 [text.rs:29-35](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L29-L35) 写出每一步的 state。

**需要观察的现象**：缩放链是「先平移到字形原点（更内层是先 scale），再由 `render_frame` 内部按已有 `pixel_per_pt` 继续缩放」。注意 `pre_*` 链式语义——「写在后者作用在先」（更靠近被绘制的点，见 u2-l1）。

**预期结果**（缩放链）：

```
text_scale = 18 / 1000 = 0.018 pt/字体单位

state₁ = state.pre_translate(x_offset, -y_offset)          # 平移到字形原点（Y 翻转）
state₂ = state₁.pre_scale(Axes::splat(0.018))              # 字体单位 → pt

最终 transform = T0 ∘ translate(x_offset, -y_offset) ∘ scale(0.018)
                 └──── 外层：pt→像素+版面定位 ──┘  └ 中层：定位 ─┘  └ 内层：字体单位→pt ┘
```

于是 `glyph_frame` 产出的 `1000 × 1000`（字体单位）框架被缩成 `18 × 18` pt，再乘 `pixel_per_pt = 2` 得到约 `36 × 36` 像素的彩色 emoji。

**关于 `text_scale = size / upem` 的含义**：它是「字体设计单位」到「排版点（pt）」的换算系数。字体里所有坐标都以 `upem` 为分母（1 em = upem 个单位），而排版时 1 em = `size` pt，所以 1 个字体单位 = `size / upem` pt。这个比值让 `glyph_frame` 产出的字体单位框架精确对应到当前字号。

> 若想真正跑一遍，可用 typst-cli 编译一个含 emoji 的文档为 PNG：写一个 `.typ` 文件包含彩色 emoji（需系统/字体目录提供彩色 emoji 字体），执行 `typst compile file.typ out.png`（PNG 导出链见 u1-l1）。emoji 是否上色取决于本机是否装了对应字体，**渲染结果待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`text_scale` 的单位是什么？为什么彩色分支要 `pre_scale` 而轮廓分支不要？

**答案**：`text_scale` 的单位是 pt/字体单位。彩色分支的 `glyph_frame` 产出的子框架是字体单位（`upem × upem`）尺寸，必须先缩放到 pt 才能交给通用的 `render_frame`；而轮廓分支的缩放在 `render_outline_glyph` 内部完成，所以不需要。

**练习 2**：`glyph_frame` 返回 `None` 时 `render_text` 会怎样？给出一种触发场景。

**答案**：`if let Some(frame)` 不匹配，什么都不画（见 [text.rs:33-35](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L33-L35)）。典型场景是某些 CBDT 位图字体里空格没有位图、也没有轮廓表，`glyph_frame` 对空格字形返回 `None`（参见 [color.rs:97-102](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/font/color.rs#L97-L102)），空格本就该不画东西。

---

## 5. 综合实践

**任务**：用一张「双字形」追踪图，把本讲的遍历、分流、缩放三件事串起来。

设想 `render_text` 收到一段 `TextItem`，含两个字形：

- 字形 A：普通拉丁字母（`should_outline == true`）；
- 字形 B：紧随其后的一个彩色 emoji（`should_outline == false`，`glyph_frame` 返回 `Some`）。

请完成：

1. **遍历**：写出 A、B 各自进入循环时的笔位 `x`（用 `x_advance.at(size)` 表达），并指出派发层已经把整段文字 `pre_translate(*pos)` 过一次（见 u1-l3 的 `render_frame`）。
2. **分流**：分别写出 A 和 B 走哪条路径、调用哪个函数。
3. **缩放**：分别写出 A 和 B 在调用渲染函数**之前**对 state 做了哪些 `pre_*` 操作，重点解释为什么 A 不 `pre_scale` 而 B 要 `pre_scale(text_scale)`。

**参考要点**：

- A：`render_outline_glyph`，state 仅 `pre_translate(x_offset, -y_offset)`；缩放（`scale = size/upem` 或 `ppem`）在函数内部做。
- B：先 `pre_translate(x_offset, -y_offset)` 再 `pre_scale(text_scale)`，然后 `render_frame` 画 `glyph_frame` 产出的字体单位子框架；缩放必须前置，因为通用渲染器只认 pt + `pixel_per_pt`。
- 两者都把 `y_offset` 取负，原因是字体坐标系 Y 向上、Frame/画布 Y 向下。

## 6. 本讲小结

- `render_text` 用「绘图笔」遍历 `text.glyphs`：笔位由 `x_advance`/`y_advance` 累积推进，字形原点由笔位 + `x_offset`/`y_offset` 定位；`Em` 值都要 `.at(text.size)` 换算成 pt。
- `should_outline` 是分流器：字体有轮廓表且字形不带任何 PNG/COLR/SVG 彩色图层时返回 `true`（走轮廓），否则走彩色字形路径。
- 轮廓路径 `render_outline_glyph` 自己负责字号缩放（慢路径 `scale = size/upem` + Y 翻转，快路径用 `ppem` 光栅化），所以调用方只 `pre_translate`。
- 彩色路径 `glyph_frame` 把彩色字形变成一个 `upem × upem` 的子 `Frame`（装一张 Image 或兜底 Tofu），用 `text_scale = size/upem` 预先 `pre_scale` 后，复用 `render_frame` 渲染。
- `text_scale = size / upem` 是「字体单位 → pt」的换算系数；`-y_offset` 的负号来自字体 Y 向上与画布 Y 向下的坐标差异。

## 7. 下一步学习建议

- **u3-l3（字形光栅化与像素级混合）**：深入 `render_outline_glyph` 的快路径——`pixglyph` 如何按 `ppem` 光栅化、`write_bitmap` 的有/无遮罩两条分支、`blend_src_over`/`alpha_mul` 的预乘 alpha 位运算。这是本讲 4.3 留下的直接续篇。
- **u3-l1 / u3-l2（渐变与平铺）**：当 `text.fill` 不是纯色而是渐变或平铺图案时，`render_outline_glyph` 会用 `GradientSampler` / `TilingSampler` 在字形像素上采样（见 [text.rs:125-143](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L125-L143)）。
- 想巩固「Frame 复用」的设计，可回看 u1-l3（`render_frame` 派发）与 u2-l2（`render_group` 三段式），理解彩色字形子框架为何能无缝套进同一套递归渲染机制。
