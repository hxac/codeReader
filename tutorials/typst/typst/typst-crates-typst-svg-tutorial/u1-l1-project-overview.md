# 讲义标题：项目定位与整体架构

## 1. 本讲目标

本讲是 typst-svg 学习手册的第一篇，目标是从「鸟瞰」视角建立全局认识。读完本讲，你应该能够：

- 说清楚 **typst-svg 是什么**：它在 Typst 工具链里扮演「把排版结果导出成 SVG 矢量图」的角色。
- 说清它的 **输入和输出**：输入是排版引擎产出的 `Page` / `Frame`，输出是一段 SVG 字符串。
- 区分它和 Typst 的另外两个导出器 **typst-render（位图 PNG）** 与 **typst-pdf（PDF 文档）** 的职责差异。
- 看懂它的 **依赖关系**：它依赖哪些 typst 内部 crate，又被谁调用。
- 认识它的 **7 个源码文件** 与 **公共 API**，为后续讲义建立地图。

本讲只做「定位 + 地图」，不深入渲染细节。渲染主链路会在第 2 单元展开。

## 2. 前置知识

在进入源码之前，先用大白话解释几个关键概念。

### 2.1 什么是 SVG

SVG（Scalable Vector Graphics，可缩放矢量图形）是一种用 **XML 文本** 描述图形的格式。和 PNG、JPG 这类「位图」不同，SVG 不存储像素点，而是存储「画一条线、画一个矩形、填一种颜色」这样的绘图指令。因此：

- 它**无限放大不模糊**（矢量）。
- 它本质上是**文本**，可以直接阅读，也可以被浏览器、图片查看器解析。

typst-svg 的工作，就是把 Typst 排版出来的页面「翻译」成一长串这样的 SVG 绘图指令。

### 2.2 什么是 Frame 和 Page

Typst 把源代码变成最终文档，大致经过这样一条流水线：

```
Typst 源码  →  解析  →  求值  →  排版(typst-layout)  →  PagedDocument / Page / Frame  →  导出
```

- **Frame（帧）** 是排版的结果。它是一棵「树」，树上的每个节点都带有一个位置和大小，叶子节点是具体的绘制元素（文字、形状、图片、链接等）。可以把它理解成一张「已经摆好位置、准备打印」的画布。
- **Page（页）** 在 Frame 外面再包了一层，额外携带「出血（bleed）」「页面背景填充」等页面级信息。

typst-svg 不负责排版，它只负责**接收已经排版好的 `Page` / `Frame`，把它画成 SVG**。

### 2.3 什么是 crate（Rust 工作区里的模块）

Typst 是一个 Rust 项目，被拆成许多个 **crate**（可以粗略理解为「独立的子模块 / 子包」），各自放在 `crates/` 目录下。typst-svg 就是其中一个：`crates/typst-svg/`。每个 crate 通过 `Cargo.toml` 声明自己依赖哪些其他 crate。

## 3. 本讲源码地图

本讲只看两个文件，它们足以建立全局观：

| 文件 | 行数 | 作用 |
| --- | ---: | --- |
| `Cargo.toml` | 37 | 声明 crate 名称、描述，以及它依赖哪些 crate |
| `src/lib.rs` | 551 | crate 的入口：模块声明、公共 API、渲染器与渲染状态的定义 |

> 提示：本 crate 一共有 7 个源码文件，约 2198 行代码，属于「中偏小」规模。其余 5 个文件（`write.rs` `path.rs` `shape.rs` `text.rs` `paint.rs` `image.rs`）会在后续讲义逐个深入。本讲只从 `lib.rs` 顶部认识它们。

永久链接基准（本讲所有链接都基于此 commit）：

```
https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/
```

## 4. 核心概念与源码讲解

### 4.1 typst-svg 的定位：输入、输出与相邻 crate

#### 4.1.1 概念说明

一句话概括：**typst-svg 是 Typst 的 SVG 矢量导出器**。

