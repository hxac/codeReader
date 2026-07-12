# 对话模板协议：Conversation 如何把聊天拼成模型输入

## 1. 本讲目标

本讲聚焦 MLC LLM 中的一个「翻译器」：**对话模板（Conversation Template）**。

当你用 OpenAI 风格的 API 发来一段 `messages`（系统提示 + 多轮对话）时，模型并不能直接「看懂」这些结构化消息。它只认得一段**连续的文本**（或者说一串 token id）。把「结构化的多轮消息」翻译成「模型训练时见过的那种特殊格式文本」，正是对话模板要做的事。

学完本讲，你应当能够：

1. 说出 `Conversation` 这个数据结构里每个关键字段（`system_template`、`roles`、`seps`、`role_content_sep`、`stop_str`/`stop_token_ids`、`role_templates`）的作用。
2. 读懂 `as_prompt()` 方法，能手工推演一段多轮对话最终会被拼成什么样的字符串。
3. 理解多模态（图片）如何作为非字符串元素混入提示列表，以及 stop 条件如何控制生成何时终止。

本讲是后续 **u6-l2 对话模板注册表**、**u6-l3 OpenAI 兼容协议** 的地基——先把「单个模板长什么样、怎么拼装」讲透，下一讲再讲「一堆模板如何被注册和查找」。

## 2. 前置知识

阅读本讲前，建议你已经了解（来自前置讲义）：

- **MLC LLM 的端到端工作流**（u1-l4）：`convert_weight → gen_config → compile → serve`，其中 `gen_config` 会把对话模板写进 `mlc-chat-config.json`。
- **ChatCompletion 请求的大致样子**：一条请求包含 `messages` 列表，每条消息有 `role`（user/assistant/system）和 `content`。
- **Pydantic BaseModel**：本讲的 `Conversation` 继承自 `pydantic.BaseModel`，它的字段校验（如 `field_validator`）由 Pydantic 驱动。你只需知道「Pydantic 会按字段类型自动校验输入、提供 `model_validate`/`model_dump` 等方法」即可。

一个关键直觉：不同模型在「训练时」被喂的提示格式差别极大。比如 Llama-2 用 `[INST] ... [/INST]`，Llama-3 用 `<|start_header_id|>...<|eot_id|>`，ChatML 用 `<|im_start|>...<|im_end|>`。**模型只有在推理时复现它训练时见过的格式，才能正常对话**——这就是对话模板存在的根本原因。

## 3. 本讲源码地图

本讲几乎全部围绕下面这一个文件展开：

| 文件 | 作用 |
| --- | --- |
| [python/mlc_llm/protocol/conversation_protocol.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/conversation_protocol.py) | 定义 `Conversation` 数据结构与 `as_prompt()` 拼装逻辑，是本讲主角。 |

为了让你看到这些字段「被填成什么样」，还会引用两个真实使用场景：

| 文件 | 作用 |
| --- | --- |
| [python/mlc_llm/conversation_template/registry.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/conversation_template/registry.py) | `ConvTemplateRegistry` 注册表与 `chatml` 内置模板实例。 |
| [python/mlc_llm/conversation_template/llama.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/conversation_template/llama.py) | Llama-2/3/4 的真实模板实例，方便对照字段含义。 |

以及两处「消费」侧引用：

| 文件 | 作用 |
| --- | --- |
| [python/mlc_llm/serve/engine_base.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py) | 调用 `as_prompt()` 并把结果送去分词（tokenize），展示提示如何进入引擎。 |
| [python/mlc_llm/serve/data.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/data.py) | 多模态用的 `ImageData` 类，说明 `as_prompt()` 返回的列表里可以混入非字符串元素。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 Conversation 字段结构**：这个「信封」里装了哪些字段，分别管什么。
- **4.2 as_prompt 拼装流程**：字段如何被组装成最终的提示。
- **4.3 stop 控制与多模态**：生成停止条件，以及图片如何进入提示。

