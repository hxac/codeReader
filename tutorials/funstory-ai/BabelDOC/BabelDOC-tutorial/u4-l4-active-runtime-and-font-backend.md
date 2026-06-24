# active 运行时与字体后端

## 1. 本讲目标

本讲是 PDF 解析前端（`new_parser`）的最后一站。在 u4-l3 里我们已经知道：内容流解释器会把文本操作符翻译成 `TextRunEvent`，再由 `glyphs`/positioner 用「字体度量」把它展开成带坐标的字形。但那套几何数学依赖一个前提——你必须先有一个能回答 `decode`、`char_width`、`unicode_text`、`get_descent` 等问题的「运行时字体对象」。

**本讲就要回答：这个运行时字体对象从哪里来、怎么造、怎么缓存、为什么这样设计。**

读完本讲你应当能够：

1. 说清 `active_*` 这套命名背后的「投影 + 惰性访问」设计模式，以及它为什么要这样做。
2. 跟着 `active_parse_runtime` → `NativePageExecutionSession` 的调用链，说出「解析会话」是如何被编排起来的。
3. 指出 `active_direct_font_backend` 按字体 `Subtype` 分派处理哪几类 PDF 字体（Type1/TrueType/Type3/CID/Type0），以及它们各自落在哪个运行时后端。
4. 解释 `ActiveFontResolver` 为什么需要两级缓存（跨页 `runtime_cache` + 每页 `legacy_descents`）。
5. 说明这套设计如何让「pymupdf 负责取页面对象、pdfminer 语义负责字体度量」两个后端共存。

## 2. 前置知识

### 2.1 PDF 字体的五种 Subtype

PDF 规范把字体分成几类，BabelDOC 直接处理的是下面五种（见 `DIRECT_FONT_SUBTYPES`）：

| Subtype | 中文叫法 | 特点 | 字符编码 |
|---|---|---|---|
| `Type1` / `MMType1` | PostScript Type1 | 经典 Adobe 字体，字形用三次贝塞尔描述 | 单字节（0–255） |
| `TrueType` | TrueType | 苹果/微软体系，字形用二次贝塞尔 | 单字节（0–255） |
| `Type3` | 用户自定义字体 | 字形本身是一段绘图过程（drawing procedure），不是固定字形表 | 单字节 |
| `CIDFontType0` / `CIDFontType2` | CID 字体 | 用 CID（Character ID）索引的大字符集字体（中日韩、符号） | 多字节 |
| `Type0` | 复合字体（Composite） | 一个外壳，内部包一个 CID 后代字体（DescendantFonts），自己提供 Encoding/ToUnicode | 多字节 |

> 关键区别：前三种（Type1/TrueType/Type3）是「简单字体」，一个字符编码 = 一个字节；CID/Type0 是「复合字体」，一个字符编码可能是多个字节（中文常用 2 字节）。

### 2.2 「投影」与「惰性」是什么意思

- **投影（project）**：把一个可能引用了其它对象（间接引用 `PdfIndirectRef`）的 PDF 字典，递归地把所有引用都解析掉、并转换成一套「标准化的纯数据结构」，得到一个自包含的、不再依赖原始 PDF 对象图的副本。
- **惰性（lazy）**：真正昂贵的字体对象（要解析嵌入字体流、构建 CMap、跑 FreeType 取字形）只在第一次被需要时构造一次，之后按 key 缓存复用。

`active_*` 这个命名前缀，就是 BabelDOC 给「主动解析 + 投影 + 缓存」这一套机制的统称。命名里的 `active` 与「惰性」并不矛盾——它是「按需主动物化（materialize on access）」的意思。

### 2.3 字体度量（font metrics）

字体度量是字体里描述「每个字多宽、字身高低」的一张表。PDF 里宽度通常以 **1/1000 em** 为单位（即一个字号单位 = 1000）。运行时字体把宽度换算成小数：

\[
\text{char\_width}(\text{cid}) = \text{width}_{1000}(\text{cid}) \times 0.001
\]

`ascent`（基线上方高度）和 `descent`（基线下方深度）也用同样单位，本讲会看到 `descent` 被反复读写，这是缓存设计的关键点之一。

## 3. 本讲源码地图

本讲涉及的文件全部在 `babeldoc/format/pdf/new_parser/` 下，按职责分三层：

| 文件 | 角色 | 一句话作用 |
|---|---|---|
| [`active_parse_runtime.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_parse_runtime.py) | 会话入口 | 脚本/独立解析的顶层入口 `run_active_parse_session` |
| [`active_font_resource_runtime.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_font_resource_runtime.py) | 资源运行时 | 把「页资源树」装配成 `PageResourceBundle`，持有跨页字体缓存 |
| [`active_font_runtime.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_font_runtime.py) | 字体解析器 | `ActiveFontResolver` + `ActiveFontAdapter`，核心缓存逻辑 |
| [`active_font_backend.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_font_backend.py) | 字体工厂 | `ActiveFontFactory.create_font`，对接直接构造器 |
| [`active_direct_font_backend.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_direct_font_backend.py) | 直接构造器 | 按 Subtype 分派，产出五个运行时字体后端类 |
| [`active_object_projection.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_object_projection.py) | 投影器 | `project_font_spec`：把含间接引用的字体字典拍扁 |
| [`font_types.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/font_types.py) | 协议定义 | `PdfFontLike` / `PdfRuntimeFontLike` 接口契约 |
| [`resources.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/resources.py) | 资源束 | `PageResourceBundle`：按 XObject 路径解析字体、含兜底字体 |

