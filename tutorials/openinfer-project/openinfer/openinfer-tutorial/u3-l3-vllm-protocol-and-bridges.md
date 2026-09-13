# u3-l3 vLLM 协议栈与 ZeroMQ 桥

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释为什么 PegaInfer 的 OpenAI HTTP 路由、分词器、chat 模板都生活在外部 `vllm-server` crate 里，而本仓库只负责「扮演」一个 vLLM EngineCore 进程。
2. 追踪一条请求在 ZeroMQ IPC 上的双向流动：`EngineCoreRequest`（HTTP 侧 → 引擎侧）与 `EngineCoreOutputs`（引擎侧 → HTTP 侧），包括启动时的 `EngineCoreReadyResponse` 握手。
3. 区分两条桥的差异与分工：`LocalEngineBridge`（legacy，消费 `TokenEvent` 流并重新折叠）与 `SteppedEngineBridge`（新，把每条 `StepOutputs` 1:1 翻译成一条 wire 消息），并说清迁移方向。
4. 对照 `wire.rs` 列出 sampling 参数在引擎契约与 vLLM wire 格式之间的转换规则，以及「不支持即拒绝」的参数门禁。

## 2. 前置知识

### 2.1 为什么需要一个「协议栈」

PegaInfer 自己不写 HTTP 层。一个生产级 OpenAI 兼容服务需要的东西非常多：`/v1/completions` 与 `/v1/chat/completions` 路由、请求校验、分词器（tokenizer）、chat 模板渲染、SSE 流式输出、usage 统计、Prometheus 指标。这些 vLLM 项目已经用 Rust 重写了一套 frontend crates。PegaInfer 直接复用它们——workspace 根 `Cargo.toml` 把这些 crate 以 git 依赖钉在固定 rev 上（[Cargo.toml:182-186](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L182-L186)）：

```toml
vllm-chat = { git = "https://github.com/vllm-project/vllm.git", rev = "89dbb264..." }
vllm-engine-core-client = { git = "https://github.com/vllm-project/vllm.git", rev = "89dbb264..." }
vllm-server = { git = "https://github.com/vllm-project/vllm.git", rev = "89dbb264..." }
vllm-text = { git = "https://github.com/vllm-project/vllm.git", rev = "89dbb264..." }
vllm-tokenizer = { git = "https://github.com/vllm-project/vllm.git", rev = "89dbb264..." }
```

> 小提醒：`docs/subsystems/frontend/frontend-architecture.md` 的 TL;DR 写着钉在 `295ac4e5`，这是文档相对最近一次 crate bump（HEAD 提交 `139d925e` "bump vllm rust crates to 89dbb264"）略有滞后。以 `Cargo.toml` 为准——本仓库的文档规范本身就规定「doc 正文权威」，读代码时留意这种漂移。

问题在于：**上游 `vllm-server` 假设引擎是一个独立进程**，两者通过消息队列通信。PegaInfer 的引擎明明编译在同一个二进制里，怎么办？答案是「冒充」（impersonation）：在进程内部用 ZeroMQ IPC socket 搭起 vLLM 期望的那对通信端点，让 `vllm-server` 以为自己连上了一个真正的 EngineCore 进程。`pegainfer-frontend` 的 `vllm` 模块就是这场冒充的全部道具。

### 2.2 需要认识的术语

| 术语 | 解释 |
|------|------|
| **ZeroMQ（ZMQ）** | 一个轻量消息库，提供多种「socket 类型」与连接模式。这里只用它的 IPC（进程间，Unix domain socket）传输，地址形如 `ipc:///tmp/pgi-…/input.sock`。 |
| **DEALER / PUSH socket** | 桥用的两种 ZMQ socket：DEALER 双向、可收可发（桥用它收请求、发握手）；PUSH 单向只发（桥用它发输出）。 |
| **MessagePack（msgpack）** | 二进制 JSON 替代品，vLLM EngineCore 协议的序列化格式。`encode_msgpack` / `decode_msgpack` 是编解码入口。 |
| **EngineCore 协议** | vLLM frontend 与引擎之间的消息词汇表：请求帧（`EngineCoreRequest`）、输出帧（`EngineCoreOutputs`）、就绪握手（`EngineCoreReadyResponse`）、工具调用（Utility）。类型定义在 `vllm-engine-core-client` crate。 |
| **SSE** | Server-Sent Events，`text/event-stream` 上的流式 HTTP 响应。chat 流式输出就是一条条 `data: {...}` 帧，最后以唯一一行 `data: [DONE]` 终止（u1-l4 已验证）。SSE 的生成在 `vllm-server` 内部，不在本仓库。 |
| **data_parallel_size / engine_index** | 一个 HTTP 端点可以背后挂多个「引擎身份」（每个对应一个调度器分区）。wire 上用这两个字段区分。 |

### 2.3 承接前两讲

u3-l1 讲了 legacy 契约（`EngineHandle` / `TokenEvent` / `TokenSink`），u3-l2 讲了 step 契约（`SchedulerHandle` / `StepOutputs` / `RequestUpdate` / `RequestControl` / `RequestLedger`）。两代契约目前共存于 `LaunchedEngine` 枚举（[pegainfer-frontend/src/engine/wiring.rs:152-156](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L152-L156)）：`Handle(EngineHandle)` 是旧引擎的手柄，`Stepped(Engine)` 是新引擎的包裹（`Engine` 含 `schedulers`、`info`、`lora` 三字段，见 [wiring.rs:132-140](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L132-L140)）。**本讲的两条桥正好一一对应这两个分支**——这就是迁移期的完整图景。

## 3. 本讲源码地图

| 文件 | 行数 | 职责 |
|------|------|------|
| [pegainfer-frontend/src/vllm/mod.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L1-L463) | 463 | `serve` 入口家族；创建 IPC namespace；按 `LaunchedEngine` 双臂分发桥；组装 `vllm_server::Config` 并启动 HTTP |
| [pegainfer-frontend/src/vllm/bridge.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L1-L962) | 962 | `LocalEngineBridge`（legacy 桥）+ **两桥共享**的 `BridgeLink` / `connect_link` 握手 / `output_loop` 发送泵 / 指标发布 |
| [pegainfer-frontend/src/vllm/bridge/stepped.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L1-L793) | 793 | `SteppedEngineBridge`（新桥）：每条 `StepOutputs` 翻译成一条 `EngineCoreOutputs` |
| [pegainfer-frontend/src/vllm/wire.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L1-L441) | 441 | 参数翻译层：`EngineCoreSamplingParams ↔ SamplingParams`、logprobs 载荷、finish reason、LoRA xarg |
| pegainfer-frontend/src/vllm/bridge/tests.rs | 683 | legacy demux 的行为测试（无 socket，直接喂 `TokenEvent`） |
| pegainfer-frontend/src/vllm/request_contract.rs | 137 | GLM5.2 prefill-only 模式的路由守卫（`max_tokens=1` 校验） |
| pegainfer-frontend/src/vllm/lora.rs | 591 | LoRA 路由与启动加载，u9-l3 详讲，本讲只路过 |

