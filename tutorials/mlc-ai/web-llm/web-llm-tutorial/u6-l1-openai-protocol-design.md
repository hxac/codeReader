# u6-l1 OpenAI 协议层设计与请求校验

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `Chat` / `Completions` 两个门面类的作用，以及 `engine.chat.completions.create()` 与 `engine.chatCompletion()` 的关系。
2. 逐条讲解 `postInitAndCheckFields` 的八道校验关卡：它检查什么、抛什么错、以及它在什么情况下会**原位改写（mutate）用户的请求对象**。
3. 理解 WebLLM 的「三层校验架构」：协议层（`postInitAndCheckFields`）、引擎层（`postInitAndCheckGenerationConfigValues`）、会话层（`getConversationFromChatCompletionRequest`）各自负责什么。
4. 掌握流式/非流式请求在**类型层面**（可辨识联合 + 函数重载）与**运行时层面**（`request.stream` 分支）的双重区分方式。
5. 分析未知或未支持字段如何触发 `UnsupportedFieldsError`——并且能说出一个关键事实：**chat 接口的未支持字段列表当前是空的**，这个机制真正生效的地方在 completion 接口。

## 2. 前置知识

本讲是高级篇第一节，假设你已完成第二单元（引擎接口层）和 u3-l2（Conversation 对话模板）。用三段话把需要的直觉补齐：

**（1）门面模式（Facade Pattern）回顾。**
在 u1-l3 我们说过：WebLLM 的协议层是一组「门面」——`Chat`、`Completions`、`Embeddings` 类本身不包含任何推理逻辑，只是把 OpenAI SDK 风格的调用姿势（`openai.chat.completions.create(...)`）翻译成引擎方法（`engine.chatCompletion(...)`）。这样做的收益是：已经写好对接 OpenAI API 的前端代码，把 `openai` 客户端换成 `CreateMLCEngine()` 的返回值，几乎不用改业务代码。

**（2）可辨识联合（Discriminated Union）。**
TypeScript 的联合类型里，如果每个成员都有一个相同名字、但取值不同的字面量字段（这里是 `stream`），编译器就能靠这个字段判断「当前值属于哪个成员」。`stream: true` 时返回流式类型，`stream?: false | null` 时返回非流式类型——这就是「流式/非流式在类型层面的区分」，我们在 u2-l3 已经从使用角度见过它，本讲从定义侧精读。

**（3）「校验」在动态语言里的两层含义。**
TypeScript 的类型检查只发生在编译期。运行时到达引擎的请求对象可能是用户用 `as any` 绕过类型、或者从 JSON 反序列化出来的——字段可能缺失、越界、甚至是类型里根本不存在的角色。所以协议层必须再写一套**运行时校验**。理解「编译期类型 vs 运行时检查」的分工，是读懂本讲源码的钥匙。

**（4）`in` 操作符。**
`"field" in request` 是 JavaScript 的运行时运算，判断对象上是否存在某个属性。`UnsupportedFieldsError` 机制正是用它逐个探测黑名单字段是否出现在请求里。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/openai_api_protocols/chat_completion.ts`（1227 行） | chat 协议的全部类型 + 门面类 + `postInitAndCheckFields` | 本讲主战场 |
| `src/openai_api_protocols/index.ts` | 协议层桶文件，把三个 `postInitAndCheckFields` 改名再导出 | `postInitAndCheckFieldsChatCompletion` 别名的由来 |
| `src/openai_api_protocols/completion.ts` | 文本补全协议 | 对照组：它的未支持字段黑名单非空 |
| `src/error.ts`（629 行） | 全部错误类 | 本讲涉及的十余个错误类的定义 |
| `src/config.ts` | 模型配置 + `postInitAndCheckGenerationConfigValues` | 数值范围校验（temperature、max_tokens 等）的真实所在地 |
| `src/conversation.ts` | 对话模板 | 非法 role 的最终抛出点 |
| `src/engine.ts` | 引擎编排 | `chatCompletion` 如何串起三层校验 |
| `tests/openai_chat_completion.test.ts`（484 行） | 协议校验的单元测试 | 无 GPU 环境下复现所有校验错误的抓手 |

## 4. 核心概念与源码讲解

在进入三个模块之前，先建立全局地图——**一次 `chatCompletion` 调用要过三层校验**：

```
engine.chatCompletion(request)
  │
  ├─ 第 0 步：getLLMStates()           找到目标模型与管线（engine.ts:801）
  │
  ├─ 第一层【协议层】postInitAndCheckFields        ← 本讲 4.2
  │    结构合法性：消息顺序、stream/n 组合、seed 类型、
  │    response_format 配对、tools 白名单、stream_options 前置条件
  │
  ├─ 第 1.5 步：摘录采样参数 → GenerationConfig     （engine.ts:810-825）
  │
  └─ 第二层【引擎层】postInitAndCheckGenerationConfigValues（engine.ts:467 / 525）
       数值合法性：temperature、max_tokens、top_p、penalty、logit_bias 范围
       （定义在 config.ts:167，不是协议文件里！）

  之后进入 prefill 时还有——
  第三层【会话层】getConversationFromChatCompletionRequest（conversation.ts:497）
       把 messages 逐条翻译进 Conversation；非法 role 在这里抛 UnsupportedRoleError
