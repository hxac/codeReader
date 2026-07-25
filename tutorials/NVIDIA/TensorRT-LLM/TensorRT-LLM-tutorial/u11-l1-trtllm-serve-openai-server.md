# trtllm-serve 与 OpenAI 兼容服务

## 1. 本讲目标

学完本讲，读者应能：

- 复述 `trtllm-serve` 从命令行参数到 HTTP 服务监听的完整启动流程，定位每一步在 `commands/serve.py` 中的源码位置。
- 说清 `OpenAIServer` 如何把 OpenAI 协议端点（`/v1/chat/completions`、`/v1/completions`、`/v1/responses`…）注册到 FastAPI，并把一次聊天请求翻译成 `LLM.generate_async`。
- 理解 `chat_tokenization.py` 作为「聊天请求 → token」集中化模块的职责，尤其是 harmony/gpt-oss 路径如何 token 化。
- 掌握 tool parser 机制：模型自由文本输出如何被增量解析成结构化的 `tool_calls`，以及 strict 模式如何用结构化标签做约束解码。
- **区分** `OpenAIServer`（普通聚合服务的具体 FastAPI 服务）与 `OpenAIService`（分离式服务实现的抽象契约）这两个容易混淆的名字。

## 2. 前置知识

本讲承接 u1-l3（首次运行）与 u3-l2（PyExecutor 单步循环），需要先掌握：

- **u1-l3**：`trtllm-serve` 是经 `setup.py` 的 console_scripts 注册的命令，默认走 `serve` 子命令，内部构造 `LLM` 并包一层 OpenAI 兼容 HTTP 壳；离线 `LLM.generate` 与在线 `trtllm-serve` 殊途同归到同一个 `LLM` 类。
- **u3-l2**：`PyExecutor` 单步循环（取请求 → 调度 → 前向 → 采样 → 响应）；`LLM.generate_async` 返回 future（`RequestOutput`）。
- 基本的 FastAPI / uvicorn 概念：路由（route）、异步端点（async endpoint）、`StreamingResponse`（SSE 流式响应）。
- OpenAI Chat Completions 协议的大致字段：`messages`、`tools`、`stream`、`temperature` 等。

几个术语解释：

- **chat template（聊天模板）**：把结构化的 `messages`（role/content 列表）渲染成模型能理解的纯文本/token 序列（如 `<|im_start|>user\n...`），由 tokenizer 的 `apply_chat_template` 完成。
- **tool calling（工具调用）**：模型在回复里嵌入对外部函数的调用（函数名 + JSON 参数），服务端解析后以结构化 `tool_calls` 字段返回给客户端。
- **SSE（Server-Sent Events）**：流式响应协议，每条消息以 `data: ...\n\n` 形式推送，最后以 `data: [DONE]\n\n` 结束。
- **harmony**：NVIDIA GPT-OSS 模型专用的聊天/推理格式（含 reasoning effort、特定控制 token），由 `HarmonyAdapter` 处理。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `tensorrt_llm/commands/serve.py` | `trtllm-serve` CLI 入口与启动逻辑（click 命令、`get_llm_args`、`launch_server`） |
| `tensorrt_llm/serve/openai_server.py` | `OpenAIServer`：FastAPI 应用、路由注册、各 OpenAI 端点的请求处理 |
| `tensorrt_llm/serve/openai_service.py` | `OpenAIService`：服务层**抽象基类**（ABC），是分离式服务的契约，**非**普通服务的父类 |
| `tensorrt_llm/serve/openai_protocol.py` | OpenAI 协议的 Pydantic 请求/响应模型（`ChatCompletionRequest` 等）及 `to_sampling_params` |
| `tensorrt_llm/serve/chat_tokenization.py` | 聊天请求 → token 的集中化逻辑（harmony token 化、通用 token 化分发） |
| `tensorrt_llm/serve/tool_parser/` | tool parser 家族：基类、工厂、各模型专用解析器 |
| `tensorrt_llm/serve/postprocess_handlers.py` | 响应后处理（含 `apply_tool_parser`，把文本解析成 `tool_calls`） |

## 4. 核心概念与源码讲解

### 4.1 serve CLI：从命令行到启动服务器

#### 4.1.1 概念说明

`trtllm-serve` 是一个基于 click 的命令组（command group）。它有一个聪明的默认行为：当用户直接执行 `trtllm-serve <model>`（不带子命令名）时，自动当作 `serve` 子命令处理——这是靠自定义的 `DefaultGroup` 实现的。

服务分两大模式：

- **普通（aggregate）服务**：一个进程同时做 prefill 与 decode，是本讲重点。入口是 `serve` 命令 → `launch_server`。
- **分离式（disaggregated）服务**：prefill 与 decode 分到不同 GPU，入口是 `disaggregated` 命令（留待 u11-l2）。

