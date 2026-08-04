# pkg/cache 中央缓存架构

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `pkg/cache` 是「谁的中心缓存」：它在**单个进程内**是全局单例，被该进程内所有组件共享，但**每个进程各自重建**，跨进程状态靠 Redis 同步。
- 复述中央缓存的生命周期：`InitWithOptions` 如何按服务身份（gateway / metadata / controllers）条件化装配各子系统，以及它启动的后台循环。
- 理解订阅机制：`discovery.Provider` 抽象如何把「K8s informer」和「standalone 静态文件」两种来源统一成同一套 `WatchEvent`，再由 `informers.go` 的事件处理器写入两张核心映射表。
- 掌握查询 API：`cache_api.go` 定义的接口体系与 `cache_impl.go` 的实现，以及 `cache_gateway_snapshot.go` 提供的「跨网关副本」快照视图。
- 准确说出哪些组件真正复用了 `pkg/cache`，并纠正一个常见误解：**PodAutoscaler 并不复用 `pkg/cache`**，它有自己独立的指标管线（见 u3-l3）。

## 2. 前置知识

本讲默认你已经学完：

- **u1-l2 / u1-l5**：知道 `pkg/` 是 Go 逻辑目录，standalone 模式用静态 endpoints 文件、K8s 模式用动态发现。
- **u2-l1**：控制器管理器入口与 `controller-runtime` 的 Manager/Cache 概念。注意：`controller-runtime` 自己也有一套 cache（`memcache`），那是给控制器读 K8s 对象用的，**和本讲的 `pkg/cache` 是两回事**——本讲讲的是 AIBrix 自己实现的、为网关/调度服务的「业务级」中央缓存。
- **u3-l3**：PodAutoscaler 的指标采集管线（collector / fetcher / aggregator），以便对比理解「为什么 PodAutoscaler 不走 pkg/cache」。

需要先建立的几个直觉概念：

| 术语 | 含义 |
| --- | --- |
| 进程级单例 | 用 `sync.Once` 保证一个进程里只初始化一次，进程内任何地方 `cache.Get()` 拿到同一个 `*Store`。 |
| Informer | client-go 的「本地缓存 + 事件流」机制：先 List 全量，再持续 Watch 增量，回调 Add/Update/Delete。 |
| Provider（发现提供者） | AIBrix 自己抽象的接口，把 K8s informer 与静态文件统一成同一套事件回调。 |
| 快照（Snapshot） | 在某个时刻把缓存里的部分状态「定格」拷贝一份，供读侧在不持锁的情况下一致地读取。 |

一句话定位：`pkg/cache` 是 AIBrix **数据平面（网关）**的「工作内存」——它把集群里哪些 Pod 在跑哪些模型、各 Pod 的实时指标、各模型的在途请求数等，全部收拢到一个进程内的内存结构里，供路由算法、限流、排队、调度秒级查询。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pkg/cache/cache_init.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_init.go) | 缓存核心：`Store` 结构体定义、单例 `Get()`、构造 `New()`、按服务身份装配的 `InitWithOptions()`，以及各后台循环的启动函数。 |
| [pkg/cache/cache_api.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_api.go) | 接口层：把 `Cache` 拆成 `PodCache`/`ModelCache`/`MetricCache`/`RequestTracker`/`ProfileCache` 等小接口，是消费方依赖的抽象。 |
| [pkg/cache/cache_impl.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_impl.go) | `cache_api.go` 接口的实现：`ListPodsByModel`、`GetMetricValueByPod`、`AddRequestCount` 等查询与请求追踪方法。 |
| [pkg/cache/informers.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/informers.go) | 事件处理层：`addPod`/`updatePod`/`deletePod`/`addModelAdapter` 等回调，把发现事件翻译成两张映射表（`metaPods`、`metaModels`）的增删改。 |
| [pkg/cache/discovery/discovery.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/discovery.go) | `Provider` 接口与 `WatchEvent` 定义，订阅机制的抽象入口。 |
| [pkg/cache/discovery/kubernetes.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/kubernetes.go) / [static.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/static.go) | Provider 的两种实现：K8s informer 与 standalone 静态 YAML。 |
| [pkg/cache/cache_gateway_snapshot.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_gateway_snapshot.go) | 跨网关副本的状态快照：周期性把本网关的 per-pod 状态写 Redis，并扫描所有副本的快照聚合到内存。 |
| [pkg/cache/pod.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/pod.go) / [model.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/model.go) | 两个核心数据结构 `Pod` 与 `Model` 的定义。 |
| [pkg/cache/README.md](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/README.md) | 包级说明（注意：其中部分示例代码与真实 API 不一致，本讲会在实践中纠正）。 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 缓存初始化与运行**、**4.2 informers 订阅机制**、**4.3 快照与查询 API**。

### 4.1 缓存初始化与运行

#### 4.1.1 概念说明

