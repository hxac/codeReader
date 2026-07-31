# 图像：栅格、SVG 与嵌入 PDF

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `handle_image()` 如何按 `ImageKind` 的三种变体（栅格 / SVG / 嵌入 PDF）分派到不同的 krilla 绘制调用。
- 解释 `PdfRasterImage` 如何通过实现 krilla 的 `CustomImage` trait，把 Typst 的 `RasterImage` 惰性地（`OnceLock`）翻译成 PDF 需要的颜色通道、Alpha 通道、ICC profile 与色彩空间。
- 区分 JPEG「直通」（`from_jpeg_with_icc`）与非 JPEG 走 `from_custom` 两条路径，以及 `convert_raster` / `convert_pdf` 用 `#[comemo::memoize]` 做的缓存。
- 读懂 `exif_transform()` 对 8 种 EXIF 旋转方向的变换矩阵与尺寸翻转逻辑，并能手算 orientation=6 的结果。

## 2. 前置知识

本讲承接 u2-l7（Frame 遍历器）与 u2-l8（类型转换工具集），在继续前请确认你已理解：

- **FrameItem 分派**：`handle_frame()` 按 `FrameItem` 变体路由，图像对应 `FrameItem::Image(image, size, span)`。
- **变换状态栈**：`FrameContext.states` 记录累计变换，`handle_image` 会用 `fc.state().transform()` 把它压入 krilla 图形状态栈。
- **`to_krilla()` 扩展 trait**：`SizeExt` / `TransformExt` 把 Typst 几何量翻译成 krilla 几何量（`SizeExt::to_krilla()` 返回 `Option`，负/非法尺寸会被丢弃）。

补充几个本讲会用到的图像领域术语：

- **栅格图（raster）**：由像素点阵组成的图（JPEG / PNG / GIF 等），与「矢量图」相对。
- **DynamicImage**：`image` crate 的统一图像容器，内部可能是 `ImageRgb8`、`ImageLuma8`、`ImageRgba16` 等多种像素布局。
- **ICC profile**：嵌入图像里的色彩描述文件，告诉阅读器「这组像素是在哪个色彩空间下定义的」，用于精准还原颜色。
- **EXIF orientation**：照片元数据里的方向标记（1–8），记录相机拍摄时的朝向，阅读器需据此旋转/翻转图像才能「正」着显示。
- **JPEG 直通（passthrough）**：不重新解码再编码 JPEG，而是把原始 JPEG 字节流（DCT 系数）原样嵌入 PDF，避免二次压缩造成画质损失。
- **CustomImage**：krilla 提供的 trait，让调用方自己提供「颜色通道 / Alpha 通道 / 位深 / 尺寸 / ICC / 色彩空间」，krilla 负责把这些组装成 PDF 图像对象。

## 3. 本讲源码地图

本讲几乎全部围绕单一文件展开，辅以分派入口与 tagged PDF hook：

| 文件 | 作用 |
| --- | --- |
| [src/image.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs) | 图像导出的全部逻辑：`handle_image` 分派、`PdfRasterImage` 实现、`convert_raster`/`convert_pdf` 缓存、`exif_transform` 方向校正。 |
| [src/convert.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs) | `handle_frame` 中 `FrameItem::Image` 的分派；`GlobalContext` 里 `image_spans` / `image_to_spans` 两张错误反查表。 |
| [src/tags/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs) | `tags::image()` hook 与 `TagHandle`（RAII 包裹图像绘制，发射无障碍标记内容）。 |

Typst 侧的图像类型定义在 `typst-library` 的 `visualize/image/` 下（`Image` / `ImageKind` / `RasterImage` / `PdfImage` 等），本讲按需引用其方法签名，不展开其实现。

## 4. 核心概念与源码讲解

### 4.1 handle_image：三种图像的分派入口

#### 4.1.1 概念说明

Typst 的 `Image` 内部持有一个 `ImageKind` 枚举，它有三种变体：

- `ImageKind::Raster(RasterImage)`：栅格图（JPEG / PNG / GIF …）。
- `ImageKind::Svg(SvgImage)`：矢量图（SVG）。
- `ImageKind::Pdf(PdfImage)`：嵌入的另一份 PDF（取其中的某一页）。

