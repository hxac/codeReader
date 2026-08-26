# XMLTranslator 核心：翻译任务的编排

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `TranslationTask` 三个字段（`element`、`action`、`payload`）各自承担的角色，以及 `payload` 为什么是泛型。
2. 掌握 `SubmitKind` 三种落地方式（`REPLACE` / `APPEND_TEXT` / `APPEND_BLOCK`）的语义。
3. 沿 `translate_elements` 生成器走完一条完整链路：任务收集 → 切分分组 → 纯文本翻译 → XML 回填修复 → 按模式提交产出。
4. 解释「翻译」与「回填」为什么要用两个独立的 LLM 运行时（`translation_llm` 与 `fill_llm`），以及两个 `protocol_version` 如何隔离缓存。
5. 独立用 jinja2 渲染 `translate.jinja` 与 `fill.jinja`，理解目标语言与用户提示是如何注入 system 消息的。

## 2. 前置知识

本讲默认你已读过 u7-l1（转换器协议）。再补充几个本讲反复出现的概念：

- **Element（XML 树节点）**：pdf-craft 用 Python 标准库 `xml.etree.ElementTree.Element` 表示章节 XML、EPUB 的 XHTML 片段。注意 `Element` **不可哈希**，所以代码里到处用内置 `id(element)`（对象身份）当字典键，而不是直接把元素放进 `set` / `dict`。
- **生成器（generator）与流式处理**：`yield` 函数产出一个惰性序列。`translate_elements` 本身是生成器，调用方（如 EPUB 翻译管线）边消费边把译文写回 ZIP——全书内容不会一次性驻留内存。
- **修复循环（repair loop）**：让 LLM 产出满足结构约束的输出，光靠提示词不够，还要「请求 → 校验 → 把错误信息作为反馈追加进对话 → 再请求」。`pdf_craft.llm.loop.run_repair_loop` 是这套机制的通用实现，本讲只看 XMLTranslator 如何使用它，细节留到 u8-l2。
- **jinja2 模板**：Python 最常用的模板引擎。`{{ var }}` 注入变量，`{% if %}` 控制分支。pdf-craft 用它管理提示词，使提示词成为可独立阅读、独立修改的数据文件。
- **为什么「翻译」与「回填」要分成两次 LLM 调用**（本讲最重要的直觉）：如果直接把带 `<b>`、`<span id="5">` 标签的 XML 丢给 LLM 说「翻译它」，模型经常顺手改结构——丢标签、并标签、改标签名。pdf-craft 的解法是拆成两步：
  1. **翻译阶段**只给纯文本，拿回高质量纯文本译文（不带任何标签）；
  2. **回填阶段**给一个「带空位的 XML 模板 + 原文 + 译文」，让另一个（或同一个）LLM 只做「把译文按语义装回模板」这一件机械的事，并用校验器反复修复直到结构完全对。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pdf_craft/transformer/xml_translator/xml_translator/translator.py` | 本讲主角：`TranslationTask`、`XMLTranslator` 编排全流程 |
| `pdf_craft/transformer/xml_translator/xml_translator/submitter.py` | `SubmitKind` 定义与 `submit()` 落地实现 |
| `pdf_craft/transformer/xml_translator/xml_translator/callbacks.py` | `FillFailedEvent` 与回调包装 |
| `pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py` | 流式切分分组（消费 `map` 回调发起翻译） |
| `pdf_craft/transformer/xml_translator/xml_translator/hill_climbing.py` | 回填结果的校验与「只保留更优解」 |
| `pdf_craft/transformer/xml_translator/template.py` | jinja2 环境工具 `create_env`（目录加载、路径防穿越） |
| `pdf_craft/transformer/xml_translator/data/translate.jinja` | 翻译阶段提示词模板 |
| `pdf_craft/transformer/xml_translator/data/fill.jinja` | 回填阶段提示词模板 |
| `pdf_craft/llm/core.py` | `LLM.template()`：实际被调用的模板加载入口 |
| `pdf_craft/llm/runtime.py` | `runtime_for()`：由 LLM 配置构造运行时 |
| `pdf_craft/llm/loop.py` | `run_repair_loop` 修复循环 |
| 两个调用点 | `pdf_craft/transformer/chapter_xml.py`（章级）、`pdf_craft/pipeline/epub/translation/translator.py`（EPUB 管线级） |
| `tests/test_xml_repair_loop.py` | 用假 runtime 单测回填循环的参考写法 |

## 4. 核心概念与源码讲解

### 4.1 任务模型：TranslationTask 与 SubmitKind

#### 4.1.1 概念说明

`XMLTranslator` 不关心「要翻的是一本书的哪一章」还是「EPUB 的目录」，它只接收一个统一的任务描述——`TranslationTask`：

- `element`：待翻译的 XML 树（`Element`），翻译结果会写回这棵树（或其副本）；
- `action`：`SubmitKind` 枚举，决定译文如何落地；
- `payload`：泛型 `T`，调用方随身携带的「回执」。XMLTranslator 全程不打开它，翻译完成后原样随结果返回。

`payload` 是泛型的关键价值：**编排器对业务一无所知**。EPUB 管线塞进去的是 `_ElementContext`（记录这个元素是目录/元数据/章节、以及该写回 ZIP 的哪个位置）；章级转换器塞进去的是 `Chapter` 对象本身。同一条翻译流水线因此可以复用在两种完全不同的上层场景。

`SubmitKind` 三种模式的语义（u7-l1 已从使用侧见过，这里看定义侧）：

| 模式 | 语义 | 典型场景 |
| --- | --- | --- |
| `REPLACE` | 译文**替换**原文文本（保留非 inline 的子元素如表格、图片） | 生成「纯译本」 |
| `APPEND_TEXT` | 译文以**文本**形式**追加**在原文之后（中间注入空格） | 中英对照阅读 |
| `APPEND_BLOCK` | 译文作为**独立块**追加（inline 标签前加空格） | 段落级对照 |

#### 4.1.2 核心流程

任务的生命周期：

```text
调用方构造 TranslationTask(element, action, payload)
        │
        ▼
