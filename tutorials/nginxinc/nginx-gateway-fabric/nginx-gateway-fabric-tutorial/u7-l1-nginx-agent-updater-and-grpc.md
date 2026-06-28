# NginxUpdater 与 gRPC Agent 通信

> 前置：本讲承接 [u6-l1 配置生成器总览](u6-l1-config-generator-overview.md)。配置生成器 `generator.Generate(conf)` 已经把 `dataplane.Configuration` 渲染成了一组 `[]agent.File`。本讲要回答的核心问题是：**这些配置文件是怎么从控制面（Go 进程）一路送到数据面（NGINX Pod）里的？** 答案是一条以 NGINX Agent 为客户端、控制面为服务端的 gRPC 通道，以及一套自研的「广播 + 订阅」消息机制。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `NginxUpdater` 接口的职责，以及 `UpdateConfig` 在一次配置更新里做了哪三件事。
- 描述控制面 gRPC 服务是如何启动的、注册了哪些 gRPC service、用什么样的安全（mTLS + TokenReview）保护连接。
- 解释 `ConnectionsTracker` 如何用 UUID 追踪每一条 Agent 连接，以及连接「就绪」的判定标准。
- 复述「广播者（Broadcaster）→ 订阅者（Subscribe 双向流）→ Messenger → Agent」这条完整消息路径，以及 Agent 反向拉文件（`GetFile`）的二次往返。
- 跟踪一次配置更新从 `UpdateConfig` 到 Agent 收到请求的完整调用链。

## 2. 前置知识

阅读本讲前，建议先理解下面几个概念：

- **控制面 / 数据面分离**：NGF 的 Go 进程只负责 watch K8s 资源、生成配置；真正处理流量的 NGINX 跑在另一个 Pod 里。两者必须有一条通信通道把配置送过去。详见 [u1-l1 项目概览](u1-l1-project-overview.md)。
- **NGINX Agent**：这是 F5 提供的一个独立组件（不是本仓库的代码），以 sidecar 形式和数据面 NGINX 跑在同一个 Pod 里。它作为一个 gRPC **客户端**，主动 **拨号** 连接到控制面的 gRPC 服务端，接收配置并触发 NGINX reload。注意方向：是数据面连控制面，不是控制面连数据面。
- **gRPC 双向流（bidirectional streaming）**：客户端和服务端各自持有一条长连接的流，双方都可以随时在上面发消息。NGF 用 `CommandService.Subscribe` 这一条双向流作为「控制面 → Agent」的主信道。
- **MPI（Management Plane Interface）协议**：NGINX Agent v3 用的 gRPC 协议，定义在 `github.com/nginx/agent/v3/api/grpc/mpi/v1`（简称 `pb`）。本讲会遇到 `CommandService`、`FileService`、`ManagementPlaneRequest`、`DataPlaneResponse`、`ConfigApplyRequest` 等类型，都来自这个包。
- **mTLS**：双向 TLS。控制面和 Agent 互相验证对方证书，共享同一个 CA。证书由 `generate-certs` 命令生成，详见 [u2-l3 初始化命令与证书生成](u2-l3-initialize-and-generate-certs.md)。
- **manager.Runnable**：controller-runtime 的抽象，表示「一个要被 Manager 管理生命周期的长跑 goroutine」。gRPC 服务就是以 Runnable 形式注册进 Manager 的。详见 [u3-l4 Leader Election 与 Runnables](u3-l4-leader-election-and-runnables.md)。

一个关键直觉先建立起来：NGF 的控制面 **不直接** 把配置写进 NGINX Pod 的磁盘，而是把「文件清单（file overviews，含名字/大小/hash）」通过一条长连接推给 Agent；Agent 拿到清单后，发现自己缺哪些文件，再 **反向** 用 `GetFile` / `GetFileStream` 这两个 RPC 把文件内容按需拉回去。这是一种「推元数据 + 拉内容」的混合模式，好处是大文件可以分块流式拉取，且 Agent 可以做增量比对。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `internal/controller/nginx/agent/agent.go` | 定义 `NginxUpdater` 接口与其实现 `NginxUpdaterImpl`，是配置下发的**总入口**。 |
| `internal/controller/nginx/agent/deployment.go` | 定义 `Deployment`（一个数据面 Deployment 的运行时状态：文件、版本、错误）与 `DeploymentStore`。`SetFiles` 计算配置版本、产出广播消息。 |
| `internal/controller/nginx/agent/broadcast/broadcast.go` | 自研的「广播者」：把一条配置消息扇出（fan-out）给该 Deployment 下所有已订阅的 Agent。 |
| `internal/controller/nginx/agent/command.go` | `commandService`，实现 gRPC `CommandService`。`Subscribe` 是每个 Agent 连接的核心循环。 |
| `internal/controller/nginx/agent/grpc/messenger/messenger.go` | `Messenger`，把一条 gRPC 双向流封装成「发 / 收」两个 channel，解耦读写。 |
| `internal/controller/nginx/agent/file.go` | `fileService`，实现 gRPC `FileService`。`GetFile` / `GetFileStream` 供 Agent 反向拉文件内容。 |
| `internal/controller/nginx/agent/grpc/grpc.go` | gRPC **服务端** 的创建、mTLS 配置、服务注册、TLS 文件热加载。 |
| `internal/controller/nginx/agent/grpc/connections.go` | `ConnectionsTracker`，按 UUID 追踪所有 Agent 连接。 |
| `internal/controller/nginx/agent/grpc/filewatcher/filewatcher.go` | 监听 TLS 证书文件变化，证书轮换时触发所有 Agent 重连。 |
| `internal/controller/nginx/agent/grpc/interceptor/interceptor.go` | gRPC 拦截器：做 TokenReview 身份校验，把连接身份注入 context。 |
| `internal/controller/manager.go` | `createAgentServices`：把上述组件装配起来并注册进 Manager。 |

