# 翻译主流程编排：do_translate 与 _do_translate_single

## 1. 本讲目标

在上一讲（u2-l1）里，我们建立了 **frontend / midend / backend** 三段式心智模型，并认识了 `TRANSLATE_STAGES` 这张「节目单」。本讲要钻进节目单的「导演」——`high_level.py` 中的两个核心函数，看它到底如何把一个 PDF 一段段地变成 mono/dual PDF。

读完本讲，你应该能够：

1. 说清 `do_translate` 在什么时候走「单文档直翻」、什么时候走「分片翻译」，以及各分片结果在哪里被合并。
2. 按真实执行顺序，背出 `_do_translate_single` 中各个 midend 阶段（DetectScannedFile → LayoutParser → ParagraphFinder → StylesAndFormulas → ILTranslator → Typesetting → PDFCreater）的调用次序与作用。
3. 理解 PDF 在被解析之前经历的一系列「修正」（`fix_null_xref` / `fix_filter` / `fix_media_box` 等）为什么要做。
4. 看懂翻译产物在落盘后又经历了哪些后处理（`fix_cmap` / `add_metadata` / `migrate_toc`）。

> 本讲承上：u2-l1 的三段式模型、u1-l4 的 `TranslationConfig`。启下：后续 u5（midend 各阶段源码）、u8-l2（分片合并的更多细节）。

## 2. 前置知识

在阅读本讲前，你需要具备以下概念（已在前面讲义建立）：

- **IL（Intermediate Language，中间表示）**：BabelDOC 在解析与渲染之间引入的「带坐标的对象树」。本讲会看到一个变量 `docs` 在多个阶段之间被反复「原地加工」。
- **`TranslationConfig`**：中心配置对象，整条流水线只读这一个 `config`（见 u1-l4）。
- **`TRANSLATE_STAGES`**：阶段全景表，每个元素是 `(stage_name, 相对耗时权重)`，顺序就是真实执行顺序。
- **PyMuPDF / `pymupdf.Document`**：BabelDOC 用来打开、修改、保存 PDF 的底层库。本讲里它出现的名字是 `Document`。
- **同步 vs 异步**：`do_translate` 本身是**同步**函数；`async_translate` 把它塞进线程池里跑（见 u2-l3）。本讲只聚焦同步主链路。

一个贯穿全讲的直觉：

> BabelDOC 的主流程可以理解成一个**三段式流水线 + 收尾打包**的过程。流水线内部，所有 midend 阶段都共用同一个 `docs`（IL 对象），每个阶段**读它、改它、再传给下一个**；流水线之外，还有一个「分片调度器」决定整份文档要不要拆成几段分别跑。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [babeldoc/format/pdf/high_level.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py) | 本讲主角。`do_translate`（分片调度）、`_do_translate_single`（单文档/单分片主链路）、PDF 预处理与后处理函数全部住在这里。 |
| [babeldoc/format/pdf/translation_config.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py) | `TranslationConfig` 配置中心与 `TranslateResult` 结果对象定义。 |
| [babeldoc/format/pdf/split_manager.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/split_manager.py) | `SplitManager` / `PageCountStrategy`：决定分片切分点。 |
| [babeldoc/format/pdf/result_merger.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/result_merger.py) | `ResultMerger`：把多个分片的 PDF 合并成最终结果。 |

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

- 4.1 `do_translate` 的分片调度逻辑（入口、分片/单文档分支、结果合并与收尾）。
- 4.2 `_do_translate_single` 的 midend 主链路（各阶段真实顺序）。
- 4.3 PDF 预处理与修正（解析前的「修 PDF」）。
- 4.4 元数据与 TOC 迁移（落盘后的「打包收尾」）。

### 4.1 do_translate：分片调度与收尾

#### 4.1.1 概念说明

`do_translate(pm, translation_config)` 是**同步翻译的总入口**。它不直接做翻译，而是扮演「调度员 + 包工头」：

- **调度员**：根据 `config.split_strategy` 决定整份文档是「一次跑完」还是「切成几片分别跑」。
- **包工头**：无论哪种方式，真正的脏活累活都委托给 `_do_translate_single`；`do_translate` 自己只负责切分、合并、统计、收尾和异常兜底。

一个关键设计：`do_translate` 的返回值是 `TranslateResult`，里面装着最终 mono/dual PDF 的路径。

#### 4.1.2 核心流程

```
do_translate(pm, config):
    1. 把 pm 挂到 config 上；记下 input_file；记录开始时间
    2. check_metadata(原始 PDF)  # 防止拿 BabelDOC 自己的产物再翻译
    3. 进入 MemoryMonitor（监控峰值内存）
       ├─ 若没有 split_strategy          → _do_translate_single（单文档直翻）
       ├─ SplitManager 算切分点
       │    ├─ 切分点为空 / 只有 1 个      → _do_translate_single（退化单文档）
       │    └─ 否则：串行处理每个分片：
       │         · 为每个分片复制 config（part_config）
       │         · 仅第 0 片做扫描检测 & 打水印
       │         · 各分片调用 _do_translate_single(part_monitor, part_config)
       │         · 用 ResultMerger.merge_results 合并各分片
       4. 记录 total_seconds、峰值内存、有效字符/token 统计
    5. 后处理：fix_cmap → add_metadata → migrate_toc
    6. pm.translate_done(result)；返回 result
    finally: pm.on_finish()；cleanup_temp_files()
```

