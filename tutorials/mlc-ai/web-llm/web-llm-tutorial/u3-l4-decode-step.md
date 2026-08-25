# u3-l4 decodeStep：解码循环与终止条件

## 1. 本讲目标

上一讲（u3-l3）我们搞清楚了 prefill：它把整段 prompt 一次性（或分块）喂进模型，写出全部 KV cache，并采样出**第一个**回复 token。本讲顺着这条线往下走，读完你应当能够：

1. 说出 `decodeStep` 的循环结构：为什么每一步只前向一个 token，以及这一步在引擎层被谁驱动。
2. 完整列出 `processNextToken` 中所有会触发停止的条件（停止 token、停止字符串、`max_tokens`、上下文窗口、手动中断），并注明各自的源码位置与对应的 `finish_reason`。
3. 理解 `filledKVCacheLength` 这个游标如何在 prefill / decode 中被维护，`context_window_size` 与滑动窗口（sliding window）对长对话分别意味着什么。
4. 读懂解码阶段的统计口径（`decodingTotalTokens`、`curRoundDecodingTotalTime`、decode tokens/s），并能用实验页面验证。

## 2. 前置知识

- **自回归生成**：语言模型每一步只预测"下一个 token"。把已生成的 token 再喂回模型，得到新 token，如此循环，直到某个条件叫停。prefill 产出第 1 个 token，decode 产出第 2、3、4……个。
- **KV cache**：Transformer 的注意力计算需要用到历史所有 token 的 Key/Value 向量。把它们缓存起来，每步只需为**新来的那一个** token 计算 K/V 并追加，避免重复计算整段历史。`filledKVCacheLength` 记录"缓存里已经写入了多少个 token"。
- **停止 token（stop token）与停止字符串（stop string）**：停止 token 是词表里的特定 token id（最典型的是 EOS，end-of-sequence），模型"自己说出来就该停"；停止字符串是调用方指定的文本片段（如 `"。"` 或 `"\n\nUser:"`），一旦出现在输出里就截断。
- **finish_reason**：OpenAI 风格响应里每个 choice 都带一个终止原因。WebLLM 中你会见到 `"stop"`（自然停止：停在了停止 token/停止字符串）、`"length"`（被长度限制截断：`max_tokens` 或上下文窗口）、`"tool_calls"`（函数调用，见 u6-l2）、`"abort"`（用户中断，见 u2-l3）。
- **滑动窗口注意力**：普通 KV cache 保留全部历史；滑动窗口模型只保留最近 N 个 token 的 KV（外加少量 attention sink 开头的 token），超出的部分自动丢弃，因此理论上可以"无限聊下去"，但会遗忘窗口外的内容。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/llm_chat.ts` | 推理管线 `LLMChatPipeline` | `decodeStep`、`processNextToken`、`triggerStop`、`embedAndForward`、状态字段与统计字段 |
| `src/engine.ts` | 引擎编排层 | 驱动 decode 的 `while` 循环、`interruptGenerate`、`decode()` 封装 |
| `src/config.ts` | 配置类型与预置模型 | `ChatConfig` 的 KV cache 三字段、`GenerationConfig` 的 `max_tokens`/`stop`/`ignore_eos` |
| `src/conversation.ts` | 对话模板 | `getStopStr()`/`getStopTokens()`：停止条件的第一来源 |
| `src/openai_api_protocols/chat_completion.ts` | 请求协议 | `stop`、`max_tokens`、`ignore_eos` 字段的文档与校验 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**4.1 decodeStep 主循环**、**4.2 终止条件判定**、**4.3 统计与 KV 长度维护**。

### 4.1 decodeStep 主循环

#### 4.1.1 概念说明

`decodeStep` 是"生成一个新 token"的最小单元。与 prefill 一次吃几十上百个 token 不同，decode 每次只把**上一步刚采样出来的那一个 token** 喂进模型。这么做的原因是自回归模型的输出天然是一个 token 一个 token 产出的：在得到第 t 个 token 之前，你不知道第 t+1 步该喂什么。

为什么每步只前向 1 个 token 却不慢？因为 KV cache 里已经存好了前 t-1 个 token 的 K/V，本步只需：为这 1 个新 token 算 embedding → 算它的 K/V 追加进 cache → 用它去"查询"整个 cache 做注意力 → 输出下一 token 的 logits。计算量远小于把整段历史重算一遍（那是 prefill 干的事）。

#### 4.1.2 核心流程

一次 `decodeStep` 的完整流程：

```text
decodeStep(genConfig)
  ├─ 1. 守卫：若 stopTriggered 已为 true，直接抛错（不能在停止后继续 decode）
  ├─ 2. 取"上一个 token"：outputIds 的最后一个元素，包成长度为 1 的 chunk
  ├─ 3. embedAndForward(chunk, 1)
  │     ├─ 把 1 个 token 转 embedding
  │     ├─ kv_state_begin_forward 预留 1 个 KV 槽位
  │     ├─ inputDataLen == 1 → 走 decode 内核（而非 prefill 内核）
  │     ├─ kv_state_end_forward 提交
  │     └─ filledKVCacheLength += 1
  ├─ 4. 不变量断言：filledKVCacheLength 必须恰好 +1，否则 Internal Error
  ├─ 5. sampleTokenFromLogits(logits, genConfig)：从 logits 采样出 nextToken
  ├─ 6. 更新统计：decodingTotalTokens += 1、计时累加
  └─ 7. processNextToken(nextToken, genConfig)：追加 token 并判定是否停止
