# 调度器接口与 SimpleScheduler

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 SGLang-Omni 对所有调度器统一要求的「五件套」接口：`inbox` / `outbox` / `start` / `stop` / `abort`，并解释为什么必须是这一套。
- 区分调度器层的两类消息：进入调度器的 `IncomingMessage` 与离开调度器的 `OutgoingMessage`，以及它们各自的 `type` 取值。
- 用 `SimpleScheduler` 写一个最小 `compute_fn`（例如把字符串转大写），并解释结果如何经 `outbox` 被送回 Stage、再路由到下游。
- 判断 `batch_compute_fn` 对「非 AR（非自回归）阶段」何时有用、何时无意义。
- 说明 `abort` 在预处理、编码器、聚合这类「无 KV 缓存」的非 AR 阶段里为什么能立刻清理，而不是像 AR 阶段那样要延迟回收。

本讲是 u4 单元「调度器与 ModelRunner 执行机制」的第一篇，承接 u3-l1「Stage 抽象与 IO 外壳」：Stage 只负责搬运与 IO，真正的计算被交给调度器，本讲就打开调度器这个黑盒的最简单一种实现。

## 2. 前置知识

- **进程内队列（`queue.Queue`）**：Python 标准库提供的「生产者—消费者」容器。一个线程往里 `put`，另一个线程 `get`，线程安全。本讲里 `inbox` 收请求、`outbox` 发结果，都是这种队列。
- **AR（autoregressive，自回归）与非 AR 阶段**：AR 阶段（如 talker、tts_engine）需要逐 token 生成、维护 KV 缓存、做 batching，复杂且昂贵；非 AR 阶段（如 preprocessing、image/audio encoder、decode、code2wav 的某些环节）往往「来一个算一个」，或「攒一小批算一次」，不需要 KV 缓存。`SimpleScheduler` 就是为后者设计的轻量调度器。
- **Stage 与调度器的耦合方式（来自 u3-l1）**：Stage 把所有可执行信号（`new_request` / `stream_chunk` / `stream_done`）`put` 进 `scheduler.inbox`；调度器在自己的线程里算完后把结果 `put` 进 `scheduler.outbox`；Stage 的 asyncio 循环再从 outbox 里把结果取走路由下游。两者**只靠这两个队列耦合**，这是「Stage 不因调度器类型而分支」这条不变量的物理基础。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它讲什么 |
| --- | --- | --- |
| [sglang_omni/scheduling/messages.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/messages.py) | 调度器层的「进出消息」类型定义 | `IncomingMessage` / `OutgoingMessage` 两个 dataclass |
| [sglang_omni/scheduling/simple_scheduler.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py) | 最简单的非 AR 调度器实现 | 主循环、批处理、并发、abort |
| [sglang_omni/scheduling/types.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/types.py) | 与上游 SGLang / OmniScheduler 相关的类型 | 对比：为什么 SimpleScheduler **不需要**这些重类型 |
| [sglang_omni/pipeline/stage/runtime.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py) | Stage 如何与调度器对接 | `inbox.put`、`outbox` 消费、`start` / `stop` / `abort` 的调用点 |
| [sglang_omni/models/qwen3_omni/stages.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/stages.py) | Qwen3-Omni 的真实阶段工厂 | `batch_compute_fn` 的生产级用法 |
| [tests/unit_test/pipeline/helpers.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/pipeline/helpers.py) | `run_scheduler` 测试夹具 | 如何在一个线程里跑调度器并取回结果 |

## 4. 核心概念与源码讲解

### 4.1 调度器统一接口与消息类型

#### 4.1.1 概念说明

在 SGLang-Omni 里，「调度器（scheduler）」是一个抽象的角色，而不是某个具体类。它对 Stage 承诺了**一组固定接口**：

- `inbox`：Stage 往里塞「要做的事」。
- `outbox`：调度器往里塞「算完的结果」。
- `start()`：在一个**专用线程**里阻塞运行主循环。
- `stop()`：把 `_running` 标志置假，让主循环退出。
- `abort(request_id)`：标记某个请求被取消，并尽量清理它。

只要一个对象实现了这五件套，Stage 就能驱动它，**完全不需要知道**它是 `SimpleScheduler`、`OmniScheduler` 还是某个流式调度器。这正是 u3-l1 所说「Stage 不因 scheduler 类型而分支」的实现基础。

与之配套的是两个跨调度器共享的消息类型，定义在 [messages.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/messages.py) 里。模块顶部注释明确写道：「Lightweight scheduler message types shared across scheduling backends」（跨调度后端共享的轻量消息类型）。

#### 4.1.2 核心流程

消息的流向可以画成下面这张图（调度器是一个「变换盒」）：

