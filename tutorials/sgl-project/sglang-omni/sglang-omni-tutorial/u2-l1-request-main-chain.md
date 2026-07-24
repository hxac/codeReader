# 请求主链路总览

## 1. 本讲目标

在 u1 里，你已经能把服务跑起来、发出第一条请求并看懂声明式配置。本讲不再深入任何单层，而是**站在高空俯瞰整条请求链路**，目标是：

- 能把一次请求从 `curl` 进、到模型前向（model forward）、再到响应出的全过程，**拆成清晰的分层**，并标注每一层「只做什么、不做什么」。
- 能清楚区分 `create_app()` 与 `launch_server()`：前者只建 FastAPI，后者管完整生命周期。这是阅读 serving 代码时最重要的一个分界点。
- 能追踪请求与响应**两条数据流**，识别其中的两次类型转换和一次提交。

学完后，你看到任何一段 serving 代码，都能立刻判断它属于哪一层、为什么放在这一层。后续 u2-l2～u2-l5 以及 u3、u4 会逐层下钻，本讲是它们的「地图」。

## 2. 前置知识

本讲假设你已经掌握（来自 u1）：

- SGLang-Omni 是面向 omni / 语音 / TTS 模型的**多阶段（multi-stage）推理服务运行时**。
- 一次生成被拆成性质迥异的异构阶段（预处理、编码器、AR 引擎、talker、解码器、vocoder、聚合等）接力完成。
- 请求主链路的骨架是：HTTP API → Client → Coordinator → Stage → Scheduler → ModelRunner → model forward。
- `sgl-omni serve` 会把 flag 合并成一份 `PipelineConfig`，再拉起整条管线。

如果你对上面任何一条感到陌生，建议先回到 u1-l1 和 u1-l4 复习。本讲只补充两个本讲要用到的术语：

- **控制平面（control plane）**：在各层之间传递「命令 / 状态」的轻量消息通道，SGLang-Omni 用 ZMQ + msgpack 实现。本讲只需知道它存在，细节在 u3-l2。
- **relay（数据平面）**：在各层之间搬运「真实大张量」的传输后端（如 CUDA IPC）。本讲只需知道它与控制平面是分开的两条路，细节在 u3-l3。

## 3. 本讲源码地图

本讲引用的关键文件如下。注意：本讲是**总览**，只引用足以说明分层与主链路的文件；每层内部的实现细节会在后续讲义展开。

| 文件 | 在本讲中的作用 |
| --- | --- |
| `docs/developer_reference/main.md` | 架构总览，给出分层职责表，是本讲「分层职责」的权威来源。 |
| `docs/developer_reference/apiserver_design.md` | API server 设计文档，给出启动路径、请求路径、`create_app` vs `launch_server`、响应路径。 |
| `sglang_omni/serve/launcher.py` | `launch_server()` / `_run_server()` 的真实实现，用来验证「完整生命周期」具体包含哪些步骤。 |
| `sglang_omni/serve/openai_api.py` | `create_app()`、`/v1/chat/completions` 路由、`_build_chat_generate_request()` 转换点的真实实现。 |
| `sglang_omni/client/client.py` | 内部 `Client` 的 `generate` / `completion` / `completion_stream`，是结果聚合层。 |
| `sglang_omni/pipeline/coordinator.py` | `Coordinator` 的 `submit` / `stream` / `_submit_request`，是请求进入管线的入口。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 分层职责** —— 整条链路有几层，每层「只做什么、不做什么」。
2. **4.2 `create_app` vs `launch_server`** —— 启动代码里最关键的一个分界点。
3. **4.3 请求 / 响应路径** —— 一次请求正反向走的两条数据流，以及其中的转换点。

### 4.1 分层职责

#### 4.1.1 概念说明

一个常见的误区是：把 HTTP 框架（FastAPI）当成整个推理服务。在 SGLang-Omni 里，HTTP 层只是最外层的「协议翻译器」。真正干活的是它下面那条多阶段管线。

SGLang-Omni 把整条链路显式地切成**六个职责层**，从外到内依次是：

```
HTTP API -> Client -> Coordinator -> Stage -> Scheduler -> ModelRunner -> model forward
```

每层都有一个明确的、不重叠的职责。文档用一张职责表把这件事钉死：