```

而 `decodeStep` 本身**不自带循环**——循环在引擎层。以流式路径为例，引擎先做一次 prefill，然后 `while (!pipeline.stopped())` 反复调用 decode：

```text
引擎层（asyncGenerate 流式路径）:
  prefill(request)          → 产出第 1 个 token，yield 首个 chunk
  while (!pipeline.stopped()):
      若 interruptSignal → pipeline.triggerStop(); break   # 手动中断
      decode(pipeline)    → pipeline.decodeStep(genConfig) → 产出下一个 token
      _getChunk() → yield 增量 chunk
  收尾：读 pipeline.getFinishReason()，yield 空 delta 的终止帧
```

#### 4.1.3 源码精读

先看管线状态字段——它们是理解 decode 的钥匙（[src/llm_chat.ts:97-113](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L97-L113)）：`filledKVCacheLength` 是 KV 游标；`contextWindowSize`/`slidingWindowSize`/`prefillChunkSize` 是窗口三参数；`stopStr`/`stopTokens` 是停止条件来源；`outputMessage`/`outputIds` 是当前轮已生成内容（字符串与 token id 两种形态）；`stopTriggered`/`finishReason` 是停止状态。

`decodeStep` 主体（[src/llm_chat.ts:902-936](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L902-L936)）：

```ts
async decodeStep(genConfig?: GenerationConfig): Promise<void> {
  if (this.stopTriggered) {
    throw Error("Cannot run decode when stopped");
  }
  const tstart = performance.now();
  this.tvm.beginScope();
  const chunk: Array<Array<number>> = [
    this.outputIds.slice(this.outputIds.length - 1),   // 只取最后一个 token
  ];
  ...
  const logits = this.tvm.detachFromCurrentScope(
    await this.embedAndForward(chunk, chunkLen),       // 前向 1 个 token
  );
  if (this.filledKVCacheLength !== prevFilledLen + chunkLen) { ... }  // 不变量
  ...
  const nextToken = await this.sampleTokenFromLogits(logits, genConfig);  // 采样
  ...
  this.processNextToken(nextToken, genConfig);         // 追加 + 判停
}
```

几个关键点：

- [src/llm_chat.ts:910-913](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L910-L913)：`outputIds.slice(this.outputIds.length - 1)` 取出上一步的 token，构造长度为 1 的 chunk——这就是"每次只前向一个 token"的字面体现。
- [src/llm_chat.ts:903-905](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L903-L905)：停止后再调 decode 会抛错，这是对引擎层循环的兜底（正常循环用 `stopped()` 判断，不会走到这里）。
- [src/llm_chat.ts:918-922](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L918-L922)：不变量断言——decode 一步后 KV 游标必须恰好 +1，用来在开发期抓住 KV cache 记账错误。

`embedAndForward` 中按输入长度分派内核的分支（[src/llm_chat.ts:1273-1277](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1273-L1277)）：`inputDataLen > 1` 调 prefill 内核，`== 1` 调 decode 内核。也就是说 prefill 与 decode 共用这一个前向原语，区别只在输入长度——这解释了 u3-l3 里提到的 `this.prefill` 字段背后可能是 `batch_prefill` 的 ABI 差异：管线侧统一，内核侧按长度分流。KV 游标递增在 [src/llm_chat.ts:1283](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1283)（`this.filledKVCacheLength += inputDataLen`）。

引擎层的驱动循环（流式路径，[src/engine.ts:608-638](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L608-L638)）：prefill 一次、然后 `while (!pipeline.stopped())` 里先查中断信号再 `await this.decode(pipeline, genConfig)`；非流式路径是同构的循环（[src/engine.ts:471](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L471)）。`this.decode` 只是对 `pipeline.decodeStep` 的一行封装（[src/engine.ts:1426-1431](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1426-L1431)）。注意循环判据是 `pipeline.stopped()`（[src/llm_chat.ts:564-566](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L564-L566)），即 `stopTriggered` 布尔位；这也是 u2-l3 讲过的 interruptGenerate 能"每步生效"的原因——信号只在每轮循环开头被检查。

另外注意：prefill 产出的第一个 token 同样会走 `processNextToken`（在 prefillStep 末尾调用，[src/llm_chat.ts:899](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L899)）。所以"停止判定"对第一个 token 一样生效——`max_tokens: 1` 的请求只会有 prefill、不会有任何 decodeStep。

#### 4.1.4 代码实践

**实践目标**：亲手跟踪一次 decode 循环的调用链，确认"每 token 一次 decodeStep"。

**操作步骤**（源码阅读型实践，不修改仓库）：

1. 从 [src/engine.ts:621](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L621) 的 `while (!pipeline.stopped())` 出发，沿 `this.decode` → `pipeline.decodeStep` → `embedAndForward` → `processNextToken` 画出调用链图。
2. 运行 u1-l2 跑通的 get-started 示例（`examples/get-started`，`npm install && npm start`），在浏览器 DevTools 控制台中发起一个 `max_tokens: 40` 的请求。
3. 在本地工作副本里，给 `processNextToken` 开头临时加一行日志（**示例代码**，实验后请还原，不要提交）：

```ts
console.debug(
  `[decode] outputIds.length=${this.outputIds.length}, nextToken=${nextToken}` +
  `, filledKVCacheLength=${this.filledKVCacheLength}`,
);
```

**需要观察的现象**：每次 decodeStep 打印一行，`outputIds.length` 从 0 逐个 +1（第一个 token 来自 prefill 的调用），`filledKVCacheLength` 等于 prompt 长度 + 已生成 token 数，两者同步增长。

**预期结果**：日志行数 ≈ `max_tokens`（或提前停止），证明一次生成 = 1 次 prefill + N 次 decodeStep。

（若不想改代码，也可只做第 1、2 步，用响应里的 `usage.completion_tokens` 代替日志行数验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `decodeStep` 不像 `prefillStep` 那样需要 `getChunkedPrefillInputData` 做分块？

**答案**：decode 每步输入长度恒为 1，天然满足 `inputDataLen <= prefillChunkSize`（`prefillChunkSize` 恒为正，见 [src/llm_chat.ts:353-357](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L353-L357) 的校验）。分块是为了把长 prompt 切成显存能承受的块，与单 token 前向无关。

**练习 2**：`embedAndForward` 里 `inputDataLen > 1` 与 `== 1` 分别走什么内核？为什么管线不直接调 `this.decoding`？

**答案**：`> 1` 走 prefill 内核（`invokePrefill`），`== 1` 走 decode 内核（`invokeDecode`），见 [src/llm_chat.ts:1273-1277](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1273-L1277)。prefill 与 decode 的 GPU kernel 形状和优化策略不同（prefill 是并行批处理、decode 是逐 token 访存受限），统一入口 `embedAndForward` 让 prefillStep/decodeStep/forwardTokensAndSample 共享嵌入与 KV 事务逻辑，再按长度分流到正确的内核。

**练习 3**：如果一个请求 `max_tokens: 1`，会发生几次 decodeStep？

**答案**：0 次。prefill 产出第一个 token 后立即调用 `processNextToken`，此时 `outputIds.length（1）>= max_tokens（1）` 触发停止（见 4.2.3 条件 3），`stopped()` 变 true，引擎层 while 循环体一次都不进。

### 4.2 终止条件判定

#### 4.2.1 概念说明

`processNextToken` 是停止判定的唯一入口，prefill 产出的第一个 token 和 decode 产出的每个 token 都经过它。它做三件事：把新 token 追加进 `outputIds`（除非它是停止 token）、把 `outputIds` 解码成 `outputMessage` 并检查停止字符串、然后逐条检查长度类条件。

停止条件一共**五类**，来源与 `finish_reason` 各不相同：

| # | 条件 | 来源 | finish_reason | 触发位置 |
| --- | --- | --- | --- | --- |
| 1 | 停止 token（含 EOS） | 模型对话配置 `stop_token_ids`，`ignore_eos` 可清空 | `"stop"` | llm_chat.ts:998-1002 |
| 2 | 停止字符串 | 对话配置 `stop_str` ∪ 请求级 `stop` 参数 | `"stop"` | llm_chat.ts:1014-1027 |
| 3 | 超过 `max_tokens` | 请求参数 | `"length"` | llm_chat.ts:1029-1034 |
| 4 | KV 写满 `context_window_size` | 模型/ChatConfig（仅非滑动窗口模型） | `"length"` | llm_chat.ts:1036-1044 |
| 5 | 手动中断 `interruptGenerate()` | 用户 | `"abort"` | llm_chat.ts:941-950（triggerStop） |

其中条件 1~4 在 `processNextToken` 内**按固定顺序逐条执行、不提前返回**；条件 5 在引擎层循环里触发 `triggerStop()`。若同一个 step 内多个条件同时命中，**后判定的条件会覆盖前面的 `finishReason`**（`stopTriggered` 一旦为 true 就不会再追加 token，但 `finishReason` 是普通赋值）。最常见的重叠是"停止字符串命中的那一步恰好也到了 `max_tokens`"，此时最终报 `"length"`。

#### 4.2.2 核心流程

```text
processNextToken(nextToken, genConfig):
  读参数：max_tokens（缺省 Infinity，<=0 抛 MinValueError）
          ignore_eos（缺省 false；为 true 时清空 stopTokens 与 stopStrs）
          stopStrs = conversation.stop_str ∪ genConfig.stop
  ├─ 条件1: nextToken ∈ stopTokens？
  │     是 → stopTriggered=true, finishReason="stop"（该 token 不进 outputIds）
  ├─ 若未停止：outputIds.push(nextToken)；更新 appearedTokensFreq（供惩罚项用）
  ├─ outputMessage = tokenizer.decode(outputIds 全量)
  ├─ 条件2: 任一 stopStr 出现在 outputMessage？
  │     是 → 从停止串处截断 outputMessage，stopTriggered=true, finishReason="stop"
  ├─ 条件3: outputIds.length >= max_tokens？
  │     是 → stopTriggered=true, finishReason="length"
  ├─ 条件4: 无滑动窗口 且 filledKVCacheLength == contextWindowSize？
  │     是 → stopTriggered=true, finishReason="length"
  └─ 若 stopTriggered 且非文本补全：conversation.finishReply(outputMessage)
       —— 把回复闭环写进对话历史，供下一轮增量编码复用