此外会少量引用消费端：[`native_page_execution_session.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_page_execution_session.py)、[`text_positioning.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/text_positioning.py)、[`il_creater_active.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py)。

## 4. 核心概念与源码讲解

### 4.1 active 投影/惰性模式：把 PDF 字体对象「拍扁」成纯数据

#### 4.1.1 概念说明

PDF 字体字典里几乎每个字段都可能是「间接引用」——比如 `FontDescriptor`、`Widths`、`ToUnicode`、`FontFile2` 往往写成 `12 0 R`，意思是「真正的内容在第 12 号对象里」。如果让字体构造器直接面对这种「半成品」字典，它就得自己懂 xref、会查表、会处理流，构造器就和具体的 PDF 后端（pymupdf 还是 pdfminer）死死绑死。

`active_*` 体系的第一步，就是用一个**投影器**把这些间接引用全部解析掉，得到一个「纯净的、自包含的 Python 字典」——这就是 `runtime_spec`。之后再交给构造器时，构造器只看到普通 `dict`/`list`/`ActiveLiteral`/流包装，完全不需要知道数据是从哪个后端来的。

> 一句话：**投影 = 把「带引用的、依赖后端的对象图」转成「无引用的、后端无关的纯数据快照」。** 这一步是整个 active 体系能兼容多后端的根基。

#### 4.1.2 核心流程

`project_font_spec` 分两步走（先解析引用，再投影类型）：

```text
输入: spec (dict，可能含 PdfIndirectRef / PdfObjectStream)
  │
  ├─ _resolve_all(spec, resolve_indirect)
  │     · 遇到 PdfIndirectRef → 调 resolve_indirect(value) 物化
  │     · 递归处理 dict / list / PdfObjectStream
  │     · 深度 > MAX_FONT_SPEC_PROJECTION_DEPTH(128) → 抛 FontSpecProjectionError
  │     · 检测到循环引用 → 抛 FontSpecProjectionError
  │
  └─ _project_value(resolved)
        · str（ASCII）→ ActiveLiteral(str)           # 名字包装
        · str（非 ASCII）→ ActiveLiteral(latin-1 bytes)
        · dict / list → 递归
        · PdfObjectStream → create_active_stream(attrs, rawdata)
        · 其它（int/float/bytes/None）→ 原样返回

输出: runtime_spec (dict，全部是纯数据 / ActiveLiteral / active stream)
```

两个安全阀值得注意：

- **深度限制** `MAX_FONT_SPEC_PROJECTION_DEPTH = 128`（[`active_object_projection.py:10`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_object_projection.py#L10)）：恶意/损坏的 PDF 可能构造极深的嵌套对象，这个上限防止栈溢出。
- **循环检测**：`_resolve_all` 用 `active_refs`（已访问的 objid 集合）和 `active_containers`（已访问的容器 `id` 集合）两条线索分别防「间接引用自环」和「容器自环」，一旦命中就抛 `FontSpecProjectionError`，对应代码在 [`active_object_projection.py:40-119`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_object_projection.py#L40-L119)。

流对象在投影时还有个细节：`_project_stream_attrs` 会把 `Filter`、`DecodeParms`、`Length` 等解码相关属性剥离（[`active_object_projection.py:183-191`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_object_projection.py#L183-L191)），因为投影后流已经是解码好的 `rawdata`，这些「怎么解码」的元信息不再需要。

#### 4.1.3 源码精读

入口 [`project_font_spec`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_object_projection.py#L17-L31)：先 `_resolve_all` 解引用，再 `_project_value` 转类型，最后断言结果必须是 dict。

`PdfIndirectRef` 的物化发生在 `_resolve_all` 中（节选）：

```python
# active_object_projection.py:50-65  —— 遇到间接引用就调 resolve_indirect 物化
ref_id = obj_ref_id(value)
if ref_id is not None:
    if ref_id in active_refs:                       # 循环引用保护
        raise FontSpecProjectionError(...)
    resolved = resolve_indirect(value)              # 由后端提供：查 xref、读对象
    if resolved is value:
        return value
    return _resolve_all(resolved, resolve_indirect, ...,
                        active_refs=active_refs | {ref_id}, ...)
```

这里的 `resolve_indirect` 是个回调，由上游「装页」阶段注入（来自 `PreparedFontSpec.resolve_indirect`）。对 pymupdf 后端，它内部用 `xref` API 取对象；但这一层对投影器是不可见的——投影器只认「给我一个值」的回调接口。

> 关键结论：投影器把「取对象」这件事抽象成一个回调，于是它本身**不绑定任何 PDF 后端**。这是 active 体系能兼容 pymupdf/pdfminer 的第一块基石。

#### 4.1.4 代码实践

**实践目标**：亲手感受「投影前 vs 投影后」的数据形态差异。

**操作步骤**（示例代码，需在已安装 BabelDOC 的环境运行）：

```python
# demo_projection.py —— 示例代码，非项目原有文件
from babeldoc.format.pdf.new_parser.active_object_projection import project_font_spec
from babeldoc.format.pdf.new_parser.active_object_backend import create_active_literal

# 1) 一个「假装」含间接引用的 spec：用 dict 模拟已物化的 FontDescriptor
spec = {
    "Type": "Font",
    "Subtype": "Type1",
    "BaseFont": "Helvetica",
    "FontDescriptor": {"FontName": "Helvetica", "Flags": 32},  # 已是 inline dict
}

# 2) 投影（resolve_indirect 留空，因为没有真正的 PdfIndirectRef）
runtime_spec = project_font_spec(spec)

print(type(runtime_spec["BaseFont"]).__name__)   # ActiveLiteral —— 字符串被包成名对象
print(type(runtime_spec["Subtype"]).__name__)    # ActiveLiteral
print(type(runtime_spec["FontDescriptor"]).__name__)  # dict —— 容器保持 dict
```

**需要观察的现象**：顶层 `BaseFont` 这种「名字类字符串」会被包成 `ActiveLiteral`，而 `FontDescriptor` 这种容器仍是 `dict`（但其内部的值也会被递归投影）。

**预期结果**：`ActiveLiteral`、`ActiveLiteral`、`dict`。（具体打印以本地运行为准——待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么投影器要同时维护 `active_refs`（按 objid）和 `active_containers`（按 `id()`）两套循环检测？

**参考答案**：`PdfIndirectRef` 有稳定的 `objid`，用集合去重能精确识别「同一个对象被再次引用」；但普通 `dict`/`list` 没有 objid，只能用 Python 内置 `id()` 识别「同一个容器对象被再次进入」。两种引用形态用两种 key，才能同时挡住「间接引用自环」和「容器自环」这两类死循环。

**练习 2**：投影后，流对象的 `Filter`/`Length` 属性为什么被丢弃？

**参考答案**：投影时流已经被解码成 `rawdata`（原始字节已就绪），`Filter`/`DecodeParms`/`Length` 这些只是「如何还原 rawdata」的元信息，对后续字体构造没有任何用处；保留它们反而可能让构造器误以为还要再解一次码。丢弃它们让投影后的流更「干净」。

---

### 4.2 解析会话编排：active_parse_runtime 与 NativePageExecutionSession

#### 4.2.1 概念说明

「会话（session）」是 BabelDOC 对「解析一份 PDF」这件事的封装。它把三样东西攥在一起：一个 `TranslationConfig`（配置/装配盘）、一个 `sink`（接收 IL 事件的 `ActiveILCreater`）、一个 `resource_runtime`（字体资源运行时）。会话负责按页驱动整个解析，但**不关心**字体具体怎么造——它只负责把「页资源树」交给 `resource_runtime`，拿到 `PageResourceBundle` 后丢给解释器。

会话有两个入口：

- **脚本/独立入口** `run_active_parse_session`：自己开 PDF、自己建 config，供测试和脚本使用。
- **产品入口** `parse_prepared_pdf_with_new_parser_to_legacy_ir`：复用主翻译流程已经打开的 PyMuPDF 文档和 config（见 u4-l1）。

#### 4.2.2 核心流程

```text
run_active_parse_session(pdf_path, ..., create_session)
  │  build_parse_only_config(pdf_path)  →  config
  │  sink = ActiveILCreater(config)
  └─ create_session(config, sink).run()    ← 调用方注入 session 类型
        │
        ▼  (以 NativePageExecutionSession 为例)
  NativePageExecutionSession.run()
        │  resource_runtime = create_active_font_resource_runtime()   ← 默认创建 active 字体运行时
        └─ PageExecutionSession(...).run()
              │  prepare_pdf_for_parse(input_file)  → doc_pdf, temp_pdf_path
              │  for each prepared page:
              │     interpreter.begin_page(page)         → sink.on_page_start / cropbox / mediabox
              │     interpreter.process_page(page):
              │        bundle = resource_runtime.build_page_resource_bundle(page.resource_tree)
              │        events = interpret_page_with_resource_bundle(page, bundle)
              │        emit_native_text_events_to_legacy_sink(events, bundle, sink, positioner)
              │     interpreter.end_page(page)           → no-op（避免重复计数）
              └─ sink.create_il()  →  Document (IL)
```

注意 `NativePageExecutionSession` 的 `create_resource_runtime` 字段**默认就是** `create_active_font_resource_runtime`（[`native_page_execution_session.py:26-28`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_page_execution_session.py#L26-L28)）——也就是说，默认走的就是本讲的 active 字体后端；这是一个可替换的钩子，将来换别的字体运行时无需动会话主体。

#### 4.2.3 源码精读

脚本入口 [`run_active_parse_session`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_parse_runtime.py#L11-L27) 只有三步：建 config、建 sink、运行调用方注入的 session。它把「用哪种 session」决定权交给 `create_session` 回调，是一种典型的「依赖注入」。

每页处理的核心在 `NativePageInterpreter.process_page`（[`native_page_interpreter.py:38-54`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_page_interpreter.py#L38-L54)），其中第一步就是把页资源树交给运行时：

```python
# native_page_interpreter.py:39-41  —— 每页都重建一次资源束（但字体后端跨页缓存）
resource_bundle = resource_runtime.build_page_resource_bundle(page.resource_tree)
```

这里的 `resource_bundle` 是个 `PageResourceBundle`（见 [`resources.py:20-31`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/resources.py#L20-L31)），它持有「根字体规格 + 根 XObject 表 + 字体解析器」。后续 text positioner 通过 `resource_bundle.get_font(xobject_path, font_name)` 按需取字体（[`text_positioning.py:34`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/text_positioning.py#L34)）。

> 关键结论：会话层只管「按页驱动 + 装配资源束」，**字体怎么造、怎么缓存全部封装在 `resource_runtime` 里**。会话和字体后端是松耦合的。

#### 4.2.4 代码实践

**实践目标**：用「源码阅读」方式把会话编排链路画出来，巩固对调用顺序的理解。

**操作步骤**：

1. 打开 [`native_parse.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_parse.py)，找到产品入口 `parse_prepared_pdf_with_new_parser_to_legacy_ir`（约 L39-75）。
2. 跟着它读：`create_active_font_resource_runtime()` → `load_prepared_pdf_pages(...)` → `create_native_page_interpreter(sink, NativeTextRunPositioner(), resource_runtime, config)` → `run_prepared_pages(...)`。
3. 对照 [`native_page_execution_session.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_page_execution_session.py) 与 [`page_execution_session.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/page_execution_session.py) 里的 `run()`，确认「每页 → build_page_resource_bundle → interpret → emit」这个循环。

