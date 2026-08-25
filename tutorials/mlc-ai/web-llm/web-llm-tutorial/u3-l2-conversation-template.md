# u3-l2 Conversation 对话模板与提示词编码

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `ConvTemplateConfig` 中 `system_template`、`roles`、`seps`、`role_content_sep` 等字段如何共同描述一套对话格式，并能读懂 Llama-2 的 `[INST]` 风格与 ChatML 的 `<|im_start|>` 风格模板。
2. 追踪从 `appendMessage` / `appendReplyHeader` 到 `getPromptArray` 的完整编码链路，说清一条 OpenAI 风格的 `messages` 数组是如何一步步变成模型实际输入的 prompt 的。
3. 理解 `MessagePlaceholders` 占位符机制，以及多模态（带图片）消息在 prompt 数组中的特殊表示方式。
4. 独立编写一个 Node 单测风格的小脚本，打印并肉眼验证两类模板的拼接结果。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一：模型只认「一整段字符串」，不认「消息数组」。**
我们调用 `engine.chatCompletion({ messages: [...] })` 时传的是结构化的消息数组，但语言模型的输入本质上是一个 token 序列。把数组「拍平」成字符串的过程，就是本讲要讲的提示词编码（prompt encoding）。

**第二：不同模型在训练时使用的「对话格式」不同，格式错了模型就不会说话。**
例如 Llama-2 用 `[INST] ... [/INST]` 包裹用户指令，Qwen 系列（ChatML 风格）用 `<|im_start|>user ... <|im_end|>`。如果给一个按 ChatML 训练的模型喂 Llama-2 格式的 prompt，输出质量会明显劣化。所以每个模型都必须携带一份「自己的格式说明书」，这就是 `conv_template`（对话模板）。

**第三：`conv_template` 来自哪里。**
回顾 u1-l4：引擎 `reload` 时会从模型权重仓库（HuggingFace）下载 `mlc-chat-config.json`，展开成 `ChatConfig`。`ChatConfig` 中的 `conv_template` 字段就是本讲的 `ConvTemplateConfig`；`conv_config` 字段则是对它的运行时局部覆盖（override），两者在创建会话时合并。

