# chatCompletion 非流式调用与多轮对话

## 1. 本讲目标

上一讲（u2-l1）我们搞清楚了引擎的生命周期：`reload` 装载模型、`unload` 释放、`resetChat` 清会话。本讲进入引擎最常用的方法 `chatCompletion`，只聚焦**非流式**路径。学完本讲，你应该能够：

1. 看懂 `ChatCompletionRequest` 的核心字段（`messages`、`temperature`、`max_tokens`、`n`、`seed` 等），知道每个字段在哪一步被校验。
2. 完整追踪一次非流式请求的调用链：`chatCompletion` → 字段校验 → `GenerationConfig` 组装 → `_generate`（一次 prefill + 多次 decode）→ `ChatCompletion` 响应组装。
3. 读懂返回的 `ChatCompletion` 结构，尤其是 `usage`（含 WebLLM 扩展的 `extra` 性能统计）的统计口径。
4. 理解多轮对话的 KV cache 复用机制：为什么「正确地追加历史」能让第二轮的 `prompt_tokens` 反而比第一轮还少。

## 2. 前置知识

- **非流式 vs 流式**：非流式请求等模型把整条回复生成完，一次性返回完整结果；流式请求则边生成边返回小片段（chunk）。本讲只讲非流式，流式留给 u2-l3。在 WebLLM 中二者的区别仅在于请求里的 `stream` 字段是否为 `true`。
- **prefill 与 decode**：模型生成文本分两个阶段。prefill（预填充）把输入 prompt 一次性「读」完并写入 KV cache，产出第一个输出 token；decode（解码）随后每次只前向一个 token，直到停止。粗略地说：**prefill 决定「多久开始说话」，decode 决定「说得多快」**。细节在单元三精读，本讲只需这个直觉。
- **KV cache**：Transformer 自回归生成时缓存已计算过的注意力键值。如果第二次请求的历史与第一次完全一致，已缓存的部分无需重新计算——这就是多轮对话能「续聊」而不用重算全文的原因。
- **重载（overload）**：TypeScript 允许同一个函数声明多个参数/返回值类型组合。`chatCompletion` 用三段重载签名让「`stream: false` 返回 `Promise<ChatCompletion>`、`stream: true` 返回 `Promise<AsyncIterable<ChatCompletionChunk>>`」在类型层面就被区分开。
- **usage 统计口径**：OpenAI 协议的 `usage` 报告 token 用量。WebLLM 额外加了 `extra` 字段输出性能数据，且 `prompt_tokens` 在多轮对话命中缓存时**只计新增部分**——这是本讲最重要的一个口径细节。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/engine.ts` | 引擎编排层 | `chatCompletion` 的重载分发、`GenerationConfig` 组装、非流式主循环、响应组装、`prefill` 中的多轮判定 |
| `src/openai_api_protocols/chat_completion.ts` | OpenAI 聊天补全协议类型与校验 | `ChatCompletionRequestBase` 字段定义、`postInitAndCheckFields` 消息校验、`ChatCompletion` / `CompletionUsage` 响应结构 |
| `src/config.ts` | 配置体系 | `GenerationConfig` 接口、`postInitAndCheckGenerationConfigValues` 数值校验 |
| `src/conversation.ts` | 对话模板与编码 | `getConversationFromChatCompletionRequest`、`compareConversationObject`（多轮判定的两块基石） |
| `src/openai_api_protocols/index.ts` | 协议层出口 | 把 `postInitAndCheckFields` 再导出为 `postInitAndCheckFieldsChatCompletion` |
| `examples/multi-round-chat/src/multi_round_chat.ts` | 官方多轮对话示例 | 本讲实践的母本 |

## 4. 核心概念与源码讲解

### 4.1 chatCompletion 重载分发：从请求到管线

#### 4.1.1 概念说明

`engine.chatCompletion(request)` 是 WebLLM 对话的主入口。它要解决三个问题：

1. **这个请求交给哪个模型？** 引擎可能同时加载了多个模型（u2-l1 讲过 `loadedModelIdToPipeline` 这张 Map），请求里的 `model` 字段就是路由依据；只加载一个模型时可以省略。
2. **请求合法吗？** 消息顺序、字段组合要在生成开始前检查。
3. **走流式还是非流式？** 由 `request.stream` 分流。

另外，上一讲提到的 OpenAI 风格门面 `engine.chat.completions.create()` 只有一行实现——直接转发给 `chatCompletion`，二者完全等价。

#### 4.1.2 核心流程

一次非流式 `chatCompletion` 的分发流程：

```text
engine.chatCompletion(request)          # 或 engine.chat.completions.create(request)
  ├─ 0.  getLLMStates(request.model)   # 从 Map 里选出 [modelId, pipeline, chatConfig]
  ├─ 1.  postInitAndCheckFieldsChatCompletion(request)
  │       # 消息结构校验（system 必须第一条、最后一条必须是 user/tool 等）
  ├─ 2.  组装 GenerationConfig          # 从 request 摘出采样参数
  ├─ 3.  lock.acquire()                 # 同一模型同时只处理一个请求
  ├─ 4.  request.stream ?
  │       ├─ true  → return asyncGenerate(...)   # 流式，下一讲
  │       └─ false → 循环 n 次 _generate()       # 非流式，本讲
  │                     ├─ postInitAndCheckGenerationConfigValues(genConfig)
  │                     ├─ prefill(...)   # 多轮判定 + 写 KV cache + 产出第 1 个 token
  │                     └─ while !stopped: decode(...)  # 逐 token 生成
  ├─ 5.  组装 ChatCompletion 响应（choices + usage）
  └─ finally: lock.release()
