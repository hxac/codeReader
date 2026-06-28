# EventHandler：事件批次的总编排

## 1. 本讲目标

本讲聚焦 NGF 控制面「事件批次」到达后的**总指挥**：`eventHandlerImpl`。读完本讲，你应当能够：

- 说出 `HandleEventBatch` 一轮处理的主流程：**解析捕获 → 构建图 → 下发配置 → 入队状态**。
- 解释 `sendNginxConfig` 如何编排 `ChangeProcessor`、`dataplane.BuildConfiguration`、`Generator`、`NginxUpdater`、`NginxProvisioner`、`StatusUpdater` 等子系统，以及它们的先后顺序与原因。
- 理解「就绪」与「状态队列」为什么是两条相对独立的线索：`graphBuiltHealthChecker` 何时把 Pod 标记 Ready，`statusQueue` 如何把写状态的动作与配置下发解耦。
- 能画出一次事件批次在 handler 内部的完整调用时序图（本讲的综合实践）。

> 本讲承接 [u4-l2 事件循环与批处理](u4-l2-event-loop-and-batching.md)：上一讲止于「事件被派发给 `EventHandler.HandleEventBatch`」，本讲就从这一行开始，看一整批事件被怎样消费、最终变成 NGINX 的真实配置与 Kubernetes 资源的 status。

---

## 2. 前置知识

本讲默认你已经建立以下认知（来自 u1~u4 前几讲）：

- **Gateway API 三层资源**：GatewayClass / Gateway / HTTPRoute 等，以及 NGF 把它们翻译成 NGINX 配置的整体定位（u1-l1）。
- **控制面/数据面分离**：Go 控制面 watch 资源并生成配置，NGINX 处理流量，NGINX Agent 经 gRPC 下发配置（u1-l1、u7 将深入）。
- **事件管线中段**：控制器把资源变更翻译成 `UpsertEvent`/`DeleteEvent`（u4-l1），`EventLoop` 用双缓冲把它们批处理成 `EventBatch`，再交给 `EventHandler`（u4-l2）。
- **ChangeProcessor 与 Graph**：`state.ChangeProcessor` 捕获资源增删改、维护集群状态快照，`Process()` 输出内部模型 `graph.Graph`（u4-l4 将深入）。

几个本讲会反复出现的术语，先给出口语化解释：

| 术语 | 一句话解释 |
| --- | --- |
| `EventHandler` | 事件批次的唯一消费者接口，NGF 用 `eventHandlerImpl` 实现它。 |
| 批（Batch） | 一轮被合并处理的事件集合，通常对应一次 NGINX 配置更新。 |
| 编排（Orchestration） | 按固定顺序调用多个子系统、把「资源变更」一步步推进到「配置生效」的过程。 |
| 就绪（Ready） | 控制面 Pod 的健康探针状态；NGF 规定「首张图构建完成」才 Ready。 |
| 状态队列（statusQueue） | 一个生产者/消费者队列，把「写 status 回 K8s」这件事从配置下发主链路上剥离。 |

---

## 3. 本讲源码地图

本讲围绕一个核心文件，并引用它依赖/协作的若干接口定义：

| 文件 | 作用 |
| --- | --- |
| [internal/controller/handler.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go) | **本讲主角**。`eventHandlerImpl` 的全部逻辑：批处理入口、解析捕获、配置下发编排、状态更新、就绪、leader 重发。 |
| [internal/framework/events/handler.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/handler.go) | 框架层定义的 `EventHandler` 接口（`HandleEventBatch`）。 |
| [internal/framework/events/loop.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go) | `EventLoop.Start`，在双缓冲翻转后调用 `handler.HandleEventBatch` 的那一行。 |
| [internal/controller/state/change_processor.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go) | `ChangeProcessor` 接口：`CaptureUpsertChange`/`CaptureDeleteChange`/`Process`/`GetLatestGraph`/`ForceRebuild`。 |
| [internal/controller/nginx/agent/agent.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/agent.go) | `NginxUpdater` 接口：`UpdateConfig`/`UpdateUpstreamServers`。 |
| [internal/controller/status/queue.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/queue.go) | `status.Queue`：`Enqueue`/`Dequeue` 与 `QueueObject`。 |
| [internal/controller/status/leader_aware_group_updater.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/leader_aware_group_updater.go) | `GroupUpdater` 接口：`UpdateGroup`。 |
| [internal/controller/nginx/config/generator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go) | `Generator` 接口：把 `dataplane.Configuration` 渲染成 `[]agent.File`。 |
| [internal/controller/health.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/health.go) | `graphBuiltHealthChecker`：首张图就绪后把健康探针置 Ready。 |
| [internal/controller/manager.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go) | 把 `eventHandlerImpl` 装配进 `EventLoop`，并把 `eventHandler.enable` 挂到 leader 回调。 |

---

## 4. 核心概念与源码讲解

我们把 handler 拆成五个最小模块：

1. **4.1** EventHandler 的定位、装配与依赖图
2. **4.2** `HandleEventBatch` 主流程：解析捕获 → 构图
3. **4.3** `sendNginxConfig`：子系统编排顺序（核心）
4. **4.4** 就绪检查与 `statusQueue` 的协作
5. **4.5** leader 切换下的重发与幂等（`enable`）

---

### 4.1 EventHandler 的定位、装配与依赖图

#### 4.1.1 概念说明

`EventLoop`（u4-l2）只负责「攒事件、批处理、派发」，它**不关心**事件意味着什么、要怎么影响 NGINX。真正「理解事件并推动系统前进」的工作，全部交给一个接口：

```go
// internal/framework/events/handler.go
type EventHandler interface {
    HandleEventBatch(ctx context.Context, logger logr.Logger, batch EventBatch)
}
```

