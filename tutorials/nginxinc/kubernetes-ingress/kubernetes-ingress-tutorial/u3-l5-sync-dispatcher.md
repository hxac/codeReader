# sync 调度器与各资源 sync 函数

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `sync(task)` 是如何用一个 `switch task.Kind` 把任务分发给 `syncIngress`、`syncVirtualServer`、`syncConfigMap` 等具体函数的。
- 记住 `syncX` 函数的「三段式通用模式」：从 Lister 取资源 → 调用 `configuration.AddOrUpdateX / DeleteX` 更新内存模型 → 用 `processChanges / processProblems` 处理返回结果。
- 看懂「变更（changes）」与「问题（problems）」两条处理通道各自负责什么。
- 解释队列堆积时控制器如何用 `DisableReloads / EnableReloads` 把多次重载合并成一次（批量优化），以及启动期一次性 reload 的特殊路径。
- 区分通用 sync 与特殊 sync：ConfigMap、Service、EndpointSlice 为什么不严格遵循三段式。

## 2. 前置知识

本讲承接 u3-l3（任务队列）与 u3-l4（事件处理器），并把视野推进到「真正干活」的那一层。在进入源码前，先用三句话回顾三件你已经知道的事：

1. **taskQueue 只负责调度，不负责干活。** u3-l3 讲过，`syncQueue` 是 client-go workqueue 的薄封装，单 worker 串行消费。worker 每出队一个 `task{Kind, Key}`，就会回调构造时注入的 `sync` 函数（[internal/k8s/controller.go:353](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L353)）。本讲的主角就是这个 `sync` 函数本身。

2. **感知层只入队，调谐层才取对象。** u3-l4 讲过，事件处理器（`createXHandlers`）唯一的职责是把对象 `AddSyncQueue` 进队列，task 里只存 `Kind` 和 `Key`（namespace/name），**不存对象本身**。所以 sync 函数的第一件事，就是用 `Key` 反查 Lister 拿到最新对象。这是「轻 handler + 重 sync」设计取向的体现。

3. **三个关键对象各司其职。** 你会在本讲频繁遇到它们：
   - `lbc.configuration`（`*Configuration`）：**内存模型**，按类型/host 索引全部资源，负责检测引用是否合法、host 是否冲突，并算出「这次变更影响了哪些资源」（u3-l6 会精读）。
   - `lbc.configurator`（`*configs.Configurator`）：**配置生成器**，把扩展资源翻译成 NGINX 配置文件并触发 reload（u4-l1 会精读）。
   - `lbc.statusUpdater`：**状态回写器**，把资源状态（Valid/Warning/Invalid）和外部 LB 地址写回 Kubernetes（u3-l7 会精读）。

sync 函数就是把三者串起来的「胶水」。

> 一个易混点：本讲只讲「sync 如何分发与编排」，**不讲** Configuration 内部如何建模、Configurator 内部如何渲染模板——那是 u3-l6 与整个第 4 单元的内容。本讲把它们当作黑盒，只关注它们的「输入/输出契约」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [internal/k8s/controller.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go) | 绝对主角：`sync` 分发器、`syncIngress`、`syncVirtualServer`、`processChanges`、`processProblems`、`updateAllConfigs` 以及批量/启动优化全部在此。 |
| [internal/k8s/transport_server.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/transport_server.go) | `syncTransportServer`——三段式的另一个样板，和 syncVirtualServer 几乎同构。 |
| [internal/k8s/configmap.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configmap.go) | `syncConfigMap`——特殊 sync 的代表：不走三段式，而是改全局配置后整体重生。 |
| [internal/k8s/service.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/service.go) | `syncService`——特殊 sync：先处理外部状态 Service，再处理被引用的资源。 |
| [internal/k8s/endpoint_slice.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/endpoint_slice.go) | `syncEndpointSlices`——特殊 sync：在 Plus 下走动态端点更新，**可能不触发 reload**。 |
| [internal/k8s/configuration.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go) | `ResourceChange`/`ConfigurationProblem`/`Operation` 类型定义，以及 `AddOrUpdateVirtualServer`/`DeleteVirtualServer` 等返回值契约。 |

---

## 4. 核心概念与源码讲解

### 4.1 sync 分发器：一个 switch 把 task 路由到具体处理函数

#### 4.1.1 概念说明

`sync` 是注册给 taskQueue 的处理函数（[controller.go:353](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L353)：`newTaskQueue(lbc.Logger, lbc.sync)`）。worker 每出队一个 task，就调用 `lbc.sync(task)`。

但 `sync` 自己**几乎不做业务逻辑**——它是一个「调度器/编排器」。它的核心是一个 `switch task.Kind`，按资源种类把 task 转交给对应的 `syncX` 函数，并在 switch 前后统一处理三件横切事务：

