# LLM-only 翻译与质量校验：ILTranslatorLLMOnly

## 1. 本讲目标

本讲是「翻译机制」单元的第三篇，承接 u6-l2 讲过的 `ILTranslator`（一段一请求、占位符 + 优先级线程池）。学完本讲，你应当能够：

- 说清 `ILTranslatorLLMOnly` 与 `ILTranslator` 的本质区别：前者把**多个段落打包成一次 JSON 请求**送给 LLM，并能专门处理「被物理切断」的跨页 / 跨栏段落。
- 理解「标题上下文」如何用 `first_paragraph`（全文首个标题）与 `recent_title_paragraph`（最近标题）两个快照跟踪，并注入提示词。
- 掌握翻译质量校验的三道关卡：**译文与原文相同**、**token 比例越界**、**Levenshtein 编辑距离过小**，以及任一关卡失败时的**逐段回退**机制。
- 解释 `high_level` 在何种条件下选择 `ILTranslatorLLMOnly`，以及运行期它在何种条件下退回 `ILTranslator`。

## 2. 前置知识

阅读本讲前，请确保已掌握以下概念（前序讲义已建立）：

- **IL 与段落**：midend 流水线把散字符聚合成 `PdfParagraph`，每个段落有 `unicode`、`pdf_paragraph_composition`、`box`、`layout_label` 等字段（见 u3-l1、u5-l3）。
- **占位符翻译**：`ILTranslator` 用 `{v1}` 公式占位符与 `<style id='N'>…</style>` 富文本占位符做 pre/post 翻译（见 u6-l2）。
- **翻译器服务**：`BaseTranslator` 是模板方法，`translate()` 负责「查缓存→限流→翻译→写缓存」，子类实现 `do_translate()`；`OpenAITranslator` 是唯一内置实现，调 OpenAI 兼容 API（见 u6-l1）。
- **优先级线程池**：`PriorityThreadPoolExecutor` 按小顶堆调度，`priority = 1048576 - paragraph_token_count`，长段先入场（见 u6-l2）。
- **stage**：`ILTranslatorLLMOnly` 与 `ILTranslator` 共用同一个 `stage_name = "Translate Paragraphs"`，二选一装配进流水线。

本讲新引入的关键术语：

- **LLM-only 模式**：当翻译引擎支持「原生 LLM 翻译」（即实现了 `do_llm_translate`）时，BabelDOC 用 `ILTranslatorLLMOnly` 把多段拼成一次结构化（JSON）请求，让 LLM 一次性返回多段译文。
- **跨页 / 跨栏段落**：PDF 版面把同一个逻辑段落拆成两段（例如正文排到页底、下一页顶继续；或分栏排版时左栏底接到右栏顶）。把它们放进同一次请求，LLM 才能在统一上下文里给出连贯译文。
- **标题上下文快照（TitleContextSnapshot）**：一份冻结的标题「摘要」，只记 `debug_id / unicode / layout_label`，避免持有可变段落对象。
- **回退（fallback）**：LLM-only 翻译质量不达标或异常时，退回 `ILTranslator` 的单段翻译路径重译。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py) | 本讲主角。LLM-only 翻译编排：跨页/跨栏打包、标题上下文、JSON 批量请求、质量校验与回退。 |
| [babeldoc/format/pdf/document_il/midend/il_translator.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py) | `ILTranslator`，既是 LLM-only 的「单段回退引擎」，也提供共享的 `pre_translate_paragraph` / `post_translate_paragraph` / 占位符构造逻辑。 |
| [babeldoc/format/pdf/high_level.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py) | 用 `translator_supports_llm()` 探测能力，并据此选择 `ILTranslatorLLMOnly` 或 `ILTranslator`。 |
| [babeldoc/format/pdf/translation_config.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py) | 定义 `TitleContextSnapshot` 与 `SharedContextCrossSplitPart`（跨分片共享的标题上下文与术语表容器）。 |
| [babeldoc/translator/translator.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py) | `BaseTranslator.llm_translate` / 抽象 `do_llm_translate`，以及 `OpenAITranslator.do_llm_translate` 的 JSON 模式实现。 |

## 4. 核心概念与源码讲解

### 4.1 跨页 / 跨栏段落处理

#### 4.1.1 概念说明

`ILTranslator`（u6-l2）的策略是「**一段一请求**」：每个 `PdfParagraph` 独立翻译，互不干扰。这对绝大多数段落没问题，但有一种情形会吃亏——**一个逻辑段落被 PDF 版面切成两段**：

- **跨页（cross-page）**：正文排到第 N 页底部没排完，第 N+1 页顶部接着排。`ParagraphFinder`（u5-l3）按页处理，于是同一句话被拆成「上一页最后一段」和「下一页第一段」两个 `PdfParagraph`。
- **跨栏（cross-column）**：双栏排版时，左栏底部接到右栏顶部，同样会在同一页内被切成相邻两段。

如果按「一段一请求」分别翻译，LLM 看不到另一半，译文容易在断点处不连贯。`ILTranslatorLLMOnly` 的做法是：**在常规逐段翻译之前，先把这些「物理上被切断」的成对段落识别出来，塞进同一次请求**，让 LLM 在统一上下文里翻译。注意：提示词**明确要求 LLM 仍按相同段落数返回、不得合并**（见 4.1.3 的结构规则），所以「打包」是为了**共享上下文与请求效率**，而非真的把两段并成一段。