translate_elements(tasks=...)          ← 任务流（可以是生成器，边翻边产）
        │  记录 id(element) → task 的映射
        ▼
流式切分 → 翻译 → 回填 → submit(element, action, mappings)
        │
        ▼
yield (translated_element, payload)    ← payload 原样归还调用方
```

#### 4.1.3 源码精读

`TranslationTask` 是一个泛型冻结数据类，只有三个字段：

- [pdf_craft/transformer/xml_translator/xml_translator/translator.py:19-23](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L19-L23)：定义 `TranslationTask(Generic[T])`，字段为 `element`、`action`、`payload`——任务模型的全部。

`SubmitKind` 与 `submit()` 的入口：

- [pdf_craft/transformer/xml_translator/xml_translator/submitter.py:11-14](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/submitter.py#L11-L14)：`SubmitKind` 枚举三值 `REPLACE` / `APPEND_TEXT` / `APPEND_BLOCK`。
- [pdf_craft/transformer/xml_translator/xml_translator/submitter.py:17-27](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/submitter.py#L17-L27)：`submit(element, action, mappings)` 把一组「块元素 → 译文文本段」映射按 action 写回，是三种模式的统一落地口。

两个真实调用点，展示 `payload` 泛型的两种用法：

- [pdf_craft/pipeline/epub/translation/translator.py:158-187](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L158-L187)：EPUB 管线的 `_generate_tasks_from_book` 产出三类任务——目录、元数据、每章 `body`——`payload` 都塞 `_ElementContext`（内含类型标签与写回上下文）。注意目录/元数据任务把 `APPEND_BLOCK` 降级为 `APPEND_TEXT`（155-156 行）。
- [pdf_craft/transformer/chapter_xml.py:35-37](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/chapter_xml.py#L35-L37)：章级适配器把 `Chapter` 对象直接当 `payload` 传入 `translate_element`，翻译后 `decode(translated)` 还原成 Chapter。

还有一处值得注意的防御：`translate_element` 若一次产出都没有，会抛 `RuntimeError("Translation failed unexpectedly")`（[translator.py:73](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L73)）。为此章级适配器在调用前先检查章节里是否有可翻译文本，空章节原样返回（[chapter_xml.py:30-34](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/chapter_xml.py#L30-L34)，注释解释了原因：OCR 会产出没有正文的空章）。

#### 4.1.4 代码实践

**实践目标**：体会 `payload` 泛型「编排器不管业务」的设计。

**操作步骤**（源码阅读型实践，无需 LLM 凭据）：

1. 阅读上面两个调用点，确认两类 `payload` 的具体类型。
2. 写一个自己的 payload 并构造任务（示例代码，仅演示数据结构，不发任何请求）：

```python
# 示例代码：仅演示 TranslationTask 的构造，不发起任何 LLM 请求
from xml.etree.ElementTree import Element
from dataclasses import dataclass

from pdf_craft.transformer.xml_translator import SubmitKind, TranslationTask

@dataclass
class MyContext:          # 你自己的「回执」类型
    chapter_title: str
    output_slot: int

