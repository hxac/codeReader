# EPUB 渲染器

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `EpubRenderer` 与编排函数 `render_epub_file` 的分层职责，以及 pdf-craft 为什么把「写 EPUB ZIP」这件事交给上游依赖 `epub-generator`。
- 掌握 `convert_pdf_to_epub` 的四个关键输出选项：`book_meta`、`lan`、`table_render`、`latex_render`（外加 `inline_latex`）。
- 理解 EPUB 渲染为什么必须有 `toc.xml`（`validate(require_toc=True)`），以及 `TocCollection` 如何把静态目录树与动态章节文件合并成 EPUB 导航。
- 理解 LaTeX 公式在 EPUB 中的多种呈现方式：块级 `Formula`、行内 `Formula`、`pylatexenc` 纯文本降级，以及 `LaTeXRender.MATHML / SVG / CLIPPING` 三种上游渲染模式。

## 2. 前置知识

### 2.1 EPUB 是什么

EPUB 本质上是一个**带约定的 ZIP 包**：里面装着若干 XHTML 章节文件、图片等资源、一个描述「本书有哪些文件、书名作者是什么」的 OPF 元数据文件，以及一个供阅读器生成目录导航的 nav（或旧版 NCX）文件。正因为它需要「整本书的目录树」才能构造导航，EPUB 渲染对输入的要求比 Markdown 渲染更高——这正是本讲反复出现的主题。

### 2.2 你需要回忆的前几讲结论

- **u6-l1**：`DocumentPackage` 是提取器与渲染器之间的中立契约，目录即数据格式（`chapters/`、`assets/`、`toc.xml`、`cover.png`、`document.json`）；`validate(require_toc=True)` 是「谁需要什么、谁强制」的分层安检。
- **u5-l1**：一章即 `Chapter`，其 `layouts` 由 `ParagraphLayout`（文字段落）与 `AssetLayout`（`ref` 为 `image` / `table` / `equation` 的资源块）混排；块内容是 `str | InlineExpression | Reference | HTMLTag` 的混合列表。
- **u5-l3**：`chapter.id is None` 的头章（`chapter_head.xml`）收纳首个目录命中之前的封面、前言等内容。
- **u6-l3**：Markdown 渲染器采用「第一遍扫描收集全书脚注 → 第二遍逐章渲染」的两遍结构，脚注按 `(page_index, order)` 全书统一编号。本讲的 EPUB 渲染器沿用同样的两遍套路。

### 2.3 两个上游依赖

- [`epub-generator==0.1.7`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L34)：真正的 EPUB 写入器。pdf-craft 只负责把章节 XML 转换成它的领域对象（`TextBlock` / `Formula` / `Image` / `Table` / `Footnote` / `Mark`），再由它的 `generate_epub` 落成 ZIP。`BookMeta`、`TableRender`、`LaTeXRender` 也都来自这个包。**该包源码不在本仓库内**，本讲对它的描述全部以 pdf-craft 侧的调用代码和官方文档为准。
- [`pylatexenc>=2.10,<3.0.0`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L35)：LaTeX 转纯文本工具，用于「不保留行内公式」时的降级。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pdf_craft/renderer/epub/renderer.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/renderer.py) | `EpubRenderer` 门面：校验包、校验语言、转发参数，仅 19 行 |
| [pdf_craft/renderer/epub/render.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py) | 编排函数 `render_epub_file` 与全部「章节 XML → epub-generator 对象」的转换逻辑 |
| [pdf_craft/renderer/epub/toc_collection.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/toc_collection.py) | `TocCollection`：把 `toc.xml` 静态树与章节文件动态内容合并成 EPUB 目录项列表 |
| [pdf_craft/renderer/epub/latex_to_text.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/latex_to_text.py) | `latex_to_plain_text`：pylatexenc 封装，失败时兜底 `[原文]` |
| pdf_craft/craft.py | 门面方法 `render_epub` / `convert_pdf_to_epub`，以及 `book_meta` 缺省时的自动提取入口 |
| pdf_craft/transform.py | `_extract_book_meta`：从 PDF 元数据构造 `BookMeta` 的真正实现 |
| pdf_craft/document/package.py | `validate(require_toc=True)`：EPUB 渲染的额外安检 |
| pdf_craft/pdf/ref.py | `TITLE_TAGS = ("title", "sub_title")`：判定段落是否为标题的 ref 集合 |

