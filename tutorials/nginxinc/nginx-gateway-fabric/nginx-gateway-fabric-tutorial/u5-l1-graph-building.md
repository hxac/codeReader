# Graph：把 Gateway API 资源变成内部模型

## 1. 本讲目标

上一讲（u4-l4）我们讲到，`ChangeProcessor.Process` 在把一批变更登记、并确认「确实有有效变更」之后，会输出一个 `graph.Graph`，然后整条「生成配置 → 下发」链路才开始工作。本讲就回答这个 `Graph` 是什么、怎么来的：

- 理解 **`Graph` 数据结构** 与构建入口 **`BuildGraph`**：它接收什么、输出什么、内部按什么顺序装配。
- 掌握 **GatewayClass** 的认领机制：NGF 怎么从集群里一堆 GatewayClass 里选出「自己负责的那一个」，又怎么把 GatewayClass 映射成图中的节点。
- 掌握 **Gateway** 的过滤与构建：只有 `gatewayClassName` 匹配的 Gateway 才会被纳入图。
- 理解图构建中的**校验（validation）与条件（conditions）收集**：错误不是靠 `panic` 或返回 `error` 抛出的，而是被「沉淀」进每个节点的 `Conditions` 字段里。

学完本讲，你应该能拿起任何一个 GatewayClass / Gateway，在源码里指出它「被接受 / 被拒绝」分别会在图的哪个字段、哪个 condition 上体现。

## 2. 前置知识

### 2.1 为什么需要 Graph 这一层

在 u4-l3 里，事件批次最终会被交给一个叫 `eventHandlerImpl` 的编排者，它会做这几件事：登记变更 → 构建图 → 生成 NGINX 配置 → 下发 → 更新状态。这里「构建图」就是本讲的主题。

直觉上，Kubernetes 里的资源是相互独立的 YAML 对象：一个 Gateway 自己不知道哪些 HTTPRoute 挂在它身上，一个 HTTPRoute 也不知道自己引用的 Service 是否存在。但生成 NGINX 配置、回写状态，都需要「连起来的、校验过的」全局视图。`Graph` 就是这张「连起来 + 校验过 + 错误被记录」的内部模型。`doc.go` 用一句话点明了它的三大职责：**校验资源、发现资源之间的连接、捕获校验/连接错误**。

### 2.2 几个关键术语

- **ClusterState**：构建图的「原料」，是从 `change_processor` 的对象存储（u4-l4）里拿到的、当前集群相关资源的快照，按资源类型装在一个个 map 里。
- **节点（node）**：图中对某类资源的「图内表示」，例如 `GatewayClass`、`Gateway`、`L7Route`。每个节点通常保留 `Source`（原始资源）+ 校验结果（`Valid`、`Conditions`）+ 连接关系。
- **Conditions（条件）**：Gateway API 标准的状态反馈机制，形如「类型=Accepted，状态=True，原因=...，消息=...」。NGF 在构建图时收集这些条件，之后再异步写回资源 `.status`（u8-l1 主题）。
- **校验（validation）**：分两类——**数据面无关校验**（hostname 是否合法、引用是否被 ReferenceGrant 授权等，与 NGINX 无关，本层处理）；**数据面相关校验**（某 HTTP 头取值是否合法，委托给 `Validator`，见 `doc.go` 注释）。

> 对 `Conditions` 还不熟悉的读者，可以先记住「一条 condition = 一个 `(Type, Status, Reason, Message)` 四元组」，它是 NGF 把图里发生的事情「翻译」给用户看的语言。完整条件体系在 u8-l1 详讲，本讲只用到与 GatewayClass / Gateway 相关的部分。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [internal/controller/state/graph/doc.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/doc.go) | 包级文档，一句话点明 graph 包「校验、连接、捕获错误」三大职责。 |
| [internal/controller/state/graph/graph.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go) | 定义 `ClusterState`、`Graph` 两个核心结构；定义唯一构建入口 `BuildGraph`；提供 `IsReferenced` 等「图是否引用某资源」的查询。 |
| [internal/controller/state/graph/gatewayclass.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gatewayclass.go) | GatewayClass 的认领（`processGatewayClasses`）、构建（`buildGatewayClass`）、校验（`validateGatewayClass`）、CRD 版本校验（`validateCRDVersions`）。 |
| [internal/controller/state/graph/gateway.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway.go) | Gateway 的过滤（`processGateways`）、构建（`buildGateways`）、校验（`validateGateway` / `validateGatewayRefs`）。 |
| [internal/controller/state/graph/validation.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/validation.go) | 数据面无关校验的工具函数（本讲以 `validateHostname` 为例）。 |
| [internal/controller/state/conditions/conditions.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go) | 所有 condition 的构造工厂（如 `NewGatewayClassResolvedRefs`、`NewGatewayClassInvalidParameters`），是「收集 conditions」的素材库。 |
| [internal/controller/state/change_processor.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go) | `Process` 在此调用 `graph.BuildGraph`，是连接 u4-l4 与本讲的桥梁。 |

---

## 4. 核心概念与源码讲解

### 4.1 Graph 数据结构与构建入口

#### 4.1.1 概念说明

构建图需要两样东西：**原料**（集群当前的资源快照）和**加工规则**（控制器名、要认领的 GatewayClass 名、校验器等）。`graph` 包用两个结构分别承载它们：