[docs/developer_reference/main.md:8-24](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/main.md#L8-L24) —— 这段给出系统总览图（第 8-11 行）和分层职责表（第 14-24 行），是本讲「分层」的权威来源。

把这张表翻译成「只做 / 不做」的对照，最容易记忆：

| 层 | 只负责（做） | 明确不负责（不做） |
| --- | --- | --- |
| **HTTP API** | OpenAI 兼容的请求 / 响应 schema、SSE 分帧、HTTP 错误码 | 不编排管线、不执行计算 |
| **Client** | `GenerateRequest`→`OmniRequest` 转换、结果聚合、音频 base64 编码 | 不决定请求进哪个 stage、不碰 GPU |
| **Coordinator** | 请求生命周期、提交到入口 stage、收集终态结果、abort 广播 | 不执行任何阶段的前向计算 |
| **Stage** | 控制平面 IO、relay IO、fan-in 聚合、stream 路由、桥接 scheduler inbox/outbox | 不自己跑模型前向（那是 Scheduler/ModelRunner 的事） |
| **Scheduler** | 单阶段执行循环、把失败传播到 stage outbox | 不解析 HTTP、不管跨 stage 拓扑 |
| **ModelRunner** | AR 前向准备、调用模型 forward、抽取输出 | 不管请求来自哪、不管响应怎么序列化 |

一个关键直觉：**外层只翻译和搬运，内层才计算**。HTTP/Client/Coordinator 基本不碰 GPU；从 Scheduler/ModelRunner 往下才真正触发 `model forward`。

#### 4.1.2 核心流程

把六层套到一次请求上，正向数据流大致是：

```
1. HTTP API     收到 OpenAI 风格 JSON，校验 schema
2. Client       转成内部请求对象，生成 request_id
3. Coordinator  把请求送进「入口 stage」
4. Stage        在阶段间搬运控制消息 / 大张量，做 fan-in
5. Scheduler    在单个阶段内执行（prefill / decode / 预处理 …）
6. ModelRunner  准备 ForwardBatch，调用模型 forward，抽取输出
```

注意第 3 步之后，请求就**进入了多阶段图**。一个请求可能要经过若干个 stage（例如 Qwen3-Omni 会经过 thinker → talker → decode / code2wav），这些 stage 间的跳转由 Stage + Coordinator 配合完成，但**对最外层的 HTTP / Client 是透明的**——外层只看到「提交进去、等终态结果」。

#### 4.1.3 源码精读

分层职责的权威定义在文档里，但你可以用源码「交叉验证」每一层确实只做表里说的事：

- HTTP 层只翻译：路由处理函数把请求交给 `_build_chat_generate_request` 转成内部对象，再把结果交给 `Client`，自己不计算。

  [sglang_omni/serve/openai_api.py:634-676](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L634-L676) —— `/v1/chat/completions` 路由：第 645 行调用 `_build_chat_generate_request` 做翻译，第 652 行根据 `stream` 分流到流式或非流式处理，全程不碰模型。

- Coordinator 只提交 + 收终态：`_submit_request` 把请求打成 `StagePayload`，通过控制平面交给入口 stage，然后等一个完成 future。

  [sglang_omni/pipeline/coordinator.py:360-417](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L360-L417) —— 第 388-392 行构造 `StagePayload`，第 402-407 行通过 `control_plane.submit_to_stage` 把请求送进 `entry_stage`，第 410 行把状态置为 `RUNNING`；Coordinator 自身不执行任何前向。

- Client 只转换 + 聚合：`generate` 把 `GenerateRequest` 经 `_build_omni_request` 转成 `OmniRequest`，再交给 Coordinator。

  [sglang_omni/client/client.py:53-69](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L53-L69) —— 第 59 行 `_build_omni_request` 做转换，第 61/68 行分别走流式 `stream` 与一次性 `submit`，Client 本身不碰 GPU。

#### 4.1.4 代码实践

**实践类型**：源码阅读型实践（巩固「目录 ↔ 层」的映射）。

1. **实践目标**：把磁盘上的目录对应到上面六层职责，证明你建立了全局地图。
2. **操作步骤**：
   - 打开 [docs/developer_reference/main.md:29-40](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/main.md#L29-L40) 的 Directory Layout。
   - 为六层职责各找一个**真实子目录**：HTTP API → `serve/`、Client → `client/`、Coordinator + Stage → `pipeline/`、Scheduler → `scheduling/`、ModelRunner → `model_runner/`。
3. **需要观察的现象**：你会发现「Coordinator」和「Stage」住在**同一个** `pipeline/` 目录里——它们都是「跨阶段编排」这一职责的组成部分，只是分工不同。
4. **预期结果**：手写一张「层 → 目录 → 代表文件」的小表。例如 Coordinator → `pipeline/coordinator.py`、Stage → `pipeline/stage/runtime.py`（Stage 的实现细节在 u3-l1）。
5. 说明：本实践无需 GPU，纯阅读。

#### 4.1.5 小练习与答案

**练习 1**：如果有人把「音频 base64 编码」的逻辑写进了 `pipeline/coordinator.py`，违反了哪条职责边界？应该放在哪一层？

> **参考答案**：违反了「Coordinator 只做请求生命周期 / 入口提交 / 终态收集 / abort」的边界。base64 编码属于「把内部结果翻译成对外格式」，应放在 **Client 层**（`Client.completion` 里确实在聚合阶段做 base64，见 `client.py` 第 131 行）。

**练习 2**：为什么「决定请求进哪个 stage」是 Coordinator 的职责，而不是 HTTP API 的职责？

> **参考答案**：因为「入口 stage」是**管线拓扑**的属性，属于运行时编排；HTTP API 层刻意对管线内部结构保持无知（它只懂 OpenAI 协议）。把拓扑知识放进 HTTP 层会把协议层和管线结构耦合死。

### 4.2 `create_app` vs `launch_server`

#### 4.2.1 概念说明

阅读 serving 代码时，你会反复遇到两个函数：`create_app()` 和 `launch_server()`。文档明确说「**This is the most important distinction in the serving code.**」（这是 serving 代码里最重要的区分）。

一句话区分：

- `create_app(client, ...)` —— **只建 FastAPI app、注册路由**。它假设你已经有一个活着的 `Client`，你想自己把 HTTP 层嵌进别的地方。
- `launch_server(pipeline_config, ...)` —— **完整的开箱即用生命周期**：从一份 `PipelineConfig` 出发，编译管线、启动运行时、建 Client、建 app、挂载 profiling 路由、跑 Uvicorn、关停时停掉运行时。

[docs/developer_reference/apiserver_design.md:39-71](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/apiserver_design.md#L39-L71) —— 文档逐条列出了 `create_app` **不做**的事（第 47-54 行）和 `launch_server` **做**的事（第 61-70 行），是本节对照表的基础。

一个直观的包含关系：

```
launch_server ⊇ create_app
（launch_server 内部会调用 create_app，但前后还包了一大圈生命周期管理）
```

#### 4.2.2 核心流程

`launch_server` 的生命周期可以拆成两个阶段：

```
启动阶段（_run_server 内）：
  1. 检查端口可用
  2. MultiProcessPipelineRunner.start()  ← 编译并启动整条多阶段管线
  3. coordinator = mp_runner.coordinator   ← 拿到 Coordinator
  4. client = Client(coordinator)          ← 建 Client
  5. app = create_app(client, ...)         ← 建 FastAPI（仅路由）
  6. _mount_profiler_routes(app, ...)      ← 挂载 /start_profile 等路由
  7. uvicorn.Server(app).serve()           ← 跑 HTTP 服务

关停阶段（finally）：
  8. mp_runner.stop()                       ← 停掉运行时
```

注意第 5 步就是 `create_app`：它在整个生命周期里只是中间一环。第 2、3、6、7、8 步都是 `create_app` **不负责**、而 `launch_server` 额外包揽的部分。

#### 4.2.3 源码精读

- `create_app` 的真实实现：建 FastAPI 实例、加中间件、把 client/model_name 存进 `app.state`、注册所有路由，然后返回 app。它**不**启动运行时、**不**跑 Uvicorn。

  [sglang_omni/serve/openai_api.py:182-271](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L182-L271) —— 第 224 行 `app = FastAPI(...)`，第 239-257 行只是把引用存进 `app.state`，第 261-271 行 `_register_*` 一系列调用注册各路由；函数返回 `app`，函数体内没有任何「启动管线 / 跑 server」的代码。

- `launch_server` 是阻塞入口，内部 `asyncio.run(_run_server(...))`。

  [sglang_omni/serve/launcher.py:451-498](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/launcher.py#L451-L498) —— `launch_server` 的 docstring（第 464-482 行）逐条列出了它管理的生命周期参数，第 483-497 行先 `apply_gpu_compat_env_defaults()` 再 `asyncio.run(_run_server(...))`。

- 真正的「完整生命周期」在 `_run_server` 里展开，可以逐行对照文档承诺的步骤：

  [sglang_omni/serve/launcher.py:343-406](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/launcher.py#L343-L406) —— 第 343-345 行 `MultiProcessPipelineRunner(...).start()`（启动管线，超时由 `SGLANG_OMNI_STARTUP_TIMEOUT` 控制，默认 600 秒），第 371 行 `client = Client(coordinator, ...)`，第 372-393 行 `create_app(...)`，第 394-396 行 `_mount_profiler_routes(...)`（这一步是 `create_app` 不做的），第 398-406 行构造并运行 `uvicorn.Server`。

- 关停语义在 `finally` 块里：

  [sglang_omni/serve/launcher.py:407-410](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/launcher.py#L407-L410) —— 无论正常退出还是异常，都 `await mp_runner.stop()` 停掉管线。这也是「完整生命周期」的一部分，`create_app` 不会有这个收尾。

#### 4.2.4 代码实践

**实践类型**：源码阅读型实践（核对文档与实现一致）。

1. **实践目标**：亲手验证文档列出的「`create_app` 不做的事」在 `_run_server` 里确实由 `launch_server` 这一路负责。
2. **操作步骤**：
   - 对照 [docs/developer_reference/apiserver_design.md:47-54](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/apiserver_design.md#L47-L54) 的「`create_app` 不做」清单：compile pipeline / start runtime / create coordinator / mount profiling routes / run Uvicorn。
   - 在 [sglang_omni/serve/launcher.py:343-406](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/launcher.py#L343-L406) 里为这五条各找到一行代码作为证据。
3. **需要观察的现象**：五条「不做」的事，在 `_run_server` 中都有对应的一行；而 `create_app` 函数体（openai_api.py 第 182-271 行）里搜不到这五条中的任何一条。
4. **预期结果**：得到一张「文档承诺 → launcher.py 行号」的对照表，例如 mount profiling routes → 第 396 行 `_mount_profiler_routes(app, ...)`。
5. 说明：纯阅读，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：你想在自己的测试脚本里，用一个**已经手动构造好的 `Client`** 对象临时起一个 HTTP 服务做调试，应该调 `create_app` 还是 `launch_server`？为什么？

> **参考答案**：调 `create_app(client, ...)`。因为 `launch_server` 会从 `PipelineConfig` 重新编译并启动一整条管线，会覆盖你已经构造好的 Client 与运行时；`create_app` 假设 Client 已就绪，只负责建路由，正好符合「只想嵌一个 HTTP 层」的场景。

**练习 2**：文档说 profiling 路由（`/start_profile` 等）由「单进程 `launch_server` 路径」挂载，多进程 launcher 路径不挂载。结合源码，挂载动作具体发生在哪一行？

> **参考答案**：发生在 [sglang_omni/serve/launcher.py:396](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/launcher.py#L396) 的 `_mount_profiler_routes(app, profiler_ctl, profiler_dir)`。`create_app` 不挂这些路由，所以单独用 `create_app` 起服务时没有 profiling 端点。

### 4.3 请求 / 响应路径

#### 4.3.1 概念说明

现在把 4.1 的分层和 4.2 的启动知识合起来，看一次请求**正反向**到底怎么走。文档用两句话概括了请求路径和职责切分：

[docs/developer_reference/apiserver_design.md:11-23](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/apiserver_design.md#L11-L23) —— 第 13 行是**启动路径**（CLI → PipelineConfig → Pipeline Startup → Coordinator → Client → FastAPI），第 17 行是**请求路径**（HTTP request → FastAPI route → Client → Coordinator → Stage pipeline → Client aggregation → HTTP/SSE response），第 19-23 行点明三者职责切分。

一个极其重要的设计原则：**服务器不把 OpenAI 风格的请求体直接喂给运行时**，而是先转换成内部请求对象（第 113 行 "The server does not pass OpenAI-style request bodies straight into the runtime."）。请求在到达 Coordinator 之前，要经过**两次类型转换**：

```
ChatCompletionRequest  ──(HTTP 层)──▶  GenerateRequest  ──(Client 层)──▶  OmniRequest
```

- 第一次转换在 HTTP 层（`_build_chat_generate_request`）：把 OpenAI schema 翻译成内部 `GenerateRequest`。
- 第二次转换在 Client 层（`_build_omni_request`）：把 `GenerateRequest` 翻译成管线真正认识的 `OmniRequest`。

之所以拆成两次，是为了**解耦**：HTTP 层只懂 OpenAI 协议、不懂管线内部结构；Client 层只懂「内部请求 ↔ 管线」、不懂 HTTP 协议。任何一层换了，另一层都不用改。

#### 4.3.2 核心流程

**请求路径（正向）**，以一次流式 chat completion 为例：

```
curl POST /v1/chat/completions  (OpenAI JSON)
  │
  ▼  [第 1 次转换]
FastAPI 路由 chat_completions() ──_build_chat_generate_request──▶ GenerateRequest
  │
  ▼
Client.completion_stream() ──generate()──┐
                                         │ [第 2 次转换]
                          _build_omni_request ──▶ OmniRequest
                                         │
                                         ▼  [1 次提交]
                          Coordinator.stream(req_id, omni_request)
                                         │
                                         ▼
                          _submit_request → 控制平面 → 入口 stage
                                         │
                                         ▼
                          (多阶段图内部接力：Stage → Scheduler → ModelRunner → forward)
```

**响应路径（反向）**，结果原路返回但在 **Client 层做聚合**：

```
终态结果 / 流式片段  ──▶  Coordinator（收集 / 合并多终态 / 推流）
                          │
                          ▼
                  Client.generate() 逐片段 yield
                          │
                          ├─ 非流式：completion() 聚合 text/audio/usage → JSON
                          └─ 流式：completion_stream() 包成 SSE delta
                          │
                          ▼
                  FastAPI → StreamingResponse / JSONResponse → curl
```

两个要点：

1. **聚合发生在 Client 层，不在 HTTP 层**：`Client.completion()` 会累积文本片段、拼接音频块、做 base64 编码（见 4.1.5 的答案）。HTTP 层拿到的已经是聚合好的结果或成型的 SSE 片段。
2. **一条管线可有多个终态**：例如文本收口 `decode` 和语音收口 `code2wav`。Coordinator 要等**所有**期望终态都完成才算请求结束——这正是 `stream()` 里那段「completed_stages >= expected_terminal_stages」判断的作用。

#### 4.3.3 源码精读

- **第 1 次转换（HTTP 层）**：`_build_chat_generate_request` 把 OpenAI 字段翻译成内部 `SamplingParams` / `Message`，并把多模态输入塞进 `metadata`。

  [sglang_omni/serve/openai_api.py:886-955](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L886-L955) —— 第 896-907 行建 `SamplingParams`，第 910 行把 messages 转成内部 `Message`，第 936-954 行把 `audios/images/videos/video_*` 等多模态输入归并进 `metadata`。返回的是 `GenerateRequest`。

- **路由分流**：路由拿到 `GenerateRequest` 后，按 `stream` 字段决定走流式 `StreamingResponse` 还是非流式 `_chat_non_stream`。

  [sglang_omni/serve/openai_api.py:645-676](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L645-L676) —— 第 645 行完成第 1 次转换，第 652-665 行流式分支返回 `StreamingResponse(media_type="text/event-stream")`，第 667-676 行非流式分支调用 `_chat_non_stream`（其内部第 691 行调用 `client.completion`）。

- **第 2 次转换 + 1 次提交（Client 层）**：`generate` 决定走流式 `coordinator.stream` 还是一次性 `coordinator.submit`。

  [sglang_omni/client/client.py:53-69](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L53-L69) —— 第 58 行生成 `request_id`（缺省时用 `uuid.uuid4()`），第 59 行 `_build_omni_request` 完成第 2 次转换，第 61 行流式 `self._coordinator.stream`、第 68 行非流式 `self._coordinator.submit`。

- **入口提交（Coordinator 层）**：`submit` 先 `_submit_request` 把请求送进入口 stage，再等完成 future。

  [sglang_omni/pipeline/coordinator.py:316-325](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L316-L325) —— `submit` 第 318 行送请求、第 320-322 行 `await future` 等终态结果，第 324-325 行 `finally` 清理 future。

- **流式多终态合并（Coordinator 层）**：`stream` 边收消息边判断是否所有期望终态都到了。

  [sglang_omni/pipeline/coordinator.py:327-358](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L327-L358) —— 第 339 行算出 `expected_terminal_stages`，第 342-353 行循环取消息：`CompleteMessage` 累计进 `completed_stages`，直到「覆盖所有期望终态」才 `return` 结束流；`StreamMessage` 直接 `yield` 透传给上层。

- **响应聚合（Client 层）**：非流式 `completion` 累积文本、拼接音频、base64 编码。

  [sglang_omni/client/client.py:75-153](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L75-L153) —— 第 100-116 行迭代 `generate()` 累积 `text_parts` / `audio_chunks` / `finish_reason` / `usage`，第 124-140 行把音频块拼成一段并 `audio_to_base64`，第 142-153 行组装成 `CompletionResult`。这正是「聚合发生在 Client 层」的实证。

#### 4.3.4 代码实践

**实践类型**：源码阅读型实践（追踪两次转换 + 一次提交）。

1. **实践目标**：在源码里精确标注「两次类型转换」和「一次提交」分别发生在哪一行，从而彻底吃透请求正向路径。
2. **操作步骤**：
   - 从 [sglang_omni/serve/openai_api.py:645](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L645) 出发，确认第 1 次转换 `ChatCompletionRequest → GenerateRequest`。
   - 跟到 [sglang_omni/client/client.py:59](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L59)，确认第 2 次转换 `GenerateRequest → OmniRequest`。
   - 跟到 [sglang_omni/client/client.py:61](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L61) 与 [sglang_omni/pipeline/coordinator.py:402-407](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L402-L407)，确认那「一次提交」是经控制平面进入入口 stage。
3. **需要观察的现象**：你会看到请求对象的类型沿路变化 `ChatCompletionRequest → GenerateRequest → OmniRequest → StagePayload`，每变一次都对应一次「职责层切换」。
4. **预期结果**：写出一条带行号的调用链：
   `openai_api.py:645 → client.py:59 → client.py:61/68 → coordinator.py:402-407`。
5. 说明：纯阅读，无需运行；如果你本地已按 u1-l4 起过服务，可在 `_build_chat_generate_request` 和 `_build_omni_request` 各加一行 `logger.info(type(...))` 观察（属于可选验证，标注为「待本地验证」）。

#### 4.3.5 小练习与答案

**练习 1**：为什么把 `ChatCompletionRequest → GenerateRequest` 的转换放在 HTTP 层，而不是放进运行时层统一做？

> **参考答案**：因为 `ChatCompletionRequest` 是 OpenAI 协议特有的，而运行时（Client 之下）刻意对 HTTP 协议保持无知。在 HTTP 层就翻译成与协议无关的 `GenerateRequest`，能让运行时既可被 HTTP 服务调用，也可被内部 Client / 测试 / 其他入口直接调用，实现协议层与运行时解耦。

**练习 2**：一次同时输出文本和语音的请求，可能有两个终态（`decode` 与 `code2wav`）。Coordinator 怎么知道「请求结束了」？

> **参考答案**：通过 [sglang_omni/pipeline/coordinator.py:339-353](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L339-L353)：先算出 `expected_terminal_stages`，每收到一个 `CompleteMessage` 就把它加入 `completed_stages`，只有当已完成集合**覆盖全部期望终态**时才结束流。这保证多终态结果都被收齐才返回。

**练习 3**：非流式请求里，音频的 base64 编码在哪一层做？为什么不在 Stage / Scheduler 里做？

> **参考答案**：在 Client 层做（`client.py` 第 131-135 行 `audio_to_base64`）。因为 base64 是「把内部张量翻译成对外传输格式」的协议侧工作，属于 Client 的「结果聚合 / 音频编码」职责；Stage / Scheduler 只负责计算和搬运，不应关心对外序列化格式。

## 5. 综合实践

**任务**：绘制一张「流式 chat completion 从 `curl` 到首个 token」的**请求时序图**，把本讲三个模块的知识串起来。

要求：

1. 画出请求正向经过的**五个组件**，按顺序：① FastAPI 路由（HTTP API 层）、② `Client`、③ `Coordinator`、④ `Stage`、⑤ `Scheduler`（其内部调用 ModelRunner 触发 `model forward`，产生首个 token）。
2. 在时序图上**标注两次类型转换**的位置：`ChatCompletionRequest → GenerateRequest`（①→② 之间，发生在 HTTP 层）、`GenerateRequest → OmniRequest`（②内部）。
3. 标注**一次提交**的位置：`Coordinator` 经控制平面把请求送进入口 `Stage`。
4. 为五个组件各写**一句话**「只负责什么」，直接套用 4.1 的职责表。例如：① FastAPI 路由——只做 OpenAI schema 校验与请求翻译，不碰管线。
5. 在图侧用一句话点出 `launch_server` 相比 `create_app` 多做了哪件最关键的事（答案：启动并管理整条多阶段管线的生命周期）。

**作图建议**：用纯文本的纵向时序图即可（组件作为纵向生命线，箭头表示消息），也可用纸笔手画。重点是**标注准确**而非美观。

**预期结果**：一张能用来向同事讲解「为什么请求要经过这五层、为什么有两次转换」的图。完成后，你应该能脱图回答：*curl 的 JSON 在哪一行变成了 `GenerateRequest`？又在哪一行变成了 `OmniRequest`？*（答案见 4.3.4 与 4.3.5）

## 6. 本讲小结

- SGLang-Omni 把请求链路显式切成六层：**HTTP API → Client → Coordinator → Stage → Scheduler → ModelRunner**，外层只翻译 / 搬运，内层才计算。
- 每层有清晰不重叠的职责（见 4.1 对照表），核心直觉是「**HTTP/Client/Coordinator 基本不碰 GPU**」。
- serving 代码最重要的区分是 **`create_app`（只建 FastAPI 路由）vs `launch_server`（完整生命周期：编译管线 → 启动运行时 → 建 Client → 建 app → 挂 profiling → 跑 Uvicorn → 关停）**，后者包含前者。
- 请求正向有**两次类型转换**（`ChatCompletionRequest → GenerateRequest → OmniRequest`）和**一次提交**（Coordinator 经控制平面送进入口 stage）。
- 响应的**聚合发生在 Client 层**（累积文本、拼接音频、base64 编码），不在 HTTP 层。
- 一条管线可有**多个终态**（如 `decode` + `code2wav`），Coordinator 要等全部期望终态完成才结束请求。

## 7. 下一步学习建议

本讲是 u2 单元「追踪请求主链路」的地图。建议按主链路自外向内继续下钻：

- **下一讲 u2-l2（OpenAI 兼容 API 服务层）**：深入 `openai_api.py` 的路由表面、`_build_chat_generate_request` 的字段映射细节、以及 SSE 流式语义。是本讲 4.3「HTTP 层」的展开。
- **u2-l3（内部 Client 客户端层）**：深入 `Client` 的 `generate / completion / completion_stream` 与 `OmniRequest` 构造，是本讲「Client 层」的展开。
- **u2-l4（Coordinator 协调器）**：深入 Coordinator 的请求状态机与多终态合并，是本讲「Coordinator 层」的展开。
- **u2-l5（声明式配置）**：从 u1-l5 的配置概念进入 `PipelineConfig / StageConfig` 的字段细节。

如果你更想先理解「Stage 之间到底怎么接力」，可以跳到 **u3-l1（Stage 抽象与 IO 外壳）**；想理解「Scheduler/ModelRunner 怎么真正跑模型」，跳到 **u4-l1 / u4-l2**。但建议先把 u2 的四篇读完，把外层三层吃透，再进 u3/u4 的内部机制，这样不会在拓扑细节里迷路。
