# 后端解析：Service、BackendRef、ReferenceGrant

## 1. 本讲目标

本讲承接 [u5-l2（路由与监听器）](u5-l2-routes-and-listeners.md)。在上一讲里，一条 HTTPRoute 已经被「认领」并绑定到了某个 Listener，它的 `backendRefs` 也被原样放进了 `RouteRule`。但「放进去」并不等于「能用」——NGF 还必须回答三个问题：**这个后端引用合法吗？它指向的 Service 在不在、端口对不对？跨命名空间引用被授权了吗？** 本讲就专门解决这三个问题。

读完本讲，你应当能够：

- 说清「后端解析」在 NGF 里被拆成了**两个阶段**：图（Graph）阶段做 `BackendRef → Service` 的校验与解析，数据面配置（dataplane.Configuration）阶段才做 `Service → Endpoints` 的真正端点解析；理解为什么这样拆。
- 追踪一条 `backendRefs` 条目从原始 YAML 字段，经 `createBackendRef` → `getPortFromRef` → `validateBackendRef`，最终变成内部模型 `BackendRef` 的完整源码路径，并说清每一步失败会沉淀成哪一种 Condition。
- 理解 `ServiceResolver` 如何用 EndpointSlice 的字段索引（field index）把 `Service + ServicePort` 解析成一组 `Endpoint`，以及 ready 过滤、端口名匹配、去重这些细节。
- 掌握 `ReferenceGrant` 的跨命名空间授权机制：它如何被「拍平」成一个集合、如何用「精确名 + 通配整个命名空间」两档查询判断引用是否被允许，以及缺少授权时图如何标记 `ResolvedRefs=False / RefNotPermitted`。

---

## 2. 前置知识

在进入源码前，先用最直白的话建立几个直觉。

**什么是 BackendRef？**

Gateway API 里，一条 Route 规则通过 `backendRefs` 声明「匹配到的流量转发给谁」。它的核心字段是 `group`/`kind`/`name`/`namespace`/`port`/`weight`。其中 `kind` 几乎总是 `Service`（NGF 也支持 `InferencePool`，见 [u12-l1](u12-l1-inference-extension.md)），`name` 是 Service 的名字，`port` 是 Service 的端口号。一个最简单的例子：

```yaml
rules:
  - backendRefs:
      - name: coffee-svc
        port: 80
```

它的意思是「把流量转发到名为 `coffee-svc` 的 Service 的 80 端口」。注意 `backendRefs` 引用的是 **Service**，而不是 Service 后面真正的 Pod。Service 只是一个「逻辑后端」，真正处理流量的是 Pod，而 Pod 的地址清单存放在 **EndpointSlice** 里。

**为什么解析要分两个阶段？**

因为「Service 在不在、端口对不对」属于**配置校验**（决定这条路由有没有资格进 NGINX 配置），而「现在有哪些 Pod 副本、它们是不是 ready」属于**运行期数据**（这些地址会随 Pod 扩缩容实时变化）。前者在构建图（Graph）时就要定夺，后者必须在生成最终数据面配置的那一刻才读取，否则图会因为任何一个 Pod 变动而频繁重建。

所以 NGF 的设计是：

| 阶段 | 做什么 | 关键产物 | 在哪个包 |
|---|---|---|---|
| 图阶段（Graph） | 校验 `backendRef` 合法性、查 Service 拿到 `ServicePort`、查 ReferenceGrant 授权 | 内部模型 `BackendRef{SvcNsName, ServicePort, Valid, ...}` | `internal/controller/state/graph/` |
| 配置阶段（dataplane.Configuration） | 用 `ServicePort` 反查 EndpointSlice，得到真正的 Pod 地址列表 | `[]resolver.Endpoint` | `internal/controller/state/resolver/` |

本讲按这条主线展开：先讲图阶段的 `BackendRef` 解析（4.1），再讲配置阶段的 `ServiceResolver`（4.2），最后讲贯穿图阶段的授权机制 `ReferenceGrant`（4.3）。

**ReferenceGrant 是什么？**

Gateway API 出于安全考虑，**默认禁止跨命名空间引用**：一条 HTTPRoute 在 `ns-a` 命名空间，就不能随便把流量转发到 `ns-b` 命名空间的 Service。要打通，必须在**目标命名空间**（即 Service 所在的 `ns-b`）里放一个 `ReferenceGrant`，显式声明「我允许 `ns-a` 里的 HTTPRoute 引用我这里的 Service」。这就像门禁授权：资源主人在自己家里写一张「允许 X 进来」的白名单。本讲 4.3 会讲 NGF 如何把所有白名单拍平、用集合做查询。

**条件（Conditions）回顾**