```

两个容易忽略的细节：

- **停止 token 不计入 `outputIds`**：它在被追加之前就被拦截（条件 1 先判再 push），因此 EOS 不会出现在最终文本里，也不占 `max_tokens` 名额；而**停止字符串的 token 会被追加**（不追加就凑不出这个字符串），然后再在文本层截断，所以它占 `max_tokens` 名额。
- **`max_tokens` 统计的是本轮全部生成 token**：包括 prefill 产出的第一个。因为第一个 token 也走了一遍 `processNextToken`。

#### 4.2.3 源码精读

**条件 1：停止 token**（[src/llm_chat.ts:998-1012](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L998-L1012)）：

```ts
// Stop condition 1: stop token; otherwise, append to `this.outputIds`
if (stopTokens.includes(nextToken)) {
  this.stopTriggered = true;
  this.finishReason = "stop";
}
if (!this.stopTriggered) {
  this.outputIds.push(nextToken);
  // Update token appearance frequency
  ...
}
```

`stopTokens` 来自会话对象（构造时与 `setConversation` 时同步，[src/llm_chat.ts:197-198](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L197-L198)、[src/llm_chat.ts:711-712](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L711-L712)），最终源头是对话配置的 `stop_token_ids`（[src/conversation.ts:289-300](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L289-L300)）。同一段代码还顺带维护 `appearedTokensFreq`——这是下一讲（u3-l5）频率惩罚的数据来源，说明"记账"与"判停"被合并在同一次遍历里。

**条件 2：停止字符串**（[src/llm_chat.ts:1014-1027](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1014-L1027)）：

```ts
let outputMessage = this.tokenizer.decode(new Int32Array(this.outputIds));
let stopPos = -1;
for (const stopStr of stopStrs) {
  stopPos = outputMessage.lastIndexOf(stopStr);
  if (stopPos != -1) {
    outputMessage = outputMessage.substring(0, stopPos);
    this.stopTriggered = true;
    this.finishReason = "stop";
    break;
  }
}
this.outputMessage = outputMessage;
```

注意它每一步都**全量重解码** `outputIds` 再 `lastIndexOf`。实现简单（也顺带刷新了 `outputMessage`），代价是每步 O(已生成长度) 的解码开销——生成几百 token 时可忽略，但这是理解"逐 token 计时里含一小段 CPU 工作"的一个来源。`stopStrs` 的组装在 [src/llm_chat.ts:986-990](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L986-L990)：模型配置的 `stop_str` 与请求级 `genConfig.stop` 做 `concat` 合并，两边都生效。

**条件 3：`max_tokens`**（[src/llm_chat.ts:1029-1034](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1029-L1034)）：`this.outputIds.length >= max_tokens` → `"length"`。`max_tokens` 的读取与校验在 [src/llm_chat.ts:966-974](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L966-L974)（缺省 `Infinity`，`<= 0` 抛 `MinValueError`）；请求进入时的范围校验则在 [src/config.ts:189-191](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L189-L191)（`postInitAndCheckGenerationConfigValues`）。

**条件 4：上下文窗口**（[src/llm_chat.ts:1036-1044](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1036-L1044)）：

```ts
if (
  this.slidingWindowSize == -1 &&
  this.filledKVCacheLength == this.contextWindowSize
) {
  this.stopTriggered = true;
  this.finishReason = "length";
}
```

两个要点：其一，用 `==` 而非 `>=` 是安全的——decode 每步 KV 恰好 +1（4.1 的不变量断言），prefill 前还有一道防线（见 4.3.3），所以一定会"恰好等于"时被截住。其二，`slidingWindowSize == -1` 的前置判断把滑动窗口模型排除在外：它们的 KV cache 由 GPU 侧自动淘汰旧页，不存在"写满"一说（详见 4.3.1）。

**收尾**：[src/llm_chat.ts:1046-1051](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1046-L1051)——停止后若非文本补全模式，调用 `conversation.finishReply(this.outputMessage)` 把回复闭环进对话历史（u3-l2 讲过的 undefined 哨兵在这里被回填）。

**条件 5：手动中断**。引擎的 `interruptGenerate()` 只置一个布尔（[src/engine.ts:771-772](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L771-L772)）；decode 循环每轮开头检查到信号后调用 `pipeline.triggerStop()`（[src/engine.ts:622-627](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L622-L627)），后者置 `finishReason = "abort"` 并同样 `finishReply`（[src/llm_chat.ts:941-950](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L941-L950)）——这就是 u2-l3 详述的协作式取消。

最后，`ignore_eos` 的处理在 [src/llm_chat.ts:976-996](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L976-L996)：为 true 时同时清空 `stopTokens` 和 `stopStrs`，于是只剩 `max_tokens`/窗口/中断能让生成停下。该字段可直接在 chat 请求里传（协议定义见 [src/openai_api_protocols/chat_completion.ts:254-257](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L254-L257)），典型用途是压测纯解码速度。`stop` 与 `max_tokens` 的请求字段定义在 [src/openai_api_protocols/chat_completion.ts:144-149](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L144-L149)，`GenerationConfig` 汇总见 [src/config.ts:145-165](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L145-L165)。

#### 4.2.4 代码实践

**实践目标**：用实验页面验证 `"stop"`（停止字符串）与 `"length"`（max_tokens）两种终止原因的确切来源，并亲手列出全部停止条件。

**操作步骤**：

1. 复制 `examples/get-started` 为一个新目录（示例工程结构见 u1-l2），修改入口脚本（**示例代码**）：

```ts
import * as webllm from "@mlc-ai/web-llm";

