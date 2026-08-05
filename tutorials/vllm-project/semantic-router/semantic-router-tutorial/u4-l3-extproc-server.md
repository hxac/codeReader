# ExtProc gRPC 服务与 Router

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `pkg/extproc` 的 `Server` 是如何被创建、启动、热重载和停止的（`NewServer` / `Start` / `Stop` / `GetRouter` 生命周期）。
- 理解 `OpenAIRouter` 为什么是「一次请求的状态机」，它持有哪些核心状态字段，以及 `RouterService` 如何用一把原子指针实现「不停机换路由」。
- 描述 Envoy 通过 ExtProc gRPC 双向流，按 `请求头 → 请求体 → 响应头 → 响应体` 四个阶段回调路由器的完整交互模型。

本讲是「请求处理主链路」（u5）的前置：它只回答「服务怎么起来、Envoy 怎么把流量交进来、路由器内部拿什么记账」，**不**展开请求体里到底怎么分类、怎么决策——那是 u5、u6 的事。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**(1) 什么是 Envoy External Processor（ExtProc）。**
Envoy 是一个高性能反向代理。ExtProc 是 Envoy 的一个 HTTP 过滤器（filter），它允许你把一个**外部 gRPC 服务**挂到请求/响应的处理流水线上：Envoy 在代理流量的同时，会把 HTTP 的头部和正文「寄」给这个外部服务，由它决定要不要改写、要不要直接回包、要不要放行。语义路由器（SR）就是这个外部 gRPC 服务。换句话说：

- **Envoy 负责搬流量**（监听端口、做 TLS、做负载均衡、转发到 vLLM/OpenAI/Anthropic 后端）；
- **SR 负责做智能决策**（分类、投影、决策、选模型、改写头部）。

这种「数据面（搬流量）与控制面（做决策）分离」的形态，正是 u1-l1 讲过的项目定位的工程落地。

**(2) gRPC 双向流（bidirectional streaming）。**
ExtProc 的核心 RPC 叫 `Process`，它的签名是一个**双向流**：Envoy 和 SR 之间建立一条长连接的 gRPC stream，Envoy 持续 `Send` 一连串 `ProcessingRequest`（先发请求头、再发请求体……），SR 持续 `Recv` 并对每一条 `Send` 回一个 `ProcessingResponse`。一次 HTTP 请求，在这条 stream 上体现为「若干条请求消息 + 若干条响应消息」的来回，直到流结束。

**(3) 「状态机」这个词的含义。**
HTTP 是无状态的，但处理「一次请求」的过程是有状态的：你得先记住请求头里的 request-id，再记住请求体里解析出的 model，再记住决策选中的模型……这些「中途记下来的东西」就是状态。`OpenAIRouter.Process` 把一次请求的处理过程组织成一个会随着消息推进而不断累积状态的循环，所以我们称它为「请求处理状态机」。承载这些状态的载体，就是本讲要重点讲的 `RequestContext`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/semantic-router/pkg/extproc/server.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go) | `Server` 结构体与 gRPC 服务的生命周期（`NewServer`/`Start`/`Stop`/`GetRouter`），以及热重载入口。 |
| [src/semantic-router/pkg/extproc/router.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router.go) | `OpenAIRouter` 核心结构体及其持有的全部状态字段（配置、分类器、缓存、工具库、选择器……）。 |
| [src/semantic-router/pkg/extproc/processor_core.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_core.go) | `Process` 主循环：收消息、按消息类型分发到四个阶段处理器。 |
| 辅助文件（顺带引用） | `request_context.go`（请求状态容器）、`utils.go`（`sendResponse`）、`router_build.go`（路由器构建）、`server_config_watch.go`（热重载监听）、`cmd/runtime_bootstrap.go`（启动编排）、`deploy/local/envoy.yaml`（Envoy 侧配置）。 |

---

## 4. 核心概念与源码讲解

### 4.1 ExtProc Server：gRPC 服务的生命周期

#### 4.1.1 概念说明

`Server` 是 SR 暴露给 Envoy 的 gRPC 服务进程级外壳。它承担三件事：

1. **持有一份当前生效的路由器**（`OpenAIRouter`），并把它注册成 Envoy ExtProc 协议要求的服务实现。
2. **监听 TCP 端口**（默认 `50051`），把 gRPC server 跑起来，并等待 `SIGINT`/`SIGTERM` 优雅退出。
3. **在配置变化时热重载**：文件变化或 K8s ConfigMap 更新时，构建一份新的路由器，原子替换掉旧的，从而不重启进程就能让新配置生效。

