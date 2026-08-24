# 讲义 u2-l4：completion 文本补全接口

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `completion` 与 `chatCompletion` 的本质区别：前者是「裸文本续写」，后者是「套对话模板的多轮会话」。
2. 读懂 `Completions` 门面类如何把 `engine.completions.create()` 转发到 `engine.completion()`。
3. 掌握 `CompletionCreateParams` 的字段体系，以及 `postInitAndCheckFields` 会抛出的四类错误。
4. 理解文本补全的「prompt 直通编码」路径：为什么不经过任何对话模板、为什么要求 KV cache 为空（`TextCompletionExpectsKVEmptyError`），以及这个错误在公共 API 下其实是一道「防御性闸门」。
5. 分析 `Completion` 响应的结构：`choices` 数组、`echo` 回显、`usage.extra` 性能块是如何在 `engine.completion()` 里组装出来的。

## 2. 前置知识

### 2.1 基础模型（base model）与指令模型（instruct model）

- **基础模型**：只做过「预测下一个词」的预训练，例如 `Llama-3.1-8B`。它的天性是**续写**——你给它 "List 3 US states: "，它接着往下写。
- **指令模型**：在基础模型之上做了对话微调（模型名常带 `-Instruct`），学会了 `<|system|>`、`<|user|>` 这类特殊标签的含义，能一问一答。

在 WebLLM 的 `prebuiltAppConfig.model_list` 里，绝大多数预置模型是指令模型。本讲的示例特意用一个**基础模型** `Llama-3.1-8B-q4f32_1-MLC` 来演示补全接口——这正是 `completion` 接口的主场。

### 2.2 KV cache 与「会话状态」（回顾 u2-l2）

模型逐 token 生成时，会把每个 token 的中间结果写进 **KV cache**，下一步只前向最新一个 token。只要对话历史没变，旧 token 的 KV 可以复用，这就是多轮对话不用重算全部 prompt 的原因。`pipeline.filledKVCacheLength` 记录了当前 cache 里已经填了多少 token——它是本讲最关键的一个状态变量。

### 2.3 可辨识联合（回顾 u2-l3）

`stream: true` / `stream?: false` 让 TypeScript 能在类型层面区分流式与非流式请求，`completion` 与 `chatCompletion` 用的是同一套技巧。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/openai_api_protocols/completion.ts` | 补全协议的全部定义：`Completions` 门面类、`CompletionCreateParams` 请求类型、`Completion` 响应类型、`postInitAndCheckFields` 校验函数 |
| `src/engine.ts` | `engine.completion()` 主流程（第 975–1102 行）、`prefill()` 中的文本补全分支（第 1379–1417 行）、流式 chunk 组装 |
| `src/conversation.ts` | `Conversation` 类的 `isTextCompletion` 模式：`getPromptArrayTextCompletion()` 与一族「文本补全下禁止调用」的守卫方法 |
| `src/llm_chat.ts` | `LLMChatPipeline.prefillStep()` 对文本补全的分支处理、`getInputData()` 中的 KV cache 空检查、`resetChat()` |
| `src/error.ts` | `TextCompletionExpectsKVEmptyError`、`TextCompletionConversationError` 等错误类定义 |
| `examples/text-completion/src/text_completion.ts` | 官方最小示例：自定义 appConfig 加载基础模型并发起补全请求 |

## 4. 核心概念与源码讲解

### 4.1 Completions 门面类与 engine.completion 入口

#### 4.1.1 概念说明

WebLLM 对外提供两套「长得像 OpenAI SDK」的调用方式：

```ts
// 方式一：OpenAI 风格门面
const reply = await engine.completions.create({ prompt: "..." });
// 方式二：直接调引擎方法
const reply = await engine.completion({ prompt: "..." });
```

两者**完全等价**。`Completions` 是一个典型的门面（Facade）类：它只持有一个 `MLCEngineInterface` 引用，`create()` 原封不动地把请求转发给 `engine.completion()`。它的存在意义是兼容那些已经在用 OpenAI SDK 写法、想把 `baseURL` 换成本地引擎的代码——业务代码几乎不用改。

#### 4.1.2 核心流程

```text
用户代码
  └─ engine.completions.create(request)     ← OpenAI 风格门面
       └─ this.engine.completion(request)   ← 真正的实现（同一份）
            ├─ getLLMStates()               ← 按 request.model 路由到已加载的管线
            ├─ postInitAndCheckFields()     ← 字段校验（见 4.2）
            ├─ 组装 GenerationConfig
            ├─ 获取模型互斥锁
            ├─ stream=true → asyncGenerate()（流式，本讲略，机制同 u2-l3）
            └─ stream=false → 循环 n 次 _generate()
                                ├─ prefill()  ← 文本补全分支在这里（见 4.3）
                                └─ decode() 循环
