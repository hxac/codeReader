# StormService 拓扑与 Prefill/Decode 解耦编排

## 1. 本讲目标

学完本讲后，你应当能够：

- 说出 **StormService、RoleSet、PodSet** 三个自定义资源（CR）各自描述什么、彼此如何嵌套。
- 画出「StormService → RoleSet → PodSet → Pod」的**四层归属链**，并标注每一层由哪个控制器负责、改的是什么对象。
- 解释 RoleSet 控制器如何用 `podGroupSize` 字段在「直接管 Pod」与「管一组原子的 PodSet」两条路径之间分叉。
- 用一个 **Prefill/Decode 分离**（PD 解耦）的例子，把拓扑 CR、角色副本编排、解耦拓扑三个最小模块串起来。
- 理解 `TopologyPolicy` 如何把推理拓扑（按节点/可用区共置）注入到生成的 Pod 上。

本讲承接 [u5-l1 分布式推理与 KubeRay 集成](u5-l1-distributed-inference-kuberay.md)：u5-l1 讲的是「一个模型实例需要多卡多机协同（张量并行）」，本讲讲的是「一个推理服务由多个**职责不同**的角色协同（如 prefill 角色 + decode 角色）」。二者解决的问题不同，但都建立在「高层 CR + 多层控制器」的同一个编排范式之上。

## 2. 前置知识

阅读本讲前，你需要了解：

- **自定义资源 (CRD) 与控制器 (Controller)**：Kubernetes 通过 CRD 定义新对象类型，通过控制器把「期望状态 (Spec)」收敛成「实际状态 (Status)」。详见 u2-l1、u2-l3。
- **OwnerReference 与级联删除**：子对象通过 `ownerReferences` 指向父对象，父对象被删除时子对象会被垃圾回收 (GC) 一并删除。这是三层归属链能「干净回收」的根基。
- **controller-runtime 的 For / Owns**：`For` 声明控制器主对象，`Owns` 声明它「拥有」的子对象（子对象变化也会触发 reconcile）。详见 u2-l1。
- **大模型推理的两个阶段**：
  - **Prefill（预填充）**：处理输入 prompt，计算并填充 KV Cache，是**计算密集**的。
  - **Decode（解码）**：逐个生成输出 token，是**显存带宽密集**的。
  - 把两个阶段拆到不同引擎（甚至不同硬件）上跑，叫 **Prefill/Decode 解耦（PD Disaggregation）**，能显著提升吞吐——这正是本讲拓扑要支撑的核心场景。

> 术语提示：本讲的「Role（角色）」不是 Kubernetes RBAC 里的 Role，而是「职责角色」，例如 `prefill` 角色、`decode` 角色。一个 StormService 里可以包含任意多个这样的角色。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [api/orchestration/v1alpha1/stormservice_types.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/stormservice_types.go) | StormService CR 的 Spec/Status 数据模型 |
| [api/orchestration/v1alpha1/roleset_types.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/roleset_types.go) | RoleSet CR + RoleSpec + TopologyPolicy 数据模型 |
| [api/orchestration/v1alpha1/podset_types.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/podset_types.go) | PodSet CR（内部 API）数据模型 |
| [pkg/controller/stormservice/stormservice_controller.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/stormservice/stormservice_controller.go) | StormService 控制器：reconcile 主循环 |
| [pkg/controller/stormservice/sync.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/stormservice/sync.go) | StormService：扩缩容 / 滚动 / 状态聚合 |
| [pkg/controller/stormservice/rolesetoperations.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/stormservice/rolesetoperations.go) | StormService：渲染并创建/更新/删除 RoleSet |
| [pkg/controller/roleset/roleset_controller.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/roleset/roleset_controller.go) | RoleSet 控制器：reconcile 主循环 |
| [pkg/controller/roleset/sync.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/roleset/sync.go) | RoleSet：选择 RollingManager、计算状态 |
| [pkg/controller/roleset/rolesyncer.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/roleset/rolesyncer.go) | RoleRollingSyncer 接口 + 按 podGroupSize 选择同步器 |
| [pkg/controller/roleset/podset_rollsyncer.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/roleset/podset_rollsyncer.go) | **PodSetRoleSyncer**：RoleSet → PodSet 的核心桥梁 |
| [pkg/controller/roleset/rolling.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/roleset/rolling.go) | Sequential / Parallel / Interleave 三种滚动编排 |
| [pkg/controller/podset/podset_controller.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podset/podset_controller.go) | PodSet 控制器：创建/修复原子 Pod 组 |
| [pkg/controller/constants/stormservice.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/constants/stormservice.go) | 贯穿三层的标签 / 注解 / 环境变量常量 |
| [samples/orchestration/topology-policy/roleset-hostname-required.yaml](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/samples/orchestration/topology-policy/roleset-hostname-required.yaml) | PD 双角色 + podGroupSize + TopologyPolicy 示例 |

---

## 4. 核心概念与源码讲解

### 4.1 拓扑 CR 三件套：StormService / RoleSet / PodSet

#### 4.1.1 概念说明

AIBrix 的 StormService 子系统借鉴了 Kubernetes **StatefulSet / Deployment** 的设计哲学，但把它推广到了「一个服务由多种角色的 Pod 组成」的场景。它用三个层层嵌套的 CR 来描述推理拓扑：

