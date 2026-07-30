# 多格式导出

## 1. 本讲目标

在上一讲（u2-l2）里，我们已经看清「把命令行参数变成一次真正编译」的主干：`CompileConfig` 预处理参数 → `compile_once` 编译并打印诊断。但 `compile_once` 真正干活的「编译 + 导出」两件事，被封装在一个叫 `compile_and_export` 的函数里，上一讲只点到为止。

本讲就把这后半段彻底讲透。读完本讲，你应该能够：

1. 解释 `compile_and_export` 如何根据 `OutputFormat` 选择不同的**编译目标类型**（`PagedDocument` / `HtmlDocument` / `Bundle`），并分发到不同的导出器。
2. 理解 PDF 与 HTML 这种「单文件」导出如何把 CLI 配置翻译成核心库的选项结构（`PdfOptions` / `HtmlOptions`）。
3. 掌握图片导出（PNG / SVG）的特殊机制：一页一文件、必须带「页面模板」、用 rayon 并行渲染。
4. 理解 Bundle 这种「一整个目录」的多文件导出，以及它如何复用前面四种选项。
5. 理解 watch 模式下的 `ExportCache` 如何靠「页面帧的哈希」跳过没变的页面，避免重复导出。

## 2. 前置知识

本讲默认你已经读过 u1-l3（参数模型）和 u2-l2（编译配置）。下面几个概念本讲会直接使用：

- **泛型编译 `typst::compile::<T>`**：Typst 的编译器是「编译到某个目标类型 `T`」的。`T` 可以是 `PagedDocument`（经典分页文档，喂给 PDF / 图片导出）、`HtmlDocument`（HTML 文档）或 `Bundle`（一个文件集合）。同一个源文件，目标类型不同，产出的中间结构也不同。这是理解本讲分发逻辑的关键。
- **`OutputFormat` 枚举**（来自 [src/args.rs:591-597](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L591-L597)）：`Pdf`、`Png`、`Svg`、`Html`、`Bundle` 五种。它有一个辅助方法 `is_paged()`（[src/args.rs:601-603](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L601-L603)），判断该格式是否产出分页文档。
- **`Page` 与 `PagedDocument`**：`PagedDocument` 是排版后的分页文档，内部是一组 `Page`；每个 `Page` 对应一页的「帧」（frame），图片导出就是逐页把这些帧渲染成位图或矢量图。
- **`Output` 枚举与 `Output::write`**（[src/args.rs:532-546](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L532-L546)）：输出要么是 `Stdout`（用 `-` 表示），要么是 `Path`。`write` 方法把字节写入对应目的地。本讲所有导出器最终都调用它把产物落盘。
- **rayon**：Rust 的数据并行库。`par_iter()` 把对集合每个元素的操作丢到线程池里并行跑。图片导出用它来并行渲染多页。
- **PPI 与点（point）**：印刷里 1 英寸 = 72 点（pt）。PNG 的 `--ppi`（每英寸像素数）要换算成「每点像素数」喂给渲染器，换算关系是 \( \text{pixel-per-pt} = \text{PPI} / 72 \)。
- **`Warned<T>`**：一个包装类型，把「编译警告」和「编译结果」打包在一起返回，保证警告不会因为出错而被丢弃。

## 3. 本讲源码地图

本讲几乎全部内容都集中在 **`src/compile.rs`** 一个文件里（参数类型在 `src/args.rs`）。下面是本讲涉及的函数与它们的职责：

| 函数 / 类型 | 行号 | 作用 |
| --- | --- | --- |
| `compile_and_export` | [L317-341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L317-L341) | 按 `OutputFormat` 选择编译目标类型并分发到导出器（本讲总入口） |
| `export_paged` | [L360-376](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L360-L376) | 分页目标的二次分发：PDF 走 `export_pdf`，图片走 `export_image` |
| `export_pdf` | [L379-388](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L379-L388) | 用 `pdf_options` 把分页文档导出成单个 PDF |
| `pdf_options` | [L600-625](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L600-L625) | 构造 `typst_pdf::PdfOptions`（时间戳、标准、标签、页面范围等） |
| `export_html` | [L344-357](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L344-L357) | 把 HTML 文档写成单个文件，并喂给 watch 的 HTTP 服务器 |
| `export_image` | [L462-534](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L462-L534) | 图片导出主逻辑：模板检查、页面过滤、并行渲染、缓存 |
| `mod output_template` | [L536-563](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L536-L563) | 页面文件名模板的解析与格式化（`{p}` / `{0p}` / `{n}` / `{t}`） |
| `export_image_page` | [L566-592](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L566-L592) | 渲染并写出单页 PNG / SVG |
| `png_options` / `svg_options` | [L628-638](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L628-L638) | 构造图片导出选项 |
| `export_bundle` | [L391-415](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L391-L415) | Bundle 多文件导出，组合四种选项 |
| `write_virtual_fs` | [L418-438](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L418-L438) | 把虚拟文件系统里的文件并行写到磁盘目录 |
| `ExportCache` | [L647-671](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L647-L671) | watch 模式下基于帧哈希的导出缓存 |

