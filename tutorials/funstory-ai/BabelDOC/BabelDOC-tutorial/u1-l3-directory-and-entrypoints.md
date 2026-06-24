# 目录结构与入口文件

## 1. 本讲目标

学完本讲后，你应该能够：

- 画出 `babeldoc` 包的顶层目录树，并用一句话说清每个子包的职责。
- 定位两条最重要的入口：CLI 入口 `main.cli()` 与翻译编排入口 `babeldoc.format.pdf.high_level`。
- 说清 `document_il` 与 `new_parser` 的分工——前者是「IL 数据模型 + 各处理阶段」，后者是「把 PDF 解析成 IL 的前端」。
- 认识 `docvision`（版面/表格识别）和 `translator`（翻译服务）这两个可替换子包。
- 完成代码实践：手绘从 `main.cli()` 到 `high_level.async_translate()` 的调用链路图，并标注每一步所在的源码文件。

本讲承接 u1-l1（项目定位）与 u1-l2（安装与 CLI 运行），不再重复「BabelDOC 是什么」「怎么装」；本讲要回答的是「**代码长在哪里、从哪进、往哪流**」。

## 2. 前置知识

本讲需要你具备的概念都来自前两讲，这里只做最小回顾：

- **Parsing（解析）**：把 PDF 文件读进来，变成可操作的数据结构。
- **Rendering（渲染）**：把处理好的数据再写回成 PDF。
- **IL（Intermediate Language，中间表示）**：BabelDOC 在解析与渲染之间引入的内部数据模型，它保留了每个字符/段落的坐标，是「保结构翻译」的关键。

在目录层面，这三个概念对应三段式分工：

| 概念 | 在代码中的位置 | 一句话职责 |
| --- | --- | --- |
| Parsing 前端 | `format/pdf/new_parser/` | PDF 文件 → IL |
| IL 处理 | `format/pdf/document_il/` | 定义 IL 模型 + 在 IL 上做版面/段落/公式/翻译/排版 |
| Rendering 后端 | `format/pdf/document_il/backend/` | IL → PDF |

理解这张表，就理解了 BabelDOC 整个目录的核心切分逻辑。

## 3. 本讲源码地图

本讲精读 / 引用的关键文件：

| 文件 | 作用 |
| --- | --- |
| `babeldoc/main.py` | CLI 入口：`cli()` 同步壳 → `main()` 异步核心；`create_parser()` 定义命令行参数 |
| `babeldoc/format/pdf/high_level.py` | **翻译编排入口**：`async_translate / translate / do_translate / _do_translate_single` 与 `TRANSLATE_STAGES` 全景表 |
| `pyproject.toml` | 声明入口点 `babeldoc = "babeldoc.main:cli"` 与依赖 |

辅助定位（目录结构用到的子包入口，本讲只做认知，不精读）：

- `babeldoc/__init__.py`（版本号）、`babeldoc/const.py`（全局常量）
- `babeldoc/format/pdf/document_il/`、`babeldoc/format/pdf/new_parser/`
- `babeldoc/docvision/`、`babeldoc/translator/`

## 4. 核心概念与源码讲解

### 4.1 顶层目录结构

#### 4.1.1 概念说明

一个 Python 包的目录结构，本质上是「这个项目把职责切成了哪几块」。BabelDOC 的顶层目录切分非常贴近它的三段式架构与可替换组件设计。看懂目录树，就能在心里建立「要改某类问题，去哪个文件夹找」的索引。

#### 4.1.2 核心流程

`babeldoc` 顶层包包含以下子包与文件，可按下表理解其归属：

