# LoadBalancerController 生命周期与 Run/Stop

## 1. 本讲目标

本讲是第三单元「控制器核心」的第一篇。前两个单元我们已经建立了两件事的共识：

- 本项目使用**原始 client-go**（`SharedInformerFactory` + `work queue`），而不是 controller-runtime（见 u1-l1、u1-l3）。
- 控制器需要监听的资源模型已经清楚：标准 Ingress 与自定义资源 VirtualServer / VirtualServerRoute / TransportServer / Policy / GlobalConfiguration（见 u2-l1）。

那么，把这些资源「监听起来、持续调谐、写回状态」的**那个长期运行的协调者**到底是什么？它从被创建到退出，经历了哪些阶段？本讲就回答这个问题。

学完本讲你应当能够：

1. 说出 `LoadBalancerController` 结构体的关键字段及其职责分组。
2. 描述 `NewLoadBalancerController` 的构造过程，以及它在构造阶段就「连好线」的组件（队列、informer、leader 选举、配置内存模型）。
3. 按顺序列出 `Run()` 的启动阶段：派生 context → 启动各类 controller → 启动 informer → 等待缓存同步 → 预热 Secret → 启动任务队列 → 阻塞。
4. 解释 `isNginxReady` 这个启动优化标志的作用，以及它如何把「就绪（能转发流量）」与「状态写回」解耦。
5. 说明 `Stop()` 与 SIGTERM 优雅关闭的关系。

> 本讲只讲**生命周期骨架**。informer 内部如何 list-watch（u3-l2）、队列如何工作（u3-l3）、事件处理器如何入队（u3-l4）、`sync()` 如何按 Kind 分发（u3-l5）都留待后续讲义展开。本讲关注的是「它们被谁、在什么时刻、按什么顺序启动」。

---

## 2. 前置知识

### 2.1 控制器范式（controller pattern）回顾

在 u1-l1 我们讲过：Kubernetes 控制器遵循「期望状态 vs 实际状态」的 **reconcile 范式**。具体到本项目：

- **期望状态**：用户通过 `kubectl apply` 写进集群的 Ingress / VirtualServer 等资源。
- **实际状态**：NGINX 进程当前加载的配置，以及转发流量用的 upstream 端点。
- **reconcile 循环**：控制器监听资源变化，把变化翻译成 NGINX 配置，reload NGINX，再把 `.status`（例如 Ingress 的外部 IP、VirtualServer 的 state）写回集群。

`LoadBalancerController` 就是这个循环的**宿主**。

### 2.2 三个你需要的 client-go 概念

这三个概念本讲会用到名字但不深入（深入在 u3-l2、u3-l3）：

- **Informer**：对某类资源做 list-watch、在本地维护一份缓存的组件。`SharedInformerFactory` 是它的工厂，可共享底层连接。
- **workqueue**：client-go 提供的带去重、指数退避的任务队列。本项目的 `taskQueue` 封装了它。
- **cache.WaitForCacheSync**：阻塞调用，直到所有 informer 的首次全量 list 完成、本地缓存与 apiserver 一致。这是控制器进入「正常工作」前的关键闸门。

### 2.3 context 与取消

Go 的 `context.Context` 是一种「传递取消信号」的机制。当父 context 被 `cancel()`，所有由它派生的子 context 都会收到 `<-ctx.Done()` 信号。本讲的 `Run()` / `Stop()` 正是用一个可取消的 context 把所有后台 goroutine 串起来的。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `internal/k8s/controller.go` | 控制器的全部实现：结构体、构造、`Run`、`Stop`、`sync` | 结构体字段、构造装配、`Run` 启动序列、`Stop` 关闭、`isNginxReady` |
| `internal/k8s/leader.go` | leader 选举的封装与回调 | `newLeaderElector`（LeaseLock）、`createLeaderHandler`、`addLeaderHandler` |
| `cmd/nginx-ingress/main.go` | 程序入口（见 u1-l4） | `lbc.Run()` 的调用点、`handleTermination` 中的 SIGTERM 处理 |

本讲引用的核心代码点都在这三个文件里。永久链接的 base 是当前 HEAD `b678c44eb`。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **控制器结构体** —— `LoadBalancerController` 持有哪些状态。
2. **NewLoadBalancerController** —— 装配过程。
3. **Run 启动序列** —— 按顺序发生了什么。
4. **Stop 与优雅关闭** —— 如何安全退出。