## 4. 核心概念与源码讲解

### 4.1 compile_and_export：按 OutputFormat 分发编译目标

#### 4.1.1 概念说明

Typst 的一个核心设计是：**同一个源文件，可以编译成不同形态的产物**。这件事在类型层面体现为 `typst::compile::<T>(world)` 是泛型的——`T` 是「编译目标类型」，决定了编译器内部走哪条排版/生成管线：

- `T = PagedDocument`：经典分页文档，对应 PDF、PNG、SVG 三种输出。
- `T = HtmlDocument`：HTML 文档，对应 HTML 输出。
- `T = Bundle`：一个文件集合（可以同时包含 HTML、PDF、图片等），对应 Bundle 输出。

`compile_and_export` 就是这层「命令行 `OutputFormat` → 编译目标类型 `T` → 对应导出器」的翻译层。它把上一讲 `CompileConfig` 里定好的 `output_format`，翻译成对 `typst::compile::<T>` 的某一次具体调用，再调用对应的 `export_*` 函数落盘。

#### 4.1.2 核心流程

`compile_and_export` 的流程非常对称，本质是一个 `match config.output_format`：

```text
match output_format:
  Pdf | Png | Svg  →  typst::compile::<PagedDocument>(world)
                       → export_paged(document)        # 再二次分发到 PDF / 图片
  Html             →  typst::compile::<HtmlDocument>(world)
                       → export_html(document)
  Bundle           →  typst::compile::<Bundle>(world)
                       → export_bundle(bundle)
```

注意三点：

1. **PDF / PNG / SVG 共用同一种编译目标** `PagedDocument`，因为它们都来自分页排版结果，差别只在最后导出那一步（PDF 序列化 vs 逐页渲染）。
2. 每个分支都用 `output.and_then(|doc| export_*(...))`：编译失败（`output` 是 `Err`）时直接短路，不再导出；编译成功才导出。
3. 警告始终被原样透传：返回类型是 `Warned<SourceResult<Vec<Output>>>`，即使导出失败，编译阶段产生的警告也不会丢。

#### 4.1.3 源码精读

总入口 [src/compile.rs:317-341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L317-L341)，三个分支的结构完全一致：

```rust
fn compile_and_export(world, config) -> Warned<SourceResult<Vec<Output>>> {
    match config.output_format {
        OutputFormat::Pdf | OutputFormat::Png | OutputFormat::Svg => {
            let Warned { output, warnings } = typst::compile::<PagedDocument>(world);
            let result = output.and_then(|document| export_paged(&document, config));
            Warned { output: result, warnings }
        }
        OutputFormat::Html => {
            let Warned { output, warnings } = typst::compile::<HtmlDocument>(world);
            let result = output.and_then(|document| export_html(&document, config));
            Warned { output: result.map(|()| vec![config.output.clone()]), warnings }
        }
        OutputFormat::Bundle => {
            let Warned { output, warnings } = typst::compile::<Bundle>(world);
            let result = output.and_then(|bundle| export_bundle(bundle, config));
            Warned { output: result, warnings }
        }
    }
}
```

这段代码做了什么：按 `output_format` 三选一，调用对应泛型实例化的 `typst::compile::<T>`，拿到 `Warned { output, warnings }` 后用 `and_then` 接上导出。三个分支的**返回值都是 `Vec<Output>`**（写出的文件列表）：PDF / HTML 是单个文件所以包成 `vec![config.output.clone()]`；图片 / Bundle 可能写多个文件，所以直接返回导出器产出的列表。这个文件列表在 `compile_once` 里会被 `write_deps` 用作依赖文件里的 outputs（见 u3-l4）。

> 小提示：`PagedDocument` 来自 `typst_layout`（[src/compile.rs:19](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L19)），`HtmlDocument` 来自 `typst_html`，`Bundle` 来自 `typst_bundle`——它们都是 typst 工作区里的核心 crate，CLI 只是调用方。

#### 4.1.4 代码实践

1. **实践目标**：用同一段源文件，分别编译成 PDF、SVG、HTML、Bundle，直观体会「同源 → 不同编译目标」。
2. **操作步骤**：
   - 准备一个最小文档 `doc.typ`，内容例如 `Hello, #text(fill: red)[Typst].`。
   - 依次运行（HTML/Bundle 目标需要相应 feature，本步骤先用 PDF/SVG 验证分发）：
     - `typst compile doc.typ out.pdf`
     - `typst compile doc.typ --format svg out.svg`
