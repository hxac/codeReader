# 解析入口与整体流程：PDF 如何变成 IL

> 本讲属于「PDF 解析前端：new_parser」单元（u4），承接 [u3-l1 IL 数据模型](u3-l1-il-data-model.md)。
> 上一讲我们把 IL 这棵「带坐标的对象树」拆开看了：`Document → Page → PdfCharacter / PdfParagraph / PdfCurve / PdfFont …`。
> 这一讲我们追问一个最自然的问题：**这棵 IL 树是从哪里长出来的？**——也就是 frontend（解析前端）如何把一个 PDF 文件「变成」一个 `il_version_1.Document`。
> 本讲只看「整体流程」，不钻进 PDF 对象语法和内容流操作符的细节（那是 u4-l2、u4-l3 的事）。你要带走的是一张**从 PDF 路径到 `Document` 的调用链地图**。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 frontend 的产品入口函数 `parse_prepared_pdf_with_new_parser_to_legacy_ir` 主要做了哪几步，它和「独立会话式」入口有何区别。
- 理解「prepared page（准备好的页）」是什么：PDF 被打开后，每一页如何被预先装载成解析器能消费的中间形态。
- 认识「page interpreter（页解释器）」与「text run positioner（文本串定位器）」各自的职责，以及它们如何配合。
- 理解 `ActiveILCreater` 作为 **sink（汇/接收器）** 的作用：它接收一串回调事件，逐个「投影」成 IL 实体，最终 `create_il()` 产出一棵 `Document`。
- 在源码里跟踪出一条从 PDF 路径到 `Document` 的关键函数调用顺序。

## 2. 前置知识

### 2.1 回顾：frontend 在三段式里的位置

在 [u2-l1](u2-l1-three-stage-architecture.md) 我们建立了三段式心智模型：**frontend（解析）/ midend（处理）/ backend（渲染）**。frontend 的输入是 PDF 字节，输出是一个 `il_version_1.Document`；之后 midend 和 backend 全程复用这一个 `docs` 变量。所以 frontend 是「造 IL」的地方，本讲的主角就是它。

frontend 的真正入口是**懒加载**的，藏在主流程 `_do_translate_single` 内部（见 [u2-l2](u2-l2-main-pipeline-orchestration.md)）。在 [high_level.py:900-911](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L900-L911) 里，函数内部 import 并调用 `parse_prepared_pdf_with_new_parser_to_legacy_ir(...)`，返回值赋给 `docs`——这一行就是「PDF → IL」的接缝：

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

### 2.2 名字里的「legacy IR」是什么

你会反复看到一个长长的函数名 `..._to_legacy_ir`。这里的 **legacy IR** 指的就是 `il_version_1`（见 [u3-l1](u3-l1-il-data-model.md)）这套「老的、对外的中间表示」。new_parser 内部其实有一套更细的对象模型（`PreparedPdfPage`、`PageResourceBundle`、各种 event），但最终都要「投影」回这套 legacy IR，因为下游 midend/backend 只认 `il_version_1`。**所以这个函数名可以读作：用 new_parser 把 PDF 解析后，转换成对外那套 IL。**

### 2.3 三个反复出现的设计词汇

源码里会反复出现这几个词，先记住它们的角色：

| 词汇 | 通俗解释 |
|---|---|
| **sink（汇/接收器）** | 解析过程会不断「发生事件」（开始一页、遇到一个字符、遇到一条曲线……）。sink 就是这些事件的目的地，负责把事件收下来、攒成 IL。本讲里 sink = `ActiveILCreater`。 |
| **prepared page（准备好的页）** | 把 PDF 一页里「解析要用到的原料」（内容流字节、资源树、裁剪框等）提前打包好，供解释器按需读取。 |
| **event（事件）/ project（投影）** | 解释器把 PDF 内容流操作符翻译成一串**事件**；sink 收到事件后，把它**投影**成对应的 IL 实体（字符→`PdfCharacter`，曲线→`PdfCurve`……）。 |

> 一个直觉比喻：PDF 内容流像一段「乐谱」，解释器是「演奏者」逐个音符吹出**事件**（声音），sink 是「录音师」把声音收下来录成一张**唱片**（IL）。本讲只关心「演奏→录音」这条流水线，不抠每个音符怎么吹。

## 3. 本讲源码地图

本讲聚焦「解析整体流程」，涉及的关键文件如下：

