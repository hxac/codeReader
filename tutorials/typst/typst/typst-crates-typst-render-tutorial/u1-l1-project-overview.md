# 项目概览与定位

## 1. 本讲目标

本讲是 typst-render 学习手册的第一篇。读完本讲,你应当能够:

- 说清楚 **typst-render 这个 crate 到底做什么**:它的输入是什么、输出是什么、在 Typst 整体架构里处于什么位置。
- 认识它依赖的 **外部渲染库**(tiny-skia、pixglyph、resvg、hayro、image 等),并理解每个库负责哪一块工作。
- 读懂它的 **公共入口** `render()` 与配置结构 `RenderOptions`,并理解 typst-cli 在导出 PNG 时是如何调用它的,以及 `pixel_per_pt = ppi / 72.0` 这个换算的物理含义。

本讲不深入任何渲染算法细节,只帮你建立「全局观」。具体的坐标系、裁剪、形状、文本等机制会在后续讲义中逐一展开。

## 2. 前置知识

在开始之前,你需要了解以下几个概念。它们都不复杂,但对理解本讲很有帮助。

### 2.1 什么是「光栅图像」与「矢量」

- **矢量图形(vector)**:用数学公式描述形状,比如「一条从 (0,0) 到 (10,10) 的线段」。它放大不会失真。Typst 内部排版产出的 `Frame`(场景树)就是矢量描述。
- **光栅图像(raster image)**:由一个个带颜色的像素点组成的网格,也就是我们常见的 PNG、JPG。放大到一定程度会看到「马赛克」。

typst-render 的核心职责,就是把 Typst 排版出来的**矢量场景**(`Frame`)转换成**像素网格**(`Pixmap`),最终可以编码成 PNG。这个过程叫**光栅化(rasterization)**。

### 2.2 长度单位 pt 与 inch

排版行业常用 **点(point,缩写 pt)** 作为长度单位,约定:

\[ 1 \text{ inch} = 72 \text{ pt} \]

一张 A4 纸的尺寸约为 595 × 842 pt。本讲后面计算 PNG 输出尺寸时会用到这个换算。

### 2.3 Rust workspace 与 crate

Typst 是一个 **Cargo workspace**,里面包含很多互相协作的 crate(可以理解成模块化的子项目),例如:

- `typst`:核心编译引擎
- `typst-layout`:把文档排成页面/帧
- `typst-render`:把帧渲染成位图(本讲义的主角)
- `typst-pdf` / `typst-svg` / `typst-html`:其它导出器
- `typst-cli`:命令行入口,串起整个流程

typst-render 就是这个家族里专门负责「输出 PNG」的那一位。

## 3. 本讲源码地图

本讲涉及的关键文件如下:

| 文件 | 作用 |
| --- | --- |
| `crates/typst-render/Cargo.toml` | 声明 crate 名称、描述与所有依赖(内部 crate 与外部渲染库) |
| `crates/typst-render/src/lib.rs` | crate 根文件:定义 `render()`、`render_merged()`、`RenderOptions`、`State`、`render_frame` 等核心入口 |
| `crates/typst-cli/src/compile.rs` | 命令行的编译/导出流程:其中的 `export_image_page` 调用 `typst_render::render` 完成 PNG 导出 |
| `crates/typst-cli/src/args.rs` | 定义命令行参数,其中 `--ppi` 的默认值决定了 PNG 的分辨率 |

> 提示:typst-render 本身体量很小,`src/` 目录下一共只有 5 个文件(`lib.rs` + `image.rs` / `paint.rs` / `shape.rs` / `text.rs`),但每个文件内部机制都很密集。本讲只看 `lib.rs` 的「门面」部分。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块:

1. typst-render 的定位:它在架构中扮演「光栅图像导出器」
2. 依赖全景:外部渲染库各司其职
3. 渲染入口 `render()` 与 `RenderOptions`,以及 CLI 调用链

---

### 4.1 typst-render 的定位:光栅图像导出器

#### 4.1.1 概念说明