- **批量模式判断**（switch 之前）：队列堆积时关闭重载。
- **Plus zone-sync 收尾**（switch 之后）：ConfigMap/Service 变更后同步 headless service。
- **启动收尾 / 批量收尾**（switch 之后）：队列排空时做一次性 reload。

这种「分发器 + 横切处理」的写法是控制器代码的典型范式：把「分发」与「干活」分开，横切优化只写一次。

#### 4.1.2 核心流程

```
taskQueue worker
      │  出队 task{Kind, Key}
      ▼
lbc.sync(task)
      │
      ├─ [1] 批量模式判断：isNginxReady && Len()>1 && !batchSyncEnabled
      │        → configurator.DisableReloads() + batchSyncEnabled=true
      │
      ├─ [2] spiffe 加锁（仅 mesh 场景）
      │
      ├─ [3] switch task.Kind ──────────────────────────┐
      │       ingress            → syncIngress          (+ updateMetrics)
      │       virtualserver      → syncVirtualServer    (+ updateMetrics)
      │       virtualServerRoute → syncVirtualServerRoute
      │       transportserver    → syncTransportServer
      │       policy             → syncPolicy
      │       configMap          → syncConfigMap
      │       service            → syncService
      │       endpointslice      → syncEndpointSlices
      │       secret             → syncSecret
      │       namespace          → syncNamespace
      │       globalConfiguration→ syncGlobalConfiguration
      │       appProtect* / ingressLink ...
      │       └─────────────────────────────────────────
      │
      ├─ [4] [Plus] 若 Kind∈{configMap,service} → syncZoneSyncHeadlessService
      │
      ├─ [5] [启动收尾] !isNginxReady && Len()==0
      │        CompleteStartup → EnableReloads → updateAllConfigs
      │        → isNginxReady=true → flushPendingStatusesAsync
      │
      └─ [6] [批量收尾] batchSyncEnabled && Len()==0
               EnableReloads → updateAllConfigs 或 ReloadForBatchUpdates
```

#### 4.1.3 源码精读

`sync` 函数全貌（[internal/k8s/controller.go:1182-1319](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1182-L1319)）。先看它的 switch 分发段——这是本模块的核心：

```go
switch task.Kind {
case ingress:
    lbc.syncIngress(task)
    lbc.updateIngressMetrics()
    lbc.updateTransportServerMetrics()
case configMap:
    if lbc.batchSyncEnabled {
        lbc.updateAllConfigsOnBatch = true
    }
    lbc.syncConfigMap(task)
case endpointslice:
    resourcesFound := lbc.syncEndpointSlices(task)
    if lbc.batchSyncEnabled && resourcesFound {
        lbc.enableBatchReload = true
    }
case secret:
    lbc.syncSecret(task)
case service:
    lbc.syncService(task)
// ... virtualserver / virtualServerRoute / globalConfiguration /
//     transportserver / policy / appProtect* / ingressLink ...
}
```

> 引自 [controller.go:1198-1250](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1198-L1250)。**说明**：这是「调度器」本身——一个纯粹的 `Kind → 函数` 路由表。注意每个 case 还会顺手做一些横切小事：Ingress/VS 处理后刷新指标；ConfigMap 在批量模式下置一个 `updateAllConfigsOnBatch` 标志；EndpointSlice 据是否命中资源决定是否启用批量重载。