另外回顾 u2-l2 的结论：多轮对话时引擎会「比对新旧 Conversation，命中即复用 KV cache」。本讲会讲到这个比对的实现（`compareConversationObject`），把上一讲的黑盒打开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/config.ts` | 定义 `ConvTemplateConfig`（模板字段）、`Role`（角色枚举）、`MessagePlaceholders`（占位符枚举），以及 `ChatConfig` 中的 `conv_template` / `conv_config` 两个字段 |
| `src/conversation.ts` | 本讲主角。`Conversation` 类维护会话状态并负责 prompt 编码；`getConversation` 工厂、`getConversationFromChatCompletionRequest`（请求→会话）、`compareConversationObject`（会话比对）也在这个文件 |
| `src/llm_chat.ts` | Conversation 的「消费方」：`LLMChatPipeline` 创建会话、往里追加消息、调用 `getPromptArray` 拿到编码结果 |
| `src/engine.ts` | 多轮对话判定：用 `compareConversationObject` 决定复用还是重置 KV cache |
| `tests/conversation.test.ts` | 行为基准。用真实模板 JSON 断言了拼接结果，是本讲实践的模板 |
| `tests/constants.ts` | 三份完整的 `mlc-chat-config.json` 字符串常量（llama-2、phi3.5-vision、qwen3），单测的数据来源 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 ConvTemplateConfig（格式的规格书）**、**4.2 Conversation 类（状态的载体与编码器）**、**4.3 getConversation 工厂（装配与比对）**。

### 4.1 ConvTemplateConfig：一套对话格式的规格说明书

#### 4.1.1 概念说明

`ConvTemplateConfig` 回答一个问题：「这个模型的每一条消息长什么样？」它把对话格式拆成可组合的零件：

| 字段 | 含义 | Llama-2 示例值 | Qwen3（ChatML）示例值 |
| --- | --- | --- | --- |
| `system_template` | 系统提示词的外壳，含 `{system_message}` 占位符 | `[INST] <<SYS>>\n{system_message}\n<</SYS>>\n\n` | `<\|im_start\|>system\n{system_message}<\|im_end\|>\n` |
| `system_message` | 默认系统提示词内容 | `"You are a helpful, respectful and honest assistant."` | `"You are a helpful assistant."` |
| `roles` | 各角色（user/assistant/tool）显示成什么字符串 | user→`[INST]`，assistant→`[/INST]` | user→`<\|im_start\|>user`，assistant→`<\|im_start\|>assistant` |
| `role_templates` | 可选的角色级模板，内含 `{user_message}` 等占位符 | `{user_message}` | `{user_message}` |
| `seps` | 每条消息末尾的分隔符数组 | `[" "]` | `["<\|im_end\|>\n"]` |
| `role_content_sep` | 角色名与内容之间的分隔符（缺省 `": "`） | `" "` | `"\n"` |
| `role_empty_sep` | 角色名与「空回复头」之间的分隔符（缺省 `": "`） | `" "` | `"\n"` |
| `stop_str` | 解码阶段的停止字符串列表 | `["[INST]"]` | `["<\|endoftext\|>", "<\|im_end\|>"]` |
| `stop_token_ids` | 解码阶段的停止 token id 列表 | `[2]` | `[151643, 151645]` |
| `system_prefix_token_ids` | 可选，prompt 起始处强制注入的 token（如 BOS） | `[1]` | `null` |
| `add_role_after_system_message` | 系统提示词后是否立刻加角色前缀 | `false` | `true` |

其中「占位符」是一组固定字符串，由 `MessagePlaceholders` 枚举定义——`{system_message}`、`{user_message}`、`{assistant_message}`、`{tool_message}`、`{function_string}` 等，运行时会被真实内容替换。`Role` 枚举只有 `user`、`assistant`、`tool` 三个值，是 `roles` 这张记录表的键。

`add_role_after_system_message` 是最容易被忽略的字段：Llama-2 的格式里，`[INST]` 已经出现在 `system_template` 开头了，所以紧跟系统提示词的第一条用户消息**不再**重复加 `[INST]` 前缀；而 ChatML 的 `system_template` 自成一段，后面的每条消息都要以 `<|im_start|>user` 开头。一个布尔值区分了两种排版传统。

#### 4.1.2 核心流程

把一条消息编码成字符串的通用公式（省略边界分支）：

```text
prompt = system_template.replace("{system_message}", system_message 或 override)
对于第 i 条消息 (role, role_str, content):
    message_str = role_templates[role] 中的占位符替换为 content 文本
                  （若无 role_templates 或无该角色模板，则直接用 content）
    role_prefix = role_str + role_content_sep        # 默认 ": "
                  （若 add_role_after_system_message=false 且这是系统提示词后的第一条消息，则为 ""）
    消息片段   = role_prefix + message_str + seps[i % seps.length]
