# 项目定位与背景：BabelDOC 是什么

## 1. 本讲目标

本讲是整本学习手册的第一讲，不写一行复杂代码，只解决一个最基本的问题：**BabelDOC 到底是什么、它为什么存在**。读完本讲，你应当能够：

- 用一句话说清 BabelDOC 的定位（PDF 科研论文双语对照翻译库）。
- 区分 PDF 处理中的 **Parsing（解析）** 与 **Rendering（渲染）** 两个阶段，并理解 BabelDOC 为什么要在中间引入一个**中间表示（IL, Intermediate Language）**。
- 看懂 BabelDOC 在整个生态里的位置：它依赖谁（上游）、谁依赖它（下游）、和哪些同类项目在做类似的事。
- 读懂 BabelDOC 特殊的版本号规则（`0.MAJOR.MINOR`，语义版本 + 骄傲版本）。

本讲只建立「心智模型」，真正的代码走读从第二单元开始。

## 2. 前置知识

本讲几乎不需要编程基础，但有几个名词先解释清楚：

- **PDF**：一种「版面优先」的文档格式。它的内部更像是一张画布上的一堆绘图指令（在某坐标画一个字、画一条线、放一张图），而不是像 Word/HTML 那样的「段落 + 流式排版」。这意味着从 PDF 里「读出结构」其实很困难。
- **科研论文**：通常双栏排版，包含大量公式、图表、参考文献。这类文档对「保留版面」的要求很高——翻译后如果版面错乱，几乎没法读。
- **双语对照（bilingual comparison）**：把译文和原文放在同一个 PDF 里，方便对照阅读。BabelDOC 能生成「原文译文并排」或「原文译文交替翻页」的对照 PDF。
- **中间表示（IL）**：可以理解成 PDF 内容的「结构化翻译稿」。它既不是原始 PDF 的字节流，也不是最终成品，而是介于两者之间、可被程序读写、保留了版面结构的数据格式。本讲只需要这个直觉，IL 的具体数据模型在第三单元精讲。

如果你对 PDF 内部结构完全陌生，本讲的「代码实践」会引导你阅读项目自带的一份科普文档，不必现在就懂。

## 3. 本讲源码地图

本讲引用的关键文件都很「轻量」，多数是文档与配置：

| 文件 | 作用 | 本讲用来讲什么 |
| --- | --- | --- |
| `README.md` | 项目主文档，包含定位、用法、Background、Roadmap、版本号说明、生态致谢 | 项目定位、两阶段思想、生态关系、版本规则 |
| `pyproject.toml` | Python 项目元数据：名称、版本、描述、入口点、依赖列表 | 项目描述、入口命令、依赖生态 |
| `docs/README.md` | 文档总入口，定义「BabelDOC 文档中间语言」 | IL 的官方定义与定位 |
| `babeldoc/const.py` | 全局常量，含 `__version__` 与水印版本号 | 版本号在源码里的实际写法 |

> 说明：`babeldoc/const.py` 不在本讲规格列出的 `source_files` 里，但它是版本号在源码中的真实落点，为了让「版本号规则」这个模块不浮于文档，本讲会顺带引用它。所有文件路径都基于当前 HEAD `980fd28`。

## 4. 核心概念与源码讲解

### 4.1 项目定位与目标：BabelDOC 是什么

#### 4.1.1 概念说明

BabelDOC 的自我介绍只有一句话，但信息量很大：

> PDF scientific paper translation and bilingual comparison library.

翻译过来就是：**面向 PDF 科研论文的翻译与双语对照库**。三个关键词决定了它的定位：

1. **科研论文（scientific paper）**：不是随便翻译一份合同或小说，而是专门针对公式多、双栏、版面复杂的学术论文优化。
2. **双语对照（bilingual comparison）**：产出物是「原文 + 译文」并列的 PDF，而不是只给译文。
3. **库（library）**：它首要设计目标是**被嵌入到其他程序里**（见 README「Mainly designed to be embedded into other programs」），命令行只是附带能力。

`pyproject.toml` 里给它的简短描述则更俏皮：

> description = "Yet Another Document Translator"

