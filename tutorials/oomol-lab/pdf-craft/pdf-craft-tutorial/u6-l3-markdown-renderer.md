# u6-l3 Markdown 渲染器：章节 XML 如何变成 Markdown 文件

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `MarkdownRenderer` 到 `render_markdown_file` 的两层封装关系，并按顺序描述一次 Markdown 渲染的完整流程：资源路径计算 → 第一遍扫描收集脚注 → 第二遍扫描逐章渲染 → 追加脚注区 → 复制封面。
2. 解释表格渲染的「先试 GFM 管道表格、遇到复杂结构就保留原始 HTML」降级策略，以及为什么必须这么做。
3. 解释 `assets_path` 参数如何同时决定资源文件被复制到哪里、以及 Markdown 里图片链接写成什么样。
4. 掌握标题层级映射（章级 `level` + 段落级 `level` → `#` 的个数）、行内/独立公式的定界符选择。
5. 在完全不依赖 OCR 凭据的环境下，手工构造一个最小 `DocumentPackage` 来验证渲染器的各种行为。

## 2. 前置知识

本讲会用到以下概念，不熟悉的读者请先补课：

- **GFM 管道表格**：GitHub Flavored Markdown 的表格语法，用 `|` 分隔单元格、用 `---` 行分隔表头与表体。它**不支持** `colspan`/`rowspan`（合并单元格），这是本讲表格策略的核心背景。
- **ATX 标题**：用 `#` 前缀表示标题，`#` 是一级标题、`##` 是二级标题，最多到 `######` 六级。
- **GFM 脚注语法**：正文里写 `[^1]` 引用脚注，文末用 `[^1]: 脚注内容` 定义脚注。
- **生成器（Generator）与两遍扫描**：渲染器大量使用 `yield` 产生字符串片段流；同时因为脚注编号需要先收集全书所有脚注、再渲染正文，章节文件会被读取两遍。
- **`os.path.relpath`**：计算「从一个目录出发到另一个目录的相对路径」，是图片链接生成的关键。
- **markdownify**：一个把 HTML 转成 Markdown 的第三方库，pdf-craft 用它做表格转换，并继承了它的 `MarkdownConverter` 类做定制。
- **承接前讲**：u5-l1 讲过章节 XML 的数据模型（`Chapter`/`ParagraphLayout`/`AssetLayout`/`BlockLayout`），u6-l1 讲过 `DocumentPackage` 契约与 `validate`，u6-l2 讲过 `HTMLTag` 泛型容器与白名单过滤——本讲是这三讲在输出端的汇合点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pdf_craft/renderer/markdown/renderer.py` | 渲染器门面 `MarkdownRenderer`：校验包、归一化参数、委托下层。仅 13 行 |
| `pdf_craft/markdown/render/render.py` | 编排函数 `render_markdown_file`：路径计算、两遍扫描、章节渲染、脚注区、封面复制 |
| `pdf_craft/markdown/render/table.py` | 表格渲染 `render_table_content`：定制 markdownify 转换器，复杂表格降级保留 HTML |
| `pdf_craft/markdown/render/layouts.py` | 布局排版 `render_layouts`：把段落/资源布局渲染成 Markdown 片段流（标题、公式、表格、图片） |
| `pdf_craft/markdown/paragraph/render.py` | 段落级序列化 `render_markdown_paragraph`：`HTMLTag` 树还原成 HTML 字符串、文本规范化 |
| `pdf_craft/extractor/chapter/reader.py` | `create_chapters_reader`：章节 XML 读取器工厂，支撑两遍扫描 |
| `pdf_craft/extractor/chapter/chapter.py` | 章节数据模型与脚注收集（`search_references_in_chapter`、`references_to_map`） |
| `pdf_craft/craft.py` | 门面方法 `render_markdown` 与一步式 `convert_pdf_to_markdown` 的衔接处 |
| `tests/test_table_rendering.py` | 表格渲染的单元测试，本讲实践的参考模板 |

## 4. 核心概念与源码讲解

### 4.1 渲染器封装：MarkdownRenderer 与 render_markdown_file

#### 4.1.1 概念说明

Markdown 渲染器分两层：

- **`MarkdownRenderer`**（`renderer/` 目录下）是公开的渲染边界，一个极薄的门面：先调用 `package.validate()` 做分层安检（回顾 u6-l1：`chapters/` 与 `assets/` 目录人人要过），再把缺省参数补齐后委托给 `render_markdown_file`。注意它调用 `validate()` 时**没有**传 `require_toc=True`——Markdown 渲染不需要 `toc.xml`，因为标题层级信息已经直接写在每个 `chapter_N.xml` 里了（对比：EPUB 渲染器必须要求 `toc.xml`，这是下一讲的内容）。
- **`render_markdown_file`**（`markdown/render/` 目录下）是真正的编排函数，完成一次渲染要做的全部事情。

这里有一个容易混淆的命名问题：`MarkdownRenderer.render` 的参数 `assets_path` 传给下层后名字变成了 `output_assets_path`——它指的是**输出侧**的资源目录（Markdown 文件旁边的图片放哪），而**输入侧**的资源目录是 `package.assets_path`（包里的 `assets/`，图片来源）。一个是「源」，一个是「目的地」，渲染过程会把需要的图片从源复制到目的地。

#### 4.1.2 核心流程

一次 `render_markdown_file` 的执行过程：

```
输入: chapters_path(包内 chapters/), assets_path(包内 assets/),
      output_path(Markdown 文件路径), output_assets_path(输出资源目录),
      cover_path(封面, 可无), aborted(协作式中止回调)