Typst 把一份源文件(`.typ`)变成最终成品,要经历两大阶段:

1. **编译 + 排版**:源码 → 抽象语法树 → 内容 → 页面布局。排版阶段的产物是一种叫 **`Frame`(帧)** 的层级化场景树,里面记录了「在哪个位置放一段文字 / 一个形状 / 一张图片」。
2. **导出**:把 `Frame` 转成用户想要的格式。不同的导出器对应不同的目标格式:

| 导出器 | 输出格式 | 性质 |
| --- | --- | --- |
| `typst-pdf` | PDF | 矢量 |
| `typst-svg` | SVG | 矢量 |
| `typst-html` | HTML | 标记语言 |
| **`typst-render`** | **PNG(位图)** | **光栅** |

也就是说,typst-render 是**唯一**把排版结果变成「像素位图」的导出器。它的输入是已经排好版的 **`Page`**(一页),输出是一个 **`tiny-skia::Pixmap`**(一个像素缓冲区),后者可以被进一步编码成 PNG 文件。

一句话总结它的角色:**typst-layout 负责「排」,typst-render 负责「画成像素」。**

#### 4.1.2 核心流程

从最宏观的层面看,typst-render 的工作流是一条直线:

```text
Page (排版结果,含一个 Frame 场景树)
        │
        ▼
typst_render::render(page, opts)
        │  1. 根据页面尺寸 + pixel_per_pt 计算像素画布大小
        │  2. 创建 Pixmap 画布并填充背景
        │  3. 递归遍历 Frame 场景树,把每个元素光栅化到画布
        ▼
tiny_skia::Pixmap (像素缓冲区)
        │
        ▼
Pixmap::encode_png()  →  PNG 文件
```

typst-render 内部把不同类型的场景元素交给不同子模块处理,这些子模块在 `lib.rs` 顶部声明:

```rust
mod image;  // 处理图像(光栅/SVG/PDF)
mod paint;  // 处理填充(纯色/渐变/平铺)
mod shape;  // 处理几何形状与描边
mod text;   // 处理文本字形
```

本讲暂不进入这些子模块,后续讲义会逐个拆解。

#### 4.1.3 源码精读

先看 crate 的「身份证」——`Cargo.toml`。它的 `description` 字段一句话点明了定位:

