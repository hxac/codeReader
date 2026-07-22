# OpenAI 兼容 API 层

## 1. 本讲目标

在上一讲（u2-l3）里，我们把一条请求在三进程环（`TokenizerManager → Scheduler → DetokenizerManager`）上的端到端流转走了一遍，但当时把「HTTP 请求是怎么进来的」「OpenAI 风格的 JSON 是怎么变成内部 `GenerateReqInput` 的」当成了黑盒。本讲就打开这个黑盒。

读完本讲，你应当能够：

- 说出 `entrypoints/openai/` 这一层的职责：把 OpenAI 风格的 HTTP 端点（`/v1/chat/completions`、`/v1/completions` 等）适配成内部统一的 `GenerateReqInput`。
- 读懂 `protocol.py` 中的请求/响应数据模型（Pydantic），并理解 `temperature` / `max_tokens` / `stream` 等字段如何最终影响 `SamplingParams` 与请求流。
- 跟踪 `OpenAIServingChat` 把一条 `ChatCompletionRequest` 转换成 `GenerateReqInput`、再交给 `tokenizer_manager.generate_request` 的完整代码路径。
- 理解流式（streaming）输出基于 SSE（Server-Sent Events）的实现细节，包括 `sse_utils.build_sse_content` 如何拼出 `data: {...}\n\n` 这种数据块。

---

## 2. 前置知识

本讲假设你已经学完 u1 与 u2 的前三讲，下面这些概念会直接用到，不再重新展开：

- **三进程环**（u2-l1 / u2-l3）：`TokenizerManager`（主进程，负责分词、路由、回写）、`Scheduler`（子进程，负责 GPU 调度执行）、`DetokenizerManager`（子进程，负责把 token id 增量解码成文本）。三者经 ZMQ + msgspec 连成一个环。
- **`GenerateReqInput`**（u2-l3）：真正「上 ZMQ 通信线」之前的进程内请求结构体，是 SGLang 内部统一的「单条生成请求」表示。
- **FastAPI + uvicorn**（u1-l2 / u2-l2）：`http_server.launch_server` 在主进程套了一层 FastAPI 应用，监听默认端口 `127.0.0.1:30000`。

下面补充几个本讲要用、但前面没细讲的概念：

- **Pydantic**：Python 的数据校验库。SGLang 用 `BaseModel` 子类来描述「一个 OpenAI 请求长什么样」，FastAPI 会自动把 HTTP body 的 JSON 解析成对应的 `BaseModel` 实例，并做类型校验。这是 OpenAI 兼容层「契约」的来源。
- **OpenAI Chat Completions API**：业界事实标准的对话补全接口。请求里通常有 `messages`（多轮对话）、`model`、`temperature`、`max_tokens`、`stream` 等字段；响应是一个 `choices` 数组，每个 choice 含一条 `message`（流式时是 `delta`）。
- **SSE（Server-Sent Events）**：一种基于 HTTP 的单向流式协议。服务端持续向客户端推送形如 `data: <一段 JSON>\n\n` 的文本块，客户端逐块解析。OpenAI 的 `stream: true` 就是走 SSE。

> 关键认知：OpenAI 兼容层本身**不做模型推理**，它只是一层「翻译层 + 编排层」——把外部 OpenAI 协议翻译成内部的 `GenerateReqInput`，把内部返回的 token 流翻译回 OpenAI 协议。真正干活的是后面的 `TokenizerManager` 与调度引擎。

---

## 3. 本讲源码地图

本讲聚焦于 `python/sglang/srt/entrypoints/` 下的 OpenAI 兼容子层，涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `entrypoints/http_server.py` | FastAPI 应用本体，注册 `/v1/chat/completions`、`/v1/completions` 等路由，并把请求转发给对应的 serving handler。 |
| `entrypoints/openai/protocol.py` | OpenAI 协议的 Pydantic 数据模型：`ChatCompletionRequest`、`CompletionRequest` 及各种响应模型，并含 `to_sampling_params()` 转换方法。 |
| `entrypoints/openai/serving_base.py` | 所有 serving handler 的抽象基类 `OpenAIServingBase`，定义统一的 `handle_request` 模板方法。 |
| `entrypoints/openai/serving_chat.py` | `/v1/chat/completions` 的具体实现 `OpenAIServingChat`，把对话请求转换成 `GenerateReqInput` 并处理流式/非流式回包。 |
| `entrypoints/openai/sse_utils.py` | 流式 SSE 数据块的构建工具，用 msgspec 高性能编码。 |

此外还会顺带提到 `entrypoints/openai/serving_completions.py`（`/v1/completions`，作为对照）和 `entrypoints/openai/utils.py`（`should_include_usage` 等）。

---

## 4. 核心概念与源码讲解

本讲按「从外向内」的顺序拆成 4 个最小模块：先看 HTTP 路由与模板方法，再看协议数据模型，然后精读 chat 请求的转换，最后看流式 SSE 输出。

### 4.1 OpenAI 兼容层在请求链路中的位置

#### 4.1.1 概念说明

OpenAI 兼容层位于「HTTP 客户端」和「三进程环」之间。它的存在意义有两点：

1. **协议兼容**：让任何会用 OpenAI SDK（`openai` Python 包、各种语言的客户端）的人，无需改动代码就能直接连上 SGLang 服务。这对生态接入极其重要。
2. **职责收口**：把「OpenAI 风格的字段」与「SGLang 内部的字段」解耦。OpenAI 协议会演化、SGLang 内部也会演化，中间隔一层适配层后，两边可以独立变化。

这一层做了三件事：路由分发（哪个 URL 找哪个 handler）、模板方法（校验 → 转换 → 走流式或非流式）、错误包装。

#### 4.1.2 核心流程

一条 `/v1/chat/completions` 请求在本层内部的流转可以用下面这段伪代码概括：

