# 三段式架构总览：解析、处理与渲染

## 1. 本讲目标

本讲是理解 BabelDOC 整体设计的「地图课」。读完本讲，你应该能够：

- 在脑海里建立 **frontend（解析）/ midend（处理）/ backend（渲染）** 三段式心智模型；
- 说清中间表示 **IL（Intermediate Language）** 在流水线中「承上启下」的作用；
- 把 `high_level.py` 里的 `TRANSLATE_STAGES` 列表与三段式架构一一对应；
- 知道这三个阶段的源码分别住在 `babeldoc` 包的哪些目录里，今后看代码不会迷路。

本讲不深入任何单个阶段的算法细节，那是后续单元（u4 解析、u5 中端、u7 渲染）的任务。本讲的唯一目标，是让你拿到一张「全局地图」。

## 2. 前置知识

阅读本讲前，请确保你已经理解下面两个概念（它们在 u1-l1、u1-l3 已建立）：

- **Parsing（解析）与 Rendering（渲染）**：任何 PDF 翻译工具本质都分这两步——先把 PDF「读懂」，再把译文「画出来」。
- **IL（中间表示）与 `TranslationConfig`**：IL 是 BabelDOC 自定义的中间数据格式，保存每个字符、段落的坐标；`TranslationConfig` 是贯穿整个流水线的中心配置对象。

本讲会反复用到几个术语，先统一约定：

| 术语 | 含义 |
| --- | --- |
| frontend（前端/解析端） | 把 PDF 文件解析成 IL 的阶段 |
| midend（中端/处理端） | 在 IL 上做各种处理（版面、段落、公式、翻译、排版）的阶段 |
| backend（后端/渲染端） | 把处理后的 IL 渲染成新 PDF 的阶段 |
| IL | Intermediate Language，BabelDOC 的中间表示，本质是一个保存坐标信息的对象树 |
| stage（阶段） | 流水线里的一个处理步骤，如「Parse Page Layout」 |

如果你对这些术语还陌生，没关系，下面会逐一展开。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [babeldoc/format/pdf/high_level.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py) | 翻译主流程的编排入口，定义 `TRANSLATE_STAGES` 全景表，按顺序调用三段式各阶段 |
| [README.md](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md) | 项目背景，解释 Parsing/Rendering 两阶段与「中间表示」思想 |
| [docs/ImplementationDetails/README.md](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/docs/ImplementationDetails/README.md) | 按真实执行顺序列出的核心处理流程文档索引 |

此外，本讲会「点名」但不精读下面这些目录（它们是三段式的实体落点）：

- 解析前端：`babeldoc/format/pdf/new_parser/`、`babeldoc/format/pdf/document_il/frontend/`
- 中端处理：`babeldoc/format/pdf/document_il/midend/`
- 渲染后端：`babeldoc/format/pdf/document_il/backend/`
- IL 数据模型：`babeldoc/format/pdf/document_il/il_version_1.py`

## 4. 核心概念与源码讲解

### 4.1 三段式架构

#### 4.1.1 概念说明

README 在「Background」一节里把话挑明了：一个 PDF 解析器/翻译器本质上只有两个阶段——

- **Parsing**：解析 PDF 的结构，得到文本块、图片、表格等；
- **Rendering**：把这些结构渲染成新的 PDF 或其他格式。

问题在于：像 mathpix 这类工具，解析后会按「单栏阅读顺序」重新排版渲染，**原版面结构就此丢失**。BabelDOC 的设计选择是：在 Parsing 和 Rendering 之间，引入一个**保留坐标信息的中间表示 IL**，从而可以「贴着原版面」把译文画回去，而不是推倒重排。

于是 BabelDOC 的流水线被自然切成了**三段**：

```
        ┌─────────────┐   IL Document    ┌─────────────┐  IL Document  ┌─────────────┐
PDF ───▶│  frontend   │ ───────────────▶ │   midend    │ ────────────▶ │   backend   │ ──▶ mono / dual PDF
(字节)  │  解析：PDF→IL│  (带坐标的对象树) │  在 IL 上处理 │  (加工后的 IL) │  渲染：IL→PDF │
        └─────────────┘                  └─────────────┘                └─────────────┘
```

三段的核心区别在于「数据形态」：

