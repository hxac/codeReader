# 章节数据模型：Chapter 与布局体系

## 1. 本讲目标

上一单元我们得到了 `toc.xml`——一份「目录坐标 + 层级」的缓存。本讲进入章节生成的第一个关键问题：**目录条目之间的正文，在内存里长什么样、在磁盘上长什么样**。

学完本讲，你应该能够：

1. 说出 `Chapter` 内部两种布局（`ParagraphLayout` 与 `AssetLayout`）的职责分工，以及 `BlockLayout` 各字段（`page_index`、`order`、`det`、`content`）的含义。
2. 识别 `BlockMember` 联合类型的两类成员——`InlineExpression`（LaTeX 公式）与 `Reference`（脚注引用），以及 `HTMLTag` 泛型容器如何嵌入 `content` 列表。
3. 独立追踪 `chapter_N.xml` 的 decode（XML → 对象）与 encode（对象 → XML）完整流程，并理解「先解引用表、再解正文」的两遍解码设计。

## 2. 前置知识

- **dataclass（数据类）**：Python 的 `@dataclass` 装饰器自动生成 `__init__`、`__repr__` 等方法，适合定义「只有数据、没有行为」的载体。本讲的所有数据类都是它。
- **联合类型与类型别名**：`ParagraphLayout | AssetLayout` 表示「二者之一」；`TypeAlias` 给复杂类型起短名，例如 `BlockMember = InlineExpression | Reference`。这是本讲数据模型的骨架语言。
- **XML ElementTree**：Python 标准库 `xml.etree.ElementTree` 把 XML 解析成一棵 `Element` 树，每个节点有 `tag`（标签名）、`attrib`（属性字典）、`text`（开闭标签之间的文本）与 `tail`（闭标签之后、下一个兄弟标签之前的文本）。本讲的编解码就是在 `Element` 树与 dataclass 对象树之间搬运。
- **泛型**：`HTMLTag[P]` 中的 `P` 是类型参数，表示「这个 HTML 标签容器里装的是什么类型的孩子」。你可以把它类比成 `list[T]` 的 `T`。
- **哨兵值（sentinel）**：用特殊取值表达「未设置」。本讲两次遇到 `-1`：`ParagraphLayout.level == -1` 表示该段落不是标题、没有层级；`Chapter.level == -1` 同理。

承接前几讲的结论：OCR 结果以 `ocr/page_N.xml` 缓存（u3-l3、u3-l4），目录分析产物以 `toc.xml` 缓存且存在即短路（u4-l3）；而 `chapters/` 目录**每次重算**（u4-l3 末尾的结论）。本讲要回答：重算出来的这份东西，数据模型是什么。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pdf_craft/extractor/chapter/chapter.py` | 本讲主战场：`Chapter` 及全部布局/成员数据类的定义，以及 `decode` / `encode` 两个方向的 XML 编解码 |
| `pdf_craft/extractor/chapter/content.py` | 定义 `Content` 类型别名，并提供围绕内容列表的四个操作函数（取首/取末/合并相邻文本/展开文本） |
| `pdf_craft/markdown/paragraph/types.py` | `HTMLTag` 泛型容器与骨架级 `decode` / `encode` / `flatten`（被 chapter.py 复用） |
| `pdf_craft/expression.py` | `ExpressionKind` 枚举与公式定界符的编解码（`InlineExpression` 的支撑） |
| `pdf_craft/extractor/chapter/mark.py` | `Mark` 脚注标记对象与 `transform2mark`（`Reference.mark` 的支撑，u5-l4 详讲） |
| `pdf_craft/common/asset.py` | `AssetRef` 字面量类型与 `ASSET_TAGS`（`AssetLayout.ref` 的取值范围） |
| `pdf_craft/common/xml.py` | `indent` 缩进、`read_xml` / `save_xml`（原子写盘） |
| `pdf_craft/extractor/chapter/generation.py` | 调用 `encode` 写出 `chapter_N.xml` 的上游；本讲只看它如何触发编码 |
| `pdf_craft/extractor/chapter/reader.py` | 调用 `decode` 读回章节的下游（`create_chapters_reader`） |

## 4. 核心概念与源码讲解

### 4.1 章节布局类型

#### 4.1.1 概念说明

一章的内容在内存中是一棵三层结构的对象树：

```
Chapter（一章）
├── ParagraphLayout（文字段落：标题、正文、脚注文字……）
│   └── BlockLayout（来自某一页的一个 OCR 文本块）
│       └── content（内联成员列表，见 4.2）
└── AssetLayout（资源块：图片、表格、公式截图）
    └── title / content / caption（三个可选内容区）
