# u2-l3 LLM 配置与 token 编码

## 1. 本讲目标

在前两讲里，我们配置好了 OCR 后端（u2-l1）与提取选项（u2-l2），它们服务于「PDF → 中间包」这一段。本讲把视线移到链路上的另一种外部服务——**文本 LLM**。学完本讲，你应该能够：

1. 熟练构造 `pdf_craft.llm.LLM` 配置对象，说出每个构造参数的含义与默认值。
2. 理解 `token_encoding` 与 tiktoken 的关系：为什么客户端要在本地数 token，这些 token 数又用在了哪里（翻译分组预算）。
3. 会用 `cache_path` 与 `log_dir_path` 让翻译任务「可复算、可排查」：重跑不重复付费，出错能定位到具体请求。

本讲只讲**配置对象本身**；请求如何执行、重试循环与修复协议属于第 8 单元（u8-l1、u8-l2），本讲只提前照面、不展开。

## 2. 前置知识

### 2.1 什么是「OpenAI 兼容」的 LLM 服务

pdf-craft 的翻译能力依赖一个文本生成服务。它不绑定某一家厂商，而是要求服务暴露 **OpenAI 兼容的 Chat Completions 接口**。接入这样一个服务需要三样东西，正好对应 `LLM` 的三个必填参数：

- `key`：API 密钥，证明「你是谁」；
- `url`：服务基地址，例如 `https://api.openai.com/v1`，指明「去哪里请求」；
- `model`：模型标识符，例如 `gpt-4.1-mini`，指明「用哪个模型」。

### 2.2 token 与 tiktoken

**token** 是大语言模型处理文本的原子单位，也是绝大多数厂商的计费与长度限制单位。它不等于字符：英文里一个 token 约等于 0.75 个单词，中文里一个汉字通常是 1 到 2 个 token（取决于具体模型）。

**tiktoken** 是 OpenAI 开源的 Python 分词器库。给它一个编码名（如 `o200k_base`），它就能在**本地**把任意字符串切成 token 列表——不需要联网调用 LLM，也不产生费用。pdf-craft 用它来「预估」一段文本会消耗多少 token，从而决定一次翻译请求打包多少内容。

> 小提醒：tiktoken 在首次使用某个编码名时需要联网下载该编码的 BPE 数据文件，之后会在本机缓存、离线可用。这一点待本地验证（取决于你的环境是否已有缓存）。

### 2.3 声明式配置：与前两讲的延续

