# IL 翻译编排：占位符、批处理与线程池

## 1. 本讲目标

本讲精读 midend 流水线中最核心、也最耗时的阶段 `ILTranslator`（`stage_name = "Translate Paragraphs"`，在 `TRANSLATE_STAGES` 中权重高达 46.96，是整条流水线调 LLM 的真正瓶颈）。学完本讲你应当能够：

1. 说清 **公式占位符 `{vN}`** 与 **富文本占位符 `<style id='N'>…</style>`** 是怎么被构造、又怎么在翻译后被还原回 `PdfParagraphComposition` 的。
2. 理解 `ILTranslator` 是如何「**一段一请求**」地把段落提交进线程池并发翻译的，以及哪些段落会被前置过滤跳过。
3. 看懂 `PriorityThreadPoolExecutor` 的优先级公式 `priority = 1048576 - paragraph_token_count`，并能推出「**长段优先入场**」这一与直觉相反的结论。
4. 把限流（漏桶）、本地翻译缓存、token 统计与线程池这几条线索串起来，解释它们的协同关系。

本讲承接 u5-l3（段落识别，产出 `pdf_paragraph`）与 u6-l1（翻译器服务 `BaseTranslator`/`OpenAITranslator` 与缓存）。`ILTranslator` 正是把「结构化的 IL 段落」和「面向纯文本的翻译器」连接起来的那一层编排代码。

## 2. 前置知识

- **IL 段落结构**（u3-l1、u5-l3）：一个 `PdfParagraph` 有一串 `pdf_paragraph_composition`，每个 composition 是「五选一」的富文本片段：`pdf_line`（带逐字 box 的行）、`pdf_formula`（公式，**不翻译**）、`pdf_same_style_characters`（同样式字符组，可带特殊样式如粗体）、`pdf_character`（单字）、`pdf_same_style_unicode_characters`（同样式纯文本，译文就用它承载）。
- **翻译器服务**（u6-l1）：`BaseTranslator.translate()` / `llm_translate()` 是模板方法，内部先查缓存、再过限流 `_translate_rate_limiter.wait()`、再调子类的 `do_translate` / `do_llm_translate`。OpenAI 兼容服务固定 `temperature=0`。
- **为什么需要占位符**：LLM 翻译的是「纯字符串」，但 IL 段落里夹杂着不能翻译的公式、以及需要保留样式的粗体/斜体片段。直接把整段 unicode 丢给 LLM，它会把公式符号翻乱、把样式信息弄丢。占位符的作用是：**翻译前**把「不可翻译/带样式」的部分替换成 LLM 不会动它的中性标记，**翻译后**再根据标记把原始 IL 对象填回去。
- **堆（heap）方向**：Python `heapq` 是**小顶堆**，`heappop` 取出**最小**的元素。本讲的优先级线程池正是基于它，这一点对理解「谁先翻译」至关重要。

> 名词约定：本讲的「**占位符**」特指 ILTranslator 自己注入的两类标记；「**placeholder-like token**」指原文里本就存在、形似占位符的串（LLM 可能会幻觉出新的同类标记，需要清理）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `babeldoc/format/pdf/document_il/midend/il_translator.py` | 本讲主角。`ILTranslator` 类，负责段落级 pre/post 翻译编排。 |
| `babeldoc/utils/priority_thread_pool_executor.py` | 带「优先级队列」的线程池 `PriorityThreadPoolExecutor`，决定哪段先翻译。 |
| `babeldoc/translator/translator.py` | 翻译器服务。提供占位符字符串的生成方法（`get_formular_placeholder` 等）、漏桶限流 `RateLimiter`、缓存与 token 统计。 |
| `babeldoc/format/pdf/high_level.py` | 装配点。`_do_translate_single` 里根据 `translator_supports_llm` 二选一地实例化 `ILTranslator` 或 `ILTranslatorLLMOnly` 并调用 `il_translator.translate(docs)`。 |
| `babeldoc/format/pdf/translation_config.py` | 配置中心。`pool_max_workers`、`min_text_length`、`disable_rich_text_translate` 等参数都从此读取。 |
| `babeldoc/format/pdf/document_il/il_version_1.py` | IL 数据模型。`PdfParagraphComposition` 等 dataclass 的字段定义。 |

