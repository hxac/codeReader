# 目录结构、模块地图与构建运行方式

> 永久链接 base：`https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/`
> 下文所有源码链接均拼接在该 base 之后，例如 `src/lib.rs#L3-L14`。

## 1. 本讲目标

前三讲我们读了 `Cargo.toml`、`lib.rs` 的公共 API（`pdf()` / `pdf_in_bundle()` / `PdfOptions`）和 PDF 标准（`PdfStandards` / `PdfStandard`）。本讲把镜头拉远到**整张地图**：

- 看清 `src/` 下一共有哪些模块、各自负责什么。
- 把 `convert.rs` 当作「调度中心」，搞清楚它按什么顺序调用其它 11 个子模块。
- 理解 `tags/`（无障碍结构树）子系统的内部五段划分。
- 知道如何在 typst 这个 Cargo workspace 里编译、检查、测试 `typst-pdf` 这一个 crate。

学完后，你应该能**不看源码，画出一张以 `convert()` 为中心的模块依赖草图**，并标注 `tags/` 五个子目录的职责。

## 2. 前置知识

### 2.1 Rust 模块系统（一句话回顾）

一个 crate 的「门面」通常写在 `src/lib.rs`（库）或 `src/main.rs`（可执行）里。门面文件用 `mod xxx;` 声明子模块：

- `mod foo;`：编译器会去找 `src/foo.rs` 或 `src/foo/mod.rs`。
- `pub mod foo;` 或 `pub use`：对外暴露；不带 `pub` 则是**私有内部模块**，只有 crate 内部可见。

`typst-pdf` 是一个**库 crate**（没有 `main.rs`），它的对外接口极少（只有 `pdf()` / `pdf_in_bundle()` / `PdfOptions` / `PdfStandards` / `Timestamp` / `Timezone`），绝大多数模块都是私有的——这是典型的「薄门面 + 厚后厨」结构。

### 2.2 Cargo workspace

typst 不是一个独立的 crate，而是一个**工作空间（workspace）**：根 `Cargo.toml` 写着 `members = ["crates/*", ...]`，即 `crates/` 下每个目录都是一个成员 crate，`typst-pdf` 是其中之一。在一个 workspace 里，`cargo build` 默认只编译 `default-members`（typst 的是 `crates/typst-cli`）；想只动某一个成员，要用 `cargo <命令> -p typst-pdf`。

### 2.3 承接前三讲的关键结论

- `typst-pdf` 是**适配器层**，不自己拼 PDF 字节，而是调用第三方库 `krilla`（见 u1-l1）。
- 输入是 `typst-layout` 产出的 `PagedDocument`（一棵 `Frame` / `FrameItem` 树），输出是 `Vec<u8>`（见 u1-l1）。
- 公共入口 `pdf()` / `pdf_in_bundle()` 都只是对 `convert::convert()` 的一行委托（见 u1-l2）。
- `PdfOptions::tagged` 默认为 `true`，会驱动无障碍结构树；这与 PDF/UA 校验是两回事（见 u1-l3）。

## 3. 本讲源码地图

本讲精读以下文件，外加 workspace 根 `Cargo.toml`：

| 文件 | 角色 | 本讲用它来 |
| --- | --- | --- |
| `src/lib.rs` | 门面：模块声明 + 公共 API | 看 12 个 `mod` 声明，建立顶层地图 |
| `src/convert.rs` | 编排核心：`convert()` 调度一切 | 看它调用哪些子模块、按什么顺序 |
| `src/tags/mod.rs` | 无障碍子系统入口 | 看 `tags/` 的 5 个子模块划分与三段式流程 |
| `Cargo.toml`（crate） | 本 crate 的依赖清单 | 确认依赖与构建方式 |
| `Cargo.toml`（workspace 根） | 工作空间成员与版本 | 确认 `-p typst-pdf` 的依据 |

## 4. 核心概念与源码讲解

### 4.1 顶层模块地图：lib.rs 的 mod 声明与 12 个子模块

#### 4.1.1 概念说明

一个 crate 的「骨架」就藏在门面文件顶部的 `mod` 声明里。`typst-pdf` 在 `src/lib.rs` 一开篇就用 12 行 `mod` 把所有内部模块挂了出来，紧接着一句 `pub use` 决定对外暴露什么。

这里的两个关键设计：

