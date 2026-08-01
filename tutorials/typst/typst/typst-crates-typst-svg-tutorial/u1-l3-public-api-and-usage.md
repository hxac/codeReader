# 公共 API 与运行方式

## 1. 本讲目标

本讲承接 [u1-l1](./u1-l1-project-overview.md)（项目定位）与 [u1-l2](./u1-l2-source-structure.md)（源码结构），从「调用者视角」俯瞰 typst-svg 暴露给外部的接口。学完后你应当能够：

- 准确说出 typst-svg 的四个导出函数 `svg` / `svg_merged` / `svg_in_bundle` / `svg_in_html` 各自的**输入类型**与**使用场景**，并能判断在某种需求下该调哪一个。
- 理解唯一的配置结构 `SvgOptions` 只有两个布尔字段 `render_bleed` 与 `pretty`，并知道它们分别控制什么。
- 看懂一次单页导出的统一骨架：`page_bleed → new → svg_header → State::new → render_page → finalize → end_document`。
- 顺着 typst-cli 的调用链，说出 `SvgOptions` 是怎么从命令行 `--pretty` 一步步传到 `typst_svg::svg` 的。

本讲**不**深入渲染细节（`render_page` / `render_frame` 的分发逻辑属于 u2 单元，`Deduplicator` 属于 u6-l3）。我们只关注「入口与编排」。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **Page 与 Frame**：typst-layout 排版的产物。`Page` 是「一页」，内含一个 `frame`（排版好的内容树）和 `bleed`（出血边距，用于印刷）；`Frame` 是一棵由 `Group / Text / Shape / Image / Link / Tag` 组成的节点树。typst-svg 的输入就是这两者。
- **PagedDocument**：多页文档，即 `Page` 的集合，`svg_merged` 的输入。
- **SVG 基础**：`<svg>` 根元素的 `viewBox`、`width`、`height` 决定画布；`<defs>` 用来集中存放可被复用的定义（字形、裁剪路径、渐变等），再用 `href` 引用。
- **RAII 与 Drop**：Rust 的「获取即初始化、离开作用域自动析构」机制。typst-svg 用它来自动闭合 XML 标签（u2-l3 详讲），本讲只需知道它让「写出标签」的代码不会忘记闭合。
- **bleed（出血）**：印刷术语，指为避免裁切误差而在页面边缘多留的一段区域，内容会「溢出」到出血区。

> 本讲提到「函数签名」时，请把它当作一个**契约**：参数类型决定了能喂给它什么，返回 `String` 意味着 typst-svg 本身不碰文件系统，由调用者负责落盘。

## 3. 本讲源码地图

本讲几乎全部围绕 typst-svg 的聚合入口文件，并补充一处外部调用点：

| 文件 | 作用 | 本讲用到的部分 |
| --- | --- | --- |
| `crates/typst-svg/src/lib.rs` | 聚合入口；定义四个公开导出函数、`SvgOptions`、`page_bleed`、`svg_header` 与渲染骨架 | 四个 `pub fn`、`SvgOptions`、`page_bleed`、`svg_header(_with_custom_attrs)` |
| `crates/typst-svg/src/write.rs` | 输出抽象（u2-l3 详讲） | 仅借 `SvgElem`/`Drop` 说明「标签如何被自动闭合」，铺垫骨架 |
| `crates/typst-cli/src/compile.rs` | 命令行的编译/导出实现 | `svg_options`、`export_image_page`、bundle 装配，用于代码实践 |
| `crates/typst-cli/src/args.rs` | 命令行参数定义 | `--pretty` 标志如何映射到 `config.pretty` |

> typst-svg 自身不到 2200 行，公开 API 极其精简：**4 个函数 + 1 个配置结构**。本讲的全部内容就是把这 5 样东西讲清楚。

## 4. 核心概念与源码讲解

### 4.1 SvgOptions：导出行为的全部旋钮

#### 4.1.1 概念说明

