# 异步推理循环 EngineLoop

## 1. 本讲目标

在上一讲（u4-l1）里，我们把 `Engine` 类看作一个 **Actor（演员模型）**：它持有 `executor`、`scheduler`、`req_manager` 三大件，通过 `start()` 拉起一个「主循环」来消费请求。但当时我们刻意留下了一个黑盒——这个「主循环」内部到底长什么样？是谁在不停地调度？是谁在调用 forward？生成出来的 token 又是怎么一路送回到用户手里的？

本讲就打开这个黑盒，专门拆解 `lmdeploy/pytorch/engine/engine_loop.py` 中的 **`EngineLoop`**。读完本讲，你应当能够：

1. 画出 `EngineLoop` 启动后并发运行的 **多条 asyncio 协程**（preprocess / main / send_response / migration），并说清它们的职责边界。
2. 理解 `_main_loop_get_outputs` 与 `_send_resps` 这两条核心数据流：前者把「调度 → 预取 → forward → 取输出」串成一条流水线，后者把推理产出送回客户端。
3. 认识两个自定义同步原语——`RunableEventAsync` 与 `CounterEvent`——它们如何替代朴素 `asyncio.Event` 来表达「有无待办工作」和「在途 forward 计数」。
4. 在源码里准确定位 `scheduler.tick()` 的真实调用位置（提示：它不在 `engine_loop.py` 里，而在 `inputs_maker.py` 中）。

## 2. 前置知识

阅读本讲前，你需要先理解以下概念（若不熟悉，建议先看 u4-l1）：

- **协程（coroutine）与 asyncio**：Python 的 `async/await` 是单线程内的协作式并发。`asyncio.Event` 是协程间的「信号灯」：`set()` 点亮、`clear()` 熄灭、`await event.wait()` 会挂起当前协程直到点亮。`asyncio.Queue` 是协程间传递数据的「传送带」，`put_nowait()` 投递、`await get()` 阻塞接收。
- **Actor 模型与 RequestManager**：`Engine` 把所有跨线程的「请求」（ADD_SESSION、ADD_MESSAGE、STOP_SESSION 等）都封装成 `Request` 对象，丢进 `RequestManager` 这个「信箱」。主循环负责从信箱里取请求、分桶派发给 `_on_*` 回调。这样所有对调度器状态的修改都发生在主循环这一条执行流里，天然线程安全。
- **Prefill / Decode 两阶段**：一次推理分「预填充」（把整段 prompt 一次性算出第一轮 KV）和「解码」（逐个 token 续写）。调度器每一步要么做 prefill、要么做 decode，由 `InputsMakerAsync.do_prefill()` 决定。
- **持续批处理（continuous batching）**：每一步 forward 都可以往当前 batch 里塞新请求或踢掉已结束的请求，而不是等整批算完。

> 关键术语速查：`forward_async`（把一批输入投递给执行器，**不阻塞**）、`get_output_async`（**阻塞等待**这一批的输出）、`prefetch`（提前为下一批做准备）、`resp_queue`（main 协程把产出投进去、send_response 协程取出来）。

## 3. 本讲源码地图

本讲围绕两个核心文件展开：

| 文件 | 作用 |
| --- | --- |
| [engine_loop.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py) | **本讲主角**。定义 `EngineLoop` 类、`CounterEvent`、`RunableEventAsync`、`EngineLoopConfig`，以及多条异步协程。 |
| [engine.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py) | `Engine.async_loop()`（L595）创建并启动 `EngineLoop`，是它的「宿主」。本讲只看这段衔接代码。 |

为了把数据流讲透，还会**点到为止**地引用几个协作者的少量代码：

| 文件 | 在本讲中的角色 |
| --- | --- |
| [engine/inputs_maker.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py) | `scheduler.tick()` 的**真正调用点**（L1163），以及 `send_next_inputs` / `prefetch_next_inputs`。 |
| [paging/scheduler.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py) | `tick()` 的定义（L143），仅做计数。 |
| [engine/request.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py) | `RequestManager.step()`（L398）与 `response()`（L374，点亮 resp.event）。 |
| [engine/model_agent/agent.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/model_agent/agent.py) | `forward_event.clear()`（L985）与 `forward_event.set()`（L944），驱动 `CounterEvent`。 |

---

## 4. 核心概念与源码讲解

### 4.1 EngineLoop：异步推理的「多协程总管」

#### 4.1.1 概念说明

上一讲我们说 `Engine.start()` 只是「拉起主循环」，但「主循环」并不是一条单一的死循环。它实际上是一个由 **`EngineLoop`** 统一管理的「协程乐团」：

- **preprocess_loop**：不停地从 `RequestManager` 取外部请求，喂进调度器。
- **main_loop**：核心节拍器。每一步决定做 prefill 还是 decode，组装输入、发起 forward、回收输出。
- **send_response_loop**：把 forward 产出的 token 打包成响应，送回给等待中的客户端。
- **migration_loop**：仅 PD 分离场景启用，负责把 KV cache 从 prefill 节点迁移到 decode 节点。

为什么要拆成多条协程而不是一个大循环？因为「取请求」「跑 GPU forward」「回送响应」这三件事的**节奏完全不同**：

- 取请求是纯 CPU、极快、应随时响应；
- forward 受限于 GPU 算力，单步几十毫秒到几百毫秒；
- 回送响应要等网络/客户端。

如果串成一个循环，慢的 forward 就会拖住请求接收，导致新请求迟迟进不了调度器（首 token 延迟变大）。拆成协程后，它们共享同一个事件循环（单线程，无需加锁），却能各自按自己的节奏推进。

> 一句话定位：`EngineLoop` 不是「一个循环」，而是「一组在同一 asyncio 事件循环里并发的协作协程」的总管对象。

#### 4.1.2 核心流程

`EngineLoop` 由 `Engine.async_loop()` 创建并启动。整体衔接如下：

```text
Engine.start()                                   # u4-l1 讲过：只 create_loop_task
   └─ RequestManager 驱动 Engine.async_loop()    # engine.py:595
         ├─ build_engine_loop(self)              # engine_loop.py:671  构造 EngineLoop
         ├─ engine_loop.start(event_loop)        # engine_loop.py:613  注册多条协程任务
         └─ await engine_loop.wait_tasks()       # engine_loop.py:633  阻塞直到全部协程退出
```

`start()` 注册了 4~5 个协程任务（见 4.1.3）。注册之后 `async_loop` 就 `await wait_tasks()`，把控制权交给事件循环，自己挂起直到 `stop()` 被调用或异常发生。

