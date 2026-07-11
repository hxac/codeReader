# MP Coordinator：跨实例协调

> 本讲是 LMCache 多进程（MP）架构的第三篇。前置讲义 `u3-l1` 已经建立了「engine 与 cache daemon 分离」的总图，`u3-l2` 拆解了 worker 与单实例 cache daemon 之间的跨进程传输管道（ZMQ 消息队列 + CUDA IPC / 共享内存）。本讲把视角从**单实例**抬高到**整个舰队（fleet）**：当几十上百个 MP cache server 分布在多个节点上时，谁来记住「哪台机器上有哪些 KV」？答案就是 **MP Coordinator**。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 MP Coordinator 在多实例部署里扮演的角色，以及它为什么必须是一个**独立进程**而非某个 server 的内置功能。
- 跟踪「mp server 注册 → 心跳 → 健康检查淘汰」这条会员（membership）主链路，并指出 `app.py` / `registrar.py` / `registry.py` 三者各自负责哪一段。
- 理解 `blend_directory.py` 如何用一个**多项式滚动哈希 + 直接定址表**，让任意一台 server 的 CacheBlend lookup 能发现整个舰队里缓存过的 chunk。
- 说明 `blend_client.py` 的「opt-in + submit/poll + best-effort」设计为什么不会拖慢推理关键路径。
- 列出 coordinator 暴露给 worker / 运维的主要 HTTP 能力。

## 2. 前置知识

阅读本讲前，建议你已经了解以下概念（若不熟悉，可先看 `u3-l1`、`u3-l2` 与 `u2-l6`）：

- **MP 架构的三类进程**：vLLM engine 进程、单实例 MP cache server daemon、可选的 MP coordinator daemon。Coordinator 属于第三类。
- **no fate-sharing（不与引擎共命运）**：把 KV cache 管理从推理引擎进程里拆出来，引擎崩溃不会连累 cache。Coordinator 把这个原则再推一层——它也不和任何单个 cache server 共命运。
- **CacheBlend**：LMCache 对「任意位置重复文本块」的 KV 复用，用 `chunk_size`（默认 256 token）作为匹配单位，靠选择性重算恢复质量（见 `u2-l6`）。
- **L2 存储**：跨实例共享的内容寻址存储层（如 Redis、S3）。Coordinator 的很多能力（配额、淘汰、blend 预取）都是围绕 L2 展开的。
- **FastAPI / uvicorn**：Coordinator 是一个标准的 Python 异步 Web 服务，用 FastAPI 定义路由、用 uvicorn 跑事件循环。
- **单调时钟（monotonic clock）**：`time.monotonic()` 返回一个不受系统时间回拨（NTP 校时）影响的时间戳，用于判断「多久没心跳了」。

> 一个贯穿全讲的直觉：coordinator 是**舰队级**组件，它记住的是「 fleet 里有哪些 server、它们缓存了哪些内容」这种**任何单台 server 都无法独自知道**的信息。

## 3. 本讲源码地图

本讲涉及的关键文件全部位于 `lmcache/v1/mp_coordinator/`，以及一份镜像该目录的设计文档：

| 文件 | 作用 |
| --- | --- |
| [lmcache/v1/mp_coordinator/app.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/app.py) | FastAPI 应用工厂 `create_app`，组装所有协作者，启动后台循环（健康检查、L2 淘汰、启动重同步）。 |
| [lmcache/v1/mp_coordinator/registry.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/registry.py) | `InstanceRegistry` + `MPInstance`，舰队会员表的纯内存、线程安全实现。 |
| [lmcache/v1/mp_coordinator/registrar.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/registrar.py) | **mp server 侧**的注册助手（`register` / `keep_registered`），即「加入舰队」的客户端代码。 |
| [lmcache/v1/mp_coordinator/blend_directory.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_directory.py) | `GlobalBlendMatcher`，舰队级 CacheBlend 指纹目录（多项式哈希 + 直接定址表）。 |
| [lmcache/v1/mp_coordinator/blend_client.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_client.py) | **mp server 侧**的 blend 客户端，opt-in 地把本地 STORE/LOOKUP 接到 coordinator 目录。 |
| [lmcache/v1/mp_coordinator/http_apis/instances_api.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/http_apis/instances_api.py) | `/instances` REST 资源：注册 / 心跳 / 注销 / 列出舰队。 |
| [lmcache/v1/mp_coordinator/http_apis/blend_directory_api.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/http_apis/blend_directory_api.py) | `/blend/fingerprints` 与 `/blend/match` 路由。 |
| [lmcache/v1/mp_coordinator/config.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/config.py) | `MPCoordinatorConfig`，frozen dataclass + 环境变量加载。 |
| [lmcache/v1/mp_coordinator/schemas.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/schemas.py) | Pydantic 线协议模型（coordinator 与 mp server 共用的「电线契约」）。 |
| [docs/design/v1/mp_coordinator/README.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/mp_coordinator/README.md) | 设计文档：主干（REST API、注册表、健康循环）与扩展缝。 |
| [docs/design/v1/mp_coordinator/blend_lookup.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/mp_coordinator/blend_lookup.md) | 设计文档：全局 CacheBlend 指纹目录的来龙去脉。 |

> 按 LMCache 的约定，`docs/design/` 目录**镜像** `lmcache/` 包树（见项目 `CLAUDE.md`）。读 `lmcache/v1/mp_coordinator/` 的代码前，先读同路径的 `docs/design/v1/mp_coordinator/` 设计文档，能快速建立「为什么这么设计」的直觉。

## 4. 核心概念与源码讲解

### 4.1 Coordinator 的角色、进程定位与 app 组装

#### 4.1.1 概念说明

随着部署规模变大，会出现**单台 cache server 无法回答**的问题：

- **「舰队里有哪些 server 还活着？」** —— 一台 server 只知道它自己。
- **「我要的这段 KV，被舰队里哪台 server 缓存过？」** —— CacheBlend 的本地匹配器（`u2-l6` 里讲的 `BlendTokenRangeMatcherV3`）只索引**本机**存过的 chunk。当请求被路由到另一台副本时，本机没存过，就得重算——副本越多，缓存分片反而越削弱复用收益。
- **「某个租户（cache_salt）的 L2 用量是不是超标了，该淘汰谁？」** —— 配额和用量是**舰队级**账本。