1. 计算资源路径:
   assets_destination = output_assets_path 若为绝对路径
                        否则 output_path.parent / output_assets_path
   assets_ref_path    = relpath(assets_destination, output_path.parent)  # 相对 Markdown 文件的引用路径
2. 创建输出目录与资源目录
3. 第一遍扫描: 逐章读取, 收集全书所有脚注 Reference,
   按 (page_index, order) 排序, 建立 ref_id → 脚注编号(从 1 起) 的映射
4. 第二遍扫描: 逐章渲染
   - 每章开始前检查 aborted(协作式中止)
   - 章与章之间、布局与布局之间用空行分隔
   - 每个 Chapter 的 layouts 交给 render_layouts(4.3 节)产出片段流, 逐片段写文件
5. 追加脚注区: "\n\n---\n\n## References", 之后每条脚注一行 "[^i]:  内容"
6. 若有封面: 把封面文件复制进资源目录(只复制, 正文不引用它)
```

注意「两遍扫描」的实现技巧：`create_chapters_reader` 返回的不是章节列表，而是一个**工厂函数**，每调用一次就返回一个全新的生成器（先 `chapter_head.xml` 若存在，再按序号读 `chapter_N.xml`）。所以第 3 步和第 4 步各调用一次 `read_chapters()`，各自从头遍历。

#### 4.1.3 源码精读

先看门面层，全文只有 13 行：

[pdf_craft/renderer/markdown/renderer.py:5-13](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/markdown/renderer.py#L5-L13)

这段代码定义 `MarkdownRenderer.render`：第 10 行先 `package.validate()` 校验包完整性；第 11-13 行委托 `render_markdown_file`，其中 `assets_path or Path("assets")` 表示输出资源目录缺省为 Markdown 文件旁的 `assets/` 子目录，`cover_path or package.cover_path` 表示封面缺省用包内 `cover.png`（若包没有封面则为 `None`，后面跳过复制）。

再看编排函数的签名与资源路径计算：

[pdf_craft/markdown/render/render.py:16-31](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/render/render.py#L16-L31)

第 24-26 行的注释解释了设计意图：**复制目的地**与**Markdown 里的引用路径**是分开算的，两者都不依赖进程的工作目录。第 27-30 行算出 `assets_destination`（绝对路径直接用；相对路径则拼到输出文件所在目录下），第 31 行用 `relpath` 算出「从 Markdown 文件出发指向资源目录」的引用路径。即使你传入绝对路径作为资源目录，`relpath` 也会把它重新表达成从输出目录出发的相对路径（例如 `../../../tmp/assets`）。

接着是目录创建与第一遍脚注收集：

[pdf_craft/markdown/render/render.py:33-42](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/render/render.py#L33-L42)

第 35 行拿到章节读取器工厂；第 37-39 行第一遍扫描，用 `search_references_in_chapter` 收集每章的脚注（该函数会按脚注 id 去重，同一脚注被多次引用只收集一次）；第 41 行按 `(page_index, order)` 全书排序——所以脚注编号遵循**页面顺序**而不是正文提及顺序；第 42 行 `references_to_map` 生成 `脚注id → 序号` 映射（从 1 开始编号）。这两个收集函数的定义在：

[pdf_craft/extractor/chapter/chapter.py:68-82](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L68-L82)

然后是第二遍扫描的主循环：

[pdf_craft/markdown/render/render.py:44-62](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/render/render.py#L44-L62)

第 47 行 `check_aborted(aborted)` 是 u3-l5 讲过的协作式中止检查点（每章开始前轮询一次，为真抛 `AbortError`）；第 49-51 行用 `need_blank_line` 状态保证只在「已经写过内容」的情况下才在章前补一个空行（`\n\n`），避免文件开头出现空行；第 53-61 行把每章的 `layouts` 交给 `render_layouts`（4.3 节）产出片段流，逐片段写入。

章节读取器工厂本身：

[pdf_craft/extractor/chapter/reader.py:8-26](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reader.py#L8-L26)

`XMLReader` 用 `chapter_\d+.xml` 模式收集文件并按序号排序；生成器先吐出 `chapter_head.xml`（无目录前置内容章，见 u5-l3），再按序吐出各章。每调用一次返回的 `generate` 都会重新遍历磁盘，这正是两遍扫描的基础。

主循环之后是脚注区与封面：

[pdf_craft/markdown/render/render.py:64-77](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/render/render.py#L64-L77)

[pdf_craft/markdown/render/render.py:80-98](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/render/render.py#L80-L98)

`_render_footnotes_section` 在没有任何脚注时直接返回（第 86-87 行）；否则先输出分隔线与 `## References` 标题，再逐条输出 `[^i]:  `（注意源码里冒号后是**两个空格**）加脚注布局的渲染结果。封面则被 `shutil.copy` 复制进资源目录（第 73-77 行），注意正文 Markdown 并不会引用封面，它只是被放到图片旁边供发布流程使用。

