# 性能观测：延迟分解与运行统计

## 1. 本讲目标

学完本讲，你应该能够：

1. 区分「首 token 延迟（TTFT）」与「逐 token 解码延迟（TPOT）」这两种性能指标各自的构成。
2. 读懂 `usage.extra` 中 `time_to_first_token_s`、`prefill_tokens_per_s`、`decode_tokens_per_s`、`e2e_latency_s` 等字段的统计口径（分子是什么、分母是什么、从何时计到何时）。
3. 理解 `curRoundLatencyBreakdown` 六个数组分别统计采样链的哪一步，以及它**不包含**模型前向时间这一关键口径。
4. 了解遗留接口 `runtimeStatsText()` 的口径、它的废弃计划，以及低层 API `forwardTokensAndSample()` 为什么仍是它的预期使用场景。

## 2. 前置知识

### 2.1 TTFT 与 TPOT：两个互补的延迟指标

评价一个 LLM 推理系统的用户体验，业界常用两个指标：

- **TTFT（Time To First Token，首 token 延迟）**：从发出请求到屏幕上出现第一个字的时间。它决定了用户点下「发送」后要盯着空白多久。
- **TPOT（Time Per Output Token，逐 token 延迟）**：之后每生成一个 token 的平均耗时，对应 `decode_tokens_per_s`（解码速度）的倒数。它决定了「打字机效果」的快慢。

在 WebLLM 的两阶段生成模型（第 3 单元已学）下，两者的来源天然不同：

\[ \text{TTFT} \approx \underbrace{\text{排队/校验}}_{\text{引擎层}} + \underbrace{T_{\text{prefill}}}_{\text{整段 prompt 一次（或分块）前向}} + \underbrace{T_{\text{首个 token 采样}}}_{\text{一次采样链}} \]

\[ \text{TPOT} \approx \underbrace{T_{\text{单 token 前向}}}_{\text{借 KV cache 免于重算历史}} + \underbrace{T_{\text{采样链}}}_{\text{每 token 一次}} \]

prefill 要对全部 \( L \) 个 prompt token 做前向，计算量约 \( 2 N_{\text{params}} L \) FLOPs，随 prompt 长度**线性增长**；decode 每步只前向 1 个 token。这就是「长 prompt 拖慢首字出现」的根本原因，本讲综合实践中你会用真实数据验证它。

### 2.2 performance.now() 与 Date.now()

管线内的计时器全部使用 `performance.now()`：它返回毫秒级浮点高精度时间戳，适合测量几百微秒到几秒的计算段；引擎层测量请求的端到端耗时用 `Date.now()`（毫秒整数）就够了。源码里所有 `(tend - tstart) / 1e3` 就是把毫秒换算成秒——`usage.extra` 中所有 `*_s` 后缀字段的单位都是**秒**。

### 2.3 你应该已经知道的事

本讲依赖第 3 单元的结论：一次请求 = 一次 `prefillStep()` + N 次 `decodeStep()`；采样链的固定顺序是 grammar 掩码 → LogitProcessor → logit_bias → 惩罚 → softmax → top-p 抽样（u3-l5）；`usage.extra` 是 WebLLM 对 OpenAI `CompletionUsage` 的私有扩展（u2-l2）。本讲回答的问题是：**这些数字到底是在哪里、用什么口径掐表算出来的。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/llm_chat.ts` | 管线层：全部计时器的埋点位置，跨轮/本轮统计字段，`runtimeStatsText()` |
| `src/engine.ts` | 引擎层：`e2e_latency_s` 的起表点，`usage.extra` 的组装公式，`forwardTokensAndSample()` 与 `runtimeStatsText()` 的转发 |
| `src/openai_api_protocols/chat_completion.ts` | 协议层：`extra_body.enable_latency_breakdown` 请求开关，`usage.extra` 各字段的官方注释 |
| `src/types.ts` | `LatencyBreakdown` 类型定义 |
| `examples/get-started-latency-breakdown/` | 官方基准示例：20 次试验 + avg/min/max/p99 统计 |

## 4. 核心概念与源码讲解

### 4.1 两层计时器：跨轮统计与本轮（curRound）统计

#### 4.1.1 概念说明

管线内维护着**两套同构的累加器**：

- **跨轮统计**（`prefillTotalTime`、`prefillTotalTokens`、`decodingTotalTime`、`decodingTotalTokens`）：字面意思是「会话累计」，服务于遗留接口 `runtimeStatsText()`。
- **本轮统计**（`curRound` 前缀的四个字段）：在**每次 `prefillStep()` 开头清零**，即「本次请求」的口径，服务于 `usage.extra`。

理解这两套字段的关键是一个隐藏事实：跨轮统计实际上**也是每次 prefill 就清零**——`resetStatsPerPrefill` 是一个恒为 `true` 的私有字段（全仓库没有任何地方修改它），所以每次新请求的 prefill 开始时跨轮统计也被重置。这解释了 `runtimeStatsText()` 为什么被标记废弃：`curRound` 系列 + `usage.extra` 已经完整覆盖了它的信息量。

#### 4.1.2 核心流程

```text
prefillStep() 开始
  ├─ resetStatsPerPrefill 为 true → resetRuntimeStats()   # 跨轮四字段清零
  ├─ tstart = performance.now()
  ├─ curRound* 五个字段 + curRoundLatencyBreakdown 重建   # 本轮口径清零
  ├─ （grammar matcher 初始化与 prefill 并行，单独计时）
  ├─ 分块前向全部 chunk，device.sync() 后采样第一个 token
  └─ tend：prefill 计时落表
       prefillTotalTokens += promptLen          # 分子是 prompt 的 token 总数
       prefillTotalTime  += (tend - tstart)/1e3 # 分母含编码+前向+首token采样

