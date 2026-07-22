# 两种使用入口：HTTP 服务 vs 进程内 Engine

## 1. 本讲目标

本讲承接 u1-l2（`sglang serve` 的安装与首次运行）。学完本讲，你应该能够：

- 说清 SGLang 的「真正计算核心」是什么，以及它有两种「包装方式」。
- 看懂 `launch_server.run_server` 如何根据几个布尔标志把请求分发给不同的服务形态。
- 掌握进程内 `Engine` 类的构造方式，以及 `generate` / `async_generate` 的调用模式。
- 理解 `EngineBase` 这层抽象为什么让两种入口「API 长得几乎一样」。
- 面对一个实际场景（在线服务 / 离线批量 / RL 后端），能判断该用 `sglang serve` 还是 `sgl.Engine`。

---

## 2. 前置知识

在进入两种入口之前，先建立两个直觉。

### 2.1 SGLang 的「引擎」是一组多进程

不管你用哪种入口，最终干活的都是同一套「SRT 引擎（SGLang Runtime engine）」。它在源码里被描述为三个组件：

1. **TokenizerManager**：运行在**主进程**里，负责把请求文本分词，再转发给调度器。
2. **Scheduler**：运行在**子进程**里，负责组批、调度、执行前向，把输出 token 发给解分词器。
3. **DetokenizerManager**：运行在**子进程**里，把 token 还原成文本，再送回 TokenizerManager。

这些进程之间通过 **ZMQ + msgspec** 做进程间通信（IPC，每个进程用不同端口）。这套多进程结构是 u2 的主题，本讲只需要记住一句话：**引擎 = 一组子进程 + 一个主进程里的 TokenizerManager**。

### 2.2 「入口」就是这组引擎的「外壳」

既然引擎是一组进程，那么「怎么用」它，就变成了「在这组进程外面套什么外壳」：

- 套一层 **HTTP 服务（FastAPI + uvicorn）** → 这就是 `sglang serve`。
- **不套外壳**，直接在你的 Python 进程里 `new` 一个对象调用 → 这就是 `sgl.Engine`。

本讲要回答的核心问题就是：**这两个外壳有什么不同、该用哪个？**

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/sglang/launch_server.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/launch_server.py) | HTTP 服务入口的总分发器 `run_server`，根据标志选择不同服务形态。 |
| [python/sglang/srt/entrypoints/http_server.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/http_server.py) | 默认 HTTP 模式的 `launch_server`：拉起引擎子进程 + 跑 uvicorn/FastAPI。 |
| [python/sglang/srt/entrypoints/engine.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/engine.py) | 进程内 `Engine` 类：`__init__` / `generate` / `async_generate` 的实现。 |
| [python/sglang/srt/entrypoints/EngineBase.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/EngineBase.py) | 抽象基类 `EngineBase`，定义两种入口共享的统一接口。 |
| [examples/runtime/engine/launch_engine.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/examples/runtime/engine/launch_engine.py) | 最小的进程内 Engine 示例。 |
| [examples/runtime/engine/offline_batch_inference.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/examples/runtime/engine/offline_batch_inference.py) | 进程内 Engine 的批量推理示例。 |
| [examples/runtime/engine/offline_batch_inference_async.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/examples/runtime/engine/offline_batch_inference_async.py) | 进程内 Engine 的异步并发推理示例。 |

---

## 4. 核心概念与源码讲解

### 4.1 两种入口的全景图：先建立心智模型

#### 4.1.1 概念说明

SGLang 的推理能力封装在一个「引擎」里。这个引擎本身是进程级的、异步的。要在不同场景下复用它，SGLang 提供了两种「入口」（entry point）：

- **HTTP 服务入口**：进程启动后，对外暴露 OpenAI 兼容的 HTTP API（如 `/v1/chat/completions`、`/v1/completions`）。任何能发 HTTP 请求的客户端（curl、openai SDK、前端页面）都能连。典型命令是 `sglang serve`。
- **进程内 Engine 入口**：把引擎当成一个普通 Python 对象，在你的代码里 `llm = sgl.Engine(...)` 然后 `llm.generate(...)`。没有网络、没有 HTTP 解析，函数调用直达引擎。

