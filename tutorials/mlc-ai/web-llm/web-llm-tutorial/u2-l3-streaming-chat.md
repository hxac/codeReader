# 流式输出：AsyncIterable 与 ChatCompletionChunk

## 1. 本讲目标

学完本讲，你应该能够：

1. 使用 `stream: true` 发起流式请求，并用 `for await...of` 逐 chunk 消费 `AsyncIterable<ChatCompletionChunk>`。
2. 讲清楚流式路径的完整执行过程：`chatCompletion` 如何分流、`asyncGenerate` 如何在每次 `decode` 后产出一个增量 chunk、最后一个 chunk 与可选的 usage chunk 长什么样。
3. 解释增量文本是如何从管线的完整消息中「切片」出来的，以及 emoji（U+FFFD 替换字符）为什么会跳帧。
4. 掌握 `interruptGenerate()` 的中断时机：它为什么能「立即」生效、中断后 `finish_reason` 是什么、部分生成的内容去了哪里。

本讲承接 u2-l2（非流式 chatCompletion），聚焦同一个 API 的另一条分支。

## 2. 前置知识

### 2.1 同步迭代器与异步迭代器

你已经熟悉数组遍历：`for (const x of arr)`。这要求 `arr` 的所有元素**已经存在**。而 LLM 流式输出的特点是：文本是一个 token 一个 token 生成出来的，**未来元素尚不存在**。

JavaScript 为此提供了异步迭代协议：

- 一个对象如果实现了 `Symbol.asyncIterator` 方法，就是**异步可迭代对象**（AsyncIterable）。
- 每次取下一个元素返回一个 Promise，产出 `{ value, done }`。
- 消费侧语法是 `for await (const x of asyncIterable)`——每次循环都会**等待**下一个元素被生产出来。

TypeScript 里的 `AsyncGenerator<T>` 是语言内置的异步生成器类型：函数体内用 `yield` 产出元素，用 `return`（或自然结束）标记 `done`。**调用一个 async generator 函数并不会执行函数体**，只会返回生成器对象；函数体在你第一次迭代时才惰性执行。这个特性在本讲 4.1 会再次强调，它是理解「锁」在哪一瞬间被占用的关键。

### 2.2 增量（delta）与「打字机效果」

非流式接口一次性返回完整回答；流式接口把回答切成许多小片段（chunk）逐个送出，每个 chunk 只携带**新增加的那部分文本**（即 delta）。前端每收到一个 chunk 就把它追加到页面，于是用户看到文字像打字机一样逐字出现。

用一个式子描述：设管线内部维护的**完整**当前消息为 \( M_i \)（第 \( i \) 次产出时），上次切片的终点位置为 \( p_{i-1} \)，则本次增量为：

\[
\Delta_i = M_i[\,p_{i-1} : |M_i|\,], \qquad p_i = |M_i|
\]

 WebLLM 的流式实现就是围绕 `prevMessageLength`（即 \( p \)）做字符串切片，见 4.2。

### 2.3 与 OpenAI SSE 的关系

OpenAI 官方 API 的流式响应走 HTTP，用 Server-Sent Events 逐条推送 `chat.completion.chunk` JSON。WebLLM 运行在浏览器进程内，没有 HTTP 层，但**保留了同样的数据结构**（`object: "chat.completion.chunk"`、`delta`、`finish_reason`），只是传输载体从 SSE 换成了 `AsyncIterable`。这让熟悉 OpenAI SDK 的开发者几乎零成本迁移。

### 2.4 前置讲义回顾

u2-l2 已建立：请求校验在 `chatCompletion` 入口完成、采样参数摘入 `GenerationConfig`、每个候选执行一次 prefill 加多步 decode。本讲只改变一点——`stream: true` 时结果不是等全部生成完再返回，而是边生成边产出。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/engine.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts) | 引擎层：`chatCompletion` 的流式分流、`asyncGenerate` 生成器、`interruptGenerate` |
| [src/openai_api_protocols/chat_completion.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts) | 协议层：`ChatCompletionRequestStreaming/NonStreaming` 类型判别、`ChatCompletionChunk` 结构、流式相关校验 |
| [src/llm_chat.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts) | 管线层：`getMessage`（完整消息）、`stopped`/`triggerStop`/`getFinishReason`（停止状态机） |
| [src/types.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts) | `MLCEngineInterface` 中 `interruptGenerate` 的接口契约 |
| [src/support.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts) | `CustomLock`：同一模型一次只处理一个请求的互斥锁 |
| [examples/streaming/src/streaming.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/streaming/src/streaming.ts) | 官方流式示例，本讲实践的基底 |

