# convert() 编排：从 PagedDocument 到 PDF 字节

> 永久链接 base：`https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/`
> 下文所有源码链接均拼接在该 base 之后，例如 `src/convert.rs#L48-L95`。

## 1. 本讲目标

在 u1-l4 里，我们画过一张「以 `convert()` 为中心的 13 步流水线」草图，但当时只标了**箭头与模块名**，没有走进任何一间屋子。本讲把镜头推进到 `convert()` **函数内部**，逐行拆开这条主线：

- `convert()` 的函数签名如何承上启下（吃进什么、吐出什么）。
- 9 个字段的 `SerializeSettings` 是如何由 `PdfOptions` 翻译出来的——哪些是「选项驱动」，哪些是「写死的策略」。
- `GlobalContext` 这只「大背包」里 10 个字段各自缓存什么、为什么需要它。
- 为什么阶段顺序**必须**是这个顺序——即各阶段之间的依赖链（尤其是 `tags::resolve` 产出的 `doc_lang`/`tree` 如何被 `set_metadata`/`set_tag_tree` 消费）。
- `finish()` 如何收尾并把 krilla 的错误「翻译」成带 span 的 Typst 诊断。

学完后，你应该能回答三个问题：① 改一个 `PdfOptions` 字段会落到 `SerializeSettings` 的哪一项、进而影响 PDF 的什么；② `GlobalContext` 里每个字段被谁读、被谁写；③ 若把 `convert()` 的某两个阶段**对调**，会在哪里崩。

> 本讲不重复 u1-l4 的「模块地图」，也不深入 `handle_frame` 的递归细节（u2-l7）、页面尺寸/bleed（u2-l6）、类型转换（u2-l8）和完整的 `ValidationError` 映射（u5-l18）。本讲只聚焦 `convert()` 这一个函数的**编排机制**。

## 2. 前置知识

### 2.1 编排者模式（orchestrator）与状态容器

`convert()` 用的是**编排者模式**：它自己几乎不画线、不嵌字、不上色，而是按固定顺序调用一组子模块。为了让这些子模块在多次调用间**共享状态**（比如「这个字体我已经转过一次了，别再转」），编排者通常会持有一个**贯穿全程的上下文对象**——本 crate 里它叫 `GlobalContext`。理解 `convert()`，一半是理解「顺序」，另一半就是理解 `GlobalContext` 这只背包里装了什么、谁在读写它。

### 2.2 krilla 的 builder 风格 API

`typst-pdf` 不自己拼 PDF 字节，而是驱动 `krilla` 库。krilla 的 `Document` 是 **builder 风格**：

1. `Document::new_with(settings)` —— 用一份序列化设置创建一个**空**文档。
2. `document.start_page_with(page_settings)` —— 开始一页，返回 `Page`；在 `Page` 上拿 `surface` 绘制，`surface.finish()` 结束本页。
3. `document.set_outline(...)` / `set_metadata(...)` / `set_tag_tree(...)` —— 挂上文档级属性。
4. `document.finish()` —— 真正序列化，返回 `Result<Vec<u8>, KrillaError>`。

`convert()` 的整体形状完全对应这套 builder：先建空文档，逐页绘制，挂文档属性，最后 `finish()`。

### 2.3 `?` 与 `SourceResult`

`convert()` 的返回类型是 `SourceResult<Vec<u8>>`，即 `Result<Vec<u8>, EcoVec<SourceDiagnostic>>`——成功是字节，失败是**一组带 span 的诊断**。函数体内大量 `?`：任何一步失败都直接把错误向上抛。理解「哪一步带 `?`」就是理解「哪一步可能失败」。

### 2.4 承接前几讲的关键结论

- 公共入口 `pdf()` / `pdf_in_bundle()` 都是对 `convert::convert()` 的一行委托；区别只在 `anchors`（命名目的地址）与 `link_resolver`（跨文档链接解析器）（见 u1-l2）。
- `PdfOptions` 有 7 个字段：`ident` / `creator` / `timestamp` / `page_ranges` / `standards` / `tagged` / `pretty`（见 u1-l2）。
- `standards.config` 是一个 krilla `Configuration`，封装了 PDF 版本号与若干校验器（见 u1-l3）。
- `convert()` 是一条「设置→建文档→页号→命名目的地→tags init→全局上下文→转页面→附件→tags resolve→大纲/元数据/结构树→finish」的主线（见 u1-l4）。本讲把这条主线**展开**。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用它来 |
| --- | --- | --- |
| `src/convert.rs` | 编排核心，`convert()` / `convert_pages()` / `finish()` / `GlobalContext` 全在此 | 精读主线的每一行 |
| `src/lib.rs` | `PdfOptions` 定义与默认值 | 确认 `SerializeSettings` 的「选项来源」 |
| `src/tags/mod.rs` | tags 子系统入口 `init()` / `resolve()` 的导出 | 确认这两个调用在主线两端的行为 |

> 说明：`tags::resolve` 的真正实现在 `src/tags/resolve/mod.rs`，本讲只用到它的**签名**（返回 `(Option<Locale>, TagTree)`）；其内部机制留待 u5-l23。

## 4. 核心概念与源码讲解

### 4.1 convert()：总体结构与四阶段划分

#### 4.1.1 概念说明

`convert()` 是整个 crate 唯一的「后厨入口」。`lib.rs` 里两个公共函数都只是把参数原样转交给它：

- `pdf(doc, options)` → `convert(doc, options, &[], None)`（独立导出：无锚点、无跨文档解析器）。
- `pdf_in_bundle(doc, options, anchors, link_resolver)` → `convert(doc, options, anchors, Some(link_resolver))`（打包导出）。

也就是说，**所有 PDF 导出最终都流经这一个函数**。它的职责是把一份 `PagedDocument`（一棵排好版的 `Frame` 树）编排成对 krilla 的调用序列，最终产出 `Vec<u8>`。

为了便于理解，可以把 `convert()` 的 ~40 行主体划分为**四个阶段**：