```

注意 `seps[i % seps.length]`：`seps` 数组长度可以是 1（所有消息同一分隔符）或 2（用户与助理消息交替使用不同分隔符，`i` 为消息下标，天然随轮次交替）。

`stop_str` 与 `stop_token_ids` 虽然写在模板里，但它们不是用来拼 prompt 的——它们会被管线取走，作为 decode 阶段的终止条件之一（详见 u3-l4）。

#### 4.1.3 源码精读

类型定义在 [src/config.ts:L16-L28](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L16-L28)：`ConvTemplateConfig` 接口，注意 `role_content_sep`、`role_empty_sep`、`add_role_after_system_message` 都是可选字段，这就是编码逻辑里要做缺省回退 `": "` 的原因。

角色与占位符在 [src/config.ts:L42-L46](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L42-L46)（`Role` 枚举：user/assistant/tool）和 [src/config.ts:L50-L65](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L50-L65)（`MessagePlaceholders` 枚举，注释里给了 role template 的使用示例）。

`ChatConfig` 中模板相关字段见 [src/config.ts:L85-L92](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L85-L92)：`conv_config?: Partial<ConvTemplateConfig>` 是覆盖项，`conv_template: ConvTemplateConfig>` 是模板本体，注释说明这些字段「affect the entire conversation」。

两份真实模板常量：Llama-2 的在 [tests/constants.ts:L77-L109](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/constants.ts#L77-L109)（一段 JSON 字符串，字段与上表一一对应）；ChatML 风格（qwen3）的在 [tests/constants.ts:L270-L301](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/constants.ts#L270-L301)。建议把这两段对照着读一遍。

> 补充说明：仓库测试常量里预置的是 llama-2 与 qwen3（ChatML）两套模板；Llama-3 等其他模型的模板不在仓库源码里，而在各自模型仓库的 `mlc-chat-config.json` 中，`reload` 时随权重一起下载（见 u1-l4）。本讲实践用这两套预置模板，Llama-3 格式作为一个扩展练习。

#### 4.1.4 代码实践

**实践目标**：不写代码，先练「人肉编译器」，对手工推演 prompt 建立直觉。

**操作步骤**：

1. 打开 [tests/constants.ts:L77-L109](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/constants.ts#L77-L109)，抄下 llama-2 模板的 `system_template`、`roles`、`seps`、`role_content_sep`、`add_role_after_system_message`。
2. 假设对话为：user 说 `test1`，assistant 说 `test2`，user 说 `test3`，然后等 assistant 回复。
3. 按 4.1.2 的公式逐条展开，把结果写在纸上。

**需要观察的现象**：第一轮用户消息前**没有** `[INST]`（因为 `add_role_after_system_message: false` 且它是系统提示词后的第一条）；等待回复的位置以 `[/INST] ` 结尾（角色名 + `role_empty_sep`）。

**预期结果**：你手工拼出的字符串应当与 [tests/conversation.test.ts:L48-L51](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/conversation.test.ts#L48-L51) 中断言的完全一致：

```text
[INST] <<SYS>>\nYou are a helpful, respectful and honest assistant.\n<</SYS>>\n\ntest1 [/INST] test2 [INST] test3 [/INST] 
```

（`\n` 为换行符；结尾还有一个空格，来自最后那条空回复头的 `role_empty_sep: " "`。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 Llama-2 模板的 `add_role_after_system_message` 是 `false`，而 Qwen3 是 `true`？

**答案**：Llama-2 的 `system_template` 以 `[INST] <<SYS>>...` 开头，`[INST]` 这个「本轮用户发言开始」的标记已经由系统提示词段提供了，紧随其后的第一条用户消息不需要再重复前缀；否则会出现两个连续的 `[INST]`。ChatML 的系统段以 `<|im_end|>\n` 自我封闭，后续每条消息（包括第一条用户消息）都必须以 `<|im_start|>角色名` 重新开头，所以是 `true`。

**练习 2**：`stop_str` 和 `stop_token_ids` 有什么区别？为什么 Qwen3 需要两个停止字符串？

**答案**：`stop_str` 是在解码出的文本上匹配的字符串（如 `[INST]`），`stop_token_ids` 是在 token id 层面匹配的终止符（如 eos token）。Qwen3 的 `stop_str` 同时含 `<|endoftext|>` 与 `<|im_end|>`，因为普通结尾与对话结尾用不同特殊 token，且字符串匹配不总与 token 边界对齐，两层兜底更稳。二者的消费方在 decode 循环（u3-l4 详述）。

### 4.2 Conversation 类：会话状态的唯一载体

#### 4.2.1 概念说明

`Conversation` 是「格式 + 历史」的结合体：它持有 `config`（一份 `ConvTemplateConfig`）和 `messages`（对话历史），对外提供追加消息、编码 prompt、复位等方法。它是推理管线与会话格式之间的桥梁——管线只管「把 prompt 编码成 token 前向」，完全不知道 `[INST]` 或 `<|im_start|>` 是什么。

`messages` 数组的每一项是一个三元组：

```text
[Role, role_name_str, content]
```

其中 `content` 有三种取值，对应三种语义：

| content 取值 | 语义 | 由谁写入 |
| --- | --- | --- |
| 字符串 | 一条已完成的消息（用户输入或模型回复） | `appendMessage` / `finishReply` |
| `Array<ChatCompletionContentPart>` | 多模态消息（文本 + 图片混合数组） | `appendMessage` |
| `undefined` | 「空回复头」——对话停在等待 assistant 发言的位置 | `appendReplyHeader` |

`undefined` 这个哨兵值很关键：它是「模型接下来要在这里接话」的标记。生成结束后，`finishReply` 会把模型输出回填到这个位置，于是这轮问答变成下一轮的「历史」。

#### 4.2.2 核心流程

一次完整生成中，Conversation 的状态流转：

```text
管线构造:  getConversation(conv_template, conv_config)   → 全新空会话
发起请求:  appendMessage(user, 输入文本)                  → 历史追加最后一条用户消息
           appendReplyHeader(assistant)                   → 追加 undefined 哨兵
编码输入:  filledKVCacheLength == 0
             ? getPromptArray()          → 全量 prompt（含系统提示词）
             : getPromptArrayLastRound() → 只编码最后一轮（增量）