- **`ClusterState`**：原料。它把构建图所需的各类资源按 `types.NamespacedName`（命名空间 + 名字）装进一个个 map。
- **`Graph`**：成品。它把原料加工成「校验过、连起来」的内部模型，并且额外携带了「引用关系」（哪些 Secret / Namespace / ConfigMap 被引用了），这些引用关系在 u4-l4 的 `IsReferenced` 判断里会用到。

两者是「输入 vs 输出」的关系：`ClusterState` 进，`Graph` 出。

#### 4.1.2 核心流程

图构建的整体流程可以抽象为：

```text
ClusterState（原料快照）
        │
        ▼
  BuildGraph（唯一入口，按依赖拓扑顺序装配）
        │
        ├── 1. 认领 GatewayClass   → processGatewayClasses + buildGatewayClass
        ├── 2. 过滤 Gateway        → processGateways + buildGateways
        ├── 3. 解析 NginxProxy / ListenerSet / BackendTLSPolicy / 各类 Filter
        ├── 4. 构建 Route（HTTP/gRPC/TCP/...）并绑定到 Listener
        ├── 5. 收集引用关系（ReferencedServices / ReferencedSecrets / ...）
        ├── 6. 处理策略 CRD（policies，必须最后，依赖前面所有资源）
        └── 7. 组装 Graph 并返回
        │
        ▼
     *Graph（成品，含 Valid / Conditions / 连接关系）
```

一个关键设计原则是：**装配顺序由依赖关系决定**。比如策略（policy）必须最后处理，因为它要判断自己能否附着（attach）到图里已经存在的资源上——这一点在源码注释里写得很明确。

#### 4.1.3 源码精读

**输入：`ClusterState` 结构**