值得单独一提的是 **EndpointSlice 的返回值**：它是唯一一个 sync 函数有返回值（`bool`），见 [internal/k8s/endpoint_slice.go:62](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/endpoint_slice.go#L62)。这个 `resourcesFound` 告诉调度器「这次端点变更是否真的影响了我管的资源」，从而决定批量收尾时要不要 reload——因为 EndpointSlice 更新非常频繁，很多时候并不影响任何路由。

#### 4.1.4 代码实践

1. **实践目标**：用肉眼把 `sync` 的 switch 看成一张「路由表」，并核对每种 Kind 都有对应的 syncX。
2. **操作步骤**：
   - 打开 [controller.go:1198](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1198)。
   - 列出 switch 中所有 `case`，统计有多少种 Kind。
   - 对每个 case，用编辑器跳转到它调用的 `syncX` 函数，确认函数存在。
3. **需要观察的现象**：你会发现 `ingress`、`virtualserver`、`virtualServerRoute`、`transportserver`、`policy`、`configMap`、`service`、`endpointslice`、`secret`、`namespace`、`globalConfiguration`，以及 `appProtect*` 系列、`ingressLink`。
4. **预期结果**：路由表与 `taskQueue` 里 `newTask` 的 `Kind` 枚举（u3-l3 讲过）一一对应，没有「分发了但没人处理」的死分支。
5. 本实践为**源码阅读型**，无需运行。

#### 4.1.5 小练习与答案

**练习**：假设有人新增了一种资源 `Foo`，但忘了在 `sync` 的 switch 里加 `case foo`，会发生什么？

**参考答案**：`sync` 会静默落入 default（什么都不做），`task` 被处理后丢弃，`Foo` 永远不会被调谐，也不会报错。这正是为什么「分发器必须覆盖每一种 Kind」是该控制器的一个隐性约束——它没有 fail-fast，全靠 codegen 与测试把关。

---

### 4.2 syncX 通用三段式：取资源 → 更新内存模型 → 处理变更

#### 4.2.1 概念说明

绝大多数 `syncX`（Ingress、VirtualServer、VSR、TransportServer、Policy）都长得几乎一模一样，遵循同一个「三段式」：

1. **取资源**：用 `task.Key` 反查对应资源的 Lister，拿到对象或确认它已不存在（删除）。
2. **更新内存模型**：把对象交给 `lbc.configuration` 的 `AddOrUpdateX` 或 `DeleteX`。这一步**不直接生成 NGINX 配置**，而是更新内存模型并算出「这次变更影响了哪些资源、产生了哪些问题」，返回 `(changes []ResourceChange, problems []ConfigurationProblem)`。
3. **处理变更 + 处理问题**：把 `changes` 交给 `processChanges`（它才真正调 `configurator` 写配置），把 `problems` 交给 `processProblems`（记事件 + 回写 status）。

关键直觉：**Configuration 负责「决策与扩散」——一个 VirtualServer 改 host 可能波及多个冲突的资源；Configurator 负责「执行」——把决策翻译成文件并 reload。** syncX 只是把两者用统一流程接起来。

#### 4.2.2 核心流程

以 `syncVirtualServer` 为例：

```
syncVirtualServer(task)
   │
   ├─ [取资源] ns ← SplitMetaNamespaceKey(key)
   │           virtualServerLister.GetByKey(key) → (obj, vsExists, err)
   │           err≠nil → syncQueue.Requeue(task, err)  // 至少一次投递
   │
   ├─ [更新内存模型]
   │     vsExists  → changes,problems = configuration.AddOrUpdateVirtualServer(vs)
   │     !vsExists → changes,problems = configuration.DeleteVirtualServer(key)
   │
   └─ [处理]
         processChanges(changes)    // 内部调 configurator.AddOrUpdateVirtualServer / DeleteVirtualServer
         processProblems(problems)  // 记事件 + 回写 status=Warning/Invalid
```

返回值契约（来自 [configuration.go:63-79](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L63-L79)）：

```go
type ResourceChange struct {
    Op       Operation  // AddOrUpdate 或 Delete
    Resource Resource   // 受影响的资源（VS/Ingress/TS 配置）
    Error    string
}

type ConfigurationProblem struct {
    Object  runtime.Object  // 出问题的资源对象
    IsError bool            // true→status=Invalid；false→status=Warning
    Reason  string
    // ...
}
```

`Operation` 只有两个取值（[configuration.go:31-39](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L31-L39)）：`Delete = iota` 与 `AddOrUpdate`。

#### 4.2.3 源码精读

`syncVirtualServer` 完整代码（[internal/k8s/controller.go:1410-1439](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1410-L1439)），三段式一目了然：

```go
func (lbc *LoadBalancerController) syncVirtualServer(task task) {
    key := task.Key
    // ... 

    ns, _, _ := cache.SplitMetaNamespaceKey(key)
    obj, vsExists, err := lbc.getNamespacedInformer(ns).virtualServerLister.GetByKey(key)
    if err != nil {
        lbc.syncQueue.Requeue(task, err)   // [段一失败→重入队] 至少一次投递
        return
    }

    var changes []ResourceChange
    var problems []ConfigurationProblem

    if !vsExists {
        nl.Debugf(lbc.Logger, "Deleting VirtualServer: %v\n", key)
        changes, problems = lbc.configuration.DeleteVirtualServer(key)   // [段二]
    } else {
        nl.Debugf(lbc.Logger, "Adding or Updating VirtualServer: %v\n", key)
        vs := obj.(*conf_v1.VirtualServer)
        changes, problems = lbc.configuration.AddOrUpdateVirtualServer(vs) // [段二]
    }

    lbc.processChanges(changes)    // [段三]
    lbc.processProblems(problems)  // [段三]
}
```

**说明**：注意三个细节——(1) `Requeue` 保证了 u3-l3 讲过的「至少一次投递」；(2) 增改与删除共用同一段「处理」逻辑，只是 `configuration` 侧入口不同；(3) sync 函数本身**不接触 `configurator`**，真正的配置生成被推迟到 `processChanges` 里。

对照看 `syncTransportServer`（[transport_server.go:63-90](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/transport_server.go#L63-L90)），结构完全同构——只是把 `virtualServerLister` 换成 `transportServerLister`、`AddOrUpdateVirtualServer` 换成 `AddOrUpdateTransportServer`。再看 `syncIngress`（[controller.go:1932-1960](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1932-L1960)）也是同一个模子。**记住一个，就记住了这一族。**

那么 `processChanges` 内部到底怎么调 `configurator`？这是「两条通道」的核心，见 [controller.go:1490-1588](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1490-L1588)。其关键骨架（精简后）：

```go
for _, c := range changes {
    if c.Op == AddOrUpdate {
        switch impl := c.Resource.(type) {
        case *VirtualServerConfiguration:
            vsEx := lbc.createVirtualServerEx(impl.VirtualServer, impl.VirtualServerRoutes, ...)
            warnings, err := lbc.configurator.AddOrUpdateVirtualServer(vsEx)  // ← 真正写配置 + reload
            lbc.updateVirtualServerStatusAndEvents(impl, warnings, err)
        case *IngressConfiguration:
            // 分 IsMaster 走 mergeable / regular
        case *TransportServerConfiguration:
            tsEx := lbc.createTransportServerEx(...)
            warnings, err := lbc.configurator.AddOrUpdateTransportServer(tsEx)
            // ...
        }
    } else if c.Op == Delete {
        // 调 configurator.DeleteX，并处理删后状态
    }
}
```

> 引自 [controller.go:1490-1520](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1490-L1520)（AddOrUpdate 分支）。**说明**：注意 `c.Resource` 的类型不再是原始的 `*VirtualServer`，而是 `*VirtualServerConfiguration`——这是 Configuration 在内存模型里算好的「VS + 它引用的 VSR + selectors」组合包。`createVirtualServerEx` 再补上 endpoints、secrets 等「扩展资源」，最终交给 `configurator.AddOrUpdateVirtualServer(vsEx)`。

另一条通道 `processProblems`（[controller.go:1441-1488](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1441-L1488)）则把 `IsError` 翻译成 status 状态：

```go
for _, p := range problems {
    lbc.recorder.Event(p.Object, eventType, p.Reason, p.Message)
    if lbc.reportCustomResourceStatusEnabled() {
        state := conf_v1.StateWarning
        if p.IsError {
            state = conf_v1.StateInvalid
        }
        switch obj := p.Object.(type) {
        case *conf_v1.VirtualServer:
            lbc.statusUpdater.UpdateVirtualServerStatus(obj, state, p.Reason, p.Message)
        // ... TransportServer / VSR / Ingress ...
        }
    }
}
```

**说明**：problems 通道专门处理「校验失败、host 冲突、孤儿 minion」这类需要回写 Invalid/Warning 状态的情况，它**不进** `processChanges`、也**不会被**批处理或启动优化延迟——因为问题数量受限于错误配置而非资源总量（见代码注释 [controller.go:1454-1462](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1454-L1462)）。

#### 4.2.4 代码实践

**本讲的主实践任务**——画出 `syncVirtualServer` 从拿到 task 到调用 `configuration` 与 `configurator` 的调用链。

1. **实践目标**：亲手跟踪一条完整的「事件 → 内存模型 → 配置生成」链路，建立端到端心智模型。
2. **操作步骤**：
   - 起点：假设一个 VirtualServer 被 `kubectl apply` 更新。
   - 在 [controller.go:1410](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1410) 的 `syncVirtualServer` 入口，画出第一跳：`virtualServerLister.GetByKey` 取对象。
   - 第二跳（[controller.go:1434](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1434)）：`configuration.AddOrUpdateVirtualServer(vs)` → 返回 `(changes, problems)`。这一步在内存模型里完成 host 冲突检测、引用校验，**不碰 NGINX**。
   - 第三跳（[controller.go:1437](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1437)）：`processChanges(changes)` → 内部对每个 `*VirtualServerConfiguration` 调 [controller.go:1497-1500](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1497-L1500) 的 `createVirtualServerEx` + `configurator.AddOrUpdateVirtualServer(vsEx)`。**这一步才真正生成 NGINX 配置并（可能）reload。**
   - 第四跳（[controller.go:1438](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1438)）：`processProblems(problems)` → 若有冲突/校验失败，回写 `.status.state=Invalid/Warning`。
3. **需要观察的现象**：`configuration`（内存模型、决策、扩散）与 `configurator`（执行、写文件、reload）被清晰分层，syncVirtualServer 只编排，不越层。
4. **预期结果**：得到一张类似下面的调用链图。

```
syncVirtualServer(task)
  └─ virtualServerLister.GetByKey                 // 取资源
  └─ configuration.AddOrUpdateVirtualServer(vs)   // 内存模型 + 冲突/引用校验
       返回 (changes, problems)
  └─ processChanges(changes)
       └─ createVirtualServerEx(...)              // 补 endpoints/secrets
       └─ configurator.AddOrUpdateVirtualServer   // 生成 + 渲染 + reload
       └─ updateVirtualServerStatusAndEvents      // 回写 status=Valid/Warning
  └─ processProblems(problems)
       └─ statusUpdater.UpdateVirtualServerStatus // 回写 status=Invalid
```

5. 本实践为**源码阅读型**，跟踪无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `processChanges` 里调用 `configurator.AddOrUpdateVirtualServer` 时，传入的是 `vsEx`（扩展资源）而不是原始的 `*VirtualServer`？

**参考答案**：因为 Configurator 要渲染 NGINX 配置，需要的不仅是 VS 的 spec，还有它引用的 VSR、上游 Service 的 endpoints、TLS Secret 等运行期信息。`createVirtualServerEx` 就负责把这些「扩展」信息组装进 `VirtualServerEx`。原始 `*VirtualServer` 只有声明式 spec，不够生成配置。

**练习 2**：`syncVirtualServer` 里 `err != nil` 时调用 `syncQueue.Requeue(task, err)`，这与 u3-l3 讲的「无指数退避」是否矛盾？

**参考答案**：不矛盾。`Requeue` 是「立即重入队」，配合 workqueue 的去重，只是把同一个 task 重新放回队列等待下次处理，并没有 `RateLimitingInterface` 的退避。重试成本由「单 worker 串行 + 去重」天然节流，与 u3-l3 的结论一致。

---

### 4.3 批量处理与重载优化：DisableReloads / EnableReloads

#### 4.3.1 概念说明

NGINX 的 reload 是**昂贵**的：每次 reload 都要 fork 新 worker、加载配置、优雅切换。如果队列里堆积了 100 个 task（比如批量重建集群时），逐个 reload 会导致 100 次 reload——既慢又可能压垮 NGINX。

控制器的解法是「**批量模式**（batch mode）」：当**已就绪**且队列长度 > 1 时，进入批量模式，调用 `configurator.DisableReloads()` 暂时关闭 reload；所有 task 处理完（队列排空）后再 `EnableReloads()`，统一做一次 reload（或一次全量重生）。这与 u3-l3 讲的「单 worker 串行」配合：因为是串行的，`Len()==0` 才是可靠的「这批处理完了」信号。

注意批量模式**只在启动后**生效（条件里有 `isNginxReady`）。启动期（`!isNginxReady`）走的是另一条更激进的「一次性 reload」路径。

#### 4.3.2 核心流程

```
sync(task) 进入
   │
   ├─ [入口判断] isNginxReady && Len()>1 && !batchSyncEnabled
   │     → DisableReloads()；batchSyncEnabled=true
   │
   │  ... 处理这个 task（不 reload）...
   │  ... 后续 task 继续进 sync，因 batchSyncEnabled 已 true，不再重复 DisableReloads ...
   │
   └─ [出口判断] batchSyncEnabled && Len()==0
         → batchSyncEnabled=false；EnableReloads()
         → 若期间有 ConfigMap 变更(updateAllConfigsOnBatch)：updateAllConfigs()（全量重生+一次 reload）
           否则：ReloadForBatchUpdates(enableBatchReload)（只 reload 受影响的）
```

启动期一次性 reload（与批量模式**互斥**的另一条路径）：

```
sync(task) 进入，且 !isNginxReady
   │  ... 逐个处理 task，期间每次 processChanges 也会 DisableReloads 生效（见 4.3.3 注意点）...
   │
   └─ [启动收尾] !isNginxReady && Len()==0
         → CompleteStartup()：一次性重建 host→resource 映射，检测冲突/孤儿
         → EnableReloads() + updateAllConfigs()：从内存态生成全部配置，单次 reload
         → isNginxReady = true
         → flushPendingStatusesAsync()：后台并行刷写所有被推迟的 status
```

#### 4.3.3 源码精读

批量模式入口（[controller.go:1182-1188](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1182-L1188)）：

```go
func (lbc *LoadBalancerController) sync(task task) {
    if lbc.isNginxReady && lbc.syncQueue.Len() > 1 && !lbc.batchSyncEnabled {
        lbc.configurator.DisableReloads()
        lbc.batchSyncEnabled = true
        nl.Debugf(lbc.Logger, "Batch processing %v items", lbc.syncQueue.Len())
    }
    // ...
```

**说明**：三个条件缺一不可——`isNginxReady` 保证「启动期不进批量模式」；`Len() > 1` 保证「只有真堆积才批量」；`!batchSyncEnabled` 保证「只开关一次」。

批量收尾（[controller.go:1305-1318](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1305-L1318)）：

```go
if lbc.batchSyncEnabled && lbc.syncQueue.Len() == 0 {
    lbc.batchSyncEnabled = false
    lbc.configurator.EnableReloads()
    if lbc.updateAllConfigsOnBatch {
        lbc.updateAllConfigs()                  // ConfigMap 改了→全量重生
    } else {
        if err := lbc.configurator.ReloadForBatchUpdates(lbc.enableBatchReload); err != nil {
            nl.Errorf(lbc.Logger, "error reloading for batch updates: %v", err)
        }
    }
    lbc.enableBatchReload = false
}
```

**说明**：排空后根据「期间是否改过 ConfigMap」二选一——改过就 `updateAllConfigs()`（全量），没改就 `ReloadForBatchUpdates()`（只 reload 受影响部分）。`updateAllConfigs`（[controller.go:1038-1152](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1038-L1152)）会把全部内存资源重新生成配置，是「大重置」入口。

启动收尾（[controller.go:1261-1303](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1261-L1303)）注释里写得很清楚，核心四步：

```go
if !lbc.isNginxReady && lbc.syncQueue.Len() == 0 {
    // Step 1: 一次性 rebuildHosts()，检测冲突/孤儿
    _, problems := lbc.configuration.CompleteStartup()
    lbc.processProblems(problems)
    // Step 2: 从内存态生成全部配置 + 单次 reload
    lbc.configurator.EnableReloads()
    lbc.updateAllConfigs()
    // Step 2b: 刷新 Prometheus 指标
    lbc.updateIngressMetrics()
    // ...
    // Step 3: 标记就绪（先 ready 再刷 status，解耦「能转发」与「status 写回」）
    lbc.isNginxReady = true
    // Step 4: 后台并行刷写所有被推迟的 status
    lbc.flushPendingStatusesAsync()
}
```

> **注意点（容易误读）**：启动期 `!isNginxReady` 时，入口那段批量判断因为带 `isNginxReady` 条件**不会触发**，即启动期不会显式 `DisableReloads`。但启动期每个 syncX 处理的「先攒内存、最后一次性 reload」效果，靠的就是 Step 2 把全部内存状态一次性 `updateAllConfigs()`——这与 u3-l1 讲的「isNginxReady 启动优化」直接对应。

#### 4.3.4 代码实践

1. **实践目标**：理解「为什么要把 100 次 reload 压成 1 次」。
2. **操作步骤**：
   - 在 [controller.go:1183](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1183) 处的判断条件上各画一条下划线，分别标注「就绪」「堆积」「未在批量中」。
   - 想象一个场景：把 50 个 VirtualServer 一次性 `kubectl apply`。
3. **需要观察的现象**：第 1 个 task 进来时 `Len()==50 > 1`，触发 `DisableReloads`；其后 49 个 task 在批量模式下处理，全程不 reload；最后一个 task 处理完 `Len()==0`，触发一次 `ReloadForBatchUpdates`。
4. **预期结果**：50 次 reload 被合并为 1 次。若这 50 个里还混了 ConfigMap 变更，则走 `updateAllConfigs()` 全量路径。
5. 本实践为**源码阅读型**，无需运行。

#### 4.3.5 小练习与答案

**练习**：如果删掉入口判断里的 `isNginxReady` 条件，启动期会发生什么？

**参考答案**：启动期也会进入批量模式并 `DisableReloads`，但启动收尾分支 `!isNginxReady && Len()==0` 仍然会调 `EnableReloads()`，所以最终还是会 reload，逻辑上看似自洽。但真正的问题是：批量收尾分支 `batchSyncEnabled && Len()==0`（[controller.go:1305](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1305)）会先于 `isNginxReady=true` 被满足，导致启动期提前 `ReloadForBatchUpdates`，与启动收尾的 `updateAllConfigs` 顺序错乱，可能引发**重复 reload 或漏掉 CompleteStartup 的冲突检测**。因此 `isNginxReady` 条件是为了保证两条收尾路径互斥、顺序正确。

---

### 4.4 ConfigMap sync 与其它特殊 sync

#### 4.4.1 概念说明

并非所有 syncX 都遵循三段式。有三类「特殊 sync」因为语义不同而另起炉灶：

- **ConfigMap（全局配置）**：ConfigMap 改的是**全局** NGINX 参数（如 `worker-processes`、日志格式、超时），影响**所有**资源。所以它不能只更新「受影响的某几个资源」，而要 `updateAllConfigs()` 全量重生。这就是为什么 4.3 的批量收尾要特判 `updateAllConfigsOnBatch`。
- **Service**：一个 Service 可能既是「控制器的外部状态 Service」（用于回写 LB 地址），又「被某些路由资源引用」。所以它要分两段处理。
- **EndpointSlice**：端点（Pod IP）变更极其频繁。在 NGINX Plus 下，控制器走 **Plus API 动态更新 upstream**，**根本不需要 reload**；在 OSS 下才走重载。这是性能差异的关键来源（u5-l4 会展开）。

#### 4.4.2 核心流程

ConfigMap sync（[configmap.go:82-134](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configmap.go#L82-L134)）：

```
syncConfigMap(task)
   ├─ 按 key 区分：nginxConfigMap 还是 mgmtConfigMap，取对象缓存到 lbc.configMap/mgmtConfigMap
   ├─ 若 !isNginxReady：跳过（等启动收尾统一处理）
   ├─ 若 batchSyncEnabled：跳过（等批量收尾，由 updateAllConfigsOnBatch 触发）
   ├─ 若 ctx 已取消：跳过
   └─ updateAllConfigs()   // 全量重生 + reload
```

#### 4.4.3 源码精读

`syncConfigMap`（[configmap.go:82-134](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configmap.go#L82-L134)）：

```go
func (lbc *LoadBalancerController) syncConfigMap(task task) {
    key := task.Key
    if key == lbc.mgmtConfigMapName && lbc.isPodMarkedForDeletion() {
        return // 关停中跳过 mgmt
    }
    switch key {
    case lbc.nginxConfigMapName:
        obj, configExists, err := lbc.configMapLister.GetByKey(key)
        // ...
        if configExists {
            lbc.configMap = obj.(*v1.ConfigMap)
            // 读取 external-status-address 等关键字段
        } else {
            lbc.configMap = nil
        }
    case lbc.mgmtConfigMapName:
        // 同理更新 lbc.mgmtConfigMap
    }
    if !lbc.isNginxReady { return }      // 启动期跳过
    if lbc.batchSyncEnabled { return }   // 批量期跳过
    if err := lbc.ctx.Err(); err != nil { return } // 关停跳过
    lbc.updateAllConfigs()               // 全量重生
}
```

**说明**：注意三个「跳过」——它们保证 ConfigMap 的全量重生**只在稳态、非批量、未关停时**发生，避免在启动/批量阶段重复做昂贵的 `updateAllConfigs`。真正落地在 [configmap.go:133](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configmap.go#L133) 的 `lbc.updateAllConfigs()`。

> 这也解释了 4.3 里那个 `updateAllConfigsOnBatch` 标志：批量模式下 ConfigMap 变更被**记录**但不**立即执行**（这里 return 了），等批量收尾时由 [controller.go:1204-1206](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1204-L1206) 置位、[controller.go:1308-1309](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1308-L1309) 统一执行。

Service sync 的两段式（[service.go:206-277](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/service.go#L206-L277)）：第一段，若该 Service 是控制器自己的外部状态 Service，更新所有 Ingress/VS 的 status；第二段，`FindResourcesForService` 找到所有引用它的路由资源，调 `configurator.AddOrUpdateResources` 重生。注意 [service.go:255](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/service.go#L255) 的注释「we don't return here」——同一个 Service 可能同时满足两段，所以两段都要跑。

EndpointSlice sync（[endpoint_slice.go:62-147](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/endpoint_slice.go#L62-L147)）：它按资源类型分流——Ingress/mergeable/VirtualServer/TransportServer，分别调 `configurator.UpdateEndpoints*` 系列。在 Plus 下这些是**动态更新**（不 reload），这也是它要返回 `resourcesFound bool` 的原因：只有真正命中资源才需要在批量收尾时 reload。

#### 4.4.4 代码实践

1. **实践目标**：对比 ConfigMap 的「全量重生」与 VirtualServer 的「增量处理」，理解为什么前者特殊。
2. **操作步骤**：
   - 在 [configmap.go:133](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configmap.go#L133) 确认 ConfigMap 最终调用 `updateAllConfigs()`。
   - 跟进 [controller.go:1093-1096](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1093-L1096)：`resources := lbc.configuration.GetResources()` 拿**全部**资源，再 `UpdateConfig(resourceExes)`。
3. **需要观察的现象**：ConfigMap sync 拿的是 `GetResources()`（全部），而 syncVirtualServer 只处理当前那一个 VS。
4. **预期结果**：你能说清「ConfigMap 一个 key 的改动会触发所有资源重生；VirtualServer 的改动只重生它自己（及因冲突被波及的资源）」。
5. 本实践为**源码阅读型**，无需运行。

#### 4.4.5 小练习与答案

**练习**：为什么 ConfigMap sync 里有 `if lbc.batchSyncEnabled { return }`，而 syncVirtualServer 里没有类似判断？

**参考答案**：因为 syncVirtualServer 的 `processChanges → configurator.AddOrUpdateVirtualServer` 本身就被 `DisableReloads` 保护（批量模式下 Configurator 不会真的 reload，只更新内部状态），等批量收尾再统一 reload 即可，无需在 syncX 层跳过。但 `updateAllConfigs()` 是一个**自包含的全量重生 + reload 流程**，如果在批量模式下直接跑，会绕过批量优化、立刻触发一次 reload，所以必须在 syncConfigMap 层显式跳过，改由批量收尾的 `updateAllConfigsOnBatch` 分支统一调度。

---

## 5. 综合实践

**任务**：跟踪一次「修改 ConfigMap + 新增一个 VirtualServer」混合事件，分析它会触发几次 reload，并写出每一步经过的函数。

**背景**：你先 `kubectl apply` 改了一个全局 ConfigMap（比如加了 `worker-processes: 16`），紧接着又 apply 了一个新 VirtualServer。两个事件几乎同时入队（假设此时 `isNginxReady` 已为 true 且队列为空）。

**操作步骤**：

1. 画出两个 task 在 `syncQueue` 中的顺序（先 configMap 还是先 virtualserver 不保证，但都满足 `Len()>1`）。
2. 第 1 个 task 进 `sync`：因 `isNginxReady && Len()>1`，触发 `DisableReloads()` + `batchSyncEnabled=true`。
3. 分情况讨论：
   - 若第 1 个是 configMap：`sync` 里 [controller.go:1204-1206](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1204-L1206) 置 `updateAllConfigsOnBatch=true`，`syncConfigMap` 因批量模式 return。
   - 若第 1 个是 virtualserver：`syncVirtualServer` → `configuration.AddOrUpdateVirtualServer` → `processChanges` → `configurator.AddOrUpdateVirtualServer`（因 DisableReloads 而不真 reload）。
4. 第 2 个 task 进 `sync`：`batchSyncEnabled` 已 true，入口判断不再重复 DisableReloads；继续处理。
5. 第 2 个处理完后 `Len()==0`，进入 [controller.go:1305-1318](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1305-L1318) 批量收尾：因 `updateAllConfigsOnBatch=true`，走 `updateAllConfigs()`，`EnableReloads()` 后**一次** reload。

**预期结论**：尽管有 2 个 task、其中一个含全局配置变更，整个过程中 NGINX 只 reload **一次**。这正是批量优化与 `updateAllConfigsOnBatch` 标志协同的价值。

**交付物**：一张包含「task → sync 入口 → switch → syncX → configuration → processChanges → configurator → 收尾」的完整调用链图，并标注唯一一次 reload 发生的位置。

## 6. 本讲小结

- `sync(task)` 是一个**纯调度器**：用 `switch task.Kind` 把任务路由到 `syncX`，自身只做横切事务（批量判断、Plus zone-sync、启动/批量收尾）。
- 绝大多数 `syncX` 遵循**三段式**：取资源（Lister.GetByKey）→ 更新内存模型（`configuration.AddOrUpdateX/DeleteX`，返回 changes+problems）→ 处理变更与问题（`processChanges`/`processProblems`）。
- **两条处理通道**：`processChanges` 把 `ResourceChange` 翻译成 `configurator.AddOrUpdateX/DeleteX`（真正写配置 + reload）；`processProblems` 把 `ConfigurationProblem` 翻译成事件 + status 回写，且不被批量/启动优化延迟。
- Configuration（决策与扩散）与 Configurator（执行）被严格分层，syncX 只编排、不越层——`processChanges` 里传入的是组装好的扩展资源（`VirtualServerEx` 等）。
- **批量优化**：队列堆积时 `DisableReloads`，排空后 `EnableReloads` + 一次 `ReloadForBatchUpdates` 或 `updateAllConfigs`，把 N 次 reload 压成 1 次；启动期另有「一次性 CompleteStartup + updateAllConfigs」路径，二者靠 `isNginxReady` 互斥。
- **特殊 sync**：ConfigMap 走全量 `updateAllConfigs`（批量期显式跳过、改由收尾统一执行）；Service 两段式（外部状态 + 被引用）；EndpointSlice 在 Plus 下可动态更新不 reload，故需返回 `resourcesFound`。

## 7. 下一步学习建议

- 本讲的 `configuration.AddOrUpdateVirtualServer` 返回的 `changes/problems` 是黑盒，下一步精读 **u3-l6（Configuration 内存模型与引用/冲突检查）**，看清 host 冲突、孤儿 minion 是如何在内存模型里被算出来的。
- `processChanges` 里反复出现的 `configurator.AddOrUpdateVirtualServer` 是通往配置生成的大门，后续进入**第 4 单元**，从 **u4-l1（Configurator 概览）** 开始，看 `VirtualServerEx` 如何变成 `.conf` 文件并 reload。
- 批量优化里的 `DisableReloads/EnableReloads/ReloadForBatchUpdates` 实际落在 `internal/nginx` 的 Manager 上，对应 **u5-l1（nginx.Manager 与进程生命周期）**——届时你会看到「关闭 reload」在进程层到底意味着什么。
- `processProblems` 与 `flushPendingStatusesAsync` 涉及 leader 选举与 status 回写，细节在 **u3-l7（Leader 选举与 Status 回写）**。
