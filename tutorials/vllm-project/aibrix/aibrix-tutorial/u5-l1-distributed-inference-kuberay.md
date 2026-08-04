# 分布式推理与 KubeRay 集成

## 1. 本讲目标

本讲聚焦 AIBrix 控制平面的「分布式推理编排」能力。大模型推理常常一张 GPU 装不下，需要把模型切分到多张卡、甚至多台机器上协同计算（例如 `--tensor-parallel-size 2` 把模型张量并行切到两张卡）。AIBrix 自己并不实现这套跨节点协同——它把这件事交给社区项目 **KubeRay**，而在其之上提供两个更高层的自定义资源（CR）来管理「一组 RayCluster」的生命周期与滚动升级。

读完本讲，你应当能够：

1. 说清 **KubeRay 是什么**，以及 AIBrix 为什么把它当作可选依赖、如何在启动时检测它。
2. 理解 `RayClusterReplicaSet` 与 `RayClusterFleet` 这两个 CR 的数据模型，以及它们与原生 `ReplicaSet` / `Deployment` 的对应关系。
3. 画出 **Fleet → ReplicaSet → RayCluster** 的三层控制层级，并解释每层控制器分别负责什么。
4. 看懂 ReplicaSet 控制器的 reconcile 主循环、`Expectations` 机制与缩容排序规则。

## 2. 前置知识

本讲建立在已完成的入门单元之上，复习三个关键概念：

- **自定义资源 (CRD / CR)**：AIBrix 用 Go 结构体在 `api/orchestration/v1alpha1/` 下定义自己的资源类型，经 `make manifests` 生成 CRD 安装到集群（详见 u2-l3）。
- **控制器注册与 Feature Gate**：`pkg/controller/controller.go` 借鉴 Kruise，用一个 `controllerAddFuncs` 切片统一注册所有控制器，并用 `--controllers` 开关决定加载哪些（详见 u2-l1、u2-l2）。
- **Kubernetes 的 OwnerReference 与级联删除**：子资源通过 `ownerReferences` 指向父资源，删除父资源时子资源会被垃圾回收器（GC）自动清理。本讲三层拓扑完全依赖这条机制。

此外需要两个本讲才引入的术语：

- **KubeRay**：Ray（分布式计算框架）的 Kubernetes Operator，其核心 CR 是 `rayclusters.ray.io`（`apiVersion: ray.io/v1`）。它负责真正把一个 RayCluster（含 head 节点 + 若干 worker 节点）拉起来并维护。AIBrix 的分布式推理 CR 在其之上做编排，**不直接创建 Pod**。
- **张量并行 (Tensor Parallelism)**：把单个模型的权重按维度切分到多张 GPU 上，推理时多卡协同。这正是 `samples/distributed/` 里 `--tensor-parallel-size 2`、`--distributed-executor-backend ray` 的含义。

> 一句话定位：AIBrix 的分布式推理 = **KubeRay（底层执行）+ RayClusterReplicaSet（副本管理）+ RayClusterFleet（滚动升级/版本管理）**。三者各司其职，缺一不可，但 KubeRay 是可选的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pkg/controller/controller.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/controller.go) | 控制器注册总入口，包含 KubeRay CRD 存在性检测的 `Initialize` 与 `checkCRDExists` |
| [api/orchestration/v1alpha1/raycluster_type.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/raycluster_type.go) | 定义 `RayClusterTemplateSpec`——被 ReplicaSet 与 Fleet 共用的「RayCluster 模板」 |
| [api/orchestration/v1alpha1/rayclusterreplicaset_types.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/rayclusterreplicaset_types.go) | `RayClusterReplicaSet` 的 Spec/Status 数据模型 |
| [api/orchestration/v1alpha1/rayclusterfleet_types.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/rayclusterfleet_types.go) | `RayClusterFleet` 的 Spec/Status 数据模型（含 scale 子资源标记） |
| [pkg/controller/rayclusterreplicaset/rayclusterreplicaset_controller.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterreplicaset/rayclusterreplicaset_controller.go) | ReplicaSet 控制器：reconcile 主循环、扩缩容、`Expectations` |
| [pkg/controller/rayclusterreplicaset/rayclusterreplicaset_utils.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterreplicaset/rayclusterreplicaset_utils.go) | ReplicaSet 工具：构造 RayCluster、过滤活跃集群、缩容排序、状态计算 |
| [pkg/controller/rayclusterfleet/rayclusterfleet_controller.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterfleet/rayclusterfleet_controller.go) | Fleet 控制器：reconcile 主循环、查询下属 ReplicaSet、创建新 ReplicaSet |
| [pkg/controller/rayclusterfleet/sync.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterfleet/sync.go) | Fleet 的滚动/同步核心：版本哈希、创建新 RS、扩缩容 |
| [samples/distributed/fleet.yaml](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/samples/distributed/fleet.yaml) | 可运行示例：单节点 RayClusterFleet |

## 4. 核心概念与源码讲解

### 4.1 KubeRay CRD 依赖检测

#### 4.1.1 概念说明

AIBrix 的核心定位是「推理基础设施」，分布式推理只是它支持的众多能力之一。并非所有用户都需要多节点张量并行——很多场景单卡就够了。因此 KubeRay 被设计成**可选依赖**：不装 KubeRay，AIBrix 控制平面照样能跑（PodAutoscaler、ModelAdapter 等正常工作）；只有当你确实要用 `RayClusterFleet` / `RayClusterReplicaSet` 时才需要安装它。

这带来一个工程问题：分布式推理控制器的代码里大量引用 `rayclusterv1.RayCluster` 这个类型。如果集群里没装 KubeRay 的 CRD，控制器在注册阶段（`SetupWithManager`）会因为「找不到 ray.io 这个 API 类型」而报错。AIBrix 的解法是——**在注册控制器之前先探测 CRD 是否存在，不存在就跳过注册**，从而优雅降级。

