# 性能观测：延迟分解与运行统计

## 1. 本讲目标

学完本讲，你应该能够：

1. 区分**首 token 延迟（TTFT）**与**逐 token 解码延迟**这两种性质完全不同的开销，并说清它们各自由哪些阶段构成。
2. 读懂 `usage.extra` 中 `time_to_first_token_s`、`prefill_tokens_per_s`、`decode_tokens_per_s`、`time_per_output_token_s`、`e2e_latency_s` 等字段的**统计口径**（分子是什么、分母是什么、从哪个时刻计到哪个时刻）。
3. 读懂 `curRoundLatencyBreakdown` 六个数组的含义，明白它们只覆盖「采样链」而不覆盖模型前向，并能用 `enable_latency_breakdown` 开关拿到它们。
4. 知道 `runtimeStatsText()` 已被标记废弃的原因，以及它与 `usage.extra` 各自适用的场景。
5. 独立设计并执行一个「改变 prompt 长度、观测 TTFT 与解码速度变化」的性能实验。

## 2. 前置知识

### 2.1 两种延迟：TTFT 与解码速度

一次生成请求的时间轴可以粗略切成三段：

```
请求到达 ──> [排队/校验] ──> prefill(整段 prompt 并行前向) ──> 第一个 token 出现
                                    │
                                    v
             decode(逐 token 前向) x N 次 ──> 生成结束
```

- **TTFT（Time To First Token）**：从请求进来到第一个字出现的时间，主要由 prefill 决定。prefill 是「批量并行」的——整段 prompt 一次（或分块）送进模型（回顾 u3-l3），计算量与 prompt 长度成正比，所以**长 prompt 直接拉长 TTFT**。
- **逐 token 解码延迟**：之后每生成一个 token 的时间，主要由 decode 决定。decode 是「串行」的——每次只前向一个 token（回顾 u3-l4），单步开销与 prompt 长度关系不大（有 KV cache 兜底），但每步都依赖上一步的结果，无法并行。

用户体感上：TTFT 决定「等多久才看到第一个字」，解码速度（token/s）决定「之后打字机有多快」。两者常常此消彼长，是流式应用最核心的两个指标。

### 2.2 两个时钟：`performance.now()` 与 `Date.now()`

- `performance.now()` 返回高精度单调时钟（毫秒，含小数），适合测代码段耗时。管线内所有计时器都用它。
- `Date.now()` 返回 Unix 时间戳（毫秒，整秒级精度），WebLLM 用它测「请求级」的端到端时间（`e2e_latency_s`）。

### 2.3 平均值与分位数

采样链的耗时波动很大（GPU 调度、GC、首次 shader 编译都会造成长尾），所以除了平均值（avg），实战中还看最小值（min）、最大值（max）和 p99 分位数——p99 表示「99% 的步骤都快于这个值」，是长尾延迟的常用度量。本讲的示例代码就会计算这四个统计量。

### 2.4 `usage.extra` 是什么

OpenAI 的 `CompletionUsage` 只有 `prompt_tokens / completion_tokens / total_tokens` 三个字段。WebLLM 在其上扩展了一个 `extra` 对象装性能指标（u2-l2 已用过它），本讲就是要彻底弄清这个对象里每个数字是怎么算出来的。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
| --- | --- |
| [src/llm_chat.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts) | 管线层：所有计时器的**写入侧**（prefill/decode 计时、采样链分解） |
| [src/engine.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts) | 引擎层：把管线读数组装成 `usage.extra`（**读取与对外口径侧**），以及废弃中的 `runtimeStatsText()` |
| [src/openai_api_protocols/chat_completion.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts) | `CompletionUsage.extra` 的类型定义与每个字段的官方注释（口径的「说明书」） |
| [src/types.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts) | `LatencyBreakdown` 类型定义（六个数组的形状） |
| [src/config.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts) | `GenerationConfig.enable_latency_breakdown`：采样链分解的总开关 |
| [examples/get-started-latency-breakdown/](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started-latency-breakdown/README.md) | 官方性能实验示例，是本讲综合实践的模板 |

一个总的阅读心法：**性能数字的「写入」全部发生在管线层（`llm_chat.ts`），「读出与换算」全部发生在引擎层（`engine.ts`）**。分清这两侧，任何字段的口径都不会搞混。

## 4. 核心概念与源码讲解

### 4.1 prefill/decode 计时器与两级统计账本

#### 4.1.1 概念说明

管线里维护着**两套账本**：

1. **跨轮累计账本**：`prefillTotalTime / prefillTotalTokens / decodingTotalTime / decodingTotalTokens`。语义是「自最近一次重置以来累计」。旧接口 `runtimeStatsText()` 读的就是这一套。
2. **当轮快照账本**：`curRoundPrefillTotalTime / curRoundDecodingTotalTokens` 等（前缀 `curRound`）。语义是「当前这一次请求（或 n 个 choice 中的某一个）的耗时」，每次 `prefillStep()` 开头清零。`usage.extra` 读的是这一套。

