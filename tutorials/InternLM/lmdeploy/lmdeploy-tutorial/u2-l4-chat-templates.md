# Chat 模板体系 model.py

## 1. 本讲目标

大模型本身只会「续写 token」，它并不天然懂得什么叫「用户说一句、我答一句」。要让一段多轮对话被模型正确理解，必须在送进模型之前，把对话按某种固定格式拼成一长串文本——这就是 **chat 模板（对话模板）** 干的事。

本讲学完后，你应该能够：

1. 说清 `ChatTemplateConfig`、`BaseChatTemplate`、`HFChatTemplate` 三者各自的职责与关系。
2. 看懂 lmdeploy 用「占位符拼接」与「委托 HuggingFace `apply_chat_template`」两种风格实现模板的差异。
3. 知道 `pipeline(...)` 里传入的 `chat_template_config` 是如何被引擎接收并最终拼出 prompt 的。
4. 为一个新模型选择内置模板、用 JSON 自定义模板，或注册一个 Python 模板类。

## 2. 前置知识

在进入源码前，先建立两个直觉。

**直觉一：模型只认 token，不认「角色」。**
你给模型的输入是一串 token id。所谓「user / assistant / system」这些角色概念，全靠在文本里插入特殊标记（special tokens）来表达。例如 ChatML 风格用 `<|im_start|>user\n` 表示「接下来是用户的话」，用 `<|im_end|>` 表示「用户的话结束」。不同模型族（Vicuna、Llama2、Qwen、ChatGLM……）的标记完全不同，所以需要一个「模板」来描述这种格式。

**直觉二：模板的核心动作就是字符串拼接。**
最朴素的模板可以抽象成下面这个公式（和官方文档一致）：

```
{system}{meta_instruction}{eosys}{user}{用户内容}{eoh}{assistant}{助手内容}{eoa}{separator}{user}...
```

其中每个花括号字段都是一个可配置的「占位符」。lmdeploy 的 `BaseChatTemplate` 就是这个公式的直接实现；而 `HFChatTemplate` 则把拼接工作交给 HuggingFace tokenizer 自带的 Jinja 模板（`apply_chat_template`）。

> 术语提示：
> - `meta_instruction`：系统提示词（system prompt）的**正文**，例如「你是一个有用的助手」。
> - `system` / `eosys`：系统段的**首尾标记**（end-of-system）。
> - `user` / `eoh`：用户段的首尾标记（end-of-head / end-of-user）。
> - `assistant` / `eoa`：助手段的首尾标记（end-of-assistant）。
> - `separator`：多轮对话中，上一轮与下一轮之间的分隔符。
> - `capability`：模型能力，取值 `completion` / `infilling` / `chat` / `python`，决定是否套用对话格式。

本讲承接 [u2-l1 核心消息与响应类型](u2-l1-core-message-types.md)：那里讲的 `GenerationConfig` 管「怎么采样」，本讲的 `ChatTemplateConfig` 管「怎么把对话拼成文本」。两者都是用户面配置，但作用阶段不同。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `lmdeploy/model.py` | **本讲主战场**。chat 模板的注册表 `MODELS`、配置类 `ChatTemplateConfig`、基类 `BaseChatTemplate`、数十个内置模板（Vicuna/Llama2/...）、以及 `HFChatTemplate` 全部在此。 |
| `lmdeploy/__init__.py` | 把 `ChatTemplateConfig` 作为公开 API 导出。 |
| `lmdeploy/pipeline.py` | `Pipeline.__init__` 接收 `chat_template_config` 参数并向下传递。 |
| `lmdeploy/serve/core/async_engine.py` | 引擎构造时调用 `get_chat_template(...)` 得到真正的模板对象，挂到 `self.chat_template`。 |
| `lmdeploy/cli/utils.py` | CLI 的 `--chat-template` 参数解析：支持内置名或 JSON 文件。 |

> 关于规格里列出的 `lmdeploy/messages.py`：经核对，chat 模板的所有类型与逻辑都集中在 `lmdeploy/model.py`，`messages.py` 并不包含模板相关代码。`ChatTemplateConfig` 是从 `model.py` 经 `__init__.py` 导出的，不是定义在 `messages.py`。本讲因此以 `model.py` 为主。

