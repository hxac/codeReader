# 请求生命周期与状态机

## 1. 本讲目标

上一讲（u8-l1）我们打开了 `PyExecutor` 单步循环里的「调度黑盒」，看清了 inflight batching 与「容量调度 + 微批调度」两步机制。但那幅图里始终把「一个请求」当作一个不透明的点——它什么时候算 prefill、什么时候算 decode、什么时候被彻底结束？又是谁把一个刚到的请求从「等待」变成「正在跑」？

本讲要回答的是**单个请求的「一生」**：它从进入 `PyExecutor` 起到生成完最后一个 token 为止，会经历哪些状态、谁在什么时候搬动它的状态，以及并发时如何给每个请求分配一个唯一身份槽位。读完本讲你应该能够：

1. 复述 `LlmRequest` 在各状态间的迁移路径，画出（含分块 prefill、重叠调度、分离式服务的）状态迁移图。
2. 解释调度器如何用一个「数值窗口」一刀切出哪些状态可调度、哪些不可调度。
3. 说清楚「等待队列」`WaitingQueue` 在请求生命周期里的角色，以及 FCFS 与优先级两种实现差别。
4. 说明 `SeqSlotManager` 如何用「槽位池」为并发请求分配身份编号，并理解它为何是一种 `BaseResourceManager`。
5. 了解数据并行（Attention DP）场景下 `ADPRouter` 如何把新请求分发到不同 rank。

## 2. 前置知识

- **请求与生成任务**：一次 LLM 推理请求，本质是「给一段 prompt，要模型逐个吐出若干新 token」。模型处理 prompt 的阶段叫 **prefill（上下文阶段）**，逐个吐 token 的阶段叫 **decode（生成阶段）**。
- **inflight batching**：每个解码迭代都重新决定当前 batch 里有哪些请求（u8-l1 已讲）。这意味着请求必须有一种「我现在处于哪个阶段」的标记，调度器才能据此决定该不该把它放进这一步。
- **KV cache 与槽位**：为了让注意力后端能并发处理多个请求，每个请求在显存里要有一个稳定的「编号」（序列槽 / sequence slot），这样它每一步的 KV 缓存、采样输出才能对得上号。
- **状态机（state machine）**：一个对象有一组离散状态，外加在特定事件下从 A 状态迁移到 B 状态的规则。本讲的请求对象就是一个典型的状态机。
- **C++ 绑定**：`PyExecutor` 是 Python 写的，但请求对象 `LlmRequest` 与状态枚举却是 C++ 实现、经 nanobind 暴露给 Python 的（u2-l3 已讲）。Python 负责「搬状态」，C++ 负责「存状态」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `cpp/include/tensorrt_llm/batch_manager/llmRequest.h` | 定义 C++ 端 `LlmRequestState` 枚举（状态机的「真身」），数值是设计过的。 |
| `tensorrt_llm/_torch/pyexecutor/llm_request.py` | Python 侧 `LlmRequest` 包装类，导入状态枚举、封装 `finish_by` 等行为，并挂载大量 `py_*` 旁路字段。 |
| `tensorrt_llm/_torch/pyexecutor/scheduler/waiting_queue.py` | 等待队列：`WaitingQueue` 抽象基类 + FCFS / 优先级两种实现 + 工厂。 |
| `tensorrt_llm/_torch/pyexecutor/seq_slot_manager.py` | 序列槽管理器：把「槽位」当资源管理，是 `BaseResourceManager` 的实现。 |
| `tensorrt_llm/_torch/pyexecutor/resource_manager.py`（节选） | `SlotManager`：真正存「空闲槽集合」与「请求→槽」映射的底层数据结构。 |
| `tensorrt_llm/_torch/pyexecutor/scheduler/adp_router.py` | Attention 数据并行路由器：把新请求分发到不同 rank。 |
| `tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py`（节选） | 调度器用「状态值窗口」判断一个请求是否可调度。 |
| `tensorrt_llm/_torch/pyexecutor/py_executor.py`（节选） | 请求生命周期编排者：拉取→入等待队列→出队列→分发→搬运状态。 |

## 4. 核心概念与源码讲解

### 4.1 LlmRequest 与请求状态机

#### 4.1.1 概念说明

在 `PyExecutor` 内部，每一个被服务的请求都被表示成一个 `LlmRequest` 对象。它在请求的整个生命周期里持续存在——从「刚被拉进来」一直到「生成结束、结果返回」。随着推理推进，这个对象会在一组**状态**之间迁移：

- **CONTEXT_INIT（上下文阶段，即 prefill）**：请求刚进来、正在算 prompt（可能分块）。
- **GENERATION_IN_PROGRESS（生成进行中，即 decode）**：prompt 已算完，正在逐个吐 token。
- **GENERATION_TO_COMPLETE（即将完成）**：下一步就要生成出最后一个 token。
- **GENERATION_COMPLETE（已完成）**：结束，准备回收资源。

这套状态是 C++ 定义的，因为状态字段真正存在 C++ 的请求对象里；Python 侧通过一行导入拿到同一个枚举：

