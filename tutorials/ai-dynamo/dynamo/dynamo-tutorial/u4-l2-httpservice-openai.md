# HttpService：OpenAI 兼容 HTTP 服务

> 本讲为 update 版本（对应 HEAD `d7f06b591`）。相对上一版（`b4338ab8`）只有两处代码变化：**#11385** 给 `/inference/v1/generate` 端点补齐了完整指标——generate.rs（+1150 行）新增 `GenerateMetricCollector`/`GenerateMetricLifecycle`，复用 metrics.rs 既有的 `InflightGuard`/`HttpQueueGuard`/`ResponseMetricCollector` 三件套，并把 `input_tokens`/`output_tokens`/`ttft_ms`/`avg_itl_ms`/`prefill_worker_id`/`decode_worker_id` 记上 `request_span`；**#13984** 让 `protocols/common.rs` 的 `FinishReason` 反序列化防御性接受裸 `"error"` 字符串（含 msgpack 形式），落回诊断消息。本版新增 4.8（generate 指标生命周期）与 4.9（FinishReason 宽容反序列化）两节，刷新 generate.rs 的全部行号与全篇永久链接；其余文件（service_v2.rs、openai.rs、metrics.rs、delta_common.rs、embeddings.rs、preprocessor.rs）本轮零改动，行号与上一版一致。更早的历史改动（#12157 embeddings 编解码收拢、#12562 usage 取 max、#13930 删除字符串前缀恢复）仍体现在 4.4/4.6/4.7 三节中。

## 1. 本讲目标

上一讲（u4-l1）我们弄清了「引擎如何被装配起来」。本讲顺着装配链往下走一步，回答八个问题：

1. 一条 HTTP 请求进入 frontend 之后，**在到达引擎之前**经历了什么？——路由匹配、就绪检查、并发记账、请求解析、指标打点，这条链全部在 `lib/llm/src/http/` 里。
2. Dynamo 同时暴露 OpenAI、Anthropic、引擎原生 Generate 三类协议端点，它们**各自的处理套路差异**在哪里？
3. 流式响应（SSE）里的每一个 `chat.completion.chunk` 是谁拼出来的？——答案是 `DeltaGenerator`，而它的「生成器状态」被 chat 与 completions 两个端点共享，usage 统计在 #12562 后有了新的「取 max」语义。
4. `POST /v1/responses/input_tokens` 端点为什么**故意不做任何就绪检查**？它如何在不惊动任何 worker 的情况下估算输入 token 数？
5. Embeddings 端点为什么在内部链路上传 **base64 字节**而不是 JSON 浮点数组？编解码现在住在哪个模块？
6. #13930 之后，后端的**带类型错误**（如 `Backend(InvalidArgument)` → 400）是如何一路活着到达客户端的？为什么 HTTP 层不再需要「字符串前缀恢复」这种补丁？
7. #11385 之后，**绕过分词器/后处理管线**的 `/inference/v1/generate` 端点如何补齐指标？`GenerateMetricLifecycle` 三件套在哪个时机挂上、`ttft_ms` 等字段又是怎么「长」到 tracing span 上的？
8. #13984 之后，`FinishReason` 为什么从「故意拒绝裸 `error`」翻转为「防御性接受并落回诊断消息」？这和滚动更新有什么关系？

读完本讲，你应该能对着一条 curl 请求说出它在源码中经过的每一个函数名，并且能解释四个容易被误解的点：**`InflightPermit` 不是限流器**、**错误类型信息不是靠字符串约定传递的**、**span 上的延迟字段是在 collector 被 drop 时才补写的**、**宽容反序列化是在为 N-2 混部买单**。

## 2. 前置知识

- **axum**：Rust 生态中基于 `tokio` 的 Web 框架。你只需要知道三件事：`Router` 负责把「HTTP 方法 + 路径」映射到 handler 函数；`State` 是被所有 handler 共享的只读上下文（通常包在 `Arc` 里）；`middleware::from_fn` 可以在请求进出 handler 之间插入一层逻辑，类似洋葱模型。
- **SSE（Server-Sent Events）**：一种 HTTP 长连接流式协议，服务端不断写入 `data: {...}\n\n` 帧。OpenAI 的 `stream=true` 响应就是 SSE。
- **RAII guard**：Rust 的资源管理惯例——把「计数 +1」放在构造函数里、「计数 -1」放在 `Drop` 里，guard 变量离开作用域时自动归还。`InflightPermit` 就是这个模式。
- **AtomicU64 与 AcqRel 内存序**：跨线程计数器。`fetch_add`/`fetch_sub` 是原子自增自减；`Acquire/Release` 保证「写入对其他线程可见」的顺序。
- **优雅关停（graceful shutdown）**：进程收到 SIGTERM 后不立刻断连，而是「先拒绝新请求 → 等在途响应发完 → 再真正退出」，Kubernetes 滚动更新依赖这个行为。
- **token 估算的「len/3」启发式**：不做真实分词，直接按「字符数 ÷ 3」给出输入 token 的粗略估计。这是 RL rollout 预算与客户端预检常用的低成本近似。
- **base64 与 little-endian f32**：把 4 字节的 IEEE 754 单精度浮点数按小端序排成字节串，再做 base64 编码——这是 embeddings 向量在内部链路上的线格式，比 JSON 浮点数组省得多。
- **tracing span 与「先声明、后记录」字段**：`tracing::info_span!` 里用 `field::Empty` 声明的字段 initially 为空，之后可以随时 `span.record("ttft_ms", ...)` 补值——4.8 节会看到 Dynamo 用这个机制在指标 collector 被 drop 时一次性补写请求摘要。`.instrument(span)` 让一个 future 的所有日志与 drop 都发生在该 span 之内，`span.in_scope(|| ...)` 则让一段同步代码暂时进入该 span。
- **Prometheus 指标三形态**：counter（只增计数，如 `output_tokens_total`）、gauge（可升可降的当前值，如 `active_requests`）、histogram（分桶观察，如 `time_to_first_token_seconds`）。每个指标带一组 label（如 `model`/`endpoint`），**label 的取值集合决定基数（cardinality）**——这就是 4.8 节要把未知模型名收敛成 `unknown_model` 哨兵的原因。
- **msgpack 与自描述格式**：`rmp_serde::to_vec_named` 是请求面 codec 用的 msgpack 编码，属于「自描述」格式——反序列化器能从线数据本身判断「这是一段字符串还是一个 map」，`deserialize_any` 因此可用（4.9 节的 `FinishReason` 依赖这一点）。
- **N-2 混部兼容**：`lib/llm/AGENTS.md` 规定 frontend 与 worker 在当前版本与往前两个版本之间任意组合都必须可互操作，原则是「宽容的读者、保守的写者」。4.9 节的 `FinishReason` 变更正是这条原则的落地。
- 前置讲义术语：`Context<T>` 信封（u3-l3）、`AsyncEngine::generate`（u3-l3）、`ModelManager` 与 worker 就绪（u4-l1/u4-l4）、make_engine 装配（u4-l1）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
|------|------|----------|
| `lib/llm/src/http/service/service_v2.rs` | HTTP 服务主体：`HttpService`/`HttpServiceConfig`/`State`、生命周期状态机、inflight 记账、路由表组装 | 主战场 |
| `lib/llm/src/http/service/openai.rs` | OpenAI 兼容端点：chat/completions、embeddings、classify、pooling、responses、`responses/input_tokens`、batch 等全部 handler，以及后端错误提取 | 请求处理链样板 + 错误提取 |
| `lib/llm/src/http/service/generate.rs` | 实验性引擎原生 `/inference/v1/generate`（token-in-token-out）端点；#11385 后自带完整指标生命周期（`GenerateMetricCollector`/`GenerateMetricLifecycle`）与 `request_span` 打点 | 第三类协议端点 + 4.8 节主战场 |
| `lib/llm/src/http/service/anthropic.rs` | Anthropic Messages API（`/v1/messages`）端点 | 第二类协议端点 |
| `lib/llm/src/http/service/health.rs` | `/health` 与 `/live` 探针 | 观察生命周期的窗口 |
| `lib/llm/src/http/service/metrics.rs` | Prometheus 指标：`InflightGuard`、`HttpQueueGuard`、`ResponseMetricCollector`（其 `Drop` 会把请求摘要补写进当前 span） | 并发与延迟的可观测面 |
| `lib/llm/src/http/service/error.rs` | `overload_status_code()`（`DYN_HTTP_OVERLOAD_STATUS_CODE`） | 错误状态码策略 |
| `lib/llm/src/protocols/common.rs` | 跨端点共享协议类型；本讲关注 `FinishReason` 的线契约与宽容反序列化（#13984） | 4.9 节主战场 |
| `lib/llm/src/protocols/openai/delta_common.rs` | `DeltaGeneratorState`：chat 与 completions 共享的流式增量生成器状态（含 #12562 的 usage 取 max） | 响应如何拼出来 |
| `lib/llm/src/protocols/openai/chat_completions/delta.rs` | chat 侧 `DeltaGenerator`，内嵌共享状态与 usage 语义测试 | 同上 |
| `lib/llm/src/protocols/openai/embeddings.rs` | `NvCreateEmbeddingRequest/Response` 协议类型（`add_special_tokens`、`truncate_prompt_tokens`） | embeddings 请求形状 |
| `lib/llm/src/preprocessor.rs` | `decode_base64_to_floats`（embeddings base64 解码）与 jail 的终端错误旁路（4.6/4.7 引用；机制详见 u4-l3） | 字节编解码与错误旁路 |
| `lib/llm/tests/responses_http_replay.rs` | Responses 面的完整 HTTP 回放测试，含 `input_tokens` 行为断言与带类型后端错误的端到端规格 | 行为规格书 |
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

[lib/llm/src/http/service/service_v2.rs:620-636](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L620-L636) — `HttpService` 本体：持有的不是「服务器」而是一个已经组装好的 `axum::Router` 加上端口/host/TLS 配置；`rl_router` 是 RL worker 发现专用的第二端口监听器（u8-l7）。

[lib/llm/src/http/service/service_v2.rs:640-741](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L640-L741) — `HttpServiceConfig`：默认端口 8787、host 0.0.0.0；`enable_chat_endpoints`/`enable_cmpl_endpoints` 默认 false（由上层按需打开），`enable_embeddings_endpoints`/`enable_responses_endpoints` 默认 true；`enable_engine_apis`（Generate 端点）、`enable_batch_endpoints`、`enable_rl` 默认 false。注意注释明确说明：Batch API 是占位实现（返回 501），Generate API 是实验性且默认关闭。

共享状态 `State` 是所有 handler 的「世界视图」：

[lib/llm/src/http/service/service_v2.rs:135-146](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L135-L146) — `State` 聚合了指标、模型目录、发现客户端、`ServiceObserver`、13 个端点开关、取消令牌和前端 API 行为配置。

[lib/llm/src/http/service/service_v2.rs:377-391](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L377-L391) — `StateFlags`：每个 `EndpointType`（Chat/Completion/Embedding/…/Batch 共 13 种）对应一个 `AtomicBool`，可以在运行时通过 `enable_model_endpoint` 翻转（Batch 除外）。

推理路由的注册入口：

[lib/llm/src/http/service/service_v2.rs:1427-1478](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L1427-L1478) — `get_endpoints_router` 逐个调用 `super::openai::chat_completions_router(...)` 等函数，把返回的 `(文档, 路由)` 对收进一个以 `EndpointType` 为键的 HashMap。路径全部可用 `DYN_HTTP_SVC_*_PATH` 环境变量覆盖（如 `DYN_HTTP_SVC_CHAT_PATH` 改掉 `/v1/chat/completions`，`DYN_HTTP_SVC_RESPONSES_PATH` 改掉 `/v1/responses`）。

[lib/llm/src/http/service/service_v2.rs:1517-1541](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L1517-L1541) — 关键中间件：每个端点路由外面包了一层闭包，请求进来先查 `state.flags.get(&endpoint_type)`，开关关着就直接返回 404。这就是「端点动态启停」的实现——不需要重建 Router。

三层叠加的顺序（在 build 里）：

[lib/llm/src/http/service/service_v2.rs:1291-1299](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L1291-L1299) — 推理路由先叠 `TraceLayer`（info 级 span），再叠 `track_inflight_inference`；[lib/llm/src/http/service/service_v2.rs:1307-1313](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L1307-L1313) — 系统路由只叠 `TraceLayer`（debug 级 span）。axum 的 layer 是「后加的先执行」，所以 inflight 记账发生在 trace span 内部，日志里能对上号。

[lib/llm/src/http/service/service_v2.rs:1320-1341](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L1320-L1341) — 兜底路由被刻意注册在 `track_inflight_inference` **之外**（源码注释原话：未匹配的请求不应获取推理许可、也不应在 draining 时返回 503，而是返回正常 404）；随后 [service_v2.rs:1338](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L1338) 全局加一层 `echo_request_id_header`，把请求头里的 `x-request-id` 原样带回响应。

RL 例外分支：

[lib/llm/src/http/service/service_v2.rs:1340-1352](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L1340-L1352) — `enable_rl_router` 由 builder flag `enable_rl` 或环境变量 `DYN_ENABLE_RL` 打开；打开但没配 `runtime` 会直接报错。RL 面不混入主路由表——它有自己的端口与生命周期（详见 u8-l7）。

Python 侧怎么用它？衔接 u4-l1：

