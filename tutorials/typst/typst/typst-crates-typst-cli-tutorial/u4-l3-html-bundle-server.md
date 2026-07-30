# HTML / Bundle 导出与 http-server

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `typst compile -f html` / `-f bundle` 这两条与 PDF/PNG/SVG 不同的导出分支在 `compile.rs` 里走的是哪段代码。
- 解释 **Bundle** 这个「多文件目标」的产物结构（`Bundle` → `VirtualFs` → 磁盘目录），以及 `write_virtual_fs` 如何用 rayon 并行落盘。
- 理解 `BundleOptions` 为什么一次性组合 html/pdf/png/svg 四套选项。
- 讲清 `ServerArgs`（`--no-serve` / `--no-reload` / `--port`）如何控制 `typst watch` 内置的 HTTP 服务器，以及 live reload（实时刷新）背后的 Server-Sent Events 机制。
- 明白为什么 `typst compile` 到 HTML 只写文件、而 `typst watch` 到 HTML 才会起服务器，以及 `--open` 在两种情况下分别打开什么。

本讲承接 u2-l3（多格式导出），把 `compile_and_export` 里之前一笔带过的 `OutputFormat::Html` 与 `OutputFormat::Bundle` 两条分支彻底展开。

## 2. 前置知识

在进入本讲前，你需要已经掌握（这些都在前置讲义中讲过）：

- **目标类型（target type）**：`typst::compile::<T>` 是泛型函数，`T` 决定编译产物的形状。`PagedDocument` 对应 PDF/PNG/SVG，`HtmlDocument` 对应 HTML，`Bundle` 对应多文件包。（见 u2-l3）
- **`compile_and_export` 的分发骨架**：它按 `OutputFormat` 把编译分发到三种目标类型，编译失败用 `and_then` 短路，警告以 `Warned<T>` 透传。（见 u2-l3）
- **`OutputFormat` 枚举**：`Pdf`/`Png`/`Svg`/`Html`/`Bundle`，其中 `is_paged()` 只对前三个返回真。（见 u1-l3）
- **watch 主循环与软失败**：`typst watch` 把 `compile_once` 装进永不退出的五步循环；`compile_once` 即使编译失败也返回 `Ok(())`，靠 `set_failed()` 改退出码。（见 u2-l5）
- **feature 开关**：`Cargo.toml` 的 `default = ["embedded-fonts", "http-server"]` 表示默认开启内置 HTTP 服务器；`--features html` 这种命令行开关走 `ProcessArgs`，最终注入到编译器的 `Library`。

下面三个术语本讲会反复用到，先建立直觉：

| 术语 | 通俗解释 |
|------|----------|
| **Bundle（包/捆）** | 一次编译能产出「多个文件」的目标。普通 PDF 是单文件，而 Bundle 可以同时产出若干 HTML、PDF、图片和原始资源（asset）文件，像把一个网站打进一个目录。 |
| **VirtualFs（虚拟文件系统）** | 导出 Bundle 时的中间产物：一个「路径 → 字节」的映射表。它先在内存里生成，再统一写盘。 |
| **Server-Sent Events（SSE）** | 一种 HTTP 长连接协议，服务器可以单向、持续地往客户端推送事件。Typst 的 live reload 用它告诉浏览器「文档重编译完了，刷新吧」。 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `crates/typst-cli/src/compile.rs` | 本讲主战场。`compile_and_export` 的 HTML/Bundle 分支、`export_html`、`export_bundle`、`write_virtual_fs`、HTTP 服务器的构造与 `open_output` 跳转都在这里。 |
| `crates/typst-cli/src/args.rs` | `ServerArgs`（`--no-serve`/`--no-reload`/`--port`）的定义，以及 `WatchCommand` 如何挂上 `server` 参数组。 |
| `crates/typst-cli/src/watch.rs` | watch 主循环；`Status::print` 里打印「serving at http://...」地址的代码也在这里。 |
| `crates/typst-kit/src/server.rs` | 内置 HTTP 服务器的真正实现（`HttpServer`）。端口选择、请求路由、live reload 注入与 SSE 推送都在这个文件里。 |
| `crates/typst-bundle/src/lib.rs` | `Bundle`、`BundleFile`、`BundleDocument` 的数据结构定义。 |
| `crates/typst-bundle/src/export.rs` | `VirtualFs` 类型别名、`BundleOptions` 结构、`export` 函数（把 `Bundle` 渲染成 `VirtualFs`）。 |

> 说明：本讲的「关键源码」是 `compile.rs` 与 `args.rs`（CLI 侧），但要把 HTTP 服务器和 Bundle 产物讲透，必须延伸到 `typst-kit/src/server.rs` 与 `typst-bundle/src/`。CLI 在这两处都只是「薄壳调用」。

## 4. 核心概念与源码讲解

### 4.1 HTML 导出：从 HtmlDocument 到落盘与服务

#### 4.1.1 概念说明

PDF/PNG/SVG 三种格式共享同一个目标类型 `PagedDocument`（「分页文档」），因为它们本质上都是「把文档切成一页一页」再渲染。HTML 不同——它不是分页的，而是一个连续的网页。所以 Typst 为它准备了独立的目标类型 `HtmlDocument`。

`OutputFormat::Html` 这条分支要做三件事：