`handle_image()` 是这三者的**统一入口**。它先做所有图像共有的准备工作（压变换、记录错误 span、发射 tagged 标记），再用 `match image.kind()` 把三种变体分别交给 krilla 的三个绘制 API：`draw_image` / `draw_svg` / `draw_pdf_page`。这种「公共前奏 + 三路分派」的结构与 `handle_frame` 对 `FrameItem` 的处理思路一致。

#### 4.1.2 核心流程

`handle_image` 的执行顺序（公共前奏 → 分派 → 收尾）：

1. **压入累计变换**：把 `FrameContext` 累计的 `transform` 用 `push_transform` 压入 krilla 图形状态栈，并用 `defer` 注册 `pop`，保证绘制在正确的页面位置。
2. **设置错误定位**：`surface.set_location(span.into_raw())`，把当前 Typst `Span` 传给 krilla，使后续 krilla 报错能反查回源码位置；`defer` 注册 `reset_location`。
3. **计算插值标志**：`interpolate = image.scaling() == Smart::Custom(ImageScaling::Smooth)`（用户显式选 `smooth` 才双线性插值，默认不插值）。
4. **登记错误反查**：`gc.image_spans.insert(span)`，记下「这个 span 产出了图像」。
5. **发射 tagged 标记**：`tags::image(...)` 返回 RAII 的 `TagHandle`，把整段绘制包进无障碍 `Span`（含 `alt` 文本）；`handle.surface()` 借出被包裹的 `Surface` 继续绘制。
6. **三路分派**：按 `ImageKind` 调用对应 krilla API。
7. **收尾**：各 `defer` 与 `TagHandle::drop` 反序执行（弹变换、重置定位、结束标记内容）。

#### 4.1.3 源码精读

公共前奏（压变换、设定位、算插值、登记 span、发射 tagged）：

[src/image.rs:23-44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L23-L44) — `#[typst_macros::time]` 标注的 `handle_image`，入口处 `push_transform` + `set_location` + 两个 `defer`，随后算 `interpolate`、登记 `image_spans`、调 `tags::image` 拿到 `TagHandle`。

三路分派（match 的三个分支）：

[src/image.rs:46-78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L46-L78) — `match image.kind()` 的三个 arm：
- `Raster`：先经 `exif_transform` 算出方向校正变换与翻转后尺寸，再 `push_transform`，`convert_raster` 转 krilla 图像，登记 `image_to_spans`，最后 `draw_image`。
- `Svg`：`surface.draw_svg(svg.tree(), size, SvgSettings { embed_text: true, .. })`，注意 `embed_text: true` 表示 SVG 里的文字以文本形式嵌入（而非转曲线），保留可选中/可搜索。
- `Pdf`：`surface.draw_pdf_page(&convert_pdf(pdf), size, pdf.page_index())`，嵌入指定页。

入口处分派关系（`handle_frame` 调 `handle_image`）：

[src/convert.rs:368-370](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L368-L370) — `FrameItem::Image(image, size, span) => handle_image(gc, fc, image, *size, surface, *span)`。注意签名里 `gc` 是 `&mut`、`fc` 是 `&mut`、`surface` 是 `&mut`，三者都需可变借用。

RAII 标记包裹（`tags::image` 返回的 `TagHandle`）：