```

记住这张图，你就不会把「temperature 越界」错归到协议层——这是读源码时最常见的张冠李戴。

### 4.1 Chat/Completions 门面类与请求类型体系

#### 4.1.1 概念说明

`chat_completion.ts` 的开头license 注释说明了它的出身：**类型定义直接采用自 openai-node 官方 SDK，仅做了少量修改**。这是 WebLLM 协议层的设计决策——不自己发明协议，而是「抄」OpenAI 的类型，从而最大化兼容生态里已有的代码。

文件内容可以分成四块：

1. **门面类**：`Chat` 和 `Completions`，纯粹转发。
2. **高层接口（HIGH-LEVEL INTERFACES）**：`ChatCompletionRequest*` 一族请求类型、`ChatCompletion` / `ChatCompletionChunk` 响应类型。
3. **校验函数**：`postInitAndCheckFields`（4.2 的主角）。
4. **支撑类型（BELOW ARE INTERFACES THAT SUPPORT THE ONES ABOVE）**：消息参数、工具调用、logprobs 等细分类型。

一个值得注意的源码阅读细节：[chat_completion.ts:89](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L89) 的注释宣称「`model` 字段被排除，请用 `CreateMLCEngine(model)` 指定模型」，但 [chat_completion.ts:260-267](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L260-L267) 实际定义了可选的 `model` 字段——这是多模型引擎（u2-l1 讲过的 `loadedModelIdToPipeline`）加入后接口演化留下的注释漂移。**以代码为准、注释为参考**，是读任何成熟源码的基本功。

#### 4.1.2 核心流程

调用链与类型层级分别如下：

```text
页面代码
  └─ engine.chat.completions.create(request)   // OpenAI SDK 姿势
       └─ Completions.create(request)          // 门面，纯转发
            └─ engine.chatCompletion(request)  // 真正干活
```

```text
ChatCompletionRequest (可辨识联合, L305-307)
├─ ChatCompletionRequestNonStreaming (L289)  stream?: false | null
└─ ChatCompletionRequestStreaming  (L297)  stream: true
     都继承自 ↓
     ChatCompletionRequestBase (L91)
     ├─ messages: Array<ChatCompletionMessageParam>   // 唯一必填字段
     ├─ 采样参数：temperature / top_p / max_tokens / *_penalty / logit_bias / seed ...
     ├─ 结构化输出：response_format: ResponseFormat
     ├─ 工具调用：tools / tool_choice
     └─ WebLLM 扩展：ignore_eos、extra_body（enable_thinking 等）

