# 异常体系与健壮性处理

## 1. 本讲目标

学完本讲，你应当能够：

- 看懂 BabelDOC 的异常定义文件 `babeldoc_exception/BabelDOCException.py`，并说清「四类异常分别在哪里被抛出、各自代表什么故障」。
- 理解翻译主链路里那段密集的「PDF 修正三件套」(`fix_null_page_content` / `fix_filter` / `fix_null_xref` / `fix_media_box`)为什么存在、它们各自修什么、失败后怎么收场。
- 掌握保存容错链路：`safe_save` → `rebuild_pdf_by_inserting_pages` → `open_pdf_with_save_fallback` → `save_pdf_with_same_path_fallback` 这四级回退是如何「保住产物」的。
- 认识翻译失败时的回退与「软失败」策略，包括 `disable_same_text_fallback` 配置开关、`ContentFilterError` 的单段跳过，以及异常如何最终变成 CLI 上的 `error` 事件。

本讲对应流水线的「防护层」，不改变 u2-l2 讲过的三段式主链路，而是回答一个工程问题：**当输入 PDF 残缺、保存失败、译文质量异常时，BabelDOC 如何既不崩溃、又能给出可读的诊断。**

## 2. 前置知识

在阅读本讲前，请确保你已经掌握：

- **三段式架构与主链路**（u2-l1、u2-l2）：frontend 把 PDF 解析成 IL，midend 原地加工 IL，backend 把 IL 渲染回 PDF；`_do_translate_single` 是真正的「车间」。
- **TranslationConfig 中心配置**（u1-l4）：流水线全程只读这一个对象，健壮性开关（如 `disable_same_text_fallback`）也挂在它上面。
- **PyMuPDF（pymupdf）Document**：BabelDOC 用它打开、修改、保存 PDF。本讲大量出现的 `doc.xref_object`、`doc.update_object`、`doc.save`、`doc.ez_save` 都是 PyMuPDF 的 API。
- **PDF 对象模型基础**（u4-l2）：xref（交叉引用表）、对象（`null` / 数组 / 字典）、内容流（Contents）等概念。本讲的「修正」就是针对这些底层结构的修补。

如果你对「扫描检测会抛 `ScannedPDFError`」还想深入，可对照 u5-l1；本讲只在异常体系里点名它，不重复其判定逻辑。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [babeldoc/babeldoc_exception/BabelDOCException.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/babeldoc_exception/BabelDOCException.py) | 定义 BabelDOC 的全部自定义异常（四个类）。 |
| [babeldoc/babeldoc_exception/\_\_init\_\_.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/babeldoc_exception/__init__.py) | 空文件（0 行）。异常不从此包导出，而是直接从 `BabelDOCException` 模块导入。 |
| [babeldoc/format/pdf/high_level.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py) | 主战场：异常抛出点、PDF 修正函数、保存容错函数、错误事件都在这里。 |
| [babeldoc/format/pdf/document_il/midend/il_translator.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py) | 翻译阶段的「软失败」：单段翻译异常被捕获、跳过而不中断全文。 |
| [babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py) | LLM-only 模式的质量校验三关，受 `disable_same_text_fallback` 控制。 |
| [babeldoc/format/pdf/translation_config.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py) | `disable_same_text_fallback` 配置项的定义与默认值。 |
| [babeldoc/main.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py) | CLI 层把 `error` 事件转成日志输出。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：异常体系、PDF 修正与容错、保存回退策略、翻译回退策略。它们共同构成一条「**尽力修、尽量保、保不住就软失败、软失败也不行才硬报错**」的防线。

### 4.1 异常体系

#### 4.1.1 概念说明

> 先纠正一个容易望文生义的点：文件/模块名叫 `BabelDOCException`，但代码里**并没有一个名为 `BabelDOCException` 的基类**。这只是一个「异常仓库」模块，里面四个类全都直接继承自 Python 内置的 `Exception`，彼此之间没有父子关系。

这种设计意味着：你不能用 `except BabelDOCException` 一网打尽所有 BabelDOC 异常，而必须逐个捕获（或捕获基类 `Exception`）。模块之所以这样命名，是历史习惯——它确实是「BabelDOC 专属异常」的集合地。

四个异常按「表示什么故障」分类：

| 异常类 | 含义 | 是否带额外属性 |
| --- | --- | --- |
| `ScannedPDFError` | 输入是扫描件且未开启 OCR workaround，无法正常翻译 | 否 |
| `ExtractTextError` | 解析出的 IL 里 CID 字符过多（字体没解开，提取不到真正文本） | 否 |
| `InputFileGeneratedByBabelDOCError` | 输入 PDF 已经被 BabelDOC 翻译过，拒绝二次翻译 | 否 |
| `ContentFilterError` | LLM 上游返回「内容违规/被过滤」（如阿里云 DashScope 的敏感内容拦截） | 是，有 `.message` |

注意第四个与众不同：只有 `ContentFilterError` 在 `__init__` 里额外存了 `self.message = message`，方便下游不依赖 `str(e)` 也能拿到原始提示。

#### 4.1.2 核心流程

四类异常的「抛出 → 上浮 → 落地」走向各不相同：

