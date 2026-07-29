# export 主流程与 VirtualFs

## 1. 本讲目标

本讲聚焦 `typst-bundle` 把编译产物 `Bundle` 转换为最终字节输出的**导出阶段**。读完本讲，你应当能够：

- 看懂 `export.rs` 中 `export()` 如何用 rayon **并行**遍历 `Bundle.files`，并为每个文档构造一个 `LateLinkResolver`。
- 理解 `BundleOptions` 如何把 HTML / PDF / PNG / SVG 四种格式的导出选项**聚合**到一个结构体里。
- 理解 `VirtualFs`（`IndexMap<VirtualPath, Bytes>`）为什么是 bundle 的**最终输出形态**，以及 asset 为什么可以「直通」。
- 能在源码中追踪一个 `BundleFile::Asset` 与一个 `BundleFile::Document` 在 `export()` 里各自走哪条路径，并解释 `LateLinkResolver::new(Some(path), ...)` 里的 `Some(path)` 为什么不可换成 `None`。

本讲是导出层的「总览篇」，只讲 `export()` 主流程、选项聚合与输出形态；至于四种格式各自如何分派与编码（`export_pdf`/`export_png`/`export_svg`/`export_html` 的细节）留到下一讲 u4-l2。

## 2. 前置知识

本讲承接 u3-l1 建立的 bundle **静态数据模型**，不再重复讲解。这里只做最小回顾：

- 顶层产物 [`Bundle`](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L44-L54) 有两个字段：`files`（`Arc<IndexMap<VirtualPath, BundleFile>>`）与 `introspector`（`Arc<BundleIntrospector>`）。
- [`BundleFile`](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L74-L82) 分两类：`Document(BundleDocument)`（由 `#document` 产生、需要排版）与 `Asset(Bytes)`（由 `#asset` 产生、原始字节）。
- [`PagedExtras`](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L103-L114) 挂在 `Paged` 变体上，携带 `format`（最终编码成哪种字节）与 `anchors`（跨文档命名锚点）。

此外，你需要知道几个非 bundle 专属的通用概念：

- **rayon**：Rust 的数据并行库。`par_iter()` 让迭代器在多个线程上并行执行，`map` / `collect` 的语义与标准库一致，但回调可能并发运行。
- **`Tracked<T>`**：comemo 提供的「可追踪引用」。被 `#[comemo::track]` 标注的类型（如 `LateLinkResolver`）通过 `.track()` 得到一个 `Tracked<LateLinkResolver>`，可以廉价地跨线程/跨记忆化边界传递，且其查询结果会被 comemo 缓存。
- **跨文档链接**：bundle 里一个文档里的 `#link(<label>)` 可以指向另一个文档里的元素。导出时必须把这个「逻辑 Location」翻译成「相对文件路径 + 片段锚点」（例如 `../appendix.html#glossary`）。这正是导出阶段要解决的核心问题之一。

## 3. 本讲源码地图

本讲只涉及两个源码文件，外加一个跨 crate 的链接解析类型：

| 文件 | 作用 |
| --- | --- |
| [crates/typst-bundle/src/export.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs) | 导出层全部代码：`export()` 主流程、`VirtualFs` 类型、`BundleOptions`、以及四个格式分派函数（本讲只用到前三个）。 |
| [crates/typst-bundle/src/lib.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs) | 提供 `Bundle` / `BundleFile` / `BundleDocument` / `PagedExtras` 数据模型（已在 u3-l1 详述）。 |
| [crates/typst-library/src/model/link.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs) | 定义 `LateLinkResolver` 与 `ResolvedLink`——本讲要解释为什么 `export()` 必须给每个文档传入 `Some(path)` 而非 `None`。 |

> 提示：`export.rs` 在 `lib.rs` 里通过 `#[path = "export.rs"] mod export_;` 引入（见 [lib.rs:3-4](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L3-L4)），再经 `pub use self::export_::{BundleOptions, VirtualFs, export};`（[lib.rs:10](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L10)）对外暴露。所以外部看到的名字是 `typst_bundle::export`，模块内部叫 `export_`。

---

## 4. 核心概念与源码讲解

### 4.1 export() 主流程：并行遍历与 LateLinkResolver 构造

#### 4.1.1 概念说明

导出阶段的任务非常明确：**把 `Bundle`（一堆内存中的文档对象）变成可以写进磁盘的字节**。