#### 4.1.3 源码精读

先看 `Engine.async_loop()` 这个「宿主」如何创建并启动 EngineLoop：

[engine.py:595-609](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L595-L609) —— 创建 EngineLoop 并启动（中文说明：用 `build_engine_loop(self)` 把 `req_manager`/`scheduler`/`executor` 等组件注入构造一个 `EngineLoop`，随后调 `start(event_loop)` 注册协程任务，最后 `await wait_tasks()` 阻塞等待它们全部结束）。

```python
async def async_loop(self):
    engine_loop = None
    try:
        from lmdeploy.pytorch.engine.engine_loop import build_engine_loop
        self._loop_main = asyncio.current_task()
        event_loop = asyncio.get_event_loop()
        engine_loop = build_engine_loop(self)
        self._engine_loop = engine_loop
        self.migration_event = engine_loop.migration_event
        engine_loop.start(event_loop)
        await engine_loop.wait_tasks()
```

再看 `EngineLoop.start()`，这是「协程乐团」的报名表：

[engine_loop.py:613-631](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L613-L631) —— 注册 4~5 条协程任务（中文说明：先 `executor.start(self.forward_event)` 启动执行器，再把 `wait_tasks`/`preprocess_loop`/`send_response_loop`/`main_loop`/`migration_loop` 用 `event_loop.create_task` 注册成具名任务，存入 `self.tasks` 集合；`migration_loop` 仅在非 `Hybrid` 角色时注册；末行给每个任务挂一个 `discard` 回调，任务结束后自动从集合里摘除）。

```python
def start(self, event_loop: asyncio.AbstractEventLoop):
    logger.info('Starting executor.')
    self.executor.start(self.forward_event)
    self.tasks.add(event_loop.create_task(self.executor.wait_tasks(), name='MainLoopWaitExecutor'))
    self.tasks.add(event_loop.create_task(self.preprocess_loop(), name='MainLoopPreprocessMessage'))
    self.tasks.add(event_loop.create_task(self.send_response_loop(), name='MainLoopSendResponse'))
    self.tasks.add(event_loop.create_task(self.main_loop(), name='MainLoopMain'))
    if self.config.role != EngineRole.Hybrid:
        self.tasks.add(event_loop.create_task(self.migration_loop(), name='MainLoopMigration'))
    for task in self.tasks:
        task.add_done_callback(self.tasks.discard)
```

启动后的协程全景图（本讲的「地图」，后续各节逐一展开）：

```text
                     ┌─────────────────────────── EngineLoop (同一 asyncio 事件循环) ───────────────────────────┐
                     │                                                                                       │
 External Requests   │  ┌── preprocess_loop ──┐    修改调度器       ┌── main_loop ──┐  forward_async   ┌── Executor/ModelAgent ──┐ │
   (ADD_MESSAGE)─────┼─▶│ req_manager.step()   │──────状态──────────▶│ 调度+预取+forward │──────────────▶│  preprocess(排空 forward_event)│ │
                     │  │ has_runable_event.set│                    │ get_output_async │◀───────────────│  background(forward_event.set)│ │
                     │  └──────────────────────┘                    └──────┬──────────┘                └──────────────────────────┘ │
                     │                                                     │ resp_queue.put_nowait(step_outputs)                            │
                     │                                                     ▼                                                               │
                     │                                          ┌── send_response_loop ──┐    resp.event.set()       ┌── 客户端/EngineInstance ──┐ │
                     │                                          │ resp_queue.get()        │──────────────────────────▶│  await resp.event.wait()    │ │
   ◀─────────Response─┼──────────────────────────────────────────│ _send_resps → _send_resp│                           └─────────────────────────────┘ │
                     │                                          └─────────────────────────┘                                                          │
                     └───────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

注意 `main_loop` 与 ModelAgent 之间有两类交互：`forward_async`（投递输入，非阻塞）和 `get_output_async`（取输出，阻塞）。这两个调用在 4.4 节会重点讲。

#### 4.1.4 代码实践

**实践目标**：把 `EngineLoop` 启动后注册的协程任务清单与各自的「主循环入口」对上号。

**操作步骤（源码阅读型）**：

1. 打开 [engine_loop.py:613-631](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L613-L631)，列出 `create_task` 一共注册了哪几个具名任务。
2. 对每个任务名，找到它对应的协程方法定义行（如 `MainLoopPreprocessMessage` → `preprocess_loop` 在 L159）。
3. 思考：为什么 `migration_loop` 要用 `if self.config.role != EngineRole.Hybrid` 守卫？（提示：Hybrid 角色是 prefill+decode 同进程，不需要跨节点迁移。）

**预期结果**：得到一张「任务名 → 入口方法 → 一句话职责」的对照表，例如：

| 任务名 | 入口方法 | 职责 |
| --- | --- | --- |
| MainLoopWaitExecutor | `executor.wait_tasks` | 等待执行器内部协程结束 |
| MainLoopPreprocessMessage | `preprocess_loop` | 消费外部请求并喂给调度器 |
| MainLoopSendResponse | `send_response_loop` | 把产出打包回送客户端 |
| MainLoopMain | `main_loop` | 调度 + prefill/decode + forward |
| MainLoopMigration | `migration_loop` | PD 分离时迁移 KV cache |

> 运行环境备注：本实践为纯源码阅读，无需 GPU。如需「眼见为实」，可在有 GPU 的机器上跑一次推理并开启 `LMDEPLOY_LOG_LEVEL=DEBUG`，日志里会出现 `Starting async task MainLoopPreprocessMessage.` 等行（即上面 `logger.info` 的输出），与任务名一一对应。

#### 4.1.5 小练习与答案

**练习 1**：如果想让 `EngineLoop` 多支持一条「指标采集」协程，需要修改哪个方法？为什么不用改 `Engine.async_loop()`？
**答案**：只需在 `EngineLoop.start()`（L613）里增加一行 `self.tasks.add(event_loop.create_task(self.metrics_loop(), name='MainLoopMetrics'))`。不用改 `async_loop()`，因为协程注册完全封装在 `EngineLoop` 内部，宿主只调 `start()` + `wait_tasks()`，这正是「总管」抽象的好处。

**练习 2**：`self.tasks` 用 `set` 而非 `list` 存任务，且末尾挂了 `task.add_done_callback(self.tasks.discard)`，为什么？
**答案**：任务结束后自动从集合摘除，避免已完成任务长期堆积；`wait_tasks()` 里对 `self.tasks` 做 `.copy()` 再 await，配合 discard 回调可以保证「拷贝那一瞬间的活跃任务」被正确等待，而新加入/退出的任务不会破坏这次等待。

---

### 4.2 同步原语：CounterEvent 与 RunableEventAsync

#### 4.2.1 概念说明

asyncio 自带的 `asyncio.Event` 只能表达「亮 / 灭」两种状态，但 `EngineLoop` 需要表达两类更复杂的状态：

1. **「在途 forward 还有几个？」**——一条朴素 Event 无法表达「3 个 forward 投出去了、但只完成了 1 个」。于是有了 **`CounterEvent`**：内部带一个计数器，被「投递」时 `clear()`（计数 +1）、被「完成」时 `set()`（计数 -1），只有计数回到 0 才真正点亮事件。
2. **「现在到底有没有可跑的活？」——这个活可能来自调度器，也可能来自别处（如长上下文分块）。于是有了 **`RunableEventAsync`**：它在 `set()` 时**主动询问**「还有没有未完成的工作」，有就点亮、没有就熄灭，而不是无条件点亮。

这两个类都不改变「单线程协程」的本质，它们只是把「条件判断」和「事件信号」绑在一起，让等待方少写一段 `while True: if has_work: break; await event.wait()` 的样板代码。

#### 4.2.2 核心流程

**CounterEvent 的状态机**（以「投递 2 个 forward、再依次完成」为例）：

```text
初始:        counter=0, event=灭
clear()x1:   counter=1, event=灭      (投递 forward A)
clear()x2:   counter=2, event=灭      (投递 forward B，A 还没完成)
set()x1:     counter=1, event=灭      (A 完成，但 B 还在飞)
set()x2:     counter=0, event=亮      (B 完成，在途归零 → 点亮)
```

语义：**`is_set() == True` 当且仅当所有已投递的 forward 都已完成**，即「forward 流水线已排空」。

**RunableEventAsync 的判定**：调用 `set()` 时，先调 `has_unfinished()`：

```text
has_unfinished() = scheduler.has_unfinished()  OR  extra_runable_checker()
                 (调度器还有 waiting/running 序列)   (例如 InputsMaker 还有未处理的长上下文分块)