task = TranslationTask(
    element=Element("body"),        # 真实场景是 encode(chapter) 产出的元素
    action=SubmitKind.APPEND_TEXT,
    payload=MyContext(chapter_title="绪论", output_slot=3),
)
print(task.action, type(task.payload).__name__)
```

**需要观察的现象**：`TranslationTask` 可以装任何 payload 类型；库对 `T` 没有任何约束。

**预期结果**：打印 `SubmitKind.APPEND_TEXT MyContext`。

**待本地验证**：需安装 pdf-craft 后在仓库根目录运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `payload` 要设计成泛型，而不是固定一个 `context: dict` 字段？

**答案**：泛型让调用方获得类型安全的回执——EPUB 管线拿到 `_ElementContext`、章级转换器拿到 `Chapter`，各自的下游处理无需再拆包转换；同时编排器（XMLTranslator）对业务零依赖，符合「格式中立翻译器」的定位（u7-l1 的 `ChapterXMLTransformer` 正是靠这一点做协议桥接）。

**练习 2**：`SubmitKind.APPEND_BLOCK` 在 EPUB 管线里对目录和元数据任务会发生什么？为什么？

**答案**：被降级为 `APPEND_TEXT`（`_generate_tasks_from_book` 开头的 `head_submit` 逻辑）。目录/元数据是行内文本列表，追加「独立块」会破坏其紧凑的排版结构。另见 u7-l1：`translate_pdf` 预检直接拒绝 `APPEND_BLOCK`，因为 PDF 回写的白块装不下双份块级内容。

**练习 3**：`translate_elements` 里为什么用 `id(task.element)` 建字典，而不是直接 `task.element` 当键？

**答案**：`xml.etree.ElementTree.Element` 不可哈希，不能直接作为 `dict` 键；`id()` 返回对象身份（内存地址），且元素对象在翻译期间始终存活，身份稳定，可安全充当键。

### 4.2 翻译执行器：translate_elements 生成器与双 LLM 运行时

#### 4.2.1 概念说明

`XMLTranslator` 是整个翻译体系的编排器。它的构造函数接收**两个** LLM 配置：

- `translation_llm`：负责「纯文本 → 纯文本译文」，要求译文质量高，可以用擅长文学翻译的模型；
- `fill_llm`：负责「译文 → 装回 XML 模板」，要求结构遵从度高，可以用擅长指令遵循的模型。

两者可以传同一个 `LLM` 实例（EPUB 管线里 `llm` 参数缺省时就是如此），但内部各建一个运行时，`protocol_version` 分别为 `"xml-translation-v1"` 与 `"xml-fill-v1"`。这个版本号参与缓存键计算，所以即使共享同一 LLM，两类请求的缓存也永不混淆；将来提示词协议升级时，改版本号即可让旧缓存整体失效（呼应 u2-l3 的缓存键设计）。

`_XMLProtocol` 是回填阶段的校验器：解析 LLM 回的 `<xml>` 块，交给 `HillClimbing.submit` 校验结构——返回 `None` 即成功；返回错误消息则以 `ProtocolRetry` 携带反馈进入下一轮，同时通过 `on_fill_failed` 回调向调用方汇报 `FillFailedEvent`。

#### 4.2.2 核心流程

从任务输入到提交输出的**五步流程**（本讲实践任务要求你独立写出，先给标准答案）：

```text
① 收集任务    调用方产出 TranslationTask 流；generate_elements 逐个上交 element，
              同时记录 id(element) → task 映射（translator.py L92-95）

② 切分分组    XMLStreamMapper.map_stream 把每个 element 的文本切成 InlineSegment，
              按 token 分数上限 max_group_score 合并成组（u7-l3 详讲）

③ 纯文本翻译  每组调用 _translate_inline_segments：
              组内片段以空行拼接成 source_text →
              _translate_text 用 translation_llm + translate.jinja 一次性翻译

④ 回填修复    _request_and_submit 用 fill_llm + fill.jinja：
              encode_friendly 把组内结构编成 <xml> 模板（含 data-orig-len 长度提示）→
              run_repair_loop + _XMLProtocol + HillClimbing 反复校验修复，
              直到 hill_climbing.submit 返回 None（成功）

⑤ 提交产出    map_stream 按 element 归还映射 → submit(element, action, mappings)
              按 SubmitKind 落地 → yield (translated_element, payload) 给调用方