| 文件 | 作用 |
|---|---|
| [`babeldoc/format/pdf/new_parser/native_parse.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_parse.py) | frontend 的入口函数集合。**主角是 `parse_prepared_pdf_with_new_parser_to_legacy_ir`**，产品主流程调用的就是它。 |
| [`babeldoc/format/pdf/new_parser/pymupdf_prepared_page_access.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/pymupdf_prepared_page_access.py) | 用 PyMuPDF 打开 PDF、把每页装载成 `PreparedPdfPage` 的上下文管理器 `load_prepared_pdf_pages`。 |
| [`babeldoc/format/pdf/new_parser/prepared_page_execution.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/prepared_page_execution.py) | 逐页驱动解释器、向 sink 发回调的循环 `run_prepared_pages`。 |
| [`babeldoc/format/pdf/new_parser/native_page_interpreter.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_page_interpreter.py) | 「页解释器」`create_native_page_interpreter`：把一页交给底层解释器并触发 sink 回调。 |
| [`babeldoc/format/pdf/new_parser/text_positioning.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/text_positioning.py) | 「文本串定位器」`NativeTextRunPositioner`：把一段文本事件展开成带坐标的字形。 |
| [`babeldoc/format/pdf/document_il/frontend/il_creater_active.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py) | **sink 本体** `ActiveILCreater`：收事件、投影成 IL，最后 `create_il()` 返回 `Document`。 |

---

## 4. 核心概念与源码讲解

### 4.1 解析入口函数：两条调用路径

#### 4.1.1 概念说明

`native_parse.py` 里其实有**一族**入口函数，容易让人看花眼。理解的关键是分清两条路径：

1. **产品路径**（主流程用的）：`parse_prepared_pdf_with_new_parser_to_legacy_ir(temp_pdf_path, *, config, doc_pdf)`。它复用调用方已经准备好的东西——`TranslationConfig`、进度监视器、已落盘的临时 PDF、已打开的 PyMuPDF 文档。`high_level._do_translate_single` 调的就是它。

2. **独立会话路径**（库/脚本/测试用的）：`parse_with_new_parser_to_legacy_ir(pdf_path, ...)`。它从零开始：自己开 PDF、自己建配置、自己跑一整个解析会话（`run_active_parse_session`）。适合脱离主流程单独解析一个 PDF。

本讲以**产品路径**为主线，因为它才是「PDF → IL」在真实翻译流程里走的路。但两条路径殊途同归——都是「装页 → 解释 → 喂给 sink → sink 产出 Document」。

#### 4.1.2 核心流程

产品入口做了**四件事**，可以记成「造 sink → 装页 → 造解释器 → 跑页」：

```
parse_prepared_pdf_with_new_parser_to_legacy_ir(temp_pdf_path, config, doc_pdf)
 │
 ├─ ① sink = ActiveILCreater(config)          # 造一个 IL 接收器
 ├─    sink.mupdf = doc_pdf                    # 把已打开的 PyMuPDF 文档交给 sink（读字体要用）
 ├─ ② resource_runtime = create_active_font_resource_runtime()   # 字体资源运行时
 ├─ ③ with load_prepared_pdf_pages(temp_pdf_path, ...) as prepared_pages:
 │        page_interpreter = create_native_page_interpreter(sink, positioner, resource_runtime, config)
 ├─ ④ return run_prepared_pages(sink, ..., prepared_pages, page_interpreter)  # 逐页跑，返回 Document
```

注意两个小细节：
- `resource_runtime`（字体资源运行时）负责把 PDF 的字体字典解析成「运行时字体」，供定位器查询字形宽度和编码。它的内部（`active_font_*` 系列）留到 [u4-l4](u4-l4-active-runtime-and-font-backend.md) 精讲，本讲把它当黑盒。
- `NativeTextRunPositioner()` 是「文本串定位器」，本讲 4.3 节展开。

#### 4.1.3 源码精读

先看产品入口的完整定义（注意它把 `ActiveILCreater` 和 `NativeTextRunPositioner` 做了**函数内 import**，这是为了延迟加载、避免循环依赖和启动开销）：

