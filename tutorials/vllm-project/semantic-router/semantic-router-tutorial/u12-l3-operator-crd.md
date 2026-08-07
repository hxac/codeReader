# Operator 与 CRD

## 1. 本讲目标

本讲是「本地编排与部署」单元的第三篇，承接 u12-l2 的结尾——那里我们指出 `deploy/kubernetes/operator` 变体由一个自定义资源 `SemanticRouter` CRD 驱动。本讲就把这个 CRD 和它背后的 Operator（控制器）彻底拆开。

学完后你应当能够：

- 读懂 `SemanticRouter` CRD 的 schema 结构，知道用户该在 `spec` 里写什么、`status` 又反映了什么。
- 说清一次 Reconcile（调和）从被触发到更新状态经历了哪些步骤、按什么顺序创建/更新哪些 Kubernetes 资源。
- 理解 Operator 如何把 CRD 的 `spec` **翻译**成路由器内核认识的 canonical v0.3 `config.yaml`，以及为什么这是 Operator 的真正核心。
- 掌握 `vllmEndpoints` 这个「Kubernetes 原生后端发现」适配器：它如何把集群里活着的 KServe/LlamaStack/Service 探测成 `config.providers.models[].backend_refs`。
- 区分 standalone 与 gateway-integration 两种 Envoy/Gateway 接入模式。

> 本讲承接 u3-l1（config.yaml v0.3 七大段落）与 u12-l2（Helm/K8s 部署形态）。如果不熟悉 canonical config 的 `version/listeners/providers/routing/global` 结构，建议先回看 u3-l1。

## 2. 前置知识

本讲用到的 Kubernetes 与 Operator 概念，先做最简解释：

- **CRD（CustomResourceDefinition，自定义资源定义）**：向 Kubernetes 注册一种「新的资源类型」。注册后，你就能像写 `kind: Deployment` 一样写 `kind: SemanticRouter`，把它存进 etcd。
- **CR（Custom Resource，自定义资源实例）**：CRD 的一个具体对象，例如一个 `kind: SemanticRouter` 的 YAML，描述「我想要一个什么样的语义路由器」。
- **Controller / Reconcile（控制器 / 调和）**：一个常驻进程，持续「观察实际状态，把它推向期望状态」。`Reconcile(ctx, req)` 是每次被唤醒后执行的函数：拿到 CR → 比对 → 创建/更新依赖资源 → 写回状态。它是**幂等**的——不关心「为什么被叫醒」，只把当前世界修正成期望的样子。
- **controller-runtime / Kubebuilder**：Go 生态写 Operator 的事实标准库与脚手架。本项目的 Operator 就用它写的：`ctrl.Manager` 管理控制器、`r.Client` 增删改查 K8s 对象、`SetupWithManager` 声明「监视谁、Owns 谁」。
- **Owner Reference（属主引用）**：给子资源打上「我由某个 CR 创建」的标记。CR 被删时，K8s 垃圾回收会自动连带删除它拥有的子资源。
- **Finalizer**：删除前的一道「刹车」。带 finalizer 的对象不会立即消失，控制器先做清理（如删 PVC），再移除 finalizer，对象才真正被删。

一句话定位：**Operator = 一个 Reconcile 循环，它把用户写的 `SemanticRouter` CR 翻译成一整套 K8s 资源 + 一份 canonical config.yaml，并持续维护它们。**

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 作用 |
| --- | --- |
| `deploy/operator/main.go` | Operator 进程入口：组装 scheme、起 Manager、注册 Reconciler、健康探针。 |
| `deploy/operator/api/v1alpha1/semanticrouter_types.go` | CRD 的 schema 源头——`SemanticRouterSpec`/`Status` 及全部子结构体，`+kubebuilder` 注解即 CRD 校验规则。 |
| `deploy/operator/controllers/semanticrouter_controller.go` | `SemanticRouterReconciler` 主体：`Reconcile` 顶层流程与 `SetupWithManager`（监视/属主声明）。 |
| `deploy/operator/controllers/semanticrouter_reconcile_flow.go` | Reconcile 的子流程：OpenShift 探测、CR 取值、finalizer、初始状态、`reconcileOwnedResources` 线性管线。 |
| `deploy/operator/controllers/semanticrouter_reconcile_resources.go` | 各类资源的「创建或更新」实现：Deployment/Service/HPA/Ingress/PVC/ServiceAccount 与状态/终结化。 |
| `deploy/operator/controllers/semanticrouter_config_data.go` | 把 canonical config 渲染成 ConfigMap（`config.yaml` + `tools_db.json`）。 |
| `deploy/operator/controllers/canonical_config_builder.go` | **配置翻译总入口**：从零搭一份 canonical config，先叠加后端发现，再叠加 CRD spec 覆盖。 |
| `deploy/operator/controllers/canonical_config_spec.go` | 把 `ConfigSpec` 各段（model_catalog/stores/integrations/services/routing）下沉到 canonical。 |
| `deploy/operator/controllers/canonical_config_backends.go` | 把后端发现结果写进 `providers.models[]` 与 `routing.modelCards`。 |
| `deploy/operator/controllers/backend_discovery.go` | `vllmEndpoints` 的三路后端发现：KServe / LlamaStack / Service。 |
| `deploy/operator/controllers/gateway_integration.go` | 判定 gateway 模式（standalone / gateway-integration）并尝试建 HTTPRoute。 |
| `deploy/operator/controllers/constants.go` | finalizer 名、条件类型、端口与默认值的集中常量。 |