「中央缓存」这个词容易让人误以为它是一个**跨进程共享**的服务（像 Redis 那样）。其实不是。`pkg/cache` 是一个**进程级单例**：每个 AIBrix 进程（网关 `cmd/plugins`、控制器 `cmd/controllers`）启动时各自调一次 `InitWithOptions`，在自己的内存里建一个 `*Store`，互不共享。

那「中心」体现在哪？体现在**进程内**：在一个网关进程里，路由算法、限流器、排队器、模型路由器几十处都通过 `cache.Get()` 拿到**同一个** `Store` 实例，避免每处各自去查 K8s、各自去拉指标。它是进程内的「单一数据源」。

跨进程的共享（比如多个网关副本看到的「同一个 Pod 总共有多少在途请求」）则是另一条路——通过 Redis 做快照同步，那是 4.3 要讲的 gateway snapshot。

#### 4.1.2 核心流程

初始化流程是一条「按需装配」的流水线：

```text
InitWithOptions(config, stopCh, opts)
  │
  ├─ once.Do(...)                          // 进程内只执行一次（单例保证）
  │     │
  │     ├─ 据 opts 推断 service 身份        // gateway / metadata / controllers
  │     ├─ New(redisClient, promAPI, routerProvider)   // 建 Store，启动指标 worker 池
  │     │
  │     ├─ 选 discovery provider            // opts.DiscoveryProvider ?: K8sProvider
  │     ├─ initDiscoveryProvider(...)       // 注册 Watch 回调 → handleDiscoveryObject
  │     ├─ initMetricsCache(...)            // 启动周期性指标拉取循环
  │     │
  │     ├─ if enableProfileCaching: initProfileCache(...)        // GPU 画像循环（可选）
  │     ├─ if enableTracing && redis!=nil: initTraceCache(...)   // 请求追踪循环（可选）
  │     ├─ if redis!=nil: initGatewaySnapshotSync(...)           // 跨副本快照循环（可选）
  │     └─ if EnableKVSync: store.initKVEventSync(...)           // KV 事件同步（可选）
  │
  └─ return store
```

关键设计点是「**按服务身份条件化装配**」：同一个 `InitWithOptions` 函数，网关进程和控制器进程传不同的 `InitOptions`，从而启动不同子集的后台循环。这避免了控制器进程也不得不起一套网关才需要的 Redis 同步逻辑。

#### 4.1.3 源码精读