decodeStep()（每个生成 token 调一次）
  ├─ tstart = performance.now()
  ├─ 单 token 前向（embedAndForward）+ 采样链（sampleTokenFromLogits）
  └─ tend：decodingTotalTime += (tend-tstart)/1e3；decodingTotalTokens += 1
```

于是：

\[ \text{prefill\_tokens\_per\_s} = \frac{\text{promptLen}}{T_{\text{prefill}}} \qquad \text{decode\_tokens\_per\_s} = \frac{\text{生成 token 数}}{T_{\text{decode 总和}}} \]

注意两个容易误读的口径细节：

1. prefill 的分母**包含第一个 token 的采样时间**（`tend` 在采样之后才取）；
2. decode 的每步时长**包含采样链**——`decodeStep` 的表覆盖「前向 + 采样」全程，不只是模型前向。

#### 4.1.3 源码精读

统计字段的声明集中在一起，注释写明了各自的重置时机：

- [src/llm_chat.ts:123-142](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L123-L142) —— 前四行是跨轮统计（"reset at every `resetChat(keepstats=false)`"），`curRound*` 四个字段"reset at every `prefillStep()`"，最后的 `curRoundLatencyBreakdown` 是公开的采样阶段细分（4.3 节专讲）。

`resetStatsPerPrefill` 恒为 true，且 `prefillStep` 开头据此清零跨轮统计：

- [src/llm_chat.ts:105](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L105) —— `private resetStatsPerPrefill = true;`，全仓库无其他赋值点。
- [src/llm_chat.ts:722-737](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L722-L737) —— `prefillStep` 入口：先按 `resetStatsPerPrefill` 重置跨轮统计，再取 `tstart`，随后清空本轮状态。
- [src/llm_chat.ts:520-525](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L520-L525) —— `resetRuntimeStats()` 的实现，只清四个跨轮字段。
- [src/llm_chat.ts:744-758](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L744-L758) —— `curRound*` 字段与 `curRoundLatencyBreakdown` 六个空数组的重建。

prefill 的计时落表点——注意 `tend` 在采样之后、分子是 `promptLen`：

- [src/llm_chat.ts:887-897](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L887-L897) —— 等待 `device.sync()` 与 grammar 初始化完成后采样第一个 token，然后 `prefillTotalTokens += promptLen`、`curRoundPrefillTotalTime += (tend - tstart) / 1e3`。

decodeStep 的计时落表点——每步 `+1` 个 token，时长含前向与采样：

- [src/llm_chat.ts:902-932](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L902-L932) —— `tstart` 在函数第一行（L907），前向（L915-917）与采样（L926）之后 `decodingTotalTime += (tend - tstart) / 1e3`、`decodingTotalTokens += 1`（L930-932）。

对外读取这些字段的 getter 群：

- [src/llm_chat.ts:583-665](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L583-L665) —— `getCurRoundPrefillTotalTokens()`、`getCurRoundDecodingTotalTime()` 等一组 getter，以及 `getCurRoundPrefillTokensPerSec()` / `getCurRoundDecodingTokensPerSec()` 两个除法封装（L656-665），引擎层组装 `usage.extra` 时逐个调用它们。

#### 4.1.4 代码实践

**实践目标**：亲手验证「跨轮统计每次 prefill 都会清零」，从而理解 `runtimeStatsText()` 与本轮统计的关系。

**操作步骤**：

1. 任意可运行的示例（如 `examples/get-started`）中加载一个小模型（如 `Qwen3-0.6B-q0f32-MLC`）。
2. 在控制台执行（示例代码）：

```ts
// 示例代码：观察跨轮统计在两次请求间的变化
const r1 = await engine.chatCompletion({
  messages: [{ role: "user", content: "你好，介绍一下你自己" }],
  stream: false,
});
console.log("第 1 轮:", await engine.runtimeStatsText());

