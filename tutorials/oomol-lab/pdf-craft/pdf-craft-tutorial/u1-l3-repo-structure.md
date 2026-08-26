# u1-l3 仓库结构与模块地图

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `pdf_craft/` 包内 `pdf`、`extractor`、`document`、`transformer`、`renderer`、`markdown`、`pipeline`、`llm`、`common` 等子模块各自的职责边界。
2. 区分两个顶层目录的不同角色：`pdf_craft/` 是**发布到 PyPI 的核心库**，`pdf_craft_tool/` 是**不随包发布的仓库本地 CLI 工具**。
3. 沿着「PDF 输入 → 提取 → DocumentPackage → 渲染/翻译 → Markdown/EPUB/PDF 输出」这条数据流，在源码中快速定位某个功能对应的模块。
4. 知道 `tests/` 下的单元测试、测试资产与冒烟矩阵分别验证哪些模块，遇到问题时能找到对应的测试来理解行为。

本讲是纯「读地图」的一讲：不需要真正跑一次转换，但会带你浏览目录、阅读几个关键的「边界文件」，并动手画一张模块依赖草图。

## 2. 前置知识

- **包（package）与模块（module）**：Python 里一个含 `__init__.py` 的目录就是一个包，一个 `.py` 文件就是一个模块。`pdf_craft/pdf/ocr.py` 表示 `pdf_craft` 包下的 `pdf` 子包中的 `ocr` 模块。
- **`__init__.py` 是包的「门面」**：外部使用者 `from pdf_craft import PDFCraft` 时，Python 执行的就是 `pdf_craft/__init__.py`。一个包把哪些名字写进 `__init__.py`，就等于宣布「这些是我的公开 API」。
- **门面模式（facade）回顾**：u1-l1 已经讲过，所有工作流都由 `PDFCraft` 这个门面类驱动。本讲我们自顶向下看这个门面「背后」由哪些模块组成。
- **数据流（data flow）**：程序把输入加工成输出的路径。pdf-craft 的核心数据流是：扫描版 PDF →（OCR 认字）→ 结构化中间产物 →（渲染）→ Markdown/EPUB；或 →（翻译 + 回写）→ 译文 PDF。
- **前置讲义**：u1-l2 中你已经完成安装，并知道提取结果会落成一个 `DocumentPackage` 中间包（`chapters/`、`assets/`、`toc.xml`、`document.json`）。本讲会把这份目录约定和源码模块一一对应起来。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| `pdf_craft/__init__.py` | 核心库的公共导入面（公开 API 边界），从各子包汇集导出 |
| `pdf_craft/craft.py` | `PDFCraft` 门面类：组合提取、渲染、翻译的对外方法 |
| `pdf_craft/transform.py` | 内部提取引擎 `PDFExtractionEngine`：OCR 循环、目录分析、章节生成、元数据落盘 |
| `pdf_craft/extractor/` | 提取层：`toc/`（目录分析）、`chapter/`（章节生成）、`pdf/`（提取入口 `PDFExtractor`） |
| `pdf_craft/pdf/` | PDF 基础设施：页面渲染、OCR 驱动器、OCR 后端适配 |
| `pdf_craft/document/` | `DocumentPackage`：渲染就绪中间产物的路径契约与元数据 |
| `pdf_craft/transformer/` | 格式无关的 XML 内容变换（含 `XMLTranslator` 翻译引擎） |
| `pdf_craft/renderer/` | 渲染层：`markdown/` 与 `epub/` 两个渲染器 |
| `pdf_craft/markdown/` | Markdown 段落解析与输出排版辅助 |
| `pdf_craft/pipeline/` | 格式专属编排：`epub/`（EPUB 翻译管线）、`pdf/`（译文回写 PDF 管线） |
| `pdf_craft/llm/` | LLM 配置与运行时（目录层级增强、翻译都用到） |
| `pdf_craft/common/` | 可复用的文件系统、XML、统计等辅助逻辑 |
| `pdf_craft_tool/` | 仓库本地 CLI（不随包发布）：手动转换、翻译、冒烟矩阵 |
| `pdf_craft_tool/README.md` | `pdf_craft_tool` 的定位与全部子命令说明 |
| `tests/` | 单元测试、PDF/EPUB 测试资产（`tests/assets/`）、冒烟矩阵（`tests/smoke/`） |
| `AGENTS.md` | 仓库工作区边界说明：各目录的职责划分 |
| `references/` | 面向贡献者的架构引用文档（`architecture.md`、`conversion-pipeline.md` 等） |
| `docs/`、`README.md`、`README_zh-CN.md` | 面向读者/使用者的文档 |
| `pyproject.toml` | 打包配置，声明「哪些目录会随包发布」 |

## 4. 核心概念与源码讲解

### 4.1 包结构：核心库 pdf_craft 的模块划分

#### 4.1.1 概念说明