1. 用 `typst::compile::<HtmlDocument>(world)` 把源码编译成一个 HTML 文档对象。
2. 用 `typst_html::html(document, &options)` 把文档对象序列化成 HTML 字符串，写进输出文件。
3. 如果当前是 watch 模式且起了服务器，就把这串 HTML 也喂给服务器（`server.set_html`），让浏览器能立刻刷新。

第 3 步是 HTML 导出区别于 PDF 导出的关键：PDF 永远只是「写文件」，而 HTML 在 watch 下还能「实时推送」。

#### 4.1.2 核心流程

```text
OutputFormat::Html
   │
   ▼
typst::compile::<HtmlDocument>(world)        ── 编译，得到 HtmlDocument（或错误）
   │  (and_then：编译失败则不导出)
   ▼
export_html(document, config)
   ├── HtmlOptions { pretty }                 ── 组装选项（仅一个 pretty 开关）
   ├── typst_html::html(document, &options)?  ── 序列化成 HTML 字符串
   ├── config.output.write(html.as_bytes())   ── 写进文件 / stdout
   └── if let Some(server) = config.server {  ── watch 模式才有的额外动作
           server.set_html(html)              ── 喂给 HTTP 服务器，触发浏览器刷新
       }
```

注意：`HtmlDocument` 编译成 HTML 字符串这一步本身可能失败（`typst_html::html` 返回 `SourceResult`），所以 `export_html` 用了 `?`。

#### 4.1.3 源码精读

先看 `compile_and_export` 里 HTML 与 Bundle 两条分支的位置，它们和 PDF/PNG/SVG 平级：

`compile_and_export` 按 `output_format` 四路分发（[compile.rs:317-341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L317-L341)）。

HTML 分支编译成 `HtmlDocument`，再调 `export_html`：

```rust
OutputFormat::Html => {
    let Warned { output, warnings } = typst::compile::<HtmlDocument>(world);
    let result = output.and_then(|document| export_html(&document, config));
    Warned {
        output: result.map(|()| vec![config.output.clone()]),
        warnings,
    }
}
```

> 这里 `result.map(|()| vec![...])` 把 `export_html` 返回的 `()` 包成「输出路径列表」，是为了和图片导出返回 `Vec<Output>` 的形状对齐（图片一页一文件，可能有多个输出）。

`export_html` 本体很薄（[compile.rs:344-357](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L344-L357)）：

```rust
fn export_html(document: &HtmlDocument, config: &CompileConfig) -> SourceResult<()> {
    let options = HtmlOptions { pretty: config.pretty };
    let html = typst_html::html(document, &options)?;
    let result = config.output.write(html.as_bytes());

    #[cfg(feature = "http-server")]
    if let Some(server) = &config.server {
        server.set_html(html);
    }

    result
        .map_err(|err| eco_format!("failed to write HTML file ({err})"))
        .at(Span::detached())
}
```

读这段代码要注意两个设计：

- **写盘与喂服务器是分离的**：`config.output.write` 把 HTML 写到磁盘（或 stdout），`server.set_html` 则把同一份 HTML 字符串交给服务器。两者互不影响——即使服务器没开（`config.server` 为 `None`），文件照常写出。注释里 u2-l3 提到「HTML 在 watch 模式下还会喂给内置 HTTP 服务器」，对应的就是这里。
- **`#[cfg(feature = "http-server")]`**：`server` 字段和相关代码只在编译 CLI 时启用了 `http-server` feature 才存在。由于该 feature 默认开启，常规构建里这段代码都会被编译进来。

`HtmlOptions` 只有一个字段，CLI 侧的 `html_options` 函数就是个透传（[compile.rs:595-597](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L595-L597)）：

```rust
fn html_options(config: &CompileConfig) -> HtmlOptions {
    HtmlOptions { pretty: config.pretty }
}
```

`pretty` 来自命令行的 `--pretty`，控制 HTML 是否带缩进、更易读（但占用更多空间）。

#### 4.1.4 代码实践

**实践目标**：亲手把一个文档编译成 HTML，并确认 `--pretty` 对输出的影响。

**操作步骤**：

1. 准备一个最小文档 `hello.typ`：

   ```typst
   #set page(width: 20cm)
   Hello, *HTML* export!
   ```

2. 用 HTML 目标编译（注意必须启用 `html` 特性）：

   ```bash
   ./target/debug/typst compile --features html hello.typ hello.html
   ```

3. 再用 `--pretty` 编译一份对比：

   ```bash
   ./target/debug/typst compile --features html --pretty hello.typ hello-pretty.html
   ```

4. 用文本编辑器或 `head` 分别查看两个 HTML 文件。

**需要观察的现象**：

- 不加 `--pretty` 时，HTML 通常被压成更紧凑的形式；加 `--pretty` 后带有缩进和换行，更易读。
- 这对应 `export_html` 里 `HtmlOptions { pretty: config.pretty }` 的取值差异。

**预期结果**：两份 HTML 都能正常在浏览器打开并显示「Hello, *HTML* export!」（粗体生效）。

> 待本地验证：不同 Typst 版本对 HTML 序列化的具体格式可能调整，若 `--pretty` 在某版本下视觉差异不明显，以本地实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把输出改成 stdout（`-f html - -`，即输入文件后输出用 `-`），`server.set_html` 还会被调用吗？为什么？

