# 字形光栅化与像素级混合

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `render_outline_glyph` 为什么要把字形分成「慢路径（路径绘制）」和「快路径（pixglyph 光栅化）」两条路，并能逐条解释触发慢路径的五个条件。
- 理解 `ppem = text.size.to_f32() * ts.sy` 这个关键量的物理含义，以及它如何成为快路径光栅化与记忆化缓存的输入。
- 读懂 `write_bitmap` 在「有遮罩」与「无遮罩」两条分支上的差异，特别是为什么有遮罩时要先渲染到一张 `mw+2 × mh+2` 的临时画布，再用 `draw_pixmap` 贴回主画布。
- 推导 `blend_src_over` 与 `alpha_mul` 这两个从 Skia 移植来的位运算函数，理解 premultiplied alpha 的 src-over 公式与 `0xff00ff` 掩码背后的「两次乘法完成四通道」技巧。

本讲是 [u2-l6 文本渲染基础](u2-l6-text-basics.md) 的深入：u2-l6 讲了文本渲染的骨架（遍历、`should_outline` 分流、`text_scale` 缩放），本讲则钻进轮廓路径里那块最硬的骨头——字形如何变成像素，以及像素之间如何混合。

## 2. 前置知识

阅读本讲前，请确保你已经掌握以下概念（它们在前序讲义中已建立，这里只做最小回顾）：

- **Frame 场景树与 State**（u1-l3、u2-l1）：渲染递归中 `State` 随身携带当前累积变换 `transform`（含根部的 `pixel_per_pt` 缩放）、可选 `mask`（裁剪遮罩）等。本讲里的 `ts` 就是 `state.transform`，`ts.sy` 是它的 Y 方向缩放分量。
- **premultiplied alpha（预乘 alpha）**：一种像素存储约定。普通（straight）alpha 下，颜色通道记录「纯色浓度」；预乘 alpha 下，颜色通道已被乘以自身的 alpha。例如半透明红色 `(R=255,G=0,B=0,A=128)` 预乘后变成 `(R=128,G=0,B=0,A=128)`。预乘的好处是合成公式更简单、合成时少一次乘法。本讲的 `blend_src_over` / `alpha_mul` **一律在预乘空间**操作。
- **src-over 合成**：把源图层「叠」到目标图层上。在预乘空间里，它的公式是

  \[
  C_{\text{out}} = C_{\text{src}} + (1 - \alpha_{\text{src}})\,C_{\text{dst}}
  \]

  即「源直接加上目标被挖去源 alpha 之后剩下的部分」。本讲末尾会把这个公式与代码逐项对上。
- **字形覆盖率（coverage）**：抗锯齿渲染下，字形在每个像素上并非「全有/全无」，而是一个 0–255 的浓度值，表示这个像素被字形覆盖的比例。本讲里 `bitmap.coverage` 就是这张浓度图。

> 术语提示：本讲多次出现「像素（pixel）」「画布（canvas / pixmap）」「画布像素坐标」。除非特别说明，「画布像素坐标」指最终输出 PNG 里的整数像素位置 `(x, y)`，原点在左上角，Y 向下。

## 3. 本讲源码地图

本讲几乎全部集中在 `src/text.rs`，辅以 `src/paint.rs` 和 `src/lib.rs`：

| 文件 | 作用 |
| --- | --- |
| [src/text.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs) | 文本渲染主体：`render_text` 遍历字形；`render_outline_glyph` 快慢路径分流；`rasterize` 调 pixglyph；`write_bitmap` 把位图写入画布；`blend_src_over`/`alpha_mul` 像素混合；`WrappedPathBuilder` 适配器。 |
| [src/paint.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs) | `PaintSampler` trait 与 `GradientSampler`/`TilingSampler`、`to_sk_color_u8`，提供「按画布位置采样一个颜色」的统一接口，供 `write_bitmap` 逐像素取色。 |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs) | `State` 结构定义（`transform`/`mask`/`pixel_per_pt` 等字段），`AbsExt::to_f32`。 |

## 4. 核心概念与源码讲解

### 4.1 render_outline_glyph 与快慢路径分流

#### 4.1.1 概念说明

在 u2-l6 里我们已知：一段文字中的每个字形，如果 `should_outline` 返回 `true`（字体有矢量轮廓表 `glyf/cff/cff2`、且字形不带 PNG/COLR/SVG 彩色图层），就走「轮廓渲染」路径，交给本讲的 `render_outline_glyph`。

把字形轮廓变成像素，本质上只有两种做法：

1. **路径绘制（慢路径）**：取出字形的贝塞尔轮廓，构造一条 `tiny-skia::Path`，用 2D 引擎的 `fill_path` / `stroke_path` 去填充/描边。优点：对任意变换（旋转、斜切、非均匀缩放）都正确，支持描边；缺点：每次都要重新做几何处理与抗锯齿，开销大。
2. **直接光栅化（快路径）**：用专门的字形光栅化库 `pixglyph`，针对「纯平移 + 均匀缩放」这种最常见的情形，把字形直接算成一张覆盖率位图（coverage map），再手工贴回画布。优点：极快，且支持记忆化缓存；缺点：只能处理轴对齐的均匀缩放，无法处理斜切/非均匀缩放/描边/翻转。

`render_outline_glyph` 的核心就是**根据当前变换和文本特征，在两条路里二选一**。这个判定的依据是一个关键量 `ppem`。

#### 4.1.2 核心流程

```text
render_outline_glyph(canvas, state, text, id):
  ts  = state.transform            # 累积变换（含 pixel_per_pt）
  ppem = text.size.to_f32() * ts.sy   # 像素/em

  if 满足任一慢路径条件:
      【慢路径】
      1. 用 WrappedPathBuilder 把字形轮廓流式拼成 sk::Path
      2. scale = size / upem；构造 Y 翻转变换 ts' = ts.pre_scale(scale, -scale)
      3. fill_path(path, paint, ts', mask)            # 填充
      4. 若有描边且 thickness>0：stroke_path(...)      # 描边
      return
  else:
      【快路径】
      bitmap = rasterize(font, id, ts.tx, ts.ty, ppem)   # 记忆化
      按 fill 类型（Solid/Gradient/Tiling）选 sampler
      write_bitmap(canvas, bitmap, state, sampler)       # 写入画布
```

#### 4.1.3 源码精读

先看入口与 `ppem` 的计算，以及慢路径的触发条件：

[src/text.rs:L44-L62](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L44-L62) —— `render_outline_glyph` 的签名、`ppem` 计算，以及慢路径的五个判定条件。