```

#### 4.1.3 源码精读

**门面类的全部实现**——注意 `create()` 的三个重载签名只是做类型收窄，函数体只有一行：

[src/openai_api_protocols/completion.ts:32-51](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/completion.ts#L32-L51) —— `Completions` 类：构造时保存引擎引用；两个重载 `create()` 分别声明非流式返回 `Promise<Completion>`、流式返回 `Promise<AsyncIterable<Completion>>`；实现体直接 `return this.engine.completion(request)`。

门面在引擎构造函数里被实例化：

[src/engine.ts:119-120](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L119-L120) —— `MLCEngine` 声明 `public chat` 与 `public completions` 两个门面属性（前者服务 `chatCompletion`，后者服务本讲接口）。

[src/engine.ts:164](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L164) —— 构造函数中 `this.completions = new API.Completions(this)`，把引擎自身注入门面。

`engine.completion()` 的入口签名（重载方式与门面完全对称）：

[src/engine.ts:975-986](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L975-L986) —— `completion()` 的三个重载；注意第 968 行的文档注释明确说明：**每个 choice（即 `n`）由一次 `prefill()` 加多次 `decode()` 组成**，这决定了 `seed` 等字段的行为。

[src/engine.ts:1199-1208](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1199-L1208) —— `getLLMStates()`：若引擎只加载了一个模型，`request.model` 可省略；加载了多个模型则必填，否则无法路由。

#### 4.1.4 代码实践

**实践目标**：验证「门面即转发」，两条调用路径产生等价的结果。

**操作步骤**（基于 `examples/text-completion`，先跑通见第 5 节综合实践）：

1. 打开 `examples/text-completion/src/text_completion.ts`，在 `reply0` 之后追加：

```ts
// —— 示例代码：追加到 text_completion.ts 的 main() 里 ——
const reply1 = await engine.completion({
  prompt: "List 3 US states: ",
  max_tokens: 64,
  seed: 42,
});
const reply2 = await engine.completions.create({
  prompt: "List 3 US states: ",
  max_tokens: 64,
  seed: 42,
});
console.log("direct   :", JSON.stringify(reply1.choices[0].text));
console.log("facade   :", JSON.stringify(reply2.choices[0].text));
```

2. `npm start` 后打开浏览器控制台查看两行输出。

**需要观察的现象**：两次调用的 `text` 完全一致（同一 seed、同一参数下采样路径相同）；两次响应的 `object` 字段都是 `"text_completion"`。

**预期结果**：输出一致，证明 `completions.create` 只是 `engine.completion` 的别名层。（采样随机性由 seed 固定；若两个模型实例或参数有差异则会不一致——请保持参数完全相同。）

### 4.2 CompletionCreateParams：字段与 postInitAndCheckFields 校验

#### 4.2.1 概念说明

`CompletionCreateParams` 是补全请求的类型，直接「采用自 openai-node 并做了小改动」。与 `ChatCompletionRequest` 相比，最重要的差异是：

| 维度 | chatCompletion | completion |
| --- | --- | --- |
| 必填输入 | `messages`（结构化消息数组） | `prompt`（一个裸字符串） |
| 对话模板 | 按 `conv_template` 包裹 system/user/assistant 标签 | **完全不用模板**，字符串原样送入 tokenizer |
| 多轮 | 靠追加 `messages` 历史、复用 KV cache | 每次调用都从头开始（见 4.3） |
| 工具/结构化输出 | 支持 `tools`、`response_format` | 不支持（带 `tools` 会直接报错） |

请求里的采样参数（`temperature`、`top_p`、`max_tokens`、`stop`、各类 penalty、`logit_bias`、`seed` 等）与 chat 接口语义一致；WebLLM 还额外支持 OpenAI 没有的 `repetition_penalty`、`ignore_eos` 与 `extra_body.enable_latency_breakdown`。

#### 4.2.2 核心流程

校验函数 `postInitAndCheckFields` 在引擎拿到管线之后、生成之前执行，共四道检查，按顺序：

```text
1. 未支持字段检查：suffix / user / best_of 出现在请求里 → UnsupportedFieldsError
2. 流式数量检查：stream=true 且 n>1                → StreamingCountError
3. seed 类型检查：seed 非整数                       → SeedTypeError
4. stream_options 检查：设了 stream_options 但没开流 → InvalidStreamOptionsError
（数值范围校验不在这一层，而在 postInitAndCheckGenerationConfigValues，u3-l5 详讲）
```

#### 4.2.3 源码精读

**请求类型的骨架**——唯一必填字段就是 `prompt`：

[src/openai_api_protocols/completion.ts:62-71](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/completion.ts#L62-L71) —— `CompletionCreateParamsBase` 以 `prompt: string` 开头；类型注释明确写着「model 被排除在外，请先 `CreateMLCEngine(model)` 或 `engine.reload(model)`」。

[src/openai_api_protocols/completion.ts:199-206](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/completion.ts#L199-L206) —— `model` 是**可选**字段：只有一个模型加载时可省略，多模型加载时必填。

[src/openai_api_protocols/completion.ts:208-245](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/completion.ts#L208-L245) —— 文件里专门划出「BELOW FIELDS NOT SUPPORTED YET」区块声明 `suffix`、`user`、`best_of` 三个不支持的字段；随后是 WebLLM 特有的 `extra_body.enable_latency_breakdown`。

**流式/非流式的类型区分**（可辨识联合）：

[src/openai_api_protocols/completion.ts:248-266](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/completion.ts#L248-L266) —— `CompletionCreateParams` 是两个子类型的联合：`stream?: false | null` 对应非流式（返回 `Promise<Completion>`），`stream: true` 对应流式（返回 `Promise<AsyncIterable<Completion>>`）。

**校验函数本体**：

[src/openai_api_protocols/completion.ts:335-339](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/completion.ts#L335-L339) —— 不支持字段名单以常量数组形式集中维护，`suffix`、`user`、`best_of`。

[src/openai_api_protocols/completion.ts:347-381](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/completion.ts#L347-L381) —— `postInitAndCheckFields()` 依次完成上面流程图里的四道检查。注意第 349–350 行：参数 `currentModelId` 目前尚未使用（有一行 eslint-disable 注释压制未使用告警），属于为将来「按模型校验」预留的钩子。

**引擎侧把请求摘入 GenerationConfig**：

[src/engine.ts:989-1005](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L989-L1005) —— `completion()` 先 `getLLMStates` 路由模型、再调 `API.postInitAndCheckFieldsCompletion` 校验，然后把请求里的采样参数逐字段抄进 `GenerationConfig`。与 chat 路径相比，这里**没有** `response_format`、`tools` 等字段——补全接口根本不携带它们。

**流式路径下对 tools 的硬拒绝**（补全接口不支持函数调用的实证）：

[src/engine.ts:514-524](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L514-L524) —— `asyncGenerate()` 用 `"messages" in request` 判断请求类型；若请求带 `tools` 却不是 chat 请求，直接抛 `"Expect chat.completions with tools, not completions."`。

#### 4.2.4 代码实践

**实践目标**：亲手触发四类校验错误，把错误信息与源码中的抛出点一一对应。

**操作步骤**：在示例页面控制台（或改代码）依次执行：

```ts
// —— 示例代码 ——
// 1. 未支持字段
try {
  await engine.completion({ prompt: "hi", suffix: "!" } as any);
} catch (e) { console.log("1:", e.name, e.message); }

