# 部署与服务化

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `swift deploy` 与 `swift infer` 的本质区别，并能描述一条 `swift deploy ...` 命令从 CLI 路由到 `uvicorn.run` 启动 HTTP 服务的完整链路。
- 理解 `SwiftDeploy` 管道如何复用 `SwiftInfer` 的模型/模板/引擎装配，再叠加一层 FastAPI 路由把推理能力「服务化」。
- 掌握 ms-swift 的 **OpenAI 兼容协议**：`ChatCompletionRequest`/`CompletionRequest`/`EmbeddingRequest` 如何与引擎内部的 `InferRequest`/`RequestConfig` 通过 `parse()` 互转，以及多 LoRA、流式、鉴权等如何实现。
- 理解 `swift app` 如何用 `run_deploy` 在子进程里拉起一个部署服务，再套一层 Gradio 聊天界面，做到「一键可视化交互」。
- 能独立部署一个模型服务，并用 `curl` / OpenAI 官方 SDK / `InferClient` 三种方式调用验证。

## 2. 前置知识

本讲是 **专家层「部署、导出与评测」单元**的第二篇，承接 [u6 推理引擎](./u6-l2-multi-backend-infer-engines.md)。在进入源码前，请先具备以下直觉：

- **OpenAI Chat Completions API**：业界事实标准的 LLM HTTP 接口。典型调用是 `POST /v1/chat/completions`，请求体含 `model`、`messages`、`temperature`、`max_tokens`、`stream` 等字段，响应是结构化的 `choices[].message.content`。只要服务端遵守这套协议，任何 OpenAI 客户端（SDK、LangChain、curl）都能无缝接入——这正是「OpenAI 兼容」的价值。
- **FastAPI 与 ASGI**：FastAPI 是基于 Starlette 的异步 Web 框架，用装饰器 `@app.post('/path')` 注册路由，天然支持 `async def` 处理函数与流式响应（`StreamingResponse`）。ms-swift 的部署服务就是用 FastAPI 写的。
- **SSE（Server-Sent Events）流式输出**：流式返回时，服务端用 `text/event-stream`，每个 chunk 以 `data: {...}\n\n` 形式推送，最后发一条 `data: [DONE]\n\n` 结束。OpenAI 的流式接口用的就是这套。
- **推理引擎的两件套**（来自 u6）：`InferRequest` 描述「问什么」（messages + 多模态资产），`RequestConfig` 描述「怎么答」（温度、max_tokens、stream 等）。本讲的 HTTP 协议层就是在它们之上多套了一层 OpenAI 字段映射。
- **LoRA adapter**：轻量微调产物（一个 `adapter_model.safetensors`）。部署时可同时挂载多个 adapter，按请求里的 `model` 字段路由到不同 adapter，实现「一份基座 + N 个 LoRA」的多服务。

> 一句话定位：`swift deploy` = `SwiftInfer`（装配模型/模板/引擎） + FastAPI（HTTP 服务化） + OpenAI 协议（标准化接口）；`swift app` = `swift deploy`（子进程拉服务） + Gradio（可视化聊天）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [swift/pipelines/infer/deploy.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py) | `SwiftDeploy` 管道：注册 FastAPI 路由、处理请求、`deploy_main` 入口、`run_deploy` 子进程上下文管理器。本讲核心。 |
| [swift/pipelines/infer/infer.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/infer.py) | `SwiftInfer` 基类：`deploy` 复用它的模型/模板/引擎装配（`get_infer_engine`）与 `infer_async`。 |
| [swift/pipelines/app/app.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/app/app.py) | `SwiftApp` 管道：用 `run_deploy` 拉起服务，再 `build_ui` 套 Gradio。 |
| [swift/pipelines/app/build_ui.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/app/build_ui.py) | Gradio 界面构建：聊天交互、多模态上传、`InferClient` 调用。 |
| [swift/infer_engine/protocol.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/protocol.py) | OpenAI 兼容协议数据类：请求 `ChatCompletionRequest`、响应 `ChatCompletionResponse` 等。 |
| [swift/infer_engine/infer_client.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/infer_client.py) | `InferClient`：走 HTTP 调用 OpenAI 兼容服务的客户端，`swift app` 与外部调用都用它。 |
| [swift/arguments/deploy_args.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/deploy_args.py) | `DeployArguments`：host/port/api_key/ssl、多 adapter 映射、日志统计等部署专属参数。 |
| [swift/arguments/app_args.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/app_args.py) | `AppArguments`：继承 `WebUIArguments + DeployArguments`，新增 `base_url` 等界面参数。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：①`deploy_main` 服务启动链路；②OpenAI 兼容协议；③`swift app` 的 Gradio 界面。

### 4.1 deploy_main 服务启动：从 CLI 到 uvicorn

#### 4.1.1 概念说明

`swift infer` 与 `swift deploy` 都属于 `SwiftInfer` 家族，但定位完全不同：

- `swift infer`：**一次性**推理。要么是终端交互式对话（`infer_cli`），要么是对一个数据集跑批（`infer_dataset`），跑完即退出。适合开发期验证效果。
- `swift deploy`：**常驻** HTTP 服务。加载一次模型后，用 uvicorn 持续监听端口，对外提供 OpenAI 兼容 API，供线上业务调用。适合生产部署。

两者共享同一套模型/模板/引擎装配逻辑（这是 `SwiftDeploy` 继承 `SwiftInfer` 的根本原因），差异只在「装配完之后做什么」：`SwiftInfer.run` 跑推理，`SwiftDeploy.run` 启服务。

#### 4.1.2 核心流程

一条 `swift deploy --model ... --infer_backend vllm` 的执行链路如下：