```

注意校验发生在**两个不同的时机**：消息结构校验在入口处（第 1 步，拿到锁之前），采样数值校验在 `_generate` 内部（prefill 之前）。这意味着一个 `temperature: -1` 的请求会先通过消息检查、拿到锁，然后在即将 prefill 时才抛错。

#### 4.1.3 源码精读

先看三段重载签名。TypeScript 根据参数类型（`ChatCompletionRequestNonStreaming` 还是 `ChatCompletionRequestStreaming`）在编译期就锁定返回值类型：

- [src/engine.ts:787-798](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L787-L798) — `chatCompletion` 的三段重载：非流式返回 `Promise<ChatCompletion>`，流式返回 `Promise<AsyncIterable<ChatCompletionChunk>>`，第三段是运行时的宽容签名。
- [src/openai_api_protocols/chat_completion.ts:289-307](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L289-L307) — 两种请求类型的定义：`ChatCompletionRequestNonStreaming` 要求 `stream?: false | null`，`ChatCompletionRequestStreaming` 要求 `stream: true`。二者的联合类型即 `ChatCompletionRequest`。这是「可辨识联合」（discriminated union）：`stream` 字段就是判别器。

入口的前两步——选模型与消息校验：

- [src/engine.ts:799-809](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L799-L809) — 记录 `timeReceived`（供后面 `usage.extra.e2e_latency_s` 使用），调用 `getLLMStates` 取回三元组，再调用 `API.postInitAndCheckFieldsChatCompletion` 做消息校验。
- [src/engine.ts:1199-1208](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1199-L1208) — `getLLMStates` 是 `getModelStates` 的 LLM 特化封装，返回 `[modelId, LLMChatPipeline, ChatConfig]`。若选中的管线类型不对（比如对 embedding 模型发聊天请求）会抛 `IncorrectPipelineLoadedError`（见 [src/engine.ts:1244-1269](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1244-L1269)）。
- [src/openai_api_protocols/index.ts:27](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/index.ts#L27) — 揭开一个命名谜题：引擎调用的 `postInitAndCheckFieldsChatCompletion` 其实就是 `chat_completion.ts` 里 `postInitAndCheckFields` 的再导出别名。

锁与分流：

- [src/engine.ts:828-841](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L828-L841) — 先 `lock.acquire()`（每个已加载模型一把 `CustomLock`，保证同一模型串行处理请求），然后按 `request.stream` 分流。流式分支直接 `return this.asyncGenerate(...)`——注意此时锁还没有释放，`asyncGenerate` 内部在流消费完毕或出错时才释放。
- [src/engine.ts:962-964](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L962-L964) — 非流式路径用 `try { ... } finally { await lock.release(); }` 兜底：无论生成成功还是中途抛错，锁一定释放。

门面只有一行：

- [src/openai_api_protocols/chat_completion.ts:60-79](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L60-L79) — `Completions.create` 的实现就是 `return this.engine.chatCompletion(request);`。`Chat` / `Completions` 两个类纯粹是为了拼出 `engine.chat.completions.create()` 这个 OpenAI 风格的调用路径。

#### 4.1.4 代码实践

1. **实践目标**：验证「门面与真身等价」，并亲眼看到非流式请求的串行排队。
2. **操作步骤**：在 u1-l2 跑通的 get-started 页面（或任意已加载引擎的页面）的控制台里执行（示例代码）：

   ```ts
   const r1 = await engine.chatCompletion({ messages: [{ role: "user", content: "Hi" }] });
   const r2 = await engine.chat.completions.create({
     messages: [{ role: "user", content: "Hi" }],
   });
   console.log(r1.object, r2.object);       // 期望都是 chat.completion
   console.log(r1.choices[0].message.content);

   // 串行验证：不 await 第一个，立刻发起第二个
   const p1 = engine.chatCompletion({ messages: [{ role: "user", content: "讲个长故事" }] });
   const p2 = engine.chatCompletion({ messages: [{ role: "user", content: "你好" }] });
   console.log((await p1).created <= (await p2).created);
   ```

   注意两次调用都没有传 `stream`，`stream` 为可选字段、缺省即非流式（见 [src/openai_api_protocols/chat_completion.ts:98-100](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L98-L100)）。
3. **需要观察的现象**：第二个请求 `p2` 的回复不会在 `p1` 生成期间开始产出——`p2` 在 `lock.acquire()` 处等待。
4. **预期结果**：`r1.object` 与 `r2.object` 均为 `"chat.completion"`；两次并发请求按发起顺序先后完成。具体回复文本因模型与采样而异，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么流式分支 `return this.asyncGenerate(...)` 时不能像非流式那样用 `finally` 释放锁？
**答案**：`asyncGenerate` 是 async generator，`return` 只是返回了一个可迭代对象，真正的生成发生在调用方逐个消费 chunk 的时候。如果入口处立刻释放锁，消费流的中途另一个请求就能插队，破坏管线内部状态。所以锁的释放被搬进了 `asyncGenerate` 内部：在 [src/engine.ts:508-532](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L508-L532) 的注释与错误处理分支、以及流结束后的 [src/engine.ts:768](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L768) 中完成。

**练习 2**：引擎加载了两个模型但请求没写 `model` 字段，会发生什么？
**答案**：`getLLMStates` → `getModelStates` 内部用 `getModelIdToUse(loadedModelIds, modelId, requestName)` 做选择，无法唯一确定时抛错。协议注释（[src/openai_api_protocols/chat_completion.ts:260-267](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L260-L267)）明确：单模型时可省略 `model`，多模型时必填。

**练习 3**：`request.model` 填了一个引擎没加载过的 `model_id`，会在哪一步抛错？
**答案**：在入口第 0 步 `getLLMStates("ChatCompletionRequest", request.model)` 里。`getModelStates`（[src/engine.ts:1229-1243](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1229-L1243)）选不出模型即抛 `SpecifiedModelNotFoundError`，根本不会进入生成阶段。

### 4.2 请求字段校验与 GenerationConfig 组装

#### 4.2.1 概念说明

请求字段分两类，走两条校验路径：

1. **消息与结构类**（`messages` 的角色顺序、`stream` 与 `n` 的组合、`response_format` 的搭配等）——在 `chatCompletion` 入口由 `postInitAndCheckFields` 检查；
2. **采样数值类**（`temperature`、`top_p`、`max_tokens`、各种 penalty 的取值范围）——被摘进 `GenerationConfig`，在 `_generate` 内、prefill 之前由 `postInitAndCheckGenerationConfigValues` 检查。

`GenerationConfig` 可以理解为「`ChatConfig` 里**每次请求都可以变**的那部分」。你在 u1-l4 见过 `ChatConfig`（加载期、三层合并确定）；`GenerationConfig` 则是请求级的：**请求里给了哪个字段就覆盖哪个，没给的字段保持 `undefined`，管线会回退到 `reload` 时确定的 `ChatConfig` 值**。

#### 4.2.2 核心流程

```text
request（用户请求）
  │ 摘取采样相关字段（engine.ts:810-825）
  ▼