## 4. 核心概念与源码讲解

### 4.1 流式重载入口：`stream` 字段如何分流

#### 4.1.1 概念说明

`engine.chatCompletion` 是一个**重载函数**：传非流式请求返回 `Promise<ChatCompletion>`（一个完整响应对象），传流式请求返回 `Promise<AsyncIterable<ChatCompletionChunk>>`（一个可逐段消费的异步序列）。判别器就是请求里的 `stream` 字段。

TypeScript 在**类型层面**区分这两种请求：`ChatCompletionRequestStreaming` 把 `stream` 声明为字面量类型 `stream: true`，`ChatCompletionRequestNonStreaming` 声明为 `stream?: false | null`，二者组成可辨识联合（discriminated union）。所以当你写 `stream: true` 时，编译器自动把返回类型收窄为异步可迭代——你不需要任何强制类型转换。

#### 4.1.2 核心流程

```text
engine.chatCompletion(request)
  ├─ 1. getLLMStates 选中管线 + postInitAndCheckFields 校验（含流式专项校验）
  ├─ 2. 摘出 GenerationConfig（与非流式完全一致）
  ├─ 3. lock.acquire()          ← 锁在这里被占用！
  └─ 4. if (request.stream)
        └─ return this.asyncGenerate(...)   ← 返回生成器对象，函数体暂不执行
             （非流式分支则继续执行 _generate，见 u2-l2）
```

流式专项校验有两条，都发生在协议层：

- 流式时 `n` 不能大于 1（引擎一次只能维护一条解码序列）→ `StreamingCountError`。
- `stream_options` 只能在 `stream: true` 时设置 → `InvalidStreamOptionsError`。

#### 4.1.3 源码精读

先看重载声明。两个签名对应两种请求，实现签名收窄为联合类型：

- [src/engine.ts:787-798](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L787-L798)：`chatCompletion` 的三个签名——非流式返回 `Promise<ChatCompletion>`，流式返回 `Promise<AsyncIterable<ChatCompletionChunk>>`。

分流点在方法体开头，锁的获取在分流**之前**：

- [src/engine.ts:827-841](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L827-L841)：先 `await lock.acquire()` 占住该模型的互斥锁，再判断 `request.stream`，为真时 `return this.asyncGenerate(...)`。

这里有个容易忽略的细节：`asyncGenerate` 是 async generator 函数，**调用它只是拿到生成器对象，函数体一行都还没跑**。真正执行要等你开始 `for await`。而锁已经在 `chatCompletion` 里被占用了——这意味着「拿到返回值但迟迟不消费」期间，锁一直是held状态，其他请求会排队。锁的释放被精心地放在生成器内部（见 4.2.3）。

类型层面的判别：

- [src/openai_api_protocols/chat_completion.ts:289-307](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L289-L307)：`ChatCompletionRequestNonStreaming`（`stream?: false | null`）与 `ChatCompletionRequestStreaming`（`stream: true`）组成联合类型 `ChatCompletionRequest`。
- [src/openai_api_protocols/chat_completion.ts:98-105](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L98-L105)：`stream` 与 `stream_options` 字段的文档注释——明确说明流式会被「一个空 chunk」终止。

两条流式专项校验：

- [src/openai_api_protocols/chat_completion.ts:490-493](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L490-L493)：`stream && n > 1` 时抛 `StreamingCountError`。
- [src/openai_api_protocols/chat_completion.ts:599-604](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L599-L604)：非流式却设置了 `stream_options` 时抛 `InvalidStreamOptionsError`。

返回的 chunk 结构（本讲的主角）：

- [src/openai_api_protocols/chat_completion.ts:362-407](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L362-L407)：`ChatCompletionChunk` 接口——同一流的所有 chunk 共享同一个 `id` 与 `created`；`object` 固定为 `"chat.completion.chunk"`；`usage` 仅在 `stream_options.include_usage` 时于最后一个 chunk 出现。
- [src/openai_api_protocols/chat_completion.ts:1081-1123](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L1081-L1123)：`ChatCompletionChunk.Choice` 与 `Choice.Delta`——中间 chunk 的 `finish_reason` 为 `null`（还没结束），增量文本放在 `delta.content`。
- [src/openai_api_protocols/chat_completion.ts:67-78](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L67-L78)：`chat.completions.create` 门面同样带流式重载，直接透传给 `engine.chatCompletion`。