```
babeldoc/
├── main.py                 # CLI 入口
├── const.py                # 全局常量（CACHE_FOLDER、WATERMARK_VERSION 等）
├── glossary.py             # 术语表加载与匹配
├── progress_monitor.py     # 多阶段进度监控
├── assets/                 # 字体/模型/CMap 资源下载、校验、离线包
├── asynchronize/           # 同步回调 → 异步事件流的桥接
├── babeldoc_exception/     # 异常体系（BabelDOCException）
├── docvision/              # 版面分析 + 表格识别（可换 RPC/本地）
├── format/pdf/             # PDF 翻译主链路（核心）
│   ├── high_level.py       #   翻译编排入口
│   ├── translation_config.py #   TranslationConfig 中心配置
│   ├── split_manager.py    #   分片翻译
│   ├── result_merger.py    #   分片结果合并
│   ├── new_parser/         #   PDF 解析前端（PDF → IL）
│   ├── document_il/        #   IL 模型 + midend 阶段 + backend 渲染
│   └── babelpdf/           #   字体子集化/CMap/编码等底层
├── pdfminer/               # vendor 内置的 pdfminer-six
├── tools/                  # executor(RPC 服务)、字体/CMap 元数据生成等
├── translator/             # 翻译器服务（OpenAI 兼容）+ 缓存
└── utils/                  # 通用工具（内存监控、优先级线程池…）
```

一个有用的记忆口诀：**「一条主线，两个可换部件，三层底层支撑」**。

- 一条主线：`format/pdf/`（high_level 编排 → new_parser 解析 → document_il 处理/渲染）。
- 两个可换部件：`docvision/`（版面模型可本地 ONNX 也可 RPC）、`translator/`（翻译服务）。
- 三层底层支撑：`assets/`（资源）、`pdfminer/`（PDF 解析底层，已 vendor）、`babelpdf/`（字体/编码底层）。

#### 4.1.3 源码精读

入口点声明在 `pyproject.toml`，这是 `babeldoc` 命令能直接在终端运行的根源：

[pyproject.toml:66-67](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L66-L67) —— 这两行把控制台命令 `babeldoc` 注册到 `babeldoc.main:cli`，即安装后终端敲 `babeldoc` 就等于调用 `babeldoc.main` 模块里的 `cli()` 函数。

版本号集中在 `babeldoc/__init__.py`（导出版本），与 `main.py`、`const.py` 一同由 `bumpver` 工具同步更新（见 [pyproject.toml:169-182](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L169-L182)）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立「目录 → 职责」的直觉索引。
2. **操作步骤**：在仓库根目录执行 `ls babeldoc/` 与 `ls babeldoc/format/pdf/`，对照上面的目录树，逐项标注每个文件夹属于「主线 / 可换部件 / 底层支撑」中的哪一类。
3. **需要观察的现象**：确认 `high_level.py`、`translation_config.py` 直接平铺在 `format/pdf/` 下，而 `new_parser/`、`document_il/` 是它的两个子目录。
4. **预期结果**：你能闭眼说出「改翻译流程去 `high_level.py`、改 IL 模型去 `document_il/`、改 PDF 解析去 `new_parser/`」。

#### 4.1.5 小练习与答案

**练习 1**：`pdfminer/` 为什么直接放在 `babeldoc/` 顶层而不是作为 pip 依赖？

> 答案：它被 **vendor（内嵌）** 进来了，BabelDOC 在其基础上做了定制。`pyproject.toml` 的依赖列表里 `pdfminer-six` 那一行是被注释掉的（见 [pyproject.toml:32](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L32)），且 ruff 配置对该目录放宽了大量规则（见 [pyproject.toml:144](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L144)），说明它是「自带副本」而非普通依赖。

**练习 2**：`babelpdf/` 和 `document_il/backend/` 都涉及 PDF 生成，二者如何区分？

> 答案：`babelpdf/` 是**底层原语**（base14 字体、CID 字体、CMap、编码、type3 字体），提供字体/编码的机制能力；`document_il/backend/pdf_creater.py` 是**上层编排**，调用这些原语把 IL 渲染成最终的 mono/dual PDF。前者是「零件」，后者是「装配车间」。

---

### 4.2 main 入口与 high_level 入口

#### 4.2.1 概念说明

BabelDOC 有两条必须分清的「入口」：