```rust
fn render_outline_glyph(
    canvas: &mut sk::Pixmap,
    state: State,
    text: &TextItem,
    id: GlyphId,
) -> Option<()> {
    let ts = &state.transform;
    let ppem = text.size.to_f32() * ts.sy;

    if ppem > 100.0
        || ppem < 0.0
        || ts.kx != 0.0
        || ts.ky != 0.0
        || ts.sx != ts.sy
        || text.stroke.is_some()
    { /* 慢路径 … */ }
```

理解 `ppem`：

- `text.size` 是字号（`Abs`，单位 pt），`.to_f32()` 转成 pt 的 f32（见 [AbsExt](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L282-L286)）。
- `ts.sy` 是累积变换的 Y 缩放分量。因为 `State` 在根部就被 `pre_scale(pixel_per_pt, pixel_per_pt)` 初始化（见 [render](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L29-L30)），且文本层只 `pre_translate` 不额外缩放，所以 `ts.sy` 至少含 `pixel_per_pt`；若文本位于某个被缩放的 group 内，`ts.sy` 还会叠上那些祖先缩放。
- 二者相乘得到的 `ppem`（pixels per em）就是「一个 em 在最终画布上占多少像素」。em 按定义等于字号的 pt 值，所以「字号(pt) × 像素/pt = 像素/em」自洽。

> 为什么用 `ts.sy` 而不是直接用 `pixel_per_pt`？因为文本可能嵌套在缩放过的 group 里。用累积变换的 `sy` 才能反映字形在画布上的**真实**像素尺寸，记忆化键（见 4.2）也因此能在不同缩放上下文下区分命中。

慢路径的五个条件，逐条解释：

| 条件 | 含义 | 为什么必须走慢路径 |
| --- | --- | --- |
| `ppem > 100.0` | 字号过大 | 快路径要生成一张与字号同尺寸的覆盖率位图；100+ px/em 会让位图过大、内存与时间都吃不消，此时直接画矢量路径反而更划算。 |
| `ppem < 0.0` | Y 缩放为负（上下翻转） | pixglyph 不支持翻转光栅化。 |
| `ts.kx != 0.0 \|\| ts.ky != 0.0` | 存在斜切（shear） | pixglyph 只做轴对齐光栅化，斜切会扭曲字形。 |
| `ts.sx != ts.sy` | X/Y 缩放不等（非均匀缩放） | pixglyph 假设均匀缩放；非均匀会拉伸字形，必须用路径。 |
| `text.stroke.is_some()` | 文本带描边 | 描边需要 `stroke_path`，必须先有路径；快路径只生成填充覆盖率，无法描边。 |

任一条件成立即走慢路径。慢路径本身（路径构造、填充、描边）如下：

[src/text.rs:L63-L102](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L63-L102) —— 慢路径：构造路径、Y 翻转、填充与描边。

```rust
let path = {
    let mut builder = WrappedPathBuilder(sk::PathBuilder::new());
    text.font.ttf().outline_glyph(id, &mut builder)?;
    builder.0.finish()?
};
let scale = text.size.to_f32() / text.font.units_per_em() as f32;
// …
// Flip vertically because font design coordinate system is Y-up.
let ts = ts.pre_scale(scale, -scale);
let state_ts = state.pre_concat(sk::Transform::from_scale(scale, -scale));
let paint = paint::to_sk_paint(&text.fill, state_ts, true, &mut pixmap, None, false);
canvas.fill_path(&path, &paint, rule, ts, state.mask);
```

要点：

- `WrappedPathBuilder` 是把 `ttf_parser` 的 `OutlineBuilder` trait 适配到 `tiny-skia::PathBuilder` 上的桥（见 4.1.3 末尾）。`outline_glyph` 会回调 `move_to/line_to/quad_to/curve_to/close`，把字形轮廓的贝塞尔段流式灌进 `sk::Path`。
- `scale = size / upem`：字体设计单位 → pt 的换算（与 u2-l6 的 `text_scale` 同义，只是这里是裸 f32）。
- `ts.pre_scale(scale, -scale)`：在已有变换上「先做 scale 再做原变换」。负号翻转 Y——字体设计坐标系 Y 向上，画布 Y 向下，必须翻转。这个局部 `ts` 只用于传给 `fill_path`/`stroke_path` 作为路径变换。
- 填充与描边都复用同一条 `path`，由 tiny-skia 处理抗锯齿和遮罩（`state.mask`）。注意慢路径里渐变/平铺走的是 `to_sk_paint` 返回的 `Pattern` 着色器（纹理），而不是 4.3 的逐像素采样——这正对应 u3-l1 提到的「大字号慢路径文本走纹理着色器」。
- 描边分支里 `width: thickness.to_f32() / scale`（[L92](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L91-L97)）值得注意：因为路径是在 `ts`（含 scale）变换下绘制的，描边宽度会被同一变换放大，所以要预先除以 `scale`，使其最终物理宽度等于 `thickness`。

最后看 `WrappedPathBuilder` 这个适配器本身：

[src/text.rs:L241-L264](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L241-L264) —— `WrappedPathBuilder`：把 `ttf_parser::OutlineBuilder` 适配到 `tiny_skia::PathBuilder`。

```rust
struct WrappedPathBuilder(sk::PathBuilder);

impl OutlineBuilder for WrappedPathBuilder {
    fn move_to(&mut self, x: f32, y: f32) { self.0.move_to(x, y); }
    fn line_to(&mut self, x: f32, y: f32) { self.0.line_to(x, y); }
    fn quad_to(&mut self, x1: f32, y1: f32, x: f32, y: f32) {
        self.0.quad_to(x1, y1, x, y);
    }
    fn curve_to(&mut self, x1: f32, y1: f32, x2: f32, y2: f32, x: f32, y: f32) {
        self.0.cubic_to(x1, y1, x2, y2, x, y);
    }
    fn close(&mut self) { self.0.close(); }
}
```

它纯粹是一对一转发：`ttf_parser` 用 `curve_to`（三次贝塞尔）输出字形轮廓，对应 `tiny-skia` 的 `cubic_to`；`quad_to`（二次贝塞尔）对应 `quad_to`。有了它，`outline_glyph` 就能把字体里的字形轮廓「直通」拼成 `sk::Path`，无需中间数据结构。

#### 4.1.4 代码实践

**实践目标**：观察慢路径触发条件对渲染结果与性能的影响，重点验证 `ppem > 100.0` 这条阈值。

**操作步骤（源码阅读型 + 参数修改型）**：