之所以要两套，是因为历史演化：最早只有累计账本 + `runtimeStatsText()` 文本输出；OpenAI 兼容 API 落地后，每个请求需要**独立、可归因**的指标，于是加了当轮账本，旧账本逐渐边缘化（4.1.3 会看到它现在只剩一个使用场景）。

#### 4.1.2 核心流程

prefill 计时器的工作流程（`prefillStep` 内）：

```text
prefillStep() 开始
  ├─ (可选) resetRuntimeStats()        # resetStatsPerPrefill 恒为 true
  ├─ tstart = performance.now()        # ← 计时起点
  ├─ 清零 curRound* 账本与 curRoundLatencyBreakdown
  ├─ (可选) 启动 grammar matcher 初始化 Promise（与 prefill 并行）
  ├─ getInputData()                    # tokenizer 编码，发生在计时范围内！
  ├─ 分块循环 embedAndForward()         # 每个 chunk 前向 + 写 KV cache
  ├─ await Promise.all([device.sync(), grammarInitPromise])
  ├─ sampleTokenFromLogits()           # 采样第一个回复 token
  └─ tend = performance.now()          # ← 计时终点
      prefillTotalTime += (tend - tstart) / 1e3
      prefillTotalTokens += promptLen  # 注意：加的是 token 数，不是 chunk 数
```

decode 计时器（`decodeStep` 内）结构完全相同，只是输入永远只有 1 个 token，且 `decodingTotalTokens += 1`。

#### 4.1.3 源码精读

**两套账本的字段定义**。[src/llm_chat.ts:123-142](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L123-L142) 定义了累计账本（`prefillTotalTime` 等 4 个）、当轮账本（`curRound*` 4 个）以及采样链分解 `curRoundLatencyBreakdown` 的初始空对象。注释明确写着累计账本「reset at every resetChat(keepStats=false)」、当轮账本「reset at every prefillStep()」。

**累计账本的重置入口**。[src/llm_chat.ts:517-525](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L517-L525) 的 `resetRuntimeStats()` 把四个累计值清零；它在两处被调用：`resetChat(keepStats=false)` 时（[src/llm_chat.ts:530-540](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L530-L540)），以及——关键细节——**每次 `prefillStep()` 开头**（[src/llm_chat.ts:733-735](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L733-L735)）：

```ts
if (this.resetStatsPerPrefill) {
  this.resetRuntimeStats();
}
```

而 `resetStatsPerPrefill` 是一个初始值为 `true` 且源码中再无其他赋值点的私有字段（[src/llm_chat.ts:105](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L105)）。结论：**走正常 chatCompletion 链路时，累计账本实际上每轮 prefill 都会被清零**，`runtimeStatsText()` 输出的也就是「最近一次 prefill 以来」的速率——这正是它被废弃的根源：语义与「跨轮累计」的名字不符，且新 API 有更精确的替代品。

**prefill 计时落账**。[src/llm_chat.ts:887-899](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L887-L899)：`await Promise.all([this.device.sync(), grammarMatcherInitPromise])` 等待 GPU 真正跑完与 grammar 初始化都完成后，采样第一个 token，然后 `prefillTotalTime += (tend - tstart) / 1e3; prefillTotalTokens += promptLen`，并同时累入 `curRound` 账本。注意两点口径：

- 计时**包含 tokenizer 编码**（`getInputData` 在 `tstart` 之后调用，[src/llm_chat.ts:851](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L851)）、grammar 初始化等待与首个 token 的采样——它是「到第一个 token 可用为止」的全部墙钟。
- `promptLen` 来自 `getInputData()` 返回的 `numPromptTokens`（[src/llm_chat.ts:851](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L851)）。多轮对话时它用 `getPromptArrayLastRound` 只编码**新增部分**（[src/llm_chat.ts:2042-2044](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2042-L2044)），所以第二轮的 `prompt_tokens` 只计增量——这与 u2-l2 的结论互相印证。

**decode 计时落账**。[src/llm_chat.ts:902-936](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L902-L936)：`decodeStep` 同样以 `tstart/tend` 夹住「单 token 前向 + 采样」，然后 `decodingTotalTokens += 1; curRoundDecodingTotalTime += ...`。每生成一个 token 计一次，所以解码速率的口径是：

\[ \text{decode\_tokens\_per\_s} = \frac{N_{\text{decode}}}{T_{\text{decode}}} \]

其中 \(N_{\text{decode}}\) 是 decode 步数（恰好等于 `completion_tokens`，因为第一个 token 是 prefill 采样的、不算 decode）。

