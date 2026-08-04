# Admission Webhook 与边车注入

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 Kubernetes 准入 webhook（Admission Webhook）在控制器体系里的位置，区分「变更型（mutating）」和「校验型（validating）」两种 webhook 的职责与触发时机。
- 读懂 AIBrix 用 `controller-runtime` 的 `CustomDefaulter` / `CustomValidator` 接口注册 webhook 的统一模式，并能对照 kubebuilder 标记找到对应的 `MutatingWebhookConfiguration` / `ValidatingWebhookConfiguration`。
- 完整描述运行时边车（`aibrix-runtime`）是如何被注入到推理 Pod 的，并列出注入过程修改了 Pod 的哪些字段。
- 看懂 PodAutoscaler、ModelAdapter、KVCache、StormService 四类 CR 的默认值设置与校验逻辑。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**准入控制是什么。** Kubernetes API Server 处理一个写请求（CREATE / UPDATE / DELETE）时，会依次走：认证（你是谁）→ 授权（你能不能做）→ **准入控制（这个对象本身合不合法、要不要改一改）** → 持久化到 etcd。准入控制里的「动态准入」就是把对象在持久化前交给一个 HTTP 回调（webhook），由回调决定是否放行或修改。这就是 Admission Webhook。

**两种 webhook 的分工。**

| 类型 | 能否修改对象 | 典型用途 | 在 AIBrix 里的对应接口 |
| --- | --- | --- | --- |
| Mutating（变更） | 能 | 填默认值、注入容器 | `webhook.CustomDefaulter` 的 `Default()` |
| Validating（校验） | 不能，只能放行/拒绝 | 校验字段合法性 | `webhook.CustomValidator` 的 `ValidateCreate/Update/Delete()` |

API Server 先调用所有 mutating webhook，再调用 validating webhook。所以「先改、后校验」是一条铁律——你不能在校验阶段再去改对象。

**Webhook 与控制器的关系。** 二者都跑在同一个 `cmd/controllers` 二进制里、由同一个 Manager 托管，但职责不同：控制器（Reconcile）是「对象已经入库后，持续观察并调整集群状态向期望态收敛」的后台循环；webhook 是「对象入库前那一瞬间的一次性拦截」。AIBrix 的 webhook 位于 `pkg/webhook/`，本讲只讲准入 webhook，不涉及 Reconcile。

> 阅读提示：本讲的边车注入逻辑分散在两个文件里——`sidecar_injection.go` 只放共享常量与工具函数（构造容器、推断引擎类型），**真正拦截 Deployment 并执行注入的 webhook 处理器在 `deployment_webhook.go`**。读源码时务必把两者配在一起看。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pkg/webhook/deployment_webhook.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/deployment_webhook.go) | 拦截原生 `Deployment`，按注解决定是否注入 `aibrix-runtime` 边车（边车注入的真正入口）。 |
| [pkg/webhook/sidecar_injection.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/sidecar_injection.go) | 边车注入的共享常量与工具：构造边车容器 `buildRuntimeSidecarContainer`、按镜像名推断引擎类型 `inferEngineType`、判断容器是否已存在 `containsContainer`。 |
| [pkg/webhook/podautoscaler_webhook.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/podautoscaler_webhook.go) | PodAutoscaler 的默认值（Defaulter）与校验（Validator），校验规则最丰富，是「默认值与校验」模块的主样例。 |
| [pkg/webhook/modeladapter_webhook.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/modeladapter_webhook.go) | ModelAdapter 的校验：检查 `artifactURL` 协议与副本数。 |
| [pkg/webhook/kvcache_webhook.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/kvcache_webhook.go) | KVCache 的默认值（补 backend/mode）与校验（后端必须合法）。 |
| [pkg/webhook/stormservice_webhook.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/stormservice_webhook.go) | StormService 的边车注入（注入到每个 Role 的 Pod 模板）与名称长度校验。 |
| [cmd/controllers/main.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/controllers/main.go) | webhook Server 的创建、证书就绪等待、各 webhook 的注册顺序。 |
| [config/webhook/manifests.yaml](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/config/webhook/manifests.yaml) | 由 kubebuilder 标记生成的 `MutatingWebhookConfiguration` / `ValidatingWebhookConfiguration` 清单。 |

## 4. 核心概念与源码讲解

### 4.1 Webhook 类型与触发时机

#### 4.1.1 概念说明

