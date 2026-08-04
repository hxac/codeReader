# PodAutoscaler 控制器与伸缩总览

## 1. 本讲目标

AIBrix 的「推理感知秒级自动伸缩」能力，落地的核心就是 `PodAutoscaler` 这个自定义资源和它背后的控制器。本讲是整个「自动伸缩」单元（单元 3）的入口，目标是让你先看清「一座大楼的结构」，再在后续讲义里逐层进入「每个房间」。

学完本讲，你应该能够：

- 说清 `PodAutoscaler` CR 的关键字段（`ScalingStrategy`、`MetricsSources`、副本边界、观测窗口）与控制器 reconcile 之间的对应关系。
- 看懂 `Reconcile` 主循环：校验 → 计算状态 → 按 `HPA` / `KPA` / `APA` 分流调度。
- 理解 `DefaultAutoScaler` 的无状态设计，掌握 `ComputeDesiredReplicas` 与 `executeScalingPipeline` 这条伸缩管线的四步编排。
- 说清楚 `algorithmCache` 为什么可以安全地跨 goroutine 复用。

本讲**不**深入「算法本身怎么算」（APA/HPA/KPA 的具体公式）和「指标怎么采集聚合」——那是 u3-l2 与 u3-l3 的内容。本讲只负责把整条「从 CR 到期望副本数」的管线接通。

## 2. 前置知识

阅读本讲前，建议你已经掌握 u2-l3「自定义资源 (CRD) 数据模型设计」中的概念：

- **CR 与控制器**：`PodAutoscaler` 是用户声明的「期望状态（Spec）」，控制器在后台不断把「实际状态」往期望状态拉齐，这个拉齐的过程叫 **reconcile（调谐）**。
- **Spec / Status / Conditions**：Spec 是用户写的输入，Status 是控制器回写的实况，`Conditions` 是一组带 `Reason/Message` 的状态条件位，方便 `kubectl describe` 排障。
- **kubebuilder 标记**：类型上的 `+kubebuilder:...` 注解是「单一数据源」，`make manifests` 会据此生成 CRD 的 OpenAPI 校验与 `printcolumn`。

此外你需要了解两个 controller-runtime 的基础概念（u2-l1 已铺垫）：

- **Reconciler**：实现 `Reconcile(ctx, req) (Result, error)` 的对象，controller-runtime 会把「某个对象发生了变化」翻译成一次 `req`（带命名空间/名字）丢给它处理。Reconciler 返回后，框架决定是否重新排队（requeue）。
- **Manager**：控制器的容器，统一管理 Client、Cache、领导选举、健康探针。

最后两个伸缩领域的术语，本讲会反复用到：

- **副本（replica）**：一个可被伸缩的工作负载实例数，比如一个 Deployment 跑了几个 Pod。
- **伸缩策略（scaling strategy）**：决定「看到指标后怎么算出副本数」的方法。AIBrix 支持三种：`HPA`（转交给 K8s 原生 HPA）、`KPA`（Knative 风格，分 stable/panic 窗口）、`APA`（AIBrix 自研，推理感知）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [api/autoscaling/v1alpha1/podautoscaler_types.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/podautoscaler_types.go) | `PodAutoscaler` CR 的数据模型：Spec/Status/Conditions、三种策略枚举、`MetricSource` 结构。是「输入契约」。 |
| [pkg/controller/podautoscaler/podautoscaler_controller.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go) | 控制器本体：注册、`Reconcile` 主循环、校验、HPA/KPA/APA 分流、状态回写、周期 resync、冷却稳定化。 |
| [pkg/controller/podautoscaler/autoscaler.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/autoscaler.go) | `AutoScaler` 接口与 `DefaultAutoScaler` 实现：无状态伸缩管线 `ComputeDesiredReplicas` → `executeScalingPipeline`，以及算法缓存。 |

辅助文件（本讲会提及但不深入）：

- `pkg/controller/podautoscaler/algorithm/algorithm.go`：`ScalingAlgorithm` 接口与 `NewScalingAlgorithm` 工厂（u3-l2 详讲）。
- `pkg/controller/podautoscaler/metrics/`：指标采集与窗口客户端（u3-l3 详讲）。
- `pkg/controller/podautoscaler/aggregation/`：指标聚合器（u3-l3 详讲）。
- `pkg/controller/podautoscaler/utils.go`：`ValidationResult` 等小工具。

## 4. 核心概念与源码讲解

### 4.1 PodAutoscaler CR 与 reconcile 主循环

#### 4.1.1 概念说明

`PodAutoscaler` 回答一个问题：「这个工作负载（比如一个 vLLM Deployment）应该跑几个副本？」用户在 Spec 里给出三样东西：

