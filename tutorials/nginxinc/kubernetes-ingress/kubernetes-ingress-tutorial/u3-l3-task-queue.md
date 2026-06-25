# 任务队列与 workqueue 模式

## 1. 本讲目标

上一讲（u3-l2）我们讲清楚了「感知层」：Informer 用 list-watch 把集群里的资源变更变成一个个事件回调。但事件回调里**不能**直接去生成 NGINX 配置、reload NGINX——那会把大量耗时操作卡死在 Informer 的事件分发线程上，而且同一资源的连续多次变更会触发多次重复重载。

本讲聚焦 Ingress Controller 的「**调度层**」：`taskQueue`。它把上游的事件，**解耦**成下游可以反复消费的任务。读完本讲，你应当能够：

- 说清 `task` 与 `kind` 这两个数据结构的设计意图，以及 `newTask` 如何用一个 `switch` 把任意资源对象归类。
- 讲清楚 `taskQueue` 如何接入 client-go 的 `workqueue`，并解释 workqueue 自带的「去重 + 至少一次投递」语义。
- 画出 worker 循环的执行流程，理解为什么 NIC 是**单 worker、串行 sync** 的控制器。
- 区分 `Enqueue` / `Requeue` / `RequeueAfter` 三种入队语义，并准确说出：本项目**没有**指数退避（exponential backoff）。

## 2. 前置知识

在进入源码前，先建立两个直觉。

**直觉一：为什么要用「队列」把事件和处理器隔开？**

想象 Informer 在 1 毫秒内连续收到同一个 Ingress 的 5 次更新（用户连续 `kubectl apply`）。如果直接在回调里 reload NGINX，你会 reload 5 次，而 NGINX 真正需要的只是「最终状态」。队列在这里扮演两个角色：

1. **削峰**：把瞬时大量事件收拢，让下游按自己的节奏消费。
2. **去重**：同一个 key 连续入队多次，下游只处理一次「最终值」。

这正是 Kubernetes 控制器的标准范式：**事件 → 入队（key）→ worker 出队 → 调谐（reconcile）**。注意一个关键设计：队列里通常**只放 key（namespace/name 字符串）**，而不放资源对象本身。worker 出队后，再回头从 Informer 的本地缓存（Lister）里取**最新**的对象。这样即使事件排队期间对象又被改了，处理器拿到的也永远是最新值。

**直觉二：client-go 的 `workqueue` 不是普通队列。**

`k8s.io/client-go/util/workqueue` 提供的 `*Type`（用 `workqueue.NewNamed(...)` 创建）是一个**带去重和「处理中」标记**的 FIFO。它的核心保证有三条，请记住，后面源码精读会反复用到：

| 行为 | 含义 |
| --- | --- |
| 去重（dedup） | 同一个 item 在队列里只出现一份；重复 `Add` 会被忽略。 |
| 处理中标记（dirty/processing） | `Get` 后、`Done` 前这段时间，item 处于「处理中」。 |
| 至少一次（at-least-once） | 如果一个 item 正在被处理时又被 `Add`，那么 `Done` 之后它**会被重新入队**，保证「至少处理一次」。 |

这套机制让控制器既能去重、又不会丢事件。本讲后面会看到 NIC 的 `taskQueue` 完全建立在这三条语义之上。

> 衔接上一讲：上一讲里 `namespacedInformer` 的各 `addXHandler` 注册的事件回调，最终都汇聚到 `lbc.AddSyncQueue(obj)`，这正是本讲的入口。

## 3. 本讲源码地图

本讲涉及两个核心文件：

| 文件 | 行数范围 | 作用 |
| --- | --- | --- |
| [internal/k8s/task_queue.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go) | 全文（1–191） | 定义 `taskQueue`、`task`、`kind`、`newTask`，封装 client-go workqueue |
| [internal/k8s/controller.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go) | 353、663–666、813–816、825、1182–1319 | `taskQueue` 的装配、`AddSyncQueue` 入口、`Run`/`Stop` 生命周期、`sync` 分发函数 |
| [internal/k8s/handlers.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/handlers.go) | 21–54 | `createIngressHandlers`：事件回调如何调用 `AddSyncQueue`（作为入队示例） |

一句话定位：`task_queue.go` 是「调度层」的全部实现，`controller.go` 是它的**唯一使用者**（构造它、喂数据给它、把 `sync` 函数注入它、启停它）。

## 4. 核心概念与源码讲解

### 4.1 task 与 kind 设计

#### 4.1.1 概念说明

`taskQueue` 投递的不是「原始资源对象」，而是一个极简的轻量结构 `task`：它只携带**两样东西**——资源的「种类」`Kind` 和它的「地址」`Key`。

为什么这样设计？回顾前置知识里的直觉一：队列里只放 key，出队后再回查 Lister。NIC 严格执行这一点——`task` 里**没有**资源对象本身，只有一个字符串 `Key`（形如 `default/cafe-ingress`）。而 `Kind` 是一个内部整数枚举，用来在 worker 出队后告诉 `sync` 函数「该按哪类资源去调谐」。

