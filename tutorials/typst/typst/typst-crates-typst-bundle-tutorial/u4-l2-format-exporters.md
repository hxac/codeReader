# 四种格式的导出实现：pdf/png/svg/html 与 `*_in_bundle` 钩子

> 本讲是 u4-l1「export 主流程与 VirtualFs」的直接续篇。u4-l1 讲了 `export()` 如何并行遍历 `Bundle.files`、如何为每个文档构造 `LateLinkResolver`、以及最终产出 `VirtualFs`；但刻意把「每个 `export_document` 内部到底怎么编码成字节」留到了本讲。本讲就拆开这个黑盒。

## 1. 本讲目标

学完本讲，你应当能够：

1. 看懂 `export_document` 如何按 `BundleDocument`（`Paged` / `Html`）与 `PagedFormat`（`Pdf` / `Png` / `Svg`）两层 `match` 分派到四个具体的导出函数。
2. 说出 PDF / SVG / PNG / HTML 四种导出各自调用哪个钩子（`pdf_in_bundle` / `svg_in_bundle` / `html_in_bundle` / `render`）、各自的输入是「整文档」还是「单页」还是「根元素」、是否需要 `anchors`、是否需要 `link_resolver`。
3. 解释每个导出函数为何都加 `#[comemo::memoize]`，以及 `LateLinkResolver` 为什么以 `Tracked` 形式跨过记忆化边界。
4. 解释 `export_html` 为什么接收 `doc.root()`（根元素）而非整个 `HtmlDocument`，以及这背后「HTML 文档在构建后被原地改写」的原因。
5. 用 `LateLinkResolver::resolve` 的 `match` 解释跨文档链接是如何被算成相对路径的。

## 2. 前置知识

本讲默认你已经学过以下内容（若没有，建议先补）：

- **u3-l1**：`BundleDocument` 分 `Paged(Box<PagedDocument>, PagedExtras)` 与 `Html(Box<HtmlDocument>)` 两个变体；`PagedExtras` 只挂在 `Paged` 上，携带 `format: PagedFormat` 与 `anchors: Vec<(Location, EcoString)>`。
- **u4-l1**：`export()` 用 `par_iter()` 并行遍历 `files`，对每个 `BundleFile::Document` 构造 `LateLinkResolver::new(Some(path), bundle.introspector)` 后调用 `export_document`；最终汇成 `VirtualFs = IndexMap<VirtualPath, Bytes>`。

补充几个本讲会用到的底层概念：

- **comemo 记忆化（memoize）**：给函数加 `#[comemo::memoize]` 后，comemo 会以「参数的哈希值」为键缓存返回值。只要参数相同就直接返回缓存，不再执行函数体。它的前提是：参数必须可哈希、可比较。typst 的内省循环可能反复调用同一个编译/导出函数，记忆化能避免重复做昂贵的排版与编码工作。
- **`Tracked<T>`**：comemo 提供的一种「可跨记忆化边界传递的引用」。它本质是一个「能被哈希、但哈希的是身份而非内容」的句柄。typst 把 `LateLinkResolver`、`Introspector` 等无法整体哈希的大对象包成 `Tracked`，这样它们既能作为记忆化函数的参数，又不会触发深拷贝或逐字段哈希。
- **命名目的地（named destination）**：PDF 里的一种「可被链接命中的具名位置」。和 HTML 里的 `id="xxx"` / `#xxx` 锚点扮演类似角色——让别的链接能精确跳到这里。
- **`PagedDocument` 与 `Page`**：`PagedDocument` 是一份分页文档（含多页），`Page` 是其中单页。PNG/SVG 这种图像格式一次只能表示一页，所以导出时取 `pages()[0]`；PDF 可以表示多页，所以传整个 `PagedDocument`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `crates/typst-bundle/src/export.rs` | 本讲主战场：`export_document` 的格式分派，以及 `export_pdf` / `export_png` / `export_svg` / `export_html` 四个导出函数。 |
| `crates/typst-bundle/src/lib.rs` | 提供 `BundleDocument`、`PagedExtras` 数据结构，以及上游 `compile_document` 如何填出 `PagedExtras.format` / `PagedExtras.anchors`。 |
| `crates/typst-bundle/src/link.rs` | `create_link_anchors` 与 `create_paged_link_anchors`：在导出**之前**把 `PagedExtras.anchors` 填好（PDF/SVG 用），或在 HTML 文档上原地注入 DOM `id`。 |
| `crates/typst-pdf/src/lib.rs` | 钩子 `pdf_in_bundle`：把 `PagedDocument` 编码成 PDF 字节，把 `anchors` 序列化为命名目的地。 |
| `crates/typst-svg/src/lib.rs` | 钩子 `svg_in_bundle`：把单页 `Page` 编码成 SVG 字符串，把锚点序列化为可链接的点。 |
| `crates/typst-html/src/encode.rs` | 钩子 `html_in_bundle`：把根元素 `HtmlElement` 序列化成 HTML 字符串。 |
| `crates/typst-render/src/lib.rs` | 钩子 `render`：把单页 `Page` 光栅化成 `tiny-skia` 像素缓冲（PNG 的基础）。 |
| `crates/typst-library/src/model/link.rs` | `LateLinkResolver` 与 `ResolvedLink`：把「指向某个 `Location` 的链接」解析成相对 URI（单文件锚点 / 跨文档相对路径）。 |

## 4. 核心概念与源码讲解

### 4.1 export_document：格式分派的总枢纽

#### 4.1.1 概念说明

