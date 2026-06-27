# Leader Election 与 Runnables

## 1. 本讲目标

本讲承接 [u3-l1 StartManager 全景](u3-l1-manager-startup-overview.md)。上一讲我们看到 `StartManager` 把控制面装配成一个运行进程，其中提到「非 leader 副本也在跑控制器做热备，但改集群的动作默认关闭、仅当选 leader 才打开」。本讲就把这把「钥匙」拆开讲透。

学完后你应该能够：

1. 说清 **leader 选举（Leader Election）** 在 NGF 里解决什么问题、如何配置、什么时候触发。
2. 读懂 `internal/framework/runnables` 包里的三种 Runnable 包装器（`Leader` / `LeaderOrNonLeader` / `CallFunctionsAfterBecameLeader`），并知道它们如何用同一个 `NeedLeaderElection()` 方法表达完全相反的语义。
3. 掌握 `CronJob` 周期任务的实现，理解「抖动（jitter）」「就绪门（ReadyCh）」「sliding」三个关键设计，并看懂 telemetry 任务如何把它与 `Leader` 包装器组合成「只在 leader 上、周期性、且等首张图就绪后才上报」。
4. 面对 NGF 里任意一个被 `mgr.Add(...)` 注册的长期任务，能判断它该不该只在 leader 上运行，并给出理由。

---

## 2. 前置知识

本讲需要你先建立以下几个概念（不熟悉的话可以先停一下）：

- **控制器（controller）与控制循环**：NGF 用 controller-runtime 框架，核心是「watch 资源 → 触发 Reconcile → 期望状态对齐」。这部分在 [u3-l3 控制器注册](u3-l3-controller-registration-and-crd-discovery.md) 已讲过。
- **高可用（HA）与多副本**：生产环境通常把控制面 Deployment 跑多个副本（Pod）。如果每个副本都去改集群、都去下发配置，就会「打架」（重复写状态、重复 reload NGINX）。所以需要一个机制选出**唯一一个「主」副本**来负责写操作。
- **controller-runtime Manager**：它是控制面的「大管家」，统一管理 Cache、Client、Metrics/Health Server，以及一堆**长期运行的 goroutine**。这些长期 goroutine 在框架里被抽象成一个接口——`manager.Runnable`。
- **Kubernetes Lease 对象**：一种很轻量的资源，常被用作分布式锁。controller-runtime 的 leader 选举就是靠抢一把 Lease 锁来决定谁是 leader。

> 关键一句话：**Runnable 是「一个长期 goroutine」的抽象，Leader Election 是「这个 goroutine 该不该在当前副本上启动」的开关。** 本讲的全部内容都在解释这两者如何配合。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [internal/framework/runnables/runnables.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/runnables.go) | 三个 Runnable 包装器：`Leader`、`LeaderOrNonLeader`、`CallFunctionsAfterBecameLeader`。本讲的核心。 |
| [internal/framework/runnables/cronjob.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/cronjob.go) | 周期任务 `CronJob`：周期性调用一个 worker 函数，带抖动与就绪门。 |
| [internal/framework/runnables/doc.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/doc.go) | 包注释，一句话说明本包「在 leader 选举启用时」辅助创建 Runnable。 |
| [internal/controller/manager.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go) | 装配现场：`createManager` 配置 leader 选举；多处 `mgr.Add(...)` 用包装器注册事件循环、gRPC 服务、provisioner、telemetry 任务等。 |
| [internal/controller/handler.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go) | `eventHandlerImpl.enable`：成为 leader 时翻转 `leader=true` 并立即下发一次配置。 |
| [internal/controller/provisioner/provisioner.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/provisioner.go) | `NginxProvisioner.Enable`：成为 leader 后才允许创建/回收数据面资源。 |
| [internal/controller/status/leader_aware_group_updater.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/leader_aware_group_updater.go) | `LeaderAwareGroupUpdater.Enable`：成为 leader 后把缓冲的状态写回请求一次性冲刷出去。 |
| [internal/framework/runnables/runnables_test.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/runnables_test.go) | 三个包装器的单元测试，断言 `NeedLeaderElection()` 的返回值与 `enableFunctions` 的调用。 |
| [internal/framework/runnables/cronjob_test.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/cronjob_test.go) | CronJob 的单元测试，验证周期执行与 context 取消的行为。 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先讲 **Leader 选举语义**（为什么需要、怎么配），再讲 **Runnable 包装**（NGF 用什么机制把任务分成「只在 leader 跑」和「所有副本都跑」），最后讲 **CronJob 周期任务**（一个把前两者组合起来的典型应用：telemetry）。

