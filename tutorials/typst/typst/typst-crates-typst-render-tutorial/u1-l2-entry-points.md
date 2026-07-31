# 渲染入口 render 与 RenderOptions

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `render()` 和 `render_merged()` 这两个公共入口各自做什么、有什么区别。
- 掌握 `RenderOptions` 的两个旋钮 `pixel_per_pt` 与 `render_bleed` 如何控制输出分辨率与出血区。
- 给定一个页面尺寸，能手算出渲染后的像素画布大小。
- 理解背景色处理（`fill_or_white`）和单位转换（`AbsExt`）这两个配套小机制。

本讲只看「入口层」：参数怎么进来、画布多大、背景怎么填、多页怎么拼。画布里的内容（文字、形状、图像）是怎么一笔一笔画上去的，留给后续讲义。

## 2. 前置知识

本讲承接 [u1-l1 项目概览与定位](u1-l1-project-overview.md)。那里我们建立了全局观：

- typst-render 是 Typst 的**光栅图像导出器**，把排版结果 `Page`/`Frame` 渲染成像素缓冲 `tiny_skia::Pixmap`，再编码成 PNG。
- 命令行 `--ppi`（默认 144）经 `pixel_per_pt = ppi / 72.0` 换算后传入本 crate。

本讲需要你先理解以下几个名词（都会在正文中再展开）：

| 名词 | 直觉解释 |
| --- | --- |
| `Page` | 一页排版完成后的结果，包含内容帧 `frame`、出血 `bleed`、背景 `fill` 等。 |
| `Pixmap` | tiny-skia 提供的 RGBA 位图画布，就是「最终那张图」在内存里的形态。 |
| pt（磅） | 排版长度单位，\(1 \text{ inch} = 72 \text{ pt}\)。 |
| `Abs` | Typst 的绝对长度类型（如 `595pt`），需要时再换算成像素。 |
| `pixel_per_pt` | 分辨率倍数：每个 pt 对应几个像素。 |

一句话：`pixel_per_pt` 决定「画多清晰」，`render_bleed` 决定「要不要把裁切出血区也画进去」。

## 3. 本讲源码地图

本讲几乎全部内容集中在单个文件里。

| 文件 | 作用 |
| --- | --- |
| `crates/typst-render/src/lib.rs` | crate 根，定义 `render` / `render_merged` / `RenderOptions` / `AbsExt`，是本讲的主角。 |
| `crates/typst-layout/src/document.rs` | 定义 `Page` 及其 `fill_or_white()` 方法（背景色决策）。 |
| `crates/typst-cli/src/compile.rs` | 命令行的 PNG 导出在这里调用 `render`，并用 `ppi/72.0` 构造 `RenderOptions`。 |

## 4. 核心概念与源码讲解

### 4.1 RenderOptions：控制输出分辨率与出血区

#### 4.1.1 概念说明

`RenderOptions` 是渲染的**全部配置**——它只有两个字段。可以把渲染想象成「拿一台相机拍排版好的版面」：

- `pixel_per_pt`：相当于相机的**分辨率/放大倍数**。值越大，每个排版点被采样成更多像素，图越清晰，但内存和耗时也越多。
- `render_bleed`：相当于「要不要把版面四周那圈**出血区**也拍进去」。出血区是印刷时留给裁切误差的额外边距，屏幕预览通常不需要。

#### 4.1.2 核心流程

```text
RenderOptions { pixel_per_pt, render_bleed }
        │
        ├── pixel_per_pt.get()  →  f32（每个 pt 多少像素）
        │       用于把 pt 尺寸换算成像素尺寸，决定画布多大
        │
        └── render_bleed        →  bool
                true  : 使用 page.bleed，画布向外扩展一圈
                false : 出血归零，画布严格等于页面尺寸
```

#### 4.1.3 源码精读

结构体定义在 [src/lib.rs:88-104](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L88-L104)，这两个字段就是全部旋钮：

