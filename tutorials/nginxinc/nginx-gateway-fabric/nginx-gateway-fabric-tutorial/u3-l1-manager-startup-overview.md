# StartManager 全景：控制面如何被装配

> 所属单元：u3「Manager 组装与控制器注册」
> 依赖：u2-l2「controller 命令与运行时配置」

## 1. 本讲目标

在 u2-l2 中我们看到，`controller` 子命令的 `RunE` 把四十多个 flag 汇聚成一个 `config.Config`，然后在最后一行调用 `controller.StartManager(conf)` 把「接力棒」交给控制面。从这一行开始，整个 NGF 控制面才真正「上电」。

本讲的目标是把 `StartManager` 这一个函数讲透。读完本讲你应当能够：

1. 说出 `StartManager` 把控制面装配出来的**整体顺序**——先建什么、后建什么、为什么这个顺序。
2. 识别这一函数里散落的各个 `create*` 工厂函数，知道每个工厂**产出什么对象**、**消费哪些配置**、**注册到哪**。
3. 理解 NGF 使用的**依赖注入（Dependency Injection）**风格：对象不自己去找依赖，而是由 `StartManager` 在装配线上把依赖「喂」进去。
4. 看清 controller-runtime 的 **Runnable 注册机制**：哪些组件「所有 Pod 都跑」（`LeaderOrNonLeader`），哪些「只有 leader 跑」（`Leader`）。

本讲只讲「**怎么装配**」，不深入每个子系统的内部实现——那是 u3-l2/u3-l3/u3-l4 以及后续单元的主题。本讲给你的是一张「控制面装配线的总装图」。

## 2. 前置知识

- **controller-runtime Manager**：来自 `sigs.k8s.io/controller-runtime`，是 K8s 控制器的「运行容器」。它封装了：与 apiserver 的连接、本地缓存（cache/informer）、工作队列、metrics/health HTTP 服务、leader 选举协调器，以及一个「Runnable 注册表」。你把一个个 Runnable（控制器、后台任务）`Add` 进去，调一次 `mgr.Start(ctx)` 它们就一起跑起来。NGF 不直接用 `controller-runtime` 自带的 Reconciler 模式，而是用自研框架（见 u4-l1），但**底座**仍是 controller-runtime Manager。
- **Runnable 与 LeaderElectionRunnable**：凡是实现了 `Start(ctx context.Context) error` 的对象都可以被 Manager 托管。如果还实现了 `NeedLeaderElection() bool` 并返回 `true`，那么这个 Runnable 只在当前 Pod 是 leader 时才启动。
- **依赖注入（DI）**：一种「对象不自己 `new` 依赖、而是由外部传入」的组装方式。在 Go 里通常表现为「构造函数接收一堆参数」或「往结构体的字段里塞值」。好处是组件可单测（传 fake 依赖）、且组装关系一目了然。
- **Scheme**：controller-runtime 需要知道「GVK（Group/Version/Kind）↔ Go 类型」的映射表，才能把 apiserver 返回的 JSON 反序列化成 Go 对象。这张表由 `init()` 里的 `AddToScheme/Install` 调用拼装。
- **config.Config 结构族**：见 u2-l2，是 `StartManager` 的唯一输入。

## 3. 本讲源码地图

本讲几乎所有内容都集中在一个文件里：

| 文件 | 作用 |
|------|------|
| [internal/controller/manager.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go) | **本讲主角**。包含 `StartManager`、`init()` 方案注册、以及全部 `create*`/`register*`/`prepare*` 工厂与辅助函数 |

为说明装配链路上涉及的对象，还要参照以下文件（**只读它们的类型定义，不深入实现**）：

| 文件 | 在本讲中的作用 |
|------|------|
| [internal/controller/config/config.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go) | `config.Config`——装配线的唯一输入 |
| [internal/controller/health.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/health.go) | `graphBuiltHealthChecker`——Pod 就绪探针，是装配线上「最早被创建」的对象之一 |
| [internal/controller/handler.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go) | `eventHandlerConfig` 与 `eventHandlerImpl`——装配线上**依赖最多**的对象，几乎所有组件都要注入给它 |
| [internal/framework/runnables/runnables.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/runnables.go) | `Leader` / `LeaderOrNonLeader` / `CallFunctionsAfterBecameLeader`——Runnable 的 leader 语义包装器 |
| [internal/framework/runnables/cronjob.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/cronjob.go) | `CronJob`——周期任务的通用载体，telemetry 用到 |
| [internal/framework/events/loop.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go) | `EventLoop`——装配线上「最后被注册、却是业务核心」的 Runnable |

## 4. 核心概念与源码讲解

### 4.1 StartManager：控制面的装配主轴

#### 4.1.1 概念说明