#### 4.1.3 源码精读

**入口与元数据防护。** `do_translate` 一上来就把进度监视器挂到 config 上，并调用 `check_metadata` 防止「拿 BabelDOC 已经翻译过的 PDF 再翻译一次」（这会触发 `InputFileGeneratedByBabelDOCError`，异常体系详见 u8-l5）：

[high_level.py: L527-L545](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L527-L545) —— `do_translate` 函数签名、挂载 `progress_monitor`、`check_metadata` 防护与计时起点。

**分片/单文档分支。** 这是 `do_translate` 最核心的判断。没有 `split_strategy` 就直翻；有策略就用 `SplitManager` 算切分点，切分点为空或只有 1 个时仍退化为单文档：

[high_level.py: L547-L565](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L547-L565) —— 单文档 vs 分片的分支判断。

**分片循环的关键细节。** 真正进入分片循环后，有几个设计点非常值得注意：

[high_level.py: L574-L663](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L574-L663) —— 对每个 `split_point` 复制出 `part_config`、重算页码范围、把对应页抽到临时文档、各分片调用 `_do_translate_single`。

逐点解释其中最关键的几行：

- **复制 config 而非共享**（`part_config = copy.copy(translation_config)`，[L577](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L577)）：每个分片要有独立的 `pages`/`working_dir`/水印模式，但不能影响别的分片。
- **扫描检测只在第 0 片做**（`if i > 0: part_config.skip_scanned_detection = True`，[L600-L602](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L600-L602)）：整份文档是不是扫描件只需判一次。
- **水印只在第 0 片打**（`if i > 0: part_config.watermark_output_mode = WatermarkOutputMode.NoWatermark`，[L647-L651](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L647-L651)）：合并时只在首页留一份水印。
- **共享上下文必须跨分片**：`shared_context_cross_split_part` 用 `id()` 断言是同一个对象（[L611-L615](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L611-L615)）。它是跨分片的「全局记忆」，存了自动术语表、标题上下文、OCR 开关等——详见 u8-l2。

> 为什么 `WatermarkOutputMode.NoWatermark` 不会真的让最终 PDF 没水印？因为合并阶段（`ResultMerger`）只把第 0 片（带水印）的结果和其它分片（无水印）拼起来，最终首页仍带水印。`WatermarkOutputMode` 枚举见 [translation_config.py: L21-L24](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L21-L24)。

**结果合并。** 所有分片串行跑完后，`ResultMerger` 把 `results` 字典合并成单个 `TranslateResult`：

[high_level.py: L679-L682](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L679-L682) —— `start merge results` / `merge_results(results)` / `finish merge results`。

**统计与收尾。** 合并完成后，`do_translate` 把耗时、峰值内存、跨分片累计的有效字符数/token 数填进 `result`，然后依次执行后处理（后处理细节见 4.4），最后 `pm.translate_done(result)` 通知进度监视器完成：

[high_level.py: L685-L720](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L685-L720) —— 填充 `total_seconds`/统计、`fix_cmap`/`add_metadata`/`migrate_toc`、`translate_done`。

**异常与 finally。** 任何异常都被 `except` 捕获并通过 `pm.translate_error(e)` 上报；`finally` 块无论成功失败都会 `on_finish()` 并清理临时文件（`cleanup_temp_files` 会删除每个分片的工作目录）：

[high_level.py: L722-L733](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L722-L733) —— 异常上报与 `finally` 清理。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `do_translate` 的分片分支被触发，并理解「单文档直翻」是默认行为。

**操作步骤**：

1. 打开 [high_level.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py)，定位到 `do_translate`（L527）。
2. 准备一份多页 PDF（如仓库自带 `examples/ci/test.pdf`）。
3. 先用普通命令翻译一次，在日志里找 `start to translate:` 和 `finish translate:`，**确认没有** `Split points determined` 这一行的出现——因为默认 `split_strategy` 为 `None`。
4. 再用 `--max-pages-per-part N`（N 取一个小值，如 `2`）翻译，观察日志中出现 `Split points determined: K parts`（K 为分片数）和 `start merge results`。

**需要观察的现象**：

- 默认情况下日志里只有一次 `start to translate` / `finish translate`，说明走了 `_do_translate_single` 单文档分支。
- 加 `--max-pages-per-part` 后日志里出现 `Split points determined: K parts`、`start merge results`、`finish merge results`。

**预期结果**：分片数 K ≈ ⌈总页数 / N⌉。每个分片会各自生成工作目录 `part_i/`（见 [translation_config.py: L431-L440](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L431-L440) 的 `get_part_working_dir`），翻译完成后被 `cleanup_part_working_dir` 清掉。

> 待本地验证：`examples/ci/test.pdf` 的实际页数与切分后的分片数 K，请以你本地运行日志为准。

