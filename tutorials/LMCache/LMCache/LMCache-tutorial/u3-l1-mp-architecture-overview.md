# 多进程 (MP) 架构总览

## 1. 本讲目标

本讲是第三单元（多进程 MP 架构）的第一篇，目标是帮你建立一张「全局地图」。

读完本讲，你应该能够：

- 说清楚 LMCache 为什么要把 KV cache 管理从推理引擎进程里「拆出去」变成一个独立的 daemon（守护进程），这条思路在社区里被概括为 **no fate-sharing（不与引擎共命运）**。
- 画出 MP 架构里 **三类进程** 的拓扑：vLLM engine 进程、MP cache server daemon、MP coordinator daemon，并标注它们之间走的是 ZMQ、CUDA IPC 还是 HTTP。
- 区分 `v1/multiprocess/` 与 `v1/mp_coordinator/` 这两条职责线：前者是「单实例的 cache 运行时」，后者是「跨实例的舰队级协调器」。
- 理解 MP 架构带来的收益：跨 worker/跨实例共享 cache、把缓存簿记工作移出 GPU 关键路径、支撑 MoE 等大模型推理加速、以及 PD（Prefill/Decode）分离。

本讲只做「总览 + 关键入口精读」；ZMQ 消息队列、二进制协议、IPC 传递张量、HTTP API 等细节，分别留给 u3-l2 ~ u3-l4 展开。

## 2. 前置知识

本讲建立在 u1-l3（目录结构）和 u1-l4（进程入口）之上。如果你还没读，至少先记住以下几点：

- **KV cache**：Transformer 注意力机制为历史 token 缓存的 Key/Value 向量，显存随上下文长度线性增长（一条 8K 请求约 1 GiB）。LMCache 把这些「算完即扔」的 KV 变成可复用资产（见 u1-l1）。
- **`LMCacheEngine` 的三大 API**：`store` / `retrieve` / `lookup`（见 u1-l6）。在「非 MP 模式」下，`LMCacheEngine` 直接跑在推理引擎进程内部；本讲要讲的是另一种部署形态——把它搬到独立进程里。
- **三种 console script / 入口**（见 u1-l4）：`lmcache`（带子命令的 CLI）、`lmcache_server`（原始 socket 服务）、`lmcache_controller`（HTTP 编排服务）。本讲会用到两个 CLI 子命令：`lmcache server` 和 `lmcache coordinator`。
- **进程（process）vs 线程（thread）**：进程有独立的地址空间和生命周期；线程共享所属进程的内存。MP 架构的核心就是把 cache 状态放进一个**独立进程**，从而拥有独立于引擎的生命周期。
- **daemon（守护进程）**：长期在后台运行、为其他进程提供服务的进程。本讲里的 MP server 和 coordinator 都是 daemon。
- **ZMQ（ZeroMQ）**：一种高性能异步消息库，常用于进程间通信（IPC）。LMCache 用它在 worker 与 cache server 之间传控制消息。
- **CUDA IPC / 共享内存（SHM）**：让一个进程把 GPU 张量或内存段「借」给另一个进程的机制，避免拷贝。worker 把 KV 交给 cache server 时会用到。

如果你对这些术语还比较陌生，先记住一句话：**MP 架构 = 把 cache 搬进独立进程，worker 通过消息队列找它存取 KV，多个 cache server 再由一个协调进程统一管理。** 细节我们边看源码边补。

## 3. 本讲源码地图

本讲涉及的关键文件，按「设计文档 → 两个 daemon 入口 → 配置 → worker 侧连接器」排列：

| 文件 | 作用 |
| --- | --- |
| [docs/design/v1/multiprocess/mp_runtime_plugin.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/multiprocess/mp_runtime_plugin.md) | MP server 侧「运行时插件」设计文档，说明 MP server 聚合了多份独立配置（`MPServerConfig` / `StorageManagerConfig` / `ObservabilityConfig` 等）。 |
| [docs/design/v1/mp_coordinator/README.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/mp_coordinator/README.md) | coordinator 的骨干设计：REST API、实例注册表、健康检查循环，以及「为什么需要 fleet 级协调」。 |
| [lmcache/v1/multiprocess/server.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/server.py) | **MP cache server 的组合器与启动函数**：`MPCacheServer` 类、`run_cache_server()`、`_build_modules()`。本讲精读的核心。 |
| [lmcache/v1/multiprocess/config.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/config.py) | `MPServerConfig`（server 自身配置）与 `CoordinatorConfig`（server 如何加入 coordinator）。 |
| [lmcache/v1/multiprocess/engine_module.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/engine_module.py) | `EngineModule` 协议与 `HandlerSpec`：server 内部「可插拔引擎模块」的抽象。 |
| [lmcache/v1/multiprocess/http_server.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/http_server.py) | `run_http_server()`：`lmcache server` 真正调用的入口，它同时拉起 ZMQ cache server 和 HTTP 前端。 |
| [lmcache/v1/mp_coordinator/app.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/app.py) | **coordinator 的 FastAPI app 工厂**：`create_app()`、lifespan、健康/淘汰后台循环。 |
| [lmcache/v1/mp_coordinator/__main__.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/__main__.py) | coordinator 进程入口：`python -m lmcache.v1.mp_coordinator` 调用的 `main()`。 |
| [lmcache/v1/mp_coordinator/registrar.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/registrar.py) | server 侧「向 coordinator 注册/心跳/注销」的辅助函数 `keep_registered()`。 |
| [lmcache/integration/vllm/vllm_multi_process_adapter.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_multi_process_adapter.py) | worker 侧连接器：`LMCacheMPWorkerAdapter`（GPU 搬 KV）与 `LMCacheMPSchedulerAdapter`（CPU 查命中），通过 ZMQ 连到 server。 |
| [lmcache/cli/commands/server.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/commands/server.py) / [lmcache/cli/commands/coordinator.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/commands/coordinator.py) | `lmcache server` 与 `lmcache coordinator` 两个 CLI 子命令。 |