把 `StartManager` 想象成一条**汽车总装流水线**：原材料是 `config.Config`（一箱配置参数），产成品是一台正在运行的 NGF 控制面进程。流水线上有一系列工位，每个工位造好一个部件（控制器注册表、配置生成器、Agent gRPC 服务、provisioner、telemetry……），并把这些部件用线束（依赖注入）连接起来。最后一个工位按下启动键——`mgr.Start(ctx)`——所有部件同时开始工作。

这条流水线有两个鲜明特点：

- **顺序很重要**：后造的部件往往要引用先造的部件。比如 `eventHandler` 要引用 `processor`、`nginxUpdater`、`nginxProvisioner`、`statusQueue`……所以这些必须先于 `eventHandler` 创建。
- **集中装配、分散实现**：`StartManager` 只负责「把谁和谁连起来」，每个部件**内部**怎么工作是部件自己的事（在各自的子系统文件里）。这正是「装配（wiring）」与「实现（implementation）」分离的设计。

#### 4.1.2 核心流程

`StartManager(cfg)` 的执行可以分成 **6 个阶段**，按代码出现顺序如下：

```text
阶段 0：准备基础设施（在任何业务对象之前）
  ├─ newGraphBuiltHealthChecker()   → healthChecker（就绪探针，最先造）
  ├─ createManager(cfg, healthChecker) → mgr（controller-runtime Manager 底座）
  ├─ recorder     = mgr.GetEventRecorder(...)   （K8s 事件记录器）
  ├─ logLevelSetter                     （运行时可调日志级别）
  └─ ctx = SetupSignalHandler()         （带信号处理的根 context）

阶段 1：注册控制器（watch 哪些资源）
  └─ registerControllers(...)  → discoveredCRDs（集群里实际存在哪些 CRD）

阶段 2：构造「无状态/纯计算」的处理器与校验器
  ├─ mustExtractGVK, genericValidator
  ├─ createPolicyManager(...)      （策略 CRD 的复合校验器）
  ├─ createPlusSecretMetadata(...) （Plus 证书元数据校验）
  ├─ createWAFFetcher / createPLMFetcher
  └─ state.NewChangeProcessorImpl(...) → processor（变更捕获器）

阶段 3：构造「有状态/带副作用」的下发与状态组件
  ├─ status.NewUpdater / NewLeaderAwareGroupUpdater → groupStatusUpdater
  ├─ licensing.NewDeploymentContextCollector → deployCtxCollector
  ├─ status.NewQueue → statusQueue
  ├─ createAgentServices(...) → nginxUpdater + gRPC server（此时已注册 Runnable）
  ├─ createAndRegisterProvisioner(...) → nginxProvisioner（已注册 Runnable）
  └─ createWAFPollerManager(...) → wafPollerManager（Plus 才有）

阶段 4：装配事件处理器与事件循环
  ├─ newEventHandlerImpl(...) → eventHandler（把上面几乎所有部件注入进去）
  ├─ prepareFirstEventBatchPreparerArgs + NewFirstEventBatchPreparerImpl → firstBatchPreparer
  ├─ events.NewEventLoop(...) → eventLoop
  ├─ mgr.Add(LeaderOrNonLeader{eventLoop})          （事件循环：所有 Pod 都跑）
  └─ mgr.Add(CallFunctionsAfterBecameLeader{...})   （成为 leader 后才启用的开关）

阶段 5：注册可观测性与启动
  ├─ registerTelemetry(...) （只读 processor/eventHandler/healthChecker）
  ├─ 打印 "Starting manager" 日志
  └─ return mgr.Start(ctx)  （阻塞，直到 ctx 关闭）
```

注意一个关键点：**`mgr` 在阶段 0 就造好了，但直到阶段 5 的 `mgr.Start(ctx)` 才真正运行**。中间阶段 1~4 的所有 `mgr.Add(...)` 只是把 Runnable「登记」到 Manager 的注册表里。`mgr.Start` 一旦被调用，会按各自是否需要 leader 选举，分批把它们启动起来。

#### 4.1.3 源码精读

先看 `StartManager` 的函数签名与最初几行——它接收**唯一参数** `config.Config`，这正印证了 u2-l2 的结论：所有 flag 最终汇聚成这一个结构体传进来：

[internal/controller/manager.go:126-149](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L126-L149) —— `StartManager` 入口。先造 `healthChecker` 与 `mgr`，再用 `mgr` 派生出 `recorder`、日志级别设置器与根 `ctx`。注意 `ctx := ctlr.SetupSignalHandler()` 产生的 context 会随进程收到 SIGTERM/SIGINT 而关闭，整个控制面的优雅退出都依赖它。

再看阶段 0 里的 **Scheme 注册**。Scheme 是 Manager 能正确反序列化 K8s 资源的前提，它由包级 `init()` 在 `StartManager` 被调用之前就装填完毕：

