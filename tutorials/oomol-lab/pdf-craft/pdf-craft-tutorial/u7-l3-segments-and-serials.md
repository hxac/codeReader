# 文本片段与序列切分

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清三类片段的职责划分：`TextSegment`（一个非空文本节点）、`InlineSegment`（一个块级元素所辖的全部内联内容）、`BlockSegment`（一次翻译请求的 id 化快照），以及它们之间「树 → 流 → 组」的加工顺序。
2. 解释「分数（score）」与「token 数」的区别：为什么分组预算要按 *XML 渲染 token 数 + 每个带 id 父标签 80 分* 计算，而不是只数文本 token。
3. 沿 `search_text_segments` → `search_inline_segments` → `expand_to_score_segments` → `resource_segmentation.split` 走完「元素变成翻译组」的全过程，理解切缝（incision）标注如何引导切分点落在结构边界上。
4. 读懂 `serial` 子包（`Segment` 协议、`split_into_chunks`、`splitter.split`）的「头—身—尾三明治」设计：上下文如何进入请求、又如何从结果中剥掉，保证每个片段恰好被翻译一次。
5. 独立构造含 `<b>`、`<i>`、MathML 公式的 XML，用 `serial.split` 做切分实验，验证切分边界永远落在完整标签之外，并观察 `max_group_tokens` 对组大小的影响。

## 2. 前置知识

本讲默认你已读完 u7-l2（XMLTranslator 核心编排）。再补充几个本讲反复出现的概念：

- **内联元素与块级元素**：HTML/XHTML 把标签分成两类。`<b>`、`<i>`、`<span>`、`<sub>` 这类「内联元素」只包裹句子中的一小段文字，不换行；`<p>`、`<div>`、`<li>` 这类「块级元素」自成一段。这个区分是本讲一切切分逻辑的基石：**块级元素是翻译单元，内联元素必须与它包裹的文字同生共死**——把 `<b>` 的开始标签和结束标签切进两个翻译请求，回填时就无法复原结构。MathML 公式标签（`<mi>`、`<mfrac>` 等）也算内联，所以公式天然不会被切断。
- **Element 的 text 与 tail**：Python 标准库 `xml.etree.ElementTree` 里，一个元素的文字不一定在它自己身上。`<p>A<b>B</b>C</p>` 中，`"A"` 是 `p.text`，`"B"` 是 `b.text`，而 `"C"` 是 `b.tail`——紧跟在 `<b>` 结束标签之后的文字挂在**前一个兄弟元素的 tail** 上。本讲的 `TextSegment` 正是把这两种位置统一成流。
- **生成器（generator）与流式处理**：`yield` 函数产出惰性序列。全书可能上千个块级元素，pdf-craft 从不把它们一次性装进内存，而是一路生成器到底（u7-l2 已见过 `translate_elements`）。
- **token 与 tiktoken**：LLM 按 token 计费、按 token 限定上下文长度。tiktoken 在本地把字符串切成 token 序列（u2-l3 讲过 `LLM.token_encoding`）。本讲的「分数」就是在 token 数之上再加权得到的预算单位。
- **为什么需要「切分」**（本讲最重要的直觉）：一本书的 XML 远超一次请求的上下文上限，必须切成若干组分别请求。但朴素地「每 1000 token 切一刀」会把句子、甚至 `<b>...</b>` 标签对拦腰斩断。pdf-craft 的解法分三步：先把 XML 树打碎成**不可再分的原子片段**（块级元素），再给每个原子标注**切缝等级**（哪里好切、哪里不好切），最后交给通用算法在预算内选最优切点，并为每组附带**头尾上下文**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pdf_craft/transformer/xml_translator/segment/text_segment.py` | `TextSegment` 数据类与 `search_text_segments`：把 XML 树展开成文本节点流；定义切缝分数 `incision_between` |
| `pdf_craft/transformer/xml_translator/segment/inline_segment.py` | `InlineSegment` 与 `search_inline_segments`：用栈状态机把文本流重组为「块级元素级」的翻译单元；含 id 分配、校验与回填匹配 |
| `pdf_craft/transformer/xml_translator/segment/block_segment.py` | `BlockSegment`：一组 `InlineSegment` 的 id 化容器，服务回填阶段的校验与提交 |
| `pdf_craft/transformer/xml_translator/xml/inline.py` | `is_inline_element`：内联标签白名单（HTML + MathML + `display` 属性） |
| `pdf_craft/transformer/xml_translator/xml_translator/score.py` | `ScoreSegment`：给片段打「分数」（XML 渲染 token 数 + id 加权），以及预算内截断 |
| `pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py` | `XMLStreamMapper`：生产路径上的分组器，把上面所有部件串起来 |
| `pdf_craft/transformer/xml_translator/serial/segment.py` | `Segment` 协议：任何「带 token 数、可截断」的类型都能被切分 |
| `pdf_craft/transformer/xml_translator/serial/chunk.py` | `split_into_chunks`：对上游 `resource_segmentation` 库的封装，产出 `Chunk(head, body, tail)` |
| `pdf_craft/transformer/xml_translator/serial/splitter.py` | `split`：头—身—尾三明治执行器，上下文进请求、结果剥头尾 |
| `pdf_craft/transformer/xml_translator/xml_translator/translator.py` | 消费方：`XMLTranslator` 把分组结果交给翻译与回填 |
| `pdf_craft/pipeline/epub/translation/translator.py` | 顶层入口：`max_group_tokens=2600` 默认值在这里暴露给用户 |

> 一个诚实的前置说明：`serial/` 子包目前在库内**没有调用方**——生产路径（EPUB 翻译、章级转换）走的是 `stream_mapper.py`，它直接调用上游 `resource_segmentation` 库并自带一套等价的「三明治」实现。`serial/` 是同一思想的**通用化、可独立使用的参考实现**（面向「任意满足 Segment 协议的类型」），也是本讲动手实验最方便的入口。两者共享同一个上游库与同一套设计词汇。

## 4. 核心概念与源码讲解

### 4.1 片段类型：TextSegment、InlineSegment 与 BlockSegment

#### 4.1.1 概念说明

把一棵章节 XML 变成可翻译的组，第一步是「降维打击」：树形结构不方便按 token 切，先把它摊平成流。pdf-craft 定义了三级片段，各自回答一个问题：

| 片段 | 回答的问题 | 粒度 |
| --- | --- | --- |
| `TextSegment` | 「哪里有文字？」 | 一个非空文本节点（`text` 或 `tail`） |
| `InlineSegment` | 「哪些文字必须一起翻译？」 | 一个块级元素（如一个 `<p>`）辖内的全部内联内容 |
| `BlockSegment` | 「这一次请求里每个片段是谁？」 | 一组 `InlineSegment` 的 id 化快照，专供回填校验 |

命名提示：`InlineSegment` 很容易被误读为「内联标签的片段」。看完源码你会发现，它实际代表的是**一个块级元素内的最大内联内容连续段**——它的 `parent` 是 `<p>` 这类块级元素本身，`<b>`、`<i>` 作为嵌套子片段挂在里面。它是送进 LLM 的最小独立翻译单元。

#### 4.1.2 核心流程

```text
XML 元素树
   │  search_text_segments(root)          深度优先遍历，摊平成文本节点流
   ▼
