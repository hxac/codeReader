# u1-l4 PDFCraft 门面：公开 API 全景

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `PDFCraft` 类每个公开方法的**输入、输出和适用场景**，并知道什么时候用一步式方法（如 `convert_pdf_to_markdown`）、什么时候用积木式方法（如 `extract_pdf` + `render_markdown`）。
2. 理解门面内部「**提取 extract → 转换 transform → 渲染 render**」的三段式组合方式：一步式方法只是把三段按固定顺序串起来。
3. 理解 `package_path` 参数背后的**临时工作区机制**：不传就落进用完即删的临时目录，传了就得到一个持久化的 `DocumentPackage`，可以反复检查、复用和二次渲染。
4. 理解 `PDFCraft` 构造的**惰性设计**：为什么 `PDFCraft()` 不带任何参数也能合法构造，以及这对「只翻译 EPUB、不碰 PDF」的使用者意味着什么。

本讲是入门单元的收官：u1-l1 给了你能力地图，u1-l2 让你跑通第一次转换，u1-l3 让你认识了模块划分，本讲则把镜头拉近到 `pdf_craft/craft.py` 这一个文件——所有工作流的「总调度台」。

## 2. 前置知识

- **门面模式（facade pattern）**：当系统内部有很多组件、调用者却只想「办一件事」时，提供一个高层的入口类，把内部组件的组装细节藏起来。`PDFCraft` 就是这样一个门面：内部是提取器、渲染器、转换器、管线，对外只暴露十来个方法。
- **上下文管理器（context manager）**：Python 中 `with` 语句管理的对象，进入 `with` 时执行 setup、退出时执行清理。本讲会看到 `TemporaryDirectory`——一个退出 `with` 后**自动删除整个目录**的上下文管理器，它是「临时工作区」的实现基础。
- **frozen dataclass**：用 `@dataclass(frozen=True)` 声明的不可变数据类，创建后字段不能修改。pdf-craft 的 `PDFOptions`、`ExtractionOptions`、`TranslationStep` 都是这种「配置对象」。
- **协议（Protocol）**：Python 的结构化接口——只要一个类「长得像」（有同名方法），就算实现了协议，不需要显式继承。`PackageTransformer`、`ChapterTransformer` 都是协议，u7 会深入，本讲只需知道「转换步骤是一个带 `transform` 方法的对象」。
- **惰性初始化（lazy initialization）**：把昂贵的初始化推迟到真正需要的那一刻。本讲会看到 `PDFCraft` 把 OCR 引擎的导入和构建推迟到第一次提取时。
- **前置讲义**：u1-l2 中你已经用 `convert_pdf_to_markdown` 完成过一次转换，并知道传 `package_path` 可以保留中间包；u1-l3 中你已经知道 `craft.py` 之下还有 `PDFExtractor`、`DocumentPackage`、渲染器等模块。本讲就是把这两头的认知精确对接起来。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `pdf_craft/craft.py` | `PDFCraft` 门面类与 `PDFOptions`、`ExtractionOptions`、`TranslationStep` | **本讲主文件**，全部工作流方法都在这里 |
| `pdf_craft/__init__.py` | 公开 API 边界 | 哪些名字被导出给使用者 |
| `pdf_craft/extractor/pdf/extractor.py` | 提取层入口 `PDFExtractor` | 门面提取调用的下一站 |
| `pdf_craft/transform.py` | 内部提取引擎 `PDFExtractionEngine` | `_pdf_engine()` 惰性导入的目标 |
| `pdf_craft/renderer/markdown/renderer.py` | `MarkdownRenderer` | `render_markdown` 的实际执行者 |
| `pdf_craft/document/package.py` | `DocumentPackage` 中间包契约 | 工作区里到底装了什么 |
| `pdf_craft/transformer/package.py` | `PackageTransformer` 协议与 `ChapterPackageTransformer` | 转换步骤的形状 |
| `pdf_craft/pipeline/epub/__init__.py` | EPUB 翻译管线入口 | `translate_epub` 的转发目标 |
| `tests/test_craft.py` | 门面行为的单元测试 | 用假引擎验证门面编排逻辑 |

## 4. 核心概念与源码讲解

### 4.1 门面模式：一个类串起所有阶段

#### 4.1.1 概念说明

u1-l3 的模块地图告诉你：pdf-craft 内部有提取引擎、两个渲染器、转换器体系、两条格式专属管线。如果你要自己组装「转一本 EPUB」，就需要依次调用 `PDFExtractor`、`ChapterPackageTransformer`、`EpubRenderer`，还要处理目录创建、元数据兜底、临时目录清理……这些编排逻辑如果摊给每个使用者，既容易写错也难以演进。

