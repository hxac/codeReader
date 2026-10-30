# PDF 导出：从 PagedDocument 到字节流

## 1. 本讲目标

Typst 编译流水线的最后一步是「导出」：把排版阶段产出的 `PagedDocument`（一叠页面帧）序列化成某种格式的字节流。本讲聚焦其中最重要的导出器 `typst-pdf`，读完本讲你应该能够：

1. 说出 `typst_pdf::pdf()` 入口函数的签名与职责，并跟踪它如何遍历 `Page`/`Frame`，把 Typst 的图元逐类翻译给底层 PDF 库 krilla。
2. 解释字体、图像这两类「重资源」在导出层如何被去重与缓存，以及字体子集化实际发生在哪一层（提示：不在 typst-pdf 里）。
3. 描述 tagged PDF（无障碍标签树）在导出层的实现入口：`FrameItem::Tag` 如何驱动标签树的构建，最终经 `document.set_tag_tree` 写入 PDF。
4. 理解 `finish`/`convert_validation_error` 如何把 krilla 的底层错误（如 `MissingFieldAltName`）翻译成带 Span、带修复提示的 Typst 诊断。
5. 巩固一个架构结论：导出 crate 只依赖 `PagedDocument` 抽象，不回头触碰求值与排版。

## 2. 前置知识

### 2.1 PagedDocument：导出器眼中的「整个文档」

第四单元（u4-l3）讲过，`typst::compile::<T>` 的排版产物是 `PagedDocument`。对导出器来说，它是一个**只读的最终结果**：一叠 `Page`，每个 `Page` 持有一个 `Frame`（页面帧）。本讲会大量用到这张「文档 = 页面列表」的图。

### 2.2 Frame 与 FrameItem：图元树

`Frame` 是排版结果的统一容器（u4-l3、u4-l4 讲过它的生成）。一个 `Frame` 由若干 `(Point, FrameItem)` 组成——「放在哪里」加「放什么东西」。`FrameItem` 共六类（组、文本、形状、图像、链接、标签），本讲的重点就是把这六类逐一翻译成 PDF 绘制指令。

### 2.3 krilla：Typst 的 PDF 后端

Typst 自己不手写 PDF 对象（不再像早期版本那样手工构造 `xref` 表），而是委托给 [krilla](https://github.com/LaurenzV/krilla) 这个第三方 Rust PDF 库。本仓库通过 git 依赖锁定 krilla 0.8.2（见 `Cargo.lock` 中 `name = "krilla"` 条目）。krilla 提供的核心抽象：

- `Document`：一份正在构建的 PDF，最终 `finish()` 成 `Vec<u8>`；
- `Page`/`Surface`：一页及其绘制画布，`push_transform`、`draw_glyphs`、`draw_image`、`push_clip_path` 等指令都发往 `Surface`；
- `Surface::set_location`：给后续绘制挂一个「位置标记」，出错时 krilla 能把它吐回来——这是本讲错误定位机制的关键。

理解分工：**typst-pdf 负责「翻译」，krilla 负责「组装与序列化」**。字体子集化、对象编号、流压缩都发生在 krilla 内部。

### 2.4 tagged PDF 与无障碍

tagged PDF 是在 PDF 里内嵌一棵「结构树」（标题、段落、列表、表格等逻辑结构），屏幕阅读器靠它向视障用户朗读文档。PDF/UA-1、PDF/A-1a/2a/3a 等标准强制要求标签树。Typst 默认输出 tagged PDF（可以用 `#set pdf(tagged: false)` 关闭）。第四单元 u4-l7 讲过 `Tag`/`TagElem`：realize 阶段会把可定位元素成对写入 Frame 的 `FrameItem::Tag(Start/End)`——它们在排版期只是「书签」，到本讲的导出层才被真正消费，长成 PDF 结构树。

### 2.5 PDF 元数据：Creator 与 Producer

PDF 信息字典里有两个容易混淆的字段：`/Creator`（制作文档的软件，Typst 在这里写 `Typst 0.15.1`）和 `/Producer`（生成 PDF 文件的软件，由 krilla 写入）。后面实践环节会用 `pdfinfo` 观察它们，并回溯各自的写入点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/typst-pdf/src/lib.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/lib.rs) | crate 门面：`pdf()` / `pdf_in_bundle()` 导出入口、`PdfOptions` 及其 `resolve` 合并校验 |
| [crates/typst-pdf/src/convert.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs) | 转换主干：`convert` 总装配、`convert_pages` 页循环、`handle_frame`/`handle_group` 帧遍历、两个上下文结构、错误本地化 |
| [crates/typst-layout/src/document.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/document.rs) | 被消费的数据抽象：`PagedDocument` 与 `Page` 的定义 |
| [crates/typst-pdf/src/text.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/text.rs) | 文本图元 → `draw_glyphs`；字体转换与 memoize 缓存；`PdfGlyph` 适配器 |
| [crates/typst-pdf/src/image.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/image.rs) | 图像图元：光栅/SVG/PDF 三类图像的绘制与 span 登记 |
| [crates/typst-pdf/src/paint.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/paint.rs) | 颜色、渐变、pattern 到 krilla 填充/描边的转换（本讲按需引用） |
| [crates/typst-pdf/src/tags/mod.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/tags/mod.rs) | 标签树子系统门面：`init`/`page`/`group`/`handle_start`/`handle_end` 等钩子 |
| [crates/typst-pdf/src/metadata.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/metadata.rs) | 信息字典：标题、作者、Creator、创建日期的写入 |
| [crates/typst-pdf/src/outline.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/outline.rs) | PDF 书签（大纲）构建 |
| [crates/typst-pdf/src/attach.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/attach.rs) | 文件附件（`attach` 元素）写入 |
| [crates/typst-cli/src/compile.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/compile.rs) | CLI 侧接线：`export_pdf` 调用 `typst_pdf::pdf`，`pdf_options` 组装选项 |

typst-pdf 的其余子模块（`link.rs` 链接、`shape.rs` 形状、`format.rs` `#set pdf(...)` 元素与标准定义、`util.rs` 坐标换算）本讲点到为止，`tags/` 目录下的 `context`、`groups`、`resolve`、`tree` 四个子模块留作 4.4 节的地图。

## 4. 核心概念与源码讲解