---

### 4.1 控制器结构体

#### 4.1.1 概念说明

一个 Kubernetes 控制器本质上是一个**有状态的长期运行 goroutine**。它的「状态」包含三类东西：

- **外部世界的手柄（handle）**：连到 apiserver 的各类客户端、本地缓存。
- **内部世界**：对集群资源的一份**内存模型**（哪些 VirtualServer 抢了同一个 host、哪些 Secret 被引用了），以及把内存模型翻译成配置的 `configurator`。
- **运行期开关 / 计数器**：例如「NGINX 是否就绪」「是否正在批量处理」「是否正在关闭」。

`LoadBalancerController` 就是这个结构体。理解它的字段分组，等于理解了整个控制器要管理哪些事情。

#### 4.1.2 核心流程

结构体本身没有「流程」，但我们可以在心里把它的字段分成五组，这正是后续讲义的分工：

```
LoadBalancerController
├─ [连接与缓存]  client / confClient / dynClient + namespacedInformers + 各 lister
├─ [工作入口]    syncQueue (任务队列) → 调用 sync()
├─ [内存与配置]  configuration (内存模型) + configurator (配置生成) + secretStore
├─ [状态与选举]  statusUpdater + leaderElector + reportIngressStatus
└─ [运行期标志]  isNginxReady / batchSyncEnabled / ShuttingDown / pendingStatus*
```

#### 4.1.3 源码精读

结构体定义开头有一个很重要的注释，它预告了本讲后半段的「启动优化」主题：在首次排空队列期间（`!isNginxReady`），每个资源的状态写回会被**推迟**进 pending 切片，而不是当场串行调用 apiserver。

[internal/k8s/controller.go:122-129](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L122-L129) —— 这段注释解释了为什么要推迟状态写回：在大规模集群，串行写回每个资源的状态是 O(N) 次 API 调用，会阻塞就绪好几分钟。

接下来是结构体本体。它有几十个字段，我们挑出与「生命周期」最相关的几组：

[internal/k8s/controller.go:164-245](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L164-L245) —— `LoadBalancerController` 结构体完整定义。几个对本讲至关重要的字段：

- `syncQueue *taskQueue`（L180）：唯一的工作队列入口。
- `ctx context.Context` 与 `cancel context.CancelFunc`（L181、L183）：运行期取消信号的根，`Run()` 创建、`Stop()` 触发。
- `isNginxReady bool`（L212）：本讲的主角之一，启动优化标志。
- `leaderElector`、`isLeaderElectionEnabled`、`leaderElectionLockName`（L196-199）：leader 选举相关。
- `ShuttingDown bool`（L233）：优雅关闭标志。
- `pendingStatusIngresses` / `pendingStatusVSes` / …（L240-244）：被推迟的状态更新，启动完成后并行刷写。

> 注意：这些字段都是**非导出的（小写）**，除了 `Logger` 和 `ShuttingDown`。这是 Go 的可见性围栏：控制器的内部状态对外不可见，只能通过方法操作。这一点和 u1-l3 讲的「`internal/` 是可见性围栏」是一致的设计哲学。

#### 4.1.4 代码实践

**实践目标**：建立结构体字段的「职责地图」。

**操作步骤**：

1. 打开 `internal/k8s/controller.go`，定位到 164 行的结构体。
2. 把字段按本节给的五组分类，标注每个字段属于哪一组（连接与缓存 / 工作入口 / 内存与配置 / 状态与选举 / 运行期标志）。

**需要观察的现象**：你会发现同一组字段在 `NewLoadBalancerController` 里往往是「连续一起赋值」的——这反映了构造过程也是按职责分组的。

**预期结果**：你能用一句话回答「`isNginxReady` 属于哪一组、它影响哪些行为」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `syncQueue` 的类型是 `*taskQueue`（指针）而不是值？

**参考答案**：因为 `taskQueue` 内部封装了 client-go 的 workqueue、worker goroutine 等可变状态，且需要被多个 goroutine 共享（生产者入队、worker 出队）。用指针保证所有持有者操作的是同一个队列实例。

**练习 2**：`pendingStatusVSes` 等切片存在的意义是什么？如果删掉它们、改成启动时当场写回状态，会出什么问题？