回忆 u2-l1 的 OCR 配置：六个 frozen dataclass 描述「用哪个模型、跑在哪」。LLM 配置是同一种思路——**配置对象只描述「是什么」，不执行任何请求**；真正发请求的是第 8 单元会精读的 `LLMRuntime`。另外一个关键区别：OCR 配置服务于以 PDF 为输入的工作流，而 LLM 配置连 PDF 都不需要——翻译一本现成 EPUB 时，完全没有 OCR 什么事。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pdf_craft/llm/core.py` | **本讲主角**：`LLM` 配置类与 `_directory` 辅助函数 |
| `pdf_craft/llm/runtime.py` | 消费配置的运行时：缓存键、重试循环、日志落盘的具体实现都在这里 |
| `pdf_craft/llm/__init__.py` | llm 子包的公开导出边界 |
| `pdf_craft/transformer/xml_translator/xml_translator/translator.py` | 配置的消费者：XMLTranslator 如何取用 `encoding`、模板与双 LLM |
| `pdf_craft/transformer/xml_translator/xml_translator/score.py` | 用 encoding 给文本片段打 token 分数的核心逻辑 |
| `pdf_craft/pipeline/epub/translation/translator.py` | `translate_epub` 参数如何映射到 LLM 配置 |
| `docs/en/EPUB_TRANSLATION.md` | 官方文档：LLM 字段表、缓存与日志说明 |

## 4. 核心概念与源码讲解

### 4.1 LLM 配置：一个类装下连接、采样与运维

#### 4.1.1 概念说明

OCR 侧有六个配置类（三族模型 × 两种运行位置），而 LLM 侧只有一个类：`LLM`。因为文本服务的差异远比 OCR 小——大家都暴露 OpenAI 兼容接口，不同的只是地址、密钥、模型名和采样偏好。

`LLM` 的参数可以分成三组：

| 分组 | 参数 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 连接（必填） | `key` / `url` / `model` / `token_encoding` | 无默认 | 接入哪个服务、哪个模型、用哪个分词编码 |
| 请求行为 | `timeout` / `top_p` / `temperature` | `None` | 单请求超时秒数与采样控制 |
| 运维策略 | `retry_times` / `retry_interval_seconds` / `cache_path` / `log_dir_path` | `5` / `6.0` / `None` / `None` | 重试次数与间隔、缓存目录、日志目录 |

值得注意的两个设计细节：

1. **构造即建目录**：传入 `cache_path` 或 `log_dir_path` 时，目录在构造函数里就被立即创建（`mkdir(parents=True, exist_ok=True)`），而不是等到第一次请求。
2. **配置对象兼任模板宿主**：`LLM` 还提供了一个 `template()` 方法，用来加载 XML 翻译器的 jinja 提示词模板。这是为了让 `XMLTranslator` 能直接从配置对象拿到模板（第 7 单元详解）。

#### 4.1.2 核心流程

构造一个 `LLM` 对象时发生的事（伪代码）：

```text
LLM(key, url, model, token_encoding, ...)
  ├─ 1. 平铺保存连接参数、采样参数、重试参数
  ├─ 2. cache_path / log_dir_path → _directory()：
  │       是 None → 保持 None（不启用）
  │       否则     → 立即递归创建目录，返回绝对 Path
  ├─ 3. get_encoding(token_encoding) → 得到 tiktoken 编码器并缓存到 _encoding
  └─ 4. 初始化空的模板缓存字典 _templates
（全程不发起任何 LLM 请求）
```

#### 4.1.3 源码精读

先看构造函数签名，注意四个必填参数与默认值：

[pdf_craft/llm/core.py:8-17](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/core.py#L8-L17)

类文档字符串一句话点破了配置与执行的分离：`LLM` 是声明式配置，执行由 `LLMRuntime` 提供。

构造体做了四件事——保存参数、归一化目录、加载编码器、准备模板缓存：

[pdf_craft/llm/core.py:18-24](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/core.py#L18-L24)

第 21-22 行把两个目录参数交给 `_directory` 处理；第 23 行 `get_encoding(token_encoding)` 在构造期就把 tiktoken 编码器准备好（本讲 4.2 的主角）。

`encoding` 是个只读属性，外部（XMLTranslator）正是通过它拿编码器去数 token：

[pdf_craft/llm/core.py:26-28](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/core.py#L26-L28)

目录归一化辅助函数——注意第 43 行的急切 `mkdir`，以及最终返回**解析后的绝对路径**：

[pdf_craft/llm/core.py:39-44](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/core.py#L39-L44)

模板加载方法——模板文件实际存放在 `transformer/xml_translator/data/` 目录下，按名缓存：

[pdf_craft/llm/core.py:30-36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/core.py#L30-L36)

`LLM` 通过 llm 子包导出（`pdf_craft/llm/__init__.py:1`），并同时出现在库的顶层公开 API 中（[pdf_craft/__init__.py:23](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/__init__.py#L23)），所以 `from pdf_craft import LLM` 即可使用。

官方文档给出的字段表与本节表格一致，并强调了重试语义——`retry_times=5` 指**首次请求之后最多再重试五次**（共最多六次尝试）：

[docs/en/EPUB_TRANSLATION.md:76-89](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/EPUB_TRANSLATION.md#L76-L89)

文档还提供了一个进阶用法预告：翻译正文与修复 XML 结构可以用两个不同的 `LLM`（`translation_llm` 与 `fill_llm`），详见 [docs/en/EPUB_TRANSLATION.md:116-137](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/EPUB_TRANSLATION.md#L116-L137)。管线侧的兜底逻辑很直白——`translation_llm = translation_llm or llm`、`fill_llm = fill_llm or llm`，两者都缺时报错（[pdf_craft/pipeline/epub/translation/translator.py:47-60](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L47-L60)）。

#### 4.1.4 代码实践

**实践目标**：不花一分钱，验证 `LLM` 构造过程的三个行为——不发请求、目录急切创建、参数原样保存。

**操作步骤**：

```python
# 示例代码：llm_config_probe.py
from pathlib import Path
from pdf_craft import LLM

