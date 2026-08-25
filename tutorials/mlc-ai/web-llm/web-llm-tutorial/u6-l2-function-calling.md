# u6-l2 函数调用 Function Calling

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清函数调用的本质：模型**从不执行函数**，它只是输出一段「我想调用哪个函数、参数是什么」的结构化声明，真正的执行者是页面里的 JavaScript。
2. 追踪 `tools` 定义从请求进入后，如何被注入对话 prompt（system 消息 + JSON Schema 输出约束），模型输出的 JSON 字符串又如何被解析回 `tool_calls` 结构。
3. 精读 `getFunctionCallUsage` 的 `tool_choice` 校验逻辑，并理解它在当前代码库中「已实现、被测试覆盖、但生产路径被注释停用」的真实状态。
4. 区分两种函数调用方式——手动模板风格（manual）与 OpenAI 风格（`tools`/`tool_choice`/`tool_calls` 字段）——在灵活性、可靠性与适用模型上的差异。
5. 完成 `tool` 消息回传的多轮闭环：执行函数 → 把结果作为 `role: "tool"` 消息追加 → 模型生成自然语言回复。

## 2. 前置知识

### 2.1 函数调用（Function Calling / Tool Use）是什么

LLM 本质上只会「接着上文往下写字符串」。所谓函数调用，是社区约定的一套协议：

- **调用方**（你的页面）在请求里声明「这里有这些函数可用，参数格式如下」；
- **模型**不真的去执行任何代码，而是在回复里输出一段约定格式的文本，例如 `[{"name": "get_current_weather", "arguments": {"location": "Tokyo"}}]`；
- **调用方**解析这段文本，自己执行对应函数，再把执行结果作为新消息喂回去；
- 模型基于结果生成最终的自然语言回复。

整个循环里「执行」永远发生在你的代码里。因此函数调用 = **输入侧的 prompt 注入** + **输出侧的文本解析**，这两侧正是本讲源码精读的两条主线。

### 2.2 两种注入位置的取舍

把「函数清单」告诉模型有两种做法：

| | 手动模板风格（manual） | OpenAI 风格（`tools` 字段） |
|---|---|---|
| 函数清单写在哪 | 你自己写的 system 消息里 | WebLLM 自动生成的 system 消息里 |
| 输出格式约束 | 无强制，靠模型自觉（如 `<tool_call>...</tool_call>` 标签） | 自动设置 `response_format` 为 JSON Schema，解码时逐 token 语法约束 |
| 输出形态 | `message.content` 原始字符串，你自己解析 | `message.tool_calls` 结构化数组，`content` 为 `null` |
| 适用模型 | 任何模型（含 Llama-3.1） | 仅 `functionCallingModelIds` 白名单内的 Hermes 系模型 |

### 2.3 需要回顾的前置概念

- **JSON Schema**：用 JSON 描述「一个合法 JSON 长什么样」的规范，`FunctionDefinition.parameters` 与输出约束都用它表达。
- **`postInitAndCheckFields` 八道关卡**：u6-l1 精读过的协议层校验函数，本讲的函数调用逻辑正是其中的第 7 关，且它会**原位改写请求**。
- **Conversation 与 `override_system_message`**：u3-l2 讲过，system 消息的内容经它替换进 `system_template`。
- **grammar 约束采样**：u3-l5 提过 `GrammarMatcher` 在采样阶段屏蔽不合法 token；`response_format.type = "json_object"` + `schema` 就会走这条路（下一讲 u6-l3 展开）。
- **多轮对话 KV cache 复用**：u2-l2 讲过引擎比对新旧 Conversation 决定是否复用缓存，工具对话的第二轮同样受益。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/openai_api_protocols/chat_completion.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts) | `tools`/`tool_choice`/`tool_calls` 的协议类型定义；`postInitAndCheckFields` 第 7 关完成模型白名单检查与 Hermes 的 prompt 注入 |
| [src/support.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts) | Hermes 函数调用常量（系统提示词模板、输出 JSON Schema）；`getToolCallFromOutputMessage` 输出解析器 |
| [src/conversation.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts) | `getFunctionCallUsage`（`tool_choice` 校验的完整实现）；`{function_string}` 占位符渲染；`tool` 角色消息进入会话 |
| [src/config.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts) | `functionCallingModelIds` 白名单；`MessagePlaceholders` 占位符枚举 |
| [src/engine.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts) | 生成结束后的后置处理：把原始输出交给解析器、改写 `finish_reason`、组装修改后的响应 |
| [src/error.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts) | 函数调用专属错误族（模型不支持、tool_choice 非法、输出解析失败等） |
| [tests/function_calling.test.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/function_calling.test.ts) | `getFunctionCallUsage` 三种 `tool_choice` 的单测；Hermes2 / Llama3.1 多轮工具对话的格式对拍 |
| [tests/openai_chat_completion.test.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/openai_chat_completion.test.ts) | `postInitAndCheckFields` 第 7 关的五个校验用例（无需 WebGPU 即可运行） |
| [examples/function-calling/](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/function-calling/README.md) | 两个子示例：`function-calling-openai`（OpenAI 风格）与 `function-calling-manual`（手动模板风格） |

## 4. 核心概念与源码讲解

### 4.1 函数调用全景：一次完整的数据流

#### 4.1.1 概念说明

WebLLM 的函数调用不是独立的 API，而是「骑」在 `chatCompletion` 之上的三段式增强：

1. **输入侧注入**：请求带 `tools` 时，协议层在 prefill 之前把函数清单写进 system 消息，并给输出套上 JSON Schema 约束——模型「知道有哪些函数」且「只能说合法 JSON」。
2. **生成照旧**：管线（`LLMChatPipeline`）对此一无所知，照常 prefill + decode，产出一段 JSON 字符串。
3. **输出侧解析**：引擎层在生成结束后，把这段字符串 `JSON.parse` 成 `tool_calls` 结构化数组塞进响应。