[internal/controller/manager.go:94-124](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L94-L124) —— `var scheme` 与 `init()`。这里把 Gateway API（v1/v1alpha2/v1beta1）、核心 API（Pod/Service/Secret/EndpointSlice 等）、NGF 自有 CRD（v1alpha1/v1alpha2）、Inference 扩展，以及 WAF 的非结构化 APPolicy/APLogConf 类型全部 `Install`/`AddToScheme` 进同一个 `scheme`。`utilruntime.Must(...)` 表示「注册失败就 panic」——这是启动期的硬性自检。

接着是阶段 1~4 的核心。`registerControllers` 产出的 `discoveredCRDs` 会被**后续多个工位**复用（决定哪些策略校验器、哪些首批事件要加载），这是装配线上「先探测、后按探测结果分支」的典型做法：

[internal/controller/manager.go:146-194](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L146-L194) —— 从 `registerControllers` 拿到 `discoveredCRDs`，接着基于它创建 `mustExtractGVK`、`policyManager`、`plusSecrets`、`wafFetcher`、`plmFetcher`，最后用这些都作为字段喂给 `state.NewChangeProcessorImpl`。注意 `processor` 的配置里出现了 `PolledWAFBundles` 这种**闭包**（延迟到 `wafPollerManager` 真正创建后再读取）——这是解决循环依赖的小技巧（`processor` 要查 poller，但 poller 还没造，先用闭包包住）。

最后看阶段 4~5 的收尾——`eventLoop` 被注册为 `LeaderOrNonLeader` Runnable，而「成为 leader 后才启用的开关」用 `CallFunctionsAfterBecameLeader` 打包：

[internal/controller/manager.go:264-287](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L264-L287) —— 注册事件循环、注册 leader-only 启用函数、注册 telemetry，最后 `return mgr.Start(ctx)`。`mgr.Start` 会阻塞，所以 `StartManager` 本身也阻塞在这里，直到进程收到退出信号。

#### 4.1.4 代码实践

**实践目标**：验证「装配顺序由依赖关系决定」这一论断，亲手找到一处「后造引用先造」的证据。

**操作步骤**：

1. 打开 [internal/controller/manager.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go)，定位到 `StartManager`。
2. 找到 `nginxUpdater, err := createAgentServices(...)`（约 L210）这一行，记下它产出的 `nginxUpdater`。
3. 向下搜索 `nginxUpdater` 在本函数内**被后续哪些行消费**：你会看到它被传给 `createAndRegisterProvisioner`（L215）、`createWAFPollerManager`（L220）、以及 `eventHandlerConfig.nginxUpdater`（L224）和 `nginxDeployments: nginxUpdater.NginxDeployments`（L248）。
4. 反向思考：为什么 `createAgentServices` 必须在 `createAndRegisterProvisioner` 之前？因为 provisioner 需要 `nginxUpdater.NginxDeployments`（数据面 Deployment 存储）作为依赖。

**需要观察的现象**：每个下游消费点都出现在 `nginxUpdater` 创建之后，没有一处「先用后造」。

**预期结果**：你应当能画出一条 `nginxUpdater → {nginxProvisioner, wafPollerManager, eventHandler}` 的依赖箭头，确认装配顺序不是随意的，而是被依赖关系「拓扑排序」出来的。

> 待本地验证：以上结论基于静态阅读，未实际运行。若你在 IDE 里对 `nginxUpdater` 做「查找用法」，应得到与上述一致的结果集。

#### 4.1.5 小练习与答案

**练习 1**：`ctx := ctlr.SetupSignalHandler()` 产生的 context 被传给了 `registerControllers`、`createAndRegisterProvisioner`、`createWAFPollerManager`、`eventHandlerConfig`、`mgr.Start`。如果进程收到 SIGTERM，这些组件会如何收到通知？

**参考答案**：`SetupSignalHandler` 返回的 context 在收到信号时会被 cancel。所有持有该 `ctx` 的 Runnable 在 `Start(ctx)` 内部通常以 `for { select { case <-ctx.Done(): return } }` 监听它，从而得知该退出。`mgr.Start(ctx)` 自身也会因 ctx 关闭而停止所有 Runnable。

**练习 2**：为什么 `healthChecker` 是装配线上**最早**被创建的对象之一（甚至在 `mgr` 之前）？

**参考答案**：因为 `healthChecker` 既要被 `createManager` 用作 `readyz` 就绪探针（L545），又要被 `eventHandler` 在「首个图构建完成后」调用 `setAsReady()`（见 health.go），还要被 `registerTelemetry` 用来延迟首次上报（L455 `healthChecker.getReadyCh()`）。它是「跨阶段共享」的状态对象，所以必须先造。

---

### 4.2 create* 工厂函数族

#### 4.2.1 概念说明

`StartManager` 里散布着一组以 `create` / `register` / `prepare` / `new` 开头的函数。它们是装配线上的「工位操作工」，每个负责**造好一个部件**或**完成一道装配工序**，并把结果返回给 `StartManager`。这种把「如何构造某对象」封装成独立函数的写法，业界称为**工厂函数（factory function）**模式。

