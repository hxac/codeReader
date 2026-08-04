# 请求/响应处理链与 API Key 认证

## 1. 本讲目标

本讲承接 [u7-l1 Envoy ExtProc 网关插件入口](u7-l1-gateway-extproc-entry.md)，把视线从「ExtProc 流主循环」下沉到「一个 HTTP 请求在网关内部被逐阶段加工」的具体过程。学完本讲，你应当能够：

- 说清 `HandleRequestHeaders` / `HandleRequestBody` / `HandleResponseHeaders` / `HandleResponseBody` 四个阶段各自**做了什么、在何时被调用**，以及它们之间的数据如何经 `processState` 与 `RoutingContext` 传递。
- 解释请求头阶段如何完成 **Header 提取、API Key 认证、按用户限流（RPM/TPM）、OpenTelemetry 追踪上下文注入**。
- 描述请求体阶段如何解析 body、校验模型可用性、做出**路由决策并把目标 Pod 注入 `target-pod` 头**。
- 描述响应体阶段如何在**流式场景下逐 chunk 扫描**、在最后一个 chunk 完成 Token 计量与 `request_end` 指标上报。
- 理解 API Key 认证「可选开启 + 常数时间比较」的安全设计，以及它与「按用户限流」的职责分工。

## 2. 前置知识

阅读本讲前，建议先建立以下直觉（不熟悉的术语下面会解释）：

- **Envoy External Processing（ExtProc）协议**：Envoy 把每个 HTTP 请求的关键节点（请求头、请求体、响应头、响应体）以 gRPC 双向流的消息形式推给外部插件，插件可以**改写头与体、或直接短路返回错误**。AIBrix 网关就是一个 ExtProc gRPC 服务。
- **`ProcessingRequest` / `ProcessingResponse`**：ExtProc 流里的两类 protobuf 消息。Envoy 发 `ProcessingRequest`（带 `RequestHeaders` / `RequestBody` / `ResponseHeaders` / `ResponseBody` 四种具体类型之一），插件回 `ProcessingResponse`。
- **`ImmediateResponse`（立即响应）**：当插件判定请求不合法（如 API Key 错误、模型不存在），它返回一个 `ImmediateResponse`，Envoy 据此**短路**返回对应 HTTP 状态码，请求不会继续往后端推理 Pod 走。
- **`RoutingContext`**：贯穿单个请求全生命周期的路由上下文对象，携带 model、message、stream、请求/响应时间、目标 Pod 等信息，是四个阶段之间的「数据传送带」。
- **SSE（Server-Sent Events）流式响应**：大模型流式输出常用 `data: {...}\n\n` 格式逐块推送，最后以 `data: [DONE]` 结束。流式响应里 Token 用量（usage）通常只在**最后一个 chunk** 携带。
- **常数时间比较（constant-time compare）**：一种抗「计时侧信道攻击」的字符串比较方式，让攻击者无法通过比较耗时长短逐字节猜出密钥。

如果你还没看过 u7-l1，请先了解 `Process` / `processOnce` / `handleProcessingRequest` 这条流主循环——本讲正是从 `handleProcessingRequest` 的分发表往下走。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pkg/plugins/gateway/gateway.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go) | 网关主体：`handleProcessingRequest` 是四阶段分发的总入口；`NewServer` 装配限流器与 API Key 配置；`processState` 保存每请求状态。 |
| [pkg/plugins/gateway/gateway_req_headers.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_headers.go) | **请求头阶段** `HandleRequestHeaders`：提取关键头、API Key 认证、按用户限流、注入追踪上下文。 |
| [pkg/plugins/gateway/api_key_auth.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/api_key_auth.go) | **API Key 认证**实现：读取环境变量 token、解析 `Bearer`、SHA-256 + 常数时间比较。 |
| [pkg/plugins/gateway/gateway_req_body.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_body.go) | **请求体阶段** `HandleRequestBody`：解析 body、校验模型、路由决策、注入 `target-pod` 头。 |
| [pkg/plugins/gateway/gateway_rsp_body.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_rsp_body.go) | **响应体阶段** `HandleResponseBody`：流式逐 chunk Token 计量、`request_end` 指标与收尾。 |
| [pkg/plugins/gateway/gateway_rsp_headers.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_rsp_headers.go) | **响应头阶段** `HandleResponseHeaders`：检查上游状态码、回填路由信息头（本讲作为链路一环简介）。 |
| [pkg/plugins/gateway/util.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/util.go) | body 解析分发 `validateRequestBody`、错误响应构造 `buildErrorResponse` / `generateErrorResponse`、头构造 `buildEnvoyProxyHeaders`。 |
| [pkg/plugins/gateway/gateway_ratelimit.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_ratelimit.go) | 按用户限流 `checkLimits`（RPM/TPM）、按模型限流 `enforceModelRPS`。 |
| [pkg/plugins/gateway/types.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/types.go) | 头常量、错误码、默认限流值、请求路径常量的集中定义。 |

## 4. 核心概念与源码讲解

本讲按「链路总览 → 请求头阶段 → API Key 认证 → 请求体阶段 → 响应体阶段」拆成五个最小模块。其中「API Key 认证」「请求/响应处理链」「Header 处理」是本讲指定的三个核心最小模块，总览模块负责把它们串成一条线。

### 4.1 四阶段处理链总览

