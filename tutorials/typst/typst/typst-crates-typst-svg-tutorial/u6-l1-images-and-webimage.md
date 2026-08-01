# 图像渲染与 WebImage

## 1. 本讲目标

typst-svg 把一页排版结果翻译成一段 SVG 字符串，其中「图片」是除文字、矢量形状之外最常见的对象。本讲聚焦 `src/image.rs`，回答三个问题：

1. 一张 Typst 内部的 `Image` 如何变成可以塞进 SVG 的东西？
2. 它最终以什么样的 SVG 元素和属性输出？
3. 浏览器查看时，图片的缩放质量由谁决定？

学完后你应当能够：

- 说清 `WebImage` / `WebImageFormat` 这一层「Web 归一化」的存在意义，以及 `WebImage::new` 对 `Raster`(Exchange/Pixel) / `Svg` / `Pdf` 四种 `ImageKind` 的不同处理。
- 看懂 `render_image` 写出的 `<image>` 元素：`xlink:href`（data URL）、`width`/`height`（pt）、`preserveAspectRatio="none"`、可选的 `image-rendering` 各自起什么作用。
- 理解 `to_base64_url` 如何把任意 `WebImage` 编码成 `data:{mime};base64,...` 内联 URL，以及 base64 对体积的影响。
- 掌握 `convert_image_scaling` 把 `Smart<ImageScaling>`（Auto/Smooth/Pixelated）映射到 CSS `image-rendering` 值的规则。

## 2. 前置知识

本讲假设你已经读过 **u2-l1（SVGRenderer 与渲染状态 State）** 和 **u5-l1（颜色序列化）**。下面补充几个本讲会用到的、与本 crate 无关的基础概念。

- **SVG `<image>` 元素与 `href`**：SVG 用 `<image>` 元素引用一张外部或内联的图片。引用地址写在 `href`（SVG 2）或更老的 `xlink:href`（SVG 1.1）属性里。typst-svg 出于兼容性使用 `xlink:href`。这个地址可以是普通 URL，也可以是下文的 data URL。
- **data URL**：形如 `data:{mime};base64,{数据}` 的字符串，把二进制内容直接编码进 URL 本身，浏览器无需再次请求文件即可解码显示。例如 `data:image/png;base64,iVBORw0KG...`。它的代价是体积膨胀约 4/3 倍（见 4.3）。
- **base64**：把任意字节流映射成 64 个可打印 ASCII 字符（`A-Za-z0-9+/`，末尾用 `=` 补齐）的编码，目的是让二进制数据能安全地放进文本协议（如 URL、XML 属性）。
- **ICC 配置文件（ICC profile）**：描述颜色设备色彩空间（如 sRGB、Display P3）的元数据。一张图片可以内嵌 ICC profile，让查看器做色彩管理、还原真实颜色。本讲里你会看到 Pixel 格式重编码 PNG 时如何把 ICC profile 一并写入。
- **`image` crate 的 `DynamicImage` 与 `PngEncoder`**：`image` 是 Rust 生态的图像处理库。`DynamicImage` 是解码后的、像素已展开的图像；`PngEncoder` 负责把像素重新编码成 PNG 字节流。typst-library 的 `RasterImage` 暴露了 `dynamic()`、`icc()`、`data()` 等方法供本 crate 使用。
- **`Smart<T>`**：Typst 里表示「自动或显式指定」的通用包装。`Smart::Auto` 表示「交由系统默认」，`Smart::Custom(v)` 表示「用户明确给了 v」。本讲的缩放策略就用它表达：`Smart<ImageScaling>`。

另外提醒两个本 crate 的既有概念（详见前置讲义）：`State`（u2-l1）携带累积 `transform` 与当前 `size`；`SvgElem`（u2-l3）是基于 `XmlWriter` 的 RAII 元素包装，作用域结束即自动闭合标签；`SvgTransform` / `SvgDisplay`（u5-l1/u2-l3）是把 typst 内部类型临时套上「可写成 SVG 属性」语义的适配器。

## 3. 本讲源码地图

本讲只涉及一个文件，但它牵出两条上游调用链与一组 typst-library 类型。

