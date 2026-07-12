# Engine 主类与请求管理

## 1. 本讲目标

本讲进入 PyTorch 后端的「心脏」——`lmdeploy/pytorch/engine/engine.py` 中的 `Engine` 类。在 u3-l1 我们已经知道 `Pipeline` 只是一个同步外观，真正跑模型 `forward` 的是 `Engine`。本讲要回答：

- `Engine` 由哪些部件拼装而成？它怎么启动、怎么停止、怎么关闭（生命周期 `start` / `stop` / `close`）？
- 外部（`EngineInstance` / `AsyncEngine`）怎么把一个「加一条消息」的诉求投递给运行在另一个事件循环里的 `Engine`？
- `RequestManager` 如何充当「请求队列 + 派发器」，把请求按类型路由到对应的 `_on_*` 回调？
- session 与 message 是怎么进入调度器（`Scheduler`）的，进入前后经历了哪些状态校验？

学完后，你应当能在源码中画出「请求从发送方到 `_on_add_message` 回调」的完整链路，并理解 `Engine` 生命周期三件套各自的边界。

## 2. 前置知识

本讲假设你已经掌握（见 u3-l1、u2-l1）：

- **两条后端、一个 Pipeline**：`pipeline()` 内部最终会创建 `Engine`（PyTorch 后端）或 `TurboMind`。
- **异步外观、同步接口**：`Pipeline` 用一个后台线程跑事件循环，对外提供同步的 `infer` / `stream_infer`。
- **核心类型**：`GenerationConfig`（采样）、`Response`（用户面响应）、`MessageStatus`（序列状态机）。

本讲引入的新角色：

| 角色 | 所在文件 | 一句话职责 |
|---|---|---|
| `Engine` | `engine.py` | PyTorch 推理引擎主类，持有调度器、执行器、请求管理器，承载生命周期 |
| `RequestManager` | `request.py` | 请求队列 + 回调派发中枢，按 `RequestType` 路由到 `_on_*` |
| `RequestSender` | `request.py` | 发送方，`EngineInstance` 通过它向 `Engine` 投递请求并等响应 |
| `Request` / `Response` | `request.py` | 引擎面的请求/响应数据类（注意与用户面的 `Response` 区分） |
| `EngineInstance` | `engine_instance.py` | `Engine` 的「推理实例」句柄，`create_instance` 的返回值 |

> 术语提醒：本讲提到的 `Response` 默认指**引擎面**的 `lmdeploy.pytorch.engine.request.Response`（带 `event: asyncio.Event`），而不是用户面 `lmdeploy.messages.Response`。两者同名但不同类，这是后续阅读的关键区分点。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [lmdeploy/pytorch/engine/engine.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py) | `Engine` 主类：构造、生命周期、`_on_*` 回调、`create_instance` |
| [lmdeploy/pytorch/engine/request.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py) | `RequestType` / `Request` / `Response` / `RequestSender` / `RequestManager` |
| [lmdeploy/pytorch/engine/engine_loop.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py) | 异步推理主循环，其 `preprocess_loop` 反复调用 `req_manager.step()` 派发请求 |
| [lmdeploy/pytorch/engine/base.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/base.py) | `EngineBase` 抽象基类，定义 `create_instance` 等接口契约 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**Engine 类与生命周期**、**RequestManager 请求派发**、**_bind_request_manager 回调注册**、**_on_add_message 与会话/消息状态回调**。

### 4.1 Engine 类：构造与生命周期

#### 4.1.1 概念说明

`Engine` 是 PyTorch 后端的「总装车间」与「总调度」。它本身**不直接做矩阵乘法**，而是把若干专职部件装配到一起：

- `executor`（执行器）：负责跨进程/跨卡跑 forward（u3-l5 提到它内部会 `build_patched_model` 并加载权重）。
- `scheduler`（调度器）：把 sequences 组织成 batch，决定 prefill/decode（u4-l4 详解）。
- `req_manager`（请求管理器）：接收外部请求并派发到回调（本讲重点）。
- `adapter_manager`、`engine_conn`、各种策略（`strategy_factory`）等辅助部件。

`Engine` 采用典型的 **Actor（演员）模型**风格：它运行在自己的异步事件循环里，外部不直接调用它的方法改状态，而是把「请求」丢进队列，由循环逐条处理并回送「响应」。这样避免了多线程并发改调度器状态带来的竞态。

#### 4.1.2 核心流程

`Engine` 的生命周期可以分成三段：

