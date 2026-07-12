# 代理与多机部署 proxy

## 1. 本讲目标

本讲聚焦 `lmdeploy/serve/proxy/`，讲清楚 lmdeploy 如何用一个**前置代理（reverse proxy）**把多台 `lmdeploy serve api_server` 实例统一成一个对外服务。学完后你应该能够：

- 说清「为什么需要 proxy」：单机单卡服务不够用时，proxy 提供多副本负载均衡与统一入口。
- 读懂 `proxy.py` 中 `NodeManager` 的节点注册、心跳探活与三种请求路由策略。
- 追踪一次 `/v1/chat/completions` 请求从进入 proxy 到被转发到某个后端实例、再把流式输出回传客户端的完整链路。
- 理解 `streaming_response.py` 中 `ProxyStreamingResponse` 为何要重写 `stream_response`，它解决了 SSE 流式响应「首字节一发出就无法改状态码」的难题。
- 认识 PD 分离部署（DistServe）的多机拓扑概念：prefill 节点与 decode 节点如何经 proxy 协作并迁移 KV cache。

## 2. 前置知识

阅读本讲前，建议你已经掌握：

- **FastAPI / uvicorn 基础**：proxy 本身就是一个 FastAPI 应用，由 uvicorn 运行；路由用 `@app.post(...)` 注册，请求体用 Pydantic 模型解析。
- **反向代理（reverse proxy）概念**：与正向代理（替客户端访问外网）相反，反向代理替服务端接收所有外部请求，再按某种策略转发给后端真实服务（upstream）。Nginx 就是典型反向代理。本讲的 proxy 扮演的就是 Nginx 的角色，后端 upstream 是一个个 `lmdeploy serve api_server`。
- **SSE（Server-Sent Events）流式响应**：服务端把内容一段段推给客户端，每段以 `data:` 前缀、`\n\n` 分隔。关键限制是——**HTTP 状态码和响应头必须在发送第一个字节之前确定**，一旦开始发 body，状态码就改不了了。这个限制是 `ProxyStreamingResponse` 存在的根本原因。
- **u8-l2 服务启动**：你已经知道 `lmdeploy serve api_server` 用 `serve()` / `launch_server()` 起单进程或多进程 DP 服务。本讲的 proxy 是「再多一层」，站在这些 api_server 前面。
- **Pydantic 模型**：proxy 用 `BaseModel` 描述节点状态、请求/响应结构。

一句话定位：proxy 是 lmdeploy 自带的「轻量级七层负载均衡器 + PD 分离调度器」，纯 Python（基于 aiohttp 做异步转发），适合把多个推理实例编排成一个逻辑服务。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [lmdeploy/serve/proxy/proxy.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py) | 主体。定义 FastAPI `app`、`NodeManager`（节点注册/心跳/路由）、所有路由（`/v1/chat/completions` 等）与启动函数 `proxy()`。约 950 行。 |
| [lmdeploy/serve/proxy/streaming_response.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/streaming_response.py) | `ProxyStreamingResponse`：重写 `stream_response`，实现「先取首块再决定状态码」的流式异常透传。 |
| [lmdeploy/serve/proxy/utils.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/utils.py) | 常量与枚举：`RoutingStrategy`（三种路由策略）、`ErrorCodes`、`APIServerException`、`AIOHTTP_TIMEOUT`。 |
| [lmdeploy/pytorch/disagg/config.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/config.py) | `ServingStrategy`（Hybrid / DistServe）、`EngineRole`（Hybrid / Prefill / Decode）、`DistServeRDMAConfig` 等拓扑枚举与配置。 |
| [lmdeploy/pytorch/disagg/messages.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/messages.py) | `PDConnectionMessage`：描述一对 prefill-decode 节点间的连接。 |
| [lmdeploy/pytorch/disagg/README.md](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/README.md) | PD 分离部署的官方上手文档（含启动命令）。 |
| [lmdeploy/cli/serve.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/serve.py) | `serve proxy` 子命令的参数注册（`add_parser_proxy`）。 |

## 4. 核心概念与源码讲解

本讲按五个最小模块推进：先看整体架构与启动入口，再钻进 `NodeManager` 的节点管理与路由策略，然后追踪一次请求的转发全链路，接着专讲流式转发的异常处理，最后展开 PD 分离部署的多机拓扑。

### 4.1 Proxy 整体架构与启动入口

#### 4.1.1 概念说明

proxy 解决的核心问题是：**当你有不止一张卡、不止一个推理实例时，如何让客户端只面对一个地址？**

设想你起了 4 个 `lmdeploy serve api_server`（分别在 4 张卡上，端口 23333~23336）。如果没有 proxy，客户端必须自己决定把请求发给哪个实例——这要求客户端懂负载均衡，极不现实。proxy 的做法是：自己监听一个端口（默认 8000），客户端把所有请求都发给它，它根据每个后端实例的负载/速度，挑一个最合适的实例转发请求，再把结果原样回传给客户端。

这个架构有三个关键角色：

- **客户端（client）**：只认识 proxy 的地址。
- **proxy（本讲主角）**：持有「后端实例清单」，按路由策略选一个，做异步转发。
- **后端节点（node）**：每个是一个独立的 `lmdeploy serve api_server`，启动时把自己的 URL 注册到 proxy。

值得强调的是：proxy **不做推理**。它不加载模型、不跑 forward，纯粹是一个 HTTP 中转站。真正干活的是后端的 api_server。这一点决定了 proxy 的实现很薄——它的大部分代码是「节点管理」和「HTTP 转发」。

#### 4.1.2 核心流程

proxy 进程的启动与运行流程：

1. `proxy()` 函数被调用（由 `lmdeploy serve proxy` 命令触发），设置 `serving_strategy` / `routing_strategy` 等参数到全局 `node_manager`。
2. `uvicorn.run(app)` 启动 FastAPI 应用，开始监听端口。
3. 与此同时，`NodeManager` 在构造时就启动了一个后台心跳线程，周期性探活所有注册节点，剔除失联的。
4. 后端 api_server 实例一个个通过 `POST /nodes/add` 把自己注册进来（携带自己的 URL 和模型列表）。
5. 客户端向 proxy 发推理请求，proxy 选节点、转发、回流。