1. **CLI 入口** `main.cli()`：程序最先被调到的地方，负责日志初始化、参数解析，然后把控制权交给异步核心。
2. **翻译编排入口** `high_level`：真正干活的地方，负责把一份 PDF 翻译出来。

之所以拆成两层，是因为 CLI 需要做大量「壳」工作（日志降噪、进程池设置、同步/异步桥接），而翻译逻辑本身是独立的、可被库直接调用的（这正是 BabelDOC「定位为库」的体现——u1-l1 提到下游 PDFMathTranslate-next 会直接调用 `high_level`，而不走 `main.cli`）。

#### 4.2.2 核心流程

CLI 层的调用顺序（同步 → 异步）：

```
cli()                  # 同步壳：日志、进程池、init()
  └─ asyncio.run(main())
       └─ main()       # 异步核心：解析参数、构造 translator/config
            └─ async for event in high_level.async_translate(config):
                 progress_handler(event)   # 消费进度事件
```

翻译编排层（异步 → 同步）：

```
async_translate(config)                    # 异步入口（库的标准调用方式）
  └─ run_in_executor(do_translate, ...)    # 把同步翻译塞进线程池
       └─ do_translate(pm, config)
            └─ _do_translate_single(...)   # 真正的一条龙：解析→处理→渲染
```

关键点：`async_translate` 本身不实现翻译，它是「同步 `do_translate`」的异步包装；真正的主链路在 `_do_translate_single`。

#### 4.2.3 源码精读

**CLI 同步壳** `cli()`：做日志降噪、调用 `init()` 创建缓存目录，最后用 `asyncio.run(main())` 进入异步：

[babeldoc/main.py:907-937](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L907-L937) —— 注意末尾三行：`speed_up_logs()` 用队列加速日志；`high_level.init()` 创建缓存目录；`asyncio.run(main())` 是同步到异步的桥。

**异步核心** `main()`：解析参数、校验服务、为每个文件构造 `TranslationConfig`，然后消费事件流：

[babeldoc/main.py:744-755](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L744-L755) —— 这一段是 CLI 与翻译编排的交接点：`async for event in babeldoc.format.pdf.high_level.async_translate(config)`，拿到 `finish` 事件就读取 `translate_result`。

**翻译编排入口**（在 `high_level.py`）：四个函数的层次关系。

- 全景表 `TRANSLATE_STAGES`：[babeldoc/format/pdf/high_level.py:60-75](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L60-L75) —— 列出所有阶段及其进度权重（如解析占 14.12%、ILTranslator 占 46.96%），这是整个翻译流水线的「目录式总览」。
- 同步入口 `translate`：[babeldoc/format/pdf/high_level.py:259-261](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L259-L261) —— 包一层 `ProgressMonitor`，调用 `do_translate`。
- 异步入口 `async_translate`：[babeldoc/format/pdf/high_level.py:299-377](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L299-L377) —— 用 `loop.run_in_executor(None, do_translate, pm, translation_config)`（第 361 行）把同步翻译放进默认线程池，再用 `AsyncCallback` 把回调转成异步事件流 yield 出去。
- 真正主链路 `do_translate` / `_do_translate_single`：[babeldoc/format/pdf/high_level.py:527-548](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L527-L548) —— `do_translate` 先做元数据校验，再按是否分片选择 `SplitManager` 多分片或直接 `_do_translate_single`。

> 说明：`do_translate` 还包含 PDF 预处理（`fix_null_xref` 等）与分片逻辑，本讲只定位入口；这些细节留给 u2-l2 精讲。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：确认「同步壳 → 异步核心 → 编排入口」三段接力。
2. **操作步骤**：
   1. 打开 `babeldoc/main.py`，定位 `cli()`（907 行）→ `asyncio.run(main())`（937 行）→ `main()`（461 行）。
   2. 在 `main()` 内定位到 `async for event in ... async_translate(config)`（745 行）。
   3. 打开 `high_level.py`，定位 `async_translate`（299 行）→ `run_in_executor`（361 行）→ `do_translate`（527 行）→ `_do_translate_single`（836 行）。