const engine = await webllm.CreateMLCEngine(
  "Llama-3.2-1B-Instruct-q4f32_1-MLC",
);

// 实验一：停止字符串截断 → 预期 finish_reason === "stop"
const r1 = await engine.chatCompletion({
  messages: [{ role: "user", content: "从 1 数到 20，每个数字一行。" }],
  stop: ["6"],          // 很短的停止字符串
  temperature: 0,
  max_tokens: 512,
});
console.log("[实验一]", JSON.stringify(r1.choices[0]));

// 实验二：很小的 max_tokens → 预期 finish_reason === "length"
const r2 = await engine.chatCompletion({
  messages: [{ role: "user", content: "从 1 数到 20，每个数字一行。" }],
  max_tokens: 5,
  temperature: 0,
});
console.log("[实验二]", JSON.stringify(r2.choices[0]));

// 实验三：ignore_eos + max_tokens，观察 EOS 被无视
const r3 = await engine.chatCompletion({
  messages: [{ role: "user", content: "只回答：好" }],
  ignore_eos: true,
  max_tokens: 32,
  temperature: 0,
});
console.log("[实验三]", r3.choices[0].finish_reason, r3.choices[0].message);
```

2. `npm install && npm start` 后打开页面，记录三组输出的 `finish_reason`、`message.content` 与 `usage.completion_tokens`。
3. 对照源码整理"停止条件清单表"：把 4.2.1 那张表的每一行，换成你自己验证过的证据（实验编号或源码行号）。

**需要观察的现象**：

- 实验一：回复在 `6` 之前被截断，`finish_reason === "stop"`，且停止串本身不出现在 `content` 中（对应 [src/llm_chat.ts:1019-1024](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1019-L1024) 的 `substring(0, stopPos)` 截断）。
- 实验二：`content` 恰好 5 个 token 左右被截断，`finish_reason === "length"`，`completion_tokens === 5`（对应 [src/llm_chat.ts:1029-1034](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1029-L1034)）。
- 实验三：即使模型想说一个字就停（EOS），也会被拉满到 32 个 token——验证 `ignore_eos` 清空停止表的行为（[src/llm_chat.ts:992-996](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L992-L996)）。

**预期结果**：三组 finish_reason 依次为 `"stop"`、`"length"`、`"length"`；实验二的 `completion_tokens` 精确等于 5（若你的 tokenizer 把标点合并计数，以 `usage.completion_tokens` 为准——它统计的就是 `outputIds.length`）。若某组与预期不符，回到 4.2.3 的对应代码段排查。

（模型输出内容与本地浏览器环境有关，具体文本**待本地验证**；三个 finish_reason 的模式是源码确定性保证的。）

#### 4.2.5 小练习与答案

**练习 1**：`max_tokens: 10` 且模型第 10 个 token 恰好拼出了停止字符串，最终 `finish_reason` 是什么？

**答案**：`"length"`。同一个 `processNextToken` 调用里，条件 2 先把 `finishReason` 置为 `"stop"`，随后条件 3（`outputIds.length >= max_tokens`）又把它覆盖为 `"length"`（[src/llm_chat.ts:1029-1034](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1029-L1034) 无 `else` 守卫）。这是"逐条覆盖、后判优先"的直接后果。

**练习 2**：为什么停止 token 不占 `max_tokens` 名额，而停止字符串占？

**答案**：条件 1 在 push 之前判定（[src/llm_chat.ts:998-1004](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L998-L1004)），停止 token 根本不进 `outputIds`，`outputIds.length` 不变；而停止字符串必须等它的 token 被追加、解码成文本后才可能被 `lastIndexOf` 找到，所以这些 token 已计入 `outputIds.length`。

**练习 3**：把 `stop: ["6"]` 换成 `stop: "6"`（字符串而非数组）还能工作吗？

**答案**：协议上 `stop` 的类型是 `string | null | Array<string>`（[src/openai_api_protocols/chat_completion.ts:149](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L149)），协议层校验会拒绝非数组/非字符串之外的形态；传单个字符串是协议允许的写法，最终会进入 `stopStrs` 合并逻辑（[src/llm_chat.ts:986-990](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L986-L990) 只做 `concat`）。稳妥起见建议总是传数组。

### 4.3 统计与 KV 长度维护

#### 4.3.1 概念说明

这一模块回答三个问题：**KV 游标怎么维护**、**长对话撞上上下文窗口会怎样**、**解码速度怎么统计**。

`filledKVCacheLength` 是整条管线的"账本"：prefill 加 prompt 长度、decode 每步加 1、`resetChat` 清零。它的三个消费方：decodeStep 的不变量断言、条件 4 的窗口判停、prefill 前的超限预检。

`context_window_size` 与 `sliding_window_size` 是互斥的两种 KV 策略（构造期强制二选一，见 [src/llm_chat.ts:359-381](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L359-L381)）：

- **固定窗口**：一次性分配 `context_window_size` 大小的满分页 KV cache。写满 → decode 以 `"length"` 停止；prompt 本身超限 → prefill 前直接抛 `ContextWindowSizeExceededError`。
- **滑动窗口**：分配 `sliding_window_size` 大小的环形 KV cache，配 `attention_sink_size` 个开头 token 锚定。超出窗口的旧 KV 由 GPU 侧自动淘汰，decode 永不因窗口停止，但模型"看不到"窗口外的历史。

解码统计分两层：`decodingTotalTokens/decodingTotalTime`（跨轮累计）与 `curRoundDecodingTotalTokens/curRoundDecodingTotalTime`（每轮 prefill 时清零）。对外暴露为 tokens/s：\(\text{decode tokens/s} = \frac{\text{curRoundDecodingTotalTokens}}{\text{curRoundDecodingTotalTime}}\)。

#### 4.3.2 核心流程

KV 游标的一生：

```text
构造期:  filledKVCacheLength = 0；按窗口策略分配 KV cache（固定 or 滑动）
reload/resetChat: resetKVCache() → filledKVCacheLength = 0
每轮 prefillStep:
  预检: 无滑动窗口 且 numPromptTokens + filledKVCacheLength > contextWindowSize
        → 抛 ContextWindowSizeExceededError（错误，而非 finish_reason）
  逐块前向: 每块 filledKVCacheLength += chunkLen
  清零本轮统计: curRoundDecodingTotalTokens = 0 等