// 2. 流式 + n>1
try {
  await engine.completion({ prompt: "hi", stream: true, n: 2 });
} catch (e) { console.log("2:", e.name, e.message); }

// 3. 非整数 seed
try {
  await engine.completion({ prompt: "hi", seed: 1.5 });
} catch (e) { console.log("3:", e.name, e.message); }

// 4. 设了 stream_options 但没开流
try {
  await engine.completion({
    prompt: "hi",
    stream_options: { include_usage: true },
  } as any);
} catch (e) { console.log("4:", e.name, e.message); }
```

**需要观察的现象**：四条 catch 各自打印的错误名与信息。

**预期结果**：

1. `UnsupportedFieldsError`（信息里会点名 `suffix` 与 `CompletionCreateParams`）；
2. `StreamingCountError`；
3. `SeedTypeError`；
4. `InvalidStreamOptionsError`。

对照 [completion.ts:347-381](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/completion.ts#L347-L381) 即可确认每条错误来自哪一行。（错误是否在第 2、3、4 项的流式分支之前抛出取决于执行顺序：校验在 `engine.completion` 第 992 行同步执行，先于任何生成。）

### 4.3 prompt 直通编码与「KV cache 必须为空」约束

#### 4.3.1 概念说明

这是本讲最核心的一块。`chatCompletion` 与 `completion` 最终都汇入同一个 `engine.prefill()`，引擎靠一个聪明的判别式区分两者：

```ts
if ("messages" in input) { /* chat 路径 */ } else { /* 文本补全路径 */ }
```

文本补全路径做三件事：

1. 把 `request.prompt` 直接当作输入字符串（**不套任何对话模板**）；
2. **无条件调用 `pipeline.resetChat()`**——清空对话状态、重置 KV cache、`filledKVCacheLength = 0`；
3. 构造一个 `isTextCompletion = true` 的特殊 `Conversation` 对象挂到管线上。

`Conversation` 类内部用 `isTextCompletion` 这个开关把自己切换成「哑模式」：`prompt` 原样存取，而 `appendMessage`、`getPromptArray` 等所有「会话式」方法全部变成守卫，一调用就抛 `TextCompletionConversationError`。

那 `TextCompletionExpectsKVEmptyError` 呢？它在 `pipeline.getInputData()` 里：如果当前是文本补全模式、但 `filledKVCacheLength !== 0`，就抛错。**结合上面的第 2 步你会发现：经由公共 API 调用时，resetChat 永远发生在检查之前，所以这个错误正常情况下不会触发**——它是管线内部的一道「不变量断言」（invariant check）：文本补全没有「多轮续接」的概念，如果哪天有人在 KV cache 非空时直接对文本补全会话做 prefill（例如绕过引擎直接调 `prefillStep`，或未来改动破坏了重置逻辑），宁可立刻报错也不能静默地在旧对话的 KV 之上续写裸文本——那会产出语义完全错乱的结果。

> 用一句话总结：**chat 接口把 KV cache 当资产（能复用就复用），completion 接口把 KV cache 当垃圾（每次全清）。** 清理动作在 engine 层完成，`TextCompletionExpectsKVEmptyError` 只是管线层对「没清干净」的保险丝。

#### 4.3.2 核心流程

```text
engine.prefill(input, pipeline, chatConfig, genConfig)
  │
  ├─ "messages" in input ?
  │    ├─ 是 → chat 路径：比对新旧 Conversation，
  │    │        相同则「Multiround chatting, reuse KVCache」（复用）
  │    └─ 否 → completion 路径：
  │         ① input_str = input.prompt            ← 裸字符串直通
  │         ② pipeline.resetChat()                ← 清 conversation + KV cache
  │         │     └─ filledKVCacheLength = 0
  │         ③ newConv = getConversation(conv_template, conv_config, true)
  │         │                                          ↑ isTextCompletion = true
  │         └─ pipeline.setConversation(newConv)
  │
  └─ pipeline.prefillStep(input_str, ...)
       ├─ conversation.isTextCompletion == true
       │     → conversation.prompt = inp          ← 不调 appendMessage
       └─ getInputData()
            ├─ isTextCompletion && filledKVCacheLength !== 0
            │     → throw TextCompletionExpectsKVEmptyError   ← 保险丝
            └─ prompts = getPromptArrayTextCompletion()  → [this.prompt]