const r2 = await engine.chatCompletion({
  messages: [
    { role: "user", content: "你好，介绍一下你自己" },
    { role: "assistant", content: r2_choices_content /* 第 1 轮回复 */ },
    { role: "user", content: "再详细一点" },
  ],
  stream: false,
});
console.log("第 2 轮:", await engine.runtimeStatsText());
console.log("第 2 轮 usage:", r2.usage.extra);
```

3. 对比两次 `runtimeStatsText()` 的 prefill 速度：第 2 轮因多轮 KV cache 复用只 prefill 增量部分（u2-l2 已学），prefill token 数骤减。

**需要观察的现象**：第 2 轮的 `runtimeStatsText` 与第 1 轮互不影响——两次输出各自独立，说明跨轮统计并未真正「跨轮」累积。

**预期结果**：`runtimeStatsText()` 的输出反映的是**最近一次 prefill 起**的统计；每轮数字独立。若第 2 轮命中 KV cache 复用，prefill tokens/sec 会因分子（增量 token 数）变小而数值波动。**待本地验证**（需要支持 WebGPU 的浏览器环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 `runtimeStatsText()` 的「跨会话累计」语义名存实亡？

**答案**：因为它依赖的四个跨轮字段在每次 `prefillStep()` 开头就被 `resetRuntimeStats()` 清零（`resetStatsPerPrefill` 恒为 true，见 [src/llm_chat.ts:733-735](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L733-L735)），所以它实际只能报告「最近一次 prefill 以来的解码速度」，与 `curRound` 系列口径重合，信息上被 `usage.extra` 完全覆盖，故被官方标记废弃。

**练习 2**：`decode_tokens_per_s` 的分母包含采样链耗时吗？

**答案**：包含。`decodeStep` 的计时从函数入口到采样完成（[src/llm_chat.ts:907-932](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L907-L932)），因此解码速度是「前向 + 采样」的整体吞吐。这也意味着开启 grammar 约束（掩码在采样链内）会直接拉低该数字。

### 4.2 usage.extra：引擎层的延迟分解口径

#### 4.2.1 概念说明

管线层只产出「原始计时」，引擎层负责把它们组装成用户可见的 `usage.extra`。这一层新增了管线层没有的两个量：

- `e2e_latency_s`：引擎收到请求（`Date.now()`）到组装响应的端到端时长，**含字段校验、模型锁排队**等引擎层开销；
- `time_to_first_token_s`：直接取 `prefill_time`（即本轮 prefill 耗时）——字段注释写明 "Mainly contains prefilling overhead"，它**不含**引擎层排队时间，所以严格说不等于用户感知的 TTFT，而是其主体部分。

`n > 1`（多候选）时各 choice 串行执行一遍 prefill+decode，引擎层把各 choice 的 token 数与耗时**分别求和**后再做除法，得到的是加权平均口径。

#### 4.2.2 核心流程

非流式路径的组装公式：

```text
timeReceived = Date.now()                     # 引擎层起表
对每个 choice i（n 个）：
  completion_tokens += choice_i 的 decode token 数
  prompt_tokens      += choice_i 的 prefill token 数
  prefill_time       += choice_i 的 prefill 耗时
  decode_time        += choice_i 的 decode 耗时