### 4.1 Leader 选举语义

#### 4.1.1 概念说明

NGF 的控制面在生产中通常以多副本 Deployment 部署（见 [u1-l4](u1-l4-install-and-deploy.md)）。多副本带来一个根本矛盾：

- **希望多副本**：单点故障时另一个副本能立刻顶上（高可用）。
- **但又不能多副本都写**：如果每个副本都去下发 NGINX 配置、都去写 Gateway/HTTPRoute 的 status、都去创建数据面 Deployment，集群会被重复操作打乱。

leader 选举就是解决这个矛盾的标准做法：**在所有副本里选出一个「主」（leader），只有它执行会改变集群状态的动作；其余副本（non-leader）保持运行、保持 watch、保持内存里有一份最新的图，作为「热备」随时准备接班。**

在 controller-runtime 里，这套机制由 Manager 内置：多个副本共同争抢一把以 `LeaderElectionID` 命名的 Kubernetes Lease 锁（基于 `coordination.k8s.io/Lease`），抢到的就是 leader。lease 有租期，leader 要定期续租；一旦 leader 宕机来不及续租，租期到期后其他副本就能接管。

#### 4.1.2 核心流程

leader 选举的生命周期可以画成下面这条主线：

```text
Manager.Start(ctx)
   │
   ├── 是否开启 leader 选举？(LeaderElection: true)
   │       否 → 当前副本就是事实上的 leader，所有 Runnable 立即启动
   │       是 → 去抢 Lease 锁(LockName) ↓
   │
   ├── 抢到锁？ ── 否 ──► 作为 non-leader 运行：
   │                     · 启动 NeedLeaderElection()=false 的 Runnable
   │                     · 控制器照常 watch、Reconcile、构建图（热备）
   │                     · 写动作被 Enable 开关拦住，不真正改集群
   │
   └── 抢到锁 ── 是 ──► 成为 leader：
                         · 启动 NeedLeaderElection()=true 的 Runnable
                         · 触发 CallFunctionsAfterBecameLeader：翻开三把写开关
                         · (原 leader 失去锁后，Manager.Start 返回错误退出)
```

注意一个容易混淆的点：**「成为 leader」与「进程启动」是两个不同时刻**。进程一启动，non-leader 相关的 Runnable 和控制器就跑起来了；而 leader 相关的 Runnable 要等到抢到锁那一刻才启动。这就是为什么本讲后面要区分「在 leader 上跑」和「在所有副本上跑」。

#### 4.1.3 源码精读

leader 选举在 `createManager` 里通过 `manager.Options` 配置：

[internal/controller/manager.go:517-528](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L517-L528) —— 选举开关、锁名、命名空间，以及一个刻意为之的 `LeaderElectionReleaseOnCancel: false`。

关键配置项逐条解释：

- `LeaderElection: cfg.LeaderElection.Enabled`：是否开启选举，由 [u2-l2](u2-l2-controller-command-and-config.md) 讲过的 flag `--leader-election-enable` 决定。关闭时单副本也能跑（它就是事实上的 leader）。
- `LeaderElectionID: cfg.LeaderElection.LockName`：**锁名是关键**。两个 NGF 实例只有在用**同一个 LockName + 同一个命名空间**时才会互相竞争同一个 leader 身份。所以多副本 HA 必须保证 LockName 一致。
- `LeaderElectionReleaseOnCancel: false`：这是源码注释里特意说明的一个反直觉选择（见下面引文）。正常 Manager 优雅退出时会等待所有 Runnable（含 leader-only 的）跑完；如果设为 `true`（立即释放锁），新 leader 可能在旧 leader 还没跑完上一个周期任务时就抢到锁并启动同名 Runnable，造成「同一个周期任务被两个副本各跑一半」。设为 `false` 正是为了避免这种重叠。

> 顺带注意 `Controller.NeedLeaderElection: false`（L527）——这一行是 NGF 「热备」设计的根：**所有控制器（watch Gateway/HTTPRoute 等的 Reconcile 循环）在 non-leader 副本上也照常运行**。否则非 leader 副本就不会去 watch 资源、不会构建图，一旦当选 leader 还得从零冷启动，接管就会有延迟。