1. 在 [src/text.rs:L56-L62](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L56-L62) 的 `if` 条件里，把 `ppem > 100.0` 暂时改成 `ppem > 100000.0`（实际上关掉这条，让大字号也走快路径）。
2. 构造一个会触发该条件的文档：用 typst 渲染一个很大的标题，例如 `#text(size: 200pt)[A]`，并以 PNG 导出。
3. 在 `render_outline_glyph` 慢路径入口（约 [L63](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L63)）和快路径入口（约 [L104](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L104)）各加一行 `eprintln!("path ppem={ppem}")` / `eprintln!("raster ppem={ppem}")`，分别观察两条路径被命中的字号区间。

**需要观察的现象**：

- 改阈值前：小字号（如 12pt）走快路径（打印 `raster`），200pt 走慢路径（打印 `path`）。
- 改阈值后：200pt 也被逼到快路径。

**预期结果**：200pt 的字形在快路径下，pixglyph 要生成一张很大的覆盖率位图，内存与耗时显著上升；极端情况下可能变慢甚至明显占用更多内存。这解释了为什么作者把阈值设在 100——大字号时矢量路径反而更经济。

> ⚠️ 本实践会**临时修改源码**，验证后请务必还原。本讲义不鼓励你提交改动。若无法本地编译 typst，可只做第 3 步的日志阅读，对照手算 `ppem`（如 `size=12pt, pixel_per_pt=2 → ppem=24`；`size=200pt → ppem=400`，超过 100 走慢路径），其余标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：某文档以默认 `pixel_per_pt = 2.0` 渲染，某段文字字号为 `60pt`，且这段文字位于一个 `scale 150%` 的 group 内。它的 `ppem` 是多少？走快路径还是慢路径？

**答案**：累积变换的 `ts.sy` 包含根部 `pixel_per_pt=2.0` 与 group 的 `1.5`，故 `ts.sy = 3.0`；`ppem = 60 × 3.0 = 180 > 100`，走**慢路径**。

**练习 2**：为什么描边文本（`text.stroke.is_some()`）无论字号多小都必须走慢路径？

**答案**：快路径只产出一张填充用的覆盖率位图，无法表达描边的「沿线线条」。描边必须基于矢量路径用 `stroke_path` 绘制，所以只要存在描边就只能走慢路径。

**练习 3**：`WrappedPathBuilder` 里 `ttf_parser` 的 `curve_to` 被转译成 `tiny-skia` 的 `cubic_to` 而非 `quad_to`，为什么这里不会出错？

**答案**：`ttf_parser::OutlineBuilder` 的 `curve_to` 在语义上就是「三次贝塞尔（cubic）」，名字虽叫 curve，但参数是两个控制点 + 终点（共 6 个数），与 `tiny-skia::PathBuilder::cubic_to` 完全对应；`quad_to` 才是二次贝塞尔（一个控制点 + 终点）。二者按语义正确对接，不存在混淆。

---

### 4.2 rasterize：字形光栅化与记忆化键

#### 4.2.1 概念说明

当 `render_outline_glyph` 判定走快路径时，实际把字形变成像素的工作交给 `pixglyph`，封装在内部函数 `rasterize` 里。它有两个关键设计：

1. **子像素定位**：字形不是按整数像素对齐的，它的子像素偏移直接影响抗锯齿结果。`pixglyph` 把 `(x, y)` 子像素位置和 `size`（即 ppem）一起作为输入，算出一张带亚像素抗锯齿的覆盖率位图。
2. **记忆化**：同一个字形、同一个字号、同一个子像素偏移会反复出现（想想一页正文里有几十个相同的「e」）。用 `comemo::memoize` 缓存光栅化结果，可避免重复计算。返回 `Arc<Bitmap>` 实现零拷贝共享。

#### 4.2.2 核心流程

```text
bitmap = rasterize(font, id, ts.tx, ts.ty, ppem)?
         ↓ （记忆化：键 = font, id, tx_bits, ty_bits, ppem_bits）
         若命中缓存 → 返回 Arc<Bitmap>
         否则：
           glyph = pixglyph::Glyph::load(font.ttf(), id)?   # 解析字形轮廓
           glyph.rasterize(tx, ty, ppem)                    # 光栅化
           → Arc<Bitmap> { coverage, left, top, width, height }
```

`Bitmap` 的几个字段在 4.3 会反复用到：`coverage` 是覆盖率一维数组（按行优先，长度 `width*height`），`left`/`top` 是这张位图应贴到画布上的**整数像素**左上角坐标（可为负，表示字形笔触伸出画布左/上边界），`width`/`height` 是位图尺寸。

#### 4.2.3 源码精读

[src/text.rs:L104-L119](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L104-L119) —— `rasterize`：被 `#[comemo::memoize]` 包裹的字形光栅化。

```rust
#[comemo::memoize]
fn rasterize(
    font: &FontInstance,
    id: GlyphId,
    x: u32,
    y: u32,
    size: u32,
) -> Option<Arc<Bitmap>> {
    let glyph = pixglyph::Glyph::load(font.ttf(), id)?;
    Some(Arc::new(glyph.rasterize(
        f32::from_bits(x),
        f32::from_bits(y),
        f32::from_bits(size),
    )))
}
```

注意签名上的「障眼法」：`rasterize` 对外暴露的键参数 `x`/`y`/`size` 声明为 `u32`，函数体内立刻用 `f32::from_bits` 还原回 f32。看调用点就明白了：

[src/text.rs:L123-L124](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L123-L124) —— 调用 `rasterize` 时把 f32 转成 bit pattern 作为键。

```rust
let bitmap = rasterize(
    &text.font, id,
    ts.tx.to_bits(), ts.ty.to_bits(), ppem.to_bits(),
)?;
```

**为什么用 `f32::to_bits()` 而不直接用 `f32` 作为 `comemo` 的键？** 因为 `comemo::memoize` 要求键可 `Hash` + `Eq`，而 Rust 的 `f32` **既没有实现 `Hash` 也没有实现 `Eq`**（`NaN != NaN`、`+0.0` 与 `-0.0` 的位模式不同等问题，使得浮点数作为映射键语义不稳）。`f32::to_bits()` 把 f32 的 IEEE-754 位模式转成 `u32`，`u32` 天然 `Hash + Eq`，于是把「子像素偏移 + ppem」精确地编进缓存键。

