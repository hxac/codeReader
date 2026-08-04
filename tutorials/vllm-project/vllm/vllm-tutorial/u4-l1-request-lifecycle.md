# 请求对象与生命周期

## 1. 本讲目标

本讲沿着「请求如何流动」这条主线进入 V1 调度内部。学完本讲，你应当能够：

- 说出 V1 内部 `Request` 对象承载了哪些核心字段，尤其是 `prompt_token_ids`、`_all_token_ids`、`num_computed_tokens` 三者各自记录什么。
- 画出 `RequestStatus` 的完整状态机，并解释「为什么只要状态值大于 `PREEMPTED` 就算完成」。
- 用自己的话描述一个请求从 `add_request` 进入调度器、经过 `WAITING → RUNNING`、可能被抢占成 `PREEMPTED`、最终落到 `FINISHED_*` 的全过程。
- 理解 `num_computed_tokens` 在 V1 异步调度下「乐观计数」的含义，以及它如何与 KV 缓存分配耦合。
- 说出 `session_id` 这类请求级标识字段的含义，以及它如何从前端经 `EngineCoreRequest` 透传到内部的 `Request`。

本讲是 u4 单元（调度与 KV 缓存管理）的起点，后续讲义（Scheduler、连续批处理、PagedAttention）都建立在「请求对象长什么样、状态如何流转」的认知之上。

## 2. 前置知识

在开始前，你需要先建立以下直觉（这些概念在 u1、u3 已建立，这里只做最小回顾）：

- **请求（request）与序列（sequence）**：在大模型推理里，一次生成任务本质上是一条 token 序列。它有一段已经给定的输入 token（prompt），以及一段要模型逐个生成的输出 token。V1 把「一次生成任务」抽象成一个 `Request` 对象，而不是把输入、输出拆成多条独立序列。这是 V1 相对旧架构的一个重要简化。
- **prefill 与 decode**：处理 prompt 的首计算叫 prefill（一次性吃掉一整段输入 token，算它们的 KV）；之后每生成一个新 token 叫 decode（用上一步的 KV + 新 token 算下一步）。一个请求的生命周期里，先 prefill，再不断 decode，直到结束。
- **KV 缓存**：每个 token 在 attention 里都会算出一份 Key/Value 向量，缓存起来供后续 token 复用。`num_computed_tokens` 这个字段记录的就是「当前请求有多少个 token 的 KV 已经写进缓存了」。
- **EngineCore 进程**：u3-l1 讲过，EngineCore 是 V1 的调度与执行编排核心，跑着一个 busy loop。`Request` 对象就住在 EngineCore 进程内，由它调度、推进、最终回收。
- **抢占（preemption）**：当显存不够时，调度器会把某些正在跑的请求「踢出去」，腾出显存给别的请求，被踢的请求以后再重新 prefill。这就是抢占。

如果你对 V1 的多进程架构（API Server / EngineCore / GPU Worker）还不熟，建议先读 u3-l1 再回来。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `vllm/v1/request.py` | **本讲核心**。定义 `Request` 类（请求对象）和 `RequestStatus` 枚举（状态机），以及 `StreamingUpdate`（流式输入更新包）。 |
| `vllm/v1/engine/__init__.py` | 定义跨进程传输用的 `EngineCoreRequest`（请求的序列化形式）、`FinishReason`（完成原因）、`EngineCoreEvent` / `EngineCoreEventType`（请求事件，用于统计）。`EngineCoreRequest` 也携带 `session_id` 等请求级字段。 |
| `vllm/v1/core/sched/scheduler.py` | 调度器。本讲只看它「如何驱动 `Request.status` 与 `num_computed_tokens` 变化」的部分，不深入调度策略本身。 |
| `vllm/v1/core/sched/request_queue.py` | 请求队列的两种实现（FCFS / 优先级），依赖 `Request.__lt__` 定义排序。 |
| `vllm/v1/engine/core.py` | EngineCore 的 `add_request` 与请求转换入口。 |
| `vllm/sequence.py` | **重要说明**：你可能在旧资料里看到请求/序列相关的类在 `vllm/sequence.py`。但在 V1 里这个文件已大幅瘦身，目前只剩一个与流水线并行相关的 `IntermediateTensors` 数据类，不再承载 `Sequence` 或 `Request`。本讲的 `Request` 全部来自 `vllm/v1/request.py`。 |
| `tests/v1/test_request.py` | 针对 `RequestStatus` 的最小测试，以及验证 `session_id` 从 `EngineCoreRequest` 透传到 `Request` 的测试，本讲代码实践会用到它。 |

> 一个容易混淆的点：`vllm/sequence.py` 名字很像「序列的家」，但 V1 的请求对象并不住在那里。记住 **V1 的请求 = `vllm/v1/request.py` 里的 `Request`**。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：4.1 `Request` 对象与核心字段、4.2 `RequestStatus` 状态机、4.3 请求生命周期（状态流转全流程）、4.4 `num_computed_tokens` 与进度追踪。其中 4.1 会专门讲到 `session_id` 等请求级字段。

### 4.1 Request 对象与核心字段

#### 4.1.1 概念说明

`Request` 是 V1 对「一次生成任务」的统一抽象。它把这件事需要用到的所有信息打包在一个对象里：

- **输入**：prompt 的 token id 序列（`prompt_token_ids`）、采样参数（`sampling_params`）、可选的多模态特征、LoRA 请求等。
- **进度**：已经算了多少 token（`num_computed_tokens`）、已经生成了哪些输出 token（`_output_token_ids`）。
- **身份与调度**：请求 id、到达时间、优先级、当前状态（`status`），以及可选的会话标识 `session_id`。
- **KV 缓存**：每个 block 的哈希（`block_hashes`），用于前缀缓存命中判定（这一细节留到 u4-l5 讲）。

