# 文件附件

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `attach_files()` 如何用 introspector 查询全部 `AttachElem`，再把每个元素逐字段翻译成 krilla 的 `EmbeddedFile`，最终通过 `document.embed_file()` 嵌入文档。
- 列出 `EmbeddedFile` 八个字段（`path` / `mime_type` / `description` / `association_kind` / `data` / `compress` / `location` / `modification_date`）各自的数据来源，尤其是 `path` 取自 `elem.path.derived`（解析后的虚拟根相对路径）而非原始参数。
- 讲透 `should_compress()` 的「三态启发式」：为什么只有部分 `Archive` / `Audio` / `Video` 格式返回 `Smart::Custom(false)`，而 `App` / `Doc` / `Font` / `Text` 和**整个 `Image` 类**（含 jpeg）都返回 `Smart::Auto`，以及它如何经 `Smart::custom()` 转成 krilla 的 `Option<bool>`。
- 解释为什么重复附件会报错（`embed_file` 返回 `None`），以及为什么附件的 `modification_date` 复用 `metadata::creation_date` 而非新增一个时间来源。

本讲承接 u2-l5（`convert()` 编排）与 u4-l16（`creation_date`）。附件嵌入是 `convert()` 收尾前的一步，紧跟在 `convert_pages()` 之后、`tags::resolve()` 之前。

## 2. 前置知识

在进入源码前，先建立几个概念：

- **PDF 文件附件（file attachment / EmbeddedFile）**：PDF 允许把任意外部文件「挂」在文档上。这类文件**不渲染到页面**，而是出现在 PDF 阅读器的「附件」面板里。一个典型用途是 ZUGFeRD / Factur-X 电子发票——视觉页面给人看，同时附一份机器可读的 XML/CSV 给软件解析。注意：`AttachElem` 在导出非 PDF 格式时会被忽略。
- **`infer` crate**：一个靠**魔数（magic bytes，文件头几个字节）**识别文件类型的库，不看扩展名。它返回 `Option<infer::Type>`，其中 `matcher_type()` 给出大类别（`App` / `Archive` / `Audio` / `Book` / `Doc` / `Font` / `Image` / `Text` / `Video` / `Custom`），`mime_type()` 给出具体 MIME 字符串（如 `"application/zip"`）。本讲的压缩判断完全依赖它。
- **`Smart<T>`**：Typst 的两态类型，`Auto`（交给默认策略）或 `Custom(T)`（显式指定）。本讲会用到它的 `custom(self) -> Option<T>` 方法：`Auto` → `None`，`Custom(x)` → `Some(x)`。
- **krilla**：typst-pdf 委托的底层 PDF 生成库（见 u1-l1）。本讲出现的 `krilla::embed::{EmbeddedFile, MimeType, AssociationKind}` 与 `Document::embed_file` 都是 krilla 的 API，typst-pdf 只做翻译。
- **PDF 内的压缩**：PDF 的流对象（stream）可以用 FlateDecode 等滤波器压缩。对**已经压缩过的格式**（zip、mp4、jpeg）再套一层 FlateDecode 通常既压不动、又白白消耗 CPU，因此理想做法是「跳过」；对**未知或文本类**格式则交给库默认决定。

## 3. 本讲源码地图

本讲精读一个文件，并引用三个上下文文件：

| 文件 | 作用 |
|------|------|
| [src/attach.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/attach.rs) | 本讲主角。包含 `attach_files()`（装配主流程）与 `should_compress()`（压缩启发式），共约 130 行。 |
| [crates/typst-library/src/pdf/attach.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/pdf/attach.rs) | `AttachElem` 与 `AttachedFileRelationship` 的定义（跨 crate）。typst-pdf 读取它的字段，但定义在 typst-library。 |
| [src/convert.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs) | 调用点：`convert()` 在 `convert_pages` 之后、`tags::resolve` 之前调用 `attach_files(&gc, &mut document)?`。 |
| [src/metadata.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs) | `creation_date()`（u4-l16）被复用为附件的 `modification_date`。 |

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：`attach_files()` 装配主流程、`should_compress()` 压缩启发式、字段映射与重复附件检测。