- **frontend**：输入是 PDF 字节流，输出是一个 `il_version_1.Document` 对象。这是「造 IL」的地方。
- **midend**：输入是一个 IL，输出还是同一个 IL，但被不断加工、补全（加了版面、段落、公式、译文……）。midend 本身又分成多个小阶段（stage）。
- **backend**：输入是加工完毕的 IL，输出是 mono（单语）和 dual（双语对照）两个 PDF 文件。

#### 4.1.2 核心流程

把三段串起来，整个翻译流水线的流程是：

1. 打开并修正 PDF（修 xref、修 filter、修 mediabox 等容错处理）；
2. **frontend**：调用解析器，把修正后的 PDF 变成 IL Document；
3. **midend**：依次运行多个 stage，每个 stage 读取并改写 IL：
   - 扫描件检测 → 版面分析 → 表格解析 → 段落识别 → 公式与样式 → 自动术语抽取 → 翻译 → 排版；
4. **backend**：把排版后的 IL 交给 `PDFCreater`，生成最终的 mono/dual PDF；
5. 后处理：修 cmap、写入元数据、迁移目录（TOC）。

注意一个关键性质：**midend 各阶段的输入输出都是同一个 IL 对象**。也就是说，IL 像「一张被反复涂改的图纸」，每个阶段往上添一层信息，而不是产生一堆中间文件。这种「原地加工」的设计，让阶段之间天然解耦。

#### 4.1.3 源码精读

三段式的「实体落点」最早体现在 `high_level.py` 顶部的 import 分组里——你能一眼看出哪几类模块分别属于哪一段。

先看后端（backend）的导入：[babeldoc/format/pdf/high_level.py:28-31](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L28-L31)，这里导入了 `PDFCreater`、`SUBSET_FONT_STAGE_NAME` 等，它们都来自 `document_il/backend/pdf_creater.py`。

再看中端（midend）的导入：[babeldoc/format/pdf/high_level.py:32-47](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L32-L47)，这一大段几乎全是 `document_il/midend/` 下的 stage 类（`DetectScannedFile`、`LayoutParser`、`ParagraphFinder`、`StylesAndFormulas`、`ILTranslator`、`Typesetting` 等）。仅凭这些 import，你就能预感到：midend 是「最热闹」的一段。

而前端（frontend）的入口是「按需懒加载」的，藏在 `_do_translate_single` 内部：[babeldoc/format/pdf/high_level.py:902-910](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L902-L910)，这里函数内 import 了 `parse_prepared_pdf_with_new_parser_to_legacy_ir` 并调用它——这一行就是「PDF → IL」的真正入口：

```python
from babeldoc.format.pdf.new_parser.native_parse import (
    parse_prepared_pdf_with_new_parser_to_legacy_ir,
)

docs = parse_prepared_pdf_with_new_parser_to_legacy_ir(
    temp_pdf_path,
    config=translation_config,
    doc_pdf=doc_pdf2zh,
)
```

返回的 `docs` 就是一个 IL `Document`，之后所有 midend 阶段都在它上面加工。

最后看后端收尾：[babeldoc/format/pdf/high_level.py:1046-1047](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L1046-L1047)，`PDFCreater(...).write(translation_config)` 把加工好的 IL 渲染成 PDF。从「造 IL」到「画 PDF」，IL 始终是中间那条主线。

> README 对这套思想的原文表述见 [README.md:357](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L357)：「We offer an intermediate representation of the results from parser and can be rendered into a new pdf or other format.」

#### 4.1.4 代码实践

**实践目标**：用一句话分别描述 frontend、midend、backend 三段的「输入 → 输出」，并在 `high_level.py` 中定位每个阶段的源码入口。

**操作步骤**：

1. 打开 [babeldoc/format/pdf/high_level.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py)，跳到 `_do_translate_single` 函数（约第 836 行起）。
2. 顺着函数体往下读，找出三段的「分界点」：
   - frontend 入口在 `parse_prepared_pdf_with_new_parser_to_legacy_ir(...)`（约 906 行）；
   - midend 从 `DetectScannedFile(...).process(...)`（约 942 行）开始，一直到 `Typesetting(...).typesetting_document(docs)`（约 1038 行）；
   - backend 入口在 `PDFCreater(...).write(...)`（约 1047 行）。
3. 把下面这张表填满（示例答案见 4.1.5）：

| 阶段 | 输入 | 输出 | high_level.py 中的源码行 |
| --- | --- | --- | --- |
| frontend | PDF 文件路径 | ？ | 906 行附近 |
| midend | IL Document | ？ | 942–1038 行 |
| backend | ？ | mono/dual PDF | 1047 行附近 |