这带来一个直接的命中规则：**两个相同字形，只要 `ppem` 相同、子像素偏移 `(ts.tx, ts.ty)` 的位模式相同，就命中同一份缓存**——与字形在文档中的逻辑位置（第几页第几行）无关，只看它落在画布上的**像素坐标对 1 的余数**（即子像素相位）和字号。这也意味着：同一字号下，子像素相位不同的同一个字会被缓存成不同条目（这是必要的，因为抗锯齿结果依赖子像素相位）。

`Arc<Bitmap>` 的引用计数共享意味着：缓存命中时多个调用方共享同一块覆盖率内存，零拷贝。

#### 4.2.4 代码实践

**实践目标**：验证「相同字形 + 相同 ppem 但不同子像素相位」是否能命中 `rasterize` 缓存。

**操作步骤（源码阅读型）**：

1. 阅读 [src/text.rs:L104-L124](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L104-L124)，确认缓存键为 `(font, id, ts.tx.to_bits(), ts.ty.to_bits(), ppem.to_bits())`。
2. 想象一个场景：一行 12pt 正文里连续出现两个字母 `e`，但第一个 `e` 的笔起点在画布 x=10.3pt（`pixel_per_pt=2` → 20.6px），第二个在 x=15.8pt（→ 31.6px）。

**需要观察的现象 / 推理**：

- 两个 `e` 的 `ppem` 相同（都是 `12 × 2 = 24`）。
- 但 `ts.tx` 不同：20.6 与 31.6，整数部分不同（20 vs 31），子像素小数部分 0.6 相同。`ts.tx.to_bits()` 因整数部分不同而不同 → **缓存未命中**，各自光栅化。
- 若改成 x=10.3px 与 x=14.3px（相差整数 4），则 `ts.tx` 的小数相位同为 0.3，但整数部分（10 vs 14）不同，`to_bits()` 仍不同 → 依然未命中。

**预期结果**：结论是 `rasterize` 的键**包含完整的 `ts.tx` 子像素坐标位模式**，而不仅仅是相位。因此严格来说，只有 `ts.tx`/`ts.ty` 完全相同的两个字形才命中。这在实践中命中率仍然很高，因为版面里大量字形的起始像素坐标会高度重复（尤其在等宽或对齐场景）。> 待本地验证：可在 `rasterize` 内 `eprintln!` 计数命中/未命中来实测。

#### 4.2.5 小练习与答案

**练习 1**：为什么缓存键里要同时包含 `ppem` 和 `ts.tx/ts.ty`，而不能只含 `ppem`？

**答案**：`ppem` 决定字形大小（覆盖率位图的尺度），但抗锯齿还依赖字形相对像素网格的**子像素相位**（`ts.tx`/`ts.ty` 的小数部分）。同一字号下，相位不同会让边缘像素的覆盖率不同，所以必须把位置一起编进键。

**练习 2**：`rasterize` 返回 `Option<Arc<Bitmap>>`。什么情况下返回 `None`？调用方拿到 `None` 会怎样？

**答案**：`pixglyph::Glyph::load` 在字体里找不到该字形轮廓（如字形 id 无效或缺轮廓表）时返回 `None`。调用点 [L123-L124](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L123-L124) 用 `?` 把 `None` 向上传播，`render_outline_glyph` 整体返回 `None`，即该字形不绘制（静默跳过）。

---

### 4.3 write_bitmap：覆盖率调制与遮罩分支

#### 4.3.1 概念说明

`rasterize` 给出了一张覆盖率位图，但它**只有 alpha（形状），没有颜色**——字形的颜色（纯色 / 渐变 / 平铺）由 `text.fill` 决定。`write_bitmap` 的职责就是：把「覆盖率（形状）」与「颜色（sampler 采样）」合成，再 src-over 叠到画布现有像素上。

这里的精髓是**把字形覆盖率当成颜色的额外 alpha**。对于一个覆盖率 `cov`（0–255）的像素：

1. 先从 `sampler` 取这一像素应填的颜色 `color`（已在预乘空间）。
2. 把 `color` 按 `cov/256` 缩放（即 `alpha_mul(color, cov)`），相当于「这个像素只被覆盖了 cov/256 这么多」。
3. 把缩放后的源颜色 src-over 到画布目标像素上。

`write_bitmap` 是泛型函数，参数 `sampler: S` 可以是：

- 一个 `PremultipliedColorU8`（纯色，其 `sample` 忽略位置直接返回自身），
- 一个 `GradientSampler`（按画布位置采样渐变），
- 一个 `TilingSampler`（按画布位置采样平铺图案）。

这三者由 [src/paint.rs:L11-L22](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L11-L22) 的 `PaintSampler` trait 统一：

```rust
pub trait PaintSampler: Copy {
    fn sample(self, pos: (u32, u32)) -> sk::PremultipliedColorU8;
}
```

#### 4.3.2 核心流程

`write_bitmap` 按 **`state.mask` 是否存在**分两条路：

```text
write_bitmap(canvas, bitmap, state, sampler):
  if state.mask.is_some():           # 【有遮罩分支】
      建临时画布 (mw+2) × (mh+2)         # 四周各留 1px padding
      for (x,y) in bitmap:
          cov  = bitmap.coverage[y*mw + x]
          sample_pos = clamp(left+x, top+y) 到画布范围   # 画布像素坐标
          color = sampler.sample(sample_pos)
          temp[(y+1, x+1)] = alpha_mul(color, cov)        # 写进临时画布
      canvas.draw_pixmap(left-1, top-1, temp, mask=state.mask)  # 交回 tiny-skia 做遮罩混合
  else:                              # 【无遮罩分支】
      for (x,y) in 画布上 bitmap 的像素包围盒:
          cov = bitmap.coverage[(y-top)*mw + (x-left)]
          若 cov==0: continue
          color = sampler.sample((x,y))
          若 cov==255 且 color 完全不透明: pixels[pi] = color  # 快路径
          否则:
              applied = alpha_mul(color, cov)
              pixels[pi] = blend_src_over(applied, pixels[pi])
```

#### 4.3.3 源码精读

先看**无遮罩分支**（更直接，便于建立直觉）：

[src/text.rs:L201-L236](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L201-L236) —— 无遮罩分支：在画布像素包围盒内逐像素混合。

```rust
let pixels = bytemuck::cast_slice_mut::<u8, u32>(canvas.data_mut());
for x in left.clamp(0, cw)..right.clamp(0, cw) {
    for y in top.clamp(0, ch)..bottom.clamp(0, ch) {
        let ai = ((y - top) * mw + (x - left)) as usize;
        let cov = bitmap.coverage[ai];
        if cov == 0 { continue; }

        let color = sampler.sample((x as u32, y as u32));
        let color = bytemuck::cast(color);
        let pi = (y * cw + x) as usize;
        // Fast path if color is opaque.
        if cov == u8::MAX && color & 0xFF == 0xFF {
            pixels[pi] = color;
            continue;
        }

        let applied = alpha_mul(color, cov as u32);
        pixels[pi] = blend_src_over(applied, pixels[pi]);
    }
}
```