```

为什么要分「段落」和「块」两层？因为 Jointer（u5-l2 的主角）会把**跨页**的文本块合并成一个连续段落：一个 `ParagraphLayout` 可以装来自第 5 页、第 6 页的多个 `BlockLayout`。而每个 `BlockLayout` 仍保留它出生时的坐标——`page_index`（第几页）、`order`（页内序号）、`det`（检测框四元组）。这些坐标不是冗余信息：PDF 回写管线（u10）要用它们把译文定位回原页面。

`AssetLayout` 与之平行：OCR 识别出的图片、表格、公式不会留在文字流里，而是按 `det` 从页面截图裁剪、以内容哈希命名存进 `assets/`（u3-l4 讲过 `AssetHub.clip`），章节数据里只留引用（`ref` + `hash` + 边界框）。

#### 4.1.2 核心流程

章节对象的产生路径（上游，u5-l3 详讲）：

1. 逐页读 `ocr/page_N.xml`，取出每页的 body 布局。
2. Jointer 把跨页可合并的文本块串成 `ParagraphLayout`，`AssetLayout` 独立成段。
3. 遇到命中目录条目的标题块时，开一个新 `Chapter`；目录开始前的内容归入 `id=None` 的章（落盘为 `chapter_head.xml`）。
4. 每章经标点归一化、章内层级分析后，`encode` 写成 `chapter_N.xml`。

一个 `Chapter` 的取值约定：

- `id: int | None`——`None` 表示「目录开始前的章」，文件名后缀用 `head`；
- `level: int`——本章标题在目录中的层级；`-1` 表示未设置（`chapter_head` 用全书最大层级填充，防止前置内容盖过正文章节标题）；
- `layouts`——按阅读顺序排列的段落与资源块。

#### 4.1.3 源码精读

五个数据类的定义全部集中在 chapter.py 开头。先是章与段落：

[chapter.py:13-24](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L13-L24) 定义 `Chapter`（id、level、layouts）与 `ParagraphLayout`（ref、level、blocks）。`ParagraphLayout.ref` 是布局类型字符串（OCR 阶段归一化出的 `text`、`title`、`sub_title` 等，见 u3-l4）；`level` 是**章内**标题层级，`-1` 表示「不是标题」。

然后是资源与文本块两个布局：

[chapter.py:49-65](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L49-L65) 定义 `AssetLayout` 与 `BlockLayout`。`AssetLayout.ref` 的类型是 `AssetRef`，取值范围固定为三种：

[asset.py:8-9](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/asset.py#L8-L9) 用 `Literal` 限定 `AssetRef = "image" | "table" | "equation"`，decode 时会校验取值。`AssetLayout` 的 `title` / `content` / `caption` 是三个内容区，分别放资源的标题、主体（表格的 HTML）、图注；`hash` 即 `assets/` 目录下 PNG 文件的名字（去掉扩展名）。

`BlockLayout.det` 是 `tuple[int, int, int, int]`（左、上、右、下边界），XML 中序列化为逗号分隔的四个整数，解析逻辑在：

[chapter.py:290-299](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L290-L299) `_parse_det` 校验「逗号分隔、恰好 4 个整数」，否则抛 `ValueError`。

上游是谁、何时写出这些文件？看 generation.py 的触发点：

[generation.py:23-42](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L23-L42) `generate_chapter_files` 先删除所有旧 `chapter_*.xml`（第 25-26 行，印证 u4-l3 的「章节每次重算」），再逐章做标点归一化与章内层级分析，最后 `encode(chapter)` 得到 Element、`save_xml` 落盘。注意文件名规则：`id=None` 写 `chapter_head.xml`，否则写 `chapter_{id}.xml`。

读回侧：

[reader.py:8-26](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reader.py#L8-L26) `create_chapters_reader` 返回一个生成器：若存在 `chapter_head.xml` 则先 yield 它，再按数字序 yield 其余章节。其中复用了 `XMLReader`（按 `chapter_N.xml` 文件名中的数字排序读取，见 [reader.py:11-41](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/reader.py#L11-L41)）。

#### 4.1.4 代码实践

**实践目标**：对一个真实的 `chapter_N.xml`，统计一章内部布局的构成，验证「段落/资源两种布局」的分类。

**操作步骤**（示例代码）：

```python
# inspect_layouts.py（示例代码）
from collections import Counter
from pathlib import Path