**需要观察的现象**：你会发现 midend 内部虽然调用了七八个不同的类，但它们**全部操作同一个 `docs` 变量**，没有任何一个阶段返回一个新的 Document 给别的变量名——这正是「原地加工 IL」的直接证据。

**预期结果**：填表后，你应该能用三句话复述整个流水线：「PDF 经 frontend 解析成 IL；midend 一连串阶段在 IL 上补全版面、段落、译文、排版；backend 把 IL 渲染成 mono/dual PDF。」

#### 4.1.5 小练习与答案

**练习 1**：为什么 BabelDOC 要在 Parsing 和 Rendering 之间插一个 IL，而不是像 mathpix 那样直接「解析→单栏重排→渲染」？

**参考答案**：因为单栏重排会丢失原版面信息（字体位置、栏宽、图文混排都丢了）。IL 保留了每个字符/段落的原始坐标，渲染时可以「贴着原版面」把译文画回去，生成保留排版的双语对照 PDF。

**练习 2**：frontend 的输出和 backend 的输入，数据类型分别是什么？

**参考答案**：frontend 的输出是一个 IL `Document`（`il_version_1.Document`）对象；backend 的输入也是这个 IL `Document` 对象（不过是经过 midend 加工后的版本）。两端用的是同一种数据类型，这正是「中间表示」的意义。

**练习 3**：观察 `_do_translate_single` 里 midend 的调用，它们之间是用「返回新对象」还是「原地修改」协作的？这对解耦有什么好处？

**参考答案**：是原地修改——所有 stage 都直接对同一个 `docs` 对象做处理。好处是每个 stage 不需要关心上游产出的具体数据结构细节，只需要「在 IL 上加自己负责的那层信息」，天然解耦，便于单独替换或跳过某个 stage。

---

### 4.2 IL 中间表示定位

#### 4.2.1 概念说明

IL（Intermediate Language）是三段式架构的「脊柱」。理解 IL，要抓住三个要点：

1. **它是一种带坐标的对象树**：IL 不是一个扁平的字符串，而是一棵 `Document → Page → (字符/段落/图形/字体/版面…)` 的树，每个字符都带着自己的 `box`（边界框坐标）。
2. **它是「承上启下」的接口**：frontend 只负责「造」它，backend 只负责「读」它去画图，midend 在中间反复「改」它。三段通过 IL 解耦。
3. **它是可序列化的**：IL 既能转成 JSON（调试用），也能用 schema（`il_version_1.rnc`）约束结构——这点会在 u3 精讲。

类比一下：如果 PDF 是一份「印刷好的成品书」，那么 IL 就是这本书的「带坐标的电子排版稿」。你可以改稿子里的文字（翻译），再按稿子重新印刷（渲染），而不必从扫描件开始重做。

#### 4.2.2 核心流程

IL 在流水线里的生命周期：

1. **诞生**：frontend 解析 PDF 内容流，为每个文本对象计算坐标，构造出初始 IL（此时只有字符和字体信息）。
2. **成长**：midend 各阶段依次往 IL 里「添料」：
   - 版面分析往 `Page` 上加 `PageLayout`（哪些区域是正文、图、表）；
   - 段落识别把散落的字符聚合成 `PdfParagraph`；
   - 公式与样式阶段标记哪些字符是公式、标记富文本样式；
   - 翻译阶段把译文写回段落；
   - 排版阶段给译文算好每个字符的新坐标。
3. **归宿**：backend 读这棵已经「写满译文和新坐标」的树，生成 PDF。

用一句话概括：**IL 是一条「会长大」的数据流**——frontend 给它一个骨架，midend 让它长出血肉，backend 把它定型成 PDF。

#### 4.2.3 源码精读

IL 的数据模型定义在 [babeldoc/format/pdf/document_il/il_version_1.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py)（这份文件由 schema 自动生成，u3 会精讲）。`high_level.py` 在顶部就把它引入了：[babeldoc/format/pdf/high_level.py:27](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L27)：

```python
from babeldoc.format.pdf.document_il import il_version_1
```

「IL 承上启下」最直接的证据，是 `_do_translate_single` 里这样一段——frontend 产出的 `docs` 被立刻喂给一连串 midend 阶段，全程不换变量名。以扫描件检测和版面分析为例：[babeldoc/format/pdf/high_level.py:942-955](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L942-L955)：

```python
DetectScannedFile(translation_config).process(
    docs, temp_pdf_path, mediabox_data
)
...
# Generate layouts for all pages
docs = LayoutParser(translation_config).process(docs, doc_pdf2zh)
```