要点：

- `bytemuck::cast_slice_mut::<u8, u32>` 把画布的字节缓冲重新解释成 `u32` 切片——每个 `u32` 就是一个预乘像素（`color & 0xFF == 0xFF` 这个判断说明 alpha 在低位字节……见下方说明）。
- `left.clamp(0, cw)..right.clamp(0, cw)` 把字形包围盒裁到画布范围内，处理字形笔触伸出画布边界的情况。
- **快路径**：`cov == 255 && color & 0xFF == 0xFF`。`cov == u8::MAX` 表示该像素被字形完全覆盖；`color & 0xFF == 0xFF` 检查的是最低字节（一个**颜色通道**，非 alpha）。为什么这能代表「完全不透明」？因为 `color` 是**预乘**颜色：预乘空间里每个颜色通道 ≤ alpha，所以只要有任一通道达到满值 0xFF，就迫使 alpha 也必须是 0xFF（否则该通道不可能达到 0xFF）。于是「某通道 == 0xFF」是预乘空间下「alpha 满值」的有效判据。字节序与 alpha 位置的细节见 4.4。两者同时成立时，src-over 退化为「直接写源颜色」（因为 `(1-1)*dst = 0`），省掉一次 `blend_src_over`。
- 慢路径：`alpha_mul(color, cov)` 把源颜色按覆盖率调制，再 `blend_src_over` 叠到目标（公式见 4.4）。

再看**有遮罩分支**，它是本讲实践的重点：

[src/text.rs:L156-L200](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L156-L200) —— 有遮罩分支：先渲染到带 1px padding 的临时画布，再 `draw_pixmap` 贴回。

```rust
if state.mask.is_some() {
    let cw = canvas.width() as i32;
    let ch = canvas.height() as i32;
    let mw = bitmap.width;
    let mh = bitmap.height;
    let left = bitmap.left;
    let top = bitmap.top;

    // Pad the pixmap with 1 pixel in each dimension so that we do
    // not get any problem with floating point errors along their border
    let mut pixmap = sk::Pixmap::new(mw + 2, mh + 2)?;
    let pixels = bytemuck::cast_slice_mut::<u8, u32>(pixmap.data_mut());
    for x in 0..mw {
        for y in 0..mh {
            let alpha = bitmap.coverage[(y * mw + x) as usize];
            // … (x,y) 在画布上的最终位置 = (left+x, top+y)
            let sample_pos = (
                (left + x as i32).clamp(0, cw) as u32,
                (top + y as i32).clamp(0, ch) as u32,
            );
            let color = sampler.sample(sample_pos);
            let color = bytemuck::cast(color);
            let applied = alpha_mul(color, alpha as u32);
            pixels[((y + 1) * (mw + 2) + (x + 1)) as usize] = applied;
        }
    }
    canvas.draw_pixmap(
        left - 1, top - 1, pixmap.as_ref(),
        &sk::PixmapPaint::default(),
        sk::Transform::identity(),
        state.mask,
    );
}
```

**为什么要绕一圈，先画到临时画布再 `draw_pixmap`？**

关键在于：无遮罩分支里，混合是手工逐像素做的，我们完全掌控；但有遮罩时，**最终的合成还必须再与裁剪遮罩取交集**——只有遮罩内（clip 区域内）的像素才该被改写。手工内层循环并不直接持有遮罩的逐像素数据去做「遮罩内才混合」。于是作者把已调好色的字形先画到一张与字形同尺寸的小画布上，然后调用 tiny-skia 的 `draw_pixmap`——它原生支持把一张 pixmap 以指定遮罩 blit 到目标，让 tiny-skia 替我们完成「遮罩相交 + src-over 合成」这一步。代价是多一次小画布分配和一次 `draw_pixmap`，但这是正确性所必需的。

**为什么是 `mw+2 × mh+2`（四周各 1px padding）？**

源码注释解释得很清楚：为了避免 `draw_pixmap` 在贴图边界处因浮点误差丢像素。具体坐标对应关系（见注释 [L173-L180](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L173-L180)）：

- 位图像素 `(x, y)` 被写进临时画布的 `(x+1, y+1)`（因为四周各留了 1px）。
- `draw_pixmap` 把临时画布的左上角放在画布的 `(left-1, top-1)`。
- 因此该像素在画布上的最终位置 = `(left-1 + x+1, top-1 + y+1) = (left+x, top+y)`，与无遮罩分支完全一致。

那 1px padding 的意义是：`draw_pixmap` 在光栅化「把临时画布贴到 (left-1, top-1)」这一步本身有抗锯齿/取整，边缘像素可能有 ±1 的浮点抖动。如果临时画布刚好等于字形尺寸（无 padding），抖动可能把字形最外圈的像素切掉或错位；多留 1px 缓冲，抖动落在透明的 padding 上，不会损伤字形像素。

**采样位置 `sample_pos` 为什么用画布坐标 `(left+x, top+y)` 而不是临时画布坐标？**

因为渐变/平铺的颜色取决于该像素**在文档画布中的位置**（一个线性渐变从画布左到右），与它落在临时画布的哪个本地坐标无关。对纯色 sampler，位置被忽略。所以无论哪个分支，`sample` 都喂画布像素坐标。注意此处 `.clamp(0, cw)/(0, ch)`：当字形笔触伸出画布时，把采样位置夹回画布内，避免越界（伸出的像素反正也会被画布裁掉，颜色取边界值即可）。

#### 4.3.4 代码实践

**实践目标**：分析有遮罩分支「临时画布 + padding + draw_pixmap」的必要性，并把坐标对应关系推导一遍。

**操作步骤（源码阅读型，配以纸笔推导）**：

1. 阅读 [src/text.rs:L156-L200](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L156-L200)。
2. 假设某字形 `bitmap = { width: 3, height: 2, left: 10, top: 20 }`。在纸上画出：
   - 临时画布尺寸 = `5 × 4`。
   - 位图像素 `(x=0,y=0)` 写入临时画布 `(1,1)`；`draw_pixmap` 贴在画布 `(9, 19)`；最终落在画布 `(10, 20)`。✓
   - 验证位图像素 `(x=2,y=1)`：写入临时画布 `(3,2)`；贴在 `(9,19)` → 画布 `(12, 20)`，而 `left+x=10+2=12, top+y=20+1=21`？
