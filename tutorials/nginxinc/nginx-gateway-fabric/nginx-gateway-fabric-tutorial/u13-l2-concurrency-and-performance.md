# 并发、性能与可靠性设计

## 1. 本讲目标

本讲从**架构视角**纵览 NGINX Gateway Fabric（NGF）控制面的并发、性能与可靠性设计。读完后你应当能够：

- 说清**事件双缓冲（double buffering）**如何把一批快速到来的资源变更压缩成一次 NGINX 配置更新，从而把 reload 次数从「每事件一次」降到「每处理窗口至多一次」。
- 说清**Leader 选举**如何让多副本控制面做到「热备 + 单写」，保证同一时刻只有一个副本改集群状态。
- 说清 **ngfsort 冲突排序**如何为 Gateway API 的冲突裁决提供**确定性**（deterministic），让「相同输入永远得到相同输出」。
- 说清**健康检查与就绪**为何以「首张图构建完成」而非「进程启动」作为就绪信号，避免配置未下发就接流量。

本讲是进阶单元（u3~u9）的「横向收束」：前面各讲分别讲了一条链路上的某一环，本讲把**横切这些链路的三大可靠性机制**（批处理、单写、确定性）和**一个对外契约**（就绪）拎出来统一讲，帮你建立系统级心智模型。

## 2. 前置知识

本讲默认你已学完：

- **u4-l2 事件循环与批处理（双缓冲）**：知道 `EventLoop`、`EventBatch`、`EventHandler`、首批事件等概念。
- **u3-l4 Leader Election 与 Runnables**：知道 `Leader` / `LeaderOrNonLeader` / `CallFunctionsAfterBecameLeader` 三个包装器、`CronJob` 周期任务。
- **u4-l3 / u4-l4**：知道 `eventHandlerImpl` 如何编排 `processor.Process` 构建图、再下发配置。

下面补充三个本讲会反复用到、但可能尚未明确点出的概念：

| 术语 | 通俗解释 |
| --- | --- |
| **reload** | 让 NGINX 重新加载配置的过程。耗时**至少 200ms**（取决于配置大小、证书数、CPU），且可能对数据面流量有副作用，因此是要被尽量减少的「昂贵操作」。 |
| **幂等（idempotent）** | 同一个输入重复处理，结果不变。事件批处理允许重复事件，因此下游 `EventHandler` 必须幂等——同 `Generation` 的重复 upsert 不触发重配。 |
| **确定性** | 给定相同的输入集合，无论以何种顺序、何种时机处理，输出都完全一致。冲突排序就是为了消除「顺序敏感」。 |

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [internal/framework/events/loop.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go) | **事件双缓冲**的核心：`EventLoop.Start` 用 `currentBatch`/`nextBatch` 两个切片做批处理。 |
| [internal/framework/runnables/runnables.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/runnables.go) | **Leader 选举语义**的三个 Runnable 包装器。 |
| [internal/framework/runnables/cronjob.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/cronjob.go) | 周期任务 `CronJob`，含 `ReadyCh` 就绪门与抖动。 |
| [internal/controller/manager.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go) | 装配入口：把 `eventLoop`、gRPC、provisioner 用包装器挂上 Manager，并配置 Leader 选举。 |
| [internal/controller/health.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/health.go) | **健康检查与就绪**：`graphBuiltHealthChecker`，首图构建后才置 ready。 |
| [internal/controller/ngfsort/sort.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/ngfsort/sort.go) | **冲突排序**：按 Gateway API 规范比较两个资源的新旧。 |
| [internal/controller/state/dataplane/sort.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/sort.go) | 路由匹配优先级排序，最终在「平局」时回调 `ngfsort`。 |

> 提示：本讲的四个机制**横切**在主链路上——双缓冲在事件管线「中段」，Leader 选举在 Manager 装配层，ngfsort 在图构建/配置生成层，就绪检查在 handler 与 health server 之间。它们彼此独立，但共同回答一个问题：**当集群高频变化、控制面多副本运行时，NGF 如何既快又不出错？**

---

## 4. 核心概念与源码讲解

### 4.1 事件双缓冲：把 N 次变更压缩成远少于 N 次 reload

#### 4.1.1 概念说明

NGF 控制面是一个**事件驱动**的系统：Kubernetes 资源（Gateway、HTTPRoute、Service……）每一次增删改，都会被翻译成一个事件丢进 `eventCh`。最朴素的做法是「来一个事件，处理一次」——而「处理一次」通常意味着重新生成 NGINX 配置并触发一次 **reload**。

问题在于 reload 是昂贵操作。源码注释把原因写得很直白：

> [internal/framework/events/loop.go:17-24](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L17-L24) —— Batching is needed because handling an event ... will typically result in reloading NGINX, which is an operation we want to minimize: (1) A reload takes time - at least 200ms ... (2) A reload can have side-effects for data plane traffic.