```
   Stage（asyncio 线程）                         Stage（asyncio 线程）
          │ put                                         ▲ get / drain
          ▼                                             │
   ┌─────────────── IncomingMessage ─────────────┐   ┌──── OutgoingMessage ────┐
   │ request_id, type ∈                          │   │ request_id, type ∈       │
   │   {new_request, stream_chunk, stream_done}, │   │   {result, stream, error},│
   │   data                                      │   │   data, target, metadata │
   └──────────────────────┬──────────────────────┘   └────────────▲─────────────┘
                          │ inbox.put                               │ outbox.put
                          ▼                                         │
                     ┌──────────────── 调度器主循环 ───────────────────┐
                     │  while running:                                   │
                     │      msg = inbox.get()          # 取一个待办       │
                     │      ... compute_fn(msg.data) ... # 算（可批可并发）│
                     │      outbox.put(OutgoingMessage(type="result"))  │
                     └───────────────────────────────────────────────────┘
```

要点：

- `IncomingMessage.type` 只有三种：`new_request`（新请求）、`stream_chunk`（流式的一块输入）、`stream_done`（流式输入结束）。它们描述的是**进入调度器的事件**。
- `OutgoingMessage.type` 也只有三种：`result`（这个请求算完了，这是最终结果）、`stream`（流式输出的一块）、`error`（这个请求失败了）。它们描述的是**离开调度器的事件**。
- `OutgoingMessage` 比 `IncomingMessage` 多两个字段：`target`（流式 chunk 要送到哪个下游 stage）和 `metadata`（附加元数据），因为输出需要被路由，而输入只需要被处理。

#### 4.1.3 源码精读

消息类型本身就是两个极简 dataclass：

