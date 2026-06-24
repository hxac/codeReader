# 自动术语抽取：AutomaticTermExtractor

## 1. 本讲目标

本讲聚焦 midend 流水线中权重第二高的 stage `Automatic Term Extraction`（自动术语抽取），对应类 `AutomaticTermExtractor`。学完本讲，你应当能够：

- 说清「自动术语抽取」要解决什么问题：为什么翻译科研论文前要先统一一批术语的译法。
- 理解 `AutomaticTermExtractor` 如何把整篇文档按 **约 600 token / 12 段** 切批，每批调一次 LLM 抽取并翻译术语。
- 掌握零散的 `raw_extracted_terms` 如何在 `SharedContextCrossSplitPart.finalize_auto_extracted_glossary` 里通过 **多数投票（majority vote）** 汇总成一张去重的自动术语表。
- 认识 `--no-auto-extract-glossary`、`--save-auto-extracted-glossary`、`--term-pool-max-workers` 三个相关开关，以及该阶段如何接入流水线、产出的术语表又如何被下一阶段（ILTranslator）消费。

本讲承接 [u6-l2 IL 翻译编排](u6-l2-il-translator-orchestration.md)：ILTranslator 是「按段调 LLM 翻译」，而 `AutomaticTermExtractor` 在它之前先跑一遍，专门用来「提炼术语并喂回翻译阶段」，两者共享同一个 `shared_context_cross_split_part` 容器。

## 2. 前置知识

在进入源码前，先建立几个直觉概念。

**术语一致性（terminology consistency）问题。**
同一篇论文里，「transformer」可能在第 1 页被译成「变换器」、第 5 页被译成「Transformer 模型」。逐段独立翻译时，LLM 没有全局视野，就会出现这种前后不一致。解决办法是：在正式翻译前，先扫一遍全文，把关键术语（人名、机构、算法名、领域名词）的固定译法定下来，形成一张**术语表（glossary）**，再让翻译阶段照着这张表译。

**多数投票（majority voting）。**
术语在全文中会反复出现，每一批都可能被抽取一次。同一个源术语（src）在不同批次里可能得到不同的译文（tgt）——例如「attention」一批给了「注意力」，另一批给了「关注」。如何决定最终译法？最朴素可靠的办法就是**数票**：哪个译文出现次数最多就选哪个。Python 标准库 `collections.Counter(tgts).most_common(1)[0][0]` 一行就能完成。

**tiktoken 与「token 计费」。**
LLM 不是按「字」计费，而是按 **token**（子词单位）计费。`tiktoken` 是 OpenAI 的分词器，可以把文本切分成 token 并计数。本讲里 tiktoken 扮演两个角色：
- **本地预算**：在切批时估算每批输入有多少 token，控制单批不超 ~600 token；
- **API 实际消耗**：翻译器内部维护了 token 计数器，本阶段结束时统计真实消耗。

> 注意：这两者不同。前者是本地估算用于调度，后者是真实 API 返回的用量统计。

**`shared_context_cross_split_part`（跨分片共享上下文）。**
在分片翻译（`--max-pages-per-part`）时，文档被切成多片分别翻译。术语表必须**全局唯一**——不能第 1 片把「attention」定为「注意力」、第 2 片定为「关注」。因此术语表存放在一个跨分片共享的容器里，详见 [u6-l3](u6-l3-llm-only-translator.md) 与 [u8-l2](u8-l2-split-and-merge.md)。本讲关注它如何收集与汇总术语。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [babeldoc/format/pdf/document_il/midend/automatic_term_extractor.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/automatic_term_extractor.py) | 本讲主角。定义 `AutomaticTermExtractor`，负责切批、调 LLM 抽取术语、写回共享上下文。 |
| [babeldoc/format/pdf/translation_config.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py) | 定义 `SharedContextCrossSplitPart`（含 `raw_extracted_terms`、`finalize_auto_extracted_glossary`、`add_raw_extracted_term_pair`）与 `TranslationConfig` 的相关配置项。 |
| [babeldoc/format/pdf/high_level.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py) | 流水线编排。在 `TRANSLATE_STAGES` 注册本阶段权重，在 `_do_translate_single` 中按条件调用。 |
| [babeldoc/translator/translator.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py) | `BaseTranslator.llm_translate` 模板方法（缓存→限流→`do_llm_translate`），是本阶段真正调用 LLM 的入口。 |
| [babeldoc/glossary.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py) | `Glossary` / `GlossaryEntry` 定义，含 `to_csv()` 导出与 `get_active_entries_for_text()` 匹配。 |
| [babeldoc/format/pdf/result_merger.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/result_merger.py) | 分片场景下把最终术语表写成用户可见的 CSV。 |