3. 发现第 2 步对 `(x=2,y=1)` 的 `top+y` 推导应为 `20+1=21`，但 `draw_pixmap` 起点 `top-1=19`，临时画布 y=2 → 画布 y = `19+2 = 21`。✓ 一致。

**需要观察的现象**：四个角落像素的坐标对应都满足 `画布位置 = (left-1 + (x+1), top-1 + (y+1)) = (left+x, top+y)`。

**预期结果**：坐标恒等式成立，证明 padding 不改变像素落点，只是给 `draw_pixmap` 的边界浮点抖动留缓冲。

> 进阶思考（待本地验证）：若把 `mw + 2` 改成 `mw`、`draw_pixmap` 起点改成 `left`，理论上坐标仍自洽，但字形边缘像素在带遮罩的 clip 场景下可能出现 1px 错位或缺失——这正是 padding 防御的对象。

#### 4.3.5 小练习与答案

**练习 1**：无遮罩分支的快路径条件是 `cov == u8::MAX && color & 0xFF == 0xFF`。如果一个完全不透明的纯色字（`color & 0xFF == 0xFF` 恒真）落在抗锯齿边缘（`cov` 介于 1–254），会走快路径还是慢路径？

**答案**：走**慢路径**。快路径要求 `cov == 255`，抗锯齿边缘的 `cov` 介于 1–254，不满足，于是走 `alpha_mul` + `blend_src_over`，把源颜色按覆盖率调制后混合到目标，正是抗锯齿的正确做法。

**练习 2**：有遮罩分支里，临时画布外圈（第 0 行/列和最后一行/列）的像素值是什么？它们参与最终合成吗？

**答案**：临时画布用 `sk::Pixmap::new` 创建，初始为全透明（alpha=0）；循环只写 `(y+1, x+1)` 起的核心区域，外圈 padding 保持全透明。`draw_pixmap` 把整张临时画布贴上去时，透明的 padding 不影响目标（src-over 下透明源不改变目标），所以 padding 是「无害的缓冲」。

**练习 3**：为什么无遮罩分支用 `bitmap.width as i32`（`mw`）作内层 `coverage` 索引的行宽，而有遮罩分支用裸 `bitmap.width`？

**答案**：两者用的都是位图自身的逻辑宽度 `mw = bitmap.width`（一个 i32，一个原 u32，仅类型不同），语义一致——`coverage` 是按位图宽度 `width` 行优先存储的。区别只在无遮罩分支里 `mw` 还要参与带符号的画布坐标运算（`left + mw` 等可能涉及负值 clamp），所以转成 `i32`；有遮罩分支的本地循环索引是非负的，用 u32 即可。

---

### 4.4 像素级混合：blend_src_over 与 alpha_mul

#### 4.4.1 概念说明

`write_bitmap` 慢路径调用的 `blend_src_over` 与 `alpha_mul`，是两个从 Skia 移植来的纯位运算函数，用整数算术高速完成预乘 alpha 合成。源码顶部注明了出处：

[src/text.rs:L266-L268](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L266-L268)

```rust
// Alpha multiplication and blending are ported from:
// https://skia.googlesource.com/skia/+/refs/heads/main/include/core/SkColorPriv.h
```

两个函数都工作在「打包成 32 位的预乘 RGBA 颜色」上。源码注释明确约束：**alpha 通道位于最高 8 位**（"Alpha channel must be in the 8 high bits"）。这正是 `blend_src_over` 用 `src >> 24` 提取 alpha 的前提。

#### 4.4.2 核心流程（公式 + 位运算对照）

预乘空间下的 src-over：

\[
C_{\text{out}} = C_{\text{src}} + (1 - \alpha_{\text{src}})\,C_{\text{dst}}
\]

代码用「定点 + 除以 256」近似（而非精确的 /255）：

[src/text.rs:L271-L273](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L271-L273)

```rust
fn blend_src_over(src: u32, dst: u32) -> u32 {
    src + alpha_mul(dst, 256 - (src >> 24))
}
```

逐项对应：

- `src >> 24` = `α_src`（取最高 8 位 = 源 alpha，范围 0–255）。
- `256 - (src >> 24)` = `(1 - α_src/256) × 256`，即「目标存活比例」的定点刻度。
- `alpha_mul(dst, 256 - α_src)` = `dst × (1 - α_src/256)`，对应公式里的 `(1-α_src)·C_dst`。
- `src + 上面` = `C_src + (1-α_src)·C_dst`，正是 src-over。✓

> 用 `256 - α` 而非 `255 - α`（即除以 256 而非 255）是 Skia 的标准快速近似：它把「除以 255」换成「右移 8 位（除以 256）」，引入微小偏差（约 1/256）但换来用位移代替除法，在逐像素热路径上非常划算。

`alpha_mul` 是「把一个预乘颜色的每个通道都乘以 `scale/256`」的快速实现：

[src/text.rs:L276-L281](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L276-L281)

```rust
fn alpha_mul(color: u32, scale: u32) -> u32 {
    let mask = 0xff00ff;
    let rb = ((color & mask) * scale) >> 8;
    let ag = ((color >> 8) & mask) * scale;
    (rb & mask) | (ag & !mask)
}
```

#### 4.4.3 源码精读：0xff00ff 掩码的位运算含义

`alpha_mul` 的目标是对 4 个通道各乘 `scale/256`。朴素的写法是拆成 4 个字节、各乘各的、再拼回——要 4 次乘法。这里用 **2 次乘法**完成，靠的是 `0x00ff00ff` 掩码的「双通道打包」技巧。

把 32 位颜色按字节编号（byte0 = 最低字节，byte3 = 最高字节，alpha 在 byte3）：

```
字节:   byte3   byte2   byte1   byte0
位:    31…24   23…16   15…8    7…0
掩码 0x00ff00ff:  0x00    0xff    0x00    0xff
```

`mask = 0x00ff00ff` 选中 **byte0 和 byte2**，把 byte1、byte3 清零。

**第一步 `rb = ((color & mask) * scale) >> 8`**：