from pdf_craft.extractor.chapter import (
    AssetLayout, Chapter, ParagraphLayout, decode,
)
from pdf_craft.common import read_xml

# 前提：你已经按 u1-l2 的方式跑过一次转换并保留了中间包：
#   craft = PDFCraft(PDFOptions(ocr=DeepSeekOCRVendorConfig(
#       base_url=..., api_key=..., model=...)))
#   craft.convert_pdf_to_markdown("tests/assets/mix.pdf", "out/md",
#                              package_path="out/package")
# 没有现成产物的话请先补跑上面两行（OCR 需要真实凭据，待本地验证）。
path = Path("out/package/chapters/chapter_1.xml")
chapter: Chapter = decode(read_xml(path))

counter = Counter()
for layout in chapter.layouts:
    if isinstance(layout, ParagraphLayout):
        counter[f"paragraph(ref={layout.ref}, level={layout.level})"] += 1
    elif isinstance(layout, AssetLayout):
        counter[f"asset(ref={layout.ref})"] += 1

print("chapter id =", chapter.id, " level =", chapter.level)
for key, count in sorted(counter.items()):
    print(f"{count:3d} × {key}")
```

**需要观察的现象**：输出中 `paragraph(ref=text, level=-1)` 之类的条目最多；带图 PDF（如 `tests/assets/figure-caption.pdf`）会出现 `asset(ref=image)`；表格多的 PDF 会出现 `asset(ref=table)`。

**预期结果**：两种布局的类型分支正好覆盖全部 `layouts` 元素，没有第三种类型。`level != -1` 的段落即本章内部的小标题。

（本脚本未在本文撰写环境中运行，输出待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BlockLayout` 要保留 `page_index` 和 `order`，而 `ParagraphLayout` 不用保留？

**答案**：`ParagraphLayout` 是合并产物，可能横跨多页，没有单一的页码；页码和页内序号属于「块出生地」的信息，所以下沉到每个 `BlockLayout`。这样既是溯源坐标（能找回 `ocr/page_N.xml` 里的原始记录），也是 u10 翻译回写时定位替换区域的几何来源。

**练习 2**：`AssetLayout.hash` 指向什么？为什么同名资源在整本书里只占一份磁盘文件？

**答案**：指向 `assets/` 目录下以内容 SHA-256 哈希命名的 PNG（u3-l4 的 `AssetHub.clip`）。因为文件名就是内容哈希，重复出现的图片（例如页眉 logo）裁剪后哈希相同，第二次裁剪时发现目标文件已存在就直接返回哈希，实现全书去重。

**练习 3**：`chapter_head.xml` 的 `Chapter.id` 是什么？为什么不能是 0？

**答案**：是 `None`。目录条目的 `id` 是文档顺序编号、从 1 开始（u4-l3）；目录开始之前的内容不属于任何目录条目，`None` 是「无主内容」的语义，也避免与合法的整数 id 冲突。

### 4.2 内联成员

#### 4.2.1 概念说明

`BlockLayout.content` 与 `AssetLayout` 的三个内容区共享同一个类型：

```python
Content = list[str | BlockMember | HTMLTag[BlockMember]]
```