调用者最关心的问题是：「我能配置什么？」 typst-svg 的回答非常克制——只有一个结构体 `SvgOptions`，且只有两个布尔字段：

- `render_bleed`：是否把页面**出血区**也纳入导出画布。默认 `false`（导出尺寸 = 页面尺寸）；为 `true` 时画布向外扩张，内容平移到出血区内部，用于印刷场景。
- `pretty`：是否输出**人类可读**（带缩进换行）的 SVG。默认 `false`（单行紧凑，体积小、利于程序处理）；为 `true` 时按每级 2 空格缩进。

它派生了 `Default`，所以 `SvgOptions::default()` 就是「两者皆 false」的最常见配置。这意味着 typst-svg 默认产出**最紧凑、不含出血**的 SVG。

#### 4.1.2 核心流程

两个字段各自驱动骨架中的一环：

- `pretty` → 进入 `xml_options(pretty)`，决定底层 `xmlwriter::XmlWriter` 的缩进模式；
- `render_bleed` → 进入 `page_bleed(page, opts)`，决定画布尺寸与平移变换。

用伪代码表示这两条影响链：

```
pretty ──▶ xml_options(pretty) ──▶ XmlWriter { indent: Spaces(2) 或 None }
render_bleed ──▶ page_bleed(page, opts) ──▶ (canvas_size, translate_transform)
```

其中 `page_bleed` 的尺寸计算可写成：

\[
\text{canvas\_size} = \text{frame.size} + \text{bleed.sum\_by\_axis}
\]

当 `render_bleed = false` 时 `bleed` 被置为零（`Sides::default()`），于是画布就等于页面尺寸、变换为单位矩阵，相当于「不做任何出血处理」。

#### 4.1.3 源码精读

配置结构定义：[src/lib.rs:174-185](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L174-L185) —— 注意它派生了 `Default`，且两个字段的注释清楚说明了用途（`render_bleed` 关系到印刷出血，`pretty` 关系到可读性）。

`pretty` 如何变成底层缩进：[src/lib.rs:162-172](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L162-L172) —— `xml_options` 把 `pretty` 翻译成 `xmlwriter::Indent::Spaces(2)` 或 `Indent::None`；同时把单引号关闭、属性缩进也固定为 `None`。也就是说 `pretty` **只影响元素层级的缩进**，属性永远紧跟在同一行。

`render_bleed` 如何影响画布：[src/lib.rs:155-160](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L155-L160) —— `page_bleed` 先按 `opts.render_bleed` 决定取不取 `page.bleed`，再用它扩张尺寸、并构造一个 `Transform::translate(bleed.left, bleed.top)`。这个平移把页面内容「推」到扩张后画布的内部，避免内容落在出血区之外。

> 小结：`SvgOptions` 故意做得很小。需要「专业印刷排版」的人打开 `render_bleed`；需要「人工阅读/调试 SVG 源码」的人打开 `pretty`。其余渲染细节（颜色、字形、去重）typst-svg 全部自行决定，不向调用者暴露。

#### 4.1.4 代码实践

**实践目标**：直观对比 `pretty` 对输出体积与可读性的影响。

**操作步骤**（命令行方式，需本地装有 typst CLI）：

1. 准备一个极简源文件 `hello.typ`，内容为 `Hello, SVG.`。
2. 不带 `--pretty` 编译：
   `typst compile hello.typ out_compact.svg`
3. 带 `--pretty` 编译：
   `typst compile --pretty hello.typ out_pretty.svg`

**需要观察的现象**：

- `out_compact.svg` 应当是几乎单行的紧凑文本；
- `out_pretty.svg` 应当有层级化的 2 空格缩进，`<svg>`、`<g>`、`<defs>` 等标签逐级换行。

**预期结果**：两个文件在浏览器/渲染器里**视觉上完全一致**，但 `out_pretty.svg` 字节数明显更大、可读性更好。这印证了 `pretty` 只影响「排版」不影响「语义」。若你没有可运行的 typst，则标注「待本地验证」，改为源码阅读：直接阅读 [src/lib.rs:162-172](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L162-L172) 的 `xml_options` 即可推得上述结论。