1. **私有为主**：这 12 个 `mod` 都不带 `pub`，说明 `convert` / `page` / `text` / `shape` / `paint` / `image` / `link` / `outline` / `metadata` / `attach` / `tags` / `util` 全是 crate 内部实现细节，外部用户根本看不到。
2. **薄对外接口**：整份 `lib.rs` 只对外 `pub use` 了 `Timestamp` / `Timezone`（来自 `metadata` 模块），加上文件里直接定义的 `pdf()` / `pdf_in_bundle()` / `PdfOptions` / `PdfStandards` / `PdfStandard`。

#### 4.1.2 核心流程

门面文件的组装流程：

```text
src/lib.rs
  ├── 1. 12 个 mod 声明（attach/convert/image/link/metadata/outline/page/paint/shape/tags/text/util）
  ├── 2. pub use metadata::{Timestamp, Timezone}   ← 对外只露这两个类型
  ├── 3. 公共入口函数：pdf()、pdf_in_bundle()       ← 都委托给 convert::convert()
  └── 4. 公共配置类型：PdfOptions / PdfStandards / PdfStandard
```

一句话概括这 12 个模块的角色：

| 模块 | 职责（一句话） |
| --- | --- |
| `convert` | **总调度**：把 `PagedDocument` 编排成 `krilla` 调用序列并 `finish()` 出字节 |
| `page` | 页面尺寸、出血框（bleed）、页码标签 |
| `text` | 文字与字体（`handle_text`、字体缓存、字形适配） |
| `shape` | 图形与几何（`handle_shape`、`convert_geometry`） |
| `paint` | 颜色、渐变、平铺图案（纯色 / 色彩空间 / gradient / pattern） |
| `image` | 栅格图、SVG、嵌入 PDF（`handle_image`） |
| `link` | 链接注记与目的地址（`handle_link`） |
| `outline` | 书签大纲（`build_outline`） |
| `metadata` | 元数据与时间戳（`build_metadata`、`Timestamp`、`Timezone`） |
| `attach` | 文件附件（`attach_files`） |
| `tags` | 无障碍结构树（tagged PDF，下文 4.3 详述） |
| `util` | 类型转换工具集（Typst 几何 → krilla 几何、路径转换等） |

其中 `convert` 是「大脑」，`util` 是「公共工具箱」被大家复用，其余 9 个是「内容翻译器」——每个负责把一类 Typst 内容翻译成 krilla 调用。

#### 4.1.3 源码精读

12 个模块声明集中在文件顶部：

[src/lib.rs:L3-L14](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L3-L14) —— crate 的全部内部子模块；注意它们全部不带 `pub`，是私有实现。

紧接着的对外导出，全 crate 仅此一句 `pub use`：

[src/lib.rs:L16](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L16) —— 只把 `metadata` 模块里的 `Timestamp` / `Timezone` 暴露给外部。

而两个公共入口都只有一行函数体，把活儿全交给 `convert`：

[src/lib.rs:L35-L38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L35-L38) —— `pdf()` 直接返回 `convert::convert(document, options, &[], None)`（独立导出：无锚点、无跨文档链接解析器）。

[src/lib.rs:L46-L54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L46-L54) —— `pdf_in_bundle()` 多传了 `anchors`（命名目的地址）和 `link_resolver`（跨文档链接解析），用于打包导出（见 u1-l2）。

> 结论：**`lib.rs` 是接待大厅，`convert.rs` 才是后厨**。门面只负责收参数、转交。

#### 4.1.4 代码实践

**实践：盲猜模块职责，再回源码核对。**

1. 实践目标：在尚未深入每个模块前，先凭名字建立直觉。
2. 操作步骤：
   - 打开 [src/lib.rs:L3-L14](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L3-L14)。
   - 针对每个 `mod` 名，先**不查源码**，用一句话写下你认为它做什么。
   - 然后用编辑器跳转到对应文件（如 `src/paint.rs`、`src/attach.rs`），看顶部文档注释或第一个 `pub(crate) fn`，核对猜测。
3. 需要观察的现象：哪些模块名一看就懂（如 `image`、`text`），哪些需要看源码才能确定（如 `paint` 到底管纯色还是渐变、`convert` 到底是总调度还是某种转换）。
4. 预期结果：核对后你会发现 `paint` 同时管纯色、渐变、平铺图案；`convert` 是总调度而非「转换函数集合」。这能帮你校正心智模型。

