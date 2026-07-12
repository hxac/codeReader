# REST 服务器与 OpenAI 端点

## 1. 本讲目标

本讲把上一讲（u11-l1）建立的那座「Python↔C++ JSON FFI 桥」推向它的最终形态——一个**对外暴露 HTTP 接口的 REST 服务器**，并回答三个问题：

1. 当你在终端敲下 `mlc_llm serve ...` 之后，到 uvicorn 开始监听端口之间，到底发生了什么？一个 `AsyncMLCEngine` 是怎样被「组装」进一个 FastAPI 应用的？
2. 这个服务器对外提供哪些端点（endpoint）？它们如何与 OpenAI 官方 API 一一对应，流式（streaming）与非流式响应分别走哪条代码路径？
3. 一条 HTTP 请求进来，要穿过哪些「关卡」（CORS、API key 校验、异常处理、依赖注入）才能抵达引擎？

学完后你应该能够：

- 画出 `cli/serve.py` → `interface/serve.py` → `openai_entrypoints.py` 的调用链；
- 说清 `ServerContext` 这个全局单例在「多模型注册中心」与「端点取引擎」之间扮演的桥梁角色；
- 独立定位并解释处理 chat completion 流式响应的 async generator；
- 理解 FastAPI 的「依赖注入（Depends）」如何被用来做 API key 鉴权，以及 SSE（Server-Sent Events）格式如何承载流式 token。

## 2. 前置知识

本讲建立在两讲之上，请先确认你已经掌握：

- **u11-l1（MLCEngine 与 JSON FFI 桥接）**：你已经知道 `AsyncMLCEngine` 是 Python 侧的类型化封装，背后最终创建的是同一个 C++ `ThreadedEngine`，并由 `RunBackgroundLoop` / `RunBackgroundStreamBackLoop` 两个后台循环驱动；`_handle_chat_completion` 是一个 async generator，逐步 yield OpenAI 风格的 delta。本讲正是把这个 async generator 包成 HTTP 流。
- **u6-l3（OpenAI 兼容协议与生成配置）**：你已经知道请求/响应用 Pydantic 模型（`ChatCompletionRequest` / `ChatCompletionResponse` / `ChatCompletionStreamResponse`）描述，`GenerationConfig` 是引擎内部的精简采样配置。本讲里这些模型就是 FastAPI 路由函数的入参和返回值。

此外需要一点 **FastAPI / Starlette** 的常识（你不必精通，本讲会边讲边解释）：

- **路由（route）**：用 `@app.post("/v1/chat/completions")` 这样的装饰器把一个函数绑定到一条 HTTP 路径。
- **依赖注入（Dependency Injection, `Depends`）**：FastAPI 允许你声明「处理请求前要先运行的函数」，常用于鉴权、读公共状态。本讲的 API key 校验就是用它实现的。
- **ASGI 服务器（uvicorn）**：FastAPI 本身只定义 app，需要一个 ASGI 服务器去真正监听 socket、收发字节。`uvicorn.run(app, host, port)` 就是这一步。
- **SSE（Server-Sent Events）**：一种基于 HTTP 的单向流式协议，每条消息形如 `data: <内容>\n\n`，浏览器/客户端逐条读取。OpenAI 的流式响应用的就是它。

> 一句话定位：本讲是「协议层（u6-l3）」与「引擎层（u11-l1）」之间的 **HTTP 适配层**——它不发明任何推理逻辑，只负责把 HTTP 请求翻译成引擎调用，再把引擎的流式输出翻译回 HTTP 响应。

## 3. 本讲源码地图

本讲涉及的关键文件，按「从外到内」的请求流向排列：

| 文件 | 角色 | 本讲用途 |
| --- | --- | --- |
| [python/mlc_llm/cli/serve.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/serve.py) | CLI 入口层 | 解析命令行参数（含 `EngineConfigOverride`），转发给接口层 |
| [python/mlc_llm/interface/serve.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/serve.py) | 接口层 / 启动编排 | 唯一的 `serve()` 函数：建引擎、装 FastAPI app、跑 uvicorn |
| [python/mlc_llm/serve/server/server_context.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/server/server_context.py) | 全局单例 / 多模型注册中心 | 端点函数靠它取到「要调用哪个引擎」 |
| [python/mlc_llm/serve/entrypoints/openai_entrypoints.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py) | OpenAI 兼容端点 | 四个 `/v1/*` 路由 + API key 鉴权 + 流式/非流式分支 |
| [python/mlc_llm/protocol/error_protocol.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/error_protocol.py) | 错误协议 | `BadRequestError` 异常与统一错误响应 |
| [python/mlc_llm/serve/engine.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine.py) | AsyncMLCEngine | `_handle_chat_completion` / `_handle_completion` 这两个被端点消费的 async generator |

记忆口诀：**CLI 解析参数 → `serve()` 组装 app → 端点处理请求 → `ServerContext` 取引擎 → async generator 产 token**。本讲第 4 节就按这条线展开。

## 4. 核心概念与源码讲解

### 4.1 serve() 启动编排：把引擎封装成 FastAPI 应用

#### 4.1.1 概念说明

「启动编排（orchestration）」指的是：把一堆散落的零件——一个推理引擎、若干路由、若干中间件、一个 ASGI 服务器——按正确顺序拼装成一个能对外服务的整体。这件事在 MLC LLM 里集中发生在一个函数里：`interface/serve.py` 的 `serve()`。

理解这一步的关键是认清三个「角色」的分工：

- **`AsyncMLCEngine`**：真正干活的推理引擎（u11-l1 已讲），是「内容生产者」。
- **FastAPI `app`**：一个 HTTP 路由表，是「门面」，它本身不懂推理。
- **`uvicorn`**：ASGI 服务器，是「门卫」，负责收发 socket 字节、驱动事件循环。

`serve()` 的工作就是把这三者接线：先造引擎，再造 app，把引擎登记进一个全局上下文，把路由挂到 app 上，最后把 app 交给 uvicorn。

#### 4.1.2 核心流程

`serve()` 的执行可以分成六个阶段：