如同 [u5-l1](u5-l1-graph-building.md) 所述，NGF 的校验不靠抛 error，而是把问题沉淀进节点 `Conditions`。本讲你会反复看到后端引用的所有错误都被写成同一种 Condition 类型 `ResolvedRefs=False`，但 `Reason` 各不相同（`RefNotPermitted`、`BackendNotFound`、`InvalidKind`、`UnsupportedValue`、`UnsupportedProtocol`）——这就是上一讲提到的「四元组条件」机制在后端解析上的具体应用。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|---|---|
| [backend_refs.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_refs.go) | **图阶段的核心。** 定义内部 `BackendRef` 结构、`createBackendRef` 解析入口、`getPortFromRef`（查 Service 拿端口）、`validateBackendRef`/`validateBackendRefHTTPRoute`（校验 + ReferenceGrant 检查）。 |
| [reference_grant.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/reference_grant.go) | `referenceGrantResolver`：把所有 `ReferenceGrant` 拍平成一个 `allowedReference` 集合，并提供 `refAllowed` 查询。 |
| [service.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/service.go) | `ReferencedService`：图阶段对「被引用的 Service」的轻量记录（记录哪些 Gateway 间接用到它、是不是 ExternalName），**不含 Pod 地址**。 |
| [resolver.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/resolver/resolver.go) | **配置阶段的核心。** `ServiceResolver` 接口与实现：用 EndpointSlice 字段索引把 `Service + ServicePort` 解析成 `[]Endpoint`。 |
| [endpointslice.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/index/endpointslice.go) | EndpointSlice 的 field index 定义（`k8sServiceName`），是 `ServiceResolver` 快速查找的索引基础（呼应 [u4-l1](u4-l1-framework-controller-and-filtering.md) 讲过的 field index）。 |
| [configuration.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go) | 调用 `ServiceResolver.Resolve` 的地方（`resolveUpstreamEndpoints`），是图阶段与配置阶段的「接缝点」。 |

---

## 4. 核心概念与源码讲解

### 4.1 BackendRef 解析：从 RouteBackendRef 到内部 BackendRef

#### 4.1.1 概念说明

Gateway API 原生的 `BackendRef`（`sigs.k8s.io/gateway-api/apis/v1.BackendRef`）只是几个字段的组合，不带任何解析结果，也不带校验结论。NGF 在图阶段要把它加工成**内部模型 `BackendRef`**——这个内部模型不仅记录了「指向哪个 Service 的哪个端口」，还记录了「这次解析是否成功（`Valid`）」「关联的 BackendTLSPolicy」「SessionPersistence 配置」「是否是镜像后端/外部认证后端/InferencePool」等图后续阶段需要的信息。

内部 `BackendRef` 结构定义如下，注意它刻意把「校验结论」和「解析产物」放在一起：