#### 4.1.4 代码实践

> **实践目标**：确认你的本地/测试环境里 leader 选举是否开启、锁名是什么、当前哪个 Pod 是 leader。

**操作步骤（源码阅读 + 集群观察型）：**

1. 在源码中确认 flag 默认值：到 `internal/controller/config/config.go` 找 `LeaderElectionConfig` 的默认 `Enabled`（结合 [u2-l2](u2-l2-controller-command-and-config.md)）。
2. 如果你已按 [u1-l4](u1-l4-install-and-deploy.md) 在 kind 里部署了 NGF，把控制面副本数调到 2（修改 values 或直接 `kubectl scale deploy`），观察 lease：

   ```bash
   # 在 NGF 所在命名空间（通常 nginx-gateway）查看 lease 锁
   kubectl get lease -n nginx-gateway
   # 查看 lease 详情，holderIdentity 字段就是当前 leader 的 Pod 名
   kubectl describe lease -n nginx-gateway <lock-name>
   ```

3. 删除当前 leader Pod，观察 `holderIdentity` 在数秒内切换到另一个副本。

**需要观察的现象 / 预期结果：**

- `holderIdentity` 指向某个具体 Pod；删掉它后，短时间内（由 lease 的 renew/deadline 决定）会切到另一个 Pod。
- non-leader Pod 的日志里仍能看到控制器在 watch/Reconcile，但**不会**出现「下发配置 / 写 status」相关日志（具体边界在 4.2 讲）。

> 注：lease 的具体名称取决于部署配置；若你在生产/测试环境拿不到 lease，可只做第 1 步的源码阅读，其余标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：假设你把 NGF 部署成两个副本，但 `LeaderElection.Enabled=false`，会发生什么？为什么生产环境强烈不建议这么做？

> **参考答案**：选举关闭时，每个副本都被视作 leader，于是两个副本都会执行「写」动作——都会下发 NGINX 配置、都写 status、都创建数据面资源，产生重复操作甚至互相覆盖。生产环境必须开启选举以保证「单写」。

**练习 2**：`LeaderElectionReleaseOnCancel: false` 是为了解决什么问题？如果改成 `true` 会有什么风险？

> **参考答案**：保证旧 leader 在优雅退出时先把 leader-only 的周期任务（如 telemetry 一次上报）跑完，再释放锁。改成 `true` 会立即释放锁，新 leader 可能在旧 leader 的周期任务还没结束时就开始跑同名任务，造成同一周期任务被两个副本各执行一部分。

---

### 4.2 Runnable 包装

#### 4.2.1 概念说明

controller-runtime 的 Manager 用 `manager.Runnable` 接口抽象「一个长期 goroutine」：

```go
type Runnable interface {
    Start(ctx context.Context) error
}
```

任何实现了 `Start(ctx)` 的东西（事件循环、gRPC server、周期上报任务……）都可以用 `mgr.Add(r)` 注册给 Manager，由它在合适的时机起一个 goroutine 跑起来，并在 Manager 退出时通过取消 `ctx` 来收尾。

但「合适的时机」是哪种？这就需要第二个接口：

```go
type LeaderElectionRunnable interface {
    Runnable
    NeedLeaderElection() bool
}
```

Manager 在启动一个 Runnable 前会**类型断言**它是否实现了 `LeaderElectionRunnable`：若实现了且 `NeedLeaderElection()` 返回 `true`，就**等到本副本成为 leader 才启动**；否则立即（进程启动时）启动。

NGF 的巧妙之处在于：它不想给每个任务都手写一遍 `NeedLeaderElection()`，而是写了**三个包装器**，靠「组合 + 嵌入」复用底层 Runnable 的 `Start`，只额外表达 leader 语义。这就是 `internal/framework/runnables/runnables.go` 的全部价值。

#### 4.2.2 核心流程

三种包装器的语义对比：

| 包装器 | `NeedLeaderElection()` | 启动时机 | 用途 |
|---|---|---|---|
| `LeaderOrNonLeader{R}` | `false` | 进程启动（所有副本） | 每个副本都必须自己干的事：跑事件循环、给本副本的数据面 Pod 起 gRPC 服务 |
| `Leader{R}` | `true` | 当选 leader | 只能由 leader 做的事：周期性 telemetry 上报（避免重复上报） |
| `CallFunctionsAfterBecameLeader{fns}` | `true`（它本身是 Leader） | 当选 leader 后**执行一次**一组开关函数 | 翻开「写」开关：允许写 status、允许建/删数据面、允许下发配置 |