defaultExtra = {
  e2e_latency_s:           (Date.now() - timeReceived) / 1000
  prefill_tokens_per_s:    prompt_tokens / prefill_time
  decode_tokens_per_s:     completion_tokens / decode_time
  time_to_first_token_s:   prefill_time
  time_per_output_token_s: decode_time / completion_tokens
  latencyBreakdown:        仅当请求开启 enable_latency_breakdown 时附带
}
若使用了 grammar/json_object：再附加 grammar_init_s 与 grammar_per_token_s
```

#### 4.2.3 源码精读

起表点与 `enable_latency_breakdown` 的传递：

- [src/engine.ts:796-825](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L796-L825) —— `chatCompletion` 入口第一行 `timeReceived = Date.now()`（L799）；随后把请求采样参数摘入 `GenerationConfig`，其中 L824 把 `request.extra_body?.enable_latency_breakdown` 一路传给管线。
- [src/openai_api_protocols/chat_completion.ts:272-286](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L272-L286) —— `extra_body` 是 WebLLM 对 OpenAI 请求的私有扩展，`enable_latency_breakdown` 开关就定义在这里。

非流式响应的 usage 组装：

- [src/engine.ts:909-916](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L909-L916) —— 逐 choice 累加四个量。
- [src/engine.ts:925-954](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L925-L954) —— `defaultExtra` 的六个公式；`usedGrammar` 为真（`response_format.type` 是 `grammar` 或 `json_object`）时展开附加 grammar 两个字段（L945-953）。

流式路径下同样的 usage 挂在**最后一个 chunk**上，且仅当 `stream_options.include_usage` 开启：

- [src/engine.ts:703-744](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L703-L744) —— 流式路径在流末尾产出 usage chunk，公式与非流式一致（注意流式限定 `n = 1`，故无需累加）。

字段语义的权威注释在协议类型里：

- [src/openai_api_protocols/chat_completion.ts:978-1026](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/chat_completion.ts#L978-L1026) —— `usage.extra` 的完整类型：`e2e_latency_s`、两个 tokens_per_s、`time_to_first_token_s`（"Mainly contains prefilling overhead"）、`time_per_output_token_s`（n>1 时是各 choice 平均）、可选的 grammar 两项与 `latencyBreakdown`。

#### 4.2.4 代码实践

**实践目标**：验证统计口径的自洽性——`e2e_latency_s ≈ time_to_first_token_s + decode 阶段总时长`。

**操作步骤**（示例代码）：

```ts
// 示例代码：口径自洽性检查
const reply = await engine.chatCompletion({
  messages: [{ role: "user", content: "用一百字介绍量子纠缠" }],
  stream: false,
});
const e = reply.usage.extra;
const decodeTotal = e.time_per_output_token_s * reply.usage.completion_tokens;
console.table({
  e2e: e.e2e_latency_s,
  ttft: e.time_to_first_token_s,
  decodeTotal: decodeTotal,
  sum: e.time_to_first_token_s + decodeTotal,
  gap: e.e2e_latency_s - (e.time_to_first_token_s + decodeTotal),
});
```

**需要观察的现象**：`sum` 与 `e2e` 接近但不相等，`gap` 为一个小的正数。

**预期结果**：`gap` 就是引擎层开销（字段校验、锁获取、响应组装）+ 跨 chunk 调度的时间。正常情况下 gap 远小于 sum；若你的页面主线程繁忙（例如同时跑动画），gap 会明显增大。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`time_to_first_token_s` 为什么不等于用户实际看到第一个字的延迟？

**答案**：它取自管线层的 `curRoundPrefillTotalTime`，起点是进入 `prefillStep()` 的时刻，不包含引擎层收到请求后的字段校验、`postInitAndCheckFields`、模型锁排队（[src/engine.ts:796-829](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L796-L829) 中起表与 prefill 之间的部分），也不含流式场景下 chunk 从引擎到渲染的传递时间。真正的用户体感 TTFT 更接近 `e2e_latency_s` 减去除首个 chunk 之外的生成时间。

**练习 2**：请求 `n: 2` 时，`time_per_output_token_s` 的口径是什么？

**答案**：两个 choice 各自串行执行完整的 prefill+decode，引擎层把两者的 `decode_time` 求和、`completion_tokens` 求和后相除（[src/engine.ts:909-930](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L909-L930)），得到的是全部 choice 的平均逐 token 延迟，与协议注释 "average over all choices" 一致。

### 4.3 curRoundLatencyBreakdown：采样阶段的逐步计时

#### 4.3.1 概念说明

`LatencyBreakdown` 把**采样链**（u3-l5 学过的固定顺序）拆成六个数组，每个数组每个被采样的 token 追加一个元素（单位：秒）：

| 字段 | 统计对象 | 对应采样链步骤 |
| --- | --- | --- |
| `grammarBitmaskTime` | 取 grammar 掩码 + 上传 GPU + 应用掩码 | 第 0 步（仅结构化输出时非空） |
| `logitProcessorTime` | 用户 LogitProcessor 的 CPU 处理（含 logits GPU↔CPU 往返） | 第 1 步（仅注册了 processor 时非空） |
| `logitBiasTime` | logit_bias 的 GPU 应用 | 第 2 步（仅设置 logit_bias 时非空） |
| `penaltyTime` | 三种惩罚的 GPU 应用 | 第 3 步（仅设置惩罚时非空） |
| `sampleTime` | softmax + argsort + top-p 抽样 | 第 4 步（恒有值） |
| `totalTime` | `sampleTokenFromLogits` 全程 | 以上全部之和（含零碎开销） |

**最重要的口径事实**：这六个数组只覆盖 `sampleTokenFromLogits` 内部——**模型前向（`embedAndForward`）不在其中**。要估算每个 token 的前向耗时，可以用「decodeStep 总时长 − totalTime」近似（见练习 2）。

两个使用条件：

1. 请求须显式设置 `extra_body: { enable_latency_breakdown: true }`，否则引擎组装 usage 时该字段为 `undefined`，管线层也不往数组里 push（双重门控，省掉生产环境下的数组维护开销）。
2. 数组在每次 `prefillStep()` 重建，长度为**本轮被采样的 token 总数**——包括 prefill 产出的第一个 token，因此通常 `sampleTime.length === completion_tokens + 1`。

#### 4.3.2 核心流程

```text
每个被采样的 token（prefill 的第 1 个 + decode 的每个）：
sampleTokenFromLogits()
  outputTokenBegin = performance.now()          # totalTime 起点
  ├─ [若 grammar] grammarBitmask 计时 → push grammarBitmaskTime
  ├─ [若 logitProcessor] 处理计时 → push logitProcessorTime
  ├─ [若 logit_bias] 应用计时 → push logitBiasTime
  ├─ [若惩罚] 应用计时 → push penaltyTime
  ├─ softmax + 抽样计时 → push sampleTime       # 恒执行
  └─ outputTokenEnd → push totalTime             # 恒执行