```
client ──POST /v1/chat/completions──▶ proxy(:8000)
                                         │ get_node_url(model) 选一个 node
                                         ▼
                              node_a(:23333) / node_b(:23334) / ...
                                         │
                              ◀──流式/非流式 response───
client ◀──转发 response──────────── proxy
```

#### 4.1.3 源码精读

先看模块级对象与启动函数。proxy.py 在模块加载时就创建了 FastAPI 应用与全局 `NodeManager`：

模块顶部创建 app 与全局 node_manager，[lmdeploy/serve/proxy/proxy.py:463-471](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L463-L471)：

```python
app = FastAPI(docs_url='/')
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    ...
)
node_manager = NodeManager()
```

注意 `node_manager` 是**模块级单例**——所有路由函数都直接引用这个全局变量，这点与 u8-l1 讲过的 `VariableInterface` 进程级单例思路一致：让所有路由零参数访问共享状态。

启动函数 `proxy()` 把命令行参数灌进 `node_manager`，再用 uvicorn 起 app，[lmdeploy/serve/proxy/proxy.py:884-941](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L884-L941)：

```python
def proxy(server_name='0.0.0.0', server_port=8000,
          serving_strategy='Hybrid',
          routing_strategy='min_expected_latency',
          api_keys=None, ssl=False, log_level='INFO',
          disable_cache_status=False,
          link_type='RoCE', migration_protocol='RDMA',
          dummy_prefill=False, **kwargs):
    node_manager.serving_strategy = ServingStrategy[serving_strategy]
    node_manager.routing_strategy = RoutingStrategy.from_str(routing_strategy)
    ...
    uvicorn.run(app=app, host=server_name, port=server_port, ...)
```