```
swift deploy ...                        # 用户命令
  └─ cli_main (swift/cli/main.py)       # 路由：ROUTE_MAPPING['deploy'] → swift.cli.deploy
       └─ swift/cli/deploy.py           # 薄入口：from swift.pipelines import deploy_main
            └─ deploy_main(args)        # deploy.py:250
                 └─ SwiftDeploy(args).main()   # SwiftPipeline.main() → run()
                      ├─ __init__:      # 装配模型/模板/引擎 + 建 FastAPI app + 注册路由
                      │    ├─ SwiftInfer.__init__  → get_infer_engine(args, template)
                      │    ├─ infer_engine.strict = True
                      │    ├─ InferStats()         # 吞吐统计
                      │    └─ FastAPI(lifespan=...) + _register_app()
                      └─ run():          # deploy.py:237
                           └─ uvicorn.run(app, host, port, ssl_*)
```

关键点：`SwiftDeploy` **没有重写 `run()` 之外的复杂逻辑**，它把「怎么拿模型/怎么推理」全部委托给父类 `SwiftInfer`，自己只负责「把推理能力包成 HTTP 服务」。

#### 4.1.3 源码精读

**入口与类定义**。`deploy_main` 只是 `SwiftDeploy(args).main()` 的一行封装：

[swift/pipelines/infer/deploy.py:250-251](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L250-L251) —— `deploy_main` 函数，构造 `SwiftDeploy` 并执行模板方法 `main()`。

```python
def deploy_main(args: Optional[Union[List[str], DeployArguments]] = None) -> None:
    SwiftDeploy(args).main()
```

`SwiftDeploy` 继承 `SwiftInfer`，`args_class` 指向 `DeployArguments`：

[swift/pipelines/infer/deploy.py:28-31](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L28-L31) —— 类声明，体现「deploy = infer + 服务化」的复用设计。

```python
class SwiftDeploy(SwiftInfer):
    args_class = DeployArguments
    args: args_class
```

**引擎装配的 deploy 专属定制**。`SwiftDeploy` 覆写了 `get_infer_engine`，只为处理 vLLM 部署的两个特殊场景——**数据并行（data parallel）必须用 async 引擎**，以及透传 `max_logprobs`。其余逻辑全部回退到 `SwiftInfer.get_infer_engine`（即 u6 讲过的、按 `infer_backend` 派发到 transformers/vllm/sglang/lmdeploy 的工厂）：

[swift/pipelines/infer/deploy.py:32-44](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L32-L44) —— vLLM data parallel 校验与参数注入。

```python
@staticmethod
def get_infer_engine(args: InferArguments, template=None, **kwargs):
    if isinstance(args, DeployArguments) and args.infer_backend == 'vllm':
        engine_kwargs = (kwargs.get('engine_kwargs') or {}).copy()
        if args.vllm_data_parallel_size > 1:
            if not args.vllm_use_async_engine:
                raise ValueError('vLLM data parallel requires `vllm_use_async_engine=True` in deploy mode.')
            engine_kwargs.setdefault('data_parallel_size', args.vllm_data_parallel_size)
        ...
    return SwiftInfer.get_infer_engine(args, template, **kwargs)
```

> 设计要点：`get_infer_engine` 是典型的「钩子覆写」——子类只干预自己关心的分支（vLLM 部署），其余回退父类。这与 u7-l1 讲 RLHF 时「覆写钩子而非重写骨架」是同一种工程思想。

**构造期建 FastAPI app**。`__init__` 在 `super().__init__()`（装配好 `infer_engine`）之后，做了三件服务化特有的事：

[swift/pipelines/infer/deploy.py:56-62](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L56-L62) —— 开启严格模式、统计、建 app。

```python
def __init__(self, args: Optional[Union[List[str], DeployArguments]] = None) -> None:
    super().__init__(args)
    self.infer_engine.strict = True       # 部署模式：请求出错直接抛，不做容错吞掉
    self.infer_stats = InferStats()       # tokens/s 吞吐统计
    self.app = FastAPI(lifespan=self.lifespan)
    self._register_app()                  # 注册所有路由
```

注意 `infer_engine.strict = True`：与 `InferEngine.async_iter_to_iter` 里的 `getattr(self, 'strict', True)` 联动，部署模式下任何推理异常都会如实抛给 HTTP 层返回 400，而不是被默默吞成空响应——线上服务的可观测性要求。

**路由注册**。`_register_app` 用 FastAPI 装饰器把处理函数绑定到 URL，这就是服务暴露的全部端点：

[swift/pipelines/infer/deploy.py:46-54](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L46-L54) —— 注册健康检查、模型列表、三类 OpenAI 端点与 swift 原生端点。

```python
def _register_app(self):
    self.app.get('/health')(self.health)
    self.app.get('/ping')(self.ping)
    self.app.post('/ping')(self.ping)
    self.app.get('/v1/models')(self.get_available_models)
    self.app.post('/v1/chat/completions')(self.create_chat_completion)
    self.app.post('/v1/completions')(self.create_completion)
    self.app.post('/v1/embeddings')(self.create_embedding)
    self.app.post('/infer/')(self.infer_handler)
```

端点分三组：
- `/health`、`/ping`：健康检查（`/ping` 兼容 SageMaker，见 [deploy.py:101-103](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L101-L103)）。
- `/v1/models`、`/v1/chat/completions`、`/v1/completions`、`/v1/embeddings`：**OpenAI 兼容端点**，任何 OpenAI 客户端可直接调用。
- `/infer/`：swift 原生批量端点，接收 `RolloutInferRequest` 列表，主要供 GRPO rollout 用（见 [deploy.py:229-235](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L229-L235)）。

**启动 uvicorn**。`run()` 是真正的服务启动点：

[swift/pipelines/infer/deploy.py:237-247](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L237-L247) —— 打印模型列表、可选开启结果落盘、用 uvicorn 监听端口。

