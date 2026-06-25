# 路由资源全景：Ingress、VirtualServer、VSR、TransportServer

## 1. 本讲目标

在 [u1-l1](u1-l1-project-overview.md) 里，我们已经建立了「Ingress 是规则、Ingress Controller 是执行者」的心智模型，并知道 NGINX Ingress Controller（以下简称 NIC）支持两类路由资源——标准 Ingress 与一组自定义资源（CRD）。本讲把镜头拉近，专门回答一个问题：**这些资源分别长什么样、各自能做什么、彼此如何组合**。

读完本讲，你应当能够：

- 说清标准 Ingress 与 VirtualServer 在「能力上限」和「写法结构」上的关键差异，能判断一个需求该用哪个。
- 理解 VirtualServer 与 VirtualServerRoute 的「聚合 / 子路由」组合关系，以及跨命名空间与标签选择器两种引用方式。
- 认识 TransportServer 在四层（L4）的定位，以及它为什么必须依赖 GlobalConfiguration 提供的 listener。
- 把 Policy 与 GlobalConfiguration 放进整张资源地图，理解「可复用策略」与「全局监听点」这两个解耦维度。

本讲只讲**用户视角的资源模型**（YAML 写法、字段语义、组合方式），不进入控制器如何 watch 它们、如何把它们翻译成 NGINX 配置——那是第 3、4 单元的主题。

## 2. 前置知识

本讲假设你已经读完 [u1-l1](u1-l1-project-overview.md)，了解以下概念（这里只做最简回顾，不展开）：

- **Ingress / Ingress Controller**：Ingress 是 Kubernetes 内置的声明式 HTTP 路由规则资源；Ingress Controller 才是真正读取规则、调谐负载均衡器配置的程序。
- **控制器范式（reconcile）**：watch 资源 → 发现期望与实际的差异 → 让实际逼近期望。
- **NIC 的资源全景**：标准 Ingress（用 annotations/ConfigMap 扩展）+ CRD（VirtualServer / VirtualServerRoute = L7，TransportServer = L4）+ Policy + GlobalConfiguration。

如果你对 K8s 的 Service / Pod / 命名空间还不熟，建议先补一下「Service 把一组 Pod 抽象成一个稳定访问入口」这个概念——本讲的 upstream 几乎都指向某个 Service。

还要先记一个 OSI 分层的直觉，它贯穿本讲：

- **七层（L7，HTTP 层）**：能看见 HTTP 报文——方法、路径、Host、请求头、Cookie。VirtualServer 工作在这一层。
- **四层（L4，TCP/UDP 层）**：只能看见字节流 / 数据报，看不见 HTTP。TransportServer 工作在这一层。

## 3. 本讲源码地图

本讲主要阅读 `docs/crd/` 下的 CRD 说明文档（它们由 `pkg/apis/configuration/v1/types.go` 的 kubebuilder 标注自动生成，是字段语义的权威说明），辅以 `examples/` 下的真实示例来对照写法。

| 文件 | 作用 |
| --- | --- | 
| `docs/crd/k8s.nginx.org_virtualservers.md` | VirtualServer CRD 的字段说明，本讲主战场 |
| `docs/crd/k8s.nginx.org_virtualserverroutes.md` | VirtualServerRoute CRD 的字段说明 |
| `docs/crd/k8s.nginx.org_transportservers.md` | TransportServer CRD 的字段说明（L4） |
| `docs/crd/k8s.nginx.org_globalconfigurations.md` | GlobalConfiguration CRD 的字段说明（listener 注册表） |
| `docs/crd/k8s.nginx.org_policies.md` | Policy CRD 的字段说明（可复用策略） |
| `README.md` | 项目对各类资源的官方定位说明 |
| `examples/ingress-resources/complete-example/cafe-ingress.yaml` | 标准 Ingress 的最小完整示例 |
| `examples/custom-resources/basic-configuration/cafe-virtual-server.yaml` | VirtualServer 的最小完整示例 |
| `examples/custom-resources/basic-configuration-vsr/` | VirtualServer + VirtualServerRoute 组合示例 |
| `examples/custom-resources/basic-tcp-udp/` | TransportServer + GlobalConfiguration 的 TCP/UDP 示例 |

> 说明：CRD 文档的字段表是从 Go 类型生成的，等我们学到 [u2-l2](u2-l2-crd-type-definitions.md) 时会回到 `types.go` 看它的源头。本讲我们把这些文档当「字段字典」来用。

## 4. 核心概念与源码讲解

### 4.1 Ingress vs VirtualServer

#### 4.1.1 概念说明

标准 **Ingress** 是 Kubernetes 内置资源（`networking.k8s.io/v1`），它的设计目标是「跨控制器可移植」——同一份 Ingress YAML，理论上可以被 nginx、traefik、haproxy 等不同控制器消费。为了维持这种可移植性，Ingress 的能力被刻意收窄：只支持**基于主机名的路由**、**基于路径的前缀路由**和**每主机的 TLS 终止**。