> 直觉：`ILTranslator` 像「一个顾客点一道菜，厨师一道一道做」；`ILTranslatorLLMOnly` 像「一桌顾客把菜单一起递给厨师，厨师一次性做完端上来」。

#### 4.1.2 核心流程

`ILTranslatorLLMOnly.translate(docs)` 是主入口，它的处理顺序是**三趟扫描**，共用一个 `translated_ids` 集合（用 `id(paragraph)` 作键）防止同一段被翻译两次：

```
translate(docs):
  1. 初始化标题上下文（若尚未初始化）  → 见 4.2
  2. 统计待翻译段落数，打开 stage 进度条
  3. 开两个优先级线程池：executor（主批次）、executor2（回退）
  4. 第一趟 process_cross_page_paragraph(docs)：跨页配对
  5. 第二趟 逐页 process_cross_column_paragraph(page)：跨栏配对
  6. 第三趟 逐页 process_page(page)：剩余段落按 token/数量打包
  7. （debug 模式）写出 translate_tracking.json
```

三趟的**优先级**很关键：跨页、跨栏先「消费」掉配对段落，第三趟 `process_page` 只翻译还没被认领的段落。

- **跨页判定**：对每对相邻页 `(page[i], page[i+1])`，取当前页**最后一个正文段**与下一页**第一个正文段**，组成一个批次提交。
- **跨栏判定**：在同一页内，对相邻的两个正文段 `p1, p2`，若它们的 `box.y2` 之差大于 20 个单位，就认为它们是「跨栏被切」的，组成一个批次。
- **正文段判定**：`layout_label ∈ {"text", "plain text", "paragraph_hybrid"}`，并且通过基础过滤（有 `debug_id`、有 `unicode`、非 CID 段、长度达标、未被翻译过）。

每趟都用统一的优先级公式 `priority = 1048576 - total_token_count`（与 u6-l2 一致，长批次先入场）。

#### 4.1.3 源码精读

**主入口与三趟扫描**：[il_translator_llm_only.py:175-244](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L175-L244)。注意它开了**两个** `PriorityThreadPoolExecutor`：`executor` 跑 LLM-only 主批次，`executor2` 预留给回退任务（见 4.4）。三趟调用顺序就是 cross-page → cross-column → page。

**跨页配对**：[il_translator_llm_only.py:388-454](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L388-L454)。关键片段：

```python
# 取相邻页对
for i in range(len(docs.page) - 1):
    page_curr = docs.page[i]
    page_next = docs.page[i + 1]
    curr_body_paragraphs = self._filter_paragraphs(page_curr, translated_ids, require_body_text=True)
    next_body_paragraphs = self._filter_paragraphs(page_next, translated_ids, require_body_text=True)
    if not curr_body_paragraphs or not next_body_paragraphs:
        continue
    last_curr_paragraph = curr_body_paragraphs[-1]      # 当前页最后一个正文段
    first_next_paragraph = next_body_paragraphs[0]      # 下一页第一个正文段
    ...
    merged_font_map = {**curr_font_map, **next_font_map}  # 合并两页字体表
    cross_page_paragraphs = [last_curr_paragraph, first_next_paragraph]
    batch_paragraph = BatchParagraph(cross_page_paragraphs, [page_curr, page_next], tracker.new_cross_page())
    executor.submit(self.translate_paragraph, batch_paragraph, ...)
    translated_ids.add(id(last_curr_paragraph))
    translated_ids.add(id(first_next_paragraph))
```

因为两段分属不同页，字体表也不同，这里**合并两页的字体映射**（`merged_font_map`）后再交给占位符构造逻辑。

**跨栏配对**：[il_translator_llm_only.py:488-526](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L488-L526)。核心判定是 `box.y2` 的跳跃：

```python
# 安全检查 box 信息
if not (p1.box and p2.box and p1.box.y2 is not None and p2.box.y2 is not None):
    continue
if p2.box.y2 - p1.box.y2 <= 20:   # y2 差距不够大，不算跨栏
    continue
```

> 关于坐标：`box.y2` 是段落包围盒的上边界（IL 坐标系 y 向上）。同栏里相邻段落的 y2 差距通常较小（行距级别）；当差距 > 20 时，说明两段之间有大块空白（典型如左栏底→右栏顶的纵向跳变），故判为跨栏。

**「正文段」与基础过滤**：[il_translator_llm_only.py:259-334](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L259-L334)。`_is_body_text_paragraph` 只认三类 `layout_label`；`_should_translate_paragraph` 统一过滤掉无 id、无 unicode、CID 段、过短段、已翻译段。注意跨页/跨栏配对额外要求 `require_body_text=True`，**标题、图注等不参与跨段配对**——这避免把章节标题和正文错误地拼到一起。

**为什么打包但不合并——看提示词**：[il_translator_llm_only.py:40-95](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L40-L95)。`PROMPT_TEMPLATE` 的 Structure Rules 第 2 条明确写：