```python
def run(self):
    args = self.args
    self.jsonl_writer = JsonlWriter(args.result_path) if args.result_path else None
    logger.info(f'model_list: {self._get_model_list()}')
    uvicorn.run(
        self.app,
        host=args.host,
        port=args.port,
        ssl_keyfile=args.ssl_keyfile,
        ssl_certfile=args.ssl_certfile,
        log_level=args.log_level)
```

`host`/`port`/`ssl_*`/`log_level` 全部来自 `DeployArguments`（默认 `host='0.0.0.0'`、`port=8000`，端口冲突时 `find_free_port` 自动找空闲端口，见 [deploy_args.py:52-57](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/deploy_args.py#L52-L57)）。

**后台统计线程**。`lifespan` 是 FastAPI 的生命周期钩子，在服务启动时拉起一个守护线程，每 `log_interval` 秒打印一次全局吞吐：

[swift/pipelines/infer/deploy.py:76-85](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L76-L85) —— 启动后台统计线程，服务退出时打印最终统计。

```python
def lifespan(self, app: FastAPI):
    args = self.args
    if args.log_interval > 0:
        thread = Thread(target=lambda: asyncio.run(self._log_stats_hook()), daemon=True)
        thread.start()
    try:
        yield
    finally:
        if args.log_interval > 0:
            self._compute_infer_stats()
```

#### 4.1.4 代码实践

**实践目标**：单卡部署一个文本模型服务，并验证 `/health` 与 `/v1/models` 端点可用。

**操作步骤**：

1. 选一个小模型（避免显存压力），启动部署服务：

```shell
CUDA_VISIBLE_DEVICES=0 swift deploy \
    --model Qwen/Qwen2.5-0.5B-Instruct \
    --infer_backend vllm \
    --port 8000 \
    --served_model_name my-qwen
```

2. 服务启动需要时间（vLLM 要加载权重、编译 kernel）。看到类似 `Uvicorn running on http://0.0.0.0:8000` 的日志后，另开一个终端做健康检查：

```shell
curl http://localhost:8000/health      # 期望: 空响应, HTTP 200
curl http://localhost:8000/v1/models   # 期望: 返回 {"data":[{"id":"my-qwen",...}],"object":"list"}
```

**需要观察的现象**：

- `swift deploy` 终端会先打印 `model_list: ['my-qwen']`（来自 `run()` 第一行），再打印 uvicorn 的启动日志。
- `/v1/models` 返回的 `id` 正是你传入的 `--served_model_name`；若未传则用 `model_suffix`（见 [deploy.py:87-92](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L87-L92) 的 `_get_model_list`）。
- 每 20 秒（`log_interval` 默认值）会打印一行全局吞吐统计。

**预期结果**：健康检查返回 200，模型列表含 `my-qwen`。**待本地验证**：实际能否拉起取决于本地是否已安装 vllm 且显存足够；若无 GPU 或未装 vllm，可把 `--infer_backend` 换成 `transformers` 降低门槛。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SwiftDeploy` 要覆写 `get_infer_engine` 而不是直接改 `SwiftInfer.get_infer_engine`？

> **参考答案**：因为 data-parallel + async-engine 的约束只在**部署**场景成立（`SwiftInfer` 的一次性推理不涉及常驻并发），改父类会污染所有推理场景。覆写让定制局部化，符合「开闭原则」——父类对扩展开放、对修改封闭。

**练习 2**：把 `--log_interval` 设为 `-1` 会发生什么？

> **参考答案**：`lifespan` 里 `if args.log_interval > 0` 不成立，后台统计线程不会启动，也不会周期打印吞吐；但每个请求若 `verbose=True` 仍会打印 `request_info`（见 [deploy.py:159-164](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L159-L164)）。

### 4.2 OpenAI 兼容协议：请求与响应的统一数据结构

#### 4.2.1 概念说明

ms-swift 的部署服务能被任何 OpenAI 客户端直接调用，靠的是一套精心设计的**双层协议**：

- **外层（HTTP 协议层）**：`ChatCompletionRequest` / `CompletionRequest` / `EmbeddingRequest` 与对应的 `*Response`，字段命名与 OpenAI 官方 API 对齐。这是客户端看到的接口。
- **内层（引擎协议层）**：`InferRequest` + `RequestConfig`（u6 已讲），这是推理引擎认识的接口。

两层之间用 `request.parse()` 桥接：把一个 OpenAI 风格的请求拆成「问什么」(`InferRequest`) 和「怎么答」(`RequestConfig`) 两件套，交给 `infer_async`。响应方向反过来：引擎产出 `ChatCompletionResponse`，直接序列化成 JSON 返回。

这种设计的好处是：**外层换协议、内层换引擎，互不干扰**。

#### 4.2.2 核心流程

一次 `POST /v1/chat/completions` 请求的处理流程：

```
HTTP POST /v1/chat/completions  (body: ChatCompletionRequest)
  │
  ├─ _check_model(request)        # model 字段必须在 model_list 里（支持多 LoRA 路由）
  ├─ _check_api_key(raw_request)  # 鉴权（若设置了 --api_key）
  ├─ _check_max_logprobs(request)# top_logprobs 不超过服务端上限
  │
  ├─ 取 adapter_path = adapter_mapping.get(request.model)   # 多 LoRA 路由
  ├─ infer_request, request_config = request.parse()        # 外层→内层
  ├─ _set_request_config(request_config)                    # 用部署默认值补全空字段
  │
  ├─ await infer_async(infer_request, request_config, adapter_request=...)
  │
  └─ if stream:  StreamingResponse(SSE, 每个 chunk: data: {...}\n\n, 末尾 data: [DONE])
     else:       JSONResponse(ChatCompletionResponse)
```

三个 OpenAI 端点共用一套核心逻辑：`/v1/completions` 与 `/v1/embeddings` 都先转换成 `ChatCompletionRequest` 再走 `create_chat_completion`，只是最后用 `to_cmpl_response()` 把响应格式转回 completion/embedding 形态。

#### 4.2.3 源码精读

**请求类：多重继承拼出 OpenAI 字段**。`ChatCompletionRequest` 同时继承 `RequestConfig`（采样参数）、`MultiModalRequestMixin`（images/videos）、`ChatCompletionRequestMixin`（model/messages/tools），一次拼齐 OpenAI 请求体的所有字段：

[swift/infer_engine/protocol.py:317-324](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/protocol.py#L317-L324) —— 用多继承把采样、多模态、对话三类字段扁平拼到一个请求类。

```python
@dataclass
class ChatCompletionRequest(RequestConfig, MultiModalRequestMixin, ChatCompletionRequestMixin):

    def __post_init__(self):
        RequestConfig.__post_init__(self)
        MultiModalRequestMixin.__post_init__(self)
        ChatCompletionRequestMixin.__post_init__(self)
        self.convert_to_base64()
```

注意 `__post_init__` 显式调用了三个父类的后置逻辑——这正是 u2-l1 讲过的「dataclass 多继承不会自动链式调用父类 `__post_init__`」的具体体现。`convert_to_base64()` 把本地图片/视频路径或 PIL 对象转成 `data:image/...;base64,...` 形式，保证 HTTP 传输自包含（见 [protocol.py:326-359](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/protocol.py#L326-L359)）。

**外层→内层的桥：parse()**。这是协议转换的关键。它把请求 `asdict` 后，按 `InferRequest` 与 `RequestConfig` 各自的字段集合分流，构造出引擎认识的「问什么 + 怎么答」：

[swift/infer_engine/protocol.py:361-368](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/protocol.py#L361-L368) —— 把 OpenAI 请求拆成 `(InferRequest, RequestConfig)`。

```python
def parse(self) -> Tuple['InferRequest', 'RequestConfig']:
    data = asdict(self)
    res = []
    for cls_type in [InferRequest, RequestConfig]:
        parameters = set(f.name for f in fields(cls_type))
        _data = {k: v for k, v in data.items() if k in parameters}
        res.append(cls_type(**_data))
    return tuple(res)
```

> 这个 `parse()` 在 u6-l1 已经从「引擎抽象」角度提过，本讲从「HTTP 入口」再看一次：它是部署服务把外部 OpenAI 请求「翻译」进引擎的咽喉。

**主处理函数：create_chat_completion**。这是 `/v1/chat/completions` 的实际处理逻辑，串联校验、协议转换、推理、流式/非流式响应：

[swift/pipelines/infer/deploy.py:176-219](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L176-L219) —— 校验三连、多 LoRA 路由、parse、infer_async、按 stream 分支返回。

```python
async def create_chat_completion(self, request, raw_request, *, return_cmpl_response=False):
    args = self.args
    error_msg = (await self._check_model(request) or self._check_api_key(raw_request)
                 or self._check_max_logprobs(request))
    if error_msg:
        return self.create_error_response(HTTPStatus.BAD_REQUEST, error_msg)
    infer_kwargs = self.infer_kwargs.copy()
    adapter_path = args.adapter_mapping.get(request.model)       # 多 LoRA 路由
    if adapter_path:
        infer_kwargs['adapter_request'] = AdapterRequest(request.model, adapter_path)

    infer_request, request_config = request.parse()              # 外层→内层
    self._set_request_config(request_config)                     # 部署默认值补全
    ...
    res_or_gen = await self.infer_async(infer_request, request_config, **infer_kwargs)
    if request_config.stream:                                    # 流式分支
        async def _gen_wrapper():
            async for res in res_or_gen:
                res = self._post_process(request_info, res, return_cmpl_response)
                yield f'data: {json.dumps(asdict(res), ensure_ascii=False)}\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(_gen_wrapper(), media_type='text/event-stream')
    elif hasattr(res_or_gen, 'choices'):
        return self._post_process(request_info, res_or_gen, return_cmpl_response)  # 非流式
    ...
```

**多 LoRA 路由的精妙之处**：`request.model` 既用来校验合法性，又作为 `adapter_mapping` 的 key 选出对应的 LoRA 路径，包成 `AdapterRequest` 传给引擎。于是一份基座可以同时服务多个 LoRA，客户端只需在 `model` 字段填 LoRA 名字即可切换。`_get_model_list` 也会把所有 adapter 名字一并暴露到 `/v1/models`（[deploy.py:87-92](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L87-L92)）。

**三道校验**：

- `_check_model`（[deploy.py:110-114](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L110-L114)）：`request.model` 必须在 `model_list`（基座名 + 各 adapter 名）里，否则 400。
- `_check_api_key`（[deploy.py:116-126](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L116-L126)）：若部署时设了 `--api_key`，则请求头必须有正确的 `Authorization: Bearer <key>`。
- `_check_max_logprobs`（[deploy.py:128-132](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L128-L132)）：`top_logprobs` 不能超过服务端 `max_logprobs`。

三个校验用 `or` 短路串联，任一失败即返回错误响应——简洁的错误聚合写法。

**默认值补全：_set_request_config**。客户端可以只传 `messages`，其余采样参数由部署时的 `--temperature`/`--max_new_tokens` 等兜底：

[swift/pipelines/infer/deploy.py:167-174](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L167-L174) —— 当请求某字段为空时，回填部署参数里的默认值。

```python
def _set_request_config(self, request_config) -> None:
    default_request_config = self.args.get_request_config()
    if default_request_config is None:
        return
    for key, val in asdict(request_config).items():
        default_val = getattr(default_request_config, key)
        if default_val is not None and (val is None or isinstance(val, (list, tuple)) and len(val) == 0):
            setattr(request_config, key, default_val)
```

**completion / embedding 复用 chat 逻辑**。两个端点都先 `from_cmpl_request` 转成 chat 请求，再调用 `create_chat_completion` 并请求 `return_cmpl_response=True`，最后用 `to_cmpl_response()` 把响应转回 completion/embedding 形态：

[swift/pipelines/infer/deploy.py:221-227](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L221-L227) —— completion / embedding 共用 chat 主链路。

```python
async def create_completion(self, request: CompletionRequest, raw_request: Request):
    chat_request = ChatCompletionRequest.from_cmpl_request(request)
    return await self.create_chat_completion(chat_request, raw_request, return_cmpl_response=True)

async def create_embedding(self, request: EmbeddingRequest, raw_request: Request):
    chat_request = ChatCompletionRequest.from_cmpl_request(request)
    return await self.create_chat_completion(chat_request, raw_request, return_cmpl_response=True)
```

**响应结构：对齐 OpenAI 并扩展**。`ChatCompletionResponse` 字段（`model`/`choices`/`usage`/`id`/`object`/`created`）与 OpenAI 完全一致，同时扩展了 `prompt_token_ids`、`prompt_logprobs`、`images_size` 等 swift 专属字段，供评测与 GRPO 复用：

[swift/infer_engine/protocol.py:457-467](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/protocol.py#L457-L467) —— 响应数据类，`object='chat.completion'` 与 OpenAI 对齐。

```python
@dataclass
class ChatCompletionResponse:
    model: str
    choices: List[ChatCompletionResponseChoice]
    usage: UsageInfo
    id: str = field(default_factory=lambda: f'chatcmpl-{random_uuid()}')
    object: str = 'chat.completion'
    created: int = field(default_factory=lambda: int(time.time()))
    prompt_token_ids: Optional[List[int]] = None
    prompt_logprobs: Optional[List] = None
    images_size: Optional[List[Tuple[int, int]]] = None
```

**客户端：InferClient**。`swift app` 与外部 Python 调用都通过 `InferClient` 走 HTTP。它继承 `InferEngine`，把 `infer_async` 实现为「拼一个 `ChatCompletionRequest` POST 给服务」：

[swift/infer_engine/infer_client.py:120-158](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/infer_client.py#L120-L158) —— `infer_async` 内部就是 POST `/chat/completions`，流式时按 `data:` 行解析。

```python
async def infer_async(self, infer_request, request_config=None, *, model=None):
    ...
    url = f"{self.base_url.rstrip('/')}/chat/completions"
    request_data = self._prepare_request_data(model, infer_request, request_config)
    if request_config.stream:
        async def _gen_stream():
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=request_data, **self._get_request_kwargs()) as resp:
                    async for data in resp.content:
                        data = self._parse_stream_data(data)
                        if data == '[DONE]':
                            break
                        ...
                        yield from_dict(ChatCompletionStreamResponse, resp_obj)
        return _gen_stream()
    else:
        ...
        return from_dict(ChatCompletionResponse, resp_obj)