此外还有 `mm_embedding_serve`（多模态编码器）、`embeddings`（纯嵌入服务）等子命令。

#### 4.1.2 核心流程

普通服务的启动链路：

```text
click `serve` 命令
  → collect_explicit_cli_keys()         # 收集用户在命令行显式设置的参数名
  → get_llm_args(...)                   # 把 CLI 参数组装成 llm_args 字典（过滤默认值）
  → update_llm_args_with_extra_dict()   # 叠加 --config YAML（CLI 显式值优先）
  → launch_server(host, port, llm_args, tool_parser, ...)
       → 绑定监听 socket（端口冲突时给出诊断）
       → PyTorchLLM(**llm_args) 或 AutoDeployLLM(**llm_args)   # 构造 LLM 引擎
       → OpenAIServer(generator=llm, ...)                        # 包 OpenAI HTTP 壳
       → _apply_fastapi_middlewares(app, middleware)
       → uvloop.run(server(host, port, sockets=[s]))            # 跑 uvicorn
```

关键设计：`get_llm_args` **不会**把所有参数一股脑塞进 `llm_args`，而是用 `is_non_default_or_required` 过滤——只保留「必填的」「CLI 显式设置过的」「与默认值不同的」参数。这避免了用 CLI 默认值去覆盖模型自带的默认值（参见 u4-l2 的模型默认值合并：框架默认 < 模型默认 < 用户显式）。

`tool_parser == "auto"` 时，调用 `resolve_auto_tool_parser(model)` 读模型 `config.json` 的 `model_type`，在 `MODEL_TYPE_TO_TOOL_PARSER` 表里查表解析；查不到就抛错并列出支持的模型类型。

#### 4.1.3 源码精读