#### 4.1.5 小练习与答案

**练习 1**：`do_translate` 里有一处 `assert id(part_config.shared_context_cross_split_part) == id(translation_config.shared_context_cross_split_part)`。为什么用 `copy.copy(translation_config)`（浅拷贝）而不是 `copy.deepcopy`？

**答案**：浅拷贝只复制 `TranslationConfig` 对象自身，而 `shared_context_cross_split_part` 仍是同一个引用——这正是断言要保证的。跨分片需要共享同一份「全局记忆」（自动术语表、OCR 开关、标题上下文等），深拷贝会让每个分片各自维护一份，导致术语提取结果无法汇总、OCR 开关无法跨片生效。

**练习 2**：如果 `SplitManager.determine_split_points` 返回的列表长度恰好为 1，`do_translate` 会走分片合并逻辑吗？

**答案**：不会。代码在 [L562-L564](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L562-L564) 显式判断 `len(split_points) == 1` 时直接调用 `_do_translate_single`，避免「只有一片还要走合并」的无谓开销。

---

### 4.2 _do_translate_single：midend 主链路

#### 4.2.1 概念说明

`_do_translate_single(pm, translation_config)` 是「单文档或单个分片」的真正翻译函数。如果说 `do_translate` 是调度员，`_do_translate_single` 就是**流水线车间**：它打开 PDF、修 PDF、解析出 IL、依次跑各个 midend 阶段、最后渲染出 PDF。

这是整本手册最值得精读的函数之一，因为它把 u2-l1 里那张 `TRANSLATE_STAGES` 节目单**落到了真实代码行**上。

#### 4.2.2 核心流程

`_do_translate_single` 的执行顺序（与 `TRANSLATE_STAGES` 高度对齐）：

```
_do_translate_single(pm, config):
    0. OCR workaround 传播（来自 shared_context）
    1. （debug 模式）保存解压后的输入 PDF
    2. 打开 PDF + 预处理修正（见 4.3）
    3. parse_prepared_pdf_with_new_parser_to_legacy_ir(...)  →  docs (IL)   【frontend】
    4. only_parse_generate_pdf 快捷分支：直接 PDFCreater.write 返回
    5. DetectScannedFile.process(docs)                        【midend 开始】
    6. LayoutParser.process(docs)                              （close_process_pool）
    7. （若 table_model）TableParser.process(docs)              # 现已被废弃，恒为 None
    8. ParagraphFinder.process(docs)
    9. StylesAndFormulas.process(docs)
   10. （若支持 LLM 术语抽取 & auto_extract_glossary）AutomaticTermExtractor
   11. （若非 skip_translation）ILTranslator / ILTranslatorLLMOnly.translate(docs)
   12. （debug 模式）AddDebugInformation
   13. （Both 水印模式）生成首页水印 PDF
   14. Typesetting.typesetting_document(docs)
   15. PDFCreater(...).write(config)  →  result                【backend】
   16. 把首页水印合并进 mono/dual PDF
   返回 result
```

注意：步骤 5–11 都是「原地修改同一个 `docs`」，这正是 IL 中间表示「承上启下、全程复用」的体现。

#### 4.2.3 源码精读

**OCR 开关跨分片传播。** 函数开头把 `shared_context_cross_split_part.auto_enabled_ocr_workaround` 反向写回 config，保证第 0 片触发的 OCR 开关能在后续分片生效：

[high_level.py: L843-L845](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L843-L845) —— 把跨分片的 OCR 决定写回当前 config。

**frontend：PDF → IL。** 用 `new_parser` 解析出 IL 文档对象 `docs`。解析细节是 u4 的主题，这里只要知道它返回一个 `il_version_1.Document`：

[high_level.py: L906-L911](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L906-L911) —— `parse_prepared_pdf_with_new_parser_to_legacy_ir` 调用，产出 `docs`。

紧接着有一个「CID 字符过多」的健壮性检查——如果一页里超过 80% 的字符是 `(cid:N)` 形式（说明文本提取失败），直接抛 `ExtractTextError`：

[high_level.py: L922-L923](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L922-L923) —— `check_cid_char` 判定后抛错。

**快捷分支：only_parse_generate_pdf。** 如果用户只想「解析后直接重新生成 PDF」（不翻译），就跳过所有 midend 处理阶段，直接进 backend：

[high_level.py: L926-L932](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L926-L932) —— 跳过翻译处理，直接 `PDFCreater.write`。

这与 `get_translation_stage` 里 `only_parse_generate_pdf` 分支会剔除一大堆 stage 的行为是一致的（见 [L271-L283](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L271-L283)）。

**midend 阶段一：DetectScannedFile。** 判断是不是扫描件（详见 u5-l1）：

[high_level.py: L938-L950](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L938-L950) —— `DetectScannedFile(config).process(docs, temp_pdf_path, mediabox_data)`。

**midend 阶段二：LayoutParser。** 版面分析（DocLayout-YOLO），并在之后 `close_process_pool()` 关闭版面推理用的进程池：

[high_level.py: L953-L961](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L953-L961) —— `LayoutParser(config).process(docs, doc_pdf2zh)` 与 `close_process_pool()`。