生成循环:  模型逐 token 输出（Conversation 不参与）
生成结束:  finishReply(完整回复文本)       → 哨兵位置回填，undefined 变字符串
复位:      reset()                        → 清空全部状态
```

`getPromptArrayInternal` 是编码核心，逐消息处理时的四个分支：

1. **空回复头分支**（content 为 `undefined`）：只输出 `role_str + role_empty_sep`，如 `[/INST] `。若 `undefined` 不在最后一条则抛内部错误。
2. **空思考块分支**：Qwen3 等推理模型在 `enable_thinking: false` 时，回复头里预置一段空 `<think>\n\n</think>\n\n`，让模型跳过思考直接作答。
3. **普通消息分支**：content 若是数组则先抽出「恰好一个」文本部分与零到多个图片（第二个文本部分抛 `MultipleTextContentError`）；再按 4.1.2 的公式做占位符替换、拼角色前缀与分隔符。
4. **多模态分支**：含图片时，该消息的返回值不再是字符串，而是 `Array<string | ImageURL>`，图片 URL 对象按模型要求的排布插入（phi3_v 系列在每张图后补一个 `"\n"`）。

#### 4.2.3 源码精读

**状态定义**：[src/conversation.ts:L34-L45](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L34-L45) 定义 `messages` 三元组数组、`config`、文本补全模式标志 `isTextCompletion` 与 `prompt`（u2-l4 已讲：文本补全会话走哑模式，会话式方法全部被守卫拦截）。

**追加消息与守卫**：[src/conversation.ts:L302-L321](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L302-L321) 的 `appendMessage` 先检查「上一条不是未完成的回复头」（否则抛 "Have unfinished reply"）、检查角色在 `config.roles` 中存在，然后 push 三元组。[src/conversation.ts:L323-L331](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L323-L331) 的 `appendReplyHeader` 压入 `undefined` 哨兵。[src/conversation.ts:L343-L360](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L343-L360) 的 `finishReply` 把哨兵回填为完整回复，并清掉空思考块标志。

**编码核心**：[src/conversation.ts:L67-L215](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L67-L215) 的 `getPromptArrayInternal`。分四段读：

- [src/conversation.ts:L76-L87](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L76-L87)：系统提示词装配——`override_system_message` 优先于模板默认值，再经 `system_template.replace(MessagePlaceholders.system, ...)` 注入外壳；`addSystem && system_prompt !== ""` 才纳入结果（phi3.5-vision 没有 system 模板，见测试注释）。
- [src/conversation.ts:L96-L125](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L96-L125)：空回复头与空思考块两个特殊分支，分别用 `role_empty_sep` / `role_content_sep` 缺省回退 `": "`。
- [src/conversation.ts:L127-L168](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L127-L168)：content 解构（文本 + 图片分离）与 `role_templates` 占位符替换——先用 `MessagePlaceholders[role]`（如 `{user_message}`）替换文本，再视函数调用开关决定 `{function_string}` 的去留。
- [src/conversation.ts:L173-L213](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L173-L213)：`add_role_after_system_message === false` 时首条消息去掉角色前缀；无图走纯字符串，有图走 `Array<string | ImageURL>`，`phi3_v` 模型类型下每张图片后追加换行符。

**三个对外编码入口**：

- [src/conversation.ts:L239-L246](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L239-L246) `getPromptArray`：带系统提示词的全量编码，起始下标 0。
- [src/conversation.ts:L256-L264](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L256-L264) `getPromptArrayLastRound`：不带系统提示词、从倒数第二条消息开始编码——**增量编码**，这是多轮对话能复用 KV cache 的前提（上一轮之前的 token 已经在 cache 里，无需重复编码）。
- [src/conversation.ts:L269-L274](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L269-L274) `getPromptArrayTextCompletion`：文本补全哑模式，直接返回 `[this.prompt]`。

**管线侧的消费链路**：

- [src/llm_chat.ts:L193-L198](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L193-L198)：管线构造时 `getConversation(config.conv_template, config.conv_config)` 创建会话，并顺手取出 `getStopStr()` / `getStopTokens()` 供 decode 使用（[src/conversation.ts:L289-L300](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L289-L300)）。
- [src/llm_chat.ts:L833-L850](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L833-L850)：每次生成前 `appendMessage` 追加本轮输入，随后 `appendReplyHeader`（或 `enable_thinking: false` 时的 `appendEmptyThinkingReplyHeader` + 空思考块）。
- [src/llm_chat.ts:L2026-L2045](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2026-L2045)：`getInputData` 按 `filledKVCacheLength` 是否为 0 选择全量或增量编码；首次编码还会把 `system_prefix_token_ids`（如 BOS）垫在 token 序列最前面。
- [src/llm_chat.ts:L948](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L948)：生成结束后 `finishReply(this.outputMessage)` 回填回复，会话闭环。

**复位**：[src/conversation.ts:L279-L287](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L279-L287) 的 `reset` 清空全部状态（`resetChat` 会调用它，见 [src/llm_chat.ts:L532](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L532) 附近）。

#### 4.2.4 代码实践

**实践目标**：用现有测试验证你对编码逻辑的理解，并观察「多模态消息」的数组形态。

**操作步骤**：

1. 在仓库根目录运行：`npx jest tests/conversation.test.ts`（jest 以 `tests/` 为测试根目录，见 [jest.config.cjs:L4](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/jest.config.cjs#L4)；无需 GPU，纯内存运算）。
2. 精读 [tests/conversation.test.ts:L240-L259](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/conversation.test.ts#L240-L259)：单图输入的断言展示了 `getPromptArray` 返回值中「图片消息」是 `[前缀字符串, ImageURL对象, "\n", 文本+分隔符]` 这样的混合数组，而系统提示词仍是纯字符串——两种形态共存于同一个返回数组。
3. 临时把该测试里的 `expect(conv1.getPromptArray(config)).toEqual(...)` 注释掉，换成 `console.log(JSON.stringify(conv1.getPromptArray(config), null, 2))`，重跑并查看控制台输出，然后恢复原样。

**需要观察的现象**：控制台输出的第二个元素是数组而非字符串，其中图片位置是一个 `{ url: "https://url1" }` 对象——prompt 数组保留图片的原始 URL，供管线后续下载图片并做 image embedding（u3-l6 详述）。

**预期结果**：测试全部通过；打印结果与断言一致。若第 3 步只看到部分现象，请检查是否传了 `config`（图片排布依赖 `config.model_type === "phi3_v"` 判断）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `getPromptArrayLastRound` 要求 `messages.length >= 3`，而且从 `length - 2` 开始编码？

**答案**：调用约定是「先 `appendMessage`（本轮输入）再 `appendReplyHeader`」，所以增量编码时最后两条消息分别是本轮输入与空回复头；`length - 2` 正好跳到本轮输入这条。历史消息（更早的下标）的 token 已经写进 KV cache，不需要也无法重复编码。少于 3 条说明这是首轮，应当走全量 `getPromptArray`。

**练习 2**：如果同一轮对话里，模型已经生成了半截回复（`appendReplyHeader` 已调用但 `finishReply` 未调用），这时又来一条新的 `appendMessage`，会发生什么？

**答案**：`appendMessage` 检测到上一条消息的 content 为 `undefined`，抛出 `"Have unfinished reply"` 错误（[src/conversation.ts:L310-L315](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L310-L315)）。这保证会话状态机不会出现「两个等待回复的空洞」。引擎层的生成互斥锁（u2-l3）在更外层阻止了这种情况的发生。

**练习 3**：`role_empty_sep` 与 `role_content_sep` 什么时候会不一样？举一个用途。

**答案**：前者用于「空回复头」（只有角色名，等待模型接话），后者用于「有内容的消息」。二者分离使模板能对「发言开始处」与「发言内容前」做不同排版，例如某些模板回复头后是换行、而历史消息的角色与内容之间是空格；Llama-2 两者同为 `" "`，Qwen3 两者同为 `"\n"`，WebLLM 保留这个自由度以兼容更多格式。

### 4.3 getConversation 工厂与「请求 → 会话」的装配

#### 4.3.1 概念说明

三个自由函数补全了从「OpenAI 请求」到「可编码会话」的最后一公里：

1. **`getConversation`（工厂）**：用展开运算符把 `conv_config`（覆盖项）合并进 `conv_template`（模板本体）再构造 `Conversation`。这是 u1-l4 所讲「三层配置合并」在会话层的落点——后写的覆盖项赢。
2. **`getConversationFromChatCompletionRequest`（翻译器）**：把 `engine.chatCompletion` 收到的 `messages` 数组翻译成 `Conversation` 对象：system 消息变成 `override_system_message`（而不是模板默认值），user/assistant/tool 消息逐条 `appendMessage`，并做顺序校验。
3. **`compareConversationObject`（比对器）**：深比较两个会话的状态（消息逐条、图片 URL 逐个比）。引擎用它判断「这次请求是不是上一轮对话的延续」，从而决定复用还是清空 KV cache——这就是 u2-l2 那句「比对新旧 Conversation，命中即复用」的实现。

#### 4.3.2 核心流程

请求到达引擎后的装配与判定流程：

```text
chatCompletion(request):
  newConv = getConversationFromChatCompletionRequest(request, chatConfig)
            ├─ getConversation(conv_template, conv_config)      # 工厂 + 覆盖合并
            ├─ 校验: 最后一条消息必须是 user 或 tool（否则 MessageOrderError）
            ├─ system 消息必须在第 0 条（否则 SystemMessageOrderError）
            └─ 注意: 默认不翻译最后一条消息（includeLastMsg=false）
  oldConv = pipeline.getConversationObject()
  compareConversationObject(oldConv, newConv)?
      ├─ 相同 → 多轮对话，复用 KV cache，只增量编码最后一轮
      └─ 不同 → resetChat()（清 KV cache）+ setConversation(newConv)
  最后一条消息由管线的 appendMessage + appendReplyHeader 加入，再编码