注意第 954 行 `docs = LayoutParser(...).process(docs, ...)`：输入 `docs`，输出又赋回 `docs`。这就是「IL 在三段之间流转、原地成长」的代码体现。

另一个能直观「看见」IL 的入口是 debug 模式下的 JSON 转储。frontend 一结束就把 IL 写成磁盘文件供人查看：[babeldoc/format/pdf/high_level.py:916-920](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L916-L920)：

```python
if translation_config.debug:
    xml_converter.write_json(
        docs,
        translation_config.get_working_file_path("create_il.debug.json"),
    )
```

这个 `create_il.debug.json` 就是「frontend 刚造好、midend 还没动过」的 IL 快照——你能在里面看到带坐标的字符和段落，是理解 IL 形态的最好材料（u3 会教你读它）。

#### 4.2.4 代码实践

**实践目标**：通过阅读源码，亲眼确认「frontend 产出 IL、midend 加工 IL、backend 消费 IL」这条主线。

**操作步骤**：

1. 在 [babeldoc/format/pdf/high_level.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py) 的 `_do_translate_single` 里，定位三处对 `docs`（IL Document）的使用：
   - **诞生**：第 906 行 `docs = parse_prepared_pdf_with_new_parser_to_legacy_ir(...)`；
   - **成长**：第 954、964、971、978、1003、1038 行，一连串 `process(docs)` / `translate(docs)` / `typesetting_document(docs)`；
   - **归宿**：第 1046 行 `PDFCreater(temp_pdf_path, docs, ...)`，IL 被交给 backend。
2. 数一数：从 `docs` 诞生到被 `PDFCreater` 消费，中间一共有多少个 stage 动过它。

**需要观察的现象**：`docs` 这个变量名从 frontend 一直用到 backend，中间从未被「丢弃重建」，只是被反复 `.process(docs)` 加工。

**预期结果**：你能列出至少 6 个动过 `docs` 的 midend 阶段（DetectScannedFile、LayoutParser、TableParser、ParagraphFinder、StylesAndFormulas、ILTranslator、Typesetting）。这正是「IL 承上启下」的代码铁证。

> 说明：此实践为「源码阅读型实践」，无需运行命令；若你想看真实 IL 形态，可在翻译时加 `--debug`，到 `~/.cache/babeldoc/working` 下找 `create_il.debug.json`（运行结果待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么说 IL「承上启下」？「上」和「下」分别指什么？

**参考答案**：「上」指 frontend（解析端），它负责把 PDF 造 IL；「下」指 backend（渲染端），它负责读 IL 画 PDF。IL 夹在中间，既承接解析结果，又供给渲染所需，所以叫承上启下。

**练习 2**：IL 在流水线中是「被复制传递」还是「被原地加工」？这种选择带来什么好处？

**参考答案**：被原地加工（同一个 `docs` 对象贯穿始终）。好处是内存占用低、阶段间无需序列化拷贝，且每个阶段只需关心「往 IL 上加自己的信息」，彼此解耦。

**练习 3**：如果有人想替换掉 BabelDOC 的翻译实现（比如换成自己的翻译引擎），他应该改 frontend、midend 还是 backend？为什么？

**参考答案**：应该改 midend 里的翻译阶段（`ILTranslator` / `ILTranslatorLLMOnly`），因为翻译就是「读 IL 段落 → 写回译文」这一步，正好在 midend。frontend 和 backend 都不关心译文从哪来，所以不动它们。这也体现了三段式解耦的好处。

---

### 4.3 TRANSLATE_STAGES 全景

#### 4.3.1 概念说明

`TRANSLATE_STAGES` 是 BabelDOC 流水线的「节目单」——它把从 PDF 到成品 PDF 的全过程，拆成一张**有序的阶段表**，每个阶段都带一个**权重数字**。这张表有两个用途：

1. **驱动进度条**：`ProgressMonitor` 用它计算整体进度（overall_progress），让你在翻译时看到百分比在动。
2. **作为三段式的「目录」**：表的每一项，对应三段式里的一个具体 stage，你顺着这张表读代码，就是顺着真实执行顺序读。

理解这张表，等于拿到了一张「带顺序、带耗时占比」的全景地图。

#### 4.3.2 核心流程

