# 特殊服务面：prefill-only 门与 LoRA 路由

## 1. 本讲目标

学完本讲，你应该能够：

1. 走查 prefill-only 校验门的完整执行路径：读 body → 解析 JSON → 判定 `max_tokens`/`max_completion_tokens` 恰为 1 → 重写 Content-Length → 转发内层路由，否则返回 400。
2. 解释 `Engine::lora` 上的 `Option<LoraClient>` 为什么就是「这个引擎能管理 LoRA 适配器」这一能力本身，以及为什么未铸造控制通道的引擎连「unsupported」错误都不会有。
3. 复述 `--enable-lora` 从 CLI flag 一路到 `serve_model_with_lora_routes` 的完整触发链，并说清 main.rs 的 LoRA 分支为何必须串行等待引擎、为何只接受 `LaunchedEngine::Stepped`。
4. 自己动手用 `tower::ServiceExt::oneshot` 驱动一个挂了 prefill-only 门的 axum Router，观察 400 与透传两种结局。

## 2. 前置知识

### 2.1 回顾：路由扩展链的最里层

在 u4-l2 我们看过装配骨架把最终路由包成三层洋葱：

```
normalize_chat_requests( mark_history( extend_router(router) ) )
        最外：effort 别名改写    内：边界测量      最里：形态选择
```

`extend_router` 有三种形态：默认服务是恒等函数（`|router| router`）；本讲的两个「特殊服务面」正是另外两种——prefill-only 门（`request_contract::prefill_only_routes`）与 LoRA 路由合并（一个 merge + fallback 闭包）。也就是说，**「这台服务器以什么契约对外服务」这个决定，落点就在 `extend_router` 这一层的函数选择上**，而选择权在 `ServePlan`（u2-l5）手里。

### 2.2 P/D 分离与 prefill-only 角色

u2-l5 提过 `ServePlan::prefill_only` 字段。在 P/D（prefill/decode 分离）部署里，一个实例可以只扮演 prefill 角色：它只计算 prompt 的 KV 缓存并保存/导出，不做自回归生成。在 OpenAI 协议上，「只 prefill、不生成」被表达为 `max_tokens=1`——算完 prompt 后产出恰好一个 token 就停。`prefill_only` 置位的实例（如 GLM5.2 TP4 P/D prefill 角色）必须拒绝一切别的请求形状，否则上游会误以为它在正常生成。这个「拒绝」不是引擎内部的调度器规则，而是**在 HTTP 路由层、请求触达引擎之前**就完成的协议契约检查。

### 2.3 LoRA 是什么

LoRA（Low-Rank Adaptation）是一种轻量的模型微调方法：冻结基座权重，只为若干线性层附加一对低秩矩阵 \( A \in \mathbb{R}^{r \times d} \)、\( B \in \mathbb{R}^{d \times r} \)（秩 \( r \ll d \)），推理时叠加 \( BA \) 的输出。一个「适配器（adapter）」就是一组这样的小矩阵，体积远小于基座。服务侧的价值：一个基座可以同时驻留多个适配器，每个请求点名用哪一个——比每个微调副本都载入一份基座便宜得多。

本讲关心的是服务面：适配器如何动态装卸（管理路由）、请求如何点名适配器（`model` 字段）、控制命令如何到达引擎（控制通道）。

### 2.4 axum/tower 速成：Router、fallback_service 与 oneshot

- **axum `Router`** 是一个路由表 + 回调的服务类型；`Router::new().route("/path", post(handler))` 注册路径。
- **`fallback_service(inner)`**：给外层 Router 装一个「兜底服务」——凡是没有命中已注册路径的请求，整包转交给 `inner`。本讲两个特殊路由都用它把「自己管的路径」和「其余一切」分开。
- **`tower::ServiceExt::oneshot(request)`**：不经过网络，直接把一个构造好的 `Request` 喂给服务并拿到 `Response`，是 axum 生态里测试中间层/路由的标准手法（项目测试大量使用）。
- **缓冲改写模式**：中间层要用 `axum::body::to_bytes(body, limit)` 把流式 body 读成完整字节，检查/改写后重新构造 `Body::from(bytes)`，并**重写 `Content-Length` 头**——原始头描述的是进来的流（可能根本没有该头），重建的 body 需要一个准确的头。u4-l2 的 effort 改写、本讲的 prefill 门与 LoRA 转发器共用这个模式。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `pegainfer-frontend/src/vllm/request_contract.rs` | prefill-only 校验门：两条 OpenAI 生成路由上的 `max_tokens=1` 强制检查 |
| `pegainfer-frontend/src/engine/control.rs` | LoRA 控制通道的词汇表：`LoraControl` 命令、`LoraClient` 异步客户端、`LoraControlError` |
| `pegainfer-frontend/src/vllm/lora.rs` | LoRA 路由层：适配器装卸管理路由、OpenAI 代理路由（model 改写 + xarg 注入）、`--lora-modules` 解析、启动装载 |
| `pegainfer-frontend/src/vllm/mod.rs` | `serve_prefill_only_with_engine_count` 与 `serve_model_with_lora_routes` 两个 serve 入口，及路由扩展链的包裹位置 |
| `pegainfer-server/src/main.rs` | 三条 serve 分支的选择：LoRA 串行分支、prefill-only 分支、默认分支 |
| `pegainfer-frontend/src/engine/wiring.rs` | `Engine` 交接包上的 `lora: Option<LoraClient>` 字段（能力声明处） |
| `pegainfer-frontend/src/vllm/wire.rs` | `LORA_ADAPTER_XARG` 常量与桥侧从 sampling params 提取适配器名 |
| `pegainfer-qwen3/src/model_line.rs` | 一条真实模型线的 LoRA CLI（`--enable-lora` 等）与 `serve_plan` 折算 |
| `pegainfer-qwen3/src/frontend_adapter.rs` | 引擎侧铸造 LoRA 通道、在 step 顶部排空命令的接线（辅读） |

## 4. 核心概念与源码讲解

### 4.1 prefill-only 校验门

#### 4.1.1 概念说明

prefill-only 实例对外的协议契约是「每个生成请求必须 `max_tokens=1`」。这个契约若靠引擎内部检查，错误要等请求穿过 HTTP 层、引擎桥、调度器之后才暴露，且错误形态是引擎的内部错误而非 HTTP 语义。所以它被实现为一个 **axum 层的校验中间层**：请求在进入 vLLM 路由（更别说引擎）之前，先被拦下检查 body，不合格立即返回 400 与 OpenAI 风格的错误 JSON。这带来三个好处：

1. 错误以 HTTP 状态码和 `invalid_request_error` 类型表达，上游 SDK 能直接理解；
2. 引擎永远不会收到违反其角色的请求，调度器无需为此设分支；
3. 检查逻辑集中在一个 ~90 行的文件里，可独立用 `oneshot` 测试。

#### 4.1.2 核心流程

