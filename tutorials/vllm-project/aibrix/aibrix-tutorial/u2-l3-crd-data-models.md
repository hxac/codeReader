# 自定义资源 (CRD) 数据模型设计

## 1. 本讲目标

本讲聚焦 AIBrix 控制平面的「数据层」——也就是 `api/` 目录下用 Go 结构体定义的自定义资源（CR）数据模型。学完本讲，你应当能够：

- 看懂 AIBrix 三大 API 分组（`autoscaling` / `model` / `orchestration`）各自的 GroupVersion 与归属的 CR。
- 读懂任意一个 CR 的 `Spec`（期望状态）与 `Status`（观测状态）字段，并能说出每个关键字段的作用。
- 理解 kubebuilder 注解（`+kubebuilder:...`）如何驱动 CRD YAML 与 deepcopy 代码的自动生成，特别是 `printcolumn` 如何决定 `kubectl get` 的输出列。

本讲只讲「数据模型长什么样、怎么生成出来的」，不讲控制器 reconcile 逻辑（那是后续 u3/u4 讲义的内容）。

## 2. 前置知识

- **CRD 与 CR**：CRD（CustomResourceDefinition）是 Kubernetes 里的「类型声明」，相当于告诉集群「我现在要管理一种新的资源类型」；CR（CustomResource）是这个类型的「实例」，是一条用户提交的具体配置。可以类比：CRD 是数据库的表结构，CR 是表里的一行数据。
- **声明式 API（Declarative API）**：用户写一份 YAML 声明「我想要的样子（Spec）」，控制器负责把它变成现实，并不断把「现在的样子（Status）」写回对象。**Spec 由用户写、Status 由控制器写**，这是后续所有字段设计的根本分工。
- **GroupVersionKind（GVK）**：Kubernetes 用「分组 + 版本 + 类型」三段式唯一标识一个资源类型，例如 `autoscaling.aibrix.ai / v1alpha1 / PodAutoscaler`。Go 程序要操作某类型前，必须先把它注册到 Scheme（类型注册表）里，这一点承接自 u2-l1。
- **kubebuilder 与 controller-gen**：kubebuilder 是构建 K8s 控制器的脚手架规范。它约定用「Go 结构体 + 注释里的标记（marker）」来描述 CRD，然后用 `controller-gen` 工具把这些标记翻译成 CRD YAML 和 deepcopy 代码。这承接自 u1-l3 讲过的 `make manifests` / `make generate`。
- **deepcopy**：控制器经常要复制对象再修改（避免改到缓存里的原始对象），因此每个 CR 类型都需要一个「深拷贝」方法。手写易错，所以由代码生成。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [api/autoscaling/v1alpha1/groupversion_info.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/groupversion_info.go) | 声明 `autoscaling.aibrix.ai` 分组与 Scheme 注册入口 |
| [api/model/v1alpha1/groupversion_info.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/model/v1alpha1/groupversion_info.go) | 声明 `model.aibrix.ai` 分组与 Scheme 注册入口 |
| [api/orchestration/v1alpha1/groupversion_info.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/groupversion_info.go) | 声明 `orchestration.aibrix.ai` 分组、Kind 常量与 Scheme 入口 |
| [api/autoscaling/v1alpha1/podautoscaler_types.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/podautoscaler_types.go) | PodAutoscaler 的 Spec/Status 与 kubebuilder 标记（本讲主线） |
| [api/model/v1alpha1/modelclaim_types.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/model/v1alpha1/modelclaim_types.go) | ModelClaim 的 Spec/Status（模型激活数据模型） |
| [api/model/v1alpha1/modeladapter_types.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/model/v1alpha1/modeladapter_types.go) | ModelAdapter 的 Spec/Status（LoRA 适配器数据模型） |
| [api/orchestration/v1alpha1/kvcache_types.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/kvcache_types.go) | KVCache CR（分布式缓存部署编排）示例 |
| [api/orchestration/v1alpha1/stormservice_types.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/stormservice_types.go) | StormService CR（带 scale 子资源的高级示例） |