```

一次翻译调用与一次回填调用的消息形状对比：

| 阶段 | system 消息 | user 消息 | 缓存 |
| --- | --- | --- | --- |
| 翻译 | `translate.jinja` 渲染（含目标语言、可选 `<rules>`） | 纯文本原文 | 走缓存 |
| 回填 | `fill.jinja` 渲染（无变量） | Source text + XML template + Translated text 三段 | 修复请求 `use_cache=False` |

#### 4.2.3 源码精读

**构造函数：双 LLM、双运行时、流式映射器**

- [pdf_craft/transformer/xml_translator/xml_translator/translator.py:26-52](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L26-L52)：`XMLTranslator.__init__` 用 `runtime_for` 分别为两个 LLM 建运行时（41-42 行，注意两个不同的 `protocol_version`），并创建 `XMLStreamMapper`（49-52 行，编码器与分组上限来自翻译 LLM 配置）。
- [pdf_craft/llm/runtime.py:169-170](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L169-L170)：`runtime_for(config, protocol_version)` 工厂。
- [pdf_craft/llm/runtime.py:152-158](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L152-L158)：`_cache_key` 的 payload 里包含 `protocol` 与 `seed`——这就是双运行时缓存隔离、以及 EPUB 管线用 `cache_seed_content=f"{版本}:{target_language}"`（[pipeline/epub/translation/translator.py:71](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L71)）让「换目标语言即换缓存」的机制根源。

**translate_elements：生成器主流程**

- [pdf_craft/transformer/xml_translator/xml_translator/translator.py:75-113](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L75-L113)：核心编排。84 行建 `element2task` 映射；92-95 行 `generate_elements` 内嵌生成器边收任务边上交元素；97-105 行把「翻译一组 inline 片段」的闭包 `_translate_inline_segments` 作为 `map` 回调交给 `map_stream`（并发度 `concurrency` 一路透传）；106-113 行每当某 element 的全部映射到齐，就 `submit` 并 `yield (译文元素, payload)`。
- [pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py:29-69](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L29-L69)：`map_stream` 按「origin 元素身份变化」判断当前元素翻译完毕（56-59 行），把缓冲的映射整批归还——这是「按元素产出」的实现位置。
- [pdf_craft/transformer/xml_translator/xml_translator/translator.py:54-73](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L54-L73)：单任务便捷方法 `translate_element`，把一个任务包成单元素元组委托给 `translate_elements`，取第一个产出返回。

**第③步：纯文本翻译**

- [pdf_craft/transformer/xml_translator/xml_translator/translator.py:115-145](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L115-L145)：`_translate_inline_segments` 是 `map` 回调的实现：构造 `HillClimbing`（回填校验器）、拼 `source_text`、调 `_translate_text` 翻译、`_request_and_submit` 回填，最后 `gen_mappings` 产出映射；137-143 行把「译文为空文本段」的映射归一成 `None`（表示该片段放弃翻译）。
- [pdf_craft/transformer/xml_translator/xml_translator/translator.py:147-152](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L147-L152)：`_render_source_text_parts` 把组内各 inline 片段用 `\n\n`（空行）拼接——空行即段落边界，与回填提示词「元素是天然分隔符」的规则呼应。
- [pdf_craft/transformer/xml_translator/xml_translator/translator.py:154-167](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L154-L167)：`_translate_text`——system 消息由 `self._translation_llm.template("translate").render(target_language=..., user_prompt=...)` 渲染（160-163 行，4.3 节详讲），user 消息就是纯文本原文；经由 `_translation_runtime.context(...)` 发出，默认走缓存。

**第④步：回填修复**

- [pdf_craft/transformer/xml_translator/xml_translator/translator.py:169-190](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L169-L190)：`_request_and_submit` 组装回填消息——system 是 `fill` 模板（无变量，184 行），user 是三段式 `Source text: / XML template: / Translated text:`（176-180 行），其中模板由 `encode_friendly(hill_climbing.request_element())` 生成。
- [pdf_craft/transformer/xml_translator/xml_translator/hill_climbing.py:34-40](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/hill_climbing.py#L34-L40)：`request_element` 生成模板并给每个子元素标 `data-orig-len`（原文 token 数提示，见 [common.py:1](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/common.py#L1)），fill.jinja 会告诉 LLM 用它区分「书名」与「年份」。
- [pdf_craft/transformer/xml_translator/xml_translator/translator.py:194-219](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L194-L219)：内联类 `_XMLProtocol` 实现修复循环协议——`validate` 解析并提交校验（成功返回 `ProtocolSuccess`；失败构造 `ProtocolRetry(error, ..., include_response=True, reset_history=True)` 并发 `FillFailedEvent`）；`empty` 处理空响应；`exhausted` 在重试耗尽时发 `over_maximum_retries=True` 的事件并返回 `None`（放弃而非抛异常，保住已爬到的最优解）。
- [pdf_craft/transformer/xml_translator/xml_translator/translator.py:221-227](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L221-L227)：调用 `run_repair_loop`，注意请求闭包里 `use_cache=False`——修复请求携带错误反馈、每次都不同，缓存它们没有意义（与 u2-l3 的结论对上）。
- [pdf_craft/llm/loop.py:61-87](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/loop.py#L61-L87)：`run_repair_loop` 通用循环：每轮把「assistant 的坏答案 + user 的错误反馈」追加进对话（83-86 行，历史上限 `history_limit=2` 条），让模型看着自己上次的错误改。
- [pdf_craft/transformer/xml_translator/xml_translator/hill_climbing.py:55-72](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/hill_climbing.py#L55-L72)：`HillClimbing.submit` 校验一轮回填，逐块只保留「完成度权重更低（更优）」的提交，返回整体错误消息或 `None`（全部通过）。类注释（20-22 行）点明策略：每个子部分只允许向更高完成度移动。u7-l5 会专讲。
- [pdf_craft/transformer/xml_translator/xml_translator/translator.py:229-246](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L229-L246)：`_extract_xml_element` 用 `decode_friendly(text, tags="xml")` 从回复中提取 `<xml>` 块：零个 → 提示「请闭合标签」；多于一个 → 提示「只回一个块」；恰好一个 → 返回元素。这三条错误消息就是修复循环里的反馈文本。

**回调与事件**

- [pdf_craft/transformer/xml_translator/xml_translator/callbacks.py:8-12](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/callbacks.py#L8-L12)：`FillFailedEvent(error_message, retried_count, over_maximum_retries)`——回填失败的三要素。
- [pdf_craft/transformer/xml_translator/xml_translator/callbacks.py:23-34](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/callbacks.py#L23-L34)：`warp_callbacks` 把四个可选钩子（源文片段拦截、译文片段拦截、块元素拦截、失败通知）补上恒等默认值，下游代码从此不必判空（函数名拼作 warp 而非 wrap，阅读源码时别以为是笔误）。

#### 4.2.4 代码实践

**实践目标**：追踪 `translate_elements` 的两个调用点，独立写出五步流程说明（这正是本讲规格中的实践任务）。

**操作步骤**：

1. 打开 [pdf_craft/transformer/chapter_xml.py:28-38](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/chapter_xml.py#L28-L38)，确认章级路径：`encode(chapter)` → 空章守卫 → `translate_element` → `decode`。
2. 打开 [pdf_craft/pipeline/epub/translation/translator.py:99-113](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L99-L113)，确认管线级路径：`translate_elements(tasks=生成器, concurrency=..., on_fill_failed=...)` 的产出被逐个消费、按 `_ElementType` 分流写回 ZIP。
3. 对照 4.2.2 的五步流程，遮住答案，用自己的话写一份 150 字左右的流程说明，并给每一步标注 translator.py 的行号范围。
4. 阅读 [tests/test_xml_repair_loop.py:1-60](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_xml_repair_loop.py#L1-L60)：注意它如何用 `object.__new__(XMLTranslator)` 绕过构造函数、再用假 `_Runtime`（预置响应序列）和假 `_Hill`（预置错误序列）把 LLM 整个替换掉——这是不花钱单测 LLM 编排逻辑的标准手法。

**需要观察的现象**：两个调用点一个用单任务便捷方法、一个用批量生成器；`payload` 类型不同但流程完全一致。

**预期结果**：你能不看讲义复述五步，并指出「翻译走缓存、回填修复不走缓存」分别对应 translator.py 的 154-167 行与 221-227 行。

**待本地验证**：第 4 步的测试可直接 `python -m unittest tests.test_xml_repair_loop` 运行验证（不耗 token）。

#### 4.2.5 小练习与答案

**练习 1**：`translation_llm` 和 `fill_llm` 传同一个 `LLM` 实例时，两类请求的缓存会串吗？

**答案**：不会。构造函数为两者分别建立运行时，`protocol_version` 一个是 `"xml-translation-v1"`、一个是 `"xml-fill-v1"`，而 `LLMRuntime._cache_key` 把 `protocol` 计入哈希（runtime.py L152-158），两个命名空间的键永不相同。

**练习 2**：为什么回填阶段的修复请求要 `use_cache=False`，而翻译阶段默认走缓存？

**答案**：翻译阶段的输入（原文+目标语言）是确定的，同样的输入必然期望同样的译文，缓存命中省时省钱；修复阶段的每轮请求都附加了上一轮的错误反馈，消息逐轮变化，缓存键各不相同、命中率趋近于零，写缓存反而占磁盘。这也是 u2-l3 讲过的「仅完整成功的结果落盘」的具体体现。

**练习 3**：`_XMLProtocol.exhausted` 在重试耗尽时返回 `None` 而不是抛异常，最终翻译结果会怎样？

**答案**：`run_repair_loop` 把 `None` 作为返回值结束循环，`_request_and_submit` 不再触发异常；`HillClimbing` 保留历轮中「完成度最高」的部分提交，`gen_mappings` 对从未成功过的片段产 `None`（translator.py L137-143 进一步把空文本段也归一为 `None`），于是这些片段保持原文不译。整本书的翻译不会因个别组耗尽重试而中断——失败被降级为「该片段放弃翻译」，并通过 `FillFailedEvent(over_maximum_retries=True)` 通知调用方。

### 4.3 提示词模板：从 jinja 文件到 system 消息

#### 4.3.1 概念说明

提示词是 LLM 应用里变更最频繁的资产。pdf-craft 把两份核心提示词做成数据文件放在 `data/` 目录，运行时按名加载、按需渲染：

- `translate.jinja`：翻译阶段的 system 提示词，**有两个变量**——`target_language`（目标语言）与 `user_prompt`（用户附加要求，可空）。
- `fill.jinja`：回填阶段的 system 提示词，**没有变量**，是一份静态的「结构守则 + 常见错误清单 + 填充算法」说明书。

注意实际的加载入口并不在 `template.py`，而在 `LLM.template()`（u2-l3 说过「LLM 兼任模板宿主」）：它从 `pdf_craft/transformer/xml_translator/data/{模板名}.jinja` 读文件、构造 `jinja2.Template` 并按名缓存。`template.py` 里的 `create_env` 是同包提供的目录级 jinja2 环境工具（带路径防穿越校验），当前代码库中没有调用点，但它是「如何安全地从目录加载模板」的参考实现。

#### 4.3.2 核心流程

```text
LLM.template("translate")
  读 pdf_craft/transformer/xml_translator/data/translate.jinja
  构造 jinja2.Template，按名缓存（同一 LLM 实例只读一次盘）
        │
        ▼  .render(target_language="简体中文", user_prompt=None 或用户文本)