```

`_prepare_request_data`（[infer_client.py:99-109](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/infer_client.py#L99-L109)）把 `InferRequest`+`RequestConfig` 重新拼回 `ChatCompletionRequest` 并剔除空字段——与 `parse()` 是互逆操作。这正是 u6-l1 强调的「本地推理与调远端服务对上层透明」的实现：同一个 `infer_async` 接口，本地引擎走模型、`InferClient` 走 HTTP，上层无感知。

#### 4.2.4 代码实践

**实践目标**：用三种客户端（curl / OpenAI SDK / `InferClient`）调用同一个部署服务，验证协议兼容性。

**前置**：先按 4.1.4 启动 `swift deploy` 服务（`--served_model_name my-qwen`，端口 8000）。

**方法 1：curl**（验证原始 HTTP 协议）

```shell
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "my-qwen",
    "messages": [{"role": "user", "content": "你是谁？请用一句话回答。"}],
    "max_tokens": 64,
    "temperature": 0
  }'
```

**方法 2：OpenAI 官方 SDK**（验证「OpenAI 兼容」承诺）

```python
# 示例代码（非项目内置）
from openai import OpenAI
client = OpenAI(api_key='EMPTY', base_url='http://127.0.0.1:8000/v1')
resp = client.chat.completions.create(
    model='my-qwen',
    messages=[{'role': 'user', 'content': '你是谁？请用一句话回答。'}],
    max_tokens=64, temperature=0)