| 阶段 | 行数区间 | 干什么 | 是否可能失败 |
| --- | --- | --- | --- |
| ① 准备 | 序列化设置 + 建空文档 + 页号转换 + 命名目的地 + tags init | 把 `PdfOptions` 翻译成 krilla 设置，准备一只「空壳文档」和后续要用的全局映射 | tags init 可能 `bail!` |
| ② 转换 | `GlobalContext::new` + `convert_pages` | 组装全局上下文，逐页把 `Frame` 树翻译成 krilla 绘制（这里才真正分发到各翻译器） | 是（`?`） |
| ③ 收集 | `attach_files` + `tags::resolve` | 附件；把遍历期间累积的结构标记解析成 `TagTree`，并得到文档语言 | 是（`?`） |
| ④ 收尾 | `set_outline` / `set_metadata` / `set_tag_tree` + `finish` | 挂文档级属性，序列化出字节，并翻译 krilla 错误 | 是（`?`） |

注意：阶段 ②③④ 里的「内容翻译」（text/shape/image/link）发生在 `convert_pages` 内部，但本讲不展开它们的实现，只关注 `convert()` 如何把它们**编排**进来。

#### 4.1.2 核心流程

`convert()` 的四阶段俯视图：

```text
convert(typst_document, options, anchors, link_resolver) -> SourceResult<Vec<u8>>
  │
  ├─ ① 准备
  │     ├─ SerializeSettings { ... }          ← 由 options 翻译而来
  │     ├─ Document::new_with(settings)       ← 空文档
  │     ├─ PageIndexConverter::new(...)       ← page_ranges 页号重映射
  │     ├─ collect_named_destinations(...)    ← 书签/锚点 → 命名目的地（同时写进文档）
  │     └─ tags::init(...) ?                  ← 预构建逻辑树（或空树）；可能 bail!
  │
  ├─ ② 转换
  │     ├─ GlobalContext::new(...)            ← 把上面所有产物塞进背包
  │     └─ convert_pages(&mut gc, &mut doc) ? ← 逐页绘制 + tags 钩子
  │
  ├─ ③ 收集
  │     ├─ attach_files(&gc, &mut doc) ?      ← 文件附件
  │     └─ tags::resolve(&mut gc) ?           ← → (doc_lang, tree)
  │
  ├─ ④ 收尾
  │     ├─ doc.set_outline(build_outline(&gc))
  │     ├─ doc.set_metadata(build_metadata(&gc, doc_lang))   ← 消费 doc_lang
  │     ├─ doc.set_tag_tree(tree)             ← 消费 tree
  │     └─ finish(doc, gc, config) ?          ← krilla finish() → Vec<u8>
```

一个**贯穿全讲的观察**：阶段 ③ 的 `tags::resolve` 同时产出 `doc_lang` 和 `tree`，而阶段 ④ 的 `set_metadata` 消费 `doc_lang`、`set_tag_tree` 消费 `tree`——所以**阶段顺序是被数据依赖锁死的**，不是随便排的（详见 4.4）。

#### 4.1.3 源码精读

`convert()` 的完整签名与主体：

[src/convert.rs:L47-L95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L47-L95) —— 整个 crate 的总入口。注意 L47 的 `#[typst_macros::time(name = "convert document")]`：这是 Typst 内部的编译计时注解，让 `convert()` 的耗时出现在编译诊断里，不影响逻辑。

签名里有四个参数，正好对应「输入 + 配置 + 两种导出模式」：

```rust
pub fn convert(
    typst_document: &PagedDocument,                      // 输入：排好版的文档
    options: &PdfOptions,                                // 配置：导出选项
    anchors: &[(Location, EcoString)],                   // 命名目的地址（独立导出为空）
    link_resolver: Option<Tracked<LateLinkResolver>>,    // 跨文档链接解析器（独立为 None）
) -> SourceResult<Vec<u8>>
```

`anchors` 与 `link_resolver` 这两个参数的存在，就是 `pdf()` 和 `pdf_in_bundle()` 能共用同一个 `convert()` 的关键（见 u1-l2）。

接下来 4.2~4.5 会逐块精读主体里的每一行；这里先记住：**函数体只有 ~40 行，但每一行都对应一个子系统的入口**。

#### 4.1.4 代码实践

**实践：从签名反推「谁调用谁」。**

1. 实践目标：在阅读函数体之前，先从参数与返回类型推断 `convert()` 的角色。
2. 操作步骤：
   - 打开 [src/convert.rs:L48-L53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L48-L53)。
   - 对四个参数各写一句：谁提供它、它会被传给后续哪个子系统（提示：`anchors` 流向 `collect_named_destinations`；`link_resolver` 进 `GlobalContext`；`options` 几乎处处被读）。
   - 再看返回类型 `SourceResult<Vec<u8>>`，回答：成功和失败分别长什么样？
3. 需要观察的现象：四个参数里，只有 `typst_document` 和 `options` 是两种导出模式都必填的；`anchors`/`link_resolver` 是「打包模式」专用的可选输入。
4. 预期结果：你能解释为什么 `pdf()` 传 `&[]` 和 `None` 而 `pdf_in_bundle()` 传实参——因为独立导出既不需要被别的文档链接进来（无锚点），也不需要链接出去（无跨文档解析）。

#### 4.1.5 小练习与答案

**练习 1**：`convert()` 为什么不直接接收 `&PdfOptions` 的所有权，而是用引用 `&PdfOptions`？
**答案**：因为 `PdfOptions` 是调用方（`pdf()`/`pdf_in_bundle()`，最终是 Typst 编译器）持有的配置，`convert()` 只是「借用」它来读取。用引用既避免了克隆，也让 `PdfOptions` 的生命周期可以长于单次导出（例如增量编译复用）。

**练习 2**：L47 的 `#[typst_macros::time(...)]` 加在 `convert()` 上、L327 的同名注解加在 `handle_frame` 上、L433 的加在 `finish` 上。这说明什么？
**答案**：说明 Typst 关心这三处的耗时——`convert()`（整体转换）、`handle_frame`（逐帧/逐页）、`finish`（最终序列化）。它们是导出链路上最可能成为性能热点的位置，因此被单独计时，方便在编译诊断里定位慢点。

---

### 4.2 SerializeSettings：把 PdfOptions 翻译成 krilla 序列化设置

#### 4.2.1 概念说明

`SerializeSettings` 是 krilla 定义的结构体，用来控制「最终 PDF 字节**长什么样**」——压缩与否、是否写 XMP、是否写结构树、版本号多少等。`convert()` 的第一件事，就是把 Typst 自己的 `PdfOptions` **翻译**成 krilla 能懂的 `SerializeSettings`。

这个翻译是 `convert()` 里最「直白」的一段：9 个字段里，有 5 个直接来自 `options.*`，4 个是**写死的策略**。看懂这张映射表，就能预测「改某个 `PdfOptions` 字段会怎样影响输出 PDF」。