> Input paragraphs may be **sliced pieces of the same original paragraph**. → You MUST treat each input paragraph **as an independent, fixed unit**. → Do NOT merge paragraphs, split paragraphs, or move content between paragraphs.

也就是说，LLM 拿到的 JSON 数组里每个段落仍是独立单元，必须按原样数量返回；打包只是为了**让 LLM 同时看到被切断的两半**，从而译文连贯，且**省请求次数**。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码 + 对照 debug 输出，理解跨页/跨栏段落如何被配对进同一批次。

**操作步骤**：

1. 准备一份**多页、且正文跨页连续**的英文 PDF（例如一篇 arXiv 论文）。
2. 用 `--debug` 翻译：`babeldoc --openai --openai-api-key <KEY> --files paper.pdf --debug --output-dir out`。
3. 打开 `out` 工作目录下的 `translate_tracking.json`。该文件由 [il_translator_llm_only.py:246-254](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L246-L254) 写出，结构见 `DocumentTranslateTracker.to_json()`：顶层有 `cross_page`、`cross_column`、`page` 三个数组（[il_translator.py:180-188](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L180-L188)）。

**需要观察的现象**：

- `cross_page` 数组非空：每个元素含两个段落（`paragraph` 数组长度为 2），`input` 分别是上一页末段与下一页首段的文本，但 `multi_paragraph_id` 相同——证明它们被同一次请求翻译。
- `cross_column` 数组里某些项也是成对段落。
- `page` 数组里则不会再出现这些已被消费的段落。

**预期结果**：跨页/跨栏段落出现在 `cross_page` / `cross_column`，而不在 `page`；同一批次的多段共享一个 `multi_paragraph_id`。若 PDF 段落都不跨页跨栏，这两个数组为空，所有段落落入 `page`。

> 待本地验证：实际配对数量取决于具体 PDF 的版面；若手头没有合适 PDF，可只做源码阅读：在 [process_cross_page_paragraph](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L359-L454) 的 `executor.submit(...)` 前临时加一行 `print(f"cross-page pair: {[p.debug_id for p in cross_page_paragraphs]}")`，再对照 `translated_ids` 理解「先消费」语义。

#### 4.1.5 小练习与答案

**练习 1**：为什么跨页/跨栏配对只选「正文段」（`require_body_text=True`），而不选标题段？

**参考答案**：标题（`layout_label == "title"`）是独立的结构单元，通常不与正文连续；若把标题和正文强行配对，会让 LLM 误以为它们是同一逻辑段的两半。只对正文段配对，才能正确捕捉「正文排到页底、下一页继续」或「左栏底→右栏顶」的真实连续关系。

**练习 2**：跨栏判定用的是 `p2.box.y2 - p1.box.y2 > 20`。如果两个相邻正文段在**同一栏**内，这个差值大致是什么量级？为什么会小于 20？

**参考答案**：同一栏内相邻段落的 y2 差值约等于「行高 × 行数 + 段间距」，通常只有几到十几个单位，远小于 20；只有当两段之间出现「整栏高度的空白跳跃」（即换到另一栏的顶部）时，y2 才会大幅增加超过 20。因此 20 是区分「同栏相邻」与「跨栏」的经验阈值。

---

### 4.2 标题上下文跟踪

#### 4.2.1 概念说明

机器翻译有个老问题：**脱离上下文的单句翻译容易出错**。例如一句话里的 "it"、"this model"、某个缩写，不联系上下文就无法准确翻译。BabelDOC 在 LLM-only 模式下，给每次请求额外塞入**标题上下文**作为提示：

- **`first_paragraph`（全文首个标题）**：整篇文档第一个被识别为标题的段落，固定不变，给 LLM 一个「这篇文档在讲什么」的全局锚点。
- **`recent_title_paragraph`（最近标题）**：随着逐页处理不断更新，反映「当前正在翻译的内容属于哪个小节」，帮助 LLM 解析代词与术语。

这两个上下文都来自 `SharedContextCrossSplitPart`——一个**跨分片共享**的容器（见 u8-l2 分片翻译）。之所以要跨分片，是因为分片后每个 part 是独立翻译的，但标题上下文必须保持全文一致。

#### 4.2.2 核心流程

```
translate(docs) 启动时（仅当 first_paragraph 未设置）:
  title = find_title_paragraph(docs)        # 扫描全文找首个 layout_label=="title" 的段
  first_paragraph          = snapshot(title)  # 全文首个标题快照
  recent_title_paragraph   = snapshot(title)  # 初始等于首个标题

逐页 process_page(page) 时:
  for paragraph in page.pdf_paragraph:
      if paragraph.layout_label == "title":
          recent_title_paragraph = snapshot(paragraph)  # 遇到新标题就更新「最近标题」
      ...打包提交...

每次构造提示词 _build_llm_prompt(...):
  若 first_paragraph 存在  → "First title in full text: ..."
  若 recent 存在且 ≠ first → "The most recent title is: ..."
```

「快照」用 `TitleContextSnapshot`——一个 `frozen` 的、带 `slots` 的 dataclass，只保存 `debug_id / unicode / layout_label` 三个不可变字段。**不直接持有段落对象**，是因为段落对象会在流水线后续阶段被反复改写（比如 4.4 的回退会重置 `unicode`），快照能保证「当时记下的标题」不被污染。

