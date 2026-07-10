# HTTP API 服务与请求分发

## 1. 本讲目标

本讲承接 u2-l1「多进程架构总览」建立的全局地图，聚焦请求生命周期的第一个进程——**HttpServer**。读完本讲你应该能够：

- 说出 LightLLM 通过 FastAPI 暴露了哪些 HTTP/WebSocket 端点，以及 `/generate`、`/generate_stream`、`/v1/chat/completions` 三类端点各自的入口函数。
- 跟踪一次请求从「HTTP 报文到达」到「`send_to_router` 把请求索引投递给 Router 进程」的完整代码路径，并能解释中间的 **分词（tokenize）、长度校验、共享内存 Req 分配、分发选路** 四个阶段。
- 解释生成结果如何通过 **zmq SUB 回流** 到 HttpServer，再以流式或一次性方式返回给客户端。

本讲覆盖三个最小模块：**HTTP 路由**、**请求分发**、**结果回流**。

## 2. 前置知识

- **FastAPI 与 uvicorn/hypercorn**：FastAPI 是基于 ASGI 的 Python Web 框架，用装饰器 `@app.post("/path")` 注册路由；hypercorn 是真正监听 TCP 端口、跑事件循环的 ASGI 服务器。LightLLM 把 FastAPI 当路由层，由 hypercorn 拉起。
- **async/await 与 AsyncGenerator**：HttpServer 是单进程多协程的。一次请求的「推理」耗时很长，不能阻塞事件循环，所以结果以 `async for ... yield` 的异步生成器逐步产出。
- **zmq 套接字模型**：`PUSH`/`PULL` 是单向投递（多生产者多消费者自动负载均衡），`PUB`/`SUB` 是广播订阅。HttpServer 用 `PUSH` 把请求发出去，用 `SUB` 把结果收回来。
- **共享内存（shared memory）**：LightLLM 的核心设计是「对象放共享内存、线上只传索引」。`Req` 这种大对象住在共享内存里，进程间只传它在共享内存里的 `index_in_shm_mem`。这就是为什么 Docker 启动必须加 `--shm-size`（见 u1-l2）。
- **tokenize / detokenize**：把文本切成整数 token id 叫分词（encode），把 token id 拼回文本叫反分词（decode）。HttpServer 负责 encode，Detokenization 进程负责 decode（见 u2-l7）。

如果对多进程整体拓扑还不清楚，建议先读 u2-l1。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lightllm/server/api_http.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_http.py) | FastAPI 路由层，注册全部 HTTP/WebSocket 端点，持有全局对象 `g_objs`，是请求的总入口。 |
| [lightllm/server/httpserver/manager.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py) | `HttpServerManager`：真正干活的人。负责分词、长度校验、共享内存 Req 分配、向下游分发、回收结果。 |
| [lightllm/server/api_lightllm.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_lightllm.py) | `/generate`、`/generate_stream` 的具体实现 `lightllm_generate` / `lightllm_generate_stream`，把请求体解析成 `SamplingParams` 并消费 `HttpServerManager.generate()` 产出的 token 流。 |
| [lightllm/server/api_openai.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_openai.py) | OpenAI 兼容端点 `/v1/chat/completions`、`/v1/completions` 的实现 `chat_completions_impl` / `completions_impl`。 |
| [lightllm/server/core/objs/io_objs/group_req.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/io_objs/group_req.py) | `GroupReqObjs` / `GroupReqIndexes` 数据类，封装「一组请求」并实现对象→索引的转换 `to_group_req_index()`。 |
| [lightllm/server/core/objs/req.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py) | `FinishStatus`：token 是否生成完毕的状态机（`NO_FINISH`/`FINISHED_STOP`/`FINISHED_LENGTH`）。 |

## 4. 核心概念与源码讲解

### 4.1 HTTP 路由（FastAPI 端点与全局对象）

#### 4.1.1 概念说明

「路由」要解决的问题是：客户端用一个 HTTP 报文（比如 `POST /generate`）打过来，框架怎么把它映射到一段 Python 代码。

LightLLM 用 FastAPI 的装饰器风格注册路由，整个 `api_http.py` 几乎就是一个路由表 + 一个全局对象容器 `g_objs`。所有端点函数都很薄——它们只做三件事：**PD 模式拦截、异常翻译、把活儿转交给具体实现函数**。真正干活的实现函数（`lightllm_generate` 等）和真正持有 zmq 套接字的 `HttpServerManager` 都被装在 `g_objs` 里。

#### 4.1.2 核心流程

```text
hypercorn 监听端口
   └─ ASGI app (FastAPI)
        └─ _AccessLogMiddleware（记录访问日志）
             └─ @app.post("/generate")  → generate()
                  ├─ run_mode 拦截：prefill/decode 模式拒绝 HTTP 请求
                  ├─ 调用 g_objs.g_generate_func(request, httpserver_manager)
                  │     （g_generate_func 默认指向 lightllm_generate）
                  └─ 把异常翻译成对应的 HTTP 状态码
```

启动时还有两个关键钩子：

