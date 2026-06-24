# 段落识别：ParagraphFinder

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `ParagraphFinder` 这个 midend 阶段（`Parse Paragraphs`）在整条翻译流水线里的位置与职责：把上一阶段 `LayoutParser` 产出的版面区域 + `new_parser` 产出的散落字符，聚合成一个个带坐标的 `PdfParagraph`。
- 理解核心的 **line-threading（穿线扫描）切行算法**：用一条水平扫描线从上往下扫，靠「碰撞计数直方图」找行间空白，从而把一坨字符切成一行一行。
- 掌握「短行切分」与「中位线宽阈值」的配合，知道 `--split-short-lines` / `--short-line-split-factor` 两个参数如何影响段落边界。
- 认识 `merge_alternating_line_number_paragraphs` 这个后处理如何修复「正文 a + 行号 l + 正文 c」被错切的版面。
- 理解 `paragraph_helper.is_cid_paragraph` 等段落判定辅助函数的作用，以及它们为何能成为流水线的「安全闸」。

## 2. 前置知识

在进入本讲前，你需要先建立以下认知（这些在依赖讲义中已讲过）：

- **IL（中间表示）**：BabelDOC 用一棵带坐标的对象树贯穿全程。一页 `Page` 下挂着 `pdf_character`（散字符）、`pdf_paragraph`（段落）、`pdf_curve`（曲线）、`page_layout`（版面区域）等并列集合。本讲做的事，就是把 `pdf_character` 里散落的字，按照 `page_layout` 的区域归属，**搬进** `pdf_paragraph`。
- **版面区域 `PageLayout`**：由 `LayoutParser`（u5-l2）用 DocLayout-YOLO 模型识别出来，每个区域有 `box`（坐标）、`class_name`（类别，如 `plain text`、`title`、`formula`、`caption`）和 `id`。它是本阶段「字符归属判断」的底图。
- **`PdfParagraph` 与 `PdfParagraphComposition`**：段落是字符的聚合，内部用 `composition` 列表表达富文本。一个 composition 可以是一整行（`pdf_line`）、一个公式（`pdf_formula`）、或单个字符（`pdf_character`）。
- **坐标系**：IL/PDF 用左下原点、y 轴向上的坐标系。这一点在看「从上往下扫描」的代码时尤其重要——代码里的 `para_y_max` 其实是视觉上「最上面」的那一行。

如果你对上面任何一个概念还模糊，建议先回看 u3-l1（IL 数据模型）和 u5-l2（版面分析）。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [babeldoc/format/pdf/document_il/midend/paragraph_finder.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/paragraph_finder.py) | 本讲主角。`ParagraphFinder` 类，负责把字符聚合成段落、切行、短行拆分、行号合并等全部逻辑。 |
| [babeldoc/format/pdf/document_il/utils/paragraph_helper.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/paragraph_helper.py) | 段落判定辅助函数：`is_cid_paragraph`、`is_pure_numeric_paragraph`、`is_placeholder_only_paragraph`。 |
| [babeldoc/format/pdf/document_il/utils/layout_helper.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/layout_helper.py) | 版面相关工具：`build_layout_index`（建 rtree 空间索引）、`get_character_layout`（查字符属于哪个版面）、`is_text_layout`、`is_bullet_point`、`add_space_dummy_chars` 等。 |
| [babeldoc/format/pdf/high_level.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py) | 流水线编排。在 `_do_translate_single` 里调用 `ParagraphFinder(translation_config).process(docs)`，并在 `--debug` 下落盘 `paragraph_finder.json`。 |
| [babeldoc/format/pdf/translation_config.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py) | 中心配置。承载 `split_short_lines`、`short_line_split_factor`、`merge_alternating_line_numbers` 等本讲相关参数。 |

## 4. 核心概念与源码讲解

### 4.1 段落识别整体流程：从散字符到结构化段落

#### 4.1.1 概念说明

经过 frontend（`new_parser`）解析后，一页 PDF 里的每个字都已经是一个带坐标的 `PdfCharacter`，但它们只是**平铺**地躺在 `page.pdf_character` 列表里，彼此之间没有「谁和谁是一句话」「谁和谁是一行」的结构关系。

`ParagraphFinder` 要解决的就是这个「散字符 → 结构化段落」的问题。它的输入是上一阶段 `LayoutParser` 写好的 `page.page_layout`（版面区域），输出是把散字符按区域、按行聚合成 `page.pdf_paragraph`（段落列表）。

在整条流水线里，它是 `TRANSLATE_STAGES` 中权重 `6.26` 的 `Parse Paragraphs` 阶段，紧跟在 `LayoutParser`（版面分析）之后、`StylesAndFormulas`（公式样式）之前：

[babeldoc/format/pdf/high_level.py:60-69](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L60-L69) 定义阶段全景表，其中第 65 行注册了 `Parse Paragraphs`。

