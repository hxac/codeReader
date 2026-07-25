# PyExecutor 单步循环

> 本讲承接 [u3-l1 请求全链路](./u3-l1-end-to-end-request-flow.md)。在 u3-l1 里，我们追踪到 `engine.enqueue_request` 是「分水岭」：之前全是 Python 编排，之后请求进入 `PyExecutor` 的内部循环。本讲就拆开这个「黑盒」，看一个请求在 PyExecutor 里如何被一步一步推进、最终吐出 token。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚 `PyExecutor` 的**单步（single-step）循环**做了哪些事：取请求 → 调度 → 资源准备 → 模型前向 → 采样 → 处理响应。
2. 在 `py_executor.py` 里定位到主循环方法（`_executor_loop` / `_executor_loop_overlap` / `_executor_loop_pp`）以及 `_handle_responses`、`_handle_executed_batch` 等关键处理函数。
3. 区分 `BatchState` 与 `BatchStatePP` 这两个数据类的用途。
4. 理解「重叠调度器（overlap scheduler）」与普通循环的本质差别。
5. 解释为什么在聚合**投机解码接受率统计**时，要跳过 `is_dummy`（attention-DP 填充 / CUDA Graph padding）请求。

## 2. 前置知识

阅读本讲前，建议你已经建立以下心智模型（u1/u2/u3-l1 已讲过）：

- **in-flight batching（持续批处理）**：服务端不是等凑齐一批再算，而是每个「步」都动态地决定哪些请求一起前向。这个「步」就是本讲要拆的单步循环。
- **Prefill 与 Decode 两阶段**：Prefill（首段、算力密集）和 Decode（逐 token、带宽密集）在同一个循环里混合调度，叫 chunked prefill / inflight batching。
- **Python 调度、C++ 加速**：循环本身、状态机、资源管理都在 Python；真正吃算力的 kernel（attention、GEMM、采样）走 C++/CUDA。
- **ResourceManager 三段式生命周期**：`prepare_resources`（步开始前分配）→ `update_resources`（步结束后更新）→ `free_resources`（请求结束时释放）。
- **投机解码（speculative decoding）**：用一个轻量「草稿器（drafter）」一次猜好几个 token，再用大模型一次性验证，被接受的 token 直接采纳，从而提升吞吐。衡量它有没有用的核心指标是**接受率（acceptance rate）**。

如果你还不熟悉上面这些，先回看 u3-l1 的全链路图。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tensorrt_llm/_torch/pyexecutor/py_executor.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py) | 本讲主战场。包含 `BatchState`/`BatchStatePP` 数据类、`PyExecutor` 类、三种事件循环、调度/前向/采样/响应处理等所有单步逻辑。 |
| [tensorrt_llm/_torch/pyexecutor/py_executor_creator.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor_creator.py) | 工厂函数 `create_py_executor`：加载模型、构建资源管理器/调度器/采样器，最后 `start_worker()` 启动循环线程。 |
| [tensorrt_llm/_torch/pyexecutor/llm_request.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/llm_request.py) | `LlmRequest` 定义。本讲引用它的 `is_dummy` 属性（三种 dummy 的并集）。 |
| [docs/source/torch/arch_overview.md](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/docs/source/torch/arch_overview.md) | 官方架构总览，给出单步流程和调度器/资源管理器的文字版定义。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 单步循环（主循环与三种变体）**、**4.2 调度与执行**、**4.3 响应处理与 is_dummy 排除**。

### 4.1 单步循环：主循环与三种变体

#### 4.1.1 概念说明

`PyExecutor` 是 PyTorch 后端真正驱动推理的「发动机」。它**不**像你写的那种「跑完一个 batch 就结束」的循环，而是一个**长驻的事件循环（event loop）**：在一个后台线程里 `while True`，每一圈（一个 iteration / 一步）把所有「活着」的请求向前推进一个 token。

这里有两个关键认知：

1. **循环是在一个独立线程里跑的。** 调用方（`LLM.generate`）只是把请求「提交」进队列就返回了；真正逐 token 的推进由 PyExecutor 的 worker 线程异步完成，结果通过 future 回传。这正是 in-flight batching 能一边收新请求一边吐 token 的根基。

2. **「单步」是循环的最小推进单位。** 每一步完成的动作，官方文档用一句话概括——

> 取新请求 → 调度 → 模型前向 → 解码/采样 → 追加 token 并处理完成的请求。