### 4.1 Conversation 字段结构

#### 4.1.1 概念说明

`Conversation` 是一个 Pydantic 模型，它同时承担两个职责：

1. **模板（template）**：描述「这个模型期望的提示长什么样」——角色用什么 token 包裹、消息之间用什么分隔、生成何时停止。这些是相对固定的「格式契约」。
2. **历史（history）**：承载当前这次请求的多轮 `messages`。这部分每次请求都变。

你可以把它理解成一张「填空题答题卡」：模板部分是题目结构（哪一行填 user、哪一行填 assistant），`messages` 是你往里填的具体内容。`as_prompt()` 就是「交卷」——把答题卡渲染成最终的连续文本。

#### 4.1.2 核心流程

模板拼装的总格式，源码注释里给出了一张「占位图」：

```
<<system>><<messages[0][0]>><<role_content_sep>><<messages[0][1]>><<seps[0]>>
          <<messages[1][0]>><<role_content_sep>><<messages[1][1]>><<seps[1]>>
          ...
<<roles[1]>><<role_empty_sep>>
```

翻译成中文：先放 `system`，然后逐条消息按 `角色 + role_content_sep + 内容 + 分隔符` 串起来，最后留一个空的 assistant 角色头（`roles[1]` 通常是 assistant），让模型从这里「接着写」。`<<...>>` 表示对应字段的值。

#### 4.1.3 源码精读

先看最顶层的两个定义。`MessagePlaceholders` 是一组「占位符」枚举，用 `{system_message}`、`{user_message}` 这种花括号串标记「内容将来填在这里」：

