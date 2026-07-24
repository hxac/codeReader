# Stage 抽象与 IO 外壳

## 1. 本讲目标

上一讲（u2-l4）我们停在 **Coordinator**：它是主进程里的「全局请求路由」，把请求 PUSH 给入口阶段、收集终态结果、广播 abort。但 Coordinator 把消息送进某个阶段的控制端点之后，**阶段内部到底发生了什么**，我们一直当作黑盒。

本讲就打开这个黑盒。读完本讲，你应当能够：

- 说清楚 `Stage` 为什么被定义为一个「IO 外壳（IO shell）」，它管什么、不管什么。
- 解释本讲的核心不变量：**`Stage` 的代码不因 scheduler 类型而分支**——`SimpleScheduler`、`OmniScheduler`、流式调度器共用同一套表面。
- 理解 `InputHandler` / `DirectInput` / `AggregatedInput` 三者如何描述「单输入直通」与「fan-in 聚合」两种输入模式。
- 画出一条「控制消息进来 → 读 relay payload → 聚合 → 推入 `scheduler.inbox`」的路径，并能定位每一步在源码里的位置。
- 解释为什么「把所有可执行工作推入 `scheduler.inbox`」是整个 Stage 抽象成立的关键不变量。

## 2. 前置知识

本讲假设你已经读过 u2-l4（Coordinator）。下面三个概念会反复出现，先建立直觉：

1. **控制平面 vs 数据平面**。控制平面传「轻量命令和状态」（谁完成了、谁中止了、数据就绪了），用 ZMQ + msgpack 序列化；数据平面传「大张量」（图像 embedding、音频波形、KV cache 片段），走 relay 后端（CUDA IPC / 共享内存 / NCCL / NIXL / Mooncake）。一个 `Stage` 同时是这两条平面的终点。

2. **阶段（stage）是一次生成接力中的一棒**。一次 omni 生成被拆成 preprocessing → encoder → thinker → talker → decode/code2wav 等多棒。每一棒都是一个 `Stage` 实例，它从上游收数据、交给自己的 scheduler 算、把结果发给下游。

3. **scheduler 是真正「算东西」的人**。`Stage` 自己不算，它把活儿装进 `scheduler.inbox`；scheduler 算完，把结果装进 `scheduler.outbox`。`Stage` 再把 outbox 里的东西翻译回控制平面消息发出去。所以你可以把 `Stage` 想象成「前台」+「快递员」，scheduler 是「后厨」。

> 一个关键区分：`Stage` 跑在 **asyncio 事件循环**里（负责 IO 与消息收发），而 scheduler 跑在 **独立线程**里（负责阻塞式的计算循环）。两者之间靠两个线程安全的 `queue.Queue`（inbox / outbox）通信。这个「跨线程、靠队列」的设计是本讲后半段的难点，先记住这个画面。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到 |
| --- | --- | --- |
| [sglang_omni/pipeline/stage/runtime.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py) | `Stage` 类的全部实现：收消息、读写 payload、fan-in、流式路由、桥接 scheduler。本讲的主战场。 | 几乎全部章节 |
| [sglang_omni/pipeline/stage/input.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/input.py) | 输入处理抽象：`InputHandler`、`DirectInput`、`AggregatedInput`。 | §4.2、§4.3 |
| [sglang_omni/scheduling/messages.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/messages.py) | scheduler 与 Stage 之间的消息类型 `IncomingMessage` / `OutgoingMessage`。 | §4.4 |
| [sglang_omni/scheduling/simple_scheduler.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py) | `SimpleScheduler`，用来印证「同一种 inbox/outbox 表面」。 | §4.4 |
| [sglang_omni/pipeline/stage_workers.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage_workers.py) | 子进程里如何用 `StageLaunchConfig` 把 `AggregatedInput` / `DirectInput` 与 scheduler 装配成一个 `Stage`。 | §4.1、§4.3 |
| [sglang_omni/pipeline/local_dispatch.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/local_dispatch.py) | 同进程阶段之间直接传 Python 对象引用的 `LocalStageDispatcher`。 | §4.1 |
| [docs/developer_reference/pipeline.md](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/pipeline.md) | 官方对 Stage / Scheduler 的设计说明，本讲术语与它对齐。 | 全篇 |

## 4. 核心概念与源码讲解

### 4.1 Stage：不分支于 scheduler 类型的 IO 外壳

#### 4.1.1 概念说明

打开 `runtime.py` 顶部的模块注释，作者用一句话给 `Stage` 下了定义：

[sglang_omni/pipeline/stage/runtime.py:1-8](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L1-L8) —— 注释明确：Stage 负责控制平面消息、数据平面（relay）IO、输入聚合、流式 chunk 路由、abort 追踪、profiling；**所有计算都 dispatch 给 scheduler（OmniScheduler 或 SimpleScheduler）**。

这就是「IO 外壳」的全部含义：

- **外壳负责 IO 与编排**：收谁的消息、读谁的 payload、聚合几个上游、把结果发给谁、流式 chunk 怎么路由、abort 怎么清理。
- **内核负责计算**：scheduler 拿到 payload 后怎么算（是不是 AR、要不要 KV cache、要不要 batch），外壳完全不关心。