3. **需要观察的现象**：每一跳都跨越了一个函数边界，且从 `async_translate` 之后才进入 `high_level.py`。
4. **预期结果**：你能写出一条「函数名:文件名:行号」的完整链路（见本讲第 5 节综合实践）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `async_translate` 要用 `run_in_executor` 调 `do_translate`，而不是直接 `await`？

> 答案：`do_translate` 是**同步**函数（CPU/IO 密集，含 PDF 解析与渲染），直接在事件循环里跑会阻塞整个循环。`run_in_executor` 把它丢到线程池，事件循环得以保持响应，从而能并发地把进度事件 yield 给调用方（如 CLI 的进度条）。

**练习 2**：下游库（如 PDFMathTranslate-next）想调用 BabelDOC，应该用 `main.cli()` 还是 `high_level.async_translate()`？

> 答案：用 `high_level.async_translate(config)`。`main.cli()` 是为「终端命令」准备的壳（带日志、读 argv），不适合被程序化调用；`async_translate` 接受一个 `TranslationConfig` 并 yield 事件流，才是干净的库 API。

---

### 4.3 document_il 与 new_parser 分工

#### 4.3.1 概念说明

这是新人最容易混淆的一对目录，因为它们都「跟 IL 打交道」。区分关键在于**方向**：

- `new_parser/`：**生产** IL。它读 PDF 文件，输出一个 `il_version_1.Document` 对象。它是 Parsing 前端。
- `document_il/`：**承载并消费** IL。它定义 IL 长什么样（数据模型），并在 IL 上做各种处理（版面、段落、公式、翻译、排版），最后把 IL 渲染回 PDF。

一句话：`new_parser` 是「造 IL 的工厂」，`document_il` 是「IL 本身 + 加工 IL 的车间」。

#### 4.3.2 核心流程

`_do_translate_single` 中能清楚看到二者的衔接顺序（先 new_parser 造 IL，再 document_il 的各阶段加工 IL）：

```
# 1) new_parser：PDF → IL
docs = parse_prepared_pdf_with_new_parser_to_legacy_ir(temp_pdf_path, ...)

# 2) document_il/midend 各阶段：在 docs 上依次加工
DetectScannedFile(...).process(docs, ...)
docs = LayoutParser(...).process(docs, doc_pdf2zh)
ParagraphFinder(...).process(docs)
StylesAndFormulas(...).process(docs)
AutomaticTermExtractor(...).procress(docs)
il_translator.translate(docs)

# 3) document_il/backend：IL → PDF
pdf_creater = PDFCreater(temp_pdf_path, docs, ...)
result = pdf_creater.write(translation_config)
```

#### 4.3.3 源码精读

`new_parser` 的解析入口（被 `_do_translate_single` 延迟导入并调用）：

[babeldoc/format/pdf/high_level.py:900-911](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L900-L911) —— 从 `babeldoc.format.pdf.new_parser.native_parse` 导入 `parse_prepared_pdf_with_new_parser_to_legacy_ir` 并调用，返回 `docs`（一个 IL `Document`）。这一行就是「Parsing 前端」与「主链路」的接缝。

`document_il` 的内部切分（按职责分四个子目录）：

| 子路径 | 职责 |
| --- | --- |
| `document_il/il_version_1.py` / `.rnc` / `.xsd` | IL 数据模型（由 schema 自动生成） |
| `document_il/xml_converter.py` | IL ↔ XML/JSON 序列化 |
| `document_il/frontend/` | IL 构建 sink（如 `il_creater_active.py` 的 `ActiveILCreater`，被 new_parser 调用） |
| `document_il/midend/` | IL 上各处理阶段（`detect_scanned_file` / `layout_parser` / `paragraph_finder` / `styles_and_formulas` / `il_translator` / `typesetting` …） |
| `document_il/backend/` | 渲染：`pdf_creater.py` 的 `PDFCreater` |
| `document_il/utils/` | 工具：`fontmap`、`spatial_analyzer`、`matrix_helper` 等 |