```

#### 4.3.3 源码精读

**工厂**：[src/conversation.ts:L363-L372](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L363-L372)——`{ ...conv_template, ...conv_config }` 一行完成覆盖合并，`isTextCompletion` 缺省 `false`。

**翻译器**：[src/conversation.ts:L469-L519](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L469-L519) 的 `getConversationFromChatCompletionRequest`。三个要点：

- [src/conversation.ts:L491-L495](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L491-L495)：最后一条必须是 `user` 或 `tool`（模型总要「回应」什么），否则抛 `MessageOrderError`。
- [src/conversation.ts:L496](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L496)：`iterEnd` 默认排除最后一条——注释写明它会被当作本轮 prefill 的输入，由管线稍后 `appendMessage`（[src/llm_chat.ts:L837](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L837)）。
- [src/conversation.ts:L499-L503](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L499-L503)：system 消息只允许出现在第 0 条（否则 `SystemMessageOrderError`），其内容写入 `override_system_message`，编码时优先生效于模板默认的 `system_message`。

**比对器**：[src/conversation.ts:L382-L459](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L382-L459) 的 `compareConversationObject`。先比标量（`function_string`、`use_function_calling`、`override_system_message`、消息条数、`isTextCompletion`，[L388-L394](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L388-L394)），再逐条深比消息三元组：文本比字符串、图片比 `image_url.url` 与 `detail`、类型不同直接判不等（[L410-L455](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L410-L455)）。注意两处「NOTE: Update this function whenever a new state is introduced」注释（[L33](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L33)、[L386](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L386)）：给 `Conversation` 加状态字段时必须同步更新比对器，否则 KV cache 复用判定会漏掉新状态、产生错误的复用。

**引擎侧的判定**：[src/engine.ts:L1379-L1397](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1379-L1397)：`compareConversationObject` 返回 `false` 时 `resetChat()` + `setConversation(newConv)`；返回 `true` 且新会话非空则记录 "Multiround chatting, reuse KVCache."。这就是篡改历史消息会触发全量重算（u2-l2）的判断点。

本文件还有一个 `getFunctionCallUsage`（[src/conversation.ts:L528-L568](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L528-L568)），把 `request.tools` 序列化成函数字符串供 `{function_string}` 占位符使用；不过其主调用点在 `getConversationFromChatCompletionRequest` 中已被注释停用（[L481-L486](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L481-L486)，为 gorilla 式函数调用保留），WebLLM 当前的函数调用走的是另一条协议层路径（u6-l2 详述），这里只需知道占位符机制的存在。

#### 4.3.4 代码实践

**实践目标**：直观验证 `compareConversationObject` 的判定粒度，理解「什么样的两次请求会被判为同一对话」。

**操作步骤**：

1. 精读 [tests/conversation.test.ts:L157-L195](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/conversation.test.ts#L157-L195)：四个请求——完全相同的两个、改了图片 URL 的、改了文本的。
2. 在本地新建文件 `tests/conversation-practice.test.ts`（示例代码，见下方），运行 `npx jest tests/conversation-practice.test.ts`。

```ts
// tests/conversation-practice.test.ts（示例代码：需要读者自行新建，仓库中不存在此文件）
import { ChatConfig } from "../src/config";
import {
  compareConversationObject,
  getConversationFromChatCompletionRequest,
} from "../src/conversation";
import { describe, expect, test } from "@jest/globals";
import { qwen3ChatConfigJSONString } from "./constants";
import { ChatCompletionRequest } from "../src/openai_api_protocols";

