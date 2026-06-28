# 自定义 CRD 与策略附着（Policy Attachment）

## 1. 本讲目标

Gateway API 本身只定义了「流量怎么进、怎么路由」（Gateway/GatewayClass/HTTPRoute 等核心资源），但真实业务还需要大量「数据面调优」：限制请求体大小、压缩响应、改写客户端 IP、开启 WAF、注入 NGINX 原生指令……这些能力如果都塞进 HTTPRoute 会让资源变得臃肿且不可扩展。NGF 的做法是提供一组**自定义 CRD**，让你按需声明，再通过 Gateway API 标准的**策略附着（Policy Attachment）**模型挂到目标资源上。

学完本讲，你应当能够：

- 说出 NGF 在 `apis/` 下定义了哪些 CRD，它们分属哪几个 API 版本、各自解决什么问题。
- 区分**配置类资源（NginxProxy）**与**策略类资源（各种 Policy）**两类 CRD 的本质差别。
- 理解 Gateway API 的 **Direct / Inherited** 策略附着语义，并能在源码的 kubebuilder 注解里识别它。
- 看懂 `graph/policies.go` 与 `graph/nginxproxy.go` 如何把用户声明的策略与 NginxProxy 解析、校验、去冲突、附着到图节点上。

## 2. 前置知识

- **CRD（CustomResourceDefinition）**：Kubernetes 扩展自定义资源的方式。NGF 的 CRD 都属于 API 组 `gateway.nginx.org`，分 `v1alpha1` 和 `v1alpha2` 两个版本。Go 侧的类型定义（如 `ClientSettingsPolicy`）配合 controller-gen 的 `+kubebuilder:` 注解，生成 CRD schema 与 deepcopy 代码。
- **策略附着（Policy Attachment，GEP-713）**：Gateway API 规定的「把附加配置挂到某个资源」的标准模式。一个 Policy 资源通过 `targetRef`（或 `targetRefs`）指向要配置的对象，控制器负责把策略解析并生效。它分两种语义：
  - **Direct（直接附着）**：策略只作用于它直接指向的那一个资源。
  - **Inherited（继承式附着）**：策略挂在父资源（如 Gateway）上时，效果会**向下继承**到挂在它下面的子资源（如 HTTPRoute）。
- **`LocalPolicyTargetReference`**：Gateway API 提供的类型，描述「在本命名空间内引用一个目标」，含 `group`、`kind`、`name`，是 Policy `targetRef(s)` 的标准字段类型。
- **本讲的坐标系**：承接 u5-l1（Graph 构建），本讲聚焦 Graph 构建流程里**最后一步**才处理的策略与 NginxProxy 解析。你应已了解 `Graph`、节点三元组（`Source`+`Valid`+`Conditions`）、`EffectiveNginxProxy` 等概念。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `apis/v1alpha1/register.go` | 注册 v1alpha1 组的所有自定义类型（策略 CRD 的「花名册」），是 CRD 类型概览的入口 |
| `apis/v1alpha2/register.go` | 注册 v1alpha2 组的类型（NginxProxy、ObservabilityPolicy） |
| `apis/v1alpha2/nginxproxy_types.go` | `NginxProxy` 的类型定义——NGF 最重要的「数据面配置」资源 |
| `apis/v1alpha1/clientsettingspolicy_types.go` | `ClientSettingsPolicy` 类型定义，是 Inherited 策略的典型样本 |
| `apis/v1alpha1/snippetspolicy_types.go` | `SnippetsPolicy` 类型定义，是 Direct 策略的典型样本 |
| `internal/controller/nginx/config/policies/policy.go` | 定义所有策略共用的 `Policy` 接口（`GetTargetRefs`/`GetPolicyStatus`/…） |
| `internal/controller/state/graph/policies.go` | 图中对策略的**解析、冲突裁决、附着**实现，本讲核心 |
| `internal/controller/state/graph/nginxproxy.go` | 图中对 NginxProxy 的解析与 GatewayClass/Gateway 两级合并（`EffectiveNginxProxy`） |
| `internal/controller/state/graph/graph.go` | `BuildGraph` 主入口，调用 `processPolicies` 与 `attachPolicies` 的位置 |

## 4. 核心概念与源码讲解

### 4.1 CRD 类型概览：NGF 自定义资源全景

#### 4.1.1 概念说明

NGF 的自定义资源其实分两类，初学者最常混淆的就是这一点：