文档 [docs/developer_reference/pipeline.md](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/pipeline.md) 把它说得更直白：「The important invariant is that `Stage` does not branch on scheduler type.」——Stage 的代码里**没有 `if isinstance(scheduler, OmniScheduler)` 这样的分支**。`SimpleScheduler`、`OmniScheduler`、`Code2WavScheduler` 都对外暴露同一套表面（`inbox` / `outbox` / `start` / `stop` / `abort`），所以同一份 Stage 代码能驱动它们全部。

为什么这条不变量如此重要？因为 omni 管线里有性质迥异的阶段：

- preprocessing / encoder / decode / code2wav 这类**非 AR 阶段**，用 `SimpleScheduler`（无 KV cache、无 batch，就是 `inbox.get → 函数 → outbox.put`）。
- thinker / talker 这类 **AR 阶段**，用 `OmniScheduler`（复用 SGLang 的 prefill/decode/KV cache 管理）。
- 流式 vocoder 用 `Code2WavScheduler`（按 chunk 处理）。

如果 Stage 要为每种 scheduler 写一套收发逻辑，代码会指数级膨胀，而且每接一个新模型族都要改 Stage。把 scheduler 差异「压」进统一的 inbox/outbox 表面之后，Stage 只需要写一遍。

#### 4.1.2 核心流程

一个 `Stage` 实例的生命周期可以画成两条并行的「跑道」：

```text
【跑道 A：asyncio 事件循环（Stage 自己跑 run()）】
   control_plane.recv()  ←收控制消息（ZMQ）
        │
        ├─ SubmitMessage      → _on_submit        → _execute ─┐
        ├─ DataReadyMessage   → _schedule_receive_task        │
        │       ├─ payload    → _on_data_ready → 读relay → _receive_payload_from_stage
        │       │                                        → input_handler.receive → _execute ─┤
        │       ├─ chunk      → _on_stream_chunk  → _route_stream_item  ──────────────────┤
        │       └─ done/error → _on_stream_signal → scheduler.inbox.put(stream_done) ────┤
        ├─ DataAckMessage     → _comm.ack_transfer                              │  推入
        ├─ AdminMessage       → _on_admin                                      │  scheduler.inbox
        └─ AbortMessage(广播) → _on_abort  ─────────────────────────────────── ┤  （跑道 A→B 的桥）
                                                                                   ▼
【跑道 B：独立线程 scheduler.start()】
   loop:  msg = inbox.get()  →  compute / forward  →  outbox.put(result/stream/error)
                                                                                   │
【跑道 A：_drain_outbox 协程，把 B 的产出搬回 A】  ◄────────────────────────────────┘
   outbox.get()
        ├─ result → _route_result → 下游 DataReadyMessage / 终态 CompleteMessage
        ├─ stream → _send_stream_to_target（local_object / CUDA IPC / relay）
        └─ error  → _send_failure → CompleteMessage(success=False)
```

要点：

1. **所有「该干的活」最终都汇入 `scheduler.inbox`**，无论是新请求（`new_request`）、流式 chunk（`stream_chunk`）还是流式结束（`stream_done`）。Stage 从不在 asyncio 线程里直接调用模型前向。
2. **scheduler 在独立线程里跑阻塞循环**，算完把结果放进 `scheduler.outbox`。
3. **`_drain_outbox` 把结果搬回 asyncio 线程**，再翻译成控制平面消息（发给下游阶段或 Coordinator）。

#### 4.1.3 源码精读

先看构造函数里的两个关键字段——它们揭示了「IO 外壳」靠什么对接 scheduler：

[sglang_omni/pipeline/stage/runtime.py:104-121](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L104-L121) —— `self.input_handler = input_handler or DirectInput()`（输入默认直通）、`self.scheduler = scheduler`（持有一个 scheduler 引用但不关心其类型）、`self._owns_external_io = role in {"single", "leader"}`（只有 leader/single 才对外收发，follower 不可见，承接 u2-l4 的「TP 阶段只与 rank0 通信」）。

`Stage` 类的 docstring 还澄清了一个容易混淆的点：

[sglang_omni/pipeline/stage/runtime.py:61-76](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L61-L76) —— `role="single"` 表示这个阶段**拥有自己的 ZMQ 控制平面和 relay reader**（即不是 TP follower），但**不代表独占一个 OS 进程**：声明式拓扑之后，多个 `role="single"` 阶段可以共享一个进程和一个 asyncio 循环（这时它们共享一个失败域，见 `stage_workers._run_process`）。`leader` / `follower` 则表示 TP 组内的 rank0 与 rank>0。

再看主循环 `run()`，它就是「跑道 A」的总入口：

[sglang_omni/pipeline/stage/runtime.py:241-275](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L241-L275) —— `run()` 先 `start()`，再起两个常驻协程（abort 监听、outbox 抽干），然后进入 `while self._running: msg = await self.control_plane.recv()` 的消息循环。leader 收到 Shutdown/Profiler/Admin 类消息会先 fanout 给 follower；普通消息交给 `_handle_message` 分派。

分派逻辑 `_handle_message` 是一个干净的 `isinstance` 链，**没有任何 scheduler 类型判断**：