**读数接口**。[src/llm_chat.ts:583-665](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L583-L665) 是两套账本的读取函数群：`getCurRoundDecodingTotalTokens()` 等一组 getter、`getCurRoundLatencyBreakdown()`，以及两个文本输出 `runtimeStatsText()`（读累计账本，[src/llm_chat.ts:633-641](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L633-L641)）和 `curRoundRuntimeStatsText()`（读当轮账本，[src/llm_chat.ts:643-651](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L643-L651)）。两者都做「token 数 ÷ 秒数」的除法，输出形如 `prefill: 123.4567 tokens/sec, decoding: 23.4567 tokens/sec`。

**引擎侧的废弃警告**。[src/engine.ts:1315-1324](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1315-L1324) 的 `engine.runtimeStatsText()` 每次调用都会打 WARNING：请改用 `ChatCompletion.usage`（非流式）或 `ChatCompletionChunk.usage`（流式，由 `stream_options` 开启），并声明**唯一预期继续使用它的流程是 `forwardTokensAndSample()`**。

**为什么 `forwardTokensAndSample()` 是例外**。这是给绕过 chatCompletion 的低层调用方用的「裸前向+采样」接口（[src/llm_chat.ts:2137-2186](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2137-L2186)）：它按 `isPrefill` 参数把耗时累进 prefill 或 decode 账本（[src/llm_chat.ts:2171-2184](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2171-L2184)），但**不经过 `prefillStep()`，因此不会触发「每次 prefill 清零」**——累计账本在这条链路上才真正「累计」，`runtimeStatsText()` 也才有稳定语义。引擎通过 [src/engine.ts:1300-1303](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1300-L1303) 的 `forward()` 把它暴露出去。

#### 4.1.4 代码实践

**实践目标**：验证「`e2e_latency_s` 包含排队等待，而 TTFT 与解码时间不含」，从而理解三个字段计时的边界。

**操作步骤**（示例代码，基于 u1-l2 的 get-started 页面改造）：

1. 用 `CreateMLCEngine` 加载一个小模型（如 `Qwen3-0.6B-q0f32-MLC`），`temperature: 0`、`max_tokens: 64` 发一次请求，记录 `usage.extra` 的三个数：`e2e_latency_s`、`time_to_first_token_s`、`time_per_output_token_s`。
2. 计算分解残差：

```ts
// 示例代码
const decodeTotal = extra.time_per_output_token_s * usage.completion_tokens;
const overhead = extra.e2e_latency_s - extra.time_to_first_token_s - decodeTotal;
console.log("排队+校验+响应组装开销(s)：", overhead.toFixed(3));
```

3. 趁第一个请求**还在生成时**立刻发起第二个请求（同一引擎、同一模型，引擎会按模型锁 FCFS 排队，回顾 u7-l2），再算一次残差，对比两轮的 `overhead`。

**需要观察的现象**：第二轮的 `e2e_latency_s` 明显大于「TTFT + 解码总时长」，残差显著变大；而 `time_to_first_token_s` 本身不受排队影响（它的计时起点在拿到锁之后）。

**预期结果**：`timeReceived` 在 `chatCompletion` 入口处、**抢锁之前**记录（[src/engine.ts:799](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L799)，锁在 [src/engine.ts:827-829](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L827-L829) 才 acquire），所以排队时间只进 `e2e_latency_s`，不进 TTFT。具体数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 `runtimeStatsText()` 的输出在正常 chatCompletion 链路下等价于「最近一次请求」的速率？

**答案**：因为 `resetStatsPerPrefill` 恒为 `true`（[src/llm_chat.ts:105](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L105)），每次 `prefillStep()` 开头都会调 `resetRuntimeStats()`（[src/llm_chat.ts:733-735](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L733-L735)）清空累计账本，所以读到的永远是「自最近一次 prefill 起」的累计，即最近一次请求的数字。

**练习 2**：`decodingTotalTokens` 与 `completion_tokens` 是什么关系？为什么恰好相等？

**答案**：`decodingTotalTokens` 在每个 `decodeStep` 末尾 `+= 1`（[src/llm_chat.ts:930-933](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L930-L933)），而第一个回复 token 是 prefill 阶段采样的、不经过 decodeStep；引擎在组装 usage 时取的 `completion_tokens = getCurRoundDecodingTotalTokens()`（[src/engine.ts:909](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L909)），二者同源，天然相等。

### 4.2 usage.extra：TTFT 与 token/s 的对外口径

#### 4.2.1 概念说明

管线层的计时器是「私有账本」，用户拿到的是引擎层组装进 `usage.extra` 的换算结果。这一层做了三件事：

1. **聚合**：非流式请求若 `n > 1`，会对每个 choice 各跑一遍 prefill+decode 循环（u2-l2），各 choice 的读数先累加。
2. **换算**：把「token 数与秒数」换算成用户关心的速率与延迟。
3. **门控**：`latencyBreakdown` 只有请求显式带 `extra_body.enable_latency_breakdown: true` 才附上（4.3 详述）。

#### 4.2.2 核心流程

非流式 chatCompletion 的统计组装流程：

