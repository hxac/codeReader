# Coordinator 协调器

## 1. 本讲目标

本讲继续自外向内追踪请求主链路。上一讲（u2-l3）我们看到内部 `Client` 把请求翻成 `OmniRequest` 后就「提交」了，但并没有说提交给谁、谁来监督请求走完整条管线、谁来把多个阶段的产出拼成一个最终结果。这一讲的主角就是**Coordinator（协调器）**——它在主链路里的位置是：

```
HTTP API → Client → Coordinator → Stage → Scheduler → ModelRunner
                         ▲ 你在这里
```

读完本讲，你应该能够：

1. 说清楚 Coordinator 在整条管线里扮演的「全局请求路由」角色——它注册了哪些端点、把请求送给谁、又从谁那里收结果。
2. 描述一个请求在 Coordinator 眼里的状态机：`pending → running → completed/failed/aborted`。
3. **讲透本讲的核心难点：多终态合并**——当一条管线同时有 `decode`（文本收口）和 `code2wav`（语音收口）两个终态时，Coordinator 如何收齐两份产出、合并成一个结果再交给 Client。
4. 解释 abort（中止）为什么用 PUB/SUB 广播，以及为什么 TP（张量并行）阶段 Coordinator 只和 rank0 通信。

> 本讲承接 u2-l3。请记住：Client 只做「翻译 + 聚合」，真正把请求送进管线、并等待终态回执的，就是 Coordinator。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：Coordinator 不碰 GPU，也不做计算。** 它本质上是一个「邮局」：登记每个阶段的通信地址（端点），把新请求作为包裹投递给入口阶段，再从各个终态阶段收取「完成回执」。它运行在主进程的 asyncio 事件循环里，全部用异步 ZMQ 通信。

**直觉二：一条管线可以有多个「终态（terminal）」。** 回顾 u1-l1：一次生成被拆成多阶段接力，最终可能同时产出「文本」和「音频」两种东西。文本来自 `decode` 阶段，音频来自 `code2wav` 阶段，它们是管线图里的两个终点（没有 `next`）。Coordinator 必须等**所有期望的终态**都完成，才能宣告请求结束。

**直觉三：控制消息用 ZMQ，大张量走 relay。** Coordinator 只搬「命令」和「状态」（谁完成了、谁失败了、中止谁），这些是小消息，用 ZMQ + msgpack 传输；真正的大张量（hidden state、音频特征等）走 relay 数据平面（u3-l3 会专门讲）。本讲只关注控制平面上的小消息。

需要的术语预备：