这里有一个关键的设计取舍：bundle 里通常有多个文件（多个文档 + 多个 asset），它们彼此**独立**——导出 `index.html` 不需要等 `cover.pdf` 先导出完。因此 `export()` 用 rayon 把这些文件的导出**并行化**，让多核 CPU 同时干活。

但文档之间并非完全独立：它们通过 `#link` 互相引用。导出单个文档时，需要把它内部所有「指向别处」的链接翻译成相对路径。这需要一个「链接解析器」，并且这个解析器必须知道**当前文档自己的路径**，才能算出「从当前文档到目标文档的相对路径」。这就是 `LateLinkResolver`（晚期链接解析器）的职责。

#### 4.1.2 核心流程

`export()` 的执行过程可以用下面的伪代码描述：

```
输入: bundle: &Bundle, options: &BundleOptions
输出: SourceResult<VirtualFs>  // 即 IndexMap<VirtualPath, Bytes>

对 bundle.files 中的每个 (path, file) 【并行】做：
    匹配 file:
        Document(doc):
            link_resolver = LateLinkResolver::new(Some(path), bundle.introspector)
            字节 = export_document(doc, options, link_resolver.track())
        Asset(bytes):
            字节 = Ok(bytes.clone())   // 直接搬运，不解析链接
    返回 (path, 字节)

用 collect_combined_result 把所有 (path, 字节) 汇总成 VirtualFs：
    - 把每个线程的结果收集到一个 Vec（不短路，全部跑完）
    - 累加所有错误；若有任何一个文档导出失败，返回 Err(全部错误)
    - 否则 Ok(汇总后的 IndexMap)
```

三个要点：

1. **并行遍历**：`par_iter()` 让每个文件在独立线程导出。
2. **文档才需要链接解析器**：只有 `BundleFile::Document` 会构造 `LateLinkResolver` 并调用 `export_document`；`Asset` 走直通分支。
3. **错误合并而非短路**：`collect_combined_result` 会把所有文档的错误**合并**上报，而不是遇到第一个错误就停（详见 4.1.3）。

#### 4.1.3 源码精读

先看整个 `export()` 函数：

[crates/typst-bundle/src/export.rs:22-40](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L22-L40) — 这是 `export()` 的全部代码：它接收一个 `&Bundle` 和 `&BundleOptions`，用 rayon 并行遍历 `bundle.files`，把每个文件导出成字节，最后汇总成 `VirtualFs`。

关键代码片段（精简后）：

```rust
#[typst_macros::time(name = "export bundle")]
pub fn export(bundle: &Bundle, options: &BundleOptions) -> SourceResult<VirtualFs> {
    bundle.files.par_iter().map(|(path, file)| {
        let data = match file {
            BundleFile::Document(doc) => {
                let link_resolver =
                    LateLinkResolver::new(Some(path), bundle.introspector.as_ref());
                export_document(doc, options, link_resolver.track())
            }
            BundleFile::Asset(bytes) => Ok(bytes.clone()),
        };
        data.map(|data| (path.clone(), data))
    }).collect_combined_result()
}
```

逐行拆解：

- `#[typst_macros::time(name = "export bundle")]`（[export.rs:23](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L23)）：给整个导出过程打上计时标签，开启 `TIMINGS` 时会输出 `export bundle` 耗时。
- `bundle.files.par_iter()`（[export.rs:27](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L27)）：把 `IndexMap` 转成 rayon 并行迭代器，`map` 闭包会在多个线程并发执行。
- `BundleFile::Document(doc)` 分支（[export.rs:30-34](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L30-L34)）：为该文档构造一个 `LateLinkResolver`，其 `base` 设为 `Some(path)`（即「当前文档相对 bundle 根的路径」），`introspector` 设为 `bundle.introspector`（整个 bundle 的统一内省器）。随后 `.track()` 把它转成 `Tracked<LateLinkResolver>` 传入 `export_document`。
- `BundleFile::Asset(bytes)` 分支（[export.rs:35](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L35)）：asset 不参与排版、不含链接，直接 `Ok(bytes.clone())` 把原始字节搬运出去。
- `data.map(|data| (path.clone(), data))`（[export.rs:37](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L37)）：把 `SourceResult<Bytes>` 映射成 `SourceResult<(VirtualPath, Bytes)>`，凑成「路径 → 字节」键值对。
- `.collect_combined_result()`（[export.rs:39](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L39)）：把并行迭代器汇总成 `SourceResult<VirtualFs>`。

