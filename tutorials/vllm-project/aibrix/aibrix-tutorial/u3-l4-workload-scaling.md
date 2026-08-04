# 工作负载伸缩与 HPA 资源映射

## 1. 本讲目标

在前三讲里，我们已经走通了 PodAutoscaler 的「大脑」：u3-l1 讲清了 reconcile 主循环与 `DefaultAutoScaler` 如何算出**期望副本数**，u3-l2 讲清了 APA/HPA/KPA 三种算法的公式，u3-l3 讲清了指标如何被采集、聚合并喂给算法。

但「算出期望副本数」和「真正让 Deployment 改变 Pod 数量」之间，还差最后一步——**把决策写回真实工作负载**。本讲就专讲这一步：

- 理解 AIBrix 把「期望副本数」落地到真实工作负载的**两条路径**：HPA 策略委托给 Kubernetes 原生 HPA，KPA/APA 策略由控制器直接改写副本数。
- 读懂 `WorkloadScale` 接口如何以「无状态 + 通用 /scale 对象」的方式读写任意工作负载的副本数。
- 读懂 `makeHPA` 如何把 PodAutoscaler 的声明翻译成一个原生 `HorizontalPodAutoscaler` 对象，并把伸缩速率/冷却窗口编码进 HPA Behavior。
- 掌握 PodAutoscaler 与原生 HPA 的关系、冲突场景与伸缩边界（min/max）的处理。

学完后，你应当能回答：**「我写了一个 PodAutoscaler，最终是谁、用什么方式、改了哪个对象的 `spec.replicas`？」**

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义）：