TextSegment 流（携带 parent_stack、block_depth、position）
   │  search_inline_segments(流)          栈状态机，按块级元素分组打包
   ▼
InlineSegment 流（一个块级元素 = 一个翻译单元，内含嵌套内联片段）
   │  expand_to_score_segments(...)        打分（4.2 节）
   ▼
翻译组（每组 ≤ max_group_score）
   │  BlockSegment(inline_segments)       进入回填阶段时 id 化
   ▼
校验 / 修复 / 提交（u7-l2 与 u7-l5 的内容）
```

两个关键不变式：

1. **文本不重不漏**：`search_text_segments` 按「自身 text → 各子元素（递归）→ 各子元素 tail」的顺序遍历，每个非空文本节点恰好产出一次。
2. **标签永不半开**：`InlineSegment` 以块级元素为界整体打包，任何后续切分都只发生在 `InlineSegment` 之间，`<b>...</b>` 因此永远同组。

#### 4.1.3 源码精读

**先看内联判定。** 一切切分都建立在「谁是内联标签」这个白名单上：

- [pdf_craft/transformer/xml_translator/xml/inline.py:L9-L106](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/inline.py#L9-L106)：`_HTML_INLINE_TAGS` 冻结集合，收录 MDN 定义的全部 HTML 内联元素，外加完整的 MathML 标签族（`mi`、`mfrac`、`msub`……）。公式标签全部内联，意味着 MathML 公式整体属于某个翻译单元内部，天然不可被切断。
- [pdf_craft/transformer/xml_translator/xml/inline.py:L109-L120](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/inline.py#L109-L120)：`is_inline_element` 的三级判定——在白名单中即为内联；否则看 `display` 属性是否为 `inline`；`math` 标签只要 `display` 不是 `"block"` 也算内联。这给了 XHTML 一条逃生通道：非标准标签可以靠 `display="inline"` 自证身份。

**TextSegment：一个文本节点的全部身份信息。**

- [pdf_craft/transformer/xml_translator/segment/text_segment.py:L20-L27](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/text_segment.py#L20-L27)：`TextSegment` 的六字段——`text`（归一化后的文本）、`parent_stack`（从根元素到文本所属元素的完整祖先链）、`left_common_depth` / `right_common_depth`（与左右邻居共享的祖先深度，稍后用于计算切缝）、`block_depth`（祖先链上最后一个**非内联**元素的深度）、`position`（`TEXT` 还是 `TAIL`）。
- [pdf_craft/transformer/xml_translator/segment/text_segment.py:L29-L39](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/text_segment.py#L29-L39)：三个派生属性。`depth = len(parent_stack) - block_depth`，即**相对块级祖先的深度**——直接内联在 `<p>` 里的文字 depth 为 0，包在 `<b>` 里的 depth 为 1；`block_parent` 就是那个块级祖先（翻译单元的拥有者）。
- [pdf_craft/transformer/xml_translator/segment/text_segment.py:L105-L130](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/text_segment.py#L105-L130)：递归遍历。注意 `normalize_text_in_element` 会把纯空白文本过滤成 `None`（跳过），把连续空白折叠成单个空格——所以「空白尾巴」不会产生片段。
- [pdf_craft/transformer/xml_translator/segment/text_segment.py:L83-L102](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/text_segment.py#L83-L102)：外层包装器在相邻两个片段之间计算 `_common_depth`（两条祖先链从根开始逐位比对 `id()`，Element 不可哈希但可比较身份），分别写入前者的 `right_common_depth` 与后者的 `left_common_depth`。这两个值是「这两个文本节点在结构上离得多远」的原始材料。
- [pdf_craft/transformer/xml_translator/segment/text_segment.py:L133-L138](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/text_segment.py#L133-L138)：`find_block_depth` 沿祖先链找**最后一个非内联元素**的下标（+1 得到「个数语义」的深度）。这条链上越靠后的内联包裹越不影响块级归属。
- [pdf_craft/transformer/xml_translator/segment/text_segment.py:L65-L80](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/text_segment.py#L65-L80)：`incision_between` 把相邻片段间的结构距离折算成切缝分：块级差 1 层记 3 分，内联差 1 层记 1 分，行尾注释写明设计意图——「数字越大越容易被拆分」。也就是说，**两个文本节点之间隔的块级边界越多，在这里下刀越安全**。该函数目前在生产路径上未被直接调用（`stream_mapper` 用的是二元切缝常量，见 4.2.3），但它是理解「切缝」词汇的最好注脚。

**InlineSegment：栈状态机组装翻译单元。**

- [pdf_craft/transformer/xml_translator/segment/inline_segment.py:L39-L78](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/inline_segment.py#L39-L78)：`search_inline_segments` 主循环。`stack` 是「每层深度一个桶」的列表，`stack_data` 三元组绑定 `(stack, 当前块级元素, 基准深度)`。每来一个 `TextSegment`：若 `block_parent` 变了（进入了新的块级元素），先把旧栈全部弹出并 yield 上一个打包好的片段；否则按 `text_segment.depth` 把片段放进对应桶。注释（[L71](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/inline_segment.py#L71)）点破关键不变式：`depth` 恰好是片段在 `stack` 中的下标，必须维持 `len(stack) == depth + 1`。
- [pdf_craft/transformer/xml_translator/segment/inline_segment.py:L92-L103](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/inline_segment.py#L92-L103)：`_pop_stack` 弹出一个桶。桶非空才生成 `InlineSegment(depth, 桶内容)`，并把它**塞回下一层桶的末尾**——这就是「弹栈成树」：内层的 `<i>` 片段成为外层片段的 child，最外层那次弹出得到整个块级元素。每个块级元素最终只向外 yield 一次。
- [pdf_craft/transformer/xml_translator/segment/inline_segment.py:L106-L130](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/inline_segment.py#L106-L130)：构造函数里的 **id 经济学**。`_parent_stack` 取首个孩子祖先链的前 `depth` 层，`parent` 即块级元素。核心是 L120-L130：按 tag 用 `nest` 分组统计子内联片段，仅当同 tag 子片段的「指纹」（[utils.py:L6-L8](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/utils.py#L6-L8) 把属性排序拼成字符串）**彼此不同**时才逐个分配临时 id（`is_the_same` 判等，[xml_translator/utils.py:L15-L25](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/utils.py#L15-L25)）。注释写明动机：**能靠 tag 数量区分就不发 id，id 越少越省 token**——4.2 节会看到每个 id 在分组预算里价值 80 分。
- [pdf_craft/transformer/xml_translator/segment/inline_segment.py:L160-L165](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/inline_segment.py#L160-L165)：`__iter__` 深度优先展开全部 `TextSegment`（跳过中间的内联层），供翻译阶段拼纯文本。
- [pdf_craft/transformer/xml_translator/segment/inline_segment.py:L189-L210](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/inline_segment.py#L189-L210)：`create_element` 从片段**重建**元素：子内联片段递归建元素，文本按「前一个子元素是否存在」决定写进 `element.text` 还是 `previous_element.tail`——严格还原 ElementTree 的 text/tail 语义。这是「片段 → XML」的可逆通道。

**BlockSegment：一次请求的 id 化快照。**

- [pdf_craft/transformer/xml_translator/segment/block_segment.py:L47-L56](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/block_segment.py#L47-L56)：构造函数用 `IDGenerator`（[utils.py:L21-L27](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/utils.py#L21-L27)，自增计数器）给组内每个 `InlineSegment` 发**全局递增**的正式 id，再由 `recreate_ids`（[inline_segment.py:L175-L187](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/inline_segment.py#L175-L187)）沿嵌套结构向下重发。`_id2inline_segment` 字典让回填阶段能按 id 反查片段。
- [pdf_craft/transformer/xml_translator/segment/block_segment.py:L61-L65](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/block_segment.py#L61-L65)：`create_element` 用统一根标签（调用处传的是 `"xml"`，见 [xml_translator/translator.py:L123-L126](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L123-L126)）把整组片段拼成回填请求的 XML 文档。
- [pdf_craft/transformer/xml_translator/segment/block_segment.py:L67-L108](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/block_segment.py#L67-L108)：`validate` 校验 LLM 回填结果——根标签是否正确、每个子元素 id 是否在预期集合内（多出的 yield `BlockUnexpectedIDError`、缺失的 yield `BlockExpectedIDsError`、tag 不符的 yield `BlockWrongTagError`），再委托给每个片段自身的 `validate`（[inline_segment.py:L212-L244](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/inline_segment.py#L212-L244)）查内层结构。
- [pdf_craft/transformer/xml_translator/segment/block_segment.py:L110-L124](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/block_segment.py#L110-L124)：`submit` 按 id 把 LLM 产出的元素与原片段配对，产出 `BlockSubmitter`（含原文文本段与经 `assign_attributes` 回填属性的结果元素），交给 u7-l5 的提交器落地。即便校验错误没清零，[inline_segment.py:L283-L323](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/inline_segment.py#L283-L323) 的 `assign_attributes` 也会尽力匹配一个「质量最高的版本」——尽力而为，不轻易放弃。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「树 → TextSegment 流 → InlineSegment 树」的两次变换，验证内联标签被完整包裹。

**操作步骤**（以下为示例代码，保存为 `seg_demo.py`，在安装了 pdf-craft 的环境中运行）：

```python
# 示例代码
from xml.etree.ElementTree import fromstring, tostring