```

#### 4.3.3 源码精读

类型定义：

- [src/types.ts:255-262](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L255-L262) —— `LatencyBreakdown` 六个 `number[]`。

管线内六个计时点（都在 `sampleTokenFromLogits` 中，且都受 `genConfig?.enable_latency_breakdown` 门控）：

- [src/llm_chat.ts:1700-1706](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1700-L1706) —— `outputTokenBegin` 起表，`totalTime` 的起点。
- [src/llm_chat.ts:1707-1748](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1707-L1748) —— grammar 掩码：`getNextTokenBitmask()` 取 CPU 掩码、`copyFrom` 上传 GPU、`fapplyBitmask` 就地屏蔽，计时推入 `grammarBitmaskTime`。
- [src/llm_chat.ts:1750-1778](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1750-L1778) —— LogitProcessor：logits 搬到 CPU、执行用户回调、搬回 GPU，计时推入 `logitProcessorTime`。
- [src/llm_chat.ts:1780-1824](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1780-L1824) —— logit_bias 的三张小表上传与应用，推入 `logitBiasTime`。
- [src/llm_chat.ts:1826-1890](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1826-L1890) —— 惩罚的应用，推入 `penaltyTime`。
- [src/llm_chat.ts:1894-1963](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1894-L1963) —— softmax、argsort、top-p 抽样全程，推入 `sampleTime`（无门控条件的步骤，但计时仍有门控）。
- [src/llm_chat.ts:1982-1986](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1982-L1986) —— `totalTime` 收表。

引擎层的出口（门控的另一半）：

- [src/engine.ts:931-933](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L931-L933) —— 组装 `usage.extra` 时，只有请求开启了 `enable_latency_breakdown` 才附带 `latencyBreakdown`，否则为 `undefined`。

官方基准示例的统计方法：

- [examples/get-started-latency-breakdown/src/get_started_latency_breakdown.ts:19-41](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started-latency-breakdown/src/get_started_latency_breakdown.ts#L19-L41) —— `computeStats` 对每个数组计算 avg/min/max/p99。
- [examples/get-started-latency-breakdown/src/get_started_latency_breakdown.ts:77-122](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started-latency-breakdown/src/get_started_latency_breakdown.ts#L77-L122) —— 20 次试验循环：发起请求、抽取 `usage.extra.latencyBreakdown` 与几个标量字段、跨 trial 汇总。**注意**：该示例的请求体（L80-100）并未设置 `extra_body.enable_latency_breakdown`，按引擎层门控逻辑（[src/engine.ts:931](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L931)）取到的 `latencyBreakdown` 会是 `undefined`，L111-116 的 push 实际落空——跑通它之后你应该自己补上这个开关（下节实践）。

#### 4.3.4 代码实践

**实践目标**：打开 `enable_latency_breakdown`，验证数组长度与「前向时间占比」两个口径结论。

**操作步骤**：

1. 进入 `examples/get-started-latency-breakdown/`，`npm install && npm start`（Parcel 起在 8888 端口，见 [package.json:5-8](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started-latency-breakdown/package.json#L5-L8)）。
2. 修改示例的请求（示例代码）：

```ts
// 示例代码：在请求中开启细分计时
const reply0 = await engine.chat.completions.create({
  messages: [{ role: "user", content: "List twenty US states." }],
  temperature: 0,
  max_tokens: 256,
  extra_body: { enable_latency_breakdown: true },  // 关键：官方示例缺这一行
});
const lb = reply0.usage?.extra.latencyBreakdown;
console.log("sampleTime 长度:", lb?.sampleTime.length);
console.log("completion_tokens:", reply0.usage?.completion_tokens);
```

3. 在控制台计算（示例代码）：

```ts
// 示例代码：估算模型前向占解码耗时的比例
const e = reply0.usage.extra;
const avgTotal = lb.totalTime.reduce((a, b) => a + b, 0) / lb.totalTime.length;
console.log("每 token 采样链:", avgTotal, "s");
console.log("每 token 总耗时:", e.time_per_output_token_s, "s");
console.log("前向占比 ≈", 1 - avgTotal / e.time_per_output_token_s);
```

**需要观察的现象**：`sampleTime.length` 比 `completion_tokens` 多 1；前向占比是一个明显小于 1 的数。

**预期结果**：数组长度 = completion_tokens + 1（多出的 1 来自 prefill 末尾的首 token 采样，[src/llm_chat.ts:890](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L890)）；`time_per_output_token_s` 略大于 `avgTotal`，差值即近似的前向耗时。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `grammarBitmaskTime`、`logitProcessorTime` 等数组在不满足条件时是**空数组**而不是填充 0？

**答案**：每个计时点都包在对应功能的 `if` 分支内（有 grammar 才计 grammar、注册了 processor 才计 processor，见 4.3.3 的源码引用），未启用的步骤根本不执行、自然不 push。这既是性能考虑（零开销），也让「数组是否为空」本身成为「该功能是否启用」的探测信号。示例的 `computeStats` 也据此对空数组返回 `undefined`。

**练习 2**：如何用 `LatencyBreakdown` 估算单个 decode token 的**模型前向**耗时？

**答案**：`decodeStep` 的表覆盖「前向 + 采样」全程，而 `totalTime` 覆盖采样全程，因此 前向 ≈ `time_per_output_token_s − avg(totalTime)`。两者相减还剩 logits 张量 dispose 等零碎开销，所以只是近似。这也解释了为什么「开满」所有采样功能（grammar + processor + 惩罚）会直接侵蚀解码速度——它们全部串行在每步的关键路径上。

### 4.4 runtimeStatsText 与 forwardTokensAndSample：遗留路径与低层 API

#### 4.4.1 概念说明

`engine.runtimeStatsText()` 是最早期的性能观测接口：返回一行 `"prefill: X tokens/sec, decoding: Y tokens/sec"` 字符串。官方已给它加上废弃警告，指引用户改用 `usage`（非流式）或 `stream_options.include_usage`（流式）。**唯一被官方认可继续使用它的场景是 `forwardTokensAndSample()`**——这是暴露给用户的低层 API：调用者直接喂 token id 数组、拿回一个采样 token，绕过整个对话模板与消息协议。这条路径没有 `usage` 可看，只能靠 `runtimeStatsText()` 观测。

#### 4.4.2 核心流程

```text
engine.forwardTokensAndSample(inputIds, isPrefill)
  └─ 管线 forwardTokensAndSample()
       ├─ 分块（按 prefillChunkSize）
       ├─ 逐块 embedAndForward
       ├─ sampleTokenFromLogits
       └─ 按 isPrefill 把时长归入 prefill 或 decode 统计
            （跨轮 + curRound 双写，供 runtimeStatsText / usage 读取）