set():  有活 → event.set()；没活 → event.clear()
```

#### 4.2.3 源码精读

**CounterEvent**——继承 `asyncio.Event`，覆写 `set/clear` 加计数：

[engine_loop.py:38-53](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L38-L53) —— 计数事件（中文说明：`set()` 先把计数器减一，只有减到 0 才真正点亮底层 Event；`clear()` 只有在「当前已点亮且计数为 0」时才熄灭，随后计数器加一。这样就实现了「N 次 clear 配 N 次 set 才点亮一次」的计数语义）。

```python
class CounterEvent(asyncio.Event):
    def __init__(self):
        super().__init__()
        self._counter = 0

    def set(self):
        if self._counter > 0:
            self._counter -= 1
        if self._counter == 0:
            super().set()

    def clear(self):
        if self._counter == 0 and super().is_set():
            super().clear()
        self._counter += 1
```

那谁在驱动这个计数器？答案是 **ModelAgent 的两条内部协程**（`EngineLoop` 把 `forward_event` 经 `executor.start(self.forward_event)` 一路传给 ModelAgent）：

- [agent.py:984-985](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/model_agent/agent.py#L984-L985) —— 每预处理完一批输入（H2D 拷贝完成、投入 `_in_que`）就 `forward_event.clear()`，意为「又多了一个在途 forward」。
- [agent.py:943-944](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/model_agent/agent.py#L943-L944) —— 每跑完一步 forward（`_async_step` 结束）就 `forward_event.set()`，意为「完成了一个在途 forward」。

> 诚实说明：在本讲涉及的所有代码路径里，`forward_event` **只被 set/clear，没有被 `await wait()`**。也就是说它目前更像一个「在途 forward 计数 / 排空状态指示器」，并不直接阻塞 `main_loop`。`main_loop` 与 ModelAgent 之间真正的阻塞同步靠的是 `await executor.get_output_async()`（见 4.4 节）和 ModelAgent 内部的三个 `asyncio.Queue`。理解它的计数语义即可，不必臆想它会卡住主循环。

**RunableEventAsync**——把「是否有活」的判断绑进 `set()`：

[engine_loop.py:56-79](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L56-L79) —— 可运行事件（中文说明：`has_unfinished()` 先问调度器是否还有未完成序列，再问外部 checker（长上下文分块）；`set()` 会先调用它，有活才点亮、没活反而熄灭，从而避免「事件亮了但其实没活可干」的假唤醒）。

```python
class RunableEventAsync:
    def __init__(self, scheduler, extra_runable_checker=None):
        self.scheduler = scheduler
        self.extra_runable_checker = extra_runable_checker
        self.event = asyncio.Event()

    def has_unfinished(self):
        if self.scheduler.has_unfinished():
            return True
        return self.extra_runable_checker is not None and self.extra_runable_checker()

    async def wait(self):
        await self.event.wait()

    def set(self):
        if self.has_unfinished():
            self.event.set()
        else:
            self.event.clear()
```

它在 `EngineLoop.__init__` 里这样被创建（注意它把「长上下文分块」也算作「待办」）：

[engine_loop.py:141](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L141) —— `self.has_runable_event = RunableEventAsync(self.scheduler, self.inputs_maker.has_pending_long_context_chunk)`（中文说明：可运行事件的「额外活源」是 InputsMaker 的待处理长上下文分块；因为分块的所有权在 InputsMaker 而非调度器的 WAITING/READY 队列，必须单独纳入判定，否则 main_loop 会以为没活而空转）。

#### 4.2.4 代码实践

**实践目标**：亲手验证 `CounterEvent` 的计数语义，确认「N 次 clear + N 次 set 才点亮」。

**操作步骤（可在任意能跑 Python 的环境，无需 GPU）**：

1. 把 `CounterEvent` 的源码（[engine_loop.py:38-53](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L38-L53)）复制到一个独立脚本里。
2. 写一段：连续 `clear()` 两次，中间 `print(ev.is_set())`；再 `set()` 一次、`print`；再 `set()` 一次、`print`。

**预期结果**（示例代码，标注为「示例代码」）：

```python
# 示例代码：验证 CounterEvent 计数语义
import asyncio