**参考答案**：它们用于在首次排空队列时推迟状态写回，避免 O(N) 次串行 API 调用阻塞就绪。若改成当场写回，在大规模集群下控制器要等很久才能标记就绪、开始转发流量。

---

### 4.2 NewLoadBalancerController

#### 4.2.1 概念说明

`NewLoadBalancerController` 是一个**构造函数**（不是 K8s 意义上的 controller-runtime constructor，而是普通的 Go 构造函数）。它的职责是：

1. 接收一大堆外部依赖与配置（客户端、flag 值、校验器等）。
2. 创建 `LoadBalancerController` 实例，把这些依赖填进去。
3. 在**构造阶段**就把若干组件「连线」好：建任务队列（并注入 `sync` 处理函数）、建各命名空间的 informer、注册 leader 选举 handler、建内存模型、建 secretStore 等。

注意一个关键设计：**构造阶段不做任何「运行」**。它只创建对象、注册 handler、建 informer（informer 被创建但还没 `Start`）。真正的「跑起来」全部留到 `Run()`。这种「构造与运行分离」是 K8s 控制器的常见模式，便于测试（可以构造后注入 fake、单独测某个方法）。

#### 4.2.2 核心流程

构造过程（按代码顺序）：

```
NewLoadBalancerController(input)
  1. 组装 specialSecrets（Plus 下含 license/clientAuth/trustedCert）
  2. 用字面量创建 lbc，填充约 30 个字段
  3. lbc.syncQueue = newTaskQueue(lbc.Logger, lbc.sync)   ← 关键：注入 sync 处理函数
  4. （可选）SPIFFE cert fetcher
  5. （可选，动态命名空间）addNamespaceHandler
  6. （可选）certManagerController / externalDNSController 子控制器
  7. 为每个目标命名空间 newNamespacedInformer(ns)        ← 建 informer（尚未 Start）
  8. （可选）GlobalConfiguration / ConfigMap / MGMTConfigMap / IngressLink handler
  9. （可选）addLeaderHandler(createLeaderHandler(lbc))   ← 注册 leader 回调、建 leaderElector
 10. statusUpdater 组装
 11. configuration = NewConfiguration(...)                ← 内存模型
 12. appProtect / dosConfiguration、secretStore
 13. （可选）telemetry collector
 14. return lbc
```

> 步骤 3 是理解整个控制器的钥匙：`newTaskQueue` 的第二个参数 `lbc.sync` 就是队列里每个任务最终被处理时调用的函数。这意味着「队列 → sync → 按 Kind 分发」这条链在**构造时**就接好了（`sync` 的分发细节见 u3-l3、u3-l5）。

#### 4.2.3 源码精读

**输入结构体**：构造函数通过一个大的 input 结构体接收所有依赖，避免参数列表爆炸。

[internal/k8s/controller.go:250-304](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L250-L304) —— `NewLoadBalancerControllerInput`。注意它汇聚了来自 `main.go` 的几乎所有 flag 值（`IngressClass`、`IsLeaderElectionEnabled`、`LeaderElectionLockName`、`ConfigMaps`、`MGMTConfigMap`、`AreCustomResourcesEnabled` 等），是 flag 与控制器之间的**契约面**。

**构造函数本体**：

[internal/k8s/controller.go:307-351](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L307-L351) —— 函数入口与结构体字面量初始化。先组装 `specialSecrets`（Plus 模式下还要拼出 license / clientAuth / trustedCert 的 `namespace/name`，见 L312-316），再用字面量把 input 里的值搬进 `lbc`。

[internal/k8s/controller.go:353-368](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L353-L368) —— **关键两行**：`lbc.syncQueue = newTaskQueue(lbc.Logger, lbc.sync)` 把 `sync` 方法绑定为队列处理器；随后按 `WatchNamespaceLabel` 是否非空决定是否注册动态命名空间 handler。

[internal/k8s/controller.go:388-397](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L388-L397) —— 为每个目标命名空间创建一个 `namespacedInformer`。注意这里只是**创建**，informer 的 `.Start()` 在 `Run()` 里才调用。`newNamespacedInformer` 内部如何组织 core / CRD / secret / dynamic 各类 informer，见 u3-l2。

