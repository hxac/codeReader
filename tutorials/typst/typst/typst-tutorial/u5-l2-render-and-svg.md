# 像素与矢量：typst-render 与 typst-svg

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Frame` 图元模型的六类图元，并解释 typst-render 与 typst-svg 如何以不同方式消费同一份 `PagedDocument`。
2. 跟踪栅格化路径：一次 `typst_render::render` 调用如何从 PPI 算出像素画布、递归遍历 Frame 树、把矩形/曲线/文字/图像/渐变逐一画进像素缓冲区。
3. 理解 SVG 序列化路径：`SVGRenderer` 如何把同样的图元写成 XML 文本，并用 `Deduplicator` 去重字形、渐变与裁剪路径。
4. 把 `tests/` 测试体系中的 `render`、`svg` 两个 stage 与这两个 crate 的入口函数一一对应起来，知道参考产物分别以文件和哈希两种方式存放。

## 2. 前置知识

- **Frame（帧）**：排版阶段的最终产物。一个 Frame 就是"一块固定尺寸的画布，上面在确定位置放着若干图元"。第四单元讲过 `layout_document` 会产出 `PagedDocument`——本质是一叠 `Page`，每个 `Page` 里有一个根 `Frame`。本讲的两个 crate 都只读这份结果，**绝不回头触碰求值与排版**。
- **栅格图 vs 矢量图**：PNG 是像素网格，每个像素存颜色；放大就会模糊。SVG 是 XML 文本，存的是几何描述（路径、引用、变换），查看器负责即时绘制，任意缩放不失真。同一份 Frame，两种"翻译"方向。
- **pt 与 PPI**：Typst 内部长度单位是 pt（磅），1 英寸 = 72 pt。栅格化时需要把物理长度换成像素数，换算系数由 PPI（每英寸像素数）决定。
- **tiny-skia**：Skia 图形库的 Rust 子集实现，提供 `Pixmap`（RGBA 像素缓冲区）、`Path`（路径）、`Paint`（画笔/着色器）、`Mask`（裁剪遮罩）等 CPU 绘制原语。typst-render 建立在它之上。
- **comemo 记忆化**：第一、四单元讲过的增量编译基石。本讲会看到导出器也大量使用 `#[comemo::memoize]`（字形栅格化、图像纹理、几何转路径），以及 `Frame` 本身可哈希的特性如何让这一切成立。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-library/src/layout/frame.rs` | `Frame`/`FrameItem`/`GroupItem` 的定义——两个导出器的共同输入 |
| `crates/typst-render/src/lib.rs` | 栅格化入口 `render`/`render_merged`、`State` 与递归分发 |
| `crates/typst-render/src/shape.rs` | 几何图形 → tiny-skia 路径，填充与描边 |
| `crates/typst-render/src/text.rs` | 文字运行 → 字形 → 路径/位图，手工像素混合 |
| `crates/typst-render/src/image.rs` | 图像 → 先缩放成纹理再作为图案绘制 |
| `crates/typst-render/src/paint.rs` | 颜色/渐变/平铺的采样抽象 `PaintSampler` |
| `crates/typst-render/src/format.rs` | PNG 格式元素（`ppi` 选项）与 `RenderOptions` |
| `crates/typst-svg/src/lib.rs` | SVG 导出入口 `svg`/`svg_merged`、`SVGRenderer`、去重器 |
| `crates/typst-svg/src/text.rs` | 字形 → `<symbol>` 定义 + `<use>` 引用 |
| `crates/typst-svg/src/shape.rs` | 形状 → `<path>` 元素与描边属性 |
| `crates/typst-svg/src/paint.rs` | 填充/描边的 `url(#id)` 引用与渐变双层结构 |
| `crates/typst-svg/src/image.rs` | 图像 → base64 数据 URL 内嵌 |
| `crates/typst-svg/src/write.rs` | XML 写出的 RAII 封装与惰性元素、数字格式化 |
| `crates/typst-svg/src/format.rs` | SVG 格式元素（`pretty` 选项）与 `SvgOptions` |
| `crates/typst-cli/src/compile.rs` | CLI 侧如何调用这两个导出器 |
| `tests/src/output.rs` | 测试体系中 `render`/`svg` 两个输出类型的实现 |
| `tests/README.md` | stage 树与 `--stages` 用法 |

## 4. 核心概念与源码讲解

### 4.1 Frame 图元模型：两个导出器的共同输入

#### 4.1.1 概念说明

第五单元的统一主题是"一份 `PagedDocument`，多种导出方向"。上一讲的 typst-pdf 如此，本讲的 typst-render（PNG）与 typst-svg 亦然。它们能共享输入的前提是 `Frame` 是一个**封闭、不可变、可哈希**的数据模型：

- **封闭**：图元只有六类，导出器只需处理这六类，排版内部的复杂性（段落、网格、数学）到这里已经全部摊平。
- **不可变 + 可哈希**：`Frame` 的条目存放在 `Arc<LazyHash<...>>` 中，克隆廉价、哈希按需计算，这让 comemo 可以缓存"这一页排出来是什么"以及下游所有派生结果。

#### 4.1.2 核心流程

两个导出器的主体都是同一个递归模式：

```text
遍历 Frame：
  对每个 (位置 pos, 图元 item)：
    Group  → 变换/裁剪处理后递归进入子 Frame
    Text   → 按字形逐个绘制
    Shape  → 构建路径 → 填充 → 描边
    Image  → 解码/缩放后绘制（PNG）或内嵌数据（SVG）
    Link   → PNG：忽略；SVG：写成 <a> 链接
    Tag    → 两者都忽略（内省元数据，与视觉无关）
```