```text
chatCompletion(request)
  ├─ timeReceived = Date.now()            # 端到端时钟起点（抢锁前）
  ├─ 组装 GenerationConfig（含 enable_latency_breakdown）
  ├─ await lock.acquire()                 # 同模型 FCFS 排队
  └─ for i in 0..n-1:                     # 每个 choice 一次
       _generate() = prefillStep() + while(!stopped) decodeStep()
       completion_tokens += 当轮 decode token 数
       prompt_tokens     += 当轮 prefill token 数
       prefill_time      += 当轮 prefill 秒数
       decode_time       += 当轮 decode 秒数
  └─ usage.extra = {
       e2e_latency_s:          (Date.now() - timeReceived) / 1000
       prefill_tokens_per_s:   prompt_tokens / prefill_time
       decode_tokens_per_s:    completion_tokens / decode_time
       time_to_first_token_s:  prefill_time        # ← TTFT 就是 prefill 耗时
       time_per_output_token_s: decode_time / completion_tokens
     }
```

#### 4.2.3 源码精读

**字段口径的「官方说明书」**。[src/openai_api_protocols/chat_completion.ts:959-1027](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L959-L1027) 定义 `CompletionUsage`，`extra` 中每个字段都有注释。值得逐条记住：

| 字段 | 注释口径 |
| --- | --- |
| `e2e_latency_s` | 从收到请求到生成响应的总秒数 |
| `prefill_tokens_per_s` | prefill 的 token/s |
| `decode_tokens_per_s` | 自回归解码的 token/s |
| `time_to_first_token_s` | 生成第一个 token 的秒数，**主要是 prefill 开销**；`n > 1` 时是所有 choice 的**总和** |
| `time_per_output_token_s` | 相邻生成 token 之间的秒数，**主要是解码开销**；`n > 1` 时是所有 choice 的**平均** |
| `grammar_init_s` / `grammar_per_token_s` | 结构化输出的 grammar 开销（和 / 每 token 平均） |

**TTFT ≈ prefill 耗时的代码依据**。[src/engine.ts:925-934](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L925-L934) 组装 `defaultExtra`：`time_to_first_token_s: prefill_time` 直接把当轮 prefill 秒数当作 TTFT，`time_per_output_token_s: decode_time / completion_tokens` 是解码平均单步。这就是 u2-l2 说过「首 token 延迟约等于 prefill 耗时」的出处。

**n 个 choice 的累加循环**。[src/engine.ts:843-916](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L843-L916)：外层 `for (let i = 0; i < n; i++)` 每个 choice 调一次 `_generate()`，然后 `completion_tokens += ...; prefill_time += ...; decode_time += ...`（[src/engine.ts:909-915](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L909-L915)）。所以 `n=2` 时 `time_to_first_token_s` 约为单 choice 的两倍（sum），`time_per_output_token_s` 不变（平均）。

**逐 token 驱动循环在哪**。TTFT 与解码期的分界就是 [src/engine.ts:457-479](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L457-L479) 的 `_generate()`：先 `await this.prefill(...)`（内部经 [src/engine.ts:1366-1424](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1366-L1424) 的包装落到 `pipeline.prefillStep`），然后 `while (!pipeline.stopped())` 循环调 `this.decode(...)`（[src/engine.ts:1429-1431](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1429-L1431)，直通 `pipeline.decodeStep`）。prefill 计时器覆盖前者，decode 计时器覆盖后者。

**流式路径的 usage chunk**。[src/engine.ts:704-744](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L704-L744)：流式请求只有设置了 `stream_options.include_usage` 才会在流的末尾追加一个 `choices: []` 的 usage chunk（[src/engine.ts:745-754](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L745-L754)）。字段与非流式一致，但直接取管线 getter（如 `pipeline.getCurRoundPrefillTokensPerSec()`，[src/engine.ts:711-715](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L711-L715)）而非自己累加——流式限定 `n=1`，无需聚合。

**completion 接口的同款口径**。文本补全路径 [src/engine.ts:1062-1092](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1062-L1092) 用完全相同的公式组装 `extra`，说明 TTFT/token/s 口径在两类接口上是统一的。

**grammar 字段的条件**。[src/engine.ts:917-920](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L917-L920) 的 `usedGrammar` 只认 `response_format.type` 为 `grammar` 或 `json_object` 两种——`structural_tag` 不在此列，因此走 structural tag 时的 `usage.extra` 里**没有** `grammar_init_s / grammar_per_token_s`（这与 u6-l4 指出的统计遗漏一致）。

#### 4.2.4 代码实践

**实践目标**：用 `n=1` 与 `n=2` 的对照实验验证「TTFT 是 sum、每 token 延迟是 average」的注释口径。

**操作步骤**（示例代码）：