门面解决的就是这个问题：`PDFCraft` 把「组件怎么组装」收编为库的内部知识，使用者只面对一个类。看它的 import 就能读出它组合了谁——`craft.py` 的开头几乎是一份「组件清单」：

[pdf_craft/craft.py:14-27](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L14-L27) 依次导入了 `DocumentPackage`（中间包契约）、`PDFExtractor`（提取入口）、`LLM`（语言模型配置）、计量类型、OCR 配置、EPUB 翻译管线、PDF 翻译管线、两个渲染器、转换器协议——**门面类的 import 列表，就是它有权调度的组件名单**。

这个门面还有一个容易忽视但很关键的设计：**构造是惰性的**。类文档字符串写得非常直白：

[pdf_craft/craft.py:69-74](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L69-L74) 声明 `PDFCraft` 负责组合提取、渲染和格式专属翻译工作流，并明确说明：构造这个门面**不会初始化 OCR**，因此「只用 EPUB 功能」的调用者可以裸构造 `PDFCraft()`，既不需要 PDF 基础设施也不需要任何凭据。

为什么要这么设计？因为 `translate_epub`（翻译一本现成的 EPUB）从头到尾不碰 PDF、不碰 OCR。如果构造 `PDFCraft` 时就去加载 OCR，那翻译 EPUB 的用户就得白白安装 Poppler、配置 OCR 凭据——这不合理。惰性构造让「能力」和「负担」精确对齐。

#### 4.1.2 核心流程

`PDFCraft` 实例内部只有两个字段，构造几乎是零成本的：

```text
PDFCraft(pdf=PDFOptions(...))
        │
        ├─ self._pdf    ← 保存 PDFOptions（OCR 配置、pdf_handler 等），只是存着，不用
        └─ self._engine ← 通常为 None；测试用 from_engine 注入假引擎
```

当某个方法真正需要提取 PDF 时，才走 `_pdf_engine()`：

```text
_pdf_engine()
  ├─ self._engine 存在？ → 直接返回（测试注入的假引擎）
  ├─ self._pdf 为 None？ → 抛 ValueError（没配 PDFOptions 就想提取）
  └─ 否则 → 惰性 import PDFExtractionEngine，用 PDFOptions 里的字段构建并返回
```

于是门面对外的每个方法可以分为两类：

| 类别 | 是否触发 `_pdf_engine()` | 例子 |
| --- | --- | --- |
| 需要 PDF 基础设施 | 是 | `extract_pdf`、`convert_pdf_to_markdown`、`convert_pdf_to_epub`、`translate_pdf`（含 `_extract_book_meta`） |
| 不需要 | 否 | `translate_epub`、`translate_package`、`render_markdown`、`render_epub`、`patch_pdf_with_package` |

注意 `render_markdown` / `render_epub` 在这张表里的位置：它们接收的是**已经提取好的** `DocumentPackage`，所以不需要引擎。这正是「EPUB-only 用户」的用法基础——拿到别人的包，或者翻译现成 EPUB，全程不触发 OCR。

#### 4.1.3 源码精读

[pdf_craft/craft.py:76-78](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L76-L78) `__init__` 接受可选的 `PDFOptions` 和一个下划线开头的内部参数 `_engine`，函数体只做两个字段的赋值——没有任何 I/O、没有任何模型加载，所以「裸构造」永远合法。

[pdf_craft/craft.py:80-82](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L80-L82) `from_engine` 是一个类方法，把现成的引擎注入门面。在仓库里搜它的调用方，会发现只出现在 `tests/test_craft.py` 中——测试用一个假引擎替换真实提取，从而**只验证门面的编排逻辑**（顺序、参数透传、异常拦截），不用真的跑 OCR。

[pdf_craft/craft.py:253-262](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L253-L262) `_pdf_engine` 是惰性设计的落点：优先返回注入的引擎；没有 `PDFOptions` 就抛出带指引信息的 `ValueError`（告诉你该写 `PDFCraft(pdf=PDFOptions(...))`）；否则才 `from .transform import PDFExtractionEngine`——注意这行 import 在**函数体内**，意味着 `craft.py` 被加载时根本不会导入提取引擎模块。

