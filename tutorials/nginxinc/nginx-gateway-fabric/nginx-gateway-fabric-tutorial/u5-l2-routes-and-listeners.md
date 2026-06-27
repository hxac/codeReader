# 路由与监听器：HTTP/gRPC/TCP/TLS/UDP

## 1. 本讲目标

本讲承接 [u5-l1（Graph 构建）](u5-l1-graph-building.md)，下钻到 Graph 里最核心的一组关系——**路由（Route）如何被识别、又如何挂到监听器（Listener）上**。

读完本讲，你应当能够：

- 说清 NGF 为什么把五种 Route（HTTPRoute、GRPCRoute、TLSRoute、TCPRoute、UDPRoute）统一成 `L7Route` 与 `L4Route` 两类内部模型，以及它们的差异。
- 追踪一条路由从原始 YAML 到「挂在某个 Listener 上」的完整源码路径，包括端口、协议、主机名三重匹配。
- 理解 ListenerSet 这一 Gateway API 扩展（GEP-1713）如何让监听器跨命名空间、跨资源被复用。
- 能够打开任意一条 HTTPRoute 的 YAML，对照源码判断它会被绑定到哪个 Listener、生成哪些 backendRefs。

---

## 2. 前置知识

在进入源码前，先用最直白的话建立几个直觉。

**路由（Route）和监听器（Listener）是什么关系？**

Gateway API 的设计里，`Gateway` 是「流量入口」，它内部声明一个或多个 `Listener`（监听器），每个 Listener 绑定一个端口、一种协议、可选一个主机名（hostname）。而 `HTTPRoute`、`GRPCRoute`、`TCPRoute` 等是「路由规则」，它们通过 `parentRefs` 声明「我想挂到哪个 Gateway 的哪个 Listener 上」，再通过 `backendRefs` 声明「匹配到的流量转发给哪个后端 Service」。

打个比方：Gateway 是一栋大楼，Listener 是大楼里某一扇门（门牌 = 主机名，门岗 = 端口/协议），Route 是一份「访客指引」，告诉门岗「长这样的访客，请带去 X 部门（后端）」。本讲要回答的核心问题就是：**NGF 如何判断一份 Route 能不能进门、进哪扇门。**

**L7 与 L4 的区别**

- **L7（七层）路由**：HTTPRoute / GRPCRoute。能在应用层做匹配——路径（path）、请求头（header）、方法（method）、查询参数（query param），匹配维度丰富。
- **L4（四层）路由**：TLSRoute / TCPRoute / UDPRoute。只工作在传输层，没有路径/请求头这些概念，主要靠 SNI（TLS 的主机名）或干脆「端口即路由」来区分。

这两类差异很大，所以 NGF 在内部用两个结构分别表示：`L7Route` 和 `L4Route`。

**协议（Protocol）与路由类型（RouteType）的对应**

| Listener.Protocol | 允许挂载的 Route Kind | 路由内部类型 |
|---|---|---|
| `HTTP` / `HTTPS` | HTTPRoute、GRPCRoute | `L7Route` |
| `TLS` | TLSRoute | `L4Route` |
| `TCP` | TCPRoute | `L4Route` |
| `UDP` | UDPRoute | `L4Route` |

这张表是本讲的「罗盘」，后面所有匹配逻辑本质都在校验它。

**条件（Conditions）回顾**

如同 [u5-l1](u5-l1-graph-building.md) 所述，NGF 的校验不靠抛 error，而是把问题沉淀进节点 `Conditions` 字段。本讲里你会反复看到「匹配失败」被写成一条 `Accepted=False` 的 Condition（如 `NoMatchingParent`、`NotAllowedByListeners`、`NoMatchingListenerHostname`），而不是让整条处理链中断。

---

## 3. 本讲源码地图

本讲涉及的关键文件都在 `internal/controller/state/graph/` 下：

| 文件 | 作用 |
|---|---|
| [route_common.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go) | 路由的「公共层」：定义 `L7Route`/`L4Route`/`RouteType`，构建所有 Route、把 Route 绑定到 Listener、主机名匹配、监听器隔离。**本讲最重要的文件。** |
| [httproute.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/httproute.go) | HTTPRoute 专属构建逻辑（规则校验、过滤器、backendRefs 解析）。作为 L7 构建的样板。 |
| [gateway_listener.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway_listener.go) | Listener 的构建与校验：按协议分发校验器、端口冲突检测、主机名冲突检测、TLS 证书解析。 |
| [listenerset.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/listenerset.go) | ListenerSet 的构建、授权校验、合并进 Gateway。 |
| [graph.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go) | `BuildGraph` 总装入口，把上述模块按拓扑顺序串起来。 |

补充：每种 Route 还有各自的薄封装文件 [tlsroute.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/tlsroute.go)、[tcproute.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/tcproute.go)、[udproute.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/udproute.go)、[grpcroute.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/grpcroute.go)，它们都委托回 `route_common.go` 的公共能力。

---

## 4. 核心概念与源码讲解

### 4.1 Route 统一处理：L7/L4 归一化模型与构建入口

#### 4.1.1 概念说明

Gateway API 一共有五种 Route 资源，如果每种都写一套独立的处理逻辑，代码会大量重复且容易不一致。NGF 的做法是**在图的内部模型层做归一化**：