#### 4.1.1 概念说明

ExtProc 把一个 HTTP 请求拆成**四个回调时机**依次推给插件，AIBrix 网关在 `handleProcessingRequest` 里用一个 `switch req.Request.(type)` 把这四个时机分别派发到四个 `Handle*` 方法。理解这条链的关键是：**每个阶段只做自己那一棒的事，阶段之间靠 `processState`（每请求状态）与 `RoutingContext`（路由上下文）传值**。

四个阶段的职责分工如下：

| 阶段 | 方法 | 时机 | 主要职责 | 产出 |
| --- | --- | --- | --- | --- |
| 请求头 | `HandleRequestHeaders` | 收到请求头 | 提取头、**认证**、**按用户限流**、注入追踪 | `user`、`rpm`、`routingCtx` |
| 请求体 | `HandleRequestBody` | 收到请求体 | 解析 body、校验模型、**路由决策** | `model`、`stream`、`traceTerm` |
| 响应头 | `HandleResponseHeaders` | 收到上游响应头 | 检查 `:status`、回填路由信息头 | `isRespError`、`respErrorCode` |
| 响应体 | `HandleResponseBody` | 收到上游响应体（流式下逐 chunk） | **Token 计量**、`request_end` 指标、收尾 | `completed` |

一个常被初学者忽略的要点：**路由决策发生在「请求体」阶段而不是「请求头」阶段**。原因很简单——模型名和用户消息都在 body 里，请求头阶段还看不到，自然无法选目标 Pod。这点 u7-l1 已点明，本讲会落实到具体代码。

#### 4.1.2 核心流程

```
Envoy 推送 ProcessingRequest
        │
        ▼
handleProcessingRequest(st, req)        ← 按 req.Request.(type) 分发
        │
        ├── RequestHeaders  → HandleRequestHeaders   (认证/限流/建 RoutingContext)
        ├── RequestBody     → HandleRequestBody      (路由决策, 注入 target-pod 头)
        ├── ResponseHeaders → HandleResponseHeaders  (检查上游状态码)
        └── ResponseBody    → HandleResponseBody     (Token 计量/收尾, 流式逐 chunk)
        │
        ▼
sendProcessingResponse → srv.Send(resp) → Envoy 据此改写或短路
```

每个 `Handle*` 方法的返回值会被写回 `processState`，供后续阶段读取。例如请求头阶段算出的 `user` 会在请求体/响应体阶段被用于 Token 计量（TPM）。

#### 4.1.3 源码精读

分发总入口在 `handleProcessingRequest`，四个 `case` 一一对应四个阶段：

[ pkg/plugins/gateway/gateway.go:L376-L459 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L376-L459) — 四阶段 `switch` 分发，每个分支调用对应 `Handle*` 并把返回值写回 `processState`。

请求头分支把返回的 `user / rpm / routingCtx` 存进状态：

[ pkg/plugins/gateway/gateway.go:L380-L386 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L380-L386) — `RequestHeaders` 分支：调用 `HandleRequestHeaders`，并把 `routingCtx.Span` 指向根 span，同时把 `metricLabel` 记为 `"gateway_req_headers"`（这是四个标签里唯一用字面量而非常量的一处）。

请求体分支把返回的 `model / stream / traceTerm` 存进状态，并在非短路时开启推理 span：

[ pkg/plugins/gateway/gateway.go:L388-L398 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L388-L398) — `RequestBody` 分支：若响应不是 `ImmediateResponse`（即请求通过了校验、将进入推理），开启 `llm.inference` span；若是流式，再额外开启 `llm.time_to_first_response_chunk` span。

响应头与响应体分支：

[ pkg/plugins/gateway/gateway.go:L400-L424 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L400-L424) — `ResponseHeaders` 分支检查上游是否出错；`ResponseBody` 分支在首块到达时结束 `firstRespSpan` 并开启 `toLastRespSpan`，若上游已判定为错误则走错误处理，否则调 `HandleResponseBody`。

> 说明：本讲的「Header 处理」最小模块集中在请求头阶段（4.2），响应头阶段只做状态码检查与信息回填，作为链路完整性在此带过。

#### 4.1.4 代码实践

**实践目标**：在四个阶段各打一行日志，直观验证它们的调用顺序与次数。

**操作步骤**（仅阅读 + 仿写，不修改源码运行逻辑）：

1. 在 `gateway_req_headers.go` 的 `HandleRequestHeaders` 开头、`gateway_req_body.go` 的 `HandleRequestBody` 开头、`gateway_rsp_headers.go` 的 `HandleResponseHeaders` 开头、`gateway_rsp_body.go` 的 `HandleResponseBody` 开头，分别想象插入一行 `klog.InfoS("phase", "stage", "req_headers"/"req_body"/"rsp_headers"/"rsp_body", "requestID", requestID)`。
2. 不实际运行，仅依据 ExtProc 协议与源码推导：发起一次**非流式**请求与一次**流式**请求时，四个阶段各被调用几次。

**需要观察的现象 / 预期结果**（待本地验证）：

- 非流式请求：四个阶段各调用 **1 次**（响应体可能被 Envoy 拆成多块，但 `processLanguageResponse` 会缓存到 `EndOfStream` 才处理）。
- 流式请求：请求头、请求体、响应头各 **1 次**；响应体阶段 `HandleResponseBody` **每个 SSE chunk 调用一次**，但 `request_end` 指标只在最后一个含 `usage` 的 chunk 触发一次。