class CounterEvent(asyncio.Event):  # 复制自 engine_loop.py:38-53
    def __init__(self):
        super().__init__()
        self._counter = 0
    def set(self):
        if self._counter > 0:
            self._counter -= 1
        if self._counter == 0:
            super().set()
    def clear(self):
        if self._counter == 0 and super().is_set():
            super().clear()
        self._counter += 1

ev = CounterEvent()
ev.clear(); ev.clear()
print(ev.is_set(), ev._counter)   # False 2
ev.set()
print(ev.is_set(), ev._counter)   # False 1
ev.set()
print(ev.is_set(), ev._counter)   # True  0   ← 在途归零才点亮
```

> 若无法本地运行，标注「待本地验证」，但依据源码可推断输出如上注释所示。

#### 4.2.5 小练习与答案

**练习 1**：如果连续调用 `set()` 三次、但之前一次 `clear()` 都没有，会发生什么？这会出问题吗？
**答案**：第一次 `set()` 时 `_counter` 已是 0，`if self._counter > 0` 不成立，直接走到 `if self._counter == 0: super().set()` 点亮事件；后两次同理，事件保持点亮。不会出问题——`CounterEvent` 对「多余的 set」是幂等的。

**练习 2**：`RunableEventAsync.set()` 为什么不直接 `self.event.set()`，而要先调 `has_unfinished()`？
**答案**：因为调用方（如 `preprocess_loop`）只知道「我刚刚加了一个请求」，却不知道调度器整体是否真的有可调度的工作（可能这个请求立刻就被判定为非法、或调度器因别的原因空了）。先查 `has_unfinished()` 再决定亮灭，可以避免「事件亮了但 main_loop 醒来发现没活」的假唤醒，把「是否有活」这个唯一真相源收敛到调度器。

---

### 4.3 preprocess_loop：把外部请求喂进调度器

#### 4.3.1 概念说明

`preprocess_loop` 是 EngineLoop 里最短、却最关键的「入口协程」之一。它的职责极其单一：**不停地调用 `req_manager.step()`，把外部世界投来的请求（ADD_SESSION / ADD_MESSAGE / STOP_SESSION / END_SESSION）转交给调度器**。

回想 u4-l1：外部线程（比如 `EngineInstance`）通过 `RequestSender` 把 `Request` 投进 `RequestManager` 的队列。但 `RequestManager` 自己不会主动处理队列——它要求「在 loop task 里」调用 `step()`。`preprocess_loop` 就是那个不停 `step()` 的人。每处理完一批请求，它就 `has_runable_event.set()`，通知 `main_loop`「可能有新活来了，别睡着」。

#### 4.3.2 核心流程

```text
preprocess_loop:
  while not stop_event:
      await req_manager.step()      # 取一批请求，按 request_priority 分桶派发给 _on_* 回调
      has_runable_event.set()       # 通知 main_loop 重新评估「有没有活」
```

这里 `step()` 内部会：`get_all_requests()` 把队列里所有请求按类型分桶 → 按 `request_priority`（如 STOP_ENGINE 先于 ADD_MESSAGE）依次 `process_request()` → 调到 `Engine._on_add_session` / `_on_add_message` 等回调（这些回调直接修改 `scheduler` 的会话与序列状态）。由于这一切都发生在 `preprocess_loop` 这一条协程里，而 `main_loop` 也跑在同一事件循环里，两者对调度器的修改不会真并发，天然安全。

#### 4.3.3 源码精读

**preprocess_loop 本体**，只有 4 行：

[engine_loop.py:159-163](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L159-L163) —— 预处理协程（中文说明：`stop_event` 未点亮就不停循环；每轮 `await req_manager.step()` 处理一批外部请求并触发对应的 `_on_*` 回调，随后 `has_runable_event.set()` 唤醒可能正在等待的 main_loop）。

```python
async def preprocess_loop(self):
    """Preprocess request."""
    while not self.stop_event.is_set():
        await self.req_manager.step()
        self.has_runable_event.set()
```

**`req_manager.step()` 的内部**——请求分桶与按优先级派发：

[request.py:398-424](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py#L398-L424) —— step（中文说明：先 `get_all_requests()` 把队列里所有请求按 `RequestType` 分桶，再按 `request_priority` 顺序遍历各类型，对非空桶调 `process_request()` 转发到绑定的回调函数）。

```python
async def step(self, **kwargs):
    reqs_by_type = await self.get_all_requests()
    for req_type in self.request_priority:
        reqs = reqs_by_type.get(req_type, [])
        if not reqs:
            continue
        _log_reqs(reqs)
        self.process_request(req_type, reqs, **kwargs)