MP Coordinator 就是回答这些**舰队级问题**的组件。它是一个独立的 FastAPI / uvicorn 进程，不跑推理、不存 KV 张量本身，只存「元信息」（谁在线、谁缓存了什么内容的指纹、用量账本）。

关键定位原则（来自设计文档 README）：

> coordinator is the fleet-level component those features will hang off.

也就是说，membership（会员）、blend 目录、配额、淘汰等能力，都「挂」在 coordinator 这个主干上。本讲聚焦其中两个最能体现 coordinator 价值的能力：**会员**与 **blend 指纹目录**。配额/淘汰/预取的设计文档（`l2_usage_and_eviction.md`、`l2_prefetch.md`）思路类似，留作扩展阅读。

#### 4.1.2 核心流程：coordinator 进程的启动

一条「从命令行到监听端口」的启动链：

```
lmcache coordinator --host 0.0.0.0 --port 9300   (或 python -m lmcache.v1.mp_coordinator)
        │
        ▼
MPCoordinatorConfig.from_env()       读 LMCACHE_MP_COORDINATOR_* 环境变量，CLI flag 覆盖
        │
        ▼
create_app(config)                   组装 registry/quota/usage/eviction/blend 等协作者 + 发现路由
        │
        ▼
uvicorn.run(app, host, port)         进入 lifespan：启动后台循环，开始监听
```

注意有**两个入口**最终都调用同一个 `create_app`：