这段注释说明：当 EventLoop 攒了 100 个事件时，**一次性处理**远比「逐个处理触发 100 次 reload」要好。双缓冲就是实现「一次性处理」的机制。

#### 4.1.2 核心流程

双缓冲的核心是**两个切片 + 一个处理 goroutine + 一个 select 主循环**。规则是：**任意时刻最多只有一个批次在被处理**；处理期间新到达的事件先攒着，等当前批次处理完，再把攒下的事件合并成**一个**新批次。

用伪代码描述主循环：

```
准备「首批事件」(集群当前完整快照)  → 放入 currentBatch，立刻处理
进入主循环 select:
  case 收到新事件 e:
       nextBatch.append(e)
       如果当前没有批次在处理(handling==false):
           交换 current/next，开始处理 current
  case 当前批次处理完(handlingDone):
       handling = false
       如果 nextBatch 非空:
           交换 current/next，开始处理 current
  case ctx.Done:
       等在途批次处理完，退出
```

关键在于「**处理一批期间，新事件堆进 nextBatch；处理完后翻转合并**」。于是：如果 100 个事件在处理第一批的 ≥200ms 内陆续到达，它们会被**合并成 1 个批次**，只触发 1 次后续 reload——而不是 100 次。

#### 4.1.3 源码精读

**数据结构**——两个字段撑起整个机制：

[internal/framework/events/loop.go:25-40](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L25-L40) 定义了 `EventLoop` 结构体，注释明确写出双缓冲语义：处理 goroutine 总是读 `currentBatch`，处理期间新事件进 `nextBatch`，二者在启动处理前交换。

```go
// The EventLoop uses double buffering to handle event batch processing.
// The goroutine that handles the batch will always read from the currentBatch slice.
// While the current batch is being handled, new events are added to the nextBatch slice.
// The batches are swapped before starting the handler goroutine.
currentBatch EventBatch
nextBatch    EventBatch
```

**主循环 select**——三个 case 构成完整状态机：

[internal/framework/events/loop.go:111-141](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L111-L141) 是事件循环本体。两个关键 case：

```go
case e := <-el.eventCh:
    el.nextBatch = append(el.nextBatch, e)   // 新事件一律先堆进 nextBatch
    if !handling {
        swapAndHandleBatch()                 // 空闲则立刻处理
    }
case <-handlingDone:
    handling = false
    if len(el.nextBatch) > 0 {
        swapAndHandleBatch()                 // 处理完，若有积压则合并成一批再处理
    }
```

**交换与处理**——`swapAndHandleBatch` 先翻转两个切片再启动处理 goroutine：

[internal/framework/events/loop.go:81-85](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L81-L85) 调用 `swapBatches()` 后立即 `handleBatch()` 并置 `handling = true`。

[internal/framework/events/loop.go:145-148](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L145-L148) 是交换实现——交换两个切片头，再把 `nextBatch` 重置为长度 0（复用底层数组容量）：

```go
func (el *EventLoop) swapBatches() {
    el.currentBatch, el.nextBatch = el.nextBatch, el.currentBatch
    el.nextBatch = el.nextBatch[:0]
}
```

> **为什么不需要加锁？** 这是最精妙的一点。处理 goroutine 拿到的是 `currentBatch` 的底层数组（[handleBatch 把 `el.currentBatch` 按值传进 goroutine 闭包参数](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L67-L79)），而主循环只会 `append` 到 `nextBatch` 的底层数组。每次交换后，被读的数组（current）与被写的数组（next）始终是**两块不同的内存**。又因为任意时刻最多只有一个批次在处理（`handling` 标志 + `handlingDone` 同步），重置 `nextBatch[:0]` 时绝不会和正在读它的 goroutine 冲突。所以无锁也安全。

**首批事件**——启动前必须先拿到集群完整视图：

[internal/framework/events/loop.go:98-106](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L98-L106) 在进入主循环前，先 `preparer.Prepare(ctx)` 生成首批事件并立即处理。注释（[loop.go:87-96](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L87-L96)）解释了原因：否则 handler 首次生成配置时只能基于残缺视图，会让客户端看到瞬态 404。注意首批与随后控制器上报的事件会有重复，但「同 Generation 的重复 upsert 不触发重配」（幂等），所以无害。

#### 4.1.4 代码实践

**实践目标**：用源码逻辑推演一次高频变更场景，量化双缓冲把 reload 次数压低了多少倍。

**操作步骤（源码阅读 + 推演型实践）**：

1. 打开 [internal/framework/events/loop.go:111-141](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L111-L141)，确认：在「有批次正在处理」时，新事件只 `append` 到 `nextBatch`，**不会**立即触发处理。
2. 设定场景：某自动化系统在 **100ms 内连续修改了 1000 个 HTTPRoute**（典型的高频抖动）。
3. 假设每批处理耗时 \(T_{\text{handle}} = 200\text{ms}\)（reload 下界）。

