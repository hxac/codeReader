# AsyncLLM 在线引擎客户端

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `AsyncLLM` 在 V1 架构里扮演的角色——它是 API Server 眼中的「引擎客户端」，也是 EngineCore 进程的「上游搭档」。
- 区分两个名字相近却处于不同层级的抽象：上层接口 `EngineClient` 与下层 IPC 客户端 `EngineCoreClient`，并理解这条「进程边界」为何重要。
- 讲透一个请求从 `add_request` 进入、到 `generate` 把结果 `yield` 出去的完整异步数据通路，包括后台 `output_handler` 如何搬运输出。
- 理解「流式输入会话（InputStream）」这种把输入本身也变成异步流的进阶用法。
- 读懂 `session_id` 这类**请求级元数据**如何从 HTTP 入口一路透传到引擎内部的 `Request` 对象。
- 画出 API Server ↔ AsyncLLM ↔ EngineCore 的交互时序图。

本讲承接 u3-l1（V1 多进程架构）与 u3-l2（VllmConfig）。u3-l1 已经建立了「API Server / EngineCore / GPU Worker 多进程、ZMQ 通信」的全局图景；本讲要钻进**进程与进程之间的那一层胶水**——AsyncLLM。

> 本次更新（对应 `#48048 feat(frontend): session id plumbing into requests`）：`generate`/`add_request`/`process_inputs` 的签名新增 `session_id` 参数，并最终落到 `EngineCoreRequest` 与 `Request` 上。这串参数本身不改变异步主流程，因此本讲保留原有结构，在 4.3.6 专门讲解它的「透传链路」，并把全文永久链接刷新到当前 HEAD、修正因新增行而位移的行号。

## 2. 前置知识

本讲用到以下几个概念，先用大白话解释：

- **异步生成器（AsyncGenerator）**：Python 里用 `async def` + `yield` 定义的函数，调用它得到一个可以 `async for` 迭代的对象。每次 `yield` 产出一个值，但不会一次性算完。vLLM 的 `generate()` 就是一个异步生成器——客户端边收边用。
- **事件循环（event loop）**：asyncio 程序的「调度中心」，在一个线程里轮流执行多个协程。`await` 表示「这里要等，先让别人跑」。
- **生产者-消费者（producer-consumer）**：一类经典并发模型。一方往队列里放数据，另一方从队列里取数据。vLLM 用「每请求一个队列 + 一个后台搬运任务」把多请求的输出分发给各自的消费者。
- **ZMQ（ZeroMQ）**：一个高性能消息库，提供 `PUSH/PULL`、`DEALER/ROUTER` 等 socket 模式。V1 进程之间就用它传消息（见 u3-l1）。
- **EngineCore 进程**：真正跑调度与 GPU forward 的独立进程（u3-l1）。AsyncLLM 自己**不**跑模型，它住在 API Server 进程里，通过 ZMQ 把请求送给 EngineCore 进程。
- **请求级元数据（request-level metadata）**：附属于一次推理请求、但**不直接参与 token 计算**的辅助信息，如 `priority`、`trace_headers`、`session_id`。它们随请求对象在各层之间**透传**，供调度、可观测、会话亲和等用途读取。`session_id` 是稳定标识「同一次会话/同一次 agent 流程」的字符串，与每次请求都不同的 `request_id` 形成对照。

一句话定位：**AsyncLLM 是「住在前端进程里的异步代理」，它把请求打包发往后端 EngineCore 进程，再把后端产出的输出分发回各个请求的调用方。**

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [vllm/engine/protocol.py](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/engine/protocol.py) | 定义上层抽象 `EngineClient`（API Server 依赖的接口）与流式输入数据结构 `StreamingInput`。`generate` 的抽象签名里就含 `session_id`。 |
| [vllm/v1/engine/async_llm.py](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py) | 本讲主角：`AsyncLLM` 类。门面 + 请求分发 + 后台输出搬运 + 流式输入 + 请求级元数据透传。 |
| [vllm/v1/engine/core_client.py](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/core_client.py) | 下层抽象 `EngineCoreClient` 及其子类（`InprocClient`/`SyncMPClient`/`AsyncMPClient` 及 DP 变体），负责与 EngineCore 进程的 ZMQ 通信。 |
| [vllm/v1/engine/output_processor.py](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/output_processor.py) | `RequestOutputCollector`（每请求输出队列）与 `OutputProcessor`（把 EngineCore 输出转成 `RequestOutput` 并投递到各队列）。 |
| [vllm/v1/engine/input_processor.py](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/input_processor.py) | `InputProcessor.process_inputs`，把 prompt 转成 `EngineCoreRequest`，是 `session_id` 等元数据汇入请求对象的最后一站。 |
| [vllm/v1/request.py](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/request.py) | 引擎内部 `Request` 对象，`session_id` 最终作为它的一个字段落地。 |

## 4. 核心概念与源码讲解

### 4.1 EngineClient 接口与 EngineCoreClient 体系

#### 4.1.1 概念说明

vLLM 在「调用方」与「引擎」之间放了两层抽象，名字很像，**千万不要混淆**：

- **上层：`EngineClient`**（定义在 `vllm/engine/protocol.py`）。它是 API Server、离线 `LLM` 等调用方依赖的接口。它声明了 `generate`、`encode`、`abort`、`check_health` 等抽象方法。`AsyncLLM` 就是它的实现类。这一层**屏蔽了引擎是怎么跑的**（单进程？多进程？GPU？CPU？）。

- **下层：`EngineCoreClient`**（定义在 `vllm/v1/engine/core_client.py`）。它是「与 EngineCore 进程通信的客户端」抽象，子类有 `InprocClient`（同进程，调试用）、`SyncMPClient`（同步，离线 `LLM` 用）、`AsyncMPClient`（异步，`AsyncLLM` 用）以及数据并行的 `DPAsyncMPClient`/`DPLBAsyncMPClient`。这一层封装的是 **ZMQ socket、序列化、进程拉起**等 IPC 细节。

关键关系：**`AsyncLLM`（一个 `EngineClient`）内部持有一个 `engine_core` 字段，它是某个 `EngineCoreClient` 子类的实例**。于是形成了清晰的进程边界：