注意 `document_il/frontend/` 与 `new_parser/` 的协作：`new_parser` 负责读 PDF 内容流，`frontend/il_creater_active.py` 提供一个 `ActiveILCreater` 作为「接收端（sink）」，把解析出的字符/事件装配成 IL 对象。所以二者是「**驱动方 + 接收方**」的关系。

主链路中各 midend 阶段的真实调用顺序：

[babeldoc/format/pdf/high_level.py:938-1003](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L938-L1003) —— 这段代码就是 midend 的「执行清单」：DetectScannedFile → LayoutParser → TableParser(可选) → ParagraphFinder → StylesAndFormulas → AutomaticTermExtractor → ILTranslator/ILTranslatorLLMOnly。顺序与 `TRANSLATE_STAGES` 一致。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：用源码验证「new_parser 造 IL、document_il 加工 IL」的分工。
2. **操作步骤**：
   1. 在 `high_level.py:906` 确认 `docs` 来自 `new_parser`。
   2. 向下读到 `high_level.py:929`，看 `only_parse_generate_pdf` 模式如何跳过所有 midend 阶段、直接把 `docs` 交给 `PDFCreater`——这反证了「midend 阶段都是对 `docs` 的可选加工」。
   3. 打开 `babeldoc/format/pdf/new_parser/native_parse.py`，确认它的返回值就是 `document_il` 中的 `Document` 类型。
3. **需要观察的现象**：`docs` 这个变量贯穿整个 `_do_translate_single`，先是 new_parser 的产物，后被多个 midend 阶段原地修改。
4. **预期结果**：你能指出「想新增一个处理阶段，应该往 `document_il/midend/` 加文件，并在 `_do_translate_single` 里插入一次 `.process(docs)` 调用」。

#### 4.3.5 小练习与答案

**练习 1**：`document_il/frontend/` 既在 `document_il` 目录下，又跟 `new_parser` 协作，它到底属于哪一边？

> 答案：它属于 `document_il`（提供 IL 的构建器 `ActiveILCreater`），但被 `new_parser` 调用。可以理解为「IL 这边提供了接收解析结果的插座，new_parser 把插头插进来」。这样设计让 IL 模型与具体解析实现解耦。

**练习 2**：`--only-parse-generate-pdf`（u1-l2 见过）跳过了哪些目录的代码？

> 答案：跳过 `document_il/midend/` 的全部阶段（扫描检测、版面、段落、公式、翻译、排版都不跑），只走 `new_parser/`（造 IL）和 `document_il/backend/`（渲染 PDF）。见 [babeldoc/format/pdf/high_level.py:925-932](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L925-L932)。

---

### 4.4 docvision 与 translator 子包

#### 4.4.1 概念说明

这两个子包是 BabelDOC 设计上的「**可替换部件**」：

- `docvision/`：**视觉感知**——识别页面里哪里是文字、标题、图、表、公式。底层是 DocLayout-YOLO 模型。
- `translator/`：**语言翻译**——把文本从源语言翻成目标语言，底层是 OpenAI 兼容 API。

它们之所以独立成包，是因为二者都依赖外部资源（模型 / API），且都存在多种实现（本地 ONNX vs RPC、官方 OpenAI vs DeepSeek/GLM/Ollama），需要可替换。

#### 4.4.2 核心流程

在 `main()` 中能直接看到这两个部件被「装配」进 `TranslationConfig` 的过程：

```
# docvision：选本地 ONNX 还是 RPC
if args.rpc_doclayout:  from babeldoc.docvision.rpc_doclayout import RpcDocLayoutModel
...
else:                   doc_layout_model = DocLayoutModel.load_onnx()

# docvision：表格检测模型（可选）
if args.translate_table_text: from babeldoc.docvision.table_detection.rapidocr import RapidOCRModel
...
else: table_model = None

# translator：实例化 OpenAI 兼容翻译器 + QPS 限流
translator = OpenAITranslator(...)
set_translate_rate_limiter(args.qps)

# 装配进 config
config = TranslationConfig(doc_layout_model=..., table_model=..., translator=..., ...)
```