#### 4.1.5 小练习与答案

**练习 1**：为什么这 12 个 `mod` 都不带 `pub`？如果改成 `pub mod convert;` 会怎样？
**答案**：因为它们是实现细节，外部用户只该用 `pdf()` / `PdfOptions` 等高层接口。若加 `pub`，会把 krilla 相关类型暴露到公开 API，破坏封装，也会让升级 krilla 成为破坏性变更。

**练习 2**：`lib.rs` 里 `pub use` 的只有 `Timestamp` / `Timezone` 两个类型，但用户明明还能用到 `PdfOptions` / `PdfStandard`，为什么后者不在 `pub use` 列表里？
**答案**：`pub use` 只用于**重新导出别的模块里的项**。`PdfOptions` / `PdfStandards` / `PdfStandard` 是直接定义在 `lib.rs` 本身的（带 `pub struct` / `pub enum`），不需要 `pub use`；而 `Timestamp` / `Timezone` 定义在 `metadata` 模块，所以要 `pub use` 提升到 crate 根。

**练习 3**：下列哪一项**不是**「内容翻译器」？`text`、`paint`、`image`、`convert`、`attach`。
**答案**：`convert`。它是总调度（编排所有翻译器）；其余四个都是把某一类 Typst 内容翻译成 krilla 调用的翻译器。

---

### 4.2 编排核心 convert.rs：convert() 如何串联各子模块

#### 4.2.1 概念说明

`convert.rs` 用的就是**编排者模式（orchestrator）**：它自己几乎不处理任何具体内容（不画线、不嵌字、不上色），而是按固定顺序：

1. 准备 krilla 文档与序列化设置；
2. 收集命名目的地址；
3. 初始化 tags；
4. 构建一个贯穿全程的 `GlobalContext`；
5. 转换所有页面（这里才真正分发到各翻译器）；
6. 附加文件；
7. 解析 tags 树；
8. 设置大纲 / 元数据 / 结构树；
9. 收尾 `finish()` 输出字节。

理解这条主线，就等于拿到了整个 crate 的「目录索引」——后续每一讲都只是展开其中一步。

#### 4.2.2 核心流程

`convert()` 的执行流水线（箭头表示先后顺序，括号是负责的子模块）：

```text
convert(typst_document, options, anchors, link_resolver)
  │
  ├─① SerializeSettings            ← 由 options.pretty/tagged/standards 组装
  ├─② Document::new_with(settings) ← 创建空 krilla 文档
  ├─③ PageIndexConverter::new      ← page_ranges 页号重映射
  ├─④ collect_named_destinations   ← 内部函数，调用 link::pos_to_xyz
  ├─⑤ tags::init                   ← (tags) 预构建逻辑树 或 空树
  ├─⑥ GlobalContext::new           ← 全局缓存（字体/图像/位置→命名目的地）
  │
  ├─⑦ convert_pages                ← (page) 逐页：
  │        └─ handle_frame         ← 递归遍历 Frame 树，按 FrameItem 分派：
  │              ├─ Group  → handle_group   (convert + tags::group)
  │              ├─ Text   → handle_text    (text)
  │              ├─ Shape  → handle_shape   (shape)
  │              ├─ Image  → handle_image   (image)
  │              ├─ Link   → handle_link    (link)
  │              └─ Tag    → tags::handle_start / handle_end  (tags)
  │
  ├─⑧ attach_files                 ← (attach) 文件附件
  ├─⑨ tags::resolve                ← (tags) 解析为 krilla TagTree + PDF/UA 校验
  ├─⑩ document.set_outline         ← (outline) build_outline
  ├─⑪ document.set_metadata        ← (metadata) build_metadata
  ├─⑫ document.set_tag_tree        ← (tags) 挂上结构树
  └─⑬ finish                       ← (convert) krilla finish() → Vec<u8>，错误映射
```

注意 `util` 模块没有出现在主线箭头里，因为它**被几乎所有翻译器复用**（类型转换、路径转换），属于横向工具层。

#### 4.2.3 源码精读

`convert()` 的完整签名与主体，逐段对应上面的流水线：