| 文件 | 作用 |
| --- | --- |
| `src/image.rs` | 图像渲染的全部逻辑：`render_image`、`WebImage`、`WebImageFormat`、`WebImage::new`、`to_base64_url`、`convert_image_scaling`，以及私有函数 `pdf_to_svg`（PDF 图转 SVG，详情见 u6-l2）。 |

相关引用点（仅供定位，本讲不展开）：

- `src/lib.rs` 的 `render_frame`：页面正文里遇到 `FrameItem::Image` 时调用 `render_image`。
- `src/text.rs` 的 `write_glyph_defs`：把彩色/位图字形当图片处理时，也复用 `render_image`。
- typst-library 的 `visualize::image`：定义 `Image` / `ImageKind` / `RasterImage` / `RasterFormat` / `ExchangeFormat` / `ImageScaling` 等输入类型。

## 4. 核心概念与源码讲解

本讲按「先准备数据，再输出元素」的顺序拆成 5 个最小模块：`WebImage` 与 `WebImageFormat`（数据载体）→ `WebImage::new`（归一化）→ `to_base64_url`（编码成 data URL）→ `render_image`（写出 `<image>`）→ `convert_image_scaling`（缩放质量映射）。

### 4.1 WebImage 与 WebImageFormat：图像的「Web 归一化」表示

#### 4.1.1 概念说明

Typst 内部能处理多种图像来源（`ImageKind`）：光栅图 `Raster`（又分「交换格式 Exchange」与「原始像素 Pixel」）、矢量 `Svg`、以及嵌入的 `Pdf` 图。但 Web 平台（SVG 文件、HTML 页面里的 `<img>`）只认少数几种格式。如果直接把内部表示塞进 SVG，会出现两类问题：

- **原始像素**没有文件编码，浏览器无法直接识别；
- **PDF 图**不是 Web 图像格式，浏览器不认。

于是 typst-svg 设计了一个中间类型 `WebImage`，它只关心「已经准备好可以塞进 Web 的一份数据」：一个格式标签 `WebImageFormat` + 一段字节 `data`。所有「怎么从内部表示得到这份数据」的复杂逻辑都收拢在 `WebImage::new`（4.2）里，`WebImage` 本身只是结果容器。

#### 4.1.2 核心流程

```text
Image (typst-library)
   │  WebImage::new(...)
   ▼
WebImage { format: WebImageFormat, data: Bytes }
   │  .to_base64_url()
   ▼
data:{mime};base64,{...}   ← 最终塞进 <image xlink:href="...">
```

`WebImageFormat` 枚举覆盖 Web 支持的 5 种格式，并为每种提供 MIME 类型与文件扩展名，前者用于拼 data URL，后者（在 typst-html 等场景）用于落盘命名。

#### 4.1.3 源码精读

`WebImage` 是一个非常薄的、可 `Hash`/`Clone` 的结构体，只有两个字段：

[src/image.rs:58-63](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L58-L63) —— `WebImage` 持有一个 `WebImageFormat` 标签与一段 `Bytes` 数据。派生 `Hash` 是为了配合 `#[comemo::memoize]`（见 4.2/4.3）。

`WebImageFormat` 用 `#[non_exhaustive]` 标注，意为将来可能新增格式而不破坏下游匹配；它只列出 Web 真正支持的 5 种：

[src/image.rs:65-74](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L65-L74) —— `Png / Jpg / Gif / Webp / Svg`。注意没有 PDF：PDF 会在 `new` 里被转成 SVG。

`mime()` 给出拼 data URL 必需的 MIME 类型（`image.rs:76-86` 与 `extension()` 在 `image.rs:88-97`）：

[src/image.rs:76-86](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L76-L86) —— 例如 `Png → "image/png"`、`Svg → "image/svg+xml"`。`mime()` 是 `to_base64_url` 的输入。

#### 4.1.4 代码实践

**实践目标**：体会「内部多来源」与「Web 少格式」之间的归一化必要性。

**操作步骤**：

1. 打开 typst-library 的 `ImageKind` 定义 [visualize/image/mod.rs:527-534](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L527-L534)，数一下 Typst 内部有几种 `ImageKind`。
2. 对照本讲的 `WebImageFormat`（5 种），指出哪一种内部来源在 `WebImageFormat` 里**找不到对应项**（答案：PDF，它会被转成 `Svg`）。