### 4.1 导出入口与选项装配

#### 4.1.1 概念说明

导出器需要一个「入口契约」：输入是排版产物加选项，输出是字节加警告。typst-pdf 把这个契约拆成两层：

- **门面函数** `pdf()`：薄薄一层，把调用转发给内部的 `convert::convert`，并用 `Warned<...>` 包裹警告列表；
- **选项装配** `PdfOptions::resolve`：调用方传入的选项（CLI 参数）与文档内 `#set pdf(...)` 写下的格式选项（存在 `PagedDocument.options` 里）在这里合并，并做一致性校验（比如「导出页面范围」与「标签化」「PDF/A-1a 等无障碍标准」互斥）。

`#[typst_macros::time(name = "pdf")]` 这个属性宏我们在 u4-l1 见过：它让这段导出耗时出现在 `--timings` 报告里。

#### 4.1.2 核心流程

```text
CLI: typst compile hello.typ
  └─ compile.rs::export_pdf
       ├─ pdf_options(config)          # 从 CLI 参数组装 PdfOptions
       └─ typst_pdf::pdf(document, &options)
            └─ convert::convert(...)   # 第 4.2 节展开
```

`resolve` 的校验规则（可对照源码读）：

1. 若指定了导出页面范围（`pages`）且显式开启 tagged → 直接报错；若 tagged 为 `auto` → 推一条警告「导出页面范围会禁用 PDF 标签化」。
2. 对四个「无障碍标准」（PDF/A-1a、A-2a、A-3a、UA-1）逐一检查：显式 `tagged: false` 或页面范围都与之冲突，报错并附提示。
3. 合并产出 `PdfOptions<Complete>`——泛型参数从 `Partial`（字段可缺省）变成 `Complete`（字段齐全），后面整个导出过程都拿这个「已解析」版本。

`tagged()` 的默认逻辑浓缩成一句话：**只要不是导出页面范围、且没人显式关掉，就输出标签树**。

#### 4.1.3 源码精读