前两者是「装饰器」：它们内嵌 `manager.Runnable`，**完全不改变被包装对象的 `Start` 行为**，只是给它贴上一张「要不要等 leader」的标签。第三者是个「一次性动作盒子」：它的 `Start` 不做长循环，只是依次调用一组 `func(ctx)`，而这些函数就是打开各子系统写能力的开关。

为什么需要第三种？因为 NGF 的「写」能力并不是单独的 Runnable，而是**寄生在那些所有副本都在跑的 Runnable 内部**的一个布尔开关。例如事件处理器在所有副本都跑（它要构建图），但它内部有个 `leader` 标志位控制「要不要把图翻译成配置下发给 NGINX」。`CallFunctionsAfterBecameLeader` 就是当选 leader 时把这些标志位一次性翻开的钩子。

#### 4.2.3 源码精读

先看三个包装器的定义——它们短得惊人，核心就是 `NeedLeaderElection()` 返回值不同：

[internal/framework/runnables/runnables.go:9-35](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/runnables.go#L9-L35) —— `Leader` 与 `LeaderOrNonLeader` 都内嵌 `manager.Runnable`，唯一区别是 `NeedLeaderElection()` 一个返回 `true`、一个返回 `false`。

注意两个编译期断言：

```go
var (
    _ manager.LeaderElectionRunnable = &Leader{}
    _ manager.Runnable               = &Leader{}
)
```

这两行不产生运行时代码，只是让编译器在编译期保证 `Leader` 同时满足 `LeaderElectionRunnable` 和 `Runnable` 两个接口——一旦有人改坏了方法签名，编译立刻失败。这是 Go 里常见的「接口保真」惯用法。

再看第三个包装器 `CallFunctionsAfterBecameLeader`：

[internal/framework/runnables/runnables.go:48-67](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/runnables.go#L48-L67) —— 它的 `Start` 只是遍历调用所有 `enableFunctions`，且自身 `NeedLeaderElection()` 返回 `true`，所以这些函数**只在成为 leader 时执行一次**。

那么这三个开关函数具体是什么？看装配现场：

[internal/controller/manager.go:268-274](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L268-L274) —— 当选 leader 时依次调用 `groupStatusUpdater.Enable`、`nginxProvisioner.Enable`、`eventHandler.enable`。这就是 u3-l1 总结里说的「三把开关」。

三把开关各管一件写动作，逐一对应：

1. **`groupStatusUpdater.Enable`** —— 允许把 Gateway/HTTPRoute 等资源的 `status` 写回 API server：
   [internal/controller/status/leader_aware_group_updater.go:58-72](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/leader_aware_group_updater.go#L58-L72)。注意它把之前缓冲在 `groupReqs` 里的写请求一次性冲刷出去——这意味着 non-leader 期间产生的状态更新并没有丢，而是攒着等当选后补写。

2. **`nginxProvisioner.Enable`** —— 允许创建/回收数据面 NGINX Deployment：
   [internal/controller/provisioner/provisioner.go:194-204](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/provisioner.go#L194-L204)。当选后立即清理 `resourcesToDeleteOnStartup`（启动期间标记要删的无效资源），避免遗留垃圾。

3. **`eventHandler.enable`** —— 翻转 `leader=true`，并立即把内存里**最新的图**下发一次配置：
   [internal/controller/handler.go:218-224](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L218-L224)。这一步是「热备」能无缝接管的根本原因——新 leader 早已在内存里维护着最新图，当选瞬间直接下发，不需要冷启动。

> 这里有个极易踩的坑：**`eventHandler` 本身是用 `LeaderOrNonLeader` 注册的（所有副本都跑）**，但它内部「要不要下发配置」被 `enable()` 翻转的 `leader` 标志位控制。也就是说，「Runnable 在所有副本跑」和「它的写副作用只在 leader 生效」是两件可以同时成立的事——这正是 `CallFunctionsAfterBecameLeader` 存在的意义。

#### 4.2.4 代码实践

> **实践目标**：把 `manager.go` 里所有 `mgr.Add(...)` 调用分类成「所有副本跑」还是「只在 leader 跑」，并给出判断依据。

**操作步骤（源码阅读型）：**

1. 在 `internal/controller/manager.go` 全文搜索 `mgr.Add(`，记录每一处的包装器类型。目前已知的几处：

   | 位置 | 被包装对象 | 包装器 | 在哪些副本跑 |
   |---|---|---|---|
   | [L264-L266](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L264-L266) | `eventLoop`（事件循环） | `LeaderOrNonLeader` | 所有副本 |
   | [L268-L274](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L268-L274) | `CallFunctionsAfterBecameLeader` | （自身 `Leader`） | 仅 leader |
   | [L336-L338](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L336-L338) | `grpcServer`（控制面↔数据面 gRPC） | `LeaderOrNonLeader` | 所有副本 |
   | [L384-L386](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L384-L386) | `provLoop`（provisioner 事件循环） | `LeaderOrNonLeader` | 所有副本 |
   | [L460-L462](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L460-L462) | telemetry `job`（见 4.3） | `Leader` | 仅 leader |

2. 对每一处问自己一个问题：**「如果这个任务在 non-leader 副本上不跑，会出什么问题？」** 以此反推它为什么选这个包装器。

**需要观察的现象 / 预期结果：**

- `eventLoop`、`grpcServer`、`provLoop` 都用 `LeaderOrNonLeader`，因为它们和**本副本自己的数据面 Pod** 绑定——每个控制面副本都要为自己名下的 NGINX Pod 起 gRPC 服务、处理发往这些 Pod 的事件。如果只在 leader 跑，non-leader 名下的数据面 Pod 就没人管了。
- telemetry 用 `Leader`：上报是集群维度的统计，多副本各报一份会重复计数。

> 注：以上分类基于当前 HEAD 的源码；如果你在本讲之后追加新的 `mgr.Add(...)`，请同步更新本表。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `grpcServer` 必须用 `LeaderOrNonLeader`，而不能用 `Leader`？

> **参考答案**：每个控制面副本都有一组自己负责下发配置的数据面 NGINX Pod，这些 Pod 会连到「自己副本」的 gRPC server 拉配置。如果 gRPC server 只在 leader 跑，那么 non-leader 副本名下的数据面 Pod 将无处连接、拿不到配置。所以它必须所有副本都跑。

**练习 2**：`eventHandler` 在所有副本都跑（`LeaderOrNonLeader`），但它写的副作用（下发配置）只在 leader 生效。这两件事为什么不矛盾？

> **参考答案**：「Runnable 在哪跑」决定的是**进程是否有这个 goroutine**；「副作用是否生效」由 goroutine 内部的 `leader` 标志位决定。non-leader 副本也跑 `eventHandler`、也构建图（这是热备的前提），但 `leader=false` 时 `sendNginxConfig` 这条写路径被门控住。当选 leader 时 `enable()` 把 `leader` 翻成 `true` 并补发一次。两件事分别管「计算」和「写入」，互不矛盾。

**练习 3**：如果你要新增一个「每天清理一次过期缓存」的任务，且清理动作会删集群里的对象，应该用哪种包装器？为什么？

> **参考答案**：应该用 `Leader`（或等价的「仅 leader」语义）。因为删除对象是改变集群状态的写动作，多副本同时清理会重复删除甚至互相干扰；交给唯一 leader 执行即可保证「单写」。若它需要周期触发，再在 `Leader` 里包一个 `CronJob`（见 4.3 telemetry 的写法）。

---

### 4.3 CronJob 周期任务

#### 4.3.1 概念说明

很多任务不是「事件来了才做」，而是「每隔一段时间做一次」，例如产品遥测（telemetry）上报：NGF 每隔固定周期把集群和产品信息汇总上报一次。这类**周期性任务**需要一个能「按周期反复调用某个函数」的执行器。

NGF 没有引入新的调度框架，而是基于 Kubernetes 自己的 `k8s.io/apimachinery/pkg/util/wait` 工具包封装了一个极简的 `CronJob`（注意：它和 Kubernetes 的 `CronJob` 资源**毫无关系**，只是借用了「周期任务」这个名字）。它只做三件事：

1. **等就绪**：启动后先阻塞在一个 `ReadyCh` 通道上，等外部通知「可以开始了」再进入周期循环。
2. **周期执行**：用 `wait.JitterUntilWithContext` 反复调用 worker 函数。
3. **抖动（jitter）**：每次执行前给周期加上一点随机抖动，避免多个实例/多个任务在同一时刻齐刷刷地触发。

#### 4.3.2 核心流程

`CronJob.Start` 的执行过程：

```text
Start(ctx):
  1) select:
       <-ReadyCh      # 等就绪门打开（外部 close(ReadyCh)）
       <-ctx.Done()   # 或者 ctx 被取消 → 直接返回 ctx.Err()
  2) 日志：Starting cronjob
  3) wait.JitterUntilWithContext(ctx, worker, Period, JitterFactor, sliding=true)
       └─ 循环：计算抖动后的等待时长 → sleep → 调 worker → （sliding=true：在 worker 返回后重新计时）
  4) ctx 取消后循环退出 → 日志：Stopping cronjob → 返回
```

**关于抖动的数学**。`wait` 包里抖动的语义是「在原周期基础上**加**一段不超过 `JitterFactor × Period` 的随机增量」（注意是只加不减）。设原周期为 \(T\)、抖动因子为 \(J\)，则实际间隔：

\[
T_{actual} \in \big[\,T,\ \; T\cdot(1+J)\,\big]
\]

对 telemetry，源码注释给出了具体取值——周期默认 24 小时，希望抖动上限是 10 分钟：

[internal/controller/manager.go:1225-1227](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L1225-L1227) —— 常量 `telemetryJitterFactor = 10.0 / (24 * 60)`。

代入：\(J = \dfrac{10}{24\times 60} \approx 0.0069\)，于是抖动上限 \(= J\times T = 0.0069 \times 24\text{h} \approx 10\text{min}\)，与注释一致。这样多个 NGF 集群的 telemetry 上报不会精确地在同一秒打到接收端，起到「削峰」作用。

**关于 `sliding`**。`sliding=true` 表示「周期在 worker 返回**之后**才开始计时」——也就是说两次 worker 调用之间的**间隔**至少是 `Period`（不含 worker 自身耗时）。若 `sliding=false`，则计时包含 worker 耗时，worker 慢时相邻两次开始时间可能被压缩。telemetry 选 `sliding=true`，保证两次上报之间留足间隔。

#### 4.3.3 源码精读

`CronJobConfig` 把可配置项捏在一个结构体里：

[internal/framework/runnables/cronjob.go:12-25](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/cronjob.go#L12-L25) —— `Worker`（每次迭代跑的函数）、`ReadyCh`（就绪门，关闭后才开始）、`Logger`、`Period`、`JitterFactor`（非正则不抖动）。

`Start` 实现是本模块的灵魂：

[internal/framework/runnables/cronjob.go:41-57](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/cronjob.go#L41-L57) —— 先 `select` 等 `ReadyCh` 或 `ctx.Done()`；通过后用 `wait.JitterUntilWithContext` 进入周期循环，`sliding=true`。

`ReadyCh` 这个设计非常关键，它把「周期任务何时开始第一次执行」的决定权交给了**外部**。对 telemetry，这个 `ReadyCh` 来自健康检查器的就绪通道：

[internal/controller/manager.go:455-462](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L455-L462) —— `createTelemetryJob(cfg, dataCollector, healthChecker.getReadyCh())` 把就绪通道传给 CronJob，再用 `mgr.Add(job)` 注册。

而这个「就绪」是什么时候触发的？结合 [u3-l1/u3-l2](u3-l2-controller-runtime-manager-and-cache.md) 讲过的健康检查设计：`graphBuiltHealthChecker` 在**首次图构建完成**时才关闭 `ReadyCh`（readyz 也是这时才就绪）。于是 telemetry 的第一次上报**一定发生在首张图建好之后**——否则上报的会是空数据或不准确的集群快照。

最后看 telemetry 任务如何把 `CronJob` 和 `Leader` 两个机制叠在一起：

[internal/controller/manager.go:1263-1273](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L1263-L1273) —— 外层 `&runnables.Leader{...}` 保证「只在 leader 上跑」，内层 `runnables.NewCronJob(...)` 保证「周期性 + 抖动 + 等就绪」。两层组合的语义就是：**只有 leader 副本、等首张图就绪后、每隔约 24h（±10min）上报一次 telemetry。**

这正是三个最小模块的「合体」：Leader 选举（只 leader 跑）+ Runnable 包装（`Leader` 套 `CronJob`）+ CronJob 周期任务（抖动周期 + 就绪门）。

#### 4.3.4 代码实践

> **实践目标**：通过阅读 + 运行单元测试，验证 CronJob 的「周期执行」与「context 取消」两种行为，并理解测试断言。

**操作步骤：**

1. 阅读测试 [internal/framework/runnables/cronjob_test.go:12-51](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/cronjob_test.go#L12-L51)。注意测试如何用 1ms 的极短 `Period` 配合 10s 超时，并用 `valCh` 断言「worker 被调用 ≥2 次」（证明它不是只跑一次就退出），再 `cancel()` 后断言 `Start` 返回 `nil`。
2. 运行本包的全部单元测试（见 [u1-l3](u1-l3-build-and-run.md) 的测试入口）：

   ```bash
   go test ./internal/framework/runnables/...
   ```

3. 再运行三个包装器的测试，确认 `NeedLeaderElection()` 的返回值与 `enableFunctions` 被调用：

   ```bash
   go test ./internal/framework/runnables/ -run 'TestLeader|TestLeaderOrNonLeader|TestCallFunctionsAfterBecameLeader' -v
   ```

**需要观察的现象 / 预期结果：**

- `TestCronJob` 通过：worker 被调用多次，`valCh` 收到递增值。
- `TestCronJob_ContextCanceled` 通过：未关闭 `ReadyCh` 时直接 `cancel()`，`Start` 返回 `context.Canceled`（对应 `select` 命中 `<-ctx.Done()` 分支）。
- 三个包装器测试通过：`Leader.NeedLeaderElection()` 为 `true`、`LeaderOrNonLeader` 为 `false`、`CallFunctionsAfterBecameLeader.Start` 后两个标志位都被置 `true`。

> 注：若当前环境没有配置 Go 工具链或模块缓存，上述命令可能无法执行，此时请以「阅读测试断言理解行为」为主，运行结果标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果 `CronJob` 启动时 `ReadyCh` 永远不被关闭、`ctx` 也不取消，会发生什么？

> **参考答案**：`Start` 里的 `select` 会一直阻塞在 `<-j.cfg.ReadyCh` 分支上，worker 一次都不会被调用。这就是 `ReadyCh` 作为「就绪门」的作用——它能把周期任务挡在门外，直到外部条件（如首张图就绪）满足。若 `ctx` 被取消，则命中另一分支返回 `ctx.Err()`。

**练习 2**：把 telemetry 的 `telemetryJitterFactor` 改成 `0`（不抖动），会有什么潜在问题？

> **参考答案**：所有开启了 telemetry 的 NGF 集群都会精确地在「启动后满 24h 的整数倍」时刻上报，上报请求会在同一秒集中打到接收端，形成周期性尖峰（thundering herd）。抖动的目的就是把上报时刻在 24h~24h10min 内随机摊开，削峰填谷。

**练习 3**：为什么 telemetry 用 `Leader` 包装 `CronJob`，而不是直接 `mgr.Add(NewCronJob(...))`？

> **参考答案**：直接注册的 `CronJob` 只实现了 `manager.Runnable`，没有 `NeedLeaderElection()`，Manager 会把它当成「所有副本都跑」。于是多副本会各自周期上报，造成重复计数。套上 `Leader` 后 `NeedLeaderElection()` 返回 `true`，只有 leader 副本执行上报，保证「集群维度只报一份」。

---

## 5. 综合实践

把三个模块串起来，完成一个**「NGF 写动作归属判定」**的小任务。

**背景**：你刚加入 NGF 团队，新人很容易误把一个写动作注册成 `LeaderOrNonLeader`，导致多副本重复写集群。请你建立一张「写动作归属表」，作为团队的速查手册。

**任务步骤：**

1. 列出 NGF 中所有「会改变集群状态」的动作，至少包括：
   - 下发 NGINX 配置给数据面（`eventHandler.sendNginxConfig`）
   - 写 Gateway/HTTPRoute 等资源的 status（`groupStatusUpdater`）
   - 创建/回收数据面 NGINX Deployment（`nginxProvisioner`）
   - 上报 telemetry（telemetry worker）

2. 对每个动作，判定它**实际**只在 leader 生效还是所有副本都生效，并说明判定依据（引用本讲的具体源码行）。注意区分两种「只在 leader」的实现方式：
   - **方式 A**：整个 Runnable 用 `Leader`/`CallFunctionsAfterBecameLeader` 包装（telemetry 属此）。
   - **方式 B**：Runnable 在所有副本跑，但内部写路径被 `enable()` 翻转的 `leader` 标志位门控（`sendNginxConfig`、status、provisioner 属此）。

3. 给出一张结论表，形如：

   | 写动作 | 实现方式 | 仅 leader? | 依据 |
   |---|---|---|---|
   | 下发 NGINX 配置 | 方式 B | 是 | `handler.go` `enable()` 翻转 `leader` 后才 `sendNginxConfig` |
   | 写 status | 方式 B | 是 | `leader_aware_group_updater.go` `Enable()` 冲刷缓冲 |
   | 创建/回收数据面 | 方式 B | 是 | `provisioner.go` `Enable()` 翻转 `leader` |
   | telemetry 上报 | 方式 A | 是 | `manager.go` L1263 `&runnables.Leader{...}` |

4. **思考题（写在表后）**：如果有人新加了一个「周期性地把 metrics 快照写到某个 ConfigMap」的功能，按你的归属表，它应该用方式 A 还是方式 B？给出设计建议。

> **参考思路**：写 ConfigMap 是集群维度的写动作，应保证「单写」。最省事且语义最清晰的是**方式 A**：`&runnables.Leader{Runnable: runnables.NewCronJob(...)}`，让 leader 副本周期性地写。若该写动作必须寄生在某个所有副本都跑的 Runnable 内部，才考虑方式 B + Enable 门控。优先选 A，因为它把 leader 语义显式表达在装配处，不容易出错。

---

## 6. 本讲小结

- **Leader 选举**解决多副本「单写」问题：多个副本抢同一把 Lease 锁（`LeaderElectionID`/`LockName`），只有 leader 执行改变集群状态的动作；`LeaderElectionReleaseOnCancel: false` 防止周期任务在副本切换时被重叠执行。
- **Runnable 包装器**是 NGF 表达 leader 语义的核心机制：`Leader`（只 leader 跑）、`LeaderOrNonLeader`（所有副本跑）、`CallFunctionsAfterBecameLeader`（当选 leader 时执行一次一组开关函数）。三者本质都是给底层 Runnable 贴上 `NeedLeaderElection()` 标签。
- **三把写开关**（`groupStatusUpdater.Enable` / `nginxProvisioner.Enable` / `eventHandler.enable`）经 `CallFunctionsAfterBecameLeader` 在当选瞬间翻开，使 non-leader 副本「只读热备」、leader 副本「才真写」——这是 NGF 多副本行为的关键。
- **CronJob** 用 `wait.JitterUntilWithContext` 实现周期任务，三要素：`ReadyCh`（就绪门，首张图建好才首次执行）、`JitterFactor`（只加不减的随机抖动，telemetry 取 \(10/(24\times60)\approx0.0069\)，即 ±10min）、`sliding=true`（间隔不含 worker 耗时）。
- **telemetry** 是三模块的合体范例：`Leader`（只 leader）套 `CronJob`（周期 + 抖动 + 等就绪），实现「仅 leader、等首图就绪后、每 ~24h 上报一次」。
- 判定一个任务该用哪种包装器的通用问法：「如果它在 non-leader 上跑/不跑，分别会出什么问题？」——和本副本数据面 Pod 绑定的用 `LeaderOrNonLeader`，集群维度写/统计的用 `Leader`。

---

## 7. 下一步学习建议

本讲讲清了「谁在 leader 上跑」。接下来沿着数据流往下走：

- **事件如何在所有副本上被捕获、批处理**：进入 [u4-l1 自研框架：控制器抽象与过滤机制](u4-l1-framework-controller-and-filtering.md) 与 [u4-l2 事件循环与批处理](u4-l2-event-loop-and-batching.md)，看 `eventLoop`（本讲 L264 注册的 `LeaderOrNonLeader`）内部如何用双缓冲把多次变更合并成一次 NGINX 配置更新。
- **状态如何被异步写回**：进入 [u8-l1 Conditions 体系与状态更新](u8-l1-conditions-and-status-updates.md)，看 `status.Queue` 与 `LeaderAwareGroupUpdater`（本讲的 `groupStatusUpdater`）如何把「当选后才允许写」落地为具体的状态写入流程。
- **可靠性全局视角**：到 [u13-l2 并发、性能与可靠性设计](u13-l2-concurrency-and-performance.md) 会把本讲的 leader 选举、事件双缓冲、健康检查重新串成一张可靠性大图，届时可以回头检验本讲建立的归属表是否仍然成立。