```

#### 4.3.3 源码精读

**分岔点**——一个 `in` 运算符决定走哪条路：

[src/engine.ts:1379-1417](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1379-L1417) —— `prefill()` 内部：`"messages" in input` 为真时走 chat 路径（比对 `compareConversationObject(oldConv, newConv)`，不同则 `resetChat()`，相同则打日志 `Multiround chatting, reuse KVCache.`）；第 1407–1417 行的 `else` 分支即文本补全路径——`input_str = input.prompt`、`pipeline.resetChat()`、`getConversation(chatConfig.conv_template, chatConfig.conv_config, true)`（第三个参数就是 `isTextCompletion`）、`pipeline.setConversation(newConv)`。

**resetChat 具体清了什么**：

[src/llm_chat.ts:530-540](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L530-L540) —— `resetChat(keepStats = false)`：`conversation.reset()`（清会话状态）、`resetRuntimeStats()`、`resetKVCache()`，并把 `filledKVCacheLength` 归零、重置 logit processor 状态。

**管线 prefillStep 里的哑模式分支**：

[src/llm_chat.ts:833-851](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L833-L851) —— `prefillStep()` 第 0 步：若 `conversation.isTextCompletion`，只做 `conversation.prompt = inp`；否则才调 `appendMessage(msgRole, ...)` 和 `appendReplyHeader(Role.assistant)` 把消息写进会话结构。随后统一进入 `getInputData()`。

**那道保险丝**：

[src/llm_chat.ts:2018-2031](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2018-L2031) —— `getInputData()` 开头：`isTextCompletion` 为真时，若 `filledKVCacheLength !== 0` 立即抛 `TextCompletionExpectsKVEmptyError`；否则 `prompts = this.conversation.getPromptArrayTextCompletion()`。对比第 2034 行起的 chat 分支：`filledKVCacheLength === 0` 时才全量编码、非零时只编码最后一轮（`getPromptArrayLastRound`）——这正是 chat 能增量、completion 必须全清的根源。

**Conversation 的「裸 prompt」存取**：

[src/conversation.ts:266-274](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L266-L274) —— `getPromptArrayTextCompletion()`：把 `this.prompt` 原样装进数组返回，**没有任何 system/roles/seps 拼接**；若误在非文本补全会话上调用则抛 `TextCompletionConversationExpectsPrompt`。

[src/conversation.ts:32-63](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L32-L63) —— `Conversation` 类声明 `isTextCompletion: boolean` 与配套的 `prompt` 字段，构造函数第三个参数默认 `false`。

**守卫方法族**——文本补全模式下这些「会话式」操作全部非法：

[src/conversation.ts:239-246](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L239-L246) —— `getPromptArray()`：`isTextCompletion` 时抛 `TextCompletionConversationError("getPromptArray")`（chat 专用的全量模板拼接在补全模式下禁止）。

[src/conversation.ts:302-321](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L302-L321) —— `appendMessage()`：同样先抛错拦截；只有会话模式才会把 `[role, role_name_str, message]` 推进 `messages` 数组。

[src/conversation.ts:343-346](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L343-L346) —— `finishReply()`：补全模式下同样被拦截——所以 [src/llm_chat.ts:947-949](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L947-L949) 的 `triggerStop()` 在中断生成时也要先判 `!isTextCompletion` 才调 `finishReply` 把半截回复写回会话。

[src/conversation.ts:279-287](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L279-L287) —— `reset()` 会把 `isTextCompletion` 和 `prompt` 一并清掉，保证会话状态机干净地回到初始。

**模式切换必然触发重置**：

[src/conversation.ts:393](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L393) —— `compareConversationObject()` 的相等判定包含 `convA.isTextCompletion !== convB.isTextCompletion`。也就是说 chat ↔ completion 两种模式互相切换时，新旧 Conversation 永远「不相等」，引擎必然走 `resetChat()` 全量重算——这就是两种接口可以安全混用在同一个引擎上的原因。

**错误类定义**：

[src/error.ts:476-481](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L476-L481) —— `TextCompletionExpectsKVEmptyError`：消息为 `"Non-chat text completion API expects KVCache to be empty."`。

#### 4.3.4 代码实践

**实践目标**：验证「先 chat 后 completion」的真实行为——它**不会**抛 `TextCompletionExpectsKVEmptyError`，而是静默重置会话；并从源码解释为什么。

**操作步骤**（示例代码，追加到示例 `main()`；需把模型换成任一 instruct 模型以便先做 chat）：

```ts
// —— 示例代码：先用 chat 建立会话，再用 completion 打断它 ——
// 1. 建立一段聊天历史
await engine.chatCompletion({
  messages: [{ role: "user", content: "My name is Alice. Hi!" }],
  max_tokens: 32,
});

