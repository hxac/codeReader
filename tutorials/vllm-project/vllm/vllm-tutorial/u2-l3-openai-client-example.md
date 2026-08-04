# OpenAI 兼容客户端调用示例

## 1. 本讲目标

本讲承接 [u2-l2](u2-l2-cli-and-serve.md)，那里我们学到了 `vllm serve` 的三层结构：`serve.py` 决策、`api_server.py` 建引擎并绑定 socket、`launcher.py` 跑 uvicorn。当服务跑起来后，它对外暴露的是一组 **HTTP 接口**。本讲要回答的是：**作为调用方，我该怎么向这个服务发请求？**

学完本讲你应该能够：

- 用标准 `openai` Python SDK 连接到本地 vLLM 服务，发送一次 chat completion 请求并拿到结果。
- 理解 `base_url` 与 `api_key` 这两个客户端配置项的作用，以及为什么 vLLM 要做「OpenAI 兼容」。
- 在源码中定位到服务端真正处理 `/v1/models` 与 `/v1/chat/completions` 这两个 HTTP 路由的位置，建立起「客户端 → HTTP 路由 → 引擎」的方位感。

## 2. 前置知识

阅读本讲前，你需要知道几个基础概念：

- **HTTP 接口 / RESTful API**：服务通过网络暴露的「动作」。客户端发一个请求（指定方法 + 路径 + 数据），服务返回结果。比如 `POST /v1/chat/completions` 表示「在这个路径上提交一个生成对话补全的请求」。
- **OpenAI API 协议**：OpenAI 公司为它的模型服务定义的一套 HTTP 接口规范（路径、请求体字段、返回体格式）。它已经成为行业事实标准，几乎所有 LLM 客户端库都「会讲」这套协议。
- **OpenAI Python SDK**：官方提供的客户端库 `openai`，封装好了请求构造、鉴权、流式解析等细节。`from openai import OpenAI` 就是它的入口。
- **base_url**：客户端访问服务时拼接的「基础地址」。OpenAI 官方的 base_url 是 `https://api.openai.com/v1`；只要你把自己的服务地址填进去，SDK 就会去访问你的服务而不是 OpenAI。
- **api_key（鉴权令牌）**：通常是一串字符串，放在请求头里证明调用方身份。vLLM 默认不强制鉴权，但 SDK 强制要求传一个非空值，所以示例里用一个占位字符串 `"EMPTY"`。

> 关键直觉：vLLM 服务「长得像」OpenAI 官方服务，因此任何会调用 OpenAI 的代码，只要改一下地址（base_url），就能改而调用 vLLM——这就是「OpenAI 兼容」最大的价值。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `examples/basic/online_serving/openai_chat_completion_client.py` | 官方提供的最小客户端示例：用 `openai` SDK 向本地 vLLM 发一次 chat completion。 |
| `vllm/entrypoints/openai/api_server.py` | 在线服务的 **HTTP 入口**：组装 FastAPI 应用、把引擎客户端（EngineClient）挂进应用状态、启动 uvicorn。 |
| `vllm/entrypoints/openai/chat_completion/api_router.py` | 定义 `POST /v1/chat/completions` 路由，把请求转交给 `OpenAIServingChat` 处理。 |
| `vllm/entrypoints/openai/models/api_router.py` | 定义 `GET /v1/models` 路由，返回当前服务可用的模型列表。 |
| `vllm/entrypoints/generate/api_router.py` | 在「generate」任务下，把上面这些路由统一挂到 FastAPI 应用上。 |

> 方位提示：客户端只和「HTTP 接口」打交道；HTTP 接口由 `api_server.py` 负责建立的应用暴露；具体路径在 `chat_completion/api_router.py` 与 `models/api_router.py`。再往下才会进入推理引擎，那属于后续讲义（u3 起）的内容。

## 4. 核心概念与源码讲解

### 4.1 OpenAI 兼容接口的意义与 base_url

#### 4.1.1 概念说明

vLLM 选择「兼容 OpenAI 接口」是一个重要的工程决策，原因有三：

