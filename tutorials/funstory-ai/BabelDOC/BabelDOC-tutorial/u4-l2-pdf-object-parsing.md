# u4-l2 PDF 对象解析：object_parser 与对象模型

## 1. 本讲目标

在 u4-l1 里，我们把 frontend（解析前端）当成一个「黑盒」：丢进去一个 PDF，出来一棵 IL（中间表示）对象树。本讲要打开这个黑盒的**第一层**——PDF 文件本身是怎么被「读懂」的。

具体地说，学完本讲你应该能够：

1. 说清楚 PDF 规范定义了哪几种「对象」（字典、数组、名字、字符串、数字、布尔、null、间接引用、流），以及它们各自的字面语法。
2. 看懂 `object_parser.py` 这个**递归下降解析器**是如何把一段 PDF 字节流逐字节地翻译成 Python 值的，并能解释它的「分派 + 回溯」机制。
3. 掌握 `object_model.py` 里 `PdfObjectDict` / `PdfIndirectRef` / `PdfObjectStream` / `ActiveLiteral` 四个核心类型的字段与用途，并理解它们是「下游在普通 Python 值之上**加了一层包装**」得到的。
4. 理解 PDF 的**间接引用（xref）**机制：为什么需要 `5 0 R` 这种写法、解析器怎么识别它、运行时又怎么把一个引用号「物化」成真正的字典或流对象。

本讲是 u4-l3（内容流解释器）的前置：内容流操作符（`Tj`/`TJ`/`Tf` …）的**操作数**本身就是本讲讲的这些 PDF 对象，不理解对象模型，就看不懂解释器在消费什么。

---

## 2. 前置知识

### 2.1 你已经知道（来自 u4-l1）

- frontend 的产品入口是 `parse_prepared_pdf_with_new_parser_to_legacy_ir`，它最终用 `ActiveILCreater` 把解析到的事件投影成 IL 实体。
- 解析链路上有个 `page interpreter`（页面解释器），它消费「内容流」；而内容流和 PDF 对象字典里存的，正是本讲要拆的对象。

### 2.2 本讲需要的新概念

**词法 vs 语法（lexing vs parsing）。** 读一段文本通常分两步：先「切块」（词法分析 / lexing），把连续字节切成一个个最小单位（token，比如名字、数字、左括号）；再「搭骨架」（语法分析 / parsing），按规则把 token 组装成有结构的东西（字典、数组）。BabelDOC 里，切块由 `tokenizer.py`（`ContentStreamTokenizer`）负责，搭骨架由 `object_parser.py` 负责。本讲重点在「搭骨架」，但会顺手用到「切块」的产物。

**递归下降（recursive descent）。** 一种手写的解析手法：为每一种结构（字典、数组、引用）写一个函数，函数里遇到嵌套结构就**调用自己**。比如解析字典时，字典的值又可能是一个数组，于是解析字典的函数会调用解析数组的函数。PDF 对象天然嵌套（字典里套数组、数组里套字典），所以这种写法非常贴合。

**间接引用（indirect reference / xref）。** PDF 文件是一个「带索引的对象仓库」：大部分对象写在文件顶层，用一个编号（objid）加代次（generation）标识；别处要用到它时，不复制内容，只写一个引用 `5 0 R`（读作「5 号对象、0 代、引用」）。文件末尾的 **xref 交叉引用表**记录了每个对象在文件里的字节偏移，查看器据此随机定位。详见 `docs/intro-to-pdf-object.md` 第 1 节。

> 如果你想先建立对 PDF 文件全貌的直觉，强烈建议先读一遍 [docs/intro-to-pdf-object.md](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/docs/intro-to-pdf-object.md)，里面有一个从 `%PDF-2.0` 到 `%%EOF` 的完整最小 PDF 示例。

---

## 3. 本讲源码地图

本讲涉及的关键文件，按「从底层到上层」排列：

| 文件 | 作用 | 本讲角色 |
|------|------|---------|
| [babeldoc/format/pdf/new_parser/object_model.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/object_model.py) | 定义 PDF 对象的 Python 类型（`PdfObjectDict` 等） | **主角之一**：对象模型类型 |
| [babeldoc/format/pdf/new_parser/object_parser.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/object_parser.py) | 递归下降解析器，字节→Python 值 | **主角之二**：解析器 |
| [babeldoc/format/pdf/new_parser/tokenizer.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/tokenizer.py) | 词法分析器（切块），被解析器调用 | 辅助：提供 token |
| [babeldoc/format/pdf/new_parser/pymupdf_object_access.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/pymupdf_object_access.py) | 把 PyMuPDF 的对象文本喂给解析器、包装成对象模型类型、解析 xref | 关键：展示对象模型类型如何被「装配」出来 |
| [babeldoc/format/pdf/new_parser/resolved_object_access.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/resolved_object_access.py) | 间接引用的「按需解析」访问层 | 关键：展示引用如何被物化 |
| [babeldoc/format/pdf/new_parser/pdf_token_serializer.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/pdf_token_serializer.py) | 反方向：把对象模型类型序列化回 PDF 文本 | 辅助：佐证类型语义 |
| [docs/intro-to-pdf-object.md](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/docs/intro-to-pdf-object.md) | PDF 对象语法的科普文档 | 实践依据 |