#### 4.1.5 小练习与答案

**练习 1**：假设你想把 typst 文档导出成「可供印刷厂使用、含出血」的 SVG，应如何设置 `SvgOptions`？又想同时方便人工检查，还要打开哪个字段？
**答案**：设 `render_bleed: true`（纳入出血）与 `pretty: true`（可读）。注意 typst-cli 当前并未把 `render_bleed` 暴露成命令行开关（见 4.3.4），所以程序化调用时才能启用它。

**练习 2**：`SvgOptions::default()` 产出的 SVG 是带缩进还是紧凑的？为什么？
**答案**：紧凑的。因为 `Default` 让两个字段都为 `false`，`xml_options(false)` 返回 `Indent::None`。

---

### 4.2 四个导出入口函数

#### 4.2.1 概念说明

typst-svg 把「导出」做成四个并列的公开函数，而非一个带枚举参数的大函数。它们的区别不在「渲染算法」，而在**输入类型**与**目标宿主环境**：

| 函数 | 输入 | 面向场景 | 是否含出血 | 是否含跨文档链接解析 | 尺寸单位 |
| --- | --- | --- | --- | --- | --- |
| `svg` | 单个 `Page` | 导出**单页** SVG 文件（CLI 默认） | 由 `opts` 控制 | 否 | pt |
| `svg_merged` | `PagedDocument` + `gap` | 把**多页**纵向拼成一张 SVG | 由 `opts` 控制 | 否 | pt |
| `svg_in_bundle` | 单个 `Page` + 锚点 + 链接解析器 | 作为**多文档打包（bundle）**的一份子，支持被其它文档链接 | 由 `opts` 控制 | 是 | pt |
| `svg_in_html` | 单个 `Frame` + `text_size` 等 | 内嵌进 **HTML** 页面 | 否（不接受 `SvgOptions`） | 是 | em |

关键洞察：

1. 前三者接收 `Page`/`PagedDocument`，因为它们产出的是「独立成页」的 SVG；`svg_in_html` 接收的是裸 `Frame`，因为 HTML 里 SVG 只是文档流的一个片段，不需要「页」的概念。
2. 只有 `svg_in_bundle` 与 `svg_in_html` 带 `link_resolver: Tracked<LateLinkResolver>`，用于把**逻辑链接目标**延迟解析成真实 URI——这是跨文档/HTML 链接所必需的。
3. `svg_in_html` 是唯一**不接收 `SvgOptions`** 的，它直接收一个 `pretty: bool`，并用 `em`（相对字号）而非 `pt`（绝对点）标注尺寸，以便 SVG 随 HTML 字号缩放。

#### 4.2.2 核心流程

四个函数共享同一个内部三段式：「准备渲染器 → 写头部 → 渲染主体 + 收尾」。差别只在每一段的具体做法：

```
┌─────────────┐
│ 1. 准备渲染器│  svg/svg_merged: SVGRenderer::new()
│             │  svg_in_bundle/svg_in_html: SVGRenderer::with_options(Some(link_resolver))
└──────┬──────┘
       ▼
┌─────────────┐
│ 2. 写 SVG 头│  svg/svg_merged/svg_in_bundle: svg_header(size)  → viewBox/width/height 用 pt
│             │  svg_in_html: svg_header_with_custom_attrs(...)  → 额外写 id、style，尺寸用 em
└──────┬──────┘
       ▼
┌─────────────┐
│ 3. 渲染+收尾 │  svg/svg_in_bundle: render_page(...)   （svg_in_bundle 额外 render_anchor）
│             │  svg_in_html: render_frame(...)         （再 render_anchor）
│             │  svg_merged: 循环 render_page，每页加 y 偏移
│             │  最后统一 finalize(svg) + xml.end_document()
└─────────────┘
```