需要特别理解的一点是「委托（delegation）」。直接实现 ExtProc 协议的是 `OpenAIRouter`，但 `Server` 并不把 `OpenAIRouter` 直接交给 gRPC 框架，而是包了一层 `RouterService`。这一层的目的就是「热重载时不打断在途请求」——后面 4.2 会详细讲。

#### 4.1.2 核心流程

`Server` 的生命周期可以画成下面这条主线：

```
cmd/runtime_bootstrap.go
        │
        ▼
extproc.NewServer(configPath, port, secure, certPath, runtimeRegistry)
   │  1. newOpenAIRouterForServer()  → 加载配置、构建 OpenAIRouter
   │  2. attachRuntimeRegistry()     → 把 Registry 挂到路由器
   │  3. publishRouterState()        → 把路由器状态发布进 Registry（供 API Server 读）
   │  4. NewRouterService(router)    → 用 RouterService 包一层
   ▼
返回 *Server（此时还没监听端口）
        │
        ▼  （bootstrap 在此处做 warmup、起 API server、标记 ready……）
server.Start()
   │  1. net.Listen("tcp", :port)
   │  2. 按 secure 装配 TLS / 自签证书
   │  3. grpc.NewServer(...) + RegisterExternalProcessorServer(...)
   │  4. go server.Serve(lis)         → 真正开始接 gRPC 连接
   │  5. go watchConfigAndReload(ctx) → 后台监听配置变化
   │  6. select { serverErr | SIGINT/SIGTERM }
   ▼
server.Stop()  → GraceacefulStop()
```

两个关键设计点：

- **`Start()` 是阻塞的**：它用 `select` 同时等待「gRPC serve 出错」和「收到中断信号」两件事，任一发生才返回。这意味着主 goroutine 会停在这里，进程不会退出，直到被显式停止。
- **配置热重载是「先校验、构建、热身，再原子替换」**：新路由器构建失败时，旧路由器原封不动继续服务；只有新路由器热身通过后，才 `Swap` 替换并 `Close` 旧的。

#### 4.1.3 源码精读

先看 `Server` 结构体本身——它只有 7 个字段，非常薄：