[src/convert.rs:L47-L95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L47-L95) —— `convert::convert()`，整个 crate 的总入口（注意 L47 的 `#[typst_macros::time]` 用于编译计时）。

其中每一步的关键行：

- **① 序列化设置**：[src/convert.rs:L54-L64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L54-L64) —— `SerializeSettings` 里 `compress_content_streams: !options.pretty`、`enable_tagging: options.tagged`、`configuration: options.standards.config`，把 u1-l2 / u1-l3 讲过的选项灌进 krilla。
- **② 创建文档**：[src/convert.rs:L66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L66) —— `Document::new_with(settings)` 得到一个空 krilla 文档。
- **③④ 页号转换 + 命名目的地**：[src/convert.rs:L67-L73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L67-L73) —— `PageIndexConverter` 处理「只导出部分页」，`collect_named_destinations` 收集书签/锚点目的地。
- **⑤ 初始化 tags**：[src/convert.rs:L75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L75) —— `tags::init(typst_document, options)?`，注意 `?`：如果用户同时开了 tagged 和 page_ranges，这里会直接报错返回（见 4.3）。
- **⑥ 全局上下文**：[src/convert.rs:L77-L84](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L77-L84) —— `GlobalContext::new(...)`，它是贯穿全程的「大背包」。
- **⑦ 转换页面**：[src/convert.rs:L86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L86) —— `convert_pages(&mut gc, &mut document)?`，逐页调用 `handle_frame`。
- **⑧ 附件**：[src/convert.rs:L87](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L87) —— `attach_files(&gc, &mut document)?`。
- **⑨ 解析 tags**：[src/convert.rs:L88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L88) —— `tags::resolve(&mut gc)?` 同时返回文档语言 `doc_lang` 和结构树 `tree`。
- **⑩⑪⑫ 挂上大纲/元数据/结构树**：[src/convert.rs:L90-L92](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L90-L92) —— 三次 `document.set_*`。
- **⑬ 收尾**：[src/convert.rs:L94](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L94) —— `finish(document, gc, options.standards.config)`。

`handle_frame` 是 `convert_pages` 内部的「分派器」，把 `FrameItem` 各变体路由到对应翻译器，正是它把所有内容模块串了起来：

[src/convert.rs:L358-L385](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L358-L385) —— `match item` 分派：`Group→handle_group`、`Text→handle_text`、`Shape→handle_shape`、`Image→handle_image`、`Link→handle_link`、`Tag→tags::handle_start/handle_end`。

而 `GlobalContext` 这个「大背包」里装着字体缓存、图像到 span 的映射、位置→命名目的地映射等全局状态：

[src/convert.rs:L278-L301](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L278-L301) —— `GlobalContext` 字段一览（字体前向/反向缓存、`image_to_spans`、`loc_to_names`、`tags` 等）。

#### 4.2.4 代码实践

**实践：为 convert() 的每个阶段写「跳过会怎样」注释。**

1. 实践目标：用「删除这一步会出什么问题」的反向思维，加深对主线顺序的理解。
2. 操作步骤：
   - 打开 [src/convert.rs:L47-L95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L47-L95)。
   - 对 ①~⑬ 每一步，在笔记里写一句话：如果**删掉**这一步，输出 PDF 会缺什么或哪里会崩。
   - 例如 ⑥ `GlobalContext::new` 删掉 → 后续所有翻译器拿不到字体缓存、拿不到 tags，根本无法编译；⑫ `set_tag_tree` 删掉 → PDF 里没有无障碍结构树，屏幕阅读器读不到语义。
3. 需要观察的现象：哪些步骤是「质量下降」（如不设元数据也能出 PDF），哪些是「直接出错」（如跳过 `finish()` 根本没有字节）。
4. 预期结果：你能区分「必需步骤」（①②⑥⑦⑬）与「增强步骤」（⑩⑪⑫ 等可空）。`待本地验证`：可尝试在本地复制一份并注释掉某行观察编译/运行表现（仅学习用，勿提交）。

#### 4.2.5 小练习与答案

**练习 1**：`convert()` 里 `tags::init` 和 `tags::resolve` 为什么被**拆在** `convert_pages` 的前后两端，而不是放在一起？
**答案**：因为 tagged PDF 是「三段式」——`init` 先预构建一棵逻辑树（需要遍历整个文档的 Tag）；中间 `convert_pages` 在绘制内容的同时，按遍历顺序发射 krilla 标记内容（Span/Artifact）；最后 `resolve` 把逻辑树 + 已发射的标记解析成 krilla `TagTree` 并做 PDF/UA 校验。所以 `init` 必须在绘制前，`resolve` 必须在绘制后。