每步 decodeStep:
  filledKVCacheLength += 1（断言校验）
  decodingTotalTokens += 1; curRoundDecodingTotalTokens += 1
  计时累加 decodingTotalTime / curRoundDecodingTotalTime
停止: 条件 4 用 filledKVCacheLength == contextWindowSize 判满
```

多轮对话时游标**不清零**（这正是 u2-l2 讲的 KV 复用）：第二轮 prefill 只把"增量 prompt"写进 cache，所以 `filledKVCacheLength` 随轮次单调增长，直到逼近窗口上限。

#### 4.3.3 源码精读

**窗口策略的构造期约束**（[src/llm_chat.ts:359-381](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L359-L381)）：读入 `sliding_window_size`/`context_window_size`/`attention_sink_size`，两者同时有效则抛 `WindowSizeConfigurationError`；用滑动窗口但没配 attention sink 抛 `AttentionSinkSizeError`；两者都是 -1 抛 `WindowSizeSpecificationError`。这两个字段的类型定义在 [src/config.ts:93-98](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L93-L98)。

KV cache 的分配容量取决于窗口策略（[src/llm_chat.ts:419-439](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L419-L439)）：`maxTotalSeqLen = slidingWindowSize != -1 ? slidingWindowSize : contextWindowSize`，传给 `create_tir_paged_kv_cache` 作为 `max_total_sequence_length`。滑动窗口模型的环形淘汰在 `resetKVCache` 里通过 `fKVCacheEnableSlidingWindowForSeq` 打开（[src/llm_chat.ts:551-558](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L551-L558)），之后超出窗口的页由 GPU 侧自动覆盖——这就是条件 4 要加 `slidingWindowSize == -1` 守卫的原因。

**prefill 前的超限预检**（[src/llm_chat.ts:2124-2133](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2124-L2133)）：

```ts
if (
  this.slidingWindowSize == -1 &&
  numPromptTokens + this.filledKVCacheLength > this.contextWindowSize
) {
  throw new ContextWindowSizeExceededError(numPromptTokens, this.contextWindowSize);
}
```

对比条件 4：**输入侧超限是抛错误**（请求失败，模型无输出），**生成侧写满是优雅停止**（`finish_reason: "length"`，输出保留已生成部分）。两者合起来保证游标永远 ≤ 窗口。

**本轮统计的清零点**在 prefillStep（[src/llm_chat.ts:733-760](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L733-L760)）：`outputIds = []`、`curRoundDecodingTotalTokens = 0`、`curRoundDecodingTotalTime = 0`、`stopTriggered = false` 等，全部逐字段清零——这是"一轮"的边界。字段定义与注释见 [src/llm_chat.ts:123-132](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L123-L132)（跨轮统计在 `resetChat` 时清零，[src/llm_chat.ts:530-540](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L530-L540)，同时把 `filledKVCacheLength` 归零）。

**decode 侧的记账**（[src/llm_chat.ts:928-933](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L928-L933)）：`this.decodingTotalTime += (tend - tstart) / 1e3; this.decodingTotalTokens += 1;`——计时区间覆盖"前向 + 采样"全过程，即一个 token 的完整产出成本。对外读取口：`getCurRoundDecodingTotalTokens/Time`（[src/llm_chat.ts:586-609](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L586-L609)）、`getCurRoundDecodingTokensPerSec`（[src/llm_chat.ts:663-665](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L663-L665)）、文本形式 `runtimeStatsText`/`curRoundRuntimeStatsText`（[src/llm_chat.ts:636-651](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L636-L651)）。这些数字最终汇入响应的 `usage.extra`（`decode_tokens_per_s` 等，u3-l3 已演示），引擎层的 `runtimeStatsText()` 查询口在 [src/engine.ts:1315-1323](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1315-L1323)（注意其弃用警告）。

#### 4.3.4 代码实践

**实践目标**：亲眼看两次"窗口"行为——固定窗口模型撞 `context_window_size` 的两种形态，并核对 decode 统计口径。

**操作步骤**（**示例代码**，基于 get-started 工程）：

1. 选一个固定窗口模型（如 `Llama-3.2-1B-Instruct-q4f32_1-MLC`，`context_window_size: 4096`，可在 `src/config.ts` 的 `prebuiltAppConfig.model_list` 中查到）。
2. 实验四（输入侧超限 → 抛错误）：

```ts
try {
  const longInput = "重复段落。".repeat(2000);   // 远超 4096 token
  const r = await engine.chatCompletion({
    messages: [{ role: "user", content: longInput }],
    max_tokens: 8,
  });
  console.log(r.choices[0].message);
} catch (e) {
  console.error("[实验四] 捕获错误：", (e as Error).name, (e as Error).message);
}
```

3. 实验五（生成侧写满 → `finish_reason: "length"`）：发一个短 prompt、`ignore_eos: true`、`max_tokens` 设成一个大于"窗口剩余额度"的大值（例如 5000），观察生成在何处停下。
4. 对每次成功的请求，用 `usage.completion_tokens` 与 `usage.extra.decode_tokens_per_s`（若你的版本输出该字段）核对：`completion_tokens` 是否等于本轮 `outputIds.length`、tokens/s 是否量级合理（1B q4 模型在桌面浏览器常见量级为每秒十几个 token，具体**待本地验证**）。

**需要观察的现象**：实验四抛出 `ContextWindowSizeExceededError`（对应 [src/llm_chat.ts:2124-2133](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2124-L2133)），而不是带 `finish_reason` 的正常响应；实验五回复被截断且 `finish_reason === "length"`，但截断点不在 `max_tokens`——是 KV 写满窗口所致（对应 [src/llm_chat.ts:1036-1044](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1036-L1044)）。

**预期结果**：你能写出这样的结论——"输入超窗口 = 异常路径，生成写满窗口 = 正常的 length 终止；两者都以 `filledKVCacheLength` 为准绳"。滑动窗口模型（如部分 Gemma 系）不会出现实验五的现象，可换模型对比后把差异记入笔记。

#### 4.3.5 小练习与答案

**练习 1**：多轮对话中第二轮的 `filledKVCacheLength` 起点是多少？

**答案**：等于第一轮结束时的值（prompt + 已生成 token 总数）。KV 复用意味着游标跨轮不清零，只有 `resetChat()`（[src/llm_chat.ts:530-540](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L530-L540)）才清零。因此长对话的可用生成空间 = `context_window_size - filledKVCacheLength`，会越聊越窄，直到 prefill 预检抛错或 decode 写满。

**练习 2**：为什么 decode 的 tokens/s 通常远低于 prefill 的 tokens/s，却能支撑打字机体验？

**答案**：prefill 是并行批处理（一次前向几十上百 token），吞吐高但只发生一次；decode 每次前向 1 个 token，访存受限、吞吐低，但每秒仍能产出十几个 token，与人眼阅读速度匹配。两者的统计口径分别对应 `prefill_tokens_per_s` 与 `decode_tokens_per_s`（[src/llm_chat.ts:646-665](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L646-L665)）。

**练习 3**：`resetChat()` 与新一轮 `prefillStep()` 都会重置一些状态，它们的分工是什么？

**答案**：`resetChat` 清**会话级**状态——对话历史、KV cache（游标归零）、跨轮统计（[src/llm_chat.ts:530-540](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L530-L540)）；`prefillStep` 清**轮次级**状态——`outputIds`、`outputMessage`、`stopTriggered`、本轮统计（[src/llm_chat.ts:739-760](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L739-L760)），但**不动** KV cache（除非检测到历史被篡改而整体重算，见 u3-l2）。

## 5. 综合实践

**做一个「解码观察台」页面**，把本讲三个模块串起来：

1. 基于 `examples/get-started` 搭一个页面，包含：模型加载、一个输入框、三个按钮（`stop 串实验` / `max_tokens 实验` / `ignore_eos 压测`）。
2. **终止原因表格**：每次请求结束后，在页面上追加一行记录 `[实验类型 | finish_reason | completion_tokens | content 末尾 20 字符]`。跑满 u3-l4 4.2.4 的三个实验与 4.3.4 的实验五，应能集齐 `"stop"`、`"length"`（max_tokens 来源）、`"length"`（窗口来源）三种终止路径的证据。
3. **停止条件清单**：对照源码手写一张五行的停止条件表（条件、源码行号、finish_reason、你的验证方式），作为页面的"文档区"。
4. **解码速度**：用 `ignore_eos: true, max_tokens: 64` 跑三轮同 prompt 请求，读取每轮 `usage.extra`（或 `engine.runtimeStatsText()`，注意弃用提示）中的 decode tokens/s，取平均作为你机器上该模型的解码速度基线；再用 u2-l3 的流式接口测首 chunk 到达时间，粗略分离 prefill 与 decode 的耗时占比。
5. **选做**：换一个滑动窗口模型重复第 2 步，验证它永远不会因窗口得到 `"length"`（此时 `"length"` 只可能来自 `max_tokens`）。

验收标准：页面上的终止原因表格能和你的停止条件清单一一对应；每一个 `finish_reason` 你都能指出它在 `src/llm_chat.ts` 里的赋值行。

## 6. 本讲小结

- `decodeStep` 每次只把上一步采样的**一个** token 前向（[llm_chat.ts:910-917](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L910-L917)），借 KV cache 免于重算历史；循环本身在引擎层（`while (!pipeline.stopped())`）。
- 停止判定集中在 `processNextToken`：停止 token、停止字符串、`max_tokens`、上下文窗口四条按序执行、后判覆盖先判；手动中断走 `triggerStop()` 给出 `"abort"`。
- 停止 token 在追加前被拦截（不占 `max_tokens` 名额）；停止字符串在文本层截断（占名额）。`ignore_eos` 会同时清空两类停止表。
- `filledKVCacheLength` 是全局账本：prefill 加块长、decode 加一（有不变量断言）、resetChat 清零、多轮复用不清零；输入侧超窗抛 `ContextWindowSizeExceededError`，生成侧写满则优雅地以 `"length"` 停止。
- 滑动窗口模型用 `sliding_window_size + attention_sink_size` 的环形 KV 自动淘汰旧页，因此不存在窗口判停，代价是遗忘窗口外历史。
- 解码统计分跨轮/本轮两层，每步 `decodingTotalTokens += 1` 且计时含采样，最终以 `usage.extra` 的 `decode_tokens_per_s` 等字段对外暴露。

## 7. 下一步学习建议

下一讲 **u3-l5 采样控制：GenerationConfig、惩罚与 LogitProcessor** 将补上本讲留下的最后一环：`sampleTokenFromLogits` 内部发生了什么——`temperature`/`top_p` 如何作用于 logits，`appearedTokensFreq`（本讲条件 1 顺带维护的那张词频表）如何被频率/出现惩罚消费，以及你如何用 `LogitProcessor` 在采样前改写 logits。读完 u3 单元后，建议把 `src/llm_chat.ts` 从头到尾再通读一遍——此时 prefill、decode、采样、统计四大件已全部就位，你会发现整条管线不过 2000 余行，且每个方法都能对应到手册里的某一讲。随后可进入单元四（缓存与完整性）或单元五（Worker 架构），视你想先深入"模型分发"还是"多线程架构"而定。