## 4. 核心概念与源码讲解

### 4.1 术语抽取批次策略

#### 4.1.1 概念说明

一篇论文动辄上万 token，不可能把全文一次性塞给 LLM 抽术语（既贵又可能超上下文窗口）。`AutomaticTermExtractor` 采用**流式切批**：遍历每页的每个段落，把它们装进一个「待处理桶」，桶满了就倒出去组成一个批次（batch）。

「桶满」由两个条件触发，**先到先flush**：

1. 桶内累计 token 数超过 **600**；
2. 桶内段落数超过 **12**。

这两个常量写死在源码里（不是配置项）。批次越大，单次 LLM 调用摊到的固定开销越低，但失败重试成本越高、上下文也越长；600 token / 12 段是一个折中。

#### 4.1.2 核心流程

```text
对每一页 page：
    清空桶 paragraphs = []，累计 token = 0
    对该页每个段落 paragraph：
        若段落不可用（无 unicode / cid 段 / 纯数字 / 纯占位符）→ 跳过，进度条 +1
        否则：
            paragraphs.append(paragraph)
            累计 token += calc_token_count(paragraph.unicode)
            若 累计 token > 600 或 len(paragraphs) > 12：
                把整桶提交到优先级线程池，清空桶
    页末若桶里还有剩余，也提交一批
```

注意：跳过的段落会立即 `pbar.advance(1)`，保证进度条总刻度（=全文档段落数）与实际遍历一致，不会卡住。

#### 4.1.3 源码精读

切批核心逻辑在 `process_page` 中：