AIBrix 把「准入 webhook」当作控制平面的**前置守卫与装修工**：在用户提交的 CR（或原生 Deployment）进入 etcd 之前，先做两件事——

1. **变更（mutating）**：给对象补默认值、注入边车容器。这一类 webhook 对应 `controller-runtime` 的 [`webhook.CustomDefaulter`](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/podautoscaler_webhook.go#L66-L77) 接口，只需实现 `Default(ctx, obj)`。
2. **校验（validating）**：检查对象是否合法，不合法就返回错误、拒绝这次写请求。对应 [`webhook.CustomValidator`](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/podautoscaler_webhook.go#L92-L123) 接口，需实现 `ValidateCreate / ValidateUpdate / ValidateDelete`。

「触发时机」由两样东西决定：

- **verbs**：kubebuilder 标记里写的 `verbs=create;update`，表示这个 webhook 只在创建和更新时触发（AIBrix 的所有 webhook 都是这两类，删除默认不拦截）。
- **failurePolicy**：标记里写的 `failurePolicy=ignore`（YAML 里渲染成 `Ignore`）。当 webhook 服务本身不可达时，`Ignore` 表示「放行请求」（fail-open），`Fail` 表示「拒绝请求」（fail-close）。AIBrix 选 `Ignore`，目的是即使 webhook 还没起来，也不至于把整个集群的创建/更新都卡死。

#### 4.1.2 核心流程

从启动到一次 webhook 调用的完整链路：

```
main.go 启动
  ├─ 解析 --disable-webhook flag（默认 false）
  ├─ 若启用 webhook：webhook.NewServer(...) 建一个 TLS webhook Server
  ├─ ctrl.NewManager(...) 把 WebhookServer 挂进 Manager
  ├─ cert.CertsManager(...) 异步签发 webhook 所需的 TLS 证书，就绪后关闭 certsReady channel
  └─ go setupControllers(...)  ← goroutine 阻塞等待 certsReady

setupControllers(mgr, ...)
  ├─ <-certsReady  （等证书就绪，webhook 是 HTTPS，没证书起不来）
  ├─ SetupModelAdapterWebhook(mgr)
  ├─ SetupKVCacheWebhookWithManager(mgr)
  ├─ SetupStormServiceWebhookWithManager(mgr)
  ├─ SetupDeploymentWebhookWithManager(mgr)
  └─ SetupPodAutoscalerWebhookWithManager(mgr)
       └─ 每个 Setup 内部：NewWebhookManagedBy(mgr).For(T).WithDefaulter(...).WithValidator(...).Complete()

运行期：
API Server 收到匹配的 CREATE/UPDATE
  → 经 Service webhook-service 路由到 operator Pod 的 webhook Server
  → Server 按 URL path 分发（path 来自 kubebuilder 标记）
  → 先跑 mutating（Default），再跑 validating（Validate*）
  → 通过则持久化，否则返回 422 给 kubectl/客户端
```

#### 4.1.3 源码精读

**(1) webhook Server 的条件化创建与证书等待。** 只有未禁用 webhook 时才建 Server，并把它交给 Manager 托管；证书未就绪时 `readyz` 探针会返回失败，保证「证书没好之前不算就绪」：

[cmd/controllers/main.go:221-226](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/controllers/main.go#L221-L226) —— 未禁用时才 `webhook.NewServer`。

[cmd/controllers/main.go:299-313](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/controllers/main.go#L299-L313) —— `readyz` 探针在 `certsReady` 关闭后才检查 webhook Server 是否已启动，证书未就绪直接返回错误。

**(2) 五个 webhook 的注册顺序，全部在证书就绪之后。** `setupControllers` 先 `<-certsReady` 阻塞，再依次注册五个 webhook：

[cmd/controllers/main.go:326-352](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/controllers/main.go#L326-L352) —— ModelAdapter / KVCache / StormService / Deployment / PodAutoscaler 五个 Setup 调用，任一失败即 `os.Exit(1)`。注意这与 u2-l1 讲过的「控制器两阶段注册」呼应：控制器在建 Manager 前就能 `Initialize`，而 webhook 必须等证书。

**(3) 统一的注册模板：Defaulter + Validator 同时挂到一个类型上。** 以 PodAutoscaler 为例：

[pkg/webhook/podautoscaler_webhook.go:47-52](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/podautoscaler_webhook.go#L47-L52) —— `For(&PodAutoscaler{})` 声明这个 webhook 服务于哪种类型，`WithDefaulter` 配 mutating，`WithValidator` 配 validating，`Complete()` 真正注册。五个 webhook 全是这套写法。

**(4) kubebuilder 标记是 webhook 配置的单一数据源。** 看这两行标记，它们会被 `controller-gen` 翻译成 `config/webhook/manifests.yaml` 里的条目：

[pkg/webhook/podautoscaler_webhook.go:56](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/podautoscaler_webhook.go#L56) —— mutating 标记：`path=/mutate-autoscaling-aibrix-ai-v1alpha1-podautoscaler,mutating=true,failurePolicy=ignore,verbs=create;update`。

[pkg/webhook/podautoscaler_webhook.go:82](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/podautoscaler_webhook.go#L82) —— validating 标记：`path=/validate-...`。

对照生成的清单 [config/webhook/manifests.yaml:67-86](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/config/webhook/manifests.yaml#L67-L86)，可以看到 `path`、`failurePolicy: Ignore`、`operations: [CREATE, UPDATE]`、`resources: podautoscalers` 一一对应。这就是「改标记 → make manifests → 清单自动更新」的闭环（回顾 u1-l3 讲过的代码生成机制）。

#### 4.1.4 代码实践

**实践目标：** 验证「kubebuilder 标记 → manifests.yaml → API Server 路由」三者的一致性。

**操作步骤：**

1. 在 [pkg/webhook/deployment_webhook.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/deployment_webhook.go) 找到 `+kubebuilder:webhook:path=/mutate-apps-v1-deployment...` 标记（第 45 行），记下它的 `path`、`mutating`、`failurePolicy`、`verbs`、`name`。
2. 打开 [config/webhook/manifests.yaml](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/config/webhook/manifests.yaml)，在 `MutatingWebhookConfiguration` 里找到 `name: medeployment.aibrix.ai` 的条目（第 7-26 行）。
3. 逐字段对照标记与 YAML。

**需要观察的现象：** 标记里的 `path` 与 YAML 里的 `clientConfig.service.path` 完全一致；`failurePolicy=ignore` 对应 YAML 的 `failurePolicy: Ignore`；`verbs=create;update` 对应 `operations: [CREATE, UPDATE]`。

**预期结果：** 两边一一对应，证明 manifests.yaml 确实由标记生成，不是手写的。如果想进一步确认，可以本地运行 `make manifests`（回顾 u1-l3），再 `git diff config/webhook/manifests.yaml`，应当没有变化。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 AIBrix 把所有 webhook 的 `failurePolicy` 都设成 `Ignore` 而不是 `Fail`？

> **参考答案：** `Fail` 会在 webhook 不可达时拒绝所有匹配的 CREATE/UPDATE。AIBrix 的 webhook 和 operator 跑在同一个进程里，operator 重启、升级或证书轮换期间，`Fail` 会导致用户连普通的 `kubectl apply` 都做不了，把集群写路径「绑架」。`Ignore`（fail-open）保证 webhook 暂不可用时集群仍可用，代价是这一小段时间跳过默认值/校验/边车注入——对辅助性功能是可接受的取舍。

**练习 2：** 给一个已存在的 CRD 类型新增 webhook，至少要改哪几处？

> **参考答案：** (a) 在 `pkg/webhook/` 写 `SetupXxxWebhookWithManager`，用 `NewWebhookManagedBy(mgr).For(&T{}).WithDefaulter(...).WithValidator(...).Complete()`；(b) 在类型上方加 `+kubebuilder:webhook:...` 标记（mutating 一条、validating 一条）；(c) 在 `cmd/controllers/main.go` 的 `setupControllers` 里、`<-certsReady` 之后调用这个 Setup；(d) 运行 `make manifests` 生成/更新 `config/webhook/manifests.yaml`。

---

### 4.2 边车注入流程

#### 4.2.1 概念说明

AIBrix 需要每个推理 Pod 里都跑一个 `aibrix-runtime` 边车容器，用来统一采集指标、辅助模型下载与引擎生命周期管理（详见单元 9）。但要求用户在手写 Deployment 时自己加这个容器既繁琐又容易出错。于是 AIBrix 用一个 **mutating webhook 拦截原生 `Deployment`**：只要用户在 Deployment 上打了 `model.aibrix.ai/sidecar-injection: "true"` 注解，webhook 就自动把边车容器「织」进 Pod 模板。

这里有一个关键设计：**注入逻辑被复用在两个 webhook 上**——`Deployment`（普通单引擎部署）和 `StormService`（Prefill/Decode 解耦拓扑，回顾 u5）。两者共享 `sidecar_injection.go` 里的构造函数与常量，差别只在「Pod 模板从哪里取」。

#### 4.2.2 核心流程

以 `Deployment` 为例，`Default()` 的注入决策与执行流程：

```
Default(ctx, obj)                         # mutating 入口
  ├─ 取 Deployment 注解
  ├─ 若 model.aibrix.ai/sidecar-injection != "true" → 直接返回（不注入）
  └─ injectAIBrixRuntime(deployment)
       ├─ 1. 确定 engineType：先看注解 model.aibrix.ai/engine；没有就 inferEngineType(容器镜像名)
       ├─ 2. 确定 sidecarImage：先看注解 model.aibrix.ai/sidecar-runtime-image；没有用默认 aibrix/runtime:v0.5.0
       ├─ 3. 幂等检查：containsContainer(已存在 aibrix-runtime) → 直接返回，避免重复注入
       ├─ 4. 保证共享卷：名为 adapter-storage 的 EmptyDir，挂载点 /tmp/aibrix/adapters
       │     · 若已存在同名卷但不是 EmptyDir → 改写成 EmptyDir
       │     · 若不存在 → 追加该 EmptyDir 卷
       ├─ 5. 给每个「非边车」容器追加该卷的 VolumeMount（让引擎与边车共享适配器存储）
       ├─ 6. buildRuntimeSidecarContainer(image, engineType) 构造边车容器
       └─ 7. 把边车容器 prepend 到 podSpec.Containers 最前面
```

#### 4.2.3 源码精读

**(1) 触发开关：注解决定是否注入。** [pkg/webhook/deployment_webhook.go:56-69](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/deployment_webhook.go#L56-L69) —— 只有注解 `model.aibrix.ai/sidecar-injection` 存在且值为 `"true"` 才调用 `injectAIBrixRuntime`。注意它取的是 **Deployment 级注解**（`deployment.GetAnnotations()`），不是 Pod 模板里的注解。

**(2) 注入的七个步骤对应 [pkg/webhook/deployment_webhook.go:73-146](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/deployment_webhook.go#L73-L146) 的 `injectAIBrixRuntime`。** 几个关键点：

- 引擎类型优先取注解 [constants.ModelLabelEngine = `model.aibrix.ai/engine`](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/constants/model.go#L27-L29)，缺省再推断；
- 幂等：[pkg/webhook/deployment_webhook.go:96-98](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/deployment_webhook.go#L96-L98) 用 `containsContainer` 检查，已注入则直接返回——因为 webhook 在 UPDATE 时也会触发，没有幂等就会无限叠加容器；
- 共享卷修正 [pkg/webhook/deployment_webhook.go:106-127](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/deployment_webhook.go#L106-L127)：同名卷但非 EmptyDir 会被**覆写**成 EmptyDir，这是为保证边车与引擎能共享适配器下载目录；
- 给已有容器补挂载 [pkg/webhook/deployment_webhook.go:128-139](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/deployment_webhook.go#L128-L139) 用 [utils.HasVolumeMount](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/utils/util.go#L323-L330) 避免重复挂载；
- **prepend 到最前** [pkg/webhook/deployment_webhook.go:145](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/deployment_webhook.go#L145)：边车被放在 `Containers` 切片首位。

**(3) 边车容器的样子——所有字段集中在一个工厂函数里。** [pkg/webhook/sidecar_injection.go:46-110](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/sidecar_injection.go#L46-L110) 的 `buildRuntimeSidecarContainer` 构造出的容器包含：名字 `aibrix-runtime`、命令 `aibrix_runtime --port 8080`、两个环境变量（`INFERENCE_ENGINE` = 推断出的引擎、`INFERENCE_ENGINE_ENDPOINT` = `http://localhost:8000`）、metrics 端口 8080、`/healthz` 存活探针与 `/ready` 就绪探针、固定的 CPU/内存 requests/limits。常量都定义在 [pkg/webhook/sidecar_injection.go:28-43](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/sidecar_injection.go#L28-L43)。

**(4) 引擎类型推断：按镜像名子串匹配。** [pkg/webhook/sidecar_injection.go:113-133](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/sidecar_injection.go#L113-L133) 的 `inferEngineType` 把镜像名转小写后依次匹配 `vllm` / `sglang` / `text-generation-inference` 或 `tgi` / `triton` / `llama`+`cpp`（→ `llamacpp`），都不命中则返回 `"unknown"`。这解释了「为什么用户通常不用手动填 `model.aibrix.ai/engine`」。

**(5) StormService 的同款注入。** [pkg/webhook/stormservice_webhook.go:73-155](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/stormservice_webhook.go#L73-L155) 复用同一个 `buildRuntimeSidecarContainer`，只是把「单个 Pod 模板」换成「遍历每个 Role 的 Pod 模板」。注意它的触发条件更宽松：只要注解 `model.aibrix.ai/sidecar-injection` **存在**（不要求值为 `"true"`）就注入（见 [stormservice_webhook.go:57-59](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/stormservice_webhook.go#L57-L59)），这是两个 webhook 的一处细节差异。

#### 4.2.4 代码实践

**实践目标：** 跟踪一次边车注入，列出被修改的 Pod 字段（这正是本讲规格里要求的实践）。

**操作步骤：**

1. 阅读 [pkg/webhook/deployment_webhook.go:73-146](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/deployment_webhook.go#L73-L146)，逐行确认 `injectAIBrixRuntime` 改了 `deployment.Spec.Template.Spec`（即 Pod 模板）的哪些字段。
2. 再读 [pkg/webhook/sidecar_injection.go:46-110](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/sidecar_injection.go#L46-L110)，确认注入的边车容器自身带了哪些子字段。

**需要观察的现象：** 注入只动 Pod 模板（`Spec.Template.Spec`），不动 Deployment 的副本数、策略等其它字段；对每个已存在的业务容器只追加一个 VolumeMount，不改动其命令与镜像。

**预期结果：** 注入过程修改了 Pod 模板的下列字段——

| 被修改的 Pod 字段 | 修改内容 |
| --- | --- |
| `Spec.Containers` | 在切片**头部插入**一个 `aibrix-runtime` 容器 |
| `Spec.Volumes` | 追加或修正一个名为 `adapter-storage` 的 **EmptyDir** 卷 |
| 每个业务容器的 `VolumeMounts` | 追加 `adapter-storage` → `/tmp/aibrix/adapters` 挂载 |

而新插入的 `aibrix-runtime` 容器自身包含：`Name`、`Image`、`Command`（`aibrix_runtime --port 8080`）、`Env`（`INFERENCE_ENGINE`、`INFERENCE_ENGINE_ENDPOINT`）、`VolumeMounts`（共享适配器卷）、`Ports`（metrics 8080）、`LivenessProbe`（`/healthz`）、`ReadinessProbe`（`/ready`）、`Resources`（requests/limits）。

> 说明：本实践是「源码阅读型」，无需运行集群；若想实测，可在本地 kind 集群装好 operator 后，提交一个带 `model.aibrix.ai/sidecar-injection: "true"` 注解的 Deployment，再 `kubectl get pod -o yaml` 查看注入结果（运行结果待本地验证）。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `injectAIBrixRuntime` 在追加边车前要做 `containsContainer` 幂等检查？

> **参考答案：** mutating webhook 在 CREATE 和 UPDATE 都会触发。如果不做幂等，用户每次 `kubectl apply`（UPDATE）都会再插一个 `aibrix-runtime`，Pod 里会叠加出多个同名边车。`containsContainer` 保证「已注入就跳过」，让注入是幂等的。

**练习 2：** 边车被 `prepend` 到 `Containers` 最前面，而不是 `append` 到末尾，这会影响 Pod 行为吗？

> **参考答案：** Kubernetes Pod 的 `containers` 是一个无序的容器列表，**启动顺序不保证**严格按数组顺序（除非用 initContainers 或 kubelet 的特定实现），所以 prepend 更多是「便于在 `kubectl describe` 里第一眼看到边车」的组织习惯，而非依赖启动顺序。真正保证边车与引擎协作的是共享卷与环境变量里指向 `localhost:8000` 的引擎端点。

---

### 4.3 默认值与校验

#### 4.3.1 概念说明

除了注入边车，webhook 还承担两类「数据层」职责：

- **默认值（Defaulter.Default）**：用户没填的字段，由 webhook 补上合理默认值，降低使用门槛。
- **校验（Validator.ValidateCreate/Update）**：拦截不合法的对象，返回带字段路径的错误，让用户立刻知道哪里写错了。

AIBrix 的校验有一个统一风格：用 `k8s.io/apimachinery/pkg/util/validation/field` 包收集 `field.ErrorList`，最后聚合成一个 `apierrors.NewInvalid(...)` 错误返回。这样做的好处是**一次校验能报出所有错误**（而不是遇到第一个就返回），且错误信息带 `spec.metricsSources[0].targetValue` 这样的字段路径，kubectl 能精准定位。

#### 4.3.2 核心流程

PodAutoscaler 的校验管线（最复杂的例子）：

```
ValidateCreate / ValidateUpdate
  └─ validatePodAutoscaler(pa)
       ├─ 1. ScaleTargetRef：name、kind 必填
       ├─ 2. 副本边界：MinReplicas ≤ MaxReplicas
       ├─ 3. 指标窗口：observeWindowSeconds、panicWindowSeconds ∈ (0, 3600]，且 panic ≤ observe
       ├─ 4. ScalingStrategy ∈ {HPA, KPA, APA}
       │     └─ 若是 HPA，禁止设置 subTargetSelector.roleName（角色级伸缩只能用 APA/KPA）
       └─ 5. MetricsSources：恰好 1 个
             ├─ targetMetric、targetValue 必填；targetValue 必须是合法正数
             └─ 按 metricSourceType 分支：
                 · POD        → 需要 protocolType/port/path
                 · EXTERNAL/DOMAIN → 可选 endpoint（空则走 K8s external.metrics API）
                 · RESOURCE   → targetMetric 只能是 cpu/memory；禁止 port/endpoint/path/protocol
                 · CUSTOM     → 无必填
                 · 其它       → NotSupported
       （所有错误聚合为 apierrors.NewInvalid 一次性返回）
```

#### 4.3.3 源码精读

**(1) PodAutoscaler 主校验函数。** [pkg/webhook/podautoscaler_webhook.go:126-264](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/podautoscaler_webhook.go#L126-L264) 的 `validatePodAutoscaler` 依次校验上述五块，每发现一个问题就 `append` 一个 `field.Error`，最后在 [第 255-263 行](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/podautoscaler_webhook.go#L255-L263) 统一返回 `apierrors.NewInvalid`。

**(2) 指标窗口的数值约束。** [pkg/webhook/podautoscaler_webhook.go:279-308](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/podautoscaler_webhook.go#L279-L308) 的 `validateMetricWindows`：窗口缺省值是 observe=180s、panic=60s（常量见 [第 41-44 行](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/podautoscaler_webhook.go#L41-L44)），自定义值必须满足 `0 < 窗口 ≤ 3600` 且 `panic ≤ observe`。

**(3) HPA 不支持角色级伸缩。** [pkg/webhook/podautoscaler_webhook.go:266-277](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/podautoscaler_webhook.go#L266-L277) 的 `validateHPARoleSubtarget`：当 `ScalingStrategy=HPA` 且设置了 `SubTargetSelector.RoleName` 时报错，提示「StormService 角色级伸缩请用 APA 或 KPA」。这把「策略与目标对象」的适配关系在准入阶段就卡死，避免留到 Reconcile 才失败。

**(4) 校验行为的测试佐证。** [pkg/webhook/podautoscaler_webhook_test.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/podautoscaler_webhook_test.go) 是一张表驱动测试，用真实断言固化了「Zero/Negative/Invalid targetValue 必报错」「HPA+roleName 必报错」「窗口越界必报错」等规则（见 [第 113-280 行](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/podautoscaler_webhook_test.go#L113-L280)）。这是理解校验语义最快的方式。

**(5) ModelAdapter 的校验：URL 协议 + 副本数。** [pkg/webhook/modeladapter_webhook.go:59-78](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/modeladapter_webhook.go#L59-L78) 的 `ValidateCreate`：先用 `url.ParseRequestURI` 查语法，再用 [utils.ValidateArtifactURL](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/utils/modeladapter.go#L24-L34) 限定协议白名单——只允许 `s3://`、`gcs://`、`tos://`、`huggingface://`、`hf://`、`/`（本地路径）；并把 `replicas` 校验为必须大于 0。注意它的 `Default` 是空实现（[第 50-52 行](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/modeladapter_webhook.go#L50-L52)），目前只做校验、不补默认值。

**(6) KVCache 的默认值：真正在 `Default` 里干活。** 与 ModelAdapter 相反，[pkg/webhook/kvcache_webhook.go:59-79](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/kvcache_webhook.go#L59-L79) 的 `Default` 会给注解补默认后端：`kvcache.orchestration.aibrix.ai/backend` 与 `.../mode` 缺省时都填 `vineyard`（[KVCacheBackendDefault](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/constants/kvcache.go#L39-L42)）。校验侧 [ValidateCreate:97-108](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/kvcache_webhook.go#L97-L108) 再委托 [utils.ValidateKVCacheBackend](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/utils/kvcache.go#L27-L35) 限定后端只能是 `vineyard` / `hpkv` / `infinistore`。**这正是「先 mutating 补默认、后 validating 校验」协作的典型样例。**

**(7) StormService 的校验：名称长度。** [pkg/webhook/stormservice_webhook.go:168-205](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/stormservice_webhook.go#L168-L205) 校验 StormService 名字 ≤63 字符；并且对会生成 PodSet 的 Role（`PodGroupSize > 1`），估算 `<stormservice>-<role>-<hash>-<index>` 拼出的 Pod 名是否会超过 63 字符上限（估算常量 `estimatedPodNameSuffixLength = 36` 见 [第 162-165 行](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/stormservice_webhook.go#L162-L165)）。把「最终 Pod 名超长」这个下游故障前移到准入阶段拦截。

#### 4.3.4 代码实践

**实践目标：** 通过阅读测试，反向理解 PodAutoscaler 的校验语义（源码阅读型实践）。

**操作步骤：**

1. 打开 [pkg/webhook/podautoscaler_webhook_test.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/podautoscaler_webhook_test.go)。
2. 对每个测试用例（如 `"Zero Target Value"`、`"HPA Does Not Support Role Subtarget"`、`"Panic Window Must Not Exceed Observe Window"`），找到它构造的 `PodAutoscalerSpec` 与期望的 `errorMsg`。
3. 回到 [validatePodAutoscaler](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/podautoscaler_webhook.go#L126-L264)，定位是哪一行代码产生了这个错误信息。

**需要观察的现象：** 每个用例的 `errorMsg`（如 `"must be greater than 0"`、`"subTargetSelector"`、`"panicWindowSeconds"`）都能在源码里找到对应的 `field.Invalid` / `field.Forbidden` 调用。

**预期结果：** 你能画出一张「测试用例 → 触发该校验的源码行」对照表，说明表驱动测试完整覆盖了 `validatePodAutoscaler` 的各分支。如果想运行，可在仓库根目录执行：

```bash
go test ./pkg/webhook/ -run TestPodAutoscalerCustomValidator -v
```

（运行结果待本地验证，取决于本地 Go 环境。）

#### 4.3.5 小练习与答案

**练习 1：** 为什么 PodAutoscaler 校验要收集 `field.ErrorList` 再一次性返回，而不是每个错误立即 return？

> **参考答案：** 用户体验：一次性把所有不合法字段都报出来，用户改一次就能通过；遇到第一个错误就 return 会导致「改一个、提交、又报下一个」的反复折腾。`field.ErrorList` + `apierrors.NewInvalid` 是 Kubernetes 生态里实现「批量报错 + 带字段路径」的标准做法。

**练习 2：** KVCache 的 `Default` 给 backend 注解补默认值 `vineyard`，而 ModelAdapter 的 `Default` 是空实现。这说明二者在使用「默认值」能力上的什么差异？

> **参考答案：** KVCache 强依赖一个具体的后端类型才能工作，所以必须在准入阶段就把缺省值补上（否则 Reconcile 还要再判断）；而 ModelAdapter 的关键字段（如 `artifactURL`）没有合理默认值、必须由用户提供，所以 Default 留空，只在 Validator 里校验用户填的值是否合法。是否实现 Default 取决于「该字段是否存在一个安全的、普适的缺省值」。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**端到端阅读任务**：

**场景：** 用户提交了下面这个 Deployment（片段）：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-vllm
  annotations:
    model.aibrix.ai/sidecar-injection: "true"
spec:
  template:
    spec:
      containers:
        - name: vllm
          image: vllm/vllm-openai:latest
```

**请完成：**

1. **触发判断（模块 1）**：追踪这个 CREATE 请求会命中哪条 webhook 路径？提示：对照 [config/webhook/manifests.yaml](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/config/webhook/manifests.yaml) 里 `medeployment.aibrix.ai` 的 `rules`，确认 `apiGroups: apps` + `resources: deployments` + `operations: CREATE` 匹配。
2. **注入推演（模块 2）**：手动模拟 [injectAIBrixRuntime](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/deployment_webhook.go#L73-L146) 的执行——写出注入后 `Spec.Template.Spec` 的 `Containers`、`Volumes`、以及 vLLM 容器的 `VolumeMounts` 各自变成什么样。引擎类型会被推断成什么？为什么？
3. **校验对照（模块 3）**：如果用户同时提交了一个 `PodAutoscaler` 来伸缩这个 Deployment，但把 `metricsSources[0].targetValue` 写成了 `"0"`，追踪 [validatePodAutoscaler](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/podautoscaler_webhook.go#L126-L264) 会返回什么错误、带什么字段路径。

**参考要点：**

1. 命中 mutating 路径 `/mutate-apps-v1-deployment`（`failurePolicy: Ignore`），对应 `DeploymentCustomDefaulter.Default`；之后还会命中 validating 路径 `/validate-apps-v1-deployment`，但其 `ValidateCreate` 是空实现（[deployment_webhook.go:154-157](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/webhook/deployment_webhook.go#L154-L157)），直接放行。
2. 注入后：`Containers = [aibrix-runtime, vllm]`（边车在前）；`Volumes` 新增一个 `adapter-storage` 的 EmptyDir；vLLM 容器的 `VolumeMounts` 新增 `adapter-storage → /tmp/aibrix/adapters`；边车自身的 `INFERENCE_ENGINE` 会被 `inferEngineType` 推断成 `"vllm"`（因为镜像名含 `vllm`）。
3. `targetValue="0"` 会被 `resource.ParseQuantity` 解析成功但 `qty.Sign() <= 0`，于是追加 `field.Invalid(msPath.Child("targetValue"), "0", "must be greater than 0")`，最终聚合为 `apierrors.NewInvalid`，错误信息含字段路径 `spec.metricsSources[0].targetValue`，与测试用例 `"Zero Target Value"` 一致。

## 6. 本讲小结

- AIBrix 的准入 webhook 跑在 `cmd/controllers` 进程内、由 Manager 托管，分 **mutating（`CustomDefaulter.Default`，可改对象）** 与 **validating（`CustomValidator.Validate*`，只校验）** 两类，触发时机由 kubebuilder 标记里的 `verbs=create;update` 与 `failurePolicy=ignore` 决定。
- 五个 webhook（ModelAdapter / KVCache / StormService / Deployment / PodAutoscaler）用 `NewWebhookManagedBy(mgr).For(T).WithDefaulter(...).WithValidator(...).Complete()` 统一注册，且**全部在 TLS 证书就绪（`<-certsReady`）之后**才挂载。
- 边车注入是 mutating webhook 的代表：注解 `model.aibrix.ai/sidecar-injection: "true"` 触发，由 `deployment_webhook.go` 的 `injectAIBrixRuntime` 执行，`sidecar_injection.go` 提供共享的容器构造与引擎推断；它幂等地修改 Pod 模板的 `Containers`、`Volumes` 与各容器的 `VolumeMounts`，并被 StormService 复用。
- 校验统一用 `field.ErrorList` 聚合错误，PodAutoscaler 的校验最完整（目标引用、副本边界、指标窗口、策略、指标源类型分支）；KVCache 演示了「先补默认值、后校验」的协作；StormService 把下游 Pod 名超长故障前移到准入阶段。
- kubebuilder 标记是 webhook 配置的单一数据源，`make manifests` 把它生成成 `config/webhook/manifests.yaml`，API Server 据此路由请求。

## 7. 下一步学习建议

- **进入自动伸缩主线：** 本讲讲了 PodAutoscaler 的「准入校验」，下一讲 [u3-l1 PodAutoscaler 控制器与伸缩总览](u3-l1-podautoscaler-overview.md) 会讲它通过准入后的 Reconcile 主循环，看校验过的 Spec 如何驱动真正的伸缩决策。
- **跟进边车下游：** 边车注入只是把 `aibrix-runtime` 放进 Pod，这个容器内部做什么在单元 9（[u9-l1 AI Runtime 边车与引擎生命周期](u9-l1-runtime-engine-lifecycle.md)）展开——指标标准化、模型下载、引擎激活协议都从这里开始。
- **继续控制平面框架：** 想更系统地理解 webhook 与控制器的装配关系，可重读 [u2-l1 控制器管理器入口与启动流程](u2-l1-controller-manager-entry.md) 里的「两阶段注册」与证书流程，本讲的 `<-certsReady` 正是它的 webhook 侧对应。