1. **零迁移成本**：生态里海量的应用、Agent 框架、笔记本都用 `openai` SDK 写好了调用代码。只要服务兼容，这些代码改个地址就能用。
2. **统一抽象**：vLLM 内部其实支持很多「任务」（chat / completion / embedding / 评分 等）。用一套标准协议对外，调用方无需学习 vLLM 私有协议。
3. **解耦客户端与服务端**：客户端只认 HTTP 协议，不关心服务端是 vLLM 还是别的；服务端只要把协议实现好。

实现「兼容」的核心机制就是 **base_url 指向本地**。

#### 4.1.2 核心流程

```text
客户端代码                          网络层                      vLLM 服务
openai.OpenAI(base_url=...)   ──HTTP POST──>   FastAPI 应用 (api_server.py)
  └─ chat.completions.create()                 └─ /v1/chat/completions 路由
                                                   └─ 引擎执行推理
客户端 <── HTTP JSON 响应 ─────────────────────── 返回结果
```

注意 base_url 末尾的 `/v1`：它和 OpenAI 官方保持一致。SDK 内部会把方法名拼到 base_url 后面，例如 `client.chat.completions.create()` 会请求 `{base_url}/chat/completions`，再加上前缀就是 `http://localhost:8000/v1/chat/completions`。

#### 4.1.3 源码精读

示例文件顶部就明确写明了两个关键变量：