模块声明与可见性见 [mod.rs:29-41](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L29-L41)：`bridge`、`lora`、`request_contract`、`wire` 全是私有子模块，其中 `stepped::SteppedEngineBridge` 在 [bridge.rs:957-959](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L957-L959) 被 `pub(crate)` 重导出——对外只露出 serve 函数与 LoRA 类型。

## 4. 核心概念与源码讲解

### 4.1 vllm::mod —— serve 入口家族与 LaunchedEngine 双臂分发

#### 4.1.1 概念说明

`mod.rs` 是协议栈的「总装车间」。它要解决三件事：

1. **何时启动 HTTP**：越早越好。`vllm-server` 启动要花约 1 秒加载分词器和 chat 模板，而多卡 MoE 模型的权重加载要几分钟——两边并行，谁也不等谁。
2. **引擎就绪的语义**：HTTP 端口只在桥注册完成后才 bind，所以「端口可达」等于「引擎就绪」，客户端不需要额外的健康探测语义（[mod.rs:56-63](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L56-L63) 的 doc 明说了这个契约）。
3. **双契约分发**：`LaunchedEngine` 是 `Handle` 还是 `Stepped`，决定给每个引擎身份（engine identity）挂哪种桥。

入口家族有四个公开函数，全部最终漏进同一个私有函数 `serve_model_on_host_with_router_extension`：

- `serve`：单引擎身份的普通服务（[mod.rs:64-82](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L64-L82)）；
- `serve_with_engine_count`：数据并行模型线用，一个端点背后 `engine_count` 个身份（[mod.rs:88-108](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L88-L108)）；
- `serve_prefill_only_with_engine_count`：要求 `max_tokens=1` 的 prefill-only 模式（GLM5.2 用），路由守卫在 `request_contract.rs`（[mod.rs:111-133](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L111-L133)）；
- `serve_model_with_lora_routes`：把已经造好的 `Engine` 包成 `LaunchedEngine::Stepped` 并叠加 LoRA 路由（[mod.rs:135-178](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L135-L178)）——注意 `std::future::ready(Ok(LaunchedEngine::Stepped(engine)))`：引擎已就绪时 future 立即解析。

`max_model_len` 的解析也在这里：优先用调用方给的值，否则读 `model_path/config.json` 的 `max_position_embeddings`（兼容嵌套 `text_config`，[mod.rs:43-54](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L43-L54)），都没有则警告并兜底 4096（[mod.rs:431-443](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L431-L443)）。

#### 4.1.2 核心流程

`serve_model_on_host_with_router_extension`（[mod.rs:204-422](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L204-L422)）的主干：

```text
1. 校验 engine_count > 0，换算 data_parallel_size
2. local_ipc_namespace()  -> /tmp/pgi-<pid>-<uuid8>/（可用 PEGAINFER_IPC_DIR 改基目录）
3. 派生两个端点：input.sock（HTTP→引擎）、output.sock（引擎→HTTP）
4. spawn engine_task：
   a. await 引擎 future；失败则取消 server shutdown token，让错误浮出
   b. match LaunchedEngine：
      - Handle(handle)  -> 校验分区数 == engine_count，为每个 index 造一个
                           LocalEngineBridge 并 spawn bridge.run()
      - Stepped(engine) -> 校验 schedulers.len() == engine_count，把 scheduler.join
                           收集起来，为每个 scheduler 造一个 SteppedEngineBridge 并 spawn
   c. JoinSet 上 join 所有桥：任何桥意外退出/失败/panic => 取消整棵 shutdown 树
   d. 桥全灭后收割 scheduler 线程（暴露 scheduler panic）
5. 同时（并行）组装 vllm_server::Config：
   transport_mode = TransportMode::Bootstrapped { input_address, output_address,
       engine_start_index: 0, engine_count, data_parallel_size, ready_timeout: 30min }
6. vllm_server::serve_with_router_extension(config, server_shutdown, extend_router)
7. 收尾：取消桥、等 engine_task、删除 IPC namespace 目录
```

第 4a 步里 `engine` 是一个 `Future` 而不是现成的引擎——这就是「HTTP 先行」的实现机制：`vllm-server` 先花时间加载分词器，桥等 future 解析后再注册，两段时间重叠（[mod.rs:225-235](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L225-L235) 的注释解释了这个设计，以及加载失败如何取消 server 而不是挂在注册等待上）。

#### 4.1.3 源码精读

**双臂分发——本讲两节内容的接线点**。[mod.rs:250-276](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L250-L276) 是 legacy 臂：

```rust
LaunchedEngine::Handle(handle) => {
    let actual_partitions = handle.scheduler_partition_count();
    if actual_partitions != engine_count {
        server_shutdown.cancel();
        anyhow::bail!("frontend declared {engine_count} engines but the resolved handle \
                       exposes {actual_partitions} scheduler partitions");
    }
    ...
    for engine_index in 0..engine_count {
        let bridge = LocalEngineBridge { input_address: ..., output_address: ...,
                                         handle: handle.clone(), max_model_len,
                                         engine_index: engine_index as u32,
                                         data_parallel_size,
                                         metrics_watch: handle.metrics_watch_for(engine_index) };
        bridges.spawn(async move { (engine_index, bridge.run(shutdown).await) });
    }
}
```

这段做了三件事：声明数与实际分区数不符则整体失败（前端声明 4 个引擎、句柄只有 2 个分区，是接线 bug，宁可拒启）；用 `servable_len` 收紧 `max_model_len`；每个引擎身份克隆一份句柄、造一座桥。

[mod.rs:277-303](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L277-L303) 是 stepped 臂，结构对称但多一步：`scheduler_joins.push(scheduler.join)` 把驱动线程的 `JoinHandle` 收起来——u3-l2 讲过 stepped 引擎自持调度线程，桥（连同 `SchedulerHandle`）全灭后提交频道断开，driver 排空退出，最后由 [mod.rs:340-349](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L340-L349) 的 `spawn_blocking` 逐个 join，把 scheduler panic 浮出水面。

**Config 组装**。[mod.rs:357-400](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L357-L400) 构造 `vllm_server::Config`，核心是 transport（[mod.rs:358-369](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L358-L369)）：