> 对比 u2-l2 学过的 Feature Gate：`--controllers` 表达的是「用户的意图」（我想启用分布式推理）；而 CRD 检测表达的是「集群的客观能力」（集群里到底有没有 KubeRay）。两者必须同时满足，控制器才会真正启动。

#### 4.1.2 核心流程

注册阶段的决策流程：

```
Initialize(mgr)
  ├── 用户开启了 distributed-inference-controller ?  (Feature Gate)
  │     ├── 否 → 跳过
  │     └── 是 → checkCRDExists("rayclusters.ray.io")
  │           ├── 查询出错（RBAC/APIServer 故障）→ fail fast，整个进程退出
  │           ├── CRD 不存在 → 打日志，跳过注册（优雅降级）
  │           └── CRD 存在 → 注册 rayclusterreplicaset.Add + rayclusterfleet.Add
  └── 返回
```

关键设计取舍是**「未找到 = 降级，其他错误 = 致命」**。因为 NotFound 是预期的合法状态（用户就是没装 KubeRay），而 RBAC 权限不足或 API Server 不可达是真正的故障，应该让进程立刻失败暴露问题，而不是静默吞掉。

#### 4.1.3 源码精读

控制器注册总入口里，分布式推理这一段是唯一带 CRD 探测的分支。先看意图判断与 CRD 探测：