#### 4.2.3 源码精读

**快照类型与共享容器**：[translation_config.py:27-54](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L27-L54)。

```python
@dataclass(frozen=True, slots=True)
class TitleContextSnapshot:
    debug_id: str | None
    unicode: str | None
    layout_label: str | None = None

class SharedContextCrossSplitPart:
    def __init__(self):
        self.first_paragraph: TitleContextSnapshot | None = None
        self.recent_title_paragraph: TitleContextSnapshot | None = None
        ...
    def snapshot_title_paragraph(self, paragraph) -> TitleContextSnapshot | None:
        if paragraph is None:
            return None
        return TitleContextSnapshot(
            debug_id=getattr(paragraph, "debug_id", None),
            unicode=getattr(paragraph, "unicode", None),
            layout_label=getattr(paragraph, "layout_label", None),
        )
```

**全文首个标题的初始化**：[il_translator_llm_only.py:180-192](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L180-L192)。`find_title_paragraph` 顺序扫描每页每段，返回第一个 `layout_label == "title"` 的段落（[il_translator_llm_only.py:159-173](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L159-L173)）。注意 `if not ... first_paragraph` 的守卫——分片翻译时第 0 个 part 已设置过，后续 part 直接复用，**保证全文首个标题一致**。

**最近标题随处理推进而更新**：[il_translator_llm_only.py:585-590](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L585-L590)。

```python
if paragraph.layout_label == "title":
    self.shared_context_cross_split_part.recent_title_paragraph = (
        self.shared_context_cross_split_part.snapshot_title_paragraph(paragraph)
    )
```

> 注意：只有 `process_page`（第三趟）会更新 `recent_title_paragraph`。第一、二趟（跨页/跨栏）发生在「逐页遍历」之前，用的是启动时初始化的值。这是一个有意为之的简化——跨页配对主要关注段间连续性，标题提示用初始快照已足够。

**注入提示词**：[il_translator_llm_only.py:910-936](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L910-L936)。生成 "## Contextual Hints for Better Translation" 区块：

```python
if title_paragraph:                       # first_paragraph
    contextual_lines.append(f"1. First title in full text: {title_paragraph.unicode}")
if local_title_paragraph:                 # recent_title_paragraph
    is_different_from_global = (local_title_paragraph.debug_id != title_paragraph.debug_id)
    if is_different_from_global:
        contextual_lines.append(f"2. The most recent title is: {local_title_paragraph.unicode}")
```

当「最近标题」与「全文首个标题」是同一个（`debug_id` 相同）时，只输出第一条，避免冗余。

#### 4.2.4 代码实践

**实践目标**：验证标题上下文确实被写进发给 LLM 的提示词。

**操作步骤**：

1. 在 [translate_paragraph](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L626-L884) 中，`final_input = self._build_llm_prompt(...)`（[第 700 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L700-L705)）之后，临时加一行 `logger.warning(final_input[:1500])` 把提示词前 1500 字打到日志。
2. 翻译一份**带清晰章节标题**的英文 PDF。
3. 观察日志。

**需要观察的现象**：

- 翻译第 1 节的段落时，提示词里出现 "First title in full text: <论文标题>"，但**没有** "The most recent title is"（因为此时最近标题 == 首个标题）。
- 翻译第 2 节及以后的段落时，开始出现 "The most recent title is: <第 2 节标题>"，且随着处理推进不断变化。

**预期结果**：`first_paragraph` 全程不变；`recent_title_paragraph` 在遇到新 `title` 段落后切换。**待本地验证**：具体提示词内容取决于 PDF 的标题识别结果（依赖 u5-l2 的版面分析给出 `layout_label == "title"`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `TitleContextSnapshot` 要设计成 `frozen=True`，而不是直接保存段落对象引用？

**参考答案**：流水线后续会改写段落对象（最典型的是 4.4 的回退会把 `paragraph.unicode` 重置并重译）。如果直接持有段落引用，记录下来的标题会跟着被污染。`frozen` 快照把当时的 `debug_id / unicode / layout_label` 拷贝成不可变值，彻底隔离了后续修改。

**练习 2**：分片翻译时（`--max-pages-per-part`），第 2 个分片还会调用 `find_title_paragraph` 重新扫描全文吗？为什么？

**参考答案**：不会。`translate()` 用 `if not self.translation_config.shared_context_cross_split_part.first_paragraph` 守卫，而 `first_paragraph` 存在跨分片共享的 `SharedContextCrossSplitPart` 上，第 0 个分片已经设置过，第 2 个分片直接复用。这保证了「全文首个标题」在所有分片里完全一致。

---

### 4.3 翻译质量校验

#### 4.3.1 概念说明

LLM 不是确定性的——即便 `temperature=0`，也可能返回：原文照搬（没翻译）、译文长度异常（漏译或啰嗦重复）、或与原文几乎一字不差（实际没动）。`ILTranslatorLLMOnly` 在把译文写回段落前，对**每个段落**做三道质量关卡，任一关失败就**丢弃这段译文、改走回退**（见 4.4）：