- `color & mask` 只保留 byte0、byte2，中间（byte1）和 byte3 被清零。于是这个 32 位数里，byte2 的值「孤悬」在高 16 位、byte0 的值在低 16 位，两者之间隔着一个全零的 byte1。
- 把整个 32 位数当作一个整数乘 `scale`（≤256，9 位）。关键观察：byte0 的值 ≤ 255，乘 scale 后 ≤ `255×256 = 65280 < 65536`，结果落在低 16 位内，**不会溢出到 byte2 所在的高 16 位**；同理 byte2×scale 落在高 16 位。两个通道的乘积在各自的 16 位半字内独立完成、互不干扰。这就实现了「一次乘法同时算两个通道」。
- `>> 8` 把结果除以 256，让每通道回到 8 位量级。

**第二步 `ag = ((color >> 8) & mask) * scale`**：

- `color >> 8` 把原 byte1→byte0、byte3→byte2（byte0 丢弃，最高位补 0）。
- 再 `& mask` 选中（现在的）byte0、byte2 = 原来的 **byte1、byte3**。
- 乘 `scale`，同样的「双通道打包」技巧对 byte1、byte3 各乘 scale。注意这里**没有 `>> 8`**——因为乘法本身已把值放大到 16 位，下一步靠掩码取出正确的那一字节。

**第三步 `(rb & mask) | (ag & !mask)`**：

- `rb & mask` 取 rb 中的 byte0、byte2（已除以 256 的结果）。
- `ag & !mask`（`!mask = 0xff00ff00`）取 ag 中的 byte1、byte3。注意 ag 的乘积落在每半字的低 8 位，而我们要的是它的高 8 位（即 `channel×scale/256`）——`& !mask`（选中 byte1=bits8-15、byte3=bits24-31）恰好取的是每个 16 位半字的高字节，等价于隐含做了一次 `>> 8`。
- 二者按位或，byte0/byte2 来自 rb，byte1/byte3 来自 ag，拼回完整的 4 通道，每通道都等于 `原值 × scale / 256`。✓

为什么这个技巧成立？数学上，因为 `color & 0x00ff00ff` 让两个被选通道之间隔着全零字节，乘 `scale`（≤256）时每个通道的乘积都严格落在各自 16 位半字内、不向相邻半字进位，于是「整数乘」等价于「两个通道各自独立乘」。

> 字节序说明：`blend_src_over` 要求 alpha 在最高字节（byte3，源码注释「Alpha channel must be in the 8 high bits」即此意）；`0x00ff00ff` 选中的是 byte0/byte2 这两个「相间」字节。tiny-skia 把预乘像素存为「颜色通道在前、alpha 在最后一字节」，故在小端机器上 byte0 是颜色通道、byte3 是 alpha——这与 4.3 快路径用 `color & 0xFF`（byte0，一个颜色通道）配合预乘不变量判断「不透明」完全自洽。混合数学只依赖「alpha 在高位、掩码选相间字节」，不依赖 R/G/B 的具体排布。

#### 4.4.4 代码实践

**实践目标**：手算一次 `blend_src_over`，把公式与位运算对上。

**操作步骤（纸笔推导型）**：

设源像素为一个半透明红色，预乘后 `src = 0x80800000`（alpha=0x80=128，R 通道预乘后也是 0x80，G=B=0；此处仅为说明位运算，不纠结 R 落在哪一字节）。目标像素 `dst = 0xFFFFFFFF`（白色不透明，所有通道满值）。`scale = 256 - (src >> 24) = 256 - 128 = 128`。

1. 算 `alpha_mul(dst, 128)`：
   - `dst & 0x00ff00ff = 0x00FF00FF`。
   - `× 128 = 0x7F80007F80`（取低 32 位 = `0x80007F80`，因为每半字 `0xFF×128=0x7F80`）。> 实际只需关心每半字：`0xFF → 0x7F80`。
   - `>> 8` → 每半字 `0x7F80 >> 8 = 0x7F`，故 `rb = 0x007F007F`。
   - `(dst >> 8) & mask = 0x00FF00FF`，`×128` → 同样每半字 `0x7F80`，`ag = 0x7F807F80`（位级）。
   - `rb & mask = 0x007F007F`；`ag & !mask = 0x7F807F80 & 0xFF00FF00 = 0x7F007F00`。
   - 合成 `0x007F007F | 0x7F007F00 = 0x7F7F7F7F`（中灰，每通道 0x7F）。
   - 物理含义：白色被半透明源遮住后，剩下 `128/256 = 50%`，即每通道约 127。
2. `blend_src_over = src + 0x7F7F7F7F = 0x80800000 + 0x7F7F7F7F`。alpha 通道：`0x80 + 0x7F = 0xFF`（源 alpha 半 + 目标存活半 ≈ 满值）；其余通道相应叠加。

**需要观察的现象**：每一步的位运算都对应到「通道按 `scale/256` 缩放」的直观含义；最终 alpha 接近 0xFF（半透明红盖在不透明白上，结果接近不透明偏红灰）。

**预期结果**：手算结果与「src-over 公式 `C_src + (1-α_src)·C_dst`」在每通道上吻合（允许 /256 近似的 1 左右误差）。

> 若你不想纠结字节排布，可用一个更简单的 sanity check：当 `src` 完全不透明（alpha=0xFF），`scale = 256-255 = 1`，`alpha_mul(dst,1) ≈ dst/256 ≈ 0`，`blend_src_over ≈ src + 0 = src`——即「不透明源完全替换目标」，与直觉一致。✓

#### 4.4.5 小练习与答案

**练习 1**：`alpha_mul` 里 `rb` 这一行有 `>> 8`，`ag` 那一行却没有。为什么 `ag` 不需要显式 `>> 8`？

**答案**：`ag` 的乘法把每通道放大到 16 位（落在半字的低 8 位），而最终 `ag & !mask`（`!mask=0xFF00FF00`）选中的是每个半字的**高 8 位**（bits 8-15、bits 24-31），取高字节本身等价于一次 `>> 8`。所以 `>> 8` 被「掩码选高位字节」隐式完成了。

**练习 2**：如果 `scale` 可能大于 256，`alpha_mul` 还正确吗？为什么实际调用中 `scale` 被限定在 0–256？

**答案**：`scale > 256` 时，`channel × scale` 可能超过 16 位（`255×257 > 65535`），会向相邻半字进位，破坏「双通道独立」的前提，结果出错。实际调用里 `scale` 取自覆盖率 `cov`（0–255）或 `256 - alpha`（1–256），最大正好 256，`255×256 = 65280 < 65536`，恰好不溢出，技巧才成立。

**练习 3**：`blend_src_over` 用 `256 - (src >> 24)` 而不是 `255 - (src >> 24)`。当 `src` 完全不透明（`alpha = 0xFF = 255`）时，`alpha_mul(dst, 1)` 会把 `dst` 完全清零吗？

