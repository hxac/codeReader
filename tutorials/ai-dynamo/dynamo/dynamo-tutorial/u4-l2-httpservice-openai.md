# HttpService：OpenAI 兼容 HTTP 服务

## 1. 本讲目标

上一讲（u4-l1）我们弄清了「引擎如何被装配起来」。本讲顺着装配链往下走一步，回答四个问题：

1. 一条 HTTP 请求进入 frontend 之后，**在到达引擎之前**经历了什么？——路由匹配、就绪检查、并发记账、请求解析、指标打点，这条链全部在 `lib/llm/src/http/` 里。
2. Dynamo 同时暴露 OpenAI、Anthropic、引擎原生 Generate 三类协议端点，它们**各自的处理套路差异**在哪里？
3. 流式响应（SSE）里的每一个 `chat.completion.chunk` 是谁拼出来的？——答案是 `DeltaGenerator`，而它的「生成器状态」被 chat 与 completions 两个端点共享。
4. 本次新增的 `POST /v1/responses/input_tokens` 端点为什么**故意不做任何就绪检查**？它如何在不惊动任何 worker 的情况下估算输入 token 数？

读完本讲，你应该能对着一条 curl 请求说出它在源码中经过的每一个函数名，并且能解释一个容易被误解的点：**`InflightPermit` 不是限流器**。

## 2. 前置知识

- **axum**：Rust 生态中基于 `tokio` 的 Web 框架。你只需要知道三件事：`Router` 负责把「HTTP 方法 + 路径」映射到 handler 函数；`State` 是被所有 handler 共享的只读上下文（通常包在 `Arc` 里）；`middleware::from_fn` 可以在请求进出 handler 之间插入一层逻辑，类似洋葱模型。
- **SSE（Server-Sent Events）**：一种 HTTP 长连接流式协议，服务端不断写入 `data: {...}\n\n` 帧。OpenAI 的 `stream=true` 响应就是 SSE。
- **RAII guard**：Rust 的资源管理惯例——把「计数 +1」放在构造函数里、「计数 -1」放在 `Drop` 里，guard 变量离开作用域时自动归还。`InflightPermit` 就是这个模式。
- **AtomicU64 与 AcqRel 内存序**：跨线程计数器。`fetch_add`/`fetch_sub` 是原子自增自减；`Acquire/Release` 保证「写入对其他线程可见」的顺序。
- **优雅关停（graceful shutdown）**：进程收到 SIGTERM 后不立刻断连，而是「先拒绝新请求 → 等在途响应发完 → 再真正退出」，Kubernetes 滚动更新依赖这个行为。
- **token 估算的「len/3」启发式**：不做真实分词，直接按「字符数 ÷ 3」给出输入 token 的粗略估计。这是 RL rollout 预算与客户端预检常用的低成本近似。
- 前置讲义术语：`Context<T>` 信封（u3-l3）、`AsyncEngine::generate`（u3-l3）、`ModelManager` 与 worker 就绪（u4-l1/u4-l4）、make_engine 装配（u4-l1）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
|------|------|----------|
| `lib/llm/src/http/service/service_v2.rs` | HTTP 服务主体：`HttpService`/`HttpServiceConfig`/`State`、生命周期状态机、inflight 记账、路由表组装 | 主战场 |
| `lib/llm/src/http/service/openai.rs` | OpenAI 兼容端点：chat/completions、embeddings、classify、pooling、responses、`responses/input_tokens`、batch 等全部 handler | 请求处理链样板 + 本次新增端点 |
| `lib/llm/src/http/service/generate.rs` | 实验性引擎原生 `/inference/v1/generate`（token-in/token-out）端点 | 第三类协议端点 |
| `lib/llm/src/http/service/anthropic.rs` | Anthropic Messages API（`/v1/messages`）端点 | 第二类协议端点 |
| `lib/llm/src/http/service/health.rs` | `/health` 与 `/live` 探针 | 观察生命周期的窗口 |
| `lib/llm/src/http/service/metrics.rs` | Prometheus 指标：`InflightGuard`、`HttpQueueGuard`、`ResponseMetricCollector` | 并发与延迟的可观测面 |
| `lib/llm/src/protocols/openai/delta_common.rs` | `DeltaGeneratorState`：chat 与 completions 共享的流式增量生成器状态 | 响应如何拼出来 |
| `lib/llm/src/protocols/openai/chat_completions/delta.rs` | chat 侧 `DeltaGenerator`，内嵌共享状态 | 同上 |
| `lib/llm/tests/responses_http_replay.rs` | Responses 面的完整 HTTP 回放测试，含 `input_tokens` 的四个行为断言 | 新端点的行为规格书 |
| `components/src/dynamo/frontend/main.py` | Python 前端把 CLI 参数灌进 `EntrypointArgs` 再 `make_engine` | 与 u4-l1 的衔接点 |

## 4. 核心概念与源码讲解

### 4.1 HttpService：装配、路由表与生命周期

#### 4.1.1 概念说明

`HttpService` 是 frontend 进程里那个「对外监听端口的 HTTP 服务器」。它要同时挂载三类路由：

- **系统路由**：`/metrics`、`/v1/models`、`/health`、`/live`、`/busy_threshold`、OpenAPI 文档，以及通过 `frontend_route_extensions` 注册的扩展路由；
- **推理路由**：`/v1/chat/completions`、`/v1/completions`、`/v1/embeddings`……直到 Anthropic、Responses 与 Generate 端点；
- **兜底路由**：未匹配路径返回协议兼容的 JSON 404。

每一类路由的中间件（middleware）叠加方式不同——这个差异是有意设计的，是本模块的重点。

另外还有一个**不挂在主路由表上**的例外：当启用 RL（`enable_rl` 或 `DYN_ENABLE_RL`）时，`HttpService` 会在一个**独立端口**上另起一个 `rl_router`，专门服务 RL worker 发现。这就是 u8-l7 会讲的 `/v1/rl/workers` 面。

#### 4.1.2 核心流程

`HttpServiceConfigBuilder::build()` 的组装流程可以概括为：

```text
build()
 ├─ 1. 解析开关：nvext / admin api / vllm generate / sglang generate / rl（env 或 builder flag）
 ├─ 2. State::new()
 │     ├─ ModelManager（模型目录，u4-l4 讲）
 │     ├─ ServiceObserver（生命周期 + inflight 计数，见 4.2）
 │     ├─ StateFlags（13 个端点类别的 AtomicBool 开关）
 │     └─ cancel_token / frontend_api_config / sse_keep_alive
 ├─ 3. 注册 Prometheus 指标（请求、worker 负载、路由队列、请求面、tokio、传输、LoRA…）
 ├─ 4. system_routes = [metrics, models, health, live, (busy_threshold), ...extensions]
 ├─ 5. inference_routes = get_endpoints_router(...)
 │     └─ 每个端点 route 外再包一层「EndpointType 开关检查」中间件
 ├─ 6. inference_router 叠两层：TraceLayer(inference span) + track_inflight_inference
 ├─ 7. system_router 叠一层：TraceLayer(system span)（没有 inflight 中间件！）
 ├─ 8. fallback_service(unmatched_router) + echo_request_id_header
 └─ 返回 HttpService { state, router, port, host, rl_router?, ... }
```

注意第 6、7 步的区别：**只有推理路由会被 `track_inflight_inference` 包住**。也就是说，`/metrics` 和 `/live` 在关停期间仍然可用（Kubernetes 需要存活探针一直应答到最后一刻），而推理路由会在 draining 阶段被拒绝。

#### 4.1.3 源码精读

先看 `HttpService` 与其配置结构。配置用 `derive_builder` 生成 builder 模式，每个字段都带默认值：

[lib/llm/src/http/service/service_v2.rs:620-636](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L620-L636) — `HttpService` 本体：持有的不是「服务器」而是一个已经组装好的 `axum::Router` 加上端口/host/TLS 配置；`rl_router` 是 RL worker 发现专用的第二端口监听器（u8-l7）。

[lib/llm/src/http/service/service_v2.rs:640-741](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L640-L741) — `HttpServiceConfig`：默认端口 8787、host 0.0.0.0；`enable_chat_endpoints`/`enable_cmpl_endpoints` 默认 false（由上层按需打开），`enable_embeddings_endpoints`/`enable_responses_endpoints` 默认 true；`enable_engine_apis`（Generate 端点）、`enable_batch_endpoints`、`enable_rl` 默认 false。注意注释明确说明：Batch API 是占位实现（返回 501），Generate API 是实验性且默认关闭。