1. **「译文与原文相同」关卡**：清理掉 20 个以上连续标点的病态输出后，若译文仍与原文逐字相同，且原文较长（token > 10），判定为「没翻译」。
2. **「token 比例」关卡**：译文 token 数与原文 token 数之比必须落在合理区间，否则判定为「过长或过短」（漏译 / 重复啰嗦）。
3. **「Levenshtein 编辑距离」关卡**：译文与原文的**字符级编辑距离**过小且原文较长时，判定为「几乎没改动」。

> 名词解释：**Levenshtein 编辑距离**指把一个字符串变成另一个所需的最少「单字符编辑（插入 / 删除 / 替换）」次数。例如 "kitten"→"sitting" 的距离是 3（k→s, e→i, +g）。距离越小，两串越相似；距离为 0 即完全相同。本讲用的是 `Levenshtein` 这个第三方库的 `distance()` 函数。

这三关都受配置项 `disable_same_text_fallback` 影响：开启后会**跳过**第 1、3 关（但仍保留第 2 关 token 比例检查），适合某些译文确实就该和原文相近的语对。

#### 4.3.2 核心流程

对每个段落 `id_`，拿到译文 `output` 后：

```
translated_text = 清理连续标点(output)
trimed_input    = 清理连续标点(原文)

# 关卡 1：译文 == 原文 且 原文够长 且 未禁用 → 回退
same_as_input = (trimed_input == translated_text)
if same_as_input and input_token_count > 10 and not disable_same_text_fallback:
    → 标记错误 "same as input"，触发回退

# 关卡 2：token 比例越界 → 回退
ratio = output_token_count / input_token_count
if not (0.3 < ratio < 3):
    → 标记错误 "too long or too short"，触发回退

# 关卡 3：编辑距离过小 且 原文够长 且 未禁用 → 回退
if not disable_same_text_fallback:
    edit_distance = Levenshtein.distance(原文, 译文)
    if edit_distance < 5 and input_token_count > 20:
        → 标记错误 "edit distance too small"，触发回退

# 三关全过 → 写回段落
post_translate_paragraph(...)
```

token 比例关卡用公式表达：

\[
r = \frac{n_{\text{out}}}{n_{\text{in}}}, \quad \text{通过当且仅当} \;\; 0.3 < r < 3
\]

其中 \(n_{\text{in}}\)、\(n_{\text{out}}\) 分别是原文、译文用 tiktoken（`gpt-4o` 编码）估算的 token 数。这个区间是经验值：中英互译时 token 比例通常在 0.5~2 之间，留出 0.3~3 的宽容带；超出则几乎可以肯定是漏译或重复。

#### 4.3.3 源码精读

三道关卡都在 `translate_paragraph` 的逐段循环里，[il_translator_llm_only.py:753-812](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L753-L812)：

```python
# 清理 LLM 偶尔产生的"标点瀑布"（20 个以上连续句号/省略号/逗号压成一个句号）
translated_text = re.sub(r"[. 。…，]{20,}", ".", output)
trimed_input = re.sub(r"[. 。…，]{20,}", ".", input_unicode)

input_token_count  = self.calc_token_count(trimed_input)
output_token_count = self.calc_token_count(output_unicode)

# 关卡 1：与原文相同
same_as_input = trimed_input == output_unicode
if (same_as_input and input_token_count > 10
        and not self.translation_config.disable_same_text_fallback):
    llm_translate_tracker.set_error_message("Translation result is the same as input, fallback.")
    llm_translate_tracker.set_placeholder_full_match()
    continue   # should_fallback 仍为 True → finally 里回退

# 关卡 2：token 比例
if not (0.3 < output_token_count / input_token_count < 3):
    llm_translate_tracker.set_error_message("Translation result is too long or too short. ...")
    continue

# 关卡 3：编辑距离
if not self.translation_config.disable_same_text_fallback:
    edit_distance = Levenshtein.distance(input_unicode, output_unicode)
    if edit_distance < 5 and input_token_count > 20:
        llm_translate_tracker.set_error_message("Translation result edit distance is too small. ...")
        continue

# 三关全过 → 写回
self.il_translator.post_translate_paragraph(inputs[id_][2], inputs[id_][3], translate_input, translated_text)
should_fallback = False
```

几个要点：

- **`continue` 的作用**：关卡失败时 `continue` 跳过 `post_translate_paragraph`，而 `should_fallback` 保持初始值 `True`（[第 740 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L740)），于是在 `finally` 块里触发回退（见 4.4）。
- **「原文较短就放行」的设计**：关卡 1 要求 `input_token_count > 10`、关卡 3 要求 `> 20`。对于很短的原文（如单词、短语），译文与原文相近甚至相同是正常的（专有名词、公式标签），不该误判为失败，所以设了 token 下限。
- **tracker 记录**：每次失败都调 `llm_translate_tracker.set_error_message(...)` 记下原因，并 `set_placeholder_full_match()`，最终写进 `translate_tracking.json` 供排查。
- **除零保护**：`input_token_count` 理论上 ≥ 1（能进到这里说明原文非空且通过了 `min_text_length`），但若 `calc_token_count` 因异常返回 0，关卡 2 的除法会抛 `ZeroDivisionError`——它会被外层 `except Exception` 兜住，整个批次回退（见 4.4 的「整批回退」）。

#### 4.3.4 代码实践