```
HTTP POST /v1/chat/completions  (JSON body)
  │
  ▼  FastAPI 解析 body → ChatCompletionRequest（Pydantic 校验）
  │   + 依赖 validate_json_request 校验 content-type
  ▼
http_server.openai_v1_chat_completions(request, raw_request)
  │   # 从 app.state 取出启动时构造好的 handler
  ▼
OpenAIServingChat.handle_request(request, raw_request)   # 基类模板方法
  │   ├── _validate_request        # 业务校验（messages 非空、tools 合法等）
  │   ├── _convert_to_internal_request  # ① 转换为 GenerateReqInput + SamplingParams
  │   └── if request.stream:
  │           _handle_streaming_request   # ② 流式：返回 StreamingResponse(SSE)
  │       else:
  │           _handle_non_streaming_request  # ③ 非流式：等完整结果再返回 JSON
  ▼
tokenizer_manager.generate_request(adapted_request, raw_request)   # ← 进入三进程环（u2-l3）
```

注意第 ②、③ 步里，本层都会调用 `self.tokenizer_manager.generate_request(...)`，也就是说**从这里开始就交棒给了上一讲讲过的三进程环**。本讲聚焦的是交棒之前与之后的那段「翻译」。

#### 4.1.3 源码精读

先看路由注册。FastAPI 用 `@app.post(path)` 装饰器声明路由，`request: ChatCompletionRequest` 参数让 FastAPI 自动把 body 解析并校验成 Pydantic 对象：

