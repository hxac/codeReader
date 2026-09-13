# 旧契约：EngineHandle 与 TokenEvent 流

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 legacy（旧一代）引擎契约的五个组成部分——`GenerateRequest`、`TokenEvent`、`TokenSink`、`KvPrefix`、`EngineHandle`——各自解决什么问题。
- 理解 `EngineHandle` 的多分区提交路由：请求绑定的 rank 优先、前缀解析绑定的 rank 次之、未绑定请求走「运行中 + 4×等待」的负载估计。
- 明白 `TokenSink` 的 abort 机制如何用「共享频道 + 每请求中止原因」支持单个请求的取消，而不打扰其他请求。
- 能追踪一次 `submit` 从前端到调度器的完整路径，包括前缀命中路由和不可路由拒绝路径。

## 2. 前置知识

本讲是单元 3 的第一讲，直接承接 u2-l2 的 `ModelLine` 协议。先复述两个已知结论，再补充本讲需要的新概念。

**承接 u2-l2**：`ModelLine::launch` 返回 `LaunchedEngine`，它是一个迁移期的二选一枚举——`Handle(EngineHandle)`（本讲的旧契约）或 `Stepped(Engine)`（u3-l2 的新契约）。当前 qwen3、gemma4、pegainfer-sim 已经迁移到新契约；glm52、qwen35、kimi-k2、deepseek-v2-lite 仍通过 `LaunchedEngine::Handle` 启动。

**什么是「契约」**：前端 crate（`pegainfer-frontend`）与模型 crate 之间的类型接缝。前端只认契约类型，不认识任何模型结构体；契约里也不允许出现 CUDA 类型。这样 server/HTTP/协议栈与 GPU 执行可以独立演进。

本讲需要的基础概念：

- **mpsc 无界通道**（`tokio::sync::mpsc::unbounded_channel`）：多生产者单消费者的队列。「无界」指不设容量上限——本项目的刻意选择：准入控制是调度器的职责（用 `Rejected` 事件表达），绝不用提交端的背压（阻塞提交）来表达。
- **watch 通道**（`tokio::sync::watch`）：只保留「最新一个值」的通道。发送端每次写覆盖旧值，接收端随时 `borrow()` 读快照。适合传播「负载」这类状态量而非事件流。
- **demux（多路分解）**：一条共享频道上混流着所有请求的事件，接收端按标签（tag）把它们拆回各自请求的过程。
- **`Arc<str>`**：线程安全的字符串引用计数指针。克隆它只是引用计数加一，不复制字符串内容。
- **`AtomicU8` 与内存序**：一个原子字节。`store(Release)` / `load(Acquire)` 配对保证：写端在 store 之前的所有写入，对读端在 load 之后的读取可见。本讲里它承载「请求中止原因」。
- **RAII hold**：用一个值的生命周期表达资源占用——值被 drop（离开作用域）时自动释放资源。`KvPrefix` 里的 hold 就是这样一块「看不见但 drop 时生效」的防驱逐钉子。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [pegainfer-frontend/src/engine/mod.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/mod.rs) | engine 模块的总目录：声明新旧两代契约的文件划分 |
| [pegainfer-frontend/src/engine/request.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/request.rs) | `GenerateRequest`：一次生成请求的全部输入 |
| [pegainfer-frontend/src/engine/event.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/event.rs) | `TokenEvent`：七个输出事件变体 + `RequestTag` |
| [pegainfer-frontend/src/engine/sink.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/sink.rs) | `TokenSink`：每请求的发送句柄 + abort 原因 |
| [pegainfer-frontend/src/engine/kv.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/kv.rs) | `KvPrefix` 前缀解析、`SubmittedRequest`、`KvCapacity` |
| [pegainfer-frontend/src/engine/handle.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/handle.rs) | `EngineHandle`：分区路由、负载馈送、线程生命周期 |
| [pegainfer-frontend/src/engine/wiring.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs) | `LaunchedEngine` 枚举：新旧契约的汇合点 |
| [pegainfer-frontend/src/vllm/bridge.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs) | `LocalEngineBridge`：旧契约的消费者（demux 与 abort 的另一端） |
| [pegainfer-frontend/src/engine/metrics.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/metrics.rs) | `SchedulerMetrics`：路由打分的数据来源 |
| [pegainfer-deepseek-v2-lite/src/scheduler.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-deepseek-v2-lite/src/scheduler.rs) | 一条仍走旧契约的模型线，作为 `TokenEvent` 发送时机的实物样本 |

## 4. 核心概念与源码讲解

先看全模块的「户口本」——engine/mod.rs 的模块文档把新旧两代契约的文件划分写得非常清楚：