1. **伸缩谁**：`ScaleTargetRef` 指向一个带 `/scale` 子资源的工作负载。
2. **看什么指标做决策**：`MetricsSources`，可以是 Pod 级（HTTP 拉取）、资源级（CPU/内存）、自定义指标、外部服务。
3. **用什么策略算**：`ScalingStrategy ∈ {HPA, KPA, APA}`。

控制器读这份 Spec，结合实时指标，算出一个**期望副本数（DesiredScale）**，再把它落到目标工作负载上。整个过程的「大脑」就是 `Reconcile` 函数。

注意一个关键设计取舍：`PodAutoscalerReconciler` 这个结构体本身**是有状态的**（它持有冷却历史、冲突检测表等），但它持有的「计算期望副本数」的那部分逻辑被抽成了**无状态**的 `AutoScaler`（见 4.2）。控制器负责「编排 + 副作用」，AutoScaler 负责「纯计算」，两者职责分离。

#### 4.1.2 核心流程

一次 `Reconcile(req)` 的主流程可以用下面的伪代码描述：

```
Reconcile(req):
    1. 取出 PodAutoscaler 对象 pa（取不到且 NotFound → 清理后直接返回）
    2. pa.Spec 合法性校验 specVR = validateSpec(pa)
    3. 冲突检测 conflictVR = checkNoMultiPodAutoscalerConflict(pa)
    4. 计算并回写 Status（Conditions：ValidSpec/Conflict/ScalingActive/AbleToScale/Ready）
    5. 若 specVR 或 conflictVR 无效 → 不伸缩，直接返回
    6. 按 ScalingStrategy 分流：
         HPA           → reconcileHPA(pa)      // 委托给 K8s 原生 HPA
         KPA / APA     → reconcileCustomPA(pa) // 自己算副本数并落地
         其他          → 什么都不做
```

除了「对象变化触发」的被动 reconcile，控制器还有一条**主动周期 resync** 线程：每隔 `DefaultResyncInterval`（10 秒）把所有 `PodAutoscaler` 对象重新入队。这是因为伸缩是「时间驱动」的——即使对象没变，指标也在变，必须定期重新评估。

#### 4.1.3 源码精读

先看控制器的注册与装配。`Add` 是被 u2-l1 里的 `controllerAddFuncs` 机制调用的入口，它先建 reconciler，再注册 watch：

[podautoscaler_controller.go:107-113](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L107-L113) —— `Add` 拆成 `newReconciler`（装配依赖）与 `add`（注册 watch）两步。

[podautoscaler_controller.go:201-234](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L201-L234) —— `add` 函数做了三件事：① `For(&PodAutoscaler{})` 监听 CR 本身的变化；② `Watches(&HorizontalPodAutoscaler{}, ...)` 反向监听 HPA——当 HPA 变化时，用 `filterHPAObject` 通过 `ownerReferences` 找回它归属的 `PodAutoscaler` 重新入队；③ `WatchesRawSource(src)` 监听一个 channel，这是周期 resync 的投递通道。最后通过 `mgr.Add(manager.RunnableFunc(...))` 把 `reconciler.Run` 作为受 Manager 生命周期管理的后台 goroutine 启动（注释解释了这样做是为了避免旧实现里 errChan + 后台 goroutine 永久泄漏）。

`newReconciler` 里值得注意的一行——它创建了那个「统一的、按请求配置」的 autoscaler：

[podautoscaler_controller.go:148-167](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L148-L167) —— 构造 `DefaultAutoScaler` 并注入到 `PodAutoscalerReconciler.autoScaler` 字段。注意这里的注释：autoscaler 是「按请求配置」的（per-request configured），这正是它无状态的体现。

接着是主角 `Reconcile`：

[podautoscaler_controller.go:278-320](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L278-L320) —— 主循环。第一行就给整个 reconcile 加了 10 秒超时（`DefaultReconcileTimeoutDuration`），避免某次指标拉取卡死阻塞后续调度。随后依次是：取对象 → `validateSpec` → `checkNoMultiPodAutoscalerConflict` → `computeStatus` 回写 → 按 `ScalingStrategy` 分流。第 307-309 行的「校验不通过就退出、不 requeue」是一个安全网设计——校验失败时写好 Conditions 就返回，等用户改对 Spec，对象变化会再次触发 reconcile。

校验逻辑本身（兜底机制）：

[podautoscaler_controller.go:392-409](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L392-L409) —— `validateSpec` 串行调用 5 个子校验。注释明确写了它的定位：in-controller validation，作为「万一 admission webhook 被绕过」时的兜底安全网（webhook 见 u2-l4）。

冲突检测是为了保证「同一个工作负载不会被两个 PodAutoscaler 同时控制」：

