# OpenAI 兼容 API 服务层

> 本讲对应仓库讲义 `u2-l2`，承接 [u2-l1 请求主链路总览](u2-l1-request-main-chain.md)。
> 上一讲我们画出了「HTTP API → Client → Coordinator → Stage → Scheduler → ModelRunner」这条分层链路，
> 本讲下钻到最外层——HTTP API，看看一份 OpenAI 格式的 JSON 请求是怎么被接住、被翻译、又怎么把模型的流式输出变回 OpenAI 格式的 SSE 文本流的。

## 1. 本讲目标

读完本讲，你应当能够：

- 说出 `create_app` 这个 FastAPI 工厂「只做什么、不做什么」，并能区分它与 `launch_server` 的职责边界（承接 u2-l1）。
- 默写 OpenAI 兼容路由表：`/v1/chat/completions`、`/v1/audio/speech`、`/health`、`/v1/models` 等分别由哪个注册函数挂上去、分别返回什么。
- 跟踪一次 `ChatCompletionRequest` 是如何被 `_build_chat_generate_request` 翻译成内部 `GenerateRequest` 的，特别是 `images / audios / videos` 这类多模态字段去了哪里。
- 讲清楚 SSE 流式的四个语义：第一个 chunk 只发 `role`、文本与音频分开发、末尾的 `finish_reason` chunk、以及 `[DONE]` 哨兵。

## 2. 前置知识

在进入源码之前，先用三段大白话建立直觉。

**OpenAI 兼容（OpenAI-compatible）是什么意思？**
OpenAI 的 HTTP API 长这样：往 `https://api.openai.com/v1/chat/completions` POST 一段 JSON（`{"model": ..., "messages": [...], "stream": true}`），就能拿到回复。所谓「兼容」，就是 SGLang-Omni 的服务端**故意长得和它一样**：同样的 URL、同样的请求字段、同样的响应结构。这样所有为 OpenAI 写的客户端 SDK（`openai-python` 等）换个 `base_url` 就能直接打过来，零迁移成本。

**FastAPI 与路由（route）。**
FastAPI 是 Python 的异步 Web 框架。一个「路由」就是把一个 URL（比如 `POST /v1/chat/completions`）绑定到一个 Python 异步函数上。当请求到达，FastAPI 负责把 JSON body 反序列化成一个 Pydantic 模型（这里就是 `ChatCompletionRequest`），交给这个函数处理，再把返回值序列化成 JSON 响应。

**SSE（Server-Sent Events，服务器发送事件）。**
流式生成（一个字一个字往外吐）不能用「一次请求一次响应」的普通 JSON。SSE 是浏览器/HTTP 原生支持的一种简单流式协议：服务器设置响应头 `Content-Type: text/event-stream`，然后用 `data: <一段JSON>\n\n` 的格式持续往连接里写一段一段的消息，客户端边读边显示，直到读到一条特殊的 `data: [DONE]` 表示「流结束」。这就是 OpenAI 流式接口的底层传输方式。

> 一个关键认知（承接 u2-l1）：HTTP 层是**翻译搬运层**，它**不碰 GPU**。真正算东西的是更内层的 Scheduler / ModelRunner。所以本讲你会看到大量「字段搬来搬去」「JSON 进、SSE 出」的胶水代码——这是有意的边界划分，不是冗余。

## 3. 本讲源码地图

本讲聚焦两个文件，必要时旁征若干邻居文件：

| 文件 | 作用 |
| --- | --- |
| [sglang_omni/serve/openai_api.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py) | **主文件**。FastAPI 应用工厂 `create_app`、所有路由注册函数、请求转换 `_build_chat_generate_request`、流式生成器 `_chat_stream` 全在这里。 |
| [sglang_omni/serve/protocol.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/protocol.py) | **协议定义**。所有 OpenAI 兼容的请求/响应 Pydantic 模型（`ChatCompletionRequest`、`ChatCompletionResponse`、流式 chunk 模型等）。 |
| [sglang_omni/client/types.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/types.py) | 内部 Client 的类型，关键是 `GenerateRequest`（转换的目标类型）与流式 `CompletionStreamChunk`。 |
| [sglang_omni/client/client.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py) | Client 层的 `completion` / `completion_stream`，HTTP 层调它们来真正驱动管线。 |
| [sglang_omni/proto/request.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/request.py) | 定义 `EXPLICIT_GENERATION_PARAMS_KEY`，理解「显式参数」概念时用到。 |

记忆口诀：**「协议在 `protocol.py`，胶水在 `openai_api.py`」**。前者只有数据形状（Pydantic 类），后者才有行为（路由与转换函数）。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 FastAPI 应用工厂 `create_app`** —— HTTP 层的入口与边界。
- **4.2 路由表面** —— 这层到底对外暴露了哪些 URL。
- **4.3 请求转换 `_build_chat_generate_request`** —— OpenAI JSON → 内部 `GenerateRequest`。
- **4.4 SSE 流式 `_chat_stream`** —— 把管线的流式 chunk 变回 OpenAI SSE。

### 4.1 FastAPI 应用工厂：create_app

#### 4.1.1 概念说明

`create_app` 是一个**应用工厂函数**（application factory）：给它一个已经连好管线的 `Client`，它返回一个配置好的 FastAPI 实例。它的职责被严格限定为「**组装 HTTP 表面**」：