**需要观察的现象 / 预期结果**：内部 `ImageKind` 有 `Raster / Svg / Pdf` 三大类（`Raster` 内再分 `Exchange`/`Pixel`），而 `WebImageFormat` 没有为 PDF 保留枚举项——这就是「归一化」要抹平的鸿沟。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `WebImage` 要派生 `Hash`？

> **答案**：因为 `WebImage::new` 和 `to_base64_url` 都标注了 `#[comemo::memoize]`，comemo 需要按参数的哈希值缓存结果；返回值类型与参数类型都需要可哈希。

**练习 2**：`WebImageFormat::extension()` 在 typst-svg 自身（生成 SVG 字符串）里会被用到吗？

> **答案**：不会。typst-svg 生成内联 SVG 时只用 `mime()` 拼 data URL。`extension()` 是给 typst-html 等需要把图片**单独落盘**的场景用的（这也是 `WebImage` 被设为 `pub`、被 typst-html 复用的原因，见 u1-l2）。

### 4.2 WebImage::new：三种 ImageKind 的格式归一化

#### 4.2.1 概念说明

`WebImage::new(image)` 是归一化的核心。它接收一个 typst-library 的 `&Image`，按 `image.kind()` 的变体走不同分支，最终产出一个 `WebImage`。四个分支的「工作量」差异巨大：有的几乎零成本（直接克隆原始字节），有的要重新编码像素，有的甚至要调用外部库做格式转换。

关键设计：整个函数被 `#[comemo::memoize]` 包裹。由于 `Image` 本身可哈希，同一张图片在一份文档里被引用多次（比如重复贴同一张图、或图像字形反复出现）时，归一化只会执行一次，之后全部命中缓存。这把「重编码/转格式」的昂贵代价摊销掉了。

#### 4.2.2 核心流程

```text
match image.kind()
├─ Raster(Exchange(fmt))      → 直接保留 fmt 与原始字节 data      （零成本）
├─ Raster(Pixel(_))           → 用 PngEncoder 把像素重新编码成 PNG （中等成本，见下）
├─ Svg(svg)                   → 直接保留 svg 字节，标为 Svg        （零成本）
└─ Pdf(pdf)                   → pdf_to_svg(pdf) 经 hayro 转成 SVG  （高成本，u6-l2 详讲）
```

其中 Pixel 分支的「重编码」值得单独画出：

```text
PngEncoder::new(&mut buf)
   │  若有 icc_profile：encoder.set_icc_profile(...)
   ▼
raster.dynamic().write_with_encoder(encoder)
   │  （把已解码的像素流式写入 PNG 容器）
   ▼
Bytes::new(buf)   ← 得到 PNG 字节
```

#### 4.2.3 源码精读

整个 `new` 是一个大 `match`，先按 `ImageKind`、再按光栅子格式分发：

[src/image.rs:100-131](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L100-L131) —— 上面流程图对应的真实代码。注意 `#[comemo::memoize]` 在第 102 行。

**分支一：Raster::Exchange**（`image.rs:105-114`）—— 把 `ExchangeFormat`（Png/Jpg/Gif/Webp）一一映射到 `WebImageFormat`，数据直接 `raster.data().clone()`，不触碰像素、不重编码。这是最高效的「透传」路径。

**分支二：Raster::Pixel**（`image.rs:115-123`）—— 本讲的重点实践对象。原始像素没有文件编码，必须现编一个：

[src/image.rs:115-123](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L115-L123) —— 逐一拆解：①新建一个写到 `Vec<u8>` 的 `PngEncoder`；②若图片带 ICC 配置文件（`raster.icc()`），调用 `set_icc_profile(...)`（用 `.ok()` 忽略失败——ICC 写入失败不致命，宁可丢色彩管理也不要中断导出）；③`raster.dynamic().write_with_encoder(encoder)` 让已解码的 `DynamicImage` 把像素按 PNG 规则流式编码进容器；④把字节包成 `Bytes`。

**分支三：Svg**（`image.rs:125`）—— 矢量 SVG 直接克隆字节，标为 `WebImageFormat::Svg`。