[podautoscaler_controller.go:360-388](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L360-L388) —— 用两张内存表 `scalingTargetToPA`（目标 → PA）和 `paToScalingKey`（PA → 目标）做双向映射。目标键由 `buildScalingTargetKey` 生成，对 StormService 还会拼上 `roleName`，从而支持「角色级」伸缩目标也纳入冲突检测。

最后是周期 resync：

[podautoscaler_controller.go:603-621](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L603-L621) —— `Run` 是个 `select` 循环，每 10 秒 `enqueuePodAutoscalers`。注意它 `defer close(r.eventCh)`，并且 `enqueuePodAutoscalers`（623-644 行）在向 channel 发送事件时用 `select { case r.eventCh <- e: case <-ctx.Done(): }` 防止在关闭期间永久阻塞——这是经过打磨的并发安全细节。

#### 4.1.4 代码实践

**实践目标**：用 `kubectl` 观察一个 `PodAutoscaler` 对象，把它的 Spec 字段、Status Conditions 与上面读到的源码对应起来。

**操作步骤**（需要已按 u1-l4 部署好带 operator 的集群；若无集群，则做下面的「源码阅读型」替代）：

1. 找一个现成的 PodAutoscaler 示例（在仓库里搜）：
   ```bash
   # 在仓库根目录
   grep -rl "kind: PodAutoscaler" samples/ config/ 2>/dev/null
   ```