一个分层心智模型（从上到下是调用方向）：

```
EventHandler (handler.go)
        │  UpdateConfig(deployment, files, vm)
        ▼
NginxUpdaterImpl (agent.go)          ← 第 1 层：接口入口
        │  deployment.SetFiles(...)  → 计算版本，产出消息
        │  broadcaster.Send(msg)     → 扇出给所有订阅者
        ▼
DeploymentBroadcaster (broadcast.go) ← 第 3 层：消息广播
        │  往每个 listener 的 listenCh 投递消息
        ▼
commandService.Subscribe (command.go)← 第 3 层：每个 Agent 一个循环
        │  收到 msg → buildRequest → msgr.Send
        ▼
Messenger (messenger.go)             ← 读写解耦
        │  server.Send(ManagementPlaneRequest)  走 gRPC 流
        ▼
NGINX Agent (数据面，外部组件)
        │  收到 ConfigApplyRequest，发现缺文件
        │  反向调用 GetFile / GetFileStream
        ▼
fileService.GetFile (file.go)        ← 反向信道：按需拉文件
        │  返回文件内容
        ▼
NGINX Agent 写盘 + reload，回 DataPlaneResponse
```

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**NginxUpdater 接口**（4.1）、**gRPC 服务与连接**（4.2）、**消息广播**（4.3）。三者串起来就是一次完整的下发链路。

### 4.1 NginxUpdater 接口：配置下发的总入口

#### 4.1.1 概念说明

`NginxUpdater` 是控制面对「把配置送给数据面」这件事的抽象。它对上游（eventHandler）只暴露两个方法：

- `UpdateConfig`：下发**完整的一组配置文件**（OSS 和 Plus 都用，会触发 NGINX reload）。
- `UpdateUpstreamServers`：**仅 Plus**，用 NGINX Plus 的动态 API 增量改 upstream server 列表（不 reload）。

为什么要抽象成接口？因为 eventHandler 需要在测试里用 fake 替换它（注意源码里 `//counterfeiter:generate . NginxUpdater` 这行注释，counterfeiter 会据此生成 mock），从而单测不需要真起 gRPC 服务。

一个关键设计：`NginxUpdater` 自己**不知道**有哪些 Agent 连上来了，也**不直接**发 gRPC。它只做两件事——算出「这次配置和上次比有没有变」，变了就把消息丢给一个 `Broadcaster`。真正和 Agent 通信是 Broadcaster + commandService 的事（见 4.3）。这种分层让「配置生成」和「网络通信」彻底解耦。

#### 4.1.2 核心流程

`UpdateConfig` 的流程，源码注释里写得很清楚：

1. **设置文件并计算版本**：`deployment.SetFiles(files, volumeMounts)` 把新文件存进 `Deployment`，并重新计算一个 `configVersion`（文件清单的 hash）。如果版本没变，返回 `nil`——意味着「配置其实没变」，直接 return，**什么都不发**。这是把 N 次无意义变更压成 0 次下发的关键。
2. **广播消息**：版本变了，就 `deployment.GetBroadcaster().Send(*msg)`，把包含文件元数据（名字、大小、hash，**不含内容**）的消息扇出给所有订阅的 Agent。
3. **登记错误**：`deployment.SetLatestConfigError(deployment.GetConfigurationStatus())`，把「各 Pod 上一次 apply 的聚合错误」记下来，供后续写 Gateway status 用。

`UpdateUpstreamServers`（仅 Plus）流程类似但更复杂：它把每个 upstream 翻译成一条 `NGINXPlusAction`（动态增删 server），逐条广播，并带 5 秒重试（因为 reload 后 NGINX 可能还没就绪）。

一个把「发消息」和「等结果」分离的设计点：`broadcaster.Send` 是**同步**的——它会阻塞到所有订阅者都回复（或出错）才返回。这样 `UpdateConfig` 返回时，配置已经下发完毕（成功或失败），eventHandler 才能据此写正确的 status。

#### 4.1.3 源码精读

先看接口定义和实现结构：