- **StormService**：最外层。对应一个完整的推理服务（如 `vllm-sim-pd`）。它声明「我要几个副本（RoleSet）」「用什么模板」「用什么更新策略」。它的地位类似 Deployment：负责版本管理与滚动升级。
- **RoleSet**：中间层。一个 RoleSet 是「一组角色的副本」。它的 `spec.roles` 是一个**角色列表**，每个角色有自己的名字（如 `prefill`、`decode`）、副本数和 Pod 模板。一个 StormService 副本 = 一个 RoleSet。
- **PodSet**：最内层（用户通常不直接写它）。它代表「一组必须同生共死的原子 Pod」。当一个角色需要多卡多机（`podGroupSize > 1`）时，RoleSet 控制器会为该角色的**每个副本**创建一个 PodSet，PodSet 控制器再创建 `podGroupSize` 个 Pod。PodSet 的源码注释明确写道它是「internal API」。

一句话概括三层职责：

> StormService 管「版本与副本数」→ RoleSet 管「角色与每个角色的 Pod」→ PodSet 管「一个多 Pod 的原子组」。

#### 4.1.2 核心流程

三者的嵌套关系与「谁创建谁」如下：

```text
StormService (用户创建)
  └─owns─► RoleSet (StormService 控制器创建, 1 个副本对应 1 个 RoleSet)
              │  spec.roles[]:
              │    - name: prefill, replicas: N, podGroupSize: 1  → 直接管 Pod
              │    - name: decode,  replicas: M, podGroupSize: k  → 管 PodSet
              └─owns─► PodSet (RoleSet 控制器创建, 仅当 podGroupSize > 1)
                          │  每个 PodSet 含 podGroupSize 个 Pod
                          └─owns─► Pod (PodSet 控制器创建)
```

关键判断点在 `podGroupSize`：

- `podGroupSize` 未设或 `<= 1`：该角色的一个副本 = 一个 Pod，RoleSet 控制器**直接**创建/管理 Pod。
- `podGroupSize > 1`：该角色的一个副本 = 一组（`podGroupSize` 个）必须协同的 Pod，RoleSet 控制器创建一个 **PodSet**，再由 PodSet 控制器把这组 Pod 原子地拉起。

#### 4.1.3 源码精读

**(1) StormService 数据模型**——它的 Spec 几乎是 Deployment 的翻版：