> 一句话定位：`object_parser.py` 负责「读懂」，`object_model.py` 负责「装什么容器」，而 `pymupdf_object_access.py` 负责「把读懂的东西放进容器并和 xref 牵线」。

---

## 4. 核心概念与源码讲解

### 4.1 PDF 对象语法：PDF 文件由哪些积木拼成

#### 4.1.1 概念说明

PDF 规范定义了一套数量很少、但足够表达一切的「基本对象」（见 [docs/intro-to-pdf-object.md:113-137](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/docs/intro-to-pdf-object.md#L113-L137)）。可以把它类比成 JSON 的扩展版——JSON 有的它基本都有（数字、布尔、null、字符串、数组、字典），但它多了两个 JSON 没有的关键类型：

- **名字（Name）**：以 `/` 开头的标识符，如 `/Type`、`/Font`。它是字典的「键」，也常作为枚举值（如 `/Subtype /Type1`）。
- **间接引用（Indirect Reference）**：形如 `5 0 R`，指「5 号对象、0 代」。

再加上一个特殊的容器：

- **流（Stream）**：一个字典 + 一段二进制字节（字典里至少有 `/Length`）。图像、字体程序、压缩后的内容流都用流来承载。

下面这张表是本讲后续所有讨论的基础，务必先记住：

| PDF 对象 | 字面语法示例 | 说明 |
|---------|------------|------|
| 名字 | `/Type`、`/BaseFont` | 键 / 枚举值，`/` 后跟名字字符 |
| 整数 | `12`、`-3` | |
| 实数 | `3.14` | |
| 布尔 | `true`、`false` | |
| null | `null` | |
| 字面字符串 | `(Potato)` | 圆括号包裹，支持 `\n` 等转义 |
| 十六进制字符串 | `<48656C6C6F>` | 尖括号包裹的十六进制 |
| 数组 | `[1 (two) 3.14 false]` | 方括号，元素间无分隔符 |
| 字典 | `<< /A 1 /B [2 3] >>` | 双尖括号，键值成对 |
| 间接引用 | `5 0 R` | objid generation R |
| 流 | `<< /Length 44 >>` `stream` … `endstream` | 字典 + 二进制 |

#### 4.1.2 核心流程

一个 PDF 文件在「对象层」上长这样（伪代码）：

```
%PDF-2.0                       ← 文件头
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj   ← 顶层对象：objid generation obj ... endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /Contents 4 0 R /MediaBox [0 0 612 792] >> endobj
4 0 obj << /Length 44 >> stream
BT /F1 24 Tf 72 720 Td (Potato) Tj ET
endstream endobj
xref                           ← 交叉引用表（objid → 字节偏移）
...
trailer << /Root 1 0 R /Size 6 >>   ← trailer 指向根对象
startxref
478
%%EOF
```

读懂它的流程是「从尾到头」：

1. 从文件末尾找到 `startxref` 指向的 xref 表偏移；
2. 读 trailer 字典，拿到根对象引用 `/Root 1 0 R`；
3. 顺着引用一层层跳转（`1 0 R` → Catalog → `/Pages 2 0 R` → `/Kids [3 0 R]` → Page → `/Contents 4 0 R`）。

注意：对象体（`<< ... >>` 或 `<< ... >> stream … endstream`）的字面写法，**与本讲讲的「对象语法」完全一致**。也就是说，`object_parser.py` 解析的对象，正是 `obj ... endobj` 之间的那一段文本。这正是为什么需要一个「PDF 对象解析器」。

用一条产生式（BNF）概括本模块：

```
object    := dict | array | name | string | number
           | "true" | "false" | "null" | indirect_ref
indirect_ref := integer integer "R"
```

#### 4.1.3 源码精读

最小 PDF 字典的真实样例就在科普文档里，例如文档对象 3（Page）：

[docs/intro-to-pdf-object.md:33-43](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/docs/intro-to-pdf-object.md#L33-L43)

```
3 0 obj
<<
  /Contents 4 0 R
  /MediaBox [ 0 0 612 792 ]
  /Parent 2 0 R
  /Resources << /Font << /F1 5 0 R >> >>
  /Type /Page
>>
endobj
```

这一个对象里就用到了本表里的大部分类型：字典、名字（`/Contents`）、间接引用（`4 0 R`）、数组（`[0 0 612 792]`）、嵌套字典（`/Resources`）。也正因为同一段文本里多种类型混杂，才需要 4.2 节那种「按首字节分派」的解析器。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：用肉眼而不是代码，把一段 PDF 对象文本拆成它的组成对象。

**操作步骤**：

1. 打开 [docs/intro-to-pdf-object.md:113-137](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/docs/intro-to-pdf-object.md#L113-L137)，对照「PDF Objects」清单。
2. 取对象 5（字体字典）作为练习素材：

   ```
   5 0 obj
   <<
     /BaseFont /Helvetica
     /Encoding /WinAnsiEncoding
     /Subtype /Type1
     /Type /Font
   >>
   endobj
   ```

3. 逐项标注：哪些 token 是「名字」、哪些是「键」、整体是什么对象类型。

**需要观察的现象**：你会看到 `/Type`、`/Font` 这种 `/` 开头的名字既充当键（左侧），又充当值（右侧 `/Font`）；而整个 `<< ... >>` 是一个字典。

**预期结果**：这是一个「键和值都是名字」的扁平字典——没有数字、数组或引用，是最简单的字典对象。后续 4.2 节你会看到解析器对它的处理极其直接。

> 待本地验证：建议你把这段文本在编辑器里用不同颜色高亮「键 / 名字值 / 字典边界」，建立直觉后再读代码。

#### 4.1.5 小练习与答案

**练习 1**：`[1 (two) 3.14 false]` 这个数组里，每个元素分别属于哪种 PDF 对象类型？

**参考答案**：`1` 是整数、`(two)` 是字面字符串、`3.14` 是实数、`false` 是布尔。一个数组可以混合存放任意类型的对象。

**练习 2**：为什么 PDF 要专门设计「名字（Name）」这种类型，而不像 JSON 那样只用字符串当键？

**参考答案**：名字是「标识符」语义，常被用作枚举值（`/Type1`、`/Page`、`/FlateDecode`），程序需要高频地按名字比较和分发；用专用类型既能在词法层就与字符串区分开，也能让名字带一套转义规则（`#xx` 十六进制转义，见 4.2 节），比裸字符串更适合做「键」。

---

### 4.2 object_parser 解析器：从字节到 Python 值的递归下降

#### 4.2.1 概念说明

`object_parser.py` 的职责很纯粹：**给它一段表示单个 PDF 对象的字节流，返回一个等价的 Python 值**。它不做 xref 解析、不读文件、不碰流字节——那些是更上层（`pymupdf_object_access.py`）的事。

它是一个典型的**手写递归下降解析器**，整体只有一个公开函数 `parse_object_bytes(data)`，内部由一个 `_Parser` 类驱动。它依赖 `tokenizer.py` 的 `ContentStreamTokenizer` 做「切块」（读名字、读数字、读字符串），自己在切块结果之上做「搭骨架」。

为什么要手写、而不用 PyMuPDF 自带的对象读取？因为 BabelDOC 要对 PDF 做非常细粒度的控制（保留坐标、改写字体、子集化），需要一套**自己掌控的对象模型**，而不是把 PyMuPDF 的 dict 照单全收。这套自研解析器让 BabelDOC 能在「PyMuPDF 给的文本」和「想要的 Python 结构」之间做精确映射。

#### 4.2.2 核心流程

解析的核心是一个「按首字节分派」的循环。算法骨架（伪代码）：

```
function parse_value(depth):
    if depth > 128: 报错（防栈溢出）
    跳过空白与注释
    看下一个字节 b：
        b == '<' 且其后是 '<'  → parse_dict()        # << 开头
        b == '['                → parse_array()        # [ 开头
        b == '/'                → 读名字，做 #xx 转义解码   # 名字
        b == '('                → 读字面字符串，取其原始字节  # (...)
        b == '<'                → 读十六进制字符串，取其字节 # <...>
        其他                    → 读「数字或关键字」，再试着当作间接引用
```

关键设计有两处：

1. **字典 vs 十六进制字符串的消歧**：`<` 单个字符开头是十六进制字符串 `<48656C>`，而 `<<` 两个字符开头才是字典。所以解析器要先 `_peek(2)`（偷看两字节）再决定，见 [object_parser.py:45-54](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/object_parser.py#L45-L54)。

2. **整数 vs 间接引用的消歧（带回溯）**：看到一个整数 `5` 时，它既可能是孤立的整数，也可能是 `5 0 R` 这个间接引用的开头。解析器的做法是**投机地继续读**——再读一个整数、再读一个关键字，若恰好是 `整数 整数 R` 三连，就判定为引用；否则**回退读指针**，把第一个整数当孤立整数返回。详见 4.4 节。

#### 4.2.3 源码精读

公开入口 `parse_object_bytes` 把字节交给 `_Parser`，并在 `parse()` 里做一次「尾随数据」校验（要求整段输入恰好被一个对象消费完，否则报 `Trailing data after object`）：

[object_parser.py:120-132](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/object_parser.py#L120-L132) —— 构造解析器、调用 `parse()`，并把 tokenizer 抛出的 `TokenizerError` 统一包装成 `ObjectParserError`。

`_parse_value` 是分派中枢，逐字节决定走哪个分支：

[object_parser.py:38-57](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/object_parser.py#L38-L57) —— 注意最后两行：对非定界符开头的 token，调用 `_read_number_or_keyword` 读出数字或关键字，再交给 `_maybe_indirect_ref` 判断是不是引用。

字典解析 `_parse_dict`：吃掉 `<<`，然后循环「读一个名字当键 → 读一个值」，直到遇见 `>>`：

[object_parser.py:74-91](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/object_parser.py#L74-L91) —— 它要求键必须是 `PdfName`，键存的是 `key.value`（去掉前导 `/` 的名字字符串），值由 `_parse_value` 递归得到。注意嵌套调用：值的解析会再次进入 `_parse_value`，于是「字典套数组套字典」自然被支持。

数组解析 `_parse_array` 结构对称，吃掉 `[`，循环读元素直到 `]`：

[object_parser.py:59-72](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/object_parser.py#L59-L72)

防栈溢出的护栏 `MAX_OBJECT_NESTING_DEPTH = 128`：

[object_parser.py:13](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/object_parser.py#L13) —— 每次进入 `_parse_value` / `_parse_array` / `_parse_dict` 都调用 `_check_nesting_depth`，嵌套超过 128 层就报错。这是防御恶意构造的「深度嵌套炸弹」（类似 XML 的 billion laughs），避免递归爆栈。

`parse_object_bytes` 最终返回的就是**普通 Python 值**，对照表如下（这是本讲最重要的一张表）：

| PDF 对象 | 字面示例 | `parse_object_bytes` 返回的 Python 值 |
|---------|---------|--------------------------------------|
| 字典 | `<< /A 1 >>` | `dict`（键为 `str`） |
| 数组 | `[1 2]` | `list` |
| 名字 | `/Font` | `str`（已做 `#xx` 转义解码） |
| 字面字符串 | `(abc)` | `bytes` |
| 十六进制字符串 | `<616263>` | `bytes` |
| 整数 | `12` | `int` |
| 实数 | `3.14` | `float` |
| 布尔 | `true` | `bool` |
| null | `null` | `None` |
| 间接引用 | `5 0 R` | `PdfIndirectRef`（**唯一**返回的自定义类型） |

> 注意：**字典返回的是普通 `dict`，不是 `PdfObjectDict`**。`PdfObjectDict` 这一层的包装是更上层 `pymupdf_object_access.parse_object_text` 做的（见 4.3.3）。这是本讲最容易记错的一点：解析器只「读懂」，不「贴标签」。

#### 4.2.4 代码实践（可运行）

**实践目标**：亲手调用 `parse_object_bytes`，验证上表的映射关系。

**操作步骤**：

1. 在仓库根目录启动一个能 import 到 `babeldoc` 的 Python（`uv run python` 或在已安装环境里）。
2. 运行下面这段**示例代码**（非项目原有代码，仅为演示）：

   ```python
   from babeldoc.format.pdf.new_parser.object_parser import parse_object_bytes
   from babeldoc.format.pdf.new_parser.object_model import PdfIndirectRef

   samples = [
       b"<< /Type /Page /Contents 4 0 R /MediaBox [0 0 612 792] >>",
       b"/Helvetica",
       b"(Potato)",
       b"<48656C6C6F>",
       b"5 0 R",
       b"true",
       b"null",
       b"3.14",
   ]
   for s in samples:
       v = parse_object_bytes(s)
       print(f"{s.decode():40r} -> {v!r:40} ({type(v).__name__})")
   ```

**需要观察的现象**：

- 字典样例应得到一个 `dict`，键为 `"Type"`/`"Contents"`/`"MediaBox"`，其中 `"Contents"` 的值是 `PdfIndirectRef(objid=4, generation=0)`，`"MediaBox"` 的值是 `[0, 0, 612, 792]`。
- `5 0 R` 得到 `PdfIndirectRef(objid=5, generation=0)`。
- `(Potato)` 与 `<48656C6C6F>` 都得到 `bytes`（且内容应相等，都表示 `Potato`）。

**预期结果**：输出类型分别约为 `dict` / `str` / `bytes` / `bytes` / `PdfIndirectRef` / `bool` / `NoneType` / `float`。

> 待本地验证：上述输出依赖当前 HEAD 的解析行为，请实际运行确认；若环境无法联网安装，可只做源码阅读，对照 4.2.3 的表格推断结果。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_parse_value` 要先 `_peek(2)` 再区分「字典」和「十六进制字符串」？如果只看一字节会怎样？

**参考答案**：字典以 `<<` 开头、十六进制字符串以 `<` 开头，两者首字节都是 `<`，只看一字节无法区分。`_peek(2)` 偷看两字节：是 `<<` 走字典分支，否则走十六进制字符串分支。这正是 PDF 语法里一个经典的「需要向前看」的歧义点。

**练习 2**：`parse()` 方法在解析完一个对象后，为什么还要检查 `self.lexer.pos != len(self.lexer.data)`？

**参考答案**：`parse_object_bytes` 的契约是「整段字节恰好表示一个对象」。如果读完对象后还剩字节（尾随数据），说明输入不规范（比如粘了两个对象），此时报 `Trailing data after object` 比静默忽略更安全，能及早暴露调用方传入了错误片段。

---

### 4.3 对象模型类型：PdfObjectDict / PdfIndirectRef / PdfObjectStream

#### 4.3.1 概念说明

`object_model.py` 是整个 new_parser 里**最短**的一个文件，但它定义了贯穿后续所有解析逻辑的「数据骨架」。它只定义四个类型：

- `PdfObjectDict`：一个带 `objid` 属性的字典——「我来自几号对象」。
- `PdfIndirectRef`：一个 `(objid, generation)` 二元组——「我指向几号对象」。
- `PdfObjectStream`：字典属性 + 二进制字节——「我是带流数据的对象」。
- `ActiveLiteral`：一个名字的轻量包装（str 或 bytes）。

为什么要单独造这些类型，而不是直接用 `dict` / `tuple` / `bytes`？因为 **PDF 对象的「身份」和「引用关系」需要被显式携带**。普通 `dict` 无法回答「你是文件里的几号对象」这个问题；普通元组 `(5, 0)` 也无法和「恰好有两个元素的数组」区分开。给它们套上专用类型后，下游代码就能用 `isinstance(x, PdfIndirectRef)` 准确判断「这是一个引用，需要去 xref 表里解引用」，而不会和普通数据混淆。

#### 4.3.2 核心流程

这些类型的「生命周期」是这样流动的（重要！）：

```
PDF 字节
   │  parse_object_bytes (object_parser.py)        ← 只「读懂」，产出普通值
   ▼
普通 dict / list / str / int / ... / PdfIndirectRef
   │  object_dict(..., objid=) (resolved_object_access.py)   ← 给字典「贴 objid 标签」
   ▼
PdfObjectDict
   │  PdfObjectStream(attrs=PdfObjectDict, rawdata=bytes)   ← 若该对象有流，升级为流
   ▼
PdfObjectStream
```

也就是说：**解析器产普通值，下游按需把它们包装成对象模型类型**。这条流水线决定了 `PdfObjectDict` / `PdfObjectStream` 不是解析器直接吐出来的，而是在 `pymupdf_object_access.py` 解析 xref 时「装配」出来的（4.4 节会看到具体代码）。

#### 4.3.3 源码精读

`PdfObjectDict` 继承自内置 `dict`，额外加一个 `objid` 字段：

[object_model.py:6-9](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/object_model.py#L6-L9) —— 因为继承 `dict`，所有字典操作（`d["Type"]`、`d.get(...)`）都能用，同时能通过 `.objid` 追溯它来自几号对象。`objid` 默认 `None`，表示「不来自顶层对象」（比如内联嵌套的小字典）。

`PdfIndirectRef` 是一个 frozen dataclass，默认 `generation=0`：

[object_model.py:17-20](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/object_model.py#L17-L20) —— frozen 表示不可变、可哈希，能当字典键；`generation` 几乎总是 0（科普文档第 2 节说明代次在实战中基本可忽略）。`frozen=True` + `slots=True` 还带来更小的内存占用。

`PdfObjectStream` 把「流的字典属性」和「流的二进制数据」绑在一起：

[object_model.py:23-37](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/object_model.py#L23-L37) —— `attrs` 是那个 `<< /Length 44 /Filter /FlateDecode ... >>` 字典，`rawdata` 是 `stream`/`endstream` 之间的字节；`decoded=True` 表示字节已经被解码（PyMuPDF 在 `xref_stream` 时已解压）。它额外实现了 `get` / `__contains__` / `get_data`，让外部可以像访问字典一样取属性、像访问流一样取字节。

`ActiveLiteral` 是名字的轻量包装，`name` 可以是 `str` 或 `bytes`：

[object_model.py:12-14](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/object_model.py#L12-L14) —— 它出现在内联图像（inline image）等场景，由 `active_object_backend.create_active_literal` 构造（见 [active_object_backend.py:9-10](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/active_object_backend.py#L9-L10)）。

反方向的佐证——序列化器把 `PdfIndirectRef` 写回 `5 0 R` 文本，证明这个类型语义就是「引用」：

[pdf_token_serializer.py:65-66](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/pdf_token_serializer.py#L65-L66) —— `f"{value.objid} {value.generation} R"`，与解析器 `_maybe_indirect_ref` 的识别规则严格对称。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：跟踪「普通 dict 如何变成 PdfObjectDict」这一步，验证 4.3.2 的装配流水线。

**操作步骤**：

1. 打开 [pymupdf_object_access.py:17-21](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/pymupdf_object_access.py#L17-L21) 的 `parse_object_text`：它调用 `parse_object_bytes`，若结果是 `dict`，就用 `object_dict(parsed, objid=objid)` 包成 `PdfObjectDict`。
2. 再打开 [resolved_object_access.py:64-65](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/resolved_object_access.py#L64-L65) 的 `object_dict` 工厂：本质就是 `PdfObjectDict(value, objid=objid)`。

**需要观察的现象**：`parse_object_bytes` 与 `parse_object_text` 的差别仅在于「后者对 dict 多套了一层 `PdfObjectDict` 并传入 `objid`」。非字典结果（名字、数组、引用等）两者返回完全相同。

**预期结果**：你能清楚说出「`PdfObjectDict` 的 `objid` 来自调用方（即 xref 号），而非来自解析器」——这正是 4.3.2 流水线图里第二层包装的含义。

#### 4.3.5 小练习与答案

**练习 1**：`PdfObjectDict` 为什么选择「继承 `dict`」而不是「组合一个 `dict` 字段」？

**参考答案**：继承后它**就是一个 dict**，所有现成的字典接口（`[]`、`.get`、`.items`、`in`）直接可用，下游代码无需学习新 API；同时只需新增一个 `.objid` 属性来携带「身份」。组合方式则需要转发所有字典方法，既啰嗦又容易漏。

**练习 2**：`PdfIndirectRef` 设为 `frozen=True` 有什么好处？

**参考答案**：frozen dataclass 不可变、自动可哈希，因此可以放进 `set`、当字典键（例如做引用去重、缓存映射）；同时也避免解析过程中被意外修改，语义上更贴近「引用是一个稳定的指针」。

---

### 4.4 间接引用与对象流：xref 的解析与物化

#### 4.4.1 概念说明

间接引用是 PDF 节省空间、支持随机访问的核心机制：对象只定义一次（写在 `obj ... endobj` 顶层），到处用 `5 0 R` 引用。于是「解析」一个 PDF 对象时遇到 `5 0 R`，解析器**无法当场**给出它指向的内容——它只产出一个 `PdfIndirectRef(objid=5)`，把「真正的解引用」推迟到运行时。

「物化（materialize）一个引用」就是：拿着 objid，去 xref 表查字节偏移，把那个对象读出来、解析成 `PdfObjectDict`（若带流则升级成 `PdfObjectStream`），并缓存。这一步在 BabelDOC 里由 `PyMuPdfObjectStore.resolve_xref` 完成——它借助 PyMuPDF 的 `xref_object` / `xref_stream` 来定位和取字节，再用本讲的 `parse_object_text` 把文本解析成对象模型类型。

**对象流（Object Stream / ObjStm）** 是一个相关优化：把很多个小对象压缩打包进一个流的字节里，再在流字典里记录每个小对象的偏移。对解析器而言，对象流里的对象和顶层对象在「对象语法」层面完全一样，区别只在「它们是被压缩存放、需要先解压再切分」——这个拆包由 PyMuPDF 在 `xref_object(compressed=False)` 时已经处理好，所以本讲的解析器拿到的仍是干净的对象文本。

#### 4.4.2 核心流程

引用识别（解析器侧）的判定规则可写成：

\[ \text{isRef}(t_1, t_2, t_3) \;=\; (t_1 \in \mathbb{Z}) \;\wedge\; (t_2 \in \mathbb{Z}) \;\wedge\; (t_3 = \texttt{R}) \]

即连续三个 token 是「整数 整数 关键字 R」时，才判定为间接引用 `PdfIndirectRef(objid=t_1, generation=t_2)`；否则只取第一个整数。由于「读三个 token」是投机行为，不匹配时必须**回退读指针**到读第一个整数之后的位置。

引用物化（运行时侧）的流程：

```
resolve_xref(objid):
    若缓存命中 → 直接返回
    否则：
        text = PyMuPDF.xref_object(objid)          # 取对象文本（已处理对象流解压）
        obj  = parse_object_text(text, objid=objid) # 解析 → PdfObjectDict（贴 objid）
        若 obj 是 PdfObjectDict 且带流字节：
            data = PyMuPDF.xref_stream(objid)        # 取已解码的流字节
            obj  = PdfObjectStream(attrs=obj, rawdata=data, objid=objid, decoded=True)
        缓存并返回 obj
```

注意一个特例：**图像 XObject**（`/Subtype /Image`）即使带流，也不升级成 `PdfObjectStream`，而是以 `PdfObjectDict` 形式缓存——因为图像字节由专门的图像处理路径读取，不在这里展开。

#### 4.4.3 源码精读

解析器侧的引用识别 `_maybe_indirect_ref`，完整体现「投机读取 + 回退」：

[object_parser.py:93-110](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/object_parser.py#L93-L110) —— 先保存 `saved_pos`，读第二个 token（必须是 int），再读第三个 token（必须是关键字 `R`）；三连命中就返回 `PdfIndirectRef(objid=first, generation=second)`，否则把 `self.lexer.pos` 复位回 `saved_pos`，只返回第一个整数 `first`。注意：进入这个函数前 `first` 已经是 int（非 int 的标量会先被 `_normalize_scalar` 处理，直接返回）。

`_normalize_scalar` 处理「非整数标量」的归一化：

[object_parser.py:112-117](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/object_parser.py#L112-L117) —— 把 `PdfName` 转成 `str`、`PdfString` 转成 `bytes`，其余原样返回。这是解析器把 tokenizer 的 token 类型「拍平」成普通 Python 值的收尾步骤。

运行时侧的引用物化 `resolve_xref`，展示 `PdfObjectDict` → `PdfObjectStream` 的升级：

[pymupdf_object_access.py:29-51](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/pymupdf_object_access.py#L29-L51) —— 三个分支：①图像 XObject 直接缓存 dict；②普通对象若有流字节，构造 `PdfObjectStream(attrs=parsed, rawdata=stream_data, objid=xref, decoded=True)`；③无流则缓存 dict。所有结果都写入 `self.cache[xref]`，保证同一个 objid 只物化一次。

「按需解引用」的访问层 `ResolvedObjectAccess.resolve`：

[resolved_object_access.py:20-31](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/resolved_object_access.py#L20-L31) —— 用 `obj_ref_id(value)` 判断传入的值是不是引用，是就去 `object_store` 取或调 `resolver` 物化，否则原样返回。这就是「惰性解引用」：引用在被真正访问时才触发解析与缓存。

引用的识别函数 `obj_ref_id`（运行时层）：

[runtime/object_primitives_runtime.py:89-94](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/runtime/object_primitives_runtime.py#L89-L94) —— `isinstance(value, PdfIndirectRef)` 时返回 `value.objid`，否则（对兼容旧 pdfminer 风格、带 `objid`+`resolve` 的对象）也兼容返回，否则返回 `None`。这一处 `isinstance` 正是把 `PdfIndirectRef` 单列为一个类型的回报：运行时能毫不含糊地认出「这是个引用」。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：把「解析器识别引用」与「运行时物化引用」两段代码串起来，理解引用的两段式生命周期。

**操作步骤**：

1. 用 4.2.4 的示例代码确认：`parse_object_bytes(b"<< /Contents 4 0 R >>")` 返回的字典里，`"Contents"` 的值是 `PdfIndirectRef(objid=4, generation=0)`。
2. 阅读 [pymupdf_object_access.py:97-105](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/pymupdf_object_access.py#L97-L105) 的 `parse_page_contents`：它从 PyMuPDF 拿到页面 `/Contents` 键的类型与值——若 `kind == "xref"`，直接构造 `PdfIndirectRef(int(value.split()[0]), 0)`（说明 PyMuPDF 已经告诉我们「这是个引用」，无需再走文本解析）；若 `kind == "array"`，才调用 `parse_object_text` 解析文本。
3. 想象后续有人对这个 `PdfIndirectRef` 调用 `ResolvedObjectAccess.resolve`：它会触发 `resolve_xref(4)`，把 4 号对象物化成 `PdfObjectStream`（因为 Contents 通常是流）。

**需要观察的现象**：同一个「引用」概念，在两条路径上产生——一是 `object_parser` 从文本 `4 0 R` 识别出来（4.4.3 的 `_maybe_indirect_ref`），二是 `pymupdf_object_access` 直接从 PyMuPDF 的类型信息 `xref` 构造出来（`parse_page_contents`）。两者殊途同归，都得到 `PdfIndirectRef`。

**预期结果**：你能画出这样一条链：PDF 文本/PyMuPDF 类型 → `PdfIndirectRef(objid=4)` →（被访问时）`resolve_xref(4)` → `PdfObjectStream(attrs, rawdata, objid=4)`。

> 待本地验证：若想看真实物化结果，可写脚本打开 `examples/ci/test.pdf`，用 `fitz.open` 拿到某页 Contents 的 xref，再调用 `build_object_store(doc).resolve_xref(xref)` 观察返回的 `PdfObjectStream`。

#### 4.4.5 小练习与答案

**练习 1**：解析器看到 `5 0 R` 时，为什么不能在读到 `5` 的瞬间就判定它是引用？

**参考答案**：因为单独的 `5` 也可能是合法的整数对象（比如 `/Count 5`）。必须继续向前看两个 token，确认是「整数 整数 R」三连，才能判定为引用；这也是为什么 `_maybe_indirect_ref` 需要「保存位置 → 投机读取 → 不匹配则回退」的机制。

**练习 2**：`resolve_xref` 对「图像 XObject」做了特殊处理（不升级成 `PdfObjectStream`）。请猜想原因。

**参考答案**：图像的像素字节体量大、且有独立的解码/采样路径（PDF 图像由专用图像处理逻辑读取），在对象访问层把它包成 `PdfObjectStream` 既浪费内存、又会和图像专用流程冲突；所以这里只保留其字典属性（如 `/Width`/`/Height`/`/ColorSpace`），把字节读取留给图像路径。这一特例体现了「对象模型服务于上层需求」的设计取向。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**手工解析 + 类型判定**任务（这正是讲义规格里要求的实践，无需运行环境也能完成）。

**任务**：结合 [docs/intro-to-pdf-object.md](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/docs/intro-to-pdf-object.md)，手写下面这个「最小 PDF 字典对象」并逐项分析：

```
<<
  /Type /Font
  /Subtype /Type1
  /BaseFont /Helvetica
  /Encoding /WinAnsiEncoding
  /ToUnicode 7 0 R
>>
```

**要求完成三件事**：

1. **对象类型判定**：整段文本会被 `parse_object_bytes` 解析成哪种 Python 类型？（答案：普通 `dict`。）若它作为顶层对象被 `parse_object_text(..., objid=5)` 处理，又会被包装成哪种对象模型类型？（答案：`PdfObjectDict(objid=5)`。）
2. **逐字段标注**：填出下表（对照 4.2.3 的映射表）。

   | 键 | 值的字面写法 | 值的 Python 类型 |
   |----|------------|----------------|
   | `/Type` | `/Font` | `str` |
   | `/Subtype` | `/Type1` | `str` |
   | `/BaseFont` | `/Helvetica` | `str` |
   | `/Encoding` | `/WinAnsiEncoding` | `str` |
   | `/ToUnicode` | `7 0 R` | `PdfIndirectRef(objid=7, generation=0)` |

3. **引用物化推演**：假设这个字典是 5 号对象，`/ToUnicode` 指向的 7 号对象是一个流。请描述：当后续代码调用 `ResolvedObjectAccess.resolve` 访问 `/ToUnicode` 时会发生什么——经过 `obj_ref_id` 识别为引用、`resolve_xref(7)` 物化、因带流字节而升级为 `PdfObjectStream(attrs=<7号字典>, rawdata=<已解码字节>, objid=7, decoded=True)` 并缓存。

**进阶（可运行）**：在能 import `babeldoc` 的环境里，用 4.2.4 的示例代码实际解析上面这段文本，验证你的第 1、2 步判断是否与程序输出一致。

> 待本地验证：第 3 步的物化结果依赖真实 PDF 的 7 号对象内容，建议用 `examples/ci/test.pdf` 找一个真实字体对象的 `/ToUnicode` 引用做端到端验证。

---

## 6. 本讲小结

- PDF 文件由 9 类基本对象（名字、数字、布尔、null、字符串×2、数组、字典、间接引用）外加「流」容器拼成；`docs/intro-to-pdf-object.md` 给了完整的最小示例。
- `object_parser.py` 是一个手写的**递归下降解析器**，`_parse_value` 按首字节分派，能处理嵌套的字典/数组；`parse_object_bytes` 是唯一公开入口，返回**普通 Python 值**。
- 它有两处关键的歧义消歧：`<` vs `<<`（用 `_peek(2)` 区分十六进制串与字典）、整数 vs `整数 整数 R`（用「投机读取 + 回退」的 `_maybe_indirect_ref` 处理）。
- `MAX_OBJECT_NESTING_DEPTH = 128` 是防深度嵌套炸弹的栈溢出护栏；`parse()` 还会校验「无尾随数据」。
- `object_model.py` 定义四个类型：`PdfObjectDict`（带 `objid` 的字典）、`PdfIndirectRef`（`objid`+`generation` 的引用）、`PdfObjectStream`（字典属性 + 流字节）、`ActiveLiteral`（名字包装）；它们是**下游在普通值之上加包装**得到的，不是解析器直接产出。
- 间接引用的生命周期是两段的：解析器只识别成 `PdfIndirectRef`，真正的物化（查 xref、解析文本、按需升级为 `PdfObjectStream`、缓存）由 `PyMuPdfObjectStore.resolve_xref` 在「被访问时」才完成——这是惰性解引用。

---

## 7. 下一步学习建议

本讲把「PDF 对象如何被读懂」讲透了，但还有两个方向值得继续：

1. **向下——内容流解释器（u4-l3）**：本讲的对象大多出现在「对象字典」里，而页面真正的绘制指令（`BT … ET` 文本块、`Tf`/`Tj`/`TJ` 操作符）住在 `/Contents` 流里。下一讲会讲 `interpreter.py` 如何把这些操作符解释成语义事件（`TextRunEvent`、`SetFontEvent`），而那些操作符的**操作数**正是本讲讲的对象（名字、数字、数组、字符串）。建议带着「操作数就是 4.2.3 表里的那些 Python 值」的视角去读 u4-l3。

2. **横向——词法层与序列化层**：如果你对「切块」细节（字面字符串的转义、名字的 `#xx` 解码、复合关键字的拆分）感兴趣，可以读 [tokenizer.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/tokenizer.py)；想看「对象模型类型如何被写回 PDF 文本」，可以读 [pdf_token_serializer.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/pdf_token_serializer.py)，它与本讲的解析器构成一组对称的「读/写」对。

3. **纵深——xref 物化全貌**：想完整理解「引用如何变成对象」，可顺着 `build_object_store` → `resolve_xref` → `ResolvedObjectAccess` → `PreparedObjectAccess` 这条链读到 [pymupdf_object_access.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/pymupdf_object_access.py) 与 [resolved_object_access.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/resolved_object_access.py)，它是 u4-l4「active 运行时」的前奏。
