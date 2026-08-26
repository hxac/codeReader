# Jointer：跨页文本块的合并

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `Jointer` 在章节生成流程中的位置：它把多页的 `body_layouts`（或 `footnotes_layouts`）加工成连续的 `ParagraphLayout` / `AssetLayout` 流。
2. 解释跨页段落合并的判定条件：`check_mergeable` 的四个条件以及默认合并策略。
3. 解释表格标题、表格注释、脚注编号三类正则如何与几何位置验证配合，把「游离的文字块」归位到表格的 `title` / `caption`。
4. 理解阅读序号切分 `split_reading_serials` 如何处理多栏布局与被图片挤压的文字块。
5. 理解 LaTeX 公式如何先被占位符保护、再经 Markdown 解析、最终还原为 `InlineExpression`。

## 2. 前置知识

本讲建立在 u5-l1 的数据模型之上，先快速回顾：

- **PageLayout**：OCR 层的一等产物，五个字段 `ref`（布局类型）、`det`（检测框 `(x1, y1, x2, y2)`，像素坐标，y 轴向下增大）、`text`（原始文本）、`order`（页内序号）、`hash`（资源哈希）。见 [pdf_craft/pdf/types.py:23-29](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pdf/types.py#L23-L29)。`ref` 的取值来自 u3-l4 讲过的 `_LAYOUT_KIND_TO_REF` 映射，例如 `text`、`sub_title`、`image`、`table_caption` 等。
- **ParagraphLayout / BlockLayout**：段落与块。一个段落可以有多个块——每个块记住自己来自哪一页（`page_index`）、页内序号（`order`）和检测框（`det`），这是跨页合并后仍能溯源的关键。见 [pdf_craft/extractor/chapter/chapter.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/chapter.py)（u5-l1 已精读）。
- **Content**：块的内容是 `list[str | BlockMember | HTMLTag[BlockMember]]`，普通字符串、脚注引用、白名单 HTML 标签混居。
- **阅读顺序（reading order）**：人类读多栏文档时「先读完左栏再读右栏」。OCR 给出的布局顺序未必符合这个顺序，所以合并段落前必须先按栏切分。
- **变异系数（CV）**：标准差与均值之比 \(\frac{\sigma}{\mu}\)，用于度量一组宽度的离散程度，本讲 reading_serials 模块用它做二次切分。

一个直觉性的问题引出全讲：OCR 是逐页、逐块识别的，它不知道「第 3 页末尾那行没写完的句子」和「第 4 页开头那行」其实是同一个自然段，也不知道「Table 1: ...」这行字是下面那个表格的标题。`Jointer` 就是专门修复这些「排版常识」的模块。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pdf_craft/extractor/chapter/jointer.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py) | 块合并器主流程 `Jointer.execute`、表格标题/脚注正则分类、LaTeX 保护与 `InlineExpression` 还原 |
| [pdf_craft/extractor/chapter/mergeable.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/mergeable.py) | 可合并判定 `check_mergeable`：句尾/续行/断词符号集与编号正则 |
| [pdf_craft/extractor/chapter/reading_serials.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reading_serials.py) | 阅读序号切分 `split_reading_serials`：多栏与挤压布局的分栏算法 |
| [pdf_craft/extractor/chapter/content.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/content.py) | `first` / `last` / `expand_text_in_content` 等内容遍历辅助函数 |
| [pdf_craft/extractor/chapter/generation.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py) | `Jointer` 的消费方：章节生成主流程（u5-l3 详讲，本讲只看装配点） |
| [pdf_craft/expression.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/expression.py) | `ExpressionKind` 与 `parse_latex_expressions`：LaTeX 定界符状态机 |
| [tests/test_jointer.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_jointer.py)、[tests/test_mergeable.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_mergeable.py)、[tests/test_reading_serials.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_reading_serials.py)、[tests/test_punctuation.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_punctuation.py) | 本讲实践的主要参照测试 |

## 4. 核心概念与源码讲解

### 4.1 块合并器：Jointer 的主流程

#### 4.1.1 概念说明

`Jointer` 解决的问题是：**把「按页、按栏、按块」组织的 OCR 输出，重组为「按自然段和资源」组织的文档流**。

它的输入是一个 `(页码, 该页 PageLayout 列表)` 的可迭代对象；输出是 `ParagraphLayout | AssetLayout` 的生成器。注意它不区分正文和脚注——章节生成流程会为两者各建一个 `Jointer`：

[pdf_craft/extractor/chapter/generation.py:97-110](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L97-L110) 中，`_extract_body_layouts` 用同一个 `Jointer` 类分别包装「每页正文布局」与「每页脚注布局」两个生成器（过滤掉目录页），下游再从脚注流中提取引用（u5-l4 主题）。

#### 4.1.2 核心流程

`execute` 的主循环可以概括为下面的伪代码（每组 = 一个阅读序号组，见 4.2）：

```text
last_tail = None  # 上一组末尾待定段的容器（段落 + 推迟输出的资源）

对每个 (页码, 组内布局):
    layouts = 资源归位(组内布局)        # 4.4：表格标题/脚注在此归位，产出段落流与资源流
    head, body, tail = 切三段(layouts)  # 前导资源 | 段落主体 | 尾随资源

    若 body 为空:
        head 与 tail 全部挂到 last_tail.override（或直接输出），本组结束

    若 last_tail 存在 且 可合并(last_tail 段落, body[0]):
        把 body[0].blocks 追加进 last_tail 段落，删掉 body[0]
        若 body 因此变空 → 继续持有 last_tail，进入下一组

    # 走到这里说明连续吞并遇阻：
    冲刷 last_tail（先做断词归一化，再输出段落，再输出推迟的资源）
    输出 head
    输出 body 除最后一个之外的全部段落
    last_tail = (body 的最后一个段落, override=tail)

循环结束后冲刷残留的 last_tail
```

