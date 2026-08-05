# 请求体处理与入口解析

## 1. 本讲目标

本讲是「请求处理主链路」的第一篇。在前置讲义 u4-l3 中，我们已经知道 Envoy 通过 ExtProc gRPC 把请求按 `请求头 → 请求体 → 响应头 → 响应体` 四个阶段喂给 `OpenAIRouter.Process`，并在请求体阶段经 `handleRequestBodyDispatch` 进入经典管线。本讲要回答的问题是：**一条 `/v1/chat/completions` 请求的 body 字节，是如何被解析成可路由的状态，又如何决定它走哪一条配方（recipe）的？**

读完本讲，你应当能够：

- 说清楚请求体从原始字节到 `ctx.RequestModel` / `ctx.UserContent` 的解析路径，以及为什么 SR 用 gjson 做「快速抽取」而不是一上来就 `json.Unmarshal`。
- 区分三种请求模型名（auto 别名、entrypoint 虚拟名、具体后端模型）以及它们对应的两种路由边界：**走配方（recipe）** 还是 **直通（passthrough）**。
- 描述「信号输入构建」如何把快速抽取的结果转成 `signalConversationHistory`，作为后续决策求值的原料。

本讲只讲到「进入决策求值之前的准备」，决策引擎内部的布尔规则求值留到 u5-l2，模型选择与 body 改写留到后续讲义。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，ExtProc 是「流式插队」而不是「转发代理」。** Envoy 把请求体以 `ProcessingRequest_RequestBody` 消息的形式发给 SR；SR 处理完后回一个 `ProcessingResponse`，告诉 Envoy「继续放行（CONTINUE，可附带头部/体改写）」或「直接回包（ImmediateResponse，如缓存命中、限流拒绝）」。也就是说，SR 看到的请求体是 Envoy 递过来的字节，而不是自己从 socket 读的。

**第二，请求里的 `model` 字段是一个「路由选择器」，未必是真模型名。** OpenAI 协议规定请求体必须有 `model` 字段，但 SR 把它复用成了「入口名」：

- `vllm-sr/auto`、`auto`、`MoM` 这类**auto 别名**表示「让路由器自己挑模型」；
- `entrypoints` 里登记的**虚拟名**（如 `balance`）表示「走某个命名配方」；
- 像 `Qwen/Qwen2.5-14B-Instruct` 这种**具体后端模型**则表示「客户端钦定了模型，别路由，直接转发」。

同一个 `model` 字符串，在 SR 里命运完全不同——这正是本讲要讲清的核心区分。

**第三，「解析」分两遍，第一遍快、第二遍全。** 大请求体（如 64KB 历史）做一次完整的 `json.Unmarshal` 进 OpenAI SDK 结构体很贵；而缓存命中、限流、fast_response 这些「早退」路径根本不需要完整解析。所以 SR 先用 gjson（一个按路径取值的 JSON 库）做一次轻量抽取，只有真正需要改写 body 时才做完整解析。这是一个贯穿请求体阶段的性能主线。

> 术语提示：**entrypoint**（入口）是 config 里把客户端可见的虚拟模型名绑定到某个 recipe 的映射；**recipe**（配方）是一个隔离的路由命名空间，里面装着自己的 signals/projections/decisions；**passthrough**（直通）表示请求钦定了具体后端模型，绕过配方路由。这三个词在本讲会反复出现。

## 3. 本讲源码地图

本讲涉及的文件都集中在 `src/semantic-router/pkg/extproc/`（请求体处理）和 `src/semantic-router/pkg/config/`（入口/配方解析）：

| 文件 | 作用 |
| --- | --- |
| `pkg/extproc/processor_req_body.go` | 请求体处理总入口 `handleRequestBody`，串起解析→入口解析→预路由→模型路由。 |
| `pkg/extproc/processor_req_body_prepare.go` | 快速抽取 `extractFastRequestState`、协议分发、预路由阶段 `runRequestPreRoutingStages`、完整解析 `prepareRequestForModelRouting`。 |
| `pkg/extproc/processor_req_body_validation.go` | 请求体形态校验 `validateRequestBody`。 |
| `pkg/extproc/utils_fast.go` | gjson 快速抽取实现 `extractContentFast` 与结果结构 `FastExtractResult`。 |
| `pkg/extproc/req_filter_entrypoint.go` | 入口/配方解析 `resolveEntrypointForRequest` 与配方作用域辅助方法。 |
| `pkg/extproc/request_routing_context.go` | 路由边界状态机 `RequestRoutingContext`（recipe vs passthrough）。 |
| `pkg/extproc/request_signal_history.go` | 信号输入构建 `signalConversationHistoryFromFastExtract`。 |
| `pkg/extproc/request_context.go` | 请求级状态容器 `RequestContext`（随消息推进累积）。 |
| `pkg/extproc/processor_core.go` | ExtProc 分发层 `handleRequestBodyDispatch` / `processRequestBody`。 |
| `pkg/config/recipes.go` | 入口表与配方解析 `RecipeForRoutingModel` / `RecipeForRequestModel`。 |
| `pkg/config/helper.go` | auto 别名判定 `IsAutoModelName` 与默认别名常量。 |

