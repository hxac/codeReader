# 讲义 u8-l1：Conditions 体系与状态更新

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清 Gateway API **Conditions（条件）**这套状态语言的四元组结构（Type / Status / Reason / Message），以及 NGF 如何在代码里集中定义它。
2. 理解 NGF 控制面为什么把「生成配置」和「写状态」**异步解耦**：`status.Queue` 作为多生产者/单消费者的缓冲，`Updater` 用乐观锁 + 指数退避重试把状态写回 Kubernetes。
3. 掌握 `GroupUpdater` 的「分组覆盖」语义，以及 `LeaderAwareGroupUpdater` 如何在非 leader 副本上**暂存状态、当选后回放**，实现 u3-l1/u3-l4 提到的「热备 + 单写」多副本策略。

本讲是「状态、条件与策略 CRD」单元（u8）的第一篇，承接 u4-l3 的 `eventHandler` 编排：u4-l3 讲到 handler 在「下发配置后入队状态」，本讲就回答「入的是什么队、谁来消费、怎么落盘、多副本下谁能写」。

## 2. 前置知识

- **Kubernetes 状态（status subresource）**：一个 CRD 通常分 `spec`（用户期望）和 `status`（控制器汇报的现实）。`status` 是独立子资源，只能用 `Status().Update()` 写，不能和 `spec` 混着改。这是乐观并发（optimistic concurrency）的隔离边界。
- **`metav1.Condition`**：Kubernetes 标准的状态汇报单元，含 `Type`（什么维度，如 `Accepted`）、`Status`（`True`/`False`/`Unknown`）、`Reason`（机器可读原因，驼峰短串）、`Message`（人读说明）、`ObservedGeneration`（看到的是第几代 spec）、`LastTransitionTime`（上次变化时间）。
- **Gateway API 的 Conditions 约定**：上游 Gateway API 为每类资源规定了「必备条件类型」。例如 `GatewayClass` 必须报 `Accepted`；`Gateway` 必须报 `Accepted` 和 `Programmed`；`HTTPRoute` 必须报 `Accepted` 和 `ResolvedRefs`；每条 `Route` 还要为每个 `parentRef` 报一组条件。控制器往 `status` 里写的就是这些条件的集合。
- **leader 选举（复习 u3-l4）**：多副本 NGF 抢同一把 Lease 锁，只有 leader 执行改集群的动作；非 leader 副本照常 watch、构建图做热备。
- **u4-l3 的关键结论**：handler 用 `statusQueue` 把「写状态」与「下发/计算」解耦，用 `NginxConfigPushed` 区分「真的下发过配置」与「纯状态变化」（如 WAF 轮询结果）。本讲把这条队列彻底拆开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `internal/controller/state/conditions/conditions.go` | Conditions 的**唯一来源字典**：定义所有 Type/Reason 常量、`Condition` 结构，以及一大批 `NewXxx()` 工厂函数。图构建阶段（u5）产生的条件几乎都来自这里。 |
| `internal/controller/status/updater.go` | 把 `[]UpdateRequest` 写回 Kubernetes 的执行器：乐观锁读取最新版本 → 用 `Setter` 改 status → `Status().Update()`，失败用指数退避重试。 |
| `internal/controller/status/queue.go` | `status.Queue`：无界 FIFO，多生产者 `Enqueue`、单消费者 `Dequeue` 阻塞读取。是配置链路与状态链路之间的解耦缓冲。 |
| `internal/controller/status/leader_aware_group_updater.go` | `LeaderAwareGroupUpdater`：包一层 `Updater`，未当选时按组暂存请求，当选后回放并切换为直通；保证多副本只有 leader 真正落盘。 |

辅助文件（本讲会点到）：`internal/controller/status/status_setters.go`（各类资源的 `Setter` 实现）、`internal/controller/status/conditions.go`（`ConditionsEqual` 比较）、`internal/controller/status/prepare_requests.go`（图 → `[]UpdateRequest`）、`internal/controller/handler.go`（队列的生产者与消费者）、`internal/controller/manager.go`（装配）。

## 4. 核心概念与源码讲解

### 4.1 Conditions 集合：状态语言的「词典与造句法」

#### 4.1.1 概念说明

Gateway API 不让控制器往 `status` 里随便塞字段，而是规定了一套**条件语言**：每个状态结论都表达成「某个 Type 处于 True/False/Unknown，原因是某个 Reason，附带一句人读的 Message」。

NGF 把这套语言做成了两层：

- **词典层**：`internal/controller/state/conditions/conditions.go` 集中定义所有 `Type`、`Reason` 常量，并提供一个内部用的 `Condition` 结构。
- **造句层**：一大堆 `NewXxx() Condition` / `NewXxx() []Condition` 工厂函数，每对应一种错误或正常场景「造一句话」。图构建（u5）在各节点上调用这些工厂，把结论沉淀进节点的 `Conditions` 字段（这正是 u5-l1 讲的「节点三元组 `Source + Valid + Conditions`」里的那个 `Conditions`）。

关键设计：**条件不是「报错」，而是「状态沉淀」**。u5-l1 已经讲过，NGF 校验失败不靠抛 `error`，而是把失败原因写进节点的 `Conditions`，再由一个 `Valid` 布尔位决定该节点是否参与配置生成。本讲关注的是这些条件最终如何变成资源 `status` 里的 `metav1.Condition`。