[internal/k8s/controller.go:434-447](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L434-L447) —— **leader 选举接线**：当 `IsLeaderElectionEnabled` 时，调用 `addLeaderHandler(createLeaderHandler(lbc))`。这一步会创建 `lbc.leaderElector`（它也是「构造好但不运行」，真正 `.Run()` 在 `Run()` 里）。下方紧跟着组装 `statusUpdater`——只有 leader 才应该写回 status（u3-l7 会详述）。

[internal/k8s/controller.go:449-469](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L449-L469) —— 构造**内存模型** `configuration = NewConfiguration(...)`，传入一堆校验器与开关（`HasCorrectIngressClass`、各 Validator、`IsTLSPassthroughEnabled` 等）。这就是 u3-l6 要讲的「Configuration」。随后建 `appProtect`/`dos` 配置与 `secretStore`。

[internal/k8s/controller.go:471-518](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L471-L518) —— （可选）**遥测**：当 `EnableTelemetryReporting` 时，建 exporter（默认端点 `oss.edge.df.f5.com:443`）与 collector，并建 `telemetryChan` 信号通道。注意这里只是「建好」，collector 的真正 `Start` 要等 leader 上任（见 4.3）。

#### 4.2.4 代码实践

**实践目标**：验证「构造与运行分离」这一设计判断。

**操作步骤**：

1. 在 `NewLoadBalancerController`（307-518）里搜索所有 `go ` 开头的语句（启动 goroutine）。
2. 你会发现：**构造函数里没有任何 `go xxx.Run()`**。所有 `.Run()`、`.Start()` 都被推迟。
3. 对照 `Run()`（722-817），统计有多少个 `go ` 启动点。

**需要观察的现象**：构造函数只做「new + 赋值 + 注册 handler」，`Run()` 才做「start」。

**预期结果**：你确认控制器可以在不运行的情况下被构造出来——这正是单元测试能 mock 客户端、单独测 `syncXxx` 的前提。

#### 4.2.5 小练习与答案

**练习 1**：为什么把 leader handler 的注册（`addLeaderHandler`）放在构造函数，而把 `leaderElector.Run()` 放在 `Run()`？

**参考答案**：构造期负责「接线」（建好 `leaderElector` 对象、注册 `OnStartedLeading` 回调），运行期负责「驱动」（在后台 goroutine 里跑选举循环）。分离两者使对象在启动前就是完备、可测试的。

**练习 2**：`lbc.syncQueue = newTaskQueue(lbc.Logger, lbc.sync)` 这行如果删掉 `lbc.sync` 参数，会破坏什么？

**参考答案**：`sync` 是队列消费每个任务时调用的处理函数。没有它，队列就不知道拿到一个任务后该做什么，整个 reconcile 循环就断了。

---

### 4.3 Run 启动序列

#### 4.3.1 概念说明

`Run()` 是控制器从「对象」变成「运行中进程」的转折点。它由 `main.go` 在 `lbc.Run()` 处调用（见 u1-l4 与本讲 4.1.3 引用的 main.go），并且**阻塞调用方**直到 context 被取消。

`Run()` 要协调一大堆后台 goroutine，并保证它们的**启动顺序**合理。最关键的顺序约束是：

> **必须先 `cache.WaitForCacheSync`（本地缓存与 apiserver 一致），然后才启动 `syncQueue`。**

为什么？因为如果在缓存同步前就让队列开始消费，informer 的本地缓存可能还没有全部资源，`sync()` 取到的就是残缺视图，会生成错误的配置。先同步、再消费，才能保证队列里第一批任务处理时，`syncXxx` 看到的是完整一致的世界。

#### 4.3.2 核心流程

`Run()` 的执行步骤时序（这是本讲的核心，也是综合实践要画的东西）：