- 建一个空的 FastAPI app；
- 装上中间件（CORS 跨域、上传体积限制）；
- 把 `Client`、模型名、speech 校验器等放进 `app.state`，供路由函数取用；
- 调用一串 `_register_xxx(app)` 把路由一组组挂上去；
- 如果开了实时会话，再挂一个 WebSocket 路由。

它**不负责**：拉起管线进程、建 ZMQ 通道、加载权重、跑 Uvicorn。那些是 `launch_server`（在 `cli/serve.py` 与 server launcher 中）干的活——`create_app` 只产出 FastAPI 对象，`launch_server` 把这个对象交给 Uvicorn 去监听端口。这正是 u2-l1 强调的 `create_app`（只建路由）与 `launch_server`（管完整生命周期）的分工。

> 设计意图：把「HTTP 形状」与「运行时生命」解耦，使得单元测试可以直接 `create_app(fake_client)` 起一个内存里的服务（见 `tests/unit_test/serve/test_openai_api.py` 大量用了这个套路），完全不需要真模型与 GPU。

#### 4.1.2 核心流程

```
create_app(client, *, model_name=..., 语音/realtime/admin 各种开关)
   │
   ├─ app = FastAPI(title="sglang-omni", version="0.1.0")
   │
   ├─ 装中间件
   │    ├─ CORSMiddleware            （允许跨域，方便浏览器直连）
   │    └─ VoiceUploadBodyLimitMiddleware   （超大音频上传在解析前就拒绝）
   │
   ├─ 写 app.state（路由函数共享的“上下文背包”）
   │    ├─ app.state.client               = client
   │    ├─ app.state.model_name           = model_name or "sglang-omni"
   │    ├─ app.state.speaker_sample_store = SpeakerSampleStore()
   │    └─ app.state.speech_service       = SpeechRequestValidator(...)
   │
   ├─ resolve_admin_api_key(admin_api_key)   （admin 鉴权 key，可为空）
   │
   ├─ 注册路由组（每组一个 _register_xxx 函数）
   │    register_favicon / _register_health / _register_models
   │    _register_admin / _register_chat_completions / _register_voices
   │    _register_generate / _register_speech / _register_speech_batch
   │    _register_speech_ws / _register_transcriptions
   │    if enable_realtime: _register_realtime
   │
   └─ return app
```

注意：`app.state` 是 FastAPI 提供的一个自由属性袋（任意属性都可挂）。这里把 `client` 挂上去，等价于在说「**所有路由都共享同一个 Client 实例**」，HTTP 层不自己造 Client，而是用启动时注入的那个。这就把「翻译」和「执行」彻底分开：路由函数从 `app.state.client` 拿到 Client，把翻译好的 `GenerateRequest` 交给它，自己就完事了。

#### 4.1.3 源码精读

应用工厂本体：

[sglang_omni/serve/openai_api.py:182-223](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L182-L223) —— `create_app` 的签名与文档串，文档串里就列出了全部对外端点（一份权威的路由清单，与模块顶部 docstring 呼应）。

中间件与 `app.state` 的装配：

[sglang_omni/serve/openai_api.py:224-257](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L224-L257) —— 建空 app、装 CORS 与上传体积限制中间件、把 `client`/`model_name`/`speaker_sample_store`/`speech_service` 塞进 `app.state`。注意 `model_name or "sglang-omni"` 这一行——模型名有默认值兜底，这就是为什么 `/v1/models` 永远至少返回一个名字。

路由注册的总调度：

[sglang_omni/serve/openai_api.py:262-276](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L262-L276) —— 连续十几个 `_register_xxx(app)` 调用，每个负责挂一组路由；最后 `if enable_realtime` 才条件挂载 WebSocket 实时会话。读这段就等于读完了「这层对外暴露的全部 URL」的索引。

模块顶部 docstring 也是一份可信的「路由说明书」：

[sglang_omni/serve/openai_api.py:4-17](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L4-L17) —— 列出 chat / speech / speech batch / speech stream(ws) / voices / models / fs / health / realtime(ws) 全部端点。

#### 4.1.4 代码实践

**实践目标**：验证 `create_app` 是一个可脱离真模型独立运行的纯工厂，并体会「注入 fake client」的测试套路。

**操作步骤**（源码阅读 + 轻量运行，不需要 GPU）：