> 小贴士：CRD 的 YAML（`config/crd/bases/vllm.ai_semanticrouters.yaml`）是由 `semanticrouter_types.go` 里的 Go 类型 + `+kubebuilder:` 注解**代码生成**出来的，不要手改那份 YAML。

## 4. 核心概念与源码讲解

### 4.1 CRD schema：SemanticRouter 的字段契约

#### 4.1.1 概念说明

`SemanticRouter` 是一个 namespaced 的自定义资源，`spec` 描述「我想要什么」，`status` 由控制器回写「现在实际怎样」。它的字段设计有一条贯穿主线：**尽量用 Kubernetes 原生概念表达部署意图（镜像、副本、存储、Service 后端），再用 `config` 段做路由器的运行时覆盖。** 这样平台工程师能像管普通工作负载一样管它，而算法/路由策略仍走路由器自己的 schema。

一个关键设计取舍：CRD **不**逐字段复制路由器的整个 v0.3 schema（那样 CRD 会永远滞后于路由器演进）。对于路由器独占、变化快的部分（`config.routing`、决策的 `algorithm`），CRD 用 `apiextensionsv1.JSON` + `PreserveUnknownFields` 做**透明透传**——CRD 只校验「它是个对象」，具体内容由路由器自己解释。这一点在 u3-l1 讲过的 routing 七段里尤为明显。

#### 4.1.2 核心流程

CRD schema 的「构造」流程其实是**编译期**的：

1. 你在 Go 类型上加 `+kubebuilder:default=...`、`+kubebuilder:validation:Enum=...`、`+kubebuilder:validation:Minimum=...` 等注解。
2. `make manifests`（底层是 `controller-gen`）扫描这些类型，生成 OpenAPI v3 schema，写入 CRD YAML。
3. `make install` 把 CRD YAML 应用进集群。
4. 之后用户提交的每个 `SemanticRouter` CR 都会被 kube-apiserver 按这份 schema 校验。

运行期则相反：控制器读 CR 的 `spec`，翻译成资源；读集群实际状态，写回 `status`。

#### 4.1.3 源码精读

先看顶层资源类型与它的 kubebuilder 元注解。`scope=Namespaced`、`shortName=sr`，并定义了 `kubectl get sr` 时显示的四列（Replicas/Ready/Phase/Age）：