```
Run()
 │
 ├─ 1. ctx, cancel = context.WithCancel(Background)      ← 创建可取消的根 context
 │
 ├─ 2. 启动「不依赖缓存」的后台组件（各 go xxx.Run(ctx.Done())）
 │      ├─ namespaceWatcherController（动态命名空间，若启用）
 │      ├─ spiffeCertFetcher（等待初始 trust bundle，30s 超时）
 │      ├─ certManagerController / externalDNSController（子控制器）
 │      ├─ leaderElector.Run(ctx)                        ← 开始竞争 leader
 │      └─ telemetryCollector（等 telemetryChan 信号后 Start）
 │
 ├─ 3. for _, nif := range namespacedInformers { nif.start() }   ← 启动所有 informer 的 list-watch
 │
 ├─ 4. 启动「单实例」controller：configMap / mgmtConfigMap / globalConfiguration / ingressLink
 │
 ├─ 5. 收集所有 cacheSyncs（lbc.cacheSyncs + 每个 nif.cacheSyncs）
 │
 ├─ 6. cache.WaitForCacheSync(ctx.Done(), totalCacheSyncs...)   ← 关键闸门：等所有缓存首次同步
 │      （失败则直接 return）
 │
 ├─ 7. preSyncSecrets()                                  ← 把所有 TLS Secret 灌进 secretStore
 │
 ├─ 8. go syncQueue.Run(time.Second, ctx.Done())         ← 启动任务队列，开始消费
 │
 └─ 9. <-ctx.Done()                                      ← 阻塞，直到 Stop() 调用 cancel()
```

注意：步骤 2 里 leader 选举与 telemetry 是**并行的**——leader 选举循环先跑起来竞争，但不阻塞后续缓存同步；只有当 leader 真正上任（`OnStartedLeading`）后才会触发 telemetry 的 `Start`（通过关闭 `telemetryChan`）。

#### 4.3.3 源码精读

[internal/k8s/controller.go:722-723](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L722-L723) —— `Run()` 入口：派生可取消的根 context。这个 `cancel` 正是 `Stop()` 要调用的。

[internal/k8s/controller.go:765-778](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L765-L778) —— leader 选举与遥测的后台启动。`go lbc.leaderElector.Run(lbc.ctx)` 开始竞争锁；遥测则 `select` 等待 `telemetryChan`（leader 上任时被 `close`，见 leader.go L66-68）或 `ctx.Done()`。

[internal/k8s/controller.go:780-797](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L780-L797) —— 启动所有 namespaced informer 与各单实例 controller。`nif.start()` 内部把若干个 `SharedInformerFactory.Start()` 放进 goroutine（见 [internal/k8s/controller.go:828-842](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L828-L842)）。

[internal/k8s/controller.go:799-809](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L799-L809) —— **关键闸门**：收集所有 `cacheSyncs`，调用 `cache.WaitForCacheSync`。如果任何一个 informer 同步失败（例如连不上 apiserver），返回 false，`Run()` 直接 `return`，控制器不会进入工作状态。

[internal/k8s/controller.go:811-816](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L811-L816) —— 缓存同步成功后：`preSyncSecrets()` 把 TLS Secret 灌入 `secretStore`，然后 `go lbc.syncQueue.Run(time.Second, lbc.ctx.Done())` 真正开始消费任务。最后 `<-lbc.ctx.Done()` 把 `Run()` 阻塞在这里，直到 `Stop()` 取消 context。

**`preSyncSecrets` 做了什么**：

[internal/k8s/controller.go:1160-1180](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1160-L1180) —— 遍历每个命名空间的 Secret lister，把支持类型的 Secret 提前加入 `secretStore`。这样队列里第一批资源开始处理时，它们引用的证书已经在 store 里了，不会因为「Secret 还没进缓存」而误判。

#### 4.3.4 `isNginxReady`：启动优化标志详解

这是本讲要求掌握的「启动优化标志」，它不在 `Run()` 里，而在 `sync()` 里被设置。理解它需要看 `sync()` 的末尾：

[internal/k8s/controller.go:1261-1303](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1261-L1303) —— **启动序列收尾**：当 `!isNginxReady && syncQueue.Len() == 0`（即首次排空队列完成）时，执行一次性昂贵操作：

- **Step 1**：`CompleteStartup()` 做一次确定性的 `rebuildHosts()`，计算 host→资源映射、检测冲突与孤儿资源（避免在排空过程中每来一个资源就重建一次，那会是 O(N²)）。
- **Step 2**：`EnableReloads()` + `updateAllConfigs()`，从内存模型一次性生成全部配置并做**单次** reload。
- **Step 3**：[internal/k8s/controller.go:1295-1296](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1295-L1296) `lbc.isNginxReady = true`。注意它是在「reload 完成」之后、状态刷写**之前**设置的——pod 此刻已经能转发流量。
- **Step 4**：`flushPendingStatusesAsync()` 在后台并行刷写被推迟的状态。

这里体现了一个核心权衡：**把「就绪」与「状态写回」解耦**。传统做法是写完所有 status 才标记就绪，但在大规模集群里 leader 写完 N 个资源的状态要好几分钟。本项目选择 reload 完就先就绪（流量不等元数据），状态写回收回后台并行做。