**需要观察的现象 / 预期结果**：

- **不做批处理（朴素方案）**：每个事件各触发一次 reload，共 \(R_{\text{无}} = 1000\) 次。
- **双缓冲**：第一批处理一开始，1000 个事件在 100ms 内全部堆进 `nextBatch`；第一批处理完（200ms 后），`handlingDone` 触发 `swapAndHandleBatch`，把这 1000 个事件**合并成 1 批**处理。于是只有首批 1 次 + 合并批 1 次 = \(R_{\text{双缓冲}} \approx 2\) 次。

更一般地，若一个持续 \(T_{\text{burst}}\) 的事件洪流，每批处理需 \(T_{\text{handle}}\)，则 reload 次数被压缩为约

\[
R_{\text{双缓冲}} \approx \left\lceil \frac{T_{\text{burst}}}{T_{\text{handle}}}\right\rceil,\qquad T_{\text{handle}}\geq 200\text{ms}
\]

而朴素方案是 \(R_{\text{无}}=N\)（事件总数）。本例压缩比约 \(1000/2 = 500\) 倍。

**本地验证（可选）**：在运行的 NGF Pod 中，开 `-v=1` 日志，可看到形如 `Handling events from the batch total=...`（[loop.go:72](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L72)）与带 `batchID` 的日志；制造一批高频变更后，观察「同一个 `batchID` 的 total 会很大、`batchID` 增长很慢」，即可佐证批处理生效。若无法本地运行，记为**待本地验证**。

> 说明：双缓冲只负责「合并事件」，是否真的少 reload 还取决于下游——`ChangeProcessor` 的 `changed` 判定（u4-l4）会过滤掉无意义变更，`NginxUpdater` 的 `configVersion` 比对（u7-l2）会在内容不变时零下发。三者叠加才是「N 次变更 → 0~1 次 reload」的完整答案。

#### 4.1.5 小练习与答案

**练习 1**：如果 `swapBatches` 里去掉 `el.nextBatch = el.nextBatch[:0]` 这一行，会发生什么？

> **答案**：交换后 `nextBatch` 仍保留旧内容，下次 `append` 会在旧事件之后继续追加，导致**已经处理过的事件被重复进队**，批次越来越大、永远处理不完历史事件。所幸下游幂等不会出错配置，但会浪费 CPU、且 `total` 日志会失真。

**练习 2**：为什么 `handleBatch` 要把 `el.currentBatch` 作为闭包参数 `batch` 传进去，而不是直接在 goroutine 里读 `el.currentBatch`？

> **答案**：goroutine 启动后，主循环可能很快又执行一次 `swapBatches` 改写 `el.currentBatch`。按值传参（拷贝切片头）后，goroutine 持有的是**启动那一刻**的切片快照，不受后续交换影响——这是无锁安全的关键之一。

---

### 4.2 Leader 选举与 Runnable 语义：多副本单写

#### 4.2.1 概念说明

生产环境通常部署多个控制面副本以做高可用。但 NGINX 配置只能由**一个**权威来源生成下发，否则两个副本各写各的会导致数据面配置来回抖动。NGF 的解法是经典的 **Leader 选举**：多个副本抢同一把 Kubernetes **Lease** 锁，只有抢到锁的 **leader** 执行「改集群」的动作，其余副本做**热备**（持续 watch、持续构建图，但不写出/不下发）。

关键洞察是「**热备 + 单写**」：

- **热备**：非 leader 副本也跑控制器、也跑事件循环、也构建图——这样 leader 倒下时，新 leader 已经持有最新状态，可秒级接管，而不是从冷启动重新 list 全集群。
- **单写**：真正会改集群（下发配置、写 status、创建数据面 Deployment）的动作，**默认关闭**，只有当选 leader 时才一次性打开。

#### 4.2.2 核心流程

NGF 用 controller-runtime 提供的 Leader 选举（基于 Lease 对象），并通过三个自研 Runnable 包装器表达「这个任务该在哪些副本上跑」：

| 包装器 | `NeedLeaderElection()` | 语义 | 典型用途 |
| --- | --- | --- | --- |
| `LeaderOrNonLeader` | `false` | 所有副本都跑 | eventLoop、gRPC 服务、provisioner 事件循环 |
| `Leader` | `true` | 仅 leader 跑 | telemetry 上报 |
| `CallFunctionsAfterBecameLeader` | `true` | 当选 leader 时**一次性**回调一批函数 | 打开三把「写开关」 |

`NeedLeaderElection()` 是 controller-runtime 的启动时机开关：返回 `true` 的 Runnable 只有在当前副本是 leader（或未启用选举）时才会被 `Start`。

#### 4.2.3 源码精读