这是框架层（`internal/framework`）定义的抽象，产品层（`internal/controller`）提供唯一实现 `eventHandlerImpl`。这个职责切分和 u1-l2 讲过的「`framework` 写如何搭控制器、`controller` 写 NGF 产品」边界一致。

`eventHandlerImpl` 的职责在源码注释里写得很明确（共四条）：

1. 把 Gateway API 与 K8s 内置资源与 NGINX 配置对齐（**reconcile 配置**）。
2. 保持 Gateway API 资源的 status 最新。
3. 更新控制面自身配置（NginxGateway CRD）。
4. 追踪 NGINX Plus 用量上报 Secret（如适用）。

要完成这么多事，它需要一大把「帮手」——也就是依赖注入进来的子系统。理解 handler 的第一件事，就是认清它**不实现业务，只做编排**：真正的活儿（捕获、构图、生成、下发、写状态）都委托给注入的接口。

#### 4.1.2 核心流程

handler 的依赖通过一个巨大的 `eventHandlerConfig` 结构体一次性注入。我们可以把这些依赖按角色分成几组：

| 角色 | 字段 | 它替 handler 干什么 |
| --- | --- | --- |
| 捕获/构图 | `processor state.ChangeProcessor` | 接收资源增删，输出 `graph.Graph` |
| 解析后端 | `serviceResolver resolver.ServiceResolver` | Service → Endpoints |
| 生成配置 | `generator ngxConfig.Generator` | `dataplane.Configuration` → `[]File` |
| 下发配置 | `nginxUpdater agent.NginxUpdater` | 经 Agent 把文件推到数据面、触发 reload |
| 管理数据面 | `nginxProvisioner provisioner.Provisioner` | 为每个 Gateway 创建/回收 NGINX Deployment |
| 数据面台账 | `nginxDeployments *agent.DeploymentStore` | 记录每个数据面 Deployment 的状态 |
| 写状态 | `statusUpdater status.GroupUpdater` | 把 status 批量写回 K8s |
| 状态队列 | `statusQueue *status.Queue` | 解耦「要写状态」与「真正去写」 |
| 就绪 | `graphBuiltHealthChecker` | 首图构建后置 Ready |
| WAF 轮询 | `wafPollerManager` | 调和 WAF bundle poller 生命周期 |
| 其它 | `k8sClient`、`eventRecorder`、`deployCtxCollector`、`logger`、各种名称/开关 flag | 读写 K8s、记事件、Plus 授权上下文、日志 |

这些依赖**绝大多数**在 [internal/controller/manager.go:222-252](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L222-L252) 的 `StartManager` 里被装配并传给 `newEventHandlerImpl`——这正是 u3-l1 讲过的「依赖注入式总装」。

#### 4.1.3 源码精读

实现体的字段，反映了 handler 在运行期自己维护的状态：

```go
// internal/controller/handler.go:156-166
type eventHandlerImpl struct {
    latestConfigurations  map[types.NamespacedName]*dataplane.Configuration
    objectFilters         map[filterKey]objectFilter
    finalizedAPResources  map[apResourceKey]struct{}
    cfg                   eventHandlerConfig
    lock                  sync.RWMutex
    leaderLock            sync.RWMutex
    finalizerLock         sync.Mutex
    finalizersInitialized bool
    leader                bool
}
```

几个值得注意的字段含义：

- `latestConfigurations`：按 Gateway 维度缓存「最近一次算出的配置」，供 `GetLatestConfiguration()` 对外暴露（被 telemetry 等读取）。读写受 `lock` 保护（见 [handler.go:1037-1059](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L1037-L1059)）。
- `objectFilters`：「需要特殊处理而非走标准 `Capture()` 」的对象表，目前只注册了 NginxGateway CRD（见 [handler.go:176-182](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L176-L182)）。
- `leader` + `leaderLock`：本副本是否为 leader，控制「改集群」动作是否放行（u3-l4 leader 选举语义在 handler 层的落地）。
- `finalizedAPResources` / `finalizerLock`：PLM 场景下给 APPolicy/APLogConf 加/卸 finalizer 的台账。

构造函数里还会启动一个常驻 goroutine（这一点很关键，4.4 会展开）：

[internal/controller/handler.go:184](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L184) —— `go handler.waitForStatusUpdates(cfg.ctx)`，即 handler 一被构造，就有一个独立的「状态消费者」goroutine 在跑。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认 handler「只编排、不实现」这个判断。

**步骤**：
1. 打开 `internal/controller/manager.go`，定位 `newEventHandlerImpl(eventHandlerConfig{...})`（约 222 行）。
2. 数一数传进来的字段，把它们按上表的角色分组。
3. 找到 `statusQueue := status.NewQueue()`（约 208 行），注意它**同时**被传给 `createAgentServices`、`createAndRegisterProvisioner`、`createWAFPollerManager` 和 `eventHandler`——说明 statusQueue 是**多生产者**的。

**预期观察**：你会发现 `processor`、`generator`、`nginxUpdater`、`statusUpdater` 等全是**接口类型**字段，handler 持有的是抽象。这正是它可测试、可替换的原因（fake 由 counterfeiter 生成，见 u13-l1）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 handler 要同时持有 `processor`（ChangeProcessor）和 `nginxDeployments`（DeploymentStore）两个「状态源」？

> **参考答案**：`processor` 持有的是**输入侧**的集群状态快照（Gateway API 资源 → Graph），决定「配置应该是什么」；`nginxDeployments` 持有的是**输出侧**的数据面台账（每个 NGINX Deployment 的文件、最新错误），决定「配置要下发到哪里、上次下发成功与否」。一个是「想要的」，一个是「实际有的」，handler 在二者之间做对齐。

