# 异步引擎封装 async_engine

## 1. 本讲目标

在 u8-l1、u8-l2 里我们已经看清了 OpenAI 兼容服务的「路由—契约—校验」骨架，以及 `serve()` 如何把引擎、路由、中间件装配成一个 FastAPI 应用。但那个「引擎」到底是什么？路由里 `VariableInterface.async_engine` 指向的对象，究竟如何把一次 HTTP 请求最终变成 GPU 上的 token？

本讲要回答的核心问题是：**底层那套会跑 forward 的引擎（PyTorch 的 `Engine` 或 TurboMind 的 `TurboMind`），如何被包装成一个可以直接喂给 FastAPI 的异步推理引擎 `AsyncEngine`。**

学完本讲你应该能够：

- 说清 `AsyncEngine` 在「服务层」与「引擎层」之间的定位，以及它替上层做了哪些事（会话管理、chat 模板、流式去 token 化、统计上报、健康探针）。
- 在源码里定位「把请求真正转发到底层 engine」的那一行代码，并讲清楚 `generate() → safe_run() → handle.async_stream_infer()` 这条主干。
- 看懂 `VLAsyncEngine` 如何在 `AsyncEngine` 之上叠加多模态（图像/视频）能力。
- 理解 `lmdeploy/serve/core/health.py` 的 `EngineHealthMonitor` 是如何周期性探活、并在 `/health` 路由被访问时把引擎状态返回给客户端的。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（这些是前几讲的结论，本讲直接承接，不再重复展开）：

- **两条后端、一个 Pipeline**：用户统一从 `pipeline()` 进入，内部在 PyTorch 引擎（纯 Python，`Engine`/`EngineInstance`）与 TurboMind 引擎（C++，`TurboMind`/`TurboMindInstance`）之间二选一。详见 u3-l1。
- **引擎层对象**：`Engine`（Actor 模型，持有 executor/scheduler/req_manager，跑 async_loop 主循环）、`EngineInstance`（轻量用户句柄，不持权重）、`RequestManager`（跨执行流的「信箱」）。详见 u4-l1、u4-l3。
- **「内部永远流式」**：服务层为了支持持续批处理，强制以流式方式产出 token，非流式请求由上层把 generator 消费完再拼装返回。详见 u8-l1。
- **GenerationConfig / chat 模板**：采样参数包与对话格式化机制。详见 u2-l2、u2-l4。
- **asyncio 基础**：`async def`/`await`、`async for`、`asyncio.create_task`、`asyncio.wait_for`、`asyncio.shield`、`asyncio.Event`、`asyncio.Lock`。本讲大量用到这些原语，不熟悉的读者可把它们理解成「协程版的函数/循环/线程/锁」。

一个直觉比喻：把 `AsyncEngine` 想象成银行大堂经理。

- 大堂经理（`AsyncEngine`）不亲自点钞（不做 forward），但他负责：取号（session 管理）、把客户的话翻译成柜台听得懂的格式（chat 模板 + 去词表化）、把柜台吐出的钱整理好递给客户（流式输出 + 统计）。
- 柜台（底层 `Engine`/`TurboMind`）才是真正点钞的地方。
- 客户（FastAPI 路由）只跟大堂经理打交道，从不直接冲进柜台。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [lmdeploy/serve/core/async_engine.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py) | 本讲主角。定义 `AsyncEngine` 类与 `GenOut` 数据类，是服务层与引擎层之间的异步封装层。 |
| [lmdeploy/serve/core/vl_async_engine.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/vl_async_engine.py) | `VLAsyncEngine`，继承 `AsyncEngine`，叠加视觉编码器与多模态 prompt 处理。 |
| [lmdeploy/serve/core/health.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/health.py) | `EngineHealthMonitor`，后台周期性探活并缓存健康快照。 |
| [lmdeploy/serve/managers/session_manager.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/managers/session_manager.py) | `Session` / `SessionManager` / `RequestHandlePool`，会话与推理句柄池。`AsyncEngine` 经它拿到底层句柄。 |
| [lmdeploy/pytorch/engine/base.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/base.py) | `EngineBase` / `EngineInstanceBase` 抽象基类，定义了 `create_instance` / `get_health_status` / `async_stream_infer` 这套契约。 |
| [lmdeploy/pytorch/engine/engine.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py) | PyTorch 引擎 `Engine`，实现 `get_health_status` / `get_schedule_metrics`。 |
| [lmdeploy/serve/openai/api_server.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_server.py) | `/health` 路由与 FastAPI lifespan 中对 `EngineHealthMonitor` 的装配。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**AsyncEngine（封装层全貌与请求转发）**、**VLAsyncEngine（多模态扩展）**、**health（健康检查机制）**。其中「请求转发链路」是本讲实践任务的焦点，单独成节。

### 4.1 AsyncEngine：服务层与引擎层之间的异步封装层

#### 4.1.1 概念说明

直接用底层 `Engine`/`TurboMind` 来写 API 服务会很痛苦，因为它们只懂「token id」和「session id」，不懂：

- 用户发来的是自然语言字符串或 OpenAI 风格的 messages 列表，需要先套 chat 模板、再 token 化。
- 一个用户可能有多轮对话，需要管理「会话（session）」并把 K/V cache 的位置（step）接上。
- 引擎一次只吐增量 token id，需要增量去词表化（detokenize）成可读字符串，还要处理「停止词」「UTF-8 不完整字节」等边界。
- 服务层要统计吞吐、命中前缀缓存比例、错误数等指标。
- 负载均衡器需要知道服务「活着没」，于是要有健康探针。

`AsyncEngine` 就是把这些「服务层才关心、但引擎层不愿意管」的杂事全部接管下来的中间层。它对上暴露一个统一的异步生成器接口 `generate()`，对下持有 `self.engine`（一个满足 `EngineBase` 契约的底层引擎），把两条后端的差异藏在身后。

关键设计：`AsyncEngine` 本身是**后端无关**的。它不 import PyTorch 的 `Engine` 或 TurboMind 的 `TurboMind` 作为成员类型，而是在 `__init__` 里用 `if backend == 'turbomind' / elif backend == 'pytorch'` 这两个工厂方法之一去构造 `self.engine`。只要两个后端都遵守同一套契约（见 4.1.3），`AsyncEngine` 的所有方法就能原样工作。

#### 4.1.2 核心流程

`AsyncEngine` 的生命周期与一次请求的处理流程可以用下面的伪代码概括：