## 4. 核心概念与源码讲解

### 4.1 三组 API 分组与版本

#### 4.1.1 概念说明

AIBrix 没有把所有 CR 塞进一个 API 分组，而是按**能力域**拆成三组：

| 分组（Group） | 能力域 | 包含的 CR |
|---------------|--------|-----------|
| `autoscaling.aibrix.ai` | 应用级自动伸缩 | PodAutoscaler |
| `model.aibrix.ai` | 模型适配与激活 | ModelAdapter、ModelClaim |
| `orchestration.aibrix.ai` | 分布式推理与拓扑编排 | StormService、RoleSet、PodSet、RayClusterReplicaSet、RayClusterFleet、KVCache |

这样做的好处是：每组对应一个相对独立的子系统，分组名本身就是文档；将来某个子系统大改版本，只影响它自己的分组，不会牵连其他组。所有分组当前都处于 `v1alpha1`，说明这些 API 仍在演进、尚未承诺向后兼容。

#### 4.1.2 核心流程

每个分组都用一份几乎相同的 `groupversion_info.go` 来「开张营业」，套路是固定的：

1. 在包注释里写两个标记：`+kubebuilder:object:generate=true`（告诉 controller-gen 为本包生成 deepcopy）和 `+groupName=xxx.aibrix.ai`（声明分组名）。
2. 定义一个 `GroupVersion` 变量，把「分组名 + 版本」绑在一起。
3. 用它构造一个 `SchemeBuilder`（控制器运行时的类型注册器）。
4. 把 `SchemeBuilder.AddToScheme` 暴露为 `AddToScheme` 函数——控制器入口（u2-l1 的 main.go）会调用它，把这组类型注册进 Manager 的 Scheme。
5. 每个 CR 类型文件在自己的 `init()` 里调用 `SchemeBuilder.Register(&Xxx{}, &XxxList{})` 把自己登记进去。

#### 4.1.3 源码精读