关键差异在"绘制"落到哪里：PNG 落到像素缓冲区，SVG 落到 XML 元素树。

#### 4.1.3 源码精读

先看共同输入的定义。`Frame` 本体是尺寸 + 基线 + 条目列表 + 软硬标记：

- [crates/typst-library/src/layout/frame.rs:L17-L30](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/frame.rs#L17-L30)：`Frame` 结构体。条目存放在 `Arc<LazyHash<Vec<(Point, FrameItem)>>>` 里——每 个图元带一个固定位置，整体被 `LazyHash` 包裹以支持增量缓存。

六类图元的枚举：

- [crates/typst-library/src/layout/frame.rs:L486-L499](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/frame.rs#L486-L499)：`FrameItem` 枚举——`Group`（带变换与裁剪的子帧）、`Text`（一次整形后的文字运行）、`Shape`（几何 + 可选填充/描边）、`Image`（图像 + 尺寸）、`Link`（链接目标 + 尺寸）、`Tag`（内省标签）。

`Group` 图元的展开形式：

- [crates/typst-library/src/layout/frame.rs:L515-L526](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/frame.rs#L515-L526)：`GroupItem` 含子 `frame`、`transform`（变换矩阵）、`clip`（可选裁剪曲线）、`label`。递归遍历时必须正确累积 `transform` 并维护裁剪。

再看两侧的遍历分发，注意它们几乎是镜像：

- [crates/typst-render/src/lib.rs:L187-L206](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-render/src/lib.rs#L187-L206)：栅格化侧的 `render_frame`——`Group` 走 `render_group` 递归，`Text`/`Shape`/`Image` 各自分发到子模块，`Link` 与 `Tag` 是空分支。
- [crates/typst-svg/src/lib.rs:L324-L338](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/lib.rs#L324-L338)：SVG 侧的 `SVGRenderer::render_frame`——同样的六路 match，但 `Link` 会调用 `render_link` 生成 `<a>` 元素，只有 `Tag` 被忽略。

把两侧对六类图元的处理整理成表：

| FrameItem | typst-render（PNG） | typst-svg |
| --- | --- | --- |
| `Group` | 递归 + 变换拼接 + 遮罩求交 | 递归 + `<g transform>` + `clip-path` 引用 |
| `Text` | 逐字形栅格化（路径或 pixglyph 位图） | 逐字形 `<use>` 引用 `<symbol>` 中去重后的字形 |
| `Shape` | tiny-skia 路径填充/描边 | `<path>` 元素 + 属性 |
| `Image` | 解码缩放为纹理后作为图案绘制 | base64 数据 URL 内嵌 `<image>` |
| `Link` | 忽略（像素图没有可点击区域） | `<a href>` 包一个透明矩形 |
| `Tag` | 忽略（内省专用） | 忽略 |

#### 4.1.4 代码实践

**实践目标**：不写一行代码，靠"对照阅读"验证两个导出器对同一图元模型的对称性。

**操作步骤**：

1. 打开 [crates/typst-render/src/lib.rs:L187-L206](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-render/src/lib.rs#L187-L206) 与 [crates/typst-svg/src/lib.rs:L324-L338](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/lib.rs#L324-L338)，逐行对照两个 match 的分支。
2. 对每个分支追问三个问题：变换从哪里来？裁剪如何维护？这个图元在目标格式里的"落点"是什么？

**需要观察的现象**：两侧的分支结构完全同构；唯一不对称的是 `Link`。

**预期结果**：你会确认"六类图元"是导出层的完整词汇表——任何排版功能最终都要坍缩成这六类才能被导出。待本地验证（纯阅读任务，无需运行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Tag` 在两个导出器里都被忽略？

**答案**：`Tag` 是内省机制的元数据（第四单元 u4-l7 讲过，realize 的 prepare 阶段生成、`Introspector` 从 Frame 里收集它们来建立 query/counter 索引）。它不产生任何视觉效果，导出器在排版收敛完成后才运行，此时标签的信息已经被消费完毕，自然可以直接跳过。

**练习 2**：为什么 PNG 忽略 `Link` 而 SVG 要渲染成 `<a>`？

**答案**：PNG 是静态像素矩阵，没有"可点击区域"的概念，链接信息无处安放；SVG 是结构化文档格式，浏览器支持 `<a>` 元素的点击跳转，所以 typst-svg 用一个透明矩形承载链接的热区。

**练习 3**：`Frame` 为什么要用 `Arc<LazyHash<Vec<(Point, FrameItem)>>>` 而不是普通 `Vec`？

**答案**：`Arc` 让 Frame 克隆只是引用计数递增（页面在多轮迭代间传递时零拷贝）；`LazyHash` 让哈希值首次计算后缓存——comemo 依赖内容的哈希来判断缓存是否命中，Frame 作为排版输出正好是下游导出与下一轮迭代缓存的键。

### 4.2 栅格化路径：typst-render 如何把图元画成像素

#### 4.2.1 概念说明

栅格化（rasterization）把连续的几何描述离散到像素网格。typst-render 是一个"翻译 + 委托"层：它把 Typst 的图元翻译成 tiny-skia 的原语（`Path`/`Paint`/`Mask`），真正的扫描线填充由 tiny-skia 完成。它自己亲手写的像素操作只有一处——小字号的字形混合（为了质量与性能，绕过了 tiny-skia）。

坐标换算是理解入口的钥匙：Typst 世界以 pt 为单位，画布以像素为单位，桥梁是

\[ \text{pixel\_per\_pt} = \frac{\mathrm{ppi}}{72}, \qquad \mathrm{px} = \operatorname{round}\!\big(\text{pixel\_per\_pt} \times \mathrm{pt}\big) \]

例如默认 PPI 144 时每 pt 占 2 像素。

#### 4.2.2 核心流程

```text
render(page, opts)：
  1. 由 ppi 算出 pixel_per_pt，进而算出画布的像素宽高（至少 1×1）
  2. 创建 sk::Pixmap 画布
  3. 画页面背景：纯色走 canvas.fill 快路径；渐变/平铺退化为画一个大矩形
  4. State 平移掉 bleed（出血边距），render_frame 递归整个页面帧
  5. 返回像素缓冲区

render_group（每个 Group 图元）：
  - Soft 帧：变换直接左乘进当前变换
  - Hard 帧：额外重置 container_transform 与 size（渐变"容器"边界）
  - 有 clip：把裁剪曲线转成路径，与父遮罩求交或新建全画布遮罩
  - 递归 render_frame
```

绘制顺序上，`render_frame` 严格按 Frame 条目的存储顺序绘制——这正是排版阶段决定的 z 序（后画的覆盖先画的）。

#### 4.2.3 源码精读

**入口与画布准备**：

- [crates/typst-render/src/lib.rs:L25-L52](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-render/src/lib.rs#L25-L52)：`render` 入口。第 29 行算 `pixel_per_pt`，第 30-31 行套用上面的公式取像素尺寸（`max(1.0)` 保证空页面也有画布），第 38-45 行处理背景：`page.fill_or_white()` 保证未设置填充时兜底为白色，纯色背景直接 `canvas.fill`，否则把整个页面尺寸当一个矩形交给形状渲染器去画渐变。

**贯穿全程的 State**：

- [crates/typst-render/src/lib.rs:L117-L129](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-render/src/lib.rs#L117-L129)：`State` 携带五样东西——当前 `transform`、**第一个 hard 帧的变换 `container_transform`**、当前 `mask`（裁剪遮罩）、`pixel_per_pt`、容器尺寸 `size`。后三个都是为渐变/平铺的 `RelativeTo::Parent` 语义服务的（见 4.2.3 末尾）。

**组的递归与软硬帧**：

- [crates/typst-render/src/lib.rs:L209-L263](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-render/src/lib.rs#L209-L263)：`render_group`。`FrameKind::Soft` 只是拼接变换；`FrameKind::Hard` 额外通过 `post_concat(container_transform.invert())` 等运算把容器坐标系切换到当前组（L213-L223），并 `with_size` 更新容器尺寸。裁剪部分（L228-L260）：把 `Curve` 转 tiny-skia 路径后，若已有父遮罩则 `intersect_path` 求交，否则新建一个全画布遮罩；遮罩创建失败（裁剪区域为空）时直接返回——整组被裁没了。

**形状：先填充后描边**：

- [crates/typst-render/src/shape.rs:L11-L84](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-render/src/shape.rs#L11-L84)：`render_shape`。先把 `Geometry`（Line/Rect/Curve）统一转成 tiny-skia 路径；矩形遇到负宽高时用符号缩放变换修正（tiny-skia 不接受负尺寸，L22-L32）；矩形填充会关闭抗锯齿（L44-L46，轴对齐矩形边缘正好落在几何边界上，开抗锯齿反而发糊）；填充规则 `NonZero/EvenOdd` 映射为 `Winding/EvenOdd`（L48-L51）；描边宽度为 0 时跳过（L61）。
- [crates/typst-render/src/shape.rs:L87-L113](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-render/src/shape.rs#L87-L113)：`convert_curve` 把 Typst 的 `Curve`（Move/Line/Cubic/Close）逐项转成路径命令。它同时被 `render_group` 复用来转换裁剪曲线。
- [crates/typst-render/src/shape.rs:L131-L138](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-render/src/shape.rs#L131-L138)：虚线模式的适配——PDF 允许奇数长度的 dash 数组，tiny-skia 只接受偶数，这里把数组复制一遍补齐。

**文字：两条字形路径**：

- [crates/typst-render/src/text.rs:L15-L41](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-render/src/text.rs#L15-L41)：`render_text` 遍历整形产物 `TextItem` 的字形列表，按 `x_advance/y_advance` 累积笔位。每个字形先问 `should_outline`（来自 `typst_library::text::color`）：普通字形走轮廓描边；彩色字形（COLR/SVG/位图 emoji）由 `glyph_frame` 取出内部小 Frame，**递归调用 `render_frame`** 画出来——图元模型的递归性在这里被漂亮地复用了。
- [crates/typst-render/src/text.rs:L44-L102](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-render/src/text.rs#L44-L102)：`render_outline_glyph` 的"慢路径"——当字号对应的 `ppem > 100`、变换含错切、XY 缩放不一致或文字带描边时，用 `ttf_parser` 的 `OutlineBuilder` 把字形轮廓直接转成路径填充。注意 L77 的 `pre_scale(scale, -scale)`：字体设计坐标系 Y 轴向上，而画布 Y 轴向下，必须垂直翻转。
- [crates/typst-render/src/text.rs:L104-L124](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-render/src/text.rs#L104-L124)：常见情形的"快路径"——用 pixglyph 库把字形栅格化为亚像素定位的覆盖位图，整个函数被 `#[comemo::memoize]` 包裹，键是字体、字形 id 和变换参数，于是同一字形在同一位置只算一次。
- [crates/typst-render/src/text.rs:L148-L239](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-render/src/text.rs#L148-L239)：`write_bitmap` 是导出器里唯一手写的像素循环：对位图覆盖区域逐像素用 `PaintSampler` 取色、按覆盖率做 alpha 乘法、再 `blend_src_over` 混合进画布（L271-L281 的两个位运算函数移植自 Skia 源码）。存在遮罩时先画到临时 pixmap 再用 tiny-skia 合成。

**图像：先缩放，再当图案**：

- [crates/typst-render/src/image.rs:L18-L65](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-render/src/image.rs#L18-L65)：`render_image` 的策略是"先把图像重采样到最终显示尺寸，再贴上去"（源码注释引用了 issue #1404 的讨论），避免缩放锯齿。最终用 `sk::Pattern` 把纹理当作填充实现在一个矩形里。
- [crates/typst-render/src/image.rs:L68-L113](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-render/src/image.rs#L68-L113)：`build_texture` 同样被 `#[comemo::memoize]` 缓存，按 `ImageKind` 分三路：栅格图用 `image` 库重采样（放大 CatmullRom、缩小 Lanczos3、`Pixelated` 时用最近邻）；SVG 图先经 resvg 栅格化；PDF 图走 `build_pdf_texture`（用 hayro 解释渲染，标准字体来自 typst-assets，L116-L164）。

**渐变与平铺的采样抽象**：

- [crates/typst-render/src/paint.rs:L13-L22](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-render/src/paint.rs#L13-L22)：`PaintSampler` trait——"给定像素坐标返回一个颜色"。纯色实现恒返回自身。
- [crates/typst-render/src/paint.rs:L29-L79](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-render/src/paint.rs#L29-L79)：`GradientSampler`。构造时按渐变的 `RelativeTo` 决定容器尺寸与"变换回容器坐标系"的矩阵（缓存逆变换，避免逐像素重算）；`sample` 时把像素坐标映射回容器坐标系再查询渐变颜色。这正是 `State.container_transform` 存在的理由：`RelativeTo::Parent` 的渐变要以第一个 hard 帧（通常是页面）为基准着色。

**PPI 从哪里来**：

- [crates/typst-render/src/format.rs:L80-L85](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-render/src/format.rs#L80-L85)：`PngFormat` 是一个 `#[elem(name = "png")]` 格式元素，字段 `ppi` 默认 144，可在文档里 `#set png(ppi: ..)` 修改。
- [crates/typst-render/src/lib.rs:L106-L114](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-render/src/lib.rs#L106-L114)：`RenderOptions::resolve` 把调用方的部分选项（Partial）与文档级默认（Complete）合并——CLI 的 `--ppi` 与文档里的 `#set png(ppi:)` 在这里汇合，文档设置优先填充未指定的项。

#### 4.2.4 代码实践

**实践目标**：验证 PPI 换算公式与画布尺寸计算。

**操作步骤**：

1. 编写 `pixel.typ`：

   ```typst
   #set page(width: 200pt, height: 100pt)
   Hello, pixels!
   ```

2. 分别执行：

   ```bash
   cargo run -p typst-cli -- compile pixel.typ pixel-144.png            # 默认 144 PPI
   cargo run -p typst-cli -- compile pixel.typ pixel-288.png --ppi 288
   ```

3. 查看两个文件的像素尺寸（文件属性、`file pixel-144.png` 或任意图像查看器）。

**需要观察的现象**：两个 PNG 的尺寸。

**预期结果**：按公式 \( 200 \times \frac{144}{72} = 400 \)、\( 100 \times \frac{144}{72} = 200 \)，第一个应为 400×200；第二个翻倍为 800×400。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么小字号字形不走 tiny-skia 的路径填充，而要用 pixglyph 加手工混合？

**答案**：小字号下字形的路径填充容易显得模糊或生硬，pixglyph 以亚像素精度栅格化出覆盖位图，配合手工的 src-over 混合能得到更清晰的文字边缘；同时整个栅格化被 comemo 记忆化，重复编译时零成本。只有大字号（`ppem > 100`）或变换异常（错切、非等比缩放、描边文字）时才值得退回路径填充。

**练习 2**：`render_group` 里 `FrameKind::Hard` 与 `Soft` 的区别处理是为了什么？

**答案**：为了 `RelativeTo::Parent` 的渐变/平铺着色。硬帧（页面、块级盒子）是渐变的"容器"边界，进入硬帧时必须重置 `container_transform` 与 `size`，让后续采样器算出的"父坐标系"落在正确的容器上；软帧只是变换的传递，不改变容器语义。

**练习 3**：`render_image` 为什么先把图像缩放到最终尺寸再绘制，而不是直接把原图当图案贴？

**答案**：直接贴大图再由绘制器缩放，采样质量取决于绘制器的滤镜且常出现摩尔纹/锯齿；先在 CPU 上用高质量滤镜（Lanczos3/CatmullRom）重采样到目标尺寸，再以 1:1 贴图，输出质量更好。顺带的好处是纹理更小、缓存更省。

### 4.3 SVG 序列化：typst-svg 如何把图元写成矢量

#### 4.3.1 概念说明

SVG 导出的本质是**序列化**而非绘制：把 Frame 树翻译成一棵 XML 元素树，几何保持矢量形式，最终由查看器（浏览器、resvg）负责渲染。这带来三个结构性差异：

1. **字形是共享资源**：PNG 里每个字形实例都要画一遍像素；SVG 里字形轮廓写进 `<defs>` 的 `<symbol>`，正文用 `<use>` 引用——同一字形出现一百次也只存一份轮廓。
2. **去重贯穿一切**：渐变、裁剪路径、平铺同样走哈希去重，多个 `<defs>` 表由 `Deduplicator` 管理。
3. **输出必须确定**：数字格式化统一处理（定点化、整数快捷路径），这是测试体系用哈希引用 SVG 参考的前提。

#### 4.3.2 核心流程

```text
svg(page, opts)：
  1. 计算（含 bleed 的）页面尺寸
  2. 建 SvgRenderer（七张去重表）与 XmlWriter
  3. 写 <svg> 头：viewBox、width/height（pt 单位）、命名空间
  4. render_page：背景矩形 + 递归 render_frame
  5. finalize：把 glyphs/clip_paths/gradients/gradient_refs/
     conic_subgradients/tilings/tiling_refs 七张表写进 <defs>
  6. xml.end_document() 收尾，返回 String
```

多页合并的 `svg_merged` 把每页放进一个带纵向平移的 `<g>`，页间留 gap——这是测试体系 `svg` stage 的调用入口。

#### 4.3.3 源码精读

**入口与头部**：

- [crates/typst-svg/src/lib.rs:L35-L46](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/lib.rs#L35-L46)：`svg` 入口——`#[typst_macros::time(name = "svg")]` 计时后，走上面流程的六步。
- [crates/typst-svg/src/lib.rs:L457-L486](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/lib.rs#L457-L486)：`svg_header_with_custom_attrs`。尺寸被钳制到至少 1pt（注释说明：resvg 等解析器处理 0 尺寸 SVG 会出问题）；`viewBox` 与 `width/height` 都用 pt 数值，保证物理尺寸语义与 PDF 一致。
- [crates/typst-svg/src/lib.rs:L128-L157](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/lib.rs#L128-L157)：`svg_merged`——先量出总尺寸（取最宽页、累加高与 gap），再逐页以 `Transform::translate(0, y)` 渲染。

**渲染器状态：七张去重表**：

- [crates/typst-svg/src/lib.rs:L202-L239](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/lib.rs#L202-L239)：`SVGRenderer` 的字段。每张表前缀一个字符（`g` 字形、`c` 裁剪路径、`f` 渐变、`r` 渐变引用、`s` 锥形渐变子级、`t` 平铺、`p` 平铺引用），用于生成互不冲突的 id。
- [crates/typst-svg/src/lib.rs:L492-L537](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/lib.rs#L492-L537)：`Deduplicator`——`IndexMap<u128, T>` 以 `typst_utils::hash128` 为键，`insert_with` 命中即复用、未命中才调用构造闭包。
- [crates/typst-svg/src/lib.rs:L540-L565](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/lib.rs#L540-L565)：`DedupId` 的显示实现——前缀字符 + 哈希的十六进制（去掉前导 0），这就是你在 SVG 文件里看到的 `g1A2B...` 这类 id 的来源。

**页面与组**：

- [crates/typst-svg/src/lib.rs:L300-L321](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/lib.rs#L300-L321)：`render_page`——非恒等变换才输出 `<g transform>`；背景是覆盖"页面 + bleed"的矩形，位置反向偏移 bleed。
- [crates/typst-svg/src/lib.rs:L342-L374](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/lib.rs#L342-L374)：`render_group`。与栅格化侧对称地处理软硬帧：硬帧**总是**生成 `<g>` 并把状态变换重置为恒等（L347-L358），保证组内坐标从零开始；裁剪曲线进 `clip_paths` 去重表后以 `clip-path="url(#...)"` 引用（L365-L371）；组的标签写成 `data-typst-label` 属性。与 PNG 侧的遮罩求交不同，SVG 的裁剪组合由查看器解析 `clip-path` 时自动完成。

**形状与描边**：

- [crates/typst-svg/src/shape.rs:L13-L48](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/shape.rs#L13-L48)：`render_shape`——每种形状就是一个 `<path>` 元素：`write_fill` 决定 `fill` 属性、`write_stroke` 决定描边属性组、变换非恒等时加 `transform`，最后几何序列化成 `d` 属性。
- [crates/typst-svg/src/shape.rs:L128-L173](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/shape.rs#L128-L173)：`write_stroke` 把 `FixedStroke` 逐项映射为 SVG 属性（`stroke-width`、`stroke-linecap`、`stroke-linejoin`、`stroke-miterlimit`、`stroke-dasharray`）。纯色直接写颜色值，渐变/平铺写 `url(#id)` 引用。
- [crates/typst-svg/src/shape.rs:L177-L201](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/shape.rs#L177-L201)：`convert_geometry_to_path` 也被 `#[comemo::memoize]` 缓存——同一几何重复出现时路径字符串只生成一次。

**填充与渐变的双层结构**：

- [crates/typst-svg/src/paint.rs:L32-L57](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/paint.rs#L32-L57)：`write_fill`——纯色写 `fill`，渐变/平铺写 `url(#id)`；填充规则映射为 `nonzero/evenodd`。
- [crates/typst-svg/src/paint.rs:L66-L85](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/paint.rs#L66-L85)：`push_gradient` 是省空间的关键设计：**不带变换**的"源渐变"进 `gradients` 表；只有当需要变换时才在 `gradient_refs` 表里登记一条"引用 + `gradientTransform`"的记录。同一个渐变在不同变换下使用 N 次，渐变定义本身只写一份。
- [crates/typst-svg/src/paint.rs:L318-L338](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/paint.rs#L318-L338)：`write_gradient_refs`——引用记录写成带 `href`（及兼容的 `xlink:href`）的渐变元素，靠 SVG 的引用机制复用源渐变。

**文字：symbol + use**：

- [crates/typst-svg/src/text.rs:L16-L23](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/text.rs#L16-L23)：`RenderedGlyph` 两形态——`Path`（轮廓路径字符串）与 `Frame`（彩色字形的内部小帧）。
- [crates/typst-svg/src/text.rs:L29-L53](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/text.rs#L29-L53)：`render_text`——整个文字运行包在一个 `<g>` 里，先做 Y 翻转（字体坐标系 Y 向上，L38），再按 `x_advance/y_advance` 累积笔位逐字形处理。
- [crates/typst-svg/src/text.rs:L55-L94](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/text.rs#L55-L94)：`render_glyph` 与 PNG 侧同样先问 `should_outline`。轮廓字形的去重键是 `(字体, 字形 id, 缩放)`——路径**预先按字号缩放**存入表中（这样描边和渐变不需要考虑字号）；图像字形的键只有 `(字体, 字形 id)`，缩放推迟到使用处（彩色字形通常很大，按字号各存一份太浪费）。
- [crates/typst-svg/src/text.rs:L117-L163](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/text.rs#L117-L163)：`render_path_glyph`——正文里每个字形实例是一个 `<use xlink:href="#id" x=".." y="..">`，填充/描边属性写在这个 use 上；`text_paint_transform`（L165-L181）负责为文字上的渐变/平铺算出正确的着色变换。
- [crates/typst-svg/src/text.rs:L184-L219](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/text.rs#L184-L219)：`write_glyph_defs`——`finalize` 阶段把字形表展开成 `<defs>` 里一串 `<symbol id=".." overflow="visible">`：路径字形内是 `<path d="..">`，帧字形内是递归渲染的形状或图像。

**图像与链接**：

- [crates/typst-svg/src/image.rs:L20-L42](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/image.rs#L20-L42)：`render_image`——图像写成 `<image xlink:href="data:{mime};base64,..." width height>`，即**整份数据 base64 内嵌**。`image-rendering` 样式属性承接 `Pixelated/Smooth` 缩放设置（L46-L56）。
- [crates/typst-svg/src/image.rs:L100-L142](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/image.rs#L100-L142)：`WebImage::new`（memoize）按图像种类准备 web 可用的格式：栅格图原样或转 PNG（保留 ICC）；SVG 原样内嵌；PDF 图先用 hayro 转成 SVG。`to_base64_url`（同样 memoize）生成数据 URL。
- [crates/typst-svg/src/lib.rs:L377-L415](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/lib.rs#L377-L415)：`render_link`——`<a href>` 里放一个透明填充、无描边的矩形作为热区；页内 `Position`/`Location` 链接在纯 SVG 场景暂不支持（TODO 注释），跨文档链接经 `link_resolver` 解析（bundle/HTML 场景，见 `svg_in_bundle`/`svg_in_html`，L48-L123）。

**写出的工程细节**：

- [crates/typst-svg/src/write.rs:L8-L54](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/write.rs#L8-L54)：`SvgElem` 是 RAII 封装——构造时 `start_element`，`Drop` 时 `end_element`，XML 的闭合永不遗漏。
- [crates/typst-svg/src/write.rs:L57-L90](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/write.rs#L57-L90)：`LazySvgElem`——`<g>` 只有在实际需要写属性时才真正创建（`init()`），避免输出大量空分组。
- [crates/typst-svg/src/write.rs:L104-L120](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/write.rs#L104-L120)：`push_num` 把数字四舍五入到 \(10^{-9}\) 精度、整数走 itao、小数走 ryu——既省空间又保证**同样的 Frame 永远序列化出同样的字节**，这是测试哈希引用的根基。

**pretty 选项**：

- [crates/typst-svg/src/format.rs:L65-L73](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/format.rs#L65-L73)：`SvgFormat` 元素只有一个字段 `pretty`（默认 false），文档里 `#set svg(pretty: true)` 可让输出带两空格缩进（对应 [crates/typst-svg/src/lib.rs:L166-L176](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/lib.rs#L166-L176) 的 `xml_options`）。

#### 4.3.4 代码实践

**实践目标**：亲眼看到 `<defs>` 去重与 `<use>` 引用机制。

**操作步骤**：

1. 编写 `vector.typ`：

   ```typst
   #set page(width: 180pt, margin: 20pt)
   #set svg(pretty: true)
   #let g = gradient.linear(red, blue)
   aaa *aaa* aaa
   #rect(width: 60pt, height: 20pt, fill: g)
   #rect(width: 60pt, height: 20pt, fill: g, stroke: 1pt)
   ```

2. 导出并打开：

   ```bash
   cargo run -p typst-cli -- compile vector.typ vector.svg
   ```

3. 在文本编辑器里阅读 `vector.svg`（已开启 pretty 缩进）。

**需要观察的现象**：文件末尾 `<defs>` 区域的内容；正文中字形与渐变的引用方式。

**预期结果**：应能看到——(a) 字母 `a` 的多个实例在正文中都是 `<use>`，而 `<defs>` 里每种（ regular 与 bold 的）`a` 字形只有一个 `<symbol>`；(b) 两个矩形共享同一个线性渐变定义，描边矩形可能多出一条带 `gradientTransform` 的引用记录；(c) `<svg>` 头部的 `width/height` 是 pt 数值。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么渐变要拆成"源渐变 + 带变换的引用"两层？

**答案**：同一个渐变（颜色站、角度相同）经常在文档里以不同变换（不同位置/尺寸的容器）被反复使用。源定义只与渐变内容和纵横比有关，可以跨使用点共享；变换部分是一条薄薄的引用记录（`href` + `gradientTransform`）。若不做拆分，每次使用都要完整重写一份渐变定义，文件会显著膨胀。

**练习 2**：`push_num` 为什么要做定点化和整数/小数分流？

**答案**：两个动机。其一，省空间——排版坐标里大量整数和短小数，itoa/ryu 的输出比 `f64` 默认格式更紧凑。其二，确定性——四舍五入到固定精度后，同一 Frame 永远得到同一字符串，SVG 输出的哈希才稳定，`tests/` 里的 svg stage 才能用哈希引用代替文件引用。

**练习 3**：轮廓字形的去重键含缩放（字号），图像字形的去重键不含，为什么不对称？

**答案**：轮廓字形预先缩放后，使用处的填充/描边就不必再考虑字号（尤其描边宽度和渐变着色），正确性优先；而彩色字形（COLR/SVG/位图）本身体积大，若每个字号各存一份会急剧膨胀，所以在 `<use>` 的 `transform` 上现场缩放，牺牲一点语义简洁换取体积。

### 4.4 与 tests/ 的 render/svg stage 对应

#### 4.4.1 概念说明

第一单元（u1-l5）讲过测试体系的 stage 树：`eval → paged → {render, pdf → pdftags, svg}`。`paged` 属性的测试会编译出 `PagedDocument`，然后同时走 `render`（PNG 视觉对照）、`pdf`、`svg` 三条输出线。本节把这两条线与源码接上：`render` stage 调用的就是 `typst_render::render_merged`，`svg` stage 调用的就是 `typst_svg::svg_merged`——测试体系没有独立的渲染实现。

#### 4.4.2 核心流程

```text
一个 paged 测试的输出管线：
  编译 → PagedDocument
    ├─ render stage：render_merged(72 PPI, 黑底, 1pt 页距) → Pixmap
    │    → encode_png → 与 ref/*.png 逐像素比对（文件引用，≤20 KiB）
    └─ svg stage：svg_merged(pretty, 1pt 页距) → String
         → hash128 → 与 hashes.txt 中的哈希比对（哈希引用）
```

CLI 侧的对应入口是 `compile.rs::export_image`：PNG 与 SVG 两个分支分别调用 `typst_render::render` 与 `typst_svg::svg`，按页并行导出，多页时要求输出路径带 `{p}` 页码模板。

#### 4.4.3 源码精读

**测试侧的 render 输出类型**：

- [tests/src/output.rs:L180-L220](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/src/output.rs#L180-L220)：`Render` 的 `make_live` 调用下面的 `render(doc, 1.0)`——即 1 pixel/pt，折合 72 PPI；哈希直接对像素数据 `hash128(live.data())` 计算；参考文件是 PNG（文件引用）。
- [tests/src/output.rs:L601-L629](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/src/output.rs#L601-L629)：`render` 辅助函数。先做安全检查（任一页超过 100cm 直接 panic，防止超大参考图），然后 `render_merged(document, opts, gap=1pt, fill=BLACK)`——**黑底**是刻意的：页面若透明能在黑底上显形。注意它还额外调用 `render_links`（L631 起）给每个链接画一个框，这是测试专有的可视化，不在 typst-render 里——因为 PNG 导出本来丢弃 `Link`，测试想验证链接确实存在。

**测试侧的 svg 输出类型**：

- [tests/src/output.rs:L405-L444](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/src/output.rs#L405-L444)：`Svg` 的 `make_live` 用 `pretty: true` 的选项调用 `svg_merged(doc, opts, 1pt)`；参考走 `hashes.txt` 哈希引用；失败报告里同时生成图像 diff（按 svg+xml 渲染比对）和文本 diff。

**stage 树文档**：

- [tests/README.md:L57-L84](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/tests/README.md#L57-L84)：stage 列表与树形图，`--stages render,svg` 这种逗号分隔写法的出处。

**CLI 侧的接线**：

- [crates/typst-cli/src/compile.rs:L493-L525](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/compile.rs#L493-L525)：`export_image` 的两个分支——先 `resolve` 合并文档级格式选项，再逐页调 `typst_render::render` + `encode_png` 或 `typst_svg::svg` 写文件。多页并行与页码模板在随后的 `export_image_pages`（L529 起）里。
- `crates/typst-cli/src/world.rs`（[L70-L71](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/world.rs#L70-L71)）：构建 Library 时把 `typst_svg::FORMAT` 与 `typst_render::FORMAT` 注册进格式清单，`#set png/svg(...)` 才能被文档选项系统收集到——这与上一讲 PDF 的 `typst_pdf::FORMAT` 注册是同一机制（u5-l3 将展开）。

#### 4.4.4 代码实践

**实践目标**：跑通一个真实视觉测试的 render 与 svg 两个 stage，并找到参考产物。

**操作步骤**：

1. 在仓库根目录执行（`rect` 是 `tests/suite/visualize/rect.typ` 里的第一个测试，属性 `paged`）：

   ```bash
   cargo testit --exact rect --stages render,svg
   ```

2. 观察输出中的 pass/fail 计数。
3. 打开参考产物：`tests/ref/visualize/rect.png`（render 参考）与 `tests/ref/visualize/rect_svg` 目录下的 `hashes.txt`（svg 参考，具体目录组织以仓库实际为准）。
4. 选做：临时修改 `tests/suite/visualize/rect.typ` 里第一个测试（比如给 `#rect()` 加 `stroke: 1pt`），重跑命令观察失败报告 `tests/store/report.html` 中的像素 diff；用 `--update` 更新参考后还原源码。

**需要观察的现象**：测试被精确过滤到单个；render 与 svg 两个输出都被执行；失败时报告里的图像/文本差异。

**预期结果**：修改后 render 参考对不上（多出描边）而失败，`--update` 后重新通过。待本地验证（`cargo testit` 别名与测试套件依赖本地构建，本讲义写作环境未运行）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 render 的参考是 PNG 文件，而 svg 的参考是哈希？

**答案**：与 u1-l5 的结论一致：`render`/`bundle` 用文件引用（并限制 ≤20 KiB，`large` 属性可豁免），`pdf`/`svg`/`html`/`pdftags` 用 `hashes.txt` 哈希引用。合理推测是：PNG 是已压缩的二进制，哈希比对与文件比对差别不大，而保留文件可以直接目视检查；SVG 是确定性文本（见 4.3.5 练习 2），哈希稳定，且体积可能很大，不适合整文件入库。

**练习 2**：测试的 `render` 辅助函数为什么要画链接框（`render_links`），而 typst-render 的 `render_frame` 直接忽略 `Link`？

**答案**：产品语义上 PNG 不承载链接，忽略是正确行为；但测试需要验证"链接确实排到了这个位置"，于是在测试运行器里遍历 Frame、给每个 `Link` 画一个可见框。这是"测试专用的额外可视化"叠加在导出器之上的典型手法。

**练习 3**：`--stages render,svg` 为什么不需要显式带上 `paged`？

**答案**：stage 构成树且蕴含前置关系——`render` 与 `svg` 都是 `paged` 的子节点，选中它们自然蕴含父 stage（编译 paged 目标产出 `PagedDocument`），这与 `pdftags` 蕴含 `pdf` 是同一规则（见 tests/README.md 的 stage 树）。

## 5. 综合实践

把本讲三条线串成一个任务：**同一份文档、两条导出路径、一次测试对照**。

1. 编写 `compare.typ`，刻意覆盖多类图元：

   ```typst
   #set page(width: 200pt, height: auto, margin: 16pt)
   #set svg(pretty: true)
   #let grad = gradient.linear(rgb("#f6d365"), rgb("#fda085"))

   = 像素与矢量                      // Text（标题字形）+ Group
   #lorem(10)                        // Text
   #rect(width: 100%, height: 24pt, fill: grad, radius: 4pt)   // Shape + 渐变
   #rect(width: 60pt, height: 12pt, stroke: 1pt + blue)        // Shape + 描边
   #link("https://typst.app")[Typst 官网]                       // Link
   ```

2. 导出两种格式：

   ```bash
   cargo run -p typst-cli -- compile compare.typ compare.png --ppi 144
   cargo run -p typst-cli -- compile compare.typ compare.svg
   ```

3. **对比图元结构**：打开 `compare.svg`（pretty 已开启），在 `<defs>` 里找出渐变定义与去重后的字形 `<symbol>`，在正文里找出引用它们的 `<use>`/`url(#..)`、`<a>` 链接与 `data-typst-label`；再打开 `compare.png`，体会同一份 Frame 在像素世界里的"摊平"——那里没有可点击链接、没有共享 symbol，只有覆盖排序的像素。
4. **跑测试对照**：执行 `cargo testit --exact rect-customization --stages render,svg`（`rect-customization` 是 rect.typ 中含填充与描边的测试），确认测试体系复用的正是本讲读过的 `render_merged`/`svg_merged` 两个入口；有兴趣可以继续用 `--stages render --update` 体验参考更新流程（改完记得还原）。

预期：你能对任意一页 Typst 输出，同时预言它的 PNG 绘制顺序与 SVG 元素结构。待本地验证。

## 6. 本讲小结

- typst-render 与 typst-svg 是同一份 `PagedDocument` 的两个纯消费者：`FrameItem` 六类图元（Group/Text/Shape/Image/Link/Tag）是导出层的完整词汇表，两侧的遍历分发几乎镜像，差异只在"落点"是像素还是 XML。
- 栅格化入口 `render` 以 \( \text{pixel\_per\_pt} = \mathrm{ppi}/72 \) 换算画布尺寸，`State` 携带变换、容器变换（服务于 `RelativeTo::Parent` 渐变）、遮罩与像素比贯穿递归；形状走 tiny-skia 路径先填充后描边，小字号字形走 pixglyph + 手工 src-over 混合的快路径，图像先重采样成纹理再当图案贴。
- SVG 侧是序列化而非绘制：`SVGRenderer` 用七个 `Deduplicator` 管理字形、渐变、平铺、裁剪路径等共享资源，字形以 `<symbol>` + `<use>` 复用，渐变拆成"源定义 + 带变换引用"两层，数字定点化保证输出字节确定。
- 两者的格式选项（`#set png(ppi:)`、`#set svg(pretty:)`）经 Format 元素机制与 CLI 选项在 `resolve` 中合并，注册发生在构建 Library 时。
- `tests/` 的 `render` stage 调 `render_merged`（72 PPI、黑底、额外画链接框），`svg` stage 调 `svg_merged`（pretty）；render 参考是文件引用、svg 参考是哈希引用——两条线均无独立实现，与你读过的源码一一对应。

## 7. 下一步学习建议

- 下一讲（u5-l3）进入 **Format 注册与 HTML 导出**：本讲反复出现的 `FORMAT` 常量、`FormatElement`、`Fields/Partial/Complete` 与 `document.options().get::<PngFormat>()` 正是那讲的伏笔，建议先回头精读 [crates/typst-render/src/format.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-render/src/format.rs) 与 [crates/typst-svg/src/format.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-svg/src/format.rs)。
- 想深入"同样的 Frame 换一种序列化"，对比阅读 `typst-svg` 与上一讲的 `typst-pdf/src/convert.rs`：两者都在做"图元翻译"，但目标语言的资源模型（`<defs>` 引用 vs PDF 对象编号）决定了不同的去重策略。
- 想理解 SVG 中字形轮廓的最终来源，可回看 u4-l5（行内排版与整形）：`TextItem` 的字形列表、`x_advance` 累积正是在那里产出的。
- 扩展阅读：`crates/typst-svg/src/write.rs` 的 RAII 写出模式与 `LazySvgElem` 是"生成结构化文本"时可直接借鉴的工程手法。