1. **配置类资源**：用来「配置 NGF 这个实现本身」或「配置数据面 NGINX 实例」，通过 **`parametersRef`** 引用，不是策略。代表是 `NginxProxy`（数据面参数）和 `NginxGateway`（控制面参数）。
2. **策略类资源**：用 Gateway API 的**策略附着**模型，通过 **`targetRef(s)`** 挂到 Gateway/Route/Service 上，实现「按需调优某条流量」。代表是 `ClientSettingsPolicy`、`ProxySettingsPolicy`、`RateLimitPolicy`、`SnippetsPolicy`、`UpstreamSettingsPolicy`、`WAFPolicy`、`ObservabilityPolicy`。

二者的根本差别：配置类资源**不附着到具体的路由对象**，它影响的是「这个 Gateway 用什么 NGINX 跑」；策略类资源**附着到具体的路由/网关对象**，影响的是「经过这个对象的流量怎么处理」。

#### 4.1.2 核心流程

NGF 把所有自定义类型先注册到 runtime scheme，这样控制器才能 watch、反序列化它们。「注册」就是一份花名册：把每个 Go 类型加到对应 GroupVersion 下。

```
apis/v1alpha1/register.go   ──┐  addKnownTypes() 注册到 scheme
apis/v1alpha2/register.go   ──┘
        │
        ▼
控制器 (controller-runtime) 只能 watch「已注册进 scheme 的类型」
        │
        ▼
两类用途分流：
  ┌─ NginxProxy/NginxGateway → 经 parametersRef 引用 → 配置数据面/控制面
  └─ 各种 Policy            → 经 targetRef(s) 附着  → 调优某条流量
```

#### 4.1.3 源码精读

所有 v1alpha1 类型都在一处集中注册，这是看「NGF 到底定义了哪些 CRD」的最快入口：