ChatCompletionMessageParam (L788-792，四成员联合)
├─ system  （content: string）
├─ user    （content: string | 内容数组，数组可含 image_url —— 多模态入口）
├─ assistant（content 可选，可带 tool_calls）
└─ tool    （content: string + tool_call_id）
```

响应侧对称地分为 `ChatCompletion`（整体返回，L312）与 `ChatCompletionChunk`（流式分片，L362），两者的 `object` 字面量分别是 `"chat.completion"` 与 `"chat.completion.chunk"`——这本身就是一种运行时可辨识标记。

#### 4.1.3 源码精读

**门面类本体**——两个类加起来不到 30 行，不含任何逻辑：

[chat_completion.ts:50-58](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L50-L58) 定义 `Chat`：持有引擎引用，并在构造时创建自己的 `completions` 成员。这样 `engine.chat` 才能继续点出 `.completions`。

[chat_completion.ts:60-79](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L60-L79) 定义 `Completions`：三个 `create` 重载签名 + 一个实现，实现体只有一行——`return this.engine.chatCompletion(request)`。注意前两个重载分别接受非流式/流式请求并返回 `Promise<ChatCompletion>` / `Promise<AsyncIterable<ChatCompletionChunk>>`，第三个是兜底宽签名。**门面连校验都不做**，校验发生在引擎里的 `chatCompletion` 内部。

**请求基类的关键字段**：

[chat_completion.ts:91-110](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L91-L110) `ChatCompletionRequestBase` 的开头：`messages` 是唯一必填字段；`stream` 的类型是宽泛的 `boolean | null`，由子接口收窄（见 4.3）。

[chat_completion.ts:272-287](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L272-L287) `extra_body`：WebLLM 私有扩展字段的收纳处（`enable_thinking`、`enable_latency_breakdown`），OpenAI 协议里没有它们——扩展而不污染协议，是这类「兼容层」设计的惯用手法。

**桶文件的别名导出**：协议目录下有三个同名函数 `postInitAndCheckFields`（chat、completion、embedding 各一份）。[openai_api_protocols/index.ts:18-49](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/index.ts#L18-L49) 用 `postInitAndCheckFields as postInitAndCheckFieldsChatCompletion` 把它们改名后统一导出；completion 与 embedding 版本同理（[index.ts:51-68](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/index.ts#L51-L68)）。引擎里 `API.postInitAndCheckFieldsChatCompletion(...)` 的长名字就是这么来的。

#### 4.1.4 代码实践

**实践目标**：验证门面转发关系，并亲手感受「同一请求、两种姿势」。

**操作步骤**：

1. 打开你在 u1-l2 跑通的 `examples/get-started` 工程（若未保留，按该讲义重新搭建）。
2. 在 `src/get_started.ts` 中找到流式调用 `engine.chat.completions.create({...})` 的位置。
3. 追加一段对照代码（**示例代码**，非仓库原有）：

```ts
// 姿势一：OpenAI SDK 风格（门面）
const r1 = await engine.chat.completions.create({
  messages: [{ role: "user", content: "用一句话介绍你自己" }],
});
// 姿势二：引擎原生方法（门面背后的真身）
const r2 = await engine.chatCompletion({
  messages: [{ role: "user", content: "用一句话介绍你自己" }],
});
console.log(r1.choices[0].message.content);
console.log(r2.choices[0].message.content);
```

4. 把 TypeScript 故意写错一次：在姿势一的返回值上写 `for await (const c of r1)`，观察编译器报错（非流式重载返回的是 `ChatCompletion`，不可迭代）。

**需要观察的现象**：两种姿势都能得到回复；故意写错的迭代代码在**编译期**就被拒绝，不需要运行。

**预期结果**：`r1` 与 `r2` 结构完全一致（`object: "chat.completion"`、`choices[0].message.content` 为模型回复）。若采样的随机性导致文字不同，多跑几次或加 `seed: 42` 对比。首次运行需下载模型，耗时取决于网络——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`Chat` 类为什么要在构造函数里创建 `Completions` 实例，而不是让 `Completions.create` 做成静态方法？

**参考答案**：`Completions` 需要持有引擎引用才能转发到 `engine.chatCompletion`；把实例挂在 `chat.completions` 上，形成 `engine.chat.completions.create(...)` 的链式路径，与 OpenAI SDK 的对象形状保持一致。静态方法无法携带每个引擎各自的状态。

**练习 2**：`ChatCompletionRole`（L694-699）包含 `"function"`，但 `ChatCompletionMessageParam`（L788-792）只有 system/user/assistant/tool 四个成员。这意味着什么？

**参考答案**：类型层面 `"function"` 角色的消息无法通过类型检查；但若用 `as any` 或 JSON 输入绕过，运行时会落到会话层的 `UnsupportedRoleError`（见 4.2.3 末尾与 4.2.4）。类型联合与运行时分发链不完全同步，是运行时校验必须存在的又一例证。

**练习 3**：WebLLM 想新增一个 OpenAI 没有的请求参数，应该放在哪里？举一个现有例子。

**参考答案**：放进 `extra_body`（OpenAI 官方 SDK 也用这个口子放厂商私有参数）。现有例子：`extra_body.enable_thinking`、`extra_body.enable_latency_breakdown`（[chat_completion.ts:272-287](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L272-L287)）。另有 `ignore_eos` 直接放在基类上，属于历史做法的并存。

### 4.2 postInitAndCheckFields：协议层校验与原位改写

#### 4.2.1 概念说明

函数签名的注释写得很直白：*"Post init and verify whether the input of the request is valid. Thus, this function can throw error or **in-place update request**."*——它做两件事：

1. **抛错**：结构非法时立即失败（fail fast），在进入 GPU 推理前就把问题暴露给调用方。
2. **原位改写**：某些合法请求需要「补全」——最典型的是 Hermes 系列的函数调用，函数会直接往 `request.messages` 头部 `unshift` 一条系统消息、并覆写 `request.response_format`。这是一种有意的 mutation 设计：下游（会话层、管线）拿到的请求已被规整为标准形态。

「Post init」的叫法来自 TypeScript 的习惯：接口构造完成后立刻做的初始化/检查钩子。它**不检查数值范围**（temperature 多大算超界？）——那属于采样语义，交给第二层 `postInitAndCheckGenerationConfigValues`。

#### 4.2.2 核心流程

函数体自带编号注释，八道关卡按序执行，**任一关卡失败即抛出，后续关卡不再运行**：

```伪代码
function postInitAndCheckFields(request, currentModelId, currentModelType):
  1. 黑名单字段检查      → UnsupportedFieldsError
  2. 逐条扫描 messages:
       user 非字符串 content → 仅 VLM 可用，否则 UserMessageContentErrorForNonVLM
       image_url.detail 非空 → UnsupportedDetailError
       url 前缀不合法        → UnsupportedImageURLError
       文本 part 多于一段    → MultipleTextContentError
       system 不在第 0 位    → SystemMessageOrderError
  3. 最后一条必须是 user 或 tool → MessageOrderError
  4. stream 且 n>1            → StreamingCountError
  5. seed 非整数               → SeedTypeError
  6. response_format 字段配对:
       有 schema 但 type≠json_object       → InvalidResponseFormatError
       有 grammar 但 type≠grammar / 反之    → InvalidResponseFormatGrammarError
       有 structural_tag 但 type 不匹配/反之 → InvalidResponseFormatStructuralTagError
  7. tools 存在时:
       模型不在函数调用白名单  → UnsupportedModelIdError
       Hermes-2-Pro/Hermes-3 且用户自带 response_format → CustomResponseFormatError
       Hermes 且用户自带 system 消息     → CustomSystemPromptError
       （合法时）原位改写：注入 Hermes 系统 prompt + json_object response_format
  8. stream_options 存在但 stream 未开 → InvalidStreamOptionsError