> [Cargo.toml:L1-L3](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/Cargo.toml#L1-L3) — 包名为 `typst-render`,描述为 "Raster image exporter for Typst."(Typst 的光栅图像导出器)。

再看 `src/lib.rs` 顶部的模块注释和模块声明,它把 crate 的内部结构交代得很清楚:

> [src/lib.rs:L1-L7](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L1-L7) — 顶部注释 "Rendering of Typst documents into raster images." 紧接着声明四个子模块 `image / paint / shape / text`,并 `use tiny_skia as sk;`。

关键观察:

- `tiny_skia` 被引入为别名 `sk`,这说明 typst-render **几乎所有光栅化工作都建立在 tiny-skia 之上**——它提供画布(`Pixmap`)、路径(`Path`)、变换(`Transform`)、遮罩(`Mask`)等基础图元。
- 输入类型 `Page` / `PagedDocument` 来自 `typst_layout`,印证了「排版结果由 typst-layout 产出」这条依赖关系。

#### 4.1.4 代码实践

**实践目标**:从源码层面确认 typst-render 的输入输出类型,加深「它把 Page 变成 Pixmap」的印象。

**操作步骤**:

1. 打开 `src/lib.rs`,定位到 `pub fn render(...)` 的签名(下面 4.3 节会精读)。
2. 只看它的参数类型和返回类型,不要看函数体。
3. 回答:输入是什么类型?输出是什么类型?

**预期结果**:你会看到签名形如 `pub fn render(page: &Page, opts: &RenderOptions) -> sk::Pixmap`,即输入一页排版结果加渲染选项,输出一个 tiny-skia 的像素缓冲区。

#### 4.1.5 小练习与答案

**练习 1**:typst-render 和 typst-svg 都把 `Frame` 转成最终产物,它们的本质区别是什么?

> **参考答案**:typst-svg 输出的是**矢量**格式(SVG,由路径/文本等数学描述构成,放大不失真);typst-render 输出的是**光栅**格式(PNG,由固定数量的像素构成,放大会失真)。

**练习 2**:为什么 typst-render 需要一个 `pixel_per_pt` 参数,而 typst-svg 不需要?

> **参考答案**:矢量格式(SVG/PDF)记录的是「形状本身」,分辨率无意义(显示时由阅读器决定);而光栅格式(PNG)必须事先决定生成多少个像素,因此需要一个「每 pt 多少像素」的分辨率参数。这正是 `pixel_per_pt` 的作用。

---

### 4.2 依赖全景:外部渲染库各司其职

#### 4.2.1 概念说明

typst-render 自己并不从零实现光栅化算法,而是**组合了多个成熟的开源渲染库**,让每个库干自己最擅长的事。理解这些依赖,就理解了 typst-render 的能力边界。

下面这些是与「渲染」直接相关的外部 crate(以及内部 crate):

| 依赖 | 类别 | 职责(一句话) |
| --- | --- | --- |
| **tiny-skia** | 外部 | 2D 光栅化引擎:提供画布 `Pixmap`、路径 `Path`、变换 `Transform`、遮罩 `Mask`、描边 `Stroke` 等基础图元,是 typst-render 的核心地基 |
| **pixglyph** | 外部 | 字形光栅化:把字体的矢量轮廓高精度地渲染成抗锯齿的 alpha 位图,专用于文字 |
| **resvg** | 外部 | SVG 渲染:当文档里嵌入一张 SVG 图像时,用它把 SVG 栅格化成纹理 |
| **hayro** | 外部 | PDF 渲染:当文档里嵌入一张 PDF 图像时,用它把 PDF 栅格化成纹理 |
| **image** | 外部 | 光栅图像(如 PNG/JPG)的解码,以及在缩放时提供不同的重采样滤镜(Lanczos3、CatmullRom、Nearest) |
| **ttf-parser** | 外部 | 解析字体表(字形度量、units-per-em 等),配合字形处理 |
| **comemo** | 外部 | 记忆化(memoization)缓存,用于缓存字形光栅化、纹理构建、渐变采样等重复计算 |
| **bytemuck** / **libm** | 外部 | 辅助:字节级类型转换 / 数学函数 |
| typst-layout / typst-library / typst-utils / typst-macros / typst-timing / typst-assets | 内部 | 提供 `Page`/`Frame` 排版结果、基础类型、过程宏(如 `#[time]`)、计时、内置资源等 |

> 名词解释:**重采样滤镜(resampling filter)** 是缩放图像时决定「新旧像素如何对应」的算法。放大时常用 CatmullRom(平滑),缩小时常用 Lanczos3(锐利、防走样),像素风格用 Nearest(最近邻,保留硬边)。这会在「图像渲染」一讲详细展开。

#### 4.2.2 核心流程

这些依赖之间的协作关系可以画成一张分工图:

```text
                ┌─────────────── typst-render ───────────────┐
   Page/Frame ──▶│                                            │──▶ Pixmap
                │  tiny-skia  ← 画布/路径/变换/遮罩/描边(地基)  │
                │     │                                      │
                │     ├──▶ pixglyph     ← 文字字形光栅化        │
                │     ├──▶ resvg        ← SVG 图像栅格化        │
                │     ├──▶ hayro        ← PDF 图像栅格化        │
                │     └──▶ image        ← 光栅图解码 + 重采样    │
                │                                            │
                │  comemo ← 缓存字形/纹理/渐变的重复计算         │
                └────────────────────────────────────────────┘
```

要点:tiny-skia 是「地基」,其它库都是在「给地基填充内容」(字形、SVG、PDF、普通图像),最后统一画到 tiny-skia 的 `Pixmap` 上。

#### 4.2.3 源码精读

`Cargo.toml` 的 `[dependencies]` 段完整列出了这些依赖(全部通过 `{ workspace = true }` 从 workspace 继承版本):

> [Cargo.toml:L15-L30](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/Cargo.toml#L15-L30) — `[dependencies]` 段:先列出内部 crate(`typst-assets` / `typst-layout` / `typst-library` / `typst-macros` / `typst-timing` / `typst-utils`),再列出外部 crate(`bytemuck` / `comemo` / `hayro` / `image` / `libm` / `pixglyph` / `resvg` / `tiny-skia` / `ttf-parser`)。

可以看到渲染相关的几个关键外部库——`tiny-skia`、`pixglyph`、`resvg`、`hayro`、`image`——都集中在这里。后续讲义会逐个看到它们在 `image.rs`、`text.rs` 等文件中被实际调用。

#### 4.2.4 代码实践

**实践目标**:亲手把 typst-render 的依赖表整理成「职责对照表」,建立直观印象。

**操作步骤**:

1. 打开 `crates/typst-render/Cargo.toml`,定位到 `[dependencies]` 段。
2. 把其中**外部**渲染相关 crate(tiny-skia / pixglyph / resvg / hayro / image)逐一挑出来。
3. 为每个库写一句话职责(可参考本讲 4.2.1 的表格,但建议你用自己的话复述)。

**预期结果**:你应当能不查资料说出「tiny-skia 是 2D 引擎,pixglyph 管字形,resvg 管 SVG,hayro 管 PDF,image 管光栅图解码与缩放」。

#### 4.2.5 小练习与答案

**练习 1**:如果一个 Typst 文档里同时包含「一段中文文字、一张 PNG 照片、一张 SVG 图标」,渲染时会分别用到哪几个外部库?

> **参考答案**:中文文字 → **pixglyph**(字形光栅化);PNG 照片 → **image**(解码与缩放);SVG 图标 → **resvg**(SVG 栅格化)。三者最终都要画到 **tiny-skia** 的画布上。

**练习 2**:`comemo` 在 typst-render 里起什么作用?为什么需要它?

> **参考答案**:`comemo` 提供**记忆化缓存**。渲染同一份文档的多个页面时,常常会反复用到相同的字形、相同的图像纹理、相同的渐变采样;把这些重复计算的结果缓存起来可以大幅提升性能。具体细节会在「记忆化与性能优化」一讲展开。

---

### 4.3 渲染入口 `render()` 与 `RenderOptions`,以及 CLI 调用链

#### 4.3.1 概念说明

typst-render 对外暴露的最重要函数是 `render()`。它接收「一页排版结果 + 渲染选项」,返回「一张像素画布」。渲染选项由结构体 `RenderOptions` 表达,目前只有两个 knob(旋钮):

- **`pixel_per_pt`**:每 pt 渲染多少像素,决定输出 PNG 的分辨率(放大倍数)。
- **`render_bleed`**:是否把「出血区(bleed)」也画出来。出血区是印刷时超出页面边缘、供裁切用的预留区域,日常屏幕预览不需要。

那么这个函数是被谁调用的?答案是 **typst-cli**(命令行)。当用户执行 `typst compile doc.typ doc.png` 时,typst-cli 会编译文档,然后对每一页调用 `typst_render::render(...)` 生成 PNG。

这里有一个关键的换算:命令行参数用的是 **`--ppi`(每英寸像素数)**,而 typst-render 内部用的是 **`pixel_per_pt`(每点像素数)**。两者之间的桥梁是:

\[ \text{pixel\_per\_pt} = \frac{\text{ppi}}{72} \]

这是因为 \(1 \text{ inch} = 72 \text{ pt}\),所以「每英寸像素数」除以「每英寸点数」就等于「每点像素数」。

#### 4.3.2 核心流程

`render()` 的执行步骤(伪代码):

```text
fn render(page, opts):
    1. bleed = 若 opts.render_bleed 则取 page.bleed,否则为零
    2. size  = page.frame.size() + bleed 之和          # 含出血区的总尺寸(pt)
    3. pxw   = round(pixel_per_pt * size.x).max(1)      # 像素宽,至少 1
       pxh   = round(pixel_per_pt * size.y).max(1)      # 像素高,至少 1
    4. ts    = Transform::from_scale(pixel_per_pt, …)   # pt→像素 的纯缩放
    5. state = State::new(size, ts, pixel_per_pt)
    6. canvas = Pixmap::new(pxw, pxh)
    7. 填充背景(纯色直接 fill;复杂 paint 走 render_shape)
    8. state = state.pre_translate(bleed 左上偏移)
    9. render_frame(canvas, state, &page.frame)         # 递归画主帧
   10. 返回 canvas
```

typst-cli 那一侧的调用链则是一层套一层:

```text
compile()
  └─ compile_once()
       └─ compile_and_export()
            └─ export_paged()        // 格式为 Png 时
                 └─ export_image()   // 遍历每一页(并行)
                      └─ export_image_page(page, …)
                           ├─ options = png_options(config)
                           ├─ pixmap = typst_render::render(page, &options)
                           └─ pixmap.encode_png()  → 写入 .png 文件
```

其中 `png_options` 把命令行的 `--ppi` 换算成 `pixel_per_pt`:

```text
fn png_options(config):
    RenderOptions {
        pixel_per_pt: Scalar::new(config.ppi / 72.0),
        render_bleed: false,
    }
```

#### 4.3.3 源码精读

先看 `render()` 本体。这里只关注「入口流程」的几行关键代码:

> [src/lib.rs:L20-L30](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L20-L30) — 函数标注了 `#[typst_macros::time(name = "render")]`(用于性能计时)。接着根据 `render_bleed` 决定出血区,计算含出血区的总尺寸 `size`,再用 `pixel_per_pt` 把 pt 尺寸换算成像素尺寸 `pxw / pxh`。

像素尺寸的计算公式是本讲的核心数学点:

\[
\text{pxw} = \max\!\big(1.0,\; \mathrm{round}(\text{pixel\_per\_pt} \times \text{size.x})\big)
\]

> [src/lib.rs:L26-L32](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L26-L32) — `pxw`/`pxh` 都套了 `.round().max(1.0)`。`.max(1.0)` 是保护:即便页面极小或 `pixel_per_pt` 极低,也保证画布至少 1×1 像素,避免 `Pixmap::new(0,0)` 失败而导致后面的 `.unwrap()` 崩溃。随后 `ts` 是一个纯缩放变换(把 pt 坐标系映射到像素坐标系),`State` 携带它进入递归渲染。

接着是背景填充与主帧渲染:

> [src/lib.rs:L34-L45](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L34-L45) — 若页面有背景色(`page.fill_or_white()`),纯色直接 `canvas.fill(...)`,复杂 paint 则构造一个全页矩形 `Geometry::Rect(size)` 走 `shape::render_shape`。最后用 `pre_translate` 把坐标原点移到出血区左上角,调用 `render_frame` 递归渲染整页场景。

再看配置结构 `RenderOptions` 及其默认值:

> [src/lib.rs:L88-L113](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L88-L113) — `RenderOptions` 含两个字段:`pixel_per_pt: Scalar` 和 `render_bleed: bool`。`Default` 实现里 `pixel_per_pt` 默认 **2.0**(即每 pt 画 2 个像素),`render_bleed` 默认 `false`。注意文档注释里提到「默认 1.0」是字段说明的通用描述,实际代码默认值是 `Scalar::new(2.0)`。

最后看 typst-cli 是如何调用它的。`export_image_page` 在 PNG 分支里调用 `typst_render::render` 并把结果编码成 PNG:

> [crates/typst-cli/src/compile.rs:L566-L582](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/compile.rs#L566-L582) — `export_image_page` 的 `Png` 分支:先用 `png_options(config)` 构造选项,再 `let pixmap = typst_render::render(page, &options);`,然后 `pixmap.encode_png()` 编码并写入文件。这就是「一页 → 一张 PNG」的落地点。

而 `png_options` 正是 ppi → pixel_per_pt 的换算处:

> [crates/typst-cli/src/compile.rs:L632-L638](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/compile.rs#L632-L638) — `png_options` 返回 `RenderOptions { pixel_per_pt: Scalar::new(config.ppi / 72.0), render_bleed: false }`。`config.ppi` 来自命令行参数 `--ppi`。

最后确认 `--ppi` 的默认值,这样换算结果才说得通:

> [crates/typst-cli/src/args.rs:L355-L357](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/args.rs#L355-L357) — `ppi` 字段标注 `#[arg(long = "ppi", default_value_t = 144.0)]`,即默认 144.0。于是 `pixel_per_pt = 144.0 / 72.0 = 2.0`,正好与 `RenderOptions` 的默认值 `2.0` 吻合——这绝不是巧合,而是 CLI 默认参数与库默认值对齐的结果。

#### 4.3.4 代码实践

**实践目标**:理解 `pixel_per_pt` 如何决定输出分辨率,以及 `ppi / 72.0` 换算的含义。

**操作步骤**:

1. 打开 `src/lib.rs` 的 `render()`(第 20–48 行),定位 `pxw`/`pxh` 的计算公式。
2. 假设把 `RenderOptions` 默认的 `pixel_per_pt` 从 `2.0` 改为 `1.0`,手算一张 A4 页面(约 595 × 842 pt)的输出像素尺寸。
3. 打开 `crates/typst-cli/src/compile.rs` 的 `png_options`(第 632–638 行),确认 `pixel_per_pt: Scalar::new(config.ppi / 72.0)`。
4. 结合 `args.rs` 中 `--ppi` 默认值 `144.0`,验证 `144 / 72 = 2.0`。

**需要观察的现象 / 预期结果**:

- `pixel_per_pt = 2.0` 时:A4 输出约为 `2.0 × 595 = 1190` 像素宽,`2.0 × 842 = 1684` 像素高。
- `pixel_per_pt = 1.0` 时:输出约为 `595 × 842` 像素,面积变为原来的 1/4(因为宽高各减半)。
- `ppi / 72.0` 的含义:ppi 是「每英寸像素数」,72 是「每英寸点数」,相除得到「每点像素数」即 `pixel_per_pt`。默认 `--ppi 144` 对应 `pixel_per_pt = 2.0`。

> 说明:以上像素尺寸为按公式手算的结果,**待本地验证**:你可以在本地用 `typst compile --ppi 72 doc.typ doc.png`(`pixel_per_pt = 1.0`)与默认 `--ppi 144` 各导出一次,对比两张 PNG 的实际像素尺寸。

**关于 `.round().max(1.0)` 的保护意义**(对应练习):它保证即使 `size` 或 `pixel_per_pt` 极小,画布也至少是 1×1 像素。否则 `Pixmap::new(0, 0)` 会返回 `None`,而 `render()` 里 `.unwrap()` 会导致程序崩溃。

#### 4.3.5 小练习与答案

**练习 1**:用户运行 `typst compile --ppi 300 doc.typ doc.png`,最终的 `pixel_per_pt` 是多少?

> **参考答案**:`300 / 72 ≈ 4.167`。即每个 pt 渲染约 4.167 个像素,属于较高分辨率,适合印刷。

**练习 2**:为什么 `render()` 里创建画布用的是 `Pixmap::new(pxw, pxh).unwrap()`?如果去掉 `.max(1.0)` 可能出什么问题?

> **参考答案**:`Pixmap::new` 在宽或高为 0 时会返回 `None`。`.unwrap()` 遇到 `None` 直接 panic。`.max(1.0)` 把下限锁在 1 像素,确保 `Pixmap::new` 总能成功,从而让 `.unwrap()` 安全。

**练习 3**:`render_merged` 与 `render` 的关系是什么?(提示:看 `src/lib.rs` 第 50–86 行。)

> **参考答案**:`render_merged` 先对文档的每一页调用 `render()` 得到各自的 `Pixmap`,再把它们按纵向(加间距 `gap`、可选整体背景 `fill`)拼成一张长图。即「多页 → 一张图」,底层仍复用单页的 `render()`。

---

## 5. 综合实践

把本讲的知识串起来,完成下面这个「全链路追踪」小任务:

**任务**:假设用户执行下面这条命令,请把「从命令行参数到最终 PNG 像素」的关键链路完整复述一遍,并手算输出尺寸。

```bash
typst compile --ppi 72 doc.typ doc.png
```

要求:

1. **依赖侧**:打开 `crates/typst-render/Cargo.toml`,指出渲染这张 PNG 会用到的核心外部库(tiny-skia 必然用到;若文档含照片还会用到 image 等)。
2. **参数侧**:追踪 `--ppi 72` 如何流动:`args.rs` 的 `ppi` → `compile.rs` 的 `config.ppi` → `png_options` 里 `pixel_per_pt = 72 / 72.0 = 1.0` → 传入 `typst_render::render` 的 `RenderOptions`。
3. **尺寸侧**:若文档页面是 A4(595 × 842 pt),按 `pxw = round(1.0 × 595) = 595`、`pxh = round(1.0 × 842) = 842` 手算输出像素尺寸。
4. **落点侧**:在 `compile.rs` 中指出最终是 `export_image_page` 调用 `typst_render::render` 后,由 `pixmap.encode_png()` 写出 `.png` 文件。

**预期产出**:一段文字描述 + 一组手算数字(595 × 842)。如果有本地 Typst 环境,可用 `typst compile --ppi 72 doc.typ doc.png` 实际导出并查看图片属性来验证(若无可运行环境,标注「待本地验证」即可)。

## 6. 本讲小结

- **typst-render 是 Typst 的光栅图像导出器**:把 typst-layout 产出的 `Page`/`Frame`(矢量场景)渲染成 `tiny-skia::Pixmap`(像素缓冲),用于 PNG 输出。
- 它体量很小(`src/` 仅 5 个文件),但**组合了多个外部渲染库**:tiny-skia 当地基,pixglyph 管字形,resvg 管 SVG,hayro 管 PDF,image 管光栅图解码与重采样,comemo 负责缓存。
- 公共入口 `render(page, opts) -> Pixmap`:根据页面尺寸、出血区、`pixel_per_pt` 计算画布像素尺寸并递归渲染。
- `RenderOptions` 只有两个旋钮:`pixel_per_pt`(分辨率)和 `render_bleed`(出血区),默认 `pixel_per_pt = 2.0`。
- CLI 调用链:`compile` → … → `export_image_page` → `typst_render::render` → `encode_png`;`pixel_per_pt = ppi / 72.0`,默认 `--ppi 144` 恰好等于 `2.0`。
- 画布尺寸公式 `round(pixel_per_pt × size).max(1.0)` 中,`.max(1.0)` 是防止零尺寸画布导致 panic 的保护。

## 7. 下一步学习建议

本讲只建立了全局观,还没有进入任何渲染机制。建议接下来按以下顺序学习:

1. **下一讲《渲染入口 render 与 RenderOptions》(u1-l2)**:会更细致地拆解 `render()` 的画布尺寸计算与 `render_merged()` 的多页拼接,以及 `AbsExt`、`fill_or_white` 等辅助逻辑。
2. **再下一讲《Frame 场景树与 render_frame 派发》(u1-l3)**:理解 `Frame` 这个层级化场景图,以及 `render_frame` 如何把不同元素(Group/Text/Shape/Image/Link/Tag)分派到子模块——这是进入进阶层的钥匙。
3. 之后进入进阶层,依次学习 State 坐标变换、Group 裁剪遮罩、纯色 Paint、形状、图像、文本六条主链。

继续阅读建议:在进入下一讲前,可以先扫一眼 `src/lib.rs` 全文(只有不到 300 行),对 `State`、`render_frame`、`render_group`、`to_sk_transform` 这几个名字混个眼熟,后续讲义会逐个深入。