**需要观察的现象**：产品入口与脚本入口最终都汇聚到「`resource_runtime` + `positioner` + 逐页 interpret」这一套，区别只在 config 与 PDF 文档的来源（复用 vs 自开）。

**预期结果**：你能用一张图把 `parse_prepared_pdf_with_new_parser_to_legacy_ir` 到 `sink.create_il()` 的每一步及所在文件标注清楚。

#### 4.2.5 小练习与答案

**练习**：`NativePageExecutionSession` 为什么把 `create_resource_runtime` 设计成可注入的字段，而不是写死 `create_active_font_resource_runtime`？

**参考答案**：为了让字体运行时**可替换**。默认走 active 直接构造器，但测试或实验中可以注入一个「假的」或「基于 pdfminer 的」运行时，而不必改动会话主体和解释器。这是依赖注入带来的可测试性与可扩展性，也是 active 体系「可换部件」思想的体现（呼应 u1-l3 里「docvision/translator 可换」的设计哲学）。

---

### 4.3 字体解析与适配：ActiveFontFactory → active_direct_font_backend 的五类字体分派

#### 4.3.1 概念说明

经过 4.1 的投影，我们拿到了后端无关的 `runtime_spec`。现在要把它变成一个真正能回答度量问题的「运行时字体」。这一步由两个角色完成：