[api/orchestration/v1alpha1/stormservice_types.go:25-60](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/stormservice_types.go#L25-L60) 定义 `StormServiceSpec`，关键字段：`Replicas`（期望 RoleSet 数）、`Selector`（标签选择器，必须匹配 template 的 label）、`Template`（RoleSet 模板）、`UpdateStrategy`、`Paused`。注意它的 `Template` 类型是 `RoleSetTemplateSpec`——即 StormService 的模板**就是**一个 RoleSet 的模板，直接体现了「StormService 副本 = RoleSet」。

[api/orchestration/v1alpha1/stormservice_types.go:149-171](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/stormservice_types.go#L149-L171) 定义两种更新策略：`RollingUpdate`（新建新版 RoleSet、逐步删旧版）与 `InPlaceUpdate`（就地改 RoleSet，用于池化模式）。

[api/orchestration/v1alpha1/stormservice_types.go:173-180](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/stormservice_types.go#L173-L180) 的 kubebuilder 标记很关键：`subresource:scale` 声明了 scale 子资源（`specpath=.spec.replicas`），意味着可以用 `kubectl scale stormservice` 直接伸缩；`printcolumn` 决定 `kubectl get stormservice` 显示哪些列。

**(2) RoleSet 数据模型**——它把「角色列表」作为核心：

[api/orchestration/v1alpha1/roleset_types.go:29-42](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/roleset_types.go#L29-L42) 定义 `RoleSetSpec`：`Roles []RoleSpec`（角色列表）、`UpdateStrategy`（Parallel/Sequential/Interleave）、`TopologyPolicy`。

[api/orchestration/v1alpha1/roleset_types.go:197-234](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/roleset_types.go#L197-L234) 定义单个 `RoleSpec`：`Name`、`Replicas`、`UpgradeOrder`（升级顺序）、`PodGroupSize`、`Template`。其中 `PodGroupSize` 的注释（L213-216）写明「For multi-node inference, set > 1」——这正是触发 PodSet 路径的开关。

**(3) PodSet 数据模型**——内部 API：

[api/orchestration/v1alpha1/podset_types.go:26-47](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/podset_types.go#L26-L47) 定义 `PodSetSpec`：`PodGroupSize`（用 `+kubebuilder:validation:Minimum=2` 强制至少 2 个）、`Template`、`RecoveryPolicy`。

[api/orchestration/v1alpha1/podset_types.go:114-115](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/podset_types.go#L114-L115) 的注释直接点明：`PodSet is an internal API used by RoleSet controller when podGroupSize > 1`——用户一般不需要手写 PodSet。

[api/orchestration/v1alpha1/podset_types.go:71-95](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/podset_types.go#L71-L95) 定义 PodSet 的生命周期相位（Pending/Running/Ready/Failed）与两种恢复策略：`ReplaceUnhealthy`（只补缺失的 Pod）与 `Recreate`（任何一个 Pod 丢失就全组重建）。后文 4.2 会解释为什么有这两种策略。

**(4) 三类 CR 同属一个 API 分组**：

[api/orchestration/v1alpha1/groupversion_info.go:28-30](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/groupversion_info.go#L28-L30) 把三个 Kind（`StormServiceKind`、`RoleSetKind`、`PodSetKind`）注册到 `orchestration.aibrix.ai/v1alpha1` 分组。

#### 4.1.4 代码实践

**实践目标**：用 `kubectl` 直观看到三层 CR 的嵌套关系与标签串联。

**操作步骤**：

1. 阅读 PD 示例 [samples/ai-gateway-integration/disaggregation/vllm-sim-pd-stormservice.yaml:18-57](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/samples/ai-gateway-integration/disaggregation/vllm-sim-pd-stormservice.yaml#L18-L57)，注意它的 `spec.template.spec.roles` 下定义了 `prefill` 和 `decode` 两个角色，各 `replicas: 1`。
2. （若有集群）`kubectl apply -f samples/ai-gateway-integration/disaggregation/vllm-sim-pd-stormservice.yaml`。
3. 依次执行：
   ```bash
   kubectl get stormservice
   kubectl get roleset -l storm-service-name=vllm-sim-pd
   kubectl get podset -l storm-service-name=vllm-sim-pd      # 本例 podGroupSize<=1，可能为空
   kubectl get pod -l storm-service-name=vllm-sim-pd -o wide
   ```
4. 用 `-o yaml` 查看任一 RoleSet 的 `metadata.ownerReferences`，确认它指向 StormService。

**需要观察的现象**：RoleSet 的 `ownerReferences.controller` 指向 StormService；Pod 的标签里有 `storm-service-name`、`roleset-name`、`role-name`，正是这三层用标签串联的证据。

**预期结果**：你能看到一条清晰的归属链——删掉 StormService 后，RoleSet、PodSet、Pod 会被级联删除。**待本地验证**（无集群时可只做源码阅读：在 [pkg/controller/stormservice/rolesetoperations.go:60-72](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/stormservice/rolesetoperations.go#L60-L72) 中确认 `renderRoleSet` 给 RoleSet 设置了指向 StormService 的 `OwnerReference`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 PodSet 被设计成「internal API」，而 StormService/RoleSet 是用户可见的？

> **答案**：用户关心的是「我要一个 PD 服务，prefill 2 副本、decode 4 副本，prefill 多机」（用 StormService + RoleSet 表达就够），而不关心「多机时每个副本要拆成一个原子组」。PodSet 是为了实现「原子调度/同生共死」而引入的实现细节，让用户直接管理会增加心智负担，故由 RoleSet 控制器自动生成。

**练习 2**：`StormService.spec.template` 的类型是 `RoleSetTemplateSpec`，这说明了什么？

> **答案**：说明「StormService 的一个副本就是一个 RoleSet」，StormService 的模板直接就是 RoleSet 的定义，二者是 1:N 的「Deployment:ReplicaSet」式关系。

---

### 4.2 角色与副本编排：三层控制器的协同 reconcile

#### 4.2.1 概念说明

三个 CR 对应三个**独立但协同**的控制器，各管一层、互不越界：

- **StormService 控制器**：watch StormService，owns RoleSet。负责扩缩 RoleSet 数量、滚动升级、聚合状态。它**不直接碰 Pod**。
- **RoleSet 控制器**：watch RoleSet，owns PodSet 和 Pod。负责把每个角色的副本数收敛到位，并根据 `podGroupSize` 决定直接管 Pod 还是管 PodSet。
- **PodSet 控制器**：watch PodSet，owns Pod。负责把一个原子组里的 `podGroupSize` 个 Pod 全部拉起，并处理 Pod 故障。

这种分层让每层控制器的逻辑都保持简单：上层只关心「子对象数量与版本」，下层只关心「单个 Pod 的生死」。三层通过 `OwnerReference` 闭合成链，删除顶层即可逐层 GC。

> 这与 u5-l1 的 RayClusterFleet → RayClusterReplicaSet → RayCluster 是同一个套路：AIBrix 反复用「高层管版本/副本、低层管实例」的分层来组织编排控制器。

#### 4.2.2 核心流程

以一次「创建 StormService」为例，三层控制器的协同流程：

```text
用户 apply StormService
   │
   ▼
[StormService 控制器 Reconcile]
   1. 加 finalizer
   2. 计算 currentRevision / updateRevision (ControllerRevision)
   3. sync(): 建 headless Service；按 spec.replicas 扩缩 RoleSet；按需 rollout
   4. 聚合 RoleSet 状态写回 StormService.status
   │   创建 N 个 RoleSet (ownerRef → StormService)
   ▼
[RoleSet 控制器 Reconcile]  (每个 RoleSet 各自触发)
   1. syncPodGroup (可选: 伦理调度器 PodGroup)
   2. syncPods → 选 RollingManager(Sequential/Parallel/Interleave)
      └─ 对每个 role 调 RoleRollingSyncer.Scale() 把副本数收敛到位
         ├─ podGroupSize<=1: 直接 Create/Delete Pod
         └─ podGroupSize>1 : Create/Delete PodSet
   3. calculateStatus 聚合各 role 的 Ready/Updated 副本数
   ▼
[PodSet 控制器 Reconcile]  (仅 podGroupSize>1 时, 每个 PodSet 各自触发)
   1. reconcilePodGroup (可选)
   2. reconcilePods: 按 PodGroupSize 补齐/缩减 Pod, 处理故障 (ReplaceUnhealthy/Recreate)
   3. updateStatus: 计算 Phase (Pending→Running→Ready)
```

#### 4.2.3 源码精读

**(1) 控制器注册：三个控制器绑在同一个 Feature Gate 下**。

[pkg/controller/controller.go:93-96](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/controller.go#L93-L96) 显示：当 `StormServiceController` Feature Gate 开启时，`roleset.Add`、`stormservice.Add`、`podset.Add` 三个控制器**一起注册**。这是合理的——三层缺一不可，故用同一个开关控制（Feature Gate 机制详见 u2-l2）。

**(2) StormService 控制器：watch StormService，owns RoleSet**。

[pkg/controller/stormservice/stormservice_controller.go:60-73](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/stormservice/stormservice_controller.go#L60-L73) 用 Builder 链声明 `For(StormService).Owns(RoleSet)`——RoleSet 的任何变化都会反向触发 StormService 的 reconcile。

[pkg/controller/stormservice/stormservice_controller.go:99-148](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/stormservice/stormservice_controller.go#L99-L148) 是 reconcile 主循环：处理 finalizer → 取 ControllerRevision → `syncRevision` 算版本 → `sync` 做实际扩缩/滚动 → `truncateHistory` 清理旧版本历史。

**(3) StormService 的 sync：扩缩 + 滚动**。

[pkg/controller/stormservice/sync.go:40-71](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/stormservice/sync.go#L40-L71) 是编排核心：先 `syncHeadlessService`（为服务发现建 headless Service），再 `scaling`（把 RoleSet 数量收敛到 `spec.replicas`），收敛完才 `rollout`（滚动升级），最后 `updateStatus`。

[pkg/controller/stormservice/sync.go:73-113](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/stormservice/sync.go#L73-L113) 的 `syncHeadlessService` 创建一个 `ClusterIP: None` 的 headless Service，且 `PublishNotReadyAddresses: true`——这是为了让尚未 Ready 的 Pod 也能被 DNS 解析，便于多机引擎启动期互相发现。

[pkg/controller/stormservice/sync.go:138-259](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/stormservice/sync.go#L138-L259) 的 `scaling` 处理扩容（`diff<0`，受 `maxSurge` 上限约束）和缩容（`diff>0`，优先删未就绪的、且受 `minAvailable` 下限约束）。这里的 `maxSurge` / `maxUnavailable` 语义与 K8s Deployment 完全一致。

**(4) StormService 渲染并创建 RoleSet**。

[pkg/controller/stormservice/rolesetoperations.go:60-108](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/stormservice/rolesetoperations.go#L60-L108) 的 `renderRoleSet` 是 StormService → RoleSet 的关键：它把 StormService 模板渲染成一个 RoleSet，设置指向 StormService 的 `OwnerReference`（L65-67，级联删除的根基），并打上 `storm-service-name`、`storm-service-revision` 标签和每角色的版本注解。

[pkg/controller/stormservice/rolesetoperations.go:111-127](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/stormservice/rolesetoperations.go#L111-L127) 的 `createRoleSet` 用 `SlowStartBatch`（指数翻倍批量创建，避免一次性打爆 API Server）创建 RoleSet。

**(5) RoleSet 控制器：watch RoleSet，owns PodSet 和 Pod**。

[pkg/controller/roleset/roleset_controller.go:65-81](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/roleset/roleset_controller.go#L65-L81) 声明 `For(RoleSet).Owns(PodSet).Owns(Pod)`——RoleSet 同时拥有 PodSet 和 Pod，所以两条路径的子对象变化都能触发它。

[pkg/controller/roleset/roleset_controller.go:109-182](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/roleset/roleset_controller.go#L109-L182) 是 RoleSet 的 reconcile：`syncPodGroup` → `syncPods` → `emitTopologyPolicyPendingReplacementEvent` → `calculateStatus`。注意 reconcile 一开始用 `context.WithTimeout(ctx, 1*time.Minute)` 给单次协调设了上限。

**(6) RoleSet 选 RollingManager：角色间的编排顺序**。

[pkg/controller/roleset/sync.go:102-127](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/roleset/sync.go#L102-L127) 的 `syncPods` 按 `UpdateStrategy` 选三种编排器之一：`Sequential`（默认，一个角色升完再升下一个）、`Parallel`（所有角色同时升）、`Interleave`（按步交错，所有角色齐头并进）。

[pkg/controller/roleset/rolling.go:43-94](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/roleset/rolling.go#L43-L94) 的 `RollingManagerSequential.Next` 展示了「先 Scale 所有角色，再按 `UpgradeOrder` 排序，逐个 Rollout，任一角色未就绪就 break」的逻辑——这正是 PD 解耦升级时「先升 prefill、再升 decode」的能力来源。

**(7) RoleSet 选 RoleSyncer：podGroupSize 分叉**。

[pkg/controller/roleset/rolesyncer.go:829-852](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/roleset/rolesyncer.go#L829-L852) 的 `GetRoleSyncerWithRecorder` 是本模块的「分叉点」：`podGroupSize > 1` 返回 `PodSetRoleSyncer`，否则按 `Stateful` 返回 `StatefulRoleSyncer` 或 `StatelessRoleSyncer`。三种同步器都实现同一个 `RoleRollingSyncer` 接口（[rolesyncer.go:37-43](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/roleset/rolesyncer.go#L37-L43)），从而对 RollingManager 屏蔽了「管 Pod 还是管 PodSet」的差异。

**(8) RoleSet → PodSet 桥梁：PodSetRoleSyncer**。

[pkg/controller/roleset/podset_rollsyncer.go:376-464](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/roleset/podset_rollsyncer.go#L376-L464) 的 `createPodSetForRole` 是整条链最关键的一环：它把一个 `RoleSpec` 渲染成一个 `PodSet`，设置指向 RoleSet 的 `OwnerReference`（L393-395），把 `role.PodGroupSize` 写进 `PodSetSpec.PodGroupSize`（L398），打上 `roleset-name`、`role-name`、`role-template-hash` 标签，并在末尾（L458-461）按需注入 TopologyPolicy 共置亲和性。PodSet 的名字形如 `{roleset}-{role}-{hash}-{index}`。

[pkg/controller/roleset/podset_rollsyncer.go:357-374](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/roleset/podset_rollsyncer.go#L357-L374) 的 `podSetSlotForRole` 用 `role-replica-index` 标签把 PodSet 分到 `expectedReplicas` 个「槽位」——每个槽位对应角色的一个副本，保证副本数收敛正确。

[pkg/controller/roleset/sync.go:165-205](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/roleset/sync.go#L165-L205) 的 `calculateStatusForRole` 同样按 `podGroupSize` 分叉：`>1` 时从 PodSet 聚合状态（[sync.go:207-247](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/roleset/sync.go#L207-L247) 的 `calculateStatusFromPodSets`），否则直接数 Pod。

**(9) PodSet 控制器：把原子组拉起**。

[pkg/controller/podset/podset_controller.go:74-86](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podset/podset_controller.go#L74-L86) 声明 `For(PodSet).Owns(Pod)`。

[pkg/controller/podset/podset_controller.go:113-153](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podset/podset_controller.go#L113-L153) 的 reconcile：加 finalizer → `reconcilePodGroup`（可选伦理调度）→ `reconcilePods` → `updateStatus`。

[pkg/controller/podset/podset_controller.go:216-239](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podset/podset_controller.go#L216-L239) 的 `reconcilePods` 比较现有 Pod 数与 `PodGroupSize`：不足时按 `RecoveryPolicy` 分流，过多时缩容。

[pkg/controller/podset/podset_controller.go:241-333](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podset/podset_controller.go#L241-L333) 实现两种恢复策略：`handleReplaceUnhealthy` 先删有重启记录的不健康 Pod、再按索引补齐缺失槽位；`handleRecreateStrategy` 则「全删重建」。后者用牺牲可用性换取「整组一致性」——多机张量并行时，任意一个 Pod 缺失整组都无意义，故宁可全重建。

[pkg/controller/podset/podset_controller.go:353-420](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podset/podset_controller.go#L353-L420) 的 `createPodFromTemplate` 揭示了 PodSet 给 Pod 注入的协调信息：`PODSET_NAME`、`POD_GROUP_INDEX`、`POD_GROUP_SIZE` 三个内置环境变量（L393-397，且会丢弃用户同名变量以防冲突），以及 `pod-group-index` 标签、`Hostname`/`Subdomain`（用于 FQDN）。这些是引擎启动期发现自己「在第几号、组里共几个」的依据。

**(10) 贯穿三层的标签契约**。

[pkg/controller/constants/stormservice.go:24-31](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/constants/stormservice.go#L24-L31) 定义了串联三层的标签：`storm-service-name`、`storm-service-revision`、`roleset-name`、`role-name`、`role-template-hash`、`role-replica-index`、`podset-name`、`pod-group-index`。这些标签既是控制器查询子对象的依据，也是网关做服务发现（按 role 选 prefill/decode Pod）的依据。

#### 4.2.4 代码实践

**实践目标**：跟踪一条完整的调用链——从「修改 `spec.replicas`」到「PodSet 控制器增删 Pod」，理解三层如何接力。

**操作步骤**（源码阅读型实践）：

1. 假设把 `vllm-sim-pd` 的 `spec.replicas` 从 1 改成 2。
2. 在 [pkg/controller/stormservice/sync.go:138-259](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/stormservice/sync.go#L138-L259) 的 `scaling` 中确认：`diff = len(activeRoleSets) - expectReplica` 为负，走「scale out」分支，调用 `createRoleSet` 多创建 1 个 RoleSet。
3. 在 [pkg/controller/stormservice/rolesetoperations.go:111-127](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/stormservice/rolesetoperations.go#L111-L127) 确认新 RoleSet 的 `ownerRef` 指向 StormService。
4. 新 RoleSet 的创建触发 RoleSet 控制器 reconcile：在 [pkg/controller/roleset/sync.go:102-127](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/roleset/sync.go#L102-L127) 选定 RollingManager，对 prefill/decode 各调一次 `Scale`。
5. 若某角色 `podGroupSize>1`，进入 [podset_rollsyncer.go:376](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/roleset/podset_rollsyncer.go#L376) 的 `createPodSetForRole`，新建 PodSet。
6. PodSet 创建触发 PodSet 控制器：[podset_controller.go:216-239](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podset/podset_controller.go#L216-L239) 按 `PodGroupSize` 补齐 Pod。

**需要观察的现象**：每一层只「创建下一层对象 + 设 ownerRef」，从不直接跨层操作（StormService 不直接建 Pod）。

**预期结果**：你能画出一条「`spec.replicas++` → RoleSet++ → PodSet++ → Pod++」的因果链，且每一步都可定位到具体函数。

#### 4.2.5 小练习与答案

**练习 1**：`SlowStartBatch`（[rolesetoperations.go:123](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/stormservice/rolesetoperations.go#L123)）为什么用「指数翻倍」而不是一次性创建所有 RoleSet？

> **答案**：批量创建前先小批量试探（如先建 1 个），成功后再翻倍（1→2→4…），避免一次性提交大量对象打爆 API Server、或在配额不足时生成大量失败事件。这是 controller-runtime 生态里常见的「慢启动」模式。

**练习 2**：为什么 PodSet 控制器要提供 `ReplaceUnhealthy` 和 `Recreate` 两种恢复策略？

> **答案**：单 Pod 服务里丢一个补一个即可（`ReplaceUnhealthy`，可用性优先）；但多机张量并行的 PodSet 里，少一个 Pod 整组就无法正常推理，此时与其带病运行不如全组重建（`Recreate`，一致性优先）。两种策略对应「无状态副本」与「强耦合原子组」两类工作负载。

---

### 4.3 解耦推理拓扑：Prefill/Decode 分离与 TopologyPolicy 共置

#### 4.3.1 概念说明

前两个模块讲清了「三层 CR 怎么协同」，本模块回到**为什么**——这套拓扑 CR 到底是为了支撑什么推理形态？

**形态一：Prefill/Decode 解耦（PD Disaggregation）**。

传统推理引擎把 prefill 和 decode 混在一个引擎里跑，二者资源画像冲突（prefill 抢算力、decode 抢显存带宽），互相拖累。PD 解耦把它们拆成两组独立引擎：请求先到 prefill 引擎算完 KV Cache，再把 KV Cache 迁移到 decode 引擎逐 token 输出。这要求「一个推理服务同时包含 prefill 和 decode 两类 Pod」——而这正是 RoleSet 的 `roles` 列表的用武之地：用两个角色分别描述 prefill 和 decode，各自独立的副本数、镜像、参数。

**形态二：拓扑共置（TopologyPolicy）**。

PD 解耦还要求 prefill 和 decode 之间的 KV 迁移延迟尽量低，因此希望它们「在同一个节点/同一个可用区」。AIBrix 用 `TopologyPolicy` 把这种放置意图声明化：指定一个拓扑键（如 `kubernetes.io/hostname` 表示单节点、`topology.kubernetes.io/zone` 表示可用区）和一个范围（StormService/RoleSet/Role），控制器会把它翻译成 Kubernetes 的 Pod 亲和性，注入到生成的 Pod 上。

#### 4.3.2 核心流程

**PD 解耦拓扑的形成**：

```text
StormService (name: vllm-sim-pd)
  template.spec.roles:
    - name: prefill   # 角色1: 计算密集的预填充引擎
        replicas: Rp, template: { llm-engine image A, port 8000 }
    - name: decode    # 角色2: 显存带宽密集的解码引擎
        replicas: Rd, template: { routing-sidecar + llm-engine image A, port 8000/8200 }
        ↓
  网关 (gateway) 通过 role 标签区分 prefill/decode Pod, 做 PD 路由:
     请求 → prefill Pod (算 KV) --KV迁移--> decode Pod (出 token)
```

**TopologyPolicy 的三种 scope**（取自 [samples/orchestration/topology-policy/README.md](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/samples/orchestration/topology-policy/README.md)）：

| scope | 共置粒度 | 适用场景 |
| --- | --- | --- |
| `StormService` | 整个服务的所有 Pod 共享同一拓扑值 | 把整个服务钉在一个域 |
| `RoleSet` | 每个 RoleSet 内部共置（不同 RoleSet 可在不同域） | 「一个副本（含 prefill+decode）放一个节点」 |
| `Role` | 同一角色的所有 Pod（跨 RoleSet）共置 | 把 prefill 池和 decode 池分别按域隔离 |

`mode` 决定是软偏好（`Preferred`，权重 100 的亲和性偏好，放不下可回退）还是硬约束（`Required`，硬亲和性，放不下则 Pending）。

#### 4.3.3 源码精读

**(1) PD 双角色示例**。

[samples/ai-gateway-integration/disaggregation/vllm-sim-pd-stormservice.yaml:18-57](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/samples/ai-gateway-integration/disaggregation/vllm-sim-pd-stormservice.yaml#L18-L57) 定义 `prefill` 角色：单容器 `llm-engine`（inference-sim），暴露 8000 端口。同文件 L57 起定义 `decode` 角色：额外有一个 `routing-sidecar` 容器（用 `--connector=nixlv2` 做 KV 迁移），`llm-engine` 监听 8200。两个角色的 `model.aibrix.ai/name` 都相同，说明它们服务同一个模型，只是职责不同。注意 `updateStrategy.type: InPlaceUpdate`，说明这是池化模式下的 PD。

**(2) TopologyPolicy 数据模型**。

[api/orchestration/v1alpha1/roleset_types.go:151-179](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/roleset_types.go#L151-L179) 定义 `TopologyPolicy`：`Scope`（枚举 StormService/RoleSet/Role）、`Mode`（Preferred/Required）、`Key`（拓扑标签键，带严格正则校验）。注释点明「Updating this policy on a live RoleSet only affects newly created or replaced Pods」——因为 Pod 亲和性创建后不可变，所以只能影响新 Pod。

**(3) TopologyPolicy 的注入点**。

[pkg/controller/roleset/podset_rollsyncer.go:458-461](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/roleset/podset_rollsyncer.go#L458-L461) 显示：`createPodSetForRole` 在生成 PodSet 时，若 `roleSet.Spec.TopologyPolicy != nil`，调用 `injectTopologyAffinity` 把拓扑意图翻译成 Pod 亲和性写进 Pod 模板。这正是「声明式拓扑 → Kubernetes 亲和性」的落地点。对 `podGroupSize<=1` 的路径，类似的注入发生在 `renderStormServicePod` 中（被 Stateful/Stateless syncer 调用）。

**(4) PD + TopologyPolicy + podGroupSize 综合示例**。

[samples/orchestration/topology-policy/roleset-hostname-required.yaml:38-65](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/samples/orchestration/topology-policy/roleset-hostname-required.yaml#L38-L65) 是一个信息密度极高的例子：`replicas: 3`（3 个 RoleSet）、`topologyPolicy: {scope: RoleSet, mode: Required, key: kubernetes.io/hostname}`（每个 RoleSet 的所有 Pod 必须在同一节点）、`roles` 含 `prefill`（`replicas:2, podGroupSize:2`，即每个 RoleSet 里有 1 个含 2 Pod 的 PodSet）和 `decode`（`replicas:4`）。它同时演示了本讲的全部三个概念：PD 双角色、PodSet（podGroupSize=2）、TopologyPolicy 共置。注释 L29-37 解释了为什么选 `scope: RoleSet`——因为「一个 StormService 副本 = 一个 RoleSet」，用 RoleSet scope 恰好实现「把一个副本（含其全部 prefill+decode Pod）钉在一个节点」。

#### 4.3.4 代码实践

**实践目标**：用一个 PD 分离的例子，画出 StormService → RoleSet → PodSet 的归属关系，并说明每个控制器分别负责什么（即本讲的总实践任务）。

**操作步骤**：

1. 阅读综合示例 [samples/orchestration/topology-policy/roleset-hostname-required.yaml](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/samples/orchestration/topology-policy/roleset-hostname-required.yaml)。它的拓扑参数：`StormService replicas=3`，prefill 角色 `replicas=2, podGroupSize=2`，decode 角色 `replicas=4`。
2. 在纸上画出归属树（见下方「预期结果」）。
3. 对树中每条边，标注「由哪个控制器的哪个函数创建」（参考 4.2.3 的源码点）。
4. 推算：部署完成后，整个服务总共有多少个 RoleSet、多少个 PodSet、多少个 Pod？

**需要观察的现象**：prefill 角色因为 `podGroupSize=2` 走 PodSet 路径，decode 角色 `podGroupSize` 未设走直接管 Pod 路径——两种路径**共存于同一个 RoleSet**。

**预期结果**（归属树与计数）：

```text
StormService tp-rs-host-req (replicas=3)
├─ RoleSet #0  (ownerRef→StormService, 由 stormservice.scaling/createRoleSet 创建)
│   ├─ prefill 角色 (replicas=2, podGroupSize=2)
│   │   ├─ PodSet ps-prefill-0 (2 Pods)   ← roleset.PodSetRoleSyncer.createPodSetForRole
│   │   └─ PodSet ps-prefill-1 (2 Pods)
│   └─ decode 角色 (replicas=4, 无 podGroupSize)
│       └─ 4 个 decode Pod (直接由 roleset.StatefulRoleSyncer 创建)
├─ RoleSet #1  (同构)
└─ RoleSet #2  (同构)
```

计数：3 个 RoleSet；每个 RoleSet 里 prefill 有 2 个 PodSet，共 3×2 = 6 个 PodSet；Pod 总数 = 3×(prefill 2×2 + decode 4) = 3×8 = 24 个 Pod。三个控制器分工：StormService 控制器建 RoleSet；RoleSet 控制器建 PodSet（prefill）和 Pod（decode）；PodSet 控制器建 prefill 的 Pod。

#### 4.3.5 小练习与答案

**练习 1**：在 PD 解耦场景里，为什么 `decode` 角色的 Pod 模板里多了一个 `routing-sidecar` 容器？

> **答案**：decode 引擎需要接收从 prefill 引擎迁移过来的 KV Cache。`routing-sidecar`（示例中用 `--connector=nixlv2`）负责处理这段跨引擎的 KV 传输与请求转发，是 PD 解耦数据面的关键组件。prefill 角色不需要它，故两角色模板不同。

**练习 2**：如果想让「prefill 池和 decode 池分别落在不同可用区」，该用哪个 `topologyPolicy.scope`？

> **答案**：`scope: Role`，`key: topology.kubernetes.io/zone`。因为 `Role` scope 让「同一角色的所有 Pod（跨所有 RoleSet）共置在同一域」，从而把所有 prefill Pod 钉在一个区、所有 decode Pod 钉在另一个区，实现按角色隔离池。

**练习 3**：`TopologyPolicy` 修改后，已经在运行的旧 Pod 会立刻被迁移吗？

> **答案**：不会。源码注释（[roleset_types.go:148-150](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/roleset_types.go#L148-L150)）指出，Pod 亲和性创建后不可变，所以新策略只影响「新建或替换的 Pod」。要让旧 Pod 生效，需要通过滚动升级逐个替换。

---

## 5. 综合实践

把本讲三个模块串成一个完整任务：**为一个小型 PD 解耦服务设计 StormService 清单，并预测控制器会生成什么**。

**任务**：设计一个 StormService，满足：服务名 `my-pd`；2 个副本；包含 `prefill`（1 副本，单 Pod）和 `decode`（1 副本，单 Pod）两角色；希望每个副本的 prefill 和 decode 落在同一个节点上（软偏好）。

**步骤**：

1. 参照 [vllm-sim-pd-stormservice.yaml](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/samples/ai-gateway-integration/disaggregation/vllm-sim-pd-stormservice.yaml) 的骨架，把 `spec.replicas` 改成 2。
2. 在 `spec.template.spec` 下加 `topologyPolicy: {scope: RoleSet, mode: Preferred, key: kubernetes.io/hostname}`（理由参考 4.3 的综合示例注释）。
3. 在不实际部署的前提下，回答：
   - 会生成几个 RoleSet？→ 2 个。
   - 每个 RoleSet 里会有 PodSet 吗？→ 不会，因为两个角色都没设 `podGroupSize>1`，走直接管 Pod 路径。
   - StormService 控制器的 [sync.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/stormservice/sync.go) 里，`scaling` 函数的 `diff` 初值是多少？→ `len(activeRoleSets) - 2`，初始为 `0 - 2 = -2`，走 scale out。
   - RoleSet 控制器默认会用哪种 RollingManager？→ `Sequential`（[sync.go:104-125](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/roleset/sync.go#L104-L125) 的默认分支）。
4. 若有集群，`kubectl apply` 后用 `kubectl get stormservice,roleset,pod -l storm-service-name=my-pd -o wide` 验证你的预测，并检查 decode Pod 的 spec 是否被注入了指向同节点 prefill Pod 的亲和性偏好。

**验收标准**：你能准确说出每一层会生成几对象、由哪个控制器生成、`topologyPolicy` 如何影响 Pod 放置，且能与源码函数一一对应。

## 6. 本讲小结

- **三层 CR**：StormService（服务/版本/副本数）→ RoleSet（角色列表）→ PodSet（多 Pod 原子组），同属 `orchestration.aibrix.ai/v1alpha1`，PodSet 是内部 API。
- **三个控制器各管一层、互不越界**，靠 `OwnerReference` 闭合成链，靠 `storm-service-name`/`roleset-name`/`role-name` 等标签串联，删除顶层即级联回收。
- **`podGroupSize` 是分叉开关**：`>1` 时 RoleSet 经 `PodSetRoleSyncer` 创建 PodSet、再由 PodSet 控制器拉起一组原子 Pod；`<=1` 时 RoleSet 直接管 Pod。两种路径可共存于同一 RoleSet。
- **角色间编排** 由 `Sequential`/`Parallel`/`Interleave` 三种 RollingManager 控制，`Sequential` 支持按 `UpgradeOrder` 排序逐角色升级——这是 PD 解耦「先 prefill 后 decode」升级的能力来源。
- **PD 解耦** 用 RoleSet 的两个角色（prefill/decode）描述，是这套拓扑 CR 的核心应用场景；网关按 `role` 标签区分两类 Pod 做 PD 路由。
- **TopologyPolicy** 把放置意图（scope + mode + key）翻译成 Pod 亲和性注入，只影响新建/替换的 Pod；`scope: RoleSet` 恰好实现「一个服务副本钉一个节点」。

## 7. 下一步学习建议

- **网关侧的 PD 路由**：本讲只讲了控制平面如何「摆出」PD 拓扑，数据平面如何把请求从 prefill 路由到 decode、并迁移 KV Cache，请继续阅读 u8-l2（Prefill/Decode 解耦路由）。
- **服务发现与中央缓存**：网关如何通过 `role` 标签发现 prefill/decode Pod，依赖 [pkg/cache](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache) 的 Pod 发现机制，见 u6-l2。
- **更深的更新语义**：本讲提到 `InPlaceUpdate` 与 `InPlaceIfPossible`，其就地镜像更新的细节在 [pkg/controller/roleset/inplace_update.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/roleset/inplace_update.go)，可作为进阶阅读。
- **对比 RayCluster 三层**：建议回头对比 u5-l1 的 Fleet→ReplicaSet→RayCluster，体会 AIBrix 如何用同一套「高层管版本、低层管实例」的范式组织不同编排场景。