#### 4.2.2 核心流程

`SerializeSettings` 的 9 字段对照表：

| 字段 | 取值 | 来源 | 含义 |
| --- | --- | --- | --- |
| `compress_content_streams` | `!options.pretty` | 选项 | 是否压缩内容流；`pretty` 时**不**压缩以便人读 |
| `ascii_compatible` | `options.pretty` | 选项 | 是否只用 ASCII 字符；`pretty` 时为真，便于阅读 |
| `pretty` | `options.pretty` | 选项 | 整体是否人类可读格式化 |
| `enable_tagging` | `options.tagged` | 选项 | 是否写无障碍结构树 |
| `configuration` | `options.standards.config` | 选项 | PDF 版本 + 校验器（见 u1-l3） |
| `no_device_cs` | `true` | 写死 | 不使用设备色彩空间（DeviceGray/RGB/CMYK） |
| `xmp_metadata` | `true` | 写死 | 始终写 XMP 元数据 |
| `cmyk_profile` | `None` | 写死 | 暂不提供 CMYK ICC 配置 |
| `render_svg_glyph_fn` | `render_svg_glyph` | 写死 | krilla-svg 提供的 SVG 字形渲染函数 |

可以把它归纳为两条规律：

1. **选项驱动**（5 项）：用户可调的 `pretty` / `tagged` / `standards` 决定了压缩、可读性、结构树、版本校验。
2. **写死策略**（4 项）：`no_device_cs`、`xmp_metadata`、`cmyk_profile`、`render_svg_glyph_fn` 不暴露给用户，反映的是 `typst-pdf` 当前固定的设计取舍。

> ⚠️ 关于 `no_device_cs` 与 `cmyk_profile` 的精确 krilla 语义，超出本 crate 可见范围，上表「含义」列是据字段名与 PDF 语义的合理推断。可确定的事实是：它们被硬编码为 `true`/`None`，与 `options` 无关；`cmyk_profile: None` 正是后续 `finish()` 里 `MissingCMYKProfile` 校验错误（4.5.3）的根源。

#### 4.2.3 源码精读

整段 `SerializeSettings` 构造：

[src/convert.rs:L54-L64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L54-L64) —— 9 个字段，逐字对应上表。

几个值得圈出的点：

- **`pretty` 一处开关、三处生效**：同一个 `options.pretty` 同时驱动 `compress_content_streams`（取反）、`ascii_compatible`（同向）、`pretty`（同向）。所以「美化输出」不是单个布尔，而是一组协同的格式化策略。
- **`tagged` 与 `standards` 是两件事**：`enable_tagging: options.tagged` 控制是否**写**结构树；而 `configuration: options.standards.config` 里可能含 PDF/UA 校验器，控制是否**校验**结构树。写 ≠ 校验（详见 u1-l3、u5-l19）。
- **`render_svg_glyph_fn`**：这行能把 SVG 字形（Typst 对彩色 emoji 等的处理）渲染能力接进 krilla，对应的函数来自 `use krilla_svg::render_svg_glyph;`（见 [src/convert.rs:L17](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L17)）。

构造完 `settings` 后，立刻用它建空文档：

[src/convert.rs:L66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L66) —— `let mut document = Document::new_with(settings);`。从此 `document` 就是后续所有「写入」的目标。

`PdfOptions` 的默认值（`pretty: false`、`tagged: true`、`standards` = PDF 1.7 无校验器）来自：

[src/lib.rs:L99-L111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L99-L111) —— `impl Default for PdfOptions`，决定了「什么都不配」时的 `SerializeSettings` 长什么样。

#### 4.2.4 代码实践

**实践：预测 `pretty` 与 `tagged` 对输出字节的影响。**

1. 实践目标：把抽象的选项映射，变成「可观察的输出差异」预判。
2. 操作步骤：
   - 对照 4.2.2 的映射表，填一张「输入选项 → `SerializeSettings` 字段 → PDF 表现」的推理表。例如：`pretty=true` → `compress_content_streams=false`、`ascii_compatible=true` → 内容流未压缩、可读，体积更大。
   - 再推理：把 `tagged` 从默认 `true` 改成 `false`，`enable_tagging` 变成什么？输出 PDF 会缺少哪一块（提示：结构树，`document.set_tag_tree` 仍会被调用，但树会是空的）？
3. 需要观察的现象：哪些选项影响**体积/可读性**（pretty），哪些影响**可访问性/合规性**（tagged、standards）。
4. 预期结果：你能说出「减小体积」与「保留无障碍」往往是**互斥**的取舍——这正是 `PdfOptions::tagged` 字段文档里提到的「为减小文档体积，可酌情关闭 tagged PDF」（见 [src/lib.rs:L82-L88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L82-L88)）。
5. `待本地验证`：若有 Typst CLI，可用同一份 `.typ` 文档分别以默认与美化/关闭 tagging 导出，对比文件大小。

#### 4.2.5 小练习与答案

**练习 1**：`compress_content_streams: !options.pretty` 为什么要把 `pretty` **取反**？
**答案**：因为「美化（可读）」与「压缩」是矛盾的：要让人能读懂内容流，就不能压缩它。所以 `pretty` 为真时压缩关闭（`!true == false`）。同一个 `pretty` 值在这里被「语义取反」使用。

**练习 2**：`cmyk_profile: None` 被写死，这意味着 CMYK 颜色在当前导出模式下会怎样？
**答案**：krilla 在校验时若发现 CMYK 颜色却无 ICC 配置，会抛出 `ValidationError::MissingCMYKProfile`。这个错误最终在 `finish()`/`convert_error()` 里被翻译成「the PDF is missing a CMYK profile / CMYK colors are not yet supported in this export mode」（见 4.5.3 与 [src/convert.rs:L587-L591](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L587-L591)）。所以这是「功能尚未完整支持」的体现，而非可配置项。

**练习 3**：为什么 `enable_tagging` 与 `configuration` 要分两个字段，而不是都塞进 `configuration`？
**答案**：`configuration`（来自 `standards`）表达的是「目标 PDF 标准与版本/校验」，是一个**合规目标**；`enable_tagging` 表达的是「是否真的去写结构树」，是一个**输出开关**。两者正交：可以「写结构树但不校验」（默认：tagged=true、无 PDF/UA），也可以「校验结构树则必然要写」。分开字段让这两种控制各司其职（见 u1-l3）。

---

### 4.3 GlobalContext：贯穿全程的全局状态容器

