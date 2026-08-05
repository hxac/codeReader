# 响应体处理与插件回调

## 1. 本讲目标

本讲是「请求处理主链路」的收尾篇，承接 u5-l1（请求体解析与入口解析）与 u5-l2（决策求值管线）。前面两讲回答的是「请求怎么进来、怎么被路由」；本讲回答的是「**后端把响应字节流回来后，Semantic Router（SR）在把它交给客户端之前做了什么**」。

学完本讲你应该能够：

- 说清楚 `handleResponseBody` 这个总分发器的「早退闸门 + 分支顺序」是如何组织的，以及为什么分支顺序不能乱。
- 理解 SR 如何把不同 provider（主要是 Anthropic）的异构响应**归一化**成统一的 OpenAI 形态，又在出口处按客户端协议**改写**回去。
- 区分流式响应与非流式响应的两条处理路径：流式是「逐块累积 + 收尾一次性回写」，非流式是「一次性把缓存、检测、记忆、告警、重放全部跑完」。
- 解释响应阶段如何回写**语义缓存**与**路由重放（router replay）**记录，并能说出一个关键事实：**缓存命中的请求根本不会进入响应体处理**。

## 2. 前置知识

在进入源码前，先用通俗语言把几个本讲会反复用到的概念对齐。这些概念大多在 u4-l3、u5-l1 已建立，这里只做最小回顾。

- **ExtProc 四阶段模型**：Envoy 把一次代理拆成「请求头 / 请求体 / 响应头 / 响应体」四个阶段，每个阶段都通过 gRPC 流调用 SR 的 `Process`。本讲聚焦最后一个阶段——响应体。SR 收到的是 Envoy 转交过来的「上游后端响应的字节」。
- **`RequestContext` 是请求级状态账本**：它是随消息推进不断累积的 per-request 结构体。响应体阶段读写的几乎所有信息（是否流式、客户端协议、上游 API 格式、累积的流式内容、缓存命中标记……）都挂在它上面。本讲会反复出现它的字段。
- **两种回包方式**：SR 对 Envoy 的响应体回复有两种语义——`CONTINUE`（放行，可附带 body/header 改写）与 `ImmediateResponse`（直接由 SR 自己回包，Envoy 不再转发给上游）。u5-l1 已经提过：**缓存命中**走的就是 `ImmediateResponse`。这一点是理解本讲「缓存命中为何不进响应阶段」的钥匙。
- **插件是 decision 级的**：在 u3-l1 我们学过，插件嵌在每条 `decisions[].plugins` 里、按路由启停。本讲的「响应阶段插件回调」（越狱检测、幻觉检测、记忆存储、响应告警）就是这些 decision 级插件在响应体阶段被触发的部分。
- **OpenAI SSE 形态**：流式响应是一串 `data: {JSON}\n\n` 行，以 `data: [DONE]` 结尾。SR 在流式路径里就是一行行解析这些 chunk。

## 3. 本讲源码地图

本讲围绕 `pkg/extproc` 下的一组响应体处理文件展开。它们共同实现「响应体阶段」的全部逻辑。

| 文件 | 作用 |
| --- | --- |
| [processor_res_body.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body.go) | 响应体总分发器 `handleResponseBody`，含早退闸门、分支顺序与 provider 归一化入口 |
| [processor_res_body_pipeline.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body_pipeline.go) | 非流式响应处理 `handleNonStreamingResponseBody`、客户端协议改写 `translateResponseBodyForClient`、响应告警聚合 |
| [processor_res_body_streaming.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body_streaming.go) | 流式响应处理 `handleStreamingResponseBody`、TTFT 记录、收尾 `finalizeStreamingResponse`、chunk 解析 |
| [processor_res_body_streaming_anthropic_client.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body_streaming_anthropic_client.go) | Anthropic 客户端的流式专用路径（入站走 `/v1/messages` 的客户端） |
| [processor_res_body_streaming_anthropic.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body_streaming_anthropic.go) | 旧路径：OpenAI 客户端命中 Anthropic 后端的流式翻译 |
| [req_filter_skip_processing.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_skip_processing.go) | `SkipProcessing` 早退闸门 |
| [processor_res_cache.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_cache.go) | 语义缓存回写：非流式 `updateResponseCache` 与流式 `cacheStreamingResponse` |
| [processor_res_memory.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_memory.go) | 响应阶段异步记忆存储 `scheduleResponseMemoryStore` |
| [recorder.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/recorder.go) | 路由重放记录回写 `attachRouterReplayResponse` |
| [req_filter_cache.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_cache.go) | 缓存命中检测（请求阶段）——理解「缓存命中不进响应阶段」的关键证据 |
| [res_filter_jailbreak.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/res_filter_jailbreak.go) / [res_filter_hallucination.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/res_filter_hallucination.go) | 响应阶段安全检测插件：响应越狱检测、幻觉检测 |

> 提示：以上行号锚点都基于当前 HEAD `7a77e1e1`。后续若代码变动，请以函数名为准重新定位。

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：先看总分发器与早退闸门（4.1），再依次进入 provider 归一化（4.2）、流式处理（4.3）、缓存与重放回写（4.4），最后是响应阶段的插件回调链（4.5）。

### 4.1 响应体分发：主入口、早退闸门与分支顺序

#### 4.1.1 概念说明

响应体阶段的总入口是 `OpenAIRouter.handleResponseBody`。它的职责不是「亲自处理响应」，而是当一个**调度员**：先过两道「早退闸门」把不该走完整管线的请求挡掉，再用一组**有严格顺序的 if 分支**把请求送到正确的子处理器。

为什么需要早退闸门？因为有两类请求虽然进入了响应体阶段，但**不应该被 SR 改写或分析**：