**midend 阶段三：TableParser（条件执行）。** 只有 `config.table_model` 为真才跑。**注意**：`TranslationConfig.__init__` 已经把 `table_model` 强制清成 `None`（[translation_config.py: L326-L331](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L326-L331)），所以这一段实际不会执行——这是一个已退役的实验性阶段，详见 u5-l5：

[high_level.py: L963-L970](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L963-L970) —— `if config.table_model:` 守卫下的 `TableParser`。

**midend 阶段四：ParagraphFinder。** 把字符聚合成段落（详见 u5-l3）：

[high_level.py: L971-L977](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L971-L977) —— `ParagraphFinder(config).process(docs)`。

**midend 阶段五：StylesAndFormulas。** 识别公式与样式（详见 u5-l4）：

[high_level.py: L978-L984](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L978-L984) —— `StylesAndFormulas(config).process(docs)`。

**midend 阶段六：AutomaticTermExtractor（条件执行）。** 只有翻译器支持 LLM（`translator_supports_llm`）且开启了 `auto_extract_glossary` 才跑自动术语抽取（详见 u6-l4）。注意这里用的是 `get_term_extraction_translator()`，可以和正文翻译用不同的引擎：

[high_level.py: L986-L995](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L986-L995) —— 取出 `translate_engine` / `term_extraction_engine`，判断是否支持 LLM，调用 `AutomaticTermExtractor`。

**midend 阶段七：ILTranslator / ILTranslatorLLMOnly。** 真正的「翻译段落」阶段。这里有个关键的二选一逻辑：如果翻译器支持 LLM-only 翻译（实现了可用的 `do_llm_translate`），就用 `ILTranslatorLLMOnly`；否则退回经典的 `ILTranslator`。两者共用同一个 `stage_name = "Translate Paragraphs"`，所以在节目单上无差别：

[high_level.py: L997-L1007](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L997-L1007) —— 根据 `support_llm_translate` 选择翻译器并调用 `il_translator.translate(docs)`。

> `translator_supports_llm` 的判定方式值得一读：它实际调用 `translator.do_llm_translate(None)`，若抛 `NotImplementedError` 就认为不支持（[L246-L256](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L246-L256)）。这是一种「以行为探测能力」的鸭子类型判定。

**Typesetting（排版重排）。** 把译文逐字符贴回版面（详见 u7-l1）：

[high_level.py: L1038-L1044](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L1038-L1044) —— `Typesetting(config).typesetting_document(docs)`。

**backend：PDFCreater.write。** 把排版后的 IL 渲染成 mono/dual PDF（详见 u7-l3）：

[high_level.py: L1046-L1047](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L1046-L1047) —— `PDFCreater(temp_pdf_path, docs, config, mediabox_data).write(config)`。

**水印合并。** 若前面生成了「带水印首页」，这里把它合并进 mono/dual PDF 的第一页（`merge_watermark_doc`）；失败时回退到无水印版本：

[high_level.py: L1048-L1067](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L1048-L1067) —— mono/dual 首页水印合并与失败回退。

#### 4.2.4 代码实践

**实践目标**：把本讲规格里要求的「为每个 midend 阶段标注行号与作用」变成一张可查的对照表，并配合 `--debug` 实际观察每个阶段的 IL 中间产物。

**操作步骤**：

1. 在 `_do_translate_single`（[L836](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L836)）里，按下表逐行核对每个阶段对应的源码行号：

   | 阶段（stage_name） | 源码位置 | 作用 |
   | --- | --- | --- |
   | `DetectScannedFile` | [L942-L944](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L942-L944) | 用 SSIM 判断是否扫描件，必要时触发 OCR workaround |
   | `Parse Page Layout`（LayoutParser） | [L954](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L954) | DocLayout-YOLO 识别页面区域，写入 `PageLayout` |
   | `Parse Paragraphs`（ParagraphFinder） | [L971](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L971) | 把字符按版面聚合成段落 |
   | `Parse Formulas and Styles`（StylesAndFormulas） | [L978](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L978) | 字符级识别公式、合并重叠公式、处理段落样式 |
   | `Translate Paragraphs`（ILTranslator/LLMOnly） | [L999-L1003](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L999-L1003) | 用占位符+批处理+线程池翻译所有段落 |
   | `Typesetting` | [L1038](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L1038) | 把译文逐字符重排回原版面 |
   | PDFCreater（Generate drawing instructions） | [L1046-L1047](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L1046-L1047) | 把 IL 渲染成 mono/dual PDF |

2. 用 `--debug` 翻译一个 PDF：

   ```bash
   babeldoc --openai --openai-api-key <KEY> --files examples/ci/test.pdf --debug
   ```

3. 进入 debug 工作目录（默认在 `~/.cache/babeldoc/working/<文件名>/`，见 [translation_config.py: L287-L298](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L287-L298)），你会看到一组「阶段快照 JSON」：
   - `create_il.debug.json`（frontend 产物）
   - `detect_scanned_file.json`
   - `layout_generator.json`
   - `paragraph_finder.json`
   - `styles_and_formulas.json`
   - `il_translated.json`
   - `typsetting.json`