```text
# 构造期（serve() 调用一次）
AsyncEngine.__init__():
    解析 chat 模板 / tokenizer / arch / session_len
    if backend == 'turbomind': self.engine = _build_turbomind(...)   # 延迟 import
    elif backend == 'pytorch': self.engine = _build_pytorch(...)
    建 SessionManager + 句柄池（每个 handle = engine.create_instance()）
    建 stat_loggers（Prometheus / 日志）
    初始化健康探针相关字段

# 运行期（每个 HTTP 请求进来）
AsyncEngine.generate(messages, session_id, gen_config, ...):  # async generator
    取/建 Session（绑定 session_id 与 epoch）
    prompt_processor.get_prompt_input()   # 字符串/messages → prompt + input_ids
    _determine_gen_config()               # 补全采样参数（贪婪则 top_k=1 等）
    async with session.request_handle() as handle:   # 从池里借一个句柄
        async with self.safe_run(handle, ...) as gen:  # 包装 + 异常清理
            async for outputs in gen:                  # 真正转发到底层 engine
                detokenize_incrementally()             # 增量去词表化
                yield GenOut(...)                       # 产出给上层

# 服务启动时
AsyncEngine.start_loop(loop):       # 把 session_mgr 绑到事件循环，并启动引擎主循环
```

要点：

1. **构造期不启动推理循环**，只装配对象；循环由 `start_loop()` 在 FastAPI lifespan 里拉起（见 u8-l2）。
2. **`generate()` 是 async generator**，用 `yield GenOut(...)` 一段段产出，天然契合流式响应。
3. **转发动作**藏在 `safe_run` 内部对 `handle.async_stream_infer` 的调用里（4.2 详述）。

#### 4.1.3 源码精读

**类定义与定位。** `AsyncEngine` 是一个普通类（注意：源码里有一行被注释掉的 `# class AsyncEngine(LogitsMixin):`，说明历史上它曾混入 logits 相关能力，现已移除），它的文档字符串把它描述为「Async inference engine. Maintaining a bunch of tm_model instances.」——「维护一堆实例」指的就是那个句柄池。

参见 [async_engine.py:79-106](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L79-L106)。

**构造函数。** `__init__` 是全班的「装配车间」，按顺序做四件事：解析模型门面信息（chat 模板、tokenizer、arch、session_len）、按 backend 构造底层引擎、建会话管理器与句柄池、初始化统计与健康字段。

```python
# 解析门面信息（节选）
self.chat_template = get_chat_template(model_path, chat_template_config, ...)
self.tokenizer = Tokenizer(model_path, ...)
self.prompt_processor = MultimodalProcessor(self.tokenizer, self.chat_template)
self.arch, self.hf_cfg = get_model_arch(model_path, ...)
self.session_len = _get_and_verified_max_len(...) if backend_config.session_len is None \
    else backend_config.session_len
```

参见 [async_engine.py:108-173](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L108-L173)。注意第 154 行 `self.stop_words = _stop_words(self.chat_template.stop_words, self.tokenizer)`：chat 模板自带的停止词在这里被转成 token id，供后续 `_determine_gen_config` 兜底使用。

**按 backend 构造底层引擎（后端无关的关键）。** 这段 `if/elif` 是整个封装层「双后端」的汇聚点，也是延迟 import 的典型用法——把对 `lmdeploy.turbomind` / `lmdeploy.pytorch.engine` 的 import 推迟到方法调用时，避免循环导入和启动期不必要的依赖加载：

```python
if backend == 'turbomind':
    self.engine = self._build_turbomind(model_path=model_path, ...)
elif backend == 'pytorch':
    self.engine = self._build_pytorch(model_path=model_path, ...)
else:
    raise ValueError(f'unsupported backend {backend}')
self.backend_config = self.engine.engine_config   # 用引擎补全后的配置回填
```

参见 [async_engine.py:133-151](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L133-L151)，工厂方法见 [async_engine.py:185-209](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L185-L209)。第 147 行 `self.backend_config = self.engine.engine_config` 是「配置回填」——引擎在构造时会校验并补全很多派生字段（如 `num_gpu_blocks`），回填后上层用的是补全版。

**两套后端遵守同一套契约。** `AsyncEngine` 之所以能后端无关，是因为 PyTorch 与 TurboMind 都实现了 `EngineBase` / `EngineInstanceBase` 这套抽象基类。关键方法签名如下：

```python
class EngineBase:
    def create_instance(self, cuda_stream_id=0): ...      # 造一个推理句柄
    async def get_health_status(self) -> dict: ...        # 健康状态
    def start_loop(self) -> None: ...                     # 启动推理循环
    ...

class EngineInstanceBase:
    async def async_stream_infer(self, *args, **kwargs): ...  # 流式推理
    async def async_cancel(self, session_id): ...
    async def async_end(self, session_id): ...
```