#### 4.1.5 小练习与答案

**练习 1**：为什么 API Key 认证放在请求头阶段，而路由决策放在请求体阶段？

> **答案**：API Key 在 `Authorization` 头里，请求头阶段就能拿到，应尽早拦截非法请求、避免浪费后续解析与路由开销；而路由决策依赖模型名与消息内容，这些都在 body 里，必须等到请求体阶段才能做。

**练习 2**：`handleProcessingRequest` 如何把请求头阶段算出的 `user` 传给响应体阶段做 TPM 计量？

> **答案**：请求头分支把 `user` 写进 `processState.user`（`st.user`），响应体分支调用 `HandleResponseBody` 时把 `st.user` 作为参数传入，方法内部据此对 `user.Name` 执行 `Incr("%v_TPM_CURRENT")`。

---

### 4.2 请求头阶段与 Header 处理

#### 4.2.1 概念说明

请求头阶段是网关对请求的「第一道关」。它要做四件事：**提取**网关关心的头、**认证**（API Key）、**按用户限流**（RPM/TPM）、**注入**追踪上下文。这一阶段还负责创建贯穿后续阶段的 `RoutingContext`。

之所以单独强调「Header 处理」，是因为 AIBrix 用一组**约定好的自定义头**在客户端与网关、网关与 Envoy 之间传递控制信息，例如 `routing-strategy`（指定路由算法）、`config-profile`（指定配置画像）、`x-session-id`（会话亲和）、`traceparent`（W3C 追踪）。请求头阶段就是这些头的「收集站」。

#### 4.2.2 核心流程

```
遍历 Envoy 传来的请求头列表
   ├── user / :path / authorization / routing-strategy / config-profile
   │   x-session-id / x-aibrix-session-key / external-filter / content-type / traceparent
   │   （按键名小写归一化，分类存入局部变量或 reqHeaders map）
   ├── 【若开启了 API Key】校验 Authorization，失败 → 401 ImmediateResponse
   ├── 【若有 user 且有 Redis】解析用户 → checkLimits(RPM/TPM)，超限 → 429 ImmediateResponse
   ├── 构造 RoutingContext（携带 path / headers / config-profile / user）
   └── 注入 x-went-into-req-headers=true + OpenTelemetry traceparent（供网关发起的下游请求继承）
```

注意一个细节：响应里设置了 `ClearRouteCache: true`。因为本阶段往请求里新增了头（如追踪头），可能影响 Envoy 的路由决策，所以要求 Envoy 清缓存重新评估路由。

#### 4.2.3 源码精读

头提取是一个对 Key 小写归一化的 `switch`：