---

### 4.1 attach_files()：附件装配主流程

#### 4.1.1 概念说明

用户在 Typst 源码里写一行：

```typ
#pdf.attach(
  "experiment.csv",
  relationship: "supplement",
  mime-type: "text/csv",
  description: "Raw Oxygen readings",
)
```

排版阶段，它变成一个 `AttachElem` 元素混入文档树。导出 PDF 时，typst-pdf 的 `attach_files()` 要把这些元素「捞」出来，逐个翻译成 krilla 能理解的 `EmbeddedFile`，再嵌入 krilla 文档。

`attach_files()` 是一个纯粹的**翻译/搬运函数**：它不做排版、不碰字节级序列化，只负责把 `AttachElem` 的字段搬到 `EmbeddedFile` 的字段，并决定「要不要压缩」「是否重复」这两件事。它本身只有约 55 行。

#### 4.1.2 核心流程

`attach_files(gc, document)` 的执行步骤：

1. **查询全部附件元素**：用 introspector 查询 `AttachElem::ELEM`，得到一个扁平列表。
2. **逐元素循环**，对每个 `AttachElem`：
   1. 取 `span`（错误定位用）与 `derived_path`（解析后的虚拟根相对路径，作为附件名）。
   2. 抽取 `mime_type`：若用户给了，用 `MimeType::new()` 校验，非法就 `bail!` 报错。
   3. 抽取 `description`（可选，直接转换）。
   4. 把 `relationship`（`Option<AttachedFileRelationship>`）映射成 krilla `AssociationKind`，`None` → `Unspecified`。
   5. 把 `data`（`Bytes`）克隆进一个 `Arc<dyn AsRef<[u8]>>`。
   6. 调 `should_compress(data)` 决定压缩策略。
   7. 组装 `EmbeddedFile`（8 个字段）。
   8. 调 `document.embed_file(file)`；若返回 `None`（重复），`bail!` 报「同名附件已存在」。
3. 全部成功则返回 `Ok(())`。

伪代码：

```
fn attach_files(gc, document):
    elements = introspector.query(AttachElem)
    for elem in elements:
        path      = elem.path.derived.to_string()
        mime_type = elem.mime_type 校验后转 MimeType（非法则报错）
        desc      = elem.description
        kind      = elem.relationship 映射到 AssociationKind
        data      = Arc<elem.data.clone()>
        compress  = should_compress(data)
        file      = EmbeddedFile { path, mime_type, description, association_kind,
                                   data, compress: compress.custom(),
                                   location: span, modification_date: creation_date(gc) }
        if document.embed_file(file) is None:
            bail "attempted to attach {path} twice"
    Ok
```

#### 4.1.3 源码精读