system 消息字符串
        │
        ▼  与 user 消息（纯文本原文）一起发给 translation_llm
```

`translate.jinja` 内部的分支逻辑：

```text
固定规则（完整翻译、保真、不总结、不审查……）
        │
        ├── user_prompt 为空  → 直接输出「只输出译文」收尾
        └── user_prompt 非空  → 追加一段 <rules>…</rules>，
                                并声明：与上方固定规则冲突时以上方为准
```

#### 4.3.3 源码精读

**加载入口**

- [pdf_craft/llm/core.py:30-36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/core.py#L30-L36)：`LLM.template(template_name)` —— 33 行硬编码定位到 `transformer/xml_translator/data/{name}.jinja`，34 行 `Template(path.read_text(encoding="utf-8"))` 直接构造（不经 `create_env`，因此无 loader、无 autoescape），35 行存入实例缓存字典。

**翻译提示词**

- [pdf_craft/transformer/xml_translator/data/translate.jinja:1-12](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/data/translate.jinja#L1-L12)：开头一句角色设定 + `{{ target_language }}` 注入点（第 1 行），随后 8 条翻译规则——完整翻译不省略、保真不增删、逐句保段落、不总结、不纠错、不审查敏感内容、不加解释、**只输出纯文本**（第 11 行，这条正是「翻译阶段不带标签」的提示词侧保障）。
- [pdf_craft/transformer/xml_translator/data/translate.jinja:13-19](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/data/translate.jinja#L13-L19)：`{% if user_prompt -%}` 分支——把用户附加要求包进 `<rules>` 标签注入，并明确「与上方规则冲突时以上方优先」。这解释了本讲学习目标里「user_prompt 如何注入」：它不是拼在末尾，而是结构化地嵌进 system 消息中部。
- [pdf_craft/transformer/xml_translator/data/translate.jinja:21](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/data/translate.jinja#L21)：收尾一句「只输出译文，别的什么都不要」。

**回填提示词**

- [pdf_craft/transformer/xml_translator/data/fill.jinja:1-12](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/data/fill.jinja#L1-L12)：角色定位「XML 结构校验器」，最高准则是**结构保持**——同标签同顺序同嵌套同属性（尤其 id），并明说「翻译流畅度是第二位的，必要时为了结构牺牲流畅」。
- [pdf_craft/transformer/xml_translator/data/fill.jinja:28-91](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/data/fill.jinja#L28-L91)：七类常见错误的 ❌/✓ 对照示例——丢块、非 id 元素数量不符、乱加 id、改标签名、漏 id、语序变化后按位置匹配（错）与按语义匹配（对）等；87-90 行点题「语义类型匹配胜过位置匹配」，90 行提到用 `data-orig-len` 提示长度。
- [pdf_craft/transformer/xml_translator/data/fill.jinja:94-124](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/data/fill.jinja#L94-L124)：四步填充算法——数结构、按元素切原文、严格结构匹配地套到译文、最后自查；124 行「Template structure is LAW」。
- [pdf_craft/transformer/xml_translator/data/fill.jinja:158-171](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/data/fill.jinja#L158-L171)：输出格式约定——只回一个 ```xml <xml>…</xml>``` 代码块、不给解释、不给备选。这与 `_extract_xml_element` 的「恰好一个 `<xml>` 块」校验（translator.py L229-246）构成提示词与解析器的闭环。

**目录加载工具（参考实现）**

- [pdf_craft/transformer/xml_translator/template.py:8-14](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/template.py#L8-L14)：`create_env(dir_path)` 创建绑定自定义 loader 的 jinja2 环境。
- [pdf_craft/transformer/xml_translator/template.py:34-42](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/template.py#L34-L42)：`_norm_template` 拒绝 `../` 形式的相对路径（防目录穿越）、统一补 `.jinja` 后缀——如果你未来想把提示词做成可外部覆盖的目录，这里是现成的安全加载范式（当前无调用点，属工具储备）。

#### 4.3.4 代码实践

**实践目标**：亲手渲染两份提示词，看清变量注入的真实产物（本讲规格实践任务的前半）。

**操作步骤**：

1. 在仓库根目录运行以下脚本（示例代码，绕过 `LLM.template` 以免构造 tiktoken 编码器触发网络下载；渲染结果与 `LLM(...).template("translate").render(...)` 完全一致）：

```python
# 示例代码：render_prompts.py —— 手动渲染 xml_translator 的两份提示词
from pathlib import Path
from jinja2 import Template