最后看门面方法如何串进工作流：

[pdf_craft/craft.py:112-119](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L112-L119)

[pdf_craft/craft.py:179-190](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L179-L190)

`PDFCraft.render_markdown` 只是参数转 `Path` 后调用 `MarkdownRenderer`；一步式 `convert_pdf_to_markdown` 在提取与转换步骤完成后调用它，把提取时用户传的 `aborted` 回调复用为渲染中止回调。

#### 4.1.4 代码实践

**实践目标**：不依赖任何 OCR 凭据，手工构造一个最小的 `DocumentPackage`（含一条脚注），用 `MarkdownRenderer` 渲染两次、指定不同的 `assets_path`，观察输出目录结构与 Markdown 内容的差异。

**操作步骤**：

1. 新建工作目录，编写以下脚本（示例代码）：

```python
# practice_u6_l3.py（示例代码）
from pathlib import Path
from pdf_craft import DocumentPackage, MarkdownRenderer

# 1. 手工构造最小包：chapters/ + assets/ 两个目录是 validate 的硬性要求
pkg = Path("mini_pkg")
(pkg / "chapters").mkdir(parents=True, exist_ok=True)
(pkg / "assets").mkdir(exist_ok=True)

# 2. 写一个带脚注的章节 XML（格式来自 extractor/chapter/chapter.py 的 decode）
chapter_xml = """<chapter id="1" level="0">
  <body>
    <paragraph ref="title" level="0">
      <block page_index="1" order="1" det="0,0,400,40">Chapter 1 Getting Started</block>
    </paragraph>
    <paragraph ref="text" level="-1">
      <block page_index="1" order="2" det="0,50,400,80">See the manual<ref id="1-1"/> for details.</block>
    </paragraph>
  </body>
  <references>
    <ref id="1-1">
      <mark>1</mark>
      <paragraph ref="text" level="-1">
        <block page_index="1" order="5" det="0,300,400,340">The official manual, chapter 3.</block>
      </paragraph>
    </ref>
  </references>
</chapter>
"""
(pkg / "chapters" / "chapter_1.xml").write_text(chapter_xml, encoding="utf-8")

# 3. 渲染两次，仅 assets_path 不同
package = DocumentPackage.from_path(pkg)
MarkdownRenderer().render(package, Path("out/a/book.md"), assets_path=Path("images"))
MarkdownRenderer().render(package, Path("out/b/book.md"), assets_path=Path("../shared"))

# 4. 打印结果
print(Path("out/a/book.md").read_text(encoding="utf-8"))
print(sorted(p.name for p in Path("out/a").iterdir()))
print(sorted(p.name for p in Path("out").iterdir()))
```

2. 运行 `python practice_u6_l3.py`。

**需要观察的现象**：

- 两份 `book.md` 的正文完全相同：第一行是 `# Chapter 1 Getting Started`（标题渲染规则见 4.3 节），第二段是 `See the manual[^1] for details.`（`<ref id="1-1"/>` 被替换成 GFM 脚注标记）。
- 文件末尾出现脚注区：`---`、`## References`、`[^1]:  The official manual, chapter 3.`。
- 目录结构差异：`out/a/` 下出现空的 `images/` 目录（渲染器无条件创建资源目录，本包没有图片所以是空的）；第二次渲染的资源目的地是 `out/shared/`（`../shared` 相对于 `out/b/` 解析），于是 `out/` 下多出 `shared/`。

**预期结果**：两次渲染都成功落盘，差异只在资源目录的位置；Markdown 正文里目前没有图片链接（包里没有图片资产），图片链接的差异验证放在 4.3 节和综合实践。以上现象由源码逻辑推出，**待本地验证**。

**补充观察**（可选）：把 `assets_path` 换成一个绝对路径（如 `Path("/tmp/md_assets")`）再渲染一次，检查 `/tmp/md_assets` 被创建，且若有图片时链接会写成从输出目录出发的相对路径形式。

#### 4.1.5 小练习与答案

**练习 1**：为什么章节文件要被读取两遍，而不是一遍读完存进列表？

**答案**：因为脚注编号是全书级的：必须先收集**所有**章节的脚注、排序并统一编号，正文里才能把 `<ref>` 替换成正确的 `[^n]`。如果只扫描一遍，渲染第一章时还不知道后面的脚注。项目选择用「读取器工厂」而非「读入列表」实现两遍扫描，让内存里同时只保留一章的数据，对大书更友好。

**练习 2**：`MarkdownRenderer.render` 调用 `package.validate()` 时不传 `require_toc=True`，这说明了什么？

**答案**：说明 Markdown 渲染不依赖 `toc.xml`。目录分析的结果（层级）在章节生成阶段就已经写入每个 `chapter_N.xml` 的 `level` 属性里了（u5-l3），渲染器只需要章内信息。而 EPUB 渲染器需要 `toc.xml` 来生成书内导航目录（`validate(require_toc=True)`），这是两种渲染器输入契约的关键差别。