这个「分层不感知」的设计让函数调用零侵入推理管线——所有逻辑都在协议层（chat_completion.ts）与引擎层（engine.ts）的后置处理里。

#### 4.1.2 核心流程

一次 OpenAI 风格函数调用的完整时序：

```text
用户页面                          WebLLM 内部
────────                          ───────────
engine.chatCompletion({
  messages: [user...],
  tools: [...],          ──►  postInitAndCheckFields 第7关：
  tool_choice: "auto"           ① 模型在 functionCallingModelIds 白名单？
                               ② Hermes 系？→ 注入 system 消息(含工具清单)
                                  + 覆写 response_format(json_object+schema)
                               ③ getConversationFromChatCompletionRequest：
                                  system → override_system_message
                               ④ json schema → xgrammar 编译 GrammarMatcher
                               ⑤ prefill + decode（每步采样被 grammar 约束）
                               ⑥ 输出 = '[{"name":...,"arguments":{...}}]'
                               ⑦ getToolCallFromOutputMessage 解析
                               ⑧ finish_reason: "stop" → "tool_calls"
                       ◄──  { message: {content: null, tool_calls: [...]},
                              finish_reason: "tool_calls" }
解析 tool_calls，
本地执行 get_current_weather()，
把结果作为 role:"tool" 消息追加 ──► 第二轮请求（KV cache 复用）
                       ◄──  自然语言最终回复
```

#### 4.1.3 源码精读

引擎在生成入口处用两行判断「这是不是函数调用请求」，并拒绝在 `completion` 接口上使用工具：

- [src/engine.ts:513-524](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L513-L524)：`isChatCompletion` 以「是否含 `messages` 字段」判别（可辨识联合），`isFunctionCalling` 以「`tools` 字段非空」判别；函数调用挂在 `chat.completions` 上，若在文本补全 `completions` 上带 `tools` 会直接抛错。

响应侧的 `finish_reason` 类型里专门为工具调用预留了枚举值：

- [src/openai_api_protocols/chat_completion.ts:1036-1040](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L1036-L1040)：`ChatCompletionFinishReason` 联合类型包含 `"tool_calls"`，表示「模型选择了调用工具而非直接作答」——它与 `stop`/`length`/`abort` 平级，是四种终止原因之一。

请求字段的文档注释则说清了流式场景下的行为约定：