[sglang_omni/pipeline/stage/runtime.py:296-308](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L296-L308) —— `SubmitMessage → _on_submit`、`DataAckMessage → _comm.ack_transfer`、`DataReadyMessage → _schedule_receive_task`、profiler/admin 各自处理。注意 `DataReadyMessage` 是数据平面「数据就绪」通知（详见 §4.4 的精读），它根据 `is_done`/`error`/`chunk_id` 被分到三种不同的处理函数。

#### 4.1.4 代码实践

**实践目标**：亲手验证「Stage 不分支于 scheduler 类型」这条不变量。

**操作步骤**：

1. 打开 [sglang_omni/pipeline/stage/runtime.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py)。
2. 在文件内搜索 `self.scheduler` 的所有使用点（编辑器里 `Cmd/Ctrl+F` 搜 `self.scheduler`）。
3. 逐一记录每个使用点调用了 scheduler 的哪个属性/方法。

**需要观察的现象**：你会看到 Stage **只**用到 scheduler 的这几个成员：`.inbox`（put 消息）、`.outbox`（get 消息）、`.start()`、`.stop()`、`.abort(request_id)`、`.admin(action, payload)`、`.tp_rank`、`.requires_tp_work_fanout`。**没有任何 `isinstance(scheduler, ...)` 或对 scheduler 具体子类的引用**。

**预期结果**：确认 Stage 把 scheduler 当成一个「黑盒接口」用。这意味着你哪怕明天新写一个 `MyFancyScheduler`，只要它实现了上述表面，就能不动 Stage 一行代码地接进来。这就是不变量的价值。

> 说明：本实践是「源码阅读型实践」，不需要 GPU，也不需要启动服务。

#### 4.1.5 小练习与答案

**练习 1**：为什么把 `role="single"` 的多个阶段放进同一个 OS 进程会让它们「共享失败域」？

**参考答案**：因为它们共享同一个 asyncio 事件循环（`_run_process` 用 `asyncio.gather` 并发跑所有 `stage.run()`）。任何一个 stage 的协程抛出未处理异常，整个进程退出，`MultiProcessPipelineRunner` 的监控会把该进程上所有在途请求 fail-all。`role="single"` 只表示「拥有自己的 ZMQ 控制平面」，与进程独占无关（见 runtime.py:67-76 的 docstring）。

**练习 2**：follower 阶段（`role="follower"`）为什么不能直接给下游阶段发数据？

**参考答案**：`self._owns_external_io = role in {"single", "leader"}` 为 `False`（runtime.py:121）。`_send_to_stage` 和 `_send_stream_to_target` 开头都有 `if not self._owns_external_io: raise/return`。外部 IO（收发控制消息、读写 relay、给下游发数据）只能由 rank0（leader）承担，承接 u2-l4「TP 阶段只与 rank0 通信」。

---

### 4.2 InputHandler 与 DirectInput：输入处理抽象

#### 4.2.1 概念说明

一个阶段的输入来源并不总是单一的。大多数阶段只有一个上游（比如 encoder 的上游只有 preprocessing），收到 payload 就能立刻算；但有些阶段需要**等多路输入到齐再合并**（比如某个聚合阶段要等图像编码、音频编码、文本编码都到齐）。

`input.py` 用一个抽象基类 `InputHandler` 把这两种模式统一起来。它的核心契约只有一句话：**`receive()` 收到一路输入后，要么返回合并好的 payload（表示「够了，开算」），要么返回 `None`（表示「还不够，继续等」）**。