NGF 选工厂函数而非 `init()` 或全局单例，有两个好处：

- **可测**：每个工厂只接收它需要的依赖（参数），单测时可以传 mock。
- **装配顺序显式**：哪个工厂先调、后调，全写在 `StartManager` 里一眼可见，没有隐式的初始化顺序坑。

按「产出物是否需要注册到 Manager」，这些工厂分两类：

- **纯构造型**：只返回对象，不碰 `mgr`。如 `createPolicyManager`、`createWAFFetcher`、`createMetricsCollector`、`prepareFirstEventBatchPreparerArgs`。
- **构造并注册型**：构造对象的同时把它 `mgr.Add` 进 Runnable 注册表。如 `createAgentServices`、`createAndRegisterProvisioner`、`registerTelemetry`。这类函数名里通常带 `register` 或者在注释里写明 "registers ... with the manager"。

#### 4.2.2 核心流程

下表把 `manager.go` 里的主要工厂逐一列出，标注其**输入**、**产出**、**是否注册 Runnable**、**leader 语义**：

| 工厂函数 | 主要输入 | 产出对象 | 注册 Runnable？ | leader 语义 |
|----------|----------|----------|:---:|------|
| `newGraphBuiltHealthChecker` | 无 | `*graphBuiltHealthChecker` | 否（作为探针挂到 mgr） | — |
| `createManager` | cfg, healthChecker | controller-runtime `manager.Manager` | 否（它是底座） | — |
| `registerControllers` | ctx, cfg, mgr, recorder, eventCh… | `discoveredCRDs` map | 是（注册 N 个自研控制器） | 全 Pod（非 leader-only） |
| `createPolicyManager` | mustExtractGVK, validator, cfg | `*policies.CompositeValidator` | 否 | — |
| `createPlusSecretMetadata` | cfg, reader | plusSecrets map（含校验） | 否 | — |
| `createWAFFetcher` / `createPLMFetcher` | cfg / logger | Fetcher / S3 Fetcher | 否 | — |
| `createMetricsCollector` | cfg | handlerMetricsCollector | 否（注册到 Prometheus registry） | — |
| `createAgentServices` | cfg, mgr, statusQueue | `*agent.NginxUpdaterImpl` | **是**：gRPC server | `LeaderOrNonLeader` |
| `createAndRegisterProvisioner` | ctx, cfg, mgr, nginxUpdater, statusQueue, recorder | `*provisioner.NginxProvisioner` | **是**：provisioner event loop | `LeaderOrNonLeader` |
| `createWAFPollerManager` | ctx, cfg, wafFetcher, nginxUpdater, statusQueue, eventCh | `wafpolling.Manager`（Plus 才有，否则 nil） | 否（自管 goroutine） | — |
| `registerTelemetry` | cfg, mgr, processor, eventHandler, healthChecker | 无返回值 | **是**：telemetry job | `Leader`（仅 leader） |
| `createTelemetryJob` | cfg, dataCollector, readyCh | `*runnables.Leader` | 由 `registerTelemetry` 负责注册 | `Leader` |
| `prepareFirstEventBatchPreparerArgs` | cfg, discoveredCRDs | objects, objectLists | 否 | — |

> 「leader 语义」一列的含义见 4.3 节：`LeaderOrNonLeader` = 每个 Pod 副本都跑；`Leader` = 仅 leader 副本跑。

#### 4.2.3 源码精读

**（1）`createManager`：建造底座。** 它是所有后续工厂的前提，因为它返回的 `mgr` 既是 cache/client/recorder 的提供者，也是 Runnable 的注册表：

[internal/controller/manager.go:509-564](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L509-L564) —— 关键看点有三：① 把 `scheme`（阶段 0 装好的类型表）传进 `manager.Options`；② 配置 leader 选举（`LeaderElection`/`LeaderElectionID`/`LeaderElectionReleaseOnCancel: false`，注释解释了为何不释放锁——避免新 leader 抢跑 Leader-only 任务）；③ 把 `Controller.NeedLeaderElection` 显式设为 `false`（L527），意味着**所有自研控制器在非 leader Pod 上也运行**，这是 NGF 的刻意设计。最后还会装一个 Pod IP 索引器（L552-561），供 Agent gRPC 连接校验使用。

**（2）`createAgentServices`：构造并注册型工厂的样板。** 它同时产出 `nginxUpdater` 并把 gRPC server 注册进 Manager：

[internal/controller/manager.go:304-341](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L304-L341) —— 注意它内部调用 `mgr.Add(&runnables.LeaderOrNonLeader{Runnable: grpcServer})`（L336），把 server 包成「所有 Pod 都跑」的 Runnable。`grpcServerPort = 8443`（见 L91）是控制面↔数据面 gRPC 的固定端口。