```
API Server 进程                              EngineCore 进程
┌──────────────────────────┐                ┌──────────────────┐
│  调用方                   │                │                  │
│    │ 依赖                 │                │   EngineCore     │
│    ▼                      │                │   (调度 + GPU)   │
│  AsyncLLM  (EngineClient) │                │                  │
│    │ 持有                  │   ──ZMQ──▶    │                  │
│    ▼                      │   ◀──ZMQ──     │                  │
│  engine_core (EngineCoreClient: AsyncMPClient)                │
└──────────────────────────┘                └──────────────────┘
```

#### 4.1.2 核心流程

1. API Server（或 `LLM`）只认识 `EngineClient` 接口，拿到一个 `AsyncLLM` 实例。
2. `AsyncLLM.__init__` 时，会通过工厂方法构造一个具体的 `EngineCoreClient` 子类，并把它存为 `self.engine_core`。
3. 之后所有「发请求 / 取输出 / 中止 / 控制类操作」都由 `AsyncLLM` 转交给 `self.engine_core`，后者用 ZMQ 跨进程完成。

#### 4.1.3 源码精读

先看上层接口。`EngineClient` 是一个抽象基类，规定了调用方能用哪些方法：

[`vllm/engine/protocol.py:L41-L51`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/engine/protocol.py#L41-L51) —— `EngineClient` 声明了 `vllm_config`、`model_config`、`renderer`、`input_processor` 等属性，以及 `is_running`、`is_stopped`、`errored` 等状态。注意它持有 `input_processor`，说明输入预处理是这层的职责。

[`vllm/engine/protocol.py:L65-L86`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/engine/protocol.py#L65-L86) —— `generate` 是最重要的抽象方法，签名里 `prompt` 既可以是原始文本、也可以是已渲染的 `EngineInput`、甚至是一个 `AsyncGenerator[StreamingInput, None]`（流式输入，见 4.5）。注意第 81 行的 `session_id: str | None = None`（本次新增），它是「请求级元数据」参数的一员，返回值是 `AsyncGenerator[RequestOutput, None]`。

再看下层。`EngineCoreClient` 的类文档把三个子类的分工说得很清楚：

[`vllm/v1/engine/core_client.py:L78-L87`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/core_client.py#L78-L87) —— 注释列出 `InprocClient`（V0 风格同进程）、`SyncMPClient`（ZMQ + 后台进程，给 `LLM`）、`AsyncMPClient`（ZMQ + 后台进程 + asyncio，给 `AsyncLLM`）。

工厂方法根据「是否多进程」「是否异步」「是否数据并行」选择具体子类：

[`vllm/v1/engine/core_client.py:L114-L139`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/core_client.py#L114-L139) —— `make_async_mp_client`：当 `data_parallel_size > 1` 时，外部负载均衡用 `DPAsyncMPClient`，内部负载均衡用 `DPLBAsyncMPClient`，否则用单引擎的 `AsyncMPClient`。这正是 u3-l1 提到的「DP>1 时多对多拓扑」在客户端侧的体现。

#### 4.1.4 代码实践

**实践目标**：亲手确认「上层接口 / 下层客户端」这条边界。

**操作步骤**：

1. 打开 [`vllm/engine/protocol.py`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/engine/protocol.py)，找到 `class EngineClient`，数一数它声明了多少个 `@abstractmethod`（`generate`、`encode`、`abort`、`check_health` 等）。
2. 打开 [`vllm/v1/engine/async_llm.py`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py) 第 72 行，确认 `class AsyncLLM(EngineClient)`——它实现的就是上面那个接口。
3. 在 `async_llm.py` 里搜索 `self.engine_core`，看它在哪里被赋值（提示：构造函数里），类型是什么。

**需要观察的现象**：`AsyncLLM` 实现的方法（`generate`/`abort`/...）和它转发给 `self.engine_core` 的方法（`add_request_async`/`get_output_async`/...）是**两套名字**——这正暗示了两层抽象。

**预期结果**：`AsyncLLM` 的 `generate()` 内部并不会直接跑模型，而是调用 `self.engine_core.add_request_async(...)` 把请求送出去。运行行为「待本地验证」（需 GPU 与模型）。

#### 4.1.5 小练习与答案

**练习 1**：`AsyncLLM` 实现的是哪个接口？它持有的下层客户端基类叫什么？
**答案**：实现 `vllm/engine/protocol.py` 里的 `EngineClient`；持有 `vllm/v1/engine/core_client.py` 里的 `EngineCoreClient`（具体为 `AsyncMPClient` 或其 DP 子类）。

**练习 2**：为什么要把「上层接口」和「下层 IPC 客户端」拆成两个抽象，而不是让 API Server 直接持有 ZMQ socket？
**答案**：为了解耦。API Server 只依赖 `EngineClient` 这个稳定接口，引擎内部是单进程还是多进程、用不用 ZMQ，它都不用关心；而进程通信、序列化、负载均衡这些易变细节被关在 `EngineCoreClient` 子类里。

---

### 4.2 AsyncLLM 门面与构造

#### 4.2.1 概念说明

`AsyncLLM` 是在线服务路径上的「门面（facade）」。它把几样东西捏在一起：

- **配置**：持有 `vllm_config`、`model_config`（来自 u3-l2 的 `VllmConfig`）。
- **输入处理器** `InputProcessor`：把外部传入的 prompt（文本/`EngineInput`）转成引擎内部用的 `EngineCoreRequest`（含 `prompt_token_ids`）。
- **输出处理器** `OutputProcessor`：把 EngineCore 产出的 `EngineCoreOutputs` 转成对外暴露的 `RequestOutput`。
- **引擎核心客户端** `engine_core`：一个 `EngineCoreClient`，构造它时会**在后台拉起 EngineCore 进程**（u3-l1）。
- **统计日志** `logger_manager`：Prometheus 等指标（u10-l2 会讲）。

#### 4.2.2 核心流程

构造 `AsyncLLM` 的顺序大致是：

```
__init__:
  1. 解析配置、tracing、renderer
  2. 建 InputProcessor          ← prompt → EngineCoreRequest
  3. 建 OutputProcessor          ← EngineCoreOutputs → RequestOutput
  4. 建 engine_core              ← 拉起 EngineCore 进程 (make_async_mp_client)
  5. 建 StatLoggerManager        ← 指标
  6. （若已在事件循环中）启动 output_handler
```

#### 4.2.3 源码精读

[`vllm/v1/engine/async_llm.py:L72-L73`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L72-L73) —— 类声明，docstring 一句话点题：「An asynchronous wrapper for the vLLM engine.」

构造函数里三大组件的建立：

[`vllm/v1/engine/async_llm.py:L137-L156`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L137-L156) —— 关键三步：第 138 行建 `input_processor`；第 141–146 行建 `output_processor`；第 149–156 行 `self.engine_core = EngineCoreClient.make_async_mp_client(...)`。注释第 148 行写得很直白：`# EngineCore (starts the engine in background process).`——拉起后台进程就发生在这里。

`output_handler` 的懒启动：

[`vllm/v1/engine/async_llm.py:L173-L179`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L173-L179) —— 先把 `self.output_handler` 置 `None`，然后 `try: asyncio.get_running_loop()`：如果当前已经在事件循环里，就立刻 `_run_output_handler()`；否则（`RuntimeError`）跳过，留到第一次 `add_request` 时再启动。这样 `__init__` 可以在事件循环启动**之前**被调用，从而让 OpenAI server 能优雅地处理启动失败。

两个工厂方法：

[`vllm/v1/engine/async_llm.py:L205-L232`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L205-L232) —— `from_vllm_config`：用 `Executor.get_class(vllm_config)` 选好执行器类，再调用 `cls(...)`。这是 API Server 构造 `engine_client` 的常见入口。

#### 4.2.4 代码实践

**实践目标**：理解 `AsyncLLM` 构造时「谁住在前端进程、谁在后台进程」。

**操作步骤**：阅读 [`vllm/v1/engine/async_llm.py:L75-L203`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L75-L203) 的 `__init__`，回答：
- `input_processor`、`output_processor`、`logger_manager` 这三者，分别跑在哪个进程？
- 哪一行真正触发了 EngineCore 后台进程的启动？

**预期结果**：前三者都住在**前端进程**（API Server 所在进程），只有 `make_async_mp_client`（第 149 行）会拉起**后台 EngineCore 进程**。这正是 V1「调度/执行分离到不同进程」的设计（u3-l1）。

#### 4.2.5 小练习与答案

**练习 1**：`AsyncLLM.__init__` 中哪一步会真正拉起 EngineCore 进程？
**答案**：`self.engine_core = EngineCoreClient.make_async_mp_client(...)`（`async_llm.py` 第 149–156 行）。

**练习 2**：为什么 `output_handler` 不在 `__init__` 里无条件启动？
**答案**：因为它依赖一个运行中的 asyncio 事件循环，而 `__init__` 可能在事件循环启动前被调用。代码用 `try: asyncio.get_running_loop()` 探测，存在则启动，否则留到首次 `add_request` 时再启动（`async_llm.py` 第 173–179、397 行）。

---

### 4.3 add_request / generate 的异步请求处理模型

#### 4.3.1 概念说明

这是本讲的核心。`AsyncLLM` 把「发请求」和「收结果」拆成两个协作的 API：

- **`add_request(...)`**：异步方法，**返回一个 `RequestOutputCollector`**（每请求专属的输出队列）。它负责：预处理输入 → 在本进程的 `OutputProcessor` 注册这个请求 → 把 `EngineCoreRequest` 通过 `engine_core.add_request_async(...)` 发给 EngineCore 进程。
- **`generate(...)`**：异步生成器，内部先调 `add_request` 拿到队列，然后循环从队列里拉 `RequestOutput` 并 `yield` 给调用方，直到 `finished`。

这种拆分是**生产者-消费者**模式：一个后台 `output_handler`（见 4.4）负责把 EngineCore 的输出推送到**所有**请求的队列；每个 `generate()` 调用只是各自队列的消费者。于是成百上千个并发请求可以共享同一个搬运任务，互不干扰。

#### 4.3.2 核心流程

一次普通（非流式）请求的端到端流程：

```
generate()                         add_request()
   │                                   │
   │── add_request(req_id,prompt,params, session_id=...) ──▶
   │                                   │
   │                          ┌────────┴────────┐
   │                          ▼                 ▼
   │            InputProcessor 把 prompt      (n>1 时)
   │            转成 EngineCoreRequest        扇出为多个子请求
   │            (session_id 也随之带上)         │
   │                   创建 RequestOutputCollector (队列 q)
   │                          │
   │            ┌─────────────┴──────────────┐
   │            ▼                            ▼
   │   output_processor.add_request   engine_core.add_request_async
   │   (本进程登记，绑定队列 q)        (ZMQ 发往 EngineCore 进程)
   │            │                            │
   │   ◀────────┘            返回 q          │
   │                                            ▼
   │                                  ┌─────────────────┐
   │  while not finished:             │  EngineCore 进程 │
   │     out = q.get_nowait()         │  调度 + forward  │
   │            or await q.get()      │  产出 outputs    │
   │     yield out                    └────────┬────────┘
   │                                          │ ZMQ PUSH
   │                            (后台 output_handler 接收并
   │                             process_outputs → 推入 q)
   └── yield RequestOutput 给 API Server / 调用方
```

#### 4.3.3 源码精读

`generate` 的文档把四步流程讲得很清楚：

[`vllm/v1/engine/async_llm.py:L569-L582`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L569-L582) —— 注释说明 `generate` 是 API Server 触发请求的主入口，并强调「一个独立的 `output_handler` 后台任务负责从 EngineCore 拉输出、塞进每请求的 AsyncStream」。

`generate` 的消费循环：

[`vllm/v1/engine/async_llm.py:L586-L614`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L586-L614) —— 第 586 行先 `await self.add_request(...)` 拿到队列 `q`（注意第 595 行把 `session_id=session_id` 透传下去）；第 604 行起 `while not finished`；第 607 行 `out = q.get_nowait() or await q.get()` 是性能要点——**先用非阻塞 `get_nowait()` 探一下**，命中就不切任务，没命中才 `await`，注释解释这能在高负载下减少任务切换；第 612 行看 `out.finished` 决定是否结束；第 614 行 `yield out`。

`add_request` 的入口分派（它要处理多种 prompt 形态）：

[`vllm/v1/engine/async_llm.py:L283-L385`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L283-L385) —— 注意三个分支：第 320 行 `if isinstance(prompt, AsyncGenerator)` 走流式输入（4.5）；第 354 行 `if isinstance(prompt, dict) and "type" in prompt` 是已渲染的 `EngineInput`，用同步的 `process_inputs`；否则是原始 prompt，用 `await process_inputs_async(...)`（第 372 行），**注释明确说原始 prompt 的 tokenize/多模态处理不能阻塞事件循环**。无论哪个分支，`session_id`（第 297 行的入参）都会被原样传给 `process_inputs`（第 367、383 行）。

请求注册的「双侧登记」：

[`vllm/v1/engine/async_llm.py:L424-L439`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L424-L439) —— `_add_request` 做两件事：第 433 行 `self.output_processor.add_request(...)`（**本进程**登记，把队列 `q` 绑到该请求）；第 436 行 `await self.engine_core.add_request_async(request)`（**跨进程**，把请求发给 EngineCore）。这两步缺一不可——前者让输出能找到归宿，后者让请求真正被执行。

下层客户端如何「发出去」：

[`vllm/v1/engine/core_client.py:L1145-L1148`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/core_client.py#L1145-L1148) —— `AsyncMPClient.add_request_async` 给请求盖上 `client_index`，调用 `_send_input(EngineCoreRequestType.ADD, request)`，把请求序列化后通过 ZMQ input socket 发出，并确保输出队列任务已启动。

队列本身的实现：

[`vllm/v1/engine/output_processor.py:L45-L96`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/output_processor.py#L45-L96) —— `RequestOutputCollector` 用一个 `asyncio.Event` + 单槽 `self.output` 实现：`put` 非阻塞写入并 `set()` 事件；`get` 阻塞等待事件；`get_nowait` 非阻塞取出。第 55 行 `self.aggregate = output_kind == RequestOutputKind.DELTA` 决定流式增量时是否合并多次输出——这是 DELTA 模式下「生产快于消费就合并」的关键。

> 补充：`generate()` 还处理了多种异常分支（客户端断连、引擎死亡、输入流错误等），见 [`async_llm.py:L619-L663`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L619-L663)。其中客户端断连（`CancelledError`/`GeneratorExit`）会触发 `await self.abort(...)`，确保 EngineCore 侧也释放资源。

#### 4.3.4 代码实践

**实践目标**：跟踪单个请求的「双侧登记」，验证进程边界。

**操作步骤**：

1. 在 [`async_llm.py:L424-L439`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L424-L439) 的 `_add_request` 里，定位第 433 行和第 436 行。
2. 跟着第 436 行跳到 [`core_client.py:L1145-L1148`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/core_client.py#L1145-L1148)，再跳到 `_send_input`（[`core_client.py:L1104-L1123`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/core_client.py#L1104-L1123)），看清请求是如何被打成 `(engine_identity, request_type, *encoded)` 通过 `input_socket.send_multipart` 发出去的。

**需要观察的现象**：同一个请求被「登记」了两次，一次在本进程（`OutputProcessor`），一次跨进程（`EngineCore`）。这两次登记的对象不同：前者登记的是「输出往哪投递」，后者登记的是「要执行什么」。

**预期结果**：能复述「`add_request` → `_add_request` →（本进程 `output_processor.add_request` + 跨进程 `engine_core.add_request_async`）」这条链路。运行行为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`generate()` 与 `add_request()` 的职责如何分工？
**答案**：`add_request` 负责预处理输入、在本进程与 EngineCore 进程双侧登记请求、返回一个队列 `RequestOutputCollector`；`generate` 只负责循环从该队列拉取并 `yield` `RequestOutput`，直到 `finished`。

**练习 2**：为什么 `_add_request` 要在 `output_processor` 和 `engine_core` **两处**都登记请求？
**答案**：因为输出处理（detokenize、组装 `RequestOutput`、投递到队列）发生在**前端进程**的 `OutputProcessor`，而真正的 forward 执行发生在**后台 EngineCore 进程**。两侧都需要知道这个请求，后台才能把结果送回，前端才能把结果投递到正确的队列。

**练习 3**：`generate` 里 `q.get_nowait() or await q.get()` 为什么要先尝试非阻塞的 `get_nowait()`？
**答案**：高负载时输出连续到达，用 `get_nowait()` 直接取可避免不必要的 `await`（任务切换），降低开销；只有队列空时才退化为阻塞 `get`（`async_llm.py` 第 605–607 行注释）。

---

### 4.3.6 请求级元数据透传：以 session_id 为例

#### 4.3.6.1 概念说明

除了 `prompt` 与 `sampling_params`，一个请求还常常携带一些「不参与 token 计算、但需要随请求一起到达引擎」的元数据：`priority`（优先级）、`trace_headers`（链路追踪）、`lora_request`、`data_parallel_rank`，以及本次新增的 `session_id`。

`session_id` 解决的问题是「**把属于同一次会话/同一次 agent 流程的多条请求关联起来**」。注意它和两个已有概念的区别：

| 标识 | 粒度 | 语义 |
|------|------|------|
| `request_id` | 每条请求唯一 | 标识「这一次」推理，完成后即失效 |
| `session_id` | 跨多条请求稳定 | 标识「这一次会话」，同会话的多条请求共享同一个值 |

后端可以据此做会话亲和（把同一 session 的请求尽量送到同一个 EngineCore/DP rank）、前缀复用、统计聚合等。它由前端（API Server）决定，需要**逐层透传**到引擎内部的 `Request` 对象。

#### 4.3.6.2 核心流程

`session_id` 的透传链路是一条贯穿多层接口的「同名参数接力」：

```
HTTP 入口 (OpenAI 请求体 session_id 或 X-Session-ID 头)
   │  GenerateBaseServing._get_session_id(...)
   ▼
EngineClient.generate(session_id=...)        ← 抽象接口 (protocol.py)
   │
   ▼
AsyncLLM.generate(session_id=...)            ← 实现入口 (async_llm.py)
   │
   ▼
AsyncLLM.add_request(session_id=...)         ← 预处理前
   │
   ▼
InputProcessor.process_inputs(session_id=...) ← prompt → EngineCoreRequest
   │
   ▼
EngineCoreRequest(session_id=...)            ← msgpack 序列化跨进程的数据类
   │  (ZMQ 发往 EngineCore 进程)
   ▼
Request.__init__(session_id=...)             ← 引擎内部最终落地对象
   │  self.session_id = session_id
   ▼
（调度器/后端按需读取 request.session_id）
```

关键点：`session_id` **不改变**异步主流程（仍是 generate → add_request → 双侧登记），它只是在每一层的方法签名里多一个 `session_id: str | None = None` 参数并被原样传递。这种「逐层透传」是 vLLM 添加请求级元数据的典型模式。

#### 4.3.6.3 源码精读

从最上层抽象接口开始。`EngineClient.generate` 在签名里新增了 `session_id`：

[`vllm/engine/protocol.py:L81`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/engine/protocol.py#L81) —— 抽象方法 `generate` 的参数列表里加入 `session_id: str | None = None`，与 `priority`、`data_parallel_rank` 等并列。这意味着**所有 `EngineClient` 实现类**都得接受它。

`AsyncLLM` 在 `generate` 与 `add_request` 两处都声明并转发它：

[`vllm/v1/engine/async_llm.py:L297`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L297) —— `add_request` 签名里的 `session_id: str | None = None`（第 565 行是 `generate` 里的对应参数）。

[`vllm/v1/engine/async_llm.py:L367-L383`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L367-L383) —— 两个分支都把 `session_id=session_id` 透传给 `InputProcessor`：已渲染 `EngineInput` 走同步 `process_inputs`（第 367 行），原始 prompt 走 `process_inputs_async`（第 383 行）。流式输入则在构造公共 `inputs` 字典时把它打包进去（第 464 行）。

`InputProcessor` 把它写进 `EngineCoreRequest`：

[`vllm/v1/engine/input_processor.py:L264`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/input_processor.py#L264) —— `process_inputs` 的签名新增 `session_id` 参数。

[`vllm/v1/engine/input_processor.py:L395`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/input_processor.py#L395) —— 在构造 `EngineCoreRequest(...)` 时 `session_id=session_id`，元数据正式进入「要被跨进程序列化」的请求数据类。

`EngineCoreRequest` 把它声明为字段（这样才能被 msgpack 序列化、随 ZMQ 传到 EngineCore 进程）：

[`vllm/v1/engine/__init__.py:L148`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/__init__.py#L148) —— `EngineCoreRequest`（一个 Struct）新增 `session_id: str | None = None` 字段。

最后在引擎内部 `Request` 上落地：

[`vllm/v1/request.py:L77`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/request.py#L77) —— `Request.__init__` 签名新增 `session_id` 参数。

[`vllm/v1/request.py:L187`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/request.py#L187) —— `self.session_id = session_id`，把它存为 `Request` 的实例属性，供调度器/后端读取。

入口侧（HTTP → 引擎）的来源有三处，统一由 `GenerateBaseServing._get_session_id` 汇总：

[`vllm/entrypoints/generate/base/serving.py:L228-L242`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/entrypoints/generate/base/serving.py#L228-L242) —— 优先级：请求体里的 `session_id` 字段 ＞ `X-Session-ID` HTTP 头 ＞ `vllm_xargs` 里的 `session_id`。其中头名称常量定义在第 44 行 `SESSION_ID_HEADER = "X-Session-ID"`。OpenAI 三类请求（chat/completion/responses）都在协议里新增了同名字段，例如 [`chat_completion/protocol.py:L397-L404`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/entrypoints/openai/chat_completion/protocol.py#L397-L404)。

#### 4.3.6.4 代码实践

**实践目标**：用一条「断点式阅读」验证 `session_id` 的完整透传链，并体会「逐层透传」的机械性。

**操作步骤**：

1. 从 HTTP 入口读起：打开 [`tests/entrypoints/openai/test_session_id.py`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/tests/entrypoints/openai/test_session_id.py)，看 `test_get_session_id_accepts_body_field` 与 `test_get_session_id_accepts_session_header` 如何断言「请求体字段优先于 `X-Session-ID` 头」。
2. 反向追踪：在仓库里 `grep -rn "session_id" vllm/v1/engine/ vllm/v1/request.py vllm/engine/protocol.py`，你会看到它在 **6 个文件的签名里几乎一字不差地重复出现**——这正是「透传」的指纹。
3. 自检：确认 `session_id` 没有任何**读取**它的消费逻辑出现在 `async_llm.py` 内（`AsyncLLM` 只是搬运，不消费）。它真正被读取的位置在更下游（调度/后端），不在本讲范围。

**需要观察的现象**：同一名字 `session_id` 在调用栈里逐层出现，每一层都只是「收下→转发」，没有中途加工。

**预期结果**：能口述「HTTP（body/header）→ `_get_session_id` → `EngineClient.generate` → `AsyncLLM.generate/add_request` → `process_inputs` → `EngineCoreRequest` → `Request.session_id`」这条链，并解释为什么 `EngineCoreRequest` 必须把它声明为**字段**而非普通参数（答案：它要跨进程序列化）。

#### 4.3.6.5 小练习与答案

**练习 1**：`session_id` 和 `request_id` 有什么区别？
**答案**：`request_id` 每条请求唯一、完成后失效，用于标识「这一次推理」；`session_id` 在同一次会话/agent 流程的多条请求间保持稳定，用于把相关请求关联起来（见 `chat_completion/protocol.py` 第 397–404 行的 docstring）。

**练习 2**：为什么 `session_id` 必须作为 `EngineCoreRequest` 的字段（Struct field），而不能只是某个函数的局部参数？
**答案**：`EngineCoreRequest` 会被 msgpack 序列化、经 ZMQ 从前端进程发往 EngineCore 进程。只有声明为字段，它才会随请求对象一起跨进程到达后端，最终在 `Request` 上落地。若是局部参数，跨进程后就丢失了。

**练习 3**：`AsyncLLM` 内部有没有「消费」`session_id`（即根据它做决策）？
**答案**：没有。`AsyncLLM` 只负责把它从 `generate`/`add_request` 透传给 `InputProcessor`，属于纯搬运。真正的消费（如会话亲和）发生在更下游的调度/后端层。

---

### 4.4 output_handler：后台搬运输出与进程边界

#### 4.4.1 概念说明

`output_handler` 是 AsyncLLM 里**唯一**的后台搬运任务（每个 `AsyncLLM` 一个）。它持续做一件事：从 `engine_core.get_output_async()` 拉取一批 `EngineCoreOutputs`，交给 `OutputProcessor` 处理，处理结果被**推入各请求自己的 `RequestOutputCollector` 队列**。各 `generate()` 任务再从自己的队列里取。

这是输出从「EngineCore 进程」回到「调用方」的完整链路中最长的一段。把它拆开看，输出要经过这些异步站点：

```
EngineCore 进程 (forward 产出 outputs)
      │  ZMQ PUSH（msgpack + 可选零拷贝张量帧）
      ▼
[前端进程] AsyncMPClient.process_outputs_socket 任务
      │  recv_multipart → decoder.decode → validate_alive
      ▼
AsyncMPClient.outputs_queue  (asyncio.Queue)
      │  get_output_async() 取出
      ▼
AsyncLLM.output_handler 任务
      │  process_outputs(...) 把 EngineCoreOutputs 转成 RequestOutput
      │  并推入各请求的 RequestOutputCollector
      ▼
各请求的 RequestOutputCollector 队列
      │  generate() 的 q.get_nowait()/q.get()
      ▼
调用方 (API Server / 用户)
```

#### 4.4.2 核心流程

`output_handler` 的循环（伪代码）：

```
while True:
    outputs = await engine_core.get_output_async()      # 拉一批
    for start in range(0, num_outputs, chunk_size):     # 分块，避免阻塞事件循环
        slice = outputs.outputs[start:start+chunk_size]
        processed = output_processor.process_outputs(slice, ...)
        # process_outputs 内部把 RequestOutput 推入对应请求的队列
        if 未到末尾: await asyncio.sleep(0)             # 让出事件循环
        if processed.reqs_to_abort:
            await engine_core.abort_requests_async(...) # stop string 触发的中止
    output_processor.update_scheduler_stats(outputs.scheduler_stats)
    if logger_manager: logger_manager.record(...)       # 指标
```

#### 4.4.3 源码精读

`output_handler` 的定义与启动：

[`vllm/v1/engine/async_llm.py:L665-L735`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L665-L735) —— 第 673–682 行特意把 `engine_core`/`output_processor`/`logger_manager` 等存成局部变量，注释（671–672 行）解释：**避免任务持有指向 `AsyncLLM` 的循环引用，否则对象无法被 GC 回收**。`logger_manager` 用可变列表 `self._logger_ref` 包裹，是为了弹性 EP 扩缩容时能替换它而不引入循环引用（见 676–680 行）。

核心循环：

- 第 688 行 `outputs = await engine_core.get_output_async()`：拉一批输出。
- 第 699 行 `for start in range(0, num_outputs, chunk_size)`：按 `VLLM_V1_OUTPUT_PROC_CHUNK_SIZE`（第 682 行）分块处理，第 711 行 `await asyncio.sleep(0)` 在块之间让出事件循环。
- 第 703 行 `output_processor.process_outputs(...)`：处理这一片；第 706–707 行注释强调「RequestOutput 被推入各自的队列」，且 `assert not processed_outputs.request_outputs`——即这里不直接返回结果，而是副作用式地投递。
- 第 714–717 行：处理因 stop string 而需要中止的请求。
- 第 725 行 `logger_ref[0].record(...)`：记录指标。

下层客户端的接收任务（链路的前两段）：

[`vllm/v1/engine/core_client.py:L1016-L1091`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/core_client.py#L1016-L1091) —— `_ensure_output_queue_task` 创建 `process_outputs_socket` 协程：第 1040 行 `await output_socket.recv_multipart(copy=False)` 收帧；第 1041 行 `validate_alive` 检查是否引擎已死；第 1042 行 `decoder.decode(frames)` 反序列化为 `EngineCoreOutputs`；第 1043 行区分 `utility_output`（控制类回执，如 utility 方法结果）与普通输出；第 1082–1083 行把普通输出 `put_nowait` 进 `outputs_queue`。同样用 `weakref` 避免循环引用（第 1029 行）。

`get_output_async`：

[`vllm/v1/engine/core_client.py:L1093-L1102`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/core_client.py#L1093-L1102) —— 从 `outputs_queue` 取；若取到的是异常（接收任务里捕获并塞进来的），就用 `_format_exception` 包成 `EngineDeadError` 抛出，从而让上层的 `output_handler` 进入错误传播路径。

#### 4.4.4 代码实践

**实践目标**：把输出回传链路的五个站点对上号。

**操作步骤**：按下表，逐个打开链接，确认每个站点「做什么、交给谁」：

| 站点 | 代码位置 | 动作 |
|------|----------|------|
| ① 收帧+解码 | [`core_client.py:L1040-L1042`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/core_client.py#L1040-L1042) | `recv_multipart` → `decode` |
| ② 入队 | [`core_client.py:L1082-L1083`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/core_client.py#L1082-L1083) | `outputs_queue.put_nowait` |
| ③ 出队 | [`core_client.py:L1099`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/core_client.py#L1099) | `await outputs_queue.get()` |
| ④ 处理+分发 | [`async_llm.py:L703-L707`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L703-L707) | `process_outputs` → 推入各请求队列 |
| ⑤ 消费 | [`async_llm.py:L607`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L607) | `generate` 的 `q.get_nowait()/get()` |

**预期结果**：能口述「EngineCore PUSH → process_outputs_socket 解码 → outputs_queue → get_output_async → output_handler 的 process_outputs → 各请求 RequestOutputCollector → generate」这条链。

#### 4.4.5 小练习与答案

**练习 1**：`output_handler` 为什么要把一批 outputs 切成 chunk 处理，并在块之间 `await asyncio.sleep(0)`？
**答案**：单批输出可能很大，一次性处理会长时间占用事件循环、饿死其他协程；分块并让出控制权，保证事件循环响应性（`async_llm.py` 第 699、711 行）。

**练习 2**：为什么 `process_outputs_socket` 任务和 `output_handler` 任务都用 `weakref.ref(self)` 而不是直接引用 `self`？
**答案**：避免后台任务持有指向 client / AsyncLLM 的循环引用，否则对象在关闭时无法被垃圾回收、资源清理不干净（`core_client.py` 第 1029 行、`async_llm.py` 第 671–682 行）。

---

### 4.5 InputStream：流式输入会话

#### 4.5.1 概念说明

普通的 `generate` 调用，prompt 是一次性给定的。但有些场景（多轮 agent 会话、增量补全）希望**输入本身也是逐块到达的流**。vLLM 支持「流式输入」：把 prompt 参数传成一个 `AsyncGenerator[StreamingInput, None]`，调用方一边 yield 输入块，引擎一边处理。

`StreamingInput` 就是每个输入块的数据结构（prompt + 可选的 sampling_params）。注意它和「流式**输出**」是两回事——后者指 `generate` 逐个 `yield` 结果，前者指**输入**逐块喂入。

#### 4.5.2 核心流程

当 `add_request` 检测到 `prompt` 是 `AsyncGenerator` 时，走 `_add_streaming_input_request`：

```
1. 校验采样参数（不支持 pooling / n>1 / FINAL_ONLY / stop）
2. 构造一个「哨兵」final_req (prompt_token_ids=[0])，作为输入流结束信号
3. 创建队列 q，启动 handle_inputs() 后台任务：
     async for input_chunk in input_stream:
         req = input_processor.process_inputs(..., resumable=True)
         await self._add_request(req, ...)        # 每块都喂给引擎
     finally:
         await self._add_request(final_req, ...)  # 流结束，发哨兵
4. generate() 照常从 q 消费输出
```

#### 4.5.3 源码精读

数据结构：

[`vllm/engine/protocol.py:L29-L38`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/engine/protocol.py#L29-L38) —— `StreamingInput`：`prompt: EngineInput` + 可选 `sampling_params`。docstring 说明它用于「multi-turn streaming sessions where inputs are provided via an async generator」。

错误包装：

[`vllm/v1/engine/async_llm.py:L60-L69`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L60-L69) —— `InputStreamError`：把用户输入生成器里抛出的异常包起来，让 `generate()` 能原样传播，而不是被包成 `EngineGenerateError`。

分派入口：

[`vllm/v1/engine/async_llm.py:L320-L336`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L320-L336) —— `add_request` 里 `if isinstance(prompt, AsyncGenerator)` 分支，转交 `_add_streaming_input_request`，并把 `session_id` 一并传入（第 335 行）。

核心实现：

[`vllm/v1/engine/async_llm.py:L441-L527`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L441-L527) ——
- 第 471–478 行：构造哨兵 `final_req`，用 `TokensPrompt(prompt_token_ids=[0])` 作为占位 prompt，注释（471–472 行）说明它「also used as the finished signal once the input stream is closed」。
- 第 484–521 行 `handle_inputs()`：`async for input_chunk in input_stream` 逐块处理；每块用 `process_inputs(..., resumable=True)`（第 494–500 行，`resumable` 表示可续接的会话）；第 509 行 `await self._add_request(req, ...)` 把每块喂进引擎。
- 第 510–515 行：捕获 `CancelledError`/`GeneratorExit` 标记取消；其他异常用 `InputStreamError` 包裹塞进队列。
- 第 516–521 行 `finally`：只要不是被取消，就发哨兵 `final_req` 表示输入流结束。
- 第 526 行 `asyncio.create_task(handle_inputs())`：把消费输入流的协程挂成后台任务，并把任务记在 `queue._input_stream_task` 上，便于 `close()` 时取消。

校验：

[`vllm/v1/engine/async_llm.py:L529-L543`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L529-L543) —— `_validate_streaming_input_sampling_params`：流式输入不支持 pooling 模型、`n>1`、`output_kind=FINAL_ONLY`、带 stop 字符串。

#### 4.5.4 代码实践

**实践目标**：理解「哨兵请求」在流式输入中的作用。

**操作步骤**：阅读 [`async_llm.py:L471-L521`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L471-L521)，思考：如果 `handle_inputs` 的 `finally` 块不发送 `final_req`，会发生什么？

**预期结果**：引擎无法知道这个会话的输入已经结束，会一直等待下一块输入，请求永远无法进入 `finished` 状态。哨兵请求就是用来显式宣告「输入流关闭」。运行行为「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：流式输入为什么需要一个 `prompt_token_ids=[0]` 的 `final_req`？
**答案**：作为输入流结束的「哨兵」请求。`handle_inputs` 在输入流耗尽后发送它，通知引擎该会话的输入已全部到达，可以收尾（`async_llm.py` 第 471–478、516–521 行）。

**练习 2**：哪些采样配置不支持流式输入？
**答案**：pooling 模型、`n>1`、`output_kind=RequestOutputKind.FINAL_ONLY`、带 stop 字符串（`async_llm.py` 第 529–543 行）。

**练习 3**：流式「输入」和流式「输出」是一回事吗？
**答案**：不是。流式输出指 `generate()` 把结果逐个 `yield` 给调用方（DELTA 模式）；流式输入指 prompt 参数本身是一个 `AsyncGenerator[StreamingInput]`，输入逐块喂入引擎，常用于多轮会话。

---

## 5. 综合实践

**任务**：梳理 AsyncLLM 从接收请求到转发给 EngineCore 的调用路径，并画出 **API Server ↔ AsyncLLM ↔ EngineCore** 的交互时序图。

### 步骤

1. **入站（请求）**：从 [`async_llm.py:L550`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L550) 的 `generate()` 出发，依次经过：
   - `add_request`（[`L283`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L283)）→ `InputProcessor.process_inputs_async`（原始 prompt）→ `_add_request`（[`L424`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L424)）→ `engine_core.add_request_async`（[`L436`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L436)）→ `AsyncMPClient.add_request_async`（[`core_client.py:L1145`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/core_client.py#L1145)）→ `_send_input`（[`L1104`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/core_client.py#L1104)）→ `input_socket.send_multipart`（ZMQ DEALER→ROUTER，跨进程）。
   - **额外观察**：在 `add_request` → `process_inputs` 这段，挑一个请求级元数据（如 `session_id`）跟着走一遍，确认它在 [`input_processor.py:L395`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/input_processor.py#L395) 被写进 `EngineCoreRequest`、从而能随 ZMQ 跨进程。

2. **出站（输出）**：从 EngineCore 进程 PUSH 开始，依次经过：
   - `process_outputs_socket` 收帧解码（[`core_client.py:L1037`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/core_client.py#L1037)）→ `outputs_queue` → `get_output_async`（[`L1093`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/core_client.py#L1093)）→ `output_handler.process_outputs`（[`async_llm.py:L703`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L703)）→ 各请求 `RequestOutputCollector` → `generate` 的 `q.get_nowait()/get()`（[`L607`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L607)）→ `yield` 给 API Server。

3. **画时序图**：参考下面的模板，把上面的调用填进去。

```
API Server          AsyncLLM (前端进程)          EngineCoreClient         EngineCore (后台进程)
    │                     │                         │                          │
    │ generate(prompt,    │                         │                          │
    │   session_id=...)   │                         │                          │
    │────────────────────▶│                         │                          │
    │                     │ add_request             │                          │
    │                     │ ├─InputProcessor        │                          │
    │                     │ │  (session_id → req)   │                          │
    │                     │ ├─output_processor      │                          │
    │                     │ │  .add_request (登记q) │                          │
    │                     │ └─add_request_async ───▶│                          │
    │                     │                         │ input_socket.send ──ZMQ─▶│ (EngineCoreRequestType.ADD)
    │                     │◀──── 返回 q ────────────│                          │
    │                     │                         │                          │ 调度 + forward
    │                     │                         │                          │ 产出 EngineCoreOutputs
    │                     │                         │◀────────── ZMQ PUSH ─────│
    │                     │                         │ process_outputs_socket   │
    │                     │                         │  decode → outputs_queue  │
    │                     │ get_output_async ◀──────│                          │
    │                     │ output_handler:         │                          │
    │                     │  process_outputs → push q                          │
    │ q.get_nowait()/get()│                         │                          │
    │◀─── yield RequestOutput ─────────────────────│                          │
    │   ...(循环直到 finished)...                    │                          │
```

4. **验证**：在图中用三种颜色/标记区分「前端进程内调用」「跨进程 ZMQ」「后台搬运任务」，确认每条箭头落在正确的进程里。

**交付物**：一张时序图 + 一段说明，指出请求与输出各自跨越进程边界的确切位置（请求在 `_send_input` 的 `input_socket.send_multipart` 处跨出；输出在 `process_outputs_socket` 的 `recv_multipart` 处跨入）。

## 6. 本讲小结

- `AsyncLLM` 是 API Server 眼中的「引擎客户端」，它实现上层接口 `EngineClient`，内部持有一个下层 IPC 客户端 `EngineCoreClient`（异步场景为 `AsyncMPClient`）——这条**进程边界**是理解在线服务路径的钥匙。
- 构造 `AsyncLLM` 时，`input_processor`/`output_processor`/`logger_manager` 都住**前端进程**，只有 `make_async_mp_client` 会拉起**后台 EngineCore 进程**。
- `add_request` 与 `generate` 分工明确：前者负责输入预处理与「双侧登记」（本进程 `OutputProcessor` + 跨进程 `engine_core.add_request_async`），返回一个每请求队列；后者只负责从队列消费并 `yield`。
- 一个**唯一的**后台 `output_handler` 任务负责把 EngineCore 输出批量拉回、分块处理、推入各请求队列，构成「单生产者 → 多消费者」模型。
- 输出从后台进程回到调用方要经过五个异步站点（收帧解码 → outputs_queue → get_output_async → process_outputs → 各请求队列 → generate）。
- 流式输入（`AsyncGenerator[StreamingInput]`）支持输入逐块喂入，靠一个 `prompt_token_ids=[0]` 的哨兵请求宣告输入流结束，且不支持 pooling/n>1/FINAL_ONLY/stop。
- 请求级元数据（如本次新增的 `session_id`）以「逐层透传」的方式从 `EngineClient.generate` → `AsyncLLM.generate/add_request` → `InputProcessor` → `EngineCoreRequest`（字段，跨进程）→ `Request.session_id` 抵达引擎，`AsyncLLM` 自身只搬运不消费。

## 7. 下一步学习建议

- **继续往下钻「执行侧」**：本讲止步于「请求发给 EngineCore 进程」。EngineCore 进程内部如何跑 busy loop、如何调度、如何调用 worker，请看 u5-l1（EngineCore 引擎核心主循环）与 u5-l2（GPU Worker）。
- **输入/输出处理器细节**：`InputProcessor` 的 tokenize 与多模态预处理、`OutputProcessor` 的 detokenize 与 logprobs，分别在 u5-l5（输入处理与 Tokenization）与 u7-l2（输出处理与解码结果）展开；`session_id` 在 `InputProcessor.process_inputs` 处汇入 `EngineCoreRequest`，正是本讲 4.3.6 与 u5-l5 的衔接点。
- **`session_id` 的下游消费**：本讲只讲「透传到 `Request.session_id`」。它在调度/后端如何被读取（会话亲和、统计）属更下游话题，可在阅读 u4-l1（请求对象与生命周期，`Request.session_id` 字段）与 u4-l2（调度器）时继续追踪。
- **数据并行下的客户端**：本讲提到的 `DPAsyncMPClient`/`DPLBAsyncMPClient` 如何在多个 EngineCore 间做负载均衡（`get_core_engine_for_request` 的打分逻辑），可结合 u9-l1（张量/流水/数据并行）阅读 [`core_client.py:L1431-L1619`](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/core_client.py#L1431-L1619)。
- **可观测性**：`output_handler` 里 `logger_manager.record(...)` 采集的指标如何暴露为 Prometheus，见 u10-l2（指标与可观测性）。