[sglang_omni/pipeline/stage/input.py:16-29](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/input.py#L16-L29) —— `InputHandler.receive` 的 docstring：「Returns merged payload if ready, None if still waiting.」外加一个 `cancel(request_id)` 用于 abort 时清理本阶段的等待状态。

`DirectInput` 是最简单的实现——单输入、不聚合，**来一个算一个**：

[sglang_omni/pipeline/stage/input.py:32-39](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/input.py#L32-L39) —— `receive` 直接 `return data`，`cancel` 是空操作。这也是 `Stage.__init__` 里 `input_handler or DirectInput()` 的默认值（runtime.py:110），即「不显式声明 fan-in 的阶段，默认就是直通」。

#### 4.2.2 核心流程

把输入处理抽象成一个可替换对象，带来一个关键好处：**Stage 的主路径不用关心是直通还是聚合**。统一入口在 `_receive_payload_from_stage`：

```text
收到一路 payload (request_id, from_stage, payload)
    │
    ▼
merged = self.input_handler.receive(request_id, from_stage, payload)
    │
    ├─ merged is None  → 什么都不做（还在等其他上游，AggregatedInput 场景）
    └─ merged is not None → _execute(merged)  ←推入 scheduler.inbox
```

无论 `input_handler` 是 `DirectInput` 还是 `AggregatedInput`，Stage 都只调一次 `receive()`，然后判断是不是 `None`。聚合的全部复杂度被关进了 `AggregatedInput` 内部。

#### 4.2.3 源码精读

Stage 调用 input_handler 的唯一位置：

[sglang_omni/pipeline/stage/runtime.py:483-512](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L483-L512) —— `_receive_payload_from_stage` 先做 abort 检查、打开 stream queue、发 `stage_input_received` 事件，然后核心两行：

```python
merged = self.input_handler.receive(request_id, from_stage, payload)
if merged is not None:
    ...
    await self._execute(merged)
```

这就是「输入处理 → 推入执行」的完整衔接。`_execute` 会把 merged 包装成 `IncomingMessage(type="new_request")` 推入 `scheduler.inbox`（详见 §4.4）。

#### 4.2.4 代码实践

**实践目标**：理解「默认直通」与「fan-in」是如何在配置层被选择的，而不是在 Stage 层。

**操作步骤**：

1. 打开 [sglang_omni/pipeline/stage_workers.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage_workers.py)。
2. 定位 `_construct_stage` 里「Build input handler」这一段。

**需要观察的现象**：你会看到这样的分支——如果 `spec.wait_for and spec.merge_fn`（即声明了 fan-in 来源与合并函数），就构造 `AggregatedInput(...)`；否则构造 `DirectInput()`。这正是 u2-l5 讲过的 StageConfig 字段（`wait_for` / `merge_fn` / `wait_for_fn`）在运行时的落点。

**预期结果**：确认「直通 vs 聚合」的决策完全由 `StageLaunchConfig`（来自声明式配置）决定，`Stage` 类本身对此一无所知。这把拓扑决策与运行时机制彻底解耦。

> 参考行：[sglang_omni/pipeline/stage_workers.py:691-709](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage_workers.py#L691-L709)。

#### 4.2.5 小练习与答案

**练习**：如果某个阶段误把一个 payload 重复 `receive` 两次，`DirectInput` 会怎样？

**参考答案**：`DirectInput.receive` 对每次调用都直接 `return data`，不做去重，也不记状态。所以同一个 payload 调两次，`_receive_payload_from_stage` 就会 `_execute` 两次，scheduler 会收到两个 `new_request`。去重/防重入不是 `DirectInput` 的职责——它假设上游协议保证每个 `(request_id, from_stage)` 的 payload 只到一次。状态化、防重入只在 `AggregatedInput` 里才需要（见 §4.3）。

---

### 4.3 AggregatedInput：fan-in 聚合

#### 4.3.1 概念说明

`AggregatedInput` 是 `InputHandler` 的另一种实现，处理 **fan-in**：一个阶段要等**多个上游**都到齐，再把它们的产物合并成一个 payload 才开算。

[sglang_omni/pipeline/stage/input.py:42-54](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/input.py#L42-L54) —— 构造参数有三个：`sources`（静态的全部可能上游名集合）、`merge`（合并函数 `dict[str, StagePayload] -> StagePayload`）、`expected_sources_fn`（可选的「请求感知」动态子集解析器）。

这里承接 u2-l5 的核心模式「**静态全集 + 请求感知子集**」：

- `sources`（对应配置里的 `wait_for`）列出所有**可能**的上游。
- `expected_sources_fn`（对应配置里的 `wait_for_fn`）在运行时根据本次请求内容，算出**真正**需要等哪几个上游。返回 `None` 表示「暂时还无法判断，先挂起等更多输入」。

为什么需要动态子集？因为不同请求可能走不同分支。比如一次纯文本请求不需要等图像编码上游，但一次多模态请求要等。静态 `wait_for` 列全集，动态 `wait_for_fn` 按请求裁剪。

#### 4.3.2 核心流程

`AggregatedInput.receive` 的判定逻辑（每个请求一份 pending 字典）：

```text
每收到一路 (request_id, from_stage, data):
  1. 若 from_stage 不在静态 sources 内 → 警告并忽略（防止拓扑错配）
  2. 把 data 存进 pending[request_id][from_stage]
  3. 解析本请求的 expected_sources（静态全集，或 expected_sources_fn 的动态结果）
       - 若 expected_sources_fn 返回 None → 还不能判定，返回 None（继续等）
  4. 校验：已到的来源不能超出 expected_sources（否则 raise ValueError）
  5. 若 pending 来源集合 == expected_sources → pop 并 merge，返回合并 payload
     否则 → 返回 None（继续等其他上游）
```

完成条件可以写成：

\[
\text{ready}(r) \iff \text{pending}(r) = \text{expected}(r)
\]

即「本请求**已收到的来源集合**」等于「本请求**期望的来源集合**」时才触发合并。等号两边都是集合相等。

#### 4.3.3 源码精读

完整的 `receive` 实现：

[sglang_omni/pipeline/stage/input.py:57-101](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/input.py#L57-L101) —— 注意几个关键点：

- 第 60-66 行：来源不在静态 `sources` 里就告警并忽略，保护拓扑一致性。
- 第 72-82 行：`expected_sources_fn` 的解析——**只在第一次解析出非 None 结果时缓存**到 `_expected_sources[request_id]`。也就是说，一个请求的「期望来源集」一旦确定就不再变；这避免了上游乱序到达时反复改判。
- 第 87-94 行：**严格校验**已收到来源必须是 expected 的子集，否则 `raise ValueError`。这能在配置写错（比如 `wait_for_fn` 返回了不在 `wait_for` 里的来源）时尽早炸出来，而不是悄悄 hang 住。
- 第 96-99 行：集合相等才合并并 `pop` 掉 pending 状态，调用 `self._merge(inputs)` 返回合并后的 payload。

`_normalize_expected_sources`（[input.py:103-136](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/input.py#L103-L136)）负责把 `wait_for_fn` 的返回值规范化成集合，并校验：非空、全是字符串、且都在静态 `sources` 内。动态解析器返回「静态拓扑之外的来源」会被直接拒绝。

`cancel`（[input.py:138-140](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/input.py#L138-L140)）在 abort 时清掉 pending 和 expected，避免内存泄漏。Stage 的 `_clear_request_state`（runtime.py:1501-1513）会在请求结束/中止时调用它。

#### 4.3.4 代码实践

**实践目标**：用最小代码感受 fan-in 的「集齐才合并」语义。

**操作步骤**（示例代码，可在本地 Python REPL 跑，无需 GPU）：

```python
# 示例代码：演示 AggregatedInput 的集齐语义
from sglang_omni.pipeline.stage.input import AggregatedInput

def merge(inputs):
    # inputs: {"img": <payload>, "txt": <payload>}
    return {"merged": inputs}

agg = AggregatedInput(
    sources={"img", "txt"},
    merge=merge,
    expected_sources_fn=None,   # 用静态全集 {"img","txt"}
)

print(agg.receive("req-1", "img", {"vec": [1, 2]}))  # → None（还没到齐）
print(agg.receive("req-1", "txt", {"toks": [9]}))    # → {'merged': {...}}（到齐，触发合并）
print(agg.receive("req-1", "img", {"vec": [3]}))     # → None（该请求已 pop，新轮次重新等）
```

**需要观察的现象**：第一次 `receive` 返回 `None`（只到一路），第二次返回合并结果（两路到齐），第三次因为 pending 已被 `pop` 又回到等待态。

**预期结果**：验证 `pending_sources == expected_sources` 才合并、合并后状态即清空的行为。**待本地验证**（取决于你本地是否能 `import sglang_omni`；若依赖未装，可只读 input.py 的逻辑手推结果）。

#### 4.3.5 小练习与答案

**练习 1**：如果 `expected_sources_fn` 第一次返回 `None`、第二次返回 `{"img"}`，会怎样？

**参考答案**：第一次返回 `None` 时，`receive` 走到第 84-85 行直接 `return None`，**不写入** `_expected_sources`，本请求继续挂起。第二次（再来一路输入时）再次解析，若返回 `{"img"}` 则缓存下来作为本请求固定的期望集。之后只要 `img` 到齐就合并。这种「先挂起、等能判定」的设计，是为了应对「要等某路上游到了才能判断还需不需要其他上游」的场景。

**练习 2**：`AggregatedInput` 为什么对「已收到来源超出 expected」直接 `raise`，而不是忽略？

**参考答案**：因为这种情形几乎一定是**拓扑/配置写错**（比如静态 `wait_for` 漏写了一个上游，或 `wait_for_fn` 算错了子集）。忽略会表现为「请求莫名 hang 住」，极难排查；直接 `raise ValueError` 能在出错当下暴露根因（input.py:89-94）。这是「快速失败（fail-fast）」优于「静默错误」的典型取舍。

---

### 4.4 scheduler 桥接：inbox/outbox 契约与跨线程协作

#### 4.4.1 概念说明

前三个模块都在讲「Stage 怎么把活儿**收**进来」。本模块讲最关键的一跳：**Stage 怎么把活儿交给 scheduler，又怎么把 scheduler 算完的结果送出去**。这一跳就是「scheduler 桥接」。

桥接的契约非常小。所有 scheduler 都暴露：

```python
class Scheduler:
    inbox: Queue[IncomingMessage]    # Stage → scheduler：要干的活
    outbox: Queue[OutgoingMessage]   # scheduler → Stage：算完的结果
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def abort(self, request_id: str) -> None: ...
```

两种消息类型定义在 [sglang_omni/scheduling/messages.py:10-23](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/messages.py#L10-L23)：

- `IncomingMessage`：`type ∈ {"new_request", "stream_chunk", "stream_done"}`，是 Stage 喂给 scheduler 的输入。
- `OutgoingMessage`：`type ∈ {"result", "stream", "error"}`，是 scheduler 吐回 Stage 的输出。

这个契约为什么是「关键不变量」？因为它把 **asyncio 线程（Stage）** 和 **阻塞计算线程（scheduler）** 解耦：

- Stage 永远只做 `scheduler.inbox.put(...)` 和 `scheduler.outbox.get(...)`，**从不直接调用模型前向**。
- scheduler 永远只做 `inbox.get(...)` 和 `outbox.put(...)`，**从不碰 ZMQ / relay / asyncio**。

于是 scheduler 可以是任意阻塞实现（SGLang 那种重循环、或 `SimpleScheduler` 的简单循环），都不影响 Stage 的 IO 逻辑；反之亦然。这就是「不分支于 scheduler 类型」能成立的物理基础。

#### 4.4.2 核心流程

桥接分「入」「出」两半：

**入：Stage → scheduler.inbox**（三种 type）

```text
_on_submit(payload)                 ─┐
_receive_payload_from_stage(merged) ─┼─► _execute(payload)
                                       │     └─ scheduler.inbox.put(IncomingMessage(type="new_request", data=payload))

_on_stream_chunk(...)  → _route_stream_item(item)
                              └─ scheduler.inbox.put(IncomingMessage(type="stream_chunk", data=item))

_receive_stream_signal(..., is_done=True)
        └─ scheduler.inbox.put(IncomingMessage(type="stream_done"))
```

注意：**三种可执行信号（新请求、流 chunk、流结束）统统走 `scheduler.inbox`**，区别只在 `IncomingMessage.type`。scheduler 自己根据 type 决定怎么处理（`SimpleScheduler` 只认 `new_request`，流式调度器还认 chunk/done）。

**出：scheduler.outbox → Stage**（由 `_drain_outbox` 协程搬运）

```text
_drain_outbox_external():
    while running or outbox 非空:
        out = outbox.get(timeout=0.1)   ← 用线程池阻塞取，不卡 asyncio
        ├─ type=="result" → _route_result → 下游 DataReadyMessage / 终态 CompleteMessage
        ├─ type=="stream" → _send_stream_to_target（local_object / 直接 CUDA IPC / relay）
        └─ type=="error"  → _send_failure → CompleteMessage(success=False)
```

#### 4.4.3 源码精读

先看「推入 inbox」的总入口 `_execute`：

[sglang_omni/pipeline/stage/runtime.py:799-814](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L799-L814) —— 关键两步：若自己是 TP leader 且 scheduler 需要 work fanout，先把 payload fanout 给 follower；然后 `self.scheduler.inbox.put(IncomingMessage(request_id=..., type="new_request", data=payload))`。**所有 new_request 都从这里进 scheduler**。

流式 chunk 与流结束同样推 inbox，但 type 不同：

[sglang_omni/pipeline/stage/runtime.py:794-797](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L794-L797) —— `_route_stream_item` 把 `StreamItem` 包成 `IncomingMessage(type="stream_chunk", data=item)` 推入 inbox。

[sglang_omni/pipeline/stage/runtime.py:762-781](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L762-L781) —— `_receive_stream_signal` 在收到 `stream_done` 时 `put_done` 到 stream queue，并推一条 `IncomingMessage(type="stream_done")` 进 inbox。

再看「从 outbox 取出」的 `_drain_outbox`：

[sglang_omni/pipeline/stage/runtime.py:935-985](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L935-L985) —— 它据 `_owns_external_io` 分两条路：leader/single 走 `_drain_outbox_external`（真发下游），follower 走 `_drain_outbox_follower`（只清状态，不发外部流量）。注意第 944-949 行用 `loop.run_in_executor(None, lambda: self.scheduler.outbox.get(timeout=0.1))` 在**线程池里阻塞取**队列——因为 `outbox` 是阻塞 `queue.Queue`，直接 `await` 会卡死 asyncio 循环。

`_route_result`（[runtime.py:1007-1067](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L1007-L1067)）是「结果去哪儿」的决策点：调用 `self.get_next(request_id, result)`，返回 `None` 就是**终态**→ 给 Coordinator 发 `CompleteMessage`（承接 u2-l4 的终态收集）；返回阶段名就 `_send_to_stage` 把结果发下游。

`_send_to_stage`（[runtime.py:1099-1182](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L1099-L1182)）展示了 Stage 怎么挑传输方式——这正是 u6-l1 要详讲的「通信路由」，这里只需看到三档：

1. **同进程（`same_process_targets`）**：经 `LocalStageDispatcher` 直接传 Python 对象引用（`local_object`），零拷贝。接收方必须只读（见 [local_dispatch.py:9-15](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/local_dispatch.py#L9-L15)）。
2. **同节点跨 GPU（`can_use_direct_cuda_ipc` 且 payload 含 CUDA tensor）**：直接 CUDA IPC（`torch_cuda_ipc`），不走 relay 池。
3. **其他**：relay 后端（cuda_ipc / shm / nccl / nixl / mooncake，由 `CommRouter` 选）。

为了印证「同一套 inbox/outbox 表面」，看一眼 `SimpleScheduler` 的循环：

[sglang_omni/scheduling/simple_scheduler.py:1-8](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L1-L8) 与 [sglang_omni/scheduling/simple_scheduler.py:45-46](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L45-L46) —— 模块注释直言「Same inbox/outbox interface as OmniScheduler so Stage doesn't need branching」，`self.inbox`/`self.outbox` 就是两个 `queue.Queue`。`OmniScheduler` 内部再复杂（复用 SGLang 的 prefill/decode/KV cache），对外也是这两个队列，所以 Stage 用同一份 `_drain_outbox` 就能驱动它。

最后，scheduler 在独立线程启动的事实：

[sglang_omni/pipeline/stage/runtime.py:153-195](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L153-L195) —— `start()` 里若 `self.scheduler is not None`，就起一个名为 `scheduler-{name}` 的 daemon 线程跑 `self.scheduler.start()`（阻塞），并在线程里 `torch.cuda.set_device` 绑定 GPU、`_set_active_stage` 绑定 profiler 的 active-stage。如果 scheduler 线程崩溃，会经 `asyncio.run_coroutine_threadsafe` 通知主循环走 `_handle_scheduler_crash`（runtime.py:1515-1534）把在途请求全部 fail 掉。

#### 4.4.4 代码实践

> 这也是本讲义规格里指定的核心实践任务。

**实践目标**：在 `runtime.py` 中定位「处理 `DataReadyMessage`」与「fan-in」的代码，并用自己的话解释「把所有可执行工作推入 `scheduler.inbox`」为什么是关键不变量。

**操作步骤**：

1. 在 [runtime.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py) 搜 `DataReadyMessage`，定位三处：
   - `_handle_message` 里的分派（runtime.py:301-302）。
   - `_schedule_receive_task`（runtime.py:310-338）——它据 `msg.is_done` / `msg.error` / `msg.chunk_id` 把同一个 `DataReadyMessage` 分到「stream signal」「stream chunk」「payload」三条处理路径。
   - `_on_data_ready`（runtime.py:381-434）——读 relay payload 后调 `_receive_payload_from_stage`。
2. 在 `_receive_payload_from_stage`（runtime.py:504）找到 fan-in 的入口 `self.input_handler.receive(...)`，确认它返回非 `None` 时才 `_execute`。
3. 在 `_execute`（runtime.py:812-814）确认「推入 inbox」那一行。
4. 写一句话回答下面的「预期结果」。

**需要观察的现象**：你会看到，无论数据来自 Coordinator 的 `SubmitMessage`、来自上游的普通 payload、来自流式 chunk、还是来自流结束信号，**它们最终都汇聚到 `scheduler.inbox.put(IncomingMessage(...))` 这一个出口**（分别在 `_execute`、`_route_stream_item`、`_receive_stream_signal`）。

**预期结果（一句话解释）**：把所有可执行工作统一推入 `scheduler.inbox`，让 Stage 的 asyncio IO 路径与 scheduler 的阻塞计算路径之间**只通过两个线程安全队列耦合**——这样 scheduler 可以换成任意实现（`SimpleScheduler` / `OmniScheduler` / 流式调度器）而 Stage 的收发代码一行都不用改，这正是「Stage 不分支于 scheduler 类型」这条不变量成立的物理基础；同时也天然把「收消息（快、IO 密集）」和「算模型（慢、GPU 密集）」放到两个线程，互不阻塞。

> 说明：本实践为「源码阅读型实践」，不需要 GPU，也不需要启动服务。

#### 4.4.5 小练习与答案

**练习 1**：`_drain_outbox_external` 为什么用 `loop.run_in_executor(... outbox.get(timeout=0.1) ...)` 而不是直接 `await outbox.get()`？

**参考答案**：因为 `scheduler.outbox` 是阻塞式 `queue.Queue`（simple_scheduler.py:46），它的 `get()` 会**阻塞当前线程**。若在 asyncio 协程里直接调用，会卡死整个事件循环，导致 Stage 无法收新消息、无法处理 abort。用 `run_in_executor` 把阻塞 get 丢到线程池，协程本身只 `await` 这个 future，事件循环就能继续转。`timeout=0.1` 保证线程池里的 get 会周期性返回，便于在 `self._running=False` 时及时退出。

**练习 2**：scheduler 线程崩了，Stage 会怎样？

**参考答案**：scheduler 线程的 `except` 块（runtime.py:178-186）捕获异常后，经 `asyncio.run_coroutine_threadsafe(self._handle_scheduler_crash(exc), loop)` 把异常投递回主循环。`_handle_scheduler_crash`（runtime.py:1515-1534）会：记录错误、对所有在途且未 abort 的请求调 `scheduler.abort` 与 `_send_failure`（给 Coordinator 发失败完成）、清理 comm、关闭控制平面。随后 `run()` 的 finally 检查 `_scheduler_crash_error` 并 `raise`，整个 Stage 进程退出，由上层 `MultiProcessPipelineRunner` 监控处理。承接 u2-l4「Coordinator 收到失败完成即 fail-fast 并广播 abort」。

**练习 3**：为什么 `_route_stream_item`（流式 chunk）和 `_execute`（新请求）都往 `scheduler.inbox` 推，却用不同的 `IncomingMessage.type`？

**参考答案**：因为不同 scheduler 对这三类信号的敏感度不同。`SimpleScheduler` 只认 `new_request`（simple_scheduler.py:222、270），忽略 chunk/done；而流式调度器（如 `Code2WavScheduler`）必须靠 `stream_chunk` 累积、靠 `stream_done` flush。用 `type` 区分让「同一个 inbox」既能服务于「一次性算完」的阶段，也能服务于「边收边算」的流式阶段，而不需要 Stage 为流式阶段单独建一条通道。这正是「不分支于 scheduler 类型」在流式场景下的体现。

---

## 5. 综合实践

把本讲四个模块串起来，做一次「**给一个假想阶段手工走一遍数据流**」的纸面推演。

**场景**：假设有一个聚合阶段 `mm_aggregate`，它的 `wait_for = {"image_encoder", "audio_encoder"}`，`merge_fn` 把两路 embedding 拼成一个字典。上游 image_encoder、audio_encoder 都在**同进程**（colocated）。请求 `req-7` 是一个图文音频多模态请求。

**任务**：

1. **画出从上游到 scheduler.inbox 的完整路径**。提示：同进程上游 → 经 `LocalStageDispatcher.send_payload`（[local_dispatch.py:36-45](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/local_dispatch.py#L36-L45)）→ 调 `Stage.receive_local_payload` → `_receive_payload_from_stage`（runtime.py:483-512）→ `AggregatedInput.receive`（input.py:57-101）→（集齐后）`_execute` → `scheduler.inbox.put`。
2. **写出第一路（image）到达时的返回值**：`AggregatedInput.receive` 返回 `None`（还没到齐），`_execute` **不会被调用**，scheduler 不会收到任何东西。
3. **写出第二路（audio）到达时的返回值**：返回 merge 后的 payload，`_execute` 把它包成 `IncomingMessage(type="new_request")` 推入 inbox。
4. **标出 scheduler 算完后结果的去向**：`scheduler.outbox` → `_drain_outbox_external` → `_route_result` → `get_next` 返回下游阶段名（非终态）→ `_send_to_stage` → 因为下游也同进程，走 `local_object` 直传（runtime.py:1099-1130）。
5. **回答一个反思题**：如果在这个场景里把 `image_encoder` 误配置成不在 `mm_aggregate` 的 `wait_for` 集合里，会在哪一行、以什么形式报错？

**预期结果**：

- 路径 1-4 应当形成闭环：上游 payload → input_handler 聚合 → scheduler.inbox → scheduler 计算 → scheduler.outbox → 下游。
- 反思题答案：在 `AggregatedInput.receive` 的第 60-66 行，`from_stage="image_encoder"` 不在 `sources` 里，会被记一条 warning 并 `return None`；结果就是两路永远凑不齐，请求**静默 hang 住**（注意：来源未声明是 warning 而非 raise，与「超出 expected 才 raise」不同——这提醒你写 fan-in 配置时要确保 `wait_for` 列全所有可能上游）。这也反向说明了 §4.3「超出 expected 就 raise」的设计价值：它至少能让另一类错误（子集算错）尽早炸出来。

> 本实践不需要 GPU，是「源码阅读 + 纸面推演」型综合实践。

## 6. 本讲小结

- `Stage` 是一个 **IO 外壳**：管控制平面消息、数据平面 relay 读写、fan-in 聚合、流式路由、abort/profiling，**把所有计算 dispatch 给 scheduler**（runtime.py 模块注释 1-8）。
- 核心不变量：**Stage 的代码不因 scheduler 类型而分支**。`SimpleScheduler`、`OmniScheduler`、流式调度器都暴露同一套 `inbox`/`outbox`/`start`/`stop`/`abort` 表面，所以同一份 Stage 代码能驱动它们全部。
- 输入处理被抽象成可替换的 `InputHandler`：`DirectInput`（单输入直通，默认）与 `AggregatedInput`（fan-in 聚合）。Stage 只调一次 `receive()`，`None` 表示继续等、非 `None` 表示开算（input.py:16-101）。
- `AggregatedInput` 承接 u2-l5 的「静态全集 + 请求感知子集」：`sources`/`wait_for` 列全集，`expected_sources_fn`/`wait_for_fn` 按请求裁剪，集合相等才合并（input.py:96-99）。
- **scheduler 桥接是关键一跳**：所有可执行信号（`new_request`/`stream_chunk`/`stream_done`）统一推入 `scheduler.inbox`，scheduler 在独立线程算完把结果放进 `scheduler.outbox`，`_drain_outbox` 再搬回 asyncio 线程翻译成控制消息。两个线程安全队列是 Stage 与 scheduler 的唯一耦合点。
- Stage 跑在 asyncio 线程做 IO，scheduler 跑在独立线程做阻塞计算；阻塞的 `outbox.get` 经 `run_in_executor` 丢线程池，避免卡死事件循环。

## 7. 下一步学习建议

本讲把「阶段内部」的 IO 外壳讲透了，接下来有三条自然的延伸：

1. **向「下」看控制平面与数据平面的实现**：本讲多次出现 `control_plane.recv()`、`relay`、`CommRouter`。下一讲 **u3-l2（控制平面与 ZMQ 消息）** 会拆开 ZMQ PUSH/PULL、PUB/SUB 与 msgpack 序列化；**u3-l3（Relay 数据平面与传输后端）** 会拆开 CUDA IPC / shm / nixl / mooncake 等后端。
2. **向「内」看 scheduler**：本讲把 scheduler 当黑盒。**u4-l1（调度器接口与 SimpleScheduler）** 会展开 `inbox.get → compute → outbox.put` 的循环与 `batch_compute_fn`；**u4-l2（OmniScheduler 与 SGLang 后端）** 会展开它如何复用 SGLang 的 prefill/decode/KV cache。
3. **想直接动手接新模型**：可以先跳到 **u7-l5（综合实战：新增一个模型家族）**，但建议先读 u3-l4（进程拓扑与多进程 Runner），理解本讲的 `Stage` 是如何被 `stage_workers._construct_stage` 装配出来、又如何被 `MultiProcessPipelineRunner` 拉进子进程的。

一个推荐的巩固练习：在读完 u3-l2 之后，回到本讲的 `_handle_message`（runtime.py:296-308），试着把每一条 `isinstance` 分支对应到一种 ZMQ 上收到的控制消息，验证你能在「消息字节 → scheduler.inbox」之间画出完整闭环。
