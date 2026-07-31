# 纯色与色彩空间转换

## 1. 本讲目标

上一讲（u3-l10）我们讲清了 `handle_shape` 如何把一个图形的几何体翻译成 krilla 路径，并提到「填充与描边颜色的具体翻译留待 `paint.rs`」。本讲就补上这一块。

读完本讲，你应该能够：

1. 说清 `convert_fill` / `convert_stroke` 如何把 Typst 的填充、描边「绘制状态」翻译成 krilla 的 `Fill` / `Stroke`。
2. 理解一个核心设计：**PDF 把「颜色」和「不透明度」分离**——Typst 颜色里携带的 alpha（透明度）会被单独拆出来，变成图形状态里的 opacity，而不是塞进颜色本身。
3. 掌握工程色（process color）按色彩空间的三路分派：`D65Gray → luma`、`Cmyk → cmyk`、其余一律 `→ RGB`。
4. 理解专色（spot / separation color）如何用一个「色料名 + 备选颜色」的 `SeparationSpace` 来表达，以及 tint（淡印）如何参与其中。

本讲**只讲纯色（`Paint::Solid`）**；渐变（`Gradient`）和平铺图案（`Tiling`）的分派入口我们会点到为止，深入实现留到下一讲 u3-l12。

## 2. 前置知识

在进入源码前，先建立两个直觉。

### 2.1 Typst 的颜色长什么样

Typst 的颜色有两类（见 [crates/typst-library/src/visualize/color.rs:282-287](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L282-L287)）：

```rust
pub enum Color {
    Process(ProcessColor),  // 工程色：可混色的常规颜色
    Spot(SpotColor),        // 专色：按油墨名引用的特定颜料
}
```

- **工程色 `ProcessColor`** 是「可混色、有明确色彩模型」的常规颜色，按色彩空间细分（[crates/typst-library/src/visualize/color.rs:1432-1451](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L1432-L1451)）：`Luma`（灰度）、`Rgb`、`LinearRgb`、`Cmyk`、`Oklab`、`Oklch`、`Hsl`、`Hsv`。日常的 `rgb(...)`、`luma(...)`、`cmyk(...)`、`oklab(...)` 都属于这一类。
- **专色 `SpotColor`** 用于专业印刷：它不靠混色还原，而是直接点名要某种油墨（如 `PANTONE 2221 C`），印刷时由人工把名字匹配到对应的印版（[crates/typst-library/src/visualize/color.rs:2246-2259](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L2246-L2259)）。

每个 `ProcessColor` 都能通过 `space()` 反查它所属的色彩空间（[crates/typst-library/src/visualize/color.rs:1534-1546](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L1534-L1546)），这正是本讲分派逻辑的依据。

### 2.2 一个颜色如何变成 4 个字节

`ProcessColor` 提供两个关键方法：

- `to_space(space)`：把颜色**无损转换到指定色彩空间**（[crates/typst-library/src/visualize/color.rs:1758-1769](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L1758-L1769)）。例如把一个 Oklab 颜色转成 sRGB。
- `to_vec4_u8()`：把颜色打包成 4 个 `u8` 字节（[crates/typst-library/src/visualize/color.rs:1753-1756](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L1753-L1756)），底层是 `to_vec4()`（[crates/typst-library/src/visualize/color.rs:1731-1751](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L1731-L1751)）。

> 关键观察：`to_vec4()` 对不同空间的返回值含义不同！
>
> - `Rgb`：`[red, green, blue, alpha]` —— 第 4 个是**透明度 alpha**。
> - `Luma`：`[luma, luma, luma, alpha]` —— 第 4 个仍是 **alpha**。
> - `Cmyk`：`[c, m, y, k]` —— 第 4 个是 **K（黑）墨版**，**没有 alpha**！

这个差异是本讲最关键的一点：CMYK 颜色天然不携带透明度，而 RGB/灰度颜色的 alpha 在第 4 个字节。typst-pdf 正是据此把「颜色分量」和「alpha」拆开处理。

### 2.3 PDF 的透明度模型（直觉）

在 CSS / Typst 里，透明度常常内嵌在颜色里（如 `rgb("#ff000080")` 是半透明红）。但 **PDF 不是这样**：PDF 用单独的「图形状态项」（ExtGState 里的 `ca` / `CA`）来表达填充 / 描边的不透明度，与颜色本身分离。所以把 Typst 颜色导出到 PDF 时，必须把 alpha 从颜色里抽出来，单独放进 `Fill::opacity` / `Stroke::opacity`。这就是本讲反复出现的「**不透明度分离**」。

---

## 3. 本讲源码地图

