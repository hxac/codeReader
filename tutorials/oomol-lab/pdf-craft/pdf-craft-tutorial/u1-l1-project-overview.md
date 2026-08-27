# pdf-craft 是什么：项目定位与能力地图

> 本讲是整本学习手册的第一讲。我们不急着读复杂的实现代码，而是先回答三个问题：**这个项目是做什么的？它支持哪几条工作流？官方文档放在哪里？** 把这三件事弄清楚，后面每一讲的源码阅读都有据可依。

## 1. 本讲目标

学完本讲，你应该能够：

1. 用自己的话说出 pdf-craft 的核心能力：**PDF 转 Markdown / EPUB、内容翻译、把译文回写 PDF**。
2. 理解项目的定位：面向**扫描版书籍与学术/技术文档**的转换，而不是任意 PDF 的通用工具。
3. 列出 pdf-craft 支持的五种工作流，并说明每种工作流需要哪些外部依赖（OCR 服务、LLM、Poppler）。
4. 熟悉 README 与 `docs/en` 下各官方指南的分工，遇到问题时知道去哪份文档查。

## 2. 前置知识

本讲几乎不需要编程基础，但下面几个名词会反复出现，先混个脸熟：

- **PDF**：常见的电子文档格式。扫描版 PDF 的每一页其实是一张**图片**，计算机无法直接“选中”其中的文字。
- **OCR（Optical Character Recognition，光学字符识别）**：把图片里的文字“认”出来，变成真正的文本。pdf-craft 的提取环节就靠它。
- **Markdown / EPUB**：两种输出格式。Markdown 是轻量纯文本标记格式；EPUB 是电子书的标准打包格式（本质是一个 ZIP 包，内含 XHTML 与资源文件）。
- **LLM（大语言模型）**：用于“翻译”环节的文本模型。pdf-craft 通过 OpenAI 兼容接口调用它，所以任何兼容该协议的服务都可以。
- **Poppler**：一套开源的 PDF 渲染工具。Python 的 `pdf2image` 库依赖它把 PDF 页面渲染成图片。它是**系统级依赖**，`pip` 装不了，需要单独安装（后文会讲到）。
- **门面模式（Facade）**：一种设计模式——用一个简单的对外类（这里是 `PDFCraft`）把内部一堆组件的调用顺序包起来，用户只面对少量方法。第 4 单元会精读它，本讲只需要知道“入口是 `PDFCraft`”即可。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md) | 英文主文档：项目定位、安装、Quick Start、五种工作流示例、OCR 后端总览 |
| [README_zh-CN.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README_zh-CN.md) | README 的中文版，结构与英文版一一对应 |
| [docs/en/API_REFERENCE.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/API_REFERENCE.md) | 公开 API 参考：`PDFCraft` 全部方法、配置对象、数据类型的权威说明 |
| [docs/en/](https://github.com/oomol-lab/pdf-craft/tree/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en) | 专题指南目录：安装、OCR 后端、PDF 翻译、EPUB 翻译、故障排查共五份 |
| [pdf_craft/\_\_init\_\_.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/__init__.py) | 库的公开出口：用户 `from pdf_craft import ...` 能拿到什么，全由它决定 |
| [pdf_craft/craft.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py) | `PDFCraft` 门面类的实现，五种工作流对应的入口方法都在这里 |
| [pyproject.toml](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml) | 包定义：Python 版本要求、依赖清单、`local` 扩展，是"依赖从哪来"的事实依据 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**项目定位**、**核心工作流**、**文档地图**。

### 4.1 项目定位

#### 4.1.1 概念说明

一句话概括：**pdf-craft 是一个以 PDF 为中心的 Python 转换库**。它做三件事：

1. **转换**：把 PDF 转成 Markdown 或 EPUB；
2. **翻译**：把转换出的内容（或已有的 EPUB）翻译成目标语言；
3. **回写**：把译文按原版式写回 PDF，生成"看起来还是原书、但文字已翻译"的 PDF。

它特别适合**扫描版文档**：原本只能当图片看的页面，经过 OCR 之后变成可搜索、可编辑的 Markdown 或 EPUB。这一点是理解整个项目的钥匙——后面大量源码（OCR 循环、目录分析、脚注处理）都是为"扫描书"这个场景服务的。

项目明确声明管线是为**书籍和学术/技术文档**设计的，覆盖正文、目录、脚注、表格、公式和图片。它不是通用 PDF 工具箱，处理发票、表单这类单页文档并不划算。

#### 4.1.2 核心流程

pdf-craft 的整体数据流可以概括为一条主线：

```text
PDF 文件
   │  (Poppler 渲染成页面图片)
   ▼
OCR 识别  ──►  目录分析(TOC)  ──►  章节生成
   │                                   │
   ▼                                   ▼
中间产物 DocumentPackage（chapters/ + assets/ + toc.xml + document.json）
   │                                   │
   ▼ (渲染器)                          ▼ (转换器 Transformer，如翻译)
Markdown / EPUB 成品          翻译后的新包 ──► 回写 PDF
```

两个关键设计在本讲先建立直觉：

- **中间产物 `DocumentPackage`**：提取的结果先落成一个磁盘目录（包含章节 XML、资源图片、目录、元数据），渲染和翻译都基于这个包进行。这是提取器与渲染器之间的"契约"。
- **转换器（Transformer）**：翻译不是硬编码在流程里的，而是以"步骤"（`TranslationStep`）的形式插进"提取之后、渲染之前"的位置，所以同一套翻译步骤既能用于 Markdown 输出也能用于 EPUB 输出。

#### 4.1.3 源码精读

先看 README 里对项目定位的原始表述：

> pdf-craft is a PDF-centered conversion library. It turns PDFs into Markdown or EPUB, and can translate the converted content or write a translated result back to PDF. It is especially useful for scanned documents... The pipeline is designed for books and academic or technical documents, including body text, tables of contents, footnotes, tables, formulas, and images.

——引自 [README.md:15-25](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L15-L25)，这段话界定了"做什么"（转换+翻译+回写）与"为谁做"（扫描书籍与学术/技术文档）。中文对应表述在 [README_zh-CN.md:15-27](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README_zh-CN.md#L15-L27)。

包的"身份信息"写死在构建配置里：

- [pyproject.toml:5-13](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L5-L13) —— 包名 `pdf-craft`、描述里写明 "focus on processing PDF files of scanned books"（专注于扫描书籍 PDF），关键词 `pdf / epub / markdown`。
- [pyproject.toml:25](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L25) —— `requires-python = ">=3.11,<4"` 之外明确限定到 Python 3.11～3.13。

从依赖清单能反推出技术栈，[pyproject.toml:26-42](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L26-L42)：

| 依赖 | 在项目里的角色 |
| --- | --- |
| `pdf2image` | PDF 页面渲染成图片（背后需要系统装 Poppler） |
| `pypdf` / `reportlab` | 读取原 PDF、把译文叠层回写成 PDF |
| `doc-page-extractor` | OCR 页面提取的底层引擎（DeepSeek/Unlimited 后端） |
| `openai` / `tiktoken` | 调用 LLM 翻译、统计 token |
| `epub-generator` | 生成 EPUB |
| `pyahocorasick` | 目录页定位用的多模式串匹配（第 4 单元会见） |

而 [pyproject.toml:48-51](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L48-L51) 定义了可选的 `local` 扩展：`pip install "pdf-craft[local]"` 才会把本地 GPU 推理所需的依赖装上——这就是 README 里"标准安装 vs local 安装"差别的来源。

最后看出入口。用户能 import 到的所有名字都汇聚在 [pdf_craft/\_\_init\_\_.py:1-51](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/__init__.py#L1-L51)：`PDFCraft`、六种 OCR 配置、`LLM`、`DocumentPackage`、`XMLTranslator` 等。这份文件就是"公开 API 的边界"——不在里面出现的模块都属于内部实现。

#### 4.1.4 代码实践

**实践目标**：验证"标准安装"与"local 扩展"的差别，并确认自己的 Python 版本可用。

1. 打开 [README.md:35-57](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L35-L57)（Installation 小节），注意两行安装命令：`pip install pdf-craft` 与 `pip install "pdf-craft[local]"`。
2. 对照 [pyproject.toml:48-51](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L48-L51)，确认 `local` 扩展只是给 `doc-page-extractor` 加上了 `[local]` 修饰。
3. 在终端执行 `python --version`，对照 [pyproject.toml:25](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L25) 检查版本是否在 3.11～3.13 区间。

**需要观察的现象**：`local` 扩展引入的是 CUDA/PyTorch 一整套本地推理栈（体积大、要求 NVIDIA GPU）；标准安装完全没有这些。**预期结果**：你能说清楚"没有 GPU 的机器应该选标准安装 + vendor OCR"。本实践只涉及读文档和查版本命令，安装与否由下一讲的完整安装实践覆盖。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 pdf-craft"对扫描版文档特别有用"？

**参考答案**：扫描版 PDF 的页面是图片，文字无法被选中和搜索。pdf-craft 用 OCR 把页面图片识别成结构化文本（正文、目录、脚注、表格、公式），再渲染成 Markdown 或 EPUB，于是原本"只能看"的扫描书变成了可搜索、可编辑的文本文档。

**练习 2**：`pip install pdf-craft` 和 `pip install "pdf-craft[local]"` 的本质区别是什么？

**参考答案**：前者只装基础依赖，OCR 走远程 vendor 服务（无需 CUDA）；后者额外安装 `doc-page-extractor[local]`（见 `pyproject.toml` 的 optional-dependencies），使 OCR 模型能在本地 NVIDIA GPU 上运行，需要 CUDA、显存和模型缓存。不确定要不要本地跑就选标准安装。

**练习 3**：`from pdf_craft import XXX` 里的 `XXX` 由哪个文件决定？

**参考答案**：由 `pdf_craft/__init__.py` 决定。它把公开类型（`PDFCraft`、`PDFOptions`、各 OCR 配置、`LLM`、`DocumentPackage` 等）显式 re-export 出来；不在其中出现的符号是内部实现，不应依赖。

### 4.2 核心工作流

#### 4.2.1 概念说明

README 的 Quick Start 与 Advanced Features 小节实际上演示了**五种工作流**。它们全部通过 `PDFCraft` 门面类的几个方法驱动：

| # | 工作流 | 入口方法 | 需要什么 |
| --- | --- | --- | --- |
| 1 | PDF → Markdown | `convert_pdf_to_markdown` | OCR 配置 + Poppler |
| 2 | PDF → EPUB | `convert_pdf_to_epub` | OCR 配置 + Poppler |
| 3 | PDF → 翻译后的 Markdown/EPUB | `convert_pdf_to_markdown` / `convert_pdf_to_epub` + `steps=[TranslationStep(...)]` | OCR 配置 + Poppler + 翻译用的 LLM |
| 4 | EPUB → 翻译 EPUB | `translate_epub` | 翻译用的 LLM（不需要 OCR、不需要 Poppler） |
| 5 | PDF → 翻译 PDF（回写） | `extract_pdf` + `translate_pdf` | OCR 配置 + Poppler + 翻译器（LLM 或普通函数） |

注意两个容易忽略的要点：

- **OCR 和 LLM 是两套独立配置**。OCR 负责"认字"（视觉模型），LLM 负责"翻译"（文本模型），凭据互不通用。README 在 PDF 翻译小节明确写了 "OCR and translation use separate configurations"。
- **Poppler 是所有以 PDF 为输入的工作流的前置条件**，因为要先把页面渲染成图片交给 OCR；而工作流 4（EPUB 翻译）输入本来就是 EPUB，不需要 OCR 也不需要 Poppler。

#### 4.2.2 核心流程

五种工作流共享同一条主干的 不同裁剪：

```text
工作流 1/2:   PDF → [提取] → [渲染(Markdown/EPUB)]
工作流 3:     PDF → [提取] → [转换步骤(翻译)] → [渲染]
工作流 5:     PDF → [提取] → [转换步骤(翻译)] → [回写 PDF]
工作流 4:     EPUB → [解析] → [转换步骤(翻译)] → [重新打包 EPUB]
```

用伪代码描述门面内部做的事：

```text
convert_pdf_to_markdown(source, output, steps):
    workspace = 临时目录（若未给 package_path）
    package   = 提取(source, workspace)        # OCR + 目录分析 + 章节生成
    for step in steps:
        package = step.transformer.transform(package)   # 例如翻译
    渲染成 Markdown(output)
    清理 workspace
```

翻译之所以能"即插即用"，是因为它被建模为一个 `TranslationStep`（转换器 + 提交模式的组合），在提取与渲染之间被顺序应用——这个结构在下一模块的源码里能直接看到。

#### 4.2.3 源码精读

**工作流 1：PDF → Markdown（Quick Start）**。[README.md:64-75](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L64-L75) 给出最小示例：构造 `PDFOptions(ocr=DeepSeekOCRVendorConfig(...))`，调用 `craft.convert_pdf_to_markdown("input.pdf", "output.md")`。紧随其后的 [README.md:77-79](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L77-L79) 说明了一个重要行为：默认使用**临时工作目录**并在结束或失败时自动删除；只有传入 `package_path` 才保留中间产物。

**工作流 2：PDF → EPUB**。[README.md:91-104](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L91-L104) 把输出换成 `output.epub`，并通过 `book_meta=BookMeta(title=..., authors=[...])` 设置书籍元数据；[README.md:106](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L106) 补充：不传 `book_meta` 时会尝试从源 PDF 的元数据里读取。

**工作流 3：转换的同时翻译**。[README.md:114-120](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L114-L120) 展示同一个 `TranslationStep` 既喂给 `convert_pdf_to_markdown` 也喂给 `convert_pdf_to_epub`——证实翻译步骤与输出格式解耦。

**工作流 5：PDF → 翻译 PDF**。[README.md:128-143](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L128-L143) 分两步：先 `extract_pdf("input.pdf", "work/cache")` 得到持久化的包，再 `translate_pdf("input.pdf", package, "translated.pdf", translator)` 把译文写回原 PDF。示例中的 `translator` 是个普通的 `str -> str` 函数（占位），实际使用时替换成你的 LLM 调用。

**工作流 4：EPUB → 翻译 EPUB**。[README.md:151-169](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L151-L169)：只需 `LLM` 配置和目标语言，`PDFCraft().translate_epub(...)` 即可；`SubmitKind.REPLACE` 生成纯译本，`APPEND_BLOCK` / `APPEND_TEXT` 保留原文并把译文附加在后。注意这里 `PDFCraft()` 构造时**不带任何 PDF 配置**。

这些方法在源码中的落点（本讲只认门脸，细节留给后续单元）：

- [pdf_craft/craft.py:69-78](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L69-L78) —— `PDFCraft` 类定义与构造函数，docstring 明确写着"构造门面不会初始化 OCR，纯 EPUB 用户可以无凭据使用 `PDFCraft()`"——这解释了工作流 4 为什么能零 OCR 配置运行。
- [pdf_craft/craft.py:179-210](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L179-L210) —— `convert_pdf_to_markdown` 与 `convert_pdf_to_epub` 两个一步式方法，中间都调用了 `self._apply_steps(package, steps)`，正是工作流 3 的插入点。
- [pdf_craft/craft.py:84-110](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L84-L110) —— `extract_pdf` / `extract_pdf_with_metering`，工作流 5 的第一步。
- [pdf_craft/craft.py:30-35](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L30-L35) —— `TranslationStep` 数据类：一个转换器字段加一个 `SubmitKind` 模式字段，共两行，却撑起了全部"边转换边翻译"的工作流。

权威的方法签名汇总在 [docs/en/API_REFERENCE.md:19-39](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/API_REFERENCE.md#L19-L39)：提取/渲染类方法与翻译/回写类方法各一张表，包括 `convert_pdf_to_*` 会清理临时工作区、`patch_pdf_with_package` 不做任何 OCR/LLM 调用等细节。

#### 4.2.4 代码实践

**实践目标**：不看第三方教程，仅凭 README 与 `craft.py` 亲手画出"五种工作流 → 入口方法 → 必需依赖"的对照表。

1. 通读 [README.md:59-169](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L59-L169)（Quick Start + Advanced Features 全部小节）。
2. 打开 [pdf_craft/craft.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py)，用编辑器的"转到定义"或搜索功能找到 README 五段示例各自调用的方法，记下它们的行号。
3. 画一张三列表格（工作流 / 方法 / 依赖），其中"依赖"一列逐项标注：OCR 配置？翻译 LLM？Poppler？
4. 对每一格依赖，在 README 或 `pyproject.toml` 里找到证据行（例如 Poppler 的证据在 [README.md:54-56](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L54-L56)，OCR/LLM 分离的证据在 [README.md:124-127](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L124-L127)）。

**需要观察的现象**：五种工作流里有四种以 PDF 为输入（都要 Poppler + OCR），只有 EPUB 翻译例外；四种涉及翻译的工作流里，只有工作流 5 的 translator 允许是普通函数，其余都走 `LLM` 配置。**预期结果**：得到一张每一格都有源码行号背书的对照表（本讲的综合实践会用到它）。本实践为纯阅读型，无需运行代码。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `translate_epub` 可以用 `PDFCraft()` 空参数构造，而 `convert_pdf_to_markdown` 必须传 `PDFOptions(ocr=...)`？

**参考答案**：`PDFCraft` 构造函数不会初始化 OCR（见 `craft.py` 中类 docstring），OCR 引擎是第一次真正处理 PDF 时才按需加载的。EPUB 翻译的输入已经是文本，不需要 OCR，所以不需要任何 PDF 基础设施或凭据。

**练习 2**：`convert_pdf_to_markdown` 结束后，中间产物去哪了？怎么保留它？

**参考答案**：默认走临时工作目录，转换结束或失败时自动删除（README Quick Start 末尾说明）。想保留中间产物（调试或复用）时传入 `package_path` 参数，提取结果会持久化在该目录。

**练习 3**：`SubmitKind.REPLACE` 与 `APPEND_TEXT` 的区别是什么？

**参考答案**：`REPLACE` 直接用译文替换原文，生成纯目标语言版本；`APPEND_TEXT` 保留原文、把译文紧跟在原文之后放进同一段文字流（另有 `APPEND_BLOCK` 把译文作为独立区块附在后面）。注意 PDF 回写不支持 `APPEND_BLOCK`。

### 4.3 文档地图

#### 4.3.1 概念说明

pdf-craft 的官方文档分两层：**README 负责"是什么 + 快速上手"**，**docs/ 目录下的专题指南负责"怎么做好一件具体的事"**。`docs/en` 与 `docs/zh-CN` 是中英文对照的两套，`docs/changelog` 是版本历史。这一层的价值在于：当你遇到具体问题（装不上 Poppler、OCR 报错、翻译中断续传）时，能立刻定位到唯一一份该看的文档，而不是在 README 里翻找。

#### 4.3.2 核心流程

按"问题 → 文档"组织：

```text
我想知道项目能干什么 .................. README（What is pdf-craft? 小节）
我要装环境 / Poppler 装不上 ........... docs/en/INSTALLATION.md
我要选 OCR 后端 / 配置模型缓存 ........ docs/en/OCR_BACKENDS.md
我要把 PDF 转成 Markdown/EPUB 或翻译 PDF  docs/en/PDF_TRANSLATION.md
我要翻译 EPUB（提示词/并发/缓存/回调）.... docs/en/EPUB_TRANSLATION.md
某个方法/参数的确切签名是什么 .......... docs/en/API_REFERENCE.md
运行报错了 ........................... docs/en/TROUBLESHOOTING.md
这个行为是哪个版本改的 ................ docs/changelog/
```

#### 4.3.3 源码精读

`docs/en` 下共五份指南加一份 API 参考（可在本地 `ls docs/en` 确认）：

| 文档 | 覆盖内容 | 关键位置 |
| --- | --- | --- |
| [INSTALLATION.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/INSTALLATION.md) | 支持的 Python 版本、Poppler 各平台安装、local 扩展要求 | README 安装小节的展开，见 [README.md:55-57](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L55-L57) 的指引 |
| [OCR_BACKENDS.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/OCR_BACKENDS.md) | 六种 OCR 配置的来源、运行位置、选型建议 | README 的 [OCR Backends 小节](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L171-L204) 是它的摘要版 |
| [PDF_TRANSLATION.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/PDF_TRANSLATION.md) | PDF 转换与翻译的完整工作流、定制项 | README Quick Start 指向它：[README.md:81-82](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L81-L82) |
| [EPUB_TRANSLATION.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/EPUB_TRANSLATION.md) | EPUB 翻译的提示词、重试、并发、缓存、进度与失败处理 | README 在 [README.md:168-169](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L168-L169) 指向它 |
| [TROUBLESHOOTING.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/TROUBLESHOOTING.md) | 常见故障排查 | README 在 [README.md:56-57](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L56-L57) 指向它 |
| [API_REFERENCE.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/API_REFERENCE.md) | 全部公开 API 的权威签名 | 开篇即给出入口约定：[API_REFERENCE.md:1-15](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/API_REFERENCE.md#L1-L15) |

一个值得注意的阅读顺序建议写在 API 参考的结尾：[API_REFERENCE.md:162](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/API_REFERENCE.md#L162) 明确说"完整工作流请从 PDF_TRANSLATION 或 EPUB_TRANSLATION 开始读，而不是自己组合内部模块"——这也是本手册的学习顺序：先掌握门面方法，再逐层下沉到内部模块。

此外 README 还提供了在线体验入口 [README.md:27-33](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L27-L33)（Inkora - PDF Craft），不装任何东西就能在浏览器里看主工作流跑起来，适合建立直观印象。

#### 4.3.4 代码实践

**实践目标**：建立"问题 → 文档"的检索反射。

1. 在本地列出文档目录：`ls docs/en docs/zh-CN docs/changelog`，确认中英对照结构。
2. 打开 [docs/en/INSTALLATION.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/INSTALLATION.md)，找到你自己操作系统（Linux/macOS/Windows）对应的 Poppler 安装说明并记录命令。
3. 打开 [docs/en/TROUBLESHOOTING.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/TROUBLESHOOTING.md)，浏览小节标题，记下至少两个你认为将来最可能用到的条目。
4. 在 [docs/en/API_REFERENCE.md:55-71](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/API_REFERENCE.md#L55-L71) 找到 `ExtractionOptions` 的字段表，数一数有多少个字段（不需要记含义，后续单元会逐个讲）。

**需要观察的现象**：README 里的每个"高级"话题末尾几乎都指向 docs/ 下的一份专题指南；API_REFERENCE 的类型说明与 `pdf_craft/__init__.py` 的导出一一对应。**预期结果**：形成一张自己手写的"遇到 X 问题看 Y 文档"速查卡。本实践为文档检索型，无需运行转换。

#### 4.3.5 小练习与答案

**练习 1**：想了解"OCR 六种后端各自需要什么凭据"，应该读 README 还是专题指南？

**参考答案**：README 的 OCR Backends 小节（README.md 171-204 行）给了决策表，足以选型；确定选型后要具体配置参数时，读 `docs/en/OCR_BACKENDS.md`，那里有每种配置的完整示例。

**练习 2**：为什么 API 参考结尾建议"不要自己组合内部模块"来完成工作流？

**参考答案**：因为 `PDFCraft` 门面已经把提取、步骤应用、渲染、临时目录清理等顺序与善后逻辑封装好；直接组合 `PDFExtractor` / 渲染器等内部组件需要自己管理工作区与生命周期，容易出错。内部组件是留给需要显式控制的进阶应用的（本手册后续单元会带你读它们）。

**练习 3**：`docs/en` 与 `docs/zh-CN` 是什么关系？

**参考答案**：同一批指南的中英文两个版本，内容对应；`docs/changelog` 则记录版本变更历史。中文读者可以用 `README_zh-CN.md` + `docs/zh-CN` 平行阅读。

## 5. 综合实践

把三个模块串起来的任务（本讲对应的正式代码实践任务）：

**任务：编写《五种工作流与依赖速查卡》。**

1. **阅读**：完整读一遍 [README.md:59-169](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L59-L169)（Quick Start 与 Advanced Features 两个小节），可对照 [README_zh-CN.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README_zh-CN.md) 的对应小节。
2. **列表**：列出五种工作流——PDF→Markdown、PDF→EPUB、PDF→翻译产物（Markdown/EPUB）、EPUB→翻译 EPUB、PDF→翻译 PDF——为每种标注：入口方法名、示例代码所在 README 行号、必需的外部依赖（vendor OCR 服务 / 本地 GPU / 翻译 LLM / Poppler）。
3. **写作**：写一段约 200 字的中文总结，说明每种工作流分别需要哪些外部依赖，以及为什么 EPUB→EPUB 翻译是其中依赖最少的。
4. **核对**：用 [docs/en/API_REFERENCE.md:19-39](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/API_REFERENCE.md#L19-L39) 的方法表核对你在第 2 步写下的每个方法名与签名，确保没有拼错、没有把 `translate_package`（包到包翻译）与 `translate_pdf`（回写 PDF）混淆。

**预期产出**：一张速查卡 + 一段总结。它是纯文档实践，不要求运行转换；若想立刻动手跑通第一个转换，那就是下一讲（u1-l2）的安装实践任务。

## 6. 本讲小结

- pdf-craft 是**以 PDF 为中心的转换库**：PDF → Markdown/EPUB，内容翻译，译文回写 PDF；专为**扫描书籍与学术/技术文档**设计，覆盖目录、脚注、表格、公式、图片。
- 五种工作流全部由 `PDFCraft` 门面驱动：`convert_pdf_to_markdown` / `convert_pdf_to_epub`（可挂 `TranslationStep`）、`translate_epub`、`extract_pdf` + `translate_pdf`。
- **OCR 与 LLM 是两套独立配置**：OCR 负责"认字"（六种后端，本地/远程各三），LLM 负责翻译；Poppler 是所有 PDF 输入工作流的系统级前置依赖。
- 默认使用**临时工作目录**并自动清理，传 `package_path` 才保留中间产物 `DocumentPackage`——这个包是提取与渲染之间的契约。
- 文档分两层：README 管"是什么 + 快速上手"，`docs/en`（及 `docs/zh-CN`）五份专题指南管具体任务，API 参考是公开签名的权威出处。
- 公开 API 的边界由 `pdf_craft/__init__.py` 划定；`PDFCraft` 构造不初始化 OCR，所以纯 EPUB 用户可以零凭据使用。

## 7. 下一步学习建议

下一讲 **u1-l2《环境安装与第一次转换》**：按照 `docs/en/INSTALLATION.md` 真正装好 pdf-craft 与 Poppler，配置 OCR 凭据，并跑通本讲只"纸上谈兵"的 Quick Start 脚本——把一个 PDF 真正变成 Markdown。

在进入下一讲之前，建议先自己浏览一遍 [pdf_craft/craft.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py)，只看方法名和 docstring，不强求看懂实现——它会成为第 4 单元（u1-l4 门面精读）的预习材料。