## 4. 核心概念与源码讲解

### 4.1 ChatTemplateConfig：模板的「参数包」

#### 4.1.1 概念说明

`ChatTemplateConfig` 是一个普通 `@dataclass`，它本身**不做拼接**，只负责「装参数」：你想用哪个内置模板（`model_name`）、想覆盖哪些占位符、系统提示词写什么、停止词有哪些。可以把它理解成一张「模板配置表」。

它和上一讲的 `GenerationConfig`、`PytorchEngineConfig` 是同一种设计风格：用 dataclass 的字段默认值表达参数，构造时即可校验。但它的特殊之处在于提供一个 `chat_template()` 方法，把这张「配置表」实例化成真正能干活的模板对象。

#### 4.1.2 核心流程

`ChatTemplateConfig` 的生命周期可以概括为三步：

```text
1. 构造配置：ChatTemplateConfig(model_name='vicuna', meta_instruction='...')
       │
       ▼
2. 实例化模板：cfg.chat_template()  →  返回一个 Vicuna 实例（BaseChatTemplate 子类）
       │  内部逻辑：
       │   - 收集所有非 None 字段 → attrs
       │   - 从 attrs 移除 model_name
       │   - 若 model_name 在注册表 MODELS 中 → 用对应类实例化
       │   - 否则 → 警告并回退到 BaseChatTemplate
       ▼
3. 使用模板：tpl.messages2prompt(messages) → 拼好的字符串 prompt
```

此外它还提供 `to_json` / `from_json` 用于把配置序列化成 JSON 文件或从 JSON 反序列化——这正是「用 JSON 自定义模板」的基础。

#### 4.1.3 源码精读

注册表本身借助 mmengine 的 `Registry`，所有模板类都注册到它里面：

[lmdeploy/model.py:L13](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L13) —— 创建名为 `model` 的注册表，`locations=['lmdeploy.model']` 表示在 `lmdeploy.model` 模块内收集被装饰的类。

[lmdeploy/model.py:L34-L69](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L34-L69) —— `ChatTemplateConfig` 数据类定义。`model_name` 是唯一必填字段；其后是一长串可选占位符，默认全是 `None`（表示「不覆盖、用模板类自带的默认值」）。

核心方法 `chat_template()`：

[lmdeploy/model.py:L71-L80](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L71-L80) —— 这里是「配置 → 实例」的转换点。它用字典推导筛出所有非 `None` 字段，删掉 `model_name`，再判断注册表里有没有这个名字：有就用对应类（如 `Vicuna`）实例化，没有就打一条警告并退回 `BaseChatTemplate`。注意 `model_path` 如果被设置，也会随 `attrs` 一起传给模板类——这对 `HFChatTemplate` 是必需的，因为它要据此加载 tokenizer。

`from_json` 支持从 JSON 文件或 JSON 字符串构造，且对未注册的 `model_name` 会临时把它注册成一个 `BaseChatTemplate`：

[lmdeploy/model.py:L91-L109](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L91-L109) —— 若 JSON 里没给 `model_name` 就生成一个随机 uuid；若名字不在注册表里，就用 `MODELS.register_module(...)` 临时注册一个 `BaseChatTemplate`，随后照常构造。这正是「JSON 文件即模板」的实现原理。

#### 4.1.4 代码实践

**实践目标**：不加载任何模型、不需要 GPU，纯 CPU 验证 `ChatTemplateConfig` → 模板对象 → prompt 字符串 的转换链。

**操作步骤**：