把「种类」单独抽出来枚举，而不是出队时再用反射判断，有两个好处：一是分发逻辑（`switch task.Kind`）非常快且类型安全；二是即使 Lister 里此刻已经查不到该资源（比如它刚被删除），`Kind` 仍能指引走删除分支。

#### 4.1.2 核心流程

```
资源对象 obj（如 *networking.Ingress）
        │
        ▼ newTask(key, obj)：用 switch 把 obj 归类为某个 kind
task{Kind: ingress, Key: "default/cafe-ingress"}
        │
        ▼ 加入 taskQueue
        │
        ▼ worker 出队拿到 task
        │
        ▼ sync(task)：用 task.Kind 分发到 syncIngress 等
```

`newTask` 的分类逻辑覆盖了 NIC 监听的**全部资源类型**，这是本讲「源码范围」的一个全集快照。

#### 4.1.3 源码精读

先看 `task` 和 `kind` 的定义：

`kind` 是一个 `int`，用 `iota` 从 `ingress` 开始自增，穷举了所有可入队资源（[internal/k8s/task_queue.go:110-133](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go#L110-L133)）：

```go
type kind int

const (
    ingress = iota      // 0
    endpointslice       // 1
    configMap           // 2
    secret
    service
    namespace
    virtualserver
    virtualServerRoute
    globalConfiguration
    transportserver
    policy
    appProtectPolicy
    appProtectLogConf
    appProtectUserSig
    appProtectDosPolicy
    appProtectDosLogConf
    appProtectDosProtectedResource
    ingressLink
)
```

注意它不区分 v1（Ingress）与 v2（VS/VSR/TS）版本号，而是按**资源种类**枚举——这是控制器视角而非模板视角的划分。

`task` 结构体本身极其简单（[internal/k8s/task_queue.go:135-139](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go#L135-L139)）：

```go
type task struct {
    Kind kind
    Key  string
}
```

`newTask` 用一个 `switch obj.(type)` 完成「对象 → kind」的归类（[internal/k8s/task_queue.go:142-190](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go#L142-L190)）。绝大部分类型是直接类型断言；只有 `*unstructured.Unstructured`（App Protect 等 CRD 走 dynamic client，没有强类型）需要再读 `GetKind()` 二次分派：

```go
switch t := obj.(type) {
case *networking.Ingress:
    k = ingress
case *discovery_v1.EndpointSlice:
    k = endpointslice
// ... ConfigMap/Secret/Service/Namespace/VS/VSR/TS/Policy/GC ...
case *unstructured.Unstructured:
    if objectKind := obj.(*unstructured.Unstructured).GetKind(); objectKind == appprotect.PolicyGVK.Kind {
        k = appProtectPolicy
    } else if objectKind == appprotect.LogConfGVK.Kind {
        k = appProtectLogConf
    } // ... 其余 unstructured 分支 ...
    else {
        return task{}, fmt.Errorf("unknown unstructured kind: %v", objectKind)
    }
default:
    return task{}, fmt.Errorf("unknown type: %v", t)
}
return task{k, key}, nil
```

两处值得记住的设计点：

- `newTask` 对**未知类型返回 error**（`unknown type` / `unknown unstructured kind`）。这是一个**显式失败**而非静默丢弃——调用方 `Enqueue` 会据此放弃入队并打 Debug 日志（见 4.2.3）。
- `Key` 不是在 `newTask` 里算出来的，而是**外部传入**的（由 `keyFunc` 预先算好）。`keyFunc` 定义在 [internal/k8s/controller.go:247](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L247)，用的是 client-go 标准的 `cache.DeletionHandlingMetaNamespaceKeyFunc`，产出 `namespace/name` 格式字符串，并正确处理删除事件里可能出现的「墓碑」对象。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `newTask` 的归类表，理解「哪些资源能进队列」。

**操作步骤**：

1. 打开 [internal/k8s/task_queue.go:114-133](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go#L114-L133)，数一下 `kind` 枚举一共有多少个值。
2. 对照 [internal/k8s/task_queue.go:142-190](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go#L142-L190) 的 `newTask`，回答：哪些 kind 来自**强类型**断言（如 `*networking.Ingress`），哪些来自 **unstructured** 的二次分派（`GetKind()`）？
3. 思考：为什么不把所有资源都走 unstructured？

**需要观察的现象 / 预期结果**：你会发现强类型断言覆盖的是 client-go 自带 + NIC 自己 codegen 出来的类型（Ingress/Secret/Service/EndpointSlice/ConfigMap/Namespace/VS/VSR/TS/Policy/GC/DosProtectedResource），而 unstructured 分支只留给 **dynamic client 监听的第三方 CRD**（App Protect 系列、IngressLink）——因为它们没有 codegen 出强类型客户端。

> 第三步的答案属于设计取舍题，不要求运行命令，标记为「源码阅读型实践」。

#### 4.1.5 小练习与答案

**练习 1**：如果一个资源对象的 Go 类型不在 `newTask` 的 `switch` 里，`Enqueue` 会发生什么？

**参考答案**：`newTask` 走到 `default` 分支返回 `fmt.Errorf("unknown type: %v", t)`，`Enqueue` 拿到 err 后只打一条 Debug 日志就 `return`，**不会**把任何东西塞进队列。这是一种静默但有日志的安全降级，避免未知类型污染队列。

**练习 2**：为什么 `task` 里只存 `Key` 字符串而不存资源对象的指针？

**参考答案**：其一，对象指针会让队列持有快照，而出队处理时该对象可能已过时；其二，worker 出队后用 `Key` 去 Lister 取的是**最新**对象，天然实现「处理最终状态」。其三，轻量的 key 让去重更可靠（按字符串 key 去重），指针则无法稳定去重。

### 4.2 workqueue 接入

#### 4.2.1 概念说明

`taskQueue` 是一个**极薄的封装**：它把 client-go 的 `workqueue.Type` 包了一层，提供面向 NIC 语义的三个入口（`Enqueue` / `Requeue` / `RequeueAfter`）和一个生命周期管理（`Run` / `Shutdown`）。

理解本模块的关键，是区分「NIC 写的代码」和「workqueue 自带的语义」。NIC 的代码量很小，大部分正确性保证（去重、至少一次、并发安全）都是 `workqueue.Type` 免费提供的。所以这一节我们既要读 NIC 的薄封装，也要复述 workqueue 的三条语义（见前置知识），看清楚封装如何依赖它们。

#### 4.2.2 核心流程

`taskQueue` 结构与生命周期：

```
newTaskQueue(logger, syncFn)
   ├─ queue = workqueue.NewNamed("taskQueue")   ← 拿到一个带去重的 FIFO
   ├─ sync   = syncFn                            ← 注入「出队后调谁」
   └─ workerDone chan                             ← worker 退出时的通知信号

Run(period, stopCh)  → wait.Until(worker, period, stopCh)   ← 启动 worker
Enqueue(obj)         → keyFunc + newTask → queue.Add(task)  ← 正常入队
Requeue(task, err)   → queue.Add(task)                      ← 失败立即重入队
RequeueAfter(...)    → goroutine 内 sleep 后 queue.Add      ← 延迟入队（目前未被使用）
Shutdown()           → queue.ShutDown() + 等 workerDone     ← 优雅关闭
```

注意一条关键事实：`taskQueue` 字段里**只有一个** `queue`（[internal/k8s/task_queue.go:24-33](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go#L24-L33)），对应**一个** worker。这意味着所有资源（Ingress、VS、Secret……）共享同一条队列、同一个 worker——后面 4.3 会展开其影响。

#### 4.2.3 源码精读

结构体本身（[internal/k8s/task_queue.go:22-33](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go#L22-L33)）：

```go
// taskQueue manages a work queue through an independent worker that
// invokes the given sync function for every work item inserted.
type taskQueue struct {
    queue      *workqueue.Type   // worker 轮询的工作队列
    sync       func(task)        // 对每个出队 item 调用
    workerDone chan struct{}     // worker 退出时关闭
    logger     *slog.Logger
}
```

`newTaskQueue` 用 `workqueue.NewNamed("taskQueue")` 建队，并把 `syncFn` 存起来（[internal/k8s/task_queue.go:37-44](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go#L37-L44)）。`NewNamed` 给队列起了名字，便于 metrics 里区分；它返回的是 client-go 最基础的 `*Type`——**不是** `RateLimitingInterface`（这一点对 4.4 至关重要）：

```go
func newTaskQueue(logger *slog.Logger, syncFn func(task)) *taskQueue {
    return &taskQueue{
        queue:      workqueue.NewNamed("taskQueue"),
        sync:       syncFn,
        workerDone: make(chan struct{}),
        logger:     logger,
    }
}
```

`Enqueue` 是正常入队路径，也是事件回调唯一会走的路（[internal/k8s/task_queue.go:51-67](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go#L51-L67)）。它三步走：算 key → 造 task → `queue.Add`：

```go
func (tq *taskQueue) Enqueue(obj interface{}) {
    key, err := keyFunc(obj)        // namespace/name
    if err != nil {
        nl.Debugf(tq.logger, "Couldn't get key for object %v: %v", obj, err)
        return
    }
    task, err := newTask(key, obj)  // 归类 kind
    if err != nil {
        nl.Debugf(tq.logger, "Couldn't create a task for object %v: %v", obj, err)
        return
    }
    nl.Debugf(tq.logger, "Adding an element with a key: %v", task.Key)
    tq.queue.Add(task)              // 依赖 workqueue 的去重
}
```

重点体会最后 `tq.queue.Add(task)` 这一行：它把「去重」和「处理中再入队」的责任全部甩给了 `workqueue.Type`。如果同一个 `task` 在很短时间内被 `Add` 十次，`queue` 里只会有一份；如果它此刻正在被 worker 处理，`Add` 会把它标记为 dirty，`Done` 之后重新投递。

`Requeue` 是失败重入队（[internal/k8s/task_queue.go:69-73](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go#L69-L73)）——**立刻**再 `Add` 一次，没有任何延迟：

```go
func (tq *taskQueue) Requeue(task task, err error) {
    nl.Errorf(tq.logger, "Requeuing %v, err %v", task.Key, err)
    tq.queue.Add(task)
}
```

`Len` 只是个带日志的取长度封装（[internal/k8s/task_queue.go:75-79](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go#L75-L79)），但它在外部 `sync` 函数里有重要作用——用来判断「队列是否排空」从而决定是否触发批量重载与启动收尾（见 4.3）。

最后看 controller 是怎么把它装配起来的。构造时 `syncQueue` 字段绑定到 `lbc.sync`（[internal/k8s/controller.go:353](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L353)）：

```go
lbc.syncQueue = newTaskQueue(lbc.Logger, lbc.sync)
```

而事件回调都从 `AddSyncQueue` 这一个窄口子进入队列（[internal/k8s/controller.go:663-666](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L663-L666)）：

```go
func (lbc *LoadBalancerController) AddSyncQueue(item interface{}) {
    lbc.syncQueue.Enqueue(item)
}
```

以 Ingress 为例，`createIngressHandlers` 的 `UpdateFunc` 在确认 `hasChanges` 后调用 `AddSyncQueue`（[internal/k8s/handlers.go:45-52](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/handlers.go#L45-L52)）：

```go
UpdateFunc: func(old, current interface{}) {
    c := current.(*networking.Ingress)
    o := old.(*networking.Ingress)
    if hasChanges(o, c) {
        nl.Debugf(lbc.Logger, "Ingress %v changed, syncing", c.Name)
        lbc.AddSyncQueue(c)
    }
},
```

这条链完整串起来就是：**Informer Update 事件 → `UpdateFunc` 做变更检测 → `AddSyncQueue(c)` → `Enqueue` → `newTask` → `queue.Add(task)`**。变更检测（`hasChanges`）发生在入队**之前**，所以「无变化的更新」根本不会进队列——这是 NIC 节省无效重载的第一道闸门（下一讲 u3-l4 会专门讲 handler 的变更检测）。

#### 4.2.4 代码实践

**实践目标**：确认 `Requeue` 在真实代码里的全部调用点，理解它专用于什么场景。

**操作步骤**：

1. 在 `internal/` 下统计所有 `lbc.syncQueue.Requeue(task, err)` 的调用点（本讲编写时用检索得到约 18 处，分布在 `controller.go`、`configmap.go`、`service.go`、`transport_server.go`、`endpoint_slice.go`、`policy.go`、`appprotect_waf.go`、`appprotect_dos.go`、`namespace.go`、`global_configuration.go`、`ingress_link.go` 等）。
2. 抽看其中任意一处，例如 `syncVirtualServer` 里取 Lister 失败时（[internal/k8s/controller.go:1417-1421](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1417-L1421)）：

   ```go
   obj, vsExists, err = lbc.getNamespacedInformer(ns).virtualServerLister.GetByKey(key)
   if err != nil {
       lbc.syncQueue.Requeue(task, err)
       return
   }
   ```

3. 思考：为什么是 `Requeue` 而不是抛 panic 或直接放弃？

**需要观察的现象 / 预期结果**：你会发现 `Requeue` 几乎全部出现在「从 Lister 取对象出错」的位置。这类错误通常是**瞬时**的（缓存尚未同步、内部竞态），所以重试是合理的。`return` 保证当前这次 sync 不继续往下走（避免用 nil/错误数据生成配置），把 task 原样放回队尾等下一轮再试。

#### 4.2.5 小练习与答案

**练习 1**：同一个 Ingress 在 10ms 内触发了 3 次 `UpdateFunc`，`AddSyncQueue` 会被调用几次？`sync` 最终会被执行几次？

**参考答案**：3 次回调若都通过了 `hasChanges`，`AddSyncQueue`/`Enqueue` 会被调用 3 次，`queue.Add` 也调用 3 次。但得益于 `workqueue.Type` 的去重，队列里只保留 1 个 `task`；worker 取出来 `sync` 只执行 **1 次**（取的是 Lister 里的最终对象）。这就是「去重 + 处理最终状态」的威力。

**练习 2**：`AddSyncQueue` 接收的是 `interface{}`，`taskQueue` 内部却要把它变成具体的 `kind`。如果将来 NIC 新增一种资源却忘了在 `newTask` 里加分支，后果是什么？

**参考答案**：`newTask` 返回 `unknown type` 错误，`Enqueue` 静默放弃入队（仅 Debug 日志）。结果是这种资源的事件**永远不会被 sync 处理**——一个隐蔽的功能缺失。这正是为什么新增资源时必须同步更新 `kind` 枚举与 `newTask` 的 switch。

### 4.3 worker 循环与 sync 注入

#### 4.3.1 概念说明

前面两节解决了「怎么入队」，本节解决「怎么出队并处理」。`taskQueue` 的 worker 是一个死循环：不断 `Get` 一个 task、调用注入的 `sync`、`Done` 标记完成，直到队列被关闭。

这里有一个对初学者极易误判的点：**NIC 是单 worker 的控制器**。不要被 `Run(period, stopCh)` 里的 `period time.Second` 误导成「每秒起一个 worker」。实际上 worker 一旦启动就在一个无限循环里阻塞运行，直到 shutdown，所以全局只有**一条**串行处理 sync 的 goroutine。这个设计用吞吐量换取了简单与正确——所有配置生成与 reload 都是顺序发生的，不会交错。

#### 4.3.2 核心流程

worker 的一次循环：

```
loop {
    t, quit := queue.Get()        // 队列空则阻塞；shutdown 时 quit=true
    if quit { close(workerDone); return }
    sync(t.(task))                // 调用注入的 sync（即 controller.sync）
    queue.Done(t)                 // 标记处理完（若有重入队则随后再投递）
}
```

`Run` 的启动与 controller 的接线：

```
controller.Run()
   ├─ ...cache.WaitForCacheSync...   ← 先等缓存同步
   ├─ preSyncSecrets()               ← 预热 SecretStore
   └─ go syncQueue.Run(time.Second, ctx.Done())   ← 起唯一 worker
        └─ wait.Until(worker, 1s, stopCh)
```

#### 4.3.3 源码精读

worker 函数（[internal/k8s/task_queue.go:90-102](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go#L90-L102)）：

```go
func (tq *taskQueue) worker() {
    for {
        t, quit := tq.queue.Get()
        if quit {
            close(tq.workerDone)
            return
        }
        nl.Debugf(tq.logger, "Syncing %v", t.(task).Key)
        tq.sync(t.(task))
        tq.queue.Done(t)
    }
}
```

三处要点：

- `t.(task)` 是把 `workqueue` 返回的 `interface{}` 断言回 `task`。因为 `Enqueue`/`Requeue` 永远只往里塞 `task` 值，这个断言是安全的。
- `tq.sync(...)` 是**同步**调用——worker 在 `sync` 返回前不会去 `Get` 下一个。所以一次 NGINX 配置生成 + reload 全程都在这一个 goroutine 里串行发生。
- `queue.Done(t)` 在 `sync` 之后。如果 `sync` 执行期间该 task 又被 `Add`（例如又来了一次同 key 事件），workqueue 会在 `Done` 后把它重新投递，实现「至少一次」。

`Run` 用 `wait.Until` 把 worker 挂到 stopCh 上（[internal/k8s/task_queue.go:46-49](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go#L46-L49)）：

```go
func (tq *taskQueue) Run(period time.Duration, stopCh <-chan struct{}) {
    wait.Until(tq.worker, period, stopCh)
}
```

`wait.Until(f, period, stop)` 的语义是「反复调用 `f`，每次后等 `period`，直到 `stopCh` 关闭」。但这里 `f = tq.worker` **永远不会在正常运行时返回**（它死循环到 shutdown）。因此 `period`（1 秒）实际上是**惰性的**：`wait.Until` 第一次调用 `worker` 后就阻塞在「等 worker 返回」上，不会再每秒重启。换言之，全局只有一条 worker goroutine，`time.Second` 这个参数在稳态下不起作用。`wait.Until` 的真正价值只是「让 worker 跑在一个受 `stopCh` 感知的包装里」。

controller 在 `Run()` 末尾启动它（[internal/k8s/controller.go:813-816](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L813-L816)）：

```go
nl.Debugf(lbc.Logger, "Starting the queue with %d initial elements", lbc.syncQueue.Len())
go lbc.syncQueue.Run(time.Second, lbc.ctx.Done())
<-lbc.ctx.Done()
```

注意启动顺序的严格性：在 `go syncQueue.Run(...)` **之前**，`controller.Run()` 已经做了 `cache.WaitForCacheSync`（等所有 Lister 同步完）和 `preSyncSecrets`（预热 Secret）。这保证 worker 第一次 `Get` 出 task 去查 Lister 时，缓存是可用的——否则 `sync` 会因为读到空缓存而误判资源缺失。

`sync` 函数本身（[internal/k8s/controller.go:1182-1319](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1182-L1319)）就是一个按 `task.Kind` 的 `switch` 分发器。核心段落（[internal/k8s/controller.go:1198-1250](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1198-L1250)）：

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
// ... service / namespace / virtualserver / virtualServerRoute /
//     globalConfiguration / transportserver / policy / appProtect* / ingressLink
}
```

这个 `switch` 正是「worker 出队的 task」与「具体调谐逻辑」之间的桥梁，也是 4.1 节 `kind` 枚举的下游消费者：每种 `kind` 对应一个 `syncXxx` 函数（下一讲 u3-l5 会逐个展开）。

**串行单 worker 与批量优化**。正因为是单 worker，集群规模大时 task 会在队列里堆积。`sync` 开头检测到堆积就开启「批处理」模式（[internal/k8s/controller.go:1183-1188](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1183-L1188)）：

```go
if lbc.isNginxReady && lbc.syncQueue.Len() > 1 && !lbc.batchSyncEnabled {
    lbc.configurator.DisableReloads()   // 中途不再每条都 reload
    lbc.batchSyncEnabled = true
    nl.Debugf(lbc.Logger, "Batch processing %v items", lbc.syncQueue.Len())
}
```

等队列排空（`Len()==0`）时再一次性 reload（[internal/k8s/controller.go:1305-1318](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1305-L1318)）。这是单 worker 设计的一个直接后果与配套优化——`Len()` 这个看似普通的封装，在这里成了批处理的触发条件。

`Shutdown` 负责优雅关闭（[internal/k8s/task_queue.go:104-108](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go#L104-L108)）：

```go
func (tq *taskQueue) Shutdown() {
    tq.queue.ShutDown()
    <-tq.workerDone
}
```

`queue.ShutDown()` 会让阻塞在 `Get()` 上的 worker 立刻收到 `quit=true`，于是 worker `close(workerDone)` 并返回；`Shutdown` 阻塞在 `<-workerDone` 上，确保**等到 worker 真正退出**后才返回。controller 的 `Stop()` 调用它（[internal/k8s/controller.go:819-826](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L819-L826)）：

```go
func (lbc *LoadBalancerController) Stop() {
    lbc.cancel()              // 关 ctx，触发 Run 里的 <-ctx.Done() 返回
    for _, nif := range lbc.namespacedInformers {
        nif.stop()
    }
    lbc.syncQueue.Shutdown()  // 关队列并等 worker 退出
}
```

#### 4.3.4 代码实践

**实践目标**：通过阅读 `Run`/`worker`/`Shutdown`，确认「单 worker、串行、优雅关闭」三件事。

**操作步骤**：

1. 读 [internal/k8s/task_queue.go:90-102](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go#L90-L102) 的 `worker`，确认它是一个 `for` 死循环，只有 `quit==true` 才返回。
2. 读 [internal/k8s/task_queue.go:46-49](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go#L46-L49) 的 `Run`，结合 `wait.Until` 的语义，推出「`period=1s` 在稳态下其实不生效，全局只有一个 worker」。
3. 读 [internal/k8s/controller.go:819-826](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L819-L826) 的 `Stop`，追踪 `cancel()` → `<-ctx.Done()` → `Run` 返回 与 `Shutdown()` → `<-workerDone` 两条退出路径。

**需要观察的现象 / 预期结果**：

- 你应当能解释：收到 SIGTERM 时，`main` 触发 `Stop`，先 `cancel()` 让 controller 的 `Run` 主循环（`<-lbc.ctx.Done()`）退出，再 `syncQueue.Shutdown()` 关队列并**等待**当前正在执行的那一个 `sync` 跑完（`Done` 之后 worker 才退出）。这正是「优雅关闭」的保证——不会在一个 sync/reload 中途被掐断。
- 「待本地验证」：若你想在本地观察单 worker 行为，可在 `worker` 的 `tq.sync(...)` 前后临时加一行 `nl.Debugf` 日志打印 goroutine id 与时间戳，运行任一带 Informer 的测试（如 `internal/k8s/controller_test.go`），应只看到**同一个** goroutine 串行打印。注意这是「示例修改」，提交前需还原。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `Run(time.Second, ...)` 的 `time.Second` 改成 `time.Minute`，控制器的稳态行为会变吗？

**参考答案**：不会。因为 `worker()` 在正常运行时是个永不返回的死循环，`wait.Until` 第一次调用它后就一直阻塞在「等它返回」，`period` 根本用不上。该参数只在 worker 因 shutdown 返回后的极短退出窗口里才有意义，而那时 `stopCh` 已关闭，`wait.Until` 直接 return。

**练习 2**：为什么 `sync` 之后才调 `queue.Done(t)`，而不是 `sync` 之前？顺序反了会怎样？

**参考答案**：`Done` 表示「我处理完了」。若在 `sync` 之前 `Done`，那么 `sync` 执行期间该 item 已被标记为不在处理中；此时若同 key 又来事件，workqueue 会把它当作全新 item 立即入队，可能造成两个 worker（虽然 NIC 只有一个，但语义上）并发处理同一资源，破坏串行保证。先 `sync` 后 `Done` 才正确实现「处理中」标记。

### 4.4 失败重试与退避

#### 4.4.1 概念说明

控制器处理事件难免失败（Lister 暂时取不到对象、APIServer 抖动等）。失败后该怎么办？业界有两类常见做法：

1. **立即重入队**：失败就马上把 task 放回队列，靠 workqueue 的去重避免风暴。
2. **速率限制 + 指数退避**：用 `workqueue.RateLimitingInterface`，失败后 `AddRateLimited`，按指数增长的延迟重试（如 5ms、10ms、20ms…封顶），避免对持续报错的资源疯狂重试。

**本模块最重要的澄清**：NIC 的 `taskQueue` **没有实现指数退避**。它用的是最基础的 `workqueue.NewNamed(...)`（即 `*Type`），不是 `RateLimitingInterface`。失败路径只有 `Requeue`（立即重入队）和 `RequeueAfter`（固定延迟）。这一点与许多 controller-runtime 教程里的默认行为**不同**，初学者极易想当然地以为「workqueue 自带指数退避」，但读源码就会发现本项目并非如此。

#### 4.4.2 核心流程

两条重试路径对比：

```
Requeue(task, err)        →  queue.Add(task)            立即重入队（依赖去重防风暴）
RequeueAfter(t, err, d)   →  go func(){ sleep(d); Add }  固定延迟后入队
```

为什么 `Requeue` 立即重入队不会形成「失败→重试→失败」的死循环风暴？因为有 `workqueue.Type` 的去重兜底：一个持续失败的 task，连续 `Add` 多次在队列里也只占一份，worker 取出来重试的频率受限于「单 worker + 每次 sync 的实际耗时」——等于被 sync 的处理速度天然节流。

数学上，设单次 sync 耗时 \(t_s\)，则同一个失败 task 的最大重试频率约为

\[
f_{\max} \approx \frac{1}{t_s}
\]

而非无限大。这就是 NIC 选择「立即重入队 + 去重」而非「指数退避」仍能保持稳定的根本原因——但它**没有**随失败次数递增的退避，请牢记。

#### 4.4.3 源码精读

重看 `Requeue`（[internal/k8s/task_queue.go:69-73](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go#L69-L73)）：注意它**只调 `queue.Add`**，没有任何延迟、没有任何「失败次数计数」、没有任何 `Forget`/`AddRateLimited`——那些是 `RateLimitingInterface` 的 API，`*Type` 上根本没有。

`RequeueAfter`（[internal/k8s/task_queue.go:81-88](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go#L81-L88)）实现了「固定延迟」：

```go
func (tq *taskQueue) RequeueAfter(t task, err error, after time.Duration) {
    nl.Errorf(tq.logger, "Requeuing %v after %s, err %v", t.Key, after.String(), err)
    go func(t task, after time.Duration) {
        time.Sleep(after)
        tq.queue.Add(t)
    }(t, after)
}
```

它启动一个 goroutine，`sleep(after)` 后再 `Add`。注意这**不是**指数退避——`after` 是调用方传入的**固定**值，且不随重试次数增长。

**一个必须如实交代的发现**：`RequeueAfter` 在整个 `internal/` 目录下**从未被调用**（本讲编写时检索确认）。也就是说，它目前是「定义了但未使用」的 API。NIC 实际生效的失败重试路径**只有** `Requeue`（立即重入队）。这个事实提醒我们：读源码不能只看接口提供了什么，还要看实际调用了什么。

对照表，把「以为有」和「真有」分清楚：

| 能力 | 是否存在于 NIC taskQueue | 说明 |
| --- | --- | --- |
| 立即重入队 | ✅ 有（`Requeue`，被广泛使用） | 失败即 `queue.Add` |
| 去重 | ✅ 有（`workqueue.Type` 自带） | 同 key 只留一份 |
| 至少一次投递 | ✅ 有（`workqueue.Type` 自带） | 处理中再入队，Done 后重投 |
| 固定延迟重试 | ⚠️ 有定义（`RequeueAfter`）但**未被调用** | 仅作预留 |
| 指数退避 | ❌ **没有** | 未使用 `RateLimitingInterface` |
| 最大重试次数限制 | ❌ 没有 | 会一直重试到成功 |

#### 4.4.4 代码实践

**实践目标**：用检索证据确认「无指数退避、`RequeueAfter` 未被使用」这两个容易被误解的事实。

**操作步骤**：

1. 在 `internal/` 全目录搜索 `RequeueAfter(` 的调用点（不含定义行）。预期：**只有定义，没有调用**。
2. 搜索 `AddRateLimited`、`NewRateLimitingQueue`、`Forget` 等 `RateLimitingInterface` 的特征 API。预期：本项目里找不到（NIC 不用速率限制队列）。
3. 统计 `lbc.syncQueue.Requeue(task, err)` 的调用点数量与分布，确认它们都集中在「Lister 取对象出错」这类瞬时错误。

**需要观察的现象 / 预期结果**：

- 第 1 步应只命中 [internal/k8s/task_queue.go:82](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go#L82) 的函数定义本身，没有外部调用。
- 第 2 步应为空，印证「无指数退避」。
- 第 3 步约 18 处，全部是瞬时错误的重试。

> 这三项都是「源码阅读型实践」，不涉及运行命令；结论以检索结果为准。

#### 4.4.5 小练习与答案

**练习 1**：假设某个 Secret 在 Lister 里始终取不到（某种持续错误），`syncSecret` 每次都 `Requeue`。这会拖垮控制器吗？为什么？

**参考答案**：不会拖垮到「无限快重试」。因为单 worker 串行执行，每次 `sync` 都要花时间（哪怕失败），同一个 task 的重试频率上限约为 \(1/t_s\)。加上 workqueue 去重，它在队列里只占一份。代价是：这个失败的 task 会持续占用 worker 的处理轮次，**间接**拖慢其他资源的处理（它一直插队重试），但不会形成指数级风暴。值得注意的是 NIC 也没有「最大重试次数」，理论上会无限重试到该资源恢复或被删除。

**练习 2**：如果产品需求要求「连续失败的资源要逐渐降低重试频率」，在不改架构的前提下，最小改动是什么？

**参考答案**：把对应失败路径从 `lbc.syncQueue.Requeue(task, err)` 改为 `lbc.syncQueue.RequeueAfter(task, err, backoff)`，其中 `backoff` 由调用方按失败次数计算（例如线性或指数增长）。`RequeueAfter` 的机制已经就绪（goroutine + sleep），只是目前没有调用者。若要更强的保证（去重与退避协同），更彻底的做法是把 `taskQueue` 底层换成 `workqueue.RateLimitingInterface` 并用 `AddRateLimited`/`Forget`——但那属于较大改动，超出「最小改动」。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「**一次 Ingress 更新事件的完整旅途**」追踪任务。请用一段文字（或带编号的步骤）回答，每一步都要标出对应的源码位置（文件 + 行号 + 永久链接）。

**场景**：用户 `kubectl apply` 修改了 `default/cafe-ingress` 的一个注解。

**请追踪并回答**：

1. **入队前**：Informer 的 `UpdateFunc` 在哪里？它如何决定要不要入队？（提示：[handlers.go:45-52](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/handlers.go#L45-L52) 的 `hasChanges`）
2. **第一次入队**：`AddSyncQueue` → `Enqueue` → `keyFunc` + `newTask` + `queue.Add` 分别发生在哪几行？产出的 `task` 长什么样（`Kind` 和 `Key` 的值）？
3. **出队与分发**：worker 在哪里 `Get`？`sync` 在哪里按 `task.Kind==ingress` 分发到 `syncIngress`？（提示：[task_queue.go:90-102](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go#L90-L102) 与 [controller.go:1198-1202](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1198-L1202)）
4. **若 `syncIngress` 中 Lister 取对象出错**：会走到哪一行？是 `Requeue` 还是 `RequeueAfter`？这次重入队是第几次入队？（提示：[controller.go:1939-1943](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1939-L1943)）
5. **收尾**：处理完后 `queue.Done(t)` 在哪一行？如果处理期间又来一次同 key 事件，会发生什么？

**预期结果（参考要点）**：

- 第 2 步：`task{Kind: ingress, Key: "default/cafe-ingress"}`。
- 第 4 步：走 [controller.go:1941](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1941) 的 `Requeue`，**立即**重入队（`queue.Add`），这是该 task 的**第二次**入队（第一次来自 `Enqueue`）。无延迟、无退避。
- 第 5 步：`Done` 在 [task_queue.go:100](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/task_queue.go#L100)；若期间同 key 再入队，workqueue 会因「处理中标记为 dirty」而在 `Done` 后重新投递，保证至少处理一次最新值。

## 6. 本讲小结

- **数据结构极简**：`taskQueue` 投递的是轻量 `task{Kind, Key}`，不放资源对象本身；`key`（`namespace/name`）由 `keyFunc` 预算，`kind` 由 `newTask` 的 `switch` 归类，穷举了 NIC 监听的全部资源类型。
- **薄封装**：`taskQueue` 是 client-go `workqueue.Type` 的极薄封装；去重、至少一次投递、并发安全这三条正确性保证全部来自 workqueue，NIC 只提供 `Enqueue`/`Requeue`/`RequeueAfter` 三个面向业务的入口。
- **单 worker 串行**：全局只有一条 worker goroutine（`Run` 的 `period` 在稳态下不生效），所有资源的 `sync` 顺序执行；这也催生了「队列堆积时 `DisableReloads` 批处理、排空后一次性 reload」的配套优化，`Len()` 是批处理的触发条件。
- **优雅关闭**：`Shutdown` 先 `queue.ShutDown()` 让 worker 收到 `quit`，再 `<-workerDone` 等当前 `sync` 跑完，保证不在 reload 中途被掐断。
- **失败重试=立即重入队**：实际生效的失败路径只有 `Requeue`（`queue.Add` 立即重投）；`RequeueAfter` 虽有定义但**全项目未被调用**。
- **无指数退避**：NIC 用的是最基础的 `workqueue.NewNamed`（`*Type`），**不是** `RateLimitingInterface`，没有指数退避、没有最大重试次数；重试频率靠「单 worker + 去重」天然节流。这是本讲最需要破除的误区。

## 7. 下一步学习建议

本讲把「事件如何变成可消费的 task、如何被串行消费」讲透了，但还有两块留白，正好是后续讲义的主题：

- **入队前的变更检测**：`UpdateFunc` 里的 `hasChanges` 到底比较了什么？为什么有的更新会被过滤掉？请看 **u3-l4 事件处理器：从资源变更到入队**，它系统讲解 `createXHandlers` 的统一模式、墓碑对象处理与变更检测。
- **出队后的具体调谐**：`sync` 的 `switch` 把每种 `kind` 分发到 `syncIngress`/`syncVirtualServer`/`syncConfigMap` 等，这些 `syncX` 内部做了什么？请看 **u3-l5 sync 调度器与各资源 sync 函数**，它展开每个 `syncX` 的「取资源 → 更新内存模型 → 处理变更」三段式。

如果你还想横向对照，可以去读 client-go 官方的 `workqueue` 与 `controller-runtime` 的 `Controller` 实现，体会「立即重入队」与「指数退避」两种设计取舍的差异——带着本讲对 NIC 实际行为的认知去对比，会更深刻。
