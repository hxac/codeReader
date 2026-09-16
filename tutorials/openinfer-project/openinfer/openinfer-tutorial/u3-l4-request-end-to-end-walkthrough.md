# 一个请求的完整旅程（端到端串联）

## 1. 本讲目标

本讲是「前端与引擎契约」单元的收官：把 u3-l1（旧契约）、u3-l2（新 step 契约）、u3-l3（vLLM 协议栈与 ZeroMQ 桥）三讲的内容串成**一条端到端的请求链路**。学完后你应该能够：

1. 独立追踪一次流式 chat 请求（`POST /v1/chat/completions`）从 HTTP 进入、经模板渲染与分词、穿过 ZeroMQ 桥、到达模型 `Scheduler::step`、再以 `StepOutputs`/`TokenEvent` 回流成 SSE 流的**全部函数跳转**，每一跳都能说出文件与函数名。
2. 用 `frontend-architecture.md` 解释契约分两代的**演进原因**：为什么从 per-request 事件流走向 step 批量消息。
3. 拿到任意一个请求阶段的异常现象（端口不通、请求 hang、参数被拒、流被取消、finish_reason=error……），能直接定位到该看哪个文件、哪段代码。

本讲通篇以 **step 新契约**（`LaunchedEngine::Stepped`）为主线走完整链路，在回程一节对照 legacy 桥的差异——这也是当前仓库的真实状态：Qwen3、Gemma 4 与 `pegainfer-sim` 走新桥，其余模型线仍走旧桥。

## 2. 前置知识

阅读本讲前，请先确认理解以下概念（前几讲已建立，这里只做一句话复习）：

- **prefill / decode 两阶段**：LLM 推理先一次性吃进 prompt（prefill），再逐个产出 token（decode）。本讲的"一步调度"（step）在真实模型里就是把一批 prefill/decode 工作打包执行一次。
- **step 契约**（u3-l2）：前端向调度器提交 `Request`，每个调度步收回**一条** `StepOutputs`，其中每个被触碰请求恰好一条扁平 `RequestUpdate`；`RequestLedger` 账本保证每个请求恰好一个终态。
- **旧 handle 契约**（u3-l1）：`EngineHandle::submit` 进、共享频道上的 `TokenEvent` 流出，前端按 tag demux（分路）。
- **vLLM 冒名顶替**（u3-l3）：PegaInfer 不自建 HTTP 层，而是复用外部 `vllm-server` crate（git 依赖，本仓库外），并在同进程内用 ZeroMQ IPC + MessagePack「假装」自己是一个 vLLM EngineCore 进程。
- **SSE（Server-Sent Events）**：HTTP 长连接上的服务端推送格式，每个事件是一行 `data: {JSON}\n\n`，OpenAI 流式接口以唯一一行 `data: [DONE]` 终止。
- **MessagePack（msgpack）**：二进制 JSON 替代品，vLLM EngineCore 协议用它编码 ZMQ 帧的载荷。
- **单调钟与墙钟**：Rust 的 `Instant` 是单调递增、适合测间隔但不是 Unix 时间；vLLM wire 协议要 Unix 浮点秒。两者的换算是桥的职责之一（本讲 4.4 节的 `UnixAnchor`）。
- **ZMQ socket 类型**：DEALER（异步请求端，对应服务端 ROUTER）与 PUSH（单向推送，对应接收端 PULL）。桥用 DEALER 收请求、PUSH 发输出。

如果对以上任何一条感到陌生，请先回到 u3-l1/u3-l2/u3-l3 复习，本讲不再重复展开它们的定义。

## 3. 本讲源码地图

本讲涉及的关键文件（按请求流经顺序排列）：

| 文件 | 在链路中的角色 |
| --- | --- |
| [docs/subsystems/frontend/frontend-architecture.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docs/subsystems/frontend/frontend-architecture.md) | 前端架构总纲：契约两代并存的现状、设计决策与迁移方向 |
| `pegainfer-frontend/src/vllm/mod.rs` | 服务总装入口：起 HTTP、并行等引擎、按 `LaunchedEngine` 分臂造桥 |
| `pegainfer-frontend/src/vllm/bridge.rs` | 桥的共享底座：ZMQ 连接与握手（`connect_link`）、输出泵；兼载 legacy 桥 |
| `pegainfer-frontend/src/vllm/bridge/stepped.rs` | 本讲主角：step 契约桥，`StepOutputs` → EngineCore 输出的 1:1 翻译 |
| `pegainfer-frontend/src/vllm/wire.rs` | 采样参数/终止原因的词汇表转换（u3-l3 已精读） |
| `pegainfer-frontend/src/engine/wiring.rs` | `SchedulerHandle::submit` 与 `scheduler_pair`：请求的入口端接线 |
| `pegainfer-frontend/src/engine/driver.rs` | `Scheduler` trait 与 `drive` 轮询循环：调度器线程的心跳 |
| `pegainfer-frontend/src/engine/ledger.rs` | `RequestLedger`：开户、记账、每步一条 `StepOutputs` 提交 |
| `pegainfer-frontend/src/engine/step.rs` | 契约 wire 类型：`Request` / `StepOutputs` / `RequestUpdate` / `Terminal` |
| `pegainfer-frontend/src/engine/request_lifecycle.rs` | `RequestEnvelope` 信封、`RequestControl::abort` 旗标、drop bomb |
| `pegainfer-sim/src/lib.rs`、`pegainfer-sim/src/main.rs` | CPU-only 参考 `SimScheduler`：本讲实践的可运行载体 |
| `pegainfer-sim/tests/frontend_e2e.rs` | 18 个模拟 HTTP E2E 测试：旅程验证的现成证据 |

## 4. 核心概念与源码讲解

### 4.1 全链路鸟瞰：一次流式 chat 请求的十站旅程

#### 4.1.1 概念说明

前三讲分别解剖了链路的**各段**，本讲把它们拼起来。理解这条链路的关键心理模型是：

> 前端（vllm-server + 桥）与调度器是**两个世界**，中间靠契约通道相连。北世界是 async 的 tokio 任务（HTTP/SSE/ZMQ），南世界是一个专用 OS 线程上的同步轮询循环（`drive`）。两个世界之间只有两条通道：一条 crossbeam 提交通道（北→南），一条 tokio mpsc 步流（南→北）。

