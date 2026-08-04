# Envoy ExtProc 网关插件入口

## 1. 本讲目标

AIBrix 的 LLM 网关（数据平面）不是自己写一个 HTTP 服务器去接收用户请求，而是把自己挂载到 Envoy 代理上，通过 **Envoy External Processing（ExtProc）** 协议参与每个请求的处理。本讲是「LLM 网关核心」单元的入口，目标是让你：

1. 理解 **Envoy ExtProc 协议** 是什么，AIBrix 网关为什么用这种方式接入请求。
2. 掌握网关二进制 `cmd/plugins` 的 **gRPC 服务启动流程**——从 `main.go` 到注册成 ExtProc server。
3. 掌握 `gateway.go` 的 **请求处理阶段**——一个请求会触发哪些 ExtProc 回调，分别对应哪些 Go 方法。
4. 准确指出 **路由决策发生在哪个阶段**，以及它与「不设路由算法」分支的区别。

本讲只打通「请求进来 → 路由决策 → 转发」这条主链路的骨架，具体的路由算法（评分、归一化、多策略）留给 u7-l3、u7-l4 与第八单元。

## 2. 前置知识

阅读本讲前，建议你已经了解（可回顾 u6-l1）：

- **控制平面 vs 数据平面**：控制平面（控制器）在后台管理模型部署；数据平面（网关）在前台接待用户请求。本讲完全聚焦数据平面。
- **`pkg/cache` 中央缓存**：网关通过 `cache.Cache` 接口拿到「模型有哪些 Pod、各 Pod 实时指标」，但本讲的入口逻辑不深入缓存内部。
- **gRPC 与 Protobuf 基础**：ExtProc 是一个 gRPC 双向流（bidi-streaming）服务，消息体由 `envoy.service.ext_proc.v3` 这个 protobuf 包定义。

下面两个概念是本讲的核心，先建立直觉：

### 2.1 什么是 Envoy External Processing（ExtProc）

Envoy 是一个高性能的可编程代理。很多时候我们不想把业务逻辑（比如「这个请求该路由到哪个 GPU Pod」）写成 Envoy 的 C++ 过滤器，而是想用更顺手的语言（这里是 Go）在外部进程里实现。**ExtProc** 就是 Envoy 为此提供的官方机制：

- Envoy 启动时配置一个「External Processor」过滤器，指向一个外部 gRPC 服务地址（AIBrix 里默认是 `:50052`）。
- 对每一个经过 Envoy 的 HTTP 请求，Envoy 会向这个 gRPC 服务**打开一条独占的双向流**。
- Envoy 按请求的生命周期，**分阶段**地把数据以 `ProcessingRequest` 消息推给外部服务：先 `RequestHeaders`（请求头），再 `RequestBody`（请求体），等上游返回后又推 `ResponseHeaders`（响应头）、`ResponseBody`（响应体，流式时会有多块）。
- 外部服务对每条 `ProcessingRequest` 回一个 `ProcessingResponse`，里面可以携带「修改请求头」「修改请求体」「直接拒绝（ImmediateResponse）」等指令。

一句话总结：**ExtProc 把 Envoy 变成一个「传话筒」，真正的请求处理大脑放在外部的 Go 进程里。** 这样 AIBrix 的路由、限流、认证逻辑都能用 Go 写，并且能独立于 Envoy 升级。

### 2.2 关键术语速查

| 术语 | 含义 |
| --- | --- |
| ExtProc | Envoy External Processing 协议，AIBrix 网关接入请求的方式 |
| `ProcessingRequest` | Envoy→网关 的消息，按阶段分为 RequestHeaders/RequestBody/ResponseHeaders/ResponseBody |
| `ProcessingResponse` | 网关→Envoy 的回复，可携带 HeaderMutation / BodyMutation / ImmediateResponse |
| `ImmediateResponse` | 网关让 Envoy「短路」直接返回响应（如认证失败、无可用 Pod），不转发到上游 |
| `RoutingContext` | 网关内部贯穿一次请求的路由上下文（模型、消息、算法、目标 Pod 等） |
| `target-pod` 头 | 网关在请求体阶段注入的响应头，告诉 Envoy 把请求转给哪个具体 Pod IP |

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [cmd/plugins/main.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/plugins/main.go) | 网关二进制入口：解析参数、初始化缓存与客户端、启动 gRPC 与 HTTP 服务、注册 ExtProc server |
| [pkg/plugins/gateway/gateway.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go) | 网关核心：`Process` 双向流主循环、按阶段分发的 `handleProcessingRequest`、路由集成点 `selectTargetPod` |
| [pkg/plugins/gateway/types.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/types.go) | 头名常量、请求路径、错误类型等约定，是网关各文件共享的「字典」 |
| [pkg/plugins/gateway/gateway_req_headers.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_headers.go) | 请求头阶段处理：认证、用户解析、构建 `RoutingContext` |
| [pkg/plugins/gateway/gateway_req_body.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_body.go) | 请求体阶段处理：**路由决策真正发生在这里** |
| [pkg/plugins/gateway/algorithms/router.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/algorithms/router.go) | 路由器管理器 `RouterManager`、`Select`、`RouterNotSet` 常量 |

## 4. 核心概念与源码讲解

本讲按「最小模块」拆成三块：①ExtProc gRPC 服务启动；②请求处理阶段；③路由调用集成点。