```python
# 示例代码：纯字符串操作，无需 GPU
from lmdeploy.model import ChatTemplateConfig, MODELS

# 1) 看看内置模板都有哪些名字
print('已注册模板：', list(MODELS.module_dict)[:10], '...')

# 2) 用内置 vicuna 模板
cfg = ChatTemplateConfig(model_name='vicuna')
tpl = cfg.chat_template()           # 得到 Vicuna 实例
print('模板类：', type(tpl).__name__)

# 3) 把一段对话拼成 prompt
messages = [{'role': 'user', 'content': '你好，你是谁？'}]
print('--- vicuna prompt ---')
print(repr(tpl.messages2prompt(messages)))

# 4) 覆盖系统提示词，观察变化
cfg2 = ChatTemplateConfig(model_name='vicuna',
                          meta_instruction='你是一个由 lmdeploy 教程驱动的机器人。')
print('--- 覆盖 meta_instruction 后 ---')
print(repr(cfg2.chat_template().messages2prompt(messages)))
```

**需要观察的现象**：

- 第 3 步会看到 `USER: 你好，你是谁？ ASSISTANT` 这样的拼接结果（Vicuna 风格）。
- 第 4 步开头会变成 `A chat between ... 你是一个由 lmdeploy 教程驱动的机器人。 ...`，证明 `meta_instruction` 被成功覆盖。

**预期结果**：能正确打印模板类名 `Vicuna` 与两段不同的拼接 prompt。本实践为纯字符串操作，结果可预期；如本地环境 `import lmdeploy` 报错，请先按 [u1-l3](u1-l3-installation-and-build.md) 完成安装。

#### 4.1.5 小练习与答案

**练习 1**：`ChatTemplateConfig` 的 `chat_template()` 方法为什么要把 `model_name` 从 `attrs` 里 `pop` 掉？

**参考答案**：因为 `model_name` 只是用来在注册表里查找对应模板类的「钥匙」，并不是模板类 `__init__` 的参数；各个模板类的构造函数接收的是 `system`/`user`/`assistant` 等占位符，并不接收 `model_name`。若不剔除会触发意外的 `TypeError`。

**练习 2**：若传入一个注册表里不存在的 `model_name='my-fancy-model'`，会发生什么？

**参考答案**：`chat_template()` 会打印一条 `Could not find ... in registered models` 的警告，但仍返回一个用所给占位符构造的 `BaseChatTemplate` 实例（参见 [model.py:L77-L79](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L77-L79)），不会抛异常。

---

### 4.2 BaseChatTemplate：占位符拼接的基类

#### 4.2.1 概念说明

`BaseChatTemplate` 是所有「占位符拼接」风格模板的基类，也是默认实现。它把第 2 节那个拼接公式直接写成代码：用一组占位符字符串（`system`/`user`/`eoh`/`assistant`/...）把对话内容包起来。

绝大多数内置模板（Vicuna、Llama2、Mistral、ChatML 风格的 `llava-chatml` 等）都是它的子类，**只覆盖占位符的默认值**即可，几乎不用改逻辑——这体现了「数据驱动」的设计：格式不同 = 占位符不同，复用同一套拼接代码。

#### 4.2.2 核心流程

`BaseChatTemplate` 有两个对外方法，对应两种输入形态：

- `get_prompt(prompt, sequence_start)`：输入是**纯字符串**。
- `messages2prompt(messages, sequence_start)`：输入是 **OpenAI 风格的 messages 列表**（`[{'role':..., 'content':...}, ...]`）。

`messages2prompt` 的执行过程（伪代码）：

```text
若 messages 是字符串：转交 get_prompt 处理，返回。

构建两张映射：
  box_map = {user: self.user, assistant: self.assistant, system: self.system, tool: self.tool}   # 各角色「起始」标记
  eox_map  = {user: self.eoh, assistant: self.eoa+separator, system: self.eosys, tool: self.eotool}  # 各角色「结束」标记

若 sequence_start 且设置了 meta_instruction 且第一条消息不是 system：
  在最前面补上 {system}{meta_instruction}{eosys}

遍历每条 message：
  ret += box_map[role] + content + eox_map[role]

若最后一条是 assistant 且 assistant 的结束标记非空：
  返回去掉末尾结束标记的 ret        # 让模型「接着写」助手的回答
否则：
  ret += self.assistant            # 追加助手起始标记，引导模型开始生成
  返回 ret
```

其中 `sequence_start` 这个布尔值非常关键：它区分「会话的第一轮」（需要带系统提示）与「后续轮」（只加 `separator` 前缀），从而支持多轮对话续接。