```

#### 4.2.3 源码精读

**函数签名与黑名单机制**：

[chat_completion.ts:409](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L409) 定义黑名单：`ChatCompletionRequestUnsupportedFields` 当前是**空数组**，行尾注释写着 "all supported as of now"。也就是说：chat 接口今天不会因未知字段抛错。

[chat_completion.ts:418-433](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L418-L433) 函数签名与第一道关卡：遍历黑名单，用 `field in request` 探测，命中则收集进数组，最后一次性抛出（把所有违规字段列在同一句报错里，比逐个抛更友好）。

[error.ts:248-256](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L248-L256) `UnsupportedFieldsError` 的定义：消息形如 "The following fields in Xxx are not yet supported: a, b"。

**机制真正生效的对照组**——completion 接口的黑名单非空：

[completion.ts:335-339](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/completion.ts#L335-L339) 列出 `suffix`、`user`、`best_of` 三个不支持字段；[completion.ts:353-361](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/completion.ts#L353-L361) 用同样的 `in` 探测 + 抛 `UnsupportedFieldsError("...", "CompletionCreateParams")`。**这个设计是「预防性脚手架」**：类型在 interface 里声明了、但引擎还没实现（或决定不实现）的字段，就登记进黑名单，让用户在运行时得到明确报错而不是静默忽略。

**消息顺序两道关卡**：

[chat_completion.ts:473-475](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L473-L475) system 消息必须位于 `index === 0`，否则抛 `SystemMessageOrderError`（[error.ts:113-118](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L113-L118)）。

[chat_completion.ts:479-488](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L479-L488) 最后一条消息必须是 `user` 或 `tool`（对话要「等模型接话」），否则抛 `MessageOrderError`（[error.ts:106-111](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L106-L111)）。

注意一个诚实的结论：**源码中没有专门的「tool 消息顺序」校验**。比如「tool 消息之前必须存在带 `tool_calls` 的 assistant 消息」「`tool_call_id` 必须能对上某次调用」都没有被检查——`tool_call_id` 在整个 `src/` 里只出现于类型定义处（[chat_completion.ts:785](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L785)）。「乱序」请求在协议层能触发的只有上述 system 位置与末位角色两种错误。

**stream/n/seed/stream_options 四道关卡**：

[chat_completion.ts:490-493](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L490-L493) 流式时 `n` 不能大于 1（单序列流，多候选无法交织输出），抛 `StreamingCountError`（[error.ts:394-399](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L394-L399)）。

[chat_completion.ts:495-500](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L495-L500) `seed` 必须是整数（`Number.isInteger`），抛 `SeedTypeError`（[error.ts:401-406](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L401-L406)）。

[chat_completion.ts:599-604](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L599-L604) `stream_options` 只能伴随 `stream: true` 出现，抛 `InvalidStreamOptionsError`（[error.ts:463-468](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L463-L468)）。

**response_format 配对检查（关卡 6）**：

[chat_completion.ts:502-548](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L502-L548) 对 `ResponseFormat`（类型定义在 [chat_completion.ts:1198-1227](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L1198-L1227)）做「双向配对」检查：`schema` 只能配 `json_object`、`grammar` 与 `type: "grammar"` 必须同现、`structural_tag` 与 `type: "structural_tag"` 必须同现。这是典型的「字段间依赖校验」，类型系统表达不了，只能运行时判。

**原位改写的现场（关卡 7）**：

[chat_completion.ts:567-596](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L567-L596) Hermes-2-Pro / Hermes-3 的硬编码函数调用支持：先拒绝用户自带的 `response_format`（`CustomResponseFormatError`）和 system 消息（`CustomSystemPromptError`），然后**直接改写请求**——覆写 `request.response_format` 为带官方 schema 的 `json_object`，并向 `request.messages` 头部 `unshift` 一条渲染好工具列表的系统消息。这就是「in-place update request」的含义：函数返回 `void`，但请求对象已被变换。测试 [tests/openai_chat_completion.test.ts:457-483](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/openai_chat_completion.test.ts#L457-L483) 正是断言了这种改写结果（`request.messages[0].role` 变成 `"system"` 等）。

**它不检查什么——另外两层的所在地**：

数值范围校验在 [config.ts:167-249](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L167-L249) `postInitAndCheckGenerationConfigValues`：`max_tokens <= 0` 抛 `MinValueError`（[config.ts:189-191](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L189-L191)）、`temperature < 0` 抛 `NonNegativeError`（[config.ts:195-197](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L195-L197)）、penalty 越界抛 `RangeError`（[config.ts:174-185](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L174-L185)）等。引擎在生成前调用它：[engine.ts:467](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L467)（非流式 `_generate` 内）与 [engine.ts:525](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L525)（流式 `asyncGenerate` 内）。

**一个容易误信文档的细节**：`temperature` 的注释宣称取值范围是 0 到 2，但实现只检查 `< 0`——`temperature: 3` 不会抛任何错（会被管线按温度语义处理）。读校验代码时，以 `if` 条件为准，不以来自 OpenAI 文档的注释为准。

非法 role 的抛出点在第三层：[conversation.ts:497-517](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L497-L517) `getConversationFromChatCompletionRequest` 用 if-else 链分发四种合法角色，落进 `else` 即抛 `UnsupportedRoleError`（[conversation.ts:515](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L515)，错误类定义在 [error.ts:127-132](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L127-L132)）。注意这段代码还会**再查一遍**末位角色与 system 位置（[conversation.ts:491-502](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L491-L502)）——因为该函数也被不走引擎校验的低层路径调用，防御性重复是有意的。

#### 4.2.4 代码实践

**实践目标**：不依赖 WebGPU，在 Node/jest 环境里直接驱动 `postInitAndCheckFields` 与 `postInitAndCheckGenerationConfigValues`，把每个错误类「打出来」。这正是仓库单测的做法——测试文件就是从 `../src/openai_api_protocols/chat_completion` 直接 import 函数来调的。

**操作步骤**：

1. 在仓库根目录新建 `tests/playground_validation.test.ts`（**示例代码**；练完可删除或留作笔记，不要提交）：

```ts
import { postInitAndCheckFields } from "../src/openai_api_protocols/chat_completion";
import { postInitAndCheckGenerationConfigValues } from "../src/config";
import { ModelType, GenerationConfig } from "../src/config";
import { ChatCompletionRequest } from "../src/openai_api_protocols/chat_completion";