#### 4.1.2 核心流程

一个条件从「被造出来」到「写进 status」要经历三步：

1. **造句**：图构建阶段调用 `conditions.NewXxx(...)` 得到一个或多个 `conditions.Condition`。
2. **合并去重**：`conditions.DeduplicateConditions` 按 `Type` 去重——**同 Type 后写者覆盖先写者**。惯用法是「先放默认（happy-path）条件，再放具体条件」，于是具体条件自然覆盖默认条件。
3. **转换**：`conditions.ConvertConditions` 把内部 `Condition` 翻译成 Kubernetes 标准 `metav1.Condition`，补上 `ObservedGeneration` 与 `LastTransitionTime`，最终被 `Setter` 写进资源 `status`。

伪代码：

```
allConds := []Condition{}
allConds = append(allConds, NewDefaultRouteConditions()...)   # Accepted=True, ResolvedRefs=True
allConds = append(allConds, route.Conditions...)              # 例如 ResolvedRefs=False（BackendNotFound）
conds := DeduplicateConditions(allConds)                       # 后者覆盖前者的 ResolvedRefs
apiConds := ConvertConditions(conds, generation, now)          # -> []metav1.Condition
```

#### 4.1.3 源码精读

**`Condition` 结构——四元组的内部表示**：

[internal/controller/state/conditions/conditions.go:231-237](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L231-L237) 定义了内部用的 `Condition`。注意它**没有** `ObservedGeneration` / `LastTransitionTime`——这两个字段是「写盘那一刻」才知道的（要看资源当前 generation 和当前时间），所以留到 `ConvertConditions` 里再补。

**`DeduplicateConditions`——按 Type 去重，后写者赢**：

[internal/controller/state/conditions/conditions.go:241-269](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L241-L269) 从**末尾**倒序扫描，第一次遇到的 `Type` 才保留，从而保证「同 Type 后写者覆盖先写者」，同时保持原顺序。这个语义是「默认条件先放、具体条件后放」这套惯用法的基石：只要具体条件的 `Type` 与默认条件相同，它就会赢。

**`ConvertConditions`——补上时间与 generation，翻译成标准条件**：

[internal/controller/state/conditions/conditions.go:272-291](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L272-L291) 把每个 `Condition` 转成 `metav1.Condition`，统一注入 `ObservedGeneration`（调用方传入资源当前 generation）和 `LastTransitionTime`（调用方传入统一时间戳）。`ObservedGeneration` 很重要——它告诉用户「这个 status 是看到第几代 spec 之后算出来的」，若落后于资源 `.metadata.generation`，说明控制器还没处理最新改动。

**默认条件集合——每类资源的「必备 happy-path」**：

| 资源 | 默认条件工厂 | 行号 |
| --- | --- | --- |
| `GatewayClass` | `NewDefaultGatewayClassConditions()`：`Accepted=True` + `SupportedVersion=True` | [L308-L323](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L308-L323) |
| `Gateway` | `NewDefaultGatewayConditions()`：`Accepted=True` + `Programmed=True` | [L968-L974](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L968-L974) |
| `Route` | `NewDefaultRouteConditions()`：`Accepted=True` + `ResolvedRefs=True` | [L399-L405](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L399-L405) |
| `Listener` | `NewDefaultListenerConditions()`：`Programmed` + `Accepted` + `ResolvedRefs` + `NoConflicts`（后两者视已有冲突条件而定） | [L642-L659](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L642-L659) |

注意 `NewDefaultListenerConditions(existingConditions)` 带参数：如果已有 `Conflicted` 或 `OverlappingTLSConfig` 等冲突类条件，就**不**再补 `NoConflicts`，避免「既说有冲突又说没冲突」的自相矛盾（见同文件 `hasConflictConditions`，[L671-L680](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L671-L680)）。

**错误条件工厂举例——把错误「造句」成条件**：

后端找不到时，`ResolvedRefs=False / Reason=BackendNotFound`，见 [internal/controller/state/conditions/conditions.go:540-549](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L540-L549)（`NewRouteBackendRefRefBackendNotFound`）。这与 u5-l3 讲的「后端解析失败错误一律沉淀为 `ResolvedRefs=False` 配不同 Reason」一一对应：

| 场景 | Type | Reason | 工厂 |
| --- | --- | --- | --- |
| 后端 Service 不存在 | `ResolvedRefs` | `BackendNotFound` | `NewRouteBackendRefRefBackendNotFound` |
| 跨命名空间引用未被 ReferenceGrant 授权 | `ResolvedRefs` | `RefNotPermitted` | `NewRouteBackendRefRefNotPermitted` |
| backendRef kind 非法 | `ResolvedRefs` | `InvalidKind` | `NewRouteBackendRefInvalidKind` |
| Route 没被任何 Listener 接纳 | `Accepted` | `NotAllowedByListeners` | `NewRouteNotAllowedByListeners` |

> 关键洞察：`Type` 表达「考察维度」，`Reason` 表达「在该维度下的具体原因」。同一 `Type` 在 `status` 里只出现一行（靠 `DeduplicateConditions` 保证），不同失败原因靠 `Reason` 区分。

#### 4.1.4 代码实践（源码阅读型）

