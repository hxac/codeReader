# SharedInformerFactory 与命名空间级 Informer

> 本讲承接 **u3-l1（LoadBalancerController 生命周期）**。上一讲我们知道了控制器如何「装配好对象再启动」，本讲要回答一个更底层的问题：**控制器靠什么源源不断地感知集群里的资源变化？** 答案就是 client-go 的 SharedInformerFactory + list-watch 模型。本讲只讲「感知」（数据进入内存缓存），不讲「处理」（sync 调谐，那是 u3-l3、u3-l5 的内容）。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 **list-watch** 与 **SharedInformerFactory** 的工作机制，解释为什么控制器不需要轮询 API Server。
2. 看懂 NIC 用 `namespacedInformer` 这个结构体把「一个命名空间内的全部监听」打包在一起的设计，区分它内部并存的 4 类工厂（core / CRD / secret / dynamic）。
3. 解释「按命名空间隔离」的原因，以及 NIC 如何用 key `""`（全局）和「标签动态命名空间」两种方式处理命名空间范围。
4. 掌握 `addXHandler` 的统一三步模式：取 Informer → 挂事件处理器 → 取 Lister 并登记 `HasSynced`。

## 2. 前置知识

如果你对 Kubernetes 控制器已经很熟，可跳过本节。

- **list-watch**：客户端先做一次 `LIST`（拉全量，建立基线缓存），再建一条 `WATCH`（长连接，收增量事件：Added/Modified/Deleted）。控制器据此「让本地缓存始终逼近集群真实状态」，而不必反复轮询。
- **Informer**：把 list-watch 包成一个带本地缓存（cache）和事件分发的对象。它额外维护一个 Indexer（可按 key 查询的本地存储）。
- **SharedInformerFactory**：工厂。同一个 Informer 被多个订阅者共享，避免对同一资源建多条 watch。它的 `Start()` 才真正发起 list-watch。
- **Lister / Store**：Informer 暴露的「只读本地缓存」视图，`GetByKey` 直接命中内存，不访问 API Server。NIC 里常把它们包成带业务方法的包装器（如 `storeToIngressLister`）。
- **ResourceEventHandler / ResourceEventHandlerFuncs**：事件回调接口。`AddFunc` / `UpdateFunc` / `DeleteFunc` 三个钩子，控制器靠它们把「资源变了」翻译成「入队一个 task」。
- **HasSynced / WaitForCacheSync**：每个 Informer 都有一个 `HasSynced()` 函数，表示「初始 LIST 是否完成」。控制器启动时必须 `WaitForCacheSync` 等全部缓存就绪，否则会用空缓存做错误的调谐。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [internal/k8s/controller.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go) | `namespacedInformer` 结构体定义、`newNamespacedInformer` 工厂、所有 `addXHandler`、`Run` 中的缓存同步、`getNamespacedInformer` |
| [internal/k8s/namespace.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/namespace.go) | Namespace 级别的 informer（监听带标签的 Namespace）+ 动态增删命名空间的 `syncNamespace` |
| [internal/k8s/service.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/service.go)、[internal/k8s/policy.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/policy.go)、[internal/k8s/transport_server.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/transport_server.go) | 各资源的 `addXHandler`，演示同一模式在不同工厂上的落地 |
| [cmd/nginx-ingress/flags.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/flags.go)、[cmd/nginx-ingress/main.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/main.go) | `-watch-namespace` / `-watch-namespace-label` 两个 flag 与 resync 周期（30s）的来源 |

## 4. 核心概念与源码讲解

### 4.1 SharedInformerFactory 与 list-watch 模型

#### 4.1.1 概念说明

「感知集群变化」有两种朴素做法：

1. **轮询**：每隔几秒 `GET` 所有 Ingress / VirtualServer / Service…… 资源上千时，API Server 直接被打垮。
2. **list-watch**：只 LIST 一次拿全量，之后挂一条长连接收增量。无变化时几乎零流量。

client-go 的 `SharedInformerFactory` 选第二种，并进一步做了「共享」优化：同一种资源的 Informer 在一个进程内只建一条 watch，多个订阅者共享同一条流和同一份缓存。