const LLM = "Llama-3.1-8B-Instruct-q4f32_1-MLC";

// A. 超范围 temperature：注意要传负数才会抛！
test("negative temperature", () => {
  const genConfig = { temperature: -0.5 } as GenerationConfig;
  expect(() => postInitAndCheckGenerationConfigValues(genConfig)).toThrow(
    "Make sure temperature >= 0.",
  );
});

// B. 负数 max_tokens
test("negative max_tokens", () => {
  const genConfig = { max_tokens: -5 } as GenerationConfig;
  expect(() => postInitAndCheckGenerationConfigValues(genConfig)).toThrow(
    "Make sure `max_tokens` > 0.",
  );
});

// C. 非法 role（绕过类型检查）
test("invalid role", () => {
  const request = {
    messages: [{ role: "function", content: "hi" }],
  } as unknown as ChatCompletionRequest;
  // 注意：非法 role 不在协议层抛，而在会话层——这里改测会话层入口
  // 可 import getConversationFromChatCompletionRequest 验证，见步骤 3
});

// D. 乱序消息：末位是 assistant
test("last message is assistant", () => {
  const request: ChatCompletionRequest = {
    messages: [
      { role: "user", content: "hi" },
      { role: "assistant", content: "hello" },
    ],
  };
  expect(() => postInitAndCheckFields(request, LLM, ModelType.LLM)).toThrow(
    "Last message should be from either `user` or `tool`.",
  );
});