GenerationConfig { temperature?, top_p?, max_tokens?, frequency_penalty?,
                   presence_penalty?, repetition_penalty?, stop?, logit_bias?,
                   logprobs?, top_logprobs?, response_format?, ignore_eos?, ... }
  │ 在 _generate 内校验（config.ts:167 起）
  ▼
postInitAndCheckGenerationConfigValues(genConfig)
  ├─ frequency_penalty / presence_penalty ∈ [-2, 2]
  ├─ repetition_penalty > 0，max_tokens > 0
  ├─ 0 < top_p ≤ 1，temperature ≥ 0
  ├─ 只设了其中一个 penalty → 另一个补默认 0.0
  └─ logit_bias 每个值 ∈ [-100, 100]
```

#### 4.2.3 源码精读

请求字段的权威定义：

- [src/openai_api_protocols/chat_completion.ts:91-110](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L91-L110) — `ChatCompletionRequestBase` 开头：`messages`（必填，对话历史数组）、`stream`、`stream_options`、`n`（生成几个候选）。注意注释说明 `model` 被排除在请求协议之外、改为在 `CreateMLCEngine(model)` 时显式指定——这是 WebLLM 与 OpenAI API 的一个刻意差异。
- [src/openai_api_protocols/chat_completion.ts:138-156](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L138-L156) — `max_tokens`（最多生成多少 token）与 `temperature`（0 到 2，越高越随机）。
- [src/openai_api_protocols/chat_completion.ts:197-208](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L197-L208) — `seed`：同一 seed 加相同参数应当可复现；且 seed 是**请求级**而非候选级——`n > 1` 时各个 choice 仍互不相同。

`GenerationConfig` 的组装只有一次「字段摘取」：

- [src/engine.ts:810-825](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L810-L825) — 逐字段把 `request.xxx` 抄进 `genConfig.xxx`，还包括 `extra_body` 里的 `enable_thinking` 与 `enable_latency_breakdown`。没有默认值填充——`undefined` 就留给管线回退。
- [src/config.ts:145-165](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L145-L165) — `GenerationConfig` 接口定义，注释点明它「本质上是 `ChatConfig` 去掉 tokenizer/模板字段，再加上 OpenAI 风格 API 才有的字段；未指定的值沿用 `reload()` 时初始化的 `ChatConfig`」。

两层校验的实现：

- [src/openai_api_protocols/chat_completion.ts:473-488](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L473-L488) — 消息校验两条硬规则：`system` 消息必须在第 0 条（否则 `SystemMessageOrderError`）；最后一条消息必须是 `user` 或 `tool`（否则 `MessageOrderError`）。这直接决定了「多轮对话必须由调用方把 assistant 回复追加进历史」——你不能把 assistant 消息放在末尾就发起请求。
- [src/openai_api_protocols/chat_completion.ts:490-500](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L490-L500) — 流式时 `n` 不能大于 1（`StreamingCountError`）；`seed` 必须是整数（`SeedTypeError`）。
- [src/config.ts:167-214](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L167-L214) — `postInitAndCheckGenerationConfigValues` 的数值校验与默认补齐。注意 `_hasValue` 辅助函数的注释：不能用 `if (value)` 判断，因为 `0` 会被误当成「未设置」。校验的调用点在 [src/engine.ts:466-468](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L466-L468)（`_generate` 内部）。

#### 4.2.4 代码实践

1. **实践目标**：亲手触发五类校验错误，并把每个错误精确对应到源码抛出点。
2. **操作步骤**：在已加载引擎的页面控制台依次执行并 `catch`（示例代码）：

   ```ts
   const bad = async (req: any) => {
     try { await engine.chatCompletion(req); }
     catch (e) { console.log(e.constructor.name, "|", (e as Error).message); }
   };
   await bad({ messages: [{ role: "user", content: "hi" }], temperature: -1 });
   await bad({ messages: [{ role: "user", content: "hi" }], max_tokens: 0 });
   await bad({ messages: [{ role: "user", content: "hi" },
                           { role: "system", content: "late" }] });
   await bad({ messages: [{ role: "user", content: "hi" },
                           { role: "assistant", content: "hey" }] });
   await bad({ messages: [{ role: "user", content: "hi" }], stream: true, n: 2 });
   ```

3. **需要观察的现象**：前两个错误（数值类）与后三个错误（结构类）抛出的错误类名不同；再对比它们抛出的时机——可以在 [src/engine.ts:805](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L805) 与 [src/engine.ts:467](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L467) 两处分别加临时日志观察先后。
4. **预期结果**：
   - `temperature: -1` → `NonNegativeError`（[src/config.ts:195-197](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L195-L197)）
   - `max_tokens: 0` → `MinValueError`（[src/config.ts:189-191](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L189-L191)）
   - `system` 不在首位 → `SystemMessageOrderError`（[src/openai_api_protocols/chat_completion.ts:473-475](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L473-L475)）
   - 末条是 `assistant` → `MessageOrderError`（[src/openai_api_protocols/chat_completion.ts:480-488](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L480-L488)）
   - `stream: true, n: 2` → `StreamingCountError`（[src/openai_api_protocols/chat_completion.ts:491-493](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L491-L493)）
   具体错误消息文案以本地运行为准，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：请求只设了 `frequency_penalty: 1.0` 没设 `presence_penalty`，最终采样时 `presence_penalty` 是多少？
**答案**：`0.0`。[src/config.ts:198-214](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L198-L214) 的补齐逻辑：只设其一会把另一个默认成 0.0 并打 warn 日志。反过来只设 `presence_penalty` 时 `frequency_penalty` 补 0.0。

**练习 2**：为什么 `temperature: 0` 不会被当成「未设置」而跳过校验？
**答案**：因为校验用 `_hasValue`（`value !== undefined && value !== null`）判断，实现处特意注释了不能用真值判断，否则 `0` 会被误判（[src/config.ts:170-173](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L170-L173)）。不过注意 `frequency_penalty` 的范围检查（[src/config.ts:174-179](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L174-L179)）用的是 `config.frequency_penalty &&` 真值判断——`0` 恰好在合法范围内，所以不影响正确性。

**练习 3**：请求里不传 `temperature`，模型用什么温度采样？
**答案**：`genConfig.temperature` 为 `undefined`，管线回退到该模型 `ChatConfig` 里的 `temperature`——即模型仓库 `mlc-chat-config.json` 的值，经 u1-l4 讲过的「仓库配置 → overrides → chatOpts」三层合并后的结果。

### 4.3 非流式主循环与 ChatCompletion 响应组装

#### 4.3.1 概念说明

非流式路径的核心是一个朴素的循环：对每个候选（`n` 个，默认 1）调用一次 `_generate`，拿到完整输出字符串后组装成 OpenAI 风格的 `ChatCompletion` 对象。`_generate` 内部就是「一次 prefill + while 循环 decode」，这正是自回归生成的最小骨架。

响应里最值得精读的是 `usage`：OpenAI 标准的三个 token 计数，加上 WebLLM 扩展的 `extra` 性能块（延迟、吞吐、首 token 耗时）。它让你不借助任何 profiler 就能拿到一轮生成的性能画像。

#### 4.3.2 核心流程

`_generate`（单个候选的生成骨架）：

```text
_generate(request, pipeline, chatConfig, genConfig)
  ├─ interruptSignal = false
  ├─ postInitAndCheckGenerationConfigValues(genConfig)   # 数值校验
  ├─ await prefill(...)      # 处理 messages、写 KV cache、产出第 1 个 token
  └─ while (!pipeline.stopped()):
        ├─ interruptSignal ? triggerStop(); break       # 用户中断
        └─ await decode(...)   # 每次前进一个 token
  return pipeline.getMessage()   # 完整输出文本