- `@app.on_event("startup")`：在事件循环就绪后调用 `g_objs.set_args()` 完成全局对象初始化，并 `create_task(httpserver_manager.handle_loop())` 启动**结果回流循环**（见 4.3）。
- `@app.on_event("shutdown")`：优雅关闭时递归 kill 所有子进程。

#### 4.1.3 源码精读

**全局对象容器 `g_objs`。** 它是进程级的单例，集中保存 FastAPI app、参数、`httpserver_manager`、`metric_client`、共享的 `token_load` 等。`set_args` 根据 `run_mode` 决定实例化哪种 manager：

```python
# lightllm/server/api_http.py:69-80 —— 全局对象，进程级单例
@dataclass
class G_Objs:
    app: FastAPI = None
    metric_client: MetricClient = None
    args: StartArgs = None
    g_generate_func: Callable = None          # /generate 用哪个实现
    g_generate_stream_func: Callable = None   # /generate_stream 用哪个实现
    httpserver_manager: Union[HttpServerManager, HttpServerManagerForPDMaster] = None
    shared_token_load: TokenLoad = None
    model_created: int = None
```

`set_args` 里还有一处体现「同一端点、多套实现」的设计：当 `use_tgi_api=True` 时，`/generate` 会改用 TGI 兼容的 `tgi_generate_impl`，否则用 LightLLM 原生的 `lightllm_generate`（见 [api_http.py:82-94](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_http.py#L82-L94)）。

**`/generate` 端点。** 典型的「薄端点」：先拦截 PD 子节点，再委托给实现函数，最后把异常翻译成 HTTP 状态码：

```python
# lightllm/server/api_http.py:231-250
@app.post("/generate")
async def generate(request: Request) -> Response:
    if get_env_start_args().run_mode in ["prefill", "decode"]:
        return create_error_response(
            HTTPStatus.EXPECTATION_FAILED, "service in pd mode dont recv reqs from http interface")
    try:
        return await g_objs.g_generate_func(request, g_objs.httpserver_manager)
    except ServerBusyError as e:
        return create_error_response(HTTPStatus.SERVICE_UNAVAILABLE, str(e))
    except ValueError as e:
        return create_error_response(HTTPStatus.BAD_REQUEST, str(e))
    except ClientDisconnected as e:
        return Response(status_code=499)   # 客户端断开，非标准但 Nginx 常用
    except Exception as e:
        return create_error_response(HTTPStatus.EXPECTATION_FAILED, str(e))
```

注意几个细节：① PD 分离模式下 prefill/decode 子节点不再从 HTTP 接请求（请求由 pd_master 转发），所以直接返回 417；② `ClientDisconnected`（客户端中途关连接）映射成 499 而非 5xx，避免误报为服务端错误；③ `ServerBusyError` 映射成 503。

**OpenAI 兼容端点 `/v1/chat/completions`。** 它用 FastAPI 的 pydantic 模型 `ChatCompletionRequest` 自动校验请求体，然后委托给 `chat_completions_impl`：

```python
# lightllm/server/api_http.py:306-320
@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(request: ChatCompletionRequest, raw_request: Request) -> Response:
    if get_env_start_args().run_mode in ["prefill", "decode"]:
        return create_error_response(HTTPStatus.EXPECTATION_FAILED, ...)
    try:
        resp = await chat_completions_impl(request, raw_request)
    except ValueError as e:
        return create_error_response(HTTPStatus.BAD_REQUEST, str(e))
    except ClientDisconnected as e:
        return Response(status_code=499)
    return resp
```

**兼容端点 `/`。** 一个小而实用的设计：根路径根据 body 里的 `stream` 字段分发到流式或非流式处理，方便只改一个 URL 的老客户端：

```python
# lightllm/server/api_http.py:291-303
@app.post("/")
async def compat_generate(request: Request) -> Response:
    request_dict = await request.json()
    stream = request_dict.pop("stream", False)
    if stream:
        return await generate_stream(request)
    else:
        return await generate(request)
```

**访问日志中间件。** 包裹 ASGI app，按状态码着色打印 `METHOD path status` 一行日志，方便排查请求是否到达、返回了什么状态（[api_http.py:123-151](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_http.py#L123-L151)）。

**startup 钩子。** 这是理解「结果回流」的入口，先记住它启动了 `handle_loop` 这个协程任务：

```python
# lightllm/server/api_http.py:492-499
@app.on_event("startup")
async def startup_event():
    loop = asyncio.get_event_loop()
    g_objs.set_args(get_env_start_args())
    loop.create_task(g_objs.httpserver_manager.handle_loop())  # 启动结果回流循环
```

除推理端点外，`api_http.py` 还注册了一批运维端点：`/liveness`、`/readiness`、`/health`（健康检查）、`/token_load`（当前 token 负载）、`/metrics`（Prometheus 指标）、`/v1/models`（模型列表）、`/tokens`（预估 token 数），以及 PD 分离专用的两个 WebSocket `/pd_register`、`/kv_move_status`。它们都不是推理主链路，本讲不展开。

#### 4.1.4 代码实践

**实践目标**：在源码层面完整列出 `api_http.py` 的端点表，并定位 `/generate` 与 `/v1/chat/completions` 的处理函数。

**操作步骤**：

1. 在 `api_http.py` 中搜索所有 `@app.` 装饰器，记录每个端点的 `方法 + 路径 + 处理函数名`。
2. 对 `/generate`：找到它调用的 `g_objs.g_generate_func`，再追到 `set_args` 里看默认指向 `lightllm_generate`（[api_lightllm.py:32](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_lightllm.py#L32)）。
3. 对 `/v1/chat/completions`：确认它调用 `chat_completions_impl`（[api_openai.py:228](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_openai.py#L228)）。

**需要观察的现象 / 预期结果**：你会得到一张类似下表的映射，注意两个端点的「薄包装」结构完全一致——拦截 + 委托 + 异常翻译：

| 端点 | 处理函数 | 最终实现 |
| --- | --- | --- |
| `POST /generate` | `generate` | `g_objs.g_generate_func` → `lightllm_generate` |
| `POST /generate_stream` | `generate_stream` | `g_objs.g_generate_stream_func` → `lightllm_generate_stream` |
| `POST /v1/chat/completions` | `chat_completions` | `chat_completions_impl` |
| `POST /v1/completions` | `completions` | `completions_impl` |
| `POST /` | `compat_generate` | 按 `stream` 字段分流到上面两个 |

#### 4.1.5 小练习与答案

**练习 1**：为什么 `/generate` 要在函数开头判断 `run_mode in ["prefill", "decode"]` 并直接返回错误？

**参考答案**：在 PD 分离部署下，prefill/decode 是两个独立的推理节点，它们不直接对外提供 HTTP 服务，请求由 pd_master 节点统一接收后再在内部转发。如果有人误连到子节点的 HTTP 端口，应尽早拒绝，避免请求进入无效的调度链路。

**练习 2**：若想让 `/generate` 改用 TGI 兼容的返回格式，应该改哪里？

**参考答案**：不改端点。启动时加 `--use_tgi_api`，`set_args` 会把 `g_generate_func` 指向 `tgi_generate_impl`（[api_http.py:87-89](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_http.py#L87-L89)），端点本身无需改动。

---

### 4.2 请求分发（从端点到 Router/Visual）

#### 4.2.1 概念说明

「请求分发」要解决的问题是：HTTP 端点拿到原始报文后，怎么把它变成 Router 进程能消费的东西。

注意一个关键区分：`lightllm_generate`（端点实现）和 `HttpServerManager.generate`（核心方法）是**两个不同的函数**，都叫 generate 容易混淆。分工是：

- `lightllm_generate`（[api_lightllm.py:32](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_lightllm.py#L32)）：**解析 JSON、构造 `SamplingParams`、消费 token 流、组装最终 HTTP 响应**。它不碰 zmq。
- `HttpServerManager.generate`（[manager.py:309](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L309)）：**分词、校验、分配共享内存 Req、向下游分发、等待 token**。它持有 zmq 套接字。

分发还涉及一个重要的**选路问题**：请求到底发给谁？答案是按优先级选第一条可用通道：**视觉(visual) → 音频(audio) → 多级 KV 缓存(multi_level_kv_cache) → Router**。其中走不走 visual，取决于模型是不是多模态模型——这由启动期自动探测决定（见 4.2.3）。

#### 4.2.2 核心流程

`HttpServerManager.generate` 是一个 `AsyncGenerator`，它的主流程是「先准备请求对象，再 yield 出每一个生成 token」：

```text
HttpServerManager.generate(prompt, sampling_params, multimodal_params, request)
  ├─ alloc_req_id()                # 分配 group_request_id
  ├─ _encode()                     # 文本 → prompt_ids（多模态会申请 embed 缓存）
  ├─ _check_and_repair_length()    # 长度校验，必要时下调 max_new_tokens
  ├─ shm_req_manager.async_alloc_req_index()  # 在共享内存里申请 N 个 Req 槽位
  ├─ req_obj.init(...)             # 初始化 Req 对象（写入 prompt_ids 等）
  ├─ transfer_to_next_module()     # 【分发】按优先级发给 visual/audio/kv/router
  ├─ _wait_to_token_package()      # 【回流】异步等待并 yield 每个 token（见 4.3）
  └─ finally: 维护 run_reqs_count
```

长度校验里有一个值得记住的小公式。一次请求允许的最大总长度受两个量约束——模型配置的 `max_req_total_len` 和实际可用的 token 容量 `max_total_token_num`，取其小再留 36 的边界余量：

\[
\text{real\_supported\_len} = \min(\text{shm\_max\_total\_token\_num} - 36,\ \text{max\_req\_total\_len})
\]

合法约束为：

\[
\text{prompt\_tokens} + \text{max\_new\_tokens} \le \text{real\_supported\_len}
\]

若超限但 `real_supported_len - prompt_tokens > 0`，HttpServer 不会直接报错，而是**下调 `max_new_tokens`** 让请求合法（见 [manager.py:581-597](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L581-L597)）；若连 1 个 token 都生不了，才抛 `ValueError`→ 端点翻译成 400。

#### 4.2.3 源码精读

**第 1 步：解析请求体构造采样参数。** 这是 `lightllm_generate` 的工作，它从 JSON 取出 `inputs`（提示文本）和 `parameters`（采样参数），构造并校验 `SamplingParams`，然后调用核心 `generate`：

```python
# lightllm/server/api_lightllm.py:32-44
async def lightllm_generate(request: Request, httpserver_manager: HttpServerManager) -> Response:
    request_dict = await request.json()
    prompt = request_dict.pop("inputs")
    sample_params_dict = request_dict["parameters"]
    sampling_params = SamplingParams()
    sampling_params.init(tokenizer=httpserver_manager.tokenizer, **sample_params_dict)
    sampling_params.verify()
    multimodal_params_dict = request_dict.get("multimodal_params", {})
    multimodal_params = MultimodalParams(**multimodal_params_dict)
    results_generator = httpserver_manager.generate(prompt, sampling_params, multimodal_params, request=request)
    ...
```

**第 2 步：分词与长度校验。** 在 `HttpServerManager.generate` 内部，先 `_encode`（多模态会通过 rpyc 申请 embed 缓存槽位），再 `_check_and_repair_length`：

```python
# lightllm/server/httpserver/manager.py:352-361
prompt_ids = await self._encode(prompt, multimodal_params, sampling_params)
...
prompt_tokens = len(prompt_ids)
prompt_ids = await self._check_and_repair_length(prompt_ids, sampling_params)
```

`_encode` 对文本有一个「字符级预检」：经验上每个 token 平均不超过 8 个字符，所以若 `len(prompt) > max_req_total_len * 8` 就在分词前直接拒绝，避免无谓的分词开销（[manager.py:519-528](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L519-L528)）。

**第 3 步：在共享内存里分配 Req 对象。** 这是「对象放共享内存」设计的体现。`best_of`（或 `n`）决定一次请求要几个样本，于是申请对应数量的 Req 槽位，再 `init` 填入 prompt_ids 等内容：

```python
# lightllm/server/httpserver/manager.py:397-418
alloced_req_indexes = []
while len(alloced_req_indexes) < sampling_params.n:
    alloc_req_index = await self.shm_req_manager.async_alloc_req_index()
    sleep_time = 0.1
    while alloc_req_index is None:        # 槽位不够就退避重试
        await asyncio.sleep(sleep_time)
        sleep_time = min(1, sleep_time * 1.1)
        alloc_req_index = await self.shm_req_manager.async_alloc_req_index()
    alloced_req_indexes.append(alloc_req_index)
req_objs: List[Req] = []
for i, req_index in enumerate(alloced_req_indexes):
    req_obj = await self.shm_req_manager.async_get_req_obj_by_index(req_index)
    req_obj.init(group_request_id + i, prompt_ids, sampling_params, self.tokenizer,
                 chunked_prefill_size=self.args.chunked_prefill_size)
    req_objs.append(req_objs)
```

槽位不足时用指数退避（`0.1 → 0.11 → ... → 1.0` 封顶）反复重试，把背压做在了 HttpServer 这一层。

**第 4 步：分发选路 `transfer_to_next_module`。** 这是本模块的核心。它把封装好的 `GroupReqObjs` 转成只含索引的 `GroupReqIndexes`，然后按优先级投递给下游进程：

```python
# lightllm/server/httpserver/manager.py:626-662
async def transfer_to_next_module(self, group_req_objs=None):
    if self.pd_mode.is_P_or_NORMAL():
        if not self.args.disable_vision:
            self.send_to_visual.send_pyobj(group_req_objs.to_group_req_index(), ...)
            return
        if not self.args.disable_audio:
            self.send_to_audio.send_pyobj(group_req_objs.to_group_req_index(), ...)
            return
        if self.args.enable_cpu_cache:
            self.send_to_multi_level_kv_cache.send_pyobj(group_req_objs.to_group_req_index(), ...)
            return
        self.send_to_router.send_pyobj(group_req_objs.to_group_req_index(), ...)   # ← 普通文本直达
        return
    if self.pd_mode.is_D():
        self.send_to_router.send_pyobj(group_req_objs.to_group_req_index(), ...)
        return
```

注意每次投递的都是 `to_group_req_index()` 的结果——**只传索引、不传对象**：

```python
# lightllm/server/core/objs/io_objs/group_req.py:15-28
@dataclass
class GroupReqObjs:                       # 持有真正的 Req 对象（住共享内存）
    group_req_id: int
    multimodal_params: MultimodalParams
    shm_req_objs: List[Req]
    time_mark: float
    def to_group_req_index(self):
        return GroupReqIndexes(           # 投递时只带每个 Req 在共享内存里的下标
            group_req_id=self.group_req_id,
            multimodal_params=self.multimodal_params,
            shm_req_indexes=[req.index_in_shm_mem for req in self.shm_req_objs],
            time_mark=self.time_mark,
        )
```

**关键细节：`disable_vision` 的默认值是怎么定的？** 命令行参数 `--disable_vision` 默认是 `None`（[api_cli.py:338-343](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L338-L343)），启动时 `api_start.py` 会按模型目录是否包含视觉模块**自动解析**：

```python
# lightllm/server/api_start.py:92-101
if args.disable_vision is None:
    if has_vision_module(args.model_dir):
        args.disable_vision = False   # 多模态模型 → 走 visual 进程
    else:
        args.disable_vision = True    # 纯文本模型 → 直达 router
```

所以同样一段分发代码，在 llama（纯文本）上 `disable_vision=True`，三个 `if` 全跳过，请求直达 `send_to_router`；在 qwen-vl（多模态）上 `disable_vision=False`，请求先发给 visualserver 计算图像嵌入，再由它转发给 router。这也解释了 u2-l1 里那张数据流图为什么对纯文本是「HttpServer → Router」直连。

**这些 zmq 套接字在哪里创建？** 全部在 `HttpServerManager.__init__` 里，连接到本机各进程端口：

```python
# lightllm/server/httpserver/manager.py:50-104
self.send_to_router = context.socket(zmq.PUSH)
self.send_to_router.connect(f"{args.zmq_mode}127.0.0.1:{args.router_port}")
...
if not self.args.disable_vision:
    self.send_to_visual = context.socket(zmq.PUSH)
    self.send_to_visual.connect(f"{args.zmq_mode}127.0.0.1:{args.visual_port}")
...
# 回流用的订阅套接字
self.zmq_recv_socket = context.socket(zmq.SUB)
self.zmq_recv_socket.connect(f"{args.zmq_mode}127.0.0.1:{args.http_server_port}")
self.zmq_recv_socket.setsockopt(zmq.SUBSCRIBE, b"")
```

`PUSH` 投递请求（去 Router/Visual），`SUB` 订阅结果（来自 Detokenization 的 PUB，见 4.3）。

#### 4.2.4 代码实践

**实践目标**：在源码里跟踪一次 `/generate` 请求，从端点一直到 `send_to_router`，画出完整的调用链。

**操作步骤**：

1. 从 [api_http.py:231](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_http.py#L231) 的 `generate` 出发，确认它调用 `g_objs.g_generate_func`，即 `lightllm_generate`。
2. 在 [api_lightllm.py:44](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_lightllm.py#L44) 进入 `httpserver_manager.generate(...)`。
3. 在 [manager.py:309](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L309) 的 `generate` 内，依次标注 `_encode`(L353)、`_check_and_repair_length`(L361)、共享内存分配(L397-407)、`transfer_to_next_module`(L434)。
4. 在 [manager.py:647](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L647) 找到 `send_to_router.send_pyobj(...)`，并确认参数是 `to_group_req_index()` 的结果。

**需要观察的现象 / 预期结果**：你会得到一条单链：

```text
POST /generate
 → api_http.generate
   → lightllm_generate（解析 JSON / 构造 SamplingParams）
     → HttpServerManager.generate（encode / 校验 / 分配 Req）
       → transfer_to_next_module
         → send_to_router.send_pyobj(to_group_req_index())   # 出 HttpServer 进程
```

> 待本地验证：若你启动的是多模态模型（如 `Qwen/Qwen-VL`），在 `transfer_to_next_module` 处会观察到请求被发往 `send_to_visual` 而非 `send_to_router`；纯文本模型（如 `meta-llama/...`）则直达 router。

#### 4.2.5 小练习与答案

**练习 1**：为什么投递给 Router 的是 `to_group_req_index()` 的结果，而不是整个 `GroupReqObjs`？

**参考答案**：`Req` 对象体积大且在推理过程中会被多个进程频繁读写，若每次跨进程拷贝整对象会带来巨大开销。LightLLM 把 `Req` 放在共享内存里，各进程通过 `index_in_shm_mem` 直接访问同一块内存，线上只投递索引，做到零拷贝。

**练习 2**：共享内存 Req 槽位不够时，HttpServer 是直接报错还是等待？为什么？

**参考答案**：等待。`async_alloc_req_index` 返回 `None` 时会指数退避重试（`sleep_time` 从 0.1 增长到 1.0 封顶），而不是立刻报错（[manager.py:398-407](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L398-L407)）。这是把「系统繁忙」的背压转化成请求排队，让前端只看到延迟而非错误，待其他请求释放槽位后即可继续。

---

### 4.3 结果回流（从 Detokenization 到 HTTP 响应）

#### 4.3.1 概念说明

「结果回流」要解决的问题是：Router 把 token 算出来后，怎么把文本送回最初发起请求的那个 HTTP 协程。

这里有个异步编程的经典难点：发起请求的协程（`HttpServerManager.generate`）和接收结果的协程（`handle_loop`）不是同一个。LightLLM 的解法是**事件 + 列表**的中转模式：

- 每个请求有一个 `ReqStatus`，里面带一个 `asyncio.Event`（信号灯）和一个 `out_token_info_list`（暂存区）。
- `handle_loop` 持续轮询 zmq `SUB` 套接字，把 Detokenization 推来的 token 写进对应请求的暂存区，然后 `event.set()` 敲一下信号灯。
- `_wait_to_token_package` 在 `event.wait()` 上挂起，醒来后消费暂存区里的 token 并 `yield` 出去。

这条回流链是闭环的最后一段（详见 u2-l1 的数据流图）：**Detokenization → (zmq PUB) → HttpServer 的 SUB → handle_loop → ReqStatus → _wait_to_token_package → lightllm_generate → HTTP Response**。

#### 4.3.2 核心流程

```text
Detokenization 进程（解码出文本，zmq PUB 推送）
        │  （注意：handle_loop 并不是直接 recv 文本，而是从共享内存的
        │    out_tokens_queue 里读，详见下方源码说明）
        ▼
HttpServerManager.handle_loop()        # startup 时启动的常驻协程
  ├─ for 每个活跃 req_status:
  │     ├─ 从 req.out_tokens_queue 逐个 peek 文本片段 + metadata
  │     ├─ 判断该 token 是否触发完成（FinishStatus）
  │     └─ 累积到 token_list
  ├─ async with req_status.lock:
  │     req_status.out_token_info_list.extend(token_list)
  │     req_status.event.set()         # 唤醒等待者
  └─ recycle_event.set()               # 顺带触发资源回收
        ▼
_wait_to_token_package()               # generate() 内的消费者
  ├─ await event.wait()
  ├─ for 每个 (sub_req_id, out_str, metadata, finish_status) in out_token_info_list:
  │     yield ...                      # 逐 token 返还给 lightllm_generate
  └─ 全部子请求完成 → 统计指标并 return
        ▼
lightllm_generate / lightllm_generate_stream
  ├─ 非流式：把所有 token 拼成完整文本，一次性 Response
  └─ 流式：每个 token 包装成 SSE data: ... 立刻 yield
```

#### 4.3.3 源码精读

**`handle_loop`：结果回流主循环。** 它在 startup 时被 `create_task` 启动（见 4.1.3），同时还会拉起一个 `recycle_resource_loop` 负责回收已完成的请求资源。主循环本身的关键是：**它并不从 zmq 直接读解码文本，而是 Detokenization 通过共享内存把文本写进 `req.out_tokens_queue`，handle_loop 再从队列里 peek 出来**：

```python
# lightllm/server/httpserver/manager.py:880-945（节选）
while True:
    try:
        await asyncio.wait_for(self.zmq_recv_socket.recv_pyobj(), timeout=0.05)  # 收 Detokenization 的轻量通知
    except asyncio.TimeoutError:
        pass
    try:
        for group_req_id_ in list(self.req_id_to_out_inf.keys()):
            req_status = self.req_id_to_out_inf.get(group_req_id_, None)
            if req_status is None:
                continue
            token_list = []
            for req in req_status.group_req_objs.shm_req_objs:
                req_id = req.request_id
                read_token_count = 1
                if req.out_tokens_queue.is_full():                  # 队列满则批量读，减少轮询
                    read_token_count = LIGHTLLM_OUT_TOKEN_QUEUE_SIZE
                for _ in range(read_token_count):
                    if not req.out_tokens_queue.is_empty():
                        text, src_index, special, count_output_tokens = req.out_tokens_queue.peek()
                        req.cumlogprob += float(req.shm_logprobs.arr[src_index])
                        metadata = {
                            "id": int(req.shm_prompt_ids.arr[src_index]),
                            "logprob": float(req.shm_logprobs.arr[src_index]),
                            ...
                        }
                        req.out_tokens_queue.pop_no_ret()
                        finished_token_index = (req.stop_str_matched_token_index
                                                if req.stop_str_matched else req.finish_token_index)
                        if finished_token_index != src_index:
                            token_list.append((req_id, text, metadata, FinishStatus()))       # 未完成
                        else:
                            finish_status = FinishStatus(FinishStatus.FINISHED_STOP) if req.stop_str_matched \
                                           else FinishStatus(req.finish_status.status)
                            token_list.append((req_id, text, metadata, finish_status))        # 完成
                    else:
                        break
            async with req_status.lock:
                req_status.out_token_info_list.extend(token_list)
                req_status.event.set()                              # 唤醒 _wait_to_token_package
    except BaseException as e:
        logger.exception(str(e)); raise e
    self.recycle_event.set()
```

注意 `zmq_recv_socket.recv_pyobj()` 在这里更像一个**节奏触发器**——Detokenization 每推送一个通知，handle_loop 就醒来轮询所有活跃请求的 `out_tokens_queue`。这种「通知走 zmq、数据走共享内存」的拆分，正是 LightLLM 高吞吐的关键之一。

`FinishStatus` 是个简单的状态机，标记一个 token 是否让该请求收尾：

```python
# lightllm/server/core/objs/req.py:21-53
class FinishStatus(ctypes.Structure):
    NO_FINISH = 0      # 还在生成
    FINISHED_STOP = 1  # 命中停止条件（EOS / stop 字符串）
    FINISHED_LENGTH = 2  # 达到 max_new_tokens
    def is_finished(self):
        return self.FINISHED_STOP <= self.status <= self.FINISHED_LENGTH
    def get_finish_reason(self):
        if self.status == self.FINISHED_STOP:   return "stop"
        elif self.status == self.FINISHED_LENGTH: return "length"
        return None
```

**`_wait_to_token_package`：消费侧。** 它在 `event.wait()` 上挂起（带 5 秒超时，便于周期性检查客户端是否断连），醒来后在锁保护下消费 `out_token_info_list`，并把每个 token `yield` 给上层：

```python
# lightllm/server/httpserver/manager.py:682-725（节选）
while True:
    try:
        await asyncio.wait_for(event.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass
    if req_status.aborted:
        raise Exception(f"req_id {group_request_id} aborted notifyed by other module")
    if not self.disable_abort and request is not None and await request.is_disconnected():
        await self.abort(group_request_id)            # 客户端断连则中止推理
        raise ClientDisconnected(...)
    async with req_status.lock:
        event.clear()
        if len(req_status.out_token_info_list) == 0:
            continue
        for sub_req_id, out_str, metadata, finish_status in req_status.out_token_info_list:
            ...
            yield sub_req_id, out_str, metadata, finish_status    # ← 关键：逐 token 上抛
            if finish_status.is_finished():
                unfinished_count -= 1
            if unfinished_count == 0:
                ... # 记录 first_token_cost、per_token_cost 等指标
                return
        req_status.out_token_info_list.clear()
```

这里有两处实战要点：① 5 秒超时让 HttpServer 能及时感知客户端断连并 `abort`，避免为已离开的客户端白算；② 所有 `best_of` 个子请求都完成（`unfinished_count == 0`）后，记录延迟指标再 `return` 结束生成器。

**`ReqStatus`：中转结构。** 它把锁、事件、暂存区、`GroupReqObjs` 绑在一起，是回流链上的「信箱」：

```python
# lightllm/server/httpserver/manager.py:949-966
class ReqStatus:
    def __init__(self, group_request_id, multimodal_params, req_objs, start_time):
        self.lock = asyncio.Lock()
        self.event = asyncio.Event()
        self.group_req_objs = GroupReqObjs(...)
        self.out_token_info_list = []   # 暂存区：handle_loop 写、_wait_to_token_package 读
        self.aborted = False
    def can_release(self):
        for req in self.group_req_objs.shm_req_objs:
            if not req.can_release():
                return False
        return True
```

**最终组装 HTTP 响应。** `_wait_to_token_package` yield 出来的 token 流，被 `lightllm_generate`（非流式）或 `lightllm_generate_stream`（流式）消费：

- 非流式 `lightllm_generate`：用 `defaultdict(list)` 按子请求 id 收集所有 token，全部生成完后 `"".join` 拼成完整文本，一次性返回 JSON（[api_lightllm.py:56-106](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_lightllm.py#L56-L106)）。
- 流式 `lightllm_generate_stream`：每收到一个 token 就立刻包装成 SSE（`data: {...}\n\n`）`yield` 出去，借助 `StreamingResponse` 实现 token 级流式输出（[api_lightllm.py:126-156](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_lightllm.py#L126-L156)）。

至此请求闭环完成。

#### 4.3.4 代码实践

**实践目标**：定位结果回流链上的「生产者—信箱—消费者」三处代码，并理解 `event` 的唤醒时机。

**操作步骤**：

1. 在 [manager.py:497](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L497) 确认 `handle_loop` 是 startup 时 `create_task` 起来的常驻协程。
2. 在 `handle_loop` 里找到「写暂存区 + 唤醒」的那两行：`req_status.out_token_info_list.extend(token_list)` 与 `req_status.event.set()`（[manager.py:938-940](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L938-L940)）。
3. 在 `_wait_to_token_package` 里找到「等待 + 消费」：`await asyncio.wait_for(event.wait(), timeout=5)` 与 `yield ...`（[manager.py:684](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L684)、[L725](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L725)）。
4. 追到 `lightllm_generate_stream` 看 token 如何变成 SSE（[api_lightllm.py:149](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_lightllm.py#L149)）。

**需要观察的现象 / 预期结果**：你应能解释清楚——为什么 `_wait_to_token_package` 用 `event.wait()` 而不是 `sleep` 轮询？因为 `handle_loop` 在写入暂存区后会主动 `event.set()` 精准唤醒它，配合 5 秒超时只用于断连检测，既低延迟又不空转。

> 待本地验证：启动服务后用 `curl -N -X POST .../generate_stream` 发一个流式请求，可在服务端日志里看到 token 逐步产出的访问日志；用 `curl .../generate`（非流式）则只会在全部生成完毕后一次性返回。

#### 4.3.5 小练习与答案

**练习 1**：`handle_loop` 收到 zmq 消息后，为什么还要去 `req.out_tokens_queue` 里 peek 文本，而不是直接用 zmq 消息里的文本？

**参考答案**：zmq 在这里只承担「轻量通知/唤醒」职责，真正的解码文本和 logprob 等数据放在共享内存的 `out_tokens_queue` 与配套数组里。这样既避免了把每个 token 的大块数据反复跨进程拷贝，又能让 HttpServer 按自己的节奏（队列满时批量读 `LIGHTLLM_OUT_TOKEN_QUEUE_SIZE` 个）消费，降低轮询开销。

**练习 2**：如果客户端在生成到一半时关闭了连接，HttpServer 会怎么处理？

**参考答案**：`_wait_to_token_package` 的 5 秒超时周期会调用 `await request.is_disconnected()` 检测断连，一旦确认就 `await self.abort(group_request_id)`（把该组 Req 标记为 `is_aborted`），并抛出 `ClientDisconnected`；上层端点捕获后返回 499（[manager.py:691-695](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L691-L695)）。被标记的请求随后会被 Router 调度循环感知并停止为其生成 token。

---

## 5. 综合实践

**任务**：用一张完整的时序图，把本讲三个模块串起来——从 `curl` 发起 `/generate` 请求，到拿到完整文本返回。

要求在你的图里至少标出下列要素，并附上每一步对应的源码位置：

1. HTTP 报文进入 FastAPI 路由 `generate`（[api_http.py:231](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_http.py#L231)）。
2. 委托给 `lightllm_generate` 解析 JSON、构造 `SamplingParams`（[api_lightllm.py:32](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_lightllm.py#L32)）。
3. 进入 `HttpServerManager.generate`：`_encode` → `_check_and_repair_length` → 共享内存分配 Req（[manager.py:309-436](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L309-L436)）。
4. `transfer_to_next_module` 经 `send_to_router.send_pyobj(to_group_req_index())` 出 HttpServer 进程（[manager.py:647](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L647)）。
5. Router/ModelBackend/Detokenization 链路（本讲作为黑盒，标注「见 u2-l4/u2-l5/u2-l7」）把 token 算出并解码。
6. `handle_loop` 从 `out_tokens_queue` peek 出文本，写进 `ReqStatus.out_token_info_list` 并 `event.set()`（[manager.py:880-945](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L880-L945)）。
7. `_wait_to_token_package` 被 `event` 唤醒后 `yield` token，`lightllm_generate` 拼接全部文本返回 JSON（[manager.py:664](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L664)、[api_lightllm.py:106](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_lightllm.py#L106)）。

**进阶思考**（可选）：如果把同一个请求改成 `/generate_stream`，第 7 步会发生什么变化？（提示：`lightllm_generate_stream` 不再拼接，而是每收到一个 token 立刻以 SSE `yield`。）

## 6. 本讲小结

- LightLLM 的 HTTP 入口是 FastAPI，端点函数都很「薄」——只做 **PD 模式拦截 + 异常翻译 + 委托**，真正逻辑在 `lightllm_generate` / `chat_completions_impl` 等实现函数里。
- `api_http.py` 与 `httpserver/manager.py` 是「路由层 / 干活层」的分工：前者注册端点、持有全局 `g_objs`；后者持有全部 zmq 套接字，完成分词、校验、Req 分配、分发、回流。
- 请求分发的核心是 `HttpServerManager.generate`，它经历 **encode → 长度校验（超限下调 `max_new_tokens`）→ 共享内存分配 Req → `transfer_to_next_module` 选路**。
- 分发选路按优先级 **visual → audio → multi_level_kv_cache → router**；纯文本模型因 `disable_vision=True`（启动期按模型自动探测）直达 router，多模态模型先经 visual 进程。
- 跨进程投递永远只传索引（`to_group_req_index()`），Req 对象住在共享内存里，这是「对象放共享内存、线上只传索引」设计的具体落地。
- 结果回流是「事件 + 暂存区」的中转模式：`handle_loop`（生产者）写 `out_token_info_list` 并 `event.set()`，`_wait_to_token_package`（消费者）被唤醒后 `yield` 给端点实现，最终由流式/非流式函数组装成 HTTP 响应。

## 7. 下一步学习建议

- **u2-l3 请求对象与共享内存通信**：本讲多次出现的 `Req`、`index_in_shm_mem`、`ShmReqManager` 到底长什么样、如何零拷贝传递，下一讲会深入其 ctypes 结构与 `ShmObjsIOBuffer` 机制。
- **u2-l4 Model Backend 推理后端与 RPC**：本讲把请求交给了 Router，下一讲看 Router 怎么用 rpyc 调度每张 GPU 上的 `ModelRpcServer`。
- **u2-l5 Router 调度循环**：理解请求进入 Router 后如何被组装成 batch、何时 prefill、何时 decode。
- **u2-l7 Detokenization 与流式输出**：本讲回流链的「上游」Detokenization 进程如何把 token 解码成增量文本，下一讲会完整展开。