[v1alpha1/register.go:33-57](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/register.go#L33-L57) 把 12 种类型（6 对 `Xxx`+`XxxList`）注册进 `gateway.nginx.org/v1alpha1` 组，包括 `NginxGateway`、`AuthenticationFilter`、`ClientSettingsPolicy`、`ProxySettingsPolicy`、`SnippetsFilter`、`UpstreamSettingsPolicy`、`SnippetsPolicy`、`RateLimitPolicy`、`WAFPolicy`。`GroupName` 常量定义在 [v1alpha1/register.go:10](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/register.go#L10)。

v1alpha2 组则只注册两类较新的资源：

[v1alpha2/register.go:34-39](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha2/register.go#L34-L39) 注册 `NginxProxy` 与 `ObservabilityPolicy`，组名同为 `gateway.nginx.org`。为何分两个版本？因为 NGF 的 CRD 是按「成熟度/引入时间」分批发布，新资源先放 v1alpha2，老的留在 v1alpha1，互不干扰。

`NginxProxy` 是配置类资源的代表。它的类型注释直接说清了它「不是策略、而是参数引用」的本质：

[nginxproxy_types.go:20-32](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha2/nginxproxy_types.go#L20-L32) 注释明确：`NginxProxy` 可被 **GatewayClass 的 `parametersRef`** 或 **Gateway 的 `infrastructure.parametersRef`** 引用；挂在 GatewayClass 上时对该类下所有 Gateway 生效，挂在 Gateway 上时只对该 Gateway 生效，两者同时存在则合并（Gateway 覆盖 GatewayClass）。注意它**没有 `targetRef`，也没有 `PolicyStatus`**——这与策略类资源截然不同。

`NginxProxySpec` 字段非常多，但可归为几类：网络与协议（`IPFamily`、`DisableHTTP2`、`DisableSNIHostValidation`）、可观测（`Telemetry`、`Metrics`、`Logging`）、客户端 IP 改写（`RewriteClientIP`）、NGINX 运行参数（`WorkerConnections`、`DNSResolver`、`ServerTokens`、`Compression`）、部署形态（`Kubernetes`，含 Deployment/DaemonSet/Service）、Plus 与 WAF 开关（`NginxPlus`、`WAF`）。见 [nginxproxy_types.go:44-146](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha2/nginxproxy_types.go#L44-L146)。

策略类资源则共享一个统一接口，这是它们能被同一套图解析逻辑处理的根本：

[policy.go:16-21](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/policy.go#L16-L21) 定义 `Policy` 接口，要求实现 `GetTargetRefs() []gatewayv1.LocalPolicyTargetReference`、`GetPolicyStatus()`、`SetPolicyStatus(...)` 并内嵌 `client.Object`。凡是被 NGF 当作「策略」处理的 CRD，都必须满足这个接口——这也是 `graph/policies.go` 能用同一份代码处理 6、7 种不同策略的原因。

#### 4.1.4 代码实践

**实践目标**：建立「NGF CRD 全景表」的肌肉记忆，区分配置类与策略类。

**操作步骤**：

1. 在 `apis/v1alpha1/register.go` 与 `apis/v1alpha2/register.go` 里数一遍注册的类型。
2. 对每个策略类型，打开其 `*_types.go`，确认它有 `Spec` 里形如 `TargetRef` 或 `TargetRefs` 的字段，以及 `Status gatewayv1.PolicyStatus` 字段。
3. 打开 `nginxproxy_types.go`，确认 `NginxProxy` **没有** `TargetRef` 字段、**没有** `PolicyStatus` 字段。

**需要观察的现象**：策略类型的 Status 都是 `gatewayv1.PolicyStatus`（带 `Ancestors` 列表，用于回写策略生效状态）；`NginxProxy` 的校验错误则回写到 GatewayClass 的 Conditions（见 4.3 节），而非自身的 PolicyStatus。

**预期结果**：你能把所有 CRD 填进一张表：

| 类型 | 版本 | 类别 | 引用方式 |
| --- | --- | --- | --- |
| NginxProxy | v1alpha2 | 配置类 | parametersRef |
| NginxGateway | v1alpha1 | 配置类 | parametersRef |
| ClientSettingsPolicy | v1alpha1 | 策略类 | targetRef |
| ProxySettingsPolicy | v1alpha1 | 策略类 | targetRef |
| RateLimitPolicy | v1alpha1 | 策略类 | targetRef |
| UpstreamSettingsPolicy | v1alpha1 | 策略类 | targetRef |
| WAFPolicy | v1alpha1 | 策略类 | targetRef |
| SnippetsPolicy | v1alpha1 | 策略类 | targetRefs |
| ObservabilityPolicy | v1alpha2 | 策略类 | targetRef |
| AuthenticationFilter | v1alpha1 | 策略类 | targetRef |
| SnippetsFilter | v1alpha1 | 策略类 | targetRef |

> 待本地验证：CRD 实际是否安装可用 `kubectl get crd | grep gateway.nginx.org` 核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `NginxProxy` 不能用 `targetRef` 附着到 HTTPRoute？

**参考答案**：因为 `NginxProxy` 是**配置类资源**，配置的是「数据面 NGINX 实例本身」（IP family、worker connections、部署副本数等），这些是 Gateway/数据面维度的参数，与某条具体路由无关，所以只能经 GatewayClass/Gateway 的 `parametersRef` 引用，不参与策略附着。

**练习 2**：如果新增一种策略 CRD，最少要让它的 Go 类型满足什么？

**参考答案**：实现 `policies.Policy` 接口（提供 `GetTargetRefs`、`GetPolicyStatus`、`SetPolicyStatus` 并内嵌 `client.Object`），并在 `register.go` 的 `addKnownTypes` 里注册。这样图层的统一解析逻辑才能识别它。

---

### 4.2 策略附着模型：Direct 与 Inherited

#### 4.2.1 概念说明

Gateway API 的策略附着（GEP-713）按「效果是否向下继承」分两种语义：

- **Direct（直接附着）**：策略只对它直接 `targetRef` 指向的那个资源生效，不向下传递。例如 `SnippetsPolicy` 挂到 Gateway 上，只有该 Gateway 拿到注入的 NGINX 片段，它下面的 HTTPRoute 不会自动继承。
- **Inherited（继承式附着）**：策略挂在 Gateway 上时，效果会**向下继承**到挂在该 Gateway 下的 HTTPRoute/GRPCRoute。例如 `ClientSettingsPolicy` 挂到 Gateway 上，该 Gateway 下所有路由的客户端连接行为都受影响。

NGF 在源码里用一行 **kubebuilder 元数据标签**把每个策略「归类」，这是 Direct/Inherited 在代码中最直接的体现：

```
+kubebuilder:metadata:labels="gateway.networking.k8s.io/policy=direct"      // Direct
+kubebuilder:metadata:labels="gateway.networking.k8s.io/policy=inherited"   // Inherited
```

这个标签会被 controller-gen 写进生成的 CRD 对象，供 Gateway API 生态工具识别策略的继承语义。

> 注意区分另一个正交维度：`targetRef`（单数）与 `targetRefs`（复数切片）。前者一条策略只能指一个目标，后者可以指多个（如 `SnippetsPolicy`、较新的策略）。**Direct/Inherited 讲的是「效果是否继承」，单复数讲的是「能指几个目标」，二者不是一回事**。

#### 4.2.2 核心流程

策略从用户 YAML 到生效的附着流程：

```
用户创建 ClientSettingsPolicy (targetRef → HTTPRoute tea)
        │
        ▼  ① processPolicies：解析 targetRef、校验、查目标是否存在
图节点 Policy{Source, Valid, TargetRefs, Conditions}
        │
        ▼  ② markConflictedPolicies：同类型同目标的策略按时间排序，后者判冲突置无效
        │
        ▼  ③ attachPolicies：按 targetRef.Kind 分发
        │      ├─ Gateway  → attachPolicyToGateway（校验 GlobalSettings，挂到 gw.Policies）
        │      ├─ HTTPRoute/GRPCRoute → attachPolicyToRoute（逐 parentRef 校验，挂到 route.Policies）
        │      └─ Service  → attachPolicyToService（挂到 svc.Policies）
        ▼
④ addPolicyAffectedStatusToTargetRefs：给被策略影响的目标加 *PolicyAffected 条件
```

#### 4.2.3 源码精读

**Inherited 样本：ClientSettingsPolicy**

[clientsettingspolicy_types.go:14](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/clientsettingspolicy_types.go#L14) 标注 `policy=inherited`，[clientsettingspolicy_types.go:16-17](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/clientsettingspolicy_types.go#L16-L17) 注释说明它是「Inherited Attached Policy」，用于配置客户端与 NGINX 之间的连接行为。它的 `TargetRef` 是单数 [clientsettingspolicy_types.go:57](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/clientsettingspolicy_types.go#L57)，CEL 校验限定 `kind` 只能是 `Gateway`/`HTTPRoute`/`GRPCRoute`、`group` 必须是 `gateway.networking.k8s.io`。Status 字段是标准的 `gatewayv1.PolicyStatus`，承载 Ancestors 状态。

**Direct 样本：SnippetsPolicy**

[snippetspolicy_types.go:12](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/snippetspolicy_types.go#L12) 标注 `policy=direct`。它的 `TargetRefs` 是**复数切片** [snippetspolicy_types.go:46](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/snippetspolicy_types.go#L46)，CEL 强制每个目标 `kind` 必须是 `Gateway`（直接附着的典型约束——只挂在网关层）。

可以用一条命令快速核对全仓库的标签分布：

```bash
grep -rn 'gateway.networking.k8s.io/policy=' apis/
```

结果是：`direct` —— `SnippetsPolicy`、`UpstreamSettingsPolicy`；`inherited` —— `ClientSettingsPolicy`、`ProxySettingsPolicy`、`RateLimitPolicy`、`WAFPolicy`。

**策略图节点的数据结构**

[policies.go:34-53](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L34-L53) 定义图层 `Policy` 结构。关键字段：

- `Source policies.Policy`：原始 CRD 对象（满足 4.1 的接口）。
- `TargetRefs []PolicyTargetRef`：解析后的目标引用（Kind/Group/Nsname）。
- `Valid bool` + `Conditions`：策略级校验结论（沿用节点三元组范式）。
- `InvalidForGateways map[types.NamespacedName]struct{}`：因 NginxProxy 配置导致「对某些 Gateway 无效」的集合——这是 Direct/Inherited 之外、针对多 Gateway 场景的细粒度有效性。
- `Ancestors []PolicyAncestor`：用于回写 `PolicyStatus.Ancestors`，按 Gateway/Route 维度记录生效情况。

**冲突裁决：markConflictedPolicies**

[policies.go:765-839](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L765-L839) 处理「同类型策略指向同一目标」的冲突。它按 `(policyGVK, TargetRef)` 分组，组内用 `ngfsort.LessClientObject` 排序（先按创建时间、再按名字字典序）确定优先级，高优先级策略与后续策略两两比对，`validator.Conflicts` 为真则把后者 `Valid` 置 `false` 并加 `PolicyConflicted` 条件。注释 [policies.go:804-819](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L804-L819) 用 `[A, B, C]` 的例子讲清了「B 与 A 冲突则 B 失效，C 仍有效」的传递逻辑。

#### 4.2.4 代码实践

**实践目标**：亲手附着一个 ClientSettingsPolicy 到 HTTPRoute，并理解 Direct/Inherited 的差别。

**操作步骤**：

1. 参考 [examples/client-settings-policy/tea-client-settings.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/client-settings-policy/tea-client-settings.yaml)，它把一个 `ClientSettingsPolicy` 通过 `targetRef` 指向名为 `tea` 的 HTTPRoute，设置 `body.maxSize: "75"`（字节）。
2. 在已部署 NGF 的集群中 apply 该示例目录下的全部资源（`gateway.yaml`、`app.yaml`、`httproutes.yaml`、`tea-client-settings.yaml`）。
3. 用 `kubectl describe clientsettingspolicy tea-client-settings` 查看 `Status.Ancestors`。

**需要观察的现象**：策略的 `Ancestors` 里会出现以该 HTTPRoute（或其所属 Gateway）为 `AncestorRef`、`controllerName` 为 NGF 控制器名的条目，`Conditions` 中应有 `Accepted=True`。向 tea 后端发送一个大于 75 字节请求体的请求，应被 NGINX 以 413 拒绝。

**预期结果**：策略成功附着到目标 HTTPRoute，`*PolicyAffected` 条件被加到目标 Route 的 status 上（由 4.2.2 步骤 ④ 完成）。

> 待本地验证：413 的实际触发阈值与 NGINX `client_max_body_size` 的取整规则有关，需在真实集群中验证。

#### 4.2.5 小练习与答案

**练习 1**：若一个 `ClientSettingsPolicy` 同时挂在 Gateway 和它下面某 HTTPRoute 上，会怎样？

**参考答案**：`ClientSettingsPolicy` 是 Inherited 策略。挂在 Gateway 上的那份会向下继承到所有子 Route；挂在 Route 上的那份只对该 Route 生效。若两者对同一配置项冲突，按 `markConflictedPolicies` 的优先级（创建时间+名字）裁决，输的一方被置 `Valid=false` 并标 `PolicyConflicted`。

**练习 2**：为什么 `SnippetsPolicy` 用 `targetRefs`（复数）且只允许 `kind: Gateway`？

**参考答案**：它是 Direct 策略，注入的是 NGINX 原生片段，属于网关层配置，不允许向下继承到 Route，所以 `kind` 限定 Gateway；又因为一条 snippets 策略可能想同时影响多个 Gateway，故用复数 `targetRefs`。这正说明「单复数」与「Direct/Inherited」是两个独立维度。

---

### 4.3 图中策略与 NginxProxy 的解析

#### 4.3.1 概念说明

图层（`internal/controller/state/graph`）是 NGF 把「Kubernetes 资源快照」翻译成「内部模型」的地方。策略与 NginxProxy 在这里有两条独立的解析路径，但共享同一套设计哲学：**错误不抛异常，而是沉淀进 Conditions + Valid 布尔位**（节点三元组范式，见 u5-l1）。

- **策略解析**：`processPolicies` → `markConflictedPolicies` → `attachPolicies`，最终把有效的 `*Policy` 挂到 Gateway/Route/Service 节点的 `Policies` 字段上。
- **NginxProxy 解析**：`processNginxProxies` → `buildNginxProxy`（校验）→ 在构建 Gateway 时经 `buildEffectiveNginxProxy` 把 GatewayClass 级与 Gateway 级两份 NginxProxy **合并**成 `EffectiveNginxProxy`。

一个关键事实：**策略必须最后处理**，因为它依赖图中其它资源（Gateway、Route、Service、NginxProxy 的 GlobalSettings）是否已就绪。`BuildGraph` 中的注释 [graph.go:383](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go#L383) 明确写了这一点。

#### 4.3.2 核心流程

**策略解析与附着**（`graph.go` 中两处调用）：

```
BuildGraph(...)
   │
   ├─ [先] 处理 GatewayClass、Gateway、Route、Service、BackendTLSPolicy……
   │
   ├─ graph.go:384  processPolicies(...)   // 解析+校验+冲突裁决，产出 map[PolicyKey]*Policy
   │       └─ processWAFPolicies(...)      // WAFPolicy 额外拉取 bundle（见 u10）
   │
   ├─ graph.go:396  addPolicyAffectedStatusToTargetRefs(...)  // 给目标加 *PolicyAffected 条件
   │
   └─ graph.go:436  g.attachPolicies(...)  // 把有效策略挂到 gw/route/svc 节点
```

**NginxProxy 解析与合并**：

```
BuildGraph(...)
   │
   ├─ graph.go:276  processNginxProxies(state.NginxProxies, gc, gws, plus)
   │       └─ 对每个被引用的 NginxProxy：buildNginxProxy → validateNginxProxy（ErrMsgs）
   │
   └─ buildGateways(...) 内部，对每个 Gateway：
          buildEffectiveNginxProxy(gcNp, gwNp)   // 合并两级，Gateway 覆盖 GatewayClass
                 ├─ 用 JSON marshal/unmarshal 做「局部覆盖全局」
                 └─ cleanupEffectiveNginxProxy 修正 JSON 合并搞不定的切片清空/互斥
```

#### 4.3.3 源码精读

**策略解析主入口 processPolicies**

[policies.go:582-658](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L582-L658) 遍历所有原始策略，对每个 `targetRef` 用 `refGroupKind` 归类（`gateway.networking.k8s.io/Gateway`、`.../HTTPRoute`、`.../GRPCRoute`、`core/Service`），若目标在图中不存在则 `continue`（丢弃该 ref）。收集到有效 targetRefs 后，跑 `checkTargetRoutesForOverlap`（防止策略目标与其它路由在「命名空间:网关:主机:端口:路径」上重叠产生歧义，见 [policies.go:660-730](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L660-L730)）和 `validator.Validate`，构造 `Policy` 节点。若策略没有任何有效 targetRef，直接 `continue`——即该策略不进图。

**附着分发 attachPolicies**

[policies.go:254-281](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L254-L281) 按 `ref.Kind` 三路分发。以 Route 为例，[attachPolicyToRoute policies.go:359-422](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L359-L422) 会：

1. 检查 `ngfPolicyAncestorsFull`（PolicyStatus.Ancestors 有 32 条上限）。
2. 校验 Route 自身 `Valid`/`Attachable`/有 `ParentRefs`，否则给 Ancestor 加 `PolicyTargetNotFound`。
3. **逐 parentRef** 用 `ValidateGlobalSettings` 校验「全局依赖」——这是策略与 NginxProxy 的交汇点：[policies.go:401-404](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L401-L404) 从 `parentRef.EffectiveNginxProxy` 读取 `TelemetryEnabled`/`WAFEnabled` 填入 `GlobalSettings`，再交给策略校验器判断「在当前 NginxProxy 配置下，这条策略是否合法」。例如 `ObservabilityPolicy` 要求 telemetry 开启、`WAFPolicy` 要求 WAF 开启，否则对该 Gateway 标 `InvalidForGateways`。
4. 通过校验的策略才 `append` 到 `route.Policies`。

Gateway 路径 [attachPolicyToGateway policies.go:424-500](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L424-L500) 逻辑类似，但额外处理：Gateway 不存在/无效时给 Ancestor 加相应条件；`SnippetsPolicy` 还会经 `propagateSnippetsPolicyToRoutes`（[policies.go:502-528](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L502-L528)）把策略传播到挂在该 Gateway 下的所有 Route（这是 Direct 策略的一个特例实现）。

**目标状态回写**

[policies.go:899-924](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L899-L924) 的 `addStatusToTargetRefs` 按 policyKind 给目标加 `NewClientSettingsPolicyAffected`、`NewRateLimitPolicyAffected` 等条件，让用户从 Route/Gateway 的 status 上就能看到「被某个策略影响了」。

**NginxProxy 解析 processNginxProxies**

[nginxproxy.go:224-267](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/nginxproxy.go#L224-L267) 分别处理 GatewayClass 级与 Gateway 级引用：用 `gcReferencesAnyNginxProxy`/`gwReferencesAnyNginxProxy` 判断是否引用了 `gateway.nginx.org` 组、`Kind=NginxProxy` 的资源，找到则 `buildNginxProxy` 校验。注意 GatewayClass 级引用「没有 namespace 会忽略」（[nginxproxy.go:235-236](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/nginxproxy.go#L235-L236) 注释），错误会进 GatewayClass status。

图层 `NginxProxy` 节点 [nginxproxy.go:29-36](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/nginxproxy.go#L29-L36) 沿用三元组：`Source`+`ErrMsgs`+`Valid`。

**两级合并 buildEffectiveNginxProxy**

这是 NginxProxy 最巧妙的设计。[nginxproxy.go:44-91](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/nginxproxy.go#L44-L91) 实现合并：

1. 若两级任一无效，直接取有效的那一级（两级都无效返回 `nil`）。
2. 把 GatewayClass 级 spec 拷两份作 `global`/`gcSpec`，Gateway 级拷一份作 `local`。
3. **把 `local` 序列化成 JSON，再反序列化「覆盖」到 `global` 上**——Go 的 JSON 反序列化天然实现「local 已设字段覆盖 global，local 未设字段保留 global」，见注释 [nginxproxy.go:64-65](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/nginxproxy.go#L64-L65)。
4. JSON 合并搞不定「切片清空」和「字段互斥」，交给 `cleanupEffectiveNginxProxy`（[nginxproxy.go:97-103](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/nginxproxy.go#L97-L103)）逐项修补：如 `cleanupKubernetes` 处理 Deployment 与 DaemonSet 互斥、`cleanupWAF` 在 Gateway 只设了 `waf.disableCookieSeed` 时从 GatewayClass 补回 `waf.enable` 等子字段（[nginxproxy.go:144-157](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/nginxproxy.go#L144-L157)）。

合并发生在 `buildGateways` 里：[gateway.go:101](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway.go#L101) 调 `buildEffectiveNginxProxy(gcNp, np)`，结果存进每个 Gateway 节点的 `EffectiveNginxProxy` 字段（[gateway.go:29-32](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway.go#L29-L32)），供后续配置生成（u6）与策略的 GlobalSettings 校验（本节上文）共用。

#### 4.3.4 代码实践

**实践目标**：追踪一个 ClientSettingsPolicy 在图中被解析、附着、回写状态的完整链路（源码阅读型实践）。

**操作步骤**：

1. 从 `BuildGraph` 入口出发：在 [graph.go:384](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go#L384) 看到 `state.NGFPolicies` 被传入 `processPolicies`。
2. 在 [policies.go:604](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L604) 处确认 `policy.GetTargetRefs()` 取出的 ref 经 `refGroupKind` 判定为 `hrGroupKind`（`gateway.networking.k8s.io/HTTPRoute`），并在 `routes` map 中命中目标 Route。
3. 跟到 [attachPolicies policies.go:264-270](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L264-L270)，进入 `attachPolicyToRoute`，确认策略被 `append` 到 `route.Policies`（[policies.go:420](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L420)）。
4. 在 [policies.go:899-903](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L899-L903) 确认该 Route 被加上 `NewClientSettingsPolicyAffected()` 条件。

**需要观察的现象**：你能画出从 `state.NGFPolicies`（原始 CRD）→ `Policy` 图节点 → `route.Policies`（附着）→ Route Conditions（回写）的四跳链路，且每一步都对应一个具体函数。

**预期结果**：理解「策略附着」在图层不是一步完成，而是「解析 → 冲突裁决 → 附着 → 状态回写」四个阶段，且 `attachPolicies` 是最后一步（在 `BuildGraph` 返回前的 [graph.go:436](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go#L436)）。

> 这是源码阅读型实践，不依赖运行集群；若要运行验证，可结合 4.2.4 的实操观察 `route.Policies` 最终如何驱动 NGINX 配置生成（见 u8-l3）。

#### 4.3.5 小练习与答案

**练习 1**：`buildEffectiveNginxProxy` 为什么用 JSON marshal/unmarshal 而不是手写逐字段合并？

**参考答案**：手写合并要在新增字段时同步维护合并代码，极易遗漏；JSON 反序列化的「已设覆盖、未设保留」语义天然实现「Gateway 覆盖 GatewayClass」，新增字段自动获得正确合并行为，零维护。代价是 JSON 合并不能区分「nil」和「空切片」，所以再用 `cleanupEffectiveNginxProxy` 做少量修补。

**练习 2**：如果一个 `WAFPolicy` 附着到某 HTTPRoute，但该 Route 所属 Gateway 的 NginxProxy 没有开启 `waf.enable`，会发生什么？

**参考答案**：在 `attachPolicyToRoute` 中，`ValidateGlobalSettings` 会拿到 `WAFEnabled=false`（由 [WAFEnabledForNginxProxy nginxproxy.go:210-212](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/nginxproxy.go#L210-L212) 从 EffectiveNginxProxy 算出），校验不通过，于是该策略被加入 `policy.InvalidForGateways[parentRef.GatewayNsName]`，不挂到 `route.Policies`，并在 Ancestor 上回写相应 Condition。这正是「策略依赖 NginxProxy 全局配置」的体现。

## 5. 综合实践

把本讲三个模块串起来，完成一个「配置 + 策略」联合的小任务：

1. **配置数据面**：创建一个 `NginxProxy`，在 `spec.logging.errorLevel` 设为 `info`，并通过 GatewayClass 的 `parametersRef` 引用它。
2. **附着策略**：再创建一个 `ClientSettingsPolicy`，`targetRef` 指向某个 HTTPRoute，设置 `body.maxSize`。
3. **源码追踪**：在 `graph.go` 中分别定位这两类资源的处理顺序——`processNginxProxies`（[graph.go:276](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go#L276)）在前、`processPolicies`（[graph.go:384](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go#L384)）在后，验证「策略依赖配置先就绪」的依赖顺序。
4. **验证理解**：用自己的话回答——为什么 NginxProxy 的校验错误写进 GatewayClass 的 Conditions，而 ClientSettingsPolicy 的状态写进自身的 `PolicyStatus.Ancestors`？（提示：前者是配置类资源、无 PolicyStatus；后者是策略类资源、走标准策略状态模型。）

> 待本地验证：日志级别与请求体限制的实际效果需在真实集群 + NGF 部署中验证；源码追踪部分可离线完成。

## 6. 本讲小结

- NGF 的自定义 CRD 分两类：**配置类**（`NginxProxy`/`NginxGateway`，经 `parametersRef` 引用，配置数据面/控制面本身）与**策略类**（各种 Policy，经 `targetRef(s)` 附着，调优某条流量），二者本质差别在于「是否有 targetRef、是否走 PolicyStatus」。
- 所有策略类资源共享 `policies.Policy` 接口（`GetTargetRefs`/`GetPolicyStatus`/`SetPolicyStatus` + `client.Object`），这是图层能用同一套代码处理多种策略的根本；类型在 `register.go` 的 `addKnownTypes` 集中注册，分属 v1alpha1/v1alpha2。
- Gateway API 策略附着分 **Direct**（只对直接目标生效，不继承）与 **Inherited**（挂在父资源时向下继承），在源码里由 `+kubebuilder:metadata:labels="gateway.networking.k8s.io/policy=direct|inherited"` 标注；`targetRef`（单数）与 `targetRefs`（复数）是与之正交的「目标数量」维度。
- 图层策略处理是 `BuildGraph` 的**最后一步**：`processPolicies`（解析+校验+重叠检查）→ `markConflictedPolicies`（按 ngfsort 优先级裁决冲突）→ `attachPolicies`（按 Kind 分发到 Gateway/Route/Service，并经 `ValidateGlobalSettings` 校验对 NginxProxy 全局配置的依赖）→ `addPolicyAffectedStatusToTargetRefs`（给目标加 `*PolicyAffected` 条件）。
- NginxProxy 经 `processNginxProxies` 解析校验后，在 `buildGateways` 中由 `buildEffectiveNginxProxy` 用 JSON marshal/unmarshal 把 GatewayClass 级与 Gateway 级两份合并（Gateway 覆盖 GatewayClass），再用 `cleanupEffectiveNginxProxy` 修补切片清空与字段互斥，产出每个 Gateway 的 `EffectiveNginxProxy`。
- 图层沿用节点三元组范式：错误不抛异常，而是沉淀进 `Conditions` + `Valid`；策略额外用 `InvalidForGateways` 表达「在多 Gateway 场景下对部分 Gateway 无效」的细粒度结论。

## 7. 下一步学习建议

- **u8-l3 策略到 NGINX 指令的生成**：本讲止于策略被挂到 `route.Policies`/`gw.Policies`；这些 `*Policy` 接下来如何被 `nginx/config/policies` 下的生成器翻译成 NGINX 指令，是下一讲的主题。
- **u10 WAF 集成**：本讲多次提到 `WAFPolicy` 与 `processWAFPolicies`/bundle 拉取，完整的 WAF bundle 来源、轮询、下发链路在 WAF 单元深入。
- **u11 可观测性**：`ObservabilityPolicy`（v1alpha2）如何驱动 OpenTelemetry tracing 配置生成，与 NginxProxy 的 `Telemetry` 字段如何配合。
- **延伸阅读源码**：`internal/controller/state/graph/graph.go`（看 `BuildGraph` 的完整拓扑顺序）、`internal/controller/state/validation/validator.go`（看 `PolicyValidator` 三个方法的契约）、`internal/controller/state/store.go`（看策略专用的 `ngfPolicyObjectStore` 如何按 `PolicyKey` 存取）。
