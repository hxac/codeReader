# gen_config 生成 mlc-chat-config.json

## 1. 本讲目标

在 u7-l1 里我们看到，`compile()` 的第一步就是**读取 `mlc-chat-config.json`**——它是编译期与运行期共享的契约文件。那么这个文件本身是从哪来的？本讲就回答这个问题：**`mlc_llm gen_config` 如何把一份 HuggingFace 模型目录，聚合成一个 `mlc-chat-config.json`。**

读完本讲，你应该能够：

- 说出 `gen_config()` 的**五个步骤**（Step 1–5），以及每一步的输入来源与产出。
- 理解一个字段（例如 `temperature`）最终值的**三级优先级链路**：模型构造 → `generation_config.json` → 系统默认。
- 讲清楚 tokenizer 配置是如何被**复制、必要时转换（RWKV / `.model`→`.json` / tiktoken）、并探测出 `tokenizer_info`** 的。
- 学会打开一个真实的 `mlc-chat-config.json`，逐字段判断它的值来自 model config、conversation template、还是系统默认。

本讲与 u2-l2（命令行视角的 `gen_config`）和 u7-l1（消费视角的 `compile`）形成闭环：u2-l2 讲「怎么调用」，u7-l1 讲「谁来读」，本讲讲「它内部如何被造出来」。

## 2. 前置知识

本讲假设你已具备 u6-l2 与 u7-l1 的认知。需要的前置概念（不熟悉也能读懂，这里给出最小解释）：

- **`mlc-chat-config.json`**：连接「编译期」与「运行期」的契约文件。编译期 `compile` 读它拿模型结构与量化方案；运行期引擎读它拿对话模板、tokenizer、采样默认值。它的字段 schema 由 `MLCChatConfig` 定义（见 u1-l4）。
- **`Model` 信封**（u3-l1）：`MODELS` 注册表里的一项，`model.config` 是配置类（带 `from_file` 类方法），`model.name` 是架构名，`model.model_task` 是 `chat`/`embedding`。
- **`ConvTemplateRegistry`**（u6-l2）：对话模板注册表，`get_conv_template(name)` 取出一个 `Conversation` 对象，`.to_json_dict()` 把它序列化成字典。
- **`ModelConfigOverride`**（u7-l1）：一个 dataclass，`.apply(model_config)` 把命令行传入的覆盖项（如 `--context-window-size`）盖到从 `config.json` 读出的模型配置上。
- **`ModelConfigOverride` 与 `OptimizationFlags` 的区别**：前者覆盖**模型结构/运行参数**（context window、shards 等），后者覆盖**编译优化开关**（flashinfer、cudagraph 等）。本讲只涉及前者。

一个值得记住的直觉：`gen_config` 本质上是一个**聚合器（aggregator）**——它不发明任何配置，而是把散落在 HF 目录里各处的信息（`config.json` / `generation_config.json` / tokenizer 文件 / 注册表里的对话模板），按一套固定的优先级，合并成一个统一的 JSON。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [python/mlc_llm/interface/gen_config.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py) | **主角**。`gen_config()` 五步主流程，以及 RWKV tokenizer 生成、系统默认填充等辅助函数。 |
| [python/mlc_llm/protocol/mlc_chat_config.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/mlc_chat_config.py) | 定义 `MLCChatConfig`（契约 schema）与 `MLC_CHAT_SYSTEM_DEFAULT`（系统默认值表）。 |
| [python/mlc_llm/cli/gen_config.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/gen_config.py) | CLI 入口层。解析 argv、做 `detect_*` 探测，再调用接口层的 `gen_config()`。 |
| [python/mlc_llm/interface/compiler_flags.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compiler_flags.py) | 定义 `ModelConfigOverride`（构造期覆盖模型配置的字段集合）。 |
| [python/mlc_llm/support/config.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/config.py) | `ConfigOverrideBase.apply`——把覆盖项盖到配置对象上的通用机制。 |
| [python/mlc_llm/support/convert_tiktoken.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/convert_tiktoken.py) | 把 tiktoken 词表转换成 HuggingFace `tokenizer.json` 格式。 |
| [python/mlc_llm/tokenizers/tokenizers.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/tokenizers/tokenizers.py) | `Tokenizer.detect_tokenizer_info` 与 `TokenizerInfo` 数据类。 |

## 4. 核心概念与源码讲解

### 4.1 config 聚合：从 config.json 到 MLCChatConfig（Step 1 & 2）

#### 4.1.1 概念说明

「聚合」要解决的核心问题是：**一个 `mlc-chat-config.json` 里的字段，来源各不相同。** 比如 `vocab_size` 来自模型架构配置，`temperature` 来自 `generation_config.json`，`conv_template` 来自注册表，`model_type` 来自命令行解析出的 `Model` 信封。`gen_config` 的 Step 1–2 就是把这些来源**按固定优先级**拼装成一个 `MLCChatConfig` 对象。

这里有一个贯穿全程的关键设计——**「懒填充 + 仅填空」**：构造 `MLCChatConfig` 时，能从模型配置确定的字段（如 `vocab_size`）直接填死；而那些**模型配置管不着**的字段（如 `temperature`、`top_p`、`eos_token_id`），先留成 `None`，留给后续步骤去填。Step 2 的填充逻辑有一个硬性闸门：