查询语句见 [src/attach.rs:17](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/attach.rs#L17)：用 `gc.document.introspector().query(&AttachElem::ELEM.select())` 拿到所有附件。这里的 `gc.document` 是上游 `typst-layout` 产出的 `PagedDocument`。

字段抽取与 `EmbeddedFile` 组装的核心片段在 [src/attach.rs:19-64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/attach.rs#L19-L64)，关键几行：

```rust
let derived_path = &elem.path.derived;          // 解析后的虚拟根相对路径
let path = derived_path.to_string();
// ... mime_type / description / association_kind 抽取 ...
let data: Arc<dyn AsRef<[u8]> + Send + Sync> = Arc::new(elem.data.clone());
let compress = should_compress(&elem.data);

let file = EmbeddedFile {
    path,
    mime_type,
    description,
    association_kind,
    data: data.into(),
    compress: compress.custom(),                 // Smart<bool> -> Option<bool>
    location: Some(span.into_raw()),
    modification_date: metadata::creation_date(gc),
};

if document.embed_file(file).is_none() {
    bail!(span, "attempted to attach file {derived_path} twice");
}
```

几个要点：

- **`path` 用的是 `.derived` 而非原始参数**。`AttachElem.path` 的类型是 `Derived<PathOrStr, EcoString>`：第一项是用户写的原始 `PathOrStr`，第二项 `.derived` 是解析后、相对虚拟根、去掉前导斜杠的路径字符串（见 [crates/typst-library/src/pdf/attach.rs:38-46](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/pdf/attach.rs#L38-L46)）。typst-pdf 用这个规范化的字符串作为附件名，也用它构造「重复」错误信息。
- **`mime_type` 是唯一会主动报错的字段**：用户给的 MIME 必须能被 `MimeType::new` 接受，否则 `bail!(elem.span(), "invalid mime type")`。其余字段都是「给了就用，不给就 `None`」。
- **`compress.custom()` 的含义**：`Smart::custom()`（[crates/typst-library/src/foundations/auto.rs:121](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/auto.rs#L121)）把 `Smart<bool>` 折叠成 `Option<bool>` 交给 krilla——`Some(false)` 表示「明确不压缩」，`None` 表示「交给 krilla 默认策略」。详见 4.2。
- **`modification_date` 复用 `creation_date`**：`AttachElem` 没有任何时间字段，而 `metadata::creation_date(gc)` 已经实现了「`document.date` → `options.timestamp` → 不写」三级回退（u4-l16），直接复用即可，详见 4.3。

调用点在 `convert()` 中见 [src/convert.rs:86-88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L86-L88)：

```rust
convert_pages(&mut gc, &mut document)?;
attach_files(&gc, &mut document)?;          // <-- 本讲
let (doc_lang, tree) = tags::resolve(&mut gc)?;
```

它排在页面绘制之后：附件是文档级对象，与页面内容无关，但要在 `finish()` 前完成嵌入。

#### 4.1.4 代码实践

**目标**：建立「Typst 字段 → krilla `EmbeddedFile` 字段」的完整映射。

**操作步骤**：

1. 打开 [crates/typst-library/src/pdf/attach.rs:32-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/pdf/attach.rs#L32-L73)，确认 `AttachElem` 有 `path` / `data` / `relationship` / `mime_type` / `description` 五个字段。
2. 打开 [src/attach.rs:19-64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/attach.rs#L19-L64)，填出下表：

| `EmbeddedFile` 字段 | 来源（Typst 侧或函数） |
|---|---|
| `path` | ？ |
| `mime_type` | ？ |
| `description` | ？ |
| `association_kind` | ？ |
| `data` | ？ |
| `compress` | ？ |
| `location` | ？ |
| `modification_date` | ？ |

**需要观察的现象**：`modification_date` 这一行的来源**不在** `AttachElem` 的五个字段里——`AttachElem` 根本没有时间字段。它来自 `metadata::creation_date(gc)`。

**预期结果**：除 `compress`（`should_compress`）、`modification_date`（`creation_date`）、`location`（`span`）三个「外源」字段外，其余字段都与 `AttachElem` 一一对应。

**（可选动手，待本地验证）**：写一段最小 Typst 源 `#pdf.attach("readme.txt", description: "hello")`，用 typst CLI 编译为 PDF，用任意 PDF 阅读器或 `pdfdetach -list`（poppler）查看附件列表中是否出现 `readme.txt`。该运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果用户没写 `mime-type:` 参数，`EmbeddedFile.mime_type` 会是什么？会报错吗？

**答案**：是 `None`，不会报错。`mime_type` 抽取（[src/attach.rs:24-32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/attach.rs#L24-L32)）在 `Option` 上做 `map`，只有用户提供了 MIME 才会走 `MimeType::new` 校验。提供但非法（如 `"text/csv"` 之外的乱写）才会报 `"invalid mime type"`。

**练习 2**：`path` 为什么用 `.derived` 而不是用户写的原始字符串？

**答案**：原始参数可能是绝对路径、包含 `..` 或带项目根前缀，不适合直接作为 PDF 附件名；`.derived` 是规范化后的「虚拟根相对、无前导斜杠」路径（见 typst-library 的 `vpath().get_without_slash()`），更稳定，也便于做「同名重复」的去重判断。

---

### 4.2 should_compress()：基于文件类型推断的压缩启发式

#### 4.2.1 概念说明

PDF 附件的字节流可以选择是否压缩。决策有个朴素原则：**对已经压缩过的格式不要再压缩**。但难点在于「怎么知道它已经压缩过」——靠扩展名不可靠（用户可能写错或省略），所以 typst-pdf 用 `infer` crate 靠**文件头魔数**判断真实类型。

`should_compress()` 返回的不是简单的 `bool`，而是 `Smart<bool>`，对应三种意图：

| 返回值 | 含义 | `compress.custom()` | krilla 行为 |
|---|---|---|---|
| `Smart::Custom(false)` | 明确：不压缩 | `Some(false)` | 强制不压缩 |
| `Smart::Custom(true)` | 明确：要压缩 | `Some(true)` | 强制压缩 |
| `Smart::Auto` | 不表态 | `None` | 交给 krilla 默认策略 |

注意整张表里**永远不会返回 `Custom(true)`**——typst-pdf 从不强制压缩，它只对「明确不该压缩」的格式表态，其余一律交给底层默认。这是一种刻意保守的设计（见 4.2.5）。

#### 4.2.2 核心流程

`should_compress(data)` 的决策树：

1. 用 `infer::get(data)` 靠魔数识别类型；**识别不出来**（`None`）直接返回 `Smart::Auto`。
2. 按大类别 `matcher_type()` 分派，在每个类别内部再用具体 `mime_type()` 精筛：
   - `Archive`：对 zip / rar / gzip / bzip2 / 7z / xz / zstd / lz4 等**明确压缩格式**返回 `Custom(false)`，其余 `Auto`。
   - `Audio`：对 mp3(mpeg) / m4a / opus / ogg / flac / amr / aac / ape 返回 `Custom(false)`，其余 `Auto`。
   - `Video`：对 mp4 / m4v / mkv / webm / quicktime / flv 返回 `Custom(false)`，其余 `Auto`。
   - `App` / `Book` / `Doc` / `Font` / `Text` / `Custom`：整体返回 `Auto`。
   - `Image`：**两条分支都返回 `Auto`**（见下文特例）。
3. 把结果经 `Smart::custom()` 折成 `Option<bool>` 交给 krilla。

#### 4.2.3 源码精读

入口与「无法识别」分支见 [src/attach.rs:69-71](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/attach.rs#L69-L71)：

```rust
fn should_compress(data: &[u8]) -> Smart<bool> {
    let Some(ty) = infer::get(data) else { return Smart::Auto };
```

典型的「明确不压缩」分支以 `Archive` 为例，见 [src/attach.rs:73-91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/attach.rs#L73-L91)：

```rust
infer::MatcherType::Archive => match ty.mime_type() {
    "application/zip" | "application/vnd.rar" | "application/gzip"
    | "application/x-bzip2" | ... | "application/zstd" | "application/x-lz4"
    | "application/x-ole-storage" => Smart::Custom(false),
    _ => Smart::Auto,
},
```

`Video` 分支同理，见 [src/attach.rs:121-130](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/attach.rs#L121-L130)，mp4 / webm 等返回 `Custom(false)`。

**特别要注意的 `Image` 特例**，见 [src/attach.rs:107-119](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/attach.rs#L107-L119)：

```rust
infer::MatcherType::Image => match ty.mime_type() {
    "image/jpeg" | "image/jp2" | "image/png" | "image/webp"
    | "image/vnd.ms-photo" | "image/heif" | "image/avif" | "image/jxl"
    | "image/vnd.djvu" => Smart::Auto,
    _ => Smart::Auto,
},
```

这里两条 `match` 臂**都返回 `Smart::Auto`**——也就是说，jpeg、png、webp 这些已压缩图像，typst-pdf **并不**强制它们「不压缩」，而是把决策权完全交给 krilla。这与「直觉上已压缩格式都该 `Custom(false)`」不同，是阅读源码时最值得留意的一点。

#### 4.2.4 代码实践

**目标**：亲手追踪多种文件类型在 `should_compress` 中的走向，验证三态输出。

**操作步骤**：

阅读 [src/attach.rs:69-133](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/attach.rs#L69-L133)，对下表每个文件类型，填出：`matcher_type()` → 命中分支 → 返回值 → `compress.custom()`（`Some(false)` 还是 `None`）。

| 文件 | matcher_type | 返回值 | `compress.custom()` | krilla 是否被强制「不压缩」 |
|---|---|---|---|---|
| `data.zip` | Archive | `Custom(false)` | `Some(false)` | 是 |
| `video.mp4` | Video | ？ | ？ | ？ |
| `photo.jpeg` | Image | ？ | ？ | ？ |
| `note.txt` | Text | ？ | ？ | ？ |
| 无法识别的字节流 | （None） | ？ | ？ | ？ |

**需要观察的现象**：重点看 `photo.jpeg` 这一行——尽管 jpeg 是「已压缩格式」，它在 `Image` 分支里走的是返回 `Smart::Auto` 的臂，因此 `compress.custom()` 是 `None`，krilla **并未**被强制「不压缩」。这与 `data.zip`（`Some(false)`，明确不压缩）形成对比。

**预期结果**：只有 zip/mp4 这类命中 `Archive`/`Audio`/`Video` 白名单的格式得到 `Some(false)`；jpeg、txt、未知格式都是 `None`（交给 krilla 默认）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `should_compress` 对 zip/mp4 返回 `Custom(false)`，对未知格式返回 `Auto`，而对 jpeg 也返回 `Auto`（而非 `Custom(false)`）？

**答案**：设计意图是「**只对明确不该压缩、且能稳定识别的格式做强制判断，其余交给 krilla 默认**」。zip/mp4 等压缩/媒体容器再套 FlateDecode 几乎压不动且浪费 CPU，所以明确表态 `Custom(false)`；未知格式因为 `infer` 无法识别，不敢贸然判断，返回 `Auto` 让底层决定。jpeg 虽是已压缩格式，但 typst-pdf 选择**不**在 typst-pdf 这一层维护图像压缩判断（`Image` 两条臂都返回 `Auto`），把图像附件的压缩决策完全留给 krilla——这样 typst-pdf 不必紧跟 `infer` 的图像类型增减而频繁更新。

**练习 2**：整张决策表里，`should_compress` 是否可能返回 `Smart::Custom(true)`？

**答案**：不可能。代码里没有任何一处返回 `Custom(true)`。typst-pdf 只负责「喊停」（对已知压缩格式），从不「喊压」，是否压缩的默认决策权始终在 krilla。

---

### 4.3 字段映射与重复附件检测

#### 4.3.1 概念说明

本模块聚焦三个容易被忽略的细节：

1. **`relationship` → `AssociationKind` 的映射**：用户的 `relationship` 是 `Option<AttachedFileRelationship>`（四种关系 + 可空），要映射到 krilla 的 `AssociationKind`（多一个 `Unspecified`）。
2. **重复附件检测**：同名附件第二次嵌入会失败，typst-pdf 把它翻译成可读错误。
3. **`modification_date` 复用 `creation_date`**：附件没有独立的时间来源，直接借用文档创建日期的逻辑。

关于 `relationship`，typst-library 对用户有句注释：「Ignored if export doesn't target PDF/A-3」（见 [crates/typst-library/src/pdf/attach.rs:63-66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/pdf/attach.rs#L63-L66)）。这指的是：**关联类型（Associated Files 关系）只有 PDF/A-3 归档标准会真正使用**它做校验；但 typst-pdf 无论目标标准如何，都会把 `association_kind` 写进 `EmbeddedFile`，是否被消费取决于下游校验器。另有重要限制：**PDF/A-2 目前不支持文件附件**（见 typst-library 文档注释）。

#### 4.3.2 核心流程

三件事的执行点：

1. **`association_kind` 映射**：`None` → `Unspecified`；四个变体一一对应 `Source` / `Data` / `Alternative` / `Supplement`。
2. **重复检测**：`document.embed_file(file)` 返回 `Option`；返回 `None` 即判定为「该路径已存在」，`bail!` 报 `attempted to attach file {derived_path} twice`。
3. **`modification_date`**：直接调用 `metadata::creation_date(gc)`（[src/metadata.rs:59-99](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs#L59-L99)），复用其三级回退。

#### 4.3.3 源码精读

`relationship` 映射见 [src/attach.rs:38-46](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/attach.rs#L38-L46)：

```rust
let association_kind = match elem.relationship.get(StyleChain::default()) {
    None => AssociationKind::Unspecified,
    Some(e) => match e {
        AttachedFileRelationship::Source => AssociationKind::Source,
        AttachedFileRelationship::Data => AssociationKind::Data,
        AttachedFileRelationship::Alternative => AssociationKind::Alternative,
        AttachedFileRelationship::Supplement => AssociationKind::Supplement,
    },
};
```

这是一一对应、外加 `None → Unspecified`，没有信息损失。`AttachedFileRelationship` 的四个变体含义见 [crates/typst-library/src/pdf/attach.rs:75-86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/pdf/attach.rs#L75-L86)：`Source`（PDF 由该源文件生成）、`Data`（用于派生 PDF 视觉）、`Alternative`（文档的替代表示）、`Supplement`（补充资源）。

重复检测见 [src/attach.rs:61-63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/attach.rs#L61-L63)：

```rust
if document.embed_file(file).is_none() {
    bail!(span, "attempted to attach file {derived_path} twice");
}
```

`embed_file` 返回 `None` 表示该路径（即 `derived_path`）已存在附件，krilla 拒绝重复嵌入；typst-pdf 把它翻译成带 span 的 Typst 诊断，错误信息里用规范化路径 `derived_path` 方便用户定位。

`modification_date` 见 [src/attach.rs:58](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/attach.rs#L58)，调用 `metadata::creation_date(gc)`。复用的理由有三：

1. `AttachElem` **没有任何时间字段**——Typst 源码里无法为单个附件指定修改时间。
2. `creation_date` 已经实现了完善的「`document.date` → `options.timestamp` → 不写」三级回退（u4-l16），表达「这份 PDF（及其附件）是何时产出的」。
3. 附件字节是在导出当下才读取/嵌入的，最合理的「时间戳」正是文档产出时间，而非某个不存在的「原始文件 mtime」。

#### 4.3.4 代码实践

**目标**：理解重复检测触发条件与 `relationship` 的可见性。

**操作步骤**：

1. 阅读 [src/attach.rs:61-63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/attach.rs#L61-L63)，回答：去重的「键」是什么？（提示：看 `derived_path`。）
2. 阅读后回答：下面两段 Typst 源，哪段会触发「attach twice」错误？

   ```typ
   // A
   #pdf.attach("data/a.csv")
   #pdf.attach("data/a.csv")
   // B
   #pdf.attach("data/a.csv")
   #pdf.attach("data/b.csv")
   ```

**需要观察的现象 / 预期结果**：A 段两次 `derived_path` 都是 `data/a.csv`，第二次 `embed_file` 返回 `None`，报 `attempted to attach file data/a.csv twice`；B 段路径不同，不报错。这说明去重**只看规范化路径，不看文件内容**——即使两个 `a.csv` 内容不同，只要路径相同就判重复。

**（可选动手，待本地验证）**：构造 A 段源码用 typst 编译，预期编译失败并打印上述错误；该运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`relationship: none`（即 `Option::None`）时，`association_kind` 是什么？

**答案**：`AssociationKind::Unspecified`（[src/attach.rs:39](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/attach.rs#L39)）。

**练习 2**：为什么附件的 `modification_date` 复用 `creation_date`，而不是引入一个新的「附件修改时间」？

**答案**：因为 `AttachElem` 不携带任何时间信息，Typst 没有给单个附件标注修改时间的机制；而 `creation_date` 的三级回退恰好表达「这份产物何时产出」，附件是产物的一部分，复用既避免重复实现，也符合语义。

**练习 3**：如果导出目标是 PDF/A-2，附件会发生什么？

**答案**：根据 typst-library 的文档注释，PDF/A-2 **当前不支持**文件附件。`attach_files` 本身仍会尝试嵌入，但具体是否在 PDF/A-2 校验阶段被拒绝，取决于 krilla 的校验器配置（与 u1-l3 的 `PdfStandards` 校验器相关）；该交互的精确报错行为待结合 krilla 校验逻辑确认。

---

## 5. 综合实践

**任务**：为一个带三种附件的 Typst 文档，预测每个附件在 PDF 中的压缩策略、关联类型与修改日期来源，并构造一个「重复附件」的反例。

**背景源码**：用户写了：

```typ
#pdf.attach("report.zip", relationship: "supplement")
#pdf.attach("clip.mp4", relationship: "data")
#pdf.attach("photo.jpeg")
```

**操作步骤**：

1. 填表预测（基于 [src/attach.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/attach.rs) 与 [crates/typst-library/src/pdf/attach.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/pdf/attach.rs)）：

   | 附件 | `matcher_type` | `should_compress` 返回 | `compress`（`Option<bool>`） | `association_kind` | `modification_date` 来源 |
   |---|---|---|---|---|---|
   | report.zip | ？ | ？ | ？ | ？ | ？ |
   | clip.mp4 | ？ | ？ | ？ | ？ | ？ |
   | photo.jpeg | ？ | ？ | ？ | ？ | ？ |

2. **关键预测点**：`photo.jpeg` 与 `report.zip` 的 `compress` 列应**不同**——zip 是 `Some(false)`，jpeg 是 `None`（交给 krilla）。三者的 `modification_date` 来源**相同**，都是 `metadata::creation_date(gc)`。

3. **反例**：在文档里再加一行 `#pdf.attach("report.zip")`，预测编译结果。

**预期结果**：第 3 步会因 `report.zip` 重复，`embed_file` 返回 `None`，触发 `attempted to attach file report.zip twice` 错误。

**（可选动手，待本地验证）**：用 typst CLI 编译上述文档，用 `pdfdetach -list`（poppler）核对附件名；用工具检查 zip 附件的流是否未压缩、jpeg 附件的压缩状态。运行结果待本地验证。

## 6. 本讲小结

- `attach_files()` 用 introspector 查询全部 `AttachElem`，逐个翻译成 krilla `EmbeddedFile` 并 `embed_file` 嵌入；它是纯翻译/搬运函数，约 55 行，无排版逻辑。
- `EmbeddedFile` 八个字段中，`path` 取自规范化的 `elem.path.derived`，`compress` 来自 `should_compress`，`location` 来自 `span`，`modification_date` 复用 `metadata::creation_date`，其余与 `AttachElem` 一一对应；`mime_type` 是唯一会主动报错（非法 MIME）的字段。
- `should_compress` 返回 `Smart<bool>`，但**只返回 `Custom(false)` 或 `Auto`**：对 `Archive`/`Audio`/`Video` 的部分已压缩格式明确「不压缩」，其余（含**整个 `Image` 类、jpeg**）一律 `Auto` 交给 krilla；经 `Smart::custom()` 折成 `Option<bool>` 后驱动 krilla。
- `relationship`（`Option`）一一映射到 krilla `AssociationKind`，`None → Unspecified`；该关联关系主要在 PDF/A-3 下被校验器消费，且 PDF/A-2 当前不支持附件。
- 重复附件以**规范化路径**为键去重：`embed_file` 返回 `None` 即 `bail!` 报 `attempted to attach file {path} twice`，只比路径不比内容。
- `attach_files` 在 `convert()` 中位于 `convert_pages` 之后、`tags::resolve` 之前，是文档级（非页面级）对象。

## 7. 下一步学习建议

- **u5-l18（错误处理与校验映射）**：本讲的 `bail!`（非法 MIME、重复附件）是 typst-pdf 主动产生的 Typst 诊断；u5-l18 讲的是另一类——把 krilla 的 `KrillaError`/`ValidationError` 翻译回带 span 的 `SourceDiagnostic`。两者构成 typst-pdf 的错误处理全貌。
- **u1-l3（PDF 标准与校验配置）**：`relationship` 是否生效、PDF/A-2 是否允许附件，都取决于 `PdfStandards` 装配的校验器。可回头对照 `accessibility_validator()` 与归档校验器的启用条件。
- **继续阅读**：[src/attach.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/attach.rs) 全文（仅约 130 行）与 [crates/typst-library/src/pdf/attach.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/pdf/attach.rs) 的 `AttachElem` 定义，可完整掌握附件从用户语法到 PDF 嵌入的全链路。
