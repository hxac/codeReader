# 多进程架构总览

> 承接上一讲：在 u1-l5 中我们站在「启动者」的视角，看清了 `normal_or_p_d_start` 如何把一组互相协作的子进程（router、detokenization、metric、visual…）一个个拉起来、分配好端口、注册好信号。但那些进程**拉起来之后，彼此是怎么说话的**？一个用户发来的 HTTP 请求，又是怎样在这堆进程之间跳来跳去，最终变回一段文字流式返回的？本讲就回答这个问题——从「进程如何被启动」切换到「进程如何协作」，建立一张贯穿整个第二单元的**请求流转地图**。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出 LightLLM 为什么选择「多进程 + 多种 IPC」的架构，以及它带来的好处与代价。
2. 准确描述 HttpServer / Router / ModelBackend / Detokenization / Metric / Health 等**每个进程模块的职责边界**，不混淆它们各自的分工。
3. 区分三类进程间通信手段——**zmq、rpyc、共享内存**——并说出它们各自适合传什么样的数据。
4. 画出一次**纯文本推理请求**从 HTTP 进入到 token 流式输出的完整数据流，标注每一跳用的是 zmq 端口、rpyc 还是共享内存。
5. 为后续 u2-l2（HTTP API）、u2-l4（ModelBackend 与 RPC）、u2-l5（Router 调度）等讲义建立一个稳固的整体认知底座。

## 2. 前置知识

本讲需要你已经具备（u1-l1 ~ u1-l5 已建立）的认知：

- **LightLLM 是多进程架构**：HttpServer / Router / ModelBackend / Detokenization 等是各自独立的进程。
- **进程是被 `api_start.py` 拉起的**：每个子进程用 `mp.Process` 启动，启动后通过 `Pipe` 回送 `init ok` 握手。
- **每个进程拿到的 `args` 里已经带好了端口**：如 `args.router_port`、`args.detokenization_port`、`args.http_server_port`、`args.metric_port` 等（这些端口是在 u1-l5 讲的端口分配阶段写进 `args` 的）。

如果你对下面三个 IPC（进程间通信）概念不熟，先建立一个最简印象，本讲会反复用到它们：

| 手段 | 直觉类比 | 在 LightLLM 里的角色 |
| --- | --- | --- |
| **zmq（ZeroMQ）** | 一个高性能「邮箱/收发室」，常用 PUSH/PULL（一对一投递）和 PUB/SUB（一对多广播） | 传**轻量的控制消息和通知**：比如「有一个新请求来了，索引是 #3」「有新 token 了，快来取」。 |
| **rpyc** | 「远程函数调用」——让 A 进程像调用本地函数一样调用 B 进程里的函数，并**能拿到返回值** | 传**需要应答的调用**：比如「初始化模型」「对这批数据做一次 prefill」「查一下当前指标」。 |
| **共享内存（shared memory / shm）** | 一块多个进程都能直接读写的「公共黑板」，写一次大家都能看到，**不用拷贝** | 放**大块且高频读写的公共状态**：比如请求对象 `Req`、输出 token 队列、token 负载统计。 |

> 一句话记住三者的分工：**zmq 传通知，rpyc 传调用，共享内存传大数据**。这三者的搭配正是 LightLLM 能做到「纯 Python 主体 + 高性能」的关键设计之一。

## 3. 本讲源码地图

本讲以官方架构文档为「骨架」，用三组源码文件为骨架「填上血肉」：

| 文件 | 作用 |
| --- | --- |
| [docs/EN/source/framework/framework.rst](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/docs/EN/source/framework/framework.rst) | **官方架构说明文档**。列出各模块职责，是本讲的「目录页」。 |
| [lightllm/server/api_start.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py) | 启动编排。本讲主要用它的「进程拉起顺序」来确认哪些进程常驻、它们各自拿到哪些端口。 |
| [lightllm/server/httpserver/manager.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py) | **HttpServer 进程**的核心。展示它如何把请求 PUSH 给下游、如何 SUB 回生成结果。 |
| [lightllm/server/router/manager.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py) | **Router 进程**的核心。展示它如何 PULL 请求、如何 PUSH 给 detokenization、如何持有多个 ModelRpcClient。 |
| [lightllm/server/router/model_infer/model_rpc.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py) | **ModelBackend 进程**与 rpyc 通信层。展示「每 GPU 一个 rpyc 服务进程」。 |
| [lightllm/server/detokenization/manager.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py) | **Detokenization 进程**的核心。展示它如何 PULL token、PUB 通知给 httpserver。 |
| [lightllm/server/metrics/manager.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py) | **Metric 进程**。展示指标如何经 rpyc 汇聚、再经 `/metrics` 端点暴露。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**架构总览 → 进程职责 → 进程间通信**。它们层层递进：先看全貌，再看每个零件，再看零件之间怎么连。

### 4.1 架构总览

#### 4.1.1 概念说明

LightLLM 的核心设计哲学，官方文档开篇一句话就点明了：

> The core design of lightllm is multi-process collaboration. Each process is responsible for a module, and multi-process collaboration is carried out through zmq and rpc.