```

响应组装的统计口径：

\[ \text{total\_tokens} = \text{prompt\_tokens} + \text{completion\_tokens} \]

\[ \text{prefill\_tokens\_per\_s} = \frac{\text{prompt\_tokens}}{\text{prefill\_time}}, \qquad \text{decode\_tokens\_per\_s} = \frac{\text{completion\_tokens}}{\text{decode\_time}} \]

若 `n > 1`，各候选的 token 数与耗时**累加**后放入同一个 `usage`（`choices` 数组则有 n 项）。

#### 4.3.3 源码精读

- [src/engine.ts:457-479](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L457-L479) — `_generate` 全貌：校验 → `prefill` → `while (!pipeline.stopped())` 循环 `decode`，期间随时响应 `interruptSignal`，最后 `pipeline.getMessage()` 返回完整文本。
- [src/engine.ts:849-871](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L849-L871) — 非流式主循环开头：`n = request.n ? request.n : 1`（默认 1）；若收到中断信号则直接 `triggerStop()` 并把该候选输出置空——**一个中断信号会停掉所有候选**。
- [src/engine.ts:872-908](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L872-L908) — 每个候选的后处理：从管线取 `finish_reason`；若带 `tools` 且以 `stop` 结束，则把输出文本解析成 `tool_calls` 并把 finish_reason 改写为 `"tool_calls"`（函数调用细节在 u6-l2）；最后 `choices.push({ finish_reason, index: i, logprobs, message })`。普通请求的 `message` 就是 `{ content: outputMessage, role: "assistant" }`。
- [src/engine.ts:909-916](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L909-L916) — usage 的累加：`completion_tokens`、`prompt_tokens`、`prefill_time`、`decode_time` 等都从管线的 `getCurRoundXXX()` 系列方法按轮读取后累加。
- [src/engine.ts:925-955](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L925-L955) — 响应对象组装：`id`（`crypto.randomUUID()`）、`object: "chat.completion"`、`created`（Unix 秒级时间戳，来自 [src/engine.ts:80-82](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L80-L82) 的 `getUnixTimestampSeconds`）、`choices` 与 `usage`（含 `extra`）。`time_to_first_token_s` 直接等于 `prefill_time`——首 token 延迟的主体就是预填充。
- [src/engine.ts:957-960](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L957-L960) — 请求结束后把 seed 重置为 `Date.now()`，避免本次 `seed` 影响后续请求的可复现性。

响应协议类型：

- [src/openai_api_protocols/chat_completion.ts:312-356](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L312-L356) — `ChatCompletion` 接口。`usage` 的注释藏着关键口径：**检测到多轮对话时，`prompt_tokens` 只计新增部分**；`n > 1` 时是所有候选的总和。
- [src/openai_api_protocols/chat_completion.ts:959-1027](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L959-L1027) — `CompletionUsage`：标准三字段 + WebLLM 专属 `extra`（`e2e_latency_s`、`prefill_tokens_per_s`、`decode_tokens_per_s`、`time_to_first_token_s`、`time_per_output_token_s`，以及结构化输出才有的 `grammar_init_s` / `grammar_per_token_s` 与可选 `latencyBreakdown`）。
- [src/openai_api_protocols/chat_completion.ts:1036-1040](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L1036-L1040) — `finish_reason` 的四种取值：`stop`（自然停止或命中停止串）、`length`（达到 `max_tokens` 或上下文将满）、`tool_calls`（调用了工具）、`abort`（用户手动中断）。

#### 4.3.4 代码实践

1. **实践目标**：完整解剖一个 `ChatCompletion` 响应对象。
2. **操作步骤**（示例代码）：

   ```ts
   const reply = await engine.chatCompletion({
     messages: [{ role: "user", content: "用一句话介绍 WebGPU" }],
     max_tokens: 64,
     temperature: 0.7,
   });
   console.log(JSON.stringify(reply, null, 2));
   console.table([{
     finish: reply.choices[0].finish_reason,
     prompt: reply.usage?.prompt_tokens,
     completion: reply.usage?.completion_tokens,
     ttft_s: reply.usage?.extra.time_to_first_token_s,
     decode_tps: reply.usage?.extra.decode_tokens_per_s,
   }]);
   ```

3. **需要观察的现象**：`choices` 数组只有一个元素且 `index: 0`；`usage.extra.time_to_first_token_s` 与 `e2e_latency_s` 的差值就是解码阶段耗时。
4. **预期结果**：能逐一指出响应里每个字段的来源（`id` 来自 `crypto.randomUUID()`、`created` 是秒级时间戳、`message.content` 来自 `pipeline.getMessage()`）。具体数值因机器与模型而异，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：把 `max_tokens` 设为 2，`finish_reason` 会是什么？为什么？
**答案**：大概率是 `"length"`。decode 循环生成满 2 个 token 就触发 `max_tokens` 停止条件，管线记录的 finish reason 为 `length`（四种取值见 [src/openai_api_protocols/chat_completion.ts:1029-1040](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L1029-L1040) 的注释）。如果模型恰好在第 2 个 token 就生成了结束符，则是 `"stop"`——所以答案是「通常 length，可能是 stop」。（停止判定的实现位于 `llm_chat.ts` 的 decode 路径，单元三详讲。）

**练习 2**：`usage.extra.time_to_first_token_s` 为什么直接等于 `prefill_time`，而不是 `prefill_time + 一次 decode 的耗时`？
**答案**：因为 prefill 阶段本身就会产出第一个输出 token——预填充读完整个 prompt 后对最后一个位置做采样就得到了首 token（`prefillStep` 返回第一个 sampled token），所以从收到请求到首 token 的耗时主体就是预填充时间。引擎直接用 `prefill_time` 近似（[src/engine.ts:929](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L929)）。

**练习 3**：`n: 3` 时响应里有 3 个 choice，`usage.completion_tokens` 是单个候选的还是三个候选的总和？
**答案**：总和。主循环里每个候选生成后都执行 `completion_tokens += selectedPipeline.getCurRoundDecodingTotalTokens()`（[src/engine.ts:909-916](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L909-L916)），`ChatCompletion.usage` 的注释也明确「all choices' generation usages combined」。

### 4.4 多轮对话：messages 历史与 KV cache 复用

#### 4.4.1 概念说明

OpenAI 协议本身是**无状态**的：每次请求都带上从 system 到当前的完整 `messages`。WebLLM 引擎内部却是有状态的（KV cache 里存着上一轮的注意力缓存），于是 `prefill` 里做了一个聪明的比对：

- 把请求的 `messages[:-1]`（除最后一条外）转换成一个新的 `Conversation` 对象；
- 与管线里保存的旧 `Conversation` 逐字段比对；
- **相同** → 判定为多轮续聊，不重置，直接复用 KV cache，只 prefill 最后一条新消息；
- **不同** → 判定为全新对话，`resetChat()` 清空 KV cache，全量 prefill。

这解释了官方示例的做法：每轮把 assistant 回复 `push` 进 `messages` 再加新 user 消息。**调用方负责维护历史，引擎负责识别「这是不是上一段对话的延续」**。

由此得到本讲最重要的口径结论：命中复用时 `usage.prompt_tokens` 只计**新增部分**（新 user 消息 + 上一轮 assistant 回复的衔接 token），所以第二轮的 `prompt_tokens` 往往**小于**第一轮；一旦修改了历史中的任何一条消息，缓存判定失败，`prompt_tokens` 会跳升为整个对话的 token 数——这时你才会观察到「轮次越多、prefill token 越多」。

#### 4.4.2 核心流程

```text
prefill(input, pipeline, chatConfig, genConfig)     # input 是 ChatCompletionRequest
  ├─ oldConv = pipeline.getConversationObject()     # 上一轮的对话对象
  ├─ newConv = getConversationFromChatCompletionRequest(input, chatConfig)
  │     # 把 messages[:-1] 逐条 appendMessage 进新 Conversation
  ├─ compareConversationObject(oldConv, newConv) ?
  │     ├─ 不相同 → pipeline.resetChat() + setConversation(newConv)   # 全量重来
  │     ├─ 相同但历史为空 → resetChat() + setConversation(newConv)
  │     └─ 相同且非空 → 复用 KV cache（仅打日志 "Multiround chatting, reuse KVCache."）
  ├─ last_msg = messages[-1]        # 最后一条就是本轮输入
  └─ pipeline.prefillStep(last_msg.content, role, name, genConfig)