[llm_request.py:28](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/llm_request.py#L28) 把 C++ 的 `LlmRequestState` 枚举引入 Python 命名空间，Python 与 C++ 用的是同一份状态定义。

为什么要把状态放在 C++？因为状态值要被 C++ 的批管理器、采样器等高性能路径高频读取；放 C++ 既快，又保证 Python/C++ 两端语义一致。

#### 4.1.2 核心流程：状态的数值是「刻意排过序」的

最关键、也最巧妙的设计，藏在 C++ 枚举的数值里。下面是 [llmRequest.h:49-76](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/cpp/include/tensorrt_llm/batch_manager/llmRequest.h#L49-L76) 的完整定义（关键看每个状态后面的数值）：

```
kUNKNOWN                              = 0
kENCODER_INIT                         = 1   // 编码器阶段（encoder-decoder 模型）
kDISAGG_CONTEXT_WAIT_SCHEDULER        = 7
kDISAGG_GENERATION_INIT               = 8   // 分离式：新生成请求刚到 decode 节点
kDISAGG_GENERATION_TRANS_IN_PROGRESS  = 9   // 分离式：正在传 KV cache

// ---- 可调度状态 开始 ----
kCONTEXT_INIT                         = 10  // 上下文阶段（prefill）
kDISAGG_CONTEXT_INIT_AND_TRANS        = 11  // 分离式：prefill 与 KV 传输并行
kDISAGG_GENERATION_TRANS_COMPLETE     = 12  // 分离式：KV 传输完成
kGENERATION_IN_PROGRESS               = 13  // 生成进行中（decode）
// ---- 可调度状态 结束 ----

kGENERATION_TO_COMPLETE               = 14  // 即将完成
kGENERATION_COMPLETE                  = 20  // 已完成
kDISAGG_CONTEXT_TRANS_IN_PROGRESS     = 21
kDISAGG_CONTEXT_COMPLETE              = 22
kDISAGG_GENERATION_WAIT_TOKENS        = 23
kDISAGG_TRANS_ERROR                   = -1  // 传输错误
```

注意 C++ 注释里写的 `// schedulable states starts`（可调度状态开始）和 `// schedulable states ends`（可调度状态结束）。**所有「可调度」的状态——`10, 11, 12, 13`——恰好构成一个连续的整数区间 `[10, 14)`**。

这绝不是巧合。调度器判断「这个请求现在能不能被排进这一步」时，用的就是一个数值比较：

[scheduler.py:510-524](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L510-L524) `_can_be_scheduled` 只做一件事——判断 `state_value >= no_schedule_until_state_value and state_value < no_schedule_after_state_value`。其中默认下界是 `CONTEXT_INIT (10)`、上界是 `GENERATION_TO_COMPLETE (14)`（见 [scheduler.py:493-494](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L493-L494) 与 [scheduler_v2.py:152-153](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler_v2.py#L152-L153)）。

也就是说，调度器根本不认识「状态名字」，它只看一个整数是否落在窗口 `[10, 14)` 内：

- `CONTEXT_INIT=10`、`GENERATION_IN_PROGRESS=13` 在窗口内 → **可调度**。
- `GENERATION_TO_COMPLETE=14` 等于上界 → **不在窗口内**（区间是左闭右开），意思是「即将完成的请求不再进入新一轮调度」。
- `DISAGG_GENERATION_INIT=8`、`GENERATION_COMPLETE=20` 都在窗口外 → **不可调度**。

这种「把语义压进数值排序」的设计，让一次「是否可调度」的判断退化成两次整数比较——O(1)、无分支、对 Python 热路径友好。代价是：**新增一个状态时必须谨慎安排它的数值**，不能随手给，否则会破坏「可调度 = 连续区间」这一不变量。这也是为什么 `GENERATION_COMPLETE=20` 故意和 `GENERATION_TO_COMPLETE=14` 隔得很远——给「可调度区间」与「完成后状态」之间留出数值缓冲。

> 小贴士：代码里频繁用 `req.state_value`（直接返回 int）而非 `req.state`（返回枚举对象），正是为了省掉每次创建枚举对象的开销（见 [scheduler.py:518-519](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/scheduler.py#L518-L519) 的注释）。

#### 4.1.3 源码精读：状态由谁、在何时迁移

请求状态不是请求自己改的，而是由 `PyExecutor` 在单步循环的固定节点上「集中搬动」。最核心的两处迁移发生在每步前向之后：

**① prefill 推进 / 完成（CONTEXT_INIT → GENERATION_IN_PROGRESS）**

[py_executor.py:6334-6363](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L6334-L6363) `_update_request_states_tp` 处理 `context_requests`：每算完一个 prefill 分块就 `move_to_next_context_chunk()`；当 `context_remaining_length == 0`（prompt 全部算完），根据是否开启重叠调度把状态迁到 `GENERATION_IN_PROGRESS` 或 `GENERATION_TO_COMPLETE`：

```python
# 关键节选：prefill 完成后的状态迁移
if request.context_remaining_length == 0:
    ...
    if not self.disable_overlap_scheduler and request.will_complete_next_iteration():
        request.set_exclude_last_generation_logits(False)
        request.state = LlmRequestState.GENERATION_TO_COMPLETE
    else:
        request.state = LlmRequestState.GENERATION_IN_PROGRESS
```

**② 生成即将结束（GENERATION_IN_PROGRESS → GENERATION_TO_COMPLETE）**

[py_executor.py:6321-6332](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L6321-L6332) 专门处理「下一步就要结束」的请求——当 `will_complete_next_iteration()` 为真（即剩余生成长度已到尽头），提前打上 `GENERATION_TO_COMPLETE`，以便重叠调度器据此调整 logits 行为。

**③ 彻底完成（→ GENERATION_COMPLETE）**

`llm_request.py` 里 Python 侧封装了一个便捷方法 [llm_request.py:955-958](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/llm_request.py#L955-L958)：`finish_by` 把状态直接设成 `GENERATION_COMPLETE` 并写上结束原因。除此之外，响应处理阶段（`_handle_responses`）也会把生成结束的请求搬进 `GENERATION_COMPLETE`（例如 [py_executor.py:6555](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L6555)）。

把这些迁移点串起来，就得到一张普通（非分离式、含分块 prefill）请求的状态迁移图：

```
              ┌─────────────────────────────────────────────┐
              │  （每步）move_to_next_context_chunk()          │
              ▼                                             │
新请求 ──► [CONTEXT_INIT] ──prefill 完成──► [GENERATION_IN_PROGRESS]
          prefill 分块       (context_remaining_length==0)   │
              │                                              │
              │ （重叠调度 + 下一步结束）will_complete_next_iteration()
              ▼                                              ▼
          [GENERATION_TO_COMPLETE] ◄─────── 下一步即将结束 ──┘
                      │
                      │  生成出最后一个 token / finish_by()
                      ▼
                [GENERATION_COMPLETE] ──► 资源回收、返回结果
```

需要强调几点：

- **同一个请求同时只处于一个状态**，迁移是线性的。
- **`will_complete_next_iteration()` 是「提前一拍」信号**，让重叠调度器能预判「这一步之后请求就结束了」，从而正确处理多算的那一个 token 的 logits（这是 u3-l2 讲过的重叠调度的产物）。
- **分离式服务会引入额外状态**：`DISAGG_GENERATION_INIT (8)`、`DISAGG_GENERATION_TRANS_IN_PROGRESS (9)`、`DISAGG_GENERATION_TRANS_COMPLETE (12)` 等。它们的数值被特意排在窗口边缘——`DISAGG_GENERATION_TRANS_COMPLETE=12` 落在可调度窗口内（KV 到齐、可以开跑），而 `DISAGG_GENERATION_INIT=8` 落在窗口外（还在等 KV，不能跑）。这是状态数值设计的第二个妙用。

#### 4.1.4 代码实践

**实践目标**：亲手从源码确认状态迁移的触发条件，验证「可调度窗口」语义。

**操作步骤**：

1. 打开 `py_executor.py`，分别跳到 `_update_request_states_tp`（约 6334 行）与 `_update_generation_requests_that_will_complete_next_iteration`（约 6321 行），逐行确认 `request.state = ...` 的赋值与各自的前置条件。
2. 打开 `scheduler.py` 的 `_can_be_scheduled`（约 510 行），记下窗口下界、上界的默认值。
3. 对照 `cpp/include/tensorrt_llm/batch_manager/llmRequest.h`（约 49 行）的枚举数值，填出下表。

**需要观察的现象 / 预期结果**：

| 状态 | 数值 | 是否在默认窗口 `[10,14)` 内 | 是否可调度 |
|------|------|------|------|
| CONTEXT_INIT | 10 | 是 | 是 |
| DISAGG_GENERATION_TRANS_COMPLETE | 12 | 是 | 是 |
| GENERATION_IN_PROGRESS | 13 | 是 | 是 |
| GENERATION_TO_COMPLETE | 14 | 否（左闭右开） | 否 |
| DISAGG_GENERATION_INIT | 8 | 否 | 否 |
| GENERATION_COMPLETE | 20 | 否 | 否 |

4. **思考题**：如果把 `GENERATION_TO_COMPLETE` 的数值从 14 改成 13（和 `GENERATION_IN_PROGRESS` 撞号），会发生什么？（答案见 4.1.5）

> 说明：本实践为「源码阅读型」，不需要 GPU 或运行模型——它考察的是你对状态机数值设计的理解。若想在运行中观察状态，需要带 GPU 的环境，属于「待本地验证」的进阶玩法。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `GENERATION_COMPLETE` 的数值是 20，而不是紧接 `GENERATION_TO_COMPLETE` 的 15？

**参考答案**：因为可调度状态被设计成一个**连续区间** `[10, 14)`。如果把后续状态（如 `GENERATION_COMPLETE`）紧贴着排，将来新增状态时很容易「挤进」可调度区间、破坏不变量。用 `20` 拉开距离，等于在「可调度区间」与「终态/分离态」之间留出数值缓冲，让「新增状态」时更容易安排数值。

**练习 2**：一个开启了重叠调度、且 `max_new_tokens=1` 的请求，会经过 `GENERATION_IN_PROGRESS` 吗？

**参考答案**：不一定。若 prefill 完成时 `will_complete_next_iteration()` 已为真（生成长度只有 1，下一步就结束），代码会直接从 `CONTEXT_INIT` 跳到 `GENERATION_TO_COMPLETE`，**跳过** `GENERATION_IN_PROGRESS`（见 6334–6363 行的 `if/else` 分支）。

### 4.2 等待队列（WaitingQueue）

#### 4.2.1 概念说明

请求到了 `PyExecutor` 之后，并不会立刻被塞进调度器。原因有二：一是**容量有限**——当前正在跑的请求数（`active_requests`）已经达到上限时，新请求必须排队等空位；二是**调度节奏**——单步循环每步只取一批，取不下的要在队列里候着。

这个「候车室」就是**等待队列** `WaitingQueue`。它的职责很纯粹：

- `add_request`：新请求入队。
- `pop_request`：按策略取一个出来交给调度。
- `prepend_request`：取出来但调度失败（容量不够），把它**原样塞回队首**，确保它不会因为「取出来又放回去」而丢掉自己原本的排队优先级。

#### 4.2.2 核心流程：两种排队策略

[waiting_queue.py:29-93](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/waiting_queue.py#L29-L93) 定义了 `WaitingQueue` 抽象基类，规定了上面的接口契约。它有两个具体实现：

**① FCFS 队列**（先来先服务）

[waiting_queue.py:96-158](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/waiting_queue.py#L96-L158) `FCFSWaitingQueue` 直接继承 Python 内置 `deque`（双端队列）：

- 入队 `append`（队尾），出队 `popleft`（队首）——经典 FIFO。
- `prepend_request` 用 `appendleft` 塞回队首，保证调度失败的请求下一次最先被取到。
- 如果请求带了优先级但用的是 FCFS 队列，[waiting_queue.py:99-106](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/waiting_queue.py#L99-L106) 会打 warning 提醒「优先级将被忽略」。

**② 优先级队列**

[waiting_queue.py:161-263](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/waiting_queue.py#L161-L263) `PriorityWaitingQueue` 用最小堆实现。每个堆元素是一个三元组：

```
(neg_priority, insertion_counter, item)
```

这里有两个精巧点（[waiting_queue.py:179-201](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/waiting_queue.py#L179-L201)）：

- **`neg_priority`（优先级取负）**：用最小堆模拟「优先级越高越先出」——优先级高 → 取负后数值小 → 堆顶。
- **`insertion_counter`（单调递增计数器）**：作为同优先级内的 FCFS 平局规则。因为计数器严格递增，任意两个元素的前两元都不会完全相同，于是 Python 的 `heapq` **永远不需要比较第三元 `item`**——`item` 因此不必实现 `__lt__`。这是一个非常实用的工程技巧（避免给复杂请求对象写比较函数）。

`prepend_request`（调度失败回塞）用的是另一个**单调递减**的计数器 `_prepend_counter`（从 -1 起，每次 -1），保证回塞的请求排在「所有正常入队请求」之前，从而不丢失排队位置（[waiting_queue.py:225-232](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/waiting_queue.py#L225-L232)）。这正是抽象基类对 `prepend_*` 方法的契约要求（[waiting_queue.py:53-63](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/waiting_queue.py#L53-L63) 的 docstring）。

两种实现通过工厂 [waiting_queue.py:266-282](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/waiting_queue.py#L266-L282) `create_waiting_queue(policy)` 按 `WaitingQueuePolicy.FCFS` / `PRIORITY` 选择，`PyExecutor` 在初始化时调用它（[py_executor.py:880-881](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L880-L881)）。

#### 4.2.3 源码精读：等待队列在单步循环里的位置

请求生命周期里，等待队列是「外部世界」与「调度器」之间的缓冲层。每步循环里 `_fetch_new_requests` 负责这一段（[py_executor.py:4994-5081](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4994-L5081)）：

```python
# 1. 算出当前总并发数（含数据并行时需 allgather 各 rank）
# 2. 把外部新到请求 fetch 进等待队列（_fetch_and_enqueue_requests）
# 3. 按可用容量从等待队列 pop 一批（_pop_from_waiting_queue）
# 4.（可选）ADP 路由分发到各 rank（见 4.4）
# 5. 合并成可执行的新请求列表返回
```

其中「pop 多少」由 `_pop_from_waiting_queue` 决定（[py_executor.py:4964-4992](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4964-L4992)）：`max_new_requests = total_max - total_num_active_requests`——也就是「总上限减去正在跑的」，剩下的名额才从队列里取。这正解释了「为什么需要等待队列」：并发满时名额为 0，新请求只能在队列里等，直到有老请求完成、腾出名额。

#### 4.2.4 代码实践

**实践目标**：验证优先级队列的「三元组 + 双计数器」设计，理解为何 `item` 不需要实现比较。

**操作步骤**：

1. 阅读 [waiting_queue.py:179-201](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/waiting_queue.py#L179-L201)，注意 `_counter`（从 0 递增）与 `_prepend_counter`（从 -1 递减）的方向相反。
2. 写一段**示例代码**（非项目代码）在本地复现这个堆行为：

```python
# 示例代码：演示 PriorityWaitingQueue 的堆元组设计
import heapq, itertools
heap = []
counter = itertools.count()          # 正常入队：0,1,2,...
prepend = itertools.count(-1, -1)    # 回塞入队：-1,-2,-3,...

def push(prio, name):
    heapq.heappush(heap, (-prio, next(counter), name))

def push_front(prio, name):
    heapq.heappush(heap, (-prio, next(prepend), name))

push(1, "A"); push(1, "B"); push_front(1, "R")  # R 是回塞的
print([heapq.heappop(heap)[2] for _ in range(3)])  # 预期: ['R', 'A', 'B']
```

**需要观察的现象 / 预期结果**：回塞的 `R` 因为拿到负计数器（-1），比正常入队的 A(0)、B(1) 都小，于是最先被弹出。即便 A、B 优先级相同，A 因计数器更小而先于 B——这就是「同优先级 FCFS」。

**预期结果**：`['R', 'A', 'B']`。本实践可在任意有 Python 的机器验证，无需 GPU。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `PriorityWaitingQueue` 要用「取负优先级」的最小堆，而不是直接用最大堆？

**参考答案**：Python 标准库 `heapq` 只提供最小堆。用「负优先级」是把「优先级越大越先出」翻转为「数值越小越靠堆顶」，从而无需自己实现最大堆。这是 Python 里模拟优先级队列的惯用法。

**练习 2**：FCFS 队列的 `prepend_requests` 用了 `extendleft(reversed(...))`（[waiting_queue.py:134-140](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/waiting_queue.py#L134-L140)），为什么要 `reversed`？

**参考答案**：`deque.extendleft` 是**逐个从左塞入**的，先塞的会被后续塞的「挤到右边」。若想保持输入顺序（第一个元素最终在最左/队首），必须先 `reversed`，让最后一个元素最先被塞入、被后续元素挤到正确位置。

### 4.3 序列槽管理（SeqSlotManager）

#### 4.3.1 概念说明

光知道「请求处于哪个状态」还不够。当多个请求并发时，注意力后端、采样器、KV cache 都需要一个稳定的整数编号来区分「这一步的张量属于哪个请求」。这个编号就是**序列槽（sequence slot）**——一个介于 `0` 和 `max_batch_size` 之间的整数。

你可以把它想象成医院的「就诊号」：每个正在被服务的病人（请求）持有一个唯一号码，服务结束（请求完成）后号码回收，可以发给下一个病人。号池大小就是最大并发数。

#### 4.3.2 核心流程：把「槽位」当成一种资源来管理

这里有一个贯穿 u7 已建立的认知：**任何随请求生灭的东西，都被统一抽象成「资源」，由 `BaseResourceManager` 用 `prepare / update / free` 三段式生命周期管理**（u7-l2）。序列槽正是这样一种资源——它在请求被调度时分配、请求完成时释放。

[seq_slot_manager.py:6-32](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/seq_slot_manager.py#L6-L32) 的 `SeqSlotManager` 就是 `BaseResourceManager` 的一个实现，体积极小但五脏俱全：

- **容量接口**（给调度器做准入控制，u7-l2 讲过）：
  - `get_max_resource_count()` 返回 `max_num_requests`（[seq_slot_manager.py:11-12](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/seq_slot_manager.py#L11-L12)）——并发请求总数不能超过槽位数。
  - `get_needed_resource_to_completion()` 恒返回 `1`（[seq_slot_manager.py:14-15](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/seq_slot_manager.py#L14-L15)）——**每个请求从头到尾只需要一个槽**（区别于 KV cache 那种「越长越多」的资源）。
- **生命周期接口**：
  - `prepare_resources`：在请求被调度时，给没有槽的请求分配槽（[seq_slot_manager.py:17-29](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/seq_slot_manager.py#L17-L29)）。
  - `free_resources`：请求结束时回收槽（[seq_slot_manager.py:31-32](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/seq_slot_manager.py#L31-L32)）。

#### 4.3.3 源码精读：底层数据结构 SlotManager

真正存「空闲槽」与「请求→槽映射」的，是 [resource_manager.py:2299-2335](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/resource_manager.py#L2299-L2335) 的 `SlotManager`。它的实现简单而典型——一个「池」加一个「映射」：

```python
class SlotManager:
    def __init__(self, max_num_requests: int):
        self.max_num_requests = max_num_requests
        self.slot_mapping = dict()                       # request_id -> slot
        self.free_slots = set(range(max_num_requests))   # 空闲槽池

    def add_slot(self, request_id):
        ...
        if len(self.free_slots) == 0:
            raise NoFreeSlotsError("No free slots")      # 池空了 → 报错
        slot = self.free_slots.pop()                     # 从池里取一个
        self.slot_mapping[request_id] = slot
        return slot

    def remove_slot(self, request_id):
        if request_id in self.slot_mapping:
            slot = self.slot_mapping.pop(request_id)
            self.free_slots.add(slot)                    # 归还到池
```

这是一个教科书式的「对象池」模式：

- 初始化时把 `{0, 1, ..., max-1}` 全部放进 `free_slots`。
- 分配：`free_slots.pop()`（取一个、从池中移除），记进 `slot_mapping`。
- 释放：从 `slot_mapping` 删掉，把号还回 `free_slots`。
- 池空时 `add_slot` 抛 `NoFreeSlotsError`——这正是「并发已满、不能再加」的硬性护栏。

`SeqSlotManager.prepare_resources` 在调用 `add_slot` 之前还有两处特判（[seq_slot_manager.py:17-29](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/seq_slot_manager.py#L17-L29)）：

1. **跳过 `is_disagg_generation_init_state` 的请求**：分离式服务里，刚到 decode 节点的生成请求还没收到 KV cache、没真正开跑，暂不占槽（避免占着茅坑不拉屎）。
2. **`seq_slot is None` 或 `is_disagg_generation_transmission_complete` 才分配**：前者是首次分配；后者是分离式请求在 KV 传完后**重新分配**一个 decode 侧的正式槽位。

此外注意一个小细节：CUDA Graph 的 dummy 请求（占位假请求）会用一个固定 id 反复进不同 batch，[resource_manager.py:2318-2330](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/resource_manager.py#L2318-L2330) 的 `add_slot` 对它「只分配一次、后续复用」，避免重复占用多个槽。

#### 4.3.4 代码实践

**实践目标**：用一个最小 Python 脚本复现 `SlotManager` 的池化行为，直观感受「并发满时抛错」。

**操作步骤**：

1. 复制 [resource_manager.py:2299-2335](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/resource_manager.py#L2299-L2335) 里 `SlotManager` 的逻辑（去掉 `fill_slot_id_tensor`、`shutdown` 等非必要部分，保留 `add_slot`/`remove_slot`），写一段**示例代码**：

```python
# 示例代码：演示槽位池的分配与回收
class SlotManager:
    def __init__(self, max_num_requests):
        self.max_num_requests = max_num_requests
        self.slot_mapping = {}
        self.free_slots = set(range(max_num_requests))
    def add_slot(self, rid):
        if not self.free_slots:
            raise RuntimeError("No free slots")
        s = self.free_slots.pop()
        self.slot_mapping[rid] = s
        return s
    def remove_slot(self, rid):
        if rid in self.slot_mapping:
            self.free_slots.add(self.slot_mapping.pop(rid))

sm = SlotManager(2)        # 最多 2 路并发
print(sm.add_slot(101))    # 预期 1（set.pop 顺序不保证，可能是 0 或 1）
print(sm.add_slot(102))    # 预期 0 或 1（另一个）
try:
    sm.add_slot(103)       # 并发满 → 报错
except RuntimeError as e:
    print("blocked:", e)
sm.remove_slot(101)        # 释放一个
print(sm.add_slot(103))    # 现在又能分配了
```

**需要观察的现象 / 预期结果**：

- 第三次 `add_slot(103)` 抛出 `No free slots`（对应源码的 `NoFreeSlotsError`），证明并发上限由池大小硬性卡住。
- `remove_slot` 之后，释放的槽被 `103` 复用——同一编号在不同时刻可服务不同请求。

**预期结果**：能稳定复现「池空报错 → 释放后可复用」的行为。本实践无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `get_needed_resource_to_completion` 返回 `1` 而不是「随生成长度增长」？

**参考答案**：序列槽是**身份编号**，不是**存储容量**。一个请求无论生成 1 个 token 还是 1000 个 token，始终只需要一个编号来标识自己；真正随长度增长的是 KV cache（那是 `KVCacheManager` 的资源，见 u7-l1）。把「需要多少资源才能跑完」返回 1，意味着准入控制只需关心「当前并发数 vs 槽位总数」。

**练习 2**：如果 `SeqSlotManager` 不在 `free_resources` 里调用 `remove_slot`，会发生什么？

**参考答案**：完成的请求占用的槽永不回收，`free_slots` 池会逐渐枯竭，新请求迟早因 `NoFreeSlotsError` 进不来——表现为「服务跑一会儿后就无法接收新请求」。这正是「资源泄漏」在槽位维度的体现，也说明 `prepare/free` 必须成对出现。

### 4.4 ADP Router：数据并行下的请求分发（了解）

#### 4.4.1 概念说明

前面三个模块都假设「一个实例内，所有请求由一个调度器统一管」。但当开启 **Attention 数据并行（Attention Data Parallelism, ADP）** 时，一个实例里会有多个 TP rank（可视为多个并行的「工作小组」），新请求需要决定**送到哪个 rank**。这个决策由 **ADP Router** 负责。

ADP Router 是**实例级**的路由器（[adp_router.py:150-157](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/adp_router.py#L150-L157) 的 docstring 明确说明）——它只管一个实例内 DP rank 之间分发；跨实例（如分离式 prefill/decode 之间）的路由不在它的职责范围。

> 提醒：不要把 ADP Router 与分离式服务里「跨节点搬 KV cache」的 transceiver（u7-l2、u11-l2）混淆。前者是「新请求分到哪个工作小组」，后者是「KV 数据怎么搬」。

#### 4.4.2 核心流程：allgather 共识 + 本地路由

ADP 路由的协议很优雅（[adp_router.py:5-14](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/adp_router.py#L5-L14)）：

1. 每个 rank 用本地信息构造一个 `RankState`（[adp_router.py:90-116](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/adp_router.py#L90-L116)），记录自己的 `rank`、`num_active_requests`（当前并发数）、`num_active_tokens`（当前 token 负载）。
2. 所有 rank 把各自的 `RankState` 序列化后做一次 **allgather**，于是每个 rank 都拿到全部 rank 的状态（[adp_router.py:243-262](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/adp_router.py#L243-L262)）。
3. **关键点**：因为每个 rank 拿到的「全局状态」完全相同，且 `route_requests` 是**纯函数**（相同输入 → 相同输出），所以每个 rank 各自本地算一遍，得到的「请求→rank」分配结果**天然一致**，无需再广播。这是分布式系统里用「相同输入 + 确定性函数」省一次通信的常见手法。

这套机制在 `py_executor.py` 的 `_fetch_new_requests` 里被串起来（[py_executor.py:5014-5062](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L5014-L5062)）：先 `gather_all_rank_states`，再 `route_requests`，最后每个 rank 只取「分给自己」的那一份。

#### 4.4.3 源码精读：三种路由策略

[adp_router.py:169-224](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/adp_router.py#L169-L224) 的工厂 `ADPRouter.create` 根据配置选三种实现：

- **`DefaultADPRouter`**（默认，[adp_router.py:338-480](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/adp_router.py#L338-L480)）：用最小堆，以 `(num_active_tokens, num_active_requests)` 为键做负载均衡——哪个 rank 当前负担轻（token 少），就把新请求给它。请求按 token 数降序排好再分配，便于均衡（[adp_router.py:419-480](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/adp_router.py#L419-L480) `_balance_requests_across_ranks`）。
- **`KVCacheAwareADPRouter`**（[adp_router.py:483-796](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/adp_router.py#L483-L796)）：在负载均衡之外，还考虑 KV cache 的前缀命中长度——若某 rank 已经缓存了这个请求的前缀，优先分给它，省掉重复 prefill。它的打分公式是

  \[ \text{score}(\text{rank}) = (\text{req\_tokens} - \text{match\_len}) + \beta \cdot \text{normalized\_load} \]

  分数越低越优：第一项是「这个 rank 上还要实算多少 token」（缓存命中越多越省），第二项是负载惩罚（[adp_router.py:608-628](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/adp_router.py#L608-L628) `_score_rank`）。
- **`ConversationAwareADPRouter`**（[adp_router.py:799-960](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/adp_router.py#L799-L960)）：把同一个 `conversation_id` 的多轮对话**粘**在同一个 rank，让对话前缀 KV 一直留在那个 rank 上，命中率最高。

注意一个工程细节：`route_requests` 在每个 rank 本地都跑一遍，为保证各 rank 结果一致，`KVCacheAwareADPRouter` 在打平分时用 `random.Random(req_id).shuffle(...)`——以 `req_id` 为种子，保证所有 rank 产生**相同的随机排列**（[adp_router.py:744-746](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/adp_router.py#L744-L746)）。注释里点明了：若各 rank 随机不一致，会让分布式协议死锁。

#### 4.4.4 代码实践

**实践目标**：理解「allgather 共识 + 确定性路由」为何能省一次广播。

**操作步骤**：

1. 阅读 [py_executor.py:4999-5063](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4999-L5063)，数一数这段代码里有几次集合通信（`tp_allgather`）。
2. 在 [adp_router.py:370-417](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/adp_router.py#L370-L417) `DefaultADPRouter.route_requests` 里确认：函数内部**没有任何** `dist.broadcast` 之类的通信调用。

**需要观察的现象 / 预期结果**：每步只有一次 allgather（在 `gather_all_rank_states` 里），路由本身零通信。各 rank 因为输入相同、`route_requests` 是纯函数，算出的 `dict[rank -> requests]` 完全一致，于是每个 rank 各取自己那份即可。

**预期结果**：能用一句话解释「为什么路由结果无需广播也能保持各 rank 一致」。本实践为源码阅读型，无需 GPU。

#### 4.4.5 小练习与答案

**练习 1**：`DefaultADPRouter` 用最小堆以 `(num_tokens, num_requests)` 为键，为什么 `num_tokens` 排在 `num_requests` 前面？

**参考答案**：token 数才是真正的「计算/显存负载」度量——一个 prefill 长 prompt 的请求比三个短 decode 请求可能还重。先比 token 数能更准确地均衡实际负担；`num_requests` 只在 token 数相同时作为次级平局。

**练习 2**：为什么 `KVCacheAwareADPRouter` 需要 `enable_block_reuse=True`（见工厂 [adp_router.py:207-213](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/scheduler/adp_router.py#L207-L213)）？

**参考答案**：它的路由打分依赖「前缀命中长度」`match_len`，而这个值要从 KV cache 的 radix 树（前缀树）里查。只有开启了 block reuse（块复用，u7-l1），radix 树才会记录可复用的前缀块，`probe_prefix_match_length` 才能返回非零命中。没有 block reuse，所有 rank 的 `match_len` 恒为 0，缓存感知路由就退化成普通负载均衡，没有意义。

## 5. 综合实践

把本讲四个模块串成一个完整的「请求一生」追踪任务。

**任务**：选定一个普通（非分离式、非数据并行）请求，画出它从「被 `_fetch_new_requests` 拉进」到「`finish_by` 结束」的**完整时序**，标注每一步涉及的状态、队列操作与槽位操作。

**操作步骤**：

1. 在 `py_executor.py` 中按顺序定位以下节点，画出调用时序图：
   - `_fetch_and_enqueue_requests` → 请求进入 `waiting_queue`（4.2）。
   - `_pop_from_waiting_queue` → 按容量从队列取出（此时若取不出会被 `prepend` 回塞）。
   - 请求进入 `active_requests`，**初始状态为 `CONTEXT_INIT`**（在 `CONTEXT_INIT` 的可调度窗口内，被调度器选中）。
   - `SeqSlotManager.prepare_resources` → `SlotManager.add_slot` 分配槽位（4.3）。
   - 前向 → `_update_request_states_tp`：prefill 分块推进；`context_remaining_length==0` 时状态迁到 `GENERATION_IN_PROGRESS`（4.1）。
   - 多步 decode 后，`will_complete_next_iteration()` 为真 → `_update_generation_requests_that_will_complete_next_iteration` 把状态迁到 `GENERATION_TO_COMPLETE`。
   - 最后一步 → `finish_by`（或响应处理）把状态设为 `GENERATION_COMPLETE`。
   - `SeqSlotManager.free_resources` → `SlotManager.remove_slot` 回收槽位。
2. 在时序图上用三种颜色/标记分别标出：**状态迁移点**、**队列操作点**、**槽位操作点**。
3. 思考：如果此时开启了 Attention DP（4.4），上述时序里会多出哪两步？（答案：`gather_all_rank_states` 的 allgather、`route_requests` 的本地分发。）

**预期结果**：得到一张清晰的「请求生命周期时序图」，能指认每一个状态值、每一次 `add_slot`/`remove_slot`、每一次入队/出队发生在代码的哪一行。这正是把「状态机 + 等待队列 + 槽位池 + 路由」四件事融会贯通的检验标准。

> 若需在真实运行中观察这些状态变化，需要带 GPU 的环境并在 `py_executor.py` 关键迁移点临时加日志（属「待本地验证」的进阶玩法）；纯源码阅读即可完成本任务的主体。

## 6. 本讲小结

- **请求是一台状态机**：`LlmRequest` 在 `CONTEXT_INIT → GENERATION_IN_PROGRESS → GENERATION_TO_COMPLETE → GENERATION_COMPLETE` 之间迁移，状态值定义在 C++ 的 `LlmRequestState` 枚举里。
- **数值即语义**：C++ 枚举的数值被刻意排过序，让「可调度状态」恰好构成连续区间 `[10, 14)`，于是「能否调度」退化成两次整数比较——新增状态时必须谨慎安排数值。
- **状态由 PyExecutor 集中搬动**：迁移发生在单步循环的固定节点（`_update_request_states_tp` 等），而非请求自己改；分离式服务靠额外的 `DISAGG_*` 状态表达「等 KV / 传 KV / KV 到齐」。
- **等待队列是并发缓冲层**：`WaitingQueue` 用 FCFS 或「负优先级最小堆 + 双计数器」实现，`prepend_*` 保证调度失败的请求不丢排队位置；`PyExecutor` 每步按「剩余名额」从队列取请求。
- **序列槽是对象池式资源**：`SeqSlotManager` 作为 `BaseResourceManager`，用 `SlotManager` 的「空闲集合 + 映射」管理身份编号，并发满时抛 `NoFreeSlotsError`；`prepare/free` 必须成对。
- **ADP Router 用共识省通信**：数据并行下，各 rank allgather 状态后本地跑同一个确定性 `route_requests`，结果天然一致、无需广播；三种策略分别是负载均衡、KV 缓存感知、对话亲和。

## 7. 下一步学习建议

- **Decoder 与 Sampling**：请求状态迁到生成阶段后，「下一个 token 是什么」由采样决定。下一讲 **u8-l3 Decoder 与 Sampling** 会接上这段，讲 `sampler`、`SamplingParams` 与 guided decoding 如何在生成阶段工作。
- **回看 KV Cache**：本讲反复提到「槽位与 KV cache 是两类不同资源」。若对资源生命周期还想加深，可重读 **u7-l1 / u7-l2**，对比 `KVCacheManager` 与 `SeqSlotManager` 在 `get_needed_resource_to_completion` 上的差异（一个随长度增长、一个恒为 1）。
- **进阶：分离式服务**：本讲出现的 `DISAGG_*` 状态只是入口。若想完整理解「KV 传输完成后状态如何从 `DISAGG_GENERATION_INIT` 迁进可调度窗口」，建议之后阅读 **u11-l2 分离式服务** 与 `py_executor.py` 中 disagg 相关的迁移代码。
- **源码延伸**：想挑战的读者，可阅读 [py_executor.py:5677-5682](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L5677-L5682) `_count_schedulable_active_requests`，看它如何把「状态窗口」思想复用到 ADP 的可调度请求计数上——这是本讲状态机思想在另一个场景的再次体现。