from pdf_craft.transformer.xml_translator.segment import (
    search_inline_segments,
    search_text_segments,
)

XML = (
    '<p>hello <b>bold</b> and <i>italic <sub>sub</sub> tail</i>'
    '<span class="x">end</span></p>'
)

root = fromstring(XML)

print("== 第一步：TextSegment 流 ==")
for s in search_text_segments(root):
    print(
        f"{s.position.name:5} depth={s.depth} "
        f"block_depth={s.block_depth} "
        f"stack={[e.tag for e in s.parent_stack]} "
        f"text={s.text!r}"
    )

print("== 第二步：InlineSegment（每个块级元素一个） ==")
for seg in search_inline_segments(search_text_segments(root)):
    print("parent tag:", seg.parent.tag, "| id:", seg.id)
    print("  xml:", tostring(seg.create_element(), encoding="unicode"))
```

**需要观察的现象**：

1. 第一步应输出 7 个 `TextSegment`，顺序为 `hello `、`bold`、` and `、`italic `、`sub`、` tail`、`end`；`bold` 的 `stack` 是 `['p', 'b']`、depth=1，而 ` and `（`b.tail`）的 `stack` 只有 `['p']`、depth=0、position=TAIL。纯空白文本（如 `<span>` 前若有的话）不会出现。
2. 第二步**只应输出一个** `InlineSegment`：parent 是 `p`（整个块级元素被打包成一个翻译单元），其 children 里嵌着 `b`、`i`、`span` 三层内联片段，`i` 里面还有 `sub`。
3. 输出片段的 `id` 是 `0`——这是 [inline_segment.py:L50](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/inline_segment.py#L50) 在 yield 前打的占位标记；嵌套子片段的 `id` 多为 `None`（本例各 tag 只出现一次，靠 tag 即可区分，无需发 id），正式 id 要到 `BlockSegment` 构造时才统一分配（4.1.3 最后一段）。

**预期结果**：`create_element()` 重建的 XML 与原文结构等价——标签层级、文本与 tail 的位置全部还原（本例 `p` 无属性，所以完全一致）。注意 `create_element` 只取 `parent.tag` 不复制属性（[inline_segment.py:L190](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/inline_segment.py#L190)），若给 `p` 加属性会看到它被丢弃——属性还原走的是 `assign_attributes`（4.1.3 最后一段）。具体打印格式以本地运行为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`<p>A<b>B</b>C<i>D</i></p>` 会产出几个 `TextSegment`、几个 `InlineSegment`？

**答案**：4 个 `TextSegment`——`A`（`p.text`，TEXT）、`B`（`b.text`，TEXT，depth=1）、`C`（`b.tail`，TAIL，depth=0）、`D`（`i.text`，TEXT，depth=1）。`InlineSegment` 只有 1 个：整段打包成以 `p` 为 parent 的片段，`b`、`i` 是它的嵌套子片段，不会单独向外 yield。

**练习 2**：为什么 `TextSegment` 要同时携带完整的 `parent_stack` 而不只是直接父元素？

**答案**：三个用途。其一，`depth` 与 `block_parent` 需要 `block_depth`（祖先链上最后一个非内联元素的位置）配合 `parent_stack` 长度才能算出，只有直接父元素不够；其二，`InlineSegment` 构造时要截取 `parent_stack[:depth]` 重建自己的祖先链与块级归属；其三，相邻片段的 `left/right_common_depth` 要逐层比较两条祖先链来度量结构距离。文本节点的「身份」大半由它的位置而非内容决定。

**练习 3**：`InlineSegment.__init__` 里，什么情况下同 tag 的子片段会**不**分配 id？这个设计的收益是什么？

**答案**：当同 tag 子片段的 `element_fingerprint`（tag + 排序后的属性）完全一致时（`is_the_same` 为真），它们可以仅靠 tag 与出现顺序互相区分，不需要 id。收益是省 token：回填阶段每个带 id 的父标签在分组分数里要加 80 分（4.2 节），id 越少，同样预算能塞进越多正文；校验时也更简单——直接按 tag 计数比对即可（`InlineWrongTagCountError`）。

### 4.2 分组策略：按 token 分数上限切组

#### 4.2.1 概念说明

有了原子片段，下一步是装车：每辆车的载重上限是 `max_group_score`（EPUB 翻译入口暴露为 `max_group_tokens`，默认 2600）。但「载重」不是文本 token 数，而是**分数**：

\[ \text{score}(s) = \bigl|\,\text{encode}(\text{render}_{\text{XML}}(s))\,\bigr| \;+\; 80 \times \bigl|\{\, p \in \text{left\_parents}(s) : p.id \neq \text{None} \,\}\bigr| \]

第一项是该片段**连同它的 XML 包裹**渲染成字符串后的 token 数——因为回填阶段 LLM 看到的是带标签的 XML，预算必须按 LLM 实际看到的东西算；第二项是「id 税」：片段每被一个带 id 的父片段包裹，就在请求里多撑起一条 `id="N"` 骨架，按经验值每个 80 分计提。公式里的 80 就是 [score.py:L10](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/score.py#L10) 的 `_ID_WEIGHT`。

#### 4.2.2 核心流程

```text
InlineSegment 流
   │ expand_to_score_segments(encoding, inline_segment)
   │    每个 TextSegment → 一个 ScoreSegment（带左右父片段链与分数）
   ▼