3. **需要观察的现象**：两条命令都成功，但 `out.pdf` 是二进制 PDF，`out.svg` 是文本 SVG（可用 `head out.svg` 看到 `<svg ...>` 标签）。
4. **预期结果**：两种产物来自同一个 `PagedDocument` 编译目标，只是导出器不同。HTML/Bundle 分支需要启用对应 feature（见 4.4 与 u4-l3），若未启用会直接报「未知格式」之类错误，属正常现象。
5. 若本地未启用 HTML feature，HTML/Bundle 部分可标注「待本地验证」，先通过 PDF/SVG 理解分发结构即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 PDF、PNG、SVG 三种格式共用一次 `typst::compile::<PagedDocument>`，而不是各自独立编译？

> **参考答案**：因为它们都来自同一套「分页排版」结果。排版是昂贵的一步，三种格式只是对同一份 `PagedDocument` 做不同的序列化/渲染，所以编译一次、导出三分支复用，避免重复排版。

**练习 2**：如果编译阶段就出错（比如源文件有语法错误），`export_html` 还会被调用吗？

> **参考答案**：不会。`output.and_then(|document| export_html(...))` 中，`output` 为 `Err` 时 `and_then` 直接把错误原样返回，闭包根本不执行。这正是 `and_then` 的短路语义。

---

### 4.2 PDF 与 HTML：单文件导出与选项构造

#### 4.2.1 概念说明

PDF 和 HTML 有一个共同点：**一个文档导出成一个文件**。它们的核心工作是「把 CLI 里那些零散的配置项，组装成核心库能理解的一个选项结构」。

- PDF 用 `typst_pdf::PdfOptions`，里面要装时间戳、PDF 标准、是否打标签、页面范围、是否美化输出等。
- HTML 用 `typst_html::HtmlOptions`，相对简单，只有「是否美化输出」。

CLI 层并不亲自做 PDF/HTML 序列化，而是当「搬运工」：构造好选项，调用核心库函数，把返回的字节用 `Output::write` 落盘。这种「CLI 只组装选项、核心库干活」的分层，和上一讲 PDF 标准链路（CLI 枚举 → 核心库枚举）是一致的思路。

#### 4.2.2 核心流程

```text
PDF:   export_pdf(document, config)
         → pdf_options(config)            # 组装 PdfOptions
         → typst_pdf::pdf(document, &options)   # 核心库序列化
         → config.output.write(&buffer)   # 落盘

HTML:  export_html(document, config)
         → html_options(config)           # 组装 HtmlOptions（只有 pretty）
         → typst_html::html(document, &options)
         → config.output.write(html.as_bytes())
         → （watch 模式）server.set_html(html)   # 顺便喂给 HTTP 服务器
```

注意 `export_paged`（[src/compile.rs:360-376](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L360-L376)）是 PDF/图片的二次分发层：PDF 走 `export_pdf`，PNG/SVG 走 `export_image`（见 4.3）。`Html | Bundle` 在这里标记为 `unreachable!()`，因为它们根本不会进入 `export_paged`——`compile_and_export` 已经把这两条路分流走了。

#### 4.2.3 源码精读

PDF 导出与选项构造：

```rust
// src/compile.rs:379-388  —— PDF 导出
fn export_pdf(document: &PagedDocument, config: &CompileConfig) -> SourceResult<()> {
    let options = pdf_options(config);
    let buffer = typst_pdf::pdf(document, &options)?;
    config.output.write(&buffer)
        .map_err(|err| eco_format!("failed to write PDF file ({err})"))
        .at(Span::detached())?;
    Ok(())
}
```

```rust
// src/compile.rs:600-625  —— 组装 PdfOptions
fn pdf_options(config: &CompileConfig) -> PdfOptions {
    let timestamp = match config.creation_timestamp {
        Some(timestamp) => convert_datetime(timestamp).map(Timestamp::new_utc),
        None => { /* 用本地时间 + 本地时区偏移 */ }
    };
    PdfOptions {
        ident: Smart::Auto,
        creator: Smart::Auto,
        timestamp,
        page_ranges: config.pages.clone(),
        standards: config.pdf_standards.clone(),
        tagged: config.tagged,
        pretty: config.pretty,
    }
}
```

这段代码做了什么：`export_pdf` 是典型的「构造选项 → 调核心库 → 落盘」三段式。`pdf_options` 把 `CompileConfig` 里的字段搬运到 `PdfOptions`：`timestamp` 优先用命令行传入的 UTC 时间，否则用本地时间（[src/compile.rs:603-614](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L603-L614)）；`page_ranges` / `standards` / `tagged` / `pretty` 都是直接透传上一讲 `new_impl` 校验好的值。`ident` 和 `creator` 留作 `Smart::Auto`，交给核心库默认处理。

HTML 导出与选项构造：

```rust
// src/compile.rs:344-357
fn export_html(document: &HtmlDocument, config: &CompileConfig) -> SourceResult<()> {
    let options = HtmlOptions { pretty: config.pretty };
    let html = typst_html::html(document, &options)?;
    let result = config.output.write(html.as_bytes());

    #[cfg(feature = "http-server")]
    if let Some(server) = &config.server {
        server.set_html(html);
    }
    result.map_err(|err| eco_format!("failed to write HTML file ({err})"))
        .at(Span::detached())
}
```