`export_document` 是 `export()`（u4-l1）与四个具体编码函数之间的**唯一分派点**。它接收三类输入：

- `doc: &BundleDocument`：要导出的文档，已经排版好（来自 `compile_document`）。
- `options: &BundleOptions`：四种格式的选项聚合（u4-l1 已讲）。
- `link_resolver: Tracked<LateLinkResolver>`：用于解析跨文档链接的解析器，`export()` 为每个文档构造好的那个。

它的全部职责就是「看清楚这个文档是什么格式，然后把它交给对应的编码函数」。因为 `BundleDocument` 已经把「paged 还是 html」分开了，`PagedExtras.format` 又把「pdf 还是 png 还是 svg」分开了，所以分派逻辑非常直白——两层 `match`。

#### 4.1.2 核心流程

```
export_document(doc, options, link_resolver)
│
├─ match doc
│   ├─ BundleDocument::Paged(doc, extras)
│   │     └─ match extras.format
│   │           ├─ PagedFormat::Pdf → export_pdf(doc, options.pdf, extras.anchors, link_resolver)
│   │           ├─ PagedFormat::Png → export_png(doc, options.png)         # 注意：无 anchors、无 link_resolver
│   │           └─ PagedFormat::Svg → export_svg(doc, options.svg, extras.anchors, link_resolver)
│   │
│   └─ BundleDocument::Html(doc)
│         └─ export_html(doc.root(), options.html, link_resolver)          # 注意：传 root 而非 doc
│
└─ 每个分支都返回 SourceResult<Bytes>，最终汇入 VirtualFs
```

需要特别留意三个「不对称」之处，它们是后续几节的核心：

1. **PNG 既不要 `anchors` 也不要 `link_resolver`**——因为 PNG 是静态图像，根本不支持链接（见 `link.rs` 文档注释「PNG documents do not support linking」）。
2. **PDF 与 SVG 需要 `anchors`**——因为它们要把跨文档链接的落地点序列化进去（PDF 用命名目的地、SVG 用可链接的点）；而 HTML 的锚点是 DOM `id`，不在 `extras.anchors` 里，而是在导出前被原地注入文档本身（见 4.2）。
3. **HTML 传的是 `doc.root()`（根元素）而非整个 `HtmlDocument`**——这是为了让函数能被记忆化，原因见 4.3。

#### 4.1.3 源码精读

先看 `export_document` 的完整签名与两层 `match`：

> [export.rs:55-75](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L55-L75) ——`export_document` 的入口与格式分派。第一层 `match doc` 区分 `Paged` / `Html`；`Paged` 分支内再 `match extras.format` 区分 `Pdf` / `Png` / `Svg`。

关键片段（去掉注释后）：

```rust
fn export_document(
    doc: &BundleDocument,
    options: &BundleOptions,
    link_resolver: Tracked<LateLinkResolver>,
) -> SourceResult<Bytes> {
    match doc {
        BundleDocument::Paged(doc, extras) => match extras.format {
            PagedFormat::Pdf => export_pdf(doc, &options.pdf, &extras.anchors, link_resolver),
            PagedFormat::Png => export_png(doc, &options.png),
            PagedFormat::Svg => export_svg(doc, &options.svg, &extras.anchors, link_resolver),
        },
        BundleDocument::Html(doc) => export_html(doc.root(), &options.html, link_resolver),
    }
}
```

`extras.format` 的来源是 `compile_document`：

> [lib.rs:316-319](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L316-L319) ——`compile_document` 在排版完 paged 文档后，把 `format` 装进 `PagedExtras`（此时 `anchors` 还是空的 `Vec::new()`，留到导出前由 `create_link_anchors` 填充）。

`PagedExtras` 的结构本身：

> [lib.rs:103-114](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L103-L114) ——`format` 记录「这份格式无关的 `PagedDocument` 最终要编码成 PDF/PNG/SVG 中的哪一种」；`anchors` 是跨文档命名锚点的「侧车」（u3-l1 已解释为何 paged 需要侧车而 html 不需要）。

#### 4.1.4 代码实践

**实践目标**：通过追踪函数签名，确认四种格式分别落到哪个 `match` 臂、传了哪些参数。

**操作步骤**：

1. 打开 [export.rs:61-74](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L61-L74)。
2. 对每个文档（扩展名 `.pdf` / `.png` / `.svg` / `.html`），先按 u2-l2 的格式推断规则确定它会变成哪种 `BundleDocument` / `PagedFormat`，再回到这里查它命中的分支。

**需要观察的现象**：四个文档命中的分支各不相同，且 PNG 分支的实参列表明显比 PDF/SVG 短。

**预期结果（自检）**：

- `.pdf` → `BundleDocument::Paged` → `PagedFormat::Pdf` → `export_pdf(doc, &options.pdf, &extras.anchors, link_resolver)`（4 个实参）。
- `.png` → `BundleDocument::Paged` → `PagedFormat::Png` → `export_png(doc, &options.png)`（2 个实参，无 anchors、无 link_resolver）。
- `.svg` → `BundleDocument::Paged` → `PagedFormat::Svg` → `export_svg(doc, &options.svg, &extras.anchors, link_resolver)`（4 个实参）。
- `.html` → `BundleDocument::Html` → `export_html(doc.root(), &options.html, link_resolver)`（3 个实参，传 `root`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 PNG 分支不需要 `link_resolver`？请引用源码或文档注释说明。

**参考答案**：因为 PNG 是光栅图像，不支持任何形式的链接。`link.rs` 的文档注释（[link.rs:131-135](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L131-L135)）明确写道「PNG documents do not support linking」。既然链接无处可放，`export_png` 自然不需要 `link_resolver`，也不需要 `anchors`。