`svg_merged` 的多页拼接逻辑稍特别：它先遍历所有页算出**总画布**（宽度取各页最大值，高度取各页高度之和加上页间 `gap`），再逐页用 `Transform::translate(0, y)` 渲染并累加 `y`。

#### 4.2.3 源码精读

最简洁的 `svg`：[src/lib.rs:30-43](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L30-L43) —— 这是阅读全部四个函数的「基准样板」。注意 `#[typst_macros::time(name = "svg")]` 给该导出加了一个计时探针，会出现在 typst 的性能报告中。

`svg_in_bundle`：[src/lib.rs:45-73](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L45-L73) —— 与 `svg` 的两处差别：用 `SVGRenderer::with_options(Some(link_resolver))` 创建渲染器（带链接解析能力），并在 `render_page` 之后用 `for (pos, id) in anchors { renderer.render_anchor(...) }` 写入若干可被链接命中的锚点。

`svg_in_html`：[src/lib.rs:75-120](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L75-L120) —— 重点看它如何用 `svg_header_with_custom_attrs` 的闭包追加 `id` 与 `style` 属性：宽度写成 `frame.width() / text_size` 后跟 `em`（第 100-103 行），并固定 `overflow: visible`。这是为了在 HTML 流里让 SVG 随字号缩放、且不被裁切。它渲染的是 `render_frame`（无页面背景、无出血）。

`svg_merged`：[src/lib.rs:122-153](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L122-L153) —— 注意第 126-132 行先算总尺寸（`num_gaps * gap` 初始化高度，再逐页 `set_max` 宽度、累加高度），第 138-149 行的循环用 `Transform::translate(Abs::zero(), y).pre_concat(bleed_ts)` 把每页放到正确纵坐标，并维护 `y += page_size.y + gap`。

#### 4.2.4 代码实践

**实践目标**：通过源码阅读，量化「pt 头」与「em 头」的差异，理解为何 HTML 内嵌要用 `em`。

**操作步骤**（纯源码阅读型）：

1. 打开 [src/lib.rs:456-466](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L456-L466)，看清 `svg_header_with_custom_attrs` 默认怎么写 `width`/`height`（数字 + `"pt"`）。
2. 对比 [src/lib.rs:97-108](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L97-L108) 里 `svg_in_html` 的 `style` 闭包：它把 `frame.width() / text_size` 写成 `em`。

**需要观察的现象**：独立 SVG 文件用绝对单位 `pt`，画布尺寸固定；HTML 内嵌用相对单位 `em`，尺寸随宿主页面字号变化。

**预期结果**：你能用自己的话解释——「独立 SVG 必须有确定尺寸才能被图片查看器正确显示；HTML 里的 SVG 是文档流的一部分，用 `em` 才能跟随正文字号一起放大缩小，保持与文字的视觉比例」。这是 `svg_in_html` 单独存在、而不复用 `svg` 的根本原因。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `svg_in_bundle` 和 `svg_in_html` 都需要 `link_resolver`，而 `svg` 不需要？
**答案**：`svg` 产出的是孤立的单页文件，其中的 `Destination::Location`（指向文档内某逻辑位置的链接）无法跨文件解析；而 bundle/HTML 场景下，多个文档（或文档与 HTML 容器）共存，需要 `LateLinkResolver` 在导出时把逻辑位置翻译成真实的目标 URI。

**练习 2**：如果要导出一份 5 页报告为**单个** SVG 文件，应调用哪个函数？若要导成 5 个独立文件呢？
**答案**：单个文件用 `svg_merged(document, &opts, gap)`（纵向拼接）；5 个独立文件则对每一页各调用一次 `svg(page, &opts)`（这也是 typst-cli 的做法）。

---

### 4.3 单页导出的统一骨架

#### 4.3.1 概念说明

四个函数看起来参数各异，但它们的身体都遵循同一条流水线。本节以最基础的 `svg` 为例，拆解这条「七步骨架」。掌握它之后，再看另外三个函数只是在某几步上做替换：