- [src/openai_api_protocols/chat_completion.ts:222-235](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L222-L235)：`tools` 的 JSDoc 明确「带 `tools` 的回复会填充 `tool_calls` 字段；流式时最后一个 chunk 含 `tool_calls`，中间 chunk 是原始字符串；若终止原因是 `length` 或 `abort` 则不返回 `tool_calls`，用户仍可拿到原始字符串」。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到「模型输出的原始字符串」长什么样，建立函数调用 = 文本生成的直觉。
2. **操作步骤**：
   ```bash
   cd examples/function-calling/function-calling-manual
   npm install
   npm start   # Parcel 在 8888 端口起服务
   ```
   打开浏览器控制台。该示例默认调用 `llama3_1_example()`（见 [examples/function-calling/function-calling-manual/src/function_calling_manual.ts:233-235](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/function-calling/function-calling-manual/src/function_calling_manual.ts#L233-L235)）。
3. **需要观察的现象**：控制台打印的 `Response 1` 不是自然语言，而是类似 `<function>{"name": "get_current_temperature", "parameters": {"location": "Paris, France"}}</function>` 的字符串——这就是「模型没有执行任何函数，只是在写字符串」的直接证据。
4. **预期结果**：四轮请求依次输出「工具调用文本 → 工具结果回传 → 自然语言 → 再次工具调用」。若模型选择直接回答，输出就是普通文本（manual 方式无强制约束）。

#### 4.1.5 小练习与答案

**练习 1**：为什么说「函数调用的执行者永远是页面代码，而不是 WebLLM 引擎」？

**答案**：引擎只负责两件事——把 `tools` 渲染进 prompt、把模型输出的 JSON 字符串解析成 `tool_calls` 结构。从源码看，`engine.ts` 中与工具相关的代码全部位于请求前校验与响应后组装两处，中间的 `prefillStep`/`decodeStep` 管线完全没有「函数」「执行」的概念；执行函数、拼装 `role: "tool"` 消息都由调用方在自己的 JavaScript 里完成。

**练习 2**：`finish_reason: "tool_calls"` 与 `"stop"` 有什么关系？

**答案**：管线层只会产生 `stop`/`length`/`abort` 三种终止原因；引擎层发现「请求带 `tools` 且管线正常 stop」时，才把它改写为 `"tool_calls"`（见 4.4.3）。若因 `max_tokens` 用尽（`length`）或手动中断（`abort`）而停止，则保持原终止原因且不解析 `tool_calls`。

### 4.2 协议层注入：第 7 关卡的白名单与 Hermes 特化

#### 4.2.1 概念说明

`postInitAndCheckFields` 是 u6-l1 精读过的协议层校验函数。它的第 7 关专门处理函数调用，且和只做检查的其他关卡不同——这一关会**原位改写请求**：注入一条 system 消息、覆写 `response_format`。当前实现是**针对 Hermes-2-Pro / Hermes-3 的硬编码特化**，不是通用机制；能用 `tools` 字段的模型由白名单圈定。

#### 4.2.2 核心流程

```text
if tools 非空:
  ├─ 7.1 currentModelId ∈ functionCallingModelIds？ 否 → UnsupportedModelIdError
  ├─ 7.2 modelId 以 "Hermes-2-Pro-" 或 "Hermes-3-" 开头？
  │    ├─ 7.2.1 用户已设 response_format？ → CustomResponseFormatError
  │    │        否则覆写 response_format = { type:"json_object",
  │    │                                    schema: officialHermes2FunctionCallSchemaArray }
  │    └─ 7.2.2 hermes_tools 占位符 ← JSON.stringify(tools)
  │             messages 中已有 system 消息？ → CustomSystemPromptError
  │             否则 messages.unshift(渲染后的 system 消息)
  └─ 请求继续走普通 chatCompletion 路径
```

#### 4.2.3 源码精读

先看协议类型——`tools` 与 `tool_choice` 的形状完全对齐 OpenAI：

- [src/openai_api_protocols/chat_completion.ts:807-839](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L807-L839)：`FunctionDefinition` 用 `name`/`description`/`parameters`（JSON Schema 对象）描述一个函数；`ChatCompletionTool` 包装它并声明 `type: "function"`——目前工具类型只有函数一种。
- [src/openai_api_protocols/chat_completion.ts:845-877](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L845-L877)：`ChatCompletionToolChoiceOption` 是三态联合类型——字符串 `"none"`（不调用）/`"auto"`（模型自行决定），或一个 `ChatCompletionNamedToolChoice` 对象（强制指定某个函数名）。

再看第 7 关本体：

- [src/openai_api_protocols/chat_completion.ts:550-558](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L550-L558)：只要 `request.tools` 非空就检查模型白名单——**注意即使 `tool_choice: "none"` 也会检查**，因为判据是 `tools` 字段的存在而非取值；不在名单内即抛 `UnsupportedModelIdError`。
- [src/config.ts:340-346](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L340-L346)：`functionCallingModelIds` 白名单目前仅 5 个模型 ID，全部是 Hermes-2-Pro / Hermes-3 系（两种量化各若干）。
- [src/openai_api_protocols/chat_completion.ts:560-577](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L560-L577)：对 Hermes 系模型，先禁止用户自带 `response_format`（抛 `CustomResponseFormatError`），再覆写为 `json_object` + `officialHermes2FunctionCallSchemaArray`——这一步是「输出必为合法 JSON 数组」的保证，schema 会在管线里被 xgrammar 编译成采样约束（u6-l3 详讲）。
- [src/openai_api_protocols/chat_completion.ts:579-596](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L579-L596)：把模板串里的 `{hermes_tools}` 占位符替换为 `JSON.stringify(request.tools)`，确认用户没写 system 消息（否则 `CustomSystemPromptError`），最后 `unshift` 注入渲染好的 system 消息——之后 `getConversationFromChatCompletionRequest` 会把它写入 `override_system_message`，进入 u3-l2 讲过的 `system_template` 渲染链。

三个常量定义在 support.ts，内容直接抄自 NousResearch 官方指南：

- [src/support.ts:103-123](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L103-L123)：`officialHermes2FunctionCallSchema` 描述单次调用 `{arguments: object, name: string}`；`officialHermes2FunctionCallSchemaArray` 把它包成数组 schema（模型一次可以并发请求多个调用）；`hermes2FunctionCallingSystemPrompt` 是带 `{hermes_tools}` 占位符的完整系统提示词，指示模型把工具调用以 JSON 对象返回。占位符枚举见 [src/config.ts:58-65](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L58-L65)。

#### 4.2.4 代码实践

1. **实践目标**：用纯单测（无需 WebGPU）验证第 7 关的三条错误路径与一条改写路径。
2. **操作步骤**：仓库里已有现成测试，直接运行：
   ```bash
   npx jest openai_chat_completion
   ```
   然后阅读 [tests/openai_chat_completion.test.ts:339-484](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/openai_chat_completion.test.ts#L339-L484) 的 `OpenAI API function calling` 测试组：`Unsupported model`（L361）、`Should not specify response format`（L382）、`Should not specify system prompt`（L405/431）、`Check system prompt and response format post init`（L457——断言改写后的 `messages[0]` 与 `response_format`）。
3. **需要观察的现象**：四个用例全绿；L457 的用例里请求被原位改写——`messages[0].role` 变成 `system`，`response_format.type` 变成 `json_object`。
4. **预期结果**：jest 报告该文件全部通过。以上命令在无 GPU 环境可正常运行（纯 TypeScript 单测）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 WebLLM 要禁止用户在 Hermes 函数调用时自带 `response_format` 和 system 消息？

**答案**：因为第 7 关会**替用户设置**这两个字段——输出约束必须是官方 Hermes 调用 schema（否则模型产出的 JSON 无法被 `getToolCallFromOutputMessage` 按 `{name, arguments}` 解析），system 消息必须包含工具清单（否则模型根本不知道有哪些函数可用）。若允许用户覆盖，注入的 prompt 与解析器之间的隐式契约就会被打破，故干脆抛 `CustomResponseFormatError` / `CustomSystemPromptError` 拒绝。

**练习 2**：如果给 `Llama-3.1-8B-Instruct-q4f16_1-MLC` 发一个带 `tools` 的请求会发生什么？Llama-3.1 还能用函数调用吗？

**答案**：抛 `UnsupportedModelIdError`（白名单检查在先）。但 Llama-3.1 仍然**可以**做函数调用——走手动模板风格，即自己在 system 消息里写工具指令、自己解析 `<function>...</function>` 输出，`examples/function-calling/function-calling-manual` 的 `llama3_1_example()` 正是这么做的。

**练习 3**：`tool_choice: "none"` 且 `tools` 非空，请求会被白名单拦下吗？

**答案**：会。判据是 `request.tools !== undefined && request.tools !== null`（chat_completion.ts L551），与 `tool_choice` 取值无关——即使明确表示「不调用」，只要声明了工具就必须用支持函数调用的模型。

### 4.3 getFunctionCallUsage 与 tool_choice 校验：一个「设计通用、现状停用」的模块

#### 4.3.1 概念说明

`getFunctionCallUsage`（conversation.ts）是 `tool_choice` 校验逻辑的**唯一完整实现**：它把 `tools` + `tool_choice` 翻译成一段「函数清单 JSON 字符串」。它原本服务于 gorilla 类模型的通用函数调用——把清单渲染进对话模板的 `{function_string}` 占位符。但当前版本里，它在生产路径上的唯一调用点被注释掉了，只被单测引用。理解这个模块能学到一件重要的事：**读源码要区分「设计意图」与「运行现状」**。

#### 4.3.2 核心流程

```text
getFunctionCallUsage(request):
  tools 未定义，或 tool_choice == "none"        → 返回 ""（不启用函数调用）
  tool_choice 是字符串但不是 "auto"/"none"       → InvalidToolChoiceError
  tool_choice 是对象但 type !== "function"       → UnsupportedToolChoiceTypeError
  tool_choice 指定了函数名：
      在 tools 中按名匹配                        → 返回 '[该函数定义]'（单元素数组）
      找不到                                     → FunctionNotFoundError
  否则（"auto" 或未指定）:
      遍历 tools，任一 type !== "function"       → UnsupportedToolTypeError
      返回 '[全部函数定义]'
```

#### 4.3.3 源码精读

- [src/conversation.ts:528-540](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L528-L540)：入口两个快速分支——`tools` 未定义或 `tool_choice == "none"` 返回空串（等价于不启用）；`tool_choice` 是字符串却既非 `auto` 也非 `none`（例如手滑写成 `"bananna"`）抛 `InvalidToolChoiceError`。
- [src/conversation.ts:541-546](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L541-L546)：`tool_choice` 是对象（即强制指定函数）但 `type` 不是 `"function"` 时抛 `UnsupportedToolChoiceTypeError`。
- [src/conversation.ts:548-558](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L548-L558)：强制指定路径——按 `function.name` 在 `tools` 里找匹配，命中则返回**只含该函数**的单元素数组 JSON（这就是「强制」的实现：模型只看得到一个函数，自然只能调它）；找不到抛 `FunctionNotFoundError`。
- [src/conversation.ts:560-567](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L560-L567)：`auto` 路径——收集全部函数定义；任何一个工具的 `type` 不是 `"function"` 抛 `UnsupportedToolTypeError`。
- [src/conversation.ts:479-486](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L479-L486)：**关键现状证据**——`getConversationFromChatCompletionRequest` 里对 `getFunctionCallUsage` 的调用被整段注释，注释写明「曾用于支持 gorilla，但既无法用 grammar 保证其输出、也无法让它符合 OpenAI 的函数调用输出格式，暂且保留」。因此 Conversation 的 `function_string`/`use_function_calling` 字段在生产中永远是空串/false。
- [src/conversation.ts:47-48](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L47-L48) 与 [src/conversation.ts:156-168](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L156-L168)：这两个字段真正的消费点在 `getPromptArrayInternal`——若 `use_function_calling` 为真，`{function_string}` 占位符会被替换为函数清单 JSON；否则替换为空串。这是为「模板内函数注入」预留的通用插槽，与 4.2 的「system 消息注入」是两条平行的注入路线。

对应的错误类族集中在 error.ts：

- [src/error.ts:276-303](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L276-L303)：`InvalidToolChoiceError`、`UnsupportedToolChoiceTypeError`、`FunctionNotFoundError`、`UnsupportedToolTypeError` 四个类的定义，各自携带出错的取值便于排障。

单测对三种合法取值的行为做了精确断言：

- [tests/function_calling.test.ts:95-131](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/function_calling.test.ts#L95-L131)：`tool_choice: "none"` 返回空串。
- [tests/function_calling.test.ts:134-172](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/function_calling.test.ts#L134-L172)：`"auto"` 返回全部三个函数的 JSON 数组串。
- [tests/function_calling.test.ts:174-217](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/function_calling.test.ts#L174-L217)：强制指定 `fn_B` 时只返回 `fn_B` 一项。

#### 4.3.4 代码实践

1. **实践目标**：跑通 `tool_choice` 校验的现有单测，并补一个非法取值的用例。
2. **操作步骤**：
   ```bash
   npx jest function_calling
   ```
   然后仿照 [tests/function_calling.test.ts:95-131](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/function_calling.test.ts#L95-L131) 的写法，在本地分支新增一个测试：把 `tool_choice` 设为 `"bananna"`，断言 `expect(() => getFunctionCallUsage(request)).toThrow()`。
3. **需要观察的现象**：新用例抛出 `InvalidToolChoiceError`，错误信息中包含非法取值。
4. **预期结果**：`getFunctionCallUsage` 相关的 3 个旧用例 + 1 个新用例全部通过。该实践为纯单测，无需浏览器（新用例结果待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`tool_choice` 强制指定某函数时，「强制」是如何落到 prompt 层面的？

**答案**：`getFunctionCallUsage` 只返回 `[该函数]` 单元素数组——模型能看到的函数清单里只有这一个，自然只能调用它。这是「收窄可见选项」式的强制，而非在指令里写「你必须调用 X」。注意此路径当前未在生产中启用（见 4.3.3 的注释证据）。

**练习 2**：既然 `getFunctionCallUsage` 没被生产代码调用，为什么还要精读它？

**答案**：三点价值——① 它是 `tool_choice` 语义（none/auto/named）最完整的参照实现，五个错误类的触发条件都定义在这里；② 它揭示了被搁置的「模板内 `{function_string}` 注入」设计，与现行「system 消息注入」形成对照，帮助理解架构取舍（注释里写明原因：无法用 grammar 保证输出、无法对齐 OpenAI 格式）；③ WebLLM 版本迭代中它可能被重新启用或改造，读懂它等于读懂了扩展点。

**练习 3**：`Conversation.function_string` 与 `override_system_message` 分别对应哪条注入路线？

**答案**：`function_string` 对应「函数清单渲染进角色模板占位符 `{function_string}`」的路线（gorilla 风格，现状停用）；`override_system_message` 对应「函数清单写进 system 消息」的路线（Hermes 风格，第 7 关注入后生效）。两者最终都经 `getPromptArrayInternal` 进入模型 prompt，但插入位置不同：前者在每条带占位符的消息里，后者只在 system 段。

### 4.4 工具调用输出解析：getToolCallFromOutputMessage 与 ToolCall 结构化

#### 4.4.1 概念说明

生成结束后，模型的输出是一段纯文本（在 grammar 约束下是合法的 JSON 数组字符串，形如 `[{"arguments": {...}, "name": "..."}]`）。解析器 `getToolCallFromOutputMessage`（support.ts）负责把它变成 OpenAI 协议的 `tool_calls` 结构。它通过 `isStreaming` 布尔参数做成重载，产出两种形状：非流式的 `ChatCompletionMessageToolCall`（用字符串 `id`）与流式的 `Delta.ToolCall`（用数字 `index`）。

#### 4.4.2 核心流程

```text
引擎层（生成结束后）:
  finish_reason == "stop" 且请求带 tools？
    ├─ 否 → 不解析（length/abort 时 tool_calls 为 undefined，保留原始终止原因）
    └─ 是 → getToolCallFromOutputMessage(原始输出字符串)
         ├─ 1. JSON.parse 失败        → ToolCallOutputParseError
         ├─ 2. 不是数组               → ToolCallOutputInvalidTypeError
         ├─ 3. 任一项缺 name/arguments → ToolCallOutputMissingFieldsError
         ├─ 4. arguments 对象 → JSON.stringify 回字符串（协议要求 arguments 是 string）
         └─ 5. 按 isStreaming 组装 Delta.ToolCall(index) 或 MessageToolCall(id)
  finish_reason: "stop" → "tool_calls"
```

#### 4.4.3 源码精读

解析器本体四步走：

- [src/support.ts:139-156](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L139-L156)：函数重载签名（`isStreaming: false/true` 对应两种返回类型）之后，先 `JSON.parse` 整段输出——失败抛 `ToolCallOutputParseError`（携带原始字符串与底层错误）；再断言结果是数组，否则抛 `ToolCallOutputInvalidTypeError("array")`。
- [src/support.ts:158-173](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L158-L173)：逐项检查 `name` 与 `arguments` 字段存在性（缺失抛 `ToolCallOutputMissingFieldsError`），并把 `arguments` 从对象 `JSON.stringify` 回字符串——OpenAI 协议中 `function.arguments` 的类型就是字符串，这一步是「结构化对象 → 协议字符串」的序列化。
- [src/support.ts:176-205](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L176-L205)：按 `isStreaming` 分叉组装——流式产出 `{index, function, type}`，非流式产出 `{id, function, type}`，其中 `id` 就是循环下标的字符串形式（`"0"`、`"1"`…）。

协议类型的定义：

- [src/openai_api_protocols/chat_completion.ts:651-687](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L651-L687)：`ChatCompletionMessageToolCall`，JSDoc 特别说明 WebLLM 中 `id` 的语义是「本次生成的各次工具调用中的下标」，与 OpenAI 服务端生成的全局唯一 ID 不同。
- [src/openai_api_protocols/chat_completion.ts:1125-1160](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L1125-L1160)：流式 `Delta.ToolCall`，其 `function.arguments` 的注释诚实警告「模型不总生成合法 JSON，可能幻觉出不存在的参数，调用函数前请自行校验」——grammar 约束的是 JSON 语法合法，不保证参数语义正确。

引擎侧的两个挂载点（非流式与流式）：

- [src/engine.ts:874-908](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L874-L908)：非流式路径——`getFinishReason() === "stop"` 且带 `tools` 时，`finish_reason` 改写为 `"tool_calls"`，输出交给解析器；注意组装消息时**函数调用请求的 `content` 恒为 `null`**，工具调用信息只出现在 `tool_calls` 字段（若因 `length`/`abort` 停止，`tool_calls` 为 `undefined`，原始文本也拿不到了——这正是 L228-233 文档所述的行为）。
- [src/engine.ts:646-685](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L646-L685)：流式路径——生成期间的中间 chunk 照常携带原始字符串 `delta.content`（打字机效果里你看到的就是 JSON 文本逐字出现）；流结束前的最后一个 chunk 改为携带 `delta.tool_calls`，`finish_reason` 同样由 `stop` 改写为 `tool_calls`。解析失败时 `catch` 中先释放模型锁再抛错，请求以异常告终。

输出解析失败会抛的三个错误类：

- [src/error.ts:193-212](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L193-L212)：`ToolCallOutputParseError`（JSON 语法错）、`ToolCallOutputInvalidTypeError`（不是数组）、`ToolCallOutputMissingFieldsError`（缺 name/arguments）。

#### 4.4.4 代码实践

1. **实践目标**：在真实请求中观察「中间 chunk 是原始 JSON 字符串、最后 chunk 是结构化 tool_calls」的双阶段形态。
2. **操作步骤**：
   ```bash
   cd examples/function-calling/function-calling-openai
   npm install && npm start
   ```
   页面会加载 `Hermes-2-Pro-Llama-3-8B-q4f16_1-MLC`（约 4.6GB 显存需求，请确认显卡），控制台逐 chunk 打印。示例源码见 [examples/function-calling/function-calling-openai/src/function_calling_openai.ts:42-77](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/function-calling/function-calling-openai/src/function_calling_openai.ts#L42-L77)。
3. **需要观察的现象**：页面 `generate-label` 里 JSON 文本逐字浮现（`delta.content` 累积）；循环结束后控制台打印的 `lastChunk.choices[0].delta` 里 `content` 为空、`tool_calls` 是形如 `[{index: 0, function: {name: "get_current_weather", arguments: "{...}"}}]` 的数组；`finish_reason` 为 `"tool_calls"`。
4. **预期结果**：对「What is the current weather in celsius in Pittsburgh and Tokyo?」这类双城市问题，Hermes 通常返回**两个** tool_calls（index 0 和 1）——因为输出 schema 是数组。具体调用次数取决于模型，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `arguments` 在解析器里要被 `JSON.stringify` 回字符串？

**答案**：OpenAI 协议规定 `tool_calls[i].function.arguments` 的类型是字符串（JSON 文本），以便不同语言的处理方自行反序列化。模型输出经 `JSON.parse` 得到 `arguments` 对象后，组装协议响应时必须再序列化回字符串才能类型对齐。

**练习 2**：函数调用请求因 `max_tokens` 用尽而停止时，响应长什么样？

**答案**：`finish_reason` 保持 `"length"`，`tool_calls` 为 `undefined`；非流式下 `message.content` 仍是 `null`（因为组装分支只看 `isFunctionCalling` 布尔）——也就是说截断的半截 JSON 原文在这条路径上拿不到，只能靠流式路径的中间 chunk 保留。这是使用 `max_tokens` 做函数调用时要注意的坑。

**练习 3**：如果模型输出了合法 JSON 但某个元素缺 `name` 字段，会发生什么？

**答案**：`getToolCallFromOutputMessage` 第 3 步抛 `ToolCallOutputMissingFieldsError`；在引擎的两个挂载点，该异常会在释放模型锁之后向上抛出，整条请求以 rejected promise 告终。不过在第 7 关覆写的 grammar 约束下，`required: ["arguments", "name"]` 已在采样阶段排除了这种输出。

### 4.5 tool 消息回传：多轮工具对话的闭环

#### 4.5.1 概念说明

拿到 `tool_calls` 后，调用方执行函数，然后把结果包装成 `role: "tool"` 消息追加进 `messages` 再次请求——模型据此生成最终回复。WebLLM 对 `tool` 角色的处理非常「薄」：`tool_call_id` **不进入 prompt**（只供调用方自己对账），消息内容按普通文本进入会话历史的 `Role.tool` 槽位。同时，`tool` 消息也被允许作为对话的最后一条（这是工具回传场景的必然要求）。

#### 4.5.2 核心流程

```text
调用方: messages = [system, user, assistant(工具调用), tool(执行结果)]
                                │
引擎: postInitAndCheckFields 关卡3: 最后一条消息必须是 user 或 tool ── tool 合法 ✔
                                │
      getConversationFromChatCompletionRequest:
        system    → override_system_message
        assistant → appendMessage(Role.assistant, content)
        tool      → appendMessage(Role.tool, content)   # tool_call_id 被忽略
        （最后一条消息不进 Conversation，直接作为本轮 prefill 输入）
                                │
      compareConversationObject(旧, 新) 命中前缀 → 复用 KV cache，只 prefill 增量
                                │
      最后一条消息 role == "tool" → 以 Role.tool 追加编码
```

#### 4.5.3 源码精读

- [src/openai_api_protocols/chat_completion.ts:771-786](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L771-L786)：`ChatCompletionToolMessageParam` 协议类型——`content` 加 `tool_call_id`（语义为「本消息回应哪次调用」，与 assistant 消息里的 `tool_calls[i].id` 对应）。
- [src/openai_api_protocols/chat_completion.ts:479-488](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L479-L488)：第 3 关要求最后一条消息来自 `user` **或 `tool`**——`tool` 被显式列入合法尾部角色，就是为「函数执行完回传结果」的场景开的口子。
- [src/conversation.ts:497-517](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L497-L517)：`getConversationFromChatCompletionRequest` 的角色分发循环——`system` 写入 `override_system_message`（u3-l2 讲过的机制，Hermes 注入的工具指令 system 消息由此进入 prompt）；`tool` 分支只取 `message.content` 调 `appendMessage(Role.tool, ...)`，**`tool_call_id` 在此处被丢弃**；未知角色抛 `UnsupportedRoleError`。注意默认不包含最后一条消息（它作为本轮 prefill 输入单独处理）。
- [src/engine.ts:1399-1406](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1399-L1406)：最后一条消息的角色映射——`last_msg.role === "tool" ? Role.tool : Role.user`，工具结果由此以 `Role.tool` 的对话槽位（Hermes 模板下渲染成 `<|im_start|>tool\n...`，Llama-3.1 下渲染成 `ipython` 角色）进入 prefill。
- [tests/function_calling.test.ts:262-303](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/function_calling.test.ts#L262-L303)：Hermes2 多轮工具对话的**格式对拍测试**——输入含 `assistant` 的 `<tool_call>` 文本与 `tool` 的 `<tool_response>` 文本，断言 `getPromptArray()` 拼出的完整 prompt 与官方格式逐字符一致（L294-302 的期望串里能看到 `<|im_start|>tool\n<tool_response>...` 段落）。同文件 [L306-414](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/function_calling.test.ts#L306-L414) 是 Llama-3.1 版对拍，工具结果渲染为 `<|start_header_id|>ipython<|end_header_id|>` 段落。
- 多轮复用：`compareConversationObject`（[src/conversation.ts:382-397](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L382-L397)）参与相等判定的字段包括 `function_string`、`use_function_calling`、`override_system_message` 与逐条消息——工具对话第二轮只是追加了消息前缀，命中即可复用 KV cache（u2-l2 的多轮机制原样生效）。

手动风格的完整回传范例（每一步都在页面代码里，引擎无感知）：

- [examples/function-calling/function-calling-manual/src/function_calling_manual.ts:44-65](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/function-calling/function-calling-manual/src/function_calling_manual.ts#L44-L65)：hermes2 示例的闭环三步——① 把第一轮的原始输出（`<tool_call>...` 文本）作为 `assistant` 消息推回 `messages`；② 页面自己「执行」函数得到 `tool_response` 文本（示例里是硬编码的假数据）并以 `role: "tool"` + `tool_call_id: "0"` 推回；③ 再次请求获得自然语言回复。Llama-3.1 版的四轮版本见 [L164-231](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/function-calling/function-calling-manual/src/function_calling_manual.ts#L164-L231)。

#### 4.5.4 代码实践

1. **实践目标**：亲手完成一次「解析 tool_calls → 执行 → 回传 → 拿到最终回复」的闭环。
2. **操作步骤**：复制 `function-calling-openai` 示例到自己的实验目录，在拿到最后一个 chunk 后：
   ```typescript
   // 示例代码：接在 for-await 循环之后
   const calls = lastChunk!.choices[0].delta.tool_calls ?? [];
   const messages: webllm.ChatCompletionMessageParam[] = [
     { role: "user", content: "What is the current weather in celsius in Pittsburgh and Tokyo?" },
     { role: "assistant", content: null, tool_calls: calls as any },
   ];
   for (const c of calls) {
     const args = JSON.parse(c.function!.arguments!);
     const result = args.location.includes("Pittsburgh") ? "18C" : "26C"; // 你的"函数"
     messages.push({ role: "tool", tool_call_id: String(c.index), content: result });
   }
   const reply = await engine.chat.completions.create({
     messages, tools, stream: false,
   } as webllm.ChatCompletionRequest);
   console.log(reply.choices[0].message.content);
   ```
3. **需要观察的现象**：第二次请求返回的是整合了两地温度的自然语言；控制台 INFO 日志出现 `Multiround chatting, reuse KVCache.`（引擎判定为多轮对话前缀命中）。
4. **预期结果**：回复中同时提到 Pittsburgh 与 Tokyo 的温度。若两次调用都走完整重新 prefill，检查是否 accidentally 修改了历史消息——任何篡改都会使 `compareConversationObject` 失配而清空 KV cache。运行结果待本地验证（依赖真实模型加载）。

#### 4.5.5 小练习与答案

**练习 1**：`tool_call_id` 在 WebLLM 中到底起了什么作用？

**答案**：在协议层它是 `ChatCompletionToolMessageParam` 的必填字段，语义上用于把工具结果与某次调用配对；但在 WebLLM 的 prompt 渲染中它**被丢弃**（conversation.ts 的 tool 分支只取 content）。真正消费它的是调用方自己的代码——当你并发执行多个工具调用时，靠 id 把结果对应回正确的调用。

**练习 2**：工具对话的第二轮请求为什么能复用第一轮的 KV cache？

**答案**：第二轮的 `messages` 是第一轮的严格前缀扩展（多了 assistant 工具调用与 tool 结果）。`getConversationFromChatCompletionRequest` 默认排除最后一条消息构造 Conversation，其消息序列与引擎内已保存的旧 Conversation 完全一致（`compareConversationObject` 逐条深比通过），引擎因此只 prefill 新增部分。

**练习 3**：Hermes 与 Llama-3.1 的工具结果在 prompt 中分别渲染成什么角色段？

**答案**：Hermes 的对话模板定义了 `tool: "<|im_start|>tool"` 角色，结果渲染为 `<|im_start|>tool\n<tool_response>...` 段；Llama-3.1 模板把 tool 映射为 `ipython` 角色，渲染为 `<|start_header_id|>ipython<|end_header_id|>` 段。两个测试文件的对拍期望串（function_calling.test.ts L294-302 与 L411）分别固化了这两种格式。

## 5. 综合实践：两种函数调用方式的 prompt 对拍

**任务**：用同一组工具定义，分别走 manual 与 OpenAI 两条路，比对它们最终生成的 prompt 差异，并定位各自的「工具 schema 渲染进 system prompt」的源码位置。

### 步骤一：运行两个子示例（浏览器，需 WebGPU）

```bash
cd examples/function-calling/function-calling-openai && npm install && npm start
# 换一个终端
cd examples/function-calling/function-calling-manual  && npm install && npm start
# 两个都是 8888 端口，分别访问、分别观察
```

把 `function_calling_openai.ts` 的 `tools`（[L21-40](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/function-calling/function-calling-openai/src/function_calling_openai.ts#L21-L40)，`get_current_weather`）抄进 manual 示例的 system prompt 文本里（改写 `llama3_1_example` 的 `system_prompt`，把原来的温度/发消息函数换成它），让两边用**同一组工具定义**回答同一个问题「What is the current weather in Pittsburgh?」。

观察：manual 的控制台输出是 `<function>{"name": ..., "parameters": ...}</function>` 这样的**原始字符串**（你要自己正则/JSON 解析）；openai 的最后 chunk 是 `tool_calls` **结构化数组**、`arguments` 已是合法 JSON 字符串。记录两者：输出是否需要你解析、格式错误是否可能发生、`finish_reason` 各是什么。

### 步骤二：定位渲染代码（源码阅读）

在源码中找出「工具 schema 进入 system prompt」的两条路径，你会发现一个关键事实——**OpenAI 风格的注入不发生在 conversation.ts**：

1. **OpenAI 风格**：渲染发生在协议层 [src/openai_api_protocols/chat_completion.ts:579-596](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L579-L596)——`hermes2FunctionCallingSystemPrompt` 的 `{hermes_tools}` 占位符被 `JSON.stringify(request.tools)` 替换后 `unshift` 进 `messages`；conversation.ts 只是在 [L499-503](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L499-L503) 把这条 system 消息写入 `override_system_message`，再经 [L78-87](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L78-L87) 套进 `system_template`。conversation.ts 里真正与「工具渲染」相关的另一处是 [L156-168](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L156-L168) 的 `{function_string}` 占位符——那是 4.3 讲的停用路线。
2. **Manual 风格**：没有引擎侧渲染，system prompt 就是你手写的字符串，同样经 `override_system_message` → `system_template` 进入 prompt。

### 步骤三：用单测对拍两种 prompt（无需 WebGPU，可直接验证）

写一个 jest 测试（示例代码，放 `tests/` 下本地分支运行）：

```typescript
// 示例代码：tests/my_fc_compare.test.ts
import { postInitAndCheckFields, ChatCompletionRequest } from "../src/openai_api_protocols/chat_completion";
import { getConversationFromChatCompletionRequest } from "../src/conversation";
import { ModelType } from "../src/config";
import { hermes2LlamaChatConfig } from "./hermes2_fixture"; // 自建夹具，见下方说明

test("compare manual vs openai function calling prompts", () => {
  const tools = [{ type: "function", function: { name: "get_current_weather",
    description: "Get weather", parameters: { type: "object",
      properties: { location: { type: "string" } }, required: ["location"] } } }];

  // 路线A：OpenAI 风格 —— 协议层自动注入 system
  const reqA: ChatCompletionRequest = {
    messages: [{ role: "user", content: "Weather in Pittsburgh?" }], tools,
  };
  postInitAndCheckFields(reqA, "Hermes-2-Pro-Llama-3-8B-q4f16_1-MLC", ModelType.LLM);
  const convA = getConversationFromChatCompletionRequest(reqA, hermes2LlamaChatConfig, true);
  console.log("A(openai):", convA.getPromptArray()[0]);

  // 路线B：manual —— system 是你自己写的
  const reqB: ChatCompletionRequest = {
    messages: [
      { role: "system", content: `You have tools: ${JSON.stringify(tools)}. Reply with <function>{...}</function>.` },
      { role: "user", content: "Weather in Pittsburgh?" },
    ],
  };
  const convB = getConversationFromChatCompletionRequest(reqB, hermes2LlamaChatConfig, true);
  console.log("B(manual):", convB.getPromptArray()[0]);
});
```

运行 `npx jest my_fc_compare`，对照两行 console 输出写结论。夹具说明：仓库中没有现成的 Hermes2 ChatConfig 导出——把 [tests/function_calling.test.ts:221-259](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/function_calling.test.ts#L221-L259) 内联定义的 `hermes2LlamaChatConfig`（含 `tool: "<|im_start|>tool"` 角色）复制到你的测试文件中即可；图省事也可以直接用 `tests/constants` 导出的 `llama3_1ChatConfig`，但 manual 分支的 system 指令就要相应换成 Llama-3.1 的 `<function>` 格式。

### 预期结论（供你核对）

- 两种方式的工具清单**都出现在 system 消息里、位置相同**（都经 `override_system_message` → `system_template`）。
- 差异一：**来源**——A 的 system 由 `hermes2FunctionCallingSystemPrompt` 模板渲染，措辞与 schema 展示格式固定；B 完全由你掌控，可以针对模型微调（例如 Llama-3.1 需要写 `<function>` 指令，Hermes 需要 `<tool_call>` 指令）。
- 差异二：**输出保证**——A 被第 7 关强制覆写 `response_format = json_object + schema`，解码时受 grammar 约束，输出必为可解析的 JSON 数组；B 无任何约束，输出格式靠模型遵循指令，解析代码需要容错。
- 差异三：**响应形态**——A 的 `content` 为 `null`、`tool_calls` 结构化、`finish_reason: "tool_calls"`；B 的 `content` 是原始文本、`finish_reason: "stop"`，解析是你的责任。
- 差异四：**适用模型**——A 仅限 `functionCallingModelIds` 白名单（Hermes 系）；B 任何模型（manual 示例就同时演示了 Hermes 与 Llama-3.1）。

## 6. 本讲小结

- 函数调用 = 输入侧 prompt 注入 + 输出侧文本解析；模型从不执行函数，执行永远在页面代码里，推理管线（prefill/decode）对工具完全无感知。
- OpenAI 风格的注入发生在协议层 `postInitAndCheckFields` 第 7 关：白名单检查（`functionCallingModelIds`，仅 5 个 Hermes 系模型 ID）→ 覆写 `response_format` 为 `json_object + officialHermes2FunctionCallSchemaArray` → 渲染 `{hermes_tools}` 占位符并 `unshift` system 消息；用户自带的 `response_format` 与 system 消息会被 `CustomResponseFormatError` / `CustomSystemPromptError` 拒绝。
- `getFunctionCallUsage`（conversation.ts）是 `tool_choice` 校验的完整实现（none→空串、auto→全量清单、named→单函数清单，配四个专属错误类），但它服务于 gorilla 风格的 `{function_string}` 占位符注入路线，生产调用点当前被注释停用，仅剩单测引用——读源码要区分设计意图与运行现状。
- 输出解析器 `getToolCallFromOutputMessage` 四步走：JSON.parse → 数组断言 → name/arguments 字段检查 → 按流式/非流式组装 `Delta.ToolCall`（数字 index）或 `MessageToolCall`（字符串 id）；`arguments` 会被序列化回字符串以对齐 OpenAI 协议。
- 引擎只在 `finish_reason === "stop"` 且带 `tools` 时解析并把终止原因改写为 `"tool_calls"`；`length`/`abort` 下不解析；流式的中间 chunk 携带原始 JSON 文本、最后一个 chunk 携带 `tool_calls`。
- `role: "tool"` 消息被允许作为对话最后一条；其 `tool_call_id` 不进 prompt（仅调用方对账用），内容经 `Role.tool` 槽位渲染（Hermes 为 `<|im_start|>tool`、Llama-3.1 为 `ipython`）；工具对话第二轮天然命中多轮 KV cache 复用。

## 7. 下一步学习建议

- **下一讲 u6-l3《JSON Mode 与结构化输出（xgrammar）》**：本讲反复出现的 `response_format = {type: "json_object", schema}` 正是由 xgrammar 的 `GrammarMatcher` 在采样阶段逐 token 落实的——下一讲深入 grammar 编译发生在哪个阶段、掩码如何作用于 logits，以及 `responseFormatCacheKey` 的性能优化。
- **顺延阅读 u6-l4《Structural Tag 工具调用》**：把「自由文本区」与「受约束 JSON 区」分段的新方案，可视为对本讲 Hermes 硬编码路线的性能改进，读完两讲对比体会演进脉络。
- **源码延伸**：通读 [src/support.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts)（解析器与 Hermes 常量）与 [tests/function_calling.test.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/function_calling.test.ts)（两种模型的多轮格式对拍），尝试为 `getFunctionCallUsage` 的 named 路径补一个 `FunctionNotFoundError` 用例。
- **应用延伸**：基于综合实践的闭环代码，把硬编码的「函数」换成真实的 `fetch` 调用（如公开天气 API），你就得到了一个纯浏览器端、无服务器的工具调用 Agent 雏形。