// E. 未知字段：chat 的黑名单为空，不会抛！
test("unknown field on chat does NOT throw", () => {
  const request = {
    messages: [{ role: "user", content: "hi" }],
    foo_bar: 123,
  } as unknown as ChatCompletionRequest;
  expect(() => postInitAndCheckFields(request, LLM, ModelType.LLM)).not.toThrow();
});
```

2. 运行：`npx jest tests/playground_validation.test.ts`（等价于 `npm test` 的子集；`npm test` 会跑全部并带覆盖率）。
3. 对 C 补全会话层验证：`import { getConversationFromChatCompletionRequest } from "../src/conversation";`，对同一个 `role: "function"` 的请求调用它，断言抛出 `"Unsupported role of message: function"`。
4. 触发一次「真正的」`UnsupportedFieldsError`：改用 completion 协议（`import { postInitAndCheckFields as checkCompletion } from "../src/openai_api_protocols/completion";`），构造 `{ prompt: "hi", best_of: 2 }`，断言抛出 `"The following fields in CompletionCreateParams are not yet supported: best_of"`。

**需要观察的现象**：A、B、D 按断言抛错；E **不抛**（这是本实践最重要的发现）；C 只在会话层抛；步骤 4 中 completion 的未知字段抛错。

**预期结果**：与断言完全一致。A/B 的报错文本来自 [error.ts:17-22](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L17-L22) 与 [error.ts:38-43](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L38-L43)。整个实践不需要浏览器与 GPU，**可在任意 Node 环境验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `postInitAndCheckFields` 要在函数体内做「收集黑名单字段、最后一次抛出」，而其他关卡都是即时抛出？

**参考答案**：黑名单命中的字段之间没有因果关系，一次性报全能让用户改完再试；其余关卡（如消息顺序）存在先后依赖，报第一个即可定位问题。这也解释了 `UnsupportedFieldsError` 构造函数接收的是数组（[error.ts:248-256](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L248-L256)）。

**练习 2**：给 `chatCompletion` 传 `temperature: 5` 会发生什么？传 `temperature: -1` 呢？

**参考答案**：`5` 不抛错——协议层不看 temperature，引擎层只拦 `< 0`（[config.ts:195-197](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L195-L197)），`5` 会进入采样（高温下输出趋于随机）。`-1` 抛 `NonNegativeError`。注释里「0 到 2」的说法并未被完整实现。

**练习 3**：如果让你给 chat 协议新增一个「已声明但暂不支持」的字段（比如 `user`），最少改几处代码？

**参考答案**：两处——在 `ChatCompletionRequestBase` 声明字段（类型层面），再把字段名字符串加进 `ChatCompletionRequestUnsupportedFields`（[chat_completion.ts:409](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L409)）。运行时探测与抛错逻辑（L424-433）无需改动，这正是数据驱动黑名单的好处。

### 4.3 流式与非流式重载

#### 4.3.1 概念说明

「流式还是非流式」必须在前端类型、门面签名、引擎签名、运行时分支四个层面保持一致，WebLLM 用**同一套机制**贯穿：以 `stream` 为判别字段的可辨识联合 + 函数重载。

设计动机有二：

1. **类型安全**：`stream: true` 时返回 `AsyncIterable<ChatCompletionChunk>`，消费者必须用 `for await`；`stream: false` 时返回 `ChatCompletion`，可以直接取 `choices[0]`。若两者共用一个宽泛的返回类型，调用方就得到处写类型断言。
2. **单一实现**：无论门面还是引擎，多个重载签名之后只有一个实现体，运行时用 `if (request.stream)` 分流——签名是「给编译器看的路标」，不是复制代码。

#### 4.3.2 核心流程

```text
编译期（TypeScript）：
  字面量 stream: true 匹配 ChatCompletionRequestStreaming
    → 重载 2 签名 → 返回 Promise<AsyncIterable<ChatCompletionChunk>>
  stream 缺省/false/null 匹配 ChatCompletionRequestNonStreaming
    → 重载 1 签名 → 返回 Promise<ChatCompletion>

运行时（engine.chatCompletion 实现体）：
  1. getLLMStates 选中模型管线                (engine.ts:801)
  2. 协议层校验 postInitAndCheckFields        (engine.ts:805)
     └─ 顺带拦截非法组合：stream 且 n>1 → StreamingCountError
  3. 摘录 GenerationConfig                    (engine.ts:810-825)
  4. 获取模型互斥锁                            (engine.ts:828-829)
  5. if (request.stream) → 返回 asyncGenerate 生成器（惰性）
     否则 → try-finally 里跑 _generate，结束释放锁 (engine.ts:832 / 843)
```

结合 u2-l3 的结论：流式路径在入口拿锁、流消费完才放锁；非流式路径用 try-finally 保证异常时也放锁。

#### 4.3.3 源码精读

**类型层面的三分结构**：

[chat_completion.ts:289-307](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L289-L307) 是整个机制的类型侧：`NonStreaming` 把 `stream` 收窄为 `false | null`（仍可选），`Streaming` 把它定为必填的字面量 `true`，二者联合成 `ChatCompletionRequest`。判别字段名相同、类型互斥——教科书式的可辨识联合。

**门面的三重载**：

[chat_completion.ts:67-78](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L67-L78) `Completions.create` 的重载声明：非流式请求 → `Promise<ChatCompletion>`；流式请求 → `Promise<AsyncIterable<ChatCompletionChunk>>`；宽签名兜底；实现体一行转发。

**引擎的镜像重载与运行时分支**：

[engine.ts:787-798](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L787-L798) `MLCEngine.chatCompletion` 用完全相同的重载结构再声明一遍——门面与引擎的签名必须同步演进，这是该模式的维护成本。

[engine.ts:799-841](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L799-L841) 实现体：第 805 行调协议层校验，第 810-825 行把请求里的采样字段摘进 `GenerationConfig`（注意 `extra_body` 在这里被摊平成 `enable_thinking` 等字段），第 832 行 `if (request.stream)` 分流——**运行时判别与编译期判别用的是同一个字段**，这是整套一致性的根基。

**非法组合的拦截点**：

[chat_completion.ts:490-493](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L490-L493) `stream && n > 1` 抛 `StreamingCountError`——类型系统无法表达「stream 为 true 时 n 必须为 1」这种跨字段约束，只能运行时判。completion 接口有同款检查（[completion.ts:363-366](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/completion.ts#L363-L366)）。

#### 4.3.4 代码实践

**实践目标**：亲眼看到「改一个字段，编译期类型与运行时行为同时切换」。

**操作步骤**：

1. 在 get-started 工程里写两段代码（**示例代码**）：

```ts
// 非流式：直接 await 出完整结果
const res = await engine.chatCompletion({
  messages: [{ role: "user", content: "数到五" }],
  stream: false,
});
console.log(res.choices[0].message.content);