[automatic_term_extractor.py:252-263](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/automatic_term_extractor.py#L252-L263) —— 累计 token 与段落数，超阈值即成批提交：

```python
total_token_count += self.calc_token_count(paragraph.unicode)
paragraphs.append(paragraph)
if total_token_count > 600 or len(paragraphs) > 12:
    executor.submit(
        self.extract_terms_from_paragraphs,
        BatchParagraph(paragraphs, tracker),
        pbar,
        total_token_count,
        priority=1048576 - total_token_count,
    )
    paragraphs = []
    total_token_count = 0
```

这里的 `priority=1048576 - total_token_count` 是**优先级线程池**的关键（与 [u6-l2](u6-l2-il-translator-orchestration.md) 的 ILTranslator 同款套路）：token 越多的批次优先级越高、越早被调度。直觉上是「让大块头先入场」，避免末尾才处理大批次拖长尾。

token 估算依赖 `calc_token_count`：

[automatic_term_extractor.py:154-158](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/automatic_term_extractor.py#L154-L158) —— 用 gpt-4o 的分词器本地估算，异常时返回 0（不影响主流程）：

```python
def calc_token_count(self, text: str) -> int:
    try:
        return len(self.tokenizer.encode(text, disallowed_special=()))
    except Exception:
        return 0
```

分词器在构造函数里一次性初始化，避免每段重复加载：

[automatic_term_extractor.py:144](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/automatic_term_extractor.py#L144) —— `self.tokenizer = tiktoken.encoding_for_model("gpt-4o")`。

跳过无效段落的过滤逻辑见 [automatic_term_extractor.py:236-251](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/automatic_term_extractor.py#L236-L251)，分别用 `is_cid_paragraph` / `is_pure_numeric_paragraph` / `is_placeholder_only_paragraph` 排除 CID 段、纯数字段、纯占位符段——这些段落里没有值得抽取的自然语言术语。

#### 4.1.4 代码实践

**实践目标**：理解切批阈值对批次数量的影响。

**操作步骤**（源码阅读型实践）：

1. 打开 `automatic_term_extractor.py`，定位 `process_page` 第 254 行的 `if total_token_count > 600 or len(paragraphs) > 12:`。
2. 设想一份「全是短段落」的文档：每个段落约 30 token，共 100 段。手工推算：12 段一批 → 约 8~9 批；而 token 累计（12×30=360）远未到 600，所以由「段落数」条件先触发。
3. 再设想一份「每段都很长」的文档：每段 400 token。推算：第 2 段累计就达 800 > 600 → 每 2 段一批。
4. 若想观察真实批次数，可在 `extract_terms_from_paragraphs` 入口加一行 `logger.info(f"batch size={len(paragraphs.paragraphs)} tokens={paragraph_token_count}")`，再带 `--debug` 跑一次翻译，查看日志。

**需要观察的现象**：长文档会产生多个批次；每个批次的段落数与 token 数被阈值约束。

**预期结果**：批次大小在 1~12 段之间，且单批 token 通常不超过约 600（边界段落除外）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `600` 改成 `300`，对 LLM 调用次数和单批上下文分别有什么影响？

> **答案**：批次变小、批次变多 → LLM 调用次数增加（更贵、更慢），但每批上下文更短、更聚焦，单批失败重试成本更低。

**练习 2**：为什么跳过的段落要立刻 `pbar.advance(1)`，而不是等批次处理完再统一推进？

> **答案**：进度条总刻度是「全文档段落数」。被跳过的段落不会进入任何批次，若不立即推进，进度条会永远少这一格、卡在 99%。立即推进保证刻度与遍历一致。

---

### 4.2 LLM 抽取与翻译

#### 4.2.1 概念说明

每个批次最终会调用一次 LLM，让它扮演「多语言术语学家」：阅读这批文本，挑出关键术语，并翻译成目标语言。输出被严格要求为 **JSON 数组**，形如 `[{"src": "LLM", "tgt": "大语言模型"}]`。

这里有一个精巧设计：如果用户已经提供了术语表（`--glossary`），本阶段会把**命中当前文本的用户术语**作为「参考术语表（Reference Glossary）」拼进 prompt，要求 LLM 抽取时与之保持一致——这是「自动术语」与「用户术语」协同的接缝点（完整的用户术语表匹配机制见 [u6-l5 术语表系统](u6-l5-glossary-system.md)）。

#### 4.2.2 核心流程

```text
对每个批次 extract_terms_from_paragraphs：
    取出 inputs = [p.unicode for p in 批次段落]
    若用户有术语表：
        用 Hyperscan 在 inputs 上匹配命中项 → 拼 reference_glossary_section
    渲染 LLM_PROMPT_TEMPLATE（target_language / text / reference / example）
    output = translate_engine.llm_translate(prompt, rate_limit_params={...})
    清洗 output（去 ```json / <json> 等包裹）
    json.loads → 逐项校验：
        src、tgt 非空，src < 100 字符
        若 src==tgt 且 src 很短（<3）→ 丢弃（无意义的"翻译"）
    每个合法 (src, tgt) → shared_context.add_raw_extracted_term_pair(src, tgt)
    finally: 进度条推进批段落数
```

`llm_translate` 是 `BaseTranslator` 的**模板方法**：它先查本地翻译缓存、再过全局漏桶限流、最后才调子类实现的 `do_llm_translate`。也就是说，术语抽取和正文翻译共享同一套缓存与限流配额。

#### 4.2.3 源码精读

prompt 模板定义在模块顶层，规则非常明确（只抽命名实体与领域名词、排除数学符号、按首次出现顺序、要求 JSON）：

[automatic_term_extractor.py:31-66](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/automatic_term_extractor.py#L31-L66) —— `LLM_PROMPT_TEMPLATE`，其中第 47 行的 `{reference_glossary_section}` 占位即用户术语表注入点。

真正的调用与解析在 `extract_terms_from_paragraphs`：

[automatic_term_extractor.py:327-349](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/automatic_term_extractor.py#L327-L349) —— 调 LLM、清洗、解析、逐项校验并写回共享上下文：

```python
output = self.translate_engine.llm_translate(
    prompt,
    rate_limit_params={
        "paragraph_token_count": paragraph_token_count,
        "request_json_mode": True,
    },
)
tracker.set_output(output)
cleaned_output = self._clean_json_output(output)
response = json.loads(cleaned_output)
...
for term in response:
    if isinstance(term, dict) and "src" in term and "tgt" in term:
        src_term = str(term["src"]).strip()
        tgt_term = str(term["tgt"]).strip()
        if src_term == tgt_term and len(src_term) < 3:
            continue
        if src_term and tgt_term and len(src_term) < 100:
            self.shared_context.add_raw_extracted_term_pair(src_term, tgt_term)
```

注意 `request_json_mode: True` 这个参数——它提示翻译后端（如 OpenAI）启用 JSON 输出模式，提高返回合法 JSON 的概率。

调用入口 `llm_translate` 的模板方法结构：

[translator.py:141-165](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L141-L165) —— 查缓存 → 限流 → `do_llm_translate` → 写缓存：

```python
def llm_translate(self, text, ignore_cache=False, rate_limit_params: dict = None):
    ...
    if not (self.ignore_cache or ignore_cache):
        cache = self.cache.get(text)
        if cache is not None:
            return cache
    _translate_rate_limiter.wait()
    translation = self.do_llm_translate(text, rate_limit_params)
    ...
    self.cache.set(text, translation)
    return translation
```

> **说明**：文件中还有一个方法 `_process_llm_response`（[第 193 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/automatic_term_extractor.py#L193)），功能与上面这段解析逻辑类似，但当前代码中**没有任何地方调用它**（属于遗留/备用代码）。活跃路径就是 `extract_terms_from_paragraphs` 内联解析那段。读源码时不要被它误导。

#### 4.2.4 代码实践

**实践目标**：看清 prompt 长什么样、参考术语表如何注入。

**操作步骤**（源码阅读型实践）：

1. 在 `extract_terms_from_paragraphs` 第 326 行 `tracker.set_input(prompt)` 附近，`tracker` 已经记录了完整 prompt。
2. 带 `--debug` 跑一次翻译（需 LLM 引擎，见下方注意），翻译结束后打开工作目录里的 `term_extractor_tracking.json`（写入逻辑见 4.4.3）。
3. 在该 JSON 里找到任意一个 `input` 字段，阅读完整 prompt：你能看到抽取规则、翻译规则，以及（若提供了 `--glossary`）一段 `Reference Glossaries` 与命中术语。

**需要观察的现象**：prompt 末尾的 `Input Text` 是当前批次的真实段落拼接；若无用户术语表，`reference_glossary_section` 为空字符串。

**预期结果**：能从 `term_extractor_tracking.json` 中找到与 `LLM_PROMPT_TEMPLATE` 对应的填充后文本。

> **注意**：本阶段要求翻译引擎实现 `do_llm_translate`（即「LLM 模式」）。当前内置只有 `OpenAITranslator` 满足，因此需用 `--openai` 系列 + 有效 key 才能真正触发；否则该阶段会被跳过（详见 4.4）。

#### 4.2.5 小练习与答案

**练习 1**：为什么术语抽取用 `llm_translate`（LLM 模式）而不是普通 `translate`？

> **答案**：术语抽取的输入是一个**结构化指令 prompt**（要求返回 JSON），不是单纯待翻译的句子。`do_llm_translate` 面向这种「自由指令」场景（可传 `request_json_mode` 等参数），而 `do_translate` 面向「原文→译文」的窄场景。

**练习 2**：`if src_term == tgt_term and len(src_term) < 3: continue` 这行过滤掉了什么样的「假术语」？

> **答案**：过滤掉源术语与译文完全相同且很短的情况，例如把 "OK" 译成 "OK"——这类无翻译价值的项不应进入术语表。

---

### 4.3 术语表汇总：多数投票

#### 4.3.1 概念说明

上一节里，每个批次都往 `shared_context.raw_extracted_terms` 里追加 `(src, tgt)` 二元组。这是一条**带重复、带噪声**的原始流水——同一个术语在不同批次里可能出现多次、译文也可能不同。

`finalize_auto_extracted_glossary` 就是「收口」环节：遍历所有原始二元组，按源术语分组，每组用多数投票选出出现次数最多的译文，最终汇总成一张**去重的 `Glossary`**。这张表就是后续翻译阶段的「自动术语表」。

#### 4.3.2 核心流程

```text
finalize_auto_extracted_glossary():
    若 raw_extracted_terms 为空 → 直接返回（auto_extracted_glossary = None）
    term_translations = {}   # src -> [tgt, tgt, ...]
    for (src, tgt) in raw_extracted_terms:
        term_translations[src].append(tgt)
    final_entries = []
    for (src, tgts) in term_translations.items():
        most_common_tgt = Counter(tgts).most_common(1)[0][0]   # 多数投票
        final_entries.append(GlossaryEntry(src, most_common_tgt))
    auto_extracted_glossary = Glossary(name=unique_name, entries=final_entries)
```

数学上，对一个源术语 \(s\)，设其候选译文集合为 \(\{t_1, t_2, \dots, t_n\}\)（含重复），最终译文为：

\[
t^*(s) = \underset{t}{\arg\max}\; \bigl|\{\,i : t_i = t\,\}\bigr|
\]

即出现频次最高的译文。`Counter.most_common(1)` 在平票时返回**首次遇到**的那个（取决于遍历顺序），因此结果稳定但不保证语言学最优。

#### 4.3.3 源码精读

汇总逻辑在 `SharedContextCrossSplitPart.finalize_auto_extracted_glossary`：

[translation_config.py:99-121](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L99-L121) —— 分组 + 多数投票 + 构造 `Glossary`：

```python
def finalize_auto_extracted_glossary(self):
    with self._lock:
        self.auto_extracted_glossary = None
        if not self.raw_extracted_terms:
            self.raw_extracted_terms = []
            return
        term_translations: dict[str, list[str]] = {}
        for src, tgt in self.raw_extracted_terms:
            term_translations.setdefault(src, []).append(tgt)
        final_entries: list[GlossaryEntry] = []
        for src, tgts in term_translations.items():
            if not tgts:
                continue
            most_common_tgt = Counter(tgts).most_common(1)[0][0]
            final_entries.append(GlossaryEntry(src, most_common_tgt))
        if final_entries:
            self.auto_extracted_glossary = Glossary(
                name=self.unique_name, entries=final_entries
            )
```

原始数据收集点（注意全程加锁，因为多批次并发写）：

[translation_config.py:72-74](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L72-L74) —— `add_raw_extracted_term_pair`，把 `(src, tgt)` 追加进 `raw_extracted_terms`。

`raw_extracted_terms` 字段本身定义在第 [41 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L41)：`self.raw_extracted_terms: list[tuple[str, str]] = []`。

汇总由 `procress`（注意源码里这个方法名是 `procress`，疑似 `process` 的拼写，但这是真实方法名）在所有批次跑完后触发：

[automatic_term_extractor.py:378](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/automatic_term_extractor.py#L378) —— `self.shared_context.finalize_auto_extracted_glossary()`。

汇总出的 `Glossary` 会被翻译阶段通过 `get_glossaries_for_translation` 取用：

[translation_config.py:130-140](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L130-L140) —— 一个值得注意的策略：当自动术语抽取开启且有结果时，**只返回自动术语表**（替代用户术语表）：

```python
def get_glossaries_for_translation(self, auto_extract_enabled: bool) -> list[Glossary]:
    with self._lock:
        if auto_extract_enabled and self.auto_extracted_glossary:
            return [self.auto_extracted_glossary]
        else:
            all_glossaries = list(self.user_glossaries)
            if self.auto_extracted_glossary:
                all_glossaries.append(self.auto_extracted_glossary)
            return all_glossaries
```

这是因为抽取阶段已经把用户术语作为「参考」喂给了 LLM，自动表里已隐含了对用户术语的遵循，故翻译阶段以自动表为准、避免重复注入。该方法被 `il_translator.py:351` 与 `il_translator_llm_only.py:133` 调用。

> 另外，`Glossary` 构造函数本身会按「归一化源术语」去重（[glossary.py:44-52](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py#L44-L52)），所以即使 `final_entries` 里有大小写/空白差异的重复源，也会在 `normalized_lookup` 层面再收敛一次。

#### 4.3.4 代码实践

**实践目标**：亲眼看到「原始噪声」→「多数投票」→「最终译表」的收敛过程。

**操作步骤**（源码阅读 + 本地数据型实践）：

1. 带 `--debug` 跑一次翻译后，打开工作目录里的 `term_extractor_freq.json`（[写入逻辑：automatic_term_extractor.py:400-410](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/automatic_term_extractor.py#L400-L410)），它是 `raw_extracted_terms` 的直接 dump——你会看到同一个 src 多次出现、译文可能不同。
2. 再打开同目录的 `auto_extractor_glossary.csv`（[写入逻辑：automatic_term_extractor.py:412-419](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/automatic_term_extractor.py#L412-L419)），它是 `finalize_auto_extracted_glossary` 后的结果——每个 src 只剩一行。
3. 对照两者：对某个在 freq 里出现多次的 src，数一下哪个 tgt 最多，确认它就是 CSV 里最终保留的译文。

**需要观察的现象**：freq.json 里 src 重复出现且 tgt 可能不一；CSV 里 src 唯一、tgt 是频次最高者。

**预期结果**：例如 freq 中 `"attention"` 出现 3 次（注意力/注意力/关注），CSV 中保留「注意力」。

**待本地验证**：具体术语取决于真实 PDF 与 LLM 输出，请以你本地跑出的文件为准。

#### 4.3.5 小练习与答案

**练习 1**：若某源术语的两个候选译文票数相同（各 2 票），`Counter.most_common(1)` 会返回哪个？

> **答案**：返回 `Counter` 内部遍历时先遇到的那个。`Counter` 基于 dict，Python 3.7+ 保持插入顺序，故是「先被 `append` 进 `raw_extracted_terms` 的那个译文」胜出——结果稳定但非语言学最优。

**练习 2**：为什么 `add_raw_extracted_term_pair` 和 `finalize_auto_extracted_glossary` 都要 `with self._lock`？

> **答案**：批次在优先级线程池里**并发**执行，多个线程会同时往 `raw_extracted_terms` 追加；`finalize` 又要在所有批次结束后读它。加锁保证「并发写不丢数据、汇总读到完整集合」。`finalize` 由主流程在 `with PriorityThreadPoolExecutor(...)` 退出后调用，此时线程池已 join，但加锁仍是防御性的正确做法。

---

### 4.4 相关配置项与流水线接入

#### 4.4.1 概念说明

自动术语抽取并非无条件运行，它受三个层面控制：

1. **CLI 开关**：`--no-auto-extract-glossary`（关闭）、`--save-auto-extracted-glossary`（导出 CSV）、`--term-pool-max-workers`（并发度）。
2. **配置联动**：`TranslationConfig` 在初始化时，若处于 `skip_translation` 或 `only_parse_generate_pdf` 模式，会强制把 `auto_extract_glossary` 置 False（既然不翻译，抽术语无意义）。
3. **能力探测**：本阶段要求翻译引擎支持 LLM 模式（`do_llm_translate`），否则整段跳过。

#### 4.4.2 核心流程

```text
get_translation_stage():
    若 not auto_extract_glossary → 把本 stage 从节目单剔除

_do_translate_single():
    term_extraction_engine = config.get_term_extraction_translator()  # 默认=主翻译器
    support_llm_term_extraction = translator_supports_llm(term_extraction_engine)
    if support_llm_term_extraction and auto_extract_glossary:
        AutomaticTermExtractor(term_extraction_engine, config).procress(docs)   # 抽取+汇总
    # 随后进入 ILTranslator，它会通过 get_glossaries_for_translation 取用自动术语表
```

#### 4.4.3 源码精读

阶段权重注册（30.0，仅次于翻译的 46.96，是全流水线第二耗时阶段，因为也要调 LLM）：

[high_level.py:68](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L68) —— `(AutomaticTermExtractor.stage_name, 30.0),  # Extract Terms`。

阶段按条件剔除：

[high_level.py:290-291](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L290-L291) —— 关闭时从 stage 列表移除：

```python
if not translation_config.auto_extract_glossary:
    should_remove.append(AutomaticTermExtractor.stage_name)
```

实际调用点（双重门控：能力 + 开关）：

[high_level.py:987-995](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L987-L995)：

```python
term_extraction_engine = translation_config.get_term_extraction_translator()
support_llm_translate = translator_supports_llm(translate_engine)
support_llm_term_extraction = translator_supports_llm(term_extraction_engine)

if support_llm_term_extraction and translation_config.auto_extract_glossary:
    AutomaticTermExtractor(term_extraction_engine, translation_config).procress(docs)
```

能力探测函数（探针式：真调一次 `do_llm_translate(None)`，捕获 `NotImplementedError` 判定不支持）：

[high_level.py:246-256](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L246-L256)：

```python
def translator_supports_llm(translator) -> bool:
    if not translator or not hasattr(translator, "do_llm_translate"):
        return False
    try:
        translator.do_llm_translate(None)
        return True
    except NotImplementedError:
        return False
    ...
```

配置项默认值与联动：

[translation_config.py:335-341](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L335-L341) —— 默认开启，但 skip_translation / only_parse_generate_pdf 时强制关闭：

```python
self.auto_extract_glossary = auto_extract_glossary
...
if self.skip_translation or self.only_parse_generate_pdf:
    self.auto_extract_glossary = False
```

术语抽取专用线程池大小，缺省回退到主池大小（再回退到 qps）：

[translation_config.py:250-253](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L250-L253)：

```python
self.term_pool_max_workers = (
    term_pool_max_workers if term_pool_max_workers is not None
    else self.pool_max_workers
)
```

`procress`（主入口）用这个池并发跑批次，并在结束后统计 token 用量、写调试文件：

[automatic_term_extractor.py:364-387](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/automatic_term_extractor.py#L364-L387) —— 用 `progress_monitor.stage_start` 开进度条、`PriorityThreadPoolExecutor` 并发、跑完 `finalize_auto_extracted_glossary()`、再 `record_term_extraction_usage` 记 token。

token 统计通过前后两次快照差值实现（[`_snapshot_token_usage`，第 160-177 行](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/automatic_term_extractor.py#L160-L177) 读取引擎上的 `token_count` / `prompt_token_count` / `completion_token_count` / `cache_hit_prompt_token_count`），累加进 [`record_term_extraction_usage`，translation_config.py:489-506](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L489-L506) 的 `term_extraction_token_usage` 字典。

用户可见的 CSV 导出（分片场景，单文档则在 `pdf_creater.py` 里同款逻辑）：

[result_merger.py:130-144](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/result_merger.py#L130-L144) —— 当 `save_auto_extracted_glossary=True` 且有自动表时，写 `{basename}{suffix}.{lang_out}.glossary.csv`：

```python
if (
    self.config.save_auto_extracted_glossary
    and self.config.shared_context_cross_split_part.auto_extracted_glossary
):
    auto_extracted_glossary_path = self.config.get_output_file_path(
        f"{basename}{debug_suffix}.{self.config.lang_out}.glossary.csv"
    )
    with auto_extracted_glossary_path.open("w", encoding="utf-8-sig") as f:
        f.write(
            self.config.shared_context_cross_split_part.auto_extracted_glossary.to_csv()
        )
```

`to_csv()` 输出三列 `source,target,tgt_lng`（[glossary.py:172-188](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/glossary.py#L172-L188)），用 `utf-8-sig` 编码以便 Excel 正确识别中文。

CLI 参数定义（注意默认值的差异）：

- [main.py:295-299](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L295-L299)：`--no-auto-extract-glossary` 是 `store_false`、默认 `True`（即默认开启抽取）。
- [main.py:321-323](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L321-L323)：`--save-auto-extracted-glossary` 是 `store_true`、默认 `False`（CLI 默认不导出 CSV，需显式加）。
- [main.py:290-293](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L290-L293)：`--term-pool-max-workers`，不填则回退 `--pool-max-workers`。

> **一个小坑**：`TranslationConfig` 构造函数里 `save_auto_extracted_glossary` 形参默认是 `True`（[translation_config.py:205](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L205)），但 CLI 入口 `main.py` 把它显式设成 `args.save_auto_extracted_glossary`（默认 False）。所以**经命令行使用时默认不导出 CSV**；只有把 BabelDOC 当库直接构造 `TranslationConfig` 且不传该参数时，默认才会导出。

#### 4.4.4 代码实践

**实践目标**：导出一张自动术语表 CSV，并解释多数投票如何决定最终译文。

**操作步骤**（需可联网的 OpenAI 兼容服务）：

1. 用 [u1-l2](u1-l2-install-and-run-cli.md) 的方式安装并配置好 `--openai` 与 key。
2. 运行：

   ```bash
   babeldoc --openai --openai-api-key <KEY> \
     --files examples/ci/test.pdf \
     --save-auto-extracted-glossary \
     --debug --working-dir ./work
   ```

3. 翻译完成后，在工作目录 `./work` 里找到 `auto_extractor_glossary.csv`（调试副本）以及输出目录里的 `<文件名>.<lang_out>.glossary.csv`（用户副本，三列 `source,target,tgt_lng`）。
4. 同时打开 `./work/term_extractor_freq.json`（原始带重复的 `(src,tgt)` 流）与 `./work/term_extractor_tracking.json`（每批 input/output）。
5. 选 CSV 中某个术语，回到 freq.json 里数它出现过几次、各候选译文频次如何，验证 CSV 保留的就是频次最高者。

**需要观察的现象**：CSV 中每个 source 唯一；freq.json 中同一 source 可能多次出现且 target 不同。

**预期结果**：能找到一个「在 freq 里有多版本译文、CSV 里只保留最高频版本」的实例，并说清它是 `Counter(tgts).most_common(1)` 选出来的。

**待本地验证**：具体术语取决于 PDF 内容与 LLM 输出。

> 若暂无可用 LLM key：可做源码阅读型实践——在 [translation_config.py:111-116](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L111-L116) 旁，手工构造 `raw_extracted_terms = [("attention","注意力"),("attention","关注"),("attention","注意力"),("transformer","变换器")]`，在 REPL 里调用 `finalize_auto_extracted_glossary()`，观察最终 `auto_extracted_glossary.entries` 为 `[("attention","注意力"),("transformer","变换器")]`。

#### 4.4.5 小练习与答案

**练习 1**：如果翻译引擎不支持 LLM 模式（例如只有传统翻译接口），自动术语抽取会发生什么？

> **答案**：`translator_supports_llm(term_extraction_engine)` 返回 False，[high_level.py:992](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L992) 的 `if` 不成立，`procress` 不会被调用，本阶段静默跳过，`auto_extracted_glossary` 保持 None，翻译阶段回退到只用用户术语表。

**练习 2**：`--only-parse-generate-pdf` 模式下，为何自动术语抽取一定不运行？

> **答案**：该模式只解析+重排、不翻译（[translation_config.py:340-341](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L340-L341) 把 `auto_extract_glossary` 强制置 False）。既然不翻译，抽术语纯属浪费 LLM 调用，故直接关闭。

## 5. 综合实践

把本讲四个模块串起来，做一个「端到端术语流转」追踪任务：

1. **准备**：用 `--debug --working-dir ./work --save-auto-extracted-glossary` 跑一次真实翻译（需 OpenAI 兼容服务）。
2. **切批**：打开 `./work/term_extractor_tracking.json`，数一下产生了多少个批次（即多少次 LLM 调用），验证每批 input 的段落数 ≤ 12、token 数大致 ≤ 600（对应 4.1）。
3. **抽取**：阅读其中一批的 `input`（prompt）与 `output`（JSON），确认 output 是 `[{"src":..,"tgt":..}]` 结构（对应 4.2）。
4. **汇总**：打开 `./work/term_extractor_freq.json`，挑一个出现 ≥2 次的 src，手工统计各 tgt 频次；再打开 `auto_extractor_glossary.csv`，确认该 src 的最终译文就是你算出的最高频项（对应 4.3）。
5. **接入**：在源码里画出这条链：`extract_terms_from_paragraphs` → `add_raw_extracted_term_pair` → `finalize_auto_extracted_glossary` → `get_glossaries_for_translation`（被 `il_translator.py:351` 调用）。说清为什么分片翻译时这张表必须放在 `shared_context_cross_split_part` 里（对应 4.4）。

**交付物**：一段文字 + 一张调用链图，说明「原始术语 → 多数投票 → 翻译阶段消费」的完整路径，并指出每一跳的源码位置。

**待本地验证**：步骤 1~4 的具体数值以本地运行结果为准。

## 6. 本讲小结

- `AutomaticTermExtractor` 在翻译前先跑一遍，专门提炼全文术语并统一译法，解决逐段翻译的术语不一致问题。
- 切批策略：累计 token 超 600 **或** 段落数超 12 即成批，用 tiktoken（gpt-4o）本地估算 token；批次投进优先级线程池（token 多的先跑）。
- 每批调一次 `llm_translate`（缓存→限流→`do_llm_translate`），要求 LLM 返回 JSON 数组 `[{src,tgt}]`；用户术语表会作为「参考」拼进 prompt。
- 抽取结果以 `(src,tgt)` 二元组流式追加进 `shared_context.raw_extracted_terms`（加锁并发安全）。
- `finalize_auto_extracted_glossary` 按 src 分组、用 `Counter.most_common` 多数投票，汇总成去重的 `Glossary`，再由翻译阶段 `get_glossaries_for_translation` 取用。
- 受 `--no-auto-extract-glossary`（关闭）、`skip_translation/only_parse_generate_pdf`（强制关闭）、引擎 LLM 能力三重门控；`--save-auto-extracted-glossary` 控制是否导出 CSV，`--term-pool-max-workers` 控制并发度。

## 7. 下一步学习建议

- 阅读 [u6-l5 术语表系统：Glossary 与 Hyperscan](u6-l5-glossary-system.md)，搞清 `get_active_entries_for_text` 如何用 Hyperscan 高性能匹配术语、`from_csv` 如何加载用户术语表——那是本讲「参考术语表注入」与「翻译阶段消费」的另一端。
- 回看 [u6-l2](u6-l2-il-translator-orchestration.md) 的 `get_glossaries_for_translation` 调用点（`il_translator.py:351`），理解自动术语表如何转化为翻译 prompt 里的术语提示。
- 若想看跨分片共享的全貌，结合 [u8-l2 分片翻译与结果合并](u8-l2-split-and-merge.md)，理解 `shared_context_cross_split_part` 为何必须在分片间共享同一份术语表。
- 进阶可阅读 `procress` 里的 token 统计（`_snapshot_token_usage` / `record_term_extraction_usage`），了解 BabelDOC 如何把术语抽取的 API 成本单独核算。