```rust
// src/compile.rs:595-597
fn html_options(config: &CompileConfig) -> HtmlOptions {
    HtmlOptions { pretty: config.pretty }
}
```

这段代码做了什么：HTML 比 PDF 简单，选项只有 `pretty`。注意那一段 `#[cfg(feature = "http-server")]`：在 watch 模式编译 HTML 时，导出的 HTML 字符串会被**同时**喂给内置 HTTP 服务器（`server.set_html`），这样浏览器就能实时预览（详见 u4-l3）。落盘和服务是同一次导出的两个副作用。

#### 4.2.4 代码实践

1. **实践目标**：观察 `--pretty` 与 `--creation-timestamp` 如何影响 PDF 导出，验证 CLI 参数确实经过 `pdf_options` 进入了产物。
2. **操作步骤**：
   - `typst compile doc.typ out.pdf`（默认）。
   - 对照运行 `typst compile --creation-timestamp 0 doc.typ out-ts.pdf`（把创建时间钉死在 UNIX 纪元 1970-01-01）。
3. **需要观察的现象**：两次产出的 PDF 用 `ls -l` 看大小可能不同；用 `strings out-ts.pdf | grep -i date`（或 PDF 查看器属性）应能看到固定的早期时间。
4. **预期结果**：`--creation-timestamp 0` 会让 `pdf_options` 走 `Timestamp::new_utc` 分支，产物里的创建时间被固定。若手头没有 PDF 属性查看工具，这部分可标注「待本地验证」。
5. `--pretty` 主要影响 HTML/SVG 的可读性（见 args.rs 注释 [src/args.rs:322-328](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L322-L328)），对 PDF 不影响 PNG。

#### 4.2.5 小练习与答案

**练习 1**：`export_paged` 里为什么 `Html | Bundle` 分支是 `unreachable!()`？

> **参考答案**：因为 `compile_and_export` 已经按 `output_format` 把 `Html` 分流到 `export_html`、`Bundle` 分流到 `export_bundle`，根本不会调用 `export_paged`。所以一旦执行到 `export_paged`，`output_format` 只可能是 `Pdf/Png/Svg`，其余两种在逻辑上不可达。

**练习 2**：`pdf_options` 里 `ident` 和 `creator` 都是 `Smart::Auto`，这意味着什么？

> **参考答案**：`Smart::Auto` 表示「让核心库自己决定默认值」，CLI 不强制覆盖。`Smart` 是 Typst 里常见的「自动 / 自定义」二选一类型（`Auto` 或 `Custom(T)`）。

---

### 4.3 图片导出：页面模板与多页并行渲染

#### 4.3.1 概念说明

图片导出（PNG / SVG）和 PDF 有一个本质区别：**PDF 是一个文件装多页，而图片是一页一个文件**。这就带来两个新问题：

1. **多页时怎么给文件命名？** 如果文档有 10 页，输出路径只有一个 `out.png`，显然不够用。所以 CLI 要求：多页图片导出时，输出路径里必须带「页面模板」占位符（如 `{p}`、`{0p}`），用来区分每一页。
2. **多页怎么高效渲染？** 每页是独立的帧，互相不依赖，天然适合并行。Typst 用 rayon 把多页渲染丢到线程池里同时跑。

`output_template` 子模块就是专门负责「把带占位符的模板，按当前页号格式化成真实文件名」的。`export_image` 则负责调度：检查模板、过滤要导出的页、并行渲染、必要时查缓存跳过。

#### 4.3.2 核心流程

```text
export_image(document, config, fmt):
  1. 判断 can_handle_multiple：
       stdout → false
       Path(p) → output_template::has_indexable_template(p)   # 路径里是否含 {p}/{0p}/{n}
  2. 按 config.pages 过滤出 exported_pages（未指定 --pages 就是全部页）
  3. 若 !can_handle_multiple 且 exported_pages.len() > 1 → bail（多页必须有模板）
  4. exported_pages.par_iter().map(|(i, page)|):
       - 若 can_handle_multiple：用 output_template::format(...) 把模板填上页号得到真实路径
       - 若 watch 且缓存命中且文件存在 → 直接复用，跳过渲染
       - 否则 export_image_page(...) 渲染单页 PNG/SVG 落盘
```

页面模板有四个占位符（见 [src/compile.rs:537](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L537) 与 args.rs 文档 [src/args.rs:302-306](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L302-L306)）：

| 占位符 | 含义 | 举例（共 10 页，当前第 2 页） |
| --- | --- | --- |
| `{p}` | 当前页号，不补零 | `2` |
| `{0p}` | 当前页号，按总页数位数补零 | `02` |
| `{n}` | `{0p}` 的别名，同样补零 | `02` |
| `{t}` | 总页数 | `10` |