## 4. 核心概念与源码讲解

### 4.1 请求体解析

#### 4.1.1 概念说明

「请求体解析」要解决的问题是：Envoy 递过来的是一段原始 JSON 字节（`[]byte`），而 SR 后续要做分类、决策、缓存查找、模型改写，这些都至少需要知道**请求里写了什么 model、用户说了什么话、是不是流式**。

朴素做法是直接 `json.Unmarshal` 进 OpenAI SDK 的 `ChatCompletionNewParams` 结构体。问题是这个结构体很大、嵌套很深，对大请求体的解析开销惊人（源码注释里给了一个量级：64KB body 完整解析约 300ms，而 gjson 路径抽取约 1ms）。更要命的是，**很多请求根本走不到需要完整结构体的地方**——命中语义缓存、被限流拒绝、命中 fast_response，这些「早退」路径只需知道 model 和用户文本就够了。

于是 SR 采用「**快速抽取优先，完整解析延迟**」策略：先用 gjson 按字段路径抽出路由必需的少量字段，只有当真要改写 body（模型路由、记忆注入、模态路由）时才做完整解析。这是一个用「按需付费」换延迟的典型设计。

#### 4.1.2 核心流程

`handleRequestBody` 是请求体阶段的主函数，它把一次请求切成一条线性流水线：

```
Envoy RequestBody 字节
  │
  ├─ 0. ctx.SkipProcessing ?            → 直接 CONTINUE（头部阶段已探测的 opt-out）
  ├─ 1. translateResponseAPIRequest     → 若是 /v1/responses，先把 body 翻译成 OpenAI 形态
  ├─ 2. validateRequestBody             → 形态校验（JSON 合法？messages 存在？）
  ├─ 3. extractFastRequestState         → gjson 快速抽取 model/stream/用户文本/图片/会话形状
  ├─ 4. ctx.RequestModel = fast.Model   → 记下原始 model 名（供后续路由判定）
  ├─ 5. isLooperRequest ?               → 内部回环请求，单独处理
  ├─ 6. ctx.UserContent / RequestImageURL = fast.*   → 暂存用户文本与首图
  ├─ 7. runRequestPreRoutingStages      → 【入口解析 + 信号构建 + 决策求值 + 早退检查】（见 4.2 / 4.3 / u5-l2）
  ├─ 8. prepareRequestForModelRouting   → 完整解析 + 记忆/模态处理
  └─ 9. handleModelRouting              → 模型选择与 body 改写（后续讲义）
```

第 0、1、2 步是「守卫」，第 3、4、6 步是本模块的重点（解析），第 7 步是下一模块（入口解析）与 4.3（信号构建）的交汇点，第 8、9 步本讲只点到为止。

#### 4.1.3 源码精读

先看主函数全貌，注意每一步的早退（`return earlyResponse`）都是一条独立的「短路」路径：