[server.go:69-77](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go#L69-L77) 定义了 `Server`：保存配置路径、委托服务 `service`、底层 `grpc.Server`、端口、TLS 开关与证书路径，以及运行时依赖容器 `runtime`（即 u4-l2 讲的 `routerruntime.Registry`）。

`NewServer` 负责把一个路由器实例「装配」成一个可用的服务：

[server.go:80-103](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go#L80-L103)：依次调用 `newOpenAIRouterForServer`（加载配置并构建路由器）、`attachRuntimeRegistry`（把 Registry 指针挂到路由器上）、`publishRouterState`（把 config / 分类服务 / 记忆库 / 选择器写进 Registry，让 API Server 能读到），最后用 `NewRouterService(router)` 包一层委托服务。

`GetRouter` 是 bootstrap 用来拿「当前路由器」做热身（warmup）的入口：

[server.go:105-108](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go#L105-L108)：直接转调 `service.GetRouter()`，而后者读的是那把原子指针的当前值（见 4.2）。

`Start` 是真正「把服务跑起来」的地方，分四段：

- [server.go:111-116](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go#L111-L116)：`net.Listen` 占用端口。
- [server.go:120-150](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go#L120-L150)：若开启 `secure`，则从 `certPath` 加载 `tls.crt`/`tls.key`，或当场生成自签证书，并装配成 gRPC 的 TLS 凭据。
- [server.go:152-163](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go#L152-L163)：根据配置计算 gRPC 最大消息体大小，设置 `MaxRecvMsgSize`/`MaxSendMsgSize`，然后 `grpc.NewServer` 并把 `s.service` 注册为 ExtProc 协议的实现。
- [server.go:166-202](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go#L166-L202)：用 goroutine 跑 `server.Serve(lis)`，再起一个后台 goroutine 跑 `watchConfigAndReload`，最后 `select` 等待 serve 出错或中断信号。

`Stop` 优雅关闭：

[server.go:205-212](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go#L205-L212)：调用 `GracefulStop()`，让在途的 gRPC 流自然收尾后再退出。

热重载的核心入口是 `reloadRouterFromConfig`，它体现了「先校验后替换」的不变式：

[server.go:248-288](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go#L248-L288)：依次做模型下载预检（仅 file 来源）、准备运行时依赖、构建新路由器、挂 Registry、warmup；任何一步失败都直接 `return err`，旧路由器不受影响。只有全部成功，才 `s.service.Swap(newRouter)` 原子替换，再 `oldRouter.Close()` 释放旧资源，最后 `publishRouterState` 把新状态刷进 Registry。注意第 277-279 行特意排除了 K8s 来源的二次 `config.Replace`，避免重复入队通知。

而触发这条重载路径的有两个源头，由 `watchConfigAndReload` 分流：

[server_config_watch.go:31-41](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server_config_watch.go#L31-L41)：若配置来源是 Kubernetes，则走 `watchKubernetesConfigUpdates`（订阅 `config.SubscribeConfigUpdates` channel）；否则走文件监听 `watchFileConfigAndReload`（用 fsnotify 监听配置文件所在目录，带 250ms 去抖和 300ms settle 延迟，避免编辑器多次保存触发抖动重载）。

最后，这套生命周期是被 `cmd/runtime_bootstrap.go` 编排起来的：

[runtime_bootstrap.go:426-437](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/runtime_bootstrap.go#L426-L437) `newExtProcServerOrFatal` 调 `NewServer`，失败直接 fatal；[runtime_bootstrap.go:439-462](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/runtime_bootstrap.go#L439-L462) `warmupRouterRuntime` 用 `GetRouter()` 拿到路由器做工具库 / 知识库预热；[runtime_bootstrap.go:507-511](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/runtime_bootstrap.go#L507-L511) `startExtProcServerOrFatal` 才真正调 `server.Start()`。端口默认值在 [runtime_bootstrap.go:45](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/runtime_bootstrap.go#L45)：`flag.Int("port", 50051, ...)`。

#### 4.1.4 代码实践

**实践目标**：把 `Server` 的生命周期与启动编排对上号，理解「创建 → 热身 → 起服」三步为什么分开。

**操作步骤**：

1. 打开 `src/semantic-router/cmd/runtime_bootstrap.go`，搜索 `newExtProcServerOrFatal`、`warmupRouterRuntime`、`startExtProcServerOrFatal` 三个函数的调用点，记录它们在启动序列里的相对顺序。
2. 打开 `src/semantic-router/pkg/extproc/server.go`，对照 `NewServer`（L80）与 `Start`（L111），确认：`NewServer` 返回时端口是否已在监听？`grpc.Server` 是否已经创建？
3. 在 `Start` 里找到 `go s.watchConfigAndReload(ctx)`（L178），再打开 `server_config_watch.go` 的 `watchConfigAndReload`，确认它根据 `usesKubernetesConfigSource()` 二选一。

**需要观察的现象 / 预期结果**：

- `NewServer` 只构建路由器与委托服务，**不**监听端口、**不**创建 `grpc.Server`——这两件事都在 `Start` 里发生。这样设计使得 bootstrap 可以在「服务对外可见」之前，先用 `GetRouter()` 把工具库 / 知识库预热好。
- 热重载失败（例如新配置引用了不存在的信号）时，进程不会崩、端口不会断，旧路由器继续服务——日志里能看到 `config_reload_failed` 事件。

> 本实践为「源码阅读型」，不要求运行；若想实跑，可参考 u1-l3 的 `make` 目标本地起栈后，修改 `config/config.yaml` 触发一次文件重载并观察日志。

#### 4.1.5 小练习与答案

**练习 1**：`Start()` 里的 `select` 同时等待哪两类事件？为什么不让 `Serve` 直接阻塞主 goroutine？

> **答案**：等待「gRPC serve 返回错误」和「`SIGINT`/`SIGTERM` 信号」两类事件。直接阻塞在 `Serve` 上就收不到中断信号，无法做 `GracefulStop` 优雅退出；用 `select` 才能在收到信号时主动调 `Stop()`。

**练习 2**：热重载时，如果新路由器的 warmup 失败，旧路由器会被替换吗？

> **答案**：不会。[server.go:269-272](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go#L269-L272) 在 warmup 失败时会 `newRouter.Close()` 后直接返回错误，`Swap`（L282）根本不会执行，旧路由器继续服务。

---

### 4.2 RouterService 与 OpenAIRouter：请求处理状态机

#### 4.2.1 概念说明

这一节回答两个问题：**(a) 为什么要在 `OpenAIRouter` 外面包一层 `RouterService`？(b) `OpenAIRouter` 自己持有什么？**

**(a) 委托 + 原子指针 = 不停机换路由。**
gRPC 框架在注册服务时，拿到的是一个固定的服务对象引用。如果热重载时要换掉路由器，又不能让在途的 gRPC 流感知到中断，最干净的办法是：gRPC 框架持有的对象永远不变（`RouterService`），它内部用一把 `atomic.Pointer[OpenAIRouter]` 指向「当前生效的路由器」。每来一个请求，`RouterService.Process` 都先 `Load()` 当前指针再委托，于是「换路由器」就退化成一次原子的 `Store`，在途请求用的是旧指针、新请求立刻用新指针。

**(b) `OpenAIRouter` 是「路由器级」的共享状态，不是「请求级」状态。**
注意区分两类状态：

- **路由器级状态**（写在 `OpenAIRouter` 字段里）：配置、分类器、缓存、工具库、模型选择器、记忆库……这些是所有请求**共享**的、相对稳定的「基础设施」。本节讲的就是这些。
- **请求级状态**（写在 `RequestContext` 里）：某一次请求的 request-id、解析出的 model、选中的决策、流式累积的内容……这些是每个请求**独占**的、随消息推进而增长的「账本」。4.3 节会讲它怎么在 `Process` 循环里累积。

`OpenAIRouter` 既是 ExtProc 协议的实现者（持有 `Process` 方法），又是这一堆基础设施的容器。

#### 4.2.2 核心流程

`OpenAIRouter.Process` 是整个数据面的入口，它的形态是一个「收一条、处理一条、回一条」的循环：

```
Envoy 建立一条 gRPC stream（对应一次 HTTP 请求的生命周期）
        │
        ▼
Process(stream):
   创建空的 RequestContext（请求级账本）
   ┌─── for { ──────────────────────────────────┐
   │  req = stream.Recv()    // 阻塞收下一条消息  │
   │  switch req.Request.(type):                 │
   │     RequestHeaders  → processRequestHeaders │
   │     RequestBody     → processRequestBody    │
   │     ResponseHeaders → processResponseHeaders│
   │     ResponseBody    → processResponseBody   │
   │  每个 handler 产出一个 ProcessingResponse，  │
   │  通过 sendResponse → stream.Send() 回给 Envoy│
   └─────────────────────────────────────────────┘
   直到 stream.Recv() 返回 EOF / cancel / 超时 → 退出
```

两个要点：

- **一个 HTTP 请求 = 一条 gRPC stream = 一次 `Process` 调用**。`RequestContext` 在这次调用内累积，调用结束即丢弃。
- **路由器字段在请求之间是共享且只读为主的**。热重载换的是整个 `OpenAIRouter`，而不是在请求中途改它的字段——这就是为什么「换路由」安全。

#### 4.2.3 源码精读

先看委托层 `RouterService`，它是热重载的关键：

[server.go:214-237](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go#L214-L237)：`RouterService` 内部只有一把 `atomic.Pointer[OpenAIRouter]`。`NewRouterService` 做 `Store`，`Swap` 做 `Store`（热重载时调），`GetRouter` 做 `Load`，而 `Process` 则是「先 `Load` 当前路由器，再把 stream 委托给它」。正因为 `Process` 每次都现读指针，热重载的 `Swap` 才能对「下一个进来的请求」立即生效。

接着看 `OpenAIRouter` 持有哪些「路由器级」基础设施字段：

[router.go:29-71](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router.go#L29-L71)：可以把这些字段分成几组来记——

| 分组 | 代表字段 | 含义 |
| --- | --- | --- |
| 配置 | `Config`、`CategoryDescriptions` | 当前生效的 `RouterConfig` 与分类目录描述 |
| 分类 | `Classifier`、`RecipeClassifiers`、`ClassificationService` | 默认配方分类器、按配方隔离的分类图、对外分类服务（u8） |
| 缓存 | `Cache` | 语义缓存后端（u9） |
| 工具 | `ToolsDatabase`、`ToolsRegistry`、`toolSelectionDBByPath` | 工具库及其检索策略注册表（u10） |
| 选择 | `ModelSelector`、`RecipeModelSelectors`、`LookupTable` | 模型选择算法注册表与查找表（u6） |
| 记忆 | `MemoryStore`、`MemoryExtractor` | 记忆库与记忆抽取器（u9） |
| 重放 | `ReplayRecorder`、`ReplayRecorders` | 路由重放审计记录器 |
| 治理 | `CredentialResolver`、`RateLimiter` | 每用户凭证解析与限流（u11） |
| 运行时桥 | `RuntimeRegistry` | 指回 u4-l2 的依赖容器，避免请求路径走包级全局变量 |
| 学习 | `routerLearningRuntime`、`lookupTableCancel` | Router Learning 运行时与查找表后台任务取消句柄 |

第 86 行的编译期断言确保 `OpenAIRouter` 真的实现了 Envoy ExtProc 协议接口：

[router.go:85-86](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router.go#L85-L86)：`var _ ext_proc.ExternalProcessorServer = (*OpenAIRouter)(nil)`——这行不产生运行时代码，但若 `OpenAIRouter` 缺少了 `Process` 方法，编译就会失败，是一种接口契约的自检。

`Close` 负责释放路由器持有的后台资源：

[router.go:75-83](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router.go#L75-L83)：主要是取消查找表的自动保存与周期重填 goroutine（`lookupTableCancel`）。热重载时 `reloadRouterFromConfig` 替换后会调它，防止旧路由器的后台任务泄漏。

现在看数据面入口 `Process`：

[processor_core.go:70-98](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_core.go#L70-L98)：先 `defer recover()` 兜住 panic（包括 CGO 推理可能抛出的 OOM panic），防止单个坏请求拖垮整个 gRPC server；再创建一个空的 `RequestContext`（只初始化了 `Headers` map 与 `TraceContext`）；然后进入 `for` 循环，`stream.Recv()` 收消息，交给 `handleProcessRequest` 处理。

注意 `RequestContext` 一开始在 [processor_core.go:83-86](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_core.go#L83-L86) 几乎是空的，它会在四个阶段处理器里被逐步填满——例如请求头阶段填 `RequestID`，请求体阶段填 `RequestModel` / `RequestQuery`，决策阶段填 `VSRSelectedModel` 等。它有上百个字段（见 [request_context.go:50-310](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/request_context.go#L50-L310)），是「一次请求的全部账本」，本讲只需记住它的角色，具体字段会在 u5/u8 的具体阶段里逐个用到。

#### 4.2.4 代码实践

**实践目标**：说明 `Process(stream)` 是如何被 Envoy 调用的，并描述 `OpenAIRouter` 持有的核心状态字段。

**操作步骤**：

1. 在 `server.go` 找到 `RouterService.Process`（L234），确认它只是 `rs.current.Load()` 后转调 `r.Process(stream)`。这就是「Envoy 实际调用的入口」——Envoy 通过 gRPC 框架调到 `RouterService.Process`，而非直接调 `OpenAIRouter.Process`。
2. 在 `processor_core.go` 读 `Process`（L70），确认它的三段结构：`defer recover()` → 建 `RequestContext` → `for { Recv; handleProcessRequest }`。
3. 在 `router.go` 读 `OpenAIRouter` 结构体（L29），把字段按「配置 / 分类 / 缓存 / 工具 / 选择 / 记忆 / 重放 / 治理 / 运行时桥」九组归类。

**需要观察的现象 / 预期结果**：

- Envoy 的一次 HTTP 请求 → 一条 gRPC stream → 一次 `RouterService.Process` 调用 → 内部委托给「当时 `Load()` 到的那把 `OpenAIRouter`」。这就是「`Process(stream)` 如何被 Envoy 调用」的完整链路。
- `OpenAIRouter` 持有的是**路由器级共享基础设施**，而不是请求级数据；请求级数据在 `RequestContext` 上。混淆这两者是初学者最常见的误解。

> 待本地验证：若你已按 u1-l3 起了本地栈，可向 Envoy 的 `8801` 端口发一个 `/v1/chat/completions` 请求，在 router 日志里能看到对应这次请求的 `request_headers_captured` 等事件——它们就是 `Process` 循环里逐阶段打出的。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `RouterService` 用 `atomic.Pointer` 而不是直接持有一个 `*OpenAIRouter` 字段？

> **答案**：热重载需要在「不锁住请求路径」的前提下替换路由器。`atomic.Pointer` 的 `Load`/`Store` 是无锁原子操作，每个请求 `Process` 开头 `Load` 一次即可；若用普通字段加互斥锁，每个请求都要竞争锁，且热重载期间可能阻塞在途请求。

**练习 2**：`OpenAIRouter` 的 `Config` 字段在一次请求处理过程中可能被改写吗？

> **答案**：不会在请求中途被改写。配置变更走的是「整体换一个新 `OpenAIRouter`」的热重载路径，老请求持有的还是旧路由器、旧 `Config`；新请求 `Load` 到新路由器、新 `Config`。请求内部代码把 `Config` 当作只读来用。

---

### 4.3 gRPC 交互模型：headers → body → response 的双向流

#### 4.3.1 概念说明

Envoy ExtProc 的交互模型由两份配置共同决定：

1. **Envoy 侧的 `processing_mode`**：告诉 Envoy「要不要把请求头/请求体/响应头/响应体发给 SR，以及怎么发（一次性发还是分块流式发）」。
2. **SR 侧的 `Process` 循环**：根据收到的消息类型，分发到对应的阶段处理器，并回一个 `ProcessingResponse`。

ExtProc 协议定义了四种 `ProcessingRequest` 消息体类型，正好对应 HTTP 的四个观察点：

| 消息类型 | 何时到达 | SR 的处理器 |
| --- | --- | --- |
| `RequestHeaders` | Envoy 收到客户端请求头后 | `processRequestHeaders` |
| `RequestBody` | Envoy 收集完（或分块收到）请求体后 | `processRequestBody` |
| `ResponseHeaders` | 上游后端返回响应头后 | `processResponseHeaders` |
| `ResponseBody` | 上游返回（或分块）响应体后 | `processResponseBody` |

而 SR 回给 Envoy 的 `ProcessingResponse` 主要有两种「形态」：

- **`CONTINUE`**：放行，让 Envoy 继续把流量往后送（可能附带头部改写）。
- **`ImmediateResponse`**：直接回包——SR 自己生成一个完整的 HTTP 响应（如缓存命中、限流拒绝、错误），Envoy 不再转发到上游。`createJSONResponseWithBody`（[router.go:95-121](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router.go#L95-L121)）就是构造这种立即响应的典型 helper。

#### 4.3.2 核心流程

把消息类型分发与响应回送连起来，就是 `Process` 循环里的这条路径：

```
stream.Recv() → req
   │
   ▼
handleProcessRequest(stream, req, ctx):
   switch req.Request.(type):
      RequestHeaders  → handleRequestHeaders  → 产出 response → sendResponse
      RequestBody     → handleRequestBodyDispatch → handleRequestBody → sendResponse
      ResponseHeaders → handleResponseHeaders → sendResponse
      ResponseBody    → handleResponseBody    → sendResponse
   每个 sendResponse 内部：stream.Send(response)
```

其中 `sendResponse` 有一个保底设计：若 handler 返回了 `nil` 响应，它会自动补发一个 `CONTINUE` 的 Body 响应，避免把 nil 送给 Envoy 触发空指针。

错误处理上，`Process` 把 `Recv` 的错误分成几类：正常 `EOF`（流自然结束）、`Canceled`/`DeadlineExceeded`（客户端取消或超时）、以及真错误。前两类被视为「优雅结束」返回 nil，并清理在途计数（`inflight.End`）。

#### 4.3.3 源码精读

消息分发就在 `handleProcessRequest` 的 `switch`：

[processor_core.go:161-182](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_core.go#L161-L182)：先从 `req.GetProtocolConfig()` 读取 Envoy 协商出的请求体模式（`FULL_DUPLEX_STREAMED` 等会写到 `ctx.FullDuplexRequestBody`），再按四种消息类型分发。未知类型走 `processUnknownRequest`，回一个 `CONTINUE`。

四个阶段处理器的结构高度一致——都是「调一个 `handleXxx` 拿到 response，再 `sendResponse`」：

- [processor_core.go:184-199](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_core.go#L184-L199)：`processRequestHeaders`，委托给 `handleRequestHeaders`（在 `processor_req_header.go`，负责捕获 request-id、识别客户端协议、检测流式期望、识别 `/v1/models` 与 router_replay 等特殊路径）。
- [processor_core.go:201-222](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_core.go#L201-L222)：`processRequestBody`，委托给 `handleRequestBodyDispatch`（这是 u5-l1 的入口；它会按 BUFFERED/STREAMED 模式分发，见下）。
- [processor_core.go:224-246](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_core.go#L224-L246)：`processResponseHeaders` 与 `processResponseBody`，分别委托给 `handleResponseHeaders` / `handleResponseBody`（u5-l3 的入口）。

请求体有一层额外的「模式分发」，因为它受 Envoy 的 `request_body_mode` 影响：

[processor_core.go:27-67](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_core.go#L27-L67)：`handleRequestBodyDispatch` 先看 `ctx.SkipProcessing`（`x-vsr-skip-processing` 全透传），再按是否已有 `StreamedBody`、是否开启 `StreamedBodyMode`、是否 `FullDuplexRequestBody`，决定走「分块累积」还是「经典整包」`handleRequestBody`。默认 BUFFERED 模式下，Envoy 把整个请求体一次性发来，直接走经典管线。

回包的统一出口是 `sendResponse`：

[utils.go:18-45](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/utils.go#L18-L45)：nil 响应自动补 `CONTINUE`；debug 模式下打印响应前会脱敏（去掉 `Authorization`/`x-api-key` 等凭证，防 CWE-532 日志泄漏）；最终 `stream.Send(response)`。脱敏那一段很值得注意——SR 会在请求阶段往上游注入 provider key（`set-header`），若把响应原样 `+v` 打印就会把 key 写进日志。

错误处理集中在 `handleProcessReceiveError`：

[processor_core.go:100-125](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_core.go#L100-L125)：先处理「流式响应中途断开」与「在途计数收尾」，再把 `io.EOF`、gRPC `Canceled`/`DeadlineExceeded`、`context.Canceled`/`DeadlineExceeded` 都视为优雅结束（返回 nil，不记为错误），其余才作为真错误上抛。

最后看 Envoy 侧是怎么「配」出这套交互的。[deploy/local/envoy.yaml:90-105](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/deploy/local/envoy.yaml#L90-L105) 配置了 `envoy.filters.http.ext_proc`：`grpc_service.cluster_name: extproc_service`，并设了四档 `processing_mode`——请求头/响应头 `SEND`、请求体/响应体 `BUFFERED`、trailer 全部 `SKIP`，`message_timeout: 300s`。配套的 cluster 定义在 [envoy.yaml:120-141](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/deploy/local/envoy.yaml#L120-L141)：`extproc_service` 指向 `127.0.0.1:50051`，正是 SR 默认监听的端口。这就把「Envoy 触发四个阶段回调」与「SR 的 `Process` 循环」精确对应上了。

#### 4.3.4 代码实践

**实践目标**：把 Envoy 的 `processing_mode` 配置与 SR 的四阶段处理器一一对应，理解一次请求在双向流上的来回过程。

**操作步骤**：

1. 打开 `deploy/local/envoy.yaml`，定位 `envoy.filters.http.ext_proc`（L90），记录四个 `*_mode` 的取值，并回答：为什么 trailer 是 `SKIP`？
2. 打开 `processor_core.go` 的 `handleProcessRequest`（L161），把 switch 的四个 case 与 Envoy 的四个 `processing_mode` 对应起来。
3. 打开 `utils.go` 的 `sendResponse`（L18），找到「nil 响应自动补 CONTINUE」与「凭证脱敏」两段逻辑，思考它们各自防的是什么问题。

**需要观察的现象 / 预期结果**：

- `request_header_mode: SEND` + `request_body_mode: BUFFERED` → 一次 `/v1/chat/completions` 请求，SR 的 `Process` 循环会先后收到 1 条 `RequestHeaders`、1 条 `RequestBody`（整包），决策后改写头部并回 `CONTINUE`；上游返回后再先后收到 1 条 `ResponseHeaders`、若干条 `ResponseBody`。
- 若 SR 决定直接回包（如缓存命中），会在请求体阶段回一个 `ImmediateResponse`，Envoy 收到后**不再**转发到上游，本次 stream 提前结束。
- trailer 设为 `SKIP` 是因为 SR 不需要处理 HTTP trailer，跳过可以减少一次不必要的来回。

> 本实践为「源码阅读型 + 配置阅读型」。可运行的进阶验证见 u5。

#### 4.3.5 小练习与答案

**练习 1**：`CONTINUE` 与 `ImmediateResponse` 这两种响应分别意味着什么？各举一个触发场景。

> **答案**：`CONTINUE` 表示「放行」，Envoy 继续把请求转发到上游后端（请求头/请求体阶段最常见，可能附带头部改写如 `accept-encoding: identity`）；`ImmediateResponse` 表示「SR 自己直接回包」，Envoy 不再转发，典型触发是语义缓存命中、限流拒绝（429）、请求校验失败（400）等。

**练习 2**：`sendResponse` 为什么要在 debug 打印前做脱敏？

> **答案**：请求阶段 SR 会把上游 provider 的 API key 作为 `set-header` 注入响应里。若按 `+v` 原样打印响应（CWE-532），key 就会写进日志。脱敏保证了即使开 debug 日志，凭证也不会泄漏。

**练习 3**：把 `processing_mode` 的 `request_body_mode` 从 `BUFFERED` 改成 `STREAMED`，`handleRequestBodyDispatch` 的行为会有什么不同？

> **答案**：STREAMED 模式下 Envoy 会把请求体分成多条 `RequestBody` 消息（每条带 `end_of_stream` 标记）。`handleRequestBodyDispatch`（[processor_core.go:55-63](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_core.go#L55-L63)）会创建一个 `StreamedBodyHandler` 累积分块，直到 `end_of_stream` 才进入完整管线；而 BUFFERED 模式下整包一次到达，直接走 `handleRequestBody`。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「一次请求的全链路画像」任务：

1. **从启动侧入手**：在 `cmd/runtime_bootstrap.go` 里画出「`NewServer` → `warmupRouterRuntime`（用 `GetRouter`）→ `startAPIServerIfEnabled` → `startExtProcServerOrFatal`（`Start`）」的顺序，标注每一步用的是 `Server` 还是 `OpenAIRouter` 的能力，以及 `RouterService` 在哪一步被创建。
2. **追一次请求**：假设一个 `/v1/chat/completions` 请求命中了语义缓存。从 Envoy 的 `ext_proc` filter（`deploy/local/envoy.yaml`）出发，依次写出 SR 的 `Process` → `handleProcessRequest` switch → 具体阶段处理器 → `sendResponse` 这条链路上会经过的函数名，并标注在哪一步会因为缓存命中而回 `ImmediateResponse`、本次 stream 提前结束。
3. **画一张状态归属图**：用两张表区分「路由器级状态」（`OpenAIRouter` 字段）与「请求级状态」（`RequestContext` 字段），各列 5 个代表字段，并说明热重载换的是哪一张表、在途请求持有的是哪一张表。

完成后再回到本讲的「学习目标」三条，自检是否都能用自己的话讲清楚。

## 6. 本讲小结

- `Server` 是 SR 暴露给 Envoy 的 gRPC 服务外壳，生命周期为 `NewServer`（构建路由器与委托服务，不监听）→ `Start`（监听端口、装配 TLS、起 `Serve` 与配置监听两个 goroutine、`select` 等退出信号）→ `Stop`（`GracefulStop`）。
- 热重载由 `reloadRouterFromConfig` 实现，遵循「先校验/构建/热身，再原子替换」：任何一步失败都保留旧路由器；文件来源走 fsnotify 监听，K8s 来源走 config 订阅 channel。
- `RouterService` 是一层用 `atomic.Pointer[OpenAIRouter]` 实现的委托层，使得「不停机换路由」退化为一次原子 `Store`，每个请求 `Process` 开头 `Load` 当前指针。
- `OpenAIRouter` 既是 ExtProc 协议实现者（持有 `Process`），又是路由器级共享基础设施的容器（配置/分类/缓存/工具/选择/记忆/重放/治理/运行时桥），请求级状态则放在 `RequestContext` 上——两者不可混淆。
- Envoy 与 SR 通过 ExtProc 双向流交互：Envoy 按 `processing_mode` 发四种 `ProcessingRequest`（请求头/请求体/响应头/响应体），SR 的 `Process` 循环 `Recv` 后用 `switch` 分发到四个阶段处理器，再经 `sendResponse` 回 `CONTINUE`（放行）或 `ImmediateResponse`（直接回包）。

## 7. 下一步学习建议

- **u5-l1 请求体处理与入口解析**：本讲只讲到「请求体消息到了 `handleRequestBodyDispatch`」，u5-l1 会深入 `handleRequestBody` 如何解析 OpenAI 请求体、如何解析 entrypoint/recipe。
- **u5-l2 决策求值管线**：承接本讲的 `RequestContext`，讲 `performDecisionEvaluation` 如何准备信号、求值信号、调用决策引擎。
- **u11-l1 API Server 管理 API**：本讲的 `publishRouterState` 把路由器状态写进 `Registry` 供 API Server 读；u11-l1 讲 API Server 如何消费这些共享依赖。
- 若想更理解「为什么不直接实现 ExtProc 接口而要委托」，可回头读 u4-l2 关于 `routerruntime.Registry` 作为数据面/控制面共享容器的论述。