`TRANSLATE_STAGES` 是一个「元组列表」，每个元组形如 `("阶段显示名", 权重)`。可以把权重理解为「该阶段相对耗时」，权重越大、阶段越慢。整体进度大致是各阶段进度的加权平均：

\[ \text{overall\_progress} \;\approx\; \frac{\sum_{i} w_i \cdot p_i}{\sum_{i} w_i} \]

其中 \(w_i\) 是第 \(i\) 个阶段的权重，\(p_i\) 是它的当前进度（0–100）。

从这张表能直接读出三段式的边界：

- **frontend**：表里的第一项 `"Parse PDF and Create Intermediate Representation"`；
- **midend**：表中间那一长串（DetectScannedFile → LayoutParser → ... → Typesetting）；
- **backend**：表末尾的 `Generate drawing instructions` / `Subset font` / `Save PDF`（以及前面的 `Add Fonts` 字体映射，它服务于渲染）。

注意：表里有些阶段是**有条件才运行**的（比如表格解析、扫描检测、自动术语抽取）。`get_translation_stage` 会根据配置把不该跑的阶段从表里剔除，得到一份「本次实际要跑的节目单」。

#### 4.3.3 源码精读

整张「节目单」定义在 [babeldoc/format/pdf/high_level.py:60-75](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L60-L75)：

```python
TRANSLATE_STAGES = [
    ("Parse PDF and Create Intermediate Representation", 14.12),
    (DetectScannedFile.stage_name, 2.45),  # DetectScannedFile
    (LayoutParser.stage_name, 14.03),  # Parse Page Layout
    (TableParser.stage_name, 1.0),  # Parse Table
    (ParagraphFinder.stage_name, 6.26),  # Parse Paragraphs
    (StylesAndFormulas.stage_name, 1.66),  # Parse Formulas and Styles
    (AutomaticTermExtractor.stage_name, 30.0),  # Extract Terms
    (ILTranslator.stage_name, 46.96),  # Translate Paragraphs
    (Typesetting.stage_name, 4.71),  # Typesetting
    (FontMapper.stage_name, 0.61),  # Add Fonts
    (PDFCreater.stage_name, 1.96),  # Generate drawing instructions
    (SUBSET_FONT_STAGE_NAME, 0.92),  # Subset font
    (SAVE_PDF_STAGE_NAME, 6.34),  # Save PDF
]
```

读这张表有几个收获：