> **答案**：不会被调用。`server.set_html` 只在 `config.server` 为 `Some` 时执行，而服务器只在 `typst watch` 模式下才创建（见 4.4.3）。`typst compile` 即便输出到 stdout，`config.server` 也是 `None`。此外，`compile_once` 在 stdout 输出 + watch 模式时会被 `new_impl` 直接 `bail!`（见 u2-l2）。

**练习 2**：`export_html` 为什么返回 `SourceResult<()>`（空成功值），而不是像图片导出那样返回 `Vec<Output>`？

> **答案**：HTML 是单文件导出，输出路径就是 `config.output` 本身，由调用方在 `compile_and_export` 里用 `.map(|()| vec![config.output.clone()])` 统一包成路径列表。这样设计让 `export_html` 只关心「渲染 + 写出 + 喂服务器」三件事，路径管理的细节上交给分发层。

---

### 4.2 Bundle 导出：VirtualFs 与并行写盘

#### 4.2.1 概念说明

Bundle 是 Typst 里最特殊的目标：**一次编译，多个文件**。

普通导出（PDF/HTML/图片）的产物是「一个文档」，而 Bundle 的产物是一个 `Bundle` 结构，里面装着一个「文件清单」——每个文件要么是一个文档（document，可以是 HTML/PDF/PNG/SVG 之一），要么是一个原始资源（asset，比如图片、字体、数据文件）。这就像用 Typst 生成一整个静态网站。