DATA = Path("pdf_craft/transformer/xml_translator/data")

# 1) 翻译提示词：注入目标语言与用户附加要求
translate_tpl = Template((DATA / "translate.jinja").read_text(encoding="utf-8"))
msg = translate_tpl.render(
    target_language="简体中文",
    user_prompt="人名保留英文原文；术语「token」不翻译。",
)
print("=== translate.jinja 渲染结果 ===")
print(msg)

# 2) 同一模板，不传 user_prompt，观察 {% if %} 分支消失
print("=== 无 user_prompt 时末尾 10 行 ===")
print("\n".join(translate_tpl.render(target_language="English").splitlines()[-10:]))

# 3) 回填提示词：无变量，原样渲染
fill_tpl = Template((DATA / "fill.jinja").read_text(encoding="utf-8"))
print("=== fill.jinja 首行 ===")
print(fill_tpl.render().splitlines()[0])
```

2. 对比第 1、2 步输出：带 `user_prompt` 时中段出现 `<rules>…</rules>` 块，不带时该块连同前置说明整体消失。
3. 把渲染出的翻译提示词与 4.2 节的三段式回填 user 消息（Source text / XML template / Translated text）拼在一起，你就得到了 XMLTranslator 每组翻译的**完整请求原文**。

**需要观察的现象**：`{{ target_language }}` 出现在第 1 行句中；`<rules>` 块只受 `user_prompt` 控制；fill.jinja 渲染前后无差别。

**预期结果**：得到两段可直接投喂任何聊天模型的提示词文本。

**待本地验证**：脚本只读本地文件，无需凭据；在仓库根目录 `python render_prompts.py` 即可运行。

#### 4.3.5 小练习与答案

**练习 1**：`translate.jinja` 为什么要声明「用户规则与固定规则冲突时以固定规则优先」？

**答案**：翻译质量的红线（完整、保真、不省略、不总结、输出纯文本）是回填阶段能正常工作的前提——如果用户要求「简单意译」，译文一旦缩水，回填模板就对不上原文结构，整个两阶段方案失效。把用户提示约束在「不破坏红线」的范围内，是编排器对不可控输入的防御。

**练习 2**：`fill.jinja` 中「Error Type 6/7」（语序变化）为什么要举中英对照的例子？

**答案**：中英互译是语序重排最剧烈的场景（英文后置的书名/年份译成中文后常前置）。按位置匹配会把「年份」填进「书名」的槽位，这种错误校验器很难发现（结构完全合法）；提示词必须教会模型「按语义类型匹配槽位」，并用具体例子固化这一行为。

**练习 3**：`LLM.template` 用 `Template(path.read_text())` 直接构造，没有启用 jinja2 的 autoescape，这会有问题吗？

**答案**：不会。autoescape 是为防 HTML/XSS 注入设计的，会把 `{{ var }}` 里的 `<`、`>` 转义成实体；这里渲染产物是发给 LLM 的纯文本提示词，反而**需要** `<rules>`、XML 片段原样出现，启用转义会破坏提示词语义。（顺带一提，`create_env` 里 `select_autoescape()` 默认只对 html/xml 后缀启用转义，对 `.jinja` 后缀同样不生效。）

## 5. 综合实践

把本讲三个模块串成一个离线可完成的任务：**不花一个 token，验证回填解析器并产出流程文档**。

1. **渲染提示词**（4.3）：运行 4.3.4 的脚本，保存两份渲染结果。
2. **单测解析器**（4.2）：模仿 [tests/test_xml_repair_loop.py:1-60](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_xml_repair_loop.py#L1-L60) 的 mock 手法，为 `_extract_xml_element` 写三个断言用例（示例代码框架）：

```python
# 示例代码：test_extract_xml.py
import unittest
from xml.etree.ElementTree import Element