**练习 3**：如果把 `output` 写成 `out/book.md`、`assets_path` 传 `Path("../shared")`，资源会被复制到哪里？为什么不会受运行脚本时所在目录影响？

**答案**：复制到 `out/../shared` 即工程根下的 `shared/`。因为 `render_markdown_file` 先把相对的 `output_assets_path` 拼接到 `output_path.parent`（输出文件所在目录）上得到绝对语义的目的地，再统一用 `relpath` 计算引用路径，复制目的地与引用路径都不依赖进程工作目录（这正是源码第 24-26 行注释强调的设计）。

### 4.2 表格渲染：GFM 管道表格与复杂度降级

#### 4.2.1 概念说明

在章节 XML 里，表格是 `AssetLayout(ref="table")`，其 `content` 是一棵 `HTMLTag` 树（`table`/`thead`/`tr`/`th` 等都在 u6-l2 的白名单里，`td`/`th` 还额外允许 `colspan`/`rowspan` 属性）。渲染表格时面临一个抉择：

- **转成 GFM 管道表格**：可读性最好、与周围 Markdown 风格一致，但 GFM 表格**不支持合并单元格**。上游的 markdownify 库遇到 `colspan`/`rowspan` 时会**静默地**把它们丢掉——合并单元格变成空单元格，数据就这么无声地丢了。
- **保留原始 HTML**：GFM 本身允许内嵌 HTML 块，信息零丢失，但可读性差一些。

pdf-craft 的策略是「先试转换、复杂则降级」：用一个定制的 markdownify 转换器去转，一旦检测到 GFM 表达不了的特征，就抛出一个内部异常，外层捕获后原样返回 HTML 字符串。**宁可输出丑一点的 HTML，也不静默丢数据**。

这个降级是安全的：能走到这一步的 HTML 都来自 `HTMLTag` 容器（u6-l2 的结论「容器即安全凭证」——标签与属性早已过白名单过滤），所以保留 HTML 不会重新引入 XSS 风险。

#### 4.2.2 核心流程

```
render_table_content(html_string):
  try:
    用 _GFMTableConverter(markdownify 子类) 转换 html_string
    转换过程中逐单元格检查:
      colspan > 1  ──┐
      rowspan > 1    ├──> 抛 _TableComplexityException
      colspan/rowspan 值不是整数 ──┘
    转换前检查: 表格含多个 <tbody> ──> 抛 _TableComplexityException
    成功 → 返回 GFM 管道表格
  except _TableComplexityException:
    返回原始 html_string(保留 HTML)
```

为什么「多个 `tbody`」也算复杂？GFM 管道表格只有「表头 + 表体」两段结构，无法表达多个分组表体，强行转换同样会丢结构。

#### 4.2.3 源码精读

先看整体结构与定制点：