[babeldoc/format/pdf/high_level.py:971-977](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L971-L977) 是真实调用点：跑完 `ParagraphFinder` 后，若开了 `--debug`，会把整棵 IL 序列化成 `paragraph_finder.json` 落盘——这是本讲代码实践的核心抓手。

#### 4.1.2 核心流程

`ParagraphFinder` 的对外入口是 `process(document)`，它逐页调用 `process_page(page)`。`process_page` 把一页的处理拆成了一串明确的步骤，可以把它当成「段落识别的六步菜谱」：

```
process_page(page):
  ① 建版面空间索引 (build_layout_index) + 预处理公式版面标签
  ② _group_characters_into_paragraphs   # 散字符 → 段落（按版面区域分组）
  ③ _split_paragraph_into_lines          # 段落内 → 行（line-threading 穿线扫描）
  ④ add_space_dummy_chars + 处理行内空格 + update_paragraph_data
  ⑤ calculate_median_line_width          # 算全页行宽中位数
  ⑥ process_independent_paragraphs       # 短行/目录/项目符号拆分
  ⑦ merge_alternating_line_number_paragraphs  # 行号交替布局合并
  ⑧ fix_overlapping_paragraphs + 渲染顺序收尾
```

数据形态的演变是本讲的主线：

```
page.pdf_character (散字符，平铺)
        │  ② 按版面区域分组
        ▼
page.pdf_paragraph (段落列表，每段是一串单字符 composition)
        │  ③ line-threading 切行
        ▼
page.pdf_paragraph (每段内部变成若干 pdf_line 行)
        │  ⑥⑦ 拆分与合并后处理
        ▼
page.pdf_paragraph (最终段落结构，交给下一阶段 StylesAndFormulas)
```

#### 4.1.3 源码精读

入口 `process` 负责逐页驱动、并在最后做两道安全检查（无段落 / CID 段落过多都直接抛错中止）：