```ts
// 示例代码：n 的聚合口径实验
for (const n of [1, 2]) {
  const r = await engine.chat.completions.create({
    messages: [{ role: "user", content: "用一句话介绍杭州" }],
    n,
    temperature: 0,
    max_tokens: 48,
  });
  console.log(n, {
    ttft: r.usage.extra.time_to_first_token_s,
    perToken: r.usage.extra.time_per_output_token_s,
    promptTokens: r.usage.prompt_tokens,
  });
}
```

**需要观察的现象**：`n=2` 时 `ttft` 大约是 `n=1` 的两倍（两次 prefill 串行相加），`perToken` 与 `n=1` 基本同量级；`prompt_tokens` 也是两倍（每个 choice 都完整 prefill 一遍）。

**预期结果**：符合上述比例关系。若偏差较大，先排查是否第二轮命中了 KV cache 复用（保持每次实验用全新会话或先 `resetChat()`）。具体倍数关系**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`prefill_tokens_per_s` 的分子分母分别是什么？为什么它通常比 `decode_tokens_per_s` 大一个数量级？

**答案**：分子是当轮 prefill 的 token 数（`prompt_tokens`，多轮对话只计增量），分母是当轮 prefill 秒数（[src/engine.ts:927](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L927)）。prefill 是批量并行前向（一次送成百上千个 token，矩阵乘大、GPU 利用率高），decode 每次只前向 1 个 token（算子小、launch 开销占比高），所以前者的 token/s 天然高得多。

**练习 2**：流式请求怎样才能拿到 `usage.extra`？

**答案**：请求里设置 `stream_options: { include_usage: true }`，流的最后一个 chunk（`choices` 为空数组）会携带 usage（[src/engine.ts:704-766](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L704-L766)）；不设置则流里没有任何性能数据。

**练习 3**：多轮对话第二轮的 `time_to_first_token_s` 为什么可能远小于第一轮？

**答案**：引擎检测到是同一会话的延续时复用 KV cache，prefill 只需前向**新增**的 token（`getPromptArrayLastRound` 只编码增量，[src/llm_chat.ts:2042-2044](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2042-L2044)），prefill 耗时与 TTFT 随之大幅下降。

### 4.3 curRoundLatencyBreakdown：采样链逐 token 分解

#### 4.3.1 概念说明

TTFT 和 token/s 告诉你「整体多快」，但当你发现解码速度上不去时，还需要知道**每一个 token 的时间花在了哪一步**。`curRoundLatencyBreakdown` 就是这个显微镜：它把 `sampleTokenFromLogits`（u3-l5 讲过的采样链）拆成六段，每采样一个 token 就往对应数组 push 一个秒数。

六 个数组（类型定义见 [src/types.ts:255-262](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L255-L262)）：

| 字段 | 计时范围 | 何时才会有数据 |
| --- | --- | --- |
| `grammarBitmaskTime` | 构建 grammar 位掩码 + 上传 GPU + 就地应用掩码 | 仅 `response_format` 为 json_object/grammar/structural_tag |
| `logitProcessorTime` | 用户注册的 `LogitProcessor.processLogits`（CPU）+ logits GPU↔CPU 往返 | 仅注册了 LogitProcessor |
| `logitBiasTime` | `logit_bias` 的打包上传与 GPU 应用 | 仅请求带 `logit_bias` |
| `penaltyTime` | 频率/出现/重复惩罚的打包上传与 GPU 应用 | 仅设置了惩罚且本轮已出现过 token |
| `sampleTime` | softmax + argsort + top-p 抽样（GPU） | 总是有 |
| `totalTime` | `sampleTokenFromLogits` 全程（上述各段之和 + 杂项） | 总是有 |

两个最重要的口径提醒：

1. **它不含模型前向**。`totalTime` 夹的是采样函数的头尾（[src/llm_chat.ts:1700](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1700) 与 [src/llm_chat.ts:1982-1986](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1982-L1986)），而 `embedAndForward`（真正的 transformer 前向）在它之前、计时范围之外。因此 `time_per_output_token_s - totalTime 的均值 ≈ 单步前向 + 同步开销`，这个差值是评估「采样开销 vs 模型本体开销」的关键。
2. **数组是条件填充的**。没开对应功能，数组就是空的——分析前先检查长度，别把空数组的 `undefined` 统计当成 0。

#### 4.3.2 核心流程

每采样一个 token（prefill 的首个 token 与 decode 的每个 token 都算一次），采样函数内部的时间线：

```text
sampleTokenFromLogits(logits)
  t0 = now ──────────────────────────────── totalTime 的计时范围 ──────────┐
  ├─ [0] grammar 掩码: getNextTokenBitmask → copyFrom → applyBitmask   # grammarBitmaskTime
  ├─ [1] LogitProcessor: logits 下沉 CPU → processLogits → 回写       # logitProcessorTime
  ├─ [2] logit_bias: 打包 → 上传 → fapplyLogitBias                     # logitBiasTime
  ├─ [3] 惩罚: 打包出现频率 → 上传 → fapplyPenalty                      # penaltyTime
  ├─ [4] 抽样: softmaxWithTemperature → argsort → sampleWithTopP       # sampleTime
  ├─ [5] LogitProcessor.processSampledToken（不单独计时）
  └─ [6] grammar.acceptToken（计入 grammar_per_token_s，不计入六数组）
  t1 = now ───────────────────────────────────────────────────────────────┘
  totalTime.push(t1 - t0)
```