**练习 2**：如果未来要给 bundle 增加一种新的 paged 格式（比如 TIFF），需要在 `export_document` 改几处？

**参考答案**：至少两处——`PagedFormat` 枚举增加一个变体（在 typst-library 里），以及 `export_document` 的内层 `match extras.format` 增加一个臂，调用一个新的 `export_tiff`。如果新格式支持链接，还要考虑是否需要 `anchors` / `link_resolver`。这正是「分派点集中」的好处：格式扩展只在这一个 `match` 处聚集。

---

### 4.2 四种格式的导出实现与 `*_in_bundle` 钩子

#### 4.2.1 概念说明

typst-bundle 自己**不做任何底层编码**——它不写 PDF 字节、不画 SVG、不渲染像素、不拼 HTML 字符串。这些能力都由兄弟 crate 提供。typst-bundle 之所以需要一套「`*_in_bundle` 钩子」，是因为 bundle 比单文件导出多两样东西：

1. **跨文档链接解析器 `link_resolver`**：单文件导出时，所有链接都在同一个文件内；bundle 里一个文档可以链接到另一个文档，需要把「目标 `Location`」算成「相对路径 + 锚点」。这个解析能力不能在编码阶段临时拼出来，必须由调用方（`export`）传进来。
2. **命名锚点 `anchors`**：要让别的文档能链接进来，自己得先在对应位置埋下「可被命中的锚点」。PDF 用命名目的地、SVG 用可链接的点、HTML 用 DOM `id`。

于是兄弟 crate 各自暴露一个「bundle 专用」的变体函数：`pdf_in_bundle`、`svg_in_bundle`、`html_in_bundle`。它们比普通的 `pdf`/`svg`/`html` 多收了 `link_resolver`（和 `anchors`）。PNG 是个例外——它不支持链接，所以直接复用了普通导出路径 `typst_render::render`，没有专门的 `_in_bundle` 变体。

#### 4.2.2 核心流程

四种格式的导出各走一条路径，但结构相似：**取输入 → 调钩子 → 包成 `Bytes`**。

| 格式 | 导出函数 | 调用的钩子 | 输入 | `anchors` | `link_resolver` | 钩子返回 |
|------|----------|-----------|------|-----------|-----------------|----------|
| PDF | `export_pdf` | `typst_pdf::pdf_in_bundle` | 整文档 `&PagedDocument` | 需要 `&[(Location, EcoString)]` | 需要 | `Vec<u8>` |
| PNG | `export_png` | `typst_render::render` | 单页 `&Page`（`pages()[0]`） | 忽略 | 不需要 | `sk::Pixmap` |
| SVG | `export_svg` | `typst_svg::svg_in_bundle` | 单页 `&Page`（`pages()[0]`） | 需要，先转成 `&[(Point, EcoString)]` | 需要 | `String` |
| HTML | `export_html` | `typst_html::html_in_bundle` | 根元素 `&HtmlElement`（`doc.root()`） | 不走 `extras`（DOM 注入） | 需要 | `String` |

注意「输入」一列的差异：PDF 拿整个文档（可多页），PNG/SVG 只拿第一页（图像格式单页约束，u2-l2 讲过），HTML 拿根元素（不是文档）。

#### 4.2.3 源码精读

**（a）PDF：传整文档，命名目的地**

> [export.rs:77-87](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L77-L87) ——`export_pdf` 把 `doc`、`options.pdf`、`extras.anchors`、`link_resolver` 原样转交给 `typst_pdf::pdf_in_bundle`，再用 `.map(Bytes::new)` 把 `Vec<u8>` 包成 `Bytes`。

钩子侧的实现：