#### 4.2.3 源码精读

[lmdeploy/model.py:L112-L141](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L112-L141) —— `BaseChatTemplate` 以 `name='base'` 注册；`__init__` 把所有占位符存为实例属性，并接收 `**kwargs`（容忍额外参数，方便子类透传）。注意默认值是空串 `''` 而非 `None`，这样可以直接做字符串拼接。

`get_prompt` 是单字符串版本的拼接，分 `completion` 能力（原样返回）与 `chat` 能力（套公式）两条分支：

[lmdeploy/model.py:L143-L167](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L143-L167) —— 这里能直接看到第 2 节那个公式的代码实现。注意它对 `meta_instruction is not None` 与 `== ''` 做了区分：注释明确写着 `# None is different from ''`——`None` 表示「不输出系统段」，空串则仍会输出首尾标记。

`messages2prompt` 处理 OpenAI 风格的列表输入：

[lmdeploy/model.py:L169-L193](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L169-L193) —— 用 `box_map` / `eox_map` 两张表把「角色 → 占位符」解耦；末尾对「最后一条是 assistant」的情况做了截断（`ret[:-len(eox_map['assistant'])]`），目的是当用户传入一段「半截助手回答」时，让模型从截断处续写，而不是重新开一段。

`match` 是自动匹配的钩子，基类返回 `None`（不匹配）：

[lmdeploy/model.py:L195-L202](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L195-L202) —— 子类可覆盖它，依据 `model_path`（模型路径或 repo id）的字符串特征返回自己的注册名，供 `get_chat_template` 自动识别。

来看两个只改默认值的子类，体会「数据驱动」：

[lmdeploy/model.py:L241-L285](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L241-L285) —— `Vicuna` 仅在 `__init__` 里把 `user='USER: '`、`assistant='ASSISTANT: '` 等占位符换成 Vicuna 的标记，并覆盖 `match` 让路径里含 `vicuna` 的模型自动选中它。它对 `get_prompt`/`messages2prompt` 做了 `[:-1]` 微调（去掉末尾一个空格），是少数需要改逻辑的情况。

[lmdeploy/model.py:L313-L347](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L313-L347) —— `Llama2` 同样只改占位符：`system='[INST] <<SYS>>\n'`、`eosys='\n<</SYS>>\n\n'`、`assistant=' [/INST] '`，体现了 Llama2 的 `[INST]...[/INST]` + `<<SYS>>` 格式。

#### 4.2.4 代码实践

**实践目标**：用多轮 messages 对比 `BaseChatTemplate`（默认空占位符）与 `Vicuna` 的输出，直观感受「占位符决定格式」。

**操作步骤**：

```python
# 示例代码：纯 CPU 字符串操作
from lmdeploy.model import ChatTemplateConfig

messages = [
    {'role': 'user', 'content': '1+1=?'},
    {'role': 'assistant', 'content': '等于 2。'},
    {'role': 'user', 'content': '那 2+2 呢？'},
]

base = ChatTemplateConfig(model_name='base').chat_template()
vicuna = ChatTemplateConfig(model_name='vicuna').chat_template()

print('=== base ===')
print(base.messages2prompt(messages))
print('=== vicuna ===')
print(vicuna.messages2prompt(messages))
```

**需要观察的现象**：

- `base` 输出几乎只有裸文本（占位符默认是空串），角色边界几乎看不出来。
- `vicuna` 输出会带 `USER: ` / `ASSISTANT: ` 标记，并能看出多轮被 `separator` 串起来。
- 最后一条是 `user`，所以末尾会自动追加 `ASSISTANT: `（引导模型作答）。

**预期结果**：两段输出风格迥异，证明格式差异完全来自占位符取值。结果可预期，纯字符串操作。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `BaseChatTemplate.__init__` 的占位符默认值用空串 `''` 而不是 `None`？

**参考答案**：因为 `get_prompt` / `messages2prompt` 里大量使用 f-string 直接拼接这些字段（如 `f'{self.user}{prompt}{self.eoh}'`）。若默认是 `None`，拼接会抛 `TypeError`；用空串则「不设置即不输出」，逻辑天然成立。而 `ChatTemplateConfig` 里默认是 `None`，语义是「不要覆盖子类的默认值」——两者语境不同。

