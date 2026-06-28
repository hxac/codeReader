# Provisioner：为每个 Gateway 创建/回收 NGINX 资源

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **Provisioner（资源置备器）** 在 NGF 控制面里的定位：它与 u4~u7 那条「生成 NGINX 配置并下发」的主链路有何不同，为什么需要它。
- 描述 `Provisioner` 接口与 `NginxProvisioner` 实现的职责，理解 `RegisterGateway` 是它的唯一入口。
- 掌握 Provisioner **自带的一条独立事件循环**：它 watch 哪些资源、用什么谓词过滤、首批事件如何准备、为什么挂在 `LeaderOrNonLeader` 上。
- 理解 **资源状态存储 `store`** 如何把「每个 Gateway 对应哪些数据面资源」做成一本台账，并用 `gatewayChanged` / 资源版本号避免无谓重建。
- 画出数据面资源的**完整生命周期**：创建一组资源 → 更新 / 重启 → 删除 / 回收 / 重建，并回答实践任务——**创建一个新 Gateway 时，provisioner 到底会创建哪些数据面资源**。

## 2. 前置知识

在进入本讲前，请先回忆以下概念（它们在前置讲义中已建立）：

- **控制面 / 数据面分离**（u1-l1）：Go 控制面只负责「翻译 YAML」，真正处理流量的 NGINX 跑在数据面 Pod 里。
- **事件双缓冲与批处理**（u4-l2）：控制面有一条事件管线，把多次资源变更压成一次处理。
- **ChangeProcessor 与图构建**（u4-l4）：资源变更被捕获后，最终产出一张 `graph.Graph`。
- **NginxUpdater 与 DeploymentStore**（u7-l1、u7-l2）：配置生成后由 `NginxUpdater.UpdateConfig` 经 gRPC 下发给数据面 Agent；`DeploymentStore` 按「数据面 Deployment」维度组织配置。

本讲要回答一个被前面讲义刻意「跳过」的问题：**那些会收到配置的 NGINX 数据面 Pod（Deployment/DaemonSet/Service/ConfigMap/Secret）是谁创建的？谁在删？**

答案是：一个叫 **Provisioner** 的子系统。它与主事件循环**平行**，独立 watch 一组资源，负责数据面工作负载的「生老病死」。理解它，才能理解 NGF「一个 Gateway = 一组 NGINX 数据面资源」的运行模型。

> 术语澄清：本讲里的 `Deployment` 一词在两种语境下出现。一是 Kubernetes 的 `appsv1.Deployment` 对象（数据面 NGINX 工作负载）；二是 u7-l2 提到的控制面内部 `agent.Deployment`（配置下发账本，按工作负载名索引）。下文会明确区分。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [internal/controller/provisioner/provisioner.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/provisioner.go) | 定义 `Provisioner` 接口、`NginxProvisioner` 实现，含创建/更新/删除/重建/重启数据面资源的全部核心逻辑。 |
| [internal/controller/provisioner/eventloop.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/eventloop.go) | 装配 Provisioner 自己的事件循环：注册要 watch 的资源类型、谓词、首批事件。 |
| [internal/controller/provisioner/store.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/store.go) | 资源状态存储：`NginxResources` 台账、变更判定、资源版本追踪、删除中标记。 |
| [internal/controller/provisioner/handler.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/handler.go) | Provisioner 事件循环的 `EventHandler`：把 upsert/delete 事件路由到 store 与 provisioner。 |
| [internal/controller/provisioner/objects.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/objects.go) | 把一个 Gateway 翻译成一组具体的数据面 Kubernetes 对象（Deployment/Service/ConfigMap/Secret…）。 |
| [internal/controller/manager.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go) | 把 Provisioner 装进控制面：创建它、注册它的事件循环、把 `Enable` 挂到 leader 当选回调。 |

---

## 4. 核心概念与源码讲解

### 4.1 Provisioner 接口与 NginxProvisioner 职责

#### 4.1.1 概念说明

先建立一个关键认知：**NGF 控制面有两条相互独立的事件循环。**

- **主事件循环**（u4 系列）：watch Gateway/HTTPRoute/Service 等 Gateway API 资源 → 构建图 → 生成 NGINX 配置 → 经 `NginxUpdater` 下发给**已经存在的**数据面 Pod。
- **Provisioner 事件循环**（本讲）：负责让那些数据面 Pod **「存在」**——按每个 Gateway 的需要，创建/更新/删除一组 NGINX 工作负载资源（Deployment 或 DaemonSet、Service、ConfigMap、Secret、ServiceAccount，可选 HPA/PDB/OpenShift Role）。

换句话说，主循环管「数据面里 NGINX 的配置内容」，Provisioner 管「数据面这组工作负载本身」。二者解耦：哪怕配置生成失败，数据面工作负载依然可以被置备出来；反之亦然。

`Provisioner` 是一个极简接口，**只有一个方法**：