print(resp.choices[0].message.content)
```

**方法 3：swift 原生 InferClient**（项目内置客户端，见 [Inference-and-deployment.md](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source_en/Instruction/Inference-and-deployment.md) Method 3）

```python
# 示例代码
from swift import InferClient, InferRequest, RequestConfig
engine = InferClient(host='127.0.0.1', port=8000)
resp = engine.infer(
    [InferRequest(messages=[{'role': 'user', 'content': '你是谁？请用一句话回答。'}])],
    RequestConfig(max_tokens=64, temperature=0))
print(resp[0].choices[0].message.content)
```

**需要观察的现象**：

- 三种方式返回的回答内容应当一致（`temperature=0` 下近似确定性）。
- curl 的原始返回体应包含 `object: "chat.completion"`、`choices[0].message.content`、`usage` 三段——与 OpenAI 官方字段对齐。
- 把 `model` 改成一个不在 `/v1/models` 列表里的名字，应收到 400 错误 `"xxx is not in the model_list"`。

**预期结果**：三种客户端都能成功调用并拿到回答。**待本地验证**：取决于服务是否成功启动；若未装 vllm，改用 `--infer_backend transformers` 同样可验证协议（只是更慢）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `/v1/completions` 和 `/v1/embeddings` 能直接复用 `create_chat_completion`？

> **参考答案**：`from_cmpl_request` 把 completion 的 `prompt`（或 embedding 的 `input`）包成一条 `{'role':'user','content':prompt}` 的 messages，从而复用整条 chat 处理链路；`return_cmpl_response=True` 让响应在最后经 `to_cmpl_response()` 转回 completion/embedding 的 `text`/`embedding` 形态。一套主链路服务三种协议，避免重复实现。

**练习 2**：流式响应里，服务端如何在客户端「中途断开」时避免资源泄漏？

> **参考答案**：`_gen_wrapper` 是 `async` 生成器，FastAPI/Starlette 的 `StreamingResponse` 在客户端断开时会取消迭代，生成器随之抛出 `GeneratorExit`/`CancelledError`，引擎侧的 `infer_async` 异步迭代也会被终止，不再继续生成 token。

### 4.3 app Gradio 界面：用 run_deploy 拉起服务再套交互层

#### 4.3.1 概念说明

`swift app` 是「零门槛交互」入口：它本质上还是 `swift deploy`（同一个 OpenAI 兼容服务），只是在服务前面再套一个 **Gradio 聊天网页**。这样不熟悉 curl/SDK 的用户也能直接在浏览器里对话、传图。

`swift app` 有两种工作模式，由 `--base_url` 参数决定：

- **自带服务模式**（`base_url` 为空，默认）：自己用 `run_deploy` 在子进程里拉起一个部署服务，再启动 Gradio 连它。一条命令搞定一切。
- **外接服务模式**（`base_url` 指向已有服务）：不重复部署，Gradio 直接连指定的 OpenAI 兼容端点。适合「服务已在别处跑好，只想要个界面」的场景。

#### 4.3.2 核心流程

```
swift app --model ...                # 或 --base_url http://...:8000/v1
  └─ SwiftApp.run()
       ├─ if args.base_url:  deploy_context = nullcontext()       # 外接模式
       │  else:              deploy_context = run_deploy(args, return_url=True)  # 自带模式
       └─ with deploy_context as base_url:
            ├─ base_url = base_url or args.base_url
            ├─ demo = build_ui(base_url, model, request_config, is_multimodal, ...)
            └─ demo.queue(default_concurrency_limit=...).launch(server_name, server_port, share)