### 4.1 ExtProc gRPC 服务启动

#### 4.1.1 概念说明

「启动」要回答两个问题：

1. AIBrix 网关进程跑起来后，怎么把自己变成一个 ExtProc server？
2. 它除了 ExtProc 的 gRPC 服务，还需要哪些周边组件（缓存、HTTP 服务、状态同步）？

答案在 `cmd/plugins/main.go` 这一条「装配流水线」里。它是 u1-l5 讲过的「同一个网关二进制，靠 `--standalone` 开关分流」的实现现场：K8s 模式走 informer 动态发现 Pod，standalone 模式走静态 YAML 发现。

#### 4.1.2 核心流程

`main.go` 的启动是一条顺序流水线：

```text
解析 flag (grpc/http/standalone/endpoints)
        │
        ▼
解析 HTTP bind 地址（兼容已废弃的 --metrics-bind-address）
        │
        ▼
获取 Redis 客户端（K8s 模式必需；standalone 可选）
        │
        ▼
注册需要外部依赖的路由算法（power-of-two 依赖 Redis）
        │
        ▼
按 standalone / K8s 分支：构建 discovery.Provider 与各 K8s client
        │
        ▼
cache.InitWithOptions(...)  ← 初始化中央缓存（注入路由工厂、发现器）
        │
        ▼
gateway.NewServer(...)      ← 构造网关 Server 对象（含限流器、缓存句柄）
        │
        ▼
（可选）启动 statesync 跨副本状态同步
        │
        ▼
StartHTTPServer(httpAddr)   ← 暴露 /metrics 与 /v1/models
        │
        ▼
grpc.NewServer → RegisterExternalProcessorServer(s, gatewayServer)  ← 关键：注册成 ExtProc
        │
        ▼
注册 gRPC health 服务 → s.Serve(lis) 阻塞
        │
        ▼
收到 SIGINT/SIGTERM → 优雅关停（syncManager.Stop / gatewayServer.Shutdown / s.GracefulStop）
```

注意三个关键点（后面源码精读会对应）：

- **ExtProc 服务是 gRPC 双向流**，监听在 `--grpc-bind-address`（默认 `:50052`），与 HTTP 服务（`:8080`）是两个独立监听端口。
- **网关把路由算法工厂注入到中央缓存**（`cache.InitWithOptions` 的 `ModelRouterProvider`），这样缓存与路由共享同一套 Pod 视图。
- **优雅关停**专门处理了 ExtProc 流的「粘性」问题：Envoy 会无限期保持空闲流打开，必须主动打断 `Recv`，否则滚动升级时旧 Pod 永远关不掉。

#### 4.1.3 源码精读

**(1) 入口参数与 Redis 校验**

