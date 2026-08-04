# ModelAdapter 与 LoRA 适配器管理

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清「LoRA 适配器（adapter）」是什么，以及为什么 AIBrix 需要一个专门的 `ModelAdapter` 控制器来管理它。
- 读懂 `ModelAdapterReconciler` 的 reconcile 主循环：从 `Reconcile` → `DoReconcile` 的四步管线（副本决策 → 加载 → Service → EndpointSlice），以及删除时的 finalizer 清理。
- 掌握 `loraClient` 如何通过 HTTP 与推理引擎（vLLM / SGLang）或运行时边车（runtime sidecar）交互，完成适配器的加载与卸载。
- 理解控制器如何用 Service + EndpointSlice 把「适配器已加载的 Pod」暴露成一个可路由的虚拟模型服务。
- 能够追踪一条「CR 创建 → 适配器加载进引擎」的完整调用链，并能运行相关单元测试验证理解。

## 2. 前置知识

本讲假设你已经读过 **u2-l3（CRD 数据模型）**，了解 `ModelAdapter` 这个自定义资源（CR）的 Spec/Status 骨架与 kubebuilder 标记。下面补充几个本讲需要的基础概念。

### 2.1 什么是 LoRA，为什么要「适配器管理」

大模型推理中，**LoRA（Low-Rank Adaptation）** 是一种参数高效微调方法：它不改动基座模型（base model）的全部权重，而是额外训练一组很小的「低秩适配矩阵」。推理时把适配器「挂载」到基座模型上，就能获得某个下游任务的能力（如 text2sql、代码生成）。

LoRA 的一个巨大优势是**体积小**：一个基座模型可以同时挂载几十甚至上百个不同的适配器，每个适配器只占用极少量显存。这种能力叫**高密度 LoRA（high-density LoRA）**。但它带来一个新的运维问题：

- 适配器是一个「文件/工件（artifact）」，需要先**下载**到推理 Pod，再由引擎**加载（load）**进显存。
- 适配器可以动态**卸载（unload）**，释放显存给别的适配器。
- 不同 Pod 上挂载的适配器集合可能不同，需要被**调度**和**发现**。

如果让用户手动 `kubectl exec` 进每个 Pod 去敲加载命令，体验极差。`ModelAdapter` CR + 控制器就是把这个过程**声明式化**：用户只要提交一个 CR 说「把这个适配器加载到带某标签的 Pod 上」，控制器就负责下载、加载、暴露服务、并在删除 CR 时卸载清理。

### 2.2 两种加载分布模式