不过，根据**并行拓扑**的不同，这条主循环有三套实现：

| 变体 | 选择条件 | 特点 |
|------|---------|------|
| `_executor_loop` | `pp_size == 1` 且关闭重叠调度器 | 最朴素：同一步内完成前向、采样、响应处理。 |
| `_executor_loop_overlap` | `pp_size == 1` 且开启重叠调度器（默认） | 把「本步前向+采样」与「上一步的响应处理」重叠，CPU 调度与 GPU 前向并行。 |
| `_executor_loop_pp` | `pp_size > 1` | 流水线并行：按 microbatch 在各 pipeline stage 间流动，用队列解耦。 |

#### 4.1.2 核心流程

三种循环在「调度 → 前向 → 采样」这条主干上一致，差别在**响应处理发生在哪一步**。下面用伪代码画出三者的节奏（省略大量分支）：

```text
# 共同的「主干」
while True:
    scheduled_batch = _prepare_and_schedule_batch()   # 取请求 + 调度
    if scheduled_batch is None: break                 # 关机
    if _can_queue(scheduled_batch):                   # 这一圈要不要前向
        _prepare_resources()                          # 分配 KV cache 等
        batch_outputs = _forward_step()               # 模型前向（GPU）
        sample_state  = _sample_async()               # 采样
    _handle_响应()                                    # ← 三种循环在这里不同
    iter_counter += 1
```

三种循环的差别浓缩成一句话：

- `_executor_loop`：**当步前向 → 当步采样 → 当步处理响应**（线性，最易读）。
- `_executor_loop_overlap`：**当步前向 → 当步采样**；但**响应处理针对的是 `self.previous_batch`（上一步）**，从而让 GPU 跑前向时 CPU 已经在为下一步调度。
- `_executor_loop_pp`：每个 rank 只负责自己的 pipeline stage，microbatch 在 stage 间流动，用 `executed_batch_queue` 解耦「前向」与「执行完成后的处理」，后者由 `_handle_executed_batch` 处理。

#### 4.1.3 源码精读

**(1) 三种循环的派发**。在 `PyExecutor.__init__` 末尾，根据并行拓扑把 `self.event_loop` 绑到某个具体方法：

[py_executor.py:955-965](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L955-L965) —— 这段根据 `pp_size` 与 `disable_overlap_scheduler` 选择 `_executor_loop_pp` / `_executor_loop` / `_executor_loop_overlap`。

```python
if self.dist.pp_size > 1:
    self.event_loop = self._executor_loop_pp
    ...
else:
    self.event_loop = (self._executor_loop
                       if self.disable_overlap_scheduler
                       else self._executor_loop_overlap)
```

**(2) 循环在后台线程里启动**。`start_worker()` 创建并启动一个守护线程，它的入口是 `_event_loop_wrapper`：

[py_executor.py:1266-1269](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L1266-L1269) —— `start_worker` 把 `_event_loop_wrapper` 作为线程目标启动。

而 `_event_loop_wrapper` 只是一个带异常捕获和清理的壳，真正干活的是 `self.event_loop()`：

[py_executor.py:1182-1204](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L1182-L1204) —— 它在 `try` 里调用 `self.event_loop()`，出错时把异常暂存到 `self._event_loop_error`（避免本地消费者挂死），`finally` 里调用 `_executor_loop_cleanup()` 做收尾。