#### 4.3.1 概念说明

`convert()` 是一条很长的主线，但真正干活的子模块（`text`/`shape`/`image`/`link`/`tags`…）是在 `convert_pages` 的递归遍历里被**反复**调用的。这些子模块之间需要共享大量状态——最典型的就是「同一个字体，别转第二次」。如果每次都现算，既慢又可能不一致。

解法是**一个贯穿全程的可变上下文对象**：`GlobalContext`（简称 `gc`）。它在阶段 ② 一开始被 `new` 出来，之后作为 `&mut gc` 在各子模块间传来传去，**所有跨调用、跨页面的共享状态都挂在它身上**。理解 `convert()`，很大程度上就是理解「`gc` 里装了什么、谁读谁写」。

#### 4.3.2 核心流程

`GlobalContext` 的 10 个字段可以分成三类：

```text
GlobalContext
├─ 【引用类】指向「外部输入」，只读
│     ├─ document: &PagedDocument        ← 输入文档（供 introspect 标题、页面等）
│     ├─ options: &PdfOptions            ← 导出选项
│     └─ link_resolver: Option<Tracked<LateLinkResolver>>  ← 跨文档链接解析（打包模式）
│
├─ 【缓存/映射类】随转换进行而累积，被各翻译器读写
│     ├─ fonts_forward:  FontInstance -> krilla Font    ← 正向缓存（防重复转字体）
│     ├─ fonts_backward: krilla Font -> FontInstance    ← 反向映射（错误定位用）
│     ├─ image_to_spans: krilla Image -> Span           ← 某图像首次出现的 span
│     ├─ image_spans:    HashSet<Span>                  ← 所有含图像的 span 集合
│     └─ loc_to_names:   Location -> NamedDestination   ← 文档内位置 → 命名目的地
│
└─ 【子系统类】专门的状态机/转换器
      ├─ page_index_converter: PageIndexConverter  ← 页号重映射（page_ranges）
      └─ tags: Tags                               ← tagged PDF 子系统的运行态
```

**读写关系**（这对理解全局至关重要）：

| 字段 | 谁写 | 谁读 |
| --- | --- | --- |
| `fonts_forward`/`fonts_backward` | `text::convert_font`（转字体时） | `text`（查缓存）；`finish`（反向映射定位字体错误） |
| `image_to_spans` | `image::handle_image`（遇到图像时） | `finish`（`SixteenBitImage` 错误找 span） |
| `image_spans` | `image::handle_image` | `finish`（`Transparency` 错误判断是否图像） |
| `loc_to_names` | `collect_named_destinations`（阶段①）→ 经 `GlobalContext::new` 注入 | `link`（文档内链接回退查找） |
| `page_index_converter` | `PageIndexConverter::new`（阶段①）→ 注入 | `page`、`link`、`outline`（页号映射） |
| `tags` | `tags::init`（阶段①）→ 注入；遍历中持续更新 | 各翻译器的 tags 钩子；`tags::resolve`（阶段③） |
| `document`/`options`/`link_resolver` | 构造时注入（只读引用） | 几乎所有子模块 |

注意 `loc_to_names`、`page_index_converter`、`tags` 这三项是**先在阶段①各自构造好，再在阶段②的 `GlobalContext::new` 里一次性注入**的——这就是为什么 `GlobalContext::new` 要排在它们之后（详见 4.4）。

#### 4.3.3 源码精读

`GlobalContext` 结构体定义（10 个字段，每个都有文档注释）：

[src/convert.rs:L277-L301](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L277-L301) —— 字段一览。其中字体字段注释（L279）点明「前向+反向」双向缓存的用途；图像字段注释（L283-L289）特别说明：同一张图可能出现多次，`image_to_spans` 只存**首次**出现的 span。

`GlobalContext::new` 的构造：

[src/convert.rs:L303-L325](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L303-L325) —— 注意它接收 6 个参数（`document`/`options`/`link_resolver`/`loc_to_names`/`page_index_converter`/`tags`），把阶段①准备好的产物**注入**背包；而 4 个缓存映射（字体前/反向、`image_to_spans`、`image_spans`）都用 `::default()` 初始化为空，留待后续翻译器在遍历中填充。

`GlobalContext::new` 在 `convert()` 中的调用点：

[src/convert.rs:L77-L84](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L77-L84) —— 把 `typst_document`、`options`、`link_resolver`、`named_destinations`、`page_index_converter`、`tags` 六个值一次性塞进 `gc`。

**双向字体缓存的妙处**——为什么字体需要两张映射，而图像只需要一张？

- 转换方向（`FontInstance → krilla Font`）：`text` 在绘制时要把 Typst 字体转成 krilla 字体，需要正向查重，避免重复转换。
- 反查方向（`krilla Font → FontInstance`）：`finish()` 报字体相关错误（如 `KrillaError::Font`、`ValidationError::ContainsNotDefGlyph`）时，krilla 只认得自己手里的 krilla 字体对象，需要**反查**回 Typst 字体，才能在错误信息里显示字体名、定位 span（见 [src/convert.rs:L442-L449](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L442-L449) 与 [src/convert.rs:L600-L606](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L600-L606) 用到的 `display_font(gc.fonts_backward.get(f))`）。

图像则不同：krilla 报图像错误时**两种情况**——要么带 `loc`（直接 `to_span(loc)`），要么只给 krilla 图像对象（`SixteenBitImage`，用 `image_to_spans` 反查）。所以图像只需「对象 → span」一张映射，外加一个 `image_spans` 集合用于「这个 span 是不是图像」的判断（`Transparency` 错误分流，见 [src/convert.rs:L656](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L656)）。

#### 4.3.4 代码实践

**实践：给 10 个字段标「读/写者」。**

1. 实践目标：把静态的结构体定义，变成动态的「数据流」理解。
2. 操作步骤：
   - 打开 [src/convert.rs:L277-L301](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L277-L301)。
   - 复制 4.3.2 的读写表，对每个字段用编辑器全局搜索其使用点（如搜 `gc.fonts_forward`、`gc.image_spans`、`gc.loc_to_names`），核对「谁写、谁读」。
3. 需要观察的现象：
   - `fonts_*` 与 `image_*` 的写入点都在对应翻译器（`text.rs` / `image.rs`）里。
   - `loc_to_names` 的写入点不在遍历里，而在 `convert()` 阶段①的 `collect_named_destinations`，经 `GlobalContext::new` 注入——它**在转换开始前就已填好**。
