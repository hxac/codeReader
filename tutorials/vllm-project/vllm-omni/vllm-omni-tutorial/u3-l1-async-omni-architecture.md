# AsyncOmni 与 AsyncOmniEngine：多阶段架构总览

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 vLLM-Omni 多阶段运行时的**五层架构**（API / Engine 代理 / Orchestration / Communication / Execution），并能指出每层对应的关键类。
- 解释 `AsyncOmniEngine` 为什么是一个「薄代理（thin proxy）」，以及 `request_queue` / `output_queue`（`janus.Queue`）如何在**主线程**与**Orchestrator 后台线程**之间传递消息。
- 区分同步入口 `Omni`、异步入口 `AsyncOmni` 与共享基类 `OmniBase` 三者的职责，理解 `_run_generation`（阻塞轮询）与 `_final_output_handler`（后台协程派发）两种取输出方式。
- 跟踪一次 `generate` 请求从 `add_request` 到 `yield OmniRequestOutput` 的 **8 步执行流**，并为每一步标注它发生在哪个文件 / 类。

本讲是进阶层 U3（多阶段运行时与编排）的**总览**，后续 u3-l2（Orchestrator）、u3-l3（阶段进程与运行时）、u3-l4（OmniConnector）会分别深入某一层。本讲负责先建立全局视图。

## 2. 前置知识

阅读本讲前，建议你已经掌握以下概念（来自入门层 u1 和核心抽象 u2）：