> 这个启动链的「工厂端」可以看 `create_py_executor`：它在完成模型/资源/调度器/采样器构建后，最后一句调用 `py_executor.start_worker()` 来真正点发动机。见 [py_executor_creator.py:1105](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor_creator.py#L1105)。

**(3) 朴素主循环 `_executor_loop` 的骨架**。这是三套里最清晰的，建议把它当作「标准答案」来读：

[py_executor.py:3984-4010](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L3984-L4010) —— `while True` 开头：`_prepare_and_schedule_batch()` 取一个批，返回 `None` 就 `break`（关机条件）。

```python
while True:
    self.hang_detector.checkpoint()
    ...
    scheduled_batch, iter_stats = self._prepare_and_schedule_batch()
    if scheduled_batch is None:
        break
```

随后在 `can_queue` 为真时执行主干（前向 + 采样 + 响应）：

[py_executor.py:4055](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4055) —— `resource_manager.prepare_resources(scheduled_batch)`（步开始前分配 KV 等资源）。

[py_executor.py:4119](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4119) —— `batch_outputs = self._forward_step(scheduled_batch)`（模型前向）。

[py_executor.py:4130](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4130) —— `sample_state = self._sample_async(scheduled_batch, batch_outputs)`（采样）。

[py_executor.py:4174](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4174) —— `finished_requests = self._handle_responses()`（产出 token、终止完成请求）。

[py_executor.py:4187](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4187) —— `resource_manager.update_resources(...)`（步结束后更新资源，例如回写 KV cache 状态）。

[py_executor.py:4222](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4222) —— `self.iter_counter += 1`（步进计数器，本步结束）。

**(4) 重叠循环 `_executor_loop_overlap` 的差别**。它的主干相同，但「响应处理」针对的是**上一步**的 `self.previous_batch`：

[py_executor.py:4617-4619](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4617-L4619) —— 本步先做前向（`_forward_step`）。

[py_executor.py:4623-4631](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4623-L4631) —— 紧接着对**上一步**的 `self.previous_batch` 调用 `_update_requests` 与 `_update_batch_acceptance_rate`。

[py_executor.py:4730-4739](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4730-L4739) —— 在本步前向+采样完成后，把当前 batch 快照存进 `self.previous_batch = BatchState(...)`，供**下一步**处理。

> 这就是「重叠」的实质：第 N 步在 GPU 上前向时，CPU 已经在第 N 步里把第 N-1 步的请求状态更新、KV 异步发送、响应发出去了。代价是有一个 step 的流水线延迟（首 token 会晚一拍），换来的吞吐提升通常很划算。

**(5) 流水线循环 `_executor_loop_pp` 与 `_handle_executed_batch`**。`pp_size > 1` 时，每个 rank 只算自己那段；microbatch 跨 stage 流动，完成的 microbatch 被塞进队列，再由 `_handle_executed_batch` 统一处理（更新请求、发 KV、处理响应、更新资源）：

[py_executor.py:3118-3141](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L3118-L3141) —— `_handle_executed_batch`：更新请求、（可选）更新接受率、发 KV、`_handle_responses()`、更新资源。

#### 4.1.4 代码实践

**实践目标**：在源码里把「三种循环 + 启动链」串起来，画一张状态/时序图。

**操作步骤**（纯源码阅读型，无需 GPU）：

1. 打开 `py_executor.py`，跳到 `start_worker`（约 L1218），确认它启动的线程目标是 `_event_loop_wrapper`。
2. 跳到 `_event_loop_wrapper`（约 L1182），确认它调用 `self.event_loop()`。
3. 跳到 `__init__` 末尾的派发段（约 L955-965），记录下三种 `event_loop` 的选择条件。
4. 分别打开 `_executor_loop`（L3984）、`_executor_loop_overlap`（L4453）、`_executor_loop_pp`（L2524），各只读 `while True` 的前 ~20 行与最后几行，找出它们「调用 `_handle_responses` / `_handle_executed_batch` 的位置」。

**需要观察的现象**：
- `_executor_loop` 的 `_handle_responses` 出现在**同一次** `can_queue` 分支内（当步当处理）。
- `_executor_loop_overlap` 的响应处理针对 `self.previous_batch`（跨步处理）。
- `_executor_loop_pp` 用 `_handle_executed_batch` 处理从队列取出的 `BatchStatePP`。

**预期结果**：你能画出三张「一格 = 一步」的时间线，标出每格里前向、采样、响应处理分别落在「本步」还是「上一步/队列」。

> 待本地验证：若有 GPU，可在不同 `Mapping`（`pp_size`、`disable_overlap_scheduler`）下打印 `self.event_loop.__name__`，确认派发结果与源码一致。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `disable_overlap_scheduler=True`，`pp_size=1`，会进哪个循环？为什么默认不选它？
> 答案：进 `_executor_loop`。它最简单、可读性高、调试友好，但放弃了对「CPU 调度」与「GPU 前向」的重叠，吞吐通常不如 `_executor_loop_overlap`，所以默认开启重叠。

**练习 2**：`_event_loop_wrapper` 在捕获异常时为什么**不**直接调用 `_handle_errors` / `_enqueue_responses`，而是只把异常暂存到 `self._event_loop_error`？
> 答案：注释里写得很清楚——那些路径会触发 `tp_gather` / `allgather` 之类的集合通信；如果只有一个 rank 崩溃，强行做集合通信会死锁。暂存异常 + cleanup 里的 `is_shutdown` 通知足以唤醒本地等待者，把异常向上抛。

---

### 4.2 调度与执行：取请求、调度、资源准备、前向、采样

#### 4.2.1 概念说明

这一模块聚焦单步循环的「上半段」：**怎么决定这一步让哪些请求进 GPU 算**。它解决三个问题：

1. **怎么取新请求**：从提交队列里把新请求捞进 `active_requests`。
2. **怎么调度**：调度器（Scheduler）决定哪些请求「有钱有资源」可以这一步算（CapacityScheduler），并从中挑出一个子集实际前向（MicroBatchScheduler）。
3. **怎么执行**：给选中的请求准备好 KV cache 等资源，跑模型前向，再采样。

一句话：调度器管「**能不能算**」，资源管理器管「**算之前要备好的料**」，前向+采样管「**真正算并出 token**」。

#### 4.2.2 核心流程

```text
_prepare_and_schedule_batch():
    new = _fetch_and_activate_new_requests()   # 从队列取新请求 → active_requests
    _handle_control_request()                  # 处理 sleep/wakeup 等控制请求
    _pad_attention_dp_dummy_request()          # attention-DP: 必要时补 dummy 占位
    _prepare_draft_requests()                  # 设置 draft tokens(投机解码)
    scheduled_batch = scheduler.schedule(...)  # 两步调度 → ScheduledRequests
    return scheduled_batch (或 None=关机)

can_queue = _can_queue(scheduled_batch)        # TP 全体是否都非空
if can_queue:
    _handle_dynamic_draft_len()                # 动态 draft 长度
    resource_manager.prepare_resources()       # 分配 KV cache 块等 ★三段式之一
    [drafter.prepare_draft_tokens()]           # 两段式投机: 先出草稿
    batch_outputs = _forward_step()            # 模型前向(在 execution_stream 上)
    sample_state  = _sample_async()            # 采样 → 选中 token
```

资源管理器的三段式生命周期在本步里被显式调用：`prepare_resources`（步开始）和 `update_resources`（步结束）都在循环里；`free_resources` 在请求完成时触发（详见 4.3）。

#### 4.2.3 源码精读

**(1) 取请求 + 调度 的入口 `_prepare_and_schedule_batch`**：

[py_executor.py:3592-3611](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L3592-L3611) —— 顺序很能说明问题：

```python
new_requests = self._fetch_and_activate_new_requests()   # 取新请求
if self.should_stop_processing: return None, None         # 关机
self._handle_control_request()                            # 控制请求
...
self._pad_attention_dp_dummy_request()                    # attention-DP 补 dummy
self._prefetch_for_context_requests()                     # 预取磁盘 KV 块
```

> `should_stop_processing` 为真就返回 `None`，主循环据此 `break`——这就是关机信号在单步层面的体现。

**(2) `can_queue`：这一步要不要真的前向**。在 attention-DP 下，这是 **TP 全体**共同的决定（任一 rank 批空就跳过）：

[py_executor.py:3281-3293](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L3281-L3293) —— 用 `tp_allgather` 聚合各 rank 批大小，`can_queue = 0 not in tp_batch_sizes`。

**(3) 资源准备**。注意它严格发生在 `can_queue` 为真之后：

[py_executor.py:4055](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4055) —— `self.resource_manager.prepare_resources(scheduled_batch)`。

**(4) 模型前向 `_forward_step`**。注意它把 forward 放到专门的 `execution_stream` 上跑，为的是和 KV cache 的 onboard/offload 传输正确同步：

[py_executor.py:6265-6303](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L6265-L6303) —— 核心是 `with torch.cuda.stream(self.execution_stream): outputs = forward(...)`，其中 `forward` 调用 `self.model_engine.forward(...)`（ModelEngine 的细节留待 u3-l3）。

```python
def forward(scheduled_requests, ...):
    return self.model_engine.forward(
        scheduled_requests, resource_manager, new_tensors_device,
        gather_context_logits=...,
        cache_indirection_buffer=...,
        num_accepted_tokens_device=...)
```

**(5) 采样 `_sample_async`**。它把前向产出的 logits 喂给采样器，得到本步每个请求选中（以及投机解码中接受）的 token：

[py_executor.py:6391-6419](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L6391-L6419) —— 先 `HandleLogits` 处理 logits，再 `self.sampler.sample_async(...)`。采样的具体参数（temperature/top_p/top_k 等）在 u8-l3 详讲。

#### 4.2.4 代码实践

**实践目标**：跟踪「一个请求被决定前向」的完整路径，体会调度器与资源管理器的分工。

**操作步骤**：

1. 在 `_prepare_and_schedule_batch` 里找到 `_fetch_and_activate_new_requests`，用 Grep 跳到它的定义，看请求如何从队列移入 `active_requests`。
2. 在 `_can_queue` 处加一行「示例代码」级别的心智注释（只读不改源码）：记录 `can_queue` 为 False 时，循环会走到 `_revert_gen_alloc`（回退 V2 调度器预先分配的 KV 容量）——见 [py_executor.py:4068](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4068) 与 [py_executor.py:3295-3311](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L3295-L3311)。
3. 阅读 `_forward_step` 的 `try/except`：它在异常时调 `_handle_errors` 并返回 `None`，思考「前向失败」如何被单步循环兜住。

**需要观察的现象**：
- `prepare_resources` 永远在 `_forward_step` 之前；`update_resources` 永远在 `_handle_responses` 之后（4.1.3 已标注）。三段式的顺序在循环里被严格遵守。

**预期结果**：你能复述「调度器决定能不能算 → 资源管理器准备料 → ModelEngine 前向 → Sampler 采样」这条因果链，并指出每一步在 `py_executor.py` 的行号。

> 待本地验证：在 `enable_iter_perf_stats=True` 时，观察 `_collect_scheduled_batch_stats` 是否正确反映了「实际进前向的请求数」而非「`active_requests` 总数」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `prepare_resources` 必须在 `_can_queue` 为真之后才调用，而不是调度完就立刻调？
> 答案：`can_queue` 可能因为 attention-DP 下某 rank 批空而为假——这一圈根本不前向。提前分配 KV 会白白占用并需要在 `_revert_gen_alloc` 里回退，V2 调度器更是已在调度阶段预分配了容量，必须显式回退以免「跨步累积、撑爆页索引缓冲」。

**练习 2**：前向为什么放在 `execution_stream` 而不是默认流？
> 答案：为了和 KVCacheTransferManager 的 onboard/offload 传输正确同步——前向产出的 KV 写入与跨节点 KV 搬运共享同一块显存，必须用流等待（`wait_stream`）保证先后顺序，否则在分离式服务里会出现数据竞争。

---

### 4.3 响应处理与 is_dummy 排除

#### 4.3.1 概念说明

单步循环的「下半段」是**响应处理**：把这一步采样出的 token 包成响应发回调用方，并处理「已经生成完」的请求（终止、释放资源）。本模块重点讲两个知识点：

1. **`_handle_responses` 的职责**：遍历 `active_requests`，决定每个请求要不要在这一步发 token、要不要终止；终止时走 `free_resources` 释放 KV 等资源（三段式的第三段）。

2. **`is_dummy` 与接受率统计的排除**。这是本讲对应最新代码变更（提交 `4b7d7199752f`，`[TRTLLM-14417][fix]`）的核心。简单说：

> 在 **attention 数据并行（attention-DP）** 和 **CUDA Graph** 场景下，系统会**凭空造出一些「假」请求**（dummy request）来填满 batch 形状（让每个 rank 都有 ≥1 个请求、让 batch 大小匹配 CUDA Graph 捕获时的形状）。这些假请求**不是真实用户请求**，它们的 draft/accept token 没有业务意义。

如果把这些假请求的草稿/接受 token 也算进**投机解码接受率**，就会扭曲这个关键指标，进而让「接受率太低就关掉投机解码」的自动门（`speculation_gate`）做出错误决定。所以聚合统计时必须把它们排除。

`LlmRequest` 提供了一个统一的 `is_dummy` 属性来识别这三类假请求：

[llm_request.py:951-953](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/llm_request.py#L951-L953) —— `is_dummy` 是 `is_attention_dp_dummy or is_cuda_graph_dummy or is_dummy_request` 的并集。

#### 4.3.2 核心流程

```text
_handle_responses():
    for request in active_requests:
        if request.is_attention_dp_dummy:        # 假请求: 直接排队终止, 不发响应
            requests_to_terminate.append(request); continue
        ... 超时 / 传输中 / 首token 等分支 ...
        if should_emit(按 decoding_iter / stream_interval):
            response = request.create_response(...)
            new_responses.append((req_id, response))
        if request_done: requests_to_terminate.append(request)
    _enqueue_responses(new_responses)             # 把响应推回调用方队列
    _terminate_requests(requests_to_terminate)    # 终止 + free_resources

# 接受率统计(单独的聚合函数):
_update_batch_acceptance_rate(scheduled_batch, sample_state):
    for request in generation_requests:
        if draft_len <= 0 or request.is_dummy:   # ← 排除 dummy
            continue
        total_draft_tokens   += request.num_draft_tokens
        total_accepted_tokens+= request.py_num_accepted_draft_tokens
    acceptance_rate = total_accepted_tokens / total_draft_tokens
    speculation_gate.record_acceptance_rate(acceptance_rate, ...)
```

接受率的定义是：

\[
\text{acceptance\_rate} \;=\; \frac{\sum_i \text{accepted\_draft\_tokens}_i}{\sum_i \text{draft\_tokens}_i}
\]

其中求和只覆盖**真实、且本步有草稿**的请求 \(i\)。把 dummy 算进去会同时污染分子（被接受数）和分母（草稿数），最糟的情况是 dummy 的 `py_draft_tokens` 被 padding 到 `max_total_draft_tokens`（为兼容 CUDA Graph），却几乎不被接受——这会把接受率人为压低，导致投机解码被误关。

#### 4.3.3 源码精读

**(1) `_handle_responses` 跳过假请求**。它对 `is_attention_dp_dummy` 的请求只做「排队终止」，绝不发响应：

[py_executor.py:6789-6794](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L6789-L6794) —— 注释直说「no responses for dummy request, and finish it」：

```python
# no responses for dummy request, and finish it
if request.is_attention_dp_dummy:
    requests_to_terminate.append(request)
    continue
```

随后按 `should_emit` 判定是否发 token，并组装响应：

[py_executor.py:6867-6878](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L6867-L6878) —— `response = request.create_response(False, self.dist.rank)`，把（含投机解码 per-position 命中统计的）响应加入 `new_responses`。

[py_executor.py:6896-6899](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L6896-L6899) —— 先 `active_requests` 用「未终止的」重建，再 `_enqueue_responses(new_responses)`（注释强调：必须先入队响应、再终止请求，确保入队成功）。

**(2) 接受率聚合显式排除 dummy**。这是本讲对齐最新代码的关键点。

聚合函数 `_update_batch_acceptance_rate`（驱动 `speculation_gate` 自动开关投机解码）：

[py_executor.py:2210-2222](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2210-L2222) —— 循环里 `if draft_len <= 0 or request.is_dummy: continue`，随后 `acceptance_rate = total_accepted_tokens / total_draft_tokens` 并喂给 `speculation_gate.record_acceptance_rate(...)`。

迭代级统计（写到 `stats.specdec_stats`，对外暴露接受长度）同样排除 dummy：

[py_executor.py:2030-2052](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2030-L2052) —— 注释 `# exclude attention dp dummy / CUDA Graph padding requests from AL calculation`，代码 `if getattr(req, 'is_dummy', False): continue`。

此外，统计采集阶段 `_is_stats_dummy_request` 与 `_collect_scheduled_batch_stats` 也按 `is_dummy` 过滤，保证「参与前向的请求计数」不被假请求污染：

[py_executor.py:1816-1818](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L1816-L1818) —— `_is_stats_dummy_request` 判定。

[py_executor.py:1823-1830](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L1823-L1830) —— `_collect_scheduled_batch_stats` 在 attention-DP 下对 context 请求按 `filter_dummies` 过滤。

**(3) 假请求从哪来**。`_pad_attention_dp_dummy_request` 在 attention-DP 且本 rank 没有可调度请求时，造一个 dummy 让每个 rank 都有 ≥1 个请求：

[py_executor.py:5756-5794](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L5756-L5794) —— 造出请求后 `llm_request.is_attention_dp_dummy = True`，并追加进 `active_requests`。

而 `_finalize_adp_dummy_allocation` 则在「TP 全体 `can_queue` 决策」做出后，**提交或回滚**这次本地试探性的 dummy 分配（因为 dummy 分配是 rank-local 的，但 `can_queue` 是 TP 全局的，某 rank 失败时其他成功的 rank 必须回滚，否则固定 dummy ID 会在每次跳过的步上泄漏缓存资源）：

[py_executor.py:3313-3345](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L3313-L3345) —— 根据 `can_queue` 决定提交还是回滚 dummy。

#### 4.3.4 代码实践

**实践目标**：本讲规格里要求的实践——在 `py_executor.py` 里定位主循环与 `_handle_responses` / `_handle_executed_batch`，并解释 `BatchState` 与 `BatchStatePP` 的区别、以及为何接受率统计要跳过 `is_dummy`。

**操作步骤**：

1. **定位与状态转换图**。用 Grep 在 `py_executor.py` 里搜 `def _handle_responses`（L6775）、`def _handle_executed_batch`（L3118）、`def _executor_loop`（L3984）、`def _executor_loop_overlap`（L4453）。把它们画进一张「一格=一步」的状态转换图：
   - 节点：`prepare_and_schedule` → `can_queue?` →（是）`prepare_resources` → `forward` → `sample` → `handle_responses` → `update_resources` → `iter_counter++`。
   - 在 overlap 版上用虚线标出 `previous_batch` 的跨步箭头。
   - 在 pp 版上用一条「队列」边连到 `_handle_executed_batch`。

2. **对比 `BatchState` 与 `BatchStatePP`**。读两个数据类：

   [py_executor.py:365-380](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L365-L380) —— `BatchState` 是「一步的快照」（`scheduled_requests` + `sample_state` + 各种计时事件），`BatchStatePP` 在其基础上多了 `microbatch_id`。

   **二者的区别**（答案要点）：
   - `BatchState`：用于 overlap 调度器（作为 `self.previous_batch` 跨步传递）和性能统计（`_process_iter_stats`）。它代表「**逻辑上的一步**」。
   - `BatchStatePP`：**仅**在流水线并行（`pp_size > 1`）里用。流水线下同时有多个 microbatch 在飞，它们被装进 `executed_batch_queue: Queue[BatchStatePP]`（见 `start_worker`，L1222-1225），每个带一个 `microbatch_id` 来标识自己。它代表「**一个流动的 microbatch**」。

3. **解释 is_dummy 排除**。把 [L2030-2052](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2030-L2052) 与 [L2210-2222](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2210-L2222) 两段并排读，写出一段说明，包含：dummy 是什么（attention-DP / CUDA Graph padding）、为什么不计（污染接受率分子分母、误导 `speculation_gate`）、`is_dummy` 由哪三个标志组成（见 [llm_request.py:951-953](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/llm_request.py#L951-L953)）。

**需要观察的现象**：
- `_handle_responses` 对 `is_attention_dp_dummy` 只终止不发响应；接受率聚合对 `is_dummy`（更宽，含 CUDA Graph dummy）整体跳过。两处口径一致地「不当真实请求对待」。

**预期结果**：你产出一图（单步状态转换）+ 两段文字（BatchState vs BatchStatePP、is_dummy 排除原因）。

> 待本地验证：在 attention-DP + 投机解码同时开启的场景下，用日志确认 dummy 请求确实进入了 `active_requests`、并在接受率计算中被跳过（可临时在 `_update_batch_acceptance_rate` 入口打印 `sum(request.is_dummy for ...)`）。

#### 4.3.5 小练习与答案

**练习 1**：`is_dummy`、`is_attention_dp_dummy`、`is_dummy_request` 三者是什么关系？`_handle_responses` 用的是哪个？
> 答案：`is_dummy` 是后两者的「并集」属性（`is_attention_dp_dummy or is_cuda_graph_dummy or is_dummy_request`，见 llm_request.py:951-953）。`_handle_responses` 只判 `is_attention_dp_dummy`（假请求一律不发响应、直接终止）；而接受率统计用更宽的 `is_dummy`，把 CUDA Graph padding 也排除掉。

**练习 2**：假设忘记在 `_update_batch_acceptance_rate` 里排除 dummy，且 dummy 的 `py_draft_tokens` 被 padding 成全零长度 `max_total_draft_tokens`，会出现什么后果？
> 答案：分母被「假草稿」显著放大，而假草稿几乎不被接受，导致 `acceptance_rate` 被人为压低；`speculation_gate` 据此可能把投机解码**永久关闭**（`speculation_permanently_disabled=True`），白白损失吞吐——正是提交 `4b7d7199752f` 修复的 bug。

**练习 3**：`BatchState` 和 `BatchStatePP` 都继承自同一组字段，为什么要分两个类？
> 答案：因为流水线并行下「一步」的概念不再单一——一个 rank 上同时有多个 microbatch 处于不同阶段，必须用 `microbatch_id` 区分并把它们放进队列解耦；非流水线场景没有 microbatch 概念，`BatchState` 足矣。分开建模让单步循环与流水线循环各自清晰。

## 5. 综合实践

**任务：给 `PyExecutor` 单步循环画一张「带分支注解的全景图」，并定位 is_dummy 修复点。**

1. 用你自己的工具（纸笔、mermaid、Excalidraw 均可）画出 `_executor_loop`（朴素版）的完整单步流程，要求：
   - 标出 `prepare_resources` / `_forward_step` / `_sample_async` / `_handle_responses` / `update_resources` 的相对顺序，并在每个节点旁标注 `py_executor.py` 的行号。
   - 用一个菱形表示 `_can_queue`，画出 `False` 分支（`_revert_gen_alloc` → 直接 `iter_counter++`）。
2. 在同一张图的角落，补一个「overlap 差异」小图：画出 `self.previous_batch` 如何从第 N 步传递到第 N+1 步的响应处理。
3. 标出三处 `is_dummy` 排除点（`_update_batch_acceptance_rate`、迭代级 specdec 统计、`_collect_scheduled_batch_stats`），并写一句话说明「如果不排除，speculation_gate 会怎样」。
4. 最后写一段「一句话总结」：PyExecutor 单步循环 = `__调度__ → __前向__ → __采样__ → __响应__`，其中响应处理在 overlap 版里针对上一步、在 pp 版里走 `_handle_executed_batch`。

> 完成后，把这张图与 [docs/source/torch/arch_overview.md:25-31](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/docs/source/torch/arch_overview.md#L25-L31) 的官方「single-step flow」文字对照，看看你是否覆盖了官方列出的全部动作（取请求/调度/前向/解码/追加 token 处理完成）。

## 6. 本讲小结

- `PyExecutor` 是 PyTorch 后端的「发动机」，在一个后台线程里跑长驻事件循环，每圈推进所有活跃请求一个 token——这就是 in-flight batching 的单步。
- 主循环有三套实现，按 `pp_size` 与 `disable_overlap_scheduler` 派发：朴素 `_executor_loop`、重叠 `_executor_loop_overlap`、流水线 `_executor_loop_pp`；三者主干一致，差别在响应处理落在「本步 / 上一步 / 队列」。
- 单步主干为：`_prepare_and_schedule_batch`（取请求+调度）→ `_can_queue` → `prepare_resources` → `_forward_step`（ModelEngine 前向）→ `_sample_async`（采样）→ `_handle_responses`（产出 token、终止完成请求）→ `update_resources`。
- `BatchState` 是「一步快照」（用于 overlap 的 `previous_batch` 与统计），`BatchStatePP` 在其上加 `microbatch_id`，仅供流水线并行的 microbatch 队列使用。
- 资源管理器的三段式 `prepare / update / free` 在循环里有明确的调用时机；调度器管「能不能算」，资源管理器管「备料」。
- 最新修复（提交 `4b7d7199752f`）确保 attention-DP / CUDA Graph 的 `is_dummy` 假请求被排除在**投机解码接受率统计**之外，避免污染 `speculation_gate` 的自动开关判定——`_handle_responses` 也对假请求只终止不发响应。

## 7. 下一步学习建议

- **深入调度器**：本讲把调度当成「调一次 `schedule()`」，下一站去 [u8-l1 调度器与 inflight batching](./u8-l1-scheduler-and-ifb.md)，拆 CapacityScheduler + MicroBatchScheduler 两步调度。
- **深入请求状态机**：本讲提到 `should_stop_processing`、`GENERATION_COMPLETE` 等状态，完整状态迁移见 [u8-l2 请求生命周期与状态机](./u8-l2-request-lifecycle-state-machine.md)。
- **深入 KV cache 与资源**：`prepare_resources`/`update_resources`/`free_resources` 背后的 KV cache 分配见 [u7-l1 分页 KV Cache 与 KVCacheManager](./u7-l1-paged-kv-cache-manager.md) 与 [u7-l2 ResourceManager 与 KV Cache 连接器](./u7-l2-resource-manager-and-connectors.md)。
- **深入模型前向**：`_forward_step` 委托给 `ModelEngine.forward`，下一讲 [u3-l3 ModelEngine 与模型前向](./u3-l3-model-engine-forward.md) 就拆它。
- **深入采样与投机解码**：`_sample_async` 的参数见 [u8-l3 Decoder 与 Sampling](./u8-l3-decoder-and-sampling.md)；本讲的 `is_dummy`/`speculation_gate` 后续在 [u10-l3 投机解码](./u10-l3-speculative-decoding.md) 完整展开。
