# 配置文件下发与 Deployment 管理

## 1. 本讲目标

本讲承接 u7-l1（NginxUpdater 与 gRPC Agent 通信），回答一个更具体的问题：

> 控制面把 NGINX 配置「文件化」之后，这些文件到底是怎么**按数据面 Deployment 组织**、又怎么**一份不差地送到每一个数据面 Pod** 的？当有多个副本时，如何保证它们落到同一份配置？

学完本讲，你应当能够：

- 说清 `DeploymentStore` / `Deployment` / `DeploymentBroadcaster` 三者的职责边界，以及为什么配置是「按 Deployment 组织」而非「按 Pod 组织」。
- 复述「推文件清单（FileOverview）+ Agent 反向拉内容（GetFile）」这套混合传输模型的完整流程，包括基于 hash 的跳过和大文件分块。
- 理解 `commandService`（MPI gRPC `CommandService`）管理的连接生命周期：`CreateConnection → UpdateDataPlaneStatus → Subscribe`，并纠正一个常见误解——**控制面并不下发「reload NGINX」命令**，reload 是 Agent 在处理 `ConfigApplyRequest` 时自发完成的。
- 解释多 Pod 场景下「同一份配置一致下发」所依赖的三个机制：单一 `Deployment` 对象、`DeploymentBroadcaster` 的「等待全部响应」屏障、以及贯穿整条事务的 `FileLock`。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自 u7-l1 等前置讲义）：

- **控制面 = gRPC 服务端，数据面 Agent = gRPC 客户端**：NGINX Agent 主动连到控制面，控制面注册了两类 gRPC 服务——`CommandService`（主信道，长连接/订阅）与 `FileService`（反向拉文件）。两者都跑在 mTLS 之上。
- **`NginxUpdater.UpdateConfig` 是下发总入口**：它只做「存文件 → 广播元数据 → 记错误」三件事，配置生成与网络通信借此解耦。
- **配置的最终形态是 `[]agent.File`**：u6 讲过配置生成器把 `dataplane.Configuration` 渲染成一组文件（`http.conf`、`stream.conf`、各种密钥/bundle 等），本讲处理的正是这组文件的下发。
- **MPI 协议（Management Plane Interface）**：NGINX Agent v3 使用的 gRPC 协议，核心数据结构有 `FileOverview`/`File`/`FileMeta`、`ConfigApplyRequest`、`CommandResponse` 等。本讲会用到这些类型。

两个本讲会反复出现、需要先记住的术语：

- **Deployment（小写，本包内类型）**：指控制面内部用 `agent.Deployment` 结构体表示的「一个数据面 Deployment/工作负载」。它**不是** Kubernetes 的 `apps/v1.Deployment` 对象本身，而是控制面里「为这个工作负载维护的配置 + 广播器 + 每个 Pod 的状态」的聚合体。注意它也可能对应 DaemonSet（见 u12 相关内容），名字沿用「Deployment」是历史习惯。
- **configVersion**：对当前所有文件元数据（名字+hash）整体计算出的一个版本号。控制面靠它判断「这次配置和上次是否一样」，是减少无谓 reload 的关键。

## 3. 本讲源码地图

本讲围绕 `internal/controller/nginx/agent/` 下的三个文件展开，并辅以广播与连接跟踪两个支撑文件：

| 文件 | 作用 |
| --- | --- |
| [deployment.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go) | 定义 `Deployment`（单数据面工作负载的配置聚合体）与 `DeploymentStore`（按 `types.NamespacedName` 索引所有 `Deployment`）。负责存文件、算 configVersion、登记每个 Pod 的错误状态。 |
| [file.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/file.go) | 实现 gRPC `FileService`：`GetFile`/`GetFileStream` 让 Agent 反向拉取文件内容；`UpdateOverview` 收集 Agent 上报的「被引用文件」清单用于标记用户挂载文件为 unmanaged。 |
| [command.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go) | 实现 gRPC `CommandService`：连接握手（`CreateConnection`/`UpdateDataPlaneStatus`）与核心的 `Subscribe` 事件循环（接收广播、转发给 Agent、等待响应、回写状态）。 |
| [broadcast/broadcast.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/broadcast/broadcast.go) | `DeploymentBroadcaster`：把一次配置更新**同步扇出**给该 Deployment 下所有订阅者，并「等待全部响应」后才返回。 |
| [grpc/connections.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/connections.go) | `ConnectionsTracker`：按 gRPC 连接 UUID 记录每条连接归属哪个 Deployment、nginx InstanceID 是否已上报。 |

调用关系（自上而下）：