```
POST /v1/completions 或 /v1/chat/completions
        │
        ▼
┌─ validate_prefill_only ──────────────────────────┐
│ 1. to_bytes 缓冲 body（上限 16 MiB）             │
│ 2. serde_json 解析为 Value                        │
│ 3. 取 max_tokens、max_completion_tokens（as_u64） │
│ 4. chat 路径：两者都给 → 必须相等，否则 400        │
│ 5. effective = chat ? max_completion_tokens       │
│                .or(max_tokens) : max_tokens       │
│ 6. effective 必须是 Some(1)，否则 400             │
│ 7. 重写 Content-Length = body.len()               │
│ 8. vllm_router.oneshot(重建的请求) 透传           │
└───────────────────────────────────────────────────┘
其余路径（/v1/models、/health 等）→ fallback_service 直接进内层路由，不检查
```

判定表（`T` = 该字段的 u64 值，`—` = 缺失或非 u64）：

| 路径 | max_tokens | max_completion_tokens | effective | 结果 |
|------|-----------|----------------------|-----------|------|
| `/v1/completions` | 1 | 任意（不看） | 1 | 透传 |
| `/v1/completions` | 2 或 `—` | 任意 | 2 或 None | 400 |
| `/v1/chat/completions` | 1 | 1 | 1 | 透传 |
| `/v1/chat/completions` | 1 | 2 | —（先报冲突） | 400（conflicts） |
| `/v1/chat/completions` | `—` | 1 | 1 | 透传 |
| `/v1/chat/completions` | `—` | `—` | None | 400（omitted） |

#### 4.1.3 源码精读