- **ZMQ socket 模式**：`PUSH/PULL` 是「一个投递、一个收取」的点对点可靠投递；`PUB/SUB` 是「一个广播、多个订阅」的扇出。Coordinator 发 abort 用 PUB，因为一条中止命令要让**所有**阶段立刻看到。
- **asyncio.Future**：一个「未来才会有的结果」的占位对象。提交请求时立刻造一个 Future，等终态回来时往里 `set_result()`，调用方 `await future` 就拿到结果。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`sglang_omni/pipeline/coordinator.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py) | **本讲主角**。`Coordinator` 类的全部逻辑：注册阶段、提交请求、收完成、合并多终态、广播 abort。 |
| [`sglang_omni/pipeline/control_plane.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/control_plane.py) | ZMQ 通信封装。`CoordinatorControlPlane` 把 Coordinator 的「投递/收完成/广播」三个动作落到具体 socket。 |
| [`sglang_omni/proto/messages.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/messages.py) | 控制平面消息类型：`SubmitMessage`、`CompleteMessage`、`AbortMessage`、`StreamMessage` 等。 |
| [`sglang_omni/proto/request.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/request.py) | `RequestState` 枚举与 `RequestInfo` 追踪结构。 |
| [`sglang_omni/pipeline/mp_runner.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/mp_runner.py) | 多进程 runner。构造 `Coordinator` 并把每个阶段（的 rank0）注册进去。 |
| [`sglang_omni/pipeline/stage_workers.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage_workers.py) | 定义 `owns_external_io`，决定一个阶段是否「对外可见」（TP 场景的关键）。 |
| [`sglang_omni/pipeline/tp_control.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/tp_control.py) | TP leader→follower 的 fanout：把 abort/工作从 rank0 转发给其余 rank。 |
| [`tests/unit_test/pipeline/test_coordinator.py`](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/pipeline/test_coordinator.py) | Coordinator 的单元测试，含多终态合并、abort 的可运行样例（本讲实践会用）。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**Coordinator 全局视图**、**entry_stage 与请求状态机**、**多终态结果合并**、**abort 广播与 TP rank0 通信**。

### 4.1 Coordinator：全局请求路由

#### 4.1.1 概念说明

回到「邮局」比喻。Coordinator 要管五件事（这正是它类文档里列出的职责）：

1. **登记阶段**（register stages）：记住每个阶段的 ZMQ 控制端点。
2. **把新请求投递给入口阶段**（submit to entry stage）。
3. **追踪请求状态**（track request state）。
4. **处理完成回执**（handle completions）。
5. **广播中止信号**（broadcast abort signals）。

它对外暴露三个「端点」概念，对应三种通信方向：

- `completion_endpoint`（PULL，自己 bind）：**收**各阶段的完成/流式回执。
- `abort_endpoint`（PUB，自己 bind）：**发**中止广播给所有阶段。
- 每个阶段的 `control_endpoint`（PUSH，向对方 connect）：**发**投递/管理命令给某个阶段。

#### 4.1.2 核心流程

```text
            ┌─────────────────────────────── Coordinator（主进程 asyncio loop）──────────────────────────────┐
            │                                                                                                  │
  Client ─submit/stream→  _submit_request  ──PUSH──►  entry_stage.control_endpoint  （把请求投进管线的入口）
                                   │                  (其它阶段也通过 control_endpoint 收到 work)
                                   │
  （后台任务 run_completion_loop）  ▼
        PULL(completion_endpoint) ←── CompleteMessage / StreamMessage  （各阶段算完往这里回报）
                                   │
                            _handle_completion / _handle_stream
                                   │
                            收齐终态 → set_result(future) → 唤醒 Client 的 await

  Client.abort ──►  abort  ──PUB(abort_endpoint)──►  所有阶段（含各 TP 组的 rank0）  立刻中止该请求
```

两条关键不变量：

- **投递是点对点（PUSH/PULL）**：入口阶段只有一个，Coorninator 只往那一个端点 PUSH `SubmitMessage`。
- **完成回执也是点对点（PULL）**：所有终态阶段都 PUSH 完成消息到**同一个** `completion_endpoint`，Coordinator 用一个 PULL socket 统一收，靠消息里的 `from_stage` 字段区分来源。

#### 4.1.3 源码精读

先看 Coordinator 类的职责声明与初始化，建立全局印象：

[sglang_omni/pipeline/coordinator.py:L41-L50](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L41-L50) —— 类文档明确列出五大职责（注册阶段、提交请求、追踪状态、处理完成、广播 abort）。

[sglang_omni/pipeline/coordinator.py:L52-L98](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L52-L98) —— 构造函数。它接收两个端点（completion / abort）、入口阶段名 `entry_stage`、可选的静态终态集合 `terminal_stages`，以及一个可选的「按请求动态解析终态」的回调 `terminal_stages_resolver`（4.3 节细讲）。注意它持有四张表：

- `_stages: dict[str, StageInfo]`——阶段注册表（名字 → 端点）。
- `_requests: dict[str, RequestInfo]`——在途请求的追踪信息。
- `_completion_futures: dict[str, asyncio.Future]`——每个请求的「结果占位符」，非流式调用方在 `await` 它。
- `_stream_queues: dict[str, asyncio.Queue]`——流式调用方的消息队列。

再看它如何登记阶段：

[sglang_omni/pipeline/coordinator.py:L100-L108](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L100-L108) —— `register_stage(name, endpoint)` 只是把 `(名字, 端点)` 记进 `_stages`。注意：**这里登记的端点，就是 Coordinator 唯一会 PUSH 命令过去的地址**。TP 场景下只有 rank0 的端点会被登记（见 4.4）。

通信层细节封装在 `CoordinatorControlPlane`：

[sglang_omni/pipeline/control_plane.py:L346-L377](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/control_plane.py#L346-L377) —— `start()` 时 bind 一个 PULL（收完成）和一个 PUB（发 abort）。注释把三个方向说得很清楚。

#### 4.1.4 代码实践（源码阅读型）

**目标**：建立「Coordinator 三方向通信」的肌肉记忆。

**步骤**：

1. 打开 `sglang_omni/pipeline/coordinator.py`，找到 `__init__`，确认它持有 `_stages / _requests / _completion_futures / _stream_queues` 四张表。
2. 打开 `sglang_omni/pipeline/control_plane.py`，找到 `CoordinatorControlPlane`。
3. 用一张纸画出 Coordinator 与外界的三个箭头，标注每个箭头用的是 PUSH/PULL/PUB 中的哪一种、谁 bind 谁 connect。

**预期结果**：你应该得到「PULL（收完成，自己 bind）、PUB（发 abort，自己 bind）、PUSH（发命令给阶段，向对方 connect）」三件套。**待本地验证**：这一步纯阅读，无需运行。

#### 4.1.5 小练习与答案

- **练习 1**：Coordinator 是「收完成」用 PULL、「发 abort」用 PUB，为什么不反过来？
  - **答案**：完成回执是每个终态阶段**主动**回报给 Coordinator 这个**唯一**接收方的，所以用点对点 PULL（一个 socket 收所有）；而 abort 是 Coordinator **一对多**要同时通知所有阶段的，必须用广播 PUB，否则它得逐个 PUSH，既慢又可能在新增阶段时漏发。
- **练习 2**：`register_stage` 只存了 `name` 和 `endpoint` 两项信息。Coordinator 凭什么知道一个阶段是不是「终态」？
  - **答案**：终态不靠 `register_stage` 传入，而是在**构造时**由 `terminal_stages` / `terminal_stages_resolver` 给定（见 4.3）。`register_stage` 只管通信地址，终态是「拓扑属性」而非「通信属性」。

---

### 4.2 entry_stage 与请求状态机

#### 4.2.1 概念说明

入口阶段（entry_stage）是管线的「前门」：所有新请求都从这里进。Coordinator 不需要理解管线的内部结构，它只需要知道「把请求扔给 entry_stage，剩下的让各阶段自己接力」。

与此同时，Coordinator 给每个在途请求维护一个状态，以便回答「这个请求现在到哪了」。状态由 `RequestState` 枚举定义，是一个简单的线性状态机。

#### 4.2.2 核心流程

请求状态机：

```text
   PENDING ──(submit_to_stage 成功)──► RUNNING
                                         │
                ┌────────────────────────┼────────────────────────┐
                ▼                        ▼                        ▼
          COMPLETED                  FAILED                   ABORTED
   (所有终态收齐 set_result)   (任一终态失败 / 致命错误)    (Client 主动 abort)
```

提交一个请求的内部流程（`_submit_request`）：

```text
1. 校验：fatal_error?  重复 request_id?  entry_stage 已注册?
2. （可选）把裸 inputs 包成 OmniRequest
3. 建 RequestInfo(state=PENDING, current_stage=entry_stage, terminal_stages=...)
4. loop.create_future()  ── 这是 Client 将 await 的占位符
5. 构造 StagePayload(request_id, request, data=raw_inputs)
6. control_plane.submit_to_stage(entry_stage, ..., SubmitMessage(request_id, payload))  ── PUSH 给入口
7. state 置 RUNNING
```

关键：**第 6 步之后，Coordinator 就「撒手」了**——请求在管线里怎么走、经过哪些阶段，它一概不管，只等终态回执回到它的 PULL socket。

#### 4.2.3 源码精读

先看状态机本身：

[sglang_omni/proto/request.py:L9-L16](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/request.py#L9-L16) —— `RequestState` 五个值：`PENDING / RUNNING / COMPLETED / FAILED / ABORTED`。

[sglang_omni/proto/request.py:L19-L28](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/request.py#L19-L28) —— `RequestInfo` 追踪结构，含 `state`、`current_stage`、`terminal_stages`、`result`、`error`。这就是 `_requests` 表里每个请求存的东西。

提交逻辑：

[sglang_omni/pipeline/coordinator.py:L360-L417](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L360-L417) —— `_submit_request`。注意 L364–365 的「致命错误快速失败」（若 `_fatal_error` 非空直接抛，不让新请求进入已崩溃的运行时）；L372–373 对裸 inputs 的兜底包装；L384–386 建 Future；L402–407 真正 PUSH `SubmitMessage` 给入口阶段；L410 把状态置为 RUNNING。

`SubmitMessage` 的结构：

[sglang_omni/proto/messages.py:L237-L255](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/messages.py#L237-L255) —— 只有两字段：`request_id` 和 `data`（一个 `StagePayload`）。投递就是这么轻。

Coordinator 给上层（Client）的两个入口方法，区别只在「怎么等结果」：

[sglang_omni/pipeline/coordinator.py:L316-L325](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L316-L325) —— `submit`：非流式。提交后直接 `await future`，拿到最终合并结果返回。

[sglang_omni/pipeline/coordinator.py:L327-L358](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L327-L358) —— `stream`：流式。建一个 `asyncio.Queue`，边收边 `yield`，直到收齐所有期望终态（L339–353）。流式调用方**不会** `await` 那个 future，而是从队列读消息（这点在 4.4 的异常处理里很关键）。

后台的「收件循环」：

[sglang_omni/pipeline/coordinator.py:L479-L497](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L479-L497) —— `run_completion_loop` 是一个常驻后台任务，不停 `recv_event()`，按消息类型分派：`StreamMessage` → `_handle_stream`；`AdminResultMessage` → `_handle_admin_result`；其余（即 `CompleteMessage`）→ `_handle_completion`。

#### 4.2.4 代码实践（源码阅读型）

**目标**：定位「提交」与「收件」两条入口，确认它们用到的不同数据结构。

**步骤**：

1. 在 `coordinator.py` 中定位 `_submit_request`（提交）和 `run_completion_loop`（收件）。
2. 追踪一次**非流式**提交：`submit → _submit_request → 建 future → submit_to_stage`；再追踪结果如何回来：`run_completion_loop → _handle_completion → future.set_result`。
3. 对照确认：非流式调用方 await 的是 `_completion_futures[rid]`，流式调用方读的是 `_stream_queues[rid]`。

**预期结果**：你能画出两条独立的「回执通道」——一条给非流式（Future），一条给流式（Queue）。**待本地验证**。

#### 4.2.5 小练习与答案

- **练习 1**：`_submit_request` 里为什么先建 `RequestInfo`（L376）再 PUSH（L403），而不是反过来？
  - **答案**：如果先 PUSH 再建追踪结构，万一 PUSH 后、建表前就收到了回执（极端情况下异步竞态），`_handle_completion` 会因为查不到 `request_id` 而把回执丢弃（见 4.3 的 L518–522）。先登记再投递，保证「回执一定有主」。
- **练习 2**：`submit`（非流式）和 `stream`（流式）都调 `_submit_request`，它们对 `entry_stage` 的处理有区别吗？
  - **答案**：没有。入口阶段对两种调用方是同一个；区别只在结果如何取回——非流式 await 一个 Future，流式读一个 Queue。

---

### 4.3 多终态结果合并（terminal merge）

> 这是本讲的核心难点，也是 Coordinator 存在的最大价值。

#### 4.3.1 概念说明

回顾 u1-l1：一条管线可以有**多个终态**。最典型的就是同时产出文本和语音的 omni 模型——`decode` 阶段吐文本、`code2wav` 阶段吐音频，它们都是「没有下游」的终点。Coordinator 不能在收到第一个终态时就宣告完成，否则另一个终态的产出会丢；它要**收齐所有期望的终态，把它们合并成一个字典**，再交给 Client。

这里有三层「终态」概念，务必分清：

1. **静态终态集合 `terminal_stages`**：构造时给定的、这条管线**可能**成为终态的全部阶段（如 `{decode, code2wav}`）。
2. **每个请求的期望终态 `terminal_stages`（存在 `RequestInfo` 里）**：**这一次**请求真正要等的终态子集。比如纯文本请求只等 `decode`，语音请求等 `{decode, code2wav}`。这个子集由 `terminal_stages_resolver` 按**请求内容**动态决定。
3. **某次完成消息的来源 `from_stage`**：是 `decode` 还是 `code2wav`。

#### 4.3.2 核心流程

完成处理的判定逻辑（用集合运算表达最清晰）。设 \( E \) 为该请求的期望终态集合，\( P \) 为已收到完成的终态集合（`_partial_results` 的键集）：

- **期望终态为空或单元素**（\(|E| \le 1\)）：收到第一个完成就直接 `set_result(result)`，不合并。
- **多终态**（\(|E| \ge 2\)）：每收到一个完成，记录 \( P \leftarrow P \cup \{\text{from\_stage}\} \)。当且仅当 \( E \subseteq P \)（代码里写 `set(partials) >= expected_terminal_stages`）时，合并所有部分结果：
  \[
  \text{merged} = \{\, \text{stage}: \text{result} \mid \text{stage} \in E \,\}
  \]
  再 `set_result(merged)`。
- **失败快速失败（fail-fast）**：任一期望终态返回 `success=False`，立刻把整个请求置 `FAILED`，广播 abort，丢弃已收的部分结果。

完整判定流程图：

```text
_handle_completion(msg):
  if msg.success == False:                         ── fail-fast
      state=FAILED; broadcast_abort; 清 partials; set_exception / 入队错误; 移除请求; return

  if from_stage not in E:                          ── 非期望终态（或该请求不期望它）
      debug 日志，忽略，return

  if |E| <= 1:                                     ── 单终态 / 未配置终态
      state=COMPLETED; set_result(msg.result); (流式)入队; 移除请求; return

  # 多终态
  partials[from_stage] = msg.result
  (流式) 把本次完成也按阶段入队
  if set(partials) < E:  return                    ── 还没收齐，继续等

  # 收齐了
  merged = dict(partials)                          ── { "decode": {...}, "code2wav": {...} }
  state=COMPLETED; set_result(merged); 移除请求
```

#### 4.3.3 源码精读

先看终态是怎么「按请求」解析出来的：

[sglang_omni/pipeline/coordinator.py:L681-L706](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L681-L706) —— `_resolve_terminal_stages`。逻辑要点：

- 没有 resolver → 直接用静态 `terminal_stages`。
- 有 resolver → 用请求内容算出一个子集；resolver 返回 `None` 也回退到静态集合。
- **校验很严**：返回空列表报 `no terminal stages`；返回静态集合之外的阶段报 `outside the static terminal stages`；返回单个字符串报 `must return a sequence`。这是「fail loud」设计——配置错了立刻在提交时报错，而不是默默等不到结果。

[sglang_omni/pipeline/coordinator.py:L708-L712](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L708-L712) —— `_expected_terminal_stages`：取某个请求当前期望的终态集合（优先用 `RequestInfo` 里那份，回退到静态集合）。这是完成处理时的判定基准。

现在是**本讲最重要的一段**——完成处理：

[sglang_omni/pipeline/coordinator.py:L499-L587](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L499-L587) —— `_handle_completion`。逐段看：

[sglang_omni/pipeline/coordinator.py:L527-L540](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L527-L540) —— **fail-fast 分支**。任一终态失败：置 FAILED、广播 abort（让其它仍在跑的阶段停下）、清掉 `_partial_results`、把错误塞进 future（非流式）/ 流队列（流式）、移除请求追踪。

[sglang_omni/pipeline/coordinator.py:L542-L551](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L542-L551) —— 收到的 `from_stage` 不在期望终态集合里时**忽略**。注释叫 "ignoring completion from inactive terminal"。典型场景：纯文本请求只期望 `decode`，但 `code2wav` 因某种原因也回了一个完成——忽略它。

[sglang_omni/pipeline/coordinator.py:L554-L564](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L554-L564) —— **单终态分支**（`len(E) <= 1`）：直接 `set_result(msg.result)`。这是「经典」行为，也是未配置 `terminal_stages` 时的兜底。

[sglang_omni/pipeline/coordinator.py:L566-L587](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L566-L587) —— **多终态合并分支**。核心三步：

1. L567–568：`partials[from_stage] = msg.result`，按阶段名存。
2. L574–575：`if set(partials) < expected_terminal_stages: return`——**还没收齐**就继续等（注意这是真子集判定 `<`，不是 `!=`）。
3. L577–587：收齐了 → `merged = dict(partials)` → `set_result(merged)`。

关键观察：**多终态时，最终结果是 `{ "decode": <文本结果>, "code2wav": <音频结果> }` 这样一个以阶段名为键的字典**，而不是某单个阶段的裸产出。下游 Client（u2-l3）正是基于这个字典结构去拼 `CompletionResult`。

`CompleteMessage` 长什么样：

[sglang_omni/proto/messages.py:L167-L195](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/messages.py#L167-L195) —— `request_id / from_stage / success / result / error`。`from_stage` 就是 Coordinator 用它来分桶合并的依据。

#### 4.3.4 代码实践（可运行）

> 这正是讲义规格要求的实践：定位提交与完成处理方法，说明 `decode` + `code2wav` 双终态如何合并。

**目标**：用一个真实可运行的单测，亲眼看到双终态合并与 fail-fast 行为。

**步骤**：

1. 阅读真实测试 [tests/unit_test/pipeline/test_coordinator.py:L15-L49](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/pipeline/test_coordinator.py#L15-L49)。它构造了一个 `terminal_stages=["decode", "code2wav"]`、入口 `preprocess` 的 Coordinator，用 `inproc://`（进程内 ZMQ，免端口）端点，并换上一个记录型假控制面 `RecordingCoordinatorControlPlane`（不发真 ZMQ）。
2. 看它如何**手工喂**完成消息：先喂 `decode` 成功，断言 future **未完成**（只到一半）；再喂 `code2wav` 成功，断言 future 的结果是 `{"decode": {...}, "code2wav": {...}}`。
3. 看同文件的失败用例 [tests/unit_test/pipeline/test_coordinator.py:L254-L285](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/pipeline/test_coordinator.py#L254-L285)：先 `decode` 成功，再喂 `code2wav` **失败**，断言 future 抛错、`_requests` 与 `_partial_results` 都被清空、abort 被广播。
4. **运行它**（在容器/venv 内， Coordinator 不需要 GPU）：
   ```bash
   pytest tests/unit_test/pipeline/test_coordinator.py -k "multi_terminal or failure_completion" -x -q
   ```

**需要观察的现象**：

- 双终态用例：喂第一个完成时 `future.done()` 为 `False`；喂第二个完成时 `future.result()` 是含两个键的合并字典。
- 失败用例：第二个完成 `success=False` 后，`await future` 抛 `RuntimeError`，且 `control_plane.aborts` 里有一条针对该 `request_id` 的 abort。

**预期结果**：两个用例都通过，证明「收齐才合并、任一失败 fail-fast 并广播 abort」两条契约成立。这一步**可在本地验证**（无需 GPU，纯异步逻辑）。

#### 4.3.5 小练习与答案

- **练习 1**：为什么多终态分支用 `set(partials) < expected_terminal_stages`（真子集）而不是 `!=` 来判断「没收齐」？
  - **答案**：用真子集 `<` 更稳妥：只要已收集合还**没有覆盖**全部期望终态，就继续等。它与「收齐」的判定 `>=`（即覆盖）互补。此外它天然兼容「同一终态不会重复回执」（一旦收齐就移除请求，不会再进这条分支）。
- **练习 2**：若请求只期望 `decode`，但管线里 `code2wav` 也回了一个 `success=True` 的完成，会发生什么？
  - **答案**：`from_stage="code2wav"` 不在期望集合 `{decode}` 里，命中 [L542-L551](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L542-L551) 的忽略分支，记一条 debug 日志后 return，不影响请求。这正是「按请求动态解析终态子集」带来的健壮性。
- **练习 3**：多终态最终结果的**结构**是什么？下游如何区分文本和音频？
  - **答案**：是一个 `{阶段名: 该阶段 result}` 的字典，如 `{"decode": {...}, "code2wav": {...}}`。下游（Client 的 builder，见 u2-l3）按键名取对应阶段的产出，再分别拼成文本与音频。

---

### 4.4 abort 广播与 TP rank0 通信

#### 4.4.1 概念说明

「abort」= 主动中止一个请求。典型场景：用户在前端点了「停止生成」，或 Client 超时。中止必须**同时**让管线里所有正在处理该请求的阶段停下来，否则一个阶段继续算、另一个阶段已停，会留下悬挂的张量和半成品。

为什么 abort 用 **PUB/SUB 广播**，而不是 Coordinator 逐个 PUSH 给每个阶段？因为：

1. 阶段可能很多（一条 omni 管线有七八个阶段，再乘以 TP rank 数），逐个 PUSH 慢。
2. 阶段集合在运行时可能变化（TP rank 数不同），广播不依赖「我记住了谁」。

**TP rank0 通信**：当一个阶段开了张量并行（`tp_size > 1`），它会派生成多个进程（rank0/leader、rank1…/follower）。但 Coordinator **只和 rank0 通信**——只把命令 PUSH 给 rank0、只从 rank0 收完成回执。其余 rank 对 Coordinator **完全不可见**。这是为了：避免 Coordinator 去理解 TP 内部结构；TP 组内部用 NCCL/进程内队列自己同步。

abort 要送达**所有 rank**，于是采用两段式：Coordinator 广播 → 各 TP 组的 rank0（SUB 收到）→ rank0 再 fanout 给组内 follower。

#### 4.4.2 核心流程

abort 的端到端路径：

```text
Client.abort(request_id)
      │
      ▼
Coordinator.abort:  校验状态(已完成/失败/中止的不再处理)
      │
      ▼  control_plane.broadcast_abort(AbortMessage(rid))   ── PUB 一次
      │
      ▼
 所有阶段的 SUB socket 收到  ──► 每个阶段把自己的该请求中止
      │
      └─ 对于 TP 组：rank0(leader) 的 SUB 收到
              │
              ▼  leader._tp_fanout.fanout_abort(msg)   ── 转发给 follower 队列
              │
              ▼
        各 follower 的 abort_queue 收到 ──► follower 也中止
```

Coordinator 侧 abort 后的善后：置 `state=ABORTED`、让 future 进入取消态（非流式）/ 向流队列塞一条失败完成（流式）、清掉请求追踪。

#### 4.4.3 源码精读

abort 消息极简：

[sglang_omni/proto/messages.py:L153-L164](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/messages.py#L153-L164) —— `AbortMessage` 只带一个 `request_id`。

广播机制：

[sglang_omni/pipeline/control_plane.py:L411-L415](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/control_plane.py#L411-L415) —— `CoordinatorControlPlane.broadcast_abort` 往 PUB socket 发。配套的 [sglang_omni/pipeline/control_plane.py:L149-L163](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/control_plane.py#L149-L163) 的 `PubSocket` 绑定后会 `sleep(0.1)`，给订阅者连接的时间（PUB 的经典「慢连接」问题：订阅者还没连上时发的消息会丢）。

Coordinator 的 abort 方法：

[sglang_omni/pipeline/coordinator.py:L432-L477](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L432-L477) —— `abort`。要点：

- L441–450：找不到请求，或请求已 `COMPLETED/FAILED/ABORTED`，直接返回 `False`（幂等，不重复中止）。
- L453：`broadcast_abort` 一次性广播。
- L456–470：置 `ABORTED`；调 `_reject_completion_future`（见下）；若是流式请求，往流队列塞一条 `success=False` 的完成，让消费者退出。
- L472–474：清掉 `_requests` 和 `_partial_results`。

非流式 vs 流式的 future 处理是个精妙细节：

[sglang_omni/pipeline/coordinator.py:L419-L430](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L419-L430) —— `_reject_completion_future`。非流式调用方在 `await future`，所以必须 `set_exception()`；而流式调用方**从不 await 这个 future**（它读队列），若给 future `set_exception` 却没人取，事件循环会报 "Future exception was never retrieved"。所以流式情况下改成 `future.cancel()`。注释把这个权衡讲得很清楚。

现在看 **TP rank0** 怎么实现。先确认 Coordinator 登记阶段时只会拿到 rank0 的端点：

[sglang_omni/pipeline/mp_runner.py:L443-L445](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/mp_runner.py#L443-L445) —— runner 启动后，遍历每个 group 的 `stage_control_endpoints` 调 `register_stage`。

[sglang_omni/pipeline/stage_workers.py:L235-L241](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage_workers.py#L235-L241) —— `stage_control_endpoints` 只返回 `owns_external_io` 为真的 spec 的端点。

[sglang_omni/pipeline/stage_workers.py:L108-L110](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage_workers.py#L108-L110) —— `owns_external_io` 当且仅当 `role in {"single", "leader"}` 为真。也就是说：**follower（rank>0）不对外可见，Coordinator 根本不知道它们存在**。

最后看 abort 如何穿透到 follower：

[sglang_omni/pipeline/stage/runtime.py:L1538-L1542](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L1538-L1542) —— leader 的 abort 循环：先从自己的 SUB socket 收到 Coordinator 广播的 abort，然后 `if role == "leader" and tp_fanout is not None: await self._tp_fanout.fanout_abort(msg)`，最后自己 `_on_abort`。

[sglang_omni/pipeline/tp_control.py:L71-L73](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/tp_control.py#L71-L73) —— `fanout_abort` 把 abort 塞进每个 follower 的 `abort_queue`。follower 用 `TPollowerControlPlane` 从该队列读 abort（不走 ZMQ）。

> 小结：Coordinator 只跟每个 TP 组的 rank0 说话；abort 用广播到 rank0，再由 rank0 fanout 到组内 follower，从而「一次广播、全员收到」。

#### 4.4.4 代码实践（可运行）

**目标**：验证 abort 在流式调用下「不留下未取异常」这一微妙契约。

**步骤**：

1. 阅读 [tests/unit_test/pipeline/test_coordinator.py:L333-L376](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/pipeline/test_coordinator.py#L333-L376)。它给事件循环装了一个异常处理器（`loop.set_exception_handler`），启动一个流式消费者，然后 abort，最后断言：流以 `error_sink == ["aborted"]` 结束、`future.cancelled()` 为真、且 `del future; gc.collect()` 后没有任何 "never retrieved" 上下文。
2. 运行：
   ```bash
   pytest tests/unit_test/pipeline/test_coordinator.py -k "stream_abort_cancels_future" -x -q
   ```
3. 想一想：如果把 `_reject_completion_future` 里流式分支的 `future.cancel()` 改成 `future.set_exception(...)`，这个测试会怎么报错？

**需要观察的现象**：测试通过；事件循环的 handler 没有捕获到 "never retrieved" 字样。

**预期结果**：通过。说明流式请求被 abort 时，future 被干净取消，不会污染事件循环。**可在本地验证**（无需 GPU）。

#### 4.4.5 小练习与答案

- **练习 1**：Coordinator 为什么要 `owns_external_io` 来过滤，而不是把每个 TP rank 都登记一遍？
  - **答案**：登记每个 rank 会让 Coordinator 不得不理解「这几个 rank 属于同一阶段、要一起投递」，复杂度爆炸；而且 follower 之间用 NCCL collective 通信，只有 rank0 收到外部 work 后才能正确发起 collective。只登记 rank0，把 TP 内部协调完全留给该阶段自己，是清晰的职责切分。
- **练习 2**：abort 已经通过 PUB 广播到了 rank0，为什么 leader 还要再 `fanout_abort` 给 follower？
  - **答案**：follower 用的是 `TPFollowerControlPlane`（基于进程内队列），**没有**自己的 ZMQ SUB socket，根本收不到 Coordinator 的 PUB 广播。它们只能靠 leader 转发。这是「follower 对 Coordinator 不可见」的直接后果。
- **练习 3**：对一个已经 `COMPLETED` 的请求再调 `abort`，会怎样？
  - **答案**：[L445-L450](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L445-L450) 直接返回 `False`，不会广播 abort，幂等。

## 5. 综合实践

**任务**：用一张图 + 一段话，把「一次同时产出文本和语音的请求」在 Coordinator 眼里的完整一生讲清楚。

请按以下顺序完成：

1. **画拓扑**：阶段为 `preprocess → thinker → talker → (decode, code2wav)`，其中 `thinker` 是 `tp_size=2` 的张量并行阶段。标注 `decode` 和 `code2wav` 为终态。
2. **画通信**：在图上标出 Coordinator 的三个 socket（completion=PULL、abort=PUB、entry PUSH），以及 thinker 的两个进程（leader=rank0、follower=rank1）。
3. **走流程**：用一段话描述——
   - Coordinator 把请求 PUSH 给 `preprocess`（entry）；
   - 请求一路接力到 `talker`，再 fan-out 到 `decode` 和 `code2wav`；
   - `decode` 先回 `CompleteMessage(success=True, result={...文本...})`，Coordinator 把它存进 `_partial_results["req"]["decode"]`，**不** resolve；
   - `code2wav` 再回 `CompleteMessage(success=True, result={...音频...})`，此时 `set(partials) >= expected_terminal_stages`，Coordinator 合并出 `{"decode": ..., "code2wav": ...}` 并 `set_result`，唤醒 Client；
   - 若中途用户 abort，Coordinator PUB 一条 `AbortMessage`，`preprocess/talker/talker-leader` 的 SUB 收到，leader 再 fanout 给 follower，全员停下。
4. **自检**：在图上指出——(a) 哪些阶段对 Coordinator「可见」（答案：只有 `owns_external_io` 为真的，即各阶段 rank0 / single）；(b) Client 最终拿到的是什么结构（答案：以阶段名为键的合并字典）。

> 提示：你可以参考 [tests/unit_test/pipeline/test_coordinator.py:L52-L108](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/pipeline/test_coordinator.py#L52-L108) 的 `terminal_stages_resolver` 用例——它展示了「同一 Coordinator、按请求内容决定终态子集」的真实写法（文本请求只等 `decode`、语音请求等 `decode`+`code2wav`）。

## 6. 本讲小结

- **Coordinator 是全局请求路由**：登记阶段端点、把请求 PUSH 给 entry_stage、用 PULL 收完成、用 PUB 广播 abort，自身不碰 GPU。
- **请求状态机**：`PENDING → RUNNING → COMPLETED/FAILED/ABORTED`，追踪结构是 `RequestInfo`，结果占位符是 `asyncio.Future`（非流式）或 `asyncio.Queue`（流式）。
- **多终态合并是核心**：`terminal_stages` 给静态集合，`terminal_stages_resolver` 按请求内容算出本次期望子集；收齐所有期望终态后合并成 `{阶段名: result}` 字典再 resolve，任一终态失败则 fail-fast 并广播 abort。
- **单终态是退化情形**：`len(E) <= 1` 时收到第一个完成就直接 resolve，不合并。
- **abort 用 PUB/SUB 广播**：一条命令让所有阶段同时停下；非流式 `set_exception`、流式 `cancel()`，避免「Future 异常从未被取」。
- **TP 阶段只与 rank0 通信**：`owns_external_io` 过滤掉 follower，Coordinator 只登记/投递/收 leader；abort 由 leader 再 fanout 到 follower 队列。

## 7. 下一步学习建议

本讲把「请求如何被监督到完成」讲清了，但刻意回避了两件事的内部细节：

1. **请求在阶段之间到底怎么接力**——`Stage` 如何收控制消息、做 fan-in、桥接 scheduler。这是下一讲 **u3-l1（Stage 抽象与 IO 外壳）** 的主题。
2. **控制平面消息的完整分类与 ZMQ 细节**——`DataReadyMessage`、`DataAckMessage` 等数据平面相关消息，留到 **u3-l2（控制平面与 ZMQ 消息）**。
3. **多终态的 `terminal_stages` 从哪来**——它由 `PipelineConfig` 派生（u2-l5 的「派生字段」），想搞清 `terminal_stages` / `terminal_stages_fn` 如何从 stage 拓扑自动算出，可结合 **u2-l5（声明式配置）** 一起读。

建议接下来读 **u3-l1**，把「Coordinator 投递出去之后，请求在阶段侧发生了什么」补全，形成 Stage↔Scheduler↔Coordinator 的完整闭环认知。