- HTTPRoute、GRPCRoute → 都变成 `L7Route`，差别仅记在一个 `RouteType` 字段里（`http` / `grpc`）。
- TLSRoute、TCPRoute、UDPRoute → 都变成 `L4Route`，`RouteType` 记为 `tls` / `tcp` / `udp`。

这样做的好处是：后续「绑定到 Listener」「收集状态」这些逻辑只要写两套（L7 一套、L4 一套），而不是五套。每种 Route 的「薄封装」只负责把各自的 CRD 字段翻译进这两个统一模型。

#### 4.1.2 核心流程

五种 Route 构建成内部模型时，遵循同一个五步范式：

1. **解析 parentRefs**：调用公共的 `buildSectionNameRefs`，把 Route 声明的 `parentRefs` 规范化成一串 `ParentRef`（含 `SectionName`/`Port`/`Kind`）。若 parentRef 指向的 Gateway/ListenerSet 不在本 NGF 管辖范围，该 Route 直接被丢弃（返回 `nil`）。
2. **校验 hostnames**：调用 `validateHostnames`，非法主机名直接置 `Valid=false` 并附 Condition。
3. **校验规则（rules）**：L7 走 `processHTTPRouteRules`（校验 path/header/method 匹配与过滤器）；L4 走各自的 backendRef 校验。
4. **解析 backendRefs**：把后端引用解析成内部 `BackendRef`（这部分详见 [u5-l3 后端解析](u5-l3-backend-resolution.md)，本讲只看它在 Route 里的存放位置）。
5. **打标**：设置 `Valid`（整体是否有效）与 `Attachable`（是否允许尝试挂到 Listener）。