以 autoscaling 分组为例，[groupversion_info.go:27-43](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/groupversion_info.go#L27-L43) 给出了模板化结构：

```go
var (
    GroupVersion = schema.GroupVersion{Group: "autoscaling.aibrix.ai", Version: "v1alpha1"}
    SchemeGroupVersion = GroupVersion
    SchemeBuilder = &scheme.Builder{GroupVersion: GroupVersion}
    AddToScheme = SchemeBuilder.AddToScheme
)
```

`model.aibrix.ai` 与 `orchestration.aibrix.ai` 两个分组用的是完全相同的四件套，只是 `Group` 字符串不同。orchestration 分组额外多了一组 Kind 常量（[groupversion_info.go:27-31](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/groupversion_info.go#L27-L31)），方便控制器在代码里用常量而非裸字符串引用类型名，减少拼写错误：

```go
const (
    StormServiceKind = "StormService"
    RoleSetKind      = "RoleSet"
    PodSetKind       = "PodSet"
)
```

注册的「下游消费点」在控制器入口 main.go 里，承接 u2-l1：[cmd/controllers/main.go:90-104](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/controllers/main.go#L90-L104) 里可以看到三个 `AddToScheme(scheme)` 是**条件化**调用的——是否注册取决于 Feature Gate 是否启用了对应控制器。这就是 u2-l2 讲的「Feature Gate 决定加载哪些 Scheme」在数据层的体现。

#### 4.1.4 代码实践

**实践目标**：亲手对照三份 `groupversion_info.go`，确认「分组名」与「所在目录」的对应关系。

**操作步骤**：

1. 打开 `api/autoscaling/v1alpha1/groupversion_info.go`，找到 `+groupName=...` 与 `Group: "..."` 两处，记录分组名。
2. 对 `api/model/v1alpha1/groupversion_info.go`、`api/orchestration/v1alpha1/groupversion_info.go` 重复。
3. 在仓库根目录运行 `ls api/`，观察二级目录名。

**需要观察的现象**：三个目录名（autoscaling / model / orchestration）与三个分组名的前缀（即 `.aibrix.ai` 之前的部分）一一对应。这不是巧合，而是 kubebuilder 项目布局约定：**目录路径 = 分组名，目录内的 `v1alpha1` 子目录 = 版本**。

**预期结果**：

| 目录 | 分组名 | 版本 |
|------|--------|------|
| api/autoscaling/v1alpha1 | autoscaling.aibrix.ai | v1alpha1 |
| api/model/v1alpha1 | model.aibrix.ai | v1alpha1 |
| api/orchestration/v1alpha1 | orchestration.aibrix.ai | v1alpha1 |

#### 4.1.5 小练习与答案

**练习 1**：为什么 AIBrix 要把自动伸缩、模型适配、拓扑编排分成三个 API 分组，而不是塞进一个 `aibrix.ai` 分组？

**答案**：按能力域分组的核心理由是「演进隔离」与「可读性」。自动伸缩（PodAutoscaler）、模型激活（ModelClaim）、分布式编排（StormService）各自生命周期不同，将来某一组需要升版本（如升到 v1beta1）时，另两组可以继续停在 v1alpha1；同时分组名本身充当文档，看 GVK 就能知道这块配置属于哪个子系统。

**练习 2**：如果新建一个 CR，忘记在 main.go 里调用它的 `AddToScheme`，会发生什么？

**答案**：该类型未注册进 Scheme，控制器运行时无法反序列化（decode）对应类型的对象，读写该 CR 时会报「no kind registered for the type」一类的错误。这就是 u2-l1 强调「Scheme 注册必须与控制器注册一致」的原因。

### 4.2 Spec/Status 字段设计

#### 4.2.1 概念说明

每个 AIBrix CR 都遵循 K8s 声明式 API 的统一骨架：

```
RootStruct {
    TypeMeta   // apiVersion / kind，序列化自动填
    ObjectMeta // name / namespace / labels / annotations
    Spec       // 用户期望状态（desired state）—— 用户写
    Status     // 控制器观测状态（observed state）—— 控制器写
}
```

Spec 与 Status 的分工是 K8s 数据模型最重要的纪律：**Spec 只进不出（用户意图）、Status 只出不进（系统实况）**。控制器的工作就是不断缩小 Spec 与 Status 之间的差距。下面以三个最典型的 CR 为例精读字段。

#### 4.2.2 核心流程

字段设计的一般思路：

1. **目标引用**：先指出「这个 CR 要管谁」（如 PodAutoscaler 的 `ScaleTargetRef`、ModelClaim 的 `PodSelector`）。
2. **期望参数**：再给出「想怎么管」（如伸缩策略、副本数、指标源、模型权重地址）。
3. **状态回写**：Status 侧提供 phase、副本计数、conditions 三件套，让用户和上层系统看得清当前进展。

#### 4.2.3 源码精读

**(a) PodAutoscaler —— 自动伸缩的数据模型**

Root 结构见 [podautoscaler_types.go:42-51](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/podautoscaler_types.go#L42-L51)，标准四件套（TypeMeta + ObjectMeta + Spec + Status）：

```go
type PodAutoscaler struct {
    metav1.TypeMeta   `json:",inline"`
    metav1.ObjectMeta `json:"metadata,omitempty"`
    Spec   PodAutoscalerSpec   `json:"spec,omitempty"`
    Status PodAutoscalerStatus `json:"status,omitempty"`
}
```

Spec 字段（[podautoscaler_types.go:54-98](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/podautoscaler_types.go#L54-L98)）关键字段：

```go
type PodAutoscalerSpec struct {
    ScaleTargetRef       corev1.ObjectReference `json:"scaleTargetRef"`        // 要伸缩谁
    SubTargetSelector    *SubTargetSelector     `json:"subTargetSelector,omitempty"` // 选 StormService/RoleSet 的某个 role
    MinReplicas          *int32                 `json:"minReplicas,omitempty"`
    MaxReplicas          int32                  `json:"maxReplicas"`           // 必填，且不能小于 minReplicas
    MetricsSources       []MetricSource         `json:"metricsSources,omitempty"` // 至少 1 个指标源
    ObserveWindowSeconds *int64                 `json:"observeWindowSeconds,omitempty"` // 平稳窗口 1..3600
    PanicWindowSeconds   *int64                 `json:"panicWindowSeconds,omitempty"`   // 紧急窗口 1..3600
    ScalingStrategy      ScalingStrategyType    `json:"scalingStrategy"`       // 枚举 HPA/KPA/APA
}
```

其中 `ScalingStrategyType` 是一个字符串枚举（[podautoscaler_types.go:107-119](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/podautoscaler_types.go#L107-L119)），取值 HPA / KPA / APA，对应 u3-l2 将精读的三种伸缩算法。`MetricSource`（[podautoscaler_types.go:145-166](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/podautoscaler_types.go#L145-L166)）描述从哪里拉指标（pod/resource/custom/external），以及 `targetMetric` + `targetValue` 这对「指标名 + 阈值」。

Status 字段（[podautoscaler_types.go:187-212](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/podautoscaler_types.go#L187-L212)）：

```go
type PodAutoscalerStatus struct {
    LastScaleTime   *metav1.Time      `json:"lastScaleTime,omitempty"`
    DesiredScale    int32             `json:"desiredScale,omitempty"`   // 算法算出的期望副本
    ActualScale     int32             `json:"actualScale,omitempty"`    // 实际运行副本
    Conditions      []metav1.Condition `json:"conditions,omitempty"`
    ScalingHistory  []ScalingDecision  `json:"scalingHistory,omitempty"` // 最近 N 条决策，上限 5
}
```

注意 `ScalingHistory` 用 `+kubebuilder:validation:MaxItems=5` 限制了条数，防止历史记录无限膨胀把 etcd 对象撑大。

**(b) ModelClaim —— 模型激活的数据模型**

ModelClaim 描述「在一组暖机 GPU Pod 上激活一个完整模型」，Spec 见 [modelclaim_types.go:30-68](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/model/v1alpha1/modelclaim_types.go#L30-L68)：

```go
type ModelClaimSpec struct {
    ModelName     *string                `json:"modelName,omitempty"`     // 客户端请求里的 model 字段
    PodSelector   *metav1.LabelSelector  `json:"podSelector,omitempty"`   // 选哪些暖机 Pod
    ArtifactURL   string                 `json:"artifactURL,omitempty"`   // 权重地址 s3:// gcs:// huggingface://
    Engine        string                 `json:"engine,omitempty"`        // vllm / sglang
    Replicas      *int32                 `json:"replicas,omitempty"`      // 当前固定为 1
    EngineConfig  *ModelClaimEngineConfig `json:"engineConfig,omitempty"` // 引擎 CLI 参数
}
```

Status（[modelclaim_types.go:124-152](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/model/v1alpha1/modelclaim_types.go#L124-L152)）的核心是一个生命周期相位 `Phase`，取值见 [modelclaim_types.go:82-106](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/model/v1alpha1/modelclaim_types.go#L82-L106)：`Pending → Scheduling → Loading → Activating → Active`（成功路径），以及 `Sleeping` / `Failed` / `Unknown`。注意 `Activating` 相位的注释说明了一个重要设计：引擎已启动但未就绪时，路由注解会被钉在「不可路由标记（端口 0）」上，避免网关把请求打到还没启动好的引擎。

**(c) ModelAdapter —— LoRA 适配器的数据模型**

ModelAdapter 描述高密度 LoRA 适配器的加载，Spec 见 [modeladapter_types.go:27-61](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/model/v1alpha1/modeladapter_types.go#L27-L61)。一个有意思的字段是 `Replicas`：

```go
// - nil (omitted): Load adapter on ALL matching pods (recommended)
// - 1: Load adapter on a single pod selected by the scheduler
// +kubebuilder:validation:Enum=1
Replicas *int32 `json:"replicas,omitempty"`
```

这里用「nil 表示全部、1 表示单 Pod」的双语义，并用 `+kubebuilder:validation:Enum=1` 强制只能填 1（填 2 会被 CRD 校验拒绝）。Status 相位见 [modeladapter_types.go:66-84](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/model/v1alpha1/modeladapter_types.go#L66-L84)：`Pending → Scheduled → Bound → ResourceCreated → Running`。

#### 4.2.4 代码实践

**实践目标**：阅读 PodAutoscaler 与 ModelClaim 的类型文件，亲手整理出 Spec/Status 字段表（即本讲的指定实践任务，先做阅读版，综合实践里再补 printcolumn 列）。

**操作步骤**：

1. 打开 `api/autoscaling/v1alpha1/podautoscaler_types.go`，逐字段抄写 `PodAutoscalerSpec` 的字段名、类型、json tag、是否 `+optional`。
2. 同样处理 `PodAutoscalerStatus`。
3. 打开 `api/model/v1alpha1/modelclaim_types.go`，对 `ModelClaimSpec` / `ModelClaimStatus` 重复。

**需要观察的现象**：
- 必填字段（无 `+optional` 且无 `omitempty` 默认值）与可选字段（带 `+optional` / `*指针`）的区别。
- Status 里都有「副本计数三件套」：`DesiredXxx` / `ReadyXxx` / `Candidates`，以及 `Conditions`。

**预期结果**（以 PodAutoscaler 为例的骨架，供你对照填充）：

| 字段（json tag） | 类型 | 必填? | 含义 |
|------------------|------|-------|------|
| `scaleTargetRef` | ObjectReference | 是 | 要伸缩的目标资源引用 |
| `maxReplicas` | int32 | 是 | 最大副本数 |
| `scalingStrategy` | enum | 是 | HPA/KPA/APA |
| `metricsSources` | []MetricSource | MinItems=1 | 指标来源列表 |
| ... | ... | ... | （其余请你自行补全） |

#### 4.2.5 小练习与答案

**练习 1**：PodAutoscaler 的 `MinReplicas` 是 `*int32`（指针）而 `MaxReplicas` 是 `int32`（值类型），为什么这样区分？

**答案**：指针类型能区分「显式填了 0」和「没填」。在 K8s 里，伸缩下限为 0 是合法语义（允许缩到零以省资源），而值类型的 0 无法和「未设置」区分，所以必须用指针。`MaxReplicas` 作为硬上限必须显式给出，故用值类型。

**练习 2**：ModelClaim 的 `Replicas` 注释说「当前固定为 1」，但字段类型是 `*int32` 而非常量，为什么不直接去掉？

**答案**：字段保留为 `*int32` 是为「前向兼容」预留——未来支持多副本时只需放开校验（去掉 `Minimum=1,Maximum=1`），而无需改动 API 结构、破坏存量 CR 的序列化。这是 CRD 数据模型常见的「字段先行、能力后到」设计。

### 4.3 kubebuilder 标记与代码生成

#### 4.3.1 概念说明

kubebuilder 标记（marker）是写在 Go 注释里、以 `+` 开头的「元信息」，本身不影响 Go 编译，但会被 `controller-gen` 工具读取，用来生成两类产物：

1. **CRD YAML**（`make manifests` 产出，落到 `config/crd/<分组>/`）：包括 OpenAPI schema（字段校验）、`printcolumn`（kubectl 输出列）、`shortName`、子资源（`/status`、`/scale`）等。
2. **deepcopy 代码**（`make generate` 产出，落到 `api/<分组>/v1alpha1/zz_generated.deepcopy.go`）：每个类型的 `DeepCopy()` / `DeepCopyInto()` 方法。

掌握「哪个标记生成什么」是阅读 AIBrix 数据模型的关键。

#### 4.3.2 核心流程

标记大致分三层：

| 层级 | 典型标记 | 作用 |
|------|----------|------|
| 类型级（写在 Root struct 上） | `+kubebuilder:object:root=true` | 标记这是 CRD 根类型（同时其 List 也要标） |
| | `+kubebuilder:subresource:status` | 启用 `/status` 子资源，让 Status 必须经 status 子接口写 |
| | `+kubebuilder:subresource:scale:...` | 启用 `/scale` 子资源（StormService 有） |
| | `+kubebuilder:resource:shortName=mc` | 设置 kubectl 短名 |
| | `+kubebuilder:printcolumn:...` | 设置 `kubectl get` 的列 |
| 字段级（写在字段上方） | `+optional` / `+kubebuilder:validation:Required` | 可选/必填 |
| | `+kubebuilder:validation:Enum=a;b` | 枚举校验 |
| | `+kubebuilder:validation:Minimum/Maximum` | 数值范围 |
| | `+kubebuilder:validation:MinItems/MaxItems` | 切片长度 |
| | `+kubebuilder:default=...` | 默认值 |

生成关系（承接 u1-l3）：`make manifests` 调 controller-gen 产 CRD YAML；`make generate` 调 controller-gen 产 `zz_generated.deepcopy.go`；两者都由 `make build` 间接触发。

#### 4.3.3 源码精读

**(a) printcolumn：决定 kubectl 看到什么**

PodAutoscaler 在 Root struct 上方写了 5 个 printcolumn 标记（[podautoscaler_types.go:34-38](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/podautoscaler_types.go#L34-L38)）：

```go
// +kubebuilder:printcolumn:name="MINPODS",type="integer",JSONPath=".spec.minReplicas"
// +kubebuilder:printcolumn:name="MAXPODS",type="integer",JSONPath=".spec.maxReplicas"
// +kubebuilder:printcolumn:name="REPLICAS",type="integer",JSONPath=".status.actualScale"
// +kubebuilder:printcolumn:name="STRATEGY",type="string",JSONPath=".spec.scalingStrategy"
// +kubebuilder:printcolumn:name="AGE",type="date",JSONPath=".metadata.creationTimestamp"
```

注意 `REPLICAS` 列的 JSONPath 指向的是 `.status.actualScale`（控制器回写的实际副本数），而不是 spec——这体现了「kubectl 表格优先展示运行实况」的惯例。这些标记被 controller-gen 翻译进 CRD YAML 的 `additionalPrinterColumns` 段（见 `config/crd/autoscaling/autoscaling.aibrix.ai_podautoscalers.yaml`）。

ModelClaim 的 printcolumn（[modelclaim_types.go:168-173](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/model/v1alpha1/modelclaim_types.go#L168-L173)）展示了「列可以同时来自 status 和 spec」：`Phase/Desired/Ready` 来自 status，`Engine/Artifact` 来自 spec。

**(b) 短名与子资源**

ModelClaim 用 `+kubebuilder:resource:shortName=mc`（[modelclaim_types.go:166](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/model/v1alpha1/modelclaim_types.go#L166)）声明了短名 `mc`，于是 `kubectl get mc` 等价于 `kubectl get modelclaims`。生成的 YAML 里对应 `shortNames: [mc]`。

`+kubebuilder:subresource:status`（几乎所有 CR 都有）启用 status 子资源：客户端必须用 `UpdateStatus` 而非 `Update` 来写 Status，从而把「用户改 Spec」与「控制器改 Status」两个写入通道隔离，避免互相覆盖。

更高级的是 StormService 的 scale 子资源（[stormservice_types.go:176](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/orchestration/v1alpha1/stormservice_types.go#L176)）：

```go
//+kubebuilder:subresource:scale:specpath=.spec.replicas,statuspath=.status.replicas,selectorpath=.status.scalingTargetSelector
```

它告诉 K8s：本 CR 可以像 Deployment 一样被 `kubectl scale`，伸缩的「读」指向 `.status.replicas`、「写」指向 `.spec.replicas`。这样 PodAutoscaler 的 `ScaleTargetRef` 才能指向一个 StormService 并直接调它的 `/scale` 接口（这也是 PodAutoscaler.Spec 里 `SubTargetSelector` 能选 StormService 某 role 的前置条件）。

**(c) 字段级校验标记**

数值范围、枚举、列表长度的标记散见各 Spec。例如 PodAutoscaler 的 `ObserveWindowSeconds` 限定 1..3600（[podautoscaler_types.go:84-85](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/podautoscaler_types.go#L84-L85)），`MetricsSources` 用 `MinItems=1`（[podautoscaler_types.go:78](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/podautoscaler_types.go#L78)）保证至少一个指标源。这些最终变成 CRD OpenAPI schema，`kubectl apply` 非法值时会被 API Server 直接拒绝，控制器根本看不到非法输入。

**(d) 代码生成产物（待本地验证）**

> 说明：以下命令需在你本地配置好 Go 工具链与 controller-gen 后运行；在只读阅读环境下为「待本地验证」。

运行 `make manifests` 后，CRD YAML 出现在 `config/crd/<分组>/`（注意 AIBrix 按分组分子目录，而非传统的 `config/crd/bases/`）；运行 `make generate` 后，`zz_generated.deepcopy.go` 出现在各 `api/<分组>/v1alpha1/` 目录。`zz_generated` 前缀表示「请勿手改，下次 generate 会覆盖」。

#### 4.3.4 代码实践

**实践目标**：验证「printcolumn 标记 → CRD YAML → kubectl 列」的完整生成链。

**操作步骤**：

1. 打开 `api/model/v1alpha1/modelclaim_types.go`，记录 `printcolumn` 标记里的 6 个 `name` 与对应 JSONPath。
2. 打开 `config/crd/model/model.aibrix.ai_modelclaims.yaml`，定位 `additionalPrinterColumns:` 段，对照是否与标记一一对应。
3. （待本地验证）在一个装好 AIBrix CRD 的集群里 `kubectl get mc -n aibrix-system`，观察输出表头是否正是这 6 列。

**需要观察的现象**：Go 注释里的标记与 YAML 里的 `additionalPrinterColumns`、以及 kubectl 实际表头，三者内容完全一致——这就是 controller-gen 的「单一数据源」价值：改一处注释，重新 generate，三处同步。

**预期结果**：ModelClaim 的 6 列为 `Phase / Desired / Ready / Engine / Artifact / Age`，分别来自 `.status.phase`、`.status.desiredReplicas`、`.status.readyReplicas`、`.spec.engine`、`.spec.artifactURL`、`.metadata.creationTimestamp`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `+kubebuilder:subresource:status` 几乎是每个 CR 的标配？不加会怎样？

**答案**：它把 Status 隔离到独立的 `/status` 子资源。好处有二：一是用户用普通 `Update` 改 CR 时无法误改 Status（Status 只能由控制器经 `UpdateStatus` 写）；二是权限上可单独把 Status 写权限授予控制器 ServiceAccount。不加的话，Spec 和 Status 共用同一个写入端点，容易互相覆盖，也不利于 RBAC 细粒度授权。

**练习 2**：如果你想把 ModelAdapter 在 `kubectl get` 里多显示一列「BaseModel」，应该改哪些地方？

**答案**：只需在 `api/model/v1alpha1/modeladapter_types.go` 的 ModelAdapter Root struct 上方，新增一行 `+kubebuilder:printcolumn:name="BaseModel",type=string,JSONPath=.spec.baseModel`，然后重新 `make manifests` 让 CRD YAML 同步即可。无需改控制器逻辑（控制器只读写对象，不关心展示）。

## 5. 综合实践

**任务**：完成规格指定的「Spec/Status 字段表 + printcolumn 标注」综合表，把本讲三个最小模块串起来。

请为 **PodAutoscaler** 和 **ModelClaim** 各产出一张完整表格，格式如下：

| 字段（json tag） | 所属 | 类型 | 必填? | 含义 | 是否出现在 kubectl 列? |
|------------------|------|------|-------|------|------------------------|

要求：

1. **Spec/Status 字段**：通读 `podautoscaler_types.go` 与 `modelclaim_types.go`，列出 Spec 和 Status 的全部顶层字段（嵌套类型如 `MetricSource`、`ModelClaimInstance` 只需列名并注明「见子结构」）。「必填」列要结合 `+optional` / `+kubebuilder:validation:Required` / 指针类型综合判断。
2. **kubectl 列标注**：在前述 `printcolumn` 标记（PodAutoscaler 在 [podautoscaler_types.go:34-38](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/podautoscaler_types.go#L34-L38)、ModelClaim 在 [modelclaim_types.go:168-173](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/model/v1alpha1/modelclaim_types.go#L168-L173)）中核对，在「是否出现在 kubectl 列」一列打勾，并写出对应的 `name`（如 `MINPODS` / `Phase`）。
3. **反思题**：对比两张表，回答——为什么 PodAutoscaler 的 kubectl 列「偏 Spec」（MINPODS/MAXPODS/STRATEGY 都来自 spec），而 ModelClaim 的列「偏 Status」（Phase/Desired/Ready 来自 status）？结合「自动伸缩器重在配置、模型激活器重在运行实况」各写一句话解释。

**参考思路**（不直接给完整答案，留给你填充）：伸缩类资源的运维者最关心「我设定的上下限和策略」，所以列以 Spec 为主；而模型激活类资源的运维者最关心「模型现在到底活没活、就绪没」，所以列以 Status 的 Phase 与副本计数为主。这种「列的选择反映运维关注点」正是 printcolumn 设计的精髓。

> 提示：填写时若拿不准某字段是否必填，回到源码看它有没有 `+optional` 标记或是否为指针类型——这是判断的金标准，不要凭名字猜。

## 6. 本讲小结

- AIBrix 的 CR 按**能力域**拆成 `autoscaling` / `model` / `orchestration` 三个 API 分组，目录路径与分组名一一对应；每组用模板化的 `groupversion_info.go` 暴露 `AddToScheme`，由 main.go 按需注册。
- 每个 CR 都遵循 K8s 声明式骨架（TypeMeta + ObjectMeta + Spec + Status），**Spec 是用户意图、Status 是控制器实况**；Status 普遍具备「Phase + 副本计数 + Conditions」三件套。
- PodAutoscaler 围绕「目标引用 + 指标源 + 伸缩策略（HPA/KPA/APA）」组织；ModelClaim / ModelAdapter 围绕「PodSelector + 权重地址 + 引擎参数 + 生命周期 Phase」组织。
- kubebuilder 标记是「单一数据源」：类型级标记生成 CRD YAML（printcolumn / shortName / status / scale 子资源），字段级标记生成 OpenAPI 校验（Enum/Minimum/MaxItems 等），`+k8s:deepcopy-gen` 与 `+kubebuilder:object:root=true` 共同驱动 deepcopy 生成。
- `printcolumn` 决定 `kubectl get` 的列，其 JSONPath 既可指向 spec 也可指向 status；`+kubebuilder:subresource:status` 与 StormService 的 `subresource:scale` 是理解「Spec/Status 写入隔离」与「可被 PodAutoscaler 伸缩」的关键。

## 7. 下一步学习建议

- 本讲只看了数据模型，**控制器如何消费这些 Spec、写这些 Status** 是下一阶段重点。建议接着读 u3-l1（PodAutoscaler 控制器与 reconcile 主循环）、u4-l1（ModelAdapter 控制器）和 u4-l3（ModelClaim 激活协议），把「字段」和「填字段的代码」对应起来。
- 若想深入 kubebuilder 代码生成机制，可对照本讲的标记，阅读 `Makefile` 中 `manifests` / `generate` 目标（u1-l3 已铺垫），并实际运行一次观察 `zz_generated.deepcopy.go` 与 CRD YAML 的变化。
- StormService 的 `subresource:scale` 是本讲埋下的伏笔，其完整含义会在 u5-l2（StormService 拓扑与 Prefill/Decode 解耦编排）展开，届时可回看 PodAutoscaler 的 `SubTargetSelector` 是如何与此联动的。