**练习 2**：`messages2prompt` 末尾判断「最后一条是 assistant」时为什么要截断？

**参考答案**：当用户传入的对话以一段 assistant 内容结尾时，通常希望模型**续写**这段回答，而非重新开一段。截掉末尾的 assistant 结束标记（如 `</s>`），就让 prompt 停在「回答还没结束」的状态，模型自然会接着补全。

---

### 4.3 HFChatTemplate：委托给 HuggingFace apply_chat_template

#### 4.3.1 概念说明

随着 HuggingFace `transformers` 把 `chat_template`（一段 Jinja2 模板）写进 tokenizer 配置成为事实标准，越来越多新模型（Qwen、InternLM2、Gemma、Llama3 等）的对话格式由 tokenizer 自带的 `apply_chat_template` 方法来生成。

lmdeploy 不会为每个新模型都手写一个 `BaseChatTemplate` 子类，而是提供 `HFChatTemplate`：它**不自己拼接字符串**，而是加载模型的 tokenizer，直接调用 `tokenizer.apply_chat_template(messages, tokenize=False)` 得到 prompt。这是目前覆盖面最广、最推荐的方式。

源码里有一句重要注释——`It MUST be at the end of @MODELS registry`（见下方链接）：因为它在自动匹配时是「兜底」选项，必须排在所有具体模板之后。

#### 4.3.2 核心流程

`HFChatTemplate` 的关键在于：它需要在初始化时「探测」出该模型对话格式里的几个关键边界标记，以便后续做多轮续接与截断。

```text
__init__(model_path):
  1. AutoTokenizer.from_pretrained(model_path)  → self.tokenizer
  2. 若 tokenizer 没有 chat_template：尝试从 AutoProcessor 取（兼容多模态 tokenizer）
  3. 用「哨兵字符串」探测三类边界：
       _user_instruction()      → user_start, user_end       （用 content='sentinel' 探测）
       _assistant_instruction() → assistant_start, assistant_end
       _system_instruction()    → system 相关标记 / 哨兵系统消息
  4. 由 eos_token / eot_token 组装 stop_words
  5. 若架构是 GptOssForCausalLM，追加 '<|call|>' 为停止词

messages2prompt(messages, sequence_start):
  1. 字符串 → 包装成 [{'role':'user','content':...}]
  2. 校验每条消息都有 role 与 content
  3. 处理 enable_thinking / reasoning_effort 为 None 的特殊情况
  4. add_generation_prompt = (最后一条不是 assistant)
  5. sequence_start=True：直接 apply_chat_template
     sequence_start=False：先插「哨兵系统消息」再调用，最后裁掉哨兵前缀
  6. 若末尾是 assistant 且 assistant_end 非空：裁掉末尾标记（让模型续写）
  7. GptOss 特殊：去掉 'commentary, '
  返回 prompt
```

#### 4.3.3 源码精读

[lmdeploy/model.py:L617-L622](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L617-L622) —— `HFChatTemplate` 以 `name='hf'` 注册，类文档明确指出它必须位于注册表末尾，因为它充当兜底匹配。

[lmdeploy/model.py:L624-L654](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L624-L654) —— 构造函数加载 tokenizer；若 tokenizer 没有 `chat_template`，则尝试从 `AutoProcessor` 取（注释指出某些多模态模型的模板挂在 processor 上）。随后调用三个 `_xxx_instruction()` 探测边界，并组装 `stop_words`。这里还用 `get_model_arch` 判断是否为 `GptOssForCausalLM`，是则把 `<|call|>` 也加入停止词。

[lmdeploy/model.py:L660-L693](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L660-L693) —— 这是「HFChatTemplate 如何调用 apply_chat_template」的核心段落。关键调用在两处：

- 第 674-677 行（`sequence_start=True` 分支）：

  [lmdeploy/model.py:L674-L677](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L674-L677) —— 直接 `self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=add_generation_prompt, **kwargs)`。`tokenize=False` 表示返回字符串而非 token id；`add_generation_prompt=True` 让模板在末尾追加「助手发言开始」的标记，引导模型生成。