```

注意 `step()` 里有一个 `await`（在 `get_all_requests()` 内部），这一处让出控制权非常关键——它让事件循环有机会切到 `main_loop` 去跑 forward，而不是被请求洪流独占。这也解释了为什么 `preprocess_loop` 不会「饿死」`main_loop`：每处理一批请求就会在 `await` 点交还调度权。

#### 4.3.4 代码实践

**实践目标**：验证「preprocess_loop 与 main_loop 交替推进」的协作关系，并定位请求派发的优先级。

**操作步骤（源码阅读型）**：

1. 在 [request.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py) 中搜索 `request_priority` 的赋值处，找出 `RequestType` 的处理顺序（谁先谁后）。
2. 在 [engine.py:277-284](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L277-L284)（`_bind_request_manager`）确认四种请求类型分别绑定到哪个 `_on_*` 回调。
3. 思考：为什么 `STOP_SESSION` 通常要排在 `ADD_MESSAGE` 之前？（提示：停止指令应尽快生效，避免被排在后面的大量新增消息拖住。）

**预期结果**：能复述「请求 → 分桶 → 按优先级 → `_on_*` 回调 → 修改调度器状态 → `has_runable_event.set()`」这条完整链路。

#### 4.3.5 小练习与答案

**练习 1**：`preprocess_loop` 每轮只 `await req_manager.step()` 一次。如果某一时刻没有新请求进来，它会忙等（busy loop）烧 CPU 吗？
**答案**：不会。`step()` 内部的 `get_all_requests()` 最终会 `await` 一个队列的 `get()`，在没请求时会挂起协程、让出事件循环，直到有新请求到达才被唤醒。所以空闲时它几乎不消耗 CPU。

**练习 2**：`preprocess_loop` 处理完请求后为什么必须调 `has_runable_event.set()`？不调会怎样？
**答案**：因为 `main_loop` 在没有可调度工作时会在 `_main_loop_try_send_next_inputs` 里 `await has_runable_event.wait()` 挂起（见 4.4.3）。若 preprocess 不点亮事件，main_loop 就不会醒来去处理刚进来的新请求，导致请求「进了信箱却没人理」的假死。注意由于 `RunableEventAsync.set()` 会先查 `has_unfinished()`，只有真有活时才会点亮，所以也不会产生假唤醒。

---

### 4.4 main_loop：调度、预取与 forward 的核心数据流

#### 4.4.1 概念说明

`main_loop` 是整个 EngineLoop 的「心脏」，每跳动一次就完成一步推理。它把四件事编排在一起：

1. **取下一批输入**（`_main_loop_try_send_next_inputs`）：决定做 prefill 还是 decode，组装 `forward_inputs`，投递给执行器。
2. **激活序列**（`scheduler.activate_seqs`）：把即将参与的序列标记为 RUNNING。
3. **取输出 + 预取下一批**（`_main_loop_get_outputs`）：**先把「下一批」也投递出去（非阻塞），再阻塞等「当前这批」的输出**——这是流水线重叠的关键。
4. **回收与让出**（`_finish_forward_output` → `resp_queue`）：把产出交给 `send_response_loop`，再点亮 `has_runable_event` 继续下一轮。

最反直觉、也最值得品味的是第 3 步里的**重叠（overlap）**：当 GPU 还在算第 N 批时，CPU 已经把第 N+1 批的输入准备好了并投递出去。这样 GPU 一空闲就能立刻拿到下一批，大幅减少气泡。

> 这也是为什么 `scheduler.tick()` 不在 `engine_loop.py` 里——它发生在「投递输入」的那一刻，而投递动作被封装在 `InputsMakerAsync` 中。

#### 4.4.2 核心流程

`main_loop` 单步（伪代码，省略 sleep 分支）：

```text
while not stop_event:
    if next_running is None:                              # 还没有「待跑的下一批」
        forward_inputs, next_running = try_send_next_inputs()
        #   └─ inputs_maker.send_next_inputs()
        #        └─ _send_next_inputs_impl(): 组输入 → executor.forward_async() → scheduler.tick()
        if next_running is None:                          # 调度器说暂时没活
            await has_runable_event.wait(); continue       # 挂起，等 preprocess 唤醒

    scheduler.activate_seqs(next_running)                 # 标记 RUNNING
    forward_inputs, next_running = main_loop_get_outputs(running=next_running, forward_inputs):
        #   update_running_seqs(running, model_inputs)
        #   publish_forward_prefix_cache(...)             # 发布前缀缓存所有权
        #   forward_inputs, next_running = prefetch_next_inputs()
        #       └─ _send_next_inputs_impl(): 组「下一批」→ forward_async() → scheduler.tick()   ← 非阻塞预取
        #   out = await executor.get_output_async()       ← 阻塞等「当前批」输出
        #   release_forward_prefix_cache_saves(...)
        #   finish_forward_output(out, ...) → resp_queue.put_nowait(step_outputs)
    inputs_maker.deactivate_evict_seqs()                  # 收尾：去激活待驱逐序列
    has_runable_event.set()                               # 准备下一轮
```

数据流上，`forward_inputs` / `next_running` 这两个变量在循环里被「滚动复用」：每轮 `get_outputs` 返回的其实是**预取出的下一批**，直接喂给下一轮的 `activate_seqs`。

**`scheduler.tick()` 到底在哪里？** 在 [inputs_maker.py:1163](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L1163)，位于 `InputsMakerAsync._send_next_inputs_impl`。无论是「发当前批」（`send_next_inputs`）还是「预取下一批」（`prefetch_next_inputs`），最终都汇入这个 `_send_next_inputs_impl`，于是 **每次 forward 投递都会让 `scheduler_tick` 自增 1**。

#### 4.4.3 源码精读

**main_loop 本体**：

[engine_loop.py:472-517](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L472-L517) —— 主循环（中文说明：循环直到 `stop_event` 点亮；若 `next_running is None` 就调 `_main_loop_try_send_next_inputs` 取一批，仍为空则 `await __no_running_warning` 后 continue；随后 `activate_seqs` 激活序列、`_main_loop_get_outputs` 跑出输出并把「下一批」滚动回来，最后 `deactivate_evict_seqs` 收尾并 `has_runable_event.set()`）。

```python
while not self.stop_event.is_set():
    if next_running is None:
        forward_inputs, next_running = await self._main_loop_try_send_next_inputs()
        if next_running is None:
            if self._sleep_requested:
                continue
            await __no_running_warning()
            continue

    scheduler.activate_seqs(next_running)
    forward_inputs, next_running = await self._main_loop_get_outputs(
        running=next_running, forward_inputs=forward_inputs)
    self.inputs_maker.deactivate_evict_seqs()
    has_runable_event.set()
```

**`_main_loop_try_send_next_inputs`——没有活就挂起**：

[engine_loop.py:399-407](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L399-L407) —— 取下一批输入（中文说明：若 `has_unfinished()` 为假，就 `await has_runable_event.wait()` 挂起，直到 preprocess 点亮；随后调用 `inputs_maker.send_next_inputs()` 真正组装并投递 forward）。

```python
async def _main_loop_try_send_next_inputs(self):
    if not self.has_runable_event.has_unfinished():
        await self.has_runable_event.wait()
    if self._sleep_requested:
        return None, None
    self.scheduler.collect_migration_done()
    return await self.inputs_maker.send_next_inputs()
```

**`_main_loop_get_outputs`——重叠的核心**：

[engine_loop.py:447-470](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L447-L470) —— 取输出并预取（中文说明：先 `update_running_seqs`；`_publish_forward_prefix_cache` 把前缀缓存所有权提前发布；**接着 `_prefetch_next_inputs()` 把下一批输入投递出去（内部非阻塞 forward_async）**；**然后才 `await executor.get_output_async()` 阻塞等当前批的输出**——预取与等待并行；最后释放前缀缓存引用、`_finish_forward_output` 把输出塞进 `resp_queue`）。

```python
self._publish_forward_prefix_cache(running, has_state_checkpoint_save)
forward_inputs, next_running = await self._prefetch_next_inputs()
out = await self.executor.get_output_async()
self._release_forward_prefix_cache_saves(running)
self._finish_forward_output(out, running, model_inputs, delta)
del out
return forward_inputs, next_running
```

**`scheduler.tick()` 的真正位置**——`_send_next_inputs_impl`：

[inputs_maker.py:1151-1165](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L1151-L1165) —— 投递并计步（中文说明：`_make_forward_inputs` 组出输入；`executor.forward_async` 非阻塞投递给 ModelAgent；记录本次 forward 类型；**`scheduler.tick()` 给调度器步数 `scheduler_tick` 自增 1**；返回这批输入与参与序列）。

```python
async def _send_next_inputs_impl(self, prefill=None, enable_empty=False):
    forward_inputs = self._make_forward_inputs(prefill, enable_empty)
    if forward_inputs is None:
        return None, None
    next_running = forward_inputs.pop('running')
    inputs = forward_inputs['inputs']
    ...
    await self.executor.forward_async(forward_inputs)
    self._last_forward_kind = self._forward_kind(inputs, forward_inputs['delta'])
    self.scheduler.tick()
    self.forward_inputs = forward_inputs
    return forward_inputs, next_running