**练习 2**：`util` 模块为什么没出现在 4.2.2 的主线箭头里？
**答案**：`util` 是横向工具层（类型转换 trait、`convert_path` 等），被 `text`/`shape`/`paint`/`image`/`link` 等多个翻译器复用，不属于「按顺序执行的一个阶段」，所以不在主线流水线上。

**练习 3**：`finish()` 为什么需要传入 `gc`（按值）和 `options.standards.config`？
**答案**：`finish()` 调用 krilla 的 `document.finish()`，失败时要把 krilla 的错误（字体/图像/校验等）翻译成带 span 的 Typst 诊断；这需要 `gc` 里的反向字体映射 `fonts_backward`、`image_to_spans` 等来定位错误来源，也需要 `config.version()` 来生成版本相关的 hint。

---

### 4.3 tags/ 无障碍子系统：五个子模块的分工

#### 4.3.1 概念说明

`tags/` 是全 crate 最复杂的子系统，负责生成 **tagged PDF**（带逻辑结构树的 PDF，供屏幕阅读器、PDF/UA 无障碍校验使用）。它独立成子目录，内部又分成 5 个模块。

它的总体策略是**三段式**（这正好对应 4.2 里 `init` → 绘制时发射 → `resolve` 的三处调用）：

1. **build（构建）**：在 `init` 阶段预遍历整棵 Frame 树，把每个 Typst 元素（标题/列表/表格/图片…）映射成一个逻辑分组，构建出一棵「逻辑结构树」。注意：**Frame 树 ≠ 逻辑结构树**（一个表格在 Frame 里是一堆分散的线/字，但在逻辑上是一个 Table），所以需要这遍预处理。
2. **tree（遍历状态机）**：在 `convert_pages` 绘制内容时，用一个状态机（`progressions`/`breaks`/`unfinished` 三条游标）按顺序开关标签，并处理「重叠标签拆分」「逻辑子节点位置切换」。
3. **resolve（解析+校验）**：把逻辑树解析成 krilla `TagTree`，并做 PDF/UA 结构合法性校验（如表格子元素约束、标题层级连续）。

#### 4.3.2 核心流程

`tags/` 的五段划分与三段式的对应关系：

```text
src/tags/mod.rs ── 子系统入口：init / disabled / 各类 hook（text/image/shape/page/tiling/group）
   │
   ├── groups      【数据模型】核心结构 Groups / Group / GroupKind（located/virtual/weak 三类分组）
   ├── tree        【构建 + 遍历】
   │     ├── build  ← 阶段①：预遍历构建逻辑树（元素→GroupKind 映射、重叠拆分）
   │     ├── mod    ← 阶段②：遍历状态机（step_start_tag/step_end_tag/close_group）
   │     └── text   ← 文本属性解析辅助
   ├── context     【结构化上下文】表格/列表/图表/网格/大纲的专用上下文（生成符合规范的标签嵌套）
   ├── resolve     【解析+校验】
   │     ├── mod         ← 阶段③：resolve() 把 Group 树 → krilla TagTree + validate_children
   │     └── accumulator ← 把标记内容包进 Span
   └── util        【工具】idvec（带 id 的向量）、prop（属性）等内部数据结构
```

门控逻辑也很关键：tagged PDF **默认开启**（`PdfOptions::tagged == true`），但有两个禁用条件——用户主动关闭，或正处于 tiling（平铺图案）内部（图案里的内容不算文档语义）。

#### 4.3.3 源码精读

`tags/mod.rs` 顶部声明了 5 个子模块：