- **`SkipProcessing` 请求**：客户端或上游 filter（如 Envoy AI Gateway）显式要求 SR「别管这个请求」，SR 应原样放行。
- **Looper 请求**：SR 内部的 router-replay 机制会自己发回一些内部请求来重放/校验路由决策，这些请求的响应只需要被「抓取」下来做审计，不需要再走一遍缓存/检测/告警。

为什么分支顺序不能乱？因为「客户端协议」和「上游 API 格式」是两个**独立维度**，组合出多个象限，其中「Anthropic 客户端 + Anthropic 后端」这个**双重 Anthropic 象限**会同时匹配新旧两条分支，必须让新分支先判，否则客户端会收到错误形态的 SSE。

#### 4.1.2 核心流程

`handleResponseBody` 的执行顺序可以用下面的伪代码描述：

```
handleResponseBody(body, ctx):
  ① SkipProcessing 闸门     → 命中则裸 CONTINUE 放行（不改写）
  ② 记录 completionLatency；defer 递减「模型活跃请求数」指标
  ③ Looper 闸门             → 命中则抓取 body 做重放，裸 CONTINUE 返回
  ④ 流式 && 客户端=Anthropic → handleAnthropicClientStreamingResponseBody  （新分支，优先）
  ⑤ 流式 && 后端=Anthropic && 客户端≠Anthropic → handleAnthropicStreamingResponseBody （旧分支）
  ⑥ normalizeProviderResponseBody（把 Anthropic 后端响应归一化成 OpenAI）
     失败 → 502
  ⑦ 流式？ → handleStreamingResponseBody
     否    → handleNonStreamingResponseBody
```

注意 ④⑤ 在 ⑥（归一化）**之前**：流式的 Anthropic 路径自己内部做翻译，不共用 ⑥；⑥ 只服务于**非流式**与**OpenAI 客户端的流式**。

#### 4.1.3 源码精读

总分发器本体，注意四个 `if` 的先后与各自的返回：