为什么要把这些都放一个对象里？因为在 V1 的多进程架构下，调度（EngineCore）和执行（GPU Worker）是分离的，调度器需要随时知道每个请求「跑到哪了、要不要继续喂给 GPU、能不能被抢占」。`Request` 就是这个共享的事实来源（single source of truth）。

其中有部分字段属于「请求级标识」，`session_id` 就是典型代表。它是前端（API Server / 客户端）为某个会话打上的标签：引擎本身**不解释它的语义**，但会把原样携带下去——供上层做会话亲和（session affinity）、计费归类或可观测性统计。这种「引擎只搬运、不消费」的字段，正是下面要讲的 `EngineCoreRequest → Request` 透传的典型例子。

注意 `Request` 和跨进程传输用的 `EngineCoreRequest` 是两个东西：

- `EngineCoreRequest`（`msgspec.Struct`，可序列化）是请求从前端进程经 ZMQ 传到 EngineCore 进程时的「信封」，`session_id` 等请求级字段就放在信封里。
- `Request`（普通 Python 类，不可序列化、带可变状态）是 EngineCore 内部真正使用、并不断更新状态的对象，它也持有同一个 `session_id`。

两者通过 `Request.from_engine_core_request` 这个类方法转换。

#### 4.1.2 核心流程

一个 `Request` 被构造出来时，核心字段是这样初始化的：

1. 记录身份：`request_id`、`arrival_time`（没传就用当前时间）、`priority`、可选的 `session_id`。
2. 挂载参数：`sampling_params`（生成模型）或 `pooling_params`（embedding/池化模型），二者不能同时为空。
3. 根据 `sampling_params` 推导 `max_tokens`（最多生成多少 token）。
4. 设置输入：`prompt_token_ids`、`num_prompt_tokens`（输入长度）。
5. 初始化输出容器：`_output_token_ids = []`，并把输入 token 复制成 `_all_token_ids`（全量 token = prompt + output）。
6. 把进度清零：`num_computed_tokens = 0`。
7. 设定初始状态：`status = RequestStatus.WAITING`。

关键数据结构关系：

```
prompt_token_ids        : [t0, t1, t2, t3]              # 输入（只读）
_output_token_ids       : [o0, o1]                       # 输出（不断 append）
_all_token_ids          : [t0, t1, t2, t3, o0, o1]       # 全量 = prompt + output
num_prompt_tokens       : 4                              # len(prompt)
num_tokens (属性)       : 6  = len(_all_token_ids)
num_output_tokens (属性): 2  = len(_output_token_ids)
num_computed_tokens     : 3  # 已写入 KV 缓存的 token 数（本例正在 prefill 中）
session_id              : "session-1"  # 可选的会话标识，引擎原样携带
```

#### 4.1.3 源码精读

先看 `Request.__init__` 的构造签名与几个最关键的字段初始化：

