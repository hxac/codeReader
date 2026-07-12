# Tokenizer、BPE 与 chat_template

## 1. 本讲目标

本讲是「模型吃什么」这条输入链路的第一站。读完本讲，你应当能够：

- 说清楚 **BPE + ByteLevel** 分词的工作原理，以及 MiniMind 为什么把词表大小定为 `6400`。
- 看懂 `trainer/train_tokenizer.py` 是怎样从 0 训练出一个分词器的，包括「普通子词 / 特殊标记 / 工具标记 / 缓冲 token」四种 token 的划分。
- 读懂 `tokenizer_config.json` 里那段长长的 `chat_template`（Jinja 模板），理解它如何把多轮对话、工具调用（`<tool_call>`）和思考标签（`<think>`）渲染成模型实际看到的输入字符串，以及 `open_thinking` 开关到底改了什么。

本讲只聚焦「文本 ↔ token id」这一层，不涉及模型结构（那是第 3 单元的事），也不涉及 Dataset 的标签/掩码构造（那是 u2-l2 的事）。

## 2. 前置知识

### 2.1 什么是 Tokenizer

大语言模型不会直接读字符串，它只认数字。**Tokenizer（分词器）** 就是介于「人类文本」和「模型输入」之间的翻译官，它完成两件事：

- **编码（encode）**：把一段文本切分成若干小单元（**token**），每个 token 对应词表里的一个整数 id。
- **解码（decode）**：把一串 id 还原回文本。

可以把它粗略理解成一本「词典」。词典越大，表达同样内容需要的 token 越少（压缩率越高），但模型要学习的「词」也越多。

### 2.2 BPE 与 ByteLevel 两个名词

- **BPE（Byte Pair Encoding，字节对编码）**：一种构造词表的算法。它从最小的字符单元开始，反复合并语料里出现频率最高的一对相邻单元，直到词表达到目标大小。最终词表里既有单字符，也有「常见组合」（如 `the`、`ing`、`你好`）。
- **ByteLevel（字节级）**：BPE 的一个变体。普通 BPE 以 Unicode 字符为初始字母表，遇到训练时没见过的字符（比如 emoji、生僻字）就会出错；ByteLevel 则以 **256 个字节** 作为初始字母表。由于任何文本都能用字节表示，这套方案**永远不会遇到无法编码的字符**，也就不需要 `<unk>`（未知）token。

MiniMind 用的就是 `BPE + ByteLevel` 组合（见 [README.md:L128](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L128) 的更新说明）。

### 2.3 压缩率

衡量分词器好坏常用「压缩率」，定义为：

\[
\text{压缩率} = \frac{\text{字符数}}{\text{token 数}}
\]

数值越大，说明一个 token 承载的字符越多，模型推理同样长度的文本需要的步数越少。MiniMind 在中文上约 `1.5~1.7` 字符/token、英文约 `4~5` 字符/token（见 [README.md:L519](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L519)）。

### 2.4 chat_template 是什么

训练对话模型时，多轮对话、系统提示、工具调用都需要用一个**统一的字符串格式**喂给模型。`chat_template` 是一段 **Jinja2** 模板字符串，它的作用就是：给定结构化的「消息列表（messages）」，自动渲染成那段统一的字符串。这样无论是训练数据构造还是推理输入，格式都能保持一致。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [trainer/train_tokenizer.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_tokenizer.py) | 从 0 训练 BPE+ByteLevel 分词器的示例脚本，并附带一个 `eval_tokenizer` 自检函数。 |
| [model/tokenizer.json](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/tokenizer.json) | 训练好的分词器本体：词表 `vocab`、合并规则 `merges`、pre_tokenizer / decoder 配置。 |
| [model/tokenizer_config.json](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/tokenizer_config.json) | 分词器的「外层配置」：特殊标记表 `added_tokens_decoder`、bos/eos/pad、以及核心的 `chat_template`。 |

