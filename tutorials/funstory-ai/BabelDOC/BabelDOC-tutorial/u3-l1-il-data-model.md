# IL 数据模型：Document / Page / Paragraph / Character

> 本讲属于「核心数据模型：中间表示 IL」单元（u3），承接 [u2-l1 三段式架构总览](u2-l1-three-stage-architecture.md)。
> 上一讲我们建立了 frontend / midend / backend 三段式心智模型，并指出 **IL（Intermediate Language）是贯穿始终的中间表示**。
> 本讲我们要把 IL 这棵「带坐标的对象树」彻底拆开：它到底有哪些实体？每个实体有哪些字段？字段之间如何嵌套？

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 IL 的核心实体层级：`Document → Page →（PdfCharacter / PdfParagraph / PdfFigure / PdfCurve / PdfFont / PageLayout …）`，并解释为什么是这种「一棵树 + 多个并列集合」的结构。
- 看懂 `il_version_1.py` 里 `@dataclass` + `field(metadata=...)` 的写法，能从字段元数据判断它是 XML 元素还是属性、是否必填、是不是列表。
- 说出 `PdfCharacter`、`PdfParagraph`、`PdfParagraphComposition` 各自的关键字段（`char_unicode`、`unicode`、`box`、`pdf_style` 等）。
- 理解 `Box`、`PdfStyle`、`PageLayout`、`PdfFont` 等支撑类型的作用，知道它们被哪些实体复用。
- 写出一段代码，加载（或构造）一个 IL `Document`，遍历每一页统计字符/段落/曲线数量，并打印第一段的 `unicode` 与 `box`。

## 2. 前置知识

在进入源码前，先建立几个本讲会用到的概念。

### 2.1 什么是「中间表示（IL）」

在前端（frontend）把 PDF 解析完、后端（backend）把它渲染回去之前，BabelDOC 需要一个**中间形态**来承载「这一页有哪些字、每个字在什么位置、属于哪一段、用什么字体」。这个形态就是 IL。它的关键特征是：**每个字符和段落都带有坐标（box）**，所以后端才能「贴着原版面」把译文画回去，而不是推倒重排。

### 2.2 dataclass 与 xsdata

`il_version_1.py` 里的实体几乎都是 Python 标准库的 `@dataclass`，并配合第三方库 **xsdata** 的元数据。阅读时抓住三个要点：

- `@dataclass(slots=True)`：`slots=True` 是为了省内存、加快属性访问。IL 里实体动辄成千上万（一页可能有几千个字符），所以省内存很重要。
- `field(default=None, metadata={...})`：`metadata` 描述这个字段在 XML 序列化时的样子。
- `class Meta: name = "pdfCharacter"`：把 Python 类名 `PdfCharacter` 映射到 XML 元素名 `<pdfCharacter>`。

元数据里最常见的几个键：

| 键 | 含义 |
|---|---|
| `"type": "Element"` | 这个字段是 XML **子元素**（嵌套对象）。 |
| `"type": "Attribute"` | 这个字段是 XML **属性**（写在标签上的键值对）。 |
| `"required": True` | 必填；缺了序列化/校验会报错。 |
| `"name": "fontId"` | 序列化时用的名字（驼峰），和 Python 属性名 `font_id`（下划线）不同。 |
| `"tokens": True` | 一个属性里用空格分隔存多个值（列表）。 |

### 2.3 「一棵树 + 并列集合」的结构

PDF 一页里同时存在「文字」「矢量曲线」「图片」「字体定义」等不同种类的东西。IL 的设计是：**在 `Page` 这一层的下面，按种类分门别类地放成多个并列的列表**（如 `pdf_character`、`pdf_paragraph`、`pdf_curve`、`pdf_font`……），而不是强行塞进一棵单一父子树。这样不同处理阶段（找段落、找公式、找表格）可以只看自己关心的那个列表，互不干扰。

## 3. 本讲源码地图

本讲只聚焦 IL 的「数据模型定义」这一个主题，涉及的关键文件如下：