「Yet Another」是开源社区常用的自嘲式命名，意思是「又一个文档翻译器」——但它「又」在哪里，正是本讲 4.2 要讲的核心：中间表示 IL。

#### 4.1.2 核心流程

从用户视角看，BabelDOC 的使用方式很直接：

1. **作为命令行工具**：安装后用 `babeldoc` 命令，传入一个 PDF 文件和一个 OpenAI 兼容翻译服务，输出译文 PDF。
2. **作为 Python 库**：通过 `high_level.do_translate_async_stream` 函数调用（README 明确推荐经由下游项目 [pdf2zh_next](https://github.com/PDFMathTranslate/PDFMathTranslate-next) 调用）。
3. **产出两种 PDF**：
   - **mono PDF**：只有译文的单语 PDF。
   - **dual PDF**：原文与译文对照的双语 PDF（可并排或交替翻页）。

> 注意：README 反复强调，BabelDOC 的所有 API 都应被视为**内部 API**，直接使用不被官方支持，推荐用法是让下游集成项目来调用它。这也印证了它「库优先」的定位。

#### 4.1.3 源码精读

项目第一行自我介绍：

- [README.md:32](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L32)：项目的一句话定位——「PDF scientific paper translation and bilingual comparison library」。
- [README.md:34-38](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L34-L38)：列出三种使用形态——在线服务（Immersive Translate）、自部署（PDFMathTranslate-next）、命令行、Python API，并点明「主要被设计为嵌入其他程序」。
- [README.md:324-329](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L324-L329)：Python API 章节，明确「All APIs of BabelDOC should be considered as internal APIs」。

项目元数据：

- [pyproject.toml:1-5](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L1-L5)：名称 `BabelDOC`、版本 `0.6.3`、描述 `"Yet Another Document Translator"`、许可证 `AGPL-3.0`。
- [pyproject.toml:67](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L67)：入口点 `babeldoc = "babeldoc.main:cli"`——安装后终端里的 `babeldoc` 命令，本质就是调用 `babeldoc.main` 模块里的 `cli()` 函数。这是后续讲义「目录结构与入口文件」的起点。

#### 4.1.4 代码实践

1. **实践目标**：在不安装任何东西的前提下，仅凭项目元数据与 README，确认 BabelDOC「库优先、命令行附带」的定位。
2. **操作步骤**：
   - 打开 [pyproject.toml:66-67](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L66-L67) 的 `[project.scripts]` 段，确认 `babeldoc` 命令指向 `babeldoc.main:cli`。
   - 打开 [README.md:324-329](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L324-L329)，阅读 Python API 的 WARNING。
3. **需要观察的现象**：README 明确不鼓励终端用户直接使用 CLI，而是引导到「在线服务 / PDFMathTranslate-next」。
4. **预期结果**：你能口头解释「为什么 BabelDOC 把 CLI 标注为 mainly for debugging，却仍然提供一个命令行入口」——因为它本质上是个库，CLI 只是方便调试和简单翻译。
5. 命令运行相关的具体输出：待本地验证（本讲不要求安装）。

#### 4.1.5 小练习与答案

**练习 1**：BabelDOC 的官方描述是 "Yet Another Document Translator"，但 README 第一行又强调它是 "library"。这两者矛盾吗？

> **参考答案**：不矛盾。"Yet Another Document Translator" 描述它「做的事情」（文档翻译），而 "library" 描述它「呈现给用户的形态」（一个可被嵌入的库，而非独立成品应用）。它「又」在中间表示 IL 这个设计上（见 4.2）。

**练习 2**：用 `babeldoc` 命令翻译一个 PDF，需要先确认入口函数是哪个？

> **参考答案**：`babeldoc.main:cli`（见 [pyproject.toml:67](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L67)）。也就是说 `babeldoc` 命令运行时，真正执行的是 `babeldoc/main.py` 文件里的 `cli()` 函数。

---

### 4.2 Parsing 与 Rendering 两阶段：为什么需要中间表示 IL

#### 4.2.1 概念说明

这是本讲最重要的一节。README 的 **Background** 章节把所有 PDF 翻译/解析工具抽象成两个阶段：

- **Parsing（解析）**：把 PDF「拆开」，读出它的结构——文本块、图片、表格、坐标等。
- **Rendering（渲染）**：把（翻译/修改后的）结构「画回」一个新的 PDF 或其他格式。

听起来简单，但难点在于：**从 Parsing 到 Rendering，中间用什么来承载结构？** 不同工具的选择不同，决定了它们的能力差异：

- **典型做法（单栏重排）**：像 mathpix 那样，把 PDF 解析成 XML，再用单栏阅读顺序（如微软的 layoutreader）重新排版。**代价是：原始版面结构丢失了**——双栏变单栏、图表位置全乱。
- **Adobe PDF Parser 做法**：转成 Word，保留结构，但**很贵**；而且 PDF/Word 在手机上阅读体验差。
- **BabelDOC 做法**：在 Parsing 和 Rendering 之间，引入一个**中间表示 IL（Intermediate Language）**。IL 保留了原始版面的空间结构（每个字符/段落都带坐标），这样既能做翻译、又能「原地」把译文渲染回原版面，从而生成双语对照 PDF 而不破坏排版。

一句话直觉：**IL 是一份「保留了坐标的结构化草稿」**，它让翻译这件事不再需要「推倒重排」，而是「贴着原版面改」。

#### 4.2.2 核心流程

把三种方案放在一起对比，差异立刻清晰：

```
方案 A（单栏重排，如 mathpix 风格）：
  PDF  ──Parsing──▶  XML（文本块 + 阅读顺序）
       ──Rendering（单栏重排）──▶  新 PDF
  问题：原始二维版面结构丢失（双栏/图表位置没了）

方案 B（BabelDOC，保结构）：
  PDF  ──Parsing──▶  IL（保留每个字符/段落的坐标 box、字体、版面）
       ──在 IL 上翻译 + 排版重排──▶
       ──Rendering──▶  mono / dual 双语 PDF（版面保留）
  优势：能产出原文译文并排/交替的对照 PDF
```

关键差异点在于 IL **保留了空间坐标信息**。这也是为什么 BabelDOC 能做「双语对照」——它知道每个译文段落应该贴回原版的哪个位置。

> 注意范围：本讲只讲「Parsing + Rendering」这个**外部两阶段视图**。BabelDOC 内部其实把流水线拆得更细（frontend/midend/backend 三段式、多个 midend 阶段），那是第二单元 `u2-l1` 的内容，本讲先不展开。

#### 4.2.3 源码精读

README 的 Background 章节是这段思想最权威的出处：

- [README.md:331-357](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L331-L357)：完整 Background，点出 mathpix、Doc2X、minerU、PDFMathTranslate 等同类项目，并明确「two main stages: Parsing / Rendering」，以及 mathpix 单栏重排导致「the original structure lost」。
- [README.md:347-357](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L347-L357)：核心论断——「We offer an intermediate representation of the results from parser and can be rendered into a new pdf or other format」，并提到流水线是插件化的（plugin-based system）。

IL 的官方定义在文档总入口：

- [docs/README.md:1-8](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/docs/README.md#L1-L8)：标题「BabelDOC Document Intermediate Language」，把 IL 定位为「parsing 与 rendering 阶段之间使用的中间语言」。
- [docs/README.md:6](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/docs/README.md#L6)：IL 的正式 schema 文件是 `babeldoc/format/pdf/document_il/il_version_1.rnc`（一种 Relax NG Compact 语法的 schema）。这正是第三单元要精读的 IL 数据模型的「宪法」。

#### 4.2.4 代码实践

1. **实践目标**：通过对比，真正理解「保结构」相比「单栏重排」赢在哪里。
2. **操作步骤**：
   - 打开 [README.md:347-357](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L347-L357)，找到 mathpix 段落里「the original structure lost」那句。
   - 打开 [docs/README.md:1-8](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/docs/README.md#L1-L8)，确认 IL 被定义在「解析与渲染之间」。
3. **需要观察的现象**：README 自己承认同类工具会「丢失原始结构」，而 BabelDOC 的卖点恰恰是用 IL 保留结构。
4. **预期结果**：你能向一个没读过代码的人解释——为什么 BabelDOC 能生成「版面不乱的对照 PDF」，而很多翻译工具只能给你「一栏纯文本译文」。核心答案：**因为 IL 保留了坐标**。
5. 若想进一步看 IL 的样子：阅读项目自带的科普文档 `docs/intro-to-pdf-object.md`（PDF 对象基础），为第三单元读懂 `il_version_1.rnc` 做铺垫；具体 IL 实体结构待第三单元验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 README 说 mathpix 这类「单栏阅读顺序重排」会「lose the original structure」？

> **参考答案**：因为它把 PDF 解析后只保留了「阅读顺序」，丢弃了字符/段落在页面上的**二维坐标**。重排时按单栏从头到尾铺开，原始的双栏、图表位置、页眉页脚的空间关系就没了。

**练习 2**：BabelDOC 的 IL 与「转成 Word」的方案相比，优势在哪？

> **参考答案**：转 Word 虽然也保留结构，但成本高（商业服务），而且 PDF/Word 在移动端阅读体验差；IL 是程序可读的中间格式，既能保结构翻译，又能渲染回适合阅读的 PDF（包括双语对照），更适合自动化流水线和插件扩展。

**练习 3**：IL 的 schema 文件叫什么？用什么语法？

> **参考答案**：`babeldoc/format/pdf/document_il/il_version_1.rnc`，使用 Relax NG Compact（`.rnc`）语法（见 [docs/README.md:6](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/docs/README.md#L6)）。

---

### 4.3 生态关系：上游、下游与同类项目

#### 4.3.1 概念说明

理解一个开源项目，看它在「依赖链」里的位置比看它自身代码更有用。BabelDOC 的生态可以分成三类：

1. **上游（BabelDOC 依赖的基础能力）**：BabelDOC 自己不发明 PDF 解析和版面检测，而是站在巨人肩膀上。
   - **PyMuPDF**：强大的 PDF 读写/渲染库，BabelDOC 用它做 PDF 打开、渲染、字体处理。
   - **pdfminer**：经典的 PDF 文本提取库，BabelDOC 甚至把它的代码**内置（vendor）**进了仓库（`babeldoc/pdfminer/`），用于内容流解释。
   - **DocLayout-YOLO**：版面检测模型，识别页面里的文本/标题/图/表/公式区域。
2. **下游（把 BabelDOC 当库用的应用）**：
   - **PDFMathTranslate-next（pdf2zh_next）**：官方推荐的自部署方案，提供 WebUI 和更多翻译服务，内部调用 BabelDOC。
   - **Immersive Translate（沉浸式翻译）**：提供 BabelDOC 的在线服务（Beta），每月有免费额度。
3. **同类（解决相近问题的项目）**：mathpix、Doc2X、minerU、PDFMathTranslate；以及解决局部问题的 layoutreader（阅读顺序）、Surya（结构识别）。

记住一个心智模型：**BabelDOC 是「中间层」**——上游喂给它解析/版面能力，下游把它包装成面向终端用户的产品。

#### 4.3.2 核心流程

从「谁来调用谁」的角度看依赖方向：

```
终端用户
   │
   ├──在线服务──▶ Immersive Translate（BabelDOC 在线 Beta）
   │
   └──自部署───▶ PDFMathTranslate-next (pdf2zh_next)   [下游应用]
                       │ 调用
                       ▼
                  BabelDOC（本库）   [中间层：解析→IL→渲染]
                       │ 依赖
                       ▼
        PyMuPDF / pdfminer / DocLayout-YOLO / ...   [上游基础能力]
```

这个方向很重要：**BabelDOC 调用上游，被下游调用**。这也是为什么 README 把 API 标为「内部 API」——稳定契约主要面向 pdf2zh_next 这一个下游。

#### 4.3.3 源码精读

README 的致谢章节是生态关系最直接的证据：

- [README.md:411-426](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L411-L426)：Acknowledgements 列出 PDFMathTranslate（下游）、DocLayout-YOLO（上游版面模型）、pdfminer（上游解析）、PyMuPDF（上游渲染）、Asynchronize（异步回调库）、PriorityThreadPoolExecutor（优先级线程池库）。
- [README.md:331-345](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L331-L345)：Background 列出同类项目 mathpix、Doc2X、minerU、PDFMathTranslate，以及 layoutreader、Surya 两个「解决局部问题」的工具。
- [README.md:34-35](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L34-L35)：明确下游——Immersive Translate 在线服务、PDFMathTranslate-next 自部署。

`pyproject.toml` 的依赖列表则从「代码层面」印证了上游关系：

- [pyproject.toml:19-55](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L19-L55)：能直接看到 `pymupdf`、`onnxruntime`（跑 DocLayout-YOLO 这类 ONNX 模型）、`openai`（翻译服务）、`tiktoken`（token 计费）、`hyperscan`（术语表高性能匹配）、`rtree`（空间索引）等。这些依赖后续讲义会逐个对应到具体功能模块。

#### 4.3.4 代码实践

1. **实践目标**：把抽象的「上下游」关系，落到具体的依赖包和项目链接上。
2. **操作步骤**：
   - 在 [pyproject.toml:19-55](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L19-L55) 找到 `pymupdf`、`onnxruntime`、`openai` 三个依赖。
   - 在 [README.md:411-426](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L411-L426) 找到对应的致谢项目 DocLayout-YOLO、PyMuPDF。
3. **需要观察的现象**：`pyproject.toml` 里的依赖（代码层）和 README 致谢里的项目（社区层）能一一对应上。
4. **预期结果**：你能在一张纸上画出来「PyMuPDF/pdfminer/DocLayout-YOLO 是上游、PDFMathTranslate-next 是下游、mathpix 是同类」的关系图。
5. 是否需要运行命令：不需要，纯阅读型实践。

#### 4.3.5 小练习与答案

**练习 1**：BabelDOC 为什么要把 pdfminer 的代码 vendor（内置）进仓库，而不是直接 `pip install pdfminer-six`？

> **参考答案**：BabelDOC 需要深度定制 PDF 内容流的解析行为（注意 `pyproject.toml` 里有一行被注释掉的 `pdfminer-six==20250416`）。把代码内置进来（`babeldoc/pdfminer/`）可以自由修改解析逻辑、固定版本、避免与上游发版节奏冲突。这是一个典型的「需要深度改造时选择 vendor 而非依赖」的工程决策。

**练习 2**：终端用户想要一个带 WebUI 的翻译服务，应该直接用 BabelDOC 吗？

> **参考答案**：不推荐。README 明确引导终端用户用在线服务（Immersive Translate）或自部署的 PDFMathTranslate-next（见 [README.md:34-35](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L34-L35)）。BabelDOC 是底层库，CLI 主要面向调试。

**练习 3**：DocLayout-YOLO 在 BabelDOC 里扮演什么角色？

> **参考答案**：上游的版面检测模型——识别页面中的文本/标题/图/表/公式区域。BabelDOC 通过 `onnxruntime` 加载它的 ONNX 模型（见 [pyproject.toml:19-55](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L19-L55) 里的 `onnx`/`onnxruntime` 依赖）。具体在第五单元 `u5-l2` 精讲。

---

### 4.4 版本号规则：语义版本 + 骄傲版本

#### 4.4.1 概念说明

BabelDOC 当前的版本是 `0.6.3`（见 `pyproject.toml` 与 `babeldoc/const.py`）。它的版本号规则有点特别，README 专门用一节解释：

> This project uses a combination of Semantic Versioning and Pride Versioning. The version number format is: "0.MAJOR.MINOR".

也就是说，版本号格式是 **`0.MAJOR.MINOR`**，三个数字的含义：

- **开头的 `0`**：固定前缀。表示项目还处于 1.0 之前的阶段（README 的 Roadmap 列出了「第一个 1.0 版本」的目标，尚未达成）。
- **MAJOR（第二段，当前是 `6`）**：当出现 **API 不兼容的改动**，或实现了值得骄傲的改进（proud improvements）时，加 1。
- **MINOR（第三段，当前是 `3`）**：当做出 **API 兼容的改动**时，加 1。

这里有两个要点：

1. **「API 兼容」特指对下游 pdf2zh_next 的兼容**，不是泛泛的语义版本。README 的 NOTE 写得很清楚：「The API compatibility here mainly refers to the compatibility with pdf2zh_next」。
2. **「Pride Versioning」（骄傲版本）**是一个有趣的变体（见 [pridever.org](https://pridever.org/)）——它允许「值得骄傲的改进」也触发 MAJOR 升级，而不像纯语义版本那样只有破坏性改动才升 MAJOR。所以 BabelDOC 的 MAJOR 跳得比传统语义版本快，这是有意为之。

#### 4.4.2 核心流程

把 `0.6.3` 拆开解读：

```
0  .  6   .  3
│     │      │
│     │      └─ MINOR：API 兼容改动时 +1（当前 3）
│     └──────── MAJOR：API 不兼容 或 骄傲改进时 +1（当前 6）
└────────────── 固定 0：表示尚未发布 1.0
```

版本号的「写在哪里」也值得注意——它同时出现在三个地方，靠工具 `bumpver` 保证一致：

- `pyproject.toml`（`version` 与 `[bumpver] current_version`）
- `babeldoc/__init__.py`（`__version__`）
- `babeldoc/main.py` 和 `babeldoc/const.py`（`__version__`）

这从 [pyproject.toml:165-182](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L165-L182) 的 `[bumpver.file_patterns]` 配置就能看出来——它声明了发版时需要同步替换版本号的文件清单。

#### 4.4.3 源码精读

- [README.md:380-391](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L380-L391)：Version Number Explanation，定义 `0.MAJOR.MINOR` 格式、MAJOR/MINOR 的含义，并说明 API 兼容主要针对 pdf2zh_next。
- [pyproject.toml:1-5](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L1-L5)：`version = "0.6.3"`，项目元数据里的版本。
- [pyproject.toml:165-182](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L165-L182)：`[bumpver]` 配置，`current_version = "0.6.3"`，以及 `file_patterns` 声明发版时要同步版本号的文件（pyproject.toml、`__init__.py`、`main.py`、`const.py`）。
- [babeldoc/const.py:9](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/const.py#L9)：源码里的 `__version__ = "0.6.3"`。
- [babeldoc/const.py:38-40](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/const.py#L38-L40)：`WATERMARK_VERSION` 的兜底逻辑——优先用 `git describe --always` 取一个 git 描述串作为水印版本，取不到时退回 `v{__version__}`。这解释了为什么生成的 PDF 水印上可能显示一串 git 描述而非纯版本号。

#### 4.4.4 代码实践

1. **实践目标**：验证版本号在多个文件中确实一致，并理解 `0.6.3` 的含义。
2. **操作步骤**：
   - 在仓库里确认这些位置都写着 `0.6.3`：[pyproject.toml:3](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L3)、[pyproject.toml:166](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L166)、[babeldoc/const.py:9](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/const.py#L9)。
   - 阅读 [README.md:380-391](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L380-L391)。
3. **需要观察的现象**：版本号不是只写在一个地方，而是分散在多个文件、靠 `bumpver` 工具同步。
4. **预期结果**：你能回答——`0.6.3` 里，`0` 是固定前缀（未到 1.0），`6` 是 MAJOR，`3` 是 MINOR；而且这里的「兼容」特指对 pdf2zh_next 的兼容。
5. 若想看水印版本的实际值：在你本机 git 仓库目录下运行 `git describe --always`，对照 [babeldoc/const.py:38-40](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/const.py#L38-L40) 的逻辑预测 PDF 水印会显示什么；具体显示值待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：当前版本 `0.6.3` 中，`6` 和 `3` 分别代表什么？「兼容」是针对谁的兼容？

> **参考答案**：`6` 是 MAJOR（API 不兼容改动或骄傲改进时 +1），`3` 是 MINOR（API 兼容改动时 +1）。「兼容」特指对下游 pdf2zh_next 的 API 兼容（见 [README.md:384-391](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L384-L391)）。

**练习 2**：为什么 BabelDOC 的 MAJOR 版本跳得比一般语义版本项目快？

> **参考答案**：因为它结合了 Pride Versioning——「值得骄傲的改进」也会触发 MAJOR +1，而不像纯语义版本那样只有破坏性改动才升 MAJOR（见 [README.md:389](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L389)）。

**练习 3**：发版时，为什么 `pyproject.toml` 里要写一大段 `[bumpver.file_patterns]`？

> **参考答案**：因为版本号同时写在 `pyproject.toml`、`babeldoc/__init__.py`、`babeldoc/main.py`、`babeldoc/const.py` 多个文件里。`bumpver` 根据 `file_patterns` 在发版时自动把这些文件里的旧版本号替换成新版本号，避免人工漏改导致版本不一致（见 [pyproject.toml:169-182](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L169-L182)）。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个任务（这是本讲规格指定的核心实践）：

> **任务**：阅读 [README.md](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md)（重点是 [Background:331-357](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L331-L357) 与 [Version Number Explanation:380-391](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L380-L391)），**用自己的话写一段不超过 100 字的总结**，要求同时覆盖两点：
>
> 1. BabelDOC 解决什么问题；
> 2. 它的中间表示（IL）思想，为什么优于「单栏重排」方案。

**参考写法（示例，你可写成自己的版本）**：

> BabelDOC 是面向 PDF 科研论文的双语对照翻译库，把 PDF「解析 → 中间表示 IL → 渲染」做成可嵌入的流水线。IL 保留了字符与段落的坐标，所以能「贴着原版面」生成双语 PDF，而不像单栏重排那样丢失原始版面结构。

**自检清单**（你的总结若满足以下全部，就算过关）：

- [ ] 提到了「PDF 翻译 / 双语对照」这一核心定位。
- [ ] 提到了「中间表示 IL / 保结构」。
- [ ] 解释了「单栏重排会丢失版面结构」这一对比点。
- [ ] 字数 ≤ 100 字。

> 如果你还能顺手注明版本 `0.6.3` 中 `0/6/3` 的含义，说明 4.4 也掌握了。

## 6. 本讲小结

- BabelDOC 是**面向 PDF 科研论文的双语对照翻译库**，定位是「被嵌入的库」，命令行只是附带能力（[README.md:32](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L32)、[README.md:38](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L38)）。
- 所有 PDF 翻译/解析工具本质上有两阶段：**Parsing（解析）** 与 **Rendering（渲染）**（[README.md:347-352](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L347-L352)）。
- BabelDOC 的核心创新是在两阶段之间引入**中间表示 IL**——它保留了坐标，所以能「保结构」地生成双语对照 PDF，优于会丢失版面的「单栏重排」方案（[README.md:357](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L357)、[docs/README.md:6](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/docs/README.md#L6)）。
- 生态上 BabelDOC 是「中间层」：上游用 PyMuPDF / pdfminer / DocLayout-YOLO，下游被 PDFMathTranslate-next、Immersive Translate 包装（[README.md:411-426](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L411-L426)）。
- 版本号格式是 `0.MAJOR.MINOR`，结合语义版本与骄傲版本，「兼容」特指对 pdf2zh_next；当前为 `0.6.3`（[README.md:380-391](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L380-L391)）。
- 入口命令 `babeldoc` 指向 `babeldoc.main:cli`，这是下一讲「安装与运行」与后续「目录结构」的起点（[pyproject.toml:67](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L67)）。

## 7. 下一步学习建议

本讲只建立了「BabelDOC 是什么」的心智模型，还没有真正跑起来。建议按以下顺序继续：

1. **`u1-l2` 安装与运行：从 CLI 翻译第一个 PDF**——亲手用 `uv` 安装 BabelDOC，跑通 `babeldoc --help` 并翻译一个示例 PDF，把本讲的「定位」变成可操作的体验。
2. **`u1-l3` 目录结构与入口文件**——从 `babeldoc.main:cli` 出发，看清整个 `babeldoc` 包的目录划分，为后面读源码建立导航。
3. **`u3-l1` IL 数据模型**——本讲只讲了 IL「保留了坐标」的直觉，第三单元会带你精读 `il_version_1.py` / `il_version_1.rnc`，看清 Document → Page → Paragraph → Character 的真实结构。

如果你急于了解全局，可以先跳到 **`u2-l1` 三段式架构总览**，但建议先完成 `u1-l2` 的动手安装，这样后面的源码讲解会更有体感。