这套「持有—吞并—冲刷」的状态机对应源码注释里的两条业务要求（见 [pdf_craft/extractor/chapter/jointer.py:68-70](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L68-L70)）：

1. 阅读序列跨越 group（跨页、跨分栏、跨因图片挤压而拆分的段落）时，验证连接处，是被拆分的自然段就拼起来；
2. 因插图、表格而被拆开的自然段，把插图存起来接到完整自然段的最后，而不是任其分割自然段。

`override` 机制就是第 2 条的实现：插图不打断合并中的段落，而是排队等段落定型后输出。

#### 4.1.3 源码精读

主循环：[pdf_craft/extractor/chapter/jointer.py:64-118](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L64-L118) —— `Jointer.execute` 生成器。关键三处：

```python
first_layout = cast(ParagraphLayout, body[0])
if last_tail and self._can_merge_paragraphs(
    last_tail.page_para, first_layout
):
    last_tail.page_para.blocks.extend(first_layout.blocks)
    del body[0]
```

（[L83-88](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L83-L88)）合并的实现极其朴素：把下一组首段的 `blocks` 列表直接 `extend` 进上一段——合并后的段落天然拥有多个块，每块保留自己的页码与坐标。

```python
if last_tail:
    _normalize_paragraph_content(last_tail.page_para)
    yield last_tail.page_para
    yield from last_tail.override
    last_tail = None
```

（[L100-104](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L100-L104)）冲刷顺序固定为「先段落、后推迟的资源」。

三段切分：[pdf_craft/extractor/chapter/jointer.py:127-147](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L127-L147) —— `_split_layouts` 从前往后收集首个段落之前的资源进 `head`，从后往前收集最后一个段落之后的资源进 `tail`（再反转回原顺序），中间即 `body`。资源（图/表/公式）永远不参与跨页合并，也不阻挡段落合并。

内部容器：[pdf_craft/extractor/chapter/jointer.py:37-57](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L37-L57) —— 三个模块私有 dataclass：`_LastTail`（待定段落 + 推迟资源）、`_AssetHolder`（资源归位中的中间态，字段比 `AssetLayout` 宽松，`title`/`content`/`caption` 还是纯 `str`）、`_PendingParagraph`（可能成为表格标题的挂起段落）。

组的迭代：[pdf_craft/extractor/chapter/jointer.py:120-125](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L120-L125) —— `_iter_layout_serials` 对每页调用 `split_reading_serials`，把切出的每个栏作为独立的一组喂给主循环。

#### 4.1.4 代码实践

**实践目标**：直观观察跨页合并与「句号阻止合并」两种结果。

操作步骤（示例代码，非项目原有代码；保存为仓库根目录的 `jointer_demo.py`，需已按 u1-l2 安装 pdf-craft）：

```python
from pdf_craft.pdf import PageLayout
from pdf_craft.extractor.chapter.jointer import Jointer

def page(text):
    # det 给一个统一的正文块位置；order=0
    return [PageLayout(ref="text", det=(100, 100, 500, 150),
                       text=text, order=0, hash=None)]

# 场景 A：第 1 页句子没写完，第 2 页是续文
for layout in Jointer([(1, page("The quick brown fox jumps over the lazy")),
                       (2, page("dog and runs away."))]).execute():
    print("A:", type(layout).__name__, "blocks =",
          [(b.page_index, b.order) for b in layout.blocks])

# 场景 B：第 1 页是完整句（句号结尾）
for layout in Jointer([(1, page("This sentence is complete.")),
                       (2, page("A new paragraph starts here."))]).execute():
    print("B:", type(layout).__name__, "blocks =",
          [(b.page_index, b.order) for b in layout.blocks])
```

运行 `python jointer_demo.py`。

需要观察的现象：场景 A 输出一个 `ParagraphLayout`、`blocks` 为 `[(1, 0), (2, 0)]`；场景 B 输出两个 `ParagraphLayout`，各含一个块。

预期结果（依据源码追踪，待本地验证）：A 合并、B 不合并。把场景 B 第 1 页的句号删掉再跑一次，应看到它变成合并——这正是 4.3 条件 1 的作用。

#### 4.1.5 小练习与答案

1. **练习**：一页只有一张图片（`body` 为空）且此时存在 `last_tail`，`head` 与 `tail` 会被输出吗？
   **答案**：不会立刻输出。它们被 `extend` 进 `last_tail.override`，推迟到该段落最终冲刷时、在段落之后输出（[L74-81](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L74-L81)），保证插图不割裂自然段。
2. **练习**：合并后的段落里，每个 `BlockLayout` 还能知道自己在原 PDF 的哪一页吗？
   **答案**：能。合并只是 `blocks.extend`，每个块保留自己的 `page_index`、`order`、`det`，这为 u5-l4 脚注关联与 u10 的 PDF 回写保留了坐标。