**单例与构造。** 包级变量 `store` 加 `sync.Once` 保证进程内只初始化一次，`Get()` 在未初始化时返回错误（[cache_init.go:42-46](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_init.go#L42-L46)）：

```go
var (
    store = &Store{}
    once  sync.Once
)
```

`Get()` 的契约很简单——没初始化就报错，消费方必须处理这个错误（[cache_init.go:141-146](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_init.go#L141-L146)）：

```go
func Get() (Cache, error) {
    if !store.initialized {
        return nil, errors.New("cache is not initialized")
    }
    return store, nil
}
```

注意返回类型是 **接口 `Cache`** 而非 `*Store`——这是典型的「面向接口」，消费方只依赖 [cache_api.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_api.go) 里定义的能力，不依赖具体结构体。

**Store 结构体是整个包的核心。** 它持有所有数据结构（[cache_init.go:72-134](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_init.go#L72-L134)），最重要的两张表是：

```go
// pod_namespace/pod_name -> *Pod
metaPods utils.SyncMap[string, *Pod]

// model_name -> *Model
metaModels utils.SyncMap[string, *Model]
```

这两张表是「双向」的：从 Pod 能查到它跑哪些模型（`metaPods[key].Models`），从模型能查到它分布在哪些 Pod（`metaModels[name].Pods`）。维护这个双向映射的一致性是 `informers.go` 的核心职责（见 4.2）。其它字段（`deploymentProfiles` GPU 画像、`requestTrackers` 请求追踪器、`gatewaySnapshotCache` 跨副本快照）都是可选增强。

`New()` 构造时会立刻启动一个指标 worker 池（[cache_init.go:157-178](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_init.go#L157-L178)），这是后面指标采集的执行单元：

```go
store.podMetricsJobs = make(chan *Pod, 100)
for w := 0; w < store.podMetricsWorkerCount; w++ {
    go store.worker(store.podMetricsJobs)   // 10 个 worker 消费 jobs channel
}
```

**按服务身份条件化装配。** `InitWithOptions` 先根据 `opts` 推断自己是哪种服务（[cache_init.go:328-349](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_init.go#L328-L349)）：

```go
if opts.EnableKVSync {
    service = "gateway"
} else if opts.RedisClient != nil {
    service = "metadata"
} else {
    service = "controllers"
}
// metadata / controllers 不需要 GPU 画像与请求追踪
if service == "metadata" || service == "controllers" {
    enableGPUOptimizerTracing = false
    enableModelGPUProfileCaching = false
}
```

随后一连串 `if` 决定启动哪些后台循环（[cache_init.go:364-391](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_init.go#L364-L391)）。把这些条件对照两个真实调用方看，就一目了然：

- **网关** `cmd/plugins` 传「全套」：KVSync、RedisClient、ModelRouterProvider、DiscoveryProvider（standalone 时是 StaticProvider）（[cmd/plugins/main.go:157-162](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/plugins/main.go#L157-L162)）。因此网关进程会启动指标循环、画像循环、追踪循环、跨副本快照循环、（可选）KV 事件同步。
- **控制器** `cmd/controllers` 传「空 options」，且**只在 ModelAdapter 控制器启用时才初始化**（[cmd/controllers/main.go:264-268](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/controllers/main.go#L264-L268)）。空 options 意味着没有 Redis、没有 KVSync、没有 router provider——控制器进程里的 cache 只用 K8s informer 发现 Pod/ModelAdapter，纯粹是为 LoRA 适配器调度服务（见 u4-l1/u4-l2）。

**指标采集循环。** `initMetricsCache` 是一个 `select` 循环，每隔 `podMetricRefreshInterval`（默认 50ms，可由 `AIBRIX_POD_METRIC_REFRESH_INTERVAL_MS` 覆盖，[cache_metrics.go:122](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_metrics.go#L122)）把所有 ready Pod 投递到 `podMetricsJobs` channel，由 worker 池并发去各引擎拉指标（[cache_init.go:402-421](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_init.go#L402-L421)）。worker 的具体拉取逻辑在 [cache_metrics.go:264-331](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_metrics.go#L264-L331)，它会调用 `engineMetricsFetcher` 统一从各引擎 HTTP 端点拉取标准化指标并写回 `pod.Metrics` / `pod.ModelMetrics`。

> 小结：`InitWithOptions` 是「配置驱动」的装配函数——同一份代码，靠 `InitOptions` 的字段值开关出不同子系统的后台循环。这种设计让网关、控制器、metadata 服务能共用同一个 cache 包而不互相耦合。

#### 4.1.4 代码实践

**实践目标**：理解「按服务身份装配」的真实效果。

**操作步骤**（源码阅读型，无需运行）：

1. 打开 [cache_init.go:325-395](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_init.go#L325-L395) 的 `InitWithOptions`。
2. 分别对照两个调用方：网关 [cmd/plugins/main.go:157-162](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/plugins/main.go#L157-L162) 与控制器 [cmd/controllers/main.go:264-268](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/controllers/main.go#L264-L268)。
3. 画一张表，列出 `initMetricsCache` / `initProfileCache` / `initTraceCache` / `initGatewaySnapshotSync` / `initKVEventSync` 这五个循环，分别标注它们在「网关进程」和「控制器进程」里是否启动。

**需要观察的现象 / 预期结果**：

| 后台循环 | 启动条件 | 网关 | 控制器 |
| --- | --- | --- | --- |
| `initMetricsCache` | 总是 | ✅ | ✅（仅 ModelAdapter 启用时） |
| `initProfileCache` | `enableProfileCaching`（controllers 被关掉） | ✅ | ❌ |
| `initTraceCache` | `enableTracing && redisClient != nil` | ✅（有 Redis 时） | ❌ |
| `initGatewaySnapshotSync` | `redisClient != nil` | ✅（有 Redis 时） | ❌ |
| `initKVEventSync` | `opts.EnableKVSync` | ✅（env 开启时） | ❌ |

结论：控制器进程的 cache 是一个「瘦身版」——只做发现 + 基础指标，没有 Redis 协同。待本地验证：若你在集群里 `kubectl logs <aibrix-controller-pod>`，应能看到 `initialize cache service=controllers` 的日志，而看不到 `Initializing gateway snapshot sync`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Get()` 返回的是接口 `Cache` 而不是 `*Store`？

> **答案**：为了让消费方（路由算法、限流器等）只依赖抽象能力（`PodCache`、`MetricCache`…），不依赖具体实现。这样未来换实现或写测试替身时，消费方代码不用改。Go 里 `*Store` 实现了 `Cache` 接口，所以 `return store, nil` 能隐式满足返回类型。

**练习 2**：`once.Do` 保证只初始化一次。如果有人在测试里**第二次**调用 `InitWithOptions` 会发生什么？

> **答案**：`once.Do` 内的闭包不会再次执行，直接返回**第一次**创建的 `store`。这就是为什么包里另提供了 `NewForTest()` / `InitForTest()` 一族函数——它们绕过 `once`、直接给包级 `store` 赋新值，供测试反复重置（见 [cache_init.go:181-313](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_init.go#L181-L313)）。

---

### 4.2 informers 订阅机制

#### 4.2.1 概念说明

`Store` 里的 `metaPods` / `metaModels` 不会自己长出来，需要有人把集群的变化「喂」给它。这就是订阅机制要解决的问题。

AIBrix 在这里做了一层关键抽象：**`discovery.Provider` 接口**。它把「数据从哪来」这件事藏起来——不管是 K8s informer（生产），还是静态 YAML 文件（standalone 调试），对 cache 来说都是同一个 `Watch(handler)` 调用，收到同一种 `WatchEvent`。这层抽象让 cache 包能同时服务于 K8s 部署和 standalone 部署（呼应 u1-l5）。

事件被 Provider 投递上来后，由 `informers.go` 里的一组处理器「翻译」成对两张映射表的增删改。

#### 4.2.2 核心流程

订阅链路分两层：

```text
        ┌─────────────────────────── Provider 层（数据来源） ───────────────────────────┐
        │  KubernetesProvider.Watch          StaticProvider.Watch                        │
        │  （informers → Add/Update/Delete）  （读 YAML → 全量 Add，无后续）              │
        └──────────────────────────┬──────────────────────────────────────────────────┘
                                   │ 统一成 WatchEvent{Type, Object, OldObject}
                                   ▼
        ┌───────────────────── initDiscoveryProvider（胶水层） ─────────────────────────┐
        │  provider.Watch(func(ev){ handleDiscoveryObject(store, ev.Type, ev.Object, ev.OldObject) }) │
        └──────────────────────────┬───────────────────────────────────────────────────┘
                                   ▼
        ┌───────────────────── informers.go（处理层，写映射表） ──────────────────────────┐
        │  *v1.Pod        → store.addPod / updatePod / deletePod                          │
        │  *ModelAdapter  → store.addModelAdapter / updateModelAdapter / deleteModelAdapter│
        └─────────────────────────────────────────────────────────────────────────────────┘
```

Provider 的契约（[discovery.go:53-70](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/discovery.go#L53-L70)）只有两个方法：`Watch(handler, stopCh)` 注册回调并启动订阅；`Type()` 返回标识。`Watch` 的语义是「**返回时已达到一致就绪态**」——动态 Provider 在返回后继续异步投递增量，静态 Provider 投递完初始状态就返回。

#### 4.2.3 源码精读

**胶水层。** `initDiscoveryProvider` 只做一件事：把 `handleDiscoveryObject` 注册成 Provider 的回调（[cache_init.go:425-432](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_init.go#L425-L432)）：

```go
func initDiscoveryProvider(store *Store, provider discovery.Provider, stopCh <-chan struct{}) error {
    return provider.Watch(func(ev discovery.WatchEvent) {
        handleDiscoveryObject(store, ev.Type, ev.Object, ev.OldObject)
    }, stopCh)
}
```

`handleDiscoveryObject` 按对象类型分流（[cache_init.go:434-467](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_init.go#L434-L467)）：`*v1.Pod` 走 add/update/delete Pod 三分支，`*ModelAdapter` 走 add/update/delete 适配器三分支。

**K8s Provider。** `KubernetesProvider.Watch` 把 informer 的 `AddFunc/UpdateFunc/DeleteFunc` 直接桥接成 `WatchEvent`（[kubernetes.go:76-101](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/kubernetes.go#L76-L101)），`Start` 后 `WaitForCacheSync` 等待初始全量同步完成，然后做一次 **post-sync 重新发射所有 ModelAdapter**（[kubernetes.go:117-120](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/kubernetes.go#L117-L120)）：

```go
adapters := modelInformer.GetStore().List()
for _, obj := range adapters {
    handler(WatchEvent{Type: EventAdd, Object: obj})
}
```

这一步很关键：Pod 和 ModelAdapter 两个 informer 的初始 List 是**并发**的，某个 ModelAdapter 的 Add 可能先于它的 Pod 到达，导致「模型→Pod」映射丢失。同步完成后再把所有适配器重新 Add 一遍（`addModelAdapter` 是幂等的），就能补全映射。

**静态 Provider。** `StaticProvider.Watch` 读 YAML、把每个 endpoint 构造成一个合成的 `*v1.Pod`、一次性全量 Add 后立即返回，没有任何后续增量（[static.go:86-95](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/static.go#L86-L95)）。这正是 standalone 模式「静态文件发现」的实现（呼应 u1-l5）。

**处理层：维护双向映射。** 以 `addPod` 为例（[informers.go:110-148](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/informers.go#L110-L148)），它先从 Pod 的 label/annotation 提取模型名与 ModelClaim 绑定，过滤掉 Ray worker Pod，然后在持锁区间里完成「双向登记」：

```go
metaPod := c.addPodLocked(pod)                    // 写 metaPods
if ok {
    c.addPodAndModelMappingLocked(metaPod, modelName)   // 写 metaModels + metaPod.Models
}
```

`addPodAndModelMappingLocked` 是双向映射的「原子单元」（[informers.go:357-382](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/informers.go#L357-L382)）：它既在 `metaPod.Models` 里记下「这个 Pod 跑这个模型」，又在 `metaModels[modelName].Pods` 里记下「这个模型在这个 Pod 上」。两端同时更新，且都用 `LoadOrStore` 做去重——重复的 Add 事件不会产生重复条目。

注意所有处理函数都遵守「**先取无锁的快速判断、再进临界区**」的模式。比如 `updatePod` 在拿锁前先算 `oldIsWorker`、先查 key 是否存在（[informers.go:155-169](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/informers.go#L155-L169)），如果是无关更新就直接返回，避免无谓加锁。

> 关于「legacy 代码」的一个重要提示：`informers.go` 顶部还有一个 `initCacheInformers` 函数（[informers.go:51-102](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/informers.go#L51-L102)），它是**旧的自建 informer 路径，当前已没有调用方**（kubernetes.go 的注释也写明 "same as the old initCacheInformers"）。现在活的路径是 `Provider` 抽象 + `handleDiscoveryObject`。读源码时要分清：文件里 `addPod` 等处理器是「活的」，`initCacheInformers` 是「历史遗留」。

#### 4.2.4 代码实践

**实践目标**：追踪一个 Pod 从「被 K8s 创建」到「出现在 `metaModels` 映射里」的完整事件链。

**操作步骤**（源码阅读型）：

1. 从 [kubernetes.go:78-81](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/kubernetes.go#L78-L81) 的 `AddFunc` 出发，看它如何包成 `WatchEvent{Type: EventAdd, Object: obj}`。
2. 跟到 [cache_init.go:436-439](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_init.go#L436-L439)，确认 `EventAdd + *v1.Pod` 分发到 `store.addPod(o)`。
3. 在 [informers.go:110-148](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/informers.go#L110-L148) 里确认：模型名从哪取（`getModelNameFromPod` → label/annotation 的 `model.aibrix.ai/name`）、worker Pod 如何被过滤、最后 `addPodAndModelMappingLocked` 怎样同时更新两张表。

**需要观察的现象 / 预期结果**：

- 一个带 `model.aibrix.ai/name: llama` 标签的 Pod 被创建后，`metaPods["default/llama-pod"]` 与 `metaModels["llama"].Pods["default/llama-pod"]` 都会被填充。
- 一个 Ray worker Pod（`ray.io/node-type: worker`）即使带模型标签，也会被 `isWorkerPod` 过滤（[informers.go:505-524](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/informers.go#L505-L524)）——因为分布式推理里 worker 不直接服务请求。
- 删除 Pod 时，[informers.go:259-273](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/informers.go#L259-L273) 会同时清 `metaPods`、`metaModels` 里的反向映射，并 `rateCalculator.PurgeEntriesForPod` 清理速率计算历史，避免高 churn 集群里 map 无限增长。

待本地验证：在 e2e 测试环境部署一个 quickstart model Pod（见 u1-l4），`kubectl logs <gateway-pod>` 开 `-v=4`，应能看到 `POD CREATED: ...` 与 `Pod added to model ...` 日志。

#### 4.2.5 小练习与答案

**练习 1**：`KubernetesProvider.Watch` 为什么在 `WaitForCacheSync` 之后还要重新发射一遍所有 ModelAdapter？

> **答案**：因为 Pod informer 和 ModelAdapter informer 的初始 List 是并发的。若某 ModelAdapter 的 Add 先到达，此时它引用的 Pod 可能还没进 `metaPods`，`addPodAndModelMappingLockedByName` 找不到 Pod 就会跳过，导致「模型→Pod」映射缺失。同步完成后再幂等地重发一次 Add，此时 Pod 已在缓存里，映射就能补全。

**练习 2**：standalone 模式下，如果一个 vLLM 容器重启换了 IP，cache 会自动更新吗？

> **答案**：不会。`StaticProvider.Watch` 只在启动时读一次 YAML 并全量 Add，之后没有任何 Watch/轮询（[static.go:86-95](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/static.go#L86-L95)）。这是 standalone 模式「只编排数据平面、不支持动态发现」的体现（呼应 u1-l5）。要更新需改 endpoints 配置并重启网关。

---

### 4.3 快照与查询 API

#### 4.3.1 概念说明

前两模块解决的是「数据怎么进来」，本模块解决「数据怎么被读出去」。

读侧的设计有两层：

1. **接口层** `cache_api.go`：把 cache 的能力拆成一组小接口（`PodCache`、`ModelCache`、`MetricCache`、`RequestTracker`、`ProfileCache`），消费方按需依赖最小接口。路由算法通常只需要 `PodCache` + `MetricCache`。
2. **实现层** `cache_impl.go`：直接读 `metaPods` / `metaModels`，大多是 `SyncMap.Load` 的简单包装。

除了「本地内存查询」，本模块还讲一种特殊的快照——**跨网关副本快照**（`cache_gateway_snapshot.go`）。它解决的是：当网关水平扩容出多个副本时，每个副本只看得到**自己**路由出去的请求，但路由算法（如 least_load）需要知道这个 Pod 在**所有副本上**的总在途请求数。这就要靠 Redis 在副本间周期性交换各自的 per-pod 状态。

#### 4.3.2 核心流程

**本地查询**很直接：

```text
路由算法 → cache.Get() → ListPodsByModel(model) / GetMetricValueByPod(pod, metric)
                                  └─ 直接读 metaModels / metaPods（内存，无 IO）
```

**跨副本快照**是一个「写—读」双相循环，每 100ms 一轮（[cache_gateway_snapshot.go:58-139](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_gateway_snapshot.go#L58-L139)）：

```text
每个网关副本，每 100ms：
  Phase 1（写）：把本副本所有 Pod 的快照字段 HSET 到 Redis
                key = aibrix:pod:{本副本名}:{ns}:{pod},  TTL=500ms
  Phase 2（读）：SCAN aibrix:pod:* 拿到所有副本的 key，
                分批 HGetAll → 聚合成 map[podKey][]各副本字段
                原子替换 gatewaySnapshotCache（atomic.Value）
```

读侧（指标 worker）在每个 Pod 的刷新周期里，从这份内存快照算出「全局在途请求数」：

\[
\text{totalRunning}(pod) = \text{localRunning} + \sum_{g \,\neq\, self} \text{running}_g(pod)
\]

其中 \(\text{localRunning}\) 是本副本的原子计数器（永远比 Redis 快照更新），求和项来自其它副本最近一次上报的 `requests_running`。之所以本地部分用原子计数器而不是也读 Redis，是为了避免「自己刚转发一个请求，但快照还没刷新」导致的短暂低估。

刷新周期 100ms、TTL 500ms，意味着即便连续 4 次（400ms）刷新失败，key 仍存活；超过 5 次失败才过期被清除——这是一个约 5 倍的安全余量。

#### 4.3.3 源码精读

**接口层。** `Cache` 是一组小接口的聚合（[cache_api.go:26-35](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_api.go#L26-L35)）：

```go
type Cache interface {
    PodCache
    ModelCache
    MetricCache
    RequestTracker
    RequestTrackerRegistry
    ProfileCache
    types.OutputPredictorProvider
    types.RouterProvider
}
```

最常用的 `PodCache` 只有两个方法（[cache_api.go:38-55](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_api.go#L38-L55)）——`GetPod` 与 `ListPodsByModel`。路由算法正是靠 `ListPodsByModel` 拿到候选 Pod 列表再打分。

**查询实现。** `ListPodsByModel` 就是一次 `metaModels.Load`（[cache_impl.go:73-80](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_impl.go#L73-L80)）：

```go
func (c *Store) ListPodsByModel(modelName string) (types.PodList, error) {
    meta, ok := c.metaModels.Load(modelName)
    if !ok {
        return nil, fmt.Errorf("model does not exist in the cache: %s", modelName)
    }
    return meta.Pods.Array(), nil
}
```

`GetMetricValueByPod` 类似——从 `metaPods` 取出 `*Pod`，再从它的 `Metrics` 这个 SyncMap 里取指标（[cache_impl.go:135-143](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_impl.go#L135-L143)）。这些查询全是纯内存、无 IO，所以路由算法可以在每次请求都调用而不用担心性能。

**请求追踪。** `AddRequestCount` / `DoneRequestCount` / `DoneRequestTrace` 是网关在请求生命周期里调用的（[cache_impl.go:179-248](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_impl.go#L179-L248)）。它们维护两件事：模型的 `pendingRequests` 计数（用 `atomic.AddInt32`，无锁），以及可选的请求 trace（用于输出长度预测，见 u6-l2）。这两个计数是 VTC、SLO 等路由算法的关键输入。

> 顺带纠正一个细节：[cache_api.go:118-156](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_api.go#L118-L156) 的 `RequestTracker` 契约明确写「ctx 可能为 nil」（请求在路由完成前被取消），所有实现都必须防 nil——这是消费方容易踩的坑。

**跨副本快照的写相。** `appendGatewayPodSnapshotToPipeline` 定义了每个 Pod 写进 Redis 的字段（[cache_gateway_snapshot.go:200-214](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_gateway_snapshot.go#L200-L214)）：

```go
fields := map[string]any{
    "gateway_instance_id": gatewayPodName,                 // 本副本标识
    "pod_uid":             string(pod.UID),
    "pod_name":            pod.Name,
    "namespace":           pod.Namespace,
    "node_name":           pod.Spec.NodeName,
    "requests_running":    strconv.Itoa(int(atomic.LoadInt32(&pod.runningRequests))),  // 本副本的在途数
    "seq":                 strconv.FormatInt(atomic.LoadInt64(&pod.completedRequests), 10), // 完成计数
    "update_time":         time.Now().Format("15:04:05.000"),
}
pipe.HSet(ctx, key, fields)
pipe.PExpire(ctx, key, gatewayPodSnapshotTTL)   // 500ms
```

这就是 `cache_gateway_snapshot.go` 提供给网关的「快照视图」字段集——以 `gateway_instance_id` 区分不同副本，以 `pod_name/namespace` 聚合同一 Pod。key 格式 `aibrix:pod:{gatewayPodName}:{ns}:{podName}`（[cache_gateway_snapshot.go:218-220](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_gateway_snapshot.go#L218-L220)）。

**跨副本快照的读相聚合。** `syncRunningRequestsGlobally` 从内存快照（不是直接打 Redis）算全局在途数（[cache_metrics.go:536-563](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_metrics.go#L536-L563)）：跳过自己的 `gateway_instance_id`，把其它副本的 `requests_running` 求和，再加上本地原子计数，写回 `pod.Metrics[RealtimeNumRequestsRunning]`。这样路由算法通过普通的 `GetMetricValueByPod` 读到的 `num_requests_running` 就是**全局值**，无需感知多副本细节。

#### 4.3.4 代码实践

**实践目标**：准确说清「谁在复用 pkg/cache」，并核对 README 与真实 API 的出入。

**操作步骤**（源码阅读型）：

1. 在仓库里搜索 `cache.Get()` 的调用方。你会看到它们集中在两处：
   - **网关进程**：`pkg/plugins/gateway/gateway.go:161`（[gateway.go:161](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L161)）的 `NewServer` 拿到 cache 存进 `Server.cache`；随后所有路由算法（`least_load.go`、`throughput.go`、`prefix_cache.go`、`vtc_basic.go`、`pd_disaggregation.go`…）、`slo_queue.go`、`queue_router.go` 都各自 `cache.Get()` 取同一实例。`Server.handleListModels` 还用 `s.cache.ListModels()` 实现 `/v1/models` 接口（[gateway.go:666](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L666)）。
   - **控制器进程**：仅 `pkg/controller/modeladapter/modeladapter_controller.go:162`（[modeladapter_controller.go:162](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L162)）为 LoRA 适配器调度取 cache。
2. **重点核对**：在 `pkg/controller/podautoscaler/` 目录下搜索 `pkg/cache` 的引用。你会**搜不到**任何对 `aibrix/pkg/cache` 的导入——唯一带 `cache` 字样的是 `memcache.NewMemCacheClient(disc)`，那是 `controller-runtime` 的对象缓存，**与 pkg/cache 无关**。

**需要观察的现象 / 预期结果（对一个常见误解的纠正）**：

> 任务描述里假设「pkg/cache 被网关插件与 PodAutoscaler 同时复用」。对照源码，这个假设**只对了一半**：pkg/cache 确实被网关插件大量复用，也被控制器的 ModelAdapter 调度复用；但 **PodAutoscaler 并不复用 pkg/cache**，它有一套完全独立的指标管线（collector / fetcher / aggregator，见 u3-l3）。
>
> 为什么这么设计？因为两者的数据来源与时效要求不同：网关路由需要在**每次请求**（毫秒级）读最新负载，所以把指标拉进进程内存供无 IO 查询；PodAutoscaler 的伸缩决策是**秒级**周期触发的，直接从指标源（Prometheus / Pod metrics endpoint）现拉即可，没必要再维护一份内存缓存。把二者缓存合一反而会引入一致性维护负担。

**额外纠错（README 与真实 API 的出入）**：[pkg/cache/README.md](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/README.md) 的 "Usage Example" 写了 `cache.NewStore()`、`store.AddPod()`、`store.GetPodsForModel()`、`store.AddRequest()`、`store.DoneRequest()`，并称核心文件是 `cache.go`。这些都**与真实代码不符**（README 属示意性质、已过时）。真实对照表：

| README 写法（不存在） | 真实 API |
| --- | --- |
| `cache.NewStore()` | `cache.New(redisClient, promAPI, routerProvider)` 或 `cache.InitWithOptions(...)` |
| `store.AddPod(pod)` | `store.addPod(pod)`（小写，仅内部/事件处理器调用） |
| `store.GetPodsForModel(m)` | `cache.Get()` 返回的 `Cache.ListPodsByModel(m)` |
| `store.AddRequest(...)` | `Cache.AddRequestCount(ctx, reqID, model)` |
| `store.DoneRequest(...)` | `Cache.DoneRequestCount(...)` / `DoneRequestTrace(...)` |
| 核心文件 `cache.go` | 实际是 `cache_impl.go` |

待本地验证：`grep -rn "func.*NewStore" pkg/cache/` 应无结果；`grep -rn "func (c \*Store) ListPodsByModel" pkg/cache/` 命中 [cache_impl.go:73](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_impl.go#L73)。

#### 4.3.5 小练习与答案

**练习 1**：网关有 3 个副本 A/B/C，某 Pod 在 A 上有 2 个在途请求、B 上有 3 个、C 上 0 个。副本 A 做路由决策时，`GetMetricValueByPod` 读到的 `num_requests_running` 是多少？

> **答案**：5。`syncRunningRequestsGlobally` 会跳过自己的 `gateway_instance_id`（A），把 B 的 3 和 C 的 0 求和得 3，再加本地原子计数 2，得 5（[cache_metrics.go:544-555](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_metrics.go#L544-L555)）。注意这是**最终一致**的——B/C 的值最多滞后 100ms（一个刷新周期）。

**练习 2**：为什么本地份额用原子计数器、远程份额才用 Redis 快照？

> **答案**：本地副本每转发/完成一个请求都会立即更新原子计数器，它是「实时」的；若本地也读 Redis 快照，会因 100ms 刷新延迟而短暂低估自己刚转发的请求。远程副本的状态本来就要靠 Redis 交换、必然有延迟，所以接受快照即可。这种「本地精确 + 远程近似」是分布式计数的常见折中。

**练习 3**：`gatewaySnapshotCache` 为什么用 `atomic.Value` 而不是 `sync.RWMutex` 保护一个普通 map？

> **答案**：写侧每 100ms 整体替换一份新 map，读侧（指标 worker）高频并发读。用 `atomic.Value.Store/Load` 可以让读侧完全无锁，写侧通过「构造好新 map 再原子替换指针」避免读写竞争。这比 RWMutex 在「写少读多且整份替换」场景下更高效。

---

## 5. 综合实践

**任务**：画出「一个 chat 请求到来时，网关如何用 pkg/cache 做出路由决策」的完整数据流图，并标注每一步用的是 cache 的哪个能力。

**要求**：

1. 从请求进入 `gateway.NewServer`（它持有 `s.cache`，[gateway.go:160-193](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/gateway.go#L160-L193)）开始。
2. 至少标出下列 cache 能力的使用点：
   - `ListPodsByModel(model)` —— 拿候选 Pod（路由算法起步）；
   - `GetMetricValueByPod(pod, metric)` —— 读各 Pod 负载/吞吐/KV 用量等指标打分；
   - `AddRequestCount(...)` —— 路由完成后登记在途请求；
   - `GetRouter(ctx)` —— 取该模型的排队路由器（若配了 queue）；
   - `DoneRequestTrace(...)` —— 请求结束时回填 input/output token，更新输出预测器。
3. 在图上额外画出「跨副本快照」如何周期性（100ms）影响 `num_requests_running` 这个指标值。
4. 用一句话标注：这条链路里**没有任何一步**走 PodAutoscaler 的指标管线，说明二者是独立的。

**预期产出**：一张包含「请求主链路 + 后台指标/快照循环」两部分、清楚区分「实时本地数据」与「周期同步数据」的示意图。如果你能在图上标出「为什么 least_load 路由需要全局在途数（多副本场景）」，就说明你真正理解了 gateway snapshot 的存在意义。

> 提示：可参考 [cache_impl.go:179-219](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_impl.go#L179-L219)（AddRequestCount）与 [cache_metrics.go:536-563](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_metrics.go#L536-L563)（全局在途聚合）两个锚点。

## 6. 本讲小结

- `pkg/cache` 是**进程级单例**（`sync.Once` + 包级 `store`），「中心」指进程内多组件共享，而非跨进程；跨进程状态靠 Redis 同步。
- `InitWithOptions` 是「按服务身份条件化装配」的入口：网关传全套 options 启动指标/画像/追踪/快照/KV 五类循环，控制器传空 options（且仅 ModelAdapter 启用时初始化）只做发现 + 基础指标。
- 订阅机制分两层：`discovery.Provider`（K8s informer 或静态 YAML）统一产出 `WatchEvent`，`informers.go` 的处理器维护 `metaPods` ↔ `metaModels` 双向映射；`initCacheInformers` 是已无调用方的 legacy 路径。
- 查询 API 由 `cache_api.go` 的小接口 + `cache_impl.go` 的纯内存实现组成，路由算法靠 `ListPodsByModel` / `GetMetricValueByPod` 做毫秒级无 IO 查询。
- `cache_gateway_snapshot.go` 提供**跨网关副本**的 per-pod 快照（`requests_running`、`completedRequests` 等），经 `syncRunningRequestsGlobally` 聚合成全局在途请求数，让多副本部署下路由算法仍能做负载感知。
- **重要纠正**：网关插件与 ModelAdapter 调度控制器复用 pkg/cache；**PodAutoscaler 不复用**，它有独立的指标管线（u3-l3）。另外 README 的 Usage Example 已过时，真实方法名见 4.3.4 的对照表。

## 7. 下一步学习建议

- **u6-l2 Pod 发现、模型画像与输出预测**：本讲的 `discovery` 子包与 `output_predictor`、`model_gpu_profile` 的深入机制，是本讲的直接延续。
- **u7-l1 Envoy ExtProc 网关插件入口**：把本讲的「读侧」放进真实请求生命周期，看 `cache.Get()` 在网关里如何被路由调用。
- **u7-l3 / u7-l4 路由抽象与基础算法**：看 `ListPodsByModel` + `GetMetricValueByPod` 的查询结果如何被打分、归一化、排序。
- **u8-l4 Redis 状态同步与配置画像**：本讲的 gateway snapshot 是「per-pod 计数同步」，u8-l4 讲的是「路由状态（前缀缓存/会话）同步」，二者是 Redis 在网关里的两类不同用途，对照阅读能建立完整的「多副本一致性」图景。
