# 嵌入 PDF 图转 SVG（hayro）

> 本讲是 **u6（图像、去重机制与集成）** 的第 2 篇，承接 [u6-l1 图像渲染与 WebImage](u6-l1-images-and-webimage.md)。在 u6-l1 中我们看到 `WebImage::new` 的四个分支里，`ImageKind::Pdf` 这一支最特殊——它调用一个私有函数 `pdf_to_svg` 把 PDF「翻译」成 SVG。本讲就专门拆解这条路径。

## 1. 本讲目标

读完本讲，你应当能够：

- 说清**为什么**矢量 SVG 导出里嵌入的 PDF 图不能直接当字节透传，必须先经 hayro 转换。
- 读懂 `pdf_to_svg` 的三段式结构：`select_standard_font` 字体闭包 → `InterpreterSettings` 解释器配置 → `hayro_svg::convert` 调用。
- 逐字段解释 `InterpreterSettings` 的四个配置（`font_resolver` / `cmap_resolver` / `warning_sink` / `render_annotations`）各自的含义与取舍。
- 列出 14 种 PDF 标准字体到 `typst_assets::pdf` 常量的映射，理解「三族 × 四变体 + 两个符号字体」的结构。
- 说明 `SvgRenderSettings { bg_color: [0, 0, 0, 0] }` 中透明背景的必要性。
- 理解 `// Keep this in sync with typst-png!` 这条跨 crate 维护约定背后「手工保持两份近乎相同的代码同步」的现实与风险。

## 2. 前置知识

本讲默认你已经读过 u6-l1，知道下列概念（若陌生请先回看）：

- **`WebImage` / `WebImageFormat`**：图像的「Web 归一化」中间表示，把 Typst 内部多种 `ImageKind` 抹平成浏览器能直接识别的少数格式（Png/Jpg/Gif/Webp/Svg）。
- **`WebImage::new`**：归一化入口，被 `#[comemo::memoize]`，重复图片只转一次。
- **`to_base64_url`**：把图像字节内联成 `data:{mime};base64,...` 的 data URL，使最终 SVG 自包含。
- **`ImageKind::Pdf(PdfImage)`**：Typst 通过 `image("foo.pdf")` 嵌入的「PDF 图」——它其实是一整页 PDF（含文字、矢量路径、可能还嵌着位图）。

此外需要一点 PDF 背景常识（下面会解释，不懂也能读）：