**（3）`createWAFPollerManager`：条件工厂。** 它根据 `cfg.Plus` 决定是否真的造对象，非 Plus 直接返回 `nil`：

[internal/controller/manager.go:393-427](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L393-L427) —— 注意它产出的对象在 `StartManager` 里被**回填**给先前创建的 `processor` 的 `PolledWAFBundles` 闭包所引用（见 4.1.3）。这是一个「先声明依赖（闭包）、后提供实现」的处理。

**（4）`createTelemetryJob`：把周期任务包装成 Leader Runnable。** 它展示了 `CronJob` 如何与 `Leader` 包装器组合：

[internal/controller/manager.go:1229-1274](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L1229-L1274) —— 返回 `&runnables.Leader{Runnable: runnables.NewCronJob(...)}}`，即「仅在 leader 上运行的周期任务」。其中有一个值得讲的常量——抖动因子：

\[ \text{jitter} \leq \text{jitterFactor} \times \text{period},\quad \text{jitterFactor} = \frac{10}{24 \times 60} \approx 0.0069 \]

含义是：默认上报周期 24 小时，最多叠加约 10 分钟的随机抖动，目的是**避免大量 NGF 实例在同一时刻集中上报**造成后端尖峰。对应的 `ReadyCh` 字段让 telemetry job 在「首个图构建完成」（`healthChecker.getReadyCh()` 关闭）后才启动，避免启动初期上报不完整数据。

#### 4.2.4 代码实践

**实践目标**：用「工厂职责一句话」给每个 `create*` 函数贴标签，培养快速阅读装配代码的能力。

**操作步骤**：

1. 在 [internal/controller/manager.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go) 里，每个工厂函数上方都有一行注释（如 `// createAgentServices creates the NGINX agent updater and gRPC server, and registers the server with the manager.`，见 L303）。把这些注释逐条抄下来。
2. 对照 4.2.2 的表格，给每个工厂补上「纯构造型 / 构造并注册型」的归类。
3. 特别关注 `createAndRegisterProvisioner`（[L343-389](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L343-L389)）——它的名字本身就宣告了「构造**并**注册」两件事，是这类工厂的命名范本。

**需要观察的现象**：函数命名与其实际行为（是否 `mgr.Add`）严格一致；「构造并注册型」函数内部一定出现 `mgr.Add(...)`。

**预期结果**：你得到一份「工厂清单 + 职责 + 是否注册」的速查表，今后读 `StartManager` 时能跳过细节、只看工厂名就能把握结构。

#### 4.2.5 小练习与答案

**练习 1**：`createMetricsCollector`（[L290-301](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L290-L301)）在 metrics 未启用时返回一个 `NoopCollector`。这种「返回空实现」的做法相比「返回 nil」有什么好处？

**参考答案**：返回空实现（no-op）后，调用方（`eventHandler`）无需判空即可直接调用 `ObserveLastEventBatchProcessTime(...)`；若返回 nil，则每个调用点都要写 `if c != nil`，既啰嗦又容易漏判。这是「空对象模式（Null Object Pattern）」。

**练习 2**：为什么 `registerTelemetry` 把任务注册成 `Leader`（仅 leader），而 `createAgentServices` 注册成 `LeaderOrNonLeader`（所有 Pod）？

**参考答案**：telemetry 上报集群级数据，多副本同时上报会造成重复统计，因此只在 leader 上跑；而 gRPC server 是**每个数据面 Pod 都需要连接自己所属的控制面副本**来接收配置，所以每个控制面副本都必须运行自己的 gRPC server，否则非 leader 副本下的数据面 Pod 将无人下发配置。

---

### 4.3 依赖注入与 Runnable 注册

#### 4.3.1 概念说明

把部件造出来只是第一步，还要把它们**正确地连起来**。NGF 在这里用了两套机制：

**依赖注入（DI）**：部件之间不互相 `new`，而是通过**结构体字段**把依赖传进去。装配线上「依赖最多」的对象是 `eventHandler`（`eventHandlerImpl`），它的配置结构 `eventHandlerConfig` 有二十多个字段，几乎把前面所有部件都收编了。读这个结构体字段表，就能反推出「事件处理一轮需要哪些协作者」。

**Runnable 注册**：controller-runtime Manager 把「长期运行的 goroutine」抽象为 Runnable。NGF 自定义了三种语义包装器（见 [internal/framework/runnables/runnables.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/runnables.go)）：

- `LeaderOrNonLeader`：`NeedLeaderElection() == false`，**所有副本都跑**。用于事件循环、gRPC server、provisioner——这些是「处理本副本数据」的核心。
- `Leader`：`NeedLeaderElection() == true`，**仅 leader 副本跑**。用于 telemetry 等全局唯一任务。
- `CallFunctionsAfterBecameLeader`：成为 leader 后**立刻调用一组开关函数**，用于把某些「默认关闭、成为 leader 才打开」的能力激活。