（[content.py:6](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/content.py#L6)，其中 `BlockMember = InlineExpression | Reference` 定义在 [chapter.py:45-46](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L45-L46)。）

也就是说，一段内容的元素序列里混居着三种居民：

| 居民 | 类型 | 含义 |
| --- | --- | --- |
| 普通文本 | `str` | OCR 认出的文字片段 |
| 行内公式 | `InlineExpression` | LaTeX 片段，`kind` 标记定界符风格，`content` 是公式本体 |
| 脚注引用 | `Reference` | 正文里的编号标记（如 ①、1），挂着对应脚注区的全部布局 |
| HTML 标签 | `HTMLTag[BlockMember]` | OCR 原文里混入的受信任 HTML（表格、上下标等），孩子递归地又是一个内容列表 |

`InlineExpression` 为什么必须独立建模？因为公式定界符（`$...$`、`\(...\)` 等）在渲染 Markdown 时要按原样重建，翻译时又要整体跳过不可拆——它必须是原子。

`Reference` 为什么是「指针」而不是把脚注文字复制进正文？因为同一条脚注可能被多处引用；对象共享（decode 时多处 `<ref id="5-7"/>` 解析出**同一个** `Reference` 实例）保证改一处即处处同步。

`HTMLTag` 是 u6-l2 的主角，本讲只需记住它是白名单内的 HTML 容器：`definition`（标签定义，含允许的属性集）、`attributes`、`children`。

#### 4.2.2 核心流程

- **公式的诞生**：Jointer 阶段用 `parse_latex_expressions`（u5-l2 详讲）把文本切成 `ParsedItem(kind, content)`，公式项转成 `InlineExpression` 存入 content。
- **引用的诞生**：章节生成时，正文块里扫描到脚注标记字符（`Mark`），就从该页的 `References` 里查出对应脚注，把标记替换成 `Reference` 对象（[generation.py:173-187](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L173-L187) 的 `expand_text_in_content` 调用点，细节留给 u5-l4）。
- **内容列表的遍历**：`flatten` 递归掀开 `HTMLTag`，把嵌套结构摊平成 `str | BlockMember` 流——搜索引用、统计公式都靠它。

#### 4.2.3 源码精读

两类内联成员与引用标识：

[chapter.py:27-42](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L27-L42) 定义 `InlineExpression(kind, content)` 与 `Reference(page_index, order, mark, layouts)`。注意 `Reference.id` 是个 property，返回 `(page_index, order)` 元组——这正是 XML 里 `id="5-7"` 的语义；`mark` 的类型是 `str | Mark`：能被 `transform2mark` 识别的编号字符升级为 `Mark` 对象，否则保留原始字符串（见 [chapter.py:466-473](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L466-L473) 的兜底逻辑）。

公式的五种定界符风格：

[expression.py:6-10](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/expression.py#L6-L10) `ExpressionKind` 枚举：`TEXT`、`INLINE_DOLLAR`（`$...$`）、`DISPLAY_DOUBLE_DOLLAR`（`$$...$$`）、`INLINE_PAREN`（`\(...\)`）、`DISPLAY_BRACKET`（`\[...\]`）。XML 属性里存的是短码，映射表见 [expression.py:23-48](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/expression.py#L23-L48) 的 `encode_expression_kind` / `decode_expression_kind`：`"$"`、`"$$"`、`"\\("`、`"\\["`、`"text"`。

`Mark` 是脚注编号的结构化表示：

[mark.py:35-55](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/mark.py#L35-L55) `Mark` 携带 `number`（数值语义，如 ⑫ → 12）、`char`（原字符）、`clazz`（编号类别：罗马数字、带圈数字、括号汉字……）、`style`（书写风格）。相等性只比较 `clazz` 与 `number`——同一编号的不同写法视为同一个标记，这是脚注匹配的容错基础。

`HTMLTag` 容器与摊平器：

[types.py:10-22](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/types.py#L10-L22) `HTMLTag` 是泛型 dataclass；`flatten` 递归展开孩子序列，遇到 `HTMLTag` 就下钻，只对外产出 `str | P` 两类原子。

content.py 提供的三个实用操作（第四个 `expand_text_in_content` 上面已见）：

[content.py:29-37](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/content.py#L29-L37) `join_texts_in_content` 把相邻的 `str` 合并（OCR 分段留下的碎片缝合）；[content.py:9-26](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/content.py#L9-L26) 的 `first` / `last` 取首/末原子（`HTMLTag` 会递归下钻）。二者都依赖 [content.py:59-63](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/content.py#L59-L63) 的 `_search_content`：先深度优先 yield 所有嵌套列表、最后 yield 当前列表——即最里层的列表最先被处理。

#### 4.2.4 代码实践

**实践目标**：遍历一章的所有 `BlockLayout.content`，把内联成员按类型分桶统计，直观看到 `Content` 列表的混居结构。

**操作步骤**（示例代码）：

```python
# inspect_members.py（示例代码）
from collections import Counter
from pathlib import Path

from pdf_craft.extractor.chapter import (
    Chapter, InlineExpression, ParagraphLayout, Reference, decode,
)
from pdf_craft.markdown.paragraph import HTMLTag, flatten
from pdf_craft.common import read_xml

path = Path("out/package/chapters/chapter_1.xml")
chapter: Chapter = decode(read_xml(path))

counter = Counter()
samples: dict[str, object] = {}

def visit(content) -> None:
    for part in flatten(content):
        if isinstance(part, str):
            counter["str(文本)"] += 1
        elif isinstance(part, InlineExpression):
            counter["InlineExpression(公式)"] += 1
            samples.setdefault("公式", part.content)
        elif isinstance(part, Reference):
            counter["Reference(脚注引用)"] += 1
            samples.setdefault("脚注", part.id)
        else:
            counter[f"未知类型: {type(part).__name__}"] += 1

for layout in chapter.layouts:
    if isinstance(layout, ParagraphLayout):
        for block in layout.blocks:
            visit(block.content)
    else:  # AssetLayout 的三个内容区
        for name in ("title", "content", "caption"):
            visit(getattr(layout, name))

print(counter)
print("样例:", samples)
```

**需要观察的现象**：`str` 数量远超其他；含公式的书会出现 `InlineExpression`；带脚注的书（`tests/assets/citation.pdf`）会出现 `Reference`，其 `id` 形如 `(5, 7)` 的元组。

**预期结果**：分桶总数 = 全章 `flatten` 产出的原子数；不会出现「未知类型」桶——`flatten` 之后只剩 `str | BlockMember` 两类。

（脚本输出随 PDF 而异，待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：`Reference.mark` 为什么设计成 `str | Mark` 而不是只用 `Mark`？

**答案**：OCR 认出的「编号样字符」未必都在 `mark.py` 的编号表里（那是一张有限的人工枚举表）。能识别的升级为 `Mark`（携带数值语义，供脚注匹配比较），不能识别的保留原字符串——decode 时 [chapter.py:466-473](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L466-L473) 的 `transform2mark` 返回 `None` 就走字符串兜底。宽进严出，不丢内容。

**练习 2**：同一条脚注在正文被引用两次，decode 之后内存里有几个 `Reference` 对象？修改其中一个的 `mark` 会影响另一处吗？

**答案**：只有一个。两处 `<ref id="5-7"/>` 都通过 `references_map[(5, 7)]` 拿到同一个实例（对象共享），改 `mark` 两处同步可见——因为它们本来就是同一个对象。这也是 encode 时要去重（`search_references_in_chapter` 用 `seen` 集合）的原因：同一对象会被搜出两次。

**练习 3**：`_search_content` 为什么先 yield 深层列表、后 yield 当前列表？这个顺序对 `join_texts_in_content` 有影响吗？

**答案**：它是「先处理孩子再处理自己」的深度优先遍历。对合并操作而言，任何一个嵌套层级里的相邻 `str` 都会被某一次遍历覆盖到，顺序只影响处理次序、不影响最终结果——合并在每个列表内部独立完成，各层互不干扰。

### 4.3 XML 编解码

#### 4.3.1 概念说明

`chapter_N.xml` 是章节数据的磁盘形态，也是提取器与下游（渲染器、翻译转换器）之间的契约。先看一份典型文档的结构（示例代码，字段取值是示意）：

```xml
<chapter id="1" level="1">
  <body>
    <paragraph ref="title" level="1">
      <block page_index="5" order="2" det="100,200,500,240">第一章 引言</block>
    </paragraph>
    <paragraph ref="text">
      <block page_index="5" order="3" det="100,260,500,400">
        正文文字，脚注标记<ref id="5-7"/>，行内公式
        <inline_expr kind="$">E=mc^2</inline_expr>，以及<b>加粗</b>片段。
      </block>
    </paragraph>
    <asset ref="image" page_index="5" det="100,420,500,620" hash="ab12cd...">
      <caption>图 1-1 图示</caption>
    </asset>
  </body>
  <references>
    <ref id="5-7">
      <mark>①</mark>
      <paragraph ref="text">
        <block page_index="5" order="20" det="100,900,500,920">脚注内容……</block>
      </paragraph>
    </ref>
  </references>
</chapter>
```

三处设计值得注意：

1. **引用表后置**：正文里只放轻量的 `<ref id="页-序"/>`，真正的脚注内容集中在文档末尾的 `<references>` 表里。这类似编程语言「先引用、后定义」的两遍式结构。
2. **骨架与载荷分离**：HTML 标签的存取由 `markdown/paragraph/types.py` 的通用 `decode` / `encode` 处理（它们只认识白名单 HTML 标签），`<ref>` 与 `<inline_expr>` 这类**项目私有载荷**通过回调函数注入。chapter.py 不需要自己写标签遍历。
3. **省略规则**：`id` 属性缺省 = `Chapter.id is None`；`level` 属性缺省 = `-1`。编码时按此省略，解码时按此兜底。

#### 4.3.2 核心流程

**decode（XML → Chapter）是两遍式的**：

```
第一遍：扫描 <references> 下所有 <ref>
        ├─ 解析 id="page-order" → (page_index, order)
        ├─ 解析 <mark> → transform2mark 或原字符串
        └─ 解析脚注的 layouts（asset / paragraph，禁止再出现 <ref>）
        建立 references_map: {(page, order): Reference}

第二遍：遍历 <body> 的子元素
        ├─ <asset>  → _decode_asset（title/content/caption）
        └─ <paragraph> → _decode_paragraph
              └─ 每个 <block> 的孩子交给骨架 decode：
                    ├─ 白名单 HTML 标签 → 递归 HTMLTag
                    ├─ <ref id="p-o">   → references_map[(p, o)]（共享实例）
                    ├─ <inline_expr>    → InlineExpression
                    └─ 其他             → ValueError
```

**encode（Chapter → XML）是收集式的**：正文的 asset/paragraph 顺序输出；随后用 `flatten` 从**所有块**（含 `HTMLTag` 深处）搜出 `Reference` 对象、按 `id` 排序去重，统一写进 `<references>`。最后 `indent` 加缩进、`save_xml` 原子写盘。

**往返幂等性**：decode 会把缩进空白（`text` / `tail`）也收进 content 列表（[types.py:29-30](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/types.py#L29-L30) 与 L46-47 的 `if child.tail:`），再次 encode 会把这些空白写回原位置；而 `indent` 只覆盖「纯空白」的 text/tail（[xml.py:9-17](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/xml.py#L9-L17) 用 `not child.tail.strip()` 判断）。两股力量互相抵消，第二遍 encode 的输出与第一遍一致——这正是综合实践要验证的性质。

#### 4.3.3 源码精读

decode 主入口：

[chapter.py:85-115](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L85-L115) `decode`：第 86-91 行先建引用表，第 93-97 行解析 `id` / `level` 属性（缺省 `None` / `-1`），第 100-101 行强制要求 `<body>` 存在，第 104-109 行按标签名分派给 asset 或 paragraph 解码器。

encode 主入口：

[chapter.py:118-144](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L118-L144) `encode`：省略规则在第 121-125 行；第 135-136 行调用 `search_references_in_chapter` 收集引用并按 `ref.id` 排序——排序保证输出字节稳定，同一份数据每次编码结果相同。收集器实现在 [chapter.py:68-75](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L68-L75)，用 `seen` 集合对同一对象去重；遍历源 [chapter.py:147-152](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L147-L152) 只扫 `ParagraphLayout` 的块内容（`AssetLayout` 的内容区在 decode 时已禁止包含 `<ref>`，见下）。

载荷编解码的分界：

[types.py:25-49](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/types.py#L25-L49) 骨架 `decode`：遍历孩子的 `text` / `tail` 收集字符串；子标签若是白名单 HTML（`tag_definition` 命中）则递归成 `HTMLTag`，否则整颗交给 `decode_payload` 回调。chapter.py 注入的回调就是「ref / inline_expr / 其他报错」三路分派。[types.py:52-76](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/markdown/paragraph/types.py#L52-L76) 骨架 `encode` 完全对称：字符串按「上一个元素是否已出现」决定写进 `text` 还是 `tail`，载荷交给 `encode_payload`。

正文块里的载荷解码回调：

[chapter.py:338-385](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L338-L385) `decode_block_member`：`<ref>` 的 `id` 必须是 `"页-序"` 格式（第 346-357 行），查表命中返回共享实例、未命中抛错（第 359-370 行）；`<inline_expr>` 必须带合法 `kind`（第 372-380 行）；其余标签一律 `ValueError`。对应编码侧 [chapter.py:412-425](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L412-L425)：公式元素写 `kind` 属性与文本，引用元素只写 `id`。

引用表自身的解码：

[chapter.py:445-489](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L445-L489) `_decode_reference`：第 482 行给脚注布局传 `references_map=None`——脚注内部再出现 `<ref>` 会命中 [chapter.py:367-370](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L367-L370) 的「无表可查」报错，即**禁止引用嵌套**。同理 asset 内容区的回调 [chapter.py:179-192](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L179-L192) 直接拒绝 `<ref>`。这两道禁令保证引用关系是「正文 → 脚注」的单向二层结构，encode 的收集器才敢只扫正文块。

asset 的编解码：

[chapter.py:154-221](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L154-L221) `_decode_asset` 校验 `ref` 必须在 `ASSET_TAGS` 内、`page_index` 是整数、`det` 是四个整数，再分别解码三个内容区；[chapter.py:224-259](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L224-L259) `_encode_asset` 空的内容区直接不写元素。注意 asset 没有 `order` 字段——一个资源块对应一次完整裁剪，无页内序号概念。

落盘的最后两步：

[xml.py:5-18](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/xml.py#L5-L18) `indent` 自实现两级空格缩进（只动「纯空白」的 text/tail，业务文本原样保留）；[xml.py:28-40](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/common/xml.py#L28-L40) `save_xml` 先写 `.xml.tmp` 临时文件再 `replace` 原子替换——即使中途崩溃也不会留下半截文件，这与 u4-l3 `toc.xml` 的「文件存在即完整」语义配套。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：完成讲义规格中的往返实验——把一个 `chapter_N.xml` 用官方 `decode` 读成对象树并打印结构，再 `encode` 写回，验证两次文件内容等价（忽略缩进）。

**操作步骤**（示例代码）：

```python
# roundtrip_chapter.py（示例代码）
from pathlib import Path
from xml.etree.ElementTree import tostring

from pdf_craft.extractor.chapter import (
    Chapter, InlineExpression, ParagraphLayout, Reference, decode, encode,
)
from pdf_craft.common import read_xml, save_xml
from pdf_craft.markdown.paragraph import flatten

src = Path("out/package/chapters/chapter_1.xml")   # 上一实践的产物路径

# ── 第 1 步：官方 decode，手工打印对象树 ──────────────────────
element_before = read_xml(src)
chapter = decode(element_before)

def dump(chapter: Chapter) -> None:
    print(f"Chapter(id={chapter.id}, level={chapter.level})")
    for i, layout in enumerate(chapter.layouts):
        if isinstance(layout, ParagraphLayout):
            print(f"  [{i}] ParagraphLayout(ref={layout.ref!r}, level={layout.level})")
            for block in layout.blocks:
                atoms = []
                for part in flatten(block.content):
                    if isinstance(part, str):
                        atoms.append(repr(part.strip()[:20] or "<空白>"))
                    elif isinstance(part, InlineExpression):
                        atoms.append(f"公式({part.kind.name}:{part.content[:15]})")
                    elif isinstance(part, Reference):
                        atoms.append(f"引用(id={part.id}, mark={part.mark!r})")
                print(f"      Block(p={block.page_index}, o={block.order}, "
                      f"det={block.det}): {' | '.join(atoms)}")
        else:
            print(f"  [{i}] AssetLayout(ref={layout.ref!r}, hash={layout.hash})")

dump(chapter)

# ── 第 2 步：encode 写回另一个文件，归一化比较 ────────────────
element_after = encode(chapter)
dst = src.with_suffix(".roundtrip.xml")
save_xml(element_after, dst)

def normalized(element) -> list[str]:
    text = tostring(element, encoding="unicode")
    return [line.strip() for line in text.splitlines() if line.strip()]

same = normalized(element_before) == normalized(element_after)
print("\n往返等价（忽略缩进与空行）:", same)
assert same, "两次内容存在差异，请检查！"
```

**需要观察的现象**：

- `dump` 打印出的树与 4.3.1 的 XML 示例逐层对应：章 → 布局 → 块 → 原子序列；
- `flatten` 摊平后，`str` 原子里夹杂着缩进空白片段（形如 `'\n  '`）——这是 4.3.2 说的「decode 收缩进空白」的直接证据；
- 最后的断言通过。

**预期结果**：`往返等价（忽略缩进与空行）: True`。归一化函数逐行 `strip` 并丢弃空行，恰好消除了 `indent` 造成的唯一差异来源。如果想进一步验证严格幂等，可把 `normalized` 换成逐行原样比较（不 strip），大概率也相等——因为空白 text/tail 会被 `indent` 重置为相同值。

（`tests/assets/mix.pdf` 等样本是否含公式、脚注视文件而定；无脚注的 PDF 不会出现 `引用(...)` 原子。运行结果待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：如果把 `chapter_1.xml` 里某个 `<ref id="5-7"/>` 手工改成 `id="99-0"`（引用表里没有），decode 会发生什么？

**答案**：抛 `ValueError: ... references undefined reference: 99-0`（[chapter.py:359-366](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L359-L366)）。引用是「先定义后使用」的强约束，悬空引用直接失败而不是静默丢弃——宁可报错也不产出语义残缺的章节。

**练习 2**：encode 时为什么必须对 `search_references_in_chapter` 的结果排序？不排序会怎样？

**答案**：引用是从散布各块的对象里收集来的，收集顺序取决于正文中出现的位置。不排序则 `<references>` 内条目顺序不稳定，同一本书两次重算可能产出字节不同的 XML，破坏「重算可复现」与 diff 对比。排序键 `ref.id = (page_index, order)` 是页码序，与生成侧写入顺序天然一致（[chapter.py:135-136](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L135-L136)）。

**练习 3**：`<asset>` 的内容区为什么禁止 `<ref>`？从「encode 收集器的扫描范围」角度回答。

**答案**：因为收集器 `_search_parts_in_chapter` 只遍历 `ParagraphLayout` 的块（[chapter.py:147-152](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L147-L152)）。如果允许 asset 里藏引用，encode 就会漏掉它——decode 读回时该 `<ref>` 查不到表而报错。decode 侧的显式拒绝（[chapter.py:179-181](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py#L179-L181)）把这条不变式挡在入口，两端的扫描范围由此对齐。

## 5. 综合实践

把本讲三个模块串成一个「章节体检 + 安全改写」小任务：

1. **准备**：按 u1-l2 的方式对 `tests/assets/citation.pdf`（带脚注，脚注引用多）跑一次 `convert_pdf_to_markdown`，保留 `package_path`。
2. **体检**（布局 + 内联成员）：写脚本遍历全部 `chapter_*.xml`，输出一张总表——每章的段落数、资源数（按 `ref` 分列）、`InlineExpression` 数、`Reference` 数、被引用次数最多的脚注 `id`。
3. **改写**（编解码）：选一个章节对象，做一次最小改写——例如把每个 `str` 原子里的中文冒号替换为英文冒号（直接改 `str` 是安全的，对象是可变的 dataclass）——然后 `encode` + `save_xml` 覆盖写回 `chapter_N.xml`。
4. **验证**：重新 `decode` 读回，确认改写已生效；再对改写后的文件做一次 4.3.4 的往返等价检查，确认你的改写没有破坏编码幂等性。
5. **思考题**：为什么改 `chapter_N.xml` 是「安全实验」而改 `toc.xml` 影响更持久？（提示：`generate_chapter_files` 开头会删除所有 `chapter_*.xml` 重建，而 `analyse_toc` 见 `toc.xml` 存在即短路——见 u4-l3。）

完成后再运行一次 `render_markdown`（或整段 `convert_pdf_to_markdown` 但复用同一 `package_path`），你会看到 Markdown 输出带着你的改写出现——这是「中间包是可编辑契约」最直接的体验。

（第 2-4 步脚本可由 4.1.4 与 4.3.4 的脚本拼装而成，运行结果待本地验证。）

## 6. 本讲小结

- 一章 = `Chapter`，其 `layouts` 是 `ParagraphLayout`（文字段落）与 `AssetLayout`（图片/表格/公式资源，`ref` 限 `image|table|equation`）的有序混排；段落之下是 `BlockLayout`，携带出生坐标 `page_index` / `order` / `det`，供溯源与 PDF 回写定位。
- 块的内容是 `Content = list[str | InlineExpression | Reference | HTMLTag[...]]`：普通文本、LaTeX 公式（`ExpressionKind` 五种定界符）、脚注引用（`id = (page_index, order)`，对象共享）、白名单 HTML 容器四类居民混居。
- `Reference` 是「指针」：正文存轻量引用，脚注内容挂在引用定义上；`mark` 是 `str | Mark`，可识别的编号字符升级为携带数值语义的 `Mark`。
- `chapter_N.xml` 的 decode 是两遍式（先建 `<references>` 引用表、再解 `<body>`，悬空引用即报错）；encode 是收集式（`flatten` 深搜引用、按 `id` 排序去重后置表），HTML 标签的存取复用 `markdown/paragraph` 的骨架编解码加回调注入。
- 三条不变式守护数据一致性：asset 与脚注内部禁止 `<ref>`（保证引用单向二层）、`level`/`id` 缺省即 `-1`/`None`、`save_xml` 临时文件原子写；往返编解码幂等，因此 `chapter_N.xml` 可以安全地手工编辑再被下游消费。

## 7. 下一步学习建议

本讲你拿到了「章节数据长什么样」，但 `layouts` 里的内容从何而来还没展开。下一讲 **u5-l2《Jointer：跨页文本块的合并》** 精读 `jointer.py` / `mergeable.py` / `reading_serials.py`，回答：`ocr/page_N.xml` 里的零散块如何被判定「可以合并」、表格标题与脚注正则如何分类、LaTeX 公式在哪一步被解析成 `InlineExpression`。之后 u5-l3 讲章节切分循环（`_generate_chapters` 的完整逻辑），u5-l4 讲脚注 `Reference` 的匹配生成与标点归一化。若你想先验证本讲的改写实验对渲染的影响，也可以提前跳读 u6-l3《Markdown 渲染器》。