**需要观察的现象**：每个 JSON 对应一个 midend 阶段**之后**的 `docs` 快照。对比相邻两个 JSON（例如 `paragraph_finder.json` 与 `styles_and_formulas.json`），可以看到 IL 在原地被一步步加工。

**预期结果**：你能把上面那张表里的行号与磁盘上的 JSON 文件**一一对应**，从而在脑海里建立「代码行 → 阶段 → 落盘快照」的完整映射。

> 待本地验证：debug 目录的确切路径与是否生成全部 JSON，取决于你的 `--debug` 配置与翻译器是否支持 LLM（不支持则没有术语抽取相关产物）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ILTranslator` 和 `ILTranslatorLLMOnly` 在 `TRANSLATE_STAGES` 节目单里只占一项？

**答案**：因为两者的 `stage_name` 都是 `"Translate Paragraphs"`（见 il_translator.py:330 与 il_translator_llm_only.py:111），`TRANSLATE_STAGES` 用 `ILTranslator.stage_name` 引用。`_do_translate_single` 在运行时二选一，但对进度条而言它们是「同一个阶段」，进度权重 46.96 也是为这个阶段整体估的。

**练习 2**：`LayoutParser.process` 之后为什么紧跟着一个 `close_process_pool()`？

**答案**：版面分析用 DocLayout-YOLO ONNX 模型推理，可能依赖一个进程池（或多进程推理）。版面是整份文档只需做一次的重资源阶段，做完之后立即 `close_process_pool()` 释放进程池，避免它在后续翻译/渲染阶段继续占用资源。

**练习 3**：如果用户传了 `--skip-translation`，`_do_translate_single` 会跳过哪些阶段？最终还能产出 PDF 吗？

**答案**：会跳过 `ILTranslator.translate(docs)`（[L997-L1007](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L997-L1007) 的 `else` 分支只打印 `skip ILTranslator`），同时 `get_translation_stage` 也会把 `ILTranslator.stage_name` 从节目单剔除、`auto_extract_glossary` 被强制关掉。但 Typesetting 和 PDFCreater 仍会执行，所以仍能产出 PDF——只是 PDF 里是原文（未翻译）的版面重排结果。

---

### 4.3 PDF 预处理与修正

#### 4.3.1 概念说明

现实世界里的 PDF 五花八门：有的 xref 表里有 `null`、有的内容流用了 ASCII85/LZW 编码、有的 MediaBox 不规范、有的单页有多个内容流……这些「脏数据」会让 BabelDOC 内置的 pdfminer 解析器（frontend）解析失败。所以在解析之前，`_do_translate_single` 会先跑一组**修正函数**把 PDF「洗」一遍。

这套修正是「防御性」的：能修则修，修不了就 `logger.exception` 后继续——宁可多跑也不要因为个别坏页让整份文档失败。

#### 4.3.2 核心流程

```
打开 PDF：open_pdf_with_save_fallback(原始路径, temp_input.pdf)
   ├─ 先尝试 safe_save 重新保存（能暴露一些损坏）
   └─ 失败则 rebuild_pdf_by_inserting_pages（用 insert_pdf 重建）

预处理三连：
   fix_null_page_content  → 删除并重建内容为 null 的页
   fix_filter             → 展平内容流编码、合并单页多个内容流
   fix_null_xref          → 把 null 的 xref 对象替换成空数组、展开特殊 Filter、清空 Annots
   fix_media_box          → 规范化 MediaBox 为 [0 0 x1 y1]、清掉 CropBox/BleedBox/TrimBox/ArtBox