1. **构造（`__init__`）**：下载模型 → 校验环境 → 构建 configs → 构建 executor → 构建 scheduler → 绑定 req_manager。注意：构造阶段**不启动**推理循环，只把所有部件造好。
2. **启动（`start` / `start_loop`）**：创建 `EngineMainLoop` 异步任务，进入 `async_loop`，后者构建并启动 `EngineLoop`（真正的 forward 循环）。
3. **停止 / 关闭（`stop` / `close`）**：`stop` 取消主循环任务；`close` 在 `stop` 基础上额外清理 CUDA 资源并保证 `_loop_finally` 收尾。

用一个流程式文字描述主循环的生命周期：

```text
start()
  └─ req_manager.create_loop_task()        # 创建 EngineMainLoop 任务
       └─ async_loop()                      # Engine.__init__ 里 set_main_loop_func 绑定
            ├─ build_engine_loop(self)       # 构建 EngineLoop（preprocess/forward 等协程）
            ├─ engine_loop.start(event_loop) # 启动各子协程
            └─ await engine_loop.wait_tasks()# 阻塞直到循环结束
            （finally）→ engine_loop.stop() + _loop_finally()

stop()  → _loop_main.cancel()              # 请求取消主循环
close() → 清 cublas + 取消 _loop_main / _loop_finally()
```

#### 4.1.3 源码精读

`Engine` 继承自抽象基类 `EngineBase`，构造函数的形参就是 `pipeline` 最终传到底层的那几个：

[engine.py:92-99](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L92-L99) 定义了 `Engine` 类与其文档字符串（`model_path` / `engine_config` / `trust_remote_code`）。

构造函数很长，但骨架清晰。前半段做「环境与配置准备」：

[engine.py:108-164](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L108-L164) 依次完成：`ConfigBuilder.update_engine_config` 补全配置 → 下载模型与 adapter → `EngineChecker.handle()` 校验环境 → 用 `ConfigBuilder.build_*` 拆出 scheduler/cache/backend/dist/misc 五份配置 → `build_executor(...)` 构建执行器并 `init()`。这一段对应「下载模型 → 校验 → 建 executor」。

后半段做「调度部件装配」：

[engine.py:167-197](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L167-L197) 构建各种策略（采样、序列、引擎策略），创建 `Scheduler`，并把测算出来的 `num_cpu_blocks` / `num_gpu_blocks` 回填进 `engine_config`。**注意第 197 行 `self.req_manager = self._bind_request_manager()`——这是本讲的核心接线点**，请求管理器在此刻被创建并绑定回调。

主循环任务在构造末尾被「登记」但未启动：

[engine.py:207-217](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L207-L217) `req_manager.set_main_loop_func(self.async_loop)` 把 `Engine.async_loop` 登记为主循环协程；`self._loop_main = None`、`self._engine_loop = None` 表明此时还没跑。

`start()` 才真正拉起循环。它幂等——若循环已存活则直接返回：

[engine.py:636-641](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L636-L641) `start()` 检查 `req_manager.is_loop_alive()`，否则 `create_loop_task()`。

`async_loop()` 是主循环协程本体，负责构建并守护 `EngineLoop`：

[engine.py:595-623](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L595-L623) 它 `build_engine_loop(self)` → `engine_loop.start(event_loop)` → `await engine_loop.wait_tasks()`；`finally` 里 `engine_loop.stop()` 并调 `_loop_finally()`。

`stop()` 与 `close()` 的分工：

[engine.py:643-646](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L643-L646) `stop()` 只做一件事：若主循环任务存在就 `cancel()`。

[engine.py:625-634](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L625-L634) `close()` 额外做两件事：① 对 cuda 设备调 `torch._C._cuda_clearCublasWorkspaces()` 释放 cuBLAS 工作区内存（注释解释：同进程内反复重建引擎会越来越多保留显存）；② 取消 `_loop_main`，若没有主循环则直接 `_loop_finally()` 收尾。

[engine.py:495-499](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L495-L499) `_loop_finally()` 清空 `migration_event` 并 `executor.release()`，是循环退出的统一收尾点。

**关于 `create_instance` 与 `cuda_stream_id`（重要：诚实说明）**

[engine.py:661-670](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L661-L670) `create_instance(self, cuda_stream_id=0)` 的函数体只有一句：`from .engine_instance import EngineInstance` 然后 `return EngineInstance(self)`。也就是说，**它声明了 `cuda_stream_id` 形参，却完全没有使用它**——`EngineInstance` 的构造连这个参数都没接收。