4. 预期结果：你会清晰看到「`gc` = 一只背包，部分格子（缓存）边走边填，部分格子（映射/子系统）出发前就装好」。

#### 4.3.5 小练习与答案

**练习 1**：`fonts_forward` 和 `fonts_backward` 都用 `FxHashMap`（rustc-hash）。为什么字体缓存要用哈希表而不是 `Vec`？
**答案**：因为查重是按「键」进行的——`text` 要按 `FontInstance` 查是否转过，`finish` 要按 krilla `Font` 反查 Typst 字体。哈希表支持 O(1) 按键查找；用 `Vec` 则需线性扫描，字体种类一多就慢。`FxHashMap` 是 rustc-hash 的非加密哈希实现，比标准 `HashMap` 更快，适合这种纯查重场景。

**练习 2**：`image_to_spans` 注释说「同一张图可能出现多次，我们只存第一次出现的 span」。这对错误信息有什么影响？
**答案**：当 krilla 报告某个图像对象的错误（如 16 位图像不支持）时，`typst-pdf` 只能定位到该图像**第一次出现**的位置，而非「所有出现位置」。这是个有意的简化：错误提示只需指到一个能复现的地方即可，不必枚举全部（见 [src/convert.rs:L283-L286](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L283-L286)）。

**练习 3**：`GlobalContext` 带生命周期 `<'a>`。这个 `'a` 是谁的引用？
**答案**：`'a` 绑定的是 `document: &'a PagedDocument`、`options: &'a PdfOptions`、`link_resolver: Option<Tracked<'a, ...>>` 这三个**借用**。也就是说 `gc` 不能比 `convert()` 的那三个参数活得更久——它只是 `convert()` 一次调用内的临时状态，调用结束即销毁。这与 `PdfOptions` 可跨多次编译复用并不冲突，因为复用的是「外面的 `options` 对象」，`gc` 只借用它。

---

### 4.4 阶段顺序的依赖链：为什么是这个顺序

#### 4.4.1 概念说明

读 `convert()` 最容易忽略的一点是：**它的阶段顺序不是任意排列，而是被数据依赖锁死的**。每一阶段的产物，往往正是下一阶段的输入。本节把这条依赖链显式地画出来，并解释几个关键节点的「为什么」。

特别要理解三个「非显然」的依赖：

1. `PageIndexConverter` 必须在 `collect_named_destinations` **之前**——因为命名目的地要把 Typst 页号映射成 PDF 页号。
2. `tags::init` 必须在 `GlobalContext::new` **之前**——因为 `Tags` 要作为字段注入 `gc`。
3. `tags::resolve` 必须在 `set_metadata` **之前**——因为 `resolve` 产出的 `doc_lang`（文档主语言）是 `build_metadata` 的输入。

#### 4.4.2 核心流程

`convert()` 阶段间的数据依赖（箭头表示「A 的产出喂给 B」）：

```text
options ───────────► SerializeSettings ──► Document::new_with ──► document
                                                              │
document, options ──► PageIndexConverter(pic) ───────────────┤
                                                              ▼
pic, anchors, document ──► collect_named_destinations ──► loc_to_names (+ 写进 document)
                                                              │
document, options ──► tags::init ──► Tags ────────────────────┤
                                                              ▼
document, options, link_resolver, loc_to_names, pic, Tags ──► GlobalContext::new ──► gc
                                                              │
gc, document ──► convert_pages ──► (填充 fonts/image 缓存、遍历期间更新 tags)
                                                              │
gc, document ──► attach_files
                                                              │
gc ──► tags::resolve ──► (doc_lang, tree)
                              │           │
              doc_lang ───────┘           └──── tree
                  │                           │
                  ▼                           ▼
     build_metadata(gc, doc_lang)      set_tag_tree(tree)
                  │
                  ▼
       set_outline / set_metadata
                  │
gc, configuration ──► finish(document, gc, config) ──► Vec<u8>
```

读这张图的方法：从上往下看「谁产出谁消费」。任何一个箭头如果被「对调」，就会在运行时拿到尚未初始化的值（编译期可能不报，但逻辑必然错乱）。

#### 4.4.3 源码精读

**依赖点 ①：页号转换器与命名目的地**

[src/convert.rs:L67-L73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L67-L73) —— 先 `PageIndexConverter::new`，再 `collect_named_destinations(&mut document, ..., &page_index_converter)`。`collect_named_destinations` 内部会调用 `crate::link::pos_to_xyz(pic, pos)`（见 [src/convert.rs:L874](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L874)），把 Typst 的逻辑页号经 `pic` 映射成 PDF 页号——所以 `pic` 必须先就绪。

> `PageIndexConverter` 的细节（如何处理 `page_ranges` 跳页）详见 u2-l6。这里只需知道：它把「Typst 文档第 i 页」映射成「PDF 第几页（若该页被排除则为 `None`）」。其页号换算可写成（对**保留**的页）：
>
> \[ \text{pdf\_index}(i) = i - \text{skipped\_before}(i) \]
>
> 其中 \(\text{skipped\_before}(i)\) 是序号小于 \(i\) 且被排除的页数。

**依赖点 ②：tags::init 必须在遍历之前，且可能失败**