1. 打开 [tests/unit_test/serve/test_openai_api.py:96-98](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/serve/test_openai_api.py#L96-L98)，看 `_fault_client` 如何用一个「会报错的假 Client」构造服务。注意它直接 `create_app(...)` 拿到 FastAPI app，再套 `TestClient`。
2. 在容器/本地装好依赖后，写一段 6 行的脚本（**示例代码，非仓库原有**）：

   ```python
   # 示例代码：用一个最小假 Client 起一个内存 HTTP 服务
   from fastapi.testclient import TestClient
   from sglang_omni.serve import create_app
   from tests.unit_test.serve.test_openai_api import SuccessfulSpeechClient

   app = create_app(SuccessfulSpeechClient(), model_name="my-tts")
   r = TestClient(app).get("/v1/models")
   print(r.status_code, r.json())
   ```

3. 运行它。

**需要观察的现象**：不需要启动任何模型进程，`/v1/models` 就能返回。

**预期结果**：输出形如 `200 {'object': 'list', 'data': [{'id': 'my-tts', 'root': 'my-tts', ...}]}`。这证明 HTTP 表面完全独立于运行时——这正是 `create_app` 与 `launch_server` 解耦带来的好处。

> 若依赖未装全导致 `import sglang_omni.serve` 失败，则「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`create_app` 把 `client` 放进 `app.state.client`，而不是作为路由函数的参数逐层传递。这种写法的好处是什么？

**参考答案**：FastAPI 的路由函数签名只暴露「与 HTTP 相关的东西」（请求体、Query 参数），而把「运行时依赖」（Client）放在 `app.state` 这种共享上下文里。好处是路由签名干净、注册简单（`@app.post(...)` 不需要手动接线），且所有路由天然共享同一个 Client 单例——这正是「HTTP 层只翻译、统一交给同一个执行入口」边界的代码体现。

---

### 4.2 路由表面：从 OpenAI 标准到 omni 扩展

#### 4.2.1 概念说明

SGLang-Omni 的 HTTP 路由可以分成四类：

1. **OpenAI 标准 chat 路由**：`POST /v1/chat/completions`。这是被服务模型的主入口，既支持纯文本，也支持「文本+音频」的多模态输出。
2. **OpenAI 标准 audio 路由**：`POST /v1/audio/speech`（文本转语音）、`POST /v1/audio/transcriptions`（语音转文本/ASR）、`GET /v1/models`、`GET /health`。
3. **omni 扩展路由**：`POST /generate`（面向 RL rollout 的低层生成接口，可吃 `input_ids`/`prompt`/`messages`）、`/v1/audio/speech/batch`（批量 TTS）、`WS /v1/audio/speech/stream`（有状态 TTS 流）、`/v1/audio/voices`（声音克隆的参考音频管理）、`WS /v1/realtime`（OpenAI Realtime API）。
4. **admin 控制路由**（u6-l4 会详讲）：`/model_info`、`/pause_generation`、`/update_weights_from_disk`、`/weights_checker` 等等，用于推理侧 RL 的权重热更新。

本讲聚焦第 1 类（chat）和 `/health`、`/v1/models` 这两个最简单的，其余作为「路由表」了解即可。

#### 4.2.2 核心流程：路由对照表

| 方法/路径 | 注册函数 | 作用 | 返回类型 |
| --- | --- | --- | --- |
| `GET /health` | `_register_health` | 健康检查，按运行时是否 `running` 返回 200/503 | JSON |
| `GET /v1/models` | `_register_models` | 列出（启动时定死的单个）模型名 | JSON |
| `POST /v1/chat/completions` | `_register_chat_completions` | 文本/多模态 chat，支持 stream | JSON 或 SSE |
| `POST /generate` | `_register_generate` | RL rollout 专用低层生成 | JSON |
| `POST /v1/audio/speech` | `_register_speech` | 文本转语音 | 二进制音频或 PCM 流 |
| `POST /v1/audio/speech/batch` | `_register_speech_batch` | 批量 TTS | JSON |
| `WS /v1/audio/speech/stream` | `_register_speech_ws` | 有状态 TTS WebSocket | WS 帧 |
| `GET/POST/DELETE /v1/audio/voices[/{name}]` | `_register_voices` | 声音克隆参考样本管理 | JSON |
| `POST /v1/audio/transcriptions` | `_register_transcriptions` | ASR 转录 | JSON/text/SSE |
| `WS /v1/realtime` | `_register_realtime`（可选） | OpenAI Realtime 会话 | WS 帧 |
| `/model_info`、`/pause_generation` 等 | `_register_admin` | RL admin 控制 | JSON |

注意 `/health` 的一个细节：它返回的状态码**取决于运行时**，而不是「进程活着」。也就是说，Uvicorn 进程在跑，但管线还没 ready（或挂了），`/health` 会返回 503。

#### 4.2.3 源码精读

健康检查——注意它读 `client.health()` 的 `running` 字段来决定状态码：

[sglang_omni/serve/openai_api.py:383-397](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L383-L397) —— `is_running` 为真返回 200 `"healthy"`，否则 503 `"unhealthy"`，并把运行时 info 一并透出。这是给 K8s/负载均衡做存活探针用的标准做法。

模型列表——注意它**不查运行时**，只回吐启动时定死的那个名字：

[sglang_omni/serve/openai_api.py:400-414](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L400-L414) —— 从 `app.state.model_name` 取名字，包成 OpenAI 的 `ModelList`/`ModelCard` 结构返回。这就是为什么 SGLang-Omni 的 `/v1/models` 永远只列一个模型（它一次只服务一个模型家族）。

chat 路由本体——这是本讲后面两个模块的重头戏，先看它的「分发骨架」：

[sglang_omni/serve/openai_api.py:634-676](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L634-L676) —— `chat_completions` 路由函数做了三件事：① 生成 `request_id`/`response_id`/`created`/`model`（响应元信息）；② 调 `_build_chat_generate_request(req)` 把请求翻译成内部 `GenerateRequest`（4.3 详讲）；③ 按 `req.stream` 二选一——流式走 `_chat_stream`（4.4 详讲），非流式走 `_chat_non_stream`。

#### 4.2.4 代码实践

**实践目标**：用 `curl` 验证 `/health` 与 `/v1/models` 两个最轻量的端点。

**操作步骤**（承接 u1-l4 启动服务后）：

1. 按 u1-l4 启动一个 text-only 的 Qwen3-Omni 服务（例如 `sgl-omni serve --model-path <path> --text-only`）。
2. 健康检查：

   ```bash
   curl -i http://127.0.0.1:30000/health
   ```

3. 列模型：

   ```bash
   curl -s http://127.0.0.1:30000/v1/models | python -m json.tool
   ```

**需要观察的现象**：服务刚启动、管线还没 ready 时 `/health` 可能先返回 503，ready 后变 200；`/v1/models` 的 `data` 数组永远只有一个元素，其 `id` 就是你启动时指定的 `--model-name`（缺省为 `sglang-omni`）。

**预期结果**：`/health` 在 ready 后返回 `{"status": "healthy", "running": true, ...}`；`/v1/models` 返回单元素列表。

> 若手头没有可启动的模型权重，此实践「待本地验证」，可改为用 4.1.4 的 `TestClient` 假 Client 路线验证 `/v1/models` 行为。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `/health` 不直接返回 200，而要去查 `client.health()` 的 `running` 字段？

**参考答案**：存活探针需要区分「进程在跑」和「服务真能用」。Uvicorn 进程活着不代表管线 ready（权重还在加载、NCCL 还在握手等）。把 `running` 映射成状态码，负载均衡器就能在 503 期间先把流量导走，避免把请求打进一个还没就绪的实例。

**练习 2**：`/v1/models` 为什么永远只列一个模型？这和 SGLang-Omni 的「一次服务一个模型家族」定位有什么关系？

**参考答案**：一个 SGLang-Omni server 进程在启动时就绑定了一份 `PipelineConfig`（即一个模型家族的多阶段拓扑，见 u2-l5），`model_name` 在 `create_app` 时写死进 `app.state`。所以 `/v1/models` 只是如实汇报这个启动期事实，而不是运行时动态扫描。要服务多个模型家族，应该起多个 server，再用 Router（u7-l1）在前面对多实例做路由。

---

### 4.3 请求转换：_build_chat_generate_request

> 这是本讲最核心的一节，也是本讲的指定代码实践所在。

#### 4.3.1 概念说明

OpenAI 的请求模型 `ChatCompletionRequest` 和 SGLang-Omni 内部的 `GenerateRequest`（定义在 `client/types.py`）是**两套不同的形状**：

- `ChatCompletionRequest` 面向外部用户，长得像 OpenAI（`messages`、`temperature`、`max_tokens`、`modalities`、`audio`、`audios`、`images`……）。
- `GenerateRequest` 面向内部 Client/管线，是一个更通用的「生成请求」，字段更扁平：`prompt/messages`、`sampling`、`stage_sampling`、`extra_params`、`metadata`、`output_modalities`。

`_build_chat_generate_request` 就是这两者之间的**翻译器**，在 `chat_completions` 路由里被调用。它要解决三类问题：

1. **字段重命名与默认值**：OpenAI 用 `max_tokens`/`max_completion_tokens`，内部用 `max_new_tokens`；OpenAI 的 `stop` 可以是字符串或列表，内部统一成列表。
2. **多模态输入归位**：`images / audios / videos` 这些 omni 扩展字段，在内部 `GenerateRequest` 里没有专门字段，全部塞进 `metadata` 这个「自由字典」。
3. **显式参数标记**：这是最微妙的一点——见下面单独解释。

**为什么这个翻译放在 HTTP 层，而不是运行时层？**
因为 `ChatCompletionRequest` 是**纯粹的外部协议**，运行时（Client/Coordinator/Stage）根本不应该知道「OpenAI 长什么样」。把翻译做在 HTTP 层，等于把外部协议的脏活全部封死在边界上，内部管线只认一种干净的 `GenerateRequest`。这样：换协议（比如将来支持别的 API 形态）只动 HTTP 层；管线逻辑不会被外部协议绑架。

**「显式参数」是什么？为什么重要？**

看这条规则：用户**没传** `temperature`，和用户**传了** `temperature=1.0`，在 `ChatCompletionRequest` 里 `temperature` 都是 `None`（没传）或 `1.0`（传了）。但「没传」时，模型自己可能想用 `temperature=0.3`（模型默认值）；而「传了 1.0」时，用户的意图是「我就要 1.0」，不能被模型默认覆盖。

HTTP 层用 `model_fields_set`（Pydantic 记录的「用户显式设置过哪些字段」）来判断哪些字段是用户主动给的，把这些字段名列表塞进 `metadata["explicit_generation_params"]`，供下游模型阶段的 request_builder 读取（在 `models/*/request_builders.py` 里被消费）。这样模型默认值就不会静默覆盖用户的「显式默认值」——这是避免「端点默认值静默覆盖模型默认值」陷阱的关键机制（u5-l3 还会从模型接入侧再讲一遍）。

#### 4.3.2 核心流程

`_build_chat_generate_request(req: ChatCompletionRequest) -> GenerateRequest` 的翻译步骤：

```
1. 规范化 stop：str → [str]，list → list，None → []
2. 构造 SamplingParams：
     temperature   = req.temperature   if not None else 1.0   # 注意兜底默认值
     top_p         = req.top_p         if not None else 1.0
     top_k         = req.top_k         if not None else -1
     min_p         = req.min_p         if not None else 0.0
     repetition_penalty = ... if not None else 1.0
     stop = stop,  seed = req.seed,  max_new_tokens = req.effective_max_tokens
3. messages: [Message(role, content) for m in req.messages]
4. output_modalities = req.modalities or ["text"]
5. stage_sampling: 把 req.stage_sampling 的 dict 逐个 SamplingParams(**...)
6. metadata（自由字典，多模态/显式参数都进这里）：
     audio_config    ← req.audio
     audios          ← req.audios
     images          ← req.images
     videos          ← req.videos
     video_fps / video_max_frames / video_min_pixels / video_max_pixels / video_total_pixels
     explicit_generation_params ← _explicit_generation_params(req)   # 关键
7. extra_params：talker_temperature / talker_top_p / talker_top_k
                 / talker_repetition_penalty / talker_max_new_tokens（仅非 None 时）
8. return GenerateRequest(model, messages, sampling, stage_sampling,
                          stage_params, extra_params, stream, max_tokens,
                          output_modalities, metadata)
```

一条主线：**所有「内部没有专门字段」的东西，统统落进 `metadata`**。这是 omni 的设计惯例——保持 `GenerateRequest` 的核心字段稳定，把模型/模态特有的东西塞进可扩展的 `metadata` 与 `extra_params`。

#### 4.3.3 源码精读

转换函数本体（重点看 metadata 装配段）：

[sglang_omni/serve/openai_api.py:886-982](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L886-L982) —— `_build_chat_generate_request`。其中第 922-958 行是关键：`audios/images/videos` 以及一串 `video_*` 参数被收进 `metadata`，并调用 `_record_explicit_generation_params` 写入显式参数清单。

显式参数的提取（基于 Pydantic 的 `model_fields_set`）：

[sglang_omni/serve/openai_api.py:863-883](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L863-L883) —— `_explicit_generation_params` 取出「用户真正设置过且非 None」的采样字段名并排序；`_record_explicit_generation_params` 把它写进 `metadata[EXPLICIT_GENERATION_PARAMS_KEY]`。注意条件 `field in fields_set and getattr(request, field) is not None`——既要求「设置过」又要求「非 None」，两者都满足才算显式。

两侧的协议形状对照（请求侧）：

[sglang_omni/serve/protocol.py:41-103](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/protocol.py#L41-L103) —— `ChatCompletionRequest`。注意第 71-84 行的 `audios/images/videos/video_*` 注释明确写了「sglang-omni extension」；第 101-103 行的 `effective_max_tokens` 属性实现 `max_completion_tokens or max_tokens` 的优先级。

请求侧的目标类型（内部 `GenerateRequest`）：

[sglang_omni/client/types.py:82-123](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/types.py#L82-L123) —— 注意它有 `metadata: dict = field(default_factory=dict)` 和 `extra_params: dict` 两个自由袋，正好承接 HTTP 层塞进来的多模态与 talker 参数。

显式参数 key 的定义：

[sglang_omni/proto/request.py:31](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/request.py#L31) —— `EXPLICIT_GENERATION_PARAMS_KEY = "explicit_generation_params"`。下游（例如 `sglang_omni/models/qwen3_tts/request_builders.py:441-443`、`models/moss_tts/request_builders.py:266-268`）会从 metadata 里读这个 key，决定哪些采样参数用用户值、其余让位给模型默认值。

#### 4.3.4 代码实践（本讲指定实践）

**实践目标**：在 `openai_api.py` 中找到 `_build_chat_generate_request`，跟踪 `images / audios / videos` 字段如何进入 `GenerateRequest.metadata`，并写一句话总结「该转换为何放在 HTTP 层而非运行时层」。

**操作步骤**（源码阅读 + 单测验证，不需要 GPU）：

1. 打开 [sglang_omni/serve/openai_api.py:922-944](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L922-L944)，逐行跟踪三条数据通路：
   - `req.audios`（`list[str]`，路径或 URL）→ 局部变量 `audios` → `metadata["audios"]`
   - `req.images` → `metadata["images"]`
   - `req.videos` → `metadata["videos"]`，外加 `req.video_fps` 等 → 各自独立的 `metadata["video_*"]` 键
2. 确认这些字段在 `GenerateRequest` 上**没有专门属性**（看 [client/types.py:82-102](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/types.py#L82-L102)），只能靠 `metadata` 字典承载。
3. 用一条现成单测佐证你的理解（这条单测验证的是 `explicit_generation_params`，但走的是同一个转换函数）：

   ```bash
   pytest tests/unit_test/serve/test_openai_api.py \
       -k "test_chat_request_preserves_explicit_default_sampling_values or test_chat_request_omits_explicit_params_when_sampling_omitted" -q
   ```

4. **自己写一句话**回答：为什么这个转换要放在 HTTP 层而不是运行时层？

**需要观察的现象**：转换函数对 `images/audios/videos` 的处理是「纯搬运」——没有任何校验、下载、解码，只是把引用（路径/URL）原样放进 `metadata`。真正的媒体读取与解码发生在更内层的 preprocessing 阶段（见 u5-l4 的 `resource_connector`，它才负责安全地读取本地媒体）。

**预期结果**：你应当得出类似下面的结论——

> 「`images/audios/videos` 在 HTTP 层只作为路径/URL 引用被原样塞进 `GenerateRequest.metadata`，供下游 preprocessing 阶段解析。转换放在 HTTP 层，是因为 `ChatCompletionRequest` 是纯外部协议，运行时不该感知它的形状；把翻译封死在边界上，管线内部只认一种干净的 `GenerateRequest`，外部协议的变更不会污染管线。」

> 关于步骤 3 的命令：若环境未配好 pytest/依赖，「待本地验证」；但你仍可直接阅读 [test_openai_api.py:648-680](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/serve/test_openai_api.py#L648-L680) 这三条单测的断言来理解 `explicit_generation_params` 的产出规则（设了 `temperature=1.0/top_p=1.0/top_k=-1` → 列表含这三项；都没设 → metadata 里压根没这个 key）。

#### 4.3.5 小练习与答案

**练习 1**：用户在请求里写了 `"max_tokens": 512`，但没写 `"max_completion_tokens"`。最终 `GenerateRequest.sampling.max_new_tokens` 会是多少？依据是哪一行代码？

**参考答案**：会等于 512。依据是 [protocol.py:101-103](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/protocol.py#L101-L103) 的 `effective_max_tokens` 属性 `return self.max_completion_tokens or self.max_tokens`——优先用 `max_completion_tokens`，为空则回落到 `max_tokens`；该属性在 [openai_api.py:906](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L906) 被传给 `SamplingParams(max_new_tokens=...)`。

**练习 2**：假设某 TTS 模型的默认 `temperature=0.3`。用户发来的 chat 请求里**完全没带** `temperature` 字段。下游会得到 `explicit_generation_params` 吗？模型默认的 0.3 还能生效吗？

**参考答案**：不会得到 `explicit_generation_params`（因为 `temperature` 不在 `model_fields_set` 里），于是下游 request_builder 知道「用户没指定温度」，可以放心使用模型自己的 0.3 默认值。反之若用户传了 `temperature=1.0`（即便是默认数值），`temperature` 会出现在显式列表里，下游就必须尊重 1.0。这正是显式参数机制要解决的「静默覆盖」问题。

**练习 3**：`talker_temperature` 这类 talker 专用参数，为什么走 `extra_params` 而不是 `metadata`？

**参考答案**：这是一种命名空间的划分约定。`metadata` 主要承载「输入数据引用 + 协议级开关」（多模态路径、显式参数、task 标记等），而 `extra_params` 承载「按字段名直传给特定阶段的生成参数」。`talker_*` 在 [openai_api.py:960-969](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L960-L969) 被逐个挑出来（仅非 None）放进 `extra_params`，便于 talker 阶段直接按字段名取用，而不必和一堆媒体引用混在同一个字典里。

---

### 4.4 SSE 流式：_chat_stream 的语义

#### 4.4.1 概念说明

当请求带 `"stream": true` 时，`chat_completions` 路由不返回普通 JSON，而是返回一个 `StreamingResponse`，其 body 是一个异步生成器 `_chat_stream` 产出的 SSE 事件流。`_chat_stream` 做的事情是：**把内部 Client 吐出的 `CompletionStreamChunk` 序列，逐个翻译成 OpenAI 格式的 SSE `data:` 行**。

OpenAI 流式 chat 的几条硬性语义，`_chat_stream` 都必须遵守：

1. **第一个 chunk 只发 `role: "assistant"`**，不带内容（告诉客户端「助手开口了」）。
2. **文本增量与音频增量分开发**：文本进 `delta.content`，音频进 `delta.audio.data`（base64）。
3. **末尾单独发一个 `finish_reason` chunk**：`delta` 为空，带上 `finish_reason`（如 `"stop"`/`"length"`）和可选 `usage`。
4. **最后发一行 `data: [DONE]`** 哨兵，表示流彻底结束。
5. **失败时不发 `[DONE]`**：若管线报错，生成器直接抛异常，让连接以错误关闭，避免客户端误以为正常结束。

#### 4.4.2 核心流程

`_chat_stream` 是一个 `async` 生成器，结构如下：

```
role_sent = False
finish_reason = None
final_usage = None

async for chunk in client.completion_stream(gen_req, request_id=...):
    # (A) 如果这个 chunk 带了 finish_reason，先记下来（可能还带 usage）
    #     特殊情况：某些管线只在“最后一个聚合 chunk”里才吐 finish_reason，
    #     但这个 chunk 可能同时还有有效的 text/audio —— 不能因为带 finish 就丢掉它的 payload。
    if chunk.finish_reason is not None:
        finish_reason, final_usage = chunk.finish_reason, chunk.usage
        if not has_payload(chunk): continue      # 纯收尾 → 留到循环后再发

    # (B) 组装 delta，按需置 emit 标志
    delta = ChatCompletionStreamDelta()
    if not role_sent:  delta.role = "assistant"; role_sent=True; emit=True   # 首chunk只发role
    if 文本且 modality 含 text: delta.content = chunk.text; emit=True
    if 音频且 modality 含 audio: delta.audio = ChatCompletionAudio(id, data); emit=True
    if not emit: continue

    # (C) 发一条 SSE：data: {chat.completion.chunk ...}\n\n
    yield f"data: {json.dumps(...)}\n\n"

# 循环结束后：
# (D) 发 finish chunk：空 delta + finish_reason(或默认"stop") + usage
yield f"data: {json.dumps(...)}\n\n"
# (E) 发哨兵
yield f"data: [STREAM_DONE_SENTINEL]\n\n"     # 即 data: [DONE]\n\n
```

三个值得记住的细节：

- **`requested_modalities` 过滤**：如果用户只请求了 `["text"]`，即便管线吐了音频 chunk，也不会发给客户端（`"audio" in requested_modalities` 为假）。反之亦然。这是按用户声明的输出模态做过滤。
- **`finish_reason` 与 payload 可能在同一 chunk**：循环顶部有一段专门处理「带 finish 的聚合 chunk」，避免把它的有效载荷误丢（这段逻辑有注释专门说明，见源码精读）。
- **`[DONE]` 哨兵的常量化**：`STREAM_DONE_SENTINEL = "[DONE]"`（[openai_api.py:117](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L117)），transcription 流也复用它。

#### 4.4.3 源码精读

流式生成器本体：

[sglang_omni/serve/openai_api.py:750-860](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L750-L860) —— `_chat_stream`。注意：首 chunk 发 role（797-801）、文本分支（803-806）、音频分支（808-818）三段并列；循环外的 finish chunk（841-858）与 `[DONE]`（860）。

「带 finish 的聚合 chunk」的特殊处理（带详细注释）：

[sglang_omni/serve/openai_api.py:771-792](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L771-L792) —— 捕获 `finish_reason` 与 `usage`，但若该 chunk 还带有有效 payload（文本或音频），就继续往下走翻译，而不是 `continue` 丢掉它。注释明确写了「Some pipelines only emit a final aggregate chunk; do not drop its text/audio」。

非流式对照（理解流式的语义后，非流式就是它的「聚合版」）：

[sglang_omni/serve/openai_api.py:679-747](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L679-L747) —— `_chat_non_stream`：调 `client.completion(...)` 拿一个聚合好的 `CompletionResult`，按 `requested_modalities` 组装 `message`（文本进 `content`、音频进 `audio`），包成 `ChatCompletionResponse` 返回。聚合（文本拼接、音频合并、base64 编码）发生在 Client 层，不在 HTTP 层——再次印证「HTTP 层不碰数据细节」。

Client 侧的流式供给（`_chat_stream` 消费的就是它）：

[sglang_omni/client/client.py:159-188](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L159-L188) —— `completion_stream`：迭代更底层的 `generate()`，把每个 `GenerateChunk` 翻成 `CompletionStreamChunk`，**音频在 yield 之前就已完成 base64 编码**，所以 HTTP 层永远不用碰 numpy / 原始字节。这就是 `_chat_stream` 里能直接 `delta.audio.data = chunk.audio_b64` 的原因。

流式 chunk 的形状：

[sglang_omni/client/types.py:211-220](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/types.py#L211-L220) —— `CompletionStreamChunk`：`text` / `modality` / `audio_b64` / `finish_reason` / `usage` / `stage_name`。`_chat_stream` 几乎就是把这个形状一一映射到 `ChatCompletionStreamDelta`。

SSE 协议响应：

[sglang_omni/serve/openai_api.py:652-665](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L652-L665) —— 流式分支返回 `StreamingResponse(_chat_stream(...), media_type="text/event-stream")`。

#### 4.4.4 代码实践

**实践目标**：用 `curl` 直观看到一条 SSE 流的逐行形态，验证「首 chunk 仅 role → 文本增量 → finish chunk → [DONE]」四个语义。

**操作步骤**（需要一个可流式的服务）：

1. 启动一个支持文本流式的服务（承接 u1-l4）。
2. 发送流式请求并强制逐行打印（`-N` 关闭缓冲，`--no-buffer`）：

   ```bash
   curl -N --no-buffer http://127.0.0.1:30000/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{
       "model": "qwen3-omni",
       "messages": [{"role": "user", "content": "用一句话介绍你自己"}],
       "stream": true
     }'
   ```

3. 观察输出，逐条对号入座。

**需要观察的现象**：终端会持续滚出形如 `data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"delta":{...},"finish_reason":null}]}` 的行。

**预期结果**（按出现顺序）：

1. 第一条的 `delta` 里**只有** `"role":"assistant"`，没有 `content`。
2. 中间若干条的 `delta` 里**只有** `"content":"..."`（一段一段的文本增量）。
3. 接近末尾出现一条 `delta` 为空（`{}`）、`"finish_reason":"stop"` 的收尾 chunk。
4. 最后一行是 `data: [DONE]`。

> 若没有可启动的模型，「待本地验证」。可退而用 `test_chat_stream_failure_closes_without_done_sentinel`（[test_openai_api.py:619-645](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/serve/test_openai_api.py#L619-L645)）做源码阅读型实践：该单测驱动 `_chat_stream` 走「失败」路径，断言**失败时不会吐 `[DONE]`**——这正好印证上面的第 5 条语义。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_chat_stream` 要在循环**之外**再单独发一个 finish chunk，而不是直接把 `finish_reason` 挂在最后一个内容 chunk 上？

**参考答案**：因为 OpenAI 客户端按约定解析：内容 chunk 的 `delta` 带文本、`finish_reason` 为 `null`；收尾 chunk 的 `delta` 为空、`finish_reason` 有值。把两者混在一个 chunk 里会让部分客户端丢失「最后一段文本」或误判结束。更何况「带 finish 的 chunk 可能还带 payload」这个现实情况（见 771-792 行），最稳妥的做法就是内容归内容、收尾归收尾，分开发。

**练习 2**：`completion_stream`（Client 层）在 yield 之前把音频编码成 base64。这个「编码前置」为什么不让 HTTP 层自己做？

**参考答案**：为了让 HTTP 层保持「纯翻译、不碰数据」。原始音频是 numpy 数组 / 原始字节，base64 编码是数据细节。把它下沉到 Client 层，HTTP 层拿到的 `CompletionStreamChunk.audio_b64` 已经是可直接写进 JSON 的字符串，`_chat_stream` 只需原样赋值。这维持了「外层只翻译搬运」的不变量（承接 u2-l1）。

**练习 3**：如果请求声明 `"modalities": ["text"]`，但管线出于某种原因吐了一个音频 chunk，会发生什么？

**参考答案**：该音频 chunk 不会发给客户端——因为 [openai_api.py:809-813](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L809-L813) 的音频分支带了 `"audio" in requested_modalities` 守卫，条件为假时 `emit` 不会被置真，进而走到 `if not emit: continue` 被静默跳过。HTTP 层按用户声明的输出模态做了过滤。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「**完整跟踪一次流式 chat 请求在 HTTP 层的命运**」的小任务。

**任务**：给定下面这条请求，请按时间顺序写出它在 HTTP 层（`openai_api.py`）经过的每一个函数与字段流转，并标注「这一步发生在哪个最小模块」。

```json
{
  "model": "qwen3-omni",
  "messages": [{"role": "user", "content": "看这张图"}],
  "images": ["/data/cat.png"],
  "temperature": 0.7,
  "modalities": ["text"],
  "stream": true
}
```

**建议步骤**：

1. **路由分发（模块 4.1 + 4.2）**：请求命中 `POST /v1/chat/completions` → `_register_chat_completions` 里的 `chat_completions` 函数（[634-676](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L634-L676)）。先生成 `request_id`、`response_id="chatcmpl-<id>"`、`created`、`model`。
2. **请求转换（模块 4.3）**：调 `_build_chat_generate_request`（[886-982](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L886-L982)）。写下产出的 `GenerateRequest` 关键字段：
   - `sampling.temperature = 0.7`（用户显式设了 → 进入 `explicit_generation_params`）
   - `metadata["images"] = ["/data/cat.png"]`
   - `output_modalities = ["text"]`
   - `stream = True`
3. **流式分支（模块 4.4）**：因 `stream=True`，返回 `StreamingResponse(_chat_stream(...), media_type="text/event-stream")`（[652-665](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L652-L665)）。
4. **SSE 产出（模块 4.4）**：`_chat_stream`（[750-860](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/openai_api.py#L750-L860)）迭代 `client.completion_stream`，依次 yield：① 首条 `delta.role="assistant"`；② 若干 `delta.content="..."`；③ 空 delta + `finish_reason="stop"`；④ `data: [DONE]`。注意由于 `modalities=["text"]`，任何音频 chunk 都会被过滤掉。

**交付物**：一张「时间 → 函数 → 字段变化 → 所属模块」的四列表格。完成后，你应当能闭着眼睛说出一条请求在 HTTP 层的完整生命周期，并且**没有一处需要 GPU**——这印证了 HTTP 层是纯翻译搬运层这一核心结论。

---

## 6. 本讲小结

- **`create_app` 是纯 HTTP 工厂**：建 app、装中间件（CORS + 上传体积限制）、把 `client` 等放进 `app.state`、挂一串路由。它**不碰 GPU、不拉管线**，所以能用假 Client 在内存里独立测试。
- **路由分四类**：OpenAI 标准 chat、OpenAI 标准 audio（speech/transcriptions/models/health）、omni 扩展（generate/batch/ws/voices/realtime）、admin 控制。`/health` 按运行时 `running` 返回 200/503，`/v1/models` 只回吐启动时定死的单模型名。
- **`_build_chat_generate_request` 是 OpenAI→内部的翻译器**：处理字段重命名/默认值（`max_tokens`→`max_new_tokens`、`stop` 规范化）、把 `images/audios/videos` 等多模态引用塞进 `metadata`、用 `explicit_generation_params` 区分「没传」与「传了默认值」以避免静默覆盖模型默认值。
- **转换放 HTTP 层是有意为之**：把外部协议（`ChatCompletionRequest`）的脏活封死在边界，内部管线只认干净的 `GenerateRequest`，外部协议变更不污染管线。
- **SSE 流式 `_chat_stream` 遵守四条语义**：首 chunk 只发 `role`、文本/音频分离、末尾空 delta + `finish_reason`、最后 `[DONE]`；按 `requested_modalities` 过滤；失败时不发 `[DONE]`。
- **HTTP 层是纯翻译搬运层**：聚合、base64 编码都在 Client 层（`completion`/`completion_stream`），HTTP 层永远不碰 numpy/原始字节——这条不变量贯穿全讲。

## 7. 下一步学习建议

- **向内下钻**：HTTP 层把请求交给了 `Client.completion` / `completion_stream`，下一讲 [u2-l3 内部 Client 客户端层](u2-l3-client-layer.md) 就讲 `GenerateRequest` 是怎么变成 `OmniRequest`、怎么提交给 Coordinator、流式片段怎么聚合的。
- **横向补齐**：本讲没细讲的 `POST /generate`（RL rollout 接口）会在 [u6-l4 RL 权重热更新与 Admin 控制](u6-l4-rl-admin-control.md) 里结合 admin 路由一起讲；`/v1/audio/transcriptions` 的 ASR 细节在 [u7-l3 ASR 转录与说话人分离](u7-l3-asr-transcription-diarization.md)。
- **源码延伸阅读**：想看「转换函数 + 单测」如何互相佐证，强烈推荐通读一遍 [tests/unit_test/serve/test_openai_api.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/serve/test_openai_api.py)，它是本讲所有行为断言的来源。