// 2. 紧接着发起文本补全
const c = await engine.completion({
  prompt: "The capital of France is",
  max_tokens: 16,
});
console.log("completion after chat:", c.choices[0].text, c.usage?.prompt_tokens);

// 3. 再追问一句，看模型还记不记得 Alice
const r = await engine.chatCompletion({
  messages: [{ role: "user", content: "What is my name?" }],
  max_tokens: 32,
});
console.log("chat after completion:", r.choices[0].message.content);
```

**需要观察的现象**：

- 第 2 步**不抛错**，正常返回补全文本；`usage.prompt_tokens` 只包含 `"The capital of France is"` 这一句的 token 数（不含之前聊天历史）。
- 第 3 步模型大概率「失忆」，不知道名字是 Alice。

**预期结果**：与源码推演一致——第 2 步进入 [engine.ts:1407-1417](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1407-L1417) 的补全分支，`resetChat()` 在 [llm_chat.ts:2028](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2028) 的检查之前就把 KV cache 清空了，所以保险丝不动作；第 3 步因为 `isTextCompletion` 参与会话比对（conversation.ts:393）再次全量重置，聊天历史已丢。模型是否真的「失忆」取决于模型本身，**待本地验证**。

**想真正看到 `TextCompletionExpectsKVEmptyError` 该怎么办？** 公共 API 做不到（引擎层永远先 reset）。它只在绕过 `engine.prefill` 直接操作管线、或未来代码破坏重置逻辑时才会出现——这正是「不变量断言」的设计意图：防的是库内部的错误，不是用户的错误。

#### 4.3.5 小练习与答案

**练习 1**：为什么文本补全不能像 chat 那样复用 KV cache 做增量？

**答案**：chat 的多轮复用依赖「新旧对话前缀完全一致」——旧轮次的 KV 恰好是新 prompt 的前缀（u2-l2 讲过的 `compareConversationObject` 比对）。而 completion 的输入是任意裸字符串，与 cache 里已有的内容没有任何前缀关系；若不清理就续写，模型会在「上一段对话的 KV + 新裸文本」这种语义错位的上下文里生成，结果不可预测。所以引擎选择每次 `resetChat()` 全清，并用 `TextCompletionExpectsKVEmptyError` 兜底。

**练习 2**：`getPromptArray()` 和 `getPromptArrayTextCompletion()` 分别给谁用？在文本补全模式下调用前者会发生什么？

**答案**：前者给 chat 路径用，按 `conv_template` 把 `messages` 拼成带角色标签的完整 prompt；后者给补全路径用，直接返回 `[this.prompt]`。补全模式下调 `getPromptArray()` 会在 [conversation.ts:239-246](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L239-L246) 抛 `TextCompletionConversationError("getPromptArray")`。

**练习 3**：`triggerStop()`（中断生成）里为什么多了一个 `!this.conversation.isTextCompletion` 判断？

**答案**：见 [llm_chat.ts:947-949](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L947-L949)。中断时 chat 路径要调 `conversation.finishReply()` 把半截回复写回 `messages`，而 `finishReply` 在补全模式下是被守卫拦截的（conversation.ts:343-346）；补全路径的输出只存在于 `outputMessage` 字符串里，没有会话结构可写，所以直接跳过。

### 4.4 Completion 响应组装：choices 与 usage

#### 4.4.1 概念说明

补全响应 `Completion` 与 chat 响应 `ChatCompletion` 结构同构，差别在 choice 内部：

- chat 的 choice 装的是 `message`（含 `role`、`content`、`tool_calls`）；
- completion 的 choice 装的是 `text`——纯文本一段，没有角色概念；
- `object` 字段一个是 `"chat.completion"`，一个是 `"text_completion"`，方便客户端区分。

`n` 参数控制生成几个候选：引擎串行跑 `n` 次「一次 prefill + 一段 decode」，每个候选成为 `choices` 数组里的一项，`index` 从 0 递增。`echo: true` 会把原 prompt 拼在文本前面——补全场景下常用于看「模型到底读到了什么」。

#### 4.4.2 核心流程

```text
非流式 completion 响应组装（engine.completion 尾部）：
  for i in 0..n-1:
      若 interruptSignal 已置位 → triggerStop()，outputMessage = ""（一次中断停掉所有候选）
      否则 outputMessage = _generate()   ← prefill + decode 循环
      choices.push({
        finish_reason,                    ← stop / length / abort...
        index: i,
        logprobs,                         ← 仅当 request.logprobs 为 true
        text: echo ? prompt + outputMessage : outputMessage,
      })
      累计 completion_tokens / prompt_tokens / prefill_time / decode_time
  返回 { id, choices, model, object: "text_completion", created,
         usage: { ...token 计数, extra: { 延迟与吞吐指标 } } }
  最后若设了 seed → setSeed(Date.now()) 重置随机源，避免影响后续请求