---

## 4. 核心概念与源码讲解

### 4.1 占位符系统：pre/post translate 的构造与还原

#### 4.1.1 概念说明

`ILTranslator` 把「翻译一个段落」拆成对称的两步：

- **pre_translate（翻译前）**：遍历段落的 `pdf_paragraph_composition`，把每个 **公式** 替换成一个公式占位符、把每个 **样式与基准样式不同的字符组**（例如粗体）用「左占位符 + 原文 + 右占位符」包起来。最终拼成一段「大部分可翻译、少数标记不动」的纯文本，交给 LLM。
- **post_translate（翻译后）**：拿到 LLM 译文，用正则在译文里把占位符「定位」回来，逐段切分成新的 `pdf_paragraph_composition` 列表：命中公式占位符处填回原 `PdfFormula` 对象，命中富文本占位符处还原为带原样式的片段，其余文本放进 `pdf_same_style_unicode_characters`。

占位符字符串本身由翻译器服务提供（不同翻译后端可以用不同标记），`ILTranslator` 只负责「注入 + 还原」的编排逻辑。OpenAI 兼容后端用的是：

| 类型 | 占位符字符串 | 正则（宽松，容忍空格） |
| --- | --- | --- |
| 公式 | `{v1}`、`{v2}`… | `\{\s*v\s*1\s*\}` |
| 富文本左 | `<style id='1'>` | `<\s*style\s*id\s*=\s*'\s*1\s*'\s*>` |
| 富文本右 | `</style>` | `<\s*\/\s*style\s*>` |

> 注意：本讲规格里把富文本占位符简称为 `{style1}`，但 **真实代码用的是 XML 风格的 `<style id='N'>…</style>`**。`{v1}` 是公式占位符的真实形态。下面一律以源码为准。

#### 4.1.2 核心流程

```
段落 pdf_paragraph_composition = [文本, 公式F1, 粗体B, 文本]
                              │
            ┌─────────────────┴──────────────────┐
            │  pre_translate (get_translate_input) │
            └─────────────────┬──────────────────┘
                              ▼
   拼接字符串：  "引言{v1}<style id='1'>重点</style>结论"
   记录 placeholders = [FormulaPlaceholder(v1→F1), RichTextPlaceholder(1→B)]
                              │
                              ▼  交给 LLM 翻译
              "Introduction {v1} <style id='1'>key</style> conclusion"
                              │
            ┌─────────────────┴──────────────────┐
            │  post_translate (parse_translate_output) │
            └─────────────────┬──────────────────┘
                              ▼
   新 composition 列表：
     [PdfSameStyleUnicodeCharacters("Introduction"),
      PdfFormula(F1),                       ← 原公式对象原样填回
      PdfSameStyleCharacters(B 的原字符),    ← 命中且字符未变时还原原对象
      PdfSameStyleUnicodeCharacters("conclusion")]
```

三类细节需要记住：

1. **占位符编号自增且防碰撞**：公式占位符占用 1 个 id（`placeholder_id + 1`），富文本占位符左右共用、占用 2 个 id（`placeholder_id + 2`）。若生成的占位符正则**恰好已在原文中出现**，就递归换下一个 id，避免与原文里已有的同类标记混淆。
2. **纯公式段不翻译**：整段只有一个 composition 且是公式时，`pre_translate` 直接返回 `None` 跳过。
3. **占位符过多则放弃富文本**：单个段落的占位符超过 40 个时，会递归地以 `disable_rich_text_translate=True` 重做，不再为样式建占位符（防止 prompt 被标记淹没）。

#### 4.1.3 源码精读

占位符字符串来自翻译器服务，OpenAI 后端覆盖了基类的默认实现：

[translator.py:360-371](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L360-L371) —— `OpenAITranslator` 返回的占位符是 `(字符串, 宽松正则)` 二元组，正则用于之后「在译文里把占位符认回来」。