这三个包装器把「该不该在 leader 上运行」的决策从业务代码里剥离出来，集中到装配阶段。

#### 4.3.2 核心流程

**依赖注入的「扇入」关系**（谁把多少依赖喂给 `eventHandler`）：

```text
StartManager 把以下对象注入 eventHandlerConfig：
  ├── nginxUpdater           (阶段3，下发 NGINX 配置)
  ├── nginxProvisioner       (阶段3，按 Gateway 创建数据面)
  ├── metricsCollector       (阶段2/3，指标)
  ├── statusUpdater          (阶段3，groupStatusUpdater，回写状态)
  ├── processor              (阶段2，变更捕获+图)
  ├── serviceResolver        (本函数内 new，Service→Endpoints)
  ├── generator              (本函数内 new，NGINX 配置生成)
  ├── k8sClient              (mgr.GetClient())
  ├── logger / logLevelSetter
  ├── eventRecorder          (mgr.GetEventRecorder)
  ├── deployCtxCollector     (阶段3，Plus 授权上下文)
  ├── graphBuiltHealthChecker(阶段0，就绪探针)
  ├── gatewayPodConfig       (cfg)
  ├── controlConfigNSName    (cfg)
  ├── gatewayCtlrName / gatewayClassName / gatewayInstanceName / plus
  ├── statusQueue            (阶段3)
  ├── nginxDeployments       (来自 nginxUpdater.NginxDeployments)
  ├── wafPollerManager       (阶段3)
  ├── inferenceExtension     (cfg)
  └── plmEnabled             (由 cfg.PLMStorageConfig != nil 推导)
```

可见 `eventHandler` 是整条装配线的「集大成者」，这就是为什么它必须放在阶段 4、靠后创建。

**Runnable 注册的「分批启动」关系**：

```text
mgr.Start(ctx) 后，controller-runtime 按 leader 语义分批拉起 Runnable：

  【立即启动（无需等待 leader）】= LeaderOrNonLeader：
     ├─ 各 registerControllers 注册的自研控制器（NeedLeaderElection=false）
     ├─ eventLoop（事件循环）
     ├─ gRPC server（控制面↔数据面）
     └─ provisioner event loop

  【成为 leader 后才启动】= Leader：
     ├─ telemetry CronJob
     └─ CallFunctionsAfterBecameLeader 内的三把开关：
           ├─ groupStatusUpdater.Enable   （允许写状态）
           ├─ nginxProvisioner.Enable     （允许创建/回收数据面）
           └─ eventHandler.enable         （允许实际下发配置）
```

最精妙的是最后三把「开关」。它们的意思是：**控制器和事件循环在所有 Pod 上都跑、都在捕获变更、都在构建图，但「真正改集群」的动作（写状态、建数据面、下发配置）默认是关的，只有当本副本当选 leader 时才打开**。这样既保证非 leader 副本的 cache 随时热备，又避免了多副本同时写造成的冲突。

#### 4.3.3 源码精读

先看 `eventHandlerConfig` 的字段定义，它就是一张「事件处理协作者清单」：

[internal/controller/handler.go:49-103](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L49-L103) —— 每个字段都有注释说明用途，例如 `nginxUpdater updates nginx configuration using the NGINX agent`、`processor is the state ChangeProcessor`。读这二十多个字段，等于读了一份「处理一个事件批次需要哪些角色」的剧本。

再看 `StartManager` 里**实际注入**的代码（与上面字段一一对应）：

[internal/controller/manager.go:222-252](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L222-L252) —— 这里把 `nginxUpdater`、`nginxProvisioner`、`metricsCollector`、`groupStatusUpdater`、`processor`、`serviceResolver`（就地 `resolver.NewServiceResolverImpl(mgr.GetClient())`）、`generator`（就地 `ngxcfg.NewGeneratorImpl(...)`）等全部塞进 `eventHandlerConfig`。注意 `plmEnabled: cfg.PLMStorageConfig != nil`（L251）——这是把「指针是否为 nil」的配置语义**降级**成「布尔开关」注入，下游无需再判断空指针。

然后是 Runnable 的三种包装器实现：

