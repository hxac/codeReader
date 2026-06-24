# 异步 API 内部实现与回调桥接

## 1. 本讲目标

本讲是「工程化与扩展」单元中深入异步机制的一篇。在 u2-l3 我们已经从**使用者**视角认识了 `async_translate` 的事件协议（`progress_start` / `progress_update` / `progress_end` / `finish` / `error`）。本讲要切换到**实现者**视角，钻进这三个文件的黑盒：

- `babeldoc/format/pdf/high_level.py`
- `babeldoc/asynchronize/__init__.py`
- `babeldoc/progress_monitor.py`

学完后你应该能够：

1. 说清 `async_translate` 如何用 `loop.run_in_executor` 把**同步**的 `do_translate` 丢进线程池，再把它发出的**同步回调**桥接成**异步事件流**。
2. 解释 `AsyncCallback` 为什么必须用 `call_soon_threadsafe` + `time.sleep(0.01)`，以及「先入队、再置 `finished`」的顺序为何能避免丢事件。
3. 区分两个事件：`cancel_event`（`threading.Event`，跨线程「停止」信号）与 `finish_event`（`asyncio.Event`，主循环「完成」握手），并指出 `on_finish` 为何恒置位于 `finally`。
4. 理解 `report_interval` 进度节流，以及分片翻译时 `create_part_monitor` 如何把进度切成「片」。

---

## 2. 前置知识

本讲默认你已掌握 u2-l3 建立的心智模型。为照顾从零开始的读者，这里用最朴素的方式补三个 Python 异步概念：

- **事件循环（event loop）**：asyncio 的「调度中心」，单线程运行，负责轮询就绪的协程。`await` 就是把控制权交还给它。它在自己的线程里跑，**不能被其它线程直接操作**——别的线程想往里塞东西，必须用线程安全的接口。
- **线程池（thread pool）**：`loop.run_in_executor(None, func, ...)` 会把一个**同步阻塞**函数交给默认线程池里的某个工人线程执行，立即返回一个 `Future`，不阻塞事件循环。这是「把同步代码挂到异步程序里」的标准做法。
- **跨线程唤醒**：当工人线程想通知事件循环「有新事件了」，但事件循环可能正在 `await` 睡觉，直接调它的 API 不安全。`loop.call_soon_threadsafe(fn, ...)` 是 asyncio 提供的**线程安全**入口，它会把 `fn` 排进事件循环的队列、必要时唤醒沉睡的循环。

还需要记住两个 BabelDOC 事实（来自 u2-l2 / u2-l3）：

- `do_translate` 是**同步**翻译总入口（`high_level.py`），真正的流水线在 `_do_translate_single`，它全程**同步**、阻塞，会调用 LLM、跑模型，可能耗时几分钟。
- `ProgressMonitor`（`progress_monitor.py`）是进度监控器，它持有两个回调槽：`progress_change_callback`（进度类事件）与 `finish_callback`（终态事件）。各 midend stage 通过 `stage_start` / `stage_update` / `stage_done` 向它汇报。

本讲的核心问题就是：**如何让一个同步、阻塞、跑在工人线程里的 `do_translate`，把它沿途发出的同步回调，变成主线程事件循环上可以 `async for` 的异步事件流？**

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `babeldoc/format/pdf/high_level.py` | 提供 `translate`（纯同步）、`do_translate`（同步主链路）、`async_translate`（异步事件流入口）三件套，是「同步 ↔ 异步」的接合处。 |
| `babeldoc/asynchronize/__init__.py` | `AsyncCallback`：把同步回调桥接为异步迭代器的「管道」，是本讲的真正主角。 |
| `babeldoc/progress_monitor.py` | `ProgressMonitor`：进度监控器，负责节流、加权、分片（part）进度，并在终态驱动 `cancel_event` / `finish_event`。 |
| `babeldoc/format/pdf/translation_config.py` | 提供 `report_interval` 等配置，决定节流间隔。 |
| `babeldoc/main.py` | `main()` 用 `create_progress_handler` 消费 `async_translate` 的事件流，渲染进度条——是这套机制的「下游消费者」示例。 |

---

## 4. 核心概念与源码讲解

### 4.1 同步转异步桥接：async_translate + AsyncCallback

#### 4.1.1 概念说明

BabelDOC 的翻译主链路 `do_translate` 是**纯同步**的——它要打开 PDF、调 ONNX 模型、发 HTTP 请求翻译，这些都会长时间阻塞线程。如果直接在 asyncio 事件循环里调用它，整个事件循环会被卡死，既没法报进度，也没法响应取消。

所以 `async_translate` 的设计是经典的「**同步内核 + 异步外壳**」：

- **异步外壳**：`async_translate` 是一个 `async` 生成器，跑在主线程事件循环里，负责 `yield` 事件给调用方（如 CLI 的进度条）。
- **同步内核**：`do_translate` 被丢进**线程池**的工人线程里跑，沿途通过回调汇报进度。
- **桥接管道**：`AsyncCallback` 把工人线程发出的同步回调，转换成主线程事件循环上可 `async for` 的异步事件流。

> 关键认知：翻译的「真活」在工人线程里同步进行，事件循环只负责「搬运事件」。两者靠一条 `asyncio.Queue` 连接。

#### 4.1.2 核心流程