describe("Practice: conversation compare", () => {
  test("override system message breaks equality", () => {
    const config = JSON.parse(qwen3ChatConfigJSONString) as ChatConfig;
    const mk = (system?: string): ChatCompletionRequest => ({
      messages: system
        ? [{ role: "system", content: system }, { role: "user", content: "hi" }]
        : [{ role: "user", content: "hi" }],
    });
    const convA = getConversationFromChatCompletionRequest(mk(), config);
    const convB = getConversationFromChatCompletionRequest(
      mk("custom system"),
      config,
    );
    // override_system_message 一个是 undefined、一个是 "custom system"
    expect(compareConversationObject(convA, convB)).toBe(false);
  });
});
```

**需要观察的现象**：即使两条消息的用户输入完全相同，只要 system 提示词出现与否不同，比对结果就是 `false`——引擎会放弃 KV cache 全量重算。

**预期结果**：测试通过（依据 [src/conversation.ts:L391](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L391) 对 `override_system_message` 的比较逻辑推演；`undefined !== "custom system"`）。如未通过请检查 `mk()` 是否真的省略了 system 消息。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`getConversationFromChatCompletionRequest` 为什么默认不把最后一条消息翻译进会话？

**答案**：因为它与最后一条消息的「去向」解耦：引擎把最后一条消息单独取出，等管线在 `asyncGenerate` 里 `appendMessage` + `appendReplyHeader`，再增量编码并 prefill。翻译器只负责「历史」，本轮输入由管线负责，两段职责清晰分离；`includeLastMsg` 参数仅测试时打开（如 [tests/conversation.test.ts:L171-L190](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/conversation.test.ts#L171-L190) 用它构造完整会话）。

**练习 2**：用户在第二轮把第一轮的系统提示词从 A 改成 B，`compareConversationObject` 会怎么判？

**答案**：`override_system_message` 参与标量比较（[L391](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L391)），A ≠ B 判为不同会话，引擎 `resetChat()` 清空 KV cache 后重新全量编码——系统提示词参与了整个 prompt 的构造，任何变化都使旧 cache 失效。

## 5. 综合实践

**任务**：写一个 Node 单测风格的打印脚本，对「Llama 家族（[INST] 风格）」与「ChatML 风格」两套模板分别构造多轮对话，打印 `getPromptArray` 的输出，肉眼验证 system prompt、角色标签与分隔符的拼接（对应本讲规格中的实践任务；仓库预置常量是 llama-2 与 qwen3，故以这两套真实模板为载体）。

**操作步骤**：

1. 确认已 `npm install`（jest 已在 devDependencies 中，`npm test` 脚本定义为 `jest --coverage`，见 [package.json:L11](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/package.json#L11)）。
2. 新建 `tests/conversation-template-practice.test.ts`（示例代码，仓库中不存在，需自行新建）：

```ts
// tests/conversation-template-practice.test.ts（示例代码）
import { ChatConfig, Role } from "../src/config";
import { getConversation } from "../src/conversation";
import { describe, test } from "@jest/globals";
import {
  llama2ChatConfigJSONString,
  qwen3ChatConfigJSONString,
} from "./constants";