但生产环境的需求远不止于此——流量灰度（把 10% 流量切到新版本）、根据请求头做内容路由、改写 URI、限流、JWT 认证……这些在标准 Ingress spec 里都没有字段。社区各控制器的「解法」是用**注解（annotations）**来扩展，比如 NIC 就用 `nginx.org/*` 一族注解。注解的本质是「把一段字符串塞进注解值里」，类型不安全、不可复用、跨控制器不通。

**VirtualServer**（`k8s.nginx.org/v1`）是 NIC 自己定义的 CRD，它**放弃了跨控制器可移植性，换取了 NGINX 全部能力的结构化表达**。它的官方定义就点明了这一点：

> The `VirtualServer` resource defines a virtual server for the NGINX Ingress Controller. It provides advanced configuration capabilities beyond standard Kubernetes Ingress resources, including traffic splitting, advanced routing, header manipulation, and integration with NGINX App Protect.
>
> 见 [docs/crd/k8s.nginx.org_virtualservers.md:8-10](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualservers.md#L8-L10)

一句话总结两者的取舍：

| 维度 | 标准 Ingress | VirtualServer |
| --- | --- | --- |
| 归属 | Kubernetes 内置 | NIC 私有 CRD |
| 可移植性 | 跨控制器 | 仅 NGINX / NGINX Plus |
| 后端声明 | 内联（path 里直接写 service+port） | 显式命名对象（upstreams），route 按名引用 |
| 路由能力 | host + path 前缀 | 前缀 / 最长前缀 / 精确 / 正则，且支持 matches 条件路由 |
| 流量切分 | 不支持 | 支持（splits，按权重） |
| 头部改写 | 仅靠注解 | 结构化字段（action.proxy.requestHeaders 等） |
| 策略复用 | 注解（每资源一份） | Policy CRD（定义一次，多处引用） |

#### 4.1.2 核心流程

两者的「结构差异」可以用一段对照伪代码看清。同样是「把 `/tea` 路由到 `tea-svc`」：

```text
# Ingress：后端内联在路径里
rules:
- host: cafe.example.com
  http:
    paths:
    - path: /tea
      backend:
        service: { name: tea-svc, port: { number: 80 } }   # 直接写死服务

# VirtualServer：先声明 upstream 对象，路由再按名引用
upstreams:
- name: tea              # upstream 是一等公民，可挂 lb-method/healthCheck 等几十个字段
  service: tea-svc
  port: 80
routes:
- path: /tea
  action:
    pass: tea            # 路由通过名字引用 upstream，解耦
```

这个差异不是风格问题，而是**抽象层次**问题：VirtualServer 把「上游（upstream）」提升为一等对象，于是流量切分、健康检查、负载均衡方法、会话保持等都成了 upstream 的属性；路由（route）只负责「匹配条件 + 动作」，动作可以是 `pass`（转发）、`proxy`（转发并改写）、`redirect`（重定向）、`return`（直接返回）。这种分离让复杂路由组合变得自然。

VirtualServer 一个请求的处理流程（用户视角）：

```text
请求进来 (host 匹配到 VirtualServer)
  └─ 在 routes 里按 path 顺序匹配
       ├─ 命中某 route → 执行其 action（pass/proxy/redirect/return）
       │    └─ 若有 matches 条件子规则，优先按条件分流
       │    └─ 若有 splits，按权重切到多个 upstream
       └─ 落到对应 upstream（service + port → Pod）
```

#### 4.1.3 源码精读

先看标准 Ingress 的最小示例，注意 backend 是**内联**在每条 path 里的：

[examples/ingress-resources/complete-example/cafe-ingress.yaml:11-28](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/ingress-resources/complete-example/cafe-ingress.yaml#L11-L28) —— `rules[].http.paths[].backend.service` 直接写明 `tea-svc` / `coffee-svc` 和端口，路由与服务耦合在一起。

再看等价的 VirtualServer，注意 upstreams 与 routes 的**分离**：

[examples/custom-resources/basic-configuration/cafe-virtual-server.yaml:9-22](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/basic-configuration/cafe-virtual-server.yaml#L9-L22) —— 第 9–15 行声明两个命名 upstream（`tea`、`coffee`），第 16–22 行的两条 route 各自用 `action.pass: tea` / `coffee` 按名引用。

VirtualServer 的能力上限来自它的字段。几个关键字段（取自 CRD 文档）：

- **`upstreams`**（数组）：命名上游对象。[docs/crd/k8s.nginx.org_virtualservers.md:206](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualservers.md#L206) 起的几十个子字段里，包含了 `lb-method`、`healthCheck`（仅 Plus）、`sessionCookie`、`slow-start` 等标准 Ingress 完全无法表达的能力。
- **`routes[].action.proxy`**：转发并改写请求/响应头、重写 URI，见 [docs/crd/k8s.nginx.org_virtualservers.md:42](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualservers.md#L42)。
- **`routes[].matches`**：基于 header / cookie / argument / 变量的条件路由（内容路由），见 [docs/crd/k8s.nginx.org_virtualservers.md:83](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualservers.md#L83)（条件定义在 [L112-L117](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualservers.md#L112-L117)）。
- **`routes[].splits`**：流量切分，权重之和必须为 100，见 [docs/crd/k8s.nginx.org_virtualservers.md:159](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualservers.md#L159) 与权重约束 [L188](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualservers.md#L188)。
- **`routes[].path`**：支持前缀 `/`、最长前缀 `^~`、精确 `=`、正则 `~`/`~*`，见 [docs/crd/k8s.nginx.org_virtualservers.md:148](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualservers.md#L148)。

相比之下，标准 Ingress 的官方能力清单只有 host 路由、path 路由、TLS 终止三项：[README.md:84-91](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/README.md#L84-L91)。README 也明确把 VirtualServer 定位为 Ingress 的「能力扩展替代品」：[README.md:60-63](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/README.md#L60-L63)。

#### 4.1.4 代码实践

**目标**：亲手感受「内联后端」与「命名 upstream」两种结构，建立对照表。

**步骤**：

1. 打开两份示例文件，并排阅读：
   - [cafe-ingress.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/ingress-resources/complete-example/cafe-ingress.yaml)
   - [cafe-virtual-server.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/basic-configuration/cafe-virtual-server.yaml)
2. 在 VirtualServer 示例的某个 upstream 上**追加一个 Ingress 无法表达的字段**，例如给 `tea` upstream 加 `lb-method: round_robin`（合法取值见 [upstreams[].lb-method 文档](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualservers.md#L242)）。
3. 思考：如果你想给 Ingress 版本的 `tea-svc` 设同样的负载均衡方法，该改哪里？（答案：只能写注解 `nginx.org/lb-method`，无法在 spec 里结构化表达。）

**需要观察的现象**：VirtualServer 里 upstream 的字段是「键值对、有类型」，而 Ingress 的扩展能力只能藏在 annotations 的字符串里。

**预期结果**：你能用一句话说出「为什么上游能力只能放进 VS 而塞不进 Ingress spec」——因为 Ingress spec 没有 upstream 这个一等对象。

#### 4.1.5 小练习与答案

**练习 1**：下面哪个需求**无法**用标准 Ingress 实现、必须用 VirtualServer？
- (a) 把 `cafe.example.com/tea` 转发到 `tea-svc`
- (b) 把 `/tea` 的 10% 流量切到 `tea-svc-v2`、90% 切到 `tea-svc`
- (c) 为 `cafe.example.com` 配 TLS 证书

> **答案**：(b)。流量切分（splits）是 VirtualServer 独有能力，标准 Ingress 的 spec 里没有对应字段，注解也表达不了百分比权重。(a) 是 Ingress 的基本能力，(c) 是 Ingress 的 TLS 终止能力。

**练习 2**：在 [cafe-virtual-server.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/basic-configuration/cafe-virtual-server.yaml) 中，route 的 `action.pass: tea` 引用的 `tea` 这个名字，是在哪里定义的？如果删掉 `upstreams` 里 `name: tea` 那一段会发生什么？

> **答案**：引用的是同文件 `upstreams` 里 `name: tea` 的那个上游对象。删掉后，`tea` 这个名字没有对应 upstream，NGINX 会把该上游当作零端点处理——对 HTTP upstream 返回 502（依据见 [upstreams[].service 文档 L256](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualservers.md#L256)）。这正体现了「按名引用 + 命名唯一」的强约束。

---

### 4.2 VirtualServerRoute 组合

#### 4.2.1 概念说明

当一个主机名下的路由规则变得很多、或需要由不同团队/不同命名空间维护时，把所有 route 塞进一个 VirtualServer 会又长又难管。**VirtualServerRoute（VSR）** 解决的就是「拆分与复用」：

> The `VirtualServerRoute` resource defines a route that can be referenced by a `VirtualServer`. It enables modular configuration by allowing routes to be defined separately and referenced by multiple VirtualServers.
>
> 见 [docs/crd/k8s.nginx.org_virtualserverroutes.md:8-10](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualserverroutes.md#L8-L10)

可以把它理解成一个「路由片段」：VSR 自己不能独立对外提供服务（它没有完整的虚拟主机身份），它必须被某个 VirtualServer **引用**后，其 subroutes 才会被组装进那个主机的配置里。

理解 VSR 的关键三句话：

1. **VirtualServer 是「聚合者 / 宿主」**：它拥有 host、TLS、upstreams（宿主级别），并声明「这些路径段交给某个 VSR」。
2. **VirtualServerRoute 是「子路由提供者」**：它定义 `subroutes`（注意是 subroutes 不是 routes），以及自己用到的 upstreams。
3. **耦合约束**：VSR 的 `host` 必须与引用它的 VirtualServer 的 `host` 完全一致。

#### 4.2.2 核心流程

VirtualServer 引用 VSR 有两种方式（都写在 `routes[]` 里）：

```text
# 方式 A：按名字（可跨命名空间，写法 namespace/name 或 name）
routes:
- path: /tea
  route: default/tea          # 引用 default 命名空间里名为 tea 的 VSR

# 方式 B：按标签选择器（批量匹配，自动纳入所有匹配的 VSR）
routes:
- path: /
  routeSelector:
    matchLabels:
      app: coffee             # 所有带 app=coffee 标签的 VSR 都被纳入
```

引用关系形成一张「装配图」：

```text
VirtualServer (host=cafe.example.com, 拥有 TLS)
  ├─ route: path=/tea   ──引用──▶ VirtualServerRoute tea  (subroutes: /tea → upstream tea)
  └─ route: path=/      ──选择──▶ 所有 label app=coffee 的 VSR (subroutes: /coffee → upstream coffee)
```

注意一个细节：宿主 VirtualServer 的 `routes[]` 用 `path` 作为挂载点，而 VSR 内部用 `subroutes[].path` 定义真正的匹配路径——两者要协调好前缀关系。

#### 4.2.3 源码精读

宿主 VirtualServer 同时演示了两种引用方式：

[examples/custom-resources/basic-configuration-vsr/cafe-virtual-server.yaml:16-22](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/basic-configuration-vsr/cafe-virtual-server.yaml#L16-L22) —— 第 17–18 行用 `route: default/tea` 按名字引用 `tea` 这个 VSR；第 19–22 行用 `routeSelector.matchLabels.app: coffee` 按标签批量引用所有 coffee 相关 VSR。

被引用的 VSR 长这样（注意它定义的是 `subroutes`，且 `host` 与宿主一致）：

[examples/custom-resources/basic-configuration-vsr/tea-virtual-server-route.yaml:7-15](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/basic-configuration-vsr/tea-virtual-server-route.yaml#L7-L15) —— `host: cafe.example.com`，`subroutes` 里一条 `/tea → pass: tea`。带标签的版本见 [coffee-virtual-server-route.yaml:6-7](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/basic-configuration-vsr/coffee-virtual-server-route.yaml#L6-L7)，正是 `app: coffee` 标签让它被宿主的 `routeSelector` 选中。

跨命名空间的写法只是把 VSR 放到别的 namespace，引用时带上前缀。VSR 在 `coffee` 命名空间：[examples/custom-resources/cross-namespace-configuration/coffee-virtual-server-route.yaml:3-5](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/cross-namespace-configuration/coffee-virtual-server-route.yaml#L3-L5)（`namespace: coffee`），宿主则写 `route: coffee/coffee` 来引用它。

字段语义层面，宿主用 `routes[].route` 引用，见 [docs/crd/k8s.nginx.org_virtualservers.md:152](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualservers.md#L152)；标签选择器见 [docs/crd/k8s.nginx.org_virtualservers.md:153-158](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualservers.md#L153-L158)。VSR 自身的 `host` 一致性约束见 [docs/crd/k8s.nginx.org_virtualserverroutes.md:18](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualserverroutes.md#L18)，`subroutes` 定义见 [L20](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualserverroutes.md#L20)。

#### 4.2.4 代码实践

**目标**：追踪一次「宿主引用两个 VSR」的装配过程，区分两种引用方式。

**步骤**：

1. 打开 [basic-configuration-vsr/](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/basic-configuration-vsr/) 目录下的三个文件：`cafe-virtual-server.yaml`、`tea-virtual-server-route.yaml`、`coffee-virtual-server-route.yaml`。
2. 在纸上画出引用关系：宿主的两条 route 分别「指向谁」。
3. 做一个思想实验：如果再新增一个 `chocolate-virtual-server-route.yaml`，给它打上 `app: coffee` 标签，**宿主需要修改吗**？

**需要观察的现象**：`routeSelector` 这种方式下，新增带正确标签的 VSR 即可被自动纳入，宿主 VirtualServer 无需改动——这是「按标签松耦合装配」的价值。

**预期结果**：你能说清 `route: default/tea`（精确点名，一个）和 `routeSelector.matchLabels`（标签匹配，一批）在「谁能被纳入」上的差别。

#### 4.2.5 小练习与答案

**练习 1**：VSR 的 `spec.host` 写成 `tea.example.com`，而引用它的 VirtualServer 的 `host` 是 `cafe.example.com`，会怎样？

> **答案**：引用关系不成立。VSR 的 host 必须与引用它的 VirtualServer 的 host 完全一致（见 [VSR host 文档 L18](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualserverroutes.md#L18) 的描述「Must be the same as the host of the VirtualServer that references this resource」）。控制器会把它当作无效引用处理，该 VSR 的 subroutes 不会被装配。

**练习 2**：既然 VirtualServer 已经有 `routes`，为什么还要单独搞一个 VirtualServerRoute 的 `subroutes`？

> **答案**：为了**模块化与跨团队/跨命名空间复用**。一个团队负责 `tea` 服务，就可以独立维护自己的 VSR（连同它的 upstream、policy），而无需编辑中心化的 VirtualServer；多个 VirtualServer 也可以复用同一个 VSR 片段。`subroutes` 这个名字本身就在提示「它是被宿主 routes 装配进去的子路由」。

---

### 4.3 TransportServer 与 L4

#### 4.3.1 概念说明

到目前为止讲的都是 HTTP（L7）。但集群里还有大量**非 HTTP** 的服务——MySQL、Redis、DNS、自定义 TCP 协议……它们没有「路径」「请求头」这些概念，只有 TCP 连接或 UDP 数据报。**TransportServer** 就是给这类四层（L4）服务做负载均衡的资源：

> The `TransportServer` resource defines a TCP or UDP load balancer. It allows you to expose non-HTTP applications running in your Kubernetes cluster with advanced load balancing and health checking capabilities.
>
> 见 [docs/crd/k8s.nginx.org_transportservers.md:8-10](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_transportservers.md#L8-L10)

理解 TransportServer 有两个要点：

1. **它必须挂在某个 listener 上**。HTTP 默认监听 80/443，但 TCP/UDP 监听哪个端口、用什么协议，需要先在 GlobalConfiguration 里「注册」一个 listener，TransportServer 再通过 `listener.name` 引用它。这一步是 L4 资源与 L7 资源最大的结构差异。
2. **它的「路由」极简**：因为 L4 看不见 HTTP，所以没有 path、没有 matches，只有「这个 listener 收到的连接 → `action.pass` 到某个 upstream」。复杂度转移到了 upstream（负载均衡方法、健康检查、会话保持）和协议（TCP / UDP / TLS_PASSTHROUGH）上。

支持的协议有三种，覆盖典型 L4 场景：

| 协议 | 含义 | 典型用途 |
| --- | --- | --- |
| `TCP` | 透传 TCP 流，NGINX 做四层负载均衡 | MySQL、Redis、自定义 TCP |
| `UDP` | 转发 UDP 数据报 | DNS |
| `TLS_PASSTHROUGH` | 不解密，按 SNI（host）把 TLS 流原样转发到后端 | 后端自己做 TLS 终止的场景 |

#### 4.3.2 核心流程

一个 TransportServer 的装配链是「三级引用」：

```text
GlobalConfiguration                 ← 注册表：定义有哪些 listener（端口+协议）
  └─ listener dns-tcp (port 5353, TCP)
        ▲
        │ listener.name 引用
        │
TransportServer dns-tcp             ← 四层服务：声明用哪个 listener、转发到哪个 upstream
  └─ action.pass: dns-app
        ▲
        │ upstream 名字引用
        │
upstream dns-app (service: coredns, port: 5353)   ← 后端 Service
```

为什么必须经过 GlobalConfiguration？因为四层监听端口是「全局稀缺资源」——一个端口只能被一个 listener 占用，必须有一个集中注册表来管理「谁占了 5353」「谁占了 3306」，避免冲突。VirtualServer 的 HTTP 80/443 是约定俗成的默认值，所以不强制走 GlobalConfiguration（除非要用自定义端口，见 4.4）。

#### 4.3.3 源码精读

一个最简的 TCP TransportServer（DNS over TCP）：

[examples/custom-resources/basic-tcp-udp/transport-server-tcp.yaml:5-14](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/basic-tcp-udp/transport-server-tcp.yaml#L5-L14) —— `listener.name: dns-tcp` + `protocol: TCP` 引用 listener，`action.pass: dns-app` 转发到 upstream。

它引用的 listener 在 GlobalConfiguration 里注册：

[examples/custom-resources/basic-tcp-udp/global-configuration.yaml:7-13](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/basic-tcp-udp/global-configuration.yaml#L7-L13) —— 定义了 `dns-udp`（UDP 5353）和 `dns-tcp`（TCP 5353）两个 listener。注意它和 ConfigMap 同名（`nginx-configuration`），这是 NIC 的约定（具体绑定机制在第 4 单元讲）。

TLS_PASSTHROUGH 的写法多了 `host`（用于 SNI 区分）：

[examples/custom-resources/tls-passthrough/transport-server-passthrough.yaml:5-15](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/tls-passthrough/transport-server-passthrough.yaml#L5-L15) —— `protocol: TLS_PASSTHROUGH`，靠 `host: app.example.com` 做 SNI 匹配，连接不解密直接转发。

字段语义：TransportServer 的 listener 字段见 [docs/crd/k8s.nginx.org_transportservers.md:22-24](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_transportservers.md#L22-L24)，`action.pass` 见 [L18-L19](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_transportservers.md#L18-L19)，upstreams 见 [L38](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_transportservers.md#L38)。注意 `healthCheck` 在 TransportServer 的 upstream 里也是 **NGINX Plus 专属**（见 [L42](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_transportservers.md#L42)）——这是 OSS 与 Plus 能力差异的一个典型落点。

#### 4.3.4 代码实践

**目标**：把「GlobalConfiguration → TransportServer → upstream」三级引用走一遍，体会 L4 资源为什么必须有 listener 注册表。

**步骤**：

1. 读 [basic-tcp-udp/global-configuration.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/basic-tcp-udp/global-configuration.yaml)，数一下它注册了几个 listener、各自协议是什么。
2. 读同目录的 [transport-server-tcp.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/basic-tcp-udp/transport-server-tcp.yaml)，确认它的 `listener.name` 能在 GlobalConfiguration 里找到。
3. 做一个思想实验：如果再写第二个 TransportServer 也引用 `listener: dns-tcp`（同一个 TCP 5353 端口），会发生什么？

**需要观察的现象**：两个 TransportServer 抢同一个 listener/端口，意味着同一个 5353 端口上的 TCP 连接不知道该转发给谁。

**预期结果**：你能解释「为什么 L4 资源需要 GlobalConfiguration 这个集中注册表」——因为端口是全局排他的稀缺资源，必须集中管理避免冲突；而 L7 的 80/443 是默认共享入口，靠 host/path 在 HTTP 层分流，不需要抢占端口。（「两个 TS 抢同一端口」属于配置冲突，控制器的冲突检测机制在 [u3-l6](u3-l6-configuration-model.md) 讲。）

#### 4.3.5 小练习与答案

**练习 1**：TransportServer 没有 `routes` 和 `path` 字段，这是为什么？

> **答案**：因为它工作在四层，看不见 HTTP 的路径、方法、头部。L4 的「路由」退化为「listener 收到的连接 → action.pass 到某 upstream」这一层，没有按 URI 分流的可能，所以不需要 path。要按内容分流，必须用 L7 的 VirtualServer。

**练习 2**：`TLS_PASSTHROUGH` 协议的 TransportServer 为什么需要 `host` 字段，而 `TCP` 协议的不强制需要？

> **答案**：`TLS_PASSTHROUGH` 不解密 TLS，但要区分「这条加密连接该转给哪个后端」，唯一能不 解密就拿到 的信息是 TLS 握手里的 **SNI（Server Name Indication）**，也就是 host。所以它靠 `host` 做 SNI 匹配来路由（见 [transport-server-passthrough.yaml L9](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/tls-passthrough/transport-server-passthrough.yaml#L9)）。纯 TCP 没有这层信息，靠 listener（端口）区分即可，不强求 host。

---

### 4.4 Policy 与 GlobalConfiguration

#### 4.4.1 概念说明

剩下两个资源——**Policy** 和 **GlobalConfiguration**——都不是「路由」本身，而是路由的**两个解耦维度**：

- **GlobalConfiguration** 解耦的是「**在哪里监听**」：它是一张 listener 注册表，把「端口 + 协议」从工作负载里抽出来集中管理。TransportServer 必须用它（见 4.3），VirtualServer 也可选用它来开非标准端口的自定义 listener。
- **Policy** 解耦的是「**对流量施加什么策略**」：限流、认证、访问控制、WAF……这些能力如果每条路由各写一份会大量重复，Policy 让你「定义一次、到处引用」。

> The `GlobalConfiguration` resource defines global settings for the NGINX Ingress Controller. It allows you to configure listeners for different protocols and ports.
> 见 [docs/crd/k8s.nginx.org_globalconfigurations.md:8-10](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_globalconfigurations.md#L8-L10)

> The `Policy` resource defines a security policy for `VirtualServer` and `VirtualServerRoute` resources. It allows you to apply various policies such as access control, authentication, rate limiting, and WAF protection.
> 见 [docs/crd/k8s.nginx.org_policies.md:8-10](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_policies.md#L8-L10)

Policy 的设计哲学值得专门记住：**Ingress 时代靠注解做策略，CRD 时代靠 Policy 做策略**。注解是字符串、每资源一份、不可复用、跨控制器不通；Policy 是强类型 CRD、可被多个 VS/VSR 引用、是 NGINX 原生概念的直接映射。这是 NIC 从 Ingress 走向 VirtualServer 体系时，在「可扩展性」上的根本升级。

一个 Policy 的 spec 里**只能设一种策略类型**的字段（互斥），目前支持：`accessControl`、`rateLimit`、`jwt`、`oidc`、`externalAuth`、`basicAuth`、`apiKey`、`ingressMTLS`、`egressMTLS`、`waf`、`cors`、`cache`。

#### 4.4.2 核心流程

**Policy 的引用流**：

```text
Policy rate-limit-policy (spec.rateLimit: {...})     ← 定义一次
   ▲
   │ policies[].name 引用（在 VirtualServer 或 route 级别）
   │
VirtualServer
  ├─ spec.policies: [{name: rate-limit-policy}]        ← 对整个虚拟主机生效
  └─ routes:
     └─ path: /api
        policies: [{name: rate-limit-policy}]          ← 仅对该 route 生效（可覆盖宿主同名策略）
```

引用时若不写 `namespace`，默认取 VirtualServer 所在命名空间（见 [policies[].namespace 文档 L38](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualservers.md#L38)）。route 级的 policies 会覆盖宿主级同类型的策略（见 [routes[].policies 文档 L149](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualservers.md#L149)）。

**GlobalConfiguration 的引用流**：

```text
GlobalConfiguration nginx-configuration
  ├─ listener http-8083  (port 8083, HTTP)
  └─ listener https-8443 (port 8443, HTTP, ssl=true)
   ▲
   │ listener.http / listener.https 引用
   │
VirtualServer
  spec.listener: {http: http-8083, https: https-8443}   ← 让这个 VS 监听非标准端口
```

#### 4.4.3 源码精读

GlobalConfiguration 的全部内容就是一张 listener 表：

[docs/crd/k8s.nginx.org_globalconfigurations.md:18-24](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_globalconfigurations.md#L18-L24) —— 每个 listener 有 `name`（被引用的钥匙）、`port`、`protocol`、`ssl`，以及可选的 `ipv4`/`ipv6` 绑定地址。一个带 HTTP/HTTPS 自定义端口的真实 GlobalConfiguration见 [examples/custom-resources/custom-listeners/global-configuration.yaml:7-14](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/custom-listeners/global-configuration.yaml#L7-L14)。

VirtualServer 引用自定义 listener 的写法：

[examples/custom-resources/custom-listeners/cafe-virtual-server.yaml:6-8](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/custom-listeners/cafe-virtual-server.yaml#L6-L8) —— `listener.http: http-8083` 和 `listener.https: https-8443`，让这个 VS 不走默认 80/443，而是走 GlobalConfiguration 注册的自定义端口。字段语义见 [docs/crd/k8s.nginx.org_virtualservers.md:33-35](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualservers.md#L33-L35)。

Policy 的 spec 则是一组互斥的策略类型字段。以限流为例，关键字段在 [docs/crd/k8s.nginx.org_policies.md:121-139](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_policies.md#L121-L139)：`rateLimit.key`（按什么限流，如 `${binary_remote_addr}` 按客户端 IP）、`rateLimit.rate`（速率，如 `10r/s`）、`rateLimit.zoneSize`（共享内存区大小）、`rateLimit.burst`（突发量）。VirtualServer 一侧，引用入口是 [docs/crd/k8s.nginx.org_virtualservers.md:36-38](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualservers.md#L36-L38)。

注意一个重要的不对称：**Policy 只服务于 VirtualServer / VirtualServerRoute，不直接服务 TransportServer**（Policy 文档明确写了它是给 VS/VSR 用的）。TransportServer 的 L4 策略（如 TLS、负载均衡方法）直接写在它自己的 spec 里。这反映了 L7 与 L4 能力面的差异。

#### 4.4.4 代码实践

**目标**：体会「策略」与「监听点」这两个维度的解耦价值。

**步骤**：

1. 读 [docs/crd/k8s.nginx.org_policies.md](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_policies.md)，数一下 Policy spec 顶层有多少种策略类型字段（提示：从 `accessControl` 到 `waf`）。
2. 读 [examples/custom-resources/custom-listeners/global-configuration.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/custom-listeners/global-configuration.yaml)，对比它和 [basic-tcp-udp/global-configuration.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/basic-tcp-udp/global-configuration.yaml) 的 listener 协议差异（一个 HTTP/HTTPS，一个 TCP/UDP）。
3. 思想实验：假设你有 20 个 VirtualServer 都要做「按 IP 限流 10r/s」。用 Policy 需要定义几份？用 Ingress 注解需要写几遍？

**需要观察的现象**：Policy 定义 1 份、被 20 个 VS 引用即可；注解方案则要在 20 个 Ingress 上各写一遍同样的注解字符串。

**预期结果**：你能说清 Policy 相比注解的两个核心优势——**强类型**（字段有 schema 校验）与**可复用**（定义一次多处引用）。这正是 CRD 体系相对 Ingress 注解的本质升级。

#### 4.4.5 小练习与答案

**练习 1**：一个 Policy 的 spec 里同时写了 `rateLimit` 和 `jwt` 两段配置，合法吗？

> **答案**：不合法。Policy 的 spec 是一组**互斥**的策略类型字段，一个 Policy 只表达一种策略（这从字段语义可推断，每种策略对应一个独立的 CRD 对象）。要做限流 + JWT，应定义两个 Policy（如 `rate-limit-policy` 和 `jwt-policy`），然后在同一个 VirtualServer 的 `policies` 数组里同时引用两个。具体的互斥校验逻辑在 [u2-l3](u2-l3-validation-and-codegen.md) 讲。

**练习 2**：GlobalConfiguration 既是 TransportServer 的必需依赖，又能被 VirtualServer 引用（自定义 listener）。这两者引用它的方式一样吗？

> **答案**：不一样。TransportServer 用 `listener.name` + `listener.protocol` 引用**单个** listener（因为 L4 一个 TS 占一个端口）；VirtualServer 用 `listener.http` / `listener.https` 引用**一对** listener（分别对应明文和加密入口，见 [custom-listeners/cafe-virtual-server.yaml L6-L8](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/custom-listeners/cafe-virtual-server.yaml#L6-L8)）。引用方式不同，反映的是 L4「单端口透传」与 L7「HTTP/HTTPS 双入口」的本质差异。

---

## 5. 综合实践

**任务**：为同一个 cafe 应用（`tea-svc`、`coffee-svc`，域名 `cafe.example.com`）分别写一份标准 Ingress 和一份 VirtualServer，让两者实现**完全等价**的路径路由，然后对比写法差异，并各追加一项「对方做不到」的能力。

**步骤**：

1. **Ingress 版本**。参照 [cafe-ingress.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/ingress-resources/complete-example/cafe-ingress.yaml)，写出 `/tea → tea-svc:80`、`/coffee → coffee-svc:80` 的标准 Ingress（`apiVersion: networking.k8s.io/v1`）。
2. **VirtualServer 版本**。参照 [cafe-virtual-server.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/basic-configuration/cafe-virtual-server.yaml)，写出等价路由（`apiVersion: k8s.nginx.org/v1`）。
3. **填对照表**，至少覆盖四列：`apiVersion/kind`、`后端声明方式`、`路由引用方式`、`TLS 写法`。
4. **能力对比**：
   - 给 VirtualServer 版本追加一个 Ingress 做不到的能力：在 `/tea` 上做 10/90 流量切分（参照 [splits 文档 L159](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualservers.md#L159) 与 [weight 约束 L188](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_virtualservers.md#L188)，权重之和必须为 100）。
   - 反过来，指出 Ingress 版本相对 VirtualServer 的一个**优势**（提示：可移植性——同一份 Ingress 能被别的控制器消费，VirtualServer 不行）。
5. **（可选，需要集群）** 把两份 YAML 分别 `kubectl apply`，用 `curl -H 'Host: cafe.example.com' http://<ingress-ip>/tea` 验证两者都能正确路由到 tea。**若无集群，标注「待本地验证」即可，不要假装已运行。**

**预期产出**：一份对照表 + 两段 YAML + 一句话结论，说明「什么时候该选 Ingress、什么时候该选 VirtualServer」。

> 参考结论方向：需要跨控制器可移植、路由简单 → 选 Ingress；需要流量切分 / 内容路由 / 头部改写 / 结构化策略，且锁定 NGINX → 选 VirtualServer。

## 6. 本讲小结

- **Ingress = 可移植但能力窄；VirtualServer = 锁定 NGINX 但能力全**。前者后端内联在 path、靠注解扩展；后者把 upstream 提升为一等对象、route 按名引用，原生支持流量切分、内容路由、头部改写。
- **VirtualServerRoute 是可被 VirtualServer 装配的「子路由片段」**，支持按名字（可跨命名空间）和按标签选择器两种引用方式，用于模块化与跨团队复用；其 `host` 必须与宿主一致。
- **TransportServer 是四层（L4）负载均衡**，服务 TCP/UDP/TLS_PASSTHROUGH 等非 HTTP 协议；它必须引用 GlobalConfiguration 注册的 listener，因为端口是全局稀缺资源。
- **GlobalConfiguration 解耦「在哪里监听」**——一张 listener 注册表（name+port+protocol），TransportServer 必需、VirtualServer 可选（用于自定义端口）。
- **Policy 解耦「施加什么策略」**——强类型、可复用的 CRD，被 VS/VSR 引用；它是 Ingress 注解方案的结构化升级，一个 Policy 只表达一种策略类型。
- **L7 与 L4 的不对称贯穿始终**：Policy 只服务 VS/VSR，TransportServer 的策略直接写在自身 spec；引用 listener 的方式也不同（L4 单端口 vs L7 HTTP/HTTPS 双入口）。

## 7. 下一步学习建议

本讲建立了**用户视角的资源模型**。下一步有两条路：

- **向下看数据真相**：[u2-l2 CRD 类型定义源码：types.go](u2-l2-crd-type-definitions.md) 会带你进入 `pkg/apis/configuration/v1/types.go`，看这些 CRD 字段的 Go 结构体与 kubebuilder 标注源头，理解「YAML 字段 → Go 类型」的映射。
- **向右看校验与生成**：[u2-l3 CRD 校验与代码生成链路](u2-l3-validation-and-codegen.md) 讲这些字段如何被校验、CRD YAML 如何自动生成。
- **继续向右看策略模型**：[u2-l4 Policy CRD 与可复用策略模型](u2-l4-policy-crd-model.md) 深入 Policy 的 selector 与引用机制。

如果你更想看「这些资源被创建后，控制器如何感知并处理它们」，可以直接跳到第 3 单元 [u3-l1 LoadBalancerController 生命周期](u3-l1-controller-lifecycle.md)，但建议先完成 u2-l2，把数据模型这层夯实。