- **PDF 标准字体（Standard Font / Base-14）**：PDF 规范预定义了 14 种字体，文档可以「只点名、不内嵌字体文件」就能引用它们（如 Helvetica、Courier、Times）。
- **内容流（content stream）**：一页 PDF 的可见内容，由一串绘图指令（画路径、贴图、显示文字）组成，需要被「解释」才能变成像素或矢量原语。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 |
|------|------|
| [`src/image.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs) | 图像渲染主文件；含 `WebImage::new`（Pdf 分支在 L126-L128）、私有函数 `pdf_to_svg`（L145-L184）。本讲焦点。 |

作为对照，还会引用同仓库的另一处「孪生」实现，用来讲跨 crate 同步：

| 文件 | 作用 |
|------|------|
| `crates/typst-render/src/image.rs` | typst-render（位图/PNG 后端）里的 `build_pdf_texture`，与 `pdf_to_svg` 配置几乎完全相同，只是输出目标是位图而非 SVG。 |

hayro 与 hayro_svg 是两个**外部 crate**（依赖见 [`Cargo.toml`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/Cargo.toml) 的 `hayro` / `hayro-svg` 两行）。它们的源码不在本仓库工作目录内，本讲依据 `image.rs` 顶部的 `use` 语句与 typst-render 中的使用方式来推断其 API 形状，不臆造未确认的内部细节。

---

## 4. 核心概念与源码讲解

### 4.1 为什么嵌入 PDF 图要先用 hayro 转换，以及它在哪接入

#### 4.1.1 概念说明

先建立一个直觉：**Typst 文档里可以嵌入一张「PDF 图」**（`image("diagram.pdf")`）。这张「图」并不是一张普通照片，而是一整页 PDF——里面有矢量线段、填充、甚至一串用 Helvetica 写的文字。

那么把它导出进 SVG 时，有两条路：

1. **栅格化（rasterize）**：把这一页 PDF 渲染成一张 PNG 像素图，再内联进 SVG。
   - 缺点：失去矢量精度、放大发虚；线稿图变成大块位图，体积反而可能更大。
2. **重新解释为矢量**：用一个「PDF 解释器」读出这一页的绘图指令，**重新发**成 SVG 的 `<path>`、`<text>` 等矢量原语。
   - 优点：保持矢量、可缩放、线稿体积小。

typst-svg 选的是第 2 条，承担「PDF 解释器」角色的就是 **hayro**。它读 PDF 内容流，输出矢量原语；而 **hayro_svg** 则是 hayro 的 SVG 后端，把这些原语直接写成 SVG 字符串。

> 关键洞察：之所以要「转」而不是「透传」，是因为 PDF 与 SVG 是两套不同的矢量描述语言。浏览器/ SVG 阅读器看不懂 PDF 内容流，必须有人把它翻译过来。

#### 4.1.2 核心流程

PDF 图进入 typst-svg 的完整链路（承接 u6-l1）：

```
image("foo.pdf")
   │  （typst-library 解析为 ImageKind::Pdf(PdfImage)）
   ▼
WebImage::new(image)                     ── image.rs:103
   │  匹配到 ImageKind::Pdf(pdf) 分支      ── image.rs:126
   ▼
pdf_to_svg(pdf)  →  String（一段 SVG）    ── image.rs:127, 146
   │  （hayro 解释 PDF 页，hayro_svg 写 SVG）
   ▼
Bytes::from_string(svg_string)           ── image.rs:127
   │  打上标签 WebImageFormat::Svg
   ▼
to_base64_url()  →  "data:image/svg+xml;base64,...."   ── image.rs:137
   │  （SVG 字符串被 base64 编码后内联）
   ▼
render_image 写出 <image xlink:href="data:image/svg+xml;base64,..."/>
```

注意最后一步的递归意味：**转换得到的 SVG 字符串本身又被 base64 内联进外层 SVG 的 `<image>` 元素**——也就是「SVG 里嵌了一段 data URL，而这段 data URL 解码出来又是 SVG」。这是合法且常见的做法。

#### 4.1.3 源码精读

接入点在 `WebImage::new` 的 Pdf 分支，只有一行实质代码：

```rust
ImageKind::Pdf(pdf) => {
    (WebImageFormat::Svg, Bytes::from_string(pdf_to_svg(pdf)))
}
```

[crates/typst-svg/src/image.rs:126-128](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L126-L128) —— 把 `pdf_to_svg` 返回的 SVG 字符串包进 `Bytes`，并标为 `WebImageFormat::Svg`。

这一行之后，`WebImage` 的其余机制（`to_base64_url` 的 `data:image/svg+xml;base64,` 前缀、`render_image` 的 `<image>` 元素）全部复用 u6-l1 已讲的逻辑，PDF 在此处就被「抹平」成了普通 SVG 图。对照其余三个分支：Exchange 格式零成本透传、Pixel 经 `PngEncoder` 重编码、Svg 透传——**Pdf 是最贵的一支**，因为它要运行一整个 PDF 解释器。

整个 `WebImage::new` 被 `#[comemo::memoize]` 标记（[image.rs:102](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L102)），所以同一张 PDF 图无论在文档里出现多少次，`pdf_to_svg` 只会真正执行一次——这是这条昂贵路径的关键保护。

#### 4.1.4 代码实践

**实践目标**：确认 PDF 图最终以「SVG-in-SVG data URL」的形式落地。

**操作步骤**：

1. 准备一个最小的 Typst 文档 `pdf_embed.typ`，内容嵌入一张 PDF 图：
   ```typst
   #image("diagram.pdf")
   ```
2. 用 typst CLI 导出 SVG：`typst c --format svg pdf_embed.typ`（命令的具体行为以本地安装版本为准，**待本地验证**）。
3. 用文本编辑器打开产出的 `pdf_embed.svg`，搜索 `data:image/svg+xml;base64,`。

**需要观察的现象**：

- `<image>` 元素的 `xlink:href` 以 `data:image/svg+xml;base64,` 开头（而不是 `image/png`），证明 PDF 被转成了 SVG 并内联。
- 把 base64 部分复制出来、`base64 -d` 解码，应得到一段以 `<svg` 开头的文本——即 hayro_svg 的输出。

**预期结果**：解码后能看到一段结构完整的 SVG（含 `<path>`、可能的 `<g>` 等），证明 PDF 内容流被翻译成了矢量原语，而不是一张位图。

> 若手头没有现成的 PDF 图，可只做源码阅读型实践：在 `WebImage::new` 的四个分支中标注各自产出的 `WebImageFormat` 与是否需要「重计算」，体会 Pdf 分支的特殊性。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接把 PDF 的原始字节以 `data:application/pdf;base64,...` 内联进 SVG？

**参考答案**：因为浏览器和 SVG 阅读器不解释 PDF 内容流。`<image>` 引用一段 PDF 字节在 SVG 语境下没有定义的渲染行为（不像 HTML 里 `<embed>`/`<iframe>` 能调用 PDF 插件）。必须先把 PDF 翻译成 SVG 原语或栅格成位图，SVG 渲染器才认识。typst-svg 选择翻译成矢量以保真。

**练习 2**：假设同一张 PDF 图在一个 10 页文档里被引用了 20 次，`pdf_to_svg` 实际会执行几次？为什么？

**参考答案**：只执行 1 次。因为 `WebImage::new` 标了 `#[comemo::memoize]`（[image.rs:102](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L102)），对同一个 `Image`（其 `Hash` 由 `PdfImage` 的 `document + page_index` 决定）的结果会被缓存复用。

---

### 4.2 pdf_to_svg 主流程：hayro 解释器与 InterpreterSettings

#### 4.2.1 概念说明

`pdf_to_svg` 是一个私有自由函数（不是 `impl SVGRenderer` 的方法），它接收一个 `&PdfImage`，返回 `String`（一段 SVG）。它的工作分三步：

1. **准备字体解析器** `select_standard_font`：一个闭包，把 PDF 的标准字体名翻译成真正的字体字节。
2. **组装解释器配置** `InterpreterSettings`：告诉 hayro「字体怎么找、CMap 怎么找、警告怎么处理、要不要画注释」。
3. **调用 `hayro_svg::convert`**：把 PDF 页交给 hayro 解释、由 hayro_svg 输出 SVG 字符串。

`PdfImage` 来自 typst-library，它包裹了一页 PDF：`page()` 返回那一页的 `&Page`，`width()`/`height()` 给出渲染尺寸（[pdf.rs:73-85](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/pdf.rs#L73-L85)）。

#### 4.2.2 核心流程

```
fn pdf_to_svg(pdf: &PdfImage) -> String
   │
   ├─① select_standard_font = 闭包：StandardFont → Option<(FontData, u32)>
   │       （把 14 种标准字体名映射到 typst_assets::pdf::* 字节，见 4.3）
   │
   ├─② interpreter_settings = InterpreterSettings {
   │       font_resolver:    按 FontQuery 分发，最终都落到 select_standard_font
   │       cmap_resolver:    恒返回 None（不启用 hayro 内嵌 CMap）
   │       warning_sink:     空闭包（静默吞掉警告）
   │       render_annotations: false（不渲染 PDF 注释）
   │   }
   │
   ├─③ cache = RenderCache::new()
   │
   └─④ hayro_svg::convert(
           pdf.page(),            // 解释哪一页
           &cache,
           &interpreter_settings,
           &SvgRenderSettings { bg_color: [0, 0, 0, 0] },  // 透明背景
       ) -> String
```

#### 4.2.3 源码精读

函数定义与「保持同步」注释：

```rust
// Keep this in sync with `typst-png`!
fn pdf_to_svg(pdf: &PdfImage) -> String {
```

[crates/typst-svg/src/image.rs:145-146](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L145-L146) —— 第 145 行的注释是本讲 4.4 节的主题：提醒维护者另一处有几乎相同的代码。

解释器配置与最终调用：

```rust
let interpreter_settings = InterpreterSettings {
    font_resolver: Arc::new(move |query| match query {
        FontQuery::Standard(s) => select_standard_font(*s),
        FontQuery::Fallback(f) => select_standard_font(f.pick_standard_font()),
    }),
    cmap_resolver: Arc::new(|_| None),
    warning_sink: Arc::new(|_| {}),
    render_annotations: false,
};

let cache = RenderCache::new();
hayro_svg::convert(
    pdf.page(),
    &cache,
    &interpreter_settings,
    &SvgRenderSettings { bg_color: [0, 0, 0, 0] },
)
```

[crates/typst-svg/src/image.rs:167-184](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L167-L184) —— 四个字段的配置与 `convert` 调用。

逐字段解读（前三字段的「为什么」在 typst-render 的孪生函数里有注释，typst-svg 这里是同义拷贝）：

| 字段 | 值 | 含义与取舍 |
|------|----|------------|
| `font_resolver` | `Arc<闭包>` | hayro 遇到字体时回调。两种查询：`FontQuery::Standard(s)`——PDF 点名了某个标准字体，直接查；`FontQuery::Fallback(f)`——PDF 用了 hayro 不认识的字体，由 `f.pick_standard_font()` 选一个「最接近」的标准字体兜底。两者最终都落到 `select_standard_font`（见 4.3）。 |
| `cmap_resolver` | `\|_\| None` | CMap 用于解析 CID 字体（典型如未内嵌编码的亚洲文字）。typst-render 注释原文：*"Fairly niche and enabling hayro's embedded cmap would add a considerable amount of data."*——这是个小众场景，而启用 hayro 自带 CMap 会显著增大二进制体积，故选择返回 `None`（代价：这类字体的文字可能渲染不对）。 |
| `warning_sink` | `\|_\| {}` | 空操作：静默吞掉 hayro 在解释过程中发出的警告（如遇到不支持的算子）。生产环境避免日志噪音的选择。 |
| `render_annotations` | `false` | PDF 注释（链接、表单、批注等）不被渲染。typst-render 注释原文：*"We want to render like it prints, so no annotations."*——要的是「打印效果」，交互/标注层不进矢量输出。 |

最后看 `SvgRenderSettings { bg_color: [0, 0, 0, 0] }`：

- 这是一个四元数组 `[r, g, b, a]`，即 RGBA。
- `[0, 0, 0, 0]` 表示 **R=0, G=0, B=0, A=0**——完全透明（alpha 通道为 0）。
- 之所以要透明：转换出的 SVG 将被嵌入外层 SVG 的 `<image>` 里，它背后已经有 Typst 排好的页面背景/其他内容。若给 PDF 图一个不透明背景（如 `[1,1,1,1]` 白），就会在每张 PDF 图后面盖一个色块，遮挡下层内容。
- 对照：typst-render 的孪生函数里这个字段写作 `bg_color: TRANSPARENT`（一个预定义的透明常量），语义完全一致。

> 小结：四个配置 + 透明背景，共同表达了一个产品取向——**只把 PDF 的矢量可视内容忠实地、安静地翻译过来**，不要交互层、不要自带的背景色、也不要为小众 CMap 付出体积代价。

#### 4.2.4 代码实践

**实践目标**：理解 `bg_color` 透明参数的必要性，并能推断改为不透明后的现象。

**操作步骤**（源码阅读 + 思想实验型）：

1. 读 [image.rs:178-183](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L178-L183)，确认 `SvgRenderSettings { bg_color: [0, 0, 0, 0] }` 是 `[r,g,b,a]` 形式、alpha=0。
2. 对照 typst-render 的 `build_pdf_texture`（`crates/typst-render/src/image.rs` 附近 L150-L156），看它把同一语义写成 `bg_color: TRANSPARENT`。
3. **思想实验**：若把本行的 `[0,0,0,0]` 改成 `[0,0,0,1]`（不透明黑），想象一张铺在彩色页面背景上的 PDF 图会变成什么样。

**需要观察/推断的现象**：

- 改成不透明后，hayro_svg 会在输出的 SVG 里先画一个铺满整页的背景矩形（对应 bg_color），这张图嵌入 Typst 文档后，PDF 内容之外的区域会被该色块覆盖，遮住下层页面背景。

**预期结果**：能用自己的话讲清「alpha=0 → 透明 → 不遮挡下层」这条因果链；并理解 typst-svg 与 typst-render 在此处取值一致、只是字面写法不同（`[0,0,0,0]` vs `TRANSPARENT`）。

> 说明：实际修改源码并重新编译以观察效果**待本地验证**；本实践以源码阅读与推断为主。

#### 4.2.5 小练习与答案

**练习 1**：`cmap_resolver` 返回 `None` 意味着什么？会带来什么代价？

**参考答案**：意味着不向 hayro 提供任何 CMap（字符映射）资源。CMap 用于把 CID 编码的字符映射到 Unicode/字形，典型场景是未内嵌字体文件与 CMap 的亚洲语言 PDF。返回 `None` 的代价是这类文字可能无法正确显示；收益是避免把 hayro 自带的（体积可观的）CMap 数据打包进二进制。这是一个「体积 vs 小众正确性」的取舍。

**练习 2**：为什么 `font_resolver` 要用 `Arc::new(闭包)` 包起来，而不是直接传闭包？

**参考答案**：因为 hayro 的 `InterpreterSettings` 字段类型要求 `font_resolver` 是 `Arc<Fn(...) -> ... + Send + Sync>`（跨线程共享的函数对象）。`Arc` 让解释器在内部按引用持有解析器、可被复制到多个线程或被多次回调，而不必每次查询都搬动闭包状态。

**练习 3**：`hayro_svg::convert` 接收的 `pdf.page()` 返回什么类型？为什么这里只传「一页」？

**参考答案**：`PdfImage::page()` 返回 `&Page<'_>`（[pdf.rs:73-75](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/pdf.rs#L73-L75)），即 PDF 文档中由 `page_index` 指定的那一页。一张「PDF 图」本质上就是某一页 PDF，所以只解释并输出那一页即可，而非整个文档。

---

### 4.3 14 种 PDF 标准字体映射（select_standard_font）

#### 4.3.1 概念说明

PDF 规范预定义了 **14 种标准字体（Base-14）**：文档可以只写字体名（如 `/Helvetica`）而不内嵌字体文件，任何合规的 PDF 阅读器都「应该」认识它们。当 hayro 解释一页 PDF、需要渲染其中文字时，遇到这些字体名就必须找到**真正的字体字节**才能画出字形。

typst-svg 自己不带这些字体文件，而是从 **typst-assets** crate 取。`select_standard_font` 就是把 hayro 的 `StandardFont` 枚举翻译成 `typst_assets::pdf::*` 常量的查表闭包。

14 种字体的结构很有规律：

- **三族衬线/无衬线/等宽**，每族 4 个变体（正、粗、斜、粗斜）= 12 个；
- 加上 **ZapfDingbats** 与 **Symbol** 两个符号字体 = 共 14 个。

#### 4.3.2 核心流程

```
select_standard_font(font: StandardFont) -> Option<(FontData, u32)>
   │
   ├─ match font {                            // 14 个分支
   │    Helvetica              → typst_assets::pdf::SANS
   │    HelveticaBold          → typst_assets::pdf::SANS_BOLD
   │    HelveticaOblique       → typst_assets::pdf::SANS_ITALIC
   │    HelveticaBoldOblique   → typst_assets::pdf::SANS_BOLD_ITALIC
   │    Courier                → typst_assets::pdf::FIXED
   │    CourierBold            → typst_assets::pdf::FIXED_BOLD
   │    CourierOblique         → typst_assets::pdf::FIXED_ITALIC
   │    CourierBoldOblique     → typst_assets::pdf::FIXED_BOLD_ITALIC
   │    TimesRoman             → typst_assets::pdf::SERIF
   │    TimesBold              → typst_assets::pdf::SERIF_BOLD
   │    TimesItalic            → typst_assets::pdf::SERIF_ITALIC
   │    TimesBoldItalic        → typst_assets::pdf::SERIF_BOLD_ITALIC
   │    ZapfDingBats           → typst_assets::pdf::DING_BATS
   │    Symbol                 → typst_assets::pdf::SYMBOL
   │  }                                         // bytes: &'static [u8]
   │
   └─ Some((Arc::new(bytes), 0))               // FontData = (Arc<字节>, 字体索引)
```

#### 4.3.3 源码精读

闭包定义与全部 14 个映射分支：

```rust
let select_standard_font = move |font: StandardFont| -> Option<(FontData, u32)> {
    let bytes = match font {
        StandardFont::Helvetica => typst_assets::pdf::SANS,
        StandardFont::HelveticaBold => typst_assets::pdf::SANS_BOLD,
        StandardFont::HelveticaOblique => typst_assets::pdf::SANS_ITALIC,
        StandardFont::HelveticaBoldOblique => typst_assets::pdf::SANS_BOLD_ITALIC,
        StandardFont::Courier => typst_assets::pdf::FIXED,
        StandardFont::CourierBold => typst_assets::pdf::FIXED_BOLD,
        StandardFont::CourierOblique => typst_assets::pdf::FIXED_ITALIC,
        StandardFont::CourierBoldOblique => typst_assets::pdf::FIXED_BOLD_ITALIC,
        StandardFont::TimesRoman => typst_assets::pdf::SERIF,
        StandardFont::TimesBold => typst_assets::pdf::SERIF_BOLD,
        StandardFont::TimesItalic => typst_assets::pdf::SERIF_ITALIC,
        StandardFont::TimesBoldItalic => typst_assets::pdf::SERIF_BOLD_ITALIC,
        StandardFont::ZapfDingBats => typst_assets::pdf::DING_BATS,
        StandardFont::Symbol => typst_assets::pdf::SYMBOL,
    };
    Some((Arc::new(bytes), 0))
};
```

[crates/typst-svg/src/image.rs:147-165](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L147-L165) —— 14 个标准字体到 typst-assets 常量的查表。

整理成对照表（便于记忆）：

| PDF 标准字体（StandardFont 变体） | typst-assets 常量 | 族/类别 |
|---|---|---|
| Helvetica / HelveticaBold / HelveticaOblique / HelveticaBoldOblique | `SANS` / `SANS_BOLD` / `SANS_ITALIC` / `SANS_BOLD_ITALIC` | 无衬线（4 变体） |
| Courier / CourierBold / CourierOblique / CourierBoldOblique | `FIXED` / `FIXED_BOLD` / `FIXED_ITALIC` / `FIXED_BOLD_ITALIC` | 等宽（4 变体） |
| TimesRoman / TimesBold / TimesItalic / TimesBoldItalic | `SERIF` / `SERIF_BOLD` / `SERIF_ITALIC` / `SERIF_BOLD_ITALIC` | 衬线（4 变体） |
| ZapfDingBats | `DING_BATS` | 符号（装饰 dingbat） |
| Symbol | `SYMBOL` | 符号（数学/希腊） |

几个值得点出的细节：

- **命名规律**：`SANS`/`FIXED`/`SERIF` 是 typst-assets 的语义化命名（无衬线/等宽/衬线），对应 PDF 的 Helvetica/Courier/Times 三族；后缀 `_BOLD`/`_ITALIC`/`_BOLD_ITALIC` 对应粗/斜/粗斜。
- **`bytes` 是 `&'static [u8]`**：typst-assets 把这些字体以静态字节常量的形式编译进二进制（`typst_assets::pdf::*`），无需运行时读文件。
- **返回值 `(FontData, u32)`**：`FontData` 由 `Arc::new(bytes)` 构造——把静态字节切片包进 `Arc`；后面的 `u32` 是「字体在集合中的索引」，固定为 `0`，因为 typst-assets 提供的每个常量都是单字体文件（不是字体集合，故索引为 0）。
- **`move` 捕获**：闭包用 `move`，但因为体里只引用 `&'static` 常量、实际不捕获任何外部变量，`move` 在此处无实质作用，更多是防御性写法。这个闭包随后被 `font_resolver` 的外层闭包捕获并搬进 `Arc`。

> 这个闭包同时服务于 `font_resolver` 的两条查询路径（[image.rs:168-171](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L168-L171)）：`Standard(s)` 直接查；`Fallback(f)` 先调 `f.pick_standard_font()` 选一个最接近的标准字体、再查同一个表。也就是说——**typst-svg 对 PDF 里任何非标准字体，也用这 14 个字体兜底**。

#### 4.3.4 代码实践

**实践目标**：把 14 种 PDF 标准字体到 typst-assets 常量的映射整理成表，并验证其「3×4+2」结构。

**操作步骤**：

1. 打开 [image.rs:147-165](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L147-L165)。
2. 按「字体族」分组，把每个 `StandardFont::*` 变体与对应的 `typst_assets::pdf::*` 常量填入一张三列表（PDF 名 / assets 常量 / 族）。
3. 数一下：三族各 4 变体 = 12，加上 ZapfDingBats、Symbol = 14，验证与「14 种 PDF 标准字体」一致。
4. 进阶：对照 typst-render 的 `build_pdf_texture`（`crates/typst-render/src/image.rs` L117-L135 附近），确认两处的映射表**逐行相同**——这正是 4.4 节「保持同步」的对象。

**需要观察的现象**：

- 两处代码的 `match` 分支顺序、常量名完全一致；唯一差别是外层函数名（`pdf_to_svg` vs `build_pdf_texture`）与最后的输出后端。

**预期结果**：得到一张 14 行的字体映射表；并能指出「Helvetica→SANS、Courier→FIXED、Times→SERIF」这条族名翻译规律。

> 说明：`typst_assets::pdf` 模块的具体字节常量定义位于 typst-assets crate（外部仓库），不在本工作目录内；本实践以读 `image.rs` 中的引用为准，不臆造 assets 内部实现。

#### 4.3.5 小练习与答案

**练习 1**：PDF 里点名了 `/TimesBold`，最终 typst-svg 用的是哪个字体字节？

**参考答案**：hayro 回调 `font_resolver(FontQuery::Standard(StandardFont::TimesBold))`，落到 `select_standard_font(StandardFont::TimesBold)`，命中 [image.rs:158](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L158) 的 `typst_assets::pdf::SERIF_BOLD`，即 typst-assets 内置的衬线粗体字体字节。

**练习 2**：若一页 PDF 用了一种既不在 14 种标准字体里、也没有内嵌字体文件的字体，typst-svg 会怎么处理？

**参考答案**：hayro 走 `FontQuery::Fallback(f)` 路径（[image.rs:170](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L170)），调用 `f.pick_standard_font()` 从 14 种里挑一个「最接近」的（例如按是衬线/等宽等特征选），再用它兜底渲染。结果是该文字「能显示出来，但字形与原作者意图可能不同」。

**练习 3**：返回元组里的 `u32`（此处固定 `0`）代表什么？为什么是 `0`？

**参考答案**：它是「字体在字体集合（font collection）文件里的索引」。一个 `.ttc` 这样的集合文件可含多个字体，需用索引选择其中之一。typst-assets 提供的每个标准字体常量都是**单字体文件**，故索引恒为 `0`。

---

### 4.4 跨 crate 同步约定：「Keep this in sync」

#### 4.4.1 概念说明

注意 `pdf_to_svg` 上方那行注释：

```rust
// Keep this in sync with `typst-png`!
```

这是一条**手工维护约定**：在仓库的另一个 crate 里，存在一段与 `pdf_to_svg` 几乎完全相同的代码。typst 维护者用这行注释提醒后来者——**改了这边，别忘了改那边**。

本仓库当前快照中，这段孪生代码可定位在 **typst-render**（负责位图/PNG 渲染的后端）的 `build_pdf_texture` 函数：`crates/typst-render/src/image.rs:116`，其上方有对称的注释 `// Keep this in sync with typst-svg!`。两者共享同一套 hayro 配置，区别只在「输出后端」：

| | typst-svg | typst-render |
|---|---|---|
| 函数名 | `pdf_to_svg` | `build_pdf_texture` |
| 输出 | SVG 字符串（矢量） | 位图 `Pixmap`（栅格） |
| hayro 调用 | `hayro_svg::convert(...)` | `hayro::render(...)` |
| 渲染设置 | `SvgRenderSettings { bg_color: [0,0,0,0] }` | `RenderSettings { x_scale, y_scale, width, height, bg_color: TRANSPARENT }` |
| `select_standard_font` 闭包 | **逐行相同** | **逐行相同** |
| `InterpreterSettings` 四字段 | **逐行相同** | **逐行相同**（typst-render 多几行解释性注释） |

#### 4.4.2 核心流程

为什么会有这种「复制 + 注释提醒」的约定？它的利与弊：

```
                ┌──────── PDF 图渲染需求 ────────┐
                │   （需 hayro + 14 标准字体）    │
                └───────────────┬────────────────┘
                                │
              ┌─────────────────┴─────────────────┐
              ▼                                   ▼
       typst-svg                           typst-render
   （矢量 SVG 输出）                      （位图 PNG 输出）
   pdf_to_svg()                          build_pdf_texture()
              │                                   │
              └──► hayro_svg::convert ◄──── hayro::render
                    （共享 hayro 解释器 + 字体配置，仅后端不同）
```

- **利**：两个 crate 独立演进，各自只依赖自己需要的后端（typst-svg 依赖 `hayro-svg`，typst-render 依赖栅格后端），不必为共用而引入额外抽象层。
- **弊**：字体映射表与 `InterpreterSettings` 配置被复制了两份。若新增了第 15 种标准字体、或改了某个配置项，**必须手动同步两处**，否则两边行为发散。唯一的「护栏」就是那一行 `// Keep this in sync` 注释——没有编译期保证。

#### 4.4.3 源码精读

typst-svg 侧的注释：

```rust
// Keep this in sync with `typst-png`!
fn pdf_to_svg(pdf: &PdfImage) -> String {
```

[crates/typst-svg/src/image.rs:145-146](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L145-L146)

typst-render 侧的对称注释与孪生函数：

```rust
// Keep this in sync with `typst-svg`!
fn build_pdf_texture(pdf: &PdfImage, w: u32, h: u32) -> Option<sk::Pixmap> {
    let select_standard_font = move |font: StandardFont| -> Option<(FontData, u32)> {
        let bytes = match font {
            StandardFont::Helvetica => typst_assets::pdf::SANS,
            // ... 与 typst-svg 逐行相同 ...
```

[crates/typst-render/src/image.rs:115-117](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/image.rs#L115-L117) —— 对称的「保持同步」注释与同名同结构的字体闭包。

> 注释字面写的是 `typst-png`，而本快照中可定位到的孪生实现位于 `typst-render`（typst-render 即承担 PNG/位图导出的后端）。无论命名如何，约定的本质是：**「矢量导出」与「位图导出」两条 PDF 图渲染路径，必须共用同一套 hayro 配置与字体映射**，否则同一份文档导出 SVG 和导出 PNG 会出现不一致（例如一边显示了某字体、另一边没有）。

值得一提的是，typst-render 的版本多了两条**解释性注释**（typst-svg 这边没有）：

- 在 `cmap_resolver` 上方：*"Fairly niche and enabling hayro's embedded cmap would add a considerable amount of data."*
- 在 `render_annotations: false` 上方：*"We want to render like it prints, so no annotations."*

这给了我们一个读懂 typst-svg 这边那几个「光秃秃」配置项的线索——它们的「为什么」写在孪生代码里。这也是「保持同步」约定的额外价值：**两处互为对方的文档**。

#### 4.4.4 代码实践

**实践目标**：亲自动手比对两处孪生代码，验证「逐行相同」并定位唯一差异。

**操作步骤**：

1. 读 typst-svg 的 [image.rs:145-184](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L145-L184)。
2. 读 typst-render 的 `build_pdf_texture`（`crates/typst-render/src/image.rs` L115-L164 附近）。
3. 逐行比对：
   - `select_standard_font` 的 14 个 match 分支是否完全一致？
   - `InterpreterSettings` 的四个字段值是否完全一致？
   - 唯一差异是否集中在「最后的 hayro 调用」与「渲染设置结构」？
4. 用 `git log` 查这两个文件的历史（例如 `git log --oneline -- crates/typst-svg/src/image.rs crates/typst-render/src/image.rs`），看是否有「同时改两处」的提交。

**需要观察的现象**：

- 字体映射表与解释器配置字节级一致；差异只在输出后端（`hayro_svg::convert` + `SvgRenderSettings` vs `hayro::render` + `RenderSettings`）。
- 历史提交里应能看到针对「PDF 图渲染」的成对改动（例如引入该特性的提交 `ffa70471d` "Turn PDF images into SVGs in HTML and SVG export (#7043)"，以及后续针对 PDF/SVG 标准的联合升级 `53456331e` "Bump PDF and SVG cinematic universe ..."）。

**预期结果**：能用一句话总结——「两份代码共享 hayro 解释器配置、只换输出后端，靠一行注释人工同步」。

> 说明：`git log` 的具体提交列表以本地仓库为准；上方给出的提交哈希来自本快照，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 typst 团队选择「复制 + 注释」而不是把配置抽成一个共享函数？

**参考答案**：两个 crate 的输出后端不同（一个调 `hayro_svg::convert`、一个调 `hayro::render`），且各自只依赖对应后端；若抽公共函数，要么放进其中某个 crate 让另一个反向依赖、要么新建一个极薄的共享 crate。在「公共部分就十几行、且很少改动」的情况下，复制 + 注释提醒是更轻量的工程选择，代价是依赖人工同步。

**练习 2**：假设维护者只在 typst-svg 这边把 `render_annotations` 改成了 `true`、忘了同步 typst-render，会出现什么问题？

**参考答案**：同一份含 PDF 图的文档，导出 SVG 时会画出 PDF 注释（链接/批注等），导出 PNG 时则不画。两种输出在视觉与可点击区域上出现不一致——这违背了「各导出格式应表现一致」的预期。这正是「保持同步」约定要防止的失败模式。

**练习 3**：typst-svg 的 `pdf_to_svg` 没有解释 `cmap_resolver: None` 的原因，但 typst-render 的孪生代码里有注释。这说明了「保持同步」约定的一个什么副作用？

**参考答案**：两处孪生代码互为「设计意图的备份文档」。typst-svg 缺少的解释，可以在 typst-render 那边读到——前提是维护者知道「保持同步」这层关系。这也提示我们读源码时，遇到带此类注释的函数，应主动去对照另一处，往往能补全缺失的「为什么」。

---

## 5. 综合实践

把本讲的知识串起来，完成下面这个「全链路追踪」任务：

**任务**：一张 `image("logo.pdf")`（单页、用 Helvetica 写了几个字）的 PDF 图，从被 Typst 解析到最终出现在导出的 SVG 里，经历了哪些步骤？请按顺序写出涉及的关键函数与 crate，并标注每一步「输入 → 输出」。

**建议作答骨架**（先自己写，再对照）：

1. typst-library 把文件解析成 `Image { kind: ImageKind::Pdf(PdfImage) }`；`PdfImage` 持有 `PdfDocument` + `page_index`（[pdf.rs:47-55](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/pdf.rs#L47-L55)）。
2. typst-svg 的 `WebImage::new(image)` 命中 Pdf 分支（[image.rs:126-128](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L126-L128)）。
3. `pdf_to_svg(pdf)` 被调（[image.rs:146](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L146)）：内部 `select_standard_font` 把 `Helvetica` 映射到 `typst_assets::pdf::SANS`（[image.rs:149](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L149)）；`InterpreterSettings` 配好四字段（[image.rs:167-175](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L167-L175)）。
4. `hayro_svg::convert(pdf.page(), ...)` 解释 PDF 页、用 SANS 字体画文字、输出 SVG 字符串（[image.rs:178-183](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L178-L183)）。
5. 返回的 `String` 经 `Bytes::from_string` 包成 `WebImage { format: Svg, data }`（[image.rs:127](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L127)）。
6. `to_base64_url()` 把它编成 `data:image/svg+xml;base64,...`（[image.rs:137-142](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L137-L142)）。
7. `render_image` 写出 `<image xlink:href="data:image/svg+xml;base64,..."/>`（[image.rs:27-42](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L27-L42)）。

**进阶**：在上面的链路中指出，typst-render 的 `build_pdf_texture` 在哪一步「分叉」（答：第 4 步——它改调 `hayro::render` 输出位图，而非 `hayro_svg::convert` 输出 SVG），并据此说明为什么两边要「Keep this in sync」。

## 6. 本讲小结

- typst-svg 对嵌入的 PDF 图选择**矢量重解释**而非栅格化：用 **hayro** 解释 PDF 内容流，用 **hayro_svg** 把解释结果写成 SVG 字符串，以保真、可缩放、省体积。
- 接入点是 `WebImage::new` 的 Pdf 分支（[image.rs:126-128](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L126-L128)）：调 `pdf_to_svg`，把返回的 SVG 标为 `WebImageFormat::Svg`，之后复用 SVG 图的 base64 内联与 `<image>` 写出机制——PDF 在此被「抹平」成普通 SVG。整个 `new` 被 `comemo::memoize`，同一 PDF 图只转一次。
- `pdf_to_svg`（[image.rs:145-184](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L145-L184)）三段式：`select_standard_font` 字体闭包 → `InterpreterSettings` 四字段配置 → `hayro_svg::convert` 调用。
- `InterpreterSettings` 四字段表达的产品取向：标准/兜底字体都来自 14 种 PDF 标准字体（typst-assets）；CMap 不启用（省体积）；警告静默；不渲染注释（要「打印效果」）；`bg_color: [0,0,0,0]` 完全透明，避免遮挡下层内容。
- 14 种标准字体呈「三族（Helvetica/Courier/Times）× 四变体（正/粗/斜/粗斜）+ ZapfDingBats + Symbol」结构，逐个映射到 `typst_assets::pdf::{SANS/FIXED/SERIF}{,_BOLD,_ITALIC,_BOLD_ITALIC}` 与 `DING_BATS`/`SYMBOL`。
- `// Keep this in sync with typst-png!` 是跨 crate 手工同步约定：typst-render 的 `build_pdf_texture` 与本函数共享同一套 hayro 配置与字体表，只换输出后端（SVG vs 位图）；唯一的同步护栏是这行注释，两处还互为对方缺失「为什么」的文档。

## 7. 下一步学习建议

- **横向对照位图后端**：读 typst-render 的 `build_pdf_texture`（`crates/typst-render/src/image.rs`），看 `hayro::render` 与 `hayro_svg::convert` 的差异，体会「同一解释器、双后端」的设计。
- **回到去重与集成主线**：本讲是 u6 的图像子主题收尾，接下来建议读 **u6-l3（去重机制 Deduplicator 与 ID 编码）**，对比「图像去重靠 `comemo::memoize`（本讲的 `WebImage::new`）」与「字形/渐变去重靠 `Deduplicator`」两条不同的去重路线。
- **收束到集成**：之后读 **u6-l4（链接、锚点与 HTML/Bundle 集成）**，看 `finalize` 如何把所有 `<defs>` 统一写出，以及 `svg_in_html`/`svg_in_bundle` 如何处理含 PDF 图（从而含内嵌 SVG data URL）的复杂文档。
- **深入 hayro（可选）**：若想理解 PDF 解释器内部，可阅读 hayro / hayro_svg crate 的文档与源码（外部仓库），重点看 `InterpreterSettings`、`FontQuery`、`StandardFont::pick_standard_font` 的定义。