- **PodAutoscaler CR 的关键字段**（u2-l3）：`ScaleTargetRef`（伸缩谁）、`MinReplicas`/`MaxReplicas`（边界）、`MetricsSources`（看什么指标）、`ScalingStrategy`（HPA/KPA/APA 三选一）。这些字段定义在 [api/autoscaling/v1alpha1/podautoscaler_types.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/podautoscaler_types.go#L53-L98)。
- **reconcile 主循环的分流**（u3-l1）：控制器在 `Reconcile` 末尾按 `ScalingStrategy` 分流到 `reconcileHPA` 或 `reconcileCustomPA`。
- **DefaultAutoScaler 是无状态纯计算**（u3-l1/u3-l2）：它只负责「算」，不负责「写」。

本讲会用到几个 Kubernetes 基础概念，这里先做通俗解释：

- **工作负载（Workload）**：能被「水平扩缩」的对象，最典型的是 `Deployment`，也包括 AIBrix 自有的 `StormService`、KubeRay 的 `RayClusterFleet` 等。它们的共同点是都有一个 `spec.replicas` 字段表示期望副本数。
- **/scale 子资源**：Kubernetes 为可伸缩对象提供的一种标准化接口，专门暴露「副本数」这一个字段，权限粒度比整对象更细。AIBrix **没有**使用它（下文会讲原因）。
- **`unstructured.Unstructured`**：controller-runtime 提供的「通用对象」类型。当你不想为每种资源生成强类型 client 时，可以用它以 `map[string]interface{}` 的方式读写任意 GVK（Group/Version/Kind）的对象。AIBrix 用它来通用化地操作 Deployment、StormService 等不同工作负载。
- **HPA（HorizontalPodAutoscaler）**：Kubernetes 内置的水平自动伸缩器。它自己就是一个控制器，会根据指标不断调整目标工作负载的副本数。AIBrix 在 HPA 策略下并不自己算副本数，而是**生成并维护一个原生 HPA 对象**，把活儿交给 Kubernetes。
- **OwnerReference / 级联删除**：Kubernetes 中对象可以声明「我归谁管」。当 owner 被删除时，其下属对象会被垃圾回收（GC）自动清理。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [pkg/controller/podautoscaler/workload_scale.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/workload_scale.go) | 定义 `WorkloadScale` 接口及其无状态实现，负责**读写任意工作负载的副本数与 Pod 选择器**（KPA/APA 路径的落地执行者）。 |
| [pkg/controller/podautoscaler/hpa_resources.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/hpa_resources.go) | `makeHPA` 把 PodAutoscaler 翻译成原生 `HorizontalPodAutoscaler`；`buildHPABehavior` 把伸缩速率/冷却窗口编码为 HPA Behavior（HPA 路径的「翻译器」）。 |
| [pkg/controller/podautoscaler/podautoscaler_controller.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go) | reconcile 主循环，含 `reconcileHPA` / `reconcileCustomPA` / `computeScaleDecision` / 冲突检测等编排逻辑，把前两个文件「粘」起来。 |
| [api/autoscaling/v1alpha1/podautoscaler_types.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/podautoscaler_types.go) | PodAutoscaler 的 Spec/Status 数据模型，本讲引用其中的 `ScalingStrategy` 枚举与边界字段。 |

## 4. 核心概念与源码讲解

### 4.1 伸缩落地总览：从「期望副本数」到「真实工作负载」的两条路径

#### 4.1.1 概念说明

前三讲我们解决了「算」的问题，本讲解决「写」的问题。一个容易混淆的关键点是：**AIBrix 并不总是自己动手改副本数**。它根据 `ScalingStrategy` 走两条截然不同的落地路径：

| 策略 | 谁来算副本数 | 谁来写副本数 | 落地入口 |
|------|------------|------------|---------|
| **HPA** | Kubernetes 原生 HPA 控制器 | Kubernetes 原生 HPA 控制器 | `reconcileHPA` → 生成并维护 HPA 对象 |
| **KPA / APA** | AIBrix `DefaultAutoScaler` | AIBrix 控制器自己（经 `WorkloadScale`） | `reconcileCustomPA` → 直接改写 `spec.replicas` |

这是本讲最重要的一张对照表。**HPA 策略下，AIBrix 退化为一个「HPA 对象的维护者」**——它不去算副本数，也不去改 Deployment，它只负责「把用户的 PodAutoscaler 声明翻译成一个标准 HPA 对象，并周期性地校正它」，真正的伸缩完全交给 Kubernetes 内置的 HPA 控制器。代码注释把它比作 KEDA——一种「包装标准 K8s HPA 以提供额外能力」的模式。

而 KPA/APA 策略下，AIBrix 才是真正的「全栈伸缩器」：自己算、自己写。

#### 4.1.2 核心流程

分流的入口在 reconcile 主循环的末尾：

```
Reconcile(pa)
  ├── 校验 spec / 冲突检测 / 回写 Status
  └── switch pa.Spec.ScalingStrategy
        ├── HPA           → reconcileHPA(pa)      # 路径一：维护 HPA 对象
        ├── KPA / APA     → reconcileCustomPA(pa)  # 路径二：自己算+自己写
        └── default       → 什么都不做
```

两条路径的共同终点都是某个工作负载的 `spec.replicas` 被改变，只是「执笔人」不同。

#### 4.1.3 源码精读

分流逻辑只有几行，但它是理解整个落地体系的「岔路口」：

[pkg/controller/podautoscaler/podautoscaler_controller.go:311-319](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L311-L319) —— 按 `ScalingStrategy` 分流到 `reconcileHPA` 或 `reconcileCustomPA`；未知策略静默跳过（Status 已在上面更新）。

注意 HPA 与 KPA/APA 是互斥的：同一个 PodAutoscaler 在同一时刻只能走一条路径。

#### 4.1.4 代码实践

**实践目标**：用源码阅读的方式，确认「两条路径」确实存在且互斥。

**操作步骤**：

1. 打开 [podautoscaler_controller.go 的 Reconcile](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L278-L320)，定位 L311 的 `switch`。
2. 分别跳转到 `reconcileHPA`（L715）与 `reconcileCustomPA`（L769）的函数签名，确认二者都接收 `ctx` 与 `pa`，返回 `(ctrl.Result, error)`。
3. 在 `reconcileCustomPA` 内搜索 `SetDesiredReplicas`，确认 KPA/APA 路径**自己**调用了写副本数的方法；而在 `reconcileHPA` 内搜索，确认 HPA 路径**没有**调用它。

**需要观察的现象**：`reconcileHPA` 中只会出现 `makeHPA`、`r.Create`/`r.Update` 针对的是 `HorizontalPodAutoscaler` 对象；`reconcileCustomPA` 中才会出现 `workloadScaleClient.SetDesiredReplicas`。

**预期结果**：你能用一句话指出——「HPA 路径只写 HPA 对象，KPA/APA 路径才直接写工作负载副本数」。

#### 4.1.5 小练习与答案

**练习 1**：如果用户把一个 PodAutoscaler 的 `scalingStrategy` 从 `KPA` 改成 `HPA`，旧路径遗留的副作用是否会被自动清理？

> **参考答案**：不会自动清理此前由 `reconcileCustomPA` 写入的副本数（那是直接落在工作负载 `spec.replicas` 上的）。切换后控制器改走 `reconcileHPA`，开始创建 `{pa.Name}-hpa` 并由原生 HPA 接管后续伸缩；但旧的副本数本身不会被回滚。反过来，从 HPA 切到 KPA 时，先前生成的 HPA 对象因带有指向 PA 的 OwnerReference，会在 PA 删除时被 GC，但在 PA 仅仅「改策略」时并不会被主动删除——这是一个需要人工留意的边界。

---

### 4.2 WorkloadScale：KPA/APA 路径的通用副本读写器

#### 4.2.1 概念说明

KPA/APA 路径下，AIBrix 要自己把期望副本数写到工作负载上。但工作负载种类很多——`Deployment`、`StormService`、`RayClusterFleet`……为每种写一套读写逻辑既冗余又难维护。

`WorkloadScale` 接口就是为了**抽象「读副本数 / 写副本数 / 取 Pod 选择器」这三件事**而生的。它有三个设计要点：

1. **无状态**：所有方法都把 `PodAutoscaler` 作为参数传入，实现体不持有任何与具体 PA 相关的可变状态，因此可被所有 reconcile 并发安全地复用。
2. **基于 `unstructured`**：用通用对象类型读写，避免为每种工作负载生成强类型 client。
3. **不使用 /scale 子资源**：见下方源码注释，是为了简化 RBAC。

接口注释里有一句精辟的职责划分：**`WorkloadScale` 提供「机制」（怎么读写副本数），`AutoScaler` 提供「智能」（算出多少副本）**。

#### 4.2.2 核心流程

`WorkloadScale` 接口共四个方法，构成 KPA/APA 落地的完整工具集：

```
                       PodAutoscaler (KPA/APA)
                               │
        ┌──────────────────────┼──────────────────────┐
        ▼                      ▼                      ▼
 GetCurrentReplicasFromScale  SetDesiredReplicas   GetPodSelectorFromScale
   读：从 scale 对象取当前副本   写：改 spec.replicas    读：取 Pod 标签选择器
        │                      │                      │
        ▼                      ▼                      ▼
   喂给算法算期望副本        落地到工作负载         喂给指标采集器筛 Pod
```

其中「通用伸缩」与「StormService 角色级伸缩」是两条子路径，由 `pa.Spec.SubTargetSelector` 是否为空来区分。

写副本数的核心是「读-改-写」三步，并用 `RetryOnConflict` 包裹以应对并发冲突：

```
SetDesiredReplicas(replicas):
  RetryOnConflict:
    1. 解析 GVK
    2. Get 当前 scale 对象（unstructured）
    3. SetNestedField(spec.replicas = replicas)
    4. Update 回 API Server
```

#### 4.2.3 源码精读

接口定义与「机制 vs 智能」的注释：

[pkg/controller/podautoscaler/workload_scale.go:44-62](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/workload_scale.go#L44-L62) —— 四个方法：`Validate` / `GetCurrentReplicasFromScale` / `SetDesiredReplicas` / `GetPodSelectorFromScale`。

**读副本数**（通用路径）：直接从 unstructured 对象里取 `spec.replicas`：

[pkg/controller/podautoscaler/workload_scale.go:132-140](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/workload_scale.go#L132-L140) —— 用 `unstructured.NestedInt64` 取 `spec.replicas`，找不到则报错。

**写副本数**（通用路径），是本模块最关键的一段：

[pkg/controller/podautoscaler/workload_scale.go:185-232](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/workload_scale.go#L185-L232) —— `SetDesiredReplicas` 通用分支：用 `schema.ParseGroupVersion` 解析 GVK，构造 unstructured 对象，Get 后用 `unstructured.SetNestedField` 改 `spec.replicas`，再 `Update`。

注意 L198 的 `retry.RetryOnConflict`：当多个控制器并发修改同一对象导致 `resourceVersion` 冲突时，它会自动重新 Get 再重试，这是写工作负载时的并发安全网。

特别注意 L224 的注释——**为什么不走 /scale 子资源**：

[pkg/controller/podautoscaler/workload_scale.go:223-226](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/workload_scale.go#L223-L226) —— 注释说明：本可以走 scale API，但它要求额外的 `/scale` RBAC 权限；为了简化部署场景，选择直接 `Update` 整对象。这是一个典型的「便利性 vs 最小权限」取舍。

**取 Pod 选择器**（通用路径），用于指标采集时筛出属于该工作负载的 Pod：

[pkg/controller/podautoscaler/workload_scale.go:330-358](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/workload_scale.go#L330-L358) —— 先试 `status.selector`（字符串格式，/scale 子资源常用），再退回 `spec.selector`（LabelSelector 格式），转换成 `labels.Selector` 返回。

> 补充：当目标是 `StormService` 且带 `SubTargetSelector.RoleName` 时，走的是角色级分支 `getCurrentReplicasForRole` / `setDesiredReplicasForRole` / `getPodSelectorForRole`（[workload_scale.go:143-183](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/workload_scale.go#L143-L183)、[234-272](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/workload_scale.go#L234-L272)、[274-320](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/workload_scale.go#L274-L320)）。它通过标签 `storm-service-name` + `role-name`（[pkg/controller/constants/stormservice.go:25-31](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/constants/stormservice.go#L25-L31)）定位某角色的 Pod，并把副本数写到 `StormService` 的对应 role 上。本讲只把它当作「通用机制的一个特化」来理解，细节留待 u5-l2。

#### 4.2.4 代码实践

**实践目标**：通过跟踪 `reconcileCustomPA` 的五步流程，亲眼看到 `WorkloadScale` 如何被调用、把期望副本数写进工作负载。

**操作步骤**：

1. 打开 [reconcileCustomPA](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L769-L845)，依次定位注释里的 Step 1 ~ Step 5。
2. 在 Step 2（L789）确认它调用了 `workloadScaleClient.GetCurrentReplicasFromScale`。
3. 在 Step 4（L820）确认它调用了 `workloadScaleClient.SetDesiredReplicas`。
4. 对照 4.2.3 中的源码，确认这两个方法内部最终都在操作工作负载的 `spec.replicas`。

**需要观察的现象**：Step 4 的 `SetDesiredReplicas` 只在 `scaleDecision.ShouldScale` 为 `true`（即期望副本数与当前不同）时才被调用；否则只更新 Status，不触发实际写操作。

**预期结果**：你能画出 `reconcileCustomPA` 的五步数据流：`getScaleResource → GetCurrentReplicasFromScale → computeScaleDecision → SetDesiredReplicas → setStatus`，并指出 `WorkloadScale` 介入了其中的第 2、4 步。

#### 4.2.5 小练习与答案

**练习 1**：`SetDesiredReplicas` 为什么要用 `RetryOnConflict` 包裹「Get-改-Update」？如果不用会怎样？

> **参考答案**：因为 controller-runtime 的客户端使用乐观并发——对象带 `resourceVersion`，若在 Get 与 Update 之间对象被别人改过，Update 会因 `resourceVersion` 不匹配返回 `Conflict` 错误。`RetryOnConflict` 会自动重新 Get 拿到最新版本再改再写。若不用，遇到并发写（例如原生 HPA 控制器与 AIBrix 同时改同一 Deployment，或多个 reconcile 叠加）时会直接失败、副本数写不进去。

**练习 2**：代码注释说「不走 /scale 子资源是为了省 RBAC」。这种取舍的代价是什么？

> **参考答案**：代价是需要对**整类工作负载对象**（如 `deployments`）拥有 `update` 权限，而非仅对 `/scale` 子资源授权。权限粒度变粗，意味着控制器理论上能改该对象的任意字段（尽管代码只改 `spec.replicas`）。好处是部署时不必为每个目标资源单独配置 `/scale` 的 RBAC，简化安装。

---

### 4.3 HPA 资源映射：把 PodAutoscaler 翻译成原生 HPA

#### 4.3.1 概念说明

当 `ScalingStrategy=HPA` 时，AIBrix 的任务从「自己伸缩」变成「生成一个 Kubernetes 能理解的原生 HPA 对象」。这个翻译工作由 `makeHPA` 完成。

翻译的关键映射关系：

| PodAutoscaler 字段 | 映射到 HPA 的位置 |
|---|---|
| `metadata.name` | HPA 名字 = `{pa.Name}-hpa` |
| （PA 自身） | HPA 的 `ownerReferences` 指向该 PA（控制器） |
| `ScaleTargetRef` | HPA 的 `scaleTargetRef`（伸缩目标不变） |
| `MaxReplicas`（为 0 时取 `MaxInt32`） | HPA 的 `maxReplicas` |
| `MinReplicas` | HPA 的 `minReplicas` |
| `MetricsSources` | HPA 的 `metrics`（按指标名分情况翻译） |
| ScalingContext（速率/冷却窗口） | HPA 的 `behavior` |

其中指标翻译有三种情况，由 `TargetMetric` 的名字决定：
- `cpu` → 资源指标，按 **平均利用率（AverageUtilization）** 伸缩；
- `memory` → 资源指标，按 **平均值（AverageValue）** 伸缩；
- 其它任意自定义指标 → Pods 指标，按 **平均值（AverageValue）** 伸缩。

`makeHPA` 还会做基础校验：`maxReplicas` 必须 ≥ `minReplicas`，且至少要有一个指标源。

#### 4.3.2 核心流程

```
reconcileHPA(pa):
  1. createScalingContext(pa)            # 汇总 min/max、速率、冷却窗口
  2. makeHPA(pa, scalingContext)          # 翻译出期望的 HPA 对象
  3. Get 现有的 {pa.Name}-hpa
       ├── 不存在  → Create(hpa)
       ├── 出错    → 返回错误并 requeue
       └── 已存在  → Update(hpa)          # 用期望态覆盖现有态
  4. 把 HPA 的 Status（CurrentReplicas/DesiredReplicas/Conditions）镜像回 PA.Status
```

`buildHPABehavior` 把 AIBrix 自有的「速率」概念翻译成 HPA 的百分比策略：

- 扩容速率 `maxScaleUpRate = 2.0`（表示「最多翻倍」）→ 转化为「每 60 秒最多增加 100%」：
  \[
  \text{scaleUpPercent} = (\text{maxScaleUpRate} - 1.0) \times 100 = (2.0 - 1.0)\times 100 = 100
  \]
- 缩容速率 `maxScaleDownRate = 2.0`（表示「最多减半」）→ 转化为「每 60 秒最多减少 50%」：
  \[
  \text{scaleDownPercent} = \left(1.0 - \frac{1.0}{\text{maxScaleDownRate}}\right) \times 100 = \left(1.0 - 0.5\right)\times 100 = 50
  \]

方向上的策略也有讲究：扩容选 `MaxChangePolicySelect`（取多个策略里变化最大的，鼓励快速扩容），缩容选 `MinChangePolicySelect`（取变化最小的，鼓励温和缩容）。

#### 4.3.3 源码精读

**`makeHPA` 主体**——名字、OwnerReference、目标、边界：

[pkg/controller/podautoscaler/hpa_resources.go:36-68](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/hpa_resources.go#L36-L68) —— HPA 名字固定为 `fmt.Sprintf("%s-hpa", pa.Name)`（L44）；`OwnerReferences` 用 `metav1.NewControllerRef` 指向该 PA（L48-50），这意味着**删除 PA 时该 HPA 会被级联 GC**；`maxReplicas` 为 0 时取 `math.MaxInt32`（L38-40，等价于「不设上限」）；并在 L65 校验 max ≥ min。

**指标翻译的三分支**：

[pkg/controller/podautoscaler/hpa_resources.go:82-123](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/hpa_resources.go#L82-L123) —— `switch strings.ToLower(source.TargetMetric)`：`cpu` 用 `ResourceMetricSourceType` + `UtilizationMetricType`（L83-94）；`memory` 用 `ResourceMetricSourceType` + `AverageValueMetricType`（L96-107）；默认用 `PodsMetricSourceType` + `AverageValueMetricType`（L109-122）。注意 CPU 的目标值会被 `math.Ceil` 向上取整为整数百分比（L84）。

**`buildHPABehavior` 的速率→百分比换算**：

[pkg/controller/podautoscaler/hpa_resources.go:131-190](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/hpa_resources.go#L131-L190) —— L140-143 算扩容百分比并对非法值兜底为 100%；L147-151 算缩容百分比并同样兜底；L154-155 选定扩容取 Max、缩容取 Min 的策略方向。

**控制器侧的 reconcileHPA 编排**：

[pkg/controller/podautoscaler/podautoscaler_controller.go:715-766](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L715-L766) —— 调 `makeHPA` 生成期望态（L720），按「不存在则 Create / 已存在则 Update」校正集群中的 HPA（L730-752），最后把 HPA 的 `Status` 镜像回 PA（L755-763）。注意：PA 在 HPA 策略下的 `DesiredScale`/`ActualScale` 完全来自 HPA 的 `Status`，AIBrix 自己不计算。

#### 4.3.4 代码实践

**实践目标**：用一个具体的 PodAutoscaler 例子，手工推演 `makeHPA` 会生成什么样的 HPA。

**操作步骤**：假设有如下 PodAutoscaler（示例，非项目原有 CR）：

```yaml
# 示例代码：仅用于推演，非仓库内现有对象
apiVersion: autoscaling.aibrix.ai/v1alpha1
kind: PodAutoscaler
metadata:
  name: my-model-pa
  namespace: default
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: my-model
  minReplicas: 2
  maxReplicas: 10
  scalingStrategy: HPA
  metricsSources:
    - targetMetric: cpu
      targetValue: "60"
```

1. 根据 [hpa_resources.go:36-68](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/hpa_resources.go#L36-L68) 推断生成的 HPA 名字、`ownerReferences`、`minReplicas`、`maxReplicas`、`scaleTargetRef`。
2. 根据 [hpa_resources.go:82-94](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/hpa_resources.go#L82-L94) 推断 `metrics` 字段：CPU 目标 60 会变成 `AverageUtilization: 60`（因 `math.Ceil(60)=60`）。

**需要观察的现象**：生成的 HPA 应名为 `my-model-pa-hpa`，`ownerReferences` 指向 `my-model-pa`，`metrics` 是一个 CPU 资源指标、目标平均利用率 60%。

**预期结果**（待本地验证）：在装有 AIBrix 的集群里 `kubectl apply` 上述 PA 后，`kubectl get hpa my-model-pa-hpa -oyaml` 应能看到与推演一致的 `scaleTargetRef`、`min/maxReplicas` 与 `metrics`，以及 `behavior` 中扩容 100%/缩容 50% 的百分比策略。

#### 4.3.5 小练习与答案

**练习 1**：为什么 CPU 用「平均利用率（AverageUtilization）」而 memory 与自定义指标用「平均值（AverageValue）」？

> **参考答案**：这是 Kubernetes HPA 的惯例。CPU 通常以「占 request 的百分比」表达目标（如「希望 CPU 利用率 60%」），所以用 `AverageUtilization`，HPA 控制器会自动除以 request；而内存和自定义指标没有统一的「request 基准」，只能直接给定一个绝对的目标平均值（如「希望每 Pod 平均消耗 2Gi 内存」），所以用 `AverageValue`。`makeHPA` 的三分支正是对这一惯例的遵循。

**练习 2**：`buildHPABehavior` 里扩容用 `MaxChangePolicySelect`、缩容用 `MinChangePolicySelect`，这种不对称体现了什么设计取向？

> **参考答案**：体现了「扩容从快、缩容从稳」的运维取向——扩容时取变化最大的策略，尽快缓解过载；缩容时取变化最小的策略，避免因指标短暂回落就急于回收 Pod 导致震荡。这与 ScalingContext 默认「扩容无冷却、缩容冷却 5 分钟」（[context.go:112-113](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/context/context.go#L112-L113)）是一致的设计意图。

---

### 4.4 冲突处理与伸缩边界（min/max）

#### 4.4.1 概念说明

副本数真正落地前，还有两道关卡：**冲突检测**与**伸缩边界**。

**冲突**有两种：

1. **PA 与 PA 的冲突**：两个 PodAutoscaler 同时盯上同一个工作负载。AIBrix **会检测**这种情况——只有第一个 PA「认领」目标成功，第二个会被判为冲突、拒绝生效。这是一个**内存级**的认领机制（基于 `scalingTargetToPA` 这张 map）。
2. **PA 与原生 HPA 的冲突**：一个 PodAutoscaler（KPA/APA）与一个用户手写的原生 HPA 同时盯上同一个 Deployment。AIBrix **不检测**这种跨类型冲突——二者都会写 `spec.replicas`，可能互相打架。这是部署时需要人工避免的隐患。

**伸缩边界（min/max）**则是兜底安全网：无论算法算出多少，最终副本数都会被夹在 `[MinReplicas, MaxReplicas]` 区间内。边界检查分两处：
- `reconcileCustomPA` 路径在 `computeScaleDecision` 中做边界裁剪；
- `reconcileHPA` 路径把 min/max 直接写进 HPA 的 `minReplicas`/`maxReplicas`，由原生 HPA 控制器负责遵守。

#### 4.4.2 核心流程

**PA-to-PA 冲突检测**（只认领「工作负载」维度）：

```
checkNoMultiPodAutoscalerConflict(pa):
  key = <apiVersion>.<Kind>/<ns>/<name>[/<roleName>]   # 工作负载维度的 key
  if key 已被别的 PA 认领:        → 判冲突，写 ConditionConflict，不生效
  else:                          → 当前 PA 认领 key，放行
```

注意：key 是按「被伸缩的工作负载」算的，**与 HPA 无关**。一个独立的原生 HPA 不在这张表里，因此不会触发此检测。

**KPA/APA 的边界与稳定化**（`computeScaleDecision`）：

```
computeScaleDecision(pa, currentReplicas):
  ① 若 current==0 且 min!=0       → 伸缩禁用（return 0, 不动作）
  ② 若 current > max              → 缩到 max      （边界裁剪）
  ③ 若 current < min              → 扩到 min      （边界裁剪）
  ④ 否则交给算法算 metricDesired
  ⑤ 稳定化（cooldown 窗口）        → 扩容取窗口内 max、缩容取窗口内 min
  ⑥ 再次夹到 [min, max]            （最终边界裁剪）
```

稳定化（stabilization）模仿 Kubernetes 原生 HPA 的行为：扩容时取冷却窗口内**最大**的推荐值（尽快扩容），缩容时取窗口内**最小**的推荐值（缓慢缩容），用历史推荐值的滑动窗口平滑抖动。

#### 4.4.3 源码精读

**PA-to-PA 冲突认领**：

[pkg/controller/podautoscaler/podautoscaler_controller.go:359-388](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L359-L388) —— `checkNoMultiPodAutoscalerConflict`：若 `scalingTargetToPA[key]` 已被别的 PA 占据，返回 `invalid(ConditionConflict, ...)`；否则由当前 PA 认领。key 的构造见 [buildScalingTargetKey:330-346](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L330-L346)，StormService 还会拼上 `roleName` 以区分角色级目标。

**边界裁剪与禁用判断**：

[pkg/controller/podautoscaler/podautoscaler_controller.go:1041-1073](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L1041-L1073) —— `computeScaleDecision` 开头的三道边界闸：`current==0 && min!=0` 判为禁用、`current>max` 缩到 max、`current<min` 扩到 min，均带 `Algorithm: "boundary-check"` 标记。

**最终边界夹取**：

[pkg/controller/podautoscaler/podautoscaler_controller.go:1096-1104](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L1096-L1104) —— 算法结果经稳定化后再一次被夹到 `[min, max]`，并打日志说明裁剪动作。

**稳定化窗口**：

[pkg/controller/podautoscaler/podautoscaler_controller.go:1180-1261](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L1180-L1261) —— `stabilizeRecommendation`：按扩缩方向选窗口（扩容用 scaleUpWindow 且 `selectMax=true`，缩容用 scaleDownWindow 且 `selectMax=false`），在窗口内的历史推荐里取极值。注意 L1089 限定：稳定化只对 KPA/APA 生效，HPA 路径不经过这里（HPA 的稳定化由原生 HPA 自己的 `behavior` 负责）。

**HPA 路径的边界**：直接由 [hpa_resources.go:62-68](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/hpa_resources.go#L62-L68) 写进 HPA 的 `minReplicas`/`maxReplicas`，并在此处校验 `max ≥ min`。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：说清「PodAutoscaler 与原生 HPA 同时存在」时的真实行为，并指出 `hpa_resources.go` 及其调用方如何处理二者关系。这是本讲指定的实践任务。

**操作步骤**：

1. **先厘清命名**：阅读 [hpa_resources.go:43-50](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/hpa_resources.go#L43-L50)。AIBrix 在 HPA 策略下生成的 HPA 名字固定为 `{pa.Name}-hpa`，并带一条指向 PA 的 `OwnerReference`（controller=true）。所以「同名」要分两种情况讨论：(a) 用户手写的 HPA 恰好也叫 `{pa.Name}-hpa`；(b) 用户手写的 HPA 名字不同、但 `scaleTargetRef` 指向同一个 Deployment。

2. **追踪 reconcileHPA 对「同名 HPA」的处理**：阅读 [podautoscaler_controller.go:730-752](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L730-L752)。可见逻辑是朴素的「不存在则 Create，已存在则 Update」——它**并不先检查现有 HPA 的 ownerReferences 是否属于本 PA**。结论：若用户预先手写了一个叫 `{pa.Name}-hpa` 的 HPA，AIBrix 会在每次 reconcile 时**用 `makeHPA` 的产物覆盖它的 spec**，并因产物自带 OwnerReference 而**把它「领养」为该 PA 的下属对象**（PA 删除时它会被 GC）。

3. **追踪对「不同名但同目标 HPA」的处理**：阅读 [checkNoMultiPodAutoscalerConflict](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L359-L388) 与 [buildScalingTargetKey](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L330-L346)。可见冲突表只认「PodAutoscaler 之间」对同一工作负载的争夺，**完全不感知原生 HPA 的存在**。结论：若一个 PA 用 KPA/APA 策略伸缩 Deployment X，同时用户又手写了一个原生 HPA 伸缩 Deployment X，两者**都会写 `spec.replicas` 并互相覆盖**——AIBrix 不会拦截。

4. **追踪 HPA 事件如何回灌**：阅读 [filterHPAObject:173-198](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L173-L198) 与 [Watches(...HorizontalPodAutoscaler...):210](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L208-L212)。控制器虽然 watch 了所有 HPA，但 `filterHPAObject` 只会把「ownerReference 指向某 PodAutoscaler 且 controller=true」的 HPA 事件翻译成对应 PA 的 reconcile 请求——**外来的、不被 PA 拥有的 HPA 事件会被直接丢弃**。

**需要观察的现象**：把上述四步串起来，你会发现 `hpa_resources.go`（`makeHPA`）本身**并不做任何冲突判定**——它只负责「按 PA 的声明产出期望的 HPA」。真正的「关系处理」分散在三处：①命名约定 `{pa.Name}-hpa`；②OwnerReference 带来的级联 GC；③控制器的「不存在则建、存在则覆盖」校正逻辑。而对「PA vs 不同名原生 HPA」这种最危险的冲突，系统**没有任何防护**。

**预期结果**：用一段话总结——

> 当 PodAutoscaler 与原生 HPA「同名」（即原生 HPA 也叫 `{pa.Name}-hpa`）时，AIBrix 不会报错，而是按「存在则覆盖」的规则在每次 reconcile 用 `makeHPA` 的产物重写其 spec 并通过 OwnerReference 领养它，真正的伸缩交给原生 HPA 控制器。当 PodAutoscaler（KPA/APA）与一个**不同名但同目标**的原生 HPA 共存时，AIBrix 的冲突检测（`checkNoMultiPodAutoscalerConflict`，只查 PA-to-PA）感知不到它，二者会并发抢写同一 Deployment 的 `spec.replicas`，造成伸缩震荡——这是部署时必须人工避免的场景。`hpa_resources.go` 在其中只承担「翻译」职责，不承担「冲突仲裁」职责。

（本结论为源码静态分析结果，**待本地验证**：可在测试集群中分别构造两种场景，用 `kubectl get deployment <name> -w` 观察副本数是否出现来回跳变。）

#### 4.4.5 小练习与答案

**练习 1**：为什么 `checkNoMultiPodAutoscalerConflict` 检测不出「PA + 原生 HPA 争抢同一 Deployment」？

> **参考答案**：因为该检测的 key（`buildScalingTargetKey`）和认领表（`scalingTargetToPA`）都是「PodAutoscaler 对工作负载」的映射——它只在 reconcile PodAutoscaler 时读写，根本不去 list 集群里的原生 HPA。原生 HPA 不在这张表里登记，自然不会被判冲突。要解决就得额外 list 同名空间的 HPA 并比对 `scaleTargetRef`，目前代码未实现。

**练习 2**：`computeScaleDecision` 里 `current==0 && min!=0` 为何要判为「伸缩禁用」而不直接扩到 min？

> **参考答案**：`current==0` 通常意味着用户主动把工作负载缩到 0（例如「关停省资源」）。此时若 `min!=0`（即用户并未要求「允许缩到 0」），代码把它理解为「处于手动关停状态，不要被自动唤醒」，于是返回 `ShouldScale=false`、保持 0，避免控制器违背用户意图强行把副本拉起来。这是一种「0 副本保护」。

**练习 3**：HPA 策略下，伸缩边界（min/max）由谁强制？

> **参考答案**：由 Kubernetes 原生 HPA 控制器强制。AIBrix 只在 `makeHPA` 里把 `MinReplicas`/`MaxReplicas` 写进 HPA 对象的对应字段（并做一次 `max≥min` 的校验），之后就不再插手；真正把副本数夹在区间内的是原生 HPA 控制器。这与 KPA/APA 路径不同——后者由 AIBrix 自己在 `computeScaleDecision` 里做边界裁剪。

---

## 5. 综合实践

把本讲四条线索串起来，完成下面这个贯穿性任务：**为同一个 Deployment 设计「HPA 策略」与「KPA 策略」两套 PodAutoscaler，对比它们的落地链路差异**。

**任务步骤**：

1. 选定一个目标 Deployment（可复用 `samples/quickstart/model.yaml` 部署出的模型 Deployment）。
2. 写两份 PodAutoscaler（示例代码，非项目原有 CR）：
   - PA-A：`scalingStrategy: HPA`，`metricsSources` 用 `cpu` 资源指标；
   - PA-B：`scalingStrategy: KPA`，`metricsSources` 用一个 pod 类型的自定义指标（如 `kv_cache_utilization`）。
3. 对 PA-A，画出落地链路：`reconcileHPA → makeHPA → Create/Update {pa.Name}-hpa → 原生 HPA 控制器改 Deployment.spec.replicas`。标注：边界由谁守、副本数由谁算、Status 从哪来。
4. 对 PA-B，画出落地链路：`reconcileCustomPA → WorkloadScale.GetCurrentReplicasFromScale → computeScaleDecision（边界裁剪 + 稳定化）→ WorkloadScale.SetDesiredReplicas → 改 Deployment.spec.replicas`。标注：边界由谁守、副本数由谁算、Status 从哪来。
5. 回答关键问题：这两份 PA 能同时存在吗？为什么？（提示：联系 4.4 的 PA-to-PA 冲突检测——它们盯上的是同一个工作负载 key。）

**预期产出**：一张双栏对照表，清楚标出两条路径在「算副本数的人 / 写副本数的人 / 边界守门人 / Status 来源 / 冲突约束」五个维度上的不同；并得出结论——PA-A 与 PA-B 不能同时生效于同一 Deployment，因为第二个会被 `checkNoMultiPodAutoscalerConflict` 判为冲突。（集群侧验证为「待本地验证」。）

## 6. 本讲小结

- AIBrix 把期望副本数落地有**两条互斥路径**：HPA 策略只维护一个原生 HPA 对象（活儿交给 K8s），KPA/APA 策略才由控制器自己改写 `spec.replicas`。
- `WorkloadScale` 是 KPA/APA 路径的**通用、无状态**副本读写器，用 `unstructured` 操作任意工作负载，写时用 `RetryOnConflict` 应对并发，并**刻意不走 /scale 子资源**以简化 RBAC。
- `makeHPA` 把 PodAutoscaler **翻译**成原生 HPA：名字 `{pa.Name}-hpa`、OwnerReference 指向 PA（级联 GC）、指标按 cpu/memory/自定义三分支翻译、速率与冷却窗口被 `buildHPABehavior` 编码成百分比策略（扩容取 Max、缩容取 Min）。
- 冲突检测只覆盖 **PA-to-PA**（按工作负载维度的内存认领表），**不覆盖 PA 与原生 HPA**；`hpa_resources.go` 只翻译不仲裁，同目标的原生 HPA 是部署隐患。
- 伸缩边界（min/max）在 KPA/APA 路径由 `computeScaleDecision` 双重裁剪（先边界闸、算法后再夹取）外加稳定化窗口，在 HPA 路径则直接写进 HPA 字段、交由原生 HPA 控制器遵守。
- 稳定化（`stabilizeRecommendation`）模仿原生 HPA：扩容取冷却窗口内 max、缩容取 min，且只对 KPA/APA 生效。

## 7. 下一步学习建议

本讲把「算→写」的最后一步补齐了。接下来可以按两个方向深入：

- **横向——StormService 角色级伸缩**：本讲多次提到 `WorkloadScale` 的角色级分支（`SubTargetSelector.RoleName`）。建议进入 **u5-l2（StormService 拓扑与 Prefill/Decode 解耦编排）**，理解 StormService/RoleSet/PodSet 三件套，再看本讲的 `setDesiredReplicasForRole`、`getPodSelectorForRole` 会非常自然。
- **纵向——伸缩算法的「最后一公里」**：本讲反复出现的 `ScalingContext`（速率、冷却窗口）来自 [pkg/controller/podautoscaler/context](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/context/context.go)，建议通读它，弄清 PA 的注解如何覆盖默认值，从而把 u3-l1~u3-l4 的伸缩体系彻底打通。

如果你对「网关侧如何消费这些副本/Pod 信息」感兴趣，也可以跳到 **u6（中央缓存与 Pod 发现）**，看 `pkg/cache` 如何把工作负载的 Pod 状态暴露给网关路由算法。