**练习 2**：`eventHandlerImpl` 的方法里，哪些会改集群、哪些只读？

> **参考答案**：写集群的有 `ensureInferencePoolServices`、`reconcileAPResourceFinalizers`、`updateStatuses`（经 statusUpdater 写回 status），它们都受 `isLeader()` 守卫或经 leader 感知的 statusUpdater；只读/纯计算的有 `parseAndCaptureEvent`、`sendNginxConfig` 里的构图与生成、`gatewayHasPendingWAFBundle` 等。

---

### 4.2 HandleEventBatch 主流程：解析捕获 → 构图

#### 4.2.1 概念说明

`HandleEventBatch` 是 `EventHandler` 接口的唯一方法，也是 EventLoop 派发批次的落点。可以这样理解它的工作模式：

> **先攒，后算**。一批事件到来时，handler 先逐条把它们「喂」给 ChangeProcessor（`Capture*`，只是登记变更，很便宜），全部喂完后，再调用一次 `processor.Process()` 一次性构建/更新 Graph。这跟 u4-l2 讲的「批处理压缩 reload 次数」是同一个思想的延续——N 条事件只触发一次（或很少次）昂贵的图构建与配置下发。

这里要区分两个动作：

- **Capture（捕获）**：把资源对象存进 ChangeProcessor 的集群状态，并标记「有变更」。轻量。
- **Process（处理）**：基于「有无变更」决定是否重建 Graph。重活。

为什么不在每条事件后立即 Process？因为一批里可能有多条针对同一资源的变更，只 Process 一次就够了。

#### 4.2.2 核心流程

`HandleEventBatch` 的伪代码：

```
HandleEventBatch(ctx, logger, batch):
    start := 记录开始时间
    defer { 记录耗时; 上报指标 metricsCollector.ObserveLastEventBatchProcessTime }
    for event in batch:
        parseAndCaptureEvent(ctx, logger, event)   # 逐条解析+捕获
    gr := processor.Process(ctx)                    # 一次性构图
    if 首图还没就绪:
        graphBuiltHealthChecker.setAsReady()        # 就绪开关
    sendNginxConfig(ctx, logger, gr)                # 进入下发编排（4.3）
```

注意三个细节：
1. `defer` 块在函数返回时统一上报「批次处理耗时」指标——这个指标就是 u11-l1 会讲的事件批次处理耗时。
2. 「首图就绪」只在第一次成功 Process 之后触发一次（`if !ready`），之后永远不再进这个分支。
3. 即便 `gr` 为 nil（例如首次尚无变更），`sendNginxConfig` 内部会直接 return（4.3 讲），不会崩。

#### 4.2.3 源码精读

主入口 [internal/controller/handler.go:189-214](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L189-L214)：

```go
func (h *eventHandlerImpl) HandleEventBatch(ctx context.Context, logger logr.Logger, batch events.EventBatch) {
    start := time.Now()
    logger.V(1).Info("Started processing event batch")

    defer func() {
        duration := time.Since(start)
        logger.V(1).Info("Finished processing event batch", "duration", duration.String())
        h.cfg.metricsCollector.ObserveLastEventBatchProcessTime(duration)
    }()

    for _, event := range batch {
        h.parseAndCaptureEvent(ctx, logger, event)
    }

    gr := h.cfg.processor.Process(ctx)

    // Once we've processed resources on startup and built our first graph, mark the Pod as ready.
    if !h.cfg.graphBuiltHealthChecker.ready {
        h.cfg.graphBuiltHealthChecker.setAsReady()
    }

    h.sendNginxConfig(ctx, logger, gr)
}
```

逐条解析+捕获的逻辑在 [parseAndCaptureEvent](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L830-L876)（handler.go:830-876），它按事件类型分三类：

- `*events.UpsertEvent`：先查 `objectFilters` 看是否需要特殊处理（如 NginxGateway CRD 走控制面配置更新），否则/之后调 `processor.CaptureUpsertChange(e.Resource)`。
- `*events.DeleteEvent`：同理，可能命中 filter 的 `delete` 回调，否则/之后调 `processor.CaptureDeleteChange(e.Type, e.NamespacedName)`。
- `events.WAFBundleReconcileEvent`：WAF bundle 异步就绪事件，**不**走 `CaptureUpsertChange`（避免用 metadata-only 桩覆盖真实策略对象），而是调 `processor.ForceRebuild()` 强制下一次 `Process()` 重建图。

`objectFilter` 的结构也值得一看（[handler.go:144-148](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L144-L148)）：

```go
type objectFilter struct {
    upsert               func(context.Context, logr.Logger, client.Object)
    delete               func(context.Context, logr.Logger, types.NamespacedName)
    captureChangeInGraph bool
}
```

第三个字段 `captureChangeInGraph` 决定：特殊处理完之后，**是否还要**把这次变更也送进图重建。NginxGateway CRD 走纯控制面配置路径，不需要进图，所以默认 `false`。

#### 4.2.4 代码实践（源码阅读型）

**目标**：验证「WAF bundle 就绪」如何绕过普通捕获路径触发重建。

**步骤**：
1. 阅读 [handler.go:854-872](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L854-L872) 的 `WAFBundleReconcileEvent` 分支。
2. 打开 `internal/controller/state/change_processor.go`，找到 [ForceRebuild](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L352-L358) 的实现（约 353 行）。
3. 阅读其上方注释：它「不修改 cluster state，只强制下一次 Process 重建」。

**预期观察**：你会看到 `ForceRebuild` 只是翻转一个「脏标志」，让随后即便没有 `Capture*` 也会重建图。这解释了为什么 WAF bundle 到货后，之前被 `gatewayHasPendingWAFBundle` 卡住的 Gateway 能被解阻塞。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `WAFBundleReconcileEvent` 不调用 `CaptureUpsertChange`，而是 `ForceRebuild`？