**答案**：几乎清零但不精确为 0。`alpha_mul(dst, 1) = dst × 1/256`，对每通道而言 `255/256 = 0`（整除截断），但对通道值的影响是「右移 8 近似除以 256」，结果约为 `dst/256`，远小于 1 即截断为 0。故 `blend_src_over ≈ src + 0 = src`，不透明源近似完全替换目标——这正是无遮罩分支快路径 `cov==255 && 不透明` 直接写 `color` 的理论依据（快路径不过是把这个「必然等于 src」的结果提前短路了）。

---

## 5. 综合实践

把本讲四块知识串起来，完成下面这个**端到端追踪任务**：

**场景**：一个 Typst 文档里有一行 48pt 的渐变填充标题，位于一个带圆角裁剪的容器内，以 `--ppi 144`（即 `pixel_per_pt = 2.0`）导出 PNG。

**任务**：

1. **判定路径**：该标题是 48pt、`pixel_per_pt=2`，无斜切/非均匀缩放/描边。计算 `ppem`，判断走快路径还是慢路径（提示：48 × 2 = 96，与阈值 100 比较）。
2. **缓存键**：若走快路径，写出 `rasterize` 这次调用的键参数（`font`、`id`、`tx.to_bits()`、`ty.to_bits()`、`ppem.to_bits()`）。思考：标题里若有两个相同字符，第二个会命中缓存吗？取决于什么？
3. **sampler 选择**：因为填充是渐变，`render_outline_glyph` 会构造哪个 sampler（[src/text.rs:L126-L129](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/text.rs#L126-L129)）？它如何在 `write_bitmap` 里逐像素取色？
4. **遮罩分支**：因为位于带裁剪的容器内，`state.mask.is_some()` 为真。说明 `write_bitmap` 走哪条分支、为什么要建 `mw+2 × mh+2` 临时画布、`draw_pixmap` 的起点为何是 `(left-1, top-1)`。
5. **像素混合**：在临时画布里，某边缘像素覆盖率 `cov = 200`，渐变采样得到一个预乘颜色 `color`。写出该像素写入临时画布的表达式（`alpha_mul(color, 200)`），并解释最终由 `draw_pixmap` 完成的合成为何等价于 src-over。

**参考要点**：

1. `ppem = 96 < 100`，无其他慢路径条件 → **快路径**。
2. 键 = `(font, id, ts.tx.to_bits(), ts.ty.to_bits(), 96.0f32.to_bits())`。第二个相同字符能否命中，取决于它的 `ts.tx`/`ts.ty` 是否与第一个**完全相等**（相同子像素相位且相同整数像素位置）——通常不相等（位置不同），故一般不命中；但若版面恰好让两字起始于同一像素坐标，则命中。
3. 选择 `GradientSampler`（`on_text=true`，文本默认 `RelativeTo::Parent`），在 `write_bitmap` 里按画布坐标 `(x,y)` 调 `sample`，内部把画布点经 `transform_to_parent`（`container_transform.invert()`）映射回答器空间再采样渐变（见 [src/paint.rs:L61-L79](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/paint.rs#L61-L79)）。
4. 走**有遮罩分支**：建临时画布以隔离已调色的字形像素，再让 tiny-skia 的 `draw_pixmap` 用 `state.mask` 完成裁剪相交；padding 防 `draw_pixmap` 边界浮点抖动损伤字形；`(left-1, top-1)` 配合 `(x+1, y+1)` 使最终落点为 `(left+x, top+y)`。
5. 临时画布像素 = `alpha_mul(color, 200)`（颜色按覆盖率调制）。`draw_pixmap` 对每个像素做 `blend_src_over(临时像素, 画布像素)`，其中临时像素的 alpha 已含覆盖率，故合成结果 = 渐变色按字形覆盖率叠加到画布，正是带裁剪的抗锯齿文本渲染。

## 6. 本讲小结

- `render_outline_glyph` 以 `ppem = text.size.to_f32() * ts.sy` 为核心量，用五个条件（`ppem>100`、`ppem<0`、`kx≠0`、`ky≠0`、`sx≠sy`、有描边）在「矢量路径慢路径」与「pixglyph 快路径」之间分流。
- 慢路径用 `WrappedPathBuilder` 把 `ttf_parser` 的字形轮廓直译为 `sk::Path`，Y 翻转后由 tiny-skia 的 `fill_path`/`stroke_path` 绘制；快路径用 `pixglyph` 直接生成覆盖率位图。
- `rasterize` 被 `#[comemo::memoize]` 缓存，键用 `f32::to_bits()` 把子像素偏移与 ppem 编为 `u32`（绕开 f32 不可 `Hash/Eq` 的限制），返回 `Arc<Bitmap>` 零拷贝共享。
- `write_bitmap` 按 `state.mask` 是否存在分两路：无遮罩时逐像素手工 `alpha_mul`+`blend_src_over`（含不透明快路径短路）；有遮罩时先渲染到 `mw+2 × mh+2` 的临时画布（1px padding 防 `draw_pixmap` 边界抖动），再交 tiny-skia 按遮罩 blit。
- `blend_src_over`/`alpha_mul` 移植自 Skia，用 `0x00ff00ff` 掩码实现「两次乘法完成四通道缩放」，以「除以 256」近似 src-over 公式 `C_out = C_src + (1-α_src)·C_dst`，在预乘空间高速合成。

## 7. 下一步学习建议

- 本讲把 `render_outline_glyph` 的快路径缓存（`rasterize`）讲透了，但 typst-render 还有两处 `comemo::memoize`：图像纹理构建 `build_texture`（u2-l5）与渐变采样 `cached`（u3-l1）。下一讲 [u3-l4 记忆化与性能优化](u3-l4-memoization-perf.md) 会把三处缓存放在一起横向对比缓存键设计与命中条件，建议接着读。
- 若你对渐变/平铺在文本快路径里如何逐像素采样还想深入，可回看 [u3-l1 渐变填充](u3-l1-gradients.md) 与 [u3-l2 平铺图案 Tiling](u3-l2-tilings.md) 中 `GradientSampler`/`TilingSampler` 的 `sample` 实现，它们正是本讲 `write_bitmap` 的 `sampler` 参数背后的颜色来源。
- 想验证本讲的行为，最直接的方式是给 `render_outline_glyph` 两条路径与 `write_bitmap` 两个分支各加 `eprintln!` 日志，渲染一份含大标题、小正文、带裁剪容器、彩色 emoji 的文档，观察分支命中的实际分布。