我们用一个简单的复杂度对比说明为什么要推迟、要批量化：

\[
T_{\text{朴素}} = \underbrace{\sum_{i=1}^{N} t_{\text{rebuild}}(i)}_{O(N^2)\ \text{host 重建}} + \underbrace{\sum_{i=1}^{N} t_{\text{status}}(i)}_{O(N)\ \text{串行 API 调用}}
\]

\[
T_{\text{优化}} = \underbrace{t_{\text{rebuild}}(1)}_{\text{一次性重建}} + \underbrace{\lceil N / W \rceil \cdot t_{\text{status}}}_{O(N/W)\ \text{并行写回},\ W=10}
\]

其中 \(N\) 是资源数量、\(W = 10\) 是并行 worker 数（`statusFlushWorkers`）。前者随资源数二次增长，后者近似线性。

**刷写状态的并发模型**：用一个带缓冲 channel 当信号量限制并发：

[internal/k8s/controller.go:2022-2029](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L2022-L2029) —— 并发常量定义：`statusFlushWorkers = 10`、`leaderPollInterval = 500ms`、`leaderPollDeadline = 60s`。

[internal/k8s/controller.go:2044-2059](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L2044-L2059) —— `flushPendingStatusesAsync`：先把各 pending 切片「快照」并置 nil（让主 goroutine 能继续接收新状态），再 `go lbc.runStatusFlush(...)`。

[internal/k8s/controller.go:2067-2099](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L2067-L2099) —— `runStatusFlush`：若启用了 leader 选举，先 `waitForLeadership()`；否则直接用 `sem := make(chan struct{}, statusFlushWorkers)` 限并发地刷写 Ingress / VS / VSR / TS / Policy 状态。

> 这段优化是较新引入的（注意 L122-129 的注释）。它说明本项目在大规模场景下对「启动延迟」做过认真的工程打磨——这也是为什么 `isNginxReady` 这个看似普通的 bool 值得单独理解。

#### 4.3.5 代码实践（与综合实践相关）

**实践目标**：把 `Run()` 的步骤时序落到一张图上。

**操作步骤**：

1. 打开 `internal/k8s/controller.go` 的 `Run()`（722-817）。
2. 画一条纵向时间轴，把每个 `go xxx`、`WaitForCacheSync`、`preSyncSecrets`、`syncQueue.Run`、`<-ctx.Done()` 标在轴上。
3. 在每个节点旁标注它对应的**函数名**和**行号**。

**需要观察的现象**：`syncQueue.Run` 一定在 `WaitForCacheSync` 之后；`<-ctx.Done()` 是最后一个动作。

**预期结果**：得到一张与 4.3.2 文字流程一致的时序图。

#### 4.3.6 小练习与答案

**练习 1**：如果 `cache.WaitForCacheSync` 返回 false（同步失败），`Run()` 会怎样？为什么这样设计？

**参考答案**：`Run()` 直接 `return`，不启动队列、不进入工作状态（见 L807-809）。设计上是 fail-fast：缓存不同步意味着控制器看不到完整资源，此时开始生成配置会出错，宁可退出让上层（Deployment）重启重试。

**练习 2**：`isNginxReady` 设为 true 的那一刻（L1295），哪些事情已经做完、哪些还没做？

**参考答案**：已经做完：内存模型 `CompleteStartup()`（含 host 重建与冲突检测）、配置生成与单次 reload。还没做：被推迟的状态写回（`flushPendingStatusesAsync` 是紧接着的 Step 4，且在后台并行）。也就是说，**能转发流量 ≠ 状态已写回**。

---

### 4.4 Stop 与优雅关闭

#### 4.4.1 概念说明

在 Kubernetes 里，Pod 被删除时会收到 `SIGTERM`，默认 30 秒后 `SIGKILL`。控制器必须在这段时间内**优雅关闭**：

- 停止接收新任务。
- 让正在处理的任务完成或放弃。
- 通知所有后台 goroutine 退出。
- 退出 NGINX 子进程。

`LoadBalancerController.Stop()` 负责前半段（停控制器自身），而与 SIGTERM 的衔接、停 NGINX 子进程，由 `main.go` 的 `handleTermination` 协调。

#### 4.4.2 核心流程

