# IL 的序列化：XMLConverter 与 schema

> 前置讲义：本讲承接 [u3-l1 IL 数据模型](u3-l1-il-data-model.md)。u3-l1 讲清了 IL 是「一棵带坐标的对象树」及其实体层级；本讲回答另一个问题：**这棵树在内存里，怎么变成磁盘上可读、可存、可对照 schema 看懂的文本？** 这就是「序列化」。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `XMLConverter` 的七个方法各自做什么、分别用 `xsdata` 还是 `orjson` 实现；
- 把一份 IL 的 JSON 调试输出（如 `create_il.debug.json`）对照 `il_version_1.rnc` / `.xsd` schema，找到 Page、Paragraph 与它们的子节点；
- 解释 `--debug` 模式下流水线为什么会产出一系列「阶段快照」JSON 文件，以及它们各自对应哪个 midend 阶段；
- 区分仓库自带的 `examples/*.xml`（DPML 格式）与真正的 IL XML，并能做概念上的对照。

## 2. 前置知识

### 2.1 什么是「序列化」

把内存里的 Python 对象，按某种规则写成一段文本（XML、JSON、字节流）的过程叫**序列化（serialization）**；反过来把文本还原成对象叫**反序列化（deserialization）**。

BabelDOC 的 IL 在内存里是一堆带 `@dataclass(slots=True)` 的 Python 对象（见 `il_version_1.py`）。调试和排查问题时，你需要把它「打印出来看」，这就是序列化的主要用途。

### 2.2 两个序列化库

BabelDOC 的 `XMLConverter` 同时用到了两个库：

- **xsdata**：一个可以把 Python dataclass 与 XML 互转的库。它读取 dataclass 字段上的 `metadata` 注解（`"type": "Element"` / `"Attribute"`、`"name"` 等），自动决定 XML 里用标签还是属性、标签叫什么名字。
- **orjson**：一个高性能 JSON 库，原生支持把 dataclass 实例序列化成 JSON。

> 直觉：**XML 路径（xsdata）**严格、带 schema 约束，适合「按契约读写」；**JSON 路径（orjson）**轻快、适合「打印出来给人看」。BabelDOC 的实际调试输出走的是 JSON 这条路。

### 2.3 schema 是什么

**schema（模式）**是一份「这份文档长什么样」的契约：规定有哪些标签、标签里能嵌套什么、属性叫什么名字、类型是整数还是字符串。BabelDOC 用了两种 schema 语言来表达同一份 IL 契约：

- `il_version_1.rnc`：**RELAX NG Compact** 语法，给人读的、简洁；
- `il_version_1.xsd`：**W3C XML Schema** 语法，给机器读的、啰嗦但通用。

而 `il_version_1.py`（带 `@dataclass` 的 Python 类）就是这份契约「编译」出来的可执行版本——它们三者描述的是同一棵 IL 树。

---

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [xml_converter.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/xml_converter.py) | 本讲主角：`XMLConverter` 类，IL 的双向序列化器（XML + JSON）。 |
| [il_version_1.rnc](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.rnc) | IL 的 RELAX NG schema（人读）。 |
| [il_version_1.xsd](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.xsd) | IL 的 W3C XML Schema（机器读）。 |
| [il_version_1.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py) | 由 schema 生成的 dataclass，`XMLConverter` 直接操作它。 |
| [high_level.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py) | 调用方：在 `--debug` 模式下，每个 midend 阶段后用 `write_json` 落盘快照。 |
| [il_creater_active_support.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active_support.py) | `LazyPassthroughInstruction`，解释 JSON 序列化为何需要特殊回调。 |
| [examples/basic.xml](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/examples/basic.xml) / [examples/complex.xml](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/examples/complex.xml) | DPML 示例（**注意：非 IL 格式**），用于概念对照。 |

---

## 4. 核心概念与源码讲解

### 4.1 XMLConverter：IL 的双向序列化器

#### 4.1.1 概念说明

`XMLConverter` 是 IL 与外部世界之间的「翻译官」。它提供两类能力：

1. **XML 双向转换**：用 xsdata 把 `il_version_1.Document` 写成 XML、或把 XML 读回 `Document`；
2. **JSON 单向输出**：用 orjson 把 `Document` 写成 JSON（**只写不读**——没有 `read_json`）。

为什么 JSON 只写不读？因为 BabelDOC 的 JSON 输出**只服务于调试**：让你看到某一时刻 IL 长什么样，并不需要再把 JSON 吃回来。真正的「可逆持久化」走 XML（有 schema 保证结构正确）。

#### 4.1.2 核心流程

`XMLConverter` 的七个方法可分成三组：

```
XMLConverter
├── XML 读写（xsdata）
│     ├── write_xml(doc, path)   # doc → 写入 .xml 文件
│     ├── read_xml(path)         # .xml 文件 → doc
│     ├── to_xml(doc)            # doc → XML 字符串
│     └── from_xml(xml)          # XML 字符串 → doc
├── JSON 只写（orjson）
│     ├── to_json(doc)           # doc → JSON 字符串
│     └── write_json(doc, path)  # doc → 写入 .json 文件
└── 深拷贝
      └── deepcopy(doc)          # doc → doc（内存复制，注释掉了 XML 往返方案）
```