[cmd/plugins/main.go:64-73](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/plugins/main.go#L64-L73) 定义了四个核心 flag：gRPC 地址、HTTP 地址、standalone 开关、endpoints 配置文件。

```go
flag.StringVar(&grpcAddr, "grpc-bind-address", ":50052", "The address the gRPC server binds to.")
flag.StringVar(&httpAddr, "http-bind-address", "", "The address the HTTP server binds to (metrics, /v1/models).")
flag.BoolVar(&standalone, "standalone", false, "Run in standalone mode without Kubernetes.")
```

[cmd/plugins/main.go:90-103](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/plugins/main.go#L90-L103) 处理 Redis：K8s 模式下拿不到 Redis 客户端就直接 `Fatal`，因为分布式限流与用户认证都依赖它；standalone 模式允许无 Redis（限流与认证被禁用并打 warning）。这正是 u1-l5 提到的「standalone 下 Redis 可选、K8s 下必需」的代码依据。

**(2) 注册带依赖的路由算法**

[cmd/plugins/main.go:105-108](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/plugins/main.go#L105-L108) 注册需要外部依赖的路由算法：

```go
// register additional routing algorithms that need dependences
if redisClient != nil {
    routing.RegisterPowerOfTwoRouter(redisClient)
}
```

大多数路由算法在 `routing.Init()`（见 4.3 节）里自注册，但 `power-of-two` 这种需要 Redis 协调的算法单独在这里注册——这体现了「无依赖算法集中初始化、有依赖算法按需注入」的设计。

**(3) 注入缓存与构造 Server**

[cmd/plugins/main.go:157-169](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/plugins/main.go#L157-L169) 初始化中央缓存并构造网关 Server：

```go
cache.InitWithOptions(config, stopCh, cache.InitOptions{
    EnableKVSync:        kvSyncEnabled && remoteTokenizerEnabled,
    RedisClient:         redisClient,
    ModelRouterProvider: routing.ModelRouterFactory,  // 路由工厂注入缓存
    DiscoveryProvider:   discoveryProvider,           // K8s informer 或 静态 YAML
})
...
gatewayServer := gateway.NewServer(redisClient, k8sClient, gatewayK8sClient)
```

注意 `ModelRouterProvider` 被传给了缓存——这意味着缓存层与网关层共用同一套路由工厂，避免两套不一致的路由实现。

**(4) 关键：注册成 ExtProc server**

[cmd/plugins/main.go:208-214](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/plugins/main.go#L208-L214) 是本模块最核心的一行：

```go
s := grpc.NewServer(opts...)

extProcPb.RegisterExternalProcessorServer(s, gatewayServer)

healthCheck := health.NewServer()
healthPb.RegisterHealthServer(s, healthCheck)
healthCheck.SetServingStatus("gateway-plugin", healthPb.HealthCheckResponse_SERVING)
```

`extProcPb.RegisterExternalProcessorServer` 来自 Envoy 的 go-control-plane 包，它把 `gatewayServer`（实现了 `Process` 方法的 `*gateway.Server`）注册为 `ExternalProcessor` 服务的处理器。Envoy 配置里的 ExtProc 过滤器指向这个 gRPC 端口后，所有匹配的 HTTP 请求都会被「转交」给这里的 `Process` 方法。同时还注册了 gRPC 健康检查服务（`gateway-plugin`），Envoy/部署系统可用它判断网关是否就绪。

**(5) 优雅关停与流粘性问题**

[cmd/plugins/main.go:226-243](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/plugins/main.go#L226-L243) 处理 SIGINT/SIGTERM：依次停 `syncManager`、调 `gatewayServer.Shutdown()`、`s.GracefulStop()`。`gatewayServer.Shutdown()` 会关闭一个内部 `shutdownCh`，这正是 4.2 节要讲的「打断空闲 `Recv`」的信号源。

> 💡 **为什么需要这么麻烦？** Envoy 出于复用连接的考虑，会**长时间保持 ExtProc 双向流不关闭**（即使两个请求之间的空闲期）。如果网关在 `Process` 里裸调 `srv.Recv()` 阻塞等待下一个请求，那么收到关停信号时这个 goroutine 永远醒不过来，`GracefulStop` 也就永远结束不了。这是 ExtProc 网关一个经典的工程坑，AIBrix 的解法见 4.2.3。

#### 4.1.4 代码实践

**实践目标**：在不运行网关的前提下，画出 `main.go` 启动时创建的两类监听端口及其用途。

**操作步骤**：

1. 阅读 [cmd/plugins/main.go:64-73](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/plugins/main.go#L64-L73)，找出 `grpcAddr` 与 `httpAddr` 两个地址。
2. 阅读 [cmd/plugins/main.go:208-214](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/plugins/main.go#L208-L214) 与 `StartHTTPServer`（[gateway.go:617-645](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L617-L645)）。
3. 填写下面这张表：

| 监听 | 默认地址 | 协议 | 谁会连它 | 用途 |
| --- | --- | --- | --- | --- |
| gRPC | `:50052` | gRPC bidi-stream | Envoy 的 ExtProc 过滤器 | 请求处理主链路 |
| HTTP | `?` | HTTP | Prometheus / 客户端 | `?` |

**需要观察的现象 / 预期结果**：

- HTTP 端口默认是 `:8080`（见 [main.go:80-82](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/plugins/main.go#L80-L82) 的回退逻辑）。
- HTTP 服务挂载了 `/metrics`（Prometheus 抓取）与 `/v1/models`（模型列表）两个 handler（见 `StartHTTPServer` 与 [gateway.go:647-676](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L647-L676)）。
- Envoy **只连 gRPC 端口**；HTTP 端口是给运维和（standalone 模式下）客户端用的。

> 待本地验证：在 standalone 模式跑起来后，`curl http://localhost:8080/v1/models` 应返回缓存里的模型列表。

#### 4.1.5 小练习与答案

**练习 1**：把 `--grpc-bind-address` 改成 `:60052` 后，还需要改哪里网关才能正常工作？

**参考答案**：需要同步修改 Envoy 配置（或 K8s 部署里 Envoy Gateway 的 ExtProc 后端地址），让 Envoy 的 External Processor 过滤器指向 `:60052`。网关自己改没用——是 Envoy 主动连网关，而不是反过来。

**练习 2**：为什么 `RegisterPowerOfTwoRouter` 要单独写在 `main.go`，而不和其它算法一起在 `routing.Init()` 里注册？

**参考答案**：因为 `power-of-two` 路由需要 Redis 客户端做跨副本协调，而 `Init()` 在 `NewServer` 内部调用、拿不到 Redis 句柄的构造时机。把「有依赖的算法」放到已经拿到 `redisClient` 的 `main.go` 里注册，是一种依赖注入的清晰拆分。

---

### 4.2 请求处理阶段

#### 4.2.1 概念说明

ExtProc 协议规定：Envoy 把一次 HTTP 请求的生命周期拆成若干阶段，**每个阶段发一条 `ProcessingRequest`**。AIBrix 网关需要为每个阶段写一段处理逻辑。核心问题是：

- 一条 gRPC 双向流 = 一个 HTTP 请求，网关怎么在这条流上「轮询」各阶段的消息？
- 每个阶段分别做了什么？尤其要分清**请求头阶段**（`HandleRequestHeaders`，做认证）与**请求体阶段**（`HandleRequestBody`，做路由）的职责边界。

#### 4.2.2 核心流程

一个非流式 chat 请求在网关内部的完整阶段流转：

```text
HTTP 请求到达 Envoy
   │  Envoy 打开一条 ExtProc 双向流，调用 gateway.Process(srv)
   ▼
┌─────────────────────────────────────────────────────────────┐
│  Process(): 为本次请求创建 processState，进入 processOnce 循环  │
└─────────────────────────────────────────────────────────────┘
   │
   │  ① RequestHeaders  → handleProcessingRequest 分发
   │     → HandleRequestHeaders: 认证、解析用户、checkLimits、建 RoutingContext
   │     回 ProcessingResponse（注入 x-went-into-req-headers、trace 头）
   ▼
   │  ② RequestBody     → handleProcessingRequest 分发
   │     → HandleRequestBody: 解析 model/message、【路由决策 selectTargetPod】
   │     回 ProcessingResponse（注入 target-pod / routing-strategy / model 头）
   ▼
   │  ===== Envoy 把请求转发给选中的上游 Pod =====
   ▼
   │  ③ ResponseHeaders → handleProcessingRequest 分发
   │     → HandleResponseHeaders: 判断上游是否出错（4xx/5xx）
   ▼
   │  ④ ResponseBody    → handleProcessingRequest 分发（流式时多次）
   │     → HandleResponseBody: 统计 token、更新指标、判断完成
   ▼
   st.completed = true → Process 主动 break 流，返回 nil
```

两个关键设计：

1. **一条流 = 一个请求**：`Process` 方法是 gRPC stream handler，Envoy 为每个 HTTP 请求开一条新流，所以网关用 `processState` 在流内保存该请求的全部状态（requestID、user、model、routerCtx、各种 trace span），流结束即丢弃。
2. **路由在请求体阶段，不在请求头阶段**：因为 OpenAI 兼容 API 的 `model` 字段在 JSON body 里，且 token 感知/前缀缓存路由需要请求体内容；请求头阶段只做「能提前做的」认证与上下文准备。

#### 4.2.3 源码精读

**(1) Server 与 processState：流内状态**

[gateway.go:74-92](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L74-L92) 的 `Server` 是进程级单例（一个网关进程只有一个），持有 redis、限流器、K8s client、cache 等共享依赖：

```go
type Server struct {
    redisClient         *redis.Client
    ratelimiter         ratelimiter.RateLimiter
    modelRateLimiter    ratelimiter.RateLimiter
    apiKeyAuth          *apiKeyAuthConfig
    client              kubernetes.Interface
    gatewayClient       gatewayapi.Interface
    cache               cache.Cache
    // ...
}
```

而 [gateway.go:94-114](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L94-L114) 的 `processState` 是**每请求**的，贯穿整条流：

```go
type processState struct {
    ctx           context.Context
    requestID     string
    user          utils.User
    model         string
    routerCtx     *types.RoutingContext   // 贯穿路由全过程
    stream        bool
    completed     bool                      // 标记请求处理完成
    rootSpan      trace.Span               // OpenTelemetry 追踪
    inferenceSpan trace.Span
    // ...
}
```

**注意区分**：`Server` 是「工厂」、跨请求复用；`processState` 是「每个请求的工作台」、用完即弃。理解这一点能避免后续读并发代码时混淆。

**(2) Process：双向流主循环**

[gateway.go:195-245](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L195-L245) 是实现 `ExternalProcessor_ProcessServer` 接口的方法，也是 Envoy 每开一条流就调一次的入口：

```go
func (s *Server) Process(srv extProcPb.ExternalProcessor_ProcessServer) error {
    rootSpan := trace.SpanFromContext(srv.Context())
    requestID := uuid.New().String()
    if rootSpan.SpanContext().HasTraceID() {
        requestID = rootSpan.SpanContext().TraceID().String()  // 优先用 traceID 作 requestID
    }
    st := &processState{ctx: srv.Context(), requestID: requestID, rootSpan: rootSpan}
    // ... 指标 + defer 清理 ...
    for {
        if err := s.processOnce(srv, st); err != nil {
            return err
        }
        if st.completed {   // 主动结束：让 Envoy 优雅发 0\r\n\r\n
            // ... 收尾指标、DoneRequestCount ...
            return nil
        }
    }
}
```

它做三件事：①生成 requestID（优先复用上游传入的 traceID，便于全链路追踪）；②用 `defer` 注册流结束时的清理（删缓冲、结束 span、归还 in-flight 计数）；③进入 `processOnce` 循环，直到 `st.completed` 主动退出。这种「主动结束」的设计（见 [gateway.go:229-243](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L229-L243)）是为了让 Envoy 能优雅关闭流并发送 HTTP 的结束帧。

**(3) processOnce：可中断的消息接收**

[gateway.go:247-290](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L247-L290) 是单次「收一条 → 处理 → 回一条」的循环体，它解决了 4.1.3 提到的流粘性问题：

```go
func (s *Server) processOnce(srv extProcPb.ExternalProcessor_ProcessServer, st *processState) error {
    if err := s.preRecvCheck(st); err != nil { return err }   // 关停/取消预检
    // 在 goroutine 里 Recv，这样关停信号能打断阻塞
    ch := make(chan recvResult, 1)
    go func() { req, err := srv.Recv(); ch <- recvResult{req, err} }()
    var req *extProcPb.ProcessingRequest
    select {
    case r := <-ch:
        // ... 处理收到 / 错误 ...
        req = r.req
    case <-s.shutdownCh:   // 关停信号打断空闲 Recv
        // ... 记失败指标、DoneRequestCount ...
        return status.Error(codes.Unavailable, "server shutdown in progress")
    }
    resp, err := s.handleProcessingRequest(st, req)
    // ...
    return s.sendProcessingResponse(srv, st, resp)
}
```

关键在 `select`：把 `srv.Recv()` 放进 goroutine，主流程同时监听 `s.shutdownCh`。一旦关停，`Shutdown()` 关闭该 channel，`select` 立即走 `shutdownCh` 分支返回错误，阻塞的 `Recv` goroutine 也因流被 `GracefulStop` 关闭而解除阻塞。`preRecvCheck`（[gateway.go:292-314](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L292-L314)）则覆盖「进入 select 前就已关停/取消」的竞态。

**(4) handleProcessingRequest：阶段分发总表**

[gateway.go:376-459](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L376-L459) 是本模块的「路由表」——按 `req.Request` 的具体类型分发到四个 handler：

```go
switch req.Request.(type) {
case *extProcPb.ProcessingRequest_RequestHeaders:
    resp, st.user, st.rpm, st.routerCtx = s.HandleRequestHeaders(...)
    st.model = st.routerCtx.Model
case *extProcPb.ProcessingRequest_RequestBody:
    resp, st.model, st.stream, st.traceTerm = s.HandleRequestBody(...)
    // 非 ImmediateResponse 才开启 inference span
case *extProcPb.ProcessingRequest_ResponseHeaders:
    resp, st.isRespError, st.respErrorCode = s.HandleResponseHeaders(...)
case *extProcPb.ProcessingRequest_ResponseBody:
    resp, st.completed = s.HandleResponseBody(...)
}
```

这张表就是请求生命周期与网关方法的对应关系，也是本讲实践任务的核心。注意几个细节：

- **请求头阶段产出 `routerCtx`**，它会被后续阶段复用——`RoutingContext` 就是这样贯穿整条流的。
- **请求体阶段才确定 `model`**（从 body 解析），所以 `st.model` 在这一步才被真正赋值；这也解释了「为什么认证放请求头、路由放请求体」。
- **`ImmediateResponse` 是短路标志**：当某个 handler 返回带 `ImmediateResponse` 的响应（如认证失败、无 Pod），表示请求被本地拒绝、不会进入推理阶段——见 [gateway.go:443-458](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L443-L458) 的失败指标统计分支。

**(5) 四个 handler 的职责边界**

| ExtProc 阶段 | 网关方法 | 主要职责 | 是否路由 |
| --- | --- | --- | --- |
| RequestHeaders | `HandleRequestHeaders` ([gateway_req_headers.go:44](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_headers.go#L44)) | API Key 认证、解析用户、`checkLimits` 限流预检、构建 `RoutingContext`、注入 trace 头 | ❌ |
| RequestBody | `HandleRequestBody` ([gateway_req_body.go:42](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_body.go#L42)) | 解析 model/message/stream、模型可用性校验、**`selectTargetPod` 路由决策**、注入 target-pod 头 | ✅ |
| ResponseHeaders | `HandleResponseHeaders` ([gateway_rsp_headers.go:29](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_rsp_headers.go#L29)) | 判断上游响应是否为错误（4xx/5xx），决定后续 body 处理走错误分支还是正常分支 | ❌ |
| ResponseBody | `HandleResponseBody` ([gateway_rsp_body.go:162](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_rsp_body.go#L162)) | 统计 token 用量、更新 TPM/RPM 指标、判断流式结束（置 `completed`） | ❌ |

#### 4.2.4 代码实践

**实践目标**：把请求生命周期与网关方法一一对应，并验证「认证在请求头阶段、路由在请求体阶段」。

**操作步骤**：

1. 打开 [gateway.go:376-428](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L376-L428)，确认 `switch req.Request.(type)` 的四个分支。
2. 阅读请求头阶段 [gateway_req_headers.go:87-105](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_headers.go#L87-L105)：当 API Key 不合法时返回 `StatusCode_Unauthorized` 的 `ImmediateResponse`——这是「请求头阶段就能短路」的证据。
3. 阅读请求体阶段 [gateway_req_body.go:42-105](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_body.go#L42-L105)：注意 `model` 是从 `validateRequestBody(...)` 解析 body 得到的（[L68](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_body.go#L68)），说明必须到 body 阶段才知道模型。
4. 在 4.2.5 的表格里填空。

**需要观察的现象 / 预期结果**：你会清楚看到——**没有任何路由调用出现在 `HandleRequestHeaders` 里**，`routingCtx` 在请求头阶段创建时算法字段还是空（[gateway_req_headers.go:129](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_headers.go#L129) `types.NewRoutingContext(ctx, "", "", ...)`）；路由调用（`selectTargetPod`）只出现在 `HandleRequestBody` 里。

#### 4.2.5 小练习与答案

**练习 1**：如果客户端发了一个 API Key 非法的请求，网关在哪个阶段返回错误？上游 Pod 会被访问吗？

**参考答案**：在请求头阶段（`HandleRequestHeaders`）就返回 `401 Unauthorized` 的 `ImmediateResponse`。因为是 `ImmediateResponse`（短路），Envoy 不会把请求转发给上游 Pod，上游完全感知不到这次请求。

**练习 2**：为什么 `Process` 要在 `st.completed` 时「主动 break」并 `return nil`，而不是等 Envoy 关流？

**参考答案**：为了让 Envoy 能优雅地给客户端发送 HTTP 响应结束帧（`0\r\n\r\n`）。代码注释（[gateway.go:229-236](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L229-L236)）明确说：主动结束流让 Envoy 干净地关闭流并发出结束标记，否则流可能挂住。

**练习 3**：`Server` 和 `processState` 分别是「每请求」还是「每进程」？为什么这么分？

**参考答案**：`Server` 是每进程单例，持有 redis/cache/K8s client 等重资源，跨所有请求复用；`processState` 是每请求一份，持有 requestID/routerCtx/span 等只属于这一次请求的可变状态。把可变状态隔离到每请求结构里，`Server` 就能保持无锁或少锁、安全并发。

---

### 4.3 路由调用集成点

#### 4.3.1 概念说明

「路由」是把一个请求分配到某个具体后端 Pod 的决策。本模块要讲清两件事：

1. **两条互斥的路由分支**：当用户**没有显式配置路由算法**（`RouterNotSet`）时，网关退化为「HTTPRoute 校验器」，把路由交给 Envoy 原生能力；当**配置了路由算法**时，网关才亲自选 Pod。
2. **亲自选 Pod 的入口**：`selectTargetPod` 方法是网关与路由算法包（`pkg/plugins/gateway/algorithms`）之间唯一的集成点。

本讲只讲「集成点」长什么样、在哪里；算法本身（评分、归一化、多策略）在 u7-l3 详述。

#### 4.3.2 核心流程

`HandleRequestBody` 在解析出 model 后，先解析路由策略（headers → config profile → env 默认），然后按策略是否为空走两条分支：

```text
                  解析请求体得到 model/message
                            │
                  validateModelAvailability
                  (模型存在 & 有 Ready Pod?)
                            │ 是
                  deriveRoutingStrategyFromContext
                  (请求头 routing-strategy → 配置画像 → 环境变量)
                            │
                  routing.Validate(strategy) 合法?
                            │
              ┌─────────────┴──────────────┐
              ▼                            ▼
   routingAlgorithm == RouterNotSet    routingAlgorithm 已设置
   (未配置算法)                        (配置了 random/least-load/...)
              │                            │
   validateHTTPRouteStatus               s.selectTargetPod(...)
   (校验 Envoy HTTPRoute 对象              ├ FilterRoutablePods
    存在且被 Accepted)                     ├ FilterPodsByLabelSelector(external-filter)
              │                            ├ routing.Select(routeCtx)  → 取得 router
   注入 model 头，                         └ router.Route(...)         → 得到目标 Pod
   由 Envoy 原生 HTTPRoute 路由                │
                                  注入 target-pod / routing-strategy 头
                                  (Envoy 据此转发到指定 Pod)
```

两条分支最终都产出注入到请求里的「头改写」，差别只在：未配置算法时只设 `model` 头，交给 Envoy 自己路由；配置算法时设 `target-pod` 头，**由网关钦点具体 Pod**。

#### 4.3.3 源码精读

**(1) 路由策略的三级解析**

[gateway_req_body.go:97-105](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_body.go#L97-L105) 解析并校验路由策略：

```go
if strategy, enabled := deriveRoutingStrategyFromContext(routingCtx); enabled {
    var ok bool
    if routingAlgorithm, ok = routing.Validate(strategy); !ok {
        // 400 Bad Request：非法策略名
        return buildErrorResponse(envoyTypePb.StatusCode_BadRequest, ...), model, stream, term
    }
    routingCtx.Algorithm = routingAlgorithm
}
```

`deriveRoutingStrategyFromContext`（[util.go:670-692](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/util.go#L670-L692)）按优先级解析策略：**请求头 `routing-strategy` > 模型配置画像 `ConfigProfile` > 环境变量 `ROUTING_ALGORITHM`**（环境变量见 [types.go:81](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/types.go#L81)）。这种三级回退让「单次请求临时指定策略」成为可能（在 header 里带 `routing-strategy: random` 即可覆盖默认）。

**(2) 两条分支的分流**

[gateway_req_body.go:127-177](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_body.go#L127-L177) 是路由的核心分流点：

```go
if routingAlgorithm == routing.RouterNotSet {
    // 分支 A：未配置算法 → 校验 Envoy HTTPRoute，交给 Envoy 原生路由
    if err := s.validateHTTPRouteStatus(ctx, model); err != nil {
        return buildErrorResponse(envoyTypePb.StatusCode_ServiceUnavailable, ...), ...
    }
    headers = buildEnvoyProxyHeaders(headers, HeaderModel, model)
} else {
    // 分支 B：配置了算法 → 网关亲自选 Pod
    externalFilter := routingCtx.ReqHeaders[HeaderExternalFilter]
    targetPodIP, err := s.selectTargetPod(ctx, routingCtx, podsArr, externalFilter)
    if targetPodIP == "" || err != nil { ... return 503 ... }
    headers = buildEnvoyProxyHeaders(headers,
        HeaderRoutingStrategy, string(routingAlgorithm),
        HeaderTargetPod,      targetPodIP,   // 钦点目标 Pod
        "content-length",     strconv.Itoa(len(routingCtx.ReqBody)),
        "X-Request-Id",       routingCtx.RequestID)
}
```

`RouterNotSet` 定义在 [router.go:35](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/algorithms/router.go#L35)，就是空字符串 `""`。

> 💡 **分支 A 的意义**：AIBrix 网关可以「什么都不配」，纯靠 Envoy Gateway 的 HTTPRoute 资源做最简单的负载均衡。这时网关只做认证、模型存在性校验、TPM/RPM 统计，**不做智能路由**。`validateHTTPRouteStatus`（[gateway.go:544-611](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L544-L611)）会用 30 秒 TTL 缓存 + `singleflight` 防止高并发下把 K8s API 打爆。

**(3) selectTargetPod：唯一的算法集成点**

[gateway.go:491-539](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L491-L539) 是网关与路由算法包之间唯一的集成点，逻辑清晰：

```go
func (s *Server) selectTargetPod(ctx context.Context, routeCtx *types.RoutingContext,
    pods types.PodList, externalFilterExpr string) (string, error) {
    readyPods := utils.FilterRoutablePods(pods.All())              // 1. 过滤可路由 Pod
    readyPods, err = utils.FilterPodsByLabelSelector(readyPods, externalFilterExpr) // 2. 外部过滤
    if routeCtx.Algorithm == routing.RouterPD {                     // 3. PD 解耦特殊处理
        engine, err := routing.ValidateAndGetLLMEngine(readyPods)
        routeCtx.Engine = engine
    }
    router, err := routing.Select(routeCtx)                         // 4. 选出路由器实例
    // 5. 快路径：只有一个 Pod 且端口数≤1 → 直接选它
    if len(readyPods) == 1 && len(utils.GetPortsForPod(readyPods[0])) <= 1 && ... {
        routeCtx.SetTargetPod(readyPods[0]); return routeCtx.TargetAddress(), nil
    }
    utils.CryptoShuffle(readyPods)                                  // 6. 加密随机打乱（避免同分时总选同一个）
    return router.Route(routeCtx, &utils.PodArray{Pods: readyPods}) // 7. 调用算法选 Pod
}
```

把它和算法包的 `Select` 对照看：[router.go:431-470](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/algorithms/router.go#L431-L470) 的 `Select` 根据 `routeCtx.Algorithm` 字符串（支持 `prefix-cache:2,least-latency:1` 这种多策略逗号配置）从 `RouterManager` 里取出对应的 `Router` 实例，最终由 `router.Route(...)` 完成打分排序。也就是说：**`selectTargetPod` 负责「准备候选 Pod + 调用算法」，算法负责「在候选里挑一个」**——职责干净分离。

**(4) 路由算法的自注册**

`routing.Init()` 在 `NewServer`（[gateway.go:176](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L176)）里被调用，触发 `defaultRM.Init()`。各个算法文件（`random.go`、`least_load.go`、`throughput.go` 等）在 init 阶段通过 `Register`/`RegisterProvider` 把自己注册进 `RouterManager` 的 `routerFactory` map（[router.go:376-386](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/algorithms/router.go#L376-L386)）。这是「可插拔路由」的基础，也是 u7-l3、u7-l4 要展开的重点。

#### 4.3.4 代码实践（可运行）

**实践目标**：用现有单元测试观察 `selectTargetPod` 的行为，并验证「注入 target-pod 头」的效果。

**操作步骤**：

1. 打开 [pkg/plugins/gateway/gateway_test.go:104](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_test.go#L104) 的 `Test_selectTargetPod`，它是专门测 `selectTargetPod` 的表驱动测试，覆盖了多种 Pod 候选场景。
2. 运行该测试（在仓库根目录）：

   ```bash
   go test ./pkg/plugins/gateway/ -run Test_selectTargetPod -v
   ```

3. 阅读测试里如何构造 `mockRouter`（[gateway_test.go:112](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_test.go#L112)）与 `types.RoutingContext`，理解「算法被替换成 mock」的注入方式。

**需要观察的现象 / 预期结果**：

- 测试应全部通过（`PASS`）。
- 你会看到测试通过 `routeCtx.TargetAddress()` 或返回的 podIP 来断言选中的 Pod，这正是 `selectTargetPod` 的产物。
- 想观察「未配置算法」分支：`Test_selectTargetPod` 不覆盖它（它总是设置算法）。要验证分支 A，需阅读 `HandleRequestBody` 的 [gateway_req_body.go:127-132](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway_req_body.go#L127-L132)，并确认 `validateHTTPRouteStatus` 在 `gatewayClient == nil`（standalone）时直接返回 nil（[gateway.go:546-548](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L546-L548)）。

> 待本地验证：若你的环境无 Go 工具链，可改为纯源码阅读——在 `selectTargetPod` 的 7 个步骤上各写一句话注释，确认你理解每一步。如果无法运行测试，明确标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：假设你想给某个请求临时用 `random` 策略（不改全局配置），怎么做？

**参考答案**：在该请求的 HTTP 头里加 `routing-strategy: random`。因为 `deriveRoutingStrategyFromContext` 的最高优先级是请求头（[util.go:673-681](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/util.go#L673-L681)），它会覆盖环境变量默认值。若该策略名非法，网关返回 400。

**练习 2**：为什么 `selectTargetPod` 在调用 `router.Route` 前要 `CryptoShuffle(readyPods)`？

**参考答案**：当多个 Pod 得分相同时，若候选顺序固定，算法（如取第一个）会总选同一个 Pod，造成负载倾斜。`CryptoShuffle` 用加密随机打乱候选顺序，保证同分时选择是随机的、不可预测的，同时也避免攻击者通过顺序推断路由行为。

**练习 3**：分支 A（`RouterNotSet`）和分支 B 在注入给 Envoy 的头上有什么本质区别？

**参考答案**：分支 A 只注入 `model` 头，不指定具体 Pod，由 Envoy 根据 HTTPRoute 资源自己负载均衡；分支 B 注入 `target-pod`（目标 Pod IP）头，相当于网关「钦点」了具体 Pod，Envoy 只需照此转发。前者是「网关放权」，后者是「网关集权」。

---

## 5. 综合实践

把本讲三块知识串起来，完成下面这个**贯穿性任务**。

**场景**：一个客户端向网关发了一个流式 chat 请求：

```http
POST /v1/chat/completions
routing-strategy: random
authorization: Bearer sk-xxx
content-type: application/json

{"model": "llama3-8b", "messages": [...], "stream": true}
```

**任务**：在下面的表格里，填出该请求在每个 ExtProc 阶段触发的网关方法、关键动作与产出，并标注**路由决策发生在哪个阶段**。

| 阶段序号 | ExtProc 消息类型 | 触发的网关方法 | 关键动作 | 注入给 Envoy 的内容 | 是否路由 |
| --- | --- | --- | --- | --- | --- |
| ① | RequestHeaders | `HandleRequestHeaders` | 校验 API Key、解析 user、`checkLimits`、建 `RoutingContext` | `x-went-into-req-headers: true` + trace 头 | ❌ |
| ② | RequestBody | `?` | `?` | `?` | ✅ |
| ③ | ResponseHeaders | `?` | `?` | （判断上游是否出错） | ❌ |
| ④ | ResponseBody（多次） | `?` | `?` | （token 统计、置 `completed`） | ❌ |

**完成后请回答**：

1. 阶段 ② 中，`selectTargetPod` 在本例里会选中哪个算法的 router？（提示：`routing-strategy: random` → `RouterRandom`）
2. 如果把 `routing-strategy` 头去掉、且环境变量 `ROUTING_ALGORITHM` 也没设，路由会走哪条分支？网关还会亲自选 Pod 吗？
3. 若阶段 ① 的 API Key 非法，阶段 ②③④ 还会执行吗？为什么？

**参考答案要点**：

- 阶段 ② 方法是 `HandleRequestBody`，关键动作是「解析 model=llama3-8b → `validateModelAvailability` → `deriveRoutingStrategyFromContext` 得到 random → `selectTargetPod` 走 `RouterRandom` 选 Pod」，注入 `target-pod`、`routing-strategy: random`、`content-length`、`X-Request-Id`。
- 问题 2：走分支 A（`RouterNotSet`），网关不再亲自选 Pod，只校验 HTTPRoute 并注入 `model` 头，交给 Envoy 原生路由。
- 问题 3：不会执行。阶段 ① 返回 `ImmediateResponse`（401），`Process` 流短路结束，请求体阶段根本不会被触发——这正是认证放在请求头阶段的价值。

## 6. 本讲小结

- AIBrix 网关通过 **Envoy ExtProc** 协议接入请求：Envoy 为每个 HTTP 请求开一条 gRPC 双向流，按生命周期分阶段推送 `ProcessingRequest`，网关用 Go 在外部实现处理逻辑。
- `cmd/plugins/main.go` 是装配流水线，关键一行是 `extProcPb.RegisterExternalProcessorServer(s, gatewayServer)`，把 `*gateway.Server` 注册为 ExtProc 处理器，gRPC 监听 `:50052`，HTTP 监听 `:8080`（metrics + /v1/models）。
- `Process` 方法是双向流主循环：一条流=一个请求，用 `processState` 保存每请求状态，循环 `processOnce` 直到 `st.completed`；`processOnce` 用 goroutine + `select` 监听 `shutdownCh` 解决了 ExtProc 流粘性导致的优雅关停难题。
- `handleProcessingRequest` 是阶段分发总表：RequestHeaders→`HandleRequestHeaders`（认证）、RequestBody→`HandleRequestBody`（路由）、ResponseHeaders→`HandleResponseHeaders`、ResponseBody→`HandleResponseBody`（统计）。
- **路由决策发生在请求体阶段**（`HandleRequestBody`），不在请求头阶段——因为 model 与 message 在 body 里；策略来源优先级为「请求头 → 配置画像 → 环境变量」。
- 路由有两条互斥分支：未配置算法（`RouterNotSet`）时网关退化为 HTTPRoute 校验器、由 Envoy 原生路由；配置算法时通过唯一集成点 `selectTargetPod` 调用 `routing.Select` + `router.Route` 亲自钦点 Pod，并注入 `target-pod` 头。

## 7. 下一步学习建议

本讲只打通了网关的「入口与阶段骨架」。建议接下来：

- **u7-l2 请求/响应处理链与 API Key 认证**：深入 `HandleRequestHeaders` 的认证细节与请求体/响应体 body 的解析改写链。
- **u7-l3 路由抽象 RouterManager 与多策略路由**：把本讲「`routing.Select` + `router.Route`」这一句展开，看清 `Router` 接口、`RouterManager` 注册中心与多策略评分归一化。
- **u7-l4 基础负载感知路由算法**：阅读 `random.go`、`least_load.go`、`throughput.go` 等具体算法，理解它们如何向 `RouterManager` 注册（呼应本讲的「可插拔路由」）。
- 如果你对**跨副本状态同步**感兴趣，可先跳读 [pkg/plugins/gateway/statesync](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/statesync)，理解多网关副本如何经 Redis 共享路由状态（对应 main.go 里 `AIBRIX_STATESYNC_ENABLED` 分支）。