> **参考答案**：因为 WAF bundle 不是一个 Kubernetes 资源对象，没有可捕获的 `client.Object`；如果硬塞一个 metadata-only 桩进去，会覆盖 ChangeProcessor 里真实的 WAFPolicy 对象，污染下一次图构建。`ForceRebuild` 只让 `Process()` 重跑一次，复用已有的真实集群状态。

**练习 2**：`HandleEventBatch` 里 `processor.Process(ctx)` 的返回值 `gr` 什么情况下是 `nil`？

> **参考答案**：当本批次没有任何被捕获的变更、且没有被 `ForceRebuild` 标脏时，`Process` 返回 nil（不重建）。此时 `sendNginxConfig` 开头的 `if gr == nil { return }` 会直接返回，避免无谓的下发——这正是 u4-l2 提到的「同 Generation 重复 upsert 不触发重配」的体现。

---

### 4.3 sendNginxConfig：子系统编排顺序（核心）

#### 4.3.1 概念说明

`sendNginxConfig` 是 handler 里最长、也最重要的方法——它就是「编排顺序」这个最小模块的全部内容。拿到一张图（`*graph.Graph`）后，它要决定：

- 要不要为 WAF poller 做调和？
- 要不要管 APPolicy/APLogConf 的 finalizer？
- 每个 Gateway 是否合法、是否能下发？
- 对每个合法 Gateway：**先构建配置，再下发，最后入队一条状态更新**。

直觉上记住一个递进链：

> **图 →（逐 Gateway）数据面台账登记 → 镜像确定 → 构建配置 → Plus 上下文 → 缓存最新配置 → 生成文件 → 下发到 Agent → 收集下发结果 → 入队状态**。

为什么配置下发和状态更新要分开？因为下发是**同步的、贵的**（要经 gRPC 把文件推到数据面 Pod 并 reload），而状态更新要写 API server、可以被外部事件（如 Service IP 变化、WAF poll 回调）独立触发。把它们拆成「下发时只入队一个状态意图」「由专门的消费者去写」，是解耦的关键（4.4 展开）。

#### 4.3.2 核心流程

`sendNginxConfig(ctx, logger, gr)` 的伪代码（省略 WAF/poll 细节）：

```
sendNginxConfig(ctx, logger, gr):
    if gr == nil: return
    defer reconcileWAFPollers(ctx, gr)          # 无论走哪个分支，最后都调和 poller
    reconcileAPResourceFinalizers(ctx, logger, gr)
    if len(gr.Gateways) == 0:
        statusQueue.Enqueue({UpdateType: UpdateAll})   # 仍要更新 GatewayClass status
        return
    ensureInferencePoolServices(ctx, gr.ReferencedInferencePools)
    for gw in gr.Gateways:
        go nginxProvisioner.RegisterGateway(ctx, gw, gw.DeploymentName.Name)   # 异步
        if 无 Listener 或 gw 无效:
            statusQueue.Enqueue({Deployment..., UpdateType: UpdateAll})
            continue
        if gatewayHasPendingWAFBundle(gr, gw) 且非 failOpen:
            statusQueue.Enqueue({..., Error: "配置被扣留"})
            continue
        deployment := nginxDeployments.GetOrStore(ctx, gw.DeploymentName, gw.Source.Name)
        nginxImage := provisioner.DetermineNginxImageName(...)
        deployment.SetImageVersion(nginxImage)
        cfg := dataplane.BuildConfiguration(ctx, logger, gr, gw, serviceResolver, plus)
        cfg.DeploymentContext := getDeploymentContext(ctx)   # Plus 才有
        setLatestConfiguration(gw, &cfg)
        volumeMounts := 从 EffectiveNginxProxy 解析
        deployment.FileLock.Lock()
        updateNginxConf(deployment, cfg, volumeMounts)        # 生成 + 下发
        deployment.FileLock.Unlock()
        err := deployment.GetLatestConfigError() ⊕ GetLatestUpstreamError()
        statusQueue.Enqueue({..., Error: err, NginxConfigPushed: true})
```

而 `updateNginxConf`（[handler.go:879-891](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L879-L891)）把「生成」和「下发」粘在一起：

```go
func (h *eventHandlerImpl) updateNginxConf(deployment *agent.Deployment, conf dataplane.Configuration, volumeMounts []v1.VolumeMount) {
    files := h.cfg.generator.Generate(conf)            // Configuration → []File
    h.cfg.nginxUpdater.UpdateConfig(deployment, files, volumeMounts)  // 下发到 Agent
    if h.cfg.plus {
        h.cfg.nginxUpdater.UpdateUpstreamServers(deployment, conf)    // Plus：动态改 upstream
    }
}
```

这里的 `generator.Generate` 即 u6-l1 的配置生成器入口，`nginxUpdater.UpdateConfig` 即 u7-l1 的 gRPC 下发入口——本讲只把它们当作「黑盒步骤」。

#### 4.3.3 源码精读

整个方法在 [internal/controller/handler.go:226-337](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L226-L337)。几个关键点逐个对应：

**(a) `gr == nil` 直接返回**（[L227-229](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L227-L229)）：保证「无变更不下发」。

**(b) `defer reconcileWAFPollers`**（[L233](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L233)）：用 defer 而非直接调用，是为了**保证无论从哪个早返回分支（如无 Gateway、Gateway 无效）退出，poller 都会被调和**——否则被删除策略的 poller 会泄漏。

**(c) 无 Gateway 也要更新 status**（[L237-244](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L237-L244)）：即便没有 Gateway，也要把 GatewayClass 的 Accepted 状态写回，所以仍 Enqueue 一个 `UpdateAll`。