[deploy/operator/api/v1alpha1/semanticrouter_types.go:1850-1865](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/api/v1alpha1/semanticrouter_types.go#L1850-L1865) — 顶层 `SemanticRouter` 类型声明：`Spec`/`Status` 双字段，`+kubebuilder:subresource:status` 表示 status 是独立子资源（写 status 不需要改 spec 的权限）。

`SemanticRouterSpec` 是用户写的主体，字段可粗分四类——部署（image/replicas/resources/persistence/probes/securityContext/nodeSelector…）、后端发现（`vllmEndpoints`）、运行时覆盖（`config`）、接入（`gateway`/`ingress`/`openshift`）：

[deploy/operator/api/v1alpha1/semanticrouter_types.go:30-137](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/api/v1alpha1/semanticrouter_types.go#L30-L137) — `SemanticRouterSpec` 全部字段。注意 `Config ConfigSpec`、`VLLMEndpoints []VLLMEndpointSpec`、`Gateway *GatewaySpec` 这三个是本讲后文的主角。

其中 `ConfigSpec` 是「路由器运行时覆盖」的集合入口。注意 `Routing` 字段是 `*apiextensionsv1.JSON` 且带 `PreserveUnknownFields`——这正是「CRD 不滞后」策略的落点：

[deploy/operator/api/v1alpha1/semanticrouter_types.go:261-320](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/api/v1alpha1/semanticrouter_types.go#L261-L320) — `ConfigSpec`：`Routing`（透传对象）、`EmbeddingModels`、`SemanticCache`、`Tools`、`PromptGuard`、`Classifier`、`ComplexityRules`、`Decisions`、`Strategy`、`Observability` 等。注释明确说 routing 「intentionally preserved as an object so the operator can pass through the router-owned … contract without lagging behind every router schema addition」。

`status` 则是控制器对世界的观察镜像，主要由 Deployment 的就绪情况驱动：

[deploy/operator/api/v1alpha1/semanticrouter_types.go:1807-1838](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/api/v1alpha1/semanticrouter_types.go#L1807-L1838) — `SemanticRouterStatus`：`Conditions`（标准 metav1.Condition 列表）、`ObservedGeneration`、`Replicas`/`ReadyReplicas`、`Phase`、`GatewayMode`、可选的 `OpenShiftFeatures`。

字段校验规则散落在各子结构体里，举两个有代表性的：缓存后端用枚举与正则双重约束；副本数与端口有数值边界。例如相似度阈值存成字符串再用正则约束成 `[0,1]`，是为了规避 CRD 里浮点数的精度问题（这条策略贯穿全 CRD，后文 4.3 会看到控制器如何把它转回真浮点）：

[deploy/operator/api/v1alpha1/semanticrouter_types.go:330-340](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/api/v1alpha1/semanticrouter_types.go#L330-L340) — `SemanticCacheConfig` 的 `BackendType` 枚举（memory/redis/valkey/milvus/qdrant/hybrid）与 `SimilarityThreshold` 的 `+kubebuilder:validation:Pattern`。

#### 4.1.4 代码实践

1. **实践目标**：把「Go 类型注解 → CRD 校验规则」这条生成链跑通，亲眼看到 schema 是代码生成的。
2. **操作步骤**：
   - 进入 `deploy/operator`，执行 `make manifests`（若未装 `controller-gen`，先按 Makefile 提示 `make controller-gen` 安装）。
   - 打开生成产物 `config/crd/bases/vllm.ai_semanticrouters.yaml`，搜索 `semantic_cache`、`backend_type`、`similarity_threshold`，对照上面读到的 Go 注解。
3. **需要观察的现象**：CRD YAML 的 `enum:` 与 `pattern:` 应当与 Go 源码里的 `+kubebuilder:validation:Enum` / `+kubebuilder:validation:Pattern` 一一对应。
4. **预期结果**：改一个注解（例如把 `MaxEntries` 的默认值注释改一下）再 `make manifests`，会看到 CRD YAML 里对应 `default:` 变化——证明 schema 是生成的，手改 YAML 会被覆盖。
5. 若本机无 `controller-gen`/集群：直接 `Read` 已提交的 `config/crd/bases/vllm.ai_semanticrouters.yaml` 做静态对照即可（**待本地验证**生成步骤）。

#### 4.1.5 小练习与答案

**练习 1**：`SemanticRouterSpec` 里哪个字段体现了「CRD 刻意不复制路由器全部 schema」？依据是什么？
**答案**：`Config.Routing`（`*apiextensionsv1.JSON`）。依据是它带 `+kubebuilder:pruning:PreserveUnknownFields` 与 `+kubebuilder:validation:Type=object`，注释明说这是为了让路由器自有的 signal/projection/decision 契约透传，而不必让 CRD 跟着每个新字段改。

**练习 2**：为什么 CRD 里很多「数值」（如相似度阈值、采样率）存成字符串而不是 float？
**答案**：规避 CRD/OpenAPI 的浮点精度与序列化问题；再配合 `+kubebuilder:validation:Pattern` 用正则限定取值范围，控制器在翻译阶段把它转回真正的数值类型（见 4.3.3）。

### 4.2 Reconcile 流程：从 CR 到一整套资源

#### 4.2.1 概念说明

`SemanticRouterReconciler` 是 Operator 的大脑。每当 `SemanticRouter` CR（或它 Owns 的 Deployment/Service/ConfigMap 等）发生变化，controller-runtime 就把一个 `ctrl.Request{NamespacedName}` 投递给 `Reconcile`。Reconcile 不问「谁变了」，只做一件事：**把当前世界拉回 spec 描述的期望状态**。

Reconciler 本身很瘦——只持有 `client.Client`（与 apiserver 交互）、`Scheme`（类型注册表，用于设置属主引用），外加一个 `sync.Once` 缓存的「是否跑在 OpenShift 上」探测结果：

[deploy/operator/controllers/semanticrouter_controller.go:36-43](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/semanticrouter_controller.go#L36-L43) — `Reconciler` 结构体定义。`isOpenShift` 用指针 + `sync.Once` 实现「探测一次、缓存永久」，避免每次调和都去 List Route。

#### 4.2.2 核心流程

顶层 `Reconcile` 是一条「守卫 + 线性管线」：

1. 探测平台（OpenShift 一次）。
2. 取 CR；不存在则直接返回（删除事件交给 finalizer 处理）。
3. 处理 finalizer（删除态走清理，常态确保 finalizer 存在）。
4. 若 `status` 全空，写入初始 `Progressing` 条件后 `Requeue`（让下一轮干净地往下走）。
5. `DeepCopy` 一份基线（供 status patch 比对）。
6. **`reconcileOwnedResources`：按固定顺序调和全部子资源**（本模块重点）。
7. `updateStatus`：读 Deployment 就绪情况，回写 `Phase`/`Conditions`，失败仅记日志不中断。

[deploy/operator/controllers/semanticrouter_controller.go:63-95](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/semanticrouter_controller.go#L63-L95) — `Reconcile` 顶层。注意 `updateStatus` 失败只记 error 不返回——状态更新失败不应阻断下一次调和。

`reconcileOwnedResources` 是真正的「资源装配流水线」，顺序严格，每一步失败即返回 error、停在原地等下次调和。顺序的设计逻辑是「被依赖者先建」：

[deploy/operator/controllers/semanticrouter_reconcile_flow.go:149-212](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/semanticrouter_reconcile_flow.go#L149-L212) — 顺序为：ServiceAccount → ConfigMap → PVC → Gateway 模式判定 → Envoy ConfigMap → Deployment → Service → HPA → Ingress → Route。ConfigMap 必须先于 Deployment（Pod 要挂载它），PVC 先于 Deployment（要挂模型缓存卷），Gateway 模式要先算出来才能生成 Envoy/Deployment/Service。

每个 `reconcileXxx` 都遵循同一个「Get → 若 NotFound 则 Create → 否则若 drift 则 Update（带冲突重试）」的幂等模板，并统一用 `controllerutil.SetControllerReference` 打上属主。以 Deployment 为例：

[deploy/operator/controllers/semanticrouter_reconcile_resources.go:111-136](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/semanticrouter_reconcile_resources.go#L111-L136) — `reconcileDeployment`：`reflect.DeepEqual` 比较 `found.Spec` 与期望 `deployment.Spec`，不一致则 `RetryOnConflict` 重试更新（应对并发调和冲突）。

状态机由 Deployment 副本数驱动，产出 `Pending`/`Progressing`/`Running` 三态：

[deploy/operator/controllers/semanticrouter_reconcile_resources.go:265-291](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/semanticrouter_reconcile_resources.go#L265-L291) — `ReadyReplicas==0` → Pending；`< Replicas` → Progressing；相等 → Running 并移除 Progressing 条件。`ObservedGeneration` 记录本次观察到的是哪一代 spec。

`SetupWithManager` 声明「监视 `SemanticRouter`，并 Owns 一堆子资源」——Owns 的含义是「子资源变化也触发本控制器的调和」：

[deploy/operator/controllers/semanticrouter_controller.go:98-109](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/semanticrouter_controller.go#L98-L109) — For(SemanticRouter) + Owns(Deployment/Service/SA/ConfigMap/PVC/HPA/Ingress)。注意这里**没有** Owns Envoy/Gateway/Route 的全部——OpenShift Route、Gateway API 对象的条件调和是平台相关的。

#### 4.2.3 源码精读（finalizer 与初始状态）

删除语义靠 finalizer 保证「先清理后消失」。删除时控制器会删掉它创建的 PVC（仅当不是用户自带 `ExistingClaim`）：

[deploy/operator/controllers/semanticrouter_reconcile_resources.go:296-333](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/semanticrouter_reconcile_resources.go#L296-L333) — `finalizeSemanticRouter` → `deleteOwnedPVCIfPresent`：PVC 名是 `<sr.Name>-models`。这避免了「CR 删了但模型缓存卷还赖着」的资源泄漏。

finalizer 名与条件类型常量集中在 constants.go：

[deploy/operator/controllers/constants.go:19-26](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/constants.go#L19-L26) — `SemanticRouterFinalizer = "semanticrouter.vllm.ai/finalizer"`，条件类型 `Available`/`Progressing`/`Degraded`。端口默认值在同一文件 40-42 行（gRPC 50051 / api 8080 / metrics 9190）。

#### 4.2.4 代码实践

1. **实践目标**：用跟踪法理解一次调和的完整调用链。
2. **操作步骤**：
   - 在 `semanticrouter_controller.go` 的 `Reconcile` 入口与 `semanticrouter_reconcile_flow.go` 的 `reconcileOwnedResources` 各设一个心理断点。
   - 对照 `deploy/operator/config/samples/vllm.ai_v1alpha1_semanticrouter_simple.yaml`（一个含 `vllmEndpoints`/`persistence`/`config` 的最小样例），逐行推断它被调和时，会按顺序生成哪些资源、各自的名字是什么（提示：多数资源名 = `sr.Name` 或 `sr.Name + "-config"`/`"-models"`）。
3. **需要观察的现象**：写出资源清单（ServiceAccount、ConfigMap `<name>-config`、PVC `<name>-models`、Envoy ConfigMap、Deployment `<name>`、Service `<name>`…）。
4. **预期结果**：你的清单应与 `reconcileOwnedResources` 的调用顺序一一对应；能指出 ConfigMap 与 PVC 先于 Deployment 生成的原因。
5. **待本地验证**：在真集群 `kubectl apply -f config/samples/...simple.yaml` 后 `kubectl get sr,deploy,svc,configmap,pvc` 观察实际产物。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `reconcileOwnedResources` 要把 ConfigMap 排在 Deployment 之前？
**答案**：因为 Deployment 的 Pod 模板会以卷形式挂载该 ConfigMap（`config.yaml`）。若先建 Deployment，Pod 会因找不到 ConfigMap 而启动失败；先建依赖、再建消费者是调和管线的基本排序原则。

**练习 2**：`updateStatus` 失败时 `Reconcile` 为什么不返回它的 error？
**答案**：状态写入失败属于「非致命」——核心资源已经调和成功，状态只是对外汇报。返回 error 会触发立即重排（requeue），而状态 patch 失败往往只是并发冲突，下一次调和自然会修正。所以仅记日志、本次返回 `nil`，让节奏由后续事件驱动。

### 4.3 配置翻译：从 CRD spec 到 canonical config（核心）

#### 4.3.1 概念说明

这是整个 Operator**真正**在做的事——把用户写的 CR 翻译成路由器内核认识的 canonical v0.3 `config.yaml`。精髓在于：**Operator 不维护一套平行 schema，而是直接构造路由器自己的 `routerconfig.CanonicalConfig` 结构体。** 看 import 就明白：

```go
import routerconfig "github.com/vllm-project/semantic-router/src/semantic-router/pkg/config"
```

Operator 直接复用路由器 `pkg/config` 包里的类型（`CanonicalConfig`、`CanonicalRouting`、`CanonicalProviderModel` 等，即 u3-l1 讲过的那些 canonical 类型）。这样「翻译」就是「把 CRD 的字段搬进路由器的类型」，零 schema 漂移。

翻译分**两步叠加**，顺序很关键：

1. **先 seed 默认骨架**：建一个最小可用的 canonical（version=v0.3、一个 gRPC listener、一个 `general` 域、空的 providers/global）。
2. **`applyDiscoveredBackends`（4.4 详述）**：把集群里发现的后端写进 `providers.models[]` 与 `routing.modelCards`，第一个模型设为 `default_model`。
3. **`applyOperatorConfigSpec`**：把 CRD `spec.config` 各段覆盖/补进 canonical（model_catalog、stores、integrations、services、routing、strategy）。

「发现先于覆盖」的意义：发现负责「有哪些模型可选」（基础设施事实），覆盖负责「路由策略与模型目录细节」（用户意图），互不踩踏。

#### 4.3.2 核心流程

翻译入口 `buildCanonicalConfig` 很短，正是「骨架 + 两步叠加」：

[deploy/operator/controllers/canonical_config_builder.go:10-58](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/canonical_config_builder.go#L10-L58) — `buildCanonicalConfig`：第 11-48 行手搓骨架（注意 `Routing.Signals.Domains` 预置了一个 `general` 域，避免空配置）；第 50 行 `applyDiscoveredBackends`；第 53 行 `applyOperatorConfigSpec`。

最终这份 `CanonicalConfig` 在 `generateConfigYAML` 里被 `yaml.Marshal` 成字符串，塞进 ConfigMap 的 `config.yaml` 键：

[deploy/operator/controllers/semanticrouter_config_data.go:91-103](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/semanticrouter_config_data.go#L91-L103) — `generateConfigYAML` = `buildCanonicalConfig` + `yaml.Marshal`。而 `reconcileConfigMap`（同文件 36-89 行）则把 `config.yaml` 和 `tools_db.json` 一起写进 `<sr.Name>-config` 这个 ConfigMap，同样遵循 Get/Create/Update 幂等模板。

`applyOperatorConfigSpec` 是把 `ConfigSpec` 分门别类下沉的调度器：

[deploy/operator/controllers/canonical_config_spec.go:12-32](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/canonical_config_spec.go#L12-L32) — 依次调 `applyOperatorModelCatalog` / `applyOperatorStoresAndIntegrations` / `applyOperatorProviderDefaults` / `applyOperatorServices` / `applyOperatorRouting`，最后若 `spec.Strategy` 非空则写 `Global.Router.Strategy`。

#### 4.3.3 源码精读（两个翻译热点）

**热点一：routing 透传。** `applyOperatorRouting` 处理 `spec.config.routing`（那个 `apiextensionsv1.JSON`）。它不是字段级搬运，而是整体反序列化成 `CanonicalRouting`，并**按「出现了哪些顶层键」做字段级覆盖**——只覆盖用户写了的那一段（modelCards/signals/projections/decisions），没写的不动：

[deploy/operator/controllers/canonical_config_spec.go:107-130](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/canonical_config_spec.go#L107-L130) — `applyOperatorRouting`：调 `canonicalRoutingFromKubernetesJSON` 解析 + 记录 fields，再 `applyCanonicalRoutingOverrides` 选择性覆盖；`ComplexityRules` 与 `Decisions` 则走强类型转换单独处理。

覆盖的「按键选择性」实现在 routing overrides 文件里——先扫 JSON 顶层键打标记，再按标记覆盖，从而让用户能「只覆盖 decisions，保留别处发现的 signals」：

[deploy/operator/controllers/canonical_routing_overrides.go:60-77](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/canonical_routing_overrides.go#L60-L77) — `applyCanonicalRoutingOverrides`：`fields.modelCards/signals/projections/decisions` 各自为真才覆盖对应字段。

**热点二：泛型转换 + 字符串强转数值。** 大量 CRD 子段（SemanticCache/Tools/Decisions…）用泛型 `convertToTypedConfig[T]` 转换。它先 `convertToConfigMap`（JSON 标签 → map），再在归一化阶段把「数字字符串」变回真数值，最后 Marshal/Unmarshal 成目标类型 `T`：

[deploy/operator/controllers/canonical_config_spec.go:201-214](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/canonical_config_spec.go#L201-L214) — `convertToTypedConfig[T]`：`convertToConfigMap(value)` → `yaml.Marshal` → `yaml.Unmarshal` 到 `T`。这种 JSON→YAML 往返是为了「用 json tag 命名 + 强转类型」服务。

字符串→数值的强转逻辑在 helpers 里，解决了「CRD 存字符串、路由器要真数值」的鸿沟：

[deploy/operator/controllers/helpers.go:90-116](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/helpers.go#L90-L116) — `convertStringValue`：依次尝试 int → float → bool，都不行才留字符串。例如 `"0.85"`→`0.85`，`"1000"`→`1000`。配合 `normalizeConfigValue` 递归处理 map/slice。

> 这条链路与 u3-l1 的 canonical 五段式严格对应：`providers`（model_catalog 里 embedding/system/modules 属 global）、`routing`、`global.services/stores/integrations`。Operator 的任务就是把 CRD 的零散字段精确归位到这五段里。

#### 4.3.4 代码实践

1. **实践目标**：亲手把 CRD spec 翻译成 canonical config 的过程「演算」一遍。
2. **操作步骤**：
   - 读 `deploy/operator/config/samples/vllm.ai_v1alpha1_semanticrouter_simple.yaml` 的 `spec.config` 段（semantic_cache / prompt_guard / tools）。
   - 对照 `applyOperatorConfigSpec` 的五个子步骤，预测每个字段最终落到 canonical 的哪一段（例如 `semantic_cache` → `global.stores.semantic_cache`，`prompt_guard` → `global.model_catalog.modules.prompt_guard`，`tools` → `global.integrations.tools`）。
   - 运行 `cd deploy/operator && go test ./controllers/...`，重点看 `canonical_config_spec_test.go` 是否通过——它正是这条翻译链的回归测试。
3. **需要观察的现象**：测试输出应全绿；若你修改 `convertStringValue` 让它不再转 float，相关用例应失败。
4. **预期结果**：能画出一张「CRD 字段 → canonical 段落」映射表。
5. **待本地验证**：`go test` 步骤。

#### 4.3.5 小练习与答案

**练习 1**：`convertToTypedConfig` 为什么走「JSON Marshal → YAML Unmarshal」的往返，而不是直接类型断言？
**答案**：因为 CRD 结构体用的是 `json` tag（snake_case），且字段多为字符串（规避精度问题）；路由器目标类型用的是 yaml。先 `json.Marshal` 得到符合 json tag 命名的中间表示，`convertToConfigMap` 顺带把数字字符串强转为真数值，再 `yaml.Unmarshal` 到目标类型，既统一了命名又修正了类型。

**练习 2**：如果用户在 `spec.config.routing` 里只写了 `decisions`，会不会把发现阶段写入的 `modelCards` 冲掉？
**答案**：不会。`applyCanonicalRoutingOverrides` 是「按键选择性覆盖」——它先扫描 JSON 顶层键打标记（`fields.decisions=true` 而 `modelCards=false`），只覆盖标记为真的字段。所以 `decisions` 被覆盖，`modelCards` 保留发现阶段的结果。

### 4.4 后端发现：vllmEndpoints 的 Kubernetes 原生适配

#### 4.4.1 概念说明

u3-l1 讲过，路由器的 `providers.models[].backend_refs` 描述「逻辑模型 → 多个后端端点 + 权重」。在纯 YAML 部署里，这些端点是手写的。Operator 提供了一条更 Kubernetes 原生的路：`spec.vllmEndpoints[]`。你只需声明「我有一个叫 llama3-8b 的模型，它在集群里由某个 InferenceService / 一组带 label 的 Service / 一个具体 Service 提供」，Operator 在调和时**实地去集群里查**，把查到的地址翻译成 `backend_refs` 与 `routing.modelCards`。

这是「基础设施即事实来源」的体现：端点地址不该硬编码进路由配置，而应由集群现状决定。三种后端类型对应三种发现策略：

- `kserve`：查 KServe `InferenceService`，推出 `<name>-predictor.<ns>.svc.cluster.local:8443`。
- `llamastack`：按 `discoveryLabels` 用 label selector `List` Service，取第一个的地址与端口。
- `service`：直接给 `name/namespace/port`，拼成 `<name>.<ns>.svc.cluster.local:<port>`。

#### 4.4.2 核心流程

发现管线在 `applyDiscoveredBackends` 里串起「发现 → 排序 → 写入」：

1. 若 `vllmEndpoints` 为空，直接返回（跳过发现）。
2. `discoverVLLMBackends` 遍历每个 endpoint，按 `backend.type` 分派到三路发现函数，把结果聚成 `map[模型名]DiscoveredProviderModel`（同名 endpoint 的多个后端会被合并成多条 `backend_refs`，权重也累加）。
3. 对模型名排序（确定性输出），逐个写：`routing.modelCards`（含 LoRA 适配器）+ `providers.models[]`（含 reasoning_family 与 backend_refs）。
4. **第一个模型被设为 `providers.defaults.default_model`**——这决定了 u5-l1 里 `auto` 别名最终落到哪个具体模型。

[deploy/operator/controllers/canonical_config_backends.go:12-51](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/canonical_config_backends.go#L12-L51) — `applyDiscoveredBackends`：注意 `sort.Strings(modelNames)` 保证输出稳定；第 45-47 行 `index == 0` 时设默认模型。

#### 4.4.3 源码精读

三路分派在 `discoverBackendEndpoint` 的 `switch` 里：

[deploy/operator/controllers/backend_discovery.go:175-217](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/backend_discovery.go#L175-L217) — 按 `backend.type` 分派；统一在末尾设 `endpoint.Name` 与 `Weight`（缺省权重 1）。

KServe 发现刻意用 `unstructured.Unstructured` 而非 KServe 的 Go 客户端，避免对 KServe 版本的硬依赖：

[deploy/operator/controllers/backend_discovery.go:54-105](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/backend_discovery.go#L54-L105) — `discoverKServeBackend`：用 GVR `serving.kserve.io/v1beta1/inferenceservices` 取对象， predictor 服务名约定为 `<name>-predictor`，端口默认 8443/https。

聚合逻辑把同名模型的多个 endpoint 合并，LoRA 适配器按名去重合并：

[deploy/operator/controllers/backend_discovery.go:231-263](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/backend_discovery.go#L231-L263) — `discoverVLLMBackends`：单个 endpoint 发现失败只记日志并 `continue`（不拖垮整体），最终若一个都没发现则返回 nil。

CRD 侧的 schema 把这套契约表达得很清楚——`model` 是逻辑模型名、`backend.type` 三选一、`weight` 控制负载均衡：

[deploy/operator/api/v1alpha1/semanticrouter_types.go:1675-1733](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/api/v1alpha1/semanticrouter_types.go#L1675-L1733) — `VLLMEndpointSpec` + `VLLMBackend`（kserve/llamastack/service 三态，后两者分别用 `DiscoveryLabels` / `Service`）。

#### 4.4.4 代码实践

1. **实践目标**：理解「CR 里的 vllmEndpoints → config 里的 backend_refs」这条映射，并体会合并/默认模型语义。
2. **操作步骤**：
   - 读 `deploy/operator/config/samples/vllm.ai_v1alpha1_semanticrouter_complexity.yaml` 的两个 `vllmEndpoints`（`llama3-8b` 权重 2、`llama3-70b` 权重 1）。
   - 推演：调和后 canonical 的 `providers.models[]` 会有几条、各自 `backend_refs` 长什么样？哪个会成为 `default_model`？（提示：按模型名升序，`llama3-70b` < `llama3-8b`，所以排第一的是 `llama3-70b`。）
3. **需要观察的现象**：写出预测的 `providers.models[]` 片段与 `default_model`。
4. **预期结果**：两条 model，各自一条 backend_ref（来自 kserve 推断的 predictor 地址），`default_model = llama3-70b`（字典序第一）。可在 `controllers` 包的测试里找到覆盖此逻辑的用例佐证。
5. **待本地验证**：实际集群里 KServe InferenceService 存在时才会推出真实地址。

#### 4.4.5 小练习与答案

**练习 1**：三个 endpoint 声明同一个 `model: llama3-8b`，但分别指向不同 Service，调和后 `providers.models[]` 里会有几条 llama3-8b？
**答案**：一条。`discoverVLLMBackends` 以 `model` 名为 key 聚合，同名的多个 endpoint 合并成**一个** `CanonicalProviderModel`，含**多条** `backend_refs`（权重各自累加）。这正是 u3-l1 讲的「逻辑模型名 → 多后端 backend_refs 加权」的来源。

**练习 2**：为什么单个 endpoint 发现失败时只 `continue` 而不让整个调和失败？
**答案**：后端发现是「尽力而为」——某个后端暂时不可达不应阻断整个路由器上线。已发现成功的模型仍能服务；失败的那个会在下一次调和（或集群事件）时重试。这降低了 Operator 与底层平台耦合的脆弱性。

### 4.5 Envoy / Gateway 集成：standalone 与 gateway-integration

#### 4.5.1 概念说明

回顾 u4-l3 与 u12-l2：路由器是 Envoy 的 ExtProc 后端，Envoy 负责搬流量、router 负责做决策。那么「谁部署 Envoy、Envoy 怎么找到 router」就有两种形态。Operator 用一个 `gatewayMode` 字符串把这两条路分开，并把它贯穿到 Deployment/Service 的生成里：

- **standalone（默认）**：用户不配 `spec.gateway`。Operator 渲染一份内置的 Envoy 静态配置（`standaloneEnvoyConfigYAML`），由它把 8801 入口流量经 ExtProc（`extproc_service` 集群）导向路由器，再用 dynamic forward proxy 转发到上游模型。
- **gateway-integration**：用户在 `spec.gateway.existingRef` 指一个**已存在**的 Gateway API `Gateway`。Operator 校验它存在，并尝试建一条 HTTPRoute 把流量接向 router Service。这种形态下流量入口由外部 Gateway 管理。

#### 4.5.2 核心流程

模式判定在调和管线里、Envoy 配置之前完成，结果存进 `sr.Status.GatewayMode`：

[deploy/operator/controllers/gateway_integration.go:33-62](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/gateway_integration.go#L33-L62) — `reconcileGatewayIntegration`：无 `existingRef` → `standalone`；有则 Get Gateway，找不到即报错，找到则尝试 `createHTTPRoute` 并返回 `gateway-integration`。

随后 `reconcileEnvoyConfig`、`reconcileDeployment`、`reconcileService` 都接收 `gatewayMode` 参数，据此决定渲染内容（standalone 模式才会下发那份 Envoy 静态配置）。Envoy 配置里关键的一段是 ExtProc 过滤器：`request_body_mode: BUFFERED`、`response_body_mode: BUFFERED`，对应 u4-l3 讲的 ExtProc 四阶段交互：

[deploy/operator/controllers/semanticrouter_envoy.go:71-87](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/semanticrouter_envoy.go#L71-L87) — `envoy.filters.http.ext_proc` 配置：`cluster_name: extproc_service` 指向 router，`failure_mode_allow: true` 表示 router 不可用时 Envoy 仍放行（避免单点拖垮）。

#### 4.5.3 源码精读（一个诚实的注意事项）

`createHTTPRoute` 目前仍是**占位实现**——它只记日志并返回 nil，真正的 HTTPRoute 构造被注释在函数体内，原因是 Gateway API v1/v1beta1 字段差异较大：

[deploy/operator/controllers/gateway_integration.go:65-75](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/operator/controllers/gateway_integration.go#L65-L75) — 占位与 TODO。这是真实源码的现状，**不要**假设 Operator 当前会真正创建 HTTPRoute——它只校验 Gateway 存在并设定模式。计划中的实现（注释里）会建三条 rule：chat completions（60s）、classify API（30s）、health（5s）各自带不同超时。

> 这正好示范了「读源码要诚实」：注释里的代码不是现行行为。这一现状也呼应 u12-l2 里 gateway 远程调用模型的对接细节需要用户自行补全。

#### 4.5.4 代码实践

1. **实践目标**：分清两种模式各自由谁负责 Envoy、流量怎么进 router。
2. **操作步骤**：对照读 `gateway_integration.go` 与 `semanticrouter_envoy.go`（standalone 段），画出两种模式下「客户端 → ? → router(ExtProc) → 上游模型」的拓扑。
3. **需要观察的现象**：standalone 下 Envoy 配置由 Operator 渲染、ExtProc 集群名是 `extproc_service`；gateway-integration 下入口是外部 Gateway，HTTPRoute 当前并不真正下发。
4. **预期结果**：能指出 `gatewayMode` 如何影响后续 `reconcileDeployment`/`reconcileService` 的渲染分支。
5. **待本地验证**。

#### 4.5.5 小练习与答案

**练习**：`reconcileGatewayIntegration` 在 `spec.gateway.existingRef` 指向的 Gateway 不存在时，行为是什么？
**答案**：返回 error（`gateway %s/%s not found`），从而让 `reconcileOwnedResources` 在 Gateway 这一步停下、本次调和失败、等下次重试。它不会静默降级到 standalone——因为用户明确表达了「要用这个 Gateway」的意图，意图落空应当显式失败而非偷偷换模式。

## 5. 综合实践

把四个最小模块串起来，完成一次「CR → 全部资源 + canonical config」的推演与验证。

**任务背景**：你要向团队解释，提交下面这份最小 CR 后，Operator 会做什么。

```yaml
# 示例代码（节选自 config/samples/vllm.ai_v1alpha1_semanticrouter_simple.yaml 的核心）
apiVersion: vllm.ai/v1alpha1
kind: SemanticRouter
metadata: { name: semantic-router-simple, namespace: default }
spec:
  replicas: 1
  vllmEndpoints:
    - name: my-llama-model
      model: llama3-8b
      reasoningFamily: qwen3
      backend:
        type: kserve
        inferenceServiceName: llama-3-8b
  persistence: { enabled: true, size: 10Gi }
  config:
    semantic_cache: { enabled: true, similarity_threshold: "0.85", max_entries: 1000, ttl_seconds: 3600 }
    prompt_guard: { enabled: true, threshold: "0.7" }
    tools: { enabled: true, top_k: 3 }
```

**步骤**：

1. **CRD 层（4.1）**：列出这份 CR 用到的 spec 字段；指出 `similarity_threshold` 为何写成字符串。
2. **Reconcile 层（4.2）**：按 `reconcileOwnedResources` 的顺序，写出会生成的资源清单与名字（SA `semantic-router-simple`、ConfigMap `semantic-router-simple-config`、PVC `semantic-router-simple-models`、Envoy ConfigMap、Deployment、Service…）。
3. **后端发现层（4.4）**：推演 `vllmEndpoints` 经 KServe 发现后，canonical 的 `providers.models[]` 与 `default_model` 长什么样（地址形如 `llama-3-8b-predictor.default.svc.cluster.local:8443`）。
4. **配置翻译层（4.3）**：把 `config` 三段映射到 canonical——`semantic_cache → global.stores.semantic_cache`、`prompt_guard → global.model_catalog.modules.prompt_guard`、`tools → global.integrations.tools`；并说明 `"0.85"` 如何变成真浮点 `0.85`。
5. **验证**：`cd deploy/operator && go test ./controllers/...` 全绿；再 `make manifests` 看 CRD 是否与类型一致。

**预期产出**：一张「CR 字段 → 生成的 K8s 资源 / canonical 段落」对照表，以及一份手推的 `config.yaml` 片段。这等价于你在脑子里跑了一遍 Operator——这正是本讲的目标。

## 6. 本讲小结

- `SemanticRouter` CRD 用 **Kubernetes 原生字段**（image/replicas/persistence/vllmEndpoints）表达部署意图，用 `config` 段做路由器运行时覆盖；变化快的 routing/algorithm 用 `apiextensionsv1.JSON` + `PreserveUnknownFields` 透传，使 CRD 不滞后于路由器 schema。
- Reconcile 是一条**幂等的线性管线**：取 CR → finalizer → 初始状态 → `reconcileOwnedResources`（SA→ConfigMap→PVC→Gateway→Envoy→Deployment→Service→HPA→Ingress→Route）→ `updateStatus`；被依赖者先建，状态由 Deployment 就绪数驱动出 Pending/Progressing/Running。
- Operator 的真正核心是**配置翻译**：它直接复用路由器 `pkg/config` 的 `CanonicalConfig` 类型，按「骨架 → 后端发现 → spec 覆盖」叠加，最终 `yaml.Marshal` 成 ConfigMap 里的 `config.yaml`——与 u3-l1 的 canonical 五段式严格对应。
- `vllmEndpoints` 是 **K8s 原生后端发现**适配器：kserve/llamastack/service 三路把集群里活着的后端探测成 `providers.models[].backend_refs` + `routing.modelCards`，第一个模型成为 `default_model`；这是「基础设施即事实来源」的落地。
- 翻译链路用泛型 `convertToTypedConfig` + JSON→YAML 往返 + 字符串强转数值，解决「CRD 存字符串、路由器要真数值」的鸿沟；routing 覆盖是「按键选择性」，不会误伤发现阶段的结果。
- Envoy/Gateway 有 **standalone**（Operator 渲染内置 Envoy 静态配置，ExtProc 集群 `extproc_service`）与 **gateway-integration**（复用外部 Gateway，HTTPRoute 当前为占位实现）两态，由 `gatewayMode` 贯穿资源渲染。

## 7. 下一步学习建议

- **回到数据面**：本讲讲的是控制面如何「生成 config.yaml」。接下来建议读 u11-l1（API Server 管理 API）——那里讲同一份 config 被 apiserver 的 ETag 三态模型部署并热同步到运行时，与本讲的「写 ConfigMap」首尾相接。
- **横向对照部署形态**：把本讲的 Operator 与 u12-l2 的 Helm chart 并排看——Helm 只渲染单容器 ExtProc、不部署 Envoy；Operator 则多出后端发现、PVC 生命周期、Gateway/Route 集成。理解「何时用 Helm、何时用 Operator」的取舍。
- **深入 CRD 校验与 Webhook**：`deploy/operator/api/v1alpha1/semanticrouter_webhook.go` 提供了准入校验（本讲未展开），可作为进阶练习，理解「schema 校验（CRD）vs 语义校验（webhook）」的分工。
- **后续单元**：u13 进入管理面板（dashboard），你会看到面板如何反过来编辑这份 config；u14 讲 E2E，其中部分 profile 会真起一套 Operator 栈做端到端断言。
