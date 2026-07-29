# 聊天模板 Chat Template

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「聊天模板」到底解决了什么问题：为什么同一个对话，喂给不同模型前需要被渲染成不同的字符串。
- 读懂 `PreTrainedTokenizerBase.apply_chat_template()` 的完整调用链，理解 `add_generation_prompt`、`continue_final_message`、`tokenize` 等关键参数的作用。
- 看懂 `utils/chat_template_utils.py` 里的 Jinja 渲染引擎：模板如何被编译、如何被沙箱化执行、`{% generation %}` 如何追踪助手 token。
- 了解聊天模板如何扩展到「工具调用（tool use）」与「多模态（图像）」两类高级场景。

本讲承接 [u3-l2](./u3-l2-batch-encoding-decode.md) 讲过的 `__call__` 编码机制——聊天模板本质上是**在 `__call__` 之前多加了一步「把对话渲染成字符串」**的预处理。

## 2. 前置知识

- **因果语言模型（Causal LM）的本质**：无论是普通的「续写」模型，还是「对话」模型，它们做的事都是**接龙**——给定一段 token 序列，预测下一个 token。所谓「聊天模型」并没有新的底层机制，它只是在一大段通用文本上预训练后，又用「按对话格式排列的文本」做了微调（fine-tuning for chat）。
- **控制 token（control tokens）**：为了让模型「看懂」对话结构，训练数据里会插入一些特殊标记，比如 `<|user|>`、`<|assistant|>`、`<|end_of_message|>`。不同模型用的标记和格式**各不相同**，即便它们都是从同一个 base 模型微调出来的。
- **Jinja 模板**：一种文本模板语言，用 `{% %}` 写控制逻辑、`{{ }}` 插值。transformers 用 Jinja2 来描述「对话 → 字符串」的渲染规则。
- 本讲假设你已经熟悉 `tokenizer(...)`（即 `__call__`）返回的 `BatchEncoding` 结构。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/transformers/tokenization_utils_base.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py) | 定义所有分词器基类，包含本讲的对外入口 `apply_chat_template()`、模板选择 `get_chat_template()`、模板保存 `save_chat_templates()`，以及 `chat_template` 属性的初始化。 |
| [src/transformers/utils/chat_template_utils.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/chat_template_utils.py) | 聊天模板的「渲染引擎」：Jinja 沙箱环境的编译、`render_jinja_template()` 渲染主流程、`{% generation %}` 助手 token 追踪，以及把 Python 函数转成工具 JSON Schema 的 `get_json_schema()`。 |
| [docs/source/en/chat_templating.md](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/chat_templating.md) | 官方聊天模板教程，用 Mistral / Zephyr 两个对比例子讲清「为什么需要模板」，并解释 `add_generation_prompt`、`continue_final_message` 的语义。 |

> 关键认知：仓库里**并没有**打包任何 `.jinja` 文件。模型自带的聊天模板几乎都存在 Hub 上该 checkpoint 的 `tokenizer_config.json`（旧格式）或独立的 `chat_template.jinja`（新格式）里，加载时分词器时被读入 `tokenizer.chat_template` 属性。

## 4. 核心概念与源码讲解

### 4.1 聊天模板：从「消息列表」到「模型专属字符串」

#### 4.1.1 概念说明

用户调聊天模型时，输入通常长这样——一个「消息列表」，每条消息有 `role`（角色）和 `content`（内容）：

```python
messages = [
    {"role": "user", "content": "Hello, how are you?"},
    {"role": "assistant", "content": "I'm doing great!"},
    {"role": "user", "content": "Show off how chat templating works!"},
]
```

但模型并不认识这个数据结构，它只认 token 序列。于是必须有一层翻译，把这个列表渲染成一段**该模型训练时见过的字符串**。问题是：不同模型训练时用的格式完全不同。