**(d) 逐 Gateway 循环**（[L249-336](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L249-L336)）：

- `RegisterGateway` 放进 **`go func`** 异步执行（[L250-254](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L250-L254)）：provisioner 创建数据面资源（Deployment/Service 等）较慢，不阻塞配置下发。
- 无效/无监听器的 Gateway：只更新状态、`continue` 跳过配置生成（[L257-267](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L257-L267)）。
- WAF bundle 未就绪且 fail-closed：**扣留配置**，入队一条带 Error 的状态（[L269-283](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L269-L283)）。这是 NGF 安全态：宁可不下发也不下发一份缺 WAF 保护的配置。
- 登记数据面 Deployment、确定镜像版本（[L285-295](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L285-L295)）。
- 构建配置 + 注入 Plus 上下文 + 缓存（[L297-304](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L297-L304)）。
- 持 `deployment.FileLock` 调 `updateNginxConf`（[L318-320](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L318-L320)）：文件锁保证同一 Deployment 的配置写入串行化。
- 合并 config/upstream 两类错误，入队状态（`NginxConfigPushed: true`）（[L322-335](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L322-L335)）。

注意 [L326-335](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L326-L335) 的 `status.QueueObject`：`NginxConfigPushed: true` 是个重要标记——它告诉状态消费者「这次真的下发过配置」，区别于纯状态更新（如 WAF poll 回调，`NginxConfigPushed: false`）。

#### 4.3.4 代码实践（源码阅读 + 参数观察型）

**目标**：理解「同一次批次、多个 Gateway」如何被并行/串行处理。

**步骤**：
1. 在 `sendNginxConfig` 的 `for _, gw := range gr.Gateways` 循环里，找出哪些操作是同步的、哪些被 `go func()` 包成了异步。
2. 思考：如果集群里有 3 个 Gateway，`RegisterGateway` 会被启动几个 goroutine？而 `updateNginxConf` 是否会并行？
3. （可选）在本地用 `make build` 后，部署一个含 2 个 Gateway 的示例，把 handler 日志级别调到 `-v=1`，观察日志里 `Started processing event batch` 与各 Gateway 的下发日志顺序。

**预期结果**：`RegisterGateway` 对每个 Gateway 各起一个 goroutine（互不影响）；而 `updateNginxConf` 在循环内是**串行**的（不同 Gateway 用不同 Deployment，FileLock 是每 Deployment 一把，但循环本身顺序执行）。`待本地验证`：多 Gateway 下的实际日志交错情况，取决于调度。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `reconcileWAFPollers` 用 `defer` 而不是在函数末尾直接调用？

> **参考答案**：因为函数有多个早返回点（`gr == nil`、`len(Gateways)==0`、Gateway 无效 `continue` 后循环结束等）。如果放在末尾，某些路径会漏掉 poller 调和，导致被删除/失效策略的 poller 无法被停止而泄漏。`defer` 保证任意退出路径都执行一次。

**练习 2**：`deployment.FileLock` 保护的是什么？为什么需要它？

> **参考答案**：保护对**同一个数据面 Deployment** 的配置文件写入（`updateNginxConf` 内的生成+下发）不被并发交错。因为 `RegisterGateway` 起了独立 goroutine、外部 WAF poll 回调也可能触发对同一 Deployment 的下发（见 u7、u10），用文件锁串行化同一 Deployment 的写入，避免半成品配置或竞态。

---

### 4.4 就绪检查与 statusQueue 的协作

#### 4.4.1 概念说明

这一节回答两个问题：**控制面什么时候才算 Ready**？**写状态为什么不在 `sendNginxConfig` 里直接做，而要绕一个队列**？

**就绪（Ready）**：NGF 不在「进程启动」时就 Ready，而在「**第一张图构建完成**」后才 Ready。这避免了控制面 Pod 还没把配置下发到数据面就被流量探针当成可用（u3-l2 已铺垫 readyz 的语义）。实现者就是 `graphBuiltHealthChecker`，它在 `HandleEventBatch` 里被一次性置位。

**statusQueue**：一个**多生产者、单消费者**的队列。

- 生产者：handler（`sendNginxConfig` 里每处理完一个 Gateway 入队一条）、provisioner（Service IP 变化时入队）、WAF poller（bundle 更新/出错时入队）。
- 消费者：handler 启动时拉起的常驻 goroutine `waitForStatusUpdates`。

这样设计的好处：配置下发（同步、慢、可能失败）与状态写回（写 API server、可合并、可重试）彻底解耦；外部事件（Service IP 变了）也能独立驱动一次状态刷新，而不必等下一次资源变更。

#### 4.4.2 核心流程

**就绪**：

```
HandleEventBatch:
    gr := processor.Process(ctx)
    if !graphBuiltHealthChecker.ready:
        graphBuiltHealthChecker.setAsReady()   # close(readyCh)，readyCheck 返回 nil
```

`readyCheck` 被注册为 controller-runtime 的健康探针，返回 nil 即「健康」。`setAsReady` 还会 `close(readyCh)`，供 telemetry 等组件阻塞等待「首图就绪」（u11-l2）。

**statusQueue**：

```
# 生产侧（多处）
statusQueue.Enqueue(&QueueObject{UpdateType, Deployment, Error, NginxConfigPushed, GatewayService})

# 消费侧（waitForStatusUpdates，常驻 goroutine）
for {
    item := statusQueue.Dequeue(ctx)          # 阻塞直到有项或 ctx 取消
    if item == nil: return
    gr := processor.GetLatestGraph()
    解析 item 对应的 gw
    根据 item.Error / NginxConfigPushed 设 gw.LatestReloadResult
    switch item.UpdateType:
        case UpdateAll:    updateStatuses(ctx, gr, gw)        # 全量 status
        case UpdateGateway: 仅更新该 Gateway 的地址/status
}
```