#### 4.4.3 源码精读

**docvision** 的本地默认实现与 RPC 备选：

[babeldoc/main.py:544-575](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L544-L575) —— 一长串 `elif args.rpc_doclayoutN` 分支，最后兜底 `from babeldoc.docvision.doclayout import DocLayoutModel; doc_layout_model = DocLayoutModel.load_onnx()`。这说明 `docvision/` 下有 `doclayout.py`（本地）和 `rpc_doclayout.py`～`rpc_doclayout8.py`（RPC 各版本），对应 `base_doclayout.py` 定义的统一基类，以及 `table_detection/rapidocr.py` 的表格模型。

**translator** 的实例化与限流：

[babeldoc/main.py:499-542](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L499-L542) —— 构造 `OpenAITranslator`，并可为「术语抽取」单独构造一个 translator；随后 `set_translate_rate_limiter(args.qps)` 设置全局 QPS 限流。`translator/` 包就两个核心文件：`translator.py`（`OpenAITranslator` + `BaseTranslator`）与 `cache.py`（peewee+SQLite 翻译缓存）。

> 说明：`OpenAITranslator` 内部如何调用 API、如何缓存、如何限流，留给 u6-l1 精讲；本讲只确认「它住在 `translator/` 包，且是一个可替换的部件」。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：确认 docvision 与 translator 都是「被装配进 config 的可替换部件」。
2. **操作步骤**：
   1. 在 `main.py:544-582` 跟踪 `doc_layout_model` 与 `table_model` 的来源（`docvision.doclayout` / `docvision.table_detection.rapidocr`）。
   2. 在 `main.py:503` 跟踪 `translator` 的来源（`babeldoc.translator.translator`）。
   3. 在 `main.py:678` 起的 `TranslationConfig(...)` 构造里，确认这三者都作为字段传入（`doc_layout_model=`、`table_model=`、`translator=`）。
3. **需要观察的现象**：这三个对象都来自顶层子包，且 `TranslationConfig` 像一个「装配盘」把它们收纳。
4. **预期结果**：你能说出「换版面模型只动 `docvision/`、换翻译服务只动 `translator/`，主链路 `high_level` 不用改」。

#### 4.4.5 小练习与答案

**练习 1**：`docvision/` 下为什么有 `rpc_doclayout.py` 到 `rpc_doclayout8.py` 这么多个版本？

> 答案：它们对应**不同版本的 RPC 版面服务协议**。客户端可以任选一个匹配的服务端（通过 `--rpc-doclayout` ~ `--rpc-doclayout7` 切换，见 [babeldoc/main.py:62-89](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L62-L89)）。多个版本并存是为了兼容服务端迭代，避免老客户端连不上新服务。

**练习 2**：`translator/` 包目前只内置了 OpenAI 兼容服务（u1-l2 提过）。如果要新增一个非 OpenAI 的翻译后端，应该改哪里？

> 答案：在 `babeldoc/translator/` 下新增一个继承 `BaseTranslator` 的翻译器类，并在 `main()` 的服务选择分支里增加装配逻辑。主链路 `high_level` 只通过 `TranslationConfig.translator` 拿到一个「能翻译的对象」，不关心它的具体后端——这正是把它独立成包的好处。

---

## 5. 综合实践

**任务**：手绘一张「从终端命令 `babeldoc` 到翻译真正开始」的调用链路图，标注每一跳的**函数名、源码文件、行号**。

要求覆盖以下所有跳点（你可以直接抄下面这条链，重点是能解释每一跳做了什么）：