补零位数由一个内部函数 `width(i)` 决定（[src/compile.rs:545-547](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L545-L547)）：\( \text{width}(i) = 1 + \lfloor \log_{10} i \rfloor \)，即总页数的十进制位数。所以 10 页时位数是 2（`02`），3 页时位数是 1（就是 `2`，看不出补零）。

#### 4.3.3 源码精读

模板检查与多页保护 [src/compile.rs:467-494](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L467-L494)：

```rust
let can_handle_multiple = match config.output {
    Output::Stdout => false,
    Output::Path(ref output) => {
        output_template::has_indexable_template(output.to_str().unwrap_or_default())
    }
};
// ... 过滤 exported_pages ...
if !can_handle_multiple && exported_pages.len() > 1 {
    let err = match config.output {
        Output::Stdout => "to stdout",
        Output::Path(_) => "without a page number template ({p}, {0p}) in the output path",
    };
    bail!("cannot export multiple images {err}");
}
```

这段代码做了什么：先判断「这个输出能不能承载多个文件」——stdout 当然不行；路径则要看里面有没有 `{p}` / `{0p}` / `{n}` 这类可索引占位符（`has_indexable_template`，[src/compile.rs:539-541](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L539-L541)）。如果不行又要导出多页，就直接报错——这正是「无模板时多页导出为何会报错」的根源。

并行渲染与模板填充 [src/compile.rs:497-533](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L497-L533)：

```rust
exported_pages
    .par_iter()                                    // ← rayon 并行
    .map(|(i, page)| {
        let output = match &config.output {
            Output::Path(path) => {
                let storage;
                let path = if can_handle_multiple {
                    storage = output_template::format(
                        path.to_str().unwrap_or_default(),
                        i + 1,                         // ← 页号从 1 开始
                        document.pages().len(),
                    );
                    Path::new(&storage)
                } else { path };
                // watch 模式下：缓存命中且文件存在则跳过渲染
                if config.watching
                    && config.export_cache.is_cached(*i, page)
                    && path.exists()
                {
                    return Ok(Output::Path(path.to_path_buf()));
                }
                Output::Path(path.to_owned())
            }
            Output::Stdout => Output::Stdout,
        };
        export_image_page(config, page, &output, fmt)?;
        Ok(output)
    })
    .collect::<StrResult<Vec<Output>>>()
```

这段代码做了什么：用 `par_iter()` 并行处理每一页。页号传给 `output_template::format` 时是 `i + 1`（因为 `i` 是 0 起的下标，而用户看到的页号从 1 开始）。watch 模式下先查 `ExportCache`（见 4.4）：若该页帧没变且磁盘文件还在，就直接复用、跳过昂贵的渲染。最后调用 `export_image_page` 真正渲染。

模板格式化 [src/compile.rs:543-562](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L543-L562)：

```rust
pub fn format(output: &str, this_page: usize, total_pages: usize) -> String {
    fn width(i: usize) -> usize { 1 + i.checked_ilog10().unwrap_or(0) as usize }
    let other_templates = ["{t}"];
    INDEXABLE.iter().chain(other_templates.iter()).fold(
        output.to_string(),
        |out, template| {
            let replacement = match *template {
                "{p}" => format!("{this_page}"),
                "{0p}" | "{n}" => format!("{:01$}", this_page, width(total_pages)),
                "{t}" => format!("{total_pages}"),
                _ => unreachable!(...),
            };
            out.replace(template, replacement.as_str())
        },
    )
}
```

这段代码做了什么：用 `fold` 依次把四个占位符替换掉。`{0p}` / `{n}` 用 `format!("{:01$}", this_page, width(total_pages))` 实现「按总页数位数补零」——第二个参数 `1$` 指定最小宽度为 `width(total_pages)`。

单页渲染与选项构造 [src/compile.rs:566-592](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L566-L592) 与 [src/compile.rs:628-638](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L628-L638)：

```rust
fn export_image_page(config, page, output, fmt) -> StrResult<()> {
    match fmt {
        ImageExportFormat::Png => {
            let options = png_options(config);
            let pixmap = typst_render::render(page, &options);
            let buf = pixmap.encode_png()...?;
            output.write(&buf)...?;
        }
        ImageExportFormat::Svg => {
            let options = svg_options(config);
            let svg = typst_svg::svg(page, &options);
            output.write(svg.as_bytes())...?;
        }
    }
    Ok(())
}

fn svg_options(config) -> SvgOptions { SvgOptions { render_bleed: false, pretty: config.pretty } }
fn png_options(config) -> RenderOptions {
    RenderOptions { pixel_per_pt: Scalar::new(config.ppi / 72.0), render_bleed: false }
}
```

这段代码做了什么：PNG 走 `typst_render::render`（位图），SVG 走 `typst_svg::svg`（矢量）。`png_options` 把命令行的 `--ppi`（每英寸像素）换算成「每点像素」：\( \text{pixel\_per\_pt} = \text{PPI} / 72 \)（默认 `--ppi 144.0`，见 [src/args.rs:355-357](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L355-L357)），所以默认每点 2 像素。