共享状态 `State` 是所有 handler 的「世界视图」：

[lib/llm/src/http/service/service_v2.rs:135-146](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L135-L146) — `State` 聚合了指标、模型目录、发现客户端、`ServiceObserver`、13 个端点开关、取消令牌和前端 API 行为配置。

[lib/llm/src/http/service/service_v2.rs:377-391](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L377-L391) — `StateFlags`：每个 `EndpointType`（Chat/Completion/Embedding/…/Batch 共 13 种）对应一个 `AtomicBool`，可以在运行时通过 `enable_model_endpoint` 翻转（Batch 除外）。

推理路由的注册入口：

[lib/llm/src/http/service/service_v2.rs:1427-1478](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L1427-L1478) — `get_endpoints_router` 逐个调用 `super::openai::chat_completions_router(...)` 等函数，把返回的 `(文档, 路由)` 对收进一个以 `EndpointType` 为键的 HashMap。路径全部可用 `DYN_HTTP_SVC_*_PATH` 环境变量覆盖（如 `DYN_HTTP_SVC_CHAT_PATH` 改掉 `/v1/chat/completions`，`DYN_HTTP_SVC_RESPONSES_PATH` 改掉 `/v1/responses`）。

[lib/llm/src/http/service/service_v2.rs:1517-1541](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L1517-L1541) — 关键中间件：每个端点路由外面包了一层闭包，请求进来先查 `state.flags.get(&endpoint_type)`，开关关着就直接返回 404。这就是「端点动态启停」的实现——不需要重建 Router。

三层叠加的顺序（在 build 里）：

[lib/llm/src/http/service/service_v2.rs:1291-1299](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L1291-L1299) — 推理路由先叠 `TraceLayer`（info 级 span），再叠 `track_inflight_inference`；[lib/llm/src/http/service/service_v2.rs:1307-1313](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L1307-L1313) — 系统路由只叠 `TraceLayer`（debug 级 span）。axum 的 layer 是「后加的先执行」，所以 inflight 记账发生在 trace span 内部，日志里能对上号。

[lib/llm/src/http/service/service_v2.rs:1320-1341](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L1320-L1341) — 兜底路由被刻意注册在 `track_inflight_inference` **之外**（源码注释原话：未匹配的请求不应获取推理许可、也不应在 draining 时返回 503，而是返回正常 404）；随后 [lib/llm/src/http/service/service_v2.rs:1338](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L1338) 全局加一层 `echo_request_id_header`，把请求头里的 `x-request-id` 原样带回响应。

RL 例外分支：

[lib/llm/src/http/service/service_v2.rs:1340-1352](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L1340-L1352) — `enable_rl_router` 由 builder flag `enable_rl` 或环境变量 `DYN_ENABLE_RL` 打开；打开但没配 `runtime` 会直接报错。RL 面不混入主路由表——它有自己的端口与生命周期（详见 u8-l7）。

Python 侧怎么用它？衔接 u4-l1：