Resource(count=Σscore, start_incision, end_incision, payload=(片段, 打分明细))
   │ resource_segmentation.split(max_segment_count=max_group_score, ...)
   │    在预算内选切点，产出 Group(head, body, tail, *_remain_count)
   ▼
跨元素合并（stream_mapper._split_into_serial_groups 的 while 循环）
   ▼
翻译组：head/tail 作为上下文进入请求，只有 body 计入「已翻译」
```

打分的伪代码：

```text
for 每个 text_segment（在 inline_segment 的展开序列中）:
    左侧入栈一个内联父片段 → 结算上一个 ScoreSegment（它从此多了右父链）
    xml = 左父标签们(占位 id="99") + 文本 + 右父闭合标签们
          （首个片段再加 data-orig-len="9999" 占位）
    score = len(encoding.encode(xml)) + 80 × 带id左父数
```

#### 4.2.3 源码精读

- [pdf_craft/transformer/xml_translator/xml_translator/score.py:L14-L20](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/score.py#L14-L20)：`ScoreSegment` 数据类——`text_segment`、`left_parents` / `right_parents`（包裹它的内联片段链，用于渲染 XML 骨架）、`text_tokens`（纯文本 token 序列）、`score`。
- [pdf_craft/transformer/xml_translator/xml_translator/score.py:L99-L149](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/score.py#L99-L149)：`_do_expand_inline_segment` 用「进入片段记 UP、离开记 DOWN」的事件流（`_expand_as_wrapped`，[L157-L164](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/score.py#L157-L164)）把嵌套结构摊平：遇到 UP 时先结算上一个 `ScoreSegment` 并清空左链（说明上一个片段已被完整闭合），遇到 DOWN 时把片段挂进右链（或清空左链，见 L138）。每个 `ScoreSegment` 因此知道自己被哪些标签前开后闭。
- [pdf_craft/transformer/xml_translator/xml_translator/score.py:L23-L35](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/score.py#L23-L35)：`expand_to_score_segments` 完成打分：用 tiktoken 编码渲染出的 XML 字符串，再加上左父链中带 id 者的 80 分/个。
- [pdf_craft/transformer/xml_translator/xml_translator/score.py:L79-L97](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/score.py#L79-L97)：`_render_score_segment` 渲染骨架：左父标签里 id 一律写成占位 `id="99"`（估算用，不必与真实 id 等长），首个片段额外注入 `data-orig-len="9999"`（真实请求里这个属性记录原文长度，供回填阶段参考，见 [xml_translator/common.py:L1](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/common.py#L1)）。
- [pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py:L13-L14](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L13-L14)：生产路径的切缝只有两档——`_PAGE_INCISION = 0`（元素边界，即两个章节/文档之间）与 `_BLOCK_INCISION = 1`（同一元素内部的块级边界）。对照 4.1.3 里 `incision_between` 的「分数越高越好切」，这两档的含义是：**块级元素之间的缝比文档边界更容易被选为切点**（前者数值更高）。
- [pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py:L128-L162](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L128-L162)：`_expand_to_resources` 把 `InlineSegment` 流变成 `Resource` 流：相邻两个片段若 `head.root is tail.root`（同属一个输入元素）则边界标 `_BLOCK_INCISION`，否则标 `_PAGE_INCISION`；整个元素的首尾边界固定 `_PAGE_INCISION`。注意它逐对比较 `id()` 来决定切缝档位——结构信息在进入通用算法前就编码完毕。
- [pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py:L164-L181](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L164-L181)：`_transform_to_resource` 计算资源的 `count = Σ score`——即这个块级元素进入预算的重量，`payload` 同时携带片段本身与打分明细（截断时要用）。
- [pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py:L71-L106](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L71-L106)：`_split_into_serial_groups` 两级分组。内层对每个输入元素调用 `resource_segmentation.split(max_segment_count=self._max_group_score, border_incision=_PAGE_INCISION, ...)` 得到初步的 `Group`；外层 while 循环**跨元素合并**：只要「已累计 body 重量 + 下一组 body 重量 + 下一组尾部预留 ≤ max_group_score」就把下一组的 body 并进来（L96-L104）。这解释了一个容易被忽略的事实——**一个翻译组可以横跨两个章节文件**，切组只认预算与切缝，不认文件边界。
- [pdf_craft/transformer/xml_translator/xml_translator/score.py:L38-L76](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/score.py#L38-L76)：`truncate_score_segment` 处理「预算不够装下整个片段」：把分数拆成 `fixed_score`（XML 骨架 + id 税，**截断文字减不掉**）与文本 token 两部分；若剩余预算连固定部分都盖不住就直接放弃整段（返回 `None`，L44-L49 的注释解释了「删光文字才达标不如整段不要」）；否则按 tiktoken 的 token 序列切前/后 N 个再 `decode` 回文本，并补 `...` 省略号。截断后的片段仍是合法片段——这正是「切分不破标签」的最后一道保险。
- [pdf_craft/pipeline/epub/translation/translator.py:L40-L72](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L40-L72)：顶层入口 `translate()` 的 `max_group_tokens: int = 2600`（L47）一路传成 `XMLTranslator(max_group_score=...)`（L70）。2600 是「每组预算」的默认值——想减少请求次数可调大，想降低单请求失败重试成本可调小。

关于上游 `resource_segmentation` 库（PyPI 包 `resource-segmentation>=0.0.7`，见 [pyproject.toml:L40](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L40)）：pdf-craft 只使用它的 `Resource` / `Segment` / `Group` / `split` 四个名字，其内部选点算法不在本仓库源码内，本讲只基于调用方式解读其接口契约（`max_segment_count` 为预算、`gap_rate` / `tail_rate` 为上下文占比、`border_incision` 为边界切缝基准），算法细节待确认。

#### 4.2.4 代码实践

**实践目标**：算出真实片段的分数，验证「id 税」与「XML 骨架税」的存在。

**操作步骤**（示例代码）：

```python
# 示例代码
import tiktoken
from xml.etree.ElementTree import fromstring