[ pkg/plugins/gateway/gateway_req_headers.go:L58-L84 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_headers.go#L58-L84) — 遍历请求头，把 `user`、`:path`、`authorization`、`external-filter`、`content-type`、`routing-strategy`、`config-profile`、`x-session-id`、`x-aibrix-session-key`、`traceparent` 分别提取到局部变量或 `reqHeaders` map。`traceparent` 还会尝试从中解析 trace ID 作为 requestID（当根 span 没有 traceID 时）。

这些头常量集中定义在 types.go：

[ pkg/plugins/gateway/types.go:L47-L66 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/types.go#L47-L66) — `HeaderWentIntoReqHeaders`、`HeaderTargetPod`、`HeaderRoutingStrategy`、`HeaderModel`、`HeaderExternalFilter`、`HeaderConfigProfile`、`HeaderSessionID`、`HeaderSessionKey`、`HeaderTraceParent`、`HeaderUpdateRPM/TPM` 等头常量。

API Key 校验紧跟其后（详见 4.3）：

[ pkg/plugins/gateway/gateway_req_headers.go:L87-L105 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_headers.go#L87-L105) — 仅当 `s.apiKeyAuth != nil && s.apiKeyAuth.token != ""`（即配置了 token）时才校验；从 `reqHeaders` 里大小写不敏感地找 `authorization`，调 `isAuthorized`，失败返回 401 与 `ErrorCodeInvalidAPIKey`。

按用户限流（在提供了 `user` 头且有 Redis 时触发）：

[ pkg/plugins/gateway/gateway_req_headers.go:L107-L127 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_headers.go#L107-L127) — 用 `utils.GetUser` 从 Redis 取用户配置，再调 `s.checkLimits` 做 RPM/TPM 校验与 RPM 自增，任一失败即返回带错误头的 `ImmediateResponse`。

构造 `RoutingContext` 并注入追踪上下文：

[ pkg/plugins/gateway/gateway_req_headers.go:L129-L152 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_headers.go#L129-L152) — `NewRoutingContext` 建上下文并填入 `ReqPath` / `ReqHeaders` / `ReqConfigProfile`；随后 `otel.GetTextMapPropagator().Inject` 把当前 span 的 traceparent 写回响应头，使网关自己发起的下游请求（如 PD 解耦时的 prefill→decode 调用）能继承同一条追踪链。

最终返回 `RequestHeaders` 响应：

[ pkg/plugins/gateway/gateway_req_headers.go:L159-L170 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_headers.go#L159-L170) — 返回 `ProcessingResponse_RequestHeaders`，带 `ClearRouteCache: true`。

#### 4.2.4 代码实践

**实践目标**：搞清一个自定义头（如 `routing-strategy`）从客户端到路由阶段的完整流转。

**操作步骤**：

1. 在客户端请求里加头 `routing-strategy: random`。
2. 跟踪：`HandleRequestHeaders` 第 70-71 行把它存入 `reqHeaders[HeaderRoutingStrategy]` → 第 131 行 `routingCtx.ReqHeaders = reqHeaders` → 后续请求体阶段 `deriveRoutingStrategyFromContext` 从 `routingCtx.ReqHeaders` 读它（util.go:671-692）。

**预期结果**：请求会按 `random` 路由，而非环境变量或配置画像里的默认算法。若同时设了 `routing-strategy` 头与配置画像，**头优先**（`deriveRoutingStrategyFromContext` 先查头）。

#### 4.2.5 小练习与答案

**练习 1**：客户端没传 `user` 头会怎样？还会限流吗？

> **答案**：不会按用户限流。`username` 为空时跳过 `utils.GetUser` 与 `checkLimits`（gateway_req_headers.go:107 的 `if username != ""`），`user.Name` 为空，后续 TPM 自增也会因 `user.Name != ""` 判断（gateway_rsp_body.go:215）而跳过。

**练习 2**：为什么响应要设 `ClearRouteCache: true`？

> **答案**：本阶段向请求注入了追踪头等新头，可能改变 Envoy 的路由匹配结果；清缓存强制 Envoy 用改写后的头重新评估路由，避免用到过期决策。

---

### 4.3 API Key 认证

#### 4.3.1 概念说明

AIBrix 网关的 API Key 认证是一个**轻量、可选**的「网关访问门禁」：它只校验**一个共享 Bearer Token**，而不是多租户的密钥库。它的设计有两个要点初学者容易误解：

1. **默认关闭**。只有当环境变量 `AIBRIX_AUTH_BEARER_TOKEN` 被设置为非空字符串时，认证才生效；否则网关对所有请求放行。这适合内部集群信任边界内的部署。
2. **与「按用户限流」是两回事**。API Key 管「能不能进网关」，而 `user` 头 + Redis 管「这个用户用了多少额度（RPM/TPM）」。两者独立，不要混淆。

安全上最关键的一行是：比较 token 时用 `subtle.ConstantTimeCompare`，并先对两端各做一次 SHA-256。这是为了抵御**计时侧信道攻击**——普通 `==` 比较会在第一个不匹配字节处提前返回，攻击者可据此逐字节爆破；常数时间比较无论是否匹配都消耗相同时间。

#### 4.3.2 核心流程

```
NewServer() 启动时：
   loadAPIKeyAuthConfig()  ← 读 env AIBRIX_AUTH_BEARER_TOKEN，TrimSpace

每个请求（请求头阶段）：
   if 配置了 token：
      从请求头取 Authorization 值
      isAuthorized(authHeader):
         extractBearerToken: 解析 "Bearer <token>"（大小写不敏感、非空）
         sha256(配置 token) vs sha256(请求 token)
         subtle.ConstantTimeCompare → 相等才放行
      失败 → 401 "Incorrect API key provided"
```

#### 4.3.3 源码精读

配置加载在 `NewServer` 中完成一次：

[ pkg/plugins/gateway/gateway.go:L183 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L183) — `apiKeyAuth: loadAPIKeyAuthConfig()`，把配置挂到 `Server` 上，后续每请求复用。

[ pkg/plugins/gateway/api_key_auth.go:L34-L38 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/api_key_auth.go#L34-L38) — `loadAPIKeyAuthConfig` 读 `AIBRIX_AUTH_BEARER_TOKEN` 并 `TrimSpace`。

核心校验逻辑：

[ pkg/plugins/gateway/api_key_auth.go:L40-L48 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/api_key_auth.go#L40-L48) — `isAuthorized`：先解析 Bearer token，再对配置 token 与请求 token 各做 SHA-256，最后用 `subtle.ConstantTimeCompare` 比较。先哈希再比较是为了让常数时间比较作用在固定 32 字节摘要上，而非长度可变的原始 token。

Bearer 解析：

[ pkg/plugins/gateway/api_key_auth.go:L50-L60 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/api_key_auth.go#L50-L60) — `extractBearerToken`：按首个空格切成两段，校验前缀为 `Bearer`（`EqualFold` 大小写不敏感），第二段非空。

被请求头阶段调用的位置已在 4.2.3 引用（gateway_req_headers.go:87-105）。注意触发条件 `s.apiKeyAuth.token != ""` 正是「默认关闭」的来源。

#### 4.3.4 代码实践

**实践目标**：验证 API Key 认证的「可选开启」与「401 短路」行为。

**操作步骤**：

1. 阅读 `NewServer`（gateway.go:179-193）与 `loadAPIKeyAuthConfig`，确认 token 来自环境变量。
2. 推演两种部署：
   - **不设** `AIBRIX_AUTH_BEARER_TOKEN`：`apiKeyAuth.token == ""`，gateway_req_headers.go:87 的 `if` 不进入，网关全放行。
   - **设** `AIBRIX_AUTH_BEARER_TOKEN=secret123`：任何请求须带 `Authorization: Bearer secret123`，否则 401。
3. 若本地可起 standalone 网关（见 [u1-l5](u1-l5-standalone-deployment.md)），在 `start.sh` 导出该环境变量后，用 `curl` 分别带与不带 `Authorization` 头打网关，观察返回码。

**需要观察的现象 / 预期结果**（待本地验证）：

- 不带头 → HTTP 401，body 是 OpenAI 格式 `{"error":{"message":"Incorrect API key provided","type":"authentication_error","code":"invalid_api_key",...}}`（由 `generateErrorMessageWithHTTPCode` 在 401 时映射为 `authentication_error` 类型，见 util.go:776-792）。
- 带正确头 → 正常进入请求体阶段。

#### 4.3.5 小练习与答案

**练习 1**：为什么比较前要先把两个 token 都过一遍 SHA-256，而不是直接 `ConstantTimeCompare([]byte(c.token), []byte(token))`？

> **答案**：`subtle.ConstantTimeCompare` 要求两个切片**长度相等**才返回有意义的常数时间结果；长度不等时它会提前返回 0，仍泄露长度信息。先哈希把两端都变成定长 32 字节摘要，既抹平长度差异，又避免在网关里以明文长期持有/比较可变长密钥。

**练习 2**：如果集群里只信任内网调用方，是否还需要开 API Key？

> **答案**：取决于信任边界。AIBrix 把它设计成可选，正是因为很多部署里网关只暴露给集群内或受控网段；此时可不开 API Key，而靠网络策略兜底。一旦网关面向更广的网络，就应设置 `AIBRIX_AUTH_BEARER_TOKEN` 开启这道门禁。

---

### 4.4 请求体处理链：解析、校验与路由注入

#### 4.4.1 概念说明

请求体阶段是网关真正「干活」的阶段：解析 JSON/multipart body 拿到 model 与 message、校验模型是否存在于缓存且有无就绪 Pod、推导路由策略、做出路由决策，最后把**目标 Pod 地址以 `target-pod` 头的形式注入回请求**，让 Envoy 把请求转发到那个具体 Pod。

这里有一个贯穿本讲的核心结论：**网关通过「改写请求头」来指挥 Envoy 路由**。插件本身不直接转发 HTTP，它只告诉 Envoy「这个请求该去哪个 Pod」，真正的网络转发由 Envoy 完成。`target-pod` 头就是这条指挥链上的指令。

此外，请求体阶段还会处理两条互斥分支：未配置路由算法时（`RouterNotSet`），网关退化为 HTTPRoute 校验器，路由交给 Envoy 原生；配置了算法时，才走 `selectTargetPod` 钦点目标 Pod。这与 u7-l1 的结论一致，本讲落实到代码。

#### 4.4.2 核心流程

```
HandleRequestBody:
  1. 解析 body
       ├── 音频端点 + multipart → parseMultipartFormData
       └── 其他 → validateRequestBody（按 path 分发：chat/responses/completions/embeddings/...）
  2. 填 RoutingContext：Model / Message / Stream / ReqBody
  3. validateModelAvailability：缓存里有无该 model、有无就绪 Pod
       └── ModelClaim 睡眠态 → 触发 wake，返回带 Retry-After 的 503
  4. 读引擎标签；applyConfigProfile（解析配置画像）
  5. deriveRoutingStrategyFromContext（头 → 画像 → env）+ routing.Validate
  6. enforceModelRPS（按模型 RPS 限流，失败可回滚）
  7. 分支：
       ├── RouterNotSet → 校验 HTTPRoute 状态，注入 model 头（交 Envoy 原生路由）
       └── 配置算法    → selectTargetPod → 注入 routing-strategy/target-pod/content-length/X-Request-Id
  8. cache.AddRequestCount（登记在途请求，返回 traceTerm）
  9. 返回 RequestBody 响应：HeaderMutation（注入的头）+ BodyMutation（可能改写后的 body）
```

#### 4.4.3 源码精读

入口与 body 解析分流：

[ pkg/plugins/gateway/gateway_req_body.go:L42-L77 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_body.go#L42-L77) — `HandleRequestBody` 开头：按是否音频 + multipart 决定走 `parseMultipartFormData` 还是 `validateRequestBody`，得到 `model / message / stream`，并写入 `RoutingContext`。

body 解析的真正分发器在 util.go，按请求路径走不同校验：

[ pkg/plugins/gateway/util.go:L160-L189 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/util.go#L160-L189) — `validateRequestBody` 用 `switch requestPath` 把 `/v1/chat/completions`、`/v1/responses`、`/v1/completions`、`/v1/embeddings`、图像/视频生成、rerank、classify、音频等分别交给专用 `validate*` 函数；未知路径返回 501。

模型可用性校验（含 ModelClaim 唤醒）：

[ pkg/plugins/gateway/gateway_req_body.go:L232-L260 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_body.go#L232-L260) — `validateModelAvailability`：缓存无该 model 时，先查 ModelClaim 绑定（睡眠态则触发 `wakeRequester.RequestWake` 并返回带 `Retry-After` 的 503）；无就绪 Pod 返回 503；正常返回 Pod 列表。

策略推导与按模型限流：

[ pkg/plugins/gateway/gateway_req_body.go:L97-L125 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_body.go#L97-L125) — `deriveRoutingStrategyFromContext` 推导策略（非法则 400），随后 `enforceModelRPS` 做按模型 RPS 限流，并用 `needsRollback` + `defer` 保证「路由失败时回滚已自增的 RPS 计数」。

两条互斥分支与 `target-pod` 注入：

[ pkg/plugins/gateway/gateway_req_body.go:L127-L177 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_body.go#L127-L177) — `RouterNotSet` 分支只校验 HTTPRoute 并注入 `model` 头；配置算法分支调 `selectTargetPod` 得到 `targetPodIP`，再注入 `routing-strategy`、`target-pod`、`content-length`、`X-Request-Id` 四个头，并打 `request_start` 日志、设置 span 属性。

按模型 RPS 限流的实现：

[ pkg/plugins/gateway/gateway_ratelimit.go:L117-L134 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_ratelimit.go#L117-L134) — `enforceModelRPS` 仅当配置画像里 `RequestsPerSecond > 0` 时生效，原子自增 per-model 计数，超限返回 429。

最终把头与（可能改写的）body 一起回给 Envoy：

[ pkg/plugins/gateway/gateway_req_body.go:L183-L198 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_body.go#L183-L198) — 返回 `ProcessingResponse_RequestBody`，含 `HeaderMutation.SetHeaders`（注入的头）与 `BodyMutation.Body`（`routingCtx.ReqBody`）。

#### 4.4.4 代码实践

**实践目标**：验证「路由结果通过 `target-pod` 头注入」这一机制。

**操作步骤**：

1. 阅读 gateway_req_body.go:145-149，看清配置算法分支注入的四个头。
2. 阅读 `selectTargetPod`（gateway.go:491-539），理解它返回的是目标 Pod 的地址字符串。
3. 推演：若 `routing-strategy: random` 且有 2 个就绪 Pod，请求体阶段会把其中一个 Pod 的地址写进 `target-pod` 头；Envoy 据此把请求转发到该 Pod。

**预期结果**：在网关日志里能看到 `request_start` 一行，带 `target_pod` 与 `target_pod_ip`（gateway_req_body.go:175-176）；Envoy 的访问日志里该请求的 upstream 即该 Pod。（待本地验证）

#### 4.4.5 小练习与答案

**练习 1**：`enforceModelRPS` 自增了计数后，若紧接着 `selectTargetPod` 失败，计数会一直占用额度吗？

> **答案**：不会。gateway_req_body.go:120-125 用 `needsRollback := true` + `defer` 在路由失败时调 `decrModelRPS` 回滚。只有走到第 179 行 `needsRollback = false` 才不回滚。

**练习 2**：`RouterNotSet` 分支为什么还要调 `validateHTTPRouteStatus`？

> **答案**：未配置路由算法时，网关不钦点 Pod，路由完全依赖 Envoy 的 HTTPRoute 资源；若该 HTTPRoute 不存在或未被网关接受（Accepted/ResolvedRefs 条件不满足），请求会转发失败。故先校验其状态，失败则直接返回 503，给出清晰错误而非让 Envoy 静默失败。

---

### 4.5 响应体处理链：流式 Token 计量与收尾

#### 4.5.1 概念说明

响应体阶段负责从上游推理 Pod 的响应里**提取 Token 用量、做 TPM 计量、上报 `request_end` 指标并收尾**。它的难点全在**流式**：流式响应由许多 SSE chunk 组成，`HandleResponseBody` 会被**每个 chunk 调用一次**，而 Token 用量（usage）通常只在最后一个 chunk 出现。因此这一阶段必须做到「逐块扫描、末块结算、全程只收尾一次」。

这里有两个容易踩坑的点：

- **TTFT（Time To First Token）**：流式下「首 token 时间」要用**第一个**响应体 chunk 的到达时间，而不是最后一块。源码里专门在首块到达时记录 `FirstTokenTime`。
- **Token 字段双命名**：Chat/Completions API 用 `prompt_tokens`/`completion_tokens`，Responses API（`/v1/responses`）用 `input_tokens`/`output_tokens`，语义相同。代码用指针字段区分「字段缺失（nil）」与「值为 0」。

#### 4.5.2 核心流程

```
HandleResponseBody（每个响应体 chunk 调一次）:
  1. 流式 & 首块 → 记录 FirstTokenTime（用于 TTFT）
  2. defer：若本次完成且之前未完成 → DoneRequestTrace + routerCtx.Delete()（仅一次）
  3. 取 usage：
       ├── 流式 → processStreamingResponse（零分配字节扫描，gjson 抽 usage / response.usage）
       └── 非流式语言 → processLanguageResponse（缓冲到 EndOfStream 再整体反序列化）
  4. 若 totalTokens != 0（末块）：
       ├── 对 user 自增 TPM（Incr %v_TPM_CURRENT）
       ├── 注入 x-update-rpm / x-update-tpm 头
       └── requestEndHelper：算 TTFT/TPOT 等，上报指标，打 request_end 日志
  5. 返回 ResponseBody 响应（带头改写）+ complete 标志
```

#### 4.5.3 源码精读

入口与首 token 时间捕获：

[ pkg/plugins/gateway/gateway_rsp_body.go:L162-L191 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_rsp_body.go#L162-L191) — `HandleResponseBody`：首块到达时记 `FirstTokenTime`；`defer` 用 `!hasCompleted && complete` 条件保证 `DoneRequestTrace` 与 `routerCtx.Delete()` 全程只执行一次。

流式与非流式分流：

[ pkg/plugins/gateway/gateway_rsp_body.go:L193-L209 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_rsp_body.go#L193-L209) — 流式调 `processStreamingResponse`，非流式语言请求调 `processLanguageResponse`。

流式零分配扫描的实现（性能关键路径）：

[ pkg/plugins/gateway/gateway_rsp_body.go:L76-L160 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_rsp_body.go#L76-L160) — `processStreamingResponse`：先用 `bytes.Contains(bodyBytes, "usage")` 预过滤，命中再逐行扫描 `data:` 前缀，用 `gjson` 只抽 `usage`（Responses API 走 `response.usage`）字段，避免把每个 chunk 反序列化成完整结构体（旧实现的 CPU/GC 开销注释里有说明）。字段缺失时才回退到 `input_tokens`/`output_tokens` 别名。

末块结算与 TPM 计量：

[ pkg/plugins/gateway/gateway_rsp_body.go:L211-L243 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_rsp_body.go#L211-L243) — 当 `totalTokens != 0` 判定为末块：对用户自增 TPM 并把新的 rpm/tpm 通过 `x-update-rpm`/`x-update-tpm` 头回写给 Envoy（供其附加到响应），随后 `requestEndHelper` 上报指标并打 `request_end` 日志；`EndOfStream` 也置 `complete=true`。

非流式语言响应的缓冲拼装：

[ pkg/plugins/gateway/gateway_rsp_body.go:L275-L345 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_rsp_body.go#L275-L345) — `processLanguageResponse`：用 `requestBuffers`（按 requestID 存的 `sync.Map`）缓冲各块，直到 `EndOfStream` 才整体 `sonic.Unmarshal`，提取 usage；缺 model 字段视为未知错误并按 `res.Code` 映射状态码。

按用户限流（RPM/TPM）的完整实现，对应请求头阶段调用的 `checkLimits`：

[ pkg/plugins/gateway/gateway_ratelimit.go:L31-L78 ](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_ratelimit.go#L31-L78) — `checkLimits`：默认 `RPM=100`、`TPM=RPM*1000=100000`（见 types.go:77-78），依次 `checkRPM` → `incrRPM` → `checkTPM`，超限返回 429 与 `ErrorCodeRateLimitExceeded`。

> 串联理解：**RPM 在请求头阶段「预扣」**（`checkLimits` 自增），**TPM 在响应体末块「按实际用量补扣」**（`Incr %v_TPM_CURRENT`）。这是因为请求到来时就能算一次请求（RPM），但生成多少 token 要等响应结束才知道（TPM）。

#### 4.5.4 代码实践（本讲主实践）

**实践目标**：追踪一次**流式 chat 请求**经过的 body 处理方法，说清 `gateway_req_body` 与 `gateway_rsp_body` 分别何时被调用——这正是本讲指定的实践任务。

**操作步骤**：

1. 准备一个流式 chat 请求（示例，非项目原有命令）：
   ```bash
   curl -N http://<gateway>/v1/chat/completions \
     -H "content-type: application/json" \
     -d '{"model":"<your-model>","stream":true,"stream_options":{"include_usage":true},"messages":[{"role":"user","content":"hi"}]}'
   ```
2. 不运行，仅沿源码画出调用链（见下方「预期结果」）。
3. 阅读关键点：
   - 请求体阶段：`validateRequestBody` → `validateChatRequest`（util.go:193-219）。注意一个**强制校验**——若用户有 TPM 限制（`user.Tpm > 0`）且 `stream:true`，则必须带 `stream_options.include_usage: true`，否则 400（util.go:211-216）。原因正是响应体阶段要靠末块的 usage 做 TPM 计量，不带 usage 就无法准确计费。
   - 响应体阶段：每个 SSE chunk 触发一次 `HandleResponseBody` → `processStreamingResponse`；只有含 `usage` 的末块才进入结算。

**需要观察的现象 / 预期结果**（待本地验证）：

调用链时序如下：

```
请求头:  HandleRequestHeaders          （1 次；认证/限流/建 RoutingContext）
请求体:  HandleRequestBody             （1 次；validateChatRequest → selectTargetPod → 注入 target-pod）
         ├ request_start 日志（1 条）
响应头:  HandleResponseHeaders         （1 次；:status=200）
响应体:  HandleResponseBody            （每个 SSE chunk 1 次）
         ├ 各 chunk：processStreamingResponse 扫描（多数 chunk 无 usage，直接过）
         ├ 首 chunk：记录 FirstTokenTime
         └ 末 chunk（含 usage）：TPM 自增 + request_end 日志（1 条）+ complete=true
```

即：`gateway_req_body`（`HandleRequestBody`）**整条请求只调用 1 次**，发生在请求体到达时、做路由决策；`gateway_rsp_body`（`HandleResponseBody`）在流式下**每个 chunk 调用 1 次**，但 Token 计量与 `request_end` 只在最后一个含 usage 的 chunk 触发一次。

**延伸观察**：若把 `stream_options.include_usage` 去掉、且该用户配了 TPM，预期在请求体阶段就被 400 拒绝（`include usage for stream options not set`），根本不会进入响应体阶段。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `processStreamingResponse` 要先用 `bytes.Contains(bodyBytes, "usage")` 预过滤，再上 `gjson`？

> **答案**：流式响应里绝大多数 chunk 是生成的 token 文本，不含 usage；若每块都做完整 JSON 校验/解析会带来显著 CPU 与 GC 开销（源码注释提到旧实现的问题）。预过滤让昂贵的解析只在「可能含 usage 的末块」执行，保护热路径吞吐。

**练习 2**：非流式语言响应为什么要把各 chunk 先缓冲到 `requestBuffers`，而不是直接解析每个 chunk？

> **答案**：非流式响应的完整 JSON 可能被 Envoy 拆成多个 body chunk 送达，单块不是合法的完整 JSON。必须缓冲到 `EndOfStream` 再整体反序列化，才能正确提取 model 与 usage。

---

## 5. 综合实践

把本讲知识串起来，做一次「端到端请求审计」阅读任务。

**背景**：假设有用户反馈「我的流式请求有时被 400 拒、有时又正常」。请你用本讲授过的链路定位可能原因。

**任务**：

1. **画时序图**：画出一次带 `user` 头、`routing-strategy: random`、`stream:true` 的 `/v1/chat/completions` 请求，在四个阶段分别经过的方法与关键判定（认证 → RPM → body 解析 → `include_usage` 校验 → 路由 → 末块 TPM）。
2. **列出所有可能返回 400 的点**：沿请求头与请求体阶段，枚举会触发 `buildErrorResponse(... StatusCode_BadRequest ...)` 的代码位置（提示：util.go 里多处 `validate*`，以及 `stream_options` 校验）。
3. **定位 TPM 相关拒绝**：解释为什么「用户配了 TPM 但客户端没带 `include_usage`」会必然 400，并说明这个保护为什么放在请求体阶段而非响应体阶段。
4. **延伸**：若要把 API Key 认证从「单一共享 token」升级为「多 API Key 查表」，请指出需要改动的最小代码面（提示：`apiKeyAuthConfig` 结构、`loadAPIKeyAuthConfig`、`isAuthorized`，以及触发条件 gateway_req_headers.go:87）。

**参考思路**（不是唯一答案）：

- 时序图核心：请求头阶段做认证 + RPM；请求体阶段做 body 解析 + `include_usage` 校验 + 路由；响应体末块做 TPM。
- 400 触发点至少包括：body 反序列化失败、`no messages`、`'model' is a required property`、`stream_options.include_usage` 未设、未知路径 501（注意 501 非 400）、非法路由策略（也是 400）。
- TPM 保护放请求体阶段：因为此时已知 `stream` 与 `user.Tpm`，可在「进入推理前」就拒绝，避免无 usage 的流式请求消耗算力后却无法计费。
- 多 Key 改造：把 `token string` 换成 `tokens map[string]struct{}`（或 map 已哈希摘要），`isAuthorized` 改为查表后常数时间比较；`loadAPIKeyAuthConfig` 改为从 Secret/文件加载多 Key。

## 6. 本讲小结

- AIBrix 网关用 `handleProcessingRequest` 把 ExtProc 的四类消息分发到 `HandleRequestHeaders` / `HandleRequestBody` / `HandleResponseHeaders` / `HandleResponseBody`，阶段间靠 `processState` 与 `RoutingContext` 传值。
- **请求头阶段**是第一道关：提取自定义头、做 API Key 认证、按用户 RPM/TPM 限流、注入 OpenTelemetry 追踪上下文，并创建 `RoutingContext`；响应带 `ClearRouteCache: true`。
- **API Key 认证**默认关闭（`AIBRIX_AUTH_BEARER_TOKEN` 非空才启用），用 SHA-256 + `subtle.ConstantTimeCompare` 抗计时攻击；它管「进网关」，与 `user` 头驱动的「按用户限流」职责分离。
- **请求体阶段**解析 body、校验模型可用性、做路由决策，并把目标 Pod 以 `target-pod` 头注入回请求指挥 Envoy 转发；RPM 在此之前预扣，路由失败会回滚 per-model RPS。
- **响应体阶段**在流式下逐 chunk 调用，靠 `bytes.Contains` 预过滤 + `gjson` 抽取末块 usage，仅在末块做 TPM 计量与 `request_end` 上报，全程用 `hasCompleted/complete` 保证收尾只执行一次。
- RPM「请求头预扣」、TPM「响应体末块按实际用量补扣」的分工，是理解整条计量链的关键。

## 7. 下一步学习建议

- 想深入「路由决策本身」如何打分与选 Pod：继续学 [u7-l3 路由抽象 RouterManager 与多策略路由](u7-l3-router-manager.md)，本讲的 `selectTargetPod` 正是它对接路由算法的唯一集成点。
- 想搞清 RPM/TPM/RPS 之外更复杂的排队语义：学 [u7-l5 限流与请求排队](u7-l5-ratelimit-and-queue.md)，本讲的 `ratelimiter` 与 `modelRateLimiter` 来自那里。
- 想看追踪上下文注入后如何被消费：学 [u11-l3 可观测性、Tracing 与二次开发指南](u11-l3-observability-and-extension.md)，理解 `traceparent` 与各阶段 span 的串联。
- 若你想扩展 API Key 认证（如多租户密钥），从 `api_key_auth.go` 的 `apiKeyAuthConfig` 与 `isAuthorized` 入手，结合综合实践第 4 题动手改造（在理解、不破坏安全约束的前提下）。