1. **构造推理引擎**：用传入的全部参数构造 `AsyncMLCEngine`，连同一份完整的 `EngineConfig`。
2. **（可选）构造 embedding 引擎**：若指定了 `--embedding-model`，再造一个 `AsyncEmbeddingEngine`（供 `/v1/embeddings` 用）。
3. **进入 `ServerContext` 上下文**：把引擎登记进全局单例，并设置 `api_key`。
4. **组装 FastAPI app**：`fastapi.FastAPI()` → 加 `CORSMiddleware` → `include_router` 挂载各端点路由。
5. **注册异常处理器**：把 `BadRequestError` 映射到统一错误响应。
6. **启动 uvicorn**：`uvicorn.run(app, host=host, port=port)`，阻塞主线程开始服务。

用伪代码表示：

```
async_engine = AsyncMLCEngine(model, device, ..., EngineConfig(...))
emb_engine   = AsyncEmbeddingEngine(...)           # 可选
with ServerContext() as ctx:                       # 登记全局单例
    ctx.add_model(model, async_engine)
    ctx.add_embedding_engine(embedding_model, emb_engine)   # 可选
    ctx.api_key = api_key

    app = FastAPI()
    app.add_middleware(CORSMiddleware, ...)         # 跨域
    app.include_router(openai_entrypoints.app)      # /v1/*
    app.include_router(metrics_entrypoints.app)     # /metrics
    app.include_router(microserving_entrypoints.app) # /microserving/*
    if enable_debug:
        app.include_router(debug_entrypoints.app)   # 调试端点
    app.exception_handler(BadRequestError)(bad_request_error_handler)  # 异常映射
    uvicorn.run(app, host, port)                    # 阻塞，开始监听
```

注意第 6 步是**阻塞**的——`uvicorn.run` 不返回，主线程进入事件循环；只有当服务器被关闭（Ctrl-C）时，`with ServerContext()` 的 `__exit__` 才会执行，从而终止所有引擎。

#### 4.1.3 源码精读

**入口函数签名**——`serve()` 接收近 30 个参数，几乎一一对应 `mlc_llm serve` 的命令行选项：

