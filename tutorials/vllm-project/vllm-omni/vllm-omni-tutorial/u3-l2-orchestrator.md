# Orchestrator：跨阶段请求编排

## 1. 本讲目标

本讲深入 vLLM-Omni 多阶段运行时的「心脏」——`Orchestrator`。在 [u3-l1](u3-l1-async-omni-architecture.md) 中我们已经建立了五层架构的全局视图：API 层把请求交给 `AsyncOmniEngine`，再由它在后台线程里运行 `Orchestrator`，请求「下沉」到各 stage 子进程、输出「上浮」回 API 层。本讲要回答的核心问题是：

> 这些请求在多个 stage 之间到底是谁在推动？推动的判断条件是什么？每个请求的「走到哪一步了」又记录在哪里？

读完本讲，你应当能够：

- 说出 `Orchestrator` 的两条处理路径 `_request_handler` 与 `_orchestration_output_handler` 各自做什么、用什么队列与主线程通信。
- 在源码里定位「请求在某 stage 完成且非最终 stage 时前推到下一 stage」的判断逻辑（`_route_output` → `_forward_to_next_stage`）。
- 解释 `OrchestratorRequestState` 如何用 `final_stage_id` / `final_output_stage_ids` / `finished_final_output_stage_ids` / `stage_submit_ts` 跟踪每个请求的阶段进度。
- 描述 `StagePool` 如何管理一个逻辑 stage 的多个副本，并在单机轮询（`select_replica_id`）与分布式负载均衡（`pick`）两条路径之间切换。

## 2. 前置知识

本讲假设你已经读过：

- **u3-l1（五层架构总览）**：知道 `janus.Queue`（`request_queue` / `output_queue`）是主线程与后台 Orchestrator 线程之间的桥；知道一次 `generate` 的 8 步执行流，其中第 [5] 步的 `_route_output` / `_forward_to_next_stage` 是串联流水线的关键。
- **u2-l3（输入输出数据结构）**：知道 `OmniEngineCoreRequest` 是投递给 stage 的请求容器，`OmniRequestOutput` 是统一输出容器。

下面补充两个本讲会反复出现的术语：

- **stage（阶段）**：一个请求被拆分成的顺序子任务。例如 Qwen3-Omni 有三个 stage：`Thinker`（stage 0，多模态理解）→ `Talker`（stage 1，文本/语音 token 生成）→ `Code2wav`（stage 2，语音波形生成）。stage 用从 0 开始的整数编号。
- **replica（副本）**：一个逻辑 stage 在物理上可以部署多份以扩容。`StagePool` 就是「一个逻辑 stage 的所有副本 + 这个 stage 的投递/路由职责」的封装。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vllm_omni/engine/orchestrator.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py) | Orchestrator 主体：后台线程的事件循环、两条处理路径、`_route_output` / `_forward_to_next_stage` 的前推逻辑，以及 `OrchestratorRequestState` 状态定义。 |
| [vllm_omni/engine/stage_pool.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_pool.py) | `StagePool`：管理一个逻辑 stage 的副本集合，负责「请求投递（submit）」「副本选择（select/pick）」「输出轮询（poll）」三件事。 |
| [vllm_omni/engine/orchestrator_monitor.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator_monitor.py) | 可选的诊断监控器：按 1 秒窗口采样「轮询循环忙/闲」「每副本队列积压/inflight 请求数」，写到 JSON 供性能分析。 |
| [vllm_omni/engine/messages.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/messages.py) | Orchestrator 与主线程之间通过 `janus.Queue` 传递的所有 `msgspec` 消息类型（`StageSubmissionMessage` / `OutputMessage` / `ErrorMessage` 等）。 |

---

## 4. 核心概念与源码讲解

### 4.1 Orchestrator 的两条处理路径

#### 4.1.1 概念说明

`Orchestrator` 不是普通的 Python 对象——它**运行在一个独立的后台线程里，并拥有自己的 asyncio 事件循环**（见类文档字符串与构造函数）。它和 API 层（主线程）之间没有直接的函数返回值链，而是靠两个 `janus.Queue` 单向通信：

- **`request_async_queue`**：主线程 → Orchestrator。主线程把「新增请求 / 流式更新 / 中止 / RPC」等指令塞进这条队列。
- **`output_async_queue`**：Orchestrator → 主线程。Orchestrator 把 stage 产出的最终输出或错误塞进这条队列，主线程再按 `request_id` 路由给各个等待中的客户端。

> 为什么要用 `janus.Queue`？因为它**一端是同步的、一端是异步的**：主线程的同步代码用 `queue.sync_q.put()`，Orchestrator 的异步循环用 `queue.async_q.get()`。这正是 u3-l1 提到的「同步端归主线程、异步端归 Orchestrator」。

Orchestrator 在这个事件循环里并行运行两条逻辑路径，可以类比为一条「请求下沉」管道和一条「输出上浮」管道：

| 路径 | 方向 | 职责 |
| --- | --- | --- |
| `_request_handler` | 下沉 | 从 `request_async_queue` 取主线程消息，把新请求投递到 stage 0 |
| `_orchestration_output_handler` | 上浮 | 轮询所有 stage 的输出，处理、路由、必要时前推到下一 stage，再把最终结果塞回 `output_async_queue` |