```

**`tick()` 本体**——只是个计数器，但语义重要：

[scheduler.py:143-145](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L143-L145) —— 计步（中文说明：每被调用一次就让 `scheduler_tick` 加 1，标记「又完成了一次 forward 派发」，供调度策略与统计按步推进）。

```python
def tick(self):
    """Mark one scheduler progress step (once per forward dispatch)."""
    self.scheduler_tick += 1
```

最后，`_finish_forward_output` 把产出交给 `send_response_loop`：

[engine_loop.py:436-445](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L436-L445) —— 发布输出（中文说明：`out` 为空直接返回；否则用 `_make_infer_outputs` 把 batch 输出拆成「按 session_id 索引的 `InferOutput` 字典」，`put_nowait` 投进 `resp_queue`，供 send_response_loop 消费）。

```python
def _finish_forward_output(self, out, running, model_inputs, delta):
    if out is None:
        return
    step_outputs = self._make_infer_outputs(out, running=running, model_inputs=model_inputs, delta=delta)
    self.resp_queue.put_nowait(step_outputs)
```

#### 4.4.4 代码实践

**实践目标（本讲必做的核心实践）**：画出 EngineLoop 中「输入预处理 → 调度 → forward → 输出回送」的协程协作图，并在源码中定位 `scheduler.tick()` 的调用位置。

**操作步骤**：

1. **画协作图**。结合本节 4.4.2 的伪代码与 4.1.3 的全景图，自己在纸上画一张包含四个泳道（preprocess_loop / main_loop / ModelAgent / send_response_loop）的时序图，标注：
   - `req_manager.step()`（preprocess → 调度器）
   - `has_runable_event.set/wait`（preprocess ↔ main_loop）
   - `forward_async`（main_loop → ModelAgent，非阻塞）
   - `scheduler.tick()`（main_loop 内，投递后立即调用）
   - `get_output_async`（main_loop ← ModelAgent，阻塞）
   - `resp_queue.put_nowait`（main_loop → send_response_loop）
   - `resp.event.set()`（send_response_loop → 客户端）
2. **定位 `scheduler.tick()`**。在仓库根目录用搜索工具查 `tick()` 的调用（参考命令：`grep -rn "\.tick()" lmdeploy/pytorch/engine lmdeploy/pytorch/paging`），确认它**不在 `engine_loop.py`**，而在 [inputs_maker.py:1163](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L1163)。
3. **追问一次为什么**：阅读 [inputs_maker.py:1167-1175](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L1167-L1175)，确认 `send_next_inputs` 和 `prefetch_next_inputs` 都最终调到 `_send_next_inputs_impl`，因此**每步 forward（含预取）都会让 `scheduler_tick` +1**。

**需要观察的现象**：你会注意到「投递 forward」与「计步」是紧挨在一起发生的——这说明 `scheduler_tick` 度量的是「调度器向外派发了多少批 forward」，而不是「完成了多少批」。完成与否要等 `get_output_async` 返回才知道。

**预期结果**：得到一张清晰的协作时序图，并能用一句话回答「`scheduler.tick()` 在哪、为什么在那」：它在 `InputsMakerAsync._send_next_inputs_impl` 投递 forward 之后调用，因为「派发即计步」。

> 若需运行验证（需 GPU）：可用 `LMDEPLOY_LOG_LEVEL=DEBUG` 跑一次推理，在日志中观察 `Sending forward inputs:` 与 `Forward session_ids:`（来自 inputs_maker）和 `Response: num_outputs=`（来自 `_log_resps`）的交替出现，它们分别对应「投递」与「回收」两端。

#### 4.4.5 小练习与答案

**练习 1**：在 `_main_loop_get_outputs` 中，为什么 `prefetch_next_inputs()` 要写在 `get_output_async()` **之前**？调换顺序会怎样？
**答案**：写在之前，是为了在「阻塞等待当前批输出」的这段 GPU 计算时间里，CPU 并行把下一批输入投递出去，实现重叠。若调换顺序，就会变成「先等当前批算完，再开始准备下一批」，CPU 与 GPU 串行，中间出现气泡，吞吐下降。

**练习 2**：`forward_inputs` 和 `next_running` 这两个变量在 `main_loop` 里是如何「滚动」到下一轮的？
**答案**：`_main_loop_get_outputs` 返回的 `forward_inputs, next_running` 其实是它内部 `_prefetch_next_inputs()` 取到的**下一批**（而不是刚跑完的当前批）。下一轮循环直接用这两个值进入 `activate_seqs(next_running)`，于是「预取的下一批」无缝变成「本轮的当前批」，无需重新组装。

**练习 3**：`scheduler.tick()` 只做 `scheduler_tick += 1`，这么「轻」的调用为什么值得单独讲？
**答案**：它的价值不在做了什么，而在**在哪里被调用、调用频率代表什么**。它被放在「每次 forward 派发」的必经之路上（`_send_next_inputs_impl`），所以 `scheduler_tick` 是度量「调度器已派发多少步 forward」的权威计数，调度策略与统计都依赖它按步推进。定位它等于定位了「调度与 forward 的衔接点」。

---

### 4.5 send_response_loop：把推理产出送回客户端

#### 4.5.1 概念说明

forward 跑完、`_make_infer_outputs` 把 batch 输出拆成了「按 session_id 索引的 `InferOutput` 字典」之后，谁来把它变成用户能收到的响应？这就是 **`send_response_loop`** 的活。它从 `resp_queue` 里取产出，调 `_send_resps` → `_send_resp`，最终通过 `req_manager.response(resp)` 点亮 `resp.event`——而等待这个 event 的正是 `EngineInstance.stream_infer`（或 AsyncEngine）。event 一亮，用户那一侧的 `await` 就返回，token 就被 yield 出去了。

为什么要单独开一条协程来回送响应，而不是在 `main_loop` 里直接回送？因为「回送」可能涉及 logprobs 装配、按 session 去重、状态判定（FINISH/SUCCESS）等逻辑，且要尽快让用户拿到 token；把它从 forward 主循环里剥离出来，main_loop 就能更专注于「调度 + 跑模型」，互不拖累。

#### 4.5.2 核心流程

```text
send_response_loop:
  while not stop_event:
      num_outs = resp_queue.qsize()
      if num_outs > 0:
          resps = [] ; 把队列里现有的全部取出合并   # 批量取，减少协程切换
      else:
          resps = (await resp_queue.get()).values()  # 没有就阻塞等一个
      _send_resps(resps):
          _log_resps / _update_logprobs
          对 step_outputs 按 session_id 去重（reversed，保留最新）
          对每个 out 调 _send_resp(out):
              判 resp_type: FINISH / (is_done ? type : SUCCESS)
              response_reqs(req_manager, out.resp, resp_type, data=...)
                  └─ req_manager.response(resp) → resp.event.set()   ← 点亮，唤醒客户端等待