// 流式：同一调用点只改 stream，返回类型随之收窄
const chunks = await engine.chatCompletion({
  messages: [{ role: "user", content: "数到五" }],
  stream: true,
});
for await (const chunk of chunks) {
  console.log(chunk.choices[0]?.delta.content ?? "");
}
```

2. 把第一段的 `stream: false` 删掉（缺省），确认类型与行为不变（缺省走非流式分支）。
3. 在流式调用上加 `n: 2`，观察抛出的 `StreamingCountError: When streaming, \`n\` cannot be > 1.`
4. 对照测试复现：`tests/openai_chat_completion.test.ts:81-94` 的用例 "When streaming \`n\` needs to be 1" 在纯 Node 下跑 `npx jest tests/openai_chat_completion.test.ts` 即可验证同一行为。

**需要观察的现象**：`stream` 字面量决定 `res` 与 `chunks` 的 IDE 悬浮类型；`n: 2` 在进入任何推理之前立即抛错（首 token 都不会出现）；jest 用例无需浏览器即通过。

**预期结果**：四步全部符合预期；步骤 3 的报错文本与 [error.ts:394-399](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L394-L399) 一字不差。步骤 1-3 需要已加载模型，**待本地验证**；步骤 4 可直接验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ChatCompletionRequestNonStreaming.stream` 是可选的 `false | null`，而 `ChatCompletionRequestStreaming.stream` 是必填的 `true`？

**参考答案**：非流式是默认行为，允许不写 `stream` 字段（OpenAI 协议里它本就可选）；流式必须显式声明 `stream: true` 才能让编译器把类型收窄到流式分支——可选的判别字段无法支撑可靠的可辨识联合。

**练习 2**：门面 `Completions.create` 与引擎 `chatCompletion` 各写了一套三重载，这带来什么维护负担？漏改一处会怎样？

**参考答案**：任何签名调整都要双处同步（实际上是三处——u5 的 Worker 代理 `WebWorkerMLCEngine` 也实现同一接口）。若门面签名先放宽而引擎没跟上，类型会在门面处通过、在引擎处报错；反之则用户拿到的类型比实际能力窄。`MLCEngineInterface`（u2-l1）就是为约束这种同步而存在的静态契约。

**练习 3**：`stream_options: { include_usage: true }` 配 `stream: false` 会怎样？为什么这个检查放在协议层而不是类型层？

**参考答案**：抛 `InvalidStreamOptionsError`（[chat_completion.ts:599-604](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L599-L604)）。类型层无法表达「字段 A 的存在依赖字段 B 的值」这种约束，只能运行时判——与 stream/n 约束同理。

## 5. 综合实践

把本讲三个模块串成一个任务：**给「五个非法请求」建立一张错误映射表，并为它补一个仓库还缺的测试用例**。

### 任务 A：五个非法请求的完整映射

规格里给出的五类非法输入，经源码核实后的**真实**抛出位置如下（这张表就是本实践的交付物）：

| # | 构造的非法输入 | 是否抛错 | 错误类 | 真实抛出点 |
| --- | --- | --- | --- | --- |
| 1 | `temperature: 3` | **否**（注释说 0-2，但只查 `<0`） | — | — |
| 1' | `temperature: -0.5` | 是 | `NonNegativeError` | [config.ts:195-197](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L195-L197)，经 [engine.ts:467](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L467) 调用 |
| 2 | `max_tokens: -5` | 是 | `MinValueError` | [config.ts:189-191](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L189-L191) |
| 3 | `role: "function"`（需 `as any`） | 是 | `UnsupportedRoleError` | [conversation.ts:515](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L515) |
| 4a | 消息乱序：末条是 assistant | 是 | `MessageOrderError` | [chat_completion.ts:479-488](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L479-L488) |
| 4b | 消息乱序：system 在中间 | 是 | `SystemMessageOrderError` | [chat_completion.ts:473-475](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L473-L475) |
| 5 | chat 请求带未知字段 `foo_bar` | **否**（黑名单为空） | — | — |
| 5' | completion 请求带 `best_of: 2` | 是 | `UnsupportedFieldsError` | [completion.ts:353-361](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/completion.ts#L353-L361)，黑名单在 [completion.ts:335-339](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/completion.ts#L335-L339) |

**操作步骤**：

1. 优先用 4.2.4 的 jest 直调法复现表中每一行（无需 GPU）；有浏览器环境的，再任选两行通过 `engine.chatCompletion` 走完整链路复现。
2. 对「否」的两行（1、5），在表旁写一句源码依据（分别是 config.ts 只检查下界、chat_completion.ts:409 黑名单为空）。
3. 思考并记录：类型系统能拦住表中的哪几行？（提示：`role: "function"` 与未知字段在**编译期**就会被 excess property / 联合类型检查拦下，只有绕过类型系统才到达运行时——这正是三层运行时校验存在的理由。）

### 任务 B：补一个测试用例（先确认、再动手）

1. 阅读 [tests/openai_chat_completion.test.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/openai_chat_completion.test.ts)，你会发现 **`stream: true` + `n: 2` 的 `StreamingCountError` 用例已经存在**（[tests/openai_chat_completion.test.ts:81-94](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/openai_chat_completion.test.ts#L81-L94)）——先跑 `npx jest tests/openai_chat_completion.test.ts` 确认它通过，并在源码 [chat_completion.ts:490-493](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L490-L493) 找到对应实现，完成「先在源码中确认行为」这一步。
2. 既然正向用例已有人写，补仓库**尚缺**的互补用例（**示例代码**）：

```ts
test("Streaming with n=1 is valid (complement of StreamingCountError)", () => {
  const request: ChatCompletionRequest = {
    stream: true,
    n: 1,
    messages: [{ role: "user", content: "Hello! " }],
  };
  expect(() =>
    postInitAndCheckFields(
      request,
      "Llama-3.1-8B-Instruct-q4f32_1-MLC",
      ModelType.LLM,
    ),
  ).not.toThrow();
});
```

3. 把用例追加进 `describe("Check chat completion unsupported requests")` 块末尾，重新运行该测试文件；再用 `npx prettier tests/openai_chat_completion.test.ts --check` 检查格式（仓库 lint 门禁要求 prettier 通过）。

**预期结果**：新增用例通过；全文件绿。注意本任务是练手性质——若想真正提交上游，请按 u7-l5 的贡献流程开分支、跑 `npm run lint` 与 `npm test` 全量通过后再提 PR。

## 6. 本讲小结

- `Chat` / `Completions` 是纯转发的门面类：`engine.chat.completions.create()` 与 `engine.chatCompletion()` 等价，前者只为兼容 OpenAI SDK 的调用姿势；协议类型直接采用自 openai-node，WebLLM 私有扩展收进 `extra_body`。
- 校验分三层：**协议层** `postInitAndCheckFields` 管结构（消息顺序、字段配对、tools 白名单）、**引擎层** `postInitAndCheckGenerationConfigValues` 管数值范围（temperature、max_tokens 等）、**会话层** `getConversationFromChatCompletionRequest` 管角色分发（非法 role 在此抛出）。
- `postInitAndCheckFields` 不只抛错，还会**原位改写请求**——Hermes 函数调用时会注入系统消息并覆写 `response_format`，下游拿到的是规整后的请求。
- 流式/非流式靠「可辨识联合 + 三重载 + 运行时 `if (request.stream)`」在类型与运行时双侧保持一致；跨字段约束（`stream` 与 `n`、`stream_options`）类型管不了，只能运行时判。
- `UnsupportedFieldsError` 机制是数据驱动的黑名单：chat 的黑名单当前为**空**（未知字段不报错），completion 的黑名单含 `suffix` / `user` / `best_of`——「报未知字段」要在 completion 上才能复现。
- 两处「文档与实现不一致」要留心：`temperature` 注释说 0-2 但只检查 `< 0`；接口注释说 `model` 被排除但实际有可选 `model` 字段——以代码为准。

## 7. 下一步学习建议

本讲把协议层的「骨架与门卫」讲完了，下一讲进入协议层最重的业务特性：

1. **u6-l2 函数调用 Function Calling**：本讲 4.2 已看到 Hermes 硬编码改写的「一半」，下一讲完整追踪 `tools` / `tool_choice` 如何渲染进对话模板（`getFunctionCallUsage` 与 conversation.ts 的工具 schema 注入），以及模型输出的 `tool_calls` 如何被解析回来。
2. **u6-l3 JSON Mode 与结构化输出**：本讲关卡 6 只讲了 `ResponseFormat` 的字段配对校验，下一讲深入 `GrammarMatcher` 如何在解码时逐 token 施加语法约束。
3. 想巩固本讲内容，可通读 [tests/openai_chat_completion.test.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/openai_chat_completion.test.ts) 全文——484 行测试几乎是 `postInitAndCheckFields` 的逐关卡文档化；再对照 [src/error.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts) 把每个错误类的触发条件默想一遍，错误体系就真正内化了。