**分支四：Pdf**（`image.rs:126-128`）—— 调私有函数 `pdf_to_svg(pdf)`（`image.rs:146-184`）用 hayro 把 PDF 图转成 SVG 字符串，再标为 `WebImageFormat::Svg`。这条路径的细节是 u6-l2 的主题，本讲只需知道「PDF 在此处被转成 SVG」。

#### 4.2.4 代码实践（本讲主实践之一）

**实践目标**：追踪一张 Pixel 格式光栅图如何经 `PngEncoder`（含 ICC profile）重编码为 PNG 并最终内联。

**操作步骤**：

1. 在 [src/image.rs:115-123](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L115-L123) 中，按顺序列出 Pixel 分支用到的三个 `RasterImage` 方法：`icc()`、`dynamic()`，以及 `data()`（注意本分支**没有**用 `data()`，为什么？）。
2. 解释 `set_icc_profile(...).ok()` 为什么要忽略 `Result`：对照「ICC profile 写入失败」与「整个导出失败」两种后果。
3. 顺着 `dynamic().write_with_encoder(encoder)` 进入 typst-library 的 `RasterImage::dynamic` 定义 [visualize/image/raster.rs:189-192](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/raster.rs#L189-L192)，确认它返回的是已解码的 `Arc<DynamicImage>`——即像素已经被展开，本分支只是给像素重新套一层 PNG 文件容器。

**需要观察的现象 / 预期结果**：Pixel 分支不调用 `data()`，因为原始 `data()` 对像素格式而言就是裸像素、不能直接当文件用；它必须经 `dynamic()`（解码后的像素）+ `write_with_encoder` 重新封装成合法 PNG。ICC profile 被尽量保留以支持色彩管理，但失败时降级而非报错。

**说明**：本实践为「源码阅读型」，不要求运行；若想看真实输出，可用 typst CLI 编译一张含 ICC 的 PNG 图片的文档为 SVG，再在产物里搜索 `data:image/png;base64,`（具体字节待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：Exchange 分支为什么不调用 `dynamic()`？

> **答案**：Exchange 格式（Png/Jpg/Gif/Webp）本身就是合法的、浏览器可识别的文件编码，直接 `data().clone()` 透传原始字节即可，无需解码再重编码。重编码既浪费算力还可能引入质量损失（尤其 Jpg）。

**练习 2**：`#[comemo::memoize]` 加在 `new` 上，对「同一张图在文档里出现 100 次」的场景有什么收益？

> **答案**：归一化（尤其 Pixel 的 PNG 重编码、Pdf 的 hayro 转换）只执行 1 次，其余 99 次命中缓存，直接返回同一个 `WebImage`。后续 `to_base64_url` 也被 memoize，base64 编码同样只做 1 次。

### 4.3 to_base64_url：把图像内联成 data URL

#### 4.3.1 概念说明

`WebImage` 拿到「格式 + 字节」后，还不能直接写进 SVG 属性——`<image>` 的 `href` 需要一个 URL。typst-svg 选择把图片**内联**成 data URL（`data:{mime};base64,...`），让产物 SVG 成为一个自包含文件，不依赖任何外部图片文件。

`to_base64_url` 就是负责这一步的方法。它同样被 `#[comemo::memoize]`，因为 base64 编码不便宜，且对同一 `WebImage` 结果恒定。

#### 4.3.2 核心流程

```text
self.format.mime()            // 例如 "image/png"
   │  eco_format!("data:{};base64,", mime)
   ▼
"data:image/png;base64,"      // URL 前缀
   │  base64 STANDARD 编码 self.data
   ▼
push_str(编码结果)            // 拼成完整 data URL
```

base64 把每 3 字节编成 4 个字符，故数据体积会膨胀约 4/3：

\[
\text{base64 字符数} = \lceil n/3 \rceil \times 4 \approx \tfrac{4}{3}n
\]

这就是为什么内联大量图片会让 SVG 文件明显变大——这是「自包含」换「体积」的取舍。

#### 4.3.3 源码精读

[src/image.rs:133-142](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L133-L142) —— 三步：用 `eco_format!` 拼前缀（`eco_format!` 比 `format!` 更省分配，返回 `EcoString`）；用 `base64` crate 的 `general_purpose::STANDARD` 引擎编码 `self.data`；把编码结果 `push_str` 到前缀之后。返回 `EcoString`。

注意它返回的是**拥有所有权的 `EcoString`**，而非借用——因为产物是新构造的字符串，且会被 memoize 缓存。

#### 4.3.4 代码实践

**实践目标**：手工推演一张极小图片的 data URL 形态。

**操作步骤**：

1. 假设一张 `WebImageFormat::Png` 的图片，其 `data` 是 6 字节 `b"ABCDEF"`。先用上面的公式估算 base64 字符数：\( \lceil 6/3\rceil \times 4 = 8 \) 个字符。
2. 写出最终 URL 的**前缀与结构**：`data:image/png;base64,<8 个 base64 字符>`，其中 MIME 来自 4.1.3 的 `mime()`。

**需要观察的现象 / 预期结果**：你能确认 URL 形式为 `data:image/png;base64,...`，并理解 4/3 膨胀率。具体 8 个字符的实际值（`QUJDREVG`）待本地用 base64 工具验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `to_base64_url` 用 `EcoString` 而不是 `String`？

> **答案**：`EcoString` 是 ecow 提供的、对小字符串与克隆更友好的字符串类型（短串内联、克隆廉价），typst 全家桶普遍用它；返回 `EcoString` 与 crate 其它部分（如 `SvgUrl` 适配、`attr` 写入）一致。

**练习 2**：若同一 `WebImage` 调两次 `to_base64_url()`，base64 会被编码两次吗？

> **答案**：不会。`#[comemo::memoize]` 会按 `&self` 的哈希缓存返回值，第二次直接返回同一个克隆的 `EcoString`。

### 4.4 render_image：写出 `<image>` 元素

#### 4.4.1 概念说明

前面三节都在「准备数据」。`render_image` 才是真正往 SVG 输出流里写元素的方法——它把前面准备的 data URL、布局尺寸、变换、缩放策略组装成一个 `<image>` 元素。

它是 `impl SVGRenderer` 的方法（`image.rs:18`），但在本文件里只定义了这一个方法。和其它主题文件一样，它通过 `pub(super)` 仅对 crate 内部可见，由编排层（`render_frame` / `write_glyph_defs`）调用。

值得强调的是 `render_image` 有**两个调用点**：

1. 页面正文里的图片：`render_frame` 遇到 `FrameItem::Image` 时（[src/lib.rs:317-319](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L317-L319)）。
2. 图像字形：`write_glyph_defs` 处理彩色/位图字形时（[src/text.rs:205-207](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L205-L207)），把字形当成小图片渲染进 `<symbol>`（见 u4-l2）。

也就是说，同一个 `render_image` 既画「用户插入的图片」，也画「字体里的彩色字形」。

#### 4.4.2 核心流程

```text
render_image(svg, state, image, size)
   │  url = WebImage::new(image).to_base64_url()
   ▼
svg.elem("image")                       // 开 <image>（RAII，作用域结束自动闭合）
   │  if state.transform 非单位矩阵：
   │     attr("transform", SvgTransform(state.transform))
   │  attr("xlink:href", url)
   │  attr("width",  size.x.to_pt())
   │  attr("height", size.y.to_pt())
   │  attr("preserveAspectRatio", "none")
   │  if let Some(value) = convert_image_scaling(...):
   │     attr_with("style", 写 "image-rendering: <value>")
   ▼
drop → </image>
```

#### 4.4.3 源码精读

[src/image.rs:18-43](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L18-L43) —— 整个 `render_image`。逐行：

- **第 27 行**：`WebImage::new(image).to_base64_url()` 把 4.2 与 4.3 串起来，一次性拿到 data URL。两个调用都被 memoize，实际只有首次真正干活。
- **第 28 行**：`svg.elem("image")` 创建子元素，返回一个新的 `SvgElem`，它持有父 `xml` 的可变借用。这就是 u2-l3 讲过的 RAII 模型。
- **第 29-31 行**：仅当 `state.transform` 非单位矩阵时才写 `transform`。`SvgTransform(...)` 是把 typst 的 `Transform` 临时套上 `SvgDisplay` 语义的适配器（u2-l3），它会挑选最短的写法（scale/translate/matrix）以压缩体积。`state` 即 u2-l1 的渲染状态，其 `transform` 是从页面根一路 `pre_concat`/`pre_translate` 累积下来的。
- **第 32 行**：`xlink:href` 写 data URL（用老命名空间以保证兼容性）。
- **第 33-34 行**：`width`/`height` 取自参数 `size: &Axes<Abs>`，单位是 **pt**（`.to_pt()`）。`size` 由排版层算好，是这块图片在页面上占据的盒子尺寸。
- **第 35 行**：`preserveAspectRatio="none"`——见下方实践的重点解释。
- **第 36-41 行**：若 `convert_image_scaling` 返回 `Some`，用 `attr_with` 直接拼一段 `style="image-rendering: <value>"`。`attr_with` 接收一个对 `SvgFormatter` 的闭包（u2-l3），适合这种需要手写多段文本的属性。

#### 4.4.4 代码实践（本讲主实践之二）

**实践目标**：解释 `preserveAspectRatio="none"` 与 `width`/`height`（pt）如何共同控制图像在 SVG 里的缩放。

**操作步骤**：

1. 在 [src/image.rs:33-35](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L33-L35) 确认 `width`/`height` 都来自 `size.to_pt()`，且 `preserveAspectRatio` 硬编码为 `"none"`。
2. 回忆 SVG 语义：`<image>` 默认会保持图片的宽高比（等比缩放、必要时留白）。`preserveAspectRatio="none"` 表示**忽略**图片自身宽高比，强行拉伸到 `width × height` 盒子。
3. 思考：为什么 typst-svg 敢用 `"none"`？因为 Typst 排版层已经决定了这块图片在页面上的确切尺寸（`size`，通常是按用户指定的 width/height 或图片原始宽高比算出来的盒子）。SVG 里的 `width/height` 与外层 SVG 的 `viewBox`（同样是 pt，见 u1-l3）使用同一套坐标系，所以图片会被精确放进排版预留的盒子里。若这里不用 `"none"`，浏览器可能二次干预宽高比，导致与排版结果不一致。

**需要观察的现象 / 预期结果**：

- 若 `size.x` 与 `size.y` 的比例和图片原始比例不同，`preserveAspectRatio="none"` 会让图片**非等比拉伸**以填满盒子——这正是排版层想要的（用户若手动指定了变形的 width/height，就应当变形）。
- `width/height` 用 pt，与 SVG 根的 viewBox 单位一致，保证 1:1 对齐，不会出现「图片在 SVG 里偏移或缩放错位」。

**说明**：想看真实效果，可用 typst CLI 编译一个含图片的 `.typ` 为 SVG，在产物里找到 `<image ... width="..." height="..." preserveAspectRatio="none" .../>`，核对 width/height 数值与排版尺寸是否一致（具体数值待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`render_image` 为什么用 `svg.elem("image")` 而不是 `svg.lazy_elem("image")`？

> **答案**：图片一定会输出一个实在的 `<image>` 元素，不存在「可能为空、需要惰性创建」的情况，所以直接用 `elem`（立即开标签）。`lazy_elem`（u2-l3）用于不确定是否需要该层 `<g>` 的场景（如可能为空的分组）。

**练习 2**：第 29 行的 `state.transform.is_identity()` 判断省掉会怎样？

> **答案**：会多写一个 `transform="matrix(1,0,0,1,0,0)"`（或等价的单位变换）属性。功能上无害，但白白增加文件体积。`SvgTransform` 适配器虽然会挑短写法，但单位矩阵仍占字符，所以这里提前判一下、单位矩阵干脆不写。

**练习 3**：同一个 `render_image` 在 `render_frame`（lib.rs:318）和 `write_glyph_defs`（text.rs:206）两处被调用，二者的 `state` 含义有何不同？

> **答案**：前者 `state` 是页面坐标系下的渲染状态（图片在页面上的位置与变换）；后者 `state` 是在字体单位/upem 空间里、为某个图像字形专门构造的状态（见 u4-l2 的 `State::new(frame.size()).pre_translate(...)`）。同一个写元素逻辑，套在不同坐标系上复用。

### 4.5 convert_image_scaling：映射 CSS image-rendering

#### 4.5.1 概念说明

图片放大/缩小时，浏览器默认用平滑插值（双线性等）来避免锯齿。但「像素艺术」类图片反而希望保留清晰的方块像素感。Typst 允许用户通过 `ImageScaling` 指定期望的缩放策略，typst-svg 需要把它翻译成浏览器认识的 CSS 属性 `image-rendering`。

注意这个函数是本文件里**唯一被 `pub`（而非 `pub(super)`）**的项（`image.rs:46`）——它和 `WebImage` 一样被 typst-html 复用（HTML 里 `<img>` 也需要同样的 `image-rendering` 映射），这是 typst-svg 对外公开的两个图像工具之一（另一个是 `WebImage`，见 u1-l2 的 `pub use`）。

#### 4.5.2 核心流程

```text
convert_image_scaling(scaling: Smart<ImageScaling>)
├─ Smart::Auto                          → None        （不输出属性，用浏览器默认）
├─ Smart::Custom(ImageScaling::Smooth)  → Some("smooth")
└─ Smart::Custom(ImageScaling::Pixelated) → Some("pixelated")
```

返回 `Option<&'static str>`：`None` 表示「什么都不写」，`Some(v)` 表示要写 `style="image-rendering: <v>"`。结合 4.4.3 的第 36-41 行看，`render_image` 正是用 `if let Some(value) = ...` 来决定写不写 `style` 属性。

#### 4.5.3 源码精读

[src/image.rs:45-56](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L45-L56) —— 三条分支一目了然。重点在两点：

- **`Smart::Auto → None`**：自动模式不写任何 `style`，完全交给浏览器默认（通常是平滑缩放）。这避免了在用户没要求时强加 CSS。
- **`Smooth` 分支的注释**（第 50-51 行）提醒：`image-rendering: smooth` 仍是实验特性，并非所有主流浏览器都实现（参见代码里给出的 MDN 链接）。即便如此 typst 仍照用户意图输出，让支持的浏览器受益。这是「忠实表达用户意图、由浏览器决定能否生效」的务实处理。

`ImageScaling` 枚举本身定义在 typst-library：[visualize/image/mod.rs:632-640](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/image/mod.rs#L632-L640) 只有 `Smooth` / `Pixelated` 两个变体，「自动」由外层的 `Smart::Auto` 表达。

#### 4.5.4 代码实践

**实践目标**：验证三种缩放设置对产物 SVG `<image>` 元素的影响差异。

**操作步骤**：

1. 对照 [src/image.rs:46-56](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L46-L56) 与 [src/image.rs:36-41](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L36-L41)，推演三种输入下 `<image>` 元素是否带 `style` 属性、带的值是什么：
   - `Smart::Auto`：`<image>` 无 `style`。
   - `Smooth`：`style="image-rendering: smooth"`。
   - `Pixelated`：`style="image-rendering: pixelated"`。
2. 在 typst 文档里给同一张像素图分别设置 `scaling: pixelated` 与默认，编译为 SVG，对比产物里该 `<image>` 元素的属性差异。

**需要观察的现象 / 预期结果**：只有显式设置 scaling 的图片，其 `<image>` 才会多出 `style="image-rendering: ..."`；默认（Auto）图片不带该属性。具体编译产物待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `convert_image_scaling` 返回 `Option<&'static str>` 而不是直接返回 `&str`？

> **答案**：因为「自动模式」需要表达「什么都不写」，这正对应 `None`。若返回 `&str` 就得用一个空串或哨兵值来表示「不写」，不如 `Option` 直观；`&'static str` 则因为返回值都是字面量、无需分配。

**练习 2**：这个函数为什么是 `pub` 而 `render_image` 是 `pub(super)`？

> **答案**：`render_image` 只在 typst-svg 内部被 `render_frame`/`write_glyph_defs` 调用，不构成对外 API；而 `convert_image_scaling` 被 typst-html 复用（HTML 的 `<img>` 同样需要 `image-rendering` 映射），故必须 `pub` 并经 `lib.rs` 的 `pub use` 对外导出（见 u1-l2）。

## 5. 综合实践

把本讲 5 个模块串起来，做一次「从 `Image` 到 `<image>`」的完整追踪。

**任务**：假设一张 **Pixel 格式、带 ICC profile** 的光栅图被插入文档，并显式设置了 `scaling: pixelated`，且在页面上被放置在一个非零位移处。请按顺序回答：

1. **归一化**：它走 `WebImage::new` 的哪个分支？依次用到 `RasterImage` 的哪些方法？最终 `WebImageFormat` 是什么？（提示：4.2 的分支二）
2. **编码**：`to_base64_url` 产出的 URL 前缀是什么？为何体积会比原始像素大约 4/3？（提示：4.3 的公式）
3. **写出**：`render_image` 写出的 `<image>` 元素会包含哪些属性？其中 `transform` 是否会出现（取决于什么）？`preserveAspectRatio` 的值是什么？是否会带 `style`？带的值是什么？（提示：4.4.3 逐行 + 4.5）
4. **复用**：若这张图在文档里出现 50 次，`#[comemo::memoize]` 分别在 `new` 和 `to_base64_url` 上各起什么作用？最终 base64 编码做了几次？

**参考答案要点**：

1. 走 Pixel 分支；用到 `icc()`（设置 ICC profile，失败用 `.ok()` 忽略）、`dynamic()`（取已解码像素，`write_with_encoder` 编码）；最终 `WebImageFormat::Png`。
2. 前缀 `data:image/png;base64,`；base64 每 3 字节编 4 字符，膨胀约 4/3。
3. 属性：`xlink:href`（data URL）、`width`/`height`（pt）、`preserveAspectRatio="none"`；`transform` 会出现（因为放在非零位移处，`state.transform` 非单位矩阵）；会带 `style="image-rendering: pixelated"`。
4. `new` 让 PNG 重编码只做 1 次、`to_base64_url` 让 base64 编码只做 1 次；50 次引用中 base64 编码总共只做 1 次（其余命中缓存）。

## 6. 本讲小结

- typst-svg 用 `WebImage { format, data }` 作为图像的「Web 归一化」中间表示，把 Typst 内部的多种 `ImageKind` 抹平成浏览器支持的少数格式。
- `WebImage::new` 是归一化核心：Exchange 格式零成本透传、Pixel 格式用 `PngEncoder`（尽量保留 ICC profile）重编码为 PNG、Svg 透传、Pdf 经 `pdf_to_svg`（hayro）转成 SVG（详情见 u6-l2）。
- `to_base64_url` 把图片内联成 `data:{mime};base64,...`，使产物 SVG 自包含；代价是体积约 4/3 膨胀。
- `render_image` 把 data URL、pt 单位的 `width`/`height`、累积 `transform`、`preserveAspectRatio="none"` 与可选的 `image-rendering` 组装成 `<image>` 元素；它在「页面图片」和「图像字形」两处被复用。
- `convert_image_scaling` 把 `Smart<ImageScaling>`（Auto/Smooth/Pixelated）映射到 CSS `image-rendering`，`Auto` 不写属性；它与 `WebImage` 一起作为 typst-svg 对 typst-html 公开的两个图像工具。
- `new` 与 `to_base64_url` 都被 `#[comemo::memoize]`，使重编码与 base64 编码在重复图片场景下只执行一次。

## 7. 下一步学习建议

- 本讲提到了 `pdf_to_svg`（4.2 分支四）但未展开：**u6-l2（嵌入 PDF 图转 SVG：hayro）** 会详细讲 hayro 的 `InterpreterSettings`、14 种 PDF 标准字体到 `typst_assets::pdf` 的映射，以及「Keep this in sync with typst-png」的跨 crate 维护约定。
- 若想理解 `render_image` 写元素时依赖的底层工具（`SvgElem` 的 RAII/Drop、`SvgTransform`/`attr_with`/`SvgFormatter` 的数字格式化），复习 **u2-l3（SVG 输出抽象层 write.rs）**。
- 若想理解 `render_image` 第二个调用点（图像字形）的来龙去脉，回顾 **u4-l1 / u4-l2（文本与字形、字形定义与符号复用）**。
- 下一站 **u6-l3（去重机制 Deduplicator 与 ID 编码）** 会讲本 crate 体积优化的底层基石——注意图像本身（不同于字形/渐变）**没有**进入 `Deduplicator` 去重表，而是靠 comemo 在「构造 `WebImage`/data URL」层面去重，可对比这两种去重思路的差异。