本讲几乎全部集中在**一个文件**里：

| 文件 | 作用 |
|------|------|
| [crates/typst-pdf/src/paint.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs) | 把 Typst 的 `Paint` / `Color` / `FixedStroke` 翻译成 krilla 的 `Fill` / `Stroke` / 颜色类型。本讲主讲其中的纯色部分。 |

需要交叉参考的「调用方」与「Typst 侧定义」：

| 文件 | 在本讲中的作用 |
|------|----------------|
| [crates/typst-pdf/src/shape.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/shape.rs) | 图形翻译器 `handle_shape`，是 `convert_fill` / `convert_stroke` 的调用方之一。 |
| [crates/typst-pdf/src/text.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs) | 文字翻译器 `handle_text`，是另一个调用方。 |
| [crates/typst-pdf/src/util.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs) | `SpotColorantToNameExt`，把专色名翻译成 krilla 色料标识。 |
| [crates/typst-library/src/visualize/color.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs) | Typst 侧 `Color` / `ProcessColor` / `SpotColor` 的定义与 `to_vec4` / `to_space`。 |

---

## 4. 核心概念与源码讲解

### 4.1 入口：convert_fill 与 convert_stroke（颜色与不透明度的分离）

#### 4.1.1 概念说明

`convert_fill` 和 `convert_stroke` 是 `paint.rs` 对外（crate 内）的两个入口函数，分别把 Typst 的「填充」和「描边」翻译成 krilla 的 `Fill` 和 `Stroke`。它们的调用方有两个：图形翻译器 `handle_shape`（[crates/typst-pdf/src/shape.rs:33-64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/shape.rs#L33-L64)）与文字翻译器 `handle_text`（[crates/typst-pdf/src/text.rs:28-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs#L28-L41)）。

这两个函数共同体现了一个核心设计——**不透明度分离**：它们都先调用 `convert_paint` 拿到一个元组 `(krilla paint, alpha_u8)`，其中颜色与透明度是**分开的两项**；透明度随后被归一化成 `[0, 1]` 的 `NormalizedF32`，放进 `Fill::opacity` / `Stroke::opacity`。

#### 4.1.2 核心流程

`convert_fill` 的执行链：

1. 接收 Typst 的 `Paint`（填充色）、`FillRule`（填充规则，如非零缠绕）、是否用于文字 `on_text`、当前图形状态 `state`、可选的 `Shape`。
2. 调用 `convert_paint(..., false)` 得到 `(krilla 的 paint, alpha)`。
3. 组装并返回 krilla 的 `Fill { paint, rule, opacity }`。

`convert_stroke` 类似，但描边还携带线宽、连接、端点、虚线等几何属性，需要一并翻译：

1. 调用 `convert_paint(..., true)` 得到 `(paint, alpha)`。注意第二个参数 `true` 表示「计算包围盒时要把描边宽度算进去」（`include_stroke_in_bbox`），这对纯色无影响，只在渐变 / 图案时有意义。
2. 把 Typst `FixedStroke` 的各项（厚度、斜接极限、连接、端点、虚线）翻译成 krilla 对应字段，其中几何属性的转换复用 `util.rs` 的 `to_krilla()` 扩展 trait（见 u2-l8）。

不透明度的归一化公式：

\[
\text{opacity} = \frac{\text{alpha}_{u8}}{255.0} \in [0, 1]
\]

由于 alpha 取值在 `0..=255`，`alpha/255.0` 必落在 `[0, 1]`，因此 `NormalizedF32::new(...).unwrap()` 永远安全。

#### 4.1.3 源码精读

`convert_fill`：[crates/typst-pdf/src/paint.rs:28-45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L28-L45)

```rust
pub(crate) fn convert_fill(
    gc: &mut GlobalContext, paint_: &Paint, fill_rule_: FillRule,
    on_text: bool, surface: &mut Surface, state: &State,
    shape: Option<&Shape>,
) -> SourceResult<Fill> {
    let (paint, opacity) =
        convert_paint(gc, paint_, on_text, surface, state, shape, false)?;

    Ok(Fill {
        paint,
        rule: fill_rule_.to_krilla(),
        opacity: NormalizedF32::new(opacity as f32 / 255.0).unwrap(),
    })
}
```

说明：第三行的解构 `(paint, opacity)` 就是「颜色与不透明度分离」的直接体现；`opacity as f32 / 255.0` 把 0–255 的 alpha 归一化到 0–1。

`convert_stroke`：[crates/typst-pdf/src/paint.rs:47-67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L47-L67)

```rust
pub(crate) fn convert_stroke(...) -> SourceResult<Stroke> {
    let (paint, opacity) =
        convert_paint(fc, &stroke.paint, on_text, surface, state, shape, true)?;

    Ok(Stroke {
        paint,
        width: stroke.thickness.to_f32(),
        miter_limit: stroke.miter_limit.get() as f32,
        line_join: stroke.join.to_krilla(),
        line_cap: stroke.cap.to_krilla(),
        opacity: NormalizedF32::new(opacity as f32 / 255.0).unwrap(),
        dash: stroke.dash.as_ref().map(convert_dash),
    })
}
```

说明：描边的几何属性（`width`/`line_join`/`line_cap` 等）逐一翻译自 Typst `FixedStroke`；`opacity` 同样单独算出。调用方 `handle_shape` 还会在更上层先过滤掉「厚度 ≤ 0」的描边（见 u3-l10），所以走到这里时 `width` 都为正。

#### 4.1.4 代码实践

**实践目标**：亲手验证「alpha 被单独拆出来」。

**操作步骤**（源码阅读 + 本地验证）：

1. 设想用户写 `#rect(fill: rgb("#ff000080"))`（半透明红色，alpha = 0x80 = 128）。
2. 追踪它在 `handle_shape` 中进入 `convert_fill`，`paint_` 为 `Paint::Solid(rgb(255,0,0,alpha=128))`。
3. `convert_paint` → `convert_solid` 返回 `(krilla rgb::Color(255,0,0), alpha=128)`。
4. 回到 `convert_fill`：`opacity = 128/255.0 ≈ 0.502`，颜色分量里**不含** alpha。
5. 本地验证：编译一个含 `rgb("#ff000080")` 的 Typst 文档为 PDF，用编辑器或 `qpdf --qdf` 解开，在 `rg`/`ca` 图形状态里应能看到约 0.502 的不透明度，而填充色对象里只有 `1 0 0`（红）三个分量。

**预期结果**：颜色对象与透明度分别落在 PDF 的不同结构里（颜色在 `rg`/`scn`，透明度在 ExtGState 的 `ca`）。

> 待本地验证：具体 PDF 操作符取决于 krilla 的输出格式，可用 `qpdf` 或 `mutool` 解包确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `convert_fill` 和 `convert_stroke` 都要把 `opacity` 单独算成一个字段，而不是把透明度揉进颜色对象？

> **答案**：因为 PDF 的透明度由独立的图形状态项（ExtGState 的 `ca` 填充 / `CA` 描边）控制，与颜色本身分离。把 alpha 留在颜色里无法表达，必须拆出来作为 `opacity`。

**练习 2**：`convert_stroke` 调 `convert_paint` 时传 `include_stroke_in_bbox = true`，而 `convert_fill` 传 `false`。这个差异对**纯色**有影响吗？

> **答案**：没有。`include_stroke_in_bbox` 只影响 `shape.bbox(...)` 算出的包围盒大小，而包围盒只被渐变 / 图案用来计算几何。纯色到处都一样涂，不需要包围盒，所以对纯色结果完全无影响。

---

### 4.2 颜色分派：convert_paint 与 convert_solid

#### 4.2.1 概念说明

`convert_paint` 是 `Paint` 的总分派器：Typst 的 `Paint` 有三个变体（纯色、渐变、平铺图案），它根据变体路由到不同处理函数。本模块聚焦纯色路径，它会经过 `convert_solid`，后者再根据 `Color` 是工程色还是专色二次分派。

#### 4.2.2 核心流程

`convert_paint` 对纯色（`Paint::Solid`）的执行链很直接：

1. 若调用方提供了 `shape`，先算出它的包围盒 `(offset, size)`（供渐变 / 图案用）；若没有（如文字场景），用零。
2. `match paint`：
   - `Paint::Solid(c)` → `convert_solid(c)` 返回 `(krilla color::Color, alpha)`，再用 `.into()` 把 `color::Color` 包成 krilla 的纯色 `paint::Paint`。
   - `Paint::Gradient(g)` → `convert_gradient(...)`（下一讲 u3-l12）。
   - `Paint::Tiling(p)` → `convert_pattern(...)`（下一讲 u3-l12）。

`convert_solid` 的二次分派：

- `Color::Process(color)` → `convert_process_solid(*color)`（见 4.3）。
- `Color::Spot(color)` → `convert_spot(color)`（见 4.4），**alpha 直接为 255**。

整体分派树（伪代码）：

```
convert_paint(paint, ...) -> (krilla Paint, alpha_u8)
├── Paint::Solid(c)     -> convert_solid(c) -> (color::Color, alpha) -> c.into()
├── Paint::Gradient(g)  -> convert_gradient(...)            // u3-l12
└── Paint::Tiling(p)    -> convert_pattern(...)             // u3-l12

convert_solid(color) -> (color::Color, alpha_u8)
├── Color::Process(pc) -> convert_process_solid(pc) -> (RegularColor, alpha)
└── Color::Spot(sc)    -> convert_spot(sc) -> separation::Color,  alpha = 255
```

#### 4.2.3 源码精读

`convert_paint` 的纯色分支：[crates/typst-pdf/src/paint.rs:104-112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L104-L112)

```rust
match paint {
    Paint::Solid(c) => {
        let (c, a) = convert_solid(c);
        Ok((c.into(), a))
    }
    Paint::Gradient(g) => Ok(convert_gradient(g, on_text, state, size, offset)),
    Paint::Tiling(p) => convert_pattern(gc, p, on_text, surface, state),
}
```

说明：函数开头（[paint.rs:78-102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L78-L102)）算出的 `offset` / `size` 对纯色分支根本用不上，纯是给渐变准备的。

`convert_solid`：[crates/typst-pdf/src/paint.rs:114-122](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L114-L122)

```rust
fn convert_solid(color: &Color) -> (color::Color, u8) {
    match color {
        Color::Process(color) => {
            let (color, alpha) = convert_process_solid(*color);
            (color.into(), alpha)
        }
        Color::Spot(color) => (convert_spot(color).into(), 255),
    }
}
```

说明：专色分支写死 `255`——专色总是以「不透明」导出，其浓淡由 tint 表达，不再叠 alpha（详见 4.4）。

#### 4.2.4 代码实践

**实践目标**：通过阅读源码，确认「纯色路径不依赖包围盒与文字标记」。

**操作步骤**：

1. 打开 [paint.rs:69-112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L69-L112)。
2. 找到 `if let Some(s) = shape { ... }` 这段（[paint.rs:78-95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L78-L95)）：它仅在 `shape` 存在时算包围盒，且只为负尺寸矩形做镜像修正（与 u3-l10 的负尺寸矩形处理呼应）。
3. 对照 `handle_text` 的调用（[text.rs:28-36](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs#L28-L36)）：文字传 `shape = None`，因此走 `else` 分支 `(Point::zero(), Size::zero())`，纯色文字的颜色转换完全不受包围盒影响。

**需要观察的现象**：纯色分支（`Paint::Solid`）下，`on_text`、`size`、`offset`、`shape` 这些参数都不参与计算——它们只服务渐变 / 图案。

#### 4.2.5 小练习与答案

**练习 1**：`convert_paint` 返回元组 `(krilla::paint::Paint, u8)` 的第二个 `u8` 代表什么？它的取值范围是多少？

> **答案**：代表透明度 alpha，取值 `0..=255`（0 全透明、255 全不透明）。它会被上层归一化为 `alpha/255.0`。

**练习 2**：`Color::Spot` 分支为什么把 alpha 直接写成 `255`，而不是像工程色那样从颜色里读？

> **答案**：专色用 tint（淡印比例）表达浓淡，PDF Separation 色彩空间不支持单独的 alpha 通道；typst-pdf 统一把专色按不透明（255）导出，浓淡交给 tint 处理（见 4.4）。

---

### 4.3 工程色转换：convert_process_solid 与 RGB / CMYK / Luma

#### 4.3.1 概念说明

工程色（`ProcessColor`）是本讲的主角。PDF 原生只支持有限的几种色彩空间，无法直接表达 Typst 的全部 8 种工程色空间（如 Oklab、HSL、HSV）。因此 typst-pdf 采取「**按目标空间归并**」的策略：先看颜色的 `space()`，按色彩空间分三路——

- `D65Gray` → 转成 krilla 灰度 `luma::Color`；
- `Cmyk` → 转成 krilla `cmyk::Color`；
- 其余（Oklab / Oklch / Srgb / LinearRgb / Hsl / Hsv）→ **统一转成 sRGB**，用 krilla `rgb::Color`。

转换都通过 `to_space(...)` 先搬到目标空间，再用 `to_vec4_u8()` 取 4 字节分量。

#### 4.3.2 核心流程

`convert_process_solid(color)` 的分派（[paint.rs:124-137](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L124-L137)）：

| 色彩空间 `space()` | 走哪条分支 | 调用 | 返回的 krilla 类型 | alpha 来源 |
|---|---|---|---|---|
| `D65Gray` | 第 1 条 | `convert_luma` | `luma::Color` | 第 4 字节 |
| `Cmyk` | 第 2 条 | `convert_cmyk` | `cmyk::Color` | **写死 255** |
| 其它（含 Oklab、Srgb 等） | 第 3 条 `_` | `convert_rgb` | `rgb::Color` | 第 4 字节 |

三个底层转换函数都遵循「`to_space(目标) → to_vec4_u8() → 取分量`」：

- `convert_cmyk`：[paint.rs:139-143](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L139-L143)。`to_space(Cmyk).to_vec4_u8()` 得 `[c,m,y,k]`，直接喂给 `cmyk::Color::new(c,m,y,k)`。**只返回颜色、不返回 alpha**。
- `convert_rgb`：[paint.rs:145-148](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L145-L148)。`to_space(Srgb).to_vec4_u8()` 得 `[r,g,b,a]`，返回 `(rgb::Color(r,g,b), a)`——alpha 从第 4 字节拆出。
- `convert_luma`：[paint.rs:150-153](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L150-L153)。`to_space(D65Gray).to_vec4_u8()` 得 `[l,l,l,a]`，返回 `(luma::Color(l), a)`——只取第 0 字节做灰度，alpha 取第 3 字节。

> **关键差异**：为什么 CMYK 的 alpha 写死 255？回看 [color.rs:1740](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L1740)：CMYK 的 `to_vec4()` 返回 `[c, m, y, k]`，第 4 个分量是 **K（黑墨版）而非 alpha**。也就是说 CMYK 颜色**天然不携带透明度**，所以导出时恒为不透明（255）。这是当前的一个行为特性：若用户对 CMYK 颜色设了透明度，导出时会丢失。

#### 4.3.3 源码精读

`convert_process_solid`：[crates/typst-pdf/src/paint.rs:124-137](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L124-L137)

```rust
fn convert_process_solid(color: ProcessColor) -> (color::RegularColor, u8) {
    match color.space() {
        ProcessColorSpace::D65Gray => {
            let (c, a) = convert_luma(color);
            (c.into(), a)
        }
        ProcessColorSpace::Cmyk => (convert_cmyk(color).into(), 255),
        // Convert all other colors in different color spaces into RGB.
        _ => {
            let (c, a) = convert_rgb(color);
            (c.into(), a)
        }
    }
}
```

说明：返回类型是 `(color::RegularColor, u8)`——`RegularColor` 是 krilla 里「非专色的常规颜色」类型，RGB/CMYK/Luma 都能 `.into()` 成它。注意 CMYK 分支没有解构 alpha，而是字面量 `255`。

三个底层函数（[paint.rs:139-153](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L139-L153)）：

```rust
fn convert_cmyk(color: ProcessColor) -> cmyk::Color {
    let components = color.to_space(ProcessColorSpace::Cmyk).to_vec4_u8();
    cmyk::Color::new(components[0], components[1], components[2], components[3])
}

fn convert_rgb(color: ProcessColor) -> (rgb::Color, u8) {
    let components = color.to_space(ProcessColorSpace::Srgb).to_vec4_u8();
    (rgb::Color::new(components[0], components[1], components[2]), components[3])
}

fn convert_luma(color: ProcessColor) -> (luma::Color, u8) {
    let components = color.to_space(ProcessColorSpace::D65Gray).to_vec4_u8();
    (luma::Color::new(components[0]), components[3])
}
```

说明：三者结构高度一致——先 `to_space` 归并到目标空间，再 `to_vec4_u8` 取分量。差别只在「取哪几个字节」「alpha 是否存在」。对照 [color.rs:1731-1751](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L1731-L1751) 的 `to_vec4()` 即可理解每个 `components[i]` 的物理含义。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：分别追踪一个 Oklab 颜色和一个 CMYK 颜色，确认它们走哪条分支、变成哪种 krilla 类型。

**操作步骤**：

1. **Oklab 颜色**（如 `oklab(0.7, 0.1, -0.05)`）：
   - 进入 `convert_process_solid`，`color.space()` 返回 `ProcessColorSpace::Oklab`（见 [color.rs:1538](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L1538)）。
   - `Oklab` 不匹配 `D65Gray` 也不匹配 `Cmyk`，落入 `_` 分支 → 调 `convert_rgb`。
   - `convert_rgb` 内：`to_space(Srgb)` 把 Oklab 转成 sRGB，`to_vec4_u8()` 得 `[r, g, b, a]`，返回 `(rgb::Color(r,g,b), a)`。
   - **最终 krilla 类型：`rgb::Color`**（再依次 `.into()` 成 `RegularColor` → `color::Color` → 纯色 `paint::Paint`）。

2. **CMYK 颜色**（如 `cmyk(20%, 0%, 80%, 0%)`）：
   - `color.space()` 返回 `ProcessColorSpace::Cmyk`（见 [color.rs:1542](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L1542)）。
   - 命中第 2 条分支 → 调 `convert_cmyk`。
   - `to_space(Cmyk).to_vec4_u8()` 得 `[c, m, y, k]`，返回 `cmyk::Color(c,m,y,k)`，alpha 写死 `255`。
   - **最终 krilla 类型：`cmyk::Color`**，且不透明。

**需要观察的现象 / 预期结果**：

- Oklab 颜色最终被「降级」成 sRGB 三字节 + 一个 alpha；其感知均匀的中间色空间信息在导出时丢失（这是 PDF 原生不支持 Oklab 的必然结果）。
- CMYK 颜色保留四分量并保持不透明。

> 待本地验证：可用 `cargo expand` 或在 `convert_process_solid` 入口加一行 `eprintln!("{:?}", color.space())`，分别喂入 Oklab / CMYK 文档，观察打印的分支。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Oklab / HSL / HSV 这些颜色都走 `convert_rgb`，而不是各自单独处理？

> **答案**：PDF 原生色彩空间只支持 DeviceRGB / DeviceCMYK / DeviceGray 等少数几种，无法直接表达 Oklab / HSL / HSV。typst-pdf 选择把它们统一 `to_space(Srgb)` 归并成 sRGB，再用 `rgb::Color` 输出，是最简单且兼容性最好的做法。

**练习 2**：`convert_cmyk` 的返回类型只是 `cmyk::Color`（没有 alpha），而 `convert_rgb` / `convert_luma` 都返回带 alpha 的元组。请结合 `to_vec4()` 解释原因。

> **答案**：因为 CMYK 的 `to_vec4()` 返回 `[c,m,y,k]`，4 个字节全是墨版分量、没有 alpha；而 RGB / Luma 的 `to_vec4()` 第 4 个字节是 alpha。所以 CMYK 无 alpha 可拆，在 `convert_process_solid` 中 alpha 被写死 255（恒不透明）。

**练习 3**：一个 `luma(50%)` 灰色且 alpha = 50% 的颜色，导出后颜色分量和不透明度分别是什么？

> **答案**：`convert_luma` 取 `to_space(D65Gray).to_vec4_u8()` 的第 0 字节作灰度（`luma::Color(≈128)`），第 3 字节作 alpha（≈128）。最终 `Fill` 的 `opacity = 128/255 ≈ 0.502`，颜色为单字节灰度。

---

### 4.4 专色转换：convert_spot 与 SeparationSpace

#### 4.4.1 概念说明

专色（spot color）是专业印刷里「按油墨名引用特定颜料」的颜色。它的价值在于：印刷厂拿到一个 `PANTONE 2221 C` 的名字，会直接用对应的标准油墨上版，颜色还原远比 CMYK 混色精确。但屏幕预览或家用打印机没有这种油墨，需要一个**备选颜色（fallback）**来近似显示。

PDF 用 **Separation 色彩空间**来表达专色：它由两部分组成——

- 一个**色料名**（colorant name，如 `PANTONE 2221 C`），告诉印刷设备用哪种油墨；
- 一个**备选颜色**（alternate color），告诉不支持该油墨的设备（屏幕等）用什么常规颜色近似。

Typst 侧的 `SpotColor` 结构（[color.rs:2246-2259](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L2246-L2259)）正好对应：`colorant`（一个 `SpotColorant`，含 `name` 与 `fallback`）+ `tint`（淡印比例 0–100%）。`SpotColorant` 见 [color.rs:2297-2309](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L2297-L2309)。

#### 4.4.2 核心流程

`convert_spot(color)`（[paint.rs:155-163](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L155-L163)）组装一个 krilla `separation::Color`：

1. **算淡印强度**：`tint.get()` 是 `0.0..=1.0` 的 `Ratio`，乘以 255 四舍五入成 `u8`：

   \[
   \text{intensity} = \mathrm{round}(\text{tint} \times 255) \in [0, 255]
   \]

2. **建 SeparationSpace**：
   - 色料名：`color.colorant.name.to_krilla()`（`None → NoColorant`、`All → AllColorants`、`Custom(s) → Custom(s)`，见 [util.rs:164-188](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs#L164-L188)）。
   - 备选颜色：`convert_process_solid(color.colorant.fallback).0` —— 把色料的**工程色 fallback** 走一遍 4.3 的管道，取其 `RegularColor`。
3. 返回 `separation::Color::new(intensity, space)`，外层（`convert_solid`）把 alpha 写成 255。

> **关键细节（易错点）**：这里用的是 `color.colorant.fallback`（**原始、未经 tint 调整**的备选工程色），而**不是** `color.fallback()` 方法。`SpotColor::fallback()` 会做 `self.colorant.fallback.lighten(1 - tint)`（[color.rs:2286-2288](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L2286-L2288)），即把 tint 折进备选色。如果这里也用 `fallback()`，tint 就会被应用两次（一次折进颜色、一次作为 intensity），导致淡印效果翻倍。所以必须用原始 `fallback`，把 tint 单独作为 intensity 传入——颜色与浓淡各司其职。

#### 4.4.3 源码精读

`convert_spot`：[crates/typst-pdf/src/paint.rs:155-163](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L155-L163)

```rust
fn convert_spot(color: &SpotColor) -> separation::Color {
    separation::Color::new(
        (color.tint.get() * 255.0).round() as u8,       // ① 淡印强度
        SeparationSpace::new(
            color.colorant.name.to_krilla(),            // ② 色料名
            convert_process_solid(color.colorant.fallback).0,  // ③ 备选颜色
        ),
    )
}
```

逐行说明：

- ① 淡印强度：`tint` 从 `Ratio` 换算到 `u8`。例如 tint = 70% → `round(0.7 × 255) = round(178.5) = 179`。
- ② 色料名：经 `SpotColorantToNameExt`（[util.rs:172-188](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs#L172-L188)）翻译成 krilla `SeparationColorant`。
- ③ 备选颜色：把 `colorant.fallback`（一个 `ProcessColor`）丢进 `convert_process_solid`，取 `.0`（即 `RegularColor`）作为 Separation 的备选色。注意是 `colorant.fallback` 字段，不是 `SpotColor::fallback()` 方法。

#### 4.4.4 代码实践

**实践目标**：追踪一个 PANTONE 专色，说清 fallback 如何参与 SeparationSpace 构造。

**操作步骤**：

设想用户写（参考 [color.rs:2364-2373](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/color.rs#L2364-L2373) 的示例）：

```typst
#let pantone = color.spot("PANTONE 2221 C", rgb("#239dad"))
#rect(fill: pantone.tint(70%))
```

1. 它构造出 `Color::Spot(SpotColor { colorant: SpotColorant { name: Custom("PANTONE 2221 C"), fallback: rgb("#239dad") }, tint: 0.7 })`。
2. 在 `convert_solid` 命中 `Color::Spot` 分支，调 `convert_spot`：
   - intensity = `round(0.7 × 255) = 179`。
   - 色料名 = `Custom("PANTONE 2221 C")`。
   - 备选颜色：`rgb("#239dad")` 是 Srgb 工程色 → `convert_process_solid` 走 `_` 分支 → `convert_rgb` → `rgb::Color(r,g,b)`（取 `.0`，丢弃 alpha）。
   - 结果：`separation::Color::new(179, SeparationSpace::new(Custom("PANTONE 2221 C"), rgb 备选))`。
3. 外层把 alpha 设为 255。

**需要观察的现象 / 预期结果**：PDF 中出现一个 Separation 色彩空间，名字是 `PANTONE 2221 C`，备选色空间是 DeviceRGB、值为 `#239dad` 的近似；该颜色以 179/255 ≈ 70% 的强度绘制。

> 待本地验证：把上述 Typst 编译成 PDF，用 `mutool show` 或文本编辑器搜索 `Separation` 与 `PANTONE 2221 C`，应能看到该色彩空间对象及其 AlternateColorSpace。

#### 4.4.5 小练习与答案

**练习 1**：`convert_spot` 用 `color.colorant.fallback` 而不是 `color.fallback()`，二者区别是什么？为什么这里必须用前者？

> **答案**：`SpotColor::fallback()` 会调用 `self.colorant.fallback.lighten(1 - tint)`，把 tint 折进备选颜色；而 `color.colorant.fallback` 是原始备选色，不含 tint 调整。这里 tint 已经单独作为 intensity（第一参数）传入 Separation，若再用 `fallback()` 就会让 tint 被应用两次，所以必须用原始 `fallback`。

**练习 2**：tint = 40% 时，`convert_spot` 第一参数 `intensity` 算出来是多少？

> **答案**：`round(0.4 × 255.0) = round(102.0) = 102`。

**练习 3**：如果一个专色的 `name` 是 `None`，它的色料名会变成什么？（提示：看 `SpotColorantToNameExt` 对 `Option<T>` 的实现。）

> **答案**：`None` 会映射成 krilla 的 `SeparationColorant::NoColorant`（[util.rs:181-188](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs#L181-L188)）。这常用于表示裁切线、上光等「不施加色料」的特殊工艺。

---

## 5. 综合实践

把本讲四个模块串起来，做一个完整的「颜色翻译追踪」任务。

**任务背景**：写一个 Typst 文档，含三个填充各异的矩形，分别预测并验证它们在 PDF 中的颜色结构。

```typst
#let pantone = color.spot("PANTONE 2221 C", rgb("#239dad"))

#rect(width: 3cm, height: 1cm, fill: rgb("#ff000080"))   // ① 半透明红
#rect(width: 3cm, height: 1cm, fill: cmyk(20%, 0%, 80%, 0%))  // ② CMYK
#rect(width: 3cm, height: 1cm, fill: pantone.tint(70%))  // ③ 专色
```

**要求**：对每个矩形，填出下表（先凭本讲知识预测，再本地验证）。

| 矩形 | 走 `convert_process_solid` / `convert_spot` 哪条分支 | 最终 krilla 颜色类型 | opacity |
|---|---|---|---|
| ① 半透明红 | `_` → `convert_rgb` | `rgb::Color(255,0,0)` | 128/255 ≈ 0.502 |
| ② CMYK | `Cmyk` → `convert_cmyk` | `cmyk::Color(51,0,204,0)` | 255（不透明） |
| ③ 专色 | （不走 process）`convert_spot` | `separation::Color`，名 `PANTONE 2221 C`，备选 rgb | 255（不透明），intensity = 179 |

**操作步骤**：

1. 先不看源码，独立填出上表的「分支」「颜色类型」「opacity」三列。
2. 把文档编译为 PDF（`typst compile`）。
3. 用 `qpdf --qdf` 或 `mutool show` 解开 PDF：
   - 矩形 ①：应在图形状态里找到约 0.502 的 `ca`，填充色为 `1 0 0 rg`。
   - 矩形 ②：应看到 `k`（CMYK）填充 `0.2 0 0 0.8 0 k` 之类，无透明度项。
   - 矩形 ③：应搜到 `Separation` 色彩空间，含 `PANTONE 2221 C` 与 DeviceRGB 备选。

**预期结果**：你的预测与解包结果一致。若有出入，回到对应模块的源码精读复核。

> 待本地验证：具体 PDF 操作符与对象编号取决于 krilla 输出，以本地 `qpdf` / `mutool` 实际结果为准。

---

## 6. 本讲小结

- `convert_fill` / `convert_stroke` 是把 Typst 填充、描边翻译成 krilla `Fill` / `Stroke` 的入口；二者都体现「**颜色与不透明度分离**」——alpha 被单独抽成 `opacity = alpha/255.0`。
- `convert_paint` 是 `Paint` 的总分派器（纯色 / 渐变 / 平铺），`convert_solid` 再按 `Color::Process` / `Color::Spot` 二次分派；专色统一按不透明（alpha 255）导出。
- 工程色 `convert_process_solid` 按色彩空间三路归并：`D65Gray → luma`、`Cmyk → cmyk`、其余 `→ sRGB`，统一用 `to_space(...) → to_vec4_u8()` 取分量。
- **CMYK 颜色天然无 alpha**（第 4 字节是 K 不是 alpha），所以导出恒不透明；RGB / Luma 的 alpha 在第 4 字节，会被拆出。
- 专色用 PDF 的 **Separation 色彩空间**表达：色料名 + 备选工程色；淡印 tint 单独作为 `intensity = round(tint×255)` 传入，备选色用**原始** `colorant.fallback`（避免 tint 双重应用）。

---

## 7. 下一步学习建议

本讲只覆盖了 `Paint::Solid`。`convert_paint` 的另外两条分支——渐变与平铺图案——涉及更复杂的几何与变换计算，是下一讲 u3-l12《渐变与平铺图案》的内容。建议：

1. 阅读 [paint.rs:200-301](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L200-L301) 的 `convert_gradient`（线性 / 径向 / 锥形），关注它如何用到本讲的 `size` / `offset` 包围盒。
2. 阅读 [paint.rs:303-402](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L303-L402) 的 `convert_gradient_stops`，看它如何复用本讲的 `convert_solid` 来处理每个色站（含 Oklab / 色相空间的中间色站插入）。
3. 阅读 [paint.rs:411-426](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L411-L426) 的 `correct_transform`，理解 `RelativeTo::Parent` 为何要「先逆向当前变换再重加容器变换」——这会回扣 u2-l7 的 `State` / `container_transform`。

掌握本讲后，你已经具备了追踪「任意一种 Typst 颜色最终落到 PDF 哪个色彩对象」的能力。