**实践目标**：用最小 Python 片段复现三道关卡，直观理解阈值。

**操作步骤**（示例代码，不依赖完整流水线）：

```python
# 示例代码：仅供理解阈值，不是项目原有代码
import tiktoken, Levenshtein, re
enc = tiktoken.encoding_for_model("gpt-4o")
def tok(s): return len(enc.encode(s, disallowed_special=()))

def check(src, out, disable_same_text_fallback=False):
    out = re.sub(r"[. 。…，]{20,}", ".", out)
    src_t = re.sub(r"[. 。…，]{20,}", ".", src)
    ni, no = tok(src_t), tok(out)
    if src_t == out and ni > 10 and not disable_same_text_fallback:
        return "FAIL-1 same as input"
    if not (0.3 < (no / ni) < 3):
        return f"FAIL-2 ratio={no/ni:.2f}"
    if not disable_same_text_fallback:
        d = Levenshtein.distance(src, out)
        if d < 5 and ni > 20:
            return f"FAIL-3 edit_distance={d}"
    return "PASS"

print(check("This is a normal English sentence about machine learning models.",
            "这是一句关于机器学习模型的普通英文句子。"))   # 预期 PASS
print(check("Transformer architecture.", "Transformer architecture."))  # 短原文，预期 PASS
print(check("Neural networks are widely used in modern applications today indeed.",
            "Neural networks are widely used in modern applications today indeed."))  # 预期 FAIL-1
```

**需要观察的现象**：

- 正常中英互译 → PASS。
- 短原文即使照搬 → PASS（关卡 1 的 token 下限放行）。
- 长原文照搬 → FAIL-1。
- 译文极短（如漏译成两三个字）→ FAIL-2，ratio 远小于 0.3。
- 译文只是改了原文一两个字符（编辑距离 < 5）且原文长 → FAIL-3。

**预期结果**：三个 FAIL 分支分别对应「没翻译 / 漏译啰嗦 / 几乎没动」。**待本地验证**：需安装 `tiktoken` 与 `Levenshtein`（BabelDOC 已依赖）。

#### 4.3.5 小练习与答案

**练习 1**：关卡 2 的比例区间是 `(0.3, 3)`。假设原文 100 token，译文分别 25、250、400 token，哪些会触发回退？

**参考答案**：25/100=0.25 < 0.3 → 触发（过短，疑似漏译）；250/100=2.5 ∈ (0.3,3) → 通过；400/100=4.0 > 3 → 触发（过长，疑似重复啰嗦）。

**练习 2**：为什么关卡 3 要同时要求 `edit_distance < 5` **和** `input_token_count > 20` 两个条件？

**参考答案**：单独看编辑距离会误伤短文本——两三个词的句子，译文哪怕正常翻译，与原文的字符级编辑距离也很容易小于 5（例如 "AI"→"人工智能" 距离其实不小，但短数字/符号串改动小）。加上「原文 token > 20」的前提，只在**原文足够长**时才信任「编辑距离过小 = 没翻译」的判断，避免对短段落误判。

---

### 4.4 回退到 ILTranslator

#### 4.4.1 概念说明

「回退（fallback）」是 LLM-only 模式的安全网：当某段的 LLM-only 译文**质量不达标**或**处理过程出错**时，不直接采用，而是退回 `ILTranslator` 的「单段翻译」路径重新翻译该段。回退分两个层次：

- **逐段回退**：批次里某一段通不过质量关卡（4.3），或该段处理抛异常，只回退**这一段**，同批次其他正常段不受影响。
- **整批回退**：批次级别的错误——例如 LLM 返回的不是合法 JSON、JSON 解析失败、返回数组长度与输入段数不一致——会让**整个批次所有段**一起回退。

回退目标是一个**内嵌的 `ILTranslator` 实例**，它在 `ILTranslatorLLMOnly.__init__` 里被创建，并打上 `use_as_fallback = True` 标记。这个标记让回退路径在翻译前先**从 composition 重建 `unicode`**，保证用的是原始文本而非可能被改动的中间状态。

#### 4.4.2 核心流程

```
ILTranslatorLLMOnly.__init__:
  self.il_translator = ILTranslator(...)          # 内嵌回退引擎
  self.il_translator.use_as_fallback = True       # 标记为回退模式
  探测 do_llm_translate(None)，不支持就 raise ValueError

translate_paragraph 批次处理:
  try:
      调 llm_translate 拿到 JSON 输出
      解析、逐段做质量校验（4.3）
      ── 某段失败：should_fallback 保持 True
      ── 某段通过：post_translate_paragraph 写回，should_fallback = False
      finally:
          if should_fallback:                      # 逐段回退
              还原 paragraph.unicode
              把 self.il_translator.translate_paragraph 提交到 executor2
  except Exception:                                # 整批回退
      该批次所有段都提交到 self.il_translator.translate_paragraph

ILTranslator.translate_paragraph（use_as_fallback=True 时）:
  paragraph.unicode = get_paragraph_unicode(paragraph)  # 从 composition 重建
  走标准 pre/translate/post 流程（llm_translate 或 translate）
```

#### 4.4.3 源码精读

**装配回退引擎 + 能力探测**：[il_translator_llm_only.py:138-151](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L138-L151)。