构建入口由 `BuildGraph` 在 [graph.go:321-331 与 341-350](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go#L321-L350) 调用，L7 与 L4 分两条流水线。

#### 4.1.3 源码精读

**归一化类型定义。** 五种 RouteType 用一个枚举表达（[route_common.go:73-86](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L73-L86)）。每个 Route 的「身份证」由 `NamespacedName + RouteType` 组成，分别封装成 `RouteKey`（L7）和 `L4RouteKey`（L4）（[route_common.go:88-102](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L88-L102)）——这样 Listener 内部用一张 `map[RouteKey]*L7Route` 就能装下所有 HTTP/GRPC 路由而不冲突。

`L7Route` 与 `L4Route` 是两个并列的结构（[route_common.go:104-119（L4Route）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L104-L119) 与 [route_common.go:144-161（L7Route）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L144-L161)）。它们共享一组「节点三元组」式的字段：`Source`（原始对象）、`Valid`（是否有效）、`Attachable`（是否可挂载）、`ParentRefs`（父引用）、`Conditions`（待回写状态）。区别在 `Spec`：L7 的 `L7RouteSpec` 含 `Rules`（多规则、匹配、过滤器），L4 的 `L4RouteSpec` 只含 `BackendRefs`/`Hostnames`，因为四层没有规则匹配的概念。

**L7 构建入口** `buildRoutesForGateways`（[route_common.go:357-408](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L357-L408)）遍历所有 HTTPRoute 与 GRPCRoute，分别调 `buildHTTPRoute` / `buildGRPCRoute`，并顺手为含 `RequestMirror` 过滤器的路由额外构造镜像路由（`buildHTTPMirrorRoutes`）。注意：若集群里没有 Gateway，直接返回 `nil`（[route_common.go:368-370](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L368-L370)）——没有 Gateway 就没有 Route 图。

**L4 构建入口** `buildL4RoutesForGateways`（[route_common.go:296-354](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L296-L354)）结构对称：分别构建 TLS/TCP/UDP 路由。其中 TCP/UDP 共用泛型函数 `buildGenericL4Route`（[route_common.go:1643-1719](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L1643-L1719)），`buildTCPRoute` 只是把它包一层（[tcproute.go:9-35](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/tcproute.go#L9-L35)）。这是「消除重复」的典型手法：两种四层路由只在「是否多 backend、是否校验主机名」上有细微差别，主体逻辑合一。

**以 HTTPRoute 为样板看五步范式。** `buildHTTPRoute`（[httproute.go:29-95](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/httproute.go#L29-L95)）：

```go
sectionNameRefs, err := buildSectionNameRefs(ghr.Spec.ParentRefs, ghr.Namespace, gws, listenerSets)
if err != nil { r.Valid = false; return r }
if len(sectionNameRefs) == 0 { return nil }   // 不属于本 NGF 任何 Gateway/ListenerSet
r.ParentRefs = sectionNameRefs
// ... 校验 hostnames → processHTTPRouteRules → 设 Valid
```

**公共的 parentRefs 规范化** 是本模块的「枢纽函数」`buildSectionNameRefs`（[route_common.go:451-544](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L451-L544)）。它处理 Gateway API 一个容易踩坑的细节：**parentRef 可以不写 sectionName**。此时规范要求「等价于挂到 Gateway 上所有 Listener」。该函数据此做三件事：

- 用 `resolveParentRef`（[route_common.go:412-449](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L412-L449)）按 `Kind`（Gateway / ListenerSet）找到父对象，并把父的 `EffectiveNginxProxy` 透传给 ParentRef。
- 若 `SectionName` 为空且无 `Port`：展开成「每个 Listener 一条 ParentRef」，保留同一个 `Idx` 以便后续识别这是内部展开的（[route_common.go:506-528](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L506-L528)）。
- 用 `checkUniqueSections` 检测「同一个父上写了重复 sectionName」，重复则报错（[route_common.go:465-472](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L465-L472)）。

> 小贴士：GRPCRoute 与 HTTPRoute 的构建几乎一模一样（对比 [grpcroute.go:19-56](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/grpcroute.go#L19-L56) 与 httproute.go），只是 `RouteType` 设为 `grpc`、规则字段不同。这印证了「归一化」的价值。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：验证「五种 Route 共享同一套构建骨架」。

**操作步骤**：

1. 打开 [route_common.go:296-354](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L296-L354)（L4 入口）与 [route_common.go:357-408](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L357-L408)（L7 入口），对比它们的结构。
2. 分别打开 [httproute.go:29-95](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/httproute.go#L29-L95)、[tlsroute.go:16-74](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/tlsroute.go#L16-L74)、[tcproute.go:9-35](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/tcproute.go#L9-L35)，找出三者都调用的两个公共函数名。

**需要观察的现象**：三个 `build*Route` 函数前 8 行几乎逐行一致。

**预期结果**：你会看到它们都先调 `buildSectionNameRefs(...)`、都判断 `len(sectionNameRefs) == 0 → return nil`、都调 `validateHostnames(...)`。这两个公共函数就是「统一处理」的落点。能说出这两个函数名即完成本实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `RouteKey` 要把 `RouteType` 也包含进 key，而不能只用 `NamespacedName`？

**参考答案**：因为同一个命名空间下，理论上 HTTPRoute 和 GRPCRoute 是不同 CRD，可以同名（Kubernetes 按资源类型隔离）。Listener 内部用 `map[RouteKey]*L7Route` 存所有 L7 路由，若 key 只有名字就可能把一个 HTTPRoute 和一个 GRPCRoute 互相覆盖；带上 `RouteType`（`http`/`grpc`）才能保证唯一。

**练习 2**：一条 HTTPRoute 的 `parentRefs` 既不写 `sectionName` 也不写 `port`，最终会被展开成几条 `ParentRef`？

**参考答案**：被展开成「目标 Gateway 上每个 Listener 一条 `ParentRef`」，且它们共享同一个原始 `Idx`（见 [route_common.go:506-528](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L506-L528)）。共享 `Idx` 的目的是后续能识别「这条 parentRef 是内部展开出来的，不是用户原写的」。

---

### 4.2 Listener 的构建与校验：协议、端口、主机名

#### 4.2.1 概念说明

Route 要挂到 Listener 上，前提是 Listener 本身是「合法且可用」的。一个 Listener 的合法性包含三个层面，恰好对应本节标题：

- **协议（Protocol）**：Listener 的协议决定它能接受哪种 Route；HTTP Listener 不能接 TCPRoute。
- **端口（Port）**：端口必须在 1–65535、不能和保护端口（如 NGINX 自身 metrics 端口）冲突；同一端口不能混用不兼容的协议。
- **主机名（Hostname）**：主机名格式要合法；同端口、同协议下不同 Listener 的主机名不能重叠。

NGF 把这三类校验抽象成一套「配置器工厂（configurator factory）」——按协议分发不同的校验器组合。这样新增一种协议只改工厂，不动调用方。

#### 4.2.2 核心流程

每个 Listener 的构建分四步（见 `listenerConfigurator.configure`）：

1. **跑 validators**：一组纯函数，逐个校验协议相关字段（端口范围、TLS 必填项、hostname 格式等），累加 Condition，并各自返回「是否仍可挂载（attachable）」。
2. **汇总 Valid/Attachable**：`Valid = len(conds) == 0`；`Attachable` 是所有 validator 返回值的逻辑与。
3. **跑 conflictResolvers**：跨 Listener 的冲突检测（端口/协议/主机名冲突），只在 Valid 时才跑。
4. **跑 externalReferenceResolvers**：解析外部引用（TLS 证书 Secret、前端 CA），同样只在 Valid 时跑。

协议到校验器的映射由工厂决定：`HTTP`/`HTTPS`/`TLS`/`TCP`/`UDP` 各有一组，未知协议落到 `unsupportedProtocol`。

#### 4.2.3 源码精读

**Listener 结构**（[gateway_listener.go:44-77](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway_listener.go#L44-L77)）——注意几个关键字段：`Valid`/`Attachable`（两者分离：一个 Listener 可以「无效但仍可挂载」）、`SupportedKinds`（它接受的 Route 类型）、`Routes`/`L4Routes`（挂上来的路由，正是下一节填充的目标）、`ResolvedSecrets`（解析出的 TLS 证书）。

**按协议分发的工厂**：`getConfiguratorForListener` 用一个 `switch listener.Protocol` 把请求路由到对应的 configurator（[gateway_listener.go:99-114](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway_listener.go#L99-L114)）。各协议的校验器组合在 `newListenerConfiguratorFactory` 里集中声明（[gateway_listener.go:116-215](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway_listener.go#L116-L215)）。例如 HTTPS 比 HTTP 多了 `validateListenerTLSTerminateFields`、TLS 证书解析器与前端 CA 解析器。

**协议 → 允许的 Route 类型**：这是「协议匹配」的权威映射，见 `getValidKindsForProtocol`（[gateway_listener.go:392-419](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway_listener.go#L392-L419)）：HTTP/HTTPS → HTTPRoute+GRPCRoute；TLS（Passthrough/Terminate）→ TLSRoute；TCP → TCPRoute；UDP → UDPRoute。这张表就是第 2 节那张「罗盘」表的源码出处。

**configure 主流程**（[gateway_listener.go:250-316](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway_listener.go#L250-L316)）就是上面四步的落地：

```go
for _, validator := range c.validators {
    currConds, currAttachable := validator(listener)
    conds = append(conds, currConds...)
    attachable = attachable && currAttachable   // 任一 validator 否决即不可挂载
}
valid := len(conds) == 0
// ...构造 Listener 对象；若 valid 才继续跑 conflict / external resolvers
```

**端口冲突检测** 用「协议分组」思路（[gateway_listener.go:663-722](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway_listener.go#L663-L722)）：把协议分成三组——secure（HTTPS、TLS）、insecure（HTTP）、L4（TCP、UDP）。同一端口的 Listener 必须属同一组；同组内还要避免「L4 同协议撞端口」或「HTTPS/TLS 主机名重叠」。冲突时把相关 Listener 全部置 `Valid=false` 并附 `ProtocolConflict` / `HostnameConflict` Condition。

**主机名冲突** 另由 `uniqueListenerConflictResolver`（[gateway_listener.go:801-838](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway_listener.go#L801-L838)）兜底：用 `(port, protocol, hostname)` 三元组去重，重复即冲突。

> 小贴士：端口范围本身由 `validateListenerPort`（[gateway_listener.go:462-472](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway_listener.go#L462-L472)）把关，还会查询 `protectedPorts`（NGINX 自身要用的端口，如 9113 metrics、8081 health）来拒绝用户占用。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：理解「协议决定校验器组合」这一设计。

**操作步骤**：

1. 打开 [gateway_listener.go:116-215](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway_listener.go#L116-L215)，找到 `https:` 和 `tcp:` 两块配置。
2. 数一数 HTTPS 比 TCP 多了哪些 validator 与 resolver。

**需要观察的现象**：HTTPS 块挂了 `validateListenerTLSTerminateFields`、TLS 证书外部引用解析器、前端 CA 解析器；TCP 块只有端口/标签/协议三类基础校验。

**预期结果**：你能说出「HTTPS 的额外校验都围绕 TLS 证书与终止配置展开，而 TCP/UDP 因为不涉及 TLS 所以轻量」。这正是协议差异在代码里的具象化。

#### 4.2.5 小练习与答案

**练习 1**：一个 HTTPS Listener（端口 443）和一个 TLS Listener（端口 443，主机名都是 `*.example.com`）会怎样？

**参考答案**：两者同属 secure 协议组、同端口、主机名重叠。`portConflictResolver` 会判定为 HTTPS/TLS 主机名重叠冲突（[gateway_listener.go:745-778](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway_listener.go#L745-L778)），把两个 Listener 都置 `Valid=false` 并附 `HostnameConflict`。

**练习 2**：为什么 `Valid` 和 `Attachable` 要分开存？

**参考答案**：有的校验失败只让 Listener「无法生成有效 NGINX 配置」（`Valid=false`），但仍然允许 Route 尝试挂上来（`Attachable=true`），这样 NGF 才能在 Route 的状态里回报「目标 Listener 无效」之类的原因，而不是让 Route 静默挂不上。例：端口冲突会让 `Valid=false`，但相关 validator 返回的 `attachable` 仍可能是 `true`，Route 因此能附上失败 Condition。

---

### 4.3 路由绑定 Listener：sectionName / port / hostname 三重匹配

#### 4.3.1 概念说明

Route 被构建成 `L7Route`/`L4Route` 后，还只是「游离」的对象——它知道自己想挂哪个 Gateway，但还没真正「挂」上去。**绑定（attachment）** 就是把 Route 与具体的 Listener 建立双向引用（Route 的 `ParentRef.Attachment` 记录结果，Listener 的 `Routes`/`L4Routes` map 记录挂上来的路由）。

绑定是否成功，取决于三重匹配：

1. **sectionName / port 匹配**：Route 指明的 Listener（按 sectionName 或 port）能不能找到、且是否 `Attachable`。
2. **协议匹配**：Route 类型必须在 Listener 的 `SupportedKinds` 里（HTTPRoute 不能挂 TCP Listener）。
3. **主机名匹配**：Route 的 `hostnames` 与 Listener 的 `hostname` 必须有交集（L4 的 TCP/UDP 例外，见后）。

任何一重不过，都不会让程序崩溃，而是写入一条 `Accepted=False` 的 Condition。

#### 4.3.2 核心流程

绑定由 `bindRoutesToListeners`（每个 Gateway 一轮）驱动，对每条 Route：

```
对每个 ParentRef:
  └─ validateParentRef          → 找出 attachableListeners；检查 Gateway/ListenerSet 有效性
       └─ findAttachableListeners   → 按 sectionName/port 三种情形筛选 Listener
  └─ (L7) 校验 GRPC 是否被禁用 HTTP2、backendRef 是否对该 Gateway 无效
  └─ tryToAttachL7RouteToListeners → 逐个 Listener：
        ├─ isRouteNamespaceAllowedByListener  (命名空间授权)
        ├─ isRouteTypeAllowedByListener       (协议/Kind 匹配)
        ├─ findAcceptedHostnames              (主机名交集)
        └─ 成功 → l.Routes[rk] = route        (建立双向引用)
  └─ 记录 Accepted 状态 + 失败 Condition
最后做 listener 隔离（isolateHostnamesForParentRefs）
```

#### 4.3.3 源码精读

**总编排** `bindRoutesToListeners`（[route_common.go:606-649](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L606-L649)）：对每个 Gateway，先绑 L7 路由、再绑 L4 路由。注意 L4 路由在绑之前会按「创建时间+名字」排序（[route_common.go:636-638](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L636-L638)），因为四层路由可能竞争同一个端口，需要确定性的优先级。

**找可挂载 Listener**：`validateParentRef`（[route_common.go:758-804](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L758-L804)）先分离出 Gateway 自有 Listener 与 ListenerSet 带来的 Listener（取决于 parentRef 的 Kind），再调 `findAttachableListeners`。

`findAttachableListeners`（[route_common.go:1191-1235](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L1191-L1235)）是「sectionName/port 匹配」的核心，它分三种情形：

| 情形 | Route 的 parentRef | 行为 |
|---|---|---|
| Case 1 | 指定了 `sectionName` | 精确找那个名字的 Listener，并核对端口是否一致 |
| Case 2 | 只指定了 `port` | 返回该端口上所有 `Attachable` 的 Listener |
| Case 3 | 都没指定 | 返回所有 `Attachable` 的 Listener |

找不到对应 sectionName 时返回 `NoMatchingParent` Condition（[route_common.go:784-787](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L784-L787)）。

**L7 绑定主循环** `tryToAttachL7RouteToListeners`（[route_common.go:1130-1187](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L1130-L1187)），其内部的 `bind` 闭包就是三重匹配的落点：

```go
bind := func(l *Listener) (allowed, attached bool) {
    if !isRouteNamespaceAllowedByListener(...) { return false, false }  // 命名空间
    if !isRouteTypeAllowedByListener(l, convertRouteType(route.RouteType)) { return false, false } // 协议
    hostnames := findAcceptedHostnames(l.Source.Hostname, route.Spec.Hostnames) // 主机名
    if len(hostnames) == 0 { return true, false }
    l.Routes[rk] = route   // 双向引用：挂成功
    return true, true
}
```

- **协议匹配** `isRouteTypeAllowedByListener`（[route_common.go:1349-1356](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L1349-L1356)）：Route 的 Kind 必须在 Listener 的 `SupportedKinds` 里。
- **命名空间授权** `isRouteNamespaceAllowedByListener`（[route_common.go:1320-1346](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L1320-L1346)）：按 Listener 的 `allowedRoutes.namespaces.from`（All / Same / Selector）判断 Route 所在命名空间是否被允许。
- **主机名交集** `findAcceptedHostnames`（[route_common.go:1237-1257](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L1237-L1257)）配合 `match`（[route_common.go:1259-1279](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L1259-L1279)）：支持精确匹配与通配符（`*.example.com`）双向匹配，并取「更具体」的一方（`GetMoreSpecificHostname`，[route_common.go:1286-1318](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L1286-L1318)）。主机名特异性可粗略理解为「子域层数越多越具体」，对两条都带通配的主机名：

\[
\text{specificity}(h) \approx \text{子域段数}(h)
\]

层数多者更具体。

**L4 绑定** `bindL4RouteToListeners` / `tryToAttachL4RouteToListeners`（[route_common.go:806-935](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L806-L935)）逻辑类似，但有一个关键不同：**TCP/UDP 没有路由判别条件（无主机名/路径/SNI），所以一个 Listener 端口只能挂一条 TCP/UDP Route**（[route_common.go:982-992](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L982-L992)）。若已有别的 TCP/UDP 路由占用，返回 `multipleRoutesOnListener`。

**监听器隔离**：绑定后还要做 `isolateHostnamesForParentRefs`（[route_common.go:697-745](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L697-L745)——`bindRoutesToListeners` 末尾调用）。Gateway API 要求「同端口、不同 Listener 上的主机名要相互隔离」，这段代码会把「被同端口另一个 Listener 认领的」主机名从当前 ParentRef 的 `AcceptedHostnames` 里剔除，确保流量只进最具体的 Listener。

#### 4.3.4 代码实践（源码阅读型 —— 本讲主实践）

**实践目标**：给定一条 HTTPRoute，手动追踪它在图中被绑定到哪个 Listener、生成哪些 backendRefs。

**操作步骤**：

1. 准备一条典型 HTTPRoute（示例代码，非项目内文件）：

   ```yaml
   # 示例代码：一条 HTTPRoute，挂在名为 http 的 Listener 上
   apiVersion: gateway.networking.k8s.io/v1
   kind: HTTPRoute
   metadata:
     name: cafe
     namespace: default
   spec:
     parentRefs:
     - name: gateway         # 不写 sectionName/port
     hostnames: ["cafe.example.com"]
     rules:
     - matches:
       - path: { type: PathPrefix, value: /coffee }
       backendRefs:
       - name: coffee-svc
         port: 80
   ```

2. 按源码顺序追踪这条 Route（用下面的「追踪表」逐行填）：

   | 步骤 | 函数（文件:行） | 这条 Route 会发生什么 |
   |---|---|---|
   | ① 构建 Route | `buildHTTPRoute` ([httproute.go:29](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/httproute.go#L29)) | `RouteType=http`；`Attachable=true` |
   | ② 规范化 parentRefs | `buildSectionNameRefs` ([route_common.go:451](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L451)) | 因未写 sectionName/port → 展开成 Gateway 上每个 Listener 一条 ParentRef |
   | ③ 解析 backendRefs | `getBackendRefs` ([httproute.go:285](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/httproute.go#L285)) | 生成 `RouteBackendRef{coffee-svc:80}`（实际 Service 解析见 u5-l3） |
   | ④ 绑定 | `bindL7RouteToListeners` ([route_common.go:1048](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L1048)) → `tryToAttachL7RouteToListeners` ([route_common.go:1130](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L1130)) | 对每个 Listener 跑三重匹配 |
   | ⑤ 命中 | `findAcceptedHostnames` ([route_common.go:1237](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L1237)) | 只有主机名能与 `cafe.example.com` 交集的 Listener 才会被 `l.Routes[rk]=route` |

3. 关键问题自检：若 Gateway 上有一个 HTTP Listener（hostname 留空 = catch-all）和一个 hostname 为 `tea.example.com` 的 HTTPS Listener，这条 Route 会挂到哪个？

**需要观察的现象**：catch-all（hostname 空）的 HTTP Listener 会与 `cafe.example.com` 交集成功（`match` 中空主机名恒真，见 [route_common.go:1260-1262](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L1260-L1262)）；`tea.example.com` 的 Listener 主机名不匹配、被跳过。同时 catch-all Listener 还要通过协议匹配（支持 HTTPRoute）和命名空间授权。

**预期结果**：Route 最终挂在那个 catch-all HTTP Listener 上，其 `ParentRef.Attachment.Attached=true`、`AcceptedHostnames` 记录 `cafe.example.com`；规则里生成一个指向 `coffee-svc:80` 的 `RouteBackendRef`。若你想确认运行时实际值，可在本地跑 NGF 后 `kubectl describe httproute cafe` 查看 `Parents[].conditions` 里的 `Accepted=True`——具体输出**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：一条 HTTPRoute 的 `hostnames` 写了 `*.example.com`，Gateway 上有个 Listener 的 hostname 是 `a.example.com`。`AcceptedHostnames` 会记录什么？

**参考答案**：`findAcceptedHostnames` 会取「更具体」的一方（[route_common.go:1252](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L1252) 调 `GetMoreSpecificHostname`）。`a.example.com`（具体）比 `*.example.com`（通配）更具体，所以记录 `a.example.com`。

**练习 2**：两条 TCPRoute 都 `parentRefs` 指向同一个 TCP Listener（同端口），会发生什么？

**参考答案**：第一条成功挂上（`l.L4Routes[key]=route`）；第二条在 `bindToListenerL4` 里发现该 Listener 已挂着「另一条」TCP 路由（[route_common.go:984-991](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L984-L991)），返回 `multipleRoutesOnListener=true`，最终第二条 Route 拿到 `Accepted=False / RouteMultipleRoutesOnListener` Condition。

---

### 4.4 ListenerSet：跨资源扩展监听器

#### 4.4.1 概念说明

Gateway API 的 [GEP-1713 ListenerSet](https://gateway-api.sigs.k8s.io/geps/gep-1713/) 让你**把监听器定义在 Gateway 之外**：一个 `ListenerSet` 资源通过 `parentRef` 挂到某个 Gateway，把自己声明的 Listener「合并」进该 Gateway。它的价值在于：

- **跨命名空间复用**：A 命名空间的团队可以把 Listener 定义在自己的 ListenerSet 里，挂到平台团队的 Gateway 上。
- **职责分离**：Gateway 只管「大楼入口」，各业务线用各自的 ListenerSet 管「各自的门」。

但要挂上去必须过两关：①父 Gateway 必须显式允许（`AllowedListeners`）；②ListenerSet 带来的 Listener 要复用 Gateway 的校验器，保证不和 Gateway 已有 Listener 冲突。

#### 4.4.2 核心流程

ListenerSet 的处理在 `BuildGraph` 里紧随 Gateway 构建之后（[graph.go:302-304](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go#L302-L304)）：

1. **buildListenerSets**：为每个 ListenerSet 找到父 Gateway，做授权校验，产出内部 `ListenerSet`（含 `Valid`）。
2. **attachListenerSetsToGateways**：把合法的 ListenerSet 按优先级（创建时间+名字）排序，逐个合并进 Gateway。
3. **mergeGatewayAndListenerSetListeners**：**复用 Gateway 的 `ListenerFactory`** 构建每个 Listener——这是关键，保证冲突状态在同一套 resolver 里累积。
4. 之后 Route 绑定时，可以通过 `parentRef.kind: ListenerSet` 直接挂到 ListenerSet 带来的 Listener 上（见 4.1 的 `resolveParentRef`）。

#### 4.4.3 源码精读

**内部模型** `ListenerSet`（[listenerset.go:18-29](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/listenerset.go#L18-L29)）：除了 `Source`/`Valid`/`Conditions`，还持有 `Gateway`（父 Gateway 引用）与 `Listeners`（它带来的、各自校验过的 Listener）。

**构建与授权** `buildListenerSets`（[listenerset.go:34-73](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/listenerset.go#L34-L73)）先按 `parentRef` 找父 Gateway，找不到则跳过；找到后交 `validateListenerSet`（[listenerset.go:75-97](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/listenerset.go#L75-L97)）做两道关：

- 父 Gateway 必须 `Valid`（已被认领且自身有效），否则给 `ParentNotAccepted`。
- 必须被父 Gateway 的 `AllowedListeners` 允许，否则给 `NotAllowed`。

**授权细则** `isListenerSetAllowedByGateway`（[listenerset.go:101-151](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/listenerset.go#L101-L151)）：按 `Gateway.Spec.AllowedListeners.Namespaces.From` 四种取值判断——`None`（默认，不允许）、`Same`（同命名空间）、`All`（任意）、`Selector`（按命名空间标签）。注意默认是「不允许」（[listenerset.go:113-115](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/listenerset.go#L113-L115)），这是安全默认。

**合并进 Gateway** `attachListenerSetsToGateways`（[listenerset.go:153-201](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/listenerset.go#L153-L201)）先按 GEP-1713 的优先级排序（创建时间早的、名字字典序靠前的优先，用 `ngfsort.LessClientObject`），再逐个合并。排序保证多个 ListenerSet 抢同一端口/主机名时结果是确定的。

**复用工厂** `mergeGatewayAndListenerSetListeners`（[listenerset.go:204-251](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/listenerset.go#L204-L251)）的核心是这一行：

```go
validatedListeners := buildListeners(
    gw,                       // 用 Gateway 自己的 ListenerFactory
    lsListeners,
    client.ObjectKeyFromObject(gw.Source),
    types.NamespacedName{..., Name: listenerSet.Source.Name},  // 记下 ListenerSetName
)
```

因为传的是同一个 `gw`，其 `ListenerFactory` 内部的冲突 resolver 状态会跨「Gateway 自有 Listener + 各 ListenerSet Listener」累积——这样 ListenerSet 带来的 Listener 一旦和 Gateway 已有 Listener 撞端口/主机名，就会被标 `Valid=false`（呼应 4.2 的冲突检测）。每个 Listener 的 `ListenerSetName` 字段被填上来源 ListenerSet 名（[gateway_listener.go:48-50](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway_listener.go#L48-L50)），后续绑定逻辑正是靠它区分「这个 Listener 来自 Gateway 还是 ListenerSet」（见 `separateGatewayAndListenerSetListeners`，[route_common.go:1385-1397](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L1385-L1397)）。

**Route 怎么挂到 ListenerSet 的 Listener**：Route 的 `parentRef` 写 `kind: ListenerSet` 即可。`resolveParentRef`（[route_common.go:431-445](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L431-L445)）会找到该 ListenerSet，并把其父 Gateway 的 `EffectiveNginxProxy` 透传过来；随后 `validateParentRef` 在绑定阶段只在该 ListenerSet 的 Listener 范围内匹配（[route_common.go:770-776](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L770-L776)）。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：理解「ListenerSet 复用 Gateway 校验器」为何能保证冲突一致。

**操作步骤**：

1. 打开 [listenerset.go:204-251](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/listenerset.go#L204-L251)，找到 `buildListeners(gw, ...)` 调用，确认第一个参数是 Gateway 对象。
2. 跟进 [gateway_listener.go:79-93](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway_listener.go#L79-L93) 的 `buildListeners`，看它如何从 `gateway.ListenerFactory` 取 configurator。
3. 回看 [gateway.go:137](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway.go#L137) 确认 `ListenerFactory` 是 Gateway 级别的字段。

**需要观察的现象**：`ListenerFactory` 挂在 `Gateway` 上，ListenerSet 合并时复用的是父 Gateway 那一个实例。

**预期结果**：你能解释「为什么 Gateway 已有一个 443 端口的 HTTPS Listener、ListenerSet 再带来一个 443 的 HTTPS Listener 会被判冲突」——因为两者走过同一个 `portConflictResolver`，它记住了端口 443 已被占用。**待本地验证**：可在集群里实际构造此场景，观察 ListenerSet 的 Listener 拿到 `ProtocolConflict` Condition。

#### 4.4.5 小练习与答案

**练习 1**：Gateway 没有写 `spec.allowedListeners`，一个 ListenerSet 想挂上来，结果如何？

**参考答案**：`isListenerSetAllowedByGateway` 在 `AllowedListeners == nil` 或 `From == nil` 时返回 `false`（[listenerset.go:106-115](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/listenerset.go#L106-L115)），默认语义是「None = 不允许」。ListenerSet 拿到 `NotAllowed` Condition、`Valid=false`，不会合并进 Gateway。

**练习 2**：Route 想挂到 ListenerSet 带来的某个 Listener，`parentRef` 该怎么写？

**参考答案**：写 `kind: ListenerSet`、`name: <ListenerSet名>`，可再加 `sectionName` 指定该 ListenerSet 内具体某个 Listener（见 [route_common.go:412-449](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L412-L449) 的 `resolveParentRef` 对 `kinds.ListenerSet` 分支的处理）。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「全链路追踪」。

**任务**：构造一个含 ListenerSet 的场景，追踪一条 HTTPRoute 从 YAML 到「挂在某个 Listener 上」的完整路径，并预测它会挂到哪个 Listener。

**资源准备**（示例代码，非项目内文件）：

```yaml
# 示例代码：Gateway 有一个 catch-all HTTPS Listener；另有一个 ListenerSet 带来专门的主机名 Listener
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata: { name: gw, namespace: default }
spec:
  gatewayClassName: nginx
  allowedListeners:
    namespaces: { from: Same }     # 允许同命名空间 ListenerSet
  listeners:
  - name: https
    port: 443
    protocol: HTTPS
    tls: { mode: Terminate, certificateRefs: [{ name: gw-cert }] }
---
apiVersion: gateway.networking.k8s.io/v1beta1
kind: ListenerSet
metadata: { name: team-a, namespace: default }
spec:
  parentRef: { name: gw }
  listeners:
  - name: team-a-api
    port: 443
    protocol: HTTPS
    hostname: api.team-a.example.com
    tls: { mode: Terminate, certificateRefs: [{ name: team-a-cert }] }
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata: { name: api-route, namespace: default }
spec:
  parentRefs:
  - kind: ListenerSet
    name: team-a
  hostnames: ["api.team-a.example.com"]
  rules:
  - backendRefs: [{ name: api-svc, port: 80 }]
```

**追踪步骤**（在源码里逐站验证你的预测）：

1. **ListenerSet 合并**：走 [listenerset.go:204](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/listenerset.go#L204) `mergeGatewayAndListenerSetListeners`。注意 Gateway 的 `https` Listener 主机名为空（catch-all）、ListenerSet 的 `team-a-api` 主机名为 `api.team-a.example.com`——判断是否会触发 4.2 的主机名冲突（提示：catch-all 与具体主机名是否算 overlap？看 [gateway_listener.go:1065-1076](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway_listener.go#L1065-L1076) `haveOverlap`）。
2. **Route 构建**：走 [httproute.go:29](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/httproute.go#L29) `buildHTTPRoute`，parentRef 因 `kind: ListenerSet` 走 [route_common.go:431-445](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L431-L445)。
3. **绑定**：走 [route_common.go:1048](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L1048) `bindL7RouteToListeners`，在 `validateParentRef` 里只匹配该 ListenerSet 的 Listener（[route_common.go:770-776](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L770-L776)）。
4. **预测**：`api-route` 应挂在 `team-a-api` Listener 上（主机名精确匹配），而不会去碰 Gateway 自有的 catch-all `https` Listener——因为它通过 `kind: ListenerSet` 锁定了范围。

**验收**：在本地集群 apply 上述资源（**待本地验证**），`kubectl describe httproute api-route` 应看到 `Parents[].controllerName` 指向 ListenerSet、`Accepted=True`；`kubectl describe gateway gw` 的 `Listeners` 里应同时出现 `https` 与合并进来的 `team-a-api`。

---

## 6. 本讲小结

- NGF 把五种 Route 归一成两类内部模型：HTTPRoute/GRPCRoute → `L7Route`，TLSRoute/TCPRoute/UDPRoute → `L4Route`，用 `RouteType` 区分；公共构建骨架集中在 `route_common.go` 的 `buildSectionNameRefs`、`buildRoutesForGateways`、`buildL4RoutesForGateways`。
- Listener 按协议由 `listenerConfiguratorFactory` 分发不同校验器组合，`configure` 跑「validators → conflictResolvers → externalResolvers」三段；`Valid` 与 `Attachable` 分离，允许「无效但仍可挂载」以回报状态。
- 协议到 Route 类型的权威映射在 `getValidKindsForProtocol`（HTTP/HTTPS→HTTPRoute+GRPCRoute；TLS→TLSRoute；TCP→TCPRoute；UDP→UDPRoute）。
- 路由绑定 Listener 走「sectionName/port → 协议 → 主机名」三重匹配，全部沉淀为 `Accepted` Condition；TCP/UDP 因无判别条件而「一端口一路由」；绑定后还做监听器主机名隔离。
- ListenerSet（GEP-1713）让监听器跨资源/跨命名空间复用，但需父 Gateway 的 `AllowedListeners` 授权，且合并时复用父 Gateway 的 `ListenerFactory` 以保证冲突检测一致；Route 通过 `parentRef.kind: ListenerSet` 直接挂其 Listener。

---

## 7. 下一步学习建议

- 本讲只讲到 `backendRefs` 被「放进」`RouteRule`，但 Service/Endpoints 的真正解析、跨命名空间 `ReferenceGrant` 授权发生在另一个环节——继续学 **[u5-l3 后端解析：Service、BackendRef、ReferenceGrant](u5-l3-backend-resolution.md)**。
- Route 挂上 Listener 后，整张 Graph 还要转成数据面配置——见 **[u5-l4 dataplane.Configuration](u5-l4-dataplane-configuration.md)**。
- 想了解绑定结果如何回写到 Kubernetes 资源 status（`Accepted`/`ResolvedRefs` 等 Condition 的下发），见 **[u8-l1 Conditions 体系与状态更新](u8-l1-conditions-and-status-updates.md)**。
- 推荐配套阅读 [Gateway API 路由绑定规范](https://gateway-api.sigs.k8s.io/api-types/httproute/) 与 [GEP-1713 ListenerSet](https://gateway-api.sigs.k8s.io/geps/gep-1713/)，把本讲的源码行为对照官方语义。