`QueueObject` 携带的 `UpdateType` 有两档：`UpdateAll`（重算几乎所有资源 status）与 `UpdateGateway`（只刷新某个 Gateway，典型场景是 Service 公网 IP 变化）。

#### 4.4.3 源码精读

**就绪**：`graphBuiltHealthChecker` 定义在 [internal/controller/health.go:17-49](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/health.go#L17-L49)：

```go
func (h *graphBuiltHealthChecker) readyCheck(_ *http.Request) error {
    h.lock.RLock()
    defer h.lock.RUnlock()
    if !h.ready {
        return errors.New("control plane is not yet ready")
    }
    return nil
}

func (h *graphBuiltHealthChecker) setAsReady() {
    h.lock.Lock()
    defer h.lock.Unlock()
    h.ready = true
    close(h.readyCh)
}
```

在 handler 里的调用点是 [handler.go:208-211](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L208-L211)。

**statusQueue 的结构**：[internal/controller/status/queue.go:27-90](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/queue.go#L27-L90)。注意 `Dequeue` 在队列为空时**阻塞**等待 `notifyCh`，ctx 取消时返回 nil——这让消费者 goroutine 能随控制面退出而干净结束：

```go
func (q *Queue) Dequeue(ctx context.Context) *QueueObject {
    q.lock.Lock()
    defer q.lock.Unlock()
    for len(q.items) == 0 {
        q.lock.Unlock()
        select {
        case <-ctx.Done():
            q.lock.Lock()
            return nil
        case <-q.notifyCh:
            q.lock.Lock()
        }
    }
    front := q.items[0]
    q.items = q.items[1:]
    return front
}
```

**消费者**：[waitForStatusUpdates](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L531-L611)（handler.go:531-611）。关键判断在 [L565-567](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L565-L567)：

```go
// 只有真正尝试过配置下发时，才更新 LatestReloadResult。
// 纯状态队列项（如 WAF poll 回调）NginxConfigPushed=false 且无 error，
// 此时更新 LatestReloadResult 会错误地清掉之前的 reload 错误。
if gw != nil && (item.NginxConfigPushed || item.Error != nil) {
    gw.LatestReloadResult = nginxReloadRes
}
```

这一段注释非常能体现「队列里混着多种来源的状态意图」的设计后果——必须用 `NginxConfigPushed` 区分「这次到底有没有动过配置」。

`UpdateAll` 分支调 [updateStatuses](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L613-L726)（handler.go:613-726），它把 GatewayClass、Routes、各类 Policy、SnippetsFilter、AuthenticationFilter、ListenerSet、InferencePool 的 status 请求**分两个 group** 提交：

```go
h.cfg.statusUpdater.UpdateGroup(ctx, groupAllExceptGateways, reqs...) // 除 Gateway 外的全部
// ...
h.cfg.statusUpdater.UpdateGroup(ctx, groupGateways, gwReqs...)        // Gateway 单独一组
```

为什么要单独一组？源码注释（[L717-718](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L717-L718)）说得很直白：**为了让 Gateway 的 status 能在公网 IP 变化时独立刷新**，而不必拖上整张图。`GroupUpdater` 接口（[leader_aware_group_updater.go:15-17](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/leader_aware_group_updater.go#L15-L17)）按 group 名分组，leader 感知（u3-l4、u8-l1）。

#### 4.4.4 代码实践（源码阅读型）

**目标**：追清楚一次「Service 公网 IP 变化」是如何**绕过**整张图重建、只刷新 Gateway status 的。

**步骤**：
1. 在 `internal/controller/provisioner/` 下找到 provisioner 在 Service 就绪/变化时往 `statusQueue` Enqueue 的地方（提示：搜索 `statusQueue.Enqueue`，注意 `UpdateType: status.UpdateGateway` 与 `GatewayService` 字段）。
2. 回到 [waitForStatusUpdates](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L531-L611) 的 `UpdateGateway` 分支（[L572-606](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L572-L606)），看它如何用 `item.GatewayService` 直接取地址，再 `PrepareGatewayRequests` + `UpdateGroup(groupGateways, ...)`。
3. 对比 `UpdateAll` 分支会重算多少类资源，体会「单独一组」省了多少 API 写。

**预期观察**：你会确认 statusQueue 把「触发源」与「写动作」解耦——provisioner 只管 Enqueue，不关心 leader 也不关心整张图；handler 的消费者才负责真正落盘。

#### 4.4.5 小练习与答案

**练习 1**：如果 `waitForStatusUpdates` 消费到一条 `item`，其 `NginxConfigPushed=false` 且 `Error=nil`，会发生什么？

> **参考答案**：不会更新 `gw.LatestReloadResult`（见 [L565-567](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L565-L567) 的判断），也不会打「NGINX configuration was successfully updated」日志（[L558-560](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L558-L560) 只在 `NginxConfigPushed` 时打）。它仍会按 `UpdateType` 走 status 刷新——这是 WAF poll 回调的典型形态：纯状态变化，不碰 reload 结果。

**练习 2**：为什么 `graphBuiltHealthChecker` 要 `close(readyCh)` 而不只是置 `ready=true`？

> **参考答案**：因为除了健康探针（轮询 `readyCheck`），还有别的组件（如 telemetry）需要**阻塞等待**首图就绪的瞬间，而不是轮询。`close(readyCh)` 让所有 `<-readyCh` 的接收方同时被唤醒，是 Go 里「一次性广播」的惯用法（u11-l2 telemetry 等首图就绪即依赖它）。

---

### 4.5 leader 切换下的重发与幂等（enable）

#### 4.5.1 概念说明

回顾 u3-l4：多副本里只有 leader 执行「改集群」动作，其余副本做热备（也 watch、也建图，但不写）。handler 层落实这一点的方式是两件事：

1. **`enable(ctx)`**：副本当选 leader 时被调用一次，立即用**最新的图**重新下发一次配置——确保新 leader 与数据面状态一致。
2. **写动作守卫**：`ensureInferencePoolServices`、`reconcileAPResourceFinalizers` 等改集群的方法，开头都有 `if !h.isLeader() { return }`。

`enable` 的设计动机很现实：leader 切换那一刻，新 leader 手里的「最新配置」可能还停留在它最后一次处理批次时的状态，而数据面可能已经被旧 leader 改过。重新下发一次，是对齐「控制面意图」与「数据面现实」的最稳妥做法。由于整条链路是幂等的（下发同样的配置文件、同样的 status），重发不会带来副作用。

#### 4.5.2 核心流程

```
# manager.go 装配时
mgr.Add(runnables.NewCallFunctionsAfterBecameLeader([
    groupStatusUpdater.Enable,
    nginxProvisioner.Enable,
    eventHandler.enable,        # <- 本节主角
]))

# 当选时
eventHandler.enable(ctx):
    leader = true
    sendNginxConfig(ctx, logger, processor.GetLatestGraph())   # 用最新图重发
```

注意 `enable` 复用了 `sendNginxConfig`——即「leader 重发」与「正常批次下发」走的是同一条编排路径，区别只在于图的来源：正常批次用刚 `Process()` 出来的图，`enable` 用 `GetLatestGraph()` 缓存的图。

#### 4.5.3 源码精读

`enable` 在 [handler.go:218-224](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L218-L224)：

```go
func (h *eventHandlerImpl) enable(ctx context.Context) {
    h.leaderLock.Lock()
    h.leader = true
    h.leaderLock.Unlock()

    h.sendNginxConfig(ctx, h.cfg.logger, h.cfg.processor.GetLatestGraph())
}
```

它的注册点在 [internal/controller/manager.go:268-274](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L268-L274)，与 `groupStatusUpdater.Enable`、`nginxProvisioner.Enable` 同属 `CallFunctionsAfterBecameLeader` 的三把「写开关」（u3-l1、u3-l4 已讲）。顺序有意义：先打开 status 写（`groupStatusUpdater.Enable`）、再打开 provisioner（`nginxProvisioner.Enable`）、最后让 handler 重发配置——此时写链路已就绪。

`isLeader()` 守卫见 [handler.go:1327-1332](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L1327-L1332)，被 [ensureInferencePoolServices](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L1226-L1227) 与 [reconcileAPResourceFinalizers](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L1070-L1071) 使用。

#### 4.5.4 代码实践（源码阅读型）

**目标**：理解非 leader 副本「热备但不写」的具体表现。

**步骤**：
1. 在 `sendNginxConfig` 里找：哪些步骤会**无条件执行**（即便非 leader 也跑），哪些受 `isLeader()` 守卫？
2. 思考：非 leader 副本收到一批事件时，它会不会调 `dataplane.BuildConfiguration`？会不会 `Enqueue` 状态？会不会真把状态写回 K8s？

**预期观察**：非 leader 副本也会构图、也会 `BuildConfiguration`、也会 `nginxUpdater.UpdateConfig`（因为 `sendNginxConfig` 本身不查 leader），但 (a) 创建 InferencePool headless Service、(b) APPolicy finalizer 维护被 `isLeader()` 拦住；最关键的是状态写——`statusUpdater` 是 `LeaderAwareGroupUpdater`，**未 Enable 前**它会缓存请求而不真正写 API server。所以「热备建图 + 单写」是靠多个机制共同保证的，不是单一开关。

> 说明：`sendNginxConfig` 内未直接 `isLeader` 守卫配置下发本身；NGF 的「单写」一致性主要落在 status 写（leader 感知 updater）与集群资源创建（`isLeader` 守卫）上，配置下发在多副本下的协调还依赖数据面侧的 DeploymentStore 与 Agent 行为（u7）。`待本地验证`：多副本下配置下发的实际一致性表现。

#### 4.5.5 小练习与答案

**练习 1**：`enable` 里为什么要用 `GetLatestGraph()` 而不是重新 `Process()`？

> **参考答案**：`GetLatestGraph()` 返回的是 ChangeProcessor 缓存的「最近一张图」，它已经反映了截至最后一次批次的所有资源状态。`enable` 发生在 leader 切换瞬间，通常**没有新的资源变更**需要捕获，重跑 `Process` 没有意义；直接用缓存图重发即可对齐数据面。若此刻恰好有未处理事件，它们会作为新批次在后续 `HandleEventBatch` 里被正常处理。

**练习 2**：三把「写开关」（`groupStatusUpdater.Enable`、`nginxProvisioner.Enable`、`eventHandler.enable`）的注册顺序能否随意调换？

> **参考答案**：不宜随意。理想顺序是先把「写链路」打开（status updater Enable，使其从缓存态进入实时写态）、再打开 provisioner（允许创建数据面资源）、最后 handler 重发配置——这样 `sendNginxConfig` 入队的状态能被正确消费、provisioner 也能正常响应 `RegisterGateway`。虽然各 `Enable` 语义上是「打开开关」、彼此幂等，但保持「写链路先就绪」的顺序更稳健。

---

## 5. 综合实践：画出一次事件批次在 handler 中的完整调用时序图

把本讲五个模块串起来，完成下面的时序图绘制任务。这是理解 handler 编排最有效的练习。

### 实践目标

用一张时序图（或步骤化文字）表达：**一批事件从进入 `HandleEventBatch`，到 NGINX 配置生效、Kubernetes status 被更新** 的全过程，标注每一步调用的子系统、所在源码行号、以及哪些动作是异步的。

### 操作步骤

1. **设定场景**：假设集群里已有 1 个 GatewayClass、1 个有效 Gateway（含 1 个 Listener）、1 个 HTTPRoute，此时用户修改了该 HTTPRoute 的一个 backendRefs。这会触发控制器发出一条 `UpsertEvent`，被 `EventLoop` 攒成一个含 1 条事件的批次。
2. **从入口画起**：`EventLoop.Start`（[loop.go:74](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L74)）→ `eventHandlerImpl.HandleEventBatch`（[handler.go:189](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L189)）。
3. **逐层展开**，每个箭头标注被调方与源码位置：
   - `parseAndCaptureEvent`（[L830](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L830)）→ `processor.CaptureUpsertChange`
   - `processor.Process`（[L206](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L206)）→ 得到 `gr`
   - `graphBuiltHealthChecker.setAsReady`（[L210](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L210)，仅首图）
   - `sendNginxConfig`（[L213](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L213)）内部：
     - `reconcileAPResourceFinalizers`（[L235](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L235)）
     - `ensureInferencePoolServices`（[L247](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L247)）
     - 循环内：`nginxProvisioner.RegisterGateway`（**异步**，[L251](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L251)）
     - `nginxDeployments.GetOrStore`（[L285](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L285)）
     - `dataplane.BuildConfiguration`（[L297](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L297)）
     - `setLatestConfiguration`（[L304](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L304)）
     - `updateNginxConf`（[L319](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L319)）→ `generator.Generate` + `nginxUpdater.UpdateConfig`
     - `statusQueue.Enqueue`（[L335](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L335)）
   - **另一条线**（并发）：`waitForStatusUpdates` goroutine `Dequeue` → `updateStatuses` → `statusUpdater.UpdateGroup`（[L715/L725](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L715-L725)）
4. **标注异步/同步**：用不同颜色或线型区分 `RegisterGateway`（异步 goroutine）与其余同步调用；用一条虚线表示「入队状态」与「消费状态」之间的解耦。
5. **对照检查**：画完后，用源码逐行核对你画的顺序与 `sendNginxConfig` 实际顺序一致。

### 需要观察的现象 / 预期结果

一张包含两个泳道（**事件处理主线程** 与 **状态消费 goroutine**）的时序图，能清楚体现：

- 「捕获 → 构图 → 就绪 → 下发 → 入队」是**同一条同步链**（主线程内）。
- 「入队状态」与「写回 status」之间隔着一个队列，二者**异步**。
- `RegisterGateway` 是主线程里唯一的异步分叉。

> 如果无法本地运行，这也是一个纯源码阅读型实践：只要你能凭源码画出这张图并标注行号，就达成了目标。

---

## 6. 本讲小结

- `eventHandlerImpl` 是 `EventHandler` 接口的唯一实现，**只做编排、不做业务**：捕获、构图、生成、下发、写状态全部委托给注入的接口。
- `HandleEventBatch` 的主流程是「逐条 `parseAndCaptureEvent` 捕获 → 一次 `processor.Process` 构图 → 置首图就绪 → `sendNginxConfig` 下发」，N 条事件只触发一次图构建。
- `sendNginxConfig` 是编排核心：按「图 → 逐 Gateway（台账登记 → 镜像 → 构建配置 → 生成文件 → 下发 Agent → 入队状态）」推进，并用 `defer reconcileWAFPollers`、`FileLock`、WAF fail-closed 等机制保证正确性与安全态。
- **就绪**用 `graphBuiltHealthChecker` 在首图构建后一次性置位（`close(readyCh)`）；**状态写回**用多生产者/单消费者的 `statusQueue` 与配置下发解耦，由 `waitForStatusUpdates` 消费，并用 `NginxConfigPushed` 区分「真下发」与「纯状态变化」。
- 多副本「单写」靠多个机制叠加：`isLeader()` 守卫集群资源创建、`LeaderAwareGroupUpdater` 守卫 status 写、`enable` 在当选时用最新图重发一次以对齐数据面。
- WAF bundle 异步就绪通过 `WAFBundleReconcileEvent` + `processor.ForceRebuild()` 触发重建，绕过普通 `Capture` 路径，避免污染集群状态。

---

## 7. 下一步学习建议

本讲止于「事件被编排成配置下发意图 + 状态入队」。要补全整条链路，建议按序深入：

- **u4-l4 ChangeProcessor 与状态存储**：本讲把 `processor` 当黑盒，下一讲打开它，看 `Capture*` 如何维护 `ClusterState`、`Process` 如何产出 `graph.Graph`、`changed_predicate` 如何避免无意义重建。
- **u5-1 ~ u5-4 图构建与 dataplane.Configuration**：理解 `processor.Process` 输出的 `graph.Graph` 长什么样、又如何被 `dataplane.BuildConfiguration`（本讲 [L297](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L297)）转成配置中间表示。
- **u6-1 配置生成器总览**：打开本讲当作黑盒的 `generator.Generate`，看 `[]File` 是怎么被渲染出来的。
- **u7-1 NginxUpdater 与 gRPC Agent 通信**：打开本讲当作黑盒的 `nginxUpdater.UpdateConfig`，看文件如何经 gRPC 推到数据面 Pod 并触发 reload。
- **u8-1 Conditions 与状态更新**：深入本讲 `updateStatuses` 里那一堆 `Prepare*Requests` 与 `GroupUpdater` 的底层，看 Conditions 如何被组装与写回。