```text
ScannedPDFError
  detect_scanned_file.py: 扫描页占比 > 80% 且未开 OCR workaround
    └─ 抛出 → 上浮到 do_translate 外层 except → translate_error(e) → error 事件

ExtractTextError
  high_level.py: check_cid_char(docs) 为真（CID 字符占比 > 80%）
    └─ 抛出 → 上浮到 do_translate 外层 except → translate_error(e) → error 事件

InputFileGeneratedByBabelDOCError
  high_level.py: check_metadata 发现输入 PDF 的 producer 含 BabelDOC 署名标记
    └─ do_translate 内单独捕获并 re-raise → 上浮到外层 except → error 事件

ContentFilterError
  translator.py: do_llm_translate 把上游 BadRequestError 的敏感内容消息转换而来
    └─ 在 il_translator._translate_one 内被捕获 → 该段跳过(add_content_filter_hint) → 不中断
```

一句话总结：前三个是**致命错误**（直接终止本次翻译），第四个是**可恢复错误**（只影响单段，其余段落照常翻译）。

#### 4.1.3 源码精读

异常的定义极其简洁——这是全部内容：

[babeldoc/babeldoc_exception/BabelDOCException.py:1-19](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/babeldoc_exception/BabelDOCException.py#L1-L19) 定义四个异常类，全部直接继承 `Exception`，`ContentFilterError` 额外保存 `self.message`。

注意 `__init__.py` 是空文件，所以下游一律用「模块名.类名」的方式导入，例如主链路里这两行：

[babeldoc/format/pdf/high_level.py:20-23](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L20-L23) 直接从 `BabelDOCException` 模块导入 `ExtractTextError` 与 `InputFileGeneratedByBabelDOCError`（而非从包 `__init__` 导入）。

四个抛出点散布在不同位置。`InputFileGeneratedByBabelDOCError` 的抛出在「元数据守卫」函数里：

[babeldoc/format/pdf/high_level.py:157-169](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L157-L169) `check_metadata` 读取 PDF 的 `producer` 字段，若同时包含字符串 `"BabelDOC"` 与 `"Translation_generated_by_AI,please_carefully_discern"`，判定为「BabelDOC 自产文件」并抛出异常。这个判定字符串正是 4.3 节 `add_metadata` 写进去的「防伪标记」，二者构成一对「写标记 / 查标记」的闭环。

`ExtractTextError` 的抛出紧接 IL 解析完成之后：

[babeldoc/format/pdf/high_level.py:823-833](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L823-L833) `check_cid_char` 统计形如 `(cid:123)` 的字符，当其数量超过全部字符的 \(0.8\)（即 80%）时返回 `True`。

[babeldoc/format/pdf/high_level.py:922-923](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L922-L923) 一旦 `check_cid_char(docs)` 为真，立刻抛 `ExtractTextError`——这说明字体没解开、提取到的全是占位符，再翻译也没有意义。

`ScannedPDFError` 的真实抛出点在扫描检测阶段（u5-l1 详讲过判定逻辑，这里只看抛出）：

[babeldoc/format/pdf/document_il/midend/detect_scanned_file.py:137-142](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/detect_scanned_file.py#L137-L142) 当扫描页占比超过 80% 且**没有**开启 `auto_enable_ocr_workaround` 时抛出 `ScannedPDFError`。

> 小坑提醒：[high_level.py:898](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L898) 有一处被注释掉的 `raise ScannedPDFError`，那是已退役的「快速预检」残留，**不是**当前生效的抛出点。真正生效的是 `detect_scanned_file.py:142`。

最后看致命异常如何「落地」为事件。`do_translate` 最外层用一个统一的 `try/except` 兜住一切：

[babeldoc/format/pdf/high_level.py:722-733](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L722-L733) 捕获任意异常 → 记日志 → 调 `pm.translate_error(e)`（把异常转成 `error` 事件）→ `raise` 向上抛；`finally` 块无条件执行 `pm.on_finish()` 与 `cleanup_temp_files()`，保证「哪怕失败也要清理临时文件、解除事件循环阻塞」。

`error` 事件的形态在 `async_translate` 的文档字符串里写明：

[babeldoc/format/pdf/high_level.py:332-335](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L332-L335) `error` 事件只含 `type` 与 `error`（异常的字符串形式）两个字段。

#### 4.1.4 代码实践

**实践目标**：在不联网、不调 LLM 的前提下，亲手验证两类致命异常的触发条件。

**操作步骤**：

1. 用任意工具（或上一个 BabelDOC 产物）准备一个 PDF，用 PyMuPDF 读它的 `producer` 元数据，确认它**不含** BabelDOC 标记，预期 `check_metadata` 不抛异常。
2. 写一段最小 Python（**示例代码**，非项目原有代码）：

   ```python
   from pymupdf import Document
   from babeldoc.format.pdf.high_level import check_metadata
   from babeldoc.babeldoc_exception.BabelDOCException import (
       InputFileGeneratedByBabelDOCError,
   )

   pdf = Document("你的文件.pdf")
   print("producer =", pdf.metadata.get("producer"))
   try:
       check_metadata(pdf)
       print("通过：非 BabelDOC 自产文件，可以翻译")
   except InputFileGeneratedByBabelDOCError as e:
       print("拦截：", e)
   ```

3. 再准备一个**已被 BabelDOC 翻译过**的 PDF（其 `producer` 会含 `BabelDOC...Translation_generated_by_AI,please_carefully_discern`），重复步骤 2。

**需要观察的现象**：

- 第 1 个文件：`producer` 为空或普通值（如 `LaTeX with hyperref`），打印「通过」。
- 第 2 个文件：`producer` 含 BabelDOC 标记，进入 `except` 分支，打印「拦截」及完整英文提示。

**预期结果**：你能复现 `InputFileGeneratedByBabelDOCError` 的抛出与捕获。`ExtractTextError` / `ScannedPDFError` 需要特殊构造的 PDF（CID 占比 >80% 或扫描页 >80%），较难手工触发，可标注「待本地验证」后通过阅读源码理解条件。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能用 `except BabelDOCException` 一次捕获 BabelDOC 的所有自定义异常？

> **参考答案**：因为代码里根本没有 `BabelDOCException` 这个基类，四个异常各自直接继承 `Exception`。`BabelDOCException` 只是**模块文件名**。要统一捕获只能用 `except Exception`（但那会顺手吞掉其他异常，不推荐），或显式列出四个类。

**练习 2**：四个异常里哪一个带有 `.message` 属性？为什么偏偏是它？

> **参考答案**：`ContentFilterError`。因为它携带的是 LLM 上游「内容过滤」的中文提示（如阿里云 DashScope 的敏感内容消息），下游 `il_translator._translate_one` 需要把它作为提示注入译文（`add_content_filter_hint`），所以单独存一份原始 `message` 比依赖 `str(e)` 更可靠。

**练习 3**：`do_translate` 在异常发生时如何保证事件循环不卡死？

> **参考答案**：靠 `finally` 块（high_level.py:730-733）无条件调用 `pm.on_finish()`。`on_finish` 会置位 `finish_event`，从而让 `async_translate` 里 `await finish_event.wait()` 解除阻塞；同时 `cleanup_temp_files()` 清理临时文件。

---

### 4.2 PDF 修正与容错

#### 4.2.1 概念说明

PDF 是一种容错性很差的格式：不同生成器（LaTeX、Word、扫描软件、各种 OCR 工具）写出的 PDF 五花八门，常见残缺包括——某些 xref 对象是 `null`、内容流用多段拼接且带间接引用的 `Filter`、`MediaBox` 原点不为 `(0,0)`、还挂着会干扰渲染的 `Annots`（批注）。PyMuPDF 和 pdfminer（BabelDOC 内部 vendor 了它）对这类残缺的容忍度不同，直接解析常常报错或拿到错位坐标。

BabelDOC 的对策是「**先修再用**」：在解析成 IL **之前**，对 `Document` 跑一遍修正三件套，把 PDF 调整到解析器和后端都「吃得下」的形态。修正被包在 `try/except` 里——**修不好也不中断**，只记一条 `auto fix failed` 日志继续走，体现「尽力而为」的工程取向。

#### 4.2.2 核心流程

主链路 `_do_translate_single` 里，修正发生在「打开 PDF」与「解析成 IL」之间：

```text
open_pdf_with_save_fallback(原始路径, 临时路径)      # 4.3 节：带保存探针地打开
  ↓ 得到 doc_pdf2zh
try:
    fix_null_page_content(doc_pdf2zh)   # 删除并重建「页面对象为 null」的空页
    fix_filter(doc_pdf2zh)              # 展开间接引用的 Filter、合并多段内容流
    fix_null_xref(doc_pdf2zh)           # 把 null 对象换成 []、展开 ASCII85/LZW 流、清空 Annots
except Exception:
    logger.exception("auto fix failed, please check the pdf file")   # 修不好也继续
  ↓
fix_media_box(doc_pdf2zh)               # 把 MediaBox 原点拉回 (0,0)，清掉 CropBox 等，返回原始几何供后端还原
  ↓
save_pdf_with_same_path_fallback(...)   # 4.3 节：避免覆盖正在读的输入文件
  ↓
parse ... → docs(IL) → check_cid_char → ...
```

同样的修正三件套还在另外两处被复用：`migrate_toc` 迁移目录前（对原始文件修一遍以便 `get_toc` 能读）和 `debug` 模式保存解压副本前。可见这是一个被反复调用的「标准化预处理」。

#### 4.2.3 源码精读

先看「逐 xref 扫一遍」的 `fix_null_xref`，它是修正的核心：

[babeldoc/format/pdf/high_level.py:453-473](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L453-L473) 遍历所有 xref 对象，分类处理：`null` → 替换成空数组 `[]`；含 `/ASCII85Decode` 或 `/LZWDecode` → 把流展开重写（注释写明是「让 pdfminer 高兴」）；含 `/Annots` → 把批注键置 null；任何分支抛错 → 一律降级为 `update_object(i, "[]")`。

`fix_filter` 负责内容流的「展平与合并」：

[babeldoc/format/pdf/high_level.py:476-493](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L476-L493) 对每个页面内容流：若 `Filter` 是间接引用（`xref` 类型），就把流字节读出并原地展开重写；若一页有多段内容流，就把它们拼接成一段新的、用空格连接的内容流，并把页面的 `Contents` 指向这个新对象。注意第 494 行有 `return`，其后关于 `Rotate` 旋转的处理被跳过（保留代码但不执行）。

`fix_null_page_content` 处理「整页对象为 null」的极端情况：

[babeldoc/format/pdf/high_level.py:441-450](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L441-L450) 找出 xref 对象为 `"null"` 的页，先删后插，用一个空白页占位，避免后续按页索引时崩溃。

`fix_media_box` 修正页面几何，且**有返回值**——这点很关键：

[babeldoc/format/pdf/high_level.py:795-820](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L795-L820) 把每页 `MediaBox` 规范成 `[0 0 x1 y1]`（原点拉回左下角），同时把 `CropBox/BleedBox/TrimBox/ArtBox` 置 null 并把它们的原始值收进 `box_set`，最后以 `{xref: box_set}` 字典返回。这个 `mediabox_data` 会被透传给 `PDFCreater`（后端）用于**还原真实裁剪框**——因为修正时抹掉了它们，渲染时必须按记录恢复，否则译文会画错位置。文件顶部那句注释 `# mediabox -> '[0 nul 792]'` 就是在描述被修掉的真实病态样例。

最后看主链路如何「包着 try 调用」这套修正：

[babeldoc/format/pdf/high_level.py:866-877](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L866-L877) 用 `open_pdf_with_save_fallback` 打开后，依次跑 `fix_null_page_content` → `fix_filter` → `fix_null_xref`（包在 `try/except` 里，失败只记日志），再跑 `fix_media_box` 取回 `mediabox_data`。

`migrate_toc` 里也有完全一致的「修正三件套 + try/except」片段，对原始输入文件再修一遍以提取目录：

[babeldoc/format/pdf/high_level.py:745-749](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L745-L749) 迁移 TOC 前对 `old_doc` 跑 `fix_filter` 与 `fix_null_xref`，失败则 `logger.exception("auto fix failed, please check the pdf file")`。

#### 4.2.4 代码实践

**实践目标**：理解 `fix_media_box`「抹掉又记录」的双面性，以及修正失败的软着陆。

**操作步骤**：

1. 阅读上面的 [fix_media_box 源码](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L795-L820)，回答：它对 `CropBox` 做了哪两件相反的事？
2. 用 `--debug` 模式翻译一个 PDF：

   ```bash
   babeldoc --openai --openai-api-key sk-xxx --files examples/ci/test.pdf --debug
   ```

3. 在工作目录找到 `input.decompressed.pdf`（这是 debug 模式下「修正 + 解压后保存」的副本，对应 high_level.py:848-862）。

**需要观察的现象**：

- debug 模式下，修正三件套在 `_do_translate_single` 入口处对 `doc_input` 跑了一遍（[L856-L858](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L856-L858)），随后 `safe_save(doc_input, output_path, expand=True, pretty=True)`（[L861](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L861)）把修正结果落盘。
- 如果日志里出现 `auto fix failed, please check the pdf file`，说明该 PDF 触发了某个修正分支的异常，但翻译仍会继续（因为包在 try/except 里）。

**预期结果**：

- 对 `CropBox` 的两件相反事：①读取其原始值存进 `mediabox_data`；②把 PDF 里的 `CropBox` 键置 null。原因是 BabelDOC 想用统一的 `MediaBox` 做几何计算，但又得在渲染时恢复真实裁剪框，所以「先记后抹、按需还原」。

> 若本地无法联网或无 API key，可只做步骤 1、2 的源码阅读部分，跳过实际翻译，标注「待本地验证」实际产物路径。

#### 4.2.5 小练习与答案

**练习 1**：`fix_null_xref` 里对 `/ASCII85Decode` 和 `/LZWDecode` 的处理为什么注释说是「让 pdfminer 高兴」？

> **参考答案**：BabelDOC 内部 vendor 了 pdfminer 用于解析。某些 PDF 的这两种编码流，pdfminer 解码会失败或拿到乱码；这里用 PyMuPDF（`xref_stream` + `update_stream`）把流提前展开成原始字节，相当于替 pdfminer 把难啃的编码预处理掉，从而「让它高兴」。

**练习 2**：修正三件套为什么必须包在 `try/except` 里、且失败后继续执行？

> **参考答案**：因为修正针对的是「不可控的第三方 PDF」，某条修正分支对某个病态对象抛错是常态。如果让修正异常直接中断主链路，就会「因为修不好而彻底放弃翻译」，与「尽力而为」的取向相悖。包住它、记日志、继续走，最差也只是个别对象未被修正，解析器再自行容错。

**练习 3**：`fix_media_box` 返回的 `mediabox_data` 最终被谁消费？

> **参考答案**：被传给 `PDFCreater`（后端）。因为 `fix_media_box` 把 `CropBox/BleedBox/TrimBox/ArtBox` 都抹成了 null，后端渲染时需要依据这份记录把真实裁剪框还原回去，否则译文字符的坐标与裁剪会错位。

---

### 4.3 保存回退策略

#### 4.3.1 概念说明

「修正」解决的是「PDF 内容残缺」，「保存回退」解决的是另一个顽疾：**PyMuPDF 把修好的 `Document` 写回磁盘时仍可能失败**。典型场景有二：

1. **保存本身就失败**：某些 PDF 内部对象引用坏得太彻底，`doc.save()` 直接抛异常。
2. **输入输出是同一个文件**：翻译时输入路径与输出路径相同（或重叠），而 `Document` 还开着输入文件，直接覆盖写入会冲突或损坏。

BabelDOC 用四级函数层层兜底：`safe_save`（普通保存失败就换 `ez_save`）→ `rebuild_pdf_by_inserting_pages`（连保存都做不到就「抽页重建」）→ `open_pdf_with_save_fallback`（打开时先做一次保存探针）→ `save_pdf_with_same_path_fallback`（输入输出同路径时先写临时文件再替换）。

#### 4.3.2 核心流程

```text
safe_save(doc, path)               # 最底层：try doc.save() → except → doc.ez_save()
   ↑ 被以下三个上层函数复用

open_pdf_with_save_fallback(原始, 输出):
   Document(原始)                  # 打开
   try: safe_save(doc, 输出)        # 用保存当「探针」验证可写性
        return doc                 # 通过 → 直接返回这个 doc
   except:
        doc.close()
        rebuild_pdf_by_inserting_pages(原始, 输出)   # 抽页重建
        return Document(输出)      # 返回重建后的新 doc

save_pdf_with_same_path_fallback(doc, 输出):
   if 输入路径 != 输出路径:
        safe_save(doc, 输出); return doc          # 不同文件，直接写
   else:
        safe_save(doc, 输出.saved)                # 同文件 → 先写 .saved 临时
        doc.close()
        .saved 替换 输出                            # 再原子替换
        return Document(输出)
```

`rebuild_pdf_by_inserting_pages` 的「抽页重建」思路尤其巧妙：既然原 `Document` 写不回去，就新建一个空 `Document`，用 `insert_pdf` 把每一页「搬运」进来（这一步 PyMuPDF 会重新组织对象表），再保存——绕过原文件损坏的对象结构。

#### 4.3.3 源码精读

最底层的 `safe_save` 只有两步，但它是整条容错链的地基：

[babeldoc/format/pdf/high_level.py:88-94](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L88-L94) 先 `doc.save()`，失败则退到 `doc.ez_save()`（注释说明 `ez_save` 内部用 `garbage=3`，能容忍「对象缺失」这类问题）。

「抽页重建」的完整实现：

[babeldoc/format/pdf/high_level.py:97-117](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L97-L117) 新建空 `Document`，`insert_pdf(source_doc)` 把源文档逐页搬入，保存到 `.rebuilt` 临时文件，`finally` 里关闭两个 doc，最后用 `.rebuilt` 原子替换目标路径。

带保存探针的打开函数：

[babeldoc/format/pdf/high_level.py:120-131](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L120-L131) 打开后立刻 `safe_save` 探测可写性；探针失败就 `close` 再走 `rebuild_pdf_by_inserting_pages`，最终返回重建出的新 `Document`。主链路正是用它拿到稳健的 `doc_pdf2zh`（见 [L866](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L866)）。

处理「同路径写入」的函数：

[babeldoc/format/pdf/high_level.py:134-154](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L134-L154) 比较输入名与输出名的绝对路径；不同则直接 `safe_save`；相同则先写到 `.saved` 临时文件，`finally` 关闭原 doc，再用 `.saved` 替换目标——避免边读边写同一个文件导致损坏。

这套保存容错并不孤立，它贯穿后处理。例如 `add_metadata`（写防伪标记）和 `fix_cmap`（重生成 ToUnicode）都走 `safe_save` + 临时文件 + `shutil.move` 的模式：

[babeldoc/format/pdf/high_level.py:216-233](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L216-L233) `fix_cmap` 对每个产物 PDF 打开、`reproduce_cmap`、`safe_save` 到临时文件、再 `shutil.move` 覆盖回去——和 `add_metadata`（[L172-L213](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L172-L213)）的写法完全一致。

防伪标记的内容由 `add_metadata` 写入，`producer` 字段形如 `BabelDOC{WATERMARK_VERSION}_{时间戳}_Translation_generated_by_AI,please_carefully_discern`：

[babeldoc/format/pdf/high_level.py:200-204](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L200-L204) 拼出 `translated_by` 字符串写入 `producer`。其中 `WATERMARK_VERSION` 在 `const.py` 里取 git 描述或版本号：

[babeldoc/const.py:31-40](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/const.py#L31-L40) 优先用 `git describe --always`，失败则退化为 `v{__version__}`。这正是 4.1 节 `check_metadata` 判定的那个标记来源——`add_metadata` 写、`check_metadata` 读，二者字符串必须严格对应。

#### 4.3.4 代码实践

**实践目标**：复现「输入输出同路径」时的安全写入，并观察 `.saved` 临时文件的生命周期。

**操作步骤**：

1. 阅读上面的 [save_pdf_with_same_path_fallback](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L134-L154) 源码。
2. 用 BabelDOC 翻译一个 PDF，**故意把 `--output` 指向与输入相同的目录且同名**（如果 CLI 允许），或在源码层面跟踪：`_do_translate_single` 在 [L882](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L882) 调用 `save_pdf_with_same_path_fallback(doc_pdf2zh, temp_pdf_path)`。
3. 观察工作目录中是否瞬时出现 `*.saved.pdf` 文件再被替换消失。

**需要观察的现象**：

- 当 `temp_pdf_path` 与 `doc.name` 解析为同一绝对路径时，会走「先写 `.saved`、关闭原 doc、再替换」分支；否则直接 `safe_save`。
- `.saved` 文件是临时态，替换后即被覆盖/删除。

**预期结果**：你能说清为什么同路径写入不能直接 `doc.save(同一路径)`——因为 `Document` 还持有输入文件的句柄，直接覆盖会冲突甚至写出半损坏的文件。若 CLI 不便构造同路径场景，可标注「待本地验证」后纯源码阅读完成本实践。

#### 4.3.5 小练习与答案

**练习 1**：`safe_save` 失败时为什么选 `ez_save` 而不是再 `save` 一次？

> **参考答案**：`save()` 失败通常是因为 PDF 内部对象缺失/损坏，重试 `save()` 结果不变。`ez_save()` 内部带 `garbage=3`，会做对象表清理与缺失对象补全，对「对象缺失」类问题更宽容，所以是有意义的「降级」而非重复。

**练习 2**：`rebuild_pdf_by_inserting_pages` 为什么能绕过原文件「写不回去」的问题？

> **参考答案**：它不修改原 `Document`，而是新建一个空 `Document`，用 `insert_pdf` 把源页逐页搬入。搬运过程会重新生成干净的对象表与 xref，等于「换了一个容器重新装一遍」，从而绕开原文件损坏的对象结构。最后用 `.rebuilt` 原子替换目标路径。

**练习 3**：`add_metadata` 写入的 `producer` 字符串，哪一段是 `check_metadata` 用来判定「自产文件」的关键？

> **参考答案**：`check_metadata` 要求 `producer` 同时包含 `"BabelDOC"` 与 `"Translation_generated_by_AI,please_carefully_discern"` 两个子串。前者来自 `translated_by` 开头的 `BabelDOC`，后者来自 `add_metadata` 固定拼接的标记段。二者都命中才会拦截。

---

### 4.4 翻译回退策略

#### 4.4.1 概念说明

前三个模块处理的是「输入与文件层面」的健壮性，本模块处理「翻译过程层面」的健壮性。翻译是最容易出错的环节：LLM 可能超时、返回乱码、命中上游内容过滤、或者干脆「原样吐回」（翻译失败却假装成功）。如果每一段翻译出错都终止整篇文档，用户体验会极差。

BabelDOC 的翻译回退分三个层次：

1. **单段软失败**：`ILTranslator` 在翻译单个段落时，把 `ContentFilterError` 与其他异常都捕获，**只跳过这一段**，其余段落继续。这是一段也不浪费的「局部容错」。
2. **质量校验三关**（LLM-only 模式）：译文若「与原文相同 / token 比例离谱 / 编辑距离过小」会被判为劣质而丢弃，触发回退重译或落到占位符。
3. **整批回退**：LLM-only 模式整批请求失败时，回退到内嵌的 `ILTranslator` 重译（u6-l3 已详讲，本讲只点其与 `disable_same_text_fallback` 的关系）。

> 注意区分：本讲的「翻译回退」与 u6-l1 讲的「翻译缓存」不同——缓存是「相同输入不重复请求 LLM」，回退是「请求失败/质量差时如何不中断」。二者在 `BaseTranslator.translate` 模板方法里协作：缓存命中直接返回、不耗限流配额；未命中才走限流→翻译→写缓存。

#### 4.4.2 核心流程

```text
ILTranslator._translate_one(段落):
   try:
       translated_text = translate_engine.translate(...)   # 内部：查缓存→限流→调LLM→写缓存
       post_translate_paragraph(...)                        # 还原占位符、写回 IL
   except ContentFilterError as e:
       logger.warning(...); add_content_filter_hint(page, 段落); return   # 内容过滤：跳过本段、加提示
   except Exception as e:
       logger.exception(...); return                                      # 其他错误：记日志、跳过本段
   # 注意：return 后主循环继续处理下一段，整篇不中断

ILTranslatorLLMOnly（批量）质量校验三关（任一失败→该条标错、continue，最终可能回退到 ILTranslator）:
   关1: 译文==原文 且 input_token>10 且 not disable_same_text_fallback   → fallback（丢弃）
   关2: not (0.3 < output_token/input_token < 3)                        → 太长/太短，丢弃
   关3: （not disable_same_text_fallback）编辑距离<5 且 input_token>20    → 几乎没变，丢弃
```

关键设计：三关里**关 2（token 比例）永远生效**，而关 1、关 3 受 `disable_same_text_fallback` 控制——某些语对（如中文→文言化中文）译文本就与原文高度相似，开启该开关可避免「正常的相似译文」被误判丢弃。

#### 4.4.3 源码精读

单段软失败的核心捕获块：

[babeldoc/format/pdf/document_il/midend/il_translator.py:1269-1278](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L1269-L1278) 捕获 `ContentFilterError` 时调 `add_content_filter_hint`（把过滤提示注入译文）后 `return`；捕获其他 `Exception` 时 `logger.exception` 记录后也 `return`——两者都「只跳过本段、不中断」，注释明确写着 `# ignore error and continue`。

LLM-only 模式的「译文与原文相同」判定（关 1）：

[babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py:768-781](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L768-L781) 当 `same_as_input` 为真、输入 token 超过 10、且 `not disable_same_text_fallback` 三者同时成立时，标记 `set_error_message("...fallback")` 并 `continue` 丢弃该译文。

「编辑距离过小」判定（关 3），同样受开关控制：

[babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py:793-804](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L793-L804) 仅当 `not disable_same_text_fallback` 时，才计算 `Levenshtein.distance`；若距离 <5 且输入 token >20，则视为「几乎没翻译」并丢弃。注意关 2（token 比例，[L783-L791](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L783-L791)）**不受**该开关影响，永远生效。

`disable_same_text_fallback` 的定义与默认值：

[babeldoc/format/pdf/translation_config.py:219](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L219) 构造参数 `disable_same_text_fallback: bool = False`，在 [L378](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L378) 赋给 `self.disable_same_text_fallback`。默认关闭（即默认启用「同文回退」保护）。

CLI 层如何把这个参数透传进 config：

[babeldoc/main.py:712](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L712) `disable_same_text_fallback=args.disable_same_text_fallback`，对应 README 里 `disable_same_text_fallback = false` 的 TOML 选项。

最后，致命的翻译异常（如 `ExtractTextError`）通过事件流落到 CLI：

[babeldoc/main.py:749-751](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L749-L751) 消费 `async_translate` 事件流时，遇到 `type == "error"` 就 `logger.error(f"Error: {event['error']}")` 并 `break`。这正是 4.1 节 `do_translate` 外层 `except` → `pm.translate_error(e)` 产生的那个事件。

#### 4.4.4 代码实践

**实践目标**：通过开关 `disable_same_text_fallback`，观察质量校验关 1、关 3 的行为差异。

**操作步骤**：

1. 用一个支持 LLM-only 模式的翻译引擎（如官方 OpenAI）翻译同一个 PDF 两次，分别用默认（`disable_same_text_fallback=false`）和 `--disable-same-text-fallback`（或 TOML 设为 `true`）：

   ```bash
   # 默认（启用同文回退保护）
   babeldoc --openai --openai-api-key sk-xxx --files input.pdf --debug
   # 关闭同文回退保护
   babeldoc --openai --openai-api-key sk-xxx --files input.pdf --debug \
            --disable-same-text-fallback
   ```

2. 对照 `il_translated.json`（debug 产物），统计两次中 `set_error_message` 含 `fallback` 或 `edit distance is too small` 的段落数量差异。

**需要观察的现象**：

- 默认情况下，凡是「译文≈原文」或「编辑距离<5」的段落都会被判为劣质而丢弃/回退，日志与 debug JSON 里能看到相应 warning。
- 开启 `--disable-same-text-fallback` 后，这类判定被跳过（关 1、关 3 关闭），但 token 比例异常（关 2）仍会拦截。

**预期结果**：你能解释开关只影响「相似性相关」的两关，而 token 比例这关始终生效——这是为了在「译文本就该相似」与「翻译质量过差」之间取得平衡。实际段落数与日志措辞「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`ILTranslator._translate_one` 捕获 `Exception` 后为什么选择 `return` 而不是 `raise`？

> **参考答案**：因为单段翻译失败（超时、LLM 报错等）不应连累整篇文档。`return` 让主循环跳过这一段继续翻译下一段，实现「局部容错」。只有解析层（`ExtractTextError`）或扫描层（`ScannedPDFError`）这类「整篇都无意义」的错误才向上抛出终止。

**练习 2**：`disable_same_text_fallback` 为 `True` 时，质量校验三关里哪一关仍然生效？为什么？

> **参考答案**：关 2（token 比例 ∉ (0.3, 3)）仍然生效。因为即使译文本该与原文相似，也不该出现「长度差 10 倍」这种极端情况——这几乎一定是 LLM 出错（截断、复读、返回了别的内容），与语对无关，所以无条件拦截。

**练习 3**：`ContentFilterError` 被捕获后调用的 `add_content_filter_hint` 做了什么？

> **参考答案**：它把「内容被上游过滤」的提示信息（`e.message`）注入到该段落所在页面/段落的译文中，让读者知道此处因内容审核未翻译，而不是翻译器静默失败。这样既不中断流程，又给了可读的诊断信息。

---

## 5. 综合实践

**任务**：复现「防二次翻译」闭环，并把本讲四个模块串起来解释。

**背景**：BabelDOC 在每次翻译产物里写入 `producer` 防伪标记，下次若拿这个产物当输入，会触发 `InputFileGeneratedByBabelDOCError` 拒绝翻译。这个闭环横跨异常体系（4.1）、保存回退（4.3）两个模块，是理解整套健壮性设计的最佳切入点。

**操作步骤**：

1. **首次翻译**：用 BabelDOC 正常翻译一个 PDF，得到 `*-mono.pdf` / `*-dual.pdf`（命名以实际输出为准）。

   ```bash
   babeldoc --openai --openai-api-key sk-xxx \
            --files examples/ci/test.pdf \
            --output ./out
   ```

2. **检查防伪标记**：用 PyMuPDF 读产物 PDF 的 `producer`，确认含 `BabelDOC` 与 `Translation_generated_by_AI,please_carefully_discern`（由 [add_metadata](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L172-L213) 写入）。

3. **二次翻译**：把上一步的产物 PDF 当输入再翻译一次：

   ```bash
   babeldoc --openai --openai-api-key sk-xxx --files ./out/*.dual.pdf
   ```

4. **观察错误事件**：预期 CLI 输出形如 `Error: Input file is generated by BabelDOC, Cannot translate files that have already been translated.`（来自 [check_metadata](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L157-L169) 抛出的异常，经 [do_translate 外层 except](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L536-L540) 与 [L722-L728](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L722-L728) 转成 `error` 事件，最后由 [main.py:749-751](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L749-L751) 打印）。

**需要解释（串联四模块）**：

- **异常体系**：这是 `InputFileGeneratedByBabelDOCError`，致命错误，直接终止翻译。
- **保存回退**：首次翻译时 `add_metadata` 用 `safe_save` + 临时文件 + `shutil.move` 把标记写进产物；这层容错保证「写标记」本身不会因 PDF 残缺而失败。
- **PDF 修正与容错**：即便产物 PDF 经历了修正三件套，`producer` 标记仍保留——因为标记写在元数据字典里，修正只动 xref/内容流/几何框，不碰元数据。
- **设计意图**：防二次翻译避免「译文套译文」的质量崩坏，也避免把双语 PDF 再次塞进解析器产生错乱的 IL（嵌套水印、重复字符层）。`check_metadata` 与 `add_metadata` 一写一读，构成自洽的防护闭环。

**预期结果**：你能用一张图说清「`add_metadata` 写标记（首次）→ 产物携带标记 → `check_metadata` 读标记（二次）→ 抛异常 → error 事件 → CLI 报错」这条完整链路，并指出每一步对应的源码行号。若本地无 API key，可只做步骤 2、4 的源码追踪部分，标注「待本地验证」。

## 6. 本讲小结

- **异常没有共同基类**：`BabelDOCException.py` 是异常仓库而非基类，四个异常（`ScannedPDFError`/`ExtractTextError`/`InputFileGeneratedByBabelDOCError`/`ContentFilterError`）各自直接继承 `Exception`，`__init__.py` 为空，需从模块直接导入。
- **PDF 修正三件套**：`fix_null_page_content`/`fix_filter`/`fix_null_xref` 在解析前把残缺 PDF 标准化，`fix_media_box` 规范几何并返回 `mediabox_data` 供后端还原；全部包在 `try/except` 里，修不好也继续。
- **保存四级回退**：`safe_save`（save→ez_save）→ `rebuild_pdf_by_inserting_pages`（抽页重建）→ `open_pdf_with_save_fallback`（保存探针）→ `save_pdf_with_same_path_fallback`（同路径先写临时再替换），核心目标是「保住产物」。
- **防二次翻译闭环**：`add_metadata` 写入 `producer` 防伪标记，`check_metadata` 读取并命中即抛 `InputFileGeneratedByBabelDOCError`，二者字符串严格对应。
- **翻译软失败**：`ILTranslator` 单段异常被捕获后只跳过本段不中断；LLM-only 质量校验三关里 token 比例永远生效，「同文」与「编辑距离」两关受 `disable_same_text_fallback` 控制。
- **致命异常落地为事件**：`do_translate` 外层 `except` → `pm.translate_error(e)` 产生 `error` 事件，`finally` 无条件 `on_finish`+清理，CLI 据 `type=="error"` 打印并退出。

## 7. 下一步学习建议

- **若想深入扫描/提取类致命错误**：回到 u5-l1 精读 `DetectScannedFile`，理解 `ScannedPDFError` 抛出前的 SSIM 判定与 OCR workaround 交互。
- **若想深入翻译回退与质量校验**：阅读 u6-l2（`ILTranslator` 占位符与线程池）、u6-l3（`ILTranslatorLLMOnly` 的跨页处理与整批回退到 `ILTranslator`），本讲的「翻译回退」是其工程兜底视角的补充。
- **若想深入后端健壮性**：阅读 u7-l3（`PDFCreater`），那里有另一套「子进程 + 超时 + 回退」模式（`subset_fonts_in_subprocess`、`save_pdf_with_timeout`），与本讲的 `safe_save` 思路一脉相承。
- **若想理解事件流如何承载错误**：结合 u2-l3（异步翻译与进度事件流）与 u8-l3（异步 API 内部实现），看 `cancel_event`/`finish_event` 如何与 `on_finish` 配合保证异常下也能优雅退出。