3. **练习**：为什么冲刷 `last_tail` 之前要调用 `_normalize_paragraph_content`？
   **答案**：因为跨行断词（如 `comput‑` / `er`）只有在相邻块同处一个段落后才能物理拼接，见 4.3.3。

### 4.2 阅读序号切分：split_reading_serials

#### 4.2.1 概念说明

在合并之前必须先回答「哪些块属于同一个阅读序列」。扫描文档常见两类干扰：

- **多栏布局**：学术论文的双栏/三栏，OCR 的 `order` 可能按「整页从上到下」交错排列两栏的块；
- **挤压布局**：一个大图或大表把旁边的文字挤窄，被挤窄的块与正常块宽度差异明显，即使同在「一栏」也应区分对待。

`split_reading_serials` 的算法思路写在它的 docstring 里（[pdf_craft/extractor/chapter/reading_serials.py:22-42](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reading_serials.py#L22-L42)）：把所有块投影到 x 轴、构建以块高度为权重的直方图、在波谷处切分。

#### 4.2.2 核心流程

```text
1. 每个块投影为 (center, size=宽度, weight=高度, payload)
2. 以「可见天际线」方式构建直方图（矮块被右侧更高的块截断）
3. 用 3 元素滑动窗口在直方图上分类局部形态，识别波谷位置
4. 按波谷从左到右切分：center < valley 的块归入当前组并从候选池移除
5. 组内再用 split_by_cv 按宽度做二次切分（CV ≤ 0.1），处理被挤压的块
6. 各组内保持 OCR 原始 order；组按从左到右顺序 yield
```

二次切分的判据是变异系数 \(\frac{\sigma}{\mu} \le 0.1\)（`_CV`，[L9](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reading_serials.py#L9)）；投影时宽度先抬升到均值的一成五（`_MIN_SIZE_RATE = 0.15`，[L11](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reading_serials.py#L11)），避免极窄块变成直方图毛刺。

#### 4.2.3 源码精读

入口与重排：[pdf_craft/extractor/chapter/reading_serials.py:43-69](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reading_serials.py#L43-L69) —— `split_reading_serials` 主体。它把 `(order, group_id, layout)` 三元组按原始 `order` 排序后，按 `group_id` 是否变化切成连续段 yield。也就是说：**组内顺序 = OCR 原始顺序，组间顺序 = 从左到右的栏序**。

投影：[pdf_craft/extractor/chapter/reading_serials.py:72-81](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reading_serials.py#L72-L81) —— `_wrap_projection` 从 `det` 算出中心、宽度，权重取块高。

分组与波谷：[pdf_craft/extractor/chapter/reading_serials.py:84-117](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reading_serials.py#L84-L117) —— `_group_projects` 对每个波谷把 `center < valley` 的块移出候选池并分组；[pdf_craft/extractor/chapter/reading_serials.py:127-161](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reading_serials.py#L127-L161) —— `_find_valleys` 用滑动窗口形态分类（`TOUCHED_GROUND` / `LEFT_GROUND` / `FLAT_GROUND` / `AT_VALLEY` / `OTHER`，[L181-206](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reading_serials.py#L181-L206)）输出波谷 x 坐标；[L164-177](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reading_serials.py#L164-L177) —— `_histograms` 按左边界排序后，用右侧更高块截断当前块的可见右边界，得到天际线式直方图。

#### 4.2.4 代码实践

**实践目标**：用合成的双栏布局验证分栏行为。

操作步骤（示例代码，非项目原有代码；参照 [tests/test_reading_serials.py:151-190](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_reading_serials.py#L151-L190) 的 `test_grouped_by_columns`）：

```python
from pdf_craft.pdf import PageLayout
from pdf_craft.extractor.chapter.reading_serials import split_reading_serials

layouts = []
for i in range(5):   # 左列，x ∈ [50, 150]
    layouts.append(PageLayout(ref="text", det=(50, 100 + i * 100, 150, 180 + i * 100),
                              text=f"Left{i}", order=i, hash=None))
for i in range(5):   # 右列，x ∈ [400, 500]
    layouts.append(PageLayout(ref="text", det=(400, 100 + i * 100, 500, 180 + i * 100),
                              text=f"Right{i}", order=i + 5, hash=None))

for group in split_reading_serials(layouts):
    print([layout.text for layout in group])
```

需要观察的现象：两列中心相距 300 像素、间隙巨大，直方图在两栏之间应出现明显波谷。

预期结果（待本地验证）：输出两个组 `['Left0'..'Left4']` 与 `['Right0'..'Right4']`。若把右列的 x 改成与左列重叠（例如 `det=(120, ...)`），应观察到合并为一个组。

#### 4.2.5 小练习与答案

1. **练习**：直方图的权重为什么用块的高度而不是统一为 1？
   **答案**：docstring 明确「高度作为权重，避免小字符干扰」——一个页眉小字块不应与整栏正文块在天际线上等权（[L36-37](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reading_serials.py#L36-L37)）。
2. **练习**：组内的 `split_by_cv` 二次切分解决什么问题？
   **答案**：被图片挤压而变窄的文字块与同栏正常块宽度差异大，按 CV ≤ 0.1 再切一刀，把挤压块单独成组（[L106-110](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reading_serials.py#L106-L110)），让 4.1 的合并逻辑在连接处做验证。
3. **练习**：单栏普通页面会输出几组？
   **答案**：一组（没有波谷时全部块进入最后一个分组，[L112-117](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/reading_serials.py#L112-L117)）。

### 4.3 可合并判定：check_mergeable 与跨行断词

#### 4.3.1 概念说明

`check_mergeable(content1, content2)` 回答一个问题：**前一块的结尾和后一块的开头放在一起看，像一个被拆开的自然段吗？**它是纯函数，只看两段内容的文本特征，是 `Jointer._can_merge_paragraphs`（[pdf_craft/extractor/chapter/jointer.py:272-283](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L272-L283)）调用的最终裁判——且仅当两段 `ref` 都是 `text` 时才会走到这里（标题永不参与跨页合并）。

实现思路参考了 MinerU 的段落切分经验（源码注释留有链接）。

#### 4.3.2 核心流程

判定按顺序检查四个条件，先命中先返回：

| 顺序 | 条件 | 位置 | 结果 |
| --- | --- | --- | --- |
| 前置 | 任一侧首/尾不是纯字符串，或去空白后为空 | [L87-94](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/mergeable.py#L87-L94) | 不合并 |
| 1 | 前段以**句尾符号**结尾（`. ! ? 。！？)）"" ; ； ] 】 } > 》`） | [L97-99](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/mergeable.py#L97-L99) | 不合并（完整段落） |
| 2 | 前段以**续行符号**结尾（`[ 【 { < 《 、 , ，`） | [L101-103](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/mergeable.py#L101-L103) | 合并（句子明显未完） |
| 3 | 前段结尾是「拉丁字母 + Unicode 连字符」且后段以拉丁字母开头 | [L105-112](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/mergeable.py#L105-L112) | 合并（跨行断词） |
| 4 | 后段以**编号**开头（如 `1.`、`(一)`、`[iv]`、`<1>`、`（1）`）且编号后还有实质内容 | [L114-118](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/mergeable.py#L114-L118) | 不合并（新编号段落） |
| 默认 | 以上皆未命中 | [L120](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/mergeable.py#L120) | 合并 |

注意两个易错点：

- 条件 1、2 看的是**前段末尾**；「`:` / `：`」既不在句尾集也不在续行集——冒号结尾走默认合并，除非后段是编号开头（这是 [tests/test_mergeable.py:11-44](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_mergeable.py#L11-L44) 记录过的真实 bug 案例）。
- 半角 `(`、全角 `（` 都**不在**续行集合中，`("Text with (", "content inside)")` 之所以合并，走的是默认分支。

编号正则由「数字形式 × 包裹符」两个维度组合生成（[L52-75](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/mergeable.py#L52-L75)）：数字形式为阿拉伯 / 大小写罗马 / 中文数字，包裹符为半角/全角圆括号、方括号、尖括号、点号、右括号、顿号，共 \(4 \times 7 = 28\) 个锚定开头的正则。

`LINK_FLAGS`（[L43-50](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/mergeable.py#L43-L50)）只含六个 Unicode 连字符（U+2010 至 U+2015：`‐ ‑ ‒ – — ―`），**不含** ASCII 的 `-`。OCR 输出的排版连字符通常是这些 Unicode 形式。

#### 4.3.3 源码精读

判定主体：[pdf_craft/extractor/chapter/mergeable.py:79-120](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/mergeable.py#L79-L120) —— `check_mergeable`。它先用 `last(content1)` / `first(content2)` 取首尾文本：

```python
for pattern in _NUMBERING_PATTERNS:
    match = pattern.match(text2_stripped)
    if match and (len(content2) > 1 or bool(text2_stripped[match.end() :].strip())):
        return False
```

（[L115-118](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/mergeable.py#L115-L118)）条件 4 的守卫很讲究：孤零零一个 `"1."`（编号后没有内容且内容列表只有一个节点）**不阻止**合并——它更可能是 OCR 切出来的碎片而非新段落（对应 [tests/test_mergeable.py:68](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_mergeable.py#L68) 的用例 `(["Text"], ["(1)"], True, "只有编号没有内容")`）。

首尾提取会递归进 HTML 标签：[pdf_craft/extractor/chapter/content.py:9-26](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/content.py#L9-L26) —— `first` / `last` 遇到 `HTMLTag` 时深入其 `children`，所以「段尾是 `<b>word</b>`」时看的是标签内的 `word`。

断词的物理拼接：[pdf_craft/extractor/chapter/jointer.py:460-491](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L460-L491) —— `_normalize_paragraph_content` 在段落冲刷前，对相邻两块调用 `_is_splitted_word`（[L548-554](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L548-L554)）；命中则把后块开头的拉丁字母串搬到前块末尾并删掉连字符，后块因此清空时整个块会被移除。

#### 4.3.4 代码实践

**实践目标**：用几组直构输入验证四个条件的走向（可直接运行，也可整理成 unittest，写法参照 [tests/test_mergeable.py:46-75](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_mergeable.py#L46-L75)）。

操作步骤（示例代码，非项目原有代码）：

```python
from pdf_craft.extractor.chapter.mergeable import check_mergeable

cases = [
    (["...jumps over the lazy"], ["dog and runs away."]),  # 预期 True：默认合并
    (["This is complete."],    ["New paragraph."]),       # 预期 False：条件 1 句尾符号
    (["数组定义如下，"],          ["继续内容"]),               # 预期 True：条件 2 全角逗号
    (["see Fig‐"],        ["ure 5 for details."]),  # 预期 True：条件 3 跨行断词
    (["前文到此为止"],            ["1. 第二个论点"]),          # 预期 False：条件 4 编号开头
    (["前文到此为止"],            ["(1)"]),                 # 预期 True：编号后无内容，不阻止
    (["Perform the procedure according to the following gradient:"],
     ["(4) Solution preparation"]),                       # 预期 False：真实 bug 案例
]
for c1, c2 in cases:
    print(check_mergeable(c1, c2), "<-", c1[-1][-24:], "|", c2[0][:24])
```

需要观察的现象：除第 2、5、7 组外全部输出 `True`；第 7 组说明冒号结尾本身不构成「句已完结」，阻止它的是后段的编号。

预期结果：如各行注释所标（与 tests/test_mergeable.py 的既有断言一致）。想改成正式测试，把 `cases` 加上期望值与描述字段，套用该文件 `test_constructed_cases` 的 `subTest` 循环即可，然后用 `python -m unittest tests.test_mergeable -v` 对跑官方用例验证环境。

#### 4.3.5 小练习与答案

1. **练习**：`["First part,"]` 与 `["second part"]` 为什么合并？走的哪个条件？
   **答案**：半角逗号 `,` 在续行符号集中，条件 2 直接返回 True。
2. **练习**：ASCII 连字符 `"This is hyper-"` + `"text"`（注意是 ASCII `-`）会怎样？
   **答案**：合并仍会发生（默认分支），但条件 3 不命中（`LINK_FLAGS` 不含 ASCII `-`），且 `_is_splitted_word` 同样不认，连字符会残留在输出里。
3. **练习**：为什么条件 4 要求「编号后有实质内容」才返回 False？
   **答案**：防止把 OCR 切碎的编号孤片误判为新段落的开头，见上文对守卫表达式的分析。

### 4.4 正则分类：表格标题、脚注与几何验证

#### 4.4.1 概念说明

`ref` 为 `table` / `image` / `equation` 的块是「资源」，但 OCR 常把它们的**标题**（"Table 1: ..."）与**注释**（"Source: ..."、"1. 注释正文"）识别成独立的 `text` 块。`_join_asset_layouts` 这个状态机负责把这些游离文字归位到资源的 `title` / `caption` 字段，把普通文字组装成段落。归位的依据是**文本正则 + 检测框几何**的双重验证——正则说「长得像」，几何说「位置对」。

#### 4.4.2 核心流程

```text
对组内每个 layout:
    ref ∈ ASSET_TAGS(image/table/equation):
        若挂起段落是表格标题（文本像 + 位置在表格正上方且贴近且水平相关）
            → 挂起段落的文本成为新资源的 title
        冲刷上一个资源与挂起段落，创建 _AssetHolder
    ref ∈ _ASSET_CAPTION_TAGS(image_caption 等上游已标注的注释块):
        冲刷挂起段落，把文本追加到上一个资源的 caption
    其他（text / sub_title）:
        若上一资源是 table 且该文本像表格注释（含脚注编号）且位于表格正下方
            → 追加到该表格的 caption，继续
        否则冲刷上一资源与挂起段落
        标题块去掉 DeepSeek OCR 总会生成的行首 "#"/"##" 符号
        包装成单块 ParagraphLayout
        若文本像表格标题 → 挂起等待（可能成为下一个表格的 title），否则立即产出
循环结束冲刷残留
```

随后 `_join_and_handle_asset_layouts` 把中间态 `_AssetHolder` 收尾：`equation` 做 `_normalize_equation`、`table` 做 `_normalize_table`，再把 `title` / `content` / `caption` 三个纯文本字段解析成 `Content`（见 4.5），产出正式的 `AssetLayout`。

#### 4.4.3 源码精读

四张正则表：[pdf_craft/extractor/chapter/jointer.py:17-30](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L17-L30)

| 正则 | 匹配什么 |
| --- | --- |
| `_TABLE_PATTERN` | 一段完整的 `<table ...>...</table>` HTML（忽略大小写、跨行），供 `_normalize_table` 从混杂文本中剥出表格本体 |
| `_TABLE_TITLE_PATTERN` | `Table 1:`、`tab. 2.`、`表 3、`、`Tab IV.` 等表格标题开头（数字支持阿拉伯 / 中文 / 罗马小写，`IGNORECASE` 兼容大写罗马） |
| `_TABLE_CAPTION_PATTERN` | `Source:`、`Note:`、`注：`、`资料来源：` 等表格注释开头 |
| `_FOOTNOTE_PATTERN` | `1)`、`2.`、`a、`、`①`…`⑳`、上标数字串后跟空白——即脚注编号的常见形态 |

文本侧的守门函数：[pdf_craft/extractor/chapter/jointer.py:390-409](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L390-L409) —— `_is_table_title_text` 与 `_is_table_caption_text`。除正则命中外还有三条共同约束：归一化空白后长度不超过上限（标题 180、注释 260 字符，[L32-33](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L32-L33)）、不含空行（`"\n\n"`）、标题额外接受以 `:` / `：` 结尾的短文本，注释额外接受 `_FOOTNOTE_PATTERN` 与字面量 `"category not applicable."`。长度与空行约束的意义是防止把大段正文误判成标题/注释。

几何三条件：[pdf_craft/extractor/chapter/jointer.py:416-456](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L416-L456)：

- `_is_block_above` / `_is_block_below`：块的 `y2` 不超过对方的 `y1`（y 向下增大，小者在上）；
- `_is_close_to_table`：垂直间距非负且 \(\text{gap} \le \max(30,\ 0.12 \times \text{表高})\)；
- `_is_horizontally_related`：水平重叠率 \(\ge 0.6\)，或宽度比 \(\ge 0.75\) 且中心距 \(\le 20\%\) 表宽，其中重叠

\[ \text{overlap} = \max\left(0,\ \min(x_2^{a}, x_2^{b}) - \max(x_1^{a}, x_1^{b})\right) \]

三个判定入口：[pdf_craft/extractor/chapter/jointer.py:364-387](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L364-L387) —— `_can_wait_for_table_title`（非标题块 + 文本像表格标题 → 值得挂起等待）、`_can_join_table_title`（像 + 在上 + 近 + 水平相关 → 成为表格 title）、`_can_join_table_caption`（像 + 在下 + 近 + 水平相关 → 成为表格 caption）。

状态机主体：[pdf_craft/extractor/chapter/jointer.py:178-270](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L178-L270) —— `_join_asset_layouts`；收尾转换：[pdf_craft/extractor/chapter/jointer.py:149-176](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L149-L176) —— `_join_and_handle_asset_layouts`。另外两处归一化：

- `_normalize_table`（[L322-361](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L322-L361)）：在 `title`、`content`、`caption` 三段文本中搜索 `_TABLE_PATTERN`，把 `<table>` 之前的一切归入 title、之后的一切归入 caption——OCR 常把「标题 + 表格 + 注释」挤在一个块里输出；
- `_MARKDOWN_HEAD_PATTERN`（[L17](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L17)、[L243-245](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L243-L245)）：删掉标题块行首的 `##` 之类符号（DeepSeek OCR 的习惯性产物）。

#### 4.4.4 代码实践

**实践目标**：验证「像脚注」与「数字开头的正文」在 `_FOOTNOTE_PATTERN` 下的分野。

操作步骤（示例代码，非项目原有代码；两组断言分别对应 [tests/test_jointer.py:463-483](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_jointer.py#L463-L483) 与 [tests/test_jointer.py:485-512](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_jointer.py#L485-L512)）：

```python
from pdf_craft.extractor.chapter.jointer import (
    _is_table_caption_text, _is_table_title_text,
)

print(_is_table_title_text("Table 1: Emergency visits"))   # 预期 True
print(_is_table_title_text("This is a normal paragraph.")) # 预期 False
print(_is_table_caption_text("1. The mean length of stay was 5.3 days."))  # 预期 True（脚注编号）
print(_is_table_caption_text("Note: Numbers may not add to totals."))     # 预期 True
print(_is_table_caption_text("12 patients were excluded from the trial."))# 预期 False
```

需要观察的现象：最后一行为 False——`\d{1,2}[\).、]` 要求数字后紧跟 `)` / `.` / `、`，而 `12 ` 后面是空格，所以「12 patients …」这行数字开头的正文不会被误收进表格注释（对应官方用例 `test_numeric_leading_body_text_is_not_attached_to_table_caption`）。

预期结果：如注释所标。进一步可在同一脚本里把 `det=(100,120,500,260)` 的表格块与 `det=(105,270,500,300)` 的文本块送进 `Jointer([(1, layouts)]).execute()`，观察脚注被收进 `AssetLayout.caption`。

#### 4.4.5 小练习与答案

1. **练习**：`_can_join_table_title` 为什么需要 `_is_horizontally_related`，只有「在表格上方且贴近」不够吗？
   **答案**：不够。双栏排版中右栏的一段正文可能恰好位于左栏表格的斜上方，「上方 + 近」都会满足；水平相关（重叠率或宽度比 + 中心距）排除这种跨栏误挂。
2. **练习**：上游已经产出 `table_caption` 这类 ref，为什么还要 `_can_join_table_caption` 这条路径？
   **答案**：OCR 不总会把注释识别成 `*_caption` 类型，很多注释被当作普通 `text` 块输出，需要正则 + 几何把它抢回来（[L223-233](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L223-L233) 的 else 分支前半段）。
3. **练习**：一个「Table 2: ...」文本块后面跟的不是表格而是正文，会发生什么？
   **答案**：它被 `_can_wait_for_table_title` 挂起；遇到非表格的下一个块时挂起段落在通用冲刷路径中作为普通 `ParagraphLayout` 产出（[L235-241](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L235-L241)），不会丢字。

### 4.5 公式解析：LaTeX 保护与 InlineExpression

#### 4.5.1 概念说明

OCR 会把公式按 LaTeX 形式混在文本里（`$...$`、`$$...$$`、`\(...\)`、`\[...\]` 四种定界）。块的 `Content` 需要把它们切出来变成 `InlineExpression` 对象（u5-l1 讲过：渲染 EPUB 时可按选项转 MathML 等）。

难点在于**解析顺序**：文本还要过一遍 Markdown/HTML 解析（`parse_raw_markdown`，u6-l2 详讲），而公式里常有 `<`、`>`（如 `0<\Re(s)<1`），直接解析会被当成 HTML 标签破坏边界。解法是先把公式替换成不含尖括号的私有占位符，解析完再换回来。

#### 4.5.2 核心流程

```text
_parse_block_content(text):
    1. parse_latex_expressions(text) 状态机扫描四种定界符
    2. 公式片段 → 占位符 "PDF_CRAFT_LATEX_{防撞序号}_{id}"
       （两侧包裹 PUA 私有区字符，正常文本几乎不可能出现）
    3. parse_raw_markdown(保护后的文本) → str 与 HTMLTag 混合的 Content
    4. expand_text_in_content 遍历所有文本节点，把占位符替换回 InlineExpression
```

#### 4.5.3 源码精读

定界符种类：[pdf_craft/expression.py:6-11](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/expression.py#L6-L11) —— `ExpressionKind` 枚举 TEXT 与四种定界；[pdf_craft/expression.py:68](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/expression.py#L68) 起的 `parse_latex_expressions` 是逐字符状态机，能正确处理 `\$` 转义（对应 [tests/test_jointer.py:624-628](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_jointer.py#L624-L628) 的用例：`r"Price is \$100"` 不触发公式）。

主函数：[pdf_craft/extractor/chapter/jointer.py:494-519](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L494-L519) —— `_parse_block_content`：先保护、再解析、再展开。展开回调 `expand_text` 把占位符替换成 `InlineExpression(kind=..., content=...)`。

保护与防撞：[pdf_craft/extractor/chapter/jointer.py:522-536](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L522-L536) —— `_protect_latex_expressions` 生成占位符；[L539-545](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L539-L545) —— `_create_latex_placeholder_prefix` 逐一递增序号直到前缀在原文中不存在，防止原文恰好包含同形文本时互相串扰（[tests/test_jointer.py:704-721](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_jointer.py#L704-L721) 有专门用例）。

深度遍历展开：[pdf_craft/extractor/chapter/content.py:42-56](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/content.py#L42-L56) —— `expand_text_in_content` 会进入 `HTMLTag.children` 递归替换，所以表格单元格里的公式同样能还原（对应 [tests/test_jointer.py:680-702](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_jointer.py#L680-L702) 的 `test_latex_protection_keeps_html_table_parsing`：`<td>$ 0<\Re(s)<1 $</td>` 结构完好）。

整块公式的归一化：[pdf_craft/extractor/chapter/jointer.py:286-319](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L286-L319) —— `_normalize_equation` 处理 `ref="equation"` 的资源：取内容中**第一个**表达式作为公式本体，之前的文本（连同已有 title）并入 `title`，之后的文本并入 `caption`（经 `ParsedItem.reverse()` 转回 Markdown 定界符形式）。

#### 4.5.4 代码实践

**实践目标**：观察一行「文字 + 公式 + 文字」被解析成的三段 Content。

操作步骤（示例代码，非项目原有代码；断言与 [tests/test_jointer.py:647-657](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_jointer.py#L647-L657) 的 `test_complex_latex_content` 一致）：

```python
from pdf_craft.extractor.chapter.jointer import _parse_block_content
from pdf_craft.extractor.chapter.chapter import InlineExpression

content = _parse_block_content(r"The integral $\int_0^\infty e^{-x^2} dx$ converges")
for item in content:
    if isinstance(item, InlineExpression):
        print("InlineExpression:", item.kind, repr(item.content))
    else:
        print("text:", repr(item))
```

需要观察的现象：输出三个元素——`text: 'The integral '`、一个 `InlineExpression`（content 为 `\int_0^\infty e^{-x^2} dx`）、`text: ' converges'`。

预期结果：如上（与官方测试断言一致）。可再试试 `r"Mix $a$ and \(b\) and $$c$$"`（预期 6 个元素，见 `test_mixed_delimiters`）体会四种定界的混排。

#### 4.5.5 小练习与答案

1. **练习**：为什么占位符两侧要用 PUA 私有区字符（`` 与 ``）？
   **答案**：Unicode 私有使用区字符不出现在正常 OCR 文本中，占位符不会与正文撞形；即便万一撞了，`_create_latex_placeholder_prefix` 还会递增序号避让。
2. **练习**：`_normalize_equation` 只取第一个表达式作为本体，后面的表达式去哪了？
   **答案**：第一个表达式之后的所有片段（含后续表达式按 Markdown 定界符还原的文本）拼接进 `caption`（[L299-300](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L299-L300)、[L318-319](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L318-L319)）。
3. **练习**：`_parse_block_content(None)` 返回什么？
   **答案**：空列表 `[]`（[L495-496](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/jointer.py#L495-L496)），对应资源字段缺省的情形。

## 5. 综合实践

把本讲全部知识串成一个单元测试文件：**构造五段典型文本，验证它们在 `Jointer` 中分别走向哪个分支**。

实践目标、操作步骤（示例代码，非项目原有代码；保存为仓库根目录 `test_jointer_practice.py`，写法参照 [tests/test_jointer.py:724-737](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_jointer.py#L724-L737) 的 `_page_layout` 辅助函数）：

```python
import unittest

from pdf_craft.pdf import PageLayout
from pdf_craft.extractor.chapter.chapter import (
    AssetLayout, InlineExpression, ParagraphLayout,
)
from pdf_craft.extractor.chapter.jointer import Jointer


def _page_layout(ref, det, text, order=0, hash=None):
    return PageLayout(ref=ref, det=det, text=text, order=order, hash=hash)


class TestFiveTextKinds(unittest.TestCase):

    def test_plain_paragraph(self):
        """普通段：直接产出 ParagraphLayout"""
        layouts = [_page_layout("text", (100, 100, 500, 150),
                                "This is a normal paragraph.")]
        result = list(Jointer([(1, layouts)]).execute())
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], ParagraphLayout)
        self.assertEqual(result[0].blocks[0].content,
                         ["This is a normal paragraph."])

    def test_table_title_attached(self):
        """表格标题：挂起后被下方表格吸收为 AssetLayout.title"""
        layouts = [
            _page_layout("text", (105, 80, 495, 110),
                         "Table 1: Emergency visits"),
            _page_layout("table", (100, 120, 500, 260),
                         "<table><tr><td>A</td></tr></table>"),
        ]
        result = list(Jointer([(1, layouts)]).execute())
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], AssetLayout)
        self.assertEqual(result[0].title, ["Table 1: Emergency visits"])

    def test_footnote_attached_as_caption(self):
        """脚注：表格正下方的编号文本被吸收为 caption"""
        layouts = [
            _page_layout("table", (100, 120, 500, 260),
                         "<table><tr><td>A</td></tr></table>"),
            _page_layout("text", (105, 270, 500, 300),
                         "1. The mean length of stay was 5.3 days."),
        ]
        result = list(Jointer([(1, layouts)]).execute())
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], AssetLayout)
        self.assertEqual(result[0].caption,
                         ["1. The mean length of stay was 5.3 days."])

    def test_cross_page_merge(self):
        """断句续行：两页各一段合并为一个段落、两个块"""
        page1 = [_page_layout("text", (100, 100, 500, 150),
                              "The quick brown fox jumps over the lazy")]
        page2 = [_page_layout("text", (100, 100, 500, 150),
                              "dog and runs away.")]
        result = list(Jointer([(1, page1), (2, page2)]).execute())
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], ParagraphLayout)
        self.assertEqual(len(result[0].blocks), 2)
        self.assertEqual([b.page_index for b in result[0].blocks], [1, 2])

    def test_formula_line_parsed(self):
        """公式行：LaTeX 被解析为 InlineExpression"""
        layouts = [_page_layout(
            "text", (100, 100, 500, 150),
            r"The integral $\int_0^\infty e^{-x^2} dx$ converges")]
        result = list(Jointer([(1, layouts)]).execute())
        content = result[0].blocks[0].content
        self.assertEqual(len(content), 3)
        self.assertEqual(content[0], "The integral ")
        self.assertIsInstance(content[1], InlineExpression)
        self.assertEqual(content[1].content, r"\int_0^\infty e^{-x^2} dx")
        self.assertEqual(content[2], " converges")


if __name__ == "__main__":
    unittest.main()
```

运行 `python test_jointer_practice.py -v`；再用 `python -m unittest tests.test_jointer tests.test_mergeable tests.test_reading_serials -v` 对跑官方测试，确认环境一致。

需要观察的现象与预期结果：五个用例全绿（表格标题/脚注两个用例的 `det` 与断言直接取自官方测试；跨页合并用例依据 4.1 的流程追踪，待本地验证）。之后的进阶改造：

1. 把 `test_cross_page_merge` 第 1 页文本改成以句号结尾，断言变为两个段落——验证条件 1；
2. 把脚注用例的文本换成 `12 patients were excluded from the trial.`，断言 `len(result) == 2` 且 `asset.caption == []`——验证 `_FOOTNOTE_PATTERN` 的编号形态约束；
3. 给公式行追加一个 `<b>` 标签与第二个 `$y$` 公式，观察 Content 中 HTMLTag 与 InlineExpression 的混排顺序。

## 6. 本讲小结

- `Jointer` 是「按页按块的 OCR 输出」到「按自然段与资源的文档流」的重组器；主循环以「持有—吞并—冲刷」状态机跨阅读序号组合并段落，资源经 `override` 排队到段落之后，不打断自然段。
- 跨页合并的最终裁判是纯函数 `check_mergeable`：句尾符号阻止合并、续行符号强制合并、Unicode 连字符断词强制合并、编号开头阻止合并，默认合并；合并后 `_normalize_paragraph_content` 再物理拼接断词。
- 表格标题与脚注的归位靠「正则说像 + 几何说对」：`_TABLE_TITLE_PATTERN` / `_TABLE_CAPTION_PATTERN` / `_FOOTNOTE_PATTERN` 配合上方/下方、贴近、水平相关三重几何验证；`_normalize_table` 还能把挤在一个块里的「标题+表格+注释」剥开。
- `split_reading_serials` 用 x 轴加权投影直方图找波谷完成分栏，组内再按宽度 CV ≤ 0.1 二次切分被图片挤压的块；组内保持 OCR 原始顺序。
- LaTeX 公式在 Markdown 解析前被 PUA 字符占位符保护，解析后经深度遍历还原为 `InlineExpression`，公式中的尖括号因此不会破坏 HTML 解析。

## 7. 下一步学习建议

本讲的产出（合并好的 `ParagraphLayout` / `AssetLayout` 流）正是下一讲 **u5-l3 章节生成流程**的输入：`generate_chapter_files` 如何按目录条目把这些布局切分进 `chapter_head.xml` / `chapter_N.xml`，并在写盘前做章内层级分析。建议先读 [pdf_craft/extractor/chapter/generation.py:90-136](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/chapter/generation.py#L90-L136)，看清本讲的 `body_jointer.execute()` 在哪里被消费。之后再进入 u5-l4（脚注 `Reference` 如何从 `footnotes_jointer` 的输出中提取并与正文 mark 关联）。若想巩固本讲的纯函数测试习惯，推荐通读 [tests/test_mergeable.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_mergeable.py)——尤其是它开头的「history bugs」真实语料回归集。