「Shared」还有一层更重要的含义——**resync（定期重同步）**。Informer 不只依赖 watch，还会每隔一段周期（NIC 里是 30 秒）把本地缓存里**所有**对象重新触发一次 `UpdateFunc` 事件。这是控制器的「兜底」：哪怕漏掉了某个 watch 事件，resync 也会让控制器重新调谐一次。设 resync 周期 \(T_{\text{resync}}\)，则在稳态下每秒大约触发 \(N / T_{\text{resync}}\) 次重入队（\(N\) 为缓存内对象数）。对 NIC，\(T_{\text{resync}} = 30\,\text{s}\)。

#### 4.1.2 核心流程

一个 Informer 的生命周期：

```text
factory.Start()                ← 启动（真正发起 list-watch）
   │
   ├─ LIST 全量 → 灌入本地缓存(Indexer) → 逐个触发 AddFunc
   │
   ├─ WATCH 长连接 → 每个事件触发对应 Add/Update/DeleteFunc
   │
   └─ 定时 resync → 把缓存对象重发为 UpdateFunc
   │
informer.HasSynced() == true   ← 表示初始 LIST 完成

控制器主循环启动前必须：cache.WaitForCacheSync(所有 informer.HasSynced...)
                                  ↑ 等缓存就绪，否则用空缓存调谐会出错
```

关键点：**事件处理器（handler）在 `Start` 之前注册，但 `Start` 调用被推迟到 `Run()`**。这是 u3-l1 讲过的「构造与运行分离」在 informer 层面的体现。

#### 4.1.3 源码精读

NIC 里每个命名空间都会建一个 core 资源的 SharedInformerFactory，并显式限定到该命名空间：

[internal/k8s/controller.go:555](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L555) —— 用 `informers.WithNamespace(ns)` 把工厂的范围锁死在某个命名空间，第二个参数 `lbc.resync` 就是上面的 resync 周期。

```go
nsi.sharedInformerFactory = informers.NewSharedInformerFactoryWithOptions(
    lbc.client, lbc.resync, informers.WithNamespace(ns))
```

而 `lbc.resync` 的值来自 main.go 中硬编码的 30 秒：