官方文档用一对最有说服力的例子说明这一点——`mistralai/Mistral-7B-Instruct-v0.1` 和 `HuggingFaceH4/zephyr-7b-beta` 其实都是从同一个 Mistral-7B base 微调出来的，但渲染结果天差地别：Mistral 用 `[INST] ... [/INST]`，Zephyr 用 `<|user|>` / `<|assistant|>`。详见 [chat_templating.md:L23-L74](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/chat_templating.md#L23-L74)（这段是本讲最核心的「为什么需要聊天模板」论述）。

结论：用错控制 token，模型效果会显著下降。聊天模板就是每个模型自带的「格式说明书」，把这段说明书（一段 Jinja 代码）作为模型的随附文件，用户无需记住任何模型的格式细节。

#### 4.1.2 核心流程

模板的存储与流转大致如下：

```
Hub checkpoint
  ├── tokenizer_config.json   （旧格式：模板作为字符串/列表嵌在里面）
  └── chat_template.jinja      （新格式：单模板独立文件）
  └── additional_chat_templates/   （新格式：多模板目录）
        │
        ▼  from_pretrained 加载分词器时读取
tokenizer.chat_template      （str，或 dict{name: template}）
        │
        ▼  apply_chat_template() 调用
渲染引擎 render_jinja_template()
        │
        ▼  Jinja 沙箱执行
渲染后的字符串
        │
        ▼  tokenize=True 时走 self(...)（即 __call__）
input_ids（BatchEncoding）
```

其中两个「存储文件名」常量定义在 [utils/hub.py:L66-L67](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/hub.py#L66-L67)：`CHAT_TEMPLATE_FILE = "chat_template.jinja"`、`CHAT_TEMPLATE_DIR = "additional_chat_templates"`。

#### 4.1.3 源码精读

**① `chat_template` 属性如何被建立。** 分词器基类 `__init__` 里把传入的 `chat_template` 参数挂到实例上；如果它是个列表（旧格式多模板，形如 `[{"name":..., "template":...}]`），则还原成 dict：

[tokenization_utils_base.py:L1088-L1092](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1088-L1092) —— 建立 `self.chat_template`，并把列表形态的多模板归一成 dict。

**② 模板如何被保存（新格式 vs 旧格式）。** `save_chat_templates()` 决定把模板写到磁盘还是塞回 `tokenizer_config.json`：

[tokenization_utils_base.py:L3309-L3345](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L3309-L3345) —— 当 `save_jinja_files=True` 且模板是 str，写 `chat_template.jinja`；若是 dict，把 `default` 写成 `chat_template.jinja`、其余写到 `additional_chat_templates/` 目录（注意里面对 `template_name` 做了路径穿越校验，防 CWE-22）；否则回退到旧格式，把模板塞进 `tokenizer_config`。

#### 4.1.4 代码实践

**实践目标**：亲眼看到一个模型自带的模板字符串，理解它就是一段 Jinja 代码。

**操作步骤**：

1. 选一个带聊天模板的 checkpoint（如 `Qwen/Qwen2.5-0.5B-Instruct` 或 `HuggingFaceH4/zephyr-7b-beta`）。
2. 加载 tokenizer，打印 `tokenizer.chat_template`。
3. 用 `tokenize=False` 渲染一段对话，对比「未渲染的消息列表」与「渲染后的字符串」。

```python
# 示例代码（需联网下载 checkpoint）
from transformers import AutoTokenizer

tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-0.5B-Instruct")

# 1. 看看模板本身长什么样（一段 Jinja 代码）
print("--- 模板前 300 字 ---")
print(tokenizer.chat_template[:300])

# 2. 看看渲染结果
messages = [
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "你好！有什么可以帮你？"},
    {"role": "user", "content": "解释一下聊天模板"},
]
rendered = tokenizer.apply_chat_template(messages, tokenize=False)
print("--- 渲染后的字符串 ---")
print(repr(rendered))
```

**需要观察的现象**：`chat_template` 里会出现 `{% for message in messages %}`、`{{ message['role'] }}` 之类的 Jinja 语法，以及 `<|im_start|>` 这类 ChatML 控制标记。

**预期结果**：渲染后的字符串里能看到 `<|im_start|>user\n你好<|im_end|>` 这类结构。如果该 checkpoint 没有自带模板，`apply_chat_template` 会抛 `ValueError`（提示 `tokenizer.chat_template is not set`）。

**待本地验证**：具体控制 token 取决于你加载的 checkpoint，上面以 Qwen 系的 ChatML 为例。

#### 4.1.5 小练习与答案

**练习 1**：为什么说「聊天模型本质还是语言模型」？聊天模板在这句话里扮演什么角色？

> **答案**：聊天模型只是在 base 模型上用「按对话格式排列的文本」做了微调，底层依然是「续写 token 序列」。聊天模板的职责，就是把你给的消息列表翻译成该模型微调时见过的那串字符（含控制 token），让模型「认得出」对话结构。

**练习 2**：`tokenizer.chat_template` 可以是 `str` 也可以是 `dict`，分别对应什么场景？

> **答案**：`str` 表示该模型只有一份默认模板；`dict`（形如 `{"default": ..., "tool_use": ...}`）表示模型有多份模板（例如默认对话模板 + 专门用于工具调用的模板），渲染时按名称选择。

---

### 4.2 apply_chat_template：对外入口与完整流程

#### 4.2.1 概念说明

`apply_chat_template()` 是用户唯一需要关心的入口。它的职责可以一句话概括：**选模板 → 渲染成字符串 →（可选）分词成 input_ids**。它把「对话结构」这个高层抽象，落地成模型能直接吃下去的 token。

#### 4.2.2 核心流程

```
apply_chat_template(conversation, ...)
   │
   ├─ ① 校验参数（tokenize、return_assistant_tokens_mask、continue_final_message 互斥关系）
   │
   ├─ ② chat_template = self.get_chat_template(chat_template, tools)
   │        （选定本次用哪份模板：dict 多模板时按 tools/名称选）
   │
   ├─ ③ 判断 batched（conversation[0] 是否又是列表/带 .messages）
   │
   ├─ ④ 校验 continue_final_message 与 add_generation_prompt 互斥
   │
   ├─ ⑤ template_kwargs = {**special_tokens_map, **kwargs}
   │        渲染引擎 render_jinja_template(...)  →  rendered_chat (字符串)
   │
   └─ ⑥ if tokenize:
            self(rendered_chat, add_special_tokens=False, ...)   # 走 __call__ 分词
            （若 return_assistant_tokens_mask，还要算 assistant_masks）
        else:
            return rendered_chat（字符串）
```

注意第 ⑥ 步：分词时 `add_special_tokens=False`。原因是模板已经把所有该有的控制 token 渲染进字符串了，不能再让 `__call__` 重复加 BOS 之类的特殊 token。

#### 4.2.3 源码精读

**① 方法签名与全部参数。** 这是整个 API 的契约，参数分三组：渲染控制（`chat_template`/`add_generation_prompt`/`continue_final_message`）、分词控制（`tokenize`/`padding`/`truncation`/`max_length`/`return_tensors`）、高级产物（`return_assistant_tokens_mask`/`tools`/`documents`）：

[tokenization_utils_base.py:L2999-L3016](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2999-L3016) —— `apply_chat_template` 的完整签名。

**② 选模板。** 先通过 `get_chat_template` 决定本次渲染用哪份模板：

[tokenization_utils_base.py:L3095](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L3095) —— 调用 `self.get_chat_template(chat_template, tools)` 选定模板字符串。

`get_chat_template()` 的选模板逻辑有三层优先级：用户显式传模板 > （多模板时）按 `tools` 选 `tool_use` > 选 `default` > 报错：

[tokenization_utils_base.py:L3257-L3287](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L3257-L3287) —— dict 多模板时优先选 `tool_use`（有 tools 时）或 `default`；单模板时回退到 `self.chat_template`；都没有则抛错。

**③ 合并 kwargs 并调用渲染引擎。** 把 `special_tokens_map`（含 `bos_token`/`eos_token` 等）与用户额外 kwargs 合并，作为模板变量传入；用户 kwargs 优先级更高：

[tokenization_utils_base.py:L3117-L3127](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L3117-L3127) —— `template_kwargs` 合并 + 调用 `render_jinja_template`。

**④ 分词分支。** 拿到字符串后，若 `tokenize=True`，调用 `self(...)`（即 `__call__`）分词，注意 `add_special_tokens=False`：

[tokenization_utils_base.py:L3132-L3141](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L3132-L3141) —— 把渲染后的字符串送进 `__call__`，关闭自动加特殊 token。

**⑤ `add_generation_prompt` 的语义。** 这个参数为 True 时，会在末尾追加「开始一条 assistant 消息」的标记（如 ChatML 的 `<|im_start|>assistant\n`），让模型知道接下来该它说话了。官方文档强调：不加的话模型可能去**续写用户的话**而不是回复。详见 [chat_templating.md:L129-L171](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/chat_templating.md#L129-L171)。注意文档也指出：并非所有模型都需要生成提示，像 Llama 这类模型在 assistant 回复前没有特殊 token，此时 `add_generation_prompt` 无效。

#### 4.2.4 代码实践

**实践目标**：为一个模型渲染一段多轮对话，设置 `add_generation_prompt=True`，并检查输出中模型特定的特殊 token 是否被正确插入。

**操作步骤**：

1. 加载一个 ChatML 风格的模型（如 `Qwen/Qwen2.5-0.5B-Instruct`），它对 `add_generation_prompt` 有明显反应。
2. 分别以 `add_generation_prompt=False` 和 `True` 渲染同一段对话。
3. 对比两段字符串的**末尾差异**。

```python
# 示例代码（需联网下载 checkpoint）
from transformers import AutoTokenizer

tok = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-0.5B-Instruct")
chat = [
    {"role": "user", "content": "Hi there!"},
    {"role": "assistant", "content": "Nice to meet you!"},
    {"role": "user", "content": "Can I ask a question?"},
]

without = tok.apply_chat_template(chat, tokenize=False, add_generation_prompt=False)
with_gp = tok.apply_chat_template(chat, tokenize=False, add_generation_prompt=True)

print("=== 不加 generation prompt（末尾）===")
print(repr(without[-60:]))
print("=== 加 generation prompt（末尾）===")
print(repr(with_gp[-60:]))
```

**需要观察的现象**：`with_gp` 的末尾会多出 `<|im_start|>assistant\n`，而 `without` 在最后一条 user 消息的 `<|im_end|>` 处结束。

**预期结果**：把 `with_gp` 的字符串末尾可直接喂给 `model.generate()`，模型会顺着 `assistant` 角色开始生成。最后再做一步「真分词」验证：

```python
ids = tok.apply_chat_template(chat, tokenize=True, add_generation_prompt=True, return_tensors="pt")
print(ids["input_ids"].shape)   # 形如 torch.Size([1, 序列长度])
print(tok.decode(ids["input_ids"][0]))   # 回看人类可读文本
```

**待本地验证**：实际末尾标记依 checkpoint 而定；若你换用纯 Llama 模型，会发现两次渲染结果相同（`add_generation_prompt` 无效）——这正是 4.2.3 末尾提到的特殊情况。

#### 4.2.5 小练习与答案

**练习 1**：`apply_chat_template` 在 `tokenize=True` 时，调用 `self(...)` 分词为什么要把 `add_special_tokens` 设成 `False`？

> **答案**：因为聊天模板在渲染阶段已经把模型需要的所有控制 token（BOS、`<|im_start|>` 等）写进了字符串。如果再让 `__call__` 自动加特殊 token，就会重复添加 BOS 之类的标记，破坏模板精心安排的格式。

**练习 2**：什么情况下 `add_generation_prompt=True` 完全没有效果？

> **答案**：当模型在 assistant 消息前没有任何特殊标记（例如 Llama 系）时，模板里没有可追加的「助手起始 token」，所以该参数无可见效果。

**练习 3**：`continue_final_message=True` 和 `add_generation_prompt=True` 能同时用吗？为什么？

> **答案**：不能。源码在 [tokenization_utils_base.py:L3109-L3115](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L3109-L3115) 显式校验并抛错：前者会去掉结尾的 EOS 让模型续写最后一条消息，后者会追加起始 token 让模型开新消息，二者语义直接冲突。

---

### 4.3 chat_template_utils：Jinja 渲染引擎

#### 4.3.1 概念说明

`apply_chat_template` 只是个「门面」，真正干活的渲染引擎在 `utils/chat_template_utils.py`。它要解决三件事：

1. **安全执行**：聊天模板来自远端 checkpoint，是不可信代码。必须在**沙箱**里执行，防止模板调用危险函数（如读写文件、执行系统命令）。
2. **编译缓存**：同一个模板会被反复渲染，不能每次都重新解析 Jinja。
3. **额外能力**：模板里要能用 `tojson`（且不要 HTML 转义）、`raise_exception`、`strftime_now`，以及一个特殊的 `{% generation %}` 块来标记「哪段是模型生成的」。

此外，这个文件还提供 `get_json_schema()`，用于把一个带类型注解和 docstring 的 Python 函数自动转成「工具调用」所需的 JSON Schema（见 4.4 节）。

#### 4.3.2 核心流程

`render_jinja_template()` 是渲染主函数，流程如下：

```
render_jinja_template(conversations, tools, documents, chat_template, ...)
   │
   ├─ ① 若 return_assistant_tokens_mask 且模板里没有 {% generation %} → 警告
   │
   ├─ ② tools 处理：dict 原样保留；函数(get_json_schema)→ 转 schema
   │
   ├─ ③ documents 校验：必须是 list[dict]
   │
   ├─ ④ 编译模板 _compile_jinja_template(chat_template)  （带 lru_cache）
   │
   ├─ ⑤ 对每个对话 conversation：
   │      ├─ continue_final_message 时：给最后一条消息打「续写标记」
   │      ├─ return_assistant_tokens_mask → _render_with_assistant_indices（追踪区间）
   │      │   否则 → compiled_template.render(...)（普通渲染）
   │      └─ continue_final_message 时：把渲染结果截断到「续写起点」
   │
   └─ 返回 (rendered 列表, generation_indices 列表)
```

编译出的沙箱环境是 `ImmutableSandboxedEnvironment`，并设置了 `trim_blocks=True, lstrip_blocks=True`（让模板里的换行更干净）。

#### 4.3.3 源码精读

**① 沙箱环境与自定义全局函数。** 这是渲染引擎的核心。`_cached_compile_jinja_template()` 构造沙箱并注入了三个自定义能力：覆盖 `tojson` 过滤器（去掉 HTML 转义）、注入 `raise_exception`、注入 `strftime_now`，并要求 jinja2 ≥ 3.1.0：

[chat_template_utils.py:L489-L495](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/chat_template_utils.py#L489-L495) —— 构造 `ImmutableSandboxedEnvironment` 并注册 `tojson`/`raise_exception`/`strftime_now`。

为什么覆盖 `tojson`？因为 Jinja 默认的 `tojson` 会做 HTML 转义，会把 `{"a": 1}` 渲染成带 `&quot;` 的串，破坏工具调用的 JSON 结构。覆盖逻辑见 [chat_template_utils.py:L481-L484](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/chat_template_utils.py#L481-L484)。

**② 编译缓存。** `_compile_jinja_template` 套了 `@lru_cache`，保证同一个模板字符串只解析一次 AST、只构造一次沙箱：

[chat_template_utils.py:L419-L425](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/chat_template_utils.py#L419-L425) —— `_compile_jinja_template` 经 `lru_cache` 委托给 `_cached_compile_jinja_template`。

**③ tools 转换：函数 → JSON Schema。** 这是工具调用的入口。`render_jinja_template` 接受的 `tools` 既可以是现成的 schema dict，也可以是 Python 函数（自动转换）：

[chat_template_utils.py:L517-L530](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/chat_template_utils.py#L517-L530) —— `tools` 是 dict 直接收；是函数则用 `get_json_schema` 转成 schema；否则报错。

**④ `{% generation %}` 与助手 token 追踪。** 当 `return_assistant_tokens_mask=True` 时，需要知道渲染结果里「哪些字符属于 assistant 生成」。引擎用一个自定义 Jinja 扩展 `AssistantTracker`，在模板遇到 `{% generation %}...{% endgeneration %}` 块时记录该块在最终字符串里的字符区间 `[start, end)`：

[chat_template_utils.py:L431-L471](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/chat_template_utils.py#L431-L471) —— `AssistantTracker` 扩展：解析 `{% generation %}` 标签，在渲染时记录生成区间的字符起止。

随后 `apply_chat_template` 用 `char_to_token` 把字符区间映射成 token 区间，得到 `assistant_masks`（生成的 token 标 1，其余标 0），逻辑见 [tokenization_utils_base.py:L3143-L3164](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L3143-L3164)。这个 mask 常用于「只在 assistant token 上算 loss」的训练（见 u9）。

**⑤ 续写（prefill）支持。** `continue_final_message` 通过给最后一条消息内容追加一个哨兵标记 `CONTINUE_FINAL_MESSAGE_TAG`，渲染后找到该哨兵的位置并截断，从而去掉结尾 EOS，让模型接着写：

[chat_template_utils.py:L544-L569](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/chat_template_utils.py#L544-L569) —— 给最后一条消息打续写哨兵；渲染后再用哨兵定位截断点（[chat_template_utils.py:L588-L605](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/chat_template_utils.py#L588-L605)）。

#### 4.3.4 代码实践

**实践目标**：通过单元测试理解「模板里有哪些变量可用」，并体会沙箱与缓存的存在。

**操作步骤**（源码阅读型实践，无需联网）：

1. 阅读 `_get_template_variables()`，理解它如何用 `jinja2.meta.find_undeclared_variables` 自动列出模板用到的变量名。

```python
# 示例代码：本地直接调用引擎工具函数，不加载任何模型
from transformers.utils.chat_template_utils import (
    _get_template_variables,
    _compile_jinja_template,
)

# 一个最小模板：用了 messages、add_generation_prompt、bos_token、eos_token
tmpl = (
    "{% for msg in messages %}"
    "{{ bos_token }}{{ msg['role'] }}: {{ msg['content'] }}{{ eos_token }}"
    "{% endfor %}"
    "{% if add_generation_prompt %}assistant:{% endif %}"
)

print("模板引用的变量：", _get_template_variables(tmpl))
# 预期：frozenset({'messages', 'add_generation_prompt', 'bos_token', 'eos_token'})

# 直接编译并渲染（绕过模型，验证沙箱可独立工作）
compiled = _compile_jinja_template(tmpl)
rendered = compiled.render(
    messages=[{"role": "user", "content": "Hi"}],
    add_generation_prompt=True,
    bos_token="<BOS>",
    eos_token="<EOS>",
)
print("渲染结果：", rendered)
# 预期：<BOS>user: Hi<EOS>assistant:
```

**需要观察的现象**：`_get_template_variables` 能精确报出模板用到的四个变量，这正是引擎自动区分「模板需要的变量」和「处理器其他参数」的依据（避免手动维护白名单）。

**预期结果**：两次调用 `_compile_jinja_template(tmpl)` 返回同一缓存对象（`lru_cache` 命中）；渲染结果与上面注释一致。

**待本地验证**：`_get_template_variables` 带 `@lru_cache`，其结果应是 `frozenset`。

#### 4.3.5 小练习与答案

**练习 1**：为什么渲染聊天模板要用 `ImmutableSandboxedEnvironment` 而不是普通 `jinja2.Environment`？

> **答案**：聊天模板来自远端 checkpoint，属于不可信代码。沙箱环境会禁止模板访问危险属性、调用系统函数等，防止恶意模板对运行环境造成破坏。`Immutable` 进一步禁止模板修改上下文对象。

**练习 2**：`{% generation %}` 块和 `assistant_masks` 是什么关系？

> **答案**：`{% generation %}` 是模板作者用来圈定「模型将要生成/已生成的文本段」的标记。渲染时 `AssistantTracker` 扩展记录这些段在结果字符串里的字符区间；随后 `apply_chat_template` 用 `char_to_token` 把字符区间换算成 token 区间，生成 `assistant_masks`（生成 token = 1，其余 = 0），用于训练时只在 assistant 部分计算 loss。

---

### 4.4 扩展能力：工具调用与多模态

#### 4.4.1 概念说明

聊天模板不止能渲染「文字对话」，它还支持两类扩展：

- **工具调用（tool use / function calling）**：告诉模型「你可以调用这些工具」，让它在回复里输出结构化的工具调用请求。模板里会有专门的 `tools` 变量和 `tool_use` 分支。
- **多模态**：消息的 `content` 不再只是字符串，而可以是「文本块 + 图像块」的列表，模板需要把图像占位符插到正确位置（图像本身的张量由 Processor 处理，见 [u4-l2](./u4-l2-multimodal-processor.md)）。

#### 4.4.2 核心流程

**工具调用**的关键是 `get_json_schema(func)`：把一个带类型注解 + Google 风格 docstring 的普通 Python 函数，自动转成模型需要的工具描述 JSON Schema：

```
用户传入 tools=[multiply_func]  （或现成 schema dict）
        │
        ▼
render_jinja_template: 是函数 → get_json_schema(func)
        │   解析 docstring（parse_google_format_docstring）
        │   解析类型注解（_convert_type_hints_to_json_schema）
        │   合并：name + description + parameters + 每个参数的描述
        ▼
{"type":"function","function":{"name":"multiply","parameters":{...}}}
        │
        ▼  作为 tools 变量传给模板渲染
```

**多模态**则体现在消息结构上：

```python
# 多模态消息：content 是「内容块」列表，含图像
message = {
    "role": "user",
    "content": [
        {"type": "text", "text": "这张图里是什么？"},
        {"type": "image", "url": "https://.../cat.jpg"},
    ],
}
```

模板会把图像块渲染成模型约定的占位符（如 `<image>`），实际像素由 Processor 在另一条路径处理。

#### 4.4.3 源码精读

**① `get_json_schema` 的契约。** 它要求函数必须有 docstring，且每个参数都要有描述（Google 格式）和类型注解，否则抛 `DocstringParsingException`/`TypeHintParsingException`：

[chat_template_utils.py:L246-L253](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/chat_template_utils.py#L246-L253) —— `get_json_schema` 的说明：基于 docstring + 类型注解生成工具 schema，供传给聊天模板。

**② 类型注解 → JSON Schema 的映射规则。** `_parse_type_hint` 把 Python 类型递归转成 JSON Schema 类型：`int→integer`、`str→string`、`list[X]→array`、`X | None→nullable`、`Literal[...]→enum` 等；视觉可用时 `Image→{"type":"image"}`、torch 可用时 `torch.Tensor→{"type":"audio"}`：

[chat_template_utils.py:L80-L95](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/chat_template_utils.py#L80-L95) —— 基础类型到 JSON Schema 的映射表，含 `Image`（图像）与 `torch.Tensor`（音频）的特殊处理。

**③ 工具调用的用法约定。** `get_json_schema` 文档里给出了完整用法：拿到 schema 后，传给 `apply_chat_template` 的 `tools=` 参数，并切换到 `tool_use` 模板：

[chat_template_utils.py:L294-L320](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/chat_template_utils.py#L294-L320) —— 官方用法示例：`get_json_schema(multiply)` 后传给 `apply_chat_template(messages, tools=[schema], chat_template="tool_use", add_generation_prompt=True)`。

#### 4.4.4 代码实践

**实践目标**：用 `get_json_schema` 把一个普通函数转成工具 schema，体会「函数签名/docstring → 结构化描述」的自动转换。

**操作步骤**：

1. 定义一个带类型注解和 Google docstring 的函数。
2. 调用 `get_json_schema`，打印结果。
3. （可选）把 schema 传给支持工具调用的模型的 `apply_chat_template`。

```python
# 示例代码：第一部分纯本地，无需模型
from transformers.utils import get_json_schema
import json


def multiply(x: float, y: float):
    """
    A function that multiplies two numbers.

    Args:
        x: The first number to multiply.
        y: The second number to multiply.
    """
    return x * y


schema = get_json_schema(multiply)
print(json.dumps(schema, indent=2))
```

**预期结果**：

```json
{
  "type": "function",
  "function": {
    "name": "multiply",
    "description": "A function that multiplies two numbers.",
    "parameters": {
      "type": "object",
      "properties": {
        "x": {"type": "number", "description": "The first number to multiply."},
        "y": {"type": "number", "description": "The second number to multiply."}
      },
      "required": ["x", "y"]
    }
  }
}
```

**进阶（待本地验证，需支持工具调用的 checkpoint）**：

```python
# 示例代码（需联网，且 checkpoint 支持 tool_use 模板）
from transformers import AutoTokenizer

tok = AutoTokenizer.from_pretrained("CohereForAI/c4ai-command-r-v01")  # 支持 tool_use
msgs = [{"role": "user", "content": "What is 179 x 4571?"}]
out = tok.apply_chat_template(
    msgs, tools=[schema], chat_template="tool_use",
    add_generation_prompt=True, return_dict=True, return_tensors="pt",
)
print(out["input_ids"].shape)
```

**需要观察的现象**：第一部分能看到 `get_json_schema` 严格校验——若删掉某个参数的 docstring 描述，会抛 `DocstringParsingException`。这正是 4.4.3 ① 所述契约。

#### 4.4.5 小练习与答案

**练习 1**：如果给 `multiply` 删掉 docstring，`get_json_schema` 会怎样？

> **答案**：会抛 `DocstringParsingException`，提示无法为没有 docstring 的函数生成 schema。`get_json_schema` 强制要求函数有 docstring、且每个参数在 docstring 里都有描述。

**练习 2**：多模态消息的 `content` 为什么用「内容块列表」而不是纯字符串？

> **答案**：因为一条消息里可能同时包含文本和图像（甚至多张图）。用结构化的内容块（`{"type":"text",...}` / `{"type":"image",...}`）才能精确表达「哪段是文字、哪段是图」。模板据此插入图像占位符，而真正的像素张量由 Processor 在另一条路径处理（见 [u4-l2](./u4-l2-multimodal-processor.md)）。

---

## 5. 综合实践

把本讲知识串起来，完成一个「对话渲染 + 续写预填 + 工具描述」的小任务：

1. 加载一个带聊天模板的 checkpoint（建议 ChatML 风格，如 `Qwen/Qwen2.5-0.5B-Instruct`）。
2. 构造一段多轮对话，分别用以下三种方式渲染并对比：
   - `tokenize=False`，看纯字符串；
   - `add_generation_prompt=True`，看末尾是否多了 assistant 起始标记；
   - `continue_final_message=True`（最后一条消息设为 assistant 且内容形如 `{"name": "`），看是否去掉了结尾 EOS（用于 JSON 预填）。
3. 把三段结果打印出来，在每一段里**圈出**它用到的控制 token，并判断这些 token 是否来自 `special_tokens_map`（提示：渲染时 `template_kwargs = {**special_tokens_map, **kwargs}`）。
4. 额外：定义一个带类型注解和 docstring 的函数，用 `get_json_schema` 生成工具 schema，观察其 `required` 字段是如何由「参数无默认值」决定的（参考 [chat_template_utils.py:L197-L198](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/chat_template_utils.py#L197-L198)）。

> 如果你没有可联网的环境，可以把第 1～3 步降级为「源码阅读型实践」：在 [tokenization_utils_base.py:L3117-L3173](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L3117-L3173) 处逐行走读，画出「字符串 → BatchEncoding」的数据流。

## 6. 本讲小结

- 聊天模型本质仍是「续写 token」的语言模型；聊天模板负责把消息列表渲染成模型训练时见过的、含控制 token 的专属字符串。
- `apply_chat_template()` 是唯一入口，流程为：`get_chat_template` 选模板 → `render_jinja_template` 渲染 → （`tokenize=True` 时）走 `__call__` 分词（且 `add_special_tokens=False`）。
- `add_generation_prompt=True` 在末尾追加「助手起始 token」；`continue_final_message=True` 去掉结尾 EOS 让模型续写，二者互斥。
- 渲染引擎在 `chat_template_utils.py`：用 `ImmutableSandboxedEnvironment` 安全执行、`lru_cache` 缓存编译、`{% generation %}` 块配合 `AssistantTracker` 追踪助手 token，用于生成 `assistant_masks`。
- 模板可扩展到工具调用：`get_json_schema()` 把带类型注解 + docstring 的 Python 函数自动转成工具 JSON Schema，经 `tools=` 传入、`tool_use` 模板渲染。
- 模板支持多模态：消息 `content` 用「内容块列表」表达文本与图像的混合，模板负责插入图像占位符，像素张量由 Processor 另行处理。

## 7. 下一步学习建议

- 想了解聊天模板如何与「多模态图像预处理」协作，继续学 [u4-l2 多模态处理器 ProcessorMixin](./u4-l2-multimodal-processor.md)。
- `assistant_masks` 主要用于训练时只在助手 token 上算 loss，相关损失计算见 [u9-l6 损失函数与标签处理](./u9-l6-loss-functions.md)。
- 渲染出的 `input_ids` 最终会喂给 `model.generate()`，生成主循环的完整机制见 [u8-l2 GenerationMixin 主循环 generate()](./u8-l2-generate-loop.md)。
- 官方更深入的「工具调用 / 结构化输出」指引在 `docs/source/en/chat_extras.md`，可作为进阶阅读。