[internal/controller/state/graph/graph.go:33-57](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go#L33-L57) 定义了原料结构。可以看到它几乎是「一类资源一个 map」：

```go
type ClusterState struct {
    GatewayClasses map[types.NamespacedName]*gatewayv1.GatewayClass
    Gateways       map[types.NamespacedName]*gatewayv1.Gateway
    HTTPRoutes     map[types.NamespacedName]*gatewayv1.HTTPRoute
    // ... Services / Namespaces / Secrets / ConfigMaps ...
    NginxProxies          map[types.NamespacedName]*ngfAPIv1alpha2.NginxProxy
    NGFPolicies           map[PolicyKey]policies.Policy
    SnippetsFilters       map[types.NamespacedName]*ngfAPIv1alpha1.SnippetsFilter
    // ...
}
```

这些 map 就是 u4-l4 里 `objectStore` / `multiObjectStore` 持久化下来的资源，在 `Process` 阶段被「摊平」成 `ClusterState`。

**输出：`Graph` 结构**

[internal/controller/state/graph/graph.go:59-113](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go#L59-L113) 定义了成品结构。重点关注它的「分类」：

```go
type Graph struct {
    GatewayClass          *GatewayClass                     // 图内的 GatewayClass 节点
    Gateways              map[types.NamespacedName]*Gateway // 图内的 Gateway 节点
    IgnoredGatewayClasses map[...]...*gatewayv1.GatewayClass // 认领我但不归我管的 GC，单独存放
    Routes                map[RouteKey]*L7Route             // HTTP/gRPC 路由（u5-l2）
    L4Routes              map[L4RouteKey]*L4Route           // TCP/UDP/TLS 路由（u5-l2）
    // —— 下面是一组「引用关系」map ——
    ReferencedSecrets       map[...]...*secrets.Secret       // 被引用的 Secret
    ReferencedNamespaces    map[...]...*v1.Namespace         // 被引用的 Namespace
    ReferencedServices      map[...]...*ReferencedService    // 被引用的 Service（u5-l3）
    ReferencedNginxProxies  map[...]...*NginxProxy           // 被引用的 NginxProxy
    NGFPolicies             map[PolicyKey]*Policy            // 策略节点（u8-l2）
    // ... 更多引用关系 ...
}
```

注意 `GatewayClass` 是**单数指针**（一个控制器一次只认领一个 GatewayClass），而 `Gateways` 是 **map**（一个 GatewayClass 可以对应多个 Gateway）。这种「单 vs 多」的差异，正反映了 Gateway API 的层级关系。

**`ReferencedSecrets` 的特殊之处**：注释特别说明它「与其它 map 不同，因为它包含了集群里**不存在**的 Secret 的条目」。这是为了能回答「这个 Secret 是否被引用了」——即便是刚刚创建还不存在的 Secret。这个细节会和 u5-l3 的 `IsReferenced` 联动。

**唯一构建入口：`BuildGraph`**

[internal/controller/state/graph/graph.go:255-268](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go#L255-L268) 是函数签名。它的参数清单很长，但可以分成三组记忆：

1. **上下文与原料**：`ctx`、`state ClusterState`。
2. **身份与能力**：`controllerName`（如 `gateway.nginx.org/nginx-gateway-controller`）、`gcName`（要认领的 GatewayClass 名）、`featureFlags`（Plus / Experimental 开关）、`validators`（校验器集合）。
3. **运行期依赖**：`plusSecrets`、`wafFetcher`、`plmFetcher`、`previousWAFBundles` 等（与本讲关系不大，属于 Plus / WAF 子系统）。

**关键的早退逻辑**：函数第一行就先认领 GatewayClass，如果「配置的 GatewayClass 存在，但它引用的 controllerName 不是我」，就直接返回空 `Graph`：

```go
processedGwClasses, gcExists := processGatewayClasses(state.GatewayClasses, gcName, controllerName)
if gcExists && processedGwClasses.Winner == nil {
    // configured GatewayClass does not reference this controller
    return &Graph{}
}
```

[internal/controller/state/graph/graph.go:269-273](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go#L269-L273) 这几行。这是一个重要的短路：**我配置的 GatewayClass 如果不指向我，我就什么都不做**，返回一个空图。后续的 handler 看到空图就不会生成任何配置。

**`policies 必须最后处理`**：[internal/controller/state/graph/graph.go:383-393](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go#L383-L393) 的注释明确写道「policies must be processed last because they rely on the state of the other resources in the graph」。这是理解整个装配顺序的钥匙。

**连接 u4-l4：谁调用 `BuildGraph`**

[internal/controller/state/change_processor.go:360-386](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L360-L386) 是 `ChangeProcessorImpl.Process` 的实现。注意第 364 行的 `getAndResetClusterStateChanged()`——这正是 u4-l4 讲的 `changed` 把门：没有有效变更时 `Process` 直接返回 `nil`，根本不会调用 `BuildGraph`。只有真的需要重建时，才会把 `clusterState` 喂给 `BuildGraph`，并把结果存到 `c.latestGraph` 返回。

#### 4.1.4 代码实践

**实践目标**：在源码里建立 `ClusterState → BuildGraph → Graph` 的完整心智链路，并理解早退逻辑。

**操作步骤**：

1. 打开 [internal/controller/state/graph/graph.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go)，对照 `BuildGraph`（L255 起）的函数体，把它从上到下每一步 `processXxx` / `buildXxx` 调用列成一个有序清单。
2. 找到早退语句（L269-L273），确认它的语义：`gcExists==true` 但 `Winner==nil` 意味着「我配置的 GatewayClass 存在，但 controllerName 不是我」。
3. 打开 [internal/controller/state/change_processor.go:360-386](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L360-L386)，确认 `BuildGraph` 的入参 `c.clusterState` 来自 u4-l4 的对象存储。

**需要观察的现象**：

- `BuildGraph` 是一个**纯函数式**的构建：它不改 `ClusterState`，只读它、产出新 `Graph`。这意味着图的重建是无副作用的，重复调用安全。
- `Graph` 里大量字段是 `map`，但 `GatewayClass` 是单数——记住这个不对称。

**预期结果**：你能用自己的话画出 `BuildGraph` 的 7 步装配流程图，并解释「为什么策略要最后处理」。

**待本地验证**：上述为源码阅读型实践，无需运行命令即可完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `GatewayClass` 字段是指针而 `Gateways` 是 map？

**参考答案**：因为一个 NGF 实例只认领一个 GatewayClass（由 `--gatewayclass` flag 指定），所以是单数；但一个 GatewayClass 可以被任意多个 Gateway 通过 `spec.gatewayClassName` 引用，所以是集合。

**练习 2**：如果配置的 GatewayClass 不存在，`BuildGraph` 会返回空图吗？

**参考答案**：不会。看 [graph.go:269-273](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go#L269-L273)：返回空图的前提是 `gcExists && Winner == nil`，即「存在但不指向我」。若 GatewayClass 根本不存在，`gcExists` 为 `false`，条件不成立，会继续往下走（此时 `Winner` 也是 `nil`，后续的 `buildGatewayClass` 收到 `nil` 会返回 `nil`，详见 4.2）。

---

### 4.2 GatewayClass 的认领与映射

#### 4.2.1 概念说明

GatewayClass 是 Gateway API 的「顶层」资源，它的 `spec.controllerName` 字段声明「我这个类由哪个控制器实现」。集群里可能同时存在多个 GatewayClass，其中：

- 有的 `controllerName` 指向 NGF——这些是「候选」。
- 其中又只有一个的名字等于 NGF 启动时 `--gatewayclass` 指定的名字——这个才是 NGF 真正「认领」并为之工作的，叫 **Winner**。
- 其余 `controllerName` 指向 NGF 但名字不匹配的——这些叫 **Ignored**，NGF 要给它们写一条「我不支持你」的状态（u8-l1），所以也要记录下来。
- `controllerName` 指向别的控制器的——跟 NGF 无关，直接忽略。

本模块讲清楚 `processGatewayClasses`（认领/分类）与 `buildGatewayClass`（转成图内节点）。

#### 4.2.2 核心流程

```text
state.GatewayClasses（集群里所有 GatewayClass）
        │
        ▼  processGatewayClasses(gcName, controllerName)
        │
        ├── 命名 == gcName ?
        │     ├── 是 → gcExists=true
        │     │     └── controllerName 匹配 ? → 设为 Winner
        │     └── 否
        │           └── controllerName 匹配 ? → 放进 Ignored
        ▼
返回 (processedGatewayClasses{Winner, Ignored}, gcExists)
        │
        ▼  buildGatewayClass(Winner, nps, crdVersions, experimental)
        │     ├── 解析 parametersRef → 找 NginxProxy
        │     ├── validateGatewayClass(...) → 收集 conds / valid / bestEffort
        │     └── 组装 *GatewayClass 节点
```

#### 4.2.3 源码精读

**图内节点：`GatewayClass` 结构**

[internal/controller/state/graph/gatewayclass.go:30-45](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gatewayclass.go#L30-L45) 定义了图内的 GatewayClass 节点：

```go
type GatewayClass struct {
    Source                *v1.GatewayClass      // 原始资源
    NginxProxy            *NginxProxy           // parametersRef 指向的 NginxProxy
    Conditions            []conditions.Condition // 校验/状态条件
    Valid                 bool                  // 是否有效
    ExperimentalSupported bool                  // 实验特性是否支持
    BestEffort            bool                  // CRD 版本是否「尽力而为」
}
```

`Source` + `Valid` + `Conditions` 这组三元组是图中**所有节点**的共同范式：保留原料、记一个布尔有效位、记一组条件。后续 `Gateway`、`L7Route` 等都遵循这个范式。

**认领分类：`processGatewayClasses`**

[internal/controller/state/graph/gatewayclass.go:53-81](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gatewayclass.go#L53-L81) 是认领逻辑。核心是一个循环 + 两个条件判断：

```go
for _, gc := range gcs {
    if gc.Name == gcName {                 // 名字匹配我配置的 GC
        gcExists = true
        if string(gc.Spec.ControllerName) == controllerName {  // 且 controllerName 也是我
            processedGwClasses.Winner = gc
        }
    } else if string(gc.Spec.ControllerName) == controllerName { // 名字不匹配但指向我
        // ... 放进 Ignored
    }
}
```

这段逻辑清晰地分出三类：名字+controllerName 都匹配（Winner）、只 controllerName 匹配（Ignored）、都不匹配（直接跳过）。`gcExists` 这个布尔之所以单独返回，是为了让调用方区分「配置的 GC 不存在」和「配置的 GC 存在但不指向我」这两种不同情况（前者是用户还没创建，后者是配置错误）。

**转成节点：`buildGatewayClass`**

[internal/controller/state/graph/gatewayclass.go:83-111](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gatewayclass.go#L83-L111)。注意第一行的保护：

```go
func buildGatewayClass(gc *v1.GatewayClass, ...) *GatewayClass {
    if gc == nil {
        return nil    // Winner 为空时，图里就没有 GatewayClass 节点
    }
    // ... 解析 parametersRef → np，校验，组装节点 ...
}
```

如果 Winner 是 `nil`（即配置的 GC 不存在或不指向我），这里直接返回 `nil`，于是 `Graph.GatewayClass` 为 `nil`。这呼应了 4.1 练习 2：GC 不存在时不会返回空图，而是返回一个「GatewayClass 字段为 nil」的图。

**CRD 版本兼容性矩阵**

[internal/controller/state/graph/gatewayclass.go:20-28](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gatewayclass.go#L20-L28) 定义了 NGF 关心的 Gateway API CRD 列表（`gatewayCRDs` map）。校验时，[validateCRDVersions](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gatewayclass.go#L207-L237)（L207-L237）会读取这些 CRD 上的版本注解，与 `consts.BundleVersion`（NGF 编译时携带的推荐版本）比较，得到三种结果：

| 安装版本与推荐版本的关系 | 结果 | 对应 condition |
| --- | --- | --- |
| major 相同、minor 相同 | 完全支持 | 不加版本相关条件 |
| major 相同、minor 不同 | 尽力而为（`BestEffort=true`） | `NewGatewayClassSupportedVersionBestEffort` |
| major 不同 | 不支持（`valid=false`） | `NewGatewayClassUnsupportedVersion` |

这个判断解释了 u1-l1 里「版本矩阵」在代码里的落点：如果用户装的 Gateway API CRD 大版本与 NGF 不兼容，GatewayClass 会被标记为「不接受」。

#### 4.2.4 代码实践

**实践目标**：追踪一个 GatewayClass 被接受/拒绝的过程在图中如何体现。

**操作步骤**：

1. 假设有三个 GatewayClass：`nginx`（name 匹配 + controllerName 匹配）、`nginx-ignored`（name 不匹配 + controllerName 匹配）、`other-controller`（name 不匹配 + controllerName 不匹配）。NGF 启动参数 `--gatewayclass=nginx`。
2. 在 [processGatewayClasses](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gatewayclass.go#L53-L81) 里手工「走一遍」循环，判断每个 GC 落到哪个分支。
3. 对照 [validateCRDVersions](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gatewayclass.go#L207-L237)，假设集群装的 CRD 大版本与 NGF 推荐版本不同，确认 `GatewayClass.Valid=false` 且带上 `NewGatewayClassUnsupportedVersion` 条件。

**需要观察的现象**：

- `nginx` → `Winner`，最终成为 `Graph.GatewayClass`。
- `nginx-ignored` → `Ignored`，进入 `Graph.IgnoredGatewayClasses`，用于后续写「我不支持你」状态。
- `other-controller` → 被完全跳过，不出现在图里。

**预期结果**：你能填出下面这张表：

| GatewayClass | name 匹配? | controllerName 匹配? | 去向 |
| --- | --- | --- | --- |
| `nginx` | ✅ | ✅ | `Winner` → `Graph.GatewayClass` |
| `nginx-ignored` | ❌ | ✅ | `Graph.IgnoredGatewayClasses` |
| `other-controller` | ❌ | ❌ | 不进图 |

**待本地验证**：可运行 `go test ./internal/controller/state/graph/... -run TestBuildGraph -v` 观察测试用例如何断言 `Winner` / `Ignored` 的划分。

#### 4.2.5 小练习与答案

**练习 1**：`IgnoredGatewayClasses` 里的资源为什么不能像普通资源一样被丢弃？

**参考答案**：因为它们的 `controllerName` 指向 NGF，NGF 有责任告诉用户「我认领你但不支持你」（因为同名配置不匹配）。所以必须记录下来，供 u8-l1 的状态更新器写入 `Accepted=False` 之类的状态。注释里也写明了这一点。

**练习 2**：`buildGatewayClass` 在什么情况下返回 `nil`？

**参考答案**：当传入的 `gc`（即 Winner）为 `nil` 时返回 `nil`，对应「配置的 GatewayClass 不存在」或「存在但不指向我」两种场景。

---

### 4.3 Gateway 的过滤与构建

#### 4.3.1 概念说明

一个 GatewayClass 可以被多个 Gateway 引用（通过 `spec.gatewayClassName`）。但 NGF 只需要为「引用我认领的那个 GatewayClass」的 Gateway 工作。所以 Gateway 也要经历「过滤 → 构建」两步，和 GatewayClass 的「认领 → 构建」是对称的。

与 GatewayClass 不同的是，Gateway 的校验更复杂：它要校验监听器（Listener）、地址（Addresses）、基础设施引用（Infrastructure.parametersRef，指向 NginxProxy）、TLS 后端证书等。本模块先把 Gateway 整体的过滤与构建讲清楚，监听器的细节留给 u5-l2。

#### 4.3.2 核心流程

```text
state.Gateways
        │
        ▼  processGateways(gcName)  —— 按 gatewayClassName 过滤
        │   只保留 spec.gatewayClassName == gcName 的
        ▼  buildGateways(...)
        │   for 每个 Gateway:
        │     ├── 解析 Infrastructure.parametersRef → NginxProxy
        │     ├── 合并 GatewayClass 级与 Gateway 级 NginxProxy → EffectiveNginxProxy
        │     ├── validateGateway(...) → conds / valid / secretRef
        │     ├── valid ? 构建完整 Gateway（含 Listeners、ListenerFactory）
        │     │         : 只构建精简 Gateway（Valid=false，不建监听器）
        ▼
返回 map[NamespacedName]*Gateway
```

注意一个重要设计：**无效的 Gateway 也进图，只是结构更精简**。这保证后续依然能给它写状态（告诉用户哪里错了），而不是干脆丢弃。

#### 4.3.3 源码精读

**图内节点：`Gateway` 结构**

[internal/controller/state/graph/gateway.go:19-50](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway.go#L19-L50) 定义了图内的 Gateway 节点。它比 `GatewayClass` 字段多得多，因为 Gateway 是「承载一切」的核心：

```go
type Gateway struct {
    Source              *v1.Gateway            // 原始资源
    NginxProxy          *NginxProxy            // 本 Gateway 引用的 NginxProxy
    EffectiveNginxProxy *EffectiveNginxProxy   // 合并 GC 级 + GW 级后的有效配置
    SecretRef           *types.NamespacedName  // 后端 TLS 用的 Secret
    DeploymentName      types.NamespacedName   // 对应的数据面 Deployment 名（u9-l1）
    Listeners           []*Listener            // 监听器（u5-l2）
    Conditions          []conditions.Condition // 条件
    Valid               bool                   // 是否有效
    ListenerFactory     *listenerConfiguratorFactory // 造监听器的工厂
    // ...
}
```

`EffectiveNginxProxy` 是一个值得留意的设计：NginxProxy 配置可以同时存在于 GatewayClass（`spec.parametersRef`）和 Gateway（`spec.infrastructure.parametersRef`）两层，`EffectiveNginxProxy` 是两者合并后的「最终生效配置」。这与 u1-l4 讲过的「NginxProxy 是数据面参数」一脉相承。

**过滤：`processGateways`**

[internal/controller/state/graph/gateway.go:52-72](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway.go#L52-L72)。逻辑很简单：只保留 `gatewayClassName` 匹配的，否则 `continue`。注意末尾：如果一个匹配的都没有，返回 `nil`（而不是空 map），这方便上层用 `len(gws)==0` 判空。

**构建：`buildGateways`**

[internal/controller/state/graph/gateway.go:74-145](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway.go#L74-L145)。这是本模块的核心。对每个 Gateway，它做四件事：

1. **解析 NginxProxy**（L91-L94）：从 `Infrastructure.parametersRef` 找出本 Gateway 引用的 NginxProxy。
2. **合并有效配置**（L101）：`buildEffectiveNginxProxy(gcNp, np)` 把 GatewayClass 级与 Gateway 级的 NginxProxy 合并。
3. **校验**（L103）：`validateGateway(...)` 返回 `conds, valid, secretRefNsName`。
4. **按有效性分流构建**（L116-L141）：这是关键——

```go
if !valid {
    builtGateways[gwNsName] = &Gateway{
        Source: gw, Valid: false,
        // ... 只保留必要字段，不建 Listeners、不建 ListenerFactory ...
    }
} else {
    gateway := &Gateway{
        Source: gw, Valid: true,
        // ... 含 ListenerFactory ...
    }
    gateway.Listeners = buildListeners(...)  // 只有 valid 才构建监听器
}
```

也就是说，**无效的 Gateway 也会进图**（带着 `Valid=false` 和它的 `Conditions`），只是不带监听器。这样下游 handler 依然能识别它并写状态，而不会因为「构建失败」就丢失它。`DeploymentName` 的构造用了 `controller.CreateNginxResourceName(gw名, gatewayClassName)`，这个名字会被 u9-l1 的 provisioner 用来创建真实的数据面 Deployment。

#### 4.3.4 代码实践

**实践目标**：理解「无效 Gateway 也进图」这一设计，并验证它能被状态更新器看到。

**操作步骤**：

1. 打开 [buildGateways](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway.go#L74-L145)，对比 `valid` 与 `!valid` 两个分支构造的 `Gateway` 字段差异。
2. 思考：如果一个 Gateway 的 `gatewayClassName` 写错了（指向不存在的类），它会被 `processGateways` 直接过滤掉，**进不了图**，因此连「你的 GatewayClass 写错了」这种状态都无法回写。这是 Gateway API 设计的一个约束——只有被引用的 GatewayClass 对应的控制器，才有资格处理这个 Gateway。

**需要观察的现象**：

- 有效 Gateway：进图，带 Listeners + ListenerFactory，后续可生成配置。
- 无效 Gateway（如引用了无效 GatewayClass）：进图，`Valid=false`，带 Conditions，但不带 Listeners。

**预期结果**：能解释「为什么无效 Gateway 不能被丢弃」——因为它需要被写回 `.status.conditions` 告诉用户错误。

**待本地验证**：可在 `gateway_test.go` 里搜索 `Valid: false` 的测试用例，观察断言的 `Gateway` 结构里有哪些字段被保留、哪些为空。

#### 4.3.5 小练习与答案

**练习 1**：`processGateways` 在没有匹配 Gateway 时返回 `nil` 而非空 map，这样做有什么好处？

**参考答案**：让上层 `buildGateways`（L81-L83）能用 `if len(gws) == 0 { return nil }` 提前返回，且保证 `Graph.Gateways` 在「没有 Gateway」时是 `nil` 而非空 map，调用方判空更简单一致（直接判 `nil` 即可）。

**练习 2**：`DeploymentName` 字段由哪两部分拼接而成？它的用途是什么？

**参考答案**：由 Gateway 名 + gatewayClassName 经 `controller.CreateNginxResourceName` 拼接。它是这个 Gateway 对应的数据面 NGINX Deployment 的名字，会被 u9-l1 的 provisioner 用来创建/回收真实的数据面资源。

---

### 4.4 校验与 Conditions 收集

#### 4.4.1 概念说明

这是本讲最重要也最能体现 NGF 设计哲学的模块。在传统的「校验失败就返回 error」模式下，一个资源的某个字段错误会导致整个处理链中断。但 Kubernetes 控制器面对的是用户随时变更的 YAML，错误是常态。NGF 的做法是：

> **校验错误不中断构建，而是被「沉淀」进节点的 `Conditions` 字段，并据此设置 `Valid` 布尔位。下游据此决定「这个节点能不能参与生成配置」，但所有节点都会被构建出来。**

这样用户能同时看到所有错误，而不是一次只暴露一个。这个模式在本模块通过 `validateGatewayClass`、`validateGateway` 等函数体现。

#### 4.4.2 核心流程

校验在「构建」内部完成，可以看作每个 `buildXxx` 内部都嵌着一次 `validateXxx`：

```text
buildGatewayClass(gc, np, crdVersions)
        │
        ▼  validateGatewayClass(gc, np, crdVersions)
        │     ├── validateCRDVersions(...) → 版本条件
        │     ├── 无 parametersRef ? → 只带版本条件返回
        │     ├── validateGatewayClassParametersRef(...) → 参数引用条件
        │     ├── np == nil ?        → NewGatewayClassRefNotFound + InvalidParameters
        │     ├── !np.Valid ?        → NewGatewayClassRefInvalid + InvalidParameters
        │     └── 都OK               → NewGatewayClassResolvedRefs
        ▼
返回 (conds, valid, experimental, bestEffort)
```

校验函数有一个统一的返回契约：**返回 `([]Condition, valid bool, ...)`**，把「发生了什么」编码进 conditions，「还能不能用」编码进 valid。`buildXxx` 据此填充节点的 `Conditions` 和 `Valid`。

#### 4.4.3 源码精读

**GatewayClass 校验：`validateGatewayClass`**

[internal/controller/state/graph/gatewayclass.go:151-196](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gatewayclass.go#L151-L196) 体现了「分层校验 + 早退」的写法。注意它的多个 `return`：

```go
func validateGatewayClass(gc, npCfg, crdVersions) ([]conditions.Condition, bool, bool, bool) {
    conds := validateCRDVersions(crdVersions)         // 第一步：CRD 版本
    if gc.Spec.ParametersRef == nil {
        return conds, versionsValid, ...              // 没有 parametersRef，只看版本
    }
    refConds := validateGatewayClassParametersRef(...) // 第二步：parametersRef 格式
    if len(refConds) > 0 {
        return conds+refConds, ...                    // 格式错，早退
    }
    if npCfg == nil {
        return ...NewGatewayClassRefNotFound...       // 引用的 NginxProxy 不存在
    }
    if !npCfg.Valid {
        return ...NewGatewayClassRefInvalid...        // 引用的 NginxProxy 无效
    }
    return ...NewGatewayClassResolvedRefs...          // 全部通过
}
```

每一层失败都「带着到目前为止收集到的 conditions」提前返回，保证用户能看到错误的具体层级。

**条件的构造工厂**

conditions 不是凭空构造的字符串，而是集中在 [conditions.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go) 里。以几个关键工厂为例：

- [NewGatewayClassResolvedRefs](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L923-L932)（L923-L932）：parametersRef 解析成功，`ResolvedRefs=True`。
- [NewGatewayClassInvalidParameters](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L956-L966)（L956-L966）：参数无效，但**注意 `Accepted` 仍是 `True`**——注释解释这是为了「防止一个参数更新错误就把整棵配置树废掉」。这是一个很重要的容错设计。
- [NewGatewayClassUnsupportedVersion](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L342-L365)（L342-L365）：CRD 版本不兼容，`Accepted=False` 且 `SupportedVersion=False`，这是少数会让 `valid=false` 的情况。

理解这些工厂的 `(Type, Status, Reason, Message)` 四元组，是理解「图里的错误如何变成用户可见状态」的桥梁——这部分在 u8-l1 会系统讲解。

**Gateway 校验：`validateGateway`**

[internal/controller/state/graph/gateway.go:267-303](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway.go#L267-L303) 展示了「有效性与警告分离」的设计：

```go
func validateGateway(gw, gc, npCfg, ...) ([]conditions.Condition, bool, *types.NamespacedName) {
    // 1. 前置硬校验：GC 不存在/无效 → 直接 invalid
    if gc == nil { conds = append(conds, NewGatewayInvalid("The GatewayClass doesn't exist")...) }
    else if !gc.Valid { conds = append(conds, NewGatewayInvalid("The GatewayClass is invalid")...) }

    // 2. 地址校验：非 IPAddress → UnsupportedAddress（也会让 invalid）
    for _, address := range gw.Spec.Addresses { ... }

    valid := len(conds) == 0   // ← 关键：在「警告类」校验之前就确定 valid

    // 3. 不支持字段的警告（不影响 valid）
    conds = append(conds, validateUnsupportedGatewayFields(gw)...)

    // 4. 引用类校验
    refsConds, secretRefNsName := validateGatewayRefs(gw, npCfg, resourceResolver, refGrantResolver)
    conds = append(conds, refsConds...)

    return conds, valid, secretRefNsName
}
```

[internal/controller/state/graph/gateway.go:293](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway.go#L293) 这一行 `valid := len(conds) == 0` 的位置非常关键：它在「警告类条件」收集之前就锁定了 `valid`。这意味着「使用了不支持的字段」（步骤 3）只产生警告 condition，**不会让 Gateway 变成无效**。这是一种「能跑就尽量跑」的渐进式兼容策略。

**引用校验：`validateGatewayRefs`**

[internal/controller/state/graph/gateway.go:147-190](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway.go#L147-L190) 校验两类引用：parametersRef（指向 NginxProxy）和 TLS 后端证书。它内部还会调用 `refGrantResolver.refAllowed(...)`（L257）做跨命名空间的 ReferenceGrant 授权检查——这就是 u5-l3 后端解析与 ReferenceGrant 在 Gateway 层的雏形。

**数据面无关校验工具：`validateHostname`**

[internal/controller/state/graph/validation.go:10-31](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/validation.go#L10-L31) 是 `doc.go` 里说的「数据面无关校验」的一个典型例子。它复用 K8s 的 `validation.IsDNS1123Subdomain` / `IsWildcardDNS1123Subdomain` 来校验 hostname：

```go
func validateHostname(hostname string) error {
    if hostname == "" { return errors.New("cannot be empty string") }
    if strings.HasPrefix(hostname, "*.") {        // 通配符主机名
        return joinErrors(validation.IsWildcardDNS1123Subdomain(hostname))
    }
    return joinErrors(validation.IsDNS1123Subdomain(hostname)) // 普通主机名
}
```

这种「复用 K8s 既有校验规则 + 区分通配符」的写法，在监听器、路由的 hostname 校验里会反复出现（u5-l2）。注意本文件只是一个工具函数集合，真正的「校验编排」还是在各 `validateXxx` 里完成。

#### 4.4.4 代码实践

**实践目标**：亲手追踪一个 GatewayClass 从「校验失败」到「条件被收集」的全过程。

**操作步骤**：

1. 构造场景：一个 GatewayClass `nginx`，其 `spec.parametersRef` 指向一个不存在的 NginxProxy（名为 `np-not-exist`）。
2. 在 [validateGatewayClass](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gatewayclass.go#L151-L196) 里走一遍：CRD 版本假设 OK（不加版本条件）→ parametersRef 格式 OK → `npCfg == nil`（因为 NginxProxy 不存在）→ 命中 [L174-L183](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gatewayclass.go#L174-L183)，返回 `NewGatewayClassRefNotFound` + `NewGatewayClassInvalidParameters`。
3. 对照 [NewGatewayClassInvalidParameters](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L956-L966)，确认它的 `Accepted=True`——这意味着即便参数错了，GatewayClass 仍被接受，只是参数被忽略。

**需要观察的现象**：

- 这个 GatewayClass 进图时 `Valid` 是多少？（提示：看 `validateGatewayClass` 返回的 `valid`，此处 `versionsValid` 为 true，所以 `valid=true`，但带上了「参数找不到」的条件。）
- 用户最终会在 `kubectl get gatewayclass nginx -o yaml` 的 `.status.conditions` 里看到什么？（提示：`Accepted=True, Reason=InvalidParameters` 和 `ResolvedRefs=False, Reason=ParamsRefNotFound`。）

**预期结果**：你能复现这张「场景 → 条件」对照表：

| 场景 | 触发的 condition | Valid |
| --- | --- | --- |
| CRD 大版本不兼容 | `NewGatewayClassUnsupportedVersion` | false |
| 无 parametersRef | （只看版本条件） | true |
| parametersRef 指向不存在的 NginxProxy | `NewGatewayClassRefNotFound` + `NewGatewayClassInvalidParameters` | true（参数被忽略，但仍接受） |
| parametersRef 指向无效的 NginxProxy | `NewGatewayClassRefInvalid` + `NewGatewayClassInvalidParameters` | true |
| 全部正确 | `NewGatewayClassResolvedRefs` | true |

**待本地验证**：可运行 `go test ./internal/controller/state/graph/... -run TestBuildGatewayClass -v`（gatewayclass_test.go 里的测试）对照上表。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `NewGatewayClassInvalidParameters` 里 `Accepted` 要保持 `True`？

**参考答案**：注释明确说明：为了「防止因 parametersRef 更新到一个无效值就废掉整棵配置树」。即宁可忽略无效参数、用默认配置继续服务，也不要让一个参数错误导致整个 GatewayClass 不可用。这是一种「降级而非停服」的容错策略。

**练习 2**：在 `validateGateway` 里，`valid` 的计算为什么要在「不支持字段警告」之前？

**参考答案**：[L293](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway.go#L293) 在收集警告条件之前就计算 `valid`，目的是让「使用了不支持的字段」只产生警告 condition、不影响 `Valid`，从而让 Gateway 仍能生成配置（只是该字段被忽略）。这是一种渐进兼容策略。

**练习 3**：`validateHostname` 为什么要把通配符（`*.` 开头）单独处理？

**参考答案**：因为通配符主机名（如 `*.example.com`）的校验规则与普通主机名不同——它允许以 `*.` 开头。K8s 提供了专门的 `IsWildcardDNS1123Subdomain` 校验函数，所以要先判断前缀再选对应规则。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「从 ClusterState 到 Graph」的全链路追踪。

**场景**：集群里有如下资源：

- 1 个 GatewayClass `nginx`（controllerName 指向 NGF，parametersRef 指向一个**有效**的 NginxProxy `nginx-proxy`）。
- 1 个 GatewayClass `nginx-other`（controllerName 也指向 NGF，但名字不是启动配置的）。
- 1 个 Gateway `gw-1`（`gatewayClassName: nginx`，引用了有效的 NginxProxy）。
- 1 个 Gateway `gw-bad`（`gatewayClassName: nginx`，但 Infrastructure.parametersRef 指向不存在的 NginxProxy）。
- 1 个 GatewayClass 引用的 Gateway API CRD 大版本与 NGF 推荐版本一致。

**任务**：

1. 在源码里追踪 `BuildGraph` 的执行：分别走到 [processGatewayClasses](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gatewayclass.go#L53-L81)、[buildGatewayClass](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gatewayclass.go#L83-L111)、[processGateways](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway.go#L52-L72)、[buildGateways](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway.go#L74-L145) 这四步。
2. 画出最终的 `Graph` 结构草图，标注每个节点的 `Valid` 与关键 `Conditions`。
3. 回答：`gw-bad` 会不会有 Listeners？它会不会进图？用户能在它的 `.status.conditions` 里看到什么？

**参考结论**：

- `nginx` 成为 `Graph.GatewayClass`，`Valid=true`，带 `NewGatewayClassResolvedRefs`。
- `nginx-other` 进入 `Graph.IgnoredGatewayClasses`。
- `gw-1` 进图，`Valid=true`，带 Listeners + ListenerFactory。
- `gw-bad` 进图但 `Valid=false`，**不带 Listeners**（因为 `buildGateways` 的 `!valid` 分支不构建监听器）；它的 Conditions 里会有 `NewGatewayInvalidParameters`（parametersRef 指向的 NginxProxy 不存在），用户能在 `.status.conditions` 看到这条错误。

## 6. 本讲小结

- `Graph` 是 NGF 把集群资源「校验 + 连接 + 记录错误」后的内部模型；`ClusterState` 是原料，`BuildGraph` 是唯一入口，按依赖拓扑顺序装配。
- 图内所有节点遵循 `Source` + `Valid` + `Conditions` 的三元组范式；错误不靠 `panic`/`error` 抛出，而是沉淀进 `Conditions`，并由 `Valid` 布尔位决定能否参与生成配置。
- GatewayClass 通过 `processGatewayClasses` 分出 Winner（认领）/ Ignored（认领但不归我管）/ 跳过三类；只有名字+controllerName 都匹配的才成为 `Graph.GatewayClass`。
- Gateway 通过 `processGateways` 按 `gatewayClassName` 过滤；**无效的 Gateway 也进图**（`Valid=false`、不带监听器），保证错误状态能被回写。
- 校验采用「分层 + 早退 + 渐进兼容」策略：CRD 版本、parametersRef、引用授权分层校验；不支持字段只产生警告不影响 `Valid`；参数错误时保持 `Accepted=True` 以降级而非停服。
- 本讲止于 `BuildGraph` 输出的 `Graph.GatewayClass` 与 `Graph.Gateways`；路由、监听器、后端解析、最终配置表示分别由 u5-l2、u5-l3、u5-l4 承接。

## 7. 下一步学习建议

- **u5-l2 路由与监听器**：本讲的 Gateway 节点里有 `Listeners []*Listener` 字段，但监听器如何构建、HTTPRoute 如何绑定到监听器，是下一讲的主题。重点读 `gateway_listener.go`、`route_common.go`。
- **u5-l3 后端解析**：本讲多次提到 `refGrantResolver`、`ReferencedServices`，后端（BackendRef → Service → Endpoints）的解析与 ReferenceGrant 授权是 u5-l3 的核心。
- **u5-l4 dataplane.Configuration**：本讲的 `Graph` 还不能直接生成 NGINX 配置，中间还有一层 `dataplane.Configuration`，它是图的「最终配置表示」，交给 u6 的配置生成器。
- **u8-l1 条件与状态更新**：本讲收集的 `Conditions` 最终如何异步写回资源 `.status`，由 u8-l1 的 status updater/queue 体系完成，建议结合本讲一起读 `conditions.go`。