**关于 `collect_combined_result` 的错误语义**（这是个容易被忽视但很重要的细节）：它来自 `typst_library::diag::ParallelCollectCombinedResult`，定义在 [crates/typst-library/src/diag.rs:255-278](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/diag.rs#L255-L278)。并行版本先 `self.collect::<Vec<_>>()`（**全部并行任务都跑完，不短路**），再委托给顺序版本的 `collect_combined_result`（[diag.rs:231-249](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/diag.rs#L231-L249)），后者用 `filter_map` 把 `Ok` 的结果收集起来、把 `Err` 的错误**累加**进一个 `EcoVec`，只要有任何错误就返回 `Err(全部错误)`。

> 含义：即便 bundle 里有 5 个文档导出失败，`export()` 也会把这 5 个文档的错误**一次性全部**上报给用户，而不是只报第一个就停。这对调试多文档项目很友好。

**为什么 `Some(path)` 而不是 `None`？**

这是本讲的核心问题之一。看 `LateLinkResolver` 的定义与 `resolve` 方法：

[crates/typst-library/src/model/link.rs:652-669](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L652-L669) — `LateLinkResolver` 只有两个字段：`base: Option<&VirtualPath>`（当前文档的路径）和 `introspector: &dyn Introspector`（用于查询目标位置所在文档与锚点）。其 `new()` 的文档明确写到：「单文档导出时 `base` 应为 `None`；bundle 导出时 `base` 应为当前文档的路径，链接将相对它解析」。

[crates/typst-library/src/model/link.rs:671-694](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L671-L694) — `resolve()` 的核心是一个对 `(from, to)` 的 `match`：

```rust
pub fn resolve(&self, location: Location) -> Option<ResolvedLink> {
    let from = self.base;                              // 当前文档路径
    let to = self.introspector.path(location);         // 目标所在文档路径
    let anchor = self.introspector.anchor(location)?.clone();
    Some(match (from, to) {
        (None, None) => ResolvedLink::Local { anchor },          // 单文档：同文档锚点
        (Some(from), Some(to)) => {                              // bundle：判断是否跨文档
            if from == to { ResolvedLink::Local { anchor } }
            else { ResolvedLink::Cross { from, to, anchor } }    // 跨文档：算相对路径
        }
        (Some(_), None) => return None,   // 目标不在任何文档里：链接失败
        (None, Some(_)) => return None,    // 理论上不应发生
    })
}
```

- 如果把 `base` 传成 `None`，那么 `from = None`。此时只要目标 `to` 是 `Some`（即链接指向某个文档里的元素），就会命中 `(None, Some(_)) => return None` 分支——**所有跨文档链接都会解析失败**，导出的 HTML/PDF/SVG 里这些链接会变成坏链。
- 只有传 `Some(path)`，`from` 才有值，才能和 `to` 比较出「是否在同一文档」，并在跨文档时产出 `ResolvedLink::Cross { from, to, anchor }`，进而由 `into_relative_uri()`（[link.rs:724-748](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L724-L748)）算出形如 `../appendix.html#glossary` 的相对路径。

换句话说，`Some(path)` 是 bundle 跨文档链接能够工作的**前提条件**；`None` 只适用于「整个 bundle 只有一个文档」的单文档导出场景。

#### 4.1.4 代码实践

**实践目标**：在源码中追踪一个 `BundleFile::Asset` 与一个 `BundleFile::Document` 在 `export()` 中各自走的路径，并验证 `Some(path)` 的必要性。

**操作步骤（源码阅读型实践）**：

1. 打开 [export.rs:24-40](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L24-L40)，找到 `par_iter().map(...)` 闭包里的 `match file`。
2. **追踪 Asset**：假设 `files` 里有一项 `("note.txt", BundleFile::Asset(bytes))`。
   - 它命中 [export.rs:35](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L35) 的 `BundleFile::Asset(bytes) => Ok(bytes.clone())`。
   - **不**构造 `LateLinkResolver`，**不**调用 `export_document`，**不**读取 `options`。字节原样克隆。
   - 最终作为 `("note.txt", bytes)` 进入 `VirtualFs`。
3. **追踪 Document**：假设有一项 `("index.html", BundleFile::Document(BundleDocument::Html(doc)))`。
   - 它命中 [export.rs:30-34](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L30-L34)。
   - 构造 `LateLinkResolver::new(Some(&"index.html"), bundle.introspector)`。
   - 调用 `export_document(doc, options, link_resolver.track())`，后者再分派到 `export_html`（下一讲详述）。
4. **验证 `Some(path)` 的必要性**：跳到 [link.rs:675-693](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L675-L693)。假设把 `export.rs:32` 改成 `LateLinkResolver::new(None, bundle.introspector.as_ref())`，那么 `from` 恒为 `None`。回答：当 `index.html` 里有一条指向 `appendix.html` 中 `<glossary>` 标签的链接时，`to` 会是 `Some("appendix.html")`，此时命中哪个分支？结果是什么？

**需要观察的现象 / 预期结果**：

- Asset 路径完全绕过格式导出与链接解析，是最快的分支。
- Document 路径必须为每个文档单独构造一个绑定了该文档路径的 `LateLinkResolver`。
- 第 4 步的答案：会命中 `(None, Some(_)) => return None`，`resolve` 返回 `None`，该跨文档链接**无法解析**，导出后为坏链。这就是为什么 bundle 里**必须**用 `Some(path)`。
- 本实践为源码阅读型，无需运行编译器；若要实际观察坏链现象，需自行修改源码后用 `cargo build` 重新编译 typst（**注意：本讲义不要求也不建议修改源码**，仅作思维实验）。

#### 4.1.5 小练习与答案

**练习 1**：`export()` 为什么对 `Asset` 分支不传入 `options`，也不构造 `LateLinkResolver`？

> **参考答案**：asset 是 `#asset` 产生的原始字节（`Bytes`），既不经过排版、也不含任何 Typst 链接，它的最终字节在编译阶段（`bundle_impl`）就已经确定。导出时只需把已有字节克隆进 `VirtualFs`，因此既不需要格式选项，也不需要链接解析器。

**练习 2**：假设一个 bundle 有 100 个文档，其中第 3、第 17 个导出失败。用 `collect_combined_result` 汇总后，`export()` 返回什么？另外 98 个文档的字节会丢失吗？

> **参考答案**：`collect_combined_result` 会先让所有 100 个文档的并行导出**全部跑完**，然后把第 3、第 17 个的错误**合并**进一个 `EcoVec`，返回 `Err(这两个错误)`。由于返回的是 `Err`，整个 `VirtualFs` 不会被产出——从外部看，这次导出失败了。失败的 98 个文档字节虽然被算出来了，但因为整体是 `Err`，它们不会落到最终结果里（这是「整体成功或整体失败」的语义）。

---

### 4.2 BundleOptions：四种格式选项的聚合

#### 4.2.1 概念说明

bundle 一次可以导出多种格式的文档：PDF、PNG、SVG、HTML。**每种格式都有自己的导出选项**——例如 PDF 有是否嵌入字体、PNG 有像素比例（pixel-per-pt）、SVG 有是否压缩等。这些选项分别定义在各自的 crate 里：`PdfOptions`（typst-pdf）、`RenderOptions`（typst-render，用于 PNG）、`SvgOptions`（typst-svg）、`HtmlOptions`（typst-html）。

`BundleOptions` 做的事情很简单：**把四种格式的选项打包到一个结构体里**，这样 `export()` 只需要接收一个参数，就能在导出任意格式文档时取出对应的那一份选项。这是一个典型的「参数聚合」模式。

#### 4.2.2 核心流程

```
BundleOptions
├── html: HtmlOptions      // HTML 文档导出选项（typst-html）
├── pdf: PdfOptions        // PDF 文档导出选项（typst-pdf）
├── png: RenderOptions     // PNG 文档导出选项（typst-render）
└── svg: SvgOptions        // SVG 文档导出选项（typst-svg）
```

`export()` 接收 `&BundleOptions`，传给 `export_document`；`export_document` 根据文档格式从 `options` 里取对应字段（如 `&options.pdf`），再传给具体的格式导出函数。

#### 4.2.3 源码精读

[crates/typst-bundle/src/export.rs:42-53](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L42-L53) — `BundleOptions` 的定义，四个公开字段分别对应四种格式：

```rust
#[derive(Debug, Default)]
pub struct BundleOptions {
    pub html: HtmlOptions,
    pub pdf: PdfOptions,
    pub png: RenderOptions,
    pub svg: SvgOptions,
}
```

几个要点：

- `#[derive(Default)]`：四种选项类型都实现了 `Default`，因此 `BundleOptions::default()` 就能得到一份「全默认」的导出配置。CLI 在没有显式指定选项时就是用它。
- 四个字段的类型来自四个不同的 crate（见 [export.rs:6-13](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L6-L13) 的 `use`），这正体现了 typst-bundle 作为「编排层」组合兄弟 crate 的定位。
- 字段都是 `pub`，调用方（如 typst-cli）可以按需只设置某一类选项，其余保持默认。

`BundleOptions` 如何被消费？看 `export_document` 的签名与分派：

[crates/typst-bundle/src/export.rs:55-75](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L55-L75) — `export_document` 按 `BundleDocument` 的变体与 `PagedExtras.format` 分派，从 `options` 里取出对应字段：

```rust
fn export_document(doc, options: &BundleOptions, link_resolver) -> SourceResult<Bytes> {
    match doc {
        BundleDocument::Paged(doc, extras) => match extras.format {
            PagedFormat::Pdf  => export_pdf(doc, &options.pdf, &extras.anchors, link_resolver),
            PagedFormat::Png  => export_png(doc, &options.png),
            PagedFormat::Svg  => export_svg(doc, &options.svg, &extras.anchors, link_resolver),
        },
        BundleDocument::Html(doc) => export_html(doc.root(), &options.html, link_resolver),
    }
}
```

可以看到：PDF 用 `&options.pdf`、PNG 用 `&options.png`、SVG 用 `&options.svg`、HTML 用 `&options.html`。`BundleOptions` 只是个「选项容器」，真正的格式分派细节是下一讲 u4-l2 的主题，这里只需理解它**把四份选项放在一处**即可。

> 注意：`export_png` 的参数列表里**没有** `link_resolver`（对比 PDF/SVG 都有）。这是因为 PNG 不支持链接（见 link.rs 文档「PNG documents do not support linking」），所以连解析器都不传。这个差异会在 u4-l2 详细展开。

#### 4.2.4 代码实践

**实践目标**：确认 `BundleOptions` 与四种格式选项类型的对应关系，并理解 CLI 如何填充它。

**操作步骤（源码阅读型实践）**：

1. 在 [export.rs:6-14](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L6-L14) 的 `use` 列表里，找到 `HtmlOptions`、`PdfOptions`、`RenderOptions`、`SvgOptions` 各自来自哪个 crate。
2. 打开 `crates/typst-cli/src/compile.rs`，搜索 `BundleOptions`（或 `export_bundle`），观察 CLI 是如何构造 `BundleOptions` 的——通常会从 CLI 参数里取出 pdf/png/svg/html 各自的选项再组装。
3. 记录：CLI 是否为四种格式都填充了选项，还是只填了用户显式指定的那几种、其余走 `Default`？

**需要观察的现象 / 预期结果**：

- 四种选项类型分别来自 `typst_html` / `typst_pdf` / `typst_render` / `typst_svg`。
- CLI 通常按需填充，未指定的格式保持默认。具体填充方式**待本地阅读 `compile.rs` 确认**（u5-l4 会专门讲 CLI 集成）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `BundleOptions` 要把四种格式的选项**全部**放在一个结构体里，而不是给 `export()` 传四个独立参数，或者让每种格式有独立的导出入口？

> **参考答案**：因为 bundle 一次编译会**同时**产出多种格式的文档——同一个 bundle 里可能既有 PDF 又有 HTML。把所有选项聚合到 `BundleOptions`，`export()` 只需一个参数就能覆盖任意格式的文档，调用方（CLI）也只需构造一次。如果拆成四个参数或四个入口，`export()` 内部还得自己根据每个文档的格式去挑选正确的选项，反而更繁琐。

**练习 2**：如果用户只导出 HTML 文档、完全不涉及 PDF/PNG/SVG，`BundleOptions` 里的 `pdf`/`png`/`svg` 字段会被用到吗？

> **参考答案**：不会被用到——`export_document` 只在 `PagedFormat::Pdf` 时读 `options.pdf`，既然没有 PDF 文档，该字段就从不被访问。但因为 `#[derive(Default)]`，这些字段仍然有默认值存在，只是空闲着，不会带来额外开销（选项本身只是配置数据，不持有昂贵资源）。

---

### 4.3 VirtualFs：路径到字节的最终输出形态（含 asset 直通）

#### 4.3.1 概念说明

导出的最终产物是什么？不是单个 PDF 文件，也不是某个文档对象，而是一棵**虚拟文件系统**——一组「路径 → 字节」的映射。这个抽象非常自然：bundle 的目标就是「一次编译产出多个文件」，而一组文件在内存里最直接的表示就是「文件名 → 文件内容」的字典。

Typst 把这个字典类型叫做 `VirtualFs`。之所以叫「Virtual」（虚拟），是因为它**还在内存里**、尚未落盘。真正写到磁盘（或发给 HTTP 客户端）是更外层（typst-cli）的工作——`VirtualFs` 是 typst-bundle 与外界之间的**交接格式**。

#### 4.3.2 核心流程

```
VirtualFs = IndexMap<VirtualPath, Bytes>

来源（bundle.files 的每一项）:
  (path, BundleFile::Document(doc))  ──export_document──►  (path, 字节)
  (path, BundleFile::Asset(bytes))   ──clone 直通────────►  (path, bytes)
```

`VirtualFs` 保留了 `bundle.files` 的**插入顺序**（因为底层是 `IndexMap`），这对「按确定顺序写盘/展示」很重要。

#### 4.3.3 源码精读

[crates/typst-bundle/src/export.rs:19-20](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L19-L20) — `VirtualFs` 的完整定义只有一行类型别名：

```rust
/// A raw mapping from paths to bytes.
pub type VirtualFs = IndexMap<VirtualPath, Bytes, FxBuildHasher>;
```

逐项拆解：

- `IndexMap` 而非 `HashMap`：保留键的**插入顺序**。bundle 里文件的顺序由 `bundle_impl` 装 `files` 的循环决定（见 [lib.rs:202-213](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L202-L213)），导出后这个顺序被 `VirtualFs` 完整保留。
- 键是 `VirtualPath`（来自 `typst-syntax`）：表示一个**虚拟**路径，即相对于 bundle 根的输出路径，如 `index.html`、`assets/logo.png`。它不是磁盘上的真实路径，所以叫 virtual。
- 值是 `Bytes`（来自 `typst_library::foundations`）：一段不可变的字节缓冲，可廉价克隆（引用计数）。
- 哈希器是 `FxBuildHasher`（来自 `rustc-hash`）：比标准 `RandomState` 更快的非加密哈希，适用于键数量有限、无需抗碰撞攻击的场景。这与 `Bundle.files` 的哈希器一致（见 [lib.rs:47](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L47)）。

**asset 直通**：再回看 `export()` 里的 `BundleFile::Asset(bytes) => Ok(bytes.clone())`（[export.rs:35](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L35)）。这里 `bytes` 的类型就是 `Bytes`，与 `VirtualFs` 的值类型**完全一致**，所以 asset 字节可以零转换地塞进 `VirtualFs`。这就是「直通」：asset 在编译阶段是什么字节，导出阶段就原样是什么字节，没有任何再加工。

对比一下：`BundleFile::Document` 的值是 `BundleDocument`（一个复杂的文档对象），必须经过 `export_document` 编码（PDF 编码、PNG 光栅化、SVG 序列化、HTML 序列化）才能变成 `Bytes`；而 `BundleFile::Asset` 的值**已经是** `Bytes`，无需任何编码。

最后，`export()` 的返回值经 `collect_combined_result` 汇总后，正是这个 `VirtualFs`。它会被返回给调用方（通常是 typst-cli 的 `export_bundle`），由后者遍历这个 `IndexMap`、把每个 `(path, bytes)` 写到磁盘上对应路径的文件里（u5-l4 会讲 `write_virtual_fs`）。

#### 4.3.4 代码实践

**实践目标**：亲手构造一个 bundle，观察它导出后的 `VirtualFs` 形态（即最终文件清单）。

**操作步骤**：

1. 在一个空目录里新建 `main.typ`，内容如下（这是一个最小的 bundle 源文件）：

   ```typ
   #asset("data.txt", read("data.txt"))
   #document("hello.html")[Hello, world!]
   ```

   并在同目录放一个 `data.txt`，内容随意（例如 `hello asset`）。
2. 用实验性 bundle 特性编译（需要开启 `bundle` feature）：

   ```bash
   typst compile --features bundle main.typ
   ```

   > 注意：bundle 是实验性特性，`--features bundle` 是允许编译 bundle 的开关（见 u1-l1）。具体命令行参数以你本地的 typst 版本为准；若命令不可用，标注「待本地验证」。
3. 观察输出目录。

**需要观察的现象 / 预期结果**：

- 输出目录里应出现两个文件：`data.txt`（asset，内容与源文件一致）和 `hello.html`（document，内容是序列化后的 HTML）。
- 这两个文件**就是 `VirtualFs` 落盘后的结果**：`VirtualFs` 里有两条映射——`("data.txt", <data.txt 的字节>)` 与 `("hello.html", <HTML 字节>)`。
- `data.txt` 走的是 asset 直通分支（字节原样搬运），`hello.html` 走的是 document 分支（经 `export_html` 编码）。
- 若 `--features bundle` 在你的环境不可用，请改为「源码阅读型实践」：对照 [export.rs:24-40](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L24-L40) 与 [lib.rs:202-213](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L202-L213)，在纸上画出从 `bundle_impl` 装填 `files` 到 `export()` 产出 `VirtualFs` 的数据流。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `VirtualFs` 用 `IndexMap` 而不是 `HashMap`？

> **参考答案**：`IndexMap` 保留插入顺序。bundle 里文件的顺序是确定的（由 `bundle_impl` 装 `files` 的循环决定），导出后用 `IndexMap` 可以让落盘顺序、日志输出顺序、HTTP 目录列表顺序都保持稳定、可预测；`HashMap` 的迭代顺序不确定，会让输出变得不可复现。

**练习 2**：`VirtualFs` 的键类型为什么是 `VirtualPath` 而不是 `PathBuf`？

> **参考答案**：bundle 的输出路径是相对于 bundle 根的**虚拟**路径，不一定对应磁盘上的真实文件（在导出落盘之前它只是内存里的映射）。用 `VirtualPath` 这个专门的类型可以在类型层面区分「输出路径」与「真实文件系统路径」，并且 `VirtualPath` 提供了 bundle 需要的路径操作（如求相对路径 `relative_from`，见 `ResolvedLink::into_relative_uri`）。`PathBuf` 则绑定到具体操作系统路径语义，不适合作为虚拟文件系统的键。

**练习 3**：一个 asset 文件在 `VirtualFs` 里的字节，与它在 `bundle_impl` 阶段被装进 `BundleFile::Asset` 时的字节，是什么关系？

> **参考答案**：完全相同。`export()` 对 asset 只做 `bytes.clone()`。由于 `Bytes` 是引用计数的不可变缓冲，`clone` 几乎零成本（只增加一个引用计数，不复制底层字节）。所以 asset 字节从编译阶段产生后就不再变化，导出只是把它「搬」进 `VirtualFs`。

---

## 5. 综合实践

把本讲的三个最小模块串起来，完成下面这个**源码追踪 + 思维实验**任务。

**任务背景**：假设有这样一个 bundle，编译后 `bundle.files`（`IndexMap`）按插入顺序包含以下四项：

| path | BundleFile |
| --- | --- |
| `index.html` | `Document(Html(...))` |
| `paper.pdf` | `Document(Paged(..., PagedExtras { format: Pdf, anchors }))` |
| `logo.svg` | `Document(Paged(..., PagedExtras { format: Svg, anchors }))` |
| `meta.json` | `Asset(<json 字节>)` |

**要求**：

1. **追踪每个文件在 `export()` 里的路径**。对上表每一行，写出：
   - 它命中 `export.rs` 里 `match file` 的哪个分支（Document 还是 Asset）。
   - 若是 Document，它构造的 `LateLinkResolver` 的 `base` 是什么？随后调用 `export_document` 时会落到 [export.rs:61-74](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L61-L74) 的哪个 `match` 臂？（提示：`paper.pdf` → `PagedFormat::Pdf` → `export_pdf`；`logo.svg` → `PagedFormat::Svg` → `export_svg`；`index.html` → `BundleDocument::Html` → `export_html`。）
   - 若是 Asset，它走了哪行代码？是否构造了 `LateLinkResolver`？
2. **画出最终 `VirtualFs` 的内容**：写出导出成功后 `VirtualFs` 这个 `IndexMap` 里的四条「路径 → 字节来源」映射，并标注每条字节的来源（asset 直通 / `export_pdf` / `export_svg` / `export_html`）。
3. **解释 `base` 的作用**：`index.html` 里有一条 `#link(<glossary>)` 指向 `paper.pdf` 中的某个标签。请用 [link.rs:675-693](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L675-L693) 的 `match` 解释：为什么 `export_pdf` 拿到的 `link_resolver` 的 `base` 是 `Some(&"index.html")` 时能正确产出 `ResolvedLink::Cross { from: "index.html", to: "paper.pdf", anchor }`？如果把 `base` 改成 `None` 会怎样？
4. **错误合并验证**：假设 `paper.pdf` 与 `logo.svg` 两个文档导出时各自产生了一个错误。根据 [diag.rs:270-278](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/diag.rs#L270-L278) 与 [diag.rs:231-249](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/diag.rs#L231-L249)，写出 `export()` 的返回值，并说明 `meta.json` 和 `index.html` 的字节是否会出现在最终结果里。

**预期结果（自检）**：

1. 前三个（`index.html`/`paper.pdf`/`logo.svg`）走 Document 分支，各自构造 `LateLinkResolver::new(Some(&各自的path), ...)`，分别落到 `export_html`/`export_pdf`/`export_svg`；`meta.json` 走 [export.rs:35](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/export.rs#L35) 的 Asset 分支，**不**构造 `LateLinkResolver`。
2. `VirtualFs` 含四条映射，顺序与上表一致；`meta.json` 的字节来源是 asset 直通，其余三项分别来自对应的 `export_*` 函数。
3. `base = Some(&"index.html")` 时，`from = "index.html"`、`to = introspector.path(loc) = "paper.pdf"`，两者不等，命中 `(Some, Some)` 的 `else` 分支产出 `Cross`；若 `base = None`，则 `from = None`，命中 `(None, Some(_)) => return None`，链接解析失败。
4. 返回 `Err([pdf 的错误, svg 的错误])`（两个错误合并）；由于是 `Err`，整个 `VirtualFs` 不产出，`meta.json` 与 `index.html` 的字节**不会**出现在最终结果里。

## 6. 本讲小结

- `export()` 用 rayon 的 `par_iter()` **并行**遍历 `Bundle.files`，每个文件在独立线程导出，最后用 `collect_combined_result` 汇总。
- `BundleFile::Document` 分支会为该文档构造一个 `LateLinkResolver::new(Some(path), bundle.introspector)` 并调用 `export_document`；`BundleFile::Asset` 分支直接 `bytes.clone()` **直通**，不解析链接。
- 每个文档的 `link_resolver` 之所以用 `Some(path)` 而非 `None`，是因为跨文档链接需要知道「当前文档的路径」才能算出相对路径；`None` 会让所有跨文档链接命中 `(None, Some(_)) => return None` 而变成坏链。
- `collect_combined_result` 的语义是**先全部跑完、再把所有错误合并上报**，不会在第一个错误处短路。
- `BundleOptions` 把 `HtmlOptions`/`PdfOptions`/`RenderOptions`(png)/`SvgOptions` 四种格式选项聚合到一个 `#[derive(Default)]` 结构体里，`export_document` 按格式取对应字段。
- `VirtualFs = IndexMap<VirtualPath, Bytes, FxBuildHasher>` 是 bundle 的**最终输出形态**——一棵保留插入顺序的「虚拟文件系统」，是 typst-bundle 与外层（CLI 落盘 / HTTP）之间的交接格式。

## 7. 下一步学习建议

本讲只讲了 `export()` 的**主流程骨架**与输出形态，刻意没有展开四个格式分派函数的内部细节。下一讲 **u4-l2「四种格式的导出实现：pdf/png/svg/html 与 `*_in_bundle` 钩子」** 会深入：

- `export_pdf` / `export_svg` / `export_html` 如何调用兄弟 crate 暴露的 `pdf_in_bundle` / `svg_in_bundle` / `html_in_bundle` 钩子，而 `export_png` 为何走 `typst_render::render`。
- 这些导出函数为何都要加 `#[comemo::memoize]`，以及 `LateLinkResolver` 如何跨记忆化边界传递。
- `export_svg` 里那段把 `anchors` 从 `Location` 转成 `point` 的 `filter_map` 在做什么（与单页约束的关系）。
- `export_html` 为什么接收 `doc.root()`（根元素）而非整个 `HtmlDocument`（提示：与记忆化边界有关，见源码注释）。

建议在进入 u4-l2 前，先回头确认你对本讲的「`Some(path)` vs `None`」与「`collect_combined_result` 合并错误」两点已经理解透彻——它们是下一讲分析各格式导出函数签名的基础。