2. 阅读该 YAML，对照 [podautoscaler_types.go:54-98](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/podautoscaler_types.go#L54-L98) 的 `PodAutoscalerSpec`，逐字段写下它的含义：`scaleTargetRef` 指向谁？`scalingStrategy` 是哪种？`metricsSources` 里 `targetMetric`/`targetValue` 是什么？
3. 若有集群，应用该 YAML 后执行：
   ```bash
   kubectl get podautoscaler -A
   kubectl describe podautoscaler <name> -n <ns>
   ```
4. 在 `describe` 输出里找到 `Conditions` 段，对照源码 [podautoscaler_controller.go:646-711](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L646-L711) 的 `computeStatus`，确认你能看到 `ValidSpec`、`ScalingActive`、`AbleToScale`、`Ready` 这几个条件位。

**需要观察的现象**：`kubectl get` 的列（MINPODS/MAXPODS/REPLICAS/STRATEGY）正是 [podautoscaler_types.go:34-38](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/podautoscaler_types.go#L34-L38) 里 `printcolumn` 标记定义的——验证「kubebuilder 标记是 CRD 输出的单一数据源」。

**预期结果**：你能用一句话说清这个 PA「伸缩谁、看什么、用什么策略」，并把 Status 的每个 Condition 对应到 `computeStatus` 里设置它的那行代码。

> 若无可用集群，请改为纯源码阅读实践：在 `computeStatus` 中找到 `Ready` 条件的判定（702-703 行 `ready := able && !scalingActive`），解释为什么「DesiredScale == ActualScale」时 Ready 才为 True。

#### 4.1.5 小练习与答案

**练习 1**：如果用户提交了一个 `scalingStrategy=HPA` 但同时设了 `subTargetSelector.roleName`，控制器会怎样？

**参考答案**：会被拒绝。见 [podautoscaler_controller.go:454-465](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L454-L465) 的 `validateScalingStrategy`：HPA 不支持角色级伸缩，只有 APA/KPA 支持。校验失败会写 `ConditionValidSpec=False` 并跳过伸缩。

**练习 2**：为什么控制器需要周期 resync，而不只依赖「对象变化事件」？

**参考答案**：因为伸缩决策依赖**外部指标**（流量、GPU 利用率等），这些指标随时间变化，但 `PodAutoscaler` 对象本身可能一直不变。若只靠对象变化触发 reconcile，指标涨上去时控制器不会被唤醒。周期 resync（`Run` + `enqueuePodAutoscalers`）保证每 10 秒重新评估一次。

---

### 4.2 DefaultAutoScaler 接口与无状态实现

#### 4.2.1 概念说明

`AutoScaler` 是「纯计算」的抽象：给它指标和当前副本数，它返回一个期望副本数建议，**不碰任何 K8s 资源**。把副作用（改 Deployment 副本数、写 Status）留给控制器，把决策逻辑抽成无状态接口，有两个直接好处：

- **可测试**：无状态函数最容易写单元测试，喂输入断输出即可。
- **并发安全**：多个 PodAutoscaler 的 reconcile 可以并发跑，共用同一个 autoscaler 实例而不互相污染。

`DefaultAutoScaler` 是目前唯一的实现，但它被设计成「按请求配置」——每个请求带自己的 `ScalingContext`（min/max、冷却窗口等），autoscaler 内部不保存任何「针对某个 PA 的」状态。

#### 4.2.2 核心流程

```
AutoScaler.ComputeDesiredReplicas(request):
    pa = request.PodAutoscaler
    for each metricSource in pa.Spec.MetricsSources:
        result = computeReplicasForSingleMetric(...)   # 走一次完整管线
        if result.Valid: 收集到 validResults
        else: 记错误，继续下一个指标（一个失败不阻塞全部）
    if 没有任何有效结果: 返回 Valid=false
    在所有有效结果里取 DesiredReplicas 的最大值作为最终建议
```

「取最大值」是多指标策略的语义：只要有一个指标认为「该扩容」，就扩——这是一种偏保守（偏可用性）的聚合策略。

无状态的关键支撑是 `algorithmCache`：算法对象本身是无状态结构体，可以缓存复用。`getOrCreateAlgorithm` 用「读锁优先 + 写锁双检（double-check）」的经典模式来保证并发安全地创建/复用。

#### 4.2.3 源码精读

接口定义只有一个方法，注释反复强调「只算、不改」：

[autoscaler.go:36-45](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/autoscaler.go#L36-L45) —— `AutoScaler` 接口。注释明确：`ComputeDesiredReplicas` "does NOT perform any actual scaling operations"，且「All per-PA configuration is extracted from the PodAutoscaler spec on each call」（每次调用都从 Spec 里取配置，不持有跨调用的 PA 状态）。

请求与响应类型：

[autoscaler.go:47-63](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/autoscaler.go#L47-L63) —— `ReplicaComputeRequest` 携带 `PodAutoscaler`、`ScalingContext`（PA 级配置的唯一真相源）、当前副本数、Pod 列表、时间戳；`ReplicaComputeResult` 返回期望副本数 + 算法名 + 原因 + 是否有效。

`DefaultAutoScaler` 的字段分两类——不可变共享组件 vs 算法缓存：

[autoscaler.go:65-77](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/autoscaler.go#L65-L77) —— 注意 `factory`、`client`、`metricsClient`、`aggregator` 都是「不可变共享组件」，而 `algorithmCache` 由 `sync.RWMutex` 保护。构造函数 [autoscaler.go:80-98](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/autoscaler.go#L80-L98) 在 `NewDefaultAutoScaler` 里一次性建好 metricsClient 和 aggregator，整个 reconciler 生命周期共用。

算法缓存——本讲的重点之一：

[autoscaler.go:102-124](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/autoscaler.go#L102-L124) —— `getOrCreateAlgorithm`。先 `RLock` 读缓存（命中是常态，读锁开销低）；未命中才升级为写锁，并在拿到写锁后**再查一次**（double-check），防止两个 goroutine 同时通过第一次检查而重复创建。它最终调用 `algorithm.NewScalingAlgorithm(strategy)` 拿到一个无状态算法结构体（见 [algorithm.go:63-74](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/algorithm.go#L63-L74)）。

**为什么 `algorithmCache` 可以安全复用？** 这是本讲实践任务要回答的核心问题，答案有三层：

1. **算法对象本身无状态**：`ScalingAlgorithm` 接口的实现（`KPAAlgorithm`/`APAAlgorithm`/`HPAAlgorithm`）都是空结构体，所有计算所需的数据都通过 `ScalingRequest` 参数传入，对象内部没有任何字段会被 `ComputeRecommendation` 改写。接口注释 [algorithm.go:31-33](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/algorithm.go#L31-L33) 明确要求「Implementations must be stateless and thread-safe」。
2. **按策略键缓存，键值恒定**：缓存键是 `ScalingStrategyType`（只有 HPA/KPA/APA 三种），同一个键永远对应同一种算法实现，不存在「缓存里的算法过时」的问题。
3. **缓存只做读/写结构体指针，不做计算**：并发访问的唯一共享状态就是这张 map，而它由 RWMutex + double-check 保护。真正的并发计算（`ComputeRecommendation`）读写的是各自的栈上局部变量，互不干扰。

主入口——多指标取最大值：

[autoscaler.go:128-205](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/autoscaler.go#L128-L205) —— `ComputeDesiredReplicas`。第 139-171 行遍历所有 `MetricsSources`，单个指标失败只记日志后 `continue`（153-156 行），不阻塞其他指标；第 178-184 行在所有有效结果里选 `DesiredReplicas` 最大者。注意第 141-147 行构造的 `MetricKey` 同时包含 `Namespace/Name`（指标归属的工作负载）和 `PaNamespace/PaName`（指标归属的 PA），后者用于多租户隔离（autoscaler.go 第 276-277 行注释强调「for proper multi-tenancy」）。

#### 4.2.4 代码实践

**实践目标**：理解 `algorithmCache` 的并发模型，亲手追踪一次缓存命中/未命中。

**操作步骤**（源码阅读 + 推理型实践）：

1. 打开 [autoscaler.go:102-124](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/autoscaler.go#L102-L124)，假设有两个 goroutine **同时**第一次请求 `strategy=APA` 的算法。
2. 推演两者的执行交错：
   - goroutine A：`RLock` → 未命中 → `RUnlock` → 申请 `Lock`。
   - goroutine B：`RLock` → 未命中 → `RUnlock` → 申请 `Lock`。
   - 假设 A 先拿到写锁：double-check 未命中 → `NewScalingAlgorithm(APA)` → 写入 map → 解锁。
   - B 拿到写锁：double-check **命中**（A 已写入）→ 直接返回缓存对象 → 解锁。
3. 回答：如果没有 double-check（即 116-118 行去掉），会发生什么？答案：A、B 会各创建一个 `APAAlgorithm` 实例，B 的会覆盖 A 的写入——由于对象无状态，结果**功能上仍然正确**，但多了一次无谓的分配，且 map 写入发生两次。double-check 是性能优化而非正确性必需。

**需要观察的现象**（用日志验证，若本地可运行）：在 `getOrCreateAlgorithm` 的两个 return 路径各加一行 `klog.Infof`（**示例代码**，仅用于理解，不修改真实源码）：

```go
// 示例代码：用于观察缓存命中
if exists {
    klog.Infof("algorithm cache hit: %s", strategy)
    return algo
}
// ... 创建后：
klog.Infof("algorithm cache miss, created: %s", strategy)
```

**预期结果**：服务启动后，每种策略只会出现一次 `cache miss`，之后全是 `cache hit`，证明算法对象被安全复用。

> 说明：上述加日志属于「示例代码」，演示如何验证缓存行为；实际请勿修改源码。

#### 4.2.5 小练习与答案

**练习 1**：`DefaultAutoScaler` 里有 `sync.RWMutex`，但接口注释说实现是「thread-safe」。这两者矛盾吗？

**参考答案**：不矛盾。`AutoScaler` 接口的「计算逻辑」本身（`ComputeRecommendation`）是无状态、天然线程安全的，不需要锁。`RWMutex` 只用来保护 `algorithmCache` 这张共享 map 的读写——这是「可变共享状态」的同步，与「计算逻辑无状态」是两回事。

**练习 2**：如果未来要新增第四种策略 `MPA`，需要改 autoscaler.go 吗？

**参考答案**：不需要改 `autoscaler.go`。`getOrCreateAlgorithm` 通过 `algorithm.NewScalingAlgorithm(strategy)` 工厂创建算法，新增策略只需在 [algorithm.go:63-74](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/algorithm.go#L63-L74) 的 switch 里加一个 case、并在 CRD 枚举（types.go 的 `Enum={HPA,KPA,APA}`）和控制器校验里放行即可。这体现了「工厂 + 缓存」对扩展的友好。

---

### 4.3 executeScalingPipeline 伸缩管线编排

#### 4.3.1 概念说明

`executeScalingPipeline` 是把「指标」变成「副本数建议」的完整流水线，是 4.2 里 `computeReplicasForSingleMetric` 调用的核心。它把一次伸缩决策拆成**四步**：采集 → 配置窗口 → 聚合 → 算法计算。前三步为算法准备「干净、聚合好的指标」，第四步交给具体的 `ScalingAlgorithm`。

注意它与控制器的分工：管线**只产出建议（`ScalingRecommendation`）**，不落地；落地（改 Deployment 副本数）和冷却稳定化在控制器的 `reconcileCustomPA` + `computeScaleDecision` 里。

#### 4.3.2 核心流程

```
executeScalingPipeline(request, metricKey, metricSource, algo):
    Step 1 采集:   snapshot = metrics.CollectMetrics(collectionSpec, factory)
                   （collectionSpec 含 Pods、时间戳、指标来源）
    Step 2 配窗口: stableWindow, panicWindow = metricWindowDurations(pa)
                   metricsClient.ConfigureMetricWindows(metricKey, stable, panic)
    Step 3 聚合:   aggregator.ProcessSnapshot(metricKey, snapshot)       # 入历史窗
                   agg = aggregator.GetAggregatedMetrics(metricKey, now)  # 出聚合值
    Step 4 计算:   recommendation = algo.ComputeRecommendation(scalingRequest)
                   （scalingRequest 含 AggregatedMetrics + ScalingContext + 当前副本数）
    return recommendation
```

窗口的概念来自 Knative KPA：用一个较长的 **stable 窗口**（默认 180 秒）做平稳决策，用一个较短的 **panic 窗口**（默认 60 秒）在流量突涨时快速进入「恐慌模式」激进扩容。这两个默认值定义在 `pkg/controller/podautoscaler/metrics/client.go`：

- `DefaultStableWindowDuration = 180 * time.Second`
- `DefaultPanicWindowDuration = 60 * time.Second`

用户可在 Spec 里用 `observeWindowSeconds` / `panicWindowSeconds` 覆盖。

冷却稳定化（在控制器侧，非管线内）模仿 K8s HPA：扩容时在 `scaleUpCooldownWindow` 内取建议的**最大值**（尽快扩），缩容时在 `scaleDownCooldownWindow` 内取建议的**最小值**（谨慎缩），从而平滑抖动。设窗口内取值函数为 \( f \)，方向为 \( d \in \{\text{up}, \text{down}\} \)，则：

\[ \text{stabilized} = \begin{cases} \max\{r_t \mid t \in [t_0 - W_{\text{up}}, t_0]\} & d = \text{up} \\ \min\{r_t \mid t \in [t_0 - W_{\text{down}}, t_0]\} & d = \text{down} \end{cases} \]

其中 \( r_t \) 是窗口内各时刻的建议副本数。

#### 4.3.3 源码精读

管线的四步全部在一个函数里：

[autoscaler.go:238-325](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/autoscaler.go#L238-L325) —— `executeScalingPipeline`。

- **Step 1（247-261 行）**：构造 `CollectionSpec` 并调用 `metrics.CollectMetrics`，拿到原始 `snapshot`。`CollectionSpec` 把 Pods、时间戳、指标来源全部打包，让采集器无需访问 PA 对象。
- **Step 2（266-270 行）**：`metricWindowDurations(pa)` 解析窗口，再 `ConfigureMetricWindows` 写入 metricsClient。这是「按请求配置」的体现——窗口是 PA 级配置，每次请求按当前 PA 的 Spec 设置。
- **Step 3（272-280 行）**：先 `ProcessSnapshot` 把本次快照喂进聚合器的历史窗口，再 `GetAggregatedMetrics` 读出聚合后的 `StableValue`、`Trend`、`Confidence`。
- **Step 4（298-322 行）**：构造 `ScalingRequest`（含 `AggregatedMetrics` + `ScalingContext` + 当前副本数 + 目标信息），调 `algo.ComputeRecommendation` 拿最终建议。

窗口解析函数：

[autoscaler.go:327-339](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/autoscaler.go#L327-L339) —— `metricWindowDurations`：Spec 没设就用默认（180s/60s），设了就用 Spec 值。

单指标封装：

[autoscaler.go:209-235](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/autoscaler.go#L209-L235) —— `computeReplicasForSingleMetric` 先 `getOrCreateAlgorithm`（注意：用的是 PA 级 `ScalingStrategy`，对所有指标共用同一个算法），再调管线，最后把 `ScalingRecommendation` 翻译成 `ReplicaComputeResult`。第 217 行的注释点明：「算法基于 PA 级策略选择，所有指标共享」。

管线产出的建议，回到控制器侧还要经过边界检查、冷却稳定化、落地三步——这部分在 `reconcileCustomPA` 与 `computeScaleDecision`：

[podautoscaler_controller.go:769-845](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L769-L845) —— `reconcileCustomPA` 是 KPA/APA 的总编排：① 取 scale 资源；② 取当前副本数；③ `computeScaleDecision` 算决策；④ 若需伸缩则 `SetDesiredReplicas` 落地；⑤ 回写 Status。它把「纯计算」委托给 `autoScaler`，把「副作用」留给自己。

[podautoscaler_controller.go:1035-1123](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L1035-L1123) —— `computeScaleDecision` 是控制器侧的决策组装：先做**边界检查**（1047-1073 行：副本为 0 且 min≠0 则禁用伸缩；超 max 则压回 max；低于 min 则拉到 min）；边界正常才调 `computeMetricBasedReplicas`（即走 autoscaler 管线）；再 `stabilizeRecommendation` 做冷却；最后再次夹紧到 `[min, max]`。第 1089-1091 行特意排除了 HPA——因为 HPA 的稳定化由 K8s 原生 HPA 自己负责。

[podautoscaler_controller.go:1136-1175](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L1136-L1175) —— `computeMetricBasedReplicas` 把 Pod 选择器、Pod 列表、时间戳打包成 `ReplicaComputeRequest`，调 `autoScaler.ComputeDesiredReplicas`。注意 1151-1157 行：对 `RayClusterFleet` 目标会额外加上 `ray.io/node-type=head` 标签要求，过滤掉 worker Pod 只统计 head——这是分布式推理场景的特殊处理。

冷却稳定化实现（前面公式的代码对照）：

[podautoscaler_controller.go:1180-1261](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L1180-L1261) —— `stabilizeRecommendation`：把本次建议追加进历史（1202 行），按方向选窗口（1210-1221 行：`recommendation > current` 用 scaleUp 窗口取 max；`< current` 用 scaleDown 窗口取 min；相等直接返回），清掉窗口外的旧记录（1264-1280 行 `cleanOldRecommendations`），最后在窗口内归约。注释（1177-1179 行）明确这是「similar to K8s HPA behavior」。

#### 4.3.4 代码实践

**实践目标**：把「一次 reconcile 从 `Spec.MetricsSources` 到 `ComputeDesiredReplicas`」的完整数据流画出来，并标注每一步的输入/输出类型。

**操作步骤**：

1. 准备一张白纸或文本文件，从左到右画出以下节点，**每个节点标注它接收的类型和产出的类型**：

   ```
   PodAutoscaler.Spec.MetricsSources ([]MetricSource)
        │
        ▼  (Reconcile 取对象 + 校验)
   reconcileCustomPA → getScaleResource → currentReplicas (int32)
        │
        ▼  (computeScaleDecision: 边界检查通过)
   computeMetricBasedReplicas
        │   组装 ReplicaComputeRequest{PodAutoscaler, ScalingContext, CurrentReplicas, Pods, Timestamp}
        ▼
   DefaultAutoScaler.ComputeDesiredReplicas
        │   遍历每个 MetricSource
        ▼
   computeReplicasForSingleMetric
        │   getOrCreateAlgorithm(ScalingStrategy) → ScalingAlgorithm
        ▼
   executeScalingPipeline
        ├─ Step1 CollectMetrics → snapshot
        ├─ Step2 ConfigureMetricWindows → (副作用：设置窗口)
        ├─ Step3 ProcessSnapshot + GetAggregatedMetrics → AggregatedMetrics
        └─ Step4 algo.ComputeRecommendation → ScalingRecommendation
        │
        ▼  (多指标取 max)
   ReplicaComputeResult{DesiredReplicas, Algorithm, Reason, Valid}
        │
        ▼  (回到 computeScaleDecision)
   stabilizeRecommendation → 夹紧 [min,max] → ScaleDecision
        │
        ▼
   workloadScaleClient.SetDesiredReplicas → 落地到 Deployment
   ```

2. 在每个箭头旁标上对应的源码行号（用本节给出的永久链接）。
3. 重点回答：**`algorithmCache` 在这条链路的哪个位置被使用？为什么同一次 reconcile 里即使有 3 个 `MetricsSources`，算法对象也只会被 `NewScalingAlgorithm` 创建一次（最多）？**

**需要观察的现象 / 预期结果**：你应该能指出——`getOrCreateAlgorithm` 在 `computeReplicasForSingleMetric`（[autoscaler.go:217](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/autoscaler.go#L217)）里被调用，而它用 PA 级 `ScalingStrategy` 作键。因此同一次 reconcile 遍历 3 个指标时，第 1 个指标 `cache miss` 创建算法，第 2、3 个指标全部 `cache hit` 复用同一个算法对象。这之所以安全，是因为算法无状态（4.2.3 的三层理由）。这正是实践任务「说明算法缓存为何可以安全复用」的答案。

> 待本地验证：如果你能在集群里跑起一个带 2 个 `metricsSources` 的 PA，可在 `getOrCreateAlgorithm` 加临时日志确认「miss 1 次 + hit 1 次」。

#### 4.3.5 小练习与答案

**练习 1**：为什么边界检查（max/min）在 `computeScaleDecision` 里做了两次（一次在调 autoscaler 前，一次在 stabilize 后）？

**参考答案**：第一次（1047-1073 行）是**短路保护**——如果副本已经越界，直接给出边界值，不必浪费资源去采集指标走完整管线（例如 current > max 时直接压回 max）。第二次（1096-1104 行）是因为 autoscaler 的建议 + 冷却稳定化的结果仍可能越界（算法不保证输出在 `[min, max]` 内，虽然有 `applyConstraints`，但稳定化取 max/min 后可能再次越界），所以落地前必须再夹紧一次。这是「防御性编程」。

**练习 2**：`executeScalingPipeline` 里 Step 2 的 `ConfigureMetricWindows` 每次请求都调用，会不会有性能问题？

**参考答案**：不会有实质问题。`metricKey` 在同一个 PA 的同一指标上是稳定的，`ConfigureMetricWindows` 通常是幂等地设置/确认窗口长度（首次创建窗口，后续为 no-op 或轻量更新）。把配置放在请求里而不是启动时，是为了支持「不同 PA 有不同窗口」的按请求配置语义，牺牲极小的重复调用开销换取灵活性。

---

## 5. 综合实践

设计一个贯穿本讲的任务：**为一种新指标源，手动「走查」整条伸缩管线，并预测控制器在每个分支上的行为。**

假设你有一个 `PodAutoscaler`，Spec 关键字段如下（**示例 YAML**，仅用于练习推演）：

```yaml
# 示例代码：用于推演的 PodAutoscaler（非仓库现有文件）
apiVersion: autoscaling.aibrix.ai/v1alpha1
kind: PodAutoscaler
metadata:
  name: vllm-pa
  namespace: default
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: vllm-server
  minReplicas: 2
  maxReplicas: 8
  scalingStrategy: APA
  metricsSources:
    - metricSourceType: pod
      protocolType: http
      port: "8000"
      path: /metrics
      targetMetric: kv_cache_utilization
      targetValue: "80"
  observeWindowSeconds: 60
  panicWindowSeconds: 30
```

请完成：

1. **校验推演**：对照 [podautoscaler_controller.go:392-409](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L392-L409)，逐条确认这份 Spec 能否通过 `validateSpec` 的 5 个子校验（重点看 `validateMetricWindows` 要求 `panicWindow ≤ observeWindow`，以及 `validatePodMetricSource` 要求 `protocol/port/path` 齐全）。
2. **分流确认**：确认 `ScalingStrategy=APA` 会进入 `reconcileCustomPA` 而非 `reconcileHPA`（见 [podautoscaler_controller.go:311-319](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L311-L319)）。
3. **窗口计算**：写出本次 reconcile 的 `stableWindow` 和 `panicWindow`（应为 60s 和 30s，因为 Spec 覆盖了默认的 180s/60s；依据 [autoscaler.go:327-339](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/autoscaler.go#L327-L339)）。
4. **边界场景**：假设当前副本数是 1（低于 minReplicas=2），预测 `computeScaleDecision` 第一步会直接返回什么（依据 [podautoscaler_controller.go:1066-1073](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L1066-L1073)）。答案：直接返回 `DesiredScale=2, ShouldScale=true, Reason="current replicas below minimum"`，**根本不会去采集指标**。
5. **画数据流图**：完成 4.3.4 的那张图，并在 `kv_cache_utilization` 这个指标的位置标注：它会经过 `metricSourceType=pod` 的 fetcher（HTTP 拉 Pod 的 `/metrics`）。

**预期结果**：你能闭着眼复述「Spec → 校验 → 分流 → 边界检查 → 管线（采集→窗口→聚合→算法）→ 冷却 → 落地 → Status」这条完整链路，并能解释每一步「为什么这么设计」。

## 6. 本讲小结

- `PodAutoscaler` CR 用 `ScaleTargetRef` + `MetricsSources` + `ScalingStrategy` 三件套声明伸缩意图；`Reconcile` 主循环按「校验 → 计算状态 → 按 HPA/KPA/APA 分流」执行，并有 10 秒周期 resync 兜底时间驱动的伸缩。
- 控制器（有状态，负责副作用）与 `AutoScaler`（无状态，负责纯计算）职责分离；`AutoScaler.ComputeDesiredReplicas` 只返回建议，不碰任何资源。
- `executeScalingPipeline` 是四步管线：采集 → 配窗口 → 聚合 → 算法计算；多指标时取期望副本数的**最大值**（偏可用性的聚合）。
- `algorithmCache` 用「RWMutex + double-check」缓存无状态算法对象，可安全跨 goroutine 复用，因为算法无状态、键恒定、共享态仅一张受保护的 map。
- 控制器侧还有边界检查（短路 + 落地前夹紧）、冷却稳定化（扩容取 max、缩容取 min）、HPA 排除等保护逻辑，模仿 K8s HPA 行为。
- 校验是「webhook + 控制器内」双重兜底；冲突检测用双射 map 防止两个 PA 抢同一个目标。

## 7. 下一步学习建议

本讲打通了「管线骨架」，接下来三讲分别深入骨架的三个部位：

- **u3-l2 伸缩算法 APA / HPA / KPA**：进入 [algorithm/algorithm.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/algorithm.go) 的 `apa.go` / `hpa.go` / `kpa.go`，看 `ComputeRecommendation` 里具体的副本数公式与 stable/panic 模式切换。
- **u3-l3 指标采集、聚合与监控管线**：深入本讲只用了一行的 `metrics.CollectMetrics`、`MetricsClient` 的窗口管理、`aggregator` 如何把多 Pod 快照聚合成 `AggregatedMetrics`。
- **u3-l4 工作负载伸缩与 HPA 资源映射**：看 `workload_scale.go` 与 `hpa_resources.go` 如何把 `SetDesiredReplicas` 真正落到 Deployment，以及 PA 与同名原生 HPA 的关系。

建议阅读顺序：u3-l2（理解算法）→ u3-l3（理解指标从哪来）→ u3-l4（理解建议如何落地），这样就能把本讲的管线两头都接上。