#### 4.3.4 代码实践

这是本讲的主实践。

1. **实践目标**：用一个 3 页文档验证 `output_template::format` 的占位符替换，并亲历「无模板时多页导出报错」。
2. **操作步骤**：
   - 写一个 3 页文档 `pages.typ`：
     ```typst
     #set page(width: 10cm, height: 10cm)
     第一页 #pagebreak()
     第二页 #pagebreak()
     第三页
     ```
   - 带模板导出：`typst compile pages.typ "page-{0p}-of-{t}.png"`
   - 不带模板再试：`typst compile pages.typ out.png`
3. **需要观察的现象**：
   - 第一步会在当前目录生成 3 个文件。
   - 第二步会直接报错退出。
4. **预期结果**（依据 [src/compile.rs:543-562](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L543-L562) 的 `format` 逻辑推导，待本地验证）：
   - 因为总页数是 3，`width(3) = 1`，补零位数是 1，所以 `{0p}` 不会显示前导零。生成的文件名应为 **`page-1-of-3.png`、`page-2-of-3.png`、`page-3-of-3.png`**，`{t}` 替换为 `3`。
   - 想直观看到补零效果（如 `02`），需要总页数 ≥ 10：可把文档改成 10 页（`#for i in range(10) { [#i] #pagebreak() }`），再用同样模板，应得到 `page-01-of-10.png … page-10-of-10.png`。
   - 第二步（无模板、多页）应报错：`cannot export multiple images without a page number template ({p}, {0p}) in the output path`（对应 [src/compile.rs:489-493](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L489-L493)）。
5. 若只想导出单页可不需要模板：`typst compile pages.typ --pages 1 out.png` 能成功（过滤后只剩 1 页，不触发多页保护）。

#### 4.3.5 小练习与答案

**练习 1**：为什么「3 页」时 `{0p}` 看起来没有补零，而「10 页」时会变成 `01`、`02`？

> **参考答案**：补零位数 `width(total_pages) = 1 + ⌊log₁₀(total_pages)⌋`。3 页时位数是 1，页号本身已是 1 位，无需补；10 页时位数是 2，于是 1 位数页号前面补一个 0 变成 `01`。

**练习 2**：把图片导出到 stdout（`typst compile pages.typ --format png -`）会发生什么？为什么？

> **参考答案**：若文档只有 1 页，会把 PNG 字节流写到 stdout；若多于 1 页则报错 `cannot export multiple images to stdout`。因为 `can_handle_multiple` 对 stdout 恒为 `false`（[src/compile.rs:469](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L469)），多页时无法区分。

---

### 4.4 Bundle 导出、VirtualFs 写盘与 ExportCache 增量缓存

本模块把三件事放在一起讲：它们都是「超出单个分页文件」的导出机制。

#### 4.4.1 概念说明

- **Bundle**：一种「把一整个文档连同它的各种衍生格式一起打包成一个目录」的导出方式。一个 Bundle 里可能同时包含 HTML、PDF、PNG、SVG 等多个文件。核心库 `typst_bundle` 把它导出成一个 **`VirtualFs`**（虚拟文件系统，一棵「路径 → 字节」的映射），CLI 再把这棵树写到真实磁盘目录。
- **ExportCache**：只在 `typst watch` 图片导出时生效的小缓存。它的目的是——文档只改了一页，没必要把每一页都重新渲染一遍。它靠「每页帧（frame）的 128 位哈希」判断某页有没有变。

#### 4.4.2 核心流程

Bundle 导出：

```text
export_bundle(bundle, config):
  1. 组装 BundleOptions { html, pdf, png, svg }   # 复用前四个 *_options 构造函数
  2. typst_bundle::export(&bundle, &options) → VirtualFs
  3. 输出必须是目录路径（不能是 stdout）→ 取 root
  4. write_virtual_fs(root, &fs)  → 并行把每个虚拟文件写到磁盘
  5. （watch 模式）server.set_bundle(bundle, fs)
```

ExportCache 判定（每次导出某页时调用 `is_cached(i, page)`）：

```text
is_cached(i, page):
  hash = hash128(page)
  若 i >= cache.len():   cache.push(hash); return false   # 新页，肯定没缓存过
  否则:                  return replace(cache[i], hash) == hash   # 旧哈希==新哈希？
```

> 关键设计（见 [src/compile.rs:642-646](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L642-L646) 注释）：缓存是一个按页号索引的哈希数组。若文档中间**插入**了一页，后续页号错位，会导致从插入点往后的页面对比错位——这会「让后面的缓存失效」（多半被迫重渲染），是有意为之的简化，换来更低的复杂度和内存占用。

#### 4.4.3 源码精读

Bundle 导出与选项组合 [src/compile.rs:391-415](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L391-L415)：