[NginxUpdater 接口定义（agent.go:31-35）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/agent.go#L31-L35) —— 两个方法，一个是整组文件下发，一个是 Plus 专属的 upstream 动态更新。

[NewNginxUpdater 工厂（agent.go:47-76）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/agent.go#L47-L76) —— 这里能看出它内部由三块拼成：`ConnectionsTracker`（连接表）、`DeploymentStore`（按 Deployment 组织文件）、`commandService` + `fileService`（两个 gRPC service）。`resetConnChan` 是证书轮换时通知重连的通道。注意 `commandService` 和 `fileService` 共享同一个 `nginxDeployments` 和 `connTracker`——这是它们能协作的基础。

接下来是本模块的核心 `UpdateConfig`：

[UpdateConfig 的流程注释（agent.go:78-87）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/agent.go#L78-L87) —— 这段注释本身就是最好的「调用链说明书」，建议逐行读懂：设置文件 → 广播元数据 → Agent 收到 ConfigApplyRequest → Agent 反向 GetFile 拉每个文件 → Agent 更新 NGINX 并回 DataPlaneResponse → 订阅者回复 broadcaster 表示事务完成 → 错误写回 deployment。

[UpdateConfig 实现（agent.go:88-105）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/agent.go#L88-L105) —— 三步：`SetFiles`（算版本）、`Send`（广播）、`SetLatestConfigError`。`msg == nil` 时打印 "No changes" 并直接 return。

版本计算在 `Deployment` 里：

[SetFiles 与 rebuildFileOverviews（deployment.go:204-209 与 272-317）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L272-L317) —— `rebuildFileOverviews` 把 `[]File` 转成 `[]*pb.File`（只含 `FileMeta`，**不含内容**），再叠加一组 `Unmanaged: true` 的文件（静态文件如 `nginx.conf`、以及用户 volume mount 的文件，告诉 Agent「这些别碰」），最后 `filesHelper.GenerateConfigVersion` 算 hash。`d.configVersion == newConfigVersion` 时返回 `nil`，这就是「无变更不下发」的闸门。

再看上游是怎么调用 `UpdateConfig` 的（这是调用链的起点）：

[handler.go 在 FileLock 保护下调用 updateNginxConf（handler.go:318-320）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/../handler.go#L318-L320) —— 注意这里**先 `deployment.FileLock.Lock()`** 再调用，这是整个 ConfigApply 事务的互斥点（4.3 会讲为什么）。

[updateNginxConf 调用 UpdateConfig（handler.go:878-891）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/../handler.go#L878-L891) —— `generator.Generate(conf)` 产出文件，紧接着 `nginxUpdater.UpdateConfig`，Plus 模式下再追加 `UpdateUpstreamServers`。

#### 4.1.4 代码实践

**实践目标**：确认 `UpdateConfig` 的「无变更即跳过」行为，并定位三个关键步骤在源码里的精确位置。

**操作步骤**：

1. 打开 [agent.go 的 UpdateConfig（agent.go:88-105）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/agent.go#L88-L105)，在 `if msg == nil {` 这一行（第 94 行）和 `deployment.SetLatestConfigError(...)` 这一行（第 104 行）各加一行日志（**示例代码，仅为阅读练习，不要提交**）：
   ```go
   // 第 94 行后
   n.logger.V(1).Info("DEBUG: SetFiles returned nil msg, skipping broadcast")
   // 第 99 行前
   n.logger.V(1).Info("DEBUG: about to broadcast", "version", msg.ConfigVersion, "files", len(msg.FileOverviews))
   ```
2. 打开 [deployment.go 的 rebuildFileOverviews（deployment.go:303-316）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L303-L316)，确认 `configVersion` 比较那两行。

**需要观察的现象**：当你反复 apply 同一份 HTTPRoute（内容不变）时，由于 eventHandler 的 changed predicate（见 [u4-l4](u4-l4-change-processor-and-store.md)）通常已经在更上层过滤掉了，但万一到达 `UpdateConfig`，你会看到 `SetFiles returned nil msg` 的日志，而**不会**看到 `about to broadcast`。

**预期结果**：「无变更」路径走第 94-97 行早退；「有变更」路径走第 99 行广播。这条路径是否真的执行，取决于上游 predicate 是否放行，属正常。

**待本地验证**：实际日志输出需在 kind 集群里运行 NGF 才能观察到；纯阅读可跳过运行，只做源码定位。

#### 4.1.5 小练习与答案

**练习 1**：`UpdateConfig` 既不接收 context 也不返回 error，那「下发失败」是如何被上游知道的？

**参考答案**：错误不通过返回值传递，而是写进 `Deployment` 的状态——`deployment.SetLatestConfigError(deployment.GetConfigurationStatus())`。`GetConfigurationStatus`（见 [deployment.go:150-164](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L150-L164)）把所有 Pod 的错误聚合成一个 error。eventHandler 在 `FileLock.Unlock()` 之后用 `deployment.GetLatestConfigError()` 取出，再 enqueue 成 status（见 [handler.go:322-335](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L322-L335)）。

**练习 2**：`UpdateUpstreamServers` 开头有 `if !n.plus { return }`。为什么这个方法仍留在 `NginxUpdater` 接口里，而不是单独建一个 Plus 专属接口？

**参考答案**：为了调用方（eventHandler）的简单。eventHandler 只持有一个 `NginxUpdater`，在 [handler.go:887-890](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L887-L890) 里无条件调用 `UpdateUpstreamServers`，由实现内部用 `n.plus` 判断是否真正执行。这样 OSS / Plus 走同一套调用代码，差异被封装在实现内部。

---

### 4.2 gRPC 服务与连接：控制面是服务端

#### 4.2.1 概念说明

本模块回答：**控制面的 gRPC 服务长什么样？Agent 怎么连进来？连接怎么被追踪和校验？**

要点先列清楚：

1. **控制面是 gRPC 服务端，Agent 是客户端**。控制面监听一个端口（`grpcServerPort`），Agent 主动拨号进来。这一点和很多人直觉相反——不是控制面去连数据面。
2. **服务端注册了两个 gRPC service**：`CommandService`（连接管理 + 配置推送的主信道）和 `FileService`（Agent 反向拉文件）。它们都来自 NGINX Agent 的 MPI 协议 `pb`。
3. **连接用 UUID 追踪**：每条 Agent 连接在拦截器里被赋予一个 UUID（来自 gRPC metadata），后续所有 RPC（`GetFile`、`Subscribe`）都靠这个 UUID 找到对应的 `Deployment`。
4. **mTLS + TokenReview 双重校验**：传输层用 mTLS（互验证书），应用层用 Kubernetes TokenReview 校验 Agent 的 ServiceAccount token，并确认对应 Pod 已 Running。
5. **证书热加载**：证书轮换（Helm pre-upgrade Job 重新生成）后，控制面 **无需重启** 即可对新连接生效；并通过 filewatcher 触发老连接重连。

#### 4.2.2 核心流程

**启动流程**（`grpc.Server.Start`）：

1. 在配置端口上 `Listen("tcp")`。
2. `getTLSConfig()` 构建 mTLS 凭证（启动时校验证书文件存在且可解析，失败则直接返回 error，服务起不来）。
3. `createServer` 用一组选项创建 `grpc.Server`：keepalive 参数、Stream/Unary 拦截器（做身份校验）、TLS 凭证、4MB 消息上限（与 Agent 侧对齐）。
4. 逐个调用 `registerServices`（即 `commandService.Register` 和 `fileService.Register`），把两个 service 挂上去。
5. 起 filewatcher 监听 3 个 TLS 文件（CA、cert、key）。
6. `server.Serve(listener)` 阻塞服务；ctx 结束时 `server.Stop()`（注意是 `Stop` 不是 `GracefulStop`，因为有长连接的双向流，GracefulStop 永远不会结束）。

**连接追踪流程**（`ConnectionsTracker`）：

- Agent 拨号成功后，第一个调用的 RPC 是 `CreateConnection`，commandService 在这里把 `{ParentName, ParentType, InstanceID}` 存进 tracker，key 是 UUID。
- Agent 发现自己的 NGINX 实例后，调用 `UpdateDataPlaneStatus`，commandService 用 `SetInstanceID` 补上 InstanceID。
- 一条连接「就绪」(`Ready()`) 的判定是 **`InstanceID != ""`**——也就是 Agent 不仅连上了，还报告了它管理的 NGINX 实例。`Subscribe` 循环在 `waitForConnection` 里会一直等到 `Ready()` 才往下走。

**身份校验流程**（拦截器 `ContextSetter`）：

- 每个 RPC 进来，拦截器先从 metadata 取 `uuid` 和 `authorization` 两个头，缺一不可。
- 用 `authorization` 里的 token 发起一次 Kubernetes `TokenReview`，校验它是合法的 ServiceAccount token。
- 解析 token 的 username（格式 `system:serviceaccount:NAMESPACE:NAME`），按 namespace + label 列出 Pod，等到至少一个 Running 的 Pod（吸收 Agent 比 NGF cache 先就绪的启动竞态）。
- 把 `GrpcInfo{UUID, Token}` 注入 context，后续 service 通过 `grpcContext.FromContext(ctx)` 取出 UUID。

#### 4.2.3 源码精读

先看服务端创建与启动：

[grpc.go 常量：keepalive 与证书路径（grpc.go:27-33）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/grpc.go#L27-L33) —— keepalive 15s/10s 防止长连接被中间设备掐断；证书路径固定在 `/var/run/secrets/ngf/` 下。

[Server 结构体（grpc.go:43-58）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/grpc.go#L43-L58) —— 持有拦截器、要注册的 service 列表、端口，以及 `resetConnChan`（filewatcher 用来通知重连）。

[Start：监听 + 建服务 + 注册 + 起 filewatcher + Serve（grpc.go:77-112）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/grpc.go#L77-L112) —— 注意第 96-102 行起 filewatcher，第 104-109 行注册 ctx 结束时 `server.Stop()`。最后一行 `server.Serve(listener)` 阻塞。

[createServer：gRPC 服务端选项（grpc.go:114-137）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/grpc.go#L114-L137) —— 关键选项：`ChainStreamInterceptor`（流式 RPC 走身份校验）、`Creds(tlsCredentials)`（mTLS）、`MaxSendMsgSize/MaxRecvMsgSize` 4MB。

mTLS 的精妙之处在证书热加载：

[buildTLSCredentials 与 buildConfigForClient（grpc.go:143-189）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/grpc.go#L143-L189) —— 启动时只做一次「文件存在且可解析」的校验（第 151-157 行）；之后**每来一个新连接**，`GetConfigForClient` 回调都从磁盘重新读 CA 池和服务端证书（第 169-189 行）。这样 Helm pre-upgrade Job 轮换证书后，新连接立刻用新证书，老连接则由 filewatcher 触发重连。`ClientAuth: tls.RequireAndVerifyClientCert` 强制校验客户端（Agent）证书。

接着看连接追踪：

[ConnectionsTracker 接口与 Connection 结构（connections.go:14-33）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/connections.go#L14-L33) —— `Connection` 含 `InstanceID`（NGINX 实例 ID）、`ParentType/ParentName`（所属 Deployment/DaemonSet）。`Ready()` 仅当 `InstanceID != ""`。

[AgentConnectionsTracker 实现（connections.go:35-83）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/connections.go#L35-L83) —— 一个 `map[string]Connection` + `sync.RWMutex`，四个方法 `Track/GetConnection/SetInstanceID/RemoveConnection` 都加锁，线程安全。

再看 CreateConnection 和 UpdateDataPlaneStatus 怎么维护这个表：

[CreateConnection：登记连接（command.go:69-118）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L69-L118) —— 从 Agent 上报的 instances 里解析出所属 Deployment 名字和类型（靠 `AgentOwnerNameLabel`/`AgentOwnerTypeLabel` 两个标签），取出 NGINX InstanceID，`connTracker.Track(grpcInfo.UUID, conn)` 存表。

[UpdateDataPlaneStatus：补 InstanceID（command.go:569-593）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L569-L593) —— Agent 发现 NGINX 实例后再调用，`connTracker.SetInstanceID` 把 InstanceID 填上，连接这才 `Ready()`。

最后看身份校验拦截器：

[validateConnection → getGrpcInfo → validateToken（interceptor.go:107-185）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/interceptor/interceptor.go#L107-L185) —— 取 metadata 的 `uuid`/`authorization`（第 116-136 行），用 token 发起 TokenReview（第 138-159 行），校验 username 是 serviceaccount 格式（第 161-168 行），列 Pod 等到 Running（第 180 行调 `waitForRunningPod`），最后注入 context。

[waitForRunningPod：吸收启动竞态（interceptor.go:193-255）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/interceptor/interceptor.go#L193-L255) —— 轮询 15s 等 Agent 的 Pod 变 Running（处理 WAF 等慢启动 sidecar 让 Pod 迟迟不 Ready 的情况）。

#### 4.2.4 代码实践

**实践目标**：搞清「一条 Agent 连接从拨号到 Ready」经历哪些 RPC，以及哪个 RPC 让连接变 Ready。

**操作步骤**：

1. 打开 [command.go 的 CreateConnection（command.go:72-118）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L72-L118)，找到 `cs.connTracker.Track(...)` 这行。
2. 打开 [UpdateDataPlaneStatus（command.go:572-593）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L572-L593)，找到 `cs.connTracker.SetInstanceID(...)` 这行。
3. 打开 [connections.go 的 Ready（connections.go:29-33）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/connections.go#L29-L33)。
4. 打开 [command.go 的 waitForConnection（command.go:287-319）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L287-L319)，确认它循环检查 `conn.Ready()`。

**需要观察的现象**：在一张纸上画出时间线——`CreateConnection`（Track，但 InstanceID 可能为空，未 Ready）→ `UpdateDataPlaneStatus`（SetInstanceID，Ready）→ `Subscribe`（waitForConnection 通过，进入主循环）。

**预期结果**：连接 Ready 的**唯一**触发点是 `UpdateDataPlaneStatus` 里成功 `SetInstanceID`。若 Agent 一直没报告 NGINX 实例，`waitForConnection` 会在 30s（`connectionWaitTimeout`）后超时返回错误，Agent 需重新建立订阅。

**待本地验证**：可在 NGF 日志里搜索 "Creating connection for nginx pod" 与 "Successfully connected to nginx agent" 两条日志的时间差，验证上述时序。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Server.Start` 关闭时用 `server.Stop()` 而不是 `server.GracefulStop()`？

**参考答案**：见 [grpc.go:104-109](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/grpc.go#L104-L109) 的注释——`CommandService.Subscribe` 是一条**长生命周期的双向流**，只要 Agent 还连着，流就不会结束，`GracefulStop` 会一直等流结束因而永远不返回。所以必须用 `Stop()` 强制关闭。

**练习 2**：mTLS 已经在传输层验证了 Agent 身份，为什么还要在应用层再做一次 TokenReview？

**参考答案**：mTLS 验证的是「证书是否由信任的 CA 签发」，但证书本身不绑定具体的 Kubernetes ServiceAccount / Pod。TokenReview 验证的是 Agent 持有的 ServiceAccount token，能精确到 `system:serviceaccount:NAMESPACE:NAME`，进而定位到具体哪个 Pod，并校验该 Pod 已 Running（[interceptor.go:161-185](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/interceptor/interceptor.go#L161-L185)）。这是纵深防御：即便证书泄露，没有合法 SA token 仍连不上。

---

### 4.3 消息广播：Broadcaster 与 Subscribe 双向流

#### 4.3.1 概念说明

本模块是本讲的「心脏」：**一条配置消息是怎么从 `broadcaster.Send` 一路送到 Agent 的 gRPC 流上的？**

涉及三个组件，务必分清：

- **`DeploymentBroadcaster`**（广播者）：每个 `Deployment` 拥有一个。它维护一组「订阅者」（subscribers），每个订阅者代表一个已连上的 Agent。`Send(msg)` 把消息扇出给所有订阅者，并**同步等待**所有订阅者回复。
- **`commandService.Subscribe`**（订阅循环）：每个 Agent 连接对应一个 `Subscribe` RPC 调用，它就是一个长循环：订阅 broadcaster → 收到消息 → 转成 gRPC 请求 → 发给 Agent → 等 Agent 回复 → 回复 broadcaster。
- **`Messenger`**（信使）：把一条 gRPC 双向流的 `Send`/`Recv` 封装成两个 Go channel（`incoming` 发、`outgoing` 收、`errorCh` 错误），让 `Subscribe` 循环可以用 `select` 同时处理「broadcaster 来的消息」「Agent 的回复」「Agent 的错误」三件事，而不必直接阻塞在 gRPC 调用上。

这套机制解决的核心问题是：**一对多 + 同步确认**。一个 Deployment 可能有多个 Pod（多个 Agent），控制面要把同一份配置发给所有 Pod，并且要等到**每个** Pod 都回复成功/失败后才能判定这次下发整体结果。Broadcaster 用「每订阅者一个 goroutine + WaitGroup」实现了并行扇出 + 汇聚等待。

另一个关键设计是 **`FileLock` 跨整个事务**。从 eventHandler `FileLock.Lock()` → `broadcaster.Send` → 每个 subscriber 发请求、收回复 → `Send` 返回 → eventHandler `FileLock.Unlock()`，这整段时间锁都不释放。这样保证了一个 Agent 在 apply 配置期间，`Deployment.files` 不会被下一次更新改掉——否则 Agent 反向 `GetFile` 时可能拿到错乱的文件版本。

#### 4.3.2 核心流程

**广播者侧（`DeploymentBroadcaster`）**：

1. `NewDeploymentBroadcaster` 启动两个常驻 goroutine：`subscriber`（管理订阅/退订）和 `publisher`（发布消息）。
2. Agent 连上时，`Subscribe()` 注册一个新订阅者，返回 `{ListenCh, ResponseCh, ID}` 三件套。
3. `Send(message)` 把消息塞进 `publishCh`；publisher goroutine 拿到后，**复制一份当前订阅者快照**，为每个订阅者起一个 goroutine：把消息写进它的 `listenCh`，再阻塞等它的 `responseCh`。所有 goroutine 用 `WaitGroup` 等齐后，往 `doneCh` 发信号，`Send` 这才返回。返回值 `len(listeners) > 0` 表示「有没有真的订阅者收到」。

**订阅者侧（`commandService.Subscribe`）**：

1. `waitForConnection` 等连接 Ready。
2. **持 `FileLock.RLock()`** → `broadcaster.Subscribe()` 注册订阅 → `setInitialConfig`（先把当前配置发一遍）→ `FileLock.RUnlock()`。这一段加锁是为了消除「订阅 + 取初始配置」与「并发配置更新」之间的竞态（源码注释 command.go:156-170 详述）。
3. 进入主 `for` 循环，`select` 四路：
   - `ctx.Done()`：连接结束，回复 responseCh 后退出。
   - `resetConnChan`：TLS 文件变了，返回 `Unavailable` 让 Agent 重连。
   - `channels.ListenCh`：收到 broadcaster 来的消息！按 `msg.Type` 构造 `ManagementPlaneRequest`（`ConfigApplyRequest` 或 `APIRequest`），`msgr.Send` 发给 Agent，记下 `pendingBroadcastRequest`。
   - `msgr.Messages()`：Agent 回复了。成功则清错误，失败则记错误；若是 broadcast 请求的回复，往 `responseCh` 发信号通知 broadcaster「我完成了」。
   - `msgr.Errors()`：连接出错，记错误并退出。

**Agent 反向拉文件**（独立于上述循环，由 Agent 主动发起）：

1. Agent 收到 `ConfigApplyRequest`，里面有文件清单（名字/size/hash）。
2. Agent 比对自己已有的文件，对缺失/变更的文件调用 `GetFile`（小文件，一次性返回）或 `GetFileStream`（大文件，分块流式）。
3. fileService 用连接 UUID 从 `connTracker` 找到 `Connection`，再用 `conn.ParentName` 从 `DeploymentStore` 找到 `Deployment`，最后 `deployment.GetFile(name, hash)` 返回内容（**此时 FileLock 仍被 eventHandler 持有**，所以读 `deployment.files` 是安全的）。
4. Agent 写盘、reload、回 `DataPlaneResponse`。

#### 4.3.3 源码精读

先看广播者接口与消息类型：

[Broadcaster 接口与 SubscriberChannels（broadcast.go:14-28）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/broadcast/broadcast.go#L14-L28) —— `Subscribe()` 返回 `ListenCh`（订阅者收消息）、`ResponseCh`（订阅者回信号）、`ID`。

[NginxAgentMessage 与 MessageType（broadcast.go:234-255）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/broadcast/broadcast.go#L234-L255) —— 两种消息类型：`ConfigApplyRequest`（整组文件）和 `APIRequest`（Plus 动态 upstream）。消息里 `FileOverviews` 是**元数据**，`ConfigVersion` 是文件清单的 hash。

广播者的发送逻辑：

[Send：投递 + 等所有订阅者回复（broadcast.go:118-139）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/broadcast/broadcast.go#L118-L139) —— 把消息塞 `publishCh`，然后等 `doneCh`（第 130 行）。返回 `len(b.listeners) > 0`。这就是 `UpdateConfig` 里 `broadcaster.Send` 会**同步阻塞**到所有 Pod 回复的原因。

[publisher：并行扇出 + WaitGroup 汇聚（broadcast.go:179-232）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/broadcast/broadcast.go#L179-L232) —— 第 189-194 行先**复制一份订阅者快照**（避免持锁发送）；第 197-219 行每个订阅者一个 `wg.Go`：发 `listenCh` 再等 `responseCh`；第 220 行 `wg.Wait()` 等齐；第 227 行发 `doneCh`。

再看订阅循环：

[Subscribe 的流程注释（command.go:120-128）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L120-L128) —— 注释把四步说清：等 Agent 注册 → 取最新配置并 apply → 订阅未来更新 → 进循环监听。

[Subscribe 加锁 + 订阅 + 初始配置（command.go:153-187）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L153-L187) —— 第 153 行起 Messenger 并 `go msgr.Run(ctx)`；第 171 行 `FileLock.RLock()`；第 174 行 `broadcaster.Subscribe()`；第 176 行 `setInitialConfig`；第 186 行 `FileLock.RUnlock()`。注释 command.go:156-170 解释了为什么要横跨「订阅 + 初始配置」加锁——防止新订阅者拿到陈旧配置后又错过并发更新。

[Subscribe 主循环（command.go:191-284）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L191-L284) —— 四路 `select`：`ctx.Done`、`resetConnChan`、`ListenCh`（收到消息，第 208-242 行构造请求并发送）、`msgr.Errors`、`msgr.Messages`（第 258-283 行处理 Agent 回复，成功才往 `responseCh` 发信号）。

请求构造：

[buildRequest：构造 ConfigApplyRequest（command.go:470-489）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L470-L489) —— 把文件清单 + InstanceID + ConfigVersion 包成 `ManagementPlaneRequest.ConfigApplyRequest`。每条请求带新的 `MessageId`/`CorrelationId`（用于日志关联）。

Messenger 把 gRPC 流封装成 channel：

[Messenger 接口与实现（messenger.go:14-44）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/messenger/messenger.go#L14-L44) —— `Run` 起两个 goroutine：`handleSend`（从 `incoming` channel 取消息调 `server.Send`）和 `handleRecv`（调 `server.Recv` 阻塞收，结果丢 `outgoing` 或 `errorCh`）。

[handleSend 与 handleRecv（messenger.go:56-111）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/messenger/messenger.go#L56-L111) —— 这就是「读写解耦」：发和收各一个 goroutine，互不阻塞，外层 `Subscribe` 用 `select` 统一调度。

最后是反向拉文件：

[GetFile：Agent 反向拉文件内容（file.go:52-78）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/file.go#L52-L78) —— 从 context 取 UUID → `getFileContents`。

[getFileContents：UUID → Connection → Deployment → 文件（file.go:138-176）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/file.go#L138-L176) —— `connTracker.GetConnection(connKey)` 取连接，`nginxDeployments.Get(conn.ParentName)` 取 Deployment，`deployment.GetFile(filename, hash)` 按名字+hash 取内容。hash 不匹配返回空（让 Agent 知道文件版本不对）。

[GetFileStream：大文件分块流式（file.go:80-136）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/file.go#L80-L136) —— 用 NGINX Agent 提供的 `files.SendChunkedFile` 按 2MB（`defaultChunkSize`）分块发送，绕开单条消息 4MB 上限。

装配总览（把这些组件接到 Manager 上）：

[createAgentServices：装配 NginxUpdater + gRPC 服务（manager.go:303-341）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L303-L341) —— 第 309 行建 `resetConnChan`；第 310 行 `NewNginxUpdater`；第 318-322 行算 token audience（`<service>.<ns>.svc`）；第 324 行 `NewServer`，注册 `CommandService.Register` 和 `FileService.Register`；第 336 行以 `LeaderOrNonLeader` Runnable 注册进 Manager（**所有副本都跑** gRPC 服务，因为每个副本都要服务自己连上来的 Agent——这点和 telemetry 只在 leader 跑不同）。

#### 4.3.4 代码实践

**实践目标**：梳理一次配置更新从 `UpdateConfig` 到 Agent 收到请求的完整 gRPC 调用链，并标注每一步所在的文件:行号。

**操作步骤**：

按下表顺序，逐个打开源码点，在每一处旁注一句话说明「数据在这一步变成了什么形态」。这就是一条完整的「配置下发」调用链：

| 步骤 | 位置 | 这一步发生了什么 |
| --- | --- | --- |
| ① eventHandler 加锁 | [handler.go:318-320](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L318-L320) | `FileLock.Lock()`，事务开始 |
| ② 生成文件 | [handler.go:884](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L884) | `generator.Generate(conf)` → `[]File`（含内容） |
| ③ 调 UpdateConfig | [handler.go:885](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L885) | 进入 NginxUpdater |
| ④ 算版本 | [agent.go:93](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/agent.go#L93) → [deployment.go:272-317](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L272-L317) | `[]File` → `NginxAgentMessage`（**只含 FileOverviews 元数据**） |
| ⑤ 广播 | [agent.go:99](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/agent.go#L99) → [broadcast.go:118-139](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/broadcast/broadcast.go#L118-L139) | 消息进 `publishCh` |
| ⑥ 扇出 | [broadcast.go:188-220](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/broadcast/broadcast.go#L188-L220) | publisher 给每个订阅者的 `listenCh` 发消息 |
| ⑦ 订阅循环收到 | [command.go:208-230](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L208-L230) | `case msg := <-channels.ListenCh`，构造 `ManagementPlaneRequest` |
| ⑧ Messenger 发送 | [command.go:232](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L232) → [messenger.go:56-73](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/messenger/messenger.go#L56-L73) | `msgr.Send` → `server.Send`（**消息真正上 gRPC 流**） |
| ⑨ Agent 处理 | （数据面，外部） | Agent 收到 ConfigApplyRequest，发现缺文件 |
| ⑩ Agent 反向拉文件 | [file.go:55-78](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/file.go#L55-L78) | Agent 调 `GetFile`，控制面按 UUID→Deployment→文件返回内容 |
| ⑪ Agent 回复 | （数据面）→ [messenger.go:87-111](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/messenger/messenger.go#L87-L111) | `DataPlaneResponse` 经 `handleRecv` 进 `outgoing` channel |
| ⑫ 订阅循环处理回复 | [command.go:258-283](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L258-L283) | `case msg := <-msgr.Messages()`，成功则发 `responseCh` |
| ⑬ 广播者收到回复 | [broadcast.go:209-217](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/broadcast/broadcast.go#L209-L217) | 每个订阅者的 goroutine 解除阻塞 |
| ⑭ Send 返回 | [broadcast.go:222-229](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/broadcast/broadcast.go#L222-L229) → [agent.go:99-104](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/agent.go#L99-L104) | 所有 Pod 回复完，`Send` 返回，记错误 |
| ⑮ eventHandler 解锁 | [handler.go:320](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L320) | `FileLock.Unlock()`，事务结束，写 status |

**需要观察的现象**：注意步骤 ④ 是数据形态的关键转折点——`[]File`（含完整内容）变成了 `NginxAgentMessage`（**只有元数据**）。文件内容的真正传输发生在步骤 ⑩，且是 **Agent 主动反向拉取**，不是控制面推。

**预期结果**：你能用一句话描述这条链路：「控制面把文件**清单**经 Broadcaster→Subscribe→Messenger 推上 gRPC 流，Agent 收到后**反向**用 GetFile 按清单拉取文件**内容**，apply 后回复，广播者汇聚所有回复后才让 UpdateConfig 返回」。

**待本地验证**：可在 NGF 日志里依次搜索 `"Sent nginx configuration to agent"`（步骤⑧附近）、`"Sending configuration to agent"`（command.go 第 213 行）、`"Getting file for agent"`（file.go 第 168 行），按 correlation_id 串起来验证上述时序。

#### 4.3.5 小练习与答案

**练习 1**：`broadcaster.Send` 是同步阻塞的。如果一个 Deployment 有 3 个 Pod，其中 1 个 Pod 的 Agent 卡住不回复，会发生什么？

**参考答案**：`publisher` 里那个卡住订阅者的 goroutine 会卡在 [broadcast.go:209-217](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/broadcast.go#L209-L217) 等 `responseCh`；`wg.Wait()`（第 220 行）因此不返回；`Send` 一直阻塞；进而 `UpdateConfig` 阻塞；进而 eventHandler 持着 `FileLock` 不释放，整条事件管线卡住。但有两个兜底：每个订阅者的 select 同时监听 `channels.listenerCtx.Done()` 和 `b.broadcasterCtx.Done()`（第 201-204、210-213 行），ctx 取消时会解除阻塞；另外 Agent 侧 gRPC 有 keepalive（15s），断了会触发 `msgr.Errors()`，订阅循环会发 `responseCh` 并退出（[command.go:243-257](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L243-L257)）。所以最终会因连接超时/断开而解除，但确实会拖慢整批下发的延迟。

**练习 2**：为什么 `Subscribe` 在调 `broadcaster.Subscribe()` 和 `setInitialConfig()` 期间要持有 `FileLock.RLock()`？用一个具体的竞态场景说明。

**参考答案**：见 [command.go:156-170](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L156-L170) 的注释。竞态场景：新 Agent 调 `Subscribe()`，刚开始 `setInitialConfig()` 取「当前」配置；与此同时 eventHandler 在另一个 goroutine 里 `FileLock.Lock()` → `broadcaster.Send(新配置)` → 改 `deployment.files`。若不持锁，可能出现：新 Agent 从 `setInitialConfig` 拿到**旧**配置，然后才完成订阅，于是**错过**这次并发的新配置更新，导致配置漂移。横跨「订阅 + 取初始配置」加读锁，保证这两步对 `deployment.files` 的视图是一致的、且与 broadcaster 的扇出互斥。

---

## 5. 综合实践

**任务**：画出一张「一次 HTTPRoute 变更触发的配置下发」**完整时序图**，要求覆盖本讲全部三个模块，并标出跨模块的数据边界。

要求：

1. **左侧画控制面**，包含 4 个泳道：eventHandler、NginxUpdaterImpl、DeploymentBroadcaster、commandService（每个 Agent 一个实例）。
2. **右侧画数据面**，包含 1 个泳道：NGINX Agent。
3. **用不同颜色标注三类消息**：
   - 黑色实线：控制面内部的方法调用（如 `UpdateConfig` → `Send`）。
   - 蓝色实线：gRPC 调用（`server.Send` 推请求、`GetFile` 拉文件、`DataPlaneResponse` 回复）。
   - 红色虚线：`FileLock` 的加锁/解锁区间（标出从事务开始到结束整段持锁）。
4. **在图上标出「数据形态转折点」**：哪一步 `[]File` 变成了「只有元数据的 NginxAgentMessage」，哪一步又反向把内容传回去。
5. **额外标注**：在这条链路上，ConnectionsTracker 的 UUID 在哪两处被用到（CreateConnection 登记、GetFile 反查 Deployment）。

**验证方式**：把时序图和 4.3.4 的 15 步表格对照，每一步都应能在图上找到对应箭头。重点自查两个易错点：(a) 文件内容是不是画成了「Agent 主动拉」而非「控制面推」；(b) `Send` 的返回是不是画在了「所有 Agent 都回复之后」而非「消息发出之后」。

**待本地验证**：如果你有 kind 集群，可部署 NGF 后 apply 一个 HTTPRoute，用 `kubectl logs -f` 跟踪控制面日志，把真实日志时间线贴到时序图旁边对照。

## 6. 本讲小结

- **`NginxUpdater` 是配置下发的总入口**，但自己不碰网络：`UpdateConfig` 只做「算版本 → 广播元数据 → 记错误」三步，无变更（`configVersion` 不变）直接早退。
- **控制面是 gRPC 服务端，Agent 是客户端**：服务端注册 `CommandService`（推送主信道）和 `FileService`（反向拉文件），受 mTLS + TokenReview 双重保护，证书可热加载。
- **连接用 UUID 追踪**：`CreateConnection` 登记、`UpdateDataPlaneStatus` 补 InstanceID 后连接才 `Ready()`，`GetFile` 靠 UUID 反查所属 Deployment。
- **广播是「一对多 + 同步确认」**：`DeploymentBroadcaster.Send` 并行扇出给所有订阅 Agent，用 WaitGroup 等齐所有回复才返回，返回值表示是否有真实订阅者。
- **`Messenger` 把双向流解耦成 channel**：发和收各一个 goroutine，`Subscribe` 主循环用 `select` 统一调度「broadcaster 消息 / Agent 回复 / 错误 / 重连」。
- **整条链路靠 `FileLock` 串成原子事务**：从 eventHandler 加锁到所有 Agent 回复，文件视图保持一致，Agent 反向 `GetFile` 才不会读到错乱版本。

## 7. 下一步学习建议

- **[u7-l2 配置文件下发与 Deployment 管理](u7-l2-config-file-push-and-deployments.md)**：本讲聚焦「控制面如何把消息送上网」，下一讲聚焦「文件按 Deployment 维度如何组织、reload 命令如何下发、多 Pod 如何保证一致」。
- **延伸阅读 1**：`internal/controller/nginx/agent/grpc/context/context.go`——看 `GrpcInfo` 如何被注入和取出，理解拦截器与服务之间靠 context 传递身份的完整闭环。
- **延伸阅读 2**：`internal/controller/nginx/agent/command_test.go` 与 `agent_test.go`——NGF 用 counterfeiter 生成的 fake（`agentfakes`、`broadcastfakes`、`grpcfakes`）来单测这条复杂的异步链路，是学习「如何测试并发广播」的好范本。测试的断言能帮你确认本讲描述的行为。
- **延伸阅读 3**：NGINX Agent v3 的 MPI 协议定义（`github.com/nginx/agent/v3/api/grpc/mpi/v1`），对照本讲的 `ManagementPlaneRequest`/`DataPlaneResponse`/`ConfigApplyRequest`，理解协议层面的字段含义。