[src/tags/mod.rs:178-197](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs#L178-L197) — `TagHandle` 持有 `&mut Surface`，`Drop` 时若 `started` 则 `end_tagged()`；`surface()` 方法借出内部 `Surface` 供绘制使用。这正是「绘制期间包裹在标记内容里」的实现。

#### 4.1.4 代码实践

**目标**：理解 SVG 分支为何独有 `embed_text: true`，而栅格 / PDF 分支没有。

**操作步骤**（源码阅读型）：

1. 打开 [src/image.rs:64-72](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L64-L72) 与 [src/image.rs:73-77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L73-L77)。
2. 对比三个分支调用 krilla 的 API 名字（`draw_image` / `draw_svg` / `draw_pdf_page`）。
3. 思考：栅格图和嵌入 PDF 本身是否还包含「可选中的文字」？

**需要观察的现象 / 预期结果**：

- 栅格图是像素点阵，不含文字语义，所以无需 `embed_text`。
- 嵌入 PDF 由 krilla 的 `draw_pdf_page` 整页复用，文字本就保留在原 PDF 内容流里，也无需额外选项。
- 只有 SVG 是矢量描述，文字可被「转曲线」也可「保留为文本」，故需 `embed_text` 开关——默认开启保留文本，利于无障碍与检索。

#### 4.1.5 小练习与答案

**练习 1**：`handle_image` 开头连续用了两个 `defer`（一个弹变换、一个重置定位）。如果漏掉弹变换的那个 `defer`，会发生什么？

**答案**：krilla 图形状态栈会多压一层变换而不弹出，导致后续同页内容（或同 `Surface` 上的其它绘制）全部被错误地叠加这段图像的平移/旋转，页面布局错乱。`defer` 保证即便提前 `?` 返回错误也能成对 pop。

**练习 2**：`interpolate` 标志的来源是 `image.scaling()`。请说出默认情况下（用户未设 `scaling`）`interpolate` 的值。

**答案**：默认 `scaling` 为 `Smart::Auto`，不等于 `Smart::Custom(ImageScaling::Smooth)`，故 `interpolate = false`，PDF 端对图像做最近邻（不插值）显示，像素边缘锐利。

---

### 4.2 PdfRasterImage 与 CustomImage 实现

#### 4.2.1 概念说明

krilla 不知道 `image` crate 的 `DynamicImage`，它只认自己定义的 `CustomImage` trait。为了让 krilla 能消费 Typst 的栅格图，`typst-pdf` 写了一个适配器 `PdfRasterImage`，它实现 `CustomImage`，把 Typst 的 `RasterImage` 按需翻译成 PDF 所需的六个量：颜色通道、Alpha 通道、每分量位数、尺寸、ICC profile、色彩空间。

核心设计是**惰性计算**：颜色通道、Alpha 通道的派生结果可能很占内存，只在 krilla 真正需要时才算一次，并用 `OnceLock` 缓存，后续直接复用。`PdfRasterImage` 用 `Arc` 共享内部 `PdfRasterImageInner`，因此克隆廉价、可跨 `comemo` 缓存复用。

#### 4.2.2 核心流程

`CustomImage` 六个方法的产出逻辑：

- `color_channel()`：把任意 `DynamicImage` 归一化为 **luma8 或 rgb8** 像素字节。纯 `ImageLuma8` / `ImageRgb8` 直接复用；其余按通道数归并（1 或 2 通道转 luma8，其它转 rgb8）。结果用 `actual_dynamic: OnceLock` 缓存。
- `alpha_channel()`：若图像有 Alpha，把每个像素的第 4 字节单独抽成一条字节流；否则返回 `None`。用 `alpha_channel: OnceLock` 缓存。
- `bits_per_component()`：恒为 `Eight`（8 位/分量）。
- `size()`：返回原图的 `(width, height)`（像素数）。
- `icc_profile()`：**仅**当原图是 8 位灰度/RGB 变体（luma8 / lumaA8 / rgb8 / rgba8）时返回原始 ICC；其余情况返回 `None`（见 4.2.4 实践）。
- `color_space()`：原图「有色」则 `Rgb`，否则（纯灰）`Luma`。

#### 4.2.3 源码精读

适配器结构与惰性字段：

[src/image.rs:83-108](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L83-L108) — `PdfRasterImage(Arc<PdfRasterImageInner>)` 与 `PdfRasterImageInner`，内含原始 `raster` 与两个 `OnceLock`（`alpha_channel`、`actual_dynamic`）；`new()` 初始化两个空 `OnceLock`。

`Hash` 实现（为何只哈希 `raster`）：

[src/image.rs:110-116](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L110-L116) — 只对 `self.0.raster` 求哈希。注释说明：两个 `OnceLock` 都是由 `raster` 派生的，故哈希 `raster` 即可；又因 `raster` 是「预哈希」的，这一步非常廉价。

颜色通道归一化（`color_channel`）：

[src/image.rs:119-137](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L119-L137) — `get_or_init` 里按 `(dynamic, channel_count)` 分四类：纯 luma8 / 纯 rgb8 直接 clone；`(_, 1|2)`（1–2 通道，含 lumaA8）转 luma8；其余（含 rgba8、16 位、浮点等）转 rgb8。这正是「PDF 端只认 8 位灰度/RGB」的归一化点。

Alpha 通道抽取（`alpha_channel`）：

[src/image.rs:139-154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L139-L154) — 仅当 `has_alpha()` 时，遍历 `pixels()` 取每个 `Rgba([_,_,_,a])` 的第 4 字节，聚合成 `Vec<u8>`；否则 `None`。

ICC profile 的条件保留：

[src/image.rs:164-178](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L164-L178) — `matches!` 判定四种 8 位灰度/RGB 变体才返回 `raster.icc()`；其余返回 `None`，注释解释「转换成 rgb8/luma8 后 ICC 可能失效，故丢弃」。

色彩空间判定（`color_space`）：

[src/image.rs:180-187](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L180-L187) — `has_color()` 为真则 `Rgb`，否则 `Luma`；注释提醒「我们已把所有图像转成 RGB 或 luma」。

#### 4.2.4 代码实践（本讲核心实践之一）

**目标**：解释 `PdfRasterImage` 为何只在「8 位灰度 / RGB 变体」时返回原始 ICC profile，其它情况丢弃 ICC。

**操作步骤**（源码追踪型）：

1. 读 [src/image.rs:119-137](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L119-L137)（`color_channel`）与 [src/image.rs:164-178](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L164-L178)（`icc_profile`）。
2. 列出 `color_channel` 对每种 `DynamicImage` 变体做了什么，判断「像素数值是否被改变」。
3. 推论：ICC 描述的是「当前像素数值所处的色彩空间」，只有当像素数值**未被改变**时 ICC 才仍有效。

**预期结果（参考答案）**：

`icc_profile` 保留的四种变体是 `ImageLuma8 / ImageLumaA8 / ImageRgb8 / ImageRgba8`：

- 纯 `ImageLuma8`、`ImageRgb8`：`color_channel` 直接 `clone`，像素数值原样不变 → ICC 仍精确有效。
- `ImageLumaA8`（2 通道）：`color_channel` 走 `(_, 1|2)` 分支转 `luma8`，只是**剥离 Alpha**，灰度数值不变 → 描述灰度的 ICC 仍有效。
- `ImageRgba8`（4 通道）：`color_channel` 走 `else` 转 `rgb8`，同样**只剥离 Alpha**，RGB 数值不变 → ICC 仍有效。

而其它 `DynamicImage` 变体（如 `ImageLuma16` / `ImageRgb16` 16 位图、`ImageRgb32F` 浮点图、`ImageBgr8` / `ImageBgra8` 等通道顺序不同的图）在 `color_channel` 里会经历**有损转换**：16→8 位降采样、浮点→8 位量化、BGR→RGB 重排，这些都会改变像素数值，使原始 ICC 与新数值对不上（色彩描述失真）。因此这类情况直接返回 `None`，让 krilla 不嵌入 ICC（回退到默认色彩空间），比嵌入一个错误的 ICC 更安全。

> 注：规格里「纯 luma8/rgb8」是这四种 8 位灰度/RGB 变体的简称。严格按代码，`ImageLumaA8` 与 `ImageRgba8` 也保留 ICC，因为它们转 luma8/rgb8 时仅丢 Alpha、不改色值。

#### 4.2.5 小练习与答案

**练习 1**：`color_channel` 对 `ImageRgba8` 返回的字节是 3 通道还是 4 通道？Alpha 去哪了？

**答案**：返回 3 通道 rgb8（`to_rgb8()` 丢弃 Alpha）。Alpha 被 `alpha_channel()` 单独抽成一条字节流，PDF 端用独立的 Soft-Mask 表达透明度，与颜色通道分离。

**练习 2**：为何 `PdfRasterImage` 内部用 `Arc` 包裹，且 `Hash` 只哈希 `raster`？

**答案**：`Arc` 让克隆零成本（仅增引用计数），便于跨 `convert_raster` 的 `comemo` 缓存共享同一份派生数据；两个 `OnceLock` 都由 `raster` 完全决定，故哈希 `raster` 即可区分不同图像，且 `raster` 自带预哈希使哈希很廉价。

---

### 4.3 convert_raster / convert_pdf：缓存与 JPEG 直通

#### 4.3.1 概念说明

把 `RasterImage` / `PdfImage` 转成 krilla 对象（`krilla::image::Image` / `krilla::pdf::PdfDocument`）是有成本的：栅格图要解析像素、抽通道；嵌入 PDF 要解析 PDF 结构。一份文档里同一张图可能被反复引用（例如每页页眉 logo），所以 `typst-pdf` 用 `#[comemo::memoize]` 给这两个转换加了**进程级缓存**——相同输入只算一次。

栅格图又分两条路：

- **JPEG 直通**：当 `raster.format()` 是 `ExchangeFormat::Jpg` 时，调 `krilla::image::Image::from_jpeg_with_icc`，把**原始 JPEG 字节**和 ICC 直接交给 krilla。krilla 不重新解码再编码，而是把 JPEG 的 DCT 压缩流原样嵌入 PDF（PDF 原生支持 DCTDecode 滤镜），既保画质又省体积。
- **其它格式（PNG / GIF / …）**：走 `from_custom(PdfRasterImage::new(raster), interpolate)`，由上一节的 `CustomImage` 实现按需提供像素数据。

> 为什么 JPEG 能直通而 PNG 不能？因为 PDF 标准内置了 JPEG（DCTDecode）解码器，可把 JPEG 流当原生图像用；PNG 没有这种 PDF 原生支持，必须解码成像素再重新封装。

#### 4.3.2 核心流程

`convert_raster(raster, interpolate)`：

1. 判 `raster.format()` 是否为 `RasterFormat::Exchange(ExchangeFormat::Jpg)`。
2. 若是 JPEG：构造 `image_data`（原始字节 `Arc`）与可选 `icc_profile`，调 `from_jpeg_with_icc` 直通。
3. 否则：`from_custom(PdfRasterImage::new(raster), interpolate)` 走 `CustomImage`。
4. 返回 `Result<krilla::image::Image, String>`（可能失败，如 16 位图在某些校验模式下不被支持）。

`convert_pdf(pdf)`：`PdfDocument::new(pdf.document().pdf().clone())`，把 Typst 侧已加载的 PDF 字节包成 krilla `PdfDocument`。

#### 4.3.3 源码精读

`convert_raster`（JPEG 直通 vs `from_custom`）：

[src/image.rs:190-211](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L190-L211) — `#[comemo::memoize]`；`if let RasterFormat::Exchange(ExchangeFormat::Jpg)` 走 `from_jpeg_with_icc`，把 `raster.data()` 原样传入；`else` 走 `from_custom(PdfRasterImage::new(raster), interpolate)`。

`convert_pdf`（同样 memoize）：

[src/image.rs:213-216](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L213-L216) — `PdfDocument::new(pdf.document().pdf().clone())`。

`handle_image` 中对栅格分支的使用（含 `?` 错误传播与 `image_to_spans` 登记）：

[src/image.rs:47-63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L47-L63) — `convert_raster(...).map_err(|err| eco_format!("failed to process image ({err})")).at(span)?`；成功后若 `image_to_spans` 未含该 image，则登记 `image → span`，供 16 位图错误反查（见下）。

`image_to_spans` 在错误映射中的实际用途：

[src/convert.rs:464-469](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L464-L469) — `KrillaError::SixteenBitImage(image, _)` 时用 `gc.image_to_spans[&image]` 反查 span，报「16 bit images are not supported」并给出「转为 8 位」的 hint。这就是 `image_to_spans` 登记的意义：krilla 只回传 krilla `Image` 对象，typst-pdf 需要自己维护「krilla 图像 → 源码 span」的反向映射才能把错误指回源码。

#### 4.3.4 代码实践

**目标**：验证「同一张 JPEG 在文档中出现两次，`convert_raster` 只解析一次」。

**操作步骤**（源码阅读型）：

1. 读 [src/image.rs:190-211](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L190-L211)，注意 `#[comemo::memoize]` 作用在整个函数上。
2. 思考 `comemo` 缓存的键是什么（答：函数参数 `raster` 与 `interpolate` 的哈希），而 `RasterImage` 是「预哈希」类型，故比较成本极低。
3. 推论：若两次引用的 `RasterImage` 哈希相同，第二次直接命中缓存返回同一个 `krilla::image::Image`。

**预期结果**：第二次调用 `convert_raster` 不会重新执行 JPEG 解析 / `from_jpeg_with_icc`，而是返回缓存的同一 `Image` 对象——这就是页眉 logo 等重复图不会拖慢导出的原因。实际运行耗时需本地验证（可用 `#[typst_macros::time]` 输出对比，但 comemo 命中后 `handle image` 计时仍会累计绘制时间，需区分「转换」与「绘制」）。

#### 4.3.5 小练习与答案

**练习 1**：`from_jpeg_with_icc` 的第三个参数 `interpolate` 从哪来？默认值是什么？

**答案**：来自 `handle_image` 开头的 `interpolate = image.scaling() == Smart::Custom(ImageScaling::Smooth)`。默认 `scaling` 为 `Auto`，故默认 `interpolate = false`。

**练习 2**：为什么 `convert_pdf` 也用 `#[comemo::memoize]`，而不是直接内联 `PdfDocument::new`？

**答案**：嵌入 PDF 的解析有成本，且同一份嵌入 PDF（如多次引用同一文件不同页）应只解析一次；memoize 以 `&PdfImage` 为键缓存 `PdfDocument`，避免重复 `PdfDocument::new`。

---

### 4.4 exif_transform：JPEG 方向校正

#### 4.4.1 概念说明

手机拍的照片常带 EXIF orientation 标记（1–8），告诉阅读器图像需如何旋转/翻转才能正向显示。对**非 JPEG** 格式，Typst 在解码阶段已把方向「烘焙」进像素数据，导出时无需再处理；但 **JPEG** 走的是「直通」（上一节），原始字节未被重解码，方向信息不会自动应用，所以必须在绘制时用**变换矩阵**把方向补上——这就是 `exif_transform()` 的职责。

EXIF 8 个方向分两类：

- **不换尺寸**（orientation 2/3/4）：只做水平/垂直镜像，宽高不变。
- **换尺寸**（orientation 5/6/7/8）：含 90° 旋转，宽高互换。

`exif_transform` 返回 `(Transform, Size)`：变换矩阵 + 变换后的新尺寸（旋转类需把宽高对调）。

#### 4.4.2 核心流程

`exif_transform(image, size)`：

1. 若非 JPEG，直接返回 `(identity, size)`（方向已烘焙进像素，无需处理）。
2. 定义两个构造器：
   - `base(hp, vp, base_ts, size)`：在 `base_ts` 基础上，按需叠加水平翻转（`hp`）与垂直翻转（`vp`），翻转用「先平移 `-size` 再 `scale(-1)`」实现镜像。
   - `no_flipping(hp, vp)`：以 `identity` 为底，返回 `base(...)`，尺寸不变。
   - `with_flipping(hp, vp)`：以「绕原点旋转 90° 再 y 翻转」为底，返回宽高互换后的 `base(...)`。
3. 按 `image.exif_rotation()` 的值（2–8，或 `_` 当作 1/无）选分支。

`pre_concat` 语义复习（u2-l8 已讲）：`a.pre_concat(b)` 得到的矩阵作用在点上时，**先施加 `b`，再施加 `a`**。

#### 4.4.3 源码精读

`exif_transform` 全貌与 JPEG 判定：

[src/image.rs:218-266](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L218-L266) — 开头判 `image.format() != RasterFormat::Exchange(ExchangeFormat::Jpg)` 则返回 `(identity, size)`；随后定义 `base` / `no_flipping` / `with_flipping`，最后 `match image.exif_rotation()` 把 2–8 映射到对应构造器。

`base` 的镜像实现（先平移再缩放）：

[src/image.rs:226-244](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L226-L244) — 水平镜像 = `scale(-1,1).pre_concat(translate(-size.x, 0))`（先把右边界搬到原点，再 x 翻转）；垂直镜像同理用 `size.y`。这样镜像后图像仍落在原 `[0,size]` 框内。

旋转类构造器 `with_flipping`：

[src/image.rs:249-254](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L249-L254) — `base_ts = rotate_at(90°, 0, 0).pre_concat(scale(1,-1))`，并把尺寸换为 `inv_size = Size::new(size.y, size.x)`（宽高互换）。

`handle_image` 中对 exif 变换的应用：

[src/image.rs:47-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L47-L51) — 栅格分支先 `let (exif_transform, new_size) = exif_transform(raster, size)`，再 `push_transform(&exif_transform.to_krilla())`，随后用 `new_size`（若有效）`draw_image`。

#### 4.4.4 代码实践（本讲核心实践之二）

**目标**：追踪一张 EXIF orientation=6 的 JPEG，写出 `exif_transform` 返回的变换与翻转后的尺寸。

**操作步骤**（手算型）：

1. 读 [src/image.rs:256-265](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L256-L265) 的 `match`，确认 orientation=6 命中 `Some(6) => with_flipping(false, true)`。
2. 展开 `with_flipping(false, true)`（[src/image.rs:249-254](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/image.rs#L249-L254)）：
   - `base_ts = rotate_at(90°, 0, 0).pre_concat(scale(1,-1))`
   - `inv_size = Size::new(size.y, size.x)`
   - 再调 `base(hp=false, vp=true, base_ts, inv_size)`，其中 `vp=true` 会再叠一次垂直翻转。

**预期结果（参考答案）**：

- **翻转后的尺寸**：`new_size = Size::new(size.y, size.x)`，即**宽高互换**。设原图 `size = (W, H)`，则导出时绘制框为 `(H, W)`。
- **变换矩阵**：设 `S = scale(1,-1)`（垂直翻转），`R = rotate(90°)`（绕原点），`U = translate(0, -inv_size.y) = translate(0, -W)`（`inv_size.y` 即原宽 `W`）。按 `pre_concat` 语义逐层展开（点先经最内层）：

\[ \text{final} = R \cdot S \cdot S \cdot U = R \cdot U \]

  因为两次垂直翻转 \( S^2 = I \) 抵消，最终净变换 = **绕原点旋转 90°，并在其前先向上平移 `W`**（`translate(0, -W)`）。

- **直观含义**：EXIF orientation=6 表示「图像需顺时针旋转 90° 才能正向显示」。代码用 `rotate(90°)` 配合平移把原图映射进宽高互换后的 `(H, W)` 绘制框，于是 PDF 阅读器无需识别 EXIF 即可正确显示方向。

> 待本地验证：若手头有带 orientation=6 的 JPEG，可用 Typst 的 `image()` 插入并导出 PDF，再用阅读器对比显示方向是否正向。

#### 4.4.5 小练习与答案

**练习 1**：orientation=3（180° 旋转）会走哪个分支？尺寸是否互换？

**答案**：`Some(3) => no_flipping(true, true)`（同时水平 + 垂直镜像 = 180° 旋转）。走 `no_flipping`，**尺寸不变**（仍是原 `size`）。

**练习 2**：为何函数开头对「非 JPEG」直接返回 `identity`？

**答案**：非 JPEG 在 Typst 解码阶段已把 EXIF 方向烘焙进像素数据（解码时即旋转/翻转到位），导出时像素本身已是正向，再叠加变换会重复旋转，故返回 `identity` 不做处理。只有 JPEG 因走「直通」未重解码，才需在绘制时补变换。

---

## 5. 综合实践

把本讲四块知识串起来：**追踪一张带 Alpha 通道、带 EXIF orientation=6 的 PNG 与一张同方向的 JPEG，对比它们在导出链路上的差异。**

1. **分派入口**：二者都经 [src/convert.rs:368-370](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L368-L370) 进入 `handle_image`，公共前奏（压变换、设定位、登记 span、发射 tagged）相同。
2. **格式判定与转换路径**：
   - PNG（非 JPEG）→ `convert_raster` 走 `from_custom(PdfRasterImage)` → 触发 `CustomImage` 的 `color_channel`（转 rgb8）、`alpha_channel`（抽出 Alpha 字节）。
   - JPEG → `convert_raster` 走 `from_jpeg_with_icc` 直通，**不**经过 `PdfRasterImage` 的通道抽取。
3. **EXIF 处理差异**：
   - PNG：`exif_transform` 开头判非 JPEG，直接返回 `(identity, size)`——方向已在解码时烘焙。
   - JPEG：orientation=6 → `with_flipping(false, true)`，返回 90° 旋转变换 + 宽高互换的 `new_size`。
4. **ICC 差异**：若二者都是 8 位 rgb8/rgba8，ICC 保留；若是 16 位，则 `icc_profile` 返回 `None`，且 JPEG 直通路径下 16 位图还可能在 `finish()` 触发 `SixteenBitImage` 错误（[src/convert.rs:464-469](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L464-L469)），错误 span 由 `image_to_spans` 反查。
5. **产出**：用一张表总结两路径在每个环节（转换函数、是否经 CustomImage、通道抽取、EXIF 变换、ICC 保留）的差异。

> 待本地验证：可准备两张内容相同但格式不同的图（一张 PNG、一张 JPEG，均带 EXIF 6 与 Alpha），分别用 Typst 导出 PDF，用 PDF 阅读器检查显示方向是否一致、Alpha 是否正确叠加。

## 6. 本讲小结

- `handle_image()` 是三种 `ImageKind` 的统一入口：栅格走 `draw_image`、SVG 走 `draw_svg`（`embed_text: true`）、嵌入 PDF 走 `draw_pdf_page`。
- `PdfRasterImage` 通过实现 krilla `CustomImage`，用 `OnceLock` 惰性提供颜色通道（归一为 luma8/rgb8）、Alpha 通道、ICC、色彩空间等六个量。
- ICC profile 仅对 8 位灰度/RGB 变体（luma8/lumaA8/rgb8/rgba8）保留，因为只有这些在 `color_channel` 里像素数值不被有损改变；其余转换会失真故丢弃。
- `convert_raster` / `convert_pdf` 均用 `#[comemo::memoize]` 缓存；JPEG 走 `from_jpeg_with_icc` 直通（保画质），其它格式走 `from_custom`。
- `exif_transform` 只对 JPEG 生效（非 JPEG 方向已烘焙进像素）；orientation 2/3/4 不换尺寸，5/6/7/8 含 90° 旋转、宽高互换；orientation=6 经两次垂直翻转抵消，净变换为绕原点旋转 90° 加平移。
- `image_spans`（集合）与 `image_to_spans`（krilla 图像→span 映射）是两张错误反查表，分别支撑透明度错误的针对性 hint 与 16 位图错误的源码定位。

## 7. 下一步学习建议

- 本讲只讲「内容翻译」，图像在 PDF/UA 校验下的约束（透明度、16 位、版本要求）发生在 `finish()`，建议接着读 **u5-l18（错误处理与校验结果映射）**，重点看 `KrillaError::Image / SixteenBitImage` 与 `ValidationError::Transparency` 如何用到本讲的 `image_spans` / `image_to_spans`。
- 若想了解 SVG 分支 `draw_svg` 的更深行为（文字嵌入、字体子集），可阅读 `krilla-svg` 的 `SvgSettings` 文档。
- 至此 u3 单元（文字、图形、色彩、图像）的内容翻译器已全部讲完，下一讲 u4 进入**文档级特性**（链接 / 书签 / 元数据 / 附件），从「单页内容」跃迁到「整文档结构」。