```
event handler（持 FileLock）
   └─ NginxUpdater.UpdateConfig(deployment.go 的调用方)
        ├─ Deployment.SetFiles   → rebuildFileOverviews → 得到 msg（或 nil）
        └─ DeploymentBroadcaster.Send(msg)   ── 同步扇出 ──┐
                                                            ▼
            每个 Pod 的 commandService.Subscribe 事件循环（收到 ListenCh）
                  └─ msgr.Send(ConfigApplyRequest)  → Agent
                                                           │
            Agent 反向调用 ←─────────────────────────────  │
            fileService.GetFile / GetFileStream            │
                  └─ Deployment.GetFile(读 files 切片)      │
                                                           ▼
            Agent 应用配置（reload NGINX）→ DataPlaneResponse
                  └─ Subscribe 收到响应 → 写 Pod 错误状态 → 响 ResponseCh
                                                            │
            DeploymentBroadcaster 收齐所有 ResponseCh ←─────┘
   └─ handler 释放 FileLock，读取 GetConfigurationStatus → 入队 status
```

## 4. 核心概念与源码讲解

### 4.1 Deployment 文件管理：一份配置服务一个数据面 Deployment

#### 4.1.1 概念说明

数据面可能是多副本的（一个 Deployment 起多个 NGINX Pod）。如果控制面「每个 Pod 各存一份配置」，那么：

- 同一份配置要复制 N 份内存；
- 下发时要分别通知 N 个 Pod，难以保证它们同时落同一版本；
- 新加副本时要单独补发。

NGF 的做法是**按工作负载（Deployment/DaemonSet）组织，而非按 Pod 组织**：控制面内部为每个数据面工作负载维护**唯一一个 `Deployment` 对象**，里面放着「该工作负载当前的权威文件集 + 一个广播器 + 每个 Pod 的最近一次错误」。所有副本共享这一个对象：

- 文件只存一份（`Deployment.files`），副本们都从这一份里取内容；
- 下发只广播一次，由广播器扇出给所有订阅中的副本；
- 新副本上线时，从这同一个对象取「当前最新配置」做初始化。

这样「多副本一致性」就被收敛成「单一数据源」问题。

#### 4.1.2 核心流程

一次配置更新在「Deployment 文件管理」这一层只做三步：

1. **存文件 + 重算 configVersion**：`SetFiles(files, volumeMounts)` 把新文件集写入 `Deployment.files`，调用 `rebuildFileOverviews` 重建文件清单并计算新的 configVersion。
2. **判同省发**：若新 configVersion 与旧值相等，说明文件没变，`rebuildFileOverviews` 直接返回 `nil`，上层据此跳过广播（即「零 reload」）。
3. **若变了，返回一条广播消息**：包含 `FileOverviews`（只有元数据，无内容）与新 configVersion，交给广播器扇出。

configVersion 的语义可形式化理解为：把当前所有受管文件的「(名字, hash)」集合压成一个稳定哈希。即令文件集为

\[
F = \{(name_i, hash_i)\}_{i=1}^{n}
\]

则

\[
\text{configVersion} = H\big(\,\text{sorted}(F)\,\big)
\]

只要任意文件的内容（hash）或文件集合本身发生变化，configVersion 就会变；内容完全相同则不变——这正是「内容驱动版本号」省去无谓 reload 的依据。

#### 4.1.3 源码精读

**`Deployment` 结构体**——一个数据面工作负载的全部配置状态：

[deployment.go:L38-L71](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L38-L71) 定义了这个聚合体。要点：

- `files []File`：权威文件集，所有副本共享这一份。
- `fileOverviews []*pb.File`：由 `files` 派生的「元数据清单」，广播时只发它（不发内容）。
- `configVersion string`：当前版本号，判同用。
- `broadcaster`：负责把更新扇出给所有订阅副本。
- `podStatuses map[string]error`：**每个 Pod** 的最近一次配置错误（key 是连接 UUID）。这是「按 Deployment 聚合、按 Pod 跟踪错误」的设计。
- `latestConfigError` / `latestUpstreamError`：在整轮更新事件里保住「前面那一次错误」不被后续成功覆盖，供最终写状态用。
- `FileLock sync.RWMutex` 与 `errLock sync.RWMutex`：两把锁分工——`FileLock` 保护文件/版本相关的整个事务，`errLock` 保护错误状态字段。

注意文件中的醒目注释 [deployment.go:L166-L172](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L166-L172)：`GetFile`/`SetFiles`/`GetFileOverviews` 等函数**自身不加锁**，要求调用方**必须已经持有 `FileLock`**。这是本讲「多 Pod 一致性」的关键伏笔。

**存文件并重算版本**：

[deployment.go:L204-L209](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L204-L209) 是 `SetFiles`，仅两行业务逻辑：写入 `files` 与 `volumeMounts`，再调 `rebuildFileOverviews`。

[deployment.go:L272-L317](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L272-L317) 是核心的 `rebuildFileOverviews`，做了三件事：

1. 把每个 `File` 的元数据收集成 `fileOverviews`；
2. 把「不该被 Agent 碰」的文件（静态文件 + 用户挂载卷里的文件）追加为 `Unmanaged: true` 的占位项，告诉 Agent「这些别动」；
3. 用 `filesHelper.GenerateConfigVersion(fileOverviews)` 算新版本号；若与旧值相等就返回 `nil`（没变化、不发），否则更新并返回广播消息。

其中「不该被碰」的静态文件清单见 [deployment.go:L22-L31](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L22-L31)（`nginx.conf`、`mime.types`、dashboard html 等），用户挂载文件则由 `UpdateOverview`（见 4.2）动态收集。