from pdf_craft.transformer.xml_translator.segment import (
    search_inline_segments, search_text_segments,
)
from pdf_craft.transformer.xml_translator.xml_translator.score import (
    expand_to_score_segments,
)

encoding = tiktoken.get_encoding("cl100k_base")

XML = '<p>hello <b>bold</b> world</p>'
inline = next(search_inline_segments(search_text_segments(fromstring(XML))))

total = 0
for s in expand_to_score_segments(encoding=encoding, inline_segment=inline):
    plain = len(encoding.encode(s.text_segment.text))
    print(
        f"text={s.text_segment.text!r:20} 纯文本token={plain:3} "
        f"分数={s.score:3} 左父数={len(s.left_parents)} "
        f"右父数={len(s.right_parents)}"
    )
    total += s.score
print("整块 count =", total)
```

**需要观察的现象**：`bold` 这个片段（左父为 `<b>`）的分数等于 `len(encode('<b>bold</b>'))`，高于它的纯文本 token 数；首个片段 `hello ` 的骨架还会额外渲染 `data-orig-len="9999"`（`is_first` 分支）。本例中 `<b>` 是独生子 tag，构造时不发临时 id，所以 `id="99"` 占位与 80 分加成都不会出现——分数差全部来自 XML 骨架本身。若把 XML 换成 `<p><b>a</b><b class="x">b</b></p>`（两个同 tag 但指纹互异的 `<b>`），两个片段会被分配临时 id，分数中随之出现每 id 80 分的「id 税」。

**预期结果**：分数 ≥ 纯文本 token 数，且差值随包裹层级单调增加。具体数值以本地运行 tiktoken 结果为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么预算不用「纯文本 token 数」而要发明「分数」？

**答案**：因为请求里 LLM 看到的不只是纯文本。回填阶段发给模型的是带 `<b id="3">` 之类标签的 XML 骨架，标签、属性、id 都消耗 token。若按纯文本计数，一个满是内联标记的段落会严重低估实际开销，导致请求超长被拒或被截断。`_ID_WEIGHT=80` 进一步把「撑起一条 id 骨架」的间接成本显式计提——这也是 4.1 节 id 经济学（能不发 id 就不发）的直接动机。

**练习 2**：`_split_into_serial_groups` 的外层 while 循环在什么条件下放弃合并、直接 yield 当前组？

**答案**：当 `next_sum_count + next_group.tail_remain_count > self._max_group_score` 时（[stream_mapper.py:L96](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L96)），即「已累计的 body 重量加上下一组的 body 与尾部上下文预留一旦超预算」就切组。合并时只并入 body，`tail` 与 `tail_remain_count` 被下一组的覆盖（L101-L103），保证上下文总是来自紧随其后的内容。

**练习 3**：把 `max_group_tokens` 从 2600 调到 800，会对翻译结果产生什么影响？

**答案**：组数与 LLM 请求数增多、单请求变小。代价是上下文变窄——跨段指代（「如上一节所述」）更容易译错，且总骨架开销占比上升；收益是单次请求失败（超时、格式错误）的重试成本更小、并发粒度更细。这是一个吞吐/质量/成本的三角权衡，`translate()` 把它暴露成参数正是为了让用户按书按模型调。

### 4.3 序列切分：serial 包与头—身—尾三明治

#### 4.3.1 概念说明

`serial/` 子包把 4.2 的分组思想抽成通用件：**任何**「带 token 数、可截断」的序列都能按预算切组，并且每组自带上下文。它解决两个问题：

1. **协议化**：`Segment` 协议只要求四个成员（`tokens`、`payload`、`truncate_after_head`、`truncate_before_tail`），不关心你是 XML 片段、音频帧还是日志行。
2. **三明治**：直接按组翻译有个隐患——每组开头的句子看不到上一组的结尾，译文容易衔接生硬。三明治设计让每组请求都带**头（head，上文）+ 身（body，本组正文）+ 尾（tail，下文）**，但从结果里**只保留 body 的译文**：head 与 tail 作为纯粹的语境出现，它们自己的翻译权留给以它们为 body 的组。这样每个片段恰好被「负责」一次，既获得上下文又不重复翻译。

#### 4.3.2 核心流程

```text
segments（Segment 协议实例的序列）
   │ split_into_chunks(segments, max_group_tokens)
   │    每个片段 → Resource(count=tokens, 切缝=0, payload=片段)
   │    resource_segmentation.split(max_segment_count, gap_rate=0.07,
   │                                tail_rate=0.5, border_incision=0)
   ▼