from pdf_craft.transformer.xml_translator.xml_translator.translator import XMLTranslator

translator = object.__new__(XMLTranslator)  # 绕过 __init__，避免构造 LLM

class TestExtract(unittest.TestCase):
    def test_no_block(self):
        result = translator._extract_xml_element("抱歉，我无法完成。")
        self.assertIsInstance(result, str)          # 返回错误消息而非元素
        self.assertIn("</xml>", result)             # 反馈里指导模型闭合标签

    def test_multiple_blocks(self):
        text = "<xml><p>a</p></xml>\n<xml><p>b</p></xml>"
        self.assertIn("2", translator._extract_xml_element(text))

    def test_single_block(self):
        result = translator._extract_xml_element('<xml><p id="1">译</p></xml>')
        self.assertIsInstance(result, Element)

if __name__ == "__main__":
    unittest.main()
```

   运行 `python -m unittest test_extract_xml -v`，三个用例应全绿（待本地验证）。

3. **写流程说明**（4.2）：合上讲义，写一份 150 字左右的「XMLTranslator 从任务输入到提交输出的五步流程说明」，每步附 translator.py 行号。
4. **闭环检查**（4.1）：在说明末尾回答——如果 `payload` 不是泛型，EPUB 管线与章级转换器还能共用同一个 `XMLTranslator` 吗？多写什么代码？

## 6. 本讲小结

- `TranslationTask(element, action, payload)` 是格式中立的翻译任务模型：`payload` 泛型让 EPUB 管线（`_ElementContext`）与章级转换器（`Chapter`）共用同一编排器。
- `SubmitKind` 三种落地（`REPLACE` / `APPEND_TEXT` / `APPEND_BLOCK`）由 `submit()` 统一执行；EPUB 管线对目录/元数据把 `APPEND_BLOCK` 降级为 `APPEND_TEXT`。
- `translate_elements` 是生成器：五步流程为「收集任务 → 切分分组 → 纯文本翻译 → 回填修复 → 按模式提交产出」，按元素粒度流式 yield，全书内容不驻留内存。
- 双 LLM 设计的实质是**两个任务两种能力**：翻译要质量、回填要结构遵从；即使共用一个 LLM 实例，双 `protocol_version` 也保证缓存命名空间隔离。
- 翻译请求走缓存、回填修复请求 `use_cache=False`；修复循环把「坏答案 + 错误反馈」追加进对话重试，耗尽后降级为「该片段保持原文」而非报错中断。
- 提示词即数据：`translate.jinja` 以 `{{ target_language }}` 与可选 `<rules>{{ user_prompt }}</rules>` 注入，`fill.jinja` 是静态结构守则；`LLM.template()` 按名加载并缓存，实际加载点在 `llm/core.py` 而非 `template.py`。

## 7. 下一步学习建议

本讲只把 `XMLStreamMapper` 与 `HillClimbing` 当黑盒用了。下一讲 **u7-l3（文本片段与序列切分）** 打开第一个黑盒：`TextSegment` / `InlineSegment` / `BlockSegment` 三类片段如何划分、token 分数如何计算、`resource_segmentation` 的切分边界为何总落在标签之外。之后再进 **u7-l4（友好 XML 编解码与流式映射）** 看 `encode_friendly` / `decode_friendly` 与 `XMLStreamMapper` 本体，**u7-l5（评分、爬山修复与提交策略）** 深挖 `HillClimbing`、`validation.py` 与 `submitter.py` 的落地细节。若你想先补 LLM 侧地基，可跳读 **u8-l2（修复循环：协议驱动的重试）**，那里系统讲 `run_repair_loop` 与 `ResponseProtocol` 的四种判定结果。