[python/mlc_llm/protocol/conversation_protocol.py:10-17](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/conversation_protocol.py#L10-L17) —— 定义了 `SYSTEM`/`USER`/`ASSISTANT`/`TOOL`/`FUNCTION` 五个占位符，本质就是字符串常量。

接下来是 `Conversation` 类本体。我们分组理解它的字段。

**（a）系统提示相关**：

[python/mlc_llm/protocol/conversation_protocol.py:38-50](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/conversation_protocol.py#L38-L50) 定义了模板名 `name`、系统提示的「外壳」`system_template`（默认含 `{system_message}` 占位符）、系统提示「内容」`system_message`、以及两个少见的开关：

- `system_prefix_token_ids`：一些模型（如 Llama）需要在整段提示被分词后，**最前面再硬塞几个特殊 token id**（例如 `<|begin_of_text|>` 对应的 id）。注意它不是字符串，而是「分词之后」才拼接的 token id 列表。
- `add_role_after_system_message`：专门为 `[INST] [/INST]` 这种风格的模板服务，控制「系统消息之后是否还要再补一个角色头」。

**（b）角色与模板**：

[python/mlc_llm/protocol/conversation_protocol.py:53-57](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/conversation_protocol.py#L53-L57) 中：

- `roles` 是 `{角色名: 角色头字符串}` 的映射，比如 `{"user": "<|im_start|>user", "assistant": "<|im_start|>assistant"}`。
- `role_templates` 是「带占位符的角色模板」，默认就是占位符本身（见下面的 `__init__`），允许某些模型给某个角色定制更复杂的外壳。

**（c）历史消息与分隔符**：

[python/mlc_llm/protocol/conversation_protocol.py:59-74](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/conversation_protocol.py#L59-L74) 中：

- `messages`：当前请求的多轮历史，每条是 `(role, content)` 二元组，`content` 可以是 `str`、`None` 或「字典列表」（多模态）。
- `seps`：消息之间的分隔符列表，**长度只能是 1 或 2**（有校验）。长度 1 时所有消息用同一个分隔符；长度 2 时 `seps[0]` 用在 user 消息后、`seps[1]` 用在 assistant 消息后。
- `role_content_sep`：角色头和内容之间的分隔符（如换行 `\n`）。
- `role_empty_sep`：当内容为空（`None`）时，角色头后面跟的分隔符——通常用来「给模型留个位子接着写」。

**（d）停止条件与函数调用**：

[python/mlc_llm/protocol/conversation_protocol.py:76-90](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/conversation_protocol.py#L76-L90) 中：

- `stop_str`：遇到这些**字符串**就停止生成（如 `<|im_end|>`）。
- `stop_token_ids`：遇到这些 **token id** 就停止生成。
- `strip_reasoning_in_history`：Qwen3 等推理模型专属，渲染历史时剥掉 `<think>...</think>` 块。
- `function_string` / `use_function_calling`：函数调用（function calling）相关，本讲末尾简要提及，详细留待 u6-l3。

再看两个细节。`__init__` 给 `role_templates` 设了一组「默认值」（让每个角色默认就等于它自己的占位符），模型可以再覆盖：

[python/mlc_llm/protocol/conversation_protocol.py:92-101](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/conversation_protocol.py#L92-L101) —— 默认 user/assistant/tool 模板就是各自的占位符，传入的 `role_templates` 会增量覆盖。

最后是一个 Pydantic 字段校验器，强制 `seps` 长度为 1 或 2：

[python/mlc_llm/protocol/conversation_protocol.py:103-109](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/conversation_protocol.py#L103-L109) —— `check_message_seps` 在构造对象时就拦截非法长度，把错误前置到模板定义阶段。

#### 4.1.4 代码实践

我们用一条命令直接构造一个 `Conversation`，把字段「填满」后打印出来，建立直观感受。

1. **实践目标**：亲手构造一个最小的 `Conversation`，看清每个字段长什么样。
2. **操作步骤**：在仓库根目录运行（已安装 `mlc_llm` 即可）：

```bash
python -c "
from mlc_llm.protocol.conversation_protocol import Conversation, MessagePlaceholders
conv = Conversation(
    name='demo',
    system_template=f'[SYS]{MessagePlaceholders.SYSTEM.value}[/SYS]',
    system_message='你是一个助手。',
    roles={'user': '<U>', 'assistant': '<A>'},
    seps=['||'],
    role_content_sep=':',
    role_empty_sep=':',
)
print(conv.model_dump_json(indent=2))
"
```

3. **需要观察的现象**：打印出的 JSON 中，`role_templates` 即便你没传也会自动出现（因为 `__init__` 填了默认值）；`seps` 是单元素列表。
4. **预期结果**：你能看到 `roles`、`seps`、`role_content_sep`、`role_empty_sep`、自动补全的 `role_templates` 等字段，且 `messages` 为空列表。
5. 若运行报错找不到模块，说明 `mlc_llm` 未安装，请先按 u1-l3 完成安装；本步骤也可改在 Python 交互式 shell 中逐步执行。

#### 4.1.5 小练习与答案

**练习 1**：`system_template` 和 `system_message` 有什么区别？为什么拆成两个字段？

**参考答案**：`system_template` 是系统提示的「外壳结构」（含 `{system_message}` 占位符和一些固定 token，如 `<|start_header_id|>system<|end_header_id|>`），`system_message` 是「实际填进去的系统提示内容」。拆开是为了让外壳与内容解耦——同一套 Llama-3 模板结构，可以填不同的系统提示。

**练习 2**：`seps` 长度为 2 时，`seps[0]` 和 `seps[1]` 分别用在哪？举一个真实模型的例子。

**参考答案**：`seps[0]` 用在 user 消息后，`seps[1]` 用在 assistant 消息后。典型例子是 Llama-2：`seps=[" ", " </s>"]`，user 后面只跟一个空格，assistant 回答结束后跟 `</s>`（句子结束符）。

### 4.2 as_prompt 拼装流程

#### 4.2.1 概念说明

字段再多，最终都要变成一段「模型能吃的文本」。`as_prompt()` 就是这个「渲染器」。它的输入是模板自身（含 `messages` 历史），输出是一个**列表**——注意，不是单个字符串，而是 `List[Union[str, ImageData]]`，因为多模态时图片不能用字符串表示。

理解 `as_prompt` 的关键，是抓住它的「主循环」：遍历 `messages`，对每条 `(role, content)` 拼出 `角色头 + 分隔符 + 内容 + 消息分隔符`，最后把相邻的字符串合并、处理函数调用占位符。

#### 4.2.2 核心流程

`as_prompt` 的伪代码可以概括为：

```
function as_prompt(config):
    # 1. 渲染系统提示（把占位符替换成 system_message）
    system_msg = system_template.replace("{system_message}", system_message)

    message_list = []
    if system_msg != "":
        message_list.append(system_msg)

    # 2. seps 归一化为长度 2（方便按下标取）
    separators = (seps[0], seps[0]) if len(seps)==1 else (seps[0], seps[1])

    # 3. （可选）剥掉历史里的 <think> 块
    messages = strip(messages) if strip_reasoning_in_history else messages

    # 4. 主循环：逐条消息拼装
    for i, (role, content) in enumerate(messages):
        sep = separators[role == "assistant"]   # assistant 用 seps[1]
        if content is None:
            append( roles[role] + role_empty_sep )   # 留空位让模型接写
            continue
        role_prefix = roles[role] + role_content_sep   # （略去 add_role_after_system_message 的特判）
        if content 是字符串:
            append( role_prefix + role_template[role].replace(占位符, content) + sep )
        else:  # 多模态字典列表
            append(role_prefix)
            for item in content:
                if item.type == "text":       append( 文本 )
                elif item.type == "image_url": append( ImageData + "\n" )
            append(sep)

    # 5. 合并相邻字符串 + 处理 {function_string} 占位符
    return combine_consecutive(message_list)
```

两个要点：

- 第 4 步里 `separator = separators[role == "assistant"]`，用布尔值当索引——`True=1` 取 `seps[1]`，`False=0` 取 `seps[0]`，巧妙实现「user/assistant 用不同分隔符」。
- 当 `content is None` 时只追加「角色头 + 空分隔符」，这正是为了让模型「接在 assistant 头后面开始生成回答」。

#### 4.2.3 源码精读

方法签名与开头，先把系统提示渲染出来、把 `seps` 归一化成长度 2：

[python/mlc_llm/protocol/conversation_protocol.py:120-144](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/conversation_protocol.py#L120-L144) —— `as_prompt` 先取 `system_msg`，若 `seps` 只有 1 个元素就复制一份成 2 个，然后把非空的 `system_msg` 放进 `message_list` 开头。

接着是「剥推理块」的分支（4.3 节细讲）与主循环入口：

[python/mlc_llm/protocol/conversation_protocol.py:146-159](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/conversation_protocol.py#L146-L159) —— 注意第 155 行用 `separators[role == "assistant"]` 选分隔符；第 157-159 行处理 `content is None`：只放 `roles[role] + role_empty_sep`，给模型留「接写位」。

`role_prefix` 的计算有一处特判 `add_role_after_system_message`，专给 `[INST]` 类模板用：

[python/mlc_llm/protocol/conversation_protocol.py:161-176](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/conversation_protocol.py#L161-L176) —— 若「系统消息后不补角色头」且当前是第一条消息且系统消息非空，则 `role_prefix` 置空；否则正常拼 `角色头 + role_content_sep`。字符串内容走第 168-176 行：把 `role_templates[role]` 里的占位符（如 `{user_message}`）替换成实际 content，再追加 separator。

主循环之后是「合并相邻字符串」与「函数调用占位符替换」：

[python/mlc_llm/protocol/conversation_protocol.py:198-208](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/conversation_protocol.py#L198-L208) —— `_combine_consecutive_messages` 把相邻的字符串拼成一个；如果没有图片，就把 `{function_string}` 占位符替换成真实的 `function_string`（函数定义串），其余未替换的占位符清空。

为了让你对「真实模板」有体感，对照看 Llama-2 的实例（注意它的 `add_role_after_system_message=False` 和双元素 `seps`）：

[python/mlc_llm/conversation_template/llama.py:86-100](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/conversation_template/llama.py#L86-L100) —— Llama-2 用 `[INST] <<SYS>>\n...\n<</SYS>>\n\n` 作系统外壳，`roles` 里 user 带了 `<s>[INST]`、assistant 是 `[/INST]`，`seps=[" ", " </s>"]`。

而 ChatML 的实例则展示了「单元素 seps + 显式 `role_content_sep`」的写法：

[python/mlc_llm/conversation_template/registry.py:38-53](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/conversation_template/registry.py#L38-L53) —— `system_template` 把 `{system_message}` 夹在 `<|im_start|>system\n` 和 `<|im_end|>\n` 之间，`stop_str=["<|im_end|>"]` 与 `stop_token_ids=[2]` 双重保险。

最后看「消费侧」：引擎拿到 `as_prompt()` 的返回后，立刻送去分词：

[python/mlc_llm/serve/engine_base.py:762-769](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L762-L769) —— `process_prompts(conv_template.as_prompt(model_config), f_tokenize)` 把提示列表交给分词器；随后若 `system_prefix_token_ids` 非空，就在第一个 prompt 前面**拼接**那段 token id（注意这是分词之后才做的）。

#### 4.2.4 代码实践

1. **实践目标**：手工往一个 `Conversation` 里塞入 system + 两轮对话，调用 `as_prompt()` 打印最终提示串，并与你「脑补」的拼装结果对照。
2. **操作步骤**：

```bash
python -c "
from mlc_llm.protocol.conversation_protocol import Conversation
conv = Conversation(
    name='demo',
    system_template='[SYS]{system_message}[/SYS]\n',
    system_message='你是一个助手。',
    roles={'user': '<U>', 'assistant': '<A>'},
    seps=['||'],
    role_content_sep=':',
    role_empty_sep=':',
)
conv.messages = [
    ('user', '你好'),
    ('assistant', '有什么可以帮你？'),
    ('user', '解释 as_prompt'),
    ('assistant', None),   # None: 给模型留接写位
]
prompt = conv.as_prompt()
print(repr(prompt[0]))
"
```

3. **需要观察的现象**：
   - 输出应依次出现：`[SYS]你是一个助手。[/SYS]` → `<U>:你好||` → `<A>:有什么可以帮你？||` → `<U>:解释 as_prompt||` → `<A>:>`（最后这个 `:` 来自 `role_empty_sep`，注意它后面没有内容，正是留给模型生成的位置）。
   - `as_prompt()` 返回的是**列表**，但因为只有字符串、没有图片，`_combine_consecutive_messages` 把它们合并成了 `prompt[0]` 一个字符串。
4. **预期结果**：打印出的字符串符合上面描述的拼接顺序；assistant 的最后一条 `None` 渲染成 `<A>:>`（角色头 + 空分隔符），后面什么都没有。
5. 「待本地验证」：若你环境里 `mlc_llm` 未安装，可把这段逻辑改成纯 Python（把 `Conversation` 换成手写的同名 dataclass）来验证拼装规则，核心循环逻辑是一致的。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `as_prompt()` 返回的是**列表**而不是单个字符串？

**参考答案**：因为多模态请求里会混入 `ImageData`（图片张量），它不是字符串，无法和文本拼在一起。返回列表后，由下游的 `process_prompts` 分别处理：字符串段送去分词、`ImageData` 段送去做图像嵌入。即便没有图片，`_combine_consecutive_messages` 也会把所有字符串合并成一个，保证文本场景下拿到 `prompt[0]` 即可。

**练习 2**：`as_prompt` 里 `separator = separators[role == "assistant"]` 这行，`role == "assistant"` 的结果是布尔值，它是怎么被当下标用的？

**参考答案**：Python 中 `True == 1`、`False == 0`，所以布尔值可以直接做列表下标。`role` 是 assistant 时取 `separators[1]`（即 `seps[1]`），否则取 `separators[0]`（即 `seps[0]`）。这正好对应「user 后用 seps[0]、assistant 后用 seps[1]」的语义。

### 4.3 stop 控制与多模态

#### 4.3.1 概念说明

模板除了「拼输入」，还承担两件与生成强相关的事：

1. **停止条件（stop）**：模型生成时何时「闭嘴」。两种停止方式：遇到特定**字符串**（`stop_str`）或特定 **token id**（`stop_token_ids`）。两者通常同时设置，互为兜底。
2. **多模态（图片）**：当 `content` 是「字典列表」而非字符串时，`as_prompt` 会把其中的图片项渲染成 `ImageData` 对象，混入返回列表。

此外还有一个推理模型相关的「历史清洗」开关 `strip_reasoning_in_history`，用于 Qwen3 这类带 `<think>...</think>` 推理块的模型。

#### 4.3.2 核心流程

**停止条件的流转**：

```
Conversation.stop_str        ┐
                             ├─► 在引擎构造 generation_config 时作为 extra_stop_* 注入
Conversation.stop_token_ids  ┘
```

即 `stop_str` / `stop_token_ids` 并不在 `as_prompt` 内消费，而是被「调用方」（engine_base）读出来，作为额外停止条件塞进采样配置。这体现了「模板是数据、消费在引擎」的分工。

**多模态拼装**（`as_prompt` 主循环内的 content 不是字符串分支）：

```
for item in content:           # content 是字典列表
    if item["type"] == "text":
        append( 文本 )          # 与纯文本同样的占位符替换
    elif item["type"] == "image_url":
        append( ImageData.from_url(url, config) )   # 图片对象
        append( "\n" )          # 图片后补一个换行
```

注意 `image_url` 分支需要 `config`（模型配置，用来确定图像嵌入尺寸等），所以 `as_prompt` 才有一个 `config=None` 参数。

#### 4.3.3 源码精读

先看多模态分支，这是 `as_prompt` 里 content 不是字符串时的处理：

[python/mlc_llm/protocol/conversation_protocol.py:178-196](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/conversation_protocol.py#L178-L196) —— 遍历 content 列表：`type == "text"` 走占位符替换；`type == "image_url"` 调 `_get_url_from_item` 取出 url，再用 `data.ImageData.from_url(image_url, config)` 生成图片对象并追加，图片后补一个 `\n`。`assert config is not None` 说明多模态必须传 config。

`_get_url_from_item` 兼容两种 image_url 写法（OpenAI 风格的字符串或 `{url: ...}` 字典）：

[python/mlc_llm/protocol/conversation_protocol.py:211-226](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/conversation_protocol.py#L211-L226) —— 既支持 `{"image_url": "https://..."}`，也支持 `{"image_url": {"url": "https://..."}}`。

`ImageData` 本身（在 `serve/data.py` 中）封装了一张图片张量及它的嵌入尺寸，`from_url` 负责下载/解码图片：

[python/mlc_llm/serve/data.py:64-83](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/data.py#L64-L83) —— `ImageData` 持有 `image`（TVM Tensor）和 `embed_size`，`__len__` 返回 `embed_size`，`from_url` 是静态方法负责把 url 变成图片张量。

再看停止条件的「消费侧」——引擎把模板里的 `stop_str` / `stop_token_ids` 当作「额外停止条件」注入采样配置：

[python/mlc_llm/serve/engine_base.py:773-777](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L773-L777) —— `get_generation_config` 把 `conv_template.stop_token_ids` 作为 `extra_stop_token_ids`、`conv_template.stop_str` 作为 `extra_stop_str` 传入。也就是说，模板定义的停止条件会**自动叠加**到每次请求的生成配置上，无需用户在请求里重复指定。

最后是「剥推理块」的辅助函数 `_strip_reasoning_in_history`：

[python/mlc_llm/protocol/conversation_protocol.py:229-252](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/conversation_protocol.py#L229-L252) —— 找到最后一条 user 消息的位置，把它**之前**的 assistant 消息里的 `<think>...</think>` 块剥掉（只保留 `</think>` 之后的内容），模仿 Qwen3 官方 chat template 的行为；最后一条 assistant（如有）保留推理，以便工具调用 prefill 场景保留上下文。

#### 4.3.4 代码实践

1. **实践目标**：体验「修改 `stop_str` 不影响已渲染的提示文本，但会影响生成行为」，并构造一个多模态 content 看它如何被解析。
2. **操作步骤**（继续基于 4.2.4 的 `conv`）：

```bash
python -c "
from mlc_llm.protocol.conversation_protocol import Conversation

conv = Conversation(
    name='demo',
    system_template='[SYS]{system_message}[/SYS]\n',
    system_message='你是一个助手。',
    roles={'user': '<U>', 'assistant': '<A>'},
    seps=['||'],
    role_content_sep=':',
    role_empty_sep=':',
    stop_str=['<END>'],          # 设置停止字符串
)
conv.messages = [('user', 'hi'), ('assistant', None)]
p1 = conv.as_prompt()[0]
print('提示文本:', repr(p1))
print('stop_str:', conv.stop_str)

# 再改成另一个 stop_str，观察提示文本是否变化
conv.stop_str = ['STOP_NOW']
p2 = conv.as_prompt()[0]
print('改 stop_str 后提示文本:', repr(p2))
print('提示文本是否相同:', p1 == p2)
"
```

3. **需要观察的现象**：`p1` 与 `p2` **完全相同**——证明 `stop_str` 不参与提示拼装，它只是「数据」，由引擎在生成时读取。改 `stop_str` 只会改变「模型生成到什么字符串时停下」，不会改变输入提示。
4. **预期结果**：两次 `as_prompt()` 返回的字符串一致；`stop_str` 字段本身变了。
5. **延伸（可选）**：试着把某条 message 的 content 改成多模态字典列表 `[{"type": "text", "text": "看这张图"}, {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]`，再调用 `conv.as_prompt(config)`（需传入一个有效 model_config），观察返回列表里会出现非字符串的 `ImageData` 元素。若不传 config 会触发 `assert config is not None` 报错——这正好验证了多模态必须有 config。「待本地验证」：图片真实下载依赖网络与 PIL，离线时可只验证 assert 报错行为。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `stop_str` 和 `stop_token_ids` 通常要同时设置？只用其中一个会有什么问题？

**参考答案**：字符串停止依赖「分词后再还原字符串」来匹配，可能在 token 边界处漏判（一个 stop_str 被拆到多个 token 里时，逐 token 拼接的字符串可能匹配不到）；token id 停止则精确但需要事先知道停止 token 的 id。同时设置可互为兜底：token id 保证精确命中，字符串保证语义层面的停止词也能生效。

**练习 2**：`strip_reasoning_in_history=True` 时，最后一条 assistant 消息的 `<think>` 块会不会被剥掉？为什么？

**参考答案**：不会。`_strip_reasoning_in_history` 只剥「最后一条 user 消息**之前**」的 assistant 推理块。最后一条 assistant（如果在 user 之后）的推理被保留，是为了工具调用（function calling）的 prefill 场景——模型需要看到完整的推理上下文来续写工具调用。

## 5. 综合实践

把三个模块串起来，做一个「mini 渲染器」验证任务：

**任务**：选一个真实的内置模板（如 `llama-2`），用 `ConvTemplateRegistry.get_conv_template("llama-2")` 取出它，往里塞入一段两轮对话（system + user + assistant + user），调用 `as_prompt()` 打印最终提示；然后**手工**对照 Llama-2 的官方格式（`[INST] <<SYS>>...<</SYS>> ... [/INST]`）核对你打印出的结果是否符合预期。

**建议步骤**：

1. 用 `from mlc_llm.conversation_template import ConvTemplateRegistry` 取出 `llama-2` 模板。
2. 打印它的 `roles`、`seps`、`role_content_sep`、`system_template`、`stop_str`，逐字段理解。
3. 设置 `conv.messages = [("user", "What is 2+2?"), ("assistant", "4"), ("user", "Thanks!"), ("assistant", None)]`，并把 `conv.system_message` 改成你想要的内容。
4. 调用 `conv.as_prompt()` 打印 `prompt[0]`。
5. 对照官方格式检查：是否出现了 `<s>[INST]`、`<<SYS>>`、`[/INST]`、`</s>` 等关键标记？最后是否留出了 assistant 接写的位置？
6. （进阶）把模板换成 `llama-3`，对比两者的 `system_template`、`stop_token_ids` 差异，体会在 u6-l2 将要讲的「模板演化」。

**预期现象**：`llama-2` 的输出应包含 `<s>[INST] <<SYS>>\n你设置的系统提示\n<</SYS>>\n\nWhat is 2+2? [/INST] 4 </s>` 这样的结构；最后一条 `("assistant", None)` 会渲染成 `[/INST]` 后留空，等待模型生成。

> 说明：`ConvTemplateRegistry` 与 `get_conv_template` 的注册/查询机制本身是 u6-l2 的主题，本综合实践只是把它当作「取模板的便捷入口」来用。

## 6. 本讲小结

- `Conversation` 是一个 Pydantic 模型，同时承载「模板格式契约」与「当前请求的历史消息」，字段可分为系统提示、角色与模板、消息与分隔符、停止条件、函数调用五大组。
- `as_prompt()` 是把结构化消息渲染成模型输入的核心方法：先渲染系统提示，再逐条消息拼 `角色头 + role_content_sep + 内容 + 分隔符`，`content is None` 时只留角色头给模型接写。
- 分隔符选择用 `separators[role == "assistant"]` 这一布尔下标技巧，实现「user/assistant 用不同分隔符」。
- `as_prompt()` 返回的是**列表**而非字符串，因为多模态（`image_url`）会向列表里混入 `ImageData` 对象，需要下游 `process_prompts` 分别处理。
- `stop_str` / `stop_token_ids` 不在 `as_prompt` 内消费，而是被引擎读出后作为「额外停止条件」注入采样配置——模板是数据，消费在引擎。
- `strip_reasoning_in_history` 模仿 Qwen3 的行为，剥掉历史中（最后一条 user 之前）的 `<think>...</think>` 推理块。

## 7. 下一步学习建议

- **u6-l2 对话模板注册表与实例**：本讲只用到了一两个现成模板，下一讲讲清楚 `ConvTemplateRegistry` 如何用「导入即注册」的模式管理几十个模板，并对比 Llama-2 与 Llama-3 模板的演化（`[INST]` 标签到 header token）。
- **u6-l3 OpenAI 兼容协议与生成配置**：本讲的 `Conversation` 是请求的「提示侧」，下一讲讲请求的「协议侧」——`ChatCompletionRequest`/`Response` 等 Pydantic 模型如何与 OpenAI API 对齐，以及 `stop_str` 如何并入 `generation_config` 的采样参数。
- **延伸阅读**：想看模型在引擎里如何被分词、送进 KV cache，可跳读 `python/mlc_llm/serve/engine_base.py` 中 `_process_request` 相关的请求处理链路（属 U9/U11 范畴）。