[pdf_craft/__init__.py:12-14](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/__init__.py#L12-L14) 在公开 API 边界上，`ExtractionOptions`、`PDFCraft`、`PDFOptions`、`TranslationStep` 从 `craft.py` 导出，随后 `translate_epub` 和 PDF 管线类型也从各自模块转出——使用者 `from pdf_craft import PDFCraft` 拿到的就是本讲分析的这个门面。

#### 4.1.4 代码实践

**实践：亲手验证「构造惰性」**

1. **实践目标**：确认 `PDFCraft()` 裸构造不会报错、不会加载 OCR，而未配置 `PDFOptions` 时调用提取方法会得到一条**有指引**的错误。
2. **操作步骤**：新建 `lazy_check.py`（示例代码）：

   ```python
   from pdf_craft import PDFCraft

   craft = PDFCraft()  # 不传任何参数
   print("构造成功，未触发任何 OCR 初始化")

   try:
       craft.extract_pdf("whatever.pdf", "unused_package")
   except ValueError as error:
       print(f"捕获 ValueError: {error}")
   ```

3. **需要观察的现象**：第一行打印正常输出；随后抛出 `ValueError`，错误信息为 `PDF extraction requires PDFCraft(pdf=PDFOptions(...))`。
4. **预期结果**：两行输出都出现。这个实践**不需要** Poppler、不需要 OCR 凭据、不需要真实 PDF 文件——因为 `ValueError` 在 [pdf_craft/craft.py:256-257](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L256-L257) 就被抛出，早于任何文件访问。基于源码可直接推断，具体输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `PDFCraft.__init__` 不在构造时校验 OCR 配置、加载 OCR 模型？

**答案**：因为门面承载了多种工作流，其中 `translate_epub`、`translate_package`、对已有包的 `render_markdown`/`render_epub` 完全不需要 PDF 基础设施。构造时初始化 OCR 会强迫这些用户安装 Poppler、提供凭据。惰性设计（把校验和加载推迟到 `_pdf_engine()`）让「EPUB-only 调用者」零负担，这正是类文档字符串 [pdf_craft/craft.py:70-74](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L70-L74) 承诺的行为。

**练习 2**：`from_engine` 类方法在仓库里主要被谁使用？这说明测试策略上有什么取舍？

**答案**：只被 `tests/test_craft.py` 使用（可用 `Grep "from_engine"` 验证）。测试用假引擎替换真实提取，把「门面的编排逻辑」（方法调用顺序、参数透传、`APPEND_BLOCK` 拦截等）与「昂贵的真实 OCR」解耦——单元测试跑得快且不需要凭据，真实链路留给冒烟测试（u11-l2 会讲）。

**练习 3**：看 `craft.py` 的 import 列表（第 14-27 行），说说门面「组合」了哪几类组件。

**答案**：四类——提取（`PDFExtractor`）、渲染（`MarkdownRenderer`、`EpubRenderer`）、转换（`PackageTransformer`、`ChapterTransformer`、`SubmitKind`）、管线（`pipeline.epub` 的翻译入口、`pipeline.pdf` 的 `PDFTranslationPipeline`），再加上贯穿各处的配置与契约类型（`DocumentPackage`、`LLM`、`OCRConfig`、计量类型）。

### 4.2 工作流方法：积木与一步式组合

#### 4.2.1 概念说明

`PDFCraft` 的公开方法可以分成两组，理解这组划分是本讲的核心：

- **积木式方法（building blocks）**：每个方法只做一件事，返回中间结果，由你自己拼接。适合需要**检查、复用或自定义流程**的场景。
- **一步式方法（workflows）**：`convert_pdf_to_markdown` 和 `convert_pdf_to_epub`，按「**提取 → 转换 → 渲染**」的固定顺序把积木串起来，一步出成品。适合「我只要结果」的场景。

两组方法的全景表（输入/输出按签名归纳）：

| 方法 | 主要输入 | 输出 | 一步到位？ |
| --- | --- | --- | --- |
| `extract_pdf` | PDF 路径 + `package_path` + 选项 | `DocumentPackage` | 否（只提取） |
| `extract_pdf_with_metering` | 同上 | `(DocumentPackage, OCRTokensMetering)` | 否 |
| `render_markdown` | 包 + 输出路径 | `None`（写文件） | 否 |
| `render_epub` | 包 + 输出路径 + 书籍选项 | `None`（写文件） | 否 |
| `translate_package` | 包 + 输出路径 + 章节转换器 | 新的 `DocumentPackage` | 否 |
| `translate_pdf` | 原 PDF + 包 + 转换器 | `None`（写译文 PDF） | 否 |
| `patch_pdf_with_package` | 原 PDF + 包 + 输出路径 | `None`（写回写 PDF） | 否 |
| `translate_epub` | EPUB 路径 + 目标语言等 | `None`（写译文 EPUB） | 否（针对现成 EPUB） |
| `convert_pdf_to_markdown` | PDF 路径 + 输出路径 | `OCRTokensMetering` | **是** |
| `convert_pdf_to_epub` | PDF 路径 + 输出路径 | `OCRTokensMetering` | **是** |

两个值得注意的细节：

1. **`extract_pdf` 的 `package_path` 是必填位置参数**，而一步式方法的 `package_path` 是可选关键字参数。这不是随意为之：提取的产物必须落盘（`DocumentPackage` 是磁盘目录的包装），积木式方法把「落到哪」的决定权（和责任）交给你；一步式方法则替你管理——下一节专门讲。
2. **一步式方法返回的是计量而不是包**。`OCRTokensMetering` 记录本次 OCR 的输入/输出 token 数（u1-l2 已见过），让你估算成本；而包的去向由 `package_path` 决定——不传就随临时目录一起消失。

#### 4.2.2 核心流程

一步式方法的骨架是统一的三段式，以 `convert_pdf_to_markdown` 为例：

```text
with _package_workspace(package_path) as workspace:     # ① 准备工作区（临时或持久）
    package, metering = extract_pdf_with_metering(       # ② 提取：PDF → DocumentPackage
        source, workspace, extraction)
    package = _apply_steps(package, steps)               # ③ 转换：按顺序套用转换步骤
    render_markdown(package, output, assets_path, ...)   # ④ 渲染：包 → Markdown 文件
    return metering                                       # ⑤ 返回 token 计量
```

`convert_pdf_to_epub` 的骨架完全相同，只是第 ④ 步换成 `render_epub`，并多一个兜底：如果调用者没传 `book_meta`，就用 `_extract_book_meta(Path(source))` 从 PDF 元数据里猜书名作者。

转换步骤（`steps`）的执行规则在 `_apply_steps` 里：

```text
_apply_steps(package, steps):
    current = package
    for index, step in enumerate(steps):          # 按传入顺序依次执行
        output = 包目录的父目录 / f"transformed-{index}"
        current = step.transformer.transform(current, output)
    return current                                 # 每一步都产出新包，原包不动
```

也就是说，多个转换步骤会形成一条「包 → 包 → 包」的链，中间产物落在 `transformed-0`、`transformed-1`……子目录里，链的最后一环才交给渲染器。

`translate_pdf` 则展示了另一种组合：它先用一个临时目录生成「译文包」，再调用 `patch_pdf_with_package` 把译文回写到 PDF——**组合发生在门面内部**，同样是积木的拼接。

#### 4.2.3 源码精读

[pdf_craft/craft.py:179-190](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L179-L190) `convert_pdf_to_markdown` 的完整方法体：`with _package_workspace(...)` 包住三段式——提取、`_apply_steps`、`render_markdown`，最后返回 `metering`。注意 `aborted` 中断检查从提取选项一路透传到渲染阶段（`(extraction or ExtractionOptions()).aborted`），让一次调用可以在任何阶段被协作式中止。

[pdf_craft/craft.py:192-210](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L192-L210) `convert_pdf_to_epub` 与 Markdown 版骨架一致；差异在 [pdf_craft/craft.py:205-206](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L205-L206)——`book_meta is None` 时调用 `self._extract_book_meta(Path(source))` 从 PDF 元数据兜底提取书名、作者等信息。

[pdf_craft/craft.py:212-221](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L212-L221) `_apply_steps` 的循环体：每个步骤的输出目录是 `package.chapters_path.parent / f"transformed-{index}"`——即工作区里的兄弟目录。转换之间是**链式**的：`current` 不断被新包替换，最终返回链尾。

[pdf_craft/craft.py:84-89](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L84-L89) `extract_pdf` 只是丢弃计量后的转发——「方便版」；需要 token 统计时用下一行的 `extract_pdf_with_metering`。

[pdf_craft/craft.py:91-110](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L91-L110) `extract_pdf_with_metering` 做的事：构造 `PDFExtractor(self._pdf_engine())`，把 `ExtractionOptions` 的十几个字段**逐个透传**给 `extract_with_metering`。这就是门面的典型工作——不实现逻辑，只做「解包配置 + 转发」。

[pdf_craft/extractor/pdf/extractor.py:14-36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/pdf/extractor.py#L14-L36) 转发的下一站：`PDFExtractor.extract_with_metering` 先确保 `package_path` 目录存在，用 defaults 字典补齐缺省值，然后调用引擎的 `extract_package(...)`，最后用 `DocumentPackage.from_path(package_path)` 把磁盘目录包装成包对象并 `validate()`。**提取的终点是一个通过校验的 `DocumentPackage`**——这是门面拿到的第一个「积木」。

[pdf_craft/renderer/markdown/renderer.py:5-13](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/markdown/renderer.py#L5-L13) 渲染积木：`MarkdownRenderer.render` 先 `package.validate()` 再调用 `render_markdown_file`，把包里的章节 XML 与 assets 渲染成 Markdown。注意 `assets_path or Path("assets")`——`assets_path` 决定 Markdown 里图片链接指向哪里，默认是相对路径 `assets`。

[pdf_craft/craft.py:147-158](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L147-L158) `translate_pdf` 的组合方式：先在循环里检查所有步骤的提交模式，遇到 `APPEND_BLOCK` 立刻抛 `ValueError`（PDF 回写不支持「追加块」）；然后 `with TemporaryDirectory(prefix="pdf-craft-translated-package-")` 生成译文包（用完即删），最后 `patch_pdf_with_package` 回写。这是一个「门面内部组合两个积木 + 一个临时目录」的完整范例。

[pdf_craft/craft.py:121-133](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L121-L133) `translate_package`：把章节转换器包成 `ChapterPackageTransformer` 再执行。转换器的形状见 [pdf_craft/transformer/package.py:16-19](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/package.py#L16-L19) 的 `PackageTransformer` 协议——`transform(package, output_path) -> DocumentPackage`；其实现 [pdf_craft/transformer/package.py:38-56](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/package.py#L38-L56) 的策略是**先复制整个包，再逐个改写章节 XML**，因此原包不会被破坏（u7-l1 展开）。

[pdf_craft/craft.py:174-177](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L174-L177) `translate_epub` 是最薄的转发——直接交给 [pdf_craft/pipeline/epub/__init__.py:1-5](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/__init__.py#L1-L5) 导出的 `translate` 函数。门面在这里的价值只是「统一入口」：所有能力都从 `PDFCraft` 出发。

#### 4.2.4 代码实践

**实践：一步式 vs 两步式，殊途同归**

这是本讲的主实践，直接对应任务书。

1. **实践目标**：验证 `convert_pdf_to_markdown` 一步式调用与「`extract_pdf` + `render_markdown`」两步式调用产出**等价**的 Markdown，并借机观察持久化工作区的内容。
2. **操作步骤**：

   准备：需要一个能访问 OCR 服务的环境（`DeepSeekOCRVendorConfig` 等，见 u1-l2），Poppler 已安装。选一个小 PDF（仓库测试资产可用，如 `tests/assets/mix.pdf`）。新建 `compare_runs.py`（示例代码）：

   ```python
   from pathlib import Path
   from pdf_craft import PDFCraft, PDFOptions
   from pdf_craft.ocr_config import DeepSeekOCRVendorConfig

   craft = PDFCraft(pdf=PDFOptions(
       ocr=DeepSeekOCRVendorConfig(api_key="你的KEY", endpoint="你的ENDPOINT"),
   ))

   # 跑法 A：一步式，保留工作区
   metering_a = craft.convert_pdf_to_markdown(
       "mix.pdf", "one_step.md",
       package_path="workspace_a",   # 持久化，不会被清理
       assets_path="assets",
   )

   # 跑法 B：两步式，先提取再渲染
   package = craft.extract_pdf("mix.pdf", "workspace_b")   # 注意：package_path 是必填参数
   craft.render_markdown(package, "two_step.md", assets_path="assets")

   print("A 计量:", metering_a)
   print("两份 Markdown 是否一致:",
         Path("one_step.md").read_text() == Path("two_step.md").read_text())
   ```

   然后在 shell 里检查工作区结构（示例命令）：

   ```bash
   ls workspace_a workspace_a/chapters workspace_a/assets
   cat workspace_a/document.json
   ```

3. **需要观察的现象**：
   - 脚本最后打印 `True`（两份 Markdown 逐字节一致）；
   - `workspace_a` 与 `workspace_b` 目录结构相同：都有 `chapters/`（章节 XML）、`assets/`（提取出的图片）、`toc.xml`（目录）、`document.json`（含 `schema` 与 `page_pixel_sizes` 字段的元数据）；
   - `A 计量` 打印出本次 OCR 的输入/输出 token 数。
4. **预期结果**：等价性成立的依据在源码里：一步式方法体 [pdf_craft/craft.py:185-189](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L185-L189) 内部调用的正是同样的 `extract_pdf_with_metering` 与 `render_markdown`，没有额外加工。注意保持两次调用的 `assets_path` 一致，否则只有图片链接不同。OCR 具备页级缓存（u3-l3 详述），第二次提取若复用同一工作区会直接命中缓存；本实践用两个不同工作区避免干扰。运行耗时与 token 数待本地验证。
   - 若手头没有 OCR 凭据，可退化为「源码阅读型实践」：对照 [pdf_craft/craft.py:179-190](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L179-L190) 与 [pdf_craft/craft.py:112-119](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L112-L119) 手工推导两条路径调用的方法序列，写出「一步式 = 两步式」的调用链证明。

#### 4.2.5 小练习与答案

**练习 1**：`extract_pdf` 的 `package_path` 是必填参数，`convert_pdf_to_markdown` 的却是可选的。为什么？

**答案**：`DocumentPackage` 是磁盘目录的包装，提取必然产生落盘产物。积木式的 `extract_pdf` 把产物当作返回值的一部分，目录去留由调用者决定，所以必须显式给路径；一步式方法把「工作区管理」也包了进去——不传 `package_path` 时自动用临时目录并在结束时清理（见 4.3），传了就持久化。必填与否反映了「谁负责清理」的差异。

**练习 2**：`convert_pdf_to_epub` 比 `convert_pdf_to_markdown` 多了哪段兜底逻辑？为什么要放在提取之后、渲染之前？

**答案**：多了 `book_meta is None` 时调用 `_extract_book_meta(Path(source))` 从 PDF 元数据提取书名、作者等（[pdf_craft/craft.py:205-206](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L205-L206)）。放在提取之后是因为它复用了 `_pdf_engine()`（惰性初始化已经在提取时完成，不重复构建）；放在渲染之前是因为 `render_epub` 需要 `book_meta` 参数——EPUB 是带元数据的书籍格式，Markdown 不是，所以 Markdown 版没有这段逻辑。

**练习 3**：`translate_pdf` 为什么在开头循环检查 `steps`，发现 `APPEND_BLOCK` 就抛 `ValueError`，而不是等转换出问题再报错？

**答案**：这是**快速失败**（fail fast）设计，见 [pdf_craft/craft.py:152-155](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L152-L155)。`APPEND_BLOCK`（追加块到译文）只有 Markdown/EPUB 这类可变长格式能承接；PDF 回写是把译文排进原版式的固定区域，没有「追加」的余地。提前拦截避免用户白跑昂贵的 OCR/翻译之后才发现参数组合不合法。

### 4.3 临时工作区：package_path 的两种命运

#### 4.3.1 概念说明

每次 PDF 提取都会产生一个完整的中间包（几十上百个 XML、图片、元数据）。大多数使用者只关心最终的 Markdown/EPUB，中间包是垃圾；但也有人需要它——调试提取质量、复用 OCR 缓存、二次渲染、接入自定义转换器。

`package_path` 参数就是把这两种需求分开的开关：

- **不传**：包落进一个**临时目录**（系统 tmp 下、前缀 `pdf-craft-package-`），`with` 块结束自动删除。用户零清理负担，默认体验干净。
- **传**：包落进你指定的**持久目录**，调用结束后依然存在，你可以反复查看和复用。

此外还有两个容易混淆的「临时目录」需要区分：

| 目录 | 谁创建 | 前缀 | 生命周期 | 装什么 |
| --- | --- | --- | --- | --- |
| 提取工作区 | `_package_workspace` | `pdf-craft-package-` | 一步式方法调用期间（传 `package_path` 则持久） | 提取产物包 |
| 翻译包临时目录 | `translate_pdf` 内部 | `pdf-craft-translated-package-` | `translate_pdf` 调用期间 | 回写前的译文包 |
| `transformed-N` 子目录 | `_apply_steps` | 无（位于工作区内） | 随工作区 | 每个转换步骤的中间包 |

#### 4.3.2 核心流程

`_package_workspace` 是一个用 `@contextmanager` 写的上下文管理器，逻辑只有两个分支：

```text
_package_workspace(package_path):
    if package_path 不为 None:
        yield Path(package_path)        # 分支一：原样交出路径，不做任何清理
        return
    with TemporaryDirectory(prefix="pdf-craft-package-") as directory:
        yield Path(directory)           # 分支二：临时目录，with 结束时整个删除
```

于是「一步式方法 + 不传 package_path」的完整生命周期是：

```text
convert_pdf_to_markdown(source, output)      # 未传 package_path
  │
  ├─ 进入 TemporaryDirectory("pdf-craft-package-xxxx")
  │    ├─ 提取：包写入临时目录（chapters/、assets/、toc.xml、document.json）
  │    ├─ 转换步骤：中间包写入 transformed-0/ ...
  │    └─ 渲染：读包 → 写 output（Markdown 文件在临时目录之外，不受影响）
  │
  └─ 退出 with：临时目录连同所有中间产物被删除，只留最终 Markdown
```

而传了 `package_path` 时，同一个 `with` 块只改成交出你的路径——方法结束后，目录里保留着完整的 `DocumentPackage`，可以再用 `DocumentPackage.from_path(...)` 包装、`validate()` 校验、送进 `render_markdown`/`render_epub`/`translate_package` 复用，**不必重跑 OCR**。

#### 4.3.3 源码精读

[pdf_craft/craft.py:270-277](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L270-L277) `_package_workspace` 全文：模块级函数（不是方法），两个分支对应「持久路径」与「临时目录」；临时分支的 `TemporaryDirectory` 在 `with` 退出时删除目录。文档字符串一句话点题：*Provide a persistent package path or a cleaned-up temporary workspace*。

[pdf_craft/craft.py:185-186](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L185-L186) `convert_pdf_to_markdown` 中工作区的使用点：`with _package_workspace(package_path) as workspace` 之后，`workspace` 作为提取的 `package_path` 传入——一步式方法内部用的正是积木式 `extract_pdf` 需要的那个必填参数。

[pdf_craft/craft.py:156-158](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L156-L158) `translate_pdf` 内部的第二个临时目录：译文包先写到 `pdf-craft-translated-package-` 前缀的临时目录，`patch_pdf_with_package` 读完生成译文 PDF 后，临时目录随即销毁。译文 PDF 是最终产物，落在你指定的 `output`，中间的译文包不需要保留。

[pdf_craft/document/package.py:17-26](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py#L17-L26) `DocumentPackage.from_path` 定义了工作区里「装了什么」：`chapters/`、`assets/`、`toc.xml`、`cover.png`（存在才记录）、`document.json`。传了 `package_path` 之后你在磁盘上看到的就是这五项。

[pdf_craft/document/package.py:28-34](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py#L28-L34) `validate` 的前几条检查：`chapters` 与 `assets` 必须是真实目录——所以复用工作区前先 `validate()` 一下，就能确认目录完好（u6-l1 会展开全部校验规则）。

#### 4.3.4 代码实践

**实践：给中间包「验明正身」并复用**

1. **实践目标**：确认持久化工作区在方法结束后依然存在、结构完整，并能被 `DocumentPackage.from_path` 重新加载、二次渲染——不重跑 OCR。
2. **操作步骤**：在 4.2.4 实践的基础上（`workspace_a` 已生成），新建 `reuse_package.py`（示例代码）：

   ```python
   from pathlib import Path
   from pdf_craft import PDFCraft, DocumentPackage

   package = DocumentPackage.from_path(Path("workspace_a"))
   package.validate()                      # 校验 chapters/ 与 assets/ 存在
   print("目录结构完好，页几何元数据:", package.page_pixel_sizes())

   # 二次渲染：不重新提取，直接把已有包渲染成 Markdown
   craft = PDFCraft()                      # 渲染不需要 PDFOptions
   craft.render_markdown(package, "second_render.md", assets_path="assets")
   ```

3. **需要观察的现象**：`validate()` 不抛异常；`page_pixel_sizes()` 打印出记录 OCR 画布尺寸的字典（键为页码）；`second_render.md` 生成且内容与 `one_step.md` 一致；整个过程**没有** OCR 请求发生（耗时几乎为零、无网络流量）。
4. **预期结果**：`render_markdown` 只读取包内容（[pdf_craft/renderer/markdown/renderer.py:10-13](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/renderer/markdown/renderer.py#L10-L13)），不触发 `_pdf_engine()`，因此不需要 OCR 凭据、不产生 token 消耗。文件内容一致性待本地验证；「无网络请求」可以从源码推断——渲染路径上没有任何 OCR 调用。

#### 4.3.5 小练习与答案

**练习 1**：`translate_pdf` 里的译文包用的是临时目录，为什么不让用户传 `package_path` 保留它？

**答案**：`translate_pdf` 的最终产物是译文 PDF（`output`），译文包只是「回写前的中间态」——`patch_pdf_with_package` 消费完它就没有用途了（[pdf_craft/craft.py:156-158](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L156-L158)）。想保留中间包的用户有更好的路径：用 `extract_pdf` 持久化提取包，再单独调用 `translate_package`/`patch_pdf_with_package` 组合。临时目录让常见路径（只想得到译文 PDF）保持零清理。

**练习 2**：不传 `package_path` 跑 `convert_pdf_to_epub`，最终 EPUB 会被临时目录一起删掉吗？

**答案**：不会。EPUB 写到你指定的 `output` 路径，在临时目录**之外**；`with` 块删除的只是提取工作区（包、转换中间产物）。临时目录里的一切都是「中间产物」，最终输出永远落在你给的路径上——这就是 `_package_workspace` 只包住提取/转换、不包住渲染写入的原因。

**练习 3**：`_apply_steps` 把中间包写到 `transformed-{index}` 子目录而不是原地覆盖，带来什么好处？

**答案**：好处有三：一是**原包不可变**——每个转换步骤都消费上一个包、产出新包（[pdf_craft/craft.py:217-220](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L217-L220)），出错时可以回退到任何一步的产物；二是**可检查**——持久化工作区里能看到每一步转换前后的 XML 差异，调试翻译问题时至关重要；三是与 `ChapterPackageTransformer` 的「复制再改写」策略（[pdf_craft/transformer/package.py:38-44](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/package.py#L38-L44)）天然契合。

## 5. 综合实践

**综合任务：做一次「三视角」转换实验，把本讲三个模块串起来。**

用一个真实 PDF（如 `tests/assets/mix.pdf`）完成以下实验，写一份简短的实验记录：

1. **门面视角**（对应 4.1）：先运行 `PDFCraft()` 裸构造的惰性检查脚本（4.1.4），确认「EPUB-only 构造合法、无凭据提取被拦截」；再用 `PDFCraft(pdf=PDFOptions(ocr=...))` 构造正式实例。
2. **工作流视角**（对应 4.2）：分别用一步式（`convert_pdf_to_markdown`，`package_path="run_a"`）与两步式（`extract_pdf` 到 `run_b` + `render_markdown`）各转换一次，`diff` 两份 Markdown 验证等价；再给一步式加一个自定义转换步骤（实现一个 `ChapterTransformer`，例如把每章文本块追加 `[已处理]` 标记，包成 `TranslationStep`），观察 `run_a/transformed-0/` 里的章节 XML 与原章节的差异，确认 Markdown 输出也带上了标记。
3. **工作区视角**（对应 4.3）：转换结束后列出 `run_a` 的目录树（`chapters/`、`assets/`、`toc.xml`、`document.json`，可能还有 `transformed-0/`）；用 `DocumentPackage.from_path(Path("run_a")).validate()` 加载校验；最后用 `craft.render_epub(package, "out.epub")` 从同一个包渲染 EPUB——验证「一次提取、多种输出」。

实验记录需回答三个问题：两份 Markdown 为何必然一致（引用 [pdf_craft/craft.py:185-189](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L185-L189) 的调用链作答）；转换步骤改变了哪些文件、没动哪些文件；从同一包渲染第二种格式时为什么零 OCR 成本。

说明：需要 OCR 凭据与 Poppler 环境才能完整跑通；无凭据时可完成第 3 步的「加载已有包 + 渲染」部分（前提是之前任意一次实践留下过持久化工作区），其余部分退化为源码阅读并标注「待本地验证」。

## 6. 本讲小结

- `PDFCraft` 是组合提取、渲染、转换、管线四类组件的**门面**；构造是惰性的，`PDFCraft()` 裸构造合法，OCR 引擎推迟到 `_pdf_engine()` 第一次被调用时才导入和构建，因此 EPUB-only 用户零负担。
- 公开方法分两组：**积木式**（`extract_pdf`、`render_markdown`、`render_epub`、`translate_package`、`translate_pdf`、`patch_pdf_with_package`、`translate_epub`）各做一件事，由你拼接；**一步式**（`convert_pdf_to_markdown`、`convert_pdf_to_epub`）按「提取 → 转换步骤 → 渲染」固定顺序串联，返回 token 计量。
- `extract_pdf` 的 `package_path` 是必填参数（产物去留由你负责），一步式方法的同名参数可选（门面替你管理）——必填与否反映了清理责任的归属。
- 转换步骤按传入顺序链式执行，每步产出新包落在工作区的 `transformed-{index}` 子目录，原包保持不可变；`translate_pdf` 会提前拦截 PDF 回写不支持的 `APPEND_BLOCK` 提交模式。
- `package_path` 不传时包写入前缀 `pdf-craft-package-` 的临时目录并自动清理；传了则持久化，可用 `DocumentPackage.from_path(...).validate()` 加载复用，二次渲染不再消耗 OCR。

## 7. 下一步学习建议

本讲之后，入门单元（u1）就完整了：你知道了项目是什么（u1-l1）、怎么跑（u1-l2）、模块怎么分（u1-l3）、门面怎么组合（本讲）。接下来进入 u2「配置体系」：

- **u2-l1 六种 OCR 后端配置**：本讲反复出现的 `PDFOptions(ocr=...)` 里的 `ocr` 到底能填什么、本地与远程如何抉择，下一讲逐个拆解 `ocr_config.py`。
- **u2-l2 PDFOptions 与 ExtractionOptions 详解**：本讲只是顺带扫过这两个冻结 dataclass 的字段，下一讲逐项讲页面范围、DPI、token 上限、中断回调等控制项。
- 继续阅读建议：带着本讲的调用链印象去读 [pdf_craft/extractor/pdf/extractor.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/pdf/extractor.py) 与 [pdf_craft/transform.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py)，预习 u3-l1「提取主链路」；对转换步骤感兴趣的可提前翻 [pdf_craft/transformer/package.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/package.py)，u7 会正式展开。