> 提示：按本仓库约定（见 u1-l3），`docs/design/` 镜像 `lmcache/` 包树——读 `lmcache/v1/multiprocess/` 的代码前，先读 `docs/design/v1/multiprocess/` 对应文档。本讲正是这么做的。

## 4. 核心概念与源码讲解

### 4.1 为什么要把 KV cache 管理独立成 daemon

#### 4.1.1 概念说明

在 u1-l6 里，你已经认识 `LMCacheEngine`。在**非 MP（in-process）模式**下，`LMCacheEngine` 直接嵌在 vLLM 的 worker 进程里——引擎一边做前向推理，一边自己管 cache。这种方式简单，但有几个痛点：

1. **共命运（fate-sharing）**：引擎 worker 一旦崩溃、被 OOM kill、或者重新加载模型，它进程内的 cache 状态（`StorageManager` 里的 KV、索引、引用计数）会跟着一起没。好不容易攒下的热缓存，因为一次重启全丢了。
2. **抢关键路径**：cache 的簿记工作（token 哈希、查找、淘汰、序列化压缩）本质是 **CPU 密集** 的，而推理前向是 **GPU 密集** 的。两者挤在同一个进程里，簿记线程会和前向抢 CPU、抢 GIL，拖慢首 token 延迟（TTFT）。
3. **无法跨 worker/跨实例共享**：vLLM 做张量并行（TP）时每个 rank 是一个 worker 进程，各自一份 in-process cache 互不共享；多个模型副本之间更是各管各的。

MP（Multi-Process）架构的解法就是：**把 cache 管理从引擎进程里拆出来，变成一个独立的 daemon 进程。** 这就是社区常说的 **no fate-sharing（不与引擎共命运）**——cache daemon 的生命周期不再绑死在某个引擎 worker 上。

这条思路带来的直接收益包括：

- **cache 可被多 worker 共享**：一个 TP 组里的多个 worker、甚至多个模型副本，都可以连到同一个 cache daemon，命中彼此存过的 KV。
- **簿记移出 GPU 关键路径**：哈希/淘汰/压缩在 daemon 进程里跑，引擎前向只负责「发请求 / 收 KV」，不再被簿记阻塞——这对降低 TTFT、提升吞吐至关重要，也是 MP 架构能**加速 MoE 推理**的关键之一（MoE 模型显存压力极大，更需要把 KV 卸载和簿记解耦出去）。
- **支撑 PD 分离**：Prefill worker 算完 KV 存进 daemon，Decode worker 再取出来复用，两个引擎角色通过同一个 daemon 衔接（见 u4-l7）。

> 术语：**fate-sharing / no fate-sharing** 来自分布式系统设计——「共命运」指多个组件的生命周期绑在一起，一个挂全挂；MP 架构刻意让 cache 与引擎「不共命运」。

#### 4.1.2 核心流程

从「为什么」到「怎么做」的推理链：

```text
痛点: 引擎进程崩溃 → cache 丢失；簿记抢 CPU；多 worker 不共享
   │
   ▼
思路: 把 cache 搬进独立进程（daemon），与引擎解耦
   │
   ├─ 引擎侧只留「瘦连接器」：发请求、搬 KV，逻辑极薄
   ├─ daemon 侧承载重逻辑：StorageManager / 哈希 / 淘汰 / blend / 序列化
   └─ 通信: 控制消息走 ZMQ；GPU 张量走 CUDA IPC / 共享内存（零拷贝）
   │
   ▼
收益: 跨 worker 共享 + 簿记不阻塞前向 + PD 分离 + MoE 加速
```

注意：MP 架构是**可选的**部署形态，不是唯一形态。单机、不需要跨 worker 共享的场景，仍然可以用 in-process 模式（u2 系列讲的那套）。MP 是为「多 worker / 多实例 / PD 分离 / 高并发」准备的重型武器。

#### 4.1.3 源码精读

这条「拆进程」的动机，最直接的代码佐证是 server 进程的入口注释——它自称「unified cache server entry point」（统一 cache 服务入口）：