[src/convert.rs:L75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L75) —— `let tags = tags::init(typst_document, options)?;`。注意末尾的 `?`：若用户同时开了 `tagged` 与 `page_ranges`，`init` 内部会直接 `bail!`（见 [src/tags/mod.rs:L30-L31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs#L30-L31)），整个 `convert()` 在此提前返回错误。`init` 必须在 `convert_pages` 之前，因为遍历时各翻译器要调用 `tags::text`/`tags::image`/`tags::shape` 等钩子，它们依赖 `Tags` 已构建。

**依赖点 ③：gc 组装后才能遍历**

[src/convert.rs:L77-L84](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L77-L84) → [src/convert.rs:L86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L86) —— `GlobalContext::new` 把前面所有产物注入 `gc`，紧接着 `convert_pages(&mut gc, &mut document)?`。遍历既需要 `gc`（字体/图像/tags），也需要 `document`（往里画页面），所以两者都要 `&mut`。

**依赖点 ④：tags::resolve 产出 doc_lang 与 tree，分别喂给 metadata 与 tag_tree**

[src/convert.rs:L88-L92](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L88-L92) —— 关键三行：

```rust
let (doc_lang, tree) = tags::resolve(&mut gc)?;      // ③ 产出
document.set_outline(build_outline(&gc));            // ④ 大纲（只用 gc）
document.set_metadata(build_metadata(&gc, doc_lang));// ④ 元数据消费 doc_lang
document.set_tag_tree(tree);                         // ④ 结构树消费 tree
```

`tags::resolve` 返回 `SourceResult<(Option<Locale>, TagTree)>`（见 [src/tags/resolve/mod.rs:L56](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/resolve/mod.rs#L56)）。其中 `doc_lang` 是从结构树里推断出的**文档主语言**，作为 `build_metadata` 的参数；`tree` 是最终的结构树，直接挂给 `set_tag_tree`。这就是为什么 `set_metadata`/`set_tag_tree` **必须排在 `tags::resolve` 之后**。

> 顺带一提：`tags::resolve` 之所以排在 `convert_pages` 之后，是因为 tagged PDF 是「三段式」——`init` 预构建逻辑树、遍历时发射标记、`resolve` 把两者合起来解析成 `TagTree` 并做 PDF/UA 校验（详见 u1-l4 与 u5-l19）。`resolve` 开头第一句就是 `gc.tags.tree.assert_finished_traversal()`（见 [src/tags/resolve/mod.rs:L57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/resolve/mod.rs#L57)），它要求遍历必须已经结束、所有标记发射完毕，否则直接报错。

**依赖点 ⑤：collect_named_destinations 既写 document 又产 loc_to_names**

[src/convert.rs:L845-L885](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L845-L885) —— 它接收 `&mut document`，在 L879 调用 `document.register_named_destination(named.clone())` 把目的地**注册进文档**；同时返回 `locs_to_names` 映射（L884），后者经 `GlobalContext::new` 成为 `gc.loc_to_names`，供 `link` 模块在解析文档内链接时回退查找。一个函数同时服务于「文档对象」和「背包」，所以它必须排在两者都就绪之后、`GlobalContext::new` 之前。

#### 4.4.4 代码实践

**实践：做「乱序实验」，预测每一处会崩在哪。**

1. 实践目标：用「故意打乱顺序」的反向推理，验证依赖链。
2. 操作步骤（纯纸面推理，**勿改源码**）：
   - 假设把 [src/convert.rs:L88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L88) 的 `tags::resolve` 移到 [src/convert.rs:L86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L86) 的 `convert_pages` **之前**，会发生什么？（提示：`resolve` 先调用 `assert_finished_traversal`，此时遍历还没开始，结构标记尚未发射，断言/校验必然失败或得到空树。）
   - 假设把 `GlobalContext::new`（L77）移到 `tags::init`（L75）**之前**，编译能不能过？（提示：`Tags` 尚未构造，无法作为参数传入 `GlobalContext::new`。）
   - 假设把 `collect_named_destinations`（L68）移到 `PageIndexConverter::new`（L67）**之前**，会怎样？（提示：`pos_to_xyz(pic, ...)` 拿不到 `pic`。）
3. 需要观察的现象：有些乱序会被**编译器**拦下（`Tags`/`pic` 未定义），有些能编译过但**运行时逻辑错乱**（resolve 拿到空树）。能区分这两类，说明你真正理解了依赖。
4. 预期结果：你能说出「编译期依赖」（变量必须先定义）与「运行期/语义依赖」（数据必须先生成）的区别。

#### 4.4.5 小练习与答案

**练习 1**：`set_outline` 为什么排在 `tags::resolve` 之后、却又不依赖 `resolve` 的产物？
**答案**：`build_outline(&gc)` 只用 `gc`（查标题、用 `page_index_converter` 算页号），不依赖 `doc_lang`/`tree`。把它排在 `resolve` 之后主要是**代码组织**——把三个 `document.set_*` 调用集中放在一起便于阅读；逻辑上它在 `convert_pages` 之后任何位置都可以。而 `set_metadata`（用 `doc_lang`）和 `set_tag_tree`（用 `tree`）才是**真正依赖** `resolve` 的。

**练习 2**：为什么 `collect_named_destinations` 需要 `&mut document`，而 `convert_pages` 之外的多数函数只读 `gc`？
**答案**：因为它要把命名目的地**注册进 krilla 文档对象**（`document.register_named_destination`），这是对 `document` 的写操作。命名目的地必须在「文档对象」里登记，PDF 阅读器才能按名字跳转；同时它的返回值又进 `gc` 供链接解析。所以它一身二任：既改 `document`，又产 `gc` 字段。

**练习 3**：若用户关闭了 `tagged`（`options.tagged = false`），`tags::init` 和 `tags::resolve` 还会跑吗？产出有什么不同？
**答案**：都会跑，但 `tags::init` 走 `Tree::empty(...)` 分支（见 [src/tags/mod.rs:L36](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs#L36)），产出一棵空树；遍历期间各 tags 钩子因 `disabled()` 为真而直接返回、不发射标记；最终 `tags::resolve` 解析出一棵**空** `TagTree`（见 [src/tags/resolve/mod.rs:L66-L67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/resolve/mod.rs#L66-L67)），`set_tag_tree` 挂上去等于没挂。结果是 PDF 里没有无障碍结构树，体积更小、但失去可访问性。

---

### 4.5 finish()：收尾与 krilla 错误的归口

#### 4.5.1 概念说明

`convert()` 的最后一行是 `finish(document, gc, options.standards.config)`。这个 `finish` 不是 krilla 的方法，而是 `convert.rs` 里的一个**包装函数**，做两件事：

1. 调用 krilla 的 `document.finish()`，真正把文档序列化成 `Vec<u8>`。
2. 若 krilla 返回错误（`KrillaError`），把它**翻译**成带 span、带 hint 的 Typst `SourceDiagnostic`。

这一步之所以值得单独讲，是因为它是**整个 crate 错误处理的归口**：所有 krilla 层的失败（字体、图像、校验、PDF 结构、限制）都从这里冒出来，并被本地化为对用户友好的诊断。

> 本节只讲 `finish()` 的**结构与代表性错误映射**。完整的 `ValidationError` 映射（透明度、PostScript、版本要求、缺失 alt 文本等几十个分支）由 **u5-l18** 专题展开。

#### 4.5.2 核心流程

`finish()` 的处理流程：

```text
finish(document, gc, configuration) -> SourceResult<Vec<u8>>
  │
  ├─ document.finish() -> Result<Vec<u8>, KrillaError>
  │     ├─ Ok(bytes) ──────────────────────────────► 直接返回 Vec<u8>
  │     └─ Err(e) ──► match e {
  │           Font(f, err)        ──► bail!(...) 用 gc.fonts_backward 定位字体
  │           Validation(ve)      ──► 对每个 (error, validators) 调 convert_error() → EcoVec
  │           Image(_, loc, err)  ──► bail!(to_span(loc), ...)
  │           SixteenBitImage(img,_)──► bail!(gc.image_to_spans[&img], ...)
  │           Pdf(_, e, loc)      ──► bail!(to_span(loc), ...) (InvalidPage / VersionMismatch)
  │           DuplicateTagId(_, loc) / UnknownTagId(_, loc) ──► bail!(to_span(loc), ...)
  │           DuplicateNamedDestination(_) ──► bail!(Span::detached(), ...)
  │           Limit(TooLongArray / TooLongDictionary / TooLargeFloat) ──► bail!(...)
  │         }
```

关键观察：**`gc` 在这一步被「消费」**——`finish` 按值接收 `gc`。因为收尾之后再无别处要用 `gc`，而错误翻译需要 `gc` 里的反向映射（`fonts_backward`、`image_to_spans`、`image_spans`）来把 krilla 的对象反查回 Typst 的 span。

#### 4.5.3 源码精读

`finish()` 函数定义（注意 L433 的编译计时注解）：

[src/convert.rs:L433-L533](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L433-L533) —— 整个收尾与错误翻译。

入口与成功路径：

[src/convert.rs:L434-L440](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L434-L440) —— `match document.finish()`，`Ok(r) => Ok(r)` 直接把字节送出。

**代表性错误 1：字体错误（用到反向缓存）**

[src/convert.rs:L442-L450](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L442-L450) —— `KrillaError::Font(f, err)`。krilla 给的是 krilla 字体对象 `f`，`display_font(gc.fonts_backward.get(&f))` 用**反向映射**反查出 Typst 字体名，写进错误信息；并附两条 hint（确保字体有效 / 该字体可能不被 Typst 支持）。这就是 4.3 里 `fonts_backward` 存在的核心理由。

**代表性错误 2：16 位图像（用到 image_to_spans）**

[src/convert.rs:L464-L470](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L464-L470) —— `KrillaError::SixteenBitImage(image, _)`。krilla 只给图像对象、不给 `loc`，于是用 `gc.image_to_spans[&image]` 反查出该图**首次出现**的 span，给出「16 bit images are not supported」+「convert the image to 8 bit」的修正建议。

**代表性错误 3：校验错误（委托给 convert_error，产出 EcoVec）**

[src/convert.rs:L451-L459](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L451-L459) —— `KrillaError::Validation(ve)`。`ve` 是一组 `(ValidationError, Validators)`；对每一个调用 `convert_error(&gc, *validators, e, configuration.version())`，收集成 `EcoVec<SourceDiagnostic>` 后整体返回。`convert_error` 是一个庞大的 `match`（[src/convert.rs:L536-L838](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L536-L838)），把 `ValidationError` 各分支映射成带 `prefix`（哪个校验器报的）和 `hint` 的诊断——这部分是 u5-l18 的主角，本讲不展开。

> 其余分支（`Image`/`Pdf`/`DuplicateTagId`/`UnknownTagId`/`DuplicateNamedDestination`/`Limit`）大多用 `to_span(loc)` 还原 span 或 `Span::detached()`，逻辑同构。其中 `Pdf::VersionMismatch` 还会用 `configuration.version()` 生成「把导出目标提到更高版本」的 hint（[src/convert.rs:L480-L491](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L480-L491)），这正是 `finish` 要额外接收 `configuration` 参数的原因。

`to_span`——把 krilla 的 `Location` 还原成 Typst `Span`：

[src/convert.rs:L841-L843](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L841-L843) —— `loc.map(Span::from_raw).unwrap_or(Span::detached())`。krilla 在内容流里记录的 `Location` 被还原成 Typst 的源码 span，从而错误可以**指到用户源码的具体位置**；若没有 loc 则用 detached span（无位置）。

#### 4.5.4 代码实践

**实践：追踪两条错误链，验证 gc 在错误定位中的作用。**

1. 实践目标：理解「为什么 `finish` 必须吃掉 `gc`」。
2. 操作步骤：
   - 在 [src/convert.rs:L442-L450](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L442-L450)（字体错误）画出数据流：`krilla Font f` → `gc.fonts_backward` → `FontInstance` → `display_font` → 字体名。回答：若 `fonts_backward` 里查不到 `f`（理论上不该发生），`display_font` 会显示什么？（提示：`get` 返回 `Option`，应会显示占位/unknown。）
   - 在 [src/convert.rs:L464-L470](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L464-L470)（16 位图像）画出：`krilla Image` → `gc.image_to_spans` → `Span`。回答：若该图在文档里出现了 3 次，错误会指到哪一次？（答：第一次，见 4.3.3。）
3. 需要观察的现象：两类错误都依赖 `gc` 的反向映射/反查表，**没有 `gc` 就无法把 krilla 的对象翻译回用户源码位置**。
4. 预期结果：你能解释为什么 `finish` 的签名是 `finish(document, gc, configuration)`——三者缺一不可：`document` 要序列化、`gc` 要反查 span、`configuration` 要给版本相关 hint。

#### 4.5.5 小练习与答案

**练习 1**：`finish` 按值接收 `document` 和 `gc`（不是引用），为什么？
**答案**：因为这是 `convert()` 的最后一步，之后不再需要它们；按值接收可以**转移所有权**，避免不必要的借用与生命周期标注。尤其 `document.finish()` 本身就消费 `Document`（返回字节而非借用），`gc` 也在错误翻译里被读取后即可丢弃。

**练习 2**：`KrillaError::Validation` 这一支为什么返回 `EcoVec<SourceDiagnostic>`（一组诊断），而其它分支只用 `bail!`（一条）？
**答案**：因为一次校验可能同时发现**多个**问题（krilla 把它们打包成 `ve` 迭代器），`typst-pdf` 希望一次性把所有校验错误都报告给用户，而不是修一个看一个。所以这一支对每个 `(error, validators)` 都生成一条 `SourceDiagnostic`，聚成 `EcoVec`。其它分支（字体、图像、限制等）通常是单一失败，用 `bail!` 一条即可。

**练习 3**：`to_span(loc)` 里 `Span::from_raw` 是「从原始值构造 span」。这个 span 是怎么来的——用户源码？还是 krilla 内部？
**答案**：它的源头是用户源码。在 `convert_pages` 绘制内容时，`typst-pdf` 把 Typst 的 `Span`（来自源码位置）通过 krilla `Location` 机制「种」进内容流（见 `handle_image`/`handle_shape` 等传入的 `*span`）。krilla 报错时回传 `Location`，`to_span` 再用 `Span::from_raw` 还原成当初那个 Typst span，于是错误就能指到用户写的源码行。这构成了「错误可溯源」的闭环。

---

## 5. 综合实践：为 convert() 的每个阶段写「跳过会怎样」

本讲贯穿一个主线：`convert()` 是一条被数据依赖锁死的流水线。请完成下面的「故障推演」任务，把四块知识（`SerializeSettings`、`GlobalContext`、阶段顺序、`finish`）串起来。

针对 `convert()` 的每个主要阶段，在笔记里写一句话注释，说明**若跳过该阶段**（注释掉那一行）会发生什么——要尽量精确到「编译失败 / 运行出错 / 质量下降」中的哪一类，以及波及哪些下游。建议覆盖以下阶段：

1. **序列化设置**（[L54-L64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L54-L64)）：跳过后 `Document::new_with` 拿不到 settings。归类？
2. **建空文档**（[L66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L66)）：跳过后后续所有 `&mut document` 调用无处可写。
3. **页号转换器**（[L67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L67)）：跳过后命名目的地与页码标签拿不到 `pic`。
4. **命名目的地**（[L68-L73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L68-L73)）：跳过后 `gc.loc_to_names` 为空，文档内链接将无法定位（可降级回退，但跳转失效）。
5. **tags::init**（[L75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L75)）：跳过后 `gc.tags` 无来源，编译期即失败。
6. **GlobalContext::new**（[L77-L84](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L77-L84)）：跳过后所有翻译器拿不到字体缓存/tags，编译期失败。
7. **convert_pages**（[L86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L86)）：跳过后 PDF 里没有任何页面内容，且 tags 遍历未发生。
8. **attach_files**（[L87](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L87)）：跳过后附件丢失（质量下降，PDF 仍可生成）。
9. **tags::resolve**（[L88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L88)）：跳过后 `doc_lang`/`tree` 无来源，且 `set_metadata`/`set_tag_tree` 编译失败。
10. **set_outline / set_metadata / set_tag_tree**（[L90-L92](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L90-L92)）：分别跳过——大纲丢失 / 元数据（含文档语言、标题）丢失 / 结构树不挂载（PDF 不可访问）。
11. **finish**（[L94](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L94)）：跳过后根本没有字节产出，函数返回类型对不上。

完成后，把你写的注释按「**必需（编译/运行必崩）**」与「**增强（质量下降但仍可出 PDF）**」分成两列。预期分类大致为：

- 必需：1、2、5、6、7、9、11（缺了它们要么编译不过、要么没有输出）。
- 增强：4、8、10（缺了它们 PDF 仍能生成，但失去跳转、附件、大纲/元数据/可访问性）。

> 验收标准：你能对每个阶段说出「跳过的后果类别」+「波及的下游字段或调用」，并解释为什么 9（tags::resolve）是「必需」——因为它产出 `doc_lang`/`tree` 这两个被 10 消费的值，是依赖链上的硬节点。

## 6. 本讲小结

- `convert()` 是整个 crate 唯一的后厨入口，签名四参数（`document`/`options`/`anchors`/`link_resolver`）正对应「输入+配置+两种导出模式」，主体可分**准备 / 转换 / 收集 / 收尾**四阶段。
- **`SerializeSettings`** 是 `PdfOptions` 到 krilla 的翻译层：9 字段中 5 个由 `options.*` 驱动（`compress_content_streams`/`ascii_compatible`/`pretty` 由 `pretty` 驱动，`enable_tagging` 由 `tagged` 驱动，`configuration` 由 `standards` 驱动），4 个写死（`no_device_cs`/`xmp_metadata`/`cmyk_profile`/`render_svg_glyph_fn`）。
- **`GlobalContext`** 是贯穿全程的背包，10 字段分三类：引用类（`document`/`options`/`link_resolver`，只读）、缓存映射类（字体前/反向、`image_to_spans`/`image_spans`、`loc_to_names`，边走边填或预先注入）、子系统类（`page_index_converter`/`tags`）；字体需双向缓存以支持错误反查，图像靠 `image_to_spans` + `image_spans` 分流透明度/16 位错误。
- **阶段顺序被依赖锁死**：`PageIndexConverter`→`collect_named_destinations`、`tags::init`→`GlobalContext::new`、`convert_pages`→`tags::resolve`→（`doc_lang`→`set_metadata`，`tree`→`set_tag_tree`）。
- **`finish()`** 包装 krilla `document.finish()`，成功返回字节，失败时用 `gc` 的反向映射把 `KrillaError`（字体/图像/校验/PDF/限制）翻译成带 span、带 hint 的 `SourceDiagnostic`，其中 `Validation` 一支聚成 `EcoVec` 一次性报全部校验问题（完整映射见 u5-l18）。

## 7. 下一步学习建议

本讲把 `convert()` 这条主线**整体**讲透了，但主线里还有几个节点是「点了名、没展开」的。下一讲起会逐个钻进去：

- **u2-l6 页面导出**：展开阶段 ② 里 `convert_pages` 的页面尺寸最小值、bleed 出血框/trim box、`PageLabelExt`、以及 `PageIndexConverter` 如何处理 `page_ranges` 跳页（本讲只用了它的接口）。
- **u2-l7 Frame 遍历器**：展开 `handle_frame`/`handle_group` 与 `FrameContext` 的变换状态栈 push/pop、`State` 的 `container_transform`/`container_size`（本讲把 `handle_frame` 当黑盒）。
- **u2-l8 类型转换工具集**：展开 `util.rs`，看那些把 Typst 几何类型翻译成 krilla 类型的 trait 与 `convert_path`（本讲只引用了 `AbsExt`/`to_span` 等）。
- **u5-l18 错误与校验映射**：展开 `convert_error` 的全部 `ValidationError` 分支（本讲只取了字体、16 位图像两个代表）。

阅读建议：复习时合上本讲，能否凭记忆复述「`convert()` 的四阶段 + `SerializeSettings` 的 5 选项驱动项 + `GlobalContext` 的三分类 + `finish` 的错误归口」？若能，说明主线已经内化，可以放心钻进细节。