describe("Practice: Llama-2 vs ChatML prompt encoding", () => {
  test("print prompt arrays of a 3-message dialog", () => {
    const cases: Array<[string, string]> = [
      ["Llama-2 ([INST] style)", llama2ChatConfigJSONString],
      ["Qwen3 (ChatML style)", qwen3ChatConfigJSONString],
    ];
    for (const [name, json] of cases) {
      const config = JSON.parse(json) as ChatConfig;
      const conv = getConversation(config.conv_template);
      conv.appendMessage(Role.user, "你好，介绍一下你自己");
      conv.appendMessage(Role.assistant, "我是一个运行在浏览器里的助手。");
      conv.appendMessage(Role.user, "谢谢！");
      conv.appendReplyHeader(Role.assistant); // 等待模型接话的位置
      const arr = conv.getPromptArray();
      console.log(`===== ${name} =====`);
      console.log("分段形态:", JSON.stringify(arr, null, 2));
      console.log("拼接结果:", arr.join(""));
    }
  });
});
```

3. 运行：`npx jest tests/conversation-template-practice.test.ts`。
4. 对照下方的「预期结果」逐字符核对两段输出，重点核对三处：系统提示词外壳、每条消息的角色标签与分隔符、结尾空回复头的形态。
5. **扩展**：再手写一份 Llama-3 风格模板跑一遍（示例代码，具体字段以模型仓库的 `mlc-chat-config.json` 为准，待本地验证）：

```ts
// 示例代码：Llama-3 风格模板（示意，非仓库自带常量）
const llama3ishTemplate = {
  system_template:
    "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n\n{system_message}<|eot_id|>",
  system_message: "You are a helpful assistant.",
  roles: {
    user: "<|start_header_id|>user<|end_header_id|>\n\n",
    assistant: "<|start_header_id|>assistant<|end_header_id|>\n\n",
    tool: "<|start_header_id|>user<|end_header_id|>\n\n",
  },
  seps: ["<|eot_id|>"],
  stop_str: ["<|eot_id|>"],
  stop_token_ids: [128009],
} as const;
```

把它作为 `getConversation(llama3ishTemplate)` 的入参（类型断言按需调整），观察 `add_role_after_system_message` 缺省（`undefined`，非 `false`）时首条用户消息的处理与 llama-2 常量的差异。

**需要观察的现象**：

- Llama-2 输出中，系统提示词后**直接**是 `test` 文本（无 `[INST]` 重复），assistant 消息以 `[/INST] ` 开头，结尾空回复头是 `[/INST] `；
- ChatML 输出中，每条消息（包括第一条用户消息）都以 `<|im_start|>角色名\n` 开头、以 `<|im_end|>\n` 结束，结尾空回复头是 `<|im_start|>assistant\n`。

**预期结果**（依据源码与常量手工推演，待本地验证）：

Llama-2 分段数组大致为：

```text
[
  "[INST] <<SYS>>\nYou are a helpful, respectful and honest assistant.\n<</SYS>>\n\n",
  "你好，介绍一下你自己 ",
  "[/INST] 我是一个运行在浏览器里的助手。 ",
  "[INST] 谢谢！ ",
  "[/INST] "
]
```

ChatML 分段数组大致为：

```text
[
  "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n",
  "<|im_start|>user\n你好，介绍一下你自己<|im_end|>\n",
  "<|im_start|>assistant\n我是一个运行在浏览器里的助手。<|im_end|>\n",
  "<|im_start|>user\n谢谢！<|im_end|>\n",
  "<|im_start|>assistant\n"
]
```

若与预期不符，回到 [src/conversation.ts:L67-L215](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L67-L215) 定位是哪个分支（系统段 / 角色前缀 / 分隔符 / 空回复头）与推演不一致，并对照 [tests/conversation.test.ts:L19-L85](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/conversation.test.ts#L19-L85) 的断言修正理解。完成后删除该实践文件，避免污染仓库测试。

## 6. 本讲小结

- `ConvTemplateConfig` 是对话格式的「规格书」：`system_template` + `system_message` 描述系统段，`roles` / `role_content_sep` / `seps` 描述每条消息的「角色名 + 分隔符 + 内容 + 结尾符」，`add_role_after_system_message` 决定系统段后首条消息是否省略角色前缀。
- `MessagePlaceholders`（`{system_message}`、`{user_message}` 等）是模板中的占位符，在 `getPromptArrayInternal` 里被逐条替换成真实内容；`{function_string}` 服务于函数调用注入。
- `Conversation` 用三元组 `[Role, role_name_str, content]` 维护历史，`undefined` 哨兵表示「等待 assistant 接话」的空回复头，`finishReply` 负责回填闭环。
- `getPromptArray` 全量编码（含系统段），`getPromptArrayLastRound` 增量编码最后一轮——增量编码是 KV cache 复用的前提；含图片的消息编码为 `Array<string | ImageURL>` 混合数组而非字符串。
- `getConversation` 工厂以 `{...template, ...conv_config}` 完成覆盖合并；`getConversationFromChatCompletionRequest` 把 OpenAI messages 翻译成会话（system → `override_system_message`，默认排除最后一条消息）；`compareConversationObject` 深比状态，引擎据此决定复用还是清空 KV cache。
- `getStopStr` / `getStopTokens` 从模板流向解码循环，成为 decode 阶段的终止条件之一（u3-l4 展开）。

## 7. 下一步学习建议

下一讲 **u3-l3 prefillStep：预填充与 KV cache 写入** 将沿着本讲的出口继续：`getPromptArray` 产出的字符串/图片混合数组在 `getInputData` 中被 tokenize 成 token 序列，随后 `prefillStep` 把它们分块送入模型。建议预习时先读 [src/llm_chat.ts:L2018-L2100](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2018-L2100)（`getInputData` 的后半段：tokenize 与图片 URL 去重），思考一个问题：为什么 prompt 要编码成「字符串数组」而不是一个大字符串？（提示：图片 token 需要单独走 image embedding 路径。）若想先了解解码侧的终止条件如何消费本讲的 `stop_str` / `stop_token_ids`，也可以提前翻阅 u3-l4。