打开仓库根目录，最显眼的是两个同名的顶层目录：`pdf_craft/` 和 `pdf_craft_tool/`。它们的分工是本讲最重要的一个区分：

- **`pdf_craft/` 是核心库**：`pip install pdf-craft` 装到用户环境里的就是它。使用者通过 `from pdf_craft import PDFCraft, ...` 使用。它内部又按「可组合的文档处理阶段」划分成若干子包，每个子包只负责一个阶段。
- **`pdf_craft_tool/` 是仓库本地工具**：开发者的「驾驶舱」，用来手动跑转换、验收、执行冒烟矩阵。它**不包含在发布的 Python 包里**，只通过 `pdf_craft` 的公共 API 组合调用，不实现任何核心逻辑。

为什么这样分？因为核心库的用户只关心「转换能力」，而开发者额外需要「可重复的实验工具和测试入口」。把工具留在仓库里、挡在发布包之外，发布的包就保持精简，工具也可以随意改动而不影响兼容性。

#### 4.1.2 核心流程

先总览 `pdf_craft/` 的目录结构（用 `find pdf_craft -type d` 即可得到）：

```text
pdf_craft/
├── __init__.py          # 公共导入面
├── craft.py             # PDFCraft 门面
├── transform.py         # 内部提取引擎 PDFExtractionEngine
├── ocr_config.py        # 六种 OCR 后端配置
├── error.py / metering.py / language.py / expression.py / functions.py / to_path.py
├── common/              # 通用辅助（文件、XML、统计）
├── pdf/                 # PDF 基础设施（页面渲染、OCR 驱动、后端适配）
├── extractor/           # 提取层
│   ├── pdf/             #   提取入口 PDFExtractor
│   ├── toc/             #   目录页定位与层级分析
│   ├── chapter/         #   章节生成（跨页合并、切分、脚注引用）
│   └── ocr/             #   OCR 相关辅助
├── document/            # DocumentPackage 中间产物契约
├── transformer/         # 格式无关的 XML 变换（含 xml_translator/ 翻译引擎）
├── markdown/            # Markdown 段落解析与排版辅助
├── renderer/            # 渲染器
│   ├── markdown/        #   包 → Markdown
│   └── epub/            #   包 → EPUB
├── pipeline/            # 格式专属编排
│   ├── epub/            #   EPUB 翻译管线（含 adapter/）
│   └── pdf/             #   译文回写 PDF 管线
└── llm/                 # LLM 配置与运行时
```

判断「一段功能该去哪个目录找」的经验法则：

| 你想找的功能 | 去哪个子包 |
| --- | --- |
| PDF 怎么被渲染成图片、OCR 怎么逐页跑 | `pdf/` |
| 目录页怎么被识别、章节怎么被切分 | `extractor/toc/`、`extractor/chapter/` |
| 中间包的目录约定（chapters/、assets/、document.json） | `document/` |
| 翻译引擎、自定义内容变换 | `transformer/` |
| Markdown / EPUB 文件怎么生成 | `renderer/`、`markdown/` |
| 现成 EPUB 怎么被翻译、译文怎么写回 PDF | `pipeline/epub/`、`pipeline/pdf/` |
| LLM 请求、重试、缓存 | `llm/` |

#### 4.1.3 源码精读

**第一个关键文件：`pdf_craft/__init__.py` —— 公开 API 的边界。**

这个只有 50 行的文件是理解包结构的最好入口：它的每一组 import 恰好对应一个子模块的公开职责。