**读单个文件（给 Agent 拉内容用）**：

[deployment.go:L188-L200](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L188-L200) 是 `GetFile(name, hash)`。它的精妙之处在返回值：找到同名文件后，**若请求的 hash 与现有 hash 相同，就返回 `nil`**——意思是「你 Agent 已经有这一版了，不用再传内容」。这是「按需传输」的开关。

**聚合每个 Pod 的错误**：

[deployment.go:L150-L164](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L150-L164) 的 `GetConfigurationStatus` 把 `podStatuses` 里所有 Pod 的错误 `errors.Join` 成一个，作为「这个 Deployment 整体配置健康度」交给上层写 Gateway 状态——只要有一个 Pod 失败，整体状态就带错。

**`DeploymentStore`：按名字索引**：

[deployment.go:L321-L326](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L321-L326) 定义接口 `DeploymentStorer`（`Get`/`GetOrStore`/`Remove`）；[deployment.go:L329-L339](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L329-L339) 是其实现，底层是 `sync.Map`，key 是数据面工作负载的 `types.NamespacedName`。

[deployment.go:L358-L371](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L358-L371) 的 `GetOrStore` 体现了「单一数据源」：先查，命中就返回已有对象；不命中才新建一个带「全新广播器」的 `Deployment`。也就是说，**同一个工作负载永远只对应一个 `Deployment` 对象与一个广播器**。

#### 4.1.4 代码实践

**实践目标**：亲手验证「configVersion 不变 → 不广播」这条省 reload 的关键路径，并理解「unmanaged」机制。

**操作步骤**（源码阅读型实践）：