```

#### 4.4.3 源码精读

- [src/llm_chat.ts:2137-2186](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2137-L2186) —— 管线版 `forwardTokensAndSample`：手动分块、前向、采样，末尾按 `isPrefill` 把 `(tend - tstart)/1e3` 与 token 数**同时**累加进跨轮与 curRound 两套字段（L2173-2184）——这是低层 API 与高层统计共用的落表点。
- [src/engine.ts:1293-1303](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1293-L1303) —— 引擎版转发，经 `getLLMStates` 路由到目标管线。
- [src/engine.ts:1315-1324](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1315-L1324) —— `runtimeStatsText()`：先打废弃警告，再返回管线的统计字符串。
- [src/llm_chat.ts:633-651](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L633-L651) —— `runtimeStatsText()` 与 `curRoundRuntimeStatsText()` 的实现：前者用跨轮字段，后者用 curRound 字段，格式相同。
- [src/web_worker.ts:265-268](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L265-L268) —— Worker 消息协议里 `runtimeStatsText` 是一种独立的请求 kind，代理引擎同样可用（u5-l1 的消息协议在性能观测上也是完整的）。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：确认「低层 API 是 `runtimeStatsText()` 的最后阵地」这一说法的调用链。

**操作步骤**：

1. 在 `src/` 中全文搜索 `runtimeStatsText` 的所有出现点（共 6 处：管线实现、引擎转发、Worker 两处、types 接口声明、message 协议）。
2. 逐个分类：哪些是「实现/转发」，哪些是「调用」。你会发现除了引擎与 Worker 的转发链路外，**没有任何高层生成路径调用它**。
3. 再读 [src/llm_chat.ts:2173-2184](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2173-L2184)，注意 `forwardTokensAndSample` 双写两套统计字段的设计。

**需要观察的现象**：`runtimeStatsText` 的调用方只有用户代码，库内部无自调用。

**预期结果**：你能画出 `engine.runtimeStatsText → getLLMStates → pipeline.runtimeStatsText` 与 `engine.forwardTokensAndSample → pipeline.forwardTokensAndSample`（落表）→ `engine.runtimeStatsText`（读表）两条链；这正是低层 API 用户「跑若干步、读一次统计」的使用模式。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `forwardTokensAndSample()` 的用户拿不到 `usage.extra`？

**答案**：`usage` 是 `chatCompletion`/`completion` 高层路径在响应组装阶段从管线 getter 拼出来的（[src/engine.ts:925-954](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L925-L954)）；`forwardTokensAndSample` 直接返回一个 token id，不经过响应组装，所以只能通过 `runtimeStatsText()`（或自己调用 getter 所在管线的公开方法）读取统计——这正是引擎废弃警告里保留它的原因。

**练习 2**：`forwardTokensAndSample` 把统计「双写」进跨轮与 curRound 两套字段，有什么好处？

**答案**：让低层 API 的计时既能在下一次低层调用间被 `runtimeStatsText()` 读到（跨轮字段，注意每次高层 prefill 会清零、低层路径不清），又能在「低层预热 + 高层正式请求」混合使用时让 `usage.extra` 取到本轮 curRound 口径的数值，两套观测口径互不干扰。

## 5. 综合实践：prompt 长度对 TTFT 与解码速度的影响实验

这是本讲的收官实验，对应官方示例 `examples/get-started-latency-breakdown`（Parcel 启动方式见其 [README.md](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started-latency-breakdown/README.md) 与 [package.json:5-8](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started-latency-breakdown/package.json#L5-L8)）。

**实验设计**：同一模型，三档长度的 prompt（约 32 / 128 / 1024 token），各发起一轮非流式请求，记录 TTFT 与 decode tokens/s，验证统计口径，最后解释「长 prompt 为什么拖慢首字出现」。

**操作步骤**：

1. `cd examples/get-started-latency-breakdown && npm install && npm start`，用支持 WebGPU 的浏览器打开 `http://localhost:8888`。
2. 仿照示例的 `main()` 写实验脚本（示例代码）：