- `pixel_per_pt: Scalar` —— `Scalar` 是 `typst-utils` 里包了一层 `f64` 的新类型，`.get()` 返回 `f64`（见 [scalar.rs:35](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs#L35-L37)）。用 `Scalar` 而不是裸 `f64`，是为了让它能自动派生 `Hash`/`Eq`，便于后续做缓存。
- `render_bleed: bool` —— 是否把出血区也渲染出来。

默认值在 [src/lib.rs:106-113](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L106-L113) 的 `Default` 实现里：

```rust
impl Default for RenderOptions {
    fn default() -> Self {
        Self {
            pixel_per_pt: Scalar::new(2.0),
            render_bleed: false,
        }
    }
}
```

⚠️ **读代码，别只读注释**：字段上方文档注释写的是「默认 `1.0`」，但 `Default` 实现实际给的是 `2.0`。真正调用 `RenderOptions::default()` 得到的是 `2.0`，以代码为准。命令行默认 `ppi = 144`，`144 / 72.0 = 2.0`，两条路径在默认情况下都收敛到 `2.0`。

#### 4.1.4 代码实践

**目标**：确认默认值，并感受 `pixel_per_pt` 对「像素总量」的影响。

1. 打开 [src/lib.rs:106-113](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L106-L113)，确认 `Default` 里的 `Scalar::new(2.0)`。
2. 像素总量与 `pixel_per_pt` 的关系是面积（平方）关系：

\[
\text{像素总数} \propto (\text{pixel\_per\_pt})^2
\]

3. 把 `pixel_per_pt` 从 `2.0` 降到 `1.0`，像素总数变为原来的 \(1/4\)（因为 \( (1/2)^2 = 1/4 \)）。这就是降分辨率能显著省内存/省时间的原因。

**预期结果**：你会理解为什么 `pixel_per_pt` 是「性价比」最高的一个旋钮——翻倍意味着 4 倍像素。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pixel_per_pt` 用 `Scalar` 包装，而不直接用 `f32`？
**答案**：`Scalar` 基于 `f64` 且实现了 `Hash`/`Eq`，方便作为记忆化缓存的键；直接用浮点数不易派生这些 trait。

**练习 2**：`render_bleed` 默认是 `true` 还是 `false`？为什么屏幕预览不需要它？
**答案**：默认 `false`。出血区是给印刷裁切留的余量，屏幕预览时画面就是页面本身，不需要那圈额外边距。

---

### 4.2 AbsExt：把 Typst 长度变成 tiny-skia 的 f32

#### 4.2.1 概念说明

Typst 内部用 `Abs` 表示绝对长度（一种带单位的强类型），而 tiny-skia 这个 2D 引擎只认 `f32` 数值（以 pt 为单位）。两者之间需要一座桥——这就是 `AbsExt` trait。它只做一件事：把 `Abs` 转成「以 pt 为单位的 `f32`」。

#### 4.2.2 核心流程

```text
Abs（Typst 长度）
   │  to_pt()        →  f64（换算成磅）
   │  as f32         →  f32（截断到单精度）
   └── 得到 tiny-skia 能直接用的 f32 坐标
```

#### 4.2.3 源码精读

定义在 [src/lib.rs:276-286](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L276-L286)：

```rust
trait AbsExt {
    /// Convert to a number of points as f32.
    fn to_f32(self) -> f32;
}

impl AbsExt for Abs {
    fn to_f32(self) -> f32 {
        self.to_pt() as f32
    }
}
```

关键点：`to_pt()` 先把 `Abs` 换算成磅（返回 `f64`），再 `as f32` 降为单精度。typst-render 里所有「pt → 像素」的换算（如下一节的 `size.x.to_f32()`）都走这个扩展方法。

#### 4.2.4 代码实践

**目标**：在 `render()` 里数一数 `to_f32()` 被调用了几次。

1. 打开 [src/lib.rs:21-48](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L21-L48)。
2. 找到 `size.x.to_f32()`、`size.y.to_f32()` 和 `bleed.left`/`bleed.top` 的 `pre_translate`（内部也用 `to_f32()`）。
3. **观察现象**：凡是来自排版层（`Abs`）的长度，进入 tiny-skia 前都经过这道转换。
4. **预期结果**：你会看到 typst-render 在「Typst 的类型世界」和「tiny-skia 的 f32 世界」交界处统一用 `AbsExt` 收口。

#### 4.2.5 小练习与答案

**练习**：为什么是 `to_pt() as f32`，而不是 `as f32` 直接转 `Abs`？
**答案**：`Abs` 是带语义的强类型，不能直接 `as f32`；必须先用 `to_pt()` 取出它表示的磅数（`f64`），再转 `f32`。

---

### 4.3 render()：单页渲染主入口

#### 4.3.1 概念说明

`render()` 是最核心的公共入口：输入一页 `Page` 和一组 `RenderOptions`，输出一张 `Pixmap`（像素画布）。它的工作可以拆成四步：

1. **算画布尺寸**：页面尺寸 + 出血，再乘以 `pixel_per_pt`，得到像素宽高。
2. **建画布**：用 tiny-skia 创建对应大小的 `Pixmap`。
3. **填背景**：根据页面背景设置，给画布铺底色。
4. **画内容**：把坐标系往内偏移出血量，然后递归渲染整页内容帧。

#### 4.3.2 核心流程

```text
render(page, opts)
  │
  ├─ 1. bleed    = opts.render_bleed ? page.bleed : 0
  │    size      = page.frame.size() + bleed.sum_by_axis()   // 水平:左+右  垂直:上+下
  │    pxw = round(pixel_per_pt * size.x.to_f32()).max(1.0)
  │    pxh = round(pixel_per_pt * size.y.to_f32()).max(1.0)
  │
  ├─ 2. canvas   = Pixmap::new(pxw, pxh)
  │
  ├─ 3. 若 page.fill_or_white() 返回 Some：
  │      Solid 纯色  → canvas.fill(color)            // 快路径
  │      其它(渐变等)→ 用一个全画布矩形走 render_shape // 通用路径
  │
  ├─ 4. state    = state.pre_translate(bleed.left, bleed.top)  // 内容向内偏移
  │    render_frame(canvas, state, &page.frame)                // 递归画内容
  │
  └─ 返回 canvas
```

#### 4.3.3 源码精读

完整函数在 [src/lib.rs:20-48](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L20-L48)（开头的 `#[typst_macros::time(name = "render")]` 是性能计时标记，后续讲义会讲）。

画布尺寸的计算在 [src/lib.rs:22-27](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L22-L27)：

```rust
let bleed = if opts.render_bleed { page.bleed } else { Sides::default() };
let size = page.frame.size() + bleed.sum_by_axis();
let pixel_per_pt = opts.pixel_per_pt.get() as f32;
let pxw = (pixel_per_pt * size.x.to_f32()).round().max(1.0) as u32;
let pxh = (pixel_per_pt * size.y.to_f32()).round().max(1.0) as u32;
```

要点逐条解释：

- `bleed.sum_by_axis()` 把四边的出血折成「水平总量 = 左 + 右」「垂直总量 = 上 + 下」（见 [sides.rs:108-110](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/sides.rs#L108-L110)），所以 `render_bleed=true` 时画布会向外「胖」一圈。
- `opts.pixel_per_pt.get()` 返回 `f64`，`as f32` 降精度后参与乘法。
- `round()` 四舍五入到整数像素。
- `.max(1.0)` 是一道**保护**：保证结果至少是 1。否则当页面某轴尺寸为 0（退化页面）或 `pixel_per_pt` 极小时，`round()` 会得到 `0.0`，`as u32` 成 `0`，下一行 `Pixmap::new(0, h)` 会返回 `None`，紧随其后的 `.unwrap()` 就会 panic。`.max(1.0)` 确保画布每条边至少 1 像素，让 `unwrap()` 永远安全。

背景填充在 [src/lib.rs:34-41](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L34-L41)：

```rust
if let Some(fill) = page.fill_or_white() {
    if let Paint::Solid(color) = fill {
        canvas.fill(paint::to_sk_color(color.to_process()));
    } else {
        let rect = Geometry::Rect(size).filled(fill);
        shape::render_shape(&mut canvas, state, &rect);
    }
}
```

这里有一条**快路径**：最常见的纯色背景直接用 `canvas.fill()` 一次性铺满，最快；若是渐变/平铺这类复杂填充，则退化成「画一个全画布大小的矩形」走通用 `render_shape`（更慢但通用）。最后 [src/lib.rs:43-45](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L43-L45) 把内容原点向内偏移 `(bleed.left, bleed.top)`，让画面正文落在出血框内部，再交给 `render_frame` 递归绘制。

#### 4.3.4 代码实践

**目标**：手算 A4 页面在 `pixel_per_pt` 改变时的输出像素尺寸，并解释 `round().max(1.0)`。

A4 页面约 \(595 \times 842\) pt。公式（以宽为例）：

\[
\text{pxw} = \max\bigl(1.0,\ \text{round}(\text{pixel\_per\_pt} \times 595)\bigr)
\]

1. **默认 `pixel_per_pt = 2.0`**：
   - \(\text{pxw} = \text{round}(2.0 \times 595) = \text{round}(1190) = 1190\)
   - \(\text{pxh} = \text{round}(2.0 \times 842) = \text{round}(1684) = 1684\)
   - 像素总数 \(\approx 1190 \times 1684 \approx 200\text{万}\)。
2. **改为 `pixel_per_pt = 1.0`**（把 [src/lib.rs:109](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L106-L113) 的 `Scalar::new(2.0)` 改成 `Scalar::new(1.0)`）：
   - \(\text{pxw} = \text{round}(1.0 \times 595) = 595\)
   - \(\text{pxh} = \text{round}(1.0 \times 842) = 842\)
   - 像素总数 \(\approx 595 \times 842 \approx 50\text{万}\)，正好是原来的 \(1/4\)。
3. **解释 `round().max(1.0)`**：若某页面宽度为 0 pt，`round(2.0 × 0) = 0`，`.max(1.0)` 把它抬到 1，避免 `Pixmap::new(0, …)` 返回 `None` 而让 `.unwrap()` 崩溃。它本质是「画布尺寸下限保护」。

> 说明：以上为按公式手算的结果，未实际运行；若你本地修改默认值后用 CLI 导出一张 A4 文档的 PNG，可对照图片属性验证像素尺寸（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：若 `render_bleed = true` 且页面左右各出血 3pt，画布宽度会比页面宽多少 pt？
**答案**：宽 `bleed.sum_by_axis().x = 左 + 右 = 6pt`，对应像素 `round(pixel_per_pt × 6)`。

**练习 2**：为什么纯色背景走 `canvas.fill()`，而渐变背景要走 `render_shape`？
**答案**：`canvas.fill()` 是一次性铺满整张画布的最快方式，只适用于单一颜色；渐变/平铺需要按位置采样颜色，只能退化成「画一个矩形」走通用形状渲染。

---

### 4.4 fill_or_white()：决定页面背景色

#### 4.4.1 概念说明

`fill_or_white()` 是 `Page` 的方法，定义在 typst-layout 里，不在 typst-render。它负责回答一个看似简单的问题：「这一页的背景该涂什么颜色？」答案取决于页面 `fill` 字段的三种状态。它的存在是为了让光栅导出器（和 SVG 导出器）统一拿到一个「兜底为白」的背景色。

#### 4.4.2 核心流程

`page.fill` 的类型是 `Smart<Option<Paint>>`，三层嵌套对应三种语义：

```text
Smart::Auto                  → 白色      （"自动"：光栅/SVG 当作白纸）
Smart::Custom(None)          → 透明 None （显式不要背景）
Smart::Custom(Some(paint))   → 该 paint  （显式指定背景）
```

#### 4.4.3 源码精读

方法定义在 [crates/typst-layout/src/document.rs:118-120](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/document.rs#L118-L120)：

```rust
pub fn fill_or_white(&self) -> Option<Paint> {
    self.fill.clone().unwrap_or_else(|| Some(Color::WHITE.into()))
}
```

对照 `Page` 结构体上的注释（[document.rs:82-105](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/document.rs#L82-L105)）：`fill` 为 `Auto` 时，PDF 导出透明、**光栅/SVG 导出按白纸**处理。这正是 `fill_or_white`（光栅用）与 `fill_or_transparent`（PDF 用）两个方法的分工。直觉上：PNG 默认透明会让白底文档「看不见」，所以光栅导出默认铺白，像一张纸。

#### 4.4.4 代码实践

**目标**：在 `render()` 里追踪 `fill_or_white()` 的返回值如何分流。

1. 回到 [src/lib.rs:34-41](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L34-L41)。
2. 假设三种页面配置，写出 `render()` 的行为：
   - 页面 `fill = Auto`：`fill_or_white()` 返回 `Some(白)` → `Paint::Solid` 命中 → `canvas.fill(白)`。
   - 页面 `fill = Custom(None)`：返回 `None` → 跳过整个 `if`，画布保持透明。
   - 页面 `fill = Custom(Some(渐变))`：返回 `Some(渐变)` → 走 `else` → `render_shape` 画矩形。
3. **预期结果**：你能说清「为什么导出的 PNG 默认是白底」——因为 `fill_or_white` 把 `Auto` 兜底成了白色。

#### 4.4.5 小练习与答案

**练习**：`fill_or_white` 和 `fill_or_transparent` 的区别是什么？为什么光栅导出选前者？
**答案**：前者把 `Auto` 兜底为白色，后者把 `Auto` 兜底为透明（`None`）。PNG 背景透明会让白底文档在大多数查看器里显得「空」，故光栅导出默认按白纸处理；PDF 本身支持页面透明，故用后者。

---

### 4.5 render_merged()：多页纵向拼成一张图

#### 4.5.1 概念说明

`render()` 一次只画一页。`render_merged()` 则把整个文档的**所有页**纵向拼成一张长图：先逐页调 `render()` 得到各自的 `Pixmap`，再从上到下依次贴到一张「足够高」的大画布上，页与页之间可以留间距 `gap`、整体铺一层底色 `fill`。

> 说明：当前 typst-cli 的 PNG 导出用的是**逐页** `render()`（见 [compile.rs:575](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/compile.rs#L575-L575)），并未调用 `render_merged`。`render_merged` 是提供给库使用者的公共 API（例如想把多页文档预览成一张长图时）。

#### 4.5.2 核心流程

```text
render_merged(document, opts, gap, fill)
  │
  ├─ 1. pixmaps = document.pages().map(|p| render(p, opts))   // 逐页渲染
  │
  ├─ 2. gap_px  = round(pixel_per_pt * gap.to_f32())
  │    pxw = max(各页 pixmap 宽度)                              // 取最宽页
  │    pxh = sum(各页高度) + gap_px * (页数 - 1)                // 总高含间距
  │
  ├─ 3. canvas  = Pixmap::new(pxw, pxh)
  │    若 fill 为 Some → canvas.fill(fill)                      // 可选底色
  │
  ├─ 4. for 每页 pixmap：
  │       canvas.draw_pixmap(0, y, pixmap, …)                  // 贴到 y 处
  │       y += pixmap.height() + gap_px                        // 下移一页+间距
  │
  └─ 返回 canvas
```

#### 4.5.3 源码精读

完整函数在 [src/lib.rs:50-86](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L50-L86)。几个关键点：

- **复用 `render()`**：[src/lib.rs:57-58](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L57-L58) 对每一页调用 `render(page, opts)`，所以单页的所有逻辑（分辨率、出血、背景）在这里完全一致。
- **gap 的单位转换**：`gap` 是 `Abs`（pt），[src/lib.rs:60-61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L60-L61) 用 `pixel_per_pt * gap.to_f32()` 换算成像素间距（这里用到 `AbsExt`）。
- **总高度计算**：[src/lib.rs:62-64](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L62-L64) 中宽度取所有页的最大值，高度是「各页高度之和 + 间距」。`pixmaps.len().saturating_sub(1)` 保证只有 1 页时 gap 项为 0（没有「页间」间距可算），`saturating_sub` 避免下溢。
- **纵向贴图**：[src/lib.rs:71-83](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L71-L83) 用 `canvas.draw_pixmap(0, y, …)` 把每页贴到当前 `y` 偏移，再让 `y` 前进 `pixmap.height() + gap`。

#### 4.5.4 代码实践

**目标**：手算一个两页文档拼图的尺寸。

假设两页都是 A4（\(595 \times 842\) pt），`pixel_per_pt = 2.0`，`gap = 10pt`：

1. 每页 `render()` 得到 \(1190 \times 1684\) 像素（见 4.3.4）。
2. `gap_px = round(2.0 × 10) = 20` 像素。
3. 拼图画布：
   - 宽 = \(\max(1190, 1190) = 1190\)。
   - 高 = \(1684 + 1684 + 20 \times (2-1) = 3388\)。
4. **预期结果**：得到一张 \(1190 \times 3388\) 的长图，两页之间有 20 像素的间隔。

> 说明：此为按公式手算，未实际运行（**待本地验证**）。

#### 4.5.5 小练习与答案

**练习 1**：为什么高度公式里乘的是 `页数 - 1` 而不是 `页数`？
**答案**：\(n\) 页之间只有 \(n-1\) 个间隔，首尾外侧不加 gap，所以间距项是 `gap × (页数 - 1)`。

**练习 2**：`render_merged` 的 `fill` 参数和单页的 `fill_or_white` 有什么不同？
**答案**：单页背景由 `Page` 自身的 `fill`（经 `fill_or_white`）决定；`render_merged` 的 `fill` 是**拼图画布整体底色**（`Option<Color>`，贴图前铺一次），与各页自己的背景相互独立。

## 5. 综合实践

把本讲的几个模块串起来，模拟「从命令行参数到最终画布」的完整推演。

**场景**：用户运行 `typst compile doc.typst 'doc-{n}.png'`，命令行 `--ppi 72`（注意不是默认的 144），文档共 2 页，每页 A4（\(595 \times 842\) pt），无出血，页面背景为默认（`Auto`）。

请按顺序推演：

1. **CLI 构造选项**：查看 [compile.rs:633-638](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/compile.rs#L633-L638) 的 `png_options`。`ppi = 72` 时，`pixel_per_pt = 72 / 72.0 = 1.0`，`render_bleed = false`。
2. **调用 render**：[compile.rs:575](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/compile.rs#L575-L575) 对每页调用 `typst_render::render(page, &options)`。
3. **算画布**：`render_bleed=false` → `bleed=0` → `size = 595×842` pt → `pxw = round(1.0×595)=595`，`pxh = round(1.0×842)=842`。
4. **填背景**：`fill_or_white()` 对 `Auto` 返回白色 → `canvas.fill(白)`。
5. **输出**：每页得到一张 \(595 \times 842\) 的白底 PNG。

**你需要回答**：

- 改用默认 `--ppi 144` 后，单张 PNG 的像素尺寸是多少？（答：\(1190 \times 1684\)）
- 若把这 2 页用 `render_merged(doc, &opts, gap=Abs::pt(8.0), None)` 拼成长图（`ppi=72`），画布多大？（答：宽 595，高 \(842+842+\text{round}(1.0×8)=1692\)）
- 整个过程中 `AbsExt::to_f32` 至少在哪两处被用到？（答：算 `pxw/pxh` 时的 `size.x.to_f32()`，以及 `render_merged` 里把 `gap` 换算成像素时）

> 说明：以上为依据源码逻辑的手算推演，非实际运行结果（**待本地验证**）。你可以真的用 CLI 以不同 `--ppi` 导出同一文档，对比 PNG 文件的像素属性来验证。

## 6. 本讲小结

- `RenderOptions` 只有两个旋钮：`pixel_per_pt`（分辨率，默认 `2.0`，注意文档注释写的 `1.0` 已过时，以 `Default` 实现为准）和 `render_bleed`（是否渲染出血区，默认 `false`）。
- `render()` 的四步：按 `页面尺寸 + 出血` 与 `pixel_per_pt` 算像素画布 → 建 `Pixmap` → 填背景 → 偏移出血量后递归渲染内容。
- `round().max(1.0)` 是画布尺寸下限保护，避免退化尺寸导致 `Pixmap::new(…).unwrap()` 崩溃。
- `fill_or_white()` 把 `Auto` 背景兜底为白色，这是 PNG 默认白底的原因；纯色走 `canvas.fill()` 快路径，渐变等走 `render_shape` 通用路径。
- `render_merged()` 复用 `render()` 逐页渲染，再纵向拼接，高度 = 各页高度之和 + `gap × (页数-1)`；当前 CLI 的 PNG 导出并未调用它。
- `AbsExt::to_f32()` 是 Typst 长度（`Abs`）到 tiny-skia `f32` 的统一转换桥，所有 pt→像素换算都走这里。

## 7. 下一步学习建议

本讲只看了「入口层」：画布怎么来、多大、铺什么底。但 `render()` 最后那行 `render_frame(canvas, state, &page.frame)` 才是真正「画内容」的起点，而我们一直刻意绕开的 `State`（坐标变换、遮罩）也藏在那里。

- **下一讲 [u1-l3 Frame 场景树与 render_frame 派发](u1-l3-frame-tree.md)**：搞清楚 `Frame` 这棵场景树长什么样，`render_frame` 如何按 `FrameItem`（Group/Text/Shape/Image/Link/Tag）把任务分派给各个子模块。
- 之后再进 `State` 与坐标变换（u2-l1），那是理解所有内容如何「落到正确像素位置」的关键。
