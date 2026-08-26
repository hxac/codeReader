# u7-l4 友好 XML 编解码与流式映射

## 1. 本讲目标

上一讲（u7-l3）我们知道了 pdf-craft 如何把章节 XML 切成一个个「翻译组」，并且知道了「头—身—尾三明治」的设计思想。本讲回答三个紧接着的问题：

1. **友好编码**：发给回填 LLM 的「XML 模板」是怎么生成的？LLM 返回的「差不多正确」的 XML 又是怎么被宽容地解析回来的？为什么这套编解码敢声称「LLM 输出可解析」？
2. **流式映射**：`XMLStreamMapper` 如何把并发的翻译结果按原文档顺序回填到正确的元素上？
3. **并发控制**：`run_concurrency` 的线程池窗口如何在不打乱顺序的前提下提升翻译吞吐？

学完本讲，你应该能独立追踪「一个翻译组从切分、请求、回填到按序 yield」的完整数据流，并能写出不依赖 LLM 的流式映射演示脚本。

## 2. 前置知识

本讲默认你已读过 u7-l2（XMLTranslator 编排）与 u7-l3（片段与序列切分）。在此之上，补充两个关键前置概念：

### 2.1 ElementTree 的 Element 模型

pdf-craft 全程使用标准库 `xml.etree.ElementTree.Element` 表示 XML 树。它有一个初学者最容易踩坑的设计——**文本被拆成 `text` 与 `tail` 两处**：

- `element.text`：该元素**开始标签之后、第一个子元素之前**的文本；
- `child.tail`：某个子元素**结束标签之后、下一个兄弟（或父元素结束标签）之前**的文本。

对于 `<p>甲<em>乙</em>丙</p>`：`p.text == "甲"`、`em.text == "乙"`、`em.tail == "丙"`。本讲的编码器、解码器、流式映射器都在这两个字段上做文章。

### 2.2 与前两讲的衔接

- u7-l2 讲过：翻译分**两次 LLM 调用**——`translation_llm` 只收纯文本（多组之间用 `\n\n` 连接），`fill_llm` 负责把译文塞回 XML 结构。**友好 XML 只出现在 fill（回填）这一次调用里**：请求里带「XML 模板」，响应里期望返回填好的 XML。
- u7-l3 讲过：翻译组以「分数」为预算（XML 渲染 token 数 + 每个带 id 父片段 80 分），头/尾只作上下文、译文只取身。本讲的 `XMLStreamMapper` 就是这套思想在**生产路径**上的落地。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pdf_craft/transformer/xml_translator/xml/friendly/tag.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/tag.py) | `Tag` 值对象与 `TagKind` 枚举：标签的词法表示与名字/属性值合法性校验 |
| [pdf_craft/transformer/xml_translator/xml/friendly/parser.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/parser.py) | `parse_tags`：手写的字符级状态机扫描器，把字符流切成「文本单元 \| Tag」流 |
| [pdf_craft/transformer/xml_translator/xml/friendly/transform.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/transform.py) | `element_to_tag` / `tag_to_element`：Element 与 Tag 之间的互转（编码侧严格校验） |
| [pdf_craft/transformer/xml_translator/xml/friendly/encoder.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/encoder.py) | `encode_friendly`：Element 树 → 缩进良好的 LLM 友好文本 |
| [pdf_craft/transformer/xml_translator/xml/friendly/decoder.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/decoder.py) | `decode_friendly`：宽容地把字符流还原为 Element 子树流 |
| [pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py) | `XMLStreamMapper`：切组、调用翻译、按文档顺序流式回配 |
| [pdf_craft/transformer/xml_translator/xml_translator/concurrency.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/concurrency.py) | `run_concurrency`：保序的线程池滑动窗口 |
| [pdf_craft/transformer/xml_translator/xml_translator/translator.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py) | 消费方：`XMLTranslator` 如何调用以上组件（u7-l2 已精读，本讲只看接线处） |
| [pdf_craft/transformer/xml_translator/xml_translator/score.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/score.py) | `ScoreSegment`：分数计算与头/尾裁剪（u7-l3 已讲，本讲引用） |

## 4. 核心概念与源码讲解

### 4.1 友好编码：encode_friendly 与 decode_friendly

#### 4.1.1 概念说明

「友好」指的是**对 LLM 友好**。源码里两处注释都指向同一个 issue：