[native_parse.py:39-75](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_parse.py#L39-L75) 定义产品入口 `parse_prepared_pdf_with_new_parser_to_legacy_ir`。它的 docstring 直白地说：**与上面的「解析专用辅助函数」不同，本函数复用调用方的 `TranslationConfig`、进度监视器、准备好的临时 PDF 和已打开的 PyMuPDF 文档**——这正是产品和独立路径的分水岭。函数体只有四步：造 sink、把 `doc_pdf` 挂到 `sink.mupdf`、建字体资源运行时、`with` 装页后造解释器、最后 `run_prepared_pages` 返回 `Document`。

作为对照，独立会话入口 `parse_with_new_parser_to_legacy_ir` 只是简单转发：

[native_parse.py:24-36](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_parse.py#L24-L36) 是兼容入口，转发到 `parse_with_native_builtin_positioner_to_legacy_ir`；后者（[native_parse.py:97-113](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_parse.py#L97-L113)）再走 `_parse_with_native_positioner_to_legacy_ir` → `_parse_with_positioner_to_legacy_ir` → `run_active_parse_session`。这条链自己管开 PDF 和配置，适合脱离主流程单独用。

> 小结：看产品流程，盯住 `parse_prepared_pdf_with_new_parser_to_legacy_ir` 这一个函数就够了；其余 `_parse_with_...` 是独立/测试用的同族函数。

#### 4.1.4 代码实践

1. **实践目标**：分清两条解析路径，并确认主流程用的是哪一条。
2. **操作步骤**：
   - 打开 [native_parse.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_parse.py)，找到两个入口：`parse_prepared_pdf_with_new_parser_to_legacy_ir`（L39）和 `parse_with_new_parser_to_legacy_ir`（L24）。
   - 用 Grep 在整个 `babeldoc/` 里搜索这两个名字，看各自被谁调用。
3. **需要观察的现象**：
   - `parse_prepared_pdf_with_new_parser_to_legacy_ir` 只被 `high_level.py` 调用（产品主流程）。
   - `parse_with_new_parser_to_legacy_ir`（及其 `_to_legacy_ir` 同族）多见于脚本/测试/独立工具。
4. **预期结果**：你能得出结论——**翻译主流程走的是「产品路径」**，它复用调用方已打开的 `doc_pdf`，而不是自己重新开文件。
5. 若想直接看到运行时证据，可在 `high_level.py:901` 的 `logger.debug(f"start parse il ...")` 附近确认日志，但**本步骤不要求实际运行**。

#### 4.1.5 小练习与答案

**练习 1**：为什么产品入口要把 `doc_pdf`（已打开的 PyMuPDF 文档）作为参数传进来，而不是自己再 `pymupdf.open(temp_pdf_path)` 一次？

> **参考答案**：因为主流程早已为了「修正 PDF、读元数据、打水印」等打开了同一个文档（`doc_pdf2zh`）。复用它一是省掉重复打开的开销，二是保证解析用的是**同一份经过修正的文档状态**（见 [u2-l2](u2-l2-main-pipeline-orchestration.md) 的 `fix_null_xref/fix_filter/fix_media_box`），避免解析与渲染对 PDF 几何的认知不一致。

**练习 2**：函数名里的 `legacy_ir` 指哪套数据结构？

> **参考答案**：指 `il_version_1`（见 [u3-l1](u3-l1-il-data-model.md)）这套「对外的、被下游 midend/backend 消费」的中间表示。new_parser 内部有更细的对象模型，但最终都要投影回 legacy IR。

---

### 4.2 prepared page 加载：把每一页预先装好

#### 4.2.1 概念说明

PDF 不能「一边读字节一边解析字符」那么简单——一页的内容是压缩过的内容流加上一套资源树（字体、图片、XObject 等）。**prepared page（准备好的页）** 就是把这些「解析要用的原料」预先装载、组织好，变成一个 `PreparedPdfPage` 对象，让解释器能高效、按需地读取。可以理解为：把生 PDF 的某一页「拆包摆好」。

装载由一个**上下文管理器** `load_prepared_pdf_pages` 负责。用 `with` 是为了确保用完一定关闭文档、释放资源。

#### 4.2.2 核心流程

```
load_prepared_pdf_pages(temp_pdf_path, should_include_page=...)
 │  with load_page_views(temp_pdf_path, ...) as raw_pages:   # 先得到「页视图」列表（含裁剪框等）
 │      document = fitz.open(temp_pdf_path)                  # 再用 PyMuPDF 打开同一份 PDF（读字体/对象）
 │      object_store = build_object_store(document)           # 构建 PDF 对象存储（xref 间接引用解析）
 │      object_access = object_store.as_resolved_access().as_prepared_object_access()
 │      for page_view in raw_pages:
 │          pages.append(build_prepared_pdf_page(page_view, object_access=object_access))
 │      yield pages                                           # 把准备好的页交给上层
 └─ 退出 with：document.close()                               # 保证关闭
```

关键点：**同一份 PDF 被打开两次视角**——`load_page_views` 给出「页视图」（页面几何/裁剪框层面），`fitz.open` 给出「对象视角」（用来读字体字典、xref 对象）。两者合起来才拼出一个完整的 prepared page。

> 说明：`fitz` 就是 PyMuPDF 的别名（`import fitz` 等价于 `import pymupdf`）。`should_include_page` 来自 `config.should_translate_page`（见 [u1-l4](u1-l4-config-and-translation-config.md) 的页面范围解析），用于**在装页阶段就跳过不需要翻译的页**，省内存省时间。

#### 4.2.3 源码精读

[pymupdf_prepared_page_access.py:13-38](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/pymupdf_prepared_page_access.py#L13-L38) 是上下文管理器 `load_prepared_pdf_pages`。它先 `with load_page_views(...)` 拿到原始页视图，再 `fitz.open(temp_pdf_path)` 打开文档、`build_object_store(document)` 构建对象存储并解析成 `object_access`；随后对每个 `page_view` 调 `build_prepared_pdf_page(page_view, object_access=object_access)` 生成 `PreparedPdfPage`，最后 `yield pages`。`finally` 里 `document.close()` 保证关闭。

被它产出的 `prepared_pages` 列表，正是 [native_parse.py:60-63](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_parse.py#L60-L63) 里 `with load_prepared_pdf_pages(...) as prepared_pages:` 拿到的对象，会传给下一步的解释器循环。

#### 4.2.4 代码实践

1. **实践目标**：理解 prepared page 是「两个视角合并」的产物，以及页面过滤发生在装载阶段。
2. **操作步骤**：
   - 阅读 [pymupdf_prepared_page_access.py:13-38](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/pymupdf_prepared_page_access.py#L13-L38)，找出「PDF 被打开了几个视角」「页是怎么逐个造出来的」「在哪一行关闭文档」。
   - 回到产品入口 [native_parse.py:60-63](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_parse.py#L60-L63)，注意 `should_include_page=config.should_translate_page` 这个参数一路传进去。
3. **需要观察的现象**：装载阶段就拿到了 `should_include_page`，意味着「不翻译的页」从一开始就被排除。
4. **预期结果**：你能说出 prepared page = `page_view`（几何视图）+ `object_access`（对象视图），并且页面范围过滤在装页时已生效。
5. **待本地验证**：若想实证，可在 `load_prepared_pdf_pages` 内临时打印 `len(pages)` 与传入 PDF 的真实页数对比（需自加日志，本讲不修改源码）。

#### 4.2.5 小练习与答案

**练习 1**：为什么同一个 `temp_pdf_path` 要用 `load_page_views` 和 `fitz.open` 两个途径打开？

> **参考答案**：`load_page_views` 提供「页面几何/裁剪框」视角，`fitz.open` 提供「对象/xref/字体字典」视角。两者各自擅长不同的访问需求，合并后才能既知道「这页长什么样」又知道「这页引用了哪些字体对象」，缺一不可。

**练习 2**：`should_include_page`（页面范围）过滤发生在哪一步？为什么放在这里？

> **参考答案**：发生在 `load_prepared_pdf_pages` 装页阶段（一路由 `config.should_translate_page` 传入）。放在这里是为了**尽早裁剪**：不需要翻译的页根本不构造 `PreparedPdfPage`，省下解析和内存开销。

---

### 4.3 page interpreter 与 text positioner：解释内容流并定位文本

#### 4.3.1 概念说明

有了 prepared page，下一步要「读懂」这页的内容流。这部分由两个角色配合：

- **page interpreter（页解释器）**：把一页的内容流（一串 PDF 操作符）**解释成一串事件**（`TextRunEvent` 文本串事件、`PathPaintEvent` 路径绘制事件、`ImageXObjectEvent` 图片事件等），并把这些事件喂给 sink。事件本身是 u4-l3 的重点，本讲把它当黑盒，只需知道「解释器产事件」。
- **text run positioner（文本串定位器）**：文本串事件只告诉你「用了哪个字体、字号多大、文本矩阵是什么」，但**没有逐字坐标**。定位器的职责是把一个 `TextRunEvent` **展开成一组带坐标的字形**（`AWLTChar`），每个字形带 `bbox`、`cid`、`advance` 等——这些正是 IL `PdfCharacter` 需要的原料。

> 为什么要把「解释」和「定位」分开？因为「解释内容流」是通用的（对所有 PDF 操作符一视同仁），而「文本怎么定位」涉及字体度量、字间距、竖排横排等细节，是可替换的策略（源码里有 `TextRunPositioner` 这个 Protocol 接口，`NativeTextRunPositioner` 是其 native 实现）。

#### 4.3.2 核心流程

解释器对外暴露三个回调（`begin_page` / `process_page` / `end_page`），`run_prepared_pages` 会按页依次调用它们：

```
对每一页 prepared_page：
 │
 ├─ interpreter.begin_page(page, pageno)
 │     ├─ sink.on_page_start()                      # sink 新建一个 il_version_1.Page
 │     ├─ sink.on_page_crop_box(...)                 # 写裁剪框
 │     ├─ sink.on_page_media_box(...)                # 写媒体框
 │     └─ sink.on_page_number(pageno)                # 写页码
 │
 ├─ ops_base = interpreter.process_page(page)
 │     ├─ resource_bundle = resource_runtime.build_page_resource_bundle(page.resource_tree)
 │     ├─ events, resource_bundle, base_operations = interpret_page_with_resource_bundle(page, resource_bundle)
 │     │        # ↑ 把内容流解释成一串事件（详见 u4-l3）
 │     ├─ emit_native_text_events_to_legacy_sink(events, resource_bundle, sink,
 │     │        xobject_end_operations=..., text_run_positioner=positioner)
 │     │        # ↑ 遍历事件；遇到文本事件就调 positioner 展开成字形，再交 sink 投影
 │     └─ return wrap_page_base_operation(...)       # 这页的「基础操作」字符串（供 backend 还原用）
 │
 ├─ sink.on_page_base_operation(ops_base)            # sink 收下基础操作
 ├─ sink.on_page_end()                               # sink 收尾这一页（统计、推进进度）
 └─ interpreter.end_page(page, pageno)               # 本实现里是 no-op（避免重复计数）
```

定位器内部（`NativeTextRunPositioner.position_text_run`）做的事：把文本矩阵和 CTM 相乘得到最终矩阵；按字号、水平缩放、字间距算出每个字形的步进；遍历事件里的 segments（数字表示 `TJ` 的负位移、bytes 表示真正的字符编码），逐字解码出 `cid` 和 unicode，计算每个字形的 `char_matrix`、宽度和 advance，产出 `AWLTChar` 列表。

#### 4.3.3 源码精读

[native_page_interpreter.py:16-68](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_page_interpreter.py#L16-L68) 是工厂 `create_native_page_interpreter`，它返回一个内部类 `_NativePageInterpreter` 实例，实现三个回调：
- [begin_page（L25-L36）](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_page_interpreter.py#L25-L36)：调用 `sink.on_page_start()` / `on_page_crop_box` / `on_page_media_box` / `on_page_number`，把页面几何信息交给 sink。
- [process_page（L38-L59）](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_page_interpreter.py#L38-L59)：先用 `resource_runtime.build_page_resource_bundle(page.resource_tree)` 构建资源包；再调 `interpret_page_with_resource_bundle(page, resource_bundle)`（[page_api.py:27-31](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/page_api.py#L27-L31)，转发到 `interpret_prepared_page`）把内容流解释成事件；然后用 `emit_native_text_events_to_legacy_sink(...)` 把事件派发给 sink，并把 `text_run_positioner` 一起传进去——**这就是定位器被调用的入口**；最后返回包装好的页基础操作。
- [end_page（L61-L67）](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_page_interpreter.py#L61-L67)：**故意留空（no-op）**。注释解释：`on_page_end()` 由执行会话统一调用，这里若再调一次会**重复统计有效文本**。这是一个容易踩坑的点——`end_page` 不等于 `on_page_end`。

[text_positioning.py:24-101](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/text_positioning.py#L24-L101) 是 `NativeTextRunPositioner.position_text_run`。它先 `multiply_matrices(event.text_matrix, event.ctm)` 得到最终矩阵；按字号、缩放算出 `charspace`、`wordspace`、`dxscale`；随后遍历 `event.segments`——遇到数字（`TJ` 数组里的负位移）就平移光标、遇到 bytes 就用 `font.decode(obj)` 解出每个 `cid`，对每个字形 `translate_existing_matrix(matrix, (pos_x, pos_y))` 算出其字符矩阵，再 `font.unicode_text(cid, ...)` 得到文本、`font.char_width(cid)` 得到宽度，组装成 `AWLTChar` 并累加 `adv`。竖排（`font.is_vertical()`）时沿 y 轴累加，横排沿 x 轴。返回的 `AWLTChar` 列表，就是 sink 投影成 `PdfCharacter` 的原料。

#### 4.3.4 代码实践

1. **实践目标**：把「解释器三回调」与「定位器职责」对应起来，理解一个文本操作符最终如何变成带坐标的字形。
2. **操作步骤**：
   - 在 [native_page_interpreter.py:25-59](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_page_interpreter.py#L25-L59) 里，给 `begin_page`、`process_page`、`end_page` 各写一句话注释，说明它调了 sink 的哪些方法。
   - 在 [text_positioning.py:24-101](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/text_positioning.py#L24-L101) 里，找到「矩阵相乘」「字间距计算」「逐 cid 解码」三段对应的行。
3. **需要观察的现象**：`process_page` 里 `text_run_positioner` 作为参数被传进 `emit_native_text_events_to_legacy_sink`，说明**定位不是解释器自己做的，而是交给一个可替换的策略对象**。
4. **预期结果**：你能描述一个 `Tj`/`TJ` 文本操作符的生命周期：内容流操作符 →（解释器）→ `TextRunEvent` →（定位器 `position_text_run`）→ 一组 `AWLTChar`（带坐标）→（sink）→ `PdfCharacter`。
5. **待本地验证**：定位器涉及字体度量，具体字形坐标需真实字体才能复现；本步骤以源码阅读为主。

#### 4.3.5 小练习与答案

**练习 1**：`_NativePageInterpreter.end_page` 为什么是空的（no-op）？

> **参考答案**：因为 `sink.on_page_end()`（含有效文本统计、进度推进）由 `run_prepared_pages` 所在的执行会话统一调用一次。如果 `end_page` 再调一次 `on_page_end`，就会**重复统计**同一页的有效字符数。源码注释明确写了这一点（[L61-L67](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_page_interpreter.py#L61-L67)）。

**练习 2**：`TextRunPositioner` 为什么设计成 Protocol（接口），而不直接写死在解释器里？

> **参考答案**：为了**解耦与可替换**。「解释内容流」是通用流程，但「文本如何定位」依赖具体字体度量策略。抽成接口后，可以换不同的 positioner 实现（例如 native 实现或其它），而不必改动解释器主体。这也是 `create_native_page_interpreter` 把 positioner 作为参数传入的原因。

---

### 4.4 ActiveILCreater 构建 IL：sink 如何把事件攒成 Document

#### 4.4.1 概念说明

前面三节一直在「喂」事件，这一节看「接收端」。`ActiveILCreater` 就是那个 sink：它实现了一组 `on_*`（页面级事件）和 `project_native_*`（把具体元素投影成 IL 实体）方法，收到事件就往自己内部的 `self.docs`（一个 `il_version_1.Document`）里填东西。等所有页处理完，调一次 `create_il()`，返回这棵完整的 `Document`。

「project（投影）」这个词很贴切：PDF 里的元素（字符、曲线、字体）有自己的原始形态，`ActiveILCreater` 把它们**投影**到 IL 坐标系里，变成 `PdfCharacter`、`PdfCurve`、`PdfFont` 等实体（见 [u3-l1](u3-l1-il-data-model.md)）。`Active` 这个前缀，是相对于旧的 `il_creater.py` 而言的——文件头注释说旧的保留作兼容，新的图形/路径归属工作落在 active 路径上。

#### 4.4.2 核心流程

sink 的生命周期与「造 IL」对应如下：

```
ActiveILCreater(config)
 │  self.docs = il_version_1.Document(page=[])          # 初始：空 Document
 │
 ├─ on_total_pages(n)        # 设置 docs.total_pages，并启动进度阶段
 │
 ├─ 对每一页：
 │    ├─ on_page_start()      → 新建 il_version_1.Page(...)，append 进 docs.page
 │    ├─ on_page_crop_box / on_page_media_box / on_page_number  → 填 Page 的几何与页码
 │    ├─ on_page_resource_font → project 字体 → 填 Page.pdf_font（PdfFont）
 │    ├─ project_native_char   → 把字形投影成 PdfCharacter，append 进 Page.pdf_character
 │    ├─ project_native_curve  → 把路径投影成 PdfCurve，append 进 Page.pdf_curve
 │    ├─ on_xobj_form / emit_native_image_xobject → 填 Page.pdf_form（图片/表单 XObject）
 │    └─ on_page_end()         → 累计有效字符/token 统计，推进进度
 │
 ├─ on_finish()              → 关闭进度阶段
 └─ create_il()              → 按页过滤后返回 self.docs
```

`project_native_char` 是最核心的一段：它从定位器产出的 `AWLTChar` 上读取 `bbox`、`cid`、`unicode`、`advance`、字体、字号，组装出一个 `il_version_1.PdfCharacter`（含 `box`、`visual_bbox`、`pdf_style`、`render_order` 等），append 进当前页的 `pdf_character` 列表——这正好接上 [u3-l1](u3-l1-il-data-model.md) 讲的字级实体。

#### 4.4.3 源码精读

[il_creater_active.py:186-194](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py#L186-L194) 定义类 `ActiveILCreater`，并声明 `stage_name = "Parse PDF and Create Intermediate Representation"`——这个名字会出现在进度监视器的阶段列表里（见 [u2-l3](u2-l3-async-translate-and-progress.md)）。类 docstring 说明：旧的 `il_creater.py` 保留作兼容工具，新的图形/路径归属工作落在 active 路径上，这样产品路径能演进而不动旧解析器。

[il_creater_active.py:196-230](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py#L196-L230) 是 `__init__`，其中 [L201](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py#L201) `self.docs = il_version_1.Document(page=[])` 创建了那棵空的 IL 树——这就是整条解析链要往里填东西的目标容器。它还初始化了字体投影缓存 `projected_font_resource_cache`、图形状态池 `graphic_state_pool`、`font_mapper`、tiktoken tokenizer 等。

[il_creater_active.py:239-246](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py#L239-L246) 是 `create_il`：再次按 `should_translate_page` 过滤一遍页（保险），然后返回 `self.docs`。这就是产品入口 `run_prepared_pages` 最终 `return` 的东西。

页面级回调：
- [on_total_pages（L248-L260）](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py#L248-L260) 设置 `docs.total_pages` 并 `progress_monitor.stage_start(self.stage_name, total)` 启动进度阶段。
- [on_page_start（L365-L384）](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py#L365-L384) 新建一个 `il_version_1.Page(pdf_font=[], pdf_character=[], page_layout=[], pdf_curve=[], pdf_form=[], unit="point")` 并 `self.docs.page.append(self.current_page)`——这一步把 [u3-l1](u3-l1-il-data-model.md) 讲的「Page 下挂九个并列集合」具象化。
- [on_page_end（L386-L408）](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py#L386-L408) 把本页 `_page_valid_chars_buffer` 拼起来用 tiktoken 算 token 数，调 `shared_context_cross_split_part.add_valid_counts(...)` 累计（供分片翻译与计费用，见 [u8-l2](u8-l2-split-and-merge.md)），最后 `self.progress.advance(1)` 推进进度。

投影方法（事件 → IL 实体）：
- [on_page_resource_font / _project_font_resource（L571-L641）](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py#L571-L641) 把 PDF 字体字典投影成 `il_version_1.PdfFont`（含 `name`、`xref_id`、`font_id`、bold/italic/serif 标志、逐字 `pdf_font_char_bounding_box`），并带缓存（`projected_font_resource_cache`）避免重复解析同一字体。
- [project_native_curve（L1154-L1264）](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py#L1154-L1264) 把路径绘制事件投影成 `il_version_1.PdfCurve`（含 `box`、`pdf_path`、`graphic_state`、`render_order`、`ctm`）。
- [project_native_char（L1292-L1421）](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py#L1292-L1421) 是字符投影的核心：读取字形的 `bbox`、`cid`、`unicode`、`advance`、字号、descent，组装 [L1383-L1394](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py#L1383-L1394) 的 `il_version_1.PdfCharacter(box=..., pdf_character_id=char_id, advance=..., char_unicode=..., vertical=..., pdf_style=..., xobj_id=..., visual_bbox=..., render_order=...)`，append 进 `self.current_page.pdf_character`。注意它还会处理竖排（`vertical`）的视觉 bbox 偏移，以及 OCR workaround 时把字符涂黑、清掉 render_order（[L1395-L1397](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py#L1395-L1397)）。

#### 4.4.4 代码实践

1. **实践目标**：建立「sink 的每个 `on_*`/`project_*` 对应 IL 里的哪类实体」的映射。
2. **操作步骤**：
   - 打开 [il_creater_active.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py)。
   - 填一张映射表：

     | sink 方法 | 产出的 IL 实体 | append 到哪个列表 |
     |---|---|---|
     | `on_page_start` | `Page` | `docs.page` |
     | `on_page_resource_font` / `_project_font_resource` | `PdfFont` | `Page.pdf_font` |
     | `project_native_char` | `PdfCharacter` | `Page.pdf_character` |
     | `project_native_curve` | `PdfCurve` | `Page.pdf_curve` |
     | `on_xobj_form` / 图片 | `PdfForm` | `Page.pdf_form` |
3. **需要观察的现象**：每个 project 方法的最后一行几乎都是 `self.current_page.<某列表>.append(<IL 实体>)`——这就是「事件落地为 IL」的物理动作。
4. **预期结果**：你能解释 `project_native_char` 里 [L1383-L1394](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py#L1383-L1394) 构造的 `PdfCharacter` 字段分别对应 [u3-l1](u3-l1-il-data-model.md) 讲的哪些属性（`box`/`char_unicode`/`pdf_style`/`visual_bbox` 等）。
5. **可选运行验证**：用 `babeldoc --debug` 翻译一个 PDF，在工作目录找到 `create_il.debug.json`（由 [high_level.py:916-920](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L916-L920) 写出），打开后确认 `page[*].pdfCharacter` 确实由这一阶段产出。若无可翻译的 PDF，本步骤标注为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`ActiveILCreater.__init__` 里为什么要缓存字体投影（`projected_font_resource_cache`）和图形状态（`graphic_state_pool`）？

> **参考答案**：一份 PDF 里同一字体会被很多页、很多 XObject 反复引用；同一组图形状态指令（颜色、线宽等）也会反复出现。缓存后，重复的字体/图形状态只解析一次、复用同一个 IL 对象，既省 CPU 又能显著减小 IL 体积、降低后续渲染去重压力。

**练习 2**：`project_native_char` 里这一段（[L1395-L1397](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py#L1395-L1397)）`if self.translation_config.ocr_workaround: pdf_char.pdf_style.graphic_state = BLACK; pdf_char.render_order = None` 是什么意图？

> **参考答案**：当启用 OCR workaround（针对扫描件，见 [u5-l1](u5-l1-detect-scanned-file.md)）时，把字符图形状态强制设为黑色、并清掉 render_order。这是为了让后续渲染按特定方式处理这类字符，配合扫描件的可读性修正。

---

## 5. 综合实践

把本讲四节串起来，完成下面的「源码跟踪型」实践——这是本讲的核心任务。

**任务：列出从 PDF 路径到返回 `Document` 的关键函数调用顺序。**

1. **实践目标**：不看答案，凭源码画出一条完整的 frontend 调用链，标注每一步所在的文件与作用。
2. **操作步骤**：
   - 从 [high_level.py:906-910](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L906-L910) 的调用点出发。
   - 依次打开下列文件，确认每个函数被谁调用、又调用了谁：
     1. [native_parse.py:39-75](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_parse.py#L39-L75) `parse_prepared_pdf_with_new_parser_to_legacy_ir`
     2. [pymupdf_prepared_page_access.py:13-38](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/pymupdf_prepared_page_access.py#L13-L38) `load_prepared_pdf_pages`
     3. [native_parse.py:64-69](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_parse.py#L64-L69) `create_native_page_interpreter`
     4. [prepared_page_execution.py:10-32](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/prepared_page_execution.py#L10-L32) `run_prepared_pages`
     5. [native_page_interpreter.py:38-59](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_page_interpreter.py#L38-L59) `process_page` → [page_api.py:27-31](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/page_api.py#L27-L31) `interpret_page_with_resource_bundle`
     6. [text_positioning.py:24-101](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/text_positioning.py#L24-L101) `NativeTextRunPositioner.position_text_run`（被事件派发调用）
     7. [il_creater_active.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py) 各 `on_*` / `project_native_*` → `create_il`（[L239-L246](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py#L239-L246)）
3. **需要观察的现象**：数据形态在这条链上的演变——PDF 路径 → `PreparedPdfPage` 列表 → 事件流（`TextRunEvent` 等）→ 带坐标字形（`AWLTChar`）→ IL 实体（`PdfCharacter`/`PdfCurve`/`PdfFont`）→ `Document`。
4. **预期结果**：你能写出类似下面这张调用顺序表（自己核对，不要直接抄）：

   | # | 函数 | 文件 | 作用 |
   |---|---|---|---|
   | 1 | `parse_prepared_pdf_with_new_parser_to_legacy_ir` | native_parse.py | 产品入口，造 sink、装页、造解释器、跑页 |
   | 2 | `ActiveILCreater(config)` | il_creater_active.py | 造 sink，初始化空 `Document` |
   | 3 | `load_prepared_pdf_pages` | pymupdf_prepared_page_access.py | 打开 PDF，逐页造 `PreparedPdfPage` |
   | 4 | `create_native_page_interpreter` | native_page_interpreter.py | 造页解释器（绑定 sink + 定位器 + 资源运行时） |
   | 5 | `run_prepared_pages` | prepared_page_execution.py | 逐页 `begin/process/end`，收尾 `create_il` |
   | 6 | `process_page` → `interpret_page_with_resource_bundle` | native_page_interpreter.py / page_api.py | 解释内容流成事件 |
   | 7 | `position_text_run`（事件派发时） | text_positioning.py | 文本事件展开成带坐标字形 |
   | 8 | `project_native_char` / `project_native_curve` / `_project_font_resource` | il_creater_active.py | 投影成 IL 实体，append 进当前页 |
   | 9 | `create_il` | il_creater_active.py | 返回完整的 `Document` |

5. **可选运行验证**：用 `babeldoc --debug` 翻译 `examples/ci/test.pdf`，确认工作目录生成 `create_il.debug.json`（[high_level.py:916-920](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L916-L920)），它就是这条链第 9 步返回的 `docs` 的 JSON 快照。**待本地验证**：实际是否生成、内容长什么样，取决于你的运行环境与字体。

## 6. 本讲小结

- BabelDOC 的 frontend 有**两条入口**：产品路径 `parse_prepared_pdf_with_new_parser_to_legacy_ir`（被 `high_level._do_translate_single` 调用，复用已打开的 `doc_pdf`）和独立会话路径 `parse_with_new_parser_to_legacy_ir`（自己开 PDF，供脚本/测试用）。主流程走的是前者。
- 产品入口只做四件事：**造 sink（`ActiveILCreater`）→ 装页（`load_prepared_pdf_pages`）→ 造解释器（`create_native_page_interpreter`）→ 跑页（`run_prepared_pages`）**。
- **prepared page** 是「页视图 + 对象视角」合并的产物，页面范围过滤在装页阶段就生效；同一份 PDF 会被 `load_page_views` 和 `fitz.open` 两个途径打开，分别提供几何与对象访问。
- **页解释器**把内容流解释成事件、把页面几何交给 sink；**文本串定位器**（`NativeTextRunPositioner`）把文本事件展开成带坐标的字形。注意 `end_page` 是 no-op，`on_page_end` 由执行会话统一调用以防重复计数。
- **`ActiveILCreater` 是 sink**：收 `on_*` 事件、用 `project_native_*` 把字符/曲线/字体/图片投影成 `PdfCharacter`/`PdfCurve`/`PdfFont`/`PdfForm`，最终 `create_il()` 返回完整 `Document`。它用缓存复用字体与图形状态。
- 数据形态沿链演变：**PDF 路径 → `PreparedPdfPage` → 事件 → 带坐标字形 → IL 实体 → `Document`**。

## 7. 下一步学习建议

- **想搞懂「事件」到底是什么** → 学 [u4-l2 PDF 对象解析](u4-l2-pdf-object-parsing.md)（PDF 对象语法与 `object_parser`）和 [u4-l3 内容流解释器](u4-l3-content-interpreter.md)（`interpreter` 如何把操作符变成 `TextRunEvent`/`PathPaintEvent`，`glyphs` 如何展开字形）。本讲把「解释器产事件」当黑盒，那里是开盒的地方。
- **想搞懂「字体资源运行时」** → 学 [u4-l4 active 运行时与字体后端](u4-l4-active-runtime-and-font-backend.md)，它会讲 `resource_runtime`、`active_font_*` 系列如何从 PDF 字体字典构建运行时字体并缓存度量。
- **想看 sink 产出的 IL 怎么被序列化** → 回顾 [u3-l2 IL 的序列化](u3-l2-il-serialization.md)，理解 `create_il.debug.json` 为什么是排查 frontend 的核心抓手。
- **想看这棵 `Document` 之后被谁加工** → 学 [u5 中端处理流水线](u5-l1-detect-scanned-file.md)，midend 各阶段如何吃掉 frontend 产出的同一个 `docs`。