```ts
// 示例代码：三档 prompt 长度的 TTFT 实验
function makePrompt(approxTokens: number): string {
  // 用重复句子把 prompt 撑到目标 token 数（token 数以 usage.prompt_tokens 实测为准）
  const unit = "The quick brown fox jumps over the lazy dog. ";
  return unit.repeat(Math.ceil(approxTokens / 16)); // 该句约 10~11 token，先粗调后实测校准
}

for (const target of [32, 128, 1024]) {
  const reply = await engine.chatCompletion({
    messages: [
      { role: "system", content: "You are a helpful assistant." },
      { role: "user", content: makePrompt(target) + "\n\nNow reply with the single word: OK." },
    ],
    temperature: 0,
    max_tokens: 64, // 压低 decode 长度，让 TTFT 成为观测主角
    stream: false,
  });
  const e = reply.usage.extra;
  console.table({
    target,
    prompt_tokens: reply.usage.prompt_tokens,       // 实际 prefill token 数（口径验证）
    ttft_s: e.time_to_first_token_s,
    prefill_tok_s: e.prefill_tokens_per_s,
    decode_tok_s: e.decode_tokens_per_s,
    e2e_s: e.e2e_latency_s,
  });
}
```

3. 口径验证：对每一档检查等式 \( \text{prefill\_tokens\_per\_s} \approx \text{prompt\_tokens} / \text{time\_to\_first\_token\_s} \)（两者分子分母同源，理应严格成立，误差只来自浮点显示）。
4. 把三档的 TTFT 与 decode tokens/s 画成曲线（折线即可）。