- 第 681-685 行（`sequence_start=False`，即交互式多轮续接分支）：

  [lmdeploy/model.py:L681-L687](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L681-L687) —— 这里有一个精巧的「哨兵（sentinel）」机制：在交互式续接时，直接调用 `apply_chat_template` 会被 tokenizer 模板里的默认 system 角色污染。为此先把「哨兵系统消息」插到消息列表最前面一起送进去，调用完再裁掉哨兵前缀（`prompt[len(self.sentinel_system_prompt):]`），从而得到干净的续接 prompt。

末尾 [L688-L693](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L688-L693) 处理「最后一条是 assistant」的截断（与基类同理），以及 GptOss 去掉 `commentary, ` 的特殊逻辑。

那么 `user_start` / `user_end` 这些标记是怎么「探测」出来的？看哨兵探测法：

[lmdeploy/model.py:L695-L704](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L695-L704) —— `_user_instruction` 构造一条 `content='sentinel'` 的用户消息，调用 `apply_chat_template`，再在结果字符串里定位 `sentinel` 的位置，它之前的就是 `user_start`、之后的就是 `user_end`。这是一种「黑盒探测」：不需要解析 Jinja，只要模板可执行就能反演出边界标记。`_assistant_instruction` / `_system_instruction`（[L706-L737](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L706-L737)）同理，且对不支持 system 角色的模型（如 gemma-2）做了容错。

最后是自动匹配的兜底逻辑：

[lmdeploy/model.py:L739-L745](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L739-L745) —— `HFChatTemplate.match` 尝试直接构造一个实例：构造成功返回 `True`（表示「我能处理这个模型」），抛异常返回 `False`。正因如此它必须放在注册表末尾——否则它会抢在 Vicuna/Llama2 等具体模板之前匹配成功。

#### 4.3.4 代码实践

**实践目标**：手动构造一个 `HFChatTemplate`，观察它对一组 messages 调用 `apply_chat_template` 后的输出，并与 `vicuna` 模板对比。

**操作步骤**：

```python
# 示例代码：需要 transformers 与网络（下载 tokenizer），不需要 GPU
from lmdeploy.model import ChatTemplateConfig

model_path = 'Qwen/Qwen2.5-7B-Instruct'   # 可换成本地已有模型目录

# 注意：chat_template() 只接收 trust_remote_code，model_path 必须写进 config
hf_cfg = ChatTemplateConfig(model_name='hf', model_path=model_path)
hf_tpl = hf_cfg.chat_template(trust_remote_code=True)

messages = [{'role': 'user', 'content': '你好，你是谁？'}]
print('=== HFChatTemplate 输出 ===')
print(hf_tpl.messages2prompt(messages))
print('=== stop_words ===')
print(hf_tpl.stop_words)
```

> 说明：日常使用中你**几乎不需要**手动写 `model_name='hf'`。`pipeline(...)` 内部会调用下文的 `get_chat_template` 自动判定——当没有具体模板匹配、但 tokenizer 有 `chat_template` 时，就会落到 `HFChatTemplate`。这里手动构造仅用于学习。

**需要观察的现象**：

- 输出里会出现 `<|im_start|>user` / `<|im_end|>` / `<|im_start|>assistant` 这类 ChatML 标记（Qwen 系列的典型格式）。
- 因为最后一条是 user，末尾会带 `add_generation_prompt=True` 追加的助手起始标记。
- `stop_words` 通常包含 `<|im_end|>` 或 `<|endoftext|>`。

**预期结果**：能打印出带 ChatML 标记的 prompt 与对应停止词。本实践需要联网下载 tokenizer，具体输出文本**待本地验证**（不同模型标记不同）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `HFChatTemplate.match()` 要放在注册表最后？

**参考答案**：因为它的 `match()` 只要能成功构造实例就返回 `True`——而任何带 `chat_template` 的 tokenizer 都能构造成功。如果它排在前面，就会「截胡」所有模型，导致 Vicuna/Llama2 等更精确的模板永远没机会被选中。放在最后，让具体模板优先匹配、它做兜底，才能兼顾「精确」与「广覆盖」。