这段定义了 serve 的全部入参，包括模型路径、设备、模式、引擎配置（并发/上下文长度/推测解码/前缀缓存等）、以及服务器参数（host/port/CORS/api_key）。
[python/mlc_llm/interface/serve.py:24-58](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/serve.py#L24-L58)

**阶段 1：构造 AsyncMLCEngine**。注意它把所有「引擎配置」打包成一个 `EngineConfig` 对象传入，而不是散着传——这正是 u11-l1 提到的「类型化对象桥」的入口：

这里创建主推理引擎，并把 `max_num_sequence`、`gpu_memory_utilization`、`speculative_mode` 等运行期参数收敛进 `engine.EngineConfig`。
[python/mlc_llm/interface/serve.py:61-87](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/serve.py#L61-L87)

**阶段 2：可选的 embedding 引擎**。`--embedding-model` 与 `--embedding-model-lib` 必须成对出现，否则报错；这是 `/v1/embeddings` 端点的数据来源：

指定 embedding 模型时，校验 lib 必填，然后构造 `AsyncEmbeddingEngine`。
[python/mlc_llm/interface/serve.py:90-101](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/serve.py#L90-L101)

**阶段 3+4+5+6：组装 app 并启动**。这一段是本讲的核心代码，值得逐行读：

进入 `ServerContext` 上下文，登记引擎与 api_key；创建 FastAPI app；加 CORS 中间件；挂载三组路由（openai/metrics/microserving）；按需挂 debug 路由；注册 `BadRequestError` 异常处理器；最后交给 uvicorn 阻塞运行。
[python/mlc_llm/interface/serve.py:103-131](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/serve.py#L103-L131)

几个要点：

- `with ServerContext() as server_context:` 不是装饰性的——它的 `__enter__` 会把自身登记成全局单例（见 4.2），所有端点函数都靠这个单例找到引擎。
- `app.include_router(...)` 挂载的是**预先定义好的 `APIRouter` 对象**（每个 entrypoints 文件里都有一个模块级 `app = fastapi.APIRouter(...)`），而不是单独的路由。这样端点可以分散在多个文件里维护。
- `enable_debug` 是个**双开关**：它既挂载额外的 debug 路由，又（通过 `ServerContext.enable_debug`，见 4.2）放行请求里的 `debug_config` 字段（u6-l3 提过这个后门）。
- `app.exception_handler(...)(handler)` 的写法是 FastAPI 的装饰器式注册——把某个异常类型绑定到一个处理函数，下文 4.4 会展开。

**CLI 层如何调用它**：`cli/serve.py` 的 `main()` 解析完参数后，把 `EngineConfigOverride`（一个 dataclass，用 `;` 分隔的字符串解析，类似 chat CLI 的 `/set`）展开成关键字参数传给 `serve()`：

`main(argv)` 是命令行入口，解析参数后调用接口层 `serve(...)`。
[python/mlc_llm/cli/serve.py:106-264](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/serve.py#L106-L264)

注意一个**命名映射细节**：CLI 里叫 `context_window_size`（用户友好），传给 `serve()` 时变成 `max_single_sequence_length``(...)`（引擎内部语义）。这种「CLI 名 ≠ 引擎名」的转换就发生在 CLI 层，接口层只认引擎术语。

#### 4.1.4 代码实践（源码阅读型）

**目标**：理清「CLI 参数 → `serve()` 参数」的对应关系，特别是引擎配置项的命名转换。

**步骤**：

1. 打开 [python/mlc_llm/cli/serve.py:230-264](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/serve.py#L230-L264)，找到 `serve(...)` 的调用。
2. 对照 [python/mlc_llm/interface/serve.py:24-58](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/serve.py#L24-L58) 的 `serve()` 形参，逐一标注每个实参来源。
3. 重点找：哪个 CLI 字段被映射成了 `max_single_sequence_length`？哪些字段来自 `parsed.overrides`（即 `EngineConfigOverride`），哪些来自 `parsed` 顶层？

**预期结果**：你会发现 `max_single_sequence_length=parsed.overrides.context_window_size` 是唯一的「改名」字段，其余基本同名透传；`speculative_mode`、`prefix_cache_mode`、`prefill_mode` 来自 `parsed` 顶层（因为它们有独立的命令行选项），而并发/显存类参数全部来自 `parsed.overrides`。

#### 4.1.5 小练习与答案

**练习 1**：如果用户没有传 `--host`，服务器监听在哪个地址？为什么这个默认值对「公网部署」是不安全的？

**答案**：默认 `host="127.0.0.1"`（见 [cli/serve.py:182-187](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/serve.py#L182-L187)），即只监听本机回环，外部机器连不上。这是安全默认——要公网部署必须显式 `--host 0.0.0.0`，此时务必同时配置 `--api-key`（见 4.4）。

**练习 2**：`serve()` 函数里，为什么 `uvicorn.run(...)` 之后没有任何代码？

**答案**：因为 `uvicorn.run` 是阻塞调用，它会接管事件循环直到服务器关闭。服务器关闭时控制权才回到 `with ServerContext()` 块的末尾，触发 `__exit__` 终止引擎。所以 `uvicorn.run` 之后写代码没有意义（正常路径下不会执行到）。

---

### 4.2 ServerContext：全局单例与多模型注册中心

#### 4.2.1 概念说明

`serve()` 把引擎造好了，但**端点函数和 `serve()` 并不在同一个作用域**——端点函数定义在 `openai_entrypoints.py` 里，它怎么拿到 `serve()` 里创建的那个 `AsyncMLCEngine`？

MLC LLM 的答案是 **`ServerContext`**：一个进程级**全局单例**，扮演「多模型注册中心」。`serve()` 在启动时把所有引擎登记进去，端点函数在处理请求时通过 `ServerContext.current()` 取出当前唯一的实例，再按模型名查到对应引擎。

这其实是一种**服务定位器模式（Service Locator）**：用一个全局可访问的对象来持有共享服务，避免把引擎对象沿着调用链一路传参（FastAPI 的路由函数签名也无法随意增加隐藏参数）。

> 为什么不用 FastAPI 自带的依赖注入来传引擎？因为引擎对象是**运行时才创建**的（`serve()` 里），而 FastAPI 的依赖通常在模块导入时就声明。用 `ServerContext` 这个单例做中转更简单直接。

#### 4.2.2 核心流程

`ServerContext` 的生命周期与关键操作：

1. **创建**：`serve()` 调 `with ServerContext() as server_context:`。
2. **登记单例**：`__enter__` 把自己赋给类变量 `ServerContext.server_context`，并禁止重复创建。
3. **登记引擎**：`add_model(model, engine)` 存入 `_models` 字典；embedding 引擎存入 `_embedding_engines`。
4. **服务期间**：端点函数调 `ServerContext.current()` 拿到单例，再 `get_engine(model)` / `get_embedding_engine(model)` 取引擎。
5. **销毁**：`__exit__` 遍历所有引擎调 `terminate()`，清空字典，把类变量重置为 `None`。

取引擎有一条**「单引擎快捷路径」**：如果只 serve 了一个模型，`get_engine(None)` 也能返回它（不必指定模型名）；一旦有多个模型，就必须精确按名查找，找不到返回 `None`。

#### 4.2.3 源码精读

**类定义与全局单例字段**。注意 `server_context` 和 `enable_debug` 都是**类变量**（不属于某个实例），这正是「全局单例」的载体：

`ServerContext` 用类变量 `server_context` 持有唯一实例，`enable_debug` 也是类级开关。
[python/mlc_llm/serve/server/server_context.py:11-22](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/server/server_context.py#L11-L22)

**`__enter__` / `__exit__`：单例的设置与清理，以及引擎的终止**。这是整个生命周期的边界：

`__enter__` 禁止重复创建并登记单例；`__exit__` 终止全部 chat/embedding 引擎、清空字典、复位单例——这就是「服务器关闭时优雅回收引擎」的落点。
[python/mlc_llm/serve/server/server_context.py:24-37](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/server/server_context.py#L24-L37)

**`current()`：端点函数的统一入口**。所有端点函数处理请求的第一行几乎都是 `ServerContext.current()`：

静态方法 `current()` 直接返回类变量里那个唯一实例。
[python/mlc_llm/serve/server/server_context.py:39-42](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/server/server_context.py#L39-L42)

**取引擎的「单引擎快捷路径」**。这段逻辑决定了 `/v1/models` 等端点在多模型场景下的行为：

`get_engine` 在只有一个模型时直接返回它（即便请求里 model 字段对不上），否则按名精确查找；找不到返回 `None`，由端点转成「模型未 served」错误。
[python/mlc_llm/serve/server/server_context.py:50-55](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/server/server_context.py#L50-L55)

**`get_model_list`：合并 chat 模型与 embedding 模型**。`/v1/models` 端点用它列出全部可用模型：

返回 chat 引擎键与 embedding 引擎键的合并列表。
[python/mlc_llm/serve/server/server_context.py:57-59](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/server/server_context.py#L57-L59)

#### 4.2.4 代码实践（源码阅读型）

**目标**：验证「单引擎快捷路径」带来的一个边界行为。

**步骤**：

1. 读 [server_context.py:50-55](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/server/server_context.py#L50-L55) 的 `get_engine`。
2. 假设你只 serve 了一个模型 `Llama-3-8B-Instruct-MLC`，然后发一个请求 `"model": "gpt-4"`。问：端点会返回「模型未 served」错误吗？

**预期结果**：**不会**。因为 `len(self._models) == 1` 成立，`get_engine("gpt-4")` 走 `next(iter(...))` 直接返回那唯一的引擎，根本不看 `"gpt-4"` 这个名字。只有当 served 模型 ≥ 2 时，名字才会被严格匹配。这是一个值得注意的「宽容」行为——单模型部署下 `model` 字段几乎被忽略。

#### 4.2.5 小练习与答案

**练习 1**：`ServerContext` 用类变量实现单例，这种方式在**多进程**（比如 `uvicorn --workers 4`）下还成立吗？

**答案**：类变量单例只在**单进程**内有效。每个 worker 进程都有自己的 `ServerContext.server_context`，互不可见。MLC LLM 的 `serve()` 默认单进程单 uvicorn，不开启多 worker，所以这个简化设计够用；要做多进程需要在进程外（如 Redis）做协调，MLC LLM 当前未走这条路。

**练习 2**：为什么 `__exit__` 里要显式遍历调用 `terminate()`，而不是直接让引擎对象被垃圾回收？

**答案**：`AsyncMLCEngine` 背后持有 C++ `ThreadedEngine` 与后台线程、显存等原生资源，这些**不会被 Python GC 自动释放**。必须显式 `terminate()` 通知 C++ 侧停止后台循环、释放显存，否则会出现线程泄漏 / 显存占用。

---

### 4.3 OpenAI 端点路由：四个兼容端点

#### 4.3.1 概念说明

`openai_entrypoints.py` 是本讲的「主舞台」。它用一个 `fastapi.APIRouter` 定义了四个与 OpenAI 官方 API 对齐的端点：

| 方法 | 路径 | 处理函数 | 作用 |
| --- | --- | --- | --- |
| POST | `/v1/embeddings` | `request_embedding` | 文本→向量（需 embedding 模型） |
| GET | `/v1/models` | `request_models` | 列出所有已 served 模型 |
| POST | `/v1/completions` | `request_completion` | 纯文本补全（非对话） |
| POST | `/v1/chat/completions` | `request_chat_completion` | 对话补全（最常用） |

每个端点函数都是 `async def`，入参是 Pydantic 请求模型（u6-l3 讲过），返回值是 Pydantic 响应模型或 FastAPI 的 `StreamingResponse`。它们共享同一个套路：**取上下文 → 取引擎 → 调引擎的 async generator → 拼响应**。

#### 4.3.2 核心流程

以最复杂的 `/v1/chat/completions` 为例，端点函数的处理流程：

```
1. server_context = ServerContext.current()           # 取全局上下文
2. 按 enable_debug 决定是否清空 request.debug_config   # 安全：默认丢弃后门字段
3. async_engine = server_context.get_engine(request.model)
   └─ 若为 None → 返回 400「模型未 served」
4. 生成 request_id（用 request.user 或 chatcmpl-{uuid}）  # 推测解码/分离式需要稳定 id
5. 分流：
   ├─ request.stream == True  → 流式分支（见 4.4 的 SSE 包装）
   └─ request.stream == False → 非流式分支：
       async for response in async_engine._handle_chat_completion(...):
           ├─ 检查 raw_request.is_disconnected() → 客户端断开则中止
           ├─ response.usage 非空 → 这是最后一块（统计信息）
           └─ 累积 delta.content 到 output_texts、记录 finish_reason、logprobs
       process_function_call_output(...)   # 函数调用解析
       wrap_chat_completion_response(...)  # 拼成最终 ChatCompletionResponse
```

两个细节值得记住：

- **`request_id` 的来源**：优先用请求自带的 `request.user`，否则生成 `chatcmpl-{uuid}`。注释 `# FIXME` 解释了原因——分离式推理（disaggregation，见 u12-l3）里 `prep_recv`/`remote_send`/`start_generation` 三个步骤必须处理**同一个**请求，所以 id 必须跨步骤稳定，不能用引擎内部自动生成的 id。
- **断连检测只在非流式分支做**：流式分支里 `StreamingResponse` 一旦停止迭代就会自动触发引擎 abort；非流式分支里引擎不会被动通知断连，所以要主动 `is_disconnected()` 轮询。

#### 4.3.3 源码精读

**路由器的创建与 API key 依赖**。这一行是整个文件的「总开关」——所有路由都挂在这个 `APIRouter` 上，并且通过 `dependencies=[Depends(verify_api_key)]` 统一加上了鉴权（4.4 详述）：

`app` 是一个 `APIRouter`，构造时注入 `verify_api_key` 依赖，使得该路由下**所有**端点在处理前都先过 API key 校验。
[python/mlc_llm/serve/entrypoints/openai_entrypoints.py:39](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L39)

**`/v1/models`：最简单的端点**。它不调引擎，只列出已登记模型，是验证「服务器活着」的最佳探针：

`GET /v1/models` 返回所有 chat + embedding 模型的 id 列表。
[python/mlc_llm/serve/entrypoints/openai_entrypoints.py:120-126](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L120-L126)

**`/v1/embeddings`：embedding 端点**。它走的是 `AsyncEmbeddingEngine`（不是 chat 引擎），并支持三种输入归一化（字符串、字符串列表、token id 列表）与可选的 Matryoshka 维度截断：

`POST /v1/embeddings` 归一化输入、异步求向量、可选降维+重归一化、可选 base64 编码，返回 `EmbeddingResponse`。
[python/mlc_llm/serve/entrypoints/openai_entrypoints.py:45-114](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L45-L114)

注意它和 chat 端点的取引擎路径不同：`get_embedding_engine(request.model)`，找不到时返回「未作为 embedding 模型 served」的错误（措辞与 chat 端点不同，便于排障）。

**`/v1/chat/completions` 的非流式分支**。这是「累积 delta 成完整响应」的核心循环：

非流式分支：异步迭代引擎的 `_handle_chat_completion`，逐块累积 `output_texts`/`finish_reasons`/`logprobs`，并轮询 `is_disconnected()`；最后解析函数调用并打包成完整响应。
[python/mlc_llm/serve/entrypoints/openai_entrypoints.py:288-340](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L288-L340)

几个要点：

- `response.usage is not None` 用来识别**最后一块**——usage（token 计数）只在流的末尾出现一次。
- `assert all(finish_reason is not None ...)` 是一道安全断言：正常结束时每条分支都应有 finish_reason（stop/length/tool_calls 等），没有说明流程异常。
- `process_function_call_output` 把模型输出里可能的函数调用文本还原成结构化 `tool_calls`（u6-l3 讲过反向过程）。

**`/v1/completions` 与 chat 端点结构几乎一致**，区别只在它处理纯文本 prompt、用 `_handle_completion`、累积 `choice.text`（而非 `delta.content`）。两者共享同一套流式/非流式骨架，对照阅读即可：

`POST /v1/completions` 的非流式分支，结构与 chat 端点对称，累积 `choice.text`。
[python/mlc_llm/serve/entrypoints/openai_entrypoints.py:182-230](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L182-L230)

**被消费的 async generator**（在 engine.py 里）。端点函数 `async for response in async_engine._handle_chat_completion(...)` 消费的就是这个生成器——它内部又 `async for delta_outputs in self._generate(...)`，而 `_generate` 最终驱动 C++ ThreadedEngine（u11-l1 讲过的双后台循环）：

`_handle_chat_completion` 是 async generator，逐块 yield `ChatCompletionStreamResponse`；异常会冒泡给端点（从而触发 BadRequestError 处理）。
[python/mlc_llm/serve/engine.py:1171-1235](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine.py#L1171-L1235)

#### 4.3.4 代码实践（源码阅读型）

**目标**：定位「处理 chat completion 流式响应的 async generator」，并解释它和端点里那个 async generator 的区别。

**步骤**：

1. 在 [openai_entrypoints.py:236-340](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L236-L340) 里找到 `request_chat_completion` 的流式分支，定位内部那个 `async def completion_stream_generator()`。
2. 在 [engine.py:1171-1235](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine.py#L1171-L1235) 里找到 `_handle_chat_completion`。
3. 回答：这两个 async generator 各自 yield 什么类型？谁消费谁？

**预期结果**：

- **引擎层** `_handle_chat_completion` yield 的是 `ChatCompletionStreamResponse`（Pydantic 对象，OpenAI 语义），它消费 `_generate` 产出的原始 delta。
- **端点层** `completion_stream_generator` yield 的是 `str`（SSE 文本 `data: {...}\n\n`），它消费引擎层的 `ChatCompletionStreamResponse`，把它们序列化成 HTTP 可发送的字节流。
- 关系是**两层嵌套的 async generator**：端点层把引擎层的「对象流」翻译成「SSE 文本流」。

#### 4.3.5 小练习与答案

**练习 1**：`/v1/chat/completions` 端点里，`request.debug_config` 在什么情况下会被设为 `None`？为什么？

**答案**：当 `server_context.enable_debug` 为 `False`（即启动时未加 `--enable-debug`）时，端点会把 `request.debug_config = None`（见 [openai_entrypoints.py:246-247](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L246-L247)）。这是安全措施——`debug_config` 是 MLC 扩展的后门字段（`ignore_eos`、`special_request` 等，u6-l3 讲过），默认对客户端关闭，避免恶意请求通过它操纵引擎内部行为。

**练习 2**：非流式分支里，为什么 `response.usage` 那一块要用 `continue` 跳过，而不是像其它块一样累加？

**答案**：因为 `usage`（prompt_tokens/completion_tokens 统计）不是「文本内容」，而是整次请求的汇总元数据，只在最后一块出现一次。它不能拼进 `output_texts`，而是单独保存到 `request_final_usage`，最终放进响应的 `usage` 字段。用 `continue` 跳过下面的 choice 累加逻辑，避免误把统计块当文本块处理。

---

### 4.4 流式响应深度剖析：SSE 与两段式 async generator

#### 4.4.1 概念说明

流式响应（streaming）是 LLM 服务的「门面担当」——用户希望看到 token 一个一个蹦出来，而不是等几秒钟后一次性返回。OpenAI 用 **SSE（Server-Sent Events）** 协议承载流式：服务器持续发送形如

```
data: {"choices":[{"delta":{"content":"你好"}}]}\n\n
data: {"choices":[{"delta":{"content":"世界"}}]}\n\n
data: [DONE]\n\n
```

的消息，客户端逐行解析。终止标记是固定的 `data: [DONE]\n\n`。

MLC LLM 的流式实现有一个**精妙的「两段式」设计**：先在端点函数作用域里手动取出 generator 的**第一块**，再用一个独立的 async generator 把「第一块 + 剩余块」重新包装成 SSE 文本流。这个看似绕弯的写法是为了解决一个真实的工程问题——**异常的作用域归属**。

#### 4.4.2 核心流程

`/v1/chat/completions` 流式分支的执行流程：

```
stream_generator = async_engine._handle_chat_completion(request, ...)   # 还没开始跑
first_response = await anext(stream_generator)                          # 手动拉第一块（在端点作用域）

async def completion_stream_generator():                                # 定义 SSE 包装器
    if first_response 是 StopAsyncIteration:                            # 引擎一块都没产出
        yield "data: [DONE]\n\n"
        return
    yield f"data: {first_response.model_dump_json(...)}\n\n"            # 先吐第一块
    async for response in stream_generator:                             # 再异步迭代剩余块
        yield f"data: {response.model_dump_json(...)}\n\n"
    yield "data: [DONE]\n\n"                                            # 终止标记

return StreamingResponse(completion_stream_generator(),                 # 交给 FastAPI 流式回传
                        media_type="text/event-stream")
```

**为什么手动拉第一块？** 关键在注释 `# We manually get the first response from generator to capture potential exceptions in this scope, rather then the StreamingResponse scope.`

如果引擎在生成第一块时就抛异常（比如请求非法触发 `BadRequestError`），那么：

- **不手动拉**：异常会发生在 `StreamingResponse` 内部，此时 HTTP 响应头已经以 200 发出，无法再改成 400 错误响应，客户端拿到的是一个「开了头却突然断掉」的坏流。
- **手动拉第一块**：异常发生在**端点函数作用域**，此时 HTTP 响应还没开始发送，FastAPI 的异常处理器（4.5 讲）可以正常介入，把它转成结构化的 400 错误响应。

这是一个用**很小的代价**（多写一个嵌套 generator）换取**正确错误语义**的经典技巧。

#### 4.4.3 源码精读

**流式分支主体（chat completion）**。这段是 4.4 的核心代码，逐行体会「手动拉第一块 + 重新包装」的两段式：

流式分支：先 `await anext(stream_generator)` 把第一块拉到端点作用域（捕获早期异常），再用 `completion_stream_generator` 把对象流包装成 `data: ...\n\n` 的 SSE 文本流，最后返回 `StreamingResponse`。
[python/mlc_llm/serve/entrypoints/openai_entrypoints.py:262-286](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L262-L286)

几个细节：

- `first_response = await anext(stream_generator)`：`anext` 是 Python 3.10+ 的内置，取 async generator 的下一个值；此时 `_handle_chat_completion` 才真正开始执行（generator 是惰性的，被 `anext` 触发才跑）。
- `if isinstance(first_response, StopAsyncIteration)`：虽然 `anext` 正常情况下遇到空流会直接抛 `StopAsyncIteration` 而不是返回它，但这段判断是对边界情况的防御性处理——若第一「块」本身就是停止信号，直接发 `[DONE]`。
- `model_dump_json(by_alias=True)`：把 Pydantic 模型序列化成 JSON 字符串，`by_alias=True` 保证字段名用 OpenAI 约定的别名（u6-l3 提过的落盘/序列化契约）。
- `media_type="text/event-stream"`：这是 SSE 的标准 MIME 类型，告诉客户端「请按 SSE 解析」。

**`/v1/completions` 的流式分支完全同构**，对照阅读可加深理解：

`/v1/completions` 流式分支与 chat 端点逐行对称，同样的「手动拉第一块 + SSE 包装」两段式。
[python/mlc_llm/serve/entrypoints/openai_entrypoints.py:156-180](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L156-L180)

**SSE 文本拼接的格式**。注意每条消息以 `data: ` 开头、以 `\n\n`（两个换行）结尾——这正是 SSE 规范要求的「事件分隔符」。客户端（包括 OpenAI 官方 SDK）依赖这个分隔符切分事件。`[DONE]` 是 OpenAI 约定的流结束标记，不是 JSON，所以单独发。

#### 4.4.4 代码实践（源码阅读 + 推理型）

**目标**：验证「手动拉第一块」对错误语义的影响。

**步骤**：

1. 读 [openai_entrypoints.py:262-286](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L262-L286) 与 [openai_entrypoints.py:128-130](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/serve.py#L128-L130)（异常处理器注册）。
2. 思考：假设把 `first_response = await anext(stream_generator)` 这一行删掉，直接把 `stream_generator` 交给 `StreamingResponse`，当一个非法的流式请求到来时，客户端会观察到什么？

**预期结果（推理）**：

- 删掉后，`_handle_chat_completion` 的首次执行被推迟到 `StreamingResponse` 开始迭代时；若它在第一块就抛 `BadRequestError`，此时 HTTP 状态行可能已按 200 发出。
- 客户端会看到一个 200 的响应头，随后流立即中断，拿不到结构化错误信息（也无法区分「模型故障」与「请求非法」）。
- 保留该行则异常发生在端点作用域，被 `bad_request_error_handler` 捕获，客户端收到正常的 400 + `{"message": ...}` JSON 错误。

> 是否真的会按 200 发出取决于 Starlette 的 `StreamingResponse` 实现细节（它在发送第一个字节前会先发响应头）。这一行为建议**待本地验证**：用 `httpx` 发一个非法流式请求，观察实际状态码与响应体。

#### 4.4.5 小练习与答案

**练习 1**：SSE 流里的每条消息为什么必须以 `\n\n` 结尾，而不是单个 `\n`？

**答案**：SSE 规范规定，一个事件由若干 `field: value\n` 行组成，以**一个空行**（即连续两个换行 `\n\n`）作为事件结束符。单个 `\n` 只是字段内换行，不会触发客户端派发事件。所以少写一个 `\n` 会导致客户端「收得到字节却拼不出事件」。

**练习 2**：流式分支里，`first_response` 被单独 yield 一次后，`async for response in stream_generator` 会不会重复 yield 它？

**答案**：不会。`anext` 已经消费了 generator 的第一个值，generator 的内部指针前进到了第二块。之后 `async for` 从第二块开始迭代，所以 `first_response` 只被 yield 一次，不会重复。这是 Python async generator 的标准语义——消费过的值不会重现。

---

### 4.5 中间件与依赖注入：CORS、API key 与异常处理

#### 4.5.1 概念说明

一条 HTTP 请求抵达端点函数之前，要穿过三层「关卡」，它们分别由 FastAPI/Starlette 的三种机制实现：

1. **CORS 中间件（Middleware）**：处理「跨域」。浏览器在调用不同源的 API 时会先发预检请求，CORS 中间件负责加上 `Access-Control-Allow-*` 响应头，否则浏览器会拒绝读取响应。
2. **API key 依赖（Dependency）**：鉴权。用 FastAPI 的 `Depends` 机制，在路由级别注入一个「先验证 API key」的函数。
3. **异常处理器（Exception Handler）**：把引擎抛出的 `BadRequestError` 统一翻译成 OpenAI 风格的错误 JSON 响应。

这三者体现了 FastAPI 的三种横切关注点（cross-cutting concern）处理方式：**中间件作用于所有请求（含非路由）、依赖作用于特定路由、异常处理器作用于异常路径**。

> 三者的执行顺序大致是：请求 → CORS 中间件 → API key 依赖 → 端点函数 →（异常则）异常处理器 → CORS 中间件（响应） → 客户端。

#### 4.5.2 核心流程

**CORS**：在 `serve()` 里通过 `app.add_middleware(CORSMiddleware, allow_origins=..., allow_methods=..., ...)` 注册。参数来自命令行的 `--allow-origins` 等，默认全部放开（`["*"]`）。

**API key**：定义一个普通函数 `verify_api_key(request)`，在 `APIRouter(dependencies=[Depends(verify_api_key)])` 时注入。FastAPI 会在**每个**挂在该 router 的端点执行前先调用它。函数内读 `Authorization: Bearer <key>` 头，与 `ServerContext.api_key` 比对，不符则抛 401。

**异常处理**：`BadRequestError`（一个 `ValueError` 子类）是引擎层用来表示「请求非法」的异常。它可能在请求处理过程中任何深处抛出（比如 prompt 超长）。`serve()` 用 `app.exception_handler(BadRequestError)(bad_request_error_handler)` 把它统一翻译成 `{"object":"error","message":...,"code":400}` 的 JSON 响应。

#### 4.5.3 源码精读

**CORS 中间件注册**（在 `serve()` 里）。默认 `allow_origins=["*"]` 等于「允许任意跨域」，公网部署时应收窄：

注册 CORS 中间件，参数来自 `--allow-credentials/--allow-origins/--allow-methods/--allow-headers`。
[python/mlc_llm/interface/serve.py:109-116](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/serve.py#L109-L116)

**API key 校验函数**。注意它只在「配置了 api_key 时」才校验——没配 `--api-key` 则完全跳过（开放访问）：

`verify_api_key`：仅当 `ServerContext` 配置了 `api_key` 时才校验 `Authorization: Bearer` 头，不符抛 401。
[python/mlc_llm/serve/entrypoints/openai_entrypoints.py:29-36](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L29-L36)

**依赖注入的挂载**。`APIRouter` 构造时传入 `dependencies`，等价于「这个 router 上所有路由都隐式依赖 `verify_api_key`」——不必在每个端点函数签名里重复声明：

路由器构造时注入鉴权依赖，使 `/v1/*` 全部端点共享 API key 校验。
[python/mlc_llm/serve/entrypoints/openai_entrypoints.py:39](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L39)

> 注意：`metrics_entrypoints` 与 `microserving_entrypoints` 的 `APIRouter` **没有**这个依赖（见 [metrics_entrypoints.py:8](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/metrics_entrypoints.py#L8)、[microserving_entrypoints.py:16](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/microserving_entrypoints.py#L16)），所以 `/metrics` 与 `/microserving/*` 不受 API key 保护——前者通常给 Prometheus 抓取，后者给内部编排调用。

**异常处理器注册**。`app.exception_handler(BadRequestError)(handler)` 的链式写法等价于「注册 handler 处理 BadRequestError」：

把 `BadRequestError` 异常绑定到 `bad_request_error_handler`，统一转成错误 JSON。
[python/mlc_llm/interface/serve.py:128-130](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/serve.py#L128-L130)

**异常与错误响应的定义**。`BadRequestError` 继承 `ValueError`（便于在引擎深处用 `raise` 抛出），`create_error_response` 产出标准 JSONResponse，`bad_request_error_handler` 是连接二者的处理函数：

`BadRequestError` 异常类、`ErrorResponse` 模型、`create_error_response` 工厂、`bad_request_error_handler` 处理器——构成统一的错误响应链路。
[python/mlc_llm/protocol/error_protocol.py:10-35](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/error_protocol.py#L10-L35)

注意 `bad_request_error_handler` 是 `async` 函数且签名是 `(request, exception)`——这是 FastAPI 异常处理器的固定签名；它把任意 `BadRequestError` 都映射成 400 状态码，消息取自异常参数 `e.args[0]`。

#### 4.5.4 代码实践（源码阅读型）

**目标**：追踪一个「请求非法」场景下，异常如何从引擎深处冒泡到 HTTP 错误响应。

**步骤**：

1. 假设你发了一个 `max_tokens` 为负数的 chat 请求。这类校验通常发生在引擎层（u6-l3 的协议校验可能放过，引擎内部的 `GenerationConfig` 校验抛 `BadRequestError`）。
2. 在 [engine.py:1233-1235](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine.py#L1233-L1235) 看 `_handle_chat_completion` 的 `except Exception as err: ... raise`——它会把异常重新抛给端点。
3. 在流式分支，这个 `raise` 发生在 `await anext(stream_generator)` 处（端点作用域，4.4 讲过为何要在这里）。
4. FastAPI 捕获 `BadRequestError`，调 [bad_request_error_handler](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/error_protocol.py#L33-L35)，返回 400 JSON。

**预期结果**：客户端收到 HTTP 400，响应体形如 `{"object":"error","message":"...","code":400}`。整条链路是 `引擎 raise → 端点不捕获 → FastAPI 异常处理器 → create_error_response → JSONResponse`。

> 具体哪些字段会在哪一层抛 `BadRequestError`，可 grep `raise BadRequestError` 自行核对，部分行为可能**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `verify_api_key` 用 `Depends` 注入，而不是写成一个中间件？

**答案**：因为 API key 只想保护 `/v1/*`（OpenAI 兼容端点），而 `/metrics`、`/microserving/*` 不需要保护。用 `Depends` 在 `APIRouter` 级别注入，可以**精确控制作用范围**（只对挂在该 router 的路由生效）；而中间件作用于全局所有请求，无法按路由区分。这正体现了「依赖比中间件粒度更细」。

**练习 2**：`BadRequestError` 为什么继承 `ValueError` 而不是 `Exception`？

**答案**：有两个好处。其一，`ValueError` 在 Python 语义上就表示「输入值非法」，与「请求参数不合法」的意图吻合，可读性好。其二，引擎代码深处（比如协议处理、配置校验）往往已经用 `ValueError` 表达同类错误，继承它可以让既有的 `raise ValueError(...)` 与新增的 `BadRequestError` 共享同一个异常处理路径，减少改动。同时它仍是 `Exception` 子类，FastAPI 的异常处理器能正常捕获。

## 5. 综合实践

把本讲四个模块串起来，完成一次「启动服务器 → 多端点验证 → 流式定位」的端到端任务。

### 实践目标

启动一个真实的 MLC LLM REST 服务器，用 `curl` 同时验证 `/v1/models`、`/v1/chat/completions`、`/v1/completions` 三个端点，并在源码里定位处理 chat completion 流式响应的 async generator，解释它的两段式结构。

### 操作步骤

1. **准备一个已编译的 MLC 模型**（参考 u1-l3 / u2-l2）。若没有现成模型库，可从 `HF://` 拉一个预量化的小模型（如 `Llama-3.2-1B-Instruct-q4f16_1-MLC`），首次运行会触发 JIT 编译（u1-l4 讲过的兜底机制）。

2. **启动服务器**（在单独终端）：

   ```bash
   mlc_llm serve ./<your-model-dir> --model-lib ./<your-model-lib>.so \
       --device auto --host 127.0.0.1 --port 8000
   ```

   观察日志，确认 uvicorn 打印出 `Uvicorn running on http://127.0.0.1:8000`——这对应 [interface/serve.py:131](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/serve.py#L131) 的 `uvicorn.run`。

3. **测试 `/v1/models`**（验证服务器活着，对应 [openai_entrypoints.py:120-126](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L120-L126)）：

   ```bash
   curl http://127.0.0.1:8000/v1/models
   ```

4. **测试 `/v1/chat/completions`（非流式）**（对应 [openai_entrypoints.py:236-340](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L236-L340)）：

   ```bash
   curl http://127.0.0.1:8000/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"<your-model>","messages":[{"role":"user","content":"用一句话介绍你自己"}],"max_tokens":64}'
   ```

5. **测试 `/v1/chat/completions`（流式）**——加 `"stream":true`，观察 SSE 文本（`data: {...}\n\n` 与结尾 `data: [DONE]`）：

   ```bash
   curl -N http://127.0.0.1:8000/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"<your-model>","messages":[{"role":"user","content":"数到 5"}],"stream":true}'
   ```

6. **测试 `/v1/completions`**（对应 [openai_entrypoints.py:132-230](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L132-L230)）：

   ```bash
   curl http://127.0.0.1:8000/v1/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"<your-model>","prompt":"Once upon a time","max_tokens":32,"stream":false}'
   ```

7. **（可选）验证 API key 鉴权**。重启服务器加 `--api-key secret123`，然后：

   ```bash
   # 不带 key → 401
   curl -i http://127.0.0.1:8000/v1/models
   # 带正确 key → 200
   curl -i -H "Authorization: Bearer secret123" http://127.0.0.1:8000/v1/models
   ```

   对照 [verify_api_key](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L29-L36) 确认 401 来自何处。

8. **源码定位任务**：在 [openai_entrypoints.py:275-282](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L275-L282) 找到 `completion_stream_generator`，结合 [engine.py:1171-1228](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine.py#L1171-L1228) 的 `_handle_chat_completion`，画出「引擎对象流 → 端点 SSE 文本流」的两层 async generator 嵌套图。

### 需要观察的现象

- `/v1/models` 返回的 `data` 数组里应包含你 serve 的模型 id（以及 embedding 模型，若有）。
- 非流式 chat 响应应是一个完整 JSON，含 `choices[0].message.content` 与 `usage`。
- 流式响应应是多行 `data: {...}`，每行一个 token 增量，结尾是 `data: [DONE]`。
- 加了 `--api-key` 后，不带 `Authorization` 头的请求应返回 401。

### 预期结果

若一切正常，说明你已经把本讲的四层（CLI→`serve()`→端点→引擎）贯通。若没有 GPU 或模型，**步骤 1–7 的运行结果待本地验证**；但**步骤 8 的源码定位任务不依赖运行环境**，必做且可完成。

## 6. 本讲小结

- `mlc_llm serve` 的启动编排集中在 `interface/serve.py` 的单个 `serve()` 函数：造 `AsyncMLCEngine` → 进入 `ServerContext` 登记单例 → 组装 FastAPI app（CORS + 多个 router）→ 注册异常处理器 → `uvicorn.run` 阻塞服务。
- `ServerContext` 是进程级全局单例与「多模型注册中心」，端点函数靠 `ServerContext.current()` 取上下文、靠 `get_engine(model)` 取引擎；它有一条「单引擎快捷路径」——只 serve 一个模型时会忽略请求里的 `model` 字段。
- 四个 OpenAI 兼容端点（`/v1/embeddings`、`/v1/models`、`/v1/completions`、`/v1/chat/completions`）共享「取上下文 → 取引擎 → 调 async generator → 拼响应」的套路；非流式分支逐块累积 delta、轮询断连，流式分支走 SSE。
- 流式响应采用「两段式 async generator」：先在端点作用域手动 `await anext` 拉第一块以捕获早期异常，再用嵌套 generator 把对象流包装成 `data: {...}\n\n` / `data: [DONE]` 的 SSE 文本流。
- 横切关注点分三种机制承载：CORS 用**中间件**（全局）、API key 用 **`Depends` 依赖**（仅 `/v1/*` router）、`BadRequestError` 用**异常处理器**统一翻译成错误 JSON；三者作用域与粒度不同。
- 端点层是纯粹的「HTTP 适配层」：它不实现任何推理逻辑，只把 Pydantic 请求翻译成对 `AsyncMLCEngine` async generator 的调用，再把结果翻译回 OpenAI 协议——推理本身始终发生在 u11-l1 讲的那座 C++ ThreadedEngine 桥的另一端。

## 7. 下一步学习建议

本讲覆盖了「单机 REST 服务器」的完整链路。接下来可以沿三条线深入：

1. **异步引擎与后台循环管理（u11-l3）**：本讲反复出现的 `AsyncMLCEngine`、`_handle_chat_completion`、`EngineConfig` 都来自 `serve/engine_base.py` 与 `serve/sync_engine.py`。下一讲会拆解 `EngineConfig` 的校验逻辑、同步/异步引擎的封装差异，以及 `ServerContext` 在多模型生命周期管理上的更多细节，补全本讲有意略过的引擎内部机制。
2. **微服务与路由器（u12-l2）**：本讲的 `serve()` 只挂了一个主引擎。若要做多模型路由、按请求分派到不同后端，需要 `cli/router.py` 与 `examples/python/microserving/custom_router.py`——它们构建在本讲的 `ServerContext` 多模型能力之上。
3. **源码延伸阅读**：若想验证本讲的流式/异常行为，可重点读 [python/mlc_llm/serve/engine.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine.py) 里的 `_generate` 与 `process_chat_completion_stream_output`，以及 [tests/python/serve](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests) 下针对 server 的集成测试——它们是验证「协议→引擎→HTTP」全链路最直接的依据。