**目标**：验证「默认条件先放、具体条件后放、去重后写者赢」这条约定。

**操作步骤**：

1. 打开 [internal/controller/status/prepare_requests.go:165-220](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/prepare_requests.go#L165-L220)（`prepareRouteStatus`）。
2. 阅读这段：它先 `append` `defaultConds`（`Accepted=True, ResolvedRefs=True`），再 `append` `conds`（图里算出的，可能含 `ResolvedRefs=False`），再 `append` 附加失败条件，最后 `DeduplicateConditions`。
3. 在脑中跑一组数据：假设 `conds` 里有一条 `ResolvedRefs=False / BackendNotFound`，默认的 `ResolvedRefs=True` 会被覆盖，最终 `status` 里 `ResolvedRefs` 那一行是 `False / BackendNotFound`。

**需要观察的现象**：去重后，每个 `Type` 只剩一条，且是「最后写入的那条」。

**预期结果**：你能口述出「为什么先放默认条件是安全的」——因为默认条件永远会被同 Type 的具体条件覆盖，不存在「报了 happy 又报了 sad」的矛盾。

> 本实践为源码阅读型，不涉及运行；若要运行，可参考本讲 4.2.4 的端到端实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `conditions.Condition` 内部结构里没有 `ObservedGeneration` 和 `LastTransitionTime`，而 `metav1.Condition` 里有？

**参考答案**：这两个字段依赖「写盘瞬间」的外部信息——`ObservedGeneration` 取决于资源当前的 `.metadata.generation`，`LastTransitionTime` 取决于当前时间。图构建阶段（造条件时）既不读资源最新 generation、也不该决定时间戳，所以把这两个字段推迟到 `ConvertConditions`（写盘前一步）统一注入，避免图构建逻辑耦合时间与版本语义。

**练习 2**：假设一个 `HTTPRoute` 同时被两个 Listener 接纳，其中一个 Listener 无效。`prepareRouteStatus` 会为它生成几条 `RouteParentStatus`？每条的 `Accepted` 分别是什么？

**参考答案**：会为每个去重后的 `parentRef`（按 `Idx` 去重，见 `removeDuplicateIndexParentRefs`）生成一条。有效 Listener 那条 `Accepted=True`；无效 Listener 那条会被附上 `FailedConditions`（如 `Accepted=False / InvalidListener`）。每条 `RouteParentStatus` 携带各自的 `Conditions`，互不影响——这正是 Gateway API「per-parent」条件的语义。

---

### 4.2 状态队列与 GroupUpdater：把写状态从主链路里剥离

#### 4.2.1 概念说明

回顾 u4-l3：handler 每处理完一批事件，要给「几乎所有资源」回写 status。问题在于——**写 Kubernetes status 是慢操作**：

- 一次可能要更新成百上千个资源（一个 Gateway 下挂的 Route 可能上千条）。
- API server 可能变慢甚至超时，每个 `Status().Update()` 都可能拖很久。

如果把写状态直接做在事件循环里，慢状态写入就会**反噬事件循环的吞吐**（变长每批处理耗时，进而拖慢配置下发频率）。NGF 的解法是**生产者/消费者解耦**：

- 配置链路只负责「把要写的状态打包成一个 `QueueObject`，丢进队列」（`Enqueue`，极快）。
- 一个常驻 goroutine `waitForStatusUpdates` 单线程消费队列，慢吞吞地把状态一个个写回。

这样事件循环永远不会被状态写入阻塞。本模块讲清这条链路的三个角色：`Queue`（管道）、`Updater`（写盘执行器）、`GroupUpdater`（分组覆盖语义）。

#### 4.2.2 核心流程

```
                       (多生产者)                                    (单消费者)
handler/agent/provisioner/waf  ──Enqueue(QueueObject)──>  status.Queue  ──Dequeue──>  waitForStatusUpdates goroutine
                                                                                            │
                                                            按组调用 statusUpdater.UpdateGroup( )
                                                                            │
                                                              GroupUpdater.UpdateGroup(ctx, name, reqs...)
                                                                            │
                                                              (leader 已 Enable 时) Updater.Update(reqs...)
                                                                            │
                                                   对每个 req: Get 最新 → Setter 改 status → Status().Update()
                                                                            │ 指数退避重试（冲突/失败）
```

几个关键点：

1. **`QueueObject` 是「写什么」的命令**：携带 `UpdateType`（`UpdateAll` 全量 / `UpdateGateway` 只更 Gateway）、可能的 `Error`（配置下发失败要把错误透传到 status）、`Deployment`（定位是哪个数据面/Gateway）、`NginxConfigPushed`（区分真下发与纯状态变化）。
2. **`Setter` 是幂等的差分函数**：拿到资源最新对象，判断要不要改、改成什么样；若新旧 status 实质相同就返回 `wasSet=false`，跳过这次 `Update` 调用，省一次 API 写。
3. **乐观锁重试**：每次写前先 `Get` 最新版本（带 resourceVersion），`Setter` 改完再 `Update`；若期间被别人改过（冲突），`wait.ExponentialBackoffWithContext` 会重试。
4. **`GroupUpdater` 的分组**：把请求按 `name` 分组（`groupGateways` / `groupAllExceptGateways` / `groupControlPlane`），同一组的后一次调用**整体覆盖**前一次——保证「以最新图为准」，不会新旧 status 混杂。

#### 4.2.3 源码精读

**(1) 队列对象与队列**

[internal/controller/status/queue.go:11-38](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/queue.go#L11-L38) 定义了 `UpdateType`（`UpdateAll` / `UpdateGateway`）和 `QueueObject`。`NginxConfigPushed` 字段的注释明确：当为 `false` 时表示这是一次「纯状态变化」（如 WAF 轮询结果），应抑制「NGINX 配置成功更新」日志——这正是 u4-l3 提到的契约。

[internal/controller/status/queue.go:40-90](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/queue.go#L40-L90) 是 `Queue` 本体：

- `Enqueue`（[L56-L67](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/queue.go#L56-L67)）：加锁追加 item，再向容量为 1 的 `notifyCh` 非阻塞发一个信号。`select { case ch<-: default: }` 保证「已经有未消费信号就不重复发」，避免信号堆积。
- `Dequeue`（[L69-L90](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/queue.go#L69-L90)）：空队列时**释放锁**并阻塞在 `notifyCh`/`ctx.Done()` 上，被唤醒后重新拿锁；取出队首并返回。注意它把 `items` 切片向前挪一位（`items[1:]`），是典型的 FIFO。

> 这是一个「无界队列 + 容量 1 信号」的经典写法：数据放切片、唤醒放 channel，互不耦合。

**(2) 生产者：handler 何时 Enqueue**

[internal/controller/handler.go:237-266](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L237-L266) 是几处典型的入队点：当没有 Gateway（只更 GatewayClass status）、或 Gateway 无效/无 Listener 时，构造一个 `QueueObject{UpdateType: UpdateAll, ...}` 入队。`Error` 字段则在 WAF bundle 未就绪等 fail-closed 场景被填上（见 [handler.go:273-282](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L273-L282)）。除了 handler，`provisioner`、`agent`（配置下发结果）、`wafPoller` 都是生产者——这正是「多生产者」。

**(3) 消费者：waitForStatusUpdates**

[internal/controller/handler.go:531-611](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L531-L611) 是常驻消费循环。关键逻辑：

- 每次取出一个 `QueueObject`，从 `processor.GetLatestGraph()` 拿到最新图（注意：消费的是**最新图**，不是入队那一刻的图——这样即便队列里有积压，最终写的也是最新状态）。
- 根据 `item.NginxConfigPushed` / `item.Error` 决定是否记录 reload 结果到 `gw.LatestReloadResult`（纯状态变化不清掉先前的 reload 错误）。
- 按 `UpdateType` 分派：`UpdateAll` 调 `updateStatuses`（全量）；`UpdateGateway` 只更单个 Gateway（用于公网 IP 变化等场景，不必刷新所有资源）。

**(4) 分组：为什么 Gateway 单独一组**

[internal/controller/handler.go:613-726](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L613-L726) 的 `updateStatuses` 最后分两次调用：

```go
h.cfg.statusUpdater.UpdateGroup(ctx, groupAllExceptGateways, reqs...)  // GC + Routes + Policies + ...
h.cfg.statusUpdater.UpdateGroup(ctx, groupGateways, gwReqs...)         // 单独 Gateway
```

注释（[L717-L718](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L717-L718)）点明原因：Gateway status 含公网 IP 地址，希望「公网 IP 一变就单独刷 Gateway」，而不必把其他几百个资源也跟着刷一遍。组名常量定义在 [handler.go:105-109](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L105-L109)。

**(5) GroupUpdater 接口**

[internal/controller/status/leader_aware_group_updater.go:9-17](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/leader_aware_group_updater.go#L9-L17) 定义 `GroupUpdater` 接口，签名 `UpdateGroup(ctx, name string, reqs ...UpdateRequest)`。注释说明：之所以抽成接口，是为了能用 `counterfeiter` 生成 fake，供 `handler_test.go` 使用（避免 import 循环）。真正的实现是 `LeaderAwareGroupUpdater`，下一节细讲。

**(6) Updater：写盘执行器**

[internal/controller/status/updater.go:31-55](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/updater.go#L31-L55) 的 `Updater` 结构上方有一段重要注释，**坦白列出了三个已知局限**（每条挂着一个 GitHub issue）：

1. 它是**同步**的——状态上报会拖慢调用方（这正是本讲为何要套一层异步队列）。
2. 它不会清理「不再被 Gateway 处理的资源」的 status。
3. 若别的控制器覆盖了 NGF 写的 status，NGF 不会主动恢复，要等下一次事件触发。

> 这段注释是阅读本文件最值得停留的地方：它把「设计取舍 + 已知债」讲得很诚实。

[internal/controller/status/updater.go:67-119](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/updater.go#L67-L119) 的 `Update` 串行处理每个 `UpdateRequest`，调用 `writeStatuses`。后者核心是 `wait.ExponentialBackoffWithContext`，退避参数为：起始 200ms、倍数 2、抖动 0.5、最多 4 步、上限 3000ms（[L99-L110](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/updater.go#L99-L110)）。重试函数 `NewRetryUpdateFunc`（[L121-L188](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/updater.go#L121-L188)）的「乐观锁 + 重试」要点：

| 步骤 | 行号 | 说明 |
| --- | --- | --- |
| `getter.Get` 取最新 | L145 | 必须读最新版本，否则 `Update` 会因 `resourceVersion` 过期而冲突失败 |
| 资源已删除 | L148-L150 | `IsNotFound` 视为成功（无需再写），返回 `(true, nil)` 终止重试 |
| `statusSetter(obj)` | L163 | 调 Setter；返回 `false` 表示无变化，跳过写、终止重试 |
| `updater.Update` | L174 | 写回；失败返回 `(false, nil)`——注意返回 `nil` 而非 error，触发 `ExponentialBackoff` 重试 |
| 成功 | L186 | 返回 `(true, nil)` |

> 这里的一个反直觉点：**遇到错误要重试时，返回的是 `(false, nil)` 而不是 `(false, err)`**。因为 `wait.ExponentialBackoffWithContext` 只在函数返回 `error != nil` 时**中止**、返回 `nil` 时按条件重试。注释（[L124-L127](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/updater.go#L124-L127)）特意解释了这一点（否则 linter 会抱怨「有错误却返回 nil」）。

**(7) Setter：幂等 + 不踩别人写的 status**

`Setter` 类型 [updater.go:26-29](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/updater.go#L26-L29) 是 `func(client.Object) (wasSet bool)`。以 `newHTTPRouteStatusSetter`（[status_setters.go:86-110](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/status_setters.go#L86-L110)）为例，它做了一件关键的事：**保留属于其他控制器的 parent status**。因为一个 `HTTPRoute` 可能被多个控制器（不止 NGF）处理，NGF 只能改自己那条 `ControllerName` 下的 status，不能擦掉别人写的。它先把别家的 parent status 收集起来，再拼上自己的，最后做相等性比较决定要不要写。

相等性比较 `routeStatusEqual`（[status_setters.go:206-238](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/status_setters.go#L206-L238)）有个细节：因为别的控制器可能乱序写 parent status，**不能用 `slices.EqualFunc`**（它在乎顺序），而是双向 `ContainsFunc` 检查「互为子集」。所有比较最终落到 `ConditionsEqual`（[status/conditions.go:9-31](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/conditions.go#L9-L31)），且**有意忽略 `LastTransitionTime`**——否则每次写都因时间戳不同而被判成「有变化」，造成无意义的重复写盘。

#### 4.2.4 代码实践

**目标**：端到端观察「一个配置错误如何变成资源 status 里的 Condition」，验证整条异步链路。

**操作步骤**：

1. 在本地 kind 集群按 u1-l4/u1-l5 部署好 NGF，apply 一个 cafe-example（Gateway + HTTPRoute + 后端 Service）。
2. 故意把 HTTPRoute 的某个 `backendRefs[].name` 改成一个**不存在的 Service 名**（如 `nonexistent-svc`），apply 之。
3. 等几秒，执行：
   ```
   kubectl describe httproute <route-name> -n <ns>
   ```
   关注 `Status` 段里 `Parents[].Conditions`。

**需要观察的现象**：

- 该 HTTPRoute 的 `ResolvedRefs` 条件应为 `False`，`Reason` 为 `BackendNotFound`，`Message` 说明找不到后端。
- `Accepted` 仍可能是 `True`（Route 本身被接纳，只是某个 backendRef 解析失败）——这正是 u5-l3 讲的「即使引用非法也生成 `Valid=false` 的 BackendRef，由数据面返回 500，错误沉淀为 `ResolvedRefs=False`」。
- 多次 `kubectl describe`，`ResolvedRefs` 的 `LastTransitionTime` **不会反复变**（因为 `ConditionsEqual` 忽略时间戳，状态没实质变化就不重写）。

**预期结果**：你能在 status 里看到本讲 4.1 讲的「Type/Status/Reason/Message」四元组，且能对应到源码里 `NewRouteBackendRefRefBackendNotFound`（[conditions.go:540-549](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L540-L549)）造出的那句话。

> 若本地无法部署，可在 `internal/controller/status/updater_test.go` 中找到 `NewRetryUpdateFunc` 的单测，断言「Setter 返回 false 时不调用 Update」「Get 失败且非 NotFound 时重试」等行为，作为替代验证。

#### 4.2.5 小练习与答案

**练习 1**：`Updater` 的注释自承「同步、会拖慢调用方」，那 NGF 是怎么避免它拖慢事件循环的？

**参考答案**：NGF 没有让事件循环直接调 `Updater.Update`，而是让事件循环只做 `statusQueue.Enqueue(QueueObject)`（极快），再由独立的常驻 goroutine `waitForStatusUpdates` 单线程消费队列、调用 `GroupUpdater → Updater`。慢写盘被完全隔离在事件循环之外，最坏情况只是队列短暂积压，不会反噬配置下发频率。

**练习 2**：`NewRetryUpdateFunc` 在 `statusSetter(obj)` 返回 `false` 时直接返回 `(true, nil)` 终止重试。为什么这样是对的？

**参考答案**：`wasSet=false` 表示「新 status 与对象上已有的 status 实质相同」（经 `ConditionsEqual` 等忽略时间的比较）。既然磁盘上的 status 已经是目标值，就没有必要再调 `Update`，更不需要重试——返回 `true` 表示「条件已满足」，正常结束。

**练习 3**：为什么 `routeStatusEqual` 不直接用 `slices.EqualFunc` 比较 `Parents` 切片？

**参考答案**：一个 Route 的 parent status 可能被多个控制器写，写入顺序无法保证。`slices.EqualFunc` 是按位置逐对比较，顺序不同就会误判为「不等」，从而触发不必要的写盘。NGF 改用「双向互为子集」的 `ContainsFunc` 检查，且比较时忽略非本控制器的条目，从而对顺序不敏感。

---

### 4.3 leader 感知写入：多副本下只有 leader 落盘

#### 4.3.1 概念说明

回顾 u3-l1/u3-l4 的多副本策略：非 leader 副本也跑控制器、跑事件循环、构建图，做**热备**；但所有「改集群」的动作（建数据面资源、下发配置、写 status）默认关闭，只有当选 leader 才打开。本模块聚焦其中一把「写开关」——**status 写入开关**，由 `LeaderAwareGroupUpdater` 实现。

直接的矛盾是：

- 非 leader 副本会构建出一份完整的图，handler 的 `updateStatuses` 会照样调用 `statusUpdater.UpdateGroup(...)`。
- 但非 leader **绝不能**写 status——否则多副本会并发改同一批资源，互相覆盖、互相打架（尤其和别的控制器写的 status 混在一起更乱）。

`LeaderAwareGroupUpdater` 的解法是一个**两阶段状态机**：

- **未 Enable（非 leader）**：`UpdateGroup` 不落盘，而是把请求**按组名暂存**进 `groupReqs map[string][]UpdateRequest`。
- **Enable（当选 leader 那一刻）**：把所有暂存请求**一次性回放**写盘，然后切换为「直通模式」——之后再来的 `UpdateGroup` 直接转发给底层 `Updater` 立即写。

这保证了：新 leader 上任的瞬间，能立刻把「它作为热备期间算出的最新 status」写出去，不会因为「之前是备、没写」而出现 status 真空。

#### 4.3.2 核心流程

```
                 非 leader 阶段                              当选 leader（仅一次）
UpdateGroup(name, reqs) ──> enabled==false                    Enable()
                          groupReqs[name] = reqs   ──暂存──>  遍历 groupReqs，逐组 Updater.Update
                          (reqs 为空则 delete[name])           切 enabled=true，清空 groupReqs
                                                                    │
                 leader 阶段（直通）                              │ 仅一次，再调 panic
UpdateGroup(name, reqs) ──> enabled==true <────────────────────────┘
                          Updater.Update(ctx, reqs...)  立即落盘
```

三个不变式：

1. **同组后写覆盖先写**：暂存阶段对同一 `name` 多次调用 `UpdateGroup`，`groupReqs[name]` 只保留最后一次（覆盖）。这呼应 4.2 的「以最新图为准」。
2. **空请求 = 清除**：暂存阶段若 `reqs` 为空，则 `delete(groupReqs, name)`——表示「这个组没什么要写的」。Enable 时就不会回放它。
3. **Enable 只能调一次**：再调会 `panic`，防止重复回放导致状态混乱。

#### 4.3.3 源码精读

**结构与构造**：

[internal/controller/status/leader_aware_group_updater.go:19-37](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/leader_aware_group_updater.go#L19-L37) 定义 `LeaderAwareGroupUpdater`，内嵌底层 `*Updater`、一把 `sync.Mutex`、`groupReqs map`、`enabled bool`。文档注释（[L19-L22](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/leader_aware_group_updater.go#L19-L22)）一句话讲清两阶段语义：「Enable 前只存请求；Enable 时回放已存请求（只能 Enable 一次）；Enable 后不再存、立即写」。

**UpdateGroup——状态机的核心**：

[internal/controller/status/leader_aware_group_updater.go:39-55](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/leader_aware_group_updater.go#L39-L55)：

```go
if !u.enabled {
    if len(reqs) == 0 {
        delete(u.groupReqs, name)   // 空请求 = 清除该组
        return
    }
    u.groupReqs[name] = reqs        // 暂存（同组覆盖）
    return
}
u.updater.Update(ctx, reqs...)      // leader 直通
```

全程在 `u.lock` 保护下，所以「暂存」与「Enable 回放」不会并发踩踏。

**Enable——回放并切换**：

[internal/controller/status/leader_aware_group_updater.go:57-72](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/leader_aware_group_updater.go#L57-L72)：

```go
if u.enabled {
    panic(errors.New("LeaderAwareGroupUpdater can only be enabled once"))
}
u.enabled = true
for name, reqs := range u.groupReqs {
    u.updater.Update(ctx, reqs...)
    delete(u.groupReqs, name)
}
```

注意回放是在**持锁**状态下串行执行的——这意味着回放期间，任何新来的 `UpdateGroup` 会阻塞等锁；拿到锁时 `enabled` 已为 `true`，于是走直通分支立即写。语义上「先回放历史、再处理新增」，顺序清晰。

**装配：谁在当选 leader 时调 Enable**：

[internal/controller/manager.go:201](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L201) 创建 `groupStatusUpdater := status.NewLeaderAwareGroupUpdater(statusUpdater)`，注入 handler（[manager.go:227](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L227)）。然后 [manager.go:268-274](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L268-L274) 把它注册进 u3-l4 讲过的 `CallFunctionsAfterBecameLeader`：

```go
mgr.Add(runnables.NewCallFunctionsAfterBecameLeader([]func(context.Context){
    groupStatusUpdater.Enable,   // 本讲的「status 写开关」
    nginxProvisioner.Enable,     // 「建数据面资源」开关
    eventHandler.enable,         // 「下发配置」开关
}))
```

这三把开关一起翻开，正是 u3-l1 总结的「当选后打开写状态、建数据面、下发配置三把开关」。它们共同实现了「非 leader 热备、leader 单写」。

**测试验证语义**：

[internal/controller/status/leader_aware_group_updater_test.go:102-152](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/leader_aware_group_updater_test.go#L102-L152) 用 envtest 把上面的状态机验证得很清楚：

- 「updater is disabled」时调 `UpdateGroup`，资源 status **没变**（`testNoStatuses`）。
- 传空请求能清除某组暂存（`should clear saved requests of group2`）。
- `Enable` 后，**之前暂存的 group1** 被回放写盘，而被清除的 group2 仍无 status。
- Enable 后再 `UpdateGroup` 直通生效。
- 第二次 `Enable` 会 `Panic`（`should panic`）。

> 这是理解本模块最值得读的测试：它把「暂存 → 回放 → 直通 → 不可重入」四个不变式全列了出来。

#### 4.3.4 代码实践（源码阅读型）

**目标**：验证「非 leader 副本构建图、调 UpdateGroup，但不会真写盘」这条多副本正确性。

**操作步骤**：

1. 把 NGF 副本数调到 2（`--set nginxGateway.replicaCount=2` 或改 Deployment），部署。
2. 在 [manager.go:268-274](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L268-L274) 处理解：两个 Pod 都会跑事件循环、都构建图、都会调用 `handler.updateStatuses → statusUpdater.UpdateGroup`，但只有持有 Lease 的那个会 `Enable`。
3. `kubectl get lease -n <ngf-ns>` 看谁持锁；`kubectl logs <非-leader-pod>` 应看不到「Updating status for resource」这类日志（因为 `LeaderAwareGroupUpdater` 在 `enabled=false` 时根本没转发给 `Updater`，而 `Updater` 才打那条 V(1) 日志）。

**需要观察的现象**：只有 leader Pod 的日志里出现 `Updating status for resource ...`；非 leader Pod 即使在处理事件批次，也不会产生 status 写日志。

**预期结果**：印证「热备 + 单写」——非 leader 副本算了图、调了 `UpdateGroup`，但请求被暂存在内存 `groupReqs` 里、从未触达 API server。

> 若本地不便起多副本，可直接阅读 [leader_aware_group_updater_test.go:102-126](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/status/leader_aware_group_updater_test.go#L102-L126) 的断言作为验证：disabled 阶段 `testNoStatuses`、`Enable` 后 `testStatuses`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Enable` 必须把暂存请求**回放**写盘，而不是「Enable 后什么都不做，等下一次事件再来写」？

**参考答案**：如果 Enable 后不回放，那么当选 leader 的瞬间到「下一次资源变更触发新一轮 status 计算」之间，集群里所有资源的 status 都停留在「前任 leader 离开时的样子」，可能与当前图不一致（例如前任和现任基于不同的 GatewayClass 配置）。回放暂存请求保证新 leader 一上任就把「自己作为热备期间算出的最新 status」立刻写出去，消除 status 真空窗口。

**练习 2**：`Enable` 在持锁状态下串行回放所有组，会不会因为组很多、写盘很慢而长时间阻塞新来的 `UpdateGroup`？这种阻塞是问题吗？

**参考答案**：确实会阻塞——回放期间新 `UpdateGroup` 会等锁。但这通常不是问题：回放只发生一次（选举切换时），且被阻塞的新请求拿到锁后走的是直通分支（`enabled` 已为 `true`），仍会被及时处理；最坏只是短暂延迟。权衡的是「回放期间不会被新请求插队、保证历史 status 先于新增 status 写入」这一顺序清晰性，NGF 选择了简单正确而非极致并发。

**练习 3**：假如把 `groupStatusUpdater.Enable` 从 `CallFunctionsAfterBecameLeader` 的列表里**漏掉**，会发生什么？

**参考答案**：`LeaderAwareGroupUpdater` 永远停留在 `enabled=false`，所有 `UpdateGroup` 调用都只暂存、从不落盘。后果是：集群里所有 Gateway/HTTPRoute/Policy 等资源的 `status` 永远不被更新（`Accepted`/`Programmed` 等条件缺失或不更新），用户和上层系统无法得知 NGF 是否正常工作。这也说明 Enable 是「status 写开关」的唯一启动点，与另外两把开关（provisioner、eventHandler）并列、缺一不可。

---

## 5. 综合实践

**任务**：给定一个错误场景，判断应该给 Gateway / HTTPRoute 设置哪些 Conditions，并对照源码确认。

**场景**：用户部署了如下配置（伪 YAML，仅用于说明，非项目原有资源）：

```yaml
# Gateway 引用了一个不存在的 TLS Secret（在别的命名空间，且没有 ReferenceGrant）
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata: { name: gw, namespace: app }
spec:
  gatewayClassName: nginx
  listeners:
    - name: https
      port: 443
      protocol: HTTPS
      hostname: example.com
      tls:
        mode: Terminate
        certificateRefs:
          - kind: Secret
            name: tls-secret
            namespace: infra   # 跨命名空间，且 infra 里无 ReferenceGrant
---
# HTTPRoute 的 backendRef 指向一个不存在的 Service
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata: { name: route, namespace: app }
spec:
  parentRefs: [{ name: gw, sectionName: https }]
  hostnames: [example.com]
  rules:
    - backendRefs: [{ name: missing-svc }]
```

**请完成**：

1. **Gateway 层面**：该 HTTPS Listener 因 `certificateRefs` 跨命名空间且无 `ReferenceGrant`，应被标记为 `Accepted=False` 且 `ResolvedRefs=False`，`Reason=RefNotPermitted`，并 `Programmed=False`。请在 [conditions.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go) 中找到对应工厂（提示：`NewListenerRefNotPermitted`，[L894-L910](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L894-L910)），确认它一次性返回 `Accepted / ResolvedRefs / Programmed` 三条条件。
2. **HTTPRoute 层面**：`backendRefs` 指向不存在的 Service，应在 `ResolvedRefs=False / BackendNotFound` 沉淀（`NewRouteBackendRefRefBackendNotFound`，[L540-L549](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L540-L549)）。但注意：因为 `parentRefs.sectionName: https` 指向的那个 Listener 本身无效（上面第 1 点），Route 还可能被附上 `Accepted=False / InvalidListener`（`NewRouteInvalidListener`，[L477-L484](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L477-L484)），作为 `FailedConditions` 挂在该 parentRef 上。
3. **链路追踪**：沿 `图节点 Conditions → prepareRouteStatus/prepareGatewayRequest（去重+转换） → Setter（保留别家 status、相等性比较） → QueueObject.Enqueue → waitForStatusUpdates → GroupUpdater(leader 已 Enable) → Updater(乐观锁重试)` 这条链，口述每个条件的「造句、合并、写盘」分别在哪个文件发生。
4. **多副本验证（可选）**：若部署了 2 副本，确认只有 leader 写 status（参考 4.3.4）。

**预期结果**：你能用本讲的三模块（Conditions 集合、状态队列与 GroupUpdater、leader 感知写入）把「一个错误从被发现到变成资源 status」的完整路径讲清楚，且每一步都能指到具体源码行。

## 6. 本讲小结

- **Conditions 是状态语言**：每条结论是 `Type/Status/Reason/Message` 四元组。`conditions.go` 是唯一词典，`NewXxx()` 工厂负责造句，`DeduplicateConditions`（按 Type 后写者覆盖）与 `ConvertConditions`（补 generation/时间）负责合并与转成 `metav1.Condition`。惯用法是「默认条件先放、具体条件后放」。
- **写状态被异步剥离**：`status.Queue`（多生产者 `Enqueue` / 单消费者 `Dequeue`）把慢写盘与事件循环解耦；`Updater` 用「Get 最新 → Setter 幂等改 → Status().Update + 指数退避重试」落盘，`Setter` 返回 `wasSet` 跳过无变化写入、并保留其他控制器写的 status。
- **`GroupUpdater` 按组覆盖**：请求按 `groupGateways` / `groupAllExceptGateways` / `groupControlPlane` 分组，同组后写整体覆盖先写；Gateway 单独一组是为了「公网 IP 变化时只刷 Gateway」。
- **leader 感知写入 = `LeaderAwareGroupUpdater`**：两阶段状态机——未 Enable 时按组暂存请求、不落盘；`Enable`（仅一次，再调 panic）时回放所有暂存请求并切换为直通。它由 `CallFunctionsAfterBecameLeader` 在当选时翻开，与 `provisioner.Enable`、`eventHandler.enable` 并列，共同实现「非 leader 热备、leader 单写」。
- **诚实的设计注释**：`updater.go` 顶部坦白了「同步会拖慢、不清理失效 status、被覆盖不恢复」三个已知局限并挂了 issue，是理解取舍的最佳入口。
- **关键不变式**：条件去重保「每 Type 一行」；Setter 比较「忽略 LastTransitionTime」防无意义重写；Enable「回放后直通、不可重入」防多副本写冲突。

## 7. 下一步学习建议

- **u8-l2 自定义 CRD 与策略附着**：本讲的 Conditions 工厂里出现了大量 `Policy*` 条件（`PolicyAccepted`、`PolicyReasonTargetConflict` 等）。下一讲将讲清这些策略 CRD（`ClientSettingsPolicy`、`NginxProxy` 等）如何附着到 Gateway/Route，以及 `PolicyAffected` 这类「被策略作用」的条件是怎么打出来的。
- **u8-l3 策略到 NGINX 指令的生成**：关注条件从「图里的策略节点」如何一路走到生成 NGINX 指令，与本讲的 `PrepareNGFPolicyRequests`（生成策略 status 请求）形成闭环。
- **回看 u4-l3/u4-l4**：本讲多次引用 handler 的 `waitForStatusUpdates` 与 `NginxConfigPushed` 契约，结合 u4-l3 的「编排顺序」与 u4-l4 的「changed predicate」会更清楚「为什么有时不入队、入队后消费的是最新图」。
- **延伸阅读**：`internal/controller/status/prepare_requests.go` 是「图 → 状态请求」的完整映射表，通读一遍能把本讲的 Conditions 工厂与各类资源 status 的对应关系彻底串起来。