1. **page_bleed**：根据 `render_bleed` 算出画布尺寸与平移变换；
2. **new**：创建一个空的 `SVGRenderer`（内部持有 7 个去重容器，详见 u6-l3）；
3. **svg_header**：写出 `<svg>` 根元素及其 `viewBox`/`width`/`height`/`xmlns` 等标准属性；
4. **State::new**：构造初始渲染上下文（变换 = 单位矩阵，尺寸 = 画布尺寸）；
5. **render_page**：渲染页面背景与整棵 frame 树（细节在 u2 单元）；
6. **finalize**：把渲染过程中收集到的字形、裁剪路径、渐变等定义统一写进 `<defs>`；
7. **end_document**：由底层 `XmlWriter` 收口，返回最终 `String`。

`page_bleed` 与 `svg_header` 是骨架里最值得精读的两块，因为它们决定了「画布长什么样」。

#### 4.3.2 核心流程

七步骨架的伪代码（对应 `svg` 函数体）：

```
fn svg(page, opts) -> String:
    (size, ts) = page_bleed(page, opts)          # 步骤 1
    renderer   = SVGRenderer::new()               # 步骤 2
    xml        = XmlWriter::new(xml_options(opts.pretty))
    svg_elem   = svg_header(&mut xml, size)       # 步骤 3
    state      = State::new(size)                 # 步骤 4
    renderer.render_page(&mut svg_elem, &state, ts, page)  # 步骤 5
    renderer.finalize(svg_elem)                   # 步骤 6
    return xml.end_document()                     # 步骤 7
```

几个要点：

- 步骤 1 的 `ts`（bleed 平移）会被传进步骤 5 的 `render_page`，作为页面内容的外层变换。
- 步骤 3 的 `svg_header` 返回一个 `SvgElem`——它是基于 `XmlWriter` 的 RAII 包装：当 `SvgElem` 在步骤 6/7 离开作用域被 `Drop` 时，会自动调用 `end_element()` 闭合 `<svg>` 标签（详见 [src/write.rs:50-54](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L50-L54)）。因此整段代码里看不到手动的「闭合 `<svg>`」语句。
- 步骤 6 `finalize` 以固定顺序写出 8 类定义（字形、裁剪路径、线性/径向渐变、渐变引用、子渐变、平铺、平铺引用）。顺序在 u6-l4 详讲，这里只需知道「所有可复用资源都在这一步集中输出到 `<defs>`」。

#### 4.3.3 源码精读

骨架本体：[src/lib.rs:30-43](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L30-L43) —— 七步与上面伪代码一一对应。

**page_bleed**：[src/lib.rs:155-160](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L155-L160) —— 三行核心：`bleed` 取不取取决于 `opts.render_bleed`；`size` 在页面尺寸上叠加出血；`ts` 是把内容平移 `(bleed.left, bleed.top)` 的变换。

**svg_header**：[src/lib.rs:436-440](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L436-L440) 只是个转发壳，真正干活的是 **svg_header_with_custom_attrs**：[src/lib.rs:443-472](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L443-L472)。这段值得细看：

- 第 450 行先把尺寸 `clamp` 到至少 1pt——注释解释 resvg 等 SVG 解析器处理不了 0 尺寸的 SVG，这是一个防御性兜底；
- 第 454 行先调用 `write_custom_attrs`（让 `svg_in_html` 有机会插入 `id`/`style`），**再**写 `viewBox` 等标准属性，保证自定义属性写在前面；
- 第 456-466 行依次写 `viewBox="0 0 w h"`、`width`/`height`（带 `"pt"` 后缀）；
- 第 467-469 行写入三个命名空间（默认、`xlink`、`h5` 即 XHTML，后者用于与 HTML 元素互操作）。

**finalize 的固定顺序**：[src/lib.rs:410-419](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L410-L419) —— 八个 `write_*` 调用按固定次序排出所有 `<defs>` 内容；`finalize` 接管 `svg` 元素的所有权（`mut self, mut svg`），意味着它在收尾后渲染器即被消耗。