[babeldoc/format/pdf/document_il/midend/paragraph_finder.py:196-215](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/paragraph_finder.py#L196-L215) —— 逐页 `process_page`，若全文档段落总数为 0 抛 `ExtractTextError`，若 CID 段落占比过高抛同样的错。

`process_page` 把上面六步串起来，关键片段：

[babeldoc/format/pdf/document_il/midend/paragraph_finder.py:235-310](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/paragraph_finder.py#L235-L310) —— 注意第 245 行 `page.pdf_paragraph = paragraphs` 把分组结果写回页面；第 290-291 行根据配置决定是否做行号合并；第 296-300 行说明若是 OCR 扫描件（`ocr_workaround`），会画白底并清空图像字符。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「散字符 → 段落」这一步在 `--debug` 产物里的形态。

**操作步骤**：

1. 确认仓库自带的测试 PDF 路径 `examples/ci/test.pdf` 存在。
2. 用 `--debug` 跑一次翻译（需 OpenAI 兼容服务）：
   ```bash
   babeldoc --debug --openai --openai-api-key sk-xxx \
     --input-lang en --output-lang zh --files examples/ci/test.pdf
   ```
3. 在 BabelDOC 的工作目录（通常是当前目录下的临时文件夹，日志里会打印路径）里找到 `paragraph_finder.json`。

**需要观察的现象**：打开 JSON，定位到某一页的 `pdf_paragraph` 字段，你会看到每个段落都有 `box`、`unicode`、`layout_id`、以及 `pdf_paragraph_composition` 列表。对比 `create_il.debug.json`（更早阶段的产物），体会「散字符是如何被打包进段落的」。

**预期结果**：`paragraph_finder.json` 里的段落数量明显少于 `create_il.debug.json` 里的字符数量，且每个段落的 `box` 是其所含字符的并集。

> 若本地无法连通翻译服务，可改用 `babeldoc --skip-translation --debug ...`，本阶段不依赖翻译，仍会产出 `paragraph_finder.json`。

#### 4.1.5 小练习与答案

**练习 1**：`process_page` 第 290 行为什么用 `getattr(self.translation_config, "merge_alternating_line_numbers", True)` 而不是直接 `self.translation_config.merge_alternating_line_numbers`？

**答案**：用 `getattr` 带默认值是一种防御式写法，即便某些旧版配置对象没有这个字段也不会报错，默认开启行号合并。当前 `TranslationConfig` 已有该字段（默认 `True`），所以两者等价，但 `getattr` 更健壮。

---

### 4.2 字符聚合为段落与段落判定辅助函数

#### 4.2.1 概念说明

有了版面区域，怎么决定「这个字符属于哪个段落」？直觉上：**同一个版面文本区域里、阅读顺序上连续的字符，归为同一段**；一旦版面区域变了、或者 xobject（Form XObject，可理解为嵌套子画布）变了、或者遇到了项目符号开头，就另起一段。

但光会聚合还不够。流水线还需要一些**判定函数**来判断「这个段落是不是有问题」——例如全是 `(cid:123)` 这种无法解码的字符（说明字体子集化丢了映射），或者整篇文档大量段落都是 CID 段落（说明解析基本失败）。这些判定函数集中在 `paragraph_helper.py`，是流水线的「安全闸」。

#### 4.2.2 核心流程

`_group_characters_into_paragraphs` 的判定逻辑（伪代码）：

```
对页面里每个字符 char（按阅读顺序）:
    char_layout = 查 rtree 得到 char 所在的版面区域
    若 char_layout 不是文本类，或 char 是孤立公式字符 → 跳过（保留在 pdf_character）
    判断 is_new_paragraph:
        - 当前还没有段落 → 新段
        - 版面区域 id 变了（且不是空格）→ 新段
        - 上一个字符的 xobject 与当前不同 → 新段
        - 当前是项目符号且段落刚起步 → 新段
    若新段：新建 PdfParagraph(layout_id=当前区域 id)
    把 char 作为单个 PdfParagraphComposition(pdf_character=char) 追加进当前段落
```

关键点：**版面区域 id（`layout_id`）是段落归属的主键**。这把 u5-l2 产出的 `page_layout` 直接用作了段落切分的边界依据。

#### 4.2.3 源码精读

聚合主循环：

[babeldoc/format/pdf/document_il/midend/paragraph_finder.py:420-512](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/paragraph_finder.py#L420-L512) —— 第 447-507 行是核心循环。注意第 454 行 `if not is_text_layout(char_layout) or self.is_isolated_formula(char)` 决定字符是否被「收进段落」；第 509 行 `page.pdf_character = skip_chars` 把没收的字符（公式、图、非文本区域里的字）留在原处，留给后续 `StylesAndFormulas` 处理。

判定「新段」的条件集中在这里：

[babeldoc/format/pdf/document_il/midend/paragraph_finder.py:476-493](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/paragraph_finder.py#L476-L493) —— 三个 `or` 分支分别对应：版面区域切换、xobject 切换、项目符号开头。

辅助函数 `is_text_layout` 列出了所有「算作文本」的版面类别（很长一串白名单）：

[babeldoc/format/pdf/document_il/utils/layout_helper.py:801-849](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/layout_helper.py#L801-L849) —— 只有落在这些类别里的字符，才会被收进段落。

接下来看「安全闸」函数。最关键的是 `is_cid_paragraph`：

[babeldoc/format/pdf/document_il/utils/paragraph_helper.py:9-36](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/paragraph_helper.py#L9-L36) —— 它先把段落里所有 composition 的字符摊平成 `chars`（注意 `pdf_same_style_unicode_characters` 分支被 `continue` 跳过，因为那是翻译后才出现的纯 unicode 文本，不含逐字对象），再用正则 `^\(cid:\d+\)$` 数出 CID 字符个数，若占比超过 80% 就判定为「CID 段落」。

> **什么是 CID？** PDF 里字体常用 CID 编码（Character ID）。当字体子集化或编码映射丢失时，解析器无法把 CID 还原成可读 unicode，只能输出形如 `(cid:122)` 的占位符。一个段落里如果绝大多数字都是 `(cid:xxx)`，说明这段基本没解析出来，翻译它毫无意义。

`check_cid_paragraph` 在 `process` 末尾用它做文档级熔断：

[babeldoc/format/pdf/document_il/midend/paragraph_finder.py:217-225](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/paragraph_finder.py#L217-L225) —— 若全文档 CID 段落占比 > 80%，直接抛 `ExtractTextError`，提前终止，避免后续白白消耗 LLM token。

另两个辅助函数用途类似但场景不同：

[babeldoc/format/pdf/document_il/utils/paragraph_helper.py:42-52](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/paragraph_helper.py#L42-L52) —— `is_pure_numeric_paragraph` 用 `^-?\d+(\.\d+)?$` 判断段落是不是纯数字（整数/小数/负数），这在 4.5 节判断「行号段」时会派上用场的姊妹思路。

[babeldoc/format/pdf/document_il/utils/paragraph_helper.py:55-94](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/paragraph_helper.py#L55-L94) —— `is_placeholder_only_paragraph` 判断段落是否「只剩公式占位符和空白」，用于过滤无意义的空段。

#### 4.2.4 代码实践

**实践目标**：手工模拟 `is_cid_paragraph` 的判定，理解 80% 阈值。

**操作步骤**：

1. 在 Python 里构造一个假的 `PdfParagraph`（用 `il_version_1` 里的 dataclass），让它包含 10 个字符，其中 9 个的 `char_unicode` 设成 `"(cid:122)"`，1 个设成 `"a"`。
2. 调用 `from babeldoc.format.pdf.document_il.utils.paragraph_helper import is_cid_paragraph`，传入该段落。

**需要观察的现象**：返回 `True`（9/10 = 90% > 80%）。

**预期结果**：把 CID 字符减到 8 个（8/10 = 80%），由于判定是 `> 0.8` 而非 `>=`，此时应返回 `False`。

> 构造 dataclass 时需要给 `PdfCharacter` 补齐 `box`、`pdf_style` 等必填字段，可参考 `examples/*.xml` 对应的 IL 结构。具体字段以本地 `il_version_1.py` 为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `is_cid_paragraph` 在遇到 `pdf_same_style_unicode_characters` 这种 composition 时直接 `continue`，而不是像其他分支那样把字符收集起来？

**答案**：`pdf_same_style_unicode_characters` 只存一段 `unicode` 字符串、不存逐字 `PdfCharacter` 对象（它是翻译完成后才出现的产物，见 u3-l1）。而 `is_cid_paragraph` 要数的是带 `char_unicode` 的 `PdfCharacter`，所以这种 composition 既没有可数的字符对象，逻辑上也只出现在翻译之后——此时再做 CID 判定已无意义，故跳过。

**练习 2**：`check_cid_paragraph` 的阈值是 `> 0.8`，`is_cid_paragraph` 单段阈值也是 `> 0.8`，这两个 0.8 含义一样吗？

**答案**：不一样。单段的 `0.8` 是「段内 CID 字符占该段总字符的比例」；文档级的 `0.8` 是「CID 段落数占全文档段落总数的比例」。一个是字符级，一个是段落级，恰好都取了 0.8 这个数。

---

### 4.3 line-threading 切行算法

#### 4.3.1 概念说明

上一节把字符按版面区域聚成了段落，但一个段落里可能有好几行字（典型的多行段落）。翻译和排版都需要「行」这个粒度——比如判断首行缩进、计算行宽、按行重排译文。所以还需要在段落内部把字符切成一行一行。

`_split_paragraph_into_lines` 用的是一个叫 **line-threading（穿线）** 的思路：想象拿一条水平线，从段落的最高处（`y_max`）匀速往下扫到最低处（`y_min`），每移动一小步就数一下「现在这条水平线穿过了多少个字符」。显然，**行与行之间的空白处，穿过的字符数是 0**；而某一行所在的高度区间内，穿过的字符数会大于 0。于是「计数为 0 的连续区间」就是行间隙，用这些间隙就能把字符分到不同的行里。

这个思路的妙处在于：它不依赖任何「行高」的先验假设，纯靠字符的几何分布自动发现行边界，对行距不均、字号混排都很鲁棒。

#### 4.3.2 核心流程

```
_split_paragraph_into_lines(paragraph):
  1. 摊出段落内所有 PdfCharacter（公式等非单字 composition 暂存为 other_compositions）
  2. 取每个字符的有效 y 范围 [y1, y2]（来自 visual_bbox）
  3. 若段落总高度 < 5（几乎平的）→ 当作单行
  4. 从 y_max 到 y_min，以 step=0.25 扫描，用「差分数组直方图」算每个扫描位置的碰撞计数
  5. 找出所有「计数 < 1（即 0）」的连续区间 → 行间隙 gaps
  6. 若没有间隙 → 当作单行
  7. 取每个 gap 的起始 y 作为分隔线，从高到低排序
  8. 每个字符按其 y 中心点落入哪两个分隔线之间，归入对应行
  9. 用切好的行重建段落的 composition（每行一个 pdf_line）
```

**碰撞计数直方图的数学**：设段落最高点 \(y_{\max}\)，步长 \(\Delta = 0.25\)，第 \(i\) 个扫描位置的纵坐标为

\[
y_i = y_{\max} - i\cdot \Delta,\quad i = 0,1,\dots,m-1
\]

某字符的纵向范围是 \([y_1, y_2]\)，它「覆盖」扫描位置 \(i\) 当且仅当 \(y_1 \le y_i \le y_2\)。于是位置 \(i\) 的碰撞计数为

\[
c_i = \#\{\,\text{字符}\mid y_1 \le y_i \le y_2\,\}
\]

行间隙就是满足 \(c_i = 0\) 的极大连续区间。

如果朴素地双重循环（对每个扫描位置遍历所有字符），复杂度是 \(O(m\cdot n)\)。源码用**差分数组（difference array）**把它降到 \(O(m+n)\)：对每个字符，在它覆盖的起始索引处 `+1`、结束索引处 `-1`，最后做一次前缀和（`np.cumsum`）就得到所有 \(c_i\)。

#### 4.3.3 源码精读

主方法：

[babeldoc/format/pdf/document_il/midend/paragraph_finder.py:652-702](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/paragraph_finder.py#L652-L702) —— 注意第 695 行的「平段落当单行」短路（`< 5` 阈值）；第 708 行固定步长 `step = 0.25`。

差分数组直方图（本讲最值得精读的一段）：

[babeldoc/format/pdf/document_il/midend/paragraph_finder.py:615-650](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/paragraph_finder.py#L615-L650) —— 第 641-642 行把每个字符的 y 范围换算成离散索引（`starts` 处 `+1`，`ends` 处 `-1`），第 647-648 行用 `np.add.at` 批量累加，第 650 行 `np.cumsum(hist[:-1])` 一次性算出所有扫描位置的碰撞计数。

找间隙：

[babeldoc/format/pdf/document_il/midend/paragraph_finder.py:724-734](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/paragraph_finder.py#L724-L734) —— 注意第 727 行的判定是 `count < 1`（即 `count == 0`）。方法开头的 docstring 写的是「less than 2」，但真实代码用的是 `< 1`，以代码为准。

把字符归入行桶：

[babeldoc/format/pdf/document_il/midend/paragraph_finder.py:757-776](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/paragraph_finder.py#L757-L776) —— 第 758 行算字符的 y 中心点，第 761-764 行拿中心点去比每条分隔线，决定落入第几个行桶；最后第 776 行用切好的行重建 `pdf_paragraph_composition`。

> 一个细节：源码里 `lines[line_idx].append(...)` 之后并没有在行内按 x 排序（相关 `sort` 语句被注释掉了，见第 772 行）。行内字符的左右顺序主要靠 frontend 解析时给出的阅读顺序保证。

#### 4.3.4 代码实践

**实践目标**：用最小 Python 脚本验证「碰撞计数直方图」能正确发现行间隙。

**操作步骤**（源码阅读 + 手工模拟型实践，无需运行翻译）：

1. 准备两组字符的 y 坐标：第一行 3 个字，y 范围都在 `[90, 100]`；第二行 3 个字，y 范围都在 `[70, 80]`。段落总范围 `para_y_min=70, para_y_max=100`。
2. 套用 `_compute_collision_counts_histogram` 的逻辑：步长 0.25，扫描位置从 100 到 70。
3. 手算：在 y=80 到 y=90 这段区间内，没有任何字符覆盖，碰撞计数应为 0——这就是行间隙。

**需要观察的现象**：直方图会呈现「高—零—高」的三段形态，中间那段 0 就是 gap。

**预期结果**：`gaps` 列表恰好包含一个区间，对应两行之间的空白；最终字符被分成 2 个行桶。

> 想直接调源码验证，可 `from babeldoc.format.pdf.document_il.midend.paragraph_finder import ParagraphFinder` 后用 `ParagraphFinder._compute_collision_counts_histogram`（它是静态方法）传入两个 numpy 数组。具体字段构造以本地源码为准。

#### 4.3.5 小练习与答案

**练习 1**：如果把扫描步长 `step` 从 0.25 调大到 2.0，会对切行结果有什么影响？

**答案**：步长越大，扫描位置越稀疏，可能错过很窄的行间隙（小于 2 个单位宽的间隙会被跨过去），导致本该分开的两行被误并成一行。反之步长越小越精细，但计算量也越大。0.25 是在精度与性能之间的折中。

**练习 2**：为什么用字符的 y 中心点（`(y1+y2)/2`）来归入行桶，而不是用 y1 或 y2？

**答案**：单个字符可能因为上下标、字号差异而 y 范围参差。用中心点更稳健——只要中心点落在某条分隔线的正确一侧即可归类，避免因字符顶端或底端越界而被错分到相邻行。

---

### 4.4 短行切分与中位线宽阈值

#### 4.4.1 概念说明

切完行之后，会出现一个常见问题：**一个版面区域里其实有多段独立文字，但因为它们在同一个 layout 区域、xobj 也相同，被错误地合成了一个大段落**。最典型的例子是「项目符号列表」「图注/表注」「文章末尾的不完整行」。

判断「这里该断段」的一个强信号是：**某一行的宽度明显比正常行短**。比如一个正常段落每行都顶到版面右边距，唯独中间某行很短就换行了——这通常意味着「这段话讲完了，下一行是另一段的开头」。

BabelDOC 用「全页行宽的中位数」作为「正常行宽」的基准，再用一个可调系数 `short_line_split_factor`（默认 0.8）当作阈值：**当某行宽度 < 中位数 × 系数时，就从这里把段落切开**。

注意这个能力默认是**关闭**的（`split_short_lines=False`），因为它「may cause poor typesetting & bugs」（可能破坏排版、引入 bug）。需要用户显式加 `--split-short-lines` 才开启。

#### 4.4.2 核心流程

```
process_independent_paragraphs(paragraphs, median_width):
  对每个段落 paragraph（且 composition 多于 1 行）:
    遍历段落内的相邻行对 (prev_line, current_line):
      若 prev_line 文本含 ≥20 个连续点（\.{20,}）→ 目录条目，从这里断段
      elif 开启了 split_short_lines 且 prev_line 宽度 < median_width × short_line_split_factor → 断段
      elif current_line 首字符是项目符号 → 断段
      断段时：把 current_line 及之后的所有行拆成一个新段落，插回列表
```

阈值判定的核心不等式：

\[
w_{\text{prev}} < w_{\text{median}} \times \alpha
\]

其中 \(w_{\text{median}}\) 是全页行宽中位数，\(\alpha\) 是 `short_line_split_factor`（默认 0.8）。当不等式成立，认为 `prev_line` 是「短行」，在此断段。

#### 4.4.3 源码精读

计算中位线宽：

[babeldoc/format/pdf/document_il/midend/paragraph_finder.py:822-839](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/paragraph_finder.py#L822-L839) —— 收集所有 `pdf_line` 的宽度（`box.x2 - box.x`），排序后取中位数。这个中位数是「这一页正常行有多宽」的基准。

短行/目录/项目符号拆分：

[babeldoc/format/pdf/document_il/midend/paragraph_finder.py:841-928](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/paragraph_finder.py#L841-L928) —— 重点看第 866 行的目录条目判定 `\.{20,}`（连续 20 个以上的点，典型如 `标题...............页码`），以及第 892-895 行的短行判定：

[babeldoc/format/pdf/document_il/midend/paragraph_finder.py:892-903](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/paragraph_finder.py#L892-L903) —— `prev_width < median_width * self.translation_config.short_line_split_factor` 就是上面那个不等式的直接翻译。注意它和「项目符号开头」是 `or` 关系——任一满足都断段。

参数定义在配置里：

[babeldoc/format/pdf/translation_config.py:178-179](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L178-L179) —— `split_short_lines: bool = False`、`short_line_split_factor: float = 0.8`。

对应的 CLI 参数：

[babeldoc/main.py:178-188](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L178-L188) —— `--split-short-lines`（开关）与 `--short-line-split-factor`（阈值系数，默认 0.8）。help 文本明确写了「实际阈值 = 当前页所有行长的中位数 × 该系数」。

#### 4.4.4 代码实践

**实践目标**：对比 `--short-line-split-factor` 取不同值时，`paragraph_finder.json` 里段落切分结果的差异。

**操作步骤**：

1. 先跑一次基线（默认 `split_short_lines=False`，短行拆分不生效）：
   ```bash
   babeldoc --debug --skip-translation --files examples/ci/test.pdf \
     --split-short-lines --short-line-split-factor 0.8
   ```
   记下 `paragraph_finder.json` 里某一页的段落数量。
2. 把系数调小到 `0.5`（阈值更严，只有更短的行才会触发断段）再跑一次。
3. 把系数调大到 `0.95`（阈值更松，稍微短一点的行就断段）再跑一次。

**需要观察的现象**：系数越大（越接近 1），被判定为「短行」的行越多，段落被切得越碎，段落数量越多；系数越小，切分越保守，段落数量越少。

**预期结果**：三组实验的段落数应呈现「0.5 ≤ 0.8 ≤ 0.95」的单调递增趋势（具体数值待本地验证，因为取决于 PDF 里短行的实际分布）。

> 提示：若 `test.pdf` 里没有明显的多段短行，差异可能很小。可以挑一篇排版密集的论文 PDF（如 arXiv 论文）做实验，效果更明显。

#### 4.4.5 小练习与答案

**练习 1**：为什么短行拆分默认关闭？

**答案**：因为「短行即断段」只是个启发式，并不总是对的——很多正常段落的最后一行本来就是短的（没写满一整行）。盲目切开会把一个完整段落错误地劈成两半，破坏后续翻译的上下文连续性，也容易在排版重排时出 bug。所以默认关闭，只在用户确认需要时开启。

**练习 2**：目录条目判定用的是 `\.{20,}`（连续 20 个点），为什么不用短行阈值来切目录？

**答案**：目录条目的特征是「标题 + 一长串点 + 页码」，点串本身就是强信号，比行宽更可靠。靠行宽切可能会把目录里不同条目错切或漏切，而 20 个连续点几乎只出现在目录的引导线上，判定更精准。

---

### 4.5 行号交替布局合并后处理

#### 4.5.1 概念说明

学术论文里常见一种版面：**正文旁边带行号**（line numbers），比如法律文书、诗歌、或带行号引用的论文。它的视觉结构是：

```
正文第一行      1
正文第二行      2
正文第三行      3
```

这种版面在同一个 layout 区域里，正文和行号是**交替排列**的。前面 `_group_characters_into_paragraphs` 按 layout 区域分组时，很容易把「正文 a、行号 l、正文 c」切成三个段落（因为行号是数字、和正文风格不同，或者中间有间距）。但实际上「正文 a」和「正文 c」是**同一段连续文字**，行号 l 只是个标注。

`merge_alternating_line_number_paragraphs` 这个后处理就是来修复这个错误的：检测出 `正文 a + 行号 l + 正文 c` 的模式（且 a 和 c 在同一 layout、同一 xobject），就把 a 和 c 合并成一段，行号 l 保留为独立的段落（因为翻译时不需要翻译行号）。

#### 4.5.2 核心流程

```
merge_alternating_line_number_paragraphs(paragraphs):
  i = 0
  while i < len(paragraphs) - 2:
      a = paragraphs[i]                              # 候选正文
      j = i + 1
      # 向后吞掉一个或多个连续的「行号段」（纯 ASCII 数字/空格）
      while paragraphs[j] 是纯数字空格段:
          j += 1
      c = paragraphs[j]                              # 候选正文
      若 a 和 c 同 layout、同 xobj:
          把 c 的 composition 拼到 a 末尾，从列表删除 c
          （不移动 i，继续尝试把更多正文接到 a 上 → 链式合并 a l+ a l+ a ...）
      else:
          i += 1
```

「行号段」的判定标准：段落里**只含 ASCII 数字和空格**（`_is_ascii_digit_or_space_paragraph`）。注意它要求 `ord(c) < 128`，排除掉全角数字或其他 unicode 数字字符，保证只匹配真正的西文行号。

#### 4.5.3 源码精读

合并主循环：

[babeldoc/format/pdf/document_il/midend/paragraph_finder.py:393-418](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/paragraph_finder.py#L393-L418) —— 第 404-408 行吞掉连续行号段；第 412 行用 `_same_layout_and_xobj` 校验 a 和 c 同源；第 413 行把 c 的 composition 接到 a 末尾；第 416 行的注释点明了「不移动 i」是为了实现 `a l+ a l+ a ...` 的链式合并。

行号段判定：

[babeldoc/format/pdf/document_il/midend/paragraph_finder.py:368-380](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/paragraph_finder.py#L368-L380) —— `_is_ascii_digit_or_space_paragraph`：段落文本为空也算（返回 True），但只要遇到一个非数字、非空格的字符就立刻返回 False；最后要求至少见过数字（避免把纯空格段误判成行号）。

辅助文本抽取与同源校验：

[babeldoc/format/pdf/document_il/midend/paragraph_finder.py:357-366](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/paragraph_finder.py#L357-L366) —— `_paragraph_text_ascii` 把段落里 `pdf_line` 和单字 composition 的 `char_unicode` 拼成字符串（注意它不抽公式、不抽 `same_style`，因为这些不会出现在行号里）。

[babeldoc/format/pdf/document_il/midend/paragraph_finder.py:382-391](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/paragraph_finder.py#L382-L391) —— `_same_layout_and_xobj` 要求 a、c 的 `layout_id` 和 `xobj_id` 都非空且相等。这个校验很关键：只有同一版面区域、同一画布里的两段，才可能是被行号打断的同一段文字；跨区域的不能合。

开关在 `process_page` 里：

[babeldoc/format/pdf/document_il/midend/paragraph_finder.py:290-291](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/paragraph_finder.py#L290-L291) —— 默认开启，可用 `--no-merge-alternating-line-numbers` 关闭。

[babeldoc/main.py:332-338](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L332-L338) —— CLI 用 `action="store_false"`、`dest="merge_alternating_line_numbers"`，所以加 `--no-merge-alternating-line-numbers` 会把它置 False。

#### 4.5.4 代码实践

**实践目标**：手工验证链式合并能把 `a l a l a` 合成一段。

**操作步骤**（源码阅读型实践）：

1. 构造一个段落列表（伪对象即可），顺序为：`正文段 a`、`"12"`（行号）、`正文段 b`、`"13"`（行号）、`正文段 c`，其中 a、b、c 的 `layout_id` 和 `xobj_id` 都相同，两个行号段只有数字字符。
2. 在纸上模拟 `merge_alternating_line_number_paragraphs` 的 `while` 循环：i=0 指向 a，吞掉 `"12"`，找到 b，校验同源后把 b 并入 a，删除 b；**i 不动**，再次循环又吞掉 `"13"`，找到 c，并入 a。

**需要观察的现象**：最终列表变成 `a（含 b、c 内容）`、`"12"`、`"13"` 三个段落——正文被合并成一段，行号段保留。

**预期结果**：链式合并后正文段数量从 3 降到 1，行号段数量不变。

> 若想用真实 PDF 验证，找一篇左侧带行号的论文（如某些 arXiv 预印本），用 `--debug` 跑一次，对比开/关 `--no-merge-alternating-line-numbers` 时 `paragraph_finder.json` 里正文段的边界。

#### 4.5.5 小练习与答案

**练习 1**：合并后为什么 `i` 不递增、而是 `continue`？

**答案**：因为合并完一次后，`a` 后面可能还跟着更多 `l+ c` 模式（链式的 `a l a l a`）。不移动 `i`，下一轮继续从同一个 `a` 出发，看能不能再把后面的正文段并进来。如果把 `i` 递增了，就只能合并相邻的一对，会漏掉更长的链。

**练习 2**：`_is_ascii_digit_or_space_paragraph` 为什么要检查 `ord(c) < 128`？

**答案**：`str.isdigit()` 在 Python 里对很多非 ASCII 数字（如全角数字 `１２３`、阿拉伯-印度数字 `١٢٣`）也返回 True。但 PDF 里的行号几乎都是 ASCII 数字，加 `ord(c) < 128` 能避免把含全角数字的正文段误判成行号段，提高判定的精确度。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「段落识别诊断」小任务：

**背景**：你拿到一份排版复杂的双语论文 PDF，怀疑 BabelDOC 的段落切分有问题（比如把一段正文切成了两段，或把列表项合成了一个大段）。你要用 `--debug` 产物定位问题，并尝试调参修复。

**任务**：

1. 用 `--debug --skip-translation` 跑这份 PDF，得到 `paragraph_finder.json` 和更早的 `create_il.debug.json`。
2. 打开 `paragraph_finder.json`，定位到出错的那一页。对照 `page_layout`（版面区域）和 `pdf_paragraph`（段落），判断问题属于哪一类：
   - **该合的没合**：两段本是一段，被切开了 → 可能是短行拆分误触发（检查是否开了 `--split-short-lines`），或行号合并没生效（检查版面是否带行号、`merge_alternating_line_numbers` 是否开）。
   - **该分的没分**：多段文字被合成一段 → 考虑开启 `--split-short-lines` 并调 `--short-line-split-factor`。
3. 针对性调参重跑，对比 `paragraph_finder.json` 的段落边界变化，确认问题缓解。
4. 用 `paragraph_helper.is_cid_paragraph` 检查出问题的段落是不是 CID 段落（若是，则不是切分问题，而是字体解析问题，需回看 frontend u4-l4）。

**交付**：写一份简短诊断报告，说明「问题属于哪一类 → 调了哪个参数 → 段落边界如何变化」。这一套流程也是 BabelDOC 维护者在排查段落相关 issue 时的真实工作方式。

## 6. 本讲小结

- `ParagraphFinder`（`Parse Paragraphs`，权重 6.26）承接 `LayoutParser`，把 `page.pdf_character` 里的散字符按版面区域聚合成 `page.pdf_paragraph`，是「散字符 → 结构化段落」的关键一跳。
- 段落归属的主键是版面区域 `layout_id`：`_group_characters_into_paragraphs` 在区域切换、xobject 切换、或遇到项目符号时另起一段，非文本/公式字符则跳过留给后续阶段。
- **line-threading 切行**用一条水平扫描线从上往下扫，靠差分数组直方图（`_compute_collision_counts_histogram`，\(O(m+n)\)）算出每个高度的碰撞计数，计数为 0 的连续区间即行间隙，从而把段落切成行。
- **短行切分**以全页行宽中位数为基准，当某行宽 < 中位数 × `short_line_split_factor`（默认 0.8）时断段；该能力默认关闭（`split_short_lines=False`），因为它可能误伤正常段落的末行。
- **行号交替合并**（`merge_alternating_line_number_paragraphs`）修复「正文 a + 行号 l + 正文 c」被错切的版面，靠「纯 ASCII 数字空格」识别行号段、靠「同 layout 同 xobj」校验同源，支持链式合并。
- **段落判定辅助函数**（`is_cid_paragraph` 等）是流水线的安全闸：CID 段落占比 > 80% 或全文档无段落时，`process` 直接抛 `ExtractTextError` 提前终止，避免无意义的后续翻译。

## 7. 下一步学习建议

本讲产出的 `page.pdf_paragraph`（带行结构的段落）会直接喂给下一个 midend 阶段。建议接着学：

- **u5-l4 公式与样式处理（StylesAndFormulas）**：本讲跳过的「公式字符」「曲线/表单」会在这一步被识别和归并，段落还会被进一步标注样式（粗体、颜色）。`is_cid_paragraph` 在那里的判定逻辑会再次出现。
- **u6-l2 IL 翻译编排**：看段落如何被批处理、加占位符后送进 LLM。你会理解为什么本讲要费大力气把段落切准——因为段落边界直接影响翻译的上下文和质量。
- 想深入理解本讲用到的版面查询基础设施（rtree 空间索引、IoU 计算、字符到版面的映射），可回看 [babeldoc/format/pdf/document_il/utils/layout_helper.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/layout_helper.py) 中的 `build_layout_index` 与 `get_character_layout`。