```rust
fn export_bundle(bundle: Bundle, config: &CompileConfig) -> SourceResult<Vec<Output>> {
    let options = BundleOptions {
        html: html_options(config),
        pdf: pdf_options(config),
        png: png_options(config),
        svg: svg_options(config),
    };
    let fs = typst_bundle::export(&bundle, &options)?;
    let root = match &config.output {
        Output::Path(path) => path,
        Output::Stdout => bail!(Span::detached(), "cannot write bundle to standard output"),
    };
    let outputs = write_virtual_fs(root, &fs).at(Span::detached())?;
    #[cfg(feature = "http-server")]
    if let Some(server) = &config.server { server.set_bundle(bundle, fs); }
    Ok(outputs)
}
```

这段代码做了什么：Bundle 是「集大成者」——它一次性把 `html_options` / `pdf_options` / `png_options` / `svg_options` 四个构造函数全用上（这就是本讲「选项构造」在 Bundle 这里的汇总点）。核心库返回 `VirtualFs` 后，输出必须是目录（stdout 不行），交给 `write_virtual_fs` 落盘。

虚拟文件系统并行写盘 [src/compile.rs:418-438](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L418-L438)：

```rust
fn write_virtual_fs(root: &Path, fs: &VirtualFs) -> StrResult<Vec<Output>> {
    std::fs::create_dir_all(root)...?;
    fs.par_iter().map(|(path, data)| {
        let realized = path.realize(root)...?;          // 虚拟路径 → 真实磁盘路径
        if let Some(parent) = realized.parent() {
            std::fs::create_dir_all(parent)...?;         // 确保子目录存在
        }
        std::fs::write(&realized, data)...?;
        Ok(Output::Path(realized))
    }).collect()
}
```

这段代码做了什么：先建好根目录，再用 `par_iter()` 并行写每个虚拟文件。`path.realize(root)` 把虚拟路径拼到真实根目录下；每个文件写入前还会确保它的父目录存在（Bundle 可能是多级目录结构）。返回所有写出的真实路径。

ExportCache 结构与判定 [src/compile.rs:647-671](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L647-L671)：

```rust
pub struct ExportCache {
    pub cache: RwLock<Vec<u128>>,     // 按页号索引的帧哈希数组
}
impl ExportCache {
    pub fn new() -> Self { Self { cache: RwLock::new(Vec::with_capacity(32)) } }

    pub fn is_cached(&self, i: usize, page: &Page) -> bool {
        let hash = typst::utils::hash128(page);
        let mut cache = self.cache.upgradable_read();
        if i >= cache.len() {
            cache.with_upgraded(|cache| cache.push(hash));
            return false;                                  // 新页：没缓存过
        }
        cache.with_upgraded(|cache| std::mem::replace(&mut cache[i], hash) == hash)
        // 旧位置：比较并替换。相等=没变(命中)，不等=变了(未命中)
    }
}
```

这段代码做了什么：缓存就是一个 `Vec<u128>`，第 `i` 位存第 `i` 页的帧哈希。`is_cached` 用 `parking_lot` 的「可升级读锁」：先以读锁判断 `i` 是否越界，越界就升级为写锁 `push` 新哈希并返回「未命中」；否则升级为写锁，用 `mem::replace` 把新哈希写进去、同时取回旧哈希比较——相等说明这页没变（命中，调用方会跳过渲染）。注意它本身只判哈希；真正的「跳过」发生在 `export_image` 里 `config.watching && is_cached(...) && path.exists()` 三重条件（[src/compile.rs:518-523](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L518-L523)）：非 watch 模式或磁盘文件已不存在时，缓存都不生效。

#### 4.4.4 代码实践

1. **实践目标**：体验 Bundle「一键导出整个目录」，并通过源码阅读理解 ExportCache 的命中逻辑。
2. **操作步骤**：
   - Bundle 需要对应 feature（详见 u4-l3）。若已启用，可运行 `typst compile doc.typ --format bundle out_bundle/`，然后用 `ls -R out_bundle/` 查看生成的目录树（待本地验证，取决于 feature 是否开启）。
   - 源码阅读型实践（无需运行）：对照 [src/compile.rs:660-670](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L660-L670)，手动模拟 watch 模式下两次编译同一份 3 页文档：第一次 `cache.len()` 从 0 涨到 3，三页都返回 `false`（全渲染）；假设第二次只有第 2 页改了，则 `is_cached(0,...)` 命中跳过、`is_cached(1,...)` 未命中重渲染、`is_cached(2,...)` 命中跳过。