> [typst-pdf/src/lib.rs:40-54](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-pdf/src/lib.rs#L40-L54) ——`pdf_in_bundle` 的文档注释说得很清楚：额外的 `anchors` 会被序列化为**命名目的地（named destinations）**，使 bundle 里其它文档能链接进这份 PDF；`link_resolver` 用来解析跨文档链接。它内部走的是和普通 `pdf` 同一个 `convert`，只是多传了 `anchors` 与 `Some(link_resolver)`。

```rust
pub fn pdf_in_bundle(
    document: &PagedDocument,
    options: &PdfOptions,
    anchors: &[(Location, EcoString)],
    link_resolver: Tracked<LateLinkResolver>,
) -> SourceResult<Vec<u8>> {
    convert::convert(document, options, anchors, Some(link_resolver))
}
```

对照普通导出 [typst-pdf/src/lib.rs:35-38](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-pdf/src/lib.rs#L35-L38)：`pdf()` 调 `convert(document, options, &[], None)`——空 anchors、无 link_resolver。这正是「bundle 版」与「单文件版」的唯一差别。

**（b）PNG：单页光栅化，无链接**

> [export.rs:89-98](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L89-L98) ——`export_png` 调 `typst_render::render(&doc.pages()[0], options)` 得到一个 `tiny-skia` 像素缓冲，再 `encode_png()` 编码成 PNG 字节。注意它取的是 `pages()[0]`——单页约束已经在 `compile_document` 校验过（u2-l2），这里只是消费第一页。

钩子侧：

> [typst-render/src/lib.rs:16-48](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-render/src/lib.rs#L16-L48) ——`render` 按 `pixel_per_pt` 把一页光栅化到 `sk::Pixmap`。注意这个函数签名里**没有任何链接相关的参数**，所以 PNG 不需要也不可能有 `_in_bundle` 变体。

PNG 的错误处理值得一看：编码失败时 `export_png` 把它压成一条 `"failed to encode PNG"`（`.at(Span::detached())`），因为像素编码阶段已经没有有意义的源码 `span` 了。

**（c）SVG：单页，锚点先从 Location 转成 Point**

SVG 是四个函数里**唯一带预处理**的。它的 `anchors` 原始形态是 `(Location, EcoString)`（和 PDF 一样），但 SVG 钩子要的是 `(Point, EcoString)`（平面坐标 + 名字）。所以 `export_svg` 先做一次转换：

> [export.rs:100-125](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L100-L125) ——`export_svg` 先用 `doc.introspector().position(*loc)?.point` 把每个锚点的 `Location` 落到这一页上的具体 `Point`，再交给 `typst_svg::svg_in_bundle`。注释解释：当前只支持单页，所有锚点都应落在这页内，所以可以安全地用文档内省器把 `Location` 转成 `point`。

```rust
let anchors = anchors
    .iter()
    .filter_map(|(loc, name)| {
        let point = doc.introspector().position(*loc)?.point;
        Some((point, name.clone()))
    })
    .collect::<Vec<_>>();
Ok(Bytes::from_string(typst_svg::svg_in_bundle(
    &doc.pages()[0], options, &anchors, link_resolver,
)))
```

钩子侧：

> [typst-svg/src/lib.rs:45-73](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-svg/src/lib.rs#L45-L73) ——`svg_in_bundle` 渲染单页，再用 `renderer.render_anchor(&mut svg, *pos, id)` 把每个锚点画成「可链接的点」。它的注释说这些锚点会被序列化成「linkable points」，让其它文档能链接进这份 SVG。

**（d）HTML：传根元素，锚点是 DOM id**

> [export.rs:127-142](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L127-L142) ——`export_html` 接收 `root: &HtmlElement`（由调用方 `export_document` 传 `doc.root()`），调 `typst_html::html_in_bundle(root, options, link_resolver)` 得到字符串，包成 `Bytes::from_string`。注意它**没有** `anchors` 参数。

钩子侧：

> [typst-html/src/encode.rs:29-40](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-html/src/encode.rs#L29-L40) ——`html_in_bundle` 的文档注释直接指向本讲：「为什么这里收根元素而不是文档，见 typst-bundle 的 `export_html`」。

那么 HTML 的锚点从哪来？答案在 bundle 的 `link.rs`：HTML 的锚点是 DOM `id`，在导出**之前**就被原地注入文档本身了：

> [link.rs:32-40](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/link.rs#L32-L40) ——对 HTML 文档，`create_link_anchors` 调 `typst_html::create_link_anchors(doc, targets)` **原地改写 DOM** 插入 `id`；对 paged 文档，则把锚点写进 `options.anchors`（即 `PagedExtras.anchors`）作为侧车。

这就是为什么 HTML 变体没有 `PagedExtras`、也不在 `export_html` 里传 `anchors`——锚点已经是 DOM 的一部分了，序列化时自然就带出去。

#### 4.2.4 代码实践

**实践目标**：完成规格里要求的核心对比表，并用源码注释解释 `export_html` 为何接收 `root` 而非整文档。

**操作步骤**：

1. 打开 [export.rs:77-142](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L77-L142)，把四个 `export_*` 函数的「钩子调用那一行」逐字抄下来。
2. 打开 [typst-pdf/src/lib.rs:40-54](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-pdf/src/lib.rs#L40-L54)、[typst-svg/src/lib.rs:45-73](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-svg/src/lib.rs#L45-L73)、[typst-html/src/encode.rs:29-40](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-html/src/encode.rs#L29-L40)、[typst-render/src/lib.rs:16-48](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-render/src/lib.rs#L16-L48)，核对每个钩子的签名。
3. 填出下面的对比表，每列都要能指出对应的源码行。

**需要观察的现象**：四行的「钩子 / 输入 / anchors / link_resolver」四列都不完全一样，尤其 PNG 全空、HTML 输入是「根元素」。

**预期结果（自检表）**：

| 格式 | 钩子 | 输入 | 需要 `anchors`？ | 需要 `link_resolver`？ |
|------|------|------|:---:|:---:|
| PDF | `pdf_in_bundle` | 整文档 `&PagedDocument` | ✅（序列化为命名目的地） | ✅ |
| PNG | `render`（非 `_in_bundle`） | 单页 `&Page`（`pages()[0]`） | ❌（忽略） | ❌ |
| SVG | `svg_in_bundle` | 单页 `&Page`（`pages()[0]`） | ✅（先 `Location→Point`，再序列化为可链接点） | ✅ |
| HTML | `html_in_bundle` | 根元素 `&HtmlElement`（`doc.root()`） | ❌（DOM `id`，导出前已原地注入） | ✅ |

**关于「`export_html` 为什么接收 root 而非整文档」**：源码注释 [export.rs:128-133](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L128-L133) 给出了三个理由：

1. 它不需要文档里的 metadata 或 introspector，只用根元素就够了；
2. 这样（只传根元素）它才能被 `#[comemo::memoize]` 干净地缓存；
3. 把 HTML 的 introspector 跨过记忆化边界比 paged 的更棘手——因为 **HTML 文档在构建之后还会被原地改写**（注入链接锚点的 DOM `id`），这意味着它不是「100% 由文档派生」的，introspector 的内容无法和文档一一对应。把根元素作为输入、把 `link_resolver` 作为 `Tracked` 参数，就绕开了「不可哈希的 introspector」这个麻烦。这个点会在 4.3 展开。

#### 4.2.5 小练习与答案

**练习 1**：`export_svg` 里那段 `filter_map`（把 `Location` 转成 `Point`）如果某个 `Location` 的 `position()` 返回 `None`，会发生什么？

**参考答案**：`filter_map` 会用 `?` 直接跳过该锚点（`position(*loc)?.point` 里 `?` 返回 `None`，整个闭包返回 `None`，该锚点被过滤掉）。结合注释「当前只支持单页，所有锚点都应落在这页内」，正常情况下不会发生；它是一道防御性过滤。

**练习 2**：为什么 PNG 用的是普通 `render`，而 PDF/SVG/HTML 都有专门的 `_in_bundle` 钩子？

**参考答案**：`_in_bundle` 钩子存在的意义是「在编码时把跨文档链接和锚点序列化进去」。PNG 是不支持链接的静态图像，没有链接也没有锚点要序列化，所以 bundle 版与普通版**完全一样**，直接复用 `render` 即可，无需额外变体。

---

### 4.3 导出函数的记忆化与时间打点

#### 4.3.1 概念说明

四个 `export_*` 函数头顶都挂着两个宏：

```rust
#[comemo::memoize]
#[typst_macros::time(name = "export pdf")]   // 各自的名字不同
fn export_pdf(...) -> SourceResult<Bytes> { ... }
```

- `#[comemo::memoize]`：让函数按参数缓存返回值。
- `#[typst_macros::time(name = "...")]`：给函数打上「计时探针」，当用户开启计时功能（CLI 的 timing 特性）时，这次调用的耗时会被记录、汇总。

这两个宏的共同点是：**它们都只对「纯函数」有意义**。记忆化要求「相同输入→相同输出」；计时要求「这次调用是一个可独立测量的单元」。typst-bundle 刻意把导出函数写成「输入 → 字节」的纯函数（除了 PNG 编码失败那种外部副作用），正是为了让这两个宏能正确工作。

#### 4.3.2 核心流程

记忆化与计时的协作关系：

```
内省循环（可能多轮）
  │
  每轮都可能再次调用 export_pdf(doc, opts, anchors, link_resolver)
  │
  ├─ 第 1 轮：参数首次出现
  │     ├─ #[time] 开始计时
  │     ├─ 执行 pdf_in_bundle，编码 PDF（昂贵）
  │     ├─ #[time] 结束计时，记录耗时
  │     └─ comemo 以「参数哈希」为键缓存返回值
  │
  ├─ 第 2 轮：参数未变
  │     └─ comemo 命中缓存，直接返回旧结果，不重编码、也不再单独计时
  │
  └─ 参数变化（如 anchors 增多，或 link_resolver 身份变了）
        └─ 视为新输入，重新编码并缓存
```

这里有个关键问题：`link_resolver` 是一个引用了「整个 bundle 内省器」的大对象，怎么参与记忆化的「参数哈希」？答案是它被包成了 `Tracked<LateLinkResolver>`——comemo 对 `Tracked` 哈希的是它的**身份**（identity），而不是它背后的内容。只要内省器在多轮之间是「同一个」（身份不变），`link_resolver` 的哈希就不变，缓存就能命中。

#### 4.3.3 源码精读

四个函数的宏标注完全同构，以 `export_pdf` 为例：

> [export.rs:77-87](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L77-L87) ——`#[comemo::memoize]` + `#[typst_macros::time(name = "export pdf")]` 双标注。注意 `link_resolver` 的类型是 `Tracked<LateLinkResolver>`（不是 `&LateLinkResolver`），这是它能跨越记忆化边界的前提。

四个函数的时间打点名各不相同，方便在计时输出里区分：

| 函数 | time 名 |
|------|---------|
| `export_pdf` | `"export pdf"` |
| `export_png` | `"export png"` |
| `export_svg` | `"export svg"` |
| `export_html` | `"export html"` |

（而 `export()` 本身的时间名是 `"export bundle"`，见 u4-l1；钩子侧 `pdf_in_bundle`/`svg_in_bundle` 又各自有 `"pdf in bundle"`/`"svg in bundle"`。这些名字会在计时报告里层层嵌套出现。）

回到 `export_html` 为何只收根元素——这是记忆化要求的直接产物。读 [export.rs:128-133](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L128-L133) 的注释：

> 「This function takes the root element rather than the document because it doesn't need the metadata or introspector and this way, it can be memoized. Bringing the HTML introspector across the memoization boundary is a little trickier than the paged one because the HTML document is mutated after being built (for linking), which means it's not 100% derived from the document.」

翻译要点：HTML 文档在构建之后还会被原地改写（注入链接锚点的 `id`），所以它「不是 100% 由文档派生」的，其 introspector 无法和文档干净对应、不好直接哈希。于是 `export_html` 只把「根元素」当输入（根元素是可哈希的树结构），把链接解析能力通过 `Tracked<LateLinkResolver>` 传入，绕开了 introspector 跨边界的问题。paged 那边之所以没这个麻烦，是因为 paged 的 `PagedDocument` 在构建后不再改写，锚点是作为独立的 `anchors` 侧车传入的。

#### 4.3.4 代码实践

**实践目标**：在源码里把「记忆化点」和「计时点」逐一标出，并理解 `Tracked` 在其中的角色。

**操作步骤**：

1. 在 [export.rs:77-142](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L77-L142) 里，圈出每个函数头顶的 `#[comemo::memoize]` 和 `#[typst_macros::time(...)]`。
2. 对比 `export_png` 与 `export_pdf` 的参数列表：前者没有 `link_resolver`，后者有。思考：PNG 不需要链接，所以它的记忆化键更小（只有 `doc` + `options`）。
3. 想象一个场景：同一份 `PagedDocument`、同一份 `anchors`，但 `link_resolver` 的身份在两轮之间变了（比如内省器被重建）。根据 `Tracked` 的语义，判断缓存是否命中。

**需要观察的现象**：四个函数都有这两个宏；`export_png` 是唯一不带 `link_resolver` 的。

**预期结果（自检）**：

- 记忆化点：`export_pdf`、`export_png`、`export_svg`、`export_html` 各一个（共 4 个 `#[comemo::memoize]`）。
- 计时点：同上 4 个 `#[typst_macros::time]`，名字分别为 `export pdf/png/svg/html`。
- 若 `link_resolver` 身份变化，`export_pdf`/`export_svg`/`export_html` 的缓存键随之变化，会**未命中**、重新编码；`export_png` 不含 `link_resolver`，不受影响。

**待本地验证**：实际计时输出（需开启 timing 特性并运行一次 bundle 编译）在本环境无法运行，属待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果有人把 `export_html` 的参数从 `root: &HtmlElement` 改成 `doc: &HtmlDocument`（连同它的 introspector），会破坏什么？

**参考答案**：会破坏 `#[comemo::memoize]`。`HtmlDocument` 内含 introspector，而 HTML 文档在构建后会被原地改写、introspector 不是「100% 由文档派生」，难以稳定哈希；即便能哈希，跨边界的语义也不干净。这正是源码注释给出的、坚持只传根元素的理由。

**练习 2**：`#[typst_macros::time]` 和 `#[comemo::memoize]` 同时存在时，第 2 轮缓存命中还会触发计时吗？

**参考答案**：不会。缓存命中时函数体根本不执行，`#[time]` 包裹的执行也就不会发生，因此第 2 轮没有新的计时记录。计时只在真正执行编码时才产生。

---

### 4.4 LateLinkResolver 与跨文档链接解析

#### 4.4.1 概念说明

前几节反复出现 `link_resolver: Tracked<LateLinkResolver>`，现在终于拆开它。`LateLinkResolver`（晚期链接解析器）解决的问题是：**在导出阶段**，把一条「指向某个 `Location` 的链接」翻译成最终的相对 URI。

为什么叫「晚期」？typst 有两个链接解析器（[link.rs:588-651](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L588-L651) 有一段对照说明）：

- `EarlyLinkResolver`：在**编译期**解析链接，HTML 导出用它（因为 HTML 没有独立的「导出阶段」适合做这件事）。
- `LateLinkResolver`：在**导出期**解析链接，paged 导出（PDF/SVG）用它。好处是可以省掉一次内省迭代，且「保持链接不解析」对未来的 PDF 2.0 元素级链接打标有用。

它的核心字段只有两个：

> [link.rs:652-668](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L652-L668) ——`base: Option<&VirtualPath>`（当前文档的路径，单文件导出时为 `None`、bundle 导出时为当前文档路径）与 `introspector: &dyn Introspector`（能查任意 `Location` 的路径和锚点）。

#### 4.4.2 核心流程

解析一条链接 `resolve(location)` 的判定逻辑（[link.rs:675-693](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L675-L693)）：

```
from = self.base                      # 链接所在文档（None 表示单文件）
to   = introspector.path(location)    # 链接目标所在文档
anchor = introspector.anchor(location)# 目标的锚点名

match (from, to):
  (None,   None)   → Local { anchor }            # 单文件：#anchor
  (Some,   Some):
    from == to     → Local { anchor }            # bundle 内同文档：#anchor
    from != to     → Cross { from, to, anchor }  # bundle 内跨文档：相对路径#anchor
  (Some,   None)   → return None                 # 目标不在任何文档里（坏链）
  (None,   Some)   → return None                 # 不该发生（非收敛情况）
```

得到 `ResolvedLink` 后，`into_relative_uri` 把它变成最终字符串（[link.rs:724-748](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L724-L748)）：

- `Local { anchor }` → `#{anchor}`（即便 anchor 为空也写 `#`，因为空 `href` 会触发页面重载，而 `#` 不会）。
- `Cross { from, to, anchor }` → 计算 `to` 相对于 `from.parent()` 的相对路径，做 percent-encoding（[link.rs:752-784](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L752-L784)），再拼上 `#anchor`（anchor 为空则不拼尾随 `#`）。

这套逻辑成立的前提是：传给 `LateLinkResolver` 的 `introspector` 必须是 **bundle 的统一内省器** `BundleIntrospector`（u5-l1 会详讲）——只有它知道「任意 `Location` 属于哪个文档路径、锚点名是什么」。

#### 4.4.3 源码精读

**构造点**（u4-l1 已讲，这里复习关键一行）：

> [export.rs:31-33](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L31-L33) ——`export()` 为每个文档构造 `LateLinkResolver::new(Some(path), bundle.introspector.as_ref())`，再 `.track()` 后传给 `export_document`。`base` 必须是 `Some(path)`（当前文档路径），否则跨文档链接会全部塌缩成 `Local` 或坏链。

**解析点**：

> [link.rs:675-693](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L675-L693) ——`resolve` 的完整 `match`。注意 `(Some(_), None) => return None`：当目标 `Location` 不在任何文档内（比如链接到 bundle 顶层的 metadata）时返回 `None`，调用方据此判定为坏链。这和 `EarlyLinkResolver` 在同样情况下「报错」不同（注释说明这是 paged 与 html 的一种权衡）。

**结果序列化点**：

> [link.rs:724-748](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L724-L748) ——`into_relative_uri`。`Cross` 分支用 `to.relative_from(&from.parent())` 算相对路径，再用 `percent_encode_path`（[link.rs:752-784](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L752-L784)）做严格的 URI 编码（只放行字母数字与 `-._~/`，连 `/` 也保留作为路径分隔符）。

**锚点的来源**（让 `introspector.anchor(location)` 有值可查）：在导出之前，bundle 会先收集「哪些 `Location` 被链接到」（`link_targets`），再为它们生成锚点：

> [link.rs:69-88](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/link.rs#L69-L88) ——`create_paged_link_anchors` 用 `AnchorGenerator` 为每个被链接的目标分配一个锚点名（尽量复用 label，否则生成 `loc-N`），写进 `PagedExtras.anchors`。这些锚点随后既被 `export_pdf`/`export_svg` 序列化进文件，也被统一内省器登记，使 `LateLinkResolver::resolve` 能查到 `anchor`。

把整条链路串起来：`link_targets`（谁被链接）→ `create_link_anchors`（分配锚点名，paged 写进 `PagedExtras.anchors`、html 注入 DOM）→ 内省器登记锚点 → `export_*`（编码时用 `LateLinkResolver` 把链接解析成相对 URI，并把 paged 锚点序列化进文件）。

#### 4.4.4 代码实践

**实践目标**：用一个具体的跨文档链接，手动走一遍 `resolve` 的 `match`，预测最终相对 URI。

**操作步骤**：

1. 设想 bundle 里有 `index.html` 与 `appendix.html` 两个文档。`index.html` 里有一条 `#link(<glossary>)`，`<glossary>` 是 `appendix.html` 里的一个标题。
2. 确定 `export_html` 拿到的 `link_resolver` 的 `base` 是什么（[export.rs:31-33](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L31-L33)）。
3. 设 `<glossary>` 的 `Location` 经统一内省器查出 `to = "appendix.html"`、`anchor = "glossary"`。用 [link.rs:675-693](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L675-L693) 的 `match` 判断命中哪条臂。
4. 用 [link.rs:724-748](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L724-L748) 的 `into_relative_uri` 推算最终字符串。

**需要观察的现象**：因为 `from("index.html") != to("appendix.html")`，应命中 `Cross` 分支。

**预期结果（自检）**：

- `base = Some(&"index.html")`。
- `from = Some("index.html")`、`to = Some("appendix.html")`、`anchor = "glossary"`，命中 `(Some, Some)` 且 `from != to` → `ResolvedLink::Cross { from: "index.html", to: "appendix.html", anchor: "glossary" }`。
- `into_relative_uri`：`from.parent()` 是 bundle 根，`to.relative_from(根)` = `"appendix.html"`，percent-encode 后仍为 `appendix.html`，anchor 非空 → 最终 URI 为 `appendix.html#glossary`。
- 若把 `base` 改成 `None`（错误用法）：`from = None`、`to = Some("appendix.html")`，命中 `(None, Some(_)) => return None`，链接解析失败成坏链。这正是 u4-l1 强调「bundle 里 `base` 必须是 `Some(path)`」的原因。

**待本地验证**：实际编译产物里 `href` 的具体值（需 `--features bundle,html` 运行）属待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`ResolvedLink::Local { anchor }` 在 anchor 为空时，`into_relative_uri` 返回 `#`（一个井号）而不是空串。为什么？

**参考答案**：见 [link.rs:726-728](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L726-L728) 的注释：空 `href` 会触发浏览器页面重载，而 `#` 不会。anchor 为空通常表示「链接到文档自身」，所以写一个 `#` 既保持「指向当前文档」的语义，又避免重载。

**练习 2**：为什么 `LateLinkResolver::resolve` 在 `(Some(_), None)` 时返回 `None`（坏链），而 `EarlyLinkResolver::resolve` 在同样情况会 `bail!` 报错？

**参考答案**：见 [link.rs:641-651](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L641-L651) 的注释。这是 paged 与 html 的一种权衡：late 方案省了一次内省迭代，代价是在「非收敛」场景下链接可能静默失效（返回 `None`），而 html 的 early 方案会明确报错。作者也承认这不是完全清晰的取舍。

---

## 5. 综合实践

**任务**：把本讲的「格式分派 / 钩子 / 记忆化 / 链接解析」四条线，用一个混合格式的 bundle 串起来，做一次端到端追踪。

**实践目标**：给定一份包含多种格式与跨链的 bundle 源文件，能逐文件说清「它进了哪个 `export_*`、调了哪个钩子、用没用 `anchors`/`link_resolver`、记忆化键长什么样」，并能预测跨文档链接的最终 URI。

**操作步骤**：

1. 准备如下 bundle 源（示例代码，非项目原有）：

   ```typ
   // main.typ —— 示例代码
   #document("paper.pdf")[
     = 引言 <intro>
     正文，详见 #link(<glossary>)[术语表]。
   ]

   #document("index.html")[
     = 首页
     看看 #link(<intro>)[PDF 的引言]。
   ]

   #document("logo.svg")[#image("logo.svg")]   // 单页图像

   #document("cover.png")[Cover]               // 单页图像

   #asset("meta.json", #context json.encode({ "title": "demo" }))
   ```

2. 对照 [export.rs:61-74](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L61-L74)，为每个文件填出下表：

   | 文件 | `BundleDocument`/`BundleFile` | `export_*` | 钩子 | `anchors`? | `link_resolver`? | 记忆化键 |
   |------|------------------------------|-----------|------|:---:|:---:|----------|
   | paper.pdf | … | … | … | … | … | … |
   | index.html | … | … | … | … | … | … |
   | logo.svg | … | … | … | … | … | … |
   | cover.png | … | … | … | … | … | … |
   | meta.json | … | （不走 `export_*`） | — | — | — | — |

3. 对 `index.html → paper.pdf` 的 `#link(<intro>)`，按 4.4.4 的方法预测 `href` 的值。
4. 说明：若内省循环跑了两轮且参数未变，`paper.pdf` 会被编码几次？依据 [export.rs:77-87](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L77-L87) 的 `#[comemo::memoize]` 回答。

**需要观察的现象**：五个文件走了四条不同的导出路径（其中 asset 不走任何 `export_*`）；跨文档链接被解析成相对 URI。

**预期结果（自检）**：

| 文件 | 类型 | `export_*` | 钩子 | anchors | link_resolver | 记忆化键 |
|------|------|-----------|------|:---:|:---:|----------|
| paper.pdf | `Paged/Pdf` | `export_pdf` | `pdf_in_bundle` | ✅ | ✅ | (doc, pdf-opts, anchors, tracked-link_resolver) |
| index.html | `Html` | `export_html` | `html_in_bundle` | ❌（DOM 注入） | ✅ | (root, html-opts, tracked-link_resolver) |
| logo.svg | `Paged/Svg` | `export_svg` | `svg_in_bundle` | ✅（先转 Point） | ✅ | (doc, svg-opts, anchors, tracked-link_resolver) |
| cover.png | `Paged/Png` | `export_png` | `render` | ❌ | ❌ | (doc, png-opts) |
| meta.json | `Asset` | —（直通 `bytes.clone()`） | — | — | — | — |

- `index.html → paper.pdf` 的 `<intro>`：`base=Some("index.html")`、`to="paper.pdf"`、`anchor="intro"` → `Cross` → 相对路径 `paper.pdf`，最终 `href = paper.pdf#intro`。
- 内省两轮、参数未变：`paper.pdf` 只编码**一次**，第 2 轮 `export_pdf` 缓存命中、不重编码。

**待本地验证**：上述 `href` 与「只编码一次」的行为，需用 `typst compile --features bundle,html main.typ` 实际编译并检查产物后确认，本环境无法运行，属待本地验证。

## 6. 本讲小结

- `export_document` 是唯一的格式分派点：第一层 `match doc` 分 `Paged` / `Html`，`Paged` 内再 `match extras.format` 分 `Pdf` / `Png` / `Svg`，最后落到四个 `export_*` 函数。
- 四种格式各自调用一个钩子：PDF→`pdf_in_bundle`、SVG→`svg_in_bundle`、HTML→`html_in_bundle`，均由兄弟 crate 提供、比普通导出多收 `link_resolver`（和 `anchors`）；PNG 是例外，直接复用普通 `typst_render::render`，无链接能力。
- 输入形态各异：PDF 拿整文档（可多页），PNG/SVG 拿 `pages()[0]`（单页），HTML 拿 `doc.root()`（根元素）。
- `anchors` 的去向不同：PDF 序列化为命名目的地、SVG 先 `Location→Point` 再序列化为可链接点；PNG 忽略；HTML 不走 `extras`，而是在导出前由 `create_link_anchors` 原地注入 DOM `id`。
- 四个 `export_*` 都挂 `#[comemo::memoize]`（按参数缓存，避免内省多轮时重复编码）和 `#[typst_macros::time]`（计时探针）；`link_resolver` 以 `Tracked<LateLinkResolver>` 跨过记忆化边界（哈希身份而非内容）。
- `export_html` 只收根元素，是因为 HTML 文档在构建后会被原地改写（注入锚点 `id`）、introspector「不是 100% 由文档派生」，难以跨记忆化边界——传根元素 + `Tracked` 绕开了这个麻烦。
- `LateLinkResolver::resolve` 用 `(base, introspector.path(loc))` 的二元组判定链接是 `Local` 还是 `Cross`，`into_relative_uri` 再算出相对路径 + percent-encoding；`base` 必须是 `Some(当前文档路径)`，否则跨文档链接全部失败。

## 7. 下一步学习建议

本讲讲完了「导出阶段如何把 `BundleDocument` 编码成字节、如何解析跨文档链接」。但有两块底层支撑还没展开，建议接下来进入第 5 单元（专家层）：

- **u5-l1「统一内省器 BundleIntrospector」**：本讲反复依赖的 `bundle.introspector`（让 `LateLinkResolver` 能查任意 `Location` 的路径和锚点）究竟是怎么把多个文档聚合成一个内省循环的。它是跨文档链接能成立的基石。
- **u5-l2「跨文档链接与锚点：create_link_anchors」**：本讲只点到「PDF/SVG 的 `anchors` 来自 `create_paged_link_anchors`、HTML 的锚点是 DOM 注入」，那一讲会完整拆解 `link_targets` 如何收集链接目标、`AnchorGenerator` 如何分配锚点名、以及「为 document/asset 自身创建空锚点」的用意。
- **u5-l3「并行与记忆化」**：会把本讲的 `#[comemo::memoize]` 放到「rayon 并行编译 + 统一内省收敛」的大图里，解释这套顺序如何保证多文档互相内省的正确性。

如果你更关心 CLI 如何驱动本讲的 `export`（落盘、HTTP 模式），可以直接跳到 **u5-l4「CLI 集成与端到端实践」**，那里会把 `export_bundle → typst_bundle::export → write_virtual_fs` 的落盘链路补全。