#### 4.3.3 源码精读

**类型与总开关**。`LatencyBreakdown` 六个数组的形状在 [src/types.ts:255-262](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L255-L262)；请求侧开关是 `extra_body.enable_latency_breakdown`（协议定义 [src/openai_api_protocols/chat_completion.ts:279-286](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L279-L286)，注释说明「为 true 时响应包含各采样阶段的耗时分解」），它被摘入 `GenerationConfig`（[src/config.ts:162-165](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L162-L165)），引擎组装 genConfig 时原样透传（[src/engine.ts:820-825](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L820-L825)）。

**每次 prefill 前清零**。[src/llm_chat.ts:751-758](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L751-L758) 在 `prefillStep` 里把六个数组全部换成空数组——breakdown 是严格的「当轮」语义。

**六段计时的写入点**（全部有 `if (genConfig?.enable_latency_breakdown)` 守卫，即不开开关就完全不计时，连 push 都不发生）：

- grammar 掩码段：[src/llm_chat.ts:1706-1748](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1706-L1748)，`grammarBitmaskBegin` 在段首取，段尾 push（[src/llm_chat.ts:1740-1747](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1740-L1747)）。注意 `getNextTokenBitmask` 的耗时同时被计入 `curRoundGrammarPerTokenTotalTime`（[src/llm_chat.ts:1715-1719](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1715-L1719)），即 grammar 的开销有两个视角可看。
- LogitProcessor 段：[src/llm_chat.ts:1750-1778](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1750-L1778)，含 logits 下沉 CPU 的 `device.sync()` 等待（那段 `await` 在计时起点之前，[src/llm_chat.ts:1752-1758](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1752-L1758)），push 在 [src/llm_chat.ts:1770-1777](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1770-L1777)。
- logit_bias 段：[src/llm_chat.ts:1781-1824](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1781-L1824)。
- 惩罚段：[src/llm_chat.ts:1826-1890](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1826-L1890)，注意外层条件——只有设置了惩罚**且** `appearedTokensFreq` 非空（本轮已生成过 token）才会进入，所以 `penaltyTime` 的第一个元素对应的是**第二个被采样的 token**。
- 抽样段：`sampleBegin` 在 [src/llm_chat.ts:1895](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1895)，push 在 [src/llm_chat.ts:1959-1963](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1959-L1963)。
- 总时间：`outputTokenBegin` 在函数开头 [src/llm_chat.ts:1700](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1700)，push 在返回前 [src/llm_chat.ts:1982-1986](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1982-L1986)。

**挂载到响应**。[src/engine.ts:922-933](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L922-L933)：引擎取出 `pipeline.getCurRoundLatencyBreakdown()`，只有 `request.extra_body?.enable_latency_breakdown` 为真时才把它放进 `defaultExtra.latencyBreakdown`，否则是 `undefined`。流式（[src/engine.ts:718-729](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L718-L729)）与 completion（[src/engine.ts:1068-1089](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1068-L1089)）同款门控。