[il_translator.py:519-533](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L519-L533) —— `create_formula_placeholder`：取占位符，若其正则已能匹配 `paragraph.unicode`（说明原文里已有同形标记）则 `formula_id + 1` 重试。

[il_translator.py:575-734](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L575-L734) —— `get_translate_input`：pre 的核心。关键片段：

```python
elif composition.pdf_formula:
    formula_placeholder = self.create_formula_placeholder(...)   # 公式：占 1 个 id
    placeholders.append(formula_placeholder)
    placeholder_id = formula_placeholder.id + 1
    chars.extend(formula_placeholder.placeholder)                # 把 "{v1}" 拼进待翻文本
...
elif composition.pdf_same_style_characters:
    if disable_rich_text_translate:
        chars.extend(...pdf_character); continue                 # 禁用富文本：直接铺字符
    # 判断该字符组样式是否与段落基准样式「足够一致」
    if is_same_style(...) or is_same_style_except_size(...) or (同字体映射):
        chars.extend(...pdf_character); continue                 # 一致则无需占位符
    placeholder = self.create_rich_text_placeholder(...)         # 不一致：左+右占位符
    placeholder_id = placeholder.id + 2                          # 样式占 2 个 id
    chars.append(placeholder.left_placeholder)
    chars.extend(composition.pdf_same_style_characters.pdf_character)
    chars.append(placeholder.right_placeholder)
```

[il_translator.py:724-729](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L724-L729) —— 占位符超 40 个则递归禁用富文本重做。

[il_translator.py:771-952](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L771-L952) —— `parse_translate_output`：post 的核心。用 `re.finditer(combined_pattern, output)` 在译文里逐处切分：占位符之间的文本 → `PdfSameStyleUnicodeCharacters`；命中公式标记 → 填回原 `PdfFormula`；命中富文本标记 → 还原 `PdfSameStyleCharacters`（若内部字符未变）或译文片段。其中 `remove_placeholder` 还会清理 LLM 幻觉出的多余同类标记（仅保留「原文已有 + 我们注入」的合法标记）。

[il_translator.py:954-989](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L954-L989) / [il_translator.py:991-1019](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L991-L1019) —— `pre_translate_paragraph` / `post_translate_paragraph`：pre 还会跳过竖排段落、跳过文本短于 `min_text_length`（默认 5）的段落；post 把切分结果写回 `paragraph.pdf_paragraph_composition`。

#### 4.1.4 代码实践

**目标**：手工模拟一次 pre/post，确认你理解了占位符的形态与还原结果。

设一段落的 composition 依次为：

1. 文本 `"我们得到 "`（基准样式）
2. 公式 `F1`（`E=mc²`）
3. 粗体字符组 `"重要结论"`（样式≠基准，假设编号得 1）
4. 文本 `"。"`

**操作步骤**：

1. 按 `get_translate_input` 的规则拼接 pre 文本：公式占 `{v1}`、粗体包 `<style id='1'>重要结论</style>`。
2. 假装 LLM 把它翻成英文：`"We obtain {v1}<style id='1'>key result</style>."`。
3. 用 `parse_translate_output` 的切分规则，列出还原后的 composition 序列。

**预期结果（pre 文本）**：

```
我们得到 {v1}<style id='1'>重要结论</style>。
```

**预期结果（post 还原后的 composition）**：

| 序号 | composition 字段 | 内容 |
| --- | --- | --- |
| 1 | `pdf_same_style_unicode_characters` | `"We obtain "` |
| 2 | `pdf_formula` | 原 `F1` 对象（`E=mc²`，不翻译、原样填回） |
| 3 | `pdf_same_style_unicode_characters` | `"key result"`（粗体内部被翻译了，字符变了，故落到 unicode 变体、沿用粗体样式 `placeholder.composition.pdf_style`） |
| 4 | `pdf_same_style_unicode_characters` | `"."` |