- **顺序即执行顺序**：从上到下，正是 `_do_translate_single` 里各 stage 被调用的真实顺序。
- **权重透露瓶颈**：`ILTranslator`（翻译段落）权重 46.96 最大，`AutomaticTermExtractor`（自动术语抽取）30.0 次之——说明**翻译和术语抽取是整条流水线最耗时的部分**，这跟直觉吻合（要调 LLM）。
- **阶段名来自常量**：表里用的是 `DetectScannedFile.stage_name` 这类引用，而非硬编码字符串。例如 [babeldoc/format/pdf/document_il/midend/detect_scanned_file.py:20](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/detect_scanned_file.py#L20) 定义 `stage_name = "DetectScannedFile"`，`LayoutParser` 的在 [babeldoc/format/pdf/document_il/midend/layout_parser.py:20](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/layout_parser.py#L20) 定义为 `"Parse Page Layout"`。这样改名只需改一处。

而「有条件剔除」的逻辑在 `get_translation_stage`：[babeldoc/format/pdf/high_level.py:264-296](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L264-L296)。比如开了 `only_parse_generate_pdf`（只解析不翻译），它会一次性把所有翻译相关阶段（DetectScannedFile/LayoutParser/.../Typesetting）全删掉，只保留 frontend 和 backend；又比如没配 `table_model`，就把 `TableParser` 删掉（[第 286-287 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L286-L287)）：

```python
if not translation_config.table_model:
    should_remove.append(TableParser.stage_name)
```

#### 4.3.4 代码实践

**实践目标**：把 `TRANSLATE_STAGES` 这张表，亲手标注成「三段式分区图」。

**操作步骤**：

1. 打开 [babeldoc/format/pdf/high_level.py:60-75](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L60-L75)。
2. 按下表，把 13 个阶段分别归入 frontend / midend / backend 三栏：

| frontend | midend | backend |
| --- | --- | --- |
| Parse PDF and Create IL | DetectScannedFile、LayoutParser、TableParser、ParagraphFinder、StylesAndFormulas、AutomaticTermExtractor、Translate Paragraphs、Typesetting | Add Fonts(FontMapper)、Generate drawing instructions(PDFCreater)、Subset font、Save PDF |

> 提示：`Add Fonts`（FontMapper）虽在 `utils/` 下，但它是为渲染选字体的，归入 backend 侧更合理；`Typesetting`（排版）算译文坐标，是 midend 的最后一步。

3. 对照权重，找出最耗时的 3 个阶段，并想想为什么是它们。

**需要观察的现象**：你会发现 midend 占了 13 项里的 8 项，权重也最大——印证了「midend 是最热闹、最耗时的一段」。

**预期结果**：你能指着这张表说：「frontend 只有 1 项，midend 有 8 项且包含最耗时的翻译（46.96）和术语抽取（30.0），backend 有 4 项负责字体与 PDF 落盘。」

#### 4.3.5 小练习与答案

**练习 1**：`TRANSLATE_STAGES` 里每个元组的第二个数字代表什么？为什么 `ILTranslator` 的数字最大？

**参考答案**：代表该阶段的「相对耗时权重」，用于计算整体进度。`ILTranslator`（46.96）最大，是因为翻译段落要大量调用 LLM，是最慢的环节，所以给它最大权重，让进度条更贴合真实耗时。

**练习 2**：阶段名为什么用 `DetectScannedFile.stage_name` 而不是直接写字符串 `"DetectScannedFile"`？

**参考答案**：用类属性 `stage_name` 作为「单一事实来源」。这样如果某天要改阶段显示名，只需改 `detect_scanned_file.py` 里一处，`TRANSLATE_STAGES`、进度条、debug 输出都会自动跟着变，避免多处硬编码导致不一致。

**练习 3**：`get_translation_stage` 在什么情况下会返回一个「比 `TRANSLATE_STAGES` 短」的列表？举两个例子。

**参考答案**：当配置决定了某些阶段不该跑时，`get_translation_stage` 会把它们剔除。例 1：`only_parse_generate_pdf=True` 时，会删掉所有翻译相关阶段，只剩 frontend + backend；例 2：没配置 `table_model` 时，会删掉 `TableParser`（Parse Table）阶段。

---

### 4.4 frontend/midend/backend 源码分布

#### 4.4.1 概念说明

知道三段式「是什么」之后，还要知道它们「住在哪」。BabelDOC 的源码目录划分，和三段式架构是**高度对齐**的——这是它「结构清晰」的体现。本节就是一张「目录 ↔ 阶段」对照表，帮你在偌大的代码库里快速定位。

回忆 u1-l3 讲过：`format/pdf/` 是主线，其中 `high_level.py` 是编排入口。在它之下，三段各有自己的家：

- **frontend 的家**：`new_parser/`（真正的解析引擎）+ `document_il/frontend/`（把解析结果搭成 IL 的「建造者」）。
- **midend 的家**：`document_il/midend/`，一个文件就是一个 stage。
- **backend 的家**：`document_il/backend/`，目前就一个核心文件 `pdf_creater.py`。
- **IL 模型的家**：`document_il/il_version_1.py`（+ `.rnc`/`.xsd` schema），三段共享。
- **共用工具的家**：`document_il/utils/`，跨阶段复用的字体、空间、矩阵等助手函数。

#### 4.4.2 核心流程

定位一个阶段对应的源码，遵循这个流程：

1. 先看它属于 frontend / midend / backend 哪一段；
2. 到对应目录找同名/相关的 `.py` 文件；
3. midend 的文件通常以**类名**命名（`detect_scanned_file.py` ↔ `DetectScannedFile` 类）；
4. 若涉及共享能力（字体度量、空间索引、矩阵分解），去 `utils/` 找。

把目录和 `TRANSLATE_STAGES` 叠在一起，就是一张完整的「源码地图」。

#### 4.4.3 源码精读

先看三段各自的目录实情。`document_il/` 顶层就按 `frontend/ midend/ backend/` 切开了，外加 IL 模型和工具：

```
babeldoc/format/pdf/document_il/
├── il_version_1.py        ← IL 数据模型（三段共享）
├── il_version_1.rnc/.xsd   ← IL 的 schema 定义
├── xml_converter.py        ← IL ↔ JSON/XML 序列化
├── frontend/               ← 帮助「建造」IL 的建造者
│   ├── il_creater_active.py
│   └── ...
├── midend/                 ← 一个文件 = 一个 stage
│   ├── detect_scanned_file.py
│   ├── layout_parser.py
│   ├── paragraph_finder.py
│   ├── styles_and_formulas.py
│   ├── table_parser.py
│   ├── il_translator.py
│   ├── il_translator_llm_only.py
│   ├── automatic_term_extractor.py
│   ├── typesetting.py
│   └── ...
├── backend/                ← 渲染
│   └── pdf_creater.py
└── utils/                  ← 跨阶段共用助手（fontmap / spatial_analyzer / matrix_helper ...）
```

而真正的「PDF 解析引擎」单独住在 `new_parser/`（因为它是体量最大、最独立的一块）：[babeldoc/format/pdf/new_parser/native_parse.py:39](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_parse.py#L39) 定义了 frontend 的总入口函数 `parse_prepared_pdf_with_new_parser_to_legacy_ir`。

「目录划分与三段对齐」的最强证据，还是 `high_level.py` 顶部的 import 路径本身。比如后端只来自 `backend`：[babeldoc/format/pdf/high_level.py:28-31](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L28-L31)

```python
from babeldoc.format.pdf.document_il.backend.pdf_creater import SAVE_PDF_STAGE_NAME
from babeldoc.format.pdf.document_il.backend.pdf_creater import SUBSET_FONT_STAGE_NAME
from babeldoc.format.pdf.document_il.backend.pdf_creater import PDFCreater
```

中端那一长串 import 全部来自 `...midend.<模块>`，正好和 `midend/` 目录里的文件一一对应：[babeldoc/format/pdf/high_level.py:32-47](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L32-L47)。前端则来自 `...new_parser.native_parse`：[babeldoc/format/pdf/high_level.py:902-904](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L902-L904)。**import 路径就是目录结构的镜像**，这是 BabelDOC 工程整洁的直接体现。

#### 4.4.4 代码实践

**实践目标**：把 `TRANSLATE_STAGES` 的每个阶段，对应到一个真实的源码文件，做出一张「阶段 → 文件」对照表。

**操作步骤**：

1. 列出 `document_il/midend/` 目录下的所有 `.py` 文件（用 `ls babeldoc/format/pdf/document_il/midend/`）。
2. 对照 [TRANSLATE_STAGES](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L60-L75) 里每个 `XxxYyy.stage_name`，找到定义它的类所在的文件。
3. 填出下表（部分示例已给出）：

| 阶段（stage_name） | 所属段 | 源码文件 |
| --- | --- | --- |
| Parse PDF and Create IL | frontend | `new_parser/native_parse.py` |
| DetectScannedFile | midend | `midend/detect_scanned_file.py` |
| Parse Page Layout | midend | `midend/layout_parser.py` |
| Parse Paragraphs | midend | ？ |
| Parse Formulas and Styles | midend | ？ |
| Translate Paragraphs | midend | ？ |
| Typesetting | midend | ？ |
| Generate drawing instructions | backend | `backend/pdf_creater.py` |

**需要观察的现象**：midend 的文件名和 stage 名/类名几乎是「一一对应」的命名（`paragraph_finder.py` ↔ `ParagraphFinder` ↔ `"Parse Paragraphs"`），这种规律让你能从名字反推文件位置。

**预期结果**：你能不查文档，直接凭命名规律定位任意一个 midend 阶段的源码文件。例如看到 `StylesAndFormulas.stage_name`，就知道去 `midend/styles_and_formulas.py`。

#### 4.4.5 小练习与答案

**练习 1**：IL 的数据模型 `il_version_1.Document` 放在哪个文件？为什么它不放在 frontend 或 backend 目录里？

**参考答案**：放在 `document_il/il_version_1.py`。因为它是 frontend、midend、backend **三段共享**的数据结构——frontend 造它、midend 改它、backend 读它，不属于任何单独一段，所以放在三段之外的顶层。

**练习 2**：`fontmap.py`（FontMapper）在 `utils/` 目录下，但它对应 `TRANSLATE_STAGES` 里的 `Add Fonts` 阶段（偏向 backend）。这说明 `utils/` 是什么性质的目录？

**参考答案**：`utils/` 是「跨阶段共用的工具集」，里面的代码可能被 midend、backend 多处复用。`fontmap.py` 虽然主要服务于渲染（选目标字体），但字体度量也可能被 midend 的排版阶段用到，所以归入共用 `utils/` 而非 `backend/`。

**练习 3**：如果有人想新增一个 midend 阶段（比如「图表标题识别」），按 BabelDOC 的目录约定，他应该把新文件放哪？文件和类要怎么命名？

**参考答案**：把新文件放在 `document_il/midend/` 下，命名与类一致（如 `figure_caption_parser.py` ↔ 类 `FigureCaptionParser`），并在类里定义 `stage_name` 常量，最后把它加进 `high_level.py` 的 `TRANSLATE_STAGES` 和 `_do_translate_single` 的调用链。这样就和现有 stage 风格一致。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这张「三段式全景图」的标注任务。

**任务**：基于 [babeldoc/format/pdf/high_level.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py)，画出一张完整的「三段式架构图」，要求包含以下信息：

1. **三段分区**：用 frontend / midend / backend 三栏，分别写出一句话的「输入 → 输出」。
2. **IL 主线**：在图上标出 IL `Document` 在三段之间流转的路径，并指出它在 `_do_translate_single` 中对应的变量名（`docs`）。
3. **阶段填充**：把 `TRANSLATE_STAGES`（[第 60-75 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L60-L75)）的每个阶段，归入对应栏，并写出每个阶段的源码文件路径。
4. **瓶颈标注**：用权重指出最耗时的两个阶段，并解释原因。

**参考画法（ASCII）**：

```
PDF ──[frontend: PDF字节 → IL]──▶ IL Document (docs)
        new_parser/native_parse.py     │
                                       ▼
                              ┌─── midend: IL → IL（原地加工）───┐
                              │ DetectScannedFile  midend/detect_scanned_file.py │
                              │ LayoutParser       midend/layout_parser.py      │
                              │ ParagraphFinder    midend/paragraph_finder.py   │
                              │ StylesAndFormulas  midend/styles_and_formulas.py│
                              │ AutomaticTermExtractor ★30.0（调LLM，耗时）      │
                              │ ILTranslator       ★46.96（调LLM，最耗时）       │
                              │ Typesetting        midend/typesetting.py        │
                              └──────────────────────────────────────┘
                                       │ 同一个 docs
                                       ▼
                              ──[backend: IL → PDF]──▶ mono / dual PDF
                              backend/pdf_creater.py (+ utils/fontmap.py)
```

完成这张图后，你已经具备了阅读 BabelDOC 任何模块所需的「全局坐标系」。后续单元（u4 解析前端、u5 中端、u7 渲染后端）会带你深入每一个方框内部。

## 6. 本讲小结

- BabelDOC 的流水线是 **frontend（解析 PDF→IL）/ midend（在 IL 上加工）/ backend（IL→PDF）** 三段式，IL 是承上启下的中间表示。
- IL 是一棵「带坐标的对象树」，由 frontend 诞生、midend 原地加工、backend 消费，全程复用同一个 `docs` 变量。
- `TRANSLATE_STAGES`（[high_level.py:60-75](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L60-L75)）是流水线的「节目单」，顺序即执行顺序，第二列数字是相对耗时权重；翻译（46.96）和术语抽取（30.0）是最耗时的瓶颈。
- 源码目录与三段式高度对齐：frontend 住 `new_parser/` + `document_il/frontend/`，midend 住 `document_il/midend/`（一个文件一个 stage），backend 住 `document_il/backend/pdf_creater.py`，IL 模型和共用工具放顶层与 `utils/`。
- `high_level.py` 顶部的 import 路径就是目录结构的镜像，import 分组本身就揭示了三段划分。
- 三段式解耦的好处：替换翻译引擎只动 midend、替换解析器只动 frontend、换渲染方式只动 backend，互不干扰。

## 7. 下一步学习建议

本讲只是「拿了地图」，接下来该走进具体的方框：

- **想看清 IL 长什么样** → 学 u3《核心数据模型：中间表示 IL》，精读 `il_version_1.py` 的实体层级，并练习读 `create_il.debug.json`。
- **想理解 frontend 如何把 PDF 变成 IL** → 学 u4《PDF 解析前端：new_parser》，从 `parse_prepared_pdf_with_new_parser_to_legacy_ir` 入手。
- **想逐阶段理解 midend** → 学 u5《中端处理流水线》，按 `TRANSLATE_STAGES` 的真实顺序，依次讲扫描检测、版面、段落、公式、表格。
- **想看主流程是怎么被编排起来的** → 接着学 u2-l2《翻译主流程编排：do_translate 与 _do_translate_single》，逐段精读 `_do_translate_single`。

建议下一步先学 **u2-l2**，把本讲这张「全景图」的每个阶段在 `_do_translate_single` 里的具体调用顺序看明白，再带着它进入 u3–u5 的细节深潜。