[pdf_craft/markdown/render/table.py:13-52](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/render/table.py#L13-L52)

类文档字符串（第 15-32 行）直接讲明了动机：markdownify 会静默丢失合并单元格信息（还引用了上游 issue #121），所以这里选择「检测复杂度、保留 HTML」。三个 `convert_*` 方法覆写了 markdownify 的钩子：每个 `td`/`th` 单元格转换前先做复杂度检查（第 38-44 行），整个 `table` 转换前检查 `tbody` 数量（第 46-52 行，`find_all("tbody", recursive=False)` 只找直接子级）。

单元格检查的细节：

[pdf_craft/markdown/render/table.py:54-73](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/render/table.py#L54-L73)

`el.get("colspan", "1")` 取属性、缺省视为 1；`int(...)` 转换失败（比如 `colspan="invalid"`）抛出的 `ValueError` 在第 70-73 行被**转译**成 `_TableComplexityException`——非法值与超界值同等对待，都走保留 HTML 的路。

入口函数：

[pdf_craft/markdown/render/table.py:76-82](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/render/table.py#L76-L82)

`heading_style="ATX"` 指定标题风格（对表格场景影响不大，主要是沿用 markdownify 的习惯参数）；`convert` 成功则返回.strip() 后的 GFM 表格，捕获 `_TableComplexityException` 后返回**未经修改的** `html_string`。

再看白名单侧的佐证——`td`/`th` 的属性白名单确实放行了 `colspan`/`rowspan`：

[pdf_craft/markdown/paragraph/tags.py:388-399](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/tags.py#L388-L399)

这也解释了为什么复杂度检测必须放在渲染层而不是解析层：解析层（u6-l2）的职责是「按 GitHub 的规则决定什么 HTML 能存在」，`colspan` 在 GitHub 白名单里是合法属性，理应保留；而「GFM 管道表格表达不了」是**输出格式**的局限，只能在渲染层兜底。

最后看调用点（在 4.3 节的 `_render_asset_content` 里）：`asset.content` 这棵 `HTMLTag` 树先被 `render_markdown_paragraph` 重新序列化成 HTML 字符串（见 4.3.3 对 `paragraph/render.py` 的讲解），再交给 `render_table_content`。

[pdf_craft/markdown/render/layouts.py:183-194](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/render/layouts.py#L183-L194)

#### 4.2.4 代码实践

**实践目标**：以 `tests/test_table_rendering.py` 为模板，直接调用 `render_table_content`，验证三类输入的行为差异。这个实践是纯函数调用，不需要任何包或凭据。

**操作步骤**：

1. 编写脚本（示例代码）：

```python
# practice_table.py（示例代码）
from pdf_craft.markdown.render.table import render_table_content

# A. 简单表格 → 应转成 GFM 管道表格
simple = (
    "<table>"
    "<thead><tr><th>Name</th><th>Age</th></tr></thead>"
    "<tbody><tr><td>Alice</td><td>25</td></tr></tbody>"
    "</table>"
)
print("=== A ===")
print(render_table_content(simple))

# B. 带 colspan 的表格 → 应原样保留 HTML
colspan = (
    "<table>"
    '<thead><tr><th colspan="2">Personal Info</th><th>City</th></tr></thead>'
    "<tbody><tr><td>Alice</td><td>25</td><td>NYC</td></tr></tbody>"
    "</table>"
)
print("=== B ===")
print(render_table_content(colspan))

# C. 非法 colspan 值 → 同样保留 HTML
invalid = '<table><tr><th colspan="invalid">H</th></tr><tr><td>x</td></tr></table>'
print("=== C ===")
print(render_table_content(invalid))
```

2. 运行 `python practice_table.py`。

**需要观察的现象**：

- A 的输出含 `|` 与 `---` 分隔行，不含任何 `<table>`/`<td>` 标签；
- B 的输出与输入字符串完全一致（`colspan="2"` 原样保留）；
- C 的输出同样保留 HTML（`int("invalid")` 抛 `ValueError` 被转译为复杂度异常）。

**预期结果**：三组输出分别呈现「GFM / 原文 / 原文」。与 `tests/test_table_rendering.py` 中 `test_simple_table_converts_to_gfm`、`test_table_with_colspan_preserves_html`、`test_invalid_colspan_value_preserves_html` 三个用例的断言一致（该测试文件是现成的行为规格）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么不干脆把所有表格都保留为 HTML，省去定制转换器？

**答案**：因为大多数 OCR 识别出的表格是没有合并单元格的简单表格，转成管道表格后与周围 Markdown 风格统一、在纯文本环境（diff、终端、代码评审）可读。保留 HTML 只是「复杂表格的必要妥协」，不应该让所有表格都为少数复杂表格付出可读性代价。

**练习 2**：`_GFMTableConverter` 是通过什么机制「中途放弃」转换的？为什么不用返回值标记失败？

**答案**：通过抛出 `_TableComplexityException` 异常。markdownify 的转换是深度递归（`convert_table` → `convert_tr` → `convert_td`），失败可能发生在任意深度的单元格上，异常可以把「放弃」信号从递归深处一步抛到最外层 `render_table_content` 的 `except` 里；用返回值标记则需要在每层递归里逐层检查、透传失败状态，代码会繁琐得多。

**练习 3**：若某表格同时带 `align="center"` 属性，会被判为复杂而保留 HTML 吗？

**答案**：不会。`_check_cell_complexity` 只检查 `colspan`/`rowspan`，`convert_table` 只检查 `tbody` 数量；`align` 不在检测清单里，GFM 管道表格的 `:---:` 语法能够表达对齐。`tests/test_table_rendering.py` 的 `test_table_with_alignment_converts_to_gfm` 断言了这一行为。

### 4.3 布局排版：layouts.py 与标题、公式、图片

#### 4.3.1 概念说明

`layouts.py` 负责「一个 Chapter（或一条脚注）内部」的排版：把 `ParagraphLayout`（文字段落）与 `AssetLayout`（资源块）交替的布局序列渲染成 Markdown 片段流。它是三层渲染结构的最内层：

```
render_markdown_file   (章与章、脚注区的编排, 4.1 节)
  └─ render_layouts    (一章内部布局的遍历与衔接)
       └─ render_markdown_paragraph  (一段内部 str/公式/HTMLTag 的序列化)
```

三类核心行为：

1. **标题层级映射**：u5-l3 讲过两级 level 体系——章级 `Chapter.level`（来自目录树深度，0 起）与段落级 `ParagraphLayout.level`（章内标题聚类，0~5，非标题为 -1）。渲染时先把章级 level 封顶到 2（`_MAX_TOC_LEVELS - 1`），再加上段落级 level、封顶到 6（`_MAX_TITLE_LEVELS`），最终输出 `level + 1` 个 `#`。只有 `ref` 为 `title`/`sub_title` 且 `level >= 0` 的段落才渲染成标题。

2. **文本成员渲染**：块内容里的三类成员分别处理——普通字符串经 `to_markdown_string` 做 Markdown 转义（`\` 与 `$` 会被转义，防止正文里的美元符号被误认成公式定界符）；`InlineExpression` 按其 `kind` 包上 `$...$` 或 `\(...\)` 等定界符；`Reference` 借助 4.1 节的映射表渲染成 `[^n]`。

3. **资源块渲染**：`AssetLayout` 按 **title → content → caption** 的顺序输出。`equation` 的内容包成独立公式 `\[...\]`；`table` 走 4.2 节；`image` 把 `assets/<hash>.png` 复制到输出资源目录并生成 `![](<引用路径>)` 链接——**复制目的地**与**链接引用路径**正是由 4.1 节算出的两个路径变量决定的。

#### 4.3.2 核心流程

`render_layouts` 的主干：

```
render_layouts(layouts, ..., toc_level=chapter.level):
  toc_level = min(toc_level, 2)                       # 章级深度封顶
  for 每个 layout(布局之间输出 "\n\n"):
    AssetLayout    → _render_asset:   title → content → caption
                     content 按 ref 分派:
                       equation → "\[LaTeX\]"
                       table    → render_table_content(4.2 节)
                       image    → 复制 hash.png + "![](引用路径)"
    ParagraphLayout → render_paragraph:
                       是标题段落(ref ∈ {title, sub_title} 且 level ≥ 0)?
                         → 输出 min(toc_level + level, 6) + 1 个 "#"
                       逐块调 render_markdown_paragraph 序列化成员
```

`render_markdown_paragraph`（`paragraph/render.py`）做两件事：把 `HTMLTag` 树按原结构重新序列化成 HTML 字符串（带属性转义），普通成员交给回调（即上面的成员渲染逻辑）；最后 `_normalize_paragraph` 做**跨片段的文本规范化**——片段内部的换行会被折叠掉，拼接处按「两侧是否都是汉字」决定要不要补一个空格（中文中文直接相连，中英之间补空格），行首空白剥除。

#### 4.3.3 源码精读

先看布局遍历与衔接：

[pdf_craft/markdown/render/layouts.py:18-51](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/render/layouts.py#L18-L51)

第 18-19 行是两个层级常量；第 31 行把章级 level 封顶到 2（意味着目录第四层及以下的章与第三层同级渲染标题）；第 33-37 行在布局之间输出 `\n\n`（Markdown 的段落分隔）；第 38-51 行按布局类型分派。

标题渲染与成员渲染：

[pdf_craft/markdown/render/layouts.py:54-86](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/render/layouts.py#L54-L86)

第 57 行判定是否标题段落（`TITLE_TAGS` 是 `("title", "sub_title")`，定义在 [pdf_craft/pdf/ref.py:1](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/ref.py#L1)）；第 58-61 行输出 `#`：level 0 对应 1 个 `#`（源码注释标明），所以「章级 0 + 段落级 0」是一级标题，「章级 1 的章」的标题是二级，「章级 0 的章内小节（段落级 2）」是三级。第 63-80 行的 `render_member` 是成员分派：字符串走 TEXT 转义；`InlineExpression` 先 `strip()`、内容非空才输出定界符包裹；`Reference` 查映射表渲染 `[^n]`（第 77 行 `get(part.id, 1)` 的缺省值 1 是纯防御——正文引用的脚注必然已在全书收集表中）。

公式定界符的对应关系由 `to_markdown_string` 决定：

[pdf_craft/expression.py:51-65](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/expression.py#L51-L65)

注意第 62-65 行：纯文本（`TEXT`）反而要转义 `\` 与 `$`——LaTeX 定界符自己管转义，而普通文本里的美元符号必须 escaping 才不会被 Markdown 渲染器误判为公式。

资源块主体：

[pdf_craft/markdown/render/layouts.py:92-156](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/render/layouts.py#L92-L156)

`_render_asset` 先渲染 `title`（第 120-129 行，用 `"".join(...).strip()` 把片段流拼成字符串再判断是否非空），再渲染主体内容，最后渲染 `caption`；`has_content` 标志保证只有前面已有内容时才在 caption 前补空行（第 154-155 行）。第 139-144 行根据 `ref` 与内容是否非空更新 `has_content`。

内容分派与图片渲染：

[pdf_craft/markdown/render/layouts.py:159-203](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/render/layouts.py#L159-L203)

[pdf_craft/markdown/render/layouts.py:206-236](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/render/layouts.py#L206-L236)

`equation`（第 167-181 行）：内容先按文本渲染，再整体包上 `\[...\]`（`ExpressionKind.DISPLAY_BRACKET`）。`table`（第 183-194 行）：序列化成 HTML 字符串后交给 `render_table_content`。`image` 的 `_render_image` 是 `assets_path` 语义的落点：

- 第 214-218 行：`hash` 为 `None` 或源文件 `assets/<hash>.png` 不存在就**静默跳过**（不报错、不输出）；
- 第 221-223 行：目标不存在才 `copy2` 复制——**幂等**，重复渲染不会重复复制（也意味着目标已存在时不更新）；
- 第 225-228 行：`asset_ref_path` 是绝对路径（Windows 跨盘符等 `relpath` 返回绝对值的场景）时直接用目标文件路径，否则用「资源引用路径 / 文件名」；
- 第 230-236 行：路径统一替换为 POSIX 风格（`\` → `/`，Markdown 链接的标准写法），alt 固定为空，输出 `![](...)`。

最后是段落序列化与文本规范化：

[pdf_craft/markdown/paragraph/render.py:7-16](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/render.py#L7-L16)

[pdf_craft/markdown/paragraph/render.py:30-50](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/render.py#L30-L50)

`_render_html_tag` 把 `HTMLTag` 节点还原成 `<tag attr="...">children</tag>`，无子节点时写成自闭合 `<tag />`；属性值经过 `&`/`"`/`<`/`>` 转义（第 63-74 行）。这就是表格 `HTMLTag` 树变回 HTML 字符串、供 markdownify 消费的地方。

[pdf_craft/markdown/paragraph/render.py:77-108](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/render.py#L77-L108)

`_normalize_paragraph` 的关键：`_split_enters` 把片段按行拆开、行间插入 `\n` 标记（第 100-108 行），而主循环遇到 `\n` 标记时**并不输出换行**（第 96-97 行只置 `is_line_head = True`）——即片段内部的换行被折叠；在行边界处，若前一个字符与新行首字符**不都是汉字**（第 85-90 行的 `not is_chinese_char(last_char) or not is_chinese_char(part[0])`），补一个空格再拼接。效果：OCR 断行处的「中文/中文」直接相连还原成连续中文，「英文/英文」补空格，符合中英混排排版习惯。

#### 4.3.4 代码实践

**实践目标**：完全在内存中构造布局对象，直接调用 `render_layouts`，观察标题、行内公式、独立公式的输出形态，不读写任何章节 XML。

**操作步骤**：

1. 编写脚本（示例代码）：

```python
# practice_layouts.py（示例代码）
from pathlib import Path
from pdf_craft.extractor.chapter import (
    AssetLayout, BlockLayout, InlineExpression, ParagraphLayout,
)
from pdf_craft.expression import ExpressionKind
from pdf_craft.markdown.render.layouts import render_layouts

title = ParagraphLayout(
    ref="title", level=0,
    blocks=[BlockLayout(page_index=1, order=1, det=(0, 0, 400, 40), content=["Energy"])],
)
para = ParagraphLayout(
    ref="text", level=-1,
    blocks=[BlockLayout(page_index=1, order=2, det=(0, 50, 400, 80),
                        content=["mass ", InlineExpression(kind=ExpressionKind.INLINE_DOLLAR, content="m"),
                                 " and speed ", InlineExpression(kind=ExpressionKind.INLINE_PAREN, content="v")])],
)
formula = AssetLayout(
    page_index=1, ref="equation", det=(0, 90, 400, 130),
    title=[], content=[r"E = mc^2"], caption=[], hash=None,
)

text = "".join(render_layouts(
    layouts=[title, para, formula],
    assets_path=Path("pkg/assets"),        # 包内资源(源)
    output_assets_path=Path("out/assets"), # 输出资源(目的地)
    asset_ref_path=Path("assets"),         # Markdown 里的引用路径
    toc_level=0,                           # 本章的章级 level
))
print(text)
```

2. 运行 `python practice_layouts.py`。

**需要观察的现象**：

- 第一段输出 `# Energy`（章级 0 + 段落级 0 → 1 个 `#`）；
- 第二段输出 `mass $m$ and speed \(v\)`（两种行内公式定界符并存：`INLINE_DOLLAR` 用 `$`，`INLINE_PAREN` 用 `\(`）；
- 第三段输出 `\[E = mc^2\]`（独立公式用方括号定界符）；
- 三段之间以空行分隔。

**预期结果**（由源码逻辑推出，**待本地验证**）：

```
# Energy

mass $m$ and speed \(v\)

\[E = mc^2\]
```

3. 修改实验：把 `toc_level` 改成 `1` 再跑一次，标题应变成 `## Energy`（章级 1 的章，标题整体降一级）；把 `toc_level` 改成 `5`，由于封顶到 2，标题仍是 `### Energy`。

#### 4.3.5 小练习与答案

**练习 1**：一本书的目录有四层（章/节/小节/小小节），第四层的章在 Markdown 输出里标题是几级？为什么封顶到 2 而不是原样透传？

**答案**：第三级。`render_layouts` 把章级 level 封顶到 `_MAX_TOC_LEVELS - 1 = 2`（[layouts.py:31](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/render/layouts.py#L31)），第四层章与第三层同为 2，加上段落级 0 后输出 `###`。封顶是为了给章内小节标题留出层级空间：Markdown 标题最多六级，若章级深度不封顶，深层目录的章内小节标题会被挤到超过六级、或与章标题失去视觉层级差。

**练习 2**：正文里出现「价格是 100$ 且 x$y$ 是公式」这样的 OCR 文本，渲染后为什么不会乱？

**答案**：普通文本成员走 `to_markdown_string(kind=TEXT, ...)`，其中的 `\` 与 `$` 会被转义成 `\\` 与 `\$`（[expression.py:62-65](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/expression.py#L62-L65)），所以孤立的美元符号不会被 Markdown 渲染器当成公式定界符；只有被识别为 `InlineExpression` 的部分才按 kind 包上定界符。

**练习 3**：重复对同一个包渲染两次到同一输出目录，图片会被复制几次？如果想强制更新图片该怎么做？

**答案**：各复制一次，共两次内只有第一次真正执行复制——`_render_image` 在目标文件已存在时跳过 `copy2`（[layouts.py:222-223](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/render/layouts.py#L222-L223)），这是幂等设计。想强制更新需先删除输出资源目录里对应的 `<hash>.png`（或整个资源目录）再渲染。

## 5. 综合实践

**任务**：把 4.1 节的最小包升级为一个「全要素包」（图片 + 简单表格 + 复杂表格 + 脚注 + 标题），然后用三种不同的 `assets_path` 各渲染一次，系统观察资源目录与图片链接的联动关系。

**步骤**：

1. 在 `mini_pkg/assets/` 里放一个图片文件并命名为 `abc123.png`（任意 PNG 皆可；临时没有图片的话，随便复制一个小文件改名即可——渲染器只检查文件存在并复制，不会解码图片内容，此法可验证机制但不适合看视觉效果）。
2. 扩展 `chapter_1.xml`，在 `<body>` 里追加（示例代码，格式依据 `extractor/chapter/chapter.py` 的解码规则）：

```xml
<asset ref="image" page_index="2" det="0,0,400,300" hash="abc123">
  <caption>Figure 1 the sample image</caption>
</asset>
<asset ref="table" page_index="2" det="0,320,400,450">
  <title>Table 1 simple table</title>
  <content><table><thead><tr><th>Name</th><th>Age</th></tr></thead>
  <tbody><tr><td>Alice</td><td>25</td></tr></tbody></table></content>
</asset>
<asset ref="table" page_index="2" det="0,470,400,600">
  <content><table><thead><tr><th colspan="2">Merged</th></tr></thead>
  <tbody><tr><td>a</td><td>b</td></tr></tbody></table></content>
</asset>
```

3. 用三种 `assets_path` 渲染到三个不同位置（示例代码）：

```python
MarkdownRenderer().render(package, Path("out/flat/book.md"))                      # 缺省 "assets"
MarkdownRenderer().render(package, Path("out/deep/nested/book.md"),
                           assets_path=Path("../img"))
MarkdownRenderer().render(package, Path("out/abs/book.md"),
                           assets_path=Path("~/md_assets").expanduser())          # 绝对路径
```

4. 逐一检查并记录：

| 检查项 | 预期 |
| --- | --- |
| `out/flat/` 目录 | `book.md` + `assets/abc123.png` |
| `out/deep/nested/book.md` 里的图片链接 | `![](../img/abc123.png)` |
| `out/abs/book.md` 里的图片链接 | 从输出目录出发指向绝对目录的相对路径形式（`relpath` 重算） |
| 两个表格 | 简单表格变成管道表格；`colspan` 表格原样保留 HTML |
| 脚注 | 正文 `[^1]` + 文末 References 区 |
| `mini_pkg/assets/abc123.png` | 依然存在（复制而非移动，原包不可变） |

5. 对 `out/flat` 再渲染一次，确认不会报错（幂等）。如果你在之前讲义的实践中保留过真实提取的包，把上述第 3 步换成真实包重跑一遍，效果相同且更直观。

以上预期均由源码逻辑推出，**待本地验证**。

## 6. 本讲小结

- Markdown 渲染器是两层结构：`MarkdownRenderer` 门面（校验包、补缺省参数）+ `render_markdown_file` 编排（路径计算、两遍扫描、逐章渲染、脚注区、封面复制）；Markdown 渲染不需要 `toc.xml`。
- **复制目的地与链接引用路径分离计算**：`assets_path` 决定资源复制到哪，Markdown 里的图片链接由 `relpath` 从输出文件位置重新推算，两者都不依赖进程工作目录。
- 章节文件被读取两遍：第一遍收集全书脚注并按 `(page_index, order)` 统一编号，第二遍渲染正文，`<ref>` 因此能替换成 `[^n]`，文末统一输出 References 区。
- 表格渲染采取「先试 GFM、复杂则保留 HTML」策略：定制 markdownify 转换器检测 `colspan`/`rowspan`/多 `tbody`/非法属性值，宁可保留 HTML 也不静默丢数据；降级是安全的，因为 HTML 早已过 u6-l2 的白名单安检。
- 标题层级 = `min(章级 level, 2) + 段落级 level` 再封顶 6，输出 `level + 1` 个 `#`；文本成员按类型分派（转义文本 / `$...$` 公式 / `[^n]` 脚注），图片渲染幂等复制且 POSIX 化链接路径。
- `_normalize_paragraph` 折叠片段内换行，并按「两侧是否都是汉字」决定拼接处是否补空格，实现中英混排的排版规范化。

## 7. 下一步学习建议

下一讲（u6-l4）学习 **EPUB 渲染器**，建议带着与 Markdown 渲染器的三点对比去读 `pdf_craft/renderer/epub/`：

1. 输入契约更强：EPUB 渲染需要 `toc.xml`（`validate(require_toc=True)`），因为要用目录树生成 EPUB 的书内导航（`toc_collection.py`）；
2. 表格与公式有独立的渲染策略选项（`TableRender`/`LaTeXRender`，如公式可转 MathML），而非 Markdown 的单一策略；
3. 输出从单文件变成 ZIP 包，涉及 `epub-generator` 上游依赖与书籍元数据（`book_meta`）。

若想巩固本讲，可以再读一遍 `tests/test_table_rendering.py` 并为其补一个「表格带 `<caption>` 标签」的用例，观察 markdownify 对 `caption` 的处理与降级路径的交互。
