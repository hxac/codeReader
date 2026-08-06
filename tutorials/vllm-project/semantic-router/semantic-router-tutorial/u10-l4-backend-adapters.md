# 多后端适配（OpenAI/Anthropic/Response API/ImageGen）

## 1. 本讲目标

本讲承接 u5-l1（请求体处理与入口解析）和 u10-l1（插件链架构），进入 Semantic Router（以下简称 SR）的「协议适配层」——也就是把**多种外部 API 形态**翻译成 SR 内部统一表示、再把内部表示翻译回客户端期望形态的那一层代码。

SR 的内部流水线（决策、信号、选择）统一跑在一个**协议无关**的中间表示上：请求侧是 OpenAI 的 `*openai.ChatCompletionNewParams`，响应侧是 OpenAI 的 `*openai.ChatCompletion`。但真实世界里的客户端与后端并不都讲 OpenAI Chat Completions：客户端可能用 Anthropic 的 `/v1/messages`、也可能用 OpenAI 的 **Response API** `/v1/responses`；后端里还混着图像生成服务。于是 SR 需要一组适配器做「进站翻译 → 跑内核 → 出站翻译」。

学完后你应当能够：

- 说清 `pkg/anthropic` 如何在 OpenAI 与 Anthropic 两种格式之间**双向翻译**，什么是「有损翻译纪律（lossy translation discipline）」，以及 `AnthropicPassthrough`、`ir.IRExtensions` 这两条「旁路通道」各自解决什么问题；
- 描述 `pkg/responseapi` 的 `Translator` 如何把**有状态**的 Response API 请求翻译成无状态的 Chat Completions 请求，特别是 `previous_response_id` 这条「链」如何被还原成完整对话上下文；
- 解释 `pkg/imagegen` 的 `Backend` 接口、`Factory` 注册表、以及 `vllm_omni` / `openai` 两种图像生成后端的差异，并知道它在哪里被请求链路调用。

## 2. 前置知识

在进入源码前，先建立四个直觉。

**第一，SR 的「进站—内核—出站」三段式。** 回顾 u5-l1：一次请求进来，SR 先把请求体解析成内部表示（OpenAI 形态），再跑决策求值选模型，最后把后端返回的响应翻译回客户端要的形态再发出去。本讲的三个适配器都贴在这条链路的两端：

- **进站**：把客户端的非 OpenAI 形态（Anthropic / Response API）翻译成 OpenAI 形态；
- **出站**：把内核产出的 OpenAI 形态响应翻译回客户端期望的形态。

当客户端和后端讲不同协议时，SR 就像一个「同声传译」夹在中间。

**第二，什么是「有损翻译」。** OpenAI 和 Anthropic 两种格式并不一一对应。例如 Anthropic 有 `top_k`、`cache_control`（缓存标记）、`metadata.user_id`、多段 system prompt、`tool_result.is_error` 等字段，OpenAI 格式里**根本没有对应位置**。如果直接做 OpenAI→Anthropic 翻译再翻译回来，这些字段会「丢掉」。SR 的策略不是假装它们不存在，而是用**旁路通道（sidecar）**把这些「无家可归」的字段单独存起来，到出站时再**原样回放（replay）**到目标格式上。这就是本讲反复出现的核心思想。

**第三，有状态 vs 无状态 API。** OpenAI Chat Completions 是**无状态**的：每次请求都得把完整对话历史塞进 `messages[]`，服务端不替你记上下文。而 OpenAI Response API（`/v1/responses`）是**有状态**的：你可以只发新的一句，再用一个 `previous_response_id` 指向上一次的响应 ID，服务端自己把历史拼回来。SR 的后端模型讲的是无状态的 Chat Completions，所以要把有状态的 Response API 请求「摊平」成无状态请求——这就需要 SR 自己**存储**历史响应，并能沿着 `previous_response_id` 这条链往回走。

**第四，什么是「工厂模式」。** 图像生成有多种后端（OpenAI 官方 API、vLLM-Omni 自建服务）。它们的能力相似（都是「给提示词、出图片」）但请求/响应格式各异。SR 用一个 `Backend` 接口抽象它们的共同能力，再用一个 `Factory` 注册表按配置里的 `backend` 字符串选用具体实现。这与 u6-l2 的选择算法注册表、u8-l4 的嵌入提供者工厂是同一套设计手法。

> 一个贯穿本讲的术语约定：本讲把 OpenAI 形态的 `*openai.ChatCompletionNewParams` 称为 **IR 核心（IR core）**，把那些 OpenAI 装不下、需要旁路保存的字段称为 **IR 扩展（IR extensions）**。「IR」即 Intermediate Representation（中间表示）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/semantic-router/pkg/anthropic/client.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/anthropic/client.go) | Anthropic 适配器的公共入口：请求/响应双向翻译、头部构建、finish_reason 映射 |
| [src/semantic-router/pkg/anthropic/request_body.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/anthropic/request_body.go) | OpenAI→Anthropic 请求体翻译的私有助手：消息构建、采样参数、system、tools、cache_control 回放 |
| [src/semantic-router/pkg/anthropic/passthrough.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/anthropic/passthrough.go) | `AnthropicPassthrough` 旁路载体：从原始 Anthropic body 抽取 OpenAI 装不下的字段 |
| [src/semantic-router/pkg/ir/extensions.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/ir/extensions.go) | `IRExtensions`：协议无关的旁路信封，跨 inbound/outbound 传递 Anthropic-only 状态 |
| [src/semantic-router/pkg/responseapi/translator.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/responseapi/translator.go) | Response API ↔ Chat Completions 的核心翻译器 |
| [src/semantic-router/pkg/responseapi/types.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/responseapi/types.go) | Response API 的请求/响应类型定义（含 `PreviousResponseID`） |
| [src/semantic-router/pkg/responseapi/store.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/responseapi/store.go) | `StoredResponse`：存进响应库的内部表示，承载链式历史 |
| [src/semantic-router/pkg/responseapi/id.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/responseapi/id.go) | `resp_` / `item_` / `conv_` 前缀的 ID 生成 |
| [src/semantic-router/pkg/responsestore/interface.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/responsestore/interface.go) | `ResponseStore` 接口：`GetConversationChain` 沿 `previous_response_id` 回溯历史 |
| [src/semantic-router/pkg/extproc/req_filter_response_api.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/req_filter_response_api.go) | `ResponseAPIFilter`：把翻译器、响应库、链式回溯串进 ExtProc 请求链路 |
| [src/semantic-router/pkg/imagegen/interface.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/imagegen/interface.go) | `Backend` 接口、`GenerateRequest`/`GenerateResponse`、`Factory` 注册表 |
| [src/semantic-router/pkg/imagegen/backend_openai.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/imagegen/backend_openai.go) | OpenAI 图像生成后端实现 |
| [src/semantic-router/pkg/imagegen/backend_vllm_omni.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/imagegen/backend_vllm_omni.go) | vLLM-Omni 图像生成后端实现 |
| [src/semantic-router/pkg/config/image_gen_plugin.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/config/image_gen_plugin.go) | 图像生成的配置类型与按后端类型的解码 |
| [src/semantic-router/pkg/extproc/req_filter_modality.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/req_filter_modality.go) | `generateImage`：在模态路由里真正调用图像后端的入口 |