```python
if hasattr(mlc_chat_config, key) and getattr(mlc_chat_config, key) is None:
    setattr(mlc_chat_config, key, value)
```

也就是说，`generation_config.json` **只能填仍然为 `None` 的字段**，绝不能覆盖已经被模型配置填好的值。这就保证了「模型架构事实」永远优先于「生成参数默认值」。

#### 4.1.2 核心流程

Step 1–2 的聚合流程（伪代码）：

```
输入: config(Path 指向 config.json), model(Model 信封), quantization, conv_template 名, 一组覆盖参数
   │
   ▼  Step 1：构造 MLCChatConfig 骨架
   │  1a. 查 ConvTemplateRegistry 取对话模板 → conversation(dict 或裸字符串)
   │  1b. model.config.from_file(config) 读 config.json → 用 ModelConfigOverride.apply 盖上命令行覆盖项
   │  1c. 用上述结果构造 MLCChatConfig：
   │        - 架构事实字段（vocab_size/context_window_size/...）从 model_config 直接取值（非 None）
   │        - 生成参数字段（temperature/top_p/eos_token_id...）留 None，等后续填
   │
   ▼  Step 2：用 generation_config.json / config.json 填空
   │  for 文件名 in ["generation_config.json", "config.json"]:
   │      for key, value in 该文件:
   │          if MLCChatConfig 有此字段 and 当前值为 None:
   │              填入 value            # 仅填空，不覆盖
```

字段最终值的三级优先级（以 `temperature` 为例）：

1. **构造期**：`MLCChatConfig` 把 `temperature` 初始化为 `None`（模型配置不提供它）。
2. **Step 2**：若 `generation_config.json`（优先）或 `config.json` 里有 `temperature`，填入；否则仍为 `None`。
3. **Step 4**（见 4.3）：若仍为 `None`，用 `MLC_CHAT_SYSTEM_DEFAULT["temperature"] = 1.0` 兜底。

注意：模型架构字段（`vocab_size` 等）在构造期就已经是非 `None` 的确定值，所以它们**不会**被 Step 2 或 Step 4 改动——优先级链路对它们而言在构造期就结束了。

#### 4.1.3 源码精读

先看 `gen_config()` 的签名——它接收的正是 `cli/gen_config.py` 解析+探测后的结构化对象（`Model`、`Quantization`、各覆盖参数）：

[python/mlc_llm/interface/gen_config.py:L89-L103](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L89-L103) —— `gen_config` 入参。注意 `config` 是 `Path`（指向 `config.json`），`model` 是 `Model` 信封，`quantization` 是 `Quantization` 对象，`conv_template` 是模板名字符串，其余是可选的运行参数覆盖项。

**Step 1a：取对话模板。** 查注册表，命中则序列化成 dict，未命中则退化为裸字符串（与 u6-l2 讲的「未命中降级为自定义模板字符串」对应）：

[python/mlc_llm/interface/gen_config.py:L106-L115](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L106-L115) —— `ConvTemplateRegistry.get_conv_template(conv_template)`；命中走 `to_json_dict()`（见下方），未命中走 `conversation = conv_template` 裸字符串。

`to_json_dict` 用 `by_alias=True` 保证字段以 `model_type` 这类别名（而非 `field_model_type`）落盘：