```
终端: babeldoc
  └─ 入口点声明 pyproject.toml:67  (babeldoc = "babeldoc.main:cli")
     └─ main.cli()                     babeldoc/main.py:907
        └─ asyncio.run(main())         babeldoc/main.py:937
           └─ main()                   babeldoc/main.py:461
              ├─ create_parser()       babeldoc/main.py:32   (定义参数)
              ├─ OpenAITranslator(...) babeldoc/main.py:503  (装配 translator)
              ├─ DocLayoutModel.load_onnx()  babeldoc/main.py:575 (装配 docvision)
              └─ async for event in high_level.async_translate(config):  babeldoc/main.py:745
                 └─ high_level.async_translate(config)   babeldoc/format/pdf/high_level.py:299
                    └─ run_in_executor(do_translate, ...) babeldoc/format/pdf/high_level.py:361
                       └─ do_translate(pm, config)        babeldoc/format/pdf/high_level.py:527
                          └─ _do_translate_single(...)    babeldoc/format/pdf/high_level.py:836
                             ├─ new_parser: PDF → IL      high_level.py:906
                             ├─ document_il/midend 各阶段 high_level.py:938-1003
                             └─ PDFCreater(...).write()   high_level.py:929/后续
```

**操作步骤**：

1. 逐行对照源码确认每个函数的行号（用编辑器或 `grep -n` 打开 `main.py` 与 `high_level.py`）。
2. 在图上用三种颜色/标记区分：①CLI 壳（`cli`/`main`）、②编排层（`async_translate`/`do_translate`）、③真正主链路（`_do_translate_single` 内的 new_parser + midend + backend）。
3. 在图旁写一句话标注「跨文件边界」发生在哪一跳（答案：`main.py:745` 处，从 `main.py` 跳入 `high_level.py`）。

**预期结果**：完成后，当你下次想找「翻译在哪里真正开始」「参数在哪解析」「进度事件从哪 yield」，都能直接在图上定位到文件和行号。这张图也是后续 u2（整体架构与主流程）各讲的基础。

> 若本地无法运行命令，本实践为纯源码阅读型，标注「待本地验证」即可，不影响建立链路认知。

## 6. 本讲小结

- `babeldoc` 顶层目录按「一条主线（`format/pdf/`）+ 两个可换部件（`docvision/`、`translator/`）+ 底层支撑（`assets/`、`pdfminer/`、`babelpdf/`）」切分。
- 两条入口要分清：CLI 壳 `main.cli()`（终端用）与翻译编排入口 `high_level.async_translate()`（库 API，下游调用）。
- `new_parser/` 负责「PDF → IL」（造 IL），`document_il/` 负责「IL 模型 + 在 IL 上加工 + 渲染回 PDF」（含 `frontend`/`midend`/`backend`/`utils` 四个子目录）。
- `do_translate` 通过 `run_in_executor` 把同步主链路塞进线程池；真正一条龙在 `_do_translate_single`，顺序就是 `TRANSLATE_STAGES`。
- `docvision` 与 `translator` 都是「装配进 `TranslationConfig` 的可替换部件」，换它们不动主链路。
- 入口点 `babeldoc` 命令由 `pyproject.toml` 的 `[project.scripts]` 注册到 `babeldoc.main:cli`。

## 7. 下一步学习建议

本讲建立了「目录地图 + 两条入口」的认知。建议接下来：

- **u1-l4 配置体系**：精读 `translation_config.py` 中的 `TranslationConfig`，理解这个「装配盘」到底收纳了多少字段、CLI/TOML 如何汇聚到它。
- **u2-l1 三段式架构总览**：把本讲的 `TRANSLATE_STAGES` 与 frontend/midend/backend 三段式对应起来，建立全景图。
- **u2-l2 主流程编排**：精读 `do_translate` 与 `_do_translate_single`，把本讲里「点到为止」的 PDF 预处理、分片、元数据迁移讲透。

继续阅读的源码顺序建议：先 `translation_config.py`（u1-l4）→ 再回到 `high_level.py` 的 `_do_translate_single`（u2-l2）→ 最后挑一个 midend 阶段（如 `paragraph_finder.py`）入门 IL 处理细节。