```

#### 4.5.3 源码精读

**`send_response_loop` 本体**：

[engine_loop.py:260-271](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L260-L271) —— 回送协程（中文说明：循环到 `stop_event` 点亮；若 `resp_queue` 里有积压，就一次性把现有的全部 `get_nowait` 合并成 `resps`（批量处理，减少唤醒次数），否则 `await que.get()` 阻塞等一个；再交给 `_send_resps` 派发）。

```python
async def send_response_loop(self):
    que = self.resp_queue
    while not self.stop_event.is_set():
        num_outs = que.qsize()
        if num_outs > 0:
            resps = []
            for _ in range(num_outs):
                resps += que.get_nowait().values()
        else:
            resps = (await que.get()).values()
        self._send_resps(resps)
```

**`_send_resps`——按 session 去重**：

[engine_loop.py:248-258](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L248-L258) —— 批量回送（中文说明：先记日志、补 logprobs；用 `is_done` 集合对 `step_outputs` 倒序遍历去重——同一 session 只回送一次（保留最新产出），避免一个 session 在同一批里被重复回送）。

```python
def _send_resps(self, step_outputs):
    self._log_resps(step_outputs)
    self._update_logprobs(step_outputs)
    is_done = set()
    for out in reversed(step_outputs):
        if out.session_id in is_done:
            continue
        is_done.add(out.session_id)
        self._send_resp(out)
```

**`_send_resp`——判定类型并真正点亮 event**：

[engine_loop.py:213-231](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L213-L231) —— 单条回送（中文说明：依据 `out.finish` 与 `out.resp.is_done` 判定 `resp_type`（FINISH / 已完成的既有类型 / SUCCESS）；把 token_ids、logits、cache_block_ids、req_metrics 等打包成 `data`，调 `response_reqs` 写回 resp 并触发回送）。

```python
def _send_resp(self, out):
    logprobs = None if out.resp.data is None else out.resp.data.get('logprobs', None)
    if out.finish:
        resp_type = ResponseType.FINISH
    elif out.resp.is_done:
        resp_type = out.resp.type
    else:
        resp_type = ResponseType.SUCCESS
    response_reqs(self.req_manager, out.resp, resp_type,
                  data=dict(token_ids=out.token_ids, logits=out.logits, ...))
```

**`response_reqs` 与 `req_manager.response()`——点亮 event 的最终动作**：

[engine.py:78-89](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L78-L89) 与 [request.py:374-376](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py#L374-L376) ——（中文说明：`response_reqs` 设置 resp 的类型、data、err_msg 后调 `req_manager.response(resp)`；后者只有一行 `resp.event.set()`，正是这一行唤醒了客户端侧 `await resp.event.wait()` 的等待，token 由此被 yield 给用户）。

```python
# engine.py
def response_reqs(req_manager, resp, resp_type, data=None, err_msg=''):
    if resp.type == ResponseType.FINISH:
        return
    resp.type = resp_type
    resp.data = data
    resp.err_msg = err_msg
    req_manager.response(resp)

# request.py
def response(self, resp):
    resp.event.set()
```

> 注意区分两个 `Response`：这里的 `out.resp` 是**引擎面**带 `asyncio.Event` 的 `Response`（定义在 `engine/request.py`），与 u2-l1 讲过的**用户面** `lmdeploy.messages.Response` 不是同一个类。引擎面 Response 负责把每一步产出「推」回去，serve 层再把它装配成用户面 Response。这个区分在 u4-l1 已强调过。

#### 4.5.4 代码实践

**实践目标**：跟踪一个 token 从「ModelAgent 产出」到「客户端 yield」的完整一跳。

**操作步骤（源码阅读型）**：

1. 从 [engine_loop.py:445](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L445)（`resp_queue.put_nowait`）出发，沿 `resp_queue` → `send_response_loop` → `_send_resps` → `_send_resp` → `response_reqs` → `req_manager.response` → `resp.event.set()` 顺序阅读。
2. 想象客户端那侧：在 `EngineInstance.stream_infer`（下一讲 u4-l3 会细讲）里，用户代码 `await resp.event.wait()`，event 一亮就取出 `resp.data['token_ids']` 并 yield。
3. 思考：为什么 `_send_resps` 要用 `reversed` + `is_done` 去重？（提示：一批 step_outputs 里同一个 session 可能出现多次，但用户每步只想收到一份最新产出。）

**预期结果**：能口述「`put_nowait` → `get` → `_send_resp` → `resp.event.set()` → 客户端 `await` 返回」这条单跳链路，并指出「点亮 event」是跨越引擎内部协程与客户端等待的唯一动作。

#### 4.5.5 小练习与答案

**练习 1**：`send_response_loop` 在 `resp_queue` 有积压时会一次性 `get_nowait` 全部取出合并，为什么这样设计？
**答案**：批量处理可以减少协程唤醒与 `_send_resps` 调用次数。当 forward 很快、产出密集时，若每来一个就唤醒一次会引入大量协程切换开销；先攒再批处理能提升吞吐，且去重逻辑本来就需要「看到一批」才能正确按 session 合并。

**练习 2**：`_send_resp` 里 `resp_type` 有三种取值（FINISH / `out.resp.type` / SUCCESS），它们分别对应什么场景？
**答案**：`FINISH` 对应 `out.finish == True`（序列停止或待迁移，本轮是最后一次产出）；`out.resp.type`（`is_done` 分支）对应本次产出使序列刚好完成、沿用其既有类型；`SUCCESS` 对应普通的一步 token 产出（还在继续生成）。注意 `response_reqs` 里对 `FINISH` 类型会直接 return 不再回送，避免对已结束的 resp 重复写入。

---

## 5. 综合实践

把本讲的四条协程串起来，完成下面这个**「单 token 的一生」追踪任务**。这是把 preprocess → main → forward → send_response 四节知识融会贯通的练习。

**任务**：假设用户发起一次流式推理，发送了一条 prompt。请按时间顺序，列出「第一个生成 token」从被请求到被 yield 给用户，依次经过了哪些协程、哪些函数、哪些同步原语，并标注每一步对应的源码行号。

**建议产出格式**（示例骨架，请自行补全行号与说明）：

```text
1. 用户调用 stream_infer → RequestSender 把 ADD_MESSAGE 投进 RequestManager 队列
2. preprocess_loop 醒来：req_manager.step()                        (engine_loop.py:162)
   └─ process_request → Engine._on_add_message                      (engine.py:388)
        └─ 新建 SchedulerSequence，挂上 resp                          (engine.py:456-479)