[http_server.py:1649-1656](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/http_server.py#L1649-L1656) 注册 `/v1/chat/completions`，函数体只有一行：从 `app.state` 取出启动时构造的 `openai_serving_chat` handler 并调用 `handle_request`。

```python
@app.post("/v1/chat/completions", dependencies=[Depends(validate_json_request)])
async def openai_v1_chat_completions(
    request: ChatCompletionRequest, raw_request: Request
):
    """OpenAI-compatible chat completion endpoint."""
    return await raw_request.app.state.openai_serving_chat.handle_request(
        request, raw_request
    )
```

这里的 `validate_json_request` 是一个 FastAPI 依赖，强制要求 `content-type: application/json`，见 [http_server.py:587-600](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/http_server.py#L587-L600)。

而 `app.state.openai_serving_chat` 这个 handler，是在服务启动阶段构造并挂到 `app.state` 上的（u2-l2 讲过的 `launch_server` 流程里）：

[http_server.py:301-305](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/http_server.py#L301-L305) 启动时构造 `OpenAIServingChat`（或其子类 `serving_chat_class`）并挂到 `app.state.openai_serving_chat`。

每个具体的 handler 类都继承自同一个抽象基类 `OpenAIServingBase`，它把「校验 → 转换 → 分流式/非流式」这套公共流程写成一个**模板方法** `handle_request`：

[serving_base.py:73-109](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_base.py#L73-L109) 定义了模板方法。核心是三步：校验、转换、按 `stream` 字段分流。

```python
async def handle_request(self, request, raw_request):
    received_time = monotonic_time()
    try:
        error_msg = self._validate_request(request)        # ① 校验
        if error_msg:
            return self.create_error_response(error_msg)
        ...
        adapted_request, processed_request = (
            self._convert_to_internal_request(request, raw_request)  # ② 转换
        )
        ...
        if hasattr(request, "stream") and request.stream:             # ③ 分流
            return await self._handle_streaming_request(...)
        else:
            return await self._handle_non_streaming_request(...)
```

这是一个典型的**模板方法模式（Template Method）**：基类把骨架写死，把可变步骤（`_convert_to_internal_request`、`_handle_streaming_request` 等）声明为抽象方法或可重写的钩子，由子类（`OpenAIServingChat`、`OpenAIServingCompletion`、`OpenAIServingEmbedding` 等）各自实现。这样所有端点共享同一套错误处理、计时、content-type 校验逻辑。

错误处理也集中在基类：`handle_request` 用 `try/except` 兜住 `HTTPException`、`ValueError`、`DS32EncodingError` 以及兜底的 `Exception`，统一通过 `create_error_response` 转成带状态码的 JSON 响应，见 [serving_base.py:209-225](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_base.py#L209-L225)。

#### 4.1.4 代码实践

**实践目标**：在源码中亲手把「URL → handler → 基类模板方法」这条链对上号。

**操作步骤**：

1. 打开 `python/sglang/srt/entrypoints/http_server.py`，分别定位 `/v1/chat/completions`（约 L1649）、`/v1/completions`（约 L1641）、`/v1/embeddings`（约 L1659）。
2. 观察它们函数体的写法几乎一模一样：`return await raw_request.app.state.<handler>.handle_request(request, raw_request)`。
3. 在同一文件搜索 `app.state.openai_serving_` 的赋值（约 L298 起），确认这些 handler 都在启动阶段一次性构造好。
4. 打开 `serving_base.py` 的 `handle_request`（L73），确认校验、转换、分流三步的顺序。

**需要观察的现象**：路由函数本身是「无脑转发」，所有真正逻辑都在 handler 类里；多个端点共享同一个 `handle_request` 模板。

**预期结果**：你能用一句话说出「新增一个 OpenAI 兼容端点需要做哪几件事」——写 Pydantic 请求模型、写 handler 子类、在 `http_server` 注册路由、在启动时挂到 `app.state`。

#### 4.1.5 小练习与答案

**练习 1**：如果客户端发来的 `content-type` 不是 `application/json`，会在哪一步被拒绝？返回什么？

> **答案**：在路由的 `Depends(validate_json_request)` 依赖里就被拒绝（[http_server.py:587](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/http_server.py#L587)），抛出 `RequestValidationError`，FastAPI 会返回 422 Unprocessable Entity。这发生在 `handle_request` 之前。

**练习 2**：`handle_request` 是定义在 `OpenAIServingChat` 里还是基类里？为什么这样设计？

> **答案**：定义在基类 `OpenAIServingBase` 里（[serving_base.py:73](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_base.py#L73)）。这样所有端点（chat/completions/embedding/…）共享同一套校验→转换→分流的骨架与错误兜底，避免每个子类重复写这段流程；子类只需覆盖可变步骤。

---

### 4.2 protocol.py：请求/响应数据模型

#### 4.2.1 概念说明

`protocol.py` 是 OpenAI 兼容层的「契约中心」：它用 Pydantic 定义了所有端点的请求与响应结构。它有两个核心作用：

1. **入参校验**：FastAPI 拿到 JSON body 后，会按这些模型做类型校验，字段类型不对或必填缺失会直接 422。
2. **协议映射**：`ChatCompletionRequest` 提供了一个关键方法 `to_sampling_params()`，把 OpenAI 的采样相关字段翻译成 SGLang 内部引擎认识的采样参数字典。

这一层同时承载了「OpenAI 原生字段」与「SGLang 扩展字段」。`protocol.py` 里大量注释会标注 `# Ordered by official OpenAI API documentation`，意思是前面这段字段严格对齐 OpenAI 官方文档顺序；之后还有 `# Extra parameters for SRT backend only`，这些是 SGLang 自己扩展的（如 `top_k`、`min_p`、`regex`、`ebnf`、`lora_path` 等），不会被 OpenAI 官方模型识别，但对 SGLang 后端有用。

#### 4.2.2 核心流程

我们重点关注 `ChatCompletionRequest` 的字段语义，以及它如何映射成采样参数。先看请求模型中几个关键字段的默认值：

| OpenAI 字段 | 类型 / 默认值 | 含义 |
| --- | --- | --- |
| `messages` | `List[...]`（必填） | 多轮对话消息列表。 |
| `model` | `str = "default"` | 模型名，支持 `base-model:adapter-name` 语法指定 LoRA。 |
| `temperature` | `Optional[float] = None` | 采样温度，`None` 表示「用模型 generation_config 的默认值」。 |
| `top_p` | `Optional[float] = None` | nucleus sampling，同上 `None` 表示回退默认。 |
| `max_tokens` | `Optional[int] = None`（已 deprecated） | 旧字段，OpenAI 建议改用 `max_completion_tokens`。 |
| `max_completion_tokens` | `Optional[int] = None` | 新字段，生成的最大 token 数。 |
| `stream` | `bool = False` | 是否流式。决定走 4.1 里的 ② 还是 ③。 |
| `stream_options` | `Optional[StreamOptions]` | 流式选项，如是否在末尾带 usage。 |
| `top_k` / `min_p` / `regex` / `ebnf` | SGLang 扩展 | 额外的采样/约束解码控制。 |

注意一个关键设计：`ChatCompletionRequest` 里 `temperature`/`top_p` 的默认值是 `None`，**而不是 OpenAI 官方的 `1.0`**。这是有意的——为了支持「三级回退优先级」：

```
用户显式传值  >  模型 generation_config 默认值  >  OpenAI 官方默认值
```

这个优先级在 `to_sampling_params` 里实现：

[protocol.py:952-969](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/protocol.py#L952-L969) 定义了带优先级回退的转换方法，内部的 `get_param` 实现了三级回退逻辑：

```python
def to_sampling_params(self, stop, model_generation_config, tool_call_constraint=None):
    def get_param(param_name: str):
        value = getattr(self, param_name)
        if value is None:
            return model_generation_config.get(
                param_name, self._DEFAULT_SAMPLING_PARAMS[param_name]
            )
        return value
    ...
```

也就是说：用户传了 `temperature` 就用用户的；没传（`None`）就看模型 `generation_config` 里有没有；再没有就用 `_DEFAULT_SAMPLING_PARAMS`（即 OpenAI 官方默认）。

`max_tokens` / `max_completion_tokens` 的合并也在这里，优先取新字段：

[protocol.py:980](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/protocol.py#L980)

```python
"max_new_tokens": self.max_completion_tokens or self.max_tokens,
```

> 小细节：`max_tokens` 在 `ChatCompletionRequest` 里被标记为 `deprecated`（[protocol.py:719-723](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/protocol.py#L719-L723)），而旧的 `CompletionRequest` 里它仍是主字段、默认 `16`（[protocol.py:330](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/protocol.py#L330)）。这就是为什么 OpenAI 把 chat 的 token 上限字段改名了，而 completion 端点保留旧名。

`stream` 字段则不在 `to_sampling_params` 里处理——它影响的是「请求走哪条返回路径」，被直接透传进 `GenerateReqInput` 的 `stream` 字段（见 4.3）。

`to_sampling_params` 还会处理结构化输出约束：如果 `response_format.type` 是 `json_schema` / `json_object` / `structural_tag`，会把它转换成采样参数里的对应约束键，见 [protocol.py:1003-1012](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/protocol.py#L1003-L1012)。这部分与 u6（结构化输出）相关，这里先知道有这么个出口即可。

#### 4.2.3 源码精读

请求模型本体在 [protocol.py:707-713](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/protocol.py#L707-L713) 定义 `class ChatCompletionRequest(BaseModel)`，开头注释 `# Ordered by official OpenAI API documentation`。

OpenAI 默认采样参数集中在 [protocol.py:831-837](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/protocol.py#L831-L837) 的 `_DEFAULT_SAMPLING_PARAMS`：

```python
_DEFAULT_SAMPLING_PARAMS = {
    "temperature": 1.0,
    "top_p": 1.0,
    "top_k": -1,
    "min_p": 0.0,
    "repetition_penalty": 1.0,
}
```

这是上面 `get_param` 三级回退的最底层默认。

请求模型里还有不少 `@model_validator(mode="before")`，它们在 Pydantic 构造对象**之前**就介入，做字段归一化。比如 [protocol.py:844-852](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/protocol.py#L844-L852) 的 `set_tool_choice_default` 会在用户没传 `tool_choice` 时，根据有没有 `tools` 自动补成 `"none"` 或 `"auto"`：

```python
@model_validator(mode="before")
@classmethod
def set_tool_choice_default(cls, values):
    if values.get("tool_choice") is None:
        if values.get("tools") is None:
            values["tool_choice"] = "none"
        else:
            values["tool_choice"] = "auto"
    return values
```

另一个值得看的是 [protocol.py:861-918](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/protocol.py#L861-L918) 的 `normalize_reasoning_inputs`：它把 OpenAI 新的嵌套 `reasoning.effort` 字段，以及各模型各自检查的 `thinking` / `enable_thinking` chat template 开关统一归一化——这是「兼容多种推理模型（DeepSeek/Qwen/GLM 等）」的关键适配点。

响应模型方面，非流式是 `ChatCompletionResponse`（[protocol.py:1073-1088](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/protocol.py#L1073-L1088)），流式是 `ChatCompletionStreamResponse`（[protocol.py:1118-1132](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/protocol.py#L1118-L1132)），后者用 `delta: DeltaMessage` 表示增量。注意 `DeltaMessage` 定义在 [protocol.py:1091-1103](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/protocol.py#L1091-L1103)，含 `role`/`content`/`reasoning_content`/`tool_calls` 字段——这与 4.4 里 `sse_utils` 的 `StreamDelta` 是两套（一套是 Pydantic 用于完整对象序列化，一套是 msgspec 用于高性能流式编码）。

此外，`protocol.py` 还为多模态消息内容定义了一组 `ChatCompletionMessageContent*Part`（图片/视频/音频/思考片段等），见 [protocol.py:489-559](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/protocol.py#L489-L559)，最终汇成 `ChatCompletionMessageContentPart` 联合类型，让一条 `content` 既可以是纯字符串，也可以是多种模态片段的列表。

#### 4.2.4 代码实践

**实践目标**：对照 OpenAI 官方 chat completions 字段表，找到 SGLang 里的对应实现，并验证 `temperature` / `max_tokens` / `stream` 的回退与透传行为。

**操作步骤（源码阅读型）**：

1. 打开 [protocol.py 的 `ChatCompletionRequest`](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/protocol.py#L707-L713)，对照 OpenAI 官方文档 [Create chat completion](https://platform.openai.com/docs/api-reference/chat/create)，逐个字段对一遍。
2. 找到 `temperature`（L736）、`top_p`（L737）、`max_tokens`（L719）、`max_completion_tokens`（L724）、`stream`（L734）。
3. 进入 `to_sampling_params`（L952），追踪这三个字段各自的去向：
   - `temperature` → 经 `get_param` 三级回退 → 进入返回字典的 `"temperature"`。
   - `max_tokens` / `max_completion_tokens` → 合并成 `"max_new_tokens"`（L980）。
   - `stream` → **不进** `to_sampling_params`，而是在 4.3 里直接透传到 `GenerateReqInput(stream=...)`。
4. 在 `protocol.py` 里搜索 `_DEFAULT_SAMPLING_PARAMS`（L831），确认最底层默认值。

**需要观察的现象**：`temperature` 在请求模型里默认 `None`，但 SGLang 实际推理时绝不会用 `None`——总会在 `to_sampling_params` 里被某一级默认值替换掉。

**预期结果**：你能画出一张「字段 → 采样参数」的映射表，并解释为什么 SGLang 把默认值设成 `None` 而不是直接写 `1.0`（答案：为了让模型的 `generation_config` 能优先于 OpenAI 官方默认生效）。

> 待本地验证：如果你想确认「模型 generation_config 默认值」这一级确实生效，可以在启动某个模型后，看服务日志里是否有 `Using default chat sampling params from model generation config: ...` 这一行（这条日志在 `serving_chat.py` 的 `__init__` 里打印，见 [serving_chat.py:202-205](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L202-L205)）。

#### 4.2.5 小练习与答案

**练习 1**：用户没传 `temperature`，模型 `generation_config` 里也没有 `temperature`，最终推理用的是什么值？

> **答案**：`_DEFAULT_SAMPLING_PARAMS["temperature"]` = `1.0`。三级回退的最后一级（[protocol.py:963-969](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/protocol.py#L963-L969)）。

**练习 2**：`stream: true` 会影响 `SamplingParams` 吗？它影响的是什么？

> **答案**：不会影响 `SamplingParams`。`stream` 不经过 `to_sampling_params`，它被透传到 `GenerateReqInput.stream`，决定的是返回路径（走 SSE 流式还是等完整结果返回 JSON），见 4.1 的 ②③ 分流。

**练习 3**：为什么 `max_tokens` 在 `ChatCompletionRequest` 里被标 `deprecated`，但在 `CompletionRequest` 里还是主字段？

> **答案**：OpenAI 在 chat 端点用 `max_completion_tokens` 取代了 `max_tokens`（语义更清晰：包含 reasoning token），但旧的 `/v1/completions` 端点保留了原字段名以维持兼容。SGLang 跟随了官方的演化方向。

---

### 4.3 serving_chat：ChatCompletion 到 GenerateReqInput 的转换

#### 4.3.1 概念说明

`OpenAIServingChat` 是 `/v1/chat/completions` 的具体实现，也是本讲最核心的一个类。它要做的事，概括成一句：

> 把一个 `ChatCompletionRequest`（多轮对话 + OpenAI 字段）加工成一个 `GenerateReqInput`（内部统一请求），并决定流式还是非流式回包。

这个加工过程涉及几件具体的事：

- 把多轮 `messages` 渲染成一段文本 prompt（应用 chat template，类似 HF 的 `apply_chat_template`），或直接用用户给的 `input_ids`。
- 调用 `to_sampling_params` 得到采样参数字典。
- 抽取多模态数据（图片/视频/音频）。
- 解析 LoRA 适配器、DP 路由、各种扩展字段。
- 把以上一切组装进 `GenerateReqInput`。

#### 4.3.2 核心流程

转换发生在重写的 `_convert_to_internal_request` 里，流程如下：

```
_convert_to_internal_request(request)
  │
  ├── _process_messages(request)                # 渲染 chat template + 抽多模态 + 算 tool_call_constraint
  │      返回 MessageProcessingResult(prompt, prompt_ids, image_data, stop, ...)
  │
  ├── request.to_sampling_params(stop, default_sampling_params, tool_call_constraint)
  │      # 4.2 讲过的三级回退，得到 sampling_params dict
  │
  ├── 选择 prompt_kwargs：text / input_ids / 多模态
  │
  ├── extract_custom_labels / extract_routed_dp_rank_from_header / _resolve_lora_path
  │      # 从 HTTP header / model 字段解析各种扩展
  │
  └── 组装 GenerateReqInput(**prompt_kwargs, sampling_params=..., stream=..., ...)
```

之后，`handle_request` 根据是否 `stream` 调用流式或非流式处理函数，二者最终都会调用 `self.tokenizer_manager.generate_request(adapted_request, raw_request)`——这就交棒给 u2-l3 的三进程环了。

#### 4.3.3 源码精读

先看类定义与构造，[serving_chat.py:161-172](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L161-L172)：

```python
class OpenAIServingChat(OpenAIServingBase):
    """Handler for /v1/chat/completions requests"""

    def __init__(self, tokenizer_manager, template_manager):
        super().__init__(tokenizer_manager)
        self.template_manager = template_manager
        self.tool_call_parser = self.tokenizer_manager.server_args.tool_call_parser
        self.reasoning_parser = self.tokenizer_manager.server_args.reasoning_parser
        ...
```

注意它持有 `tokenizer_manager` 和 `template_manager` 两个协作者：前者是通向三进程环的入口，后者负责 chat template 渲染。构造时还顺带从 `server_args` 读取了 `tool_call_parser` / `reasoning_parser`（工具调用与思维链解析器，与 u6 相关），以及模型的默认采样参数。

转换方法是核心，[serving_chat.py:661-700](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L661-L700)。先做消息处理与采样参数计算：

```python
def _convert_to_internal_request(self, request, raw_request=None):
    ...
    is_multimodal = self.tokenizer_manager.model_config.is_multimodal
    processed_messages = self._process_messages(request, is_multimodal)
    sampling_params = request.to_sampling_params(
        stop=processed_messages.stop,
        model_generation_config=self.default_sampling_params,
        tool_call_constraint=processed_messages.tool_call_constraint,
    )
    ...
```

这里 `_process_messages`（[serving_chat.py:779-861](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L779-L861)）负责把 `messages` 渲染成 prompt：如果用户提供了 `input_ids` 就直接用；否则按是否指定 chat template 走 Jinja 渲染或 conversation 渲染。它同时抽取图片/音频/视频数据，并算出 `tool_call_constraint`（工具调用约束，会传给 `to_sampling_params` 影响结构化输出）。

接着根据是否多模态、是否有预计算 `input_ids`，选择 prompt 的传入形式（`text` 或 `input_ids`），见 [serving_chat.py:703-723](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L703-L723)。

最后把一切组装进 `GenerateReqInput`，[serving_chat.py:740-775](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L740-L775)。只看关键字段：

```python
adapted_request = GenerateReqInput(
    **prompt_kwargs,                         # text 或 input_ids
    image_data=processed_messages.image_data,
    sampling_params=sampling_params,         # 上面算出的采样参数
    return_logprob=request.logprobs,
    stream=request.stream,                   # ← stream 在这里透传
    lora_path=lora_path,
    routed_dp_rank=effective_routed_dp_rank,
    rid=request.rid,
    priority=request.priority,
    ...
)
```

注意 `stream=request.stream`——这就是 4.2 里说的「`stream` 不影响采样参数，只透传到 `GenerateReqInput`」的落点。

交棒给三进程环的地方在两个 handler 里。非流式：[serving_chat.py:1437-1460](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L1437-L1460)，关键一行是 `await self.tokenizer_manager.generate_request(...).__anext__()`——取第一个（也是唯一一个）完整结果，再交给 `_build_chat_response` 包装成 OpenAI 响应：

```python
async def _handle_non_streaming_request(self, adapted_request, request, raw_request):
    try:
        ret = await self.tokenizer_manager.generate_request(
            adapted_request, raw_request
        ).__anext__()
    except ValueError as e:
        return self.create_error_response(str(e))
    if not isinstance(ret, list):
        ret = [ret]
    response = self._build_chat_response(request, ret, int(time.time()))
    return response
```

流式：[serving_chat.py:1170-1196](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L1170-L1196)。这里有个**值得学习的工程技巧**：它会先 `await generator.__anext__()` 拉出第一个 chunk，再包进 `prepend_first_chunk` 里返回。注释解释了原因——为了让校验错误（比如上下文超长）能在 HTTP 200 发送**之前**抛出，从而返回一个正常的 HTTP 400，而不是已经发了 200 再把错误塞进 SSE 流里：

```python
async def _handle_streaming_request(self, adapted_request, request, raw_request):
    generator = self._generate_chat_stream(adapted_request, request, raw_request)
    try:
        first_chunk = await generator.__anext__()   # 提前触发校验
    except ValueError as e:
        return self.create_error_response(str(e))   # 还能返回正常的 400
    async def prepend_first_chunk():
        yield first_chunk
        async for chunk in generator:
            yield chunk
    return StreamingResponse(
        prepend_first_chunk(),
        media_type="text/event-stream",
        background=self.tokenizer_manager.create_abort_task(adapted_request),
    )
```

（`background=...` 是在客户端断连时自动 abort 请求的清理任务，承接 u2-l3 里讲过的 `AbortReq` 取消机制。）

#### 4.3.4 代码实践

**实践目标**：在源码里完整走一遍「`ChatCompletionRequest` → `GenerateReqInput`」的转换，并标注每个关键字段的来源。

**操作步骤（源码阅读型 / 调用链跟踪）**：

1. 从 [http_server.py:1649](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/http_server.py#L1649) 的 `/v1/chat/completions` 出发。
2. 进 [serving_base.py:73](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_base.py#L73) 的 `handle_request`。
3. 进 [serving_chat.py:661](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L661) 的 `_convert_to_internal_request`，依次标注：
   - `sampling_params` 来自 `request.to_sampling_params(...)`（L696）。
   - `stream` 透传自 `request.stream`（L749）。
   - `lora_path` 来自 `_resolve_lora_path(request.model, request.lora_path)`（L734）。
   - `routed_dp_rank` 来自 HTTP header 或 body（L729）。
4. 走到 [serving_chat.py:740](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L740) 的 `GenerateReqInput(...)` 构造点。
5. 最后到 [serving_chat.py:1445](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L1445)（非流式）或 [serving_chat.py:1235](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L1235)（流式）的 `tokenizer_manager.generate_request(...)`，确认交棒点。

**需要观察的现象**：转换函数是纯「数据搬运 + 字段映射」，不触碰 GPU；真正的推理在交棒之后才开始。

**预期结果**：你能画出一张表，左列是 `GenerateReqInput` 的关键字段，右列是它在 `ChatCompletionRequest`（或 HTTP header / server_args）里的来源。

#### 4.3.5 小练习与答案

**练习 1**：`_convert_to_internal_request` 是 `OpenAIServingChat` 自己实现的，还是继承自基类？为什么？

> **答案**：自己实现的（重写了基类的抽象方法，[serving_chat.py:661](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L661)；基类在 [serving_base.py:164](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_base.py#L164) 声明为 `@abstractmethod`）。因为每个端点（chat / completion / embedding）的「OpenAI 字段 → 内部请求」映射规则不同，必须各自实现，但骨架（校验→转换→分流）由基类统一。

**练习 2**：流式 handler 为什么要先 `await generator.__anext__()` 拉第一个 chunk 再返回 `StreamingResponse`？

> **答案**：为了在发送 HTTP 200 之前触发校验，让上下文超长这类错误能以正常的 HTTP 400 返回，而不是发完 200 再把错误塞进 SSE 流（客户端难以处理）。见 [serving_chat.py:1179-1196](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L1179-L1196) 的注释。

---

### 4.4 sse_utils：流式 SSE 输出

#### 4.4.1 概念说明

当 `stream: true` 时，OpenAI 协议要求服务端用 SSE 持续推送数据块。SSE 的格式很简单，每个事件是：

```
data: <一段 JSON>\n\n
```

即 `data: ` 前缀 + JSON + 两个换行。客户端逐块读取，遇到 `data: ` 就解析后面的 JSON。

SGLang 把「构造这种 SSE 块」的逻辑单独抽到 `sse_utils.py`，并且**没有用 Pydantic**，而是用了 `msgspec.Struct`——因为流式输出对每个 token 都要编码一次，性能极其敏感，msgspec 比 Pydantic 快得多。这就是为什么流式有一套独立的 `StreamDelta` / `StreamChoice` / `StreamChunk`，而不是复用 `protocol.py` 里的 `DeltaMessage` 等。

#### 4.4.2 核心流程

一个流式 chat 响应在 SGLang 里由若干 SSE 块组成，大致顺序是：

```
1. 首块：delta.role = "assistant", content = ""     # 告诉客户端角色
2. 内容块（若干）：delta.content = "一段文本"        # 每个 token/批次一个
   （若有 reasoning_parser：先推 reasoning_content 块）
   （若有 tool_call_parser：推 tool_calls 增量块）
3. 结束块：finish_reason = "stop"/"length"/"tool_calls"
4. （可选）usage 块：stream_options.include_usage=true 时最后带 token 用量
```

每个块都由 `build_sse_content(...)` 拼成 `data: {...}\n\n` 字符串，然后 `yield` 出去。`_generate_chat_stream` 是一个异步生成器，外层 `StreamingResponse` 把它包成 HTTP 流式响应。

delta（增量）的计算也有讲究：除非开了 `incremental_streaming_output`，否则每次拿到的是「到目前为止的完整文本」，需要用 `stream_offsets[index]` 记录上次发到哪里，本次只发增量部分。

#### 4.4.3 源码精读

先看 `sse_utils.py` 的数据结构，[sse_utils.py:13-46](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/sse_utils.py#L13-L46)：

```python
class StreamDelta(msgspec.Struct, omit_defaults=True):
    reasoning_content: Optional[str]
    role: Optional[str] = None
    content: Optional[str] = None

class StreamChoice(msgspec.Struct):
    index: int
    delta: StreamDelta
    logprobs: Optional[dict] = None
    finish_reason: Optional[str] = None
    matched_stop: Union[None, int, str] = None

class StreamChunk(msgspec.Struct, omit_defaults=True):
    id: str
    object: str
    created: int
    model: str
    choices: List[StreamChoice]
    usage: Optional[dict] = None
```

注意 `StreamDelta` 的注释（[sse_utils.py:14-21](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/sse_utils.py#L14-L21)）解释了一个微妙的设计：`reasoning_content` 故意**没有默认值**（必填）。因为 `omit_defaults=True` 时，如果它默认 `None`，就会被整个字段丢掉，导致客户端 SDK 读 `data.reasoning_content` 时抛 `AttributeError`。所以这里让它必填，序列化成 `null` 或字符串。这是一个「兼容 OpenAI SDK 行为」的细节陷阱。

核心构建函数在 [sse_utils.py:52-99](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/sse_utils.py#L52-L99)，它把传入的 `role`/`content`/`reasoning_content`/`finish_reason`/`usage` 等组装成一个 SSE 字符串：

```python
def build_sse_content(chunk_id, created, model, index, role=None, content=None,
                      reasoning_content=None, finish_reason=None, ...):
    delta = StreamDelta(role=role, content=content, reasoning_content=reasoning_content)
    choice = StreamChoice(index=index, delta=delta, ...)
    chunk = StreamChunk(id=chunk_id, object="chat.completion.chunk",
                        created=created, model=model, choices=[choice], usage=usage)
    return (_SSE_DATA_B + _stream_encoder.encode(chunk) + _SSE_NL_B).decode()
```

最末一行就是 SSE 格式的核心：`b"data: "` + JSON 字节 + `b"\n\n"`，再 decode 成字符串。`_stream_encoder` 是模块级的单例 `msgspec.json.Encoder()`（[sse_utils.py:49](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/sse_utils.py#L49)），复用编码器避免每次重建。

调用方在 `serving_chat.py` 的 `_generate_chat_stream` 里。先发首块（带 role），[serving_chat.py:1295-1304](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L1295-L1304)：

```python
if is_firsts.get(index, True):
    is_firsts[index] = False
    yield build_sse_content(
        chunk_id=content["meta_info"]["id"], created=int(time.time()),
        model=request.model, index=index, role="assistant", content="",
    )
```

然后每个迭代步调 `_generate_stream_content` 产出内容/reasoning/tool_call 块，[serving_chat.py:1308-1323](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L1308-L1323)。`_generate_stream_content` 内部对普通内容块的产出见 [serving_chat.py:566-574](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L566-L574)：

```python
yield build_sse_content(
    chunk_id=content["meta_info"]["id"], created=int(time.time()),
    model=request.model, index=index, content=delta, logprobs=remaining_logprobs,
    usage=usage,
)
```

最后发结束块，[serving_chat.py:1335-1342](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L1335-L1342)，带 `finish_reason`（若是工具调用则改成 `"tool_calls"`）。

> 对照：`/v1/completions` 端点（`serving_completions.py`）没有用 `build_sse_content`，而是直接用 Pydantic 的 `CompletionStreamResponse(...).model_dump_json()` 拼 `f"data: {json}\n\n"`（见 [serving_completions.py:432-440](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_completions.py#L432-L440)）。两条路径格式一致（都是 `data: ...\n\n`），但 chat 走了高性能的 msgspec 路径，completion 走了 Pydantic 路径——这也反映了 chat 端点是主力、对延迟更敏感。

#### 4.4.4 代码实践

**实践目标**：亲手发一个流式 chat 请求，观察 SSE 原始字节流，并把它和源码里的 `build_sse_content` 调用对应起来。

**操作步骤**：

1. 启动一个小模型服务（承接 u1-l2）：
   ```bash
   sglang serve --model-path <小模型> --port 30000
   ```
2. 用 `curl` 的 `--no-buffer` 发流式请求，直接看原始 SSE 字节：
   ```bash
   curl -N http://127.0.0.1:30000/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"default","messages":[{"role":"user","content":"用一句话介绍你自己"}],"stream":true}'
   ```
3. 观察输出，你会看到一连串形如 `data: {"id":"chatcmpl-...","object":"chat.completion.chunk",...}` 的块，最后通常有一个 `data: [DONE]`（或以 usage 块结尾）。
4. 在输出里找出：
   - 第一个块：`delta.role == "assistant"`、`delta.content == ""`（对应 [serving_chat.py:1297](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L1297) 的首块）。
   - 中间若干块：`delta.content` 是文本片段（对应 [serving_chat.py:566](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L566) 的内容块）。
   - 最后一个 choice 块：`finish_reason == "stop"`（对应 [serving_chat.py:1335](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L1335) 的结束块）。

**需要观察的现象**：每个块都以 `data: ` 开头、`\n\n` 结尾；块的 JSON 结构与 `sse_utils.py` 里 `StreamChunk` 的字段一一对应。

**预期结果**：你能把 `curl` 看到的某一块原文，逐字段对到 `StreamChunk` / `StreamChoice` / `StreamDelta` 的定义上。

> 待本地验证：不同模型/不同 `stream_options` 下，末尾是否出现 usage 块、是否出现 `data: [DONE]` 可能有差异，以实际服务行为为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么流式用 `msgspec.Struct` 而不是复用 `protocol.py` 里的 Pydantic 模型？

> **答案**：性能。流式每个 token/批次都要序列化一次，msgspec 的 JSON 编码比 Pydantic 快得多；模块级复用 `_stream_encoder` 单例进一步降低开销。非流式只在请求结束时序列化一次，用 Pydantic 无所谓。

**练习 2**：`StreamDelta.reasoning_content` 为什么不设默认值（必填）？

> **答案**：因为 `StreamChunk`/`StreamDelta` 用了 `omit_defaults=True`。若 `reasoning_content` 默认 `None`，序列化时会整个丢掉这个键，导致 OpenAI SDK 客户端读 `data.reasoning_content` 时抛 `AttributeError`。让它必填，序列化成 `null`，客户端就能安全读到。见 [sse_utils.py:14-21](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/sse_utils.py#L14-L21) 注释。

**练习 3**：一个流式 chat 响应至少会发哪几种 SSE 块？

> **答案**：至少三种——首块（`role="assistant"`，空 content）、若干内容块（`delta.content` 非空）、结束块（`finish_reason` 非 null）。若开启 `stream_options.include_usage`，末尾还会多一个 usage 块。

---

## 5. 综合实践

把本讲的四个模块串起来，完成下面这个「全链路阅读 + 实跑」任务：

**任务**：跟踪一条带 `temperature`、`max_tokens`、`stream` 三个字段的 chat 请求，从 HTTP 进入到第一个 SSE 块产出，标注每个字段在每一站的形态。

**步骤**：

1. **准备请求**。写一个最小的请求 JSON：
   ```json
   {
     "model": "default",
     "messages": [{"role": "user", "content": "你好"}],
     "temperature": 0.3,
     "max_tokens": 20,
     "stream": true
   }
   ```
2. **第一站：路由与校验**（对应 4.1）。在 [http_server.py:1649](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/http_server.py#L1649) 确认 FastAPI 把 body 解析成 `ChatCompletionRequest`，并记录此时 `request.temperature=0.3`、`request.stream=True`。
3. **第二站：转换**（对应 4.2 + 4.3）。在 [serving_chat.py:696](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L696) 的 `to_sampling_params` 处，回答：
   - 因为 `temperature=0.3` 非 None，`get_param("temperature")` 直接返回 `0.3`（不触发回退）。
   - `max_new_tokens` 取 `max_completion_tokens or max_tokens` = `20`（[protocol.py:980](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/protocol.py#L980)）。
   - `stream` 不进采样参数，透传到 `GenerateReqInput(stream=True)`（[serving_chat.py:749](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L749)）。
4. **第三站：交棒三进程环**（承接 u2-l3）。在 [serving_chat.py:1235](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L1235) 确认 `tokenizer_manager.generate_request` 被调用——从这里开始就是上一讲的三进程环了，本层不再关心。
5. **第四站：SSE 产出**（对应 4.4）。实跑这个请求（用 4.4.4 的 curl 命令），抓取前两个 SSE 块，在源码里定位：
   - 首块来自 [serving_chat.py:1297](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L1297)（`role="assistant"`）。
   - 内容块来自 [serving_chat.py:566](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/openai/serving_chat.py#L566) 的 `build_sse_content`。
6. **产出**：画一张时序图，横轴是「路由 → handle_request → _convert_to_internal_request → to_sampling_params → GenerateReqInput → generate_request → SSE 块」，在每个节点上标注 `temperature` / `max_tokens` / `stream` 三个字段当时的值或去向。

**预期结果**：你能清楚说出，这三个 OpenAI 字段里，`temperature` 和 `max_tokens` 最终落进了 `SamplingParams`（影响的是「怎么采样/生成多少」），而 `stream` 完全不影响采样、只决定了「结果怎么返回给客户端」。这正是 OpenAI 兼容层「翻译」工作的精髓。

---

## 6. 本讲小结

- OpenAI 兼容层（`entrypoints/openai/`）是一层**翻译 + 编排层**：把 OpenAI 协议的 HTTP 请求适配成内部统一的 `GenerateReqInput`，把内部 token 流翻译回 OpenAI 协议，本身不做推理。
- 路由函数（`http_server.py` 里的 `openai_v1_chat_completions` 等）只做无脑转发，真正逻辑在各 handler 类里；所有 handler 共享基类 `OpenAIServingBase` 的模板方法 `handle_request`（校验 → 转换 → 按流式分流）。
- `protocol.py` 用 Pydantic 定义请求/响应契约；`ChatCompletionRequest.to_sampling_params()` 实现了「用户传值 > 模型 generation_config > OpenAI 默认」的三级回退，决定了 `temperature`/`top_p` 的最终值。
- `OpenAIServingChat._convert_to_internal_request` 把对话渲染成 prompt、算出采样参数、抽取多模态与扩展字段，组装成 `GenerateReqInput`，再交棒给 `tokenizer_manager.generate_request`（进入 u2-l3 的三进程环）。
- 流式输出走 SSE，每个块是 `data: {JSON}\n\n`；`sse_utils.build_sse_content` 用 msgspec 高性能编码，`StreamChunk`/`StreamDelta` 是独立于 Pydantic 的一套结构，`reasoning_content` 故意必填以兼容 OpenAI SDK。
- `stream` 字段不影响 `SamplingParams`，只决定返回路径（SSE 流式 vs 完整 JSON）；流式 handler 会先拉第一个 chunk 以便把校验错误在 HTTP 200 之前以 400 返回。

---

## 7. 下一步学习建议

本讲把「HTTP 请求 → `GenerateReqInput`」这一段讲透了，并把交棒点（`tokenizer_manager.generate_request`）留给了上一讲。接下来建议：

- **如果想往「请求进引擎后怎么调度」走**：进入 u3（调度器与连续批处理），看 `GenerateReqInput` 进入 `TokenizerManager` 后，`Scheduler` 如何把它组成批、分配 KV、驱动前向。本讲的 `sampling_params` 字段会在 u6（采样与结构化输出）里被 `Sampler` 真正消费。
- **如果想往「结构化输出 / 工具调用」走**：本讲多次提到 `response_format`、`tool_call_constraint`、`regex`/`ebnf`，这些约束最终在 u6-l3（结构化输出与文法后端）落地，可以带着本讲的「字段在 `to_sampling_params` 里被翻译成 `json_schema`/`structural_tag` 键」这个认知去读 `constrained/` 目录。
- **如果想往「其他端点」走**：对照阅读 `serving_completions.py`、`serving_embedding.py`、`serving_responses.py`，它们都继承自 `OpenAIServingBase`，结构同构，区别只在 `_convert_to_internal_request` 的字段映射。`serving_responses.py`（`/v1/responses`）是较新的 OpenAI Responses API，值得单独研究。
- **关于 chat template 渲染细节**：本讲把 `_process_messages` / `_apply_jinja_template` 当成黑盒，深入阅读这些方法可以理解多轮对话如何拼成最终 prompt，建议在读完 u3 后回来看。