#### 4.1.2 核心流程

关键洞察是：**两条路最终都调用同一个 `_launch_subprocesses`，拉起同一套引擎子进程。区别只在于「主进程里有没有一层 FastAPI/uvicorn 外壳」。**

用伪代码/流程图表示：

```
            ┌─────────────────────────────── sglang serve (HTTP 入口) ──────────────────────────────┐
            │                                                                                       │
            │   run_server(args)                                                                    │
            │      └─► http_server.launch_server(args)                                              │
            │              ├─► Engine._launch_subprocesses(args)   ← 和右边完全相同                │
            │              │        ├─► TokenizerManager (主进程)                                   │
            │              │        ├─► Scheduler        (子进程)                                   │
            │              │        └─► DetokenizerManager(子进程)                                  │
            │              └─► _setup_and_run_http_server(...)                                       │
            │                       └─► uvicorn.run(FastAPI app)   ← 多出来的一层外壳               │
            │                                                                                       │
            │   请求路径: HTTP 请求 → FastAPI 路由 → tokenizer_manager.generate_request(...)         │
            └───────────────────────────────────────────────────────────────────────────────────────┘

            ┌─────────────────────────────── sgl.Engine (进程内入口) ───────────────────────────────┐
            │                                                                                       │
            │   llm = sgl.Engine(**kwargs)                                                          │
            │      └─► Engine.__init__                                                              │
            │              ├─► Engine._launch_subprocesses(args)   ← 和左边完全相同                │
            │              │        ├─► TokenizerManager (主进程, 存为 self.tokenizer_manager)      │
            │              │        ├─► Scheduler        (子进程)                                   │
            │              │        └─► DetokenizerManager(子进程)                                  │
            │              （不启动 FastAPI / uvicorn）                                              │
            │                                                                                       │
            │   请求路径: llm.generate(...) → 直接调用 self.tokenizer_manager.generate_request(...) │
            └───────────────────────────────────────────────────────────────────────────────────────┘
```