3. preprocess_loop: has_runable_event.set()                         (engine_loop.py:163)
4. main_loop 醒来（此前在 has_runable_event.wait()）                (engine_loop.py:401-402)
   └─ _main_loop_try_send_next_inputs → inputs_maker.send_next_inputs
        └─ _send_next_inputs_impl: forward_async + scheduler.tick()  (inputs_maker.py:1161-1163)
5. main_loop: activate_seqs → _main_loop_get_outputs                (engine_loop.py:511-515)
   └─ prefetch_next_inputs（投下一批）→ await get_output_async（阻塞等当前批）(engine_loop.py:463-464)
6. ModelAgent 跑完 forward，forward_event.set()（计数 -1）            (agent.py:944)
7. get_output_async 返回 out → _finish_forward_output                (engine_loop.py:466)
   └─ _make_infer_outputs 拆成 InferOutput 字典 → resp_queue.put_nowait (engine_loop.py:445)
8. send_response_loop 取出 → _send_resps → _send_resp               (engine_loop.py:271, 258, 213)
   └─ response_reqs → req_manager.response → resp.event.set()       (request.py:376)
9. 客户端 await resp.event.wait() 返回，yield 出第一个 token
```

**进阶思考题**（可选）：

- 如果在第 5 步把 `prefetch_next_inputs` 和 `get_output_async` 的顺序对调，吞吐会怎样？为什么？
- `forward_event`（CounterEvent）在第 4、6 步分别被 `clear` / `set` 各一次，本步结束后它的 `is_set()` 应该是 True 还是 False？为什么？（答：True，因为一次 clear 配一次 set，计数回到 0。）

> 说明：本实践为源码阅读型，无需 GPU 即可完成；若要在真实运行中观察，开启 `LMDEPLOY_LOG_LEVEL=DEBUG` 后，上述步骤 2/4/7/8 都有对应的 debug/info 日志可对照。

## 6. 本讲小结

- `EngineLoop` 是 `Engine.async_loop()` 创建的「多协程总管」，在同一个 asyncio 事件循环里并发运行 `preprocess_loop` / `main_loop` / `send_response_loop` / `migration_loop` 四条核心协程（外加一条 `executor.wait_tasks`），各自节奏不同、互不阻塞。
- `main_loop` 是心脏，每跳一次完成「取输入 → 激活 → 预取下一批 + 阻塞等当前批输出 → 回收」；其中**预取在等待之前**，实现了 CPU 准备输入与 GPU 计算 forward 的重叠。
- 两个自定义同步原语：`CounterEvent`（带计数器的事件，N 次 clear 配 N 次 set 才点亮，用于追踪在途 forward）与 `RunableEventAsync`（`set()` 时主动查 `has_unfinished()` 决定亮灭，避免假唤醒）。
- `scheduler.tick()` **不在 `engine_loop.py`**，而在 [inputs_maker.py:1163](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L1163) 的 `_send_next_inputs_impl`，每次 forward 派发（含预取）都会让 `scheduler_tick` 自增——「派发即计步」。
- 响应回送由独立的 `send_response_loop` 负责：从 `resp_queue` 批量取产出 → `_send_resps` 按 session 去重 → `_send_resp` 判类型 → `response_reqs` → `resp.event.set()` 点亮事件，唤醒客户端等待。
- 关键区分：`forward_async`（非阻塞投递）vs `get_output_async`（阻塞等输出）；引擎面带 `asyncio.Event` 的 `Response` vs 用户面 `lmdeploy.messages.Response`。

## 7. 下一步学习建议

本讲把 `EngineLoop` 的内部协作讲透了，但还有两块留白，建议接下来按顺序补齐：

1. **u4-l3 引擎实例与流式推理 engine_instance**：本讲止步于「`resp.event.set()` 点亮事件」，但客户端那侧到底是怎么 `await`、怎么把 token yield 出去、怎么处理停止条件的？这就需要看 `EngineInstance` 如何包装单个推理实例。建议重点读 `engine_instance.py` 的 `stream_infer` 与 `input_process`/`inputs_maker` 对 prompt 的预处理。
2. **u4-l4 调度器 Scheduler：prefill 与 decode**：本讲里 `_send_next_inputs_impl` 调用的 `do_prefill()`、`activate_seqs`、`deactivate_evict_seqs` 都只是「入口」，真正的「哪些序列进 batch、做 prefill 还是 decode、长上下文怎么分块、显存不够时驱逐谁」都藏在 `paging/scheduler.py` 里。建议结合 u4-l5（BlockManager）一起读，理解 KV cache 的物理块如何与这里的调度步骤对应。
3. **延伸阅读**：若对 PD 分离感兴趣，可先读本讲提到的 `migration_loop`（[engine_loop.py:594-611](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L594-L611)）与 `drain_for_sleep`/`resume_from_sleep`（[engine_loop.py:165-198](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L165-L198)），它们展示了 EngineLoop 如何在 sleep/wakeup 与 KV 迁移场景下安全地排空与恢复流水线，是本讲同步原语的高级用法。