那么 `cuda_stream_id` 到底有什么用？它来自抽象基类的契约：

[base.py:38-40](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/base.py#L38-L40) `EngineBase.create_instance(self, cuda_stream_id=0)` 把「按 cuda stream 创建实例」写进了接口契约。真正消费这个参数的是**另一个后端**——TurboMind（C++）：在 `lmdeploy/turbomind/turbomind.py:380-388` 中 `create_instance(self, cuda_stream_id=0)` 把它透传给 `TurboMindInstance(self, cuda_stream_id)`（555-557 行存为 `self.cuda_stream_id`），因为 TurboMind 用多条 CUDA stream 实现并发推理。

结论：**在 PyTorch 引擎里 `cuda_stream_id` 是一个为「与 TurboMind 保持 API 对齐」而保留的空形参**（历史遗留 + 接口一致性），PyTorch 后端的并发由自己的异步循环 + 调度器承担，并不把实例绑定到具体 CUDA stream。读源码时不要被它的名字误导，以为 PyTorch 在这里做了 stream 绑定。

> 延伸：`mp_engine/base.py:106-108` 的 `create_instance` 同样声明却不用该参数，进一步印证它是「契约层形参」。

> 关于 `_get_max_session_len` 的容量测算（顺带理解 KV 容量上限）。引擎在构造期根据 GPU 物理块数推算单会话最大 token 数：

\[ \text{max\_tokens} = (\text{num\_gpu\_blocks} - \text{num\_reserved\_gpu\_blocks}) \times \text{block\_size} - \text{block\_size} \]

见 [engine.py:290-303](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L290-L303)。其中减去最后一个 `block_size` 是为解码预留余量；若开启滑动窗口且窗口不超容量，则把上限放大到 \(2^{63}-1\)（实质不限）。

#### 4.1.4 代码实践

**目标**：把 `Engine` 的生命周期三件套在源码里对上号，并搞清 `create_instance` / `cuda_stream_id` 的真实角色。

**步骤**：

1. 打开 `engine.py`，定位 `start` / `stop` / `close` 三个方法（636 / 643 / 625 行附近）。
2. 在 `start()` 的 `create_loop_task()` 处设断点或加日志（仅阅读亦可），确认它最终触发 `async_loop`（提示：`set_main_loop_func(self.async_loop)` 在第 207 行把两者绑定）。
3. 对比 `stop()` 与 `close()` 的差异：`close()` 多了哪一行 CUDA 清理？为什么同进程反复重建引擎需要它？
4. 定位 [engine.py:661-670](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L661-L670) 的 `create_instance`，核对：函数体里 `cuda_stream_id` 是否被用到？
5. 切到 `lmdeploy/turbomind/turbomind.py` 的 `create_instance`（380 行附近）与 `TurboMindInstance.__init__`（555 行附近），对比 TurboMind 是否真的消费了 `cuda_stream_id`。

**预期结果**：

- `start()` 幂等，重复调用不会创建多个循环。
- `close()` 比 `stop()` 多了 `torch._C._cuda_clearCublasWorkspaces()`（仅 cuda 设备）。
- 若无主循环任务，`close()` 直接走 `_loop_finally()`（因为 `_loop_main` 为 `None` 时 `cancel` 无意义）。
- **PyTorch 的 `create_instance` 声明 `cuda_stream_id` 但函数体未使用**（仅 `EngineInstance(self)`）；该参数是 `EngineBase` 契约的一部分，真正使用它的是 TurboMind（C++）后端——这是为「双后端 API 对齐」保留的空形参。

> 本实践为「源码阅读型」，不需要 GPU；若运行需真实模型与显卡，可标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`Engine.__init__` 里为什么把 `req_manager.set_main_loop_func(self.async_loop)` 而不直接 `asyncio.create_task(self.async_loop())`？

**参考答案**：因为构造阶段 `Engine` 还没有自己的事件循环——循环要等 `start()` 被调用、进入 `req_manager.create_loop_task()` 后才由 `event_loop.create_task` 创建（见 request.py:236-237）。`set_main_loop_func` 只是「登记协程工厂」，把「何时跑」推迟到 `start()`，从而让构造与启动解耦。

**练习 2**：`stop()` 之后引擎里的显存会立刻释放吗？

**参考答案**：不会完全释放。`stop()` 只是 `cancel()` 主循环任务，触发 `_loop_finally` → `executor.release()`；要彻底回收 cuBLAS 占用的显存，还需 `close()` 里那行 `torch._C._cuda_clearCublasWorkspaces()`。

---

### 4.2 RequestManager：请求派发中枢

#### 4.2.1 概念说明

`Engine` 跑在自己的事件循环里，外部（`EngineInstance`）怎么安全地把「加消息」这种诉求交给它？答案是用一个**请求队列 + 回调表**的中介——`RequestManager`。

`RequestManager` 的设计很像一个单线程的「信箱」：

- **发送方**（`RequestSender`）把 `Request` 放进 `asyncio.Queue`，并持有一个 `Response`（内含 `asyncio.Event`）等回信。
- **主循环**不断从队列取出请求，按类型查回调表，调用对应函数处理；处理完后通过 `Response.event.set()` 把「回信」投递给发送方。

这种「不直接调用、走队列」的方式，保证了调度器状态只在主循环这一条执行流里被修改——天然线程安全。

#### 4.2.2 核心流程

请求的完整往返（以「加一条消息」为例）：

```text
EngineInstance 侧                         Engine 侧（主循环）
─────────────────                         ───────────────────
RequestSender.send_async(ADD_MESSAGE,data)
  └─ _gather_request: 造 Request + Response(event)
  └─ req_que.put_nowait(reqs)      ───►   asyncio.Queue
                                          │
                                          ▼
                          engine_loop.preprocess_loop: await req_manager.step()
                                          │
                          step() → get_all_requests() 取出并按类型分桶
                                 → 按 request_priority 顺序遍历
                                 → process_request(ADD_MESSAGE, reqs)
                                          │
                                          ▼
                          callbacks[ADD_MESSAGE] = _on_add_message(reqs)
                                          │
                                          ▼  (处理完毕，回信)
                          req_manager.response(resp) → resp.event.set()
  ◄─── async_recv 被唤醒，拿到 resp.type / resp.data ────
```

关键点：`step()` 按**优先级顺序**处理一类请求，而不是先到先服务——这保证控制类请求（如 `STOP_ENGINE`）能插队。

#### 4.2.3 源码精读

先看请求类型的枚举（一共六种）：

[request.py:15-23](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py#L15-L23) `RequestType` 定义了 `ADD_SESSION` / `ADD_MESSAGE` / `STOP_SESSION` / `END_SESSION` / `STOP_ENGINE` / `RESUME_ENGINE`。

请求与响应的数据类：

[request.py:26-46](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py#L26-L46) `Response` 带 `event: asyncio.Event`（回信信号）、`type: ResponseType`、`data`、`err_msg`、`is_done`；`Request` 带 `type`、`sender_id`、`data`、`resp`。注意 `Request.resp` 指向配对的 `Response`，两者一一绑定。

`RequestManager.__init__` 建立回调表、优先级表与阻塞集合：

[request.py:175-194](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py#L175-L194) `callbacks`（类型→回调）、`request_priority`（处理顺序）、`requests`（队列，初始为 `None`，`create_loop_task` 时才建）、`_blocked_request_types`（sleep 时的准入闸门）。

派发的核心是 `step()`：

[request.py:398-424](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py#L398-L424) `step()` 先 `get_all_requests()` 把队列里所有请求按类型分桶，再按 `request_priority` 顺序逐类 `process_request`。`get_all_requests` 的细节见 [request.py:339-364](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py#L339-L364)——它把队列里可能成 list 的元素展平，再归入 `reqs_by_type`。

`process_request` 查表调用回调：

[request.py:378-396](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py#L378-L396) 先检查该类型是否被阻塞（`is_request_blocked`），若阻塞则逐个 `reject_request`；否则 `self.callbacks[req_type](reqs, **kwargs)` 调用回调；找不到回调则回 `HANDLER_NOT_EXIST`。

回信动作只有一个原语——置位事件：

[request.py:374-376](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py#L374-L376) `response(resp)` 就是 `resp.event.set()`，发送方的 `async_recv` 据此醒来（见 [request.py:139-154](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py#L139-L154)）。

发送方如何造请求并入队：

[request.py:103-137](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py#L103-L137) `_gather_request` 为每个请求配对一个 `Response`（初始 `INTERNAL_ENGINE_ERROR`，被阻塞则 `reject_request`）；`batched_send_async` / `send_async` 把成批请求 `put_nowait` 进队列。

主循环里调用 `step()` 的位置（确认派发确实发生在 `EngineLoop` 中）：

[engine_loop.py:159-163](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L159-L163) `preprocess_loop` 在 `while not stop_event.is_set()` 里反复 `await self.req_manager.step()`，随后 `has_runable_event.set()` 通知 forward 协程「有活可干了」。

> 优先级表会插队：[request.py:178-181](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py#L178-L181) 顺序为 `STOP_ENGINE > ADD_SESSION > ADD_MESSAGE > STOP_SESSION > END_SESSION`，所以「停引擎」总能优先于「加消息」被处理。

#### 4.2.4 代码实践

**目标**：验证「请求按类型分桶、按优先级处理」这一派发模型。

**步骤**：

1. 阅读 [request.py:339-364](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py#L339-L364) 的 `get_all_requests`，确认它返回的是「按类型分桶」的字典，而非按入队顺序的列表。
2. 阅读 `step()`（398-424 行），确认遍历顺序是 `request_priority` 而非字典插入顺序。
3. 思考：若同一批里同时有 `STOP_ENGINE` 和 `ADD_MESSAGE`，谁先被处理？为什么这样设计对「优雅停机」很重要？

**预期结果**：`STOP_ENGINE` 先处理。这样停机时不会还在不停接收新消息导致状态不一致。

#### 4.2.5 小练习与答案

**练习 1**：`Response.event` 为什么用 `asyncio.Event` 而不是直接返回值？

**参考答案**：因为发送方与处理方在不同执行流（发送方可能在另一个线程的事件循环里）。`asyncio.Event` 是跨协程的同步原语，处理方 `event.set()`、发送方 `await event.wait()`，二者解耦，且 `async_recv` 还能在等待时检查 `is_loop_alive()` 以感知引擎崩溃（见 request.py:144-152）。

**练习 2**：`process_request` 里 `func(reqs, **kwargs)` 的 `reqs` 是「同类型的批量请求」还是「所有请求」？

**参考答案**：是「同类型的批量请求」。`step()` 已按类型分桶，`process_request` 一次只处理某一类型在当前批次里的全部请求，因此 `_on_add_message(reqs)` 收到的是「这一步内累积的所有 ADD_MESSAGE 请求」——这正是「批处理」的入口。

---

### 4.3 _bind_request_manager：把回调钉到请求类型

#### 4.3.1 概念说明

`RequestManager` 的 `callbacks` 表默认是空的。`Engine` 必须告诉它「收到哪种请求该调哪个方法」——这就是 `_bind_request_manager` 的职责。它把 `RequestType` 与 `Engine` 的 `_on_*` 方法一一绑定，相当于把「请求类型」翻译成「`Engine` 内部的处理函数」。

这是一个典型的**命令模式（Command Pattern）**：每种请求是一个命令对象，`bind_func` 注册对应的执行者，派发时查表执行。新增一种请求类型，只需新增枚举值 + 绑定一个回调，无需改派发逻辑。

#### 4.3.2 核心流程

```text
Engine.__init__  ──► self.req_manager = self._bind_request_manager()
                              │
                              ▼
            RequestManager()
            bind_func(ADD_SESSION,   _on_add_session)
            bind_func(STOP_SESSION,  _on_stop_session)
            bind_func(END_SESSION,   _on_end_session)
            bind_func(ADD_MESSAGE,   _on_add_message)
                              │
                              ▼ （此后 step() 派发时）
            callbacks[req.type](reqs)  →  对应的 _on_* 被调用
```

注意：`STOP_ENGINE` / `RESUME_ENGINE` 这两个枚举值在这里**没有绑定回调**——它们在本讲的 `Engine` 里不会被 `step()` 派发到 `Engine` 自身（其处理路径在更上层的 mp_engine / serve 层）。

#### 4.3.3 源码精读

[engine.py:277-284](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L277-L284) `_bind_request_manager` 创建 `RequestManager()` 并连绑四个回调。`bind_func` 的实现极其简单——只是写字典：

[request.py:366-368](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py#L366-L368) `bind_func(req_type, callback)` → `self.callbacks[req_type] = callback`。

绑定后的四个回调分别是（后续模块详解 `_on_add_message` / `_on_add_session`）：

| 回调 | 触发请求类型 | 行号 | 作用 |
|---|---|---|---|
| `_on_add_session` | `ADD_SESSION` | 305 | 新建一个会话（session） |
| `_on_stop_session` | `STOP_SESSION` | 317 | 停止会话内所有序列的生成 |
| `_on_end_session` | `END_SESSION` | 372 | 彻底销毁会话及其缓存 |
| `_on_add_message` | `ADD_MESSAGE` | 388 | 往会话里加一条消息（真正的推理入口） |

通用的回信辅助方法 `_response`，把「写回响应 + 置位事件」封装成一句：

[engine.py:286-288](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L286-L288) 它转调模块级函数 `response_reqs`（[engine.py:78-89](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L78-L89)），后者先判 `resp.type == FINISH` 则跳过（避免重复回信），否则设置 `type/data/err_msg` 并 `req_manager.response(resp)`。

#### 4.3.4 代码实践

**目标**：补全「请求类型 → 回调」映射表，并理解 `_on_*` 命名约定。

**步骤**：

1. 在 `engine.py` 中用搜索定位所有形如 `def _on_` 的方法定义。
2. 把它们与 `_bind_request_manager`（277-284 行）的四个 `bind_func` 一一对应，填出上面的映射表。
3. 确认 `STOP_ENGINE` 与 `RESUME_ENGINE` 是否在 `_bind_request_manager` 里绑定了回调，并思考为什么。

**预期结果**：找到 `_on_add_session` / `_on_stop_session` / `_on_end_session` / `_on_add_message` 四个 `_on_*` 方法，与四个 `bind_func` 一一对应；`STOP_ENGINE` / `RESUME_ENGINE` 未绑定。

#### 4.3.5 小练习与答案

**练习**：为什么 `_bind_request_manager` 用 `bind_func` 动态注册，而不是在 `RequestManager.__init__` 里写死？

**参考答案**：为了让 `RequestManager` 保持「与具体引擎无关」的通用中介。`RequestManager` 只负责队列与派发机制，具体「每种请求谁来处理」由使用者（`Engine`）通过 `bind_func` 注入。这样同一套 `RequestManager` 可以被不同引擎（如 mp_engine）复用，符合依赖倒置。

---

### 4.4 _on_add_message 与会话/消息状态回调

#### 4.4.1 概念说明

`ADD_MESSAGE` 是四个回调里最重要的——它是**推理的真正入口**。当用户调 `pipeline.stream_infer(...)` 时，最终就是 `EngineInstance` 通过 `RequestSender` 发了一个 `ADD_MESSAGE` 请求，主循环把它派发到 `_on_add_message`。

理解这条回调，需要先分清两个层级：

- **session（会话）**：一次完整的多轮对话容器，用 `session_id` 标识，存活于 `scheduler.sessions` 字典里。
- **message / sequence（消息/序列）**：会话内的一条输入，对应一条 `SchedulerSequence`，才是被调度的最小单元。

一条消息要被调度，必须先有一个所属的会话——所以 `ADD_SESSION` 通常先于 `ADD_MESSAGE` 发生。`_on_add_message` 的职责就是：校验会话存在 → 预处理多模态输入 → 把消息挂进调度器（新建序列或续写已有序列）→ 设置采样参数与最大生成长度。

#### 4.4.2 核心流程

```text
_on_add_message(reqs):
  for req in reqs:
    ├─ session 不存在？ → 回 SESSION_NOT_EXIST，跳过
    ├─ 有多模态输入？  → input_processor.preprocess_input(...) 展开图像/视频
    │                    （prefix caching 开启时还会算 content hash）
    └─ 收集到 valid_reqs
  if valid_reqs: _add_message(valid_reqs)

_add_message(reqs):
  for req in reqs:
    ├─ session 仍不存在？ → 回 SESSION_NOT_EXIST
    ├─ 会话内首条消息？ → sess.add_sequence(...)  新建 SchedulerSequence
    │                       （若 migration_request，置 migration_event）
    └─ 否则续写：msg.update_token_ids(...) + msg.state.activate()
    ├─ __update_max_new_tokens(msg)  裁剪 max_new_tokens 不超 max_session_len
    └─ msg.resp = req.resp           把响应句柄挂到序列上（forward 产出后回信）
```

四个 `_on_*` 回调都遵循同一套「查会话 → 改状态 → 回信」的模式，差别只在改什么状态。

#### 4.4.3 源码精读

`_on_add_session` 是最简单的一个，可作为模板理解：

[engine.py:305-315](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L305-L315) 它检查 `session_id` 是否已在 `scheduler.sessions`，没有则 `scheduler.add_session(session_id)`；重复添加返回 `SESSION_REPEAT`，新建返回 `SUCCESS`。

`_on_add_message` 先做存在性校验与多模态预处理：

[engine.py:388-426](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L388-L426) 关键分支：① 会话不存在直接回 `SESSION_NOT_EXIST`（394-395）；② 有多模态输入时调 `self.input_processor.preprocess_input`（415），并在开启前缀缓存时 `ensure_multimodal_content_hashes`（419-420）；③ `language_model_only` 模式下丢弃多模态输入（409-413）。校验通过的请求收集进 `valid_reqs`，最后交给 `_add_message`。

`_add_message` 把消息真正挂进调度器：

[engine.py:428-479](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L428-L479) 两种情况：

- **会话内没有序列**（首条消息）：`sess.add_sequence(...)` 新建 `SchedulerSequence`，传入 `token_ids`、`sampling_param`、`adapter_name`、`multimodals`、`migration_request` 等；若是 PD 分离的迁移请求则 `self.migration_event.set()`（462-466）。
- **会话内已有序列**（多轮续写）：取已有 `msg`，`msg.update_token_ids(..., mode=UpdateTokenMode.INPUTS)` 续写输入，`msg.sampling_param = sampling_param` 更新采样，`msg.state.activate()` 把状态激活回调度队列（467-476）。

末尾两步对两种情况都执行：`__update_max_new_tokens(msg)` 裁剪生成长度（430-442，Prefill 角色强制 `max_new_tokens=1`，否则不超 `max_session_len`），`msg.resp = req.resp`（479）把响应句柄挂到序列上——后续 forward 产出的 token 就是通过这个 `resp` 回送给发送方的。

`_on_stop_session` 与 `_on_end_session` 展示了状态回调如何处理「正在跑的序列」：

[engine.py:317-339](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L317-L339) `_on_stop_session` 先记录原本已 `STOPPED` / `TO_BE_MIGRATED` 的序列响应 id，再 `scheduler.stop_session`，对**新变成停止态**的序列 `reject_request`（即取消其正在进行的流式响应）——这是「停掉正在生成的回复」的真正实现。

[engine.py:372-386](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L372-L386) `_on_end_session` 区分 `preserve_cache`（保留缓存只 `state.finish()`）与彻底销毁（`end_session`），见 [engine.py:676-683](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L676-L683) 的 `end_session` 还会触发多模态内存回收。

> 状态机呼应：这里出现的 `MessageStatus.STOPPED` / `TO_BE_MIGRATED`，正是 u2-l1 提到的引擎面 `MessageStatus` 序列状态机。`msg.state.activate()` 是把序列从停止态重新拉回 `RUNNING` 调度队列的动作。

#### 4.4.4 代码实践

**目标**：跟踪一条消息从 `_on_add_message` 到挂上调度器的路径，看清「首条消息」与「续写」两条分支。

**步骤**：

1. 在 [engine.py:428-479](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L428-L479) 的 `_add_message` 里找到 `if len(sess.sequences) == 0` 分支（453 行）与 `else` 分支（467 行）。
2. 对照 `SchedulerSession.add_sequence` 与 `SchedulerSequence.update_token_ids` 的签名（在 `lmdeploy/pytorch/messages.py` 中），确认两分支分别新建/复用序列。
3. 找到第 479 行 `msg.resp = req.resp`，解释：为什么要把 `Response` 挂到序列上？（提示：forward 产出 token 后，谁负责回信？）

**预期结果**：

- 首条消息：`sess.add_sequence(...)` 新建序列；续写：`msg.update_token_ids(...)` + `msg.state.activate()`。
- `msg.resp = req.resp` 让调度器/forward 循环在每产出一段 token 时，能通过这个 `resp`（及其中 `event`）回信给 `EngineInstance`，从而驱动 `stream_infer` 的 `yield`。

> 本实践为「源码阅读型」，无需 GPU。

#### 4.4.5 小练习与答案

**练习 1**：如果用户直接发 `ADD_MESSAGE` 而**没有先** `ADD_SESSION`，会发生什么？

**参考答案**：`_on_add_message` 在第 394-395 行检查 `session_id not in self.scheduler.sessions`，会直接回 `ResponseType.SESSION_NOT_EXIST`，该请求不会进入 `_add_message`。`EngineInstance` 端通常会用 `async_try_add_session`（见 engine_instance.py:32-40）先确保会话存在，再发消息。

**练习 2**：`_add_message` 里 `__update_max_new_tokens` 为什么要在「挂进调度器之后」再裁剪 `max_new_tokens`？

**参考答案**：因为裁剪要用到 `msg.num_valid_ids`（当前序列的有效输入长度），而这个值只有在序列新建/续写完成、token 确定后才准确。若输入 + `max_new_tokens` 超过 `max_session_len`，引擎会把 `max_new_tokens` 下调到 `max_session_len - num_all_tokens` 并告警（438-442），避免生成超出 KV 容量。

---

## 5. 综合实践

把四个模块串起来，完成一次「请求的完整生命周期」源码追踪。

**任务**：画出从「用户调用 `pipeline.stream_infer(...)`」到「`_on_add_message` 把序列挂进调度器」的完整调用链时序图，并标注每一步所在的文件与行号。

**建议步骤**：

1. 从 `Pipeline.stream_infer` → `_EventLoopThread` → `AsyncEngine.generate`（serve/core/async_engine.py）一路追到 `EngineInstance.async_stream_infer`（engine_instance.py）。
2. 在 `EngineInstance` 里找到它如何拿到 `RequestSender`（提示：`req_manager.build_sender()`，request.py:292-298）并发送 `ADD_MESSAGE`。
3. 切换视角到主循环：`start()` → `async_loop` → `EngineLoop.preprocess_loop` → `req_manager.step()` → `process_request` → `_on_add_message`。
4. 在图中标出三处「跨执行流」的衔接点：① 请求入队（`put_nowait`）；② 回信置位（`event.set()`）；③ 发送方唤醒（`async_recv`）。
5. 用一句话写出 `Engine` 生命周期三件套（`start` / `stop` / `close`）各自负责清理什么资源。

**预期产出**：一张包含「发送方线程/协程」与「引擎主循环」两条泳道的时序图，至少标注 8 个文件:行号锚点，并说明 `RequestManager` 如何作为两条泳道之间的唯一桥梁。

> 若要在本地真实运行验证，需要一个 PyTorch 后端可加载的小模型（如 Qwen2.5-0.5B-Instruct）与 GPU；否则按上述源码阅读型完成即可，运行部分标注「待本地验证」。

## 6. 本讲小结

- `Engine` 是 PyTorch 后端的总装车间：构造期装配 `executor` / `scheduler` / `req_manager` 等部件，但**不启动循环**；`start()` 才通过 `req_manager.create_loop_task()` 拉起 `async_loop` 主循环。
- 生命周期三件套分工：`start()` 幂等启动；`stop()` 只 `cancel` 主循环任务；`close()` 额外清理 cuBLAS 显存并保证 `_loop_finally` 收尾。
- `RequestManager` 是 Actor 模型的「信箱」：发送方把 `Request` 入队并等 `Response.event`，主循环在 `step()` 里按 `request_priority` 顺序派发到 `callbacks`，实现跨执行流的安全通信。
- `_bind_request_manager` 把四种请求类型（`ADD_SESSION` / `STOP_SESSION` / `END_SESSION` / `ADD_MESSAGE`）绑定到对应的 `_on_*` 回调，是命令模式的注册点。
- `_on_add_message` 是推理真正入口：校验会话 → 预处理多模态 → 新建或续写 `SchedulerSequence` → 裁剪 `max_new_tokens` → 把 `resp` 挂到序列上供 forward 回信。
- 派发由 `EngineLoop.preprocess_loop` 反复调用 `req_manager.step()` 驱动，是连接「请求管理」与「forward 推理」的衔接点（u4-l2 详解）。

## 7. 下一步学习建议

- **u4-l2 异步推理循环 EngineLoop**：本讲只提到 `preprocess_loop` 调 `step()`，下一讲将完整拆解 `EngineLoop` 的多协程协作（preprocess / forward / send_response），看清 token 是怎么从 forward 流回 `resp` 的。
- **u4-l3 EngineInstance 与流式推理**：从「发送方」视角补全本讲缺的那一半——`EngineInstance` 如何把 `ADD_MESSAGE` 包装成流式 `yield`。
- **u4-l4 调度器 Scheduler**：本讲序列挂进了 `scheduler.sessions`，下一阶段看 `Scheduler` 如何把这些序列组织成 prefill/decode batch。
- **延伸阅读**：可先扫一眼 [engine_instance.py:32-58](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_instance.py#L32-L58) 的 `async_try_add_session` / `try_add_session`，它们是发送方调用 `ADD_SESSION` 的最薄封装，能帮你把本讲的「接收方回调」与下一讲的「发送方封装」对上。