它处在 Typst 流水线的「最后一公里」——排版（typst-layout）已经把内容摆好位置，typst-svg 负责把这份摆好的内容翻译成浏览器能显示、能保存为 `.svg` 文件的矢量图。

理解它的定位，关键看三件事：

1. **输入**：排版引擎产出的 `Page`（单页）或 `Frame`（一帧画布）。
2. **输出**：一段 `String`，内容是合法的 SVG 文本。
3. **谁调用它**：`typst-cli`（命令行导出 `.svg` 文件）、`typst-html`（在 HTML 里内嵌 SVG 或复用图像处理逻辑）、`typst-bundle`（多文档打包）。

#### 4.1.2 核心流程

从「Typst 源码」到「磁盘上的 .svg 文件」的完整链路：

```
Typst 源码(.typ)
   │  解析 + 求值
   ▼
typst-layout 排版
   │  产出
   ▼
PagedDocument ── 包含多页 ──> Page ── 包含 ──> Frame（带位置的绘制树）
   │
   │  typst-svg 接管
   ▼
SVG 字符串(String)
   │  typst-cli 写文件
   ▼
output.svg（矢量图）
```

注意 typst-svg 是一个**相对纯粹的「翻译」步骤**：给定 `Page`，产出字符串。它本身不做排版决策。

#### 4.1.3 源码精读

文件顶部的文档注释开门见山点明了定位：

[src/lib.rs:1-1](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L1-L1) —— 注释 `Rendering of Typst documents into SVG images.`，说明本 crate 的职责就是把文档渲染成 SVG 图像。

输入类型来自 typst-layout，在 `use` 中可以看到：

[src/lib.rs:19-19](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L19-L19) —— `use typst_layout::{Page, PagedDocument};`，这正是 typst-svg 的「输入来源」：它消费排版引擎产出的 `Page` 和 `PagedDocument`。

最核心的导出函数 `svg` 的签名也印证了「输入 Page、输出 String」：

[src/lib.rs:30-43](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L30-L43) —— `pub fn svg(page: &Page, opts: &SvgOptions) -> String`。函数体只有 7 步（`page_bleed` → `SVGRenderer::new` → `svg_header` → `render_page` → `finalize` → `end_document`），这些步骤会在第 2 单元逐个拆解。

而它的调用者之一 `typst-cli`，正是这样使用它的：

`typst-cli/src/compile.rs:585` 处有 `let svg = typst_svg::svg(page, &options);`，拿到字符串后直接 `output.write(svg.as_bytes())` 写成 `.svg` 文件。

> 对比相邻导出器（无需深入，建立印象即可）：
> - **typst-render**：把 `Page` 渲染成**位图像素**（PNG），输出的是栅格图像，放大会模糊。
> - **typst-pdf**：把文档导出成 **PDF** 文件。
> - **typst-svg**（本 crate）：把 `Page`/`Frame` 渲染成 **SVG 矢量文本**，放大不模糊。
>
> 三者都消费排版结果，只是「翻译目标格式」不同。

#### 4.1.4 代码实践

**实践目标**：亲手确认 typst-svg 的「输入 → 输出」契约。

**操作步骤**：

1. 打开 [src/lib.rs:32-32](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L32-L32)，读 `svg` 函数签名。
2. 确认第一个参数类型（`&Page`）来自哪里：它在 [src/lib.rs:19-19](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L19-L19) 被 `use` 进来，来自 `typst_layout`。
3. 确认返回类型是 `String`。

**需要观察的现象**：函数签名里**没有任何文件名、文件句柄或路径参数**——它只返回字符串。

**预期结果**：你会确认 typst-svg 自身「不知道文件系统」，它只负责产出 SVG 文本；「写到哪个文件」是 `typst-cli` 的职责。这就是职责边界的体现。

**待本地验证**：如果你本地能编译 Typst，可以在 `typst-cli/src/compile.rs` 附近加一行日志打印 `svg.len()`，观察导出字符串的大小随页面内容变化。

#### 4.1.5 小练习与答案

**练习 1**：typst-svg 的输入是「源代码」还是「排版结果」？