- **stage（阶段）**：vLLM-Omni 把一个复杂全模态模型拆成若干顺序执行的子任务，每个子任务就是一个 stage。例如 Qwen3-Omni 被拆成 Thinker、Talker、Code2wav 三个 stage（见 [examples/offline_inference/qwen3_omni/end2end.py:333-337](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/offline_inference/qwen3_omni/end2end.py#L333-L337)）。
- **AR 与 Diffusion**：自回归（Autoregressive，逐 token 生成）与扩散（Diffusion，逐步去噪生成图像/音频 latent）。一个 stage 的类型由 `stage_type` 标记，常见取值是 `"llm"`（含 `"llm_ar"` / `"llm_generation"`）和 `"diffusion"`。
- **OmniPromptType / OmniRequestOutput**：见 u2-l3，分别是统一输入与统一输出容器。
- **janus.Queue**：一个跨「同步线程 ↔ 异步事件循环」的双端队列。它有一个 `sync_q`（供普通线程用阻塞 `put/get`）和一个 `async_q`（供 asyncio 协程用 `await put/get`）。两端操作的是**同一个**底层队列，这正是它能跨线程/协程通信的原因。
- **进程 vs 线程 vs 协程**：Orchestrator 跑在一个后台**线程**里并拥有自己的 asyncio **事件循环**；每个 stage 的执行体（EngineCore / DiffusionEngine）通常是独立的**子进程**，靠 ZMQ IPC 通信。

如果你对 vLLM 本身的 EngineCore / InputProcessor / OutputProcessor 还不熟悉也没关系，本讲会在用到时点明。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [docs/design/module/async_omni_architecture.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/module/async_omni_architecture.md) | 官方架构设计文档，含五层架构图、8 步执行流、时序图。是本讲的「骨架」。 |
| [vllm_omni/entrypoints/omni_base.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/omni_base.py) | `OmniBase`：`Omni` 与 `AsyncOmni` 的共享基类，负责构造引擎、计算 final stage、处理输出消息。 |
| [vllm_omni/entrypoints/omni.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/omni.py) | `Omni`：同步离线入口，用阻塞 `while` 循环轮询输出。 |
| [vllm_omni/entrypoints/async_omni.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/async_omni.py) | `AsyncOmni`：异步在线/流式入口，用后台协程派发输出。 |
| [vllm_omni/engine/async_omni_engine.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/async_omni_engine.py) | `AsyncOmniEngine`：薄代理，在调用方线程里做 stage-0 预处理，并经 `janus.Queue` 与后台 Orchestrator 线程通信。 |
| [vllm_omni/entrypoints/client_request_state.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/client_request_state.py) | `ClientRequestState`：跟踪单个入口请求及其 `asyncio.Queue`。 |
| [vllm_omni/engine/messages.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/messages.py) | 队列消息类型（`StageSubmissionMessage`、`OutputMessage`、`ErrorMessage` 等）。 |

> 提示：Orchestrator 的内部实现（`vllm_omni/engine/orchestrator.py`）是 u3-l2 的主题，本讲只在「执行流」里指出它的方法位置，不展开其编排细节。

## 4. 核心概念与源码讲解

### 4.1 五层架构与组件职责

#### 4.1.1 概念说明

把一次全模态请求（例如「文本 → 音频」）跑完，需要五层协作：

1. **API 层**：用户直接接触的入口。同步是 `Omni`（离线脚本），异步是 `AsyncOmni`（在线服务、流式）。它们都继承 `OmniBase`。
2. **Engine 代理层**：`AsyncOmniEngine`。它**本身不做推理**，只负责「把 stage-0 的输入预处理完，然后打包成消息丢给后台线程」，以及「从后台线程把输出消息取回来」。所以文档称它为 *thin proxy*（薄代理）。
3. **Orchestration（编排）层**：`Orchestrator`，跑在一个后台**线程**里。它持有所有 stage 的客户端，负责把一个 stage 的输出**路由**并**前推**到下一个 stage，直到最终 stage 完成。
4. **Communication（通信）层**：每个 stage 对应一个客户端（`StageEngineCoreClient` 或 `StageDiffusionClient`），用 ZMQ ROUTER/PULL + msgpack 与 stage 子进程通信。
5. **Execution（执行）层**：真正加载模型、跑推理的子进程（`StageCoreProc` / `DiffusionEngine`）。

这五层之所以要拆开，是为了**解耦**：编排逻辑（谁先谁后、何时前推）与执行逻辑（怎么跑 transformer / diffusion）互不干扰；而且每个 stage 可以是独立进程，从而支持多阶段流水线重叠、跨节点部署等高级特性（后续讲义展开）。

#### 4.1.2 核心流程

下图节选自官方设计文档，是本讲的「导航地图」（建议对照原图阅读）：

[docs/design/module/async_omni_architecture.md:5-47](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/module/async_omni_architecture.md#L5-L47) —— 这段 `text` 代码块画出了从 API 层到 Execution 层的完整五层框图，并标出了 `request_queue` / `output_queue`（janus.Queue）两个跨层通道。

把它抽象成数据流，一条请求在层间的走向是：

```text
用户
 │  generate(prompt)
 ▼
API 层 (Omni / AsyncOmni)
 │  add_request(...)
 ▼
Engine 代理层 (AsyncOmniEngine)          ── 做 stage-0 预处理 ──►  request_queue (janus.sync_q)
 │                                                                       │
 ▼                                                                       ▼
                                                          Orchestrator 后台线程 (Orchestration 层)
                                                                  │  stage_clients[i].add_request_async
                                                                  ▼
                                                          Communication 层 (ZMQ + msgpack)
                                                                  │  IPC
                                                                  ▼
                                                          Execution 层 (stage 子进程)
                                                                  │  推理结果
                                                                  ▼
                                                          output_queue (janus)  ◄── 回程
 ▼
API 层读取 output_queue，yield OmniRequestOutput 给用户
```

关键点：**请求是「下沉」到子进程执行的，输出再「上浮」回 API 层**，中间所有跨层都靠队列/消息，没有任何直接的函数返回值链。

#### 4.1.3 源码精读

API 层的类定义直接体现了分层职责。`AsyncOmni` 同时实现 vLLM 的 `EngineClient` 协议和 omni 的 `OmniBase`：

[entrypoints/async_omni.py:116-117](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/async_omni.py#L116-L117) —— `class AsyncOmni(EngineClient, OmniBase)`：注意它**不是**继承 `AsyncOmniEngine`，而是「持有」一个 `self.engine`。这就是「代理」而非「继承」的关系。

`OmniBase.__init__` 里构造这个被代理的引擎实例：

[entrypoints/omni_base.py:178-186](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/omni_base.py#L178-L186) —— 构造 `AsyncOmniEngine` 并赋给 `self.engine`。无论是同步的 `Omni` 还是异步的 `AsyncOmni`，底层都是同一个引擎。

Orchestrator 后台线程的启动入口在 `_bootstrap_orchestrator` 里，它 new 出一个事件循环、初始化各 stage，然后 `await orchestrator.run()`：

[engine/async_omni_engine.py:381-431](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/async_omni_engine.py#L381-L431) —— 注意第 413-428 行构造 `Orchestrator(...)` 时，把 `self.request_queue.async_q` 和 `self.output_queue.async_q` 传了进去：这就是「janus 的异步端归 Orchestrator 用」的接线点。

#### 4.1.4 代码实践

1. **实践目标**：用肉眼把五层架构图与真实源码一一对应。
2. **操作步骤**：
   - 打开 [async_omni_architecture.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/module/async_omni_architecture.md) 的第 5–47 行架构图。
   - 针对图中每一个方框（`AsyncOmni`、`Omni`、`AsyncOmniEngine`、`Orchestrator`、`StageEngineCoreClient`、`StageCoreProc`），在源码里找到它的 `class` 定义行。
3. **需要观察的现象**：图中 Communication 层画了 `StageEngineCoreClient` 与 `StageDiffusionClient` 两种客户端；它们对应 AR stage 与 Diffusion stage 的不同通信方式。
4. **预期结果**：你应当能写出一张「方框名 → 文件:类定义行」对照表。例如 `Orchestrator` → `vllm_omni/engine/orchestrator.py`。
5. 待本地验证（无需运行，纯源码阅读）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AsyncOmni` 选择「持有 `self.engine`」而不是「继承 `AsyncOmniEngine`」？

> **参考答案**：`AsyncOmni` 是面向用户的 API，需要实现 vLLM 的 `EngineClient` 协议并提供 `generate()`；而 `AsyncOmniEngine` 是内部代理，职责是「预处理 + 跨线程通信」。两者关注点不同，组合（has-a）比继承（is-a）更清晰，也避免了把引擎的内部细节（队列、线程）暴露到 API 表面。

**练习 2**：架构图里 `request_queue` 和 `output_queue` 各自承载数据的**方向**是什么？

> **参考答案**：`request_queue` 是「下行」——API/代理层把新请求（`StageSubmissionMessage`、`AbortRequestMessage` 等）丢给 Orchestrator；`output_queue` 是「上行」——Orchestrator 把 stage 产出的结果（`OutputMessage`、`ErrorMessage`、`StageMetricsMessage`）送回 API 层。

---

### 4.2 AsyncOmniEngine：薄代理、janus 队列与 add_request / try_get_output

#### 4.2.1 概念说明

`AsyncOmniEngine` 是整个架构里**最容易被误解**的类：名字里有 "Engine"，但它自己**完全不跑模型**。它的全部工作是：

- 在**调用方所在线程**（主线程）里，对 stage-0 的输入做预处理（分词、多模态处理等），因为这一步依赖较重的 tokenizer/processor，提前在主线程做可以减少一次跨线程往返。
- 把预处理后的请求打包成 `StageSubmissionMessage`，放进 `request_queue.sync_q`。
- 从 `output_queue.sync_q` 取 Orchestrator 回送的输出消息。

为什么要这样设计？因为 Orchestrator 跑在另一个线程的独立事件循环里，**不能直接调用**它的协程方法。`janus.Queue` 提供了「同步端 ↔ 异步端」的桥：主线程往 `sync_q` 放，Orchestrator 协程从 `async_q` 取；反过来 Orchestrator 往 `async_q` 放，主线程从 `sync_q` 取。

#### 4.2.2 核心流程

`AsyncOmniEngine` 的请求/输出接口可以概括为两对：

```text
下行（请求）：
  add_request(...)            # 同步，主线程调用
  add_request_async(...)      # 异步包装，内部仍调 add_request
        │
        ▼
  _build_add_request_message  # stage-0 预处理：InputProcessor.process_inputs
        │
        ▼
  request_queue.sync_q.put(StageSubmissionMessage)
        │  (跨线程)
        ▼
  Orchestrator._request_handler  从 request_queue.async_q 取

上行（输出）：
  Orchestrator 把结果 put 进 output_queue.async_q
        │  (跨线程)
        ▼
  try_get_output()           # 同步：output_queue.sync_q.get(timeout=)
  try_get_output_async()     # 异步：output_queue.sync_q.get_nowait()
```

注意一个细节：`try_get_output_async()` 名字里带 `async`，但它读的依然是 `sync_q`（用 `get_nowait`），只是被 `await` 调度。这是因为 `output_queue` 的异步端被 Orchestrator 占用了，API 侧只能从同步端读。

#### 4.2.3 源码精读

类的 docstring 一句话点明了它的定位：

[engine/async_omni_engine.py:122-135](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/async_omni_engine.py#L122-L135) —— *「Thin proxy that launches an Orchestrator in a background thread … communicates with it via janus queues (sync side for callers, async side for orchestrator).」*

三个 janus 队列在 `__init__` 里被**预先**构造（而不是延迟到 Orchestrator 线程里），原因是注释里说的：主进程的 ZMQ ROUTER 线程在 `on_register` 触发时必须能看到非空的 `request_queue`：

[engine/async_omni_engine.py:260-267](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/async_omni_engine.py#L260-L267) —— `request_queue`、`output_queue`、`rpc_output_queue` 三个 `janus.Queue`。

**下行接口 `add_request`** 的核心是 `_build_add_request_message`，它对 stage-0 做输入预处理：

[engine/async_omni_engine.py:716-758](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/async_omni_engine.py#L716-L758) —— 注意第 702 行的条件 `if stage_type != "diffusion" and not isinstance(prompt, EngineCoreRequest)`：只有 LLM 类型的 stage-0 才在主线程跑 `InputProcessor.process_inputs`（第 718 行）；diffusion stage 不走这条路径。第 738 行 `upgrade_to_omni_request` 把被 InputProcessor 丢弃的 omni 扩展字段（`prompt_embeds` / `additional_information`，见 u2-l3）捡回来。

预处理完成后，`add_request` 把消息塞进队列：

[engine/async_omni_engine.py:1325-1341](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/async_omni_engine.py#L1325-L1341) —— `add_request` 调 `_build_add_request_message` 构造消息，然后 `self.request_queue.sync_q.put(msg)`。

**上行接口 `try_get_output` / `try_get_output_async`**：

[engine/async_omni_engine.py:1700-1716](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/async_omni_engine.py#L1700-L1716) —— 同步版用 `sync_q.get(timeout=)`，异步版用 `sync_q.get_nowait()`。两者都从 `output_queue` 读；当队列为空且 Orchestrator 线程已死时，抛 `RuntimeError("Orchestrator died unexpectedly…")`。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证「主线程放、Orchestrator 线程取」的跨线程队列语义。
2. **操作步骤**（源码阅读型）：
   - 在 [async_omni_engine.py:1341](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/async_omni_engine.py#L1341) 的 `request_queue.sync_q.put(msg)` 处，确认它运行在调用方线程。
   - 用 Grep 在 `orchestrator.py` 里找到从 `request_queue.async_q` 取消息的代码（提示：`_request_handler`，[orchestrator.py:584](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L584)）。
3. **需要观察的现象**：放和取分别用 `sync_q` 和 `async_q`，但操作的是同一个 `janus.Queue` 对象。
4. **预期结果**：能画出「`AsyncOmniEngine`（主线程）─sync_q→ 共享队列 ─async_q→ `Orchestrator`（后台线程事件循环）」的图。
5. 待本地验证（纯源码阅读）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 diffusion 类型的 stage-0 不在主线程跑 `InputProcessor.process_inputs`？

> **参考答案**：diffusion stage 接收的是 `OmniDiffusionSamplingParams`（尺寸/步数/CFG 等，见 u1-l4）和原始 prompt dict，它的「预处理」是 pipeline 内部的 `encode_prompt` / `prepare_latents`，发生在 worker 子进程里（见 u5-l3）。主线程没有 diffusion pipeline，自然无法提前处理；而且 diffusion 的输入结构（单条 prompt）也无需 LLM 式的分词。代码用 `stage_type != "diffusion"` 把它排除在外。

**练习 2**：`try_get_output_async()` 为什么要用 `get_nowait()` 而不是 `await async_q.get()`？

> **参考答案**：`output_queue` 的 `async_q` 已经被 Orchestrator 后台线程的事件循环占用（Orchestrator 用它 `put` 输出）。同一个 `janus.Queue` 的 `async_q` 绑定到的是「第一个 await 它的事件循环」，也就是 Orchestrator 的循环。API 侧若再 `await async_q.get()`，会和 Orchestrator 争抢同一端，行为未定义。所以 API 侧只能从 `sync_q` 用非阻塞 `get_nowait()`，由调用方的协程自行做轮询/休眠。

---

### 4.3 OmniBase 的共享逻辑：_run_generation、_compute_final_stage_id 与输出派发

#### 4.3.1 概念说明

`OmniBase` 是 `Omni` 和 `AsyncOmni` 的**共同地基**，把两者都需要的能力抽到一起：

- 构造 `AsyncOmniEngine`、维护 `request_states`（每个入口请求一个 `ClientRequestState`）。
- **计算最终 stage**：`_compute_final_stage_id` —— 一个多阶段流水线里，哪个 stage 的输出才是要还给用户的「最终结果」？这取决于请求要的输出模态（text/audio/image…）。
- **统一处理输出消息**：`_handle_output_message` 负责把 `OutputMessage` / `ErrorMessage` / `StageMetricsMessage` 分类分发；`_process_single_result` 把一条结果组装成用户可见的 `OmniRequestOutput`。

`Omni`（同步）和 `AsyncOmni`（异步）的差异，**只在于「如何把输出消息取出来」**：

- `Omni._run_generation`：在主线程里用一个阻塞 `while active_reqs:` 循环，反复调 `engine.try_get_output()` 同步拉取。
- `AsyncOmni`：起一个后台协程 `_final_output_loop`，把 `try_get_output_async()` 拉到的消息**按 request_id 路由**到每个请求自己的 `asyncio.Queue`；`generate()` 协程则从自己那条队列里 `await get()`，从而支持多个请求并发与流式。

这种「共享处理逻辑 + 不同的取数方式」是本模块最重要的设计直觉。

#### 4.3.2 核心流程

**同步 `Omni._run_generation` 的主循环**：

```text
for 每条 prompt:
    engine.add_request(req_id, prompt, sampling_params_list, final_stage_id)
    request_states[req_id] = ClientRequestState(req_id)

while 还有活跃请求:
    msg = engine.try_get_output()                 # 阻塞拉一条
    _handle_output_message(msg)                   # 分类（共享逻辑）
    output = _process_single_result(msg, ...)     # 组装 OmniRequestOutput（共享逻辑）
    yield output
    if msg.finished: 标记该请求完成
```

**异步 `AsyncOmni` 的取数方式**（后台协程 + 每请求队列）：

```text
_final_output_handler() 启动一次后台协程 _final_output_loop:
    while True:
        msg = await engine.try_get_output_async()
        ... 分类（ACK / duplex / ErrorMessage）...
        _handle_output_message(msg)               # 共享逻辑
        await req_state.queue.put(msg)            # 按 request_id 路由到每请求 asyncio.Queue

generate() 协程:
    engine.add_request_async(...)
    async for output in _process_orchestrator_results(req_id):
        # 内部 await req_state.queue.get()，再 _process_single_result
        yield output
```

两者的「业务逻辑」`_handle_output_message` / `_process_single_result` 完全相同，区别只在 I/O 模型。

#### 4.3.3 源码精读

`OmniBase` 计算最终 stage 的入口：

[entrypoints/omni_base.py:339-355](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/omni_base.py#L339-L355) —— `_compute_final_stage_id` 委托给 `get_final_stage_id_for_e2e`，根据请求声明的输出模态，在所有 stage 的 metadata（`final_output` / `final_output_type`）里找出最终输出 stage。例如「text→audio」请求，最终 stage 通常是产出 audio 的 Code2wav/vocoder stage。

`OmniBase._handle_output_message` 是**两类入口共享**的消息分类器：

[entrypoints/omni_base.py:372-424](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/omni_base.py#L372-L424) —— 它先处理 `StageMetricsMessage`、`ErrorMessage`，再把 `OutputMessage` 关联到 `request_states[req_id]`，更新指标时间戳。返回值是一个四元组 `(should_continue, req_id, stage_id, req_state)`，调用方据此决定是跳过还是继续组装结果。

同步入口 `Omni._run_generation` 的阻塞轮询循环：

[entrypoints/omni.py:170-200](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/omni.py#L170-L200) —— `while active_reqs:` 里 `msg = self.engine.try_get_output()`（第 171 行），随后调共享的 `_handle_output_message`（173 行）与 `_process_single_result`（185 行）。注意第 196 行 `if ... msg.finished: active_reqs.discard(req_id)` 是循环的退出条件。

异步入口 `AsyncOmni` 的后台派发协程：

[entrypoints/async_omni.py:862-921](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/async_omni.py#L862-L921) —— `_final_output_handler` 只在第一次 `generate()` 时启动一次 `_final_output_loop`（幂等，见 868-869 行）。循环里 `await engine.try_get_output_async()`（877 行），把消息按 `request_id` 路由到对应 `ClientRequestState.queue`（921 行 `await req_state.queue.put(msg)`）。

`AsyncOmni.generate` 从每请求队列读取并 yield：

[entrypoints/async_omni.py:792-858](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/async_omni.py#L792-L858) —— `_process_orchestrator_results` 内部 `result = await req_state.queue.get()`（811 行），再调共享的 `_process_single_result`（836 行），`yield output_to_yield`（854 行）；`if result.finished: break`（857 行）是退出条件。

`ClientRequestState` 就是「每请求一个 `asyncio.Queue`」的载体：

[entrypoints/client_request_state.py:6-18](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/client_request_state.py#L6-L18) —— 它跟踪 `request_id`、当前 `stage_id`、指标，以及那条 `asyncio.Queue`（第 18 行）。

#### 4.3.4 代码实践

1. **实践目标**：对比同步与异步两条「取输出」路径，体会共享逻辑的复用。
2. **操作步骤**：
   - 打开 [omni.py:170-200](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/omni.py#L170-L200)（同步）与 [async_omni.py:862-921](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/async_omni.py#L862-L921)（异步）。
   - 在两处分别圈出「调用 `_handle_output_message`」和「调用 `_process_single_result`」的行。
3. **需要观察的现象**：两处调的是**同名同参**的 `OmniBase` 方法，没有任何重复实现。
4. **预期结果**：你能用一句话概括差异——「同步直接同步轮询 `output_queue`；异步多了一层后台协程把消息分发到每请求 `asyncio.Queue`」。
5. 待本地验证（纯源码阅读）。

#### 4.3.5 小练习与答案

**练习 1**：`AsyncOmni._final_output_handler` 为什么用 `if self.final_output_task is not None: return` 做幂等？

> **参考答案**：可能有多个并发请求同时调 `generate()`，每个都会调 `_final_output_handler()`。但全局只需要**一个**后台派发协程来读 `output_queue` 并按 `request_id` 路由。幂等保证只启动一次，后续调用直接返回，避免多个协程争抢同一个 `output_queue`。

**练习 2**：`_compute_final_stage_id` 在「text→audio」与「text→image」请求里，返回值会一样吗？

> **参考答案**：通常不一样。它依据请求声明的输出模态（`output_modalities`）选 stage：text→audio 的最终 stage 是产出 audio 的 stage（如 Qwen3-Omni 的 Code2wav）；text→image 的最终 stage 是 diffusion 图像 stage。这个值会一路传到 `final_stage_id` 参数，被 Orchestrator 用来判断「输出到达哪个 stage 就算请求完成」。

---

### 4.4 一次 generate 请求的 8 步执行流

#### 4.4.1 概念说明

官方文档用 8 个带编号的箭头步骤描述了一次 `generate` 请求的完整生命周期（见 [async_omni_architecture.md:49-92](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/module/async_omni_architecture.md#L49-L92)）。理解这 8 步，就理解了整个多阶段运行时的「主干道」。本模块把每一步**落**到具体文件/类上，让你能拿着这张表去源码里定位。

我们以 Qwen3-Omni 的「text → audio」为例：请求依次穿过 Thinker（stage 0）→ Talker（stage 1）→ Code2wav（stage 2），最终产出音频。

#### 4.4.2 核心流程

8 步执行流（含文件/类定位）如下表：

| 步骤 | 发生位置 | 做了什么 |
| --- | --- | --- |
| **[1] App → generate** | `AsyncOmni.generate`（[async_omni.py:454](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/async_omni.py#L454)） | 用户调用 `generate(prompt, request_id)`；生成内部唯一 `request_id`（[async_omni.py:505-506](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/async_omni.py#L505-L506)）。 |
| **[2] 启动派发 + add_request** | `AsyncOmni`（[async_omni.py:534, 604-611](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/async_omni.py#L534)） | 首次调用时启动 `_final_output_handler()`；调 `engine.add_request_async(stage_id=0, ...)`。 |
| **[3] stage-0 预处理 + 入队** | `AsyncOmniEngine.add_request`（[async_omni_engine.py:1300-1341](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/async_omni_engine.py#L1300-L1341)） | `_build_add_request_message` 跑 `InputProcessor.process_inputs`（[718](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/async_omni_engine.py#L718)），然后 `request_queue.sync_q.put(msg)`（[1341](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/async_omni_engine.py#L1341)）。 |
| **[4] Orchestrator 收请求** | `Orchestrator._request_handler`（[orchestrator.py:584, 642](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L584)） | 后台线程从 `request_queue.async_q` 取消息，`_handle_add_request` 把请求交给 `stage_clients[0].add_request_async`。 |
| **[5] 轮询/路由/前推** | `Orchestrator._orchestration_loop`（[orchestrator.py:896, 1245, 1728](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L896)） | 反复 poll 各 stage 输出；`_route_output` 判断 finished；若 finished 且非最终 stage，`_forward_to_next_stage` 把结果投给下一 stage（Thinker→Talker→Code2wav）；最终 `output_queue.put`。 |
| **[6] API 侧拉输出** | `AsyncOmni._final_output_loop`（[async_omni.py:877](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/async_omni.py#L877)） | `await engine.try_get_output_async()`，按 `request_id` 路由到 `ClientRequestState.queue`。 |
| **[7] 组装并 yield** | `AsyncOmni._process_orchestrator_results`（[async_omni.py:811, 836, 854](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/async_omni.py#L792)） | 从每请求队列 `get`，`_process_single_result` 组装 `OmniRequestOutput`，`yield` 给用户。 |
| **[8] 结束** | `AsyncOmni._process_orchestrator_results`（[async_omni.py:857-858](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/async_omni.py#L857-L858)） | 收到 `result.finished == True`（最终 stage 完成），`break`，`generate()` 结束。 |

#### 4.4.3 源码精读

步骤 [5] 是整个架构的「心脏」，它把多个 stage 串成流水线。文档对它的描述：

[async_omni_architecture.md:69-78](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/module/async_omni_architecture.md#L69-L78) —— 注意第 75-77 行：*「if finished and not final_stage and non-async-chunk: _forward_to_next_stage(...)」*。这正是 Thinker 完成后把隐藏态/输出前推给 Talker、Talker 完成后前推给 Code2wav 的判断点。`async-chunk` 是一种流式分块模式（见 Qwen3-Omni 示例的 `--async-chunk`），它会在该判断上走不同分支。

> 说明：上面这个链接指向的是文档里的**文字描述**所在区块；由于 `async_omni_architecture.md` 与 `async_omni_engine.py` 是不同文件，阅读时请打开 [async_omni_architecture.md 的第 69–78 行](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/module/async_omni_architecture.md#L69-L78) 对照。

文档还提供了等价的 mermaid 时序图，直观展示了 APP / AsyncOmni / Engine / Orchestrator / Stage-0 Client / Next Stage Client 之间的消息往返：

[async_omni_architecture.md:96-126](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/module/async_omni_architecture.md#L96-L126) —— `loop poll route forward` 块（114-121 行）就是步骤 [5] 的可视化：Orchestrator 反复 `get_output_async` → `_route_output` → 必要时 `add_request_async` 给下一 stage → `output_queue.put` 回送。

#### 4.4.4 代码实践（本讲主实践任务）

这是本讲规格要求的代码实践任务。

1. **实践目标**：为一次「text → audio」请求，给 8 步执行流的**每一个箭头**标注它发生在哪个文件 / 类 / 方法，形成一份「执行流注释表」。
2. **操作步骤**：
   - 打开 [async_omni_architecture.md:49-92](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/module/async_omni_architecture.md#L49-L92)。
   - 仿照本模块 4.4.2 的表格，自己重做一张更详细的表，多加一列「关键源码行」，例如步骤 [3] 填 `async_omni_engine.py:718 (InputProcessor.process_inputs)`、`async_omni_engine.py:1341 (request_queue.sync_q.put)`。
   - 对 Thinker(stage0)→Talker(stage1)→Code2wav(stage2) 的两次「前推」，分别指出它们都对应步骤 [5] 里的 `_forward_to_next_stage`（[orchestrator.py:1728](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L1728)）。
3. **需要观察的现象**：步骤 [3] 与 [4] 之间隔着一条线程边界（主线程 → Orchestrator 线程），它们靠 `janus.Queue` 衔接；步骤 [5] 内部多次「轮询→路由→前推」循环，对应 stage0→stage1→stage2 的两次推进。
4. **预期结果**：得到一张 8 行（每个箭头一行）、列含「步骤号 / 文件 / 类.方法 / 关键行号 / 一句话职责」的完整注释表。
5. 待本地验证（纯源码阅读，无需运行模型）。

#### 4.4.5 小练习与答案

**练习 1**：步骤 [3] 里，`AsyncOmniEngine` 为什么要在主线程就把 `InputProcessor.process_inputs` 跑掉，而不是直接把原始 prompt 丢给 Orchestrator？

> **参考答案**：注释（[async_omni_engine.py:1318-1324](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/async_omni_engine.py#L1318-L1324)）写明是为了「avoiding a queue + coroutine-switch round-trip」。在主线程预处理后，Orchestrator 收到的是 ready-to-submit 的 `OmniEngineCoreRequest`，少了一次跨线程往返；同时主线程持有 tokenizer/processor，预处理更方便。

**练习 2**：如果 stage0 是 diffusion（例如单阶段文生图 Z-Image-Turbo），步骤 [3] 的预处理会发生什么？

> **参考答案**：`_build_add_request_message` 里的条件 `if stage_type != "diffusion"`（[async_omni_engine.py:702](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/async_omni_engine.py#L702)）为假，跳过 `InputProcessor.process_inputs`，直接把原始 prompt dict 装进 `StageSubmissionMessage`。后续步骤 [4]/[5] 由 diffusion 专用的客户端（`StageDiffusionClient`）和调度器处理（见 u5-l1/u5-l2）。

**练习 3**：步骤 [8] 的「finished == True」是由谁、在什么时候设置的？

> **参考答案**：由 Orchestrator 在步骤 [5] 里设置。当某个请求的输出到达 `_compute_final_stage_id` 确定的最终 stage 且该 stage 报告完成时，Orchestrator 把回送到 `output_queue` 的 `OutputMessage.finished` 置为 `True`。API 侧（步骤 [7]）据此 `break` 退出读取循环。

## 5. 综合实践

把本讲的知识串起来，完成下面这个综合任务（源码阅读型）：

**任务：为「text → audio」请求画一张带源码锚点的完整调用链时序图。**

要求：

1. 以 Qwen3-Omni 三阶段（Thinker / Talker / Code2wav）为背景。
2. 在时序图上画出 6 个参与者：`App`、`AsyncOmni`、`AsyncOmniEngine`、`Orchestrator（后台线程）`、`Stage Clients`、`Stage 子进程`。
3. 每条箭头标注：发生在哪个**步骤号**（[1]–[8]）、对应**哪个方法**、方法定义的**文件:行号**。
4. 特别标出两个「跨线程/跨进程边界」：
   - 主线程 ↔ Orchestrator 线程：`request_queue.sync_q.put` ↔ `request_queue.async_q.get`（janus）。
   - Orchestrator ↔ stage 子进程：`stage_clients[i].add_request_async`（ZMQ IPC）。
5. 在图中标出两次「前推」（Thinker→Talker、Talker→Code2wav）都落在 `_forward_to_next_stage`（[orchestrator.py:1728](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L1728)）。

完成后，你应当能用这张图向别人解释：*「用户调一次 `generate('你好')`，数据是怎么在 5 层架构里下沉再上浮、最终变成一段音频的。」* 如果想进一步验证，可参考文档给出的可运行脚本（需下载 Qwen3-Omni 权重，待本地验证）：

```bash
cd examples/offline_inference/qwen3_omni
python end2end.py --output-dir output_audio --query-type text --async-chunk --enable-stats
```

该脚本用同步 `Omni` 入口（[end2end.py:359](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/offline_inference/qwen3_omni/end2end.py#L359)），但其底层引擎、队列、Orchestrator 与本讲讲的 `AsyncOmni` 路径完全一致——这正是 `OmniBase` 共享逻辑的价值。

## 6. 本讲小结

- vLLM-Omni 多阶段运行时是**五层架构**：API（`Omni`/`AsyncOmni`）→ Engine 代理（`AsyncOmniEngine`）→ Orchestration（`Orchestrator` 后台线程）→ Communication（ZMQ 客户端）→ Execution（stage 子进程）。
- `AsyncOmniEngine` 是**薄代理**，自身不推理；它只在主线程做 stage-0 输入预处理，再用 `janus.Queue`（`request_queue` / `output_queue`）与后台 Orchestrator 线程通信。
- `add_request` 走 `_build_add_request_message`（含 `InputProcessor.process_inputs`，仅 LLM stage）→ `request_queue.sync_q.put`；`try_get_output` / `try_get_output_async` 从 `output_queue.sync_q` 读。
- `OmniBase` 抽出**共享逻辑**：`_compute_final_stage_id` 决定最终输出 stage，`_handle_output_message` / `_process_single_result` 处理输出；`Omni`（阻塞 `while` 轮询）与 `AsyncOmni`（后台协程 + 每请求 `asyncio.Queue`）只差在「取输出方式」。
- 一次 `generate` 的 **8 步执行流**：generate → 启动派发+add_request → 预处理+入队 → Orchestrator 收请求 → 轮询/路由/前推 → API 拉输出 → 组装 yield → finished 退出。
- 多阶段流水线的「串联」发生在步骤 [5] 的 `_route_output` / `_forward_to_next_stage`，它让 Thinker→Talker→Code2wav 依次推进，直到最终 stage 完成。

## 7. 下一步学习建议

本讲建立了**全局视图**，接下来建议按层深入：

- **u3-l2 Orchestrator**：精读 [vllm_omni/engine/orchestrator.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py)，搞清楚本讲步骤 [4][5] 里 `_request_handler` / `_orchestration_loop` / `_route_output` / `_forward_to_next_stage` 的具体编排逻辑，以及 `OrchestratorRequestState` 如何跟踪每个请求的阶段进度。
- **u3-l3 阶段进程与运行时**：深入 Communication/Execution 层，理解 `StageEngineCoreProc` 子进程如何启动、`StageRuntime` 如何选择单机/分布式、`StageClient` 如何用 ZMQ 投递请求。
- **u3-l4 OmniConnector 体系**：理解步骤 [5] 前推时，stage 之间的大张量（隐藏态/KV）如何通过 `OmniConnector`（SharedMemory / Mooncake 等）全解耦传输。
- 若你更关心某一类 stage 的内部：AR 看 U4，Diffusion 看 U5；在线服务 API 看 U6。