保存修正后的 PDF：save_pdf_with_same_path_fallback → doc_pdf2zh
```

#### 4.3.3 源码精读

**带回退地打开 PDF。** `open_pdf_with_save_fallback` 先尝试 `safe_save` 重存（重存过程能暴露损坏），失败则用「插入页」的方式重建整份文档：

[high_level.py: L120-L131](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L120-L131) —— 打开 PDF，保存失败时走 `rebuild_pdf_by_inserting_pages`。

其中 `safe_save` 是最基础的「两段保存」：先正常 `doc.save`，失败再用 `ez_save`（PyMuPDF 的宽松保存）：

[high_level.py: L88-L94](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L88-L94) —— `safe_save` 的两段尝试。

**`_do_translate_single` 中的预处理调用。** 注意它被包在 `try/except` 里，任何修正异常都只记录不中断：

[high_level.py: L868-L882](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L868-L882) —— `fix_null_page_content` / `fix_filter` / `fix_null_xref` / `fix_media_box` 的调用与 `save_pdf_with_same_path_fallback`。

**fix_null_page_content：** 检测哪些页的 `xref_object == "null"`，删掉后插入一个空白页占位：

[high_level.py: L441-L450](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L441-L450) —— 修复内容为 null 的页。

**fix_filter：** 把页面内容流的 Filter（如 `/ASCII85Decode`、`/LZWDecode`）展开成裸流，并把单页多个内容流合并成一个。展开特殊编码是为了让 pdfminer 不必自己实现这些解码器；合并多流是为了让解释器只面对一条线性的内容流：

[high_level.py: L476-L493](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L476-L493) —— `fix_filter` 的核心逻辑（注意函数里有一段 `return` 之后的旋转处理代码是**死代码**，不会执行）。

**fix_null_xref：** 遍历所有 xref 对象：`null` 的替换成空数组 `[]`，含 `/ASCII85Decode` 或 `/LZWDecode` 的展开裸流，含 `/Annots` 的把 `Annots` 置 null（避免注释干扰）：

[high_level.py: L453-L473](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L453-L473) —— `fix_null_xref` 逐对象修复。

**fix_media_box：** 把页面 MediaBox 规范成 `[0 0 x1 y1]`（左下角对齐到原点），并把 CropBox/BleedBox/TrimBox/ArtBox 全部置 null（统一用 MediaBox 作为页面边界）。返回的 `mediabox_data` 会在后面传给 `PDFCreater` 用于还原：

[high_level.py: L795-L820](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L795-L820) —— `fix_media_box` 规范化页面边界框。

> 为什么 mediabox_data 要留着传给 PDFCreater？因为渲染时要按原始页面尺寸画译文，而规范化时改写了边界框，需要这份记录来还原真实页面几何。这一步是 frontend 与 backend 之间的一根「暗线」。

#### 4.3.4 代码实践

**实践目标**：理解「修正」是解析前必不可少的步骤，并能指出每个修正函数解决哪一类坏 PDF。

**操作步骤**：

1. 用 `--debug` 翻译一个 PDF，找到工作目录中的 `input.decompressed.pdf`（[L848-L862](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L848-L862) 在 debug 模式下会保存解压后的输入）。
2. 用任意 PDF 工具（如 `mutool show`、`qpdf` 或 PyMuPDF 脚本）对比原始 PDF 与 `input.decompressed.pdf` 的内容流编码差异。
3. 阅读下面这张「故障 → 修正函数」对照表，确认你理解每个修正点：

   | PDF 病症 | 修正函数 | 源码位置 |
   | --- | --- | --- |
   | 整页内容流是 `null` | `fix_null_page_content` | [L441-L450](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L441-L450) |
   | xref 对象为 `null` / 特殊 Filter / 带 Annots | `fix_null_xref` | [L453-L473](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L453-L473) |
   | 内容流用了 ASCII85/LZW、单页多流 | `fix_filter` | [L476-L493](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L476-L493) |
   | MediaBox 不规范、带 CropBox 等子框 | `fix_media_box` | [L795-L820](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L795-L820) |
   | PDF 结构损坏、普通保存失败 | `open_pdf_with_save_fallback` / `rebuild_pdf_by_inserting_pages` | [L120-L131](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L120-L131) / [L97-L117](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L97-L117) |

**需要观察的现象**：`input.decompressed.pdf` 通常比原文件大（因为展开了解码、合并了流），但内容流变得线性可读。

**预期结果**：你能解释「为什么 BabelDOC 要在解析前重写一遍 PDF」——因为它的 frontend 解析器对裸流更友好，预处理把异构 PDF 统一成一种它能稳定消化的形态。

> 待本地验证：`input.decompressed.pdf` 仅在 `--debug` 模式生成；不同 PDF 的差异请以本地文件为准。

#### 4.3.5 小练习与答案

**练习 1**：`fix_filter` 函数末尾 `return` 之后还有一段处理页面旋转（Rotate）的代码，它会执行吗？

**答案**：不会。`return`（[L493](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L493)）之后的代码是注释里写的 `# skip rotate for now` 的死代码，Python 不会执行函数 `return` 之后的语句。这是一个被临时禁用、尚未删除的功能残留。

**练习 2**：为什么 `fix_media_box` 要把 CropBox/BleedBox/TrimBox/ArtBox 全部置 null？

**答案**：这些子框都会影响「页面实际可见区域」。如果它们存在且与 MediaBox 不一致，frontend 解析和 backend 渲染可能用不同的坐标基准，导致译文贴错位置。统一置 null 后，全流程都只认 MediaBox 这一个边界，坐标系统一。

---

### 4.4 元数据与 TOC 迁移

#### 4.4.1 概念说明

`PDFCreater.write` 产出 PDF 之后，`do_translate` 还会做三件「打包收尾」的事：

1. **`fix_cmap`**：对每个输出 PDF 重建 CMap（字符映射），保证译文里的 CJK 字符能被复制/搜索。
2. **`add_metadata`**：写入特殊的 producer/creator 元数据，**标记这是 BabelDOC 生成的 PDF**——这既是一种「水印/署名」，也是 4.1 里 `check_metadata` 防「二次翻译」的依据。
3. **`migrate_toc`**：把原文的书签目录（TOC）迁移到译文 PDF，保留可点击的目录结构。

这三步都是**在最终 PDF 路径上原地改写**，且都带异常兜底——任何一个失败都不影响已经生成的 PDF。

#### 4.4.2 核心流程