Chunk(head, body, tail, head_remain_tokens, tail_remain_tokens) 流
   │ splitter.split(segments, transform, max_group_tokens)
   │    head/tail 按 remain 预算截断（可截半个片段，truncate_* 负责收尾）
   │    batch = head + body + tail  →  transform(batch)  ← 这里发出一次 LLM 请求
   │    结果切片：transformed[len(head) : len(transformed)-len(tail)]
   ▼
transform 的 body 部分（T 类型），逐组 yield，拼起来 == 原序列
```

三明治不变式（本模块的命根子）：

\[ \text{concat}\bigl(\text{body}_1, \text{body}_2, \dots, \text{body}_n\bigr) \;=\; \text{原序列} \]

即无论 head/tail 如何重叠、截断，输出拼起来与输入一一对应、不重不漏。

#### 4.3.3 源码精读

- [pdf_craft/transformer/xml_translator/serial/segment.py:L8-L17](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/serial/segment.py#L8-L17)：`Segment` 协议。`tokens` 是进入预算的重量；`truncate_after_head(remain_tokens)` / `truncate_before_tail(remain_tokens)` 分别「保留前 N token」「保留后 N token」——协议把截断权下放给实现者，因为只有实现者知道怎样截才不破坏自身结构（XML 片段要补闭合标签，音频帧要对齐采样边界）。`ST` TypeVar（`bound="Segment"`）让切分器对任意实现保持类型安全。
- [pdf_craft/transformer/xml_translator/serial/chunk.py:L9-L18](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/serial/chunk.py#L9-L18)：`_INCISION = 0`——这个通用实现里所有切缝同档（不区分结构边界），`Chunk` 数据类携带三段列表与两个剩余预算。
- [pdf_craft/transformer/xml_translator/serial/chunk.py:L21-L43](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/serial/chunk.py#L21-L43)：`split_into_chunks` 调用上游 `split`，参数 `gap_rate=0.07`（上下文占预算的比例基准）与 `tail_rate=0.5`；上游返回的 `Group.head/body/tail` 里既有 `Resource` 也有 `Segment`（多个资源被算法捆成不可分的段），`_expand_payloads`（[L46-L52](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/serial/chunk.py#L46-L52)）负责摊平回 payload 列表。四个 `*_remain_count` 字段被转译成 `*_remain_tokens`——这是 head/tail 截断的预算。
- [pdf_craft/transformer/xml_translator/serial/splitter.py:L7-L32](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/serial/splitter.py#L7-L32)：`split` 是三明治的执行器。L13-L26 分别对 head（`remain_left=False`，从右往左保留到预算耗尽）与 tail（`remain_left=True`，从左往右保留）做截断；L27 把 `head + body + tail` 整包交给 `transform`（生产环境里这一次调用就是一次 LLM 请求）；L29-L32 从返回值里**切掉头尾**——`transformed[len(head) : -len(tail)]` 只保留 body 对应的结果。L29 的注释还记录了一个 Python 切片陷阱：`[-0:]` 等于 `[0:]`（整个列表），所以 tail 非空才用负索引。
- [pdf_craft/transformer/xml_translator/serial/splitter.py:L35-L50](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/serial/splitter.py#L35-L50)：`_truncate_extra_content` 的截断循环。按 `remain_left` 决定遍历方向，预算够就整段保留、不够就调用片段自己的 `truncate_after_head` / `truncate_before_tail` 削成残段并把预算清零。**注意残段只作为上下文进入请求，永远不会出现在输出里**——输出只取 body。
- 生产路径的对照（同一思想的两份实现）：[stream_mapper.py:L108-L126](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L108-L126) 的 `_truncate_and_transform_group` 与 [L194-L253](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L194-L253) 的 `_truncate_group_gap` 手工完成了同样的三明治（截断粒度更深：先按 Resource、再按 ScoreSegment、最后按 token 三层削），并在截断后**重新运行 `search_inline_segments`**（[L236-L239](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L236-L239)）把残缺的打分片段重新打包成结构完好的 `InlineSegment`——这就是「切分边界总落在标签之外」在生产代码里的最终保障。
- 消费侧闭环：[xml_translator/translator.py:L97-L105](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L97-L105) 把 `map_stream` 的 `map` 回调接到 `_translate_inline_segments`，每组调用一次（纯文本翻译 + 回填修复，L115-L145）；[stream_mapper.py:L39-L44](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L39-L44) 的 `execute` 闭包里，`map(head + body + tail)` 之后取 `[len(head) : len(head)+len(body)]` 正是 splitter L30 的同款切片。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：用 `serial.split` 切一段含 `<b>`、`<i>` 与 MathML 公式的 XML，验证两件事——(a) 任何输出片段都是结构完整的标签；(b) 调大/调小 `max_group_tokens` 会改变组数与每组规模。

**操作步骤**（示例代码，保存为 `serial_demo.py`）：

```python
# 示例代码
import tiktoken
from dataclasses import dataclass
from xml.etree.ElementTree import fromstring, tostring