[backend_refs.go:25-52](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_refs.go#L25-L52) —— 内部 `BackendRef` 结构。关键字段：`SvcNsName`（解析出的 Service 命名空间名）、`ServicePort`（从 Service 取到的端口对象）、`Valid`（解析是否成功，`false` 时数据面不会为它生成配置）、`Weight`、`BackendTLSPolicy`、`IsMirrorBackend`/`IsExternalAuthBackend`/`IsInferencePool`（标记这条后端的特殊用途）。

#### 4.1.2 核心流程

图阶段的后端解析入口是 `addBackendRefsToRouteRules`，它遍历每条 L7 路由的每条规则，逐个把原始 `RouteBackendRef` 解析成内部 `BackendRef`。主流程是：

```text
addBackendRefsToRouteRules(routes)          // graph.go 总装时调用
  └─ 遍历每条 route
       └─ addBackendRefsToRules(route)      // 遍历 route.Spec.Rules
            └─ 对每条 rule 的每个 RouteBackendRef:
                 ├─ (若 InferencePool 伪装成 Service) resolveInferencePoolRef 补全端口
                 ├─ createBackendRef(ref, ...)        // ★ 解析入口
                 │     ├─ 算 weight（非法 weight 置 0）
                 │     ├─ validateRouteBackendRef(...) // 校验 group/kind/port/weight + ReferenceGrant
                 │     │     └─ 校验失败 → 返回 invalid BackendRef + Condition
                 │     ├─ getPortFromRef(...)          // 查 Service + 匹配端口
                 │     │     └─ Service 不存在/端口不匹配 → BackendNotFound Condition
                 │     ├─ (若 ExternalName Service) 校验 DNS resolver 配置
                 │     ├─ findBackendTLSPolicyForService(...)  // 关联 BackendTLSPolicy
                 │     └─ validateRouteBackendRefAppProtocol(...) // 校验 appProtocol 兼容性
                 └─ 把解析出的 BackendRef 收集进 rule.BackendRefs
```

有四个关键设计点值得记住：

1. **即便引用非法，也要生成一个 `BackendRef`**。注释里写得很直白：「Data plane will handle invalid ref by responding with 500. Because of that, we always need to add a BackendRef to group.Backends, even if the ref is invalid.」也就是说，非法后端不是被丢弃，而是以 `Valid=false` 进入分组，最终数据面会返回 500——这样能保证「配置生成」和「状态回写」的视图一致。

2. **`Valid=false` 的 BackendRef 不参与配置生成**。结构注释明确：「No configuration should be generated for an invalid BackendRef.」校验结论通过 `Valid` 字段传递到下游。

3. **weight 始终被计算**，即使引用非法。非法 weight 会被置为 0（拿不到流量），这是把「流量权重」与「引用合法性」解耦的处理。

4. **多 BackendRef 的 BackendTLSPolicy 一致性校验**。如果一条规则有多个后端，它们引用的 BackendTLSPolicy 必须一致（同样的 CA、hostname），否则整组后端被标记为非法——这是 [u12-l3](u12-l3-tls-and-certificates.md) 的伏笔。

#### 4.1.3 源码精读

**解析入口 `addBackendRefsToRules`** —— 遍历规则，跳过无效匹配/无效过滤器/零后端的规则（零后端是允许的，比如带 redirect 过滤器的规则），然后逐个解析：

[backend_refs.go:88-157](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_refs.go#L88-L157) —— 注意第 99-110 行的三道「跳过闸门」：规则匹配无效（`!rule.ValidMatches`）、规则过滤器无效（`!rule.Filters.Valid`）、零 backendRef（`len == 0`，对应纯 redirect 场景）。这三道闸门保证只有「值得解析后端」的规则才进入解析。

**核心解析函数 `createBackendRef`** —— 这是本模块最长的函数，按「算 weight → 校验引用 → 查端口 → ExternalName 校验 → 关联 BackendTLSPolicy → 校验 appProtocol」顺序推进，每一步失败都构造一个 `Valid=false` 的 `BackendRef` 并返回对应 Condition：

[backend_refs.go:252-403](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_refs.go#L252-L403) —— 重点看三段：
- 第 273-293 行：先调 `validateRouteBackendRef` 做整体校验，失败则返回带 `Weight` 但 `Valid=false` 的 BackendRef（呼应设计点 1、3）。
- 第 300 行：`getPortFromRef` 查 Service 与端口，失败返回 `BackendNotFound` Condition。
- 第 388-402 行：全部通过，返回 `Valid=true` 且带 `BackendTLSPolicy`、`SessionPersistence` 的完整 BackendRef。

**查 Service 拿端口 `getPortFromRef`** —— 这一步把「BackendRef 的 port 字段」落实成「Service 上真实的 ServicePort 对象」：

[backend_refs.go:519-537](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_refs.go#L519-L537) —— 两种失败：Service 在 `services` map 里不存在（返回 `field.NotFound`），或 Service 上没有匹配端口（`getServicePort` 返回错误）。注意它查的是从 ChangeProcessor 传进来的 `services` 快照（见 [u4-l4](u4-l4-change-processor-and-store.md) 的 ClusterState），而不是实时去 API server 查——这是图阶段「基于快照」的体现。

**校验函数 `validateBackendRef`** —— 这是「合法性 + 授权」的总闸，按 group → kind → ReferenceGrant → port → weight 顺序逐项检查，**任何一项失败立即返回**：

[backend_refs.go:574-620](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_refs.go#L574-L620) —— 重点看第 594-603 行的 **ReferenceGrant 检查**：只有当 `ref.Namespace` 不为空且与路由命名空间不同时（即跨命名空间引用），才调用 `refGrantResolver(toService(refNsName))` 询问授权；未被授权则返回 `NewRouteBackendRefRefNotPermitted`。同命名空间引用天然放行，无需 ReferenceGrant。本函数服务于 L4 路由（TLS/TCP/UDP）。

HTTP/GRPC 路由走更严格的孪生函数 `validateBackendRefHTTPRoute`，它额外支持 `InferencePool` 后端，并据此选择查 `toService` 还是 `toInferencePool` 授权：

[backend_refs.go:636-692](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_refs.go#L636-L692) —— 第 654-674 行的 `switch` 把后端分成「普通 Service」「InferencePool（显式 kind）」「InferencePool（伪装成 headless Service）」三种授权查询路径，授权对象分别是 `toService` 与 `toInferencePool`（见 4.3）。

> **L4 路由的后端解析**复用同一个 `validateBackendRef`，只是入口不同：TCP/UDP 路由由 `validateBackendRefL4RouteMulti` 驱动（支持多 BackendRef 加权，是 [u6-l4](u6-l4-stream-tcp-udp-config.md) stream upstream 的基础），见 [route_common.go:1722-1791](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L1722-L1791)。它和 L7 的差别在于：不做 appProtocol 兼容性校验（TCP/UDP 与应用层协议无关），且支持多后端加权。

#### 4.1.4 代码实践

**实践目标**：通过阅读单元测试，验证「Service 端口不匹配」与「Service 不存在」两种情况下，`getPortFromRef` 分别返回什么样的 Condition。

**操作步骤**：

1. 打开 [backend_refs_test.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_refs_test.go)，搜索 `RefNotPermitted`（如第 244 行）与 `RefBackendNotFound` 的测试用例。
2. 关注测试里构造的 `refGrantResolver`（如 `alwaysFalseRefGrantResolver`）和 `services` map 输入，理解每个用例「喂」给 `createBackendRef` 什么样的输入、期望得到什么 `expectedCondition`。
3. 对照 [conditions.go:518-582](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L518-L582)，把每类后端错误 Condition 的 `Type`/`Status`/`Reason` 填进下表。

**需要观察的现象**：所有后端错误的 `Type` 都是 `ResolvedRefs`、`Status` 都是 `False`，只有 `Reason` 和 `Message` 不同。

**预期结果**（你可对照填表）：

| 错误场景 | Condition 构造函数 | Reason |
|---|---|---|
| group/kind 不支持 | `NewRouteBackendRefInvalidKind` | `InvalidKind` |
| 跨命名空间未被授权 | `NewRouteBackendRefRefNotPermitted` | `RefNotPermitted` |
| Service 不存在 / 端口不匹配 | `NewRouteBackendRefRefBackendNotFound` | `BackendNotFound` |
| port 为 nil / weight 非法 | `NewRouteBackendRefUnsupportedValue` | `BackendRefUnsupportedValue` |
| appProtocol 与路由类型不兼容 | `NewRouteBackendRefUnsupportedProtocol` | `UnsupportedProtocol` |

（上述 Reason 取自 [conditions.go:518-582](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L518-L582)，可直接核对。）

#### 4.1.5 小练习与答案

**练习 1**：假设一条 HTTPRoute 在 `default` 命名空间，其 `backendRefs` 指向 `tea-svc`（同在 `default`）的 80 端口，但集群里根本没有 `tea-svc` 这个 Service。这条路由最终会带哪条 Condition？数据面会怎样处理？

> **答案**：`getPortFromRef` 在 `services` map 里查不到 `default/tea-svc`，返回 `field.NotFound`；`createBackendRef` 据此生成 `NewRouteBackendRefRefBackendNotFound`（`ResolvedRefs=False / BackendNotFound`），同时仍生成一个 `Valid=false` 的 `BackendRef` 进入分组。数据面收到非法后端，匹配该规则的请求会返回 **500**。

**练习 2**：为什么 `createBackendRef` 在引用非法时仍要返回一个 `BackendRef`，而不是直接跳过？

> **答案**：因为数据面对非法引用的处理方式是「返回 500」而非「假装没有这条规则」。如果跳过，数据面配置里会缺这条后端，行为可能变成「404 或落到默认后端」，与「校验失败应返回 500」的预期不一致。始终生成 BackendRef（带 `Valid=false`）能保证「图的状态视图」与「数据面行为」保持一致，也让 `ResolvedRefs` 状态能被正确回写。

---

### 4.2 ServiceResolver：Service → Endpoints 的解析

#### 4.2.1 概念说明

图阶段只解析到「Service + ServicePort」这一层（`BackendRef.SvcNsName` + `BackendRef.ServicePort`），它**不包含任何 Pod 地址**。真正生成 NGINX upstream 时，NGF 需要的是一组具体的 `IP:Port`（Pod 地址）。把 `Service + ServicePort` 翻译成 Pod 地址清单的工作，交给 `ServiceResolver`，而且这件事发生在**数据面配置构建阶段**（`dataplane.Configuration`），不是图阶段。

为什么要延迟？因为 EndpointSlice（Kubernetes 存放 Pod 地址的资源）变化非常频繁——每一次 Pod 扩缩容、重启、ready 状态翻转都会改 EndpointSlice。如果把端点解析放进图阶段，图就会随每次 Pod 变动而重建，触发昂贵的全量重配（reload）。把端点解析下沉到「生成 NGINX 配置的那一刻」才读取，能避免无谓的图重建——这正是 [u4-l4](u4-l4-change-processor-and-store.md) 提到「EndpointSlice 是唯一不被 ChangeProcessor 持久化的类型」的根本原因。

> **术语：EndpointSlice**。Kubernetes 用 `discovery.k8s.io/v1.EndpointSlice` 存放「Service 后面现在有哪些 Pod、它们的地址、是否 ready」。一个 Service 通常对应多个 EndpointSlice（按地址类型 IPv4/IPv6/FQDN、按端口分片）。每个 EndpointSlice 上有一个标签 `kubernetes.io/service-name`，标明它属于哪个 Service——这是 `ServiceResolver` 快速查找的索引基础。

#### 4.2.2 核心流程

`ServiceResolver.Resolve` 的执行过程：

```text
Resolve(ctx, svcNsName, svcPort, allowedAddressType)
  ├─ 校验入参非空（空则 panic，编程错误才触发）
  ├─ 用 field index "k8sServiceName" 列出该 Service 的所有 EndpointSlice
  │     client.MatchingFields{index.KubernetesServiceNameIndexField: svcNsName.Name}
  │     + client.InNamespace(svcNsName.Namespace)
  ├─ 列表为空或出错 → 返回 error（"no endpoints found"）
  └─ resolveEndpoints(...)
       ├─ filterEndpointSliceList(...) // 过滤：忽略 FQDN、忽略地址类型不符、忽略端口不匹配的 slice
       ├─ 过滤后为空 → 返回 error（"no valid endpoints"）
       └─ 遍历每个 slice 的每个 endpoint:
            ├─ endpointReady(endpoint) == false → 忽略（记日志）
            ├─ findPort(slice.Ports, svcPort) → 拿到端点端口
            └─ 把每个 address + port 放进 endpointSet（map 去重）
          返回 []Endpoint
```

几个关键设计点：

1. **字段索引加速**。`Resolve` 不遍历集群里所有 EndpointSlice，而是用一个**字段索引** `k8sServiceName`（值为 Service 名）直接命中目标 Service 的所有 slice。这个索引在缓存层（controller-runtime cache）上由 `ServiceNameIndexFunc` 建立——呼应 [u4-l1](u4-l1-framework-controller-and-filtering.md) 讲过的「field index 不是过滤器而是缓存上的加速索引」。

2. **三层过滤**：忽略 FQDN 类型 slice（ExternalName 走另一条路径，见下文）、忽略地址类型不在 `allowedAddressType` 里的 slice、忽略端口不匹配的 slice。

3. **ready 才算数**。`endpointReady` 要求 `Conditions.Ready != nil && *Ready == true`，未就绪（包括 `nil` 条件）的端点被丢弃，只记一行 V(1) 日志。

4. **去重**。同一个端点可能出现在多个 slice 里，用 `map[Endpoint]struct{}` 去重；map 初始容量按「就绪端点预估数」预分配，避免反复扩容（注释里明确说这是性能优化）。

5. **端口匹配靠「名字」**。`findPort` 用 `svcPort.Name` 去 slice 的 `Ports` 里找同名端口。为什么是名字？因为把 Service 的 TargetPort 翻译成容器真实端口的活儿，由 Kubernetes 的 EndpointSlice 控制器干完了，NGF 只需按端口名对齐（详见 `findPort` 的注释）。

**ExternalName 的特例**：ExternalName 类型的 Service 没有真正的 Pod，只有一个外部 DNS 名。这种情况下不走 `ServiceResolver.Resolve`，而是在配置阶段直接构造一个 `Resolve=true` 的单端点，让 NGINX 在运行期做 DNS 解析。这部分逻辑在 [configuration.go:2571-2604](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L2571-L2604) 的 `resolveUpstreamEndpoints` 里，是图阶段 `BackendRef` 解析与配置阶段 `ServiceResolver` 的「接缝点」。而图阶段已经把 ExternalName 的外部名记录在 `ReferencedService.ExternalName` 里（见 4.1.3 的 service.go），供这里取用。

#### 4.2.3 源码精读

**ServiceResolver 接口与 Endpoint 结构**：

[resolver.go:25-49](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/resolver/resolver.go#L25-L49) —— `Resolve` 的签名：输入 `svcNsName` + `svcPort` + 允许的地址类型，输出 `[]Endpoint`。`Endpoint` 结构含 `Address`/`Port`/`IPv6`/`Resolve`（DNS 名需运行期解析）/`Weight`（L4 多后端加权用）。

**Resolve 主流程**：

[resolver.go:63-97](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/resolver/resolver.go#L63-L97) —— 第 70-73 行的入参校验用 `panic`（这是「编程错误」而非「用户错误」，故 panic 而非返回 error）；第 78-83 行用 `MatchingFields` + `InNamespace` 列出 EndpointSlice；第 85-87 行空列表即视为「无端点」返回 error。

**端点解析主体 `resolveEndpoints`**：

[resolver.go:126-169](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/resolver/resolver.go#L126-L169) —— 第 134 行先过滤 slice 列表；第 142 行用预估容量初始化去重集合；第 144-161 行遍历就绪端点，调 `findPort` 拿端口、把每个地址塞进集合。

**端口匹配 `findPort`**：

[resolver.go:229-243](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/resolver/resolver.go#L229-L243) —— 按 `svcPort.Name` 在 slice 的端口列表里找；若遇到 `Port == nil`（表示「所有端口都有效」）则用 `getDefaultPort` 兜底；找不到返回 0。

**字段索引定义**：

[endpointslice.go:10-40](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/index/endpointslice.go#L10-L40) —— `KubernetesServiceNameIndexField = "k8sServiceName"`，索引函数 `ServiceNameIndexFunc` 从 EndpointSlice 的 `kubernetes.io/service-name` 标签取值。这个索引在控制器注册时通过 `WithFieldIndices` 挂到 cache 上（见 [u4-l1](u4-l1-framework-controller-and-filtering.md)）。

#### 4.2.4 代码实践

**实践目标**：阅读 `ServiceResolver` 的单元测试，理解「未就绪端点被忽略」「跨 slice 端点去重」「端口不匹配被过滤」三种行为，并解释为何这些设计能保证 NGINX upstream 的正确性。

**操作步骤**：

1. 打开 [service_resolver_test.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/resolver/service_resolver_test.go)。
2. 关注测试构造的 fake EndpointSlice：第 42-55 行构造了一个 `Serving=true/Terminating=true`（即未 ready）的端点，第 58-62 行构造了一个 `Conditions` 为 nil（视为未 ready）的端点——这些都被期望「忽略」。第 98 行 `dupeAddresses` 含重复地址，验证去重。
3. 注意第 84 行测试 client 用 `WithIndex(... index.KubernetesServiceNameIndexField, index.ServiceNameIndexFunc)` 建立了与生产代码相同的索引，保证测试贴近真实行为。

**需要观察的现象**：未就绪的端点（包括 `nil` 条件）不会出现在返回的 `[]Endpoint` 里；重复地址只出现一次。

**预期结果**：在生产环境里，这保证了一个 Pod 在启动完成（ready）前不会被 NGINX 转发流量，避免 502；去重则避免 upstream 里出现重复 server 条目。（具体断言值需对照测试用例，**待本地验证**。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ServiceResolver.Resolve` 把 EndpointSlice 列表为空当作 error，而不是返回空端点列表？

> **答案**：因为「该 Service 没有任何 EndpointSlice」通常意味着配置出错（比如 Service 选不到 Pod、或 Service 名写错）。返回 error 能让上层把这条后端标记为不可用并回写 `BackendNotFound` 之类状态；若静默返回空列表，NGINX upstream 会生成一个空 upstream，匹配流量会直接 502，且控制面察觉不到问题。

**练习 2**：如果同一个 Pod 的 IP 同时出现在两个 EndpointSlice 里（某些 K8s 版本的已知行为），`resolveEndpoints` 会怎么处理？

> **答案**：用 `map[Endpoint]struct{}` 去重。因为 `Endpoint` 结构以 `Address+Port`（以及 `IPv6` 等）为键，相同的 `IP:Port` 只会在 map 里占一个键，最终 `[]Endpoint` 里不会重复。这正是源码第 142-166 行用 set 而非 slice 收集端点的原因。

---

### 4.3 ReferenceGrant：跨命名空间引用授权

#### 4.3.1 概念说明

Gateway API 默认禁止跨命名空间引用——一条在 `ns-a` 的 HTTPRoute 不能直接转发流量到 `ns-b` 的 Service。要打通，必须在**目标命名空间**（`ns-b`，即被引用资源所在命名空间）里放一个 `ReferenceGrant`，显式授权：

```yaml
# 在 ns-b 命名空间里
apiVersion: gateway.networking.k8s.io/v1beta1
kind: ReferenceGrant
metadata:
  name: allow-httproute
  namespace: ns-b
spec:
  from:
    - group: gateway.networking.k8s.io
      kind: HTTPRoute
      namespace: ns-a        # 允许谁引用
  to:
    - group: ""
      kind: Service          # 允许引用什么
      name: tea-svc          # 可省略，省略则允许该命名空间内所有 Service
```

这张白名单有四个维度：**谁（from：group/kind/namespace）** 引用 **什么（to：group/kind/name/namespace）**。NGF 在图阶段把集群里**所有** ReferenceGrant 拍平成一个集合，再在解析每个 `backendRef` 时用一次集合查询判断「这次跨命名空间引用是否被授权」。

为什么用集合而不是每次去遍历所有 grant？因为一次图构建要解析成百上千个 backendRef，而 grant 通常很少且构建期不变——预计算成 `map[allowedReference]struct{}` 后，每次查询是 O(1)。

#### 4.3.2 核心流程

```text
图构建开始（BuildGraph）
  └─ newReferenceGrantResolver(state.ReferenceGrants)   // 把所有 grant 拍平成 allowedReference 集合
       └─ 遍历每个 grant 的 每个 to × 每个 from → 生成一个 allowedReference 放进 map
            （to.name 为空 = 通配整个命名空间；to.group == "core" 归一为 ""）

解析某个 backendRef 时（见 4.1.3 validateBackendRef）：
  └─ resolver.refAllowedFrom(fromHTTPRoute(routeNs))   // 返回一个闭包，固定 from
       └─ 闭包被传入 toService(refNsName) 调用 → refAllowed(to, from)
            ├─ 查「精确名」key：to{name, namespace} + from   ← 命中即放行
            └─ 查「通配」key：to{无 name, namespace} + from  ← 命中（grant 没写 name）也放行
            都没命中 → 返回 false → validateBackendRef 返回 RefNotPermitted Condition
```

两个关键设计：

1. **两档查询**。`refAllowed` 先查「精确名」（grant 里写了具体 `to.name`），再查「通配整个命名空间」（grant 里省略 `to.name`，表示该命名空间内所有同类资源都允许）。这对应 Gateway API 规范里 ReferenceGrant 的 `to.name` 可选语义。

2. **类型安全的构造器**。`reference_grant.go` 提供了一组小工厂函数（`toService`、`toSecret`、`fromHTTPRoute`、`fromTCPRoute`…）来构造 `toResource`/`fromResource`，避免调用方手写字符串出错。注释明确要求「Use these functions when calling refAllowed instead of creating your own」——这是把「哪些 (group,kind) 组合是 NGF 支持的」收敛到一处定义。

> **注意区分两种「引用」**：本讲讲的是 **Route → Service（后端引用）** 的授权，from 是 HTTPRoute/GRPCRoute/TLSRoute/TCPRoute/UDPRoute。同一个 `referenceGrantResolver` 还被用于 **Gateway → Secret/ConfigMap（TLS 证书、CA）** 的授权（from 是 Gateway），以及 WAF 场景的 APPolicy/APLogConf 引用——所以你会看到 `toSecret`/`toConfigMap`/`toAPPolicy`/`fromGateway`/`fromWAFPolicy` 这些构造器。它们共享同一套授权机制，是 [u12-l3 TLS](u12-l3-tls-and-certificates.md) 和 [u10 WAF](u10-l1-wafpolicy-and-bundles.md) 的复用基础。

#### 4.3.3 源码精读

**集合构造 `newReferenceGrantResolver`**：

[reference_grant.go:164-200](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/reference_grant.go#L164-L200) —— 三层嵌套遍历：每个 grant × 每个 `to` × 每个 `from`，笛卡尔积成若干 `allowedReference` 放进 map。第 170-173 行处理 `to.Name` 为 nil（通配）；第 175-178 行把 `"core"` 归一为空字符串（K8s 核心组既可写 `""` 也可写 `"core"`，统一成 `""` 方便比较）；第 185 行把 grant 所在命名空间作为 `to.namespace`（授权总是「目标命名空间说了算」）。

**两档查询 `refAllowed`**：

[reference_grant.go:203-227](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/reference_grant.go#L203-L227) —— 第 204-207 行构造「精确名」key；第 211-218 行构造「通配」key（去掉 `name` 字段）；第 220-224 行依次查这两个 key，任一命中即放行。

**闭包工厂 `refAllowedFrom`**：

[reference_grant.go:231-235](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/reference_grant.go#L231-L235) —— 固定 `from`，返回一个只需传 `to` 的闭包。这正是 4.1.3 里 `createBackendRef` 调用 `refGrantResolver.refAllowedFrom(getRefGrantFromResourceForRoute(route.RouteType, routeNs))` 拿到的那个闭包——它已经把「这是某个命名空间的某种 Route 在发起引用」绑死，下游只需告诉它「要引用哪个 Service」。

**from 选择器 `getRefGrantFromResourceForRoute`**：

[backend_refs.go:806-815](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_refs.go#L806-L815) —— 按路由类型返回对应的 `fromResource`（HTTPRoute→`fromHTTPRoute`，GRPCRoute→`fromGRPCRoute`）。这保证授权查询里 `from.kind` 与实际路由种类一致。

#### 4.3.4 代码实践

**实践目标**：亲手构造一个跨命名空间后端引用，验证「没有 ReferenceGrant 时」图会把这条路由标记为 `ResolvedRefs=False / RefNotPermitted`，并对比「补上 ReferenceGrant 后」状态变为放行。

**操作步骤**：

1. 在一个已部署 NGF 的 kind 集群里准备两个命名空间：

   ```bash
   kubectl create namespace app-ns      # HTTPRoute 在这里
   kubectl create namespace backend-ns  # Service 在这里
   ```

2. 在 `backend-ns` 部署一个后端应用与 Service（可复用 [examples/](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/) 里的 cafe 示例 Deployment/Service，改命名空间即可）。

3. **先不创建** ReferenceGrant，直接在 `app-ns` 创建一条跨命名空间的 HTTPRoute：

   ```yaml
   apiVersion: gateway.networking.k8s.io/v1
   kind: HTTPRoute
   metadata:
     name: cross-ns-route
     namespace: app-ns
   spec:
     parentRefs:
       - name: <你的 Gateway 名字>     # 替换为实际 Gateway
     hostnames:
       - "tea.example.com"
     rules:
       - backendRefs:
           - name: tea-svc             # 位于 backend-ns
             namespace: backend-ns     # ★ 跨命名空间引用
             port: 80
   ```

4. 观察 HTTPRoute 状态：

   ```bash
   kubectl get httproute cross-ns-route -n app-ns -o yaml
   ```

5. 然后在 `backend-ns` 补上 ReferenceGrant（见 4.3.1 的 YAML 示例，把 `from.namespace` 改为 `app-ns`），再次观察状态变化。

**需要观察的现象**：
- 第 4 步（无 grant）：HTTPRoute 的 `status.parents[].conditions` 里出现 `ResolvedRefs=False`，`Reason=RefNotPermitted`，`Message` 形如 `Backend ref to Service backend-ns/tea-svc not permitted by any ReferenceGrant`。
- 第 5 步（有 grant）：同一条件变为 `ResolvedRefs=True`，请求可被正常转发。

**预期结果**：与源码 [backend_refs.go:594-602](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_refs.go#L594-L602) 的逻辑一致——无授权时返回 `NewRouteBackendRefRefNotPermitted`，回写为 `ResolvedRefs=False`。（具体输出依赖你的集群与 Gateway 名字，**待本地验证**。）

> **若没有集群**：可改为纯源码阅读型实践——在 [backend_refs_test.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_refs_test.go) 第 241-247 行的用例里，`alwaysFalseRefGrantResolver` 模拟「无授权」，断言期望得到 `RefNotPermitted` Condition；再找一个 `alwaysTrueRefGrantResolver` 的用例对比「有授权」。运行 `go test ./internal/controller/state/graph/ -run TestCreateBackendRef` 即可验证。

#### 4.3.5 小练习与答案

**练习 1**：如果一个 ReferenceGrant 的 `to` 段没写 `name` 字段，它的授权范围是什么？源码在哪里体现？

> **答案**：表示授权「目标命名空间内**所有**该 group/kind 的资源」。源码体现在 `newReferenceGrantResolver` 第 170-173 行（`to.Name` 为 nil 时 `toName` 留空），以及 `refAllowed` 第 211-218 行的「通配」key（构造查询时去掉 `name` 字段），两者都靠「`name` 字段为空」来匹配「整命名空间通配」语义。

**练习 2**：为什么 ReferenceGrant 必须放在**目标命名空间**（被引用资源所在），而不是发起引用的路由所在命名空间？

> **答案**：这是 Gateway API 的安全模型——资源主人对自己的资源有最终决定权。把授权放在目标命名空间，意味着只有「能写 `backend-ns` 的人」才能放行对 `backend-ns` 资源的引用，防止「能写 `app-ns` 路由的人」单方面打通到任意命名空间。源码第 185 行用 grant 所在命名空间（`nsname.Namespace`）作为 `to.namespace`，正是这一模型的落地。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「后端解析全链路追踪」任务。

**场景**：在 `app-ns` 有一条 HTTPRoute，其某条规则的 `backendRefs` 跨命名空间指向 `backend-ns/tea-svc:80`，而 `tea-svc` 有 2 个就绪 Pod、1 个未就绪 Pod。

**任务**：按下面的表格，逐栏追踪从 YAML 到 NGINX upstream 的完整链路，标注每一栏「发生在哪个阶段、由哪个文件/函数处理、产物是什么」。

| 链路环节 | 阶段 | 处理者（文件:函数） | 产物 / 结论 |
|---|---|---|---|
| 1. 跨命名空间引用是否被授权？ | 图 | reference_grant.go: `newReferenceGrantResolver` + `refAllowed` | 有 grant → 放行；无 → `RefNotPermitted` |
| 2. Service 存在且端口匹配？ | 图 | backend_refs.go: `getPortFromRef` + `getServicePort` | 得到 `ServicePort`；否则 `BackendNotFound` |
| 3. appProtocol 兼容性？ | 图 | backend_refs.go: `validateRouteBackendRefAppProtocol` | 不兼容 → `UnsupportedProtocol` |
| 4. 汇成内部 BackendRef | 图 | backend_refs.go: `createBackendRef` | `BackendRef{SvcNsName, ServicePort, Valid, ...}` |
| 5. 记录被引用的 Service | 图 | service.go: `buildReferencedServices` | `ReferencedService{GatewayNsNames, ...}` |
| 6. 解析真实 Pod 地址 | 配置 | resolver.go: `Resolve` + EndpointSlice 索引 | `[]Endpoint`（2 个就绪，1 个未就绪被丢弃） |
| 7. ExternalName 特例（本场景不触发） | 配置 | configuration.go: `resolveUpstreamEndpoints` | 单端点 `Resolve=true` |

**验证要点**：
- 解释为什么环节 1–5 在图阶段、环节 6 在配置阶段（提示：哪些数据会高频变化）。
- 对照源码说明环节 6 里那个「未就绪 Pod」是被哪一行代码丢弃的（[resolver.go:147-150](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/resolver/resolver.go#L147-L150)）。
- 思考：如果删掉 ReferenceGrant，环节 2 以后的链路还会执行吗？（提示：不会，因为 `createBackendRef` 在校验失败时已返回 `Valid=false`，但仍是生成一个 BackendRef 进分组，数据面返回 500。）

---

## 6. 本讲小结

- 后端解析被刻意拆成**两阶段**：图阶段做 `BackendRef → Service` 的校验与解析（产出内部 `BackendRef`），配置阶段才做 `Service → Endpoints` 的真正端点解析（`ServiceResolver`）——这样高频变化的 Pod 地址不会触发昂贵的图重建。
- 图阶段的核心是 `createBackendRef`：按「算 weight → 校验引用 → 查端口 → ExternalName 校验 → 关联 BackendTLSPolicy → 校验 appProtocol」推进，**即便引用非法也生成一个 `Valid=false` 的 BackendRef**（数据面对非法引用返回 500），所有错误沉淀为 `ResolvedRefs=False` + 不同 Reason 的 Condition。
- `ServiceResolver.Resolve` 用 EndpointSlice 的字段索引 `k8sServiceName` 快速定位目标 Service 的所有 slice，再经过滤（FQDN/地址类型/端口）、ready 检查、去重，产出 `[]Endpoint`；端口匹配靠 `svcPort.Name` 对齐。
- `ReferenceGrant` 是跨命名空间引用的授权机制：图构建时把所有 grant 笛卡尔积拍平成 `allowedReference` 集合，解析时用「精确名 + 通配命名空间」两档 O(1) 查询；缺授权即返回 `RefNotPermitted`；授权必须放在目标命名空间（资源主人说了算）。
- ReferenceGrant 机制不仅用于 Route→Service，还被 Gateway→Secret/ConfigMap（TLS）、WAF 等场景复用，靠一组类型安全的 `toXxx`/`fromXxx` 构造器统一表达。

---

## 7. 下一步学习建议

- 本讲产出的内部 `BackendRef` 与 `ReferencedService`，是下一讲 [u5-l4（dataplane.Configuration）](u5-l4-dataplane-configuration.md) 的直接输入——那里会讲图如何被转成数据面最终配置表示，并真正调用 `ServiceResolver` 拿到端点。
- 若想深入 TLS 后端的解析（`BackendTLSPolicy`、CA bundle 如何进入配置），可跳读 [u12-l3（TLS 与证书）](u12-l3-tls-and-certificates.md)，本讲提到的 `findBackendTLSPolicyForService` 与「多后端 BackendTLSPolicy 一致性校验」在那里展开。
- 若对「InferencePool 作为后端」的特殊解析路径（`resolveInferencePoolRef`、`toInferencePool` 授权）感兴趣，可读 [u12-l1（推理扩展）](u12-l1-inference-extension.md)。
- 推荐继续精读 [backend_refs.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_refs.go) 与其测试 [backend_refs_test.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_refs_test.go)，用不同输入跑测试、观察 Condition 输出，是巩固本讲最快的方式。