[internal/controller/provisioner/provisioner.go:46-49](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/provisioner.go#L46-L49) —— 定义「触发 NGINX 资源被创建/更新/删除」的接口，唯一方法 `RegisterGateway`。

为什么接口这么小？因为 Provisioner 的对外契约就是「主循环每构建出一个 Gateway 视图，就喊我一声」。至于具体建什么、删什么、怎么建，全部是 `NginxProvisioner` 的内部实现细节。这种「窄接口 + 厚实现」的写法，也方便用 counterfeiter 生成测试 fake（见文件顶部 `//counterfeiter:generate . Provisioner` 注释）。

#### 4.1.2 核心流程：RegisterGateway 的分支

`RegisterGateway` 是整条数据面生命周期的「调度入口」，被主循环的 `eventHandlerImpl.sendNginxConfig` 在每个 Gateway 上调用：

[internal/controller/handler.go:249-254](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L249-L254) —— 主循环遍历图中的每个 Gateway，**异步**调用 `nginxProvisioner.RegisterGateway`。

注意两点：其一，调用是放在 `go func()` 里的，即 Provisioner 的工作与配置生成/下发并行，互不阻塞；其二，传入的 `gw.DeploymentName.Name` 是图中预先算好的「该 Gateway 对应的数据面资源基础名」（见 `graph.Gateway.DeploymentName` 字段）。

`RegisterGateway` 内部是一个清晰的三段式判断：

[internal/controller/provisioner/provisioner.go:787-829](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/provisioner.go#L787-L829) —— 先 `isLeader` 门控；再用 store 判断本 Gateway 是否真的有变化；最后按 `Valid && len(Listeners) > 0` 二选一：**有效则置备，无效则回收**。

把它画成流程图：

```
RegisterGateway(ctx, gateway, resourceName)
        │
        ▼
   非 leader? ──是──▶ 直接返回（写操作只在 leader 上发生）
        │否
        ▼
 store.registerResourceInGatewayConfig(...)  // 把新 Gateway 视图写进台账
        │
        ├─ 没变化? ──是──▶ 返回（避免无谓重建）
        │
        ▼
 ┌── gateway.Valid && len(Listeners) > 0 ?
 │
 ├─ 是（有效）: handleObjectDeletion（清掉已不该存在的旧资源）
 │             → provisionNginx（建/更新一组数据面资源）
 │
 └─ 否（无效）: deprovisionNginxForInvalidGateway（删除该 Gateway 的全部数据面资源）
```

#### 4.1.3 源码精读：leader 门控与配置

`NginxProvisioner` 结构体本身只是几样东西的容器：一个 `k8sClient`、一本 `store`、一套标签选择器、一个启动期待删清单、配置 `cfg`，以及一把 `lock` 和一个 `leader` 布尔位。

[internal/controller/provisioner/provisioner.go:74-86](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/provisioner.go#L74-L86) —— `NginxProvisioner` 的字段。

这里的关键设计是 **leader 门控**：所有会改动集群的写操作（`provisionNginx`、`deprovisionNginxForInvalidGateway`、`deleteObject`、`reprovisionNginx`）方法体第一行都是 `if !p.isLeader() { return nil }`。这与 u3-l4 讲的「多副本单写」策略一致——非 leader 副本也会跑这条事件循环、也接收事件，但不落地任何写动作。

[internal/controller/provisioner/provisioner.go:215-221](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/provisioner.go#L215-L221) —— `isLeader()` 的实现，受 `lock` 保护。

但「不写」不等于「忘记」。非 leader 收到需要删除的信号时，会把它**记在 `resourcesToDeleteOnStartup`** 里，等自己当选 leader 时再补删：

[internal/controller/provisioner/provisioner.go:193-213](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/provisioner.go#L193-L213) —— `Enable` 在当选 leader 时被调用（见下文装配）：置 `leader=true`，补删启动期积压的待删 Gateway，然后清空清单。

`Enable` 是谁调的？在 manager 装配阶段，它与 `groupStatusUpdater.Enable`、`eventHandler.enable` 一起被挂进 `CallFunctionsAfterBecameLeader`（这正是 u3-l1 讲的「当选后翻开三把写开关」之一）：

[internal/controller/manager.go:268-274](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L268-L274) —— 把 `nginxProvisioner.Enable` 注册为「当选 leader 后执行一次」的回调。

至于 Provisioner 需要的配置，全部塞在一个 `Config` 结构里，由 manager 的 `createAndRegisterProvisioner` 工厂填好后注入：

[internal/controller/provisioner/provisioner.go:51-72](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/provisioner.go#L51-L72) —— `Config` 字段：包括 `DeploymentStore`（与 NginxUpdater 共享的配置下发账本）、`StatusQueue`（回写状态用）、`GatewayPodConfig`（控制面自身信息）、Plus/WAF/Inference 等开关，以及一系列 Secret 名。

#### 4.1.4 代码实践：读源码画「调用入口」关系图

**实践目标**：确认「主循环 → Provisioner」的调用边界，以及 leader 门控的位置。

**操作步骤**：

1. 打开 [internal/controller/handler.go:249-254](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L249-L254)，确认 `RegisterGateway` 是在 `go func()` 中被调用的。
2. 用编辑器全局搜索 `func (p *NginxProvisioner)` ，逐个查看 `provisionNginx`、`deprovisionNginxForInvalidGateway`、`deleteObject`、`reprovisionNginx` 的第一行，确认都有 `if !p.isLeader() { return nil }`。
3. 打开 [internal/controller/manager.go:268-274](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L268-L274)，确认 `nginxProvisioner.Enable` 与另外两个 `Enable` 并列。

**需要观察的现象**：所有「写集群」的方法都被同一把 `isLeader()` 锁住；而读、记账（store）相关的方法（如 `setResourceToDelete`）没有这道锁——因为记账是幂等的、可以在非 leader 上安全进行。

**预期结果**：你应当能画出一句话——「主循环异步喊 Provisioner；Provisioner 每个写方法都先问一句『我是 leader 吗』」。

> 待本地验证：若你能在 kind 里跑起多副本控制面，可观察非 leader 副本的日志中不会出现 `Creating/Updating nginx resources`，但 `setResourceToDelete` 的记账路径仍会执行。

#### 4.1.5 小练习与答案

**练习 1**：`Provisioner` 接口为什么只暴露 `RegisterGateway` 一个方法，而不是 `CreateGateway`/`UpdateGateway`/`DeleteGateway` 三个？

**参考答案**：因为「该建还是该删」这个判断本身依赖 Gateway 的当前有效性（`gateway.Valid` 与 `Listeners`），这个信息只有调用方（主循环构建出的 `graph.Gateway`）知道。接口只负责「把最新视图交给我」，由实现内部决定走建/更新还是删的分支。这样接口窄、调用方简单，且判断逻辑集中在 Provisioner 一处便于维护。

**练习 2**：非 leader 副本收到一个「Gateway 已被删除」的事件时，Provisioner 会立刻删数据面资源吗？

**参考答案**：不会。写操作受 `isLeader()` 门控，非 leader 直接返回。但它会通过 `setResourceToDelete` 把这个待删 Gateway 记进 `resourcesToDeleteOnStartup`，等该副本当选 leader 时，由 `Enable` 集中补删。

---

### 4.2 Provisioner 的独立事件循环

#### 4.2.1 概念说明

`RegisterGateway` 只是「主循环 → Provisioner」这一条单向调用。但 Provisioner 自己还要回答另一个问题：**用户直接动手改了数据面资源怎么办？** 比如有人手滑 `kubectl delete` 了某个 NGINX Deployment，或者直接改了 Service。

为此，Provisioner 装配了**自己的**事件循环（注意：和主循环是两个不同的 `events.EventLoop`）。它 watch 一组资源类型，把变更交给自己的 `eventHandler`，由后者决定是「重新置备」还是「清理」。

这条事件循环 watch 的资源，正是 Provisioner 自己会创建的那批数据面资源，外加 Gateway 本身和一个特殊的 Secret 通道。

#### 4.2.2 核心流程：注册哪些控制器、用什么过滤

`newEventLoop` 是装配入口，它的核心是一张 `controllerRegCfgs` 注册表，每一项说明「watch 什么类型 + 用什么谓词过滤」：

[internal/controller/provisioner/eventloop.go:67-161](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/eventloop.go#L67-L161) —— 注册表，覆盖 Gateway、Deployment、DaemonSet、Service、ServiceAccount、ConfigMap、Secret、HPA、PDB。

注意每类资源的谓词都不一样，但有一个共同基线：**`nginxResourceLabelPredicate`**——只有带 NGF 自己标签（`app.kubernetes.io/instance` + `app.kubernetes.io/managed-by`）的资源才被认领。这是 Provisioner「只管自己生的孩子」的关键，避免误伤集群里其它无关的 Deployment/Service。

几个值得注意的谓词细节：

- **Gateway**：无额外谓词，任何 Gateway 变更都进队列。
- **Deployment/DaemonSet**：`GenerationChangedPredicate`（忽略纯 status 变更）`+ nginxResourceLabelPredicate + RestartDeploymentAnnotationPredicate`。最后一个谓词专门放行「带重启注解的变更」，配合 4.4 节要讲的滚动重启。
- **Secret**：最特殊，用 `Or` 连接两个条件——「带 NGF 标签」**或**「名字落在 `secretsToWatch` 名单里」。这份名单在函数开头动态拼装，把控制面命名空间下的 agent TLS、docker registry、Plus usage、dataplane key 等 Secret 都纳入：

[internal/controller/provisioner/eventloop.go:40-60](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/eventloop.go#L40-L60) —— 拼装 `secretsToWatch` 名单，这些是「用户提供的源 Secret」，变更后需要同步到每个数据面工作负载。

- **OpenShift 专属**：当检测到运行在 OpenShift 时，额外注册 `Role` 与 `RoleBinding` 控制器（用于绑定 SCC）：

[internal/controller/provisioner/eventloop.go:163-188](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/eventloop.go#L163-L188) —— OpenShift 下追加 Role/RoleBinding。

注册完控制器后，还要为「首批事件」准备一份集群当前状态清单。这里有一个**铁律**：`GatewayList` 必须排在最前，确保 Provisioner 先看到所有 Gateway，再看到它们名下的数据面资源：

[internal/controller/provisioner/eventloop.go:209-234](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/eventloop.go#L209-L234) —— `objectList` 中 `GatewayList` 必须首位；随后用它构造 `FirstEventBatchPreparer`（首批事件准备器，u4-l2 已讲）与 `EventLoop`。

> 为什么 Gateway 必须在最前？因为若 Provisioner 先看到一个「孤儿」数据面 Deployment、却还不知道它属于哪个 Gateway，就无法判断该重建还是该回收。先 list Gateway，建立了「Gateway → 资源」的心智地图，后续资源事件才能被正确归因。

最后，这条 `EventLoop` 作为 Runnable 挂到 manager 上，包装成 `LeaderOrNonLeader`（u3-l4 已讲该语义）——**所有副本都跑**这条循环，但写动作仍由各自方法内的 `isLeader()` 把关：

[internal/controller/manager.go:384-386](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L384-L386) —— 把 Provisioner 的事件循环注册为 `LeaderOrNonLeader` Runnable。

#### 4.2.3 源码精读：事件如何被路由

事件循环把批次交给 `eventHandler.HandleEventBatch`，后者按 upsert/delete 分流：

[internal/controller/provisioner/handler.go:61-76](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/handler.go#L61-L76) —— 遍历批次，未知事件类型直接 `panic`（fail-fast）。

upsert 路径里最关键的步骤是「**这个资源属于哪个 Gateway？**」，由 `getGatewayForManagedResource` 通过标签 `gateway.networking.k8s.io/gateway-name`（放不下时退到注解）反查：

[internal/controller/provisioner/handler.go:120-132](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/handler.go#L120-L132) —— 先用 `labelSelector` 确认是 NGF 管理的资源，再读出它绑定的 Gateway 名。

得到归属后，走 `updateOrDeleteResources`，其中又有一个**版本号短路**：只有当资源的 `resourceVersion` 与台账里记录的不一致时，才真正触发置备，避免「自己刚写的变更又回流成事件」导致死循环：

[internal/controller/provisioner/handler.go:192-202](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/handler.go#L192-L202) —— `hasResourceVersionChanged` 为假则直接返回。

Secret 有一条特别的旁路：如果变更的 Secret 不属于任何具体 Gateway、但它是「用户级源 Secret」（如 NGINX Plus 的 license Secret，需复制到每个数据面工作负载），则对**所有** Gateway 各跑一次置备：

[internal/controller/provisioner/handler.go:104-113](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/handler.go#L104-L113) —— `isUserSecret` 命中时走 `provisionResourceForAllGateways`。

#### 4.2.4 代码实践：追踪「孤儿资源」的处理

**实践目标**：理解首批事件里 Gateway 必须首位的原因，以及资源版本短路的作用。

**操作步骤**：

1. 阅读 [internal/controller/provisioner/eventloop.go:209-221](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/eventloop.go#L209-L221) 的注释，记录「GatewayList MUST be first」这条约束。
2. 阅读 [internal/controller/provisioner/handler.go:176-202](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/handler.go#L176-L202)，跟踪 `updateOrDeleteResources` 的两个早退条件：Gateway 不存在（孤儿，待 GC）、版本号未变。

**需要观察的现象**：当某个数据面资源对应的 Gateway 在 store 里找不到时，代码不会立刻删，而是记下 `setResourceToDelete`（非 leader）或打日志准备 GC（leader）。

**预期结果**：你能解释「为什么 Provisioner 重启后不会误删上轮创建的资源」——因为它先 list 出全部 Gateway 重建台账，孤儿资源才会被识别并清理。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Secret 的谓词用 `Or(nginxResourceLabelPredicate, SecretNamePredicate{...})`，而其它资源只用 `nginxResourceLabelPredicate`？

**参考答案**：因为存在「用户级源 Secret」（如 Plus license、docker registry 凭证），它们住在控制面命名空间、**不带 NGF 数据面标签**，但它们的内容变了需要同步给所有数据面工作负载。`SecretNamePredicate` 按名字把这些源 Secret 也纳入 watch，使 `isUserSecret` 旁路能捕获到它们的变更。

**练习 2**：Provisioner 的事件循环为什么挂在 `LeaderOrNonLeader` 而不是 `Leader`？

**参考答案**：所有副本都需要 watch 资源、维护 store 台账（用于读判断与 leader 切换后的衔接）。真正「写集群」的动作由各方法内部的 `isLeader()` 单独把关。如果整条循环只在 leader 上跑，那么非 leader 副本就没有最新台账，切换 leader 时衔接成本更高。

---

### 4.3 资源状态存储 store：台账、变更判定与版本追踪

#### 4.3.1 概念说明

Provisioner 需要一本「账」来回答这些问题：

- 这个 Gateway 名下目前有哪些数据面资源？（用来决定要建/删什么）
- 这个 Gateway 上次的样子是什么？（用来判断「这次到底有没有变化」）
- 这个资源当前的 `resourceVersion` 是多少？（用来短路回流事件）
- 这个 Gateway 是不是正在被删？（避免删一半又触发重建）

这本账就是 `store`。它和 u4-l4 的 `state.ChangeProcessor` 里的 store 不同——那个 store 存的是 Gateway API 资源快照用于建图，而这个 store 存的是「数据面工作负载资源」的元信息，专供置备逻辑用。

#### 4.3.2 核心流程：台账的两张表

`store` 内部维护两张主表（外加一个并发安全的「删除中」集合）：

[internal/controller/provisioner/store.go:42-65](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/store.go#L42-L65) —— `gateways`（所有 Gateway 原始对象，启动期用于找孤儿资源）与 `nginxResources`（Gateway → 其名下数据面资源台账）。

`nginxResources` 的值类型 `NginxResources` 是一个「资源清单」结构，字段就是可能为某 Gateway 创建的全部数据面资源类型：

[internal/controller/provisioner/store.go:21-40](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/store.go#L21-L40) —— `NginxResources`：Deployment/DaemonSet/Service/ServiceAccount/两个 ConfigMap/多种 Secret/HPA/PDB 等，外加一个指向图节点 `*graph.Gateway` 的指针。

注意这些字段只存 `metav1.ObjectMeta`（名字、命名空间、标签、**resourceVersion**），不存完整 spec——因为置备逻辑只需要知道「有哪些、是哪个版本」，完整 spec 每次都由 `buildNginxResourceObjects` 现算。

台账的写入是分类型的：`registerResourceInGatewayConfig` 用一个 type switch 把不同类型资源落到 `NginxResources` 的对应字段。其中**只有 `*graph.Gateway` 这一支会返回「是否真的变了」**，其余类型一律返回 `true`：

[internal/controller/provisioner/store.go:121-163](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/store.go#L121-L163) —— type switch 分发；Gateway 分支调用 `gatewayChanged` 并返回其结果，其余分支记账后恒返回 `true`。

为什么只有 Gateway 要判变？因为主循环每次处理事件批次都会调 `RegisterGateway`（哪怕只是某条 HTTPRoute 变了）。如果 Route 变化也触发数据面 Deployment 重建，就是浪费。所以对 Gateway 这一类型，必须精确判断「影响数据面资源」的字段是否真的变了。

#### 4.3.3 源码精读：gatewayChanged 的精确判定

`gatewayChanged` 决定了「这次 RegisterGateway 要不要真正动手」。它比较四个维度：

[internal/controller/provisioner/store.go:259-279](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/store.go#L259-L279) —— 比较 `Valid`、`Source`（原始 Gateway 对象）、`EffectiveNginxProxy`（合并后的数据面配置）、`Listeners`。

其中监听器比较尤其讲究：**只比较端口/协议/主机名/名字的集合，忽略顺序**，因为决定数据面 Service/容器端口的是「有哪些端口」，而不是它们的排列：

[internal/controller/provisioner/store.go:285-316](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/store.go#L285-L316) —— 把新旧监听器各自建成 `listenerKey` 集合，再 `reflect.DeepEqual` 两个集合。

这个设计呼应了 u4-l4 的 changed predicate 思想：把「资源变了」收窄为「影响配置的字段变了」，从而把 N 次无谓事件压成零次重建。

至于资源版本追踪，`getResourceVersionForObject` 同样用 type switch，从台账里取回某资源的 `resourceVersion`，供 4.2 节的 `hasResourceVersionChanged` 做短路：

[internal/controller/provisioner/store.go:404-437](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/store.go#L404-L437) —— 按类型从 `NginxResources` 取 `resourceVersion`。

另外两个小而关键的辅助：`gatewayExistsForResource` 反向查询「某个资源属于哪个 Gateway」（用 `matchesObject` 按类型逐字段比对），以及 `markGatewayDeleting` / `isGatewayDeleting` 用 `sync.Map` 标记「正在删除」的 Gateway，防止「删一半又被重建」的竞态：

[internal/controller/provisioner/store.go:486-494](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/store.go#L486-L494) —— 删除中标记的存与查。

> 关于并发的小说明：store 的两张主表用一把 `sync.RWMutex`（`lock`）保护，读多写少故用 RWMutex；而 `deletingGateways` 单独用 `sync.Map`，因为它「只加（删除时）并在 deprovision 完成后随台账一起清」，访问模式更适合 `sync.Map`。

#### 4.3.4 代码实践：制造一次「无效重建」并解释为什么不会发生

**实践目标**：体会 `gatewayChanged` 如何挡掉一次本不该发生的数据面重建。

**操作步骤**：

1. 阅读 [internal/controller/provisioner/store.go:259-279](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/store.go#L259-L279)，列出四个比较维度。
2. 假想场景：用户给某 HTTPRoute 加了一条规则（不改 Gateway、不改监听器端口）。主循环重建图后调用 `RegisterGateway`。
3. 推演：`Source`（Gateway 本身）变了吗？`EffectiveNginxProxy` 变了吗？`Listeners` 的端口/协议/主机名集合变了吗？

**需要观察的现象**：三个维度都不变，`gatewayChanged` 返回 `false`，`RegisterGateway` 在 [provisioner.go:797-799](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/provisioner.go#L797-L799) 处早退，不会重建数据面 Deployment。

**预期结果**：你能解释「为什么改一条路由规则不会导致 NGINX Pod 重建，只会导致配置热更新」——前者归 Provisioner 管（被 `gatewayChanged` 挡掉），后者归 NginxUpdater 管（走 u7 的配置下发）。

#### 4.3.5 小练习与答案

**练习 1**：`NginxResources` 的字段为什么存 `metav1.ObjectMeta` 而不是完整的 `*appsv1.Deployment` 等强类型？

**参考答案**：置备逻辑只需要「资源叫什么、在哪个命名空间、当前 resourceVersion 是多少」这三类元信息；完整 spec 每次都由 `buildNginxResourceObjects` 根据最新图重新计算。只存 ObjectMeta 让台账轻量、统一（一个结构能装下所有类型），也避免了「台账里的旧 spec 与真实集群漂移」的问题。

**练习 2**：`listenersChanged` 为什么故意忽略监听器的顺序？

**参考答案**：因为影响数据面资源（Service 端口、容器端口）的只是「有哪些端口+协议+主机名」这个集合，与顺序无关；监听器之间的冲突检测早在图构建阶段（u5-l2）完成。忽略顺序意味着「只是调换监听器声明顺序」不会触发无谓的数据面重建，再次体现「精确判变」的原则。

---

### 4.4 数据面资源的生命周期：创建、更新、重启与回收

#### 4.4.1 概念说明

前三个模块讲了「Provisioner 是什么、它的事件循环怎么转、它的台账怎么记」。本模块把这一切串起来，回答实践任务的核心问题：**创建一个新 Gateway 时，到底会创建哪些数据面资源？以及它们后来怎么更新、删除。**

负责「把一个 Gateway 翻译成一组具体对象」的，是 `buildNginxResourceObjects`。它的产出清单与安装顺序在源码里有明确注释：

> 安装顺序：secrets → configmaps → serviceaccount → role/binding（OpenShift）→ service → deployment/daemonset → hpa → pdb

这些资源**全部以 Gateway 为 Owner**（通过 `setOwnerReference` 设置 owner reference），因此当用户删除 Gateway 时，Kubernetes 自身的垃圾回收会连带清理它们；Provisioner 也会主动 `deprovision`。这就是「数据面资源生命周期与 Gateway 绑定」的实现方式。

#### 4.4.2 核心流程：一个新 Gateway 的完整置备链路

把 4.1~4.3 串成一条完整链路：

```
用户 kubectl apply 一个有效 Gateway（含至少一个 Listener）
        │
        ▼ （主循环）
 图构建：graph.Gateway{Valid:true, Listeners:[...], EffectiveNginxProxy, DeploymentName}
        │
        ▼ sendNginxConfig 里 go func()
 RegisterGateway(ctx, gw, gw.DeploymentName.Name)
        │
        ├─ isLeader? 否 → 返回
        ├─ store.registerResourceInGatewayConfig → 新 Gateway，记进台账
        │
        ▼ （有效分支）
 handleObjectDeletion：若有旧类型资源已不该存在（如从 Deployment 切到 DaemonSet）→ 删旧
        │
        ▼
 buildNginxResourceObjects(resourceName, gateway.Source, EffectiveNginxProxy, Listeners)
   产出按固定顺序排列的一组对象（见 4.4.3）
        │
        ▼
 provisionNginx：逐个 createOrUpdateNginxResource
   ├─ 对每个对象：CreateOrUpdate（带重试），成功后 store.registerResourceInGatewayConfig
   ├─ 若对象是 LoadBalancer Service 且 Gateway 声明了 IP 地址 → patchServiceStatus 回写外部 IP
   └─ 若 agent ConfigMap 被更新（且工作负载非新建）→ restartNginxAfterConfigUpdate 滚动重启
        │
        ▼
 后续：NginxUpdater 把生成的 NGINX 配置下发给这些新建的数据面 Pod（u7）
```

资源命名遵循统一规则：所有数据面资源共享一个「基础资源名」（即 `gw.DeploymentName.Name`，由 `CreateNginxResourceName` 生成，超长时截断并加 hash 保证唯一且不超 63 字符）：

[internal/framework/controller/resource.go:16-50](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/resource.go#L16-L50) —— 命名与截断 hash 逻辑，受 `MaxServiceNameLen = 63` 约束。

截断算法可写成：当 `name + "-" + suffix` 超长时，保留后缀，对 name 截断并插入 8 字符 hash，即

\[
\text{result} = \text{truncName} \;+\; \text{"-"} \;+\; \text{hash}_8 \;+\; \text{"-"} \;+\; \text{suffix}
\]

其中 `truncName` 的最大长度为

\[
\text{maxNameLen} = 63 - 2\cdot|\text{"-"}| - 8 - |\text{suffix}|
\]

#### 4.4.3 源码精读：建什么、怎么建、怎么重启

**建什么**——`buildNginxResourceObjects` 的总装与安装顺序：

[internal/controller/provisioner/objects.go:92-225](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/objects.go#L92-L225) —— 总装函数；[objects.go:202-210](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/objects.go#L202-L210) 处的注释明确列出安装顺序。

具体而言，对一个典型有效 Gateway，会创建：

| 资源 | 数量 | 说明 |
| --- | --- | --- |
| Secret | 1+ | agent TLS Secret（复制自控制面）；docker registry Secret（若有）；Plus license/CA/clientSSL（Plus 时）；dataplane key（NGINX One 时） |
| ConfigMap | 2 | `*-includes-bootstrap`（main/events 模板，Plus 时含 mgmt）与 `*-agent-config`（NGINX Agent 配置） |
| ServiceAccount | 1 | 数据面 Pod 用的 SA（禁止自动挂载 token） |
| Role/RoleBinding | 0 或 2 | 仅 OpenShift，用于绑定 SCC |
| Service | 1 | 默认 LoadBalancer，端口由所有 Listener（含 ListenerSet）+ 可选健康检查端口决定 |
| Deployment 或 DaemonSet | 1 | 由 `EffectiveNginxProxy.Kubernetes` 决定用哪种；NGINX 容器 + init 容器（跑 `initialize` 命令）+ 可选 WAF/endpoint-picker 容器 |
| HPA | 0 或 1 | 仅当 NginxProxy 配置了 autoscaling |
| PDB | 0 或 1 | 仅当 NginxProxy 配置了 PodDisruptionBudget |

端口来源是所有 Listener（Gateway 本体 + 已挂载的 ListenerSet），按「端口+协议」去重，UDP 协议会被映射为 `ProtocolUDP`：

[internal/controller/provisioner/objects.go:340-358](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/objects.go#L340-L358) —— `buildPortsFromListeners` 把图里的监听器收敛成端口+协议集合。

Deployment 与 DaemonSet 的二选一由 `EffectiveNginxProxy.Kubernetes.DaemonSet` 是否设置决定：

[internal/controller/provisioner/objects.go:863-922](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/objects.go#L863-L922) —— `buildNginxDeployment`：配了 DaemonSet 字段就建 DaemonSet，否则建 Deployment。

**怎么建**——`provisionNginx` 遍历对象逐个 createOrUpdate，用 `nginxProvisionState` 跟踪「哪个是工作负载、agent ConfigMap 是否被改」，为后续重启判断做准备：

[internal/controller/provisioner/provisioner.go:389-446](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/provisioner.go#L389-L446) —— `provisionNginx` 主体：逐对象 `createOrUpdateNginxResource`，成功后登记进 store，并在全部完成后按需重启。

每个对象的实际写入用 controller-runtime 的 `CreateOrUpdate`，包了一层 `wait.PollUntilContextCancel` 做重试，并对 `spec.loadBalancerClass` 这类**不可变字段**的特殊错误做了识别（因为删 Service 重建会释放公网 IP，必须避免）：

[internal/controller/provisioner/provisioner.go:481-554](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/provisioner.go#L481-L554) —— `createOrUpdateNginxResource` 的重试与不可变字段处理；相关判定函数 `isLoadBalancerClassImmutabilityErr` 在 [provisioner.go:885-899](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/provisioner.go#L885-L899)。

**怎么重启**——当 agent ConfigMap 内容变了、但工作负载本身不是新建的，NGINX 进程不会自动重新读 Agent 配置，需要主动触发一次滚动重启。实现方式是给 Pod 模板打上 `kubectl.kubernetes.io/restartedAt` 注解：

[internal/controller/provisioner/provisioner.go:586-640](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/provisioner.go#L586-L640) —— `restartNginxAfterConfigUpdate`：只有「agent ConfigMap 被更新 且 工作负载非新建」时才打重启注解。

这条路径也解释了 4.2 节 Deployment/DaemonSet 谓词里为什么有 `RestartDeploymentAnnotationPredicate`——正是为了把这次重启注解变更放行进事件循环（否则会被 `GenerationChangedPredicate` 配合注解变更的语义过滤掉）。

**怎么删**——无效 Gateway 走 `deprovisionNginxForInvalidGateway`，按一套固定删除顺序（工作负载 → Service → HPA → PDB → OpenShift Role → SA → ConfigMap → Secret）逐个 delete，并清掉 store 台账与 `DeploymentStore` 里的配置账本：

[internal/controller/provisioner/provisioner.go:687-733](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/provisioner.go#L687-L733) —— `deprovisionNginxForInvalidGateway`；待删清单由 [objects.go:1864-1951](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/objects.go#L1864-L1951) 的 `buildResourcesForInvalidGatewayCleanup` 构造。

注意这里调用了 `p.cfg.DeploymentStore.Remove(...)`——这正是与 u7-l2 的 `DeploymentStore` 的衔接点：数据面工作负载被回收时，对应的配置下发账本也要一并清掉，避免 NginxUpdater 还在往一个已不存在的 Deployment 推配置。

**怎么重建**——当用户**误删**了某个数据面资源（而它对应的 Gateway 仍有效），delete 事件走 `reprovisionResources` → `reprovisionNginx`，用 `Create`（容忍 `AlreadyExists`）把它补回来：

[internal/controller/provisioner/handler.go:282-306](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/handler.go#L282-L306) —— `reprovisionResources`：只有 Gateway 仍有效且不在删除中时才重建。

#### 4.4.4 代码实践：列出「创建一个新 Gateway」触发的全部资源（实践任务）

**实践目标**：亲手从源码确认——创建一个有效的新 Gateway 时，Provisioner 会触发哪些数据面资源的创建。这是本讲的核心实践任务。

**操作步骤**：

1. 从入口 [internal/controller/provisioner/provisioner.go:801-821](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/provisioner.go#L801-L821) 进入有效分支，确认它调用 `buildNginxResourceObjects`。
2. 在 [internal/controller/provisioner/objects.go:212-224](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/objects.go#L212-L224) 处记录 `objects` 切片的拼装顺序：`secretsList → configmapsList → serviceAccount → (openshiftObjs) → service, deployment → (hpa) → (pdb)`。
3. 对一份**最简 OSS 场景**（无 Plus、无 WAF、无 Inference、非 OpenShift、无 autoscaling/PDB 配置）做减法，列出必然创建的资源。

**需要观察的现象 / 预期结果**：在最简场景下，创建一个有效 Gateway 会触发以下数据面资源被 create：

- 1 个 Secret（agent TLS，复制自控制面命名空间）
- 2 个 ConfigMap（`<name>-includes-bootstrap`、`<name>-agent-config`）
- 1 个 ServiceAccount
- 1 个 Service（LoadBalancer，端口来自 Listener）
- 1 个 Deployment（含 NGINX 容器 + `init` 容器）

即「至少 6 个对象」。若启用 Plus，会额外增加 license/CA/clientSSL Secret 与 mgmt 配置；若启用 WAF，Deployment 里会增加 `waf-enforcer` 与 `waf-config-mgr` 容器；若启用 Inference 扩展，会增加 `endpoint-picker-shim` sidecar；若配置了 autoscaling/PDB，会再各加一个对象。

> 待本地验证：在 kind 集群里部署 NGF 后 `kubectl apply` 一个最简 Gateway，用 `kubectl get deploy,svc,sa,configmap,secret -l gateway.networking.k8s.io/gateway-name=<gw>` 观察实际被创建的对象集合，与上述清单对照。

**进阶操作**：把同一个 Gateway 从默认的 Deployment 模式切换到 DaemonSet 模式（在 NginxProxy 里配 `kubernetes.daemonSet`），观察 `handleObjectDeletion` 如何先删掉旧 Deployment（`needToDeleteDeployment` 为真）再建 DaemonSet——见 [provisioner.go:831-881](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/provisioner.go#L831-L881)。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `restartNginxAfterConfigUpdate` 只在「agent ConfigMap 被更新 **且** 工作负载不是新建」时才触发滚动重启？

**参考答案**：新建的工作负载本就会用最新的 agent ConfigMap 启动，无需重启；只有当工作负载已存在、而 agent ConfigMap 内容变了时，运行中的 NGINX Agent 才需要重新加载配置，此时靠给 Pod 模板打 `restartedAt` 注解触发 Kubernetes 的滚动重启。这个「且」条件避免了新建场景下的多余重启。

**练习 2**：数据面资源与 Gateway 的生命周期是如何绑定的？有几种回收机制？

**参考答案**：两种机制叠加。其一是 **owner reference**：`buildNginxResourceObjects` 里每个对象都经 `setOwnerReference` 把 Gateway 设为 Owner，因此用户删除 Gateway 时，Kubernetes 垃圾回收会自动连带删除这些资源。其二是 **Provisioner 主动 deprovision**：当 Gateway 变为无效（`Valid=false` 或无 Listener）或被删除时，`deprovisionNginxForInvalidGateway` 按固定顺序主动 delete 并清掉 store 台账与 `DeploymentStore`。双重机制确保即使某条路径失效，资源也不会泄漏。

**练习 3**：`determineReplicas`（[objects.go:943-976](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/objects.go#L943-L976)）为什么在启用 HPA 时要去读集群里**现有** Deployment 的副本数，而不是用配置值？

**参考答案**：因为 HPA 会直接改写 `Deployment.Spec.Replicas`，而 HPA 的 `Status.DesiredReplicas` 是最终一致的、有滞后。若 Provisioner 用配置值或滞后的 Status 值去覆盖，就会和 HPA 抢着写副本数，造成 Pod 抖动。所以启用 HPA 时，Provisioner 读当前实际副本数「原样回写」，把扩缩容权让给 HPA。这是一个典型的「避免与控制器抢字段」的设计。

---

## 5. 综合实践

把本讲全部模块串起来，完成下面这个「全流程追踪」任务。

**场景**：在一个已部署 NGF 的 kind 集群里，依次执行——

1. `kubectl apply` 一个有效 Gateway（含一个 HTTP Listener）；
2. 等数据面就绪后，给同一个 Gateway 切换为 DaemonSet 模式（修改其 `parametersRef` 指向的 NginxProxy）；
3. `kubectl delete gateway <名字>`。

**任务**：对这三步，分别写出——

- 触发的是主循环还是 Provisioner 事件循环？（提示：二者都会被触发，但职责不同）
- Provisioner 内部走的是 `provisionNginx` / `deprovisionNginxForInvalidGateway` / `reprovisionNginx` 中的哪条路径？
- store 台账发生了什么变化？`gatewayChanged` 返回什么？
- 最终集群里数据面资源的增减情况。

**参考思路（要点）**：

- 步骤 1：主循环建图 → `RegisterGateway`（有效分支）→ `buildNginxResourceObjects` 产出约 6 个对象 → `provisionNginx` 逐个 create；store 新增一条 `NginxResources`。
- 步骤 2：`EffectiveNginxProxy` 变化 → `gatewayChanged` 返回 `true` → `buildNginxResourceObjects` 这次产出 DaemonSet → `handleObjectDeletion` 因 `needToDeleteDeployment` 为真先删旧 Deployment → `provisionNginx` 建 DaemonSet；store 中 `Deployment` 字段清空、`DaemonSet` 字段填入。
- 步骤 3：Gateway delete 事件 → `handleDeleteEvent` 标记 `deletingGateways`、清台账、`DeploymentStore.Remove`；同时 owner reference 触发 Kubernetes GC 删除剩余数据面资源；若该副本非 leader，则记进 `resourcesToDeleteOnStartup` 等当选后补删。

> 待本地验证：实际在 kind 中跑一遍，用 `kubectl get ... -l gateway.networking.k8s.io/gateway-name=<gw> -w` 持续观察三步的资源变化，与上述要点对照。

## 6. 本讲小结

- **Provisioner 与主循环平行**：主循环管「数据面 NGINX 的配置内容」，Provisioner 管「数据面这组工作负载本身」；二者通过 `RegisterGateway` 这一个异步调用衔接。
- **窄接口 + leader 门控**：`Provisioner` 接口只有 `RegisterGateway`；所有写集群的方法首行都是 `if !p.isLeader() { return nil }`，非 leader 只记账（`setResourceToDelete`），当选后由 `Enable` 补删。
- **独立事件循环**：Provisioner 有自己的 `events.EventLoop`，watch Gateway + 它自己创建的全部数据面资源类型；共同基线是「NGF 标签谓词」只认领自家资源；Secret 额外按名字名单放行用户级源 Secret；首批事件中 `GatewayList` 必须首位以建立归属地图。
- **store 是精确的台账**：`NginxResources` 只存 ObjectMeta；`gatewayChanged` 比较 Valid/Source/EffectiveNginxProxy/Listeners 四维（监听器忽略顺序），把「Route 变了」挡在数据面重建之外；资源版本号短路防止回流事件死循环。
- **完整生命周期**：建（`buildNginxResourceObjects` 按 secrets→configmaps→SA→service→workload→HPA→PDB 顺序产出，全以 Gateway 为 owner）、更新（`CreateOrUpdate` 带重试，识别 `loadBalancerClass` 不可变）、重启（agent ConfigMap 变更时打 `restartedAt` 注解滚动重启）、删（`deprovisionNginxForInvalidGateway` 按序 delete 并清 `DeploymentStore`）、重建（误删后 `reprovisionNginx` 补回）。
- **最简场景下一个有效 Gateway 至少创建 6 个对象**：agent TLS Secret、2 个 ConfigMap、ServiceAccount、Service、Deployment；Plus/WAF/Inference/autoscaling 等会在此基础上增项。

## 7. 下一步学习建议

- **横向对照**：回到 u7-l2 重读 `DeploymentStore`，理解「Provisioner 创建数据面 Deployment」与「NginxUpdater 向该 Deployment 推配置」是如何通过同一个 `DeploymentStore` 衔接的——本讲多处出现的 `p.cfg.DeploymentStore.Remove(...)` 就是这条衔接点。
- **向上追溯配置来源**：本讲反复出现的 `EffectiveNginxProxy`（决定 Deployment 还是 DaemonSet、是否 HPA/PDB、镜像、资源等）来自 u8-l2 讲的 NginxProxy 合并逻辑，建议结合阅读 [internal/controller/state/graph/nginxproxy.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/nginxproxy.go)。
- **进入高级特性**：u10（WAF 集成）会用到本讲 `configureWAF` 注入的 WAF 容器与共享卷；u12-l1（推理扩展）会用到 `endpoint-picker-shim` sidecar；u12-l2（NGINX Plus）会用到本讲的 Plus license/usage Secret 与 mgmt 配置。
- **动手扩展**：若想加一种新的数据面资源（或新的 NginxProxy 字段），需要同步改动四处——`objects.go` 的 `buildNginxResourceObjects`（建什么）、`setter.go` 的 `objectSpecSetter`（更新时如何回写 spec）、`store.go` 的 `NginxResources` + `matchesObject` + `getResourceVersionForObject`（台账与归因）、`eventloop.go` 的注册表（watch 新类型）。这正是 u13-l3「二次开发」会展开的完整改动链路。