llm = LLM(
    key="sk-fake-key-not-real",        # 假密钥：构造阶段不会校验
    url="https://api.openai.com/v1",
    model="gpt-4.1-mini",
    token_encoding="o200k_base",
    timeout=120,
    cache_path="tmp/llm-cache",
    log_dir_path="tmp/llm-logs",
)

print("model:", llm.model)
print("retry:", llm.retry_times, "次，间隔", llm.retry_interval_seconds, "秒")
print("cache_path 已解析为绝对路径:", llm.cache_path)
print("log_dir_path:", llm.log_dir_path)
```

**需要观察的现象**：

1. 脚本是否在**没有任何 API key 有效性的情况下**正常结束（即构造不发请求）。
2. 运行前后 `tmp/llm-cache`、`tmp/llm-logs` 两个目录是否被自动创建。

**预期结果**：脚本打印出模型名、重试参数（`5 次，间隔 6.0 秒`）与两个绝对路径；两个目录在构造后立即出现在磁盘上。待本地验证：若环境从未用过 `o200k_base`，第 10 行会触发 tiktoken 的编码文件下载（需网络）。

#### 4.1.5 小练习与答案

**练习 1**：`LLM` 与 OCR 配置类（如 `DeepSeekOCRVendorConfig`）在「类的设计」上有什么明显不同？

<details><summary>参考答案</summary>

OCR 侧按「模型族 × 运行位置」拆成六个 frozen dataclass，字段各不相同；LLM 侧只有**一个普通类**，因为所有兼容 OpenAI 接口的服务共享同一组连接参数（key/url/model），差异可以全部塞进一个构造函数的可选参数里。此外 OCR 配置类是 frozen 的，而 `LLM` 没有加冻结约束（但它同样只在构造期做归一化，运行期属性基本只读）。
</details>

**练习 2**：如果把 `cache_path` 传成一个**已存在文件**的路径（而不是目录），构造会发生什么？

<details><summary>参考答案</summary>

`_directory` 会先执行 `result.mkdir(parents=True, exist_ok=True)`。对一个已存在的**文件**路径，`mkdir` 会抛 `FileExistsError`（`exist_ok` 只容忍已存在的**目录**），构造直接失败。这体现了「急切建目录」策略：配置错误在构造期就暴露，而不是拖到第一次请求。
</details>

**练习 3**：为什么 `LLM` 类里会有一个 `template()` 方法去读 `transformer/xml_translator/data/` 下的模板？这样设计的好处是什么？

<details><summary>参考答案</summary>

XMLTranslator 需要用 jinja 模板渲染提示词（`translate.jinja`、`fill.jinja`），而它持有的是 `LLM` 配置对象，于是模板加载与按名缓存的能力被放到了 `LLM` 上（[pdf_craft/llm/core.py:30-36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/core.py#L30-L36)）。好处是 `XMLTranslator` 不必再依赖一个模板管理器对象——配置对象「自带弹药」，调用侧只需传一个 `LLM`。
</details>

### 4.2 分词编码：token_encoding 与翻译分组

#### 4.2.1 概念说明

`token_encoding` 是四个必填参数里最容易被初学者忽视的一个。它解决的问题：**pdf-craft 要把整本书切成一批批发给 LLM 翻译，每批多大合适？**

批太小 → 请求次数多、慢、上下文破碎；批太大 → 单次请求贵，而且一旦失败整批重来代价高。官方默认把每组的预算定为 2600 token（`max_group_tokens`，见 [docs/en/EPUB_TRANSLATION.md:106-114](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/EPUB_TRANSLATION.md#L106-L114)）。要执行这个预算，客户端必须先能回答「这段文本有几个 token」——这就是 tiktoken 的职责，也是 `token_encoding` 存在的意义。

需要强调：tiktoken 的计数是**本地估算**。它并不和你选的模型通信，只是用同名（或相近）的词表把文本切一遍。即便你的服务商用别的分词器，这个估算仍足以完成「分组预算」这类粗粒度任务。

#### 4.2.2 核心流程

编码器从配置流向分组器的路径：

```text
LLM(token_encoding="o200k_base")
  └─ 构造期 get_encoding() → self._encoding
       └─ XMLTranslator.__init__ 取 translation_llm.encoding
            └─ 传给 XMLStreamMapper(encoding=..., max_group_score=2600)
                 └─ 对每个文本片段调用 expand_to_score_segments(encoding=...)
                      └─ score = len(encode(片段的 XML 渲染)) + 80 × 带编号父层数
                           └─ 按 max_group_score 把片段切成一个个「翻译组」