3. **需要观察的现象 / 推导结果**：第一次全量渲染，第二次只重渲染变化页。
4. **预期结果**：Bundle 目录下应出现多种格式文件；ExportCache 的行为如上推导（命中条件还需 `config.watching && path.exists()`）。
5. ExportCache 的实际效果只能在 `typst watch` + 图片输出时观察（见 u2-l5），本练习以源码推导为主。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ExportCache` 在「非 watch」的普通 `typst compile` 里不起作用？

> **参考答案**：`export_image` 里跳过渲染的三重条件第一个就是 `config.watching`（[src/compile.rs:518](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L518)）。普通编译 `config.watching` 为 `false`，短路求值，`is_cached` 根本不会被调用，每次都全量渲染。

**练习 2**：Bundle 为什么不能输出到 stdout？

> **参考答案**：Bundle 是一个目录、多个文件，stdout 只是一个字节流、无法表达目录结构。所以 `export_bundle` 在输出为 `Stdout` 时直接 `bail!("cannot write bundle to standard output")`（[src/compile.rs:402-404](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L402-L404)）。

**练习 3**：`is_cached` 里 `mem::replace(&mut cache[i], hash) == hash` 这一行为什么能同时完成「更新缓存」和「判断是否变化」两件事？

> **参考答案**：`mem::replace` 把 `cache[i]` 替换成新 `hash` 的同时，返回**旧值**。拿旧值和新 `hash` 比较：相等说明这页帧没变（命中）；不等说明变了（未命中）。一次操作兼顾了「写入本轮哈希供下次比对」和「判断本轮是否变化」。

---

## 5. 综合实践

把本讲的分发、选项构造、模板、并行、缓存串起来：

**任务**：准备一个 3 页文档（`pages.typ`，内容同 4.3.4），完成下列三组对照实验，并用一张表记录「编译目标类型 / 写出文件数 / 用到的选项构造函数」。

1. **PDF（单文件分页）**：`typst compile pages.typ out.pdf`
   - 预期：1 个 PDF 文件，走 `PagedDocument` → `export_paged` → `export_pdf` → `pdf_options`。
2. **PNG（多文件图片）**：`typst compile pages.typ "page-{0p}-of-{t}.png"`
   - 预期：3 个文件（`page-1-of-3.png` 等），走 `PagedDocument` → `export_image` → `output_template::format` + `png_options`，多页并行渲染。
3. **PNG 单页（无模板）**：`typst compile pages.typ --pages 1 only.png`
   - 预期：1 个文件 `only.png`，验证「过滤后只剩一页就不需要模板」。

完成后，对照 `compile_and_export`（[src/compile.rs:317-341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L317-L341)）确认：三组实验前两组都走同一个 `typst::compile::<PagedDocument>(world)`，差别只在 `output_format` 决定的导出分支——这正是「同源编译、按格式分发导出」的精髓。

进阶（可选）：用 `typst watch pages.typ "page-{p}.png"` 启动监听，修改其中一页内容，观察终端只提示重新渲染变化页（`ExportCache` 生效，详见 u2-l5）。

## 6. 本讲小结

- `compile_and_export` 是导出总入口：按 `OutputFormat` 把编译分发到三种目标类型——`Pdf/Png/Svg` 共用 `typst::compile::<PagedDocument>`，`Html` 用 `HtmlDocument`，`Bundle` 用 `Bundle`。
- PDF 与 HTML 是「单文件」导出，核心是「用 `pdf_options` / `html_options` 把 CLI 配置组装成核心库选项，再落盘」；HTML 在 watch 模式还会顺手喂给 HTTP 服务器。
- 图片导出是「一页一文件」：多页时输出路径必须带 `{p}` / `{0p}` / `{n}` 占位符（`{t}` 表总页数），否则报「cannot export multiple images」；多页用 rayon `par_iter` 并行渲染。
- 页面文件名由 `output_template::format` 生成，页号从 1 开始，`{0p}` 按总页数位数补零（位数 \( 1 + \lfloor \log_{10} \text{total} \rfloor \)）。
- `png_options` 把 `--ppi` 换算成每点像素（\( \text{PPI}/72 \)），默认 144 PPI = 每点 2 像素。
- Bundle 是「目录级」导出，一次性组合四种选项，经 `VirtualFs` 并行写到磁盘；`ExportCache` 只在 watch 图片导出时按帧哈希跳过未变页面。

## 7. 下一步学习建议

- **u2-l4（诊断与终端输出）**：本讲的导出器大量使用 `bail!` / `.at(Span::detached())` 报错，这些错误最终如何被格式化打印到终端，是下一讲的主题。
- **u2-l5（Watch 模式与增量重编译）**：本讲提到的 `ExportCache`、`config.watching`、`Status::Compiling/Success` 都服务于 watch 主循环，下一讲会把整条增量重编译链路串起来。
- **u4-l3（HTML / Bundle 导出与 http-server）**：若你想深入了解 HTML/Bundle 目标、`http-server` feature 下的内置 HTTP 服务器与 live reload，可在专家层继续。
- 继续阅读建议：精读 `src/compile.rs` 的 `compile_once`（上一讲已讲），把它与本讲的 `compile_and_export`、`print_diagnostics`、`write_deps` 拼成一张完整的「单次编译」全景图。