翻译过来就是：**「把一个推理服务，拆成若干个各司其职的进程，进程之间用 zmq 和 rpc 协作」**。

这是一种典型的**「按职责切进程」**的架构。对比之下：

- 单进程异步框架（如纯 asyncio）：所有事情在一个进程里做，简单但难以充分利用多核、也容易因为某一段阻塞拖垮全局。
- LightLLM 的多进程方案：把「收请求」「调度」「推理」「解码」「监控」拆成独立进程，各自跑在自己的事件循环里，**互不阻塞**；GPU 推理这种重活在专门的 model 进程里，不会卡住负责收请求的 http 进程。

这种拆分的代价是：进程之间不能直接共享内存里的 Python 对象，必须显式地用 zmq / rpyc / 共享内存来通信——这正是本讲要讲清楚的「通信拓扑」。

#### 4.1.2 核心流程

官方文档把 LightLLM 的常驻模块归纳为七个：

[docs/EN/source/framework/framework.rst:4-14](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/docs/EN/source/framework/framework.rst#L4-L14) —— 这段是全篇的「目录」：第 4 行点明「多进程 + zmq/rpc 协作」的总纲；L7-L13 依次列出 Http Server、Metric Server、Health Server、Router、Visual Server、Cache Manager Server、Model Backend 七个模块及其一句话职责。

我们先把它们粗分成三层（一次纯文本请求只会动到第一层；多模态才会牵扯第二层；监控是横切的第三层）：

```
┌─────────────────────────── 第一层：请求主干（纯文本必经） ───────────────────────────┐
│                                                                                       │
│   HttpServer  ──zmq──▶  Router  ──rpyc──▶  ModelBackend(每 GPU 一个)                   │
│       ▲                   │                              │                            │
│       │                   └──────────zmq──────────────────┘                            │
│       │                               ▼                                                 │
│       └─────────────zmq PUB/SUB────  Detokenization                                    │
│                                                                                       │
└───────────────────────────────────────────────────────────────────────────────────────┘

┌─────────────── 第二层：多模态扩展（仅多模态请求经过） ───────────────┐
│   HttpServer ──zmq──▶ VisualServer/AudioServer ──▶ Router            │
│   HttpServer ──rpyc──▶ CacheManagerServer (存图像/特征)              │
└──────────────────────────────────────────────────────────────────────┘

┌─────────────── 第三层：横切监控（所有进程都用 rpyc 汇报） ───────────┐
│   各进程 ──rpyc──▶ MetricServer  ──▶ /metrics 端点 (Prometheus)      │
│   HealthMonitor / HttpServer 内的共享时间戳  ──▶ /health 端点        │
└──────────────────────────────────────────────────────────────────────┘
```

> 说明：本讲后续「核心流程」与「代码实践」都聚焦**第一层（请求主干）**，因为它是所有请求的必经之路，也是理解 LightLLM 最关键的部分。第二、三层会在 4.2 里点到为止，详细留到 u7（多模态服务、指标监控）。

#### 4.1.3 源码精读

哪些进程是「常驻」的？回到启动编排源码就能确认。在 `normal` 模式下，`normal_or_p_d_start` 按如下顺序拉起子进程（u1-l5 已讲过拉起机制，这里只看「拉起了谁」）：

- **Metric 进程**最先起（其它进程要往它上报指标，所以必须先就绪）：

[lightllm/server/api_start.py:478-483](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L478-L483) —— 拉起 `start_metric_manager`。

- **Router 与 Detokenization 同批起**（它们俩紧耦合：router 产出 token，detokenization 消费 token）：

[lightllm/server/api_start.py:485-491](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L485-L491) —— 一次 `start_submodule_processes` 同时拉起 `start_router_process` 和 `start_detokenization_process`。注意 Router 进程内部还会再 fork 出若干个 ModelBackend 子进程（见 4.3）。

- **HttpServer 由 hypercorn 拉起**（它是一个外部 ASGI 命令进程，不是 `mp.Process`）：

[lightllm/server/api_start.py:494-512](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L494-L512) —— 构造 `hypercorn ... lightllm.server.api_http:app` 命令并 `subprocess.Popen`，对外监听 `args.host:args.port`（默认 `127.0.0.1:8000`）。多模态、CPU 多级缓存等进程在更靠前的位置按需拉起（[api_start.py:423-476](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L423-L476)）。

这就和 4.1.2 的三层图对上了：常驻主干是 **HttpServer + Router + ModelBackend + Detokenization + Metric**。

#### 4.1.4 代码实践

**实践目标**：从「启动日志」反推「进程拓扑」，把抽象的架构图落到能看见的进程上。

**操作步骤**：

1. 用 `python -m lightllm.server.api_server --model_dir <某小模型> --tp 1` 启动一个服务（若本地无 GPU/模型，可跳过实际启动，直接做下面的源码阅读步骤）。
2. 在另一个终端执行 `ps -ef | grep lightllm`（或 `htop` 里按进程树看），观察进程名。注意每个进程都通过 `setproctitle` 设了可读名字，例如：
   - `lightllm::<服务名>::router_server`
   - `lightllm::<服务名>::detokenization_server`
   - `lightllm::<服务名>::model_infer:RANK0`
   - （metric 进程同理）
3. 对照 [model_rpc.py:162](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L162)、[detokenization/manager.py:172](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L172)、[router/manager.py:540](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L540) 这三处 `setproctitle` 调用，确认你看到的进程名与源码一一对应。

**需要观察的现象 / 预期结果**：你会看到一个清晰的「父进程（api_server）→ router → model_infer:RANK0」的进程树，外加 detokenization、metric 等同级进程。`--tp 2` 时应能看到两个 `model_infer:RANK0/1`。

> 若无法实际启动：**待本地验证**。可改为纯源码阅读——在 `manager.py` 各 `start_*_process` 函数里找到对应的 `setproctitle` 行，列一张「进程名 → 启动函数」对照表。

#### 4.1.5 小练习与答案

**练习 1**：官方文档列了七个模块，为什么本讲说「纯文本请求只动第一层」？
> **参考答案**：Visual Server 与 Cache Manager Server 是专门为多模态（图像/音频）服务的——前者负责编码图像/音频信息，后者负责缓存原始图像与编码后的特征。纯文本请求没有图像/音频，HttpServer 会直接把它交给 Router，跳过这两个模块（见 4.3.3 中 `transfer_to_next_module` 的分支逻辑）。

**练习 2**：Metric 进程为什么要比 Router 先启动？
> **参考答案**：因为 Router、HttpServer 等进程一启动就会通过 `MetricClient` 向 Metric 进程上报指标（如 `lightllm_request_count`）。如果 Metric 还没就绪，这些上报会失败或阻塞。把 Metric 排在最前，是为了让所有后续进程都能立即拿到一个可用的指标汇聚点。

---

### 4.2 进程职责

#### 4.2.1 概念说明

知道了「有哪些进程」，接下来要分清「每个进程到底负责什么」。这是理解架构最容易混淆的一步——很多人会误以为 Router 在做推理，或以为 HttpServer 在做调度。实际上每个进程的职责是**严格隔离**的。

LightLLM 给每个进程都取了一个能反映职责的名字，源码里通过 `setproctitle` 写进进程名，方便排查。我们逐个澄清：

| 进程 | 一句话职责 | 「它不管」的事 |
| --- | --- | --- |
| **HttpServer**（hypercorn + api_http） | 收 HTTP 请求、分词（tokenize）、把请求交给下游、把生成结果流式返回给用户 | 不做调度、不做 GPU 推理 |
| **Router** | 收请求入队、决定本轮 prefill 还是 decode、决定 prefill/decode 哪些请求、把 token 推给 detokenization | 不直接做 GPU 计算（它通过 rpyc 委托给 ModelBackend） |
| **ModelBackend**（每 GPU 一个） | 真正在 GPU 上跑模型：`init_model` / `prefill_batch` / `decode_batch` | 不管调度、不管 HTTP |
| **Detokenization** | 把生成的 token id 反向解码成文本、做增量解码、流式通知 httpserver | 不做推理、不直接回 HTTP 响应 |
| **Metric** | 汇聚所有进程上报的性能指标，供 `/metrics` 拉取 | 不参与请求处理 |
| **Visual / Cache Manager** | 多模态专用：编码图像、缓存图像/特征 | 纯文本时不参与 |

#### 4.2.2 核心流程

四个主干进程的职责，可以用「一次请求的生命周期」串起来。下面是纯文本请求经过的完整旅程（先建立直觉，4.3 再讲通信细节）：

```
用户 curl /generate
      │
      ▼
① HttpServer: 收请求 → tokenize → 在共享内存建好 Req 对象 → 把「请求索引」发给下游
      │
      ▼
② Router: 收到索引 → 从共享内存还原 Req → 入请求队列 → 调度循环决定 prefill/decode
      │                                            │
      │  (rpyc 调用 prefill_batch / decode_batch)  │
      ▼                                            ▼
③ ModelBackend(每 GPU 一个): 在 GPU 上算出新的 token id，写回共享内存里的 Req
      │
      │  (Router 把「这个请求要被解码了」的索引转给 detokenization)
      ▼
④ Detokenization: 从共享内存 Req 读出新 token id → 解码成文本 → 广播「有新文本了」
      │
      ▼
① HttpServer: 收到广播 → 从共享内存 Req 读出文本 → 拼进 HTTP 响应流式吐给用户
```

注意一个关键点：**真正的「大块数据」（prompt、生成的 token、logprobs）几乎不在线路上搬运，而是放在共享内存里的 `Req` 对象上；线上（zmq）传的只是轻量的「索引 / 通知」。** 这是 LightLLM 架构最精妙的设计之一，4.3 会展开。

#### 4.2.3 源码精读

**① HttpServer 的职责**——官方文档对它的描述：

[docs/EN/source/framework/framework.rst:26-31](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/docs/EN/source/framework/framework.rst#L26-L31) —— 列出 HttpServer 的四件事：收 API 请求；系统查询类请求找 Metric/Health；纯文本请求 tokenize 后发给 Router；多模态请求算 MD5、找 Cache Manager、打包发给 Visual Server。

代码里，HttpServer 的入口是 `HttpServerManager.generate(...)`，它正是上面「tokenize → 建 Req → 转交下游」的实现（详见 u2-l2）。本讲只看它「转交下游」这一个动作：

[lightllm/server/httpserver/manager.py:626-651](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L626-L651) —— `transfer_to_next_module` 是 HttpServer「分流」的枢纽：L632-L633 视觉、L636-L637 听觉、L640-L645 多级 KV 缓存，都不命中时落到 L647-L650，**把请求索引发给 Router**。注意它发的是 `group_req_objs.to_group_req_index()`（一组索引），而不是整个请求对象。

**② Router 的职责**——官方文档：

[docs/EN/source/framework/framework.rst:42-48](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/docs/EN/source/framework/framework.rst#L42-L48) —— Router 负责「存请求 + 调度」：收 HttpServer/Visual 发来的请求入队；决定本轮是 prefill 还是 decode；prefill 轮决定 prefill 哪些请求，decode 轮决定 decode 哪些请求。

Router 的核心是一个**事件循环** `loop_for_fwd` → `_step`，默认每 `schedule_time_interval`（默认 30ms）跑一圈（[router/manager.py:52](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L52)、[router/manager.py:221-227](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L221-L227)）。这个循环的细节是 u2-l5 的主题，这里只需记住：**Router 是调度的大脑，但不是算力的手**。

**③ ModelBackend 的职责**——官方文档：

[docs/EN/source/framework/framework.rst:64-70](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/docs/EN/source/framework/framework.rst#L64-L70) —— ModelBackend 由 `base_backend.py` 里的 `ModeBackend` 基类定义，三个关键函数：`init_model`（解析模型、选定模型类）、`prefill_batch`（对一批数据做 prefill）、`decode_batch`（对一批数据做 decode）。每个 backend 持有一个 `model`（真正的模型类，基类是 `TpPartBaseModel`）和一个 `tp_rank`（代表一个设备）；**backend 可以有多个**（每 GPU 一个）。

「每 GPU 一个 backend」这件事，在 rpyc 层体现得最清楚（详见 4.3）。

**④ Detokenization 的职责**——它在 `DeTokenizationManager` 里，主循环 `handle_loop` 做两件事：从 router 收「待解码请求索引」，再在 `gen_token_out` 里把新 token id 解码成文本并通知 httpserver：

[lightllm/server/detokenization/manager.py:100-153](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L100-L153) —— `gen_token_out` 遍历所有待解码请求，对每个有新 token 的请求调用 `decode_token` 解码、做停止串匹配，再把文本 `push` 进请求的输出队列（L142）；只要有新文本产出（L148 `exist_decode`），就向 httpserver 发一个通知（L149）。

#### 4.2.4 代码实践

**实践目标**：用「职责三问」自检，确保你分得清四个主干进程。

**操作步骤**：针对下面三个问题，只读源码、不运行，写出你的判断，再对照参考答案。

1. 「决定本轮该 prefill 哪些请求」这件事，代码在哪个进程？去 [router/manager.py:424-428](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L424-L428) 看 `_generate_new_batch` 是不是在 Router 里。
2. 「真正在 GPU 上算 attention」这件事，在哪个进程？去 [model_rpc.py:101](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L101) 看 `self.backend.init_model(kvargs)`——它跑在 `ModelRpcServer` 里，即 model 进程。
3. 「把 token id 变成可读文字」这件事，在哪个进程？去 [detokenization/manager.py:115-120](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L115-L120) 看 `decode_token` 调用。

**需要观察的现象 / 预期结果**：你会确认——调度在 Router、计算在 ModelBackend、解码在 Detokenization，三者绝不混淆。

#### 4.2.5 小练习与答案

**练习 1**：有人说「Router 负责推理，因为它持有 `model_rpc_clients`」，这句话对吗？
> **参考答案**：不对。Router 持有 `model_rpc_clients`（每个 GPU 一个），但这些是 **rpyc 客户端**——Router 只是通过它们「远程调用」model 进程里的 `prefill_batch`/`decode_batch`，真正的 GPU 计算发生在 model 进程内。Router 是「调度的大脑」，不是「算力的手」。

**练习 2**：HttpServer 既不做调度也不做推理，那它为什么还要 `tokenize`？能不能把 tokenize 推给 Router 做？
> **参考答案**：tokenize 放在 HttpServer 有两个好处：一是可以在进入下游前就做长度校验与截断（见 `httpserver/manager.py` 的 `_check_and_repair_length`），把非法请求挡在门外；二是多模态场景下，文本 token 数与图像 token 数需要一起估算，HttpServer 手里有完整的请求上下文，最适合做这件事。所以这个分工是有意为之。

---

### 4.3 进程间通信

#### 4.3.1 概念说明

明确了职责，剩下最关键的问题就是：**这些进程具体怎么通信？**

LightLLM 同时使用了三种 IPC 手段，每种都有它「不可替代」的场景：

- **zmq**：用于「投递通知 / 控制消息」。它不需要返回值，是「发出去就行」的单向通信。LightLLM 主要用两种模式：
  - **PUSH / PULL**：一对一可靠投递。一个进程 PUSH 出去，另一个进程 PULL 进来，天然负载均衡。
  - **PUB / SUB**：一对多广播。一个进程 PUB，所有订阅了的进程都能收到。
- **rpyc**：用于「需要返回值的远程调用」。比如 Router 要让 model 进程「算一次 prefill 并告诉我算完了」，必须用请求-应答模式，zmq 不擅长这个。
- **共享内存**：用于「多个进程都要频繁读写的大块公共状态」。典型例子是请求对象 `Req`（里面有 prompt token 数组、输出 token 队列、logprobs 数组等）。共享内存的好处是**零拷贝**——一个进程写进去，另一个进程直接读到，不用把数据序列化、跨进程搬运。

> 为什么不全用 rpyc？因为 rpyc 每次调用都要序列化参数、跨 socket 传、再反序列化，传大对象（比如几千个 token 的 prompt、整段 logprobs）既慢又费内存。共享内存恰好补上这个短板。三者各司其职，才能又快又灵活。

#### 4.3.2 核心流程

把三种 IPC 套到 4.2.2 的请求旅程上，每一跳用的手段就清楚了：

```
① HttpServer ──(共享内存: 写入 Req 对象)──┐
   └──(zmq PUSH → router_port)──────────▶ ② Router      【传的是请求索引，轻量】

② Router ──(rpyc: prefill_batch/decode_batch)──▶ ③ ModelBackend(每 GPU 一个)
   │        【rpyc 用于需要应答的调用；推理结果经共享内存 Req 回流】
   └──(zmq PUSH → detokenization_port)────────▶ ④ Detokenization  【传请求索引】

③ ModelBackend ──(共享内存: 把新 token id 写进 Req 的输出队列)

④ Detokenization ──(共享内存: 从 Req 读 token、写回解码后的文本)
   └──(zmq PUB → http_server_port)──────────▶ ① HttpServer  【PUB 一个"有新文本"的唤醒通知】

横切：所有进程 ──(rpyc → metric_port)──▶ MetricServer
      HttpServer 的 /metrics 端点 ──(rpyc generate_latest)──▶ MetricServer
```

记住这张图的「三色」：**蓝色=zmq（通知），绿色=rpyc（调用），橙色=共享内存（大数据）**。下面用源码逐条印证。

#### 4.3.3 源码精读

**A. HttpServer → Router：zmq PUSH/PULL**

HttpServer 端，构造一个 PUSH socket 连接到 `router_port`：

[lightllm/server/httpserver/manager.py:49-51](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L49-L51) —— `send_to_router = context.socket(zmq.PUSH)`，`connect(...127.0.0.1:{router_port})`。

Router 端，构造对应的 PULL socket 绑定同一个 `router_port`：

[lightllm/server/router/manager.py:81-83](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L81-L83) —— `zmq_recv_socket = context.socket(zmq.PULL)`，`bind(...127.0.0.1:{router_port})`。

> 经验法则：**PUSH 一端 `connect`，PULL 一端 `bind`**——这是 zmq 里「多个生产者投递给一个消费者」的常见接法。HttpServer（可能多个 hypercorn worker）是生产者，Router 是唯一消费者。

发送的内容是「索引」而非「对象」：

[lightllm/server/httpserver/manager.py:647-650](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L647-L650) —— `send_to_router.send_pyobj(group_req_objs.to_group_req_index(), ...)`，发的是 `to_group_req_index()`（把对象压扁成索引），真正的大对象在共享内存里。

**B. Router → ModelBackend：rpyc（每 GPU 一个服务）**

这是 LightLLM 通信设计里最值得品味的一段。Router 不直接持有模型，而是为**每个 GPU 设备** fork 一个 model 进程，里面跑一个 rpyc 服务：

[lightllm/server/router/model_infer/model_rpc.py:165-171](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L165-L171) —— 每个 model 进程在 `_init_env` 里 `model_rpc_server = ModelRpcServer(...)`，然后用 `ThreadedServer(model_rpc_server, socket_path=socket_path, ...)` 起一个 **rpyc 服务**。注意这里用的是 **Unix domain socket**（`socket_path`，形如 `/tmp/lightllm_model_infer_xxxx.sock`，见 [model_rpc.py:213-216](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L213-L216)）——同机进程间通信，比 TCP 更快。

[model_rpc.py:188-200](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L188-L200) —— `mp.Process(target=_init_env, ...)` 为每个 rank 开一个独立进程，这正是「每 GPU 一个 backend」的实现。

Router 端则为每个 model 进程建一个 **rpyc 客户端** `ModelRpcClient`，并把远程方法包成异步：

[model_rpc.py:115-137](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L115-L137) —— `ModelRpcClient` 把 `conn.root.init_model` 等远程方法用 `rpyc.async_` 包成异步，这样 Router 在「等 model 算完」时不会卡住自己的事件循环。Router 持有的 `self.model_rpc_clients` 就是一个「每 GPU 一个客户端」的列表（见 [router/manager.py:139](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L139)）。

被暴露出来的远程方法就是 `ModelRpcServer` 上以 `exposed_` 开头的方法：

[model_rpc.py:40-50](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L40-L50) —— `ModelRpcServer(rpyc.Service)`，`exposed_` 前缀是 rpyc 的约定，表示这个方法可以被远程调用。

> 小知识：rpyc 默认会把返回值做成「代理对象」（netref），跨进程访问仍走 socket，有性能损耗。LightLLM 在需要拿到真实数据的地方用 `from rpyc.utils.classic import obtain` 把它**一次性取回本地**（如 [model_rpc.py:146](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L146) 的 `obtain(await ans)`），避免反复跨进程访问。

**C. Router → Detokenization：zmq PUSH/PULL（传索引）**

Router 产出推理结果后，要告诉 detokenization「这个请求可以解码了」：

[lightllm/server/router/manager.py:85-86](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L85-L86) —— Router 建一个 PUSH socket 连接到 `detokenization_port`。

[lightllm/server/detokenization/manager.py:30-32](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L30-L32) —— Detokenization 建对应的 PULL socket 绑定 `detokenization_port`。

[router/manager.py:419-421](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L419-L421) —— Router 收到新请求后，把它 extend 进请求队列，并 `send_to_detokenization.send_pyobj(group_req_indexes, ...)`——同样只发索引，detokenization 拿索引去共享内存还原 Req。

**D. Detokenization → HttpServer：zmq PUB/SUB（唤醒通知）**

Detokenization 解码出新文本后，用一个 PUB 通知 HttpServer「来活了」：

[lightllm/server/detokenization/manager.py:34-36](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L34-L36) —— Detokenization 建一个 PUB socket **绑定** `http_server_port`（注意：这个 `http_server_port` 不是用户访问的 8000，而是 u1-l5 里分配的内部端口，专门给这对 PUB/SUB 用）。

[lightllm/server/detokenization/manager.py:148-149](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L148-L149) —— 只要本轮有新文本（`exist_decode`），就 `pub_to_httpserver.send_pyobj(None, ...)`——注意它发的是 **`None`**！因为这个消息只是个「唤醒信号」，真正的文本在共享内存的 Req 里，HttpServer 收到信号自己去取。

HttpServer 端订阅这个 PUB：

[lightllm/server/httpserver/manager.py:102-104](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L102-L104) —— `zmq_recv_socket = context.socket(zmq.SUB)`，`connect(...http_server_port)`，`setsockopt(zmq.SUBSCRIBE, b"")` 订阅所有消息。

[httpserver/manager.py:880-882](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L880-L882) —— `handle_loop` 里循环 `zmq_recv_socket.recv_pyobj()`，一旦收到唤醒，就去遍历各 Req 的输出队列，把新文本捞出来交给 HTTP 流式响应。

> 为什么用 PUB/SUB 而不是 PUSH/PULL？因为 HttpServer 可能有多个 hypercorn worker 进程，每个 worker 都需要收到「有新 token」的通知去各自的请求里取数据——这是一对多广播，正是 PUB/SUB 的主场。

**E. 共享内存：承载 Req 与公共状态**

整个通信里最「隐形」却最关键的一环是共享内存。HttpServer、Router、ModelBackend、Detokenization 都通过 `ShmReqManager` 拿到**同一个** `Req` 对象：

- HttpServer 创建并初始化 Req（[httpserver/manager.py:99](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L99) 的 `ShmReqManager()`、L399-L417 的 `async_alloc_req_index`/`async_get_req_obj_by_index`/`req_obj.init(...)`）；
- Router、Detokenization 各自 `ShmReqManager()` 用同样的索引还原出同一块内存（[router/manager.py:66](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L66)、[detokenization/manager.py:43](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L43)、[detokenization/manager.py:49-53](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L49-L53) 的 `link_prompt_ids_shm_array`/`link_logprobs_shm_array`）。

此外，还有一些**跨进程的标量公共状态**也走共享内存，用 `SharedInt` 承载，并用唯一服务名做前缀避免多实例冲突：

- `shm_max_total_token_num`：model 进程探测出真实显存能放多少 token 后，写进共享内存，让 HttpServer 提前拦截超长请求（[router/manager.py:65](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L65) 与 [httpserver/manager.py:133](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L133)）。
- `shared_token_load`：Router 写入的机器负载，HttpServer 读取用于 `/token_load` 端点（[router/manager.py:73](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L73)）。

> 这就是为什么 u1-l2 反复强调启动时要加 `--shm-size`——多进程协作**强依赖共享内存**，shm 太小会直接启动失败。

**F. 横切：Metric 与 Health（rpyc + 共享内存）**

每个进程都持有一个 `MetricClient`，它是个 rpyc 客户端，连接到 Metric 进程：

[lightllm/server/router/manager.py:96](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L96) 与 [httpserver/manager.py:112](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L112) —— 都构造 `MetricClient(args.metric_port)`。

Metric 进程则是一个 rpyc 服务（`MetricServer(rpyc.Service)`），暴露 `exposed_counter_inc` / `exposed_histogram_observe` / `exposed_gauge_set` / `exposed_generate_latest` 等方法：

[lightllm/server/metrics/manager.py:32-62](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py#L32-L62) —— `MetricServer` 与一众 `exposed_*` 方法。

[lightllm/server/metrics/manager.py:149-161](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py#L149-L161) —— `start_metric_manager` 用 `ThreadedServer(service, port=args.metric_port)` 把它挂在 TCP `metric_port` 上（注意：metric 用的是 TCP，而不是 model 那种 Unix socket）。

HttpServer 的 `/metrics` 端点，本质就是一次 rpyc 调用，把聚合好的 Prometheus 数据取回来：

[lightllm/server/api_http.py:405-410](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_http.py#L405-L410) —— `metrics()` 端点调用 `g_objs.metric_client.generate_latest()`，把返回的文本以 `text/plain` 吐出，供 Prometheus 拉取。

健康检查则更轻量——不用 rpyc，而是 HttpServer 直接读一个共享内存里的「最近一次成功推理时间戳」`latest_success_infer_time_mark`（[httpserver/manager.py:123-124](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L123-L124)），每次出 token 就更新它（[httpserver/manager.py:723](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L723)）。`/health`、`/liveness`、`/readiness` 等端点据此判断服务是否「活着」。

#### 4.3.4 代码实践

**实践目标**：把 4.3.2 的「三色数据流图」落到源码上，自己能指认每一跳的端口名与通信手段。这正是本讲规格里要求的「依据 framework.rst 绘制一张多进程数据流图，标注一次推理请求经过的进程与通信通道（zmq 端口 / rpyc）」。

**操作步骤**：

1. 准备一张白纸或一个文本文件，先照抄 4.1.2 的三层架构图。
2. 对照下面的「寻宝清单」，在源码里找到每条通信链路的端口名与 zmq 模式，并标到你的图上：
   - HttpServer→Router：[httpserver/manager.py:50-51](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L50-L51) 的 PUSH `connect(router_port)` ↔ [router/manager.py:82-83](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L82-L83) 的 PULL `bind(router_port)`。
   - Router→Detokenization：[router/manager.py:85-86](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L85-L86) 的 PUSH `connect(detokenization_port)` ↔ [detokenization/manager.py:31-32](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L31-L32) 的 PULL `bind(detokenization_port)`。
   - Detokenization→HttpServer：[detokenization/manager.py:34-35](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L34-L35) 的 PUB `bind(http_server_port)` ↔ [httpserver/manager.py:102-103](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L102-L103) 的 SUB `connect(http_server_port)`。
   - Router→ModelBackend：[model_rpc.py:165-171](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L165-L171) 的 rpyc `ThreadedServer`（Unix socket）↔ Router 侧的 `ModelRpcClient`（[model_rpc.py:208](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L208) `unix_connect`）。
   - 各进程→Metric：rpyc TCP `metric_port`（[metrics/manager.py:161](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py#L161)）。
3. 在每条边上用不同颜色（或标注）区分：**zmq PUSH/PULL、zmq PUB/SUB、rpyc、共享内存**。
4. 在图旁加一句注释：哪些边传的是「索引/通知」（轻量），哪些边传的是「真实数据」（走共享内存）。

**需要观察的现象 / 预期结果**：你会得到一张类似 4.3.2 的图，但**每条边都标注了具体端口名（如 `router_port`、`detokenization_port`、`http_server_port`）、zmq 模式（PUSH/PULL/PUB/SUB）或协议（rpyc/Unix socket/TCP）**。这张图就是后续 u2 各讲义的「导航地图」。

#### 4.3.5 小练习与答案

**练习 1**：Detokenization 通知 HttpServer 时 `send_pyobj(None)`，为什么发一个 `None` 就够了？
> **参考答案**：因为这条 PUB 消息只是个「唤醒信号」——告诉 HttpServer「有新 token 产生了，快去取」。真正的解码文本并不走 zmq，而是放在共享内存里的 `Req.out_tokens_queue` 中。HttpServer 收到 `None` 后，自己去遍历各个 Req 的输出队列取数据。用 `None` 而非具体数据，正是为了让「通知」和「数据」解耦：通知走 zmq（快、广播），数据走共享内存（零拷贝）。

**练习 2**：Router↔ModelBackend 用 rpyc，而各进程↔Metric 也用 rpyc，但底层传输一个是 Unix socket、一个是 TCP。为什么 ModelBackend 这边要用 Unix socket？
> **参考答案**：Router 和 ModelBackend 一定在同一台机器上（同一个 GPU 节点），且它们之间是**最高频**的调用（每个 prefill/decode step 都要调），对延迟极敏感。Unix domain socket 走内核内存拷贝、不经过网络协议栈，比 TCP 快得多。而 Metric 是汇聚所有进程的低频上报，用 TCP 更通用（甚至可以跨节点）。这是「按场景选传输」的典型取舍。

**练习 3**：HttpServer 把请求发给 Router 时，发的是 `to_group_req_index()`（索引）而不是整个 `Req` 对象。这样设计的好处是什么？
> **参考答案**：`Req` 对象里包含 prompt token 数组、输出队列、logprobs 等较大且会被多个进程反复读写的数据。如果每次都把整个对象序列化后经 zmq 传给 Router、再传给 Detokenization，既慢又容易在多份拷贝之间产生不一致。改成「对象放共享内存，线上只传索引」，所有进程读写的是**同一块内存**，既零拷贝又天然一致。这正是 LightLLM「轻量控制消息走 zmq、大数据走共享内存」分工的体现。

---

## 5. 综合实践

**综合任务**：动手画一张属于你自己的「LightLLM 多进程数据流图」，并用它向自己解释一次 `/generate` 请求的完整往返。

**要求**：

1. 节点至少包含：`用户/curl`、`Hypercorn(api_http)`、`HttpServerManager`、`Router`、`ModelBackend(RANK0)`、`Detokenization`、`MetricServer`。（多模态可选加 `VisualServer`、`CacheManagerServer`。）
2. 每条有向边必须标注三样东西：
   - **通信手段**：zmq PUSH / zmq PULL / zmq PUB / zmq SUB / rpyc / 共享内存；
   - **端口或通道**：如 `router_port`、`http_server_port`、`metric_port`、Unix socket、`ShmReqManager`；
   - **载荷**：是「请求索引」「唤醒通知(None)」还是「真实数据(Req/token)」。
3. 用虚线标出「请求方向」（从用户到生成），用实线标出「结果回流方向」（从 model 回到用户）。
4. 在图下用 5~8 句话，按时间顺序口述一次请求的往返（参考 4.2.2 与 4.3.2）。

**自检要点**：你的口述里应当能自然出现这些词——tokenize、PUSH 索引、PULL、调度循环、rpyc prefill/decode、共享内存写 token、PULL 索引、解码、PUB 唤醒、SUB 取文本、流式返回。如果某个词对不上某条边，就回到 4.3.3 的源码对照修正。

> 若想更进一步：在图上额外标出 `MetricClient` 的上报路径（各进程 → metric_port）和 `/metrics` 端点的拉取路径，体会「横切监控」与「请求主干」是两张独立的网。

## 6. 本讲小结

- LightLLM 的核心设计是**多进程协作**：HttpServer / Router / ModelBackend / Detokenization / Metric 等各司其职，进程之间用 zmq、rpyc、共享内存通信。
- 四个主干进程职责严格隔离：**HttpServer** 收请求并 tokenize、**Router** 调度、**ModelBackend**（每 GPU 一个）做 GPU 推理、**Detokenization** 把 token 解码成文本——调度在 Router、计算在 Model、解码在 Detokenization，互不混淆。
- 三类 IPC 各有分工：**zmq**（PUSH/PULL/PUB/SUB）传轻量通知，**rpyc** 传需要应答的远程调用，**共享内存**承载 `Req` 等大块公共状态、做到零拷贝。
- 一次请求的数据流是闭环：HttpServer PUSH 索引给 Router → Router rpyc 调 ModelBackend 算 token、同时 PUSH 索引给 Detokenization → Detokenization 解码后 PUB 唤醒 HttpServer → HttpServer 从共享内存取文本流式返回。
- 「对象放共享内存、线上只传索引」是贯穿全架构的关键设计，也是 LightLLM 强依赖 `--shm-size` 的根本原因。
- Metric 用 rpyc 汇聚各进程指标、经 `/metrics` 暴露；Health 则更轻量地用共享内存时间戳判断存活——监控是横切在请求主干之外的独立一张网。

## 7. 下一步学习建议

有了本讲这张「导航地图」，第二单元的后续讲义就是「逐段放大」某一条边或某个进程：

- **u2-l2 HTTP API 与请求分发**：放大「用户 → HttpServer → Router」这一段，看 `/generate`、`/v1/chat/completions` 等端点如何处理、`HttpServerManager.generate` 如何 tokenize 并建 Req。
- **u2-l3 请求对象与共享内存通信**：放大「共享内存」这一环，深入 `Req` 结构与 `ShmReqManager`/`ShmObjsIOBuffer` 的零拷贝机制。
- **u2-l4 Model Backend 推理后端与 RPC**：放大「Router → ModelBackend」这条 rpyc 边，看 `ModelRpcClient` 的异步包装与 `base_backend.py` 的 `infer_loop`。
- **u2-l5 Router 调度循环**：放大 Router 内部的 `loop_for_fwd` / `_step` 事件循环，看 30ms 一个 tick 的调度逻辑。
- **u2-l7 Detokenization 与流式输出**：放大「Router → Detokenization → HttpServer」这一段，看增量解码与 PUB/SUB 流式推送。

建议在进入下一篇之前，先确保你能合上源码、对着自己画的「三色数据流图」复述一次请求的完整往返——这张图是整个第二单元的基石。