[examples/basic/online_serving/openai_chat_completion_client.py:10-14](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/examples/basic/online_serving/openai_chat_completion_client.py#L10-L14) —— 这两行说明：导入官方 SDK，并把「API key 设为占位字符串 `EMPTY`」「API base 指向本地 `http://localhost:8000/v1`」。`v1` 后缀正是协议版本号，缺一不可。

#### 4.1.4 代码实践（源码阅读型）

1. 实践目标：理解 base_url 的拼接。
2. 操作步骤：阅读示例第 14 行的 `openai_api_base`，再阅读示例第 46-50 行的 `client.chat.completions.create(...)` 调用。
3. 需要观察的现象：注意调用中并没有再次写 `/v1` 或 `/chat/completions`。
4. 预期结果：你应能说清「SDK 根据 `base_url` + 方法名自动拼出完整 URL」。
5. 这一步无需运行，是纯阅读理解。

#### 4.1.5 小练习与答案

**练习**：如果把 `openai_api_base` 改成 `http://localhost:8000`（去掉 `/v1`），请求会发往哪里？会发生什么？

**答案**：请求会发往 `http://localhost:8000/chat/completions`（少了 `/v1` 前缀）。而 vLLM 把路由注册在 `/v1/chat/completions`，所以这个请求会命中不到路由，返回 404。`/v1` 必须保留。

---

### 4.2 客户端示例精读（openai_chat_completion_client.py）

#### 4.2.1 概念说明

官方示例脚本把一次完整的「列出模型 → 发起对话 → 打印结果」流程浓缩在 60 多行里。它同时支持普通（一次性返回）和流式（逐 token 返回）两种模式，是学习客户端调用的最佳起点。

#### 4.2.2 核心流程

1. 用 `api_key` 与 `base_url` 构造一个 `OpenAI` 客户端对象。
2. 调用 `client.models.list()` 拿到服务端可用模型列表，取第一个的 `id` 当作 `model` 参数。
3. 构造对话消息列表 `messages`（含 system / user / assistant / user 多轮）。
4. 调用 `client.chat.completions.create(messages=..., model=..., stream=...)`。
5. 根据 `stream` 决定：流式则逐条 `print`，非流式则直接 `print` 整个返回对象。

#### 4.2.3 源码精读

[examples/basic/online_serving/openai_chat_completion_client.py:16-24](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/examples/basic/online_serving/openai_chat_completion_client.py#L16-L24) —— 多轮对话的 `messages` 结构。注意第二条 user 之前有一条 assistant 消息，演示了「带历史」的多轮对话如何表达。

[examples/basic/online_serving/openai_chat_completion_client.py:36-43](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/examples/basic/online_serving/openai_chat_completion_client.py#L36-L43) —— 构造客户端，并用 `models.list()` 动态取模型 id。这一步很实用：你不必硬编码模型名，服务端有什么就用什么。

[examples/basic/online_serving/openai_chat_completion_client.py:46-50](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/examples/basic/online_serving/openai_chat_completion_client.py#L46-L50) —— 真正的 chat completion 调用。`model` 来自上一步的列表，`stream` 由命令行参数控制。

[examples/basic/online_serving/openai_chat_completion_client.py:54-58](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/examples/basic/online_serving/openai_chat_completion_client.py#L54-L58) —— 流式与非流式的分支处理：流式时返回的是一个可迭代对象，需要 `for c in chat_completion` 逐块消费；非流式时直接拿到完整结果。

#### 4.2.4 代码实践

1. 实践目标：跑通一次真实的 chat completion 调用（或读懂脚本逻辑）。
2. 操作步骤：
   - （需要服务）先用 `vllm serve <一个小模型>` 启动服务，等待日志出现监听端口；
   - 安装 SDK：`pip install openai`（在项目 `.venv` 外另起环境即可，这只是客户端）；
   - 运行示例：`python examples/basic/online_serving/openai_chat_completion_client.py`；
   - 再试流式：`python examples/basic/online_serving/openai_chat_completion_client.py --stream`。
3. 需要观察的现象：非流式一次性打印整个响应对象；流式则分多次打印多个增量块。
4. 预期结果：控制台输出包含模型生成的回答文本（针对示例中的「2020 年世界大赛在哪里举行」）。
5. 若本地无可用 GPU / 模型，则改为**源码阅读型实践**：阅读脚本并复述「base_url、api_key、model、messages、stream」这五个要素分别对应哪一行代码、各起什么作用。

> 说明：本任务依赖一个可用的 vLLM 服务。若环境不具备，按上面第 5 条做阅读型实践即可，本讲不要求你一定运行成功。

#### 4.2.5 小练习与答案

**练习 1**：示例为什么先调用 `client.models.list()` 而不是直接写死 `model="<某模型名>"`？

**答案**：为了让脚本对任意已部署模型通用。服务启动后 `GET /v1/models` 会返回实际可用的模型 id（即你 `vllm serve` 时指定的模型名），取 `models.data[0].id` 即可，不必在客户端硬编码。

**练习 2**：流式与非流式，客户端拿到的对象类型有什么不同？

**答案**：非流式（`stream=False`）返回一个完整的 `ChatCompletion` 对象，内含全部生成的 token；流式（`stream=True`）返回一个迭代器，每次产出一个 `ChatCompletionChunk`，里面通常只有一个增量 token。所以代码里要用 `for ... in` 来消费流式结果。

---

### 4.3 服务端入口 api_server.py 的位置与职责

#### 4.3.1 概念说明

客户端发出的 HTTP 请求，最终由 vLLM 服务进程里的 **FastAPI 应用**接收。`vllm/entrypoints/openai/api_server.py` 就是这个应用的总装配车间。它的职责不是「逐条处理请求的逻辑」，而是**把各种组件拼装成一个可对外服务的应用**。

> 承接 [u2-l2](u2-l2-cli-and-serve.md)：那一讲提到 api_server.py 负责「建引擎并绑 socket」。本模块把这句话落实到具体函数。

#### 4.3.2 核心流程

`api_server.py` 的装配顺序大致是：

1. `setup_server`：校验参数、提前绑定端口（socket），避免与引擎初始化的竞态。
2. `run_server` → `run_server_worker`：在异步上下文里创建引擎客户端 `engine_client`（即 `EngineClient`，是和推理引擎通信的句柄）。
3. `build_app`：创建 FastAPI 应用，按「支持的任务」逐个把路由器（router）挂上去。
4. `init_app_state`：把 `engine_client` 以及各种 serving 对象（模型列表、渲染器等）写入 `app.state`，供路由处理函数读取。
5. `build_and_serve` → `serve_http`（来自 launcher）：用 uvicorn 监听已绑定的 socket，开始接收请求。

```text
setup_server (绑 socket)
        │
run_server_worker
        │  build_async_engine_client → engine_client
        ▼
build_app  ──►  FastAPI app + 一堆 router
        │
init_app_state ──► app.state.engine_client = engine_client （把引擎挂进应用状态）
        │
serve_http (launcher 跑 uvicorn)
```

#### 4.3.3 源码精读

[vllm/entrypoints/openai/api_server.py:189-211](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/api_server.py#L189-L211) —— `build_app` 的开头：创建 FastAPI 应用，并把命令行参数 `args` 存入 `app.state.args`，后续处理函数可随时读取。

[vllm/entrypoints/openai/api_server.py:214-240](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/api_server.py#L214-L240) —— `build_app` 按需挂载路由器。注意 `register_generate_api_routers(app)`（在「generate」任务下调用）才是真正把 chat completion / completion / responses 等路由挂上的地方——这一点我们放在下一模块展开。

[vllm/entrypoints/openai/api_server.py:310-313](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/api_server.py#L310-L313) —— 鉴权中间件：如果设置了 `--api-key` 或环境变量 `VLLM_API_KEY`，服务会把有效令牌列表交给 `AuthenticationMiddleware`。这正是客户端示例里那个 `api_key` 的服务端对应物——若服务端设了 key，客户端就必须填对，否则被拒。

[vllm/entrypoints/openai/api_server.py:355-397](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/api_server.py#L355-L397) —— `init_app_state` 把 `engine_client`、模型配置、请求日志器等写入 `app.state`。第 394 行 `state.engine_client = engine_client` 是关键：它把「推理引擎句柄」与「HTTP 应用」绑定起来，路由处理函数正是从这里取到引擎去发起推理。

[vllm/entrypoints/openai/api_server.py:751-764](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/api_server.py#L751-L764) —— `run_server` / `run_server_worker`：先 `setup_server` 绑端口，再在异步引擎客户端上下文里进入 `build_and_serve`。

[vllm/entrypoints/openai/api_server.py:792-804](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/api_server.py#L792-L804) —— 文件末尾的 `__main__` 块：用 `FlexibleArgumentParser` + `make_arg_parser` 解析参数，再 `uvloop.run(run_server(args))`。注意注释说明此处需与 `cli/main.py` 的 CLI 入口保持一致（上一讲讲的 `vllm serve` 最终也走到这条路径）。

#### 4.3.4 代码实践（源码阅读型）

1. 实践目标：建立「请求到达后从 app.state 取引擎」的方位感。
2. 操作步骤：在 `api_server.py` 中找到 `init_app_state`，确认 `state.engine_client = engine_client`；再打开任一路由文件（如下一模块的 `models/api_router.py`），看它如何通过 `request.app.state.openai_serving_models` 间接拿到服务能力。
3. 需要观察的现象：路由处理函数本身不创建引擎，而是「从应用状态里拿」。
4. 预期结果：你能用一句话说清「引擎在初始化时被挂进 app.state，请求处理时被各 serving 对象读取」。
5. 待本地验证：可在启动日志中观察 `Supported tasks:` 一行，对照 `build_app` 里哪些 `if "..." in supported_tasks` 分支被触发。

#### 4.3.5 小练习与答案

**练习**：为什么 `setup_server` 要在引擎初始化**之前**就先把端口（socket）绑定好？

**答案**：源码注释点明了原因——为了避免与 Ray（多机分布式）的竞态。提前绑定端口可以让外部（如调度器）尽早探测到服务端口可用，而不必等耗时的引擎初始化完成。这是一种「先占端口、再慢启动」的工程技巧。

---

### 4.4 两个关键路由：/v1/models 与 /v1/chat/completions

#### 4.4.1 概念说明

客户端调用最终落到具体的 HTTP 路径上。本模块聚焦示例脚本用到的两条路径，看它们在源码里如何被定义、如何被挂载：

- `GET /v1/models`：列出可用模型，对应客户端的 `client.models.list()`。
- `POST /v1/chat/completions`：发起对话补全，对应客户端的 `client.chat.completions.create()`。

> 注意：这两条路由并不在 `api_server.py` 里直接定义，而是各自由独立的「router」文件提供，再由 `build_app` 间接挂载。这是 FastAPI 的常见组织方式，便于把不同功能的路由拆到不同文件。

#### 4.4.2 核心流程

```text
build_app (api_server.py)
   └─ if "generate" in supported_tasks:
         register_generate_api_routers(app)          # generate/api_router.py
            └─ attach chat / completion / responses 等 router
                 ├─ chat:  /v1/chat/completions       # chat_completion/api_router.py
                 └─ models: /v1/models                # 由 models/api_router.py 单独挂载
```

请求到达后的处理（以 chat 为例）：

1. FastAPI 把请求体解析成 `ChatCompletionRequest`。
2. 路由函数 `create_chat_completion` 从 `app.state.openai_serving_chat` 取出处理器。
3. 调用 `handler.create_chat_completion(request, raw_request)`，由它驱动引擎并产出结果。
4. 根据返回类型：错误 → `JSONResponse`；完整结果 → `JSONResponse`；流式 → `StreamingResponse`（SSE）。

#### 4.4.3 源码精读

[vllm/entrypoints/generate/api_router.py:21-26](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/generate/api_router.py#L21-L26) —— `register_generate_api_routers` 把 chat completion 的 router 挂到应用上。这是「路径从哪来」的总开关。

[vllm/entrypoints/openai/chat_completion/api_router.py:40-53](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/chat_completion/api_router.py#L40-L53) —— `POST /v1/chat/completions` 的真正定义。注意路径字符串 `/v1/chat/completions` 正是客户端 `base_url + /chat/completions` 拼出来的目标。

[vllm/entrypoints/openai/chat_completion/api_router.py:57-74](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/chat_completion/api_router.py#L57-L74) —— 路由处理逻辑：取 handler、调用 `create_chat_completion`，并依据返回类型在「错误响应 / 普通响应 / 流式响应」三种之间分流。流式用 `text/event-stream`（SSE），对应客户端的 `--stream` 体验。

[vllm/entrypoints/openai/models/api_router.py:20-25](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/models/api_router.py#L20-L25) —— `GET /v1/models` 的定义：从 `app.state.openai_serving_models` 取处理器，调用 `show_available_models()` 并返回 JSON。

[vllm/entrypoints/openai/models/serving.py:149-165](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/openai/models/serving.py#L149-L165) —— `show_available_models` 的实现：返回基础模型卡片，并额外把已加载的 LoRA 适配器也加进列表。这解释了为什么客户端 `models.data[0].id` 能拿到模型名，也说明 LoRA 适配器也会作为「模型」暴露。

#### 4.4.4 代码实践（源码阅读 + 可选运行）

1. 实践目标：把客户端调用与服务端路由一一对应。
2. 操作步骤：
   - 阅读示例脚本第 42 行（`client.models.list()`）与 `models/api_router.py:20`（`/v1/models`），确认对应关系。
   - 阅读示例脚本第 46 行（`chat.completions.create`）与 `chat_completion/api_router.py:40`（`/v1/chat/completions`），确认对应关系。
   - （可选，需服务）用 `curl` 直接验证：`curl http://localhost:8000/v1/models`，对比 `openai` SDK 返回的结构。
3. 需要观察的现象：客户端 SDK 的每个高层方法，都精确映射到一条服务端路由。
4. 预期结果：你能画一张「SDK 方法 → HTTP 方法+路径 → 路由文件 → 处理函数」的对照表。
5. 待本地验证：`curl` 的实际返回结构需在有服务时才能看到。

#### 4.4.5 小练习与答案

**练习 1**：客户端示例用 `client.models.list()` 取模型，如果服务端同时加载了 LoRA 适配器，返回列表里会有什么？

**答案**：除了基础模型卡片外，已加载的 LoRA 适配器也会以 `ModelCard` 形式出现（见 `models/serving.py:149-165`，它把 `lora_cards` extend 进列表）。所以 `models.data[0]` 不一定是基础模型——更稳妥的做法是按名称精确选择。

**练习 2**：流式响应与非流式响应，服务端分别用什么返回类型？

**答案**：非流式返回 `JSONResponse`（一次性的完整 JSON）；流式返回 `StreamingResponse`，媒体类型为 `text/event-stream`（见 `chat_completion/api_router.py:68-74`）。后者就是 SSE，客户端逐块解析。

---

## 5. 综合实践

把本讲内容串起来，完成下面这个**端到端追踪任务**：

> **任务**：写一份「调用追踪表」，把客户端的一句代码，逐步对应到服务端的源码位置。

请按下表填写（示例第一行已给出）：

| 客户端代码（示例脚本） | HTTP 方法 + 路径 | 服务端路由文件:行 | 处理函数 | 最终去向 |
| --- | --- | --- | --- | --- |
| `client = OpenAI(base_url=...)` | （无，仅构造） | — | — | 保存 base_url/api_key |
| `client.models.list()` | `GET /v1/models` | （请你填） | （请你填） | `OpenAIServingModels.show_available_models` |
| `chat.completions.create(stream=False)` | （请你填） | （请你填） | `create_chat_completion` | （请你填：引擎 or handler） |
| `chat.completions.create(stream=True)` | （请你填） | 同上 | 同上 | 返回 `StreamingResponse`（SSE） |

要求：

1. 至少把四行全部填完，行号要准确（参考本讲给出的永久链接）。
2. 用一句话总结：从「客户端方法」到「服务端路由」之间，靠什么把两者解耦？
3. 参考答案要点：靠的是 **OpenAI HTTP 协议**作为契约——客户端只讲协议，服务端只实现协议，二者互不依赖对方的代码。

> 进阶（需可用服务）：启动一个小模型服务，分别用 `openai` SDK 与 `curl` 命中 `/v1/models`，确认两者返回结构一致，从而直观体会「协议兼容」的含义。

## 6. 本讲小结

- vLLM 通过实现 **OpenAI 兼容 HTTP 接口**，让任何用 `openai` SDK 的代码只需改 `base_url` 即可调用，迁移成本几乎为零。
- 客户端示例的关键是 `base_url`（指向本地 `http://localhost:8000/v1`）和 `api_key`（占位 `"EMPTY"`，服务端不强制鉴权时可任意填）。
- `vllm/entrypoints/openai/api_server.py` 是 HTTP 服务的总装配车间：`build_app` 创建 FastAPI 应用并挂载路由器，`init_app_state` 把 `engine_client` 挂进 `app.state`，`serve_http`（来自 launcher）用 uvicorn 监听 socket。
- `POST /v1/chat/completions` 定义在 `chat_completion/api_router.py`，由 `generate/api_router.py` 在「generate」任务下挂载；`GET /v1/models` 定义在 `models/api_router.py`，返回基础模型与 LoRA 适配器列表。
- 路由处理函数本身不直接跑推理，而是从 `app.state` 取出 serving 对象，再由它们驱动引擎——HTTP 层与引擎层由此解耦。

## 7. 下一步学习建议

本讲只到「HTTP 路由」为止，路由之后请求如何进入引擎还没展开。建议接下来：

- 阅读 [u3-l1 V1 多进程架构总览](u3-l1-v1-process-architecture.md)，理解 `engine_client`（即 `EngineClient`）背后是一个**多进程架构**：API Server 进程通过它把请求转交给 EngineCore 进程。
- 阅读 [u3-l4 AsyncLLM 在线引擎客户端](u3-l4-async-llm-engine-client.md)，看清 `app.state.engine_client` 真正的实现类 `AsyncLLM` 如何异步把请求送入引擎。
- 若对采样细节感兴趣，可先跳读 [u2-l4 SamplingParams 采样参数入门](u2-l4-sampling-params-basics.md)，再回到 u3 系统理解在线链路。