> 注意第 3 项：因为译文 `"key result"` 与原字符 `"重要结论"` 不同，[il_translator.py:913-936](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L913-L936) 会落到 `pdf_same_style_unicode_characters` 而非复用原 `pdf_same_style_characters`。本结果未经本地实跑，属「源码阅读型推导」。

#### 4.1.5 小练习与答案

**练习 1**：为什么公式占位符用 `{vN}` 而富文本用 `<style>` 这种 XML 标签？请从「LLM 是否容易原样保留」的角度回答。

> **答案**：`<style>…</style>` 是成对标签，LLM 对 HTML/XML 标签有较强「保持结构不变」的先验（prompt 里也明确要求 `Keep all tags unchanged`），适合包裹「需要翻译但保样式」的片段；`{vN}` 紧凑且像占位符，LLM 更倾向原样保留，适合「完全不翻译」的公式。两类标记形态不同，也便于还原时用同一套正则区分。

**练习 2**：若某段落的占位符数量达到 50 个，会发生什么？

> **答案**：触发 [il_translator.py:724-729](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L724-L729) 的阈值（>40），打印 warning 并以 `disable_rich_text_translate=True` 递归重做该段，不再为样式生成富文本占位符。

---

### 4.2 段落批处理：过滤、并发与「一段一请求」

#### 4.2.1 概念说明

先把一个常见的误解澄清：**`ILTranslator` 并不把多个段落打包成一个 LLM 请求**。它对每个段落各发一次翻译请求（pre 拼一串文本 → 一次 `translate`/`llm_translate` → post 还原）。那种「跨段、跨栏、跨页合并成一个大 prompt」的批处理，是下一讲 `ILTranslatorLLMOnly`（u6-l3）的职责。

`ILTranslator` 的「批处理」体现在 **并发编排** 层面：

1. **前置过滤**：先把明显不需要翻译的段落剔除，根本不提交给线程池。
2. **并发提交**：剩余段落以「一段一个任务」的形式丢进 `PriorityThreadPoolExecutor`，最多 `pool_max_workers` 段同时在飞。
3. **优先级排队**：任务进队时带上 `priority`，决定谁先被空闲线程领取（见 4.3）。

#### 4.2.2 核心流程

```
对 docs 的每一页 page：
    对该页每一个 pdf_paragraph：
        ① 过滤（不提交）：
           - is_pure_numeric_paragraph        纯数字段
           - is_placeholder_only_paragraph    全是占位符的段
           - 整段只有 1 个 composition 且是公式 / same_style_unicode
           - pre 阶段文本 < min_text_length(默认 5)
        ② 计算 paragraph_token_count = tiktoken 编码长度
        ③ 若是 title，刷新「最近标题」上下文
        ④ executor.submit(translate_paragraph,
                           priority = 1048576 - token_count,
                           paragraph_token_count = ...,
                           title_paragraph / local_title_paragraph = ...)
线程池内：每个任务独立 pre → translate → post，互不干扰。
```

注意 `process_page` 里**每一页**都会重建 `page_font_map` 与 `page_xobj_font_map`（字体查找表），供 pre 阶段判断「字符组字体映射后是否与基准同字体」之用。

#### 4.2.3 源码精读

[il_translator.py:388-426](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L388-L426) —— `translate`：总入口。先在文档里找首个标题段、写入共享上下文（供 prompt 当 hint），统计总段落数，开进度条，然后 `with PriorityThreadPoolExecutor(max_workers=pool_max_workers)` 包住逐页处理。

[il_translator.py:444-481](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L444-L481) —— `process_page`：核心提交逻辑。

```python
paragraph_token_count = self.calc_token_count(paragraph.unicode)
if paragraph.layout_label == "title":
    self.shared_context_cross_split_part.recent_title_paragraph = ...  # 刷新最近标题
executor.submit(
    self.translate_paragraph, paragraph, page, pbar, tracker.new_paragraph(),
    page_font_map, page_xobj_font_map,
    priority=1048576 - paragraph_token_count,            # 优先级（见 4.3）
    paragraph_token_count=paragraph_token_count,
    title_paragraph=...first_paragraph,                  # 全文首个标题（hint）
    local_title_paragraph=...recent_title_paragraph,     # 最近标题（hint）
)
```