导出入口函数，接收 `&PagedDocument` 与 `&PdfOptions`，返回「警告 + 字节」：[crates/typst-pdf/src/lib.rs:L40-L47](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/lib.rs#L40-L47)

```rust
#[typst_macros::time(name = "pdf")]
pub fn pdf(
    document: &PagedDocument,
    options: &PdfOptions,
) -> Warned<SourceResult<Vec<u8>>> {
    let mut warnings = EcoVec::new();
    let output = convert::convert(document, options, &[], None, &mut warnings);
    Warned { output, warnings }
}
```

它只做三件事：开一个警告列表、调 `convert`、打包返回。多出来的两个参数（`&[]` 与 `None`）是 bundle 场景用的「锚点」与「跨文档链接解析器」，普通导出留空；bundle 变体见 [crates/typst-pdf/src/lib.rs:L56-L66](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/lib.rs#L56-L66) 的 `pdf_in_bundle`（供 typst-bundle 在多文档打包时复用同一套转换）。

`PdfOptions` 的四个字段：[crates/typst-pdf/src/lib.rs:L70-L91](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/lib.rs#L70-L91)

- `ident`：稳定文档标识，CLI 传 `Smart::Auto`（没有长期项目概念），为 `Auto` 时用标题+作者哈希生成文档 ID；
- `creator`：`/Creator` 元数据，`Auto` 时默认 `Typst $version`；
- `timestamp`：创建时间戳，仅当文档 `date` 为 `auto` 时生效；
- `format`：格式选项（页面范围、标准、tagged、pretty）。

合并与互斥校验的现场：`resolve` 先从文档读回 `#set pdf(...)` 的设置再叠加调用方选项：[crates/typst-pdf/src/lib.rs:L95-L151](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/lib.rs#L95-L151)。其中「页面范围禁用标签化」的警告分支在 [L107-L117](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/lib.rs#L107-L117)，四个无障碍标准的冲突表在 [L120-L143](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/lib.rs#L120-L143)。

tagged 的最终判定：[crates/typst-pdf/src/lib.rs:L162-L164](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/lib.rs#L162-L164)

```rust
pub(crate) fn tagged(&self) -> bool {
    self.format.pages.v.is_none() && self.format.tagged.v.unwrap_or(true)
}
```

「没有页面范围」且「tagged 未显式设为 false」即开标签——这就是 Typst 默认产出无障碍 PDF 的源头。

CLI 侧的接线：[crates/typst-cli/src/compile.rs:L402-L413](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/compile.rs#L402-L413) 里 `export_pdf` 组好选项、调 `typst_pdf::pdf`、把字节交给 `Output::write`；而 [L645-L672](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/compile.rs#L645-L672) 的 `pdf_options` 展示了 CLI 给 `ident`/`creator` 都传 `Auto`、时间戳取本地时间（或 `--creation-timestamp` 的 UTC）的默认策略。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到 `resolve` 的警告分支被触发，确认「文档内设置」与「CLI 选项」会在导出入口汇合。
2. **操作步骤**：
   - 准备一个两页文档（示例代码）：

     ```typst
     // two-pages.typ（示例代码）
     #set page(height: 80pt)
     First page
     #pagebreak()
     Second page
     ```

   - 用已安装的 typst CLI 或源码构建执行：`cargo run -p typst-cli -- compile --pages 1 two-pages.typ out.pdf`（`--pages` 参数定义见 [crates/typst-cli/src/args.rs:L388-L399](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/args.rs#L388-L399)）。
3. **需要观察的现象**：终端出现警告 `exporting a page range disables PDF tagging`，并附带两条 hint。
4. **预期结果**：输出只有 1 页的 PDF；警告文本与 [crates/typst-pdf/src/lib.rs:L107-L117](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/lib.rs#L107-L117) 中的字符串逐字对应——因为 `pdf_options` 把 `pages` 塞进了选项，`tagged` 保持 `auto`，恰好命中该分支。若改用 `#set pdf(tagged: false)`（或 CLI 传显式 false）再配 `--pages`，警告消失。
5. 本实践不涉及修改源码，结果可在本地直接验证（若 CLI 未安装，用 `cargo run -p typst-cli` 即可，构建方式见 u1-l2）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CLI 的 `PdfOptions::ident` 传 `Smart::Auto` 而不是自己造一个？

**答案**：`ident` 的文档注释（lib.rs L74-L81）要求它是「跨多次编译稳定」的标识，用来生成 PDF 文档 ID；CLI 没有长期项目身份的概念，伪造一个反而会让文档 ID 漂移或碰撞，所以按注释建议传 `Auto`，此时退化为用标题与作者的哈希。

**练习 2**：`PdfOptions<F: Fields = Partial>` 里的泛型 `Fields` 起什么作用？

**答案**：它让同一结构体表达两种状态：入口处 `Partial`（格式字段可以缺省，等待与文档设置合并），`resolve` 之后得到 `Complete`（所有格式字段都有确定值）。这样「未解析的选项」和「已解析的选项」在类型上就是两个类型，导出主流程只接受 `PdfOptions<Complete>`，编译期就杜绝了「拿着没合并完的选项去导出」。

**练习 3**：`pdf()` 与 `pdf_in_bundle()` 为何不合并成一个带 `Option` 参数的函数？

**答案**：普通导出根本没有锚点与跨文档链接解析器可传，独立入口让公共路径的签名保持干净；同时 `#[typst_macros::time]` 的计时名称也不同（`pdf` 与 `pdf in bundle`），便于在 `--timings` 报告里区分两类耗时。

### 4.2 页面与 Frame 的遍历转换

#### 4.2.1 概念说明

这是整个导出器的骨架：`convert` 总装配 → `convert_pages` 逐页循环 → `handle_frame`/`handle_group` 递归下钻 Frame 树。理解它的关键是三个「上下文」结构：

- `State`：当前坐标变换 + 「最近一个硬 Frame」的变换与尺寸。为什么需要后者？因为渐变和 pattern 是相对**容器**定义的，绘制一个渐变填充时必须知道「它挂在哪个容器、容器多大」，否则渐变角度和尺寸就错了。
- `FrameContext`：单页（或单个 pattern）转换的局部状态——一个 `State` 栈（进出一个项/组就 push/pop）和链接注解登记表。
- `GlobalContext`：整份文档共享的资源缓存与索引：字体双向映射、图像 span 登记、命名目的地表、页面索引换算器、标签树上下文。

#### 4.2.2 核心流程

`convert` 的十步装配：

```text
convert(document, options, ...)
  1. options.resolve(...)              # 合并文档内 #set pdf(...) 设置（4.1 节）
  2. tags::init(...)                   # 初始化标签树（4.4 节）
  3. SerializeSettings { ... }         # 压缩、XMP、是否 tagging 等 krilla 序列化设置
  4. Document::new_with(settings)      # 创建 krilla 文档
  5. PageIndexConverter::new(...)      # 页面范围 → PDF 页号 重映射
  6. collect_named_destinations(...)   # 带标签标题 → 命名目的地
  7. GlobalContext::new(...)
  8. convert_pages(...)                # 逐页绘制（本节主角）
  9. attach_files(...)                 # 文件附件
 10. tags::resolve(...) → set_outline / set_metadata / set_tag_tree
 11. finish(document, gc)              # krilla 序列化 + 错误翻译（4.4 节）
```

`convert_pages` 对每一页：

```text
对第 i 页：
  ├─ 页号换算后不在导出范围 → continue
  ├─ PageSettings::from_wh(宽 + 出血, 高 + 出血)   # 最小 3×3，PDF 1.4–1.7 规范要求
  ├─ 出血非零 → 设置 TrimBox（裁切标记内的成品区）
  ├─ 页码标签：numbering 生成；若跳过页则用真实页号兜底
  ├─ document.start_page_with(settings) → surface
  ├─ FrameContext::new(页号, 页面尺寸 + 出血)
  └─ tags::page { handle_frame(页面帧, 出血, 背景填充) }
       └─ surface.finish()，收集链接注解挂到 page
```

`handle_frame` 对帧内每个 `(point, item)`：

```text
push state（保存现场）
  ├─ 硬帧 → register_container(本帧尺寸)     # 更新渐变容器基准
  ├─ 有背景填充 → 画一个矩形（标记为 Background 工件）
  ├─ 平移到 padding 之后
  └─ 对每个 (point, item)：
       push state; 平移到 point
       match item:
         Group(g)  → handle_group：复合组变换、可选裁剪路径，递归 handle_frame
         Text(t)   → handle_text（4.3 节）
         Shape(s)  → handle_shape
         Image(i)  → handle_image（4.3 节）
         Link(d)   → handle_link
         Tag(Start/End) → flags.tagged 时驱动标签树（4.4 节）
       pop state
pop state ×2
```

坐标变换的复合可以写成：每进入一层，当前变换 \( M \) 更新为

\[ M' = M \cdot T(\Delta) \]

其中 \( T(\Delta) \) 是该项位置的平移（或组自带的变换矩阵）。`State` 里始终存着「从页面原点到当前项局部坐标系」的累积矩阵，`surface.push_transform` 时一次性交给 krilla。

#### 4.2.3 源码精读

被消费的数据抽象——`PagedDocument` 的四个字段：[crates/typst-layout/src/document.rs:L18-L23](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/document.rs#L18-L23)。注意 `introspector` 是 `Arc` 包裹的，且 `Hash` 实现（[L59-L66](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/document.rs#L59-L66)）刻意不哈希它——注释说明「内省器完全由页面派生」，这是 comemo 缓存友好性的体现。`Page` 的字段（[L98-L120](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/document.rs#L98-L120)）里 `fill: Smart<Option<Paint>>` 的注释直接点明了导出器该怎么消费它：PDF 用 `fill_or_transparent()`，栅格/SVG 用 `fill_or_white()`——两个方法就在 [L126-L135](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/document.rs#L126-L135)。这是「数据侧给消费者留好语义出口」的一个小巧思。

`convert` 总装配：[crates/typst-pdf/src/convert.rs:L47-L98](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L47-L98)。第一行就是「从文档读回 `#set pdf(...)`」：`options.resolve(typst_document.options().get::<PdfFormat>(), warnings)?`（[L54-L55](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L54-L55)）；`SerializeSettings` 里能看到 `render_svg_glyph_fn: render_svg_glyph`（[L59-L69](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L59-L69)）——SVG 字形回退绘制函数被注入给 krilla；末尾三行 `set_outline`/`set_metadata`/`set_tag_tree`（[L93-L95](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L93-L95)）分别把书签、信息字典、结构树挂上 krilla 文档。

页循环：[crates/typst-pdf/src/convert.rs:L100-L176](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L100-L176)。页面尺寸把出血算进去并 `.max(3.0)`（[L107-L119](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L107-L119)），注释解释了原因：PDF 1.4–1.7 规定页面最小 3×3 单位，零尺寸页面 krilla 与阅读器都无法处理；出血非零时补一个 `TrimBox`（[L121-L128](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L121-L128)）。页码标签的兜底逻辑（[L130-L148](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L130-L148)）值得读注释：跳页导出时 PDF 物理页号与 Typst 真实页号错位，至少用标签把真实页号标出来。

六类图元的分发现场：[crates/typst-pdf/src/convert.rs:L361-L385](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L361-L385) 的 `match item`。对照 [crates/typst-library/src/layout/frame.rs:L486-L499](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/frame.rs#L486-L499) 的 `FrameItem` 枚举定义，六个变体一一对应六个 `handle_*`（Tag 变体在 match 里拆成 Start/End 两个分支，且都先判 `flags.tagged`）。

三个上下文结构：`State` 的字段注释直说了 `container_transform`/`container_size` 是为渐变与 pattern 准备的（[crates/typst-pdf/src/convert.rs:L181-L220](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L181-L220)）；`FrameContext`（[L223-L278](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L223-L278)）维护状态栈与链接注解表；`GlobalContext`（[L281-L328](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L281-L328)）的字段注释同样信息量大——`fonts_forward`/`fonts_backward` 缓存字体双向映射，`image_to_spans` 的注释坦承「同一图像多次出现会有多个 span，只记第一个」。

`handle_group` 的裁剪路径处理：[crates/typst-pdf/src/convert.rs:L396-L433](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L396-L433)。把组的 `clip` 曲线灌进 krilla 的 `PathBuilder`，用**当前累积变换**变换到页面坐标系后 `push_clip_path`，递归画完子帧再 `pop`。

页面范围换算器 `PageIndexConverter`：[crates/typst-pdf/src/convert.rs:L907-L938](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L907-L938)。预计算「Typst 页号 → PDF 页号」映射，被跳过的页不占 PDF 页号——`--pages 2,5` 之后第 5 页在 PDF 里就是第 1 页。

命名目的地收集：[crates/typst-pdf/src/convert.rs:L865-L905](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L865-L905)。这里能看到导出器对内省器的**读取**：`introspector().query(&HeadingElem::ELEM.select())` 找出所有带 label 的标题（同名去重取第一个），把 `Location` 映射成 PDF 命名目的地——这正是 u4-l7 讲的内省数据在导出层的用途之一（供书签/外部链接跳转）。

#### 4.2.4 代码实践

1. **实践目标**：用打点的方式亲眼确认 `convert_pages` 的遍历顺序与「页数 = Frame 数量」的对应关系（源码阅读 + 临时日志型实践）。
2. **操作步骤**：
   - 在本地分支给 [crates/typst-pdf/src/convert.rs:L101](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L101) 的 `for` 循环体开头临时加一行 `eprintln!("page {i}: {}x{} bleed {:?}", typst_page.frame.width(), typst_page.frame.height(), typst_page.bleed);`；
   - 用 `cargo run -p typst-cli -- compile docs-overview.typ out.pdf` 编译一个 `#set page(width: 200pt, height: 100pt)` 的三页文档（用 `#pagebreak()` 分页）；
   - 再试一个带出血的版本：`#set page(width: 200pt, height: 100pt, margin: (x: 10pt))` 加 `#set page(bleed: 5pt)`（bleed 参数以本地文档为准，若编译报参数不存在则去掉该项）；
   - 观察后 `git checkout -- crates/typst-pdf/src/convert.rs` 还原。
3. **需要观察的现象**：日志按 0、1、2 顺序打印三行；带出血版本打印的 bleed 是非零值，且页面对象宽高 = 帧宽高 + 左右/上下出血。
4. **预期结果**：`pdfinfo out.pdf` 的 `Pages: 3` 与日志行数一致；出血版本的 PDF 用 `pdfinfo -box` 能看到 MediaBox 比 TrimBox 各边大 5pt（对应 `with_trim_box` 的写入）。TrimBox 的具体数值输出为待本地验证项（不同 poppler 版本的显示格式略有差异）。
5. 本实践需要修改源码再还原，请务必只在本地的临时分支操作。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `FrameContext::push` 是「克隆栈顶再压栈」，而不是保存增量？

**答案**：见 [convert.rs:L241-L243](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L241-L243)。`State` 只含一个矩阵和一对「容器矩阵+尺寸」，克隆成本极低；克隆式压栈让当前状态永远在栈顶，读取时无需回放整条链，代码简单且分支预测友好。这是「用小对象的值语义换掉复杂的撤销逻辑」的典型取舍。

**练习 2**：`handle_frame` 里判断 `frame.kind().is_hard()` 才 `register_container`，「硬帧」是什么？

**答案**：`FrameKind` 定义在 [crates/typst-library/src/layout/frame.rs:L459-L482](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/frame.rs#L459-L482)：`Soft` 帧跟随父级尺寸（默认），`Hard` 帧使用自身尺寸。渐变/pattern 需要挂在一个有确定尺寸的容器上，所以只有遇到硬帧才更新容器基准；若软帧也登记，子内容的渐变会错误地以一个「虚尺寸」为基准。

**练习 3**：跳页导出时，页内链接指向「第 3 页」会怎样？

**答案**：链接目标里的页号先经过 `PageIndexConverter::pdf_page_index` 换算成 PDF 页号；若目标页被跳过则得到 `None`，链接不会被写入（`collect_named_destinations` 里 `pos_to_xyz` 返回 `None` 时直接跳过该目的地）。同时因为「页面范围 → 禁用标签化」的规则（4.1 节），跳页导出本来就不带结构树。

### 4.3 资源嵌入：字体、图像与绘制

#### 4.3.1 概念说明

文档里最重的资源是字体与图像。导出层面对的共同问题是**去重**：同一字体在一篇文档里被绘制一千次文本，PDF 里只需要一份字体程序；同一张 logo 出现十次，只需要一个图像对象。typst-pdf 的策略分三层：

1. **转换层去重**（typst-pdf）：`GlobalContext.fonts_forward` 把 `FontInstance`（Typst 侧字体+变化轴实例）映射到 `krilla::text::Font`，查表命中就不再重建；
2. **构建层记忆化**（comemo）：`build_font` 标了 `#[comemo::memoize]`，即使换一次编译、`GlobalContext` 重建了，同一 `FontInstance` 的转换结果仍被缓存——这是 u4-l1/u5（将讲）的增量编译机制在导出层的延伸；
3. **序列化层子集化**（krilla）：typst-pdf 源码里搜不到 "subset" 一词——只嵌入「实际用到的字形」这个重活是 krilla 在 `document.finish()` 序列化时完成的。typst-pdf 的职责止于「把正确的字体数据和字形 ID 交给 krilla」。

图像同理：`convert_raster` 产出 `krilla::image::Image`（krilla 侧按值去重），`image_to_spans` 记住「图像 → 首次出现的 Span」，让后续校验错误能指回源码。

#### 4.3.2 核心流程

文本图元的转换路径：

```text
TextItem { font: FontInstance, size, fill, stroke, text, glyphs: Vec<Glyph> }
  ├─ tags::text(...)                     # 先开标签（4.4 节）
  ├─ convert_font(font)                  # 查 fonts_forward；未命中 → build_font
  │    └─ build_font（comemo::memoize）
  │         data 字节 + index + variations ──→ krilla Font::new_variable
  ├─ paint::convert_fill / convert_stroke # 填充描边（渐变需要 State 的容器信息）
  ├─ surface.push_transform(当前累积变换)
  └─ surface.draw_glyphs(原点, glyphs, font, text, size)
       # glyphs 是 &PdfGlyph —— 对 Glyph 的零成本重新解释
```

`PdfGlyph` 是 `#[repr(transparent)]` 包装：`t.glyphs.as_slice()` 通过 `TransparentWrapper::wrap_slice` 直接把 `&[Glyph]` 视作 `&[PdfGlyph]`，再为 `PdfGlyph` 实现 krilla 的 `Glyph` trait（glyph_id、text_range、x_advance、x_offset 四个方法）。零拷贝、零分配。

图像图元的分派：

```text
match image.kind():
  Raster → EXIF 方向变换 → convert_raster → draw_image（登记 image_to_spans）
  Svg    → draw_svg（krilla_svg，开启 embed_text）
  Pdf    → draw_pdf_page（嵌入 PDF 的某一页）
```

#### 4.3.3 源码精读

`handle_text` 主体：[crates/typst-pdf/src/text.rs:L18-L60](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/text.rs#L18-L60)。注意两点：`surface.push_transform(&fc.state().transform().to_krilla())` 把 4.2 节维护的累积矩阵交给 krilla；`defer(surface, |s| s.pop())` 是 u3-l5 出现过的 RAII 惯用法——用守卫保证变换一定被弹出，中途 `?` 提前返回也不会泄漏。

字体转换与缓存：[crates/typst-pdf/src/text.rs:L62-L76](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/text.rs#L62-L76) 的 `convert_font` 先查 `fonts_forward`，未命中才构建并双向登记（backward 表供错误消息反查字体名，见 4.4 节的 `display_font`）。真正的构建在 [L78-L104](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/text.rs#L78-L104)：

```rust
#[comemo::memoize]
fn build_font(typst_font: FontInstance) -> SourceResult<krilla::text::Font> {
    let font_data: Arc<dyn AsRef<[u8]> + Send + Sync> =
        Arc::new(typst_font.data().clone());
    let variations = ...;  // 可变字体的变化轴设置
    match krilla::text::Font::new_variable(font_data.into(), typst_font.index(), &variations) {
        Some(f) => Ok(f),
        None => bail!(Span::detached(), "failed to process {}", ...),
    }
}
```

值得体会的设计：`FontInstance` 是可哈希的「字体实例」身份（含变化轴），所以既能在 `FxHashMap` 里做进程内去重，也能做 comemo 跨编译的 memoize 键；字体字节用 `Arc` 共享，克隆不复制数据。`Font::new_variable` 让 krilla 在可变字体上按需实例化变化轴——同一个可变字体文件的不同字重会得到不同的 krilla Font。

字形适配器：[crates/typst-pdf/src/text.rs:L106-L130](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/text.rs#L106-L130)。`TransparentWrapper` 派生让 `&[Glyph]` 无拷贝地当 `&[PdfGlyph]` 用；`x_advance` 的注释还专门解释了不用 `Em::at` 的原因（它带一次「结果是否有限」的检查，逐字形调用太贵）——导出层对性能的敏感可见一斑。

图像三分支：[crates/typst-pdf/src/image.rs:L24-L80](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/image.rs#L24-L80)。开头的 `surface.set_location(span.into_raw())`（[L33](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/image.rs#L33)）是 4.4 节错误定位机制的另一半：把 Span 的原始值挂到 krilla 的绘制位置上，krilla 校验失败时能带着 Location 回来，`to_span` 再还原成 Span。光栅分支里 `image_to_spans` 的「首见登记」（[L56-L58](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/image.rs#L56-L58)）配合 `GlobalContext.image_spans`（[convert.rs:L289-L292](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L289-L292)）使用。SVG 分支交给 `krilla_svg` 并开启 `embed_text`（文字仍作为文本嵌入而非曲线）；PDF 分支直接嵌入源 PDF 的指定页。

填充与描边（渐变/pattern）：[crates/typst-pdf/src/paint.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/paint.rs) 的 `convert_fill`/`convert_stroke` 接收 `fc.state()`——即 4.2 节 `State` 维护的容器变换与尺寸，用来把「相对容器的渐变」钉到正确的页面区域。本讲不展开其内部，读者可在 `handle_text` 的调用点（text.rs L28-L41）看到传参方式。

#### 4.3.4 代码实践

1. **实践目标**：验证「同一字体多次出现只嵌一份、且被子集化」，并把观察到的现象与源码缓存点对上。
2. **操作步骤**：
   - 编写文档（示例代码）：

     ```typst
     // fonts.typ（示例代码）
     = 标题用了同一字体
     正文第一段。*粗体* 是另一组字形。
     正文第二段，还是那个字体。
     ```

   - `cargo run -p typst-cli -- compile fonts.typ fonts.pdf`；
   - 运行 `pdffonts fonts.pdf`（poppler-utils 工具；没有则 `strings fonts.pdf | grep -i font` 粗看）。
3. **需要观察的现象**：`pdffonts` 只列出**一个**（Libertinus Serif 之类的默认字体）条目而非三行；`name` 列带 `ABCDEF+` 这样的子集前缀。
4. **预期结果**：条目唯一对应 `fonts_forward` 的去重；子集前缀证明嵌入的是「用到的字形的子集」——由 krilla 在序列化时完成（typst-pdf 源码中无 subset 逻辑，转换止步于 `build_font`）。中文字符会显著增大子集（字形多），可以再编译一篇纯英文文档对比 PDF 体积。`pdffonts` 的具体输出行为为待本地验证项。
5. 若不想装 poppler，可改用纯阅读实践：在 [text.rs:L66-L75](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/text.rs#L66-L75) 处确认「查缓存 → 未命中构建 → 双向登记」的三步，再在 [image.rs:L56-L58](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/image.rs#L56-L58) 处对比图像的「首见登记」策略差异（字体双向、图像单向，因为字体错误消息需要反向查名，图像只需要 span）。

#### 4.3.5 小练习与答案

**练习 1**：可变字体的不同字重会生成几个 PDF 字体？为什么？

**答案**：每个「字体 + 变化轴取值组合」（即每个不同的 `FontInstance`）各生成一个 krilla Font，因为 `build_font` 用 `Font::new_variable` 按变化轴实例化。它们可能共享同一份原始字体数据（`Arc` 克隆），但 krilla 序列化时会分别处理；子集化后通常各自成为独立的字体对象。

**练习 2**：`GlobalContext.image_to_spans` 为什么只记「第一次出现的 Span」？会导致什么可观察的缺陷？

**答案**：注释（convert.rs L286-L288）说明同一图像可以有多个 span，只存首个是为了错误报告「有个出处」即可。缺陷是：当同一图像的多次使用中某一次触发校验错误（比如透明度违规），报错只会指向第一次出现的位置，未必是出错的那一次——这是「够用的近似」在错误定位上的有意取舍。

**练习 3**：为什么 `PdfGlyph` 用 `#[repr(transparent)]` + `TransparentWrapper` 而不是直接为 `Glyph` 实现 krilla 的 trait？

**答案**：`Glyph` 定义在 typst-library，krilla 的 `Glyph` trait 是外部 crate 的 trait；受孤儿规则限制，typst-pdf 无法「为外部类型的外部 trait 实现」直接挂上去。透明包装是最轻的解法：`#[repr(transparent)]` 保证 `&[PdfGlyph]` 与 `&[Glyph]` 内存布局一致，`wrap_slice` 零拷贝完成「类型视图」切换——和 u2-l4 讲的 AST「类型化视图」是同一种思想。

### 4.4 标签树与校验错误本地化

#### 4.4.1 概念说明

两个看似无关的主题放在同一节，因为它们共享同一条「krilla ↔ Typst」边界：

**标签树**：tagged PDF 的结构树不是一次建好的，而是**伴随绘制过程增量生长**的。krilla 的 `Surface` 支持 `start_tagged`/`end_tagged`——绘制指令流里内嵌标记；typst-pdf 一边遍历 Frame 一边通过这些调用告诉 krilla「现在开始画的是个标题/段落/工件」，krilla 据此累积结构树，最后由 `document.set_tag_tree` 接收一棵 Typst 侧自建的总树完成装配。驱动信号就是 4.2 节看到的 `FrameItem::Tag(Start/End)`——realize 阶段种下的「书签」在这里发芽。

**错误本地化**：krilla 在 `finish()` 时才做规范校验（字符串超长、透明度违规、缺 alt 文本……），报出来的是自己的错误类型，既没有 Span 也没有 Typst 风格的提示。`finish` 与 `convert_validation_error` 负责把它们翻译成 `SourceDiagnostic`：带上（可能来自 `set_location` 的）Span、带上「怎么修」的 hint。这让用户看到的仍是 Typst 统一格式的诊断，而不是底层库的裸错误。

#### 4.4.2 核心流程

标签子系统的生命周期：

```text
init（convert 第 2 步）
  └─ options.tagged() ? tree::build(扫描全文档 Tag) : Tree::empty

绘制期（convert_pages / handle_frame 内的钩子）
  tags::page      → 页级：页面装饰标记为 Artifact
  tags::group     → 组级：按 FrameParent 进入/离开逻辑子节点
  tags::handle_start / handle_end ← FrameItem::Tag(Start/End) 且 flags.tagged
  tags::tiling    → pattern 内部禁用标签
  tags::text/image/... → 各图元开自己的内容标签

resolve（convert 第 10 步）
  └─ 产出 (文档语言, 结构树 Tree)
       └─ document.set_tag_tree(tree)
```

错误翻译的路径：

```text
document.finish() → Result<Vec<u8>, KrillaError>
  ├─ Ok(bytes) → 返回
  └─ Err(err) → match err:
       Font(...)          → "failed to process 字体名" + hints
       Validation(vec)    → 逐条 convert_validation_error（本节主角）
       Image/Pdf/Limit/…  → 各自映射为带 hint 的 bail!
```

校验错误条目的通用形态：`"{标准名} error: {描述}"` 前缀 + 若干 hint。`to_span(loc)` 把 krilla 回传的 `Location`（就是 `set_location` 挂上去的 Span 原始值）还原成 Span——没有位置信息的错误退化为 `Span::detached()`。

#### 4.4.3 源码精读

标签系统入口：[crates/typst-pdf/src/tags/mod.rs:L29-L39](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/tags/mod.rs#L29-L39) 的 `init` 按 `tagged()` 决定 `tree::build`（预扫描）还是 `Tree::empty`。页面钩子 [L77-L97](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/tags/mod.rs#L77-L97)：若当前处于某个「工件」（页眉页脚类装饰）上下文，整页绘制包进 `ContentTag::Artifact`——屏幕阅读器会跳过工件。组钩子 [L57-L75](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/tags/mod.rs#L57-L75) 把 `FrameParent`（谁包住了这个组）转成「进入逻辑子节点」，这正是 `handle_group` 递归时标签树能跟着帧嵌套结构同步生长的原因。`tags/` 目录四个子模块各司其职：`tree`（构建状态机与预扫描）、`context`（Tags 上下文与图元级钩子）、`groups`（GroupId 分组）、`resolve`（最终装配出树与文档语言）。

绘制侧的消费点回到 4.2 节的 match：[crates/typst-pdf/src/convert.rs:L375-L384](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L375-L384)。`Tag::Start(_, flags)`/`Tag::End(_, _, flags)` 只有在 `flags.tagged` 为真时才触发 `tags::handle_start/handle_end`——u4-l7 讲过 Tag 的 flags：不是所有标签都要进 PDF 结构树（有的只服务内省），导出层据此过滤。

错误翻译总入口 `finish`：[crates/typst-pdf/src/convert.rs:L437-L526](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L437-L526)。看两处细节：`Font` 错误用 `display_font(gc.fonts_backward.get(&f))` 反查字体名（4.3 节 backward 表的用途）；`Validation` 分支把 krilla 的错误列表逐条 map 后 `collect` 成 `EcoVec<SourceDiagnostic>` 一次返回——多条校验错误能同时呈给用户。

`convert_validation_error` 是一张巨大的「错误 → 诊断」翻译表：[crates/typst-pdf/src/convert.rs:L529-L858](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L529-L858)。规格里点名的 `MissingFieldAltName`：

```rust
ValidationError::MissingFieldAltName(loc) => {
    let span = to_span(*loc);
    error!(
        span, "{prefix} missing alt name";
        hint: "make sure your form fields have an alt name";
    )
}
```

（[L727-L733](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L727-L733)）——表单字段缺替代名称时，前缀 `{prefix}` 会展开成触发它的标准名（如 `PDF/UA-1 error:`），hint 告诉用户改什么。邻近的 `MissingAltText`（[L720-L726](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L720-L726)，图片/公式缺 alt 文本）、`NoDocumentLanguage`（[L734-L738](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L734-L738)）、`NoDocumentTitle`（[L755-L759](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L755-L759)）都是 PDF/UA 校验的常客。表里还有两类值得注意：

- 带 `loc` 的错误（如 `Transparency`，[L644-L671](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L644-L671)）会用 `gc.image_spans.contains(&span)` 区分「图像透明」还是「填充/描边透明」，给出不同的 hint——`image_spans` 登记表在这里兑现价值；
- `RequiresNewerPdfVersion`（[L795-L856](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L795-L856)）甚至会遍历 `gc.options.validators()` 找出「最限制版本的标准」，据此在三种 hint 策略里选择——这是一段「教用户调 PDF 版本与标准组合」的交互式错误处理。

Span 还原桥：[crates/typst-pdf/src/convert.rs:L861-L863](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L861-L863) 的 `to_span`，一行 `loc.map(Span::from_raw).unwrap_or(Span::detached())`。与 image.rs 的 `set_location(span.into_raw())` 首尾呼应，构成「带位置绘制 → 带位置报错」的完整回路。

元数据写入点（呼应 2.5 节）：[crates/typst-pdf/src/metadata.rs:L9-L53](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/metadata.rs#L9-L53) 的 `build_metadata`。`/Creator` 的默认值在 [L25-L32](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/metadata.rs#L25-L32)：`format!("Typst {}", typst_utils::version().raw())`；`language` 永远会写一个（[L10-L12](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/metadata.rs#L10-L12) 注释说明 PDF/UA-1 隐式要求文档语言）；创建日期的三级优先级（文档显式日期 > 选项时间戳 > 不写）见 [L59-L99](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/metadata.rs#L59-L99)。

#### 4.4.4 代码实践

1. **实践目标**：触发一条真实的 krilla 校验错误，观察它被翻译后的完整形态（前缀 + Span 指向 + hint），再对照源码找到翻译它的那一行。
2. **操作步骤**：
   - 编写文档（示例代码）：

     ```typst
     // a11y.typ（示例代码）
     #set pdf(standard: "a-2a")   // 无障碍存档标准，强制标签与标题
     没有设置标题的文档。
     ```

   - `cargo run -p typst-cli -- compile a11y.typ a11y.pdf`；
   - 若上面顺利编过，再试 `#set pdf(standard: "a-1b")` 并把正文换成一张不带 alt 的 `image("x.png", alt: none)` 场景（无现成图片可跳过）。
3. **需要观察的现象**：编译失败，错误形如 `PDF/A-2a error: missing document title`，hint 为 `set the title with 'set document(title: ...)'`，且错误发生在**导出阶段**而非排版阶段（`--timings` 里能看到 pdf 阶段被计时）。
4. **预期结果**：这条错误正是 [convert.rs:L755-L759](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L755-L759) 的 `NoDocumentTitle` 分支产出；`PDF/A-2a error:` 前缀来自 `failing_validators.to_comma_list()`（[L535](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L535)）。补上 `#set document(title: [我的文档])` 后应能编译通过。`standard` 的合法取值（如 `"a-2a"`、`"ua-1"`）以 [crates/typst-pdf/src/format.rs:L727](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/format.rs#L727) 起的 `PdfStandard` 枚举 `#[string(...)]` 标注为准。
5. 第二个场景（图片 alt）是否触发取决于图像内容，属待本地验证项；即便不触发，也可在 [convert.rs:L720-L726](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L720-L726) 读到对应分支。

#### 4.4.5 小练习与答案

**练习 1**：`pdf()` 返回 `Warned<SourceResult<Vec<u8>>>`，「警告」和「错误」在导出层各对应什么场景？

**答案**：错误（`SourceResult::Err`）让编译失败，如 `finish` 翻译出的字体处理失败、校验违规；警告不阻断输出，如 4.1 节「页面范围禁用标签化」由 `resolve` 推入 `warnings`。二者最终都汇入 `Warned` 由调用方（CLI）分别渲染。

**练习 2**：为什么标签树的构建要「预扫描 + 绘制期增量」两段式，而不是绘制完再从 Frame 重建一遍？

**答案**：`init` 里的 `tree::build` 预扫描可以先验地建立整棵结构树的骨架与编号（`resolve` 再最终装配），绘制期的 `handle_start`/`handle_end`/`group` 钩子则让结构节点与 krilla 的绘制指令流（`start_tagged`/`end_tagged`）在时间上对齐——PDF 结构树必须与内容流的标记一一对应，边画边标记是最不容易错位的方式。若绘制后再重建，需要再次遍历 Frame 并复现所有嵌套规则，等于把状态机写两遍。

**练习 3**：`MissingFieldAltName`（表单字段缺替代名称）在当前 Typst 里实际能被触发吗？源码怎么看出来？

**答案**：大概率触发不了。该分支上方 [convert.rs:L711-L712](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L711-L712) 的注释写明「以下错误目前还不会发生，只有等 Typst 完整支持 PDF/A 与 PDF/UA 才会出现」，但仍然写了消息以防万一。同一注释下的 `MissingAnnotationAltText`、`MissingHeadingTitle` 等同属此类——防御性翻译，保证 krilla 未来扩展校验时用户不会看到未本地化的错误。

## 5. 综合实践

**任务：给一份 PDF 做「解剖报告」，把每项观察结论钉回源码。**

1. 准备一份「五脏俱全」的文档（示例代码）：

   ```typst
   // report.typ（示例代码）
   #set document(title: [解剖报告], author: ([你]), date: datetime(year: 2026, month: 1, day: 1))
   #set page(width: 160pt, height: auto, numbering: "1")
   #set text(lang: "zh")

   = 引言 <intro>

   这是正文，包含 #link("https://typst.app")[外链] 与 *强调*。

   #figure(image("../../docs/assets/files/rick.jpg"), caption: [示例图]) <fig>

   见 @intro 与 @fig。

   #pagebreak()
   第二节：分页之后。
   ```

   （图片路径换成你本地任意一张图；没有就删掉 figure 与 `@fig`。）
2. `cargo run -p typst-cli -- compile report.typ report.pdf`，然后逐项检查并填写下表：

   | 观察 | 命令 | 结果 | 源码定位 |
   | --- | --- | --- | --- |
   | 页数 | `pdfinfo report.pdf \| grep Pages` | 2 | `convert_pages` 的循环（[convert.rs:L100-L176](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/convert.rs#L100-L176)） |
   | Creator | `pdfinfo report.pdf \| grep Creator` | `Typst 0.15.1` | [metadata.rs:L25-L32](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/metadata.rs#L25-L32) |
   | Producer | `pdfinfo report.pdf \| grep Producer` | krilla 写入（Typst 源码中无写入点） | ——（krilla 内部） |
   | 标题/作者/日期 | `pdfinfo report.pdf` | 与 `#set document` 一致 | [metadata.rs:L34-L48](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/metadata.rs#L34-L48) |
   | 字体子集 | `pdffonts report.pdf` | 带子集前缀的条目 | [text.rs:L78-L104](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/text.rs#L78-L104) + krilla 序列化 |
   | 书签 | `pdfinfo report.pdf \| grep -i topic` 或阅读器大纲 | 「引言」一级条目 | [outline.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/outline.rs) 与 `collect_named_destinations` |
   | 结构树 | `pdfinfo -struct report.pdf`（或 `mutool show report.pdf structreeroot`） | 标题/段落/figure 节点 | `tags::init` 起的全链路（4.4 节） |
3. 最后做一次对照实验：`--pages 1` 重新导出，验证警告出现、Pages 变 1、`-struct` 输出消失（页面范围禁用标签化）。
4. 预期：表中每行都能「观察 → 源码」双向对上；Producer 与 `-struct` 的具体输出格式因工具版本而异，属待本地验证项。

## 6. 本讲小结

- 导出入口 `typst_pdf::pdf()` 是一层薄门面：真正的装配在 `convert::convert`——「解析选项 → 初始化标签 → 建 krilla 文档 → 逐页绘制 → 挂书签/元数据/结构树 → finish 序列化」。
- 遍历骨架是三层递归：`convert_pages` 走页面，`handle_frame` 走帧内 `(Point, FrameItem)`，`handle_group` 下钻子帧；`State`/`FrameContext`/`GlobalContext` 分别管变换与渐变容器、单页状态栈、全局资源缓存。
- 资源嵌入的关键词是去重与分层：字体经 `fonts_forward` 缓存 + `#[comemo::memoize]` 的 `build_font` 跨编译记忆，图像「首见登记」span；**字体子集化发生在 krilla 序列化层，typst-pdf 只负责翻译**。
- tagged PDF 由绘制流内嵌标记增量构建：realize 种下的 `FrameItem::Tag(Start/End)` 在导出层被 `tags::` 系列钩子消费，最终 `set_tag_tree` 装配；默认开启，页面范围导出会禁用。
- krilla 的校验错误在 `convert_validation_error` 被逐条本地化成「标准名前缀 + Span + hint」的 Typst 诊断；`set_location`/`to_span` 构成带位置绘制的完整回路。
- 架构复现了 u4-l1 的结论：typst-pdf 只依赖 `PagedDocument` 抽象（页面、内省器、格式选项），不依赖求值与排版的任何内部结构。

## 7. 下一步学习建议

- 下一讲 u5-l2 转向另外两个导出器：`typst-render`（Frame → 像素）与 `typst-svg`（Frame → 矢量），对比三种导出器对同一 `Frame` 抽象的不同消费方式，你会再次看到 `fill_or_white()` 与 `fill_or_transparent()` 的分工。
- 想深入无障碍：通读 [crates/typst-pdf/src/tags/](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/tags/mod.rs) 的 `tree/` 与 `resolve/` 子模块，配合 PDF/UA-1 规范理解每类 `ContentTag` 的语义。
- 想理解 krilla 边界：对照 `Cargo.lock` 中 krilla 的 git 依赖（0.8.2），浏览其仓库的 `Surface`/`Document` API，体会 Typst「翻译层」与「序列化层」的解耦设计。
- 若对 `#set pdf(...)` 如何从脚本层流到导出器感兴趣，可预习 u5-l3（Format 注册机制）：从 [crates/typst-pdf/src/format.rs:L101-L102](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-pdf/src/format.rs#L101-L102) 的 `#[elem(name = "pdf")]` 开始反向追到 `DocumentFormatOptions`。