[控制器注册：分布式推理分支带 CRD 检测（controller.go:68-87）](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/controller.go#L68-L87)

```go
if features.IsControllerEnabled(features.DistributedInferenceController) {
    // Check if the KubeRay CRD exists. Only skip if CRD is not found.
    // For other errors (RBAC, API server issues), fail fast.
    crdName := "rayclusters.ray.io"
    exists, err := checkCRDExists(mgr.GetAPIReader(), crdName)
    if err != nil {
        return fmt.Errorf("failed to check for KubeRay CRD %s: %w", crdName, err)
    }
    if !exists {
        klog.InfoS("KubeRay CRD not found, skipping distributed inference controller. ...")
        // Don't add the controller functions, effectively disabling this controller
    } else {
        controllerAddFuncs = append(controllerAddFuncs, rayclusterreplicaset.Add)
        controllerAddFuncs = append(controllerAddFuncs, rayclusterfleet.Add)
    }
}
```

注意两点：① 它用 `mgr.GetAPIReader()` 而不是缓存的 Client——因为注册发生在控制器启动前，此时 informer 缓存尚未就绪，必须直连 API Server 实时查询；② 只有「CRD 存在」时才把两个 `Add` 函数追加进切片，未存在时什么都不追加，于是下游 `SetupWithManager` 遍历切片时根本看不到这两个控制器。

具体的探测实现非常精简——它不读取 CRD 的完整定义，只取 `PartialObjectMetadata`（仅元数据），降低对 RBAC 权限和传输体积的要求：

[checkCRDExists 用 PartialObjectMetadata 探测（controller.go:118-134）](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/controller.go#L118-L134)

```go
func checkCRDExists(c client.Reader, crdName string) (bool, error) {
    crd := &metav1.PartialObjectMetadata{
        TypeMeta: metav1.TypeMeta{
            APIVersion: "apiextensions.k8s.io/v1",
            Kind:       "CustomResourceDefinition",
        },
    }
    err := c.Get(context.TODO(), client.ObjectKey{Name: crdName}, crd)
    if err != nil {
        if apierrors.IsNotFound(err) {
            return false, nil
        }
        return false, fmt.Errorf("error checking CRD %q: %w", crdName, err)
    }
    return true, nil
}
```

此外还有第二道防线。即便 CRD 探测通过、控制器注册进去了，万一探测与真正注册之间 CRD 被删了（极小概率），下游装配阶段还会兜底：

[SetupWithManager 对 NoKindMatchError 容错（controller.go:103-114）](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/controller.go#L103-L114) 会捕获 `meta.NoKindMatchError`，只打一条「CRD 未安装，控制器将空转」的日志并 `continue`，不让整个 Manager 启动失败。

#### 4.1.4 代码实践

**实践目标**：验证「不装 KubeRay 时，分布式推理控制器被优雅跳过，而其他控制器不受影响」。

**操作步骤**：

1. 阅读 [pkg/features/features.go:24-33](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/features/features.go#L24-L33)，确认 `DistributedInferenceController` 的常量字符串是 `"distributed-inference-controller"`。
2. 阅读 [controller.go:68-87](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/controller.go#L68-L87)，追踪代码路径。
3. 做一次思想实验（无需真实集群）：假设你在一个**没有**安装 KubeRay 的集群里启动 operator，问自己——
   - `checkCRDExists` 返回什么？→ `(false, nil)`
   - 进入哪个分支？→ `!exists` 分支，只打日志
   - `controllerAddFuncs` 里有没有分布式推理？→ 没有
4. 进阶（若有测试集群）：先**不**装 `config/dependency/kuberay-operator`，启动 operator，观察日志是否出现 `KubeRay CRD not found, skipping distributed inference controller`；再用 `kubectl get rayclusterfleet` 应返回找不到该类型的错误。然后单独安装 KubeRay operator（不需要重启 AIBrix operator 的话需要确认其重启后探测到 CRD）。

**需要观察的现象**：日志中应出现降级提示；`kubectl get rayclusterfleets` 在未装 KubeRay 时报「unknown resource type」。

**预期结果**：operator 进程正常启动且其余控制器（PodAutoscaler 等）功能正常。分布式推理相关命令在装好 KubeRay 后才可用。

> 待本地验证：步骤 4 涉及真实集群行为，若无环境可停留在源码阅读型实践（步骤 1-3）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `checkCRDExists` 用 `mgr.GetAPIReader()` 而不是普通的 `mgr.GetClient()`？
**答案**：注册阶段（`Initialize`）发生在 Manager 启动、informer 缓存填充之前。`GetClient()` 依赖本地缓存，此时缓存为空或未就绪，查询会失败或拿到空结果；`GetAPIReader()` 直接请求 API Server，能拿到实时、权威的结果。

**练习 2**：如果 `checkCRDExists` 返回的 `err` 是「RBAC 权限不足」，代码会怎样？为什么这是对的？
**答案**：`err != nil` 分支会 `return fmt.Errorf(...)`，导致 `Initialize` 返回错误，**整个 operator 进程启动失败**。这是对的——权限不足说明部署配置有问题（RBAC 漏配），应当 fail fast 暴露问题，而不是静默把分布式推理关掉，那样用户会误以为「装好了能用」。

---

### 4.2 RayClusterReplicaSet：管理 RayCluster 的 ReplicaSet

#### 4.2.1 概念说明

理解这一层最快的办法是类比 Kubernetes 原生对象：

| 原生 K8s | AIBrix 分布式推理 | 管理的对象 |
| --- | --- | --- |
| `ReplicaSet` | `RayClusterReplicaSet` | `RayCluster`（而非 Pod） |
| `Pod` | `RayCluster` | 由 KubeRay 管理的真实 Pod |

也就是说，`RayClusterReplicaSet` 做的事和原生 `ReplicaSet` 一模一样——**维持「期望副本数」个被管对象存在**——只不过它管的对象从 Pod 换成了 RayCluster。源码注释也明确指出这一点（见下方引用）。

`RayClusterReplicaSet` 的核心字段是 `Replicas`（想要几个 RayCluster）和 `Template`（每个 RayCluster 长什么样）。控制器不断比对「当前活跃的 RayCluster 数」与「期望副本数」，少了就创建、多了就删除。

#### 4.2.2 核心流程

ReplicaSet 控制器的 reconcile 主循环：

```
Reconcile(req)
  ├── Get RayClusterReplicaSet；找不到则清理 Expectations 并返回（对象被删）
  ├── Expectations.Satisfied?  → rsNeedsSync（是否还在等之前的创建/删除生效）
  ├── List 所有匹配 selector 的 RayCluster
  ├── filterActiveClusters  → 剔除正在删除/未就绪的集群
  ├── 若 rsNeedsSync 且对象未在删除中：
  │     currentReplicas = len(active clusters)
  │     desiredReplicas = spec.replicas
  │     ├── current < desired → ExpectCreations(diff) → scaleUp：循环创建 RayCluster
  │     └── current > desired → ExpectDeletions(diff) → scaleDown：并发删除（带信号量限流 10）
  └── calculateStatus → updateReplicaSetStatus（仅在 Expectations 满足且状态有变化时写回）
```

这里出现一个关键机制 **Expectations（期望）**，它是 kube-controller-manager 的经典设计，AIBrix 直接复用了 `pkg/controller/util/expectation` 包。它的作用是**防止抖动（hot loop）**：当控制器决定创建 3 个 RayCluster 后，会记下「我期望看到 3 次 create 事件」。在 watch 到这 3 个事件之前，`SatisfiedExpectations` 返回 `false`，控制器就不会再次尝试创建——避免「List 时缓存还没更新 → 以为少了 → 又创建一遍」的无限循环。

#### 4.2.3 源码精读

先看数据模型。`RayClusterReplicaSetSpec` 几乎是原生 ReplicaSetSpec 的翻版，唯一区别是 `Template` 字段从 `PodTemplateSpec` 换成了 `RayClusterTemplateSpec`：

[RayClusterReplicaSetSpec（rayclusterreplicaset_types.go:28-55）](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/rayclusterreplicaset_types.go#L28-L55) 关键字段：`Replicas *int32`（指针，区分「未指定」与「显式 0」，默认 1）、`Selector *LabelSelector`、`Template RayClusterTemplateSpec`。

而 `RayClusterTemplateSpec` 只是把 KubeRay 的 `rayclusterv1.RayClusterSpec` 包了一层元数据，被 ReplicaSet 和 Fleet 共用：

[RayClusterTemplateSpec 包装 KubeRay 的 RayClusterSpec（raycluster_type.go:25-34）](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/raycluster_type.go#L25-L34)

```go
type RayClusterTemplateSpec struct {
    metav1.ObjectMeta `json:"metadata,omitempty"`
    Spec rayclusterv1.RayClusterSpec `json:"spec,omitempty"`
}
```

> 这就是 AIBrix 与 KubeRay 的耦合点：整个编排层只认 `rayclusterv1.RayClusterSpec`，真正的 head/worker 节点定义、Ray 版本等细节都来自 KubeRay 的类型。AIBrix 不重新发明这套字段。

再看控制器的装配——它 watch 自己（`For RayClusterReplicaSet`）并「拥有」其创建的 RayCluster（`Owns RayCluster`），后者保证 RayCluster 的事件变化也会触发 reconcile，且删除 ReplicaSet 会级联删除其 RayCluster：

[ReplicaSet 控制器装配（rayclusterreplicaset_controller.go:73-76）](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterreplicaset/rayclusterreplicaset_controller.go#L73-L76)

```go
err := ctrl.NewControllerManagedBy(mgr).
    For(&orchestrationv1alpha1.RayClusterReplicaSet{}).
    Owns(&rayclusterv1.RayCluster{}).
    Complete(r)
```

reconcile 主循环的核心是「比副本数 → 扩或缩」：

[Reconcile 主循环的扩缩决策（rayclusterreplicaset_controller.go:138-152）](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterreplicaset/rayclusterreplicaset_controller.go#L138-L152)

```go
if rsNeedsSync && replicaset.DeletionTimestamp == nil {
    currentReplicas := int32(len(filteredClusters))
    desiredReplicas := *replicaset.Spec.Replicas
    if currentReplicas < desiredReplicas {
        diff := desiredReplicas - currentReplicas
        _ = r.Expectations.ExpectCreations(rsKey, int(diff))
        scaleError = r.scaleUp(ctx, replicaset, int(diff))
    } else if currentReplicas > desiredReplicas {
        diff := currentReplicas - desiredReplicas
        _ = r.Expectations.ExpectDeletions(rsKey, int(diff))
        scaleError = r.scaleDown(ctx, replicaset, filteredClusters, int(diff))
    }
}
```

扩容就是循环 `constructRayCluster` + `Create`，每创建一个就 `CreationObserved` 抵消一个 expectation：

[scaleUp 循环创建（rayclusterreplicaset_controller.go:164-173）](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterreplicaset/rayclusterreplicaset_controller.go#L164-L173)

其中 `constructRayCluster` 把 ReplicaSet 的 Template 物化成一个 RayCluster，并用 `GenerateName`（名字前缀+自动后缀）和 OwnerReference 挂上父子关系：

[constructRayCluster 物化 RayCluster 并建立 OwnerReference（rayclusterreplicaset_utils.go:93-108）](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterreplicaset/rayclusterreplicaset_utils.go#L93-L108)

缩容则更讲究——它先按规则排序，再并发删除（最多 10 并发，用带缓冲 channel 当信号量限流）。排序规则决定「先删谁」：

[缩容排序规则（rayclusterreplicaset_utils.go:125-133）](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterreplicaset/rayclusterreplicaset_utils.go#L125-L133) 的优先级是：

1. **未就绪的优先删**（`orderRayClusterNotReadyBeforeReady`）——优先淘汰不健康的集群。
2. **删除成本低的优先删**（`orderRayClusterLowerDeletionCostBeforeHigher`）——读取注解 `controller.kubernetes.io/pod-deletion-cost`，值小的先删。
3. **创建时间早的优先删**（旧实例先淘汰）。
4. **按 namespace/name 字典序兜底**，保证排序稳定、确定。

最后，`filterActiveClusters` / `isClusterActive` 决定哪些集群算「在册」。注意一个微妙点：**未 provisioned 完成的集群也算 active**，因为 ReplicaSet 要像管 Pod 一样把「还在 init 阶段」的集群计入计数，避免重复创建：

[isClusterActive 的三段判断（rayclusterreplicaset_utils.go:210-227）](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterreplicaset/rayclusterreplicaset_utils.go#L210-L227)

#### 4.2.4 代码实践

**实践目标**：通过阅读测试，理解 ReplicaSet 创建一个 RayCluster 的最小输入与 reconcile 触发方式。

**操作步骤**：

1. 打开 [rayclusterreplicaset_controller_test.go:53-92](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterreplicaset/rayclusterreplicaset_controller_test.go#L53-L92)，阅读 `BeforeEach` 里构造的测试 CR。
2. 注意它的 `Spec.Replicas` 是 `ptr.To(int32(1))`，`Selector.MatchLabels` 是 `foot: bar`，而 `Template.Labels` 是 `foo: bar`（测试里的 selector/template 标签故意不一致）。
3. 阅读 [第 105-119 行的 It 块](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterreplicaset/rayclusterreplicaset_controller_test.go#L105-L119)，看它如何手动构造 reconciler 并调用 `Reconcile`，断言不报错。
4. 追踪问题：当 `Replicas=1` 且 List 出的活跃 RayCluster 为 0 时，reconcile 会走 `scaleUp(ctx, replicaset, 1)`，创建 1 个 `GenerateName` 为 `test-resource-` 的 RayCluster。

**需要观察的现象 / 预期结果**：测试用 `envtest`（真实 API Server + etcd，详见 [suite_test.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterreplicaset/suite_test.go)）启动后，reconcile 应成功，且集群里应出现一个被该 ReplicaSet 拥有的 RayCluster。运行命令：

```bash
make envtest                        # 安装 envtest 二进制（详见 u1-l3）
go test ./pkg/controller/rayclusterreplicaset/... -ginkgo.focus="successfully reconcile"
```

> 待本地验证：测试需要 envtest 环境与 KubeRay CRD 已注册到测试 API Server（见 suite_test.go 的 `AddToScheme`），若环境不全可只做源码阅读。

#### 4.2.5 小练习与答案

**练习 1**：`Spec.Replicas` 为什么是 `*int32` 而不是 `int32`？
**答案**：指针类型才能区分「用户没填（nil，默认补 1）」和「用户显式填了 0（缩到 0 个集群）」。值类型无法表达 nil，会把「未指定」和「0」混为一谈。

**练习 2**：缩容时为什么用信号量把并发限制为 10？
**答案**：避免一次性向 API Server 发起大量 delete 请求，冲击 API Server 和 etcd；同时用 goroutine + `sync.WaitGroup` 并发执行又比串行快。这与原生 ReplicaSet/Deployment 控制器的做法一致。

---

### 4.3 RayClusterFleet：Deployment 风格的编排层

#### 4.3.1 概念说明

如果说 `RayClusterReplicaSet` 对应原生 `ReplicaSet`，那么 `RayClusterFleet` 就对应原生 **`Deployment`**。原生世界里，用户很少直接操作 ReplicaSet，而是用 Deployment——因为 Deployment 在 ReplicaSet 之上增加了**版本管理与滚动升级**能力。

`RayClusterFleet` 做的正是这件事：它管的是「一组 ReplicaSet」，通过给模板算哈希、按版本（revision）保留历史 ReplicaSet，实现：

- **滚动升级 (RollingUpdate)**：改了 `spec.template`（比如换了模型镜像），Fleet 创建一个新 ReplicaSet，逐步把副本从旧 RS 迁到新 RS。
- **重建升级 (Recreate)**：先删旧再建新。
- **回滚 (rollback)**：历史 ReplicaSet 被保留（默认 10 个），可以回退到旧版本。
- **暂停/恢复 (paused)**、**进度截止 (progressDeadlineSeconds)** 等 Deployment 语义。

AIBrix 直接把 Kubernetes Deployment 控制器的算法搬了过来（`sync.go` / `rolling.go` / `rollback.go` / `recreate.go` / `progress.go` 一一对应 Deployment 控制器的同名文件），只是把操作对象从 Pod 换成 RayCluster。这也是为什么 `RayClusterFleetSpec` 的字段（`Strategy`、`RevisionHistoryLimit`、`Paused`、`ProgressDeadlineSeconds`）看起来和 `appsv1.DeploymentSpec` 几乎一样。

#### 4.3.2 核心流程

Fleet 的 reconcile 比 ReplicaSet 复杂，因为它要先处理「版本」再处理「副本」：

```
Reconcile(req)
  ├── Get RayClusterFleet
  ├── 空 selector 拦截（选中全部集群是危险的，直接拒绝并告警）
  ├── getReplicaSetsForFleet      → 取出本 Fleet 拥有的所有 ReplicaSet
  ├── getRayClusterMapForFleet    → 取出所有 RayCluster，按 owner RS 的 UID 分桶
  ├── 若 Fleet 正在删除 → syncStatusOnly 后返回
  ├── checkPausedConditions / 若 Spec.Paused → sync 后返回
  ├── 若有 rollback 意图 → rollback
  ├── isScalingEvent? → sync（纯扩缩容，不涉及模板变更）
  └── 按 Spec.Strategy.Type 分发：
        ├── Recreate  → rolloutRecreate
        └── RollingUpdate → rolloutRolling
```

其中 `sync` / `rolloutRolling` / `rolloutRecreate` 内部都会调用 `getAllReplicaSetsAndSyncRevision`，它的职责是**找到「新 ReplicaSet」**：用模板哈希（`pod-template-hash`）匹配——和当前 Fleet 模板哈希一致的 RS 就是「新 RS」；找不到就按模板创建一个新的。副本数随后在新旧 RS 之间分配。

版本演进靠 revision 注解和哈希实现：模板变了 → 哈希变 → 旧 RS 不再匹配 → 创建新 RS → 滚动迁移。模板没变 → 哈希不变 → 复用同一个 RS，只是调整它的副本数。

#### 4.3.3 源码精读

Fleet 的数据模型带有 Deployment 全套字段，且通过 kubebuilder 标记暴露了 **scale 子资源**——这让外部伸缩器（如 PodAutoscaler）能像伸缩 Deployment 一样伸缩 Fleet，只改副本数而不触碰模板：

[RayClusterFleetSpec 的 Deployment 风格字段（rayclusterfleet_types.go:28-74）](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/rayclusterfleet_types.go#L28-L74)

[scale 子资源标记（rayclusterfleet_types.go:154-158）](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/rayclusterfleet_types.go#L154-L158)：

```go
// +kubebuilder:subresource:scale:specpath=.spec.replicas,statuspath=.status.replicas,selectorpath=.status.scalingTargetSelector
```

控制器的装配比 ReplicaSet 多 watch 一层——它既拥有 ReplicaSet，又拥有 RayCluster（跨层 watch，方便感知底层状态变化）：

[Fleet 控制器装配，跨层 Owns（rayclusterfleet_controller.go:76-80）](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterfleet/rayclusterfleet_controller.go#L76-L80)

```go
err := ctrl.NewControllerManagedBy(mgr).
    For(&orchestrationv1alpha1.RayClusterFleet{}).
    Owns(&orchestrationv1alpha1.RayClusterReplicaSet{}).
    Owns(&rayclusterv1.RayCluster{}).
    Complete(r)
```

reconcile 顶部的「空 selector 拦截」是个安全门——空 selector 会选中集群里**所有** RayCluster，极易引发误操作，所以控制器明确拒绝并告警：

[空 selector 安全拦截（rayclusterfleet_controller.go:126-137）](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterfleet/rayclusterfleet_controller.go#L126-L137)

Fleet 找到自己的 ReplicaSet 靠的是 OwnerReference（`metav1.IsControlledBy`），而不是单靠 label——label 用于选中候选，OwnerReference 用于确认归属，二者配合避免「误领养」别人的 RS：

[getReplicaSetsForFleet 用 OwnerReference 确认归属（rayclusterfleet_controller.go:188-211）](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterfleet/rayclusterfleet_controller.go#L188-L211)

创建新 ReplicaSet 的逻辑（在 `sync.go`）是 Fleet 滚动升级的核心：它给模板打上 `pod-template-hash` 标签、把哈希加进 selector（让每个版本的 RS 只匹配自己版本的 RayCluster），再用确定性的名字 `{fleet名}-{hash}` 保证幂等：

[getNewReplicaSet 算哈希并创建新 RS（sync.go:193-236）](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterfleet/sync.go#L193-L236) 关键片段：

```go
podTemplateSpecHash := util.ComputeHash(&newRSTemplate, d.Status.CollisionCount)
newRSTemplate.Labels = labelsutil.CloneAndAddLabel(..., appsv1.DefaultDeploymentUniqueLabelKey, podTemplateSpecHash)
newRSSelector := labelsutil.CloneSelectorAndAddLabel(d.Spec.Selector, appsv1.DefaultDeploymentUniqueLabelKey, podTemplateSpecHash)
// ...
newRS := orchestrationv1alpha1.RayClusterReplicaSet{
    ObjectMeta: metav1.ObjectMeta{
        Name: d.Name + "-" + podTemplateSpecHash,  // 确定性命名，保证幂等
        OwnerReferences: []metav1.OwnerReference{*metav1.NewControllerRef(d, controllerKind)},
    },
    // ...
}
```

注意 `createNewReplicaSet`（[rayclusterfleet_controller.go:247-270](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterfleet/rayclusterfleet_controller.go#L247-L270)）是一个简化版（用 `GenerateName`），实际滚动升级走的是 `sync.go` 里带哈希的 `getNewReplicaSet` 路径。

#### 4.3.4 代码实践

**实践目标**：通过示例 YAML 理解一个 RayClusterFleet 如何描述一个两节点的张量并行推理拓扑。

**操作步骤**：

1. 打开 [samples/distributed/fleet-two-node.yaml](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/samples/distributed/fleet-two-node.yaml)。
2. 定位三个关键部分：
   - `spec.replicas: 1` → 想要 1 个 RayCluster（即 1 套 head+worker 组合）。
   - `spec.template.spec.headGroupSpec` → head 节点跑 `vllm serve ... --tensor-parallel-size 2 --distributed-executor-backend ray`，即 vLLM 借助 Ray 做张量并行。
   - `spec.template.spec.workerGroupSpecs` → 1 个 worker（`small-group`，replicas 1），持有另一张 GPU。
3. 追踪这条链：用户提交这个 Fleet → Fleet 控制器（若模板变了）创建/复用一个 ReplicaSet → ReplicaSet 控制器 `scaleUp` 创建 1 个 RayCluster → **KubeRay operator** 接管，真正拉起 head Pod + worker Pod → vLLM 在多卡间做张量并行推理。
4. 注意 YAML 末尾的 `Service` 和 `HTTPRoute`：它们用 `model.aibrix.ai/name` 标签把流量导到这个 Fleet 暴露的服务上，网关据此路由（承接 u1-l4 的发现机制与 u7 的网关）。

**需要观察的现象 / 预期结果**：

```bash
kubectl apply -f samples/distributed/fleet-two-node.yaml
kubectl get rayclusterfleet,rowrayclusterreplicaset,raycluster -l model.aibrix.ai/name=qwen-coder-7b-instruct
```

预期看到：1 个 Fleet → 1 个 ReplicaSet（名字含哈希）→ 1 个 RayCluster → 其下 head/worker Pod 逐渐 Ready。

> 待本地验证：需要已安装 KubeRay operator 与具备 GPU 的节点；纯 CPU 环境无法真正跑通 vLLM 张量并行，但可观察 CR 的创建与级联关系。

#### 4.3.5 小练习与答案

**练习 1**：Fleet 改了 `spec.template`（换镜像）后，旧的 RayCluster 会立刻被删吗？
**答案**：不会。Fleet 会创建一个**新 ReplicaSet**（新哈希），按 `RollingUpdate` 策略（`maxSurge`/`maxUnavailable`）逐步把副本从旧 RS 迁到新 RS。旧 RS 不会立刻删除，而是被缩到 0 并保留（受 `revisionHistoryLimit` 控制，默认 10），以便回滚。

**练习 2**：为什么 Fleet 的 `Owns` 既要 ReplicaSet 又要 RayCluster？只 Owns ReplicaSet 不够吗？
**答案**：只 Owns ReplicaSet 也能靠 OwnerReference 级联删除，但 Fleet 的 reconcile 需要**实时感知 RayCluster 的就绪状态**来计算 `readyReplicas`、判断滚动是否完成。`Owns(&RayCluster{})` 让 RayCluster 的状态变化也能触发 Fleet 的 reconcile，保证状态收敛及时。

---

### 4.4 分布式推理拓扑：三层控制层级总览

#### 4.4.1 概念说明

把前两个模块串起来，就得到 AIBrix 分布式推理的完整拓扑——一个严格的三层所有权链：

```
RayClusterFleet          （用户面向，管版本/滚动/回滚，对应 Deployment）
   │ owns
   ▼
RayClusterReplicaSet     （管副本数，对应 ReplicaSet；一个 Fleet 不同版本各有其 RS）
   │ owns
   ▼
RayCluster  (ray.io)     （KubeRay 管理，描述 head+worker 拓扑）
   │ owns
   ▼
Pod                       （真正的推理容器，含 vLLM + aibrix-runtime 边车）
```

设计哲学是**单一职责 + 复用 K8s 成熟模式**：

- Fleet 只关心「我当前应该是哪个版本的模板、新旧 RS 各占多少副本」——它把「具体维持几个 RayCluster」完全委托给 ReplicaSet。
- ReplicaSet 只关心「维持期望副本数个 RayCluster」——它把「RayCluster 里 head/worker 怎么连、Ray 怎么起」完全委托给 KubeRay。
- KubeRay 只关心「把一个 RayCluster 描述对象变成真实运行的 Ray 集群」。

每一层都不越界。这种分层让 AIBrix 能直接复用 Kubernetes 社区千锤百炼的 Deployment/ReplicaSet 算法（哈希、revision、滚动、回滚），只把「被管对象」从 Pod 替换成 RayCluster。

#### 4.4.2 核心流程：一次模板变更的完整生命周期

```
用户：kubectl edit rayclusterfleet（改 image）
  │
  ▼
1. Fleet reconcile：发现模板哈希变了 → 原 RS 不再是「新 RS」
2. Fleet 创建新 RS（新哈希），revision = maxOldRevision + 1
3. Fleet 按策略分配副本：新 RS 扩容、旧 RS 缩容（RollingUpdate）
4. 新 RS reconcile：current(0) < desired → scaleUp → 创建新 RayCluster
5. 旧 RS reconcile：current > desired(0) → scaleDown → 删除旧 RayCluster
6. KubeRay：为新 RayCluster 拉 head+worker Pod，删旧的
7. Fleet 据 RayCluster 就绪状态更新 status（UpdatedReplicas 等）
```

副本在新旧 RS 间的分配遵循 Deployment 的比例分配算法（[sync.go 的 scale 函数，sync.go:313 起](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterfleet/sync.go#L313)），目标是：在不超过 `maxSurge`（总副本上限）和不少于 `maxUnavailable`（可用下限）的前提下，尽快把流量切到新版本。

可用副本的稳定化可用一个简单关系描述：滚动期间任意时刻，期望满足

\[
\text{available}(\text{新RS}) + \text{available}(\text{旧RS}) \;\geq\; \text{desiredReplicas} - \text{maxUnavailable}
\]

同时总副本数不超过：

\[
\text{total} \;\leq\; \text{desiredReplicas} + \text{maxSurge}
\]

这两条约束共同定义了「先建新再删旧」的安全节奏。

#### 4.4.3 源码精读

层级关系最直接的证据是三段 `OwnerReference` 代码，它们闭合成链：

**Fleet → ReplicaSet**：Fleet 创建 RS 时把自己设为 controller owner。
[createNewReplicaSet 设置 OwnerReference（rayclusterfleet_controller.go:254-256）](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterfleet/rayclusterfleet_controller.go#L254-L256)（滚动升级的真实路径在 [sync.go:213](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterfleet/sync.go#L213) 同样设置 `NewControllerRef(d, controllerKind)`）。

**ReplicaSet → RayCluster**：ReplicaSet 创建 RayCluster 时把自己设为 controller owner。
[constructRayCluster 设置 OwnerReference（rayclusterreplicaset_utils.go:100-102）](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterreplicaset/rayclusterreplicaset_utils.go#L100-L102)

```go
OwnerReferences: []metav1.OwnerReference{
    *metav1.NewControllerRef(replicaset, controllerKind),
},
```

**RayCluster → Pod**：这一层由 **KubeRay operator** 负责，不在 AIBrix 仓库内（AIBrix 不直接创建 Pod）。

级联删除就是顺着这条链：删 Fleet → GC 删其 RS → GC 删其 RayCluster → KubeRay 的 GC 删 Pod。所以用户只需 `kubectl delete rayclusterfleet`，整套多节点拓扑会被干净地回收。

最后看 Fleet 如何把 RayCluster 按归属 RS 分桶（用于精确计算每个 RS 的副本数）：

[getRayClusterMapForFleet 按 owner UID 分桶（rayclusterfleet_controller.go:213-245）](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterfleet/rayclusterfleet_controller.go#L213-L245) 取每个 RayCluster 的 `controllerRef.UID`，归入对应 RS 的桶里——这正是滚动升级时区分「新版本的集群」与「旧版本的集群」的依据。

#### 4.4.4 代码实践

**实践目标**：亲手画出三层拓扑并验证 OwnerReference 闭合成链（实践任务的核心）。

**操作步骤**：

1. 准备一张纸或文本文件，画三个方框：`RayClusterFleet` / `RayClusterReplicaSet` / `RayCluster (ray.io)`，用箭头标 `owns`。
2. 在每个箭头旁标注对应源码位置：
   - Fleet→RS：[rayclusterfleet_controller.go:254-256](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterfleet/rayclusterfleet_controller.go#L254-L256)
   - RS→RayCluster：[rayclusterreplicaset_utils.go:100-102](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterreplicaset/rayclusterreplicaset_utils.go#L100-L102)
   - RayCluster→Pod：KubeRay operator（仓库外）
3. 在拓扑图上再标出每个控制器装配时 `Owns` 了谁：
   - Fleet：[rayclusterfleet_controller.go:78-79](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterfleet/rayclusterfleet_controller.go#L78-L79)（同时 Owns RS 和 RayCluster）
   - RS：[rayclusterreplicaset_controller.go:75](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/rayclusterreplicaset/rayclusterreplicaset_controller.go#L75)（只 Owns RayCluster）
4. 回答实践任务的两个问题（见下）。

**需要观察的现象 / 预期结果**：得到一张完整的三层拓扑图，能指出每层的职责边界与 OwnerReference 闭合点。

#### 4.4.5 小练习与答案

**练习 1（实践任务问题一）**：为什么 `controller.go` 要先检测 `rayclusters.ray.io` CRD 是否存在？
**答案**：分布式推理控制器在装配（`For`/`Owns`）和 reconcile（`List`/`Create` `rayclusterv1.RayCluster`）时强依赖 KubeRay 的 CRD。若 CRD 不存在，`SetupWithManager` 会抛 `NoKindMatchError`、reconcile 时 List 会失败。AIBrix 把 KubeRay 定位为**可选依赖**，因此在注册前探测：存在才注册，不存在则优雅跳过（只打日志），让不需要分布式推理的用户也能正常用 AIBrix 其余功能。而探测本身的非 NotFound 错误（RBAC/API Server 故障）则 fail fast，避免静默吞错。

**练习 2（实践任务问题二）**：ReplicaSet 与 Fleet 之间是什么层级关系？
**答案**：是**父子拥有关系**（Fleet 拥有 ReplicaSet），类比 Deployment↔ReplicaSet。Fleet 面向用户、负责版本管理与滚动升级，按模板哈希维护一个「新 RS」+ 若干「旧 RS」；ReplicaSet 负责把自己那份副本数维持好（创建/删除 RayCluster）。二者通过 OwnerReference 闭合：删 Fleet 级联删 RS，删 RS 级联删 RayCluster。一个 Fleet 可同时拥有多个 RS（对应不同版本），但任一时刻只有一个「新 RS」承担实际副本。

**练习 3**：如果用户直接创建一个 `RayClusterReplicaSet`（不建 Fleet），系统会怎样？
**答案**：完全合法。ReplicaSet 控制器独立工作，不依赖 Fleet。用户会得到稳定的副本数管理，但**没有滚动升级/回滚能力**——改模板不会自动迁移，需要手动删旧 RS 建新的。这跟原生世界里「可以直接用 ReplicaSet，但通常用 Deployment」是同一个道理。

## 5. 综合实践

**任务**：在一个已安装 KubeRay 的集群里，走完「从 Fleet 到 Pod」的完整链路，并验证三层级联。

**步骤**：

1. 安装依赖与 CRD（承接 u1-l4 的三步法）：
   ```bash
   kubectl apply -f config/dependency/kuberay-operator   # 关键：先装 KubeRay
   kubectl apply -f config/crd                            # AIBrix CRD，含 RayClusterFleet/ReplicaSet
   kubectl apply -f config/default                       # AIBrix operator
   ```
2. 确认 operator 日志里**没有** `KubeRay CRD not found` 降级提示（因为这次装了 KubeRay，4.1 的检测应通过）。
3. 部署示例：
   ```bash
   kubectl apply -f samples/distributed/fleet.yaml       # 单节点版，资源要求低
   ```
4. 用一条命令观察三层对象的出现与归属：
   ```bash
   kubectl get rayclusterfleet,rayclusterreplicaset,raycluster -l model.aibrix.ai/name=qwen-coder-7b-instruct
   ```
   预期：先出现 Fleet，再出现一个名字含哈希的 ReplicaSet，再出现一个 RayCluster。
5. 验证 OwnerReference 闭合：
   ```bash
   kubectl get rayclusterreplicaset <rs-name> -o jsonpath='{.metadata.ownerReferences}'
   kubectl get raycluster <rc-name> -o jsonpath='{.metadata.ownerReferences}'
   ```
   预期：RS 的 owner 是 Fleet，RayCluster 的 owner 是 RS。
6. 验证级联删除：`kubectl delete rayclusterfleet qwen-coder-7b-instruct`，预期 RS、RayCluster、Pod 依次自动消失。
7. （源码验证）把 `spec.replicas` 从 1 改成 2，观察 ReplicaSet 控制器的 `scaleUp` 是否新建一个 RayCluster；改回 1，观察 `scaleDown` 按排序规则删除哪一个。

> 待本地验证：本实践依赖真实 K8s 集群、KubeRay operator 与（理想情况下）GPU 节点。无集群时可降级为：对照源码在纸上推演上述每一步对应哪个控制器的哪个函数（Fleet 的 sync/rolling、RS 的 scaleUp/scaleDown、KubeRay 的拉起 Pod）。

## 6. 本讲小结

- AIBrix 的分布式推理把 **KubeRay** 当作可选依赖，在控制器注册前用 `checkCRDExists` 探测 `rayclusters.ray.io`：不存在则优雅降级（只打日志），其他错误 fail fast。
- 整体是严格三层所有权链：**RayClusterFleet → RayClusterReplicaSet → RayCluster (ray.io) → Pod**，分别对应 Deployment、ReplicaSet、KubeRay、容器运行时，每层单一职责。
- `RayClusterReplicaSet` 复刻原生 ReplicaSet 语义，用 `Expectations` 机制防抖动，靠 `filterActiveClusters` + 缩容排序规则（未就绪→低成本→旧→字典序）决定删谁。
- `RayClusterFleet` 复刻原生 Deployment 语义，通过模板哈希 + revision 管理新旧 ReplicaSet，支持 RollingUpdate/Recreate/回滚/暂停，并暴露 scale 子资源供外部伸缩器使用。
- 三层通过 `metav1.NewControllerRef` 设置的 OwnerReference 闭合，删除 Fleet 即可干净回收整条链；AIBrix 不直接创建 Pod，把执行细节完全交给 KubeRay。
- `RayClusterTemplateSpec` 是与 KubeRay 的唯一耦合点——它直接内嵌 `rayclusterv1.RayClusterSpec`，AIBrix 不重新发明 head/worker 字段。

## 7. 下一步学习建议

- **下一讲 u5-l2（StormService 拓扑）**：本讲的 Fleet/ReplicaSet 面向「对称的多副本 RayCluster」；而 StormService/RoleSet/PodSet 面向 **Prefill/Decode 解耦**这类非对称拓扑，是更现代的推理编排方式，建议紧接着学。
- **横向回看 u3（自动伸缩）**：Fleet 暴露了 scale 子资源，PodAutoscaler（u3-l4 的 `WorkloadScale`）可以像伸缩 Deployment 一样伸缩 Fleet——可回去对照「伸缩目标如何指向分布式推理拓扑」。
- **深入 KubeRay**：本讲的 `RayClusterSpec` 来自 `github.com/ray-project/kuberay`，若要理解 head/worker 如何组网、Ray 启动参数含义，建议阅读 KubeRay 仓库的 `ray-cluster.types.go` 与其文档。
- **网关侧**：分布式推理拓扑最终通过 `model.aibrix.ai/name` 标签被网关发现并路由（见 [fleet-two-node.yaml](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/samples/distributed/fleet-two-node.yaml) 末尾的 HTTPRoute），可在学完 u7（网关）后回来看这条衔接。