参见 [base.py:9-59](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/base.py#L9-L59)。两套后端各自实现：PyTorch 在 [engine.py:699](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L699)、TurboMind 在 [turbomind.py:420](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L420)。

**会话管理与句柄池。** `AsyncEngine` 不直接持有推理句柄，而是把这件事委托给 `SessionManager`：

```python
self.session_mgr = SessionManager()
self.session_mgr.build_request_handle_pool(self.engine, self.backend_config.max_batch_size)
```

参见 [async_engine.py:163-164](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L163-L164)。`build_request_handle_pool` 内部一次性造出 `max_batch_size` 个句柄：

```python
class RequestHandlePool:
    def __init__(self, engine, size: int):
        self.size = size
        self.handles = [engine.create_instance() for _ in range(size)]   # 借助 EngineBase 契约
        self.pool: asyncio.Queue = None   # 惰性初始化，因为 asyncio.Queue 必须在异步上下文里建
```

参见 [session_manager.py:172-186](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/managers/session_manager.py#L172-L186)。这个池是后续「借句柄 → 转发 → 还句柄」模式的基础。

**启动推理循环。** `start_loop` 把 `SessionManager` 绑到指定事件循环，并按上层选同步 API 还是异步 API 决定如何启动引擎主循环：

```python
def start_loop(self, loop, use_async_api=False):
    self.session_mgr.attach_event_loop(loop)
    if hasattr(self.engine, 'start_loop'):
        if use_async_api:
            return self.engine.start_loop()       # 异步 API：直接在当前循环启动
        else:
            fut = concurrent.futures.Future()     # 同步 API：跨线程投递，避免阻塞
            ...
```

参见 [async_engine.py:794-818](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L794-L818)。它的文档注释点出一个重要约束：PyTorch 引擎会绑定到某个事件循环，所以 pipeline 在生命周期内只能二选一地使用同步接口（`__call__`/`stream_infer`）或异步接口（`generate`）。`start_loop` 就是让用户显式做出这个选择。

**统计与睡眠/唤醒。** 构造期还会 `_build_stat_loggers()`，在开启 `enable_metrics` 时挂上日志型与 Prometheus 型统计器（[async_engine.py:211-227](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L211-L227)）。`sleep`/`wakeup`（[async_engine.py:371-403](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L371-L403)）支持显存腾挪：sleep 卸载权重/丢 KV cache，wakeup 重新分配，TurboMind 唤醒后因 gateway 被重置需重建句柄池（第 400-401 行）。这些是 u8-l2 里 `/sleep` 路由背后的实现。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：在 `async_engine.py` 中确认「`AsyncEngine` 不直接知道用哪个后端」这一设计，并定位两套后端的契约入口。
2. **操作步骤**：
   - 打开 [async_engine.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py)，用搜索找到 `self.engine =` 的全部出现位置，确认它只出现在 `__init__` 的 `if/elif` 分支里（第 135、140 行）与若干 `self.engine.xxx` 转发调用里。
   - 打开 [base.py:9-59](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/base.py#L9-L59)，记下 `EngineBase` 与 `EngineInstanceBase` 各自声明的抽象方法。
   - 用 `Grep` 在 `lmdeploy/pytorch/engine/engine.py` 与 `lmdeploy/turbomind/turbomind.py` 中搜索 `def create_instance`、`def get_health_status`、`def async_stream_infer`，确认两个后端都实现了这些方法。
3. **需要观察的现象**：你会看到两套后端各自在相近的方法名下有独立实现，但对外签名一致。
4. **预期结果**：得出结论——`AsyncEngine` 通过 `EngineBase`/`EngineInstanceBase` 这套「鸭子类型契约」实现后端无关；新增第三套后端只要实现这套契约，`AsyncEngine` 无需改动。

#### 4.1.5 小练习与答案

**练习 1**：`AsyncEngine.__init__` 里第 147 行为什么要做 `self.backend_config = self.engine.engine_config`，而不是直接沿用传入的 `backend_config`？

**参考答案**：传入的 `backend_config` 是用户侧的「半成品」（很多派生字段如 `num_gpu_blocks`、补全后的 `session_len` 还没有值）。底层引擎在构造时会根据硬件、模型配置把这些字段测算并填好；回填后 `self.backend_config` 才是「引擎实际生效」的配置，供统计、句柄池重建等后续逻辑使用。

**练习 2**：`RequestHandlePool` 里的 `self.pool: asyncio.Queue = None` 为什么不直接在 `__init__` 里构造？

**参考答案**：因为 `asyncio.Queue` 必须在已有事件循环的异步上下文里创建，而 `RequestHandlePool.__init__`（进而 `AsyncEngine.__init__`）可能在循环启动前就被调用。所以它采用「惰性初始化」——第一次 `await get()` 时才建队列并装入预生成的句柄（见 [session_manager.py:178-186](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/managers/session_manager.py#L178-L186)）。

### 4.2 请求转发链路：generate() → safe_run() → handle.async_stream_infer()

这是本讲实践任务的焦点。我们要回答：**「在 async_engine.py 中，把请求转发到底层 engine 的方法到底是哪一个？」**

#### 4.2.1 概念说明

「转发」分三层，每一层只多做一件引擎层不愿意管的事：

| 层 | 方法 | 职责 |
| --- | --- | --- |
| 编排层 | `generate()` | 会话管理、prompt 预处理、采样参数补全、增量去词表化、统计、组装 `GenOut` 并 `yield` |
| 安全层 | `safe_run()` | 把底层异步生成器包进上下文管理器；捕获取消/异常并做兜底清理（`async_cancel`/`async_end`），把异常包装成 `SafeRunException` |
| 转发层 | `handle.async_stream_infer()` | **真正把请求投递到底层 engine 的那一行**；`handle` 是从池里借来的 `EngineInstance`/`TurboMindInstance` |

注意：编排层和安全层都「不算」真正的转发——它们只是在准备和善后。真正跨进引擎边界的是 `handle.async_stream_infer(session_id, **kwargs)`。`handle` 满足 `EngineInstanceBase` 契约，PyTorch 侧实现见 [engine_instance.py:175](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_instance.py#L175)，TurboMind 侧见 [turbomind.py:687](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L687)。

#### 4.2.2 核心流程

一次 `generate()` 的内部流转：

```text
generate(messages, session_id, gen_config, ...)
  │
  ├─ session = session_mgr.get(session_id, step=...)        # 取/建会话
  ├─ prompt_input = prompt_processor.get_prompt_input(...)  # chat 模板 + token 化
  ├─ gen_config = _determine_gen_config(session, input_ids, gen_config)
  │
  └─ async with session.request_handle() as handle:        # ① 借句柄（从池）
        async with self.safe_run(handle, session=..., **prompt_input, ...) as gen:
              # ② safe_run 内部：generator = handle.async_stream_infer(session_id, **kwargs)
              #    然后 yield generator  ← 这里就是【转发到底层 engine】
              async for outputs in gen:                    # ③ 消费引擎产出的 EngineOutput
                    token_ids += outputs.token_ids
                    response, state = tokenizer.detokenize_incrementally(token_ids, state, ...)
                    out = GenOut(response, ..., token_ids=res, ...)
                    yield out                               # ④ 产出给服务层
```

#### 4.2.3 源码精读

**① 借句柄。** `async with session.request_handle() as handle:` 进入 `Session.request_handle` 上下文，它从池里 `await hnd_pool.get()` 拿一个句柄，退出时 `hnd_pool.put(self._handle)` 归还：

```python
@asynccontextmanager
async def request_handle(self):
    hnd_pool = self._session_mgr().request_handle_pool
    self._handle = await hnd_pool.get()
    self._active = asyncio.Event()
    try:
        yield self._handle
    except SafeRunException:
        pass                       # safe_run 已包装过的取消，这里吞掉
    finally:
        if self._handle is not None:
            hnd_pool.put(self._handle)
            self._handle = None
        self._active.set()
```

参见 [session_manager.py:78-111](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/managers/session_manager.py#L78-L111)。注意第 90-91 行 `except SafeRunException: pass`：这与 `safe_run` 的异常包装（见 ③）配合，让「已处理的取消」不会冒泡。

**② `safe_run`：转发 + 兜底清理。** 这是答案所在。`safe_run` 是一个 `@asynccontextmanager`，它第一行就调用了真正转发的方法：

```python
@asynccontextmanager
async def safe_run(self, handle, session, **kwargs):
    generator = handle.async_stream_infer(session.session_id, **kwargs)   # ← 转发到底层 engine
    kwargs.pop('multimodal', None)

    async def cleanup_after_exception():
        try:
            await asyncio.shield(handle.async_cancel(session.session_id))   # 取消时清理
        except ...: ...
        if self.backend == 'pytorch':
            await asyncio.shield(handle.async_end(session.session_id))     # PyTorch 还要 end session

    try:
        metrics_processor.increase_api_routed_requests()
        yield generator                 # 把生成器交给 generate() 的 async for
    except (asyncio.CancelledError, GeneratorExit) as e:
        ...
        await cleanup_after_exception()
        raise SafeRunException(...) from e   # 包装，供 request_handle 区分「已处理」
    except Exception as e:
        ...
        raise SafeRunException(...) from e
    finally:
        await generator.aclose()
        metrics_processor.decrease_api_routed_requests()
```

参见 [async_engine.py:427-477](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L427-L477)。两个细节值得记住：

- **第 429 行 `handle.async_stream_infer(session.session_id, **kwargs)` 是「把请求转发到底层 engine」的那一行**。`kwargs` 里带着 `input_ids`/`prompt`、`gen_config`、`sequence_start`/`sequence_end`、`step`、`multimodal` 等。
- 清理协程用 `asyncio.shield` 保护，确保即便外层任务已进入 cancelling 状态，`async_cancel`/`async_end` 仍能跑完（注释 L434-L436 解释了原因）。`SafeRunException` 是为了让外层 `request_handle` 能用 `except SafeRunException: pass` 把「已处理的取消」与「意外 CancelledError」区分开（注释 L459-L464）。

**③④ 消费引擎输出 + 增量去词表化。** `generate()` 的后半段在 `safe_run` 上下文里 `async for outputs in gen:` 逐 step 消费 `EngineOutput`，做增量去词表化并组装 `GenOut`：

```python
async with self.safe_run(handle, session=session, **prompt_input,
                         gen_config=gen_config, ..., stream_output=stream_response,
                         sequence_start=sequence_start, sequence_end=sequence_end,
                         step=history_len) as gen:
    outputs = EngineOutput(ResponseType.INTERNAL_ENGINE_ERROR, [])   # 兜底默认值
    async for outputs in gen:
        ...
        output_len = len(outputs.token_ids)
        token_ids += outputs.token_ids[:output_len - hit_stop_token]
        response, state = self.tokenizer.detokenize_incrementally(
            token_ids, state, skip_special_tokens=..., spaces_between_special_tokens=...)
        out = GenOut(response, history_len, input_len, gen_len, finish_reason,
                     token_ids=res, routed_experts=outputs.routed_experts,
                     cache_block_ids=outputs.cache_block_ids, cached_tokens=cached_tokens)
        yield out
```

参见 [async_engine.py:655-720](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L655-L720)。其中 `detokenize_incrementally` 是「增量」去词表化——每次只解码新增片段，避免每步都对整段历史重新 decode（这是流式输出的性能关键）。`hit_stop_token` 用来在命中停止词时把最后一个 token 截掉。

**产出格式 `GenOut`。** `generate()` yield 出去的是 `GenOut` 数据类，它把一次产出所需的所有字段打包，并可经 `to_response()` 转成用户面 `Response`：

```python
@dataclasses.dataclass
class GenOut:
    response: str
    history_token_len: int
    input_token_len: int
    generate_token_len: int
    finish_reason: Literal['stop', 'length', 'error', 'abort'] | None = None
    token_ids: list[int] | None = None
    logprobs / logits / last_hidden_state / cache_block_ids / routed_experts / cached_tokens ...
    def to_response(self, index: int = 0) -> Response: ...
```

参见 [async_engine.py:43-75](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L43-L75)。`cache_block_ids` 服务于 PD 分离（把 KV 块号带给 decode 节点），`routed_experts` 服务于 RL 路由回放——这些都是引擎产出的「附带数据」，`AsyncEngine` 透传不加工。

#### 4.2.4 代码实践（源码阅读型 + 可选运行）

1. **实践目标**：亲手在源码里定位「转发到底层 engine」的那一行，并理解它前后做了什么。
2. **操作步骤**：
   - 打开 [async_engine.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py)，定位 `async def safe_run`（[L427](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L427)）。
   - 找到第 429 行 `generator = handle.async_stream_infer(session.session_id, **kwargs)`，确认这就是转发点。
   - 跟进 `handle` 的来源：回到 `generate()`，找到 `async with session.request_handle() as handle:`（[L633](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L633)），再到 [session_manager.py:78-111](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/managers/session_manager.py#L78-L111) 看句柄如何从池里借出。
   - （可选，需 GPU 与权重）用 `lmdeploy serve api_server <model>` 起服务，开 `LMDEPLOY_LOG_LEVEL=DEBUG`，发一次 chat 请求，在日志里观察 `[request_handle] session N acquiring an instance` / `acquired an instance` / `releasing the instance` 三条日志，对应借—用—还的全过程。
3. **需要观察的现象**：DEBUG 日志会显示一个 session 在请求期间恰好占用一个句柄，请求结束后归还。
4. **预期结果**：能画出 `generate → request_handle(借) → safe_run → handle.async_stream_infer(转发) → async for 消费 → request_handle(还)` 的完整链路。若无法本地运行，明确标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么转发用的是 `handle.async_stream_infer`，而不是直接调 `self.engine` 上某个方法？

**参考答案**：`self.engine` 是整个引擎（持有权重、跑主循环的「重对象」），不能被并发请求同时直接调用。引擎通过 `create_instance` 派发出轻量句柄（`EngineInstance`/`TurboMindInstance`），每个句柄持有自己的 `RequestSender`「专线」（详见 u4-l3），多请求经各自句柄并发投递，由引擎内部的 `RequestManager` 信箱统一排队，保证主循环单流修改调度器状态、天然线程安全。

**练习 2**：`safe_run` 在异常分支里为什么要把异常 `raise SafeRunException(...) from e`，而不是直接 `raise`？

**参考答案**：为了让外层 `Session.request_handle` 能区分「已被 `safe_run` 处理过的取消」（`except SafeRunException: pass`）与「意外的 `CancelledError`」。如果不包装，被抑制的异常会让任务停留在 cancelling 状态，在下一个 await 点再次抛出 `CancelledError`（源码注释 L459-L464、L469-L474 详细说明了这个坑）。

### 4.3 VLAsyncEngine：在 AsyncEngine 之上叠加多模态

#### 4.3.1 概念说明

`VLAsyncEngine`（Visual-Language Async Engine）是 `AsyncEngine` 的子类，专门处理图文/视频输入。它的设计哲学是**最小增量**：能复用的全部复用，只在三个点上做扩展。

1. **多一个视觉编码器** `vl_encoder`（`ImageEncoder`），负责把图片/视频编码成模型能消化的 embedding。
2. **换一个 prompt 处理器**：用带 `vl_encoder` 的 `MultimodalProcessor` 替换纯文本的，使 `get_prompt_input` 能识别 messages 里的图片并产出多模态输入。
3. **个别约束**：比如前缀缓存只对 PyTorch + 新版多模态预处理可用，否则强制关闭并告警；再如 VLM 必须显式指定 chat 模板（`model_name == 'base'` 直接报错）。

#### 4.3.2 核心流程

```text
VLAsyncEngine.__init__():
    (PyTorch 时) try_import_deeplink(device_type)             # VLM 视觉侧依赖 deeplink
    self.vl_encoder = ImageEncoder(...)                        # ① 视觉编码器
    if 开了 prefix_caching 但不支持:
        backend_config.enable_prefix_caching = False + 告警    # ② 约束
    super().__init__(...)                                      # ③ 复用 AsyncEngine 全部装配
    self.prompt_processor = MultimodalProcessor(               # ④ 替换处理器
        self.tokenizer, self.chat_template, vl_encoder=self.vl_encoder, backend=backend)
    if self.model_name == 'base': raise RuntimeError(...)      # ⑤ VLM 必须指定 chat 模板
```

注意第 ③ 步：`super().__init__()` 会先把父类的（纯文本）`prompt_processor` 建好，子类紧接着第 ④ 步覆盖它——所以最终生效的是多模态版。`generate()` 本身**完全没有被重写**，多模态能力是通过「换处理器」注入的：`generate()` 调 `self.prompt_processor.get_prompt_input(...)`，在 VLM 下它就会返回带 `multimodal` 字段的 `prompt_input`，再原样经 `safe_run` 透传给底层引擎。

#### 4.3.3 源码精读

```python
class VLAsyncEngine(AsyncEngine):
    """Visual Language Async inference engine."""

    def __init__(self, model_path, backend='turbomind', backend_config=None,
                 vision_config=None, trust_remote_code=False, **kwargs):
        from lmdeploy.serve.processors import MultimodalProcessor
        from lmdeploy.utils import try_import_deeplink
        from lmdeploy.vl.engine import ImageEncoder

        if backend == 'pytorch':
            try_import_deeplink(backend_config.device_type)
        self.vl_encoder = ImageEncoder(model_path, backend, vision_config,
                                       backend_config=backend_config,
                                       trust_remote_code=trust_remote_code)
        if backend_config and backend_config.enable_prefix_caching:
            supports_prefix_caching = backend == 'pytorch' and getattr(self.vl_encoder, '_uses_new_preprocess', False)
            if not supports_prefix_caching:
                backend_config.enable_prefix_caching = False
                logger.warning('Prefix caching is disabled for this VL model path. ...')
        super().__init__(model_path, backend=backend, backend_config=backend_config,
                         trust_remote_code=trust_remote_code, **kwargs)
        self.prompt_processor = MultimodalProcessor(self.tokenizer, self.chat_template,
                                                    vl_encoder=self.vl_encoder, backend=backend)
        if self.model_name == 'base':
            raise RuntimeError('please specify chat template ...')
```

参见 [vl_async_engine.py:12-52](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/vl_async_engine.py#L12-L52)。

几个关键点：

- **延迟 import**（第 22-24 行）：`MultimodalProcessor`、`ImageEncoder`、`try_import_deeplink` 都在方法内导入，避免在纯文本场景下也无谓地拉起视觉依赖（视觉栈很重）。
- **`ImageEncoder` 在 `super().__init__` 之前建**（第 28 行）：因为父类构造会用到 `backend_config`，而这里可能要先改 `backend_config.enable_prefix_caching`。
- **前缀缓存约束**（第 33-38 行）：只有 PyTorch 后端 + 新版预处理（`_uses_new_preprocess`）才支持 VLM 前缀缓存，否则关掉并告警——这是 u9-l3（Prefix 缓存）会展开的话题，本讲只需知道「VLM 下前缀缓存有额外门槛」。
- **覆盖 `close`**（见下）：先释放视觉编码器再调父类 `close`。

```python
def close(self):
    if hasattr(self, 'vl_encoder'):
        del self.vl_encoder
        super().close()
```

参见 [vl_async_engine.py:54-57](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/vl_async_engine.py#L54-L57)。

**多模态处理器做什么。** `MultimodalProcessor.get_prompt_input` 统一处理文本与多模态：字符串直接走文本路径；list 输入先探测 `_has_multimodal_input`，若有图片且 `vl_encoder` 可用，才走多模态分支产出带 `multimodal` 数据的字典。

```python
async def get_prompt_input(self, prompt, do_preprocess, ...):
    if isinstance(prompt, str):
        return await self._get_text_prompt_input(prompt=prompt, ...)
    elif isinstance(prompt, list):
        has_multimodal_input = self._has_multimodal_input(prompt)
        if not has_multimodal_input or self.vl_encoder is None:
            return await self._get_text_prompt_input(prompt=prompt, ...)
        # Process multimodal input → 返回带 multimodal 字段的 dict
        ...
```

参见 [multimodal.py:196-227](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/processors/multimodal.py#L196-L227)。返回的 `multimodal` 字段会经 `generate()` → `safe_run` 的 `**kwargs` 透传给底层引擎（这也是 `safe_run` 里 `kwargs.pop('multimodal', None)` 的由来——避免在异常路径上长期持有大块多模态张量）。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：验证「`VLAsyncEngine` 没有重写 `generate()`，多模态能力靠替换处理器注入」这一结论。
2. **操作步骤**：
   - 打开 [vl_async_engine.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/vl_async_engine.py)，确认整个类只有 `__init__` 和 `close` 两个方法，没有 `generate`。
   - 回到 [async_engine.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py)，在 `generate()` 里找到 `prompt_input = await self.prompt_processor.get_prompt_input(...)`（[L544](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L544)）和 `**prompt_input`（[L657](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L657)），确认多模态数据是顺着这两个点流进底层引擎的。
3. **需要观察的现象**：`prompt_input` 这个 dict 在 VLM 下会多一个 `multimodal` 键。
4. **预期结果**：能解释「`VLAsyncEngine` 通过依赖注入（换 `prompt_processor`）而非方法重写来获得多模态能力」。

#### 4.3.5 小练习与答案

**练习 1**：`VLAsyncEngine` 为什么要在调用 `super().__init__()` **之前**就构造 `self.vl_encoder`？

**参考答案**：因为父类 `__init__` 会读取并使用 `backend_config`，而子类需要在父类之前根据 `vl_encoder` 的能力（`_uses_new_preprocess`）决定是否关闭 `backend_config.enable_prefix_caching`。若先 `super().__init__()`，错误的 prefix caching 配置就已经被引擎消费了。

**练习 2**：既然 `VLAsyncEngine` 没有重写 `generate()`，那纯文本输入在 VLM 引擎里会不会出错？

**参考答案**：不会。`MultimodalProcessor.get_prompt_input` 会先用 `_has_multimodal_input` 探测，纯文本（或没有可用 `vl_encoder`）时退回 `_get_text_prompt_input`，行为与纯文本 `AsyncEngine` 一致。所以同一个 VLM 引擎既能图文混排也能纯文本。

### 4.4 健康检查机制 health.py

#### 4.4.1 概念说明

线上服务必须回答负载均衡器一个简单问题：「你还能不能正常处理请求？」这就是健康检查（health check）。回答它有两条互相补充的路径：

1. **周期性后台探活**：一个独立协程每隔一段时间主动问引擎「你还活着吗」，把结果缓存下来。
2. **按需探活**：当 `/health` 路由被访问时，如果缓存显示「不健康」，就立刻再探一次以确认。

`lmdeploy/serve/core/health.py` 的 `EngineHealthMonitor` 就是干这两件事的总管。它的设计要点是「**有界、不重叠、可过期**」：

- **有界**：每次探活用 `asyncio.wait_for` 套超时，引擎卡死时探活不会无限挂起。
- **不重叠**：用 `asyncio.Lock` 保证同一时刻只有一个探活在跑（后台轮询与 `/health` 触发的即时探活共享同一把锁）。
- **可过期**：即便上次探活成功，若超过 `unhealthy_after` 秒没有新的成功探活或调度进展，也判为不健康——应对「探活协程自己被卡住」的极端情况。

健康状态取值：`initializing`（启动中）、`healthy`（健康）、`sleeping`（显存被 sleep 卸载，非异常）、`unhealthy`（不健康）、`pending`（上一次探活还没返回，内部态）。

#### 4.4.2 核心流程

健康检查涉及三层协作：

```text
api_server /health 路由
   │  读 monitor.snapshot()
   │  若 unhealthy → await monitor.refresh_snapshot()（即时再探一次）
   ▼
EngineHealthMonitor（health.py，缓存层 + 调度探活）
   │  后台 _run(): 每 poll_interval 秒 probe_once()
   │  probe_once → async_engine.health_probe(timeout, scheduler_stall_timeout)
   ▼
AsyncEngine.health_probe（async_engine.py，翻译层）
   │  asyncio.create_task(self.engine.get_health_status())
   │  asyncio.wait_for(..., timeout)
   │  再用 _validate_scheduler_progress 校验调度器在前进
   ▼
Engine.get_health_status（engine.py，引擎层，真正回答）
   │  检查 req_manager.is_loop_alive() / 主循环 / engine_loop 任务是否还活着
   │  返回 dict(alive=..., message=..., schedule_metrics=...)
```

「探针如何获取引擎状态」的完整答案就是这条链：`EngineHealthMonitor` 经 `AsyncEngine.health_probe` 调到底层 `Engine.get_health_status`，后者检查推理循环任务是否还活着、并附带 `schedule_metrics`（调度器在前进的证据），`AsyncEngine` 再据此判断调度器是否真的在推进。

#### 4.4.3 源码精读

**环境变量与默认值。** 三个超时阈值都可被环境变量覆盖，默认值见常量定义：

```python
HEALTH_POLL_INTERVAL = 'LMDEPLOY_HEALTH_POLL_INTERVAL'
HEALTH_PROBE_TIMEOUT = 'LMDEPLOY_HEALTH_PROBE_TIMEOUT'
HEALTH_UNHEALTHY_AFTER = 'LMDEPLOY_HEALTH_UNHEALTHY_AFTER'

DEFAULT_PROBE_TIMEOUT = 10.0      # 单次探活最长等待
DEFAULT_POLL_INTERVAL = 12.0      # 后台轮询间隔（应 > probe_timeout，否则告警）
DEFAULT_UNHEALTHY_AFTER = 90.0    # 多久没进展就判不健康
```

参见 [health.py:16-22](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/health.py#L16-L22)。`_env_override_float`（[health.py:25-33](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/health.py#L25-L33)）实现「环境变量优先、解析失败回退默认」。

**`EngineHealthMonitor` 的构造与启动。** 构造时把三阈值经环境变量覆盖、建探活锁、初始化快照为 `initializing`：

```python
class EngineHealthMonitor:
    def __init__(self, async_engine, poll_interval=DEFAULT_POLL_INTERVAL,
                 probe_timeout=DEFAULT_PROBE_TIMEOUT, unhealthy_after=DEFAULT_UNHEALTHY_AFTER):
        self.async_engine = async_engine
        self.poll_interval = _env_override_float(HEALTH_POLL_INTERVAL, poll_interval)
        self.probe_timeout = _env_override_float(HEALTH_PROBE_TIMEOUT, probe_timeout)
        self.unhealthy_after = _env_override_float(HEALTH_UNHEALTHY_AFTER, unhealthy_after)
        if self.poll_interval <= self.probe_timeout:
            logger.warning(...)                       # 间隔应大于超时，避免探活重叠
        self._task: asyncio.Task | None = None
        self._started_time = time.monotonic()
        self._last_success_time: float | None = None
        self._probe_lock = asyncio.Lock()             # 探活互斥锁
        self._snapshot = dict(status='initializing', message='Engine health monitor is starting.')
```

参见 [health.py:36-79](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/health.py#L36-L79)。`start`/`stop`（[health.py:81-94](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/health.py#L81-L94)）创建/取消后台任务；`_run`（[health.py:96-99](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/health.py#L96-L99)）就是「探一次 → 睡 `poll_interval` 秒 → 循环」。

**探活核心。** `probe_once` 拿锁后调 `_probe_once_unlocked`，后者真正调 `async_engine.health_probe`：

```python
async def _probe_once_unlocked(self):
    probe_time = time.monotonic()
    if self.async_engine is None:
        result = dict(status='unhealthy', message='Async engine is not initialized.')
    else:
        try:
            result = await self.async_engine.health_probe(timeout=self.probe_timeout,
                                                          scheduler_stall_timeout=self.unhealthy_after)
        except Exception as e:
            result = dict(status='unhealthy', message=f'Engine health probe failed: {e}')
    status = result['status']
    if status == 'pending':
        logger.info('Engine health probe skipped: previous backend health probe is still pending.')
        return
    if status in ('healthy', 'sleeping'):
        self._last_success_time = probe_time     # 记录最近一次成功时刻，供 snapshot 判过期
    self._snapshot = dict(status=status, message=result['message'])
```

参见 [health.py:105-124](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/health.py#L105-L124)。`refresh_snapshot`（[health.py:126-129](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/health.py#L126-L129)）= 主动探一次再返回快照，供 `/health` 路由即时确认用。

**带过期判断的快照。** `snapshot()` 在返回前做两道过期检查——「健康」状态太久没更新、或「初始化」状态太久没完成首次探活，都改成 `unhealthy`：

```python
def snapshot(self) -> dict:
    snapshot = dict(self._snapshot)
    now = time.monotonic()
    if snapshot['status'] == 'healthy' and self._last_success_time is not None:
        if now - self._last_success_time > self.unhealthy_after:
            snapshot['status'] = 'unhealthy'
            snapshot['message'] = f'No successful health probe for {now - self._last_success_time:.1f}s.'
    elif snapshot['status'] == 'initializing' and now - self._started_time > self.unhealthy_after:
        snapshot['status'] = 'unhealthy'
        snapshot['message'] = 'Engine health monitor did not complete an initial probe.'
    return snapshot
```

参见 [health.py:131-141](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/health.py#L131-L141)。注意它用 `time.monotonic()`（单调时钟）而非墙上时钟，因为健康检查只关心「间隔」，不受系统时间回调影响。

**`AsyncEngine.health_probe`：有界探活 + 防重入 + 调度进展校验。** 这一层是 `EngineHealthMonitor` 与底层引擎之间的翻译官，它做三件事：

```python
async def health_probe(self, timeout, scheduler_stall_timeout) -> dict:
    if self.is_sleeping:
        return self._make_health_result(status='sleeping', message='Engine is sleeping.')

    # 防重入：上一次探活还没结束就返回 pending
    if self._health_probe_task is not None:
        if not self._health_probe_task.done():
            return self._make_health_result(status='pending', message='Previous backend health probe is still pending.')
        ...

    # 有界探活：asyncio.wait_for 套超时，asyncio.shield 防内部任务被取消
    self._health_probe_task = asyncio.create_task(self.engine.get_health_status(), name='EngineHealthProbe')
    try:
        backend_status = await asyncio.wait_for(asyncio.shield(self._health_probe_task), timeout=timeout)
    except asyncio.TimeoutError:
        return self._make_health_result(status='unhealthy', message=f'Backend health probe timed out after {timeout:.1f}s.')
    except Exception as e:
        ...

    if not backend_status['alive']:
        return self._make_health_result(status='unhealthy', ...)

    # 调度进展校验：确保调度器在前进，而非只是循环还活着
    schedule_metrics = backend_status['schedule_metrics']
    ...
    valid_progress, invalid_message = self._validate_scheduler_progress(schedule_metrics, scheduler_stall_timeout=...)
    if not valid_progress:
        return self._make_health_result(status='unhealthy', message=invalid_message)
    return self._make_health_result(status='healthy', ...)
```

参见 [async_engine.py:285-356](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L285-L356)。`asyncio.shield` 保护 `_health_probe_task`：即便 `wait_for` 因超时抛 `TimeoutError`，底层 `get_health_status` 任务也不会被连带取消（避免探活任务被反复中断留下脏状态）。

**`_validate_scheduler_progress`：判断调度器是否真的在推进。** 这是健康检查的精髓——「推理循环还活着」不等于「还能干活」。它综合三个信号：调度器 `scheduler_tick` 是否在增长、是否有已派发的请求句柄（`num_dispatched`）、已派发但调度器长期空转（`active_seqs + waiting_seqs == 0`）是否超时：

```python
def _validate_scheduler_progress(self, metrics, scheduler_stall_timeout):
    now = time.monotonic()
    if self._last_scheduler_tick is None or metrics.scheduler_tick != self._last_scheduler_tick:
        self._last_scheduler_tick = metrics.scheduler_tick      # tick 前进了，刷新时间
        self._last_scheduler_tick_time = now
        self._idle_schedule_start_time = None

    if self.session_mgr.request_handle_pool.num_dispatched == 0:
        return True, ''                                          # 没有在途请求，无需推进

    if metrics.active_seqs + metrics.waiting_seqs == 0:
        # 有在途请求却没有活跃/等待序列 → 可能卡住
        if now - self._idle_schedule_start_time > scheduler_stall_timeout:
            return False, '...'
    ...
    last_progress_time = max(self._last_scheduler_tick_time, self._dispatched_start_time)
    if now - last_progress_time > scheduler_stall_timeout:
        return False, f'Backend scheduler_tick has not advanced for {now - self._last_scheduler_tick_time:.1f}s.'
    return True, ''
```

参见 [async_engine.py:252-279](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L252-L279)。其中 `num_dispatched` 来自句柄池（[session_manager.py:193-198](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/managers/session_manager.py#L193-L198)）。

**底层引擎如何回答。** `Engine.get_health_status` 检查推理循环相关任务是否还活着，并附上 `schedule_metrics`：

```python
async def get_health_status(self) -> dict:
    if not self.req_manager.is_loop_alive():
        return dict(alive=False, message='PyTorch engine request loop is not alive.', schedule_metrics=None)
    if self._loop_main is not None and self._loop_main.done():
        return dict(alive=False, message='PyTorch engine main loop has stopped.', schedule_metrics=None)
    if self._engine_loop is not None:
        engine_loop_ok, done_tasks = self._health_check_tasks(self._engine_loop.tasks)
        if not engine_loop_ok:
            return dict(alive=False, message=f'PyTorch engine loop task has stopped: {done_tasks}.', schedule_metrics=None)
    return dict(alive=True, message='PyTorch engine is healthy.',
                schedule_metrics=self.get_schedule_metrics())
```

参见 [engine.py:699-725](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L699-L725)。注释（L700-L703）点出关键：调度器指标在运行时失败后仍可能可读，所以必须先检查 Engine 拥有的循环任务是否还活着，再返回指标。`schedule_metrics` 由调度器的 `schedule_metrics` 属性提供：

```python
@property
def schedule_metrics(self):
    return ScheduleMetrics(
        active_seqs=self.num_running(),
        waiting_seqs=self.num_waiting() + self.num_ready(),
        total_blocks=..., free_blocks=...,
        prefix_cache_hit_rate=self.block_trie.hit_rate(),
        scheduler_tick=self.scheduler_tick,    # 每次 schedule() 自增（见 scheduler.py:145）
    )
```

参见 [scheduler.py:914-923](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L914-L923)。

**`/health` 路由与 lifespan 装配。** FastAPI 路由直接读 monitor 快照，仅在缓存显示不健康时才即时再探：

```python
@router.get('/health')
async def health() -> JSONResponse:
    monitor = VariableInterface.health_monitor
    if monitor is None:
        data = dict(status='unhealthy', message='Engine health monitor is not initialized.')
        return JSONResponse(jsonable_encoder(data), status_code=HTTPStatus.SERVICE_UNAVAILABLE)
    data = monitor.snapshot()
    if data['status'] == 'unhealthy':
        data = await monitor.refresh_snapshot()        # 缓存说不健康，立刻再确认一次
    status_code = HTTPStatus.OK if data['status'] in ('healthy', 'sleeping') else HTTPStatus.SERVICE_UNAVAILABLE
    return JSONResponse(jsonable_encoder(data), status_code=status_code)
```

参见 [api_server.py:292-304](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_server.py#L292-L304)。`EngineHealthMonitor` 在 FastAPI lifespan 里创建并启动，应用关闭时停止：

```python
health_monitor = EngineHealthMonitor(async_engine)
VariableInterface.health_monitor = health_monitor
try:
    health_monitor.start()
    ...
    yield
finally:
    await health_monitor.stop()
    ...
```

参见 [api_server.py:1473-1497](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_server.py#L1473-L1497)。

#### 4.4.4 代码实践（可运行 / 源码阅读型）

1. **实践目标**：亲眼看到 `/health` 返回的引擎状态，并理解探针如何获取它。
2. **操作步骤**：
   - （需 GPU 与权重）启动服务：`lmdeploy serve api_server <model>`。
   - 另开终端：`curl -i http://127.0.0.1:23333/health`，观察返回的 JSON（`status` 字段）与 HTTP 状态码（健康为 200，不健康为 503）。
   - 尝试用环境变量调整阈值后重启，观察行为变化：`LMDEPLOY_HEALTH_POLL_INTERVAL=5 LMDEPLOY_HEALTH_PROBE_TIMEOUT=3 lmdeploy serve api_server <model>`。
   - （源码阅读型，无需运行）阅读 [health.py:105-124](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/health.py#L105-L124) → [async_engine.py:285-356](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L285-L356) → [engine.py:699-725](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L699-L725)，画出探针从 `/health` 到 `get_health_status` 的完整调用链。
3. **需要观察的现象**：服务刚启动时 `/health` 可能短暂返回 `initializing`，随后变为 `healthy`；手动 `/sleep` 后会变为 `sleeping`。
4. **预期结果**：能一句话说明「健康检查探针经 `EngineHealthMonitor` → `AsyncEngine.health_probe` → `Engine.get_health_status` 获取引擎状态，前者管缓存与轮询，中间层管有界与防重入，后者真正检查推理循环任务是否存活并返回调度指标」。若无法本地运行，明确标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`EngineHealthMonitor` 已经在后台周期性探活了，为什么 `/health` 路由还要在缓存显示 `unhealthy` 时再 `refresh_snapshot()` 一次？

**参考答案**：后台轮询有间隔（默认 12 秒），缓存里的 `unhealthy` 可能是十几秒前的旧状态——引擎可能已经恢复。在返回 503 给负载均衡器（进而摘除流量）之前，立刻再探一次能避免误摘；这是一次「确认性探活」，且因 `_probe_lock` 而不会与后台探活重叠。

**练习 2**：`health_probe` 里 `asyncio.wait_for(asyncio.shield(self._health_probe_task), timeout)` 同时用了 `wait_for` 和 `shield`，各起什么作用？

**参考答案**：`wait_for(timeout)` 给探活设上限——引擎卡死时探活不会无限挂起，超时即判 `unhealthy`。`shield` 保护内部 `_health_probe_task`——当 `wait_for` 因超时抛 `TimeoutError` 时，`wait_for` 默认会取消它等待的协程，但 `shield` 阻止这种连带取消，让 `get_health_status` 任务得以保留（其结果由下一次 `health_probe` 通过 `_health_probe_task.done()` / `.result()` 回收，见 [async_engine.py:293-305](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L293-L305)）。

**练习 3**：为什么 `_validate_scheduler_progress` 在 `num_dispatched == 0` 时直接返回健康？

**参考答案**：`num_dispatched == 0` 表示当前没有任何请求句柄被借出（没有在途请求）。没有请求时调度器本就不需要推进 `scheduler_tick`，把它判成不健康是误报。只有「有在途请求（`num_dispatched > 0`）但调度器长期不前进」才是真正的卡死。

## 5. 综合实践

把本讲三个最小模块串起来，完成一个「**读懂一次完整请求在服务侧的旅程**」的任务。

**任务**：给定一次 `POST /v1/chat/completions` 请求（带一张图片），请在源码中标注它从进入服务到产出第一个 token，依次经过 `AsyncEngine` 的哪些方法、在哪一行真正转发到底层引擎、以及这一过程中 `EngineHealthMonitor` 是否会介入。

**建议步骤**：

1. 从 [api_server.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_server.py) 的 `/v1/chat/completions` 路由（u8-l1 已讲）找到它最终调用 `async_engine.generate(...)` 的位置。
2. 进入 [async_engine.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py) 的 `generate()`，依次标注：`session_mgr.get` → `prompt_processor.get_prompt_input`（VLM 下走 [multimodal.py:196](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/processors/multimodal.py#L196) 的多模态分支）→ `_determine_gen_config` → `session.request_handle()` → `safe_run` → **[async_engine.py:429](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L429) `handle.async_stream_infer`**（转发点）→ `async for outputs in gen` → `detokenize_incrementally` → `yield GenOut`。
3. 说明：正常推理期间 `/health` 的后台探活由独立协程跑，与本次请求互不干扰；若请求触发了 `safe_run` 的异常清理路径（`async_cancel`/`async_end`），不影响 `EngineHealthMonitor` 的快照——只有调度器长期不前进或循环任务停止才会让快照变 `unhealthy`。
4. 把这条链路画成一张时序图（请求主线 vs. 健康探活旁路），作为本讲的学习产出。

## 6. 本讲小结

- `AsyncEngine` 是服务层（FastAPI 路由）与引擎层（PyTorch `Engine` / TurboMind `TurboMind`）之间的**后端无关异步封装层**：替上层接管会话管理、chat 模板、流式去词表化、统计上报与睡眠/唤醒，对下通过 `EngineBase`/`EngineInstanceBase` 契约屏蔽两套后端差异。
- **把请求转发到底层 engine 的那一行是 [async_engine.py:429](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L429) 的 `handle.async_stream_infer(session.session_id, **kwargs)`**，藏在 `safe_run` 上下文里；`handle` 是从 `RequestHandlePool` 借来的轻量句柄。`generate()` 是 async generator，用 `yield GenOut` 一段段产出。
- `safe_run` 用 `asyncio.shield` 保护清理协程、用 `SafeRunException` 包装异常，使「已处理的取消」能被 `Session.request_handle` 精确识别，避免任务残留 cancelling 状态。
- `VLAsyncEngine` 以**最小增量**继承 `AsyncEngine`：只加 `vl_encoder`、替换 `prompt_processor` 为多模态版、加少量约束（前缀缓存门槛、必须指定 chat 模板），**不重写 `generate()`**——多模态能力靠依赖注入获得。
- 健康检查由 `EngineHealthMonitor`（缓存 + 后台轮询）+ `AsyncEngine.health_probe`（有界、防重入、校验调度进展）+ `Engine.get_health_status`（检查循环任务存活并返回 `schedule_metrics`）三层协作；`/health` 路由读缓存、不健康时再即时确认一次。
- 健康判断不仅看「循环是否活着」，更看「调度器是否在前进」：`_validate_scheduler_progress` 综合 `scheduler_tick` 增长与 `num_dispatched` 判断卡死，避免「活着但不干活」的假健康。

## 7. 下一步学习建议

- **向下深入引擎层**：本讲的转发点 `handle.async_stream_infer` 在 PyTorch 侧落到 `EngineInstance`（u4-l3 已讲其「请求—回应乒乓」），建议重读 [engine_instance.py:175](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_instance.py#L175) 看它如何把请求投进 `RequestManager` 信箱。
- **PD 分离的复用**：`AsyncEngine` 末尾的 `free_cache` / `p2p_*` 方法（[async_engine.py:820-837](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L820-L837)）是 u9-l5（PD 分离）的服务侧入口，`GenOut.cache_block_ids` 承载 KV 块号跨节点传输，学完 u9-l5 可回看本讲理解全链路。
- **多模态全链路**：本讲只到 `VLAsyncEngine` 的封装层，真正的视觉编码与多模态输入注入在 `lmdeploy/vl/` 与 `lmdeploy/pytorch/multimodal/`，这正是 u9-l1 的主题。
- **可观测性**：本讲多次出现 `metrics_processor`，统计与 Prometheus 指标的细节是 u10-l4 的内容；建议结合本讲的 `do_log_stats`、`IterationStats`、`schedule_metrics` 一起读。