[commands/serve.py:48](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/commands/serve.py#L48) —— 从 `tensorrt_llm.serve` 导入 `OpenAIServer`（与 `OpenAIDisaggServer`），这是 serve.py 与服务层对接的入口。

[commands/serve.py:935-936](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/commands/serve.py#L935-L936) —— `@click.command("serve")` 声明命令名为 `serve`，第一个位置参数是 `model`。

[commands/serve.py:2469-2476](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/commands/serve.py#L2469-L2476) —— `DefaultGroup.resolve_command`：若第一个参数不是已注册子命令，就当作 `serve`，实现「裸命令默认走 serve」。

[commands/serve.py:2479-2486](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/commands/serve.py#L2479-L2486) —— `main = DefaultGroup(commands={...})` 注册五个子命令（serve / disaggregated / disaggregated_mpi_worker / mm_embedding_serve / embeddings）。

[commands/serve.py:123-176](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/commands/serve.py#L123-L176) —— `is_non_default_or_required`：定义三类「会被保留」的参数（必填 / CLI 显式 / 与默认不同）。

[commands/serve.py:184-310](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/commands/serve.py#L184-L310) —— `get_llm_args`：组装 `cli_maybe_overrides` 字典后，用字典推导 + `is_non_default_or_required` 过滤出真正要传给 LLM 的参数。

[commands/serve.py:1308-1320](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/commands/serve.py#L1308-L1320) —— `tool_parser == "auto"` 的解析：查不到抛 `click.BadParameter` 并列出支持模型类型。

[commands/serve.py:517-619](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/commands/serve.py#L517-L619) —— `launch_server` 主体。注意端口绑定（L550-L571）在构造 LLM **之前**，这样端口冲突能立刻报错而不是等模型加载完。

[commands/serve.py:573-585](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/commands/serve.py#L573-L585) —— 按 `backend` 构造 `PyTorchLLM` 或 `AutoDeployLLM`，并 `pop("build_config")`（PyTorch/AutoDeploy 后端不用 TRT 的 build_config）。

[commands/serve.py:597-616](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/commands/serve.py#L597-L616) —— 构造 `OpenAIServer`，挂中间件，最后 `uvloop.run(server(host, port, sockets=[s]))` 把已绑定的 socket 交给 uvicorn。

#### 4.1.4 代码实践

**源码阅读型实践：追踪启动链路与参数过滤**

1. 实践目标：确认「命令行参数 → llm_args」的过滤行为。
2. 操作步骤：
   - 在 [get_llm_args](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/commands/serve.py#L184-L310) 里找到 `cli_maybe_overrides` 字典（约 L235-L302）。
   - 阅读 [is_non_default_or_required](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/commands/serve.py#L123-L176)，列出三类「会被保留」的参数。
3. 观察现象/预期结果：你能说出——「必填参数（model/backend/tokenizer 等）」「CLI 显式设置的（名字在 `explicit_cli_keys` 里）」「与 `TorchLlmArgs` 默认值不同的」三类会被保留；纯默认值会被丢弃，从而不会覆盖模型自带默认值。
4. 若本机有 GPU 与小模型，可运行 `trtllm-serve <model> --host 0.0.0.0 --port 8000`，观察日志里 `Auto-detected tool parser:`、`trtllm/... is registered` 等行；无 GPU 则标「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `launch_server` 要在构造 `LLM` 之前先绑定 socket？

**答案**：模型加载可能耗时几十秒到几分钟。若先加载模型再绑端口，期间别的进程可能抢走该端口，导致加载完成后 `bind()` 才失败，白白浪费加载时间。提前绑定既校验端口可用，又把端口「占住」。

**练习 2**：`DefaultGroup` 解决了什么问题？

**答案**：让 `trtllm-serve <model>`（不带子命令）与 `trtllm-serve serve <model>` 等价，降低用户记忆负担——最常见的用法就是最简的用法。

### 4.2 OpenAIServer：FastAPI 路由与请求处理

#### 4.2.1 概念说明

`OpenAIServer` 是普通（aggregate）服务的 FastAPI 应用容器。它：

- 持有一个 `generator`（即 `LLM`，或 `MultimodalEncoder` / `VisualGen`）。
- 在 `__init__` 里根据 `server_role` 注册不同的路由集合（普通 LLM / 多模态编码器 / 嵌入 / 视觉生成）。
- 把每个 OpenAI 端点实现成一个 `async` 方法，内部把请求翻译成 `LLM.generate_async`。

> **命名澄清（重要）**：`OpenAIServer`（openai_server.py）**不继承** `OpenAIService`。`OpenAIService`（openai_service.py）是一个抽象基类（ABC），仅定义 `openai_completion` / `openai_chat_completion` / `is_ready` / `setup` / `teardown` 的契约，**唯一**的实现者是分离式服务的 `OpenAIDisaggregatedService`（见 u11-l2）。也就是说：
>
> - 普通 `trtllm-serve` → `OpenAIServer`（具体 FastAPI 服务，本讲主角）。
> - 分离式服务 → `OpenAIDisaggServer` + `OpenAIDisaggregatedService`（继承 `OpenAIService`）。
>
> 二者是两套并行实现，名字相近但不要混。

#### 4.2.2 核心流程（以 `/v1/chat/completions` 非流式为例）

```text
POST /v1/chat/completions
  → OpenAIServer.openai_chat(request, raw_request)
       1. ensure_request_chat_template_allowed()        # 若不允许 per-request 模板则拦截
       2. request.to_sampling_params(vocab_size, ...)   # 协议参数 → SamplingParams
       3. (若 use_harmony) 实际路由到 chat_harmony（见 4.3）
       4. parse_chat_messages_coroutines(messages)      # 解析消息，产出多模态协程
       5. async_apply_chat_template(...)                # 渲染聊天模板 → prompt token ids
          （harmony 路径则用 tokenize_harmony_chat_request）
       6. prompt_inputs(prompt)                         # 包装成生成输入
       7. self.generator.generate_async(inputs, sampling_params, streaming=...)
              → 返回 RequestOutput（future）
       8. asyncio.create_task(await_disconnected(...))  # 客户端断连则 abort
       9. 流式：StreamingResponse(SSE)；非流式：_create_chat_response → JSONResponse
```

其中第 7 步是分水岭——之前全是 HTTP/协议层的 Python 编排，之后进入 u3-l2 讲过的 PyExecutor 单步循环（Scheduler / 前向 / Sampling）。

#### 4.2.3 源码精读

[serve/openai_server.py:243](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_server.py#L243) —— `class OpenAIServer(_VideoRoutesMixin)`，注意它不继承 `OpenAIService`。

[serve/openai_server.py:254-268](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_server.py#L254-L268) —— `__init__` 签名：接收 `generator`（LLM/编码器/VisualGen）、`tool_parser`、`server_role`、`chat_template` 等。

[serve/openai_server.py:457-474](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_server.py#L457-L474) —— 按 `server_role` 分派路由注册：`VISUAL_GEN` / `MM_ENCODER` / `EMBEDDING` / 默认（普通 LLM 走 `register_routes`）。

[serve/openai_server.py:791-884](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_server.py#L791-L884) —— `register_routes`：注册 `/health`、`/v1/models`、`/metrics`、`/v1/completions`、`/v1/chat/completions`、`/v1/responses` 等。

[serve/openai_server.py:849-855](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_server.py#L849-L855) —— 关键两行：`/v1/completions` 绑定 `openai_completion`；`/v1/chat/completions` 根据 `self.use_harmony` 在 `openai_chat` 与 `chat_harmony` 间二选一。

[serve/openai_server.py:1464-1465](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_server.py#L1464-L1465) —— `async def openai_chat`：标准聊天端点入口。

[serve/openai_server.py:1515-1520](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_server.py#L1515-L1520) —— `request.to_sampling_params(...)`：把 OpenAI 协议字段翻译成引擎的 `SamplingParams`（实现见 [openai_protocol.py:565-604](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_protocol.py#L565-L604)，把 `temperature`/`top_p`/`stop_token_ids`/`max_tokens` 等映射过去）。

[serve/openai_server.py:1571-1584](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_server.py#L1571-L1584) —— 标准路径用 `async_apply_chat_template` 渲染模板；注意它**不**调用 `chat_tokenization.py`，而是直接用 `inputs.utils` 的异步工具（原因见 4.3）。

[serve/openai_server.py:1634-1647](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_server.py#L1634-L1647) —— `self.generator.generate_async(...)`：提交生成请求，拿到 `promise`（`RequestOutput`），并起一个断连监听任务。

[serve/openai_server.py:694-703](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_server.py#L694-L703) —— `await_disconnected`：客户端断连时 `promise.abort()`，避免浪费算力。

[serve/openai_server.py:1652-1672](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_server.py#L1652-L1672) —— 流式返回 `StreamingResponse`（SSE），非流式经 `_create_chat_response` 后返回 `JSONResponse`。

[serve/openai_server.py:2564-2590](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_server.py#L2564-L2590) —— `__call__`：构造 `uvicorn.Config` 与 `uvicorn.Server`，`await server.serve(sockets=sockets)` 正式对外服务。

#### 4.2.4 代码实践

**源码阅读型实践：端点 → 处理函数映射**

1. 实践目标：建立「URL → 处理方法 → generate_async」的映射表。
2. 操作步骤：在 [register_routes](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_server.py#L791-L884) 中，对每个 `add_api_route` 找到其绑定的方法。
3. 观察现象/预期结果：填出一张表，如 `/v1/completions → openai_completion`、`/v1/chat/completions → openai_chat 或 chat_harmony`、`/v1/responses → openai_responses`、`/health → health`、`/v1/models → get_model`。
4. 进阶（有 GPU 时）：启动服务后用 `curl http://localhost:8000/v1/chat/completions -d '{...}'` 验证；无 GPU 则标「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`/v1/chat/completions` 为什么可能路由到两个不同的方法？

**答案**：GPT-OSS（gpt_oss）模型使用 harmony 格式，其 token 化与后处理都不同，故用 `chat_harmony`；其他模型走标准 `openai_chat`。选择由 `self.use_harmony`（在 `_init_llm` 中据 `model_config.model_type == "gpt_oss"` 判定）决定。

**练习 2**：`OpenAIServer` 与 `OpenAIService` 是什么关系？

**答案**：没有继承关系。`OpenAIServer` 是普通聚合服务的具体 FastAPI 类；`OpenAIService` 是分离式服务的抽象契约（ABC），由 `OpenAIDisaggregatedService` 实现。二者名字相近但属于两套实现。

### 4.3 聊天 token 化：chat_tokenization 与 harmony 路径

#### 4.3.1 概念说明

`chat_tokenization.py` 是把「OpenAI 聊天请求」转成「token id 序列」的集中化模块。它的价值在于：把原本散落在各处的 token 化逻辑抽出来，让 harmony 路径、外部 router（resource governor）等复用同一份代码。

模块提供三个层次：

- `tokenize_harmony_chat_request`：harmony/gpt-oss 专用，调 `HarmonyAdapter.openai_to_harmony_tokens`。
- `render_chat_request_for_tokenizer`：标准路径，调 tokenizer 的 `apply_chat_template`（`tokenize=False`，先拿文本再编码）。
- `tokenize_chat_request_for_serving`：顶层分发器，按 `uses_harmony_tokenization` 在上面两者间二选一。

「harmony token 化」指 GPT-OSS 模型不使用 HF 的 chat template，而是用自己的 `HarmonyAdapter` 把 messages + tools + reasoning_effort 拼成带控制 token 的序列。

#### 4.3.2 核心流程

```text
tokenize_chat_request_for_serving(request, ...)
  ├─ 若 request.prompt_token_ids 已存在 → 直接返回（已被上游预 token 化，如分离式接力）
  ├─ uses_harmony_tokenization(...)？
  │    ├─ 是 → tokenize_harmony_chat_request(request)
  │    │          → HarmonyAdapter.openai_to_harmony_tokens(messages, tools,
  │    │                                  reasoning_effort, tool_choice)
  │    └─ 否 → render_chat_request_for_tokenizer(request, tokenizer)
  │                → tokenizer.apply_chat_template(messages, add_generation_prompt,
  │                                                  tools, documents, ...)
  │                → encode_rendered(rendered, tokenizer)   # 文本 → token ids
  └─ (可选) set_prompt_token_ids=True 时回写到 request，避免重复 token 化
```

#### 4.3.3 源码精读

[serve/chat_tokenization.py:38-49](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/chat_tokenization.py#L38-L49) —— `uses_harmony_tokenization`：受 `DISABLE_HARMONY_ADAPTER` 环境变量控制，最终看 `model_type == "gpt_oss"`。

[serve/chat_tokenization.py:68-89](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/chat_tokenization.py#L68-L89) —— `tokenize_harmony_chat_request`：调 `adapter.openai_to_harmony_tokens`，把 messages/tools/reasoning_effort/tool_choice 转成 harmony token 序列。

[serve/chat_tokenization.py:92-111](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/chat_tokenization.py#L92-L111) —— `render_chat_request_for_tokenizer`：把 `tools`/`documents`/`chat_template` 塞进 `chat_template_kwargs`，调 `tokenizer.apply_chat_template(..., tokenize=False)` 拿到渲染文本（或已是 token 列表）。

[serve/chat_tokenization.py:114-143](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/chat_tokenization.py#L114-L143) —— `tokenize_chat_request_for_serving`：顶层分发器。

[serve/openai_server.py:59](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_server.py#L59) 与 [serve/openai_server.py:2061-2062](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_server.py#L2061-L2062) —— `OpenAIServer` 在 harmony 路径（`chat_harmony`）里 import 并调用 `tokenize_harmony_chat_request`。

[serve/openai_server.py:2083](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_server.py#L2083) —— harmony 路径设 `sampling_params.detokenize = False`，因为 HarmonyAdapter 从原始 token id 重建输出，不走标准 detokenize。

[serve/router_utils.py:287-294](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/router_utils.py#L287-L294) —— 外部 router / resource governor 路径调用通用分发器 `tokenize_chat_request_for_serving`，复用同一套逻辑（含前缀缓存编码）。

**chat_tokenization 相对「以往内联 token 化」的变化**：在重构前，聊天 token 化逻辑内联在各调用点，harmony 路径与标准路径、外部 router 各写一份。抽出 `chat_tokenization.py` 后：① harmony 与标准 token 化有统一入口与一致的工具/文档处理（`get_chat_completion_tool_dicts`）；② 外部 router（router_utils）能复用同一份逻辑；③ 便于单测。需要注意一个现状：标准 `openai_chat` 路径目前仍直接用 `async_apply_chat_template`（异步版，因多模态需并发加载图像），尚未切到本模块的同步分发器——所以本模块当前主要服务 **harmony 路径**与 **router 路径**。

#### 4.3.4 代码实践

**源码阅读型实践：对比两条 token 化路径**

1. 实践目标：说清 harmony 与标准 token 化的输入输出差异。
2. 操作步骤：
   - 读 [chat_harmony](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_server.py#L2011-L2142)，找到 `tokenize_harmony_chat_request` 调用（L2061）与 `sampling_params.detokenize = False`（L2083）。
   - 读 [openai_chat](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_server.py#L1571-L1584)，找到标准 `async_apply_chat_template`。
3. 观察现象/预期结果：harmony 路径 `detokenize=False`（HarmonyAdapter 自管 detokenize），且 `reasoning_parser` 强制为 `"gpt_oss"`；标准路径则正常 detokenize、用模型默认 reasoning_parser。
4. 最小调用示例（示例代码，非项目原码）：
   ```python
   # 示例代码：直接调用 harmony token 化（需 gpt_oss 模型与 HarmonyAdapter）
   from tensorrt_llm.serve.chat_tokenization import tokenize_harmony_chat_request
   # request 是一个 ChatCompletionRequest 对象
   tokens = tokenize_harmony_chat_request(request)
   ```
   运行结果：待本地验证（需 gpt_oss checkpoint）。

#### 4.3.5 小练习与答案

**练习 1**：`tokenize_chat_request_for_serving` 为什么先检查 `request.prompt_token_ids is not None`？

**答案**：分离式服务中，上游 context worker 可能已经 token 化并把 token id 经 orchestrator 接力传过来（或客户端直接传 `prompt_token_ids`）。直接复用避免重复 token 化，且保证 token identity 一致（这是 u11-l2 的关键点）。

**练习 2**：标准 `openai_chat` 为何没用 `tokenize_chat_request_for_serving`？

**答案**：标准路径需要**异步**渲染（多模态消息要并发加载图像），故用 `async_apply_chat_template` + `asyncio.gather`；而 `chat_tokenization.py` 目前是同步接口，更适合 harmony 与外部 router。这是历史演进中的现状，不是最终形态。

### 4.4 Tool Parser：把工具调用嵌入/解析输出

#### 4.4.1 概念说明

Tool calling 让模型能调用外部函数。但 LLM 本质上只产出 token 序列（解码成文本）。Tool parser 就是「文本 ↔ 结构化 `tool_calls`」的双向桥梁，工作在两个阶段：

- **请求阶段（约束，可选）**：当工具声明 `strict=True` 时，用「结构化标签（structural tag）」做约束解码，保证生成的工具参数严格符合其 JSON Schema（呼应 u8-l3 的 guided decoding）。
- **后处理阶段（解析，必有）**：把模型输出文本里嵌入的工具调用片段（如 Qwen3 的 `<tool_call>{...}</tool_call>`）**增量**解析成结构化的 `tool_calls` 字段，塞回 OpenAI 响应。

不同模型有不同的工具调用文本格式，故每种格式对应一个 parser（`Qwen3ToolParser`、`DeepSeekV3Parser`、`KimiK2ToolParser` 等）。工厂 `ToolParserFactory` 按名字分发；`resolve_auto_tool_parser` 按 `model_type` 自动选。

#### 4.4.2 核心流程

**解析流程（在后处理器里，每个输出/每次流式增量调用）**：

```text
模型产出 token → detokenize 成 delta_text
  → postprocess_handlers.apply_tool_parser(args, output_index, text, streaming)
       ├─ 工厂按 args.tool_parser 名字取/缓存一个 parser 实例（每 output_index 一个）
       ├─ streaming？
       │    ├─ 是 → parser.parse_streaming_increment(text, tools)   # 增量、有状态
       │    └─ 否 → parser.detect_and_parse(text, tools)            # 一次性
       └─ 返回 (normal_text, calls)
  → 把 calls 包装成 DeltaMessage(tool_calls=[...]) 或非流式的 ChoiceMessage
  → 若该 output 触发过 tool_call，finish_reason 从 "stop" 改写为 "tool_calls"
```

**约束流程（strict 工具，请求阶段，在 openai_chat 内）**：

```text
检测到 request.tools 里有 tool.function.strict=True
  → _build_tool_strict_guided_decoding_params(tools, tool_parser_name)
       ├─ 取 parser.structure_info()(name) → StructureInfo(begin, end, trigger)
       ├─ 每个 strict 工具的 JSON Schema 包成 {type:"json_schema", json_schema: parameters}
       ├─ 组装 stag_format = {type:"triggered_tags", triggers, tags}
       └─ 返回 GuidedDecodingParams(structural_tag=...)
  → 注入 sampling_params.guided_decoding（若未被 response_format 占用）
```

这呼应 u8-l3：约束解码在采样前把非法 token 置 -inf，保证生成的工具参数 JSON 合法；而 parser 在生成后做解析。二者是「生成时保证合法」与「生成后提取结构」的互补关系。

#### 4.4.3 源码精读

[serve/tool_parser/__init__.py:1-3](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/tool_parser/__init__.py#L1-L3) —— 包导出 `ToolParserFactory`。

[serve/tool_parser/tool_parser_factory.py:20-40](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/tool_parser/tool_parser_factory.py#L20-L40) —— `MODEL_TYPE_TO_TOOL_PARSER` 映射表（如 `qwen3 → qwen3`、`deepseek_v3 → deepseek_v3`、`kimi_k2 → kimi_k2`、`glm4 → glm4`）。

[serve/tool_parser/tool_parser_factory.py:43-53](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/tool_parser/tool_parser_factory.py#L43-L53) —— `resolve_auto_tool_parser(model)`：读 `<model>/config.json` 的 `model_type` 查表。

[serve/tool_parser/tool_parser_factory.py:56-82](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/tool_parser/tool_parser_factory.py#L56-L82) —— `ToolParserFactory`：`parsers` 字典登记所有 parser 类；`create_tool_parser` 按名字实例化。

[serve/tool_parser/base_tool_parser.py:16-44](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/tool_parser/base_tool_parser.py#L16-L44) —— `BaseToolParser` 抽象基类，维护流式状态（`_buffer`、`current_tool_id`、`streamed_args_for_tool` 等），并定义 `bot_token`/`eot_token`（begin/end of tool）钩子。

[serve/tool_parser/base_tool_parser.py:90-98](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/tool_parser/base_tool_parser.py#L90-L98) —— `detect_and_parse`：一次性解析抽象方法。

[serve/tool_parser/base_tool_parser.py:113-291](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/tool_parser/base_tool_parser.py#L113-L291) —— `parse_streaming_increment`：基类增量解析，处理「先发工具名、再增量发参数」、partial JSON、多工具分隔等通用逻辑。

[serve/tool_parser/base_tool_parser.py:300-316](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/tool_parser/base_tool_parser.py#L300-L316) —— `supports_structural_tag` 与 `structure_info`：供 strict 约束解码使用。

[serve/tool_parser/core_types.py:8-27](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/tool_parser/core_types.py#L8-L27) —— `ToolCallItem`（解析出的单个工具调用）、`StreamingParseResult`（`normal_text` + `calls`）、`StructureInfo`（约束解码的 begin/end/trigger）。

[serve/tool_parser/qwen3_tool_parser.py:13-37](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/tool_parser/qwen3_tool_parser.py#L13-L37) —— `Qwen3ToolParser`：格式为 `<tool_call>\n{"name":..., "arguments":{...}}\n</tool_call>`，故 `bot_token="<tool_call>\n"`、`eot_token="\n</tool_call>"`。

[serve/tool_parser/qwen3_tool_parser.py:43-70](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/tool_parser/qwen3_tool_parser.py#L43-L70) —— `detect_and_parse`：用正则提取每个 `<tool_call>...</tool_call>` 块并 `json.loads`。

[serve/tool_parser/qwen3_tool_parser.py:109-114](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/tool_parser/qwen3_tool_parser.py#L109-L114) —— `structure_info`：返回约束解码所需结构（`begin='<tool_call>\n{"name":"<name>", "arguments":'`、`end='}\n</tool_call>'`、`trigger='<tool_call>'`）。

[serve/postprocess_handlers.py:190-212](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/postprocess_handlers.py#L190-L212) —— `apply_tool_parser`：在后处理里调用 parser，按 `streaming` 选 `detect_and_parse` 或 `parse_streaming_increment`，并缓存每 output 的 parser 实例（`args.tool_parser_dict`）。

[serve/postprocess_handlers.py:293](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/postprocess_handlers.py#L293) 与 [serve/postprocess_handlers.py:343-345](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/postprocess_handlers.py#L343-L345) —— 流式后处理调用 `apply_tool_parser`，并在 `finish_reason=="stop"` 但有过 tool_call 时改写为 `"tool_calls"`。

[serve/openai_server.py:159-228](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_server.py#L159-L228) —— `_build_tool_strict_guided_decoding_params`：构建 strict 工具的结构化标签约束。

[serve/openai_server.py:1527-1540](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/openai_server.py#L1527-L1540) —— `openai_chat` 里：若启用 tool_parser 且有 tools，按需关 `skip_special_tokens`（让特殊 token 透传给 parser），并在未被 response_format 占用时注入 strict 约束。

#### 4.4.4 代码实践

**源码阅读 + 可选运行实践：亲眼看到 tool parser 解析**

1. 实践目标：验证 tool parser 如何把 `<tool_call>{...}</tool_call>` 文本解析成结构化调用。
2. 操作步骤：
   - 读 [Qwen3ToolParser.detect_and_parse](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/serve/tool_parser/qwen3_tool_parser.py#L43-L70)，理解正则提取与 `json.loads`。
   - 写一段最小脚本（示例代码，非项目原码），直接驱动 parser，**不需要 GPU**：
     ```python
     # 示例代码：直接测试 Qwen3 parser（不依赖 GPU）
     from tensorrt_llm.serve.tool_parser.tool_parser_factory import ToolParserFactory
     p = ToolParserFactory.create_tool_parser("qwen3")
     text = ('<tool_call>\n{"name": "get_weather", '
             '"arguments": {"city": "SF"}}\n</tool_call>')
     tools = [{"type": "function",
               "function": {"name": "get_weather",
                            "parameters": {"type": "object"}}}]
     result = p.detect_and_parse(text, tools)
     print("normal_text:", repr(result.normal_text))
     print("calls:", result.calls)
     ```
3. 观察现象/预期结果：`calls` 应含一个 `ToolCallItem(name="get_weather", parameters='{"city": "SF"}')`；`normal_text` 为空（或工具调用前的普通文本）。
4. 若有支持工具调用的模型（如 Qwen3）与 GPU：`trtllm-serve <model> --tool_parser auto`，再用带 `tools` 字段的 `/v1/chat/completions` 请求验证返回的 `tool_calls` 与 `finish_reason="tool_calls"`；无 GPU 则标「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：strict 工具约束解码与普通 tool parser 解析有何关系？

**答案**：二者是「生成时」与「生成后」两个阶段。约束解码（structural tag）在生成时保证 strict 工具的参数 JSON 严格合法；parser 在生成后把文本解析成结构化 `tool_calls`。即便不开 strict，parser 仍能解析；开了 strict，parser 解析时更不会因 JSON 非法而丢调用。

**练习 2**：为什么 `apply_tool_parser` 要为每个 `output_index` 缓存一个 parser 实例（`tool_parser_dict`）？

**答案**：流式 parser 是**有状态**的（`_buffer`、`current_tool_id`、`streamed_args_for_tool` 等），不同 output（如 `n>1` 的多候选）的流不能共享状态，否则会串味。故每 output 独占一个实例。

## 5. 综合实践

**贯穿本讲的任务：画出一次带工具调用、流式的 `/v1/chat/completions` 请求的完整时序图。**

要求：

1. 从「curl 敲下」一直画到「SSE 推出 `tool_calls`」，标出关键转换点：HTTP 请求 → `ChatCompletionRequest` → `to_sampling_params` → token 化 → `_build_tool_strict_guided_decoding_params`（若 strict）→ `generate_async` → 流式后处理 `apply_tool_parser` → `StreamingResponse`。
2. 用不同标记区分职责层：
   - **HTTP 编排层**：`serve.py` + `openai_server.py`（`launch_server` / `openai_chat`）。
   - **token 化层**：`chat_tokenization.py`（harmony）或 `async_apply_chat_template`（标准）。
   - **引擎层**：`PyExecutor` 单步循环（黑盒，u3-l2）。
   - **后处理层**：`postprocess_handlers.apply_tool_parser` + `tool_parser`。
3. 写一段说明，回答三个问题：
   - 若该模型是 gpt_oss，链路在哪一步分叉到 harmony？（答：路由 `/v1/chat/completions` 时据 `use_harmony` 选 `chat_harmony`，token 化走 `tokenize_harmony_chat_request`。）
   - tool parser 在哪一步把文本变成 `tool_calls`？（答：后处理 `apply_tool_parser`，流式用 `parse_streaming_increment`。）
   - `chat_tokenization` 相对以往内联 token 化的变化是什么？（答：抽出共享模块，统一 harmony 与标准路径的工具/文档处理，供 router 复用；标准 `openai_chat` 因异步多模态暂仍用 `async_apply_chat_template`。）
4. 可选运行验证（有 GPU 时）：`trtllm-serve <qwen3-model> --tool_parser auto`，用 `curl -N` 发一个带 `tools` 与 `"stream": true` 的请求，观察 SSE 事件里 `delta.tool_calls` 的**增量**出现，以及最后的 `data: [DONE]`。无 GPU 则整段标「待本地验证」并改为纯源码阅读。

## 6. 本讲小结

- `trtllm-serve` 是 click 命令组，`DefaultGroup` 让裸命令默认走 `serve`；`get_llm_args` 用 `is_non_default_or_required` 过滤，避免 CLI 默认值覆盖模型默认值。
- `launch_server` **先绑 socket 再加载模型**，端口冲突早报错；按 `backend` 构造 `PyTorchLLM`/`AutoDeployLLM`，再包 `OpenAIServer`，最后 `uvloop.run` 起 uvicorn。
- `OpenAIServer` 是普通服务的 FastAPI 容器，按 `server_role` 注册路由；`/v1/chat/completions` 据 `use_harmony` 在 `openai_chat` 与 `chat_harmony` 间二选一；端点内部把请求翻译成 `LLM.generate_async`。
- `chat_tokenization.py` 是聊天 token 化的集中模块：harmony 路径（`tokenize_harmony_chat_request`）与外部 router（`tokenize_chat_request_for_serving`）复用它；标准 `openai_chat` 因异步多模态仍用 `async_apply_chat_template`。
- tool parser 是「文本 ↔ `tool_calls`」桥梁：请求阶段可用 strict 结构化标签约束解码，后处理阶段用 `detect_and_parse`/`parse_streaming_increment` 把嵌入文本解析成结构化调用，并把 `finish_reason` 改写为 `"tool_calls"`。
- `OpenAIService`（openai_service.py）是分离式服务的抽象契约（被 `OpenAIDisaggregatedService` 继承），与 `OpenAIServer` 是两套并行实现，名字相近但勿混。

## 7. 下一步学习建议

- **u11-l2 分离式服务**：理解 `OpenAIService` 的具体实现 `OpenAIDisaggregatedService`、`OpenAIDisaggServer` 与 disagg coordinator 如何跨节点搬运 KV cache。
- **u8-l3 Decoder 与 Sampling**：深入 `SamplingParams`、guided decoding 的 bitmask/structural-tag 实现，理解 strict 工具约束的底层机制。
- **u3-l2 PyExecutor 单步循环**：看 `generate_async` 之后的 future 如何被单步循环推进到产出 token。
- **想加一个新模型的工具调用支持？** 仿照 `qwen3_tool_parser.py` 写一个 parser（继承 `BaseToolParser`，实现 `has_tool_call`/`detect_and_parse`/`structure_info`），再在 `ToolParserFactory.parsers` 与 `MODEL_TYPE_TO_TOOL_PARSER` 注册。