from pdf_craft.transformer.xml_translator.segment import (
    search_inline_segments, search_text_segments,
)
from pdf_craft.transformer.xml_translator.serial import split  # Segment 协议切分器

encoding = tiktoken.get_encoding("cl100k_base")


@dataclass
class InlineChunk:
    """实现 Segment 协议：一个块级元素 = 一个不可分片段。"""
    xml: str
    tokens: int

    @property
    def payload(self) -> "InlineChunk":
        return self

    def truncate_after_head(self, remain_tokens: int) -> "InlineChunk":
        return InlineChunk(self.xml + " …", min(remain_tokens, self.tokens))

    def truncate_before_tail(self, remain_tokens: int) -> "InlineChunk":
        return InlineChunk("… " + self.xml, min(remain_tokens, self.tokens))


XML = (
    "<chapter>"
    "<p>Tokenization splits text into units. <b>Each unit</b> has a cost.</p>"
    "<p>Inline tags such as <i>emphasis</i> must stay with their text.</p>"
    "<p>Formulas like <math><mi>E</mi><mo>=</mo><mi>m</mi>"
    "<msup><mi>c</mi><mn>2</mn></msup></math> are inline too.</p>"
    "<p>A long paragraph stands alone and carries enough tokens to be "
    "counted as one whole resource in the segmentation algorithm.</p>"
    "<p>Short one.</p>"
    "</chapter>"
)


def build_chunks(xml_text: str) -> list[InlineChunk]:
    root = fromstring(xml_text)
    chunks = []
    for seg in search_inline_segments(search_text_segments(root)):
        xml = tostring(seg.create_element(), encoding="unicode")
        chunks.append(InlineChunk(xml=xml, tokens=len(encoding.encode(xml))))
    return chunks


def run(max_group_tokens: int) -> None:
    chunks = build_chunks(XML)
    calls: list[list[str]] = []

    def transform(batch: list[InlineChunk]) -> list[InlineChunk]:
        calls.append([c.xml for c in batch])   # 记录每次请求看到的完整三明治
        return list(batch)                      # 恒等变换：原样返回，便于校验

    results = list(split(chunks, transform, max_group_tokens))

    print(f"\n== max_group_tokens={max_group_tokens} ==")
    print(f"片段总数={len(chunks)}  请求次数={len(calls)}  输出个数={len(results)}")
    for i, batch in enumerate(calls, 1):
        print(f"  请求{i}: 三明治={len(batch)} 段")

    # 断言一：不重不漏（三明治不变式）
    assert [c.xml for c in results] == [c.xml for c in chunks], "输出必须与输入一一对应"
    # 断言二：标签永不半开（每个片段都能独立解析）
    for c in results:
        fromstring(c.xml)
    print("  断言通过：输出一一对应，且每个片段都是完整 XML")


if __name__ == "__main__":
    run(60)    # 小预算：组多，每组少
    run(200)   # 大预算：组少，每组多
```

**需要观察的现象**：

1. `build_chunks` 应产出 5 个片段（5 个 `<p>` 各一个 `InlineSegment`，`<b>`、`<i>`、`<math>` 都被包在各自段落内部，不会独立成段）。
2. 两次运行的「请求次数」不同：`max_group_tokens=60` 时组更多、每组更小；`200` 时组更少。每次请求的「三明治段数」可能**大于**该组 body 数——多出来的就是 head/tail 上下文。
3. 两条断言在两种预算下都应通过：输出与输入逐项相同（恒等变换下即不重不漏），且每个片段 `fromstring` 都能解析（标签配对完整）。

**预期结果**：上述现象成立。若把 `XML` 换成单个超长段落（token 数远超 `max_group_tokens`），观察上游算法如何处理「单体超过预算」的资源——这取决于 `resource_segmentation` 的实现，具体行为待本地验证。

**运行方式**：在仓库根目录执行 `python serial_demo.py`（需要已安装 pdf-craft 及其依赖 `tiktoken`、`resource-segmentation`，参考 u1-l2 的安装讲义）。以上代码未在本环境实际运行，输出数值请以本地为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`transform` 收到的是 `head + body + tail`，为什么输出要切掉 `len(head)` 个头部和 `len(tail)` 个尾部？如果切多了会怎样？

**答案**：head 与 tail 是**借来的上下文**：它们的翻译权属于相邻的组（在那里它们是 body）。切掉是为了维持不重不漏不变式——每个片段恰好被翻译一次。切多了（比如把 body 的前几个也当成 head 丢掉）会漏译；切少了会重复翻译并在回填时产生两份互相冲突的译文。`splitter.py:L29-L32` 的 `transformed[len(head) : -len(tail)]` 就是精确的切除，且专门处理了 `len(tail)==0` 时负索引失效的切片陷阱。

**练习 2**：`Segment` 协议为什么把 `truncate_after_head` / `truncate_before_tail` 设计成实现者的义务，而不是切分器自己按 token 硬切？

**答案**：因为「怎样截断才安全」是类型相关的知识。XML 片段截断后必须仍是配对完整的标签（生产实现里截断后还要重跑 `search_inline_segments` 重新打包，见 [stream_mapper.py:L236-L239](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L236-L239)）；`truncate_score_segment` 则知道「固定分数减不掉、文字删光不如整段放弃」。切分器只管预算账目，截断的安全性下放给最了解结构的实现者——这是协议设计的分权。

**练习 3**：head 的截断方向是 `remain_left=False`（从右往左保留），tail 是 `remain_left=True`（从左往右保留）。为什么方向必须相反？

**答案**：上下文的价值在于**贴近 body**。head 是 body 之前的内容，最有用的是它**最靠后**的部分（紧挨着本组开头），所以预算不够时从右往左保留、先保住紧邻 body 的那几段；tail 是 body 之后的内容，最有用的是它**最靠前**的部分，所以从左往右保留。两个方向都指向 body，让有限的上下文预算花在刀刃上。

## 5. 综合实践

把本讲三个模块串成一个「迷你分组报表器」。任务：给定一个模拟章节（若干 `<p>`，混入 `<b>`、`<i>`、MathML），产出一份分组报告，回答「这本书会被切成几组、每组多重、上下文带了多少」。

```python
# 示例代码 group_report.py
import tiktoken
from xml.etree.ElementTree import fromstring

