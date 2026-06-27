# controller-runtime Manager 与缓存

## 1. 本讲目标

在 u3-l1 里，我们站在「十公里高空」看了 `StartManager` 如何把控制面装配成一个运行进程。本讲下钻到装配链路中最关键的一环——**`createManager` 到底造出了什么**，以及**它读取的资源在内存里长什么样**。

读完本讲，你应当能够：

1. 说清楚 controller-runtime 的 **Manager** 是什么、由哪些零件组成，以及 NGF 用哪些 `manager.Options` 把它装配出来。
2. 理解 Manager 背后的 **缓存（Cache）**：为什么 NGF 要给所有资源「瘦身」，以及用 `DefaultTransform`、`ByObject`、`DefaultNamespaces` 三件套做按需裁剪。
3. 讲明白 **metrics server** 与 **health probe server** 是如何挂到 Manager 上的、默认监听哪些端口、`readyz` 在什么情况下才返回就绪。

本讲是 u3-l3「控制器注册与 CRD 发现」的直接前置——控制器最终都要注册到本讲创建的这个 Manager 上。

## 2. 前置知识

### 2.1 controller-runtime 与「Manager」是什么

[controller-runtime](https://pkg.go.dev/sigs.k8s.io/controller-runtime) 是 Kubernetes 官方的控制器脚手架库。它的核心抽象是 **Manager**：一个进程级的「容器」，内部聚合了构建一个控制器所需的全部基础设施：

- **Cache（缓存）**：一份基于 informer 的本地内存存储，watch 集群里你关心的资源，避免每次读都打 API Server。
- **Client（客户端）**：读写 Kubernetes 的统一入口，读优先走 Cache，写直接打 API Server。
- **EventRecorder（事件记录器）**：用来往 Kubernetes Events 里写记录。
- **Leader Election（主从选举）**：多副本下保证只有一个 leader 执行有副作用的动作。
- **Metrics / Health Server**：两个内置 HTTP 服务，分别暴露 Prometheus 指标和健康探针。
- **Runnables（可运行物）**：被 Manager 统一启停的长期任务，包括各个控制器以及其他后台任务。

一句话：**Manager = 进程里所有控制器共享的那套「水电煤气」**。NGF 的控制面 Pod 起来后，第一件事就是把这套基础设施造好。

### 2.2 「scheme」是什么

Kubernetes 的资源由 `Group/Version/Kind`（GVK）标识。要让 Manager 的 Cache 和 Client 认识某种资源，必须把这种资源的 Go 类型「注册」进一个 **scheme**——它本质是一张「GVK ↔ Go 类型」的双向映射表。没注册进 scheme 的资源，Manager 既无法反序列化、也无法 watch。

### 2.3 内存里的资源有多「胖」

Kubernetes 资源对象除了你关心的业务字段，还带着一大堆元数据：`managedFields`（服务端字段管理信息，可能很大）、`annotations`、`labels`、`finalizers` 等等。而像 `Secret` 这种资源，`Data` 字段可以塞进任意键值（镜像仓库凭证、应用密钥、WAF bundle 凭证……）。如果照单全收地缓存，控制面内存会非常浪费——**NGF 其实只用到其中极少数键**。

本讲的核心动机正是：**在资源进入缓存之前，把用不到的字段裁掉**。这就是 cache transform 要解决的问题。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [internal/controller/manager.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go) | 控制面总装入口。本讲聚焦其中的 `createManager`、`buildManagerCache`、`getMetricsOptions` 三个函数，以及 `init()` 里 scheme 的注册。 |
| [internal/framework/controller/cache/transform.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/cache/transform.go) | 可复用框架里的缓存「瘦身」函数：`TransformGatewayClass` / `TransformSecret` / `TransformConfigMap`。位于 `internal/framework`，说明它是通用能力，不绑定 NGF 产品逻辑。 |
| [internal/controller/config/config.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go) | `config.Config` 结构族。本讲读取其中 `WatchNamespaces`、`MetricsConfig`、`HealthConfig`、`GatewayPodConfig` 等字段。 |
| [internal/controller/state/graph/shared/secrets/secrets.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/shared/secrets/secrets.go) | 定义 NGF 真正关心的 Secret 键名常量（`AuthKey`、`TLSCertKey` 等）。 |
| [internal/controller/state/graph/shared/configmaps/configmaps.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/shared/configmaps/configmaps.go) | 定义 NGF 真正关心的 ConfigMap 键名常量（`MainConfKey`、`AgentConfKey` 等）。 |
| [internal/controller/health.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/health.go) | `graphBuiltHealthChecker`——挂到 `readyz` 的就绪检查实现。 |

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**4.1 Manager 创建选项**、**4.2 缓存 transform 与 namespace 过滤**、**4.3 Metrics 与 Health server 的挂载**。

### 4.1 创建 controller-runtime Manager（manager 创建选项）

#### 4.1.1 概念说明

`createManager` 是「造水电煤气」的工厂。它的输入是 NGF 的运行配置 `config.Config` 和一个健康检查器，输出是一个已经配好 scheme、cache、metrics、leader election 的 `manager.Manager`。后续所有控制器、事件循环、gRPC 服务，都会被「挂」到这个 Manager 上，由它统一 `Start`/`Stop`。

关键在于：**Manager 的行为几乎全部由 `manager.Options` 这个结构体决定**。NGF 在这一步要回答一连串问题——用哪个 scheme？缓存怎么裁？metrics 监听哪个端口？是否开启 leader 选举？这些都是通过给 `manager.Options` 的字段赋值来回答的。

#### 4.1.2 核心流程

`createManager` 的装配顺序是固定的五步：

```text
1. 组装 manager.Options（scheme / logger / metrics / leader election / controller 默认配置 / cache）
2. 若健康探针开启，设置 HealthProbeBindAddress
3. 取 Kubernetes REST 配置，并设置 10s 超时
4. manager.New(clusterCfg, options) 真正构造 Manager
5. 注册 readyz 检查 + 给 Pod 的 status.podIP 建一个 field index
```

其中第 1 步是本模块重点，第 5 步的 field index 埋下伏笔：它让控制面能「按 Pod IP 反查 Pod」，后续校验 NGINX Agent 的 gRPC 连接是否来自合法数据面 Pod 时会用到。

#### 4.1.3 源码精读

先看 scheme 是怎么攒出来的。它是包级变量，在 `init()` 里一次性把 NGF 要 watch 的所有 API 类型注册进去——[internal/controller/manager.go:94-124](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L94-L124) 注册了 Gateway API（v1/v1beta1/v1alpha2）、K8s 核心（core/discovery/apps/policy/rbac/auth）、NGF 自有 CRD（v1alpha1/v1alpha2）、Inference 扩展，以及 WAF 相关的非结构化类型（APPolicy/APLogConf）。**任何没在这里注册的类型，Manager 都无法缓存。**

接着是 `createManager` 本体——[internal/controller/manager.go:509-564](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L509-L564)。关键几行：

- [L510-L530](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L510-L530)：组装 `manager.Options`。逐字段看：
  - `Scheme: scheme` —— 挂上前面 `init()` 攒好的那张映射表。
  - `Logger: cfg.Logger.V(1)` —— controller-runtime 自身的内部日志压到 verbosity 1，避免刷屏。
  - `Metrics: getMetricsOptions(cfg.MetricsConfig)` —— 决定 metrics server 监听地址（见 4.3）。
  - `LeaderElection` / `LeaderElectionNamespace` / `LeaderElectionID` —— 来自 `cfg.LeaderElection` 与 Pod 所在 namespace。
  - `LeaderElectionReleaseOnCancel: false` —— 注释解释：关闭时不立即释放锁，要等所有 leader-only Runnable 跑完，避免新 leader 抢跑（[L520-L523](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L520-L523) 的注释）。
  - `Controller.NeedLeaderElection: helpers.GetPointer(false)` —— **关键设计**：所有控制器在非 leader 副本上也运行（做热备图构建），只有「写集群」的动作才受 leader 约束。这与 u3-l1 讲的「热备 + 单写」策略一致。
  - `Cache: buildManagerCache(cfg)` —— 缓存选项，本讲 4.2 详解。
- [L532-L534](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L532-L534)：仅当 `HealthConfig.Enabled` 时，把 `HealthProbeBindAddress` 设成 `:<port>`。
- [L536-L539](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L536-L539)：取 REST 配置（`ctlr.GetConfigOrDie()`，读的是 Pod 里挂载的 ServiceAccount 凭证），设置 [L88-L89](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L88-L89) 定义的 `clusterTimeout = 10s`，然后 `manager.New` 真正造出 Manager。
- [L544-L548](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L544-L548)：注册名为 `readyz` 的就绪检查，回调 `healthChecker.readyCheck`（4.3 详解）。
- [L552-L561](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L552-L561)：通过 `mgr.GetFieldIndexer()` 给 `corev1.Pod` 建 `status.podIP` 索引，索引函数是 [internal/framework/controller/index/pod.go:12-19](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/index/pod.go#L12-L19) 的 `PodIPIndexFunc`——它返回 `[]string{pod.Status.PodIP}`。

`createManager` 返回的 `mgr` 随即回到 [StartManager](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L126-L131)（[L128](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L128)），后续用它取 client、APIReader、cache、event recorder，并注册各种 Runnable。

#### 4.1.4 代码实践

**实践目标**：用「读源码」的方式，亲手把「flag → Config → manager.Options」这一段串起来，理解一个用户配置项如何最终影响 Manager 行为。

**操作步骤**：

1. 打开 [internal/controller/config/config.go:121-129](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L121-L129) 的 `LeaderElectionConfig`，记下 `Enabled`、`LockName` 字段。
2. 回到 [createManager 的 L517-L519](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L517-L519)，确认这两个字段分别灌进了 `LeaderElection` 和 `LeaderElectionID`。
3. 再到 [cmd/gateway/commands.go:167-171](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L167-L171)，看到默认锁名是 `nginx-gateway-leader-election-lock`，它由 `--leader-election-lock-name` 覆盖（回顾 u2-l2 讲的反向布尔 `--disable-leader-election`）。
4. 追问自己：如果 `Enabled=false`，`manager.New` 会怎样？——controller-runtime 在不开 leader election 时，所有副本都会以 leader 身份运行（适合单副本）。

**需要观察的现象 / 预期结果**：你能画出一条 `--leader-election-lock-name <名字>` → `cfg.LeaderElection.LockName` → `options.LeaderElectionID` → `manager.New` 的数据流，并解释「锁名相同的多个 NGF 实例会互相竞争同一个 leader 租约」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Controller.NeedLeaderElection` 要显式设成 `false`？不设会怎样？

> **答案**：controller-runtime 的默认值会让控制器只在 leader 上运行。但 NGF 希望非 leader 副本也跑控制器、也构建图，做「热备」；真正的「写集群」动作由更外层的 `CallFunctionsAfterBecameLeader` 包装器在当选 leader 后才打开（见 u3-l1）。所以必须把控制器的 leader 要求显式关掉，否则非 leader 副本根本不会 reconcile。

**练习 2**：假设你想让 NGF 多 watch 一种全新的自定义资源 `Foo`，从 scheme 的角度，至少要改哪一处？

> **答案**：至少要在 [init()](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L96-L124) 里调用 `scheme` 的 `AddToScheme`/`Install` 把 `Foo` 的 GVK↔Go 类型注册进去；否则 Cache 反序列化时会报「no kind registered」。

---

### 4.2 缓存配置：transform 与 namespace 过滤（cache transform）

#### 4.2.1 概念说明

Manager 的 Cache 用 informer 把 watch 到的资源原样存进内存。NGF 要 watch 的资源量很大（GatewayClass 集群级、各命名空间的 Secret/ConfigMap/Service……），而且很多资源「胖」——典型例子是 `Secret`：它的 `Data` 可以装任意内容，但 NGF 只用到十来个固定键（TLS 证书、CA、Basic Auth 凭证、Plus 授权 JWT 等）。

cache 的 **transform** 机制，就是在 informer 把对象塞进本地存储**之前**，对对象做一次「外科手术」——删掉用不到的字段。因为所有下游消费者（控制器、Client 读）拿到的都是「已经瘦过身」的对象，所以这层裁剪**对业务逻辑完全透明**：业务代码本来就只读那几个键，删掉其余键它感知不到，但内存占用却大幅下降。

NGF 还用 **namespace 过滤**（`DefaultNamespaces`）把缓存范围收窄到指定命名空间，进一步降低多租户/受限部署下的内存与 watch 压力。

> 术语：`TransformFunc` 是 controller-runtime（`k8s.io/client-go/tools/cache`）定义的「`func(any) (any, error)`」类型；NGF 的三个函数都返回这种闭包。

#### 4.2.2 核心流程

`buildManagerCache` 组装一个 `cache.Options`，分三步——[internal/controller/manager.go:570-597](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L570-L597)：

```text
buildManagerCache(cfg):
  1. 若 WatchNamespaces 非空：
       - 建一个 namespaces map
       - 兜底把「控制面 Pod 自己的 namespace」也加进去（否则控制面读不到自己的配置）
       - 设到 cacheOpts.DefaultNamespaces
  2. cacheOpts.DefaultTransform = cache.TransformStripManagedFields()
       —— 对所有资源统一剥掉 managedFields
  3. cacheOpts.ByObject = {
       GatewayClass: TransformGatewayClass(controllerName)
       Secret:        TransformSecret()
       ConfigMap:     TransformConfigMap()
     }
       —— 对这三类资源做专属裁剪
```

三类专属 transform 的行为：

| Transform | 输入 | 保留什么 | 丢弃什么 | 设计意图 |
| --- | --- | --- | --- | --- |
| `TransformGatewayClass` | `*gatewayv1.GatewayClass` | 匹配本控制器名的 GC 的完整 Spec | 不匹配的 GC 的 Spec 清空；一律剥 managedFields | 非本控制器的 GatewayClass 也要进缓存（保证 watch/列举正常、cache key 完整），但内容清零省内存 |
| `TransformSecret` | `*corev1.Secret` | `secretKeys` 里命中的键 | 其余 Data 键；managedFields | 一个集群里 Secret 数量可能极多、单个可能很大，但 NGF 只用十几个固定键 |
| `TransformConfigMap` | `*corev1.ConfigMap` | `configMapKeys` 里命中的键 | 其余 Data/BinaryData；managedFields | 只关心 bootstrap 配置相关的几个键 |

一个关键设计点：当目标键都没命中时，这些 transform **不是把对象整个丢弃，而是把它的 `Data` 置 nil、保留对象壳**。因为 informer 需要对象作为「cache key」存在，控制器才能收到它的事件、判断它是否被引用。这叫「**保留 cache key 完整性，同时最小化存储字节**」。

为什么这对业务透明？举例：`TransformSecret` 只保留 [secretKeys](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/cache/transform.go#L13-L29) 里的键，而业务侧（`shared/secrets`、graph 解析）也**只可能**通过这些常量键去读 Secret（见 [secrets.go:33-69](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/shared/secrets/secrets.go#L33-L69) 的 `AuthKey`、`TLSCertKey` 等）。读和裁两边用的是**同一套键常量**，所以裁掉的键业务永远不会去访问——这就是「不改业务逻辑却降内存」的根本原因。

#### 4.2.3 源码精读

先看 `buildManagerCache`——[internal/controller/manager.go:570-597](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L570-L597)：

- [L572-L581](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L572-L581)：namespace 过滤。注意 [L573-L575](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L573-L575) 的兜底——如果用户给的 `WatchNamespaces` 里没有控制面所在 namespace，会自动追加，因为控制面要能读到自己的配置资源。
- [L583](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L583)：`DefaultTransform = cache.TransformStripManagedFields()`——**所有**资源统一剥 managedFields。managedFields 是服务端字段管理元数据，体积常常比业务字段还大，而 NGF 完全不读它。
- [L584-L594](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L584-L594)：`ByObject` 给 GatewayClass/Secret/ConfigMap 挂各自的 transform。

再看三个 transform 的实现——[internal/framework/controller/cache/transform.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/cache/transform.go)：

- [L13-L29 `secretKeys`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/cache/transform.go#L13-L29)：列出 NGF 关心的 Secret 键——`auth`（Basic Auth）、`ca.crt`（CA）、TLS 证书与私钥、`license.jwt`（Plus 授权）、`dataplane.key`（NGINX One Console）、WAF bundle 的 `username`/`password`/`token`、`seaweedfs_admin_secret`（PLM S3）、docker registry 凭证等。
- [L31-L37 `configMapKeys`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/cache/transform.go#L31-L37)：`ca.crt` 以及 NGINX Agent 的 `nginx-agent.conf`、`main.conf`、`events.conf`、`mgmt.conf` 等 bootstrap 配置键（对应 [configmaps.go:17-26](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/shared/configmaps/configmaps.go#L17-L26)）。
- [L45-L61 `TransformGatewayClass`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/cache/transform.go#L45-L61)：类型断言保护（不是 GatewayClass 原样返回）→ 若 `Spec.ControllerName` 不匹配本控制器，把 `Spec` 清成空结构体 → 一律 `SetManagedFields(nil)`。
- [L67-L93 `TransformSecret`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/cache/transform.go#L67-L93)：遍历 `secretKeys` 把命中的键收集到新 map；若一个都没命中（`found==false`），`Data=nil`；否则替换成精简 map。这保证「无关 Secret」也占极少内存。
- [L99-L143 `TransformConfigMap`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/cache/transform.go#L99-L143)：逻辑同上，分别处理 `Data`（字符串）与 `BinaryData`（字节），都没命中则双双置 nil。

#### 4.2.4 代码实践

**实践目标**：亲手验证「transform 裁剪对业务透明」这一论断，方法是比对 transform 保留的键集合与业务侧读取的键常量，确认两者同源。

**操作步骤**：

1. 打开 [transform.go 的 `secretKeys`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/cache/transform.go#L13-L29)，列出它保留的 14 个键。
2. 打开 [secrets.go:33-69](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/shared/secrets/secrets.go#L33-L69)，确认 NGF 业务侧定义的键常量（`AuthKey="auth"`、`CAKey="ca.crt"`、`TLSCertKey`、`TLSKeyKey`、`LicenseJWTKey="license.jwt"` 等）与 transform 保留的键**一一对应**。
3. 用 `Grep` 在 `internal/controller` 下搜索 `secret.Data[`，观察业务代码读取 Secret 时用的键名——预期全部落在 `secretKeys` 集合内，没有任何一处会读到被裁掉的键。
4. 思考一个反例：假设 NGF 未来新增一个功能，要从 Secret 读一个名为 `"new-thing"` 的键，却忘了把它加进 `secretKeys`——会发生什么？

**需要观察的现象 / 预期结果**：grep 结果会显示业务侧只通过 `secrets.*Key` 常量访问 Secret，而这些常量都在 `secretKeys` 里。反例的结论是：那个键在进缓存前就被 transform 删掉了，业务读到的会是空值/不存在——**这正是「transform 与业务必须共用同一套键」的约束所在**，新增键时必须同步更新 [transform.go 的 `secretKeys`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/cache/transform.go#L13-L29)。

> 说明：以上为源码阅读型实践，无需运行集群即可完成推理；若想实证内存差异，可在带 envtest 的集成测试里对比「开/关 transform」时控制面进程的内存占用（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`TransformSecret` 在没有任何目标键命中时，为什么把 `Data` 置 nil 而不是返回 `nil` 让 informer 丢弃这个对象？

> **答案**：丢弃对象会破坏 cache 的完整性——informer 需要每个被 watch 的对象作为存储条目存在，控制器才能收到它的增删改事件、并判断它是否被 ReferenceGrant 等资源引用。置 nil 保留了「对象存在」这个事实，只去掉无用的负载字节，是「保 key、删 value」的折中。

**练习 2**：`DefaultTransform` 与 `ByObject` 里的 `Transform` 是什么关系？两者会都执行吗？

> **答案**：controller-runtime 会对每个对象先应用 `DefaultTransform`，再应用该对象类型在 `ByObject` 里指定的 `Transform`（具体叠加顺序以 controller-runtime 实现为准）。NGF 让 `DefaultTransform` 统一剥 managedFields，再让三类资源各自做字段级裁剪——所以 GatewayClass/Secret/ConfigMap 既被剥了 managedFields，又被做了专属裁剪。

**练习 3**：`WatchNamespaces` 为什么要在末尾兜底加入控制面自己的 namespace？

> **答案**：控制面需要读自己命名空间下的配置资源（如 `NginxGateway` 控制面配置、mTLS 证书 Secret）。如果用户配置 `WatchNamespaces` 时漏掉了它，控制面会读不到自身配置而无法正常工作，所以代码在 [L573-L575](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L573-L575) 强制补上。

---

### 4.3 Metrics 与 Health server 的挂载（metrics/health 端口）

#### 4.3.1 概念说明

controller-runtime Manager 内置两个 HTTP 服务：

- **Metrics server**：暴露 Prometheus 指标（控制器自身的指标 + NGF 注册的业务指标）。
- **Health probe server**：暴露 `/healthz`、`/readyz` 等探针端点，供 Kubernetes 的 liveness/readiness probe 调用。

NGF 让这两个服务**可开关**：默认都开，但都能通过 flag 关掉（回顾 u2-l2 的反向布尔 `--disable-metrics`、`--disable-health`）。两个服务默认端口分别是 **9113**（metrics，见 [config.go:10 `DefaultNginxMetricsPort`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L10) 与 [commands.go:159](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L159)）和 **8081**（health，见 [commands.go:164](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L164)）。

本模块的关键洞见是：`readyz` 不是「进程起来了」就返回就绪，而是**等到第一张图构建完成**后才返回就绪——这避免了「控制面还没把集群状态翻译成 NGINX 配置，就被 Service 标记为 Ready 接流量」的窗口期。

#### 4.3.2 核心流程

两个服务的挂载点分布在 `createManager` 与 `getMetricsOptions` 里：

```text
metrics 链路:
  getMetricsOptions(cfg.MetricsConfig):
    默认 BindAddress="0"   // controller-runtime 里 "0" 表示禁用 metrics server
    若 Enabled:
       若 Secure: SecureServing=true   // HTTPS
       BindAddress = ":<port>"         // 默认 9113
  → 灌进 manager.Options.Metrics

health 链路:
  若 cfg.HealthConfig.Enabled:
    options.HealthProbeBindAddress = ":<port>"   // 默认 8081
  manager.New(...) 之后:
    mgr.AddReadyzCheck("readyz", healthChecker.readyCheck)
```

`readyCheck` 的判定逻辑来自 [internal/controller/health.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/health.go)：内部维护一个布尔 `ready`，初始为 `false`，此时 `readyCheck` 返回 `"control plane is not yet ready"` 错误；直到事件处理器第一次把图构建出来、调用 `setAsReady()` 把 `ready` 置 `true` 并关闭 `readyCh`，`/readyz` 才返回 200。

#### 4.3.3 源码精读

- **Metrics 选项**——[getMetricsOptions: L1405-L1416](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L1405-L1416)：
  - [L1406](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L1406)：默认 `BindAddress: "0"`。在 controller-runtime 的 metrics server 里，`"0"` 表示**不监听任何端口**（禁用 metrics server）。所以 NGF 的 metrics 默认是「关」的，只有当 `MetricsConfig.Enabled` 为真时才真正绑定端口。
  - [L1408-L1413](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L1408-L1413)：开启时，按 `Secure` 决定是否 HTTPS，并把 `BindAddress` 设成 `:<port>`。
  - 这份 `MetricsConfig` 来自 [config.go:103-111](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L103-L111) 的 `Port`/`Enabled`/`Secure` 三字段。
- **业务指标注册**：metrics server 一旦开启，业务侧指标在 [createMetricsCollector: L290-L301](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L290-L301) 注册到 `metrics.Registry`（`MustRegister(handlerCollector)`）。关闭时返回 Noop 采集器，零开销。
- **Health 探针**——[createManager L532-L534](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L532-L534)：仅当 `HealthConfig.Enabled` 时设 `HealthProbeBindAddress`；这份配置来自 [config.go:113-119](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L113-L119) 的 `HealthConfig`。
- **readyz 注册**——[L544-L548](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L544-L548)：`mgr.AddReadyzCheck("readyz", healthChecker.readyCheck)`。
- **就绪判定**——[health.go:24-35 `readyCheck`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/health.go#L24-L35)：用读写锁保护 `ready`，未就绪时返回错误；就绪逻辑在 [health.go:37-44 `setAsReady`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/health.go#L37-L44) 置位并 `close(readyCh)`。`readyCh` 还被遥测任务消费（[createTelemetryJob](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L1229-L1274) 传入 `healthChecker.getReadyCh()`），确保首次上报发生在就绪之后。

#### 4.3.4 代码实践

**实践目标**：理解 metrics/health 的「可关闭 + 默认端口」语义，并能据此解释一个部署现象。

**操作步骤**：

1. 阅读 [getMetricsOptions L1405-L1416](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L1405-L1416)，确认「`BindAddress="0"`」在 controller-runtime 中等价于「禁用 metrics server」。
2. 对照 [config.go 的 MetricsConfig/HealthConfig](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L103-L119)，列出三对「字段 ↔ flag」：`Port↔--metrics-port`、`Enabled↔--disable-metrics(取反)`、`Secure↔--metrics-secure`；health 同理。
3. 假设运维部署时把 readinessProbe 指向 `http://:8081/readyz`，问：Pod 刚起来但第一张图还没构建完时，这个探针会返回什么？

**需要观察的现象 / 预期结果**：

- 若用 `--disable-metrics` 启动，控制面根本不会监听 9113，`curl :9113/metrics` 会连接失败——因为 `BindAddress` 保持 `"0"`。
- readinessProbe 在图构建完成前会收到 503（`readyCheck` 返回错误），直到首次图构建完才转 200。**这意味着 NGF 在「真正把集群状态翻译成配置」之前，不会被外部 Service 接入流量**。

> 说明：以上结论由源码直接推出；如要在真实集群验证，可用 `kubectl describe pod` 看 readinessProbe 的 `last state`，并在启动初期 `curl <pod-ip>:8081/readyz` 观察从 503 到 200 的跳变（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么不把 `/readyz` 直接设成「进程启动即就绪」？

> **答案**：进程起来 ≠ 配置就绪。NGF 起来后还要先 watch 全量资源、构建第一张图、把配置下发到 NGINX。若在此之前就被标记 Ready，流量会被路由到一个「还没下发配置」的数据面，导致请求失败。所以 `readyz` 绑定到「首次图构建完成」这个更有意义的就绪信号。

**练习 2**：metrics server 的 `BindAddress` 默认值 `"0"` 代表什么？为什么 NGF 要默认禁用它、再按 flag 开启？

> **答案**：`"0"` 在 controller-runtime metrics server 里表示不绑定端口（禁用）。这样设计是因为暴露指标是「可选能力」，应当显式开启；同时这也让 `getMetricsOptions` 在关闭分支下不需要任何额外配置，逻辑更简单、更安全（默认不对外开放端口）。

**练习 3**：`metrics.Registry.MustRegister(handlerCollector)`（[L298](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L298)）只在什么条件下执行？

> **答案**：只在 `cfg.MetricsConfig.Enabled` 为真时执行；否则 [createMetricsCollector](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L290-L301) 返回 `NewControllerNoopCollector()`，既不注册采集器、也不产生指标采集开销。

---

## 5. 综合实践

**任务**：用一个完整推理，把本讲三个模块串起来，回答「cache transform 如何在不改业务逻辑的情况下降低内存占用」。

请按以下步骤完成（源码阅读 + 推理型，无需运行集群）：

1. **定位裁剪边界**。打开 [buildManagerCache](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L570-L597)，画出三层裁剪：① `DefaultTransform` 剥 managedFields（全资源）；② `ByObject` 三类专属裁剪；③ `DefaultNamespaces` 限定命名空间。
2. **证明「对业务透明」**。以 Secret 为例：
   - 列出 [transform.go `secretKeys`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/cache/transform.go#L13-L29) 保留的键。
   - 用 `Grep` 在 `internal/controller/state/graph` 下搜 `secrets.` 的键常量引用，确认业务侧只通过这些常量读 Secret。
   - 得出结论：transform 删的键，业务侧从不访问，故透明。
3. **量化收益（定性）**。设想一个集群有 5000 个 Secret，其中大部分是 docker-registry 凭证或应用密钥（单个可能几 KB 到 1MB）。说明：① 不裁剪时，这 5000 个 Secret 的 `Data` 全量进内存；② 裁剪后，非 NGF 相关的 Secret `Data=nil`，仅保留对象壳。用文字估算内存量级差异。
4. **关联就绪信号**。回到 [health.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/health.go)，说明正是因为裁剪后的精简缓存仍能正常驱动图构建（业务逻辑无感知），`readyz` 才能在「首次图构建完成」时可靠地翻成就绪——裁剪不影响正确性，只影响内存。

**预期产出**：一段 200 字左右的说明，包含「三层裁剪」「键集合同源」「内存量级对比」三个论点，并给出「transform 与业务必须共用同一套键常量」这一维护约束。

## 6. 本讲小结

- `createManager` 用一个 `manager.Options` 把 controller-runtime Manager 装配出来：scheme（决定能 watch 哪些资源）、logger、metrics、leader election、`Controller.NeedLeaderElection=false`（让非 leader 也跑控制器做热备）、cache。
- Manager 之外还会顺手做两件附属装配：注册 `readyz` 就绪检查、给 Pod 的 `status.podIP` 建 field index（供后续校验 Agent 连接）。
- 缓存裁剪有三层：`DefaultNamespaces` 限定命名空间（并兜底加入控制面自身 namespace）、`DefaultTransform` 剥所有资源的 managedFields、`ByObject` 对 GatewayClass/Secret/ConfigMap 做字段级裁剪。
- cache transform 在「对象进缓存之前」生效，因此对业务透明；其正确性前提是「transform 保留的键集合」与「业务读取的键常量」同源——新增键必须两边同步。
- metrics server 默认 `BindAddress="0"`（禁用），按 `--metrics-port`（默认 9113）开启；health server 默认 8081，二者均可通过反向布尔 flag 关闭。
- `readyz` 的就绪条件不是「进程启动」，而是「首次图构建完成」——避免在配置真正下发前就被接入流量。

## 7. 下一步学习建议

- 接着读 **u3-l3「控制器注册与 CRD 发现」**：本讲只造好了 Manager 这个「容器」，下一讲讲清楚各类资源控制器（`controller.Register`）如何挂到这个 Manager 上，以及如何按 CRD 是否存在动态启用/禁用控制器。
- 想深入 cache 与过滤的更多细节，可跳读 **u4-l1「自研框架：控制器抽象与过滤机制」**，看 predicate、namespacedName filter、field index 如何在控制器层进一步减少无谓事件。
- 对 leader 选举语义感兴趣，可提前翻 **u3-l4「Leader Election 与 Runnables」**，理解 `LeaderOrNonLeader` / `Leader` / `CallFunctionsAfterBecameLeader` 三种 Runnable 包装如何与 `NeedLeaderElection=false` 配合实现「热备 + 单写」。
- 若想验证缓存内存收益，可在 **u13-l1「测试体系」** 介绍的 envtest 集成测试基础上，构造大量 Secret 观察 transform 开关前后的进程内存差异。