[pdf_craft/__init__.py:1-1](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/__init__.py#L1-L1) 从外部依赖 `epub_generator` 转口导出 `BookMeta`、`LaTeXRender`、`TableRender` 三个类型——这说明 EPUB 生成能力来自被 pin 住的外部包，pdf-craft 只做封装。

[pdf_craft/__init__.py:12-24](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/__init__.py#L12-L24) 依次导出：门面 `PDFCraft` 与选项类（来自 `craft.py`）、EPUB/PDF 两条管线入口（`pipeline/`）、内容变换器与翻译器（`transformer/`）、`LLM`（`llm/`）、计量类型（`metering.py`）。

[pdf_craft/__init__.py:25-47](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/__init__.py#L25-L47) 导出六种 OCR 配置（`ocr_config.py`）与 PDF 基础设施类型（`pdf/`：`PDFHandler`、`PDFDocument`、`OCREvent` 等）。

[pdf_craft/__init__.py:48-50](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/__init__.py#L48-L50) 最后三行分别导出中间产物契约 `DocumentPackage`（`document/`）、提取入口 `PDFExtractor`（`extractor/`）、两个渲染器（`renderer/`）。

也就是说：**这个文件的 import 分组就是一张「子模块 → 公开职责」对照表**。反向它也划定了边界——没出现在这里的名字都是内部实现，调用者不应依赖。

**第二个关键文件：`pyproject.toml` —— 谁会被发布。**

[pyproject.toml:54-54](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L54-L54) 声明 `packages = [{include = "pdf_craft"}]`：发布包**只包含** `pdf_craft` 一个目录。这就是「`pdf_craft_tool` 不随包发布」在构建配置层面的证据——它没被列进去，用户 `pip install` 后根本拿不到它。

**第三个关键文件：`AGENTS.md` —— 仓库自己写的边界说明。**

仓库在 [AGENTS.md:5-12](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/AGENTS.md#L5-L12) 的「工作区边界」一节明确写了每个目录的角色，与我们的结论一致：

- [AGENTS.md:7-7](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/AGENTS.md#L7-L7)：`pdf_craft/` 是包源码，公共导入从 `pdf_craft/__init__.py` 暴露。
- [AGENTS.md:8-8](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/AGENTS.md#L8-L8)：`tests/` 是轻量单元测试和小型 PDF fixture。
- [AGENTS.md:11-11](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/AGENTS.md#L11-L11)：`pdf_craft_tool/` 是**未发布的**本地 CLI，`scripts/` 只保留依赖源码同步辅助脚本。
- [AGENTS.md:12-12](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/AGENTS.md#L12-L12)：`pdf-craft-output/`、`models-cache/`、`.venv/` 等都是生成产物，不进提交。

此外 [AGENTS.md:16-17](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/AGENTS.md#L16-L17) 还指向了 `references/` 下的两份深度文档：`architecture.md`（模块归属）和 `conversion-pipeline.md`（转换流水线）。当你以后需要判断「新代码该放哪」时，`references/architecture.md` 是权威参考。

#### 4.1.4 代码实践

**实践：用脚本打印 pdf_craft 的子包结构，验证本节的目录图。**

1. **实践目标**：用代码而非肉眼确认 `pdf_craft` 包内有哪些子包和顶层模块，加深「包结构」的直观印象。
2. **操作步骤**：在仓库根目录创建脚本 `list_pkg.py`（示例代码，放在你自己的工作目录即可）：

   ```python
   # 示例代码
   from pathlib import Path
   import pdf_craft

   root = Path(pdf_craft.__file__).parent
   print(f"包根目录: {root}\n")
   for p in sorted(root.iterdir()):
       if p.is_dir() and (p / "__init__.py").exists():
           subs = sorted(f.name for f in p.iterdir() if f.is_dir())
           files = sorted(f.name for f in p.glob("*.py"))
           print(f"[子包] {p.name}/")
           if subs:
               print(f"       下级子包: {', '.join(subs)}")
           print(f"       模块: {', '.join(files)}")
       elif p.suffix == ".py":
           print(f"[模块] {p.name}")
   ```

   然后运行：

   ```shell
   poetry run python list_pkg.py
   ```

3. **需要观察的现象**：输出应列出 `common`、`document`、`extractor`、`llm`、`markdown`、`pdf`、`pipeline`、`renderer`、`transformer` 九个子包，其中 `extractor` 含下级子包 `chapter`、`ocr`、`pdf`、`toc`，`pipeline` 含 `epub`、`pdf`，`transformer` 含 `xml_translator`；顶层模块包括 `craft.py`、`transform.py`、`ocr_config.py` 等。
4. **预期结果**：输出与 4.1.2 节的目录树一致。若个别文件名有出入（例如后续版本新增了模块），以你的实际输出为准——这正好说明「地图要以源码为准」。
5. 本实践只做目录遍历，不触发 OCR、不联网，可以确定能运行。

#### 4.1.5 小练习与答案

**练习 1**：使用者执行 `from pdf_craft import XMLTranslator` 成功，但 `from pdf_craft.transformer.xml_translator.xml_translator.translator import XMLTranslator` 也能成功。两个导入哪个是「被支持的用法」？为什么？

<details>
<summary>参考答案</summary>

前者。`pdf_craft/__init__.py` 是包声明的公共导入面（见 [pdf_craft/__init__.py:15-22](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/__init__.py#L15-L22)），走这个入口的用法受兼容性保护（`references/architecture.md` 也要求把公开名称视为公共 API）。后者是深入内部路径，模块重命名或调整时随时可能失效。
</details>

**练习 2**：为什么 `pyproject.toml` 里 `packages = [{include = "pdf_craft"}]` 只包含 `pdf_craft`，这给使用者带来什么影响？

<details>
<summary>参考答案</summary>

因为 `pdf_craft_tool` 是开发者本地工具，不属于发布内容。影响是：`pip install pdf-craft` 之后用户环境中**没有** `pdf_craft_tool`，想用这个 CLI 必须克隆仓库、安装开发依赖后用 `python -m pdf_craft_tool` 运行。
</details>

**练习 3**：你想找「OCR 后端的六种配置类」定义在哪个文件，最快捷的方法是什么？

<details>
<summary>参考答案</summary>

看 [pdf_craft/__init__.py:25-36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/__init__.py#L25-L36) 的 import 来源：`from .ocr_config import ...`，即定义在 `pdf_craft/ocr_config.py`。公共导入面就是「类名 → 文件」的索引。
</details>

### 4.2 数据流方向：从 PDF 输入到三种输出

#### 4.2.1 概念说明

只记住「哪个目录干什么」还不够，更重要的是记住**数据怎么流**。pdf-craft 的所有能力都围绕一条主链路展开：

> **PDF 文件 →（提取：OCR + 目录分析 + 章节生成）→ DocumentPackage 中间包 →（可选内容变换）→ 渲染成 Markdown / EPUB；或 →（翻译后）回写成新 PDF**

这条链路上有一个承上启下的枢纽：`DocumentPackage`（由 `document/` 子包定义）。它是「提取」与「渲染/翻译」之间的**稳定契约**——提取器只负责产出这个包，渲染器和翻译管线只消费这个包，两边互不关心对方内部实现。理解了这一点，你就明白了为什么 `document/` 要独立成一个子包，而不是塞进 `extractor/` 里。

另一个设计要点是**延迟加载（lazy loading）**：`PDFCraft` 构造时不会初始化 OCR，只有真正做 PDF 提取时才去加载重型依赖。这让只用 EPUB 翻译功能的用户完全不需要安装 PDF/OCR 基础设施。

#### 4.2.2 核心流程

主数据流的文字版流程图：

```text
PDF 文件
   │
   ▼  pdf/（PDFHandler 渲染页面 → OCR 驱动器 → doc-page-extractor 后端）
ocr/page_N.xml 等分析缓存（可丢弃，用于断点续跑）
   │
   ▼  extractor/toc/（目录页定位 + 标题层级分析）
toc.xml（目录树）
   │
   ▼  extractor/chapter/（跨页合并、章节切分、脚注引用收集）
chapters/chapter_N.xml + assets/（图片等资源）+ cover.png（可选）
   │
   ▼  document/（DocumentPackage：把以上产物打包成契约，写入 document.json）
DocumentPackage 中间包
   │
   ├──（无变换）────────────────────────────────┐
   │                                            ▼
   │  可选 transformer/（如 XMLTranslator 翻译） renderer/markdown ──► Markdown 文件
   │  产出「变换后的包」                          renderer/epub    ──► EPUB 文件
   │                                            （markdown/ 提供段落解析与排版辅助）
   ▼
pipeline/pdf（PDFTranslationPipeline + PDFPatcher，读取原 PDF 与包内几何信息）
   │
   ▼
译文回写后的 PDF 文件
```

提取阶段可以概括为固定的四步（下一小节逐行对应源码）：

1. **OCR 循环**：逐页识别，产出 `ocr/page_N.xml` 缓存；
2. **目录分析**：从页数据推断目录树，产出 `toc.xml`；
3. **章节生成**：按目录切分章节，产出 `chapters/chapter_N.xml`；
4. **元数据落盘**：组装 `DocumentPackage` 并写入 `document.json`。

#### 4.2.3 源码精读

**提取引擎的四步流水线：`pdf_craft/transform.py`。**

[pdf_craft/transform.py:21-22](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L21-L22) 定义 `PDFExtractionEngine`，docstring 明确写着它是「PDFCraft 门面使用的内部提取引擎」——注意「内部」二字，使用者不需要直接碰它。

[pdf_craft/transform.py:66-69](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L66-L69) 一口气定义了四类产物的落盘路径：`assets/`、`ocr/`、`chapters/`、`toc.xml`。这四行就是 4.2.2 流程图中中间产物约定的源头。

[pdf_craft/transform.py:78-94](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L78-L94) 是第一步：迭代 `self._ocr.recognize(...)` 生成器，逐页消费 OCR 事件（同时统计 token、收集失败页）。OCR 的具体驱动逻辑在 `pdf/` 子包（后续 u3 单元精读）。

[pdf_craft/transform.py:106-112](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L106-L112) 是第二、三步：先 `analyse_toc(...)`（来自 `extractor/toc/`）产出目录，再 `generate_chapter_files(...)`（来自 `extractor/chapter/`）按目录切出章节文件。

[pdf_craft/transform.py:116-120](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L116-L120) 是第四步：构造 `DocumentPackage` 并 `write_metadata(...)` 写入页面几何信息——这些几何数据正是日后 `pipeline/pdf` 把译文回写 PDF 时定位文字位置的依据。

**门面如何串起全链路：`pdf_craft/craft.py`。**

[pdf_craft/craft.py:69-74](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L69-L74) `PDFCraft` 类的 docstring 点明两件事：它「组合提取、渲染和格式专属翻译工作流」，且「构造时不初始化 OCR，只用 EPUB 的调用者可以无凭据构造 `PDFCraft()`」。

[pdf_craft/craft.py:179-190](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L179-L190) `convert_pdf_to_markdown` 是主链路的最小完整样本，四行对应四个阶段：`_package_workspace` 提供工作区（L185）→ `extract_pdf_with_metering` 提取（L186）→ `_apply_steps` 应用可选变换步骤（L187）→ `render_markdown` 渲染（L188）。`convert_pdf_to_epub`（L192 起）结构完全相同，只是渲染器换成 EPUB。

[pdf_craft/craft.py:253-262](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L253-L262) `_pdf_engine` 是「延迟加载」的证据：只有真正需要 PDF 提取时才 `from .transform import PDFExtractionEngine`（L259 的注释写明这是为了让只用 EPUB 的调用者不导入重型适配层）。提取入口再经 [pdf_craft/extractor/pdf/extractor.py:5-13](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/pdf/extractor.py#L5-L13) 的 `PDFExtractor`（`extractor/` 子包的公开边界，其 docstring 也强调「重型 OCR 导入保持懒加载」）转发到引擎。

**一句话串联**：`craft.py`（编排）→ `extractor/`（边界）→ `transform.py`（引擎）→ `pdf/` + `extractor/toc/` + `extractor/chapter/`（干活的子包）→ `document/`（契约）→ `renderer/` 或 `pipeline/`（输出）。这条调用链与 4.2.2 的数据流图逐段对应。

#### 4.2.4 代码实践

**实践：把一个提取产物目录「翻译」回模块归属。**

1. **实践目标**：拿到一个真实的 `DocumentPackage` 目录，把其中每类文件与产出它的子模块对应起来，验证数据流不是纸上谈兵。
2. **操作步骤**：
   - 如果你按 u1-l2 的实践保留了 `package_path` 指定的提取产物，直接用它；否则重跑一次（需要 OCR 凭据）：

     ```python
     # 示例代码
     from pdf_craft import PDFCraft, PDFOptions, ExtractionOptions
     from pdf_craft.ocr_config import DeepSeekOCRVendorConfig

     craft = PDFCraft(pdf=PDFOptions(
         ocr=DeepSeekOCRVendorConfig(api_key="...", endpoint="..."),
     ))
     craft.extract_pdf("tests/assets/citation.pdf", "my-package",
                       options=ExtractionOptions(page_indexes=range(1, 3)))
     ```

   - 然后列出 `my-package/` 的目录内容：

     ```shell
     find my-package -maxdepth 2 | sort
     ```

3. **需要观察的现象**：目录里应出现 `ocr/`（含 `page_N.xml`）、`toc.xml`、`chapters/`、`assets/`、`document.json`，可能还有 `cover.png`。
4. **预期结果**：按下表把每个产物标注到源码模块（依据就是 4.2.3 精读的 [pdf_craft/transform.py:66-120](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transform.py#L66-L120)）：

   | 产物 | 产出它的模块 |
   | --- | --- |
   | `ocr/page_N.xml` | `pdf/`（OCR 驱动） |
   | `toc.xml` | `extractor/toc/` |
   | `chapters/chapter_N.xml` | `extractor/chapter/` |
   | `assets/`、`document.json` | `document/`（`write_metadata`） |

   若当前环境没有 OCR 凭据无法生成产物，可改用任何已有的提取产物目录完成标注——该部分**待本地验证**。
5. 注意：`ocr/` 属于「可丢弃的分析缓存」（断点续跑用），真正构成 `DocumentPackage` 契约的是 `chapters/`、`assets/`、`toc.xml`、`cover.png` 与 `document.json`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `DocumentPackage` 要放在独立的 `document/` 子包，而不是放在 `extractor/` 里？

<details>
<summary>参考答案</summary>

因为它是提取器与渲染器/翻译管线之间的**中立契约**：`renderer/`、`transformer/`、`pipeline/pdf` 都要消费它。放进 `extractor/` 会让消费方反向依赖提取模块，破坏「提取 → 契约 → 渲染」的单向数据流。`references/architecture.md` 也把它列为与 extractor、renderer 平行的阶段。
</details>

**练习 2**：只做 EPUB 翻译（`translate_epub`）的用户为什么可以完全不装 OCR 相关设施？

<details>
<summary>参考答案</summary>

`PDFCraft` 构造是惰性的：`_pdf_engine()`（[pdf_craft/craft.py:253-262](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L253-L262)）只在真正提取 PDF 时才懒加载 `PDFExtractionEngine` 及其 OCR 依赖；`translate_epub` 走的是 `pipeline/epub/`，根本不触发这条路径。
</details>

**练习 3**：`convert_pdf_to_markdown` 与 `convert_pdf_to_epub` 在源码结构上是什么关系？

<details>
<summary>参考答案</summary>

同构。两者都是「workspace → 提取 → `_apply_steps` → 渲染」的四段式（[pdf_craft/craft.py:179-210](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L179-L210)），区别只在最后一步调 `render_markdown` 还是 `render_epub`（以及 EPUB 额外提取书籍元数据）。
</details>

### 4.3 工具与测试：pdf_craft_tool 与 tests

#### 4.3.1 概念说明

仓库里除了核心库还有两块「工程质量保障」：

- **`pdf_craft_tool/`（仓库本地 CLI）**：手动转换、翻译、验收工具，外加一套「冒烟矩阵」运行器。它读取 `.env` 里的凭据组装配置，每次运行创建独立的工作目录，方便对比多次实验。它是你日后做本手册几乎所有动手实践的趁手工具。
- **`tests/`（测试与资产）**：分三类内容——轻量单元测试（`test_*.py`，不需要 GPU/网络）、测试资产（`tests/assets/` 下的小型 PDF 与 EPUB 样本）、冒烟矩阵配置（`tests/smoke/*.json`，声明式描述要跑哪些转换组合）。

特别值得注意的是两份「架构守卫测试」：`test_module_boundaries.py` 和 `test_composable_boundaries.py`。它们不断言业务行为，而是**用测试固化模块边界**——例如「EPUB 编排必须归 `pipeline/epub` 所有」「XMLTranslator 不得依赖 EPUB 细节」。这是大型项目防止架构被慢慢侵蚀的常用手段，也反过来帮我们确认 4.1 节讲的边界是「被测试保护的」，不是文档里的一厢情愿。

#### 4.3.2 核心流程

`pdf_craft_tool` 的子命令树（定义在 `pdf_craft_tool/cli.py`，用 `python -m pdf_craft_tool --help` 查看）：

| 子命令 | 作用 | 需要什么 |
| --- | --- | --- |
| `pdf extract` | PDF → 可复用的 DocumentPackage | OCR 配置 |
| `pdf convert` | PDF → Markdown 或 EPUB | OCR 配置 |
| `pdf translate` | PDF → 翻译后的 Markdown / EPUB / PDF | OCR + LLM |
| `package translate` | 已有包 → 翻译后的包（不重跑 OCR） | LLM |
| `package render` | 已有包 → Markdown / EPUB | 无需 OCR/LLM |
| `package patch-pdf` | 原 PDF + 包 → 回写 PDF | 无需 OCR/LLM |
| `epub translate` | EPUB → 翻译 EPUB | LLM |
| `smoke assets / run / matrix` | 列出资产 / 跑单条冒烟通路 / 跑 JSON 矩阵 | 视通路而定 |

`tests/` 与模块的对应关系（这也是综合实践要标注的内容）：

| 测试文件 | 验证的模块 |
| --- | --- |
| `test_jointer.py`、`test_mergeable.py`、`test_reading_serials.py`、`test_punctuation.py` | `extractor/chapter/`（合并、切分、标点） |
| `test_toc_text.py`、`test_toc_llm_analyser.py` | `extractor/toc/` |
| `test_page_extractor_structured.py` | `pdf/page_extractor.py`（OCR 后端适配） |
| `test_parser.py`、`test_table_rendering.py`、`test_cv_splitter.py` | `markdown/` |
| `test_llm_loop.py`、`test_llm_guaranteed.py`、`test_llm_runtime.py` | `llm/` |
| `test_pdf_patcher.py`、`test_pdf_text_layout.py` | `pipeline/pdf/` |
| `test_ocr_config.py` | `ocr_config.py` |
| `test_expression.py` | `expression.py` |
| `test_craft.py` | `craft.py`（门面） |
| `test_tool.py` | `pdf_craft_tool/`（CLI 行为） |
| `test_smoke.py` | 冒烟运行器 |
| `test_module_boundaries.py`、`test_composable_boundaries.py` | 架构约束（跨模块） |
| `tests/assets/`、`tests/smoke/*.json` | 测试资产与冒烟矩阵 |

#### 4.3.3 源码精读

**工具的定位声明：`pdf_craft_tool/README.md`。**

[pdf_craft_tool/README.md:3-5](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/README.md#L3-L5) 一句话讲清定位：「仓库内的开发、验收与手动转换工具，**不包含在发布的 pdf-craft Python 包中**，只通过 pdf_craft 的公共 API 组合 Extractor、Renderer、Pipeline 和 Transformer」——注意它列出的四个词正是 4.1/4.2 节讲的四个核心阶段。

[pdf_craft_tool/README.md:40-46](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/README.md#L40-L46) 描述工作目录规则：每次 `pdf extract/convert/translate` 都在 Git 忽略的 `pdf-craft-output/manual/` 下创建以「来源-操作-日期-序号」命名的独立目录（如 `citation-convert-20260822-001/`），同一次调用绝不覆盖已有目录；目录内保存中间 `package/`、翻译缓存和日志，方便检查、恢复或后续单独渲染。

[pdf_craft_tool/README.md:50-74](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/README.md#L50-L74) 给出 PDF 与 Package 两组命令示例，其中的注释直接印证数据流：`pdf extract`（PDF → 可复用包）、`package render`（不需要 OCR 配置）、`package patch-pdf`（不需要 OCR/LLM）。

[pdf_craft_tool/README.md:155-157](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/README.md#L155-L157) 列出冒烟矩阵的全部 route：`package`、`markdown`、`epub`、`pdf-patch`、`epub-check`、`epub-translate` 以及验证 renderer 分支的 `package-markdown`、`package-epub`——恰好覆盖主数据流的每一条输出分支。

**子命令的注册处：`pdf_craft_tool/cli.py`。**

[pdf_craft_tool/cli.py:55-116](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/cli.py#L55-L116) 用 argparse 依次注册 `pdf`（L55，下设 `extract`/`convert`/`translate`）、`package`（L78，下设 `translate`/`patch-pdf`/`render`）、`epub`（L106）、`smoke`（L116）四组子命令，与 4.3.2 的表格一一对应。

**架构守卫测试：`tests/test_module_boundaries.py`。**

[tests/test_module_boundaries.py:9-11](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_module_boundaries.py#L9-L11) 断言 `translate_epub` 函数必须属于 `pdf_craft.pipeline.epub` 模块——「EPUB 编排归 pipeline 所有」这条边界被固化为测试。

[tests/test_module_boundaries.py:12-18](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_module_boundaries.py#L12-L18) 更进一步：把 `transformer/xml_translator/` 下所有源码拼起来，断言其中**不出现** `pipeline.epub`、`Zip(`、`search_spine_paths` 等字样——即翻译引擎必须与 EPUB 容器格式彻底解耦。这正是 4.1 节「transformer 格式无关」结论的直接证据。

#### 4.3.4 代码实践

**实践：跑通 CLI 帮助与一个无凭据依赖的架构测试。**

1. **实践目标**：亲手驱动 `pdf_craft_tool` 看到子命令树；运行一个不需要 OCR/LLM 的单元测试，体会「轻量测试」的含义。
2. **操作步骤**：

   ```shell
   # 步骤 1：查看 CLI 子命令树
   poetry run python -m pdf_craft_tool --help

   # 步骤 2：查看 pdf 组的三个子命令
   poetry run python -m pdf_craft_tool pdf --help

   # 步骤 3：运行架构守卫测试（纯文本断言，不联网、不需要 .env）
   poetry run pytest tests/test_module_boundaries.py -v
   ```

3. **需要观察的现象**：
   - 步骤 1/2 的帮助文本列出 `pdf`、`package`、`epub`、`smoke` 四组子命令，`pdf` 组下有 `extract`、`convert`、`translate`；
   - 步骤 3 输出 `test_epub_orchestration_is_owned_by_pipeline` 与 `test_xml_translator_is_format_agnostic` 两条用例。
4. **预期结果**：步骤 3 两条用例均为 `PASSED`（2 passed）。若你想进一步观察「边界被破坏会怎样」，可以临时在 `pdf_craft/transformer/xml_translator/` 任一文件里加入一行含 `search_spine_paths` 的字符串再跑测试，应看到失败——**实验后记得还原，不要把修改留在源码里**。
5. 本实践不依赖 OCR 凭据与网络；步骤 4 的破坏性实验涉及临时改源码，属可选项，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`pdf_craft_tool` 的 `package render` 子命令为什么不需要 OCR 配置？

<details>
<summary>参考答案</summary>

因为 `package render` 的输入是**已经提取好的 DocumentPackage**，直接走 `renderer/` 渲染；OCR 只发生在「PDF → 包」的提取阶段（`pdf/` 子包）。这正体现了 `DocumentPackage` 作为中间契约把两段流程解耦的设计（[pdf_craft_tool/README.md:62-64](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft_tool/README.md#L62-L64) 的注释也这么写）。
</details>

**练习 2**：`tests/smoke/minimal.json` 与 `tests/test_jointer.py` 的验证方式有什么本质区别？

<details>
<summary>参考答案</summary>

`test_jointer.py` 是轻量单元测试：不联网、不跑 OCR，针对纯函数断言输入输出，秒级完成。冒烟矩阵是端到端验收：声明式 JSON 描述完整的转换组合（资产、route、OCR backend、页数等），真实执行「PDF → 输出」全链路并检查产物，需要凭据与网络（或本地模型）。前者验证模块正确性，后者验证链路可用性。
</details>

**练习 3**：如果有人把 EPUB 的 ZIP 处理代码搬进了 `transformer/xml_translator/`，哪个测试会失败？

<details>
<summary>参考答案</summary>

`tests/test_module_boundaries.py` 的 `test_xml_translator_is_format_agnostic`（[tests/test_module_boundaries.py:12-18](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_module_boundaries.py#L12-L18)）——它扫描该目录全部源码，发现 `Zip(` 或 `search_spine_paths` 字样即断言失败。
</details>

## 5. 综合实践

**综合实践：绘制一张带测试标注的模块依赖草图。** 这是本讲的收官任务，把三个最小模块（包结构、数据流、工具与测试）串成一张图。

1. **实践目标**：产出一张「PDF 输入 → Markdown/EPUB/PDF 输出」的模块依赖草图，每个环节标注负责的子包，并标注 `tests/` 下对应的测试文件。
2. **操作步骤**：
   - 先用 4.1.4 的脚本或 `find pdf_craft -type d` 确认子包清单；
   - 参考 4.2.2 的流程图，画一张**属于你自己的**图（ASCII、mermaid 或纸笔手绘皆可），要求覆盖：`pdf/` → `extractor/toc/` → `extractor/chapter/` → `document/` → 三条输出分支（`renderer/markdown`、`renderer/epub`、`pipeline/pdf`），以及插入在渲染前的 `transformer/` 与独立的 `pipeline/epub`；
   - 打开 `tests/` 目录，按 4.3.2 的表格把测试文件标注到图中对应模块旁边；
   - 最后在图上用虚线标出 `pdf_craft_tool/` 从外部调用门面 `PDFCraft` 的位置。
3. **需要观察的现象**：画图过程中你会被迫回答「这一步的输入是什么、输出是什么、谁来负责」——答不上来的地方就是需要回读源码的地方。
4. **预期结果**：草图应与下面的参考骨架等价（细节可自行增删）：

   ```text
   tests/test_page_extractor_structured.py      tests/test_toc_text.py / test_toc_llm_analyser.py
             │                                            │
   PDF ──► pdf/ ──► ocr/*.xml ──► extractor/toc/ ──► extractor/chapter/ ──► document/
                                                        (test_jointer/test_mergeable/     │
                                                         test_reading_serials/            │
                                                         test_punctuation)                │
   ┌──────────────────────────────────────────────────────────────────┼──────────────┐
   │                                                  可选 transformer/  │              │
   │                                                  (xml_translator)  ▼              ▼
   │                                            renderer/markdown ──► .md    renderer/epub ──► .epub
   │                                            (test_parser 等)               (test_table_rendering 等)
   │                                                                              │
   └──► pipeline/pdf ──► 回写 .pdf                                     pipeline/epub ──► 翻译 .epub
        (test_pdf_patcher / test_pdf_text_layout)                           （从现有 EPUB 直接进入）
                    ▲
                    │  craft.py (test_craft.py) —— 门面编排以上全部
   pdf_craft_tool/ ·····▶ craft.py（仅通过公共 API 组合；test_tool.py / tests/smoke/*.json）
   ```

5. 完成后把草图保存下来——后续 u3（提取主链路）、u6（文档包与渲染）、u10（PDF 回写）单元都会在这张图上「放大」局部，你可以不断往上面补充细节。

## 6. 本讲小结

- 仓库分两层：`pdf_craft/` 是发布到 PyPI 的核心库，`pdf_craft_tool/` 是不随包发布的仓库本地 CLI（证据：`pyproject.toml` 只打包 `pdf_craft`，`AGENTS.md` 与 `pdf_craft_tool/README.md` 明确其定位）。
- `pdf_craft/__init__.py` 是公开 API 边界，它的 import 分组就是「子模块 → 公开职责」的对照表。
- 核心数据流：PDF →（`pdf/` OCR）→ `extractor/toc/` → `extractor/chapter/` → `document/`（DocumentPackage 契约）→ 可选 `transformer/` → `renderer/`（Markdown/EPUB）或 `pipeline/pdf`（回写 PDF）；`pipeline/epub` 独立处理现有 EPUB 的翻译。
- 提取引擎 `PDFExtractionEngine`（`transform.py`）的固定四步：OCR 循环 → `analyse_toc` → `generate_chapter_files` → `write_metadata`。
- `DocumentPackage` 是提取与渲染之间的中立契约，让 `package render` 无需 OCR、`package patch-pdf` 无需 OCR/LLM 成为可能。
- `tests/` 分三层：轻量单元测试（按模块命名）、测试资产（`tests/assets/`）、冒烟矩阵（`tests/smoke/*.json`）；`test_module_boundaries.py` 等架构守卫测试把模块边界固化为断言。

## 7. 下一步学习建议

- **下一讲（u1-l4）**：精读 `pdf_craft/craft.py` 的 `PDFCraft` 门面，逐个掌握 `convert_pdf_to_markdown`、`convert_pdf_to_epub`、`extract_pdf`、`translate_pdf`、`translate_epub` 等公开方法——本讲只画了它们背后的地图，下一讲把地图上的每个入口走一遍。
- **延伸阅读**（贡献者视角）：仓库 `references/architecture.md`（模块归属与公共 API 约束）和 `references/conversion-pipeline.md`（转换流水线与中间产物契约）是本讲内容的权威补充，值得通读一遍。
- **动手准备**：把综合实践的草图保留好；进入 u3 单元（提取主链路）时，我们将放大图中 `pdf/` 与 `extractor/` 这一段逐行精读。