> 项目已自带训练好的分词器（在 `model/` 下），**不建议重新训练**——词表一旦改变，所有旧权重都会失效（见脚本顶部注释 [trainer/train_tokenizer.py:L1-L2](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_tokenizer.py#L1-L2)）。本脚本主要用于学习原理。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 BPE+ByteLevel 训练流程**、**4.2 特殊标记体系**、**4.3 chat_template**。

### 4.1 BPE + ByteLevel 分词训练

#### 4.1.1 概念说明

分词器训练的本质是：从一批语料里统计「哪些字符组合经常一起出现」，把它们合并成新 token，逐步把词表从 256 个字节扩充到目标大小（这里是 6400）。

为什么是 6400？这是 MiniMind 针对**小模型**做的一个明确取舍。模型的 embedding 层和输出层参数量都正比于词表大小：

\[
P_{\text{embed}} = \text{vocab\_size} \times \text{hidden\_size}
\]

以 `hidden_size=768` 为例：

| 词表大小 | embedding 参数量 |
| --- | --- |
| 6,400（MiniMind） | \(6400 \times 768 \approx 4.9\text{M}\) |
| 151,643（Qwen2） | \(151643 \times 768 \approx 116.5\text{M}\) |

对 minimind-3（总参 64M）来说，如果用 Qwen2 那么大的词表，光 embedding+输出层就会吃掉几乎所有参数。所以**刻意保持词表精简**是小模型的合理选择（详见 [README.md:L371-L372](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L371-L372)）。代价是压缩率偏低、同样文本要消耗更多 token，但对一个学习用的小模型是值得的。

#### 4.1.2 核心流程

`train_tokenizer()` 的训练流程可以概括为：

1. **读语料**：`get_texts()` 从 `sft_t2t_mini.jsonl` 取最多 10000 条样本，把每条对话的 `content` 拼成一段纯文本，作为训练语料生成器。
2. **建空 BPE 模型**：`Tokenizer(models.BPE())` 创建一个空的 BPE 分词器。
3. **装 ByteLevel 预分词器**：`pre_tokenizers.ByteLevel(add_prefix_space=False)` 决定先把文本映射到字节级表示。
4. **准备特殊标记**：拼出「核心特殊标记 + 工具/思考标记 + 缓冲 token」三段（详见 4.2）。
5. **配置训练器**：`BpeTrainer` 设定 `vocab_size=6400`、初始字母表为 256 字节、把特殊标记作为词表开头的保留位。
6. **训练**：`train_from_iterator()` 基于语料执行 BPE 合并，填满词表。
7. **装解码器**：`decoders.ByteLevel()` 让 id 能正确还原回文本。
8. **后处理与保存**：写出 `tokenizer.json` 和 `tokenizer_config.json`。

伪代码：

```text
tokenizer = BPE()
tokenizer.pre_tokenizer = ByteLevel()
trainer = BpeTrainer(vocab_size=6400,
                     initial_alphabet=ByteLevel.alphabet(),   # 256 字节
                     special_tokens=[<核心标记>, <工具标记>, <缓冲标记>])
tokenizer.train_from_iterator(corpus, trainer)
tokenizer.decoder = ByteLevel()
save(tokenizer.json); save(tokenizer_config.json)
```

#### 4.1.3 源码精读

常量定义——词表大小 6400、特殊标记总数 36：

[trainer/train_tokenizer.py:L7-L10](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_tokenizer.py#L7-L10) — 定义语料路径、输出目录、`VOCAB_SIZE=6400` 与 `SPECIAL_TOKENS_NUM=36`。

语料读取——逐行解析 jsonl，拼出纯文本：

[trainer/train_tokenizer.py:L12-L22](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_tokenizer.py#L12-L22) — `get_texts` 用生成器逐行产出文本，`if i >= 10000: break` 限制只用前一万行做演示。

创建 BPE 模型并装上 ByteLevel 预分词器：

[trainer/train_tokenizer.py:L25-L26](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_tokenizer.py#L25-L26) — `models.BPE()` 建空模型，`pre_tokenizers.ByteLevel(add_prefix_space=False)` 把文本先变成字节级表示。

配置训练器并执行合并：

[trainer/train_tokenizer.py:L43-L50](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_tokenizer.py#L43-L50) — `BpeTrainer` 指定 `vocab_size`、`initial_alphabet=pre_tokenizers.ByteLevel.alphabet()`（256 字节作初始字母表）和 `special_tokens`；随后 `train_from_iterator` 跑 BPE 合并把词表填到 6400。

装解码器并保存：

[trainer/train_tokenizer.py:L51-L56](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_tokenizer.py#L51-L56) — `decoders.ByteLevel()` 让编解码可逆，然后把 `tokenizer.json` 落盘。

训练完成后，词表与合并规则就写进了 `model/tokenizer.json` 的 `model` 字段：

[model/tokenizer.json:L345-L354](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/tokenizer.json#L345-L354) — `model.type="BPE"`、`unk_token=null`（ByteLevel 不需要未知 token）、`byte_fallback=false`、紧跟着 `vocab` 字典。前面的特殊标记占据 id 0~35，之后才是 BPE 学到的字节/子词合并。

预分词器与解码器在 `tokenizer.json` 里同样标注为 ByteLevel：

[model/tokenizer.json:L332-L344](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/tokenizer.json#L332-L344) — `pre_tokenizer` 与 `decoder` 均为 `ByteLevel` 类型，保证编解码双向一致。

#### 4.1.4 代码实践

**目标**：用项目自带的 `model/` 分词器（不必重新训练）测量压缩率，验证 README 给出的「中文 1.5~1.7、英文 4~5」说法。

**步骤**（在仓库根目录执行）：

```python
# 文件名：tmp_check_compress.py（示例代码，可随手删除）
from transformers import AutoTokenizer
tok = AutoTokenizer.from_pretrained("./model")

zh = "白日依山尽，黄河入海流。"
en = "The sun sets in the west."

for name, text in [("中文", zh), ("英文", en)]:
    n_ids = len(tok.encode(text))
    print(f"{name} | 字符数={len(text)} | tokens={n_ids} | 压缩率={len(text)/n_ids:.2f}")
```

**需要观察的现象**：中文的压缩率明显低于英文（因为 6400 的小词表对中文覆盖更弱，常用字也常被拆开）。

**预期结果**：中文压缩率落在 1.5~1.7 附近，英文落在 4~5 附近。**待本地验证**（实际数值随文本内容有波动）。

#### 4.1.5 小练习与答案

**练习 1**：如果要把词表从 6400 扩到 32000，embedding 层参数会变为原来的多少倍？
**答**：\(32000/6400 = 5\) 倍。因为 \(P_{\text{embed}} = \text{vocab\_size} \times \text{hidden\_size}\)，与 vocab_size 成正比。

**练习 2**：为什么 ByteLevel 方案可以不要 `<unk>` token？
**答**：ByteLevel 以 256 个字节作为初始字母表，任何字符（含 emoji、生僻字）最终都能拆成字节序列，永远不会出现「词表里没有的字符」，因此无需 `<unk>`。在 `tokenizer.json` 里也能看到 `"unk_token": null`。

---

### 4.2 特殊标记体系（special_tokens_list）

#### 4.2.1 概念说明

词表里除了 BPE 学到的普通子词，还有一类**特殊标记（special tokens）**。它们不参与正常的子词合并，而是作为「控制信号」存在，用来界定对话结构、工具调用、思考块等。MiniMind 把特殊标记分成三类：

1. **核心控制标记**（`special_tokens_list`）：如 `<|im_start|>`（消息开始）、`<|im_end|>`（消息结束/也作 eos）、`<|endoftext|>`（padding/unk），以及一批为多模态预留的占位符（vision/audio/box 等）。这些标记 `special=True`，模型不会把它们当普通文本处理。
2. **工具/思考标记**（`additional_tokens_list`）：`<tool_call>`/`</tool_call>`、`<tool_response>`/`</tool_response>`、`<think>`/`</think>`。它们虽然长得像标签，但被设成 `special=False`——也就是说**它们和普通子词一样会参与训练 loss**，模型需要真正「学会」生成它们。
3. **缓冲标记**（`buffer_tokens`）：`<|buffer1|>` ~ `<|buffer9|>`，纯粹占位，为将来新增标记预留 id 空间，避免以后改词表时打乱已有 id 顺序。

#### 4.2.2 核心流程

标记清单的拼装逻辑：

```text
核心标记 special_tokens_list     (21 个，special=True)
+ 工具/思考标记 additional_tokens_list (6 个，special=False)
+ 缓冲标记 buffer_tokens           (num_buffer 个，special=False)
= all_special_tokens              (共 SPECIAL_TOKENS_NUM=36 个)
```

缓冲数量是「凑齐」算出来的：

\[
\text{num\_buffer} = \text{SPECIAL\_TOKENS\_NUM} - \text{len}(\text{核心} + \text{工具})
\]

代入数字：\(36 - (21 + 6) = 9\)，所以有 `<|buffer1|>` 到 `<|buffer9|>`。

训练后还有一步**关键后处理**：脚本会把「不在核心标记列表里」的标记（即工具/思考标记和缓冲标记）的 `special` 字段强制改成 `False`。这一步决定了 `<tool_call>`、`<think>` 等能像普通词一样被学习。

#### 4.2.3 源码精读

核心标记清单（多模态占位 + 对话边界）：

[trainer/train_tokenizer.py:L28-L33](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_tokenizer.py#L28-L33) — `special_tokens_list`，含 `<|im_start|>`、`<|im_end|>`、`<|endoftext|>` 及 vision/audio/box 等占位符，全部 `special=True`。

工具与思考标记：

[trainer/train_tokenizer.py:L35-L39](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_tokenizer.py#L35-L39) — `additional_tokens_list` 定义 `<tool_call>`/`</tool_call>`、`<tool_response>`/`</tool_response>`、`<think>`/`</think>`。

缓冲标记与总数拼装：

[trainer/train_tokenizer.py:L40-L42](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_tokenizer.py#L40-L42) — 用 `num_buffer = special_tokens_num - len(...)` 凑出 9 个 `<|bufferN|>`，再拼成 `all_special_tokens`。

后处理——把非核心标记的 `special` 改成 `False`：

[trainer/train_tokenizer.py:L58-L64](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_tokenizer.py#L58-L64) — 读回刚保存的 `tokenizer.json`，遍历 `added_tokens`，凡是不在 `special_tokens_list` 里的，`special` 置为 `False`。这就是让 `<tool_call>`/`<think>` 可被学习的关键。

最终配置里能直接看到这种区分——以 `<tool_call>` 为例：

[model/tokenizer_config.json:L174-L205](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/tokenizer_config.json#L174-L205) — id 21~24（`<tool_call>`/`</tool_call>`/`<tool_response>`/`</tool_response>`）的 `"special": false`。

思考标记同样是 `special=false`：

[model/tokenizer_config.json:L206-L221](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/tokenizer_config.json#L206-L221) — id 25、26 的 `<think>`/`</think>` 也是 `special=false`。

而核心标记（如 `<|im_end|>`）则是 `special=true`：

[model/tokenizer_config.json:L22-L29](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/tokenizer_config.json#L22-L29) — id 2 的 `<|im_end|>` 为 `"special": true`。

注意 `additional_special_tokens` 字段**只列核心标记**，不含工具/思考标记（因为它们 `special=false`）：

[model/tokenizer_config.json:L295-L316](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/tokenizer_config.json#L295-L316) — 列出的全是 vision/audio/im_start 等核心标记，没有 `<tool_call>` 等。

bos/eos/pad 的指派：

[model/tokenizer_config.json:L317-L325](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/tokenizer_config.json#L317-L325) — `bos_token="<|im_start|>"`、`eos_token="<|im_end|>"`、`pad_token`/`unk_token` 都复用 `<|endoftext|>`。这套指派决定了 `eval_llm.py` 里 `tokenizer.bos_token` 等取到的是什么。

#### 4.2.4 代码实践

**目标**：验证同一份词表里，「核心标记」与「工具/思考标记」在 `special` 属性上的区别，并观察它们对 `convert_ids_to_tokens` 的影响。

**步骤**：

```python
# 示例代码
from transformers import AutoTokenizer
tok = AutoTokenizer.from_pretrained("./model")

for t in ["<|im_end|>", "<tool_call>", "<think>", "<|buffer1|>"]:
    tid = tok.convert_tokens_to_ids(t)
    # 追溯它在 added_tokens_decoder 里的 special 标志
    info = tok.added_tokens_decoder.get(tid)
    print(f"{t:15} id={tid:3} special={getattr(info, 'special', '?')}")
```

**需要观察的现象**：`<|im_end|>` 是 special，而 `<tool_call>`/`<think>`/`<|buffer1|>` 不是 special。

**预期结果**：四者的 id 分别约为 2、21、25、27；`<|im_end|>` 为 `True`，其余为 `False`。**待本地验证**（`added_tokens_decoder` 的对象属性访问方式可能随 transformers 版本略有差异，可改为读 `tokenizer_config.json` 直接查 `special` 字段）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `<tool_call>` 要被设成 `special=False`，而不能像 `<|im_end|>` 那样设成 `special=True`？
**答**：`<tool_call>` 是模型需要**主动学会生成**的内容标签——它在训练时要参与交叉熵 loss、要被采样输出。如果设成 `special=True`，分词器会把它当作不可分割的控制符，某些处理流程会跳过对它的损失计算或屏蔽它的梯度；设成 `special=False` 让它表现得像普通子词，才能被正常学习。`<|im_end|>` 则是结构边界符，由模板固定注入、作为停止信号，不需要模型「学着生成」。

**练习 2**：缓冲标记 `<|buffer1|>` 目前没被任何模板使用，留着有什么用？
**答**：为将来扩展预留连续的 id 槽位。如果以后要新增特殊标记（比如新的工具协议标签），可以直接启用某个 buffer id，而不必在词表末尾追加、打乱既有 id 顺序——这能保证旧权重里的 embedding 行含义不变。

---

### 4.3 chat_template：多轮对话、tool_call 与 think 注入

#### 4.3.1 概念说明

`chat_template` 是一段写在 `tokenizer_config.json` 里的 **Jinja2 模板**。`tokenizer.apply_chat_template(messages)` 调用时，transformers 会把 `messages`（一个「消息字典列表」）喂给这段模板，渲染出一段模型实际看到的纯文本。

它要统一处理四种角色：

- **system**：系统提示，可选；如果传了 `tools`，会和工具说明合并进一个 system 块。
- **user**：用户发言。
- **assistant**：模型回复，可能内含 `<think>` 思考块和 `<tool_call>` 工具调用。
- **tool**：工具执行结果，会被包装成 `<tool_response>` 塞进一个 user 块里。

理解这段模板是理解「思考开关」「工具调用」如何落到输入字符串的关键——它和 u1-l3 提到的 `open_thinking` 直接相关。

#### 4.3.2 核心流程

模板的整体结构（按渲染顺序）：

```text
1. 渲染头部 system 块
   ├─ 若提供 tools：渲染 "# Tools ... <tools>{每个tool的json}</tools>" + 工具调用说明，包成一个 system 块
   └─ 否则若首条是 system：渲染普通 system 块

2. 反向扫描，标记「最后一条真实用户提问」的位置（last_query_index）
   （用于跳过 <tool_response> 这种伪 user 消息）

3. 正向遍历每条消息：
   ├─ user / system(非首条)： <|im_start|>{role}\n{content}<|im_end|>\n
   ├─ assistant：             <|im_start|>assistant\n<think>\n{reasoning}\n</think>\n\n{content}
   │                          [+ 若有 tool_calls：每个渲染 <tool_call>{"name":..,"arguments":..}</tool_call>]
   │                          <|im_end|>\n
   └─ tool：                  连续的 tool 合并进一个 <|im_start|>user 块，
                              每条包成 <tool_response>\n{content}\n</tool_response>

4. 若 add_generation_prompt=True：追加 <|im_start|>assistant\n
   ├─ open_thinking=True：   再追加 <think>\n            （只给起始标签，留给模型续写思考）
   └─ 否则：                 再追加 <think>\n\n</think>\n\n （空思考块，让模型跳过思考直接回答）
```

最关键的就是第 4 步：**`open_thinking` 决定生成时注入的是「半个 think 起始标签」还是「一个空的 think 块」**。

- `open_thinking=1`：注入 `<think>\n`，模型从这里开始续写思考内容，写完自己闭合 `</think>` 再给答案 → **先思考后回答**。
- `open_thinking=0`：注入 `<think>\n\n</think>\n\n`，思考块是空的，模型直接进入正式回答 → **直答模式**。

这就是 u1-l3 所说「open_thinking 并非独立模型，而是模板内 `<think>` 标签的开关」的底层实现。

#### 4.3.3 源码精读

整段模板就存在 `tokenizer_config.json` 的 `chat_template` 字段里：

[model/tokenizer_config.json:L333](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/tokenizer_config.json#L333) — 整段 Jinja 模板（单行，含大量 `\n` 转义）。下面按逻辑片段解读关键字串。

**① tools 头部**：当传入 `tools` 时，渲染工具说明并示范 `<tool_call>` 的格式：

```jinja
{%- if tools %}
    {{- '<|im_start|>system\n' }}
    ...
    {{- "# Tools\n\nYou may call one or more functions ... <tools>" }}
    {%- for tool in tools %}{{- tool | tojson }}{%- endfor %}
    {{- "\n</tools>\n\nFor each function call, return a json object ... <tool_call>\n{\"name\": <function-name>, \"arguments\": <args-json-object>}\n</tool_call><|im_end|>\n" }}
```

这一段把工具定义 dump 成 JSON 塞进 `<tools>`，并告诉模型「调用时输出 `<tool_call>{...}</tool_call>`」。

**② assistant 消息渲染**：从 content 里拆出 `<think>` 思考块，再拼回，并处理 `tool_calls`：

```jinja
{%- elif message.role == "assistant" %}
    {%- set reasoning_content = '' %}
    {%- if '</think>' in content %}
        {%- set reasoning_content = content.split('</think>')[0]...split('<think>')[-1]... %}
        {%- set content = content.split('</think>')[-1]... %}
    {%- endif %}
    {{- '<|im_start|>' + message.role + '\n<think>\n' + reasoning_content... + '\n</think>\n\n' + content... }}
    {%- if message.tool_calls %}
        {%- for tool_call in message.tool_calls %}
            {{- '<tool_call>\n{\"name\": \"' ~ tool_call.name ~ '\", \"arguments\": ' ~ tool_call.arguments|tojson ~ '}\n</tool_call>' }}
```

注意：历史 assistant 消息也会被强制包进 `<think>...</think>` 结构（`{%- if true %}` 分支恒成立），保证训练和推理格式一致。

**③ tool 消息合并**：连续的 tool 结果合并进同一个 user 块，每条包成 `<tool_response>`：

```jinja
{%- elif message.role == "tool" %}
    {%- if loop.first or (messages[loop.index0 - 1].role != "tool") %}
        {{- '<|im_start|>user' }}        # 连续 tool 的第一条才开新 user 块
    {%- endif %}
    {{- '\n<tool_response>\n' ~ content ~ '\n</tool_response>' }}
    {%- if loop.last or (messages[loop.index0 + 1].role != "tool") %}
        {{- '<|im_end|>\n' }}            # 连续 tool 的最后一条才闭合
    {%- endif %}
```

**④ add_generation_prompt + open_thinking 分支**（本讲最核心）：

```jinja
{%- if add_generation_prompt %}
    {{- '<|im_start|>assistant\n' }}
    {%- if open_thinking is defined and open_thinking is true %}
        {{- '<think>\n' }}              # 只注入起始标签 → 思考模式
    {%- else %}
        {{- '<think>\n\n</think>\n\n' }} # 注入空 think 块 → 直答模式
    {%- endif %}
{%- endif %}
```

这段就是 `open_thinking` 开关的全部实现。

模板在推理侧的使用方式（`eval_llm.py`）：

[eval_llm.py:L74-L78](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L74-L78) — 预训练权重走 `tokenizer.bos_token + prompt` 纯文本续写；SFT 权重走 `apply_chat_template(..., add_generation_prompt=True, open_thinking=bool(args.open_thinking))`，把 `open_thinking` 命令行参数透传进模板。

`open_thinking` 参数声明：

[eval_llm.py:L45](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L45) — `--open_thinking` 默认 0（直答），设 1 进入思考模式。

#### 4.3.4 代码实践

**目标**：手动用 `apply_chat_template` 渲染一条**带工具调用**的多轮对话，检查 `<tool_call>` / `<tool_response>` 的实际输出，并对比 `open_thinking` 两种取值的差异。

**步骤**（仓库根目录）：

```python
# 示例代码：tmp_check_template.py
from transformers import AutoTokenizer
tok = AutoTokenizer.from_pretrained("./model")

tools = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询某城市天气",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}
    }
}]

messages = [
    {"role": "user", "content": "北京今天天气怎么样？"},
    {"role": "assistant", "content": "",
     "tool_calls": [{"function": {"name": "get_weather", "arguments": '{"city":"北京"}'}}]},
    {"role": "tool", "content": '{"weather":"晴, 25℃"}'},
]

# 1) 不开思考
print("=== open_thinking=False ===")
print(tok.apply_chat_template(messages, tokenize=False, tools=tools,
                              add_generation_prompt=True, open_thinking=False))
print("\n=== open_thinking=True ===")
print(tok.apply_chat_template(messages, tokenize=False, tools=tools,
                              add_generation_prompt=True, open_thinking=True))
```

**需要观察的现象**：

1. 头部出现 `<|im_start|>system\n # Tools ... <tools>{...get_weather...}</tools>`，并以「For each function call, return ... `<tool_call>` ...」收尾。
2. assistant 那轮渲染出 `<tool_call>\n{"name": "get_weather", "arguments": {"city":"北京"}}\n</tool_call>`。
3. tool 那轮被包成 `<|im_start|>user\n<tool_response>\n{...}\n</tool_response><|im_end|>`。
4. 末尾的 `add_generation_prompt`：`open_thinking=False` 时是 `<think>\n\n</think>\n\n`；`open_thinking=True` 时只有 `<think>\n`。

**预期结果**：与上面四点一致。**待本地验证**（不同 transformers 版本对 `tool_calls` 字段的解析细节可能略有差异；若渲染不出工具块，检查 `tools` 与 `tool_calls` 字段名是否符合 OpenAI 协议）。

#### 4.3.5 小练习与答案

**练习 1**：同一条 messages，`open_thinking=1` 和 `open_thinking=0` 渲染出的字符串差别在哪？
**答**：差别只在末尾 `add_generation_prompt` 注入的内容。`open_thinking=1` 注入 `<|im_start|>assistant\n<think>\n`（只有起始标签，引导模型先写思考再闭合 `</think>`）；`open_thinking=0` 注入 `<|im_start|>assistant\n<think>\n\n</think>\n\n`（一个空 think 块，模型直接跳过思考给答案）。

**练习 2**：为什么连续多条 `tool` 消息会被合并进**同一个** `<|im_start|>user ... <|im_end|>` 块，而不是每条单独一个块？
**答**：为了让模型把「同一轮里的多个工具返回结果」当成一次 user 反馈，而不是多轮独立对话。模板用 `loop.first / messages[loop.index0-1].role != "tool"` 判断「是否是连续 tool 序列的第一条」来决定是否新开 `<|im_start|>user`，用 `loop.last / ... != "tool"` 判断「是否是最后一条」来决定是否闭合 `<|im_end|>`。

---

## 5. 综合实践

把三个模块串起来，完成一个「跑通分词器训练 + 自检」的小任务（对应规格里的实践任务）。

**目标**：体验 `train_tokenizer.py` 的完整流程——训练一个**自己的迷你分词器**，用 `eval_tokenizer` 打印压缩率；再用项目自带分词器渲染一条带 `tool_calls` 的多轮对话。

**步骤**：

1. 进入 trainer 目录并运行脚本（脚本内路径是相对 `trainer/` 写的）：

   ```bash
   cd trainer
   python train_tokenizer.py
   ```

   它会读 `../dataset/sft_t2t_mini.jsonl` 前 10000 行，把新分词器写到 `../model_learn_tokenizer/`。

2. 观察脚本末尾 `eval_tokenizer` 打印的内容（[trainer/train_tokenizer.py:L143-L152](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_tokenizer.py#L143-L152)）：每个样本的「字符数 / tokens / 压缩率」以及平均压缩率。记下平均值。

3. 切回根目录，用**项目自带**的 `model/` 分词器渲染带工具调用的多轮对话（直接复用 4.3.4 的脚本），核对 `<tool_call>` / `<tool_response>` 输出。

**需要观察的现象**：

- 新训练出的分词器压缩率应与 README 给出的中文 1.5~1.7、英文 4~5 区间接近；由于只用了 10000 行语料，数值可能略低于正式版 `model/` 分词器。
- 工具对话渲染结果应包含 system 的 `# Tools` 头、assistant 的 `<tool_call>`、以及 tool 角色的 `<tool_response>`。

**预期结果**：

- `eval_tokenizer` 末尾打印形如 `平均压缩率: x.xx`，中英样本分别落在上述区间。
- 工具渲染输出包含 4.3.4 列出的四个特征。

**如果跑不起来**：

- `train_tokenizer.py` 依赖 `dataset/sft_t2t_mini.jsonl`，若未下载数据集会报 FileNotFoundError——此时可跳过训练部分，直接用 `model/` 自带分词器做 4.3.4 的模板渲染实践（这是纯读取操作，不需要数据集）。
- 整个训练 + 自检结果**待本地验证**；不要假设具体压缩率数值，以本机实际输出为准。

> ⚠️ 注意：训练出来的 `model_learn_tokenizer/` **不要**用来替换 `model/` 下的官方分词器，否则所有预训练/SFT 权重都会失效（词表 id 对不上）。

---

## 6. 本讲小结

- MiniMind 用 **BPE + ByteLevel** 从 0 训练分词器，词表仅 **6400**，是为了压缩 embedding/输出层的参数占比，代价是压缩率偏低（中文约 1.5~1.7、英文约 4~5 字符/token）。
- 特殊标记分三类：**核心控制标记**（`special=True`，如 `<|im_start|>`/`<|im_end|>`）、**工具/思考标记**（`special=False`，如 `<tool_call>`/`<think>`，需要模型学会生成）、**缓冲标记**（预留扩展位）。
- bos=`<|im_start|>`、eos=`<|im_end|>`、pad/unk 复用 `<|endoftext|>`，这套指派决定了 `eval_llm.py` 里取到的特殊 token。
- `chat_template` 是一段 Jinja 模板，统一渲染 system/user/assistant/tool 四种角色，并把工具调用渲染成 `<tool_call>`、工具结果渲染成 `<tool_response>`。
- **`open_thinking` 开关**的本质：`add_generation_prompt` 时，`1` 注入半个 `<think>\n`（思考模式），`0` 注入空 `<think>\n\n</think>\n\n`（直答模式）——思考能力是模板 + 标签实现的，不是独立模型。

## 7. 下一步学习建议

- 本讲只解决了「文本 ↔ token id」。下一讲 **u2-l2（数据集加载与标签/掩码构造）** 会接着讲这些 id 是如何被 `PretrainDataset` / `SFTDataset` 组装成训练张量的，尤其是 SFT 如何用 `bos_id`/`eos_id` 定位 assistant 段、用 `loss_mask` 让只有回答部分参与 loss——那一步直接依赖本讲的 `<|im_start|>`/`<|im_end|>` 标记。
- 想验证自己对模板的理解，可以提前读 [dataset/lm_dataset.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py) 里的 `pre_processing_chat` / `SFTDataset`，看它如何调用 `apply_chat_template`。
- `open_thinking` 与采样生成的配合，会在 **u3-l6（自定义 generate）** 讲模型自回归生成时再次出现，届时可回看本讲的模板注入逻辑。