> **答案**：是排版结果（`Page`/`Frame`）。typst-svg 不参与解析或排版，它只接收已经摆好位置的内容。

**练习 2**：同样是「导出文档」，typst-svg 和 typst-render 的输出本质有什么不同？

> **答案**：typst-svg 输出的是矢量 SVG **文本**（绘图指令，放大不模糊）；typst-render 输出的是位图**像素**（如 PNG，放大会模糊）。

**练习 3**：为什么说 typst-svg 是「相对纯粹」的翻译步骤？

> **答案**：因为给定 `Page`，它只产出一段 `String`，本身不做排版决策，也不直接操作文件系统。

---

### 4.2 依赖地图：Cargo.toml

#### 4.2.1 概念说明

一个 crate 的 `Cargo.toml` 里藏着两件重要信息：

1. **它依赖谁**（`[dependencies]`）：它需要借助哪些其他 crate 才能工作。
2. **谁依赖它**：反过来，哪些 crate 会调用 typst-svg 的功能。

把这两个方向连起来，就能画出 typst-svg 在整个工具链里的「上下游关系图」。这是理解架构最快的方式。

#### 4.2.2 核心流程

typst-svg 的依赖可以分为两组：

- **typst 家族依赖**（来自 Typst 工作区内部）：提供排版结果、字体、资产、工具函数等。
- **第三方依赖**：提供 SVG/XML 写入、哈希、数字格式化、字体解析、图像/PDF 处理等基础能力。

```
            上游（被 typst-svg 依赖）
   typst-layout ──┐  提供排版结果 Page/Frame
   typst-library ─┤  提供颜色/几何/图片等类型
   typst-assets ──┤  提供字体等资产
   typst-utils ───┤  提供哈希等工具
   typst-macros ──┤  提供过程宏（如计时）
   typst-timing ──┘  提供性能计时
                 │
              typst-svg
                 │
            下游（调用 typst-svg）
   typst-cli ─────►  导出 .svg 文件        (用 svg)
   typst-html ────►  HTML 内嵌 SVG / 图像处理 (用 svg_in_html、WebImage、convert_image_scaling)
   typst-bundle ──►  多文档打包            (用 svg_in_bundle)
```

#### 4.2.3 源码精读

`[dependencies]` 段完整声明了所有依赖：