[src/tags/mod.rs:L22-L26](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs#L22-L26) —— `mod context; mod groups; mod resolve; mod tree; mod util;`，即五段划分。

子系统入口 `init()`，集中体现了「门控」和「构建/空树」两条分支：

[src/tags/mod.rs:L28-L39](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs#L28-L39) —— 若 `options.tagged` 为真且未设 `page_ranges`，则 `tree::build(...)` 构建逻辑树；否则用 `Tree::empty(...)`。**注意 L30-L31 的 `bail!`：同时开 tagged 和 page_ranges 直接报错。**

禁用判定（两种禁用条件合一）：

[src/tags/mod.rs:L147-L149](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs#L147-L149) —— `disabled(gc)` 返回 `!gc.options.tagged || gc.tags.in_tiling`。

绘制时的 hook 一览（每个都在 `disabled()` 时直接返回，不发射标记）：

[src/tags/mod.rs:L41-L55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs#L41-L55) —— `handle_start` / `handle_end`，被 `convert.rs` 的 `FrameItem::Tag` 分支调用（见 4.2.3）。

[src/tags/mod.rs:L199-L228](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs#L199-L228) —— `text` hook：更新 bbox、解析文本属性、用 `start_tagged` 发射一个带语言的 `Span` 标记并返回 `TagHandle`。`image`（[L230-L252](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs#L230-L252)）和 `shape`（[L254-L282](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs#L254-L282)）同理，分别发射带 alt 文本的 Span 与 Artifact 标记。

`init()` 的门控还有一个专门的测试：

[src/tags/mod.rs:L308-L321](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs#L308-L321) —— `tagged_and_page_range` 测试：同时设 `page_ranges` 与默认 `tagged=true`，断言 `tags::init` 报错信息为 `"cannot enable tagged PDF and export a page range"`。

#### 4.3.4 代码实践

**实践：把 tags/ 五个子模块贴到三段式上。**

1. 实践目标：把抽象的「init→发射→resolve」与具体文件对应起来。
2. 操作步骤：
   - 对照 4.3.2 的树状图，在五个文件名（`groups.rs`、`tree/build.rs`、`tree/mod.rs`、`resolve/mod.rs`、`context/mod.rs`）旁边各写一句话职责。
   - 然后跳进 `src/tags/tree/build.rs`、`src/tags/resolve/mod.rs`、`src/tags/context/mod.rs` 各看前 20 行的文档注释核对。
3. 需要观察的现象：`groups.rs` 是单文件（不是目录），而 `context` / `resolve` / `tree` / `util` 是目录——因为 `groups` 只是数据定义，体积小，没必要拆目录。
4. 预期结果：你能复述「build 构建逻辑树 → tree 遍历发射 → resolve 解析校验，context 在其中为表格/列表等结构提供专用嵌套规则，groups 是数据模型，util 是底层容器」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `tags::init` 要拒绝「同时开 tagged 和 page_ranges」？
**答案**：tagged PDF 的结构树与页面是一一对应的整体语义；只导出部分页面会破坏结构树的完整性（结构元素引用的页可能被裁掉），导致 PDF/UA 不合规。所以源码在 [src/tags/mod.rs:L30-L31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs#L30-L31) 直接 `bail!`。

**练习 2**：`disabled()` 有两个禁用条件，分别是什么？为什么 tiling 内部要禁用？
**答案**：条件是「用户关闭 tagged」或「正处于 tiling 内部」。平铺图案（Tiling）是用来重复填充区域的装饰，其内部的文字/图形不代表文档语义内容，若也发射结构标记会污染结构树，所以在 tiling 内一律禁用（见 [src/tags/mod.rs:L100-L142](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs#L100-L142) 的 `tiling` 函数，会把整个图案标成 Background artifact）。

**练习 3**：`groups` 是文件还是目录？`context` 呢？为什么有这种差别？
**答案**：`groups` 是单文件（`src/tags/groups.rs`），`context` 是目录（`src/tags/context/`，下含 table/list/figure/grid/outline 等多个文件）。差别源于体积与复杂度：`groups` 主要是数据模型定义，一个文件够用；`context` 要为多种结构（表格、列表、图表…）各写一套规则，自然拆成多文件。

---

### 4.4 构建与测试：在 typst workspace 中操作本 crate

#### 4.4.1 概念说明

`typst-pdf` 是 typst 工作空间的一个**库成员**（不是可执行程序）。workspace 根 `Cargo.toml` 写着 `members = ["crates/*", ...]`，而 `default-members = ["crates/typst-cli"]`——意味着在仓库根目录直接敲 `cargo build` 会去编 `typst-cli`（连带它的依赖，包括 `typst-pdf`），而不是单独把 `typst-pdf` 当目标。

要单独操作本 crate，用 `-p`（`--package`）指定包名：

| 命令 | 作用 |
| --- | --- |
| `cargo check -p typst-pdf` | 只做类型检查，最快 |
| `cargo build -p typst-pdf` | 编译本库（产出 `.rlib`） |
| `cargo test -p typst-pdf` | 跑本 crate 的单元测试 |
| `cargo doc -p typst-pdf --no-deps` | 只生成本 crate 的 rustdoc |

> 注意：本 crate **没有** `tests/` 集成测试目录，测试都是源码内联的 `#[cfg(test)] mod tests`（例如 4.3.3 提到的 `tagged_and_page_range` 在 `src/tags/mod.rs` 末尾）。所以 `cargo test -p typst-pdf` 实际跑的是这些内联单元测试。

#### 4.4.2 核心流程

构建流程：

```text
cd 到仓库根目录（typst/）
  └─ cargo check -p typst-pdf      # 类型检查：拉取 workspace 依赖（krilla、typst-library 等）
       ├─ 成功 → "Finished" + 警告（若有）
       └─ 失败 → 编译错误（行号指向 src/ 下某文件）

cargo test -p typst-pdf            # 运行内联单元测试
       └─ 例如 tags::tests::tagged_and_page_range ... ok
```

环境要求（来自 workspace 根 `Cargo.toml`）：

- Rust 工具链版本不低于 `rust-version = "1.92"`（注释提示 CI 里也要同步改）。
- edition = `"2024"`。

#### 4.4.3 源码精读

本 crate 的 `Cargo.toml` 全部依赖都是 `{ workspace = true }`，即从 workspace 根统一取版本：

[../../Cargo.toml（crate）L15-L36](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/Cargo.toml#L15-L36) —— `[dependencies]` 段，可见 `krilla` / `krilla-svg`（PDF 后端）、`typst-layout`（提供 `PagedDocument`）、`typst-library`（提供 `Frame`/`FrameItem`/诊断等）都在这里，印证了 u1-l1 讲过的依赖关系。

workspace 根的成员声明与默认成员：

[../../Cargo.toml（workspace 根）L1-L4](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/Cargo.toml#L1-L4) —— `members = ["crates/*", ...]`、`default-members = ["crates/typst-cli"]`，解释了为什么必须用 `-p typst-pdf`。

[../../Cargo.toml（workspace 根）L6-L16](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/Cargo.toml#L6-L16) —— workspace 级 `rust-version = "1.92"`、`edition = "2024"`，本 crate 通过 `{ workspace = true }` 继承。

#### 4.4.4 代码实践

**实践：本地编译并跑一次单元测试。**

1. 实践目标：确认本地工具链能编译本 crate，并亲眼看到内联测试运行。
2. 操作步骤（在仓库根目录执行）：
   - `cargo check -p typst-pdf` —— 先做最快的类型检查。
   - `cargo test -p typst-pdf` —— 运行单元测试。
3. 需要观察的现象：
   - `cargo check` 末尾出现 `Finished` 字样（可能伴随若干 lint 警告）。
   - `cargo test` 输出里能看到 `tags::tests::tagged_and_page_range` 这类测试名，状态为 `ok` 或 `ignored`。
4. 预期结果：编译通过、测试通过。`待本地验证`：具体测试数量与耗时取决于机器与网络（首次需拉取 `typst-assets` 等 git 依赖）。
5. 失败排查：若报 Rust 版本不够，按 workspace 根的 `rust-version` 升级工具链（`rustup update`）。

#### 4.4.5 小练习与答案

**练习 1**：在仓库根目录直接敲 `cargo build`（不带 `-p`），会编译什么？为什么？
**答案**：会编译 `default-members` 指定的 `crates/typst-cli`（及其全部依赖，含 `typst-pdf`）。因为 workspace 根设了 `default-members = ["crates/typst-cli"]`。若只想编 `typst-pdf` 本身，必须加 `-p typst-pdf`。

**练习 2**：`cargo test -p typst-pdf` 跑的测试在哪个目录？
**答案**：没有单独的 `tests/` 目录；测试是源码内联的 `#[cfg(test)] mod tests`，分布在 `src/` 各文件末尾（如 `src/tags/mod.rs`）。`cargo test` 会自动收集并运行它们。

**练习 3**：本 crate 的 `Cargo.toml` 里依赖版本写成 `{ workspace = true }`，好处是什么？
**答案**：版本由 workspace 根 `Cargo.toml` 的 `[workspace.dependencies]` 统一管理，所有成员 crate 用同一版本，避免版本分裂，也方便一次性升级（例如 `krilla`、`typst-library` 全 workspace 同步）。

---

## 5. 综合实践：画一张模块依赖草图

把本讲三块内容（顶层地图、convert 主线、tags 子系统）串成一张图。请准备纸笔或绘图工具，完成以下任务：

1. **以 `convert::convert()` 为中心节点**，向外画箭头，标出它在主线中**直接调用**的子模块：`page`（经 `convert_pages`）、`attach`（`attach_files`）、`tags`（`init` / `resolve`）、`outline`（`build_outline`）、`metadata`（`build_metadata`）、以及 `link`（`collect_named_destinations` 间接用到 `pos_to_xyz`）。再用一个虚线框圈出 `convert_pages → handle_frame → handle_text/handle_shape/handle_image/handle_link` 这条「内容分派」支路，标注它们分别属于 `text`/`shape`/`image`/`link` 模块。
2. **把 `util` 画成横向工具层**，用虚线箭头指向所有复用它的翻译器（说明它不在主线、而是被大家共享）。
3. **单独画一个 `tags/` 子图**，画出五段划分（`groups` / `tree`（build+mod+text）/ `context` / `resolve`（mod+accumulator）/ `util`），并在每个名字旁标注它属于三段式中的哪一段（①build 构建 / ②tree 遍历发射 / ③resolve 解析校验 / 数据模型 / 工具）。
4. **画完后自检**：能否不看任何资料，口头复述「`pdf()` → `convert()` → 13 步流水线 → `finish()`」？能否说出 `tags::init` 与 `tags::resolve` 为何被分置在 `convert_pages` 前后？

> 验收标准：图里至少出现 11 个顶层模块名 + tags 的 5 个子模块名，且主线箭头顺序与 4.2.2 的流水线一致。这张图将是你后续阅读 u2/u3/u4/u5 各讲的「导航地图」。

## 6. 本讲小结

- `typst-pdf` 采用「薄门面 + 厚后厨」结构：`lib.rs` 只对外暴露 `pdf()` / `pdf_in_bundle()` / `PdfOptions` / `PdfStandards` / `PdfStandard` / `Timestamp` / `Timezone`，12 个内部模块全部私有。
- 12 个模块分三类：`convert`（总调度）、`util`（公共工具箱）、以及 9 个「内容翻译器」（page/text/shape/paint/image/link/outline/metadata/attach）。
- `convert()` 是一条 13 步流水线：设置 → 建文档 → 页号转换 → 命名目的地 → tags init → 全局上下文 → 转页面 → 附件 → tags resolve → 大纲/元数据/结构树 → finish。
- `handle_frame` 是内容分派器，按 `FrameItem` 变体把工作路由到对应翻译器；`GlobalContext` 是贯穿全程的「大背包」。
- `tags/` 是最复杂的子系统，内部五段划分（groups/tree/context/resolve/util），遵循「build 构建 → tree 遍历发射 → resolve 解析校验」三段式；默认开启，但与 `page_ranges` 互斥。
- 本 crate 是 typst workspace 的库成员，用 `cargo check/build/test -p typst-pdf` 单独操作；测试为源码内联，无独立 `tests/` 目录。

## 7. 下一步学习建议

本讲建立了全局地图，接下来进入**第二单元（转换主链路）**，按主线顺序逐层展开：

- **u2-l5 `convert()` 编排**：精读 `convert()` 每一步与 `SerializeSettings`、`GlobalContext` 的细节（本讲只画了地图，u2-l5 会走进每间屋子）。
- **u2-l6 页面导出**：展开 `convert_pages` 的页面尺寸、bleed 出血框、页码标签。
- **u2-l7 Frame 遍历器**：深入 `handle_frame` / `handle_group` 与变换状态栈（本讲只标了分派箭头，u2-l7 会讲透 push/pop）。
- **u2-l8 类型转换工具集**：展开 `util.rs`（本讲只说它是「工具箱」，u2-l8 讲具体 trait 与 `convert_path`）。

阅读建议：动手做完第 5 节的草图后再进入 u2，因为它就是你后续每讲的目录索引。