```
do_translate 收尾阶段：
   fix_cmap(result, config)       # 对 mono/dual 各 PDF 调 reproduce_cmap
   add_metadata(result, config)   # 写 producer = "BabelDOC<ver>_<time>_Translation_generated_by_AI,please_carefully_discern"
   migrate_toc(config, result)    # 把原文 TOC set_toc 到 dual PDF（交替页模式跳过）
   pm.translate_done(result)
```

#### 4.4.3 源码精读

**check_metadata（防二次翻译）。** 这是入口处的守卫，与 `add_metadata` 写入的标记配套：如果 PDF 的 producer 里同时含 `BabelDOC` 和 `Translation_generated_by_AI,please_carefully_discern`，说明这是 BabelDOC 的产物，拒绝再次翻译：

[high_level.py: L157-L169](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L157-L169) —— `check_metadata` 检测 producer 标记并抛 `InputFileGeneratedByBabelDOCError`。

**add_metadata（写署名标记）。** 遍历 `result` 里的 4 个 PDF 路径（mono/dual × 有水印/无水印），对每个去重后写入 producer。关键字符串是：

\[ \texttt{producer} = \texttt{"BabelDOC"} \,\|\, \text{WATERMARK\_VERSION} \,\|\, \text{时间戳} \,\|\, \texttt{"Translation\_generated\_by\_AI,please\_carefully\_discern"} \]

同时它还会用正则 `[\uD800-\uDFFF]` 把元数据里的 surrogate（代理区）字符删掉，避免 PyMuPDF 写入非法 Unicode：

[high_level.py: L172-L213](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L172-L213) —— `add_metadata` 写 producer/creator 并清理 surrogate 字符。

**fix_cmap（重建字符映射）。** 对每个输出 PDF 调用 `reproduce_cmap`（从 backend 导入）：

[high_level.py: L216-L233](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L216-L233) —— `fix_cmap` 遍历输出 PDF 调 `reproduce_cmap`。

**migrate_toc（迁移目录）。** 先对原文档跑一次 `fix_filter` + `fix_null_xref` 以便能读出 TOC，再用 `old_doc.get_toc()` 取出目录，最后 `set_toc` 到 dual PDF。注意：

- `use_alternating_pages_dual`（交替页双语）模式会**跳过** TOC 迁移，因为交替页的页码与原文不对应。
- 只迁移到 `dual_pdf_path` 和 `no_watermark_dual_pdf_path`（mono 的相关行被注释掉了）。
- 保存走的是 `PDFCreater.save_pdf_with_timeout`（带超时保护的子进程保存，详见 u7-l3）。

[high_level.py: L736-L791](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L736-L791) —— `migrate_toc` 完整逻辑。

> 一个小瑕疵（供你练读源码）：[L761](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L761) 写的是 `for i in len(old_doc)`，`len(...)` 返回整数不可迭代——这段代码只在 `only_include_translated_page` 为真时才会触发，正常路径走不到，但确实是个潜在 bug。读源码时要能识别这种「主路径正确、边路有坑」的情况。

**do_translate 里三步的调用顺序。** 注意它们都在 `do_translate` 的主 try 块尾部，`migrate_toc` 还被单独包了 `try/except`，失败只记日志不中断：

[high_level.py: L711-L719](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L711-L719) —— `fix_cmap` → `add_metadata` → `migrate_toc` → `translate_done`。

#### 4.4.4 代码实践

**实践目标**：亲眼看到 `add_metadata` 写入的标记，并验证 `check_metadata` 能据此拒绝二次翻译。

**操作步骤**：

1. 正常翻译一个 PDF，得到 `*.mono.pdf` 和 `*.dual.pdf`。
2. 用一段最小 Python 脚本读取产物的元数据：

   ```python
   # 示例代码：读取 BabelDOC 产物的 producer 标记
   import pymupdf
   doc = pymupdf.open("xxx.dual.pdf")
   print(doc.metadata.get("producer"))
   ```

3. 再用 BabelDOC 翻译这个 `*.dual.pdf`，观察是否被拒绝。

**需要观察的现象**：

- 步骤 2 打印的 producer 里应包含 `BabelDOC` 和 `Translation_generated_by_AI,please_carefully_discern`。
- 步骤 3 应抛出 `InputFileGeneratedByBabelDOCError`，日志出现 `input file ... is generated by BabelDOC`。

**预期结果**：成功复现「写入标记 → 检测标记 → 拒绝二次翻译」的闭环，证明 4.1 的入口守卫与 4.4 的署名写入是配套设计。

> 待本地验证：具体文件名以你本地输出为准。

#### 4.4.5 小练习与答案

**练习 1**：`add_metadata` 为什么要用正则 `re.sub(r"[\uD800-\uDFFF]", "", v)` 清理 surrogate 字符？

**答案**：surrogate 区（U+D800–U+DFFF）是 UTF-16 用来编码辅助平面字符的代理对半位，单独出现是非法 Unicode。某些原始 PDF 的元数据里可能混入孤立 surrogate，PyMuPDF 写入时会报错或产生损坏的 PDF。提前删掉这些字符能保证 `set_metadata` 稳定成功。

**练习 2**：`migrate_toc` 为什么在 `use_alternating_pages_dual` 模式下跳过？