[il_translator.py:585-636](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L585-L636) —— 过滤分支：纯数字、纯占位符直接 `return None`；单 composition 且为公式/unicode 也跳过。

[il_translator.py:1214-1278](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L1214-L1278) —— `translate_paragraph`：线程池里真正执行的 worker，pre → `translate_engine.translate` 或 `llm_translate` → post，异常被 `try/except` 吞掉并记日志（单段失败不影响其他段）。

#### 4.2.4 代码实践

**目标**：通过 `--debug` 输出确认「一段一请求」与前置过滤。

**操作步骤**：

1. 用 `babeldoc --debug ...` 翻译一个 PDF（参数见 u1-l2）。
2. 翻译结束后打开工作目录里的 `translate_tracking.json`（路径见 [il_translator.py:418-426](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L418-L426)）。
3. 统计 `page[*].paragraph[*]` 中有 `input`/`output` 字段的条目数，与 PDF 实际段落数比较。

**需要观察的现象**：

- 每个被翻译的段落都有一条独立的 `input`（pre 后的占位符文本）和 `output`（译文），印证「一段一请求」。
- 纯数字段、纯公式段不会出现在 `input` 里，印证前置过滤。

**预期结果**：`translate_tracking.json` 中带翻译记录的段落数 < 总段落数。若无法运行，可改为「源码阅读型实践」：在 [il_translator.py:585-589](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L585-L589) 列出所有 `return None` 的分支并各举一个会被它跳过的段落例子。

#### 4.2.5 小练习与答案

**练习 1**：`translate_tracking.json` 里某段的 `input` 是 `引言{v1}<style id='1'>重点</style>结论`、`placeholders` 数组里有两条记录。这说明该段在 pre 阶段被注入了几个占位符？分别是什么类型？

> **答案**：2 个。一个是 `type: formula`（`{v1}`，对应一个公式对象），一个是 `type: rich_text`（`<style id='1'>`/`</style>`，对应一段粗体字符组）。见 [il_translator.py:94-130](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L94-L130) 的 `to_dict` 序列化。

**练习 2**：为什么 `process_page` 每页都要重建 `page_font_map`，而不是全局复用？

> **答案**：不同页可能引用不同字体（且 xobject 内还有独立字体表 `pdf_xobject[*].pdf_font`），pre 阶段判断「样式是否一致」需要查当前页/当前 xobj 的字体映射（[il_translator.py:452-461](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L452-L461)）。每页重建可保证字体查表的正确性与隔离性。

---

### 4.3 优先级线程池：谁先翻译

#### 4.3.1 概念说明

`PriorityThreadPoolExecutor` 是标准库 `ThreadPoolExecutor` 的子类，唯一区别是把内部的 FIFO 队列换成了 **基于小顶堆的优先级队列**：`submit` 时可传 `priority` 关键字，**数值越小越先被取出执行**。

`ILTranslator` 给每个段落任务的优先级是：

\[
\text{priority} = 1048576 - \text{paragraph\_token\_count}
\]

其中 \(1048576 = 2^{20}\)。

#### 4.3.2 核心流程（注意方向！）

把公式代入两个典型段落，按「小顶堆、最小者先出队」推演：

| 段落 | token 数 | priority |
| --- | --- | --- |
| 长段 A | 1000 | \(1048576-1000=1047576\) |
| 短段 B | 10 | \(1048576-10=1048566\) |

\(1047576 < 1048566\)，所以 **长段 A 的 priority 更小、先被取出执行**。

> ⚠️ **这与「短作业优先」的直觉相反**：本讲规格里把效果概括为「优先翻译短段」，但 **源码实际是「长段优先入场」**。`1048576 - token_count` 让 token 越多（越长的段落）priority 越小、越早被调度。这是一种「**长杆前置**」策略：把最耗时的段落尽早送进（受 QPS 限制的）翻译管线，避免它们堆积在队尾成为拖慢整体进度的尾巴。本结论是阅读源码得出，**强烈建议在 4.3.4 的实践中亲自验证**。