[components/src/dynamo/frontend/main.py:418-431](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/frontend/main.py#L418-L431) — frontend 把 `http_host`/`http_port`/`enable_anthropic_api`/`reasoning_field_name` 等参数塞进 kwargs，最终以 `EntrypointArgs(EngineType.Dynamic, **kwargs)` 走 `make_engine`。也就是说你在命令行敲的 `--http-port 8000`，会变成 Rust 侧 `HttpServiceConfig` 的 `port` 字段——HTTP 服务本身完全由 Rust 实现，Python 只负责传配置。

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

**预期结果**：`/openapi.json` 里能看到所有已注册路由（`route_docs` 的产物），且派生子路由遵循「父路径去尾部斜杠再拼接」的规则。若你的安装版本与 HEAD 不一致，具体环境变量名以 [service_v2.rs:1026-1064](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L1026-L1064) 为准。待本地验证。

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

[lib/llm/src/http/service/service_v2.rs:219-225](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L219-L225) — `ServiceStage` 三态枚举，用 `u8` 存储（0/1/2），配合 `AtomicU8` 实现无锁读取。这个阶段与 runtime 的 `CancellationToken` 是**解耦的**——前端需要先停止收新请求、再拆除发现与传输状态。

[lib/llm/src/http/service/service_v2.rs:259-263](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L259-L263) — `ServiceObserver` 只有两个字段加一个通知器：`stage: AtomicU8`、`inflight_inference: AtomicU64`、`inflight_zero: Notify`。注意 `Notify` 的用法——它不是计数信号量，只是「唤醒所有等在这一点上的任务」的广播器。状态迁移入口是 [service_v2.rs:290-313](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L290-L313) 的 `start_draining`/`start_stopping`，两者都先打 info 日志再 `store`。

[lib/llm/src/http/service/service_v2.rs:319-323](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L319-L323) — `acquire_inflight()` 全文就是 `fetch_add(1)` 加构造 guard，**没有任何 if 判断上限**——这就是「它不限流」的直接证据。[service_v2.rs:335-354](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L335-L354) 的 `wait_inflight_zero_or_timeout` 里的循环值得细读：先 `notified()` 再 `enable()` 再查计数，这个顺序保证「注册等待发生在读计数之前」，最后一个 permit 释放时的 notify 不会丢。

[lib/llm/src/http/service/service_v2.rs:358-374](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L358-L374) — `InflightPermit` 的 `Drop`：`fetch_sub(1, AcqRel)` 返回的是**旧值**，等于 1 说明这次减完是 0；只有「减到 0」且「已不在 Ready 阶段」才 `notify_waiters()`。Ready 阶段不通知是性能优化——正常运行时每次请求结束都广播一遍毫无意义。

[lib/llm/src/http/service/service_v2.rs:103-132](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L103-L132) — `track_inflight_inference` 中间件全文。注意 `body.into_data_stream().map(move |result| { let _permit = &permit; result })` 这个技巧：把 permit 以引用方式捕获进每一帧的闭包，只要还有帧没被消费，permit 就活着。客户端中途断开导致 body 被 drop 时，permit 同样随之释放——不会泄漏。

[lib/llm/src/http/service/service_v2.rs:928-949](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L928-L949) — 非 TLS 路径的优雅关停：`with_graceful_shutdown` 的 future 一旦被触发，先 `start_draining()`，再 `wait_inflight_zero_or_timeout(shutdown_timeout)`（超时时间来自 [service_v2.rs:1017-1023](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L1017-L1023) 的 `DYN_HTTP_GRACEFUL_SHUTDOWN_TIMEOUT_SECS`，默认 5 秒），最后 `start_stopping()` 并 cancel runtime 令牌。

与 `ServiceObserver` 并行的另一套「HTTP 层指标」在 metrics.rs：

[lib/llm/src/http/service/metrics.rs:1375-1410](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/metrics.rs#L1375-L1410) — `create_inflight_guard` / `create_response_collector` / `create_http_queue_guard` 三件套。注意注释里的明确区分：`InflightGuard` 跟踪「引擎正在处理」，`HttpQueueGuard` 跟踪「handler 开始到首 token」（含 prefill 时间）。它们暴露为 `{prefix}_inflight_requests` 等 Prometheus 指标（见 [metrics.rs:615-623](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/metrics.rs#L615-L623)）。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：用并发压力验证三件事——① `InflightPermit` 不会因并发数拒绝请求；② inflight 计数能被 `/metrics` 观测到；③ draining 阶段确实开始返回 503。

**操作步骤**：

1. 启动一个「故意慢」的 sample 集群，让请求持续几秒，方便观察（`--delay` 是每个 token 的间隔，`--max-tokens` 是输出长度）：
   ```bash
   cd examples/backends/sample/launch
   DYN_HTTP_PORT=8000 ./agg.sh --model-name sample-model --delay 0.05 --max-tokens 200
   ```
   （`--delay`/`--max-tokens` 定义见 [components/src/dynamo/common/backend/sample_engine.py:138-139](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/common/backend/sample_engine.py#L138-L139)，多余参数会透传给 sample_main。）
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
| 输入形态 | 结构化 OpenAI 请求（messages/text/responses items） | Anthropic Messages（system 分离、`anthropic-version` 头） | **token_ids 进、token_ids 出**（token-in-token-out） |
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
 └─ DefaultBodyLimit                           # DYN_HTTP_BODY_LIMIT_MB 请求体上限
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

「pre-commit 错误窥视」值得单独一提：流式请求一旦开始返回就必然是 HTTP 200，后端错误只能塞在 SSE error 帧里。Dynamo 提供一个可选窗口（`DYN_HTTP_PRE_COMMIT_ERROR_PEEK_MS`），在提交 200 之前短暂窥视第一帧，如果是同步后端错误（例如纯文本模型收到了图片，即 `Backend(InvalidArgument)`），就还能返回规范的 4xx——这正是 4.7 节要讲的带类型错误的消费场景之一。

#### 4.3.3 源码精读

OpenAI 侧的 handler 入口：

[lib/llm/src/http/service/openai.rs:1944-2027](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L1944-L2027) — `handler_chat_completions`：读体、解析、双重就绪检查（进程级 + 模型级，注释解释了为什么聚合请求打到 decode-only 命名空间会挂）、构造 Context、建立断连监控，然后把真正的处理 `tokio::spawn` 出去——handler 本身只负责协议层，业务在子任务中执行，避免长连接拖住 axum worker。

[lib/llm/src/http/service/openai.rs:3790-3795](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L3790-L3795) — `check_ready`：一行 `is_ready()` 检查，不 ready 返回 503。所有推理 handler 共用。

[lib/llm/src/http/service/openai.rs:3813-3849](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L3813-L3849) — `check_model_serving_ready`：**模型级**就绪门，与 `check_ready` 是 AND 关系——进程 ready 且该模型在至少一个命名空间里凑齐了完整 worker 集合（每个 worker 的 `needs` DNF 都被该命名空间当前出现的 worker 类型满足）才放行。doc 注释特意说明用户可见的错误信息刻意不含内部术语（worker type、namespace），运维细节走 `GET /v1/models/{model}/ready`。

[lib/llm/src/http/service/openai.rs:2747-2819](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L2747-L2819) — `chat_completions` 业务体前半段：应用请求模板 → 模型别名规范化（alias → primary，注释提到这是为了与 vLLM/SGLang 行为一致）→ 尽早创建 inflight guard（让校验错误也计入指标）→ 五层校验依次执行，任何一层失败都 `mark_error` 后返回。

[lib/llm/src/http/service/openai.rs:2947-3040](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L2947-L3040) — 流式分支的核心循环：`async_stream::stream!` 内逐帧处理——`enforce_single_tool_call` 时只保留 index 0 的工具调用、`deduplicate_stream_roles` 去重 role、空块（多字节 token 组装产物）被丢弃但仍要记指标（注释解释了不记账会低估输出 token 与 TTFT）、可选的 `tool_call_dispatch`/`reasoning_dispatch` 侧信道事件先行，最后 `EventConverter` 转成 SSE event。结尾 `Sse::new(...).keep_alive(...)` 挂上心跳。

[lib/llm/src/http/service/openai.rs:3041-3084](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L3041-L3084) — 非流式分支：同样先窥视首帧错误，然后 `NvCreateChatCompletionResponse::from_annotated_stream` 把整条流聚合成单条 JSON；聚合完成后如果发现 context 已被 kill（客户端断开），指标改记 Cancelled。

[lib/llm/src/http/service/openai.rs:3964-3979](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L3964-L3979) — `chat_completions_router`：标准三件套——`smart_json_error_middleware`（错误 JSON 化）、`DefaultBodyLimit`（请求体上限）、`with_state`。所有 openai.rs 里的 router 构造函数都是这个形状。

Anthropic 侧：

[lib/llm/src/http/service/anthropic.rs:73-92](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/anthropic.rs#L73-L92) — `DEFAULT_MESSAGES_PATH = "/v1/messages"` 与 `anthropic_messages_router`：除了标准三件套还多一层 `anthropic_error_middleware`，把错误转成 Anthropic 的 `{type:"error", error:{...}}` 信封。

[lib/llm/src/http/service/anthropic.rs:249-360](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/anthropic.rs#L249-L360) — `handler_anthropic_messages`：与 chat handler 同构（同样的就绪检查、Context 构造、断连监控），差别在请求校验（`validate_anthropic_messages`/`validate_anthropic_tools`）和响应格式转换。service_v2.rs 里的 `unmatched_route_fallback` 会根据路径是否落在 Anthropic 命名空间内，决定 404 用哪种错误信封（见 [service_v2.rs:83-101](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L83-L101)，`path_within_namespace` 做的是完整路径段匹配，`/v1/messages_beta` 不会误判）。

Generate 侧：

[lib/llm/src/http/service/generate.rs:3-11](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L3-L11) — 模块文档开宗明义：这是**实验性的引擎原生端点，默认关闭**，`enable_engine_apis` 或 `DYN_VLLM_ENABLE_INFERENCE_V1_GENERATE` 打开；「把完整请求保存在不透明后端信封里」，流式（stream=true）尚未实现。

[lib/llm/src/http/service/generate.rs:95-109](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L95-L109) — `generate_router`：默认路径 `/inference/v1/generate`，结构与 openai 的 router 一致（连 `smart_json_error_middleware` 都是复用 openai.rs 导出的）。

[lib/llm/src/http/service/generate.rs:788-966](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L788-L966) — `handler_generate`：校验 → 拒绝 stream=true（501）→ 若请求未指定 model 则按 `VLLM_INFERENCE_V1_GENERATE_CAPABILITY` 能力查找已注册模型（0 个报 404，多个报 400 要求显式指定）→ 模型级就绪检查 → 按**能力**取引擎（这与 openai 按模型名取引擎不同，是为了防止 vLLM 信封误投给 SGLang worker）。#11385 之后它的开头多了一段「模型名归一 + 指标生命周期建立」的前置步骤（793-825 行，详见 4.8 节），且**每个早退分支都会先 `mark_error` 再返回**——被拒请求也要进指标。

[lib/llm/src/http/service/generate.rs:968-1074](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L968-L1074) — `generate_dispatch`：`run_until_killed` 把 `engine.generate(context)` 包在 `tokio::select! { biased; operation, killed }` 里——客户端 kill 信号一到立刻放弃，返回 499（nginx 风格的 request_cancelled）。注释解释了为什么 dispatch 要 `tokio::spawn` 分离：unary 工作必须比 Axum handler 活得久，handler 被 drop 时才能触发已武装的断连监控。#11385 后它还接收 `GenerateMetricLifecycle` 并在流上逐帧 `observe`（1027-1030 行，见 4.8）。

[lib/llm/src/http/service/generate.rs:199-248](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L199-L248) — `VllmTitoEnvelope`：把 vLLM 特有字段（sampling_params、cache_salt、priority、kv_transfer_params…）序列化进信封；注释明确 `token_ids` 故意不在其中——`PreprocessedRequest.token_ids` 才是路由与线上格式的权威表示，worker 端从它重建 vLLM 请求。

三类端点共用的探针：

[lib/llm/src/http/service/health.rs:40-61](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/health.rs#L40-L61) 与 [lib/llm/src/http/service/health.rs:63-98](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/health.rs#L63-L98) — `/live` 只查 cancel_token（进程还活着吗），`/health` 查 is_ready 并列出发现到的全部实例端点。K8s 的 livenessProbe 应指 `/live`、readinessProbe 应指 `/health`。

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
2. 打开 Anthropic 面重启 frontend（`--enable-anthropic-api`，对应 [main.py:425](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/frontend/main.py#L425) 的 `enable_anthropic_api`），然后：
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

**答案**：默认情况下 SSE 一旦开始就是 HTTP 200，错误会以 `event: "error"` 的 SSE 帧出现（EventConverter 负责识别）。要让同步后端错误表现为规范 4xx，需要设置 `DYN_HTTP_PRE_COMMIT_ERROR_PEEK_MS` 打开 pre-commit 窥视窗口（[openai.rs:2537-2545](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L2537-L2545) 的 `pre_commit_error_peek_timeout` 与 [openai.rs:2912-2933](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L2912-L2933) 的使用点），在提交 200 之前短暂等待第一帧信号——注释里举的例子正是 `Backend(InvalidArgument)`（文本模型收到图片内容）。

**练习 3**：`handler_chat_completions` 为什么用 `tokio::spawn` 把业务包一层，而不是直接 await？

**答案**：为了把「handler 协议层」与「长耗时业务」解耦。connection monitor 需要在业务完成（或失败）后决定是否 disarm；若业务直接在 handler 里 await，客户端断开导致的任务取消会与 axum 的连接管理纠缠。spawn 出去后 handler 只 await JoinHandle，`connection_handle.disarm()` 的语义清晰（openai.rs:2012-2024）。generate.rs 的 dispatch 同理，且注释额外指出 unary 工作必须比 handler 活得久。

---

### 4.4 流式增量序列化：DeltaGeneratorState 共享状态

#### 4.4.1 概念说明

客户端收到的每个 SSE `data:` 帧里是一个 `chat.completion.chunk` JSON 对象。这些对象不是引擎直接产出的——引擎吐的是 `BackendOutput`（token_ids、logprobs、finish_reason……），把这份「裸输出」翻译成 OpenAI 增量格式的是 **DeltaGenerator**。

关键结构：chat 端点和 text completions 端点各有一个 `DeltaGenerator`（分别在 `protocols/openai/chat_completions/delta.rs` 和 `protocols/openai/completions/delta.rs`），但两者的「生成器状态」被抽取到了公共模块 `delta_common.rs` 的 **`DeltaGeneratorState`** 里。共享的是：

- 响应身份三件套：`id`（如 `chatcmpl-<request_id>`）、`object`（`chat.completion.chunk` 或 `text_completion`）、`created` 时间戳、`model` 名；
- token 用量累计：`usage: CompletionUsage`（prompt/completion/total tokens 及其 details）；
- 行为开关：`DeltaGeneratorOptions`（是否带 usage、是否连续 usage、是否 logprobs、token 显示为文本还是 `token_id:<id>`、nvext 响应字段选择）；
- 计时器：`RequestTracker`（TTFT/ITL 按时序记录，供 per-worker 指标）。

而两个端点**不共享**的是各自的增量语义：chat 端有 `emitted_role_choices`（哪些 choice 已发过 role）、`service_tier`；completions 端有自己的文本拼接逻辑。

这个切分让「新增一种 OpenAI 风格端点」时不必重写 usage 统计和 TTFT 计时，也让 #12562 这样的 usage 语义修正只改一处就能同时覆盖 chat 与 completions 两个端点。

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
 ├─ state.update_usage_from_backend_output(output)   # 见下方两条规则
 ├─ generator.create_logprobs(...)                   # 可选
 └─ 产出 NvCreateChatCompletionStreamResponse { id, object, created, model, choices, usage? }

流结束时
 └─ state.get_usage()  →  total = prompt.saturating_add(completion)
```

#12562 之后，`update_usage_from_backend_output` 对 completion tokens 有两条规则并存：

1. **默认按帧累加**：`completion_tokens += token_ids.len()`——逐帧累计本请求生成的 token；
2. **后端给出 `completion_usage` 时取 max**：`completion_tokens = max(累计值, backend 值)`。语义上后端的 completion_usage 是**请求级总量**而不是「本帧增量」——对 `n>1` 的多 choice 请求，后端会报告整个请求的总量；对报告 0 的后端，则保留本地累计值不被清零。

一个协议细节：OpenAI 规定**非流式响应必须带 usage**。`enable_usage_for_nonstreaming(&mut stream_options, original_stream_flag)` 在原始请求不是流式时强制把 `include_usage` 置 true，即使客户端没要。

#### 4.4.3 源码精读

[lib/llm/src/protocols/openai/delta_common.rs:17-48](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/openai/delta_common.rs#L17-L48) — `DeltaGeneratorOptions` 与它的构造函数：开关全部从请求推导——`enable_usage`/`continuous_usage_stats` 来自 `stream_options`，logprobs 来自请求参数，nvext 响应字段来自扩展头。

[lib/llm/src/protocols/openai/delta_common.rs:50-100](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/openai/delta_common.rs#L50-L100) — `DeltaGeneratorState` 本体与构造：记录 created 时间戳（注释幽默地指出 u32 秒级时间戳到 2106 年才溢出）、usage 清零、无条件创建 `RequestTracker`——注释特别强调：即使 `response_fields` 不让 nvext 字段回给客户端，tracker 仍然内部记录 TTFT/ITL 供指标使用。

[lib/llm/src/protocols/openai/delta_common.rs:138-168](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/openai/delta_common.rs#L138-L168) — usage 更新逻辑（#12562 修正后的样子）：`completion_tokens` 先按 token_ids 长度累加（注释再次解释 u32 精度足够）；若 backend 提供了 `completion_usage`，prompt_tokens 以它为准覆盖（对 embedding 场景至关重要），而 completion_tokens 改为 [delta_common.rs:155-158](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/openai/delta_common.rs#L155-L158) 的 `max` 钳制——后端总量更高时采用后端值，后端报 0 时保留本地累计值。[delta_common.rs:174-178](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/openai/delta_common.rs#L174-L178) 的 `get_usage` 用 `saturating_add` 防溢出。

[lib/llm/src/protocols/openai/delta_common.rs:189-217](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/openai/delta_common.rs#L189-L217) — `enable_usage_for_nonstreaming`（非流式强制带 usage，满足 OpenAI 规范；带 `original_stream_flag` 参数，流式直接返回）与 `force_include_usage`（无条件开启，配合 `FORCE_INCLUDE_USAGE` 开关在 handler 入口使用，见 [openai.rs:1951-1953](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L1951-L1953)）。文件尾部的单测（221 行起）验证了「缺失时插入、已存在时只翻转 include_usage 且保留兄弟字段」两个行为。

chat 侧如何消费共享状态：

[lib/llm/src/protocols/openai/chat_completions/delta.rs:40-62](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/openai/chat_completions/delta.rs#L40-L62) — chat 的 `DeltaGenerator`：字段就三个——共享的 `state`、可选 `service_tier`、`emitted_role_choices: HashSet<u32>`（记录哪些 choice 的 role 帧已发过，配合 handler 侧的 `deduplicate_stream_roles` 防止 role 重复）。所有身份/usage 访问都是对 `state` 的一行委托（`update_isl`、`tracker` 都是转发）。

[lib/llm/src/protocols/openai/chat_completions/delta.rs:19-38](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/openai/chat_completions/delta.rs#L19-L38) — `NvCreateChatCompletionRequest::response_generator`：handler 用它从请求一键构造生成器；`object` 字段在这里固定为 `"chat.completion.chunk"`（completions 端点会传不同的 object 字符串，这就是两个端点共享状态却产出不同格式的方式）。

#12562 的新语义由三个测试钉死（completions/delta.rs 有一组对称的测试）：

[lib/llm/src/protocols/openai/chat_completions/delta.rs:549-626](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/openai/chat_completions/delta.rs#L549-L626) — 三个测试分别覆盖：后端 usage 更高时采用后端值（`test_completion_tokens_use_backend_usage_when_higher`）、`n=2` 多 choice 时后端值按请求总量对待（`test_completion_tokens_treat_backend_usage_as_request_total`）、后端报 0 时保留本地累计值（`test_completion_tokens_keep_aggregated_count_when_backend_usage_is_zero`）。

#### 4.4.4 代码实践

**实践目标**：亲眼看到 usage 的两种行为——流式按需、非流式强制；并验证「后端 usage 覆盖」规则。

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
2. （源码阅读型）打开 `lib/llm/src/protocols/openai/completions/delta.rs` 的测试区（约 363-440 行），对比它与 chat 侧三个 usage 测试的异同；再打开 [delta_common.rs:138-168](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/openai/delta_common.rs#L138-L168)，在笔记里回答：为什么 `max` 而不是赋值？提示——想想 `n>1` 时「按帧累加」会得到什么、后端报告的又是什么。
3. （可选，需 Rust 环境）跑这几个测试：
   ```bash
   cargo test -p dynamo-llm --lib protocols::openai::chat_completions::delta::tests::test_completion_tokens
   ```

**需要观察的现象**：三种请求的 usage 出现情况不同；每个 chunk 的 `id` 保持一致（形如 `chatcmpl-...`）、`object` 恒为 `chat.completion.chunk`、`model` 回显规范化后的模型名。sample 后端的 usage 遵循本地累加路径（它不产出 `completion_usage`），completion_tokens 与 `--max-tokens` 生成的帧数一致。

**预期结果**：非流式必有 usage（`enable_usage_for_nonstreaming` 生效）；流式仅在显式 `include_usage` 时于末帧给出 usage。chunk 之间 id 一致、created 一致——这正是共享 `DeltaGeneratorState` 的直接体现。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么把状态抽到 `delta_common.rs` 而不是让两个 `DeltaGenerator` 各自维护一份字段？

**答案**：chat 与 completions 两个端点对「响应身份、usage 累计、选项、TTFT 计时」的需求完全一致，差异只在增量语义（choice 结构、role 去重、文本拼接）。抽出共享状态后，#12562 这样的 usage 语义修正（后端总量取 max）只改 [delta_common.rs:155-158](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/openai/delta_common.rs#L155-L158) 一处，就同时修好了两个端点；新增端点也能直接复用。

**练习 2**：`n=2`（两个 choice）的请求，后端每帧报告 `completion_tokens: 5`（请求总量），本地累加路径会数到多少？最终 usage 报多少？

**答案**：本地累加会数到「两个 choice 的 token 总和」（例如每 choice 5 个就是 10）；后端报告的是请求总量 5。取 `max` 后最终报 10——这正是 `test_completion_tokens_treat_backend_usage_as_request_total` 断言的反向场景：当后端把 per-choice 计入总量（如 5+5=10 的请求报 10）时采用后端值。关键在于两条规则谁大听谁，保证不低估也不重复计数。

**练习 3**：客户端没传 `stream_options`，为什么非流式响应里还是有 usage？

**答案**：请求侧调用 `enable_usage_for_nonstreaming(&mut stream_options, original_stream_flag)`（[chat_completions/delta.rs:20-25](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/openai/chat_completions/delta.rs#L20-L25) 转发到 [delta_common.rs:194-208](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/openai/delta_common.rs#L194-L208)），它在非流式时把 `stream_options.include_usage` 强制置 true（缺失则先插入默认结构），随后 `DeltaGeneratorOptions::new` 读到 true，`get_usage` 的结果就会进入最终 JSON。注释明确说这是为了符合 OpenAI API 规范「非流式响应必须包含 usage」。

**练习 4**：`RequestTracker` 在客户端没开启任何 nvext 字段时也被创建，为什么？

**答案**：见 delta_common.rs 构造函数的注释——tracker 的数据同时服务于内部 per-worker 指标（`last_ttft`、`last_itl`，由 `ResponseMetricCollector` 在 handler 侧消费），`response_fields` 只控制哪些字段回给客户端。观测永远开启，回显按需开关。

---

### 4.5 输入 token 预估端点：POST /v1/responses/input_tokens

#### 4.5.1 概念说明

Responses 面的子端点 `POST /v1/responses/input_tokens` 的用途很朴素——**在不发起任何推理的情况下，估算一条请求会消耗多少输入 token**。

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

[lib/llm/src/http/service/openai.rs:3228-3236](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L3228-L3236) — `handler_responses_input_tokens` 全文。注意它的 `State` 参数直接绑定成 `(_state, _template)`——连状态都不用看。函数体只有三行：读体 → 解析 → 返回估算。与之对照，紧随其后的 `handler_responses`（[openai.rs:3241-3259](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L3241-L3259)）做了完整的双重就绪检查——两个 handler 的开头并排读，差异一目了然。

[lib/llm/src/http/service/openai.rs:4224-4247](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L4224-L4247) — `responses_router`：父路径 `/v1/responses` 与派生子路径 `/v1/responses/input_tokens` 注册在**同一个** Router 上，共享 `smart_json_error_middleware` 与 `DefaultBodyLimit`。`input_tokens_path = format!("{}/input_tokens", path.trim_end_matches('/'))` 旁边的大段注释解释了尾部斜杠问题；`RouteDoc` 两条都返回，所以 `/openapi.json` 能看到子端点。

估算逻辑本身在 `dynamo-protocols` crate 的 `CountInputTokensRequest::estimate_tokens`（外部依赖，不在本仓库），行为规格由回放测试钉死：

[lib/llm/tests/responses_http_replay.rs:603-626](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/tests/responses_http_replay.rs#L603-L626) — `input_tokens_counts_input`：断言返回 `object == "response.input_tokens"`、计数 > 0，并且**引擎收到的请求数为 0**（"Counting is pre-flight only; it must never reach a backend"）。

[lib/llm/tests/responses_http_replay.rs:628-654](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/tests/responses_http_replay.rs#L628-L654) — `input_tokens_does_not_gate_on_model`：用一个 frontend 不服务的模型名、以及完全不带 `model` 字段，都必须返回 200。这是「不做模型级就绪检查」的行为规格。

[lib/llm/tests/responses_http_replay.rs:656-690](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/tests/responses_http_replay.rs#L656-L690) — `input_tokens_counts_instructions_and_tools`：同一 `input` 加上 `instructions` 与 `tools` 后计数必须变大——证明估算范围覆盖了完整请求而非只有 `input` 字段。

[lib/llm/tests/responses_http_replay.rs:692-726](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/tests/responses_http_replay.rs#L692-L726) — `input_tokens_rejects_malformed_json`：坏 JSON 返回 400，错误信封与 `/v1/responses` 完全一致（`code:400`、`type:"Bad Request"`）。

[lib/llm/tests/responses_http_replay.rs:728-832](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/tests/responses_http_replay.rs#L728-L832) — 工具调用会话 item 的两个测试：`input_tokens_accepts_tool_call_conversation_items`（`function_call`/`function_call_output` 必须被计入，含 `input_tokens_tolerates_tool_shapes_it_cannot_model` 的宽容分支）与 `parallel_tool_calls_are_one_assistant_message_for_both_endpoints`——把同一批 item 同时发给 `input_tokens` 与 `/v1/responses`，用引擎实际收到的 chat 消息作为「应记几次 assistant 角色标记」的真值，钉死估算器与转换器的 coalescing 等价性。

顺带一提 metrics.rs 侧的配套：[lib/llm/src/http/service/metrics.rs](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/metrics.rs) 的测试里有一条回归——「无 choice 的 nvext 元数据帧（如 `engine_data.prompt_token_ids`）必须原样到达客户端 SSE」，测试使用类型化的 `process_chat_response_using_event_converter_and_observe_metrics`（[metrics.rs:2174](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/metrics.rs#L2174)）。这保证了 RL 需要的 token 级元数据不会被指标层吞掉——与 `input_tokens` 一起构成对 rollout 场景的支撑。

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

**答案**：能。`responses_router` 派生子路径时用 `path.trim_end_matches('/')` 先去掉尾部斜杠再拼 `/input_tokens`（[openai.rs:4230-4237](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L4230-L4237)），注册出来的是 `/custom/input_tokens`。如果不做这个 trim，会注册出 `/custom//input_tokens`，而 axum 不把它当作等价路径，客户端实际调用的地址就会 404。父路径本身仍按原样（带斜杠）注册，所以已有配置行为不变。回放测试 `input_tokens_path_normalizes_a_trailing_slash_parent`（[responses_http_replay.rs:885](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/tests/responses_http_replay.rs#L885) 起）钉死了这个行为。

**练习 3**：为什么回放测试要专门断言「估算后引擎收到的请求数为 0」，还要断言「并行工具调用只记一次 assistant 消息」？

**答案**：前者钉住「pre-flight only」的契约——一旦有人图省事把估算改成走一遍真实 preprocessor + 引擎，这条测试会立刻红，因为它会把估算成本变成一次真实（可能很贵的）分词/推理。后者钉住估算器与 chat 转换器的**等价性**：转换器 `convert_input_items_to_messages` 会把连续的 assistant 侧 item 合并成一条消息，估算器只对「已 flush 的 pending assistant 消息」记一次角色标记。两边规则一旦漂移，估算就会系统性偏高或偏低，而这个偏差在 RL 预算场景里会直接变成超额 token 消耗。

---

### 4.6 Embeddings 端点与 base64 字节传输（#12157 后的形态）

#### 4.6.1 概念说明

`/v1/embeddings` 是另一类推理端点：输入文本（或 token_ids），输出定长浮点向量。它的特殊之处在于**内部链路上的向量线格式是 base64 字节串，不是 JSON 浮点数组**。

为什么？一个 embedding 模型动辄输出 1024～4096 维 float32。JSON 表示形如 `[0.0133..., -0.0521..., ...]`，每个数 10 多个字符，序列化/解析都贵；而 base64(LE f32) 每 4 字节一个数、编码后约 5.3 个字符，且 Python 侧可以直接 `torch → numpy.tobytes() → base64`（显式 little-endian float32），Rust 侧 `f32::from_le_bytes` 原样还原——**字节级直传，零浮点格式化**。所以在 worker → frontend 的内部跳上永远传 base64，只在 HTTP 出口按客户端的 `encoding_format` 决定要不要转回浮点数组。

#12157 做了一次收拢：此前 openai.rs 里有一份本地解码函数 `decode_base64_embedding_to_floats`（连同三个单测），与 preprocessor.rs 里 tokens 路径的解码逻辑重复。现在 openai.rs 删除了自己的副本，改为 `use crate::preprocessor::decode_base64_to_floats`——**编解码统一住在 preprocessor.rs**（编码侧的 Python 对应物是 `components/src/dynamo/vllm/handlers.py` 的 `_encode_floats_to_base64`，见 u8-l8）。注意 `protocols/openai/embeddings.rs` 这个协议模块里放的是**请求/响应类型**（含新增的 `add_special_tokens` 与 `truncate_prompt_tokens` 字段），不是编解码函数——找编解码要去 preprocessor.rs。

#### 4.6.2 核心流程

```text
POST /v1/embeddings（encoding_format 缺省 = float）
 └─ 就绪检查 / Context 构造（与 chat 同构）
 └─ client_wants_float = !(encoding_format == Base64)     # 只有显式要 Base64 才透传
 └─ engine.generate(request) → 流
 └─ NvCreateEmbeddingResponse::from_annotated_stream(stream)   # 聚合（embeddings 天然非流式）
 └─ if client_wants_float:
      for each embedding_obj:
        Base64(s) → decode_base64_to_floats(s) → Float(Vec<f32>)   # HTTP 出口转回浮点
 └─ observe_embedding_latency + inflight.mark_ok
```

解码失败的容错：任何一条向量解不开（非法 base64 或字节数不是 4 的倍数）都会记 error 日志、`mark_error`，并返回 500「Failed to decode embedding payload」——不会把半解码的响应发出去。

#### 4.6.3 源码精读

[lib/llm/src/http/service/openai.rs:1402-1415](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L1402-L1415) — 内部线格式的动机注释 + `client_wants_float` 的判定：worker 永远以 base64 发向量以避免内部跳上的 JSON 浮点数组序列化/解析开销（注释提到 DIS-2154 有实测数据）；客户端要 float（默认）时才在 HTTP 边界解码，使公开响应形状与 `encoding_format` 选择一致。

[lib/llm/src/http/service/openai.rs:1493-1518](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L1493-L1518) — 出口解码循环：遍历 `response.inner.data`，遇到 `EmbeddingVector::Base64(s)` 就调 `decode_base64_to_floats` 替换成 `Float`；失败路径返回 500 并计入错误指标。

[lib/llm/src/preprocessor.rs:300-319](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/preprocessor.rs#L300-L319) — `decode_base64_to_floats`：标准 base64 解码 → 校验字节数是 4 的倍数（尾部残字节拒绝）→ `chunks_exact(4)` 逐块 `f32::from_le_bytes`。doc 注释明确说明它被「tokens 路径的 postprocessor 与 HTTP embedding handler 共享」——这就是 #12157 收拢后的单一实现点。

[lib/llm/src/protocols/openai/embeddings.rs:14-37](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/openai/embeddings.rs#L14-L37) — `NvCreateEmbeddingRequest`：`inner` flatten 内嵌 OpenAI 的 `CreateEmbeddingRequest`，外加 Dynamo 扩展字段——`add_special_tokens`（#12157 新增，vLLM 兼容：raw text 输入是否加模型声明的特殊 token，缺省时加、与 vLLM pooling 默认一致，调用方给 token_ids 时忽略）与 `truncate_prompt_tokens`（vLLM 风格截断长度，-1 为「截到模型最大长度」哨兵；当前实现走右截断、保留前 N 个 token）。

[lib/llm/src/http/service/openai.rs:1431-1464](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L1431-L1464) — embeddings handler 的指标 guard 与引擎调用：同样「尽早创建 inflight guard 让校验错误也计数」、`get_embeddings_engine(model)` 按模型取引擎、`engine.generate` 的拒绝错误计入 rejection 指标。多进程嵌入 worker 池如何喂这个引擎见 u8-l8。

#### 4.6.4 代码实践

**实践目标**：观察 `encoding_format` 两种取值下响应形状的差异，并手工验证 base64 向量可解码为 little-endian float32。

**操作步骤**：

1. 启动 sample 集群后分别请求（sample 后端未必实现 embeddings 端点；若 404，改用任一真实 embedding 模型，或跳到第 2 步做纯源码/数据练习）：
   ```bash
   # 默认（float）
   curl -s localhost:8000/v1/embeddings -H 'content-type: application/json' \
     -d '{"model":"sample-model","input":"hello"}' | python3 -c \
       'import json,sys; d=json.load(sys.stdin); print(type(d["data"][0]["embedding"]), d["data"][0]["embedding"][:4])'

   # 显式 base64
   curl -s localhost:8000/v1/embeddings -H 'content-type: application/json' \
     -d '{"model":"sample-model","input":"hello","encoding_format":"base64"}' | python3 -c \
       'import json,sys,base64,struct; s=json.load(sys.stdin)["data"][0]["embedding"];
        b=base64.b64decode(s); print("bytes:",len(b),"dim:",len(b)//4,"first3:",struct.unpack("<3f",b[:12]))'
   ```
2. （离线验证，无需服务）用 Python 生成一段向量，按「little-endian f32 → base64」编码，再对照 [preprocessor.rs:300-319](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/preprocessor.rs#L300-L319) 的规则手算：7 个 float 编码后是 28 字节，能否通过「4 的倍数」校验？如果故意造 5 字节的载荷，Rust 侧会返回什么错误信息？
   ```python
   import base64, struct
   vec = [0.0, 1.0, -1.0, 2.5]
   print(base64.b64encode(struct.pack("<4f", *vec)).decode())   # 合法载荷
   print(base64.b64encode(bytes(5)).decode())                    # 5 字节 → 应被拒绝
   ```

**需要观察的现象**：float 模式拿到 JSON 数组；base64 模式拿到字符串，且 `字节数 ÷ 4 == 向量维度`、`struct.unpack("<3f", ...)` 能还原出合理浮点数。5 字节载荷在离线推演里对应 Rust 侧 `Err("base64-decoded embedding byte length 5 is not a multiple of 4")`。

**预期结果**：确认「内部线格式 = base64(LE f32)、出口按需转浮点」的双层设计；并记住编解码的实现位置在 preprocessor.rs（协议类型在 protocols/openai/embeddings.rs）。待本地验证。

#### 4.6.5 小练习与答案

**练习 1**：为什么不在 worker 侧直接按客户端的 `encoding_format` 生成最终格式？

**答案**：因为 worker 不该知道客户端要什么——线格式是**内部契约**，`encoding_format` 是**外部协议参数**。统一内部传 base64 让内部跳的序列化成本与客户端选择解耦：客户端要 float 时由 HTTP 边界解码一次；客户端要 base64 时直接透传、零转换。同时也让多 worker/多副本路径（见 u8-l8 的嵌入 worker 池）的线格式保持一致。

**练习 2**：`decode_base64_to_floats` 为什么要求字节数是 4 的倍数并拒绝尾部残字节？

**答案**：载荷是若干个 4 字节的 LE f32 拼接。长度非 4 的倍数意味着编码侧出错（维度数 × 4 ≠ 字节数），此时「截断处理」会静默产生一个错误维度的向量——比报错危险得多。`chunks_exact` 天然跳过不完整块，显式的长度检查则把这种情况变成可观测的 500，符合「fail loud」原则。

**练习 3**：`add_special_tokens` 缺省时的行为是什么？为什么默认值要跟 vLLM 的 pooling 默认一致？

**答案**：缺省时 raw text 输入**包含**模型声明的特殊 token（等价于 `Some(true)`），与 vLLM pooling 默认对齐；调用方直接给 `token_ids` 时该字段被忽略（此时分词已经完成）。对齐的原因是兼容性：已有按 vLLM 语义写的客户端迁移到 Dynamo 时不应因为默认值不同而得到系统性偏移的 embedding——这类偏差很难从监控上发现。

---

### 4.7 后端错误提取与 #13930 的类型保留

#### 4.7.1 概念说明

推理请求的失败有两类完全不同的暴露方式：非流式（或 pre-commit 窗口内）可以返回规范的 HTTP 4xx/5xx；流式一旦提交 200 就只能靠 SSE error 帧。无论哪种，HTTP 层都需要从引擎流里的 `Annotated` 事件中**提取错误并决定状态码**——这就是 `extract_backend_error_if_present` 的职责。

错误的「状态码映射」本质上是**分类问题**：

- `InvalidArgument` 类（客户端的请求不合法，比如文本模型收到图片）→ **400**，客户端应当改请求后重试；
- 容量拒绝（过载/准入拒绝）→ 可配置的过载状态码（默认 503，`DYN_HTTP_OVERLOAD_STATUS_CODE`）；
- 其他未知错误 → **500**，且要「消毒」（sanitized）——不向客户端泄漏内部细节。

本次 #13930 之前的旧世界里有一个补丁：某些适配器路径会把带类型的错误**序列化进一个泛型错误的 message 字符串**里（前缀 `"BackendInvalidArgument: "`），HTTP 层于是靠**字符串前缀匹配**把它恢复成 400。这个分支被删掉了（-26 行）。它能被删掉，是因为根因被修了——错误在流经预处理器 jail（工具调用解析的隔离层，u4-l3 详讲）时**类型信息不再被擦除**：

- 入口侧 `take_while` 把第一个错误事件**扣下来**，根本不让它进 jail（jail 的 finalize 看到的只会是真 EOF，不会拿错误流合成假完成帧）；
- 出口侧把扣下的**原始带类型 Dynamo 注解**接回主流；jail 自己产出的 `JailAnnotated.error` 恒为 `None`（两处 `debug_assert` 分别保证「终端错误必须绕过 jail」与「dynamo-parsers 不得构造错误」）。

于是 HTTP 层收到的错误事件带着完整的 `ErrorType`，直接走类型化分类即可——字符串前缀恢复成了死代码。代价与收益：好处是**类型即契约**（新增错误种类不需要同步维护字符串约定）；行为变化是——如果确实有旧路径仍把错误塞进 message 字符串（未携带类型），现在会按「未知错误」消毒成 500，而不再是 400。

#### 4.7.2 核心流程

#13930 之后 `extract_backend_error_if_present` 的判定阶梯（对 `event: "error"` 事件）：

```text
event.event == "error" ?
 ├─ 否 → 检查 data 载荷是否自带 {code ≥ 400} / comment 是否像错误（旧兼容路径）
 └─ 是：
      1. invalid_argument = error.error_type() ∈ {InvalidArgument, Backend(InvalidArgument)}
         （只看本节点类型，不看 cause 链——内层 invalid 不得覆盖外层 unavailable/internal）
      2. error_str = 沿 source() 链收集各层 message（", " 连接）——诊断串，不保证是 JSON
      3. overloaded = request_was_rejected(error)      # 容量拒绝
      4. status_message 能解析成 {message, code} JSON ?
         ├─ 是 → code = overloaded ? overload_status_code()
         │              : payload.code 显式给出 ? 用它
         │              : invalid_argument ? 400
         │              : 500
         └─ 否 → invalid_argument ? 400
                  : overloaded ? overload_status_code()（带 Overloaded 消毒标记）
                  : 500（消毒）
```

与之配套的「jail 旁路」（在 preprocessor，详见 u4-l3）：

```text
进入 jail 的流
 └─ take_while: 首个 error Annotated 存入 terminal_error，流在此截断（jail 看不到它）
 └─ jail 正常处理非错误帧（JailAnnotated.error 恒为 None，debug_assert 保证）
 └─ 出口: take_while 丢弃 jail 在截断点之后仍吐出的任何帧
        chain(once(terminal_error))   # 原始带类型错误注解原样接回主流
```

#### 4.7.3 源码精读

[lib/llm/src/http/service/openai.rs:2222-2241](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L2222-L2241) — `BackendErrorInfo`：提取结果的载体。注意第三个字段 `sanitized: Option<SanitizedError>`——当状态码本身不足以恢复分类时（如过载被配置成 503/500）保留已确定的消毒标记。

[lib/llm/src/http/service/openai.rs:2245-2267](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L2245-L2267) — `extract_backend_error_if_present` 的开头与**类型化分类**：`error.error_type()` 匹配 `ErrorType::InvalidArgument | ErrorType::Backend(BackendError::InvalidArgument)`。注释强调只分类本节点的错误、不查 cause 链——这正是 #13930 之后唯一的 invalid-argument 判定途径（旧的字符串前缀分支已删除）。

[lib/llm/src/http/service/openai.rs:2269-2299](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L2269-L2299) — 诊断串的组装：优先用 `DynamoError::message()`（避免 `to_string()` 带上 `ErrorType` 前缀破坏 JSON 解析），沿 `source()` 链逐层拼接；以及容量拒绝的判定（注释解释了为何错误链优先于 payload code——worker 自报的 503 与真实宕站在客户端视角无法区分，见 `DYN_HTTP_OVERLOAD_STATUS_CODE`）。

[lib/llm/src/http/service/openai.rs:2301-2350](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L2301-L2350) — 判定阶梯主体：JSON 状态载荷（保留显式 HTTP 风格状态码，如 415）→ typed invalid_argument → 400 → overloaded → 过载状态码 → 兜底 500。**#13930 删除的分支就位于 typed 分支与兜底之间**——旧代码会在兜底前尝试 `error_str.strip_prefix("BackendInvalidArgument: ")` 恢复 400，现在未知错误一律按消毒 500 处理。

jail 侧的旁路实现（机制属 u4-l3，这里只看与错误类型相关的三处）：

[lib/llm/src/preprocessor.rs:4835-4861](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/preprocessor.rs#L4835-L4861) — 大段注释解释了问题与契约：jail 的 vendored finalize 无法区分「错误导致的 EOF」与「自然 EOF」，可能从残缺参数合成假的 `tool_calls` 完成帧；解法是 `terminal_error` 锁存第一个错误 Annotated，`take_while` 让它**根本到不了 jail**，出口侧的 `take_while`/`chain` 丢弃 jail 在该点之后的产出并替换为错误——「绝不让合成完成帧到达调用方」。

[lib/llm/src/preprocessor.rs:4926-4934](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/preprocessor.rs#L4926-L4934) — 入口映射处：`debug_assert!(a.error.is_none(), "terminal errors must bypass the jail")`，且 `JailAnnotated` 的 `error` 字段**无条件置 `None`**——注释说明终端错误已被上面的 `take_while` 移除、原始带类型 Dynamo 注解将在出口接回。

[lib/llm/src/preprocessor.rs:4970-4973](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/preprocessor.rs#L4970-L4973) — 出口映射处的第二道 `debug_assert`："dynamo-parsers must not construct errors"——jail（来自 dynamo-parsers 的 vendored 代码）不构造错误，错误只能来自 Dynamo 原生注解，因此不需要任何字符串转换。

行为规格由回放测试钉死（#13930 同步修正了它）：

[lib/llm/tests/responses_http_replay.rs:128-232](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/tests/responses_http_replay.rs#L128-L232) — `streaming_backend_error_closes_partial_output_and_counts_failure`：构造一个带类型的 `Backend(InvalidArgument)` 错误（场景正是「多模态内容发给未开启多模态的模型」），断言流式响应正确收尾（partial 输出的 done 帧依次关闭、`response.failed` 最后到达、无 `response.completed`），客户端拿到的错误码是 `invalid_prompt` 且 message 保留原始文本，指标计一次 `ErrorType::Internal` 的失败。**#13930 的改动点在第 146-149 行**：测试直接 `DynamoError::builder().error_type(Backend(InvalidArgument)).build()`，删掉了旧版里 `DynamoError::msg(typed_error.to_string())` 这层「模拟类型擦除」的包装——因为真实链路（jail 修复后）不再擦除类型，测试也就不需要再模拟擦除。

#### 4.7.4 代码实践

**实践目标**：验证「客户端错误 → 400/SSE error 帧」的提取路径，以及未知错误不再被字符串前缀恢复。

**操作步骤**：

1. 启动 sample 集群，发一条会触发后端校验失败的请求（sample 后端对非法参数的报错路径有限，若无法触发，改做第 2 步的源码阅读型实践）：
   ```bash
   # 非流式：观察错误信封的 code/type 字段
   curl -s -i localhost:8000/v1/chat/completions -H 'content-type: application/json' \
     -d '{"model":"sample-model","messages":[{"role":"user","content":"hi"}],"max_tokens":-5}' \
     | tail -1 | python3 -m json.tool

   # 流式 + pre-commit 窗口：同步错误应表现为 4xx 而非 SSE error 帧
   DYN_HTTP_PRE_COMMIT_ERROR_PEEK_MS=50 DYN_HTTP_PORT=8000 ./agg.sh --model-name sample-model
   curl -s -i -N localhost:8000/v1/chat/completions -H 'content-type: application/json' \
     -d '{"model":"sample-model","stream":true,"messages":[{"role":"user","content":"hi"}],"max_tokens":-5}' \
     | head -1
   ```
2. （源码阅读型）在 [openai.rs:2245](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L2245) 的 `extract_backend_error_if_present` 里做一次「考古」：
   - `git log -L 2245,2350:lib/llm/src/http/service/openai.rs --oneline | head -30` 查看这段函数最近的演变；
   - `git show 6841ea190a -- lib/llm/src/http/service/openai.rs` 查看 #13930 删除的 26 行；
   - 在笔记里回答：旧分支要恢复的字符串前缀是什么？它存在的前提（哪条路径会产出这种字符串）被哪个提交、在哪一层修掉了？
3. （可选，需 Rust 环境）跑回放测试确认行为规格仍然成立：
   ```bash
   cargo test -p dynamo-llm --test responses_http_replay streaming_backend_error
   ```

**需要观察的现象**：非法参数的请求返回 400（或流式下带 `event: error` 的 SSE 帧），错误信封是 OpenAI 风格 `{code, message, type}`；`git show` 能看到被删除的 `SERIALIZED_BACKEND_INVALID_ARGUMENT_PREFIX` 常量与 `strip_prefix` 分支；preprocessor.rs 中两处 `debug_assert` 与「error 恒为 None」的赋值。

**预期结果**：能用自己的话说出完整因果链——「jail 用 take_while 让终端错误绕行 → 类型注解原样链回 → HTTP 层凭 ErrorType 分类 → 字符串前缀恢复成为死代码被删除」。待本地验证。

#### 4.7.5 小练习与答案

**练习 1**：为什么 `invalid_argument` 的判定只看本节点的 `error_type()`，不沿 `source()` 链向下找？

**答案**：因为错误链是分层的——外层可能是 Unavailable 或 Internal（如路由失败），内层恰好裹了一个 InvalidArgument（如某个下层适配器的参数校验）。若沿链找，内层的 400 会覆盖外层本应报的 503/500，客户端会以为「改改请求就好」，实际问题是服务端/部署侧的。源码注释原话："An inner invalid argument must not override an outer unavailable or internal error"。诊断串（`error_str`）倒是会把整条链拼出来给日志用。

**练习 2**：删掉字符串前缀恢复分支后，什么情况下错误会「降级」为 500？这是回归吗？

**答案**：当错误事件不携带类型（`ErrorType::Unknown` 或泛型 DynamoError）且其 message 不含可解析的 JSON 状态载荷、又恰巧以 `"BackendInvalidArgument: "` 开头时——旧版会恢复 400，现在按未知错误消毒为 500。设计上这不是回归而是**收紧**：该分支的存在前提（适配器把类型序列化进 message）已由 #13930 在 jail 层修复，正常链路不会再产出这种字符串；对真正未知的错误，500 + 消毒本就是更安全的外部行为（不泄漏内部细节、不误导客户端重试）。回放测试删除 `DynamoError::msg(...)` 包装也印证了这一点。

**练习 3**：为什么 jail 的入口和出口各要一个 `debug_assert`，而不是只在一处检查？

**答案**：两处断言守护的是**两个不同方向的契约**。入口处（`terminal errors must bypass the jail`）保证进入 jail 映射的 `Annotated` 不可能带错误——如果违反，说明 `take_while` 的扣留逻辑被破坏，错误会再次面对「被 jail finalize 吞掉/改写」的风险；出口处（`dynamo-parsers must not construct errors`）保证 jail（vendored 的 dynamo-parsers 代码）自身不产错误——如果违反，说明 jail 内部开始合成 `JailAnnotated.error`，而下游的错误分类只认 Dynamo 原生类型，字符串化的错误会退化成 500。`debug_assert` 只在 debug 构建生效，release 零开销——这是把「不变量」变成可执行文档的典型手法。

---

### 4.8 Generate 端点的指标生命周期：GenerateMetricLifecycle（#11385）

#### 4.8.1 概念说明

`/inference/v1/generate` 是三类端点里最「裸」的一个：token_ids 进、token_ids 出，**绕过分词器与后处理管线**（请求里的 token 已经是渲染好的，不需要模板、媒体计数、detokenize）。这带来一个指标上的空洞——chat/completions 端点的响应指标（TTFT、ITL、OSL、cached tokens）本来是从后处理管线产出的 `LLMMetricAnnotation` 里观察到的，generate 端点根本没有这条注解流。

#11385（+1150 行）补上了这个空洞，思路不是新写一套指标，而是**复用 metrics.rs 的三件套**并换一个观察点：

- `InflightGuard`：在途请求 gauge（`dynamo_frontend_active_requests`）；
- `HttpQueueGuard`：从 handler 开始到首 token 的排队 gauge（`dynamo_frontend_queued_requests`）；
- `ResponseMetricCollector`：TTFT/ITL/OSL/ISL/cached tokens 等 histogram 与 counter。

新的组合体叫 `GenerateMetricLifecycle`：**在 handler 一进来就建立**（哪怕请求随后被 400/404/503 拒绝，也计入 `requests_total` 的失败序列），然后**移动进 dispatch 任务**，在流上逐帧 `observe`，最终随任务结束 drop。这解决了 generate 端点一个特有的结构问题：指标状态要跨越「handler 返回 → spawn 出去的 dispatch 任务」这条所有权边界，用一个结构体把三个 guard 与 tracker 打包搬运，比在两个函数间传一堆可变引用干净得多。

另一个关键设计是 **span 字段的「先声明、后补写」**：`generate_dispatch_span` 用 `tracing::field::Empty` 声明六个字段（`input_tokens`/`output_tokens`/`ttft_ms`/`avg_itl_ms`/`prefill_worker_id`/`decode_worker_id`），真正的值由 `ResponseMetricCollector` 的 `Drop` 实现在 collector 销毁那一刻 `span.record(...)` 补上——因为 TTFT/ITL 只有到请求结束才知道全貌。这就把「Prometheus 指标」与「单请求 tracing 摘要」统一到了同一份数据采集点上。

还有一条容易被忽略的细节：**指标用的模型名是归一过的**。请求里的 `model` 先经 `ModelManager::resolve_canonical_name` 把别名解析成主名；若请求没带 model，则把「按能力发现的模型列表」整体过一遍同样的解析再去重。解析结果再经 `metric_model_for` 收敛——没注册的名字一律换成哨兵 `unknown_model`，防止客户端随便编造模型名把 Prometheus 的 label 基数打爆。

#### 4.8.2 核心流程

```text
handler_generate（handler 一进来就做，早于任何就绪检查）
 ├─ 1. 归一模型名
 │     request.model 有值 → resolve_canonical_name(model)     # 别名 → 主名
 │     request.model 为空 → list_generate_models_for_capability(...)
 │                          .map(resolve_canonical_name) 去重   # 能力发现 + 归一
 ├─ 2. metric_model = metric_model_for(resolved)               # 未注册 → "unknown_model"
 ├─ 3. tracker = RequestTracker::new(); tracker.record_isl(token_ids.len())
 ├─ 4. dispatch_span = generate_dispatch_span(request_id, metric_model)
 │        # target="request_span"，六个字段声明为 Empty
 ├─ 5. dispatch_span.in_scope(|| GenerateMetricLifecycle::new(state, metric_model, ...))
 │        ├─ inflight: InflightGuard(model, Endpoint::Generate, unary, request_id)
 │        └─ collector: GenerateMetricCollector
 │             ├─ response: ResponseMetricCollector
 │             ├─ http_queue: Some(HttpQueueGuard)     # 首 token 前 hold
 │             ├─ tracker / input_tokens / output_tokens=0
 │             └─ worker_info_observed = false          # worker 标签只抄一次
 ├─ 6. 之后的每个早退分支（!ready→503 / 校验失败→400 / stream→501 /
 │        无模型→404 / 多模型→400 / 模型不就绪→503 / 取引擎失败）：
 │        metric_lifecycle.mark_error(generate_metric_error_type(status)); return
 └─ 7. tokio::spawn(generate_dispatch(...).instrument(dispatch_span))
          │   # dispatch 整个跑在 span 里 ⇒ collector 的 drop 也发生在 span 里
          ├─ engine.generate(context) → stream
          ├─ stream.map(|mut a| { collector.observe(&mut a); a })
          │     observe 每帧做五件事：
          │       a. routing_data.take() → tracker.set_external_timing / 
          │          set_external_worker_info / set_external_query_token_ids
          │       b. observe_worker_info()（仅一次）→ response.set_worker_info(
          │            prefill_id/rank/type, decode_id/rank/type)
          │       c. cached_tokens = completion_usage.prompt_tokens_details.cached_tokens
          │            （仅当 usage.prompt_tokens == 本请求 input_tokens 才采信）
          │       d. output_tokens += token_ids.len(); observe_current_osl(output_tokens)
          │       e. 首 token 且 chunk>0 → drop(HttpQueueGuard)   # 排队结束
          │          response.observe_response(input_tokens, chunk_tokens)
          └─ 结束：mark_ok / mark_error(Cancelled|Unavailable|Internal)
                    → collector 在 span 内 drop
                      → ResponseMetricCollector::drop 补写 span 六字段
                        + flush ITL histogram + 最终 OSL histogram
```

关于第 6 步的 `generate_metric_error_type`：它把 HTTP 状态码映射回指标用的 `ErrorType`（400→Validation、404→NotFound、501→NotImplemented、429/529→Overload、503→Unavailable、499→Cancelled、其余 5xx→Internal），保证 `requests_total` 的 `error_type` label 与响应状态码语义一致。

关于 cached tokens 的两条规则（容易考）：**流中**采信后端 `completion_usage`，但带一个过滤条件——`usage.prompt_tokens == self.input_tokens` 才算数，因为**迁移重试**（RetryManager）会把已产出的 token 拼回新请求的 prompt，此时 attempt 本地的 usage 不再代表这个逻辑请求；**drop 时**再兜底一次 `tracker.cached_tokens()`（路由器侧的估计），填补后端没报或被迁移撑大的情况。谁先给出有效值用谁，两边都有时后端优先。

#### 4.8.3 源码精读

先看两个新结构体与它们的装配：

[lib/llm/src/http/service/generate.rs:646-659](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L646-L659) — `GenerateMetricCollector` 的 doc 注释直说了动机：generate 端点「刻意绕过产出 `LLMMetricAnnotation` 的分词器/后处理管线，token ID 已经渲染好，所以从**原始 token delta** 观察同样的响应指标，同时不碰分词器与媒体指标」。字段就是三件套的引用加两个计数器和一个「worker 信息是否已抄送」的 bool。

[lib/llm/src/http/service/generate.rs:661-691](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L661-L691) — `GenerateMetricLifecycle`：包着 `InflightGuard` + `GenerateMetricCollector` + `metric_model`。`new()` 一口气建好 inflight guard（`Endpoint::Generate`、unary）与 collector；`mark_error()` 只是转发给 inflight guard——注意它**不动 collector**，失败请求不该往 TTFT/OSL histogram 里灌零值。

[lib/llm/src/http/service/generate.rs:693-708](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L693-L708) — `GenerateMetricCollector::new`：`create_response_collector` + `create_http_queue_guard`，就是 4.2 节讲过的 metrics.rs 三件套工厂。generate 端点没有复用 `create_inflight_guard` 之外的开销——一切与 chat 端点同源。

span 的声明与「补写」两端：

[lib/llm/src/http/service/generate.rs:153-166](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L153-L166) — `generate_dispatch_span`：`info_span!` 指定 `target: "request_span"`，六个业务字段全部 `tracing::field::Empty`——此刻只有 `request_id` 与 `model` 有值。

[lib/llm/src/http/service/metrics.rs:1942-1995](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/metrics.rs#L1942-L1995) — `ResponseMetricCollector::drop` 的后半段就是「补写端」：flush ITL histogram、只为真正产出过 token 的请求记最终 OSL（注释解释了给失败请求记 0 会污染 histogram 的 sum/count），然后 `tracing::Span::current()` 上 `span.record("input_tokens"/"output_tokens"/"ttft_ms"/"avg_itl_ms"/"prefill_worker_id"/"decode_worker_id")`。注释点明动机："InflightGuard::Drop and on_response logs will inherit these"——span 关闭前补的字段会被同 span 的后续日志继承。**这段代码不是 #11385 新写的**（chat 端点早就在用），新的是 generate 端点把 collector 的 drop 安排进了自己的 dispatch span。

handler 侧的接线：

[lib/llm/src/http/service/generate.rs:793-825](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L793-L825) — 指标生命周期的建立点，注意它发生在 `check_ready` **之前**：别名解析（807 行）→ 隐式模型按能力发现并归一（794-803 行，`canonical_generate_models` 的定义在 [generate.rs:49-59](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L49-L59)，用 `BTreeSet` 顺带去重排序）→ `metric_model_for`（809-813 行）→ 建 tracker 并 `record_isl` → `dispatch_span.in_scope(|| GenerateMetricLifecycle::new(...))`（816-825 行）。`in_scope` 保证 `HttpQueueGuard` 的「开始排队」时间戳落在 span 内。

[lib/llm/src/http/service/generate.rs:61-73](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L61-L73) — `generate_metric_error_type`：状态码 → `ErrorType` 的映射表，每个早退分支的 `mark_error` 都用它。

[lib/llm/src/http/service/generate.rs:941-965](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L941-L965) — dispatch 的 spawn 与 `.instrument(dispatch_span)`：这一行是 span 补写能生效的**关键**——collector 随 `GenerateMetricLifecycle` move 进 dispatch 任务，任务又整体 instrument 在 dispatch_span 里，所以 drop 发生时 `Span::current()` 正是那个声明了六个 Empty 字段的 span。

流上逐帧观察的实现：

[lib/llm/src/http/service/generate.rs:711-728](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L711-L728) — `observe_worker_info`：从 `tracker.get_worker_info()` 把 prefill/decode 的 worker id、dp rank、worker type **抄送一次**给 `ResponseMetricCollector`（`worker_info_observed` 防重复）。worker 信息是路由器沿 `routing_data` 注解带回来的，可能晚于首帧到达，所以每帧都试、抄到即止。

[lib/llm/src/http/service/generate.rs:730-774](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L730-L774) — `observe` 全文，五步对应 4.8.2 流程图的 a–e。两段注释值得读：`filter(|usage| usage.prompt_tokens as usize == self.input_tokens)` 旁边解释了**迁移重试会把已交付 token 计入新 attempt 的 prompt**，所以 attempt 本地的 usage 不能代表逻辑请求；`chunk_tokens` 旁边解释 RetryManager 只 yield 新生成的 delta，所以逐帧累加**跨迁移仍然精确**。

[lib/llm/src/http/service/generate.rs:777-785](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L777-L785) — `Drop for GenerateMetricCollector`：兜底 `tracker.cached_tokens()`——「后端匹配的 usage 出现时是权威（流中已 latch）；这个逻辑请求级的路由器估计用来填补缺失或被迁移撑大的 attempt usage」。

[lib/llm/src/http/service/generate.rs:1026-1030](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L1026-L1030) — 消费点：`stream.map(move |mut annotated| { metric_collector.observe(&mut annotated); annotated })`——指标观察以 `&mut` 借用方式嵌在流上，原始帧原样放行给 `GenerateResponse::from_annotated_stream_with_options` 聚合，指标层对业务零侵入。

模型名归一的两侧定义：

[lib/llm/src/discovery/model_manager.rs:601-610](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/discovery/model_manager.rs#L601-L610) — `resolve_canonical_name`：查别名表，命中返回主名，未命中原样返回。

[lib/llm/src/discovery/model_manager.rs:1009-1022](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/discovery/model_manager.rs#L1009-L1022) — `metric_model_for`：注册过的名字原样返回（保留大小写），否则返回 [model_manager.rs:119](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/discovery/model_manager.rs#L119) 的 `UNKNOWN_METRIC_MODEL`（`"unknown_model"`）——doc 注释明说这是为了「未知模型的请求不要污染 Prometheus label 基数」，且要求**所有**在引擎查找之前创建的 metric child 都用它。

行为规格（同文件测试区，全部为 #11385 新增）：

[lib/llm/src/http/service/generate.rs:2222-2251](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L2222-L2251) — `generate_dispatch_span_uses_resolved_request_id`：用一个捕获 span 字段的 tracing Layer 断言 span 用的是**解析后的** request id，且六个业务字段全部存在于 span metadata。

[lib/llm/src/http/service/generate.rs:2593-2759](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L2593-L2759) — `successful_generate_populates_frontend_metrics`：一次成功请求后逐个断言 `dynamo_frontend_active_requests`/`queued_requests` 归零、`request_duration_seconds` 计 1、ISL=3/OSL=2、`output_tokens_total`=2、TTFT 与 ITL histogram 各计 1、`cached_tokens`=2，以及带 worker 标签的 `WORKER_LAST_*` gauge 三件——这张断言清单本身就是「generate 端点暴露哪些指标」的权威索引。

[lib/llm/src/http/service/generate.rs:2761-2824](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L2761-L2824) — `split_router_worker_metadata_populates_generate_metrics`：手工构造带 `routing_data.worker_id`（prefill/decode 各一个 id）的帧，断言 worker 标签确实从 `RequestTracker` 流进了指标——对应 4.8.2 的 a/b 两步。

[lib/llm/src/http/service/generate.rs:2825-2859](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L2825-L2859) 与 [generate.rs:2861](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L2861) 起 — 两条 cached_tokens 规格测试：`generate_metrics_fall_back_to_tracker_cached_tokens`（后端不报时用 tracker 估计）与 `migrated_generate_uses_logical_request_cache_metrics`（迁移场景下 attempt usage 被过滤、以逻辑请求口径计数）。

[lib/llm/src/http/service/generate.rs:1521-1566](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L1521-L1566) — `generate_alias_uses_primary_model_for_routing_and_metrics`：用别名发请求，断言路由与**指标 label** 都用主名——4.8.1 讲的归一链路的行为规格。

[lib/llm/src/http/service/generate.rs:1566-1660](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L1566-L1660) — `generate_early_failures_are_counted`：各种早退（无模型/多模型/stream/坏参数）都要计入失败指标——「生命周期建立在就绪检查之前」的行为规格。

#### 4.8.4 代码实践

**实践目标**：让 `/inference/v1/generate` 真正跑起来（或退化为跑测试），观察三件事——① inflight/queued gauge 随压力起伏；② TTFT/ITL/OSL/cached_tokens 各 histogram 有样本；③ `request_span` 上的六个字段在日志里带值出现。

**操作步骤**：

1. **有 vLLM 环境时**（generate 端点需要 worker 广播 `VLLM_INFERENCE_V1_GENERATE_CAPABILITY`，sample 假后端不会广播）：启动 vLLM 后端并打开端点，然后压测（示例代码，非项目自带）：
   ```bash
   DYN_VLLM_ENABLE_INFERENCE_V1_GENERATE=1 DYN_HTTP_PORT=8000 python3 -m dynamo.frontend
   for i in $(seq 1 50); do
     curl -s -X POST localhost:8000/inference/v1/generate \
       -H 'content-type: application/json' \
       -d '{"model":"<你的模型>","token_ids":[1,2,3,4,5],"stream":false,
            "max_tokens":32}' > /tmp/gen_$i.json &
   done; wait
   ```
2. 压测中与压测后各采样一次指标：
   ```bash
   curl -s localhost:8000/metrics | grep -E 'dynamo_frontend_(active|queued|request_duration|input_sequence|output_sequence|output_tokens_total|time_to_first|inter_token|cached_tokens)' | grep -v '^#'
   ```
3. 打开 request_span 日志再发一条请求（让 span 字段可见）：
   ```bash
   RUST_LOG=request_span=info python3 -m dynamo.frontend   # 具体级别名待本地验证
   curl -s -X POST localhost:8000/inference/v1/generate -H 'content-type: application/json' \
     -d '{"model":"<你的模型>","token_ids":[1,2,3],"stream":false,"max_tokens":8}'
   # 在日志里找 target=request_span 的 generate 事件，检查
   # input_tokens/output_tokens/ttft_ms/avg_itl_ms/prefill_worker_id/decode_worker_id
   ```
4. **无 vLLM 环境时**（本讲的兜底路径，纯本地可跑）：
   ```bash
   cargo test -p dynamo-llm --lib http::service::generate::tests
   ```
   重点看 `successful_generate_populates_frontend_metrics`、`split_router_worker_metadata_populates_generate_metrics`、`generate_early_failures_are_counted` 三个测试的断言输出。

**需要观察的现象**：

- 步骤 2：压测中 `dynamo_frontend_active_requests` 与 `dynamo_frontend_queued_requests` 大于 0，压测后归零；`dynamo_frontend_time_to_first_token_seconds` 与 `inter_token_latency_seconds` 的 sample_count 等于成功请求数；`output_tokens_total` 是 counter（只增）。
- 步骤 3：`request_span` 的 generate 事件里六个字段**有值**（不再是 Empty），且 `ttft_ms` 与首 token 到达时刻对得上、`avg_itl_ms` 大致等于总时长减 TTFT 除以 token 间隔数。
- 步骤 4：generate 模块的全部测试通过（含 4.8.3 列出的指标测试）。

**预期结果**：整理一张「指标名 → 类型（gauge/histogram/counter）→ 采点（InflightGuard 建立/HttpQueueGuard 释放/observe 每帧/collector drop）」的对照表。若第 1 步环境不可得，以第 4 步的测试断言 + 源码精读为产出。generate 端点在 sample 后端上返回 404 本身也值得记录——那是能力发现（`VLLM_INFERENCE_V1_GENERATE_CAPABILITY`）在防串台，且这条 404 现在也会计入 `requests_total{error_type="not_found"}`。待本地验证。

#### 4.8.5 小练习与答案

**练习 1**：为什么 `GenerateMetricLifecycle` 要在 `check_ready` **之前**建立，而不是等到真正取到引擎之后？

**答案**：因为指标要覆盖「被拒绝的请求」。generate 端点的失败大头恰恰发生在早期——服务未就绪（503）、参数不合法（400）、stream 未实现（501）、模型不存在（404）。若生命周期建立在就绪检查之后，这些请求就 from 指标里消失，`requests_total` 只统计成功路径，过载与配置错误在监控上不可见。建立之后每个早退分支都 `mark_error(generate_metric_error_type(status))` 再返回（generate.rs:827-873 的每个分支），`generate_early_failures_are_counted` 测试钉死了这一行为。代价是失败请求也会短暂占用一个 inflight 计数——这正确反映了「handler 正在处理它」的事实。

**练习 2**：`ttft_ms` 是在哪一行代码写进 span 的？为什么不在首 token 到达时立即写？

**答案**：在 [metrics.rs:1982-1984](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/metrics.rs#L1982-L1984)——`ResponseMetricCollector::drop` 里 `span.record("ttft_ms", ...)`。不立即写有两个原因：其一，span 字段一旦记录还可以再覆盖，但 `tracing` 的惯用法是「摘要字段在 span 关闭前统一补写」，这样同一 span 的所有日志（包括 InflightGuard drop 时的完成日志）都能继承完整字段——metrics.rs 注释原话 "InflightGuard::Drop and on_response logs will inherit these"；其二，`avg_itl_ms` 这类字段本来就只有到结束才能算（`itl_sum_secs / itl_count`），统一在 drop 时写让所有摘要字段的时机一致。而 drop 能落在正确的 span 里，靠的是 dispatch 任务 `.instrument(dispatch_span)`（generate.rs:953）。

**练习 3**：一个客户端用乱编的模型名 `fake-model-x` 连发 1000 条请求，Prometheus 会新建多少个模型 label 序列？为什么？

**答案**：零个新序列——全部落到既有的 `unknown_model` 哨兵上。`handler_generate` 用 `metric_model_for`（model_manager.rs:1016-1022）把未注册名收敛成 `UNKNOWN_METRIC_MODEL`（`"unknown_model"`，model_manager.rs:119），doc 注释明说目的是防止未知模型名污染 label 基数。对比一下：如果不做这个收敛，1000 个不同的乱造名字就是 1000 个新时间序列，Prometheus 的内存与查询都会被拖垮——这是「把不可控输入映射到有界标签集」的标准手法。

**练习 4**：请求在中途被迁移到另一个 worker 重试，`output_tokens` 会不会重复计数？cached_tokens 会不会被高估？

**答案**：都不会，两处各有防护。`output_tokens`：RetryManager 把已交付 token 拼进重试请求的 prompt 但**只 yield 新生成的 delta**，所以逐帧 `chunk_tokens = token_ids.len()` 累加对逻辑请求精确（generate.rs:761-764 的注释）。cached_tokens：流中采信后端 usage 前先过滤 `usage.prompt_tokens == self.input_tokens`——迁移后的 attempt prompt 变长（含已产出 token），这个过滤把 attempt 本地口径挡掉；drop 时兜底的 `tracker.cached_tokens()` 是逻辑请求级的路由器估计（generate.rs:749-758 与 777-785）。`migrated_generate_uses_logical_request_cache_metrics` 测试钉死了这条规格。

---

### 4.9 FinishReason 的宽容反序列化（#13984）

#### 4.9.1 概念说明

`FinishReason` 是后端告诉前端「这条请求为什么停止」的枚举：`eos`、`length`、`stop`、`cancelled`（别名 `abort`）、`content_filter`，以及带消息的 `Error(String)`。它是**跨进程序列化类型**——worker 把它写进流式输出的最后一个 chunk，frontend 反序列化后再决定返回 200 还是 5xx。

它的线契约（wire contract）是「一端保守、一端宽容」：

- **序列化（保守）**：单元变体输出裸字符串（`"stop"`），`Error` 输出单键 map（`{"error":"<msg>"}`）——所有 Rust 生产者都发这个形态；
- **反序列化（宽容）**：除了上述形态，还接受 `Display` 形态的字符串 `"error: <message>"`——因为 Python 引擎适配器（如 `dynamo.vllm` 的 custom-encoder 路径）用 `str(error)` 上报失败；**#13984 之后**，连裸 `"error"`（无消息）也接受，落回诊断消息 `"backend emitted finish_reason=error without a message"`。

这次变更是一次**立场翻转**，值得把新旧两个 doc 注释对照读：

- **旧版（`b4338ab8`）**：「裸 `"error"` 被故意拒绝。发出它的生产者正在丢弃自己手里的消息；在这里接受它会用编造的文本掩盖缺陷，而不是让缺陷保持可见。」——此时生产者应当改抛 `dynamo._core.InvalidArgument`，让前端拿到带真实消息的 400。
- ****新版（`d7f06b591`）**：「裸 `"error"` 被防御性接受并落回诊断消息，**避免后端的畸形错误升级成 frontend 的反序列化失败**。」

为什么翻转？关键在**故障的爆炸半径**。反序列化失败发生在 frontend 聚合响应流的路径上——一个本该表现为「这条请求失败、错误信封回给客户端、指标记一次 error」的事件，会变成「整条响应无法解码」的 5xx，错误信息完全丢失（客户端只看到 internal error，日志里是 serde 报错而不是后端的原始线索）。两害相权：接受裸 `"error"` 最多是**这条请求**带一条不够精确的错误消息；拒绝它则把**可观测性也一起毁掉**。再叠加 `lib/llm/AGENTS.md` 的 N-2 混部要求（「宽容的读者、保守的写者」「默认部署路径上的无条件解析失败通常是兼容性 bug」）——某个版本的 worker 或某个 Python 适配器就是会发出裸 `"error"`，frontend 不该因此宕掉整条响应。

#### 4.9.2 核心流程

反序列化的判定阶梯（自定义 `Deserialize` 走 `deserialize_any`，由线数据自身形态分流）：

```text
线数据到达（JSON / msgpack，均为自描述格式）
 └─ deserialize_any(FinishReasonVisitor)
      ├─ 是字符串 → visit_str → str::from_str（FinishReason::from_str）
      │    ├─ "eos" | "length" | "stop"                       → 单元变体
      │    ├─ "cancelled" | "abort"                           → Cancelled
      │    ├─ "content_filter"                                → ContentFilter
      │    ├─ "error"            ← #13984 新增分支
      │    │     → Error("backend emitted finish_reason=error without a message")
      │    ├─ "error: <msg>"                                   → Error(<msg>)
      │    │     （消息内部再含 "error: " 也原样保留）
      │    └─ 其他                                              → Err(Invalid FinishReason variant)
      └─ 是 map    → visit_map
           ├─ 首键 == "error" 且无第二键 → Error(next_value)
           └─ 其他（未知键 / 多键 / 空 map）→ Err
```

序列化方向不变：`Error(msg)` 只输出 `{"error": msg}`——**读宽容、写保守**，Dynamo 自己产出的形态永远是规范的 map 形式。

请求面 codec 用的是 `rmp_serde::to_vec_named`（msgpack），msgpack 的字符串值同样走 `visit_str` → `from_str`，所以裸 `"error"` 的 msgpack 形态自动被新分支覆盖——新增的回归测试正是用 msgpack 钉的（见 4.9.3）。

错误最终如何在 HTTP 面暴露：`generate_dispatch` 聚合流时，`Error` 型 `FinishReason` 会被转成 `MaybeError`，generate 端点返回**消毒过的** 500（消息不外泄，见 4.9.3 的测试）；chat 端点则走 4.7 节的 `extract_backend_error_if_present` 分类阶梯。

#### 4.9.3 源码精读

[lib/llm/src/protocols/common.rs:48-62](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/common.rs#L48-L62) — `FinishReason` 的 doc 注释即线契约文档：序列化形态（裸串/单键 map）、`Display` 形态的来由（Python 适配器）、以及 #13984 后的裸 `"error"` 防御性接受说明。**改这段注释正是本提交的核心**——契约文档与行为一起翻新。

[lib/llm/src/protocols/common.rs:63-82](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/common.rs#L63-L82) — 枚举定义：`#[serde(rename = "error")]` 只约束 `Error` 的**序列化**名（裸 `"error"` 恰好与单元变体同名，这是旧版能「故意拒绝」而新版能「字符串直收」的字面基础）；`Cancelled` 带 `alias = "abort"`。

[lib/llm/src/protocols/common.rs:97-114](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/common.rs#L97-L114) — **本次改动的落点**：`FromStr` 新增第 107-109 行的 `"error"` 分支。注意它排在 `"error: "` 前缀分支**之前**（match 的字面量优先于守卫分支），且诊断消息是固定字符串。

[lib/llm/src/protocols/common.rs:116-129](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/common.rs#L116-L129) — 自定义 `Deserialize` 用 `deserialize_any`：注释解释了这要求**自描述格式**（请求面的 `rmp_serde::to_vec_named` 满足；裸 bincode 这类非自描述格式会在这里失败）——这是「字符串还是 map 由线数据决定」的前提。

[lib/llm/src/protocols/common.rs:136-150](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/common.rs#L136-L150) — `expecting` 消息现在把 `"error"` 列为合法形态（错误提示同步更新）；`visit_str` 直接委托 `value.parse()`，并让 parse 的具体失败原因经 `E::custom` 透传（而不是泛泛的 invalid_value）。

行为规格（同文件测试区）：

[lib/llm/src/protocols/common.rs:836-863](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/common.rs#L836-L863) — `test_finish_reason_deserializes_every_producer_form`：map 形态、`Display` 形态、消息内嵌 `"error: "` 的嵌套用例逐一断言。

[lib/llm/src/protocols/common.rs:879-887](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/common.rs#L879-L887) — **#13984 新增**的 `test_finish_reason_accepts_bare_error_from_msgpack`：`rmp_serde::to_vec_named("error")` 编出的 msgpack 必须反序列化成带诊断消息的 `Error`。用 msgpack 钉是因为请求面 codec 就是它——这条测试守住的是 worker→frontend 内部跳的真实形态。

[lib/llm/src/protocols/common.rs:888-903](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/common.rs#L888-L903) — `test_finish_reason_rejects_unknown_forms`：`"nonsense"`、`{"nonsense":"boom"}`、双键 map、空 map、整数仍然拒绝——**#13984 从这份拒绝清单里删掉了 `r#""error""#`**，宽容只放开这一种形态。

错误在 HTTP 面的终点（与 4.7、4.8 呼应）：

[lib/llm/src/http/service/generate.rs:2465-2482](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/generate.rs#L2465-L2482) — `backend_error_finish_returns_sanitized_500`：终端 `FinishReason::Error("sensitive backend failure")` 到达时，generate 端点返回 500 且响应体只有 `"internal server error"`——断言 `!body.to_string().contains(secret)`。错误细节进日志与指标（`mark_error(ErrorType::Internal)`，见 4.8），不进客户端响应。

#### 4.9.4 代码实践

**实践目标**：亲手验证裸 `"error"` 的接受路径、看到「立场翻转」的提交现场，并跑通全部 FinishReason 回归测试。

**操作步骤**：

1. 跑协议层测试（无需任何服务，只需 Rust 工具链）：
   ```bash
   cargo test -p dynamo-llm --lib protocols::common::tests::test_finish_reason
   ```
2. 做一次「立场考古」（只读 git 操作）：
   ```bash
   git show d7f06b591f -- lib/llm/src/protocols/common.rs
   # 重点对照两段：
   #  (a) doc 注释里被删除的「A bare "error" ... stays rejected on purpose」四行
   #      与新增的「accepted defensively with a diagnostic fallback message」；
   #  (b) 测试区：删掉的「bare error stays rejected」注释块 vs 新增的 msgpack 测试，
   #      以及 rejects_unknown_forms 清单里消失的 r#""error""#。
   ```
3. （离线推演，示例代码非项目自带）用 Python 模拟一个发裸 `"error"` 的后端 chunk，确认 JSON 与 msgpack 两个形态都在新阶梯的接受范围内：
   ```python
   import json, msgpack           # pip install msgpack
   chunk_json = json.dumps({"token_ids": [], "finish_reason": "error"})
   chunk_msgpack = msgpack.packb({"token_ids": [], "finish_reason": "error"}, use_bin_type=True)
   # 两种形态经 FinishReasonVisitor 都应得到
   # Error("backend emitted finish_reason=error without a message")
   # 而 {"finish_reason": "error: boom"} 得到 Error("boom")，
   #    {"finish_reason": "nonsense"} 仍然是 Err。
   ```
4. （可选）对照 [lib/llm/AGENTS.md](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/AGENTS.md) 的「N-2 Worker / Frontend Compatibility」一节，找出哪句话直接预言了这次翻转（提示："Prefer tolerant readers and conservative writers"）。

**需要观察的现象**：步骤 1 全绿；步骤 2 的 diff 里能看到「删四行注释 + 加三分支 + 测试清单增删」的完整对照；步骤 3 的推演表三种输入三种结果。

**预期结果**：能用自己的话写出一行结论——「`FinishReason` 的读侧现在覆盖三种 error 形态（map、`error: msg`、裸 `error`），写侧仍只发 map 形态；裸形态的代价是错误消息不精确，收益是后端畸形不再炸掉 frontend 的响应反序列化」。步骤 1 的测试结果待本地验证。

#### 4.9.5 小练习与答案

**练习 1**：旧版注释说「接受裸 error 会用编造的文本掩盖缺陷」，新版接受了它——被「掩盖」的缺陷去哪了？

**答案**：去了两处可观测的地方，而不是消失。其一，诊断消息本身是自描述的：`"backend emitted finish_reason=error without a message"` 明确告诉你**后端发了裸 error**——看到它就知道生产侧有 bug，只是不再是「解不开整条响应」这种灾难式发现；其二，这条请求在指标上仍然计一次失败（generate 路径 `mark_error(ErrorType::Internal)`，chat 路径走 4.7 的分类阶梯），Grafana 上看得到。工程取舍是：**缺陷的可见性从「解析失败炸响应」降级为「日志与指标里的一条记录」，而客户端不再承受连错误都拿不到的 5xx。**

**练习 2**：为什么 `Deserialize` 用 `deserialize_any` 而不是像普通枚举那样 `#[derive(Deserialize)]`？

**答案**：因为 `Error` 变体的线形态是**单键 map**，而其余变体是**裸字符串**——反序列化器必须先看线数据是什么形态再决定怎么解析，这正是 `deserialize_any` 的用途（要求自描述格式，common.rs:116-122 的注释专门讨论了这一点，并指出请求面用的 `rmp_serde::to_vec_named` 满足、裸 bincode 不满足）。派生的 `Deserialize` 没法表达「同一个枚举按值形态分流」这种外部标签（externally tagged）之外的混合契约，所以手写 visitor：`visit_str` 走 `FromStr`（字符串三形态统一在那里判定），`visit_map` 只处理 `{"error": msg}`。

**练习 3**：后端发来 `finish_reason: "error: CustomEncoder failed: error: nested"`，前端解析出的消息是什么？为什么这不是歧义？

**答案**：消息是 `"CustomEncoder failed: error: nested"`——完整保留，内嵌的 `"error: "` 不被误切。因为 `FromStr` 的匹配是**整串相等判断 `"error"` + 前缀判断 `starts_with("error: ")` 后取余全串**（common.rs:107-110），只剥最外层一次前缀，不做递归或反复剥离。[common.rs:849-853](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/common.rs#L849-L853) 的测试用例就是这条嵌套消息。真正歧义的场景是后端想表达「消息恰好是空串」——`"error: "` 剥完前缀后是空串，会得到 `Error("")`；而裸 `"error"` 得到的是诊断消息，两者可区分。

## 5. 综合实践

把本讲九个模块串成一条完整的「请求考古」任务：

**任务**：对一条 `curl -N .../v1/chat/completions`（stream=true）请求，产出一张「时间线表」，左侧是客户端可观察的现象，右侧是对应的源码位置。

建议步骤：

1. 启动慢速 sample 集群（4.2 节的命令，`--delay 0.05 --max-tokens 100`）。
2. 带上 `x-request-id: demo-001` 发流式请求，用 `curl -N` 观察：
   - 响应头里是否回显了 `x-request-id`（→ `echo_request_id_header`）；
   - 首帧到达前的等待（→ 引擎 generate + pre-commit 窥视未开启时直接提交 200）；
   - 每帧的 `id`/`object`/`created` 是否恒定（→ `DeltaGeneratorState`）；
   - 末帧的 `finish_reason` 与 usage（→ `stream_options` 与 `enable_usage_for_nonstreaming` 对照，以及 #12562 的取 max 规则）。
3. 压测期间采样 `/metrics`，记录 `inflight_requests` 的峰值与回落（→ `InflightPermit`）。
4. 压测中发 SIGTERM，记录：新请求的状态码（503）、`/live` 的状态码（200）、frontend 日志中的 draining/stopping 两条日志、在途流式请求是被完整发完还是被截断（→ `ServiceStage` 状态机 + `wait_inflight_zero_or_timeout`）。
5. 最后对同一条 prompt 调一次 `/v1/responses/input_tokens`，把估算值写进时间线的最左端——它是这条请求「还没发生时」就能知道的第一个数字（→ `handler_responses_input_tokens`）。
6. 补两个错误与字节格式的观察：对 `/v1/embeddings` 分别用 float 与 base64 两种 `encoding_format` 各发一条，记录响应形状差异（→ `decode_base64_to_floats`）；再发一条非法参数请求，记录错误信封与状态码（→ `extract_backend_error_if_present` 的类型化阶梯）。
7. （#11385/#13984 新增）按 4.8.4 打开 `/inference/v1/generate` 并压测（无 vLLM 环境则跑 `cargo test -p dynamo-llm --lib http::service::generate::tests` 与 `protocols::common::tests::test_finish_reason`），记录：inflight/queued gauge 的起伏、TTFT/ITL histogram 的 sample_count、`request_span` 事件上的六个字段值（→ `GenerateMetricLifecycle` 与 `ResponseMetricCollector::drop`）；再对照 `git show d7f06b591f` 说一遍 `FinishReason` 从「故意拒绝」到「防御性接受」的理由（→ `FromStr` 的裸 `error` 分支）。
8. 把以上观察各写一行「现象 → 源码文件:行号」的对照，作为本讲的产出物。

如果只能做静态分析（无运行环境），改为：通读 [service_v2.rs:103-132](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/service_v2.rs#L103-L132)、[openai.rs:2947-3040](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L2947-L3040) 与 [openai.rs:2245-2350](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/openai.rs#L2245-L2350)，手绘这条时间线并标注每一步的函数名。

## 6. 本讲小结

- **HttpService 是「一个 Router + 一份共享 State」**：`HttpServiceConfig` 用 builder 声明式描述端点开关与路径，`build()` 组装系统路由（无 inflight 中间件）、推理路由（有 inflight 中间件 + trace）与协议兼容的 404 兜底，路径全部可用 `DYN_HTTP_SVC_*_PATH` 覆盖；RL 发现面走独立端口与独立 Router。
- **`InflightPermit` 是记账器不是限流器**：`acquire_inflight()` 无上限、无拒绝，职责是精确追踪「响应体未完成」的请求数，服务优雅关停与 Prometheus 指标；真正的准入在路由器与引擎侧。
- **生命周期三态机 Ready→Draining→Stopping** 与 runtime 取消令牌解耦：draining 期间推理路由 503、`/live` 仍 200、`/health` 503，`wait_inflight_zero_or_timeout`（默认 5s，可用 `DYN_HTTP_GRACEFUL_SHUTDOWN_TIMEOUT_SECS` 调）等在途响应发完；许可以「响应体」为生命周期。
- **三类协议端点共享一套 State，差异在 handler 内部**：openai.rs 做最深的整形与校验，anthropic.rs 是同构 handler + 协议信封转换，generate.rs 是「不透明信封 + 按能力路由」的实验性引擎原生通道。
- **流式增量由 DeltaGenerator 产出，状态抽在 delta_common.rs**：`DeltaGeneratorState` 统一持有响应身份/usage/选项/TTFT 计时，chat 与 completions 共享；非流式强制带 usage；#12562 后 completion_tokens 对后端 `completion_usage` 取 max（后端报请求总量、报 0 时保留本地累计）。
- **`POST /v1/responses/input_tokens`**：本地 len/3 估算、不做就绪/模型门、绝不触达后端，`instructions`/`tools`/工具调用 item 全额计入并对齐 chat 转换器的合并规则——为 RL rollout 预算与客户端预检提供零成本预检。
- **Embeddings 走 base64(LE f32) 内部线格式**：worker 永远发 base64、HTTP 出口按 `encoding_format` 决定是否转浮点；#12157 后编解码统一在 preprocessor.rs 的 `decode_base64_to_floats`（协议类型在 protocols/openai/embeddings.rs，含 `add_special_tokens`/`truncate_prompt_tokens`）。
- **#13930 后错误类型全程存活**：jail 用 `take_while` 让终端错误绕行、原始带类型注解链回，HTTP 层凭 `ErrorType` 分类（InvalidArgument→400、过载→可配状态码、未知→消毒 500），`BackendInvalidArgument: ` 字符串前缀恢复分支被删除——类型即契约，取代字符串约定。
- **#11385 后 generate 端点指标自持**：`/inference/v1/generate` 绕过分词器/后处理管线，改由 `GenerateMetricLifecycle`（`InflightGuard` + `HttpQueueGuard` + `ResponseMetricCollector` + `RequestTracker`）从**原始 token delta** 观察同样的响应指标；生命周期建立在就绪检查之前（早退也计数），模型名经 `resolve_canonical_name` + `metric_model_for` 归一并收敛到 `unknown_model` 哨兵以保 label 基数；worker 标签从 `routing_data` 经 `RequestTracker` 抄送，cached tokens 以后端 usage 为权威、以 tracker 估计兜底，且过滤迁移 attempt 的本地口径。
- **span 摘要字段的补写时机在 drop**：`request_span` 上六个字段先以 `field::Empty` 声明，由 `ResponseMetricCollector::drop` 统一 `span.record`——dispatch 任务 `.instrument(dispatch_span)` 保证 drop 落在正确的 span 里，同一 span 的完成日志随之继承完整字段。
- **#13984 后 `FinishReason` 读宽容、写保守**：反序列化接受 map 形态、`"error: <msg>"` Display 形态与裸 `"error"`（落回诊断消息 `"backend emitted finish_reason=error without a message"`，msgpack 路径有专门回归测试），序列化仍只发规范 map 形态；立场从「拒绝以暴露缺陷」翻转为「接受以保住 frontend 的可解码性」——N-2 混部下「宽容的读者」原则的落地。

## 7. 下一步学习建议

- **下一讲（u4-l3）**顺着「引擎拿到请求之前」往前走：`OpenAIPreprocessor` 如何做模板渲染、媒体计数与分词，`LocalModel` 如何加载 tokenizer——那是 handler 调 `engine.generate` 之后、真正到达 worker 之前的整形层；本讲 4.7 提到的 jail 终端错误旁路（preprocessor.rs 的 `take_while`/`chain` 与两处 `debug_assert`）也将在那里展开完整机制。
- 想看「指标三件套怎么被消费」，直接读 [lib/llm/src/http/service/metrics.rs](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/http/service/metrics.rs) 中 `InflightGuard`/`HttpQueueGuard`/`ResponseMetricCollector` 的实现，并对照 u12-l1 的可观测性讲义——4.8 节的 generate 指标家族（`dynamo_frontend_*`）与 `request_span` 字段正是那讲看板对照的素材。
- 想追 `FinishReason` 的「另一半」：它序列化后如何随 `LLMEngineOutput` 跨进程、迁移重试如何只 yield 新 delta（`RetryManager`），会在 u7（分离式服务）与 u12-l4（故障容忍）中展开；N-2 兼容原则的完整文本在 [lib/llm/AGENTS.md](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/AGENTS.md)。
- 想追 `input_tokens` 的完整动机与 RL 服务面（`/v1/rl/workers`、nvext token-in/token-out、`RlWorkerMetadata`），直接跳到 u8-l7；估算器与 chat↔Responses 转换器的等价性测试就在 [responses_http_replay.rs:728-832](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/tests/responses_http_replay.rs#L728-L832)。
- 想看 embeddings 的另一端（Python worker 如何 `torch→numpy.tobytes→base64` 编码、多进程嵌入池如何分词对齐），跳到 u8-l8——本讲的 `decode_base64_to_floats` 就是那条字节链的 Rust 落点。
- 想理解「请求进入引擎之后怎么跨进程」：回到 u3-l4（请求面 Pipeline）与 u3-l3（AsyncEngine 抽象），本讲的 `engine.generate(request)` 就是那两条链的入口。
- 建议动手实验：把 `DYN_HTTP_GRACEFUL_SHUTDOWN_TIMEOUT_SECS` 调成 1 秒重做 4.2 的实践，观察在途长流被截断的行为差异；再用 `DYN_HTTP_SVC_RESPONSES_PATH` 带尾部斜杠重启，验证 4.5 练习 2 的派生规则；最后做一遍 4.7 的 git 考古，亲手看到被删除的字符串前缀分支。