```

比对相等的定义（[src/conversation.ts:374-381](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L374-L381) 的注释）：两个 Conversation 的 `getPromptArray()` 产出完全一致才算相等，由 `messages`、`function_string`、`use_function_calling`、`override_system_message` 等字段共同决定。

#### 4.4.3 源码精读

- [src/engine.ts:1366-1397](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1366-L1397) — `prefill` 的多轮判定核心：函数注释写得很清楚——`messages[-1]` 当作本轮用户输入，`messages[:-1]` 转成 Conversation 代表历史；新旧 Conversation 匹配即多轮续聊（复用 KV cache），不匹配则重置一切。源码注释原文是 "Multiround chatting, reuse KVCache."（[src/engine.ts:1396](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1396)）。
- [src/engine.ts:1399-1417](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1399-L1417) — 取 `messages` 最后一条作为本轮输入（支持 `user` 或 `tool` 角色，`name` 字段作为可选角色名传入）；对比 `completion()` 的分支——文本补全**每次都 resetChat**，完全没有多轮复用。
- [src/conversation.ts:469-519](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L469-L519) — `getConversationFromChatCompletionRequest`：新建 Conversation 后，把请求消息按角色逐条 `appendMessage`（system 走 `override_system_message`，user/assistant/tool 各有分支，未知角色抛 `UnsupportedRoleError`）。注意 `iterEnd = input.length - 1`——最后一条被排除在历史之外。
- [src/conversation.ts:382-407](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L382-L407) — `compareConversationObject` 开头：先比对 `function_string`、`use_function_calling`、`override_system_message`、消息条数、`isTextCompletion` 这些「快捷字段」，任一不同立即返回 false；随后逐条逐字段比对消息内容。
- [src/openai_api_protocols/chat_completion.ts:340-345](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L340-L345) — `ChatCompletion.usage` 的注释：多轮对话时 `prompt_tokens` 只计新增部分。同一口径在 [src/openai_api_protocols/chat_completion.ts:966-971](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L966-L971)（`CompletionUsage.prompt_tokens`）再次声明。
- [examples/multi-round-chat/src/multi_round_chat.ts:48-66](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/multi-round-chat/src/multi_round_chat.ts#L48-L66) — 官方示例第二轮的做法：先 `messages.push({ role: "assistant", content: replyMessage0 })` 再 `push` 新的 user 消息。第 53-55 行的注释警告：如果改掉历史中的 system 消息（示例中被注释的那行），会导致内部 reset、KV cache 全部作废。
- [examples/multi-round-chat/src/multi_round_chat.ts:68-79](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/multi-round-chat/src/multi_round_chat.ts#L68-L79) — 官方断言：第二轮的 `prompt_tokens` 若**大于**第一轮就直接 `throw`——用可观察指标验证 KV 复用确实生效。这是「多轮命中时 prompt_tokens 变小而非变大」的最直接源码证据。

#### 4.4.4 代码实践

1. **实践目标**：用一组对照实验验证「历史不变 → 复用缓存；历史被改 → 全量重算」。
2. **操作步骤**（示例代码，可在控制台执行）：

   ```ts
   const hist: any[] = [{ role: "user", content: "给我介绍三个美国州" }];
   const r1 = await engine.chatCompletion({ messages: hist });
   hist.push({ role: "assistant", content: r1.choices[0].message.content });
   hist.push({ role: "user", content: "再来两个！" });

   // 实验A：历史原样追加（应命中复用）
   const r2 = await engine.chatCompletion({ messages: hist });
   console.log("A:", r1.usage?.prompt_tokens, "->", r2.usage?.prompt_tokens);

   // 实验B：篡改历史第一条后再请求（应触发 reset 全量 prefill）
   hist[0].content = "给我介绍三个欧洲国家";
   const r3 = await engine.chatCompletion({ messages: hist });
   console.log("B:", r3.usage?.prompt_tokens);
   ```

3. **需要观察的现象**：实验 A 中第二轮 `prompt_tokens` 很小（只有「上一轮回复 + 新问题」的量级）；实验 B 中 `prompt_tokens` 跳升为整个对话历史的 token 总量。还可在 DevTools 控制台看到 loglevel 的 `"Multiround chatting, reuse KVCache."` 日志。
4. **预期结果**：A 的 `r2.prompt_tokens << r1.prompt_tokens`（与 [examples/multi-round-chat/src/multi_round_chat.ts:73-79](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/multi-round-chat/src/multi_round_chat.ts#L73-L79) 的断言一致）；B 的 `r3.prompt_tokens` 明显大于 A。具体数字因模型 tokenizer 而异，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么最后一条消息必须是 `user` 或 `tool`？（提示：结合 `getConversationFromChatCompletionRequest` 的 `iterEnd`。）
**答案**：因为 `messages[:-1]` 被当作「已发生的历史」转成 Conversation，`messages[-1]` 被当作「本轮要 prefill 的新输入」。如果最后一条是 assistant，它到底算历史还是算输入就无法自洽，所以 [src/openai_api_protocols/chat_completion.ts:480-488](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L480-L488) 与 [src/conversation.ts:490-495](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L490-L495) 两处都做了此检查并抛 `MessageOrderError`。

**练习 2**：第二轮请求只把上一轮 assistant 回复的前半句追加进历史，会发生什么？
**答案**：`compareConversationObject` 逐字段比对消息内容（[src/conversation.ts:408-459](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L408-L459) 的消息比对循环）发现不一致 → 返回 false → `pipeline.resetChat()` 清空 KV cache → 全量 prefill 整个历史。功能上仍能得到正确回复（对话历史以本次请求为准），只是损失了性能。

**练习 3**：`completion()`（文本补全）为什么享受不到这个优化？
**答案**：[src/engine.ts:1407-1417](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1407-L1417) 的 `else` 分支对每次 completion 请求都无条件 `pipeline.resetChat()` 再 `setConversation`——文本补全没有 messages 历史，也就没有「续聊」可言（这也是 u2-l4 会讲到的「completion 期望 KV cache 为空」的根源）。

## 5. 综合实践

**任务：写一个三轮对话页面，用 `usage` 数据亲证 KV cache 复用。**

前置：按 [examples/multi-round-chat/README.md](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/multi-round-chat/README.md) 的方式搭一个最小页面（`npm install` + `npm start`，Parcel 起本地服务，与 u1-l2 的 get-started 同套路），也可以直接在 get-started 页面控制台完成。

1. **实践目标**：维护三轮 `messages` 历史，逐轮记录 `usage`，观察并解释 prefill / decode token 数的变化规律。
2. **操作步骤**（示例代码，改写自 [examples/multi-round-chat/src/multi_round_chat.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/multi-round-chat/src/multi_round_chat.ts)）：

   ```ts
   const engine = await webllm.CreateMLCEngine("Llama-3.2-1B-Instruct-q4f32_1-MLC");
   const messages: webllm.ChatCompletionMessageParam[] = [
     { role: "system", content: "你是一个简洁的助手。" },
     { role: "user", content: "用一句话解释什么是 KV cache" },
   ];
   const questions = ["再给一个生活中的类比", "如果把类比换成数据库索引呢？"];

   for (let round = 0; round <= questions.length; round++) {
     const reply = await engine.chatCompletion({ messages, max_tokens: 128 });
     const u = reply.usage!;
     console.table([{
       round,
       finish: reply.choices[0].finish_reason,
       prompt_tokens: u.prompt_tokens,          // 本轮 prefill 的 token（增量口径）
       completion_tokens: u.completion_tokens,   // 本轮 decode 的 token
       ttft_s: u.extra.time_to_first_token_s,
       decode_tps: u.extra.decode_tokens_per_s,
     }]);
     if (round < questions.length) {
       messages.push({ role: "assistant", content: reply.choices[0].message.content! });
       messages.push({ role: "user", content: questions[round] });
     }
   }
   ```

   （模型可换为你机器跑得动的任一 `model_id`；模型选择依据见 u1-l1/u1-l4。）
3. **需要观察的现象**：
   - 第 1 轮 `prompt_tokens` = system + 第一问的 token 量；
   - 第 2、3 轮 `prompt_tokens` 只有「上一轮 assistant 回复 + 新问题」的量级，**远小于**全部历史的 token 总量——因为命中了 KV cache 复用（`prefill` 的比对逻辑，[src/engine.ts:1381-1397](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1381-L1397)）；
   - 每轮 `completion_tokens` 与该轮回复长度大致成正比；`decode_tps` 逐轮基本稳定（decode 速度与历史长度近似无关）。
4. **解释「prefill token 什么时候会越来越多」**：命中复用时，每轮 prefill 的只是增量（上一轮回复 + 新问题），所以**不会**随历史总量线性增长——官方示例甚至断言第二轮必不大于第一轮（[examples/multi-round-chat/src/multi_round_chat.ts:68-79](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/multi-round-chat/src/multi_round_chat.ts#L68-L79)）。只有两种情况你会看到它暴涨：(a) 修改/删除了历史中的任意消息，比对失败触发 `resetChat()` 全量重算；(b) 回复本身很长，使「上一轮回复」这段增量变大。换句话说，**决定 prefill 量的是增量而非存量，除非缓存被打破**。
5. **预期结果**：三轮对话语义连贯（第 2、3 轮能接住上文），`prompt_tokens` 呈现「大 → 小 → 小」而非单调递增。具体数值与回复内容**待本地验证**。
6. **加分项**：在第 3 轮之前偷偷执行 `messages[1].content = "换个问题"`，再观察第 3 轮 `prompt_tokens` 的跳变，与本节 4.4.4 的实验 B 相互印证。

## 6. 本讲小结

- `chatCompletion` 用三段 TypeScript 重载在类型层面区分流式/非流式，判别器是请求里的 `stream` 字段；`engine.chat.completions.create()` 只是它的一行转发门面。
- 非流式主循环 = 「`n` 次 `_generate`（每次 = 一次 prefill + while 循环 decode）+ 一次性组装 `ChatCompletion`」；同一模型由 `CustomLock` 串行处理请求。
- 校验分两层：消息结构在入口（`postInitAndCheckFields`），采样数值在 `_generate` 内（`postInitAndCheckGenerationConfigValues`）；未指定的采样参数回退到 `reload` 时三层合并出的 `ChatConfig`。
- `usage` 除标准 token 计数外还有 WebLLM 扩展的 `extra` 性能块：`time_to_first_token_s ≈ prefill_time`，`decode_tokens_per_s` 反映逐 token 速度。
- 多轮对话由调用方维护 `messages`、引擎内部比对新旧 `Conversation` 判定：命中则复用 KV cache，`prompt_tokens` 只计增量（第二轮反而比第一轮小）；篡改历史会触发全量重算，此时 prefill 量才随轮次暴涨。

## 7. 下一步学习建议

本讲只讲了 `chatCompletion` 的「一次性返回」路径。下一讲 **u2-l3（流式输出）** 将走进 `asyncGenerate` 这个 async generator：增量 chunk 如何拼接、末尾空 chunk 如何标志结束、以及 `interruptGenerate()` 为什么能立即打断 decode 循环（提示：本讲 [src/engine.ts:471-477](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L471-L477) 的 `interruptSignal` 检查就是答案的一半）。若你想先横向铺开接口层，可跳去 **u2-l4（completion 文本补全）** 对比「每次 resetChat」的另一条路径，或 **u2-l5（embedding）**；若想纵向深入推理管线内部（`prefillStep` 如何分块、`decodeStep` 如何判停），请进入单元三的 **u3-l3 / u3-l4**。顺手推荐阅读 [examples/multi-round-chat/src/multi_round_chat.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/multi-round-chat/src/multi_round_chat.ts) 全文——不到 80 行，是本讲所有结论的可运行证明。