**官方示例怎么消费它**。[examples/get-started-latency-breakdown/src/get_started_latency_breakdown.ts:19-41](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started-latency-breakdown/src/get_started_latency_breakdown.ts#L19-L41) 的 `computeStats()` 对每个数组算 avg/min/max/p99（p99 取 `sorted[Math.floor(0.99 * (len - 1))]`）；主循环跑 `numTrials = 20` 轮（[src/get_started_latency_breakdown.ts:77-122](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started-latency-breakdown/src/get_started_latency_breakdown.ts#L77-L122)），逐轮收集 `usage.extra` 的 `decode_tokens_per_s / e2e_latency_s / time_per_output_token_s / completion_tokens`，并把 breakdown 的六个数组摊平进跨轮累计数组。

**一个需要留意的示例现状**：示例的请求体（[src/get_started_latency_breakdown.ts:80-100](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started-latency-breakdown/src/get_started_latency_breakdown.ts#L80-L100)）**并没有传 `extra_body: { enable_latency_breakdown: true }`**。对照引擎侧的门控（[src/engine.ts:931-933](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L931-L933)），`usage.extra.latencyBreakdown` 应为 `undefined`，示例里 `...(logitProcessorTime || [])` 会全部落到空数组，最终 `computeStats` 输出空对象。因此要在示例里真正看到分解数据，需要自己补上这个开关——这正是下面实践的任务（行为推断基于源码，**待本地验证**）。

#### 4.3.4 代码实践

**实践目标**：让示例真正产出采样链分解数据，并验证「数组长度 = 采样次数」的推断。

**操作步骤**：

1. 进入 `examples/get-started-latency-breakdown/`，`npm install && npm start`，先原样跑一次，观察控制台 `Latency stats:` 是否为空对象 `{}`。
2. 修改请求（示例代码）：

```ts
// 示例代码：在 create() 的参数里追加
const reply0 = await engine.chat.completions.create({
  messages: [{ role: "user", content: "List twenty US states." }],
  n: 1,
  temperature: 0,
  max_tokens: 128,
  frequency_penalty: 1.2,   // 保留示例原参数，确保 penaltyTime 有数据
  presence_penalty: 1.0,
  // ↓ 关键新增：打开采样链分解开关
  extra_body: { enable_latency_breakdown: true },
} as any);
```

3. 重新运行，在控制台检查 `latencyStats` 现在是否包含 `sampleTime`、`totalTime`、`penaltyTime` 的 avg/min/max/p99；`logitProcessorTime`、`grammarBitmaskTime` 是否仍为空（没注册 processor、没用 response_format，理应为空）。
4. 打印数组长度与 token 数的关系（示例代码）：

```ts
// 示例代码
const lb = reply0.usage?.extra.latencyBreakdown;
console.log("totalTime.length =", lb?.totalTime.length);
console.log("completion_tokens + 1 =", reply0.usage.completion_tokens + 1);
```

**需要观察的现象**：第 1 步 `Latency stats` 为空；第 3 步只有条件开启的段有统计；第 4 步两个数字相等。

**预期结果**：`totalTime.length === completion_tokens + 1`。依据：`sampleTokenFromLogits` 在 `prefillStep`（采第一个 token，[src/llm_chat.ts:890](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L890)）与每个 `decodeStep`（[src/llm_chat.ts:926](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L926)）各执行一次且每次 push 一个元素，而 `completion_tokens` 只数 decode 步。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么说 `totalTime` 的均值不能当作「每个 token 的生成耗时」？

**答案**：`totalTime` 只覆盖 `sampleTokenFromLogits`（采样链），不含 `embedAndForward` 的 transformer 前向（后者在 decodeStep 中位于采样之前、计时范围之外）。真正的单 token 耗时是 `time_per_output_token_s`；两者之差约等于单步前向 + 同步开销。

**练习 2**：请求带 `frequency_penalty: 1.2` 但 `penaltyTime` 数组仍可能比 `totalTime` 短一个元素，为什么？

**答案**：惩罚段的执行条件是「设置了惩罚**且** `appearedTokensFreq` 非空」（[src/llm_chat.ts:1827-1837](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1827-L1837)），而 `appearedTokensFreq` 在每次 prefillStep 开头清空（[src/llm_chat.ts:740-742](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L740-L742)）。prefill 采样的第一个 token 时表还是空的，跳过惩罚段，所以 `penaltyTime.length = totalTime.length - 1`（n=1 时）。

**练习 3**：如果不改示例、只看 `decode_tokens_per_s`，你能否判断「解码慢」是模型前向慢还是采样链慢？

**答案**：不能。`decode_tokens_per_s` 是总口径。需要打开 `enable_latency_breakdown`，用 `time_per_output_token_s`（总单步）减去 `totalTime` 均值（采样链单步）得到前向单步耗时，再对比两者占比：若采样链占比高，考虑去掉 logit_bias/惩罚或 LogitProcessor；若前向占比高，则是模型规模与 GPU 算力的瓶颈。

## 5. 综合实践

**任务：定量刻画「长 prompt 为什么拖慢首字出现」。**

按照 [examples/get-started-latency-breakdown/README.md](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started-latency-breakdown/README.md) 的步骤搭好环境（`npm install && npm start`，需支持 WebGPU 的浏览器），然后改造其主循环为如下实验（示例代码）：

```ts
// 示例代码：prompt 长度扫描实验
const engine = await webllm.CreateMLCEngine("Qwen3-0.6B-q0f32-MLC", {
  logLevel: "INFO",
});

// 1. 准备三档长度的 prompt（英文按 ~1 token/词 估算，先粗后细）
const mkPrompt = (nWords: number) =>
  `Summarize the following passage in one sentence.\n\n` +
  Array.from({ length: nWords }, (_, i) => `word${i}`).join(" ");

const results: any[] = [];
for (const nWords of [30, 120, 1000]) {
  // 每档跑 3 轮取均值，降低波动
  for (let trial = 0; trial < 3; trial++) {
    const r = await engine.chat.completions.create({
      messages: [{ role: "user", content: mkPrompt(nWords) }],
      temperature: 0,
      max_tokens: 64,                     // 固定生成长度，让 decode 项可比
      extra_body: { enable_latency_breakdown: true },
    } as any);
    const e = r.usage.extra;
    results.push({
      nWords,
      prompt_tokens: r.usage.prompt_tokens,   // 实际 token 数，用来分档
      ttft_s: e.time_to_first_token_s,
      prefill_tps: e.prefill_tokens_per_s,
      decode_tps: e.decode_tokens_per_s,
      per_token_s: e.time_per_output_token_s,
      e2e_s: e.e2e_latency_s,
    });
  }
}
console.table(results);
```

**实验要求与检查清单**：

1. **记录 TTFT 曲线**：以 `prompt_tokens` 为横轴、`time_to_first_token_s` 为纵轴，三档各取均值，画出趋势（手绘或电子表格均可）。
2. **验证统计口径**：任选一轮，检查 `prompt_tokens` 是否落在对应档位（约 32/128/1024 上下，含对话模板的额外 token——模板本身也要占几十个 token，属于口径的一部分）；再检查该轮 `time_to_first_token_s` 是否约等于 `e2e_latency_s - 64 × time_per_output_token_s`（生成长度固定为 64，端到端 ≈ TTFT + 解码总量 + 少量组装开销）。
3. **对照解码速度**：确认三档的 `decode_tokens_per_s` 大致持平（decode 有 KV cache，单步开销对 prompt 长度不敏感；若长档明显变慢，注意是否已接近 `context_window_size` 触发了额外行为）。
4. **用分解数据定位**：打开 `enable_latency_breakdown` 后，比较三档 `totalTime` 均值是否基本不变——它只覆盖采样链，与 prompt 长度无关；变长的只是前向部分，即 `time_to_first_token_s` 里除去编码与采样的主体。
5. **写结论**：用一段话回答「长 prompt 为什么拖慢首字出现」。参考要点：prefill 的计算量与 prompt token 数成正比（注意力与逐层投影都随序列长度线性乃至二次增长）；TTFT 的口径（[src/engine.ts:925-930](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L925-L930)）就是当轮 prefill 耗时，且包含 tokenizer 编码（[src/llm_chat.ts:737](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L737) 起计时）；而 decode 借助 KV cache 每步只算一个 token，速度基本不受 prompt 长度影响。

具体数值依赖本机 GPU，**待本地验证**；重点是趋势与口径自洽，而非绝对数。

## 6. 本讲小结

- WebLLM 的性能数字**写入在管线层**（`llm_chat.ts` 的 `performance.now()` 计时器）、**读出换算在引擎层**（`engine.ts` 组装 `usage.extra`）；`CompletionUsage.extra` 的注释（[chat_completion.ts:959-1027](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L959-L1027)）是口径的权威说明书。
- 统计分**两级账本**：跨轮累计（`prefillTotalTime` 等，`runtimeStatsText()` 读它，且因 `resetStatsPerPrefill` 恒真、每次 prefill 即清零）与当轮快照（`curRound*`，每次 `prefillStep()` 清零，`usage.extra` 读它）。
- **TTFT 的口径就是当轮 prefill 耗时**（`time_to_first_token_s = prefill_time`），包含 tokenizer 编码、分块前向、grammar 初始化等待与首个 token 采样；`e2e_latency_s` 用 `Date.now()` 从抢锁前计起，多出的部分是排队与响应组装。
- `n > 1` 时 TTFT 与 grammar 初始化时间是各 choice 的**和**，每 token 延迟是**平均**；`completion_tokens` 恰好等于 decode 步数（首个 token 由 prefill 采样）。
- `curRoundLatencyBreakdown` 六个数组是**采样链**的逐 token 显微镜，不含模型前向；数组按功能条件填充，需要请求显式带 `extra_body.enable_latency_breakdown: true`（官方示例没带，需自行补上）。
- `runtimeStatsText()` 已废弃；`usage.extra`（非流式）与 `stream_options.include_usage` 触发的 usage chunk（流式）是现在的标准观测面，`forwardTokensAndSample()` 低层链路是旧接口仅存的适用场景。

## 7. 下一步学习建议

- **u7-l4（测试体系）**：本讲的所有口径断言（如 `totalTime.length === completion_tokens + 1`）都可以沉淀为测试；读 `tests/` 时特别留意断言里对 `usage.extra` 字段的使用方式，学会从测试反推口径。
- **延伸阅读源码**：`src/llm_chat.ts` 中 grammar 统计的另一视角（`curRoundGrammarInitTotalTime` / `curRoundGrammarPerTokenTotalTime`，[src/llm_chat.ts:169-173](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L169-L173) 与 [src/llm_chat.ts:611-624](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L611-L624)），把 u6-l3 的结构化输出开销与本讲的观测面对应起来。
- **动手方向**：把综合实践升级为「prefill_chunk_size 对 TTFT 的影响」实验（结合 u3-l3 的分块机制，注意该值读自 wasm metadata、不能经 appConfig 修改，需换不同 model_lib 才能对比），或给自己的应用接一个实时性能面板，用 `enable_latency_breakdown` 的 p99 监控采样链长尾。