[Cargo.toml:15-34](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/Cargo.toml#L15-L34) —— 这里能看到 typst 家族依赖（`typst-assets` `typst-layout` `typst-library` `typst-macros` `typst-timing` `typst-utils`）以及一组第三方依赖。

其中几个第三方依赖的用途（建立印象即可，后续讲义会用到）：

| 依赖 | 在 typst-svg 里的用途（简述） |
| --- | --- |
| `xmlwriter` | 底层 XML/SVG 写入器，负责拼出标签和属性 |
| `image` / `base64` | 图像解码/编码、把图像转成 base64 data URL 内嵌 |
| `ttf-parser` | 解析字体文件，提取字形轮廓 |
| `hayro` / `hayro-svg` | 把嵌入的 PDF 图转换成 SVG（见第 6 单元） |
| `indexmap` / `rustc-hash` | 去重容器（`Deduplicator`）的底层实现 |
| `comemo` | 记忆化（形状几何转路径时复用结果） |
| `itoa` / `ryu` | 高速把整数 / 浮点数格式化成字符串 |

`Cargo.toml` 顶部还声明了 crate 的描述：

[Cargo.toml:2-3](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/Cargo.toml#L2-L3) —— `name = "typst-svg"` 与 `description = "SVG exporter for Typst."`，与 4.1 节的定位完全一致。

关于「谁依赖 typst-svg」，代码里可以直接验证（这些是真实调用点）：

- `typst-cli/src/compile.rs:585`：`typst_svg::svg(page, &options)` —— 命令行导出单页 SVG。
- `typst-bundle/src/export.rs:119`：`typst_svg::svg_in_bundle(...)` —— 打包时按页导出，并支持跨文档链接锚点。
- `typst-html/src/encode.rs:392`：`typst_svg::svg_in_html(...)` —— 在 HTML 里内嵌一段 SVG。
- `typst-html/src/rules.rs:778` 与 `:796`：`typst_svg::WebImage::new(...)` 和 `typst_svg::convert_image_scaling(...)` —— 复用 typst-svg 的图像处理逻辑。

#### 4.2.4 代码实践

**实践目标**：画出 typst-svg 的上下游依赖关系，并解释「为什么 typst-html 也要依赖 typst-svg」。

**操作步骤**：

1. 打开 [Cargo.toml:15-34](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/Cargo.toml#L15-L34)，挑出所有以 `typst-` 开头的依赖（共 6 个）。
2. 在纸上画出「上游：6 个 typst crate → typst-svg → 下游：typst-cli / typst-html / typst-bundle」的关系图。
3. 思考并回答：typst-html 明明是导出 HTML 的，为什么还要用 typst-svg？

**需要观察的现象**：typst-html 同时用到了 typst-svg 的**两类东西**——一个是 `svg_in_html`（把一帧画布渲染成 SVG 文本嵌进 HTML），另一个是 `WebImage` / `convert_image_scaling`（图像归一化与缩放映射）。

**预期结果**：你应该得出这样的结论——**typst-svg 不只是一个「画 .svg 文件」的 crate，它还把图像处理（`WebImage`）这一块能力做成了可被 `typst-html` 复用的公共组件**。所以 typst-html 依赖它，既为了内嵌 SVG，也为了复用图像归一化逻辑。这正是本讲「架构」部分的关键洞察：typst-svg 同时服务于「文件导出」和「HTML 内嵌」两条路径。

> 说明：以上调用点（compile.rs / export.rs / encode.rs / rules.rs 的行号）基于本讲 HEAD，若你阅读时仓库已更新，行号可能变化，但函数名不变。

**待本地验证**：若本地可编译，运行一次 `typst compile input.typ output.svg`，确认产物为文本 SVG；再用浏览器打开观察矢量效果。

#### 4.2.5 小练习与答案

**练习 1**：typst-svg 依赖的 typst 家族 crate 里，哪一个负责提供「排版结果 `Page`/`Frame`」？

> **答案**：`typst-layout`（见 `use typst_layout::{Page, PagedDocument};`）。

**练习 2**：`xmlwriter` 这个第三方依赖在 typst-svg 里大致起什么作用？

> **答案**：它是底层 XML 写入器，负责把「开始标签、属性、结束标签」拼成最终的 SVG 文本。

**练习 3**：为什么 typst-html 会依赖 typst-svg？给出两条理由。

> **答案**：(1) 用 `svg_in_html` 把帧渲染成 SVG 内嵌进 HTML；(2) 复用 `WebImage` / `convert_image_scaling` 做图像归一化和缩放映射。

---

### 4.3 模块声明与文件职责

#### 4.3.1 概念说明

`lib.rs` 是 crate 的入口文件。它做的第一件事就是用 `mod` 声明「本 crate 由哪几个内部模块组成」。typst-svg 把功能拆成了 7 个源码文件，`lib.rs` 既是它们的「目录」，又是把它们拼装到一起的「胶水」。

#### 4.3.2 核心流程

typst-svg 的源码组织遵循一个清晰思路：**一个抽象层 + 一组渲染对象**。

```
src/
├── lib.rs     入口：模块声明、公共 API、渲染器(SVGRenderer)、渲染状态(State)、去重器
├── write.rs   输出抽象层：SVG 元素包装、数字格式化、值序列化（地基）
├── path.rs    矢量原语：SVG path 数据构建器
├── shape.rs   形状：把 Shape 转成 <path>，处理填充/描边
├── text.rs    文本：逐字形渲染，字形定义与复用
├── paint.rs   绘制系统：颜色、渐变（线/径/圆锥）、平铺（最复杂）
└── image.rs   图像：<image> 内嵌、WebImage、PDF 转 SVG
```

其中 `lib.rs` 的一个关键设计是：**虽然渲染逻辑分散在 `shape.rs` `text.rs` `paint.rs` `image.rs` 等文件里，但它们都用 `impl SVGRenderer { ... }` 为同一个 `SVGRenderer` 类型添加方法**。也就是说，`SVGRenderer` 这个类型是被「拼装」出来的——`lib.rs` 定义它的字段，其他文件给它添方法。这是一种很常见的 Rust 模块组织方式。

#### 4.3.3 源码精读

文件顶部的 6 条 `mod` 声明，正好对应 6 个子文件（加上 `lib.rs` 自己共 7 个）：

[src/lib.rs:3-8](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L3-L8) —— 声明 `image` `paint` `path` `shape` `text` `write` 六个子模块。

注意这里没有 `mod lib;`，因为 `lib.rs` 本身就是 crate 根。除了私有模块，`lib.rs` 还把图像模块里的两个类型**重新导出**给外部使用：

[src/lib.rs:11-11](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L11-L11) —— `pub use image::{WebImage, convert_image_scaling};`。这正是 4.2 节提到的「typst-html 复用图像处理能力」的来源：这两个符号被公开导出，所以 `typst-html` 才能调用 `typst_svg::WebImage::new(...)`。

#### 4.3.4 代码实践

**实践目标**：把 6 条 `mod` 声明与磁盘上的文件一一对应。

**操作步骤**：

1. 打开 [src/lib.rs:3-8](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L3-L8)，数出 6 个模块名。
2. 列出 `src/` 目录下的文件（`lib.rs` + 6 个 = 7 个）。
3. 做一张「模块名 → 文件 → 一句话职责」的对照表（可参考 4.3.2 的说明）。

**需要观察的现象**：每条 `mod xxx;` 都能在 `src/` 下找到一个 `xxx.rs` 文件，且文件名与模块名完全一致。

**预期结果**：你得到一张 7 行的表，能对每个文件说出它负责什么。这就是后续讲义的「阅读地图」。

**待本地验证**：可在仓库根目录用 `ls crates/typst-svg/src/` 核对文件个数。

#### 4.3.5 小练习与答案

**练习 1**：typst-svg 一共有多少个 `.rs` 源码文件？

> **答案**：7 个（`lib.rs` + `write.rs` `path.rs` `shape.rs` `text.rs` `paint.rs` `image.rs`）。

**练习 2**：为什么渲染逻辑分散在多个文件里，却都属于同一个 `SVGRenderer` 类型？

> **答案**：因为各文件都用 `impl SVGRenderer { ... }` 给同一个类型添加方法，`lib.rs` 只定义字段，方法被「拼装」上去。这是 Rust 常见的模块组织方式。

**练习 3**：`WebImage` 定义在哪个模块？为什么 `typst-html` 能直接用 `typst_svg::WebImage`？

> **答案**：定义在 `image` 模块；因为 `lib.rs` 用 `pub use image::{WebImage, convert_image_scaling};` 把它重新导出为 crate 的公共 API。

---

### 4.4 公共 API：导出函数与配置

#### 4.4.1 概念说明

「公共 API」指的是 crate 对外暴露、供别人调用的接口。typst-svg 的公共 API 主要由两部分组成：

1. **四个导出函数**：`svg`、`svg_merged`、`svg_in_bundle`、`svg_in_html`，对应四种不同的使用场景。
2. **一个配置结构 `SvgOptions`**：控制导出行为（是否含出血、是否美化输出）。

外加一组图像相关类型（`WebImage`、`convert_image_scaling`）作为可复用组件。

#### 4.4.2 核心流程

四个导出函数对应四个场景：

| 函数 | 场景 | 输入要点 |
| --- | --- | --- |
| `svg` | 导出**单页**为一个 SVG 文件 | 一个 `Page` |
| `svg_merged` | 把**多页文档**合并成**一个** SVG（页与页之间加间距） | 整个 `PagedDocument` + 间距 |
| `svg_in_bundle` | 在**打包多文档**时导出单页，支持跨文档链接锚点 | `Page` + 锚点 + 链接解析器 |
| `svg_in_html` | 把**一帧**渲染成 SVG，用于**内嵌进 HTML** | 一个 `Frame` + 文本尺寸等 |

每个导出函数内部都遵循相似的步骤（以 `svg` 为例）：

```
page_bleed       计算出血偏移，得到最终尺寸 size 和变换 ts
   ▼
SVGRenderer::new 新建渲染器（持有 7 个去重容器）
   ▼
svg_header       写 <svg> 头（viewBox / width / height / 命名空间）
   ▼
State::new       建立初始渲染上下文（单位变换 + 尺寸）
   ▼
render_page      渲染页面（背景填充 + 帧内容）
   ▼
finalize         把累积的字形/裁剪/渐变/平铺等定义写入 <defs>
   ▼
end_document     由 XmlWriter 收尾，返回完整字符串
```

#### 4.4.3 源码精读

最常用的入口 `svg`，函数体就是上面流程的直接对应：

[src/lib.rs:30-43](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L30-L43) —— 注意它的 7 行函数体几乎就是「流程图」的逐行翻译。其中 `#[typst_macros::time(name = "svg")]` 是一个计时宏（来自 `typst-macros`/`typst-timing` 依赖），用于统计导出耗时。

配置结构 `SvgOptions` 只有两个字段：

[src/lib.rs:174-185](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L174-L185) —— `render_bleed`（是否把「出血」区域也画进 SVG，印刷场景需要）和 `pretty`（是否把 SVG 格式化成人类可读）。

`page_bleed` 根据 `render_bleed` 决定尺寸和偏移：

[src/lib.rs:155-160](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L155-L160) —— 当 `render_bleed` 为真时，把页面四边的出血加进总尺寸，并算出一个平移变换 `ts`（把内容从「含出血的左上角」移回正确位置）。

另外三个导出函数签名（先建立印象，细节留到第 6 单元）：

- `svg_in_bundle`：[src/lib.rs:45-73](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L45-L73) —— 比 `svg` 多了 `anchors`（可被链接的锚点）和 `link_resolver`（跨文档链接解析器）。
- `svg_in_html`：[src/lib.rs:75-120](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L75-L120) —— 输入是 `Frame` 而非 `Page`，并写入自定义 `id`/`style`，尺寸用 `em` 单位（适应 HTML 排版）。
- `svg_merged`：[src/lib.rs:122-153](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L122-L153) —— 遍历所有页，纵向堆叠并加入 `gap` 间距，合并成单个 SVG。

#### 4.4.4 代码实践

**实践目标**：跟踪一次 `svg()` 导出的执行步骤，理解它的「主流程骨架」。

**操作步骤**：

1. 打开 [src/lib.rs:30-43](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L30-L43)，把函数体里每一步（`page_bleed` / `new` / `svg_header` / `State::new` / `render_page` / `finalize` / `end_document`）和 4.4.2 的流程图对上号。
2. 再看 [src/lib.rs:174-185](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L174-L185) 的 `SvgOptions`，确认它只有两个字段。
3. 查看调用方如何构造它：`typst-cli/src/compile.rs:628-629` 定义了 `fn svg_options(config) -> SvgOptions { SvgOptions { render_bleed: false, pretty: config.pretty } }`。

**需要观察的现象**：CLI 默认 `render_bleed: false`（不画出血），`pretty` 跟随用户是否要求美化输出。

**预期结果**：你能用自己的话说出「一次单页 SVG 导出经历哪 7 步」，并知道 `render_bleed` 和 `pretty` 分别影响什么（出血区域是否画出 / SVG 是否缩进美化）。

**待本地验证**：若本地可运行，分别用美化与非美化模式导出同一个 `.typ`，对比 `.svg` 文本是否带缩进换行。

#### 4.4.5 小练习与答案

**练习 1**：`SvgOptions` 有哪两个字段？各自控制什么？

> **答案**：`render_bleed`（是否把出血区域画进 SVG）和 `pretty`（是否把 SVG 格式化成人类可读）。

**练习 2**：要把一个多页文档合并成**单个** SVG 文件，应该用哪个函数？

> **答案**：`svg_merged`，它接收整个 `PagedDocument` 和页间距 `gap`。

**练习 3**：`svg()` 函数体里，`finalize` 这一步大概在做什么？

> **答案**：把渲染过程中累积、去重后的「定义」（字形、裁剪路径、渐变、平铺等）统一写入 SVG 的 `<defs>` 区域。细节会在第 2、4、5 单元展开。

## 5. 综合实践

把本讲的三块知识（定位、依赖、API）串起来，完成下面这个综合任务。

**任务：绘制 typst-svg 的「全景架构图」并标注一次导出的数据流。**

要求在一张图（或一段文字）里包含：

1. **上下游**：上游 6 个 typst 依赖（标出 `typst-layout` 提供 `Page`/`Frame`）、typst-svg 本体、下游 3 个调用方（`typst-cli` / `typst-html` / `typst-bundle`），并标出各自调用了哪个公共函数。
2. **输入输出**：在 typst-svg 旁标注「输入：`Page`/`Frame` → 输出：`String`(SVG)」。
3. **内部结构**：在 typst-svg 内部画出 7 个源码文件，并标注 `lib.rs` 是入口、`write.rs` 是输出抽象地基。
4. **主流程**：在 `svg()` 函数旁列出它的 7 步骨架（`page_bleed → new → svg_header → State::new → render_page → finalize → end_document`）。

**参考要点（可用来自查）**：

- 上游应出现：`typst-layout`、`typst-library`、`typst-assets`、`typst-utils`、`typst-macros`、`typst-timing`。
- 下游调用关系：`typst-cli`→`svg`、`typst-bundle`→`svg_in_bundle`、`typst-html`→`svg_in_html` + `WebImage`/`convert_image_scaling`。
- 特别标注：typst-html 之所以依赖 typst-svg，**不只是为了内嵌 SVG，还为了复用图像处理组件**——这是本讲最重要的架构洞察。

**待本地验证**：若本地可编译运行，用 `typst compile hello.typ out.svg` 跑通一次，再用浏览器打开 `out.svg`，确认它确实是矢量文本（放大不模糊、可被文本编辑器打开）。

## 6. 本讲小结

- **typst-svg 是 Typst 的 SVG 矢量导出器**：接收排版引擎（typst-layout）产出的 `Page`/`Frame`，输出一段 SVG 字符串。
- 它和 **typst-render（位图）**、**typst-pdf（PDF）** 是并列的「导出器」，区别在于翻译的目标格式。
- 它**依赖 6 个 typst 家族 crate**（最关键是提供排版结果的 `typst-layout`），并**被 typst-cli / typst-html / typst-bundle 调用**。
- typst-html 依赖 typst-svg，既为了 `svg_in_html` 内嵌 SVG，也为了复用 `WebImage` / `convert_image_scaling` 这套图像处理组件。
- 本 crate 由 **7 个源码文件（约 2198 行）** 组成，`lib.rs` 是入口，`write.rs` 是输出抽象地基，其余按「原语→形状→文本→绘制→图像」分层。
- 公共 API 是 **4 个导出函数 + `SvgOptions`**；一次单页导出遵循 `page_bleed → header → render_page → finalize → end_document` 的 7 步骨架。

## 7. 下一步学习建议

本讲建立了「地图」，接下来建议：

1. **先读下一篇 u1-l2《源码结构与模块职责》**：更细致地看 7 个文件之间的依赖与拼装关系，理解 `lib.rs` 如何把分散的 `impl SVGRenderer` 汇聚起来。
2. **再读 u1-l3《公共 API 与运行方式》**：深入四个导出函数的差异和 `SvgOptions` 在 CLI 里的构造过程。
3. **进入第 2 单元**：开始拆解渲染主链路（`SVGRenderer`/`State` 调度、`Frame` 遍历、`write.rs` 输出抽象），这是理解整个 crate 的基石。
4. **建议先动手**：按本讲「综合实践」画一张架构图，带着这张图再读后续源码，会事半功倍。