- **`ActiveFontFactory`**（工厂）：实现 `RuntimeFontFactory` 协议，唯一方法 `create_font(objid, runtime_spec)`，内部直接转交给 `construct_active_direct_runtime_font`。
- **`active_direct_font_backend`**（直接构造器）：按 `Subtype` 分派，构造出五个运行时字体后端类之一。

「适配」二字来自 `ActiveFontAdapter`：它把上面造出来的后端包一层，对外暴露统一的 `PdfRuntimeFontLike` 接口，并附加 `xobj_id`/`legacy_descent`/`font_id_temp` 三个适配字段。下游（text positioner、IL sink）只认 adapter，不直接碰后端。

#### 4.3.2 核心流程

构造分派逻辑（`_construct_active_direct_pdfminer_font`）：

```text
classify_font_subtype(runtime_spec)  → 读取 "Subtype"
  │
  ├─ "Type1" / "MMType1"   → _build_simple_font_backend(try_font_metrics=True)
  │                          → ActiveSimpleRuntimeFontBackend
  ├─ "TrueType"            → _build_simple_font_backend(try_font_metrics=True)
  │                          → ActiveSimpleRuntimeFontBackend
  ├─ "Type3"               → _build_type3_font_backend
  │                          → ActiveType3RuntimeFontBackend
  ├─ "CIDFontType0" / "CIDFontType2" → _build_cid_font_backend
  │                          → ActiveCIDRuntimeFontBackend
  ├─ "Type0"               → _construct_type0_font:
  │       取 DescendantFonts[0]，把根的 Encoding/ToUnicode 复制给后代，
  │       再递归 _construct_active_direct_pdfminer_font(后代 spec)
  │       （后代通常是 CIDFont → ActiveCIDRuntimeFontBackend；
  │        若后代缺 Subtype，兜底当 Type1 处理）
  └─ 其它                  → 返回 None（工厂层会抛 NotImplementedError）
```