```rust
transport_mode: TransportMode::Bootstrapped {
    input_address,
    output_address,
    engine_start_index: 0,
    engine_count,
    data_parallel_size: engine_count,
    // 桥在引擎 future 解析后才注册，所以这个超时实际约束的是整个引擎加载
    // （多卡 MoE 要几分钟，冷启动更久）；加载失败已经由 engine task 取消
    // server 兜底，这里只抓真正的挂死。
    ready_timeout: Duration::from_mins(30),
},
```

`Bootstrapped` 模式告诉 `vllm-server`：不要自己 spawn 引擎进程，去这对地址上等引擎注册。其余字段全是服务面配置（CORS、TLS、日志开关、`served_model_name` 等），最后在 [mod.rs:402-403](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L402-L403) 交给 `vllm_server::serve_with_router_extension`。

**IPC namespace**。两个 socket 地址由 [mod.rs:221-223](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L221-L223) 生成，落到 [bridge.rs:927-939](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L927-L939) 的两个小函数：每次启动创建一个 `/tmp/pgi-<pid>-<uuid前8位>` 目录（`PEGAINFER_IPC_DIR` 可改基目录），拼出 `ipc://<dir>/input.sock` 与 `ipc://<dir>/output.sock`；服务退出时 [mod.rs:420](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L420) 整目录删掉。UUID 保证多个 PegaInfer 实例同机互不串线。

#### 4.1.4 代码实践

**实践目标**：用无 GPU 的 e2e 测试验证「端口可达 = 引擎就绪」与分区数校验这两条本讲断言。