| 文件 | 作用 |
|---|---|
| [`babeldoc/format/pdf/document_il/il_version_1.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py) | IL 全部实体的 Python 定义（由 schema 自动生成）。本讲的主角。 |
| [`babeldoc/format/pdf/document_il/il_version_1.rnc`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.rnc) | IL 的 RelaxNG Compact schema，是 `.py` 的「上游契约」，更简洁，适合用来速览结构。 |
| [`babeldoc/format/pdf/document_il/xml_converter.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/xml_converter.py) | 用 xsdata 把 IL `Document` 序列化为 XML / JSON 的转换器，加载 IL 时会用到它。 |
| [`babeldoc/format/pdf/high_level.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py) | 主流程；其中把解析得到的 `docs`（即一个 `Document`）写成调试 JSON 的位置，本讲会用来定位「真实的 IL 从哪来」。 |

> 提示：仓库里的 `examples/*.xml`（如 `basic.xml`）是 **DPML** 格式（标签是 `<wp:p>`、`<wp:run>`），**不是** IL 格式，不要混淆。IL 的 XML 标签是 `<pdfCharacter>`、`<pdfParagraph>` 这一类，由 `il_version_1` 定义。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. **Document 与 Page 实体** —— IL 的顶层入口与「一页」的容器。
2. **PdfCharacter 与 PdfParagraph** —— 文字的最小单位与它的聚合。
3. **PdfParagraphComposition 富文本** —— 段落内部如何表达「一段里混着普通字、公式、换行」。
4. **Box / PdfStyle / PageLayout 支撑类型** —— 被大量实体复用的几何、样式、版面描述。

### 4.1 Document 与 Page 实体

#### 4.1.1 概念说明

`Document` 是整份 IL 的**根**，对应一份 PDF。它下面只有一种孩子：`Page`（页）。`Page` 是一页的**容器**，它不直接装「字」，而是装好几个**并列的列表**——这一页所有的字符、段落、曲线、图、矩形、字体、版面区域……都分门别类地挂在 `Page` 下。这种设计让 midend 的每个阶段都能精准地只读自己关心的集合。

#### 4.1.2 核心流程

一个 `Document` 从被创建到被消费的生命周期：

```text
il_creater 初始化:  Document(page=[])            # 空文档，page 列表为空
        │
frontend 解析每页:  Page(...) 被构造并 append 进 Document.page
        │
midend 各阶段:      原地改写 Page 内部的 pdf_paragraph / pdf_character / pdf_curve ...
        │
backend 渲染:       读取 Page 里所有集合，画回 PDF
```

注意：全程复用**同一个** `Document` 对象（主流程里就叫 `docs`），各阶段是在它内部「原地加工」，而不是产出新对象。这正是 u2-l1 讲过的「IL 承上启下、全程复用」。

#### 4.1.3 源码精读

**Document 根实体** —— [`il_version_1.py:1352-1371`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L1352-L1371)（中文说明：定义 IL 的根，包含一个 `page` 列表（至少 1 页）和 `total_pages` 总页数属性）。

关键两行（去掉冗长的 metadata 后）：

```python
@dataclass(slots=True)
class Document:
    class Meta:
        name = "document"
    page: list[Page] = field(default_factory=list, ...)        # min_occurs=1：至少一页
    total_pages: int | None = field(default=None, ...)         # name="totalPages"
```

- `page: list[Page]` 是**一对多**关系（一份文档有多页）；`default_factory=list` 表示默认空列表。
- `total_pages` 的 `metadata["name"]="totalPages"`：Python 用下划线 `total_pages`，序列化成 XML/JSON 时写成驼峰 `totalPages`。

**Page 容器** —— [`il_version_1.py:1244-1349`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L1244-L1349)（中文说明：定义「一页」的容器，下面挂载 mediabox/cropbox 几何、以及字符/段落/曲线/图/字体/版面等并列集合）。

`Page` 下的字段可以分成三类：

| 类别 | 字段（Python 属性名） | 含义 |
|---|---|---|
| 几何 | `mediabox`, `cropbox` | 页面的媒体盒与裁剪盒（决定页面大小与可视区域） |
| 内容集合 | `pdf_paragraph`, `pdf_character`, `pdf_curve`, `pdf_figure`, `pdf_rectangle`, `pdf_form`, `pdf_xobject` | 这一页的各种内容，全是 `list[...]` |
| 元信息集合 | `pdf_font`, `page_layout` | 这一页用到的字体定义、版面分析得到的区域 |
| 属性 | `page_number`, `unit` | 页码、坐标单位（如 `pt`） |
| 原始指令 | `base_operations` | 解析时透传的底层 PDF 绘图指令（字符串） |

对应的 schema 写法更简洁，可以对照看 [`il_version_1.rnc:7-23`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.rnc#L7-L23)（中文说明：Page 元素里依次声明 mediabox、cropbox，以及若干 `*`（零或多个）的内容元素，最后是 pageNumber 与 Unit 属性）：

```rnc
Page =
  element page {
    element mediabox { Box },
    element cropbox { Box },
    PDFXobject*,
    PageLayout*,
    PDFRectangle*,
    PDFFont*,
    PDFParagraph*,
    PDFFigure*,
    PDFCharacter*,
    PDFCurve*,
    PDFForm*,
    attribute pageNumber { xsd:int },
    attribute Unit { xsd:string },
    element baseOperations { xsd:string }
  }
```

> `*` 表示「零或多个」，对应 Python 里的 `list[...]`；不带符号的（如 `mediabox`）是「恰好一个」，对应必填字段。

**真实的 Document 从哪来** —— 解析器返回的就是这个类型。前端创建处 [`il_creater_active.py:201`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py#L201)（中文说明：解析会话开始时先用 `il_version_1.Document(page=[])` 建一个空文档，随后逐页填充）。主流程里拿到它之后会写成调试 JSON：[`high_level.py:906-920`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L906-L920)（中文说明：`parse_prepared_pdf_with_new_parser_to_legacy_ir` 返回 `docs`（一个 Document），在 `--debug` 下用 `xml_converter.write_json` 写出 `create_il.debug.json`）。

#### 4.1.4 代码实践

**目标**：用肉眼确认 `Document` 与 `Page` 的嵌套关系。

**步骤**：

1. 打开 [`il_version_1.py:1244`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L1244) 的 `Page` 类。
2. 数一数 `Page` 下一共有多少个 `list[...]` 字段（即「并列集合」）。
3. 打开 [`il_version_1.rnc:7-23`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.rnc#L7-L23)，核对 schema 里带 `*` 的元素是否和 Python 里的 `list` 字段一一对应。

**预期结果**：`Page` 下应有 9 个列表字段（`pdf_xobject`、`page_layout`、`pdf_rectangle`、`pdf_font`、`pdf_paragraph`、`pdf_figure`、`pdf_character`、`pdf_curve`、`pdf_form`），与 schema 中 9 个带 `*` 的元素对应。

#### 4.1.5 小练习与答案

**练习 1**：`Document.page` 的 `min_occurs=1` 是什么意思？为什么文档不能一页都没有？

> **答案**：表示 `page` 列表至少要有 1 个元素。因为 `Document` 代表一份真实的 PDF，而 PDF 至少有一页；空文档没有意义，也无法渲染。

**练习 2**：`total_pages` 在 Python 里是下划线命名，为什么 schema 里要叫 `totalPages`？

> **答案**：Python 内部统一用下划线风格（PEP 8）；而 XML/JSON 习惯用驼峰。`metadata["name"]="totalPages"` 让 xsdata 在序列化时输出驼峰，反序列化时再映射回 `total_pages`，两边各用各的惯例。

---

### 4.2 PdfCharacter 与 PdfParagraph

#### 4.2.1 概念说明

- **`PdfCharacter`** 是文字的**最小单位**：一个字符。它带着这个字符的 Unicode、在页面上的坐标盒（box）、所用样式（字体 + 字号）、以及一些渲染控制位（是否竖排、缩放、渲染顺序等）。一页可能有几千个 `PdfCharacter`，它们是前端从 PDF 内容流里逐字「摊开」出来的。
- **`PdfParagraph`** 是 midend 的段落识别阶段（`ParagraphFinder`）把散落的字符**聚合成段落**后的产物。它有一个整段的 `box`、整段的样式、一个 `unicode` 字符串（整段纯文本），以及一个 `pdf_paragraph_composition` 列表（见 4.3）描述段落内部的精细结构。

一句话区分：`PdfCharacter` 是「原材料」（每个字在哪），`PdfParagraph` 是「加工品」（这些字组成了一段话，整段在哪、整段是什么文本）。

#### 4.2.2 核心流程

```text
PDF 内容流中的文本操作符(Tj/TJ)
        │  frontend glyphs 展开 + 定位
        ▼
Page.pdf_character[]   ← 每个字一个 PdfCharacter（带 box / char_unicode / pdf_style）
        │  midend ParagraphFinder：按坐标聚合成行、再聚合成段
        ▼
Page.pdf_paragraph[]   ← PdfParagraph（整段 box / 整段 unicode / composition）
```

注意两件事：

1. **字符不会被删掉**：聚合成段落后，原来的 `pdf_character` 列表仍在 `Page` 上；段落内部通过 `pdf_paragraph_composition` 引用/重组这些字符（详见 4.3）。所以一页上「字符视角」和「段落视角」是并存的。
2. **段落的 `unicode` 与 composition 的关系**：`unicode` 是整段的纯文本快照（方便翻译时整段送 LLM）；`composition` 则保留了「哪几个字是同一样式、哪里嵌了公式、哪里换了行」的精细结构，供后端逐字排版时使用。

#### 4.2.3 源码精读

**PdfCharacter** —— [`il_version_1.py:626-716`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L626-L716)（中文说明：定义单个字符，含样式 `pdf_style`、坐标盒 `box`、可选的视觉盒 `visual_bbox`、Unicode 文本 `char_unicode`、以及竖排/缩放/渲染顺序等控制位）。

精简后的关键字段：

```python
@dataclass(slots=True)
class PdfCharacter:
    class Meta:
        name = "pdfCharacter"
    pdf_style: PdfStyle | None        # 这个字用什么字体+字号 (Element, required)
    box: Box | None                   # 这个字在页面上的坐标盒 (Element, required)
    visual_bbox: VisualBbox | None    # 字形实际「视觉」包围盒（可选，比 box 更贴字形）
    vertical: bool | None             # 是否竖排
    scale: float | None               # 缩放系数
    char_unicode: str | None          # ← 这个字的 Unicode 文本 (required)
    advance: float | None             # 写完这个字后画笔前进的距离
    render_order: int | None          # 渲染顺序（后端按它排序画）
    sub_render_order: int | None      # 子级渲染顺序
```

> 重点记住三个：`char_unicode`（字是什么）、`box`（字在哪）、`pdf_style`（字长什么样）。这三个是字符的「身份 + 位置 + 长相」。

**PdfParagraph** —— [`il_version_1.py:1151-1241`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L1151-L1241)（中文说明：定义一个段落，含整段 `box`、整段 `pdf_style`、精细结构列表 `pdf_paragraph_composition`、整段纯文本 `unicode`，以及缩放/版面标签等属性）。

精简后的关键字段：

```python
@dataclass(slots=True)
class PdfParagraph:
    class Meta:
        name = "pdfParagraph"
    box: Box | None                             # 整段的坐标盒 (required)
    pdf_style: PdfStyle | None                  # 整段的基础样式 (required)
    pdf_paragraph_composition: list[...]        # 段落内部的精细结构（见 4.3）
    unicode: str | None                         # ← 整段纯文本快照 (required)
    optimal_scale: float | None                 # 排版阶段算出的最优缩放
    vertical: bool | None                       # 是否竖排段落
    first_line_indent: bool | None              # 是否首行缩进
    layout_id: int | None                       # 所属版面区域 id（关联 PageLayout）
    layout_label: str | None                    # 版面区域类别标签（如 title/text）
    render_order: int | None                    # 渲染顺序
```

- `unicode` 是整段文本，翻译阶段（ILTranslator）主要拿它去翻译。
- `layout_id` / `layout_label` 把段落挂到版面分析（`PageLayout`）的结果上——本讲 4.4 会讲 `PageLayout`。
- `optimal_scale` 是排版阶段（Typesetting）回填的字段，初建时通常是 `None`，说明 **IL 的字段会随流水线阶段被逐步「填满/改写」**。

对照 schema：[`il_version_1.rnc:67-83`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.rnc#L67-L83)（PdfCharacter）与 [`il_version_1.rnc:101-116`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.rnc#L101-L116)（PdfParagraph）。

#### 4.2.4 代码实践

**目标**：理解「字符视角」与「段落视角」并存。

**步骤**：

1. 在 `Page` 上（[`il_version_1.py:1291`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L1291)）确认 `pdf_paragraph` 与 `pdf_character` 是两个**独立**的列表字段。
2. 思考：一段话「Hello」由 5 个字符组成。解析后 `Page.pdf_character` 里会有 5 个 `PdfCharacter`；段落识别后 `Page.pdf_paragraph` 里会有 1 个 `PdfParagraph`，其 `unicode == "Hello"`。
3. 思考：如果只改 `pdf_paragraph[0].unicode` 而不动 `pdf_character`，会发生什么不一致？

**预期结果 / 待本地验证**：你能说出「`unicode` 是快照、`composition` 才是后端排版的依据」这一结论。真正修改文本时两处都要同步，否则会出现「段落文本变了但字符坐标没变」的错位。具体行为可在第 5 节综合实践中用真实数据观察。

#### 4.2.5 小练习与答案

**练习 1**：`PdfCharacter` 的 `char_unicode` 和 `PdfParagraph` 的 `unicode` 有什么区别？

> **答案**：`char_unicode` 是**单个字符**的 Unicode；`unicode` 是**整段**的纯文本字符串（把段内字符拼起来）。前者是字级，后者是段级。

**练习 2**：为什么 `PdfParagraph.box` 是「整段」的盒，而段内每个字还有自己的 `box`？

> **答案**：整段 `box` 是段落的外接矩形，用于版面布局、判断段落位置、和其他元素做空间关系判断（如是否在某个版面区域内）；字级 `box` 用于后端逐字精确排版。两者粒度不同、用途不同。

---

### 4.3 PdfParagraphComposition 富文本

#### 4.3.1 概念说明

一段话往往不是「纯文本」那么简单：中间可能夹着一个公式（\(E=mc^2\)）、一段加粗的文字、一个手动换行。`PdfParagraphComposition` 就是用来表达**段落内部的混合结构**的——它是「富文本」的基本单元。一个段落下有**多个** `PdfParagraphComposition`，按顺序拼起来就是整段。

#### 4.3.2 核心流程

`PdfParagraphComposition` 是一个**多选一（choice）**容器，schema 里写得很直白：[`il_version_1.rnc:117-124`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.rnc#L117-L124)（中文说明：一个 composition 节点可以是下列五种之一：行、公式、同样式字符组、单字符、同样式 Unicode 字符组）。

```rnc
PDFParagraphComposition =
  element pdfParagraphComposition {
    PDFLine
    | PDFFormula
    | PDFSameStyleCharacters
    | PDFCharacter
    | PDFSameStyleUnicodeCharacters
  }
```

在 Python 里，「多选一」被表达成：5 个字段都是 `可选`，同一时刻**只有一个不为 `None`**：

```text
PdfParagraphComposition
├── pdf_line: PdfLine | None                      # 一行（含若干字符）
├── pdf_formula: PdfFormula | None                # 一个公式（含字符/曲线/表单）
├── pdf_same_style_characters: PdfSameStyleCharacters | None   # 一组同字体同字号的字
├── pdf_character: PdfCharacter | None            # 单个字符
└── pdf_same_style_unicode_characters: PdfSameStyleUnicodeCharacters | None  # 同样式纯文本片段
```

整段就是这些节点的**有序拼接**：例如 `["同样式字'In '", "公式 E=mc²", "同样式字' we trust.']`。

#### 4.3.3 源码精读

**PdfParagraphComposition** —— [`il_version_1.py:1109-1148`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L1109-L1148)（中文说明：段落内部的一个组成片段，5 个可选字段对应 schema 的 5 种 choice，任一时刻只有一项非空）。

它引用的几种子类型也都定义在同一文件，各自承载不同信息：

| 子类型 | 行号 | 含义 |
|---|---|---|
| `PdfLine` | [`1050-1076`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L1050-L1076) | 一行：一个 `box` + 若干 `PdfCharacter`，可选 `render_order` |
| `PdfFormula` | [`981-1047`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L981-L1047) | 公式：含 `pdf_character`/`pdf_curve`/`pdf_form`，以及 `x_offset`/`y_offset` 偏移 |
| `PdfSameStyleCharacters` | [`1079-1106`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L1079-L1106) | 同样式字符组：一个 `box` + 一个 `PdfStyle` + 若干 `PdfCharacter` |
| `PdfCharacter` | [`626-716`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L626-L716) | 复用 4.2 讲的单字符实体 |
| `PdfSameStyleUnicodeCharacters` | [`909-933`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L909-L933) | 同样式纯文本片段：一个 `unicode` 字符串 + 可选样式（不含字级坐标） |

> 设计要点：`PdfSameStyleUnicodeCharacters` 只有 `unicode` 而**没有**逐字 `box`——它用于「样式一致、无需逐字定位」的纯文本片段（典型场景是**译文**写回时：译文逐字坐标由排版阶段现算，不需要逐字 box）。这正是翻译流程能高效替换文本的关键。

**PdfFormula 的偏移字段** —— [`il_version_1.py:1015-1034`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L1015-L1034)（中文说明：公式带有 `x_offset`/`y_offset`（必填）与 `x_advance`（可选），描述公式在行内的水平/垂直偏移与占位宽度，供排版对齐公式）。

#### 4.3.4 代码实践

**目标**：学会判断一个 composition 节点到底是 5 种里的哪一种。

**步骤**：阅读 [`il_version_1.py:1109-1148`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L1109-L1148)，写一段伪代码判断某个 composition 节点 `c` 的类型：

```python
# 示例代码（伪代码，演示判断逻辑）
def describe(c):
    if c.pdf_line is not None:                 return "一行"
    if c.pdf_formula is not None:              return "公式"
    if c.pdf_same_style_characters is not None:return "同样式字符组"
    if c.pdf_character is not None:            return "单字符"
    if c.pdf_same_style_unicode_characters is not None: return "同样式文本"
    return "空"
```

**预期结果**：对一个真实的段落，依次判断其每个 composition 节点，能还原出「这段话 = 文本 + 公式 + 文本」这样的结构序列。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `PdfParagraphComposition` 用「5 个可空字段」而不是「一个带类型标签的联合体」来表达多选一？

> **答案**：因为 Python 的 xsdata dataclass 要和 XML schema 的 `choice` 一一对应。XML 序列化时，哪个子元素出现就填哪个字段、其余留空；读回来时按出现的子元素名回填对应字段。这样既符合 XML 习惯，又能在 Python 里直接用 `is not None` 判断。

**练习 2**：`PdfSameStyleUnicodeCharacters` 为什么没有逐字 `box`？

> **答案**：它代表「样式一致的一段纯文本」，常用于译文。译文是重新生成的，逐字坐标由排版阶段（Typesetting）现算，所以在 IL 里不需要预先存逐字 box，只存整段 `unicode` 即可，节省空间且更灵活。

---

### 4.4 Box / PdfStyle / PageLayout 支撑类型

#### 4.4.1 概念说明

有一批类型本身不是「内容实体」，而是被大量实体**复用**的「描述片段」：

- **`Box`**：一个矩形坐标盒 `(x, y, x2, y2)`，几乎所有有位置的东西（字符、段落、曲线、图、版面区域……）都内嵌一个 `Box`。它是 IL「带坐标」的最小单元。
- **`PdfStyle`**：文字样式 = `GraphicState`（图形状态）+ `font_id`（字体 id）+ `font_size`（字号）。字符、段落、同样式字符组都用它。
- **`PageLayout`**：版面分析（`LayoutParser`，详见 u5）识别出的一个区域，带 `class_name`（类别：标题/正文/图/表/公式…）、`conf`（置信度）、`id` 和 `box`。段落的 `layout_id` 就是指向它。
- **`PdfFont`**：字体定义，挂在 `Page` 上，存字体名、子类型（Type1/TrueType/CID…）、粗体/斜体/衬线标志、ascent/descent 等度量。`PdfStyle.font_id` 引用的就是它。

理解这批支撑类型，你就能解释 IL 里「位置、长相、版面归属」这三类信息的统一表达方式。

#### 4.4.2 核心流程

这些类型像「乐高积木」一样被拼装：

```text
PdfStyle = GraphicState + font_id + font_size
                                  │ font_id 引用
                                  ▼
                               PdfFont   （字体度量、子类型）挂在 Page.pdf_font[]

Box (x,y,x2,y2)
  ├── 内嵌于 PdfCharacter.box / PdfParagraph.box / PdfCurve.box / PdfFigure.box ...
  └── 内嵌于 PageLayout.box   （版面区域也用同一个 Box）

PageLayout (id, class_name, conf, box)
                                  ▲ layout_id 引用
                                  │
PdfParagraph.layout_id ───────────┘   （段落挂到某个版面区域）
```

`Box` 的几何含义很简单：`(x, y)` 是左下角，`(x2, y2)` 是右上角（schema 注释 `# from (x,y) to (x2,y2)`）。盒子的宽高为：

\[
\text{width} = x_2 - x,\qquad \text{height} = y_2 - y
\]

> 坐标原点与单位由 `Page.unit`（如 `pt`）和 PDF 的页面坐标系决定；本讲只需记住「盒子的四个数定义了一个轴对齐矩形」。

#### 4.4.3 源码精读

**Box** —— [`il_version_1.py:18-50`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L18-L50)（中文说明：定义坐标盒，四个必填浮点属性 `x, y, x2, y2`，表示从 `(x,y)` 到 `(x2,y2)` 的矩形）：

```python
@dataclass(slots=True)
class Box:
    class Meta:
        name = "box"
    x: float | None    # required
    y: float | None    # required
    x2: float | None   # required
    y2: float | None   # required
```

对照 schema [`il_version_1.rnc:24-31`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.rnc#L24-L31)。

**PdfStyle** —— [`il_version_1.py:583-609`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L583-L609)（中文说明：文字样式，由 `graphic_state`（图形状态）、`font_id`（字体 id）、`font_size`（字号）三个必填项组成）：

```python
@dataclass(slots=True)
class PdfStyle:
    class Meta:
        name = "pdfStyle"
    graphic_state: GraphicState | None   # required
    font_id: str | None                  # required，引用 PdfFont.font_id
    font_size: float | None              # required
```

**PageLayout** —— [`il_version_1.py:313-345`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L313-L345)（中文说明：版面区域，含 `box`、`id`、`conf`（置信度）、`class_name`（区域类别））。注意它的 `class_name`（如 `title`/`text`/`figure`/`table`/`isolate_formula`）来自 DocLayout-YOLO 模型，段落通过 `layout_id`/`layout_label` 与之关联。

**PdfFont** —— [`il_version_1.py:362-468`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L362-L468)（中文说明：字体定义，挂在 `Page.pdf_font[]`，含 `name`/`font_id`/`xref_id`/`encoding_length`/`font_subtype`，以及 `bold`/`italic`/`serif`/`monospace` 标志和 `ascent`/`descent` 度量，还有逐字包围盒列表 `pdf_font_char_bounding_box`）。`PdfStyle.font_id` 正是指向这里的 `font_id`。

#### 4.4.4 代码实践

**目标**：看清支撑类型被谁复用。

**步骤**：

1. 在 `il_version_1.py` 里搜索 `box: Box | None`，数一数有多少种实体内嵌了 `Box`（提示：字符、段落、曲线、矩形、图、公式、行、版面区域……）。
2. 搜索 `pdf_style: PdfStyle | None`，确认 `PdfCharacter`、`PdfParagraph`、`PdfSameStyleCharacters` 都复用了同一个 `PdfStyle`。

**预期结果**：你会发现 `Box` 和 `PdfStyle` 是被复用最多的两个支撑类型——这正是 IL「位置与样式统一表达」的体现。

#### 4.4.5 小练习与答案

**练习 1**：`PdfParagraph.layout_id` 和 `PageLayout.id` 是什么关系？

> **答案**：一对「逻辑外键」关系。`Page.pdf_layout[]` 里每个 `PageLayout` 有唯一 `id` 和类别 `class_name`；段落的 `layout_id` 存的就是某个 `PageLayout.id`，表示「这个段落落在那个版面区域内」。这是 schema 层的逻辑关联，并非数据库外键。

**练习 2**：为什么把字体信息单独做成 `PdfFont` 挂在 `Page` 上，而不是每个字符都存一份完整字体？

> **答案**：一份文档往往反复使用少数几种字体，若每个字符都存完整字体定义会大量冗余。把字体集中存在 `Page.pdf_font[]`、字符只存 `font_id` 引用，既省空间又便于统一管理（如后端做字体子集化时按 `font_id` 聚合字形）。

---

## 5. 综合实践

把本讲四个模块串起来：**写代码加载（构造）一个 IL `Document`，遍历每页统计 `pdf_character`、`pdf_paragraph`、`pdf_curve` 的数量，并打印第一段的 `unicode` 与 `box`。**

### 5.1 实践目标

- 用真实的 `il_version_1` 数据类**亲手构造**一棵小型 IL 树，验证你对字段名与嵌套关系的理解。
- 写出一段适用于**任意** `il_version_1.Document` 的遍历统计函数——它同样能用在解析器返回的真实 `docs` 上。

### 5.2 操作步骤

把下面的「示例代码」保存为 `il_walk.py`，然后在装有 BabelDOC 的环境里运行（`python il_walk.py`）。

```python
# 示例代码：构造一个最小 IL Document 并遍历统计
from babeldoc.format.pdf.document_il import il_version_1 as il


def box(x, y, x2, y2):
    return il.Box(x=x, y=y, x2=x2, y2=y2)


def style(font_id="F1", font_size=12.0):
    # PdfStyle 需要 graphic_state（必填 Element）；GraphicState 所有字段可选
    return il.PdfStyle(graphic_state=il.GraphicState(),
                       font_id=font_id, font_size=font_size)


# 1) 构造第一页：必填 mediabox / cropbox / base_operations / page_number / unit
page = il.Page(
    mediabox=il.Mediabox(box=box(0, 0, 612, 792)),
    cropbox=il.Cropbox(box=box(0, 0, 612, 792)),
    base_operations=il.BaseOperations(value=""),
    page_number=1,
    unit="pt",
)

# 2) 放两个字符（字级 box + char_unicode）
page.pdf_character.append(il.PdfCharacter(
    pdf_style=style(), box=box(100, 700, 110, 712), char_unicode="H"))
page.pdf_character.append(il.PdfCharacter(
    pdf_style=style(), box=box(110, 700, 120, 712), char_unicode="i"))

# 3) 放一段文字（整段 box + 整段 unicode）
page.pdf_paragraph.append(il.PdfParagraph(
    box=box(100, 700, 120, 712), pdf_style=style(), unicode="Hi"))

# 4) 组装 Document（page 至少 1 个）
doc = il.Document(page=[page], total_pages=1)


# —— 适用于任意 il_version_1.Document 的遍历函数 ——
def walk(doc):
    for p in doc.page:
        yield (p.page_number,
               len(p.pdf_character),
               len(p.pdf_paragraph),
               len(p.pdf_curve))


for page_number, n_char, n_para, n_curve in walk(doc):
    print(f"Page {page_number}: "
          f"characters={n_char}, paragraphs={n_para}, curves={n_curve}")

# 打印第一页第一段的 unicode 与 box
first_p = doc.page[0].pdf_paragraph[0]
b = first_p.box
print("first paragraph unicode:", first_p.unicode)
print(f"first paragraph box: x={b.x}, y={b.y}, x2={b.x2}, y2={b.y2}")
```

### 5.3 需要观察的现象

- 程序不报错地构造出 `Document`，说明你对**必填字段**（`mediabox`/`cropbox`/`base_operations`/`page_number`/`unit`/`box`/`pdf_style`/`unicode`/`char_unicode`/`total_pages`）的判断是对的。
- 输出形如：`Page 1: characters=2, paragraphs=1, curves=0`。
- 第一段打印：`first paragraph unicode: Hi` 以及 `box: x=100, y=700, x2=120, y2=712`。

### 5.4 预期结果

代码能正常运行并打印上述结果。这个 `walk()` 函数拿到真实文档时同样适用——因为真实的 `docs`（由 `parse_prepared_pdf_with_new_parser_to_legacy_ir` 返回，见 [`high_level.py:906`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L906)）就是同一个 `il_version_1.Document` 类型。

### 5.5 进阶（可选）：跑真实文档

1. 用 `babeldoc --openai ... --files xxx.pdf --debug` 翻译任意 PDF，`--debug` 会在工作目录写出 `create_il.debug.json`（写出位置：[`high_level.py:916-920`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L916-L920)）。
2. 该 JSON 由 [`xml_converter.py:50-61`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/xml_converter.py#L50-L61) 的 `to_json/write_json` 生成，其嵌套结构与本讲讲的字段一致（键名对应 Python 属性名）。
3. 你也可以用 [`xml_converter.py:33-35`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/xml_converter.py#L33-L35) 的 `XMLConverter().read_xml(path)` 从一个 IL XML 文件读回 `Document` 对象（注意：仓库自带的 `examples/*.xml` 是 DPML 格式，**不是** IL XML，不能直接用）。

> 若你手头没有可翻译的 PDF 或网络受限，第 5.2 节的内存构造示例已能完整验证学习目标；进阶部分标注为「待本地验证」。

---

## 6. 本讲小结

- IL 的顶层是 `Document`（根，含 `page` 列表与 `total_pages`），`Page` 是一页的容器，下面挂载 **9 个并列集合**（字符、段落、曲线、图、矩形、表单、xobject、字体、版面区域）——「一棵树 + 并列集合」的结构。
- `PdfCharacter` 是字级最小单位（`char_unicode` + `box` + `pdf_style`），`PdfParagraph` 是段落级聚合（整段 `unicode` + `box` + `pdf_style` + composition）；字级与段级视角并存。
- `PdfParagraphComposition` 用「5 个可空字段」表达段落内部的富文本多选一（行 / 公式 / 同样式字符组 / 单字符 / 同样式文本），译文常用不含逐字 box 的 `PdfSameStyleUnicodeCharacters`。
- `Box`（坐标盒）、`PdfStyle`（样式）、`PageLayout`（版面区域）、`PdfFont`（字体度量）是被大量实体复用的支撑类型；段落的 `layout_id`/`font_id` 通过逻辑外键关联到 `PageLayout`/`PdfFont`。
- 阅读要点：`@dataclass(slots=True)` 省内存；`metadata` 里 `Element`/`Attribute`/`required`/`name`/`tokens` 决定字段的 XML 形态；`.rnc` schema 是 `.py` 的简洁上游契约。
- IL 字段会随流水线阶段被逐步填满/改写（如 `optimal_scale`、`layout_id`），所以同一个 `Document` 对象在不同阶段看起来的「完整度」不同。

## 7. 下一步学习建议

- **横向（序列化）**：下一篇 [u3-l2 IL 的序列化：XMLConverter 与 schema](u3-l2-il-serialization.md) 讲解如何把这棵树写成 XML/JSON、如何对照 `.rnc`/`.xsd` 看 IL 结构，并阅读调试输出。
- **纵向（来源）**：想知道这棵 `Document` 树是怎么从 PDF 一字节一字节建出来的，进入第四单元 [u4-l1 解析入口与整体流程](u4-l1-parse-entry-and-flow.md)，看 `new_parser` 与 `ActiveILCreater` 如何填充 `Page` 的各个集合。
- **纵向（消费）**：想知道这棵树被谁读，可先看 [u5-l3 段落识别 ParagraphFinder](u5-l3-paragraph-finder.md)（填 `pdf_paragraph`）、[u6-l2 IL 翻译编排](u6-l2-il-translator-orchestration.md)（改 `unicode` 与 composition）、[u7-l1 排版重排 Typesetting](u7-l1-typesetting.md)（回填 `optimal_scale`）。