1. 打开 [deployment.go:L272-L317](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L272-L317)，定位 `if d.configVersion == newConfigVersion { return nil }`。
2. 顺藤摸瓜找到它的调用方 `UpdateConfig`（[agent.go:L88-L105](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/agent.go#L88-L105)），确认 `msg == nil` 时只打一条日志就 return，不会调 `broadcaster.Send`。
3. 再看 `ignoreFiles`（[deployment.go:L22-L31](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L22-L31)），思考：为什么要把 `nginx.conf` 这类文件标记为 unmanaged？如果不标记，Agent 在写配置目录时可能会怎样对待它？

**需要观察的现象 / 预期结果**：

- 当生成器产出的文件集与上次逐字节一致时，`rebuildFileOverviews` 返回 `nil`，`UpdateConfig` 不再向 Agent 发任何 RPC——这就是「内容没变就零 reload」。
- 不标记 unmanaged 的话，Agent 可能在「对齐期望状态」时删除/覆盖镜像内置的 `nginx.conf` 骨架，导致 NGINX 无法启动。标记后，Agent 对这些文件只读不写。

> 待本地验证：可构造两次内容相同的 `SetFiles` 调用（例如写一个最小单测，复用 `StoreWithBroadcaster` 注入 mock 广播器），断言第二次 `SetFiles` 返回 `nil`、广播器的 `Send` 只被调用一次。

#### 4.1.5 小练习与答案

**练习 1**：`Deployment` 里有 `podStatuses map[string]error` 又有 `latestConfigError`，二者为何都要存在？只用一个会出什么问题？

**参考答案**：`podStatuses` 是**每个 Pod**的粒度，用于聚合出整个 Deployment 的健康状态（`GetConfigurationStatus`）；`latestConfigError` 是**整轮更新事件**的粒度。注释 [deployment.go:L51-L60](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L51-L60) 指出：一轮事件里可能先做配置下发、再做 Plus upstream API 更新，若只用 `podStatuses`，后续成功的 API 调用会覆盖前面的失败错误，导致写状态时丢错。`latestConfigError` 把「这次配置下发」的错误冻结住，保证最终状态如实反映。

**练习 2**：`GetOrStore` 为什么用「先 Get 再 Store」而不是直接 `sync.Map.LoadOrStore`？

**参考答案**：因为它在 miss 时要**新建一个带全新 `DeploymentBroadcaster` 的对象**（[deployment.go:L367](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L367)，`broadcast.NewDeploymentBroadcaster(ctx)` 会启动两个常驻 goroutine）。直接用 `LoadOrStore` 会在并发 miss 时创建多个广播器再丢弃，泄漏 goroutine。「先查后建」配合 `sync.Map` 降低了竞态概率；命中即复用，保证一工作负载一广播器。

### 4.2 文件传输流程：推清单、拉内容（GetFile）

#### 4.2.1 概念说明

NGF 没有采用「控制面把每个文件的完整内容一次性推给 Agent」的朴素做法，而是用了一套**混合传输模型**：

- **推（Push）**：控制面在 `Subscribe` 长连接上推一条 `ConfigApplyRequest`，里面只带**文件清单**（`FileOverview`：名字、hash、大小、权限，**不含内容**）。
- **拉（Pull）**：Agent 拿到清单后，对每个需要的文件**反向调用**控制面的 `FileService.GetFile`，按需拉取内容。

为什么这么设计？

1. **按需传输**：`GetFile` 的 hash 比对让「Agent 已有的文件」直接跳过，只传真正变化的文件，省带宽、省时间。
2. **解耦两条信道**：「下发指令」走 `CommandService` 长连接（一条 per Pod），「传大块内容」走 `FileService` 的独立 RPC，互不阻塞。
3. **大文件友好**：超过阈值的文件可走 `GetFileStream` 分块流式传输，避免单条 gRPC 消息过大。

#### 4.2.2 核心流程

```
控制面 broadcaster.Send(ConfigApplyRequest{FileOverviews, configVersion})
        │  （只含元数据）
        ▼
Agent 收到清单，逐文件判断：
   for each File in FileOverviews:
       if 本地已有同名且同 hash 文件:  跳过（不拉）
       else if 文件较大:              调 GetFileStream 分块拉
       else:                          调 GetFile 一次性拉
        │
        ▼
控制面 fileService.GetFile / GetFileStream
   → getFileContents → Deployment.GetFile(返回内容 或 nil 表示 hash 命中)
        │
        ▼
Agent 写盘 → reload NGINX → 回 DataPlaneResponse(COMMAND_STATUS_OK / ERROR)
```

关键点：控制面侧的 `Deployment.GetFile` 在 hash 命中时返回 `nil`，相当于一道**服务端二次确认**——即便 Agent 误判需要拉，控制面也会用「我这边这版 hash 就是你给的那个」来省掉不必要的内容传输。

分块大小的常量见 [file.go:L19](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/file.go#L19)：`defaultChunkSize = 2097152`（2 MiB）。一个大小为 `S` 的文件需要

\[
\text{chunks} = \left\lceil \frac{S}{\text{chunkSize}} \right\rceil
\]

个数据块，由 [file.go:L178-L186](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/file.go#L178-L186) 的 `calculateChunks` 计算。

#### 4.2.3 源码精读

**`File` 与 `fileService`**：

[file.go:L23-L26](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/file.go#L23-L26) 定义 `File{Meta *pb.FileMeta; Contents []byte}`——内存里既存元数据也存内容，但**广播只发 Meta、GetFile 才发 Contents**。

[file.go:L29-L34](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/file.go#L29-L34) 的 `fileService` 持有 `nginxDeployments *DeploymentStore` 与 `connTracker`，靠后者把「这条 gRPC 连接」定位到「它属于哪个 Deployment」。

**一次性拉取 `GetFile`**：

[file.go:L55-L78](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/file.go#L55-L78) 先校验连接上下文与请求合法性，核心委托给 `getFileContents`。

[file.go:L138-L176](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/file.go#L138-L176) 的 `getFileContents` 是「连接 → Deployment → 文件」的三级解析：

1. `connTracker.GetConnection(connKey)` 拿到连接信息，校验 `InstanceID` 非空（连接已就绪）；
2. `nginxDeployments.Get(conn.ParentName)` 拿到该连接归属的 `Deployment`；
3. `deployment.GetFile(filename, hash)` 取内容；若返回空，按「hash 不匹配」或「未找到」打日志并返回 `codes.NotFound`。

注意 [file.go:L52-L54](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/file.go#L52-L54) 的注释：调用 `GetFile` 时 `Deployment` 对象**已经被锁住**（FileLock），因为整个事务在 handler 里被锁包裹（见 4.3）。这就是为什么 `Deployment.GetFile` 自身不加锁却安全。

**分块流式 `GetFileStream`**：

[file.go:L83-L136](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/file.go#L83-L136) 先同样取内容，再委托给上游 SDK 的 `files.SendChunkedFile`：先发一个 `FileDataChunk_Header`（声明 `ChunkSize`、总块数 `Chunks`、文件 Meta），再用 `bytes.NewReader` 把内容按 2 MiB 一块推出去。文件超大（超过 `uint32` 上限）会直接报 `codes.Internal`。

**反向收集「被引用文件」：`UpdateOverview`**：

[file.go:L198-L229](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/file.go#L198-L229) 是一个容易被忽略但很重要的回调。Agent 启动或文件变化时会把自己的「当前文件清单」上报给控制面；NGF 虽然不允许 Agent 自己改 NGINX 配置（所以 `UpdateFile` 是 no-op，见 [file.go:L233-L235](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/file.go#L233-L235)），但**借用这次上报拿到「NGINX 实际引用了哪些文件」**，存进 `deployment.latestFileNames`。`rebuildFileOverviews` 随后用它把「用户挂载卷里、被 NGINX 引用的文件」标记为 unmanaged，避免 Agent 误删用户挂进来的证书/配置。

#### 4.2.4 代码实践

**实践目标**：把 hash 跳过与分块这两个传输细节走一遍，能口算分块数。

**操作步骤**（源码阅读 + 推算型实践）：

1. 打开 [deployment.go:L188-L200](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L188-L200)，确认：当 `hash == file.Meta.GetHash()` 时返回 `nil`（内容为空）。
2. 跟到 [file.go:L150-L166](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/file.go#L150-L166)，确认 `len(contents) == 0` 时控制面返回 `codes.NotFound`——但注意此时 `fileFoundHash != ""`，日志会区分「未找到」与「hash 不匹配」两种情况。
3. 用 [file.go:L178-L186](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/file.go#L178-L186) 的公式口算：一个 5 MiB 的 bundle 文件，按 `chunkSize = 2 MiB`，需要几块？

**预期结果**：

- hash 命中 → 内容不传（GetFile 返回空 → 控制面回 NotFound，Agent 据此知道「不用换」）。这条路径是「只改了一个小文件时，其余几十个文件都不重传」的关键。
- 5 MiB ÷ 2 MiB：5 = 2×2 + 1，余数 1 > 0，所以 `divide + 1 = 3` 块。即 `chunks = 3`。

> 待本地验证：可在 `calculateChunks` 的单测里加入 `fileSize=5*1024*1024` 用例，断言结果为 3。

#### 4.2.5 小练习与答案

**练习 1**：为什么「推清单 + 拉内容」比「控制面直接把全部文件内容塞进 ConfigApplyRequest」更好？

**参考答案**：① 清单小，主信道（`Subscribe` 长连接）不会被大块内容阻塞，指令下发更及时；② hash 比对让未变文件零传输，多副本、高频小改场景下显著省带宽；③ 大文件可走 `GetFileStream` 分块，规避单条 gRPC 消息过大；④ 内容传输走独立 `FileService`，与指令信道故障隔离。

**练习 2**：`UpdateFile` 和 `UpdateFileStream` 为什么是 no-op？NGF 却认真处理 `UpdateOverview`，矛盾吗？

**参考答案**：不矛盾。NGF 的设计原则是「NGINX 配置只能由控制面单向管理」，所以 Agent 主动改文件（`UpdateFile`）必须被忽略。但 `UpdateOverview` 不是「改配置」，而是 Agent 把「我这边当前的文件清单」上报——NGF 借此得到 `latestFileNames`，用于把用户挂载卷里被引用的文件标为 unmanaged，防止后续下发时误删。前者是「禁止反向写」，后者是「利用反向只读信息」，二者一致。

### 4.3 command 服务与 reload：连接生命周期与事件循环

#### 4.3.1 概念说明

先纠正一个常见误解：**「reload NGINX」并不是控制面下发的一条命令**。控制面注册的 `CommandService` 是 MPI 协议里负责**连接与订阅管理**的 gRPC 服务（创建连接、上报状态、维护双向流），它的名字里的「Command」指的是协议层的服务名，而不是「给 NGINX 下 reload 指令」。真正的 reload 发生在：控制面在 `Subscribe` 流上推一条 `ConfigApplyRequest`（文件清单 + configVersion）→ Agent 反向拉文件、写盘 → **Agent 自己** reload NGINX → 回一条 `CommandResponse`。控制面只负责「把正确的文件以正确的版本送到」，reload 是 Agent 端 `ConfigApply` 的副作用。

> 小提示：NGINX **Plus** 有一条「不 reload」的路径——通过 NGINX Plus API 动态更新 upstream（`UpdateUpstreamServers`，走 `APIRequest`）。那是 u12-l2 的主题，本讲聚焦 OSS 也适用的「文件 + reload」主路径。

`commandService` 管理的连接生命周期分三步：

1. **`CreateConnection`**：Agent 刚连上时调用，控制面从 Agent 上报的标签里解析出它归属的「工作负载名 + 类型」，登记到 `ConnectionsTracker`。此时 nginx InstanceID 可能还没拿到。
2. **`UpdateDataPlaneStatus`**：Agent 发现自己的 nginx 实例后再调用，补上 InstanceID。`Connection.Ready()` 即「InstanceID 非空」。
3. **`Subscribe`**：核心双向流。每个 Pod 一条，贯穿该 Pod 的整个生命周期：等连接就绪 → 订阅广播器 → 应用初始配置 → 进入「收广播 → 转发 Agent → 等响应 → 回写状态」的事件循环。

#### 4.3.2 核心流程

`Subscribe` 的整体时序（[command.go:L130-L285](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L130-L285)）：

```
1. 从 ctx 取 gRPC 身份；defer 注销连接
2. waitForConnection: 每 1s 轮询，最多等 30s，直到 Connection.Ready() 且 Deployment 在 store 中
3. FileLock.RLock()                        ← 关键：跨「订阅 + 初始配置」加读锁
4.   broadcaster.Subscribe()               ← 拿到本 Pod 的 ListenCh / ResponseCh
5.   setInitialConfig():                   ← 给新 Pod 补发当前最新配置
       - 校验镜像版本
       - GetFileOverviews() 取当前清单
       - msgr.Send(ConfigApplyRequest)
       - waitForInitialConfigApply() 等响应
       - （Plus）补发 NGINX Plus API actions
       - logAndSendErrorStatus() 把结果入 statusQueue
6. FileLock.RUnlock()
7. 事件循环 for-select:
   - ListenCh 收到广播 msg:
       ConfigApplyRequest → buildRequest → msgr.Send 给 Agent
   - msgr.Messages() 收到 Agent 响应:
       OK → SetPodErrorStatus(nil)；否则记错
       若是广播请求 → 向 ResponseCh 发信号（解阻塞 broadcaster）
   - msgr.Errors(): 连接错误 → 记错、解阻塞、返回（Agent 会重连重订阅）
   - ctx.Done() / resetConnChan: 退出
```

其中第 3–6 步的「跨订阅与初始配置加读锁」是专门为消除一个竞态而设计的，源码注释讲得很清楚（见 4.3.3）。

#### 4.3.3 源码精读

**连接握手**：

[command.go:L72-L118](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L72-L118) 的 `CreateConnection` 从 Agent 标签里解析 `ParentName`（工作负载 `ns/name`）与 `ParentType`（Deployment/DaemonSet），连同 nginx InstanceID 一起 `connTracker.Track`。解析失败（标签缺失）直接回 `COMMAND_STATUS_ERROR`，Agent 拿到错误自知要重试。

[command.go:L572-L593](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L572-L593) 的 `UpdateDataPlaneStatus` 在 Agent 发现 nginx 实例后补上 InstanceID（`connTracker.SetInstanceID`）——这是连接从「未就绪」翻「就绪」的转折点（[connections.go:L31-L33](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/connections.go#L31-L33) 的 `Ready()`）。

**等待连接就绪**：

[command.go:L287-L319](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L287-L319) 的 `waitForConnection` 用「1 秒 ticker + 30 秒 timer」轮询两个条件：连接 Ready、且对应 Deployment 已进入 store。这覆盖了「Agent 先连上、控制面还没建好 Deployment 对象」的时序错位。

**消除竞态的关键注释**：

[command.go:L156-L187](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L156-L187) 是本讲最值得精读的一段注释。它描述的竞态是：

1. 新 Agent 调 `Subscribe`，开始 `setInitialConfig`；
2. 与此同时，事件处理器调 `broadcaster.Send` 推一次配置更新（发送方在整个广播事务里持有写锁）；
3. 新 Agent 从 `setInitialConfig` 拿到**旧配置**，然后才订阅；
4. 结果新 Agent 错过了第 2 步那次更新，**配置漂移**。

解法：把「订阅」与「初始配置」**包在同一次锁里**（这里是 `RLock`，因为 `setInitialConfig` 只读文件、广播发送方持写锁会互斥等待）。这样要么新订阅者先拿到锁、订阅并应用最新配置、再处理之后的更新；要么发送方先拿锁完成更新，新订阅者随后在 `setInitialConfig` 里直接拿到这份最新配置。两种顺序都不会漏更新。

**初始配置**：

[command.go:L323-L401](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L323-L401) 的 `setInitialConfig`：先 `validatePodImageVersion` 防止「镜像版本与 Deployment 期望不符」（滚动升级中新旧 Pod 共存时很重要），再 `GetFileOverviews` 取当前清单，`msgr.Send` 发出 `ConfigApplyRequest`，`waitForInitialConfigApply` 等响应；Plus 场景还会带 30 秒重试地把 NGINX Plus API actions 补发一遍（reload 后 nginx 可能还没完全就绪）。最后 `logAndSendErrorStatus` 把成功/失败写入 Pod 状态并入队。

**事件循环（广播消费）**：

[command.go:L191-L284](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L191-L284) 是常驻 `for-select`。`ListenCh` 收到广播后，按 `msg.Type` 构造请求（`ConfigApplyRequest` 或 Plus 的 `APIRequest`）经 `msgr.Send` 发给 Agent；`msgr.Messages()` 收到响应后写 Pod 错误状态，并——**仅对广播请求**——向 `ResponseCh` 发信号。`pendingBroadcastRequest` 这个标志位用于区分「广播请求的响应」与「初始配置的响应」，避免初始配置的成功响应被误当作广播完成信号（[command.go:L271-L282](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L271-L282)）。

**构造请求**：

[command.go:L470-L489](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L470-L489) 的 `buildRequest` 把 `FileOverviews` + `configVersion` 打包成 `ConfigApplyRequest`，每条请求带新生成的 `MessageId`/`CorrelationId`，便于日志与 Agent 侧关联。

**状态回写**：

[command.go:L441-L468](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L441-L468) 的 `logAndSendErrorStatus` 把单个 Pod 的错误汇入 `Deployment`（`SetPodErrorStatus`），再用 `GetConfigurationStatus()` 取「全 Deployment 聚合错误」入 `statusQueue`，且置 `NginxConfigPushed: true`——后者是 u4-l3 提到的「真下发」标记。

**广播器的「等待全部响应」屏障**：

[command.go:L191-L198](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L191-L198) 注释明确：收到 `ListenCh` 消息时 Deployment 必须已被锁住，且锁要持有到「收到 Agent 响应并回 `ResponseCh`、返回 handler 释放锁」为止。

`ResponseCh` 信号被谁消费？看广播器：[broadcast.go:L120-L139](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/broadcast/broadcast.go#L120-L139) 的 `Send` 把消息投进 `publishCh` 后，**阻塞等 `doneCh`**；[broadcast.go:L180-L232](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/broadcast.go#L180-L232) 的 `publisher` goroutine 收到消息后，**快照当前所有订阅者**，用 `sync.WaitGroup` 给每个订阅者的 `listenCh` 投递消息，并**逐一等它们在 `responseCh` 上回应**，全部回完才向 `doneCh` 发信号。也就是说：**`broadcaster.Send` 返回 ⟺ 该 Deployment 下所有 Pod 都已收到并处理完这次配置**。这就是多 Pod 一致性的核心屏障。

#### 4.3.4 代码实践

**实践目标**：用源码注释自证「为什么订阅与初始配置必须包在同一次锁里」，并理解 `ResponseCh` 在广播屏障里的角色。

**操作步骤**（源码阅读型实践）：

1. 精读 [command.go:L158-L170](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L158-L170) 的竞态注释，用你自己的话把「4 步竞态」复述一遍。
2. 找到 `RLock` 的获取与释放点（[command.go:L171](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L171) 与 [command.go:L186](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L186)），确认 `broadcaster.Subscribe()` 与 `setInitialConfig()` 都在锁内。
3. 追 `ResponseCh`：从 [command.go:L275](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L275)（订阅者发信号）到 [broadcast.go:L214](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/broadcast/broadcast.go#L214)（publisher 收信号），画出「广播器等所有订阅者回应」的闭环。

**需要观察的现象 / 预期结果**：

- 若不加这把锁：一个刚启动的 Pod 可能在「拿到旧初始配置」与「订阅完成」之间，错过一次并发下发的更新，导致它与其他 Pod 配置不一致。
- `ResponseCh` 使 `Send` 成为同步屏障：handler 在 `FileLock` 内调 `Send` 时，会一直阻塞到该 Deployment **所有** Pod 都回完响应，才继续。这是「一次更新对全部副本原子可见」的实现基础。

> 待本地验证：阅读 `command_test.go`（同目录）中针对 `Subscribe` 的用例，找出模拟「订阅期间发生并发广播」的测试，看断言如何验证 Pod 最终配置一致。

#### 4.3.5 小练习与答案

**练习 1**：控制面从未调用过一个叫 `Reload` 的 RPC。那么 NGINX 到底是怎么被触发 reload 的？

**参考答案**：控制面在 `Subscribe` 流上发的是 `ConfigApplyRequest`（文件清单 + configVersion）。Agent 收到后，反向用 `GetFile`/`GetFileStream` 拉取变化的文件、写盘，然后**由 Agent 自身**执行 nginx reload 作为「应用这份配置」的步骤，完成后回 `CommandResponse`。控制面只对「文件与版本」负责，reload 是 Agent 端 ConfigApply 的副作用——这正是控制面/数据面解耦的体现。

**练习 2**：`pendingBroadcastRequest` 这个局部变量为什么必须区分「广播请求」和「初始配置请求」的响应？不区分会怎样？

**参考答案**：因为 `setInitialConfig` 也会经 `msgr` 发请求并收到响应，这些响应会进同一个 `msgr.Messages()`。若不区分，初始配置的成功响应会被误当成「一次广播完成」去触发 `ResponseCh`，导致广播器以为某个订阅者已完成、提前放行 `Send`，破坏「等待全部响应」的屏障；更糟的是会让 broadcaster 在 Pod 还没真正应用配置时就认为下发完成。`pendingBroadcastRequest` 确保**只有广播请求的响应**才 signaling `ResponseCh`（[command.go:L271-L282](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L271-L282)）。

**练习 3**：`waitForConnection` 为什么要同时等「连接 Ready」**和**「Deployment 在 store 中」两个条件？

**参考答案**：两者就绪的时序并不固定。Agent 可能先完成连接握手（`CreateConnection`+`UpdateDataStatus` 让 `Ready()` 为真），而控制面这边为该工作负载创建 `Deployment` 对象（`GetOrStore`）可能稍晚（要等事件处理器跑完一轮）。只等 `Ready()` 就去取 Deployment 会拿到 nil；两个条件都满足才能保证后续 `broadcaster.Subscribe()` 与 `GetFileOverviews()` 有对象可操作（[command.go:L307-L315](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L307-L315)）。

## 5. 综合实践

**任务**：用本讲三个最小模块的知识，完整解释「当一个数据面 Deployment 有 3 个 Pod 时，一次配置更新如何保证 3 个 Pod 一致地落到同一份配置」。请按下列子问题逐条作答，并给出对应源码位置作为依据。

1. **单一数据源**：3 个 Pod 共享的是哪一份对象？为什么不会出现「每个 Pod 一份配置」？
   - 提示：从 [deployment.go:L358-L371](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L358-L371) 的 `GetOrStore` 与 [deployment.go:L38-L71](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L38-L71) 的 `Deployment` 结构切入。

2. **同一份消息**：3 个 Pod 收到的 `ConfigApplyRequest` 是否完全相同（含 configVersion）？扇出发生在哪里？
   - 提示：[broadcast.go:L180-L232](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/broadcast/broadcast.go#L180-L232) 的 `publisher` 把同一条 `msg` 发给每个订阅者的 `listenCh`。

3. **同步屏障**：控制面什么时候才认为「这次下发完成」？若有 1 个 Pod 处理慢、另 2 个已回完，会发生什么？
   - 提示：`Send` 阻塞到 `doneCh`，而 `doneCh` 要等 `WaitGroup` 内**所有**订阅者的 `responseCh` 都回信号（[broadcast.go:L197-L220](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/broadcast/broadcast.go#L197-L220)）。慢的那个 Pod 会让整次 `Send` 一直等到它回完。

4. **事务不撕裂**：在等待这 3 个 Pod 响应期间，若有新的一轮配置变更到来，会不会把文件改掉、导致某个 Pod 拉到「半新半旧」的内容？
   - 提示：handler 在 [handler.go:L318-L320](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L318-L320) 用 `deployment.FileLock.Lock()/Unlock()` 把 `updateNginxConf` 整段包住；而 `broadcaster.Send` 正是在这把写锁内被调用、并阻塞到全部 Pod 响应。期间 `Deployment.GetFile`（[deployment.go:L188-L200](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L188-L200)）读到的文件集不会被下一次 `SetFiles` 改动，因为下一次 `SetFiles` 必须等写锁释放。

5. **新加入的 Pod**：如果在下发过程中扩容到 4 个 Pod，新 Pod 如何保证不漏掉这次更新？
   - 提示：[command.go:L156-L187](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/command.go#L156-L187) 的「订阅 + 初始配置同锁」设计。

**预期产出**：一张时序图或一段文字说明，串联「handler 持写锁 → SetFiles 存文件 + 算 configVersion → broadcaster.Send 同步扇出 → 3 个 Pod 各自 Subscribe 收到同一 msg → 各自发 ConfigApplyRequest → Agent 反向 GetFile 拉同一份内容 → 各自 reload → 各自回 ResponseCh → 广播器收齐 3 个响应 → Send 返回 → handler 解锁、写聚合状态」。能讲清「为什么是 3 个 Pod 落到同一份配置」即达标。

> 待本地验证：上述「3 个 Pod」的并发行为可在 `internal/controller/nginx/agent/` 的集成测试（envtest 或 fake 广播器）中通过「注入 N 个 mock 订阅者、断言全部收到同一 configVersion、且 `Send` 在最后一个回响应前未返回」来验证。

## 6. 本讲小结

- **按 Deployment 组织，而非按 Pod**：`DeploymentStore` 以工作负载名为 key，每个数据面工作负载对应**唯一一个** `Deployment` 对象（含一份权威文件集 + 一个广播器 + 每 Pod 错误表），多副本共享单一数据源。
- **内容驱动的 configVersion**：`rebuildFileOverviews` 对全量文件元数据算版本号；不变则返回 `nil`，`UpdateConfig` 据此零广播、零 reload。
- **推清单 + 拉内容**：`ConfigApplyRequest` 只带元数据，Agent 用 `FileService.GetFile`/`GetFileStream` 反向拉内容；hash 命中跳过，大文件 2 MiB 分块，省带宽且不阻塞指令信道。
- **「CommandService」不是 reload 命令**：它管连接与订阅生命周期（`CreateConnection → UpdateDataPlaneStatus → Subscribe`）；reload 是 Agent 在处理 `ConfigApplyRequest` 时自发完成的，控制面只对「文件 + 版本」负责。
- **多 Pod 一致性三件套**：① 单一 `Deployment` 对象保证同源；② `DeploymentBroadcaster.Send` 是「等待全部响应」的同步屏障；③ handler 贯穿整条事务的 `FileLock` 保证下发期间文件集不撕裂，且新 Pod 的「订阅 + 初始配置」同锁以防漏更新。
- **状态回写**：每个 Pod 的错误经 `SetPodErrorStatus` 汇入 `Deployment`，`GetConfigurationStatus` 聚合后入 `statusQueue`，并带 `NginxConfigPushed: true` 标记真下发。

## 7. 下一步学习建议

- **接 u8-l1（状态与条件）**：本讲反复出现的 `statusQueue` / `QueueObject` / `NginxConfigPushed` 正是状态更新子系统的输入，下一讲会讲它如何异步写回 Gateway 资源的 Conditions。
- **接 u9-l1（Provisioner）**：本讲的 `Deployment` 对象由 `GetOrStore` 按 `gw.DeploymentName` 创建，而这些「每个 Gateway 一个数据面工作负载」正是 Provisioner 动态创建/回收的，建议连起来读 `internal/controller/provisioner/`。
- **NGINX Plus 的「不 reload」路径**：本讲提到 `UpdateUpstreamServers` 走 `APIRequest` 动态更新 upstream，详见 u12-l2，可与本讲的「文件 + reload」主路径对照。
- **动手建议**：在 `internal/controller/nginx/agent/` 目录下读 `command_test.go` / `file_test.go` / `deployment_test.go`，找出模拟「多订阅者并发」「hash 命中跳过」「configVersion 不变不广播」的用例，用测试断言印证本讲的结论。