- [encoder.py:11-12](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/encoder.py#L11-L12)：`# why implement XML encoding?` → issue #149
- [decoder.py:10-11](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/decoder.py#L10-L11)：`# why implement XML decoding?` → issue #149

为什么不直接用标准库的 XML 解析器？因为**标准库是严格的，LLM 是马虎的**。LLM 的输出里可能混进未转义的 `&`、忘了闭合的标签、多余的示例块。`ET.fromstring` 遇到这些问题会直接抛 `ParseError`，一次回填就全盘失败。friendly 子包的策略是一套**非对称设计**：

- **编码（我方 → LLM）严格**：出自我们之手的模板必须干净规整，元素名/属性名不合法就直接抛 `ValueError`，绝不把脏结构发出去；
- **解码（LLM → 我方）宽容**：解析器**永远不抛异常**，任何看不懂的标签构造都**降级为普通文本**，能救回多少结构就救回多少。

这就是「保证 LLM 输出可解析」的真正含义：不是保证 LLM 输出正确，而是保证**无论 LLM 输出什么，解码器都能给出一个结果**，再由上层（u7-l5 的校验与爬山修复）判断质量。

#### 4.1.2 核心流程

整条链路可以概括为：

```text
Element 树
  │  element_to_tag（严格校验名字/属性）
  ▼
encode_friendly ──► 缩进良好的 XML 文本（发给 fill_llm 的「XML 模板」）
                          │  LLM 改写文本、（理想情况下）保留标签结构
                          ▼
                    字符流（LLM 响应）
  │  parse_tags（状态机：合法→Tag，非法→按普通文本冲刷）
  ▼
「文本单元 | Tag」交替流
  │  _collect_elements（开标签栈 + 宽容闭合）
  ▼
Element 子树生成器 ──► tags 过滤（如只取顶层 <xml>）──► clone 后逐个 yield
```

解码侧的宽容体现在三个具体规则上：

1. **未闭合的标签**：遇到闭合标签时，从栈顶向下搜索同名开标签，把中间所有未闭合的元素**一并隐式闭合**；
2. **多余的闭合标签**：找不到匹配的开标签时，把它的原始字面量（`proto`）追加到上一个已闭合元素的 `tail` 里；
3. **非法标签名**（如 `<1bad>`）：不当作标签，整体作为普通文本保留。

#### 4.1.3 源码精读

**（1）Tag：标签的词法对象**

[Tag](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/tag.py#L13-L18) 是个 dataclass，四字段：`kind`（[OPENING / CLOSING / SELF_CLOSING](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/tag.py#L7-L10)）、`name`、`proto`（**原始字面量**，降级为文本时用它还原原貌）、`attributes`（名值对列表）。[`__str__`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/tag.py#L20-L40) 把 Tag 渲染回规范形式：自闭合 `<img src="a.png"/>`、闭合 `</p>` 不带属性。

名字合法性由 [`find_invalid_name`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/tag.py#L42-L59) 检查：名字必须非空、以字母或下划线开头。字符白名单定义在 [tag.py:76-96](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/tag.py#L76-L96)——名字字符集是字母/数字/`-`/`_`/`:`/`.`，属性值额外放行 URL 常见符号（`/ ? & = : % ;` 等）。这套**比 XML 规范更严格**的子集，正如 [transform.py:25-26](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/transform.py#L25-L26) 的注释所说，是「为了让 LLM 更容易理解」而刻意收窄的。

**（2）parse_tags：字符级状态机**

[`parse_tags(chars)`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/parser.py#L28-L29) 接收任意可迭代字符流（字符串、生成器都行），产出 `str | Tag` 交替流。核心是九个相位的状态机 [`_parse_char`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/parser.py#L49-L144)：

| 相位 | 含义 |
| --- | --- |
| OUTSIDE | 标签外，攒普通文本 |
| LEFT_BRACKET / LEFT_SLASH | 读到 `<` / `</`，判断是开还是闭 |
| TAG_NAME / TAG_GAP | 拼标签名 / 名后空白 |
| ATTRIBUTE_NAME / _EQUAL / ATTRIBUTE_VALUE | 属性三步：名、`=`、双引号值 |
| MUST_CLOSING_SIGN | 自闭合 `/>` 后只允许 `>` |

关键在 [`_generate_by_result`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/parser.py#L146-L166) 的两条宽容分支：

- `Failed`（如 `<a=b>`）：把 tag 缓冲区里的字符**全部冲刷进普通文本**，回到 OUTSIDE——不报错；
- `Success` 但 [`_is_tag_valid`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/parser.py#L168-L172) 不通过（名字非法、闭合标签带属性）：把 `proto` 原字面量写进普通文本——**降级而非丢弃**。

**（3）编码侧的严格互转**

[`element_to_tag`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/transform.py#L13-L36) 把 Element 转成 Tag 时做三件事：属性**按名排序**（保证输出稳定、利于缓存）、忽略闭合标签的属性、对名字与属性值做校验——不合法直接 `ValueError`。注意方向差异：[`tag_to_element`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/transform.py#L6-L10)（解码用）则完全不校验，宽容到底。

**（4）encode_friendly：生成 LLM 模板**

入口 [`encode_friendly(element, indent=2)`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/encoder.py#L13-L22) 只是递归函数 [`_encode_element`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/encoder.py#L37-L79) 的薄封装。三个值得注意的细节：

- **短文本单行化**：文本 ≤ [`_TINY_TEXT_LEN = 35`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/encoder.py#L34) 个字符、无子元素、无换行时，写成一行 `<b>bold</b>`（[encoder.py:53](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/encoder.py#L53)）——省 token 也省行数；
- **空元素自闭合**：无子无文本的元素渲染成 `<img src="a.png"/>`（[encoder.py:45-47](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/encoder.py#L45-L47)）；
- **文本节点的自我防混**：[`_escape_text`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/encoder.py#L25-L31) 对**文本内容**先过一遍 `parse_tags`，若文本里本身含有「长得像标签」的内容（OCR 常见），就 HTML 转义成 `&lt;...&gt;`，确保解码器不会把它误认成结构——这是编解码能往返一致的关键一步。

tail 文本在[子元素循环](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/encoder.py#L60-L73)里处理：每个子元素编码完后，紧随其后的 tail 写在同样的缩进层上。

**（5）decode_friendly：宽容重建**

入口 [`decode_friendly(chars, tags=())`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/decoder.py#L12-L20) 是生成器：`chars` 是**字符的可迭代对象**（字符串即可，也可是流式生成器——全书内容无需驻留内存），`tags` 过滤只保留指定顶层标签的子树（传 `"xml"` 就只收 `<xml>...</xml>`），命中的元素经 `clone_element` 克隆后 yield，避免调用方之间共享可变树。

重建逻辑在 [`_collect_elements`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/decoder.py#L23-L52)：

- 维护 `opening_stack`（未闭合元素栈）与 `last_closed_element`（最近闭合的元素）两个状态；
- 开标签：压栈；自闭合标签：**立即产出**并记为 last_closed（[decoder.py:41-43](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/decoder.py#L41-L43)）；
- 闭标签：[`_pop_element`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/decoder.py#L62-L75) 从栈顶**向下搜索**同名标签，把之上所有元素一并弹出（隐式闭合未闭合的内层），产出最外层被弹出的那棵子树；找不到同名者返回 `None`，其 `proto` 追加进 last_closed 的 tail（[decoder.py:36-37](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/decoder.py#L36-L37)）；
- 文本单元：有未闭合栈就写入栈顶 `.text`，否则追加到 last_closed 的 `.tail`（[decoder.py:48-52](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/decoder.py#L48-L52)）。

还有一个容易忽略的语义：**只有完整闭合的子树才会被产出**。流结束时仍未闭合的元素被静默丢弃——这正是消费方 `_extract_xml_element` 能给出「No complete `<xml>...</xml>` block found」错误提示的原因。

**（6）在 XMLTranslator 中的接线**

请求侧：`_request_and_submit` 把 `encode_friendly(hill_climbing.request_element())` 作为「XML template」嵌进 fill 提示词（[translator.py:176-180](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L176-L180)）。`request_element` 是一个 `<xml>` 根的模板树，每个子块带 `data-orig-len` 属性记录原文 token 数（[hill_climbing.py:34-40](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/hill_climbing.py#L34-L40)，属性名见 [common.py:1](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/common.py#L1)）。

响应侧：[`_extract_xml_element`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L229-L246) 用 `decode_friendly(text, tags="xml")` 解析 LLM 响应，并执行两条业务校验：一个 `<xml>` 块都没有 → 返回错误字符串；找到多个 → 同样返回错误字符串（提示「只要一个块，别带示例和解释」）。错误字符串随后成为修复循环的反馈（u7-l5 详讲）。

#### 4.1.4 代码实践：往返实验

**实践目标**：亲手验证「encode → 模拟 LLM 改写 → decode」往返后结构不丢失，并观察宽容解码的三种降级行为。

**操作步骤**（示例代码，不依赖网络）：

```python
# roundtrip.py —— 示例代码
from xml.etree.ElementTree import Element, SubElement
from pdf_craft.transformer.xml_translator.xml import encode_friendly, decode_friendly

# 1. 构造含三种内联标记的元素：em（普通内联）、sup（上标）、img（自闭合带属性）
p = Element("p")
p.text = "Where "
em = SubElement(p, "em"); em.text = "energy"; em.tail = " equals "
sup = SubElement(p, "sup"); sup.text = "2"; sup.tail = " "
img = SubElement(p, "img"); img.set("src", "a.png"); img.tail = " end."

encoded = encode_friendly(p)
print(encoded)

# 2. 模拟 LLM：只改文本、保留标签结构
translated = encoded.replace("Where", "其中").replace("energy", "能量") \
                    .replace("equals", "等于").replace("end.", "结束。")

# 3. 解码并校验结构
elements = list(decode_friendly(translated, tags="p"))
assert len(elements) == 1
q = elements[0]
print([c.tag for c in q])                    # 预期 ['em', 'sup', 'img']
print(q.text.strip())                        # 预期 其中
print(q[0].text, "/", q[0].tail.strip())     # 预期 能量 / 等于
print(q[2].get("src"))                       # 预期 a.png
print(q[2].tail.strip())                     # 预期 结束。

# 4. 宽容性实验：三种「坏输入」
print(list(decode_friendly("<p>hello")))               # 未闭合 → 预期 []（空）
print(list(decode_friendly("<p>a</p>tail</div>")))     # 多余闭标签 → tail 混入 "</div>"
print(list(decode_friendly("<p><1bad>x</1bad></p>")))  # 非法标签名 → 降级为文本
```

**需要观察的现象**：

1. `encoded` 的排版：`<p>` 多行展开、`<em>energy</em>` 等短文本单行、`<img src="a.png"/>` 自闭合；
2. 第 3 步所有断言通过——标签、嵌套、属性、文本/尾文本位置全部保留；
3. 第 4 步三种坏输入**都不抛异常**，而是分别给出「空结果 / 尾部混入字面量 / 标签变文本」。

**预期结果**（`encoded` 的形状，基于对 `_encode_element` 的逐行推演）：

```xml
<p>
  Where
  <em>energy</em>
  equals
  <sup>2</sup>
  <img src="a.png"/>
  end.
</p>
```

注意往返比较要基于 **strip 后的文本 + 结构**，而不是逐字符相等：解码后 `q.text` 会保留缩进换行（如 `"\n  其中\n  "`）。以上断言值均为源码推演结论，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_escape_text` 要先 `parse_tags` 再转义，而不是直接 `escape_html(text)`？

**答案**：因为「文本里长得像标签的内容」才需要藏起来。`parse_tags` 能区分「真标签」与「纯文本片段」，只对文本中疑似 Tag 的部分做 HTML 转义。若无脑整体转义，正常文本中的 `&`、`<` 也会被转成实体，模板体积膨胀且 LLM 阅读体验变差。

**练习 2**：`decode_friendly("<a><b><c></a>")` 会产出什么？

**答案**：产出一个 `<a>` 元素，内含 `<b><c/></b>` 子树。`</a>` 触发 `_pop_element("a", ...)` 时向下搜索到栈底的 `a`，把 `c`、`b`、`a` 全部弹出——未显式闭合的 `b`、`c` 被隐式闭合，产出最外层的 `a`。

**练习 3**：编码器遇到 `Element("1bad")` 会怎样？解码器遇到 `<1bad>` 呢？

**答案**：编码器抛 `ValueError`——`element_to_tag` 调 `find_invalid_name` 检查到名字以数字开头（[transform.py:27-29](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/transform.py#L27-L29)）；解码器不抛错——`_is_tag_valid` 判定非法后把 `<1bad>` 的字面量写进普通文本（[parser.py:156-159](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml/friendly/parser.py#L156-L159)）。严格出口、宽容入口，正是非对称设计的体现。

### 4.2 流式映射：XMLStreamMapper

#### 4.2.1 概念说明

`XMLStreamMapper` 是 u7-l3「三明治切分」思想的**生产实现**。它解决的问题是：翻译任务以 `TranslationTask` 列表进来（每个 task 带一棵 Element 树），翻译结果必须**按原文档顺序、逐棵树地**交还给调用方——中间还要并发调用 LLM、按预算分组、把译文片段对位到原文片段。

u7-l2 讲过 `translate_elements` 是生成器、「全书内容不驻留内存」，兑现这个承诺的正是 stream mapper 的全链路生成器设计。

它对外的契约浓缩在两个类型别名里（[stream_mapper.py:17-21](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L17-L21)）：

- `InlineSegmentMapping = tuple[Element, list[TextSegment]]`：**一块译文**＝回填得到的块元素 + 其中的文本片段序列；
- `InlineSegmentGroupMap = Callable[[list[InlineSegment]], list[InlineSegmentMapping | None]]`：**翻译函数**的类型——收一组内联片段，返回等长的映射结果（失败处为 `None`）。

#### 4.2.2 核心流程

`map_stream` 的三个阶段：

```text
阶段一：切组（惰性生成器 _split_into_serial_groups）
  task 元素流 ──► 每元素展开为 Resource 流（一个 InlineSegment 一个 Resource）
              ──► resource_segmentation.split 按预算初分组
              ──► 贪心合并相邻组（总分 + 下一组身分 + 下一组尾 ≤ 预算 就并入）

阶段二：执行（并发，execute 闭包）
  每组 ──► 截取 head / body / tail（head/tail 克隆保护）
       ──► map(head + body + tail)        # 实际是 XMLTranslator._translate_inline_segments
       ──► 结果切片 [len(head) : len(head)+len(body)]   # 只取身的译文

阶段三：复序（按提交顺序消费并发结果）
  (origin, target) 对 ──► origin.head.root 变化时 yield (元素, 该元素的映射缓冲)
```

关键的顺序语义：**阶段三按「组的提交顺序」消费结果**（由 4.3 的 `run_concurrency` 保证），因此即使第 5 组先翻译完，也要等第 1~4 组交完才轮到它；而每棵 task 树的映射按 `origin.head.root`（即 task 的根元素，见 [text_segment.py:29-31](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/segment/text_segment.py#L29-L31) 的 `root` 属性——`parent_stack[0]`）聚合，切换时整批 yield。

#### 4.2.3 源码精读

**（1）切分与跨元素合并**

[`_expand_to_resources`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L128-L162) 把每个 task 元素展开成 Resource 流：`search_text_segments` 找文本片段 → `callbacks.interrupt_source_text_segments` 钩子（源文拦截，u9-l2 会用到）→ `search_inline_segments` 打包成内联片段 → 每个内联片段经 [`_transform_to_resource`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L164-L181) 变成一个 Resource，`count` 是其全部 ScoreSegment 分数之和（即 u7-l3 的 token 预算分）。

切口（incision）标注结构边界：[相邻两个内联片段](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L145-L148)若属于**同一个 task 根**记 `_BLOCK_INCISION = 1`，跨 task 根记 `_PAGE_INCISION = 0`（[stream_mapper.py:13-14](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L13-L14)）。切分算法优先在低级切口处下刀。

[`_split_into_serial_groups`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L71-L106) 先对每个元素调 `resource_segmentation.split` 初分组，然后进入[贪心合并循环](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L88-L104)：只要「当前累计分 + 下一组身分」加上下一组的尾仍不超 `max_group_score`，就把下一组的 body 并进来。**这就是 u7-l3 说的「翻译组可跨元素合并」的出处**——短章节会拼车，减少请求次数。

**（2）三明治的截取与切片**

[`execute` 闭包](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L39-L44) 是每组的工作函数：

```python
head, body, tail = self._truncate_and_transform_group(group)
head = [segment.clone() for segment in head]   # 克隆：上下文与结果解耦
tail = [segment.clone() for segment in tail]
target_body = map(head + body + tail)[len(head) : len(head) + len(body)]
return zip(body, target_body, strict=False)
```

`[len(head):len(head)+len(body)]` 这一刀把「上下文的翻译结果」切掉，只留身的译文——三明治思想最直白的一行代码。head/tail 的克隆（[stream_mapper.py:41-42](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L41-L42)）防止同一段文本既当 A 组的尾、又当 B 组的头时被翻译函数意外篡改。

[`_truncate_group_gap`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L194-L253) 配合 [`_truncate_items`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L256-L284) 与 score.py 的 [`truncate_score_segment`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/score.py#L38-L76)，把头/尾上下文裁到预算内：从靠近「身」的一端保留，超出的文本按 token 砍掉并补 `...` 省略号；若砍光文本还不够，整段放弃（[score.py:44-49](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/score.py#L44-L49) 的注释解释了原因：XML 标签开销是固定分，砍不动）。

**（3）复序与回配**

[`map_stream`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L29-L69) 的主循环消费 `run_concurrency` 的产出，把 `(origin, target)` 对按 `origin.head.root` 聚合：

- root 与当前不同 → `yield current_element, mapping_buffer`，开启下一棵树的缓冲（[stream_mapper.py:56-59](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L56-L59)）；
- `target` 为空（该片段回填失败或译文无文本）→ **直接丢弃**，原文保持不变——这正是 u7-l2「重试耗尽降级为该片段保持原文」的落点（[stream_mapper.py:61](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L61)）；
- yield 前依次过 `callbacks.interrupt_block_element` 与 `interrupt_translated_text_segments` 两个译文拦截钩子（[stream_mapper.py:63-64](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L63-L64)，钩子定义见 [callbacks.py:15-20](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/callbacks.py#L15-L20)）。

**（4）在 XMLTranslator 中的接线**

[translator.py:97-105](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L97-L105)：`translate_elements` 用 `generate_elements` 惰性注册 `id(element) → task` 映射，把元素流交给 `map_stream`，`map` 参数绑定 `_translate_inline_segments`（[translator.py:115-145](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L115-L145)——内部构造 HillClimbing、发起纯文本翻译与 XML 回填，u7-l5 详讲）。每棵树映射完成后，`submit` 按 `SubmitKind` 把译文落地（[translator.py:106-113](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L106-L113)）。

#### 4.2.4 代码实践：读源码，说清顺序保证

**实践目标**：不看讲义，能向别人解释「并发请求与结果回填的顺序保证」。

**操作步骤**：

1. 通读 [stream_mapper.py:29-69](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L29-L69)，标出三处：`run_concurrency(...)` 调用、`origin.head.root` 的判断、`mapping_buffer` 的清空时机；
2. 回答下面两个问题（答案见后）；
3. 追加一个思考实验：如果把 `run_concurrency` 换成「谁先完成谁先 yield」的版本，`map_stream` 会发生什么？

**需要观察的现象 / 预期结果**：你能写出如下两问的答案。

**问题 A：为什么 `map_stream` 敢假设结果按文档顺序到达？**
**答案**：顺序保证来自两层。第一层在 `run_concurrency`（见 4.3）：结果按**提交顺序** yield，靠「只等最老的那个 future」实现；第二层在 `map_stream` 自身：组的生成顺序就是元素流的顺序（`_split_into_serial_groups` 严格按输入元素顺序消费），因此 `(origin, target)` 对按文档序到达，按 `origin.head.root` 分桶聚合即可还原每棵树的完整映射。

**问题 B：回填时「对位」靠什么？**
**答案**：靠 `zip(body, target_body)` 的**位置对齐**（[stream_mapper.py:44](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L44)）：翻译函数收到的列表顺序是 `head + body + tail`，返回列表等长，切出身的第 i 个结果必然对应身的第 i 个原文片段。翻译函数内部（HillClimbing/BlockSegment）再靠 id 属性与标签结构把译文文本分配进块元素（u7-l5 展开）。

**思考实验答案**：若改为「先完成先 yield」，同棵树的映射可能乱序到达，`origin.head.root` 分桶会把属于后棵树的片段提前并入前棵树的缓冲（因为 root 判断只看当前对），映射会串树。要修复就得全量排序或按 root 重组成字典树，流式与低内存的优势就没了。

#### 4.2.5 小练习与答案

**练习 1**：`execute` 里为什么要对 head/tail 做 `clone()`，body 却不用？

**答案**：同一段内联片段可能既是 A 组的尾、又是 B 组的头（三明治共享上下文）。翻译函数可能修改传入片段的状态（如 `recreate_ids` 会重编 id），克隆 head/tail 保证「上下文副本」互不干扰。body 是翻译的真正对象，翻译函数为其产出全新的 mapping（译文元素 + 新文本片段），原文片段本身仍由切分侧持有，专门用于 `zip` 对位。

**练习 2**：`_split_into_serial_groups` 的合并循环里，判断条件为什么要把 `next_group.tail_remain_count` 也加进总分？

**答案**：tail 是下一组自带的「头侧上下文」预算。合并后两组共用一个请求，若不把这部分计入，合并后的实际请求体（身 + 头尾上下文）可能超预算，导致发给 LLM 的内容过长。宁可少合并，不可超预算。

**练习 3**：某片段回填失败时（mapping 为 `None`），最终输出的章节里这段文字是什么？

**答案**：原文。`map_stream` 在 [stream_mapper.py:61](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L61) 直接跳过 `None` 映射，该片段不进 `mapping_buffer`；`submit` 只处理有映射的块，未覆盖处保留原树文本。失败只影响局部，不中断全书。

### 4.3 并发控制：run_concurrency

#### 4.3.1 概念说明

[`run_concurrency`](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/concurrency.py#L10-L52) 只有 40 多行，却是整个翻译管线的「发动机」。它要同时满足三个看似冲突的目标：

1. **并发**：多个翻译组同时请求 LLM，提高吞吐；
2. **保序**：结果必须按参数顺序产出（上一节已见这是复序的前提）；
3. **有界**：内存中同时挂起的任务不超过 `concurrency` 个，且与上游生成器联动实现流式。

LLM 请求是网络 I/O，Python 线程在等待 I/O 时释放 GIL，所以线程池对这类负载是真实有效的并行。

#### 4.3.2 核心流程

```text
参数（惰性生成器）────────────┐
                              ▼
        ┌── 先提交 concurrency 个 future 填满窗口
        │
        ├── 循环：从队头取 future
        │     ├── future.result()   ← 只等「最老」的那个
        │     ├── yield 结果        ← 因此产出顺序 == 提交顺序
        │     └── 从参数生成器再取一个，补进窗口尾部
        │
        └── 参数耗尽后逐个收尾
```

用公式表达其时间语义：设第 \( k \) 个参数的完成时刻为 \( C_k \)、产出时刻为 \( Y_k \)，则

\[ Y_k = \max_{j \le k} C_j, \quad k = 1, 2, \ldots \]

即产出顺序严格保持提交顺序（\( Y_1 \le Y_2 \le \cdots \)，按到达单调），但某个慢组会**队头阻塞**（head-of-line blocking）后面已完成组的产出。吞吐上，\( n \) 个耗时 \( t_1, \ldots, t_n \) 的任务在窗口为 \( c \) 时，总墙钟时间约为最慢串行链之和，而非全部耗时之和（\( T_{\text{serial}} = \sum_i t_i \) 是 concurrency=1 时的特例）。

#### 4.3.3 源码精读

- **快速路径**（[concurrency.py:17-20](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/concurrency.py#L17-L20)）：`concurrency == 1` 时不建线程池，同步逐个执行——默认场景零线程开销；
- **断言防护**（[concurrency.py:15](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/concurrency.py#L15)）：`concurrency >= 1`，防上界为 0 的配置错误；
- **滑动窗口**（[concurrency.py:25-43](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/concurrency.py#L25-L43)）：`futures` 是双端队列。先用 `concurrency` 个参数填满窗口（[concurrency.py:27-33](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/concurrency.py#L27-L33)），此后每 `popleft` 一个最老的 future、`result()` 阻塞取值、`yield`，再从参数迭代器补一个新 future（[concurrency.py:35-43](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/concurrency.py#L35-L43)）。**保序的机关就在 `popleft` + `result()`**：永远只等最老的 future，后来的 future 就算早完成也只能在队列里排队；
- **流式联动**：`parameters` 本身是惰性生成器（stream mapper 的组就是边消费边生成的），所以「切组 → 提交 → 翻译 → 消费结果」四级流水线真正交错执行；
- **中断处理**（[concurrency.py:45-48](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/concurrency.py#L45-L48)）：捕获 `KeyboardInterrupt` 后 `shutdown(wait=False, cancel_futures=True)` 立刻丢弃未开始的任务再重新抛出；正常路径则在 `finally` 中 `shutdown(wait=True)` 等线程收尾（[concurrency.py:50-52](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/concurrency.py#L50-L52)）——Ctrl+C 不用等满窗口的在途请求。

在 [stream_mapper.py:46-50](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L46-L50) 处，`map_stream` 把切组生成器、`execute` 闭包与 `concurrency` 一起交给 `run_concurrency`——翻译组在工作线程里发起 LLM 请求，主线程按序收结果复序。

#### 4.3.4 代码实践：亲眼看「完成顺序 ≠ 产出顺序」

**实践目标**：用一个不依赖网络的小实验，验证「先完成的后产出、产出顺序等于提交顺序」与并发加速两个事实。

**操作步骤**（示例代码）：

```python
# conc_demo.py —— 示例代码
import time
from pdf_craft.transformer.xml_translator.xml_translator.concurrency import run_concurrency

def work(i: int):
    duration = 3.0 - i        # 参数 0 最慢（3s）、1 次之（2s）、2 最快（1s）
    time.sleep(duration)
    return i, duration

start = time.time()
for order, result in enumerate(run_concurrency(parameters=range(3), execute=work, concurrency=3)):
    i, duration = result
    print(f"产出第 {order} 个 = 参数 {i}（计划耗时 {duration}s，"
          f"产出于 +{time.time()-start:.1f}s）")
```

**需要观察的现象**：

1. 三个任务大约在 +1s、+2s、+3s 依次完成（参数 2 最早）；
2. 但**产出顺序恒为参数 0、1、2**，且三个产出时刻都 ≈ +3s——参数 1、2 虽早已完成，被队头的参数 0 阻塞到 +3s 才交付。

**预期结果**：三行输出按参数 0/1/2 排列，产出时刻都约 +3.0s；总耗时约 3s（串行需 6s）。把 `concurrency` 改成 1 再跑，产出与完成交错进行、总耗时约 6s——这就是快速路径与窗口路径的差别。以上为推演结论，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：把 `concurrency` 设为 10，但全书只有 3 个组，会开 10 个线程吗？

**答案**：会创建 `max_workers=10` 的线程池，但初始填充循环最多提交 3 个 future 就遇到 `StopIteration`（[concurrency.py:27-33](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/concurrency.py#L27-L33)），`ThreadPoolExecutor` 按需创建线程，实际只有 3 个任务在跑。窗口大小是上限，不是预分配。

**练习 2**：`run_concurrency` 抛出异常（比如 `execute` 里 LLM 请求炸了）会怎样？

**答案**：`execute` 的异常在 `future.result()` 处重新抛出（Future 的语义），生成器向上传播；`finally` 中 `executor.shutdown(wait=True)` 会等其余在途 future 结束后再清理。若炸的是 `KeyboardInterrupt` 则走快速通道：不等待、取消未开始的任务。注意上层 `map_stream` 没有捕获普通异常——单个组失败会把整个 `translate_elements` 打断，所以真正的组级容错在 `execute` 内部（HillClimbing 重试耗尽返回 `None` 映射）完成。

**练习 3**：为什么「保序」对 pdf-craft 特别重要，而普通无序并发（如不排序的 gather）不行？

**答案**：因为消费方 `map_stream` 依赖「结果到达顺序 == 文档顺序」来按 `origin.head.root` 分桶聚合（4.2.3 第 3 节）。无序产出会把不同章节的映射混进同一个缓冲区，译文串章。保序 + 有界窗口让全链路可以做成纯生成器流，全书翻译不需要把「全部结果列表」放进内存。

## 5. 综合实践

**任务：不接 LLM，跑通 XMLStreamMapper 的流式复序流水线。**

用 4.1 的往返知识与 4.2/4.3 的流水线理解，写一个端到端演示：构造三个小章节元素，用一个**确定性的假翻译函数**替代 LLM，观察切组、并发与按序回配。

```python
# stream_demo.py —— 示例代码
import tiktoken
from xml.etree.ElementTree import Element, SubElement
from pdf_craft.transformer.xml_translator.xml_translator.callbacks import warp_callbacks
from pdf_craft.transformer.xml_translator.xml_translator.stream_mapper import (
    XMLStreamMapper, InlineSegment,
)

def make_chapter(*texts: str) -> Element:
    root = Element("chapter")
    for t in texts:
        SubElement(root, "p").text = t
    return root

elements = [
    make_chapter("alpha beta gamma", "delta epsilon"),
    make_chapter("zeta eta"),
    make_chapter("theta iota", "kappa lambda mu", "nu xi"),
]

def fake_map(inline_segments: list[InlineSegment]):
    results = []
    for seg in inline_segments:          # 组里的每个内联片段（含头尾上下文）
        texts = []
        for ts in seg:                    # InlineSegment 可迭代出 TextSegment
            clone = ts.clone()
            clone.text = f"〔译〕{ts.text}"
            texts.append(clone)
        results.append((seg.create_element(), texts))  # 用模板元素冒充回填结果
    return results                        # 长度必须等于入参长度（含头尾）

encoding = tiktoken.get_encoding("cl100k_base")
mapper = XMLStreamMapper(encoding=encoding, max_group_score=400)
callbacks = warp_callbacks(None, None, None, None)   # 四个钩子全用默认直通

for element, mappings in mapper.map_stream(iter(elements), callbacks, fake_map, concurrency=2):
    first = mappings[0][1][0].text if mappings else "（无）"
    print(f"yield: chapter 含 {len(mappings)} 个映射块，首个译文: {first}")
```

**验证清单**：

1. 三个 chapter 按**构造顺序**被 yield（即使 concurrency=2 且某个后提交的组先「翻译」完）；
2. 每个 mapping 的文本片段都带 `〔译〕` 前缀，且顺序与原文一致；
3. 调低 `max_group_score`（如 200、100）再跑，观察「含 N 个映射块」的变化——更小的预算切成更多的组，每次 yield 的映射块更碎；
4. 把 `fake_map` 中某个 `results.append(...)` 换成 `results.append(None)`，验证该片段被跳过后章节其余部分照常输出（对应 4.2.3 的「失败降级」）。

**预期结果**：输出三行 yield，顺序为 chapter 1 → 2 → 3；前缀齐全；`max_group_score` 越小组数越多。具体的分块数值取决于 tiktoken 对示例文本的编码分数，**具体数字待本地验证**。跑通后，把 `fake_map` 想象成 `XMLTranslator._translate_inline_segments`（纯文本翻译 + encode_friendly 模板 + decode_friendly 回填 + 爬山修复），你就完整看懂了 pdf-craft 翻译引擎的数据流。

## 6. 本讲小结

- **友好 XML 是一套非对称编解码**：出口严格（`element_to_tag` 校验名字/属性值、属性排序）、入口宽容（`parse_tags` 状态机永不抛错，非法标签降级为普通文本、未闭合标签隐式补闭、多余闭标签并入 tail）；「保证可解析」的含义是任何 LLM 输出都能解出一个结果。
- **短文本单行化（≤35 字符）与自闭合标签**让发给 fill LLM 的模板紧凑省 token；`_escape_text` 防止文本内容中的「假标签」污染结构。
- **XMLStreamMapper 是三明治思想的生产实现**：切组（含跨元素贪心合并）→ 并发执行（head/tail 克隆、结果按 `[len(head):len(head)+len(body)]` 切片）→ 按 `origin.head.root` 分桶复序，全链路生成器、全书不驻内存；`None` 映射直接跳过、原文保留。
- **run_concurrency 用「双端队列 + 只等最老 future」实现保序并发**：产出顺序恒等于提交顺序，代价是队头阻塞；concurrency=1 走零开销快速路径；Ctrl+C 取消在途任务不等满窗口。
- **两次 LLM 调用的分工在本讲合拢**：translation LLM 收纯文本，fill LLM 收 `encode_friendly` 模板、回 `decode_friendly(text, tags="xml")` 解析，且要求恰好一个 `<xml>` 块。

## 7. 下一步学习建议

本讲把「译文怎么回来」讲完了，但回填结果的质量如何判定、坏了怎么修，还没有展开。下一讲 **u7-l5 评分、爬山修复与提交策略** 将精读 `score.py`（译文质量评分的启发式维度）、`hill_climbing.py`（每轮只保留更优解的迭代修复）、`validation.py` 与 `submitter.py`（REPLACE/APPEND 两种落地方式），其中 `HillClimbing.submit` 正是本讲 `execute` 闭包里那个 `map` 函数的核心。建议先重读 [translator.py:115-145](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L115-L145) 的 `_translate_inline_segments`，带着问题进入下一讲：`hill_climbing.gen_mappings()` 返回的映射是怎么打分出来的？