#### 4.1.2 核心流程

`Orchestrator.run()` 是事件循环的入口。它把两条路径各包成一个 `asyncio.Task`，然后用 `asyncio.gather` 同时跑起来：

```text
run()
 ├─ create_task(_request_handler())              # 请求下沉路径
 ├─ create_task(_orchestration_output_handler()) # 输出上浮路径
 ├─ (可选) membership_watcher / duplex_reaper_loop
 └─ asyncio.gather(*tasks)
```

两条路径互相独立、并发推进：一条不断把新请求喂给 stage 0，另一条不断把各 stage 的成品抽出来往上传或往后推。这就实现了「阶段重叠」——stage 0 处理第 N 个请求的同时，stage 1 可以在处理第 N-1 个请求。

#### 4.1.3 源码精读

`run()` 创建两条任务并 `gather`，`finally` 里统一做关闭清理：

`run` 入口与两条任务的创建：
[vllm_omni/engine/orchestrator.py:505-513](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L505-L513) —— 创建 `request_task` 与 `output_task` 两个协程任务。

`_request_handler` 是一个按消息类型分发的循环，从队列读消息、按 `type` 字段路由：

[vllm_omni/engine/orchestrator.py:584-625](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L584-L625) —— `add_request` / `streaming_update` / `add_companion_request` / `abort` / `interaction` / `collective_rpc` 各自落到对应的 `_handle_*` 方法，`ShutdownRequestMessage` 则置位 `_shutdown_event` 并 `break` 退出循环。

其中 `_handle_add_request` 是新请求的入口：它总是从 stage 0 开始，先建一个 `OrchestratorRequestState` 记录这个请求的全生命周期状态，再调 `stage_pools[0].submit_initial(...)` 投递：

[vllm_omni/engine/orchestrator.py:642-690](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L642-L690) —— 构造 `req_state`、登记到 `self.request_states`、记录 `stage_submit_ts[0]`，然后 `submit_initial` 到 stage 0。

> 注意一个细节：`_handle_add_request` 里 `stage_id = 0` 是硬编码的。无论模型有几个 stage，新请求永远从 stage 0 进入；之后的 stage 推进完全交给上浮路径的 `_route_output` → `_forward_to_next_stage`。

`_orchestration_output_handler` 是上浮路径的薄包装，真正干活的是 `_orchestration_loop`（下一节精读）：

[vllm_omni/engine/orchestrator.py:888-894](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L888-L894) —— 它只是 `try: await self._orchestration_loop()` 并妥善处理 `CancelledError`。

至于 Orchestrator 是在哪里被实例化并 `run()` 的——在 `AsyncOmniEngine` 里：它把三条 `janus.Queue` 的 `async_q` 端连同 `stage_pools` 传给 `Orchestrator`，然后在后台线程的循环里 `await orchestrator.run()`：