```

片段分数的数学表达：

\[
\text{score}(s) = \left| E(\text{xml}(s)) \right| + 80 \cdot \left| \{\, p \in \text{left\_parents}(s) \mid p.\text{id} \neq \varnothing \,\} \right|
\]

其中 \( E \) 是 tiktoken 编码函数，\(|\cdot|\) 表示 token 数或集合大小。也就是说：一段文本的「开销」等于它连同包裹标签渲染成 XML 后的 token 数，再加上每个带 `id` 的祖先标签固定记 80 分（`_ID_WEIGHT = 80`，因为带编号的标签回填时要做对齐，值得在预算里多占权重）。

除了分组，编码器还有第二用途：修复阶段给每个块打上「原文长度」标记，供译文完整性校验参考（见 4.2.3 最后一处引用）。

#### 4.2.3 源码精读

构造期加载编码器（core.py 第 23 行），并以属性暴露：

[pdf_craft/llm/core.py:23-28](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/core.py#L23-L28)

`XMLTranslator` 在构造时把**翻译用 LLM** 的编码器交给流式分组器（第 50 行），把**修复用 LLM** 的编码器交给爬山修复器（第 121 行）——两个编码器可以不同：

[pdf_craft/transformer/xml_translator/xml_translator/translator.py:49-52](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L49-L52)

[pdf_craft/transformer/xml_translator/xml_translator/translator.py:120-122](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L120-L122)

分组器持有个编码器与预算上限两个字段（`max_group_tokens` 从 EPUB 管线一路传到这里的 `max_group_score`，见 [pdf_craft/pipeline/epub/translation/translator.py:70](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L70)）：

[pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py:24-27](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/stream_mapper.py#L24-L27)

token 计数真正发生的地方——第 31 行数正文 token，第 32-34 行按上面的公式算片段分数：

[pdf_craft/transformer/xml_translator/xml_translator/score.py:31-34](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/score.py#L31-L34)

`_ID_WEIGHT` 常量定义在文件头部：

[pdf_craft/transformer/xml_translator/xml_translator/score.py:10-11](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/score.py#L10-L11)

编码器的第二用途：修复请求构造模板元素时，给每个子块写入原文 token 数：

[pdf_craft/transformer/xml_translator/xml_translator/hill_climbing.py:34-40](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/hill_climbing.py#L34-L40)

#### 4.2.4 代码实践

**实践目标**：用假密钥的 `LLM` 对一段中英混合文本计数 token，直观感受「token ≠ 字符」，并体会分组预算的含义。

**操作步骤**：

```python
# 示例代码：token_count.py
from pdf_craft import LLM

llm = LLM(
    key="sk-fake", url="https://api.openai.com/v1",
    model="gpt-4.1-mini", token_encoding="o200k_base",
)

samples = [
    "The quick brown fox jumps over the lazy dog.",
    "大语言模型正在把纸质书籍变成可检索的知识。",
    "PDF 转换为 Markdown 后，可以用 Git 管理 revision 历史。",
]
for text in samples:
    tokens = llm.encoding.encode(text)
    print(f"字符 {len(text):>3} 个 → token {len(tokens):>3} 个 | {text[:18]}...")