from pdf_craft.transformer.xml_translator.segment import (
    search_inline_segments, search_text_segments,
)
from pdf_craft.transformer.xml_translator.xml_translator.score import (
    expand_to_score_segments,
)
from pdf_craft.transformer.xml_translator.serial import split

encoding = tiktoken.get_encoding("cl100k_base")

CHAPTER = """
<chapter>
  <p>The scheduler picks cut points inside a budget.</p>
  <p>Inline runs such as <b>bold</b> and <i>italic</i> never split.</p>
  <p>Math stays inline: <math><mi>a</mi><mo>+</mo><mi>b</mi></math>.</p>
  <p>Groups may span two consecutive elements when the budget allows it,
     because merging only counts weights and incisions.</p>
  <p>Head and tail give every request its neighbourhood.</p>
  <p>Tiny tail.</p>
</chapter>
"""

root = fromstring(CHAPTER)
inline_segments = list(search_inline_segments(search_text_segments(root)))

# 1) 打分：每个块级元素的 count = Σ ScoreSegment.score
weights = []
for seg in inline_segments:
    weights.append(sum(s.score for s in expand_to_score_segments(encoding, seg)))

# 2) 组装 Segment 协议实现，交给 serial 切组
class Weighted:
    def __init__(self, xml: str, tokens: int):
        self.xml, self.tokens = xml, tokens
    @property
    def payload(self):
        return self
    def truncate_after_head(self, n):
        return Weighted(self.xml + " …", min(n, self.tokens))
    def truncate_before_tail(self, n):
        return Weighted("… " + self.xml, min(n, self.tokens))

chunks = [
    Weighted(
        xml="".join(t.text for t in seg),  # 翻译阶段实际发送的是纯文本（对照 4.1.3 的 __iter__）
        tokens=w,
    )
    for seg, w in zip(inline_segments, weights)
]

for budget in (120, 400):
    calls = []
    def transform(batch, _calls=calls):
        _calls.append(len(batch))
        return list(batch)
    results = list(split(chunks, transform, budget))
    body_sizes = [n for n in calls]  # 每次请求的三明治规模
    print(
        f"budget={budget:4} → 请求 {len(calls)} 次, "
        f"三明治规模 {body_sizes}, "
        f"正文输出 {len(results)} 段, "
        f"总重量 {sum(c.tokens for c in chunks)}"
    )
```

要求：

1. 解释报告里「三明治规模」与「正文输出段数」的差值来自哪里（head/tail 上下文）。
2. 把某个 `<p>` 换成嵌套很深的内联结构（如三层 `<b><i><span>`），观察它的 `tokens`（分数）相对纯文本 token 的膨胀，呼应 4.2 的 id 税与骨架税。
3. 写成 pytest 测试（参考 `tests/` 下如 `tests/test_mergeable.py` 的纯函数测试组织方式），至少断言：输出个数 == 输入个数；每个 budget 下 `sum(len(batch)) >= len(results)`（三明治只会多不会少）。

以上脚本未在本环境运行，数值输出待本地验证。

## 6. 本讲小结

- **三级片段各司其职**：`TextSegment` 是「一个非空文本节点 + 完整祖先链」的摊平结果；`InlineSegment` 是以块级元素为界的最小翻译单元（内联标签作为嵌套子片段，绝不外露半开标签）；`BlockSegment` 是进入回填阶段时整组片段的 id 化快照，支撑校验与按 id 回配。
- **内联白名单是一切的基石**：`is_inline_element`（HTML 内联 + MathML + `display` 属性）决定了块级边界在哪里，从而决定了哪里可以下刀。
- **预算单位是分数不是 token**：`score = XML 渲染 token 数 + 80×带 id 左父数`。id 是稀缺资源——`InlineSegment.__init__` 仅在同 tag 子片段指纹互异时才发 id，正是在给分数省钱。
- **切缝引导切点**：结构边界被标注成切缝等级（`incision_between` 的 `block_diff*3 + inline_diff`；生产路径简化为页界 0 / 块界 1 两档），通用算法在预算内优先在好切的地方下刀。
- **组可以跨元素**：`_split_into_serial_groups` 的外层合并循环只认预算，不认章节文件边界；`max_group_tokens`（默认 2600）是用户手里的吞吐/质量旋钮。
- **三明治保证「带上下文且恰好翻译一次」**：head/tail 进入请求提供邻接语境，输出严格切掉头尾；`serial/` 子包（`Segment` 协议 + `split_into_chunks` + `splitter.split`）是这一思想的通用参考实现，生产路径 `stream_mapper` 直接对接 `resource_segmentation` 并自带等价逻辑，截断后还会重打包 `InlineSegment` 兜底结构完整性。

## 7. 下一步学习建议

下一讲 u7-l4（友好 XML 编解码与流式映射）将顺着本讲的输出继续走：翻译组聚合成请求时，XML 如何被 `encode_friendly` 转成 LLM 友好的文本、译文又如何被 `decode_friendly` 与 `XMLStreamMapper` 映射回原元素。建议先自行阅读 `pdf_craft/transformer/xml_translator/xml/friendly/encoder.py` 的入口函数，并带着一个问题读：**「友好编码保留了哪些结构信息，才能让本讲的 id 体系在译文中存活？」** 若想巩固本讲，可回头对照 `stream_mapper.py` 的 `_truncate_group_gap` 与 `serial/splitter.py` 的 `_truncate_extra_content`，画一张两套三明治实现的对照表。