`Bundle` 的数据结构在 `typst-bundle` 里定义（[lib.rs:44-54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-bundle/src/lib.rs#L44-L54)）：它有一个 `files` 字段，是从虚拟路径到 `BundleFile` 的有序映射。`BundleFile` 有两种（[lib.rs:75-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-bundle/src/lib.rs#L75-L82)）：

```rust
pub enum BundleFile {
    Document(BundleDocument),   // 一个文档（HTML 或某种分页格式）
    Asset(Bytes),               // 原始资源（图片/字体/二进制等）
}
```

但 `Bundle` 本身不能直接写盘——它的 `Document` 还是结构化的文档对象，不是字节。所以中间需要一个 **`VirtualFs`**：把 `Bundle` 里每个文件渲染成字节后，得到一张「路径 → 字节」的表。`VirtualFs` 就是一个类型别名（[export.rs:20](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-bundle/src/export.rs#L20)）：

```rust
pub type VirtualFs = IndexMap<VirtualPath, Bytes, FxBuildHasher>;
```

CLI 拿到这张表后，再用 `write_virtual_fs` 把它铺成一个真实的磁盘目录。

#### 4.2.2 核心流程

```text
OutputFormat::Bundle
   │
   ▼
typst::compile::<Bundle>(world)              ── 编译，得到 Bundle（含文件清单）
   │  (and_then)
   ▼
export_bundle(bundle, config)
   ├── BundleOptions { html, pdf, png, svg } ── 一次性组合四套选项
   ├── typst_bundle::export(&bundle, &opts)? ── 渲染：Bundle → VirtualFs（路径→字节）
   ├── 取输出根目录 root（必须是 Output::Path，stdout 直接 bail!）
   ├── write_virtual_fs(root, &fs)           ── 并行把每个文件写进 root 目录树
   └── if let Some(server) { server.set_bundle(bundle, fs) }  ── watch 下喂服务器
```

`typst_bundle::export` 内部同样用 rayon 并行渲染每个文件（[export.rs:23-40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-bundle/src/export.rs#L23-L40)）：`bundle.files.par_iter()`，对 `Document` 调对应格式的渲染器，对 `Asset` 直接克隆字节。

#### 4.2.3 源码精读

Bundle 分支在 `compile_and_export` 里（[compile.rs:335-339](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L335-L339)）：

```rust
OutputFormat::Bundle => {
    let Warned { output, warnings } = typst::compile::<Bundle>(world);
    let result = output.and_then(|bundle| export_bundle(bundle, config));
    Warned { output: result, warnings }
}
```

`export_bundle` 把「组合选项 → 渲染 → 写盘 → 喂服务器」串起来（[compile.rs:391-415](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L391-L415)）：

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
        Output::Stdout => {
            bail!(Span::detached(), "cannot write bundle to standard output")
        }
    };

    let outputs = write_virtual_fs(root, &fs).at(Span::detached())?;

    #[cfg(feature = "http-server")]
    if let Some(server) = &config.server {
        server.set_bundle(bundle, fs);
    }

    Ok(outputs)
}
```

读这段要注意：

- **stdout 被显式拒绝**：Bundle 是多文件产物，无法塞进单一流，所以输出到 `-` 时直接 `bail!`。
- **`server.set_bundle` 同时需要 `bundle` 和 `fs`**：服务器既要能按路由分发文件（用 `fs`），又要判断某个文件是不是 HTML（用 `bundle.files` 的类型信息来决定是否注入 live reload 脚本，见 4.4.3）。

`write_virtual_fs` 是 CLI 侧真正「把虚拟文件系统铺成磁盘目录」的函数（[compile.rs:418-438](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L418-L438)）：

```rust
fn write_virtual_fs(root: &Path, fs: &VirtualFs) -> StrResult<Vec<Output>> {
    std::fs::create_dir_all(root)
        .map_err(|err| eco_format!("failed to create output directory ({err})"))?;

    fs.par_iter()
        .map(|(path, data)| {
            let realized = path
                .realize(root)
                .map_err(|err| eco_format!("failed to realize path ({err})"))?;

            if let Some(parent) = realized.parent() {
                std::fs::create_dir_all(parent)
                    .map_err(|err| eco_format!("failed to create directory ({err})"))?;
            }

            std::fs::write(&realized, data)
                .map_err(|err| eco_format!("failed to write file ({err})"))?;
            Ok(Output::Path(realized))
        })
        .collect()
}
```

关键细节：

- **`fs.par_iter()`**：用 rayon 并行写每个文件，多个文件可同时落盘。
- **先建根目录，再逐文件建父目录**：`create_dir_all` 幂等，子目录的父链会被自动补齐，所以即便文件路径嵌套很深（如 `assets/img/a.png`），也能正确创建。
- **`path.realize(root)`**：把虚拟路径（`VirtualPath`）拼到真实根目录上，得到最终的磁盘绝对路径。

#### 4.2.4 代码实践

**实践目标**：通过源码阅读理解 Bundle 写盘过程，并尝试触发 Bundle 导出。

**操作步骤**：

1. 阅读 `write_virtual_fs`（[compile.rs:418-438](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L418-L438)），画一张「VirtualFs 条目 → 磁盘文件」的映射草图：每个 `(VirtualPath, Bytes)` 经 `realize(root)` 得到绝对路径，父目录被 `create_dir_all` 补齐，最后 `std::fs::write`。

2. （可选，需要较新的 Typst bundle 能力）尝试编译一个含 `document` / `asset` 元素的文档为 Bundle。先用 `--format bundle` 指定格式，输出路径会变成一个目录：

   ```bash
   ./target/debug/typst compile --features bundle,html --format bundle doc.typ out-dir
   ```

3. 检查输出目录 `out-dir/` 的结构。

**需要观察的现象**：

- 输出是一个**目录**而不是单个文件，里面按 `VirtualPath` 组织了若干文件。
- 对照 `write_virtual_fs` 理解每个子文件是如何被并行写入的。

**预期结果**：Bundle 目录被正确创建，内部文件结构与 `VirtualFs` 的条目一一对应。

> 待本地验证：Bundle 目标的文档写法（`document`、`asset` 元素）属于较新的实验性能力，具体可用语法以本地 Typst 版本的文档为准；若尚不支持，本实践退化为「源码阅读型实践」——重点是理解 `write_virtual_fs` 的并行写盘逻辑。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `export_bundle` 在输出为 stdout 时 `bail!`，而 `export_html` 不需要这个检查？

> **答案**：HTML 是单文件，可以正常写进 stdout（`Output::write` 对 `Stdout` 分支直接写 `stdout()`）。Bundle 是多文件产物，一个 stdout 流无法表达多个独立文件，所以必须拒绝。

**练习 2**：`write_virtual_fs` 为什么对每个文件都单独 `create_dir_all(parent)`，而不是先递归建好所有目录再写文件？

> **答案**：因为写盘是 `par_iter()` **并行**的——每个文件在自己的 rayon 任务里独立处理，无法假定「目录已经建好」。`create_dir_all` 是幂等的，并行调用同一目录也安全，所以每条任务各自保证自己的父目录存在是最稳妥的做法。

---

### 4.3 BundleOptions：四格式选项的组合

#### 4.3.1 概念说明

PDF 导出只需 `PdfOptions`，HTML 导出只需 `HtmlOptions`。但 Bundle 导出**不知道**自己内部每个文档会是什么格式——一个 Bundle 里可能既有 HTML 文档，又有 PDF 文档，还有 PNG/SVG 图片。所以 Bundle 必须把四种格式的选项**一次性都准备好**，交给底层 `typst_bundle::export`，由它根据每个文件的实际类型挑用对应的那套。

`BundleOptions` 就是这四套选项的打包（[export.rs:43-53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-bundle/src/export.rs#L43-L53)）：

```rust
pub struct BundleOptions {
    pub html: HtmlOptions,
    pub pdf: PdfOptions,
    pub png: RenderOptions,
    pub svg: SvgOptions,
}
```

#### 4.3.2 核心流程

CLI 侧 `export_bundle` 用四个 helper 函数分别组装每套选项，再塞进 `BundleOptions`：

```text
export_bundle
   │
   ├── html_options(config)  → HtmlOptions   { pretty }
   ├── pdf_options(config)   → PdfOptions     { timestamp, page_ranges, standards, tagged, pretty, ... }
   ├── png_options(config)   → RenderOptions  { pixel_per_pt, render_bleed }
   └── svg_options(config)   → SvgOptions     { render_bleed, pretty }
         │
         ▼
   BundleOptions { html, pdf, png, svg }  ── 传给 typst_bundle::export
```

注意这四个 helper 函数**同时也被单格式导出复用**：`export_pdf` 用 `pdf_options`，`export_image_page` 用 `png_options`/`svg_options`，`export_html`（间接）用 `html_options`。这是 CLI 侧避免重复代码的典型手法。

#### 4.3.3 源码精读

`export_bundle` 里组装 `BundleOptions` 的四行（[compile.rs:392-397](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L392-L397)）：

```rust
let options = BundleOptions {
    html: html_options(config),
    pdf: pdf_options(config),
    png: png_options(config),
    svg: svg_options(config),
};
```

四个 helper 的实现都很短，关键看它们各自从 `CompileConfig` 取了什么：

- `html_options`（[compile.rs:595-597](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L595-L597)）：只取 `pretty`。
- `pdf_options`（[compile.rs:600-625](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L600-L625)）：最复杂，取时间戳、页面范围、PDF 标准、tagged、pretty 等。
- `png_options`（[compile.rs:633-638](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L633-L638)）：`pixel_per_pt = config.ppi / 72.0`，把 `--ppi` 换算成每点像素。
- `svg_options`（[compile.rs:628-630](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L628-L630)）：取 `render_bleed` 和 `pretty`。

PPI 的换算值得注意：命令行给的是「每英寸像素数」（`--ppi`，默认 144），而渲染器要的是「每点像素数」。因为 1 英寸 = 72 点，所以：

\[
\text{pixel\_per\_pt} = \frac{\text{ppi}}{72}
\]

默认 `144 / 72 = 2`，即每个点渲染成 2×2 像素。

#### 4.3.4 代码实践

**实践目标**：通过对比「单格式导出」与「Bundle 导出」对同一选项的复用，理解 `BundleOptions` 的组合思想。

**操作步骤**：

1. 在 `compile.rs` 里找到 `pdf_options`、`png_options`、`svg_options`、`html_options` 四个函数的定义位置（行号见 4.3.3）。
2. 用搜索（`Grep`）找出每个函数的所有调用点。你会发现：
   - `pdf_options` 同时被 `export_pdf` 和 `export_bundle` 调用。
   - `png_options` 同时被 `export_image_page`（PNG 分支）和 `export_bundle` 调用。
   - `svg_options` 同理。
3. 改一个参数观察行为：把一个含图片的文档用 `--ppi 288` 编译成 PNG，再对比默认 `--ppi 144`。

**需要观察的现象**：

- `--ppi 288`（翻倍）生成的 PNG 尺寸（像素宽高）大约是默认的两倍。
- 这对应 `png_options` 里 `pixel_per_pt = config.ppi / 72.0` 的线性关系。

**预期结果**：PPI 翻倍，输出图片像素尺寸近似翻倍。

#### 4.3.5 小练习与答案

**练习 1**：如果将来 Typst 新增一种导出格式（比如 EPUB），并希望它也能出现在 Bundle 里，需要改动 `BundleOptions` 和 CLI 的哪些地方？

> **答案**：需要在 `typst-bundle` 的 `BundleOptions` 加一个字段（如 `pub epub: EpubOptions`），并在 CLI 的 `export_bundle` 里用对应的 helper（如 `epub_options(config)`）填充它。同时 `typst_bundle::export` 内部的 `export_document` 要新增对应该格式的渲染分支。CLI 侧的 helper 函数最好也复用于单独的 EPUB 导出路径。

**练习 2**：`BundleOptions` 派生了 `#[derive(Debug, Default)]`（见 export.rs:42-43），`Default` 在这里有什么用？

> **答案**：提供一个全默认值的 `BundleOptions` 作为基础。各子选项（`HtmlOptions`/`PdfOptions` 等）自身也有 `Default` 实现，`BundleOptions` 的 `Default` 就是把它们的默认值组合起来。这在需要构造「带默认值的选项、再局部覆盖」时很方便。

---

### 4.4 ServerArgs 与 HttpServer：内置服务器与 live reload

#### 4.4.1 概念说明

前面三个模块讲的都是「写文件」。这一节讲 watch 模式下独有的能力：**在本地起一个 HTTP 服务器，浏览器访问就能实时看到编译结果，源文件一改，浏览器自动刷新**。

这套机制由两部分组成：

1. **CLI 侧（`compile.rs` + `args.rs`）**：决定「要不要起服务器、起在哪个端口、要不要 live reload」，并把每次编译出的 HTML/Bundle 喂给服务器。
2. **服务器实现（`typst-kit/src/server.rs`）**：真正的 HTTP 服务、请求路由、live reload 脚本注入与 SSE 推送。

关键约束：**服务器只在 `typst watch` 模式下才会创建**。`typst compile` 即便输出 HTML 也只写文件、不起服务器。而且服务器只服务 `Html` 和 `Bundle` 两种格式——PDF 没法在浏览器里「实时预览」，所以不起服务器。

#### 4.4.2 核心流程

服务器从创建到 live reload 的完整链路：

```text
typst watch -f html doc.typ
   │
   ▼  CompileConfig::watching → new_impl
判断是否起服务器：
   watch.is_some() && !server.no_serve && output_format ∈ {Html, Bundle}
   │  满足
   ▼
HttpServer::new(title, port, !no_reload)
   ├── start_server：绑定 port，否则在 3000..=3005 找空闲端口
   ├── 初始占位页 PLACEHOLDER_HTML（「Waiting for output…」）
   └── 起一个独立线程跑 server.incoming_requests() 循环
   │
   ▼  每轮 compile_once → export_html / export_bundle
server.set_html(html)   或   server.set_bundle(bundle, fs)
   │  把新内容放进 Bucket，condvar.notify_all()
   ▼
浏览器侧：
   - 请求 /  → 返回 HTML（若开启 reload，注入 <script>）
   - 页面里的 EventSource("/__events") 建立长连接
   - server 每次收到新内容 → SSE 推送 event: reload
   - 浏览器收到 reload → location.reload()
```

#### 4.4.3 源码精读

**① CLI 参数：`ServerArgs`**

`ServerArgs` 是 watch 命令专属的参数组（[args.rs:492-510](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L492-L510)）：

```rust
#[cfg(feature = "http-server")]
#[derive(Debug, Clone, Parser)]
pub struct ServerArgs {
    /// Disables the built-in HTTP server for HTML export.
    #[clap(long)]
    pub no_serve: bool,

    /// Disables the injected live reload script for HTML export. The HTML that
    /// is written to disk isn't affected either way.
    #[clap(long)]
    pub no_reload: bool,

    /// The port where HTML is served.
    ///
    /// Defaults to the first free port in the range 3000-3005.
    #[clap(long)]
    pub port: Option<u16>,
}
```

它通过 `#[clap(flatten)]` 挂在 `WatchCommand` 上（[args.rs:122-133](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L122-L133)）。三个开关的含义：

| 开关 | 作用 |
|------|------|
| `--no-serve` | 完全不起 HTTP 服务器（只写文件） |
| `--no-reload` | 起服务器，但不注入 live reload 脚本（浏览器不会自动刷新） |
| `--port <N>` | 指定端口；不指定则在 3000–3005 找第一个空闲端口 |

注意 `--no-reload` 的注释强调：**写到磁盘的 HTML 不受影响**——live reload 脚本只在服务器返回给浏览器时才注入，落盘的文件永远是干净的。

**② CLI 侧：何时构造服务器**

在 `CompileConfig::new_impl` 里，服务器的构造受三重条件守护（[compile.rs:184-196](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L184-L196)）：

```rust
#[cfg(feature = "http-server")]
let server = if let Some(command) = watch
    && !command.server.no_serve
    && matches!(output_format, OutputFormat::Html | OutputFormat::Bundle)
{
    Some(HttpServer::new(
        &eco_format!("{input}"),
        command.server.port,
        !command.server.no_reload,
    )?)
} else {
    None
};
```

三个条件缺一不可：

1. `watch.is_some()`——必须 watch 模式（`typst compile` 走的是 `CompileConfig::new`，传 `None`）。
2. `!no_serve`——用户没禁用服务器。
3. 输出格式是 `Html` 或 `Bundle`——PDF/PNG/SVG 不起服务器。

第三参数 `!command.server.no_reload` 把「是否启用 live reload」传给 `HttpServer::new`。

构造出的 `server` 存进 `CompileConfig.server` 字段（[compile.rs:86-88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L86-L88)），后续被 `export_html`/`export_bundle`/`open_output`/`Status::print` 共用。

**③ 服务器实现：`HttpServer`**

`HttpServer` 结构很简单（[server.rs:20-23](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L20-L23)）：一个监听地址 `addr` 加一个「内容桶」`bucket`（`Arc<RouterBucket>`）。

`HttpServer::new` 做四件事（[server.rs:27-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L27-L41)）：绑定端口、生成占位 HTML（首次编译前显示「Waiting for output…」）、把初始路由放进桶里、**另起一个线程**跑请求循环。这个独立线程是关键——HTTP 服务不阻塞主线程的 watch 循环。

端口选择在 `start_server`（[server.rs:109-141](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L109-L141)）：

```rust
const BASE_PORT: u16 = 3000;
// ...
match TcpListener::bind(addr) {
    Ok(listener) => break listener,
    Err(err) if err.kind() == io::ErrorKind::AddrInUse => {
        if let Some(port) = port {
            bail!("port {port} is already in use")     // 用户指定端口被占 → 直接报错
        } else if retries < 5 {
            retries += 1;                                // 自动从 3000 试到 3005
        } else {
            bail!("could not find free port for HTTP server");
        }
    }
    // ...
}
```

逻辑很清晰：用户指定 `--port` 时被占就报错；不指定时按 3000→3005 顺序试，全占满才报错。

**④ live reload：Bucket + SSE**

live reload 的核心是一个「内容桶」`Bucket<T>`（[server.rs:313-340](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L313-L340)），它封装了 `Mutex<T>` + `Condvar`：

- `put(data)`：写入新内容，并 `condvar.notify_all()` 唤醒所有等待者。
- `wait()`：阻塞直到有人 `put`。

每次 `compile_once` 产出新 HTML，`server.set_html` 就调 `bucket.put(...)`（[server.rs:50-52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L50-L52)），既更新了服务内容，又唤醒了所有连着 `/__events` 的浏览器。

浏览器侧的刷新靠 **Server-Sent Events（SSE）**。`handle_events_blocking` 是一个长连接循环（[server.rs:198-220](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L198-L220)）：

```rust
loop {
    bucket.wait();                                   // 等待新内容
    write!(writer, "event: reload\ndata:\n\n")?;     // 推送 SSE "reload" 事件
    writer.flush()?;
}
```

而浏览器里监听这个事件的脚本，是服务器在返回 HTML 时**动态注入**的（[server.rs:223-226](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L223-L226)）：在 `</body>` 前插入一小段 JS。注入的脚本内容（[server.rs:376-381](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L376-L381)）：

```rust
const LIVE_RELOAD_SCRIPT: &str = "\
<script>\
  new EventSource(\"/__events\")\
    .addEventListener(\"reload\", () => location.reload())\
</script>\
";
```

`EventSource` 是浏览器原生 API，专门接收 SSE。它连到 `/__events`，收到 `reload` 事件就 `location.reload()` 刷新页面。

注意注入只发生在 `HttpBody::Html` 上（[server.rs:168-177](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L168-L177)）：HTML 才会被插脚本，`Raw` 资源（图片、字体等）不插。Bundle 场景下，`set_bundle` 会判断每个文件是不是 HTML 文档（用 `bundle.files` 的类型信息），HTML 走 `HttpBody::Html`、其余走 `HttpBody::Raw`（[server.rs:55-78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/server.rs#L55-L78)）。

**⑤ `open_output` 的服务器跳转**

`--open` 选项在 watch+服务器场景下会打开**网址**而不是文件（[compile.rs:674-681](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L674-L681)）：

```rust
#[cfg(feature = "http-server")]
if let Some(server) = &config.server {
    let url = format!("http://{}", server.addr());
    return open_path(OsStr::new(&url), viewer.as_deref());
}
```

这样 `typst watch --open -f html doc.typ` 会直接用默认浏览器打开 `http://127.0.0.1:<port>/`。

**⑥ watch 状态行打印服务器地址**

watch 每轮刷新状态时，如果起了服务器，会多打印一行「serving at http://...」（[watch.rs:116-122](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L116-L122)）：

```rust
#[cfg(feature = "http-server")]
if let Some(server) = &config.server {
    out.set_color(&color)?;
    write!(out, "serving at")?;
    out.reset()?;
    writeln!(out, " http://{}", server.addr())?;
}
```

这就是你在终端看到的那个可点击地址的来源。

#### 4.4.4 代码实践

**实践目标**：亲手启动 watch 的内置 HTTP 服务器，观察 live reload，并验证三个开关的效果。

**操作步骤**：

1. 准备 `hello.typ`：

   ```typst
   #set page(width: 20cm)
   Hello at #datetime().display().
   ```

2. 启动 watch 并自动打开浏览器：

   ```bash
   ./target/debug/typst watch --features html --open -f html hello.typ hello.html
   ```

3. 终端会打印类似 `serving at http://127.0.0.1:3000`，浏览器自动打开该地址并显示页面。

4. 保持 watch 运行，**修改 `hello.typ` 的内容并保存**，观察浏览器。

5. 重新启动，分别测试两个开关：
   - `--no-serve`：终端不再有 `serving at` 行，只写文件。
   - `--no-reload`：服务器照常起，但改文件后浏览器不会自动刷新（需手动刷新）；磁盘上的 `hello.html` 仍会更新。

**需要观察的现象**：

- 修改源文件后，watch 重新编译，浏览器**自动刷新**显示新内容（默认行为）。
- `--no-reload` 下浏览器不自动刷新，但磁盘 HTML 文件内容确实变了。
- `--port 4000` 能把服务起在指定端口；若该端口被占，会直接报错（而不是换端口）。

**预期结果**：默认配置下 live reload 工作正常；`--no-serve` 完全关闭服务器；`--no-reload` 关闭自动刷新但保留服务器。

> 待本地验证：`--open` 调用系统默认浏览器，在无图形界面的纯命令行环境下可能无法打开（此时退化为「源码阅读型实践」——对照 `open_output` 的 server 分支理解它打开的是 URL 而非文件）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `HttpServer::new` 要在一个**独立线程**里跑 `incoming_requests()` 循环，而不是在主线程？

> **答案**：因为 watch 的主线程要跑「监听文件变化 → 等待 → 重编译」的循环（见 u2-l5）。HTTP 服务是阻塞式的（`incoming_requests()` 是死循环），如果放在主线程，主线程就无法继续做编译和文件监听。独立线程让「服务 HTTP 请求」和「编译 + watch」并行，二者通过 `Bucket`（`Mutex + Condvar`）通信：主线程编译完调 `set_html` → `bucket.put`，HTTP 线程在 `bucket.wait` 被唤醒后推送 SSE。

**练习 2**：live reload 用 SSE（`EventSource`）而不是 WebSocket，这样做有什么取舍？

> **答案**：SSE 是单向（服务器→浏览器）、基于 HTTP 的轻量协议，浏览器原生支持 `EventSource`，服务端实现也简单（`tiny_http` 普通响应 + 手写 chunk 即可，见 `handle_events_blocking`）。对于「只需服务器通知浏览器刷新」这种单向场景，SSE 足够且更简单；WebSocket 是双向的，能力过剩、实现更重。Typst 这里只需要「编译完通知刷新」，所以选了 SSE。

**练习 3**：如果一个 Bundle 里同时有 HTML 文档和 PNG 图片，浏览器访问图片对应的路由时，会注入 live reload 脚本吗？

> **答案**：不会。`set_bundle` 根据每个文件在 `bundle.files` 里的类型决定 `HttpBody`：只有 HTML 文档会被包成 `HttpBody::Html`（才会注入脚本），PNG 等资源被包成 `HttpBody::Raw`。而 `inject_live_reload_script` 只在 `handle_body` 处理 `HttpBody::Html` 时调用。所以图片路由返回纯二进制，不带脚本。

## 5. 综合实践

把本讲四个模块串起来，做一个「HTML 实时预览」小实验，并对照源码解释每一步。

**任务**：用 watch 模式编译一个引用了本地资源的文档为 HTML，启动内置服务器，在浏览器实时预览，并解释从「保存文件」到「浏览器刷新」之间发生了什么。

**步骤**：

1. 建一个工作目录，准备：
   - `doc.typ`：一段 HTML 友好的内容，例如带标题、强调、列表。
   - （可选）一张图片 `img.png` 放在同目录，文档里 `#image("img.png")`。

2. 启动 watch + 服务器：

   ```bash
   ./target/debug/typst watch --features html --open -f html doc.typ out/doc.html
   ```

3. 等待「serving at http://127.0.0.1:XXXX」出现，浏览器打开页面。

4. 修改 `doc.typ`（如改一行文字），保存。观察浏览器是否自动刷新。

5. 对照源码，**画出事件流时序**：

   - 文件保存 → `Watcher` 检测到变化（u2-l5）→ `watcher.wait()` 返回。
   - `world.reset()` → `compile_once` 重新编译。
   - `compile_and_export` 走 `OutputFormat::Html` 分支 → `export_html`。
   - `export_html` 写盘 **并** 调 `server.set_html(html)`。
   - `server.set_html` → `bucket.put` → `condvar.notify_all`。
   - HTTP 线程里 `handle_events_blocking` 的 `bucket.wait()` 被唤醒 → 推送 SSE `event: reload`。
   - 浏览器 `EventSource` 收到 reload → `location.reload()` → 重新请求 `/` → 拿到新 HTML（含注入的 reload 脚本）。

6. **额外验证 BundleOptions 思想**：把同一份文档编译成 Bundle（若本地支持），检查输出目录里是否同时包含 HTML 和被引用的资源文件，理解为什么 `export_bundle` 要一次性准备四套选项（因为它不知道每个文件最终是什么格式）。

**预期结果**：你能用一句话解释「保存 → 刷新」链路上每一步对应的函数，并指出哪些代码只在 watch 模式（而非 compile 模式）才会执行——即 `config.server` 为 `Some` 的所有分支（`export_html`/`export_bundle` 里的 `set_html`/`set_bundle`、`open_output` 的 URL 跳转、`Status::print` 的地址行）。

> 待本地验证：HTML 与 Bundle 目标属于较新的实验性能力，具体可用内容与命令行行为以本地 Typst 版本为准。核心目标——讲清 CLI 如何在 watch 模式下驱动 HTTP 服务器与 live reload——不依赖具体文档语法。

## 6. 本讲小结

- `OutputFormat::Html` 走独立的 `HtmlDocument` 目标类型：`compile_and_export` 编译成 `HtmlDocument`，`export_html` 序列化成 HTML 字符串写盘，并在 watch 模式下额外调 `server.set_html`。
- `OutputFormat::Bundle` 是唯一的多文件目标：编译成 `Bundle`（文件清单），经 `typst_bundle::export` 渲染成 `VirtualFs`（路径→字节映射），再由 `write_virtual_fs` 用 rayon **并行**铺成磁盘目录树；stdout 被显式拒绝。
- `BundleOptions` 一次性打包 html/pdf/png/svg 四套选项，因为 Bundle 内部每个文件的格式在编译期才知道；这四个 helper 同时被对应的单格式导出复用。
- HTTP 服务器**只在 `typst watch` 且格式为 Html/Bundle 且未 `--no-serve` 时**创建，端口默认在 3000–3005 间选取；它在独立线程跑请求循环，主线程通过 `Bucket`（`Mutex+Condvar`）喂数据。
- live reload 靠 SSE：服务器在返回 HTML 时动态注入一段 `EventSource("/__events")` 脚本，每次重编译 `bucket.put` 后向长连接推送 `event: reload`，浏览器收到即 `location.reload()`；`--no-reload` 关闭注入但不影响落盘文件。
- `--open` 在 watch+服务器下打开 `http://{addr}` 网址而非文件路径；`Status::print` 会多打印一行 `serving at http://...`。

## 7. 下一步学习建议

- **u4-l4（自更新机制）**：本讲又一次见到了「watch + 服务器」这种「主循环 + 独立线程 + 共享状态」的并发结构。下一讲的自更新会展示另一类并发与外部交互（下载、解压、self_replace），可以对比两种场景的设计取舍。
- **u4-l6（info 命令与环境内省）**：本讲多次出现 `#[cfg(feature = "http-server")]` 这种条件编译。建议接着读 info 命令，看它如何把「编译期 feature 开关」与「运行时配置」整体报告给用户，巩固对 feature 体系的全局理解。
- **延伸阅读**：如果想深入 live reload 的并发原语，可以读 `server.rs` 的 `Bucket<T>`（`parking_lot` 的 `Mutex`+`Condvar`）以及 u2-l5 提到的 `comemo::evict`，理解 watch 模式如何同时管理「文件缓存」「记忆化缓存」和「服务器内容」三套陈旧状态。