[components/src/dynamo/frontend/main.py:418-431](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/components/src/dynamo/frontend/main.py#L418-L431) — frontend 把 `http_host`/`http_port`/`enable_anthropic_api`/`reasoning_field_name` 等参数塞进 kwargs，最终以 `EntrypointArgs(EngineType.Dynamic, **kwargs)` 走 `make_engine`。也就是说你在命令行敲的 `--http-port 8000`，会变成 Rust 侧 `HttpServiceConfig` 的 `port` 字段——HTTP 服务本身完全由 Rust 实现，Python 只负责传配置。

#### 4.1.4 代码实践

**实践目标**：把 HttpServiceConfig 的每个开关和真实 HTTP 路由对上号，验证「路径环境变量」与「端点开关」确实生效。

**操作步骤**：

1. 按 u1-l2 的方式启动 sample 聚合拓扑（CPU 即可）：
   ```bash
   cd examples/backends/sample/launch
   DYN_HTTP_PORT=8000 ./agg.sh --model-name sample-model
   ```
2. 在另一个终端探测默认路由表：
   ```bash
   curl -s localhost:8000/v1/models | head
   curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/health
   curl -s localhost:8000/openapi.json | python3 -m json.tool | grep '"summary\|/"' | head -40
   ```
3. 换一个环境变量重启 frontend，观察路径迁移（注意 responses 面现在带一个子路由）：
   ```bash
   DYN_HTTP_SVC_RESPONSES_PATH=/api/v2/responses DYN_HTTP_PORT=8000 python3 -m dynamo.frontend
   # 再试
   curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/v1/responses/input_tokens \
        -H 'content-type: application/json' -d '{"input":"hi"}'      # 预期 404
   curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/api/v2/responses/input_tokens \
        -H 'content-type: application/json' -d '{"input":"hi"}'      # 预期 200
   ```

**需要观察的现象**：默认路径返回 200/405；改了 `DYN_HTTP_SVC_RESPONSES_PATH` 后，`/v1/responses/input_tokens` 变成 OpenAI 风格 JSON 404（来自 `unmatched_route_fallback`），而 `/api/v2/responses/input_tokens` 正常应答——子路由是**从父路径派生**出来的。

**预期结果**：`/openapi.json` 里能看到所有已注册路由（`route_docs` 的产物），且派生子路由遵循「父路径去尾部斜杠再拼接」的规则。若你的安装版本与 HEAD 不一致，具体环境变量名以 [service_v2.rs:1026-1064](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L1026-L1064) 为准。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `/metrics` 和 `/live` 在服务 draining 阶段仍应答 200，而 `/v1/chat/completions` 返回 503？

**答案**：因为 `track_inflight_inference` 中间件只叠加在推理路由上（service_v2.rs:1296-1299），系统路由没有这层；同时 `/live` 的 handler（health.rs:40-61）只检查 `is_cancelled()`（runtime 取消令牌），而 `/health`（health.rs:63-98）与推理中间件检查的是 `is_ready()`（`ServiceStage == Ready`）。draining 阶段 `is_ready()` 已为 false 但 `cancel_token` 还没被 cancel，所以存活探针绿灯、流量探针红灯——这正是滚动更新想要的窗口。

**练习 2**：`enable_model_endpoint(EndpointType::Chat, false)` 之后，`/v1/chat/completions` 返回什么？为什么不是 503？

**答案**：返回 404。get_endpoints_router 给每个端点路由包了一层 `route_layer`（service_v2.rs:1518-1536），handler 执行前先查 `StateFlags`，开关关闭直接 `Err(StatusCode::NOT_FOUND)`。它表达的是「这个能力不存在」而非「暂时不可用」。

**练习 3**：客户端传入的 `x-request-id` 请求头，响应里能拿回来吗？

**答案**：能。`echo_request_id_header`（service_v2.rs:58-68）在进入任何 handler 之前克隆该头，响应生成后再插回去。这是全链路日志关联的基础。

---

### 4.2 InflightPermit 与 ServiceObserver：并发记账与优雅关停

#### 4.2.1 概念说明

这是本讲最需要澄清的一组概念。很多人看到 `InflightPermit` 这个名字会以为它是「并发上限许可证」（semaphore）——**它不是**。读源码会发现：

- `acquire_inflight()` 只是 `fetch_add(1)` 然后返回一个 RAII guard，**没有任何上限判断，永远不会因为并发太高而拒绝请求**；
- 它的真正职责是两个：① 精确统计「响应体还没发完的推理请求数」，供优雅关停等待和 Prometheus 指标使用；② 在关停时把「最后一个响应发完」这件事通知给等待方。

那 HTTP 层的背压在哪里？答案是「分层记账」：`InflightGuard`（在途）、`HttpQueueGuard`（从 handler 开始到首 token 的排队）、`ResponseMetricCollector`（TTFT/ITL）三件套都是**观测器**而非**闸门**。真正的准入控制（admission）发生在更下层——路由器的队列与 worker 侧（u6/u7 会讲到）。HTTP 层唯一会拒绝请求的条件是「服务不在 Ready 阶段」（503）。

另一个关键设计：**许可的生命周期是「整个响应体」而不是「handler 函数」**。对 SSE 流式响应，handler 早早返回了 `Response`，但 token 还在持续吐出——如果把许可绑在 handler 上，关停逻辑会误以为请求已结束。Dynamo 的做法是把许可移进响应体流的 `map` 闭包里，流真正结束（或被 drop）时许可才释放。

#### 4.2.2 核心流程

生命周期是一个三态单向状态机：

\[
\text{Ready} \;(0) \;\xrightarrow{\text{SIGTERM}}\; \text{Draining} \;(1) \;\xrightarrow{\text{inflight}=0\ \text{或超时}}\; \text{Stopping} \;(2)
\]

请求侧的记账与关停等待配合如下：

```text
请求进入推理路由
  └─ track_inflight_inference:
       1. if !is_ready() → 503（不计数，不延长 drain 窗口）
       2. permit = acquire_inflight()          # 计数 +1
       3. if !is_ready() → drop(permit); 503   # 关闭检查与计数之间的竞态窗口
       4. response = next.run(request)          # 真正的 handler
       5. 把 body 转成 stream，闭包捕获 permit  # 许可随 body 存活
       6. body 结束/被 drop → permit.drop()     # 计数 -1
            └─ 若计数归零且 stage != Ready → notify_waiters()

关停路径
  SIGTERM → start_draining()（新请求开始被拒）
          → wait_inflight_zero_or_timeout(5s 默认)
          → start_stopping() → cancel_token.cancel()（runtime 开始拆除）
```

第 3 步的二次检查是为了封死一个竞态：在「readiness 检查通过」与「计数 +1」之间，关停可能恰好介入。如果不复查，这条请求会在 draining 期间悄悄溜进来，把 drain 窗口无限拉长。

#### 4.2.3 源码精读

[lib/llm/src/http/service/service_v2.rs:219-225](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L219-L225) — `ServiceStage` 三态枚举，用 `u8` 存储（0/1/2），配合 `AtomicU8` 实现无锁读取。这个阶段与 runtime 的 `CancellationToken` 是**解耦的**——前端需要先停止收新请求、再拆除发现与传输状态。

[lib/llm/src/http/service/service_v2.rs:259-263](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L259-L263) — `ServiceObserver` 只有两个字段加一个通知器：`stage: AtomicU8`、`inflight_inference: AtomicU64`、`inflight_zero: Notify`。注意 `Notify` 的用法——它不是计数信号量，只是「唤醒所有等在这一点上的任务」的广播器。状态迁移入口是 [service_v2.rs:290-313](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L290-L313) 的 `start_draining`/`start_stopping`，两者都先打 info 日志再 `store`。

[lib/llm/src/http/service/service_v2.rs:319-323](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L319-L323) — `acquire_inflight()` 全文就是 `fetch_add(1)` 加构造 guard，**没有任何 if 判断上限**——这就是「它不限流」的直接证据。[service_v2.rs:335-354](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L335-L354) 的 `wait_inflight_zero_or_timeout` 里的循环值得细读：先 `notified()` 再 `enable()` 再查计数，这个顺序保证「注册等待发生在读计数之前」，最后一个 permit 释放时的 notify 不会丢。

[lib/llm/src/http/service/service_v2.rs:358-374](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L358-L374) — `InflightPermit` 的 `Drop`：`fetch_sub(1, AcqRel)` 返回的是**旧值**，等于 1 说明这次减完是 0；只有「减到 0」且「已不在 Ready 阶段」才 `notify_waiters()`。Ready 阶段不通知是性能优化——正常运行时每次请求结束都广播一遍毫无意义。

[lib/llm/src/http/service/service_v2.rs:103-132](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L103-L132) — `track_inflight_inference` 中间件全文。注意 `body.into_data_stream().map(move |result| { let _permit = &permit; result })` 这个技巧：把 permit 以引用方式捕获进每一帧的闭包，只要还有帧没被消费，permit 就活着。客户端中途断开导致 body 被 drop 时，permit 同样随之释放——不会泄漏。

[lib/llm/src/http/service/service_v2.rs:928-949](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L928-L949) — 非 TLS 路径的优雅关停：`with_graceful_shutdown` 的 future 一旦被触发，先 `start_draining()`，再 `wait_inflight_zero_or_timeout(shutdown_timeout)`（超时时间来自 [service_v2.rs:1017-1023](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L1017-L1023) 的 `DYN_HTTP_GRACEFUL_SHUTDOWN_TIMEOUT_SECS`，默认 5 秒），最后 `start_stopping()` 并 cancel runtime 令牌。

与 `ServiceObserver` 并行的另一套「HTTP 层指标」在 metrics.rs：

[lib/llm/src/http/service/metrics.rs:1375-1410](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/metrics.rs#L1375-L1410) — `create_inflight_guard` / `create_response_collector` / `create_http_queue_guard` 三件套。注意注释里的明确区分：`InflightGuard` 跟踪「引擎正在处理」，`HttpQueueGuard` 跟踪「handler 开始到首 token」（含 prefill 时间）。它们暴露为 `{prefix}_inflight_requests` 等 Prometheus 指标（见 [metrics.rs:615-623](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/metrics.rs#L615-L623)）。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：用并发压力验证三件事——① `InflightPermit` 不会因并发数拒绝请求；② inflight 计数能被 `/metrics` 观测到；③ draining 阶段确实开始返回 503。

**操作步骤**：

1. 启动一个「故意慢」的 sample 集群，让请求持续几秒，方便观察（`--delay` 是每个 token 的间隔，`--max-tokens` 是输出长度）：
   ```bash
   cd examples/backends/sample/launch
   DYN_HTTP_PORT=8000 ./agg.sh --model-name sample-model --delay 0.05 --max-tokens 200
   ```
   （`--delay`/`--max-tokens` 定义见 [components/src/dynamo/common/backend/sample_engine.py:138-139](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/components/src/dynamo/common/backend/sample_engine.py#L138-L139)，多余参数会透传给 sample_main。）
2. 写一个压测脚本 `load.sh`（示例代码，非项目自带）：
   ```bash
   #!/bin/bash
   # 200 并发 × 每条流式请求
   for i in $(seq 1 200); do
     curl -sN localhost:8000/v1/chat/completions \
       -H 'content-type: application/json' \
       -d '{"model":"sample-model","stream":true,
            "messages":[{"role":"user","content":"hello"}]}' \
       > /tmp/resp_$i.out &
   done
   wait
   grep -L '"finish_reason"' /tmp/resp_*.out | wc -l   # 没拿到完整结束帧的响应数
   ```
3. 压测进行中，另开终端采样指标：
   ```bash
   curl -s localhost:8000/metrics | grep -E 'inflight_requests|http_queue' | grep -v '^#'
   ```
4. 压测进行中对 frontend 进程发 SIGTERM（模拟 K8s 滚动更新），立刻再打一发新请求：
   ```bash
   kill -TERM <frontend_pid>
   curl -s -i localhost:8000/v1/chat/completions -H 'content-type: application/json' \
        -d '{"model":"sample-model","messages":[{"role":"user","content":"hi"}]}' | head -1
   curl -s -i localhost:8000/live  | head -1
   ```

**需要观察的现象**：

- 步骤 2：200 条并发请求**没有一条因为并发本身被拒**——全部拿到 200（可能有少量因模型未就绪 404/503，取决于 worker 是否已注册）。
- 步骤 3：`inflight_requests` 这个 gauge 会爬升到一个接近 200 的峰值，随响应完成回落到 0。
- 步骤 4：SIGTERM 之后新请求返回 `503 Service Unavailable`，同时 `/live` 仍返回 200；frontend 日志出现 `frontend service entering draining stage`（service_v2.rs:290-298 的日志）。

**预期结果**：把三个观察写成结论——「HTTP 层不做并发限流，只做生命周期准入 + 观测」。若你的环境里 sample worker 启动较慢，第 2 步前先 `curl localhost:8000/v1/models` 确认模型已列出。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果 200 并发把 frontend 打挂了，`InflightPermit` 会救它吗？应该去哪里找真正的过载保护？

**答案**：不会。`InflightPermit` 无上限、无拒绝逻辑。HTTP 层唯一的拒绝条件是 `!is_ready()`。真正的过载保护在下游：路由器侧的调度与准入（`lib/llm/src/kv_router/scheduler.rs`，u6-l2 讲）、prefill 路由的 admission（u7-l2 讲），以及引擎自身的 batching 上限。

**练习 2**：为什么 `track_inflight_inference` 要做两次 `is_ready()` 检查？去掉第二次会怎样？

**答案**：封死「检查通过之后、计数 +1 之前」的窗口。若去掉第二次检查，一条在 `start_draining()` 前一微秒通过检查的请求会在 draining 期间被计入 inflight，`wait_inflight_zero_or_timeout` 会为它多等最多整个 shutdown 超时。第二次检查发现不 ready 就 `drop(permit)` 并返回 503，保证 drain 窗口不被延长（源码注释原话："Requests rejected during draining should not extend the drain window"）。

**练习 3**：流式请求的 handler 函数早就返回了，为什么 inflight 计数还没归零？

**答案**：因为 permit 被 move 进了 `body.into_data_stream().map(...)` 的闭包（service_v2.rs:127-130），它的生命周期绑定到响应体流而不是 handler 调用。SSE 还在吐 token，流就没结束，permit 就还活着。这是「以响应体为粒度记账」的核心实现。

---

### 4.3 三类协议端点：openai.rs / anthropic.rs / generate.rs

#### 4.3.1 概念说明

Dynamo 的 HTTP 面同时说三种「方言」，差异不在路由而在**请求/响应的语义深度**：

| 维度 | openai.rs | anthropic.rs | generate.rs |
|------|-----------|--------------|-------------|
| 默认路径 | `/v1/chat/completions`、`/v1/completions`、`/v1/responses` 等 | `/v1/messages` | `/inference/v1/generate`（vLLM 风格）、`/generate`（SGLang 风格） |
| 输入形态 | 结构化 OpenAI 请求（messages/text/responses items） | Anthropic Messages（system 分离、`anthropic-version` 头） | **token_ids 进、token_ids 出**（token-in/token-out） |
| 框架做了多少整形 | 最多：模板、校验、reasoning 路由、工具调用解析、nvext 策略 | 中等：Anthropic→内部表示→Anthropic 错误信封转换 | 最少：请求装进**不透明后端信封**，框架不理解内容 |
| 引擎接口 | `OpenAIChatCompletionsStreamingEngine` 等类型化引擎 | 复用 chat 引擎 + 外层转换 | 按能力（capability）发现的 Generate 引擎 |
| 开关 | builder/env 可配 | `enable_anthropic_api`（默认关，实验性） | `DYN_VLLM_ENABLE_INFERENCE_V1_GENERATE` / `DYN_SGLANG_ENABLE_GENERATE`（默认关） |
| 错误格式 | OpenAI 风格 `{code, message, type}` | Anthropic 风格 `{type:"error", error:{type, message}}` | vLLM 风格嵌套 `{error:{message, type, code}}` |

这个「三方言、一套 State」的设计要点是：三类端点共享同一个 `State`（同一套就绪检查、指标、模型目录），只在 handler 内部走各自的协议转换。注意就绪检查的**深度**也分三档：chat 端点做「进程级 + 模型级」双重检查；`input_tokens` 端点（4.5 节）**完全不做**；Generate 端点按能力查到模型后再做模型级检查。

#### 4.3.2 核心流程

以最典型的 `POST /v1/chat/completions` 为例，完整请求链：

```text
axum 路由匹配 POST /v1/chat/completions
 └─ echo_request_id_header                     # 回显 x-request-id
 └─ EndpointType::Chat 开关检查                  # 关了 → 404
 └─ track_inflight_inference                    # 就绪检查 + inflight 许可
 └─ TraceLayer (inference span)
 └─ smart_json_error_middleware                 # 把 axum 原生错误转成 OpenAI JSON
 └─ DefaultBodyLimit                            # DYN_HTTP_BODY_LIMIT_MB 请求体上限
 └─ handler_chat_completions
     ├─ read_json_request_body / parse_json_request    # 解析 + 容错（控制字符转义、UTF-8 修复）
     ├─ check_ready(&state)                            # 进程级就绪 → 503
     ├─ resolve_request_model + check_model_serving_ready  # 模型级就绪 → 503
     ├─ nvext 策略应用（DYN_DISABLE_FRONTEND_NVEXT）
     ├─ context_from_headers_with_input_trigger        # 构造 Context<T> 信封
     ├─ create_connection_monitor                       # 客户端断连监控
     └─ tokio::spawn(chat_completions(...))             # 真正的业务在子任务里
          ├─ apply_chat_completions_request_template    # 请求模板
          ├─ resolve_canonical_name                     # 别名 → 主模型名
          ├─ create_inflight_guard / http_queue_guard    # 指标 guard
          ├─ 5 层字段校验（unsupported/required/stream_options/generic/reasoning args）
          ├─ get_chat_completions_engine_with_parsing   # 从 ModelManager 取引擎
          ├─ engine.generate(request).await             # ← 进入 u3-l3 的 AsyncEngine 体系
          └─ if streaming:
               ├─ pre-commit 错误窥视（DYN_HTTP_PRE_COMMIT_ERROR_PEEK_MS）
               ├─ async_stream::stream! 循环：空块丢弃 / 工具调用提前派发 /
               │   reasoning 累积派发 / EventConverter → SSE event
               ├─ monitor_for_disconnects_with_activity # 断连 & 超时监控
               └─ Sse::new(stream).keep_alive(...)      # 可选心跳
             else:
               ├─ check_for_backend_error（首帧窥视）
               ├─ NvCreateChatCompletionResponse::from_annotated_stream  # 聚合成单条 JSON
               └─ RoutedReasoning::new(...).into_response()
```

「pre-commit 错误窥视」值得单独一提：流式请求一旦开始返回就必然是 HTTP 200，后端错误只能塞在 SSE error 帧里。Dynamo 提供一个可选窗口（`DYN_HTTP_PRE_COMMIT_ERROR_PEEK_MS`），在提交 200 之前短暂窥视第一帧，如果是同步后端错误（例如纯文本模型收到了图片），就还能返回规范的 4xx。

#### 4.3.3 源码精读

OpenAI 侧的 handler 入口：

[lib/llm/src/http/service/openai.rs:1965-2048](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/openai.rs#L1965-L2048) — `handler_chat_completions`：读体、解析、双重就绪检查（进程级 + 模型级，注释解释了为什么聚合请求打到 decode-only 命名空间会挂）、构造 Context、建立断连监控，然后把真正的处理 `tokio::spawn` 出去——handler 本身只负责协议层，业务在子任务中执行，避免长连接拖住 axum worker。

[lib/llm/src/http/service/openai.rs:3837-3842](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/openai.rs#L3837-L3842) — `check_ready`：一行 `is_ready()` 检查，不 ready 返回 503。所有推理 handler 共用。

[lib/llm/src/http/service/openai.rs:3876-3903](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/openai.rs#L3876-L3903) — `check_model_serving_ready`：**模型级**就绪门，与 `check_ready` 是 AND 关系——进程 ready 且该模型在至少一个命名空间里凑齐了完整 worker 集合（每个 worker 的 `needs` DNF 都被该命名空间当前出现的 worker 类型满足，落码为 `model.has_ready_workers()`）才放行。注释特意说明用户可见的错误信息刻意不含内部术语（worker type、namespace），运维细节走 `GET /v1/models/{model}/ready`。

[lib/llm/src/http/service/openai.rs:2794-2878](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/openai.rs#L2794-L2878) — `chat_completions` 业务体前半段：应用请求模板 → 模型别名规范化（alias → primary，注释提到这是为了与 vLLM/SGLang 行为一致）→ 尽早创建 inflight guard（让校验错误也计入指标）→ 五层校验依次执行，任何一层失败都 `mark_error` 后返回。

[lib/llm/src/http/service/openai.rs:2994-3085](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/openai.rs#L2994-L3085) — 流式分支的核心循环：`async_stream::stream!` 内逐帧处理——`enforce_single_tool_call` 时只保留 index 0 的工具调用、`deduplicate_stream_roles` 去重 role、空块（多字节 token 组装产物）被丢弃但仍要记指标（注释解释了不记账会低估输出 token 与 TTFT）、可选的 `tool_call_dispatch`/`reasoning_dispatch` 侧信道事件先行，最后 `EventConverter` 转成 SSE event。结尾 `Sse::new(...).keep_alive(...)` 挂上心跳。

[lib/llm/src/http/service/openai.rs:3087-3135](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/openai.rs#L3087-L3135) — 非流式分支：同样先窥视首帧错误，然后 `NvCreateChatCompletionResponse::from_annotated_stream` 把整条流聚合成单条 JSON；聚合完成后如果发现 context 已被 kill（客户端断开），指标改记 Cancelled。

[lib/llm/src/http/service/openai.rs:4009-4024](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/openai.rs#L4009-L4024) — `chat_completions_router`：标准三件套——`smart_json_error_middleware`（错误 JSON 化）、`DefaultBodyLimit`（请求体上限）、`with_state`。所有 openai.rs 里的 router 构造函数都是这个形状。

Anthropic 侧：

[lib/llm/src/http/service/anthropic.rs:73-92](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/anthropic.rs#L73-L92) — `DEFAULT_MESSAGES_PATH = "/v1/messages"` 与 `anthropic_messages_router`：除了标准三件套还多一层 `anthropic_error_middleware`，把错误转成 Anthropic 的 `{type:"error", error:{...}}` 信封。

[lib/llm/src/http/service/anthropic.rs:249-360](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/anthropic.rs#L249-L360) — `handler_anthropic_messages`：与 chat handler 同构（同样的就绪检查、Context 构造、断连监控），差别在请求校验（`validate_anthropic_messages`/`validate_anthropic_tools`）和响应格式转换。service_v2.rs 里的 `unmatched_route_fallback` 会根据路径是否落在 Anthropic 命名空间内，决定 404 用哪种错误信封（见 [service_v2.rs:83-101](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L83-L101)，`path_within_namespace` 做的是完整路径段匹配，`/v1/messages_beta` 不会误判）。

Generate 侧：

[lib/llm/src/http/service/generate.rs:1-11](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/generate.rs#L1-L11) — 模块文档开宗明义：这是**实验性的引擎原生端点，默认关闭**，`enable_engine_apis` 或 `DYN_VLLM_ENABLE_INFERENCE_V1_GENERATE` 打开；「把完整请求保存在不透明后端信封里」，流式（stream=true）尚未实现。

[lib/llm/src/http/service/generate.rs:66-80](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/generate.rs#L66-L80) — `generate_router`：默认路径 `/inference/v1/generate`，结构与 openai 的 router 一致（连 `smart_json_error_middleware` 都是复用 openai.rs 导出的）。

[lib/llm/src/http/service/generate.rs:583-660](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/generate.rs#L583-L660) — `handler_generate`：校验 → 拒绝 stream=true（501）→ 若请求未指定 model 则按 `VLLM_INFERENCE_V1_GENERATE_CAPABILITY` 能力查找已注册模型（0 个报 404，多个报 400 要求显式指定）→ 模型级就绪检查 → 按**能力**取引擎（这与 openai 按模型名取引擎不同，是为了防止 vLLM 信封误投给 SGLang worker）。

[lib/llm/src/http/service/generate.rs:725-812](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/generate.rs#L725-L812) — `generate_dispatch`：`run_until_killed` 把 `engine.generate(context)` 包在 `tokio::select! { biased; operation, killed }` 里——客户端 kill 信号一到立刻放弃，返回 499（nginx 风格的 request_cancelled）。注释解释了为什么 dispatch 要 `tokio::spawn` 分离：unary 工作必须比 Axum handler 活得久，handler 被 drop 时才能触发已武装的断连监控。

[lib/llm/src/http/service/generate.rs:163-206](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/generate.rs#L163-L206) — `VllmTitoEnvelope`：把 vLLM 特有字段（sampling_params、cache_salt、priority、kv_transfer_params…）序列化进信封；注释明确 `token_ids` 故意不在其中——`PreprocessedRequest.token_ids` 才是路由与线上格式的权威表示，worker 端从它重建 vLLM 请求。

三类端点共用的探针：

[lib/llm/src/http/service/health.rs:40-61](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/health.rs#L40-L61) 与 [lib/llm/src/http/service/health.rs:63-98](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/health.rs#L63-L98) — `/live` 只查 cancel_token（进程还活着吗），`/health` 查 is_ready 并列出发现到的全部实例端点。K8s 的 livenessProbe 应指 `/live`、readinessProbe 应指 `/health`。

#### 4.3.4 代码实践

**实践目标**：用同一组输入对比三类端点的行为差异，直观感受「方言」之别。

**操作步骤**：

1. 用 4.2 节启动的集群（Anthropic 默认关闭，需要单独打开）：
   ```bash
   # OpenAI 面
   curl -sN localhost:8000/v1/chat/completions -H 'content-type: application/json' \
     -d '{"model":"sample-model","stream":true,
          "messages":[{"role":"user","content":"hi"}]}' | head -8

   # 触发一个 404，看 OpenAI 错误信封
   curl -s localhost:8000/v1/not_a_route | python3 -m json.tool

   # 触发 405（GET 一个 POST 路由）
   curl -s -i localhost:8000/v1/chat/completions | head -5
   ```
2. 打开 Anthropic 面重启 frontend（`--enable-anthropic-api`，对应 [main.py:425](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/components/src/dynamo/frontend/main.py#L425) 的 `enable_anthropic_api`），然后：
   ```bash
   curl -s localhost:8000/v1/messages/missing | python3 -m json.tool   # Anthropic 错误信封
   curl -s localhost:8000/v1/messages_beta/missing | python3 -m json.tool  # 仍是 OpenAI 信封
   ```
3. 尝试打开 Generate 端点（预期拿到的不是推理结果，就是明确的拒绝）：
   ```bash
   DYN_VLLM_ENABLE_INFERENCE_V1_GENERATE=1 DYN_HTTP_PORT=8000 python3 -m dynamo.frontend
   curl -s -X POST localhost:8000/inference/v1/generate \
     -H 'content-type: application/json' -d '{"token_ids":[1,2,3],"stream":false}' | head -3
   ```

**需要观察的现象**：OpenAI 信封形如 `{"code":404,"message":"Route not found: ..."}`；Anthropic 信封形如 `{"type":"error","error":{"type":"not_found_error",...}}`；`/v1/messages_beta` 不会因为前缀相似而误用 Anthropic 信封（`path_within_namespace` 做整段匹配）。

**预期结果**：三种错误格式与 4.3.1 表格一一对应。Generate 端点在 sample 后端上大概率返回 404（sample worker 不广播 `VLLM_INFERENCE_V1_GENERATE_CAPABILITY` 能力），这本身就是一个有价值的观察：能力发现防串台。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Generate 端点按 capability 查找引擎，而 chat 端点按模型名查找？

**答案**：因为 Generate 的请求体对框架是不透明的（token_ids + 引擎私有字段），框架无法像 chat 那样从请求语义判断该投给哪个后端；若 vLLM 信封被误投给 SGLang worker，worker 解不开就出错。所以 worker 注册时带上 `VLLM_INFERENCE_V1_GENERATE_CAPABILITY` 或 `SGLANG_GENERATE_CAPABILITY`，路由层按能力过滤，天然防串台。chat 请求是结构化的，模型名 + ModelManager 就够。

**练习 2**：流式请求下，后端在第一帧就报了 `InvalidArgument`，客户端会看到什么？怎样才能让它看到 4xx？

**答案**：默认情况下 SSE 一旦开始就是 HTTP 200，错误会以 `event: "error"` 的 SSE 帧出现（EventConverter 负责识别）。要让同步后端错误表现为规范 4xx，需要设置 `DYN_HTTP_PRE_COMMIT_ERROR_PEEK_MS` 打开 pre-commit 窥视窗口（[openai.rs:2584](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/openai.rs#L2584) 的 `pre_commit_error_peek_timeout` 与 [openai.rs:2965-2982](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/openai.rs#L2965-L2982) 的使用点），在提交 200 之前短暂等待第一帧信号。

**练习 3**：`handler_chat_completions` 为什么用 `tokio::spawn` 把业务包一层，而不是直接 await？

**答案**：为了把「handler 协议层」与「长耗时业务」解耦。connection monitor 需要在业务完成（或失败）后决定是否 disarm；若业务直接在 handler 里 await，客户端断开导致的任务取消会与 axum 的连接管理纠缠。spawn 出去后 handler 只 await JoinHandle，`connection_handle.disarm()` 的语义清晰（openai.rs:2033-2045）。generate.rs 的 dispatch 同理，且注释额外指出 unary 工作必须比 handler 活得久。

---

### 4.4 流式增量序列化：DeltaGeneratorState 共享状态

#### 4.4.1 概念说明

客户端收到的每个 SSE `data:` 帧里是一个 `chat.completion.chunk` JSON 对象。这些对象不是引擎直接产出的——引擎吐的是 `BackendOutput`（token_ids、logprobs、finish_reason……），把这份「裸输出」翻译成 OpenAI 增量格式的是 **DeltaGenerator**。

关键结构：chat 端点和 text completions 端点各有一个 `DeltaGenerator`（分别在 `protocols/openai/chat_completions/delta.rs` 和 `protocols/openai/completions/delta.rs`），但两者的「生成器状态」被抽取到了公共模块 `delta_common.rs` 的 **`DeltaGeneratorState`** 里。这就是本讲标题里说的「共享流式增量序列化器的生成器状态」——共享的是：

- 响应身份三件套：`id`（如 `chatcmpl-<request_id>`）、`object`（`chat.completion.chunk` 或 `text_completion`）、`created` 时间戳、`model` 名；
- token 用量累计：`usage: CompletionUsage`（prompt/completion/total tokens 及其 details）；
- 行为开关：`DeltaGeneratorOptions`（是否带 usage、是否连续 usage、是否 logprobs、token 显示为文本还是 `token_id:<id>`、nvext 响应字段选择）；
- 计时器：`RequestTracker`（TTFT/ITL 按时序记录，供 per-worker 指标）。

而两个端点**不共享**的是各自的增量语义：chat 端有 `emitted_role_choices`（哪些 choice 已发过 role）、`service_tier`；completions 端有自己的文本拼接逻辑。

这个切分让「新增一种 OpenAI 风格端点」时不必重写 usage 统计和 TTFT 计时——本次 #13730 在工具调用 jail 里合成额外 chunk 时，也正是靠这个统一状态结构把 `scrub_synthetic_chunk_metadata`（清掉 usage/llm_metrics/nvext，避免客户端重复计数）做成一处生效的公共函数（见 `protocols/openai/chat_completions.rs`）。

#### 4.4.2 核心流程

```text
chat_completions handler
 └─ request.response_generator(request_id)          # chat_completions/delta.rs:27-37
      ├─ DeltaGeneratorOptions::new(stream_options, return_tokens_as_token_ids,
      │                              enable_logprobs, nvext)   # 从请求推导开关
      └─ DeltaGenerator::new(model, options, request_id)
           └─ DeltaGeneratorState::new("chatcmpl-{id}", "chat.completion.chunk", model, options)

每个 BackendOutput 到达
 ├─ state.update_isl(isl)                            # prompt token 数
 ├─ state.update_usage_from_backend_output(output)   # 累加 completion tokens；
 │                                                      backend 给了 completion_usage 就以它为准
 ├─ generator.create_logprobs(...)                   # 可选
 └─ 产出 NvCreateChatCompletionStreamResponse { id, object, created, model, choices, usage? }

流结束时
 └─ state.get_usage()  →  total = prompt.saturating_add(completion)
```

一个协议细节：OpenAI 规定**非流式响应必须带 usage**。`enable_usage_for_nonstreaming(&mut stream_options, original_stream_flag)` 在原始请求不是流式时强制把 `include_usage` 置 true，即使客户端没要。

#### 4.4.3 源码精读

[lib/llm/src/protocols/openai/delta_common.rs:19-49](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/protocols/openai/delta_common.rs#L19-L49) — `DeltaGeneratorOptions` 与它的构造函数：四个开关全部从请求推导——`include_usage`/`continuous_usage_stats` 来自 `stream_options`，logprobs 来自请求参数，nvext 响应字段来自扩展头。

[lib/llm/src/protocols/openai/delta_common.rs:50-100](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/protocols/openai/delta_common.rs#L50-L100) — `DeltaGeneratorState` 本体与构造：记录 created 时间戳（注释幽默地指出 u32 秒级时间戳到 2106 年才溢出）、usage 清零、无条件创建 `RequestTracker`——注释特别强调：即使 `response_fields` 不让 nvext 字段回给客户端，tracker 仍然内部记录 TTFT/ITL 供指标使用。

[lib/llm/src/protocols/openai/delta_common.rs:134-168](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/protocols/openai/delta_common.rs#L134-L168) — usage 更新逻辑：`completion_tokens` 按 token_ids 长度累加（注释再次解释 u32 精度足够）；若 backend 提供了 `completion_usage`，则以它为准覆盖 prompt_tokens——注释说明这对 embedding 场景至关重要（prompt 长度由 worker 按 embedding 序列长度算出）。[delta_common.rs:170-183](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/protocols/openai/delta_common.rs#L170-L183) 的 `get_usage` 用 `saturating_add` 防溢出。

[lib/llm/src/protocols/openai/delta_common.rs:186-214](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/protocols/openai/delta_common.rs#L186-L214) — `enable_usage_for_nonstreaming`（非流式强制带 usage，满足 OpenAI 规范；带 `original_stream_flag` 参数，流式直接返回）与 `force_include_usage`（无条件开启）。文件尾部的单测（217-244 行）验证了「缺失时插入、已存在时只翻转 include_usage 且保留兄弟字段」两个行为。

chat 侧如何消费共享状态：

[lib/llm/src/protocols/openai/chat_completions/delta.rs:40-62](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/protocols/openai/chat_completions/delta.rs#L40-L62) — chat 的 `DeltaGenerator`：字段就三个——共享的 `state`、可选 `service_tier`、`emitted_role_choices: HashSet<u32>`（记录哪些 choice 的 role 帧已发过，配合 handler 侧的 `deduplicate_stream_roles` 防止 role 重复）。所有身份/usage 访问都是对 `state` 的一行委托（`update_isl`、`tracker` 都是转发）。

[lib/llm/src/protocols/openai/chat_completions/delta.rs:19-38](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/protocols/openai/chat_completions/delta.rs#L19-L38) — `NvCreateChatCompletionRequest::response_generator`：handler 用它从请求一键构造生成器；`object` 字段在这里固定为 `"chat.completion.chunk"`（completions 端点会传不同的 object 字符串，这就是两个端点共享状态却产出不同格式的方式）。

#### 4.4.4 代码实践

**实践目标**：亲眼看到 usage 的两种行为——流式按需、非流式强制。

**操作步骤**：

1. 对同一个模型分别发流式与非流式请求，对比最后一帧：
   ```bash
   # 非流式：响应里必有 usage
   curl -s localhost:8000/v1/chat/completions -H 'content-type: application/json' \
     -d '{"model":"sample-model",
          "messages":[{"role":"user","content":"hello"}]}' \
     | python3 -c 'import json,sys; d=json.load(sys.stdin); print("usage:", d.get("usage"))'

   # 流式不带 stream_options：最后一帧 data 里 usage 应为 null
   curl -sN localhost:8000/v1/chat/completions -H 'content-type: application/json' \
     -d '{"model":"sample-model","stream":true,
          "messages":[{"role":"user","content":"hello"}]}' | tail -4

   # 流式显式带 include_usage：最后一帧应有 usage
   curl -sN localhost:8000/v1/chat/completions -H 'content-type: application/json' \
     -d '{"model":"sample-model","stream":true,
          "stream_options":{"include_usage":true},
          "messages":[{"role":"user","content":"hello"}]}' | tail -4
   ```
2. （源码阅读型）打开 `lib/llm/src/protocols/openai/completions/delta.rs:39-60`，对比它构造 `DeltaGeneratorState` 时传的 `object` 字符串与 chat 侧的不同，并在笔记里记下两个端点各自多了哪些自有字段。

**需要观察的现象**：三种请求的 usage 出现情况不同；每个 chunk 的 `id` 保持一致（形如 `chatcmpl-...`）、`object` 恒为 `chat.completion.chunk`、`model` 回显规范化后的模型名。

**预期结果**：非流式必有 usage（`enable_usage_for_nonstreaming` 生效）；流式仅在显式 `include_usage` 时于末帧给出 usage。chunk 之间 id 一致、created 一致——这正是共享 `DeltaGeneratorState` 的直接体现。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么把状态抽到 `delta_common.rs` 而不是让两个 `DeltaGenerator` 各自维护一份字段？

**答案**：chat 与 completions 两个端点对「响应身份、usage 累计、选项、TTFT 计时」的需求完全一致，差异只在增量语义（choice 结构、role 去重、文本拼接）。抽出共享状态后，修一个 usage 统计 bug（例如 backend completion_usage 覆盖逻辑）只改一处；新增端点也能直接复用——#13730 给工具调用 jail 合成的 chunk 清理元数据（`scrub_synthetic_chunk_metadata`）就是在这层公共结构上一次处理两个端点的。

**练习 2**：客户端没传 `stream_options`，为什么非流式响应里还是有 usage？

**答案**：请求侧调用 `enable_usage_for_nonstreaming(&mut stream_options, original_stream_flag)`（delta.rs:20-25 转发到 delta_common.rs:190-205），它在非流式时把 `stream_options.include_usage` 强制置 true（缺失则先插入默认结构），随后 `DeltaGeneratorOptions::new` 读到 true，`get_usage` 的结果就会进入最终 JSON。注释明确说这是为了符合 OpenAI API 规范「非流式响应必须包含 usage」。

**练习 3**：`RequestTracker` 在客户端没开启任何 nvext 字段时也被创建，为什么？

**答案**：见 delta_common.rs:85-88 的注释——tracker 的数据同时服务于内部 per-worker 指标（`last_ttft`、`last_itl`，由 `ResponseMetricCollector` 在 handler 侧消费），`response_fields` 只控制哪些字段回给客户端。观测永远开启，回显按需开关。

---

### 4.5 输入 token 预估端点：POST /v1/responses/input_tokens（本次新增）

#### 4.5.1 概念说明

本次 #13768 给 Responses 面新增了一个子端点：`POST /v1/responses/input_tokens`。它的用途很朴素——**在不发起任何推理的情况下，估算一条请求会消耗多少输入 token**。

谁需要它？主要是两类调用方：

- **RL rollout 编排器**：在把一批 prompt 派给 worker 之前先做预算核算（u8-l7 会看到完整的 RL 服务面）；
- **客户端预检**：想在发送前判断请求是否超出上下文窗口，避免等一个注定失败的请求。

它的三个设计决定都值得记住：

1. **不做就绪检查**。handler 里既没有 `check_ready` 也没有 `check_model_serving_ready`。源码注释解释了原因：客户端经常拿本 frontend 并不服务的路由名来问，而一个 pre-flight 估算也不需要活着的模型。
2. **估算完全本地完成**，用 `len/3` 启发式，永远不会把请求转发到后端引擎（回放测试里有专门断言：估算后引擎收到的请求数必须为 0）。
3. **计数范围与真实请求一致**：`input`、`instructions`、`tools`、工具调用会话 item（`function_call`/`function_call_output`）都计入，且与 chat 转换器共享同一套「coalescing」模型——并行工具调用只记一次 assistant 角色标记，因为转换器会把它们合并成一条 assistant 消息。

返回体形如 `{"object": "response.input_tokens", "input_tokens": N}`。

#### 4.5.2 核心流程

```text
POST /v1/responses/input_tokens
 └─ echo_request_id_header / EndpointType::Responses 开关 / track_inflight_inference
 └─ smart_json_error_middleware + DefaultBodyLimit（与父路由同一套中间件）
 └─ handler_responses_input_tokens
      ├─ read_json_request_body       # 读体（坏 JSON → 400，与 /v1/responses 同款信封）
      ├─ parse_json_request::<CountInputTokensRequest>
      └─ request.estimate_tokens()    # 本地启发式：input/instructions/tools 全计入
           └─ Json(CountInputTokensResponse::new(n))   # {"object":"response.input_tokens","input_tokens":n}
```

注意它**仍然**会经过 `track_inflight_inference`——因为它是 Responses 端点族的一部分，draining 期间同样返回 503。它跳过的只是 handler 内部的业务级就绪门。

路由派生也有一个值得学的小细节：子路径是从父路径**去掉尾部斜杠后拼接**的。如果配了 `DYN_HTTP_SVC_RESPONSES_PATH=/custom/`，父路由 `/custom/` axum 能匹配，但朴素拼接会注册出 `/custom//input_tokens`——axum 不认为这等价于客户端实际会调用的 `/custom/input_tokens`。所以派生时 `trim_end_matches('/')`。

#### 4.5.3 源码精读

[lib/llm/src/http/service/openai.rs:3266-3284](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/openai.rs#L3266-L3284) — `handler_responses_input_tokens` 全文。doc 注释写明了三件事：用 `len/3` 启发式；类比 Anthropic 的 `/v1/messages/count_tokens`；**刻意**不做 readiness 与 model-serving 检查（客户端常拿本 frontend 不服务的路由名来问，且 pre-flight 估算不需要活模型）。函数体只有三行：读体 → 解析 → 返回估算。

[lib/llm/src/http/service/openai.rs:4268-4292](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/openai.rs#L4268-L4292) — `responses_router`：父路径 `/v1/responses` 与派生子路径 `/v1/responses/input_tokens` 注册在**同一个** Router 上，共享 `smart_json_error_middleware` 与 `DefaultBodyLimit`。`input_tokens_path = format!("{}/input_tokens", path.trim_end_matches('/'))` 旁边的大段注释解释了尾部斜杠问题；`RouteDoc` 两条都返回，所以 `/openapi.json` 能看到子端点。

估算逻辑本身在 `dynamo-protocols` crate 的 `CountInputTokensRequest::estimate_tokens`（外部依赖，不在本仓库），行为规格由回放测试钉死：

[lib/llm/tests/responses_http_replay.rs:606-625](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/tests/responses_http_replay.rs#L606-L625) — `input_tokens_counts_input`：断言返回 `object == "response.input_tokens"`、计数 > 0，并且**引擎收到的请求数为 0**（"Counting is pre-flight only; it must never reach a backend"）。

[lib/llm/tests/responses_http_replay.rs:629-656](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/tests/responses_http_replay.rs#L629-L656) — `input_tokens_does_not_gate_on_model`：用一个 frontend 不服务的模型名、以及完全不带 `model` 字段，都必须返回 200。这是「不做模型级就绪检查」的行为规格。

[lib/llm/tests/responses_http_replay.rs:659-693](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/tests/responses_http_replay.rs#L659-L693) — `input_tokens_counts_instructions_and_tools`：同一 `input` 加上 `instructions` 与 `tools` 后计数必须变大——证明估算范围覆盖了完整请求而非只有 `input` 字段。

[lib/llm/tests/responses_http_replay.rs:695-722](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/tests/responses_http_replay.rs#L695-L722) — `input_tokens_rejects_malformed_json`：坏 JSON 返回 400，错误信封与 `/v1/responses` 完全一致（`code:400`、`type:"Bad Request"`）。

[lib/llm/tests/responses_http_replay.rs:724-800](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/tests/responses_http_replay.rs#L724-L800) — 工具调用会话 item 的两个测试：`function_call`/`function_call_output`（不带 id/status 的转换器产物形状）必须被计入；且 `parallel_tool_calls_are_one_assistant_message_for_both_endpoints` 把同一批 item 同时发给 `input_tokens` 与 `/v1/responses`，用引擎实际收到的 chat 消息作为「应记几次 assistant 角色标记」的真值——钉死估算器与转换器的 coalescing 等价性。

顺带一提 metrics.rs 侧的配套变化：本次 #13730 在 [lib/llm/src/http/service/metrics.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/metrics.rs) 的测试里补了一条回归——「无 choice 的 nvext 元数据帧（如 `engine_data.prompt_token_ids`）必须原样到达客户端 SSE」，并在测试中改用类型化的 `process_chat_response_using_event_converter_and_observe_metrics`（[metrics.rs:2174](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/metrics.rs#L2174)）。这保证了 RL 需要的 token 级元数据不会被指标层吞掉——与 `input_tokens` 一起构成对 rollout 场景的支撑。

#### 4.5.4 代码实践

**实践目标**：验证新端点的四个行为——① 不触达后端；② 不做模型门；③ 覆盖 instructions/tools；④ 估算与真实分词的偏差量级。

**操作步骤**：

1. 启动 sample 集群（同 4.2 节），然后：
   ```bash
   # 基线
   curl -s -X POST localhost:8000/v1/responses/input_tokens \
     -H 'content-type: application/json' \
     -d '{"model":"sample-model","input":"List /tmp"}' | python3 -m json.tool

   # 加上 instructions 与 tools，计数应变大
   curl -s -X POST localhost:8000/v1/responses/input_tokens \
     -H 'content-type: application/json' \
     -d '{"model":"sample-model","input":"List /tmp",
          "instructions":"You are a careful filesystem assistant.",
          "tools":[{"type":"function","name":"list_directory",
                    "description":"test tool",
                    "parameters":{"type":"object","properties":{"path":{"type":"string"}},
                                  "required":["path"]}}]}' | python3 -m json.tool

   # 用一个本 frontend 不服务的模型名 + 不带 model
   curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/v1/responses/input_tokens \
     -H 'content-type: application/json' \
     -d '{"model":"some/other/model","input":"hi"}'
   curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/v1/responses/input_tokens \
     -H 'content-type: application/json' -d '{"input":"hi"}'

   # 坏 JSON
   curl -s -X POST localhost:8000/v1/responses/input_tokens \
     -H 'content-type: application/json' -d '{"input": ' | python3 -m json.tool
   ```
2. 对比估算与真实分词（示例代码，非项目自带）：
   ```python
   # pip install transformers  之后对同一批 prompt 做真实分词
   import json, urllib.request, transformers
   tok = transformers.AutoTokenizer.from_pretrained("<你本地有的任一模型>")
   prompts = ["List /tmp", "Explain KV-cache disaggregation in three sentences.", "hi"]
   for p in prompts:
       body = json.dumps({"input": p}).encode()
       req = urllib.request.Request("http://localhost:8000/v1/responses/input_tokens",
                                    data=body, headers={"content-type": "application/json"})
       est = json.load(urllib.request.urlopen(req))["input_tokens"]
       real = len(tok(p)["input_ids"])
       print(f"{p[:30]!r:35} est={est:4} real={real:4} ratio={est/max(real,1):.2f}")
   ```
3. 同时观察 worker 进程日志（或 frontend 的请求日志），确认估算请求期间**没有**生成调用发生。

**需要观察的现象**：四条 curl 分别返回——递增的两个计数、两个 200、一个 400（`type:"Bad Request"`）。估算值与真实分词数同数量级但不相等（len/3 启发式对英文偏准、对中文/代码偏差更大）。

**预期结果**：整理成一张「估算 vs 真实」的小表，并写一行结论说明该端点适合做预算控制而不适合做精确计费。若 `transformers` 不可用，可退化为「估算值 ≈ 字符数/3」的手工核对。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：`input_tokens` 端点为什么不做 `check_ready` / `check_model_serving_ready`，而 chat 端点必须做？

**答案**：两个原因。其一，语义不同：chat 端点要把请求投给真实 worker，投错（如聚合请求打到 decode-only 命名空间）会挂起或崩溃，所以需要模型级就绪门；`input_tokens` 只做本地算术，不需要活着的模型。其二，调用方不同：客户端（尤其是路由层/编排器）经常拿本 frontend 不服务的模型名来预检，若在这里做模型门，预检会失败。代价是它仍受 draining 限制（因为在 `track_inflight_inference` 之内），这是合理的——关停中的进程不该再接任何请求。

**练习 2**：如果把 `DYN_HTTP_SVC_RESPONSES_PATH` 设成 `/custom/`（带尾部斜杠），`/custom/input_tokens` 还能访问吗？为什么？

**答案**：能。`responses_router` 派生子路径时用 `path.trim_end_matches('/')` 先去掉尾部斜杠再拼 `/input_tokens`（openai.rs:4277-4284），注册出来的是 `/custom/input_tokens`。如果不做这个 trim，会注册出 `/custom//input_tokens`，而 axum 不把它当作等价路径，客户端实际调用的地址就会 404。父路径本身仍按原样（带斜杠）注册，所以已有配置行为不变。

**练习 3**：为什么回放测试要专门断言「估算后引擎收到的请求数为 0」，还要断言「并行工具调用只记一次 assistant 消息」？

**答案**：前者钉住「pre-flight only」的契约——一旦有人图省事把估算改成走一遍真实 preprocessor + 引擎，这条测试会立刻红，因为它会把估算成本变成一次真实（可能很贵的）分词/推理。后者钉住估算器与 chat 转换器的**等价性**：转换器 `convert_input_items_to_messages` 会把连续的 assistant 侧 item 合并成一条消息，估算器只对「已 flush 的 pending assistant 消息」记一次角色标记。两边规则一旦漂移，估算就会系统性偏高或偏低，而这个偏差在 RL 预算场景里会直接变成超额 token 消耗。

## 5. 综合实践

把本讲五个模块串成一条完整的「请求考古」任务：

**任务**：对一条 `curl -N .../v1/chat/completions`（stream=true）请求，产出一张「时间线表」，左侧是客户端可观察的现象，右侧是对应的源码位置。

建议步骤：

1. 启动慢速 sample 集群（4.2 节的命令，`--delay 0.05 --max-tokens 100`）。
2. 带上 `x-request-id: demo-001` 发流式请求，用 `curl -N` 观察：
   - 响应头里是否回显了 `x-request-id`（→ `echo_request_id_header`）；
   - 首帧到达前的等待（→ 引擎 generate + pre-commit 窥视未开启时直接提交 200）；
   - 每帧的 `id`/`object`/`created` 是否恒定（→ `DeltaGeneratorState`）；
   - 末帧的 `finish_reason` 与 usage（→ `stream_options` 与 `enable_usage_for_nonstreaming` 对照）。
3. 压测期间采样 `/metrics`，记录 `inflight_requests` 的峰值与回落（→ `InflightPermit`）。
4. 压测中发 SIGTERM，记录：新请求的状态码（503）、`/live` 的状态码（200）、frontend 日志中的 draining/stopping 两条日志、在途流式请求是被完整发完还是被截断（→ `ServiceStage` 状态机 + `wait_inflight_zero_or_timeout`）。
5. 最后对同一条 prompt 调一次 `/v1/responses/input_tokens`，把估算值写进时间线的最左端——它是这条请求「还没发生时」就能知道的第一个数字（→ `handler_responses_input_tokens`）。
6. 把 5 个观察各写一行「现象 → 源码文件:行号」的对照，作为本讲的产出物。

如果只能做静态分析（无运行环境），改为：通读 [service_v2.rs:103-132](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/service_v2.rs#L103-L132) 与 [openai.rs:2994-3085](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/openai.rs#L2994-L3085)，手绘这条时间线并标注每一步的函数名。

## 6. 本讲小结

- **HttpService 是「一个 Router + 一份共享 State」**：`HttpServiceConfig` 用 builder 声明式描述端点开关与路径，`build()` 组装系统路由（无 inflight 中间件）、推理路由（有 inflight 中间件 + trace）与协议兼容的 404 兜底，路径全部可用 `DYN_HTTP_SVC_*_PATH` 覆盖；RL 发现面走独立端口与独立 Router。
- **`InflightPermit` 是记账器不是限流器**：`acquire_inflight()` 无上限、无拒绝，职责是精确追踪「响应体未完成」的请求数，服务优雅关停与 Prometheus 指标；真正的准入在路由器与引擎侧。
- **生命周期三态机 Ready→Draining→Stopping** 与 runtime 取消令牌解耦：draining 期间推理路由 503、`/live` 仍 200、`/health` 503，`wait_inflight_zero_or_timeout`（默认 5s，可用 `DYN_HTTP_GRACEFUL_SHUTDOWN_TIMEOUT_SECS` 调）等在途响应发完。
- **许可以「响应体」为生命周期**：permit 被 move 进 body 流的 map 闭包，SSE 没吐完就不释放，客户端断连导致 body drop 时同样正确归还。
- **三类协议端点共享一套 State，差异在 handler 内部**：openai.rs 做最深的整形与校验，anthropic.rs 是同构 handler + 协议信封转换，generate.rs 是「不透明信封 + 按能力路由」的实验性引擎原生通道。
- **流式增量由 DeltaGenerator 产出，状态抽在 delta_common.rs**：`DeltaGeneratorState` 统一持有响应身份/usage/选项/TTFT 计时，chat 与 completions 两个端点共享它、只各自实现增量语义；非流式强制带 usage 以符合 OpenAI 规范。
- **本次新增 `POST /v1/responses/input_tokens`**：本地 len/3 估算、不做就绪/模型门、绝不触达后端，与 `instructions`/`tools`/工具调用 item 全额计入，并对齐 chat 转换器的消息合并规则——为 RL rollout 预算与客户端预检提供零成本预检。

## 7. 下一步学习建议

- **下一讲（u4-l3）**顺着「引擎拿到请求之前」往前走：`OpenAIPreprocessor` 如何做模板渲染、媒体计数与分词（本次 #13730 对它做了 nvext 透传与 tool-call jail 的大重构），`LocalModel` 如何加载 tokenizer——那是 handler 调 `engine.generate` 之后、真正到达 worker 之前的整形层。
- 想看「指标三件套怎么被消费」，直接读 [lib/llm/src/http/service/metrics.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/metrics.rs) 中 `InflightGuard`/`HttpQueueGuard`/`ResponseMetricCollector` 的实现，并对照 u12-l1 的可观测性讲义。
- 想追 `input_tokens` 的完整动机与 RL 服务面（`/v1/rl/workers`、nvext token-in/token-out、`RlWorkerMetadata`），直接跳到 u8-l7；估算器与 chat↔Responses 转换器的等价性测试就在 [responses_http_replay.rs:773-800](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/tests/responses_http_replay.rs#L773-L800)。
- 想理解「请求进入引擎之后怎么跨进程」：回到 u3-l4（请求面 Pipeline）与 u3-l3（AsyncEngine 抽象），本讲的 `engine.generate(request)` 就是那两条链的入口。
- 建议动手实验：把 `DYN_HTTP_GRACEFUL_SHUTDOWN_TIMEOUT_SECS` 调成 1 秒重做 4.2 的实践，观察在途长流被截断的行为差异；再用 `DYN_HTTP_SVC_RESPONSES_PATH` 带尾部斜杠重启，验证 4.5 练习 2 的派生规则。