[cmd/nginx-ingress/main.go:298](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/main.go#L298)

```go
ResyncPeriod: 30 * time.Second,
```

工厂的真正 `Start()` 不在构造期，而在 `Run()` 里逐个命名空间地启动：

[internal/k8s/controller.go:780-783](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L780-L783)

```go
for _, nif := range lbc.namespacedInformers {
    nif.start()
}
```

随后是全工程性的「等所有缓存就绪」：

[internal/k8s/controller.go:799-809](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L799-L809) —— 把 `lbc.cacheSyncs` 与每个 `nsi.cacheSyncs` 拼成总表，一次性 `WaitForCacheSync`。注意此时 `syncQueue` 还没启动（在 815 行才 `Run`），这保证**缓存未就绪时绝不会有任何调谐发生**。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认「构造期注册、运行期启动、就绪后才调谐」的三段顺序。

1. 打开 [internal/k8s/controller.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go)。
2. 在 `NewLoadBalancerController`（307 行起）里搜 `newNamespacedInformer`（394 行）：注意此处只建工厂、注册 handler，**没有** `Start`。
3. 跳到 `Run()`（722 行）：确认 780 行 `nif.start()`、807 行 `WaitForCacheSync`、815 行 `syncQueue.Run` 的先后顺序。
4. **观察现象**：你能看到一条「先装好监听 → 启动监听 → 等数据齐 → 才开始干活」的线性链。
5. **预期结果**：用一句话写下这三步对应的行号；如果某天有人把 815 行挪到 807 行之前，控制器会怎样？（答：会用尚未同步的空缓存触发错误的 sync。）

> 本地验证项：resync 周期 30s 是硬编码值，无法用 flag 调整，属「待本地验证」的设计约束。

#### 4.1.5 小练习与答案

**练习 1**：为什么 NIC 不用控制器轮询，而用 list-watch？

**参考答案**：资源数量大时轮询会把 API Server 打垮且流量浪费；list-watch 只 LIST 一次建基线，之后靠长连接收增量，稳态几乎零流量，并自带 resync 兜底。

**练习 2**：如果把 `cache.WaitForCacheSync` 去掉，启动初期可能出什么问题？

**参考答案**：sync 处理函数会基于尚未同步的（近乎空的）缓存判断资源是否存在，导致误删已生成的 NGINX 配置或漏配。

---

### 4.2 namespacedInformer 结构：一个命名空间的全部监听

#### 4.2.1 概念说明

NIC 要在「一个命名空间内」监听很多种资源：core 的 Ingress / Service / EndpointSlice / Pod / Secret，自定义的 VirtualServer / VSR / TransportServer / Policy，以及（启用时）App Protect 的 WAF / DoS 资源。如果让每种资源各自 `NewSharedInformerFactory`，会到处散落、难以统一启动/停止/等待同步。

NIC 的做法是：**把「同一个命名空间内、所有要监听的资源」打包成一个 `namespacedInformer` 对象**。控制器顶层只持有一张 `map[string]*namespacedInformer`（按命名空间名索引）。这是一个「聚合（composite）」设计：组合优先于散落。

#### 4.2.2 核心流程

```text
namespacedInformer（每命名空间一个）
├─ sharedInformerFactory        ← core 资源：Ingress / Service / EndpointSlice / Pod
├─ secretInformerFactory        ← Secret（独立工厂，带 helm 过滤）
├─ confSharedInformerFactory    ← NIC CRD：VS / VSR / TS / Policy（仅 -enable-custom-resources）
├─ dynInformerFactory           ← 动态资源：WAF / DoS（仅 Plus + App Protect）
│
├─ 各种 *Lister（从各 Informer.GetStore() 取出，供 sync 期读缓存）
├─ cacheSyncs []InformerSynced  ← 汇总本命名空间所有 HasSynced
└─ stopCh / lock                ← 生命周期与并发控制
```

四种工厂**按需创建**，并非每个都存在：

- 没开 `-enable-custom-resources` 时，`confSharedInformerFactory` 不建，也就不监听 CRD。
- 该命名空间不在 `secretNamespaceList` 里时，`secretInformerFactory` 不建（见 4.3）。
- 没开 App Protect 时，`dynInformerFactory` 不建。

#### 4.2.3 源码精读

结构体定义在 [internal/k8s/controller.go:521-549](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L521-L549)，关键部分：

```go
type namespacedInformer struct {
    namespace                 string
    sharedInformerFactory     informers.SharedInformerFactory            // core
    confSharedInformerFactory k8s_nginx_informers.SharedInformerFactory  // NIC CRD
    secretInformerFactory     informers.SharedInformerFactory            // Secret
    dynInformerFactory        dynamicinformer.DynamicSharedInformerFactory // WAF/DoS
    ingressLister             storeToIngressLister
    svcLister                 cache.Store
    endpointSliceLister       storeToEndpointSliceLister
    podLister                 indexerToPodLister
    secretLister              cache.Store
    virtualServerLister       cache.Store
    virtualServerRouteLister  cache.Store
    transportServerLister     cache.Store
    policyLister              cache.Store
    // ... App Protect 相关 lister 省略
    stopCh      chan struct{}
    lock        sync.RWMutex
    cacheSyncs  []cache.InformerSynced
}
```

注意三类 Lister 的命名：`storeToIngressLister`、`storeToEndpointSliceLister`、`indexerToPodLister` 是项目自定义的包装器（定义于 [internal/k8s/utils.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/utils.go)），它们在原始 `cache.Store` 上加业务方法（如按 Service 取 EndpointSlice、取 Pod 列表）；其余资源直接用裸 `cache.Store`。

工厂的按需创建逻辑在 `newNamespacedInformer`：

[internal/k8s/controller.go:551-603](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L551-L603) —— 函数末尾 `lbc.namespacedInformers[ns] = nsi` 把成品登记进顶层 map。注意它内部调用 `addCustomResourceHandlers`（593 行）与 `addAppProtectHandlers`（597 行），这两个函数内部各自判断开关决定是否建对应工厂。

#### 4.2.4 代码实践（源码阅读型）

**目标**：列出 `namespacedInformer` 持有的全部 Lister 字段，并标注每个 Lister 来自哪一类工厂。

1. 打开结构体定义 [controller.go:521-549](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L521-L549)。
2. 画一张表，左列是 Lister 字段名，右列标注来源工厂：
   - `ingressLister / svcLister / endpointSliceLister / podLister` → `sharedInformerFactory`（core）
   - `secretLister` → `secretInformerFactory`
   - `virtualServerLister / virtualServerRouteLister / transportServerLister / policyLister` → `confSharedInformerFactory`（CRD）
   - `appProtectXxxLister / appProtectDosXxxLister` → `dynInformerFactory`（dynamic）
3. **预期结果**：你应得到一张「Lister → 工厂」映射表，并能解释为什么有些资源用同一个工厂、有些要分开（提示：core 用 `kubernetes.Interface`，CRD 用 `confClient`，WAF/DoS 用 `dynClient`，Secret 用独立工厂以加 helm 过滤）。

#### 4.2.5 小练习与答案

**练习**：为什么 Secret 要用独立的 `secretInformerFactory`，而不和 Ingress/Service 一起放进 `sharedInformerFactory`？

**参考答案**：两点。一是 Secret 工厂要带一个 `TweakListOptions` 过滤掉 helm release secret（`type=helm.sh/release.v1`），避免无谓事件；二是并非每个被监听的命名空间都要监听 Secret，只有命中 `secretNamespaceList` 的才建。这两个需求都不适用于通用 core 工厂。

---

### 4.3 按命名空间隔离与动态命名空间

#### 4.3.1 概念说明

「按命名空间隔离」有两层动机：

1. **作用域控制**：生产中常要求控制器只看特定命名空间（多租户、安全边界、降低 API Server 负载）。NIC 用 `-watch-namespace` 指定一个或多个命名空间；不指定则默认 `NamespaceAll`（即 key `""`，监听全部）。
2. **资源隔离与冲突检测**：不同命名空间的资源互不影响，`getNamespacedInformer(ns)` 取出的就是该命名空间的本地缓存，避免跨命名空间误读。

NIC 还支持第三种「动态命名空间」模式：`-watch-namespace-label foo=bar`。此时控制器不预先知道要监听哪些命名空间，而是监听 **Namespace 资源本身**——凡是带上该标签的 Namespace 出现，就为它**动态创建**一个 `namespacedInformer`；标签被移除或 Namespace 被删，就**动态销毁**。这是把「要监听什么」也纳入了 list-watch 的范围。

#### 4.3.2 核心流程

```text
启动期（NewLoadBalancerController）：
  namespaceList = -watch-namespace 拆分得到（或默认 [""] 全局）
  for ns in namespaceList:
      newNamespacedInformer(ns)          ← 预建各命名空间的 nsi

动态模式（-watch-namespace-label）额外：
  addNamespaceHandler(label)             ← 额外建一个「监听 Namespace 自身」的 informer
        │
        └─ Namespace 事件 → syncNamespace
              ├─ Namespace 新增/打标 → newNamespacedInformer(key) + start
              └─ Namespace 删/去标 → removeNamespacedInformer / cleanupUnwatchedNamespacedResources
```

读取时统一入口 `getNamespacedInformer(ns)`：先尝试全局 key `""`，命中就直接返回（说明是全局监听），否则按精确命名空间查。

#### 4.3.3 源码精读

顶层 map 与启动期预建：

[internal/k8s/controller.go:388-397](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L388-L397) —— 注意 390 行的特判：动态模式下若初始 `namespaceList` 含 `""`，则**跳过**预建（因为此时还没拿到任何带标签的命名空间）。

动态模式的开关在 [controller.go:362-368](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L362-L368)：`isDynamicNs := input.WatchNamespaceLabel != ""`，条件成立才挂 Namespace 监听。

Namespace 自身的监听在 [internal/k8s/namespace.go:49-62](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/namespace.go#L49-L62)，关键是用 `WithTweakListOptions` 把标签选择器塞进 ListOptions：

```go
optionsModifier := func(options *meta_v1.ListOptions) {
    options.LabelSelector = nsLabel
}
nsInformer := informers.NewSharedInformerFactoryWithOptions(
    lbc.client, lbc.resync, informers.WithTweakListOptions(optionsModifier)).
    Core().V1().Namespaces().Informer()
```

动态增删的核心是 `syncNamespace`：[namespace.go:64-128](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/namespace.go#L64-L128)。逻辑分两支：

- **lister 里已不存在该 key**（Namespace 被删或去标）：[namespace.go:73-99](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/namespace.go#L73-L99) 区分「Namespace 仍在但去标」（`cleanupUnwatchedNamespacedResources`，清资源但留空壳）与「Namespace 已删」（`removeNamespacedInformer`，连 informer 一起拆）。
- **key 仍在**（Namespace 新增或加标）：[namespace.go:100-127](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/namespace.go#L100-L127) 若该命名空间尚无 nsi，则 `newNamespacedInformer` + `start()`，再 `WaitForCacheSync` 等这个新命名空间的缓存就绪。

读取入口 `getNamespacedInformer`：[controller.go:848-864](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L848-L864)。注意 853 行：如果全局 key `""` 存在（即控制器监听全部命名空间），就直接返回它，**忽略**传入的具体命名空间——因为全局缓存里本来就包含所有命名空间。

> 顺带一提，动态模式下 cert-manager、external-dns 两个子控制器也要同步增删它们的命名空间级 informer，见 [namespace.go:94-99](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/namespace.go#L94-L99) 与 [namespace.go:118-123](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/namespace.go#L118-L123)。

#### 4.3.4 代码实践（源码阅读型）

**目标**：理解 `getNamespacedInformer` 的全局/精确双分支，并解释为什么全局模式下「忽略入参」是安全的。

1. 打开 [controller.go:848-864](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L848-L864)。
2. 跟踪 `syncIngress` 或 `syncVirtualServer` 中任何一处 `getNamespacedInformer(ns)` 调用（如 [controller.go:1417](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1417)）。
3. 回答：当控制器以 `-watch-namespace`（即全局 `""`）启动时，对命名空间 `nsA` 的资源取缓存，返回的是哪个 nsi？为什么不会出错？
4. **预期结果**：返回全局 nsi（key `""`）。安全的原因是全局 informer 用 `NamespaceAll` 监听，本地缓存天然包含所有命名空间的对象，`GetByKey("nsA/xxx")` 能命中。

> 待本地验证项：动态模式下若刚 `newNamespacedInformer` 后立即有事件到达，是否一定能在 `WaitForCacheSync` 之后才处理——可在 [namespace.go:124](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/namespace.go#L124) 加日志观察。

#### 4.3.5 小练习与答案

**练习 1**：`-watch-namespace a,b` 与 `-watch-namespace-label env=prod` 能同时用吗？

**参考答案**：不能。[flags.go:313-315](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/flags.go#L313-L315) 中 `mustValidateWatchedNamespaces` 检测到二者同时非空就 `Fatal` 退出，二者互斥。

**练习 2**：动态模式下，为什么 `NewLoadBalancerController` 里初始 `namespaceList` 含 `""` 时要 `break` 跳过预建？

**参考答案**：动态模式以标签为准，初始时还没有任何带标签的命名空间被发现，`""` 在这里不代表「全局」而是「暂无目标」，预建一个全局 informer 会违背「只看带标签命名空间」的语义。真正要监听的命名空间会在 `syncNamespace` 里逐个动态创建。

---

### 4.4 handler 注册：addXHandler 统一模式

#### 4.4.1 概念说明

每个 Informer 都需要挂上事件处理器（`ResourceEventHandler`），NIC 用一个统一模式 `addXHandler`。它的三步是：

1. 从对应工厂取出该资源的 Informer；
2. `informer.AddEventHandler(handlers)` 把 `createXHandlers(lbc)` 返回的事件处理器挂上去；
3. `informer.GetStore()` 取出 Lister 存进 nsi，并把 `informer.HasSynced` 追加进 `nsi.cacheSyncs`（供 4.1 的总同步等待）。

之所以每个资源都有独立的 `addXHandler`（而不是一个泛型函数），是因为 client-go 的 typed informer 工厂 API 是强类型的（`sharedInformerFactory.Networking().V1().Ingresses()`、`confSharedInformerFactory.K8s().V1().VirtualServers()`），不同资源走不同工厂链路，无法用同一个泛型调用收口。这是 raw client-go（非 controller-runtime）的典型特征，也是 [CLAUDE.md](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/CLAUDE.md) 里强调的「Uses raw client-go, not controller-runtime」。

#### 4.4.2 核心流程

```text
newNamespacedInformer(ns)
   ├─ nsi.addIngressHandler(createIngressHandlers(lbc))    → sharedInformerFactory
   ├─ nsi.addServiceHandler(createServiceHandlers(lbc))    → sharedInformerFactory
   ├─ nsi.addEndpointSliceHandler(...)                     → sharedInformerFactory
   ├─ nsi.addPodHandler()                                   → sharedInformerFactory
   ├─（命中 secretNamespaceList 时）nsi.addSecretHandler(...)→ secretInformerFactory
   ├─ addCustomResourceHandlers:
   │     ├─ nsi.addVirtualServerHandler(...)               → confSharedInformerFactory
   │     ├─ nsi.addVirtualServerRouteHandler(...)          → confSharedInformerFactory
   │     ├─ nsi.addTransportServerHandler(...)             → confSharedInformerFactory
   │     └─ nsi.addPolicyHandler(...)                      → confSharedInformerFactory
   └─ addAppProtectHandlers:
         └─ nsi.addAppProtectXxxHandler(...)               → dynInformerFactory

每个 addXHandler 内部统一：
   informer := nsi.<factory>.<Group>().<Version>().<Resource>().Informer()
   informer.AddEventHandler(handlers)        ← 事件回调：AddFunc/UpdateFunc/DeleteFunc
   nsi.<x>Lister = informer.GetStore()
   nsi.cacheSyncs = append(nsi.cacheSyncs, informer.HasSynced)
```

注意：事件处理器本身（`createIngressHandlers` 等）定义在 [internal/k8s/handlers.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/handlers.go) 及各资源文件，它们内部最终都调用 `lbc.AddSyncQueue(obj)`（[controller.go:664-666](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L664-L666)）把事件转成入队——**这是连接「感知」与「调谐」的桥**，调谐部分留待 u3-l3（task queue）与 u3-l4（handlers）展开。

#### 4.4.3 源码精读

最清晰的范本是 `addIngressHandler`，来自 core 工厂：

[internal/k8s/controller.go:681-690](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L681-L690)

```go
func (nsi *namespacedInformer) addIngressHandler(handlers cache.ResourceEventHandlerFuncs) error {
    informer := nsi.sharedInformerFactory.Networking().V1().Ingresses().Informer()
    if _, err := informer.AddEventHandler(handlers); err != nil {
        return fmt.Errorf("failed to add Ingress event handler: %w", err)
    }
    nsi.ingressLister = storeToIngressLister{Store: informer.GetStore()}
    nsi.cacheSyncs = append(nsi.cacheSyncs, informer.HasSynced)
    return nil
}
```

CRD 资源走的是另一条工厂链，但模式完全一致。TransportServer：

[internal/k8s/transport_server.go:52-61](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/transport_server.go#L52-L61)

```go
func (nsi *namespacedInformer) addTransportServerHandler(handlers cache.ResourceEventHandlerFuncs) error {
    informer := nsi.confSharedInformerFactory.K8s().V1().TransportServers().Informer()
    if _, err := informer.AddEventHandler(handlers); err != nil {
        return fmt.Errorf("failed to add TransportServer event handler: %w", err)
    }
    nsi.transportServerLister = informer.GetStore()
    nsi.cacheSyncs = append(nsi.cacheSyncs, informer.HasSynced)
    return nil
}
```

同理，Service 用 core 工厂 [service.go:130-139](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/service.go#L130-L139)，Policy 用 CRD 工厂 [policy.go:50-59](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/policy.go#L50-L59)。唯一「不挂 handler」的是 Pod：[controller.go:692-697](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L692-L697) 的 `addPodHandler()` 只取 Lister、不 `AddEventHandler`——因为 Pod 变化不直接触发调谐，只在 sync 期被 Service 的 selector 查询用到（被动读缓存）。

#### 4.4.4 代码实践（源码阅读型 + 修改观察型）

**目标**：用 `Grep` 一次性列出全部 `addXHandler`，并核对它们的工厂来源与是否挂 handler。

1. 在 `internal/k8s` 下搜索 `func (nsi *namespacedInformer) add`，应得到约 14 个匹配（含 App Protect）。
2. 制表：每个 handler 取自哪个工厂、是否 `AddEventHandler`。
3. **修改观察**：在 `addIngressHandler` 的 `AddEventHandler` 之后临时加一行日志（示例代码，非项目原代码）：

   ```go
   nl.Debugf(lbc.Logger, "Ingress handler registered for namespace %s", nsi.namespace) // 示例代码
   ```

   注意 `nsi` 方法里没有 `lbc`，需把 logger 经参数传入或改用包级 logger；此处仅说明思路，不要求真能编译。
4. **预期结果**：你会确认「除 Pod 外，所有被监听资源都挂了事件处理器」，并且事件处理器最终都汇聚到 `AddSyncQueue`。

> 待本地验证项：Pod 不挂 handler 的设计——可在 [controller.go:4036](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L4036) 看到 `podLister.ListByNamespace` 的被动读取用法。

#### 4.4.5 小练习与答案

**练习 1**：为什么几乎每个 `addXHandler` 都要把 `informer.HasSynced` append 到 `nsi.cacheSyncs`？

**参考答案**：`Run()` 启动时要 `WaitForCacheSync(全部 HasSynced...)`，只有把每个 Informer 的同步函数都登记进来，才能保证「开始调谐前，所有资源的初始 LIST 都已完成」。

**练习 2**：`addPodHandler` 与其他 `addXHandler` 的关键区别是什么？为什么？

**参考答案**：它不 `AddEventHandler`，只建 Lister。因为 Pod 变化不直接驱动 NGINX 重配——Pod 信息是在 sync Service 时按 selector 被动查询的，不需要把每个 Pod 变化都转成一次入队，避免事件风暴。

---

## 5. 综合实践

**任务**：用一张图把「一个 Ingress 更新事件，从 API Server 到进入 `syncQueue` 之前」的完整路径画出来，并标注每一步对应的源码位置。

提示步骤：

1. API Server 推送 Ingress 变更 → `sharedInformerFactory` 的 Ingress Informer 收到事件。
2. Informer 调用挂载的 `createIngressHandlers(lbc)` 的 `UpdateFunc`（定义于 handlers.go，本讲不展开，u3-l4 详述）。
3. handler 内部调用 `lbc.AddSyncQueue(obj)`（[controller.go:664-666](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L664-L666)）。
4. 该 Ingress Informer 必然属于某个 `namespacedInformer`，它是在 `newNamespacedInformer`（[controller.go:551-603](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L551-L603)）里通过 `addIngressHandler`（[controller.go:681-690](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L681-L690)）挂上的。
5. 该 nsi 由 `Run()`（[controller.go:780-783](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L780-L783)）`start()`，且只有在 `WaitForCacheSync`（[controller.go:807](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L807)）通过后，`syncQueue` 才会取出这个 task。

**交付物**：一张流程图（文字版即可）+ 每个节点旁标出 `文件:行号` 与一句话职责说明。完成后，你就把本讲四个最小模块（SharedInformerFactory、namespacedInformer 结构、按命名空间隔离、handler 注册）串成了一条完整链路。

## 6. 本讲小结

- NIC 用 **raw client-go 的 SharedInformerFactory + list-watch** 感知集群变化，resync 周期硬编码 30 秒；所有 informer 在 `Run()` 里统一 `Start` 并 `WaitForCacheSync` 后才启动 `syncQueue`。
- **`namespacedInformer`** 是「一个命名空间内全部监听」的聚合体，内含 core / Secret / CRD / dynamic 四类按需创建的工厂，外加各资源 Lister 与 `cacheSyncs`。
- **按命名空间隔离**支持三种范围：精确列表（`-watch-namespace`）、全局（key `""`）、动态（`-watch-namespace-label`，靠监听 Namespace 自身动态增删 nsi）。
- **`addXHandler`** 是统一三步模式（取 Informer → 挂 `AddEventHandler` → 取 Lister 并登记 `HasSynced`）；不同资源走不同 typed 工厂链，是 raw client-go 强类型风格的体现；Pod 例外，只建 Lister 不挂 handler。
- 本讲只覆盖「感知」（数据进缓存 + 入队），事件处理器的变更检测细节与 `syncQueue` 的出队/退避留待 u3-l3、u3-l4、u3-l5。

## 7. 下一步学习建议

- **u3-l3（任务队列与 workqueue 模式）**：承接本讲「事件 → `AddSyncQueue`」的桥，讲 task 如何入队、出队、失败重试与指数退避。
- **u3-l4（事件处理器）**：展开 `createIngressHandlers` / `createSecretHandlers` 等的 AddFunc/UpdateFunc/DeleteFunc 细节，重点看 UpdateFunc 如何做变更检测避免无效重载、如何处理 `DeletedFinalStateUnknown`。
- 建议先读 [internal/k8s/task_queue.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go) 与 [internal/k8s/handlers.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/handlers.go)，再回头看本讲的 `addXHandler`，体会「感知层只负责把事件变成 task，处理交给 queue+sync」的分层。