## 4. 核心概念与源码讲解

### 4.1 EPUB 生成

#### 4.1.1 概念说明

pdf-craft 在 EPUB 这条链路上做了一个清晰的分工决策：**自己不写 EPUB 文件格式**。EPUB 是一种细节繁多的容器格式（mimetype 排序、OPF 清单、nav/NCX 双目录……），pdf-craft 的比较优势在「把 OCR 章节数据结构化」，于是它把前者整个委托给 `epub-generator`，自己只做两件事：

1. 把 `Chapter` / `ParagraphLayout` / `AssetLayout` 翻译成 `epub-generator` 的领域对象（`TextBlock`、`Formula`、`Image`、`Table`、`Footnote`、`Mark`）；
2. 组织好目录树和元数据，交给 `generate_epub` 一次性落盘。

与 Markdown 渲染器一样，这里是两层结构：`EpubRenderer` 是薄门面，`render_epub_file` 是编排函数。

#### 4.1.2 核心流程

`render_epub_file` 的执行过程（与 u6-l3 的 Markdown 渲染同构，但多了目录合并）：

```text
1. create_chapters_reader(chapters_path)     # 章节读取器工厂（可重复调用）
2. 第一遍扫描：逐章 search_references_in_chapter
   → 收集全书脚注 → 按 (page_index, order) 排序 → references_to_map 编号 1..N
3. 第二遍扫描：逐章
   a. 为每章构造延迟回调 get_chapter（真正被调用时才转换内容）
   b. chapter.id is None        → 记为 get_head（头章/前言）
   c. 首个 layout 是标题段落     → 提取标题文本 → toc_collection.collect(...)
4. 组装 EpubData(meta=book_meta, get_head, chapters=toc_collection.normalize().target,
                 cover_image_path=cover_path)
5. check_aborted(aborted)                    # 落盘前最后一次中止检查
6. generate_epub(...)                        # 上游写 ZIP；
   assert_not_aborted=...                    # 生成过程中仍可轮询中止
```

注意一个 Python 经典陷阱的防御写法：`get_chapter` 闭包用默认参数 `ch=chapter` 把当前章节绑定进去（render.py:68），避免循环变量的迟绑定（late binding）让所有闭包都引用最后一章。

#### 4.1.3 源码精读

先看 19 行的门面：