# 再看预算：默认一组 2600 token，相当于多少个这样的中文句子？
sample_tokens = len(llm.encoding.encode(samples[1]))
print(f"默认一组 2600 token ≈ {2600 // sample_tokens} 句这样的中文")
```

**需要观察的现象**：三段文本的「字符数 / token 数」比例差异——纯英文、纯中文、中英混排各不相同。

**预期结果**（待本地验证，具体数字以运行为准）：纯英文段 token 数明显少于字符数；纯中文段 token 数接近甚至略多于汉字数；中英混排介于两者之间。最后一句会打印出一个两位数左右的「每组可容纳句数」，帮你建立对 2600 预算的体感。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `max_group_tokens` 从 2600 调大到 5200，翻译请求的数量与失败重试成本分别怎么变？

<details><summary>参考答案</summary>

请求次数大约减半（组更大、组数更少），但单次请求的输入输出 token 翻倍；一旦某一组翻译失败需要重试，重试的代价也随组大小线性增长。官方文档正是这样建议的：[docs/en/EPUB_TRANSLATION.md:112](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/EPUB_TRANSLATION.md#L112)「更大的组意味着更少但更大的请求，并提高失败重试的成本」。
</details>

**练习 2**：片段分数为什么不直接用「正文文本的 token 数」，而要把包裹标签一起渲染成 XML 再计数，还要给带 `id` 的父标签加 80 分？

<details><summary>参考答案</summary>

因为发给 LLM 的不是裸文本，而是 friendly XML（带标签、带编号的模板），LLM 实际消耗的 token 包含标签开销；只用正文计数会系统性低估组的大小。带 `id` 的标签在回填阶段承担「原文—译文对齐」职责，占用的预算权重更高，所以固定加 `_ID_WEIGHT = 80`（[pdf_craft/transformer/xml_translator/xml_translator/score.py:31-34](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/score.py#L31-L34)）。
</details>

**练习 3**：`token_encoding` 填错了（比如服务商用的是完全不同的分词器），翻译还能跑通吗？会有什么后果？

<details><summary>参考答案</summary>

能跑通。`token_encoding` 从不出现在发给服务端的请求参数里，它只用于客户端本地的分组预算与长度估算（tiktoken 在本地切词）。后果只是分组大小的估算偏差——可能组偏大或偏小，影响成本与效率，但不影响请求的正确性。
</details>

### 4.3 缓存与日志：让翻译可复算、可排查

#### 4.3.1 概念说明

翻译一本书动辄几百次 LLM 请求。如果跑到 80% 时网络抖动失败，从头再来意味着再付一遍钱——**缓存**（`cache_path`）解决的就是这个问题：成功的翻译结果落盘，重跑时直接命中，跳过请求。

**日志**（`log_dir_path`）则回答「到底发生了什么」：每次请求、命中、失败都以 JSON 行写入日志文件。当你怀疑「为什么这里翻译得不对」「是不是卡在重试」时，日志是第一现场。官方文档还特别提醒：缓存只保留**成功**的请求，且不同书、不同翻译任务应使用不同的缓存目录，便于检查与清理（[docs/en/EPUB_TRANSLATION.md:89](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/EPUB_TRANSLATION.md#L89)）；排查不可恢复错误时应检查回调报告与 `log_dir_path` 输出（[docs/en/EPUB_TRANSLATION.md:168](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/EPUB_TRANSLATION.md#L168)）。

#### 4.3.2 核心流程

一次带缓存的请求流程：

```text
LLMContext.request(input)
  ├─ 1. 计算缓存键 key = sha256(JSON{url, model, messages, seed,
  │        temperature, top_p, max_tokens, protocol_version})
  ├─ 2. cache_path 存在且 {key}.txt 存在？
  │      ├─ 是 → 记日志 "cache-hit"，直接返回文件内容（零请求、零费用）
  │      └─ 否 → 记日志 "cache-miss"，进入请求循环
  ├─ 3. 最多 retry_times+1 次尝试；每次：
  │      ├─ 记日志 "request"
  │      ├─ 流式调用 LLM；成功且非空 → 把响应写入临时文件 {key}.{会话id}.txt
  │      ├─ 空响应 → 记 "empty-response"，重试
  │      └─ 异常 → 可重试则记 "transport-error" 并 sleep(retry_interval_seconds)；
  │               不可重试则记 "non-retryable-error" 并抛 LLMTransportError
  └─ 4. 上下文正常退出 → 临时文件改名转正为 {key}.txt（已有正式文件则删临时文件）
         上下文异常退出 → 删除临时文件（失败不留下缓存）
