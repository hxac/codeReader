# 用户视角的 document 与 asset 元素

## 1. 本讲目标

在 [u3-l1](u3-l1-bundle-data-model.md) 中，我们看的是 bundle 编译**之后**的「静态数据模型」——`Bundle`、`BundleFile`、`BundleDocument` 这些产物结构。本讲往回退一步，站到**用户**这一侧：用户在 `.typ` 源码里到底写了什么，才会编译出那些 `BundleFile::Document` 与 `BundleFile::Asset`？

读完本讲，你应当能够：

1. 说清 `#document(...)` 元素（`DocumentElem`）的每个字段含义，并区分哪些是「bundle 专用」、哪些是通用元数据。
2. 理解 `documents-in-bundle-export` 语义：一个 `document` 元素 = bundle 输出里的一个文件。
3. 看懂 `#asset(...)` 元素（`AssetElem`）如何用原始字节直通一个文件，并与 `read` / `json.encode` 组合。
4. 解释 `BundlePath` 与普通 Typst 路径的区别，以及它的校验规则。
5. 讲清元数据传播的「hack 但必要」机制：`ShowSet::show_set` 如何把显式 `document(...)` 参数变成上下文可用的样式。

---

## 2. 前置知识

本讲假设你已经读过：

- **u1-l1**：知道 bundle 是 Typst 的多文件输出目标（`Target::Bundle`），需要 `--features bundle` 开启。
- **u1-l2**：知道 `typst::compile::<Bundle>(world)` 走 `Output::create` → `bundle()` → `bundle_impl()` 这条链。
- **u2-l1**：知道 `bundle_impl` 的「realize → collect → parallelize」主流程，以及 collect 维护 `Tag` / `Asset` / `Document` 三类顶层白名单。
- **u3-l1**：知道 `Bundle` 的 `files: IndexMap<VirtualPath, BundleFile>`，`BundleFile` 分 `Document(BundleDocument)` 与 `Asset(Bytes)`。

几个需要先建立的小概念：

- **元素（element）**：Typst 里用 `#xxx(...)` 构造的东西，底层是一个 Rust 结构体（如 `DocumentElem`）。元素既能用「构造函数」显式创建（`#document(...)`），也能用 `set` 规则配置（`#set document(title: ...)`）。
- **可设置字段（settable field）**：元素上标注了 `#[settable]`（在 Typst 源码里写作 `pub field: T` 配合宏）的字段。这类字段既能作为构造参数传入，也能通过 `set` 规则从样式链（style chain）里读取。
- **样式链（StyleChain）**：Typst 解析「这个位置上某个样式值是多少」的数据结构。它把多层 `set` 规则叠起来，内层（更局部）的规则覆盖外层。本讲的「元数据传播」本质就是把显式参数塞进这条链。
- **show-set 规则**：一种「元素在被实现（realize）时，主动往样式链里注入样式」的机制。`page` 元素早就这么做了，`document` 元素为了在 bundle 里工作也用了它。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [crates/typst-library/src/model/document.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/document.rs) | 定义 `DocumentElem`（用户侧 `#document`）、`DocumentFormat`/`PagedFormat`、`DocumentInfo`，以及关键的 `ShowSet` 实现 |
| [crates/typst-library/src/model/asset.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/asset.rs) | 定义 `AssetElem`（用户侧 `#asset`）与 `AssetData`（原始字节包装） |
| [crates/typst-library/src/foundations/path.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/foundations/path.rs) | 定义 `BundlePath`——bundle 输出路径类型及其校验 |
| [crates/typst-bundle/src/lib.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs) | `collect` / `compile_document` 消费这两个元素，产出 `BundleFile` |

辅助理解（用于讲清元数据传播链路）：

| 文件 | 作用 |
| --- | --- |
| [crates/typst-library/src/foundations/content/field.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/foundations/content/field.rs) | `Settable::copy_into`——把字段值写进样式 map |
| [crates/typst-realize/src/lib.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-realize/src/lib.rs) | realize 时调用 `show_set`，以及在 `RealizationKind::Bundle` 下放行 `set document` |
| [crates/typst-library/src/model/title.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/title.rs) | `#title()` 元素从样式链读取 `DocumentElem::title` 的证据 |

> 注意：`DocumentElem` / `AssetElem` / `BundlePath` 都住在 **typst-library**，而不是 typst-bundle。typst-bundle 只是它们的**消费者**。这印证了 u1-l1 的结论：typst-bundle 是组合兄弟 crate 的「编排层」。

---

## 4. 核心概念与源码讲解

### 4.1 DocumentElem：用户视角的 document 元素与 bundle 文档语义

#### 4.1.1 概念说明

`DocumentElem` 就是用户在源码里写的 `#document(...)`。它有两副面孔：

- **在单文档导出（pdf / png / svg / html）里**：它只用于「装元数据」——你几乎总是用 `#set document(title: ..., author: ...)` 来配置标题、作者等，而不是显式构造元素。这些元数据会被嵌入输出（例如 PDF 的文档信息字典），但默认不在页面上渲染。
- **在 bundle 导出里**：它升级成「一个文件」——每个 `#document("路径", ...)[ 内容 ]` 对应 bundle 输出里的**一个文件**。Typst 会把它的 `body` 当成一个独立的小项目，按 `path` 的扩展名推断格式，单独排版/导出。