#### 4.1.4 代码实践

**实践目标**：直观感受「中间 chunk `finish_reason` 为 null、最后一个 chunk 非 null」，并触发一次流式专属校验错误。

**操作步骤**：

1. 进入 `examples/streaming` 目录，执行 `npm install`，再 `npm start`（Parcel 会在 8888 端口起服务，见 [examples/streaming/package.json:5-8](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/streaming/package.json#L5-L8)）。
2. 打开浏览器控制台，观察示例已有的 `console.log(chunk)` 输出。
3. 把示例中的 for-await 循环改造为如下「示例代码」，单独打印每个 chunk 的关键字段：

```ts
// 示例代码：打印每个 chunk 的骨架
for await (const chunk of asyncChunkGenerator) {
  console.log(JSON.stringify({
    delta: chunk.choices[0]?.delta?.content ?? "",  // 本帧增量
    finish_reason: chunk.choices[0]?.finish_reason, // 中间帧为 null
    hasUsage: chunk.usage !== undefined,            // 仅最后可能为 true
  }));
}
```

4. 另起一轮请求，加上 `n: 2` 且保持 `stream: true`，用 try/catch 包住调用。

**需要观察的现象**：

- 步骤 3：绝大多数帧 `finish_reason` 为 `null`，只有最后一帧非 null；`hasUsage` 只在最后一帧可能为 true（示例请求设置了 `include_usage`）。
- 步骤 4：抛出 `StreamingCountError`，错误信息说明流式不支持 `n > 1`。

**预期结果**：与上述一致。具体错误文案以本地运行为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `chatCompletion` 要在返回 `asyncGenerate(...)` **之前**就 `lock.acquire()`？如果改成在生成器体内第一行才加锁，会有什么问题？

**答案**：`asyncGenerate` 是 async generator，调用它不执行函数体；若在体内才加锁，那么「`chatCompletion` 已返回、调用方还没开始 for-await」的窗口期内锁是空闲的，另一个请求可能插队成功，导致两个生成器交错使用同一条 KV cache，输出错乱。提前在 `chatCompletion` 中加锁，保证从 API 返回那一刻起该模型的解码权已被独占。

**练习 2**：`stream: true` 和 `stream_options: { include_usage: true }` 分别属于哪一层校验？后者单独使用会发生什么？

**答案**：`stream` 是请求联合类型的判别字段（类型层 + 运行时 `if (request.stream)` 分流）；`stream_options` 的合法性校验在协议层 `postInitAndCheckFields` 的第 8 条：不流式却设置它，抛 `InvalidStreamOptionsError`（[src/openai_api_protocols/chat_completion.ts:599-604](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L599-L604)）。它唯一的作用是让流式响应额外多出一个携带 `usage` 的收尾 chunk。

### 4.2 增量 chunk 的生成与拼接

#### 4.2.1 概念说明

`asyncGenerate` 是 `_generate()`（u2-l2 讲过的非流式主循环）的「可迭代版本」：把「prefill 一次 + decode N 次」的循环改写进一个 async generator，每完成一步就 `yield` 一个 chunk。

关键认知：**管线本身不产增量**。`LLMChatPipeline` 始终维护一份完整的当前输出消息（`outputMessage`，由 `getMessage()` 返回）。生成器在每一步之后读取这份完整消息，用 `prevMessageLength` 记住上次切到哪里，**切片得到本帧增量**。这就是 2.2 节公式的落地。

另外两件小事也在这一层处理：

- **emoji 防截断**：一个 emoji 常由多个 token 组成，解码到一半会出现替换字符 `�`（U+FFFD）。此时跳过本帧不产出，等凑齐完整 emoji 再一起给。
- **收尾协议**：流的末尾固定有一个「空 delta + 非 null `finish_reason`」的 chunk；若开启 `include_usage`，再追加一个 `choices` 为空、只带 `usage` 的 chunk。消费方据此知道流结束了、为什么结束、耗费多少。

#### 4.2.2 核心流程

```text
asyncGenerate(request, model, pipeline, chatConfig, genConfig)
  ├─ 0. 预处理：校验 genConfig、设置 seed（出错 → 释放锁并抛出）
  ├─ 1. 一次性量：created、id（整条流共享）、
  │      interruptSignal = false、prevMessageLength = 0
  ├─ 2. 定义 _getChunk(pipeline)：
  │      curMessage = pipeline.getMessage()        ← 完整消息
  │      若末尾 � 数量 % 4 !== 0 → 返回 undefined（跳帧）
  │      delta = curMessage.slice(prevMessageLength)
  │      prevMessageLength = curMessage.length
  │      返回 { delta: {content, role:"assistant"}, finish_reason: null, ... }
  ├─ 3. prefill(request, ...) → _getChunk → yield   ← 第一个 chunk（约等于 TTFT）
  ├─ 4. while (!pipeline.stopped()):
  │      若 interruptSignal → triggerStop(); break
  │      decode(pipeline) → _getChunk → yield
  ├─ 5. yield 最后一个 chunk：delta 为空（或 tool_calls），finish_reason = stop/length/tool_calls/abort
  ├─ 6. 若 stream_options.include_usage → yield usage chunk（choices: []）
  └─ 7. await lock.release()                        ← 锁在这里才释放
```

#### 4.2.3 源码精读

生成器开头的一次性准备：

- [src/engine.ts:500-518](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L500-L518)：`asyncGenerate` 的实现签名取得锁，并用 `"messages" in request` 判断这是 chat 请求还是纯文本补全请求——同一段代码同时服务 `chatCompletion` 与 `completion` 的流式分支。
- [src/engine.ts:519-532](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L519-L532)：预处理放在 try-catch 中，出错先释放锁再抛出——这是「锁的释放散布在各 catch 点」的第一处。
- [src/engine.ts:534-538](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L534-L538)：`created`、`id` 在这里生成，整条流的每个 chunk 复用同一对值；`interruptSignal` 清零、`prevMessageLength = 0`。

emoji 跳帧的判定函数：

- [src/engine.ts:540-550](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L540-L550)：`_countTrailingReplacementChar` 从消息末尾向前数连续的 `�` 字符数量。

chunk 的组装核心：

- [src/engine.ts:552-567](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L552-L567)：`_getChunk` 读取管线完整消息 `getMessage()`；若末尾 `�` 数量不是 4 的倍数则返回 `undefined`（本帧跳过）；否则 `slice(prevMessageLength)` 切出增量并推进游标——这正是 2.2 节公式 \( \Delta_i = M_i[p_{i-1}:|M_i|] \) 的实现。
- [src/engine.ts:573-588](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L573-L588)：组装 `ChatCompletionChunk`：`delta: { content: deltaMessage, role: "assistant" }`，`finish_reason: null`（还没结束）。同一函数的另一个分支组装 `Completion`（纯文本补全的流式帧，字段是 `text` 而非 `delta`），见 [src/engine.ts:590-604](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L590-L604)。

自回归主循环：

- [src/engine.ts:608-619](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L608-L619)：先 `prefill`（u2-l2 讲过：多轮对话会比对 Conversation 复用 KV cache），随后立刻产出第一个 chunk——所以**首帧到达时间约等于 prefill 耗时**，这就是 TTFT。
- [src/engine.ts:621-638](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L621-L638)：`while (!pipeline.stopped())` 循环——每轮先检查中断信号，再 `decode` 一步、取 chunk、`yield`。注意 `if (curChunk)` 的判空：跳帧时 `curChunk` 为 `undefined`，这一轮就不 yield。

收尾的两个 chunk：

- [src/engine.ts:645-685](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L645-L685)：最后一个 chunk——`finish_reason` 取自 `pipeline.getFinishReason()`（stop / length / tool_calls / abort）；普通请求 `delta` 为空对象 `{}`，函数调用请求则在最后一个 chunk 里一次性给出解析好的 `tool_calls`。
- [src/engine.ts:703-766](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L703-L766)：`stream_options.include_usage` 为真时追加 usage chunk——`choices: []`，`usage` 汇总 prompt/completion token 数与 `extra` 性能块（prefill/decode 速度、TTFT 等，字段口径与非流式响应一致，详见 u7-l3）。
- [src/engine.ts:768](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L768)：生成器正常走完才 `await lock.release()`——与 4.1 的加锁位置首尾呼应。

管线侧的「完整消息」从哪来：

- [src/llm_chat.ts:513-515](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L513-L515)：`getMessage()` 直接返回 `outputMessage`。
- [src/llm_chat.ts:1014-1027](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1014-L1027)：每步 `decode` 后 `processNextToken` 用 `tokenizer.decode(outputIds)` 重新解码出完整 `outputMessage`；若命中 stop 字符串还会在此截断。也就是说，流式的每一帧都是「重新解码的完整消息」的切片，而非增量解码。

#### 4.2.4 代码实践

**实践目标**：验证「拼接所有 delta 等于 `getMessage()`」，并看清 usage chunk 的形态。

**操作步骤**：

1. 在 [examples/streaming/src/streaming.ts:39-50](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/streaming/src/streaming.ts#L39-L50) 的循环里追加计数与拼接（示例代码）：

```ts
// 示例代码
let frames = 0;
let joined = "";
for await (const chunk of asyncChunkGenerator) {
  frames += 1;
  joined += chunk.choices[0]?.delta?.content || "";
  if (chunk.usage) {
    console.log("usage chunk:", chunk.usage.completion_tokens, "frames:", frames);
  }
}
console.log("拼接结果与 getMessage 是否一致:",
  joined === (await engine.getMessage()));
```

2. 保持请求中的 `stream_options: { include_usage: true }`（示例默认已设置，见 [examples/streaming/src/streaming.ts:24-27](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/streaming/src/streaming.ts#L24-L27)）。
3. 让模型输出一段包含 emoji 的回答（例如要求它「用三个 emoji 打招呼」）。

**需要观察的现象**：

- 步骤 1 打印 `true`：逐帧拼接与管线最终消息完全一致。
- 步骤 2：`usage chunk` 只打印一次，且此时 `choices` 为空数组。
- 步骤 3：某些时刻页面上增量文本短暂「停顿一帧」——那是 U+FFFD 跳帧在起作用，emoji 被凑齐后一次性出现。

**预期结果**：一致性与唯一 usage chunk 应稳定复现；跳帧现象与模型分词方式有关，能否肉眼观察到待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `prevMessageLength` 的更新语句（[src/engine.ts:567](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L567)）删掉，流式输出会变成什么样？

**答案**：游标永远停在 0，每一帧的 delta 都是「从头到现在的完整消息」。前端若继续做追加渲染，会得到 1、1+2、1+2+3…的重复文本。这反过来印证了增量完全靠切片游标实现。

**练习 2**：为什么中间 chunk 的 `finish_reason` 必须是 `null` 而不是省略该字段？

**答案**：`ChatCompletionChunk.Choice.finish_reason` 的类型是 `ChatCompletionFinishReason | null`（[src/openai_api_protocols/chat_completion.ts:1094](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L1094)），消费方靠「非 null」这一事件判断流终止。保持字段恒存在、以 null 表示「未结束」，让每一帧结构同构，解析代码不必做存在性分支，也兼容 OpenAI SDK 的类型定义。

### 4.3 中断机制：interruptGenerate

#### 4.3.1 概念说明

用户点了「停止生成」按钮后发生什么？很多人想象的是「取消正在进行的计算」，但 WebLLM 的实现是**协作式取消（cooperative cancellation）**：`interruptGenerate()` 只把一个布尔标志 `interruptSignal` 置为 `true`，**不做任何其他事**。真正停下的是生成循环自己——它在每轮 decode 之间检查这个标志，发现为真就调用 `pipeline.triggerStop()` 并跳出循环。

它为什么「看起来立即生效」：

1. JavaScript 主线程的事件循环里，按钮点击回调能在生成循环的任意 `await` 间隙插进来执行（解码每一步都有 await 点）。
2. 单步 decode 的耗时通常是几十毫秒量级，标志最多等到当前这一步算完就生效。
3. 中断不是抛异常，而是走「正常收尾」路径：`finishReason` 被置为 `"abort"`，已生成的部分消息完整保留，会话状态被正确闭合——下一轮对话依然可用。

#### 4.3.2 核心流程

```text
用户点击「停止」 ──→ engine.interruptGenerate()
                        └─ interruptSignal = true        （仅此一行副作用）

生成循环（asyncGenerate 第 4 步）
  while (!pipeline.stopped()):
     检查 interruptSignal ──为真──→ pipeline.triggerStop()
                                      ├─ stopTriggered = true
                                      ├─ finishReason = "abort"
                                      └─ conversation.finishReply(部分消息)
                                    break 跳出循环
     decode 一步 → yield 一帧
  → 照常 yield 最后一个 chunk（finish_reason: "abort"）
  → 释放锁
```

#### 4.3.3 源码精读

- [src/engine.ts:771-773](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L771-L773)：`interruptGenerate` 的全部实现——一行赋值。标志字段声明在 [src/engine.ts:146](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L146)。
- [src/types.ts:183-185](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L183-L185)：`MLCEngineInterface` 把它定义为同步无返回值方法——主线程引擎和 Worker 代理引擎（u5-l1）都必须提供这个方法，因此点击按钮的代码与引擎架构解耦。
- [src/engine.ts:621-627](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L621-L627)：流式循环中的检查点——每轮 decode **之前**检查信号，为真则 `triggerStop()` 并 `break`。
- [src/engine.ts:471-477](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L471-L477)：非流式路径 `_generate` 的循环检查同一标志——两种模式共享同一套中断语义。
- [src/llm_chat.ts:941-950](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L941-L950)：`triggerStop()`——置 `stopTriggered`、`finishReason = "abort"`，并调用 `conversation.finishReply(outputMessage)` 把已生成的部分消息正式写进对话历史，保证会话状态一致。
- [src/llm_chat.ts:564-566](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L564-L566)：`stopped()` 返回 `stopTriggered`，即循环的退出条件。
- [src/openai_api_protocols/chat_completion.ts:1036-1040](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L1036-L1040)：`ChatCompletionFinishReason` 联合类型中明确包含 `"abort"`——文档注释写明它是「用户手动停止生成」时的原因。

中断后能拿到什么：

- 中断瞬间 `getMessage()`（[src/engine.ts:1310-1313](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1310-L1313)）返回的就是截止停止前的部分消息（`outputMessage`）。
- 流不会异常终止：生成器照常走到最后一个 chunk，其 `finish_reason` 为 `"abort"`，消费方的 for-await 正常退出。

#### 4.3.4 代码实践

**实践目标**：验证中断「立即生效」且部分内容可取回。

**操作步骤**：

1. 打开 [examples/streaming/src/streaming.ts:48](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/streaming/src/streaming.ts#L48)，示例特意留了一行注释 `// engine.interruptGenerate();  // works with interrupt as well`。取消注释后，第一帧产出就会触发中断。
2. 刷新页面运行，观察控制台。

**需要观察的现象**：流只产出了极少量帧（通常 1～2 帧）就结束；最后一个 chunk 的 `finish_reason` 为 `"abort"`；最后一行 `Final message` 打印的是被截断的部分回答。

**预期结果**：与上述一致；由于中断发生在第一次 yield 之后、下一次 decode 检查之前，生成内容的长短有随机性（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `interruptGenerate()` 不需要 await，也不会让 for-await 循环抛异常？

**答案**：它只是同步地置一个布尔标志（[src/engine.ts:771-773](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L771-L773)），不触碰 GPU 计算也不打断 Promise。生成循环在下一轮检查点优雅退出，生成器按正常流程 yield 完最后一个 chunk 后自然结束——对消费方来说这是一次「成功但提前结束」的流。

**练习 2**：中断后马上发起下一轮对话（把含部分回答的 messages 追加回去），会正常工作吗？依据是哪行代码？

**答案**：会。`triggerStop()` 在置位后调用了 `conversation.finishReply(outputMessage)`（[src/llm_chat.ts:947-949](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L947-L949)），把部分消息正式闭合进对话历史；下一轮 prefill 比对 Conversation 一致即可复用 KV cache（u2-l2 讲过的多轮机制），无需任何特殊处理。

**练习 3**：如果把中断检查从「每轮 decode 前」移到「整个 while 循环之后」，行为会怎么变？

**答案**：标志将永远不被检查，模型会一直生成到自然停止（stop/length），`"abort"` 这个 finish_reason 在流式路径中形同虚设。检查点放在循环内、decode 之前，是「最多再等一步 decode」就能停下的关键。

## 5. 综合实践

**任务**：把 `examples/streaming` 改造成一个「打字机 + 停止按钮」页面，完整走一遍本讲的三个模块：流式入口、增量拼接、中断。

**第 1 步：改造 HTML**（`examples/streaming/src/streaming.html`，在 `generate-label` 之后追加，示例代码）：

```html
<button id="stop-btn" disabled>停止生成</button>
<div id="status-label"></div>
```

**第 2 步：改造 TS**（替换 `streaming.ts` 中 `main()` 的请求部分，示例代码）：

```ts
// 示例代码：打字机 + 停止按钮
const stopBtn = document.getElementById("stop-btn") as HTMLButtonElement;
const statusLabel = document.getElementById("status-label")!;

const request: webllm.ChatCompletionRequest = {
  stream: true,
  stream_options: { include_usage: true },
  messages: [
    { role: "system", content: "You are a pirate chatbot who always responds in pirate speak!" },
    { role: "user", content: "用两百字介绍你自己，越详细越好。" },
  ],
  max_tokens: 512,
};

stopBtn.disabled = false;
stopBtn.onclick = () => {
  engine.interruptGenerate();                    // 只置标志，立即返回
  statusLabel.innerText = "已请求中断，等待当前 decode 步完成…";
};

const chunks = await engine.chat.completions.create(request);
let message = "";
let finishReason = "(未结束)";
for await (const chunk of chunks) {
  message += chunk.choices[0]?.delta?.content || "";   // 增量拼接（4.2）
  setLabel("generate-label", message);                 // 打字机效果
  if (chunk.choices[0]?.finish_reason) {
    finishReason = chunk.choices[0]?.finish_reason!;   // 收尾帧（4.1/4.3）
  }
  if (chunk.usage) {
    console.log("usage:", chunk.usage.completion_tokens, "个生成 token");
  }
}
stopBtn.disabled = true;
// 中断后 getMessage() 仍是可用的部分消息（4.3）
statusLabel.innerText =
  `finish_reason: ${finishReason}；getMessage 长度: ${(await engine.getMessage()).length}`;
```

**第 3 步：运行与记录**：

1. `npm install && npm start`，浏览器打开 `http://localhost:8888`。
2. 正常跑完一轮，记下 `finish_reason`（应为 `stop` 或 `length`）。
3. 再跑一轮，在文字输出到一半时点「停止生成」，记下：状态栏显示的 `finish_reason`（应为 `abort`）、页面文字停止增长、`getMessage` 长度与页面文字长度一致。
4. 不中断地连发两轮（第二轮把第一轮的回答 append 进 messages），对比 usage chunk 里 `prompt_tokens` 的变化，复习 u2-l2 的 KV cache 复用。

**预期结果**：中断后页面文字立即停止增长（最多延迟一步 decode，几十毫秒量级）；部分内容完整保留在页面与 `getMessage()` 中；`finish_reason` 为 `abort`。若你的机器 decode 单步明显偏慢，可换更小的模型复测（待本地验证）。

## 6. 本讲小结

- `chatCompletion` 靠 `stream` 字段分流：流式分支在**入口处**取得模型锁后返回 `asyncGenerate(...)` 生成器对象，锁直到流被完整消费才在生成器末尾释放。
- 增量 chunk 不是管线的原生产物：管线维护完整消息 `outputMessage`，生成器用 `prevMessageLength` 做切片（\( \Delta_i = M_i[p_{i-1}:|M_i|] \)）；末尾出现不完整 emoji（U+FFFD 计数非 4 的倍数）时会跳帧。
- 一条流的骨架是：首帧（prefill 后产出，约等于 TTFT）→ 若干中间帧（`finish_reason: null`）→ 一个空 delta 收尾帧（`finish_reason` 为 stop/length/tool_calls/abort）→ 可选的 usage 帧（`choices: []`）。
- `interruptGenerate()` 是协作式取消：只置布尔标志，生成循环在每轮 decode 前检查，命中即 `triggerStop()`——`finishReason` 置 `"abort"`、部分消息经 `finishReply` 闭合进对话、流正常收尾不抛错。
- 中断后 `getMessage()` 返回截止停止前的部分内容，引擎与锁的状态都是干净的，可立即发下一轮请求。

## 7. 下一步学习建议

- 下一讲 u2-l4 将看 `completion` 文本补全接口——你会发现它的流式帧（`text` 字段）与本讲 `_getChunk` 的另一个分支共用同一套 `asyncGenerate` 代码。
- 想深挖「每帧背后的一步 decode 如何决定停止」，进入 u3-l4（decodeStep 与终止条件），那里详细拆解 `processNextToken` 的四个停止条件。
- 想理解为什么浏览器卡顿会拖慢流式输出、以及如何把整个引擎搬进 Web Worker，直接跳到 u5-l1（Web Worker 架构与消息协议）——`interruptGenerate` 在 Worker 模式下会变成一条跨线程消息。