```python
self.il_translator = ILTranslator(
    translate_engine=translate_engine,
    translation_config=translation_config,
    tokenizer=self.tokenizer,
)
self.il_translator.use_as_fallback = True
try:
    self.translate_engine.do_llm_translate(None)   # 探测是否支持原生 LLM 翻译
except NotImplementedError as e:
    raise ValueError("LLM translator not supported") from e
```

> 注意：`OpenAITranslator.do_llm_translate` 在 `text is None` 时直接 `return None`（[translator.py:297-299](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L297-L299)），所以这个探测调用不会真的发请求，只会因「方法存在」而通过，或因「未实现」抛 `NotImplementedError`。

**逐段回退（finally 块）**：[il_translator_llm_only.py:823-848](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L823-L848)。

```python
finally:
    self.total_count += 1
    if should_fallback:
        self.fallback_count += 1
        inputs[id_][4].set_fallback_to_translate()
        ...                                          # 还原 paragraph.unicode = paragraph_unicodes[id_]
        executor.submit(                             # 注意：这里提交给第二线程池 executor2
            self.il_translator.translate_paragraph,
            inputs[id_][2], batch_paragraph.pages[id_], pbar, inputs[id_][3],
            page_font_map, xobj_font_map,
            priority=1048576 - paragraph_token_count,
            paragraph_token_count=paragraph_token_count,
            title_paragraph=title_paragraph,
            local_title_paragraph=local_title_paragraph,
        )
    else:
        self.ok_count += 1
```

**整批回退（外层 except）**：[il_translator_llm_only.py:852-884](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L852-L884)。当 `_clean_json_output` + `json.loads` 失败，或 `len(translation_results) != len(inputs)`（[第 734-737 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L734-L737) 抛异常）时，整批所有段都改走 `self.il_translator.translate_paragraph`。

**回退引擎的 `use_as_fallback` 行为**：[il_translator.py:1230-1232](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L1230-L1232)。

```python
if self.use_as_fallback:
    # il translator llm only modifies unicode in some situations
    paragraph.unicode = get_paragraph_unicode(paragraph)
```