[processor_req_body.go:31-99](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_req_body.go#L31-L99) —— `handleRequestBody` 全流程：先把字节存进 `ctx.OriginalRequestBody`，再依次跑「跳过处理 → Response API 翻译 → 校验 → 快速抽取 → 记 model → looper → 记 user content → 预路由 → 完整解析 → 模型路由」。每一步都可能提前返回。

第 3 步快速抽取的封装在 `extractFastRequestState`，它先按入站协议分发，再把抽取出来的 `Stream` 标志写回上下文：

[processor_req_body_prepare.go:49-67](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_req_body_prepare.go#L49-L67) —— `extractFastRequestState`：调用 `extractRequestSignalsForProtocol` 抽取；解析失败按 `codes.InvalidArgument` 记一个 `parse_error` 指标；若 `fast.Stream` 为真则置 `ctx.ExpectStreamingResponse = true`。

协议分发很简单——OpenAI 是默认，Anthropic `/v1/messages` 用理解其 content-block 形态的变体，但下游拿到的 `FastExtractResult` 契约是一样的：

[processor_req_body_prepare.go:74-81](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_req_body_prepare.go#L74-L81) —— `extractRequestSignalsForProtocol` 按 `ctx.ClientProtocol` 在 `extractContentFastAnthropic` 与默认的 `extractContentFast` 之间二选一。

快速抽取的核心是 `extractContentFast`，它用 gjson 直接按路径取值，**不分配完整 SDK 结构体**：

[utils_fast.go:51-69](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/utils_fast.go#L51-L69) —— `extractContentFast`：先抽 model/stream（`extractModelAndStreamFast`），再抽 metadata，最后遍历 `messages` 数组填充用户文本/图片/会话形状，并统计 tools 定义数。model 缺失会直接报 `errMissingModel`。

model 与 stream 的抽取只取两个字段，足以驱动后续路由判定与流式检测：

[utils_fast.go:139-158](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/utils_fast.go#L139-L158) —— `extractModelAndStreamFast`：`model` 不存在报错、类型非 string 报类型错；`stream` 不存在视为假，存在则必须是布尔。

快速抽取的产出全部装进 `FastExtractResult`，这是后续信号构建（4.3）的原料：

[utils_fast.go:20-45](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/utils_fast.go#L20-L45) —— `FastExtractResult` 结构：除了 `Model/Stream/UserContent/FirstImageURL/Metadata`，还携带大量「会话形状」计数字段（`UserMessageCount`、`AssistantToolCallCount`、`LastMessageRole` 等），它们喂给 conversation 信号族。

会话形状的统计发生在遍历 messages 时，按 role 分流累加各类计数：

[utils_fast.go:167-196](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/utils_fast.go#L167-L196) —— `consumeFastExtractMessage`：按 role（user/system/assistant/developer/tool）分别累加计数，记录最后一条消息的角色与是否为 tool 结果，并捕获用户文本与首图。

> **为什么抽这么细？** 这些计数字段（如「上一条是 tool 结果后紧跟一条 user」`LastUserAfterToolResult`）是 conversation 信号族的判定依据——它们让路由器能识别「这是一个多轮工具调用会话」并据此选模型。这些字段在 4.3 会再次出现。

解析前的守卫之一是形态校验，它用 gjson 做最小必要检查，**不重复 SDK 解析器的逐块校验**：

[processor_req_body_validation.go:26-55](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_req_body_validation.go#L26-L55) —— `validateRequestBody`：先用 `gjson.ValidBytes` 拒掉非法 JSON；Response API 与 Anthropic 协议各有分支；对 OpenAI 协议，仅当路径是 `/v1/chat/completions`（或空）时检查 `messages` 必须存在且为数组。

注意校验函数对路径的判定：`path != "" && path != "/v1/chat/completions"` 时直接放行——非 chat 路径（如 embeddings）不做 messages 检查。这说明校验是「**只挡会让后续管线无定义的形状错误**」，而不是完整 schema 校验。

第 4 步记下原始 model 名，是本模块与下一模块的衔接点：

[processor_req_body.go:60-63](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_req_body.go#L60-L63) —— 把快速抽取到的 `fast.Model`（去空白）作为 `originalModel`，并在 `ctx.RequestModel` 为空时写入；这个 `originalModel` 接下来就是「入口解析」的输入。

#### 4.1.4 代码实践

**实践目标**：跟踪一次 `/v1/chat/completions` 请求，看清 body 字节如何变成 `ctx.RequestModel` 与 `ctx.UserContent`。

**操作步骤**：

1. 在仓库根目录找到请求体处理的 Go 源文件，在 `handleRequestBody` 的第 4 步前后（即 `originalModel := strings.TrimSpace(fast.Model)` 这一行附近）加一行临时日志（**仅本地学习用，勿提交**）：
   ```go
   logging.ComponentDebugEvent("extproc", "trace_body_parse", map[string]interface{}{
       "request_id":    ctx.RequestID,
       "parsed_model":  fast.Model,
       "user_content_len": len(fast.UserContent),
       "image_count":   fast.ImageContentCount,
   })
   ```
2. 按 u1-l3 / u1-l4 学过的本地运行方式起一套服务（`vllm-sr serve` 或对应 make 目标），确保 router 跑起来。
3. 用 `curl` 向 envoy 入口（默认 8801）发一个最小 chat 请求：
   ```bash
   curl -s http://localhost:8801/v1/chat/completions \
     -H 'content-type: application/json' \
     -d '{"model":"vllm-sr/auto","messages":[{"role":"user","content":"用一句话解释 HNSW"}]}'
   ```
4. 再发一个 **故意缺 model 字段** 的请求，观察返回：
   ```bash
   curl -s http://localhost:8801/v1/chat/completions \
     -H 'content-type: application/json' \
     -d '{"messages":[{"role":"user","content":"hi"}]}'
   ```

**需要观察的现象**：

- 第 3 步应在 router 日志里看到 `trace_body_parse` 事件，`parsed_model` 为 `vllm-sr/auto`，`user_content_len` 大于 0。
- 第 4 步应在响应里看到一个 400 错误，提示缺少 model 字段——这正是 `extractModelAndStreamFast` 抛出的 `errMissingModel` 经 `validationResponseFromRequestError` 转成的 `createErrorResponse(400, ...)`。

**预期结果**：你能复现「model 缺失 → 400」这条短路路径，并验证快速抽取确实先于完整解析发生（因为缺 model 时根本走不到完整解析）。

> 若本地无法完整起服，可退化为「源码阅读型实践」：在 `handleRequestBody` 里数清有几处 `if ... != nil { return ... }` 早退点，并说出每一条早退分别对应什么场景（跳过处理、Response API 翻译失败、校验失败、抽取失败、looper、预路由早退、完整解析失败）。这些早退点的存在，正是「快速抽取优先」策略能省算力的原因。运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 SR 不在请求体阶段一上来就 `json.Unmarshal` 进完整 SDK 结构体？

> **答案**：因为很多请求会走早退路径（缓存命中、限流、fast_response、looper），这些路径只需要 model 和用户文本就够了；完整解析大请求体开销很大（注释给出 64KB 约 300ms vs gjson 约 1ms）。所以 SR 用 gjson 做轻量「快速抽取」，把昂贵的完整解析延迟到真要改写 body 时（`prepareRequestForModelRouting`）。

**练习 2**：`extractContentFast` 在 `model` 字段缺失时会怎样？这个错误最终如何变成 HTTP 响应？

> **答案**：`extractModelAndStreamFast` 返回 `errMissingModel`；`extractFastRequestState` 把它包成 `status.Errorf(codes.InvalidArgument, ...)` 返回；`handleRequestBody` 经 `validationResponseFromRequestError` 识别出 `InvalidArgument` 码，转成 `createErrorResponse(400, ...)`，最终作为 400 响应回给客户端。

**练习 3**：`FastExtractResult` 里那些 `UserMessageCount`、`AssistantToolCallCount` 之类的计数字段是给谁用的？

> **答案**：它们是 conversation 信号族的判定输入，用来识别会话形状（如「多轮工具调用」「最后一条是 tool 结果」）。这些字段会被搬进 `signalConversationHistory`（见 4.3），供决策求值前的信号抽取使用。

---

### 4.2 入口/配方解析

#### 4.2.1 概念说明

「入口解析」要解决的问题是：拿到了 `originalModel` 之后，**这条请求该走哪条配方（recipe），还是根本不该走配方？**

回忆 u3-l1：config.yaml 的 `entrypoints` 段把客户端可见的虚拟模型名绑定到命名 recipe，顶层 `routing` 本身就是 `default` 配方；recipe 内部的 signals/projections/decisions 名字是**局部**的，不能跨 recipe 引用。所以路由器必须先确定「这条请求属于哪个 recipe」，否则决策引擎根本不知道该拿哪一组 decisions 去求值。

请求模型名分三类，对应两种路由边界：

| 模型名类型 | 例子 | 路由边界 |
| --- | --- | --- |
| **auto 别名** | `vllm-sr/auto`、`auto`、`MoM` | 走 `default` recipe（让路由器自己挑模型） |
| **entrypoint 虚拟名** | `balance`、`accuracy`（`entrypoints` 里登记的） | 走该名字映射到的 recipe |
| **具体后端模型** | `Qwen/Qwen2.5-14B-Instruct` | **passthrough**（绕过配方路由，钦定转发） |

关键区分在「走配方」与「直通（passthrough）」：前者会触发该 recipe 的信号抽取、投影、决策求值；后者钦定了具体后端模型，绕过 recipe-local 的信号/决策/插件，只用共享的 provider/backend 基础设施。这两条路径在代码里由 `RequestRoutingContext` 的三态枚举严格区分，避免「用一个 nil 指针既表示直通又表示未解析」的歧义。

#### 4.2.2 核心流程

入口解析发生在预路由阶段的最开头，**先于任何信号求值**：

```
runRequestPreRoutingStages(originalModel, fast, ctx)
  │
  ├─ resolveEntrypointForRequest(originalModel, ctx)   ← 本模块核心
  │     └─ Config.RecipeForRoutingModel(model)
  │           ├─ 是 auto/remom/fusion/flow 别名？  → DefaultRecipe()
  │           ├─ 是 entrypoint 虚拟名？            → RecipeByName(entrypoint.Recipe)
  │           └─ 都不是（具体后端模型）？          → 返回 (nil, false)
  │     ├─ 命中 recipe：ctx.Routing.SelectRecipe(recipe)
  │     └─ 未命中：    ctx.Routing.SelectPassthrough()
  ├─ populatePinnedSessionFromHeaders(ctx)
  ├─ signalConversationHistoryFromFastExtract(fast)    ← 4.3
  └─ performDecisionEvaluation(...)                    ← u5-l2
```

一旦 `ctx.Routing` 被设成 `routingRecipe` 或 `routingPassthrough`，后续所有「这条请求走哪组 decisions」「用哪个 classifier」都从它派生。这是一个「**先定性、再求值**」的设计：必须先确定 recipe 边界，决策引擎才有正确的 candidates 集合。

#### 4.2.3 源码精读

入口解析的入口函数极其简短，但它把「定性」逻辑全委托给了 config 层：

[req_filter_entrypoint.go:13-28](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_entrypoint.go#L13-L28) —— `resolveEntrypointForRequest`：调 `Config.RecipeForRoutingModel(originalModel)`；命中则 `ctx.Routing.SelectRecipe(recipe)` 并打一条 `entrypoint_recipe_resolved` 事件；未命中则 `ctx.Routing.SelectPassthrough()`。注意它在任何信号求值之前执行。

判定逻辑在 config 层，按「auto 别名优先，否则查入口表」的顺序：

[recipes.go:185-195](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/recipes.go#L185-L195) —— `RecipeForRoutingModel`：auto/remom/fusion/flow 别名都解析到 `DefaultRecipe()`；否则委托 `RecipeForRequestModel`。具体后端模型 ID 故意解析失败（返回 false），让调用方走 passthrough。

入口表查找就是遍历 `entrypoints`，看模型名是否在某条映射的 `ModelNames` 里：

[recipes.go:165-179](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/recipes.go#L165-L179) —— `RecipeForRequestModel`：trim 后遍历 `c.Entrypoints`，用 `slices.Contains` 匹配 `ModelNames`，命中则返回该 entrypoint 指向的 recipe。空名直接返回 false。

入口表本身的数据结构很朴素——一组虚拟名 + 一个 recipe 名：

[recipes.go:58-61](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/recipes.go#L58-L61) —— `EntrypointMapping`：`ModelNames []string` 绑定到一个 `Recipe RecipeName`。注释强调这些虚拟名**永不到达后端**，只用来选路由画像。

auto 别名的判定依赖一份可配置的别名清单，缺省时保留新旧兼容：

[helper.go:124-130](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/helper.go#L124-L130) —— `IsAutoModelName`：模型名是否在 `EffectiveAutoModelNames()` 里。

[helper.go:12-14](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/helper.go#L12-L14) —— 默认 auto 别名常量：`MoM`、`auto`、`vllm-sr/auto` 三者并存，分别对应历史名、旧别名、新命名空间别名。

「走配方 vs 直通」的状态机由 `RequestRoutingContext` 用一个三态枚举表达，这是为了避免「nil 既表示直通又表示未解析」的歧义：

[request_routing_context.go:5-19](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/request_routing_context.go#L5-L19) —— `requestRoutingResolution` 三态枚举（`routingUnresolved`/`routingRecipe`/`routingPassthrough`）与 `RequestRoutingContext` 结构：把「解析结果」与「recipe 指针」分开存。

两个 setter 分别记录两种边界：

[request_routing_context.go:22-42](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/request_routing_context.go#L22-L42) —— `SelectRecipe` 记录一条显式 recipe 解析（nil 时退回 unresolved）；`SelectPassthrough` 记录「请求钦定了具体后端模型」，清空 recipe 指针。

读侧的访问器严格按枚举返回，确保「直通」不会被误读成「有一条空 recipe」：

[request_routing_context.go:48-65](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/request_routing_context.go#L48-L65) —— `SelectedRecipe` 仅在 `routingRecipe` 态返回非 nil；`IsPassthrough` 仅在 `routingPassthrough` 态为真。这把「未解析」「走配方」「直通」三者区分得毫无歧义。

这个 `Routing` 字段就挂在请求上下文上，是整条请求「路由边界」的唯一真相源：

[request_context.go:142-144](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/request_context.go#L142-L144) —— `RequestContext.Routing` 注释明确写道：「已解析但无 recipe 的上下文代表具体模型直通」。

「走哪组 decisions」正是从 `Routing` 派生的——这是入口解析影响决策求值的方式：

[req_filter_entrypoint.go:62-77](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_entrypoint.go#L62-L77) —— `decisionCandidatesForRequest`：从 `ctx.Routing.SelectedRecipe()` 取该 recipe 的 `Profile.Decisions`；recipe 没有 decisions 时返回**空非 nil 切片**，刻意阻止决策引擎回退到 default profile 的 decisions（守住 recipe 隔离边界）。

同理，「用哪个 classifier」也从 `Routing` 派生，且命名 recipe 永不跨边界回退：

[req_filter_entrypoint.go:30-48](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_entrypoint.go#L30-L48) —— `classifierForRequest`：从 `RecipeClassifiers` 按 recipe 名取分类器；命名 recipe 取不到时返回 nil（不回退到 default），只有 default recipe 才回退到 `r.Classifier`。

#### 4.2.4 代码实践

**实践目标**：用同一份 config，观察三种 `model` 名分别解析出哪种路由边界。

**操作步骤**：

1. 打开 `config/recipes/multi-objective/config.yaml`（u3-l2 学过的多入口配方），找到它的 `entrypoints` 段，记下其中登记的几个虚拟名及其指向的 recipe。
2. 阅读本讲的 `resolveEntrypointForRequest` 与 config 层的 `RecipeForRoutingModel`，画一张决策表：给定 `originalModel` 字符串 → 命中哪条分支 → `ctx.Routing` 最终处于哪个枚举态。
3. （选做，**待本地验证**）起服后分别用三种 model 名发请求，在日志里找 `entrypoint_recipe_resolved` 事件：
   - `vllm-sr/auto`（auto 别名）
   - multi-objective 里登记的某个虚拟名（entrypoint）
   - 一个 config 里真实存在的具体后端模型名（passthrough）

**需要观察的现象**：

- auto 别名与 entrypoint 虚拟名都应触发 `entrypoint_recipe_resolved`，但 `recipe` 字段不同（auto 恒为 `default`，entrypoint 为其映射的 recipe 名）。
- 具体后端模型名**不应**触发该事件，因为 `resolveEntrypointForRequest` 走的是 `SelectPassthrough` 分支，只打 debug 日志不打 resolved 事件。

**预期结果**：你能清楚地说出「同一个 `model` 字段，因为属于不同类别，会落到 recipe 路由或 passthrough 两条完全不同的路径上」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `RequestRoutingContext` 要用三态枚举（unresolved/recipe/passthrough），而不是简单地用一个 `*RoutingRecipe` 指针（nil 表示直通）？

> **答案**：因为 nil 指针无法区分「还没解析」和「解析成了直通」两种情况。用三态枚举把 `routingPassthrough`（钦定具体模型，绕过配方）与 `routingUnresolved`（还没跑入口解析）显式分开，可以避免下游误把「直通」当成「有一条空 recipe」处理，也避免误把「未解析」当成「直通」放行。

**练习 2**：一条请求的 model 是 `balance`（假设它登记在 `entrypoints` 里指向 `balance` recipe），另一条的 model 是 `Qwen/Qwen2.5-14B-Instruct`。它们的 `ctx.Routing.SelectedRecipe()` 分别返回什么？这如何影响后续决策求值？

> **答案**：前者返回 `balance` recipe 指针（`routingRecipe` 态），决策引擎用 `balance` recipe 的 `Profile.Decisions` 求值；后者返回 nil（`routingPassthrough` 态），`decisionCandidatesForRequest` 返回空切片，决策引擎没有 candidates，请求作为钦定模型直通，绕过 recipe-local 信号/决策/插件。

**练习 3**：`decisionCandidatesForRequest` 在「recipe 存在但没有 decisions」时为什么返回**空非 nil**切片而不是 nil？

> **答案**：为了守住 recipe 隔离边界。如果返回 nil，决策引擎可能会把它当成「没指定 candidates」而回退到 default profile 的 decisions，导致跨 recipe 误引用。返回空非 nil 切片明确表示「这个 recipe 就是没有任何 decisions」，让决策引擎在该 recipe 内空转而不泄漏到别的 recipe。

---

### 4.3 信号输入构建

#### 4.3.1 概念说明

「信号输入构建」要解决的问题是：决策引擎不会自己读 JSON——它需要一个**结构化、协议无关**的输入。快速抽取产出的 `FastExtractResult` 正好是这份原料，但它是个「抽取结果」结构，而决策引擎想要的是「会话历史」。于是需要一个搬运层，把 `FastExtractResult` 转成 `signalConversationHistory`。

这一步的意义在于**解耦**：上游的 gjson 抽取只关心「从 JSON 里抠字段」，不关心信号语义；下游的决策引擎只关心「会话长什么样」，不关心字段是从 OpenAI 还是 Anthropic 协议抠出来的。中间这层搬运让两端各自演化而不互相牵扯。这也是为什么 4.1.1 强调「OpenAI 与 Anthropic 抽取变体产出同一份 `FastExtractResult` 契约」——有了统一契约，信号构建就可以对协议无感。

需要强调：本模块只讲「信号输入是怎么备好的」，不讲信号如何被求值、如何投影、如何进决策——那些是 u5-l2（决策求值管线）与 u8（分类信号系统）的内容。

#### 4.3.2 核心流程

信号输入构建夹在「入口解析」与「决策求值」之间，是 `runRequestPreRoutingStages` 的第三步：

```
runRequestPreRoutingStages:
  resolveEntrypointForRequest(...)            ← 4.2：先定性走哪个 recipe
  populatePinnedSessionFromHeaders(ctx)       ← 从头部取会话钉扎信息
  history := signalConversationHistoryFromFastExtract(fast)   ← 本模块：备信号输入
  performDecisionEvaluation(originalModel, history, ctx)      ← u5-l2：信号求值 + 决策
```

数据流向是单向的：

```
原始 JSON 字节
  → extractContentFast → FastExtractResult（4.1）
  → signalConversationHistoryFromFastExtract → signalConversationHistory（本模块）
  → performDecisionEvaluation → 信号抽取/投影/决策（u5-l2/u8）
```

注意 `history` 只携带「会话文本与形状」，不携带 `model`——`model` 由 `originalModel` 单独传给 `performDecisionEvaluation`。这是一种「文本原料」与「路由选择器」分轨传递的设计。

#### 4.3.3 源码精读

先看搬运发生在 `runRequestPreRoutingStages` 的哪一行，这能帮你定位整条预路由链：

[processor_req_body_prepare.go:101-113](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_req_body_prepare.go#L101-L113) —— `runRequestPreRoutingStages` 开头：先 `resolveEntrypointForRequest`（4.2），再 `populatePinnedSessionFromHeaders`，然后用 `signalConversationHistoryFromFastExtract(fast)` 构建会话历史，最后把它与 `originalModel` 一起喂给 `performDecisionEvaluation`。

搬运函数本身是一个逐字段拷贝（含切片深拷贝），把抽取结果转成内部会话历史结构：

[request_signal_history.go:32-56](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/request_signal_history.go#L32-L56) —— `signalConversationHistoryFromFastExtract`：把 `FastExtractResult` 的字段逐个搬进 `signalConversationHistory`，对 `PriorUserMessages`/`NonUserMessages`/`AssistantToolNames`/`metadata` 做了 `append([]string(nil), ...)` 深拷贝，避免共享底层数组被后续误改。

目标结构 `signalConversationHistory` 与 `FastExtractResult` 字段几乎一一对应，但它是包内私有结构，承担「信号语义」而非「抽取结果」的角色：

[request_signal_history.go:9-30](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/request_signal_history.go#L9-L30) —— `signalConversationHistory` 结构：`currentUserMessage`/`priorUserMessages`/`nonUserMessages` 是文本原料，其余是 conversation 信号族要用的会话形状事实（消息计数、工具调用计数、末条角色等）。

值得体会的一个细节：搬运时对切片做了防御性深拷贝（`append([]string(nil), result.PriorUserMessages...)`）。这是因为 `FastExtractResult` 在请求处理全程被多处引用（如 `ctx.UserContent = fast.UserContent`），若不拷贝，信号侧的改动会反噬到其他读 fast 的地方。这是「请求级共享状态」场景下常见的稳妥写法。

此外，用户文本还会被单独存进请求上下文，供缓存查找、幻觉检测等非信号消费方使用——这就是为什么 4.1.2 流程里第 6 步要把 `fast.UserContent` 写到 `ctx.UserContent`：

[request_context.go:215-216](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/request_context.go#L215-L216) —— `RequestContext.UserContent` / `RequestImageURL`：用户文本与首图既喂给信号构建（经 history），也喂给幻觉检测、缓存等后续阶段，所以单独存在上下文里。

> **本模块的边界**：`performDecisionEvaluation` 拿到 `history` 之后做什么（如何调 classifier 抽信号、如何调决策引擎求值），是 u5-l2 的主题。本讲只到「原料备好、交棒」为止。

#### 4.3.4 代码实践

**实践目标**：不动手跑，纯靠阅读，验证「信号输入构建」是否真的把 `FastExtractResult` 完整地搬进了决策入口。

**操作步骤**（源码阅读型实践）：

1. 打开 `request_signal_history.go`，对照 `FastExtractResult`（utils_fast.go:20-45）与 `signalConversationHistory`（request_signal_history.go:9-30）两张字段表，逐项核对哪些字段被搬运、哪些被丢弃。
2. 在 `processor_req_body_prepare.go` 的 `runRequestPreRoutingStages` 里，确认 `history` 与 `originalModel` 是分开传给 `performDecisionEvaluation` 的（文本原料与路由选择器分轨）。
3. 思考：为什么 `metadata` 也要搬进会话历史？（提示：metadata 信号族会用到它。）

**需要观察的现象**：

- `FastExtractResult` 的几乎所有字段都在 `signalConversationHistory` 里有对应（`Model`/`Stream` 除外——它们不进会话历史，而是分别走 `originalModel` 与 `ctx.ExpectStreamingResponse`）。
- 切片类字段（`PriorUserMessages` 等）都做了深拷贝。

**预期结果**：你能画出一张「`FastExtractResult` 字段 → `signalConversationHistory` 字段 → 被哪个信号族消费」的映射表，并解释为什么 `Model` 不进 history。

#### 4.3.5 小练习与答案

**练习 1**：`signalConversationHistoryFromFastExtract` 为什么对 `PriorUserMessages` 等切片做 `append([]string(nil), ...)` 深拷贝，而不是直接赋值？

> **答案**：因为 `FastExtractResult` 在请求处理全程被多处引用（用户文本也被写进 `ctx.UserContent` 供缓存/幻觉检测用）。直接赋值会让 `signalConversationHistory` 与 fast 共享底层数组；若信号侧追加或修改元素，会反噬到其他读 fast 的地方。深拷贝切断共享，是共享请求状态下的防御性写法。

**练习 2**：`FastExtractResult.Model` 为什么没有被搬进 `signalConversationHistory`？

> **答案**：因为 `Model` 不是「会话内容」，而是「路由选择器」。它的去向是 `originalModel`，单独传给 `performDecisionEvaluation`，并在更早的 `resolveEntrypointForRequest` 里决定了走哪个 recipe。会话历史只承载文本原料与会话形状事实，与 model 名分轨传递。

**练习 3**：如果一条请求的 body 里完全没有 `messages` 字段（比如某个非 chat 端点），`extractContentFast` 会返回什么？这会如何影响信号输入构建？

> **答案**：`extractContentFast` 检查到 `messages` 不存在或不是数组时，会直接返回一个几乎全空的 `FastExtractResult`（只有 model/stream/metadata）。`signalConversationHistoryFromFastExtract` 据此产出一个空的 `signalConversationHistory`（无用户文本、所有计数为零）。决策引擎拿到空历史后，信号抽取基本不会命中任何 conversation/keyword 信号，最终大概率落到兜底路由。

## 5. 综合实践

把本讲三个模块串起来，做一次「**给一条真实请求画全身像**」的练习。

**任务**：选择仓库里某个 recipe（推荐 `config/recipes/balance/`），构造一条**多轮带工具调用**的 `/v1/chat/completions` 请求体（即 messages 里包含 user、assistant（带 tool_calls）、tool、user 这样的序列），然后：

1. **解析侧**：手工模拟 `extractContentFast`，写出这条请求的 `FastExtractResult` 关键字段值——尤其是 `UserContent`、`PriorUserMessages`、`HasAssistantReply`、`AssistantToolCallCount`、`ToolResultCount`、`LastMessageRole`、`LastUserAfterToolResult`。
2. **入口侧**：分别用 `vllm-sr/auto`、balance 的 entrypoint 虚拟名（若配置了）、一个具体后端模型名作为 `model`，预测 `resolveEntrypointForRequest` 会把 `ctx.Routing` 设成哪个枚举态，以及 `decisionCandidatesForRequest` 会返回哪组 decisions。
3. **信号侧**：写出 `signalConversationHistoryFromFastExtract` 产出的 `signalConversationHistory`，并猜测这些会话形状事实会让哪些 conversation 信号命中（如「多轮工具调用」类信号）。
4. **交棒**：最后写出 `performDecisionEvaluation` 收到的两个输入（`originalModel` 与 `history`），并指出它接下来会做什么（留给 u5-l2 回答）。

**交付物**：一张包含上述四步的「请求全身像」表，外加一段话解释「为什么这条多轮工具调用请求，在 auto 别名下与在具体模型名下，走的完全是两条路」。

> 这个练习不要求你真的跑通服务（运行结果**待本地验证**），重点是让你能把「字节 → 抽取 → 入口定性 → 信号备料 → 决策交棒」这条链在脑子里完整走一遍。如果你能不看源码写出第 1、2 步的字段值与枚举态，本讲就过关了。

## 6. 本讲小结

- 请求体处理的主函数 `handleRequestBody` 是一条**带多个早退点的线性流水线**：跳过处理 → Response API 翻译 → 校验 → 快速抽取 → 记 model → looper → 记 user content → 预路由 → 完整解析 → 模型路由。
- SR 用 **gjson 快速抽取（`extractContentFast`）优先、完整 SDK 解析延迟**的策略省算力：缓存/限流/fast_response 等早退路径只付快速抽取的成本，只有真要改写 body 时才做完整解析。
- 请求里的 `model` 字段是**路由选择器**而非 necessarily 真模型名：auto 别名走 `default` recipe、entrypoint 虚拟名走映射的 recipe、具体后端模型走 **passthrough** 绕过配方路由。
- 「走配方」与「直通」由 `RequestRoutingContext` 的**三态枚举**严格区分，避免 nil 歧义；后续「走哪组 decisions」「用哪个 classifier」全部从 `ctx.Routing` 派生，且命名 recipe 永不跨边界回退。
- 信号输入构建是一层**搬运解耦**：`signalConversationHistoryFromFastExtract` 把协议无关的 `FastExtractResult` 转成 `signalConversationHistory`，连同 `originalModel` 一起交棒给 `performDecisionEvaluation`，文本原料与路由选择器分轨传递。
- 本讲的终点是「决策求值之前的准备」；`performDecisionEvaluation` 内部如何抽信号、调决策引擎、选路由，是下一讲 u5-l2 的主题。

## 7. 下一步学习建议

- **紧接着读 u5-l2（决策求值管线）**：本讲交棒出去的 `history` 与 `originalModel`，在 `performDecisionEvaluation` 里如何变成 `prepareSignalEvaluationInput` → `evaluateSignalsForDecision` → `runDecisionEngine`，那是请求链路的真正内核。
- **回头补 u2-l2 / u2-l3**：如果对「conversation 信号族如何消费那些会话形状计数」「投影如何把信号协调成路由带」还不够清楚，可重读信号层与投影层讲义，把 4.3 的字段映射补全到「被哪个信号/投影消费」。
- **延伸阅读 config 层**：想彻底搞懂 recipe 隔离边界，可读 `pkg/config/recipes.go` 的 `ConfigForRecipe`（u3-l3 提过的隔离视图），看「recipe 局部名字不可跨 recipe 引用」是如何在配置层被强制的。
- **性能线**：对「快速抽取 vs 完整解析」感兴趣的话，可对比 `extractContentFast`（gjson）与 `parseRequestForProtocol`（SDK 解析）两条路径的开销差异，并在 `prepareRequestForModelRouting` 里找出「什么条件下才会触发完整解析」。