[vllm/v1/request.py:60-81](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/request.py#L60-L81) —— `Request` 类与构造函数签名。注意参数既支持 `sampling_params` 也支持 `pooling_params`，分别对应生成模型与池化（embedding）模型；第 77 行的 `session_id: str | None = None` 就是新增的会话标识参数。

[vllm/v1/request.py:96-98](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/request.py#L96-L98) —— 设置到达时间，并把初始状态固定为 `WAITING`。**任何新请求一开始都是 WAITING**，这是状态机的起点。

[vllm/v1/request.py:107-130](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/request.py#L107-L130) —— 推导 `max_tokens`：池化模型恒为 1（只算一次池化输出），生成模型取 `sampling_params.max_tokens`。最后一句 `raise ValueError` 保证两者至少有一个。

[vllm/v1/request.py:132-149](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/request.py#L132-L149) —— 核心：`prompt_token_ids` 记录输入；`_output_token_ids` 从空列表开始；`_all_token_ids` 通过把 `prompt_token_ids` 拷贝一份来初始化（没有 token id 时则用占位 `0` 填充至 `num_prompt_tokens`）。这意味着**全量 token 一开始就等于 prompt**。

[vllm/v1/request.py:173-174](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/request.py#L173-L174) —— `num_computed_tokens = 0`：进度计数器从零开始，表示尚未为任何 token 计算并写入 KV。

再看输出如何追加，以及只读视图的设计：

[vllm/v1/request.py:252-263](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/request.py#L252-L263) —— `append_output_token_ids`：每生成新 token，同时往 `_output_token_ids` 和 `_all_token_ids` 追加，并刷新 block 哈希。注意它同时改两个列表，所以外部不应绕过它直接 append。

[vllm/v1/request.py:180-184](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/request.py#L180-L184) —— `output_token_ids` 和 `all_token_ids` 是 `_output_token_ids` / `_all_token_ids` 的 **`ConstantList` 只读视图**。注释解释：防止外部直接 `append`，因为那会绕过 `_all_token_ids` 的同步更新。

接着看请求级标识字段（`trace_headers` 与 `session_id`）：

[vllm/v1/request.py:185-187](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/request.py#L185-L187) —— `trace_headers` 与 `session_id` 都被原样存到实例上。`session_id` 与 `trace_headers` 一样属于「引擎只搬运、不消费」的请求级元数据：它不参与调度或采样决策，只是跟着请求对象一起走完整个生命周期。

`session_id` 的「搬运」要跨过进程边界。它的起点在前端构造的 `EngineCoreRequest` 信封里：

[vllm/v1/engine/__init__.py:148](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/__init__.py#L148) —— `EngineCoreRequest`（一个 `msgspec.Struct`）同样声明了 `session_id: str | None = None`。它随请求一起经 ZMQ 序列化传到 EngineCore 进程。

[vllm/v1/request.py:225-250](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/request.py#L225-L250) —— `from_engine_core_request`：在进程边界把信封拆包成内部 `Request`。第 246 行 `session_id=request.session_id` 正是把信封里的 `session_id` 透传到 `Request` 上——这一行是 `session_id` 从前端落到运行时对象的唯一通道。

最后是与身份相关的属性和排序：

[vllm/v1/request.py:274-284](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/request.py#L274-L284) —— `num_tokens` = 全量 token 数；`num_output_tokens` = 已生成输出数。它们都是基于内部列表长度的只读属性。

[vllm/v1/request.py:337-348](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/request.py#L337-L348) —— `__lt__`：优先级队列排序依据。顺序为 **优先级（小值优先）→ 到达时间（早的优先）→ request_id → 对象 id**。注意 `session_id` 不参与排序，它只是被携带的标签。这个方法会被优先级队列直接用来 `heappush/heappop`（见 4.3）。

#### 4.1.4 代码实践

这个实践**不需要 GPU、不需要下载模型**——我们直接构造一个 `Request` 对象，观察它的内部字段如何随输出追加而变化，并验证 `session_id` 的携带。

**实践目标**：亲手构造 `Request`，验证 `prompt_token_ids`、`_all_token_ids`、`num_computed_tokens` 三者的关系，并确认 `session_id` 被原样保存。

**操作步骤**（在已按 u1-l2 装好 vLLM 的 `.venv` 里执行）：

```python
# 文件名：play_request.py（示例代码，非项目原有文件）
from vllm.v1.request import Request
from vllm.sampling_params import SamplingParams

sp = SamplingParams(max_tokens=10, temperature=0.0)
req = Request(
    request_id="req-1",
    prompt_token_ids=[10, 11, 12, 13],   # 假装这是一段 prompt
    sampling_params=sp,
    pooling_params=None,
    session_id="session-1",              # 可选的会话标识
)

print("status            :", req.status)               # WAITING
print("num_prompt_tokens :", req.num_prompt_tokens)    # 4
print("num_computed_tokens:", req.num_computed_tokens) # 0
print("all_token_ids     :", list(req.all_token_ids))  # [10, 11, 12, 13]
print("output_token_ids  :", list(req.output_token_ids))  # []
print("num_tokens        :", req.num_tokens)           # 4
print("num_output_tokens :", req.num_output_tokens)    # 0
print("session_id        :", req.session_id)           # session-1
print("is_finished       :", req.is_finished())        # False

# 模拟模型生成了两个 token
req.append_output_token_ids([100, 101])
print("--- 追加输出后 ---")
print("all_token_ids     :", list(req.all_token_ids))  # [10,11,12,13,100,101]
print("num_tokens        :", req.num_tokens)           # 6
print("num_output_tokens :", req.num_output_tokens)    # 2
```

**需要观察的现象**：
1. 构造后 `status` 是 `WAITING`、`num_computed_tokens` 是 0。
2. `_all_token_ids` 一开始就等于 `prompt_token_ids` 的拷贝。
3. 调用 `append_output_token_ids` 后，`all_token_ids` 和 `output_token_ids` 同步增长，而 `num_computed_tokens` **不变**（它由调度器在 `schedule()` 里推进，而不是在这里）。
4. `session_id` 等于构造时传入的字符串——它只是被存下来，不参与任何计算。

**预期结果**：如注释所示。

**待本地验证**：如果你尝试 `req.output_token_ids.append(999)`，应当报错——因为它是 `ConstantList` 只读视图，禁止直接 append。这一点请你实际跑一下确认（作者未在本环境运行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_all_token_ids` 在构造时就要拷贝一份 `prompt_token_ids`，而不是等生成时再拼？如果 `prompt_token_ids` 传的是 `None`（纯 embedding 输入）会怎样？

**参考答案**：因为 KV 缓存、block 哈希、`num_tokens` 等都按「全量 token 的位置」来索引，prompt 和 output 在位置空间上是连续的，统一用一个 `_all_token_ids` 能让所有「第 i 个 token」的查询 O(1)。当 `prompt_token_ids=None` 时（见 4.1.3 第 132-149 行的 `else` 分支），构造会用 `[0] * num_prompt_tokens` 填充占位，长度对齐到 `num_prompt_tokens`（由 `prompt_embeds` 推导）。

**练习 2**：`Request` 和 `EngineCoreRequest` 为什么不合并成一个类？以 `session_id` 为例说明它在两者之间是怎么流动的。

**参考答案**：`EngineCoreRequest` 是 `msgspec.Struct`、`gc=False`、`omit_defaults`，设计目的是**跨进程序列化传输**（ZMQ），要轻量、可编码；而 `Request` 是带大量可变状态（`num_computed_tokens`、`status`、`block_hashes`…）的运行时对象，**不该也不需要被序列化**。`session_id` 同时声明在两者上（`EngineCoreRequest` 在 `vllm/v1/engine/__init__.py:148`，`Request` 在 `vllm/v1/request.py:77`），由 `from_engine_core_request`（`vllm/v1/request.py:246`）在进程边界做一次拷贝完成透传。两者职责清晰，拆分让「序列化信封」与「运行时状态」互不污染。`tests/v1/test_request.py` 里的 `test_request_copies_session_id_from_engine_core_request` 正是对这条透传链的最小回归测试。

---

### 4.2 RequestStatus 状态机

#### 4.2.1 概念说明

`RequestStatus` 是请求的「生命周期阶段标签」。它是一个 `IntEnum`（整数枚举），每个状态对应一个整数。设计上有一个非常巧妙之处：**枚举值的排列顺序本身就是「是否完成」的判定依据**。

状态分三大类：

- **等待类（WAITING 系列）**：请求已入队但还没轮到它跑。包括普通 `WAITING`，以及几种「在等某个外部条件」的变体：等结构化输出的语法编译（`WAITING_FOR_STRUCTURED_OUTPUT_GRAMMAR`）、等远端 KV 传过来（`WAITING_FOR_REMOTE_KVS`）、等流式输入的下一段（`WAITING_FOR_STREAMING_REQ`）。
- **运行中（RUNNING）**：请求在本 step 被调度、正参与计算。
- **被抢占（PREEMPTED）**：之前在跑，因显存不足被踢出，等显存空出来再重新 prefill。
- **完成类（FINISHED_* 系列）**：请求已经结束，不会再被计算。细分停止原因（见下表）。

#### 4.2.2 核心流程

判定「是否完成」的核心是这一行逻辑：

\[ \text{is\_finished}(s) \iff s > \text{PREEMPTED} \]

也就是说，`PREEMPTED` 是一条分水岭——**枚举值排在它之后的统统算「完成」**。这样调度器只要一次整数比较就能判断一个请求还要不要再管它，非常高效。

各种完成状态对应外部可见的 `FinishReason`：

| `RequestStatus` | `FinishReason` | 含义 |
|---|---|---|
| `FINISHED_STOPPED` | `STOP`（"stop"） | 命中了 stop 字符串或 EOS |
| `FINISHED_LENGTH_CAPPED` | `LENGTH`（"length"） | 达到 `max_tokens` 或 `max_model_len` |
| `FINISHED_ABORTED` | `ABORT`（"abort"） | 被客户端主动 abort |
| `FINISHED_IGNORED` | `LENGTH`（"length"） | prompt 超过模型长度上限，被忽略（对齐 OpenAI，reason 也算 length） |
| `FINISHED_ERROR` | `ERROR`（"error"） | 请求级内部错误（如 KV 加载失败） |
| `FINISHED_REPETITION` | `REPETITION`（"repetition"） | 检测到重复 token（幻觉保护） |

注意 `WAITING_FOR_STREAMING_REQ` 也映射到 `STOP`——流式输入会话自然结束时按 stop 处理。

`FinishReason` 本身也是个 `IntEnum`，`__str__` 把整数映射回字符串（`"stop"`/`"length"`/…），这些字符串最终出现在 `RequestOutput.finish_reason` 里，属于对外 API 的一部分。

#### 4.2.3 源码精读

[vllm/v1/request.py:351-367](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/request.py#L351-L367) —— `RequestStatus` 枚举定义。重点看注释 `# Note: anything after PREEMPTED will be considered as a finished status.`，这正是 `is_finished` 实现的依据。`enum.auto()` 从 1 开始依次递增，所以顺序就是值的大小关系。

[vllm/v1/request.py:372-378](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/request.py#L372-L378) —— `is_finished` 与 `get_finished_reason` 两个静态方法。`is_finished` 仅一句 `status > RequestStatus.PREEMPTED`。

[vllm/v1/request.py:385-393](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/request.py#L385-L393) —— `_FINISHED_REASON_MAP`：完成状态 → `FinishReason` 的映射表。

[vllm/v1/engine/__init__.py:43-65](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/__init__.py#L43-L65) —— `FinishReason` 定义。它是 `IntEnum`（紧凑序列化），并在 docstring 里解释了每种原因；`__str__` 通过 `FINISH_REASON_STRINGS` 把整数转成对外的字符串。

[vllm/v1/request.py:307-311](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/request.py#L307-L311) —— `Request.is_finished()` 与 `get_finished_reason()`，都委托给上面的静态方法，是调度器判断请求归宿的入口。

#### 4.2.4 代码实践

`tests/v1/test_request.py` 就是专门测请求对象的最小用例，直接跑它即可。它现在包含两个测试函数。

**实践目标**：验证 `RequestStatus` 的字符串表示与 `is_finished` 的分水岭行为，并确认 `session_id` 的透传。

**操作步骤**：

```bash
.venv/bin/python -m pytest tests/v1/test_request.py -v
```

**需要观察的现象**：两个测试全部通过：
- `test_request_status_fmt_str`：5 个断言，验证 `f"{RequestStatus.WAITING}" == "WAITING"` 等。这依赖枚举的 `__str__` 返回 `self.name`（见 `vllm/v1/request.py:369-370`）。
- `test_request_copies_session_id_from_engine_core_request`：构造一个带 `session_id="session-1"` 的 `EngineCoreRequest`，调用 `Request.from_engine_core_request(...)` 后断言 `request.session_id == "session-1"`——这正是 4.1 讲的「信封 → 运行时对象」透传。

如果想进一步验证 `is_finished` 的分水岭，可写一小段（示例代码）：

```python
from vllm.v1.request import RequestStatus as S
for st in [S.WAITING, S.RUNNING, S.PREEMPTED, S.FINISHED_STOPPED, S.FINISHED_ABORTED]:
    print(f"{st.name:35s} value={st.value:2d} is_finished={S.is_finished(st)}")
```

**预期结果**：`PREEMPTED` 及之前都为 `False`，`FINISHED_*` 全为 `True`。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `PREEMPTED` 的枚举顺序错放到 `FINISHED_*` 之后，会发生什么？

**参考答案**：`is_finished` 用 `status > PREEMPTED` 判定。若 `PREEMPTED` 排到了最后，那么真正的完成状态值都会「小于等于」它，`is_finished` 对所有完成状态都返回 `False`，调度器会永远不释放这些请求，造成显存泄漏和死循环。这正说明「枚举值顺序」在这里是隐式契约，不能随意调换。

**练习 2**：`FINISHED_IGNORED` 为什么对应 `LENGTH` 而不是单独一个 reason？

**参考答案**：被忽略的请求是 prompt 长度超过模型 `max_model_len` 上限的请求（见 `_FINISHED_REASON_MAP` 上方注释）。为了和 OpenAI API 行为一致（超长都返回 `length`），所以它的对外 finish reason 也是 `"length"`，而不是另造一个 `"ignored"`。这是「对齐外部事实标准」的设计取舍。

---

### 4.3 请求生命周期（状态流转全流程）

#### 4.3.1 概念说明

把 4.1 的字段和 4.2 的状态串起来，就能讲清一个请求的完整一生。这一节是本讲的核心。一个请求从外部进来，会经历：**入队（WAITING）→ 被选中运行（RUNNING）→ 计算推进 → （可能抢占 PREEMPTED 再回 WAITING）→ 完成（FINISHED_*）→ 释放资源**。

需要强调的是：状态的每一次变迁都发生在**调度器（Scheduler）**里，而不是 `Request` 自己驱动。`Request` 是被动的数据载体，调度器才是状态机的主人。`session_id`、`trace_headers` 这些被搬运的字段会全程伴随请求对象，直到它被释放。

#### 4.3.2 核心流程

下面是一个请求从生到灭的状态流转图（伪状态机）：

```
                  add_request
                       │
                       ▼
        ┌─── WAITING ◄──────────────────────┐
        │         │                          │
        │   schedule() 选中                  │ 重新入队
        │         │                          │ (resume)
        │         ▼                          │
        │     RUNNING ──── 抢占 ───► PREEMPTED┘
        │         │
        │         │ update_from_output
        │         │ 命中停止条件
        │         ▼
        └─►   FINISHED_* (STOP/LENGTH/ABORT/...)
                       │
                       ▼
                 _free_request (释放 KV blocks)
```

关键变迁点：

1. **入队**：`EngineCore.add_request` → `scheduler.add_request` → 进入等待队列，状态 `WAITING`，记一条 `QUEUED` 事件。
2. **选中运行**：`schedule()` 把等待队列里的请求捞出来分配 KV block，状态置 `RUNNING`，记 `SCHEDULED` 事件，并推进 `num_computed_tokens`。
3. **抢占**：`_preempt_request` 把 `RUNNING` 的请求置为 `PREEMPTED`，释放它的 KV block，并把 `num_computed_tokens` 清零（重算）。
4. **恢复**：被抢占的请求重新进等待队列，下一轮 `schedule()` 再次把它置回 `RUNNING`。
5. **完成**：`update_from_output` 检测到停止条件（EOS / max_tokens / stop 串…），把状态置为对应的 `FINISHED_*`，然后 `_free_request` 释放它的 KV block。
6. **主动终止**：客户端 abort 时，`finish_requests(..., FINISHED_ABORTED)` 直接把状态置为 `FINISHED_ABORTED` 并清理。

还有几条「带条件的等待」边（4.2.1 提到的 WAITING_FOR_* 系列），它们是普通 `WAITING` 的变体：请求暂时卡在某个外部条件上，条件满足后再回到正常调度。本讲点到为止，细节分散在 KV 传输、流式输入、结构化输出等专题讲义。

#### 4.3.3 源码精读

**入队路径**：

[vllm/v1/engine/core.py:439-483](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/core.py#L439-L483) —— `EngineCore.add_request`：做参数校验（request_id 必须是 str、pooling task 是否支持、KV 传输参数是否配了 connector），然后调用 `self.scheduler.add_request(request)`（第 479 行）。

[vllm/v1/core/sched/scheduler.py:2213-2235](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/core/sched/scheduler.py#L2213-L2235) —— `Scheduler.add_request`：新请求（`request_id` 不存在）走 `_enqueue_waiting_request` 进等待队列，登记进 `self.requests` 字典，并记一条 `QUEUED` 事件。注意如果是流式可恢复请求还会建一个 `streaming_queue`。

[vllm/v1/engine/core.py:983-991](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/core.py#L983-L991) —— 跨进程请求进来后，用 `Request.from_engine_core_request(...)` 把可序列化的 `EngineCoreRequest` 转成内部 `Request`（`session_id` 就在这一步被透传过来）。这就是「信封拆包」的边界。

**选中运行路径**：

[vllm/v1/core/sched/scheduler.py:1055-1075](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/core/sched/scheduler.py#L1055-L1075) —— 把请求 `append` 进 `self.running`，根据它原来是 `WAITING` 还是 `PREEMPTED` 分类（新调度 vs 恢复），最后第 1074 行 `request.status = RequestStatus.RUNNING`、第 1075 行推进 `num_computed_tokens`。

[vllm/v1/core/sched/scheduler.py:1056-1059](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/core/sched/scheduler.py#L1056-L1059) —— 记 `SCHEDULED` 事件（仅当 `log_stats` 开启）。

**抢占路径**：

[vllm/v1/core/sched/scheduler.py:1287-1309](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/core/sched/scheduler.py#L1287-L1309) —— `_preempt_request`：断言只有 `RUNNING` 能被抢占；释放 KV block 与 encoder cache；第 1293 行置 `PREEMPTED`；第 1294 行 `num_computed_tokens = 0`（抢占后要重算）；第 1309 行 `num_preemptions += 1`。

**完成路径**：

[vllm/v1/core/sched/scheduler.py:1670-1739](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/core/sched/scheduler.py#L1670-L1739) —— `update_from_output` 的开头：遍历每个被调度的请求，处理本步模型产出，准备判断是否停止。

[vllm/v1/core/sched/scheduler.py:1807-1815](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/core/sched/scheduler.py#L1807-L1815) —— 检测停止：调用 `_update_request_with_output` 判断是否命中停止条件；池化模型一有输出就直接 `FINISHED_STOPPED`。

[vllm/v1/core/sched/scheduler.py:1895-1907](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/core/sched/scheduler.py#L1895-L1907) —— 若 `stopped`，先抓取 `finish_reason`（在状态可能被 `_handle_stopped_request` 改写之前），再处理停止并视情况 `_free_request` 释放资源。

**主动终止路径**：

[vllm/v1/core/sched/scheduler.py:2237-2298](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/core/sched/scheduler.py#L2237-L2298) —— `finish_requests`：先断言传入的是完成状态；把请求从 running / waiting 队列里移除；第 2295 行把状态置为 `finished_status`；第 2296 行 `_free_request` 释放 KV block（部分场景会延迟释放）。

**请求队列的组织**：

[vllm/v1/core/sched/request_queue.py:75-128](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/core/sched/request_queue.py#L75-L128) —— `FCFSRequestQueue`：先来先服务，底层就是 `deque`。

[vllm/v1/core/sched/request_queue.py:131-152](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/core/sched/request_queue.py#L131-L152) —— `PriorityRequestQueue`：基于堆，`heappush/heappop` 直接用 `Request.__lt__`（4.1.3 讲过的 priority→arrival_time→request_id 顺序）。注意它的 docstring：优先级队列里「没有队头概念」，`prepend` 实际等价于 `add`。

#### 4.3.4 代码实践

**实践目标**：用源码阅读的方式，亲手把一个请求从 `WAITING` 走到 `FINISHED_*` 的状态序列写出来，并定位每一步在源码的哪一行发生。

**操作步骤**：

1. 打开 `vllm/v1/request.py`，确认构造时 `status = RequestStatus.WAITING`（第 98 行）。
2. 在 `vllm/v1/core/sched/scheduler.py` 中用搜索定位以下三处赋值，记下行号：
   - `request.status = RequestStatus.RUNNING`（应在 1074 行附近）
   - `request.status = RequestStatus.PREEMPTED`（应在 1293 行附近）
   - `request.status = RequestStatus.FINISHED_STOPPED`（应在 1814 行附近，pooling 模型）
3. 写出状态序列。

**需要观察的现象 / 预期结果**：一个正常生成请求（无抢占）的状态序列是：

```
WAITING  --(schedule 选中)-->  RUNNING  --(命中 EOS/max_tokens)-->  FINISHED_STOPPED 或 FINISHED_LENGTH_CAPPED
```

一个遭遇抢占的请求：

```
WAITING --> RUNNING --> PREEMPTED --> WAITING --> RUNNING --> ... --> FINISHED_*
```

**思考题（不运行）**：`_preempt_request` 里为什么要把 `num_computed_tokens` 清零，而不是保留？因为抢占会释放该请求的 KV block，原来算好的 KV 没了，重新运行时必须从 prompt 重新 prefill，所以进度归零。（见 4.4 进一步解释。）

#### 4.3.5 小练习与答案

**练习 1**：一个请求能否从 `FINISHED_STOPPED` 回到 `RUNNING`？为什么？

**参考答案**：正常情况下不能。`finish_requests` 会把它从所有队列移除并 `_free_request`，调度器不再持有它的引用，自然不会再调度。唯一的「回环」是流式输入会话（streaming session）：`_handle_stopped_request` 在流式场景下会把状态从完成**重置回 `WAITING`**，等待下一段输入（见 `scheduler.py:1898-1900` 的注释「may reset the status to WAITING for streaming requests that continue」）。但即便如此，它也是从「即将完成」被拉回 `WAITING`，而不是从真正的终态回来。

**练习 2**：`add_request` 里如果传入了一个已存在的 `request_id` 会怎样？

**参考答案**：不会报错也不会覆盖，而是被当作流式输入的下一段处理（见 `scheduler.py:2214-2226`）：若已有请求处于 `WAITING_FOR_STREAMING_REQ`，则立刻推进它；否则把 `StreamingUpdate` 追加进已有请求的 `streaming_queue`。这是流式输入复用同一 `request_id` 的机制。

---

### 4.4 num_computed_tokens 与进度追踪

#### 4.4.1 概念说明

`num_computed_tokens` 是请求对象里**最容易被低估、却最关键**的字段之一。它回答一个问题：「这个请求目前有多少个 token 的 KV 已经算好并写进缓存了？」

- 对 prefill：它从 0 走到 `num_prompt_tokens`，表示输入被一段段消化。
- 对 decode：每生成一个 token，它 +1。
- 对抢占：被重置为 0，因为 KV block 被释放了。

理解它要抓住 V1 的一个设计特征：**异步调度（async scheduling）**。调度器在 `schedule()` 阶段就会「乐观地」把 `num_computed_tokens` 往前推，而不是等到 GPU 真的算完。这是因为调度器要和 GPU 计算重叠（u3-l1 讲过的 CPU 调度与 GPU 计算重叠），它假设这一步安排的 token 一定会被算出来。

为了配合这种乐观假设，`Request` 还有几个相关计数器：

- `num_in_flight_tokens`：已经安排计算、但结果还没回来的 token 数。
- `num_output_placeholders` / `num_stale_output_tokens`：异步调度与抢占时用来处理「在飞行的输出」的簿记量。

这些「飞行中」计数器是异步调度的产物，本讲只建立认知，细节属于 u4-l2/u4-l3 调度器专题。

#### 4.4.2 核心流程

`num_computed_tokens` 的典型生命周期：

```
构造:        num_computed_tokens = 0
schedule():  num_computed_tokens = num_computed_tokens + num_new_tokens   # 乐观推进
             (并设置 num_in_flight_tokens += num_new_tokens)
GPU 出结果:   update_from_output 里 num_in_flight_tokens -= num_scheduled   # 兑现
preempt():   num_computed_tokens = 0                                          # 清零重算
```

进度与 KV 缓存分配的关系：调度器在 `schedule()` 里用 `num_computed_tokens` 决定「这个请求从第几个 token 开始算、需要新分配几个 KV block」。当 `num_computed_tokens == num_tokens`（全量都算完了）且本步有新输出时，就是一次纯 decode。

前缀缓存（u4-l5）也会影响起点：如果请求的 prompt 前缀命中了缓存，`num_computed_tokens` 会直接从命中位置开始，跳过已缓存部分的计算。

#### 4.4.3 源码精读

[vllm/v1/request.py:160-163](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/request.py#L160-L163) —— `num_in_flight_tokens` 等计数器的注释，明确说明「异步调度下，`num_computed_tokens` 是乐观计数（counts them optimistically）」。这是理解本字段的最关键注释。

[vllm/v1/request.py:173-174](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/request.py#L173-L174) —— `spec_token_ids`（推测解码的草稿 token）与 `num_computed_tokens = 0` 的初始化。

[vllm/v1/core/sched/scheduler.py:828-832](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/core/sched/scheduler.py#L828-L832) —— 在 `schedule()` 里计算本步的 `num_computed_tokens`（本地已缓存 + 外部 KV 已传来的），并断言它不超过请求总 token 数。

[vllm/v1/core/sched/scheduler.py:1075](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/core/sched/scheduler.py#L1075) —— `request.num_computed_tokens = num_computed_tokens`：在选中运行的同时**乐观推进**进度。

[vllm/v1/core/sched/scheduler.py:1738-1739](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/core/sched/scheduler.py#L1738-L1739) —— `update_from_output` 里 `request.num_in_flight_tokens -= num_tokens_scheduled`：GPU 结果回来后，把「在飞行」计数兑现。

[vllm/v1/core/sched/scheduler.py:1294](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/core/sched/scheduler.py#L1294) —— 抢占时 `request.num_computed_tokens = 0`：进度归零，因为 KV 没了，要重新 prefill。

#### 4.4.4 代码实践

**实践目标**：通过「断点阅读」理解 `num_computed_tokens` 在一步里的推进过程。

**操作步骤**（源码阅读型实践，无需运行）：

1. 在 `scheduler.py` 的 `schedule()` 中找到对等待队列请求计算 `num_new_tokens` 的逻辑（大致在第 874 行之后），理解 `num_new_tokens` 是「本步要新算多少 token」。
2. 跟踪 `num_new_tokens` 如何累积到 `num_computed_tokens`，并在第 1075 行写回 `request.num_computed_tokens`。
3. 想象一个 `prompt_token_ids` 长度 100、`enable_chunked_prefill` 开启的场景：第一次 schedule 可能只算 64 个 token，`num_computed_tokens` 从 0 → 64；第二次 64 → 100（prefill 完成）；之后每次 decode +1。

**需要观察的现象 / 预期结果**：你能用自己的话讲清「一个长 prefill 是如何被切成多步、每步推进 `num_computed_tokens` 直到 prefill 完成的」。这正是 u4-l3（连续批处理与分块预填充）要展开的内容——本讲先建立这个进度模型。

**待本地验证**：若你想实测，可在 `scheduler.py:1075` 临时加一行 `logger.info` 打印 `(request.request_id, num_computed_tokens, request.num_tokens)`，跑一次离线推理（参考 u2-l1 的 `LLM.generate`），观察日志中一个请求的 `num_computed_tokens` 如何从 0 增长。修改源码仅为调试，请勿提交。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `num_computed_tokens` 要在 `schedule()`（CPU 侧）就推进，而不是等 `update_from_output`（结果回来后）才推进？

**参考答案**：因为 V1 是异步调度，调度器的下一步决策（哪些请求继续跑、要不要新拉请求）必须立刻基于「计划中的进度」做，而不能等 GPU 算完。若等结果回来才推进，CPU 调度就会被 GPU 拖住，无法重叠，吞吐下降。代价是要引入 `num_in_flight_tokens` 等簿记量来处理「安排了但还没兑现」的 token，并在异常（如抢占）时正确回滚。

**练习 2**：如果一个请求 `num_computed_tokens == num_tokens` 恒成立，它会一直占着显存吗？

**参考答案**：不会。「`num_computed_tokens == num_tokens`」只说明当前已算的部分到顶了，但每生成一个新 token，`num_tokens`（= `len(_all_token_ids)`）会随 `append_output_token_ids` 增长，于是又有了新的待算 token。当输出达到 `max_tokens` 或命中停止条件，请求进入 `FINISHED_*`，`_free_request` 才会真正释放它的 KV block，显存才回收。所以「进度到顶」≠「请求完成」。

---

## 5. 综合实践

把本讲四个模块串起来，做一个**纯 CPU、可独立运行**的「迷你请求生命周期模拟器」。目标是让你把 `Request` 的字段变化与 `RequestStatus` 的状态流转亲手走一遍。

**任务**：写一个脚本 `mini_request_lifecycle.py`（示例代码，非项目原有文件），完成以下事情：

1. 用 `SamplingParams(max_tokens=5)` 和一段 `prompt_token_ids=[1,2,3,4,5]` 构造一个 `Request`，并传入 `session_id="demo-session"`。
2. 打印初始状态：确认 `status == WAITING`、`num_computed_tokens == 0`、`num_tokens == 5`、`session_id == "demo-session"`。
3. 模拟调度器选中它：手动 `req.status = RequestStatus.RUNNING`，并把 `req.num_computed_tokens` 设为 5（假装 prefill 完成）。
4. 模拟 3 次 decode：每次 `req.append_output_token_ids([x])`，并把 `num_computed_tokens` 自增。打印每次的 `num_tokens`、`num_output_tokens`、`num_computed_tokens`。
5. 模拟命中 `max_tokens`：把状态设为 `RequestStatus.FINISHED_LENGTH_CAPPED`，调用 `req.is_finished()` 与 `req.get_finished_reason()` 验证。

**参考实现骨架**：

```python
# 示例代码，非项目原有文件
from vllm.v1.request import Request, RequestStatus
from vllm.sampling_params import SamplingParams

req = Request(
    request_id="demo",
    prompt_token_ids=[1, 2, 3, 4, 5],
    sampling_params=SamplingParams(max_tokens=5),
    pooling_params=None,
    session_id="demo-session",
)
assert req.status == RequestStatus.WAITING
assert req.num_computed_tokens == 0
assert req.num_tokens == 5
assert req.session_id == "demo-session"

# 模拟被调度运行 + prefill 完成
req.status = RequestStatus.RUNNING
req.num_computed_tokens = req.num_prompt_tokens

# 模拟 3 步 decode
for i, tok in enumerate([100, 101, 102], start=1):
    req.append_output_token_ids(tok)
    req.num_computed_tokens += 1
    print(f"decode#{i}: num_tokens={req.num_tokens} "
          f"output={req.num_output_tokens} computed={req.num_computed_tokens}")

# 模拟达到长度上限完成
req.status = RequestStatus.FINISHED_LENGTH_CAPPED
print("is_finished     :", req.is_finished())            # True
print("finish_reason   :", req.get_finished_reason())    # FinishReason.LENGTH
print("finish_reason str:", req.get_finished_reason())   # str -> "length"
```

**验收标准**：
- 跑通后能看到 `num_tokens` 从 5 增长到 8，`num_output_tokens` 从 0 增长到 3。
- `is_finished()` 最终为 `True`，`get_finished_reason()` 的字符串形式是 `"length"`。
- `session_id` 全程不变，确认它只是被携带、不被计算逻辑改动。

**进阶（可选）**：在步骤 4 中间插入一次「抢占」——把 `status` 设成 `PREEMPTED`、`num_computed_tokens` 清零，再恢复回 `RUNNING`，观察 `is_finished()` 始终为 `False`（因为 `PREEMPTED` 在分水岭之前）。

**注意**：本实践是「直接操纵 `Request` 内部字段来模拟调度器行为」，仅用于学习。真实环境里这些字段都由 `Scheduler` 维护，业务代码不应直接改 `status` / `num_computed_tokens`。

## 6. 本讲小结

- `Request`（`vllm/v1/request.py`）是 V1 对「一次生成任务」的统一抽象，把输入（`prompt_token_ids`）、输出（`_output_token_ids`）、全量 token（`_all_token_ids`）、进度（`num_computed_tokens`）、参数（`sampling_params`）、状态（`status`）都装在一个对象里。
- `RequestStatus` 是一个 `IntEnum` 状态机，「是否完成」靠 `status > PREEMPTED` 这一次整数比较判定——枚举值的排列顺序本身就是隐式契约。
- 一个请求的典型生命周期是 `WAITING → RUNNING → FINISHED_*`，中间可能经历 `RUNNING → PREEMPTED → WAITING` 的抢占循环；每一次状态变迁都发生在调度器里，`Request` 本身是被动的。
- `num_computed_tokens` 在 V1 异步调度下是「乐观计数」：`schedule()` 阶段就推进，靠 `num_in_flight_tokens` 等簿记量在结果回来后兑现，抢占时清零重算。
- `Request` 与可序列化的 `EngineCoreRequest` 通过 `from_engine_core_request` 在进程边界转换；`vllm/sequence.py` 在 V1 已不再承载请求/序列类，只剩 `IntermediateTensors`。
- `Request` 还携带若干「引擎只搬运、不消费」的请求级字段，如 `session_id` 与 `trace_headers`：`session_id` 由前端经 `EngineCoreRequest`（`vllm/v1/engine/__init__.py`）透传，在 `from_engine_core_request` 中落到 `Request.session_id`，全程伴随请求对象但不参与调度或采样决策。
- 优先级队列直接复用 `Request.__lt__`（优先级 → 到达时间 → request_id）排序。

## 7. 下一步学习建议

本讲建立了「请求对象」和「状态流转」的认知，接下来顺着调度主线往下走：

- **u4-l2 Scheduler 调度器核心**：看 `schedule()` 如何决定每步选哪些请求、生成 `SchedulerOutput`，这正是驱动本讲状态变迁的「主人」。
- **u4-l3 连续批处理与请求队列**：深入 `request_queue.py` 的 FCFS / 优先级队列，以及 chunked prefill 如何把长 prefill 切成多步——对应本讲 4.4 里 `num_computed_tokens` 的多步推进。
- **u4-l4 PagedAttention 与 KV 缓存管理**：理解抢占时 `_free_request` 释放的「KV block」到底是什么、按 block 管理如何减少显存碎片。

建议在进入 u4-l2 前，先用本讲第 5 节的综合实践把状态流转亲手跑一遍，这样读调度器源码时会对每一步在改哪个字段有清晰的对应感。