补充：当线程池尚未饱和（提交速度 < 消费速度）时，优先级几乎不影响实际执行顺序——任务一进队就被空闲线程领走。优先级只在「积压」时才显出作用。

#### 4.3.3 源码精读

[priority_thread_pool_executor.py:58-101](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/utils/priority_thread_pool_executor.py#L58-L101) —— `PriorityQueue`：基于 `heappush/heappop` 的优先队列，注释明确「retrieves open entries in priority order (lowest first)」，`_get` 用 `heappop` 取最小项。

[priority_thread_pool_executor.py:162-200](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/utils/priority_thread_pool_executor.py#L162-L200) —— `submit`：

```python
priority = kwargs.get("priority", random.randint(0, sys.maxsize - 1))
if "priority" in kwargs:
    del kwargs["priority"]
f = _base.Future()
w = _WorkItem(f, fn, args, kwargs)
self._work_queue.put((priority, w))   # (优先级, 工作项)
```

未指定 `priority` 时给一个随机值（保证可比、不报错）；`ILTranslator` 始终显式指定。

[il_translator.py:477](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L477) —— `priority=1048576 - paragraph_token_count`，即上文公式的落点。

#### 4.3.4 代码实践

**目标**：验证「长段优先入场」这一结论。

**操作步骤**：

1. 阅读上面的推演，确认你理解 `1048576 - token_count` + 小顶堆 ⇒ 长段先出队。
2. （可选，可运行）写一个最小脚本，构造一个 `PriorityThreadPoolExecutor(max_workers=1)`（只有 1 个 worker，强制积压，让优先级显形），按 `(token=1000, token=10)` 各 `submit` 一个「打印自己 token 数并 sleep」的任务：

```python
# 示例代码（非项目原有代码）
import time
from babeldoc.utils.priority_thread_pool_executor import PriorityThreadPoolExecutor

def job(tag):
    print("start", tag)
    time.sleep(0.2)
    return tag

with PriorityThreadPoolExecutor(max_workers=1) as ex:
    ex.submit(job, "long(1000)",  priority=1048576 - 1000)
    ex.submit(job, "short(10)",   priority=1048576 - 10)
    time.sleep(1)  # 让 worker 把队列里两个任务都跑完
```

**需要观察的现象 / 预期结果**：`max_workers=1` 时 worker 先取 priority 更小的「long(1000)」执行，再执行「short(10)」——打印顺序为 `start long(1000)` → `start short(10)`，印证长段优先。若本地不便运行，则回到源码推导：`1047576 < 1048566`，长段先出队。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：若把优先级公式改成 `priority = paragraph_token_count`（去掉 `1048576 -`），行为会变成什么？

> **答案**：变成「短段优先」：token 越少 priority 越小、越先出队。这正是「短作业优先」的常规直觉方向，但 **当前源码并非如此**——它用的是 `1048576 - token_count`，因此实际是长段优先。

**练习 2**：为什么优先级线程池要给「未指定 priority」的任务一个随机值，而不是固定 0？

> **答案**：固定值会让所有无优先级任务退化为同序 FIFO（且可能与显式指定者混淆）；随机值保证可比、且避免所有任务挤在同一优先级上。见 [priority_thread_pool_executor.py:190](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/utils/priority_thread_pool_executor.py#L190)。

---

### 4.4 限流与缓存参数：把并发、QPS、缓存串起来

#### 4.4.1 概念说明

`ILTranslator` 自身只管「编排」，真正的「节流」和「去重」由两个外部机制承担，它们与线程池协同：

- **线程池大小 `pool_max_workers`**：决定「同时在飞」的最多段落数（并发上限）。
- **漏桶限流 `RateLimiter`（全局单例）**：决定「实际打到 LLM 的请求频率」。并发再高，也会被漏桶匀速成 `qps` 次/秒。
- **本地翻译缓存**：命中缓存的请求既不调 LLM、也不消耗限流配额（u6-l1 已讲）。

三者关系一句话：**线程池放开并发，漏桶卡住频率，缓存负责省掉重复请求。**

#### 4.4.2 核心流程

```
ILTranslator 线程池（pool_max_workers 个 worker，并发跑 pre/post）
        │  每个任务调用 translate_engine.translate(text) 或 llm_translate(prompt)
        ▼
BaseTranslator.translate / llm_translate:
    ① 查缓存 cache.get(text)  ──命中──▶ 直接返回（不耗配额）
    ② 未命中 ──▶ _translate_rate_limiter.wait()   # 漏桶：sleep 到下一个允许时刻
    ③ do_translate / do_llm_translate            # 真正调 OpenAI 兼容 API
    ④ cache.set(text, translation)               # 写缓存
```

参数链路：`--qps`（CLI）→ `TranslationConfig.qps` → 既设漏桶 `set_translate_rate_limiter(qps)`，又作为 `pool_max_workers` 的默认值（见下）。

#### 4.4.3 源码精读

[translation_config.py:244-247](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L244-L247) —— `pool_max_workers` 默认取 `qps`：

```python
self.pool_max_workers = pool_max_workers if pool_max_workers is not None else qps
```

这意味着默认情况下「并发线程数 = QPS」——一个保守的设定：每秒最多发出 qps 个请求，也就开 qps 个线程在等。

[translator.py:28-59](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L28-L59) —— `RateLimiter` 漏桶：`min_interval = 1.0 / max_qps`，用 `time.monotonic()`（不受系统时钟回拨影响），`wait()` 内部计算需要 sleep 多久、并更新 `next_request_time`。

[translator.py:72-76](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L72-L76) —— 全局单例 `_translate_rate_limiter` 与 `set_translate_rate_limiter(max_qps)`：所有翻译器共享同一个 QPS 配额。

[translator.py:120-165](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L120-L165) —— `translate` / `llm_translate` 模板方法：查缓存 → `_translate_rate_limiter.wait()` → 真正翻译 → 写缓存。注意 `rate_limit_params`（携带 `paragraph_token_count`）一路透传到 `do_translate`/`do_llm_translate`，供后端按需使用（如 OpenAI 后端用它判断是否开 JSON 模式）。

[il_translator.py:382-386](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L382-L386) —— `calc_token_count`：用 `tiktoken.encoding_for_model("gpt-4o")` 估算 token 数，既用于优先级、也随 `paragraph_token_count` 传给翻译器。

#### 4.4.4 代码实践

**目标**：观察 QPS 与缓存的协同。

**操作步骤**：

1. 用 `--qps 1` 翻译同一份 PDF **两次**。
2. 第二次翻译时留意日志里的 `translate cache call count`（见 [translator.py:103-110](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L103-L110) 的 `__del__` 统计）。

**需要观察的现象**：

- 第一次：受 `qps=1` 限制，段落大致每秒处理一个，速度慢。
- 第二次：大量段落命中 `~/.cache/babeldoc/cache.v1.db`，几乎不调 LLM，速度快得多；`translate cache call count` 显著上升。

**预期结果**：第二次的「实际调用次数」远小于第一次，缓存命中率明显。**待本地验证**（取决于缓存是否被清理与文本是否完全一致）。

#### 4.4.5 小练习与答案

**练习 1**：把 `--qps` 调到很大、但 `--pool-max-workers` 保持默认，效果如何？反过来呢？

> **答案**：`pool_max_workers` 默认 = `qps`。若只调大 `qps`，两者都增大，并发与频率同步上升；若显式设 `pool_max_workers` 小于 `qps`，则线程少、漏桶空转（频率上限用不满）；若 `pool_max_workers` 大于 `qps`，则线程多但都被漏桶堵住排队，实际频率仍由 `qps` 决定。

**练习 2**：为什么缓存命中时既不调 LLM 也不耗限流配额？

> **答案**：`translate`/`llm_translate` 在 `_translate_rate_limiter.wait()` **之前**就 `return cache`（[translator.py:127-139](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L127-L139)），命中即短路返回，自然跳过限流与真实请求。

---

## 5. 综合实践

把本讲四条线索（占位符、并发编排、优先级、限流/缓存）串成一个端到端观察任务：

1. 用 `--debug --qps 2` 翻译一份带公式与粗体的多段落 PDF。
2. 打开 `translate_tracking.json`，挑一个 `placeholders` 非空的段落，对照它的 `input` 字段：
   - 指出哪些是公式占位符（`{vN}`）、哪些是富文本占位符（`<style id='N'>…</style>`）。
   - 解释 `output` 里这些占位符是否被 LLM 原样保留（即 `placeholder_full_match` 是否为 true）。
3. 解释该段的 `paragraph_token_count`（若 tracker 里有）如何决定了它在 `PriorityThreadPoolExecutor` 里的 priority，并据此判断它是「长段优先」还是排在后面。
4. 把同一份 PDF 立即再翻译一次，对比日志中的 `translate call count` 与 `translate cache call count`，说明缓存对「限流压力」的缓解作用。
5. 用一句话总结：`ILTranslator` 是如何在不破坏版面结构的前提下，把并发、优先级、限流、缓存四件事揉进「一段一请求」的翻译循环里的。

> 若本地无可用 PDF/API，可降级为「源码阅读型」：通读 `translate_paragraph`（[il_translator.py:1214-1278](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L1214-L1278)），画出 pre → translate → post 的调用时序，并标注每一步用到的配置项（`pool_max_workers`、`min_text_length`、`disable_rich_text_translate`）。

## 6. 本讲小结

- `ILTranslator` 把翻译一个段落拆成对称的 **pre/post** 两步：pre 用 `{vN}`（公式）和 `<style id='N'>…</style>`（富文本）两类占位符把「不可翻/带样式」部分替换成中性标记，post 用正则在译文里把它们定位、还原回 `PdfParagraphComposition`。
- 占位符编号自增（公式占 1、富文本占 2），并做 **防碰撞**（正则已在原文出现则换 id）；占位符超 40 个会递归禁用富文本重做。
- `ILTranslator` 是 **「一段一请求」**，不做跨段批处理（那是 u6-l3 `ILTranslatorLLMOnly` 的事）；它的「批处理」是并发编排——前置过滤掉纯数字/纯公式/过短段后，逐段提交进线程池。
- 优先级公式 `priority = 1048576 - paragraph_token_count` 配合 **小顶堆**，实际效果是 **长段优先入场**（长杆前置），与「短作业优先」直觉相反，可在实践中验证。
- 限流（全局漏桶 `RateLimiter`）、缓存（命中即短路、不耗配额）与线程池（`pool_max_workers` 默认 = `qps`）三者协同：线程池放开并发、漏桶卡住频率、缓存省去重复。
- `high_level` 用 `translator_supports_llm` 在 `ILTranslator` 与 `ILTranslatorLLMOnly` 间二选一，但二者共用 `stage_name = "Translate Paragraphs"`。

## 7. 下一步学习建议

- 下一讲 **u6-l3 LLM-only 翻译与质量校验**：精读 `ILTranslatorLLMOnly`，看它如何把**多个段落/跨栏/跨页**合并成大 prompt 做真正的批处理、如何跟踪标题上下文、又如何用 token 比例与 Levenshtein 距离做质量校验并在失败时回退到本讲的 `ILTranslator`。
- 回顾 **u6-l1**：把本讲里反复出现的 `translate_engine.translate` / `llm_translate`、漏桶、缓存再对照一遍，巩固「编排层（本讲）与服务层（u6-l1）」的分层。
- 进阶可读 `il_translator.py` 的 `generate_prompt_for_llm` / `_build_glossary_block` / `_build_context_block`（[il_translator.py:1042-1164](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/il_translator.py#L1042-L1164)），理解占位符文本最终被包进怎样的 prompt 模板（与 u6-l4 自动术语抽取、u6-l5 术语表衔接）。