```
主线程事件循环                        工人线程（线程池）
─────────────────                    ──────────────────
async_translate:
  loop = get_running_loop()
  callback = AsyncCallback()          （callback 持有 loop 引用）
  构造 ProgressMonitor，把两个回调
    槽接到 callback.step_callback
    和 callback.finished_callback
  future = loop.run_in_executor(  ──>  do_translate(pm, config):
                 do_translate, ...)        跑流水线，沿途调用：
                                            pm.stage_start(...)
                                            pm.stage_update(...)
                                              → progress_change_callback
                                                  = callback.step_callback
  async for event in callback:              ┌──────────────────────────┐
    yield event                  <───────    │ step_callback:           │
                                              │  loop.call_soon_threadsafe│
                                              │    (queue.put_nowait, ev)│
                                              │  time.sleep(0.01)        │
                                              └──────────────────────────┘
  ...（异常时置 cancel_event）
  await finish_event.wait()  <───────  finally: pm.on_finish()
                                          → loop.call_soon_threadsafe
                                              (finish_event.set)
```

#### 4.1.3 源码精读

先看 `async_translate` 全貌（含它自己写的、面向调用方的事件协议文档）：

[babeldoc/format/pdf/high_level.py:299-376](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L299-L376) —— 异步翻译入口，docstring 里就是事件协议契约（`progress_start`/`progress_update`/`progress_end`/`finish`/`error` 五种事件及其字段）。

核心装配在 347–361 行：

```python
loop = asyncio.get_running_loop()
callback = asynchronize.AsyncCallback()

finish_event = asyncio.Event()
cancel_event = threading.Event()
with ProgressMonitor(
    get_translation_stage(translation_config),
    progress_change_callback=callback.step_callback,   # 进度类事件 → step_callback
    finish_callback=callback.finished_callback,         # 终态事件 → finished_callback
    finish_event=finish_event,
    cancel_event=cancel_event,
    loop=loop,
    report_interval=translation_config.report_interval,
) as pm:
    future = loop.run_in_executor(None, do_translate, pm, translation_config)
```

要点逐条对应：

- `asyncio.get_running_loop()` 拿到**当前**事件循环，稍后传给 `AsyncCallback` 与 `ProgressMonitor`，用于跨线程唤醒。
- `progress_change_callback=callback.step_callback`、`finish_callback=callback.finished_callback`：把 `ProgressMonitor` 的两个回调槽接到桥接管道上。stage 汇报进度 → `step_callback` 入队；`do_translate` 报完成/出错 → `finished_callback` 入队。
- `loop.run_in_executor(None, do_translate, pm, translation_config)`：把同步的 `do_translate` 交给默认线程池，**立即返回 future，不阻塞**。翻译真活从此刻起在工人线程里跑。
- 注意两个事件的**类型差异**：`finish_event = asyncio.Event()`（异步，给主循环用），`cancel_event = threading.Event()`（同步，给工人线程用）。这个区分是 4.2 的主题。

随后主循环进入消费循环：

```python
try:
    async for event in callback:
        event = event.kwargs
        yield event
        if event["type"] == "error":
            break
except CancelledError:
    cancel_event.set()
except KeyboardInterrupt:
    logger.info("Translation cancelled by user through keyboard interrupt")
    cancel_event.set()
```

- `async for event in callback`：`callback` 是个异步可迭代对象（见 4.1.4 对 `AsyncCallback` 的剖析），每次 `await` 会从内部队列取一个事件。
- `yield event`：把事件转发给 `async_translate` 的调用方（CLI 进度条就是在这里收到事件的）。
- 收到 `error` 立即 `break`：错误后不再消费后续事件。
- **取消传播**：若调用方取消了本协程（`CancelledError`），或用户按了 Ctrl+C（`KeyboardInterrupt`），就 `cancel_event.set()`——这会把「停止」信号传进工人线程（4.2 详述）。

循环结束后还有一句关键的收尾握手：

```python
if cancel_event.is_set():
    future.cancel()
logger.info("Waiting for translation to finish...")
await finish_event.wait()
```

`await finish_event.wait()` 会**阻塞主循环，直到工人线程通过 `on_finish` 置位 `finish_event`**。这保证：即便用户已经取消、主循环不再 `yield`，也会**等工人线程把 `do_translate` 的 `finally` 块跑完**（清理临时文件、关闭线程池）才返回。这是「优雅退出」的护城河。

> 旁注：还有一个**纯同步**入口 `translate`，它构造 `ProgressMonitor` 时**不传任何回调**，所以根本不发事件，直接返回 `TranslateResult`：
> [babeldoc/format/pdf/high_level.py:259-261](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L259-L261)。这正是 u2-l3 提到的「`translate()` 只返回结果、不发事件」的来源。

现在看桥接管道 `AsyncCallback` 的实现（全文件只有 52 行，是本讲真正的核心）：