架构文档用一句话定义了这条边界——分词器、chat 模板、HTTP、指标、LoRA 路由在调度器之**北**；KV、批处理、CUDA 在之**南**；契约不含任何 CUDA 类型。见 [docs/subsystems/frontend/frontend-architecture.md:L7-L9](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docs/subsystems/frontend/frontend-architecture.md#L7-L9)，这段就是全链路的「宪法」。

#### 4.1.2 核心流程

以 `pegainfer-sim`（或任何 stepped 引擎，如 Qwen3）上的一次 `POST /v1/chat/completions`（`stream: true`）为例，完整旅程共十站：

```
客户端                     北世界（async）                        南世界（同步线程）
──────                    ──────────────                        ────────────────
  │
  ├─① POST /v1/chat/completions ──► vllm-server HTTP 路由（外部 crate）
  │                                    ② chat 模板渲染 + 分词 → EngineCoreRequest(msgpack)
  │                                    ② ZMQ DEALER ──► input.sock
  │                                       │
  │                                    ③ SteppedEngineBridge::run 的 select 收帧
  │                                       handle_message 解帧（Add/Abort/Utility）
  │                                    ④ start_request：wire 校验 + convert_sampling
  │                                       SchedulerHandle::submit(Request) ──► crossbeam 通道
  │                                                                          ⑤ drive() 排空提交
  │                                                                             ledger.register 开户
  │                                                                             Scheduler::submit 拿走所有权
  │                                                                          ⑥ Scheduler::step(&mut ledger)
  │                                                                             准入/GPU 工作/写账本
  │                                                                          ⑦ ledger.commit_step()
  │                                                                             ──► StepOutputs 一条消息 ──► tokio mpsc
  │                                       ⑧ select steps.recv() → dispatch_step
  │                                          reduce_update：每请求至多一条 EngineCoreOutput
  │                                          send_outputs → output_loop(PushSocket) → output.sock
  │ ◄──────────────────────────────────── ⑨ vllm-server 收 EngineCoreOutputs
  ├─◄ SSE: data: {chunk}                      增量 detokenize，逐块推送
  ├─◄ SSE: data: {chunk}
  └─◄ ⑩ terminal（finish_reason + data: [DONE]），streams/names 摘除该请求
```

每站对应的文件与函数：

| 站 | 文件 | 函数/位置 |
| --- | --- | --- |
| ① | 外部 `vllm-server` crate（本仓库外） | HTTP 路由 |
| ② | 外部 crate + `vllm/mod.rs` 的 `Config` | 模板渲染、分词、msgpack 编码 |
| ③ | `vllm/bridge/stepped.rs` | `run` → `handle_message` |
| ④ | `vllm/bridge/stepped.rs` → `vllm/wire.rs` | `start_request` → `convert_sampling` |
| ⑤ | `engine/driver.rs` → `engine/ledger.rs` | `drive` → `RequestLedger::register` |
| ⑥ | 模型 crate（如 `pegainfer-sim/src/lib.rs`） | `Scheduler::step` |
| ⑦ | `engine/ledger.rs` | `commit_step` |
| ⑧ | `vllm/bridge/stepped.rs` | `dispatch_step` → `reduce_update` |
| ⑨ | 外部 crate | 增量 detokenize + SSE |
| ⑩ | `vllm/bridge/stepped.rs` | `reduce_update` 的 terminal 分支 |

#### 4.1.3 源码精读

整条链路的分层与迁移现状，架构文档 TL;DR 一段讲得最完整：step 契约（`StepOutputs` wire + `RequestLedger` 生命周期 + 契约自有的轮询 driver）与 legacy handle 契约（`EngineHandle` + per-request `TokenEvent`）并存，Qwen3、Gemma 4、`pegainfer-sim` 已迁移，其余四条线仍走旧契约——见 [docs/subsystems/frontend/frontend-architecture.md:L1-L5](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docs/subsystems/frontend/frontend-architecture.md#L1-L5)。

为什么会有两代？答案藏在设计决策清单里。最重要的一条是**step 批量 wire**：调度器的自然输出单位是「步批次」，而不是「每请求一条频道」——per-request 频道方案试过并被否决，因为调度器对 N 条频道的 for 循环本身就是瓶颈，见 [docs/subsystems/frontend/frontend-architecture.md:L34-L35](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docs/subsystems/frontend/frontend-architecture.md#L34-L35)。u3-l1 学过的旧契约恰恰依赖共享频道 + 前端 demux，这正是新契约要消灭的约定式排序。

第二站里「HTTP 路由/模板/分词在本仓库外」的事实，由 vllm 协议栈段落记载：HTTP 路由、OpenAI 类型、分词器、chat 模板、Prometheus 全部住在外部 `vllm-server`/`vllm-metrics`/`vllm-text` crate，桥只负责把每条 `RequestUpdate` 1:1 翻译成 EngineCore 输出——见 [docs/subsystems/frontend/frontend-architecture.md:L94-L96](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docs/subsystems/frontend/frontend-architecture.md#L94-L96)。

#### 4.1.4 代码实践

**实践目标**：把十站旅程内化为一张可以随时查阅的「函数级路牌表」。

**操作步骤**：

1. 不看本讲正文，只在纸上凭记忆写出十站的编号与一句话描述。
2. 打开 `pegainfer-frontend/src/engine/` 与 `pegainfer-frontend/src/vllm/` 两个目录，对照 `engine/step.rs` 的类型定义（[step.rs:L49](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/step.rs#L49) 的 `Request`、[step.rs:L82](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/step.rs#L82) 的 `StepOutputs`、[step.rs:L90](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/step.rs#L90) 的 `RequestUpdate`、[step.rs:L233](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/step.rs#L233) 的 `Terminal`），给每一站标注「携带的数据类型」。
3. 补全一张三列表：**站号 × 文件::函数 × 数据类型**。

**需要观察的现象**：第④站与第⑦站的数据类型发生了「词表切换」——④ 把 wire 类型（`EngineCoreRequest`/`EngineCoreSamplingParams`）翻成契约类型（`Request`/`SamplingParams`），⑦ 把账本内部状态翻成契约输出（`StepOutputs`）。任何类型只要名字带 `EngineCore`，就只允许出现在桥里。

**预期结果**：得到一张完整的十站路牌表；其中①②⑨三站在本仓库内**找不到函数体**（它们在外部 git 依赖里），这本身就是要记住的结论。

本实践为纯阅读型，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：一个请求的 `prompt_tokens`（分词结果）是在哪一站产生的？PegaInfer 本仓库里能找到这段代码吗？

<details><summary>参考答案</summary>

在第②站，由外部 `vllm-server` crate 做 chat 模板渲染与分词。本仓库找不到——`pegainfer-frontend` 只在 `vllm/mod.rs` 构造 `Config` 时把 tokenizer/模板配置（`GenerationConfigMode`、`RendererSelection` 等）交给外部 crate。桥收到的 `EngineCoreRequest.prompt_token_ids` 已经是 token id 数组。
</details>

**练习 2**：为什么说「调度器的自然输出单位是步批次」？如果改成每请求一条频道，最先出问题的是什么？

<details><summary>参考答案</summary>

因为调度器每一步天然同时推进一批请求（一个 batch 一次 GPU 工作）；按步聚合成一条消息，一次 channel send 就把整批结果都送出去了。改成 per-request 频道后，调度器每步要对 N 条频道做 N 次 send/recv 轮询，这个 for 循环本身成为瓶颈——文档明确说该方案「试过并被否决」（frontend-architecture.md 第 34-35 行）。此外请求内事件的顺序也会从「结构保证」退化成「约定保证」。
</details>

**练习 3**：十站中哪几站运行在 tokio async 上下文，哪几站运行在专用 OS 线程？

<details><summary>参考答案</summary>

③④⑧（桥的 select 循环、`handle_message`/`start_request`、`dispatch_step`）运行在 tokio async 任务上；⑤⑥⑦（`drive` 的排空/step/commit）运行在 `spawn_scheduler` 创建的专用 OS 线程上。①②⑨在外部 crate 的 async HTTP 服务里。北 async、南同步，中间靠 crossbeam（提交）与 tokio mpsc（步流）两条通道衔接。
</details>

### 4.2 北段：并行起服与 ZeroMQ 握手

#### 4.2.1 概念说明

旅程开始**之前**还有一段「基建期」：服务是怎么站起来的？PegaInfer 做了一件容易被忽略但影响观察方式的事——**HTTP 起服与引擎加载并行**。权重加载（多 GPU MoE 模型要几分钟）不阻塞端口绑定；但反过来，端口可达仍然意味着引擎就绪。这看似矛盾，实际由「HTTP 只在桥注册之后才绑定」保证。

第二个概念是 **EngineCore 握手**：外部 vllm-server 假设引擎是一个独立进程，启动时要先收到一条 `EngineCoreReadyResponse`（报告 max_model_len、KV 块数、并行度等）才认为引擎活着。桥的 `connect_link` 负责完成这次冒名顶替的「自我介绍」。

#### 4.2.2 核心流程

起服时序（`vllm::serve` 一族入口的共同路径）：

```
vllm::serve(engine_future, …)
  └─ serve_model_on_host_with_router_extension
       ├─ 造 IPC 命名空间：input.sock / output.sock
       ├─ tokio::spawn(engine_task)          ┄┄ 并行
       │    ├─ engine.await → LaunchedEngine
       │    ├─ match { Handle ⇒ N 个 LocalEngineBridge
       │    │         Stepped ⇒ N 个 SteppedEngineBridge }
       │    └─ 每桥 connect_link：
       │         等 IPC 端点 → DEALER connect(input)
       │         → 发 EngineCoreReadyResponse(msgpack)
       │         → PUSH connect(output) → 起 output_loop 子任务
       │    └─ JoinSet 收割桥任务
       ├─ 构造 Config { transport_mode: Bootstrapped{input,output,…} }
       └─ vllm_server::serve_with_router_extension(config, …)   ← HTTP 才开始绑端口
```

桥运行期的心脏是一个四臂 `tokio::select!`：shutdown 令牌、子任务退出、步流到达（南→北）、请求帧到达（北→南）。

#### 4.2.3 源码精读

**入口分身**。`vllm::serve` 是最常用的包装：它拿一个「未来才会 resolve 的引擎」加模型路径起服——见 [pegainfer-frontend/src/vllm/mod.rs:L64-L82](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L64-L82)。文档注释把并行策略说得很直白：HTTP 前端（分词器、chat 模板）立即启动，桥等 `engine` resolve 后挂上；HTTP 只在桥注册后才绑定，所以「端口可达」依然等于「引擎就绪」。

**engine_task 的双臂分发**。总装函数 `serve_model_on_host_with_router_extension` 先 spawn 一个 engine_task（[mod.rs:L233-L249](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L233-L249)），引擎 resolve 后按 `LaunchedEngine` 的两个变体造桥——这正是 u3-l2 结尾「双臂」的消费现场：`Handle` 臂造 `LocalEngineBridge`（[mod.rs:L251-L276](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L251-L276)），`Stepped` 臂收集调度器线程句柄并造 `SteppedEngineBridge`（[mod.rs:L277-L303](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L277-L303)）。两臂都会校验「前端声明的引擎数 == 实际调度器数」，不符即取消整个服务。

**握手现场**。`BridgeLink` 是桥的连接态（DEALER + 输出通道 + 子任务集），legacy 桥与 stepped 桥共用——见 [pegainfer-frontend/src/vllm/bridge.rs:L692-L696](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L692-L696)。`connect_link` 先等两个 IPC 端点就绪（[bridge.rs:L699-L710](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L699-L710)），再以 DEALER 连上 input（[bridge.rs:L718-L722](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L718-L722)），然后把 `EngineCoreReadyResponse`（max_model_len、num_gpu_blocks、dp/tp 规模、KV 容量折算等，[bridge.rs:L738-L764](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L738-L764)）编码成 msgpack 发出去（[bridge.rs:L770-L773](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L770-L773)）。注意 `vllm_version` 字段填的是 `"pegainfer-local-bridge"`——冒名顶替的自白书。最后 PUSH 连上 output，并 spawn `output_loop` 子任务（[bridge.rs:L775-L783](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L775-L783)）。

**输出泵**。桥内所有发送都走 unbounded mpsc，由唯一的 `output_loop` 串行编码发送（[bridge.rs:L809-L819](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L809-L819)）——这样多桥、多子任务共享一条 PUSH socket 而互不踩踏。

**HTTP 侧的接线**。`Config` 的 `transport_mode` 是 `TransportMode::Bootstrapped { input_address, output_address, …, ready_timeout: 30min }`（[mod.rs:L357-L369](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L357-L369)）——ready_timeout 只兜「真正挂死的加载」，加载**失败**会由 engine_task 主动取消 server。最终 `vllm_server::serve_with_router_extension`（[mod.rs:L402-L403](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L402-L403)）把 HTTP 立起来。

#### 4.2.4 代码实践

**实践目标**：亲眼验证「端口可达 = 引擎就绪」以及握手后 `/health` 可用。

**操作步骤**：

1. 启动模拟引擎（CPU 即可，无 GPU 要求）：

   ```bash
   cargo run --release -p pegainfer-sim -- --port 8000
   ```

   注意 workspace 的 default-members 只有 `pegainfer-server`，所以必须带 `-p pegainfer-sim`。默认 `--model-id Qwen/Qwen3-0.6B` 会按 HF id 拉取分词器元数据，若无外网，改用本地任意含 `tokenizer.json` 等元数据的模型目录路径（u1-l4 讲过 sim 只需三个元数据文件）。

2. 服务打印就绪信息后，探测健康端点（e2e 测试用的同一端点，见 [frontend_e2e.rs:L965-L983](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/tests/frontend_e2e.rs#L965-L983)）：

   ```bash
   curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/health
   ```

3. （可选，源码阅读型）在 `mod.rs` 的 `Config` 构造处找到 `ready_timeout: Duration::from_mins(30)`，写下它兜的是什么、不兜什么。

**需要观察的现象**：`/health` 返回 2xx；且 sim 启动到端口可达之间没有「权重加载」阶段（本来就没有权重）。

**预期结果**：`200`。若在 GPU 机型上用真实模型重复本实践，会观察到端口在权重加载完成**后**才可达——这正是「HTTP binds only after the bridge registers」的可观察面。（sim 场景下加载极快，现象差异不明显，属于预期。）

若本机无法运行（无 Rust 环境等），标记**待本地验证**，改为精读 [mod.rs:L56-L59](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L56-L59) 的文档注释并复述并行策略。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ready_timeout` 设 30 分钟这么大？它失败和引擎加载失败分别走什么路径？

<details><summary>参考答案</summary>

多 GPU MoE 模型权重加载要几分钟，冷启动更久，30 分钟是给「还在正常加载」的余量。引擎**失败**（future 返回 Err）由 engine_task 捕获并 `server_shutdown.cancel()`，错误立刻上浮；`ready_timeout` 只兜「加载既没成功也没失败、真挂死」的情况（[mod.rs:L364-L368](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L364-L368) 的注释）。
</details>

**练习 2**：`EngineCoreReadyResponse` 里的 `num_gpu_blocks`/`block_size` 从哪来？stepped 桥传的是什么？

<details><summary>参考答案</summary>

来自契约的 `EngineInfo.kv_capacity`（`Option<KvCapacity>`）：`connect_link` 拿到 `Some(c)` 后按 `total_blocks`/`block_size` 填写，并折算出 vLLM 风格的 `kv_cache_max_concurrency`（块数 ÷ 每请求块数，[bridge.rs:L724-L737](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L724-L737)）。`None` 时填 0/16 的占位。sim 引擎 `EngineInfo` 两字段都是 `None`（[pegainfer-sim/src/lib.rs:L133-L136](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L133-L136)），所以握手报 0 块。
</details>

### 4.3 中段：stepped 桥的入口分支——从 ZMQ 帧到 `SchedulerHandle::submit`

#### 4.3.1 概念说明

第③④站是请求「由北入南」的关卡。stepped 桥的 `run` 是一个永续的 `tokio::select!` 循环，同时盯着四件事：关停令牌、子任务异常、南边来的步流、北边来的 ZMQ 帧。请求到达时走 `handle_message` → `start_request`，在这里完成三件事：

1. **词表校验**：缺失字段与不支持的采样参数在这里拒绝（根本不进调度器）；
2. **词表翻译**：`EngineCoreSamplingParams` → 契约 `SamplingParams`；
3. **身份铸造与登记**：`SchedulerHandle::submit` 铸 `RequestId` 与 abort 旗标，桥侧登记 `streams`/`names` 两张表用于回程 demux 与 abort 反查。

abort（客户端断开/取消）在 step 契约里是**旗标不是拆通道**：桥先摘掉自己的 stream 状态，再翻转 `RequestControl` 的原子布尔，调度器下次触碰时静默 `retire`。

#### 4.3.2 核心流程

```
input.recv() ──► ZmqMessage(2 帧: 类型帧 + msgpack 载荷帧)
   │
   ├─ Add 帧    ─► decode EngineCoreRequest ─► start_request
   │                 ├─ prompt_token_ids 缺失?  ─► 终态 Error 输出（不提交）
   │                 ├─ sampling_params 缺失?   ─► 终态 Error 输出（不提交）
   │                 ├─ unsupported_request_params? ─► 终态 Error 输出（不提交）
   │                 ├─ lora_adapter_from_sampling_params
   │                 ├─ （tracing 开时）开根 span
   │                 ├─ convert_sampling → SchedulerHandle::submit(Request)
   │                 └─ streams[id] / names[request_id] 登记
   ├─ Abort 帧  ─► 对每个 request_id：names 反查 → 摘 stream → control.abort()
   └─ Utility 帧 ─► send_utility_response 回执
```

`SchedulerHandle::submit` 的内部（北端视角）：

```
submit(request):
  id       = RequestId(next_id++)          # 桥不关心，前端铸
  abort    = Arc<AtomicBool>(false)
  envelope = RequestEnvelope::new(id, abort, step_tx, request, Instant::now())
  submit_tx.send(envelope)                 # crossbeam 无界通道，永不阻塞
  return RequestControl { id, abort }      # 调用方持旗标，可随时翻
```

#### 4.3.3 源码精读

**select 循环**。`SteppedEngineBridge::run` 先 `take_steps()` 拿到唯一的步流接收端，`connect_link` 完成握手，并**立即**发一条只有 stats 的空批次让前端仪表盘先归零（[stepped.rs:L78-L111](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L78-L111)）；随后建立 `UnixAnchor` 与 `streams`/`names` 两张表（[stepped.rs:L113-L115](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L113-L115)）。主循环四臂 select 见 [stepped.rs:L122-L170](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L122-L170)：`biased` 保证关停优先；步流到达转 `dispatch_step`（回程，4.4 节）；请求帧到达转 `handle_message`。

**解帧分发**。`handle_message` 校验「恰好 2 帧」，按第一帧的类型字节分派 Add/Abort/Utility（[stepped.rs:L262-L313](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L262-L313)）。Abort 分支的顺序很讲究：**先摘自己的 stream 状态，再翻 abort 旗标**（`Release` 写排在摘除之后），调度器下次触碰即 `retire`，路上已发出的 update 会因查不到 stream 而被丢弃（[stepped.rs:L285-L299](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L285-L299)）。

**入口校验**。`start_request` 解构 `EngineCoreRequest`，缺 `prompt_token_ids` 或 `sampling_params` 直接回终态 Error（[stepped.rs:L324-L353](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L324-L353)）；`wire::unsupported_request_params` 命中同样拒绝——这些请求**从未见过调度器**（[stepped.rs:L355-L366](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L355-L366)，判定本体在 [wire.rs:L121](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L121)）。u3-l3 讲过的采样参数翻译入口是 [wire.rs:L77](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L77) 的 `convert_sampling`。

**提交现场**。trace 开启时先开请求根 span（上下文随 `Request.trace_parent` 进入调度器，成为 queue/prefill/decode span 的父），然后一行完成第④站的核心动作：

- [stepped.rs:L396-L413](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L396-L413)：构造契约 `Request`（prompt_tokens、翻译后的参数、max_tokens、lora_adapter、logprobs 档位、trace_parent、client_label）并 `self.scheduler.submit(...)`。
- [stepped.rs:L415-L419](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L415-L419)：`names[wire 的字符串 id] = 契约 RequestId`、`streams[RequestId] = SteppedStream`。回程 demux 与 abort 反查全靠这两张表。

**北端 submit**。`SchedulerHandle::submit` 见 [wiring.rs:L80-L90](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L80-L90)：铸 id、造 abort 旗标、装信封、走 crossbeam 发送——发送**永不失败**；若调度器已死，`RequestEnvelope` 的 drop bomb（[request_lifecycle.rs:L84](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/request_lifecycle.rs#L84) 起）会在步流上替该请求补一条 `Failed` 终态。通道选型理由（crossbeam 同步消费 / 无界无背压 / 准入拒绝用 `Rejected` 表达）在 [wiring.rs:L1-L17](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L1-L17) 的模块注释与 [frontend-architecture.md:L40-L41](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docs/subsystems/frontend/frontend-architecture.md#L40-L41)。

#### 4.3.4 代码实践

**实践目标**：用现成测试验证「入口拒绝不进调度器」的分支顺序。

**操作步骤**：

1. 运行 stepped 桥的单元测试（CPU 即可）：

   ```bash
   cargo test --release -p pegainfer-frontend --lib negative_engine_core_logprob_counts_are_rejected_before_submission -- --nocapture
   ```

2. 对照测试体 [stepped.rs:L713-L740](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L713-L740)：给负数 `logprobs` 的 wire 请求，断言了三件事——`streams` 为空、提交通道 `try_recv` 为空（即**没有**提交给调度器）、输出通道立即收到一条 `finish_reason: Error` 的批量输出。

**需要观察的现象**：测试通过；输出里可见「请求被翻译成终态 Error 输出」而非进入调度器。

**预期结果**：1 个测试通过。这条测试就是第④站「校验先于提交」的可执行证据。若无法本地运行，标记**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Abort 分支必须「先摘 stream、后翻旗标」，反过来会怎样？

<details><summary>参考答案</summary>

反过来（先翻旗标后摘表）存在竞态：旗标翻转后、表项摘除前，一条携带 terminal 的 update 可能仍被 demux 成正常输出发给已取消的客户端。先摘表则任何在途 update 到达时查不到 stream 项而被静默丢弃；`Release` 写（abort 旗标）排在 `streams.remove` 之后，调度器侧读到旗标时表已摘除（[stepped.rs:L288-L299](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L288-L299) 注释）。
</details>

**练习 2**：`SchedulerHandle::submit` 为什么可以「永不失败」？请求会不会因此丢失？

<details><summary>参考答案</summary>

提交通道是无界 crossbeam 通道，send 不阻塞、不失败；即使调度器线程已退出（send 返回 Err），代码会把退回的信封 drop 掉，触发 `RequestEnvelope` 的 drop bomb 在步流上补发 `Failed` 终态（[wiring.rs:L77-L88](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L77-L88)）。所以请求不会无声丢失——「每个未应答请求必有终态」由 drop bomb 兜底。背压故意不存在：准入控制是调度器的职责，用 `Rejected` 终态表达。
</details>

**练习 3**：`client_label: Some(Arc::from(request_id.as_str()))` 里装的 wire 字符串 id，桥在哪些地方需要它？

<details><summary>参考答案</summary>

回程输出必须用 wire 的字符串 id 标记（`SteppedStream.request_id`），Abort 帧也只带 wire id，需要 `names` 表反查回契约 `RequestId`（[stepped.rs:L415-L419](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L415-L419) 与 L293）。契约 `RequestId` 是前端铸造的 u64，wire id 是 vLLM 协议的字符串——两张表就是两个身份体系的双向映射。
</details>

### 4.4 南段：drive 循环、账本与 StepOutputs 的回流

#### 4.4.1 概念说明

第⑤⑥⑦站发生在**专用 OS 线程**上。契约的哲学是「循环只写一份」：排空顺序、每迭代一次 metrics 发布一次 commit、关停与致命错误处理，全部固化在 `driver.rs` 的 `drive` 函数里，成为代码而非各模型 crate 的自觉。模型侧的全部义务就是 `Scheduler` 的三个方法（`submit`/`step`/`metrics`），u3-l2 已学；本讲补上它在真实请求里被**谁、以什么节奏**调用。

回程（第⑧站起）则是把南世界的「一步」翻译回北世界的「一条消息」：`dispatch_step` 拿一条 `StepOutputs`，对每个 update 找到对应 stream 状态，`reduce_update` 产出至多一条 `EngineCoreOutput`，整批连同 stats 一次性 `send_outputs`。

#### 4.4.2 核心流程

调度器线程每迭代的固定节奏（永不暂停）：

```
loop:
    while 提交通道有信封:          # ⑤ 排空
        ledger.register(envelope)   #    开户（prompt_len、queued_at 入账）
        scheduler.submit(queued)    #    只转移所有权，不做裁决
    step = scheduler.step(&ledger)  # ⑥ 模型侧一步：admit/push_tokens/finish/retire
    metrics.publish(scheduler.metrics())
    ledger.commit_step()            # ⑦ 本步所有 update 合并成一条 StepOutputs 发出
    if step 是 Err:
        ledger.fail_all(err); commit; return     # 引擎级致命：全员销账
    if 空闲 且 提交通道已断: return               # 优雅退出
    if 空闲: spin_loop 提示                       # 让出发射槽，下一轮继续探测
```

sim 引擎的一步（第⑥站的具体化）：

```
SimScheduler::step:
    for 每条新请求:
        已 abort? → ledger.retire
        planned_completion(): 按 max_tokens 计划补全 token（或重放剧本）
        ledger.admit(id)
        ledger.echo_prompt(...)           # 仅当请求了 prompt_logprobs
        pending 空 → ledger.finish        # max_tokens=0 之类
        否则入 running，next_token_at = now + TTFT(prompt_len)
    for 每条 running:
        已 abort? → retire
        未到 next_token_at? → 留在 running
        ledger.push_tokens(id, &[token], &logprobs)
        pending 空 → finish；否则 next_token_at = now + TPOT
    park_if_waiting()                     # 最长 1ms 的切片停车
```

其中 \( TTFT = base + L_{prompt} / r_{prefill} \)（毫秒），TPOT 为常数——u1-l4 的时序模型在源码里的落点。

#### 4.4.3 源码精读

**循环本体**。`drive` 见 [driver.rs:L59-L104](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L59-L104)：内层 `try_recv` 循环排空提交并 `register` + `scheduler.submit`（[driver.rs:L66-L79](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L66-L79)）；随后一步三拍——`step`、`metrics`、`commit_step`（[driver.rs:L80-L83](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L80-L83)）。致命错误路径：账本给**所有**未结账户销账并附真实错误（[driver.rs:L84-L92](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L84-L92)）；空闲迭代以 `spin_loop` 收尾、忙迭代绝不暂停（[driver.rs:L93-L102](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L93-L102)）。`Scheduler` trait 三方法的契约语义见 [driver.rs:L20-L41](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L20-L41)——特别注意 `submit` 的注释：**裁决一律推迟到 step**，因为驱动每迭代只 commit 一次，推迟不丢任何信息。

**开户与提交**。`RequestLedger::register` 消化信封、开 `Queued` 账户、断言 id 无重复，返回 `QueuedRequest` 给 `Scheduler::submit`（[ledger.rs:L303-L317](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L303-L317)）；`commit_step` 把本步声明合并成唯一一条 `StepOutputs` 发到步流，没触碰任何请求就什么都不发（[ledger.rs:L322-L330](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L322-L330)）。模型侧的账本写入口：`admit`（[ledger.rs:L160](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L160)）、`push_tokens`（[ledger.rs:L196](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L196)）、`finish`（[ledger.rs:L241](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L241)）、`retire`（[ledger.rs:L267](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L267)）。

**sim 的一步**。`start_engine` 直接以 `spawn_scheduler` 起线程并装进契约 `Engine`（[pegainfer-sim/src/lib.rs:L115-L139](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L115-L139)）；`SimScheduler::step` 的准入段与 token 泵见 [lib.rs:L187-L252](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L187-L252)，末尾的 `park_if_waiting` 把停车切成最长 1ms 的片段（[lib.rs:L170-L179](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L170-L179)）——注释说明了原因：新提交只在步间排空，整段 TTFT 睡眠会卡死准入。

**回程分发**。`dispatch_step` 遍历一条 `StepOutputs` 的所有 update：查无 stream 项（已 abort/已终结）即丢弃；`reduce_update` 产出输出与是否终结；终结则摘表并收进 `finished_requests`（[stepped.rs:L194-L225](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L194-L225)）。整批与「发送时拉取」的 stats 一起发出（[stepped.rs:L246-L259](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L246-L259)）——注释点明时机妙处：driver 在 commit **之前**已发布本步 metrics，所以这条批次携带的正是与自己 token 匹配的快照。

**双契约对照**（本讲必答题）：legacy 桥 `LocalEngineBridge::run` 结构同为四臂 select（[bridge.rs:L110-L157](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L110-L157)），但北端持有的是 `EngineHandle`，南端事件来自共享 `(RequestTag, TokenEvent)` 频道，需要 `dispatch_burst` 把逐 token 事件**折叠**成批量输出（[bridge.rs:L404](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L404) 起）；abort 走 `RequestAbortReason` 原子原因而非布尔旗标，且区分「未出首 token = Disconnected / 已出 = Cancelled」（[bridge.rs:L196-L215](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L196-L215)）。stepped 桥没有折叠层——调度器已经折好了。文档对两桥分工的记载见 [frontend-architecture.md:L70-L72](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docs/subsystems/frontend/frontend-architecture.md#L70-L72)。

#### 4.4.4 代码实践

**实践目标**：在无 GPU 环境下运行 `drive` 循环的契约自测，验证第⑤⑥⑦站的节奏。

**操作步骤**：

1. 运行 driver 测试（CPU 即可）：

   ```bash
   cargo test --release -p pegainfer-frontend --lib driven_engine -- --nocapture
   ```

2. 精读测试 `driven_engine_streams_and_drains_on_shutdown`（[driver.rs:L177-L209](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L177-L209)）：一个每步每请求发一枚 token 的 `EchoScheduler`，断言收齐 token 序列 `0,1,2`、终态 `Finished{Length, prompt 2, completion 3}`，随后 drop 句柄、join 驱动线程验证优雅退出。

**需要观察的现象**：测试通过——意味着「提交 → register → EchoScheduler::step 逐 token push → commit_step → 步流收端拿到合并后的 StepOutputs」全链在无 GPU 下可复现。

**预期结果**：`driven_engine_streams_and_drains_on_shutdown` 与 `fatal_step_fails_in_flight_requests_with_the_error` 两个测试通过（后者验证致命路径的 `fail_all` 销账）。若无法运行，标记**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`Scheduler::submit` 为什么故意「不做任何裁决」？把准入判定写进 submit 会破坏什么？

<details><summary>参考答案</summary>

驱动每迭代只 `commit_step` 一次。若 submit 时就写裁决（如 `Rejected`），该裁决要等本迭代末尾的 commit 才发出——与把同样裁决写在 step 里**落在同一次 commit**，没有任何收益；而统一「step 是唯一账本写点」让「每个未应答请求恰好一个终态」只需在一处验证（u3-l2 的账本状态机）。见 [driver.rs:L23-L27](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L23-L27) 的 trait 注释。
</details>

**练习 2**：为什么 stepped 桥的 stats 是「发送时拉取」而不是像 legacy 桥那样起一个 watch 订阅任务？

<details><summary>参考答案</summary>

step 契约的 driver 忙轮询：每次 spin 都会更新 metrics 单元，watch 的 `changed()` 边沿会逐 spin 触发，把订阅者变成消息洪水。所以 stepped 桥不给 `connect_link` 传 watch（[bridge.rs:L785-L800](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L785-L800) 注释），而是在发批次时拉一次快照盖上；空闲引擎不发布任何东西。metrics 单元本身是 `Mutex<SchedulerMetrics>` 的拉取式设计，见 [wiring.rs:L46-L55](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L46-L55)。
</details>

**练习 3**：sim 的 `park_if_waiting` 最长只睡 1ms。如果改成睡满到 `next_token_at`，最先坏掉的是哪个环节？

<details><summary>参考答案</summary>

准入（以及 abort 响应）。新提交只在步**间**排空（drive 的内层循环），睡满 TTFT/TPOT 间隔意味着这期间到达的请求要等几百毫秒才能 `register`；1ms 切片让停车几乎不延迟下一轮排空。见 [lib.rs:L19-L22](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L19-L22) 的常量注释。注意这是 sim 在 step 契约下唯一一处「策略内的停车」，真实模型（如 gemma4 的 async prefill drain）有各自受控的例外。
</details>

### 4.5 回程终点与异常定位：reduce_update、SSE 与排障手册

#### 4.5.1 概念说明

回程最后三个容易踩坑的细节，全部集中在 `reduce_update`：

1. **元数据步不发输出**：一条只携带 `scheduled`/`cached_tokens`/`kv_transfer` 的 update（没有任何 token 与终态）**不产出** wire 输出，事实暂存在 stream 状态里等下一个真输出搭车——否则 vLLM 侧会收到一个空 chunk。
2. **Stop 终态的哨兵 token**：PegaInfer 在发 token 前就抑制了 EOS，而 vLLM 文本解码器约定「Stop 终态输出的最后一个 token 会被无条件删除」。于是桥在 Stop 输出末尾补一枚哨兵 token 专门给它删——usage 计数因此保住了被抑制的 EOS。
3. **两种时钟**：契约时间戳是单调 `Instant`，wire 要 Unix 浮点秒，`UnixAnchor` 在桥启动时同时读两个钟做基准，之后一律换算。

#### 4.5.2 核心流程

一条 `RequestUpdate` 的翻译决策树：

```
reduce_update(state, update):
    scheduled?      → 暂存 Queued/Scheduled 两个 wire 事件 + prompt_tokens
    cached_tokens?  → 暂存
    kv_transfer?    → 暂存
    prompt_echo?    → 翻译 prompt logprobs；失败 ⇒ fail_prompt（abort + Error 终态）
    for 每个 token:  → 组装 PositionLogprobs（带或不带 logprob）
    terminal?
      Finished{Stop} 且有哨兵 → token_ids 末尾补哨兵
      Finished{其他} → convert_finish_reason
      Rejected/Failed → EngineCoreFinishReason::Error + StopReason::Text(渲染消息)
    无 token、无 prompt_echo、未终结? → 返回 (None, false)   # 元数据步不发输出
    否则 → 一条 EngineCoreOutput（搭车 first_token_events、prefill_stats、kv_transfer）
```

#### 4.5.3 源码精读

**元数据暂存与搭车**。`SteppedStream` 的字段就是「等待搭车的事实清单」：首 token 前攒着的 Queued/Scheduled 事件、`prompt_tokens`、`cached_tokens`、P/D 换手元数据、哨兵 id——见 [stepped.rs:L426-L447](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L426-L447)。prefill stats 的惰性构造（`computed + cached == prompt` 的上游不变式）在 [stepped.rs:L489-L503](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L489-L503)。

**翻译本体**。[stepped.rs:L510-L608](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L510-L608) 是 `reduce_update` 全文。哨兵补发的注释与实现见 [stepped.rs:L559-L571](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L559-L571)；哨兵 id 的选取（EOS 或显式 stop token）在 [bridge.rs:L586-L591](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L586-L591)。`Rejected`/`Failed` 在此只剩字符串通道——类型化分类止步于桥，客户端看到的是渲染后的消息（[stepped.rs:L574-L588](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L574-L588)）。「什么都不携带就不发」的守卫在 [stepped.rs:L591-L593](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L591-L593)。

**时钟锚**。`UnixAnchor` 一次性同时读系统钟与单调钟，之后正负两个方向的换算共用一个基准（[stepped.rs:L613-L633](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L613-L633)）。

**关停路径**。桥退出循环后，把所有在途请求的 abort 旗标翻转（调度器下次触碰即 retire），drop 输出通道、abort 子任务（[stepped.rs:L172-L182](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L172-L182)）；总装层再等调度器线程排空退出（[mod.rs:L337-L349](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L337-L349)），最后删除 IPC 命名空间目录（[mod.rs:L420](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L420)）。

**排障手册**（把全链路倒过来用）：

| 症状 | 大概率阶段 | 首先看 |
| --- | --- | --- |
| 端口不通 / 起服 hang | ①② 之前的基建期 | [mod.rs:L233-L249](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L233-L249) engine_task、`connect_link` 的端点等待与 30 分钟 ready_timeout |
| 请求 400/参数错误、秒回 error | ④ 入口校验 | [wire.rs:L121](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L121) `unsupported_request_params`、[stepped.rs:L324-L366](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L324-L366) |
| 请求被接受后 hang 到天荒地老 | ⑤⑥⑦ 调度器侧 | 模型 crate 的 `Scheduler::step`；若是引擎致命，`fail_all` 已发 `Failed` 终态——查 `scheduler fatal, engine winding down` 日志（[driver.rs:L84-L92](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L84-L92)） |
| 流中途停止、无 finish_reason | abort 路径 | [stepped.rs:L285-L299](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L285-L299)、[request_lifecycle.rs:L195](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/request_lifecycle.rs#L195) |
| finish_reason=error 带文本 | 模型侧 fail/reject 的渲染 | 模型 `step` 里调用 `ledger.fail`/reject 的位置 + [stepped.rs:L574-L588](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L574-L588) |
| Stop 结束但少一个 token / usage 少 1 | 哨兵机制 | [stepped.rs:L559-L571](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L559-L571)、[bridge.rs:L586-L591](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L586-L591) |
| 指标不动 / 与 token 不匹配 | stats 时机 | [wiring.rs:L46-L61](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L46-L61)、[stepped.rs:L185-L192](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L185-L192)（空闲引擎本来就不发布） |

#### 4.5.4 代码实践

**实践目标**：跑通一次真实的流式 chat 请求，并把 SSE 观察与 `reduce_update` 的代码分支一一对上。

**操作步骤**：

1. 运行 e2e 测试（CPU 即可，会自动搭好带最小分词器元数据的服务）：

   ```bash
   cargo test --release -p pegainfer-sim --test frontend_e2e chat_completions_streaming -- --nocapture
   ```

2. 精读断言体 [frontend_e2e.rs:L613-L671](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/tests/frontend_e2e.rs#L613-L671)：`max_tokens=2`、prompt 一词 "alpha"，断言首 chunk 带 `role: assistant`、内容拼接等于 `" alpha alpha"`（sim 循环回放 prompt token）、末 chunk `finish_reason: "length"`。
3. 对照 `reduce_update` 回答：Queued/Scheduled 两个事件搭在哪条输出上？（提示：`first_token_events` 只在第一条**真**输出时随行发出。）

**需要观察的现象**：测试通过；SSE 文本流中每个 `data:` chunk 都是合法 JSON 且 `object == "chat.completion.chunk"`，最后一行是 `data: [DONE]`。

**预期结果**：`chat_completions_streaming_emits_role_content_and_done` 通过（相关联的 `streaming_completion_emits_terminal_done` 也会被过滤器命中）。若无法运行，标记**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么「只有 scheduled 的 update」不能单独发一条 wire 输出？

<details><summary>参考答案</summary>

那样发出的输出没有任何 token、logprobs、终态——对 vLLM 文本解码器而言是一个空 chunk，只会污染流。所以 `first_token_events` 暂存 Queued/Scheduled 两个事件，等第一条携带 token 的输出**搭车**发出（[stepped.rs:L429-L433](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L429-L433) 字段注释、[stepped.rs:L591-L593](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L591-L593) 守卫）。唯一的例外是 prompt_echo（prompt logprobs）——它本身就是可交付内容。
</details>

**练习 2**：如果没有哨兵 token 机制，一个以 Stop 结束的请求会在客户端表现出什么？

<details><summary>参考答案</summary>

vLLM 文本解码器会无条件删除 Stop 终态输出的最后一个 token。PegaInfer 已在源头抑制 EOS，若桥再不补哨兵，最后一个**真实生成**的 token 会被误删——正文缺字、usage 计数也少一枚。补一枚专供删除的 EOS/stop 哨兵（[stepped.rs:L559-L571](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L559-L571)）正好对冲这一语义。
</details>

**练习 3**：排障手册里「请求 hang」一行为什么让你先查模型 crate 的 `step`，而不是桥？

<details><summary>参考答案</summary>

北世界的每一环（提交通道、步流、桥 select）要么永不阻塞、要么有 drop bomb/`fail_all` 兜底，未应答请求必有终态；真正能「既不推进也不报错」的是模型 `Scheduler::step` 自身（如调度死锁、GPU 挂起）。引擎级致命另有专属日志 `scheduler fatal, engine winding down` 可查。这是契约设计的直接推论：异常路径都收敛到了账本。
</details>

## 5. 综合实践

**任务：写一份「函数级旅程日志」，并用 pegainfer-sim 验证它。**

这是本讲的毕业练习，产出一篇可长期维护的文档（建议存为自己的笔记，不写入仓库）。

**第一步：写旅程日志。** 以「一次 streaming chat 请求」为题，写十节，每节固定格式：

```
## 第 N 站：<一句话标题>
- 文件：<相对路径>
- 函数：<函数名（及关键行号）>
- 输入类型 → 输出类型：<类型名>
- 运行环境：<tokio async 任务 / 调度器 OS 线程 / 外部 crate>
- 一句话：<这里发生了什么>
```

要求覆盖 4.1.2 的全部十站；①②⑨ 三站注明「外部 vllm-server crate，函数体不在本仓库」，但要写清它们与仓库内代码的**接缝位置**（`Config` 构造与 `connect_link` 握手）。

**第二步：用 pegainfer-sim 验证可观察的站点。**

1. 启动 sim（无 GPU 要求）：

   ```bash
   cargo run --release -p pegainfer-sim -- --port 8000 --tpot-ms 200
   ```

   （`--tpot-ms 200` 把 token 间隔放大到肉眼可辨；默认 12ms。）

2. 发流式请求并计时：

   ```bash
   curl -N http://127.0.0.1:8000/v1/chat/completions \
     -H 'Content-Type: application/json' \
     -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"alpha"}],
          "max_tokens":4,"temperature":0.0,"stream":true}' \
     | while read -r line; do echo "$(date +%s.%N) $line"; done
   ```

3. 在旅程日志的验证栏里记录三个可观察事实，并与源码预测对齐：
   - **相邻 `data:` 行的时间差 ≈ 200ms** —— 对应第⑥⑦站：`SimScheduler::step` 的 `next_token_at = now + TPOT` 与每步一条 `StepOutputs`（[lib.rs:L241-L247](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L241-L247)）；
   - **首 chunk 与后续 chunk 的间隔明显更长** —— TTFT 项 \( base + L_{prompt}/r_{prefill} \)（[lib.rs:L91-L93](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L91-L93)），可用 `--prefill-tokens-per-ms` 调节验证；
   - **首 chunk 带 `role: assistant`、末 chunk 带 `finish_reason: "length"`、最后一行 `data: [DONE]`** —— 对应第⑧⑩站的 `reduce_update` 与外部 crate 的 SSE 组装（e2e 断言在 [frontend_e2e.rs:L647-L668](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/tests/frontend_e2e.rs#L647-L668)）。

4. 改参数复跑：把 `--tpot-ms` 改成 50、`--prefill-tokens-per-ms` 改成 10，预测并记录节奏变化。

**一个诚实的提醒（也是验证练习的一部分）**：`pegainfer-sim` 的二进制**没有安装任何 logger**（其 `main.rs` 从未调用日志初始化，`Cargo.toml` 也不依赖日志后端），所以 `RUST_LOG=info` 对它无效——桥里的 `info!("local vLLM engine … stepped bridge connected")` 之类语句在 sim 下是静默的。真正安装日志器的是 `pegainfer-server`（真实模型入口）。因此本练习用**可观察行为**（时间戳节奏、SSE 内容）代替日志验证；如果你想在 sim 上看日志，可以自己在 `main.rs` 加两行日志初始化（示例代码，非项目原有）：

```rust
// 示例代码：在 pegainfer-sim/src/main.rs 的 main() 开头加入
// （需自行向 pegainfer-sim/Cargo.toml 添加 env_logger 依赖）
env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
```

改完重跑 `RUST_LOG=info cargo run --release -p pegainfer-sim -- --port 8000`，你就能在 stderr 里看到握手后那条 `stepped bridge connected` 日志——它会告诉你 `input=`/`output=` 两个 IPC socket 的路径，即第③站的物理落点。

**验收标准**：旅程日志十站齐全、每站有文件与函数名；验证栏记录了至少两组参数下的节奏数据并与公式预测一致；能指出三处「本仓库找不到函数体」的站点及它们的接缝位置。全程无需 GPU；无法运行的环境下，把第二步替换为精读 `frontend_e2e.rs` 的断言并逐条标注它验证了哪一站，其余标记**待本地验证**。

## 6. 本讲小结

- **一条链路，两个世界**：北世界（外部 vllm-server + 桥的 async select 循环）与南世界（调度器专用 OS 线程的同步 `drive` 轮询）之间只有两条通道——crossbeam 提交通道（北→南）与 tokio mpsc 步流（南→北）；任何带 `EngineCore` 前缀的类型只允许出现在桥里。
- **起服是并行的，但端口语义不骗人**：HTTP 与引擎加载并行跑，可 HTTP 只在桥注册后才绑定端口，所以「端口可达 = 引擎就绪」依然成立；引擎失败由 engine_task 主动取消服务，30 分钟 `ready_timeout` 只兜真挂死。
- **北向入口三步走**：`handle_message` 解帧 → `start_request` 做 wire 校验（不合规请求根本不进调度器）与 `convert_sampling` 词表翻译 → `SchedulerHandle::submit` 铸 id/旗标/信封，永不失败，丢失由 drop bomb 兜底成 `Failed` 终态。
- **南向节奏是「排空 → step → metrics → commit」**：裁决一律发生在 `Scheduler::step` 这个唯一账本写点，`commit_step` 每迭代把所有 update 合并成一条 `StepOutputs`；引擎级致命由 `fail_all` 给全部未结账户销账。
- **回程翻译的三个坑**：元数据步不发输出（事实搭车下一条真输出）；Stop 终态补哨兵 token 对冲 vLLM 的「删最后一个 token」语义；单调钟 `Instant` 经 `UnixAnchor` 换算成 wire 的 Unix 浮点秒。
- **排障从症状倒推站点**：参数错看 `wire.rs`、hang 看模型 `step`、流断看 abort 旗标、少 token 看哨兵、指标不动先确认引擎是否空闲（空闲本来就零发布）。

## 7. 下一步学习建议

本讲完成了前端与契约部分的闭环。接下来按依赖关系有两条路：

1. **进入单元 4（共享运行时）**：推荐先读 [u4-l1 张量与设备层](u4-l1-tensor-and-device-context.md)，开始接触 `pegainfer-kernels`/`pegainfer-core`——它们是南世界 `Scheduler::step` 里真正干活的底座。如果你更关心调度器本身的实现，可以先跳到单元 6 的 u6-l1（Qwen3 调度器），看一个真实模型如何写 `step`。
2. **巩固本单元**：重读 [docs/subsystems/frontend/frontend-architecture.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docs/subsystems/frontend/frontend-architecture.md) 的「What was deleted, and the heirs」表（L98-L108）——它解释了今天看到的每条设计是从哪次阵痛演化来的；再浏览 `pegainfer-qwen3/src/frontend_adapter.rs`（u6-l2 的主角），提前感受「一个模型如何实现本讲的 `Scheduler` trait」。

若你想为迁移 legacy 桥出力，文档已点名下一步：glm52 迁移到 step 契约（多调度器试点），完成后旧契约模块与 `LaunchedEngine::Handle` 将被删除——届时本讲的排障手册也该随之删去 legacy 行。