---

## 4. 核心概念与源码讲解

### 4.1 Anthropic 互转

#### 4.1.1 概念说明

`pkg/anthropic` 解决的问题是：当客户端用 Anthropic 的 `/v1/messages` 协议、而后端（或反过来）用 OpenAI 协议时，如何在这两种格式之间无损地来回翻译。

这里要先讲清楚 SR 的一个架构事实：**SR 内核只认 OpenAI 形态**。所以 Anthropic 适配器要服务两类场景，构成两个「单元（cell）」：

| 单元 | 客户端 | 后端 | 进站 | 出站 |
| --- | --- | --- | --- | --- |
| 逆向单元（inverse cell） | OpenAI | Anthropic | 无（已是 OpenAI） | `ToAnthropicRequestBody` 把请求转成 Anthropic；`ToOpenAIResponseBody` 把 Anthropic 响应转回 OpenAI |
| 对称出站单元（Anthropic→Anthropic） | Anthropic | 任意 | `ParseAnthropicRequest` 把 Anthropic 请求转成 OpenAI IR | `EmitAnthropicResponse` 把 OpenAI 响应转回 Anthropic |

两个单元都遵循同一条铁律——**有损翻译纪律**：当一个字段在源格式里有、在 OpenAI IR 里没有对应位置时，它不能被默默丢掉，而要被旁路保存，到出站时回放。区别只在于「旁路」用哪个载体：

- **逆向单元**用 `AnthropicPassthrough`（一个**每请求一次**的临时结构）；
- **对称单元**用 `ir.IRExtensions`（一个**协议无关**的旁路信封，随请求上下文走完全程）。

> 为什么需要两个不同的旁路？因为逆向单元的「丢失」发生在出站翻译的**同一刻**（OpenAI→Anthropic 时直接知道哪些 Anthropic 字段没翻译进来），所以可以现场塞进 `AnthropicPassthrough`；而对称单元的「丢失」横跨进站和出站两个阶段（进站 `ParseAnthropicRequest` 时丢、出站 `EmitAnthropicResponse` 时补），需要一个能跨阶段携带的信封，那就是 `IRExtensions`。

#### 4.1.2 核心流程

**请求翻译（OpenAI → Anthropic）** 的入口是 `ToAnthropicRequestBody`，它等价于传一个空旁路的 `ToAnthropicRequestBodyWithPassthrough`。整个流程可以用下面这段伪代码概括：

```
输入: openAIRequest (*openai.ChatCompletionNewParams), pt (*AnthropicPassthrough, 可空)
1. (systemPrompt, messages) = buildAnthropicMessages(openAIRequest.Messages)
     # 把 OpenAI messages 切成「system 文本」+「Anthropic 消息列表」
2. params = MessageNewParams{
     Model:     openAIRequest.Model,
     Messages:  messages,
     MaxTokens: resolveMaxTokens(openAIRequest),   # Anthropic 必填，缺省 4096
     System:    buildSystem(systemPrompt, pt),     # 优先用 pt.SystemBlocks
   }
3. applyOpenAIToolsToAnthropicParams(&params, openAIRequest)   # 只转 function 类型工具
4. applyToolsCacheControl(&params, pt)             # 回放 tools[i] 上的缓存标记
5. applyMessagesCacheControl(params.Messages, pt)  # 回放消息块上的缓存标记
6. applyUserMessageImageBlocks(params.Messages, pt)# 回放用户消息里的图片块
7. applyToolResultPassthrough(params.Messages, pt) # 回放 tool_result 的 is_error/数组内容
8. applySampling(&params, openAIRequest, pt)       # temperature/top_p/stop; top_k 来自 pt
9. applyMetadata(&params, openAIRequest, pt)       # metadata.user_id
10. return json.Marshal(params)
```

**响应翻译（Anthropic → OpenAI）** 的入口是 `ToOpenAIResponseBody`，把 `anthropic.Message` 翻成 `openai.ChatCompletion`：

```
输入: anthropicResponse ([]byte), model, ext (*ir.IRExtensions, 可空)
1. resp = json.Unmarshal(anthropicResponse) -> anthropic.Message
2. (toolCalls, content) = openAIToolCallsFromAnthropicContent(resp.Content)
     # text 块拼成 content 文本; tool_use 块转成 OpenAI tool_calls
3. extractThinkingBlocks(resp.Content, ext)              # 思维链签名存进 ext（旁路）
4. captureAnthropicStopReasonIntoExt(resp.StopReason, …) # stop_reason 存进 ext（旁路）
5. extractAnthropicUsageIntoExt(resp.Usage, ext)         # 缓存用量存进 ext（旁路）
6. finishReason = openAIFinishReasonFromAnthropic(resp.StopReason, len(toolCalls))
7. 组装 openai.ChatCompletion（id/object/choices/usage）并返回
```

注意第 3~5 步：当 `ext == nil` 时（逆向单元的旧调用方），这些 `extract*` / `capture*` 函数都是 **nil-safe** 的空操作，响应里那些 Anthropic-only 的状态会被静默丢弃——这正是「逆向单元的旧调用方逐字节不变」的由来。

#### 4.1.3 源码精读

**请求翻译的公共入口。** `ToAnthropicRequestBody` 是不带旁路的简版，内部直接委托给带旁路版本；这正是「传 nil 旁路时行为与旧版逐字节一致」的契约落点：