```

#### 4.4.3 源码精读

**n 个候选的生成循环**：

[src/engine.ts:1029-1066](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1029-L1066) —— `const n = request.n ? request.n : 1;`（缺省 1）后进入 for 循环：每个候选调一次 `this._generate()`（即 [engine.ts:457-479](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L457-L479) 的「prefill 一次 + decode 直到 stopped」）；第 1038–1041 行处理中断：一个中断信号应停掉**所有**候选，后续候选直接 `triggerStop()` 且 `outputMessage = ""`。

**choice 的组装，重点看 `echo`**：

[src/engine.ts:1052-1061](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1052-L1061) —— `text: request.echo ? request.prompt + outputMessage : outputMessage`——回显就是简单的字符串拼接；`logprobs` 直接复用 chat 的 `ChatCompletion.Choice.Logprobs` 结构（tokenizer 产生的 token 级对数概率数组）。

**响应对象与 usage.extra**：

[src/engine.ts:1071-1092](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1071-L1092) —— 组装 `Completion`：`id` 用 `crypto.randomUUID()`、`object: "text_completion"`；`usage.extra` 里是 WebLLM 特有性能块：`e2e_latency_s`（端到端秒数）、`prefill_tokens_per_s`、`decode_tokens_per_s`、`time_to_first_token_s`（等于 prefill 总耗时）、`time_per_output_token_s`，以及可选的 `latencyBreakdown`（仅当 `request.extra_body.enable_latency_breakdown` 为真）。

**seed 用完即弃**：

[src/engine.ts:1094-1097](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1094-L1097) —— 请求结束后立刻 `setSeed(Date.now())` 把随机源重置回非确定状态，防止本次 seed 泄漏影响后续无关请求。

**响应类型定义**：

[src/openai_api_protocols/completion.ts:272-312](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/completion.ts#L272-L312) —— `Completion` 接口：`id` / `choices` / `created`（Unix 秒）/ `model` / `object: "text_completion"` / `usage`。

[src/openai_api_protocols/completion.ts:314-331](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/completion.ts#L314-L331) —— `CompletionChoice` 接口：`finish_reason`（`stop`、`length` 等，复用 chat 的枚举）/ `index` / `logprobs`（注释明确说明与 openai-node 不同，复用了 ChatCompletion 的 Logprobs 类型）/ `text`。

**流式补全的 chunk 长什么样**（机制与 u2-l3 相同，这里只看数据形状）：

[src/engine.ts:589-604](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L589-L604) —— `asyncGenerate()` 里的 `_getChunk()`：非 chat 分支产出的每个流式元素就是一个完整 `Completion` 对象，`choices[0].text` 装的是本轮增量 `deltaMessage`，`finish_reason` 为 `null`（最后一帧为空 delta + 终止原因），`object` 为 `"text_completion"`。

#### 4.4.4 代码实践

**实践目标**：观察 `n`、`echo`、`logprobs` 与 `usage.extra` 的实际效果。

**操作步骤**（示例代码，可直接替换示例里的 `reply0` 调用）：

```ts
// —— 示例代码 ——
const reply = await engine.completion({
  prompt: "List 3 US states: ",
  echo: true,
  n: 3,
  max_tokens: 48,
  logprobs: true,
  top_logprobs: 1,
});
console.log("choices 数量:", reply.choices.length);
reply.choices.forEach((c, i) =>
  console.log(`#${i} finish=${c.finish_reason} text=${JSON.stringify(c.text)}`),
);
console.log("usage:", reply.usage);
```

**需要观察的现象**：

- `choices.length === 3`，三个 `index` 分别为 0、1、2；
- 每个 `text` 以 `"List 3 US states: "` 开头（echo 生效），且三个候选内容互不相同（seed 未固定）；
- `logprobs.content` 里每个元素带 token 字符串与 `logprob` 数值；
- `usage.prompt_tokens` 是**一个**候选的 prompt 计数 ×3 量级、`completion_tokens` 是三个候选生成 token 之和；`usage.extra.time_to_first_token_s` 与 `prefill_tokens_per_s` 有值。

**预期结果**：与上述一致；若把 `echo` 去掉，`text` 不再包含原 prompt。具体数值**待本地验证**（不同浏览器 GPU、不同模型结果不同）。

#### 4.4.5 小练习与答案

**练习 1**：`n=3` 时 `usage.prompt_tokens` 为什么大约是单候选的 3 倍？chat 接口多轮对话时为什么第二轮 `prompt_tokens` 反而可能比第一轮小？

**答案**：completion 的每个候选各自完整跑一遍「prefill + decode」（[engine.ts:1036-1066](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1036-L1066) 的 for 循环里 `prompt_tokens += getCurRoundPrefillTotalTokens()`），且每次 prefill 前都重置 KV，无法复用，所以按候选数线性累加。chat 的多轮命中 `compareConversationObject` 相等分支时复用 KV cache，只对新增部分 prefill，统计口径是「增量 token」，因此可能更小（u2-l2 的结论）。

**练习 2**：`finish_reason` 可能出现哪些值？分别在什么情况下产生？

**答案**：复用 `ChatCompletionFinishReason`：命中停止字符串/停止 token 或模型自然结束为 `stop`；达到 `max_tokens` 或上下文窗口限制为 `length`；调用 `interruptGenerate()` 中断为 `abort`（[engine.ts:472-475](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L472-L475) 的 interruptSignal 检查会调 `triggerStop()` 把 finishReason 置为 abort）。流式中间帧为 `null`。

**练习 3**：为什么引擎要在请求结束时 `setSeed(Date.now())`？

**答案**：seed 会固定管线的采样随机源。若不重置，下一个**没有**指定 seed 的请求会继续沿用旧 seed，产生「看起来可复现」的假象，破坏默认的非确定性语义。见 [engine.ts:1094-1097](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1094-L1097)。

## 5. 综合实践

**任务：同一个基础模型上「chat vs completion」对比实验 + 会话状态观察。** 这个任务把本讲四个模块串起来。

**准备**：进入 `examples/text-completion/`，执行 `npm install && npm start`（见 [examples/text-completion/package.json:6](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/text-completion/package.json#L6)，Parcel 在 8888 端口起服务），用支持 WebGPU 的浏览器打开。

**第一步：读懂示例为什么要自定义 appConfig。** 示例选的 `Llama-3.1-8B-q4f32_1-MLC` 是基础模型，**不在** `prebuiltAppConfig.model_list` 里（那里几乎都是 instruct 模型），所以必须自己写 `model_list`：

[examples/text-completion/src/text_completion.ts:16-41](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/text-completion/src/text_completion.ts#L16-L41) —— 注意三个细节：① `model` 指向基础模型权重仓库；② `model_lib` 复用了 **Instruct** 版的 wasm（`Llama-3_1-8B-Instruct-q4f32_1-ctx4k_cs1k-webgpu.wasm`）——同架构的模型可共用模型库（u1-l4 的结论在这里落地）；③ `overrides.context_window_size: 2048` 收小上下文省显存。前缀由 [src/config.ts:333-334](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L333-L334) 的 `modelVersion` 与 `modelLibURLPrefix` 拼出官方 wasm 分发地址。

**第二步：对比两种接口。** 对同一句输入分别调用：

```ts
// —— 示例代码 ——
const viaCompletion = await engine.completion({
  prompt: "List 3 US states: ",
  max_tokens: 48,
});
const viaChat = await engine.chatCompletion({
  messages: [{ role: "user", content: "List 3 US states: " }],
  max_tokens: 48,
});
console.log("completion:", viaCompletion.choices[0].text);
console.log("chat      :", viaChat.choices[0].message.content);
console.log("objects   :", viaCompletion.object, "|", viaChat.object);
```

对照输出与 [engine.ts:1379-1417](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1379-L1417)：completion 路径把裸字符串直通 tokenizer；chat 路径会把这句包进 Llama-3 的角色标签（`<|start_header_id|>user<|end_header_id|>...`）再编码。**预期**：基础模型在 completion 下通常续写得更自然；在 chat 模板下可能不遵守指令格式（比如不结束、重复 header）——因为它没学过这些标签。具体表现**待本地验证**。

**第三步：会话状态实验。** 按 4.3.4 的脚本先 chat 一句（告诉模型你的名字）再 completion 一句，最后 chat 追问名字。观察：第二步 completion 正常返回且 `prompt_tokens` 只计裸 prompt；第三步模型「失忆」。解释：[engine.ts:1410](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1410) 的 `resetChat()` 先于 [llm_chat.ts:2028](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2028) 的 KV 检查执行，所以**不抛 `TextCompletionExpectsKVEmptyError` 而是静默清空会话**；该错误是管线层的不变量保险丝，公共 API 下不可达。

**产出**：一份包含两组接口输出对照、三次调用 `usage` 数字、以及「为什么没有抛错」的源码级解释的实验记录。

## 6. 本讲小结

- `engine.completions.create()` 与 `engine.completion()` 完全等价：`Completions` 门面类只做一行转发，为兼容 OpenAI SDK 写法而生。
- `completion` 的输入是裸字符串 `prompt`，**不经过任何对话模板**直通 tokenizer；`chatCompletion` 的输入是结构化 `messages`，要按 `conv_template` 包裹——这是两个接口的本质差异，也决定了基础模型适合前者、指令模型适合后者。
- 引擎在 `prefill()` 里用 `"messages" in input` 区分两条路径；completion 路径**无条件 `resetChat()`**：清会话、清 KV cache、`filledKVCacheLength = 0`。
- `TextCompletionExpectsKVEmptyError` 是管线层的不变量断言：公共 API 下 reset 永远先于检查，故不可达；它防的是内部误用（在非空 KV 上对裸文本 prefill 会导致语义错乱）。`Conversation` 的 `isTextCompletion` 开关还让 `appendMessage`、`getPromptArray`、`finishReply` 等会话式方法全部变成守卫。
- 响应 `Completion` 的 `choices[i].text` 是纯文本（`echo: true` 时前缀拼接原 prompt），`n` 个候选由 n 次独立的「prefill + decode 循环」串行产生，一次中断停掉全部候选。
- `usage.extra` 提供 `time_to_first_token_s`、`prefill_tokens_per_s`、`decode_tokens_per_s` 等性能指标；请求级 `seed` 在结束时被重置，不污染后续请求。

## 7. 下一步学习建议

- **u2-l5（embedding 接口）**：第三类 OpenAI 风格接口，走独立的 `EmbeddingPipeline`，与补全的「单管线复用」形成对照。
- **u3-l2（Conversation 对话模板）**：本讲把 `Conversation` 当「哑模式」用；下一阶段应精读它的会话模式——`getPromptArray` 如何拼 system/roles/seps，回头更能体会两种模式的分工。
- **u3-l3 / u3-l4（prefillStep 与 decodeStep）**：本讲停在 `prefillStep()` 门口；想理解裸 prompt 如何分块送入 WebGPU、KV cache 如何写入，就顺着这两讲往下读。
- **u7-l3（延迟分解）**：本讲出现的 `usage.extra` 指标在那一讲有完整口径说明与实测方法。