1. 打开 [pegainfer-sim/tests/frontend_e2e.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/tests/frontend_e2e.rs#L1-L60) 通读 `SimServer::spawn`：它在 `TempDir` 里手工造最小模型元数据，起 sim 引擎，然后轮询健康端点。注意它测的就是 `Stepped` 臂——sim 是 step 契约引擎（u1-l4）。
2. 运行（纯 CPU，无需 GPU 与权重）：

   ```bash
   cargo test --release -p pegainfer-sim --test frontend_e2e \
       frontend_rejects_engine_partition_mismatch -- --nocapture
   ```

   这个测试（[frontend_e2e.rs:463](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/tests/frontend_e2e.rs#L463)）故意让前端声明数与引擎实际分区数不一致，验证 4.1.3 读到的 bail 分支真的拒启。
3. 再跑一个正向用例确认全链路：

   ```bash
   cargo test --release -p pegainfer-sim --test frontend_e2e \
       chat_completions_streaming_emits_role_content_and_done -- --nocapture
   ```

**需要观察的现象**：第一个测试快速失败且错误信息含 `frontend declared ... engines`（或分区不匹配的同义表述）；第二个测试通过，日志里能看到 sim 引擎启动、HTTP 起服、SSE 以 `data: [DONE]` 收尾。**预期结果**：两测均符合上述描述——本环境未实际执行，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `serve` 的参数是 `engine: impl Future<Output = Result<LaunchedEngine>>` 而不是 `LaunchedEngine` 本身？

答案：为了让 HTTP 前端与引擎加载并行。收到一个 future，`vllm-server` 的分词器/模板加载（约 1 秒）与权重加载（多卡 MoE 可达分钟级）同时进行；若传现成引擎，加载串行在前，端口可用时间被推迟整个加载期。

**练习 2**：`engine_count` 与 `data_parallel_size` 在本讲代码里是什么关系？谁消费它们？

答案：在 `serve_with_engine_count` 的调用语境下两者相等（[mod.rs:363-364](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L363-L364) 直接传同一个值）：一个「前端可见引擎身份」对应一个调度器分区，即一个数据并行 rank。`data_parallel_size` 消费者是 `vllm-server`（wire 协议里区分多引擎身份），`engine_count` 是本地循环造桥的次数。

**练习 3**：ready_timeout 设 30 分钟，会不会让「引擎加载失败」的客户端等半小时？

答案：不会。加载失败路径由 engine_task 处理：future 解析为 `Err` 时立刻 `server_shutdown.cancel()`（[mod.rs:239-245](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L239-L245)），server 被取消而返回；ready_timeout 只兜底「future 永不解析」的真挂死。

### 4.2 vllm::bridge —— 共享 BridgeLink 与 LocalEngineBridge（legacy 桥）

#### 4.2.1 概念说明

这个文件装了三样东西：

1. **`connect_link`——两桥共用的传输层**。不管哪代契约，向北面对 `vllm-server` 的部分完全一样：等 IPC 端点出现、连 DEALER、发就绪握手、连 PUSH、起发送泵。抽象成 `BridgeLink` 结构后，stepped 桥直接复用（[bridge.rs:688-696](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L688-L696) 的注释明说「引擎数据面以北的一切在两桥间 identical」）。
2. **`LocalEngineBridge`——legacy 桥本体**。它拿到的是 `EngineHandle`（u3-l1），消费的是每请求的 `TokenEvent` 流。
3. **指标发布**。把调度器的负载快照翻译成 wire 上的 `SchedulerStats`。

legacy 桥的核心难题是** demux（多路分解）**：u3-l1 讲过，legacy 契约用一条共享频道承载所有请求的事件，靠 `RequestTag` 标签区分。桥是这条频道的唯一消费者，必须把混流拆回每请求、再折叠成 vLLM 期望的批量输出消息。

#### 4.2.2 核心流程

**握手（`connect_link`，[bridge.rs:698-807](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L698-L807)）**：

```text
1. wait_for_ipc_endpoint x2：轮询文件系统等 input.sock/output.sock 出现
   （20ms 一次；vllm-server 是 socket 的 bind 方，先出现才能 connect）
2. 造 DealerSocket，peer identity = EngineId::from_engine_index(engine_index)
3. 连 input_address
4. 组装 EngineCoreReadyResponse 并 msgpack 发出 —— 握手完成
5. 造 PushSocket 连 output_address
6. spawn output_loop 子任务：从内部 unbounded channel 收 EngineCoreOutputs，
   逐条 encode_msgpack 后 ZMQ send
7. （仅 legacy）若有 metrics watch：spawn publish_scheduler_stats 子任务
```

其中第 4 步的 ready 报文要向 `vllm-server` 报告引擎「能力」。KV 容量换算（[bridge.rs:724-737](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L724-L737)）把契约的 `KvCapacity`（总块数 `B`、块大小 `s`）翻译成 vLLM 口径的两个数：KV 总 token 数，以及单请求并发上限

\[ C = \frac{B}{\lceil L_{\max} / s \rceil} \]

即「块总数 ÷ 每请求最多占的块数」——vLLM 用它估算能同时服务多少条满长请求。

**legacy 桥主循环（`run`，[bridge.rs:81-170](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L81-L170)）** 是一个 `biased` 的四路 `tokio::select!`（优先级从上到下）：

```text
biased select:
  1. shutdown.cancelled()        -> 退出
  2. child_tasks.join_next()     -> 任一子任务退出 => 引擎fatal，退出
  3. event_rx.recv()             -> 调度器吐了 TokenEvent => dispatch_burst
  4. input.recv()                -> vllm-server 来了请求帧 => handle_message
退出清理：把所有在途请求的 abort 原子量置位（调度器下次 emit 时退役它们），
         drop 发送端，abort 全部子任务。
```

`biased` 关键字让分支按书写顺序判定——停机检查永远优先于新工作，这是优雅关停的确定性来源。

#### 4.2.3 源码精读

**请求帧解析（`handle_message`，[bridge.rs:172-229](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L172-L229)）**。每条 ZMQ 消息严格两帧：`[类型帧, msgpack 载荷帧]`。类型只有三种（[bridge.rs:187-227](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L187-L227)）：

- **Add**：载荷反序列化为 `EngineCoreRequest`，走 `start_request`；
- **Abort**：载荷是 `Vec<String>` 请求 id 列表，逐个拆除本地状态并置 abort 位。注意 [bridge.rs:207-214](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L207-L214) 的细节：已经吐过 token 的请求 abort 原因是 `Cancelled`（客户端主动取消流），还没吐过第一个 token 的是 `Disconnected`——u3-l1 讲过的反应式退役在这里落地；
- **Utility**：探活/控制类调用，[bridge.rs:852-879](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L852-L879) 一律用最小合法值应答（`is_sleeping`/`is_paused`/`reset_prefix_cache` 回 `false`，其余回 `Nil`）。

**Add → 契约请求（`start_request`，[bridge.rs:231-351](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L231-L351)）**。这是一条校验漏斗，任何一步不过都发一条 `FinishReason::Error` 的终态输出（请求被拒也必须有始有终）：

```text
缺 prompt_token_ids        -> 终态 Error
缺 sampling_params         -> 终态 Error
unsupported_request_params -> 终态 Error     （wire.rs 的参数门禁，见 4.4）
lora xarg 非法             -> 终态 Error + StopReason::Text
全部通过：
  tag = request_id；abort_reason = Arc<AtomicU8>
  token_tx = TokenSink::new(tag, event_tx, abort_reason)   # 挂进共享频道
  handle.submit(GenerateRequest { prompt_tokens, params: convert_sampling(...),
                   max_tokens, lora_adapter, kv_transfer_params, token_tx, ... })
  streams.insert(tag, RequestStreamState { abort_reason, trace_root, stop_sentinel_id })
```

关键点：`TokenSink` 是在桥里铸造、随 `GenerateRequest` 递给调度器的（[bridge.rs:311-313](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L311-L313)），调度器朝它 emit；`streams` 表以同一个 tag 索引，是 demux 的本地半边。另外 [bridge.rs:322-328](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L322-L328) 在 submit 前开 fastrace 根 span——tracing 关闭时用 `Span::noop()` 避免每请求分配，这是性能敏感路径上的惯用手法。

**burst 分桶（`dispatch_burst`，[bridge.rs:399-465](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L399-L465)）**。被 `event_rx.recv()` 唤醒后，先把频道里已就绪的整批事件（`first` + 循环 `try_recv`）按 tag 分桶，**保持首次出现顺序**（输出确定性），再把每请求的事件折叠成至多一条 `EngineCoreOutput`，整批作为**一条** `EngineCoreOutputs` 发出——把过去每请求每步 N 条 ZMQ 消息坍缩成一条（[bridge.rs:399-403](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L399-L403) 的 doc 写明了这个动机）。找不到 stream 表项的事件直接丢弃——那是已 abort/已完成请求的迟到事件。

**事件折叠（`reduce_request`，[bridge.rs:473-584](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L473-L584)）**。把 u3-l1 的七种 `TokenEvent` 折进一条 wire 输出：

| TokenEvent | 折叠去向 |
|---|---|
| `Scheduled` | 暂存为 first-token 元数据（Queued/Scheduled 两个 wire 事件 + PrefillStats），随**首个**真正的输出一次性发出 |
| `Token` | 追加进 `new_token_ids`，logprob 转成 wire 的 `PositionLogprobs` |
| `PromptTokens` | legacy 模型线在准入期就拒绝 prompt-logprob 请求，此处为空分支（[bridge.rs:526-528](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L526-L528)） |
| `KvTransfer` | 暂存，搭下一班输出车（P/D 分离的交接元数据） |
| `Finished` | 终态 + finish_reason；**Stop 时补发 stop sentinel**（见下） |
| `Error` / `Rejected` | 终态 Error + `StopReason::Text(原因)`（拒绝≠正常完成） |

**stop sentinel 协议 token**（[bridge.rs:532-550](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L532-L550)）是两桥共享的最微妙翻译：PegaInfer 引擎吐 token 前会**压掉 EOS**，而 vLLM 的文本解码器对 `FinishReason::Stop` 的输出会**无条件删掉最后一个 token**（它假设那是 EOS）。若不补，投机解码一步提交 `[可见token, EOS]` 时可见 token 会被前端误删。所以桥在 Stop 终态时把 `stop_sentinel_id`（EOS 或首个显式 stop token，取法见 [bridge.rs:586-588](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L586-L588)）追加进 token 列表，专门给解码器删。

**指标发布**。[bridge.rs:593-604](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L593-L604) 把 `SchedulerMetrics` 快照翻译成 wire 的 `SchedulerStats`（running/waiting 数、KV 使用率 = 已用块/总块）；[bridge.rs:629-651](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L629-L651) 的 `SpecDecodeTracker` 把传输层的**累计**计数器差分成 wire 要的**区间增量**（零 draft 区间直接丢，避免前端接受率日志除零）；[bridge.rs:659-686](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L659-L686) 是推送循环：先发一次当前快照让 Prometheus gauge 初始化，之后每次 watch 变化发一条 stats-only 输出批。

#### 4.2.4 代码实践

**实践目标**：不碰 socket，直接验证 demux 的折叠行为。

1. 通读 [pegainfer-frontend/src/vllm/bridge/tests.rs:1-60](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/tests.rs#L1-L60)：`Demux` 测试骨架就是 `run` 循环去掉 socket——注册请求、朝共享频道打带标签事件、一次排一个 burst、检查折叠出的 `EngineCoreOutputs`。文件头注释说明这些测试钉的是「HTTP 层结构上看不到」的行为，且**完整 HTTP→ZMQ→桥链路由 sim 的 `frontend_e2e` 集成测试把守**。
2. 精读 [`burst_batches_multiple_requests_into_one_message`](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/tests.rs#L408-L447)（两请求一批一条消息）与 [`stop_output_appends_eos_for_vllm_decoder`](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/tests.rs#L176-L202)（sentinel 补发）。
3. 运行（纯 CPU）：

   ```bash
   cargo test --release -p pegainfer-frontend --lib vllm::bridge::tests -- --nocapture
   ```

**需要观察的现象**：demux 系列测试（分桶、sentinel、first-token 元数据只随首输出发一次、abort 丢弃迟到 token、stats-only 批）全部通过。**预期结果**：13 个测试通过（`token_and_finish_in_one_burst_coalesce`、`aborted_request_drops_late_tokens`、`spec_stats_are_per_interval_deltas_that_skip_idle_intervals` 等，含两个 `#[tokio::test]`）——待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `dispatch_burst` 要保持「首次出现顺序」而不是用 `HashMap` 迭代序？

答案：wire 输出的顺序会变成 HTTP 侧可见的行为；`HashMap` 迭代序随机，同一批事件会产出顺序不定的输出批，破坏确定性与可测试性。保序让每请求事件保持到达序、批内请求保持首次出现序。

**练习 2**：legacy 桥退出时为什么不能直接 `drop(event_tx)` 了事，而要先给每个在途请求置 abort？

答案：调度器还在朝共享频道 emit；没人排水的 unbounded channel 只会积压（u3-l1：提交/事件频道故意无背压）。置 abort 位让调度器在下次 emit 时自己退役请求（反应式），之后桥才安全消失。

**练习 3**：`connect_link` 里 `wait_for_ipc_endpoint` 等的是什么？谁是 socket 的 bind 方？

答案：等 `ipc://` 路径出现在文件系统上（20ms 轮询，[bridge.rs:941-955](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L941-L955)）。bind 方是 `vllm-server`（它在 `Bootstrapped` 模式下创建这对端点），桥是 connect 方——先有端点文件才能 connect，这个等待消解了「HTTP 与引擎并行启动」的竞态。

### 4.3 vllm::bridge::stepped —— SteppedEngineBridge（新桥）

#### 4.3.1 概念说明

[stepped.rs:1-8](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L1-L8) 的模块 doc 一句话定调：**legacy 桥把每 token 事件流重新折叠成 burst 输出，新桥把每条 `StepOutputs` 1:1 翻译成一条 `EngineCoreOutputs`**。原因在 u3-l2 已经建立：step 契约里调度器已经把一步批好了——每个被触碰请求恰好一条扁平 `RequestUpdate`，本来就是「每请求折叠」的成品。桥只剩纯翻译工作：补 wall-clock 时间戳、修 EOS 语义、维护请求名册。

两个新的小问题也随之出现：

1. **请求身份的双重表示**。wire 上是字符串 `request_id`，契约内是 `RequestId`（由 `RequestControl` 持有）。abort 帧只带字符串，所以桥要维护 `names: HashMap<String, RequestId>` 反查表。
2. **时间戳的时钟域转换**。契约里的 `ScheduledInfo` 用单调钟 `Instant`（防系统时间跳变），wire 要 Unix 浮点秒。`UnixAnchor` 在桥启动时同时读两个钟，之后所有换算共享这一个锚点。

#### 4.3.2 核心流程

```text
run():
1. scheduler.take_steps() 拿 StepReceiver（tokio mpsc；只能拿一次，重复拿报错）
2. connect_link(..., metrics_watch = None)   # 注意：不挂推送式 stats 子任务
3. 立刻发一条 stats-only 批（让 Prometheus gauge 在任何流量前初始化）
4. 建 UnixAnchor；streams: HashMap<RequestId, SteppedStream>；names: HashMap<String, RequestId>
5. biased select 四路（与 legacy 完全同构）：
   shutdown / child_tasks / steps.recv() => dispatch_step / input.recv() => handle_message
6. 退出：所有在途请求 control.abort()；drop 发送端；abort 子任务
       （分区句柄随之消失 => 提交频道断开 => driver 排空退出 => mod.rs 收割线程）

dispatch_step(step):  # 一条 StepOutputs -> 至多一条 EngineCoreOutputs
  for update in step.updates:
    streams 里没有该 id => 丢弃（已 abort/已完成的迟到更新）
    reduce_update(state, update, anchor) -> (Option<output>, terminated)
    terminated => 移除 streams/names 表项，记入 finished_requests
  有输出 => 一条 RequestBatchOutputs（outputs + finished_requests + scheduler_stats）
  无输出但有 spec-decode 增量 => 单独发一条 stats-only 批（否则增量搁浅到
  永远不来的下一班）
```

**stats 的 pull-at-send 差异**值得单独说：[stepped.rs:83-86](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L83-L86) 注释解释了为何不传 watch——stepped 契约的 driver 是忙轮询（u3-l2），每次自旋都会变成一次 watch 变化，推送式发布会把每 spin 变成一条消息。所以新桥在**发批时**才读 `scheduler.metrics()`（[stepped.rs:185-192](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L185-L192)），空转引擎零消息。legacy 引擎的调度器空闲时会停车，watch 节奏有界，推送才合理（[bridge.rs:786-789](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L786-L789) 的注释正是两边对照的出处）。

#### 4.3.3 源码精读

**Add → 契约提交（`start_request`，[stepped.rs:315-421](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L315-L421)）**。校验漏斗与 legacy 逐字相同（缺字段、unsupported 参数、LoRA xarg），分叉在最后：不再铸造 `TokenSink`，而是

```rust
let control = self.scheduler.submit(Request {
    prompt_tokens,
    params: convert_sampling(&sampling_params),
    max_tokens: sampling_params.max_tokens as usize,
    lora_adapter,
    kv_transfer_params,
    logprobs: requested_logprobs(&sampling_params),
    prompt_logprobs: requested_prompt_logprobs(&sampling_params),
    trace_parent,
    client_label: Some(Arc::from(request_id.as_str())),
});
names.insert(request_id.clone(), control.id());
streams.insert(control.id(), SteppedStream::new(request_id, control, trace_root, stop_sentinel_id));
```

（[stepped.rs:403-419](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L403-L419)）`submit` 返回的 `RequestControl` 是请求的**控制柄**（u3-l2：abort 是布尔旗标不是拆频道），整个存进 `SteppedStream`——对比 legacy 把 `Arc<AtomicU8>` 塞给 `TokenSink`，控制权模型从「共享原子量」变成「句柄方法调用」（`RequestControl::abort` 见 [request_lifecycle.rs:195](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/request_lifecycle.rs#L195)）。

**Abort 的反查路径（[stepped.rs:285-300](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L285-L300)）**：`names.remove(&request_id)` 拿到 `RequestId` → `streams.remove(&id)` 拿到状态 → `state.control.abort()`。先删表再置位的顺序与 legacy 同理：飞行中的更新找不到表项自然丢弃。

**每请求翻译（`reduce_update`，[stepped.rs:510-608](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L510-L608)）**。输入是**一条结构化记录**而不是事件序列，代码因此从「循环折叠」变成「逐字段搬运」：

- `update.scheduled` → 暂存 Queued/Scheduled wire 事件（时间经 `anchor.unix()` 换算）+ 记下 prompt_tokens（[stepped.rs:515-527](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L515-L527)）；
- `update.cached_tokens` → 更新缓存命中数（分块 prefill 时它可能晚于 `Scheduled` 到达，所以 `take_prefill_stats` 惰性构建，[stepped.rs:485-503](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L485-L503)，维持上游不变式「computed + cached == prompt」）；
- `update.prompt_echo` → `to_wire_prompt_logprobs`；**格式非法时不再丢字段而是杀请求**（`fail_prompt`：置 abort + 终态 Error，[stepped.rs:469-482](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L469-L482)）——漏掉一个本该有分数的位置会让后续所有 offset 错位，宁可失败；
- `update.tokens` + `update.logprobs` → `new_token_ids` + positions；
- `update.terminal` 三变体：`Finished`（Stop 时同样补 stop sentinel，[stepped.rs:559-573](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L559-L573)）、`Rejected`（类型化原因到此降级成字符串——wire 只承载字符串，[stepped.rs:574-582](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L574-L582)）、`Failed`。

**UnixAnchor（[stepped.rs:610-633](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L610-L633)）**：启动时同刻读 `SystemTime`（`sys`）与 `Instant`（`instant`），之后 `unix(t) = sys ± (t - instant)`。所有 wire 时间戳共享同一时钟基，桥内自洽。

#### 4.3.4 代码实践

**实践目标**：验证新桥「结构化记录翻译」的三个行为——prompt-echo 单独成输出、非法 prompt 分数杀请求、拒绝的降级渲染。

1. 精读 [stepped.rs:751-792](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L751-L792) 的测试 `prompt_only_flushes_and_malformed_prompt_aborts_its_request`：用 `scheduler_pair()` 造一对真 handle/backend，`bridge.start_request` 走真实校验漏斗（带 `prompt_logprobs: Some(2)`），再手工构造 `RequestUpdate` 喂 `reduce_update`。
2. 运行（纯 CPU，前端 crate 的 lib 测试不依赖 GPU）：

   ```bash
   cargo test --release -p pegainfer-frontend --lib vllm::bridge::stepped -- --nocapture
   ```

3. 对照运行 sim 的 spec 指标 e2e（它走完整的 HTTP→ZMQ→stepped 桥）：

   ```bash
   cargo test --release -p pegainfer-sim --test frontend_e2e \
       stepped_bridge_reports_spec_decode_counters_to_prometheus -- --nocapture
   ```

**需要观察的现象**：第一组测试断言「只有 prompt_echo 的 update 也产出一条输出（`new_token_ids` 为空、`new_prompt_logprobs_tensors` 有值）」「畸形分数触发终态 Error 且 `backend.ledger.is_aborted`」。第二组验证 SpecDecodeTracker 的差分计数最终出现在 Prometheus 端点上。**预期结果**：三测通过——待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：新桥为什么不需要（也不能要）`publish_scheduler_stats` 子任务？

答案：stepped 契约的 driver 忙轮询，负载 cell 每次 spin 都「变化」，watch 订阅边沿会每自旋触发一次，推送即消息风暴。所以改为发批时拉取（`self.scheduler.metrics()`），空转引擎零消息，且批上的 stats 与批内的 token 同源同时刻。

**练习 2**：`names` 与 `streams` 两张表为什么不能合并成一张 `HashMap<String, ...>`？

答案：wire 的字符串 id 只在请求进出时出现，契约内的 `RequestId` 才是 `StepOutputs.updates` 的键。dispatch 路径每次都要用 `RequestId` 查状态，若以字符串为键就得在每个 update 上反查字符串；两张表让高频路径（step 翻译）走 `RequestId` 直查，低频路径（abort）才走字符串反查。

**练习 3**：`take_steps()` 为什么设计成「只能成功一次」？

答案：`StepReceiver` 是多条消息流的独占消费者（内部是 tokio mpsc Receiver）。若可重复拿，两个消费者会随机瓜分 step 消息，批语义和 finished_requests 追踪全部错乱。重复调用返回 `Err`（「partition step stream already taken」，[stepped.rs:79-82](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L79-L82)）把误用变成显式失败。

### 4.4 vllm::wire —— 参数翻译与「不支持即拒绝」

#### 4.4.1 概念说明

`wire.rs` 是唯一的「词汇表翻译处」：引擎契约的 `SamplingParams`（`pegainfer-frontend/src/sampler.rs`）与 vLLM 的 `EngineCoreSamplingParams`（外部 crate）在语义上并不一一对应，两边的「缺省值表示法」也不同。翻译必须遵守两条纪律：

1. **绝不静默吞参数**。客户端发了引擎不支持的参数（penalty、seed 等），要么支持，要么在 `unsupported_request_params` 里点名拒绝——「装作支持」会产出与客户端预期不同的文本。
2. **语义对齐而非数值对齐**。例如 wire 的 `top_k: 0` 不是「top-0」而是「不限制」，契约用 `-1` 表达同一语义；浮点比较用精确相等是为了识别「客户端发了任何非默认值」（[wire.rs:114-119](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L114-L119) 的注释专门解释为什么 `#[allow(clippy::float_cmp)]` 是故意的）。

#### 4.4.2 核心流程：sampling 参数转换规则表

`convert_sampling`（[wire.rs:77-112](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L77-L112)）方向的规则汇总（wire → 契约）：

| wire 字段 | 契约字段 | 转换规则 |
|---|---|---|
| `eos_token_id` + `stop_token_ids` | `ignore_eos: bool` | `ignore_eos = (eos_token_id 为空 && stop_token_ids 为空)`。vLLM 前端把 `ignore_eos=true` 降级成 `_eos_token_id: None`，而 `_all_stop_token_ids` **总是**携带模型 EOS 集（它为 min_tokens 掩码而生）——若从后者推导会把所有 ignore_eos 请求判错，所以只认前两者（[wire.rs:78-84](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L78-L84) 注释） |
| `temperature <= 0.0` | 整组贪心快路径 | 直接返回 `temperature: 0.0, top_k: -1, top_p: 1.0, min_p: 0.0, seed: None`（[wire.rs:85-94](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L85-L94)），不逐字段拷 |
| `top_k: 0` | `top_k: -1` | wire 用 `0` 表示「不限 top-k」，契约用 `-1`；其余值转 `i32`（超界饱和到 `i32::MAX`，[wire.rs:98-102](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L98-L102)） |
| `seed` | 恒 `None` | 逐请求种子需要调度器向 `select_batch` 喂请求局部步数，落地前**有种子且 temperature>0 的请求在门禁被拒**，翻译层绝不走私一个种子（[wire.rs:105-109](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L105-L109)） |
| `min_p` / `top_p` / `temperature` | 原样透传 | `min_p` 的取值合法域 `[0,1)` 由门禁把守 |

门禁 `unsupported_request_params`（[wire.rs:121-173](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L121-L173)）拒绝的参数：负 logprobs 计数（绕过 HTTP 校验的直连客户端）、`min_p` 越界或非有限、`temperature>0 且带 seed`、非零 `frequency_penalty`/`presence_penalty`、`repetition_penalty != 1.0`、非空的 `ec_transfer_params`（本引擎没有 encoder cache connector）。返回 `Some(描述)` 即拒绝理由，`None` 即可服务。

反方向（契约 → wire）的翻译也在本文件：

- `convert_finish_reason`（[wire.rs:199-205](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L199-L205)）：`Length/Stop/Error` 三个变体一一映射；
- `to_wire_position_logprobs`（[wire.rs:19-41](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L19-L41)）：契约的 `TokenLogprob { rank, logprob, top_logprobs }` 变成 wire 的 entries——被采 token 排第一，候补按序追加（rank = 序号+1），与被采 token 同 id 的候补跳过；
- `to_wire_prompt_logprobs`（[wire.rs:43-75](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L43-L75)）：prompt 回显去掉引擎包含的、无分数的**首位 token**（vLLM 自己补），且「缺任何一个本该有分数的位置」是错误而不是截短——每个后续 offset 都会因此错位。

另有 LoRA 的私货通道：`lora_adapter_from_sampling_params`（[wire.rs:183-197](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L183-L197)）从 `extra_args` 里掏 `pegainfer_lora_adapter` xarg（常量在 [wire.rs:17](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L17)），这是 u9-l3 LoRA 链路的起点。

#### 4.4.3 源码精读

整读 `convert_sampling`（[wire.rs:77-112](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L77-L112)）：

```rust
pub(crate) fn convert_sampling(params: &EngineCoreSamplingParams) -> SamplingParams {
    // ……（ignore_eos 推导的注释，见上表）
    let ignore_eos = params.eos_token_id.is_none() && params.stop_token_ids.is_empty();
    if params.temperature <= 0.0 {
        return SamplingParams {
            temperature: 0.0,
            top_k: -1,
            top_p: 1.0,
            min_p: 0.0,
            seed: None,
            ignore_eos,
        };
    }

    SamplingParams {
        temperature: params.temperature,
        top_k: if params.top_k == 0 {
            -1
        } else {
            i32::try_from(params.top_k).unwrap_or(i32::MAX)
        },
        top_p: params.top_p,
        min_p: params.min_p,
        // 逐请求种子需要调度器向 select_batch 喂请求局部步数；落地前
        // 带种子的请求在桥里被拒绝，而不是被静默忽略。
        seed: None,
        ignore_eos,
    }
}
```

注意 `ignore_eos` 的计算在贪心分支**之前**——无论贪心与否，EOS 语义独立于采样温度。

配套单测是理解这些规则最快的路径：[`convert_sampling_honors_ignore_eos_lowering`](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L224-L241) 用三种参数组合钉死 ignore_eos 推导；[`convert_sampling_passes_min_p_and_never_seed`](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L243-L259) 钉死 min_p 透传 + 种子永不为 Some + 贪心归零；[`unsupported_sampling_params_are_refused`](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L261-L289) 逐项过门禁（包括「贪心请求带种子是无害 no-op，放行」这个边界）。

#### 4.4.4 代码实践

**实践目标**：先预测、后验证——把 4.4.2 的转换表变成可执行断言。

1. 运行 wire 的全部单测（纯 CPU）：

   ```bash
   cargo test --release -p pegainfer-frontend --lib vllm::wire -- --nocapture
   ```

2. 验证门禁的「贪心 + seed 放行」边界：读 [wire.rs:273-279](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L273-L279)，确认逻辑是 `temperature > 0.0 && seed.is_some()` 才拒绝。
3. （选做，写代码不改源码）在本地 scratch 目录写一个依赖 `pegainfer-frontend` 的小测试也做不到——`convert_sampling` 是 `pub(crate)`。所以正确姿势是**改测试参数重跑**：把 [wire.rs:224-241](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L224-L241) 里 `params.stop_token_ids = vec![42]` 改成空 `vec![]`，预测断言结果如何翻转（第三组断言 `!ignore_eos` 将失败），跑测试验证你的预测后**改回源码**（本仓库纪律：不留实验残渣）。

**需要观察的现象**：步骤 1 全绿；步骤 3 中按预测失败。**预期结果**：9 个 wire 测试通过（两个 convert_sampling、unsupported 门禁、ec_transfer、LoRA xarg、两组 logprobs 折叠、两组 prompt 回显）——待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `top_k` 不直接透传，而要做 `0 → -1` 的映射？

答案：两边对「不限制」的编码不同：vLLM wire 沿用 Python vLLM 的惯例 `top_k=0` 表示禁用 top-k 过滤；PegaInfer 的 `SamplingParams`/采样内核用负值（`-1`）表示同一语义。翻译层若不映射，`0` 会被当成「保留 0 个候选」的退化请求。

**练习 2**：一个客户端发 `temperature=0.7, seed=42`，请求的命运是什么？发 `temperature=0, seed=42` 呢？

答案：前者在 `unsupported_request_params` 被拒（[wire.rs:131-133](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L131-L133)），桥发终态 Error + `StopReason::Text("per-request seed is not supported yet")`。后者放行：贪心采样与种子无关，seed 是无害的 no-op，拒绝它反而误伤（[wire.rs:276-278](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/wire.rs#L276-L278) 的测试注释原话：「A greedy request's seed is a no-op, not a lie — allowed」）。

**练习 3**：`to_wire_prompt_logprobs` 为什么坚持「首位 token 必须无分数」否则报错？

答案：首位 prompt token 是模型输入而非模型输出，天然没有分数（引擎在 `PromptEcho.logprobs[0]` 放 `None`）。若输入数据破坏了这个布局（比如长度不匹配、中间缺分数），说明上游已经错了——截短载荷会让每个后续位置的 offset 错一位，客户端拿到的 logprob 全部张冠李戴，比直接报错糟糕得多。

## 5. 综合实践

把本讲四个模块串成一张图 + 一张表（这正是本讲规格里的原始任务）。

**任务 A：画新旧两条桥的完整数据通路图**。参考答案（对照源码逐跳可查）：

```text
                     ┌────────────────────────── 同一进程 ──────────────────────────┐
 客户端                │                                                             │
   │  POST /v1/chat/completions (SSE)                                              │
   ▼                │  ┌────────────────────────┐      vllm-server crate（外部）     │
 HTTP ◄──── SSE ────┼──┤  HTTP 路由/校验/分词/模板 │                                 │
                     │  └───────────┬────────────┘                                 │
                     │              │ EngineCoreRequest帧 [Add|Abort|Utility, msgpack]
                     │              ▼         msgpack 解码                         │
                     │      ipc:///…/input.sock   (DEALER, 桥为 connect 方)         │
                     │              │                                              │
                     │   ┌──────────┴───────────────┐                              │
                     │   │  mod.rs: engine_task 双臂  │  LaunchedEngine::?          │
                     │   └───────┬───────────┬───────┘                              │
                     │      Handle 臂      Stepped 臂                                │
                     │           │               │                                  │
                     │  LocalEngineBridge   SteppedEngineBridge                     │
                     │   · handle_message     · handle_message（同帧协议）           │
                     │   · TokenSink 入共享频道 · scheduler.submit(Request)          │
                     │   · handle.submit(      · 返回 RequestControl                │
                     │     GenerateRequest)        │                                │
                     │           │               │  (crossbeam 提交频道, u3-l2)      │
                     │           ▼               ▼                                  │
                     │     EngineHandle      SchedulerHandle → driver 线程           │
                     │     (u3-l1 契约)      Scheduler::step (u3-l2 契约)            │
                     │           │               │                                  │
                     │   TokenEvent 流        StepOutputs (每步一条消息)              │
                     │   (共享频道, 按 tag)    (扁平 RequestUpdate 列表)              │
                     │           ▼               ▼                                  │
                     │   dispatch_burst       dispatch_step (1:1)                   │
                     │   分桶→reduce_request  reduce_update                          │
                     │           └───────┬───────┘                                  │
                     │           EngineCoreOutputs (msgpack 编码)                    │
                     │                   ▼                                          │
                     │      ipc:///…/output.sock  (PUSH → output_loop 发送泵)        │
                     │                   │                                          │
                     │        vllm-server 组装 OpenAI chunk                          │
                     └──────────── SSE: data: {…} … data: [DONE] ◄──────────────────┘
```

逐跳核对方法：从 [mod.rs:250-303](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L250-L303) 的双臂开始，Handle 臂沿 [bridge.rs:143-156](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L143-L156)（input.recv 分支）与 [bridge.rs:131-142](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L131-L142)（event_rx 分支）走；Stepped 臂沿 [stepped.rs:143-168](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge/stepped.rs#L143-L168) 的两个 select 分支走；汇合点是两桥共用的 `engine_output` → `output_loop` → PUSH socket。

**任务 B：列出至少三个 sampling 参数的转换规则**。参考答案即 4.4.2 的表格，任选三行均可，推荐这三条信息量最大的：

1. `ignore_eos` 的推导（wire 双字段 → 契约布尔，含 `_all_stop_token_ids` 陷阱）；
2. `top_k` 的 `0 → -1` 语义映射 + i32 饱和转换；
3. `seed` 的「翻译层恒 None + 门禁拒绝 temperature>0 的带种子请求」组合拳（以及贪心 + seed 放行的边界）。

**验证方式**：跑 `cargo test --release -p pegainfer-frontend --lib vllm`（覆盖 wire + bridge demux + stepped 三组测试）与 `cargo test --release -p pegainfer-sim --test frontend_e2e`（覆盖整条 HTTP→ZMQ→桥→SSE 链路）。全部纯 CPU，无需 GPU 与权重。本环境未执行，待本地验证。

## 6. 本讲小结

- PegaInfer 不写 HTTP 层：OpenAI 路由、分词器、chat 模板、SSE、Prometheus 全在 git 依赖钉死的外部 `vllm-server` 等 crate 里；本仓库的 `vllm` 模块只在进程内用 ZeroMQ IPC「扮演」一个 vLLM EngineCore 进程，完成 `EngineCoreReadyResponse` 握手后即可被 `vllm-server` 当真引擎驱使。
- `mod.rs` 是总装车间：HTTP 与引擎加载并行启动（引擎以 future 传入），端口 bind 晚于桥注册（端口可达 = 引擎就绪），`LaunchedEngine` 的 `Handle`/`Stepped` 双臂决定给每个引擎身份挂 `LocalEngineBridge` 还是 `SteppedEngineBridge`，分区数不符整体拒启。
- 传输层两桥共享（`connect_link`/`BridgeLink`/`output_loop`）：DEALER 收请求帧、PUSH 发输出帧、msgpack 编码、两帧式 `[类型, 载荷]` 协议（Add/Abort/Utility 三种请求帧）。
- legacy 桥的核心是 demux：一条共享 `TokenEvent` 频道按 tag 分桶、保序折叠、整批一条 `EngineCoreOutputs`；stepped 桥只是 1:1 翻译器（每条 `StepOutputs` → 一条 wire 消息），外加 `names` 反查表处理字符串 id 的 abort、`UnixAnchor` 做单调钟→墙钟换算、pull-at-send 的指标发布。
- 两桥共享两个微妙协议修补：Stop 终态补发 stop sentinel token（PegaInfer 压 EOS、vLLM 解码器删末 token 的语义对冲），以及「不支持即拒绝」的参数门禁（penalty/seed/ec_transfer 点名拒绝，绝不静默吞）。
- `wire.rs` 是唯一词汇表翻译处：`ignore_eos` 从 `eos_token_id`+`stop_token_ids` 推导（绝不用 `all_stop_token_ids`）、`top_k` 的 `0→-1` 映射、贪心快路径整组归零、种子永不出现在契约侧。

## 7. 下一步学习建议

- **下一讲 u3-l4「一个请求的完整旅程」**：把 u3-l1/u3-l2/u3-l3 串成端到端链路，从 `POST /v1/chat/completions` 一路追到 `TokenEvent`/`StepOutputs` 回流 SSE。建议先自己做一遍 4.1.4 的 e2e 测试再读那一讲。
- 通读 [docs/subsystems/frontend/frontend-architecture.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docs/subsystems/frontend/frontend-architecture.md)：它是前端边界的权威叙述，本讲的「两桥并存」在其中定位为「migration pending」——终点是所有模型线迁移到 step 契约后删除 legacy 契约与 legacy 桥。
- 对指标链路感兴趣的读者可以预习 [docs/subsystems/frontend/prometheus-metrics.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docs/subsystems/frontend/prometheus-metrics.md)，u10-l4 会从 `SchedulerMetrics` 一路讲到 dashboard。
- LoRA 的 xarg 私货通道（本讲 4.4 的 `pegainfer_lora_adapter`）在 `vllm/lora.rs` 展开，u9-l3 精讲。