关键点：

- **构造时**一次性建好 xsdata 的 `XmlParser` / `XmlSerializer`，复用避免重复初始化开销；
- **JSON 输出**带三个 orjson 选项：换行结尾、缩进 2 空格、按 key 排序（`OPT_SORT_KEYS` 让调试输出稳定、可 diff）；
- **遇到无法识别的对象**时，用一个 `default` 回调兜底（见 4.1.3 的 `_orjson_default`）。

#### 4.1.3 源码精读

**导入与两个底层库**：文件顶部同时引入 xsdata（XML）与 orjson（JSON），并引入 IL 模型与一个特殊类型 `LazyPassthroughInstruction`：

[xml_converter.py:4-13](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/xml_converter.py#L4-L13) —— 引入 xsdata 的 `XmlParser`/`XmlSerializer`/`SerializerConfig`、orjson，以及 IL 模型 `il_version_1` 和 `LazyPassthroughInstruction`。

**构造函数**：建好复用的解析器与带 2 空格缩进配置的序列化器：

[xml_converter.py:22-27](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/xml_converter.py#L22-L27) —— `__init__` 创建 `XmlParser()` 与 `XmlSerializer(context=..., config=SerializerConfig(indent="  "))`。

**XML 四件套**：`to_xml` 直接调 `serializer.render`，`from_xml` 调 `parser.from_string(xml, il_version_1.Document)`——注意反序列化时**显式指定目标类型是 `Document`**，xsdata 才知道把 XML 根标签映射成哪个 dataclass：

[xml_converter.py:37-44](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/xml_converter.py#L37-L44) —— `to_xml` 调 `self.serializer.render(document)`；`from_xml` 调 `self.parser.from_string(xml, il_version_1.Document)`。

**JSON 输出**：用 orjson，关键在 `default=_orjson_default` 这个兜底回调：

[xml_converter.py:50-61](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/xml_converter.py#L50-L61) —— `to_json` 用 `OPT_APPEND_NEWLINE | OPT_INDENT_2 | OPT_SORT_KEYS` 三个选项，并把 `_orjson_default` 作为 `default`。

**为什么需要 `_orjson_default`**：IL 里有一个字段类型「名不副实」。`GraphicState.passthrough_per_char_instruction` 在 schema 与 dataclass 里声明为 `str | None`：

[il_version_1.py:54-58](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L54-L58) —— `GraphicState.passthrough_per_char_instruction: str | None`。

但运行时它实际装的是 `LazyPassthroughInstruction`——一个「字符串兼容」的延迟求值包装器（`__str__`/`__eq__`/`__hash__` 都让它表现得像字符串）。orjson 不认识这个自定义类，于是交给 `_orjson_default`，后者调它的 `materialize()` 把它变成真正的字符串：

[xml_converter.py:16-19](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/xml_converter.py#L16-L19) —— `_orjson_default`：遇到 `LazyPassthroughInstruction` 就 `materialize()`，否则抛 `TypeError`。

`materialize()` 本身做的事是把昂贵的图形状态渲染推迟到「真正需要字符串时」才执行，并缓存结果：

[il_creater_active_support.py:119-130](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active_support.py#L119-L130) —— `LazyPassthroughInstruction.materialize()` 调 `render_passthrough_snapshot(...)` 拼出最终字符串并缓存到 `self._value`。

> 设计要点：这是「**声明类型 vs 运行时类型**」不匹配的一个真实案例——为了让一个延迟对象能在需要时当字符串用，作者让它「鸭子类型」成 str，再在序列化边界上用 `default` 回调做一次显式落地。这是一处值得学习的工程技巧。

**deepcopy**：注意它实际用的是 `copy.deepcopy`，而「用 XML 往返来实现深拷贝」的那行被注释掉了：

[xml_converter.py:46-48](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/xml_converter.py#L46-L48) —— `deepcopy` 返回 `copy.deepcopy(document)`，注释里保留了 `return self.from_xml(self.to_xml(document))` 的旧方案。

这说明作者**试过**用「序列化→反序列化」来实现深拷贝（一个经典技巧：序列化天然产生一份完全独立的副本），但最终改回了更直接、更快的 `copy.deepcopy`。

#### 4.1.4 代码实践

**实践目标**：亲手跑通 `XMLConverter` 的 JSON 与 XML 路径，观察两者输出的差异。

**操作步骤**（以下为**示例代码**，可直接保存为 `try_xmlconverter.py` 在仓库根目录运行）：

```python
# 示例代码：手动构造一个最小 IL Document 并序列化
from babeldoc.format.pdf.document_il import il_version_1 as il
from babeldoc.format.pdf.document_il.xml_converter import XMLConverter

def make_box():
    return il.Box(x=0.0, y=0.0, x2=612.0, y2=792.0)  # 一页 Letter 尺寸

page = il.Page(
    mediabox=il.Mediabox(box=make_box()),
    cropbox=il.Cropbox(box=make_box()),
    page_number=1,
    unit="pt",
    base_operations=il.BaseOperations(value=""),
)
doc = il.Document(page=[page], total_pages=1)

conv = XMLConverter()

# 1) JSON 路径（orjson，调试用，键已排序）
print(conv.to_json(doc))

# 2) XML 路径（xsdata，标签来自 dataclass 的 Meta.name）
print(conv.to_xml(doc))

# 3) XML 往返：to_xml -> from_xml 应能还原
roundtrip = conv.from_xml(conv.to_xml(doc))
print("roundtrip equal page_number:", roundtrip.page[0].page_number)
```

**需要观察的现象**：

1. `to_json` 输出里顶层是 `{"page": [...], "totalPages": 1}`，注意 Python 字段名 `total_pages` 在 JSON 里变成了 schema 名 `totalPages`；
2. `to_xml` 输出形如 `<document totalPages="1"><page pageNumber="1" Unit="pt">...</page></document>`，标签名 `<document>`/`<page>` 与 `.rnc` 里 `element document`/`element page` 完全对应；
3. `from_xml` 还原后能取回 `page_number`。

**预期结果**：JSON 与 XML 都能成功生成；字段名到 schema 名的映射（`total_pages`→`totalPages`、`page_number`→`pageNumber`）在两种输出里一致。

**待本地验证**：上述脚本依赖 `babeldoc` 已正确安装（`uv tool install` 或开发模式），字段名与实际 dataclass 一致（已对照 `il_version_1.py` 核实，但不同版本可能有增删，运行时若报参数名错误，请以本机 `il_version_1.py` 为准）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `XMLConverter` 没有 `read_json` 方法？

> **参考答案**：JSON 路径只为调试输出服务（`write_json` 只写不读）。把 JSON 还原成 `Document` 不是需求——真正需要「可逆持久化」时走的是有 schema 保护的 XML（`read_xml`）。给一个「只写」用途提供「读」方法会误导使用者以为它能可靠还原，反而危险。

**练习 2**：如果删掉 `to_json` 里的 `default=_orjson_default`，用真实 PDF 跑 `--debug` 会怎样？

> **参考答案**：IL 中 `GraphicState.passthrough_per_char_instruction` 实际装的是 `LazyPassthroughInstruction`，orjson 不认识这个自定义类型，会抛 `TypeError`，导致调试 JSON 写入失败。`_orjson_default` 正是兜底：遇到它就 `materialize()` 成字符串。

**练习 3**：`deepcopy` 方法里被注释掉的 `return self.from_xml(self.to_xml(document))` 是什么思路？为什么被弃用？

> **参考答案**：这是「用序列化往返实现深拷贝」的经典技巧——序列化必然产生与原对象无引用关联的全新副本。弃用的原因应是性能：一次 XML 序列化 + 反序列化（字符串拼接 + xsdata 解析）比 `copy.deepcopy` 直接复制对象树慢得多。

---

### 4.2 il_version_1.rnc / .xsd：IL 的结构契约

#### 4.2.1 概念说明

`.rnc`（RELAX NG Compact）和 `.xsd`（W3C XML Schema）是**同一份 IL 契约的两种写法**：

- `.rnc` 简洁、像伪代码，适合人读、人改；
- `.xsd` 冗长但通用，能被各种 XML 工具和验证器消费。

`il_version_1.py`（带 dataclass 的 Python 类）是从这份契约**生成**出来的可执行版本。所以读 `.rnc` 就等于在读「IL 到底有哪些实体、字段、嵌套关系」的权威定义——这比读生成的 `il_version_1.py`（一千多行）轻松得多。

> 与 u3-l1 的关系：u3-l1 用「数据模型」的视角讲了实体层级；本节用「schema 语法」的视角讲**同一棵树**，两者互为印证。

#### 4.2.2 核心流程

读懂 `.rnc` 只需掌握几个符号：

| 语法 | 含义 |
| --- | --- |
| `element document { ... }` | 定义一个名为 `document` 的 XML 标签 |
| `Page+` | 该位置必须出现 **1 个或多个** Page |
| `PDFCharacter*` | 该位置可出现 **0 个或多个** PDFCharacter |
| `attribute x { xsd:float }` | 定义一个名为 `x`、类型为浮点数的**属性** |
| `... ?` | 前面的元素/属性**可选** |
| `A | B | C` | **多选一**（联合） |
| `list { xsd:float, ... }` | 一个**空格分隔的列表**属性（如变换矩阵的 6 个数） |

整份 schema 是自顶向下定义的：从 `Document` 开始，逐层展开到 `Page`、再到 `PDFCharacter`/`PDFParagraph` 等。

#### 4.2.3 源码精读

**根节点 Document**：一棵 IL 树只有一个根 `<document>`，含 1 个或多个 `Page`，外加一个整数属性 `totalPages`：

[il_version_1.rnc:1-6](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.rnc#L1-L6) —— `start = Document`；`Document = element document { Page+, attribute totalPages { xsd:int } }`。

对照 dataclass：`Document.Meta.name = "document"`，字段 `total_pages` 的 `metadata` 里 `name="totalPages"`、`type="Attribute"`，正好对应 schema：

[il_version_1.py:1353-1371](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L1353-L1371) —— `class Document` 内 `Meta.name = "document"`；`page` 为 `min_occurs=1` 的 Element；`total_pages` 映射为属性 `totalPages`。

**Page 容器**：一页里**并列**挂着多个集合（xobject、版面区域、矩形、字体、段落、图、字符、曲线、表单），用 `*` 表示可有可无、可重复：

[il_version_1.rnc:7-23](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.rnc#L7-L23) —— `Page = element page { element mediabox {...}, element cropbox {...}, PDFXobject*, PageLayout*, PDFRectangle*, PDFFont*, PDFParagraph*, PDFFigure*, PDFCharacter*, PDFCurve*, PDFForm*, attribute pageNumber {...}, attribute Unit {...}, element baseOperations {...} }`。

这正对应 u3-l1 讲的「Page 是一个下挂九个并列集合的容器」。`mediabox`/`cropbox`/`baseOperations` 没有 `?`/`*`，是**必填**的——所以 4.1.4 的示例代码必须给 `mediabox`/`cropbox`/`baseOperations` 赋值。

**PdfCharacter**：字符是字级最小单位，`char_unicode` 是必填属性，`PDFStyle` 与 `Box` 是必填子元素，`visual_bbox` 可选：

[il_version_1.rnc:67-83](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.rnc#L67-L83) —— `PDFCharacter = element pdfCharacter { attribute vertical {...}?, ..., attribute char_unicode { xsd:string }, ..., PDFStyle, Box, element visual_bbox { Box }? }`。

**PdfParagraph 与其富文本联合**：段落 `pdfParagraph` 必有 `unicode` 属性、一个 `Box`、一个 `PDFStyle`，以及 0 个或多个 `PDFParagraphComposition`；而 `PDFParagraphComposition` 是一个**五选一**的联合——这正是 u3-l1 讲的「段落富文本多选一」：

[il_version_1.rnc:101-124](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.rnc#L101-L124) —— `PDFParagraph = element pdfParagraph { attribute unicode {...}, ..., Box, PDFStyle, PDFParagraphComposition* }`；`PDFParagraphComposition = element pdfParagraphComposition { PDFLine | PDFFormula | PDFSameStyleCharacters | PDFCharacter | PDFSameStyleUnicodeCharacters }`。

**变换矩阵的 list 语法**：`PDFCurve`/`PDFForm` 里的 `ctm`（当前变换矩阵）是一个 6 浮点数的列表，写成空格分隔的属性值：

[il_version_1.rnc:175-184](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.rnc#L175-L184) —— `attribute ctm { list { xsd:float, xsd:float, xsd:float, xsd:float, xsd:float, xsd:float } }?`。

一个 PDF 变换矩阵 \(\begin{bmatrix}a&b&c&d&e&f\end{bmatrix}\) 在 XML/JSON 里就序列化成形如 `"1.0 0.0 0.0 1.0 50.0 100.0"` 的字符串（6 个数对应 `[a, b, c, d, e, f]`）。

**对照 .xsd**：`.xsd` 是同一份契约的「啰嗦版」。例如根节点同样声明 `<document>` 必含 1..∞ 个 `page`、且 `totalPages` 必填：

[il_version_1.xsd:3-10](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.xsd#L3-L10) —— `<xs:element name="document">` 内 `<xs:element maxOccurs="unbounded" ref="page"/>` 与 `<xs:attribute name="totalPages" use="required" type="xs:int"/>`。

`.xsd` 里 `use="required"` 对应 `.rnc` 里没有 `?`，`minOccurs="0" maxOccurs="unbounded"` 对应 `.rnc` 里的 `*`。

#### 4.2.4 代码实践

**实践目标**：不看 `il_version_1.py`，仅凭 `.rnc` 推断一个合法 IL 文档的最小骨架，并验证你的推断。

**操作步骤**：

1. 打开 [il_version_1.rnc:1-23](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.rnc#L1-L23)，列出 `Document` 与 `Page` 的**必填项**（没有 `?` 或 `*` 的）。
2. 据此画出最小合法 IL 的 XML 骨架（伪 XML 即可）。
3. 用 4.1.4 的示例代码实际跑一遍 `to_xml`，把真实输出与你的骨架对比。

**需要观察的现象**：你推断的必填项（`page`、`totalPages`、`mediabox`、`cropbox`、`pageNumber`、`Unit`、`baseOperations`）应当都出现在真实 XML 里；可选项（如 `pdfParagraph`、`pdfCharacter`）在你没赋值时不出现在 XML 里。

**预期结果**：你的骨架与 `to_xml` 真实输出在「必填项」上完全一致，说明 `.rnc` 就是 IL 结构的权威定义。

#### 4.2.5 小练习与答案

**练习 1**：`.rnc` 里 `PDFParagraphComposition` 是 `A | B | C | D | E` 的联合，这用面向对象的话怎么理解？

> **参考答案**：等价于一个「五种子类型的多态基类」——一个段落成分对象，运行时具体是 `PDFLine`、`PDFFormula`、`PDFSameStyleCharacters`、`PDFCharacter`、`PDFSameStyleUnicodeCharacters` 五者之一。schema 用 `|` 表达「这一格可以是这五种里的任意一种」。

**练习 2**：`.rnc` 里 `PDFCharacter*`（带星号）与 `PDFStyle`（不带星号）在 `pdfCharacter` 定义里分别意味着什么？

> **参考答案**：`PDFStyle` 不带 `*`/`?`，是**必填且唯一**的子元素（每个字符必须有且仅有一个样式）；`PDFCharacter*` 出现在 `Page` 定义里，表示一页可以有**任意多个**字符（包括 0 个）。星号管「数量」，有无星号管「是否必填」。

**练习 3**：为什么同一份契约要同时维护 `.rnc` 和 `.xsd` 两个文件？

> **参考答案**：分工不同。`.rnc` 简洁，是作者**设计和维护** IL 结构时主要编辑的版本（人友好）；`.xsd` 通用，能被标准 XML 工具链（验证器、第三方绑定生成器）消费（机器友好）。两者表达等价约束，通常 `.xsd` 由 `.rnc` 转换而来以保持同步。

---

### 4.3 debug JSON 输出：阶段快照

#### 4.3.1 概念说明

`XMLConverter` 真正在流水线里**被调用**的方法，几乎只有 `write_json`。它的用途是：**在 `--debug` 模式下，每个 midend 阶段处理完 IL 后，把当前这棵 IL 树落盘成一份 JSON 快照**。

于是你能在工作目录里看到一连串文件：`create_il.debug.json`、`detect_scanned_file.json`、`paragraph_finder.json`……每个文件都是「IL 在该阶段结束后长什么样」的切面。把它们按顺序 diff，就能看出每个阶段对 IL 做了什么改动——这是排查 midend 问题的核心手段。

#### 4.3.2 核心流程

在 `high_level._do_translate_single` 里：

1. 创建一次 `xml_converter = XMLConverter()`（全程复用）；
2. 每跑完一个 midend 阶段，就 `if translation_config.debug: xml_converter.write_json(docs, config.get_working_file_path("阶段名.json"))`；
3. `get_working_file_path` 把文件名拼到 `working_dir` 下；
4. 因为用了 `OPT_SORT_KEYS`，相邻两个快照的 diff 只显示**真正变化**的字段，键顺序不会干扰。

```
解析完成 → create_il.debug.json
扫描检测后 → detect_scanned_file.json
版面分析后 → layout_generator.json
表格解析后 → table_parser.json（仅 --translate-table-text）
段落后    → paragraph_finder.json
公式样式后 → styles_and_formulas.json
翻译后    → il_translated.json
调试标注后 → add_debug_information.json
排版后    → typsetting.json
```

#### 4.3.3 源码精读

**创建序列化器**：在解析 IL 之前就建好一个复用的 `XMLConverter`：

[high_level.py:900](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L900) —— `xml_converter = XMLConverter()`。

**第一个快照**：解析（frontend）一结束，立刻把「刚造好的 IL」写成 `create_il.debug.json`——这正是本讲代码实践任务要找的文件：

[high_level.py:916-920](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L916-L920) —— `if translation_config.debug: xml_converter.write_json(docs, translation_config.get_working_file_path("create_il.debug.json"))`。

**关键时序**：`create_il.debug.json` 的写入发生在 `only_parse_generate_pdf` 短路检查**之前**，所以即便用 `--only-parse-generate-pdf`（跳过翻译直接出 PDF），这份快照依然会生成——这意味着你**不调用翻译 API 也能拿到一份 IL 快照**用于学习。

[high_level.py:925-932](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L925-L932) —— `if translation_config.only_parse_generate_pdf:` 直接跳到 `PDFCreater.write`，但其上方的 `create_il.debug.json` 已先写好。

**逐阶段快照**：此后每个 midend 阶段都遵循同一模式。例如段落识别后：

[high_level.py:971-977](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L971-L977) —— `ParagraphFinder(...).process(docs)` 之后 `xml_converter.write_json(docs, ...("paragraph_finder.json"))`。

类似的还有：

- [high_level.py:946-950](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L946-L950) —— `detect_scanned_file.json`
- [high_level.py:957-961](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L957-L961) —— `layout_generator.json`
- [high_level.py:966-970](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L966-L970) —— `table_parser.json`（仅 `table_model` 开启时）
- [high_level.py:980-984](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L980-L984) —— `styles_and_formulas.json`
- [high_level.py:1009-1013](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L1009-L1013) —— `il_translated.json`
- [high_level.py:1040-1044](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L1040-L1044) —— `typsetting.json`

**文件落盘位置**：`get_working_file_path` 只是把文件名拼到 `working_dir`：

[translation_config.py:428-429](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L428-L429) —— `get_working_file_path` 返回 `Path(self.working_dir) / filename`。

**`--debug` 开关**：CLI 的 `--debug` 同时开启 DEBUG 日志级别与这些 JSON 快照。该参数从 `main.py` 一路透传进 `TranslationConfig.debug`：

[main.py:53-55](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L53-L55) —— `--debug` 选项，help="Use debug logging level."；在 [main.py:685](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L685) 以 `debug=args.debug` 传入配置。

#### 4.3.4 代码实践

**实践目标**：拿到一份真实的 `create_il.debug.json`，并对照 `.rnc` 找到其中的 Page 与 Paragraph。

**操作步骤**：

1. 用仓库自带的示例 PDF 跑一次带 `--debug` 的解析。为避免调用翻译 API，叠加 `--only-parse-generate-pdf`（按上面源码分析，`create_il.debug.json` 会在跳过翻译前写出）：

   ```bash
   babeldoc --debug --only-parse-generate-pdf \
     --files examples/ci/test.pdf \
     --output ./out
   ```

   > **待本地验证**：本机 CLI 是否能在不提供 `--openai`/key 的情况下进入解析流程（不同版本 CLI 校验不同）。若必须提供翻译参数，可只解析不翻译的方向以 `--only-parse-generate-pdf` 为主；若该路径仍要求构造 translator，则改用任意可用 key 跑一次 `--debug` 翻译，效果相同。

2. 在 `working_dir`（通常在输出目录下）里找到 `create_il.debug.json`。
3. 打开它，对照 [il_version_1.rnc:7-23](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.rnc#L7-L23) 找到 `"page"` 列表，再在其中找 `"pdfParagraph"`。

**需要观察的现象**：

- 顶层是 `{"page": [...], "totalPages": N}`；
- 每个 page 对象里有 `mediabox`/`cropbox`/`pageNumber`，以及（解析阶段通常还没生成段落的）`pdfCharacter` 列表；
- **注意**：`create_il.debug.json` 是「刚解析完」的快照，此时通常**还没有 `pdfParagraph`**——段落是后续 `ParagraphFinder` 阶段才聚合出来的。要看到段落，应改看 `paragraph_finder.json`。

**预期结果**：你能在 `create_il.debug.json` 里定位到 `page` 与 `pdfCharacter`；在 `paragraph_finder.json` 里定位到 `pdfParagraph` 及其 `pdfParagraphComposition`，并与 `.rnc` 的定义一一对应。

#### 4.3.5 小练习与答案

**练习 1**：为什么相邻两个阶段快照之间能直接用 `diff` 比较？

> **参考答案**：`to_json` 用了 `OPT_SORT_KEYS`，所有键按字母序稳定排列。所以两份快照之间键的顺序一致，`diff` 只会显示出「值真正变化」的行，不会被键顺序的扰动淹没。

**练习 2**：`create_il.debug.json` 里通常找不到 `pdfParagraph`，为什么？去哪个文件找？

> **参考答案**：`create_il.debug.json` 是 frontend 解析刚结束、midend 还没开始的快照。此时 IL 里只有字级的 `pdfCharacter`；段落是后续 `ParagraphFinder` 阶段把字符聚合成段落才产生的。要找 `pdfParagraph` 应看 `paragraph_finder.json`。

**练习 3**：流水线里 `xml_converter = XMLConverter()` 只创建一次、全程复用，有什么好处？

> **参考答案**：`__init__` 里要构建 xsdata 的 `XmlContext`/`XmlSerializer`/`XmlParser`，这些对象有初始化成本。复用一个实例，让八九次 `write_json` 调用共享同一套序列化基础设施，避免重复构造。不过严格说 JSON 路径（orjson）并不依赖这些 xsdata 对象，这里复用更多是为统一管理与为可能的 XML 输出预留。

---

### 4.4 examples/*.xml 示例结构：DPML 与 IL 的概念对照

#### 4.4.1 概念说明

> ⚠️ **重要区分（承接 u3-l1 的提示）**：仓库 `examples/` 下的 `basic.xml`、`complex.xml` 等**不是 IL 格式**，而是 **DPML**（`xmlns:wp="urn:ns:yadt:dpml"`）——一种**人写的、示意性的**文档标记格式，标签是 `<wp:document>`、`<wp:p>`、`<wp:run>` 这类。它们**不能**被 `XMLConverter.read_xml` 读回（`read_xml` 只认 `il_version_1.rnc` 定义的 `<document>`/`<pdfParagraph>` 等 IL 标签）。

那么为什么本讲要讲它们？因为 DPML 用更直观的标签表达了「一份文档由页、段落、文本块、公式、表格、图构成」这套**通用概念**，而这套概念与 IL 的实体**结构同构**。把 DPML 当作「IL 的简化示意」来读，能帮你快速建立直觉，再映射回真实的 IL 标签。

#### 4.4.2 核心流程

DPML → IL 的概念对照表：

| DPML 标签 | 概念 | 对应的 IL 实体（`.rnc`） |
| --- | --- | --- |
| `<wp:document>` | 文档根 | `Document`（`element document`） |
| `<wp:page>` | 一页 | `Page`（`element page`） |
| `<wp:p>` | 一个段落 | `PdfParagraph`（`element pdfParagraph`） |
| `<wp:run>` | 一段同样式文本 | `PdfSameStyleUnicodeCharacters`（最常用） |
| `<wp:break type="line">` | 换行 | `PdfLine`（行切分） |
| `<wp:math>` | 行内公式 | `PDFFormula`（`element pdfFormula`） |
| `<wp:figure>` | 图片 | `PdfFigure`（`element pdfFigure`） |
| `<wp:table>` | 表格 | 表格区域（`PageLayout.class_name` 标注） |

DPML 把「样式」直接写在标签属性上（`font-family`、`color`、`align`）；IL 则把样式抽成独立的 `PdfStyle`/`PdfFont` 对象，通过 `font_id`/`layout_id` 等**逻辑外键**关联——这是两者最大的表达差异（DPML 内联样式，IL 外联样式）。

#### 4.4.3 源码精读

**DPML 的基本结构**：根是 `<wp:document>`，含若干 `<wp:page>`，每页含若干 `<wp:p>` 段落，段落里是 `<wp:run>` 文本块，中间可以插 `<wp:break>` 换行：

[basic.xml:1-12](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/examples/basic.xml#L1-L12) —— 一个 `document` 含一个 `page`，`page` 里一个 `<wp:p>` 段落，段落里一段文本、一个 `<wp:break type="line"/>`、再一段文本。

映射到 IL：这一个 `<wp:p>` 对应一个 `pdfParagraph`；其中的两段文本对应两个 `pdfParagraphComposition`（`PDFSameStyleUnicodeCharacters` 类型），中间的 `<wp:break>` 对应一次 `PDFLine` 行切分。

**富内容段落**：`complex.xml` 演示了更丰富的结构——同一个段落里混合普通文本与行内公式：

[complex.xml:90-98](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/examples/complex.xml#L90-L98) —— 一个 `<wp:p>` 段落里，`<wp:run>` 文本块中嵌入 `<wp:math>\sum_{i=1}^{n} O(n \log n)</wp:math>`。

映射到 IL：这正是 `PDFParagraphComposition` 联合里「文本（`PDFSameStyleUnicodeCharacters`）与公式（`PDFFormula`）交替」的直观体现——一个段落由若干成分组成，每个成分可以是文本或公式。

**表格**：DPML 用 `<wp:table>/<wp:tr>/<wp:td>` 表达表格：

[complex.xml:122-167](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/examples/complex.xml#L122-L167) —— 一个带 `frame="all"` 的规格表，含 `<wp:thead>` 表头与若干 `<wp:tr>` 行、`<wp:td>` 单元格。

在 IL 里，表格不是用专门的 `<table>` 标签表达的——版面阶段（`LayoutParser`）会把这块区域识别成 `class_name` 为表格的 `PageLayout`，单元格结构再由 `TableParser` 处理。这是 DPML（显式表格标签）与 IL（基于版面区域识别）的又一表达差异。

> 再次强调：以上是**概念对照**，用于建立直觉。真实 IL XML 的标签是 `<pdfParagraph>`、`<pdfFormula>`、`<pageLayout>` 等（见 4.2），由 `XMLConverter.to_xml` 产出，**不是** DPML 的 `<wp:p>`、`<wp:math>`。

#### 4.4.4 代码实践

**实践目标**：用 DPML 示例训练「读文档结构 → 映射 IL 实体」的直觉，再回到真实 IL 验证。

**操作步骤**：

1. 打开 [complex.xml:75-109](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/examples/complex.xml#L75-L109)（第三页正文），数一数这页有几个 `<wp:p>` 段落、其中含几个 `<wp:math>`。
2. 按 4.4.2 的对照表，写出这页「如果有等价 IL，会有几个 `pdfParagraph`、每个段落里会交替出现哪些 `pdfParagraphComposition` 类型」。
3. 回到 4.3.4 拿到的 `paragraph_finder.json`（真实 IL），找一段含公式的段落，确认它的 `pdfParagraphComposition` 里确实出现了 `pdfFormula` 与文本类型的交替。

**需要观察的现象**：DPML 里 `<wp:p>` 的数量等于 IL 里 `pdfParagraph` 的数量概念；DPML 里 `<wp:math>` 的位置，在 IL 里对应一个 `pdfFormula` 类型的 composition 成分。

**预期结果**：你能熟练地把 DPML 的「段落—文本块—公式」结构翻译成 IL 的「pdfParagraph—pdfParagraphComposition—pdfFormula/PDFSameStyleUnicodeCharacters」结构。

**待本地验证**：步骤 3 依赖一份真实含公式的 PDF 快照；若手头 PDF 不含公式，可只做步骤 1–2 的概念映射练习。

#### 4.4.5 小练习与答案

**练习 1**：为什么不能用 `XMLConverter().read_xml("examples/basic.xml")` 读 `basic.xml`？

> **参考答案**：`read_xml` 用 xsdata 按 `il_version_1.Document` 反序列化，只认 schema 定义的根标签 `<document>` 及其子标签（`<page>`/`<pdfParagraph>` 等）。而 `basic.xml` 是 DPML，根标签是带命名空间的 `<wp:document>`、子标签是 `<wp:p>`/`<wp:run>`，与 IL schema 不匹配，xsdata 会解析失败或得到空对象。

**练习 2**：DPML 把字体颜色写在 `<wp:run color="...">` 上，IL 把它放在哪里？

> **参考答案**：IL 不把样式内联在文本上，而是抽成独立的 `PdfStyle`（含 `font_id`、`font_size`）与 `PdfFont` 对象，文本通过 `font_id` 这类逻辑外键引用样式对象。这样相同样式的文本共享同一个样式对象，节省空间也便于统一修改。DPML 内联、IL 外联，是两者核心表达差异。

**练习 3**：DPML 的 `<wp:table>` 在 IL 里通常以什么形式存在？

> **参考答案**：IL 没有与 `<wp:table>` 一一对应的标签。版面分析阶段会把表格所在区域识别为一个 `class_name` 标注为表格的 `PageLayout`（版面区域），单元格结构则由 `TableParser`（需开启表格模型）进一步处理。也就是说，IL 用「版面区域 + 类型标注」表达表格，而非显式表格标签。

---

## 5. 综合实践

**任务**：完整走一遍「运行 → 落盘 → 对照 schema → 概念映射」的闭环，把本讲四个模块串起来。

**步骤**：

1. **运行并落盘**：用 `--debug`（必要时叠加 `--only-parse-generate-pdf`）翻译/解析 `examples/ci/test.pdf`，在工作目录取得 `create_il.debug.json` 与 `paragraph_finder.json`。
2. **对照 schema**（模块 4.2 + 4.3）：打开 `paragraph_finder.json`，在其中找一个 `pdfParagraph`，列出它的 `unicode`、`box`、`pdfStyle`、`pdfParagraphComposition` 字段，并逐项在 [il_version_1.rnc:101-124](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.rnc#L101-L124) 里找到对应定义。
3. **理解序列化细节**（模块 4.1）：在该段落的 `pdfStyle`/`graphicState` 里找 `passthrough_per_char_instruction` 字段，说明它为何能被 orjson 正常序列化（提示：`_orjson_default` + `LazyPassthroughInstruction.materialize()`）。
4. **概念映射**（模块 4.4）：把这个真实段落「反向翻译」成 DPML 风格的伪 XML（用 `<wp:p>`/`<wp:run>`/`<wp:math>`），体会两种表达方式的差异。
5. **验证往返**（模块 4.1）：用 4.1.4 的示例代码，把一个手构的最小 `Document` 跑 `to_xml` → `from_xml` 往返，确认无损。

**预期结果**：你能自信地说出——「IL 在内存里是 dataclass 对象树；`XMLConverter` 用 orjson 把它写成带排序键的调试 JSON、用 xsdata 按schema把它写成/读回 XML；`examples/*.xml` 是 DPML 示意格式，与 IL 概念同构但标签不同，不能混用。」

**待本地验证**：步骤 1 依赖本机 CLI 能否在不调用翻译 API 的情况下产出快照；步骤 2–5 在拿到任意一份真实或手构的 IL 后即可完成。

---

## 6. 本讲小结

- `XMLConverter` 是 IL 的双向序列化器：**XML 走 xsdata**（`to_xml`/`from_xml`/`write_xml`/`read_xml`，带 schema 约束、可逆），**JSON 走 orjson**（`to_json`/`write_json`，只写不读、用于调试）。
- JSON 输出带 `OPT_SORT_KEYS`，键稳定排序，让阶段快照之间可直接 `diff`；遇到自定义的 `LazyPassthroughInstruction` 时用 `_orjson_default`→`materialize()` 兜底。
- `il_version_1.rnc`（人读）与 `il_version_1.xsd`（机器读）是同一份 IL 结构契约，`il_version_1.py` 是其生成的 dataclass；三者描述同一棵树，读 `.rnc` 最轻松。
- 流水线在 `--debug` 模式下，每个 midend 阶段后用同一个 `XMLConverter` 实例 `write_json` 落盘一份阶段快照（`create_il.debug.json`、`paragraph_finder.json` 等），是排查 midend 行为的核心抓手。
- `examples/*.xml` 是 **DPML** 示意格式（`<wp:p>`/`<wp:run>`），**不是 IL**，不能被 `read_xml` 读取；但它与 IL 实体概念同构，可作「简化示意」帮助建立直觉。
- `deepcopy` 用 `copy.deepcopy` 实现，「序列化往返实现深拷贝」的旧方案已注释弃用。

## 7. 下一步学习建议

- **横向（解析前端）**：本讲的 `create_il.debug.json` 是 frontend 解析的产物。下一篇 [u4-l1 解析入口与整体流程](u4-l1-parse-entry-and-flow.md) 讲 `new_parser` 如何把 PDF 字节流一步步变成这棵 IL 树。
- **纵向（IL 加工）**：拿到 `create_il.debug.json` 后，建议结合后续 [u5 中端处理流水线](u5-l1-detect-scanned-file.md) 各篇，逐个对照 `detect_scanned_file.json`、`layout_generator.json`、`paragraph_finder.json`、`styles_and_formulas.json`，亲眼看到每个 midend 阶段对 IL 做了什么改动——这是把本讲的「快照能力」转化为「调试能力」的最佳练习。
- **深入 schema**：若你对 schema 生成 dataclass 的机制感兴趣，可对比 `il_version_1.rnc` 与 `il_version_1.py` 的逐项映射，理解 xsdata 的 `metadata={"type": "Element"/"Attribute", "name": ...}` 是如何由 schema 编译出来的。