[src/semantic-router/pkg/anthropic/client.go:88-128](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/anthropic/client.go#L88-L128) —— `ToAnthropicRequestBody` 与 `ToAnthropicRequestBodyWithPassthrough`。后者先 `buildAnthropicMessages` 切出 system 与消息，组装 `MessageNewParams`（`MaxTokens` 来自 `resolveMaxTokens`、`System` 来自 `buildSystem`），再依次回放 tools 缓存标记、消息缓存标记、用户图片块、tool_result 旁路，最后应用采样与元数据。注意它把字段分成了两类：**有 OpenAI 对应的**（model/messages/max_tokens/system）直接映射，**没有 OpenAI 对应的**（cache_control/top_k/metadata/images/tool_result 细节）一律走 `applyXxx(pt)` 旁路回放。

**MaxTokens 是 Anthropic 的硬性必填项。** 这是 OpenAI 与 Anthropic 一个关键差异：OpenAI 的 `max_tokens` 可省略，Anthropic 的 `max_tokens` 必填。所以翻译时必须给一个确定值：

[src/semantic-router/pkg/anthropic/request_body.go:23-32](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/anthropic/request_body.go#L23-L32) —— `resolveMaxTokens` 按优先级取 `MaxCompletionTokens` → `MaxTokens` → 兜底常量 `DefaultMaxTokens = 4096`。这个兜底值在进站解析 `ParseAnthropicRequest` 时也用同样的 4096，保证两端对称。

**消息角色的拆分与转换。** OpenAI 把 system 当成 `messages[]` 里的一条角色为 `system` 的消息；Anthropic 把 system 单独放在顶层 `system` 字段、`messages[]` 只装 user/assistant。`buildAnthropicMessages` 就是做这个拆分：

[src/semantic-router/pkg/anthropic/request_body.go:395-433](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/anthropic/request_body.go#L395-L433) —— `buildAnthropicMessages` 遍历 OpenAI 消息，`OfSystem` 抽出 system 文本，`OfUser` 调 `userMessageBlocks`（保留图片块），`OfAssistant` 调 `assistantContentBlocks`（文本 + tool_use 块），`OfTool` 转成 Anthropic 的 `tool_result` 块并强制要求 `tool_call_id` 非空（缺失即报错）。

**采样参数与 top_k 旁路。** temperature / top_p / stop 都有 OpenAI 对应，直接搬；但 `top_k` 是 Anthropic 独有，只能从旁路来：

[src/semantic-router/pkg/anthropic/request_body.go:58-73](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/anthropic/request_body.go#L58-L73) —— `applySampling`。前三个 `if` 处理有 OpenAI 对应的采样参数，最后一个 `if pt != nil && pt.TopK != nil` 才把旁路里的 `top_k` 补回去。`applyMetadata`（[request_body.go:77-88](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/anthropic/request_body.go#L77-L88)）同理：`metadata.user_id` 优先取旁路、否则回退到 OpenAI 的 `user` 字段。

**旁路载体本身。** `AnthropicPassthrough` 是一个纯数据结构，承载所有「OpenAI 装不下」的 Anthropic 字段；它的生命周期只有一次请求：

[src/semantic-router/pkg/anthropic/passthrough.go:30-73](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/anthropic/passthrough.go#L30-L73) —— `AnthropicPassthrough` 结构。注意每个字段都是「指针/可空」语义（`TopK *int64`、`CacheControl map[…]`），**空值表示「入站没设、出站不要发」**，这是与「有就回放、没有就不发」纪律配套的约定。`BuildPassthroughFromAnthropicBody`（[passthrough.go:148-158](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/anthropic/passthrough.go#L148-L158)）是它的构造器，用 gjson 从原始 Anthropic body 里**宽容地**抽取这些字段（畸形 JSON 静默跳过，绝不因此让请求失败）。

**响应翻译与 finish_reason 映射。** Anthropic 的 `stop_reason` 取值比 OpenAI 的 `finish_reason` 多，需要「收缩」映射：

[src/semantic-router/pkg/anthropic/client.go:207-219](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/anthropic/client.go#L207-L219) —— `openAIFinishReasonFromAnthropic`。`max_tokens → length`、`tool_use → tool_calls`，其余（含 `pause_turn` / `refusal` / `stop_sequence`）一律坍缩成 `stop`。这种「丢失细节」由 `captureAnthropicStopReasonIntoExt`（[client.go:229-244](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/anthropic/client.go#L229-L244)）补救：当 `ext` 非空时，被坍缩掉的 `stop_reason` 原值被存进 `ext.AnthropicStopReason`，供出站发射器还原。

**IRExtensions：跨阶段的协议无关旁路。** 这是对称单元（Anthropic→Anthropic）用的信封。进站 `ParseAnthropicRequest` 往里塞、出站 `EmitAnthropicResponse` 从里读：

[src/semantic-router/pkg/ir/extensions.go:14-93](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/ir/extensions.go#L14-L93) —— `IRExtensions` 结构。它把 `SourceProtocol`、`SystemBlocks`、`CacheControl`、`Thinking`/`ThinkingSignatures`、`TopK`、`ServerTools`、缓存用量计数等全部收纳。文档注释里写得很清楚：**inbound 解析器填充它、outbound 发射器与插件读取它；`nil` 表示这次请求是纯 OpenAI 形态、没有扩展**。块 ID 用稳定的 JSON 路径（如 `messages[3].content[1]`）做键，这样即使插件改写了 IR 核心，旁路查找也不会错位。

**头部构建。** 翻译 body 之外还要翻译 HTTP 头：Anthropic 要 `anthropic-version`、`x-api-key`、`:path` 等，且要移除 OpenAI 的 `authorization`：

[src/semantic-router/pkg/anthropic/client.go:52-81](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/anthropic/client.go#L52-L81) —— `BuildRequestHeadersWithPassthrough` / `buildRequestHeaders`。头部优先级是「包默认 < 旁路 < profile 钉死值」：旁路的 `AnthropicVersion` 可覆盖包默认的 `2023-06-01`，而调用方在追加 profile 头部时按 Envoy 约定「后写的同键头胜出」，自然拿到最高优先级。`HeadersToRemove()`（[client.go:84-86](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/anthropic/client.go#L84-L86)）返回要删的 `authorization`、`content-length`。

#### 4.1.4 代码实践

**实践目标：** 用一个真实的 OpenAI Chat Completions 请求体，手工（或借助测试）跟踪 `ToAnthropicRequestBody` 对它做了哪些字段转换，验证「有对应直接映射、无对应走旁路」这条规律。

**操作步骤：**

1. 阅读 `pkg/anthropic/client_test.go`，找到调用 `ToAnthropicRequestBody` 的表驱动测试用例，观察输入的 OpenAI 请求体与期望的 Anthropic body。
2. 准备这样一段输入请求体（示例代码，非项目原有）：

   ```json
   {
     "model": "claude-sonnet",
     "messages": [
       {"role": "system", "content": "你是助手"},
       {"role": "user", "content": "你好"}
     ],
     "max_tokens": 512,
     "temperature": 0.7,
     "top_p": 0.9
   }
   ```

3. 对照 4.1.2 的伪代码，逐字段预测翻译结果：
   - `messages[0]`（system）→ 顶层 `system` 字段；
   - `messages[1]`（user）→ `messages[0]`；
   - `max_tokens` → `max_tokens`（命中 `resolveMaxTokens` 第二条分支）；
   - `temperature` / `top_p` → 直接出现在出站 body；
   - 没有 `top_k`、没有 `cache_control` → 这些字段**不出现在出站 body 里**。

**需要观察的现象：**

- 出站 Anthropic body 的 `system` 是字符串还是数组？（提示：当 `pt.SystemBlocks` 为空、且 systemPrompt 非空时，`buildSystem` 返回的是单元素数组，见 [request_body.go:38-54](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/anthropic/request_body.go#L38-L54)。）
- 出站 body 里**找不到** `top_k` 字段，因为它没有 OpenAI 来源、且你没传旁路。

**预期结果：** 出站 body 形如 `{"model":"claude-sonnet","messages":[{"role":"user","content":[{"type":"text","text":"你好"}]}],"max_tokens":512,"system":[{"type":"text","text":"你是助手"}],"temperature":0.7,"top_p":0.9}`。

**待本地验证：** 上面这个具体 JSON 是否逐字段与真实 `go test` 输出一致，请在本地运行 `cd src/semantic-router && go test ./pkg/anthropic/ -run TestToAnthropicRequestBody -v` 后比对（具体测试函数名以仓库现状为准）。

#### 4.1.5 小练习与答案

**练习 1：** 如果 OpenAI 请求里**既没给** `max_tokens` **也没给** `max_completion_tokens`，翻译到 Anthropic 时 `max_tokens` 会是多少？为什么必须给一个确定值？

> **答案：** 取兜底常量 `DefaultMaxTokens = 4096`（见 `resolveMaxTokens` 的 default 分支）。必须给确定值是因为 Anthropic API 把 `max_tokens` 列为**必填**字段，缺省会被上游直接拒收。

**练习 2：** 一个 OpenAI assistant 消息里带了 `tool_calls`，翻译成 Anthropic 时会变成什么？反过来，Anthropic 响应里的 `tool_use` 块又会被 `ToOpenAIResponseBody` 翻成什么？

> **答案：** 进站方向，`assistantContentBlocks` 把每个 tool_call 翻成一个 `tool_use` 内容块（含 `id`、`name`、解析后的 `input`）；如果 tool_call 缺 `id` 或函数名，直接返回错误。出站方向，`openAIToolCallsFromAnthropicContent` 把 `tool_use` 块翻回 OpenAI 的 `tool_calls`（`type:"function"`、`arguments` 为 input 的 JSON 文本，空 input 兜底 `"{}"`），`finish_reason` 被映射成 `tool_calls`。

**练习 3：** `captureAnthropicStopReasonIntoExt` 在 `ext == nil` 时会有任何副作用吗？这个设计是为了保证什么？

> **答案：** 不会有任何副作用——它开头就 `if ext == nil { return }`。这个 nil-safe 设计是为了让旧的逆向单元调用方（传 `nil` ext）**逐字节保持旧行为**：那些 Anthropic-only 的 stop_reason 被静默丢弃，响应的 OpenAI 外壳完全不携带它们；只有对称单元（传非 nil ext）才把细节存进旁路供出站还原。

---

### 4.2 Response API 翻译

#### 4.2.1 概念说明

`pkg/responseapi` 解决的是一个「状态错配」问题：客户端用 OpenAI 的 **Response API**（`/v1/responses`，有状态、靠 `previous_response_id` 串对话），而 SR 背后的 LLM 后端讲的是 **Chat Completions**（无状态、每次要把完整 `messages[]` 都发过去）。

Response API 与 Chat Completions 在形态上有几处关键不同：

| 维度 | Response API | Chat Completions |
| --- | --- | --- |
| 状态 | 有状态，服务端存历史 | 无状态，每次发全量 |
| 上下文衔接 | `previous_response_id` 指向上一次响应 | 把历史全部塞进 `messages[]` |
| 系统指令 | 顶层 `instructions` | `messages[]` 里 role=`system` 的一条 |
| 输入 | `input` 可以是字符串或数组 | `messages[]` 必须是数组 |
| 输出 | `output[]` 含 message / function_call / function_call_output 等条目 | `choices[].message` |
| 标识 | `resp_xxxx` / `item_xxxx` / `conv_xxxx` | 无对应链式 ID |

所以 SR 要做的事是：**在请求阶段**把 Response API 请求（连同它通过 `previous_response_id` 引用的历史）摊平成一个完整的 Chat Completions 请求；**在响应阶段**把 Chat Completions 响应重新「状态化」成一个带新 `resp_` ID、指回旧 ID 的 Response API 响应，并存进响应库供下次回溯。

这里的核心机制是**链式回溯**：SR 自己维护一个 `ResponseStore`（内存或 Redis 后端），存每一轮的请求输入和模型输出；当新请求带了 `previous_response_id`，SR 就沿这条链往回走，把历史逐条还原成 chat 消息，拼到当前请求前面。

#### 4.2.2 核心流程

**请求翻译（Response API → Chat Completions）** 由 `Translator.TranslateToCompletionRequest` 完成，但它需要一个**已经回溯好的历史**作为输入。完整的链路在 ExtProc 的 `ResponseAPIFilter.TranslateRequest` 里：

```
TranslateRequest(body):
1. 解析 body 为 ResponseAPIRequest
2. 若 req.Input 为空 → 不是 Response API 请求，返回 nil
3. 生成新 respCtx.GeneratedResponseID = GenerateResponseID()
4. detectImageGenTool(req)  # 扫描 tools 里有没有 image_generation
5. 若 req.PreviousResponseID 非空:
       history = store.GetConversationChain(ctx, req.PreviousResponseID)
       # 沿 previous_response_id 一路回溯，按时间正序（最旧在前）返回
6. respCtx.ConversationID = determineConversationID(req, history)
       # 优先级: req.conversation_id > 链首响应的 conv_id > 新生成
7. completionReq = translator.TranslateToCompletionRequest(req, history)
8. 注入 stream / metadata 等传输字段
9. return (respCtx, translatedBody)
```

第 5 步是关键：`GetConversationChain` 沿 `previous_response_id` 回溯整条链。第 7 步里，`TranslateToCompletionRequest` 把历史和当前输入拼成 `messages[]`：

```
TranslateToCompletionRequest(req, history):
1. instructions = resolveInstructions(req, history)
       # req.instructions 优先；否则取历史里最近一条非空 instructions
2. messages = []
3. if instructions != "": messages += [system(instructions)]
4. messages += buildHistoryMessages(history)
       # 对每条历史响应: 先把它的 Input 逐项转成消息，再把它的 Output 逐项转成消息
5. messages += parseInput(req.Input)
       # input 是字符串 → 一条 user 消息；是数组 → 逐项转（支持多模态图片）
6. completionReq = {Model: req.Model, Messages: messages}
7. 可选字段搬运: temperature / top_p / max_output_tokens→max_tokens / stream / tools / tool_choice
8. return completionReq
```

**响应翻译（Chat Completions → Response API）** 由 `TranslateToResponseAPIResponse` 完成，把 `choices` 重新状态化：

```
TranslateToResponseAPIResponse(req, resp, previousResponseID):
1. responseID = GenerateResponseID()
2. output = []
3. 对每个 choice:
       if msg.Content != "": output += [message 条目(output_text 类型)]
       对每个 tool_call:       output += [function_call 条目(name/arguments/call_id)]
4. usage = {Input/Output/Total tokens}
5. return ResponseAPIResponse{
       ID: responseID, PreviousResponseID: previousResponseID,
       Output: output, OutputText: 拼接文本, Usage: usage,
       回显 req 的 instructions/tools/metadata/采样参数 ...
   }
```

随后 `ResponseAPIFilter` 会把这条新响应**存进响应库**（默认 `store=true`），成为下一次请求可回溯的历史节点，闭合链式状态。

#### 4.2.3 源码精读

**请求类型与链式 ID。** Response API 请求的核心字段，注意 `PreviousResponseID` 是「有状态」的根源：

[src/semantic-router/pkg/responseapi/types.go:13-56](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/responseapi/types.go#L13-L56) —— `ResponseAPIRequest`。`Input` 用 `json.RawMessage` 保留原始字节，因为 input 既可能是字符串也可能是数组，翻译时再按需解析（见 `parseInput`）。`Store *bool` 控制是否落库（默认 true），`PreviousResponseID` 是链式衔接键。

**ID 生成。** Response API 用带前缀的随机十六进制串做 ID，遵循 OpenAI 约定：

[src/semantic-router/pkg/responseapi/id.go:11-46](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/responseapi/id.go#L11-L46) —— `resp_` / `item_` / `msg_` / `conv_` 四个前缀常量与对应生成函数。随机源用 `crypto/rand`，失败时退化为定长计数器兜底（绝不因随机源故障让请求失败）。`IsValidResponseID` 用前缀 + 长度做合法性校验。

**链式回溯的核心调用点。** 这是本模块最关键的一段——把 `previous_response_id` 变成完整历史的入口：

[src/semantic-router/pkg/extproc/req_filter_response_api.go:121-127](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/req_filter_response_api.go#L121-L127) —— 当 `req.PreviousResponseID != ""` 时调 `f.store.GetConversationChain`。注意错误处理：只有「非 NotFound」的错误才记 warn，`ErrNotFound` 被静默忽略（链断了就当没有历史，不阻断请求）。

**GetConversationChain 的契约。** 回溯语义由接口注释精确规定：

[src/semantic-router/pkg/responsestore/interface.go:32-35](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/responsestore/interface.go#L32-L35) —— `GetConversationChain`：「从给定 responseID 出发、沿 `previous_response_id` 往回走，返回**按时间正序（最旧在前）**的全部响应」。这个「最旧在前」的顺序直接决定了 `buildHistoryMessages` 拼出来的对话顺序正确。后端实现有内存和 Redis 两种（见 [interface.go:117-123](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/responsestore/interface.go#L117-L123) 的 `MemoryStoreType` / `RedisStoreType`）。

**历史拼装。** 拿到历史后，每条 `StoredResponse` 的 Input 与 Output 都要转成 chat 消息：

[src/semantic-router/pkg/responseapi/translator.go:37-58](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/responseapi/translator.go#L37-L58) —— `buildHistoryMessages`。对每条历史响应，先把它的 `Input` 逐项转成消息、再把它的 `Output` 逐项转成消息；遇到畸形条目只记 warn 并跳过（`continue`），不中断整条链。`outputItemToMessage`（[translator.go:312-349](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/responseapi/translator.go#L312-L349)）按条目类型分派：`message` → 对应 role 的消息、`function_call` → assistant 的 tool_calls、`function_call_output` → tool 消息（带 `tool_call_id`）。

**指令回退。** Response API 的 `instructions` 是可选的，且可能只在前几轮设过；当前轮没设时要回溯取：

[src/semantic-router/pkg/responseapi/translator.go:24-34](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/responseapi/translator.go#L24-L34) —— `resolveInstructions`：req 自己的 instructions 优先；否则遍历历史取最近一条非空；都没有就返回空串（不加 system 消息）。

**请求翻译主体。** 把指令、历史、当前输入、采样/工具参数组装成 Chat Completions 请求：

[src/semantic-router/pkg/responseapi/translator.go:60-114](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/responseapi/translator.go#L60-L114) —— `TranslateToCompletionRequest`。注意几个搬运细节：`max_output_tokens` → `max_tokens`、流式时设 `stream_options.include_usage`、`temperature`/`top_p` 用指针判空后搬运。工具处理见下一步。

**工具过滤：只放行 function 类型。** Response API 的 `tools` 里可能混着内置工具（`image_generation`、`code_interpreter`、`web_search`），这些不该透传给 Chat Completions 后端：

[src/semantic-router/pkg/responseapi/translator.go:356-365](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/responseapi/translator.go#L356-L365) —— `convertTools` 只保留 `ToolTypeFunction` 且 `Function` 非空的工具；内置工具被有意剥离（由路由器自身处理，如图像生成走模态路由）。`convertToolChoice`（[translator.go:118-137](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/responseapi/translator.go#L118-L137)）把字符串/对象两种 tool_choice 形态映射到 SDK 的联合参数。

**响应翻译主体。** 把 Chat Completions 的 choices 重新状态化成 Response API 的 output 条目：

[src/semantic-router/pkg/responseapi/translator.go:139-205](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/responseapi/translator.go#L139-L205) —— `TranslateToResponseAPIResponse`。文本内容 → `ItemTypeMessage` 条目（`output_text` 内容块），tool_calls → `ItemTypeFunctionCall` 条目；`outputText` 用 `strings.Builder` 拼接所有文本；usage 直接搬运。生成的新响应带新 `resp_` ID、`PreviousResponseID` 指回旧 ID，并回显 req 的 instructions/tools/metadata/采样参数（让客户端拿到的响应字段完整）。

**存回响应库闭合链。** 翻译完的响应会被落库，成为下一轮可回溯的节点：

[src/semantic-router/pkg/extproc/req_filter_response_api.go:239-251](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/req_filter_response_api.go#L239-L251) —— `maybeStoreTranslatedResponse`：`shouldStore = req.Store == nil || *req.Store`（默认存），且库启用时把响应转成 `StoredResponse` 存下。`StoredResponse`（[store.go:7-52](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/responseapi/store.go#L7-L52)）同时保存 Input 与 Output，这正是它能被 `buildHistoryMessages` 还原成完整对话的原因。

#### 4.2.4 代码实践

**实践目标：** 描述 Response API 如何把 `previous_response_id` 还原成完整上下文，并追踪一次两轮对话在 SR 内部的「摊平」过程。

**操作步骤：**

1. 阅读 `pkg/responseapi/roundtrip_test.go`，找到模拟多轮 `previous_response_id` 链的测试用例，观察 `GetConversationChain` 的 mock/stub 返回了什么、最终拼出的 Chat Completions `messages[]` 长什么样。
2. 假设两轮对话如下（示例代码，非项目原有）：

   **第 1 轮请求** `POST /v1/responses`：
   ```json
   {"model":"m","input":"我叫小明","instructions":"你是助手"}
   ```
   假设 SR 翻译后得到回复文本 `"你好，小明"`，生成响应 ID `resp_AAA`，并存入响应库（Input=[`我叫小明`]，Output=[`你好，小明`]，instructions=`你是助手`）。

   **第 2 轮请求** `POST /v1/responses`：
   ```json
   {"model":"m","input":"我叫什么？","previous_response_id":"resp_AAA"}
   ```
   注意第 2 轮**没有** `instructions`、也没有重复发第 1 轮的内容，只给了 `previous_response_id`。

3. 对照 4.2.2 的流程跟踪第 2 轮：
   - 第 5 步：`GetConversationChain(ctx, "resp_AAA")` 返回 `[resp_AAA 那条 StoredResponse]`；
   - 第 7 步 `TranslateToCompletionRequest`：
     - `resolveInstructions`：req 没给 → 回溯历史取到 `"你是助手"`；
     - `buildHistoryMessages`：把 resp_AAA 的 Input（`我叫小明` → user）和 Output（`你好，小明` → assistant）转成两条消息；
     - `parseInput`：当前 `input`（`我叫什么？`）→ 一条 user 消息。

**需要观察的现象：** 第 2 轮虽然只发了一句，但最终送给后端的 Chat Completions 请求的 `messages[]` 应当是完整的四条：

```
[
  {role: system,    content: "你是助手"},   ← resolveInstructions 回溯而来
  {role: user,      content: "我叫小明"},   ← 历史 Input
  {role: assistant, content: "你好，小明"},  ← 历史 Output
  {role: user,      content: "我叫什么？"}   ← 当前 input
]
```

**预期结果：** 后端因此能正确回答「小明」——它收到了完整上下文。这验证了 SR 用「自管存储 + 链式回溯」把有状态 Response API 适配到了无状态后端。

**待本地验证：** 上述 `messages[]` 的精确顺序与字段名（尤其 system 消息是否被加在最前、assistant 历史是否带 tool_calls 形态），请运行 `cd src/semantic-router && go test ./pkg/responseapi/ -run TestRoundtrip -v` 后比对真实输出。

#### 4.2.5 小练习与答案

**练习 1：** 如果第 2 轮请求的 `previous_response_id` 指向一个**根本不存在**的 ID，SR 会怎么做？请求会失败吗？

> **答案：** 不会失败。`TranslateRequest` 里对 `GetConversationChain` 的错误做了判断：`errors.Is(err, responsestore.ErrNotFound)` 时静默忽略、不记 warn，`history` 为空，于是当成一次全新对话处理（`resolveInstructions` 取空、`buildHistoryMessages` 拼出空历史）。请求照常进行。

**练习 2：** 一个 Response API 请求带了 `tools: [{"type":"image_generation"}, {"type":"function","function":{...}}]`，翻译到 Chat Completions 时 `tools` 会变成什么？

> **答案：** `convertTools` 只保留 `ToolTypeFunction` 的工具，`image_generation` 这种内置工具会被剥离。同时 `detectImageGenTool`（[req_filter_response_api.go:83-94](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/req_filter_response_api.go#L83-L94)）会把 `HasImageGenerationTool` 置真并抽出 `ImageGenToolParams`，供后续模态路由（见 4.3）使用——内置工具由路由器自身而非后端 LLM 处理。

**练习 3：** `StoredResponse` 为什么要同时存 Input 和 Output？只存 Output 行不行？

> **答案：** 不行。`buildHistoryMessages` 还原历史时，对每条历史响应既要还原它的 Input（用户当时说了什么）也要还原它的 Output（模型当时回了什么），二者共同构成对话的「用户轮—助手轮」配对。只存 Output 会丢失用户的历史发言，对话链就断了。这也是 `toStoredResponse` 在落库前用 `parseResponseAPIInputItems(req.Input)` 把当前输入也存进去的原因。

---

### 4.3 图像生成工厂

#### 4.3.1 概念说明

`pkg/imagegen` 解决的问题是：当一次请求被判定向「图像（DIFFUSION）」模态路由（见 u10-l1 的插件链与模态检测）时，SR 需要调用一个**图像生成后端**把提示词变成图片。后端可能有多种——OpenAI 官方的图像 API、自建的 vLLM-Omni 服务——它们的请求/响应格式各异，但抽象能力一致。

SR 用经典的「接口 + 工厂」来收敛这种差异：

- **`Backend` 接口**定义所有图像后端的共同能力：报名字、生图、健康检查；
- **`GenerateRequest` / `GenerateResponse`** 是一套**超集（superset）**请求/响应模型，把两种后端用到的参数都收进来，由各后端自行取用自己关心的字段；
- **`Factory`** 是一个 `name → constructor` 的注册表，按配置里的 `backend` 字符串选用具体实现。

这套设计与 u6-l2 的选择算法注册表、u8-l4 的嵌入提供者工厂同源：**用字符串配置切换实现，新增后端只需 `Register` 一个构造器**。

#### 4.3.2 核心流程

图像生成的调用发生在模态路由的请求阶段。当模态检测判定为 DIFFUSION 时，`OpenAIRouter.generateImage` 会：

```
generateImage(ctx, cfg, diffusionModel):
1. (pluginCfg, promptPrefixes) = resolveImageGenConfig(cfg, decision, diffusionModel)
       # 优先用 decision.plugins[image_gen]；否则回退 legacy model_config
2. backend = imagegen.CreateBackend(pluginCfg)
       # 全局 DefaultFactory 按 pluginCfg.Backend 字符串选构造器
3. genReq = GenerateRequest{
       Prompt: ExtractImagePrompt(ctx.UserContent, promptPrefixes),  # 剥前缀
       Width:  pluginCfg.DefaultWidth,
       Height: pluginCfg.DefaultHeight,
   }
4. 若 Response API 带了 image_generation 工具参数: 用其 size/quality/model 覆盖 genReq
5. genResp = backend.GenerateImage(ctx, genReq)   # 真正调用后端
6. 记录 metrics（成功/失败 + 延迟）
7. return ImageGenResult{ImageURL, ImageBase64, RevisedPrompt, Model, ResponseText}
```

工厂内部 `Create` 的分派很简单：

```
Factory.Create(cfg):
1. constructor = f.backends[cfg.Backend]   # 按 backend 字符串查构造器
2. 若不存在 → 报错 "unknown image generation backend"
3. return constructor(cfg)                  # 用配置构造具体后端
```

`NewFactory` 在构造时注册了两个内置后端：`"vllm_omni"` 与 `"openai"`。

#### 4.3.3 源码精读

**Backend 接口与请求/响应模型。** 这是对所有图像后端的抽象：

[src/semantic-router/pkg/imagegen/interface.go:11-20](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/imagegen/interface.go#L11-L20) —— `Backend` 接口三个方法：`Name()`、`GenerateImage(ctx, *GenerateRequest)`、`HealthCheck(ctx)`。

[src/semantic-router/pkg/imagegen/interface.go:22-71](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/imagegen/interface.go#L22-L71) —— `GenerateRequest`（prompt、negative_prompt、width/height、num_inference_steps、guidance_scale、seed、model、quality、style）与 `GenerateResponse`（ImageURL、ImageBase64、RevisedPrompt、Model、Backend）。注意 `GenerateRequest` 是**超集**：`quality`/`style` 是 OpenAI 专属、`num_inference_steps`/`guidance_scale` 是扩散模型专属，两类字段并存，各后端只取自己需要的。

**工厂注册表与分派。** 这是「按字符串切实现」的核心：

[src/semantic-router/pkg/imagegen/interface.go:74-108](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/imagegen/interface.go#L74-L108) —— `Factory`：`backends` 是 `map[string]func(*config.ImageGenPluginConfig)(Backend, error)`；`NewFactory` 注册 `vllm_omni` 与 `openai` 两个内置项；`Create` 按 `cfg.Backend` 查表，未知名直接报错。`Register` 是开放的，外部可注册更多后端。

**全局工厂与便捷函数。** 为了让调用方不必每次手动 `NewFactory`：

[src/semantic-router/pkg/imagegen/interface.go:110-116](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/imagegen/interface.go#L110-L116) —— `DefaultFactory` 是包级全局实例，`CreateBackend(cfg)` 是它的薄封装。`generateImage`（见下）调用的就是它。

**OpenAI 后端。** 走 OpenAI 官方图像 API，鉴权用 Bearer token：

[src/semantic-router/pkg/imagegen/backend_openai.go:26-61](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/imagegen/backend_openai.go#L26-L61) —— `NewOpenAIBackend`：要求 `api_key` 非空（否则报错），`base_url` 缺省 `https://api.openai.com`，`model` 缺省 `gpt-image-1`，超时缺省 60s。配置解码用 `cfg.OpenAIBackendConfig()`（带类型校验，backend 类型不符会报错）。

[src/semantic-router/pkg/imagegen/backend_openai.go:68-152](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/imagegen/backend_openai.go#L68-L152) —— `GenerateImage`：组装 `{model, prompt, n:1, size:"WxH", quality, style}`，POST `/v1/images/generations`，带 `Authorization: Bearer`。响应里图片既可能是 URL 也可能是 `b64_json`——后者会被同时填进 `ImageBase64` 和一个 `data:image/png;base64,...` 形式的 `ImageURL`，方便上层统一处理。

**vLLM-Omni 后端。** 走自建的多模态服务，复用 Chat Completions 端点、把扩散参数塞进 `extra_body`：

[src/semantic-router/pkg/imagegen/backend_vllm_omni.go:28-53](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/imagegen/backend_vllm_omni.go#L28-L53) —— `NewVLLMOmniBackend`：要求 `base_url` 非空，超时缺省 120s（比 OpenAI 长，因为扩散生成更慢），并保存默认的 `steps`/`cfg_scale`/`seed`。

[src/semantic-router/pkg/imagegen/backend_vllm_omni.go:60-132](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/imagegen/backend_vllm_omni.go#L60-L132) —— `GenerateImage`：组装 `{messages:[{role:user, content:prompt}], model, extra_body:{width,height,num_inference_steps,true_cfg_scale,seed,negative_prompt}}`，POST `/v1/chat/completions`（**不带鉴权头**，因为 vLLM-Omni 通常在内网）。响应解析用 `extractImageURL` 从 `choices[0].message.content` 里找 `image_url` 类型的内容块抽出 URL；若 content 只是字符串则报「响应只有文本、没有图」。

**两种后端的差异速查：**

| 维度 | OpenAI 后端 | vLLM-Omni 后端 |
| --- | --- | --- |
| 端点 | `/v1/images/generations` | `/v1/chat/completions` |
| 鉴权 | `Authorization: Bearer <key>`（必填） | 无 |
| 模型缺省 | `gpt-image-1` | 配置指定 |
| 关键参数 | `size`/`quality`/`style` | `extra_body` 里的 `width/height/num_inference_steps/true_cfg_scale/seed/negative_prompt` |
| 图片返回 | URL 或 `b64_json` | content 里的 `image_url` 块 |
| 超时缺省 | 60s | 120s |

**真正调用后端的入口。** 模态路由里这段代码把工厂、配置解析、请求构建、指标记录串起来：

[src/semantic-router/pkg/extproc/req_filter_modality.go:571-631](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/req_filter_modality.go#L571-L631) —— `generateImage`：`resolveImageGenConfig` 取配置 → `imagegen.CreateBackend` 建后端 → 构建 `GenerateRequest`（用 `ExtractImagePrompt` 剥掉 `prompt_prefixes`）→ 若 Response API 带了 `image_generation` 工具参数则覆盖 size/quality/model → `backend.GenerateImage` → 记录 `metrics.RecordImageGenRequest(backend.Name(), 成功/失败, 延迟)`。注意它会把 OpenAI 专属的 `quality` 仅在有值时传，让 vLLM-Omni 后端自然忽略它不认的字段。

**配置：按后端类型的懒解码。** 这与 u10-l1 讲的「插件配置三层（DecisionPlugin / 强类型结构 / 访问器）+ StructuredPayload 懒解码」一脉相承：

[src/semantic-router/pkg/config/image_gen_plugin.go:286-309](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/config/image_gen_plugin.go#L286-L309) —— `ImageGenPluginConfig`：`Backend` 是类型字符串、`BackendConfig` 是 `*StructuredPayload`（原样保存，按需解码）。两个访问器 `VLLMOmniBackendConfig()` / `OpenAIBackendConfig()`（[image_gen_plugin.go:347-361](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/config/image_gen_plugin.go#L347-L361)）各自解码成对应的强类型配置，且都用 `decodeImageGenBackendConfig[T]` 做 backend 字符串校验——类型不符即报错，避免把 openai 配置误当 vllm_omni 解。

#### 4.3.4 代码实践

**实践目标：** 通过阅读配置与测试，理解「同一个 `Backend` 接口、两种实现」如何被工厂按字符串分派，并对比两种后端的请求构造差异。

**操作步骤：**

1. 阅读 `pkg/imagegen/interface_test.go`，找到测试 `Factory.Create` 在「已知后端」与「未知后端」两种输入下的行为断言。
2. 阅读 `pkg/imagegen/backend_vllm_omni_test.go`，观察 `VLLMOmniBackend.GenerateImage` 构造的请求体里 `extra_body` 的字段名（`num_inference_steps`、`true_cfg_scale` 等）。
3. 在 `pkg/imagegen/interface.go` 的 `NewFactory` 里临时**只注释掉** `f.Register("openai", NewOpenAIBackend)` 一行（示例操作，非永久改动），重新编译。

**需要观察的现象：**

- 注释前：`CreateBackend(&ImageGenPluginConfig{Backend:"openai", ...})` 能成功返回 `*OpenAIBackend`；
- 注释后：同样的调用会返回错误 `unknown image generation backend: openai`，而 `Backend:"vllm_omni"` 仍能正常创建。

**预期结果：** 这验证了「后端集合 = `NewFactory` 里 `Register` 过的项」，工厂是开放的、可扩展的——新增一种后端（如 `replicate`）只需写一个实现 `Backend` 接口的类型并 `Register("replicate", NewReplicateBackend)`，调用方代码完全不用改。

**待本地验证：** 上述注释实验需要本地修改源码后 `go build ./pkg/imagegen/`；若不便改源码，可改为阅读 `interface_test.go` 里对未知 backend 的错误断言来确认同一行为。

> 改完记得还原源码——本讲义要求不修改源码。

#### 4.3.5 小练习与答案

**练习 1：** 假设你要新增一个 `replicate` 图像后端，需要改哪些地方？`generateImage` 调用方需要改吗？

> **答案：** 写一个 `ReplicateBackend` 类型实现 `Backend` 接口（`Name`/`GenerateImage`/`HealthCheck`），并在 `NewFactory` 里加 `f.Register("replicate", NewReplicateBackend)`。可选地在 `config/image_gen_plugin.go` 加一个 `ReplicateImageGenConfig` 强类型与访问器、在 `Validate` 里加 `case "replicate"` 分支。**调用方 `generateImage` 完全不用改**——它只认 `Backend` 接口与 `CreateBackend`，这正是接口+工厂的价值。

**练习 2：** 为什么 `GenerateRequest` 要把 `quality`/`style`（OpenAI 专属）和 `num_inference_steps`/`guidance_scale`（扩散专属）这些互不相关的字段放进同一个结构？

> **答案：** 因为这是一个**超集**请求模型：它要同时服务两类后端。各后端在 `GenerateImage` 里只取自己关心的字段（OpenAI 取 quality/style、vLLM-Omni 取 num_inference_steps/guidance_scale），其余字段被自然忽略。这样上层（`generateImage`）只需构建一个统一的 `GenerateRequest`，不必为每种后端写一套请求构造逻辑。代价是结构里有些字段对某些后端无意义，但接口因此保持单一。

**练习 3：** Response API 请求里的 `tools:[{type:image_generation, size:"1024x1024", quality:"hd"}]` 是如何影响最终图像生成请求的？

> **答案：** `detectImageGenTool` 把它抽成 `ImageGenToolParams` 存进 `ResponseAPICtx`（见 4.2）。随后在 `generateImage` 里（[req_filter_modality.go:589-610](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/req_filter_modality.go#L589-L610)），这些参数会**覆盖** `GenerateRequest` 的默认 size/quality/model（`auto` 值被忽略）。于是客户端通过工具参数声明的大小/质量被透传给底层图像后端，实现「同一后端、按请求定制输出」。

---

## 5. 综合实践

把本讲三个适配器串起来，完成下面这个「端到端追踪」任务，画出一张完整的请求流转图。

**场景：** 一个 Anthropic SDK 客户端发起一次多轮对话，第二轮带了一个图像生成请求。

**任务：**

1. **进站（Anthropic 适配）**：客户端发的是 `/v1/messages`（Anthropic 形态）。追踪 `ParseAnthropicRequest` 如何把它翻译成 OpenAI IR 核心 + `IRExtensions` 旁路。找出：哪些字段进 IR 核心、哪些进 `IRExtensions`（提示：`top_k`、`cache_control`、`thinking`、多段 system）。

2. **内核**：IR 核心进入 u5 讲的请求处理主链路，跑信号→决策→选择。假设决策命中了一条挂了 `image_gen` 插件、且模态检测判定为 DIFFUSION 的路由。

3. **图像生成**：追踪 `generateImage` → `imagegen.CreateBackend` → `backend.GenerateImage`。对照 4.3 的差异表，说明若配置选了 `vllm_omni` 后端，请求会被发到哪个端点、带哪些 `extra_body` 参数。

4. **出站（Anthropic 适配）**：图像生成结果（一张图片 URL）要翻译回 Anthropic 形态交给客户端。追踪 `EmitAnthropicResponse` 如何把 OpenAI 形态的响应翻回 `anthropic.Message`，并说明 `IRExtensions` 里进站保存的 `cache_control` 等状态在出站如何被回放。

5. **对比**：如果同一个客户端用的是 Response API（`/v1/responses`）而非 Anthropic 协议，且带了 `previous_response_id`，进站会改走哪条翻译路径（4.2）？两种协议在「状态管理」上的根本差异是什么？

**预期产出：** 一张标注了「客户端协议 → 进站翻译器 → IR 核心(+旁路) → 内核 → 图像后端 → 出站翻译器 → 客户端」的流程图，并在每个翻译节点旁注明「哪些字段直接映射、哪些走旁路」。这个练习把本讲三个模块与本手册前面 u5（请求链路）、u10-l1（插件/模态）的知识连成一条线。

**待本地验证：** 流程图里各翻译器的具体字段映射，请对照本讲 4.1.3 / 4.2.3 / 4.3.3 引用的源码逐项核对；若想观察真实行为，可阅读 `pkg/anthropic/inbound_test.go` 与 `pkg/extproc/req_filter_modality*.go` 的相关测试。

## 6. 本讲小结

- SR 内核统一跑在 OpenAI 形态的 IR 核心上；`pkg/anthropic`、`pkg/responseapi`、`pkg/imagegen` 三个适配器分别负责 Anthropic 协议互转、Response API 状态化翻译、图像生成后端适配，它们都贴在请求链路的「两端」或作为插件被调用。
- **Anthropic 互转**遵循「有损翻译纪律」：有 OpenAI 对应的字段直接映射，没有的（`top_k`、`cache_control`、多段 system、`tool_result.is_error`、思维链签名等）走旁路通道——逆向单元用每请求一次的 `AnthropicPassthrough`，对称单元用跨阶段的协议无关信封 `ir.IRExtensions`；所有旁路函数都 nil-safe，保证旧调用方逐字节不变。
- **Response API 翻译**用「自管 `ResponseStore` + 链式回溯」把有状态 API 适配到无状态后端：`GetConversationChain` 沿 `previous_response_id` 回溯历史（最旧在前），`buildHistoryMessages` 把每条历史的 Input+Output 还原成 chat 消息，`resolveInstructions` 回溯取系统指令；响应翻译则把 Chat Completions 重新状态化成带新 `resp_` ID、指回旧 ID 的响应并存库，闭合链式状态。
- **图像生成工厂**用 `Backend` 接口 + `Factory` 注册表收敛多种后端：`GenerateRequest`/`GenerateResponse` 是覆盖两类后端的超集模型，`Create(cfg)` 按 `backend` 字符串分派；`openai` 后端走 `/v1/images/generations` 带 Bearer 鉴权，`vllm_omni` 后端走 `/v1/chat/completions` 把扩散参数塞进 `extra_body`、无鉴权；新增后端只需实现接口并 `Register`，调用方零改动。
- 三套机制共享同一条设计哲学：**用 IR/接口隔离差异，用旁路/超集保留细节，用注册表实现可扩展**——这与本手册 u6-l2（选择算法注册表）、u8-l4（嵌入提供者工厂）、u10-l1（插件配置懒解码）的手法一脉相承。

## 7. 下一步学习建议

- **若想深入 Anthropic 适配的流式分支**：阅读 `pkg/anthropic/client_stream.go` 与 `sse_out.go`，它们把本讲的非流式互转扩展到 SSE 流式场景，思维链块、`tool_use` 增量等都有对应的流式处理；这与 u5-l3 讲的响应体流式处理相接。
- **若想深入 Response API 的存储后端**：阅读 `pkg/responsestore/` 下的内存与 Redis 实现，看 `GetConversationChain` 如何在两种后端里实现「沿 previous_response_id 回溯、按时间正序返回」；这与 u9 讲的向量/缓存多后端抽象是同类问题。
- **若想把模态路由看全**：阅读 `pkg/extproc/req_filter_modality.go` 全文，看模态检测（classifier/keyword/hybrid）如何决定 AR/DIFFUSION/BOTH、`generateImage` 之外 `buildBothResponse` 如何把文本与图片合成多部分内容；这与 u10-l1 的插件链、u8 的分类信号系统汇合。
- **下一讲 u11-l1（API Server 管理 API）**将进入控制面，讲解 `pkg/apiserver` 暴露的管理端点（配置部署/ETag、classify、kbs 等），与本讲「数据面适配器」形成数据面/控制面的对照——回顾 u4-l2 的 Registry 容器可知二者如何共享运行时依赖。