#### 4.3.4 代码实践

**实践目标**：追踪 typst-cli 如何从命令行构造 `SvgOptions` 并调用 `svg`，并据此解释 `pretty` 与 `render_bleed` 的实际影响。这是本讲规格指定的核心实践。

**操作步骤**（源码阅读 + 调用链跟踪）：

1. 命令行入口：阅读 [crates/typst-cli/src/args.rs:322-328](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/args.rs#L322-L328)，确认 `--pretty` 标志映射到 `CompileCommand`/`CompileArgs` 的 `pretty: bool` 字段。
2. 配置构造：阅读 [crates/typst-cli/src/compile.rs:627-630](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/compile.rs#L627-L630) 的 `svg_options`。你会看到 `SvgOptions { render_bleed: false, pretty: config.pretty }`——`pretty` 来自命令行，而 `render_bleed` 被**硬编码为 `false`**。
3. 实际调用：阅读 [crates/typst-cli/src/compile.rs:583-589](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/compile.rs#L583-L589) 的 `ImageExportFormat::Svg` 分支，确认它正是调用 `typst_svg::svg(page, &options)`，然后把返回的 `String` 字节写入输出。
4. （扩展）bundle 装配：阅读 [crates/typst-cli/src/compile.rs:391-397](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/compile.rs#L391-L397)，看 `svg_options(config)` 同样被塞进 `BundleOptions`，说明 bundle 导出复用同一份配置。

**需要观察的现象与预期结果**：

- **`pretty` 的影响**：当用户加 `--pretty`，`config.pretty = true` → `svg_options` 产出 `pretty: true` → `xml_options(true)` → `XmlWriter` 用 2 空格缩进 → 最终 SVG 带换行缩进、体积更大但可读。不加则为紧凑单行。
- **`render_bleed` 的影响**：由于 CLI 把它硬编码为 `false`，**命令行导出的 SVG 永远不含出血区**，画布严格等于页面尺寸。要让 `render_bleed` 生效，必须像 typst-cli 这样**程序化调用** typst-svg 并显式设为 `true`——这条调用链恰好说明了「同一份 typst-svg 库，不同宿主可以有不同配置策略」。

**结论**（用一句话回答规格的问题）：`SvgOptions` 由 `CompileConfig` 经 `svg_options()` 构造，`pretty` 端到端透传自 `--pretty` 命令行标志，影响 XML 缩进与可读性；`render_bleed` 在 CLI 中固定为 `false`，因此命令行产物不含出血，只有程序化调用才能启用印刷出血。

#### 4.3.5 小练习与答案

**练习 1**：在七步骨架里，`page_bleed` 返回的 `ts` 最终被用在哪里？如果 `render_bleed = false`，`ts` 是什么？
**答案**：`ts` 作为外层变换传给 `render_page`（步骤 5）。当 `render_bleed = false` 时，`bleed` 为零，`ts = Transform::translate(0, 0)` 即单位矩阵，相当于不平移。

**练习 2**：为什么 `svg()` 函数体里看不到「闭合 `<svg>` 标签」的代码？最终标签是在哪一步闭合的？
**答案**：因为 `SvgElem` 实现了 `Drop`，在 `finalize(svg)` 把 `svg` 元素消耗、以及函数返回导致 `xml` 离开作用域时，`Drop` 会自动调用 `xml.end_element()` 闭合 `<svg>`；`<svg>` 的真正文本闭合发生在最后的 `xml.end_document()`（步骤 7）期间。

**练习 3**：`svg_header_with_custom_attrs` 为什么要先把尺寸 clamp 到至少 1pt？
**答案**：因为 resvg 等下游 SVG 解析器无法正确处理 0 尺寸的 SVG；clamp 是防御性兜底，避免空页面产出无法被渲染的文件（见第 449-450 行注释）。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「从命令行到 SVG 头部」的完整追踪与验证。

**任务**：用 typst-cli 导出一个两页文档，检验 `pretty` 与（不可变动的）`render_bleed` 的效果，并手画一次配置流转图。

**操作步骤**：

1. 新建 `two.typ`，写入两行内容（如两段标题），确保它排版出 2 页。
2. 运行 `typst compile two.typ page.svg`，再用 `typst compile --pretty two.typ page_pretty.svg`，对比两者头部的前若干行（可用文本编辑器或 `head` 查看，本地环境允许时）。
3. 在两个文件里找到 `<svg` 根标签，确认其 `viewBox`、`width`、`height` 与 `xmlns*` 属性，并与 [src/lib.rs:456-469](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L456-L469) 的写入逻辑逐一对照。
4. 在 `page_pretty.svg` 里找到 `<defs>` 块，确认它是 `finalize`（[src/lib.rs:410-419](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L410-L419)）产出的可复用资源集合（即使内容简单，也应能看到结构）。
5. 画一张配置流转图：`--pretty (args.rs)` → `config.pretty (CompileConfig)` → `svg_options (compile.rs)` → `SvgOptions.pretty` → `xml_options` → `XmlWriter.indent`，并在图上标注 `render_bleed` 在 CLI 路径上被钉死为 `false`。

**需要观察的现象与预期结果**：

- 两个文件视觉一致，但 `page_pretty.svg` 有缩进、`page.svg` 紧凑；
- 两者的 `width`/`height` 都用 `pt` 且数值等于页面尺寸（印证 `render_bleed=false`，画布未扩张）；
- 配置流转图能清楚说明「命令行只有一个 `--pretty` 开关真正影响 SVG；`render_bleed` 在 CLI 下不可达」。

> 若本地无法运行 typst CLI，可把步骤 2-4 改为纯源码阅读：依据 [src/lib.rs:30-43](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L30-L43) 与 [src/lib.rs:443-472](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L443-L472) 推演输出形态，并明确标注「待本地验证」。

## 6. 本讲小结

- typst-svg 的公开 API 极简：**4 个导出函数 + 1 个 `SvgOptions`**，全部在 `src/lib.rs`。
- 四个函数按「输入类型 + 目标宿主」分工：`svg`（单页文件）、`svg_merged`（多页纵向拼接）、`svg_in_bundle`（bundle 一份子 + 锚点 + 跨文档链接）、`svg_in_html`（HTML 内嵌、`em` 单位、裸 Frame）。
- `SvgOptions` 只有两个布尔旋钮：`pretty`（缩进可读性）与 `render_bleed`（印刷出血）；默认皆为 `false`。
- 单页导出遵循统一七步骨架：`page_bleed → new → svg_header → State::new → render_page → finalize → end_document`，另外三个函数只是替换其中若干步。
- `page_bleed` 决定画布尺寸与平移；`svg_header` 写出 `viewBox`/`width`/`height`/命名空间，并把尺寸 clamp 到 ≥1pt 以兼容下游解析器。
- 在 typst-cli 中，`SvgOptions` 由 `svg_options(config)` 构造，`pretty` 来自 `--pretty` 标志端到端透传，而 `render_bleed` 被**硬编码为 `false`**——命令行产物永远不含出血。

## 7. 下一步学习建议

本讲只看了「入口与编排」，完全没展开 `render_page` / `render_frame` 内部。建议接下来进入第 2 单元：

- **u2-l1 SVGRenderer 渲染器与渲染状态**：拆开 `SVGRenderer` 持有的 7 个 `Deduplicator`、`State` 的变换语义，以及 `render_page` 如何处理页面背景。
- **u2-l2 Frame 遍历与 Group/Link/Anchor**：看 `render_frame` 如何按 `FrameItem` 分发，软/硬 frame 与裁剪如何处理。
- **u2-l3 SVG 输出抽象层 write.rs**：精读本讲反复提到的 `SvgElem` / `LazySvgElem` / `Drop` 机制，理解标签为何能被自动闭合。

读完第 2 单元，你就能把本讲的「七步骨架」里第 5、6 步的细节完全补齐。