这个函数对应 CLI 子命令 `serve proxy`。CLI 侧的参数注册在 [lmdeploy/cli/serve.py:178-215](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/serve.py#L178-L215)，`SubCliServe.proxy` 用延迟导入 `from lmdeploy.serve.proxy.proxy import proxy` 后 `convert_args` 转发（与 u1-l5 讲过的 CLI 模板完全一致）。注意 CLI 多了一个 `--disable-gdr`，对应 GPUDirect RDMA 开关；还有 `--dummy-prefill`，用于性能剖析时跳过真实 prefill。

两类策略枚举是理解后续分支的关键。服务策略 `ServingStrategy` 决定「prefill 和 decode 是否分离」，[lmdeploy/pytorch/disagg/config.py:7-18](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/config.py#L7-L18)：

```python
class ServingStrategy(enum.Enum):
    Hybrid = enum.auto()      # prefill+decode 在同一引擎
    DistServe = enum.auto()   # prefill 与 decode 分到不同引擎，KV 需迁移
```

`EngineRole` 描述一个后端节点扮演的角色，[lmdeploy/pytorch/disagg/config.py:21-36](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/config.py#L21-L36)。注意其注释点出一个重要事实：技术上每个引擎都是 hybrid 引擎，「角色」更多是为了让 proxy 能正确发现并区分它们。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是建立全局认知。

1. **实践目标**：搞清 proxy 进程里「谁负责什么」，定位关键全局对象。
2. **操作步骤**：
   - 打开 `proxy.py`，搜索 `@app.`，列出所有注册的路由路径，按用途分组（节点管理 / 推理 / DistServe）。
   - 确认 `node_manager` 是模块级全局（出现在第 471 行），所有路由都通过它访问节点。
3. **需要观察的现象**：路由可分为三类——`/nodes/*`（管理后端节点）、`/v1/chat/completions` 与 `/v1/completions`（推理转发）、`/distserve/*`（PD 连接管理）。
4. **预期结果**：你会得到约 9 条路由，其中真正做推理转发的只有 2 条（chat / completion），其余是节点运维与 PD 连接。
5. 如需确认运行时行为，可执行 `lmdeploy serve proxy --help` 对照参数（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：proxy 进程会不会加载大模型权重？为什么？

> **答**：不会。proxy 只做 HTTP 转发与节点调度，不调用任何引擎的 forward；模型加载与推理都发生在后端 `api_server` 节点上。这也是 proxy 能用纯 Python + aiohttp 实现、本身很轻的原因。

**练习 2**：`proxy()` 函数里为什么是修改全局 `node_manager` 的属性，而不是把配置作为参数传进去？

> **答**：因为 FastAPI 的路由函数（如 `chat_completions_v1`）签名由框架决定，无法接收额外参数。配置必须挂在一个所有路由都能访问的地方——模块级单例 `node_manager` 就是这个「共享黑板」，启动时写入、请求时读取。

---

### 4.2 NodeManager：节点注册、心跳与三种路由策略

#### 4.2.1 概念说明

`NodeManager` 是 proxy 的「大脑」，它维护一张「后端节点清单」，并回答两个核心问题：

1. **现在有哪些节点活着、各自能服务哪些模型、负载多重？**（节点注册与状态维护）
2. **来了一个请求，该转发给谁？**（路由策略）

每个节点的状态用一个 Pydantic 模型 `Status` 描述，[lmdeploy/serve/proxy/proxy.py:46-52](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L46-L52)：

```python
class Status(BaseModel):
    role: EngineRole = EngineRole.Hybrid
    models: list[str] = Field(default=[], ...)
    unfinished: int = 0               # 当前在途请求数（负载指标）
    latency: deque = Field(...)       # 最近若干次请求的耗时（观察指标）
    speed: int | None = Field(...)    # 节点吞吐能力（如 RPM）
```

三个关键字段支撑了三种路由策略：`unfinished`（在途请求数）、`latency`（历史耗时）、`speed`（吞吐能力）。

`NodeManager` 还会把节点状态**持久化**到 `proxy_config.json`，这样 proxy 重启后能「记得」上次的节点清单（除非加 `--disable-cache-status`）。配合一个后台心跳线程定期探活，失联节点会被自动剔除。

#### 4.2.2 核心流程

**节点注册**（`add`）：

1. 收到一个 `POST /nodes/add`，带 `url` 与可选 `status`。
2. 若 `status.models` 非空（调用方显式声明了模型），直接强注册。
3. 否则，proxy 主动去访问该节点的 `APIClient.available_models`，问它「你能服务哪些模型」，拿到列表后再登记。
4. 写回 `proxy_config.json`。

**心跳探活**（后台线程）：

1. 每隔 `CONTROLLER_HEART_BEAT_EXPIRATION`（默认 90 秒，可由环境变量 `LMDEPLOY_CONTROLLER_HEART_BEAT_EXPIRATION` 调整）触发一次。
2. 对每个节点发 `GET /health`，非 200 或异常的节点加入待删除列表。
3. 批量 `remove`。

**路由选择**（`get_node_url`）——三种策略：

- `random`：按 `speed` 做加权随机（不是均匀随机）。
- `min_expected_latency`：估算每个节点的「预期延迟」，选最小的。
- `min_observed_latency`：看历史 `latency` 队列的均值，选最小的。

#### 4.2.3 源码精读

先看节点注册。`add` 在没有显式模型列表时，会反向查询节点，[lmdeploy/serve/proxy/proxy.py:152-178](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L152-L178)：

```python
def add(self, node_url, status=None):
    if status is None:
        status = self.nodes.get(node_url, Status())
    if status.models != []:  # force register directly
        self.remove(node_url)
        self.nodes[node_url] = status
        self.update_config_file()
        return
    try:
        from lmdeploy.serve.openai.api_client import APIClient
        client = APIClient(api_server_url=node_url)
        status.models = client.available_models
        self.nodes[node_url] = status
    except requests.exceptions.RequestException as e:
        ...
        return self.handle_api_timeout(node_url)
    self.update_config_file()
```

注意它复用了 u8-l2 讲过的 `APIClient`（同一个 OpenAI 兼容客户端）去拉取节点的模型列表——这保证 proxy 与后端节点讲的是同一套 `/v1/models` 协议。

心跳线程与探活逻辑，[lmdeploy/serve/proxy/proxy.py:61-68](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L61-L68) 与 [lmdeploy/serve/proxy/proxy.py:219-235](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L219-L235)：

```python
CONTROLLER_HEART_BEAT_EXPIRATION = int(os.getenv('LMDEPLOY_CONTROLLER_HEART_BEAT_EXPIRATION', 90))

def heart_beat_controller(proxy_controller):
    while True:
        time.sleep(CONTROLLER_HEART_BEAT_EXPIRATION)
        proxy_controller.remove_stale_nodes_by_expiration()
```

`remove_stale_nodes_by_expiration` 对每个节点发 `GET /health`，失败即删。这个心跳线程在 `NodeManager.__init__` 里以 daemon 线程启动（[proxy.py:112-113](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L112-L113)），主进程退出时自动结束。

路由选择是本模块的重头戏。三种策略共用一个收集候选节点与速度的内部函数 `get_matched_urls`，[lmdeploy/serve/proxy/proxy.py:251-318](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L251-L318)。其中 `min_expected_latency` 分支最能体现「负载感知」思想：

```python
elif self.routing_strategy == RoutingStrategy.MIN_EXPECTED_LATENCY:
    all_matched_urls, all_the_speeds = get_matched_urls()
    ...
    min_latency = float('inf')
    all_indexes = [i for i in range(len(all_the_speeds))]
    random.shuffle(all_indexes)
    for index in all_indexes:
        latency = self.get_nodes(role)[all_matched_urls[index]].unfinished / all_the_speeds[index]
        if min_latency > latency:
            min_latency = latency
            min_index = index
    url = all_matched_urls[min_index]
    return url
```

这里用「在途请求数 ÷ 吞吐速度」估算每个节点的预期延迟，选最小的。用公式表达即：

\[
\text{expected\_latency}_i = \frac{\text{unfinished}_i}{\text{speed}_i}, \quad i^* = \arg\min_i \text{expected\_latency}_i
\]

`speed` 越大（吞吐越强）或 `unfinished` 越小（越空闲），预期延迟越低，越容易被选中。注释里的 `random.shuffle` 只打乱遍历顺序、不影响最终选中的是全局最小者。

`random` 策略则是按速度做加权随机（快节点概率更高），而非均匀随机，[lmdeploy/serve/proxy/proxy.py:279-287](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L279-L287)：

```python
speed_sum = sum(all_the_speeds)
weights = [speed / speed_sum for speed in all_the_speeds]
index = random.choices(range(len(all_matched_urls)), weights=weights)[0]
```

权重公式为：

\[
w_i = \frac{\text{speed}_i}{\sum_j \text{speed}_j}
\]

注意一个细节：有些节点可能没填 `speed`（为 `None`），`get_matched_urls` 会用「有 speed 节点的平均值」给它们兜底（[proxy.py:275-276](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L275-L276)），保证分母不空、逻辑不崩。

而 `min_observed_latency` 则完全不看实时负载，只看 `latency` 队列的历史均值（无历史则视作无穷大），[proxy.py:304-316](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L304-L316)。三种策略的差异可总结为：`random` 看能力、`min_expected_latency` 看能力+实时负载、`min_observed_latency` 看历史表现。

`pre_call` / `post_call` 是路由策略的数据来源，它们在请求前后更新 `unfinished` 与 `latency`，[lmdeploy/serve/proxy/proxy.py:419-437](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L419-L437)：

```python
def pre_call(self, node_url):
    self.nodes[node_url].unfinished += 1   # 在途 +1
    return time.time()

def post_call(self, node_url, start):
    if node_url in self.nodes:
        self.nodes[node_url].unfinished -= 1              # 在途 -1
        self.nodes[node_url].latency.append(time.time() - start)  # 记录耗时
```

#### 4.2.4 代码实践

1. **实践目标**：验证三种路由策略对同一组节点会选出不同目标。
2. **操作步骤**：
   - 阅读 `get_node_url`（[proxy.py:251-318](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L251-L318)），手工模拟：假设有 2 个节点 A、B，`speed` 分别为 2、1，`unfinished` 分别为 4、1。
   - 用上面的公式分别计算 `min_expected_latency` 下两者的预期延迟，判断会选谁。
3. **需要观察的现象**：A 的预期延迟 = 4/2 = 2，B 的 = 1/1 = 1，故选 B（更空闲）。
4. **预期结果**：即便 A 吞吐更强（speed=2），但因为当前积压请求多，`min_expected_latency` 会避开它。这正是「负载感知」的价值。
5. 若想真实运行，可起两个 api_server 节点注册到同一 proxy，再用不同 `--routing-strategy` 观察分发（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：节点状态为何要持久化到 `proxy_config.json`？

> **答**：让 proxy 重启后无需后端节点重新注册就能「记得」上次的节点清单，减少运维摩擦。用 `--disable-cache-status` 可关闭，此时每次启动都是空表。

**练习 2**：`min_expected_latency` 与 `min_observed_latency` 的本质区别是什么？

> **答**：前者是**前向预测**（用当前在途请求数 + 吞吐能力估算「接下来会多慢」），后者是**后视统计**（用最近若干次请求的实际耗时判断「过去有多慢」）。前者对突发负载更敏感，后者更平滑但反应慢半拍。

---

### 4.3 请求路由与转发全链路（Hybrid 模式）

#### 4.3.1 概念说明

有了节点清单和路由策略，下一步是真正处理一个推理请求。本模块以 Hybrid 模式（prefill 与 decode 同处一个引擎，最常见）下的 `/v1/chat/completions` 为例，追踪完整链路。

转发用 **aiohttp**（异步 HTTP 客户端）实现，而不是同步的 `requests`。原因有二：一是 proxy 要同时处理大量并发客户端连接，必须非阻塞；二是后端 api_server 是流式产出，proxy 需要「边收边发」，aiohttp 的 `async for line in response.content` 天然适合。

转发分两个层次：

- **应用层重组装**（`stream_generate` / `generate`）：proxy 用 `ChatCompletionRequest` Pydantic 模型解析请求，再 `model_dump()` 成 dict 转发。会改写请求体（DistServe 模式需要）。
- **裸字节透传**（`forward_raw_request_*`）：不解析、不重组，原样转发原始 body 字节。Hybrid 模式用这一路，最高保真。

#### 4.3.2 核心流程

Hybrid 模式下一次 chat 请求的完整流程（[proxy.py:654-668](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L654-L668)）：

1. `check_request_model`：确认请求的 `model` 在某节点的能力清单里，否则返回 404。
2. `get_node_url(model)`：按路由策略选一个后端节点 URL。
3. `pre_call(node_url)`：该节点 `unfinished += 1`，记录起始时间。
4. 分流：
   - **流式**（`stream=True`）：调 `forward_raw_request_stream_generate` 得到一个异步生成器，包进 `ProxyStreamingResponse` 返回。**收尾用 `BackgroundTasks`**：在响应完全发出后异步执行 `post_call`（`unfinished -= 1`、记录 latency）。
   - **非流式**：`await forward_raw_request_generate` 拿到完整文本，同步调 `post_call`，包成 `JSONResponse` 返回。

注意流式与非流式在「何时调 `post_call`」上的差别：非流式是请求-响应同步的，可以直接调；流式则必须等到全部 token 流完，所以挂到 FastAPI 的 `BackgroundTasks` 上由框架在响应结束后触发——这正是 `create_background_tasks` 的用途（[proxy.py:439-448](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L439-L448)）。

#### 4.3.3 源码精读

Hybrid 分支主逻辑，[lmdeploy/serve/proxy/proxy.py:654-668](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L654-L668)：

```python
if node_manager.serving_strategy == ServingStrategy.Hybrid:
    node_url = node_manager.get_node_url(request.model)
    if not node_url:
        return node_manager.handle_unavailable_model(request.model)
    logger.info(f'A request is dispatched to {node_url}')
    start = node_manager.pre_call(node_url)
    if request.stream is True:
        response = node_manager.forward_raw_request_stream_generate(
            raw_request, node_url, '/v1/chat/completions')
        background_task = node_manager.create_background_tasks(node_url, start)
        return ProxyStreamingResponse(response, background=background_task,
                                      media_type='text/event-stream')
    else:
        response = await node_manager.forward_raw_request_generate(
            raw_request, node_url, '/v1/chat/completions')
        node_manager.post_call(node_url, start)
        return JSONResponse(json.loads(response))
```

流式透传生成器，逐行读取后端响应并补上 SSE 分隔符 `\n\n`，[lmdeploy/serve/proxy/proxy.py:384-404](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L384-L404)：

```python
async def forward_raw_request_stream_generate(self, raw_request, node_url, endpoint):
    try:
        target_url = node_url.rstrip('/') + endpoint
        headers = self._prepare_headers(raw_request)
        body_bytes = await raw_request.body()
        async with aiohttp.ClientSession() as session:
            async with session.post(target_url, headers=headers, data=body_bytes,
                                    timeout=self.aiotimeout) as response:
                if response.status != 200:
                    error_body = await response.read()
                    raise APIServerException(status_code=response.status, body=error_body)
                async for line in response.content:
                    if line.strip():
                        yield line + b'\n\n'
    except APIServerException:
        raise   # 交给外层 ProxyStreamingResponse 处理
    except (Exception, GeneratorExit, aiohttp.ClientError) as e:
        ...
        yield self.handle_api_timeout(node_url)
```

这里有两个精妙设计：

1. **裸字节透传**：直接把 `raw_request.body()` 原样 POST 给后端，不解析不重组，最大程度保真（自定义字段、特殊 content-type 都不会丢）。
2. **首块异常上抛**：若后端返回非 200，立即 `raise APIServerException`（注意它不被本函数的 `except` 吞掉，而是 `raise` 重新抛出）。这个异常会被 `ProxyStreamingResponse` 在取首块时捕获——这就是下一模块要讲的核心机制。

`_prepare_headers` 会剥离 `host` 头并补上 `X-Forwarded-For` / `X-Forwarded-Host` / `X-Forwarded-Proto`（[proxy.py:450-460](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L450-L460)），这是反向代理的标准做法，让后端知道「真正的客户端是谁、原始协议是什么」。

错误处理用一套统一的错误码，定义在 utils.py，[lmdeploy/serve/proxy/utils.py:38-49](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/utils.py#L38-L49)：

```python
class ErrorCodes(enum.Enum):
    MODEL_NOT_FOUND = 10400
    SERVICE_UNAVAILABLE = 10401
    API_TIMEOUT = 10402

err_msg = {
    ErrorCodes.MODEL_NOT_FOUND: 'The request model name does not exist in the model list.',
    ...
}
```

`handle_api_timeout` 把错误码与消息打包成一行 JSON 字节（带 `\n`），作为流的一部分 yield 出去（[proxy.py:340-347](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L340-L347)）。这是流式场景下「中途出错」的唯一表达方式——body 已经开始流了，状态码改不了，只能把错误塞进数据流。

#### 4.3.4 代码实践

1. **实践目标**：把「请求被路由到后端实例」的过程走一遍。
2. **操作步骤**：
   - 在 `proxy.py` 中从 `chat_completions_v1`（第 573 行）读到第 668 行，把 Hybrid 分支的 5 步（校验模型→选节点→pre_call→转发→post_call）画成流程图。
   - 定位 `forward_raw_request_stream_generate`（第 384 行）与 `forward_raw_request_generate`（第 406 行），对比两者的差异。
3. **需要观察的现象**：流式版本是 `async def ... yield`（异步生成器），非流式版本是 `async def ... return`（普通协程）；流式版本用 `BackgroundTasks` 收尾，非流式直接同步 `post_call`。
4. **预期结果**：你能口述出「流式为何要用 BackgroundTasks」——因为流式响应返回时 token 还没流完，`post_call` 必须推迟到流结束。
5. 真实运行可在本地起 1 个 proxy + 2 个 api_server 节点，发流式 chat 请求观察日志里的 `A request is dispatched to ...`（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么流式分支用 `forward_raw_request_stream_generate`（裸字节），而 DistServe 的流式分支却用 `stream_generate`（应用层重组）？

> **答**：Hybrid 模式只需原样转发，用裸字节最保真、最快；而 DistServe 模式需要在请求体里**注入 `migration_request` 字段**（告诉 decode 节点去哪取 KV cache），必须解析、改写请求体，所以走应用层重组的 `stream_generate`。

**练习 2**：`pre_call` 返回的 `start` 时间戳在流式分支里如何被使用？

> **答**：它被传给 `create_background_tasks(node_url, start)`，封装成一个 `BackgroundTasks`，挂在 `ProxyStreamingResponse` 上。FastAPI 在流式响应全部发送完毕后执行该后台任务，即调用 `post_call(node_url, start)`，完成 `unfinished -= 1` 与 `latency.append`。

---

### 4.4 ProxyStreamingResponse：流式转发的异常透传

> 这是本讲第二个核心最小模块（`streaming_response`），也是 proxy 设计中最巧妙的一处。

#### 4.4.1 概念说明

回到 4.1 提到的 SSE 限制：**HTTP 状态码必须在发第一个字节前确定**。对 proxy 来说这是个真问题——

设想客户端发了一个 chat 请求，proxy 把它转发给后端节点。如果后端节点回的是 200，一切正常，proxy 也回 200 并开始流式转发。但如果后端节点回的是 500（比如显存 OOM）或 400（请求非法），proxy 该怎么办？

如果 proxy 已经向客户端发了 `200 OK` 的响应头（因为它「以为」转发成功），那就晚了——状态码改不了，只能在一个 200 响应里塞错误信息，客户端会误以为成功。FastAPI 自带的 `StreamingResponse` 正是这么做的：它一上来就发 `200`，然后才开始迭代 body 生成器。

`ProxyStreamingResponse` 的解法是：**在发响应头之前，先偷偷取生成器的第一个 chunk**。如果取首块时生成器抛了 `APIServerException`（意味着后端非 200），就用后端的真实状态码发响应头；否则正常发 200 并继续流。

#### 4.4.2 核心流程

`stream_response`（ASGI 协议层方法）的处理流程：

1. 拿到 body 迭代器，**先 `__anext__()` 取第一个 chunk**。
2. 若取首块抛 `APIServerException`：
   - 用异常里的 `status_code` 与 `body` 发 `http.response.start` + `http.response.body`（`more_body=False`，结束）。
   - 即把后端的错误状态原样透传给客户端。
3. 若取首块成功（正常首块）：
   - 先发 `200` 的 `http.response.start`。
   - 把首块作为第一段 body 发出（`more_body=True`）。
   - 继续迭代剩余 chunk，逐段发出。
   - 流中途若再抛异常，发一段 500 错误 JSON 并结束（此时状态码已是 200，无法改）。
4. 正常结束后发一个空 body（`more_body=False`）收尾。

#### 4.4.3 源码精读

完整实现，[lmdeploy/serve/proxy/streaming_response.py:10-71](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/streaming_response.py#L10-L71)：

```python
class ProxyStreamingResponse(StreamingResponse):
    """StreamingResponse that can handle exceptions thrown by the generator."""

    async def stream_response(self, send) -> None:
        iterator = self.body_iterator.__aiter__()
        try:
            first_chunk = await iterator.__anext__()   # 先取首块
        except APIServerException as e:
            headers = self._convert_headers_to_asgi(e.headers) if e.headers else self.raw_headers
            await send({'type': 'http.response.start', 'status': e.status_code, 'headers': headers})
            await send({'type': 'http.response.body', 'body': e.body, 'more_body': False})
            return

        # 正常情况，先发 200 响应头
        await send({'type': 'http.response.start', 'status': self.status_code, 'headers': self.raw_headers})
        # 带上首块一起发
        await send({'type': 'http.response.body', 'body': first_chunk, 'more_body': True})

        # 继续流式输出
        try:
            async for chunk in iterator:
                await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
        except Exception:
            error_data = {'error': True, 'status': 500, 'message': 'Internal streaming error'}
            await send({'type': 'http.response.body', 'body': json.dumps(error_data).encode('utf-8'),
                        'more_body': False})
            return

        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
```

注意三个细节：

1. **只有首块异常能改状态码**：`APIServerException` 只在取首块时被捕获（对应 `forward_raw_request_stream_generate` 在发第一个 chunk 前的 `raise`）。一旦 200 头发出，后续异常只能塞进 500 JSON body。
2. **`APIServerException` 携带后端真实信息**：`status_code` 与 `body` 来自后端节点的原始错误响应（见 [utils.py:52-59](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/utils.py#L52-L59)），实现「错误透传」而非「错误吞掉」。
3. **`_convert_headers_to_asgi`**（[streaming_response.py:69-71](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/streaming_response.py#L69-L71)）：把 dict 形式的头转成 ASGI 要求的 `[(bytes, bytes)]` 元组列表，并统一小写化。

需要强调一个与前模块呼应的事实：**`APIServerException` 机制只对 Hybrid 模式的 `forward_raw_request_stream_generate` 生效**。DistServe 模式用的是 `stream_generate`（[proxy.py:349-366](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L349-L366)），它不抛 `APIServerException`，而是把错误 `yield` 成数据行——所以 DistServe 的流式错误不会改状态码，只能出现在 body 流里。这是两条转发路径在设计上的有意差异。

#### 4.4.4 代码实践

1. **实践目标**：理解「先取首块」为何能解决状态码问题。
2. **操作步骤**：
   - 对照 `forward_raw_request_stream_generate`（[proxy.py:384-404](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L384-L404））与 `stream_response`（[streaming_response.py:16-67](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/streaming_response.py#L16-L67)），画出「后端返回 500」时数据/异常的流动路径。
   - 思考：如果不在发头前取首块，会怎样？
3. **需要观察的现象**：后端 500 → 生成器在产出任何 chunk 前 `raise APIServerException` → `stream_response` 取首块时捕获 → 用 500 发响应头与错误 body → 客户端看到真实的 500。
4. **预期结果**：你能解释清楚「首块是探针」——它既是正常流的第一段数据，又是探测后端是否健康的试金石。
5. 若不取首块直接发 200，则后端 500 会被掩盖成「200 + 错误 JSON body」，客户端误判成功——这正是该设计要避免的。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `stream_response` 对「首块异常」和「中途异常」采用不同处理？

> **答**：首块异常发生在响应头发送之前，状态码还能改，所以透传后端真实状态码；中途异常发生在 200 响应头已发出之后，状态码不可变更，只能把错误塞进一段 500 JSON body。两者的差异是 HTTP/ASGI 协议「头先于体、头不可改」约束的直接结果。

**练习 2**：`ProxyStreamingResponse` 继承自 `StreamingResponse` 却只重写了 `stream_response`，为什么不用别的方式（比如中间件）来实现错误透传？

> **答**：因为错误发生在 body 生成器迭代过程中，而状态码必须在发头前确定——只有接管「发头」这一步才能解决。中间件层拿不到生成器内部的逐 chunk 异常时机，无法做到「先取首块再决定状态码」。重写 `stream_response` 是最精准的切入点。

---

### 4.5 PD 分离部署 DistServe 与多机拓扑

#### 4.5.1 概念说明

前面四个模块都在讲 Hybrid 模式（一个节点完整地做 prefill + decode）。DistServe（PD 分离）是另一种部署形态：把 prefill 阶段（处理长 prompt，计算密集）和 decode 阶段（逐 token 生成，访存密集）**分到不同的引擎**，让各自针对的工作负载最优。

PD 分离的核心难点是：prefill 引擎算完 prompt 后，生成的 KV cache 存在自己显存里，而后续 decode 要在 decode 引擎上继续——必须把 KV cache 从 prefill 引擎**迁移**到 decode 引擎。这个迁移走高速互联（RDMA / NVLink），proxy 在其中扮演「调度协调者」。

此时多机拓扑有三个角色：

- **prefill 节点**（`EngineRole.Prefill`）：只做 prefill。
- **decode 节点**（`EngineRole.Decode`）：只做 decode。
- **proxy**：收到请求后，先派给一个 prefill 节点算 prompt，再把「接着生成」的指令连同 KV 位置信息派给一个 decode 节点。

#### 4.5.2 核心流程

DistServe 模式下一次请求的双跳流程（以 `/v1/chat/completions` 为例，[proxy.py:669-735](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L669-L735)）：

1. **构造 prefill 子请求**：深拷贝原请求，把 `max_tokens=1`、`stream=False`、`with_cache=True`、`preserve_cache=True`——只要 prefill 的副产物（KV cache），不要真生成。
2. **派发 prefill**：`get_node_url(model, EngineRole.Prefill)` 选一个 prefill 节点，发 prefill 子请求，拿到 `prefill_info`（含 `id`、`cache_block_ids`、`remote_token_ids`）。
3. **建/查 KV 连接**：若该 (prefill, decode) 对尚未建立迁移连接，经 `pd_connection_pool.connect` 建立一条 RDMA/NVLink 通道。
4. **构造 decode 请求**：把 prefill 的 `remote_session_id` / `remote_block_ids` / `remote_token_id` 包成 `MigrationRequest`，塞进原请求的 `migration_request` 字段——告诉 decode 节点「去 prefill 节点的哪些 block 取 KV」。
5. **派发 decode**：`get_node_url(model, EngineRole.Decode)` 选一个 decode 节点，转发改写后的请求，流式或非流式返回。
6. **会话归档**：`shelf_prefill_session` / `unshelf_prefill_session` 管理迁移通道上的会话上下文。

#### 4.5.3 源码精读

prefill 子请求的构造（强制只 prefill 不生成），[lmdeploy/serve/proxy/proxy.py:672-678](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L672-L678)：

```python
prefill_request_dict = copy.deepcopy(request_dict)
prefill_request_dict['max_tokens'] = 1
prefill_request_dict['max_completion_tokens'] = 1
prefill_request_dict['stream'] = False
prefill_request_dict['with_cache'] = True
prefill_request_dict['preserve_cache'] = True
```

`with_cache=True` + `preserve_cache=True` 是关键：让 prefill 节点算完 prompt 后**保留** KV cache（默认推理完就回收），供 decode 节点迁移取用。

连接池预热端点，对每一对 (prefill, decode) 建立迁移通道，[lmdeploy/serve/proxy/proxy.py:553-564](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L553-L564)：

```python
@app.post('/distserve/connection_warmup')
async def connection_warmup():
    await asyncio.gather(*[
        node_manager.pd_connection_pool.connect(
            PDConnectionMessage(
                p_url=p_url, d_url=d_url,
                protocol=node_manager.migration_protocol,
                rdma_config=node_manager.rdma_config,
            )) for p_url in node_manager.prefill_nodes for d_url in node_manager.decode_nodes
    ])
    return JSONResponse({'SUCCESS': True})
```

`PDConnectionMessage` 描述一对 prefill-decode 节点的连接参数，[lmdeploy/pytorch/disagg/messages.py:31-37](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/messages.py#L31-L37)：

```python
class PDConnectionMessage(BaseModel):
    p_url: str
    d_url: str
    protocol: MigrationProtocol = MigrationProtocol.RDMA
    tcp_config: DistServeTCPConfig | None = None
    rdma_config: DistServeRDMAConfig | None = None
    nvlink_config: DistServeNVLinkConfig | None = None
```

RDMA 配置含 GPUDirect RDMA（`with_gdr`）与链路类型（RoCE/IB），[lmdeploy/pytorch/disagg/config.py:53-68](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/config.py#L53-L68)。`with_gdr=True` 让 KV cache 直接从 GPU 显存经 RDMA 搬到对端显存，绕过 CPU 中转，是高带宽迁移的关键。

构造 decode 请求时把 prefill 产出的定位信息打包，[lmdeploy/serve/proxy/proxy.py:708-718](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L708-L718)：

```python
remote_session_id = int(prefill_info.get('id')) if prefill_info.get('id') else 0
remote_block_ids = prefill_info.get('cache_block_ids') or []
remote_token_id = prefill_info.get('remote_token_ids')[-1] if prefill_info.get('remote_token_ids') else 0

request_dict['migration_request'] = MigrationRequest(
    protocol=node_manager.migration_protocol,
    remote_engine_id=p_url,
    remote_session_id=remote_session_id,
    remote_block_ids=remote_block_ids,
    remote_token_id=remote_token_id,
    is_dummy_prefill=node_manager.dummy_prefill).model_dump(mode='json')
```

decode 节点拿到 `migration_request` 后，知道要去 `p_url` 这个 prefill 节点的 `remote_session_id` 会话、取 `remote_block_ids` 这些 KV block、从 `remote_token_id` 接着生成。

`dummy_prefill` 模式（CLI `--dummy-prefill`）跳过真实 prefill，用于性能剖析——只测 decode 与迁移链路的吞吐，不测 prefill 计算（[proxy.py:681-682](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L681-L682) 与 [proxy.py:698](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L698)）。

#### 4.5.4 代码实践

1. **实践目标**：画出 PD 分离部署的拓扑与请求双跳数据流。
2. **操作步骤**：
   - 阅读 [disagg/README.md](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/README.md)，按其「Quick Start」梳理启动顺序：先起 proxy（`--serving-strategy DistServe`），再分别起 prefill 与 decode 两个 api_server（用 `--role Prefill` / `--role Decode`、`--proxy-url` 指向 proxy）。
   - 在 [proxy.py:669-735](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L669-L735) 中标注「prefill 子请求构造 → prefill 派发 → 连接建立 → decode 请求改写 → decode 派发」五步。
3. **需要观察的现象**：一个客户端请求在 proxy 内部变成了两次后端调用（先 prefill 节点、再 decode 节点），中间夹一次 KV 迁移连接的建立/复用。
4. **预期结果**：你能说清「为什么 DistServe 必须用应用层重组的 `stream_generate` 而非裸字节转发」——因为 decode 请求体里要注入 `migration_request`。
5. 真实部署需多卡 + RDMA 环境，且**目前仅 PyTorch 后端支持 PD 分离**（见 README）。无此环境则做源码阅读即可（待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 prefill 子请求要设 `max_tokens=1` 且 `preserve_cache=True`？

> **答**：`max_tokens=1` 是为了只让 prefill 节点跑 prefill 阶段、几乎不生成 token（省算力）；`preserve_cache=True` 是为了让 prefill 算出的 KV cache 保留在显存不被回收，供 decode 节点经迁移通道取走。两者合起来实现「prefill 节点只负责造 KV」。

**练习 2**：README 的「Trouble Shooting」提到「proxy 断开后连接池需重新 warm up」，结合源码解释原因。

> **答**：KV 迁移连接（`PDConnectionPool`）是 proxy 进程在内存里维护的运行时状态，连接对象随 proxy 进程消亡而消失。proxy 重启后，prefill 与 decode 节点间的 RDMA/NVLink 通道需要重新 `connection_warmup` 建立。README 也指出未来可用 ETCD 这类独立服务做连接发现，避免反复预热。

---

## 5. 综合实践

**任务**：在本地用 1 个 proxy 编排 2 个 api_server 后端节点，观察请求路由与流式转发。

**环境准备**（需 GPU 与已安装 lmdeploy；若无则降级为源码阅读版，见末尾）：

1. **启动 proxy**（终端 1）：

   ```bash
   lmdeploy serve proxy --server-name 0.0.0.0 --server-port 8000 \
       --routing-strategy min_expected_latency --serving-strategy Hybrid --log-level INFO
   ```

2. **启动两个后端节点**（终端 2、3，各占一张卡，`--proxy-url` 指向 proxy 自动注册）：

   ```bash
   CUDA_VISIBLE_DEVICES=0 lmdeploy serve api_server <模型路径> \
       --server-port 23333 --proxy-url http://0.0.0.0:8000 --backend pytorch
   CUDA_VISIBLE_DEVICES=1 lmdeploy serve api_server <模型路径> \
       --server-port 23334 --proxy-url http://0.0.0.0:8000 --backend pytorch
   ```

   > 注：`--proxy-url` 让 api_server 启动时向 proxy 发 `POST /nodes/add` 注册自己（见 u8-l2）。

3. **观察节点注册**：

   ```bash
   curl http://0.0.0.0:8000/nodes/status
   curl http://0.0.0.0:8000/v1/models
   ```

   预期看到两个节点 URL 与它们共同暴露的模型列表。

4. **发流式 chat 请求**，并观察 proxy 日志：

   ```bash
   curl -X POST http://0.0.0.0:8000/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"<模型名>","messages":[{"role":"user","content":"用一句话介绍上海"}],"stream":true,"max_tokens":32}'
   ```

   预期：proxy 日志打印 `A request is dispatched to http://...:23333`（或 23334）；客户端收到逐段 SSE 数据。

5. **连续发多次请求**，观察路由策略效果：`min_expected_latency` 会把请求往更空闲的节点派；可换 `--routing-strategy random` 重启 proxy 对比。

**需要验证的现象**：

- 两个节点都注册成功，`/v1/models` 聚合了它们的能力。
- 流式输出是逐段到达（SSE），而非一次性返回。
- proxy 日志里能看到请求被分发到不同节点（负载均衡生效）。

**源码阅读降级版**（无 GPU 环境时）：不启动服务，改为——

- 从 `chat_completions_v1`（[proxy.py:573](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L573)）出发，画出 Hybrid 分支的完整时序图（含 `check_request_model` → `get_node_url` → `pre_call` → `forward_raw_request_stream_generate` → `ProxyStreamingResponse` → `BackgroundTasks(post_call)`）。
- 用 curl 或 Postman 构造一个发往 `localhost:8000` 的请求体，对照 `ChatCompletionRequest` 字段，标注哪些字段会被 proxy 原样透传、哪些会在 DistServe 模式下被改写。

> 上述运行结果为「待本地验证」——是否可跑取决于本地是否有 GPU、模型权重与 RDMA（DistServe 才需要）环境。

## 6. 本讲小结

- **proxy 的定位**：lmdeploy 自带的轻量反向代理 + 负载均衡器，站在多个 `api_server` 实例前，自身不做推理，只做节点管理与 HTTP 转发。
- **NodeManager 三职责**：维护 `node_url → Status` 清单（含 `unfinished`/`latency`/`speed`/`models`/`role`）、后台心跳探活剔除失联节点、用三种路由策略（`random` / `min_expected_latency` / `min_observed_latency`）选目标节点。
- **转发两层次**：Hybrid 用裸字节透传（`forward_raw_request_*`，最保真），DistServe 用应用层重组（`stream_generate`，可改写请求体注入 `migration_request`）；底层都是 aiohttp 异步客户端。
- **流式收尾**：流式响应靠 FastAPI `BackgroundTasks` 在响应结束后异步触发 `post_call`（更新 `unfinished` 与 `latency`），因为流式返回时 token 还没流完。
- **ProxyStreamingResponse 的精髓**：重写 `stream_response`，在发响应头前先取首块——首块异常即透传后端真实状态码，解决 SSE「头不可改」难题；此机制仅对 Hybrid 的 `forward_raw_request_stream_generate` 生效。
- **DistServe 多机拓扑**：prefill 节点造 KV（`preserve_cache=True`），decode 节点接续生成，proxy 经 `PDConnectionPool` 建立 RDMA/NVLink 迁移通道并用 `MigrationRequest` 把 KV 定位信息带给 decode 节点；目前仅 PyTorch 后端支持。

## 7. 下一步学习建议

- **深入 PD 分离的底层**：本讲只讲了 proxy 侧的协调，真正的 KV 迁移由 `lmdeploy/pytorch/disagg/` 的 `backend/`（DLSlime / Mooncake）与 `conn/` 实现。建议接着读 u9-l5（PD 分离部署 disagg），看 `PDConnectionPool` 背后的传输引擎如何搬 KV cache。
- **对比 launch_server 的多进程 DP**：u8-l2 讲的 `launch_server()` 是「单机多卡 DP」的进程编排，本讲的 proxy 是「多实例负载均衡 + 多机 PD」。两者解决不同层次的扩展问题，建议对照阅读，理清「何时用 launch_server、何时用 proxy」。
- **阅读 api_server 的被注册侧**：proxy 的 `/nodes/add` 依赖后端 api_server 主动注册。建议看 `lmdeploy/serve/openai/api_server.py` 中 `--proxy-url` / `--role` 参数如何触发注册，闭合「节点自注册」的另一半链路。
- **关注协议与配置**：若要做二次开发，重点掌握 `ServingStrategy` / `EngineRole` / `RoutingStrategy` 三个枚举（本讲已覆盖）与 `MigrationProtocol`（RDMA/NVLink），它们是 proxy 行为的全部开关。