```
SIGTERM 到达
  └─ main.handleTermination 捕获信号
       ├─ lbc.ShuttingDown = true     ← 设置关闭标志（供别处检查，例如避免再发起 reload）
       ├─ lbc.Stop()                  ← 停控制器
       │     ├─ lbc.cancel()          ← 取消根 context → 所有 <-ctx.Done() 立即返回
       │     ├─ for nif { nif.stop() }← close 每个 informer 的 stopCh
       │     └─ lbc.syncQueue.Shutdown() ← 关闭队列，不再接收任务
       ├─ nginxManager.Quit()         ← 退出 NGINX 主进程
       ├─ 等待各子进程退出（nginx / app-protect / dos / iprepd）
       └─ listener.Stop()             ← 停指标监听
```

核心机制是**一个 cancel 串起所有后台 goroutine**：`Run()` 在 L723 创建的 `ctx`，其 `Done()` 通道被 `leaderElector.Run`、`syncQueue.Run`、各 controller 的 `Run(ctx.Done())` 共同监听。`Stop()` 只需 `lbc.cancel()` 一次，所有这些 goroutine 就会收到退出信号——这是 Go context 的「一呼百应」能力。

#### 4.4.3 源码精读

[internal/k8s/controller.go:820-826](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L820-L826) —— `Stop()` 本体，只有三步：`cancel()` 取消 context；遍历停止所有 namespaced informer；`syncQueue.Shutdown()` 关队列。

[internal/k8s/controller.go:844-846](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L844-L846) —— `nsi.stop()` 只是 `close(nsi.stopCh)`，各 `SharedInformerFactory.Start(nsi.stopCh)` 监听的就是这个 channel。

**main.go 的衔接**：