[python/mlc_llm/protocol/conversation_protocol.py:L111-L113](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/conversation_protocol.py#L111-L113) —— `Conversation.to_json_dict`，`exclude_none=True` 使未设置的字段不出现在最终 JSON 里。

**Step 1b：读 config.json 并盖上命令行覆盖项。** 这一步把 u7-l1 讲过的 `ModelConfigOverride.apply` 用上了：

[python/mlc_llm/interface/gen_config.py:L117-L126](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L117-L126) —— `ModelConfigOverride(...).apply(model.config.from_file(config))`。`from_file` 读 HF 的 `config.json` 生成模型配置对象，`apply` 再把命令行覆盖项（如 `--context-window-size`）盖上去。

`apply` 的实现只覆盖「非 None 且目标拥有该字段」的项，这与 Step 2 的「仅填空」是同一种「不发明、只覆盖」的思想：

[python/mlc_llm/support/config.py:L90-L108](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/config.py#L90-L108) —— `ConfigOverrideBase.apply`：`value is None` 时 `continue` 跳过，字段不存在时警告，否则覆盖并用 `from_dict` 重建。

`ModelConfigOverride` 自己只声明了 8 个可覆盖字段——它正是 CLI 里 `--context-window-size` 等参数的落点：

[python/mlc_llm/interface/compiler_flags.py:L140-L151](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compiler_flags.py#L140-L151) —— `ModelConfigOverride` 的字段：`context_window_size`/`sliding_window_size`/`prefill_chunk_size`/`attention_sink_size`/`max_batch_size`/`tensor_parallel_shards`/`pipeline_parallel_stages`/`disaggregation`。

**Step 1c：构造 `MLCChatConfig` 骨架。** 这是聚合的核心——区分「直接取值」与「留 None」：

[python/mlc_llm/interface/gen_config.py:L127-L145](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L127-L145) —— 构造 `MLCChatConfig`。架构事实字段（`vocab_size`/`context_window_size`/`prefill_chunk_size`/`tensor_parallel_shards`/…）从 `model_config` 直接取值；`conv_template` 用 1a 的结果；`model_task`/`embedding_metadata` 取自信封；这里**没有**给 `temperature`/`top_p`/`eos_token_id` 等传值，它们会落到 schema 的 `None` 默认。

注意两处 `getattr(model_config, "...", default)` 的容错写法：`active_vocab_size` 缺省回退到 `vocab_size`，`pipeline_parallel_stages`/`disaggregation` 缺省回退到 `1`/`False`——因为不是所有模型配置都声明了这些字段。

骨架的字段定义在 schema 里，注意哪些默认是 `None`（它们就是后续要被「填空」的字段）：

[python/mlc_llm/protocol/mlc_chat_config.py:L24-L63](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/mlc_chat_config.py#L24-L63) —— `MLCChatConfig` 字段。`temperature`/`presence_penalty`/`frequency_penalty`/`repetition_penalty`/`top_p`/`pad_token_id`/`bos_token_id`/`eos_token_id` 均默认 `None`；而 `vocab_size`/`context_window_size`/`quantization` 等是必填（无默认），必须由构造期提供。

> 小贴士：`field_model_type`/`field_model_config`/`field_model_task` 用了 `Field(alias=...)`，是因为 Pydantic 默认把以 `model_` 开头的字段当成「受保护命名空间」会报警告，加别名既绕开警告又让落盘 JSON 仍是 `model_type`/`model_config`/`model_task`。

**Step 2：用 `generation_config.json` / `config.json` 填空。**

[python/mlc_llm/interface/gen_config.py:L146-L162](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L146-L162) —— 遍历 `["generation_config.json", "config.json"]`，对每个文件里的每个键，仅当 `MLCChatConfig` 拥有该字段**且当前为 `None`** 时才填入。

两个细节决定了优先级：

- 列表顺序 `generation_config.json` 在前——同一字段若两个文件都有，`generation_config.json` 先把 `None` 填掉，`config.json` 就再无机会覆盖。
- `is None` 闸门——架构字段（构造期已非 `None`）不会被这里改动。

#### 4.1.4 代码实践

**实践目标**：在不运行模型的前提下，靠阅读源码画出「字段来源优先级表」。

**操作步骤**：

1. 打开 [python/mlc_llm/protocol/mlc_chat_config.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/mlc_chat_config.py)，把 `MLCChatConfig` 的字段分成三类：A. 必填无默认（构造期取值）；B. 默认 `None`（待 Step 2/4 填空）；C. 有具体默认值（如 `pipeline_parallel_stages=1`、`version`）。
2. 对 B 类的每个字段，判断它能不能在 `generation_config.json` 里通常找到（如 `temperature`/`top_p` 通常有；`eos_token_id` 通常有）。
3. 写下 `temperature` 字段在三种输入下的最终值：① HF 目录有 `generation_config.json` 且含 `temperature=0.6`；② 没有 `generation_config.json`；③ `generation_config.json` 存在但不含 `temperature`。

**需要观察的现象**：B 类字段就是「优先级链路真正起作用」的字段；A 类字段优先级在构造期即终结。

**预期结果**：

| 情形 | `temperature` 最终值 | 来源 |
| --- | --- | --- |
| ① 有 `generation_config.json` 含 0.6 | `0.6` | Step 2 |
| ② 无 `generation_config.json` | `1.0` | Step 4 系统默认 |
| ③ 有该文件但不含 `temperature` | `1.0` | Step 4 系统默认 |

> 待本地验证：若本地有一份 HF 模型目录，可 `cat generation_config.json` 与最终 `mlc-chat-config.json` 对照确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Step 2 的循环里要判断 `getattr(mlc_chat_config, key) is None`？如果去掉这个判断会怎样？

**参考答案**：这是「仅填空」闸门，保证生成参数文件**不覆盖**架构事实。如果去掉，`config.json` 里的 `vocab_size` 等会盖掉 Step 1c 已经从模型配置算好的值；更糟的是 `config.json` 里可能有同名但语义不同的键，造成混乱。保留闸门后，架构字段的优先级永远最高。

**练习 2**：`generation_config.json` 与 `config.json` 都含 `bos_token_id` 时，最终用哪一个？

**参考答案**：用 `generation_config.json` 的。因为循环顺序是 `["generation_config.json", "config.json"]`，前者先把 `None` 填掉，后者因 `is None` 不成立而跳过。这体现「更专门的文件优先」。

**练习 3**：`active_vocab_size` 在 Step 1c 被设成 `getattr(model_config, "active_vocab_size", model_config.vocab_size)`，它还会被 Step 2 改动吗？

**参考答案**：不会。因为它在构造期已经是非 `None` 的整数（即便模型没有该字段，也回退成了 `vocab_size`），Step 2 的 `is None` 闸门会跳过它。它真正可能被改动是在**后面的 Step 5**（用 HF tokenizer 实测覆盖，见 4.3）。

---

### 4.2 tokenizer 配置生成：复制、转换与探测（Step 3）

#### 4.2.1 概念说明

`gen_config` 不只生成 JSON，还**把 tokenizer 文件搬进产物目录**，并保证运行期引擎能直接用。这一步（Step 3）要处理三类麻烦：

1. **tokenizer 文件格式不统一**：有的模型用 `tokenizer.json`（HF fast tokenizer，运行期最友好），有的只有 `tokenizer.model`（SentencePiece），有的用 tiktoken 的 `*.tiktoken`，RWKV 甚至用自带的 `rwkv_vocab_vXXXXXXX.json/txt`。
2. **运行期倾向 `tokenizer.json`**：MLC 运行期（`tokenizers-cpp`）最喜欢 `tokenizer.json`，所以 `gen_config` 会尽量把别的格式**转换**成它。
3. **需要探测 tokenizer 的「行为元信息」**：比如解码时是 `byte_fallback`（LLaMA-2 风格）还是 `byte_level`（LLaMA-3/GPT-2 风格）——这决定运行期如何把 token 还原成字符串。

所以 Step 3 的本质是：**复制能直接用的 → 转换不能直接用的 → 探测运行期需要的行为信息 → 校验正确性。**

#### 4.2.2 核心流程

Step 3 的五个子步骤（源码注释 3.1–3.5）：

```
Step 3.1  复制 TOKENIZER_FILES 中存在的文件（tokenizer.json/model、vocab.json、merges.txt…）
          → 同时把文件名登记进 mlc_chat_config.tokenizer_files
   │
   ▼
Step 3.2  若发现 rwkv_vocab_v<8位日期>.(json|txt) → 生成二进制 tokenizer_model（msgpack）
   │
   ▼
Step 3.3a 若只有 tokenizer.model 而无 tokenizer.json → 用 transformers 转成 tokenizer.json
Step 3.3b 若仍无 tokenizer.json 但有 *.tiktoken     → convert_tiktoken 转成 tokenizer.json
   │
   ▼
Step 3.4  Tokenizer.detect_tokenizer_info(产物目录) → 填 mlc_chat_config.tokenizer_info
   │
   ▼
Step 3.5  校验 tokenizer.json 的 added_tokens 无重复（防模型发布者的常见错误）
```

#### 4.2.3 源码精读

**Step 3.1：复制 tokenizer 文件并登记。**

先看「哪些文件算 tokenizer 文件」——这是一张写死的清单：

[python/mlc_llm/interface/gen_config.py:L294-L301](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L294-L301) —— `TOKENIZER_FILES` 列表：`tokenizer.model`/`tokenizer.json`/`vocab.json`/`merges.txt`/`added_tokens.json`/`tokenizer_config.json`。

复制循环把存在的文件 `shutil.copy` 到产物目录，并把文件名追加到 `tokenizer_files`：

[python/mlc_llm/interface/gen_config.py:L166-L174](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L166-L174) —— 遍历 `TOKENIZER_FILES`，存在则复制 + 登记，不存在则记日志。

**Step 3.2：RWKV 词表生成二进制 `tokenizer_model`。** RWKV 系列不用标准 HF tokenizer，而用自带词表文件（文件名形如 `rwkv_vocab_v20230424.txt`）。`gen_config` 用正则识别它，按扩展名走两条解析路径，最终用 `msgpack` 打包成 `{id: bytes}` 字典：

[python/mlc_llm/interface/gen_config.py:L176-L188](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L176-L188) —— 用正则 `rwkv_vocab_v\d{8}\.(json|txt)` 匹配；`.txt` 走 `txt2rwkv_tokenizer`，`.json` 走 `json2rwkv_tokenizer`。

两个转换函数都把 `idx → bytes` 写进 msgpack：

[python/mlc_llm/interface/gen_config.py:L47-L69](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L47-L69) —— `txt2rwkv_tokenizer`：逐行解析 `idx raw length` 三段式文本词表，`eval(raw)` 还原成 bytes（`check_string` 先校验它是合法字面量），断言长度匹配，msgpack 落盘。

[python/mlc_llm/interface/gen_config.py:L72-L86](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L72-L86) —— `json2rwkv_tokenizer`：直接 `json.load`，把 key 编码成 bytes，msgpack 落盘。

**Step 3.3a：`tokenizer.model` → `tokenizer.json`。** 若模型只有 SentencePiece 的 `tokenizer.model` 而没有 `tokenizer.json`，借助 `transformers.AutoTokenizer` 把它转成运行期更友好的 fast tokenizer：

[python/mlc_llm/interface/gen_config.py:L189-L218](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L189-L218) —— 条件是「有 `tokenizer.model` 且无 `tokenizer.json`」；用 `AutoTokenizer.from_pretrained(..., use_fast=True).backend_tokenizer.save(...)` 导出 `tokenizer.json`，并登记进 `tokenizer_files`；转换失败则 `try/except` 跳过（不致命）。

**Step 3.3b：tiktoken → `tokenizer.json`。** tiktoken 系（如一些 GPT 类模型）用 `*.tiktoken` 文件存 BPE 词表，`convert_tiktoken` 把它转成 HF `tokenizer.json` 格式：

[python/mlc_llm/interface/gen_config.py:L220-L235](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L220-L235) —— 条件是「仍无 `tokenizer.json` 且目录里有 `*.tiktoken`」；调用 `convert_tiktoken.convert_tiktoken(src, output, context_window_size)`，并把生成的 `tokenizer.json`/`vocab.json`/`merges.txt`/`special_tokens_map.json` 登记进 `tokenizer_files`。

转换器内部用 BPE 算法从 `mergeable_ranks` 反推出 vocab 与 merges，套进一个 GPT-2 风格的 `tokenizer.json` 模板：

[python/mlc_llm/support/convert_tiktoken.py:L65-L67](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/convert_tiktoken.py#L65-L67) —— `convert_tiktoken(model_path, output_dir, context_window_size=None)` 入口。

[python/mlc_llm/support/convert_tiktoken.py:L12-L31](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/convert_tiktoken.py#L12-L31) —— `bpe`：标准的「贪心合并最低 rank 字节对」算法，把一个 token 还原成它的合并序列，用于推导 merges。

**Step 3.4：探测 tokenizer 行为元信息。** 转换/复制完之后，调用 TVM 侧的 `DetectTokenizerInfo` 探测产物目录，把结果填进 `tokenizer_info`：

[python/mlc_llm/interface/gen_config.py:L237-L239](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L237-L239) —— `Tokenizer.detect_tokenizer_info(str(output))`，结果 `asdict` 后赋给 `tokenizer_info`。

`TokenizerInfo` 只关心三件事——解码方式、编码是否前加空格、解码是否去掉首空格：

[python/mlc_llm/tokenizers/tokenizers.py:L17-L45](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/tokenizers/tokenizers.py#L17-L45) —— `TokenizerInfo` 数据类：`token_postproc_method`（`byte_fallback` 如 LLaMA-2/Mixtral，或 `byte_level` 如 LLaMA-3/GPT-2/Phi-2）、`prepend_space_in_encode`、`strip_space_in_decode`。

这三个字段决定了运行期如何把生成的 token id 还原成人类可读文本，所以必须在 `gen_config` 阶段探测好写进配置：

[python/mlc_llm/tokenizers/tokenizers.py:L114-L127](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/tokenizers/tokenizers.py#L114-L127) —— `Tokenizer.detect_tokenizer_info`：经 `_ffi_api.DetectTokenizerInfo` 调进 C++ 侧分析，再用 `TokenizerInfo.from_json` 反序列化。

**Step 3.5：added_tokens 去重校验。** 这是一道正确性防线——某些模型发布者（链接里的 Hermes-2-Pro 案例）会在 `tokenizer.json` 的 `added_tokens` 里放重复 token，破坏 HF tokenizer 的一致性，导致运行期解码错误。`gen_config` 在此主动检测并**抛异常**：

[python/mlc_llm/interface/gen_config.py:L242-L260](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L242-L260) —— 若 `tokenizer.json` 存在，扫描 `added_tokens`，用 `appeared_content` 集合发现重复 `content` 即 `raise ValueError("Duplicated vocab in tokenizer.json")`，把错误前置到编译准备期。

#### 4.2.4 代码实践

**实践目标**：跟踪 tokenizer 文件从「HF 目录」到「产物目录 + tokenizer_info」的完整旅程。

**操作步骤**：

1. 选一个**只有 `tokenizer.model`** 的小模型（如老版 LLaMA-2、或带 SentencePiece 的模型），运行 `gen_config`（命令见下方综合实践）。
2. 对比源目录与产物目录：`ls` 两边的 tokenizer 相关文件，确认 `tokenizer.model` 被复制、且多出了一个新生成的 `tokenizer.json`。
3. 打开产物 `mlc-chat-config.json`，找到 `tokenizer_files` 与 `tokenizer_info` 两个字段。
4. 再选一个 **tiktoken** 系模型（带 `*.tiktoken`）重复上述对比。

**需要观察的现象**：

- 「`.model` only」模型：产物 `tokenizer_files` 里同时含 `tokenizer.model` 与 `tokenizer.json`，且后者是 Step 3.3a 新生成的。
- tiktoken 模型：产物多出 `vocab.json`/`merges.txt`/`special_tokens_map.json`/`tokenizer.json` 四件套。
- `tokenizer_info.token_postproc_method`：LLaMA-2 风格是 `byte_fallback`，LLaMA-3/GPT-2 风格是 `byte_level`。

**预期结果**：你能用一句话说明「为什么运行期只认 `tokenizer.json`，而 `gen_config` 要兜底把各种格式都转成它」。

> 待本地验证：上述需要真实 HF 模型目录。若本地无模型，可退化为阅读实践——对照 Step 3.1–3.5 的源码，画出每种「源格式 → 产物」的转换分支图。

#### 4.2.5 小练习与答案

**练习 1**：Step 3.3a 和 Step 3.3b 都是「转换出 `tokenizer.json`」，它们的触发条件有何不同？为什么 3.3b 还要再判一次「仍无 `tokenizer.json`」？

**参考答案**：3.3a 触发于「有 `tokenizer.model` 且无 `tokenizer.json`」，3.3b 触发于「仍无 `tokenizer.json` 且有 `*.tiktoken`」。3.3b 之所以要再判一次「仍无」，是因为 3.3a 可能刚刚成功生成了 `tokenizer.json`——如果 3.3a 成功了，3.3b 就不该再跑（否则会白做一遍甚至覆盖）。两个步骤合起来构成「尽量保证最终有 `tokenizer.json`」的兜底链。

**练习 2**：`tokenizer_info` 为什么要在 `gen_config` 阶段探测并写进配置，而不是运行期现算？

**参考答案**：因为它是 tokenizer 的**固有属性**（取决于词表类型，不随请求变化），在准备期一次性算好写进配置，运行期直接读取即可，避免每次启动引擎都重新分析词表；同时它也作为「契约」让编译期与运行期对解码行为达成一致。

**练习 3**：Step 3.5 发现重复 token 时为什么选择**抛异常**，而不是像 Step 3.3 那样 `try/except` 跳过？

**参考答案**：因为重复 `added_tokens` 会**破坏运行期解码正确性**（HF tokenizer 行为不一致），属于必须修的硬错误， silently 跳过会让模型在运行时产生难以定位的乱码。而 Step 3.3 的转换失败只是「少了最优格式」，运行期仍可退回用 `tokenizer.model`，属于可降级的软错误，故用 `try/except` 跳过并记日志。

---

### 4.3 系统默认填充与最终落盘（Step 4 & 5）

#### 4.3.1 概念说明

聚合的最后两步是「兜底」与「定稿」：

- **Step 4（系统默认填充）**：经过 Step 1–3，仍可能有些生成参数字段是 `None`（HF 目录里既没有 `generation_config.json`，也没在 `config.json` 里给出）。这时用一张写死的「系统默认表」`MLC_CHAT_SYSTEM_DEFAULT` 兜底，保证运行期拿到配置时这些字段**一定有值**。
- **Step 5（active_vocab_size 实测 + 落盘）**：用 HF tokenizer 实测「真实词表大小」覆盖可能被 padded 的 `active_vocab_size`（这点 u2-l2 已提过），最后把整个 `MLCChatConfig` 序列化成 `mlc-chat-config.json`。

至此，一个字段的完整优先级链路是：**模型配置（构造期取值）> generation_config.json（Step 2 填空）> 系统默认（Step 4 兜底）**，且对 `active_vocab_size` 还有一个「HF tokenizer 实测」的最终覆盖（Step 5）。

#### 4.3.2 核心流程

```
Step 4  apply_system_defaults_for_missing_fields(config)
        │  for key, value in MLC_CHAT_SYSTEM_DEFAULT:
        │      if config.key is None: config.key = value
        ▼
Step 5a 若有 tokenizer.json：用 HF tokenizer 实测 len(tokenizer)，覆盖 active_vocab_size
Step 5b json.dump(config.model_dump(by_alias=True)) → mlc-chat-config.json
```

#### 4.3.3 源码精读

**系统默认表**——只有 8 个生成相关字段，且都是「安全中性」的默认（温度 1.0、top_p 1.0、无惩罚等）：

[python/mlc_llm/protocol/mlc_chat_config.py:L11-L21](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/mlc_chat_config.py#L11-L21) —— `MLC_CHAT_SYSTEM_DEFAULT`：`pad_token_id=0`/`bos_token_id=1`/`eos_token_id=2`/`temperature=1.0`/`presence_penalty=0.0`/`frequency_penalty=0.0`/`repetition_penalty=1.0`/`top_p=1.0`。

**Step 4 的填充函数**——遍历默认表，**只填仍为 `None`** 的字段（与 Step 2 同样的「仅填空」思想）：

[python/mlc_llm/interface/gen_config.py:L28-L32](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L28-L32) —— `apply_system_defaults_for_missing_fields`：对 `get_system_defaults_for_missing_fields()` 返回的每个键值 `setattr`。

注意它把「哪些字段缺值」的判断下沉到 `MLCChatConfig` 自己的方法里，这样默认表与「填空」逻辑解耦：

[python/mlc_llm/protocol/mlc_chat_config.py:L65-L78](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/mlc_chat_config.py#L65-L78) —— `get_system_defaults_for_missing_fields`：遍历 `MLC_CHAT_SYSTEM_DEFAULT`，仅收集当前值为 `None` 的字段。注释解释了为何这样设计——便于「先创建 `MLCChatConfig`、中途覆盖可选值、最后统一套默认」。

Step 4 的调用点：

[python/mlc_llm/interface/gen_config.py:L262-L263](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L262-L263) —— Step 4 调用 `apply_system_defaults_for_missing_fields(mlc_chat_config)`。

**Step 5a：实测 active_vocab_size。** 很多模型为了张量对齐会把 `vocab_size` 向上 pad 到 64/256 的倍数，但真正用到的词表（active）往往更小。用 HF tokenizer 的 `len()` 实测真实大小，可以让运行期 lm_head 只算 active 部分，省计算、省显存：

[python/mlc_llm/interface/gen_config.py:L265-L286](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L265-L286) —— 若有 `tokenizer.json`，用 `AutoTokenizer.from_pretrained` 实测 `len(hf_tokenizer)`，与当前 `active_vocab_size` 不同则覆盖并记日志；失败则 `try/except` 跳过（不致命）。

> 这一步与 u2-l2 讲的「gen_config 用 HF tokenizer 实测 active_vocab_size 覆盖被 padded 的 vocab_size」完全对应——这是本讲在源码层的落点。

**Step 5b：落盘。** 用 `model_dump(by_alias=True)` 保证字段以别名（`model_type`/`model_config`/`model_task`）写出，与 schema 的 `Field(alias=...)` 设计呼应：

[python/mlc_llm/interface/gen_config.py:L288-L291](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L288-L291) —— `json.dump(mlc_chat_config.model_dump(by_alias=True), out_file, indent=2)`，写出 `mlc-chat-config.json`。（源码此处注释编号误写为「Step 5」，与上一段 Step 5 重复，但语义上是「定稿落盘」。）

落盘的 `version` 字段来自一个常量，用于运行期做兼容性判断：

[python/mlc_llm/support/constants.py:L8](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/constants.py#L8) —— `MLC_CHAT_CONFIG_VERSION = "0.1.0"`，作为 `MLCChatConfig.version` 的默认值写入配置。

#### 4.3.4 代码实践

**实践目标**：验证「系统默认」确实只兜底未被任何来源提供的字段。

**操作步骤**：

1. 想象一个**没有 `generation_config.json`** 的极简 HF 目录（只有 `config.json` + tokenizer）。
2. 推演 `temperature`/`top_p`/`repetition_penalty`/`eos_token_id` 四个字段在各步骤后的值：
   - Step 1c 后：均为 `None`；
   - Step 2 后：`config.json` 里若有则填，否则仍 `None`；
   - Step 4 后：用系统默认兜底。
3. 打开 [python/mlc_llm/protocol/mlc_chat_config.py:L11-L21](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/mlc_chat_config.py#L11-L21) 的默认表，确认你的推演。

**需要观察的现象**：即便 HF 目录信息缺失，最终 `mlc-chat-config.json` 里这四个字段也一定非 `None`。

**预期结果**：`temperature=1.0`、`top_p=1.0`、`repetition_penalty=1.0`、`eos_token_id=2`（前提是 `config.json` 没给）。

> 待本地验证：可构造一个最小 HF 目录实测，或直接阅读 `get_system_defaults_for_missing_fields` 确认逻辑。

#### 4.3.5 小练习与答案

**练习 1**：如果用户希望 `temperature` 最终是 `0.6`，但既不改 `generation_config.json` 也不传命令行参数，能靠「系统默认」实现吗？

**参考答案**：不能。系统默认表里 `temperature` 恒为 `1.0`，且它是「最后兜底」——只在所有来源都没提供时才生效。要得到 `0.6`，要么在 HF 目录的 `generation_config.json` 里写 `temperature=0.6`（Step 2 填入），要么运行期在请求里覆盖（`/set temperature=0.6` 或请求体 `temperature`）。`gen_config` 阶段不提供单独的 `--temperature` 命令行参数。

**练习 2**：Step 5a 实测 `active_vocab_size` 与 Step 1c 的 `active_vocab_size=getattr(..., vocab_size)` 是什么关系？

**参考答案**：Step 1c 给的是「保守初值」——若模型配置没声明 `active_vocab_size` 就退回 `vocab_size`（可能被 padded）。Step 5a 用 HF tokenizer 实测真实词表大小，**覆盖**这个初值。两者是「先给保守值、再用实测精修」的关系，目的是让运行期用尽可能小的真实词表尺寸。

**练习 3**：为什么最后落盘要用 `model_dump(by_alias=True)` 而不是普通的 `asdict` 或不带别名 的 dump？

**参考答案**：因为 schema 里 `model_type`/`model_config`/`model_task` 三个字段为了避免 Pydantic 的 `model_` 保护命名空间警告，实际属性名是 `field_model_type` 等、再用 `Field(alias=...)` 声明别名。若不用 `by_alias=True`，落盘的键会变成 `field_model_type`，与运行期/编译期期望的 `model_type` 不一致，会破坏契约。`by_alias=True` 保证对外键名符合 `mlc-chat-config.json` 的约定。

---

## 5. 综合实践

把本讲的三块知识（config 聚合、tokenizer 生成、系统默认）串成一个端到端任务：**亲手生成一份 `mlc-chat-config.json`，并逐字段标注来源。**

**实践目标**：对真实产物里的每个字段，说出它来自「model config / conversation template / generation_config.json / 系统默认 / 实测」中的哪一个。

**操作步骤**：

1. 选一个小模型（如 `RedPajama-INCITE-Chat-3B-v1` 或任何你本地有的 HF 模型目录），运行：

   ```bash
   mlc_llm gen_config <HF 模型目录> \
       --quantization q4f16_1 \
       --model-type auto \
       --conv-template redpajama_chat \
       -o ./dist/output
   ```

   > 命令各参数含义见 [python/mlc_llm/cli/gen_config.py:L24-L104](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/gen_config.py#L24-L104)。`--conv-template` 必填且取值受限，choices 来自 `CONV_TEMPLATES`：

   [python/mlc_llm/interface/gen_config.py:L304-L359](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py#L304-L359) —— `CONV_TEMPLATES` 集合，作为 CLI `--conv-template` 的合法取值。

2. 打开 `./dist/output/mlc-chat-config.json`，按下表逐字段标注来源（示例答案供自查）：

   | 字段 | 典型来源 | 对应步骤 |
   | --- | --- | --- |
   | `model_type` | model 信封（`model.name`，经 `--model-type` 解析） | Step 1c |
   | `quantization` | 命令行 `--quantization`（`QUANTIZATION` 注册表） | Step 1c |
   | `model_config` | `config.json` + `ModelConfigOverride` 覆盖 | Step 1b/1c |
   | `vocab_size` | model config | Step 1c |
   | `active_vocab_size` | HF tokenizer 实测（覆盖初值） | Step 5a |
   | `context_window_size` | model config（可被 `--context-window-size` 覆盖） | Step 1b/1c |
   | `conv_template` | `ConvTemplateRegistry`（`--conv-template` 指定） | Step 1a |
   | `temperature` / `top_p` | `generation_config.json`，缺失则系统默认 | Step 2 / Step 4 |
   | `eos_token_id` | `generation_config.json`，缺失则系统默认 `2` | Step 2 / Step 4 |
   | `tokenizer_files` | 产物目录里实际复制/转换出的文件名 | Step 3.1/3.3 |
   | `tokenizer_info` | `Tokenizer.detect_tokenizer_info` 探测 | Step 3.4 |
   | `version` | 常量 `MLC_CHAT_CONFIG_VERSION` | schema 默认 |

3. 回答三个验收问题：
   - 你的产物里 `temperature` 的值是多少？它是从哪个文件来的，还是系统默认？
   - `tokenizer_files` 里有没有 `tokenizer.json`？它是直接复制的，还是由 `tokenizer.model`/tiktoken 转换来的？
   - `active_vocab_size` 与 `vocab_size` 是否相等？若不等，哪个更大、为什么？

**预期结果**：你能对每一个字段说出「它由哪一步、从哪个来源写入」，并在脑中画出完整的优先级链路图。

> 待本地验证：本实践需要可联网下载或本地已有的 HF 模型目录，以及安装了 `transformers`（Step 3.3 转换需要）。若无模型，可退化为纯阅读实践——直接对照上表，在源码里找到每个字段被赋值的那一行（本讲 4.1–4.3 已给出全部行号），完成「字段 → 源码行 → 来源」的对应。

## 6. 本讲小结

- `gen_config()` 是一个**聚合器**，分五步把 HF 目录聚合成 `mlc-chat-config.json`：Step 1 构造骨架（取对话模板 + 读 config.json + 命令行覆盖）→ Step 2 用 `generation_config.json`/`config.json` 填空 → Step 3 处理 tokenizer（复制/转换/探测/校验）→ Step 4 套系统默认 → Step 5 实测 `active_vocab_size` 并落盘。
- 字段值的优先级链路是：**模型配置（构造期）> `generation_config.json`（Step 2）> 系统默认（Step 4）**；其底层机制是 Step 2 的 `is None` 闸门——只填空、不覆盖，保证架构事实永远优先。
- tokenizer 处理遵循「**尽量产出 `tokenizer.json`**」原则：直接复制 `TOKENIZER_FILES`，缺失时依次尝试 RWKV 词表生成、`.model`→`.json`、tiktoken→`.json` 转换，最后探测 `tokenizer_info` 并校验 `added_tokens` 去重。
- 系统默认表 `MLC_CHAT_SYSTEM_DEFAULT` 只含 8 个生成参数字段，是「安全中性」的最后兜底；`get_system_defaults_for_missing_fields` 把「判断缺值」下沉到 schema 类，实现「先创建、后填默认」的懒填充。
- `active_vocab_size` 经历「保守初值（vocab_size）→ HF tokenizer 实测覆盖」两步，让运行期用真实词表尺寸而非 padded 尺寸。
- 落盘用 `model_dump(by_alias=True)`，确保 `model_type`/`model_config`/`model_task` 等以别名写出，符合 `mlc-chat-config.json` 的跨期契约。

## 7. 下一步学习建议

本讲讲完了「`mlc-chat-config.json` 如何被造出来」。建议接下来：

- **进入 U8（编译优化 pass 深入）**：u7-l2 画了 pass 流水线地图，U8 各讲义（融合/派发/附加/低 batch 与内存）逐个展开算法。本讲产出的 `mlc-chat-config.json` 正是 u7-l1 `compile()` 的输入，至此你已完整掌握「产物 → 编译」的入口。
- **回顾 u4-l3（convert_weight 全流程）**：`convert_weight` 与 `gen_config` 都直读 HF 目录、彼此独立（见 u1-l4）。对比两者的「聚合/转换」思路，能加深对「四步工作流」分工的理解。
- **结合 u11（Python 引擎与服务端）**：运行期 `MLCEngine` 启动时会读 `mlc-chat-config.json` 来构造对话模板、加载 tokenizer、设定采样默认值——本讲的每个字段都将在那里被消费，带着本讲的字段来源表去读 u11 会非常有收获。
- **阅读 [python/mlc_llm/interface/gen_config.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/gen_config.py) 全文**：它是本讲的主角，篇幅不长（约 360 行），通读一遍能把五个步骤的细节（日志、异常处理、边界条件）补全。