- `python -m lmcache.v1.mp_coordinator`：纯环境变量配置，见 [`__main__.py` 的 `main()`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/__main__.py#L20-L30)。
- `lmcache coordinator --host ... --port ...`：带 CLI flag，flag 覆盖环境变量，见 [`cli/commands/coordinator.py` 的 `execute()`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/commands/coordinator.py#L143-L203)。后者用 `dataclasses.replace(config, **overrides)` 在 frozen 配置上叠加用户显式传入的字段。

#### 4.1.3 源码精读：create_app 组装了什么

[`create_app`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/app.py#L67-L191) 是整个 coordinator 的「装配车间」。它先逐个构造协作者，再把它们打包进一个类型化的上下文 `CoordinatorContext`，最后让 FastAPI 跑起来。

最关键的一段是协作者的创建（[app.py:79-101](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/app.py#L79-L101)）：

```python
registry = InstanceRegistry()                 # 舰队会员表（本讲 4.2）
quota_manager = QuotaManager()                # 每个 cache_salt 的 L2 配额
usage_manager = L2UsageManager()              # 每个 cache_salt 的 L2 用量
eviction_manager = L2EvictionManager(...)      # 配额/LRU L2 淘汰调度器
resync_manager = L2ResyncManager(...)          # 启动时从某台 server 回填用量
prefetch_manager = PrefetchManager()           # 预取代理
token_hasher = TokenHasher(                    # 把 token_ids 解析成 object keys
    chunk_size=config.chunk_size, hash_algorithm=config.hash_algorithm
)
blend_directory = GlobalBlendMatcher(          # 本讲 4.3 的 blend 指纹目录
    chunk_size=config.chunk_size, probe_stride=config.blend_probe_stride
)
```

注意一个容易踩的坑：`token_hasher` 和 `blend_directory` 都吃 `config.chunk_size`。设计文档与配置 docstring 都反复强调：**coordinator 的 `chunk_size` 必须等于所有 mp server 的 `--chunk-size`**，否则 blend 匹配出来的 key 和实际存的 key 对不上。这是「全舰队一个 chunk size」的硬约束。

接下来是一个类型化的上下文 [`CoordinatorContext`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/http_apis/dependencies.py#L27-L50)，它把上面这些协作者收拢成一个对象挂在 `app.state.ctx` 上，handler 通过 `get_context(request)` 拿到它。这样做的好处是：handler 不再到处写 `getattr(request.app.state, "xxx")` 这种字符串取值，类型也更清晰。

[`lifespan`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/app.py#L141-L173) 在 app 启动时跑，做三件关键事：

1. 创建一个共享的 `httpx.AsyncClient`（[`outbound_client`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/app.py#L147-L151)），用于 coordinator **主动**调用某台 mp server（例如发淘汰指令）。它必须在 lifespan 里创建，才能绑定到运行中的事件循环。
2. 启动若干个后台 asyncio 任务（[`app.py:155-160`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/app.py#L155-L160)）：
   - `_health_loop`：周期性淘汰心跳过期的实例（4.2 详述）。
   - `_eviction_loop`：周期性检查用量是否超配额，超了就发淘汰 RPC。
   - `_startup_resync`：启动时一次性从某台 server 回填用量与淘汰追踪器。
3. 在 `finally` 里 cancel 这些任务、等在途淘汰派发完成、关闭 http client——保证优雅退出。

最后是**路由自动发现**（[app.py:186-189](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/app.py#L186-L189)）：

```python
for router in discover_api_routers(apis_path, package):
    app.include_router(router)
```

`discover_api_routers`（见 [`router_discovery.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/utils/router_discovery.py#L17-L54)）扫描 `http_apis/` 目录下所有名字以 `_api` 结尾的模块，取出每个模块里那个名为 `router` 且类型为 `APIRouter` 的属性挂到 app 上。**这意味着：新增一个能力（比如配额），只需丢一个 `http_apis/quota_api.py` 文件进去，无需改 `create_app` 任何一行**——这正是设计文档说的「扩展缝（extension seam）」。

#### 4.1.4 代码实践：把 coordinator 跑起来

1. **实践目标**：亲眼看到 coordinator 作为独立 HTTP 服务启动，并响应最基础的健康检查与舰队列表。
2. **操作步骤**：
   - 在一台装好 lmcache 的机器上执行（CPU 即可，无需 GPU）：
     ```bash
     LMCACHE_MP_COORDINATOR_PORT=9300 python -m lmcache.v1.mp_coordinator
     ```
     或等价的 `lmcache coordinator --port 9300`。
   - 另开一个终端，做两个请求：
     ```bash
     curl -s http://127.0.0.1:9300/healthz
     curl -s http://127.0.0.1:9300/instances
     ```
3. **需要观察的现象**：第一条返回存活标记（k8s liveness 探针用的）；第二条返回 `{"instances": []}`——此刻还没有任何 mp server 加入。
4. **预期结果**：进程日志里出现 `MP coordinator listening on http://0.0.0.0:9300`（对应 [app.py:161-163](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/app.py#L161-L163)）。如果你没有 GPU 或没装完整依赖，`lmcache coordinator` 会打印安装提示并退出（见 [coordinator.py:166-172](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/commands/coordinator.py#L166-L172)）——此时改用 `python -m lmcache.v1.mp_coordinator` 并确保 `uvicorn`、`fastapi` 已安装。
5. 若本地不便运行：明确标注「待本地验证」，改为阅读 [`__main__.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/__main__.py#L20-L30) 与 [`config.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/config.py#L112-L171) 的 `from_env()`，逐一列出每个 `LMCACHE_MP_COORDINATOR_*` 环境变量对应的配置字段与默认值。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `outbound_client`（httpx.AsyncClient）必须在 `lifespan` 里创建，而不是在 `create_app` 顶层？

> **答案**：因为它要发起**异步** HTTP 调用，必须绑定到「正在运行的事件循环」。`create_app` 执行时 uvicorn 的事件循环还没启动；`lifespan` 是 FastAPI 在循环启动后调用的，在那里创建才能保证 client 与后台循环同属一个循环。参见 [app.py:147-151](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/app.py#L147-L151) 与 dependencies.py 的注释。

**练习 2**：`discover_api_routers` 用什么规则决定一个模块是不是「API 模块」？

> **答案**：模块名必须以 `_api` 结尾（`suffix="_api"` 默认值），且模块里存在一个名为 `router` 且类型为 `fastapi.APIRouter` 的属性。参见 [router_discovery.py:43-53](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/utils/router_discovery.py#L43-L53)。这就是为什么 `http_apis/` 下的 `instances_api.py`、`blend_directory_api.py`、`quota_api.py`、`cache_api.py` 会被自动注册，而 `dependencies.py` 不会。

---

### 4.2 注册与发现机制：registry + registrar + instances_api

#### 4.2.1 概念说明

「会员（membership）」是 coordinator 最基础的能力：维护一张 `instance_id → MPInstance` 的表，记录当前有哪些 mp server 在线、它们的 HTTP 地址是什么。这张表是 coordinator 一切「舰队级」能力的地基——要做 blend 预取、发淘汰指令，都得先知道目标 server 的 `ip` + `http_port`。

这里有一个清晰的三段式分工，初学者很容易把三者搞混，务必分清：

| 角色 | 文件 | 跑在哪 | 职责 |
| --- | --- | --- | --- |
| **数据结构** | `registry.py` | coordinator 进程内 | 「会员表」本身：增删改查 instance，线程安全。**纯数据**，没有 socket、没有 HTTP。 |
| **REST 资源** | `http_apis/instances_api.py` | coordinator 进程内 | 把 HTTP 请求（`POST /instances` 等）翻译成对 registry 的调用。 |
| **客户端助手** | `registrar.py` | **mp server 进程内** | mp server 用来「加入舰队」的代码：发注册、发心跳、注销。 |

一句话：**registry 是「表」，instances_api 是「表的 REST 门面」，registrar 是「别人怎么填这张表」的客户端代码**。

设计文档强调 registry 是「**纯会员**」——只存 ip、端口、心跳时间戳、metadata，**不存**模型名、并行配置等信息。为什么？因为一台 server 可能托管多个模型，若把模型信息塞进会员表就会失真。模型级的路由索引属于未来的「路由 router」，不在主干里（见 [README.md "Registry"](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/mp_coordinator/README.md#L115-L122)）。

#### 4.2.2 核心流程：注册 → 心跳 → 淘汰

```
mp server 启动
   │ keep_registered() 作为 asyncio task 在 mp server 自己的事件循环里跑
   ▼
POST /instances {instance_id?, ip, http_port, ...}  ──▶ coordinator
        │  instances_api.register_instance:
        │     生成 id（若为空）、写 registry、返回 {instance_id, re_registered}
   ▼ （每 heartbeat_interval 秒循环一次）
PUT /instances/{id}/heartbeat  ──▶ coordinator 更新 last_heartbeat_time
        │  返回 200 已知 / 404 未知（让 client 重新注册）
   ▼ （coordinator 侧，独立后台循环 _health_loop）
每隔 health_check_interval 秒：
   registry.stale(instance_timeout)  找出心跳过期的 id
   registry.deregister(id)            把它们踢掉
```

两条独立的时间线要区分清楚：

- **心跳**是 mp server **主动**发的（`keep_registered` 循环里 `PUT .../heartbeat`）。
- **淘汰**是 coordinator **自己**周期性扫的（`_health_loop` → `evict_stale`）。coordinator 不需要 server 配合就能踢人。

两条线靠一个量耦合：server 的心跳频率（`heartbeat_interval`，默认 5s）必须明显小于 coordinator 的 `instance_timeout`（默认 30s），否则正常 server 会被误判过期。这正是 [config.py docstring](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/config.py#L27-L29) 强调的「set this comfortably above the mp servers' own heartbeat cadence」。

#### 4.2.3 源码精读

**(a) registry.py —— 纯会员表**

[`MPInstance`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/registry.py#L25-L52) 是一个普通 dataclass，记录单个 server 的会员信息：

```python
@dataclass
class MPInstance:
    instance_id: str
    ip: str
    http_port: int
    registration_time: float        # 挂钟时间，仅用于展示
    last_heartbeat_time: float      # 单调时钟，用于判活
    metadata: dict[str, str] = field(default_factory=dict)
    p2p_advertised_url: str = ""    # P2P 传输通道用
    mq_port: int = 0                # P2P 查询 RPC 的 ZMQ 端口
```

注意两个时间字段用的是**不同的时钟**：`registration_time` 用挂钟时间（`time.time()`）方便人看；`last_heartbeat_time` 用**单调时钟**（`time.monotonic()`）判活——这样 NTP 校时回拨不会让一台好好的 server 被误判成「很久没心跳」。

[`InstanceRegistry`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/registry.py#L55-L176) 用一把 `threading.Lock` 保护一个 `dict[str, MPInstance]`。所有公开方法都在锁内完成，最值得品的是 [`register`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/registry.py#L67-L83)：它把「检查 id 是否已存在」和「写入」放在**同一次加锁**里，所以即便两个相同 id 的注册请求并发到达，也不可能都返回 `False`（即 `re_registered` 标志是准确的）：

```python
def register(self, instance: MPInstance) -> bool:
    with self._lock:
        existed = instance.instance_id in self._instances
        self._instances[instance.instance_id] = instance
        return existed
```

[`stale`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/registry.py#L159-L175) 用单调时钟算出所有过期 id，给 `evict_stale` 用：

```python
def stale(self, timeout: float) -> list[str]:
    now = time.monotonic()
    with self._lock:
        return [iid for iid, inst in self._instances.items()
                if now - inst.last_heartbeat_time > timeout]
```

**(b) instances_api.py —— REST 门面**

[POST /instances](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/http_apis/instances_api.py#L34-L65) 处理注册。一个细节：若请求体里 `instance_id` 为空，coordinator 用 `f"mp-{uuid.uuid4().hex}"` 生成一个并返回，让客户端**学到**自己被分配的 id。请求体由 [`RegisterRequest`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/schemas.py#L66-L90) 这个 Pydantic 模型校验，`ip` 不允许为空、`http_port` 必须在 1–65535，否则 FastAPI 直接返回 422。

[PUT /instances/{id}/heartbeat](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/http_apis/instances_api.py#L68-L82) 是核心的「判活」端点。它调用 `registry.update_heartbeat`，若返回 `False`（说明 coordinator 不认识这个 id——可能它重启过、注册表是空的），就返回 **404**，提示客户端「重新注册」：

```python
if get_context(request).registry.update_heartbeat(instance_id, time.monotonic()):
    return HeartbeatResponse(instance_id=instance_id)
return JSONResponse(status_code=404,
                    content={"error": f"unknown instance {instance_id}; re-register"})
```

这个 404 语义是整个自愈机制的关键（见下面 registrar 的处理）。

**(c) registrar.py —— mp server 侧客户端**

[`keep_registered`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/registrar.py#L75-L149) 是 mp server 在自己的事件循环里跑的 asyncio task，体现了一个重要的工程原则：**best-effort，绝不拖垮 mp server**。它的循环逻辑：

1. 若还没有 id（`assigned_id is None`），发 `POST /instances` 注册，拿到 id。
2. 否则发 `PUT .../heartbeat`。若返回 **404**（coordinator 把自己忘了），就把 `assigned_id` 置空，下一轮重新注册（[registrar.py:130-136](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/registrar.py#L130-L136)）。
3. 任何 `httpx.HTTPError / ValueError / ValidationError`（网络抖动、5xx、JSON 坏掉、版本不匹配）都被 `except` 捕获并记日志，**保留当前 id** 下一轮重试（[registrar.py:139-143](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/registrar.py#L139-L143)）——既不丢身份也不会重复注册。
4. `finally` 里发 `DELETE /instances/{id}` 注销（[registrar.py:145-148](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/registrar.py#L145-L148)），保证优雅退出。

注意 [registrar.py docstring](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/registrar.py#L1-L13) 的一句设计哲学：coordinator 端没有「专用的 client 对象」，mp server 端也只用一个**通用的 `httpx.AsyncClient`** 加几个函数。这镜像了 coordinator 主动调 mp server 时的做法——双方都「直接 HTTP 调对方的端点」，不搞专门的 RPC 框架。

#### 4.2.4 代码实践：模拟一台 mp server 加入舰队

1. **实践目标**：用一个独立 Python 脚本扮演 mp server，完整走一遍「注册 → 心跳 → 列出舰队 → 注销」，亲手验证 `re_registered` 标志与 404 自愈。
2. **操作步骤**：先按 4.1.4 启动 coordinator，再运行下面的**示例代码**（非项目原有代码，仅作演示）：
   ```python
   # 示例代码：模拟 mp server 的注册/心跳行为
   import asyncio, httpx

   async def main():
       async with httpx.AsyncClient(base_url="http://127.0.0.1:9300") as c:
           # 注册（不传 instance_id，让 coordinator 分配）
           r = await c.post("/instances", json={"ip": "10.0.0.5", "http_port": 8000})
           iid = r.json()["instance_id"]
           print("registered as", iid, "re_registered=", r.json()["re_registered"])
           # 心跳
           hb = await c.put(f"/instances/{iid}/heartbeat")
           print("heartbeat status:", hb.status_code)
           # 列出舰队
           print("fleet:", (await c.get("/instances").json())["instances"])
           # 注销
           await c.delete(f"/instances/{iid}")

   asyncio.run(main())
   ```
3. **需要观察的现象**：第一次注册 `re_registered=False`；心跳返回 200；`GET /instances` 能看到刚刚注册的那台；注销后再列舰队为空。
4. **进阶验证 404 自愈**：把脚本里注册得到的 `iid` 故意改成 `"mp-nonexistent"` 再 `PUT .../heartbeat`，应返回 404（对应 [instances_api.py:79-82](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/http_apis/instances_api.py#L79-L82)）。
5. 若本地无法运行：标注「待本地验证」，改为阅读 [`registrar.py:keep_registered`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/registrar.py#L75-L149)，画出「正常心跳 / 404 / 网络异常」三条分支各自下一步动作的状态机。

#### 4.2.5 小练习与答案

**练习 1**：coordinator 重启后，registry 里的数据还在吗？mp server 会怎样？

> **答案**：不在。registry 是**纯内存、临时（ephemeral）**的（设计文档 Concurrency & lifecycle 一节）。coordinator 重启后表是空的，但 mp server 的 `keep_registered` 会在下一次心跳时收到 404，于是重新注册——表很快被心跳重建。设计文档明确：durable 的状态（如配额）应放外部存储（Redis），而不是这里。

**练习 2**：为什么 `registration_time` 用 `time.time()`，而 `last_heartbeat_time` 用 `time.monotonic()`？

> **答案**：`registration_time` 只给人看（展示「何时加入」），用挂钟时间直观。`last_heartbeat_time` 用来判活（`now - last > timeout`），必须用单调时钟——否则系统时间被 NTP 回拨会让「间隔」变成负数或异常小，误判存活。参见 [registry.py docstring](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/registry.py#L34-L37) 与 [stale()](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/registry.py#L159-L175)。

---

### 4.3 全局 CacheBlend 指纹目录：blend_directory

#### 4.3.1 概念说明

这是 coordinator 最能体现「舰队级价值」的能力。回顾 `u2-l6`：CacheBlend 让任意位置的重复文本块复用 KV，本地匹配器 `BlendTokenRangeMatcherV3` 只索引**本机**存过的 chunk。问题在于：

> 当副本数增加，一台 server 只见过它自己处理过的请求。请求被负载均衡到另一台副本时，那台 server 的本地目录里没有这段内容，明明舰队里别的 server 已经算过并存在 L2 了，它却只能重算。**缓存分片（sharding）反过来削弱了复用收益。**

`blend_directory`（[`GlobalBlendMatcher`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_directory.py#L205-L211)）就是解决这个问题的「舰队级指纹目录」：

- 某台 server **STORE** 时，把存的 token 范围的「指纹」发布给 coordinator（`POST /blend/fingerprints`）。
- 任意一台 server **LOOKUP** 时，把请求 token 发给 coordinator，coordinator 在全舰队的指纹里匹配（`POST /blend/match`），返回命中的 chunk 在 L2 的存储 key（`object_key`）。
- 拿到 `object_key` 后，请求方从**共享 L2** 预取这段 KV，复用之。

设计文档 [`blend_lookup.md`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/mp_coordinator/blend_lookup.md) 给出了一句关键设计决策：**「coordinator does all hashing」**。也就是说，server 只发**原始 token** + 自己才知道的存储映射（`object_key`），所有的切块、哈希、匹配算法都在 coordinator 上跑。好处是：未来要升级匹配算法（比如从 chunk 级改为 token 级），只需改 coordinator 一个地方，**不用改任何 server、不用全舰队重新部署、不用维护跨 server 的哈希一致性**。

> 一句话回答本讲的总问题——**coordinator 解决的「哪个实例持有哪些 KV」，在 blend 维度上是这样解的**：它不直接存「实例 → KV」映射，而是存「内容指纹 → L2 object_key」映射；因为 L2 是全舰队共享的内容寻址存储，任意 server 拿到 `object_key` 都能取到对应 KV，所以 coordinator 只需回答「这段内容在不在舰队的 L2 里、对应的 key 是什么」，而不必关心「具体存在哪台机器」。

#### 4.3.2 核心流程与算法

匹配算法与本地匹配器**完全一致**（这是刻意为之，方便「原算法全球化」）：

1. **登记（register）**：把 token 按 `chunk_size` 切块，每块算一个 64 位多项式哈希，插入「直接定址表」。
2. **查询（match）**：对请求 token 做滚动哈希，每隔 `probe_stride` 个位置探测一次表，命中且通过 64 位全量复检（防哈希桶碰撞）的就是匹配。

**为什么用多项式滚动哈希？** 因为它能让「在一个长序列上找所有长度为 C 的窗口是否等于某个已知块」变成 O(n) 扫描（n 是请求长度），而不用对每个窗口重新算 C 次乘加。直接定址表让「查表」变成一次 numpy gather。

数据结构（来自 [blend_directory.py docstring](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_directory.py#L1-L47) 与 [blend_lookup.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/mp_coordinator/blend_lookup.md#L111-L117)）：

```
_scopes : dict[model_scope -> _ScopeTable]      # 按模型名分表
   _ScopeTable.slots       : int64 数组，哈希低位 -> 条目 id（-1 空）
   _ScopeTable.hashes/locs : 每个 id 的全量哈希 + (object_key, old_st)
   _ScopeTable.poly_to_cid : dict，幂等插入与淘汰查找
_by_key : dict[object_key -> [(scope, poly)]]    # 反向映射，用于按 key 淘汰
```

**作用域（scope）= 模型名**。因为 K 是模型相关的，跨模型的内容永远不该匹配。值得注意的是 **`cache_salt` 不在 key 里**：跨 salt 的匹配在 retrieve 时用**请求方自己的 salt** 展开 `object_key`，若 L2 里没有同 salt 副本就在预取时确认性 miss——这样「每个模型一张表」就足够保证租户隔离，不必为每个 `(model, salt)` 维护一张表（见 [blend_directory.py:18-23](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_directory.py#L18-L23)）。

#### 4.3.3 数学原理：多项式滚动哈希

设 chunk 长度为 \(C\)，token 序列为 \(t_0, t_1, \dots\)，多项式基为 \(b\)（项目里 [`POLY_BASE`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_directory.py#L71) 是 `0x9E3779B97F4A7C15`，黄金分割常数）。起始位置 \(j\) 的 chunk 哈希定义为（一切运算在 `uint64` 下，即模 \(2^{64}\) 自然回绕）：

\[
H_j = \sum_{i=0}^{C-1} t_{j+i} \cdot b^{\,i} \pmod{2^{64}}
\]

「滚动」的含义：已知 \(H_j\)，可以 \(O(1)\) 推出下一个窗口的哈希 \(H_{j+1}\)，而不必重新求和：

\[
H_{j+1} = \big(H_j - t_j\big) \cdot b \;+\; t_{j+C}\cdot b^{\,C-1} \pmod{2^{64}}
\]

- 减去 \(t_j\)：丢掉滑出窗口的那个 token 的贡献。
- 乘 \(b\)：把剩余 token 的幂次整体抬高一位。
- 加 \(t_{j+C}\cdot b^{C-1}\)：补进新滑入窗口的 token。

于是对长度 \(n\) 的请求，先 \(O(n)\) 算出所有窗口哈希，再每隔 `probe_stride` 取一个去查表——查询代价 \(O(n/\text{stride} + \text{命中数})\)。具体实现交给 numba 内核 `rolling_hash_windows_numba` / `chunk_hash_windows_numba`（在 `lmcache/v1/multiprocess/token_hasher.py`，本讲作为黑盒调用）。

**关于碰撞**：64 位多项式哈希仍有极小概率碰撞。设计上**容忍**它——直接定址表用低位哈希定址，查询时再做一次 64 位全量复检（[blend_directory.py:379](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_directory.py#L379)），碰撞只导致一次「白跑的预取」，下游发现取不到就重算，**绝不会给出错误的 KV**（见 [blend_directory.py:43-44](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_directory.py#L43-L44)）。

#### 4.3.4 源码精读

**(a) GlobalBlendMatcher.match —— 一次 numpy gather 完成探测**

[`match`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_directory.py#L341-L392) 是查询热路径，精髓在「向量化探测 + 稀疏复检」：

```python
rolling = rolling_hash_windows_numba(arr, self._chunk_size, POLY_BASE)
probe = rolling[:: self._probe_stride]          # 每隔 stride 取一个窗口哈希
with table.lock:
    entry_ids = table.slots[probe & table.mask]  # 一次 gather 定位每个窗口的条目 id
    hit_positions = np.nonzero(entry_ids >= 0)[0]  # 只看非空槽
    for p in hit_positions.tolist():             # 稀疏 Python 循环
        cid = int(entry_ids[p])
        if int(probe[p]) != table.hashes[cid]:   # 64 位全量复检，防桶碰撞
            continue
        ...
```

关键性能点：`table.slots[probe & table.mask]` 是**一次 numpy 花式索引**，把「成千上万个窗口各自查表」压成单次 gather；只有命中的少量位置才进入 Python 循环做复检。所以持锁时间极短（亚毫秒级）——这是「向量化探测、原地修改、按 scope 分锁」设计的回报。

**(b) GlobalBlendMatcher.register —— 哈希在锁外，写入在锁内**

[`register`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_directory.py#L254-L311) 的一个并发优化：**耗时的 numba 哈希计算放在任何锁之外**（[blend_directory.py:274-292](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_directory.py#L274-L292)），只有真正写表时才持有该 scope 的锁。同时它还做了一致性校验：若一个 range 切出的 chunk 数 `n_chunks` 与它带的 `object_keys` 数量不一致（说明发布方有 bug 或 chunk_size 不一致），**整段跳过**并记 error 日志（[blend_directory.py:280-291](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_directory.py#L280-L291)）——因为对齐前缀会把 chunk 错位地映射到错误的存储 key，比直接丢掉更糟。

**(c) _ScopeTable —— 自适应大小的直接定址表**

[`_ScopeTable`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_directory.py#L123-L134) 与本地匹配器最大的区别：本地用固定大小的 \(2^{20}\) 数组，这里**按 scope 动态 sizing**。`insert` 时若 `_TABLE_GROWTH * 条目数 > 当前表大小`（默认 4 倍冗余），就 [`_rebuild`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_directory.py#L186-L202) 扩容并顺手压缩墓碑（tombstone）。`evict` 不真删，只把 `locs[cid]` 置 `None`（墓碑），当墓碑数超过活条目一半时才 rebuild（[blend_directory.py:182-183](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_directory.py#L182-L183)）。**rebuild 只发生在写路径**，查询路径永远不付这个代价——这让小 scope 保持小表，不浪费内存。

**(d) REST 门面 blend_directory_api.py**

三个端点直接转发到目录：[POST /blend/fingerprints](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/http_apis/blend_directory_api.py#L54-L74)（登记）、[DELETE /blend/fingerprints](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/http_apis/blend_directory_api.py#L77-L86)（按 key 淘汰）、[POST /blend/match](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/http_apis/blend_directory_api.py#L89-L107)（查询）。注意 match 端点把请求 token 用 base64 紧凑编码（`tokens_b64`，小端 uint32），比 JSON 整数列表小约 1.4 倍，且能一次 `np.frombuffer` 直接送进匹配器（见 [schemas.py 的 encode_tokens/decode_tokens](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/schemas.py#L24-L63)）。

#### 4.3.5 代码实践：阅读设计文档并梳理 HTTP 能力

1. **实践目标**：通读 blend 设计文档，确认「coordinator 做所有哈希」「跨 salt 隔离靠一张表/模型」「碰撞只浪费预取不致错」这三条设计决策在代码里的落点。
2. **操作步骤**：
   - 阅读 [`docs/design/v1/mp_coordinator/blend_lookup.md`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/mp_coordinator/blend_lookup.md) 的「Why」「This PR」「Identity and scope」「Failure modes」四节。
   - 对照 [`blend_directory.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_directory.py)，找到：① `POLY_BASE` 定义处；② 「`cache_salt` 不在 key 里」的注释；③ 64 位全量复检那行。
3. **需要观察的现象**：文档说的「Failure modes」表格里每一类失败（coordinator 宕机、哈希碰撞、过期条目、跨 salt 无副本、发布丢失），其处理方式都归结为「miss → 重算」，没有任何一行是「返回错误 KV」。
4. **预期结果**：你能用一句话向同事解释——「blend 目录是 best-effort 的内容寻址索引，所有失败都安全降级为本地重算」。

#### 4.3.6 小练习与答案

**练习 1**：如果两段不同的 chunk 内容碰巧算出相同的 64 位多项式哈希，会发生什么错误？

> **答案**：不会产生错误 KV。直接定址表用低位定址、64 位全量复检；若两段内容**低位相同但全量不同**，复检会拒绝（[blend_directory.py:379-380](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_directory.py#L379-L380)）。即便极端到 64 位也相同（概率极低），最坏后果是一次「白跑的 L2 预取」，下游发现取不到就重算。这正是 [blend_directory.py:43-44](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_directory.py#L43-L44) 说的「never wrong KV」。

**练习 2**：为什么 `cache_salt` 不放进指纹 key，却仍能保证租户隔离？

> **答案**：因为匹配结果在 retrieve 阶段会用**请求方自己的 salt** 展开成 `ObjectKey`（`ipc_key_to_object_keys`）。若 L2 里没有同 salt 的副本，预取就确认性 miss 并重算。这样「每个模型一张表」即可隔离，避免为每个 `(model, salt)` 维护一张表；同时避免了「首写者的 salt 被钉死、其他租户永远匹配不到自己那份相同内容」的陷阱（见 [blend_directory.py:18-23](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_directory.py#L18-L23) 与 blend_lookup.md 同名小节）。

---

### 4.4 mp-server 侧的协调客户端：blend_client（与 registrar 的关系）

#### 4.4.1 概念说明

4.2 讲了 mp server 怎么加入会员（`registrar.py`），4.3 讲了 coordinator 上的 blend 目录。中间还差一环：**mp server 上的 blend 模块，怎么把本地 STORE/LOOKUP 接到 coordinator 目录？** 这就是 [`blend_client.py` 的 `BlendCoordinatorClient`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_client.py#L69-L78)。

它和 `registrar.py` 是平行的两套「mp server → coordinator」客户端，但解决的问题不同：

| | `registrar.py`（会员） | `blend_client.py`（blend 目录） |
| --- | --- | --- |
| 解决 | 「我在不在线」 | 「我存/查的内容在不在舰队」 |
| 协议 | asyncio（mp server 事件循环里） | **同步** httpx + 后台 daemon 线程 |
| 模式 | 注册 + 周期心跳 | submit/poll 查询 + fire-and-forget 发布 |
| 失败 | 404 自愈、重试 | 降级为本地重算 |

为什么 `blend_client` 用**同步** `httpx.Client` 而不是 async？因为 blend 的处理逻辑跑在**同步线程池**里、没有 asyncio 循环（见 [blend_client.py docstring](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_client.py#L1-L13)）。它自己起一个 daemon 线程 + 一个匹配线程池来并发处理多个查询。

两个贯穿设计的核心词：

- **opt-in（可选开启）**：没有配置 coordinator URL 时，blend 模块拿到的是 `None`，所有发布/查询路径都被跳过，行为和「没有 coordinator」完全一样。这是 no fate-sharing 在客户端的体现——coordinator 挂了或没配，推理照跑。
- **best-effort + 不阻塞关键路径**：STORE 发布是「fire-and-forget」（发出去就不管了）；LOOKUP 查询用「submit 一次 + 之后 poll」的非阻塞模式，且整个查询有严格的 wall-clock 预算（`match_budget_s`），超时就放弃舰队匹配、退回本地。

#### 4.4.2 核心流程：submit/poll 的非阻塞匹配

```
LOOKUP 请求到达 mp server 的 blend 模块
   │
   ▼
submit_match(rid, model_scope, tokens)     幂等（同 rid 只提交一次），放入 _match_q
   │ （handler 立即返回，不等结果）
   ▼  daemon 线程 _run():
   从 _match_q 取出查询 → 丢进 ThreadPoolExecutor（最多 match_concurrency 个并发）
        │  _handle_match(): POST /blend/match，结果写进 _results[rid]
   ▼  （handler 在 prefix 解析后、sparse 预取前）
poll_match(rid) → PENDING（还没回） / list[RemoteMatch]（好了） / None（从未提交）
   │  若仍在 PENDING 且未超 match_budget_s：继续等
   │  若超时：放弃舰队匹配，走本地
   ▼
take_match(rid)  消费完清理
```

一个细节：daemon 的 [`_run`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_client.py#L239-L252) 会**优先处理 match 队列、其次才处理 publish 队列**——因为 match 在用户请求的关键路径上（影响延迟），而 STORE 发布是后台账本更新（不影响本次请求），不能让 best-effort 的发布流量堵住查询。

#### 4.4.3 源码精读

三个对外的关键方法：

- [`enqueue_register`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_client.py#L139-L149) / [`enqueue_evict`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_client.py#L151-L161)：把发布/淘汰请求塞进 `_publish_q`，fire-and-forget。
- [`submit_match`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_client.py#L163-L175)：幂等提交一次查询（`rid` 已在结果表就直接返回），写 `PENDING` 占位。
- [`poll_match`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_client.py#L177-L188)：返回 `PENDING` / 结果列表 / `None`。

最值得读的是 [`maybe_from_env`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_client.py#L207-L235)——opt-in 的入口。它读 `LMCACHE_COORDINATOR_URL`：为空就返回 `None`（blend 模块因此走纯本地）；非空才创建 client。注意它读的是 `LMCACHE_COORDINATOR_*`（mp server 侧前缀，由 `CoordinatorConfig` 提供），**不是** coordinator 进程自己的 `LMCACHE_MP_COORDINATOR_*`——两套环境变量分别给两个角色用，别混淆。

[`_handle_match`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_client.py#L254-L280) 的失败处理是 best-effort 的范本：任何异常都 `except Exception` 捕获、记 warning，结果保持为空列表（`matches = []`）。也就是说「coordinator 宕机 / 网络超时 / 返回坏 JSON」对调用方的效果完全一样——**降级为本地重算**，绝不抛错给推理流程。

#### 4.4.4 代码实践：对比两套客户端的失败语义

1. **实践目标**：把 `registrar.py`（会员）与 `blend_client.py`（blend）两套客户端的「失败时各自怎么收场」对比清楚。
2. **操作步骤**：
   - 在 [`registrar.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/registrar.py#L112-L148) 里找到 `try/except/finally`，记录：404 时做什么、HTTP 异常时做什么、finally 做什么。
   - 在 [`blend_client.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_client.py#L254-L280) 里找到 `_handle_match` 的 `except Exception`，记录：失败时 `matches` 是什么值、谁最终承担「没匹配到」的后果。
3. **需要观察的现象**：两套客户端都「永不让上层崩」，但收尾方式不同——registrar 保留身份重试，blend_client 直接给空结果让上层重算。
4. **预期结果**：你能解释为什么 blend 选择「空结果」而非「重试」——因为 blend 在请求关键路径上，重试会拉高延迟，而重算是缓存未命中时本就该做的事，零额外代价。

#### 4.4.5 小练习与答案

**练习 1**：`blend_client` 为什么让 daemon 优先处理 `_match_q` 而不是 `_publish_q`？

> **答案**：因为 match 在用户请求的关键路径上（lookup 延迟直接影响 TTFT），而 publish 是 STORE 之后的后台账本更新，对本次请求无影响。让查询优先、发布靠后，避免 best-effort 的发布流量堵塞查询。参见 [`_run`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/blend_client.py#L239-L252) 与 blend_lookup.md「Match queries run concurrently」一节。

**练习 2**：`LMCACHE_COORDINATOR_URL` 和 `LMCACHE_MP_COORDINATOR_HOST/PORT` 分别给谁用？

> **答案**：前者是 **mp server 侧**用的（告诉它 coordinator 在哪，由 multiprocess 的 `CoordinatorConfig` 读取，开启 blend_client / registrar），后者是 **coordinator 进程自己**用的（决定它绑定哪个地址，由 [`MPCoordinatorConfig.from_env`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/config.py#L112-L171) 读取）。一个是「去哪找 coordinator」，一个是「coordinator 自己绑哪」。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「**coordinator 能力盘点**」任务。这也是本讲规格里指定的实践。

### 任务

阅读 [`docs/design/v1/mp_coordinator/README.md`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/mp_coordinator/README.md) 与 [`blend_lookup.md`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/mp_coordinator/blend_lookup.md)，然后完成两件事：

**第一件**：用**一句话**描述 coordinator 解决的「哪个实例持有哪些 KV」问题。

参考答案（先自己写，再对照）：

> Coordinator 不直接维护「实例 → KV」映射，而是维护舰队会员表（谁在线、地址是什么）与内容指纹目录（哪些 chunk 内容在共享 L2 里、对应的 object_key 是什么）；因为 L2 是全舰队共享的内容寻址存储，任意 server 拿到 object_key 都能取回 KV，所以 coordinator 只需回答「这段内容在不在舰队的 L2 里」与「目标 server 在不在线、地址是什么」即可。

**第二件**：列出 coordinator 暴露给 worker / 运维的**主要 HTTP 能力**，并标注每个能力落在哪个 `http_apis/*_api.py`。可按下表整理（请逐条去源码里确认端点真实存在）：

| 能力分组 | 代表端点 | 方向 | 落点文件 |
| --- | --- | --- | --- |
| 会员 | `POST /instances`、`PUT /instances/{id}/heartbeat`、`DELETE /instances/{id}`、`GET /instances` | mp → coordinator | [`instances_api.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/http_apis/instances_api.py) |
| 存活探针 | `GET /healthz` | k8s → coordinator | [`health_api.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/http_apis/health_api.py) |
| blend 目录 | `POST /blend/fingerprints`、`DELETE /blend/fingerprints`、`POST /blend/match` | mp(blend) → coordinator | [`blend_directory_api.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/http_apis/blend_directory_api.py) |
| 预取派发 | `POST /cache/prefetches`、`GET /cache/prefetches/{iid}/{rid}` | 运维 → coordinator → 某 mp | [`cache_api.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/http_apis/cache_api.py) |
| L2 pin/unpin | `POST /cache/pins`、`DELETE /cache/pins` | 运维 → coordinator | [`cache_api.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/http_apis/cache_api.py) |
| 配额/用量 | `PUT /quota/{salt}`、`GET /quota`、`PUT /quota/config`、`POST /quota/events` | 运维/mp → coordinator | [`quota_api.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/http_apis/quota_api.py) |

> 自检：你应当能解释每个端点是「谁主动调谁」（mp → coordinator，还是 coordinator → mp，还是运维 → coordinator）。比如 `POST /cache/prefetches` 是**运维调 coordinator**，coordinator 再从 registry 查到目标 server 地址后**主动调**那台 server 的 `/cache/prefetches`——这就是设计文档说的「server-initiated work 解析地址后 POST」。

### 进阶（可选）

阅读 [`cache_control/`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/cache_control) 下的 `eviction_manager.py` / `usage_manager.py` / `resync_manager.py`，回答：`_eviction_loop` 为什么「冷启动配额表不会导致大面积误删」（提示：见 [app.py:119-129](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/app.py#L119-L129) 的注释「salts without an explicit quota are exempt from eviction」）。

## 6. 本讲小结

- **Coordinator 是舰队级组件**：它是一个独立 FastAPI/uvicorn 进程，回答任何单台 cache server 都无法独自回答的舰队级问题（谁在线、谁缓存了什么、用量是否超标），自身不跑推理、不存 KV 张量。
- **`create_app` 是装配车间**：组装 registry/quota/usage/eviction/blend 等协作者，用类型化的 `CoordinatorContext` 收拢，`lifespan` 启动 health/eviction/resync 三个后台循环；路由靠 `discover_api_routers` 自动发现 `*_api.py`，新增能力零改主干。
- **会员是三段式分工**：`registry.py`（纯内存表，单调时钟判活）、`instances_api.py`（REST 门面）、`registrar.py`（mp server 侧客户端，404 自愈、best-effort）。会员表是临时（ephemeral）的，重启后靠心跳重建。
- **blend 目录用多项式滚动哈希 + 直接定址表**：coordinator 做所有哈希，server 只发原始 token；查询是一次 numpy gather + 稀疏复检；64 位碰撞与过期条目都安全降级为「白跑预取 → 重算」，**永不返回错误 KV**。
- **两套客户端都是 no fate-sharing**：`registrar`（async，会员）与 `blend_client`（同步线程池，blend）都 opt-in、都 best-effort——coordinator 挂掉或没配，推理照常进行。
- **`cache_salt` 不进指纹 key**：靠 retrieve 时用请求方自己的 salt 展开 object_key 实现租户隔离，所以「每个模型一张表」即可，无需 `(model, salt)` 笛卡尔积。

## 7. 下一步学习建议

- **协议细节**：本讲的 HTTP/REST 是 coordinator ↔ mp server 之间的通信。worker ↔ cache daemon 之间的二进制协议（`ClientMetaMessage`、ZMQ 帧）在 `u3-l2` 已讲；`u3-l4`（HTTP API 与通信协议）会把两套协议对照展开，建议接着读。
- **可观测性**：coordinator 的 health 循环、blend 查询延迟、配额/用量都是天然的指标源。`u3-l5`（可观测性体系）讲 `mp_observability/`，建议结合本讲的 `_health_loop` / `_eviction_loop` 理解「事件如何聚合成指标」。
- **配额与淘汰**：本讲对 `quota_api` / `eviction_manager` 一带而过。`u4-l5`（淘汰策略与配额管理）会深入 LRU/isolated_lru、quota_manager、eviction_controller 的协同，是本讲 4.1 提到的协作者的自然延伸。
- **源码延伸阅读**：`docs/design/v1/mp_coordinator/` 下还有 `l2_prefetch.md`、`l2_usage_and_eviction.md` 两份设计文档，分别对应 `cache_api.py` 的预取派发与 `quota_api.py` 的用量/淘汰，读完本讲后直接看这两份最能巩固理解。