注意：在两条路里，**HTTP server、Engine、TokenizerManager 都运行在主进程里**（这是源码 docstring 里反复强调的一句话，见 [engine.py:192-194](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/engine.py#L192-L194) 和 [http_server.py:2657-L2659](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/http_server.py#L2657-L2659)）。真正在子进程里的只有 Scheduler 和 DetokenizerManager。

#### 4.1.3 源码精读：谁在调用 `_launch_subprocesses`

先看 HTTP 那条路。`http_server.launch_server` 的函数体非常短：

```python
# http_server.py:2661-L2684（节选）
# Launch subprocesses
(tokenizer_manager, template_manager, port_args,
 scheduler_init_result, subprocess_watchdog) = Engine._launch_subprocesses(
    server_args=server_args, ...)

_setup_and_run_http_server(server_args, tokenizer_manager, ...)
```

可以看到，HTTP 服务的第一步就是 `Engine._launch_subprocesses(...)`——和进程内 `Engine` 用的是**同一个类方法**。唯一多出来的事是 `_setup_and_run_http_server`，它负责把 FastAPI app 跑在 uvicorn 上。

再看进程内那条路。`Engine.__init__` 里同样调用 `_launch_subprocesses`（[engine.py:235-L246](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/engine.py#L235-L246)）：

```python
# engine.py（节选）
(tokenizer_manager, template_manager, port_args,
 scheduler_init_result, subprocess_watchdog) = self._launch_subprocesses(...)
self.tokenizer_manager = tokenizer_manager
```

这就从源码层面证明了「两种入口共享同一套引擎子进程」。

#### 4.1.4 代码实践

> 实践目标：在不启动任何服务的前提下，用「读源码」的方式验证两种入口共享 `_launch_subprocesses`。

1. 用搜索工具在 `python/sglang/srt/entrypoints/` 下找出所有调用 `_launch_subprocesses` 的位置。
2. 预期会发现至少两处：`engine.py`（进程内 Engine 构造时）和 `http_server.py`（HTTP 服务启动时）。
3. 记录这两处各自的「下一步」：进程内 Engine 拿到 `tokenizer_manager` 后直接存为属性；HTTP 服务拿到后传给 `_setup_and_run_http_server`。

预期结果：你会清晰地看到「HTTP 服务 = 引擎子进程 + FastAPI 外壳」，而进程内 Engine「只有引擎子进程、没有外壳」。

#### 4.1.5 小练习与答案

**练习 1**：有人说「`sglang serve` 和 `sgl.Engine` 是两套完全不同的引擎实现」，对吗？

> **答案**：不对。两者底层调用同一个 `Engine._launch_subprocesses`，拉起的是同一套 TokenizerManager + Scheduler + DetokenizerManager 子进程。区别只在主进程里是否多了一层 FastAPI/uvicorn HTTP 外壳。

**练习 2**：在两条入口里，「TokenizerManager」分别运行在哪个进程？

> **答案**：都运行在**主进程**里。源码 docstring 明确说「The HTTP server, Engine, and TokenizerManager all run in the main process」。所以无论 HTTP 还是进程内，TokenizerManager 都不在子进程。

---

### 4.2 HTTP 服务入口：`launch_server.run_server` 的模式分发

#### 4.2.1 概念说明

`python/sglang/launch_server.py` 里的 `run_server(server_args)` 是一个**模式分发器（dispatcher）**。它本身不做推理，只做一件事：根据 `server_args` 上的几个布尔标志，把启动流程路由到不同的「服务形态」。默认形态就是大家最熟悉的 HTTP 服务。

理解分发器的好处是：你会知道 `sglang serve` 其实不是一个单一程序，而是根据参数变成好几种东西（普通 HTTP 服务、Ray 分布式、gRPC 服务、编码分离服务等）。这些高级形态分别在 u8/u9 讲，本讲只聚焦默认的 HTTP 形态。

#### 4.2.2 核心流程

`run_server` 的分发逻辑是一个 `if / elif / else` 链，优先级从上到下：

1. 若 `server_args.encoder_only` 为真（编码分离，多模态编码器专用）：
   - 若同时开了 grpc → 走 `serve_grpc_encoder`；
   - 否则 → 走 `encode_server.launch_server`。
2. 若 `server_args.smg_grpc_mode` 为真 → 走 `grpc_server.serve_grpc`（legacy gRPC）。
3. 若 `server_args.use_ray` 为真 → 走 `sglang.srt.ray.http_server.launch_server`（Ray 分布式后端）。
4. **否则（默认）→ 走 `sglang.srt.entrypoints.http_server.launch_server`**，即标准 HTTP 服务。

注意第 4 步才是 `sglang serve` 不带特殊参数时真正走的路。

#### 4.2.3 源码精读

分发逻辑全部集中在一个函数里，读起来像伪代码：

[launch_server.py:15-L52](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/launch_server.py#L15-L52) —— `run_server` 根据 `encoder_only` / `smg_grpc_mode` / `use_ray` 三个标志，把启动流程分到 encode 分离、gRPC、Ray、默认 HTTP 四种形态；默认 HTTP 形态调用 `http_server.launch_server`。

精简后的关键分支：

```python
def run_server(server_args):
    if server_args.encoder_only:
        ...  # 编码分离：grpc 或 http
    elif server_args.smg_grpc_mode:
        ...  # legacy gRPC
    elif server_args.use_ray:
        ...  # Ray 分布式
    else:
        # 默认 HTTP 模式
        from sglang.srt.entrypoints.http_server import launch_server
        launch_server(server_args)
```

而 `http_server.launch_server`（[http_server.py:2638-L2684](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/http_server.py#L2638-L2684)）的 docstring 写明：「The SRT server consists of an HTTP server and an SRT engine」，即「SRT 服务 = HTTP server + SRT engine」。它的函数体先 `Engine._launch_subprocesses(...)` 起引擎，再 `_setup_and_run_http_server(...)` 起 uvicorn（[http_server.py:2487-L2524](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/http_server.py#L2487-L2524) 里能看到 `uvicorn.Config(...)` / `uvicorn.Server(...)` 的调用）。

另外提一句：这个文件作为脚本直接运行（`python -m sglang.launch_server`）时会被 `__main__` 警告「推荐改用 `sglang serve`」（[launch_server.py:55-L73](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/launch_server.py#L55-L73)），并最终用 `try/finally + kill_process_tree` 回收子进程——这和 u1-l2 讲过的「派生多子进程后必须清理」是一致的。

#### 4.2.4 代码实践

> 实践目标：亲手启动默认 HTTP 服务，并用最朴素的 HTTP 客户端验证它「是 HTTP」。

操作步骤（需要一个可用的小模型路径与 GPU 环境，**待本地验证**）：

1. 启动服务：
   ```bash
   sglang serve --model-path <小模型路径> --port 30000
   ```
2. 另开一个终端，访问模型列表端点：
   ```bash
   curl http://localhost:30000/v1/models
   ```
3. 发一条 completion 请求：
   ```bash
   curl http://localhost:30000/v1/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"<小模型路径>","prompt":"The capital of France is","max_tokens":8}'
   ```

需要观察的现象：

- 服务启动日志里会出现「拉起子进程」相关的输出（Scheduler / Detokenizer）。
- `/v1/models` 返回 JSON，说明这是一层 HTTP API。
- 请求经过「HTTP → FastAPI → TokenizerManager」流转（细节留到 u2）。

预期结果：你能用纯 HTTP 调用拿到生成结果，证明这条入口的本质是「HTTP 服务」。

#### 4.2.5 小练习与答案

**练习 1**：如果你给 `sglang serve` 加了 `--use-ray`，`run_server` 会走到哪个分支？

> **答案**：会跳过 `encoder_only` 和 `smg_grpc_mode` 两个分支，命中 `elif server_args.use_ray`，调用 `sglang.srt.ray.http_server.launch_server`。注意该分支还会在 `ImportError` 时提示你 `pip install 'sglang[ray]'`。

**练习 2**：默认 HTTP 形态下，`http_server.launch_server` 内部做的两件大事分别是什么？

> **答案**：第一件是 `Engine._launch_subprocesses(...)` 拉起引擎子进程（TokenizerManager + Scheduler + DetokenizerManager）；第二件是 `_setup_and_run_http_server(...)` 把 FastAPI app 跑在 uvicorn 上，对外暴露 HTTP API。

---

### 4.3 进程内 Engine：构造与 `generate`

#### 4.3.1 概念说明

`sgl.Engine`（实际类是 `sglang.srt.entrypoints.engine.Engine`，通过 `__init__.py` 的 `LazyImport` 暴露为 `sgl.Engine`，见 [__init__.py:79](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/__init__.py#L79)）是一个**普通 Python 类**。你可以在自己的脚本里实例化它，像调用函数一样推理，完全不经过 HTTP。

它适合这些场景：

- **离线批量推理**：一次性灌入几万条 prompt，要的是总吞吐，不需要 HTTP 的序列化/解析开销。
- **RL 训练 rollout 后端**：训练循环里直接 `llm.generate(...)` 拿采样结果（u12 会展开）。
- **嵌入式集成**：把推理能力塞进另一个 Python 应用（例如搜索、Agent 框架）里。

它的劣势是不便于跨网络、跨语言访问；并发模型需要你自己用 asyncio 管理。

#### 4.3.2 核心流程

`Engine` 的生命周期分三步：

1. **构造** `Engine(**kwargs)`：`kwargs` 与 `ServerArgs` 的字段一一对应。内部 `load_plugins()` → 构造 `ServerArgs`（默认 `log_level="error"`）→ 注册 `atexit.register(self.shutdown)` → `_launch_subprocesses(...)` → 保存 `self.tokenizer_manager` → 建立 ZMQ socket。
2. **推理** `llm.generate(...)` / `await llm.async_generate(...)`：把参数打包成 `GenerateReqInput`，调用 `self.tokenizer_manager.generate_request(obj, None)` 得到一个**异步生成器**。
   - 同步 `generate`：用 `self.loop.run_until_complete(...)` 驱动这个异步生成器，阻塞直到拿到结果。
   - 异步 `async_generate`：原生 `await`，可与其他任务并发。
3. **关闭** `llm.shutdown()`：停止 watchdog、关 ZMQ socket、`kill_process_tree` 回收所有子进程。也支持 `with sgl.Engine(...) as llm:` 上下文管理（`__exit__` 会自动 shutdown）。

注意一个关键设计：**引擎内核是异步的（基于 asyncio/uvloop）**，而同步 `generate` 只是在外面包了一层「事件循环驱动」。这就是为什么同时存在 `generate` 和 `async_generate`。

#### 4.3.3 源码精读

**类定义**：`Engine` 继承自 `EngineScoreMixin` 和 `EngineBase`（[engine.py:183-L195](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/engine.py#L183-L195)），docstring 写明它由 TokenizerManager + Scheduler + DetokenizerManager 三部分组成，且三者分别运行在主进程/子进程/子进程。

**构造函数**：[engine.py:204-L281](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/engine.py#L204-L281) —— 接收与 `ServerArgs` 相同的 kwargs；先 `load_plugins()`，再构造 `ServerArgs`（未指定时默认 `log_level="error"`，见 [engine.py:220-L223](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/engine.py#L220-L223)），注册 `atexit` 关闭钩子（[engine.py:232](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/engine.py#L232)），然后调用 `_launch_subprocesses` 启动引擎子进程并保存 `self.tokenizer_manager`。关键片段：

```python
def __init__(self, **kwargs):
    load_plugins()
    if "server_args" in kwargs:
        server_args = kwargs["server_args"]
    else:
        if "log_level" not in kwargs:
            kwargs["log_level"] = "error"          # 进程内默认不打日志
        server_args = self.server_args_class(**kwargs)
    self.server_args = server_args

    atexit.register(self.shutdown)                  # 退出时自动回收子进程

    (... ) = self._launch_subprocesses(...)         # 拉起同一套引擎子进程
    self.tokenizer_manager = tokenizer_manager
```

**同步 `generate`**：[engine.py:318-L415](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/engine.py#L318-L415) —— 把入参打包成 `GenerateReqInput`，调用 `self.tokenizer_manager.generate_request(obj, None)` 拿到异步生成器，然后用事件循环驱动它；流式则包成同步生成器返回。核心两步：

```python
obj = GenerateReqInput(text=prompt, input_ids=input_ids, sampling_params=sampling_params, ...)
generator = self.tokenizer_manager.generate_request(obj, None)
# 非流式：阻塞驱动一次
ret = self.loop.run_until_complete(generator.__anext__())
return ret
```

**异步 `async_generate`**：[engine.py:417-L509](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/engine.py#L417-L509) —— 入参与 `generate` 一致，但直接 `await generator.__anext__()`，适合放进 `asyncio.create_task` 里并发。

**关闭**：[engine.py:911-L933](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/engine.py#L911-L933) —— `shutdown()` 停掉 watchdog、关闭 ZMQ、`kill_process_tree(os.getpid(), include_parent=False, wait_timeout=60)` 回收所有子进程；同时实现了 `__enter__` / `__exit__`，所以可以用 `with` 语法。

> 提示：`Engine.generate` 的入参和 `io_struct.py::GenerateReqInput` 完全对应；`Engine.__init__` 的入参和 `server_args.py::ServerArgs` 完全对应。这是 SGLang 一以贯之的「同一套字段，多层复用」风格。

#### 4.3.4 代码实践

> 实践目标：用进程内 Engine 跑一次最小推理，并理解 `__main__` 守卫的必要性。

操作步骤（需要 GPU 与可下载的小模型，**待本地验证**）：

1. 把下面的脚本存为 `my_engine.py`（基于 `examples/runtime/engine/launch_engine.py`）：
   ```python
   import sglang as sgl

   def main():
       llm = sgl.Engine(model_path="<小模型路径>", log_level="info")
       out = llm.generate("The capital of France is", {"max_tokens": 8})
       print(out["text"])
       llm.shutdown()

   if __name__ == "__main__":
       main()
   ```
2. 运行 `python my_engine.py`。

需要观察的现象：

- 脚本会启动若干子进程（Scheduler / Detokenizer），首次启动有明显的模型加载时间。
- `out["text"]` 直接返回生成文本，全程没有 HTTP、没有端口监听。
- **若删掉 `if __name__ == "__main__":` 守卫，会陷入无限递归 spawn 子进程**（见下一节 4.4 的解释）。

预期结果：在纯 Python 进程内拿到生成结果，体感上「就是一次函数调用」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Engine` 同时提供 `generate` 和 `async_generate` 两个方法？

> **答案**：引擎内核是异步的（基于 asyncio/uvloop），`generate_request` 返回异步生成器。`generate` 用 `self.loop.run_until_complete(...)` 把它包成同步阻塞调用，方便简单脚本；`async_generate` 则保留原生 `await`，便于在异步程序里用 `asyncio.create_task` 并发多条请求（如离线批量推理）。

**练习 2**：`Engine.__init__` 接收的参数和哪个类一一对应？

> **答案**：和 `ServerArgs`（`python/sglang/srt/server_args.py`）一一对应。源码注释明确写「The arguments of this function is the same as `sglang/srt/server_args.py::ServerArgs`」。也可以直接传一个现成的 `server_args=` 跳过构造。

---

### 4.4 共享契约 `EngineBase` 与最小示例

#### 4.4.1 概念说明

你可能会问：既然 HTTP 服务和进程内 Engine 是两套外壳，为什么它们的 API（`generate`、`flush_cache`、`update_weights_from_tensor`、`shutdown`……）长得几乎一样？

答案是有一层抽象基类 **`EngineBase`**（[EngineBase.py:7-L77](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/EngineBase.py#L7-L77)）。它的 docstring 写明：「This base class provides a unified API for both HTTP-based engines and engines」——即它为「基于 HTTP 的引擎」和「进程内引擎」提供统一 API。进程内 `Engine` 继承了它；HTTP 侧也有对应的实现，因此上层调用方可以用同一套接口代码切换两种后端。

`EngineBase` 定义的关键抽象方法包括：

- `generate(...)`：生成。
- `flush_cache()`：清空前缀缓存。
- `update_weights_from_tensor(...)`：用内存里的张量热更新权重（RL 常用）。
- `release_memory_occupation()` / `resume_memory_occupation()`：临时释放 / 恢复 GPU 显存占用。
- `shutdown()`：关闭并清理。
- 还有非抽象的 `load_lora_adapter` / `unload_lora_adapter`。

#### 4.4.2 核心流程

三个示例展示了 `Engine` 的三种典型用法：

1. **最小用法**（[launch_engine.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/examples/runtime/engine/launch_engine.py)）：`Engine(...) → generate(...) → shutdown()`，三行核心代码。
2. **批量用法**（[offline_batch_inference.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/examples/runtime/engine/offline_batch_inference.py)）：用 `**dataclasses.asdict(server_args)` 把 CLI 解析出的 `ServerArgs` 直接展开成 `Engine` 的 kwargs；`generate` 一次传入一个 prompt 列表做批量推理。
3. **异步并发用法**（[offline_batch_inference_async.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/examples/runtime/engine/offline_batch_inference_async.py)）：用 `await engine.async_generate(...)` 配合 `asyncio.create_task`，把上百条 prompt 并发投递，模拟「在线式」的批量推理。

还有一个所有示例都强调的细节：**必须有 `if __name__ == "__main__":` 守卫**。因为 `Engine` 会用 `spawn` 方式创建子进程，spawn 会重新导入主模块；如果没有这个守卫，导入时就会再次执行 `sgl.Engine(...)`，从而无限递归地 spawn 子进程。

#### 4.4.3 源码精读

**`EngineBase` 接口**：[EngineBase.py:13-L77](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/EngineBase.py#L13-L77) —— 用 `@abstractmethod` 声明 `generate` / `flush_cache` / `update_weights_from_tensor` / `release_memory_occupation` / `resume_memory_occupation` / `shutdown` 等统一接口。这就是「HTTP 引擎」与「进程内引擎」API 同构的根源。`generate` 的签名很长，但核心是 `prompt`、`sampling_params`、`input_ids`、`image_data`、`stream` 这几项。

**最小示例**：[launch_engine.py:8-L17](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/examples/runtime/engine/launch_engine.py#L8-L17) —— 三行即可完成一次推理：

```python
def main():
    llm = sgl.Engine(model_path="meta-llama/Meta-Llama-3.1-8B-Instruct")
    llm.generate("What is the capital of France?")
    llm.shutdown()

if __name__ == "__main__":
    main()
```

注意文件里第 14-15 行的注释明确解释了 `__main__` 守卫：「Spawn starts a fresh program every time, if there is no __main__, it will run into infinite loop to keep spawning processes」。

**批量示例**：[offline_batch_inference.py:26-L33](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/examples/runtime/engine/offline_batch_inference.py#L26-L33) —— 用 `sgl.Engine(**dataclasses.asdict(server_args))` 把 `ServerArgs` 展开为构造参数，再把一个 prompt 列表传给 `generate`，返回值是一个结果列表：

```python
llm = sgl.Engine(**dataclasses.asdict(server_args))
outputs = llm.generate(prompts, sampling_params)
for prompt, output in zip(prompts, outputs):
    print(f"Prompt: {prompt}\nGenerated text: {output['text']}")
```

**异步示例**：[offline_batch_inference_async.py:19-L50](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/examples/runtime/engine/offline_batch_inference_async.py#L19-L50) —— 把 `async_generate` 包成 `asyncio.create_task`，一次性并发投递多条请求，体现「异步批量」模式。

#### 4.4.4 代码实践

> 实践目标：把「最小用法」改写成「批量用法」，并对比同步与异步的差异。

操作步骤（**待本地验证**）：

1. 参考 `offline_batch_inference.py`，构造一个 4 条 prompt 的列表和一个 `sampling_params = {"temperature": 0.8, "top_p": 0.95}`，用同步 `generate` 一次跑完，记录总耗时。
2. 参考 `offline_batch_inference_async.py`，把同样的 4 条 prompt 用 `async_generate` + `asyncio.create_task` 并发跑完，记录总耗时。
3. 比较两种方式在「批量大小变大（例如 4 → 400）」时的耗时变化。

需要观察的现象：

- 批量很小时，同步与异步耗时接近（瓶颈在模型加载和首 token）。
- 批量变大后，异步并发能把请求「同时」塞进调度器，整体吞吐更高；同步则是一条一条阻塞。

预期结果：你会直观感受到「进程内 Engine + 异步接口」非常适合离线高吞吐场景，而这正是 `Engine` 相对 HTTP 服务的核心优势之一。

#### 4.4.5 小练习与答案

**练习 1**：为什么所有 `Engine` 示例都必须有 `if __name__ == "__main__":` 守卫？

> **答案**：`Engine` 创建子进程时用 `spawn` 方式，spawn 会重新导入主模块。若没有守卫，主模块被导入时会再次执行 `sgl.Engine(...)`，从而再次 spawn 子进程，形成无限递归。守卫保证「只有作为脚本直接运行时才构造 Engine，被 import 时不构造」。

**练习 2**：`EngineBase` 上声明了哪些「RL / 运维」相关的抽象方法？

> **答案**：至少包括 `update_weights_from_tensor(...)`（用张量热更新权重）、`release_memory_occupation()` / `resume_memory_occupation()`（临时释放/恢复显存）、`flush_cache()`（清缓存）以及非抽象的 `load_lora_adapter` / `unload_lora_adapter`。这些方法让 `Engine` 既能做普通推理，也能当 RL rollout 后端（详见 u12）。

---

## 5. 综合实践

本讲的综合实践直接对应本讲的学习目标：**用两种入口加载同一个模型，记录差异**。

### 5.1 任务描述

选一个你本地可用的小模型（例如 `meta-llama/Meta-Llama-3.1-8B-Instruct` 或更小的模型）。完成下面两张对比表。

### 5.2 A 路：HTTP 服务（`sglang serve`）

1. 启动：`sglang serve --model-path <模型> --port 30000`
2. 调用：用 `curl` 或 `openai` SDK 访问 `/v1/completions`。
3. 关闭：`Ctrl+C` 停止服务。

### 5.3 B 路：进程内 Engine（`sgl.Engine`）

1. 写一个带 `if __name__ == "__main__":` 守卫的脚本，参考 `launch_engine.py`。
2. 调用：`llm.generate(prompt)`，打印 `out["text"]`。
3. 关闭：`llm.shutdown()`（或用 `with sgl.Engine(...) as llm:`）。

### 5.4 记录差异（建议填表）

| 维度 | `sglang serve`（HTTP） | `sgl.Engine`（进程内） |
| --- | --- | --- |
| 启动方式 | CLI 命令，后台常驻 | Python 脚本里 `new` 对象 |
| 调用方式 | HTTP 请求 / OpenAI SDK | Python 函数调用 `generate` |
| 是否监听端口 | 是（默认 30000） | 否 |
| 跨网络/跨语言 | 支持 | 不支持 |
| 序列化开销 | 有（HTTP/JSON） | 几乎无 |
| 典型场景 | 在线服务、多客户端 | 离线批量、RL 后端、嵌入集成 |
| 关闭方式 | `Ctrl+C` / kill 进程 | `llm.shutdown()` / `with` 退出 |

### 5.5 思考题

- 同一个模型、同样的 prompt，两种入口生成的文本是否一致？为什么？（提示：关注 `temperature` 等采样参数与随机种子。）
- 如果你要给一个 Web 前端提供聊天接口，选哪种？如果你要在训练循环里做大规模采样，选哪种？

> 说明：上述实践依赖真实 GPU 与可下载的模型权重，若本地不具备条件，请标注「待本地验证」，并把重点放在「读懂两条入口的源码差异」上。

---

## 6. 本讲小结

- SGLang 的「引擎」是同一套多进程（TokenizerManager 在主进程 + Scheduler / DetokenizerManager 在子进程），`sglang serve` 和 `sgl.Engine` 只是它的两种「外壳」。
- **两种入口共享同一个 `Engine._launch_subprocesses`**，HTTP 服务 = 引擎子进程 + FastAPI/uvicorn 外壳；进程内 Engine = 只有引擎子进程、没有外壳。
- `launch_server.run_server` 是模式分发器，默认（不带特殊标志）走 `http_server.launch_server`，此外还有 Ray / gRPC / encode 分离等形态。
- `sgl.Engine` 是普通 Python 类，`__init__` 的参数与 `ServerArgs` 一一对应；`generate` 是「同步包装异步内核」，`async_generate` 是原生异步。
- `EngineBase` 是两种入口共享的抽象基类，定义了 `generate` / `flush_cache` / `update_weights_from_tensor` / `shutdown` 等统一接口，这正是两边 API 同构的根源。
- 所有 `Engine` 示例都需要 `if __name__ == "__main__":` 守卫，否则 spawn 子进程会无限递归。

---

## 7. 下一步学习建议

本讲建立了「两种入口共享同一套引擎子进程」的心智模型，但**引擎内部这些进程之间到底怎么通信、一条请求怎么流转**还没有展开。建议下一步学习：

- **u2-l1 多进程架构与 ZMQ IPC 消息协议**：深入 `io_struct.py`、`TokenizerManager`、`DetokenizerManager`，看懂进程间用 ZMQ + msgspec 传递的消息结构体。
- **u2-l2 启动流程与 ServerArgs 配置**：系统过一遍 `ServerArgs` 的上百个字段，理解 `--tp` / `--dp` / `--chunked-prefill-size` 等参数如何同时作用于 `sglang serve` 和 `sgl.Engine`（因为两者共用 `ServerArgs`）。
- **u2-l3 请求端到端流转**：把本讲提到的 `tokenizer_manager.generate_request(...)` 这一句展开成完整的「HTTP/Engine → TokenizerManager → Scheduler → DetokenizerManager → 回流」链路。

如果你更关心离线/RL 用法，可以先跳到 **u12-l3 RL 后端与离线批量推理**，但建议至少先读完 u2 的进程模型，否则 `update_weights`、`flush_cache` 等接口的作用对象会不清晰。