[processor_res_body.go:16-60](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body.go#L16-L60) —— `handleResponseBody`：先调 `handleSkipProcessingResponseBody`（①），再用 `defer metrics.DecrementModelActiveRequests` 登记活跃请求递减（②），再调 `handleLooperResponseBody`（③）。随后是两条流式 Anthropic 分支（④⑤），最后 `normalizeProviderResponseBody`（⑥）后按是否流式二分（⑦）。

两条流式分支的顺序与守卫条件是本模块的重点。[processor_res_body.go:38-48](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body.go#L38-L48) 的新分支用 `ClientProtocol == Anthropic` 判定；[processor_res_body.go:45-48](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body.go#L45-L48) 的旧分支额外加了 `ClientProtocol != Anthropic` 守卫。源码注释把这一点讲得很清楚：注释里说「这个判断必须排在旧的 `handleAnthropicStreamingResponseBody` 分支之前，否则双重 Anthropic 象限会命中旧分支，导致客户端收到 OpenAI SSE」。

两道早退闸门的实现都很薄。SkipProcessing 闸门：

[req_filter_skip_processing.go:75-83](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_skip_processing.go#L75-L83) —— `handleSkipProcessingResponseBody`：若 `ctx.SkipProcessing` 为真，返回一个**不带任何 mutation** 的 `CONTINUE`，即「我把 body 原样放给客户端，不动一个字节」。

Looper 闸门：

[processor_res_body.go:62-73](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body.go#L62-L73) —— `handleLooperResponseBody`：若 `ctx.LooperRequest`，调用 `attachRouterReplayResponse` 把响应体抓进重放记录，然后返回裸 `CONTINUE`。它**绕开了**缓存/检测/告警，因为这些内部重放请求不该再产生副作用。

最后看一个被反复复用的「构造放行响应」的小工具，理解后续代码会很简单：

[processor_res_body.go:103-118](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body.go#L103-L118) —— `buildResponseBodyContinueResponse`：包一个 `CommonResponse_CONTINUE`，可选地挂上 `BodyMutation`（改写 body）和 `HeaderMutation`（改写头）。当两个 mutation 都为 nil 时，就是「原样放行」。

#### 4.1.4 代码实践

- **实践目标**：建立对分发器「分支顺序」的肌肉记忆，并验证早退闸门的存在。
- **操作步骤**：
  1. 打开 `processor_res_body.go`，在 `handleResponseBody` 的每个 `return` 前面（共 6 处返回点）用铅笔/编辑器标注它是哪条分支。
  2. 用 `grep` 在 `pkg/extproc` 内搜 `LooperRequest =` 与 `SkipProcessing =`，找到这两个字段是在**请求阶段**的哪个环节被置位的（提示：分别在 looper 相关的请求处理与 skip 处理中）。
  3. 做一个思维实验：如果把 ④（新分支）和 ⑤（旧分支）的代码**对调**，对于一个「Anthropic 客户端 + Anthropic 后端」的流式请求，会命中哪条分支、客户端会看到什么形态的 SSE？
- **需要观察的现象**：你会确认 ⑤ 的守卫 `ClientProtocol != Anthropic` 正是为了「在新分支缺失或被对调时仍兜底保护」而存在。
- **预期结果**：对调后，由于旧分支的 `APIFormat == Anthropic` 条件仍成立、而守卫 `ClientProtocol != Anthropic` 会被双重 Anthropic 请求**拒绝**，于是该请求会**两条都不命中**，落到 ⑥ 归一化后再走普通流式路径——这正是注释警告的「客户端收到 OpenAI SSE」的错配场景。
- 若无法实际构造 Anthropic 后端环境，本步骤标注为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`handleResponseBody` 里 `defer metrics.DecrementModelActiveRequests(ctx.RequestModel)` 为什么用 `defer` 而不是直接调用？

**参考答案**：因为本函数有多个 `return` 出口（早退闸门、各分支、错误 502）。用 `defer` 能保证无论从哪条路径返回，活跃请求计数都恰好递减一次，避免漏减导致「在途请求计数」虚高。这个计数用于排队深度估计（u11-l3 会讲 inflight）。

**练习 2**：SkipProcessing 闸门返回的 `CONTINUE` 带了 body/header mutation 吗？这意味着什么？

**参考答案**：没带（`buildResponseBodyContinueResponse(nil, nil)`）。意味着 SR 完全不触碰响应字节，Envoy 把上游响应原样转给客户端——这正是「跳过处理」的语义。

---

### 4.2 Provider 归一化：把异构后端统一成 OpenAI 形态

#### 4.2.1 概念说明

SR 的内部管线（缓存、检测、告警、指标）都假设响应是 **OpenAI ChatCompletion 形态**。但后端可能是 Anthropic（响应是 `content/stop_reason` 那一套）。于是在响应体阶段需要一个**归一化**步骤：把 Anthropic 响应翻成 OpenAI。

这里有一个微妙但关键的设计：归一化发生在**两处**，方向相反：

- **入口归一化**（`normalizeProviderResponseBody`）：上游→内部，Anthropic 响应 → OpenAI 响应。让后续管线面对统一形态。
- **出口改写**（`translateResponseBodyForClient`）：内部→客户端，按**客户端**期望的协议再翻一次（比如客户端用的是 `/v1/messages`，就得把 OpenAI 形态翻回 Anthropic SSE/JSON 再给它）。

也就是说：响应在 SR 内部走一圈是「进站翻成 OpenAI → 处理 → 出站翻成客户端协议」。这种「中间统一、两端适配」是处理多 provider 异构的经典手法。

#### 4.2.2 核心流程

```
normalizeProviderResponseBody(body, ctx):
  if 上游 APIFormat != Anthropic: 原样返回（无需归一化）
  else: anthropic.ToOpenAIResponseBodyWithExt(body, model, IRExtensions)
        → (transformedBody, true, nil)
        失败 → (nil, false, err)  // 上层据此回 502
```

出口改写 `translateResponseBodyForClient` 则是一个二选一的分发：客户端是 Anthropic 就 `EmitAnthropicResponse`；是 Response API（`/v1/responses`）就调 `ResponseAPIFilter.TranslateResponse`；都不是就原样透传。它返回 `(body, transformed)`，`transformed` 用来决定要不要生成 body mutation。

#### 4.2.3 源码精读

入口归一化，注意它只对 Anthropic 后端生效，并保留 `IRExtensions`（推理结果扩展，如 cache 用量、thinking 签名）：

[processor_res_body.go:75-101](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body.go#L75-L101) —— `normalizeProviderResponseBody`：当 `ctx.APIFormat == APIFormatAnthropic` 时调 `anthropic.ToOpenAIResponseBodyWithExt`，把 Anthropic 响应翻成 OpenAI。源码注释说明：保留 `IRExtensions` 是为了让 Anthropic 独有的 stop reason、cache 用量计数、server-tool 计数、thinking 块签名落到 per-request sidecar 上，随后由出站 emitter 再现给 Anthropic 客户端。

出口改写分发器，注意它的分支顺序注释与 nil 防御：

[processor_res_body_pipeline.go:58-94](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body_pipeline.go#L58-L94) —— `translateResponseBodyForClient`：先 nil 防御；再判 `ClientProtocol == Anthropic` 走 `EmitAnthropicResponse`；否则判 Response API。注释指出 Anthropic 与 Response API 互斥（入站路径不同：`/v1/messages` vs `/v1/responses`）。

有了「是否被改写」的标记，构造 mutation 的逻辑就很简洁——只有真的改写了才需要替换 body 并删掉旧的 `content-length`：

[processor_res_body_pipeline.go:96-111](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body_pipeline.go#L96-L111) —— `buildInitialResponseMutations`：`bodyWasTransformed` 为假时返回 `(nil, nil)`（零改写零开销）；为真时生成 `BodyMutation` 替换 body，并 `RemoveHeaders: ["content-length"]`（因为新 body 长度变了，必须让 Envoy 重算）。

> 在 4.1 的总分发器里，`normalizeProviderResponseBody` 返回的 `anthropicTransformed` 会与出口的 `clientTransformed` 用**或**合并（[processor_res_body_pipeline.go:28-31](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body_pipeline.go#L28-L31)），任一处改写都触发 mutation。

#### 4.2.4 代码实践

- **实践目标**：追踪一次「OpenAI 客户端 + Anthropic 后端」的非流式请求，看响应被翻两次。
- **操作步骤**：
  1. 在 `normalizeProviderResponseBody` 入口与出口各加一行日志（**示例代码**，非项目原有）：`logging.Debugf("norm-in size=%d", len(responseBody))` 与对应出口。
  2. 在 `translateResponseBodyForClient` 的返回前打印 `clientTransformed`。
  3. 用一个 OpenAI 客户端请求经过配置成 Anthropic 后端的 SR，观察日志。
- **需要观察的现象**：入口归一化把 Anthropic body 翻成 OpenAI（`anthropicTransformed=true`）；因为客户端是 OpenAI，出口改写 `clientTransformed=false`。最终 mutation 仍会生成（因为「或」合并）。
- **预期结果**：客户端收到 OpenAI 形态响应，且响应长度与上游不同（因为删了 `content-length`、换了 body）。
- 若没有 Anthropic 后端可测，改为阅读 `pkg/anthropic` 中 `ToOpenAIResponseBodyWithExt` 与 `EmitAnthropicResponse` 的字段映射，描述它们互为逆操作关系。标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么入口归一化和出口改写都返回一个「是否改写」的布尔，而不是无条件生成 mutation？

**参考答案**：性能与正确性。多数请求（OpenAI 客户端 + OpenAI 后端）两处都不改写，返回 `(nil,nil)` 让 Envoy 零拷贝透传上游 body；只有真改写时才付出「替换 body + 删 content-length」的代价。无条件 mutation 会破坏所有透传请求的字节流与长度。

**练习 2**：`IRExtensions` 为什么要在归一化时「透传」进来？

**参考答案**：因为 Anthropic 响应里有些字段（cache 用量、thinking 块签名、stop_reason 等）OpenAI 形态装不下，但又必须最终还回给 Anthropic 客户端。把它们存在 per-request 的 `IRExtensions` sidecar 里，出站 emitter 再现时读取，保证客户端看到与上游一致的字段。

---

### 4.3 流式响应处理：分块累积、TTFT 与收尾

#### 4.3.1 概念说明

流式响应（SSE）和非流式有本质区别：非流式是「一次到齐」，流式是「许多小块陆续到」。Envoy 的 STREAMED 模式会把响应体按**任意字节偏移**切成多次调用送进来，所以 SR 处理流式的核心是「**边累积、边解析、到末尾再一次性收尾**」。

这带来三个关键问题，本模块逐一回答：

1. **TTFT（Time To First Token，首 token 延迟）怎么测？**——在**第一个**流式块到达时记录，并喂给延迟/缓存热度模型。
2. **累积什么？**——把每个 `data:` 块里的 `delta.content`、`reasoning_content`、`refusal`、tool call、metadata 累到 `RequestContext` 上。
3. **何时收尾？**——看到 `data: [DONE]` 或上游的 streamDone 信号时，调 `finalizeStreamingResponse` 一次性完成「记指标 + 结束在途 + 缓存回写 + 重放回写」。

#### 4.3.2 核心流程

普通流式路径（`handleStreamingResponseBody`，每个块调一次）：

```
on each chunk:
  recordStreamingTTFT(ctx)          # 仅首块生效，记录 TTFT、更新缓存热度
  ensureStreamingState(ctx)         # 初始化各 map
  [Response API 流式] reassembleSSEFrames  # 重组跨块的残帧
  parseStreamingChunk(chunk)        # 解析每行 data:, 累积 content/reasoning/toolcalls
  streamDone = chunk 含 "data: [DONE]"
  if streamDone: finalizeStreamingResponse(ctx)   # 收尾：指标+缓存+重放
  return CONTINUE(nil, nil)         # OpenAI SSE 原样透传（不改写）
```

收尾 `finalizeStreamingResponse`（被幂等保护，只生效一次）：

```
if ctx.StreamingComplete: return    # 幂等
ctx.StreamingComplete = true
记录 completion 延迟指标
inflight.End(model, token)          # 结束在途计数
reportStreamingUsageMetrics + calibrateTokenEstimator
cacheStreamingResponse(ctx)         # 把累积内容重建成完整响应再缓存
attachRouterReplayResponse(ctx, replayBody, isFinal=true)
```

> TTFT 与缓存热度的关系（u11-l3 会展开）：SR 用历史 TTFT 估计「这次命中缓存」的概率。直觉是：缓存命中的请求 TTFT 往往更短、更稳定，因此 TTFT 可作为缓存热度的信号。用 sigmoid 把 TTFT 距离映射到 \([0,1]\)：

\[ \text{warmth} \approx \sigma(-k \cdot (ttft - \tau)) \]

TTFT 越小（越接近缓存命中特征），warmth 越接近 1。这条估计会反过来影响后续的负载与调度决策。

#### 4.3.3 源码精读

普通流式主循环，注意 TTFT 在最前面、收尾在 `streamDone` 时：

[processor_res_body_streaming.go:16-52](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body_streaming.go#L16-L52) —— `handleStreamingResponseBody`：`recordStreamingTTFT` → `ensureStreamingState` →（Response API 才重组帧）→ `parseStreamingChunk` → 判 `data: [DONE]` → 必要时 `finalizeStreamingResponse`。普通 OpenAI 流式最后返回 `buildResponseBodyContinueResponse(nil, nil)`，即**不改写地透传**上游 SSE。

TTFT 记录的「只记一次」逻辑：

[processor_res_body_streaming.go:54-74](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body_streaming.go#L54-L74) —— `recordStreamingTTFT`：靠 `ctx.TTFTRecorded` 守卫保证只记首个块；算出 `ttft = time.Since(ctx.ProcessingStartTime)`，记 Prometheus 指标、更新 `latency.UpdateTTFT` 与 `CacheWarmthEstimate`。

收尾函数，注意最上面的幂等守卫：

[processor_res_body_streaming.go:91-135](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body_streaming.go#L91-L135) —— `finalizeStreamingResponse`：注释解释了为什么需要幂等——Anthropic 客户端流式路径里，emitter 的 streamDone 在某个块触发后，后面可能还有一个也匹配 `data: [DONE]` 的终止块，会导致本函数被进入两次，第二次必须是 no-op。函数体内依次记 completion 延迟、`inflight.End`、报 usage、调 `cacheStreamingResponse`、构造重建响应做 `attachRouterReplayResponse`。

逐块解析，看累积了哪些字段：

[processor_res_body_streaming.go:138-162](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body_streaming.go#L138-L162) —— `parseStreamingChunk`：按 `\n` 拆行，只处理 `data: ` 前缀的行，跳过 `[DONE]`，逐行 `json.Unmarshal` 后抽 metadata/content/toolCalls/usage。

[processor_res_body_streaming.go:200-213](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body_streaming.go#L200-L213) —— `extractStreamingDeltaContent`：累积 `StreamingContent`（`delta.content`）、`StreamingReasoning`（`delta.reasoning_content`，推理模型的思考流）、`StreamingRefusal`（`delta.refusal`）。注释强调保留 reasoning 是为了让缓存命中时返回的重建响应带着和直播流一样的思考内容。

**Anthropic 客户端的特殊流式路径**值得单独看，因为它体现了「SR 接管线路」的强约束：

[processor_res_body_streaming_anthropic_client.go:44-111](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body_streaming_anthropic_client.go#L44-L111) —— `handleAnthropicClientStreamingResponseBody`：无论上游是 Anthropic 还是 OpenAI SSE，都先翻译成 OpenAI SSE 喂给累积器，再用 `EmitAnthropicSSEChunk` 重发成 Anthropic SSE 给客户端。它还处理「跨块残帧重组」与「keepalive ping」。

[processor_res_body_streaming_anthropic_client.go:113-125](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body_streaming_anthropic_client.go#L113-L125) —— `emptyAnthropicBodyMutation`：一旦进入这条路径，**SR 完全接管线路**，每次返回都必须用 BodyMutation 替换 body（哪怕替换成空）。注释说明：如果返回 nil mutation，Envoy 会原样转发上游 chunk，导致客户端看到「合成的 Anthropic 事件 + 泄漏的上游帧」（最明显是多出一个 `message_stop`）。这正是为什么错误/零产出路径也要用空 body mutation。

#### 4.3.4 代码实践

- **实践目标**：观察流式响应「逐块累积、到 [DONE] 收尾」的过程。
- **操作步骤**：
  1. 在 `parseStreamingChunk` 里每解析出一个非空 `delta.content` 时，打印累积长度 `len(ctx.StreamingContent)`（**示例代码**：`logging.Debugf("acc=%d", len(ctx.StreamingContent))`）。
  2. 在 `finalizeStreamingResponse` 入口打印 `ctx.StreamingComplete` 的值，验证幂等守卫。
  3. 对 SR 发起一个流式 `/v1/chat/completions` 请求（`stream: true`）。
- **需要观察的现象**：你会看到多行逐块累积日志，长度递增；最后在 `data: [DONE]` 时触发一次 `finalizeStreamingResponse`，`StreamingComplete` 从 false 翻成 true。
- **预期结果**：缓存里写入一条「由累积内容重建」的完整 ChatCompletion（见 4.4）。若再次发送相似请求，应命中缓存（`x-vsr-cache-hit: true`）。
- 若无法发起流式请求，改为阅读 `buildReconstructedStreamingResponse`（4.4 源码），描述它如何把 `StreamingContent` + metadata 拼成一个非流式 JSON。标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `finalizeStreamingResponse` 必须幂等？

**参考答案**：因为同一个流可能因不同信号触发两次收尾判断（emitter 的 streamDone 与后续的 `data: [DONE]` 终止块）。若不幂等，会重复记指标、重复 `inflight.End`、重复缓存写入。幂等守卫 `if ctx.StreamingComplete { return }` 保证收尾动作只生效一次。

**练习 2**：普通 OpenAI 流式路径最后 `return buildResponseBodyContinueResponse(nil, nil)`，即不改写 body。那 SR 是怎么「看见」流式内容的？

**参考答案**：SR 不改写，但**旁路解析**：`parseStreamingChunk` 把每个 chunk 解析后累积到 `RequestContext`，用于指标、TTFT、缓存重建与重放。客户端实际收到的仍是上游原始 SSE 字节，SR 只「读」不「写」。只有 Anthropic 客户端路径才真正改写 body。

**练习 3**：`anthropicPingCadence`（约 15s）的 keepalive ping 解决什么问题？

**参考答案**：在稀疏 token 窗口（两次 chunk 间隔很长）时，中间设备（middlebox）可能切断长连接。ping 让客户端（含 SDK 的 MessageStream 累积器）在静默期收到「无害事件」，保持连接存活。ping 只在距上一块超过 cadence 时才发，首块前不发。

---

### 4.4 缓存与重放回写：写入门槛与缓存命中特殊路径

#### 4.4.1 概念说明

响应阶段有两类「写回」副作用：把这次请求-响应对写进**语义缓存**（下次相似查询可直接命中），以及把它写进**路由重放记录**（router replay，供审计与离线分析路由决策）。两者都发生在响应阶段，但门槛与时机不同。

本模块还有一个**反直觉但极其重要**的事实：**缓存命中的请求根本不会进入响应体处理**。因为缓存命中是在**请求阶段**判定的，命中时 SR 直接用 `ImmediateResponse` 把缓存里的响应回给客户端，Envoy 不会再去访问上游、也就没有「响应体阶段」可谈。理解这一点能避免一个常见误读——以为「缓存命中的响应还会再被缓存一次」。

#### 4.4.2 核心流程

非流式缓存回写（`updateResponseCache`）的门槛是串行多级闸门，顺序锁定：

```
updateResponseCache(ctx, body):
  if 无 RequestID 或 body 为空:        return
  if 该请求未启用语义缓存:              return
  ① 状态码闸门: 上游非 2xx → 不缓存（避免把瞬时错误冻进缓存）
  ② 留存策略闸门 ShouldSkipCacheWrite: DSL 显式 drop → 不缓存
  ③ 个性化上下文闸门 HasPersonalizedContext: 含个性化内容 → 不缓存
  计算 TTL（decision/global 默认，可被 retention ttl_turns 覆盖）
  Cache.UpdateWithResponse(RequestID, body, TTL)
```

流式缓存回写（`cacheStreamingResponse`）多了「**重建**」一步：流式本身没有完整 JSON，需要先用累积的 `StreamingContent`/metadata/usage 拼出一个完整 ChatCompletion（`buildReconstructedStreamingResponse`），再写缓存。它复用同一套状态码/留存/个性化闸门。

路由重放回写（`attachRouterReplayResponse`）则简单得多：只要 `ctx.RouterReplayID` 非空、recorder 存在，就把响应体（流式为重建体、非流式为 finalBody）挂到重放记录上，`isFinal=true` 时再发一条 `router_replay_complete` 日志事件。

#### 4.4.3 源码精读

非流式缓存回写与三级闸门，注意顺序注释引用的 issue §2.8：

[processor_res_cache.go:21-74](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_cache.go#L21-L74) —— `updateResponseCache`：状态码闸门（[L32-39](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_cache.go#L32-L39)，注释明确「绝不缓存非 2xx，否则会把瞬时上游失败冻成 HTTP 200 冻进整个 TTL」）→ 留存闸门 → 个性化闸门 → 算 TTL → `Cache.UpdateWithResponse`。注释里写明顺序锁定为 `scope -> retention -> personalized -> TTL -> write`。

流式缓存回写与重建：

[processor_res_cache.go:81-133](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_cache.go#L81-L133) —— `cacheStreamingResponse`：先过 `validateStreamingCachePreconditions`（流未完成/被中止/无内容/缺 metadata 都跳过），再过同样三级闸门，最后 `buildReconstructedStreamingResponse` 重建后写缓存。

[processor_res_cache.go:157-231](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_cache.go#L157-L231) —— `buildReconstructedStreamingResponse`：用累积的 `StreamingContent`/`StreamingReasoning`、metadata（id/model/created/finish_reason）、usage 拼一个标准 `chat.completion` JSON。注释强调保留 `reasoning_content`，让缓存命中返回与直播流一致的推理内容。

重放回写（流式与非流式共用）：

[recorder.go:398-432](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/recorder.go#L398-L432) —— `attachRouterReplayResponse`：`RouterReplayID` 非空且 recorder 存在时，`AttachResponse` 挂响应体、合并 tool trace；`isFinal=true` 时发 `router_replay_complete` 事件。非流式在 [processor_res_body_pipeline.go:45](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body_pipeline.go#L45) 调用（`isFinal=true`），流式在 `finalizeStreamingResponse` 末尾调用。

现在看「缓存命中不进响应阶段」的硬证据——命中发生在请求阶段：

[req_filter_cache.go:158-184](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_cache.go#L158-L184) —— 缓存命中分支：`ctx.VSRCacheHit = true` 后，`createCacheHitResponse` 造响应、`updateRouterReplayStatus`、**在请求阶段就** `attachRouterReplayResponse(ctx, cachedResponse, true)`（L182），然后 `return response, true`。这个 `response` 是 `ImmediateResponse`：

[utils/http/response.go:483-497](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/utils/http/response.go#L483-L497) —— `CreateCacheHitResponse` 返回的是 `ProcessingResponse_ImmediateResponse`（HTTP 200 + 缓存 body + 一组 `x-vsr-*` 头）。`ImmediateResponse` 让 Envoy 直接把这份 body 回给客户端、**不再回调响应体阶段**。

> 对应 `RequestContext` 字段：`VSRCacheHit`（[request_context.go:164](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/request_context.go#L164)）、`RouterReplayID`（[request_context.go:253](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/request_context.go#L253)）。

#### 4.4.4 代码实践

- **实践目标**：验证「缓存命中不进响应体阶段」与「正常请求会在响应阶段回写缓存」。
- **操作步骤**：
  1. 在 `handleResponseBody` 第一行加一条 `logging.Debugf("response-body-phase entered, cacheHit=%v", ctx.VSRCacheHit)`（**示例代码**）。
  2. 在 `updateResponseCache` 的 `Cache.UpdateWithResponse` 前加一条日志。
  3. 发送两次相同（或高度相似）的非流式请求。
- **需要观察的现象**：第一次请求：进入响应体阶段（日志 1），命中 `updateResponseCache` 写缓存（日志 2）。第二次相似请求：在请求阶段就命中缓存（响应头含 `x-vsr-cache-hit: true`），**响应体阶段的日志 1 不会出现**——因为它走了 `ImmediateResponse`，根本没到响应阶段。
- **预期结果**：第二次请求的 `cacheHit=true` 日志永不打印（因为没进响应阶段），印证缓存命中的特殊路径。
- 若难以构造两次相似请求，可改为阅读 `req_filter_cache.go` 命中分支，列出它在请求阶段已经做完了哪些「本属响应阶段」的事（重放状态、重放回写）。标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么状态码闸门必须放在最前面、且绝不缓存非 2xx？

**参考答案**：若缓存了 4xx/5xx 错误响应，后续命中它的请求会被当作 HTTP 200 重放，把一次**瞬时**上游失败冻结成整个 TTL 内的「永久失败」。状态码闸门在最前能在任何重建/TTL 计算之前就把错误响应挡掉。

**练习 2**：流式缓存为什么要「重建」成完整 JSON 而不是直接存 SSE 字节流？

**参考答案**：因为缓存命中时要以**非流式可比较**的形态参与相似度检索（按查询嵌入匹配），且命中后可能以非流式形态回放给客户端。SSE 是分块流，无法直接做语义相似度。重建出完整 `chat.completion` JSON 才能进 KNN 索引并被 `ImmediateResponse` 整体重放。

**练习 3**：缓存命中的请求，其路由重放记录是在哪个阶段写入响应体的？

**参考答案**：在**请求阶段**（`req_filter_cache.go:182` 的 `attachRouterReplayResponse`），不在响应阶段。因为命中后没有响应阶段。

---

### 4.5 响应阶段插件回调：越狱、幻觉、记忆与告警

#### 4.5.1 概念说明

u3-l1 讲过插件是 decision 级的。其中有一类插件专门在**响应阶段**触发，因为它们要检查的是「**模型已经生成的回答**」而非「用户的提问」：

- **响应越狱检测（response jailbreak）**：在模型回答上跑越狱分类器，抓住那些「骗过了输入侧检测、但在输出侧暴露」的对抗内容。
- **幻觉检测（hallucination）**：检查模型回答是否含幻觉（基础版或 NLI 版）。
- **记忆存储（memory store）**：把这一轮用户消息 + 助手回答异步写进智能体记忆，供后续会话检索。
- **响应告警（response warnings）**：把上述检测的结论统一写进 `x-vsr-response-warnings` 头，或按配置把告警内联进 body。

这些回调**只在非流式路径**里以「顺序调用」的形式出现（流式路径里它们大多收束到 `finalizeStreamingResponse` 的重建/缓存环节，或干脆不在流式里做逐块检测）。理解它们的**调用顺序**与**谁能短路**是本模块的核心。

#### 4.5.2 核心流程

`handleNonStreamingResponseBody` 把非流式响应像流水线一样依次处理（顺序即源码顺序）：

```
handleNonStreamingResponseBody(body, ctx, latency, initialTransformed):
  parseResponseUsage → reportNonStreamingUsage → calibrateTokenEstimator
  updateResponseCache(ctx, body)                       # 先回写缓存
  translateResponseBodyForClient → buildInitialResponseMutations   # 出口改写
  if performResponseJailbreakDetection 返回非 nil: return 它   # 可短路（block 动作）
  if performHallucinationDetection 返回非 nil: return 它       # 可短路
  scheduleResponseMemoryStore(ctx, body)                # 异步记忆
  markUnverifiedFactualResponse(ctx)                    # 标记未验证事实
  applyResponseWarnings(...)                            # 聚合告警到头/体
  updateRouterReplayHallucinationStatus(ctx)
  attachRouterReplayResponse(ctx, finalBody, isFinal=true)
  return response
```

关键点：**越狱与幻觉检测能短路**（动作是 `block` 时直接返回拦截响应）；**缓存回写发生在检测之前**（即使后面检测出问题，这一条响应仍可能已被缓存——除非被闸门挡掉）；**记忆与告警不会短路**，只产生副作用或改写头/体。

#### 4.5.3 源码精读

非流式流水线本体，按顺序读：

[processor_res_body_pipeline.go:16-47](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body_pipeline.go#L16-L47) —— `handleNonStreamingResponseBody`：依次 `reportNonStreamingUsage` → `calibrateTokenEstimator` → `updateResponseCache` → `translateResponseBodyForClient` + `buildInitialResponseMutations` → 越狱检测 → 幻觉检测 → `scheduleResponseMemoryStore` → `markUnverifiedFactualResponse` → `applyResponseWarnings` → `updateRouterReplayHallucinationStatus` → `attachRouterReplayResponse`。注意两个 `if ... != nil { return }` 就是短路点。

响应越狱检测——它对**模型回答**做检测：

[res_filter_jailbreak.go:18-45](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/res_filter_jailbreak.go#L18-L45) —— `performResponseJailbreakDetection`：注释说明它「在 LLM 响应体上跑越狱分类器，抓那些骗过输入侧检测的对抗内容」。配置取自 decision 的 `GetResponseJailbreakConfig`，阈值缺省回退到全局 `PromptGuard.Threshold` 再到 0.5。动作是 `block` 时返回拦截响应，否则返回 nil 并置 `ctx.ResponseJailbreakDetected`。

幻觉检测——可短路但通常放行：

[res_filter_hallucination.go:19-45](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/res_filter_hallucination.go#L19-L45) —— `performHallucinationDetection`：按 decision 是否启用 NLI 选择基础或 NLI 检测。注释说明它「返回 nil 放行响应，告警在 processor_res_body.go 处理」，即幻觉通常不 block，而是转成告警。

异步记忆存储——用 `goSafely` 包 goroutine：

[processor_res_memory.go:9-61](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_memory.go#L9-L61) —— `scheduleResponseMemoryStore`：判定 `autoStore` 开启、`MemoryExtractor` 存在、且**响应越狱未检出**才存；提取本轮用户消息与助手回答，用 `goSafely`（带 `defer recover`）异步调 `ProcessResponseWithHistory`。注释引用 issue #1843：用 recover 包裹是为了让记忆路径的 panic 不至于拖垮整个 router 进程。

响应告警聚合——把多种告警汇进一个头：

[processor_res_body_pipeline.go:130-162](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body_pipeline.go#L130-L162) —— `applyResponseWarnings`：按固定顺序收集 `HallucinationDetected`、`UnverifiedFactualResponse`、越狱 三类告警 code，写进 `x-vsr-response-warnings` 头（逗号分隔，顺序确定）；`body` 动作的告警会内联改写 body。注释引用 #2204，说明每条告警可选 `header`（默认，仅加 code）/`body`（内联改写）/`none`（抑制）。

[processor_res_body_pipeline.go:173-194](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body_pipeline.go#L173-L194) —— `setResponseWarningsHeader`：把逗号拼接的 codes 写进 `x-vsr-response-warnings`（`headers.VSRResponseWarnings`），与已有 `HeaderMutation` 合并。

#### 4.5.4 代码实践

- **实践目标**：理解「检测可短路、告警不改流」的边界。
- **操作步骤**：
  1. 阅读 `handleNonStreamingResponseBody`，标出哪些调用「可能 return 提前结束」、哪些「只产生副作用」。
  2. 在一条配置了响应越狱检测（action=`block`）的 decision 上，构造一个会触发越狱的模型回答（或阅读其单元测试断言）。
  3. 在一条配置了幻觉告警（action=`header`）的 decision 上，构造一个会触发幻觉的回答。
- **需要观察的现象**：越狱 `block` 时，客户端收到的是拦截响应（`performResponseJailbreakDetection` 的返回值），后续记忆/告警/重放**不再执行**；幻觉 `header` 时，响应正常返回，但多了 `x-vsr-response-warnings: <code>` 头。
- **预期结果**：印证「block 类插件短路流水线，warning 类插件只加头/体不改流」。
- 若难以触发真实检测，改为阅读 `res_filter_jailbreak.go` / `res_filter_hallucination.go` 后续未贴出的分支，说明 `block` 与非 `block` 动作的返回差异。标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`updateResponseCache` 在越狱检测**之前**调用。若一个响应后来被越狱检测判定为 `block`，它是否已经被缓存？

**参考答案**：可能已经被缓存（只要它过了状态码/留存/个性化闸门）。也就是说，缓存回写在检测之前。但被 `block` 的响应通常内容异常，是否真正落缓存取决于闸门；设计上把缓存前置是为了让「正常但触发告警」的响应也能被缓存复用，而真正恶意的由闸门与配置（如 retention/个性化）把关。

**练习 2**：`scheduleResponseMemoryStore` 为什么用 `goSafely` 异步执行，且在「响应越狱检出」时跳过？

**参考答案**：异步是为了不阻塞响应返回给客户端（记忆写入慢且非关键路径）；`goSafely` 带 recover 防止记忆路径 panic 拖垮进程。越狱检出时跳过，是为了不把对抗性内容写进长期记忆污染后续检索。

**练习 3**：三类告警 code 的拼接顺序为什么要在源码里「固定」？

**参考答案**：为了让 `x-vsr-response-warnings` 头的值是**确定性**的（同样的检测结果产生同样的头串），便于客户端解析、测试断言与缓存键稳定。源码用固定顺序的 `appendNonEmpty` 保证这一点。

---

## 5. 综合实践

把本讲五条主线串起来，做一次「**响应体阶段全链路追踪**」。

**任务**：选一个非流式请求和一个流式请求，各自画出从 `handleResponseBody` 进入到返回的完整调用图，标注每一步的「分支判断 / 副作用 / 是否短路」。

建议步骤：

1. **准备**：确保 SR 已启用语义缓存，并配了至少一个 decision 级响应插件（如幻觉告警）。
2. **非流式追踪**：发起一个普通 `/v1/chat/completions`（非 `stream`）请求，对照 4.1→4.2→4.4→4.5，在 `handleResponseBody`、`normalizeProviderResponseBody`、`handleNonStreamingResponseBody`、`updateResponseCache`、`translateResponseBodyForClient`、`performHallucinationDetection`、`applyResponseWarnings`、`attachRouterReplayResponse` 各入口加临时日志，记录它们的执行顺序与返回值。
3. **流式追踪**：发起一个 `stream: true` 请求，对照 4.3，记录 `handleStreamingResponseBody` 被调用的次数、每次 `parseStreamingChunk` 后 `StreamingContent` 的累积长度、`recordStreamingTTFT` 仅首块生效、`finalizeStreamingResponse` 恰好一次。
4. **缓存命中对照**：再次发起与第 2 步相似的请求，观察它**不进入** `handleResponseBody`（响应头含 `x-vsr-cache-hit: true`，且响应阶段的日志全部缺失）。
5. **产出**：画两张调用图（非流式 / 流式），并用一段话解释「为什么缓存命中请求没有响应阶段日志」。

> 任何无法在本机复现的步骤，请明确标注「待本地验证」，不要伪造日志输出。

## 6. 本讲小结

- `handleResponseBody` 是个**调度员**：两道早退闸门（SkipProcessing / Looper）+ 有严格顺序的分支（新 Anthropic 客户端流式 > 旧 Anthropic 后端流式 > 归一化 > 流式/非流式二分）。分支顺序由「双重 Anthropic 象限」决定，不能乱。
- **Provider 归一化**走「进站翻成 OpenAI、出站翻成客户端协议」的双向模式；只有真改写时才生成 body mutation 并删 `content-length`，多数请求零开销透传。
- **流式处理**是「逐块累积 + 末尾收尾」：首块记 TTFT，每块解析累积 content/reasoning/toolcall，看到 `[DONE]` 调一次幂等的 `finalizeStreamingResponse` 完成指标、在途、缓存与重放；Anthropic 客户端路径下 SR 完全接管线路，每次返回必须替换 body。
- **缓存回写**有三级顺序锁定的闸门（状态码 → 留存 → 个性化），流式需先重建完整 JSON 再写；**重放回写**在非流式末尾与流式收尾各做一次。
- **关键事实**：缓存命中在请求阶段用 `ImmediateResponse` 回包，**不进入响应体阶段**，其重放回写也在请求阶段完成。
- **响应阶段插件回调**（越狱/幻觉/记忆/告警）按固定顺序在非流式流水线里执行；越狱与幻觉可短路（`block`），记忆异步且遇越狱跳过，告警统一汇入 `x-vsr-response-warnings` 头。

## 7. 下一步学习建议

- 本讲多次提到「在途计数」「TTFT 与缓存热度」——它们是 u11-l3（限流、在途、延迟与授权）的主题，建议接着读 `pkg/inflight` 与 `pkg/latency`，理解响应阶段 `inflight.End` 与 `latency.UpdateTTFT` 如何反哺调度。
- 缓存回写的多后端实现（内存 HNSW / Milvus / Qdrant / Redis / Valkey）在 u9-l3（语义缓存）展开；本讲只讲了「何时写」，那里讲「写到哪、怎么查」。
- 响应阶段的越狱/幻觉检测背后是分类信号系统，u8-l3（PII 与越狱检测）会深入越狱检测的 BERT + 对比学习模型。
- 多 provider 互转的细节（`ToOpenAIResponseBodyWithExt` / `EmitAnthropicResponse` / Response API 翻译）属 u10-l4（多后端适配），可据需深入。
- 想看完整的「请求→响应」端到端契约，可阅读 `e2e/testcases`（u14-l1），那里有真实 kind 集群对流式/非流式/缓存命中的断言。