**三个包装器**——语义极简，核心就是 `NeedLeaderElection()` 的返回值：

[internal/framework/runnables/runnables.go:9-35](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/runnables.go#L9-L35) 定义 `Leader`（返回 `true`）与 `LeaderOrNonLeader`（返回 `false`），二者都内嵌一个 `manager.Runnable`。

[internal/framework/runnables/runnables.go:37-67](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/runnables.go#L37-L67) 定义 `CallFunctionsAfterBecameLeader`，其 `Start` 依次调用所有 `enableFunctions`：

```go
func (j *CallFunctionsAfterBecameLeader) Start(ctx context.Context) error {
    for _, f := range j.enableFunctions {
        f(ctx)
    }
    return nil
}
```

**装配——谁用了哪个包装器**：

[internal/controller/manager.go:257-274](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L257-L274) 是核心装配点。`eventLoop` 用 `LeaderOrNonLeader`（所有副本都跑，做热备）；紧接着用 `CallFunctionsAfterBecameLeader` 注册三个「写开关」：

```go
if err = mgr.Add(&runnables.LeaderOrNonLeader{Runnable: eventLoop}); err != nil { ... }

if err = mgr.Add(runnables.NewCallFunctionsAfterBecameLeader([]func(context.Context){
    groupStatusUpdater.Enable,    // 允许写资源 status
    nginxProvisioner.Enable,      // 允许创建/回收数据面工作负载
    eventHandler.enable,          // 允许下发配置
})); err != nil { ... }
```

这三个 `.Enable` 就是「单写」的总闸：非 leader 副本里它们不会被调用，于是即便 eventLoop 在跑、图在构建，也不会真正下发配置或写 status。当选 leader 的那一刻，它们被一次性翻开。

**Manager 层的选举配置**：

[internal/controller/manager.go:509-530](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L509-L530) 在 `createManager` 里配置选举：

```go
LeaderElection:          cfg.LeaderElection.Enabled,
LeaderElectionNamespace: cfg.GatewayPodConfig.Namespace,
LeaderElectionID:        cfg.LeaderElection.LockName,        // Lease 对象的名字
LeaderElectionReleaseOnCancel: false,
Controller: ctrlcfg.Controller{
    NeedLeaderElection: helpers.GetPointer(false),            // 控制器在非 leader 上也跑(热备)
},
```

两处注释值得细读：

- [manager.go:520-524](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L520-L524)：**故意不**开启 `LeaderElectionReleaseOnCancel`。原因是 Manager 优雅停止时会**等待所有 Runnable（含 Leader-only 的）跑完**；若开启该选项，新 leader 可能在旧 leader 的周期任务（如 telemetry）还没跑完时就启动，造成**同一周期任务被两个副本重叠执行**。关掉它，让交接更安全。
- [manager.go:525-528](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L525-L528)：控制器设 `NeedLeaderElection: false`，让非 leader 副本也 watch 资源、构建图——这就是「热备」。

> 补充：周期任务（如 telemetry）用 `Leader` 套 `CronJob`，二者叠加得到「仅 leader、且等首图就绪后、带抖动地周期执行」。[internal/framework/runnables/cronjob.go:41-57](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/cronjob.go#L41-L57) 的 `CronJob.Start` 先阻塞在 `<-ReadyCh` 上（就绪门），再 `wait.JitterUntilWithContext` 周期执行 worker，`sliding=true` 表示周期在每次 worker 执行后才重新计算。

#### 4.2.4 代码实践

**实践目标**：判定若干典型任务该用哪个包装器，并说明理由。

**操作步骤（推理型实践）**：

1. 打开 [manager.go:264](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L264)（eventLoop）、[manager.go:336](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L336)（grpcServer）、[manager.go:384](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L384)（provisioner loop），确认它们都用 `LeaderOrNonLeader`。
2. 对下面 4 个任务，逐一判断该用 `Leader`、`LeaderOrNonLeader` 还是 `CallFunctionsAfterBecameLeader`：
   - (a) 接收数据面 Agent 的 gRPC 长连接；
   - (b) 把资源 status 写回 API server；
   - (c) 每 24h 上报一次产品遥测；
   - (d) 给某个 Gateway 创建数据面 Deployment。

**预期结果**：

- (a) **LeaderOrNonLeader**：每个副本都和**自己绑定的数据面 Pod** 通信，不是集群级单写。
- (b) **CallFunctionsAfterBecameLeader**（其背后是 `groupStatusUpdater.Enable`）：status 是集群级写，必须单写。
- (c) **Leader**（叠加 CronJob）：集群级统计，只应统计/上报一次。
- (d) **CallFunctionsAfterBecameLeader**（其背后是 `nginxProvisioner.Enable`）：创建工作负载是集群级写。

> 通用判据：**与本副本数据面 Pod 绑定**的用 `LeaderOrNonLeader`；**集群维度的写或统计**用 `Leader` 或 `CallFunctionsAfterBecameLeader`。这一规律可推广到任何你想新增的后台任务。

#### 4.2.5 小练习与答案

**练习 1**：为什么 eventLoop 用 `LeaderOrNonLeader` 而不是 `Leader`？

> **答案**：非 leader 副本需要持续处理事件、构建图，才能保持「热备」状态，leader 倒下时秒级接管。如果 eventLoop 只在 leader 上跑，新 leader 上台后要冷启动 list 全集群并重建图，接管窗口会拉长。代价是所有副本都做重复计算——但真正昂贵的「下发/写」已被 `CallFunctionsAfterBecameLeader` 的三把开关挡住。

**练习 2**：如果误把 telemetry 用 `LeaderOrNonLeader`，会发生什么？

> **答案**：每个副本都会周期上报，导致同一集群的画像被重复统计多次，污染遥测数据。这正是它必须用 `Leader` 的原因。

---

### 4.3 冲突排序 ngfsort：确定性的冲突裁决

#### 4.3.1 概念说明

Gateway API 规范规定：当多条 Route（或多个策略、多个 ListenerSet）**匹配同一流量**产生冲突时，必须按一套**确定性的优先级规则**裁决出唯一赢家。规范给出的「终极平局打破」规则是：

1. 创建时间更早（oldest by creation timestamp）的优先；
2. 仍相同，则按 `{namespace}/{name}` 字母序，靠前的优先。

为什么要**确定性**？因为 NGF 每次 rebuild 都要把内部对象排成有序切片再去生成配置；如果排序不确定（例如依赖 map 遍历顺序），同一组资源在不同时刻可能生成**不同**的 NGINX 配置，触发无谓 reload，甚至产生来回抖动。`ngfsort` 就是把这个「规范平局规则」实现成单一可复用的比较函数，确保「**相同输入 → 相同输出**」。

#### 4.3.2 核心流程

`ngfsort` 包提供两个比较函数，本质都是把「创建时间 → namespace → name」三级字典序封装成一个 `Less` 谓词：

```
Less(meta1, meta2):
  if 创建时间相同:
      if namespace 相同:  return name1 < name2
      else:               return namespace1 < namespace2
  else:                   return 创建时间更早的为 true(更小)
```

调用方用 `sort.Slice(..., ngfsort.LessXxx)` 把冲突对象排成稳定顺序，然后「第一个」就是赢家。注意：`ngfsort` 只负责**平局打破**，更上层的业务优先级（路径长度、header 匹配数等）由各调用方先比较，只有这些都比较不出胜负时才落到 `ngfsort`。

#### 4.3.3 源码精读

**核心比较函数**：

[internal/controller/ngfsort/sort.go:10-19](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/ngfsort/sort.go#L10-L19) 是 `LessObjectMeta`，注释直接指向 Gateway API 的冲突裁决文档：

```go
// LessObjectMeta compares two ObjectMetas according to the Gateway API conflict resolution guidelines.
func LessObjectMeta(meta1 *metav1.ObjectMeta, meta2 *metav1.ObjectMeta) bool {
    if meta1.CreationTimestamp.Equal(&meta2.CreationTimestamp) {
        if meta1.Namespace == meta2.Namespace {
            return meta1.Name < meta2.Name
        }
        return meta1.Namespace < meta2.Namespace
    }
    return meta1.CreationTimestamp.Before(&meta2.CreationTimestamp)
}
```

[internal/controller/ngfsort/sort.go:25-37](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/ngfsort/sort.go#L25-L37) 是 `LessClientObject`，对 `client.Object` 做同样的事（从对象上取 `CreationTimestamp`/`Namespace`/`Name`）。二者等价，只是一个吃 `*ObjectMeta`、一个吃 `client.Object`，方便不同调用点复用。

**调用点 1——L4 路由冲突**：

[internal/controller/state/graph/route_common.go:635-638](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L635-L638) 按 `ngfsort.LessClientObject` 对 L4 路由切片排序，注释明说「so that we process the routes in the priority order」：

```go
sort.Slice(l4RouteSlice, func(i, j int) bool {
    return ngfsort.LessClientObject(l4RouteSlice[i].Source, l4RouteSlice[j].Source)
})
```

**调用点 2——策略冲突**：

[internal/controller/state/graph/policies.go:797-803](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L797-L803) 对附着到同一目标的多个策略排序，让后续 `markConflictedPolicies` 能按「优先级顺序」裁决出赢家、给其余策略打 `Conflicted` 条件。

**调用点 3——ListenerSet 冲突**：

[internal/controller/state/graph/listenerset.go:192-197](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/listenerset.go#L192-L197) 对引用同一 Gateway、可能冲突的多个 ListenerSet 排序。

**调用点 4——HTTPRoute 匹配规则的终极平局**：

[internal/controller/state/dataplane/sort.go:69-98](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/sort.go#L69-L98) 的 `higherPriority` 最能体现「ngfsort 作为最后一道平局打破」的角色。它先按 Gateway API 规范比较业务优先级（是否有 method 匹配、header 数、query 参数数，见注释 [sort.go:36-68](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/sort.go#L36-L68)），业务维度都比不出胜负时，最后一行落到 `ngfsort`：

```go
// If still tied, compare the object meta of the two routes.
return ngfsort.LessObjectMeta(rule1.Source, rule2.Source)
```

> 这正是「**ngfsort 在冲突解析中的作用**」：它是所有冲突裁决链路共同的「兜底 tie-breaker」，把规范里那两条客观、可复现的规则（创建时间、`{ns}/{name}`）固化为代码，消除一切顺序敏感。

#### 4.3.4 代码实践

**实践目标**：手工验证 `ngfsort` 的确定性，并理解它为何能减少无谓 reload。

**操作步骤（推演型实践）**：

1. 构造两个「业务维度完全相同」的 HTTPRoute，仅 `namespace/name` 与创建时间不同：
   - RouteA：`default/route-a`，创建时间 `10:00:00`；
   - RouteB：`default/route-b`，创建时间 `10:00:05`。
2. 用 [LessObjectMeta](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/ngfsort/sort.go#L10-L19) 的逻辑推演：二者创建时间不等，`LessObjectMeta(A, B)` 因 A 更早而返回 `true`。
3. 现在把 A、B 放进一个集合，**任意打乱顺序**后再排序。

**预期结果**：

- 无论初始顺序如何，排序后切片**永远是 `[A, B]`**——A 永远排第一，成为冲突赢家。
- 这意味着：哪怕控制器两次事件把 A、B 以不同顺序上报，NGF 生成的 NGINX 配置里赢家恒为 A，配置文本不变，`configVersion` 不变，**不会触发 reload**。

> 可对照单元测试 [internal/controller/ngfsort/sort_test.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/ngfsort/sort_test.go) 验证各种创建时间相同/不同、namespace 相同/不同的组合下，比较结果是否符合规范。

#### 4.3.5 小练习与答案

**练习 1**：如果两个资源创建时间戳**精确相等**（同一秒创建），`LessObjectMeta` 如何裁决？

> **答案**：进入 `if meta1.CreationTimestamp.Equal(...)` 分支，再比 `Namespace`；若 namespace 也相同（同命名空间），最后比 `Name`（字母序）。所以 `default/route-a` < `default/route-b`。这保证即使时间戳并列也有唯一确定的赢家。

**练习 2**：为什么 `higherPriority`（dataplane/sort.go）要先比 header 数、query 参数数，最后才用 `ngfsort`？

> **答案**：Gateway API 规范规定冲突优先级是**多级 tie-break**：业务相关度（匹配越具体越优先）在前，创建时间/名字这种「纯客观排序」只作为最后兜底。把更具体的匹配排在前面，能让真正更「精准」的路由赢；只有两个路由在所有业务维度都并列时，才用 ngfsort 的客观规则打破平局。

---

### 4.4 健康检查与就绪：首图就绪才接流量

#### 4.4.1 概念说明

Kubernetes 用两类探针管理 Pod 流量：

- **liveness**：进程是否活着（失败会被重启）；
- **readiness**：是否准备好接流量（失败会从 Service 的 Endpoints 里摘除）。

对 NGF 控制面而言，「进程启动」远不等于「准备好」。如果进程一启动就报 ready，Kubernetes 会立刻把流量导过来，可此时 NGINX 配置还没生成下发，数据面拿到的可能是空配置——客户端会看到大量 **404**。

NGF 的做法是：**只有当第一张图构建完成（意味着配置已基于完整集群视图生成并下发）后，才把 Pod 置为 ready**。这把「就绪」从「进程维度」精确收窄到「配置维度」，避免了「通电但没配置」的窗口期。

#### 4.4.2 核心流程

`graphBuiltHealthChecker` 是一个极简的两态状态机：

```
初始: ready=false, readyCh 未关闭
  ↓ (handler 处理完首批事件、构建出第一张图后调用 setAsReady)
就绪: ready=true, close(readyCh)  → 同时唤醒所有等在 readyCh 上的消费者
```

它对外提供三件事：
1. `readyCheck`：满足 controller-runtime `Checker` 类型，挂到 health/readyz server；
2. `getReadyCh`：返回一个 `<-chan struct{}`，供其他组件（如 telemetry 的 CronJob）阻塞等待「就绪」事件；
3. `setAsReady`：handler 在首图构建后调用，原子地翻转为 ready 并关闭 channel。

#### 4.4.3 源码精读

**状态结构**——一个 channel + 一个 bool + 一把读写锁：

[internal/controller/health.go:16-22](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/health.go#L16-L22) 定义 `graphBuiltHealthChecker`。`readyCh` 在构造时（[health.go:9-14](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/health.go#L9-L14)）就 `make` 好，代表「是否就绪」。

**就绪探针**：

[internal/controller/health.go:24-35](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/health.go#L24-L35) 的 `readyCheck` 用读锁保护，未就绪时返回 error（Kubernetes 据此摘除流量）：

```go
func (h *graphBuiltHealthChecker) readyCheck(_ *http.Request) error {
    h.lock.RLock()
    defer h.lock.RUnlock()
    if !h.ready {
        return errors.New("control plane is not yet ready")
    }
    return nil
}
```

**翻转就绪**：

[internal/controller/health.go:37-44](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/health.go#L37-L44) 的 `setAsReady` 用写锁置 `ready=true` 并 `close(readyCh)`——关闭 channel 是 Go 里「一次性广播」的标准手法，所有阻塞在 `getReadyCh()` 上的消费者会同时被唤醒。

**谁调用 setAsReady**：

[internal/controller/handler.go:206-211](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L206-L211) 在 `eventHandlerImpl.HandleEventBatch` 里，`processor.Process` 构建出图后，**仅首次**调用 `setAsReady`：

```go
gr := h.cfg.processor.Process(ctx)
// Once we've processed resources on startup and built our first graph, mark the Pod as ready.
if !h.cfg.graphBuiltHealthChecker.ready {
    h.cfg.graphBuiltHealthChecker.setAsReady()
}
```

注意是「if not ready 才 set」——保证只翻转一次（重复 `close` 一个已关闭的 channel 会 panic，所以这个判断既是优化也是安全防护）。

**readyCh 的另一个消费者**：

[internal/controller/manager.go:455](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L455) 把 `healthChecker.getReadyCh()` 作为 `ReadyCh` 传给 telemetry 的 CronJob（[cronjob.go:42-47](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/cronjob.go#L42-L47) 先阻塞在它上面）。于是 telemetry 也会等到首图就绪后才开始周期上报——避免上报一份空画像。

> 与 u3-l2 的呼应：controller-runtime Manager 的 health/readyz server 默认挂载；readyz 在「首图构建完成」而非「进程启动」时才返回成功。这与「readyz 就绪前不接流量」共同构成了启动期的安全门。

#### 4.4.4 代码实践

**实践目标**：理解就绪翻转的「一次性」语义，并验证它对启动窗口的保护。

**操作步骤（源码阅读 + 推演型实践）**：

1. 阅读 [handler.go:206-211](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L206-L211)，确认 `setAsReady` 只在 `!ready` 时调用。
2. 阅读单元测试 [handler_test.go:574-607](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler_test.go#L574-L607)：测试先断言 `readyCheck` 失败、`readyChannel` 未关闭，调用 `HandleEventBatch` 后再断言 `readyCheck` 成功、`readyChannel` 已关闭。

**预期结果**：

- 启动后、首图构建前：`readyCheck` 返回 error → Pod 不 ready → Service 不导流量。
- 首批事件处理完、第一张图构建后：`setAsReady` 被调用一次 → `readyCheck` 此后永远返回 nil → Pod ready → 开始接流量。
- 此后即便再处理 1000 个批次，也不会再 `close(readyCh)`（被 `if !ready` 挡住），不会 panic。

> **待本地验证**（可选）：在一个 kind 集群里部署 NGF，在控制面 Pod 启动初期反复 `curl http://<pod>:8081/readyz`（health 端口），应先看到 503/错误、首图就绪后转为 200；同时 `kubectl get pod -w` 观察 `READY` 列从 `0/1` 变 `1/1` 的时机，应与首图就绪吻合而非与进程启动吻合。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `setAsReady` 用 `close(readyCh)` 而不是再往 channel 里发一个值？

> **答案**：就绪是一个**一次性、广播**事件——可能有多个消费者（readyz、telemetry、未来的其他组件）都在等它。`close` 会让**所有**阻塞在该 channel 上的消费者同时解除阻塞，且后续任何接收都能立即拿到零值，天然表达「永久就绪」。发送单个值只能唤醒一个消费者，且需要预知消费者数量。

**练习 2**：如果删掉 [handler.go:209](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L209) 的 `if !h.cfg.graphBuiltHealthChecker.ready` 判断，第二次处理批次时会怎样？

> **答案**：会再次 `close` 一个已经关闭的 `readyCh`，触发 `close of closed channel` panic，直接让控制面进程崩溃重启。所以这个判断是不可省略的安全防护。

---

## 5. 综合实践

把本讲四个机制串起来，设计一个**「高频变更 + 多副本」的综合分析任务**。

**场景**：生产集群里 NGF 以 2 副本部署（开启 Leader 选举）。某 CI 一次性 apply 了 500 个 HTTPRoute，其中有 3 个业务维度完全相同、互相冲突（`ns-a/route-x`、`ns-a/route-y`、`ns-b/route-z`，创建时间依次为 t、t、t+1s）。

**请按下列步骤完成分析**：

1. **双缓冲**：参照 [loop.go:111-141](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L111-L141) 推演——这 500 个 upsert 事件会在首批处理期间堆进 `nextBatch`，最终合并成 1 批处理。请估算 reload 次数（提示：用 4.1.4 的公式，设 \(T_{\text{handle}}=200\text{ms}\)、事件在 50ms 内全部到达）。
2. **Leader 选举**：参照 [manager.go:257-274](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L257-L274) 说明——只有 leader 副本会真正下发配置；另一个副本虽也构建图，但其 `eventHandler.enable` 未被翻开。请说明若此时 leader 崩溃，新 leader 为何能秒级接管（提示：热备 + `CallFunctionsAfterBecameLeader`）。
3. **冲突排序**：参照 [ngfsort/sort.go:10-19](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/ngfsort/sort.go#L10-L19) 判断 3 个冲突 Route 的赢家。注意 `ns-a/route-x` 与 `ns-a/route-y` 创建时间同为 t、同 namespace，故比 name → `route-x` 胜；`ns-b/route-z` 时间为 t+1s 更晚，败给 t。综合赢家是 `ns-a/route-x`。
4. **就绪**：参照 [handler.go:206-211](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L206-L211) 说明——若这是 NGF 启动后的首批事件，处理完才会 `setAsReady`，Pod 才开始接流量；若 NGF 早已就绪，则本批处理不会改动就绪状态。

**产出**：一张时序表，列出「事件到达 → 双缓冲合并 → leader 单写下发 → ngfsort 选出赢家 → 配置落盘」各步对应的源码位置与本场景下的具体取值。这张表能帮你把「并发/性能/可靠性」三条原本分散的线索拧成一根。

## 6. 本讲小结

- **事件双缓冲**用 `currentBatch`/`nextBatch` 两个切片把「处理一批期间到达的新事件」攒起来，处理完合并成一批，将 reload 次数从「每事件一次」（\(N\)）压缩到约「每处理窗口至多一次」（\(\lceil T_{\text{burst}}/T_{\text{handle}}\rceil\)），且因读写落在不同底层数组而**无需加锁**。
- **Leader 选举**用 Kubernetes Lease 实现多副本「单写」：`LeaderOrNonLeader` 让 eventLoop/gRPC/provisioner 在所有副本跑（**热备**），`CallFunctionsAfterBecameLeader` 在当选时一次性翻开 `groupStatusUpdater.Enable`/`nginxProvisioner.Enable`/`eventHandler.enable` 三把写开关；`LeaderElectionReleaseOnCancel=false` 防止周期任务在交接时重叠执行。
- **ngfsort** 是所有冲突裁决链路的**确定性兜底 tie-breaker**，把规范里的「创建时间 → namespace → name」三级规则固化为可复用比较函数，保证「相同输入 → 相同输出」，消除顺序敏感与无谓 reload。
- **健康检查与就绪**以「首图构建完成」而非「进程启动」作为 ready 信号，`graphBuiltHealthChecker` 用「channel 关闭」做一次性广播，既驱动 readyz 摘/接流量，也作为 telemetry 等周期任务的就绪门，避免「通电但没配置」的 404 窗口。
- 四者**横切**在主链路上、彼此独立又互相增强：双缓冲管「合并」、Leader 选举管「单写」、ngfsort 管「确定性」、就绪检查管「对外契约」，共同支撑 NGF 在高频变更与多副本下的「快而稳」。

## 7. 下一步学习建议

- 想看双缓冲下游如何把「合并后的事件」再压到「零 reload」：继续读 **u4-l4（ChangeProcessor 的 `changed` 判定）** 与 **u7-l2（`configVersion` 内容驱动版本号）**，三者构成完整的「N → 0~1」压缩链。
- 想深入 Leader 选举与周期任务：复习 **u3-l4**，并对照 **u11-l2（telemetry）** 看 `Leader` + `CronJob` 的真实用法。
- 想理解冲突裁决的完整规则：阅读 **u5-l2（路由与监听器）** 与 Gateway API 官方文档的 [Conflict Resolution Guidelines](https://gateway-api.sigs.k8s.io/concepts/guidelines/?h=conflict#conflicts)，再看 `dataplane/sort.go` 的 `higherPriority` 如何把业务优先级与 ngfsort 衔接。
- 下一讲 **u13-l3（二次开发：新增功能与扩展点）** 将从「如何改动」的角度收束整个学习手册，建议在动手扩展前回顾本讲的「确定性」与「单写」原则——任何新后台任务、新冲突场景都应遵循它们。