```

自带模式下，`run_deploy` 是一个**上下文管理器**：进入时 spawn 子进程跑 `deploy_main`，轮询直到服务可访问，`yield` 出 base_url 给 Gradio 用；退出时 `terminate` 子进程。这样 Gradio 与部署服务的生命周期被绑死——关掉 app，部署服务也一并退出。

#### 4.3.3 源码精读

**SwiftApp 管道**。`SwiftApp` 继承 `SwiftPipeline`，`run()` 是全部逻辑：

[swift/pipelines/app/app.py:16-39](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/app/app.py#L16-L39) —— 决定自带/外接模式、构建 UI、启动 Gradio。

```python
class SwiftApp(SwiftPipeline):
    args_class = AppArguments
    args: args_class

    def run(self):
        args = self.args
        deploy_context = nullcontext() if args.base_url else run_deploy(args, return_url=True)
        with deploy_context as base_url:
            base_url = base_url or args.base_url
            demo = build_ui(
                base_url,
                args.model_suffix,
                request_config=args.get_request_config(),
                is_multimodal=args.is_multimodal,
                studio_title=args.studio_title,
                lang=args.lang,
                default_system=args.system)
            concurrency_count = 1 if args.infer_backend == 'transformers' else 16
            if version.parse(gradio.__version__) < version.parse('4'):
                queue_kwargs = {'concurrency_count': concurrency_count}
            else:
                queue_kwargs = {'default_concurrency_limit': concurrency_count}
            demo.queue(**queue_kwargs).launch(
                server_name=args.server_name, server_port=args.server_port, share=args.share)
```

注意并发上限 `concurrency_count` 按 `infer_backend` 区分：`transformers` 后端是同步阻塞生成，只能并发 1；vllm/sglang/lmdeploy 是异步高并发引擎，开到 16。这是对引擎并发能力的正确认知体现。

**run_deploy：子进程管理器**。这是「app 复用 deploy」的核心：

[swift/pipelines/infer/deploy.py:268-289](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L268-L289) —— spawn 子进程跑部署、轮询可访问性、退出时终止。

```python
@contextmanager
def run_deploy(args: DeployArguments, return_url: bool = False):
    ...
    deploy_args = DeployArguments(**args_dict)        # 把 AppArguments 收窄回 DeployArguments
    mp = multiprocessing.get_context('spawn')        # 用 spawn 而非 fork（CUDA 友好）
    process = mp.Process(target=_deploy_main, args=(deploy_args, ))
    process.start()
    try:
        while not is_accessible(deploy_args.port):   # 轮询直到 /v1/models 可访问
            time.sleep(1)
        yield f'http://127.0.0.1:{deploy_args.port}/v1' if return_url else deploy_args.port
    finally:
        process.terminate()
        logger.info('The deployment process has been terminated.')