源码文档里给这第二副面孔起了一个标签，叫 [documents-in-bundle-export](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/document.rs#L72-L93)：「在 bundle 导出中，一个 document 元素代表 bundle 输出里的单个文件」。

一个最小的 bundle 例子（来自源码文档）：

```typ
#document("index.html", title: [Home])[
  #title()
  View #link(<list>)[my famous list].
]

#document("list.html", title: [My Famous List])[
  #title()
  - My
  - Famous
  - List
] <list>
```

这两个 `#document(...)` 会在 bundle 里产出 `index.html` 与 `list.html` 两个文件，并且因为 `#title()` 的存在，标题既被渲染进页面、也被嵌入元数据。

#### 4.1.2 核心流程

把 `DocumentElem` 的字段按「是否 bundle 专用」分成两类：

| 字段 | 类型 | bundle 专用？ | 作用 |
| --- | --- | --- | --- |
| `path` | `BundlePath`（必填） | ✅ 是 | bundle 里的输出路径 |
| `format` | `Smart<DocumentFormat>` | ✅ 是 | 显式指定导出格式；`auto` 时按 `path` 扩展名推断 |
| `body` | `Content`（必填） | ✅ 是 | 文档内容，会被单独排版 |
| `title` | `Option<Content>` | ❌ 否 | 标题（PDF 查看器窗口名 / 浏览器标签名） |
| `author` | `OneOrMultiple<EcoString>` | ❌ 否 | 作者列表 |
| `description` | `Option<Content>` | ❌ 否 | 描述 |
| `keywords` | `OneOrMultiple<EcoString>` | ❌ 否 | 关键词 |
| `date` | `Smart<Option<Datetime>>` | ❌ 否 | 创建日期；`auto` 用当前时间 |

「bundle 专用」字段意味着：在非 bundle 目标里显式构造 `#document(...)` 会直接报错（见 4.1.3 的 `DOCUMENT_UNSUPPORTED_RULE`），只能用 `set document(...)` 配置那些通用元数据字段。

**bundle 里一个 document 的生命周期**（承接 u2-l1 / u2-l2）：

```
#document("paper.pdf", title:[...])[ body ]
        │
        ▼  realize（RealizationKind::Bundle）后
   collect() 把它认成 Child::Document，并校验 path 唯一
        │
        ▼  parallelize 并行处理每个文档
   compile_document():
     1. determine_format()  → 显式 format 或按扩展名推断
     2. TargetElem::target.set(...) 切换目标到 Paged/Html
     3. 分派 layout_document_for_bundle / html_document_for_bundle
        │
        ▼  产出
   BundleDocument::Paged(..) / BundleDocument::Html(..)
        │
        ▼  装进 files
   BundleFile::Document(BundleDocument)
```

关键点：`DocumentElem` 自身**不做排版**。它只是一个「描述符」——告诉 bundle「这里有一个文件，路径是 X，格式是 Y，内容是 body」。真正把 `body` 排版成 `PagedDocument` / `HtmlDocument` 的是 `compile_document` 调用的兄弟 crate（见 u2-l2）。

#### 4.1.3 源码精读

**元素定义与字段**（注意宏属性里的 `ShowSet`，它是 4.4 的主角）：

[crates/typst-library/src/model/document.rs:125-186](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/document.rs#L125-L186)：`DocumentElem` 的结构定义。`#[elem(since = "forever", Locatable, ShowSet)]` 声明了它可被定位（`Locatable`，bundle 需要靠 location 做跨文档锚点）且实现了 `ShowSet`。`path` 与 `body` 标了 `#[required]`（必填），`format` / `title` / `author` 等都是可设置字段。每个字段的文档注释里都标明了它是否「only supported in the bundle target」。

**非 bundle 目标下显式构造会报错**：

[crates/typst-library/src/model/document.rs:219-227](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/document.rs#L219-L227)：`DOCUMENT_UNSUPPORTED_RULE` 常量。当用户在 pdf 等单文档目标里直接写 `#document(...)`（而不是 `set document`）时，会触发这条规则，报「constructing a document is only supported in the bundle target」，并给出「开启 bundle」或「改用 `set document(..)`」两个 hint。

**格式推断**（承接 u2-l2）：

[crates/typst-library/src/model/document.rs:191-206](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/document.rs#L191-L206)：`Packed<DocumentElem>::determine_format`。先用 `self.format.get(styles).custom()` 取显式 `format`；取不到再退到 `determine_format_from_path` 按扩展名推断；都没有就报 `unknown document format`。

[crates/typst-library/src/model/document.rs:209-217](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/document.rs#L209-L217)：`determine_format_from_path`——`.pdf`/`.svg`/`.png` 映射到对应的 `PagedFormat`，`.html` 映射到 `DocumentFormat::Html`，其余扩展名返回 `None`。

**消费 DocumentElem 的地方**（typst-bundle 侧）：

[crates/typst-bundle/src/lib.rs:188-192](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L188-L192)：在 `bundle_impl` 的 `parallelize` 闭包里，`Child::Document(document, styles, locator)` 被取出：`document.path.clone().into_inner()` 拿到 `VirtualPath`（输出路径），`compile_document(...)` 把 `body` 编译成 `BundleDocument`，`document.location().unwrap()` 拿到用于锚点的 `Location`。

#### 4.1.4 代码实践

**实践目标**：亲手验证「一个 `#document(...)` = bundle 里的一个文件」，并观察格式按扩展名推断。

**操作步骤**：

1. 在一个空目录里新建 `main.typ`：

   ```typ
   #document("index.html", title: [首页])[
     #title()
     Hello, bundle.
   ]

   #document("report.pdf")[
     = 第一节
     这是一个 PDF 文档。
   ]
   ```

2. 用开启 bundle feature 的 typst 编译到目录 `out/`（命令形如 `typst compile --features bundle main.typ out/`，具体子命令与参数以本地 typst 版本为准——**待本地验证**）。

3. 查看 `out/` 目录。

**需要观察的现象**：

- `out/` 下应出现两个文件：`index.html` 与 `report.pdf`。
- `index.html` 的页面里能看到「首页」（因为 `#title()` 把标题渲染了出来）。
- `report.pdf` 没写 `format`，但因为扩展名是 `.pdf`，被推断成 PDF。

**预期结果**：bundle 把两个 `#document` 分别导出为对应格式的文件，互不干扰。如果观察到报错「constructing a document is only supported in the bundle target」，说明你忘了带 `--features bundle`。

> 说明：本实践未在本讲义环境中实跑，文件结构以源码文档与逻辑为依据，请以本地实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：把上面 `report.pdf` 的扩展名改成 `.png`，但内容有两页（多加几个分页）。会发生什么？

**参考答案**：格式会被推断成 `PagedFormat::Png`；但因为 PNG 是图片格式、只支持单页，`compile_document` 会通过 `delayed_error` 报「expected document to have a single page」（见 u2-l2 的单页约束）。之所以用 `delayed_error` 而非立即报错，是因为页数可能随内省收敛而变化。

**练习 2**：在单文档 PDF 目标（不带 bundle feature）里写 `#document("x.pdf")[Hi]`，会得到什么错误？给出两个 hint 的内容。

**参考答案**：触发 `DOCUMENT_UNSUPPORTED_RULE`，报「constructing a document is only supported in the bundle target」；两个 hint 分别是「try enabling the bundle target」和「or use a `set document(..)` rule to configure metadata」。

---

### 4.2 AssetElem 与 AssetData：原始字节直通

#### 4.2.1 概念说明

`AssetElem` 是用户写的 `#asset(...)`。它和 `document` 的根本区别是：**asset 不参与排版**。你给它一段原始字节，Typst 原封不动地把它写进 bundle 里的指定路径。

它解决两类需求：

1. **搬运文件**：把项目里已有的文件（CSS、图片、字体、数据文件）原样复制到输出 bundle。典型搭配是 `read`：
   ```typ
   #asset("styles.css", read("styles.css"))
   ```
   第一个参数是 bundle 里的输出路径，`read` 的参数是项目里的源路径。

2. **生成数据**：用 Typst 代码计算出一些字节直接吐成文件，最常见的搭配是 `json.encode`：
   ```typ
   #context {
     let headings = query(heading)
     asset("meta.json", json.encode((count: headings.len())))
   }
   ```
   这会在 bundle 里生成一个 `meta.json`，内容是文档里标题的数量。

#### 4.2.2 核心流程

`AssetElem` 的结构极简——只有两个字段：

| 字段 | 类型 | 作用 |
| --- | --- | --- |
| `path` | `BundlePath`（必填） | bundle 里的输出路径 |
| `data` | `AssetData`（必填） | 原始字节；给字符串则按 UTF-8 编码 |

`AssetData` 是对 `Bytes` 的薄包装（`pub struct AssetData(pub Bytes)`），支持从 `Str` 或 `Bytes` 转换而来。在 bundle 里它会被「拆箱」成纯 `Bytes`，成为 `BundleFile::Asset(bytes)`（回顾 u3-l1）。

**asset 的生命周期**比 document 简单得多，因为它不需要排版：

```
#asset("meta.json", json.encode(...))
        │
        ▼  realize（RealizationKind::Bundle）后
   collect() 认成 Child::Asset，校验 path 唯一
        │
        ▼  parallelize
   asset.path.clone().into_inner()  → VirtualPath（输出路径）
   asset.data.0.clone()             → Bytes（原始字节）
        │
        ▼  装进 files
   BundleFile::Asset(Bytes)
```

注意：asset **没有**「编译」这一步。它只是「路径 + 字节」的二元组，直接落进 `files`。

#### 4.2.3 源码精读

**AssetElem 定义**：

[crates/typst-library/src/model/asset.rs:52-66](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/asset.rs#L52-L66)：`AssetElem` 只有两个必填字段 `path: BundlePath` 与 `data: AssetData`。注意宏属性是 `#[elem(since = "0.15.0", Locatable)]`——比 document 晚（`0.15.0`），且**没有** `ShowSet`（asset 没有需要传播的元数据）。同样标了 `Locatable`，让 bundle 能拿到它的 `Location` 用于跨文档链接（详见 u5-l2）。

**AssetData 的类型转换**：

[crates/typst-library/src/model/asset.rs:78-86](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/asset.rs#L78-L86)：`AssetData(pub Bytes)` 与它的 `cast!`。从 `Str` 进来时用 `Bytes::from_string` 按 UTF-8 编码；从 `Bytes` 进来时直接用。这就是为什么 `read(...)`（返回字符串或字节）和 `json.encode(...)`（返回字符串）都能直接喂给 `asset`。

**非 bundle 目标下报错**：

[crates/typst-library/src/model/asset.rs:68-75](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/asset.rs#L68-L75)：`ASSET_UNSUPPORTED_RULE`，报「assets are only supported in the bundle target」，hint 是「try enabling the bundle target」。

**消费 AssetElem 的地方**：

[crates/typst-bundle/src/lib.rs:183-187](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L183-L187)：在 `bundle_impl` 的 `parallelize` 闭包里，`Child::Asset(asset)` 被取出——`asset.path.clone().into_inner()` 取路径，`asset.data.0.clone()` 取字节（`.0` 就是 `AssetData` 内部的 `Bytes`），`asset.location().unwrap()` 取 location。对比上一行 `Document` 分支调用了 `compile_document`，这里**没有任何编译调用**——纯数据搬运。

#### 4.2.4 代码实践

**实践目标**：用 `#context` + `json.encode` 生成一个反映文档统计信息的 `meta.json` asset。

**操作步骤**：

1. 新建 `main.typ`：

   ```typ
   #document("doc.pdf")[
     = 引言
     = 方法
     = 结论
   ]

   #context {
     let headings = query(heading)
     let meta = (count: headings.len(), titles: headings.map(h => h.body))
     asset("meta.json", json.encode(meta))
   }
   ```

2. 用 `--features bundle` 编译到 `out/` 目录（**待本地验证**具体命令）。

3. 打开 `out/meta.json`。

**需要观察的现象**：

- `meta.json` 内容是一段 JSON，`count` 字段为 `3`，`titles` 是三个标题的文本。
- `meta.json` 的生成发生在内省收敛之后（因为它依赖 `query(heading)`），所以 `context` 块必不可少——去掉 `#context` 会在解析期就报错。

**预期结果**：asset 把 Typst 运行时计算的结果原样序列化成文件落盘，证明 asset 不限于「复制已有文件」，也能承载「生成数据」。

> 说明：JSON 的具体键顺序与转义细节以本地实际输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `asset("meta.json", json.encode(meta))` 必须包在 `#context { ... }` 里，而 `#asset("styles.css", read("styles.css"))` 不需要？

**参考答案**：`json.encode(meta)` 里的 `meta` 依赖 `query(heading)`，而查询是内省操作、必须在 `context` 块里才能拿到收敛后的结果。`read("styles.css")` 只是读文件、不依赖内省，所以可以在顶层直接用。

**练习 2**：`AssetData` 为什么要同时支持 `Str` 和 `Bytes` 两种输入？

**参考答案**：因为 `read`、`json.encode` 等函数返回字符串（适合文本类资产如 CSS/JSON），而二进制资产（图片、字体）需要按原始字节处理。统一收口到 `AssetData` 后，字符串走 UTF-8 编码、字节直接透传，用户不必关心底层类型。

---

### 4.3 BundlePath：bundle 输出路径与校验

#### 4.3.1 概念说明

`document` 和 `asset` 都有一个 `path: BundlePath` 字段。它和你在 Typst 里常见的「路径」（`PathOrStr` / `RootedPath`，用于 `image(...)` / `include ...`）**不是同一种东西**：

- **普通路径**（输入路径）：指向项目里**已经存在**的源文件，用来**读**。相对路径会相对于当前 `.typ` 文件解析。
- **BundlePath**（输出路径）：指向 bundle 里**将要生成**的文件，用来**写**。它**总是相对于 bundle 根**，不会相对于当前文件解析。

源码注释直说了这一点：「Unlike `PathOrStr`, a string cast through this is always an absolute path instead of being resolved relative to a file. This is not used for normal paths in Typst files, but rather for output file paths in bundle mode.」

#### 4.3.2 核心流程

`BundlePath` 是对 `VirtualPath` 的包装：

```rust
pub struct BundlePath(VirtualPath);
```

它做了三件事：

1. **构造校验**：`BundlePath::new` 拒绝「根路径」（空路径），要求至少有一个组件（即至少一个文件名段）。
2. **字符串转换**：通过 `cast!`，用户传的字符串（如 `"a/b/c.json"`）会被 `VirtualPath::new` 解析成结构化路径；解析失败（如包含反斜杠、或试图 `..` 逃逸）会给出专门的 bundle 错误信息。
3. **拆箱**：`into_inner` / `as_ref` 把内部的 `VirtualPath` 暴露出去，供 typst-bundle 用作 `files` 这个 `IndexMap` 的 key。

路径里可以包含斜杠（`"css/main.css"`），bundle 落盘时会自动创建中间目录（见 u5-l4 的 `write_virtual_fs`）。

**两类校验错误的来源**（都在 `format_bundle_error`）：

| `PathError` | 触发条件 | 报错信息 |
| --- | --- | --- |
| `Escapes` | 路径用 `..` 逃出 bundle 根 | `path "..." would escape the bundle root` |
| `Backslash` | 路径含反斜杠 `\` | `path must not contain a backslash` + 改用正斜杠的 hint |

注意 bundle 的 `Escapes` 错误信息与普通路径不同（普通路径说「escape the project/package root」，bundle 说「escape the bundle root」）。

#### 4.3.3 源码精读

**BundlePath 定义与构造校验**：

[crates/typst-library/src/foundations/path.rs:253-274](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/foundations/path.rs#L253-L274)：`BundlePath(VirtualPath)`。`new` 在 `path.is_root()` 时 `bail!("path must have at least one component")`——禁止空输出路径。`into_inner` 取出内部 `VirtualPath`。

**作为 VirtualPath 的引用**：

[crates/typst-library/src/foundations/path.rs:276-280](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/foundations/path.rs#L276-L280)：`impl AsRef<VirtualPath> for BundlePath`。这就是为什么 typst-bundle 里能写 `elem.path.as_ref()` 拿到 `&VirtualPath`。

**字符串 → BundlePath 的转换**：

[crates/typst-library/src/foundations/path.rs:282-288](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/foundations/path.rs#L282-L288)：`cast! BundlePath`。用户传字符串时，`VirtualPath::new(&v)` 解析，失败则走 `format_bundle_error`。

**bundle 专属错误信息**：

[crates/typst-library/src/foundations/path.rs:291-302](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/foundations/path.rs#L291-L302)：`format_bundle_error`。对比 [crates/typst-library/src/foundations/path.rs:227-251](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/foundations/path.rs#L227-L251) 的普通路径版本 `format_resolve_error`，可以看到两者措辞不同：bundle 版强调「bundle root」，且反斜杠 hint 更简短（不再提 Windows 历史兼容）。

**消费端读取 path**：

[crates/typst-bundle/src/lib.rs:251](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L251) 与 [crates/typst-bundle/src/lib.rs:254](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L254)：`collect` 里对 `AssetElem` / `DocumentElem` 都取 `elem.path.as_ref()` 作为查重 key。路径唯一性校验在 u2-l1 讲过（`Entry::Vacant` / `Occupied` + `delayed_error`），这里只强调：**比较的就是 `BundlePath` 内部的 `VirtualPath`**。

#### 4.3.4 代码实践

**实践目标**：通过源码阅读，理解 `BundlePath` 与普通 `PathOrStr` 的差异，并预测几种路径输入的解析结果。

**操作步骤**：

1. 打开 [crates/typst-library/src/foundations/path.rs:282-288](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/foundations/path.rs#L282-L288)，确认字符串到 `BundlePath` 的 cast 走 `VirtualPath::new`。
2. 对照 [crates/typst-library/src/foundations/path.rs:304-331](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/foundations/path.rs#L304-L331) 的单测 `test_resolve`，理解普通相对路径如何被解析（如 `..` 相对于父目录）。
3. 预测下列 bundle 路径的解析结果（先别跑）：
   - `#asset("data/info.json", ...)` —— 合法，输出到 `data/info.json`。
   - `#asset("../escape.json", ...)` —— 应触发 `Escapes`，报「would escape the bundle root」。
   - `#asset("sub\\file.json", ...)` —— 应触发 `Backslash`，报「path must not contain a backslash」。

**需要观察的现象**：后两者在编译期就报错（cast 失败），而不是等到落盘。

**预期结果**：bundle 路径是「写」路径，受到比普通「读」路径更严格的根约束——任何试图逃出 bundle 根或使用反斜杠的输入都会被拦下。

> 说明：解析逻辑以源码为准；如想确认报错文本，可本地构造最小用例编译观察。

#### 4.3.5 小练习与答案

**练习 1**：为什么 bundle 输出路径「总是相对于 bundle 根」，而不像普通路径那样相对于当前文件？

**参考答案**：输出路径描述的是「产物放在哪」，与源码在项目里的物理位置无关。如果让它相对于当前 `.typ` 文件解析，那么把一段模板 `include` 到不同目录的文件里，同一句 `#asset("x.json", ...)` 就会输出到不同地方，破坏可预测性。统一相对于 bundle 根，让路径语义与源码位置解耦。

**练习 2**：`BundlePath::new` 为什么拒绝根路径（空路径）？

**参考答案**：bundle 里的每个文件都必须有一个文件名（path 至少一个组件）。空路径既无法命名输出文件，也无法据此推断格式（`determine_format_from_path` 要靠扩展名），所以在构造时就拒绝。

---

### 4.4 元数据传播：ShowSet 把 document 属性变成上下文可用

这是本讲最精妙、也最「hack」的部分。它回答一个关键问题：

> 我用 `#document("x.html", title: [My title])[ ... ]` 显式传了 `title`，为什么文档体里的 `#title()` 和 `#context document.title` 能读到它？

答案藏在 `impl ShowSet for Packed<DocumentElem>` 里。

#### 4.4.1 概念说明

先理解「正常」的元素是怎样的：元素的某个可设置字段，其值通常来自样式链（`set` 规则），元素实例只是「读取」它。也就是说，信息流是 **样式链 → 元素**。

但 `document` 元素遇到了一个难题：用户既能用 `set document(title: ...)` 设置标题（信息在样式链里），也能用 `document(title: ...)` 显式传标题（信息在元素实例的字段里）。而后者的信息默认**不在样式链里**——可是 `#title()` 元素和 `context document.title` 都是从样式链读 `DocumentElem::title` 的（见下方源码证据）。

为了让「显式参数」也能被这些「从样式链读取」的消费者看到，`DocumentElem` 实现了 `ShowSet`：在元素被 realize 时，**反向**把显式字段值塞回样式链。信息流变成 **元素 → 样式链**（再被下游读出来）。

源码注释承认这很 hacky：

> Making the properties available like this is inconsistent with normal elements, but consistent with `page` and necessary to make the `title` element work. Nonetheless, it's fairly hacky and the whole thing should probably be revisited at some point. Also see #6721.

为什么「necessary（必要）」？因为不这么做，`#title()` 读不到显式 `title` 参数。为什么「hacky」？因为它打破了元素字段「只读不写回样式链」的惯例，让信息流反向。它与 `page` 元素的行为一致（`page` 也用 show-set 把页面参数注入样式），所以算是一种「既定但不够干净」的惯用法。相关 issue 是 [#6721](https://github.com/typst/typst/issues/6721)。

#### 4.4.2 核心流程

完整的元数据传播链分两条路径，最终都汇入「嵌入输出元数据」：

**路径 A：显式参数 → show_set → 样式链**

```
document("x.html", title:[T])[ body ]
   │ realize 时调用 show_set
   ▼
copy_into: 把 title/author/.../date 写进一个 Styles map（仅当字段被显式设置）
   │ map.apply(...) 注入到 body 的样式链底层（外层）
   ▼
body 内的 #title() / context document.title 从样式链读到 [T]
```

**路径 B：set 规则 → 样式链**

```
body 内的 #set document(title:[T2])
   │ 普通样式规则，作用在更内层
   ▼
样式链中 title 在更内层取值为 [T2]，覆盖路径 A 的外层 [T]
```

两条路径叠加后，**内层的 `set` 规则覆盖外层的显式参数**——这正是文档注释里说的「document set rules within a document override explicit arguments passed to the document element」。

**最终嵌入**：当 `compile_document` 把 body 交给 `layout_document_for_bundle` / `html_document_for_bundle` 排版时，这两个函数会新建一个 `DocumentInfo` 并调用 `info.populate(styles)`，从（已合并的）样式链里读出 title/author/... 嵌进 PDF/HTML 元数据。

用样式链的层级关系概括优先级（内层覆盖外层）：

\[ \text{最终值} = \text{resolve}(\text{链上最内层的 set document 规则，否则回退到 show\_set 注入的显式参数}) \]

#### 4.4.3 源码精读

**ShowSet 实现（本讲核心）**：

[crates/typst-library/src/model/document.rs:229-251](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/document.rs#L229-L251)：`impl ShowSet for Packed<DocumentElem>` 的 `show_set`。它新建一个空 `Styles`，然后对 `format` / `title` / `author` / `description` / `keywords` / `date` 逐个调用 `copy_into(&mut styles)`，最后返回。注释明确说明了动机、与 `page` 的一致性、hacky 性质，并指向 #6721。

**copy_into 的语义**：

[crates/typst-library/src/foundations/content/field.rs:530-538](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/foundations/content/field.rs#L530-L538)：`Settable::copy_into`——「仅当字段有值（`Some`）时，才把它 clone 进 styles」。这就是「显式参数才传播」的实现：没传的字段（`None`）不会污染样式链，保留 `set` 规则或默认值。

**realize 何时调用 show_set**：

[crates/typst-realize/src/lib.rs:558-562](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-realize/src/lib.rs#L558-L562)：realize 在处理元素时，若它实现了 `ShowSet`，就 `map.apply(show_settable.show_set(styles))`——把 show_set 返回的样式注入到当前样式 map。这正是「元素 → 样式链」反向流的入口。

**bundle 放行 set document**：

[crates/typst-realize/src/lib.rs:607-624](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-realize/src/lib.rs#L607-L624)：`visit_styled` 里对 `DocumentElem::ELEM` 样式的处理。当 `RealizationKind::Bundle` 时，既不报「not allowed inside containers」错，也对 `format` 字段放行（`set document(format:)` 在 bundle 里合法）——这给「每个 bundle 文档内部都能再 `set document(...)`」开了绿灯。对比之下，单文档导出（`RealizationKind::Document`）会调 `info.populate(local)` 把元数据收进顶层 `DocumentInfo`。

**下游消费者证据 1：#title() 读样式链**：

[crates/typst-library/src/model/title.rs:50-57](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/title.rs#L50-L57)：当 `#title()` 没给 body 时（`Smart::Auto`），它 `styles.get_cloned(DocumentElem::title)` 从样式链读标题，读不到就报「document title was not set」。这条读取能成功，前提正是 show_set（或某条 `set document`）把 title 放进了样式链。

**下游消费者证据 2：populate 嵌入元数据**：

[crates/typst-library/src/model/document.rs:349-375](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/document.rs#L349-L375)：`DocumentInfo::populate`。对每个字段先 `styles.has(...)` 判断是否被设置过，再 `styles.get_ref(...)` / `get_cloned(...)` 取解析后的值。它读的是**合并后的样式链**，所以既能看到 show_set 注入的显式参数，也能看到内层 `set` 规则（且后者优先）。

[crates/typst-layout/src/pages/mod.rs:156-158](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-layout/src/pages/mod.rs#L156-L158) 与 [crates/typst-html/src/document.rs:159-161](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-html/src/document.rs#L159-L161)：分别在 paged 与 html 的文档编译入口里调用 `info.populate(styles)` + `info.populate_locale(styles)`。这是 bundle 里每个文档元数据被最终「固化」的地方——它们读的样式链里已经包含了 show_set 注入的显式参数。

#### 4.4.4 代码实践

**实践目标**：验证「bundle 文档能从被 `include` 的子文件里拾取 `set document` 元数据」，并理解 show_set 的「hack 但必要」。

**操作步骤**：

1. 新建 `paper.typ`（子文件）：

   ```typ
   #set document(title: [My Paper], author: ("Alice",))

   = Introduction
   This is a paper written for single-document export,
   but now included in a bundle.
   ```

   这是一个「为单文档导出而写」的普通文件——顶层用 `set document(...)` 配置元数据。

2. 新建 `main.typ`（bundle 入口）：

   ```typ
   #document("paper.pdf", include "paper.typ")
   ```

   这里 `document` 元素的 `body` 是 `include "paper.typ"`，即把 `paper.typ` 的内容原样搬进来作为文档体；没有显式传 `title`。

3. 用 `--features bundle` 编译到 `out/`（**待本地验证**命令）。

4. 检查产物 `out/paper.pdf` 的元数据：
   - 若系统装了 `poppler`，运行 `pdfinfo out/paper.pdf`，查看 `Title:` 与 `Author:` 字段。
   - 或在任意 PDF 阅读器的「文档属性」里查看。

**需要观察的现象**：

- `pdfinfo` 输出里 `Title:` 应为 `My Paper`，`Author:` 应包含 `Alice`。
- 尽管 `main.typ` 里没有写 `title:`，标题仍被正确拾取——因为 `paper.typ` 里的 `set document(title: ...)` 在文档体被排版时进入了样式链，并被 `DocumentInfo::populate` 读出嵌入 PDF。

**预期结果**：这验证了文档注释里的承诺——「documents written for single-document export can be used with explicit document elements while properly retaining metadata」。也就是说，旧的、用 `set document` 写的单文档源码，可以零改动地塞进 bundle 的 `#document(...)` 里复用，元数据不丢。

**附加说明任务（show_set 为什么是「hack 但必要」）**：

阅读 [crates/typst-library/src/model/document.rs:229-251](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/document.rs#L229-L251) 的注释后，用自己的话写一段（建议 3–5 句）说明，要点应包含：

1. **必要**：`#title()` 与 `context document.title` 都是从样式链读 `DocumentElem::title`（见 [title.rs:50-57](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/title.rs#L50-L57)）。若不用 show_set 把显式 `document(title:...)` 参数写回样式链，这些读取就拿不到显式参数。
2. **hack**：它让信息流反向（元素字段 → 样式链），与「元素只读样式、不写回」的正常惯例不一致。
3. **一致**：它与 `page` 元素的做法一致，是有先例的惯用法，但团队认为整体设计值得重新审视（#6721）。

> 说明：`pdfinfo` 是否可用取决于本地是否安装 `poppler`；如未安装，可改在 `paper.typ` 体内加一行 `#context [title is: #document.title]` 来在页面上打印标题作为验证。

#### 4.4.5 小练习与答案

**练习 1**：如果同时写了 `#document("x.pdf", title: [Explicit])[ #set document(title: [FromSet]); ... ]`，最终嵌入 PDF 的标题是哪个？为什么？

**参考答案**：是 `[FromSet]`。因为显式 `title:` 经 show_set 注入到样式链的**外层**（document 元素层），而 body 内的 `set document(title:[FromSet])` 在**更内层**；样式链内层覆盖外层，`DocumentInfo::populate` 读到的解析值是 `[FromSet]`。这正是文档注释说的「document set rules within a document override explicit arguments」。

**练习 2**：`show_set` 里为什么用 `copy_into`（仅当字段有值才复制），而不是无条件地把所有字段写进样式？

**参考答案**：`copy_into` 只在字段被显式设置（`Some`）时才写回。这样，没传的字段（`None`）就不会覆盖样式链里已有的值（比如来自 `set document` 或默认值），避免「没传参数反而把别人的设置清空」的灾难。它精确地只传播「用户显式给的东西」。

**练习 3**：`AssetElem` 没有 `ShowSet`（见 4.2.3 的宏属性）。这合理吗？为什么？

**参考答案**：合理。asset 只有 `path` 和 `data` 两个纯数据字段，没有任何「需要被上下文读取」的元数据，也没有像 `#title()` 那样的下游消费者。它不需要把字段反向注入样式链，所以不必实现 `ShowSet`。这也反衬出 `DocumentElem` 实现 `ShowSet` 的唯一动机：让元数据被下游（title 元素、context、populate）读到。

---

## 5. 综合实践

把本讲四个模块串起来，搭一个接近真实的小 bundle 项目。

### 任务

在一个目录里创建如下文件结构，用 `--features bundle` 编译，并逐项验证：

```typ
// main.typ —— bundle 入口
#document("index.html", title: [站点首页])[
  #title()
  欢迎来到我的站点。去看看 #link(<report>)[研究报告]。
]

#document("report.pdf", include "report.typ")

#context {
  let hs = query(heading)
  asset("meta/stats.json", json.encode((
    heading_count: hs.len(),
    titles: hs.map(h => h.body.plain_text()),
  )))
}

#asset("assets/style.css", read("style.css"))
```

配套文件：

```typ
// report.typ
#set document(title: [Research Report], author: ("Bob",))

= Finding A
= Finding B
```

```css
/* style.css */
body { font-family: sans-serif; }
```

### 验证清单

1. **document 元素 → 文件**（4.1）：`out/` 下应出现 `index.html`、`report.pdf`。
2. **asset 元素 → 文件**（4.2）：应出现 `out/meta/stats.json`（含 `heading_count: 2`）与 `out/assets/style.css`（原样复制）。
3. **BundlePath 中间目录**（4.3）：`meta/` 与 `assets/` 子目录被自动创建；若把某路径改成 `../x` 应报「escape the bundle root」。
4. **元数据传播**（4.4）：`report.pdf` 的元数据 Title 应为 `Research Report`、Author 含 `Bob`——来自 `report.typ` 的 `set document`，经 `include` 进入文档体后被 `populate` 拾取。

### 参考答案要点

- `index.html` 与 `report.pdf` 是两个 `#document`，各自走 `compile_document` 分支（html 走 `html_document_for_bundle`，pdf 走 `layout_document_for_bundle`）。
- `meta/stats.json` 走 asset 直通：`asset.data.0` 的 `Bytes` 被原样写入（lib.rs:183-187）。
- `report.pdf` 的标题不是 `main.typ` 显式传的，而是 `report.typ` 里 `set document(title:...)` 的结果——证明 show_set + populate 链路对「include 进来的旧单文档源码」同样生效。

### 进阶（可选）

- 在 `index.html` 里用 `#context document.title` 把首页标题打印到页面上，验证显式参数 `title:[站点首页]` 经 show_set 进入了样式链（即使 body 内没有 `set document`）。
- 把 `report.pdf` 改成同时显式传 `title:[Override]` 又在 `report.typ` 里 `set document(title:...)`，观察哪个生效（应为 set 规则，见 4.4.5 练习 1）。

> 说明：本综合实践涉及多个文件与可选依赖（如查看 PDF 元数据的工具），未在本讲义环境实跑；请以本地编译结果为准，遇到与预期不符处对照源码逐段排查。

---

## 6. 本讲小结

- `DocumentElem`（`#document(...)`）在 bundle 里代表**一个输出文件**：`path` / `format` / `body` 是 bundle 专用字段，`title` / `author` / `description` / `keywords` / `date` 是通用元数据字段。在非 bundle 目标里显式构造会触发 `DOCUMENT_UNSUPPORTED_RULE`。
- `AssetElem`（`#asset(...)`）是**原始字节直通**，只有 `path` 与 `data`（`AssetData`≈`Bytes`）。它不参与排版，常与 `read`（搬运文件）或 `json.encode`（生成数据，需包在 `#context` 里）搭配。
- `BundlePath` 是「写」路径，**总是相对于 bundle 根**，与「读」用的普通 `PathOrStr` 不同；它拒绝空路径、反斜杠和 `..` 逃逸。
- 元数据传播靠 `ShowSet::show_set`：在 realize 时把 `document(...)` 的显式参数经 `copy_into` **反向**写回样式链，让 `#title()` 与 `context document.title` 能读到；body 内的 `set document` 规则更内层，会覆盖显式参数。
- 最终元数据由 `DocumentInfo::populate(styles)` 在 paged/html 文档编译入口读出并嵌入输出；这使「为单文档导出写的旧源码」可零改动 `include` 进 bundle。
- `DocumentElem` 实现 `ShowSet` 是「hack 但必要」（与 `page` 一致，见 #6721）；`AssetElem` 无此需求，故未实现。

---

## 7. 下一步学习建议

本讲讲清了「用户侧元素如何描述一个 bundle 文件」。接下来：

- **u4-l1（导出主流程与 VirtualFs）**：看 `Bundle` 里的 `BundleFile::Document` / `BundleFile::Asset` 如何被 `export()` 转成最终的「路径 → 字节」映射（`VirtualFs`）并落盘。本讲的 asset 直通将在那里被完整串联。
- **u4-l2（四种格式导出）**：深入 `export_document` 如何按 `PagedFormat` / `Html` 分派到 `*_in_bundle` 钩子，把本讲里 `compile_document` 产出的 `BundleDocument` 真正编码成字节。
- **u5-l2（跨文档链接）**：本讲多次提到 `Locatable` 与 `location()`——那里会讲这些 location 如何变成跨文档可跳转的锚点（HTML 的 DOM id、paged 的命名目的地）。

建议同时回看源码：把 [crates/typst-library/src/model/document.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/document.rs) 的文档注释（尤其是 `documents-in-bundle-export` 与 `Metadata` 两节）当作权威参考，对照本讲的理解查漏补缺。