**答案**：交替页双语模式是「原文页 + 译文页」交替排列，译文 PDF 的页数是原文的 2 倍，页码与原文不再一一对应。原文 TOC 里指向「第 N 页」的条目在新 PDF 里会指错位置，所以索性跳过，避免目录跳转到错误的页。

---

## 5. 综合实践

把本讲的「调度 → 主链路 → 预处理 → 收尾」串起来，完成下面这个端到端的源码追踪任务。

**任务**：为 `do_translate` → `_do_translate_single` 这条主链路画一张完整的「执行时序图」，要求：

1. **横向分层**：`do_translate（调度层）` / `_do_translate_single（车间层）` / `PDF 文件系统`。
2. **纵向时间轴**从上到下，按真实顺序标出以下关键节点（每条都要带上源码行号）：
   - `check_metadata`（[L535](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L535)）
   - 分片判断（[L547-L565](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L547-L565)）
   - 打开 PDF + 预处理四连（[L866-L882](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L866-L882)）
   - 解析出 IL（[L906](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L906)）
   - 7 个 midend 阶段（见 4.2.4 的表）
   - `PDFCreater.write`（[L1047](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L1047)）落盘
   - 收尾三步 `fix_cmap`/`add_metadata`/`migrate_toc`（[L711-L714](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L711-L714)）
3. 在 `PDF 文件系统`那一层，标出每个时间点磁盘上出现的文件（`input.pdf`、`create_il.debug.json`、`*.mono.pdf`、`*.dual.pdf` 等）。
4. 用虚线标出「分片模式」下哪些节点会重复执行、哪些只在第 0 片执行（扫描检测、水印）。

**验收标准**：拿你画的图，对照 `--debug` 实际运行产生的日志和 JSON 文件，能做到「图上每个节点都能在日志/磁盘上找到对应物」。

> 这是一个源码阅读型综合实践，不需要你修改任何代码。重点是建立「代码行 ↔ 运行时行为 ↔ 磁盘产物」的三重对应。

## 6. 本讲小结

- `do_translate` 是**同步翻译总入口**，扮演调度员：根据 `split_strategy` 决定单文档直翻还是分片串行；分片时各片复制 config、仅第 0 片做扫描检测与水印，最后用 `ResultMerger` 合并。
- `_do_translate_single` 是**单文档/单分片的车间**：打开+修 PDF → 解析出 IL → 依次跑 DetectScannedFile/LayoutParser/ParagraphFinder/StylesAndFormulas/AutomaticTermExtractor/ILTranslator/Typesetting → `PDFCreater.write` 出 PDF。所有 midend 阶段共用同一个 `docs`（IL）原地加工。
- midend 阶段的**真实执行顺序**就是 `TRANSLATE_STAGES` 的顺序；`ILTranslator` 与 `ILTranslatorLLMOnly` 二选一但共用 stage_name；`only_parse_generate_pdf` 与 `skip_translation` 会短路掉部分阶段。
- PDF 在被解析前要经历一组**防御性修正**（`fix_null_page_content`/`fix_filter`/`fix_null_xref`/`fix_media_box`），把异构 PDF 统一成 frontend 能稳定消化的形态；`mediabox_data` 会传给 backend 用于还原页面几何。
- 落盘后还有三步**收尾**：`fix_cmap`（重建 CMap）、`add_metadata`（写入署名标记）、`migrate_toc`（迁移原文目录）；其中 `add_metadata` 写入的标记正是入口 `check_metadata` 防止二次翻译的依据。
- 整条链路处处带**异常兜底**：预处理、水印、TOC 迁移失败都只记日志不中断，保证「能产出 PDF」的健壮性优先。

## 7. 下一步学习建议

本讲建立了主流程的「骨架」，接下来的学习建议沿骨架逐节深入：

1. **想深入 midend 某个阶段**：进入第 5 单元。建议先读 u5-l1（DetectScannedFile，最简单）、u5-l2（LayoutParser，涉及 ONNX 模型），再读 u5-l3（ParagraphFinder）和 u5-l4（StylesAndFormulas）。
2. **想深入翻译机制**：第 6 单元。本讲里「ILTranslator/ILTranslatorLLMOnly 二选一」「AutomaticTermExtractor 条件执行」只是入口，u6-l1 到 u6-l3 会讲清占位符、批处理、优先级线程池和质量校验回退。
3. **想深入分片与异步**：u8-l2（SplitManager/ResultMerger 细节）、u8-l3（`async_translate` 如何把同步 `do_translate` 桥接成异步事件流）。本讲的 `shared_context_cross_split_part` 在 u8-l2 会有完整解释。
4. **想深入 PDF 后端**：u7-l3（PDFCreater）。本讲里反复出现的 `mediabox_data`、`save_pdf_with_timeout`、`reproduce_cmap` 都在那里。

建议的阅读顺序：u5（midend 各阶段）→ u6（翻译）→ u7（渲染后端）→ u8（工程化）。每读一个阶段，回过头在 `_do_translate_single` 里定位它对应的行号，你会对这条主链路越来越熟。