> [lmcache/v1/multiprocess/server.py:1-L2](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/server.py#L1-L2) —— 模块 docstring 写明本文件是 `MPCacheServer` 组合器与「统一 cache server 入口」。

而 MP server 与「非 MP 模式」的关键区别，在运行时插件设计文档里点得很清楚：**非 MP 模式只有一份 `LMCacheEngineConfig`，而 MP 模式有多份独立的配置 dataclass**（server 自身、storage、observability 各一份）。这说明 MP server 是一个「自带完整配置体系、独立运行」的进程，而不是引擎进程里的一个对象：

> [docs/design/v1/multiprocess/mp_runtime_plugin.md:L11-L17](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/multiprocess/mp_runtime_plugin.md#L11-L17) —— 文档明确指出 MP 模式有多份独立配置（`MPServerConfig`、`StorageManagerConfig`、`ObservabilityConfig` 等），与非 MP 模式的单一 `LMCacheEngineConfig` 形成对比。

coordinator 的设计文档则把「为什么需要舰队级协调」的痛点列得很直白——「mp servers are independent today（今天的 mp server 彼此独立）」：

> [docs/design/v1/mp_coordinator/README.md:L11-L17](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/mp_coordinator/README.md#L11-L17) —— 说明每个 mp server 各自独立、配额在实例内、没有跨节点 token 匹配路由，coordinator 正是这些 fleet 级能力要挂载的地方。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：用源码佐证「MP server 是独立进程、且配置体系独立于引擎」。
2. **操作步骤**：
   - 打开 `lmcache/v1/multiprocess/config.py`，找到 `MPServerConfig` 数据类。
   - 再回想 u1-l5 里的 `LMCacheEngineConfig`（由 `config_base` 动态生成、单一事实来源）。
   - 对比两者：`MPServerConfig` 是普通 `@dataclass`，字段如 `host`/`port`/`chunk_size`/`max_gpu_workers`，明显是「一个网络服务」的配置，而不是「引擎内的一个对象」的配置。
3. **需要观察的现象**：`MPServerConfig` 里有 `host`、`port` 这种「监听地址」字段——in-process 的引擎对象是不需要监听端口的，这反证了它是个独立网络进程。
4. **预期结果**：你能用一句话向别人解释「为什么 `MPServerConfig` 里会有 `host`/`port`」——因为它是个 daemon。

> 参考位置：[lmcache/v1/multiprocess/config.py:L16-L30](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/config.py#L16-L30) —— `MPServerConfig` 的前几个字段。

#### 4.1.5 小练习与答案

**练习 1**：如果一个 vLLM worker 用 in-process 模式跑 `LMCacheEngine`，某次因显存 OOM 被 kill 重启，cache 会怎样？换成 MP 模式呢？

> **参考答案**：in-process 模式下，cache 状态在 worker 进程内，进程被 kill = cache 全丢（fate-sharing）。MP 模式下，cache 在独立的 daemon 进程里，worker 重启不影响 daemon，cache 仍在——下次 worker 重连即可继续命中（no fate-sharing）。

**练习 2**：为什么把 cache 簿记（哈希/淘汰/序列化）从引擎进程搬到 daemon，能降低 TTFT？

> **参考答案**：前向推理是 GPU 密集、对延迟敏感的关键路径；簿记是 CPU 密集。两者同进程时会抢 CPU 和 GIL，簿记阻塞前向。拆进程后，引擎前向只做「发请求/收 KV」，簿记在另一个进程的线程池里并行跑，不再拖慢首 token。

---

### 4.2 MP 全景拓扑：三类进程与两条职责线

#### 4.2.1 概念说明

理解了「为什么要拆进程」，接下来要看「拆成了哪些进程、它们怎么连」。MP 架构里有 **三类进程**，分属 **两条职责线**：

**三类进程：**

1. **vLLM engine 进程**（你的推理服务本身）。它内部又分两个角色（见 u2-l5）：
   - **Scheduler**（CPU 侧）：决定哪些请求命中、要不要分配。在 MP 模式下它用 `LMCacheMPSchedulerAdapter` 做「查命中（lookup）」。
   - **Worker**（GPU 侧）：真正搬 KV。在 MP 模式下它用 `LMCacheMPWorkerAdapter` 存/取 KV。
2. **MP cache server daemon**（`v1/multiprocess/`）。一个独立进程，监听一个 ZMQ 地址（如 `tcp://host:5555`），持有 `StorageManager`（L1 CPU / L2 磁盘·远端），跑各种 engine module（lookup / transfer / blend / management）。它是「单实例的 cache 运行时」。
3. **MP coordinator daemon**（`v1/mp_coordinator/`，**可选**）。一个独立的 FastAPI/uvicorn HTTP 进程，管理**多个** cache server 组成的舰队（fleet）：谁还活着、配额用了多少、该淘汰谁、跨实例的 blend 命中该路由给谁。

**两条职责线：**

| 职责线 | 代码位置 | 通信方式 | 解决的问题 |
| --- | --- | --- | --- |
| **per-worker runtime**（单实例运行时） | `v1/multiprocess/` | ZMQ（控制）+ CUDA IPC/SHM（张量） | 一个或多个 worker 如何共用一个 cache daemon 存取 KV |
| **fleet coordination**（舰队协调） | `v1/mp_coordinator/` | HTTP/REST | 多个 cache server 之间如何发现彼此、统一配额与淘汰、跨实例 blend |

一句话区分：**`multiprocess/` 管「一个 cache 怎么存取」，`mp_coordinator/` 管「一群 cache 怎么协作」。** 前者是必选（开了 MP 就有），后者是可选（opt-in，设了 coordinator URL 才启用）。

#### 4.2.2 核心流程

把它们连起来的进程拓扑图如下（这是本讲最重要的图，后面的实践就要你亲手画它）：

```text
┌──────────────────────────── vLLM engine 进程（每副本/每 TP 组）────────────────────────────┐
│                                                                                          │
│   Scheduler (CPU) ──── LMCacheMPSchedulerAdapter ────┐                                    │
│        (查命中 lookup)                                  │ ZMQ 发 LOOKUP 请求               │
│   Worker   (GPU) ──── LMCacheMPWorkerAdapter ─────────┤ ZMQ 发 STORE/RETRIEVE             │
│        (搬 KV)                                          │ + CUDA IPC / SHM 传 GPU 张量      │
└────────────────────────────────────────────────────────┼───────────────────────────────────┘
                                                         │
                                          控制面: ZMQ tcp://host:5555
                                          数据面: CUDA IPC handle / POSIX SHM
                                                         │
                                                         ▼
                              ┌──────────── MP cache server daemon（v1/multiprocess/）────────────┐
                              │                                                                   │
                              │   MessageQueueServer  (绑定 tcp://host:port)                      │
                              │        │                                                           │
                              │   MPCacheServerContext  (共享状态)                                  │
                              │        ├─ StorageManager  (L1 CPU 热缓存 / L2 磁盘·远端)            │
                              │        ├─ TokenHasher     (按 chunk_size 切 token 算 key)          │
                              │        └─ SessionManager                                            │
                              │   EngineModules:                                                   │
                              │        ├─ LookupModule        (命中查询)                           │
                              │        ├─ P2PController        (对等发现)                           │
                              │        ├─ ManagementModule     (worker 存活/回收 reap)             │
                              │        ├─ LMCacheDrivenTransfer / EngineDrivenTransfer (搬 KV)     │
                              │        └─ BlendV3Module (可选, engine_type=blend)                  │
                              └───────────────────────────────────────────────────────────────────┘
                                                         │
                                          控制面: HTTP/REST（可选, opt-in）
                                          (注册/心跳/配额/淘汰/blend 路由)
                                                         │
                                                         ▼
                              ┌──────────── MP coordinator daemon（v1/mp_coordinator/）──────────┐
                              │                                                                   │
                              │   FastAPI (uvicorn http://host:9300)                               │
                              │        ├─ InstanceRegistry   (成员: 谁还活着)                      │
                              │        ├─ QuotaManager        (配额)                               │
                              │        ├─ L2UsageManager      (L2 用量)                            │
                              │        ├─ L2EvictionManager   (L2 淘汰)                            │
                              │        └─ GlobalBlendMatcher  (跨实例 blend 目录)                  │
                              │   后台循环: _health_loop (踢失联) / _eviction_loop (按配额淘汰)     │
                              └───────────────────────────────────────────────────────────────────┘
```

读图要点：

- **engine → cache server**：控制消息（LOOKUP/STORE/RETRIEVE）走 ZMQ；GPU 张量走 CUDA IPC handle 或 POSIX 共享内存（零拷贝，细节在 u3-l2）。
- **cache server → coordinator**：纯 HTTP/REST。server 启动时（若配了 `--coordinator-url`）向 coordinator 注册、周期性心跳，挂了就发 DELETE。coordinator 反向找 server 时，也是直接 HTTP 调 server 的某个端点。
- **coordinator 是可选的**：不配 URL，cache server 照常独立运行；配了才加入舰队。

#### 4.2.3 源码精读

拓扑图里「engine → cache server 走 ZMQ」这一段，在 worker 侧连接器里有直接证据：`LMCacheMPWorkerAdapter` 持有一个 `MessageQueueClient`，连的就是 server 的 ZMQ 地址：

> [lmcache/integration/vllm/vllm_multi_process_adapter.py:L1102-L1102](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_multi_process_adapter.py#L1102-L1102) —— worker 适配器创建 ZMQ 客户端：`self.mq_client = MessageQueueClient(server_url, context)`，这是 worker 连向 cache daemon 的「那一根线」。

而「cache server → coordinator 走 HTTP、且可选」这一段，在 coordinator README 的传输表里写得很清楚：

> [docs/design/v1/mp_coordinator/README.md:L22-L36](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/mp_coordinator/README.md#L22-L36) —— 列出 `POST /instances`（注册）、`PUT /instances/{id}/heartbeat`（心跳）、`DELETE /instances/{id}`（注销）等 REST 端点，并说明 coordinator 反向 push 时也是 HTTP 调 server 的具体端点。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：在源码里找到「三类进程各自的入口函数」，验证拓扑图。
2. **操作步骤**：
   - engine 进程：在 `lmcache/integration/vllm/vllm_multi_process_adapter.py` 找 `LMCacheMPSchedulerAdapter`（约 L562）和 `LMCacheMPWorkerAdapter`（约 L1042）两个类——它们就是 engine 进程内的「瘦连接器」。
   - cache server daemon：找 `lmcache/v1/multiprocess/server.py` 的 `run_cache_server()`（L283）。
   - coordinator daemon：找 `lmcache/v1/mp_coordinator/__main__.py` 的 `main()`（L20）。
3. **需要观察的现象**：三类入口分别用了三种不同的「服务器」——ZMQ `MessageQueueServer`、uvicorn/FastAPI、以及 worker 侧的 `MessageQueueClient`。
4. **预期结果**：你能把三个文件名/函数名与拓扑图里的三个方框一一对应。

#### 4.2.5 小练习与答案

**练习 1**：拓扑图里 engine 到 cache server 用了两种通道（ZMQ 和 CUDA IPC/SHM），为什么不用一种？

> **参考答案**：ZMQ 擅长传「控制消息/小数据」（请求类型、key、mask），但把 GPU 张量序列化进 ZMQ 会很慢。CUDA IPC/SHM 能让进程间零拷贝共享 GPU/内存张量，适合传大块 KV。所以控制面走 ZMQ、数据面走 IPC/SHM，各取所长（细节见 u3-l2）。

**练习 2**：如果完全不需要跨实例协调，只在一台机器上跑，coordinator 必须启动吗？

> **参考答案**：不必。coordinator 是 opt-in：cache server 只在配置了 `--coordinator-url`（或 `LMCACHE_COORDINATOR_URL`）时才注册。不配，server 独立运行，拓扑图最下面那个方框整个不存在。

---

### 4.3 `multiprocess/` server：单实例的 cache daemon

#### 4.3.1 概念说明

现在我们zoom进 cache server daemon 本身。它的核心是 `server.py` 里两个东西：

- **`MPCacheServer`**：一个「组合器（compositor）」。它本身几乎不干活，只负责把一组**可插拔的 engine module**（`EngineModule`）组装起来，并对外提供聚合的 `report_status()` / `close()`。
- **`run_cache_server()`**：真正的启动函数。它建上下文、建模块、建 ZMQ server、注册 handler、起线程池，然后阻塞保持运行。

这里有个重要的设计思想——**「组合器 + 可插拔模块」**：cache server 的能力（lookup、transfer、blend、management……）不是写死在一个大类里，而是拆成一个个 `EngineModule`，每个模块自带一组 ZMQ handler。`_build_modules()` 根据配置决定加载哪些模块。这让你能按需启用 blend、选择 transfer 模式等，而不用动核心代码。

另一个关键对象是 **`MPCacheServerContext`**（共享上下文）：所有模块共享它，里面装着 `StorageManager`、`TokenHasher`、`SessionManager`、`chunk_size` 等。模块之间不直接互调，而是通过这个共享上下文协作——这是典型的「共享上下文 + 独立模块」解耦模式。

#### 4.3.2 核心流程

`run_cache_server()` 的启动流程（伪代码）：

```text
run_cache_server(mp_config, storage_manager_config, obs_config, coordinator_config):
    1. 初始化可观测性 (event_bus / Prometheus / trace)
    2. (engine_driven 模式) 校验 /dev/shm 共享内存容量够不够
    3. 建 MPCacheServerContext  (持有 StorageManager / TokenHasher / chunk_size ...)
    4. modules = _build_modules(ctx, mp_config, coordinator_config)
           ├─ LookupModule
           ├─ P2PController(coordinator_config)   # 用于对等发现
           ├─ 按 supported_transfer_mode 选 transfer 模块
           │     (lmcache_driven / engine_driven / auto 两个都要)
           ├─ 按 engine_type 选 blend 模块 (可选, 'blend' → BlendV3Module)
           └─ ManagementModule(liveness_targets=transfer+blend 模块)
    5. engine = MPCacheServer(ctx, modules)
    6. server = MessageQueueServer(bind_url=tcp://host:port)
    7. 把每个 module 的 handler 注册进 server
    8. 起两类线程池: AFFINITY 池(GPU, STORE/RETRIEVE) + NORMAL 池(CPU, LOOKUP)
    9. torch_dev.init(); server.start()
   10. 阻塞循环 (while True: sleep) 直到 Ctrl-C → 清理退出
```

几个要点：

- **线程池分两类**：`AFFINITY`（GPU 亲和，跑 STORE/RETRIEVE，受 `max_gpu_workers` 限制）和 `NORMAL`（CPU，跑 LOOKUP 等，受 `max_cpu_workers` 限制）。这延续了 4.1 讲的「把 GPU 搬运和 CPU 簿记分开调度」的思想。
- **liveness 目标**：`ManagementModule` 会盯着 transfer/blend 模块里的「每 worker 注册状态」，定期 reap（回收）失联 worker 的资源——这是 no fate-sharing 的另一面：daemon 不会为已经死掉的 worker 永远占着内存。

#### 4.3.3 源码精读

**`MPCacheServer` 组合器**——注意它只是持有 context 和 modules 列表，`report_status()` 聚合各模块状态：

> [lmcache/v1/multiprocess/server.py:L64-L75](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/server.py#L64-L75) —— `MPCacheServer` 的类定义与 docstring，自称「assembles pluggable engine modules（组装可插拔引擎模块）」。

**`run_cache_server()` 的签名**——注意它接受四份独立配置，印证 4.1 讲的「MP 模式配置体系独立」：

> [lmcache/v1/multiprocess/server.py:L283-L309](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/server.py#L283-L309) —— 启动函数签名，入参为 `mp_config` / `storage_manager_config` / `obs_config` / `coordinator_config` 四份配置。

**建 ZMQ server 并绑定地址**——这是「独立网络进程」的铁证：

> [lmcache/v1/multiprocess/server.py:L364-L368](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/server.py#L364-L368) —— `MessageQueueServer(bind_url=f"tcp://{mp_config.host}:{mp_config.port}")`，daemon 在此监听。

**两类线程池**——把 GPU 搬运与 CPU 簿记分开：

> [lmcache/v1/multiprocess/server.py:L377-L390](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/server.py#L377-L390) —— 按 `ThreadPoolType.AFFINITY` / `NORMAL` 把 handler 分到两个线程池，分别受 `max_gpu_workers` / `max_cpu_workers` 限制。

**`_build_modules()` 按配置选模块**——以 transfer 模式为例：

> [lmcache/v1/multiprocess/server.py:L195-L205](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/server.py#L195-L205) —— 根据 `supported_transfer_mode`（`lmcache_driven` / `engine_driven` / `auto`）决定加载哪个 transfer 模块，未知值直接 `raise ValueError`（早失败）。

**`EngineModule` 协议**——定义了模块契约：`get_handlers()` / `report_status()` / `close()`：

> [lmcache/v1/multiprocess/engine_module.py:L43-L65](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/engine_module.py#L43-L65) —— `EngineModule` Protocol，每个模块自带一组 handler 注册进 ZMQ server。

> 说明：`lmcache server` 这个 CLI 子命令实际调用的是 `http_server.py` 里的 `run_http_server()`（[lmcache/v1/multiprocess/http_server.py:L207](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/http_server.py#L207)），它在 FastAPI 的 lifespan 里调用 `run_cache_server(return_engine=True)`——也就是说**同一个进程同时跑了 ZMQ cache server 和 HTTP 前端**。`run_cache_server()` 是其中「纯 ZMQ、不带 HTTP」的那条路径，便于单独理解（也方便测试）。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：跟踪 cache server 从「配参 → 组装模块 → 起 ZMQ」的完整启动链。
2. **操作步骤**：
   - 打开 `lmcache/v1/multiprocess/server.py`，从 `if __name__ == "__main__":` 块（约 L438）开始读。
   - 跟着 `parse_args()` → `parse_args_to_mp_server_config()` → `run_cache_server()` → `_build_modules()` 的顺序走一遍。
   - 数一下 `_build_modules()` 最终 `return` 的列表里有几个模块（[L274-L280](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/server.py#L274-L280)）。
3. **需要观察的现象**：模块列表 = `[lookup_module, p2p_controller, management, *transfer_modules, *blend_modules]`。在默认配置（`engine_type="default"`、`supported_transfer_mode="auto"`）下，blend 列表为空、transfer 有两个。
4. **预期结果**：默认配置下共 5 个模块（lookup + p2p + management + 2 个 transfer），没有 blend 模块。
5. 如果想本地实跑（需 GPU + 装好依赖）：`python -m lmcache.v1.multiprocess.server --host localhost --port 5555`，观察日志里 `LMCache ZMQ cache server is running on tcp://...`。**待本地验证**（无 GPU 主机可只做源码阅读部分）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_build_modules()` 里 transfer 模块要放在 management 模块之后 return，但 management 构造时却要把 transfer 模块作为 `liveness_targets` 传进去？

> **参考答案**：return 顺序决定 `close()` 的反序释放——management 排前，关闭时先停它的 reaper 再清 transfer 状态（见 L270-L273 注释）。而构造时 management 需要 transfer/blend 模块作为「存活目标」，因为它负责定期扫描这些模块里的 per-worker 注册、回收失联 worker 的资源。两个顺序服务不同目的。

**练习 2**：`AFFINITY` 线程池和 `NORMAL` 线程池分别跑什么？为什么分开？

> **参考答案**：`AFFINITY` 跑 GPU 相关的 STORE/RETRIEVE（搬 KV，受 `max_gpu_workers` 限制，控制 GPU 并发）；`NORMAL` 跑 CPU 相关的 LOOKUP/END_SESSION（受 `max_cpu_workers` 限制）。分开是为了不让 CPU 簿记任务挤占有限的 GPU 搬运并发，也避免 GPU 任务阻塞在 CPU 队列里。

---

### 4.4 `mp_coordinator/`：跨实例的舰队协调器

#### 4.4.1 概念说明

coordinator 解决的是「**一群** cache server 怎么协作」的问题。回想 4.1 引用的那句「mp servers are independent today」：每个 cache server 各自独立、配额在实例内、没有跨节点 token 匹配路由。当你部署了多个模型副本、跨多台机器时，你会想要：

- **成员管理（membership）**：知道当前舰队里有哪些 cache server 还活着。
- **统一配额与淘汰**：不要让某个租户/某个 salt 撑爆全舰队的 L2 存储；用量要全局看。
- **跨实例 blend 路由**：某个 token 序列在哪个 server 上存过？把 blend 查询路由过去。
- **L2 预取**：提前把热数据拉到该去的实例。

coordinator 的形态是一个**独立的 FastAPI / uvicorn HTTP 进程**（注意：它和 4.3 的 ZMQ cache server 是完全不同的两种 daemon）。它用 REST API 接收 server 的注册/心跳，在后台循环里做健康检查（踢失联）和 L2 淘汰（按配额）。关键设计原则有三条：

1. **restful + thin endpoints**：端点很薄，真正逻辑挂在 `app.state` 上的一组「共享协作者（shared collaborators）」上：`registry` / `quota_manager` / `usage_manager` / `eviction_manager` 等。
2. **opt-in + best-effort**：cache server 配了 coordinator URL 才注册；coordinator 挂了绝不阻塞 server（失败只记日志重试）。
3. **ephemeral registry**：注册表是临时的——coordinator 重启后从心跳重建；持久状态（如配额）应放外部存储（Redis），不放这里。

#### 4.4.2 核心流程

coordinator 启动与运行的流程（`create_app()` + lifespan）：

```text
create_app(config):
    1. 建共享协作者:
         registry = InstanceRegistry()        # 成员表 (id → ip, http_port, 心跳时间)
         quota_manager = QuotaManager()        # 配额
         usage_manager = L2UsageManager()      # L2 用量
         eviction_manager = L2EvictionManager(quota, usage, ...)  # 按配额淘汰
         prefetch_manager = PrefetchManager()
         token_hasher = TokenHasher(chunk_size, hash_algorithm)   # 解析 pin 的 token→key
         blend_directory = GlobalBlendMatcher(...)                # 跨实例 blend 目录
    2. 打包成 CoordinatorContext 挂到 app.state
    3. 定义后台循环:
         _health_loop()    : 定期 evict_stale() 踢掉失联实例
         _eviction_loop()  : 定期按配额向某个 server 派发淘汰 RPC
         _startup_resync() : 启动时从某个活 server 回填用量/淘汰状态
    4. lifespan: 起 httpx.AsyncClient + 三个后台 task; 退出时 cancel + aclose
    5. 自动发现 http_apis/ 下的所有 APIRouter 并 include
    6. 返回 FastAPI app (由 uvicorn 跑)

# 同时, 每个 cache server 侧 (若配了 coordinator URL):
keep_registered():  POST /instances → 周期 PUT /heartbeat → 退出时 DELETE /instances/{id}
```

注意 `eviction_loop` 里一个温和的设计（docstring 强调）：**没设显式配额的 salt 在外部 quota controller 给它设默认上限之前，不参与淘汰**——避免冷启动的空配额表把数据全淘汰掉。

#### 4.4.3 源码精读

**`create_app()` 建共享协作者**——这是 coordinator 的「大脑」：

> [lmcache/v1/mp_coordinator/app.py:L67-L101](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/app.py#L67-L101) —— 构建 `registry` / `quota_manager` / `usage_manager` / `eviction_manager` / `blend_directory` 等协作者。

**健康检查循环**——踢失联实例：

> [lmcache/v1/mp_coordinator/app.py:L113-L117](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/app.py#L113-L117) —— `_health_loop()` 周期调用 `evict_stale(registry, instance_timeout)`。

**L2 淘汰循环**——注意 docstring 里「未设配额的 salt 暂免淘汰」的保护：

> [lmcache/v1/mp_coordinator/app.py:L119-L129](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/app.py#L119-L129) —— `_eviction_loop()` 定期 `eviction_manager.execute_evictions(registry, http_client)`，向 server 派发淘汰 RPC。

**lifespan**——起后台 task、退出时清理：

> [lmcache/v1/mp_coordinator/app.py:L141-L173](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/app.py#L141-L173) —— lifespan 起一个 `httpx.AsyncClient`（绑定到运行中的事件循环）+ 三个后台 task，退出时 cancel 并 `aclose()`。

**进程入口**——`main()` 从环境变量读配置、建 app、交给 uvicorn：

> [lmcache/v1/mp_coordinator/__main__.py:L20-L30](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/__main__.py#L20-L30) —— `MPCoordinatorConfig.from_env()` → `create_app(config)` → `uvicorn.run(...)`。

**server 侧如何加入 coordinator**——`keep_registered()` 的注册调用：

> [lmcache/v1/mp_coordinator/registrar.py:L33-L70](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/registrar.py#L33-L70) —— `register()` 向 `POST {base_url}/instances` 发注册请求，body 用共享的 `schemas` 模型。

> 补充：coordinator 也通过 `lmcache coordinator` CLI 子命令启动（[lmcache/cli/commands/coordinator.py:L19-L28](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/commands/coordinator.py#L19-L28)），它从 `LMCACHE_MP_COORDINATOR_*` 环境变量读配置、CLI flag 覆盖，最后同样调 `create_app()` + uvicorn。

#### 4.4.4 代码实践（源码阅读型 + 可选实跑）

1. **实践目标**：理解「一个 cache server 如何加入 coordinator」，验证它是 opt-in 且 best-effort。
2. **操作步骤**：
   - 读 `lmcache/v1/mp_coordinator/registrar.py` 的 `keep_registered()`（约 L75 起）：找到它 `POST /instances`（注册）、`PUT /instances/{id}/heartbeat`（心跳，约 L128）、`DELETE /instances/{id}`（注销，约 L148）的三段。
   - 读 `lmcache/v1/multiprocess/config.py` 的 `CoordinatorConfig`（[L192-L218](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/config.py#L192-L218)）：注意 `url` 默认空字符串——空就不注册。
   - 读 `lmcache/v1/multiprocess/http_server.py` 里调用 `keep_registered()` 的条件（约 L123：`if coordinator_config.url and http_config is not None:`）。
3. **需要观察的现象**：注册逻辑被 `if coordinator_config.url` 包住；`url` 为空时整段不执行。
4. **预期结果**：你能解释「为什么 coordinator 挂了不会拖垮 cache server」——因为注册是 best-effort，失败只记日志重试，且整个 coordinator 集成是 opt-in。
5. **可选实跑（待本地验证，需装好依赖）**：参考 [examples/disagg_prefill_mp/README.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/disagg_prefill_mp/README.md) 的 `lmcache server --l1-size-gb 100 --eviction-policy LRU`，再开一个终端 `lmcache coordinator`，观察 server 日志里是否出现向 coordinator 注册的记录。

#### 4.4.5 小练习与答案

**练习 1**：coordinator 重启后，`InstanceRegistry` 里的成员会怎样？需要持久化吗？

> **参考答案**：注册表是 ephemeral（临时）的，coordinator 重启后为空，靠 server 周期心跳重新填充（README L131-L133 明确）。所以成员信息不需要持久化；真正需要持久的（如配额）应放外部存储（Redis），不放 coordinator 内存里。

**练习 2**：`_eviction_loop` 里为什么强调「没设配额的 salt 暂免淘汰」？

> **参考答案**：这是防「冷启动误杀」。coordinator 刚启动时配额表是空的/未同步的，如果立刻按默认配额淘汰，可能把大量数据误删。所以策略是：等外部 quota controller 通过 `PUT /quota/config` 显式设了默认上限（并 resync 了各 salt 用量）之后，该 salt 才纳入淘汰——避免空配额表 mass-evict。

---

## 5. 综合实践

把本讲所有内容串起来，完成下面这个「拓扑绘制 + 职责标注」任务。

**任务背景**：假设你要向团队介绍 LMCache 的 MP 部署形态，需要一张带职责标注的进程拓扑图。

**步骤**：

1. **画出三类进程的方框**：vLLM engine 进程（含 Scheduler + Worker 两角色）、MP cache server daemon、MP coordinator daemon。
2. **画出三条连接线并标注协议**：
   - engine Worker → cache server：标「ZMQ（STORE/RETRIEVE）+ CUDA IPC/SHM（KV 张量）」。源码依据：[`vllm_multi_process_adapter.py:L1102`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_multi_process_adapter.py#L1102-L1102) 的 `MessageQueueClient`。
   - engine Scheduler → cache server：标「ZMQ（LOOKUP）」。源码依据：[`vllm_multi_process_adapter.py:L562`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_multi_process_adapter.py#L562-L562) 的 `LMCacheMPSchedulerAdapter`。
   - cache server → coordinator：标「HTTP/REST（注册/心跳/配额/淘汰），opt-in」。源码依据：[`registrar.py:L33-L70`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/registrar.py#L33-L70) 与 [README 传输表](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/mp_coordinator/README.md#L22-L36)。
3. **给每个 daemon 方框标注启动入口与监听地址**：
   - cache server：入口 `run_cache_server()`（[`server.py:L283`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/server.py#L283-L283)），监听 `tcp://host:5555`（[`server.py:L365`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/server.py#L365-L365)）；CLI：`lmcache server`。
   - coordinator：入口 `main()`（[`__main__.py:L20`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/__main__.py#L20-L20)），监听 `http://host:9300`；CLI：`lmcache coordinator`。
4. **给每个 daemon 方框标注内部职责**：
   - cache server：`MPCacheServerContext`（StorageManager / TokenHasher）+ 一组 `EngineModule`（Lookup / P2P / Management / Transfer / Blend）。
   - coordinator：`registry` + `quota_manager` + `usage_manager` + `eviction_manager` + `blend_directory`，外加 `_health_loop` / `_eviction_loop` 两个后台循环。
5. **在图旁写一句 no fate-sharing 的解释**，并标出哪条连接是 opt-in（coordinator 那条）。

**验收标准**：把图拿给一个没读过 LMCache 源码的同事，他应该能看懂「谁连谁、走什么协议、各进程干什么、哪部分可选」。本讲 4.2.2 的拓扑图就是参考答案，你可以照着它自查。

## 6. 本讲小结

- **MP 架构的动机是 no fate-sharing**：把 cache 管理从引擎进程里拆成独立 daemon，避免引擎崩溃连累 cache、把 CPU 簿记移出 GPU 关键路径，并让多 worker/多实例共享 cache、支撑 PD 分离与 MoE 加速。
- **三类进程**：vLLM engine 进程（Scheduler + Worker 两角色）、MP cache server daemon（`v1/multiprocess/`）、MP coordinator daemon（`v1/mp_coordinator/`，可选）。
- **两条职责线**：`multiprocess/` 是「单实例 cache 运行时」（worker 用 ZMQ + CUDA IPC/SHM 连它）；`mp_coordinator/` 是「舰队级协调」（server 用 HTTP/REST 连它，opt-in）。
- **cache server = 组合器 + 可插拔模块**：`MPCacheServer` 组装一组 `EngineModule`（Lookup/P2P/Management/Transfer/Blend），共享 `MPCacheServerContext`；线程池分 `AFFINITY`（GPU 搬运）与 `NORMAL`（CPU 簿记）两类。
- **coordinator = FastAPI + 共享协作者 + 后台循环**：`registry`/`quota_manager`/`usage_manager`/`eviction_manager`/`blend_directory` 挂在 `app.state`；`_health_loop` 踢失联、`_eviction_loop` 按配额淘汰；注册表是临时的、集成是 best-effort 的。
- **两种 daemon 形态迥异**：cache server 用 ZMQ + 自定义二进制协议 + 线程池；coordinator 用 FastAPI/uvicorn + REST + asyncio 事件循环。下一讲会深入前者。

## 7. 下一步学习建议

本讲只搭了「全景骨架」，强烈建议按下面顺序深入各块血肉：

- **下一步首选 u3-l2《MP Server/Client 与进程间通信》**：拆开 `multiprocess/mq.py`（ZMQ 消息队列）、`futures.py`（异步结果等待）、`posix_shm.py`（共享内存）、以及 CUDA IPC handle 如何跨进程传 GPU 张量——把本讲拓扑图里「engine → cache server」那两根线（ZMQ + IPC/SHM）讲透。
- **u3-l3《MP Coordinator：跨实例协调》**：深入 `registry.py`（成员表）、`registrar.py`（注册流程）、`blend_directory.py`（跨实例 blend 目录），理解 coordinator 如何回答「哪个实例持有哪些 KV」。
- **u3-l4《HTTP API 与通信协议》**：对比 cache server 的二进制协议（`multiprocess/protocol.py` 的 `ClientCommand`/`ServerReturnCode`）与 coordinator 的 REST，以及 `http_api_registry.py` 如何让插件注册新 endpoint。
- 想看 MP 在 PD 分离里的真实用法，可先读 [examples/disagg_prefill_mp/README.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/disagg_prefill_mp/README.md)，对应 u4-l7。
- 复习时回头对照 u2-l5（vLLM 集成适配器）里关于 `LMCacheMPConnector` 如何在 scheduler/worker 两阶段被调用的描述——本讲的 `LMCacheMPSchedulerAdapter` / `LMCacheMPWorkerAdapter` 正是它在 MP 模式下的两个角色实现。