**外层路由与兜底。** 门构造一个新的 Router，只注册两条 OpenAI 生成路径，其余一切交给 `fallback_service` 原样放行——所以 prefill-only 实例的 `/v1/models` 等非生成路由行为与默认服务完全一致：[request_contract.rs:L16-L22](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/request_contract.rs#L16-L22)。注意 `.with_state(vllm_router.clone())` 把内层 Router 当作 handler 的 state 传入，闭包里再 clone 一次作为 fallback。

**错误形态。** 任何检查失败都折叠成一个 400 响应，body 是 OpenAI 风格的错误信封，`message` 用 `anyhow` 的 `{error:#}` 展开完整因果链：[request_contract.rs:L24-L42](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/request_contract.rs#L24-L42)。`type` 与 `code` 都是 `invalid_request_error`——与 vLLM 上游对非法请求的分类一致，上游 SDK 的错误分支能直接命中。

**核心判定。** 缓冲与解析（16 MiB 上限 `REQUEST_BODY_LIMIT` 在 [L14](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/request_contract.rs#L14)）：[request_contract.rs:L48-L53](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/request_contract.rs#L48-L53)。两个 token 上限字段用 `as_u64` 提取——字符串、浮点、负数都会得到 `None`，等价于「缺失」，最终落入 `omitted` 的 400 分支，这就是格式错误也在门前被拦截的原因。chat 路径的同给冲突检查与 effective 折算：[request_contract.rs:L58-L72](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/request_contract.rs#L58-L72)——`/v1/completions` 只认 `max_tokens`，OpenAI 的老接口本来就没有 `max_completion_tokens`，给了也不看（给了 1 仍然 400，见判定表第 2 行，这是有测试锁定的刻意行为）。契约本体只有一条 `ensure!`：[request_contract.rs:L73-L77](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/request_contract.rs#L73-L77)，错误消息把实际收到的值（或 `omitted`）拼进去，用户不需要猜哪里错。

**重写头并透传。** 通过检查的请求在这里重建：[request_contract.rs:L78-L86](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/request_contract.rs#L78-L86)。门本身不改 body 字节（长度其实没变），但请求可能是以 chunked 编码到来的（此时根本没有 `Content-Length` 头），而 `parts.headers` 是原样继承的——从缓冲字节重建 body 就必须重设一个准确的头。`oneshot` 把请求整包交给内层 vLLM Router，它的响应原样返回给客户端。

**serve 侧的接线。** prefill 分支最终把 `prefill_only_routes` 作为 `extend_router` 传入骨架：[vllm/mod.rs:L158-L182](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L158-L182)，函数文档写明「引擎在此拒绝它不服务的东西，请求到达引擎之前先 400」。骨架最终把它包进 `mark_history` 与 `normalize_chat_requests` 之内：[vllm/mod.rs:L493-L500](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L493-L500)。也就是说一个 prefill 请求的实际旅程是：effort 别名改写 → 会话边界测量 → **prefill 门** → vLLM 路由 → 引擎桥。

**现成的行为锁定测试。** 两个测试分别锁接受案与驳案：[request_contract.rs:L100-L115](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/request_contract.rs#L100-L115) 断言 chat 路径三种 `=1` 的写法都透传到内层（内层是个返回 200 的假 Router）；[request_contract.rs:L117-L136](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/request_contract.rs#L117-L136) 断言 `max_tokens=2`、两字段冲突、`/v1/completions` 只给 `max_completion_tokens` 三种形状都 400。这两个测试就是判定表的机器可读版本。

#### 4.1.4 代码实践

1. **实践目标**：亲手复现 prefill-only 门的行为——`max_tokens=2` 得到 400 且错误 JSON 含 `invalid_request_error`；`max_tokens=1` 透传到内层兜底路由。

2. **操作步骤**：

   a. 先跑项目自带的行为锁定测试（无需 GPU）：

   ```bash
   cargo test --release -p pegainfer-frontend --lib prefill_only
   ```

   b. 由于 `prefill_only_routes` 是 `pub(crate)`，草稿实践需仿写。在仓库外建一个独立草稿 crate（避免改动源码仓库）：

   ```bash
   cargo new /tmp/prefill-gate-lab
   cd /tmp/prefill-gate-lab
   ```

   在 `Cargo.toml` 加依赖（版本取自本 workspace 的根 `Cargo.toml`：axum 0.8、tower 0.5、tokio 1、serde_json 1、anyhow 1）：

   ```toml
   [dependencies]
   axum = "0.8"
   tower = { version = "0.5", features = ["util"] }
   tokio = { version = "1", features = ["full"] }
   serde_json = "1"
   anyhow = "1"
   ```

   c. 在 `src/lib.rs` 里仿写一个最小门（**示例代码**，逻辑按真源码裁剪，保留判定与转发骨架）：

   ```rust
   use axum::Json;
   use axum::Router;
   use axum::body::Body;
   use axum::body::to_bytes;
   use axum::extract::Request;
   use axum::http::StatusCode;
   use axum::response::IntoResponse;
   use axum::response::Response;
   use axum::routing::post;
   use serde_json::json;
   use tower::ServiceExt as _;

   pub fn prefill_gate(inner: Router) -> Router {
       Router::new()
           .route("/v1/completions", post(validate))
           .with_state(inner.clone())
           .fallback_service(inner)
   }

   async fn validate(
       axum::extract::State(inner): axum::extract::State<Router>,
       request: Request,
   ) -> Response {
       let (mut parts, body) = request.into_parts();
       let body = to_bytes(body, 16 * 1024 * 1024).await.expect("body");
       let value: serde_json::Value = serde_json::from_slice(&body).expect("json");
       let max_tokens = value.get("max_tokens").and_then(|v| v.as_u64());
       if max_tokens != Some(1) {
           return (
               StatusCode::BAD_REQUEST,
               Json(json!({
                   "error": {
                       "message": format!("prefill only routes require max_tokens=1, got {}",
                           max_tokens.map_or("omitted".to_owned(), |v| v.to_string())),
                       "type": "invalid_request_error",
                       "code": "invalid_request_error"
                   }
               })),
           ).into_response();
       }
       parts.headers.insert(
           axum::http::header::CONTENT_LENGTH,
           axum::http::HeaderValue::from_str(&body.len().to_string()).unwrap(),
       );
       inner.oneshot(Request::from_parts(parts, Body::from(body)))
           .await
           .expect("inner router")
   }

   #[cfg(test)]
   mod tests {
       use super::*;
       use axum::http::StatusCode;
       use axum::routing::any;

       #[tokio::test]
       async fn gate_rejects_and_forwards() {
           // 内层只装一个 fallback：没命中路径的请求都到它这里，
           // 正好观察「透传到内层 fallback」。
           let inner = Router::new().fallback(|| async { "inner-fallback" });
           let gate = prefill_gate(inner);

           // max_tokens=2 → 400，错误 JSON 含 invalid_request_error
           let req = Request::post("/v1/completions")
               .body(Body::from(r#"{"max_tokens":2}"#)).unwrap();
           let resp = gate.clone().oneshot(req).await.unwrap();
           assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
           let bytes = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
           let err: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
           assert_eq!(err["error"]["type"], "invalid_request_error");

           // max_tokens=1 → 透传到内层 fallback
           let req = Request::post("/v1/completions")
               .body(Body::from(r#"{"max_tokens":1}"#)).unwrap();
           let resp = gate.oneshot(req).await.unwrap();
           assert_eq!(resp.status(), StatusCode::OK);
           let bytes = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
           assert_eq!(&bytes[..], b"inner-fallback");
       }
   }
   ```

   d. 运行 `cargo test`。

3. **需要观察的现象**：第一条请求被门拦下，状态 400，body 是含 `invalid_request_error` 的 JSON 信封；第二条请求穿过门，响应来自内层 fallback（`inner-fallback`）。

4. **预期结果**：草稿测试两条断言全过；`cargo test --release -p pegainfer-frontend --lib prefill_only` 报告 `prefill_only_contract_accepts_chat_token_aliases` 与 `prefill_only_contract_rejects_invalid_or_conflicting_limits` 两个用例通过（用例名即源码中的测试函数名）。草稿运行结果待本地验证（示例代码的行为由源码逻辑推出，未替你执行）。

#### 4.1.5 小练习与答案

**练习 1**：对 `/v1/completions` 发 `{"max_completion_tokens": 1}`（不带 `max_tokens`）会怎样？为什么这条规则和 `/v1/chat/completions` 不同？

**答案**：400，`effective_max_tokens` 是 `None`（`/v1/completions` 分支只读 `max_tokens`），错误消息为 `... got omitted`。差异的原因是协议本身：`/v1/completions`（旧版补全接口）只有 `max_tokens` 字段，`max_completion_tokens` 是 chat 接口的新拼写，所以在 completions 路径上它不是合法的替身。这条行为由测试 `("/v1/completions", r#"{"max_completion_tokens":1}"#) → BAD_REQUEST` 显式锁定。

**练习 2**：门为什么要把 `/v1/models`、`/health` 这类路径放行？如果门不放行会发生什么？

**答案**：门的契约只约束「生成请求」——prefill-only 角色的限制是「不做自回归生成」，不是「不服务任何 HTTP」。路由发现、健康检查、模型列表与普通实例行为一致，上游才能用同一套客户端管理它。若不放行（例如把检查挂成全局 middleware），这些路径要么 404 要么 400，实例在集群里看起来像坏了。实现上就是 `fallback_service(vllm_router)` 一行。

**练习 3**：请求以 `Transfer-Encoding: chunked` 到达（没有 `Content-Length`），门转发时为什么必须手工重写该头？

**答案**：门用 `to_bytes` 把流式 body 缓冲成完整字节再重建 `Body::from(body)`。原始 `parts.headers` 里要么没有 `Content-Length`（chunked），要么描述的是已被消费的流；重建后的 body 是一个已知长度的整体，下游若按头读定长会出错。所以转发前用 `body.len()` 重设（[request_contract.rs:L78-L82](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/request_contract.rs#L78-L82)）。

### 4.2 LoRA 控制通道：LoraClient 是能力本身

#### 4.2.1 概念说明

u3-l5 讲过 `Scheduler` trait 只有三个方法——`submit`、`step`、`metrics`——并且「超出三方法的能力由调度器自建通道、在自己的 step 里排空」。LoRA 适配器的动态装卸就是这句话的实例：装载/卸载适配器会改变引擎的权重状态，必须由调度器在安全时机（批空闲时）应用，因此它不适合塞进 `Scheduler` 契约，而是**一条独立的控制通道**。

这条通道的设计核心是一个类型层面的决定：引擎交接包 `Engine` 上挂着 `lora: Option<LoraClient>`。**这个 `Option` 就是能力本身**——`Some` 表示「这个引擎能管理适配器，拿着客户端就能下命令」；`None` 表示「这个引擎没有这门能力」。注意这里**不存在「unsupported」错误路径**：没有铸造通道的引擎压根不会挂载 LoRA 路由，无人能「问」它，也就无需回答。对比一下常见的替代设计（布尔开关 `supports_lora` + 调用时返回 `Unsupported` 错误）：开关与客户端可能不一致（开关开着但客户端忘了给），而 `Option` 里要么有可用的客户端、要么没有，第二种真相源不存在。lora.rs 测试里的一条注释把这一点说得很直白：「An engine without LoRA never mounts these routes at all — the `Option` on `Engine::lora` is the capability」。

#### 4.2.2 核心流程

```
引擎侧（模型 crate，以 qwen3 为例）                    服务侧（frontend）
─────────────────────────                            ─────────────────────
launch 时：
  LoraClient::channel() ──铸出──▶ (client, rx)
  rx 交给调度器（spawn 前捕获）
  Engine { lora: Some(client), .. } ──────────────▶ serve_model_with_lora_routes
                                                      │ 取出 client（None → 显式报错）
                                                      ▼
调度器每一步：                                    /v1/load_lora_adapter 路由
  step 顶部排空 rx → pending_control                  │ client.load(req).await
  批空闲时应用命令 → oneshot 应答 ◀───────────────────┘
  （命令改权重前，其后到达的生成请求先缓在
   post_control_deferred，防止用错适配器集合）
```

命令往返（`roundtrip`）的两端都可能失败：`send` 失败说明接收端（调度器线程）已消失；`rx.await` 失败说明引擎收到命令但应答槽被 drop（引擎在应用前拆除了）。两种都折叠为 `LoraControlError::EngineGone`。

#### 4.2.3 源码精读

**模块文档——设计意图的第一手陈述**：[control.rs:L1-L11](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/control.rs#L1-L11)。三句话概括：通道由引擎自己铸造；接收端在 `step` 内排空、批空闲时应用；词汇表（命令类型）定义在 frontend 层——**客户端的使用者定义词汇**——使 frontend 不依赖任何模型 crate 类型。

**命令词汇**：[control.rs:L30-L45](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/control.rs#L30-L45)。`LoraControl` 三个变体 `Load`/`Unload`/`List`，每个都自带一个 `oneshot::Sender` 应答槽——命令与「往哪儿回话」是一个值，不存在关联 id 的簿记。接收端类型是 `crossbeam_channel::Receiver<LoraControl>`：[control.rs:L47-L49](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/control.rs#L47-L49)——选 crossbeam 而非 tokio mpsc，因为消费者是调度器线程里的同步代码（与 u3-l6 里「提交通道选 crossbeam」同理）。

**错误词汇**：[control.rs:L51-L58](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/control.rs#L51-L58)。`EngineGone`（引擎或应答在回答前消失）与 `Failed(String)`（引擎应用命令失败，原文回传）。

**客户端与铸造**：[control.rs:L60-L73](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/control.rs#L60-L73)。`LoraClient` 只是 `crossbeam Sender` 的包装，`Clone` 成本是一次通道引用计数；`channel()` 一行铸造双端。三个异步方法 `load`/`unload`/`list` 全部委托给同一个 `roundtrip`：[control.rs:L75-L103](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/control.rs#L75-L103)——`send` 与 `await` 各映射到 `EngineGone`，引擎侧的 `Err(String)` 映射到 `Failed`。客户端是异步的（HTTP handler 直接 `.await`），但传输是同步队列：命令瞬时入队，等待只发生在应答槽上。

**能力声明处**：`Engine` 交接包的字段：[wiring.rs:L145-L153](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L145-L153)，字段注释即设计格言：「The `Option` is the capability — no separate flag exists to disagree with it」（不存在能与它不一致的独立开关）。

**引擎侧的铸造与排空（以 qwen3 为例，辅读）**：通道在调度器线程吞掉接收端**之前**铸造：[frontend_adapter.rs:L200-L207](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-qwen3/src/frontend_adapter.rs#L200-L207)（`lora_control` 为假时两端都是 `None`——引擎如实说「我没有这门能力」）。接收端存进调度器状态，注释写明「在每个 step 的顶部排空进 `pending_control`」，且命令待应用期间新到的生成请求缓存在 `post_control_deferred`，防止它们跑在一个即将被改变的适配器集合上：[frontend_adapter.rs:L240-L249](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-qwen3/src/frontend_adapter.rs#L240-L249)。这正是 u3-l5「超契约能力由调度器自建通道」的落地代码。

#### 4.2.4 代码实践

1. **实践目标**：验证 `LoraClient` 在引擎缺席时的行为——所有命令都得到 `EngineGone`，以及 `channel()` 铸出的双端如何配合。

2. **操作步骤**：

   a. 运行 frontend 里现成的 LoRA 路由测试（无需 GPU，内含用 drop 接收端模拟「引擎已消失」的帮手 `gone_engine_client`）：

   ```bash
   cargo test --release -p pegainfer-frontend --lib lora
   ```

   b. 阅读帮手的注释与实现：[lora.rs:L411-L418](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/lora.rs#L411-L418)——`LoraClient::channel()` 后立刻丢弃接收端（`let (client, _rx) = ...`），此后每次往返 `send` 都失败，于是全部折叠为 `EngineGone`。注意注释同时说明：引擎没铸造通道时路由根本不挂载，所以「unsupported」不是一个可达的路由状态。

   c. 想亲手验证往返，可在草稿 crate（见 4.1.4 的 `/tmp/prefill-gate-lab`，需再加 `pegainfer-frontend` 依赖——仅当 workspace 内可用时）写（**示例代码**）：

   ```rust
   use pegainfer_frontend::engine::{LoraClient, LoraControlError};

   #[tokio::test]
   async fn engine_gone_when_receiver_dropped() {
       let (client, rx) = LoraClient::channel();   // 铸出双端
       drop(rx);                                   // 引擎线程消失
       let err = client.list().await.unwrap_err();
       assert_eq!(err, LoraControlError::EngineGone);
   }
   ```

   若让 `rx` 活着但不排空，`client.list()` 会一直等应答——这印证了「命令入队即时、等待只在应答槽」的结构。（此草稿依赖 workspace 内 crate，运行结果待本地验证；不改仓库的前提下也可以只在心中推演 `roundtrip` 的两个 `map_err` 分支。）

3. **需要观察的现象**：`gone_engine_client` 系测试全部通过（三个路由测试断言 503/400）；接收端存活但不排空时客户端会挂起而非报错。

4. **预期结果**：`cargo test --release -p pegainfer-frontend --lib lora` 全绿（含 `load_lora_adapter_route_reports_gone_engine` 等用例）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `LoraControlError` 里没有 `Unsupported` 变体？

**答案**：因为「不支持」这个状态在类型上不可达。没有 LoRA 能力的引擎 `Engine.lora` 是 `None`，`serve_model_with_lora_routes` 在装路由前就因 `None` 而报错退出（见 4.4.3），LoRA 路由不存在于该服务上；而挂载了路由的服务必有可用客户端。错误枚举只需覆盖「能力存在但执行失败」的两类：引擎消失（`EngineGone`）与引擎拒绝/失败（`Failed`）。

**练习 2**：命令通道为什么用 crossbeam 而应答槽用 tokio oneshot？

**答案**：两端运行在不同世界。命令的消费者是调度器线程里的同步 `step` 代码（crossbeam 的同步 `recv`/`try_recv` 正合适，且与 u3-l6 提交通道的选型同理）；应答的消费者是 HTTP handler 里的异步代码，`oneshot::Receiver` 实现 `Future` 可以直接 `.await`，让出线程而不是阻塞 tokio worker。

**练习 3**：引擎收到 `Load` 命令后、真正应用前，调度器如何保证不会有生成请求刚好跑在旧的适配器集合上？

**答案**：qwen3 的调度器在 `step` 顶部把通道排空进 `pending_control` 队列，命令待应用期间新提交的生成请求缓存在 `post_control_deferred`，等命令应用完再放行（[frontend_adapter.rs:L245-L249](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-qwen3/src/frontend_adapter.rs#L245-L249) 的字段注释）。控制命令与生成请求在同一调度线程上串行化，不需要任何锁。

### 4.3 LoRA 路由层：管理路由与 OpenAI 代理路由

#### 4.3.1 概念说明

LoRA 服务面在 HTTP 上呈现两组路由，都定义在 lora.rs：

- **管理路由** `/v1/load_lora_adapter`、`/v1/unload_lora_adapter`：vLLM 风格的适配器装卸接口，handler 做参数校验后直通 `LoraClient`，是控制通道的 HTTP 门户。
- **OpenAI 代理路由** `/v1/models`、`/v1/completions`、`/v1/chat/completions`：让客户端**用适配器名当 `model` 字段**来点名适配器。代理路由把 `model` 改写回基座名、把适配器名注入 `vllm_xargs` 侧信道，再转交给真正的 vLLM 路由。

「适配器名当 model 名」是 vLLM 生态的惯例：客户端不需要新 API，`model: "my-adapter"` 就选用了 `my-adapter`。`/v1/models` 则把基座名与已装载适配器名合并列出，客户端能发现可用适配器。两组路由共享一个 `Arc<RwLock<HashSet<String>>>`（已装载适配器名集合），管理路由装卸时更新它，代理路由改写时查它。

#### 4.3.2 核心流程

**管理路由**（以 load 为例）：

```
POST /v1/load_lora_adapter {lora_name, lora_path, load_inplace?, is_3d_lora_weight?}
  → 本地校验：name 非空 / path 非空 / is_3d_lora_weight 必须为 false（否则 400）
  → control.load(LoadLoraAdapterRequest).await
      Ok        → adapter_names.insert(name) → 200 "Success: ... added successfully."
      EngineGone→ 503 {"error": "engine is gone"}
      Failed(m) → 400 {"error": m}
```

**OpenAI 代理改写决策树**（`rewrite_lora_request_body`）：

```
读 body JSON
  ├─ 没有 "model" 字符串字段 ──────────────▶ 原样放行（交给 vLLM 自己报错）
  ├─ model == base_model_name ────────────▶ 原样放行（基座请求，无需改写）
  ├─ model ∉ adapter_names（读锁查集合）──▶ 原样放行（未知名字留给上游判 404）
  └─ model ∈ adapter_names ───────────────▶ 改写：
        model ← base_model_name
        vllm_xargs["pegainfer_lora_adapter"] ← 适配器名（保留 xargs 里已有的键）
        重新序列化 body → 重写 Content-Length → oneshot 进 vLLM 路由
```

改写注入的 `vllm_xargs` 键随后开始它的**侧信道旅程**：vLLM 上游把它放进 `sampling_params.extra_args` 原样下传；stepped 桥用 `lora_adapter_from_sampling_params` 提取（空串/非字符串直接拒绝该请求）并写进 `Request::lora_adapter` 字段，随提交进入调度器——调度器据此为该请求选适配器。整条链上「适配器选择」从不占用 OpenAI 协议的正式字段，全部走 `vllm_xargs`。

#### 4.3.3 源码精读

**路由注册与状态**。管理路由：[lora.rs:L144-L155](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/lora.rs#L144-L155)，state 是 `{control: LoraClient, adapter_names}`。OpenAI 代理路由：[lora.rs:L157-L173](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/lora.rs#L157-L173)，state 持有内层 `vllm_router`、基座名、served 名列表与适配器名集合。

**load handler 的三段式**：[lora.rs:L175-L218](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/lora.rs#L175-L218)。先本地校验（空名、空路径、三维 LoRA 权重直接 400，后者消息 `is_3d_lora_weight=true is not supported`）；再直通控制通道；按三种结果映射响应——成功是**纯文本** 200（`Success: LoRA adapter '...' added successfully.`，vLLM 兼容文案），`EngineGone` 是 503，`Failed` 是 400。unload handler 同构：[lora.rs:L220-L256](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/lora.rs#L220-L256)，成功时从集合移除名字。注意「先命令成功、后更新集合」的顺序——集合只反映引擎确认过的状态。

**启动装载**：[lora.rs:L268-L291](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/lora.rs#L268-L291)。`--lora-modules` 列出的适配器在服务启动（HTTP 端口 bind）前逐个经控制通道装载（`load_inplace: false`），失败带 `failed to load startup LoRA module {name} from {path}` 上下文中止启动——启动期适配器缺位是致命的，与运行期装卸（可重试的 4xx/5xx）待遇不同。

**代理改写本体**：[lora.rs:L341-L377](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/lora.rs#L341-L377)。决策树对应代码的四个早退分支；关键细节是 `map.entry("vllm_xargs").or_insert_with(...)`——**若 body 里已有 `vllm_xargs` 对象（例如外层 `mark_history` 已写入边界测量结果，见 u4-l4），只追加键、不整体覆盖**。改写后 body 字节真的变了，所以 Content-Length 重写在这里不是防御性的而是必需的：[lora.rs:L318-L339](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/lora.rs#L318-L339)（缓冲上限放宽到 128 MiB，`LORA_ROUTE_BODY_LIMIT` 在 [L32](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/lora.rs#L32)，因为带长 prompt 的生成请求远大于管理请求）。

**models 列表**：[lora.rs:L379-L405](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/lora.rs#L379-L405)。served 名（空则退回基座名）与适配器名合并、排序、去重后逐条产出 OpenAI model 卡片（`owned_by: "vllm-frontend-rs"`，`created: 0`）——排序去重保证响应稳定，不随装载顺序抖动。

**侧信道的两端**。常量定义：[wire.rs:L19](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/wire.rs#L19)——键名 `pegainfer_lora_adapter`，带项目前缀以免与上游 vLLM 的 xargs 撞名。桥侧提取与校验：[wire.rs:L211-L225](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/wire.rs#L211-L225)——空串或非字符串直接 `bail!`，该请求以带原因的 Error 终态被拒。stepped 桥把它写进提交给调度器的 `Request`：[stepped.rs:L375](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge/stepped.rs#L375) 提取、[stepped.rs:L437-L453](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge/stepped.rs#L437-L453) 落到 `Request::lora_adapter` 字段（字段定义见 [step.rs:L70](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/step.rs#L70)，一个普通 `Option<String>` 负载字段，不携带任何引擎内部状态）。

**行为锁定测试（选读）**：改写的三种放行与命中：[lora.rs:L477-L520](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/lora.rs#L477-L520)；端到端转发（内层假路由还断言了 Content-Length 与 body 长度一致）：[lora.rs:L522-L567](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/lora.rs#L522-L567)；models 列表排序去重：[lora.rs:L569-L590](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/lora.rs#L569-L590)。

#### 4.3.4 代码实践

1. **实践目标**：跟踪「一个点名适配器的请求」的完整改写链，把 4.3.2 的决策树逐分支对上代码与测试。

2. **操作步骤**：

   a. 运行改写相关测试：

   ```bash
   cargo test --release -p pegainfer-frontend --lib rewrite_lora
   cargo test --release -p pegainfer-frontend --lib lora_openai_forwarder
   ```

   b. 用 `git grep -n "pegainfer_lora_adapter" pegainfer-frontend/src` 找出这个键的全部出现点，按顺序编号：常量定义 → 代理路由注入 → 桥侧提取 → `Request` 字段。

   c. 构造三个假想 body 过一遍决策树（纸面推演即可）：`{"prompt":"hi"}`（无 model）、`{"model":"base","prompt":"hi"}`、`{"model":"adapter-a","prompt":"hi"}` 且集合含 `adapter-a`，写出各自的输出 body；再给第三个 body 预置 `"vllm_xargs":{"history_tokens":10}`，确认改写后该键仍在。

3. **需要观察的现象**：测试通过；grep 显示该键恰好出现在 wire.rs（定义+提取+测试）与 lora.rs（注入）两处文件；预置 xargs 的改写结果同时含 `history_tokens` 与 `pegainfer_lora_adapter` 两个键。

4. **预期结果**：纸面推演与测试断言一致——命中分支的输出 body 为 `model="base"`、`vllm_xargs.pegainfer_lora_adapter="adapter-a"`；预置键被保留（这正是 `or_insert_with` 的意义）。grep 结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么代理路由对「未知 model 名」原样放行，而不是自己报 404？

**答案**：改写层的职责边界是「把已装载适配器名翻译回基座名」。未知名字可能是拼错的适配器，也可能是任何 vLLM 上游认识的别名——上游对未知 model 自有错误处理（model not found），由它回答语义最准确；改写层自作主张报 404 反而可能拦下上游本可服务的请求。同理，没有 `model` 字段的请求（对 `/v1/completions` 而言 model 本就可选）直接放行。

**练习 2**：`adapter_names` 为什么用 `RwLock<HashSet>` 而不是普通 `Mutex<Vec>`？

**答案**：读写两种负载不同：管理路由低频写，代理路由**每个生成请求**都要查一次成员资格。`RwLock` 让高频读互不阻塞（且读侧用 `.read().await`，在 tokio 的异步锁上不占 worker 线程）；`HashSet` 把成员判断做成 O(1)。`Vec` + `Mutex` 会让所有请求在改写时排队。另外 `Arc` 共享让管理路由、代理路由、models 路由看到同一个集合——它就是「当前装载了什么」的唯一真相。

**练习 3**：启动装载失败与运行期 `/v1/load_lora_adapter` 失败，待遇为何不同？

**答案**：启动装载失败以 `?` 中止整个启动（错误带模块名与路径上下文）——`--lora-modules` 是运维显式声明的期望状态，缺一块就不该带病上线；运行期装卸是客户端发起的动态操作，失败映射为可重试的 400/503 响应，服务继续运行。这与 u4-l1 「引擎计数对账宁可死不带病服务」是同一条纪律：声明的东西必须齐。

### 4.4 装配与触发链：从 `--enable-lora` 到 serve_model_with_lora_routes

#### 4.4.1 概念说明

u2-l5 讲过 `ServePlan` 的 `lora_modules: Option<Vec<LoraModule>>` 与 `prefill_only: bool` 两个字段——本讲的两个特殊服务面正是被它们点亮的。`main.rs` 依 plan 三分支，而 **LoRA 分支是唯一串行的**：它 `await` 引擎加载完成才调 serve，且只接受 `LaunchedEngine::Stepped`。两个「为什么」：

1. **为何串行**：LoRA 路由的构造闭包需要**捕获引擎的控制平面**（`LoraClient`），而客户端在 `Engine` 交接包里——引擎没加载完，闭包的原料就不存在。对比默认分支：它传给 serve 的是一个引擎 `Future`，HTTP 前端装配（秒级）与权重加载（分钟级）并发（u4-l1）。
2. **为何只认 Stepped**：`lora: Option<LoraClient>` 字段长在 `Engine`（step 契约的交接包）上，legacy `EngineHandle` 没有这个字段。LoRA 服务面从一开始就是 step 契约的能力，不为 legacy 契约补造。

#### 4.4.2 核心流程

`--enable-lora` 的完整触发链（十四步）：

```
 1. clap 解析 --enable-lora（Qwen3Cli 独占 flag，default false）
 2. --lora-modules name=path 经 parse_lora_modules_arg 解析为 LoraModule
 3. line.validate：--lora-modules/--max-loras/--max-lora-rank 要求 --enable-lora；
    与 --dump-graph-png/--batch-invariant/--dflash/--eagle3 互斥
 4. line.serve_plan：lora_modules = enable_lora.then(|| lora_modules.clone())
    （--enable-lora 而不给模块 → Some(vec![])：路由照挂、启动零装载）
 5. main：if let Some(lora_modules) = plan.lora_modules → 进 LoRA 分支
 6. 串行 await 引擎加载（spawn_blocking 线程 join 回来）
 7. 模式匹配 LaunchedEngine::Stepped(engine)，Handle → bail!
 8. serve_model_with_lora_routes(engine, ..., lora_modules, port, ...)
 9. 引擎侧（launch 期间）已按 enable_lora 铸造 LoraClient::channel()，
    rx 随调度器线程走，client 装进 Engine.lora
10. serve 内：engine.lora.clone() → None 则 anyhow!("engine does not expose
    LoRA adapter control")（显式报错——引擎与 plan 说法不一致是装配错误）
11. load_startup_lora_modules：逐个装载 --lora-modules 列表，失败中止启动
12. 装配骨架：engine_count 硬编码 1（LoRA 模式单分区），engine Future 用
    std::future::ready(Ok(...)) —— 引擎本来就已加载完，串行分支的必然结果
13. extend_router 闭包：openai_router.merge(lora_router).fallback_service(router)
14. 骨架再包 mark_history → normalize_chat_requests；TCP 端口照常在桥注册后 bind
```

prefill-only 分支（对照）：plan.prefill_only → `serve_prefill_only_with_engine_count(engine_future, ...)`，引擎仍是 Future（并发加载），只是 `extend_router` 换成 `prefill_only_routes`。

#### 4.4.3 源码精读

**CLI flag 与解析**。`--enable-lora` 是 Qwen3 独占 flag：[qwen3/model_line.rs:L38-L53](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-qwen3/src/model_line.rs#L38-L53)，同一组还有 `--lora-modules`（值解析器 `parse_lora_modules_arg` 接受 vLLM 风格 `name=path`、JSON 对象或单元素 JSON 列表，实现见 [lora.rs:L71-L96](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/lora.rs#L71-L96)）、`--max-loras`、`--max-lora-rank`。`LoraModule` 本体只是 `{name, path}`：[lora.rs:L65-L69](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/lora.rs#L65-L69)。

**validate 规则**（全部发生在引擎加载之前，u2-l4 的校验矩阵）：`--lora-modules` 单独给、`--max-loras`/`--max-lora-rank` 单独给都要求 `--enable-lora`：[qwen3/model_line.rs:L188-L202](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-qwen3/src/model_line.rs#L188-L202)；`--dump-graph-png` 与 LoRA 互斥（LoRA 禁用 CUDA Graph，见 u1-l3/CLAUDE.md 的 CUDA Graph 背景即知为何互斥——捕获的图不能带动态适配器集合）；`--batch-invariant`、`--dflash-draft-model-path`、`--eagle3-draft-model-path` 与 `--enable-lora` 的互斥分布在同文件 [L208-L213](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-qwen3/src/model_line.rs#L208-L213)、[L243-L248](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-qwen3/src/model_line.rs#L243-L248)。

**serve_plan 折算**：[qwen3/model_line.rs:L288-L294](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-qwen3/src/model_line.rs#L288-L294)——一行 `enable_lora.then(|| cli.lora_modules.clone())`，其余字段 `..ServePlan::default()`（u2-l5 讲过的防漂移约定）。注意 `--enable-lora` 不带任何模块时得到 `Some(vec![])`，**LoRA 路由照挂、启动零装载**，后续用 `/v1/load_lora_adapter` 动态装载——「路由存在」与「启动时装了什么」是两件事。

**main.rs 的 LoRA 分支**：[main.rs:L215-L237](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L215-L237)。三步看清楚：`engine_load.await`（串行，源码注释写明「LoRA routes need the engine's control plane when the router is built, so this path stays sequential」）；`let LaunchedEngine::Stepped(engine) = launched else { bail!("LoRA serving requires a step-driven engine") }`（[L221-L223](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L221-L223)，legacy Handle 在此被挡下）；调 `serve_model_with_lora_routes`，shutdown token 用 `shutdown_token_from_ctrl_c()`（引擎已就绪，从一开始就是优雅关停——对照 u1-l3 的 Ctrl-C 两段式，串行分支不存在「加载期 kill」阶段）。对照 prefill-only 分支：[main.rs:L254-L265](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L254-L265)——引擎仍以 Future 传入，并发加载照旧，只是 serve 函数不同。

**serve_model_with_lora_routes 装配**：[vllm/mod.rs:L184-L229](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L184-L229)。逐段：能力检查 [L196-L201](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L196-L201)（`engine.lora` 为 `None` 时显式报 `engine does not expose LoRA adapter control`——这是 4.2 讲的「Option 即能力」在装配侧的兑现：**能到这一步却拿不到客户端，说明引擎与 plan 的说法不一致，是装配错误而非运行错误**）；启动装载 [L202](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L202)；基座名取 served 名第一个否则退回 model_id [L203-L206](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L203-L206)（它就是代理改写的 `base_model_name`）；骨架调用 [L207-L216](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L207-L216) 里两个值得注意的参数——engine Future 是 `std::future::ready(Ok(LaunchedEngine::Stepped(engine)))`（引擎已在手，Future 立即就绪，串行分支的自然结果），`engine_count` 硬编码 `1`（LoRA 模式单分区服务）；路由闭包 [L217-L226](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L217-L226)：LoRA OpenAI 代理路由（捕获内层 router 的克隆）与 lora 管理路由 merge 后，整体再 `fallback_service(router)`——**最终路由的优先级**：LoRA 代理路由吃掉 `/v1/models` 与两条生成路径，管理路由吃掉两条装卸路径，其余一切（健康检查、tokenize 等）落到原封不动的 vLLM 路由。基座名从闭包外的 `served_model_name` 移进闭包（`served_model_name.clone()` 传入骨架、原 vec 被 move 进闭包），这就是「路由构建期就需要引擎控制平面」的具体形状——闭包捕获了 `control` 与适配器集合。

**骨架内的最终包裹**：闭包产出的 router 在骨架里再被 `mark_history`、`normalize_chat_requests` 包住：[vllm/mod.rs:L493-L500](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L493-L500)。于是 LoRA 请求的完整洋葱序：effort 改写（最外）→ 边界测量 → **LoRA 代理改写** → vLLM 路由。这个顺序解释了 4.3.3 的一个细节：LoRA 改写看到 body 时 `vllm_xargs` 可能已被 `mark_history` 填过，所以必须 `or_insert_with` 追加而非覆盖；而 LoRA 改写替换 `model` 不影响边界测量（测量只看 messages，不看 model）。

#### 4.4.4 代码实践

1. **实践目标**：亲手写出 `--enable-lora` 从 CLI 到 `serve_model_with_lora_routes` 的完整触发链（本讲实践任务的下半部分）。

2. **操作步骤**：

   a. 通读 [main.rs:L215-L237](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L215-L237)（LoRA 分支）与 [main.rs:L254-L278](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L254-L278)（prefill 与默认分支），标出三者各以什么形态持有引擎（值 / Future）。

   b. 对照 4.4.2 的十四步，给每步找到源码行号并抄录关键语句（答案见 4.4.5 练习 1 的参考链）。

   c. 用只含 `config.json`（真实 Qwen3 配置）与 `tokenizer.json` 的模型目录做干跑（无权重，预期在权重加载处失败，但校验链已完整走过）：

   ```bash
   cargo run --release -- --model-path <dir> \
       --lora-modules my-adapter=/tmp/no-such-adapter
   ```

   预期在引擎加载之前得到 `--lora-modules requires --enable-lora`（validate 矩阵第 3 步拦截）。补上 `--enable-lora` 再跑，观察错误**推进到引擎/权重加载阶段**——这印证校验先于 launch（u2-l4 的三层分层失败）。再叠加 `--batch-invariant` 验证互斥规则。

3. **需要观察的现象**：第一次干跑被 CLI 规则拒绝且不触 GPU；加 `--enable-lora` 后错误位置推进到加载阶段；叠加 `--batch-invariant` 得到「不支持与 --enable-lora 同用」的规则错误。

4. **预期结果**：三条错误各来自链条的第 3 步（line.validate）、第 6/9 步（加载）、第 3 步（互斥规则），与源码错误文本逐字对应。实际输出待本地验证（错误消息原文以源码为准）。

#### 4.4.5 小练习与答案

**练习 1**：默写 `--enable-lora` 的触发链（压缩为一步一句），并标注每步的源码落点。

**答案**（参考链）：
1. clap 收 `--enable-lora`（qwen3/model_line.rs L38-L40）；
2. `--lora-modules` 经 `parse_lora_modules_arg` 成 `LoraModule`（lora.rs L71-L96）；
3. `validate` 查从属与互斥规则（qwen3/model_line.rs L188-L202 等）；
4. `serve_plan` 折出 `lora_modules=Some(...)`（L288-L294）；
5. main 按 `plan.lora_modules` 进 LoRA 分支（main.rs L215）；
6. 串行 await 引擎（L218-L220）；
7. 要求 `Stepped`，Handle 即 bail（L221-L223）；
8. 调 `serve_model_with_lora_routes`（L227-L236）；
9. 引擎侧 launch 时已铸造 `LoraClient::channel()` 并装进 `Engine.lora`（frontend_adapter.rs L200-L207、wiring.rs L150-L152）；
10. serve 内取 `engine.lora`，`None` 显式报错（mod.rs L196-L201）；
11. `load_startup_lora_modules` 装载启动列表（L202 / lora.rs L268-L291）；
12. 骨架以 `ready(Ok(...))` 为引擎 Future、`engine_count=1`（L207-L216）；
13. 路由闭包 `openai_router.merge(lora_router).fallback_service(router)`（L217-L226）；
14. 骨架再包 `mark_history`、`normalize_chat_requests`（L493-L500）。

**练习 2**：`--enable-lora` 而不给 `--lora-modules`，服务面是什么样？这条路径的意义是什么？

**答案**：`serve_plan` 折出 `Some(vec![])`，main 仍进 LoRA 分支，`load_startup_lora_modules` 对空列表是空操作——LoRA 管理路由与代理路由照常挂载，`/v1/models` 只列基座名。之后运维经 `POST /v1/load_lora_adapter` 动态装载，装载成功的名字进入 `adapter_names`，随即可被 `model` 字段点名。意义：适配器集合是运行时状态，不必在启动时定死。

**练习 3**：若某条模型线的引擎忘了铸造 LoRA 通道，但用户给了 `--enable-lora`，故障在哪个环节以什么形态暴露？为什么这样设计是对的？

**答案**：在 `serve_model_with_lora_routes` 的能力检查处（mod.rs L196-L201）以 `engine does not expose LoRA adapter control` 中止启动——引擎已加载完（分钟级成本已付），但服务拒绝带病上线。这是「Option 即能力」设计里唯一需要显式错误的缝：plan（来自 CLI）说要有 LoRA，引擎交接包说没有，两份声明冲突属于**装配错误**，必须在服务可达之前炸掉，而不是等第一个 `/v1/load_lora_adapter` 请求撞上。与之对照，「引擎支持但没有该路由」的状态在类型上就不存在（练习见 4.2.5-1）。

## 5. 综合实践

**任务：给「prefill-only + LoRA」各画一张请求命运图，并验证 prefill 门的两种结局。**

1. **准备**：完成 4.1.4 的草稿 crate（prefill 门 + oneshot 测试），确认 `max_tokens=2` → 400（错误 JSON 含 `invalid_request_error`）、`max_tokens=1` → 内层 fallback 200。
2. **prefill 命运图**：画一个请求从 TCP 进来到引擎的完整路径，标出它在哪一层被检查（effort 改写 → 边界测量 → **prefill 门** → vLLM 路由 → 桥 → 调度器），并在门处分出 400 与透传两条岔路，各配一个 body 示例与对应响应。
3. **LoRA 命运图**：画 `POST /v1/completions {"model":"adapter-a", ...}` 的路径，标出代理改写发生的位置（洋葱第几层）、`vllm_xargs.pegainfer_lora_adapter` 的注入点、桥侧的提取点、`Request::lora_adapter` 的落点，以及调度器如何据它选适配器；同图并列 `POST /v1/load_lora_adapter` 的另一条路（HTTP → LoraClient → crossbeam → step 顶部排空 → 批空闲应用 → oneshot 应答）。
4. **交叉验证**：两张图都应以 `vllm/mod.rs:L493-L500` 的包裹语句为「洋葱总纲」，检查你的箭头顺序与它一致；若不一致，回到源码修正。
5. **留档**：把两张图与门测试的关键断言整理进你的学习笔记，作为 u5（扩展与二次开发）的素材——u5-l2 接入新模型线时你会再次需要「服务形态由 ServePlan + extend_router 决定」这张地图。

## 6. 本讲小结

- **prefill-only 门**是 axum 层的协议契约检查：缓冲 body → 判 `max_tokens`/`max_completion_tokens` 恰为 1（completions 只认前者，chat 两者同给须相等）→ 重写 Content-Length 后 `oneshot` 透传；不合格在触达引擎之前得到 400 + `invalid_request_error` 信封。非生成路径经 `fallback_service` 原样放行。
- **LoRA 能力的类型化**：`Engine::lora: Option<LoraClient>` 上的 `Option` 就是能力本身——没有通道的引擎不挂 LoRA 路由，无人能问，因此错误枚举里没有 `Unsupported`；唯一显式的错误在装配侧（引擎与 `--enable-lora` 说法不一致时拒绝启动）。
- **控制通道**：引擎 launch 时 `LoraClient::channel()` 铸双端，接收端随调度器走、在 `step` 顶部排空、批空闲时应用；命令走 crossbeam、应答走 tokio oneshot，两端消失都折叠为 `EngineGone`。
- **LoRA 路由两组**：管理路由（load/unload，直通控制通道，成功 200 文本、EngineGone 503、Failed 400）与 OpenAI 代理路由（`model` = 适配器名 → 改回基座名 + 注入 `vllm_xargs.pegainfer_lora_adapter`，经桥提取为 `Request::lora_adapter`；`/v1/models` 合并基座与适配器名排序去重列出）。
- **触发链与串行原因**：`--enable-lora` → validate 从属/互斥规则 → `serve_plan.lora_modules=Some(...)` → main LoRA 分支；串行是因为路由闭包要捕获引擎控制平面、且 `lora` 字段只在 step 契约的 `Engine` 上，故必须 await 加载并要求 `LaunchedEngine::Stepped`，`engine_count` 随之硬编码 1。
- **洋葱位置**：两个特殊路由都是 `extend_router` 的形态（最里层），外层依次是 `mark_history`、`normalize_chat_requests`——LoRA 改写必须 `or_insert_with` 追加 xargs 以保留边界测量结果，顺序即依赖。

## 7. 下一步学习建议

- **下一讲 u4-l6（可观测性）**：看 `SchedulerMetrics` 如何从调度器每迭代发布、经共享单元或 watch 通道抵达桥与 `/metrics`——其中 prefix-cache 的累计计数器（u1-l4 的 detect 门、本讲的 prefill 门都不涉，但 CLAUDE.md 提到的 `/metrics` 前缀缓存计数就在那条链上）。
- **回读源码**：若想彻底吃透「Option 即能力」，回读 [wiring.rs:L141-L153](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L141-L153)（交接包字段都是显式语义的清单）与 [control.rs:L1-L11](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/control.rs#L1-L11) 的模块文档。
- **为 u5 做准备**：u5-l1 将讲 KV 前缀解析与请求生命周期边缘（drop 炸弹、abort、DeferredFinish），u5-l2 会把本讲的触发链扩展成「从零接入一条新模型线」的完整清单——届时 `serve_plan` 与 `extend_router` 将再次成为核心词汇，建议先做完本讲综合实践的两张命运图。