```

几个细节值得注意：
- **用 `spawn` 而非 `fork`**：CUDA 和许多推理框架在 fork 子进程时会出问题，`spawn` 启动全新解释器更安全。
- **`is_accessible` 轮询**：用 `InferClient` 调一次 `get_model_list()`，成功才认为服务就绪（[deploy.py:254-260](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L254-L260)），避免 Gradio 在模型还没加载完时就发起请求。
- **`AppArguments → DeployArguments` 收窄**：因为 `AppArguments` 含一些 UI 专属字段，子进程只需部署相关参数，故按 `DeployArguments` 的签名过滤（[deploy.py:273-278](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L273-L278)）。

**build_ui：Gradio 界面构建**。`build_ui` 用 `gr.Blocks` 搭聊天界面，所有推理调用都走 `InferClient(base_url=base_url)`——和外部 HTTP 客户端完全一样，没有特殊通道：

[swift/pipelines/app/build_ui.py:96-137](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/app/build_ui.py#L96-L137) —— 构建 Blocks、绑定按钮事件、用 InferClient 调服务。

```python
def build_ui(base_url, model=None, *, request_config=None, is_multimodal=True, ...):
    client = InferClient(base_url=base_url)
    model = model or client.models[0]
    ...
    with gr.Blocks() as demo:
        gr.Markdown(f'<center><font size=8>{studio_title}</center>')
        ...
        chatbot = gr.Chatbot(label='Chatbot')
        textbox = gr.Textbox(lines=1, label='Input')
        ...
        model_chat_ = partial(model_chat, client=client, model=model, request_config=request_config)
        textbox.submit(add_text, ...).then(model_chat_, [chatbot, history_state, system_state], [chatbot, history_state])
        submit.click(add_text, ...).then(model_chat_, ...)
        ...
    return demo
```

`model_chat` 是异步生成器，把界面历史转成 messages，调 `client.infer_async`，逐 token 更新聊天气泡（流式时）：

[swift/pipelines/app/build_ui.py:53-74](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/app/build_ui.py#L53-L74) —— 流式逐 chunk 拼接回答并刷新界面。

```python
async def model_chat(history, real_history, system, *, client, model, request_config):
    if history:
        messages = _history_to_messages(real_history, system)
        resp_or_gen = await client.infer_async(
            InferRequest(messages=messages), request_config=request_config, model=model)
        if request_config and request_config.stream:
            response = ''
            async for resp in resp_or_gen:
                if resp is None:
                    continue
                response += resp.choices[0].delta.content
                history[-1][1] = _parse_text(response)
                real_history[-1][-1] = response
                yield history, real_history
        ...
```

**AppArguments：组合两个参数家族**。`AppArguments` 多继承 `WebUIArguments`（Gradio 服务参数：`server_name`/`server_port`/`share`）与 `DeployArguments`（部署参数），再加 `base_url`/`studio_title`/`is_multimodal`/`lang` 四个 UI 专属字段：

[swift/arguments/app_args.py:14-39](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/app_args.py#L14-L39) —— 组合 WebUI + Deploy 参数，`base_url` 决定自带/外接模式。

```python
@dataclass
class AppArguments(WebUIArguments, DeployArguments):
    base_url: Optional[str] = None
    studio_title: Optional[str] = None
    is_multimodal: Optional[bool] = None
    lang: Literal['en', 'zh'] = 'en'
    verbose: bool = False      # app/eval 下默认关闭详细请求日志
    stream: bool = True        # app 下默认开流式
    ...
```

注意 `verbose` 在 `DeployArguments` 默认 `True`，但 `AppArguments` 覆盖为 `False`（界面场景不需要刷屏日志）；`stream` 同理默认 `True`。这是 u2 讲过的「派生参数类覆盖父类默认值」的实例。

#### 4.3.4 代码实践

**实践目标**：用 `swift app` 一键拉起带 Gradio 界面的多模态聊天服务，并对比它与 `swift deploy` 的体验差异。

**操作步骤**：

1. 参考项目内置示例 [examples/app/mllm.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/app/mllm.sh)，启动一个多模态 app：

```shell
CUDA_VISIBLE_DEVICES=0 \
MAX_PIXELS=1003520 \
swift app \
    --model Qwen/Qwen2.5-VL-3B-Instruct \
    --stream true \
    --infer_backend vllm \
    --vllm_max_model_len 8192 \
    --lang zh
```

2. 终端会先打印部署子进程的启动日志（vLLM 加载），随后 Gradio 打印一个本地 URL（类似 `http://127.0.0.1:7860`）。
3. 在浏览器打开该 URL，在输入框提问、用上传按钮传图片测试多模态。

**需要观察的现象**：

- `swift app` 启动日志里能看到**两段**：先是部署服务（vLLM/transformers）的加载日志，后是 Gradio 的 `Running on local URL`。
- 关闭 `swift app`（Ctrl+C）时，日志会打印 `The deployment process has been terminated.`——证明 `run_deploy` 的 `finally` 把部署子进程也一并终止了。
- 体验对比：`swift deploy` 只暴露 API（需自己写客户端），`swift app` 直接给可视化界面，适合 demo / 内部试用。