```

日志文件按「每个运行时一个」生成，名为 `request-{随机hex}.log`，内容是若干 JSON 行，每行形如：`{"session": "...", "category": "cache-hit", "attempt": 0, "model": "...", "cache_key": "..."}`。category 共有七种：`request` / `success` / `cache-hit` / `cache-miss` / `transport-error` / `non-retryable-error` / `empty-response`。

一个容易忽略的细节：**缓存键里含有协议版本号**（`protocol` 字段）。XML 翻译器为翻译与修复分别使用 `xml-translation-v1`、`xml-fill-v1` 两个版本串，提示词或协议一旦升级，旧缓存自动失效，不会把过期结果当命中。另外，**修复（fill）请求显式绕过缓存**（`use_cache=False`），重跑时命中的只是正文翻译，结构修复请求仍会真实发起。

#### 4.3.3 源码精读

配置侧：两个目录参数在构造期经 `_directory` 急切创建（见 4.1.3 的 [pdf_craft/llm/core.py:21-22](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/core.py#L21-L22)）。运行时侧在构造 `LLMRuntime` 时根据 `log_dir_path` 建好日志器：

[pdf_craft/llm/runtime.py:41-48](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L41-L48)

注意第 44-45 行：openai 客户端自己带的重试被关掉（`max_retries=0`），重试完全由 pdf-craft 按 `retry_times` / `retry_interval_seconds` 控制——这正是配置参数能精确描述重试行为的前提。

缓存命中与未命中分支：

[pdf_craft/llm/runtime.py:110-117](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L110-L117)

缓存键的计算——sha256 摘要覆盖了端点、模型、完整消息、种子、采样参数与协议版本：

[pdf_craft/llm/runtime.py:152-158](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L152-L158)

请求成功后先把响应写进**带会话 id 的临时文件**，只有上下文正常退出才在 `__exit__` 里把它转正为正式缓存文件；异常退出则删除（第 93-103 行）：

[pdf_craft/llm/runtime.py:131-134](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L131-L134)

[pdf_craft/llm/runtime.py:93-103](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L93-L103)

重试循环：`range(retry_times + 1)` 印证了「首请求 + N 次重试」的语义，第 145-146 行是重试间隔：

[pdf_craft/llm/runtime.py:121-146](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L121-L146)

日志的写入格式与日志器的创建：

[pdf_craft/llm/runtime.py:160-166](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L160-L166)

[pdf_craft/llm/runtime.py:173-182](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L173-L182)

协议版本如何进入缓存命名空间，以及修复请求为何绕过缓存：

[pdf_craft/transformer/xml_translator/xml_translator/translator.py:41-42](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L41-L42)

[pdf_craft/transformer/xml_translator/xml_translator/translator.py:221-224](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L221-L224)

（对比：正文翻译请求走默认的 `use_cache=True`，见 [pdf_craft/transformer/xml_translator/xml_translator/translator.py:154-167](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L154-L167)。）

#### 4.3.4 代码实践

**实践目标**：跑一次带 `cache_path` 与 `log_dir_path` 的小规模 EPUB 翻译，亲眼看到缓存文件与 JSON 日志，并验证第二次运行命中缓存。

**操作步骤**：

1. 准备一个小的 EPUB 文件（一两章即可），以及一个可用的 OpenAI 兼容凭据。
2. 编写并运行脚本：

```python
# 示例代码：translate_with_cache.py
from pdf_craft import LLM, PDFCraft, SubmitKind