**练习 2**：`sequence_start=False`（多轮续接）时，`HFChatTemplate` 为什么要插入「哨兵系统消息」再裁掉？

**参考答案**：HuggingFace 的 `chat_template` 很多带有默认 system 角色。在交互式续接（非首轮）时，若直接调用，模板会反复插入默认系统提示，污染上下文。插入一条「哨兵」系统消息让模板按既有逻辑渲染，渲染完再按哨兵字符串的长度把前缀裁掉，就能得到不含默认 system 的干净续接 prompt（参见 [model.py:L681-L687](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L681-L687)）。

---

### 4.4（补充）模板如何接入 pipeline：get_chat_template

虽然本讲的核心是三个模板类型，但读者一定会问：`ChatTemplateConfig` 传给 `pipeline(...)` 之后去了哪里？这里补一条把全图收口的调用链。

[lmdeploy/pipeline.py:L38](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L38) —— `Pipeline.__init__` 接收 `chat_template_config: ChatTemplateConfig | None`。

[lmdeploy/serve/core/async_engine.py:L123](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L123) —— 引擎构造时执行 `self.chat_template = get_chat_template(model_path, chat_template_config, trust_remote_code=...)`，得到真正的模板对象挂在引擎上。

[lmdeploy/model.py:L748-L767](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L748-L767) —— `get_chat_template` 是整条链的「路由器」：

- 若用户传了 `config`：直接 `config.chat_template(...)`（用户显式指定优先）。
- 否则遍历 `MODELS.module_dict`，依次调用每个模板类的 `match(model_path)`，命中第一个即用之。
- 一个都没匹配上就用 `'base'`。