`ModelAdapter` 的 `Spec.Replicas` 字段决定适配器加载到多少个 Pod（见 [api/model/v1alpha1/modeladapter_types.go:50-56](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/model/v1alpha1/modeladapter_types.go#L50-L56)）：

- **`Replicas` 省略（nil）**：把适配器加载到**所有**匹配标签的 Pod 上（推荐，对应「全量广播」语义）。
- **`Replicas: 1`**：只加载到**单个**由调度器选出的 Pod 上（对应「稀疏放置」语义，节省显存）。

> 注意：kubebuilder 标记 `+kubebuilder:validation:Enum=1` 限定该字段只能是 `1`，其它整数值会被校验 webhook 拒绝；省略（nil）则走「加载到所有 Pod」分支。

### 2.3 直连引擎 vs 运行时边车

控制器要把适配器加载到引擎里，有两种 HTTP 调用路径：

- **直连引擎（direct engine）**：控制器直接调用推理引擎（vLLM/SGLang）自带的 LoRA 加载 API。
- **运行时边车（runtime sidecar）**：控制器调用注入到 Pod 里的 `aibrix-runtime` 边车，由边车负责下载工件、再委托引擎加载。这样工件下载、凭证管理等复杂逻辑被收敛到边车里（边车本身是 **u9-l1** 的主题）。

控制器会**自动探测**目标 Pod 是否注入了边车，从而决定走哪条路径——这是本讲 4.3 的重点。

## 3. 本讲源码地图

本讲涉及的关键文件（均在 `pkg/controller/modeladapter/` 下）：

| 文件 | 作用 |
| --- | --- |
| [modeladapter_controller.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go) | 控制器主体：reconcile 主循环、副本/加载/Service/EndpointSlice 四步编排、重试退避、周期同步。本讲最核心的文件。 |
| [lora_client.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/lora_client.go) | LoRA 加载/卸载 HTTP 客户端 `loraClient`：构造 URL、列出模型、调用引擎/边车 API。 |
| [resources.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/resources.go) | 构造控制器「拥有的」子资源：headless Service 与 EndpointSlice。 |
| [utils.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/utils.go) | 辅助函数：`BuildURLs`（按引擎/边车选 URL）、`DetectRuntimeSidecar`（探测边车）、`extractHuggingFacePath`（URL 转换）。 |
| [README.md](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/README.md) | 示例 YAML：Deployment、ModelAdapter CR、生成的 Service/EndpointSlice。 |

此外会少量引用：

| 文件 | 作用 |
| --- | --- |
| [api/model/v1alpha1/modeladapter_types.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/model/v1alpha1/modeladapter_types.go) | `ModelAdapter` CR 的 Spec/Status/Phase 定义。 |
| [scheduling/scheduler.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/scheduling/scheduler.go) | `Scheduler` 接口与策略工厂（单 Pod 模式下选 Pod 用）。 |

---

## 4. 核心概念与源码讲解

### 4.1 ModelAdapter 生命周期与整体流程

#### 4.1.1 概念说明

`ModelAdapter` 控制器本质上是一个**声明式生命周期管理器**：它持续观察 `ModelAdapter` CR 的期望状态（`Spec`）和集群实际状态（哪些 Pod 已经加载了该适配器），并驱动二者收敛一致。

`ModelAdapter` 有一个**阶段（Phase）状态机**，定义在 [api/model/v1alpha1/modeladapter_types.go:66-84](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/model/v1alpha1/modeladapter_types.go#L66-L84)：

| Phase | 含义 |
| --- | --- |
| `Pending` | CR 刚创建，尚未开始处理（初始状态）。 |
| `Scheduled` | 单 Pod 模式下，调度器已为适配器选好目标 Pod。 |
| `Bound` | 适配器正在某 Pod 上加载（中间态）。 |
| `ResourceCreated` | Service/EndpointSlice 等子资源已创建。 |
| `Running` | 适配器已成功加载，对外服务就绪。 |
| `Failed` | 加载失败（所有候选 Pod 都失败）。 |
| `Unknown` / `Scaled` | 清理态 / 预留的多副本缩放态（当前未在主路径启用）。 |

除了 Phase，Status 里还有三个计数器（[modeladapter_types.go:87-116](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/model/v1alpha1/modeladapter_types.go#L87-L116)）：

- `Candidates`：匹配 `Spec.PodSelector` 且健康的 Pod 总数（候选池大小）。
- `DesiredReplicas`：期望加载副本数（全量模式 = Candidates；单 Pod 模式 = 1）。
- `ReadyReplicas`：已成功加载的副本数。
- `Instances`：已成功加载的 Pod 名列表。

这三组字段会被 `printcolumn` 显示在 `kubectl get modeladapter` 的输出列里（见 [modeladapter_types.go:131-136](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/model/v1alpha1/modeladapter_types.go#L131-L136)），方便运维一眼看清「想要几个、好了几个、有几个候选」。

#### 4.1.2 核心流程

控制器把一次完整 reconcile 拆成「入口分流 → 四步管线」，整体流程如下：

```
用户提交 ModelAdapter CR
        │
        ▼
  Reconcile(ctx, req)
        │
        ├── CR 不存在？ → 返回（依赖 finalizer 清理）
        │
        ├── 正在删除？(DeletionTimestamp != 0)
        │       └── 有 finalizer？→ unloadModelAdapter(逐 Pod 卸载) → 移除 finalizer → 更新
        │
        └── 未删除 → 确保 finalizer 存在 → DoReconcile
                                    │
            ┌───────────────────────┼────────────────────────┐
            ▼                       ▼                        ▼
     Step1 reconcileReplicas   Step2 reconcileLoading   Step3 reconcileService
     (选目标 Pod，写             (调用引擎加载适配器，      (创建 headless Service
      DesiredReplicas)           维护 Instances 列表)       暴露模型)
                                    │
                                    ▼
                            Step4 reconcileEndpointSlice
                            (用已加载 Pod 的 IP 填充端点)
                                    │
                                    ▼
                          状态有变化？→ 更新 Status → Ready
```

除了事件驱动的 reconcile，控制器还有一个**周期同步循环**（见 4.2.3），每隔 10 秒把所有 `ModelAdapter` 重新入队，保证即使没有外部事件也能持续修复漂移（比如某 Pod 上的适配器被引擎意外移除后重新加载）。

#### 4.1.3 源码精读

CR 的 Spec 字段集中表达了「加载什么、加载到哪、怎么下载」三类意图（[modeladapter_types.go:27-61](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/model/v1alpha1/modeladapter_types.go#L27-L61)）：

```go
type ModelAdapterSpec struct {
    BaseModel            *string                // 挂到哪个基座模型（可选，用于给生成的 Service 打标签）
    PodSelector          *metav1.LabelSelector  // 用标签选出候选 Pod（必填）
    SchedulerName        string                 // 调度器名（默认 "default"）
    ArtifactURL          string                 // 适配器工件地址，支持 huggingface://、s3、gcs 等（必填）
    CredentialsSecretRef *corev1.LocalObjectReference // 下载凭证指向的 Secret
    Replicas             *int32                 // nil=加载到所有 Pod；1=加载到单 Pod
    AdditionalConfig     map[string]string      // 附加配置（如 api-key、model-artifact）
}
```

控制器自身的注册与装配在 [modeladapter_controller.go:129-191](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L129-L191) 的 `newReconciler`：它从 manager 的 cache 取出 Pod/Service/EndpointSlice 的 informer 构造 lister，初始化中央缓存 `cache.Get()`，并用配置里的调度策略名构造调度器，最后组装出 `ModelAdapterReconciler`。

> 术语解释：
> - **informer / lister**：client-go 的本地缓存机制。控制器不每次都直连 API Server，而是在本地维护一份对象缓存（informer 订阅变更），读取时用 lister 高速查询。
> - **中央缓存 `pkg/cache`**：AIBrix 的公共依赖（**u6-l1** 主题），调度器通过它查询每个 Pod 当前挂载了多少适配器、负载如何，从而做调度决策。

`Add` 函数用 controller-runtime 的 Builder 链声明「监听谁、拥有谁」（[modeladapter_controller.go:237-277](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L237-L277)）：

```go
err := ctrl.NewControllerManagedBy(mgr).
    Named(controllerName).
    For(&modelv1alpha1.ModelAdapter{}, builder.WithPredicates(predicate.Or(
        predicate.GenerationChangedPredicate{},      // spec.generation 变化
        predicate.LabelChangedPredicate{},           // 标签变化
        predicate.AnnotationChangedPredicate{},      // 注解变化（重试状态写在注解里）
    ))).
    Owns(&corev1.Service{}).                         // 拥有并监听 Service
    Owns(&discoveryv1.EndpointSlice{}).              // 拥有并监听 EndpointSlice
    Watches(&corev1.Pod{}, ...,                      // 监听带 adapter 标签的 Pod
        builder.WithPredicates(podWithLabelFilter(...))).
    WatchesRawSource(src).                           // 监听周期同步事件通道
    Complete(r)
```

几个关键设计点：

1. **`For(ModelAdapter)` + 三个谓词**：只有 spec generation / 标签 / 注解变化才触发 reconcile。注意注解变化也被纳入——因为控制器把**重试计数、上次重试时间**等状态写在 `ModelAdapter` 的注解里（4.2.4 详述），更新注解会自然触发下一次 reconcile。
2. **`Owns(Service)` / `Owns(EndpointSlice)`**：控制器「拥有」这两个子资源（通过 OwnerReference），它们任何变化都会触发所属 `ModelAdapter` 的 reconcile；当 `ModelAdapter` 被删除时，Kubernetes 会因 OwnerReference 自动级联删除它们。
3. **`Watches(Pod)` + `lookupLinkedModelAdapterInNamespace`**：当匹配 `adapter.model.aibrix.ai/enabled=true` 标签的 Pod 发生变化（新建/就绪）时，把同命名空间下**所有** `ModelAdapter` 重新入队。注释解释了为什么是「所有」：待处理（pending）的适配器需要在新 Pod 就绪时被立即调度，而不仅是已绑定的那个（[modeladapter_controller.go:217-234](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L217-L234)）。

#### 4.1.4 代码实践

**实践目标**：用一个真实 CR 示例，把 `Spec` 字段和「加载到哪」对应起来。

**操作步骤**：

1. 阅读 [README.md:45-67](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/README.md#L45-L67) 给出的 `ModelAdapter` CR 示例：

   ```yaml
   apiVersion: model.aibrix.ai/v1alpha1
   kind: ModelAdapter
   metadata:
     name: text2sql-lora-1
     namespace: default
   spec:
     additionalConfig:
       model-artifact: jeffwan/rank-1
     baseModel: llama2-70b
     podSelector:
       matchLabels:
         model.aibrix.ai/name: llama2-70b
     schedulerName: default-model-adapter-scheduler
     status:
       phase: Configuring   # 注：README 是早期笔记，实际初始 phase 应为 Pending
   ```

2. 对照 [modeladapter_types.go:27-61](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/model/v1alpha1/modeladapter_types.go#L27-L61) 的 Spec 定义，标注示例中每个字段属于 Spec 的哪一项。

**需要观察的现象 / 预期结果**：

- 该 CR 没有写 `replicas`，因此 `Spec.Replicas == nil`，控制器会走「加载到所有匹配 `model.aibrix.ai/name: llama2-70b` 标签的 Pod」分支。
- `artifactURL` 在示例里实际放在了 `additionalConfig.model-artifact`（README 是早期写法）；当前代码以 `Spec.ArtifactURL` 为准（必填）。这是 README 笔记与现行代码的一个差异点，标注出来即可，**待本地验证**你所在版本的必填字段是否校验通过。

#### 4.1.5 小练习与答案

**练习 1**：如果用户希望某个 LoRA 适配器只加载到一个 Pod 上（节省显存），应该在 CR 里怎么写？为什么不能写 `replicas: 3`？

> **答案**：把 `spec.replicas` 设为 `1`。kubebuilder 标记 `+kubebuilder:validation:Enum=1` 限定该字段只能是 `1`，写 `3` 会被校验 webhook 拒绝；想「加载到所有 Pod」则直接省略该字段（nil）。

**练习 2**：`Status.Instances`、`Status.Candidates`、`Status.ReadyReplicas` 三个字段分别描述什么？哪一个会被 `kubectl get` 默认显示？

> **答案**：`Instances` 是已成功加载适配器的 Pod 名列表；`Candidates` 是匹配 selector 的健康候选 Pod 总数；`ReadyReplicas` 是已就绪（成功加载）的副本数。三者中 `Candidates`、`ReadyReplicas` 都通过 `printcolumn`（[modeladapter_types.go:132-134](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/model/v1alpha1/modeladapter_types.go#L132-L134)）显示在 `kubectl get` 的 `Desired/Ready/Candidates` 列里；`Instances` 是列表不直接显示为列。

---

### 4.2 ModelAdapter reconcile 主循环

#### 4.2.1 概念说明

主循环解决一个问题：**每次被触发时，如何安全、幂等地把一个 `ModelAdapter` 推进到 Running（或清理掉）**。它要处理三类情况：

1. **新建/更新**：跑四步管线，把适配器加载到 Pod 上并暴露服务。
2. **删除**：在引擎里卸载适配器，再移除 finalizer 放行删除。
3. **漂移修复**：某 Pod 重启后适配器丢失，或某 Pod 被删除，需要重新加载或迁移。

幂等性靠两层保证：每次 reconcile 都重新计算 `Instances` 与候选 Pod 的差集；加载前先用 `getModels` 查询引擎里是否已存在该适配器（4.3 详述），避免重复加载。

#### 4.2.2 核心流程

**入口 `Reconcile`（[modeladapter_controller.go:313-369](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L313-L369)）**：

```
Get(ModelAdapter)
  ├── NotFound → 直接返回（finalizer 负责清理）
  ├── DeletionTimestamp == 0（未删除）：
  │     └── 没有 finalizer？→ 加 finalizer + Update → 返回 requeue
  │         （有 finalizer）→ DoReconcile(ctx, req, instance)
  └── DeletionTimestamp != 0（删除中）：
        └── 有 finalizer？→ unloadModelAdapter（逐 Pod 卸载）
                           → RemoveFinalizer → Update → 返回
```

**`DoReconcile` 四步（[modeladapter_controller.go:417-493](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L417-L493)）**：

```
Step1 reconcileReplicas :  算 DesiredReplicas，单 Pod 模式下调调度器选 Pod
Step2 reconcileLoading  :  对每个目标 Pod 调 loraClient.LoadAdapter，维护 Instances
Step3 reconcileService  :  创建/确认 headless Service
Step4 reconcileEndpointSlice : 用已加载 Pod 的 IP 填充 EndpointSlice
                          → 状态不一致则更新 Status(Ready)
```

**副本决策 `reconcileReplicas`（[modeladapter_controller.go:581-618](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L581-L618)）**：

```
activePods = getActivePodsForModelAdapter(selector)   // 按 selector 列出 + 过滤 terminating/未就绪
Status.Candidates = len(activePods)
// 清理 Instances 里已不活跃的 Pod
loadOnAll = (Spec.Replicas == nil)
  ├── loadOnAll:  DesiredReplicas = Candidates → reconcileLoadOnAllPods（实际加载延后到 Step2）
  └── 单 Pod:     DesiredReplicas = 1            → reconcileLoadOnSinglePod（调调度器选 1 个 Pod）
```

**单 Pod 调度 `reconcileLoadOnSinglePod`（[modeladapter_controller.go:629-697](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L629-L697)）**：

- 当前已加载副本 < 1 时，从「未在 Instances 中且已稳定就绪」的候选 Pod 里，调 `scheduler.SelectPod` 选出 1 个，把选择结果写进注解 `adapter.model.aibrix.ai/scheduled-pods`，并把 Phase 置为 `Scheduled`。
- 若候选 Pod 数不够或没有就绪 Pod，则带退避 requeue 等待（`RetryBackoffSeconds = 5`）。

> 这里有一个**稳定性细节**：`isPodReadyForScheduling` 要求 Pod 进入 Ready 状态后**至少稳定 5 秒**才视为可调度（[modeladapter_controller.go:1029-1048](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L1029-L1048)），避免在 Pod 频繁抖动（flapping）时把适配器调度到马上又会挂掉的 Pod 上。

#### 4.2.3 源码精读：周期同步循环

除了事件驱动，控制器注册了一个随 manager 生命周期运行的 goroutine，周期性把所有 `ModelAdapter` 重新入队（[modeladapter_controller.go:268-277](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L268-L277)）：

```go
if err := mgr.Add(manager.RunnableFunc(func(ctx context.Context) error {
    reconciler.Run(ctx)
    return nil
})); err != nil { return err }
```

`Run` 本体是一个 ticker 循环（[modeladapter_controller.go:375-393](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L375-L393)）：

```go
func (r *ModelAdapterReconciler) Run(ctx context.Context) {
    ticker := time.NewTicker(r.resyncInterval) // DefaultResyncInterval = 10s
    defer ticker.Stop()
    defer close(r.eventCh)
    for {
        select {
        case <-ticker.C:
            r.enqueueModelAdapters(ctx)  // 把所有 MA 作为 GenericEvent 发到 eventCh
        case <-ctx.Done():
            return
        }
    }
}
```

`enqueueModelAdapters`（[modeladapter_controller.go:395-415](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L395-L415)）遍历所有 `ModelAdapter`，向 `eventCh` 发送 `GenericEvent`，该通道由 `add` 里的 `WatchesRawSource(src)` 接入。注意 send 时用 `select` 同时监听 `ctx.Done()`，避免关停时消费者已退出而永远阻塞。

#### 4.2.4 源码精读：加载重试与指数退避

加载可能因 Pod 还没完全就绪、网络抖动而失败。控制器用「注解记录重试状态 + 指数退避」来稳健重试（[modeladapter_controller.go:1057-1098](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L1057-L1098)）：

```go
func (r *ModelAdapterReconciler) tryLoadModelAdapterOnPod(...) (bool, bool, error) {
    retryCount, lastRetryTime := r.getRetryInfo(instance, pod.Name)
    backoffDuration := r.calculateExponentialBackoff(retryCount)
    if time.Since(lastRetryTime) < backoffDuration {
        return false, true, fmt.Errorf("waiting for exponential backoff: %v", backoffDuration)
    }
    if retryCount >= MaxLoadingRetries { // MaxLoadingRetries = 5
        return false, false, fmt.Errorf("max retries (%d) exceeded", MaxLoadingRetries)
    }
    r.updateRetryInfo(instance, pod.Name, retryCount+1)
    _, exists, err := r.loraClient.LoadAdapter(ctx, instance, pod)
    // ... 区分可重试 / 不可重试错误；成功则 clearRetryInfo
}
```

退避时长计算（[modeladapter_controller.go:1218-1235](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L1218-L1235)）以 `RetryBackoffSeconds = 5` 为基数，乘以 \(2^{\text{retryCount}}\)，并对乘数封顶（防止溢出与过长等待）：

\[
\text{backoff}(n) =
\begin{cases}
0, & n = 0 \\
\min(2^n,\ 60) \times 5 \ \text{秒}, & n \ge 1
\end{cases}
\]

代入得：第 1 次重试等 10s，第 2 次 20s，第 3 次 40s，第 4 次 80s，第 5 次 160s（随后达到 `MaxLoadingRetries` 不再重试）。封顶 60 倍即最多 300s（5 分钟）。

是否可重试由错误文本判定（[modeladapter_controller.go:1101-1126](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L1101-L1126)）：`connection refused`、`timeout`、`service unavailable`、`bad gateway` 等属于可重试的瞬时错误；其余视为不可重试（如参数非法）。重试状态以「每个 Pod 一组 key」的形式存在注解里（`adapter.model.aibrix.ai/retry-count.<podName>`、`...last-retry-time.<podName>`），见 [modeladapter_controller.go:1129-1163](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L1129-L1163)。

> 设计含义：把重试状态写在 `ModelAdapter` 的**注解**里（而非内存），意味着控制器重启后仍能恢复重试进度；代价是每次更新注解会触发一次 reconcile（这正是 4.1.3 把 `AnnotationChangedPredicate` 纳入触发条件的原因）。

#### 4.2.5 源码精读：加载主逻辑 `reconcileLoading`

`reconcileLoading`（[modeladapter_controller.go:728-893](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L728-L893)）是连接「副本决策」与「引擎调用」的桥梁，核心分两种情况：

- **副本不足**（已加载 < DesiredReplicas）：把调度器选中的 Pod 排在候选列表前面（优先加载），逐个调 `tryLoadModelAdapterOnPod`；**只有加载成功才把 Pod 名追加进 `Instances`**（保证 `Instances` 永远只含已加载成功的 Pod）。
- **副本已足**：对所有现有 `Instances` 再跑一次 `tryLoadModelAdapterOnPod`。因为 `LoadAdapter` 内部会先查引擎已加载列表（4.3），对已存在的适配器直接返回 `exists=true`，所以这是一次幂等的「补偿（reconcile）」——若某 Pod 重启后丢了适配器，这里会重新加载。

它还处理**Pod 迁移**：通过对比 `oldInstances` 与当前 `Instances` 检测 Pod 被移除（[modeladapter_controller.go:756-776](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L756-L776)），在迁移期间把 Ready/Scheduled 置为 False，加载成功后再恢复为 True，让上游能感知到「适配器正在迁移」。

#### 4.2.6 代码实践

**实践目标**：运行真实的控制器单元测试，验证「重复加载是幂等的」「错误会触发重试」。

**操作步骤**：

1. 在仓库根目录运行加载客户端的单元测试（这是控制器调用引擎的底层逻辑）：

   ```bash
   go test ./pkg/controller/modeladapter/ -run TestLoadAdapter -v
   go test ./pkg/controller/modeladapter/ -run TestUnloadAdapter -v
   ```

2. 阅读 [lora_client_test.go:35-192](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/lora_client_test.go#L35-L192)，关注 `prepareModelApiResponseWithTwoModels(... "qwen-lora-test")` 这个用例：当 `/v1/models` 的返回里已经包含适配器名时，`LoadAdapter` 返回 `(loaded=false, exists=true)`，**不会**再调用 load API。

**需要观察的现象 / 预期结果**：

- `TestLoadAdapter` 全部 PASS，其中「model exists ok」用例验证了幂等性。
- 若环境没有 Go 工具链，标注「待本地验证」，改为**源码阅读型实践**：在 `reconcileLoading` 里找到「只有 success 才 append 到 Instances」的那一行（[modeladapter_controller.go:807-811](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L807-L811)），并解释为什么不能在调用 `LoadAdapter` 之前就加入 `Instances`。

#### 4.2.7 小练习与答案

**练习 1**：控制器既监听 `ModelAdapter` 事件，又有 10 秒一次的周期同步。会不会因此重复加载适配器？为什么？

> **答案**：不会。每次 `reconcileLoading` 调 `tryLoadModelAdapterOnPod` → `LoadAdapter`，而 `LoadAdapter` 会先 GET `/v1/models` 查询引擎，若适配器已存在则直接返回 `exists=true` 不再 POST load；并且只有加载成功才把 Pod 名加入 `Instances`。所以重复触发是幂等的。

**练习 2**：某 Pod 在加载适配器后因 OOM 重启，适配器丢失。控制器如何自动恢复？

> **答案**：周期同步（或 Pod 事件）触发 `reconcileLoading`；当 `successfulLoadings >= DesiredReplicas` 时进入「补偿」分支，对现有 `Instances` 逐个再调 `tryLoadModelAdapterOnPod`。引擎里已不存在该适配器，故 `getModels` 返回 false，重新执行加载。

---

### 4.3 LoRA 加载/卸载客户端 loraClient

#### 4.3.1 概念说明

`loraClient` 是控制器与「加载目标」之间的**唯一 HTTP 出口**。它屏蔽了两件复杂的事：

1. **目标是谁**：同一个 Pod 可能装了边车，也可能没装；引擎可能是 vLLM 也可能是 SGLang，它们各自的 LoRA API 路径和端口都不同。
2. **payload 长什么样**：直连引擎时要传「本地路径」（工件已下载好），走边车时要传「原始 URL + 凭证」（让边车去下载）。

`loraClient` 把这些差异收敛成两个高层方法：`LoadAdapter(ctx, instance, pod) → (loaded, exists, err)` 与 `UnloadAdapter(instance, pod) → err`。

#### 4.3.2 核心流程

**`LoadAdapter`（[lora_client.go:82-108](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/lora_client.go#L82-L108)）**：

```
useSidecar = EnableRuntimeSidecar(全局开关) && DetectRuntimeSidecar(pod)(Pod 里有 aibrix-runtime 容器)
urls = BuildURLs(podIP, config, useSidecar, engineType)
models = getModels(urls.ListModelsURL)        // GET /v1/models
if models[instance.Name] 存在: return (loaded=false, exists=true)   // 幂等短路
loadAdapterCall(urls.LoadAdapterURL, ...)     // POST 加载
return (loaded=true, exists=false)
```

**URL 构造 `BuildURLs`（[utils.go:120-158](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/utils.go#L120-L158)）** 按三档选 host + 路径：

| 场景 | host（端口） | list 路径 | load 路径 | unload 路径 |
| --- | --- | --- | --- | --- |
| Debug 模式 | `localhost:30081` | /v1/models | /v1/load_lora_adapter | /v1/unload_lora_adapter |
| 边车模式 | `podIP:8080` | /v1/models | /v1/lora_adapter/load | /v1/lora_adapter/unload |
| 直连 vLLM | `podIP:8000` | /v1/models | /v1/load_lora_adapter | /v1/unload_lora_adapter |
| 直连 SGLang | `podIP:8000` | /v1/models | /load_lora_adapter | /unload_lora_adapter |

（路径常量定义见 [lora_client.go:39-54](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/lora_client.go#L39-L54)。）

**`UnloadAdapter`（[lora_client.go:111-160](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/lora_client.go#L111-L160)）**：与 load 对称，但**故意忽略 HTTP 错误**（返回 nil）。因为卸载发生在 CR 删除路径上，基座模型 Pod 可能已经被删，此时卸载失败不应阻塞 finalizer 移除——属于「尽力而为（best effort）」清理。

#### 4.3.3 源码精读：直连 vs 边车的 payload 差异

`loadAdapterCall`（[lora_client.go:208-315](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/lora_client.go#L208-L315)）按 `useSidecar` 构造不同请求体：

**边车路径**——把原始工件 URL、下载凭证、附加配置原样交给边车，由边车负责下载与委托加载（[lora_client.go:213-248](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/lora_client.go#L213-L248)）：

```go
payload := map[string]interface{}{
    "lora_name":    instance.Name,
    "artifact_url": instance.Spec.ArtifactURL, // 原样下放
}
if instance.Spec.CredentialsSecretRef != nil && c.k8sClient != nil {
    secret, _ := c.k8sClient.CoreV1().Secrets(ns).Get(ctx, ref.Name, ...)
    credentials := /* secret.Data 转 string map */
    payload["credentials"] = credentials
}
if instance.Spec.AdditionalConfig != nil {
    payload["additional_config"] = instance.Spec.AdditionalConfig
}
```

注意：边车路径需要 `k8sClient` 来读取下载凭证 Secret，这就是为什么 `newReconciler` 用 `NewLoraClientWithK8sClient`（带 clientset）而非无 client 的构造器（[modeladapter_controller.go:188](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L188)）。

**直连路径**——把 `huggingface://org/repo` 这种 URL 转成本地路径交给引擎（工件需已存在于 Pod 本地）（[lora_client.go:250-278](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/lora_client.go#L250-L278)）：

```go
artifactURL := instance.Spec.ArtifactURL
if strings.HasPrefix(instance.Spec.ArtifactURL, "huggingface://") {
    artifactURL, err = extractHuggingFacePath(instance.Spec.ArtifactURL) // org/repo
}
payload := map[string]string{
    "lora_name": instance.Name,
    "lora_path": artifactURL,
}
```

`extractHuggingFacePath`（[utils.go:49-69](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/utils.go#L49-L69)）解析 `huggingface://` 协议，取出 `host + path` 拼成 `org/repo` 形式。

**列模型 `getModels`（[lora_client.go:162-205](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/lora_client.go#L162-L205)）**：GET `/v1/models`，解析 OpenAI 风格的 `{"data":[{"id":"..."}]}`，把每个 `id` 收进 map。这是实现「幂等加载」的关键——加载前先确认引擎里有没有。若 `AdditionalConfig["api-key"]` 存在，会带 `Authorization: Bearer <token>` 头（[lora_client.go:167-170](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/lora_client.go#L167-L170)），用于访问需要鉴权的引擎。

**卸载 payload `buildUnloadPayload`（[lora_client.go:318-338](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/lora_client.go#L318-L338)）**：边车模式额外带 `cleanup_local: true`，让边车在卸载后清理本地下载的工件文件；直连模式只发 `lora_name`。

#### 4.3.4 代码实践

**实践目标**：追踪一个 LoRA 适配器「从 CR 到引擎加载」的调用链，列出 `loraClient` 调用的关键方法与对应引擎 API。

**操作步骤**（源码阅读型追踪）：

1. 从 [modeladapter_controller.go:1076](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L1076) 的 `r.loraClient.LoadAdapter(ctx, instance, pod)` 出发。
2. 进入 [lora_client.go:82](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/lora_client.go#L82) 的 `LoadAdapter`，依次记录它调用的方法与 HTTP 请求。
3. 填写下面这张映射表。

**需要观察的现象 / 预期结果**（参考答案）：

| 步骤 | loraClient 方法 | HTTP 方法 | 引擎/边车 API 路径（以直连 vLLM 为例） | 作用 |
| --- | --- | --- | --- | --- |
| 1 | `BuildURLs` | — | — | 按 useSidecar/engineType 选 host 与路径 |
| 2 | `getModels` | GET | `/v1/models` | 查询引擎已加载的模型列表 |
| 3 | `loadAdapterCall` | POST | `/v1/load_lora_adapter` | 直连 vLLM 时传 `{lora_name, lora_path}` |
| （卸载） | `UnloadAdapter`→`buildUnloadPayload` | POST | `/v1/unload_lora_adapter` | 删除 CR 时尽力卸载 |

若走边车路径，第 3 步路径变为 `/v1/lora_adapter/load`、payload 变为 `{lora_name, artifact_url, credentials, additional_config}`；SGLang 直连路径则是 `/load_lora_adapter`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `LoadAdapter` 要先 GET `/v1/models` 再 POST 加载，而不是直接 POST？

> **答案**：为了**幂等**。若引擎里已有同名适配器（比如上次加载成功但状态还没回写），直接 POST 可能触发引擎的「重复加载」错误。先查询列表，命中则返回 `exists=true` 短路，既避免错误又让控制器知道「无需新加载」。

**练习 2**：`UnloadAdapter` 为什么对 HTTP 错误「视而不见」（返回 nil）？

> **答案**：卸载发生在 CR 删除路径（finalizer 清理）上。此时基座模型 Pod 可能已经被删，HTTP 调用必然失败；若把错误返回上去，会阻塞 finalizer 移除，导致 CR 永远卡在删除中。所以采用 best-effort：记录告警日志但放行。

---

### 4.4 资源管理：Service、EndpointSlice 与 Finalizer

#### 4.4.1 概念说明

把适配器加载进 Pod 只是「内部就绪」。要让网关/客户端能**按适配器名路由请求**，控制器还需创建两类 Kubernetes 子资源（定义在 [resources.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/resources.go)）：

- **headless Service**：名字等于 `ModelAdapter` 名，selector 指向基座模型 Pod，提供「按适配器名解析到一组 Pod IP」的 DNS 入口。
- **EndpointSlice**：由控制器**手动维护**（而非让 Service controller 自动生成），里面只放「真正已加载该适配器」的 Pod IP。

之所以手动维护 EndpointSlice，是因为 Service 的 selector 只能按标签选「基座模型 Pod」，无法表达「该 Pod 上已加载了这个适配器」这层动态语义——这层语义只有控制器知道（存在 `Status.Instances` 里）。

此外，删除时的**资源清理**由 finalizer 机制保证。

#### 4.4.2 核心流程

**Service 创建 `reconcileService`（[modeladapter_controller.go:934-967](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L934-L967)**：

```
Get(Service, name=instance.Name)
  ├── NotFound → buildModelAdapterService → SetControllerReference(OwnerRef) → Create
  └── 存在 → （暂不 diff 更新，留 TODO）
```

**EndpointSlice 维护 `reconcileEndpointSlice`（[modeladapter_controller.go:968-1011](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L968-L1011)）**：

```
podList = Status.Instances 里每个仍存活(未删)的 Pod
Get(EndpointSlice, name=instance.Name)
  ├── NotFound 且 podList 非空 → buildModelAdapterEndpointSlice → Create → Phase=Running
  └── 已存在 → 用 podList 的 PodIP 重写 Endpoints → Update → Phase=Running
```

**删除清理 `unloadModelAdapter`（[modeladapter_controller.go:895-919](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L895-L919)）**：遍历 `Status.Instances`，对每个 Pod 调 `loraClient.UnloadAdapter`；Pod 已不存在则跳过（`IsNotFound` continue）。完成后由 `Reconcile` 移除 finalizer。

#### 4.4.3 源码精读

**构造 headless Service**（[resources.go:64-102](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/resources.go#L64-L102)）：

```go
func buildModelAdapterService(instance *modelv1alpha1.ModelAdapter) *corev1.Service {
    labels := map[string]string{ModelAdapterKey: instance.Name}      // adapter.model.aibrix.ai/name
    if instance.Spec.BaseModel != nil {
        labels[ModelIdentifierKey] = *instance.Spec.BaseModel         // model.aibrix.ai/name
    }
    ports := []corev1.ServicePort{{Name: "http", Port: 8000, TargetPort: intstr.FromInt(8000), ...}}
    return &corev1.Service{
        ObjectMeta: metav1.ObjectMeta{
            Name: instance.Name, Namespace: instance.Namespace, Labels: labels,
            OwnerReferences: []metav1.OwnerReference{*metav1.NewControllerRef(instance, controllerKind)},
        },
        Spec: corev1.ServiceSpec{
            ClusterIP: corev1.ClusterIPNone,        // headless
            PublishNotReadyAddresses: true,
            Ports: ports,
        },
    }
}
```

要点：

- **`ClusterIP: None`**：headless Service，DNS 查询直接返回后面的 Pod IP（由 EndpointSlice 提供），而非一个虚拟 ClusterIP。这让网关能拿到具体 Pod 地址做精细路由。
- **`PublishNotReadyAddresses: true`**：即使 Pod 未 Ready 也发布地址。LoRA 加载发生在 Pod Ready 之后，但网关可能需要在适配器就绪瞬间就能路由。
- **不设 `Selector`**：注意这里**没有**在 ServiceSpec 里写 selector（README 示例里 selector 是旧形态）。控制器改用**手动维护 EndpointSlice** 来精确控制哪些 Pod 对外可见。
- **OwnerReference**：指向 `ModelAdapter`，CR 删除时 Service 会被级联 GC。

**构造 EndpointSlice**（[resources.go:28-62](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/resources.go#L28-L62)）：

```go
addresses := /* 每个 pod 的 Status.PodIP 包装成 discoveryv1.Endpoint */
ports := []discoveryv1.EndpointPort{{Name: ptr.To("http"), Protocol: ptr.To(TCP), Port: ptr.To(int32(8000))}}
return &discoveryv1.EndpointSlice{
    ObjectMeta: /* Name=instance.Name, OwnerRef=instance */,
    AddressType: discoveryv1.AddressTypeIPv4,
    Endpoints:   addresses,
    Ports:       ports,
}
```

这样，网关（**u7 单元**）就可以通过「Service 名 = 适配器名」解析到一组已加载该适配器的 Pod IP，从而实现按适配器名的请求路由。README 里给出的 EndpointSlice 示例（[README.md:111-138](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/README.md#L111-L138)）正是控制器产出的形态。

#### 4.4.4 代码实践

**实践目标**：理解删除一个 `ModelAdapter` 时，控制器如何保证「引擎里的适配器被卸载 + 子资源被回收」。

**操作步骤**（源码阅读型）：

1. 阅读 `Reconcile` 的删除分支（[modeladapter_controller.go:346-366](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L346-L366)），记录三步顺序。
2. 思考：finalizer `adapter.model.aibrix.ai/finalizer`（定义在 [modeladapter_controller.go:60](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L60)）若移除失败会怎样。

**需要观察的现象 / 预期结果**：

- 删除顺序：`unloadModelAdapter`（逐 Pod 卸载，best-effort）→ `RemoveFinalizer` → `Update`。只有 finalizer 被成功移除并写回后，Kubernetes 才真正删除该 CR 对象。
- Service / EndpointSlice **不需要**控制器手动删——它们带 OwnerReference 指向该 CR，CR 删除时由 Kubernetes 垃圾回收（GC）级联删除。
- 若 `unloadModelAdapter` 返回错误，`Reconcile` 直接 `return ctrl.Result{}, err`，finalizer 不会被移除，CR 停留在 `Terminating` 状态等待下次重试——这保证卸载不会因偶发错误被跳过。

#### 4.4.5 小练习与答案

**练习 1**：Service 没有写 `spec.selector`，那它怎么知道要把流量发到哪些 Pod？

> **答案**：靠控制器**手动维护的同名 EndpointSlice**。Service 的 DNS 解析直接读取同名 EndpointSlice 里的地址列表；而该 EndpointSlice 的地址由控制器根据 `Status.Instances`（已加载该适配器的 Pod）写入。这样精确表达了「加载了此适配器的 Pod」这层 selector 表达不了的动态语义。

**练习 2**：为什么 EndpointSlice 的端口写死成 8000？

> **答案**：8000 是推理引擎（vLLM/SGLang）默认的服务端口（`DefaultInferenceEnginePort`，[modeladapter_controller.go:106](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L106)）。LoRA 适配器复用基座模型的 HTTP 服务端口对外提供推理，故 EndpointSlice 指向 8000。代码注释也提示「后续应支持动态配置」。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「端到端调用链追踪」。

**任务**：假设用户提交了如下 CR（单 Pod 模式，直连 vLLM，无边车）：

```yaml
apiVersion: model.aibrix.ai/v1alpha1
kind: ModelAdapter
metadata:
  name: text2sql-lora
  namespace: default
spec:
  baseModel: llama2-70b
  podSelector:
    matchLabels:
      model.aibrix.ai/name: llama2-70b
  artifactURL: huggingface://jeffwan/rank-1
  replicas: 1
```

请按时间顺序写出控制器会执行的**关键动作**与**对应源码位置**，并标注每次 `loraClient` 发出的 HTTP 请求。完成后，你的追踪表应大致包含：

1. 首次 reconcile：加 finalizer（[modeladapter_controller.go:335-345](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L335-L345)）→ 初始化 Status=Pending（[modeladapter_controller.go:419-428](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L419-L428)）。
2. 第二次 reconcile：`reconcileReplicas` → 单 Pod 模式 → `schedulePods` 选 1 个 Pod → Phase=Scheduled。
3. `reconcileLoading` → `tryLoadModelAdapterOnPod` → `LoadAdapter`：先 **GET `http://<podIP>:8000/v1/models`**，未命中则 **POST `http://<podIP>:8000/v1/load_lora_adapter`**，body 为 `{"lora_name":"text2sql-lora","lora_path":"jeffwan/rank-1"}`（注意 `huggingface://` 已被 `extractHuggingFacePath` 转换）。
4. `reconcileService` 创建 headless Service `text2sql-lora`；`reconcileEndpointSlice` 写入该 Pod IP → Phase=Running。
5. 若用户 `kubectl delete modeladapter text2sql-lora`：`UnloadAdapter` 发 **POST `/v1/unload_lora_adapter`** → 移除 finalizer → CR 与 Service/EndpointSlice 被 GC。

**延伸思考**：如果把 `replicas: 1` 删掉（全量模式），步骤 2 的调度环节会怎样变化？（提示：`reconcileLoadOnAllPods` 是近乎空操作，所有匹配 Pod 都成为加载目标，无需调度器选 Pod。）

> 说明：以上 HTTP 请求路径与 body 结构均可由 [lora_client_test.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/lora_client_test.go) 的断言佐证；若要在真实集群验证端到端行为，需部署带 LoRA 能力的 vLLM，属于**待本地验证**部分。

## 6. 本讲小结

- `ModelAdapter` 控制器把「LoRA 适配器加载/卸载」声明式化：用户提交 CR，控制器负责调度、加载、暴露服务与清理。
- 主循环 `DoReconcile` 是四步管线：**reconcileReplicas（选 Pod）→ reconcileLoading（调引擎加载）→ reconcileService（建 headless Service）→ reconcileEndpointSlice（填 Pod IP）**；外加 10 秒周期同步循环修复漂移。
- `Spec.Replicas` 决定分布：`nil` = 加载到所有匹配 Pod（全量模式），`1` = 调度器选单 Pod（稀疏模式）。
- `loraClient` 是与引擎/边车交互的唯一 HTTP 出口，通过 `BuildURLs` 自动适配「直连 vLLM / 直连 SGLang / 运行时边车」三种目标的端口与路径；加载前先 GET `/v1/models` 实现幂等短路。
- 加载失败用「注解记录重试状态 + 指数退避（基数 5s，封顶 300s，最多 5 次）」稳健重试，并区分可重试/不可重试错误。
- 资源管理：控制器手动维护同名 **headless Service + EndpointSlice**（只含已加载 Pod），并用 **finalizer** 保证删除时先卸载引擎里的适配器；Service/EndpointSlice 靠 OwnerReference 级联 GC。

## 7. 下一步学习建议

- **u4-l2 ModelAdapter 调度策略**：本讲多次提到 `scheduler.SelectPod`，下一讲深入 `scheduling` 包，讲解 `leastAdapters`、`binPack`、`leastLatency`、`leastThroughput`、`random` 五种调度策略的打分逻辑与如何选择。
- **u4-l3 ModelClaim 模型激活与池化策略**：`ModelAdapter` 管「适配器」，`ModelClaim` 管「整模型在引擎上的激活/停用」，二者是并列的模型生命周期原语，建议对比学习。
- **u9-l1 AI Runtime 边车与引擎生命周期**：本讲的「边车路径」把工件下载、凭证处理下放给了 `aibrix-runtime` 边车；想看清 `/v1/lora_adapter/load` 在边车内部如何被处理、如何委托引擎，请阅读运行时边车讲义。
- **继续阅读源码**：带着本讲的调用链，去读 [scheduling/scheduler.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/scheduling/scheduler.go) 与 [modeladapter_controller_test.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller_test.go) 中的集成测试，理解控制器在真实 envtest 集群里的端到端行为。