三个后端类都实现同一套方法（`PdfRuntimeFontLike` 协议，[`font_types.py:24-29`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/font_types.py#L24-L29)）：

| 方法 | 含义 | 简单字体 | CID 字体 |
|---|---|---|---|
| `decode(data)` | 把内容流字节解码成 CID 列表 | 直接 `bytearray` | 走 CMap（如 Identity-H） |
| `unicode_text(cid, fb)` | CID → Unicode 文本 | cid2unicode 字典 | ToUnicode / unicode_map |
| `is_multibyte()` | 是否多字节编码 | 恒 `False` | 恒 `True` |
| `is_vertical()` | 是否竖排 | 恒 `False` | 由 CMap 决定 |
| `char_width(cid)` | 字宽（已 ×0.001） | Widths 表 / 度量 DB | W 表 / DW |
| `char_disp(cid)` | 字形位移（竖排用） | 0 | W2 表 |
| `get_descent()` | 基线下深度 | descent×0.001 | descent×0.001 |
| `runtime_identity()` | 缓存身份（见 4.4） | `id(self)` | `id(self)` |
| `compute_encoding_length(...)` | 每个 CID 占几个字节 | 恒 1 | 由 Encoding/CMap 推断（1 或 2） |

**编码长度的推断**值得单独说：对 CID 字体，`compute_encoding_length`（[`active_direct_font_backend.py:247-274`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_direct_font_backend.py#L247-L274)）会优先用 pymupdf 的 `xref_get_key` 读 `Encoding`：`Identity-H/V` → 2，`WinAnsiEncoding` → 1；否则解析 ToUnicode CMap 的 `begincodespacerange`，用码段十六进制位数算：

\[
\text{encoding\_length} = \frac{\text{len}(\text{code\_range\_hex\_digits})}{2}
\]

例如码段 `<0000> <FFFF>` 是 4 位十六进制 → 长度 2。这套值最终被 IL sink 的 `_compute_font_encoding_length`（[`il_creater_active.py:683-692`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py#L683-L692)）用来正确切分文本字节。

#### 4.3.3 源码精读

工厂 [`ActiveFontFactory.create_font`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_font_backend.py#L13-L23)：忽略 objid（缓存键由上层 `resolve_active_font_map` 管），直接调 `construct_active_direct_runtime_font`，失败则抛 `NotImplementedError`。

分派核心 [`_construct_active_direct_pdfminer_font`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_direct_font_backend.py#L283-L295) 就是一串 `if subtype in (...)`：

```python
# active_direct_font_backend.py:283-295  —— 按 Subtype 分派到五个构造函数
subtype = classify_font_subtype(runtime_spec)
if subtype in ("Type1", "MMType1"):
    return _construct_type1_font(runtime_spec)      # → ActiveSimpleRuntimeFontBackend
if subtype == "TrueType":
    return _construct_truetype_font(runtime_spec)   # → ActiveSimpleRuntimeFontBackend
if subtype == "Type3":
    return _construct_type3_font(runtime_spec)      # → ActiveType3RuntimeFontBackend
if subtype in ("CIDFontType0", "CIDFontType2"):
    return _construct_cid_font(runtime_spec)        # → ActiveCIDRuntimeFontBackend
if subtype == "Type0":
    return _construct_type0_font(runtime_spec)      # → 递归到后代 CIDFont
return None
```

`Type0` 的处理最能体现「外壳 + 后代」结构（[`_construct_type0_font`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_direct_font_backend.py#L320-L334)）：取 `DescendantFonts[0]`，把根的 `Encoding`/`ToUnicode` 复制进后代 spec，再递归调用自己；若后代连 `Subtype` 都没有（损坏的 PDF），就兜底当 Type1 处理——这是为了「不丢文本」的容错，注释里明确说这是在复刻 pdfminer 的兜底行为。

简单字体的度量来源在 [`_build_simple_font_backend`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_direct_font_backend.py#L337-L397)：

```python
# active_direct_font_backend.py:347-358  —— 优先用内置 AFM 度量库，否则用 spec 里的 Widths
if try_font_metrics:
    try:
        metrics_descriptor, metrics_widths = FontMetricsDB.get_metrics(basefont)  # 内置 Adobe Core 35
        descriptor = dict(metrics_descriptor)
        widths = dict(metrics_widths)
    except KeyError:
        widths = _build_spec_widths(spec)   # 字典里没这个字体名 → 读 spec 的 FirstChar/Widths
```

`FontMetricsDB.get_metrics`（[`font_data_runtime.py:23-26`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/runtime/font_data_runtime.py#L23-L26)）查的是内置的 `FONT_METRICS`（Adobe Core 35 AFM，含 Helvetica/Courier/Times 等及其别名 Arial/CourierNew，见 `runtime/data/fontmetrics.py`）。这就是为什么一个没嵌入字体文件的 `Helvetica` 也能算出正确字宽。

CID 字体的构造 [`_build_cid_font_backend`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_direct_font_backend.py#L462-L573) 最重：要构建 CID CMap（`build_cid_cmap`）、若嵌入 `FontFile2` 则用 `TrueTypeFont` + FreeType 抽字形（`build_cid_unicode_map`）、根据横竖排分别取 `W`/`W2` 宽度表与 `DW`/`DW2` 默认宽。这是整个字体构造里最贵的一步，也直接解释了 4.4 为什么必须缓存。

适配器 [`ActiveFontAdapter`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_font_runtime.py#L12-L59) 是个薄壳：所有度量方法（`decode`/`char_width`/...）都原样转发给 `self.backend`，并用 `__getattr__`（L58-59）兜底转发未显式声明的方法。它在 backend 之上只多了三个字段：`xobj_id`（字体所在 XObject id）、`legacy_descent`（原始 descent 快照）、`font_id_temp`（临时字体 id）。下游 text positioner 读 `font_id_temp` 作为字体标识（[`text_positioning.py:70`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/text_positioning.py#L70)）。

> 关键结论：**构造器是后端无关的纯 Python**（只吃 `runtime_spec` 字典），唯一的后端耦合是 `compute_encoding_length` 可选用 pymupdf 的 `xref_*` API（且有纯 Python 兜底）。这保证同一套字体语义既能跑在 pymupdf 取来的 spec 上，也能复刻 pdfminer 的行为。

#### 4.3.4 代码实践

**实践目标**：亲手用最小 spec 造一个运行时字体，观察它的类型与度量；并回答「active_direct_font_backend 处理哪几类字体」。

**操作步骤**（示例代码）：

```python
# demo_font_construct.py —— 示例代码，非项目原有文件
from babeldoc.format.pdf.new_parser.active_direct_font_backend import (
    construct_active_direct_runtime_font,
)
from babeldoc.format.pdf.new_parser.active_font_backend import ActiveFontFactory
from babeldoc.format.pdf.new_parser.active_font_runtime import ActiveFontResolver
from babeldoc.format.pdf.new_parser.prepared_page import PreparedFontSpec

# 1) 最小 Type1 Helvetica spec（Helvetica 在内置 FONT_METRICS 里）
spec = {"Subtype": "Type1", "BaseFont": "Helvetica"}
font = construct_active_direct_runtime_font(spec)
print("backend 类型:", type(font).__name__)   # 期望 ActiveSimpleRuntimeFontBackend
print("fontname:", font.fontname)             # 期望 Helvetica
print("is_multibyte:", font.is_multibyte())   # 期望 False（简单字体）
print("'A' 的字宽:", font.char_width(65))      # 期望约 0.556（556/1000）

# 2) 用工厂 + 解析器走完整链路
factory = ActiveFontFactory()
resolver = ActiveFontResolver(font_factory=factory)
fs = PreparedFontSpec(name="F1", objid=1, spec=spec)
m = resolver.resolve_font_map((fs,))
print("adapter 类型:", type(m["F1"]).__name__) # 期望 ActiveFontAdapter
print("adapter.char_width(65):", m["F1"].char_width(65))
```

**需要观察的现象**：Type1 字体落到了 `ActiveSimpleRuntimeFontBackend`；`char_width(65)` 返回的是已 ×0.001 的小数（'A' 在 Helvetica AFM 里是 556）。

**预期结果**：`ActiveSimpleRuntimeFontBackend` / `Helvetica` / `False` / 约 `0.556`。（具体数值待本地验证。）

> 这个实践同时回答了本讲的两个核心问题：**处理哪几类字体**（Type1/MMType1、TrueType、Type3、CIDFontType0/CIDFontType2、Type0 共五大类，分别落在 `ActiveSimpleRuntimeFontBackend`/`ActiveType3RuntimeFontBackend`/`ActiveCIDRuntimeFontBackend` 三个后端类）；**为什么用 adapter**（统一接口 + 附加 `xobj_id`/`legacy_descent`/`font_id_temp`，让下游与具体后端解耦）。

#### 4.3.5 小练习与答案

**练习 1**：`Type0` 字体为什么没有自己专属的后端类？

**参考答案**：`Type0` 只是一个「外壳」，真正承载字形与度量的是它 `DescendantFonts` 里的 CID 后代字体。所以代码把根的 `Encoding`/`ToUnicode` 复制给后代 spec 后，递归调用 `_construct_active_direct_pdfminer_font`，最终落到后代的 `ActiveCIDRuntimeFontBackend`。只有当后代 spec 损坏（缺 `Subtype`）时才兜底当 Type1。

**练习 2**：`char_width` 在简单字体里「先按 int 查、再按 unicode 字符查」，为什么要有两条路径？

**参考答案**：内置 AFM 度量库的 widths 表是按**字形名/字符**键存的（如 `"A": 556`），而 `char_width(cid)` 收到的是整数 CID。所以先 `widths.get(cid)`（int）碰运气，不行就把 cid 经 `unicode_text` 转成字符 `"A"` 再 `widths.get("A")` 查。两条路径兼容「按码索引」和「按字形名索引」两种宽度表来源。

---

### 4.4 字体资源缓存：runtime_cache 与 legacy_descents 的两级缓存

#### 4.4.1 概念说明

`ActiveFontResolver` 的核心问题是：**同一份文档里，同一个字体会在很多页上反复出现**（正文字体可能每页都用）。而造一个字体（尤其 CID 字体）非常贵——要解析 CMap、跑 FreeType、构建宽度表。如果每页都重造，性能不可接受。

所以 `ActiveFontResolver` 维护**两级缓存**：

1. **`runtime_cache`（跨页共享）**：key 是字体 `objid`，value 是造好的 `backend` 对象。整个文档只造一次，所有页复用同一个 backend。这个缓存由 `ActiveFontResourceRuntime` 持有，跨页传递。
2. **`legacy_descents`（每页独立）**：key 是 `(objid 或 backend id, 字体名)`，value 是该字体**在本页首次出现时**的原始 descent。每个 `ActiveFontResolver`（即每页）新建一个空的 `legacy_descents`。

为什么 descent 要单独搞一套每页缓存？因为 backend 是跨页共享的可变对象，它的 `.descent` 字段会在文本定位时被改成 0（对齐 pdfminer 的行为），但 IL sink 在投影字体元数据时又需要原始 descent。于是用 `legacy_descents` 把「本页第一次见到这个字体时的原始 descent」记下来，后续在本页内可随时还原。

#### 4.4.2 核心流程

`resolve_active_font_map`（每次 resolve 一个字体规格时）：

```text
for font_spec in font_specs:
  cache_key = font_spec.objid
  backend = runtime_cache.get(cache_key)         # ① 跨页缓存命中？
  if backend is None:
      runtime_spec = project_font_spec(font_spec.spec, resolve_indirect=...)  # 投影
      backend = font_factory.create_font(font_spec.objid, runtime_spec)        # 贵！造一次
      runtime_cache[cache_key] = backend          # ② 存入跨页缓存
  # —— 至此 backend 一定是同一个共享对象 ——
  descent_key = (font_spec.objid 或 id(backend), font_spec.name)
  if descent_key not in legacy_descents:
      legacy_descents[descent_key] = backend.descent   # ③ 记下本页的原始 descent
  backend.descent = 0                            # ④ 文本定位用 0 descent（pdfminer 行为）
  result[font_spec.name] = ActiveFontAdapter(
      backend=backend,
      xobj_id=font_spec.objid,
      legacy_descent=legacy_descents[descent_key],   # ⑤ adapter 带上原始 descent 快照
  )
```

跨页缓存的生命周期由 `ActiveFontResourceRuntime` 管理（[`active_font_resource_runtime.py:24-32`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_font_resource_runtime.py#L24-L32)）：它在自身持有 `_runtime_cache`，**每次建页都把它传给一个全新的 `ActiveFontResolver`**，而 `legacy_descents` 是 resolver 自己 `default_factory=dict` 新建的（每页空）。于是：

- `runtime_cache` 跨页 → backend 全文档共享（省掉重复构造）。
- `legacy_descents` 每页新建 → 每页各自快照 descent（隔离 `backend.descent = 0` 的副作用）。

IL sink 侧的还原：`register_native_font_resources`（[`il_creater_active.py:439-462`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py#L439-L462)）在投影字体元数据前，会把 `font.descent` 临时还原成 `legacy_descent`，投影完再恢复成 0：

```python
# il_creater_active.py:447-462  —— 投影字体时用原始 descent，平时用 0
original_descent = font.descent
font_key = (... font.runtime_identity())          # 用 runtime_identity 算去重 key
if font_key not in emitted_font_keys:
    font.descent = getattr(font, "legacy_descent", font.descent)   # 还原
    emitted_font_keys.add(font_key)
self.on_page_resource_font(font, ...)             # 投影进 IL
font.descent = original_descent                   # 恢复
```

而 `_projected_font_resource_cache_key`（[`il_creater_active.py:643-659`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py#L643-L659)）用 `(xref_id, xobj_id, objid, font_id, runtime_identity(), fontname, ascent, descent)` 作为第三级缓存键——这是 IL sink 自己投影 PdfFont 时的去重，`runtime_identity()` 在这里是「这个 backend 对象是谁」的身份标识（后端类里返回 `id(self)`）。

#### 4.4.3 源码精读

缓存核心 [`resolve_active_font_map`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_font_runtime.py#L70-L101)：

```python
# active_font_runtime.py:80-100  —— 跨页缓存 + 每页 descent 快照
cache_key = font_spec.objid
backend = runtime_cache.get(cache_key) if cache_key is not None else None
if backend is None:
    runtime_spec = project_font_spec(font_spec.spec,
                                     resolve_indirect=font_spec.resolve_indirect)
    backend = font_factory.create_font(font_spec.objid, runtime_spec)
    if cache_key is not None:
        runtime_cache[cache_key] = backend          # 跨页缓存：造一次，处处复用
descent_root = font_spec.objid if font_spec.objid is not None else id(backend)
descent_key = (descent_root, font_spec.name)
if descent_key not in legacy_descents:
    legacy_descents[descent_key] = backend.descent  # 每页首次：记原始 descent
backend.descent = 0                                 # 改写为 0（文本定位口径）
font = ActiveFontAdapter(backend=backend, xobj_id=font_spec.objid,
                         legacy_descent=legacy_descents[descent_key])
result[font_spec.name] = font
```

`ActiveFontResolver`（[`active_font_runtime.py:104-116`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_font_runtime.py#L104-L116)）把 `legacy_descents` 和 `runtime_cache` 都做成 `default_factory=dict` 字段。注意：`runtime_cache` 虽然是 resolver 的字段，但 `ActiveFontResourceRuntime` 每次建页都**显式把自己的 `_runtime_cache` 传进去**（[`active_font_resource_runtime.py:29-32`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_font_resource_runtime.py#L29-L32)），而 `legacy_descents` 不传 → 每页新生成。这就是「跨页共享 backend，每页独立 descent」的实现技巧。

代码里的注释（[`active_font_resource_runtime.py:24-28`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_font_resource_runtime.py#L24-L28)）把意图说得很清楚：跨页缓存让「直接构造的字体保留和兼容路径相同的、不断演化的后端状态」，而 `legacy_descents` 留在每页 resolver 内部，是为了「每页都快照字体当前的 descent」。

> 关键结论：缓存不是「锦上添花」而是「必需」。CID 字体的构造涉及 CMap 解析 + FreeType 取字形，开销巨大；同一字体在文档里出现几十次，不缓存会让解析慢到不可用。而 descent 的每页快照，则是为了在「共享可变 backend」与「每页独立度量口径」之间取得平衡。

#### 4.4.4 代码实践

**实践目标**：验证 `runtime_cache` 的「同一 objid → 同一 backend 对象」身份复用，并解释为什么需要缓存。

**操作步骤**（示例代码）：

```python
# demo_font_cache.py —— 示例代码，非项目原有文件
from babeldoc.format.pdf.new_parser.active_font_backend import ActiveFontFactory
from babeldoc.format.pdf.new_parser.active_font_runtime import ActiveFontResolver
from babeldoc.format.pdf.new_parser.active_font_resource_runtime import (
    create_active_font_resource_runtime,
)
from babeldoc.format.pdf.new_parser.prepared_page import (
    PreparedFontSpec,
    PreparedPageResources,
)

spec = {"Subtype": "Type1", "BaseFont": "Helvetica"}

# 1) 直接用 resolver：同一个 resolver 连续 resolve 同一 objid 的两个 spec
resolver = ActiveFontResolver(font_factory=ActiveFontFactory())
fs_a = PreparedFontSpec(name="F1", objid=7, spec=spec)
fs_b = PreparedFontSpec(name="F1", objid=7, spec=spec)   # 同 objid
ma = resolver.resolve_font_map((fs_a,))
mb = resolver.resolve_font_map((fs_b,))
print("同一 resolver 内 backend 复用:", ma["F1"].backend is mb["F1"].backend)  # 期望 True

# 2) 跨 resolver（模拟跨页）：用 ActiveFontResourceRuntime 共享 runtime_cache
rt = create_active_font_resource_runtime()
tree1 = PreparedPageResources(root_font_specs=(fs_a,), xobject_map={})
tree2 = PreparedPageResources(root_font_specs=(fs_a,), xobject_map={})
b1 = rt.build_page_resource_bundle(tree1)
b2 = rt.build_page_resource_bundle(tree2)
f1 = b1.get_font((), "F1")
f2 = b2.get_font((), "F1")
print("跨页(跨 bundle) backend 复用:", f1.backend is f2.backend)  # 期望 True
print("每页 legacy_descent 独立:", f1.legacy_descent == f2.legacy_descent)  # 值相等，但来自各自 resolver
```

**需要观察的现象**：`backend is backend` 两次都为 `True`——证明昂贵的字体对象只造了一次，跨页复用同一个对象。

**预期结果**：两处 `True`。（具体待本地验证；若 objid 传 `None`，缓存键为 `None` 会跳过缓存，复用断言会变 `False`——这正是代码里 `if cache_key is not None` 守卫的含义。）

**回答「为什么需要缓存」**：构造一个字体要投影 spec、（CID 字体还要）解析 CMap、对嵌入 FontFile2 跑 FreeType 抽字形、构建宽度/位移表——这些都是 O(字体大小) 的重活。文档里同一字体在几十页上重复引用，不缓存就会重复造几十遍；`runtime_cache` 用 `objid` 当键保证「造一次、处处复用」，是性能刚需。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `runtime_cache` 跨页共享，而 `legacy_descents` 每页新建？

**参考答案**：`runtime_cache` 缓存的是「昂贵且不变」的字体后端对象（CMap、字形表等构造好就不变），跨页共享能避免重复构造，所以由 `ActiveFontResourceRuntime` 长期持有并传给每页的 resolver。`legacy_descents` 记录的是「本页首次见到该字体时的原始 descent」，而 `backend.descent` 会被改写成 0 用于文本定位——这是可变状态，若跨页共享会被互相污染，所以每页 resolver 自己新建一个空字典，隔离副作用。

**练习 2**：如果某字体的 `objid` 为 `None`（比如 `resources.py` 里兜底造的 UNKNOW 字体），缓存还能命中吗？

**参考答案**：不能。`resolve_active_font_map` 用 `if cache_key is not None` 守卫，`cache_key=None` 时既不查也不存 `runtime_cache`，每次都会重新调 `create_font`。兜底字体的 spec 极简（`{"Subtype": "Type1", "BaseFont": name}`），构造很便宜，所以不缓存也无妨；这是一种「只对真实 objid 字体做重缓存」的合理取舍。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「**追踪一个字体从 PDF 对象到 IL 的完整旅程**」的任务。

**任务**：选一份真实 PDF（可用 `examples/ci/test.pdf`），在 `--debug` 模式下翻译一次，然后结合源码完成下面四步追踪表：

| 阶段 | 发生在哪 | 关键函数/类 | 数据形态变化 |
|---|---|---|---|
| ① 取字体对象 | pymupdf 装页 | `load_prepared_pdf_pages` → `PreparedFontSpec` | PDF xref → `spec`(dict，可能含间接引用) |
| ② 投影 | active 投影器 | `project_font_spec` | spec → `runtime_spec`(无引用纯数据) |
| ③ 构造 | 直接构造器 | `_construct_active_direct_pdfminer_font` | runtime_spec → `Active*RuntimeFontBackend` |
| ④ 适配 + 缓存 | 字体解析器 | `resolve_active_font_map` → `ActiveFontAdapter` | backend(跨页缓存) + legacy_descent(每页) |
| ⑤ 消费 | text positioner / IL sink | `NativeTextRunPositioner` / `register_native_font_resources` | adapter → AWLTChar / PdfFont(IL) |

**操作步骤**：

1. 运行 `babeldoc --debug --files examples/ci/test.pdf --openai ...`（具体参数见 u1-l2），让流水线产出 `--debug` 快照。
2. 在 [`active_direct_font_backend.py:283-295`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_direct_font_backend.py#L283-L295) 的分派处，对照你这份 PDF 用到的字体 Subtype，判断每个字体分别走了哪个构造分支。
3. 在 [`active_font_runtime.py:80-100`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_font_runtime.py#L80-L100) 的缓存逻辑处，说明「同一正文字体在第 2 页和第 5 页」分别命中了哪一级缓存。
4. 用 4.3.4 / 4.4.4 的示例脚本，单独造出你这份 PDF 里某个 Type1/CID 字体的 backend，验证它的 `is_multibyte`/`char_width` 与你在 PDF 阅读器里看到的字宽是否一致（粗略核对即可）。

**预期结果**：你能用一句话说清「PDF 里一个 `Type0` 中文字体」的完整路径：取对象 → 投影 → `_construct_type0_font` 递归到 CID 后代 → `ActiveCIDRuntimeFontBackend` → 跨页缓存 → adapter → 喂给 positioner 算字形坐标、喂给 IL sink 投影成 `PdfFont`。

> 若无法运行真实翻译，步骤 2–3 可改为纯源码阅读：直接在上述两处加 `print`/`logging`（仅本地调试，勿提交），观察 Subtype 分派与缓存命中。结果「待本地验证」。

## 6. 本讲小结

- **active 投影/惰性模式**：`project_font_spec` 把含间接引用的字体字典递归解析、转成后端无关的 `runtime_spec`（带深度上限与循环检测），是整套体系兼容多后端的根基。
- **解析会话编排**：`run_active_parse_session` / `NativePageExecutionSession` 把 config、sink、resource_runtime 攥在一起按页驱动；字体怎么造全封装在可注入的 `resource_runtime` 里，与会话松耦合。
- **字体解析与适配**：`ActiveFontFactory` → `_construct_active_direct_pdfminer_font` 按 `Subtype` 分派处理五大类字体（Type1/MMType1、TrueType、Type3、CIDFontType0/2、Type0），落到三个后端类（Simple/Type3/CID），再被 `ActiveFontAdapter` 统一包装。
- **两级缓存**：`runtime_cache` 跨页共享昂贵的 backend 对象（按 objid），`legacy_descents` 每页独立快照原始 descent——前者省重复构造，后者隔离 `descent=0` 的可变副作用。
- **双后端兼容**：构造器是后端无关的纯 Python（只吃字典），唯一耦合是 `compute_encoding_length` 可选用 pymupdf `xref_*`（有纯 Python 兜底）；整套机制实现「pymupdf 取页面对象 + pdfminer 语义算字体度量」共存。

## 7. 下一步学习建议

本讲讲完了 PDF 解析前端（u4 全单元）的最后一环——运行时字体。至此「PDF → IL」的 frontend 已经完整闭环。建议下一步：

1. **横向收口 frontend**：回头重读 u4-l1 的 `parse_prepared_pdf_with_new_parser_to_legacy_ir`，现在你应该能把「装页 → 投影字体 → 构造 backend → 缓存 → positioner 算字形 → sink 投影 IL」整条链路一气讲完。
2. **进入 midend**：frontend 产出的 `Document`（IL）接下来交给中端处理。下一单元（u5）从 `DetectScannedFile` 开始，按 `TRANSLATE_STAGES` 顺序逐阶段加工 IL——建议先读 u5-l1（扫描检测）与 u5-l3（段落识别），它们直接消费本讲产出的 `PdfCharacter`/`PdfFont`。
3. **深入字体度量细节**（可选）：若对 CID CMap、ToUnicode、FreeType 抽字形感兴趣，可继续读 `new_parser/runtime/` 下的 `cid_cmap_runtime.py`、`font_unicode_maps.py`、`to_unicode_parser_runtime.py`，它们是本讲 `ActiveCIDRuntimeFontBackend` 的底层支撑。