> CLI 侧也走同一套：`lmdeploy serve api_server ... --chat-template vicuna`（或一个 JSON 文件路径）会被 [lmdeploy/cli/utils.py:L67-L88](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/utils.py#L67-L88) 解析成 `ChatTemplateConfig`。

> 提示：`ChatTemplateConfig` 的文档字符串里写了「All the chat template names: `lmdeploy list`」，但经核对当前 CLI（`lmdeploy/cli/entrypoint.py` 与 `lmdeploy/cli/cli.py`）只注册了 `check_env` / `chat` / `serve` / `lite` 子命令，并不存在 `list` 子命令，该描述已过时。要查看全部内置模板名，请用 Python：
>
> ```python
> from lmdeploy.model import MODELS
> print(list(MODELS.module_dict))
> ```

## 5. 综合实践

把三个模块串起来：自己**注册一个 Python 模板类**，并用它与 `HFChatTemplate` 处理同一组 messages，对比两种风格的输出差异。

**实践目标**：掌握「自定义模板类 + 注册 + 经 ChatTemplateConfig 使用」的完整闭环，并直观对比「占位符拼接」与「Jinja apply_chat_template」。

**操作步骤**：

```python
# 示例代码：注册部分纯 CPU；对比 HFChatTemplate 需 transformers + 网络
from lmdeploy.model import MODELS, BaseChatTemplate, ChatTemplateConfig


# 1) 注册一个自定义 ChatML 风格模板
@MODELS.register_module(name='my-chatml')
class MyChatML(BaseChatTemplate):
    """一个自定义 ChatML 风格模板。"""

    def __init__(self,
                 system='<|im_start|>system\n',
                 meta_instruction='你是一个由本教程创建的机器人。',
                 eosys='<|im_end|>\n',
                 user='<|im_start|>user\n',
                 eoh='<|im_end|>\n',
                 assistant='<|im_start|>assistant\n',
                 eoa='<|im_end|>',
                 separator='\n',
                 stop_words=None,
                 **kwargs):
        super().__init__(system=system, meta_instruction=meta_instruction, eosys=eosys,
                         user=user, eoh=eoh, assistant=assistant, eoa=eoa,
                         separator=separator, stop_words=stop_words or ['<|im_end|>'],
                         **kwargs)


messages = [
    {'role': 'user', 'content': '你是谁？'},
]

# 2) 用自定义模板拼 prompt
mine = ChatTemplateConfig(model_name='my-chatml').chat_template()
print('=== 自定义 ChatML 模板 ===')
print(mine.messages2prompt(messages))

# 3) 与 HFChatTemplate 对比（需可联网下载 tokenizer）
try:
    hf = ChatTemplateConfig(model_name='hf',
                            model_path='Qwen/Qwen2.5-7B-Instruct').chat_template(trust_remote_code=True)
    print('=== HFChatTemplate（Qwen） ===')
    print(hf.messages2prompt(messages))
except Exception as e:
    print('HFChatTemplate 部分待本地验证，跳过：', e)
```

**需要观察的现象**：

- 自定义模板输出包含 `<|im_start|>system\n你是一个由本教程创建的机器人。<|im_end|>` 开头，随后是 user / assistant 段。
- 若 HFChatTemplate 可运行，对比两者：自定义模板用的是**你写的占位符**，HFChatTemplate 用的是 **Qwen tokenizer 里那段 Jinja 模板**——格式接近但来源不同。

**预期结果**：成功注册并使用自定义模板；能讲清「`BaseChatTemplate` 子类 = 自己拼字符串」与「`HFChatTemplate` = 委托 tokenizer」两条路线的本质区别。HFChatTemplate 部分若本地无网络/GPU 环境，**待本地验证**。

## 6. 本讲小结

- `ChatTemplateConfig`（`model.py`）是模板的「参数包」，本身不拼接；经 `chat_template()` 方法在 `MODELS` 注册表里查名实例化，支持 `to_json` / `from_json` 做 JSON 自定义。
- `BaseChatTemplate` 是「占位符拼接」基类，直接实现 `{system}{meta_instruction}{eosys}{user}{content}{eoh}{assistant}...` 公式；绝大多数内置模板（Vicuna/Llama2/Mistral…）只覆盖占位符默认值即可复用。
- `messages2prompt` 同时支持字符串与 OpenAI 风格 messages 列表；`sequence_start` 区分首轮与续接，末尾对 assistant 结尾做截断以支持续写。
- `HFChatTemplate` 不自己拼字符串，而是加载 tokenizer 调用 `apply_chat_template(tokenize=False, add_generation_prompt=...)`；用「哨兵探测」反演 user/assistant/system 边界标记，并用「哨兵系统消息」处理多轮续接。
- `HFChatTemplate` 必须位于注册表末尾，因为其 `match()` 是兜底逻辑（只要能构造实例就匹配），否则会抢占具体模板。
- `pipeline(chat_template_config=...)` 经 `get_chat_template`（`model.py` 末尾）路由：用户显式配置优先，否则按 `match()` 自动匹配，CLI 的 `--chat-template` 走同一通路。

## 7. 下一步学习建议

- **接入新模型时如何选模板**：新模型优先依赖 `HFChatTemplate`（让 tokenizer 的 Jinja 模板干活）；仅当需要特殊行为（如 CodeLlama 的 infilling、ChatGLM 的 `[Round n]`）时才手写 `BaseChatTemplate` 子类，可参考 [u10-l1 添加新 PyTorch 模型完整流程](u10-l1-add-new-pytorch-model.md)。
- **多模态模板**：VLM 的图像占位符如何融入 prompt，见 `lmdeploy/vl/model/base.py` 的 `get_input_prompt`（它会调用 `chat_template.messages2prompt`），后续 [u9-l1 视觉语言模型 VLM 处理](u9-l1-vision-language-models.md) 会展开。
- **工具调用 / function calling**：留意 `messages2prompt` 里 `tool` / `eotool` 占位符与 `**kwargs`（如 `tools=`、`enable_thinking`）的传递，这关系到 [u8-l1 OpenAI 兼容 API 服务总览](u8-l1-openai-api-server-overview.md) 里的 tool 接口。
- **继续阅读源码**：通读 `lmdeploy/model.py` 全文（尤其各子类的 `match` 方法），理解自动匹配的优先级；再读 `get_chat_template` 把整条「配置 → 模板 → prompt」链路在脑中跑通。