[babeldoc/asynchronize/__init__.py:11-51](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/asynchronize/__init__.py#L11-L51) —— 同步回调 → 异步迭代器的桥接器。

```python
class AsyncCallback:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.finished = False
        self.loop = asyncio.get_event_loop()

    def step_callback(self, *args, **kwargs):
        args = Args(args, kwargs)
        self.loop.call_soon_threadsafe(self.queue.put_nowait, args)
        time.sleep(0.01)

    def finished_callback(self, *args, **kwargs):
        if self.finished:
            return
        self.step_callback(*args, **kwargs)
        self.finished = True
    ...
    async def __anext__(self):
        if self.finished and self.queue.empty():
            raise StopAsyncIteration
        result = await self.queue.get()
        return result
```

逐点拆解为什么这样写：

1. **持有 loop 引用**（`self.loop`）：因为 `step_callback` 是被**工人线程**调用的，它必须用 `loop.call_soon_threadsafe` 才能安全地操作属于主线程的 `asyncio.Queue`。源码注释直接引用了 StackOverflow 的经典回答说明「事件循环可能在睡觉，必须用线程安全版本唤醒」。
2. **`time.sleep(0.01)`**：故意让工人线程睡 10ms，**主动释放 GIL**，给事件循环留出处理消息的窗口。没有它，工人线程可能连续回调、长时间霸占 GIL，导致主循环来不及取事件、进度条卡顿。
3. **「先入队、再置 `finished`」**（`finished_callback` 第 33–34 行）：先把终态事件 `step_callback` 入队，**然后**才 `self.finished = True`。顺序不能反——若先置 `finished`，极端情况下 `__anext__` 可能在事件入队前就看到 `finished and queue.empty()` 而提前 `StopAsyncIteration`，**丢掉最后的 `finish`/`error` 事件**。
4. **`if self.finished: return` 幂等保护**（第 31–32 行）：终态回调可能被多次触发（见 4.2 中 `on_finish` 的行为），这个守卫保证只入队一次终态事件。
5. **`__anext__` 的「排空」语义**（第 47–48 行）：即使 `finished` 已置位，只要队列里还有事件就继续取，确保**所有已入队事件都被消费完**才结束迭代。

`Args` 只是把 `(*args, **kwargs)` 打包成一个对象塞进队列；消费侧 `async_translate` 用 `event.kwargs` 取回字典（[high_level.py:364](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L364)）。

#### 4.1.4 代码实践

**实践目标**：脱离 BabelDOC 的完整流水线，用最小代码复现「工人线程发同步回调 → 主循环 `async for` 取异步事件」的桥接模式，亲眼看到 `call_soon_threadsafe` + `time.sleep` 的作用。

**操作步骤**（以下为**示例代码**，可直接保存为 `bridge_demo.py` 用 `python bridge_demo.py` 运行，不依赖 BabelDOC、不需 API key）：

```python
# 示例代码：复刻 AsyncCallback 桥接模式的最小演示
import asyncio
import time

class Args:
    def __init__(self, kwargs):
        self.kwargs = kwargs

class AsyncCallback:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.finished = False
        self.loop = asyncio.get_event_loop()

    def step_callback(self, **kwargs):
        self.loop.call_soon_threadsafe(self.queue.put_nowait, Args(kwargs))
        time.sleep(0.01)  # 释放 GIL，让事件循环有机会处理

    def finished_callback(self, **kwargs):
        if self.finished:
            return
        self.step_callback(**kwargs)
        self.finished = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.finished and self.queue.empty():
            raise StopAsyncIteration
        return (await self.queue.get()).kwargs

def worker(callback):  # 模拟 do_translate：在工人线程里同步干活并回调
    for i in range(5):
        time.sleep(0.2)            # 模拟耗时阶段
        callback.step_callback(type="progress_update", step=i)
    callback.finished_callback(type="finish", result="done")

async def main():
    callback = AsyncCallback()
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, worker, callback)  # 同步内核丢进线程池
    async for event in callback:                            # 异步外壳消费事件
        print("收到事件:", event)
    await future

asyncio.run(main())
```

**需要观察的现象**：

- 主线程的 `print` 会**穿插在** `worker` 的 `time.sleep(0.2)` 之间逐条打印，说明事件是「边产生边消费」的，而非等工人线程全跑完才一次性吐出。
- 若把 `step_callback` 里的 `time.sleep(0.01)` 注释掉，多跑几次可能观察到事件打印**扎堆**出现（GIL 未及时让出），这正是源码加这句 `sleep` 的动机。

**预期结果**：依次打印 5 条 `progress_update`（step 0–4）后打印 1 条 `finish`，程序正常退出。**若你无法本地运行，明确标注「待本地验证」。**

#### 4.1.5 小练习与答案

**练习 1**：如果把 `finished_callback` 里的两句调换顺序（先 `self.finished = True`，再 `self.step_callback(...)`），可能出现什么 bug？

**参考答案**：可能丢掉最后的终态事件。因为置 `finished` 后、入队前存在一个时间窗，此时若主循环的 `__anext__` 恰好执行，会看到 `finished and queue.empty()` 为真而提前 `raise StopAsyncIteration`，导致 `finish`/`error` 事件永远不被消费。这正是源码坚持「先入队、再置位」的原因。

**练习 2**：`translate`（同步入口）和 `async_translate`（异步入口）都调用了 `do_translate`，二者给 `ProgressMonitor` 传的回调有什么区别？为什么？

**参考答案**：`translate` 构造 `ProgressMonitor` 时**不传** `progress_change_callback` 和 `finish_callback`（见 [high_level.py:259-261](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L259-L261)），所以 stage 汇报时 `if self.progress_change_callback:` 判断为假、不发任何事件，函数直接返回 `TranslateResult`。`async_translate` 则把两个槽接到 `AsyncCallback`，把回调桥接成事件流。区别源于用途：前者只要结果，后者要「边翻译边报进度」。

---

### 4.2 取消与完成事件：cancel_event 与 finish_event 的协作

#### 4.2.1 概念说明

异步程序必须回答两个问题：**怎么停**（用户中途取消）和**怎么知道结束**（翻译跑完了）。BabelDOC 用**两个不同种类的事件对象**分别承担，这是一个容易被忽略但非常关键的设计：

| 对象 | 类型 | 所在空间 | 用途 |
| --- | --- | --- | --- |
| `cancel_event` | `threading.Event` | **同步**世界 | 工人线程里的 stage 轮询它判断「是否该停」 |
| `finish_event` | `asyncio.Event` | **异步**世界 | 主循环 `await` 它等待「翻译彻底结束」 |

为什么不能合二为一？因为 `threading.Event` 的 `.set()` 可以从任意线程调用、对工人线程友好，但**不能**直接 `await`；`asyncio.Event` 可以 `await wait()`，但**只能**在事件循环所在线程操作。两者职责正交，故各用一种。

还有一个关键事实：`do_translate` 把 `pm.on_finish()` 放在 `finally` 块里，**无论成功、失败、取消都会执行**——它是「完成握手」的唯一可靠出口。

#### 4.2.2 核心流程

完成握手的三方时序：

```
工人线程 do_translate:
  正常完成 → pm.translate_done(result)        # 入队 finish 事件
  出错     → pm.translate_error(e)             # 入队 error 事件
  ────────────────────────────────────────────
  finally: pm.on_finish()                      # 恒执行
              ├─ cancel_event.set()             # 置位取消信号
              ├─ loop.call_soon_threadsafe(
              │     finish_event.set)          # 跨线程置位完成信号
              └─ 若 cancel_event 已置 → finish_callback(error=CancelledError)
                                        （被 finished 守卫挡掉，见 4.2.3）

主线程 async_translate:
  await finish_event.wait()   # 阻塞，直到上面的 finish_event.set() 生效
```

取消握手（用户中途取消）：

```
主线程:
  async for 被 CancelledError / KeyboardInterrupt 打断
    → cancel_event.set()                       # 把「停」信号传进工人线程

工人线程:
  各 stage 在循环里调 pm.raise_if_cancelled()
    → 发现 cancel_event.is_set() → raise CancelledError
  → do_translate 捕获 → pm.translate_error(e) → finally: on_finish()
```

#### 4.2.3 源码精读

两个事件在 `async_translate` 里创建并注入 `ProgressMonitor`：

[babeldoc/format/pdf/high_level.py:350-361](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L350-L361) —— `finish_event`（asyncio）与 `cancel_event`（threading）的创建与注入。

`ProgressMonitor.__init__` 把它们存起来（注意校验：`finish_event` 必须配 `loop`）：

[babeldoc/progress_monitor.py:52-57](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L52-L57) —— 存储两个事件并校验 `finish_event requires a loop`。

核心是 `on_finish`，它同时操作两个事件：

[babeldoc/progress_monitor.py:139-147](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L139-L147) —— 完成握手：置 `cancel_event`、跨线程置 `finish_event`、并尝试发 `CancelledError` 终态。

```python
def on_finish(self):
    if self.disable or self.parent_monitor and self.parent_monitor.disable:
        return
    if self.cancel_event:
        self.cancel_event.set()
    if self.finish_event and self.loop:
        self.loop.call_soon_threadsafe(self.finish_event.set)
    if self.cancel_event and self.cancel_event.is_set():
        self.finish_callback(type="error", error=CancelledError)
```

三个细节务必看懂：

1. **`cancel_event` 被无条件置位**：哪怕是正常完成，`on_finish` 也会 `cancel_event.set()`。这看似奇怪，其实无害——因为下一步要用 `finish_callback` 发一个 `CancelledError`，但会被 `AsyncCallback.finished_callback` 的 `if self.finished: return` 守卫挡掉（正常路径里 `translate_done` 已经先把 `finish` 事件入队并置 `finished=True`，见 [progress_monitor.py:237-241](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L237-L241)）。所以正常完成**不会**误发 `error`。
2. **`finish_event` 必须跨线程置位**：`on_finish` 跑在工人线程，而 `finish_event` 属于主线程的 asyncio 世界，所以用 `loop.call_soon_threadsafe(self.finish_event.set)`——这正是主循环 `await finish_event.wait()` 能被唤醒的原因。
3. **`on_finish` 在 `finally` 里恒执行**：

[babeldoc/format/pdf/high_level.py:730-733](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L730-L733) —— `do_translate` 的 `finally` 块恒调 `pm.on_finish()` 与 `cleanup_temp_files()`，保证完成握手与清理必发生。

而成功 / 失败两条终态路径分别由这两个方法入队事件：

[babeldoc/progress_monitor.py:237-248](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L237-L248) —— `translate_done` 发 `finish`，`translate_error` 发 `error`，二者都走 `finish_callback`（即 `AsyncCallback.finished_callback`）。

它们在 `do_translate` 里的调用点：

- 成功：[high_level.py:719](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L719) `pm.translate_done(result)`
- 失败：[high_level.py:728](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L728) `pm.translate_error(e)`（在 `except Exception` 里）

最后看「取消」方向。工人线程里的 stage 通过下面两个方法轮询 / 触发取消：

[babeldoc/progress_monitor.py:250-259](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L250-L259) —— `raise_if_cancelled`（stage 循环里主动抛 `CancelledError`）与 `cancel`（外部触发取消）。

主循环侧，取消信号来源于 `async_translate` 的 `except`：

[babeldoc/format/pdf/high_level.py:368-374](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L368-L374) —— 捕获 `CancelledError` / `KeyboardInterrupt` 后 `cancel_event.set()`，把停止信号传进工人线程；随后若 `cancel_event.is_set()` 则 `future.cancel()`。

> 小结：`cancel_event` 是「跨线程的停止开关」，由主循环在取消时置位、由工人线程的 stage 轮询；`finish_event` 是「跨线程的完成信号」，由工人线程的 `on_finish` 跨线程置位、由主循环 `await` 等待。两者一停一收，构成了优雅退出。

#### 4.2.4 代码实践

**实践目标**：在源码层面验证「正常完成时 `on_finish` 试图发的 `CancelledError` 为何不会污染事件流」。

**操作步骤**（源码阅读型实践）：

1. 打开 [progress_monitor.py:139-147](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L139-L147) 的 `on_finish`，确认它**无条件**调 `self.finish_callback(type="error", error=CancelledError)`。
2. 跟踪 `finish_callback` 的真实实现 `AsyncCallback.finished_callback`（[asynchronize/__init__.py:28-34](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/asynchronize/__init__.py#L28-L34)），看 `if self.finished: return` 守卫。
3. 再看正常路径里 `translate_done`（[progress_monitor.py:237-241](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L237-L241)）会先调 `finish_callback(type="finish", ...)`，而 `finished_callback` 内部先 `step_callback`（入队）再置 `self.finished = True`。

**需要观察的现象 / 预期结果**：在脑中（或纸上）排出执行顺序——正常完成时 `translate_done` 先把 `finish` 事件入队并置 `finished=True`，随后 `on_finish` 想再发 `error=CancelledError`，但被 `if self.finished: return` 挡掉。于是事件流以**一个 `finish` 事件**干净收尾，不会有误发的 `error`。若顺序颠倒（`translate_done` 在 `on_finish` 之后），就会看到误发的 `CancelledError`——这从反面印证了「`translate_done` 必须在 `finally`/`on_finish` 之前被调用」的隐含约束。**此结论待本地用调试器单步验证。**

#### 4.2.5 小练习与答案

**练习 1**：为什么 `finish_event` 用 `asyncio.Event` 而 `cancel_event` 用 `threading.Event`？互换会怎样？

**参考答案**：`finish_event` 需要在主循环里 `await wait()`，必须用 `asyncio.Event`（且只能由事件循环所在线程操作，故 `on_finish` 用 `call_soon_threadsafe` 跨线程置位）。`cancel_event` 需要在工人线程的 stage 循环里高频轮询 `is_set()`，`threading.Event` 对任意线程友好、轻量。互换后：用 `threading.Event` 无法 `await`（主循环只能忙等或轮询，浪费）；用 `asyncio.Event` 在工人线程里直接 `.set()` 会破坏 asyncio 的线程安全约束。

**练习 2**：用户在翻译中途按 Ctrl+C，从主循环到工人线程停止，`cancel_event` 经历了哪几步？

**参考答案**：① 主循环的 `async for` 抛 `KeyboardInterrupt` → ② `async_translate` 的 `except KeyboardInterrupt` 分支 `cancel_event.set()`（[high_level.py:370-372](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L370-L372)）→ ③ 工人线程里正在跑的 stage 在循环中调 `pm.raise_if_cancelled()`（[progress_monitor.py:250-252](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L250-L252)）发现已置位 → 抛 `CancelledError` → ④ `do_translate` 捕获后 `translate_error`、`finally` 里 `on_finish` 置 `finish_event` → ⑤ 主循环 `await finish_event.wait()` 解除阻塞后退出。

---

### 4.3 进度节流：report_interval 与加权进度

#### 4.3.1 概念说明

翻译一个大 PDF，单个 stage（比如 `ILTranslator`）可能要处理成千上万个段落。如果每翻译一个段落就发一个 `progress_update` 事件，会有两个问题：

1. **事件洪流**：工人线程疯狂回调、主循环疯狂渲染进度条，CPU 和 GIL 争抢严重。
2. **无意义刷新**：人眼分辨不出 99.1% 和 99.2% 的差别，刷太快纯属浪费。

所以 `ProgressMonitor` 做了**时间节流**：两次 `progress_update` 之间至少间隔 `report_interval` 秒（默认 0.1s，即每秒至多约 10 次）。同时，进度不是简单数「完成了几个 stage」，而是按 `TRANSLATE_STAGES` 里每个 stage 的**权重**加权——这样耗时的 `ILTranslator`（权重 46.96）能占据进度条近一半，符合直觉。

#### 4.3.2 核心流程

节流判断在每个 stage 的 `advance` → `stage_update` 里发生：

```
stage.advance(n):
  current += n
  pm.stage_update(stage, n):
    delta = now - last_report_time
    if delta < report_interval and stage.total > 3:   # 节流：太近就跳过
        return                                        # 不发事件
    发 progress_update 事件
    last_report_time = now
```

加权进度的数学表达：设各 stage 的归一化权重为 \(w_i\)（满足 \(\sum w_i = 1\)），则总进度为已完成 stage 的权重和加上当前 stage 的部分进度：

\[
\text{overall} = 100 \times \left( \sum_{i \in \text{已完成}} w_i \;+\; w_{\text{当前}} \cdot \frac{\text{current}}{\text{total}} \right)
\]

> 注：`stage.total > 3` 这个条件意味着**总量很小的 stage 不节流**——因为小 stage 本来事件就少，节流反而可能让它一个事件都发不出来。

#### 4.3.3 源码精读

`report_interval` 从配置流入 `ProgressMonitor`。配置侧默认值：

[babeldoc/format/pdf/translation_config.py:186](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L186) —— `report_interval: float = 0.1`，并在 [translation_config.py:270](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L270) 赋给 `self.report_interval`。

注入处（`async_translate` 构造 `ProgressMonitor` 时）：

[babeldoc/format/pdf/high_level.py:359](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L359) —— `report_interval=translation_config.report_interval`。

节流核心逻辑在 `stage_update`：

[babeldoc/progress_monitor.py:214-235](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L214-L235) —— 节流判断与 `progress_update` 事件构造。

```python
def stage_update(self, stage, n: int):
    ...
    report_time_delta = time.time() - self.last_report_time
    if report_time_delta < self.report_interval and stage.total > 3:
        return
    if self.progress_change_callback:
        ...
        self.progress_change_callback(
            type="progress_update",
            stage=stage.display_name,
            stage_progress=stage_progress,
            ...
            overall_progress=self.calculate_current_progress(stage),
            ...
        )
        self.last_report_time = time.time()
```

要点：

- `last_report_time` 在 `stage_start`（[progress_monitor.py:130](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L130)）和 `stage_done`（[progress_monitor.py:152](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L152)）都被重置为 `0.0`，保证**每个 stage 的第一个和最后一个 `update` 一定能发出来**（因为 `0 - 0` 不满足 `< report_interval` 的「距上次太近」语义被绕过，且重置后首帧 delta 足够大）。
- `overall_progress` 由 `calculate_current_progress(stage)` 算出，它遍历所有 stage、把已完成的按权重累加、再加上当前 stage 的部分进度，对应上面的公式。

权重来自 `TRANSLATE_STAGES`，在 `ProgressMonitor.__init__` 里归一化：

[babeldoc/progress_monitor.py:34-44](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L34-L44) —— 把 `stages` 的权重除以总和归一化，存进每个 `TranslationStage`。

另外，`ProgressMonitor` 构造时还会**立刻**发一个 `stage_summary` 事件，把所有 stage 及其占比告诉下游（让进度条提前知道总共有几段）：

[babeldoc/progress_monitor.py:58-70](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L58-L70) —— 构造期发 `stage_summary`，列出每个 stage 的名字与占比 `percent`。

#### 4.3.4 代码实践

**实践目标**：直观感受 `report_interval` 对事件密度的影响。

**操作步骤**（源码阅读 + 参数实验型）：

1. 阅读上面的节流逻辑，确认：总量 `> 3` 的 stage，相邻两次 `progress_update` 至少间隔 `report_interval` 秒。
2. 若本地有可翻译的 PDF 与 API key：用两种配置翻译同一个 PDF，对比事件数量——
   - `babeldoc --report-interval 0.001 ...`（几乎不节流）
   - `babeldoc --report-interval 1.0 ...`（每秒至多 1 次）
3. 若无可运行环境：在 4.1.4 的 `bridge_demo.py` 里给 `worker` 的回调加节流计数（用一个全局计数器统计 `step_callback` 被调用次数 vs 实际被 `async for` 消费次数），观察当 `worker` 连续回调时，节流能砍掉多少事件。**无运行环境时标注「待本地验证」。**

**预期结果**：`report_interval` 越大，单位时间内被消费的 `progress_update` 越少、进度条越「平滑」但越「迟钝」；越小事件越密、越精细但 CPU 开销越大。默认 `0.1` 是体验与开销的折中。

#### 4.3.5 小练习与答案

**练习 1**：为什么节流条件里要有 `and stage.total > 3`？去掉它会怎样？

**参考答案**：总量很小的 stage（比如只有 1–3 个 item）本来事件就极少，若还套用「距上次不足 0.1s 就跳过」，可能导致它**一个 `progress_update` 都发不出来**，进度条上看不到这个 stage 的过程。加 `stage.total > 3` 只对「长 stage」节流，短 stage 放行，兼顾刷新频率与可见性。

**练习 2**：`stage_start` 和 `stage_done` 里都把 `last_report_time` 重置为 `0.0`，这一步对节流有什么影响？

**参考答案**：重置为 `0.0` 后，`time.time() - 0.0` 是一个很大的正数，必然 `>= report_interval`，于是 stage 的**首个 `stage_update`** 不会被节流挡掉；同时也保证 `stage_start` 发出的 `progress_start` 之后能尽快跟上一个真实的 `progress_update`。这避免了「stage 刚开始时进度条卡住不动」的观感。

---

### 4.4 分片进度监控：create_part_monitor 与 part_offset

#### 4.4.1 概念说明

承接 u8-l2 的分片翻译：当 PDF 太长、用 `--max-pages-per-part` 切成多片串行翻译时，进度该怎么算？如果每片都从 0% 重新涨到 100%，进度条会来回跳，体验很差。

BabelDOC 的解法是**父子 ProgressMonitor**：

- **父 monitor**（`async_translate` 创建的那个）：负责全局进度 `[0, 100]`，它的回调槽接 `AsyncCallback`。
- **子 monitor（part monitor）**：每片翻译时由 `create_part_monitor` 创建，它的回调槽接到父 monitor 的 `_handle_part_progress` / `_handle_part_finish`。
- **part_offset**：每片只占全局进度的一段。第 \(i\) 片（从 0 计）的全局起点是 \(i \times \frac{100}{\text{总片数}}\)，本片内部进度再乘以 \(\frac{1}{\text{总片数}}\) 叠加上去。

这样进度条从第 0 片平滑涨到第 N 片结束，全程单调。

#### 4.4.2 核心流程

```
父 ProgressMonitor (total_parts=N, part_index=0)
  回调 → AsyncCallback (事件流)
  ├─ create_part_monitor(0, N) → 子 monitor A
  │     回调 → 父._handle_part_progress → 父.progress_change_callback (附 part_index)
  │     _do_translate_single(子A, part0_config)
  ├─ create_part_monitor(1, N) → 子 monitor B
  │     ...
  └─ ...
每片内部进度 p∈[0,100] 映射到全局:
  overall = part_offset + p / N
  part_offset = i * (100 / N)
```

#### 4.4.3 源码精读

`create_part_monitor` 造一个绑定父 monitor 的子监控器：

[babeldoc/progress_monitor.py:72-86](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L72-L86) —— 创建 part monitor，把回调指向父的 `_handle_part_progress` / `_handle_part_finish`，并设 `parent_monitor=self`。

子 monitor 的进度回调路由到父，再由父转发给真正的 `AsyncCallback`：

[babeldoc/progress_monitor.py:88-94](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L88-L94) —— `_handle_part_progress` 给事件补上 `part_index` / `total_parts` 后转发。

子 monitor 的终态由 `_handle_part_finish` 处理，它把每片的 `translate_result` 收集进父的 `part_results`：

[babeldoc/progress_monitor.py:96-108](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L96-L108) —— `_handle_part_finish`：出错直接冒泡到父的 `finish_callback`，否则把结果存进 `part_results[part_index]`。

`part_offset` 的计算在 `calculate_current_progress`：

[babeldoc/progress_monitor.py:175-185](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L175-L185) —— 按是否为 part monitor 取不同的 `part_offset`：子 monitor 用 `part_index * part_weight`，父 monitor 用 `len(part_results) * part_weight`（已完成片数）。

```python
def calculate_current_progress(self, stage=None):
    ...
    part_weight = 1 / self.total_parts
    if self.parent_monitor:
        part_offset = self.part_index * part_weight      # 子：按自己是第几片
    else:
        part_offset = len(self.part_results) * part_weight  # 父：按已完成几片
    part_offset *= 100
    progress = self._calculate_current_progress(stage) * part_weight + part_offset
    return progress
```

调用点在分片分支：每片创建 part monitor 并用它跑 `_do_translate_single`：

[babeldoc/format/pdf/high_level.py:654-662](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L654-L662) —— `part_monitor = pm.create_part_monitor(i, len(split_points))`，然后 `_do_translate_single(part_monitor, part_config)`。

注意一个精妙之处：子 monitor 的 `disable` 检查处处都带 `self.parent_monitor and self.parent_monitor.disable`（如 [progress_monitor.py:111](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L111) 等多处）——这意味着只要父被禁用，所有子也跟着禁用（返回 `DummyTranslationStage`，[progress_monitor.py:300-316](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L300-L316)）。水印首页渲染（`generate_first_page_with_watermark`）正是靠 `watermarked_config.progress_monitor.disable = True` 临时静音进度（[high_level.py:1090](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L1090)）。

#### 4.4.4 代码实践

**实践目标**：用一个无副作用的最小模型，验证 part_offset 让多片进度单调递增。

**操作步骤**（示例代码，无需 BabelDOC 运行环境）：

```python
# 示例代码：用 ProgressMonitor 验证 part_offset 的全局进度计算
from babeldoc.progress_monitor import ProgressMonitor

N = 3
events = []
def on_progress(**kw):
    if kw.get("type") == "progress_update":
        events.append((kw.get("part_index"), round(kw["overall_progress"], 1)))

# 父 monitor，无真实翻译，只手动模拟 stage 推进
parent = ProgressMonitor(
    stages=[("A", 50.0), ("B", 50.0)],
    progress_change_callback=on_progress,
    total_parts=N,
)
for i in range(N):
    child = parent.create_part_monitor(i, N)
    st = child.stage_start("A", 10)
    st.advance(10)
    # 退出 with 即 stage_done；这里手动调 stage_update/done 模拟
    child.stage_update(st, 0)
    child.stage_done(st)

prev = -1
for part_idx, prog in events:
    assert prog >= prev, f"进度回退: {prog} < {prev}"
    prev = prog
print("全部进度:", events)
print("单调递增:", prev)
```

**需要观察的现象**：不同 `part_index` 的事件，其 `overall_progress` 应落在各自片段内（第 0 片约 0–33、第 1 片约 33–66、第 2 片约 66–100），且整体单调不减。

**预期结果**：断言通过，最终 `prev` 接近 100。**若本地未安装 BabelDOC，标注「待本地验证」。**

#### 4.4.5 小练习与答案

**练习 1**：父 monitor 的 `part_offset` 为什么用 `len(self.part_results)` 而不是 `part_index`？

**参考答案**：父 monitor 自身没有「当前在第几片」的概念，它通过 `part_results` 字典收集**已完成**片的结果（见 [progress_monitor.py:104-105](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L104-L105)）。用 `len(part_results)` 表示「已经彻底跑完了几片」，从而把全局进度推到正确位置；而子 monitor 才用 `part_index` 表示「我是第几片」。

**练习 2**：水印首页渲染时为什么要把 `progress_monitor.disable = True`？为什么子 monitor 也会跟着静音？

**参考答案**：首页水印是用一份独立的 `watermarked_config` 单独跑一次 `Typesetting` + `PDFCreater.write`（[high_level.py:1090-1103](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L1090-L1103)），这部分进度不该混进主进度条，否则会让进度条「倒退」或抖动。设 `disable=True` 后，`stage_start` 返回什么都不做的 `DummyTranslationStage`（[progress_monitor.py:111-112](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L111-L112)）。而所有 stage 方法在入口都检查 `self.parent_monitor and self.parent_monitor.disable`，所以父被禁用时所有子 monitor 也立刻静音，不必逐个设置。

---

## 5. 综合实践

把四个最小模块串起来，完成下面这个「**时序图 + 取消路径**」的综合任务（这是本讲规格里指定的核心实践）。

**任务**：阅读 `async_translate` 全文（[high_level.py:299-376](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L299-L376)），画出「**主线程事件循环 ↔ 线程池中的 `do_translate` ↔ `ProgressMonitor` 回调**」三者交互的时序图，并标注取消翻译时 `cancel_event` 的作用路径。

**建议产出格式**（可用文字或画图工具）：

```
主线程事件循环            AsyncCallback(队列)        工人线程 do_translate        ProgressMonitor
     │                         │                          │                          │
     │ run_in_executor ────────────────────────────────────▶                          │
     │                         │                          │ stage_start ───────────────▶
     │                         │                          │                          │ progress_change_callback
     │                         │◀── call_soon_threadsafe(put) ──────────────────────────◀
     │ async for (await get)   │                          │                          │
     │◀── event ───────────────│                          │                          │
     │ yield event             │                          │                          │
     │             ...翻译中...                                                        │
     │  [用户取消]                                                                       │
     │ except CancelledError ──▶ cancel_event.set() ──────────────────────────────────▶│
     │                         │                          │ raise_if_cancelled() ─────▶│
     │                         │                          │  → CancelledError          │
     │                         │                          │ translate_error(e) ────────▶│ finish_callback(error)
     │                         │◀── call_soon_threadsafe(put) ──────────────────────────◀
     │                         │                          │ finally: on_finish() ──────▶│
     │                         │                          │                          │ loop.call_soon_threadsafe(finish_event.set)
     │ await finish_event.wait() ◀─────────────────────────────────────────────────────│
     │ (解除阻塞，退出)                                                                 │
```

**要求在图中至少标注**：

1. `run_in_executor` 把同步 `do_translate` 交给工人线程的时机。
2. 工人线程经 `ProgressMonitor` 回调、再经 `AsyncCallback.step_callback` 用 `call_soon_threadsafe` 跨线程入队的路径。
3. **取消时 `cancel_event` 的两条作用路径**：① 主循环 `except` 里 `cancel_event.set()` 把停止信号**传入**工人线程；② 工人线程的 stage 在 `raise_if_cancelled` 里读到它后抛 `CancelledError`，最终由 `on_finish` 跨线程置 `finish_event` 让主循环的 `await finish_event.wait()` 解除阻塞。
4. `on_finish` 恒位于 `do_translate` 的 `finally`，保证完成握手必发生。

**进阶（可选）**：在图上额外标出分片翻译时 `create_part_monitor` 在何处插入——它把每片的进度经 `_handle_part_progress` 汇聚回父 monitor，再由父的回调流入同一个 `AsyncCallback` 队列。

---

## 6. 本讲小结

- **同步内核 + 异步外壳**：`async_translate` 用 `loop.run_in_executor` 把同步的 `do_translate` 丢进线程池，自己只在主循环里 `async for` 消费事件，翻译真活与事件搬运彻底解耦。
- **`AsyncCallback` 是桥接主角**：它持有事件循环引用，用 `call_soon_threadsafe` 跨线程把回调写入 `asyncio.Queue`，并用 `time.sleep(0.01)` 主动让出 GIL；「先入队、再置 `finished`」的顺序 + `if self.finished: return` 守卫，保证终态事件不丢、不重。
- **两个事件各司其职**：`cancel_event`（`threading.Event`）是跨线程的停止开关，被工人线程的 stage 轮询；`finish_event`（`asyncio.Event`）是跨线程的完成信号，由 `on_finish` 用 `call_soon_threadsafe` 置位、被主循环 `await` 等待。`on_finish` 恒在 `do_translate` 的 `finally`，是优雅退出的护城河。
- **进度节流**：`stage_update` 用 `report_interval`（默认 0.1s）对总量 `>3` 的长 stage 节流，`last_report_time` 在 `stage_start`/`stage_done` 重置以保证首末帧必发；总进度按 `TRANSLATE_STAGES` 权重加权。
- **分片进度**：`create_part_monitor` 造子 monitor，其回调汇聚回父；`part_offset = part_index × (100/总片数)` 让多片进度单调铺满 `[0,100]`；父的 `disable` 会级联静音所有子 monitor。
- **下游消费**：`main.py` 的 `create_progress_handler` 把这套事件流渲染成 rich / tqdm 进度条（[main.py:786-825](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L786-L825)、[main.py:744-746](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L744-L746)），证明这套桥接对任意 UI 都通用。

---

## 7. 下一步学习建议

- **横向对照 RPC 服务**：下一讲 u8-l4（executor RPC 服务）会把这套异步事件流进一步包装成 **NDJSON 流式 HTTP 协议**。你会看到 `WorkerEvent` 与本讲的 `progress_*` 事件高度同构，只是传输介质从「进程内 `asyncio.Queue`」换成了「HTTP 长连接」。学完两讲后，对比「进程内异步桥接」与「跨进程 RPC 流」两种事件传递的异同。
- **纵向深挖健壮性**：u8-l5（异常体系与健壮性处理）会讲 `do_translate` 里那些 `fix_*` 修正与 `safe_save` 容错，与本讲的 `finally` / `on_finish` 收尾机制是同一套「无论如何都要善后」哲学的不同侧面。
- **动手扩展**：如果你想给自己的应用接入 BabelDOC 进度，最简单的方式是直接 `async for event in async_translate(config)`，按 `event["type"]` 分发——本讲 4.1.4 的 `bridge_demo.py` 已经把最小模式演示清楚了。