[vllm_omni/engine/async_omni_engine.py:413-431](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/async_omni_engine.py#L413-L431) —— `request_async_queue=self.request_queue.async_q`、`output_async_queue=self.output_queue.async_q`、`stage_pools=self.stage_pools`。

#### 4.1.4 代码实践

**实践目标**：亲手验证两条路径的「消息类型分发」与「队列方向」。

**操作步骤**：

1. 打开 `vllm_omni/engine/orchestrator.py`，定位 `_request_handler`（约第 584 行）。
2. 列出它处理的全部消息类型，以及每种类型对应的 `_handle_*` 方法。
3. 打开 `vllm_omni/engine/messages.py`，找到 `StageSubmissionMessage` 与 `OutputMessage` 的定义，确认它们分别走「下沉队列」和「上浮队列」。

**需要观察的现象**：

- `_request_handler` 的 `if/elif` 分支里没有出现 stage 输出的处理——输出处理全在 `_orchestration_loop` 里，这印证了两条路径职责分离。
- `StageSubmissionMessage.type` 的取值只有 `"add_request"` 和 `"streaming_update"` 两种（见 [messages.py:18-29](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/messages.py#L18-L29)），说明「新请求」和「流式追加更新」复用同一个消息结构。

**预期结果**：你得到一张「消息类型 → 处理方法 → 走哪条队列」的对照表，证明 Orchestrator 内部是「单线程事件循环 + 两条逻辑管道」的结构，而非多线程抢占。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_request_handler` 和 `_orchestration_output_handler` 能并发运行而不需要加锁保护 `self.request_states`？

> **答案**：因为它们运行在**同一个 asyncio 事件循环**里（同一个后台线程），协程之间是协作式调度，`await` 才会切换。`self.request_states` 的读写都在协程的同步代码段内完成，不存在真正的并行抢占，所以不需要锁。`asyncio.gather` 让它们「交替推进」而非「同时执行」。

**练习 2**：如果主线程在 Orchestrator 还没消费某条 `add_request` 消息时就发来了对应的 `abort`，会发生什么？

> **答案**：消息按入队顺序被消费。`abort` 在 `add_request` 之后入队，就会在后处理；`_handle_abort` → `_cleanup_request_ids` 会尝试中止并清理该 `request_id`。即便 `add_request` 已经把它投递到了 stage 0，清理路径也会向所有 stage pool 广播 `abort_requests`，保证不会残留。

---

### 4.2 编排主循环与阶段前推（流水线的心脏）

#### 4.2.1 概念说明

如果说 4.1 讲的是「两条管道的入口」，那么本节讲的是「把多个 stage 串成流水线」的核心机制。它由三个方法接力完成：

- **`_orchestration_loop`**：一个 `while` 循环，**轮询所有 stage 的所有副本**，把原始输出收集上来。
- **`_route_output`**：对每一条输出做决策——该送回前端吗？该前推到下一 stage 吗？请求整体完成该清理吗？
- **`_forward_to_next_stage`**：真正把输出「翻译」成下一 stage 的输入并投递过去。

本节标题里的「判断请求 finished 且非最终阶段则前推」，正是 `_route_output` 中那个组合条件判断。

#### 4.2.2 核心流程

`_orchestration_loop` 的骨架是一个双层循环（外层遍历 stage，内层遍历该 stage 的可用副本），并按 stage 类型分两条轮询路径：

```text
while not shutdown:
    idle = True
    for stage_id in range(num_stages):
        pool = stage_pools[stage_id]
        for replica_id in pool.available_replica_ids():
            if diffusion stage:
                output = pool.poll_diffusion_output(replica_id)   # 非阻塞 nowait
                if output: _handle_processed_outputs(...)
            else:  # llm stage
                raw = await pool.poll_llm_raw_output(replica_id)  # 带 1ms 超时
                raw_output = pool.process_llm_raw_outputs(...)    # 跑输出处理器
                _handle_processed_outputs(...)
            idle = False
    monitor.note_loop(idle=idle)
    await asyncio.sleep(0.001 if idle else 0)   # 闲则让出 1ms，忙则立即下一轮
```

关键设计：

- **轮询而非回调**：Orchestrator 不等 stage 主动通知，而是主动去每个副本的输出队列里「捞」。diffusion 用非阻塞的 `get_diffusion_output_nowait`，LLM 用带 1ms 超时的 `get_output_async`，避免空转阻塞。
- **忙则不休、闲则让步**：本轮只要有任何副本产出（`idle=False`），就 `sleep(0)` 立刻进入下一轮；只有全空闲时才 `sleep(0.001)` 让出 CPU。这是事件循环里常见的「尽可能快地消费已就绪事件」模式。

拿到输出后，`_handle_processed_outputs` 逐条查 `request_states`、在完成时构造 stage 指标，然后交给 `_route_output` 决策。

`_route_output` 的决策可以用下面这张状态流转图概括（以 3 阶段模型为例，`final_stage_id=2`）：

```text
                    ┌─────────────────────────────────────────────────────┐
   stage 0 输出 ───►│ _route_output(stage=0)                              │
   (finished)       │  finished 且 0 < final_stage_id(2) ──► 前推到 stage 1│
                    └─────────────────────────────────────────────────────┘
                                          │
                    ┌─────────────────────▼──────────────────────────────┐
   stage 1 输出 ───►│ _route_output(stage=1)                              │
   (finished)       │  finished 且 1 < final_stage_id(2) ──► 前推到 stage 2│
                    └─────────────────────────────────────────────────────┘
                                          │
                    ┌─────────────────────▼──────────────────────────────┐
   stage 2 输出 ───►│ _route_output(stage=2)                              │
   (finished)       │  finished 且 final_output ──► 送回前端 + 请求整体完成│
                    │  (stage_id 不再 < final_stage_id，所以不再前推)     │
                    └─────────────────────────────────────────────────────┘
```

#### 4.2.3 源码精读

`_orchestration_loop` 的双层轮询与按 stage 类型分流：

[vllm_omni/engine/orchestrator.py:896-1039](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L896-L1039) —— 外层 `for stage_id`、内层 `for replica_id in pool.available_replica_ids()`；diffusion 走 `poll_diffusion_output`，LLM 走 `poll_llm_raw_output` + `process_llm_raw_outputs`。循环末尾的忙闲调度：

[vllm_omni/engine/orchestrator.py:1035-1039](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L1035-L1039) —— `note_loop(idle=idle)` 后，`idle` 则 `sleep(0.001)`，否则 `sleep(0)`。

`_handle_processed_outputs` 在请求完成或流式段结束时构造指标，再调 `_route_output`：

[vllm_omni/engine/orchestrator.py:1041-1071](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L1041-L1071) —— 其中 `if output.finished or segment_finished:` 分支负责构造 stage 指标。

**本节最关键的代码**——`_route_output` 里「判断请求 finished 且非最终阶段则前推」的组合条件：

[vllm_omni/engine/orchestrator.py:1328-1371](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L1328-L1371)

把这段条件拆开看，它由四个子条件「与」起来，缺一不可：

```python
if (
    (finished or (streaming.enabled and streaming.segment_finished))  # ① 本 stage 已完成
    and stage_id < req_state.final_stage_id                            # ② 且不是最终 stage
    and (not async_chunk or not receives_async_chunks(stage_id + 1))   # ③ 下一 stage 不是异步块模式
    and (not next_stage_already_submitted(stage_id) or streaming.enabled)  # ④ 没投过，或流式
):
    # ... CFG 伴生检查后：
    await self._forward_to_next_stage(req_id, stage_id, output, req_state, ...)
```

- **①** 既要完成才有东西可往后传（流式场景下「段结束」也算）。
- **②** 是核心：`stage_id < final_stage_id`，即「非最终阶段」。最终 stage 的输出不会再前推，而是直接回前端。
- **③④** 是为「异步块（async_chunk）」和「流式」两种高级模式开的口子，初学时可以先把它们视为 `True`，理解主干。

`_route_output` 开头还有一段判断「请求整体是否完成」的逻辑（基于 `final_output_stage_ids` 的子集关系，见 4.3 节）：

[vllm_omni/engine/orchestrator.py:1265-1273](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L1265-L1273) —— `request_finished = final_output_stage_ids.issubset(finished_final_output_stage_ids)`。

`_forward_to_next_stage` 负责把输出「翻译」成下一 stage 的输入。它的第一步永远是 `next_logical = src_stage_id + 1`，然后按下一 stage 的类型分三条分支（diffusion / PD 分离部署 / 普通 AR）：

[vllm_omni/engine/orchestrator.py:1728-1740](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L1728-L1740) —— 方法签名与 `next_logical = src_stage_id + 1`。

对于「下一 stage 是 AR（如 Talker）」这条最常见的分支，它调用下一 stage client 的 `process_engine_inputs` 把上游输出转成下游输入，再 `submit_initial` / `submit_update`：

[vllm_omni/engine/orchestrator.py:1977-2054](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L1977-L2054) —— `next_inputs = next_client.process_engine_inputs(...)`，逐个 `_build_next_stage_request` 后按是否已投递选择 `submit_update` 或 `submit_initial`。

#### 4.2.4 代码实践

**实践目标**：定位「判断请求 finished 且非最终阶段则前推」的代码，并据此画出一个请求在 3 个阶段间转移的状态机图。

**操作步骤**：

1. 在 `orchestrator.py` 中打开 `_route_output`（约第 1245 行）。
2. 找到 4.2.3 引用的那段组合条件（约第 1328 行），抄下它的四个子条件。
3. 假设一个 3 阶段模型（stage 0 / 1 / 2，`final_stage_id=2`），分别令 `stage_id = 0, 1, 2`，代入条件 ② `stage_id < final_stage_id`，判断每个 stage 完成时是否进入前推分支。

**需要观察的现象**：

- `stage_id=0`：`0 < 2` 成立 → 前推到 stage 1。
- `stage_id=1`：`1 < 2` 成立 → 前推到 stage 2。
- `stage_id=2`：`2 < 2` **不成立** → 不前推；同时因为 stage 2 是 `final_output` stage，它的完成会触发「请求整体完成」并清理。

**预期结果**：你画出如下的状态机图（节点是 `stage_id`，边标注触发条件）：

```text
   [stage 0: finished] --前推--> [stage 1: finished] --前推--> [stage 2: finished]
                                                                    │
                                                          final_output + 子集满足
                                                                    ▼
                                                            [请求完成，清理]
```

并在每个节点旁边标注对应代码行：前推判断在 [orchestrator.py:1328-1371](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L1328-L1371)，请求完成清理在 [orchestrator.py:1373-1374](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L1373-L1374)。

#### 4.2.5 小练习与答案

**练习 1**：如果某 stage 的输出既不 `finished` 也不属于流式段结束，`_route_output` 会做什么？

> **答案**：前推条件 ① 不满足，不会调用 `_forward_to_next_stage`。但只要该 stage 是 `final_output` stage，仍会把这条中间输出通过 `OutputMessage` 送回前端（见 [orchestrator.py:1284-1298](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L1284-L1298)），`finished` 字段为 `False`。这正是流式生成时「逐块返回」的来源。

**练习 2**：`_forward_to_next_stage` 里 `next_logical = src_stage_id + 1`。这隐含了什么关于 stage 编号的约定？

> **答案**：stage 必须按执行顺序用连续整数 0, 1, 2... 编号，「下一 stage」恒为「当前 stage + 1」。所以 vLLM-Omni 的多阶段流水线是**线性顺序**的，不支持 stage 之间的分支或跳转（至少在 Orchestrator 这一层是这样）。

---

### 4.3 OrchestratorRequestState 状态机

#### 4.3.1 概念说明

每来一个请求，`_handle_add_request` 都会 new 一个 `OrchestratorRequestState`，存进 `self.request_states[request_id]`。这个对象是「这个请求在 Orchestrator 眼里的全部状态」——它贯穿请求从进入 stage 0 到最终完成的全过程，承担两个职责：

1. **记住这个请求要去哪儿**：`final_stage_id`（最终 stage 编号）、`final_output_stage_ids`（哪些 stage 产出才算「最终输出」）。
2. **记住这个请求走到哪了**：`stage_submit_ts`（每个 stage 何时投递的）、`finished_final_output_stage_ids`（哪些最终输出 stage 已经完成）。

有了这两个集合，`_route_output` 就能用一行子集判断决定请求是否整体完成。

#### 4.3.2 核心流程

`OrchestratorRequestState` 的状态推进可以用「投递时间戳 + 完成集合」两个维度刻画：

```text
新请求到达
  └─ req_state.final_stage_id = 2
     req_state.final_output_stage_ids = {2}     # 由 API 层根据输出模态算出
     req_state.stage_submit_ts = {0: t0}        # stage 0 已投递

stage 0 完成，前推到 stage 1
  └─ req_state.stage_submit_ts = {0: t0, 1: t1}

stage 1 完成，前推到 stage 2
  └─ req_state.stage_submit_ts = {0: t0, 1: t1, 2: t2}

stage 2 完成（final_output stage）
  └─ req_state.finished_final_output_stage_ids = {2}
     判断: {2}.issubset({2}) == True  →  request_finished = True
     → 清理 request_states[request_id]
```

请求整体完成的判据是：

\[
\text{request\_finished} \;=\; \text{final\_output\_stage\_ids} \;\subseteq\; \text{finished\_final\_output\_stage\_ids}
\]

为什么用「子集」而不是「相等」？因为有的模型会有**多个最终输出 stage**（`final_output_stage_ids` 可以含多个编号，例如同时输出文本和音频），只要这些「最终输出 stage」全部完成，请求就算完成。

> `final_stage_id` 与 `final_output_stage_ids` 的初始值来自 API 层。`AsyncOmni` 在 `add_request` 时根据请求的**输出模态**计算它们——例如「要音频」就把产出音频的那个 stage 算进去（见 [entrypoints/utils.py:695-737](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/utils.py#L695-L737) 的 `get_final_stage_id_for_e2e`，从后往前找第一个 `final_output=True` 且模态匹配的 stage）。

#### 4.3.3 源码精读

`OrchestratorRequestState` 的字段定义（只列与阶段进度直接相关的）：

[vllm_omni/engine/orchestrator.py:161-188](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L161-L188) —— 关键字段：

- `final_stage_id: int = -1`（第 168 行）：最终 stage 编号，前推条件 `stage_id < final_stage_id` 的右值。
- `final_output_stage_ids: set[int]`（第 169 行）：哪些 stage 的产出构成「最终输出」。
- `finished_final_output_stage_ids: set[int]`（第 170 行）：已完成的最终输出 stage，随请求推进不断累加。
- `stage_submit_ts: dict[int, float]`（第 176 行）：每个 stage 的投递时间戳，既用于指标计算，也用于判断「下一 stage 是否已投递过」（`_next_stage_already_submitted`）。

请求创建时这些字段如何初始化（注意 `final_output_stage_ids` 默认就是 `[final_stage_id]`）：

[vllm_omni/engine/orchestrator.py:666-678](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L666-L678) —— `final_output_stage_ids = set(msg.final_output_stage_ids or [final_stage_id])`、`stage_submit_ts[stage_id] = _time.time()`。

完成集合的累加发生在 `_route_output`：

[vllm_omni/engine/orchestrator.py:1271-1273](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L1271-L1273) —— `req_state.finished_final_output_stage_ids.add(stage_id)` 后用 `issubset` 判定 `request_finished`。

`stage_submit_ts` 的另一个用途——判断下一 stage 是否已经投递过（避免重复投递）：

[vllm_omni/engine/orchestrator.py:1376-1377](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L1376-L1377) —— `_next_stage_already_submitted` 就是检查 `(stage_id + 1) in req_state.stage_submit_ts`。

请求完成后的清理（从 `request_states` 移除、释放副本绑定、撤销运行计数）集中在 `_cleanup_request_ids`：

[vllm_omni/engine/orchestrator.py:1096-1170](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L1096-L1170) —— 所有拆除路径（stage 出错、abort、副本丢失、membership 注销）都汇入这里，保证 tracker 状态不会比请求活得更久。

#### 4.3.4 代码实践

**实践目标**：通过源码确认 `final_output_stage_ids` 与 `finished_final_output_stage_ids` 这对集合如何驱动请求完成。

**操作步骤**：

1. 打开 `orchestrator.py` 第 161 行起的 `OrchestratorRequestState`，确认 `final_output_stage_ids` 与 `finished_final_output_stage_ids` 都是 `set[int]`、默认空集。
2. 跟踪 `_handle_add_request`（第 666 行）确认 `final_output_stage_ids` 在建请求时被初始化。
3. 跟踪 `_route_output`（第 1271 行）确认 `finished_final_output_stage_ids` 在每个最终输出 stage 完成时被 `add`。
4. 假设某模型 `final_output_stage_ids = {1, 2}`（两个最终输出 stage），手工模拟「stage 1 先完成、stage 2 后完成」时 `issubset` 的两次求值。

**需要观察的现象**：

- 只有 stage 1 完成：`{1, 2}.issubset({1})` → `False`，请求未完成，stage 2 的输出仍会被前推和等待。
- stage 2 也完成：`{1, 2}.issubset({1, 2})` → `True`，请求整体完成。

**预期结果**：你验证了「请求完成 = 所有最终输出 stage 都完成」这一判据，并理解了为什么用集合子集而非简单相等。

#### 4.3.5 小练习与答案

**练习 1**：`stage_submit_ts` 既是时间戳字典，又被当成了「哪些 stage 已投递」的判据。这种「一物两用」有什么风险？

> **答案**：风险在于语义耦合——如果将来某个重构在投递失败时不写 `stage_submit_ts`，或在不真正投递时就写入，那么 `_next_stage_already_submitted` 的判断就会失真，可能导致重复投递或漏投递。当前代码靠「投递成功才写时间戳」的约定维持正确性。更稳健的做法是用独立的「已投递 stage 集合」，但那样会多一份需要同步维护的状态。

**练习 2**：`OrchestratorRequestState` 里有一个 `streaming: StreamingInputState` 字段。它在阶段前推里扮演什么角色？

> **答案**：它标记这个请求是不是流式输入请求（如实时语音对话）。在 `_route_output` 的前推条件里，`req_state.streaming.enabled` 和 `req_state.streaming.segment_finished` 会让「段结束」也触发前推（条件 ① 的 `or` 分支），并把上游 token 等信息打包给下游 stage，实现边输入边推进的多轮流水线。

---

### 4.4 StagePool 与阶段副本管理

#### 4.4.1 概念说明

前面三节里反复出现 `stage_pools[stage_id].submit_initial(...)` / `poll_llm_raw_output(...)` / `available_replica_ids()`。`StagePool` 是 Orchestrator 和具体 stage 子进程之间的「中间层」：

- **向上**对 Orchestrator 暴露统一接口：`submit_initial`（首次投递）、`submit_update`（流式追加）、`poll_*`（轮询输出）、`abort_requests`（中止）。
- **向下**管理这个逻辑 stage 的若干副本（`clients` 列表），并决定一条请求该路由到哪个副本。

副本路由有两条路径：

| 模式 | 触发条件 | 选择方法 | 策略 |
| --- | --- | --- | --- |
| 单机 / legacy | 没有挂载 hub（`is_distributed == False`） | `select_replica_id` | 轮询（round-robin），同一请求粘滞同一副本 |
| 分布式 | 通过 `attach_hub` 挂载了 `OmniCoordClientForHub` | `pick`（async） | 查 hub 的 UP 副本快照 + `LoadBalancer` 策略（见 u3-l5） |

#### 4.4.2 核心流程

一条请求首次进入某 stage 的流程（`submit_initial`）：

```text
submit_initial(request_id, req_state, request)
  ├─ 取本 stage 的采样参数（diffusion 还要 from_params 归一化）
  ├─ 选副本：_pick_or_select(request_id)
  │     ├─ 单机: select_replica_id → 轮询 + 粘滞
  │     └─ 分布式: await pick → hub 快照 + LB
  ├─ 拿到 replica_id 对应的 client
  └─ client.add_request_async(...)   # 经 ZMQ 投递到该副本的子进程
```

输出轮询（被 `_orchestration_loop` 调用）：

```text
poll_llm_raw_output(replica_id, timeout_s=0.001)
  └─ await client.get_output_async()   # 从该副本的输出队列拉一条
poll_diffusion_output(replica_id)
  └─ client.get_diffusion_output_nowait()  # 非阻塞拉一条
```

副本集合的「稳定性」是 `StagePool` 的一个重要设计：删除副本时**不真正移除列表元素，而是把对应槽位置 `None`**（见 [stage_pool.py:255-265](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_pool.py#L255-L265) 的 `remove_client`）。这样其余副本的 `replica_id`（也就是列表下标）保持不变，已经在飞的请求绑定不会错位。遍历时要用 `live_replica_ids()` 跳过 `None` 空洞。

#### 4.4.3 源码精读

`StagePool` 的类文档把职责说得很清楚——「一个逻辑 stage 的副本集合 + 这个 stage 的路由职责（负载均衡 + 亲和性）」：

[vllm_omni/engine/stage_pool.py:56-80](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_pool.py#L56-L80) —— 注释明确：分布式模式下 `pick` 查 hub 缓存的副本列表并经负载均衡路由，同一 `request_id` 后续粘滞同一副本；非分布式回退到 `select_replica_id` 轮询。

`submit_initial` 的核心——选副本 + 投递（这里看 LLM 分支）：

[vllm_omni/engine/stage_pool.py:927-993](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_pool.py#L927-L993) —— `_pick_or_select` 选 `replica_id`，然后 `output_processor.add_request` 登记、`_llm_client(replica_id).add_request_async` 投递；失败时回滚绑定与 output processor 状态。

副本选择的「单机轮询 + 粘滞」实现：

[vllm_omni/engine/stage_pool.py:534-568](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_pool.py#L534-L568) —— `select_replica_id`：先看缓存绑定（粘滞），失效则释放；亲和性（CFG 伴生跟父请求同副本）；最后在可用副本里 round-robin。

分布式模式的 `pick`（带限界等待）：

[vllm_omni/engine/stage_pool.py:280-333](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_pool.py#L280-L333) —— 三级策略：① 粘滞（已绑定且仍可用）；② 继承亲和性（CFG 伴生跟父请求）；③ 全新选择——轮询 hub 的 UP 副本、跑 `LoadBalancer.select`，最多等 `DISPATCH_WAIT_TIMEOUT_S=10s`。

`_pick_or_select` 是两条路径的统一桥：

[vllm_omni/engine/stage_pool.py:1068-1077](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_pool.py#L1068-L1077) —— `is_distributed` 为真走 async `pick`，否则走同步 `select_replica_id`。

两条输出轮询路径：

[vllm_omni/engine/stage_pool.py:1122-1141](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_pool.py#L1122-L1141) —— `poll_llm_raw_output` 带 `timeout_s` 超时；[stage_pool.py:1152-1159](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_pool.py#L1152-L1159) —— `poll_diffusion_output` 非阻塞 `nowait`。

副本稳定性（删而不挤）与安全遍历：

[vllm_omni/engine/stage_pool.py:255-265](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_pool.py#L255-L265) —— `remove_client` 把槽位置 `None`；[stage_pool.py:146-148](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_pool.py#L146-L148) —— `live_replica_ids` 跳过空洞。

#### 4.4.4 代码实践

**实践目标**：对比单机 `select_replica_id` 与分布式 `pick` 两条副本选择路径的差异。

**操作步骤**：

1. 打开 `stage_pool.py`，分别定位 `select_replica_id`（约第 534 行）与 `pick`（约第 280 行）。
2. 列出两者各自用到的「粘滞 / 亲和性 / 选择」三个阶段。
3. 找到 `_pick_or_select`（约第 1068 行），确认它用 `is_distributed`（即「是否挂载了 hub」）来切换两条路径。

**需要观察的现象**：

- 两者都先尝试粘滞（已绑定的副本若仍可用就直接用），保证同一请求在多次进入同一 stage 时落到同一副本（这对 KV cache 局部性很重要）。
- `select_replica_id` 在多副本时用 `_next_replica_id` 做简单 round-robin；`pick` 则用 `LoadBalancer.select`（u3-l5 会讲 LEAST_QUEUE_LENGTH 等策略），并支持「没有可用副本时限界等待」。

**预期结果**：你得到一张对照表，说明 `StagePool` 通过 `_pick_or_select` 这一个开关，在「单机轮询」与「分布式负载均衡」之间无缝切换，而 Orchestrator 上层完全感知不到差异。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `StagePool` 删除副本时要把槽位置 `None`，而不是直接 `pop` 掉？

> **答案**：因为 `replica_id` 就是 `clients` 列表的下标，是其余副本和「在飞请求绑定」的稳定标识。直接 `pop` 会让后续元素下标错位，导致已记录的 `request_id → replica_id` 绑定指向错误的副本。置 `None` 保留了下标稳定性，代价是需要 `live_replica_ids()` 跳过空洞。

**练习 2**：`submit_initial` 在 LLM 分支里，投递前先调了 `output_processor.add_request`，投递失败时又调 `remove_request` 回滚。为什么要这么谨慎？

> **答案**：因为 output processor 内部会为这个请求维护跨步累积的状态（如多模态张量累积）。如果登记了却投递失败而不回滚，这个孤儿状态会泄漏。`StagePool` 把「登记—投递—失败回滚」做成一个原子动作，保证要么全成功、要么不留痕迹（见 [stage_pool.py:965-992](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_pool.py#L965-L992)）。

---

### 4.5（补充）Orchestrator Monitor：诊断流水线瓶颈

这是一个**可选的诊断组件**，不影响正确性，但对理解「为什么我的多阶段流水线慢」很有帮助，故单列一节简介。

#### 4.5.1 概念说明

`OrchestratorMonitor` 用 `--enable-orch-monitor` 开启。它按 **1 秒窗口**采样两类信号：

- **轮询循环忙/闲**：`note_loop(idle=...)` 在 `_orchestration_loop` 每轮结尾被调用，统计一个窗口内有多少轮「有产出（active）」、多少轮「空转（idle）」。`loop_active` 占比低说明 Orchestrator 大部分时间在空转——stage 产出跟不上。
- **每副本积压**：`replica_monitor_sample(replica_id)` 返回某副本的 `(outputs_queue_size, inflight)`——输出队列积压多少条、当前绑定了多少在飞请求。积压高说明该副本消费慢，是瓶颈 stage。

#### 4.5.2 源码精读

窗口定义与采样入口：

[vllm_omni/engine/orchestrator_monitor.py:31-34](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator_monitor.py#L31-L34) —— `_WINDOW_S = 1.0` 与采样器类型。

`note_loop` 累加忙/闲计数、按窗口滚动：

[vllm_omni/engine/orchestrator_monitor.py:102-108](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator_monitor.py#L102-L108)

Orchestrator 在 `_orchestration_loop` 每轮调用它：

[vllm_omni/engine/orchestrator.py:1035](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L1035) —— `self._orch_monitor.note_loop(idle=idle)`。

`flush` 在 `run()` 的 finally 里把全部窗口写成一个 JSON 文件（路径可用 `VLLM_OMNI_ORCH_MONITOR_PATH` 覆盖）：

[vllm_omni/engine/orchestrator_monitor.py:110-130](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator_monitor.py#L110-L130)

> 这是「源码阅读型实践」的好素材：你不需要真跑模型，只要读懂采样逻辑就能推断出「哪个副本的 outputs_queue_size 持续偏高，哪个 stage 就是瓶颈」。

---

## 5. 综合实践

把本讲的四个模块串起来，完成下面这个**源码追踪任务**（不要求运行模型，纯阅读 + 画图）：

**场景**：一个 3 阶段的 Qwen3-Omni 模型，stage 0 = Thinker、stage 1 = Talker、stage 2 = Code2wav，`final_stage_id = 2`，`final_output_stage_ids = {2}`（用户请求音频输出）。假设每个 stage 单副本。

**任务**：

1. **请求进入**：从 `_handle_add_request`（[orchestrator.py:642](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L642)）开始，写明 `req_state` 此时的 `final_stage_id`、`final_output_stage_ids`、`stage_submit_ts` 各是什么。
2. **stage 0 → 1**：在 `_route_output`（[orchestrator.py:1245](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L1245)）里代入 `stage_id=0`，确认前推条件成立，追踪到 `_forward_to_next_stage`（[orchestrator.py:1728](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L1728)），指出 Talker 的输入由哪个方法构造（提示：`process_engine_inputs`）。
3. **stage 1 → 2**：同法代入 `stage_id=1`。
4. **stage 2 完成**：代入 `stage_id=2`，确认前推条件 ②（`2 < 2`）不成立，转向 [orchestrator.py:1265-1273](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/orchestrator.py#L1265-L1273)，算出 `request_finished`，并指出最终输出经 `OutputMessage`（[messages.py:90](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/messages.py#L90)）送回前端。
5. **画出完整状态机**：横轴是时间，纵轴是 stage，标出每个箭头发生在哪个方法、哪一行，以及 `req_state` 在每个节点上的关键字段快照。

**验收标准**：你的图应当能回答——「为什么 stage 2 完成后请求就整体完成了？」「为什么 stage 0 完成时请求没有整体完成？」两个问题，且每个结论都附有具体的源码行号。

> 如果你想进一步用真实运行验证，可以开启 `--enable-orch-monitor`，观察一次 3 阶段请求期间各副本的 `inflight` 变化，应能看到请求依次「绑定到 stage 0 → 绑定到 stage 1 → 绑定到 stage 2 → 全部释放」的轨迹（待本地验证）。

## 6. 本讲小结

- `Orchestrator` 运行在独立后台线程的 asyncio 事件循环里，靠 `janus.Queue` 与主线程单向通信，内部有「请求下沉（`_request_handler`）」和「输出上浮（`_orchestration_loop`）」两条并发路径。
- `_orchestration_loop` 主动轮询所有 stage 的所有副本（diffusion 用 `nowait`、LLM 用 1ms 超时），忙则不休、闲则让步 1ms。
- 「阶段前推」的核心是 `_route_output` 里的组合条件，其中 `stage_id < final_stage_id`（非最终 stage）是判断要不要往后推的关键；真正的翻译与投递由 `_forward_to_next_stage` 完成，它按下一 stage 类型分 diffusion / PD / AR 三条分支。
- `OrchestratorRequestState` 用 `final_output_stage_ids ⊆ finished_final_output_stage_ids` 这一行子集判断决定请求整体完成，`stage_submit_ts` 既算指标又兼作「是否已投递」判据。
- `StagePool` 是 Orchestrator 与子进程之间的中间层，统一 `submit_initial/submit_update/poll_*` 接口，并通过 `_pick_or_select` 在「单机轮询」与「分布式负载均衡」间无缝切换；删副本时置 `None` 保下标稳定。
- 可选的 `OrchestratorMonitor` 按秒采样忙闲比与每副本积压，是定位多阶段流水线瓶颈的利器。

## 7. 下一步学习建议

- **向下钻一层**：本讲多次提到「经 ZMQ 投递到子进程」「`client.add_request_async`」。下一讲 [u3-l3（阶段进程与运行时：StageEngineCoreProc 与 StageRuntime）](u3-l3-stage-process-runtime.md) 会拆开这条 ZMQ 链路，讲清每个 stage 作为独立子进程如何启动、如何接收请求。
- **横向扩展**：`_forward_to_next_stage` 里 diffusion 分支提到的 `kv_sender_info`、以及 PD 分离部署分支，涉及 stage 之间的数据/ KV 传输，这正是 [u3-l4（全解耦通信：OmniConnector 体系）](u3-l4-omni-connectors.md) 的主题。
- **向深扩展**：如果你对分布式多副本调度感兴趣，`StagePool.pick` 里的 `LoadBalancer` 来自 OmniCoordinator，[u3-l5（OmniCoordinator：副本注册与负载均衡）](u3-l5-omni-coordinator.md) 会讲清 LEAST_QUEUE_LENGTH 等策略如何选副本。