`get_paragraph_unicode` 从 `pdf_paragraph_composition` 重新拼接出段落文本（[layout_helper.py:200-212](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/layout_helper.py#L200-L212)）。这一步是必须的：LLM-only 的失败尝试可能已经动过 `unicode`，回退前要还原成「干净的原文」。

**最终统计**：[il_translator_llm_only.py:255-257](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L255-L257) 打印 `Total / Successful / Fallback` 三项计数，是排查「回退率」的第一手信息。

#### 4.4.4 代码实践（本讲核心实践任务）

**实践目标**：比较 `translator_supports_llm` 为 True / False 两种情况下 `high_level` 选择的翻译器，并解释 `ILTranslatorLLMOnly` 在何种条件下回退到 `ILTranslator`。

**操作步骤（源码阅读型）**：

1. 读选择逻辑 [high_level.py:986-1007](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L986-L1007)：

   ```python
   support_llm_translate = translator_supports_llm(translate_engine)
   ...
   if not translation_config.skip_translation:
       if support_llm_translate:
           il_translator = ILTranslatorLLMOnly(translate_engine, translation_config)
       else:
           il_translator = ILTranslator(translate_engine, translation_config)
       il_translator.translate(docs)
   ```

2. 读能力探测 [high_level.py:246-256](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L246-L256)：`translator_supports_llm` 调 `translator.do_llm_translate(None)`，捕获 `NotImplementedError` 返回 `False`。

3. 整理回退条件：通读 [translate_paragraph](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L626-L884) 的两处回退（逐段 `finally`、整批 `except`），列出触发条件。

**需要观察的现象 / 结论（预期结果）**：

- **选哪个翻译器**：内置 `OpenAITranslator` 实现了 `do_llm_translate` → `support_llm_translate=True` → 用 `ILTranslatorLLMOnly`。若有人接入一个只实现 `do_translate`、未实现 `do_llm_translate` 的自定义翻译器（基类抽象方法 [translator.py:167-174](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L167-L174) 会抛 `NotImplementedError`）→ `support_llm_translate=False` → 用 `ILTranslator`。另外 `--skip-translation` 时两者都不跑。
- **逐段回退条件**（任一即回退该段）：① 译文与原文相同且原文 token > 10（未禁用 same-text-fallback）；② token 比例不在 (0.3, 3)；③ 编辑距离 < 5 且原文 token > 20（未禁用）；④ 译文不是字符串；⑤ 该段处理过程中抛异常。
- **整批回退条件**：① LLM 输出经 `_clean_json_output` 后仍非合法 JSON；② 解析得到的译文条数与输入段数不一致；③ 批次处理中抛出未被内层 `try` 捕获的异常。

> 待本地验证（可选运行型）：用一个返回不稳定 JSON 的 mock 翻译引擎跑 `ILTranslatorLLMOnly`，观察日志里 `Total / Successful / Fallback` 计数与 `translate_tracking.json` 中各段 `llm_translate_trackers` 的 `error_message` / `fallback_to_translate` 字段。

#### 4.4.5 小练习与答案

**练习 1**：`ILTranslatorLLMOnly` 内部已经持有一个 `ILTranslator` 实例用于回退。那当 `support_llm_translate=False` 时，`high_level` 为什么不也用 `ILTranslatorLLMOnly`（反正它内部有 `ILTranslator`）？

**参考答案**：`ILTranslatorLLMOnly.__init__` 第一步就探测 `do_llm_translate(None)`，不支持会直接 `raise ValueError("LLM translator not supported")`（[第 144-147 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L144-L147)），根本构造不出来。它的整套逻辑（JSON 批量请求、质量校验）都依赖原生 LLM 翻译能力，没有这个能力就该直接用 `ILTranslator` 的单段路径。

**练习 2**：逐段回退和整批回退，分别把任务提交到哪个线程池？为什么要用两个池？

**参考答案**：两者都把回退任务提交给**第二个** `PriorityThreadPoolExecutor`（代码里叫 `executor2`，见 [第 215-216 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py#L215-L216) 与 finally 里的 `executor.submit`，注意那里传入的 `executor` 形参实际绑定的是 `executor2`）。主批次（`executor`）跑 LLM-only 多段请求，回退任务（`executor2`）跑单段 `ILTranslator` 请求，分池隔离避免两类任务互相阻塞；两个池共享同一个全局漏桶限流器与翻译缓存（u6-l1）。

---

## 5. 综合实践

**任务**：用一份多页英文论文 PDF，端到端观察 `ILTranslatorLLMOnly` 的「打包 → 校验 → 回退」全过程，并计算回退率。

**步骤**：

1. `babeldoc --openai --openai-api-key <KEY> --files paper.pdf --debug --output-dir out`。
2. 打开 `out/translate_tracking.json`，按顶层 `cross_page` / `cross_column` / `page` 三个数组分类统计：
   - 有多少段落是跨页配对、跨栏配对、普通逐段？（看每条记录的 `multi_paragraph_id` 分组）
   - 在所有 `llm_translate_trackers` 里，统计 `fallback_to_translate=true` 的段落数，以及各自的 `error_message`（"same as input" / "too long or too short" / "edit distance too small" / 其它异常）。
3. 对照日志末尾的 `Translation completed. Total: X, Successful: Y, Fallback: Z`，验证 `Z` 与上一步统计的回退段落数一致。
4. **分析**：回退率（Z / X）高说明什么？哪些段落最容易回退？（通常是含大量公式占位符、或极短的段落。）

**预期结果**：你应能用一句话说清——「`ILTranslatorLLMOnly` 把跨页跨栏与同页段落分别打包成 JSON 请求送 LLM，对返回的每段做 token 比例与编辑距离校验，不达标或解析失败的段落回退到内嵌 `ILTranslator` 单段重译」。**待本地验证**：回退率与具体 PDF、LLM 服务质量强相关。

## 6. 本讲小结

- `ILTranslatorLLMOnly` 是 LLM-only 模式的翻译编排器，与 `ILTranslator` 共用 stage_name `"Translate Paragraphs"`，二选一装配，由 `high_level` 据 `translator_supports_llm()` 探测结果决定。
- 它把段落**三趟扫描**提交：先跨页配对（相邻页末段+首段）、再跨栏配对（同页 `box.y2` 差 > 20）、最后剩余段落按 token>200 或 >5 段打包；三趟共用 `translated_ids` 防重复。
- 每次请求是一个 JSON 数组（多段），提示词要求 LLM **按原段落数返回、不得合并**；打包的目的是共享上下文与省请求，而非真正合并段落。
- **标题上下文**用 `first_paragraph`（全文首个标题，固定）与 `recent_title_paragraph`（逐页遇到 title 即更新）两个 `TitleContextSnapshot` 快照跟踪，注入 "Contextual Hints"，跨分片共享。
- **质量校验三关**：译文=原文（token>10）、token 比例 ∉ (0.3,3)、Levenshtein 编辑距离<5（token>20）；后两关的 token 下限避免误伤短文本，第 1、3 关受 `disable_same_text_fallback` 控制。
- **回退分两层**：逐段失败走 `finally` 单段回退、整批失败（JSON 非法 / 条数不符）走外层 `except` 整批回退；回退目标是内嵌的 `ILTranslator`（`use_as_fallback=True`，先从 composition 重建 `unicode`），任务投到第二个线程池。

## 7. 下一步学习建议

- **u6-l4 自动术语抽取**：`AutomaticTermExtractor` 同样依赖 `translator_supports_llm`，且把抽取的术语写进 `SharedContextCrossSplitPart`，本讲的 `_cached_glossaries` 与 glossary 提示块正是消费它的产物。
- **u6-l5 术语表系统**：深入 `_build_llm_prompt` 里 `get_active_entries_for_text` 的匹配机制，理解术语如何按批文本动态注入。
- **u8-l2 分片翻译**：理解 `SharedContextCrossSplitPart` 为何必须跨分片共享——本讲的标题上下文与术语表都依赖它。
- **源码延伸阅读**：对比 [il_translator.py 的 ILTranslator.translate_paragraph](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L1214-L1278) 与本讲的 LLM-only 版本，体会「单段 vs 多段批处理」两套设计在 pre/post 翻译、占位符处理上的复用与差异。