llm = LLM(
    key="your-api-key", url="https://api.openai.com/v1",
    model="gpt-4.1-mini", token_encoding="o200k_base",
    cache_path="tmp/book-cache",     # 成功翻译的缓存目录
    log_dir_path="tmp/book-logs",    # 请求日志目录
)
PDFCraft().translate_epub(
    "source.epub", "translated.epub",
    target_language="zh", submit=SubmitKind.APPEND_BLOCK, llm=llm,
)
```

3. 翻译结束后检查两个目录：

```bash
ls tmp/book-cache | head        # 预期：若干 <64位十六进制>.txt 文件
ls tmp/book-logs | head         # 预期：若干 request-<hex>.log 文件
head -5 tmp/book-logs/request-*.log | head -20   # 预期：JSON 行
grep -c cache-hit tmp/book-logs/request-*.log    # 第一次运行应为 0
```

4. 用**相同的输入与配置**再跑一遍脚本，然后再次执行上面的 `grep -c cache-hit`。

**需要观察的现象**：

- 第一次运行：`book-cache` 里出现若干 `.txt` 文件，每个文件是一段成功翻译的正文；`book-logs` 里出现 `request-*.log`，逐行是 JSON，包含 `request`、`success`、`cache-miss` 等类别。
- 第二次运行：日志中 `cache-hit` 数量大于 0；`book-cache` 文件数量基本不再增长；总耗时明显下降。同时仍能看到 `request` 类别的日志（来自绕过缓存的 fill 修复请求）。

**预期结果**（待本地验证，具体计数以实际输出为准）：缓存文件名即 4.3.2 所述 sha256 键；日志为合法 JSON 行。若没有可用凭据，可改做**源码阅读型实践**：对照 [pdf_craft/llm/runtime.py:152-158](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L152-L158) 手写一段脚本，用 `hashlib.sha256(json.dumps({...}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()` 复算一个缓存键，验证「消息里改一个字，键就完全变化」。

#### 4.3.5 小练习与答案

**练习 1**：为什么缓存写入要先用临时文件 `{key}.{会话id}.txt`，等上下文退出再改名，而不是成功一次就写正式文件？

<details><summary>参考答案</summary>

「成功一次」不等于「整个任务成功」。采用临时文件 + 上下文退出时统一转正（[pdf_craft/llm/runtime.py:93-103](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L93-L103)）后：任务中途崩溃时临时文件被删除，缓存里只留下「完整跑完的任务」的结果，避免半成品污染缓存；改名是原子操作，也不会出现读到写了一半的文件。同时若正式键已存在，新临时文件直接丢弃，保留先到者，保证缓存内容稳定。
</details>

**练习 2**：同一本书用 `REPLACE` 模式译完一次，换 `APPEND_BLOCK` 模式再译，能命中上次的缓存吗？

<details><summary>参考答案</summary>

大部分能。缓存键只覆盖 url、model、messages、seed、采样参数、max_tokens 与协议版本（[pdf_craft/llm/runtime.py:152-158](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L152-L158)），而 `SubmitKind` 决定的是译文如何**写回**文档、不进入 LLM 的消息内容——发给模型的翻译请求与提示词不变，所以正文翻译仍然命中。这正是「翻译」与「提交」两个阶段解耦带来的红利（提交细节在第 7 单元展开）。
</details>

**练习 3**：日志里出现大量 `transport-error` 且 `attempt` 逐步递增，最可能是什么问题？应该调整 `LLM` 的哪个参数，或者先检查什么？

<details><summary>参考答案</summary>

`transport-error` 表示请求发出了但传输层失败（超时、连接错误、5xx/429 等，判定见 [pdf_craft/llm/error.py:6-13](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/error.py#L6-L13)），常见原因是网络不稳、服务端限流或超时太短。应先确认网络与凭据，再考虑调大 `timeout` 或 `retry_interval_seconds`（给限流恢复留时间）；`retry_times` 控制最多重试几次，超过后抛出 `LLMTransportError` 终止。若是 429 限流，还应降低翻译任务的 `concurrency`。
</details>

## 5. 综合实践

把本讲三个模块串成一个「翻译前体检脚本」`llm_preflight.py`。它在你真正花钱翻译之前，回答三个问题：配置对不对、预算估得准不准、缓存/日志目录是否就绪。

```python
# 示例代码：llm_preflight.py
from pathlib import Path
from pdf_craft import LLM

llm = LLM(
    key="your-api-key", url="https://api.openai.com/v1",
    model="gpt-4.1-mini", token_encoding="o200k_base",
    timeout=120, cache_path="tmp/preflight-cache", log_dir_path="tmp/preflight-logs",
)

# 1. 配置体检：目录是否已创建并解析为绝对路径
assert isinstance(llm.cache_path, Path) and llm.cache_path.is_absolute()
assert isinstance(llm.log_dir_path, Path) and llm.log_dir_path.is_absolute()

# 2. 预算体检：统计待译文本的 token，估算请求数
sample_text = Path("source_excerpt.txt").read_text(encoding="utf-8")  # 摘录一段原文
total = len(llm.encoding.encode(sample_text))
groups = -(-total // 2600)  # 向上取整
print(f"摘录 {total} token ≈ {groups} 个翻译组（默认每组 2600）")

# 3. 提示词预览：不花一分钱看到将要发给 LLM 的系统提示
print(llm.template("translate").render(target_language="zh", user_prompt=None)[:200])
```

**验收标准**：

1. 脚本零 API 费用跑通（`template().render` 是纯本地 jinja 渲染）。
2. 第 2 步输出与书籍实际规模量级一致（整本书 = 摘录 token 数 × 全书/摘录字数比）。
3. 第 3 步打印出的开头应与 [pdf_craft/transformer/xml_translator/data/translate.jinja](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/data/translate.jinja) 的渲染结果一致（"You are a translator..."）。

有真实凭据的读者可以加做第 4 步：用这套配置翻译一个小 EPUB，重跑一次，对比 `tmp/preflight-logs` 两轮日志中 `cache-hit` 的数量变化（待本地验证）。

## 6. 本讲小结

- `LLM` 是**单一声明式配置类**：四个必填项（`key`/`url`/`model`/`token_encoding`）加可选的采样、超时、重试与运维目录；构造期不发任何请求，但会急切创建缓存/日志目录并加载 tiktoken 编码器。
- `token_encoding` 驱动**客户端本地 token 计数**：编码器从 `LLM.encoding` 流向 `XMLStreamMapper`，片段分数 = XML 渲染的 token 数 + 每个带 `id` 父标签 80 分，用于把全书切成不超过 `max_group_tokens`（默认 2600）的翻译组。
- `cache_path` 实现断点复算：缓存键是覆盖端点、模型、消息、种子、采样与协议版本的 sha256；只有完整成功的上下文才把临时文件转正，失败不留下缓存。
- `log_dir_path` 输出 JSON 行日志，七种类别（`request`/`success`/`cache-hit`/`cache-miss`/`transport-error`/`non-retryable-error`/`empty-response`）足以还原每一次请求的命运。
- 重试语义精确可描述：openai 客户端自带重试被关闭，pdf-craft 按 `retry_times`（首请求后最多重试次数，默认 5）与 `retry_interval_seconds`（默认 6 秒）自行控制。
- 正文翻译请求走缓存，XML 结构修复请求显式绕过缓存（`use_cache=False`）——重跑时省的是翻译的钱，不是修复的钱。

## 7. 下一步学习建议

配置体系（第 2 单元）到此收尾。下一步有两条路：

1. **主线推荐**：进入第 3 单元「PDF 提取主链路」，从 [u3-l1 提取主链路：从门面到引擎](u3-l1-extraction-chain.md) 开始，看 `PDFOptions` 与 `ExtractionOptions` 如何被引擎消费；之后顺 u3 → u4 → u5 读完整条提取链。
2. **翻译方向支线**：如果你更关心翻译，可以直接预习 [u7-l2 XMLTranslator 核心](u7-l2-xml-translator-core.md)（本讲 4.2/4.3 提到的分组与缓存将在那里完整展开），以及 [u8-l1 LLM 运行时：请求、重试与缓存](u8-l1-llm-runtime-and-retry.md)——那里会精读 `LLMRuntime`、`LLMContext` 与修复循环 `run_repair_loop` 的完整实现。

无论走哪条路，建议先完成本讲第 5 节的综合实践：一个不花钱的 preflight 脚本能让你在后续所有翻译实验里少踩凭据与预算的坑。