**外接模式练习**：先在终端 A 跑 `swift deploy --port 8000 ...`，再在终端 B 跑 `swift app --base_url http://127.0.0.1:8000/v1 --model_suffix <部署时的模型名>`。此时 `swift app` 不会再加载模型（`_init_torch_dtype` 检测到 `base_url` 直接返回，见 [app_args.py:41-46](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/app_args.py#L41-L46)），只起 Gradio 连接已有服务。

**预期结果**：浏览器能正常对话与传图；外接模式下 `swift app` 启动极快（不加载模型）。**待本地验证**：多模态需 vllm/transformers 与足够显存；无 GPU 可换纯文本小模型 + `transformers` 后端验证界面流程。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `run_deploy` 用 `multiprocessing.get_context('spawn')` 而不是默认的 `fork`？

> **参考答案**：`fork` 会复制父进程的内存状态，而 PyTorch/CUDA 运行时、vLLM 等在 fork 后的子进程中常常出错（CUDA 上下文不能跨 fork 共享）。`spawn` 启动全新的解释器进程、重新 import，避免了这些 CUDA/fork 不兼容问题。

**练习 2**：如果想让 `swift app` 连接一个公司内部已有的 vllm 服务，应该传哪些参数？还会在本地加载模型吗？

> **参考答案**：传 `--base_url http://<host>:<port>/v1`（以及 `--model_suffix` 指定模型名）。不会在本地加载模型——`AppArguments._init_torch_dtype` 检测到 `base_url` 后，只取 `model_meta` 而把 `model_info` 置 None 直接返回，`SwiftApp.run` 也走 `nullcontext()` 分支不启动子进程。

## 5. 综合实践

把本讲三个模块串起来，完成一个「**多 LoRA 部署 + OpenAI 客户端切换 + Gradio 界面**」的完整链路：

1. **准备多个 LoRA adapter**。若你没有现成的，可参考 [examples/deploy/lora/server.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/deploy/lora/server.sh) 用项目内置的 `swift/test_lora` 与 `swift/test_lora2`（它们带 `args.json`，无需显式传 `--model`）。

2. **部署多 LoRA 服务**（一条命令挂两个 adapter，给它们命名 `lora1`/`lora2`）：

```shell
CUDA_VISIBLE_DEVICES=0 swift deploy \
    --host 0.0.0.0 --port 8000 \
    --adapters lora1=swift/test_lora lora2=swift/test_lora2 \
    --infer_backend vllm
```

3. **用 OpenAI SDK 验证多 LoRA 路由**：分别用 `model='lora1'` 和 `model='lora2'` 调用同一个服务，观察回答差异（两个 adapter 行为不同）。同时调 `/v1/models`，确认返回的列表同时含 `lora1` 和 `lora2`（对应 [deploy.py:87-92](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L87-L92)）。

4. **叠加 Gradio 界面**：在另一个终端用外接模式启动 app，连到上面这个服务：

```shell
swift app --base_url http://127.0.0.1:8000/v1 --model_suffix lora1 --lang zh
```

5. **梳理调用链**：在笔记里画出「浏览器 → Gradio → InferClient → HTTP → SwiftDeploy → infer_async → 引擎 → ChatCompletionResponse → HTTP 回流 → Gradio 刷新气泡」的完整数据流，并标注每一步对应的源码文件。

**验收标准**：能说清为什么第 3 步换 `model` 字段就能切 LoRA（答案在 `adapter_mapping.get(request.model)` 与 `AdapterRequest`）；能说清第 4 步为什么 app 启动很快、不重复加载模型（答案在 `base_url` 分支）。**待本地验证**：完整跑通需要 GPU、vllm 与可用的 LoRA 权重。

## 6. 本讲小结

- `swift deploy` 与 `swift infer` 同属 `SwiftInfer` 家族，共享模型/模板/引擎装配；区别在 `run()`——infer 跑完即退，deploy 用 `uvicorn.run` 常驻成 HTTP 服务。`SwiftDeploy` 只覆写 `get_infer_engine`（vLLM data-parallel 定制）和 `run`（启动 uvicorn），是「钩子式扩展」的范例。
- 部署服务的全部端点由 `_register_app` 注册：`/health`、`/ping`、`/v1/models`、`/v1/chat/completions`、`/v1/completions`、`/v1/embeddings`、`/infer/`。前三类 `/v1/*` 是 **OpenAI 兼容**端点。
- OpenAI 兼容靠**双层协议**实现：外层 `ChatCompletionRequest`（多继承 `RequestConfig + MultiModalRequestMixin + ChatCompletionRequestMixin`）经 `parse()` 拆成内层 `(InferRequest, RequestConfig)` 交给 `infer_async`；响应 `ChatCompletionResponse` 字段与 OpenAI 对齐并扩展。completion/embedding 复用 chat 主链路。
- **多 LoRA 路由**：`request.model` 既校验合法性又作为 `adapter_mapping` 的 key 选出 adapter 路径，一份基座可同时服务多个 LoRA。
- 流式输出用 SSE：`StreamingResponse` 每个生成 chunk 发 `data: {...}\n\n`，末尾发 `data: [DONE]\n\n`。`InferClient.infer_async` 是其逆操作。
- `swift app` = `run_deploy`（子进程拉部署）+ Gradio。`run_deploy` 是上下文管理器，用 `spawn` 起子进程、轮询 `is_accessible`、退出时 `terminate`。`--base_url` 决定自带/外接模式。
- 部署模式的可观测性：`infer_engine.strict=True`（错误如实抛 400）、`InferStats` 周期打印吞吐、可选 `JsonlWriter` 落盘每次请求。

## 7. 下一步学习建议

- **评测**：本讲的 OpenAI 兼容服务是评测的天然后端。下一讲 [u8-l3 模型评测](./u8-l3-model-evaluation.md) 会讲 `swift eval` 如何以 EvalScope 为后端评测模型，届时你会看到 `infer_backend` 选择如何影响评测速度——本讲的部署服务就是评测可复用的推理入口。
- **导出与量化**：若想把训练产物转成更适合部署的形态（合并 LoRA、FP8 量化、ollama 导出），回顾 [u8-l1 模型导出与量化](./u8-l1-export-and-quantization.md)。`swift deploy` 加载量化模型只需 `--model <量化模型id>`。
- **强化学习的 rollout**：本讲的 `/infer/` 端点与 `RolloutArguments`（继承 `DeployArguments`）是 GRPO rollout 的服务化形态。学到 [u7 GRPO](./u7-l2-grpo-algorithm-core.md) 时你会看到 `GRPOVllmEngine` 如何复用这套部署抽象做在线 rollout。
- **源码延伸阅读**：想深入流式与并发，可读 [swift/infer_engine/infer_engine.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/infer_engine.py) 的 `async_iter_to_iter`（异步生成器转同步迭代器）与 `_batch_infer_stream`；想自定义部署行为，可研究 `DeployArguments` 的各字段与 `lifespan` 钩子。