[internal/framework/runnables/runnables.go:9-67](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/runnables.go#L9-L67) —— `Leader` 与 `LeaderOrNonLeader` 唯一区别就是 `NeedLeaderElection()` 返回 `true` 还是 `false`；`CallFunctionsAfterBecameLeader.Start` 则是遍历调用所有 `enableFunctions`（它自身也是 `Leader`，所以这些函数只在成为 leader 时执行一次）。

最后看 `StartManager` 如何把这三把开关打包注册：

[internal/controller/manager.go:268-274](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L268-L274) —— `runnables.NewCallFunctionsAfterBecameLeader([]func(context.Context){ groupStatusUpdater.Enable, nginxProvisioner.Enable, eventHandler.enable })`。这三把开关的设计是理解 NGF「多副本行为」的钥匙：非 leader 副本看似在跑，实则只读不写。

#### 4.3.4 代码实践

**实践目标**（本讲 `practice_task`）：画出 `StartManager` 中各组件的**创建与装配顺序图**，并把「依赖注入箭头」和「Runnable 注册标记」都标上。

**操作步骤**：

1. 在纸上或文档里画一条从左到右的时间轴，标出阶段 0~5（见 4.1.2）。
2. 在每个阶段下方画出该阶段创建的对象方框（`healthChecker`、`mgr`、`processor`、`nginxUpdater`、`nginxProvisioner`、`eventHandler`、`eventLoop` 等）。
3. 用**实线箭头**表示依赖注入：从「被依赖对象」指向「消费它的对象」。例如 `nginxUpdater → eventHandler`、`processor → eventHandler`、`nginxUpdater.NginxDeployments → nginxProvisioner`、`healthChecker → {createManager 的 readyz, registerTelemetry}`。
4. 用**虚线箭头 + 标签**表示 Runnable 注册：从对象指向 `mgr`，标注 `LeaderOrNonLeader` 或 `Leader`。例如 `eventLoop --(LeaderOrNonLeader)--> mgr`、`grpcServer --(LeaderOrNonLeader)--> mgr`、`telemetryJob --(Leader)--> mgr`。
5. 用**特殊标记**标出 `CallFunctionsAfterBecameLeader` 里的三把开关（`groupStatusUpdater.Enable` / `nginxProvisioner.Enable` / `eventHandler.enable`），并注明它们「成为 leader 后才触发」。

**需要观察的现象**：

- `eventHandler` 方框应是「入箭头最多」的节点（集大成者）。
- `mgr` 方框应是「出箭头最多」的节点（它派生 cache/client/recorder，又收容所有 Runnable）。
- 所有「真正写集群/下发配置」的动作都挂在 `Leader` 语义或 leader 触发的开关上。

**预期结果**：得到一张与下图类似的拓扑（简版）：

```text
cfg ──► StartManager ──► [阶段0] healthChecker, mgr
                              │
                  ┌───────────┼───────────────┐
                  ▼           ▼               ▼
            [阶段1]      [阶段2]          [阶段3]
         registerCtrls   processor     nginxUpdater─┐
         discoveredCRDs  policyManager  statusQueue  │
                         (注入processor)  nginxProvisioner◄──┘
                              │              │
                              └──────┬───────┘
                                     ▼
                            [阶段4] eventHandler ◄── 注入几乎所有部件
                                     │
                                eventLoop ──(LeaderOrNonLeader)──► mgr
                  CallFunctionsAfterBecameLeader{3把开关} ──(Leader)──► mgr
                            [阶段5] registerTelemetry ──(Leader)──► mgr
                                     │
                                 mgr.Start(ctx)
```

> 待本地验证：上图是阅读 `manager.go` 后归纳的逻辑拓扑，非工具自动生成。建议你用自己的画图工具按上述步骤重画一遍以加深印象。

#### 4.3.5 小练习与答案

**练习 1**：假设部署了 3 个 NGF 控制面副本且开了 leader 选举。一个 Gateway 资源变更事件到来时，三个副本分别会做什么？谁的 cache 会更新？谁会真的下发配置？

**参考答案**：三个副本都运行着控制器与事件循环（`LeaderOrNonLeader`），所以三个副本的本地 cache **都会**捕获到这次变更、都会构建图。但「下发配置」受 `eventHandler.enable` 这把开关控制，该开关只在 leader 副本上被 `CallFunctionsAfterBecameLeader` 打开，所以**只有 leader 副本**会真正通过 `nginxUpdater` 下发配置、通过 `groupStatusUpdater` 回写状态。非 leader 副本构建图是为了「热备」，一旦切换可立即接手。

**练习 2**：`createAndRegisterProvisioner` 把 provisioner event loop 注册成 `LeaderOrNonLeader`（[L384](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L384)），但 `nginxProvisioner.Enable` 又被放进 leader-only 的开关列表（[L270](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L270)）。这两个事实矛盾吗？

**参考答案**：不矛盾。「注册成 `LeaderOrNonLeader`」说的是 event loop 这个**goroutine**在所有副本上启动运行；而 `nginxProvisioner.Enable` 控制的是它内部「**是否真正执行**创建/回收数据面资源」的开关。即：所有副本的 provisioner loop 都在跑（可能在做本地状态维护），但真正改集群的动作只有 leader 副本打开。这与 4.3.2 末尾讲的「三把开关」是同一套机制。

## 5. 综合实践

**任务**：化身 NGF 控制面的「总装工程师」，为 `StartManager` 撰写一份《控制面装配说明书》，并完成一次「增配一个新子系统」的演练。

**第 1 步：装配说明书。** 用一张表格 + 一张拓扑图总结本讲。表格列出：每个工厂函数、所在行号、产出对象、是否注册 Runnable、leader 语义、它依赖哪些前置对象。拓扑图沿用 4.3.4 的简版，但要把对象具体到行号。

**第 2 步：追踪一条「贯通全链」的数据。** 选择 `cfg.GatewayClassName`（来自 flag `--gatewayclass`）。在 [manager.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go) 里搜索它的每一次出现，记录它被注入到哪些对象（如 `recorderName` L133、`processor` 的 `GatewayClassName` L168、`eventHandlerConfig.gatewayClassName` L245、provisioner 的 `GCName` L367、`createMetricsCollector` 的 constLabels L295）。你会直观看到「一个配置项如何沿装配线分发到多个子系统」。

**第 3 步：演练「增配一个新子系统」（纸上设计，不写源码）。** 假设你要给 NGF 加一个「配置变更审计日志」后台任务，要求：每个副本都跑、每分钟把最近一分钟的配置变更摘要打印出来。请回答：

1. 它应该被包装成 `Leader` 还是 `LeaderOrNonLeader`？（答：`LeaderOrNonLeader`，因为题目要求每个副本都跑。）
2. 它需要消费哪些已有对象？（答：至少需要 `processor` 或 `eventHandler` 来获知变更，以及 `cfg.Logger`。）
3. 它的工厂函数该叫什么、放在 `StartManager` 的哪个阶段？（答：例如 `createAuditLogger`，放在阶段 3 或 4，因为它依赖 `processor`。）
4. 它如何被注册？（答：`mgr.Add(&runnables.LeaderOrNonLeader{Runnable: auditLogger})`，参照 `createAgentServices` 的写法。）

**预期结果**：你能用本讲的三套工具（装配阶段划分、工厂函数表、Runnable 语义）独立分析「加一个新组件需要改哪些地方」，这正是 u13-l3「二次开发」的预热。

> 待本地验证：第 3 步为设计演练，不涉及实际编码与运行。

## 6. 本讲小结

- `StartManager(cfg config.Config)` 是控制面的**总装入口**，所有 flag 在此汇聚成一个 `config.Config`，被装配成一台运行中的控制面。它遵循「准备底座 → 注册控制器 → 构造处理器 → 构造下发/状态组件 → 装配事件处理器与循环 → 注册可观测性并启动」的 6 阶段流水线。
- 装配线由一组 **`create*`/`register*`/`prepare*` 工厂函数**构成。工厂分两类：纯构造型（只返回对象）与构造并注册型（同时 `mgr.Add`）。装配顺序由依赖关系「拓扑排序」决定——被依赖者必先于依赖者创建。
- NGF 用**结构体字段注入**做依赖注入，`eventHandlerConfig`（handler.go）是「依赖最多」的集大成者，读它的字段表就能反推「处理一轮事件需要哪些协作者」。
- controller-runtime 的 Runnable 被三种自研包装器赋予 leader 语义：`LeaderOrNonLeader`（所有副本跑，含事件循环/gRPC/provisioner）、`Leader`（仅 leader 跑，含 telemetry）、`CallFunctionsAfterBecameLeader`（成为 leader 后才打开「写集群/下发配置」的三把开关）。
- 一个关键洞察：**非 leader 副本也在跑控制器和事件循环、也在构建图，但真正改集群的动作默认关闭**，只在本副本当选 leader 时才打开——这就是 NGF「热备 + 单写」的多副本策略。

## 7. 下一步学习建议

本讲只画了「装配线总图」，每个工位的内部实现都还没展开。建议按以下顺序继续：

- **u3-l2「controller-runtime Manager 与缓存」**：深入 `createManager` 与 `buildManagerCache`，搞清 cache 的 transform、namespace 过滤、metrics/health 端口如何配置。
- **u3-l3「控制器注册与 CRD 存在性发现」**：把本讲一笔带过的 `registerControllers`、`filterControllersByCRDExistence`、`featureFlagControllerCfgs` 讲透——它们决定 NGF 到底 watch 哪些资源。
- **u3-l4「Leader Election 与 Runnables」**：把本讲的 `Leader`/`LeaderOrNonLeader`/`CallFunctionsAfterBecameLeader` 与 controller-runtime 的 leader 选举细节、`CronJob` 周期任务机制结合讲清。
- 之后进入 u4「事件管线」，沿 `eventCh → EventLoop → eventHandler.HandleEventBatch` 这条本讲埋下的主线，看一个事件批次如何真正流动。

> 阅读源码时的一个小窍门：任何时候在 NGF 代码里迷路了，回到 [internal/controller/manager.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go) 的 `StartManager`——它是整条调用链的「总目录」。