[pegainfer-frontend/src/engine/mod.rs:L21-L26](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/mod.rs#L21-L26) 声明：legacy 逐 token 契约由 `request`/`event`（请求与事件流）、`sink`（共享 tagged 频道上的每请求发送句柄）、`kv`（KV 前缀解析）、`handle`（路由、负载馈送、关停）四组文件构成，保留到所有模型线迁移完毕为止。

一句话概括旧契约的数据流：

```
前端 bridge                     EngineHandle                     模型调度器线程
    │  submit(GenerateRequest) ──►  按 rank 路由选择分区通道 ──►  (GenerateRequest, KvPrefix)
    │                                                                    │
    │ ◄── 共享频道上的 (RequestTag, TokenEvent) 流 ◄── TokenSink.send ────┘
    │  (bridge 的 demux 循环按 tag 拆回各请求)
```

### 4.1 engine::request：GenerateRequest，一次生成的全部输入

#### 4.1.1 概念说明

`GenerateRequest` 是前端交给引擎的「一单活」：prompt 是哪些 token、采样参数是什么、最多生成多少、结果发到哪。它是一个纯数据结构，没有任何 GPU 类型——这就是「契约不含 CUDA」的具体体现。

#### 4.1.2 核心流程

- 协议栈（vLLM 桥）收到 HTTP 请求 → 解码出 prompt token 与采样参数 → 组装 `GenerateRequest`（其中 `token_tx` 指向本请求的 `TokenSink`）→ 交给 `EngineHandle::submit`。
- 字段可分四组：身份（`request_id`、`queued_at_unix_s`、`trace_parent`）、路由（`data_parallel_rank`）、生成内容（`prompt_tokens`、`params`、`max_tokens`、`lora_adapter`、`kv_transfer_params`、`logprobs`、`prompt_logprobs`）、回执地址（`token_tx`）。

#### 4.1.3 源码精读

[pegainfer-frontend/src/engine/request.rs:L4-L32](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/request.rs#L4-L32) 定义了整个结构体。几个值得逐条读的字段注释：

- `trace_parent`（L14）：调用方请求 span 的追踪上下文。调度器把自己的 queue/prefill/decode span 挂在它下面，host 侧阶段耗时就能附到前端开启的同一条 trace 上；追踪关闭时为 `None`，调度器完全跳过 span 工作。`SpanContext` 是 `Copy` 的，所以能随调度器 `Clone` 请求状态一起走。
- `data_parallel_rank`（L18）：前端选定的逻辑数据并行 rank；`None` 表示交给 handle 放到最空闲分区（等待请求权重 4×）——这是 4.5 节路由逻辑的输入。
- `token_tx`（L29）：调度器发出本请求 `TokenEvent` 的地方。注释点明：同一引擎的所有请求共享一条 tagged 输出频道，前端按 tag 多路分解。

真实填充现场在 vLLM 桥里：[pegainfer-frontend/src/vllm/bridge.rs:L329-L344](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L329-L344) 把 wire 格式的 `EngineCoreRequest` 翻译成 `GenerateRequest` 并 `submit`。注意 L334：桥固定填 `data_parallel_rank: Some(self.engine_index as usize)`——每个 DP 分区各有一个桥实例，各自把请求钉在自己的分区上。

#### 4.1.4 代码实践

**实践目标**：把 `GenerateRequest` 的 12 个字段与 wire 请求的来源一一对应。

**操作步骤**：

1. 打开 [bridge.rs:L231-L351](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L231-L351) 的 `start_request`，通读一遍。
2. 对每个字段回答：「它来自 `EngineCoreRequest` 的哪个字段 / 哪个辅助函数 / 桥自己捏造？」例如 `params` 来自 `convert_sampling(&sampling_params)`，`stop_sentinel_id` 来自 `stop_sentinel_id(eos_token_id, stop_token_ids)`。
3. 特别留意 L311-L313：`tag`、`abort_reason`、`token_tx` 三件套是如何同时创建、分别去向何方的。

**需要观察的现象**：`abort_reason` 这个 `Arc<AtomicU8>` 既进了 `TokenSink`（调度器手里），又留在 `RequestStreamState`（前端手里）——同一个原子的两端共享，这就是取消机制的物理基础。

**预期结果**：一张 12 行的字段来源表。此实践为纯源码阅读，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `prompt_tokens` 是 `Vec<u32>` 而不是字符串？

**答案**：分词（tokenize）发生在前端以北的 vLLM 协议栈里，到达契约层时文本已经变成 token id 序列；引擎从头到尾只处理 id，不需要词表反过来查文本。

**练习 2**：`request_id` 为什么是 `Option`？谁有权利不填？

**答案**：契约不强制业务身份——直接驱动引擎的场景（基准测试、集成测试、模拟器）没有 vLLM 的 request_id 概念，`TokenSink::standalone` 的样例路径就不需要它；事件回路由 `RequestTag`（sink 自己持有的 tag）承担，与这个字段无关。

### 4.2 engine::event：TokenEvent，七个变体的输出词汇表

#### 4.2.1 概念说明

引擎对一次请求的全部「说话内容」都通过 `TokenEvent` 表达。它是前端唯一能听见的引擎语言：排队了、出 token 了、结束了、出错了、被拒了。七个变体按惯例排成一条时间线：`Scheduled`（至多一次，最先）→ 可选的 `PromptTokens` / `KvTransfer` → 零或多个 `Token` → 恰好一个终态（`Finished` / `Error` / `Rejected`）。

#### 4.2.2 核心流程

一个正常请求的事件序列：

```
Scheduled(队列耗时, prompt 长度, 缓存命中数)
  └─► Token(id, logprob?) × N        ← 每个解码步一枚
        └─► Finished(Length|Stop, prompt 数, completion 数)
```

异常出口有两类：调度器拒单（`Scheduled` 后紧跟 `Rejected`，例如上下文超限）、执行出错（`Error`）。`PromptTokens` 与 `KvTransfer` 是两个「搭车」变体：前者回传 prompt 的逐 token logprobs，后者透传 P/D（prefill/decode 分离）交接元数据。

#### 4.2.3 源码精读

[pegainfer-frontend/src/engine/event.rs:L18-L54](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/event.rs#L18-L54) 定义七个变体：

| 变体 | 行号 | 载荷 | 发出时机（以 deepseek-v2-lite 为实物样本） |
|------|------|------|------|
| `Scheduled` | L20-27 | 排队/调度时间戳、prompt 数、缓存命中数 | 准入那一刻。见 [scheduler.rs:L653-L674](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-deepseek-v2-lite/src/scheduler.rs#L653-L674) 的 `send_scheduled`：先发它，发失败（消费者已走）则连后续终态都不发 |
| `Token` | L28-31 | token id、可选 logprob | 每个解码步。见 [scheduler.rs:L575-L581](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-deepseek-v2-lite/src/scheduler.rs#L575-L581) |
| `PromptTokens` | L32-35 | prompt 的 id 与逐位 logprobs | 客户端请求 `prompt_logprobs` 时回传。契约保留了它，消费端在 [bridge.rs:L526](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L526) 的 demux 与多条 e2e 测试的匹配臂中；当前树中的生产调度器没有发送点（历史上的发送方 qwen3 已迁移到 step 契约） |
| `KvTransfer` | L38 | 不透明 JSON | P/D 交接时。实物样本：[glm52/src/scheduler/mod.rs:L1446](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-glm52/src/scheduler/mod.rs#L1446) |
| `Finished` | L39-43 | 终因 + 双侧计数 | EOS 命中（[scheduler.rs:L562-L566](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-deepseek-v2-lite/src/scheduler.rs#L562-L566)，`Stop`）或达到 `max_tokens`（[scheduler.rs:L604-L608](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-deepseek-v2-lite/src/scheduler.rs#L604-L608)，`Length`） |
| `Error` | L44-48 | 消息 + 双侧计数 | 执行失败。见 [scheduler.rs:L614-L631](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-deepseek-v2-lite/src/scheduler.rs#L614-L631) 的 `emit_error` |
| `Rejected` | L49-53 | 消息 + 双侧计数 | 准入被拒。见 [scheduler.rs:L146-L162](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-deepseek-v2-lite/src/scheduler.rs#L146-L162)：拒绝也先发 `Scheduled` 再发 `Rejected` |

配套类型：[event.rs:L11-L16](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/event.rs#L11-L16) 的 `FinishReason { Length, Stop, Error }`；[event.rs:L59](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/event.rs#L59) 定义 `RequestTag = Arc<str>`——注释说明用 `Arc<str>` 是让每次事件打标签只花一次引用计数自增，而不是一次字符串拷贝；[event.rs:L62-L68](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/event.rs#L62-L68) 的 `unix_now_s()` 是全契约统一的时间戳基准（UNIX 纪元起的秒数，f64）。

#### 4.2.4 代码实践

**实践目标**：用真实的调度器代码验证「七变体时间线」不是纸面约定。

**操作步骤**：

1. 打开 deepseek-v2-lite 的 [scheduler.rs:L548-L612](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-deepseek-v2-lite/src/scheduler.rs#L548-L612) `emit_token_or_finish`，注意它同时覆盖三条出口：EOS→`Finished(Stop)`、普通 token→`Token`、达到上限→`Finished(Length)`。
2. 找到 L582-L591：`Token` 发送失败（`is_err`）时函数直接返回 `true` 表示请求终结——这就是「发送失败即取消」的语义（消费者没了，调度器顺势退场）。
3. 再读 [scheduler/tests.rs:L194-L219](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-deepseek-v2-lite/src/scheduler/tests.rs#L194-L219)，看测试如何断言「`Scheduled` 之后紧跟 `Rejected`/`Finished`」的顺序。

**需要观察的现象**：测试对事件顺序的断言（先 `Scheduled` 再终态）与 4.2.2 的时间线完全一致。

**预期结果**：确认时间线在真实代码里有逐点对应。纯阅读实践，无需 GPU。

#### 4.2.5 小练习与答案

**练习 1**：`Rejected` 与 `Error` 都是「没善终」，语义差别是什么？

**答案**：`Rejected` 是准入层拒绝——请求从未进入执行（`completion_tokens` 恒为 0，且惯例上仍先发 `Scheduled`）；`Error` 是执行中途失败——可能已经生成了一部分 token，双侧计数如实上报。

**练习 2**：为什么 `Scheduled` 里要带 `cached_tokens`？

**答案**：让前端能区分「排队慢」和「prompt 大」。前缀缓存命中时实际要预填充的 token 变少，TTFT 的构成随之变化；不区分会误导延迟归因（u7-l3 前缀缓存讲义会用到这个字段）。

### 4.3 engine::sink：TokenSink，共享频道上的按请求回执与取消

#### 4.3.1 概念说明

`TokenSink` 是调度器手里「本请求的发话器」。它解决的问题：N 个并发请求如果各开一条频道、各配一个消费者任务，每步要唤醒 N 个沉睡的消费者；改成所有请求共享一条 tagged 频道、单一 demux 循环消费，每步只需约 1 次唤醒。取消（abort）也随之搬家：从「drop 掉每请求的接收端让发送失败」变成「设置该请求的共享中止原因」，单请求取消不再影响同频道的其他请求。

#### 4.3.2 核心流程

发送侧（调度器）每次 `send(event)`：

```
send(event):
  if abort_reason != None:        ← 该请求已被前端中止
      return Err(event)           ← 调度器读作"消费者没了，退役该请求"
  tx.send((tag.clone(), event))   ← 打标签上共享频道
      └─ 失败（整个 demux 没了）→ 同样 Err
```

取消侧（前端 demux）收到 vLLM 的 Abort 帧时，先移除自己的流状态，再把中止原因写进那个共享 `AtomicU8`；调度器下一次 `send`/`is_closed` 就会发现并退役请求。`Release` 写 / `Acquire` 读的配对保证「先移除状态、后置原因」的顺序对调度器可见。

#### 4.3.3 源码精读

[pegainfer-frontend/src/engine/sink.rs:L15-L16](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/sink.rs#L15-L16) 定义频道类型别名：载荷是 `(RequestTag, TokenEvent)` 元组。

[pegainfer-frontend/src/engine/sink.rs:L36-L40](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/sink.rs#L36-L40) 是结构体本体：`tag`（回执路由标签）、`tx`（共享频道发送端）、`abort_reason`（与其他端共享的原子字节）。其上方 L10-L34 的文档注释值得整段精读——它解释了从「每请求一条频道」到「一条共享 tagged 频道」的演化动机，以及取消语义如何保持与旧机制「反应式退役」等价。

[pegainfer-frontend/src/engine/sink.rs:L56-L69](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/sink.rs#L56-L69) 是核心方法对：`send` 先查中止原因再上频道；`is_closed` 是「该请求被中止 **或** 整个共享接收端没了」的析取。注释点明：`tx.is_closed()` 是引擎级信号（整个 demux 消失），每请求的信号是中止原因——两级故障要分开。

[pegainfer-frontend/src/engine/sink.rs:L73-L81](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/sink.rs#L73-L81) 提供两个细分查询：`is_cancelled`（流已开始后前端显式取消）与 `is_disconnected`（首个输出到达客户端前就断连）。区分它们的是前端桥：[bridge.rs:L207-L214](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L207-L214) 在处理 Abort 帧时按 `has_emitted_tokens` 决定写哪个原因——注释解释了为什么以「首个 token 是否抵达客户端」为界：`Scheduled` 元数据可以随首个输出一起冲刷，但不能证明客户端见过 token。

[pegainfer-frontend/src/engine/sink.rs:L108-L127](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/sink.rs#L108-L127) 定义 `RequestAbortReason { None=0, Cancelled=1, Disconnected=2 }` 及其 `from_raw`/`store`（`Release` 序）。未知原始值一律回落 `None`，保证向前兼容。

[pegainfer-frontend/src/engine/sink.rs:L97-L105](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/sink.rs#L97-L105) 的 `standalone()` 给直接驱动方（基准、集成测试、模拟器）开私有频道，取消标志永不触发——u1-l4 里 pegainfer-sim 用的就是这类路径。

#### 4.3.4 代码实践

**实践目标**：运行 `TokenSink` 自带的三个单元测试，验证取消/断连/关闭三态判别。

**操作步骤**：

1. 运行（`pegainfer-frontend` 是无 CUDA 的纯 CPU crate，无需 GPU；首次构建需拉取 crates.io 依赖）：

   ```bash
   cargo test --release -p pegainfer-frontend --lib token_sink -- --nocapture
   ```

2. 对照三个测试读代码：[sink.rs:L134-L161](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/sink.rs#L134-L161)（`Cancelled` 置位后 `send` 返回 `Err`）、[sink.rs:L163-L179](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/sink.rs#L163-L179)（drop 接收端 ≠ 显式取消）、[sink.rs:L181-L199](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/sink.rs#L181-L199)（`Disconnected` 与 `Cancelled` 互斥可辨）。

**需要观察的现象**：三个测试全绿；第一个测试里 `rx.try_recv()` 收到的元组第一项是 `"request-a"`——标签确实随事件同行。

**预期结果**：3 passed。若本机无法拉取依赖构建，则退化为纯阅读，标注「待本地验证」即可——三个测试的断言本身已经把行为讲清楚了。

#### 4.3.5 小练习与答案

**练习 1**：调度器如何得知「该退役这个请求了」？有几种途径？

**答案**：两种，殊途同归：`send` 返回 `Err`（中止原因非 `None`，或共享接收端消失），或主动调 `is_closed()` 轮询。两者语义上等价于旧机制里「消费者 drop 了接收端」。

**练习 2**：为什么 `TokenSink` 要 `derive(Clone)`？

**答案**：调度器内部常把请求状态克隆进多处内部结构（如 active/pending 队列、trace 辅助），克隆 sink 让任何持有请求状态的代码都能发事件；`Arc` 语义保证克隆廉价且共享同一个中止原子。

### 4.4 engine::kv：KvPrefix，前缀解析与不可见的 KV 内部

#### 4.4.1 概念说明

`KvPrefix` 回答一个问题：这条请求的 prompt 前缀有多少 token 已经物化在目标 rank 的 GPU 前缀缓存里？它由 KV store 在请求进调度器**之前**产出。设计上的关键取舍：契约层完全不懂 KV 内部（本 crate 无 CUDA），所以「钉住这些块不被驱逐」的句柄是一个不透明的 `Box<dyn Any + Send>`——对契约只能 drop，只有铸造它的 `pegainfer-kv-store` 能向下转型取回。

#### 4.4.2 核心流程

```
KV store 解析前缀 ──► KvPrefix::resolved(hit_tokens, rank, hold)
                          │
EngineHandle::submit_resolved(req, kv_prefix)
                          │ 路由：hold 钉在 rank 上 → 该 rank 赢得路由
                          ▼
              (GenerateRequest, KvPrefix) 进调度器
                          │ 调度器做 match_and_add_prefix 消费命中
                          ▼
              hold 被 drop ──► 释放防驱逐钉子
```

降级不是独立状态：超时或池压力都表现为更小的 `hit_tokens`——数字本身携带全部下游语义（分离式解码的准入会拿它对账交接长度；其他场景只是从 `hit_tokens` 处开始预填充）。

另一个类型 `KvCapacity` 表达池容量：一个 \( L \) token 的请求占用 \( \lceil L / B \rceil \) 块（\( B \) 为块大小），所以批适配检查必须**逐请求向上取整再求和**——直接加总裸 token 数会少算，可能放进一个调度器只能推迟的批。

#### 4.4.3 源码精读

[pegainfer-frontend/src/engine/kv.rs:L22-L30](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/kv.rs#L22-L30) 定义结构：`hit_tokens`、`hold: Option<Box<dyn Any + Send>>`、`rank`。字段注释点明路由含义：hold 钉在**这个** rank 的块上，路由去别处会静默丢命中、白费钉子——所以 `submit_resolved` 按 `rank` 路由。

[pegainfer-frontend/src/engine/kv.rs:L35-L42](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/kv.rs#L35-L42) 与 [L47-L53](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/kv.rs#L47-L53) 是两个构造器：`none()`（未解析或降级为零，从头预填充，调度器自己的 GPU 前缀匹配照常生效）与 `resolved()`。

[pegainfer-frontend/src/engine/kv.rs:L90](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/kv.rs#L90) 定义 `SubmittedRequest = (GenerateRequest, KvPrefix)`——注释说明用元组而非包装结构是刻意的直白：store 的产出是「前缀解析」，不是一种新请求。

[pegainfer-frontend/src/engine/kv.rs:L115-L118](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/kv.rs#L115-L118) 的 `blocks_for` 用 `div_ceil` 实现整块分配的向上取整；其上方 L92-98 的注释解释了为什么必须逐请求取整。

#### 4.4.4 代码实践

**实践目标**：手算验证整块取整对批适配的影响。

**操作步骤**：

1. 假设 `KvCapacity { total_blocks: 10, block_size: 16 }`（总容量 160 token）。
2. 手算三条请求各 50 token 时的块占用：每条 \( \lceil 50/16 \rceil = 4 \) 块，合计 12 块 > 10 块——放不下。
3. 对比错误算法：\( 50 \times 3 = 150 \leq 160 \)，会误判「放得下」。
4. 在源码里确认 `blocks_for` 的实现与你手算一致。

**需要观察的现象**：两种算法结论相反——这正是 L92-98 注释警告的「summing raw token counts under-counts」。

**预期结果**：理解为什么适配检查必须逐请求取整。纯手算 + 阅读实践，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：为什么不把 hold 定义成具体类型（比如某个 KV 块守卫）？

**答案**：契约 crate 必须无 CUDA、无 KV 内部知识；具体类型会把 `pegainfer-kv-cache` 的实现细节拉进前端依赖。`Box<dyn Any + Send>` 让契约只承担「搬运 + drop」两个动作，铸造方保留向下转型的独占权（`hold_any`）。

**练习 2**：`KvPrefix::none()` 意味着「没有前缀缓存可用」吗？

**答案**：不。它只意味着「请求到达前没有跑过预解析」；调度器内部自己的 GPU 前缀匹配（如 qwen3 的 PrefixProbe）照常进行。`none()` 的字段注释原话是「prefill from scratch」相对的是 store 层解析，而非调度器层匹配。

### 4.5 engine::handle：EngineHandle，多分区路由与生命周期

#### 4.5.1 概念说明

`EngineHandle` 是前端持有的引擎门面。它解决三件事：

1. **路由**——多分区（逻辑 DP rank）引擎每分区一条提交通道，`submit` 决定进哪条；
2. **负载馈送**——每分区一个 `watch` 通道广播 `SchedulerMetrics`，既是路由打分的依据也是可观测性来源；
3. **生命周期**——最后一个 handle 克隆 drop 时关闭全部提交通道并 join 全部引擎线程。

#### 4.5.2 核心流程

`submit_resolved` 的路由是一个三分支 match，优先级从高到低：

1. 前缀已解析（`kv_prefix.rank() == Some(r)`）→ 走 `r`（hold 钉在那；若调用方还另绑了不一致的 rank，`debug_assert` 报警——那是调用方 bug）；
2. 未解析但请求自带 `data_parallel_rank == Some(b)` → 走 `b`；
3. 两者皆无 → `least_loaded_partition()`。

选出的分区若在拓扑范围内，`(req, kv_prefix)` 进该分区通道，完事。**越界不是引擎故障而是调用方错误**：handle 用请求自己的 `token_tx` 补发一对 `Scheduled → Rejected`，`submit` 仍返回 `Ok`——请求得到与调度器拒单完全相同的表现形式。

`least_loaded_partition` 的打分公式：

\[ \text{score}(p) = \text{num\_running\_reqs}(p) + 4 \times \text{num\_waiting\_reqs}(p) \]

等待请求权重 4×，与 vLLM 的 DP 负载均衡策略同款；并列时取下标最小的分区。没有负载馈送的分区记 0 分，所以单分区退化情形恒返回 0。

#### 4.5.3 源码精读

[pegainfer-frontend/src/engine/handle.rs:L52-L69](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/handle.rs#L52-L69) 定义两个结构体：`EngineHandle`（`inner`、`servable_len`、`kv_capacity`、`metrics_watches`）与 `EngineInner`（`submit_txs` 每分区一条、`join_handles` 每线程一个）。

[pegainfer-frontend/src/engine/handle.rs:L96-L105](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/handle.rs#L96-L105) `new_with_join_handles` 构造多分区 handle，断言至少一个分区；L88-L95 的文档注释写明语义：每分区一条提交通道 + 一个自主引擎线程，drop 最后一个克隆时按分区顺序 join 所有线程。真实用例：[glm52/src/lib.rs:L1222](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-glm52/src/lib.rs#L1222)（多分区 DP）与 [deepseek-v2-lite/src/engine.rs:L30](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-deepseek-v2-lite/src/engine.rs#L30)（单分区 + `with_servable_len`）。

[pegainfer-frontend/src/engine/handle.rs:L195-L200](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/handle.rs#L195-L200)：公开的 `submit` 只是 `submit_resolved(req, KvPrefix::none())` 的别名——普通路径不解析前缀。

[pegainfer-frontend/src/engine/handle.rs:L207-L236](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/handle.rs#L207-L236) 是本讲最核心的一段：L215-L225 的三分支路由 match（含 `debug_assert` 的不一致报警），L226-L230 的通道发送（错误映射回 `SendError<GenerateRequest>`，剥掉 KvPrefix），L234 的越界兜底。

[pegainfer-frontend/src/engine/handle.rs:L243-L260](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/handle.rs#L243-L260) `least_loaded_partition`：`min_by_key` 的键是 `(score, partition)` 二元组——分数并列时下标小者胜；分数取自 `metrics_watches` 的快照 `borrow()`。数据结构定义在 [metrics.rs:L20-L29](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/metrics.rs#L20-L29)（`num_running_reqs` = 占着解码/预填充槽位的请求，`num_waiting_reqs` = 已准入未运行如 KV 压力、预取等待）。

[pegainfer-frontend/src/engine/handle.rs:L274-L287](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/handle.rs#L274-L287) `reject_unroutable`：用请求自己的 sink 发 `Scheduled`（`cached_tokens: 0`）再发 `Rejected`（消息注明 `data_parallel_rank {partition} is outside 0..{partitions}`）——与模型调度器的准入拒单同一表面。

[pegainfer-frontend/src/engine/handle.rs:L289-L303](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/handle.rs#L289-L303) `Drop for EngineInner`：先 `clear` 全部发送端（引擎线程的 `blocking_recv` 由此收到 `None` 而退出），再逐个 join；若某线程 panic 则记 warning 不中断其余 join；跳过 join 自己（从引擎线程里 drop 最后克隆的死锁保护）。

消费侧总装在 [vllm/mod.rs:L251-L276](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L251-L276）：`LaunchedEngine::Handle(handle)` 分支校验「声明的引擎数 = 分区数」，然后**每分区**克隆一个 handle、装配一个 `LocalEngineBridge`（各带自己的 `engine_index` 与对应 `metrics_watch_for`）——这正解释了 4.1 节看到的「桥固定填 `data_parallel_rank: Some(engine_index)`」。

#### 4.5.4 代码实践

**实践目标**：运行 handle 的路由测试，亲手核对负载打分。

**操作步骤**：

1. 运行（纯 CPU，无需 GPU）：

   ```bash
   cargo test --release -p pegainfer-frontend --lib multi_partition -- --nocapture
   ```

2. 精读 [handle.rs:L372-L401](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/handle.rs#L372-L401) `multi_partition_submit_places_unbound_on_the_least_loaded`：
   - 初始：rank 0 有 2 个 running（分 2），rank 1 有 1 个 waiting（分 \( 4 \times 1 = 4 \)）→ 未绑定请求进 rank 0；
   - 测试中段用 `load_tx0.send_replace` 把 rank 0 抬到 6 running（分 6），rank 1 仍是 4 → 下一发未绑定请求翻到 rank 1。
3. 再读 [handle.rs:L404-L420](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/handle.rs#L404-L420)：给单分区 handle 提交 `rank = 7` 的请求，断言收到 `Scheduled` + 消息含 `outside 0..1` 的 `Rejected`，且 `submit` 本身返回 `Ok`。

**需要观察的现象**：4 个 multi_partition 测试全绿；负载翻盘的瞬间路由随之翻转。

**预期结果**：4 passed（routes_by_bound_rank / places_unbound_on_the_least_loaded / out_of_range_rank_is_rejected_not_dropped / drop_joins_every_thread）。无法构建时退化为阅读，标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：越界 rank 为什么不直接让 `submit` 返回 `Err`？

**答案**：`Err` 语义是「引擎通道关了」（引擎级故障）。越界是调用方参数错误，请求本身完好——用请求自己的事件流回答 `Scheduled → Rejected`，让前端协议栈走与普通拒单完全相同的路径，避免为一种调用方 bug 增加一条特殊错误处理分支。

**练习 2**：为什么 `metrics_watches` 的长度就是「前端可见的分区数」（`scheduler_partition_count`）？

**答案**：构造器 `with_metrics_watches` 断言向量非空并把它作为引擎的 DP 拓扑声明（注释：空向量是非法引擎而非缺指标的单分区）。于是这份元数据身兼两职：路由打分 + 拓扑尺寸，vllm/mod.rs 正是用它校验「声明的引擎数 = 分区数」。

**练习 3**：`Drop for EngineInner` 为什么要判断 `join_handle.thread().id() != thread::current().id()`？

**答案**：防止从引擎线程内部 drop 最后一个 handle 克隆时 join 自己——自 join 是死锁，标准库会 panic。跳过自己只清理通道，让外部的最后一克隆负责收尸。

## 5. 综合实践

把本讲五个最小模块串成一条链。**任务：为 legacy 契约的一次 `submit` 画时序图，并制作七变体发出时机表。**

### 步骤一：画主路径时序图

参与者四列：`vllm-server（HTTP/ZMQ）`、`LocalEngineBridge（demux 循环）`、`EngineHandle`、`调度器线程（如 dsv2-lite）`。

1. 从 [bridge.rs:L311-L344](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L311-L344) 出发：Add 帧 → `start_request` → 创建 tag/abort_reason/`TokenSink` → `handle.submit(GenerateRequest{...})`。
2. 进入 [handle.rs:L207-L236](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/handle.rs#L207-L236)：画出三分支路由判定框。
3. 主路径（桥已绑定 rank）：直通该分区通道；调度器侧接 [scheduler.rs:L653-L674](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-deepseek-v2-lite/src/scheduler.rs#L653-L674) 的 `send_scheduled` 发 `Scheduled`，随后每步 `Token`，终态 `Finished`。
4. 回程：`(RequestTag, TokenEvent)` 沿共享频道回到 demux（[bridge.rs:L131-L142](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs#L131-L142) 的 `event_rx.recv()` 分支），按 tag 查 `streams` 表分发。

### 步骤二：补两条特殊路径

- **前缀命中路由**：调用方持 `KvPrefix::resolved(hit, rank, hold)` 提交 → 路由 match 的第一分支（[handle.rs:L215-L222](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/handle.rs#L215-L222)）→ hold 随元组进调度器 → 调度器消费命中后 drop hold（画成生命线末端的一个 ✕）。
- **拒绝路径**：`data_parallel_rank` 越界 → [handle.rs:L274-L287](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/handle.rs#L274-L287) 用请求自己的 sink 补发 `Scheduled → Rejected`，`submit` 返回 `Ok(())`——时序图上应表现为「没有调度器参与，handle 自己回答了请求」。

### 步骤三：七变体时机表

把 4.2.3 的表格扩展成你自己的版本：每行 = 变体 + 触发条件 + 恰好一次/零或多次 + 源码证据链接（用 dsv2-lite 的行号，`PromptTokens` 标注「契约保留、当前无生产发送点」）。

### 验证（可选）

```bash
cargo test --release -p pegainfer-frontend --lib -- token_sink multi_partition
```

跑通则把两个测试名标注在时序图对应位置（路由分支标注 multi_partition 测试、abort 判定标注 token_sink 测试）。本环境未执行上述命令，构建可行性待本地验证。

## 6. 本讲小结

- legacy 契约由五个类型承担：`GenerateRequest`（输入）、`TokenEvent`（七变体输出词汇表）、`TokenSink`（共享 tagged 频道上的每请求回执）、`KvPrefix`（前缀解析 + 不透明防驱逐 hold）、`EngineHandle`（多分区路由/负载/生命周期）。
- 路由优先级：前缀解析绑定的 rank > 请求自带的 `data_parallel_rank` > 最小负载分区；负载打分为 \( \text{running} + 4 \times \text{waiting} \)，与 vLLM DP 策略同款。
- 取消不靠拆频道：前端往每请求的 `AtomicU8` 写 `Cancelled`/`Disconnected`（以首 token 是否抵达客户端为界），调度器在下次 `send`/`is_closed` 时反应式退役请求。
- 越界 rank 是调用方错误：handle 补发 `Scheduled → Rejected`，`submit` 仍返回 `Ok`，与调度器拒单同表面。
- 「发送失败即取消」贯穿始终：频道无界、无背压，准入控制永远用 `Rejected` 表达。
- 该契约处于迁移期：glm52/qwen35/kimi-k2/deepseek-v2-lite 仍走 `LaunchedEngine::Handle`；qwen3/gemma4/sim 已迁往 step 契约（下一讲）。

## 7. 下一步学习建议

下一讲（u3-l2）学习**新契约**：`step.rs` 的 `RequestUpdate`/`StepOutputs`（一步一消息取代逐 token 事件流）、`ledger.rs` 的 `RequestLedger`（terminal-exactly-once 账本）、`driver.rs` 的 `Scheduler` trait 与专用线程 drive 循环。对比阅读建议：把本讲的 `TokenSink::send` 与新契约的「调度器只写账本、账本合并成一条 `RequestUpdate`」对照，体会「每请求一频道 → 共享频道 → 逐步批消息」的演化动机；背景材料见 [docs/subsystems/frontend/frontend-architecture.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docs/subsystems/frontend/frontend-architecture.md)。若想先看旧契约的真实消费者全貌，可通读 [bridge.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/bridge.rs) 的 `dispatch_burst`（u3-l3 展开）。