**需要观察的现象**：

- TTFT 随 prompt 档位近似线性增长；
- `prefill_tokens_per_s` 通常显著高于 `decode_tokens_per_s`（prefill 是大批量矩阵乘，GPU 利用率高）；
- `decode_tokens_per_s` 在三档之间基本持平（decode 借 KV cache 只前向 1 个 token，与 prompt 长度几乎无关——但 1024 档因 KV cache 变长，注意力开销略增，可能小幅下降）。

**预期结果与结论（待本地验证）**：长 prompt 拖慢首字出现，不是因为模型「变慢」，而是因为 prefill 的计算量与 prompt token 数成正比（\[ \text{FLOPs} \approx 2 N_{\text{params}} L \]），且受 `prefillChunkSize` 显存约束只能分块串行；虽然 prefill 阶段的吞吐（tokens/s）远高于 decode，总耗时 \( T = L / \text{tokens\_per\_s} \) 仍随 \( L \) 线性上升，全部计入 TTFT。工程启示：控制 system prompt 与历史消息长度、或利用多轮 KV cache 复用（第二轮只 prefill 增量），是压低 TTFT 的两个正交手段。

## 6. 本讲小结

- 管线内有**两套同构计时器**：跨轮统计（`prefillTotalTime` 等）与本轮 `curRound*` 统计；因 `resetStatsPerPrefill` 恒为 true，跨轮统计实际也随每次 prefill 清零，`runtimeStatsText()` 由此走向废弃。
- `usage.extra` 的口径：`time_to_first_token_s` 就是本轮 prefill 耗时（含首 token 采样，不含引擎层排队）；`decode_tokens_per_s` 的分母含采样链；`e2e_latency_s` 用 `Date.now()` 从请求进入引擎起计；`n > 1` 时为求和后的加权平均。
- `curRoundLatencyBreakdown` 六个数组只细分**采样链**（grammar 掩码、LogitProcessor、logit_bias、惩罚、抽样、总时长），模型前向不在其中；需请求显式开启 `extra_body.enable_latency_breakdown`，数组长度为 completion_tokens + 1（含 prefill 的首 token）。
- 官方示例 `get-started-latency-breakdown` 的请求体未开启该开关，需要自行补上 `extra_body` 才能拿到细分数据。
- `runtimeStatsText()` 的最后阵地是低层 API `forwardTokensAndSample()`——该路径无 `usage` 可读，其计时在管线内双写进两套统计字段。

## 7. 下一步学习建议

本讲是第 7 单元（高级主题）的中段。接下来建议：

1. **u7-l4 测试体系与质量保障**：本讲的口径结论（如数组长度、TTFT 公式）很多可以固化成断言，学完测试体系后可以尝试为 `LatencyBreakdown` 写一个 mock 单测。
2. 对照阅读 `tests/llm_chat_pipeline.test.ts` 中与统计相关的断言，看官方如何用真实模型验证口径。
3. 若你对性能优化本身感兴趣，可回到 `src/llm_chat.ts` 思考两个开放问题：prefill 分块大小如何影响 TTFT（u3-l3 已铺垫）、采样链各步骤中哪些可以与下一次前向重叠（grammar matcher 初始化的并行化是一个现成范例，[src/llm_chat.ts:763-831](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L763-L831)）。