[cmd/nginx-ingress/main.go:359-361](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/main.go#L359-L361) —— `main()` 先 `go handleTermination(...)`（独立 goroutine 等信号），再 `lbc.Run()`（阻塞主 goroutine）。

[cmd/nginx-ingress/main.go:851-869](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/main.go#L851-L869) —— `handleTermination` 的 SIGTERM 分支：设 `ShuttingDown = true` → `lbc.Stop()` → `nginxManager.Quit()` → 等子进程退出 → `listener.Stop()`。

`ShuttingDown` 字段（[controller.go:233](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L233)）是导出的，供其他包（如 NGINX manager）在关闭期间抑制非必要的 reload。

#### 4.4.4 代码实践

**实践目标**：验证「一次 cancel 串起所有 goroutine」的设计。

**操作步骤**：

1. 在 `Run()`（722-817）里数出所有出现 `lbc.ctx.Done()` 或 `lbc.ctx` 的位置。
2. 确认它们全部依赖同一个 `ctx`（L723 创建）。
3. 在 `Stop()`（820-826）里确认 `cancel()` 就是这个 ctx 的取消函数。

**需要观察的现象**：`Stop()` 没有逐个通知每个 goroutine，只调一次 `cancel()`。

**预期结果**：你能解释「为什么 `Stop()` 这么短」——因为关闭的复杂度被 context 机制吸收了。

#### 4.4.5 小练习与答案

**练习 1**：如果 `Stop()` 里只调用 `lbc.syncQueue.Shutdown()` 而忘记 `lbc.cancel()`，会发生什么？

**参考答案**：队列会停，但 `leaderElector.Run`、各 controller 的 `Run(ctx.Done())` 仍然阻塞在 `<-ctx.Done()` 上（因为 context 没被取消），这些 goroutine 不会退出，造成 goroutine 泄漏；`Run()` 主 goroutine 也会一直阻塞在 `<-lbc.ctx.Done()`。所以 `cancel()` 是关闭的「总闸」。

**练习 2**：为什么关闭流程里要 `ShuttingDown = true`？谁能读到它？

**参考答案**：它是一个导出标志，供 NGINX manager 等组件在关闭期间抑制不必要的 reload（避免退出途中又触发一次重载）。把「正在关闭」的状态显式化，比让各组件各自猜测更可靠。

---

## 5. 综合实践

**任务**：为 `LoadBalancerController` 的完整生命周期绘制一张「时序图 + 职责标注」图，并回答三个追问。这张图要把本讲的四个最小模块全部串起来。

**操作步骤**：

1. **画出三条泳道**：`main.go` / `NewLoadBalancerController` / `Run+sync+Stop`。
2. 在 `NewLoadBalancerController` 泳道标出 4 个「构造期就接好的线」：① `syncQueue` 绑定 `sync`；② 各 `namespacedInformer`（未 Start）；③ `addLeaderHandler`（leaderElector 未 Run）；④ `configuration` 内存模型。
3. 在 `Run+sync` 泳道按 4.3.2 的时序画出 9 个步骤，重点标出 `WaitForCacheSync` 闸门与 `syncQueue.Run` 的先后关系。
4. 在 `sync()` 泳道标出首次排空队列时的 4 个 Step（CompleteStartup → updateAllConfigs+reload → `isNginxReady=true` → flushPendingStatusesAsync）。
5. 在 `Stop` 处画出 SIGTERM → `ShuttingDown=true` → `cancel()` → informer stop → `syncQueue.Shutdown()` 的链路。

**追问（请结合源码行号回答）**：

- (a) 如果缓存同步失败，控制器的任务队列会不会启动？为什么？（提示：L807-809）
- (b) `isNginxReady` 设为 true 之后，还有哪一步没做完？这步是同步还是异步？（提示：L1295、L1302、L2044）
- (c) 一个 follower 副本（非 leader）在启动时会尝试写回 status 吗？谁阻止了它？（提示：`runStatusFlush` + `waitForLeadership`，[leader.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/leader.go)）

**预期结果**：一张能自洽解释「控制器从构造到退出全过程」的图，三个追问都能用具体行号支撑。leader 选举的内部机制（LeaseLock、回调）将在 u3-l7 深入，本实践只需定位到「谁阻止了 follower 写状态」。

> 说明：本实践是「源码阅读型实践」，不要求真实集群。如果无法确认某处行为，请标注「待本地验证」。

---

## 6. 本讲小结

- `LoadBalancerController` 是 reconcile 循环的宿主，字段按「连接与缓存 / 工作入口 / 内存与配置 / 状态与选举 / 运行期标志」五组划分。
- `NewLoadBalancerController` 遵循**构造与运行分离**：构造期只 new + 注册 handler，真正启动全部留给 `Run()`；最关键的接线是 `syncQueue` 绑定 `sync` 处理函数。
- `Run()` 的顺序铁律是：**先 `cache.WaitForCacheSync`，后 `syncQueue.Run`**，中间穿插 `preSyncSecrets` 预热；它以 `<-ctx.Done()` 阻塞收尾。
- `isNginxReady` 是启动优化标志：首次排空队列后做一次性 host 重建与单次 reload，随后立刻置 true（就绪），状态写回收回后台并行（10 worker）刷写，把「就绪」与「状态写回」解耦。
- `Stop()` 极简——一次 `cancel()` 串起所有后台 goroutine，配合 `main.handleTermination` 处理 SIGTERM 并设置 `ShuttingDown`。
- leader 选举在构造期接线（`addLeaderHandler`）、运行期驱动（`leaderElector.Run`）；只有 leader 写回 status，follower 在 `runStatusFlush` 中被 `waitForLeadership` 拦截（细节见 u3-l7）。

---

## 7. 下一步学习建议

本讲建立了生命周期骨架，但每一根「线」都还有内部机制值得展开。建议按依赖顺序继续：

- **u3-l2 SharedInformerFactory 与命名空间级 Informer**：深入 `newNamespacedInformer` / `namespacedInformer`，理解 list-watch 与多类资源的组织方式（本讲只用到它们的 `.start()`）。
- **u3-l3 任务队列与 workqueue 模式**：深入 `taskQueue`，理解 4.2.3 里那行 `newTaskQueue(lbc.Logger, lbc.sync)` 的第二参数如何被消费、worker 循环与退避如何工作。
- **u3-l5 sync 调度器**：展开 4.3.4 里只引用了开头的 `sync()`，看它如何按 `task.Kind` 分发到 `syncIngress` / `syncVirtualServer` 等。
- **u3-l7 Leader 选举与 Status 回写**：把本讲 4.3.4、综合实践 (c) 里点到为止的 leader 选举与 `statusUpdater` 彻底讲清。

阅读建议：先重读 [controller.go:722-817](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L722-L817)（`Run`），再带着「这张时序图」进入 u3-l2，体会每个 `nif.start()` 之后 informer 内部发生了什么。