[sglang_omni/scheduling/messages.py:10-23](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/messages.py#L10-L23) 定义了 `IncomingMessage`（三字段：`request_id` / `type` / `data`）与 `OutgoingMessage`（五字段：多了 `target` 与 `metadata`）。`type` 用 `Literal[...]` 限定取值，既起到文档作用，也便于类型检查器发现拼写错误。

再看 `SimpleScheduler` 在构造时就建好了这两个队列：

[sglang_omni/scheduling/simple_scheduler.py:45-47](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L45-L47) — `self.inbox` 与 `self.outbox` 都是普通的 `queue.Queue`，外加一个布尔属性 `requires_tp_work_fanout = True`。后者是 Stage 用来判断「是否需要把工作扇出给 TP follower」的开关（详见 u6-l6），它出现在这里正说明：**即便是这么简单的调度器，也要满足 Stage 对调度器的全部约定**。

作为对比，[types.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/types.py) 里那一大堆类型——`SchedulerRequest`、`SchedulerOutput`、`RequestOutput`、`ModelRunnerOutput`、`ARRequestData`——是给 `OmniScheduler` / 上游 SGLang 用的重类型，它们带着 `input_ids`、`attention_mask`、KV 槽位、logprob 等自回归专属字段。`SimpleScheduler` **完全不碰**这些（它顶多只 import 了 `messages.py` 里的两个轻量消息），这正是它「轻」的来源。

#### 4.1.4 代码实践

**实践目标**：亲手验证「调度器只要满足五件套，Stage 就能用它」，并通过测试夹具体会「往 inbox 喂消息、从 outbox 收结果」的完整往返。

**操作步骤**（源码阅读型实践，无需 GPU）：

1. 打开 [tests/unit_test/pipeline/helpers.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/pipeline/helpers.py#L53-L70)，阅读 `run_scheduler` 函数。它就是一个最小驱动机：起一个后台线程跑 `scheduler.start()`，往 `scheduler.inbox.put(message)`，再 `scheduler.outbox.get(timeout=2.0)` 收结果。
2. 在仓库根目录运行（这条命令只导入 `SimpleScheduler`，不依赖 torch）：

   ```bash
   python -m pytest tests/unit_test/pipeline/test_simple_scheduler_concurrent.py -q
   ```

**需要观察的现象**：每个测试都用 `run_scheduler` 把几条 `IncomingMessage("req-x", "new_request", ...)` 推进 `inbox`，然后断言从 `outbox` 拿到的 `OutgoingMessage` 的 `request_id` 与 `data`。

**预期结果**：测试通过。这证明 `SimpleScheduler` 的对外契约就是「`inbox.put` 进去 → `outbox.put` 出来」，与 Stage 的耦合方式完全一致。

#### 4.1.5 小练习与答案

**练习 1**：`IncomingMessage.type` 和 `OutgoingMessage.type` 各有哪些取值？为什么输出比输入多 `target` 字段？

**参考答案**：输入 `type` ∈ {`new_request`, `stream_chunk`, `stream_done`}；输出 `type` ∈ {`result`, `stream`, `error`}。输入只需要被「处理」，不需要知道去哪；而输出里的 `stream` chunk 需要被路由到特定下游 stage，所以 `OutgoingMessage` 多了 `target`（目标 stage）和 `metadata`（附加路由信息）。

**练习 2**：`SimpleScheduler` 是否 import 了 `types.py` 里的 `SchedulerRequest` / `ARRequestData`？

**参考答案**：没有。[simple_scheduler.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L20) 只从 `messages` import 了 `IncomingMessage` / `OutgoingMessage` 两个轻量类型。那些重类型是 `OmniScheduler` 等自回归调度器的专属。

---

### 4.2 SimpleScheduler 主循环：inbox.get → compute → outbox.put

#### 4.2.1 概念说明

`SimpleScheduler` 是非 AR 阶段的默认调度器，类文档直白地写着它的定位：

> Process requests one at a time via a callable. Supports sync and async callables for `new_request` messages only. Streaming stages should provide a dedicated scheduler implementation (for example `Code2WavScheduler`) rather than rely on SimpleScheduler.

换句话说，它的核心心智模型就是一句话：**收到一个 `new_request`，调用你给的 `compute_fn(data)`，把返回值塞进 `outbox`**。没有 KV 缓存、没有 token 级调度、没有 prefill/decode 之分。它的存在是为了让 preprocessing、encoder、decode、聚合这类「来一个算一个」的阶段能用同一套 Stage 骨架。

模块顶部的注释甚至把它总结成一行的公式（[simple_scheduler.py:1-8](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L1-L8)）：`inbox.get() → run function → outbox.put()`。注意：这条注释描述的是「最简模式」；后面会看到 `batch_compute_fn` / `max_concurrency` 是在它之上叠加的可选能力，不改变这个骨架。

#### 4.2.2 核心流程

最简（串行）主循环的伪代码：

```text
start():
    running = True
    loop = new_event_loop()        # 为了 await 异步 compute_fn
    while running:
        msg = next_message()       # 优先取 pending，否则 inbox.get(timeout=0.1)
        if msg is None: continue
        if msg.type == "new_request":
            if consume_if_aborted(msg.request_id): continue   # 已中止则跳过
            batch = collect_batch(msg)                         # 默认就 [msg]
            run_batch(batch, loop)                             # 调 compute_fn，发 outbox
```

`run_batch` 在最简模式下退化为对每条消息调用 `_run_single`：

```text
_run_single(msg, loop):
    if consume_if_aborted(msg.request_id): return     # 算之前再查一次
    result = compute_fn(msg.data)                      # 你的函数
    if iscoroutine(result): result = loop.run_until_complete(result)   # 支持异步
    if consume_if_aborted(msg.request_id): return     # 算完再查一次（防中途 abort）
    emit_result(msg.request_id, result, outbox)       # outbox.put(OutgoingMessage(type="result"))
```

可以看到「abort 检查」被安排在计算前、计算后两个点（计算中也可能抛异常，异常路径也会先查一次 abort），这是为了让一次已经发出的 abort 尽量不要浪费一次无谓的计算或发出多余结果。

#### 4.2.3 源码精读

`start()` 根据是否开了并发，分两条路：

[sglang_omni/scheduling/simple_scheduler.py:206-212](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L206-L212) —— `start()` 把 `_running` 置真，然后根据 `_max_concurrency` 走 `_start_serial` 或 `_start_concurrent`。它**阻塞当前线程**（docstring 明说 "blocks the thread"），所以 Stage 一定是在专用线程里调它的（见 4.2.4 引用的 runtime 代码）。

串行主循环：

[sglang_omni/scheduling/simple_scheduler.py:214-242](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L214-L242) —— 新建一个事件循环（用于 `run_until_complete` 跑异步 `compute_fn`）；循环里 `next_message()` 取消息，若是 `new_request` 就先查 abort，再 `collect_batch` 收一个批（默认退化为单条），最后 `run_batch`。若 `compute_fn` 抛异常，则捕获后为批里每条未中止的消息发一个 `error`，且**不终止循环**——一个请求失败不能把整个 stage 拖垮。

单条执行与结果发射：

[sglang_omni/scheduling/simple_scheduler.py:156-171](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L156-L171) 与 [sglang_omni/scheduling/simple_scheduler.py:132-142](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L132-L142) —— `_run_single` 调 `self._fn(msg.data)`，支持同步或返回协程；`_emit_result` 把结果包成 `OutgoingMessage(type="result")` 放进 `outbox`。这就是「结果如何通过 outbox 送回 Stage」的源头。

那么 Stage 这边怎么消费 `outbox`？看 runtime：

[sglang_omni/pipeline/stage/runtime.py:941-985](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L941-L985) —— Stage 的 `_drain_outbox_external` 在 asyncio 循环里用 `run_in_executor` 把 `scheduler.outbox.get(timeout=0.1)` 拉到事件循环线程，再按 `out.type` 分派：`result` → `_route_result` 路由到下游 stage；`stream` → 送到目标或 coordinator；`error` → `_send_failure`。**调度器只负责往 outbox 放，路由是 Stage 的事**——职责干净分离。

而 Stage 把待办推给调度器的位置在 [sglang_omni/pipeline/stage/runtime.py:776](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L776)（以及 795、812）—— 多处 `self.scheduler.inbox.put(...)`，把 `new_request` / `stream_chunk` / `stream_done` 推进去。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：用 `SimpleScheduler` 实现一个假想的「uppercase 阶段」——`compute_fn` 把输入字符串转大写，并说明结果如何经 outbox 回到 Stage。

**操作步骤**：

1. 新建一个临时脚本 `play_uppercase.py`（注意：这是**示例代码**，不是仓库已有文件，不要提交；仓库里没有这个文件）：

   ```python
   # 示例代码：最小 SimpleScheduler 驱动实验
   import threading
   from sglang_omni.scheduling.simple_scheduler import SimpleScheduler
   from sglang_omni.scheduling.messages import IncomingMessage

   # 1) 定义 compute_fn：吃一个 payload，吐一个结果
   def to_upper(payload):           # payload 就是 IncomingMessage.data
       return payload.upper()

   # 2) 建调度器（最简串行模式）
   sched = SimpleScheduler(to_upper)

   # 3) 复刻 Stage 的做法：起专用线程跑 start()
   t = threading.Thread(target=sched.start, daemon=True)
   t.start()

   # 4) 复刻 Stage 的做法：把待办推进 inbox
   sched.inbox.put(IncomingMessage("req-1", "new_request", "hello omni"))

   # 5) 从 outbox 取回结果（这正是 Stage._drain_outbox 在做的事）
   out = sched.outbox.get(timeout=2.0)
   print(out.request_id, out.type, out.data)   # 期望：req-1 result HELLO OMNI

   sched.stop(); t.join(timeout=1.0)
   ```

2. 在已安装 `sglang-omni` 的环境里运行：

   ```bash
   python play_uppercase.py
   ```

**需要观察的现象**：脚本应当打印 `req-1 result HELLO OMNI`。这串过程里，`to_upper` 被 `_run_single` 调用，结果被 `_emit_result` 包成 `OutgoingMessage(type="result", data="HELLO OMNI")` 放进 `outbox`；主线程的 `outbox.get()` 拿到的就是它——**和 Stage 的 `_drain_outbox_external` 做的事一模一样**。

**预期结果**：输出 `req-1 result HELLO OMNI`。如果看到长时间卡住，多半是 `compute_fn` 抛了异常导致 `outbox` 里放的是 `type="error"`（可用 `sched.outbox.get_nowait()` 排查），或忘了 `start()` 线程。

> 待本地验证：以上输出基于源码逻辑推断，请以本地实际运行结果为准。

**一句话回答实践任务**：`compute_fn` 的返回值在 `_emit_result` 里被包成 `OutgoingMessage(type="result")` 放进 `self.outbox`；Stage 的 `_drain_outbox_external` 从 `outbox.get()` 取到它，按 `out.type == "result"` 走 `_route_result`，把结果通过控制平面/数据平面送到声明的下游 stage。调度器只管「算完放进 outbox」，路由完全交给 Stage。

#### 4.2.5 小练习与答案

**练习 1**：`start()` 为什么必须在一个**专用线程**里被调用，而不是直接在 Stage 的 asyncio 循环里调？

**参考答案**：因为 `start()` 是阻塞的（`while self._running` 死循环），若直接在 asyncio 线程里调，会卡死整个 stage 的事件循环，无法收消息、无法 drain outbox。runtime 把它放进名为 `scheduler-<stage>` 的 daemon 线程（见 [runtime.py:188-193](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L188-L193)），调度器线程与 asyncio 线程**只通过 `inbox` / `outbox` 两个队列通信**，互不阻塞。

**练习 2**：如果 `compute_fn` 抛异常，整个 stage 会挂掉吗？

**参考答案**：不会。[simple_scheduler.py:229-240](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L229-L240) 把异常捕获、记录日志，并仅对这条请求发一个 `type="error"` 的 `OutgoingMessage`，循环继续。这是「per-request 错误隔离」。

---

### 4.3 batch_compute_fn：什么时候对非 AR 阶段有用

#### 4.3.1 概念说明

最简模式下 `SimpleScheduler` 是「来一个算一个」。但有些非 AR 阶段，尤其是 GPU 编码器（image/audio encoder），**逐个算效率很低**：每次前向只处理 batch=1，GPU 利用率低。这类计算有「批处理收益」——把多个请求的输入拼成一个大 batch 一起前向，单次开销被多个请求摊薄。

`batch_compute_fn` 就是为这种场景准备的：你额外提供一个「吃一个 payload 列表、返回一个结果列表」的函数，调度器会**尽量攒一小批**（不超过 `max_batch_size`，或不超过 `max_batch_cost` 的成本预算），等满或等到 `max_batch_wait_ms` 后一次性调用它。

注意它与「AR 阶段的 batching」是两回事：AR 阶段的 batching 由 SGLang 的 prefill/decode 调度器做，涉及 KV 缓存、连续 batching、token 级调度，非常复杂（见 u4-l2）；`SimpleScheduler` 的 `batch_compute_fn` 只是非 AR 阶段「无状态、可整批前向」的简单批处理。

> 重要约束：`batch_compute_fn` 与 `max_concurrency > 1` 互斥（[simple_scheduler.py:62-65](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L62-L65) 构造期就检查并抛 `ValueError`）。因为前者是「攒批」，后者是「多 worker 并发各算各的」，两种加速模型逻辑上冲突。

#### 4.3.2 核心流程

批处理逻辑的核心是「先来一条，再尽量多收几条」：

```text
collect_batch(first_msg):
    batch = [first_msg]
    if batch_fn is None or max_batch_size <= 1: return batch   # 没开批处理
    deadline = now + max_batch_wait_s
    while len(batch) < max_batch_size:
        msg = inbox.get_nowait()      # 非阻塞抢
        if empty:
            if now >= deadline: break
            msg = inbox.get(timeout=remaining)   # 等到 deadline
        if msg.type == "new_request":
            if 超过 max_batch_cost: pending.appendleft(msg); break   # 放回去下批再说
            batch.append(msg)
        else:
            pending.append(msg)        # 非 new_request 不进批，留到后面处理
    return batch
```

然后 `run_batch` 对整批一次性调用：

```text
run_batch(batch, loop):
    if batch_fn is None or len(batch) <= 1: 逐条 _run_single        # 退化
    payloads = [msg.data for msg in batch]
    results = batch_fn(payloads)                                     # 一次调用
    if iscoroutine(results): results = loop.run_until_complete(results)
    if len(results) != len(batch): raise ValueError(...)            # 契约：等长
    for msg, result in zip(batch, results):
        if consume_if_aborted(msg.request_id): continue
        emit_result(msg.request_id, result, outbox)                  # 逐条发回
```

注意三点：

1. **批内的结果逐条发回**——即使前向是一次 batch，下游依然按 `request_id` 各收各的，对 Stage 透明。
2. **返回值数量必须等于输入数量**，否则视为契约违反，整个批的每条请求都会变成 `error`。
3. **成本预算**：`request_cost_fn` 给每条消息打一个「成本」（如字节大小），`max_batch_cost` 限制一批总成本，避免一个超大请求把一整批的显存撑爆。

可以用一个极简的「利用率」直觉来理解批处理收益。设单次前向固定开销为 \(C_f\)，每条请求的可变开销为 \(C_v\)，则：

- 逐个算 \(n\) 条请求总开销：\[ T_{\text{serial}} = n(C_f + C_v) \]
- 攒成一批算：\[ T_{\text{batch}} = C_f + nC_v \]

当 \(C_f\) 较大（GPU 前向固定开销高）时，批处理能把 \(nC_f\) 压成 \(C_f\)，这是它对 GPU 编码器有价值的根本原因。

#### 4.3.3 源码精读

收批逻辑：

[sglang_omni/scheduling/simple_scheduler.py:101-130](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L101-L130) —— `_collect_batch`。注意两个细节：一是成本超限时用 `pending_messages.appendleft(msg)` 把多出来的消息**放回队首**留给下一批（[L124](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L124)），不丢弃；二是非 `new_request` 消息被 `pending.append` 留到后面（[L129](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L129)），保证流式消息不被卷进批。

成本打分：

[sglang_omni/scheduling/simple_scheduler.py:88-91](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L88-L91) —— `_message_cost` 只对 `new_request` 算成本，其余返回 0，与「只攒 new_request」一致。

执行与契约校验：

[sglang_omni/scheduling/simple_scheduler.py:173-194](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L173-L194) —— `_run_batch`。当 `batch_fn` 为空或批只有 1 条时，退化为逐条 `_run_single`（[L178-L180](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L178-L180)）；否则一次性 `batch_fn(payloads)`，并强制 `len(results) == len(batch)`，否则抛 `ValueError`。

生产级真实用法——Qwen3-Omni 的图像编码器：

[sglang_omni/models/qwen3_omni/stages.py:848-855](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/stages.py#L848-L855) —— 同时给出单条版本 `_encode` 与批版本 `_encode_batch`，并配 `max_batch_size=32`、`max_batch_wait_ms=50`、`request_cost_fn`、`max_batch_cost`（字节预算）。注释说得很直白：「Preserve the calibrated image-encoder batching shape and add a small batch_wait so video benchmarks at concurrency=16 batch together」——即高并发下把多个图像请求攒批以提升编码器吞吐。对应的批函数 `_encode_batch` 见 [stages.py:823-844](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/stages.py#L823-L844)，它走 `_batch_image_encoder_payloads` 做真正的批量前向。音频编码器在 [stages.py:858-920](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/stages.py#L858-L920) 用同样的模式。

测试契约：

[tests/unit_test/pipeline/test_scheduler.py:31-66](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/pipeline/test_scheduler.py#L31-L66) —— `test_simple_scheduler_batch_and_error_contracts` 用 `batch_compute_fn=lambda payloads: [p.upper() for p in payloads]`、`max_batch_size=2` 验证两条请求被攒批后各自拿到大写结果；又用返回长度不匹配的坏 `batch_compute_fn` 验证每条请求都变成 `ValueError`。这正是上面「等长契约」的官方证据。

#### 4.3.4 代码实践

**实践目标**：把 4.2 的 uppercase 阶段从「逐个算」改成「批处理」，直观观察 `batch_compute_fn` 的等长契约。

**操作步骤**（示例代码，在 4.2 脚本基础上改）：

```python
# 示例代码：batch_compute_fn 实验
import threading
from sglang_omni.scheduling.simple_scheduler import SimpleScheduler
from sglang_omni.scheduling.messages import IncomingMessage

def to_upper(payload):            # 单条版本，留作退化路径
    return payload.upper()

def to_upper_batch(payloads):     # 批版本：吃列表，吐等长列表
    return [p.upper() for p in payloads]

sched = SimpleScheduler(
    to_upper,
    batch_compute_fn=to_upper_batch,
    max_batch_size=2,
    max_batch_wait_ms=10,
)
t = threading.Thread(target=sched.start, daemon=True); t.start()

for i, word in enumerate(["alpha", "beta"]):
    sched.inbox.put(IncomingMessage(f"req-{i}", "new_request", word))

outs = [sched.outbox.get(timeout=2.0) for _ in range(2)]
print([(o.request_id, o.data) for o in outs])   # 期望两条都被转大写
sched.stop(); t.join(timeout=1.0)
```

**需要观察的现象**：

1. 正常版：输出两条大写结果 `ALPHA`、`BETA`。
2. 故意把 `to_upper_batch` 改成 `return ["only-one"]`（长度不匹配），重跑：两条请求都会变成 `type="error"`、`data` 是 `ValueError`——复刻了 [test_scheduler.py:49-66](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/pipeline/test_scheduler.py#L49-L66) 的坏分支。
3. 再试着同时传 `max_concurrency=2` 和 `batch_compute_fn=...`：构造期就抛 `ValueError("...mutually exclusive")`，根本建不出来。

**预期结果**：上述三种现象分别对应「批处理成功」「等长契约校验」「互斥约束校验」。

> 待本地验证：请以本地实际运行结果为准。

#### 4.3.5 小练习与答案

**练习 1**：一个「把整数加 1」的纯 CPU 小函数，适合用 `batch_compute_fn` 吗？为什么？

**参考答案**：一般不适合。`batch_compute_fn` 的收益来自「单次调用的固定开销 \(C_f\) 较大」（典型如 GPU 前向）。纯 CPU 的 `x+1` 几乎没有固定开销，攒批反而徒增 `max_batch_wait_ms` 的等待延迟。判断标准是：**批处理收益 ≈ 把 \(nC_f\) 压成 \(C_f\)，只有 \(C_f\) 显著时才划算**。

**练习 2**：`_collect_batch` 遇到成本超限时，多出来的那条消息会被丢弃吗？

**参考答案**：不会。它会通过 `pending_messages.appendleft(msg)`（[L124](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L124)）放回待办队首，由下一次 `_next_message` 优先取出（[L94-L95](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L94-L95)），保证不丢消息。

---

### 4.4 abort 在非 AR 阶段的作用

#### 4.4.1 概念说明

`abort(request_id)` 表示「这个请求不要了」。在 AR 阶段（u4-l2、u6-l4），abort 非常麻烦：请求可能正在 prefill、可能占着 KV 缓存槽位、可能流式吐了一半 token，必须谨慎回收。但在 `SimpleScheduler` 管辖的非 AR 阶段，事情简单得多——**没有 KV 缓存、没有长期占用的重资源**，abort 通常只需要：

1. 标记这个 `request_id` 已中止；
2. 跳过它（既不白算，也不发多余结果）；
3. 如果构造时给了 `abort_callback`，调一下做额外清理（例如释放该请求临时缓存的内容）。

`SimpleScheduler.abort` 的关键设计：**调用即标记、立即触发回调**（如果有）。因为非 AR 阶段的请求要么还没开始算（在 inbox 队列里排队），要么正在算（单次 `compute_fn` 调用），要么已经算完（结果在 outbox 里）——这三种情况都能用「查集合 + 跳过」处理，不需要像 AR 那样延迟回收。

#### 4.4.2 核心流程

abort 在三个时点被检查（「检查」就是查 `request_id` 是否在已中止集合里）：

```text
abort(request_id):
    with lock: aborted.add(request_id)        # 打标记
    cleanup_aborted_request(request_id)        # 立即调 abort_callback（若有）

# 主循环里三处检查点：
_run_single(msg, loop):
    if consume_if_aborted(msg.request_id): return      # ① 算之前
    result = compute_fn(msg.data)
    ...
    if consume_if_aborted(msg.request_id): return      # ② 算之后、发结果之前
    emit_result(...)
```

`_consume_if_aborted` 是「查 + 消费」的原子操作：若在集合里，就从集合移除并返回真（让调用方跳过这次处理）；若不在，返回假。这样每个已中止请求的标记只会被消费一次，不会无限堆积。

为防止集合无限增长，`abort` 里还有一个软上限：当集合超过 10000 条时，删掉最旧的 5000 条之外的多余项（[L305-L308](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L305-L308)）——这是一个有界的「最近已中止」集合。

#### 4.4.3 源码精读

标记与立即回调：

[sglang_omni/scheduling/simple_scheduler.py:302-309](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L302-L309) —— `abort` 加锁写入集合，然后**立即**调 `_cleanup_aborted_request`。对比 OmniScheduler 的 abort（见 u4-l2，需要走「标记 to_finish、延迟到下一步回收 KV」），这里没有延迟路径，因为非 AR 阶段没有可延迟回收的重资源。

回调执行（容错）：

[sglang_omni/scheduling/simple_scheduler.py:72-78](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L72-L78) —— `_cleanup_aborted_request` 把 `abort_callback` 包在 try/except 里，失败只记日志不抛。这保证「清理失败不能把主循环拖崩」。

检查点位置：

[sglang_omni/scheduling/simple_scheduler.py:80-86](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L80-L86) —— `_consume_if_aborted`。它被调用在 `_run_single` 的 [L159（算前）](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L159) 与 [L169（算后发结果前）](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L169)、异常路径 [L166](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L166)、批处理结果分发 [L192](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L192)，以及主循环取到 new_request 后的 [L223](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L223)——覆盖了请求生命周期的每个关键缝隙。

Stage 何时调 `scheduler.abort`：

[sglang_omni/pipeline/stage/runtime.py:617](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L617)（及 643、765、1530、1561）—— Stage 收到 abort 控制消息时调 `self.scheduler.abort(request_id)`，由 PUB/SUB 广播保证所有相关 stage 都收到（详见 u3-l2）。

#### 4.4.4 代码实践

**实践目标**：验证「abort 之后，请求不会被白算、也不会发出 result」。

**操作步骤**（示例代码）：

```python
# 示例代码：abort 实验
import threading, time
from sglang_omni.scheduling.simple_scheduler import SimpleScheduler
from sglang_omni.scheduling.messages import IncomingMessage

cleaned = []
def slow_upper(payload):
    time.sleep(0.3)                 # 模拟慢计算
    return payload.upper()

sched = SimpleScheduler(slow_upper, abort_callback=cleaned.append)
t = threading.Thread(target=sched.start, daemon=True); t.start()

sched.inbox.put(IncomingMessage("req-z", "new_request", "hello"))
time.sleep(0.05)
sched.abort("req-z")                # 计算进行中 abort

# 给点时间让循环跑完一轮
time.sleep(0.5)
# outbox 应该是空的（既没 result 也没 error）
import queue
try:
    o = sched.outbox.get_nowait()
    print("意外拿到:", o)
except queue.Empty:
    print("outbox 为空，请求被丢弃 ✓")
print("abort_callback 收到:", cleaned)   # 立即被调用
sched.stop(); t.join(timeout=1.0)
```

**需要观察的现象**：即便 `slow_upper` 在 abort 时已经开算，最终 `outbox` 里**没有** `req-z` 的 `result`（因为算完后的检查点 [L169](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L169) 把它拦截了）；同时 `abort_callback` 被**立即**调用（`cleaned == ["req-z"]`）。

**预期结果**：打印 `outbox 为空，请求被丢弃 ✓` 与 `abort_callback 收到: ['req-z']`。

> 待本地验证：时序类实验对机器负载敏感，请以本地实际结果为准；若 `time.sleep(0.05)` 内计算尚未开始，则会走「算前检查点」[L159](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L159) 拦截，最终现象一致。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `SimpleScheduler.abort` 能「立即」调 `abort_callback`，而 OmniScheduler 的 abort 往往要延迟回收？

**参考答案**：因为非 AR 阶段**没有 KV 缓存、没有长期占用的 token 槽位**这类重资源。请求要么在排队（直接跳过即可）、要么在一次无状态 `compute_fn` 里（算完检查点拦截即可），不存在「请求正在 prefill、KV 已分配、需等下一步安全回收」的复杂状态，所以清理可以即时完成。OmniScheduler 因为绑定了 SGLang 的 KV 管理，abort 必须配合 prefill/decode 步骤谨慎回收（见 u4-l2、u6-l4）。

**练习 2**：如果 `abort_callback` 自己抛了异常，主循环会崩吗？

**参考答案**：不会。[simple_scheduler.py:75-78](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/simple_scheduler.py#L75-L78) 用 `try/except Exception` 包住回调，失败只 `logger.exception(...)` 记日志，不向上抛。

---

## 5. 综合实践

把四个模块串起来，做一个「会批处理、能 abort、结果走 outbox」的最小非 AR 阶段。

**任务**：实现一个 `word_count` 阶段——`compute_fn` 把一段文本按空格切分返回词数；再给一个 `batch_compute_fn` 让多条请求攒批；最后验证 abort 能让排队中的请求被丢弃。

**参考实现骨架**（示例代码）：

```python
# 示例代码：综合实践
import threading, queue
from sglang_omni.scheduling.simple_scheduler import SimpleScheduler
from sglang_omni.scheduling.messages import IncomingMessage

def count(payload: str) -> int:                 # 单条
    return len(payload.split())

def count_batch(payloads: list[str]) -> list[int]:   # 批处理（等长）
    return [len(p.split()) for p in payloads]

cleaned = []
sched = SimpleScheduler(
    count,
    batch_compute_fn=count_batch,
    max_batch_size=4,
    max_batch_wait_ms=20,
    abort_callback=cleaned.append,
)
t = threading.Thread(target=sched.start, daemon=True); t.start()

# ① 攒批：两条文本
sched.inbox.put(IncomingMessage("a", "new_request", "hello world omni"))
sched.inbox.put(IncomingMessage("b", "new_request", "one two three four five"))
# ② 排队中 abort 第三条（先 put 再 abort，模拟排队时取消）
sched.inbox.put(IncomingMessage("c", "new_request", "should be cancelled"))
sched.abort("c")

outs = []
try:
    while True:
        outs.append(sched.outbox.get(timeout=1.0))
except queue.Empty:
    pass
print({(o.request_id, o.type, o.data) for o in outs})
print("cleaned:", cleaned)
sched.stop(); t.join(timeout=1.0)
```

**验收清单**：

1. `a` 的结果为 `3`，`b` 的结果为 `5`（批处理正确，且结果按 `request_id` 各回各的）。
2. `c` 不出现在 `outs` 的 `result` 里（被 abort 拦截，尽管它已被 `put` 进 inbox）。
3. `cleaned` 包含 `"c"`（abort 回调被立即调用）。
4. 若把 `count_batch` 改成返回长度不匹配，`a`、`b` 都应变 `error`（等长契约）。

> 待本地验证：并发与时序相关，请以本地运行结果为准。

完成本任务后，你就用 `SimpleScheduler` 复现了一个「真实的非 AR 阶段」所依赖的全部机制：单条计算、批处理、结果经 outbox 回流、abort 清理——这与 [Qwen3-Omni 编码器](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/stages.py#L848-L855) 在生产环境里用的完全是同一套接口。

## 6. 本讲小结

- 调度器对 Stage 的契约是固定的五件套：`inbox` / `outbox` / `start` / `stop` / `abort`。`SimpleScheduler`、`OmniScheduler`、流式调度器都满足它，所以 Stage 的代码不需要为调度器类型分支。
- 消息分两类：进入调度器的 `IncomingMessage`（`type` ∈ `new_request` / `stream_chunk` / `stream_done`）与离开调度器的 `OutgoingMessage`（`type` ∈ `result` / `stream` / `error`，多 `target` / `metadata`），定义在 [messages.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/messages.py#L10-L23)。
- `SimpleScheduler` 的最简主循环就是 `inbox.get → compute_fn → outbox.put`，在专用线程里阻塞运行，异常只影响单条请求、不拖垮循环。
- `batch_compute_fn` 是非 AR 阶段的简单批处理（不同于 AR 的连续 batching），适合「单次前向固定开销大」的 GPU 编码器；与 `max_concurrency > 1` 互斥，且必须返回等长结果列表。
- `abort` 在非 AR 阶段能「即时清理」，因为没有 KV 缓存等重资源；标记 + 多处检查点保证已中止请求不被白算、不发多余结果。
- 调度器只负责「算完放进 outbox」，结果的路由（result → 下游、stream → 目标、error → 失败上报）是 Stage 的 `_drain_outbox` 的事。

## 7. 下一步学习建议

- **u4-l2 OmniScheduler 与 SGLang 后端**：去看一个「重」调度器如何复用 SGLang 的 prefill/decode、KV 缓存与连续 batching，并体会它与 `SimpleScheduler` 在 abort 路径上的本质差异（延迟回收 vs 即时清理）。
- **u4-l3 ModelRunner 与 AR 前向路径**：理解调度器算完之后，真正的模型前向（`ForwardBatch`、sampling、多模态 embedding 注入）发生在哪里。
- **u4-l4 流式调度器与流式 vocoder**：本讲多次提到「流式阶段要用专用调度器而非 SimpleScheduler」，这一讲会给出 `Code2WavScheduler` 等流式实现，补齐 `stream_chunk` / `stream_done` 的处理。
- 若想立刻看到 `batch_compute_fn` 的真实生产用法，直接读 [qwen3_omni/stages.py 的 image/audio encoder 工厂](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/stages.py#L786-L855)，这是本讲 4.3 的完整对照。