[renderer.py:9-18](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/renderer.py#L9-L18) —— `EpubRenderer.render` 接收全部输出选项：`package.validate(require_toc=True)` 强制要求 `toc.xml` 存在；`lan` 只允许 `"zh"` / `"en"`，否则抛 `ValueError`；随后把包内四个路径与选项原样转发给 `render_epub_file`。对照 [package.py:33-34](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py#L33-L34)，`require_toc=True` 时缺 `toc.xml` 会抛 `ValueError("document package is missing toc.xml")`——这就是「EPUB 渲染必须有 toc.xml」的直接出处（原因见 4.2）。

编排函数主体：

[render.py:56-62](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L56-L62) —— 第一遍扫描：遍历所有章节收集脚注 `Reference`，按 `(page_index, order)` 排序后用 `references_to_map` 建立「引用 id → 全书序号」映射（实现见 [chapter.py:78-82](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L78-L82)，就是 `enumerate(references, 1)`）。`search_references_in_chapter`（[chapter.py:68-75](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L68-L75)）内部用 `seen` 集合对每章去重，保证多处引用同一脚注只编一个号。

[render.py:66-93](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L66-L93) —— 第二遍扫描与目录收集：`id is None` 的头章成为 `get_head`；其余章节要求 `layouts` 非空且首个 layout 是 `ref in TITLE_TAGS` 的标题段落（`TITLE_TAGS` 定义在 [pdf_craft/pdf/ref.py:1](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ref.py#L1)，即 `("title", "sub_title")`），标题文本经 `_iter_text_in_title`（[render.py:112-117](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L112-L117)）拼接字符串成员得到，为空则兜底 `"Untitled"`；`have_body = len(chapter.layouts) > 1` 决定该目录项是否携带正文回调。

[render.py:95-109](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L95-L109) —— 组装 `EpubData` 并调用 `generate_epub`：`chapters` 取 `toc_collection.normalize().target`（4.2 详解）；落盘前 `check_aborted`（[metering.py:8-12](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/metering.py#L8-L12)，条件为真即抛上游 `AbortError`），同时把 `assert_not_aborted` 传给上游，让 ZIP 生成过程中也能响应协作式中止。

章节到上游对象的翻译核心：

[render.py:119-172](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L119-L172) —— `_convert_chapter_to_epub`：逐 layout 分派——`AssetLayout` 走 `_convert_asset_to_epub`；`ParagraphLayout` 把所有块的 content 摊平后包成 `TextBlock`，`kind` 依 `layout.ref in TITLE_TAGS` 取 `HEADLINE` 或 `BODY`，`level` 直接沿用段落级 level（u5-l3 的章内层级分析结果）。章末再收集本章脚注，逐个转成 `Footnote(id=编号, contents=...)`。

[render.py:188-263](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L188-L263) —— `_convert_asset_to_epub` 按 `asset.ref` 三分支：`equation` 提取 LaTeX 文本构造成 `Formula`；`image` 按 `asset.hash` 拼 `assets/<hash>.png` 路径构造 `Image`（文件不存在则返回 `None` 丢弃）；`table` 优先找 HTML 内容构造 `Table`，找不到 HTML 就退回整表截图 `Image`。注意 title 与 caption 也走 `_transform_content`，所以表格标题里也能含公式。

[render.py:299-329](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L299-L329) —— `_transform_content` 是内容成员的总分派器，四种成员四种命运：`str` 原样透传；`InlineExpression` 见 4.3；`Reference` 在 `ref_id_to_number` 有效时变成 `Mark(id=编号)`；`HTMLTag` 递归转换成上游的 `EpubHTMLTag`（名称与属性原样保留——安全性由 u6-l2 的白名单安检兜底，这里无需重复检查）。

一个值得注意的细节：正文段落的块内容转换时**显式传了 `ref_id_to_number=None`**（[render.py:145](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L145)），脚注内容同样传 `None`（[render.py:277](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L277)、[render.py:289](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L289)）。由于 `_transform_content` 的 `Reference` 分支要求 `ref_id_to_number` 为真值（[render.py:314](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L314)），这意味着**按当前源码，Mark 只会出现在资源的 title/caption/表格内容里，正文段落内的脚注引用标记会被静默跳过**（脚注本体仍按编号写进章末 `Footnote` 列表）。这与 Markdown 渲染器里正文 `[^n]` 的做法不同，读者可在综合实践中亲眼验证这一行为。

#### 4.1.4 代码实践

**实践目标**：不跑 OCR，直接用积木式方法把一个已有 `DocumentPackage` 渲染成 EPUB，并检查包内结构。

1. 操作步骤：
   - 准备一个提取产物包目录（u3 系列实践中用 `package_path` 保留的那个；若无，先运行 `PDFCraft(pdf=PDFOptions(ocr=...)).extract_pdf("book.pdf", "pkg")`）。
   - 写脚本（示例代码）：

   ```python
   from pathlib import Path
   from pdf_craft import PDFCraft
   from pdf_craft.document import DocumentPackage

   craft = PDFCraft()  # 渲染已有包不需要 PDFOptions
   package = DocumentPackage.from_path(Path("pkg"))
   craft.render_epub(package, "book.epub")
   ```

   - 运行 `unzip -l book.epub` 列出包内文件，关注三类条目：XHTML 章节文档、图片资源、目录/元数据文件。
   - 再删掉（或改名）`pkg/toc.xml` 后重跑脚本，观察报错。
2. 需要观察的现象：EPUB 是 ZIP 容器，`unzip -l` 能看到章节与资源；删掉 `toc.xml` 后应抛 `ValueError: document package is missing toc.xml`。
3. 预期结果：由 [renderer.py:13](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/renderer.py#L13) 与 [package.py:33-34](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py#L33-L34) 直接推出；包内具体文件清单取决于上游 epub-generator，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`render_epub`（积木式）与 `convert_pdf_to_epub`（一步式）谁会自动提取 PDF 元数据填充 `book_meta`？

答案：只有 `convert_pdf_to_epub` 会——见 [craft.py:205-206](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L205-L206)，`book_meta is None` 时调用 `self._extract_book_meta(Path(source))`。`render_epub` 的 `book_meta` 缺省就是 `None`，不做任何补齐（[craft.py:135-145](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L135-L145)）。

**练习 2**：为什么 `get_chapter` 要写成 `def get_chapter(ch=chapter):` 而不是直接闭包引用 `chapter`？

答案：Python 闭包按名字迟绑定循环变量。若直接引用，循环结束后所有闭包都会看到最后一个 `chapter`；用默认参数在函数定义时求值的特点把当前值固化进闭包（[render.py:68-74](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L68-L74)）。

**练习 3**：`_convert_asset_to_epub` 里 `image` 分支为什么检查 `image_file.exists()` 后返回 `None` 而不是抛错？

答案：资源按内容哈希裁剪落盘是 u3-l4 的提取行为，渲染时文件缺失属于「宁可少一张图也不让整本书渲染失败」的降级策略（[render.py:219-231](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L219-L231)）；调用侧 `if asset_element:` 直接跳过 `None`。

### 4.2 目录集合

#### 4.2.1 概念说明

**为什么 EPUB 必须有 `toc.xml`**：Markdown 是「无结构的单文件」，标题层级自带导航；EPUB 则需要一棵目录树来生成 nav/NCX 导航、决定章节文件的组织与阅读顺序。pdf-craft 的章节切分本身就由 `toc.xml` 驱动（u5-l3），渲染时同样以它为骨架。

但仅有静态的 `toc.xml` 不够：目录树里的标题文本来自 OCR，可能失真；有的目录条目在正文里找不到对应章节（空章）；有的章节切出来了却不在目录里。`TocCollection` 就是**静态目录树（toc.xml）与动态章节内容（chapter_N.xml）的合并器**，输出一份「标题来自真实章节、结构来自目录树、空节点被修剪」的 `TocItem` 列表。

#### 4.2.2 核心流程

`TocCollection` 的工作分三步：

```text
collect(toc_id, title, have_body, get_chapter)   # 每个真实章节调用一次
  ├─ DFS 在 toc.xml 树中找到 toc_id 的祖先栈 [根...自己]
  ├─ 沿栈逐级 _find_or_append_toc_item：已有则复用，没有则
  │   新建 TocItem(title="unknown") 挂到当前层级并登记 id 映射
  ├─ 把栈顶（自己）的 title / get_chapter 覆写为真实值
  └─ 栈找不到（章节不在目录中）→ 追加到 extra_toc_items 平铺在末尾
      have_body=True 时登记进 _having_body_toc_set

normalize()   # 渲染前修剪
  └─ 递归删除「自己无正文 且 无子节点」的目录项
     （有子节点但无正文的保留为纯导航节点）

target        # root_toc_items + extra_toc_items，喂给 EpubData.chapters
```

#### 4.2.3 源码精读

[toc_collection.py:9-24](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/toc_collection.py#L9-L24) —— 构造与输出：初始化时若 `toc_path` 存在则 `decode(read_xml(toc_path))` 载入 u4-l3 的 `Toc` 树（只含 id/level/children 坐标，不含标题文本——标题由本类从真实章节回填）；四份状态：root 树、root_toc_items / extra_toc_items 两个输出列表、`_id_to_toc_item` 与 `_having_body_toc_set` 两个登记表。

[toc_collection.py:26-53](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/toc_collection.py#L26-L53) —— `collect`：先 DFS 查祖先栈；命中则沿栈把整条链「按需补建 + 逐级下钻」，最后覆写栈顶的标题与回调；未命中则新建 `TocItem` 平铺进 `extra`。注意覆写发生在栈顶——祖先链上尚未被真实章节访问过的节点以 `title="unknown"` 占位，等它们自己的 `collect` 调用到来时再补上真实标题。

[toc_collection.py:60-78](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/toc_collection.py#L60-L78) —— `_find_raw_toc_item_stack`：手写迭代式 DFS（显式栈存 `(索引, 兄弟列表, 当前项)`），返回从根到目标 id 的路径。这是在 `Toc.children` 嵌套树上的一次标准先序查找。

[toc_collection.py:80-96](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/toc_collection.py#L80-L96) —— `_find_or_append_toc_item`：按 id 复用已建节点；若同一 id 出现在另一分支则抛 `RuntimeError`（目录树里 id 应当唯一，u4-l3 的 id 即文档顺序编号）；否则新建占位节点追加到当前层。

[toc_collection.py:98-113](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/toc_collection.py#L98-L113) —— `_clean_no_content_items`：自底向上递归删除「无正文且无子节点」的叶子目录项；中文注释点明了设计意图——**有子节点但自己没正文的条目保留为「仅存在于目录中的章节」**，让读者能通过导航跳到它的子节，这对阅读体验更重要。

调用侧再确认一遍数据流：[render.py:88-93](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L88-L93) 把 `chapter.id`（u5-l3 中由目录摊平字典 `ref2toc` 命中时记录的目录条目 id）作为 `toc_id` 传入；`have_body=False` 时传 `get_chapter=None`，该目录项成为纯导航节点。头章不进目录，而是 `get_head`（[render.py:76-77](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L76-L77)），作为 EPUB 的卷首材料先于第一章呈现。

#### 4.2.4 代码实践

**实践目标**：用源码阅读 + 微实验理解 `collect / normalize / target` 的行为，不依赖完整 EPUB 渲染。

1. 操作步骤：
   - 打开你提取包里的 `toc.xml`，按 u4-l3 的 `decode` 心算出树结构（item 的嵌套即层级）。
   - 写一个独立小脚本（示例代码），直接驱动 `TocCollection`：

   ```python
   from pathlib import Path
   from pdf_craft.renderer.epub.toc_collection import TocCollection
   from pdf_craft.common import read_xml
   from pdf_craft.extractor.toc import decode

   tc = TocCollection(Path("pkg/toc.xml"))
   # 模拟两个真实章节：一个命中目录 id=1，一个不在目录中
   tc.collect(toc_id=1, title="第一章（真实标题）", have_body=True, get_chapter=lambda: "chapter-1")
   tc.collect(toc_id=999, title="附录（目录外）", have_body=True, get_chapter=lambda: "extra")
   for item in tc.normalize().target:
       print(item.title, item.get_chapter)
   ```

   - 再补一次 `tc.collect(toc_id=1, title="x", have_body=False, get_chapter=None)` 之后重复 `normalize()`，观察 `id=1` 且无子节点无正文时是否被修剪。
2. 需要观察的现象：命中目录的条目挂进树形结构；目录外条目平铺在 `target` 末尾；无正文无子节点的条目在 `normalize` 后消失。
3. 预期结果：由 [toc_collection.py:26-58](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/toc_collection.py#L26-L58) 与 [toc_collection.py:98-113](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/toc_collection.py#L98-L113) 的逻辑直接推出；注意 `toc_id` 需换成你包里真实存在的 id（可在 `toc.xml` 里数出第几个 item）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `TocItem` 的标题要从真实章节回填，而不是直接用目录页 OCR 出来的标题文本？

答案：`toc.xml` 的 `Toc` 节点根本不存标题文本（u4-l3：只存「坐标 + 层级」，文本留在 OCR 缓存中按坐标反查）；且正文标题的 OCR 质量通常优于目录页小字排版。`collect` 用章节首个标题段落提取的文本覆写 `toc_item.title`（[toc_collection.py:42](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/toc_collection.py#L42)），提取失败还有 `"Untitled"` 兜底（[render.py:84-86](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L84-L86)）。

**练习 2**：一个目录条目在 `toc.xml` 里存在，但对应的 `chapter_N.xml` 缺失（比如该章全部页面 OCR 失败被丢弃），渲染结果会怎样？

答案：没有任何 `collect` 命中它的 id，它的 `TocItem` 要么根本不会被创建（`_find_or_append` 只在作为某个真实章节的祖先时被调用），要么以无正文状态存在；`normalize` 的 `_clean_no_content_items` 会把「无正文且无子节点」的它修剪掉（[toc_collection.py:105-113](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/toc_collection.py#L105-L113)）。若它有子节点，则保留为纯导航节点。

**练习 3**：`_find_or_append_toc_item` 在什么情况下抛 `RuntimeError`，这个约束保护了什么不变量？

答案：同一 id 的 `TocItem` 已存在但不在当前分支列表中时抛出（[toc_collection.py:83-88](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/toc_collection.py#L83-L88)）。它保护的不变量是「一个目录 id 只对应输出树中的一个节点」——`_id_to_toc_item` 登记表隐含了节点全局唯一，若允许同一 id 挂到两处，回填标题与正文回调时就无法确定该写哪一份。

### 4.3 公式转换

#### 4.3.1 概念说明

PDF 里的数学公式在章节数据里有**两条来源**，在 EPUB 里有**三种命运**：

| 来源（u5-l1 数据模型） | 位置 | 转换路径 |
| --- | --- | --- |
| `AssetLayout(ref="equation")` | 独立成块的展示公式 | 恒定为 `Formula`（不受 `inline_latex` 影响） |
| `InlineExpression`（行内） | 段落文本中间 | `inline_latex=True`（默认）→ `Formula`；`False` → `latex_to_plain_text` 纯文本 |

而 `Formula` 最终长什么样，由 `latex_render` 决定——这是传给 `generate_epub` 的上游选项，官方文档列出三种模式：`MATHML`、`SVG`、`CLIPPING`（见 [docs/en/PDF_TRANSLATION.md:71-74](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/PDF_TRANSLATION.md#L71-L74)，`TableRender` 同页记载为 `HTML` 或 `CLIPPING`）。`MATHML` 是默认值（[renderer.py:11](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/renderer.py#L11)）。

「多一种呈现方式」之所以必要：EPUB 阅读器对 MathML / SVG 的支持参差不齐，纯文本则永远可读但丢失结构——`latex_render` 与 `inline_latex` 两个旋钮让使用者按目标阅读器取舍。

#### 4.3.2 核心流程

```text
章节 XML 中的公式
├─ AssetLayout(ref="equation")
│    └─ _extract_text_from_content：拼接 str 与 InlineExpression.content
│       → 空则丢弃，非空 → Formula(latex_expression=...)
└─ InlineExpression（行内）
     ├─ inline_latex=True  → Formula(latex_expression=content.strip())
     └─ inline_latex=False → latex_to_plain_text(content.strip())
            ├─ pylatenc LatexNodes2Text 成功 → 近似纯文本（如 \alpha → α）
            └─ 任何异常 → f"[{latex_content}]"   # 保底：原文放方括号里

Formula 的最终呈现由 latex_render 决定：MATHML / SVG / CLIPPING（上游处理）
```

#### 4.3.3 源码精读

[latex_to_text.py:1-10](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/latex_to_text.py#L1-L10) —— 整个模块只有 10 行：模块级单例 `LatexNodes2Text()`（避免每次调用重复初始化），`latex_to_plain_text` 把任何异常吞掉并返回 `f"[{latex_content}]"`——降级永不抛错，最坏情况是读者看到方括号里的原始 LaTeX，而不是整本书渲染失败。

[render.py:308-312](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L308-L312) —— `_transform_content` 的 `InlineExpression` 分支：`inline_latex` 为真产出 `Formula`，否则走纯文本降级。注意先 `strip()`——OCR 抓下来的公式两端常带空白。

[render.py:175-185](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L175-L185) —— `_extract_text_from_content`：为块级公式提取 LaTeX 表达式的辅助函数，`flatten` 摊平嵌套内容后只拼接 `str` 与 `InlineExpression.content` 两类成员（`Reference`、`HTMLTag` 不参与公式文本）。

[render.py:208-217](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L208-L217) —— `equation` 资源分支：提取结果为空返回 `None` 丢弃该块；非空构造 `Formula(latex_expression=..., title=..., caption=...)`——块级公式不检查 `inline_latex`，独立公式永远保留 LaTeX 交给上游。

[render.py:101-108](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L101-L108) —— `latex_render` 与 `table_render` 在这里离开 pdf-craft 的世界：作为参数透传给 `generate_epub`，由上游决定 MathML / SVG / 截图等具体呈现。pdf-craft 侧对它们的全部知识就是「原样转发 + 提供默认值 `TableRender.HTML` / `LaTeXRender.MATHML`」（[renderer.py:10-11](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/renderer.py#L10-L11)）。

顺带对照 `table_render`：表格内容是 `HTMLTag` 时优先构造 `Table`（HTML 模式的基础），HTML 缺失时退回整表截图 `Image`（[render.py:233-261](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L233-L261)）；`TableRender.CLIPPING` 则是让上游直接用截图的另一种取舍。

#### 4.3.4 代码实践

**实践目标**：用同一份包对比 `LaTeXRender.MATHML` 与另一模式的产物差异，并验证 `inline_latex` 开关的效果。

1. 操作步骤（示例代码）：

   ```python
   from pathlib import Path
   from pdf_craft import PDFCraft, LaTeXRender
   from pdf_craft.document import DocumentPackage

   craft = PDFCraft()
   package = DocumentPackage.from_path(Path("pkg"))
   craft.render_epub(package, "book_mathml.epub", latex_render=LaTeXRender.MATHML)
   craft.render_epub(package, "book_clipping.epub", latex_render=LaTeXRender.CLIPPING)
   craft.render_epub(package, "book_noinline.epub", inline_latex=False)
   ```

   - 挑一本含公式的 PDF 提取出的包（技术文档最佳；没有就先提取一个）。
   - 运行后执行 `unzip -l book_mathml.epub` 与 `unzip -l book_clipping.epub`，diff 两份清单。
   - 解包对比 XHTML：`unzip -p book_mathml.epub | grep -c math` 之类（或解压后全文搜索公式所在章）。
2. 需要观察的现象：不同 `latex_render` 下资源清单或 XHTML 内容的差异（例如是否引入额外资源文件、公式在 XHTML 中的标记形式）；`inline_latex=False` 的版本里行内公式变成普通文本字符。
3. 预期结果：`inline_latex=False` 的行为由 [render.py:308-312](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L308-L312) 确定；`latex_render` 的包内差异由上游 epub-generator 决定，**本仓库源码无法推出具体差异，待本地验证**——这正是本实践想让你建立的直觉：哪些行为由 pdf-craft 决定，哪些由上游决定。

#### 4.3.5 小练习与答案

**练习 1**：`inline_latex=False` 时，独立成块的展示公式会变成纯文本吗？

答案：不会。`inline_latex` 只影响 `InlineExpression` 分支（[render.py:308-312](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L308-L312)）；`equation` 资源分支不读取该开关，恒定构造 `Formula`（[render.py:208-217](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L208-L217)）。

**练习 2**：`latex_to_plain_text` 为什么捕获 `Exception` 而不是让错误冒出来？兜底格式 `f"[{latex_content}]"` 有什么好处？

答案：公式降级是「锦上添花」而非关键路径，一处坏 LaTeX 不应让整本 EPUB 渲染失败；方括号包原文保证信息不丢失——读者至少能看到原始表达式，也便于事后排查（[latex_to_text.py:6-10](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/latex_to_text.py#L6-L10)）。

**练习 3**：脚注内容里的 `InlineExpression` 在 `inline_latex=True` 时会变成什么？会在哪一层的 `Footnote.contents` 里出现？

答案：仍是 `Formula`——`_convert_reference_to_footnote_contents` 调用 `_transform_content` 时照常传入 `inline_latex`（仅 `ref_id_to_number` 传 `None`，见 [render.py:281-296](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L281-L296)）；产出的 `TextBlock` 进入该脚注的 `Footnote.contents`，即脚注文本内的公式与正文公式走同一套转换。

## 5. 综合实践

**任务**：对同一个 `DocumentPackage` 产出多个 EPUB 变体，横向对比 `latex_render` 与 `book_meta` 两个维度的差异，最终形成一张「选项 → 产物差异」对照表。

1. 准备：一个含公式、含表格、带 PDF 元数据（可用 pypdf 或 `pdfinfo` 事先确认书名作者非空）的 PDF；用 `extract_pdf` + `package_path="pkg"` 提取并保留包。
2. 写脚本（示例代码）：

   ```python
   from pathlib import Path
   from pdf_craft import PDFCraft, PDFOptions, DeepSeekOCRVendorConfig, BookMeta, LaTeXRender

   craft = PDFCraft(pdf=PDFOptions(
       ocr=DeepSeekOCRVendorConfig(api_key="...", model="..."),  # 换成你的 OCR 配置
   ))  # 一步式方法需要 PDFOptions；包缓存已存在，OCR 实际不会重新花钱

   # 维度一：book_meta —— 自动提取 vs 显式指定
   craft.convert_pdf_to_epub("book.pdf", "auto_meta.epub", package_path="pkg",
                             lan="zh")   # 复用已有包缓存，几乎零 OCR 成本
   craft.convert_pdf_to_epub("book.pdf", "manual_meta.epub", package_path="pkg",
                             book_meta=BookMeta(title="我的书名", authors=["某作者"]))

   # 维度二：latex_render
   for mode in (LaTeXRender.MATHML, LaTeXRender.SVG, LaTeXRender.CLIPPING):
       craft.convert_pdf_to_epub("book.pdf", f"epub_{mode.name.lower()}.epub",
                                 package_path="pkg", latex_render=mode)
   ```

   注意 `convert_pdf_to_epub` 复用 `package_path` 时，OCR 缓存与 `toc.xml` 均已存在（u2-l2、u4-l3 的缓存语义），多跑几次不会重复花费 token。
3. 检查：
   - `unzip -l` 逐个列出 6 个 EPUB 的文件清单并 diff；
   - 解压后打开 OPF 元数据文件，对比 `auto_meta.epub`（书名来自 PDF 元数据，提取逻辑见下）与 `manual_meta.epub`（你显式给的书名作者）；
   - 用任何阅读器或浏览器插件打开 `epub_mathml.epub`，翻到公式页检查 4.1.3 末尾提到的「正文段落内脚注引用标记是否可见」，并对照章末 Footnote 列表。
4. 预期结果：
   - `book_meta` 的自动提取路径：[craft.py:205-206](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L205-L206) 判断缺省后委托 [craft.py:264-267](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L264-L267)，真正实现位于 [transform.py:123-139](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L123-L139)：从 OCR 侧读 PDF 元数据，逐字段 `remove_surrogates` 清理代理字符后构造 `BookMeta`（title/description/publisher/isbn/authors/editors/translators/modified）；**标题为空时回退为文件名主干 `pdf_path.stem`**；读元数据抛 `PDFError` 时打印警告并返回 `None`（EPUB 无显式元数据）。
   - 其余产物差异**待本地验证**，把观察记进对照表。

## 6. 本讲小结

- EPUB 渲染是两层结构：19 行的 `EpubRenderer` 门面（校验 + 转发）+ `render_epub_file` 编排；pdf-craft 不写 EPUB 格式，只把章节 XML 翻译成 `epub-generator` 的领域对象（`TextBlock`/`Formula`/`Image`/`Table`/`Footnote`/`Mark`）再交 `generate_epub` 落盘。
- EPUB 渲染强制要求 `toc.xml`：`validate(require_toc=True)`，因为 EPUB 导航需要整棵目录树；Markdown 渲染则没有这个要求——这是两种渲染器输入契约的最大差异。
- `TocCollection` 是静态目录树与动态章节内容的合并器：`collect` 沿 DFS 祖先链「按需补建 + 回填真实标题/正文回调」，目录外章节平铺到末尾，`normalize` 修剪「无正文且无子节点」的空目录项、保留「有子节点的纯导航节点」。
- 与 Markdown 渲染器同构的两遍扫描：第一遍收集全书脚注并按 `(page_index, order)` 统一编号，第二遍逐章构造**延迟求值**的 `get_chapter` 回调（默认参数防迟绑定）；落盘前后都有协作式中止检查点。
- 公式有多重命运：块级 `equation` 恒为 `Formula`；行内 `InlineExpression` 由 `inline_latex` 决定保留 `Formula` 还是 `latex_to_plain_text` 降级（pylatexenc，失败兜底 `[原文]`）；`Formula` 的最终呈现由 `latex_render`（`MATHML`/`SVG`/`CLIPPING`）在上游决定，`table_render`（`HTML`/`CLIPPING`）同理。
- 只有 `convert_pdf_to_epub` 会在 `book_meta` 缺省时自动从 PDF 元数据提取（标题空则回退文件名主干，失败则警告并放弃）；积木式 `render_epub` 不做任何补齐。

## 7. 下一步学习建议

至此 u6「文档包与渲染」单元完结，你已经掌握 pdf-craft 的两条输出管线。下一讲进入 **u7-l1 转换器协议：PackageTransformer 与 ChapterTransformer**，学习在「提取」与「渲染」之间插入自定义内容变换的扩展点——`convert_pdf_to_epub` 的 `steps` 参数（本讲已数次照面）正是它的接入口。建议先重读 [render.py:95-100](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/epub/render.py#L95-L100) 中 `EpubData` 的组装点，思考「如果要在渲染前改写每章文本，应该改包还是改渲染器」，然后带着答案去读 `pdf_craft/transformer/`。
