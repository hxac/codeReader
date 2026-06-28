# 项目概览：NGINX Gateway Fabric 是什么

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标只有一个：**让你在进入任何源码之前，先建立对 NGINX Gateway Fabric（下文简称 NGF）的清晰认知**。

学完本讲，你应该能够：

1. 用一句话说清 NGF 是什么、它解决什么问题。
2. 认出 Gateway API 的核心资源（`GatewayClass`、`Gateway`、各种 `Route`），并理解它们之间的关系。
3. 区分「控制面」与「数据面」，并说清 NGINX Agent 在两者之间扮演的角色。
4. 看懂 NGF 的版本兼容性矩阵，知道如何挑选适合自己集群的 NGF / Gateway API / NGINX 版本。

本讲**不要求你会写 Go**，也不要求你懂 Kubernetes 控制器的内部原理。我们只建立「地图」，后续讲义再带你逐层深入源码。

## 2. 前置知识

本讲假设你大致了解下面几个概念（不熟也没关系，我们会顺带复习）：

- **Kubernetes（K8s）**：一个容器编排系统。在 K8s 里，一切被管理的对象都叫「资源（Resource）」，比如 `Pod`、`Service`、`Deployment`。
- **CRD（CustomResourceDefinition，自定义资源定义）**：K8s 允许你「自己定义新的资源类型」。Gateway API 的 `Gateway`、`HTTPRoute` 就是通过 CRD 加进 K8s 的，用法和内置资源一样（`kubectl get gateway`）。
- **控制器模式（Controller Pattern）**：「观察实际状态 → 对比期望状态 → 采取行动让两者一致」的循环。K8s 里几乎所有自动化都遵循这个模式。
- **Ingress**：K8s 内置的、把集群外流量导入集群内服务的老规范。它功能比较简单，Gateway API 正是它的「下一代替代品」。
- **NGINX**：一个久经考验的开源 Web 服务器 / 反向代理 / 负载均衡器，以稳定、高性能著称。

> 一句话回顾：**「用户用 YAML 声明想要的流量规则」→「某个控制器读取这些 YAML」→「控制器把规则翻译成真正能处理流量的软件（比如 NGINX）的配置」**。NGF 就是这条链路上的一套实现。

## 3. 本讲源码地图

本讲涉及的文件都很轻量，重点是「读懂项目自述与配置」，几乎不涉及复杂逻辑：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/README.md) | 项目的「自我介绍」：定位、支持的资源、版本矩阵、安装入口。 |
| [go.mod](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/go.mod) | Go 模块定义文件，能看出技术栈与关键依赖（controller-runtime、Gateway API、NGINX Agent、gRPC 等）。 |
| [docs/developer/design-principles.md](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/design-principles.md) | 设计原则，解释「为什么 NGF 要做成这样」。 |
| [cmd/gateway/main.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/main.go) | 程序入口，能看到 `gateway` 二进制有哪些子命令。 |
| [cmd/gateway/commands.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go) | 各子命令的 flag 定义，含控制器名称的取值规则。 |
| [internal/controller/config/config.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go) | 控制面的运行时配置结构，能看到 `GatewayCtlrName` 字段。 |
| [deploy/default/deploy.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/deploy/default/deploy.yaml) | 默认部署清单，能看到控制面 Deployment、`--gateway-ctlr-name` flag 和 GatewayClass 的真实写法。 |

> 说明：后三个文件是「锦上添花」的真实源码锚点，帮你把抽象概念落到具体代码与 YAML 上，不必现在就完全看懂。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 Gateway API 与 NGF 的定位**：NGF 到底实现了什么。
- **4.2 控制面 / 数据面分离**：NGF 的整体架构，以及 NGINX Agent 的作用。
- **4.3 版本与兼容性矩阵**：如何挑选合适的版本组合。

### 4.1 Gateway API 与 NGF 的定位

#### 4.1.1 概念说明

**Gateway API** 是 Kubernetes 官方（SIG Network）推出的一套「流量入口」规范，用一组 CRD 来描述「外部流量如何进入集群、如何被路由到后端服务」。它是老 `Ingress` 资源的下一代替代，相比 Ingress，它更**面向角色、表达力更强、可移植**。

Gateway API 的核心资源可以分成三层：

| 层级 | 资源 | 类比 | 职责 |
| --- | --- | --- | --- |
| 实现层 | `GatewayClass` | `StorageClass` / `IngressClass` | 声明「这一类 Gateway 由哪个控制器实现」。 |
| 实例层 | `Gateway` | 一台负载均衡器实例 | 声明「我要一个监听若干端口/协议/域名的入口」，由若干 `Listener` 组成。 |
| 路由层 | `HTTPRoute` / `GRPCRoute` / `TCPRoute` / `TLSRoute` / `UDPRoute` | 路由表 | 声明「匹配某条件的请求，转发到哪个后端（`backendRefs` → Service）」，并通过 `parentRefs`「挂」到某个 `Gateway` 上。 |

**NGF 的定位**，在 README 开篇就说得很直白：它是 Gateway API 的一个**开源实现**，并且**用 NGINX 作为数据面**：

> [README.md:L12-L16](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/README.md#L12-L16) —— NGF 实现了 Gateway API 的核心资源（`Gateway`、`GatewayClass`、`HTTPRoute`、`GRPCRoute`、`TCPRoute`、`TLSRoute`、`UDPRoute`），用来把 NGINX 配置成 HTTP 或 TCP/UDP 的负载均衡器、反向代理或 API 网关。

换句话说：**Gateway API 是「图纸规范」，NGF 是「把图纸变成真正跑起来的 NGINX」的那家施工队。** 用户写 Gateway API 的 YAML，NGF 负责把它翻译成 NGINX 能听懂的配置。

#### 4.1.2 核心流程

从「用户声明」到「流量被处理」，NGF 背后的逻辑链是：

```text
1. 用户创建 GatewayClass（指向 NGF 这个实现）
2. 用户创建 Gateway（声明监听器 Listener：端口/协议/域名）
3. 用户创建 HTTPRoute 等（声明路由规则，parentRefs 指向上面的 Gateway）
4. NGF 控制面 watch 到这些资源
5. NGF 校验 + 拼装出一份「内部配置」
6. NGF 把配置翻译成 NGINX 配置文件
7. 配置被下发到数据面，NGINX reload 生效
8. 外部请求进入 NGINX，按规则转发到后端 Service → Pod
```

其中第 1 步里 `GatewayClass` 指向「哪个控制器」，是理解 NGF 身份的关键（见下面的源码精读）。

#### 4.1.3 源码精读

**① 控制器名称（controllerName）——NGF 的「身份证」**

GatewayClass 通过 `spec.controllerName` 告诉集群「我这个类由谁实现」。NGF 把自己的控制器域名硬编码为 `gateway.nginx.org`：

> [cmd/gateway/commands.go:L30](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L30) —— `domain = "gateway.nginx.org"`，这是控制器名称必须使用的域名前缀。

启动控制面时，通过 `--gateway-ctlr-name` flag 传入完整控制器名（默认 `gateway.nginx.org/nginx-gateway-controller`），它最终被存进运行时配置结构：

> [cmd/gateway/commands.go:L34-L36](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L34-L36) —— `gateway-ctlr-name` flag 的定义，要求形如 `DOMAIN/PATH`。
>
> [internal/controller/config/config.go:L32-L33](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L32-L33) —— `GatewayCtlrName string` 字段，控制面用它判断「哪些 GatewayClass 归我管」。

**② 真实部署里的写法**

在默认部署清单里，你能同时看到 flag 和 GatewayClass 两处都用了同一个控制器名，从而「对上号」：

> [deploy/default/deploy.yaml:L335-L339](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/deploy/default/deploy.yaml#L335-L339) —— 控制面容器以 `controller` 子命令启动，传入 `--gateway-ctlr-name=gateway.nginx.org/nginx-gateway-controller` 和 `--gatewayclass=nginx`。
>
> [deploy/default/deploy.yaml:L456-L464](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/deploy/default/deploy.yaml#L456-L464) —— 名为 `nginx` 的 `GatewayClass`，其 `spec.controllerName` 正是 `gateway.nginx.org/nginx-gateway-controller`，与上面的 flag 一致。

> 注意：这个 GatewayClass 还带了一个 `parametersRef` 指向 `NginxProxy`（NGF 自定义的参数资源）。这是 NGF 存放「实现专属全局配置」的地方，后续讲义会专门讲，现在只需知道「GatewayClass 还能挂参数」。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「GatewayClass 的 controllerName」和「控制面启动 flag」是如何对应上的，从而理解 NGF 的身份是怎么被识别的。

**操作步骤**：

1. 打开 [deploy/default/deploy.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/deploy/default/deploy.yaml)。
2. 搜索 `controllerName:`，记录它的值（应为 `gateway.nginx.org/nginx-gateway-controller`）。
3. 再搜索 `gateway-ctlr-name`，记录控制面容器传给它的值。
4. 比较两者：它们必须相等，NGF 才会认领这个 GatewayClass。

**需要观察的现象**：两处字符串完全一致。

**预期结果**：你会理解——如果把 GatewayClass 的 `controllerName` 改成别的值，NGF 就不再管理它（它会被「晾在那」）。这就是 GatewayClass 实现「多控制器共存」的方式。

> 待本地验证：若你已部署 NGF，可尝试 `kubectl get gatewayclass nginx -o yaml` 查看 `.spec.controllerName`，并对照控制面 Pod 的启动参数（`kubectl get deploy nginx-gateway -o yaml` 里对应容器的 args）。

#### 4.1.5 小练习与答案

**练习 1**：Gateway API 中的 `Gateway` 和 `GatewayClass`，哪个更像「蓝图规范」、哪个更像「一台真实设备」？

> **参考答案**：`GatewayClass` 更像规范/类别（声明「由哪个实现负责」，集群级别，通常只创建一次）；`Gateway` 更像一台真实的、会实际监听端口的负载均衡器实例。

**练习 2**：如果一个 `HTTPRoute` 想被某个 `Gateway` 处理，它靠哪个字段建立联系？

> **参考答案**：靠 `parentRefs`（HTTPRoute 的 `spec.parentRefs`）指向目标 `Gateway`。`GatewayClass` 只决定「Gateway 由谁实现」，不直接决定 Route 挂在哪里。

---

### 4.2 控制面 / 数据面分离

#### 4.2.1 概念说明

NGF 的整体架构遵循一个非常经典的划分：

- **控制面（Control Plane）**：负责「思考」。它 watch Kubernetes 里的 Gateway API 资源，做校验、拼装、翻译，最终生成 NGINX 的配置文件。NGF 的控制面就是那个用 Go 写的 `gateway` 二进制。
- **数据面（Data Plane）**：负责「干活」。真正接收客户端请求、做负载均衡、转发到后端的，是 **NGINX**（可选开源版 OSS 或商业版 NGINX Plus）。数据面不关心 YAML，只关心自己的配置文件。

把两者分开的好处是：控制面可以随时重启、升级、甚至短暂出错，而数据面（NGINX）继续按既有配置转发流量，**数据路径与控制路径解耦，互不拖累**。

那么控制面生成的配置，怎么送到数据面的 NGINX 手里？这就轮到 **NGINX Agent** 出场了：

> [README.md:L23](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/README.md#L23) —— 「NGINX Gateway Fabric uses NGINX Agent to configure NGINX.」

**NGINX Agent** 是一个独立项目（[nginx/agent](https://github.com/nginx/agent)），运行在数据面侧，专门负责「接收配置 → 写盘 → 让 NGINX reload」。它把控制面与 NGINX 之间的「配置下发」这件事标准化了，控制面不需要自己去 SSH 进数据面改文件。

设计原则文档也强调了 NGINX 作为数据面的定位：

> [docs/developer/design-principles.md:L1-L9](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/design-principles.md#L1-L9) —— NGF 以 NGINX 为数据面技术，目标是成为集群流量进出（ingress/egress）的基础设施组件，并继承 NGINX 稳定、高性能、安全、功能丰富的优点。

#### 4.2.2 核心流程

控制面与数据面的协作流程：

```text
┌─────────────── 控制面 (Go: gateway controller) ───────────────┐
│  watch Gateway API 资源 → 生成 NGINX 配置文件                    │
│  内嵌一个 gRPC 服务端，等待 Agent 连接                            │
└──────────────────────────────┬─────────────────────────────────┘
                               │ gRPC + mTLS（双向证书认证）
                               ▼
┌─────────────── 数据面 (NGINX + NGINX Agent) ───────────────────┐
│  NGINX Agent（gRPC 客户端）连接控制面                            │
│  收到配置文件 → 写到磁盘 → 触发 NGINX reload                      │
│  NGINX 处理真实客户端流量 → 转发到后端 Service                     │
└─────────────────────────────────────────────────────────────────┘
```

要点：

1. 控制面是配置的「生产者」，数据面 NGINX 是配置的「消费者」。
2. NGINX Agent 是两者之间的「快递员 + 安装工」。
3. 这条通信链路用 **mTLS（双向 TLS）** 保护，控制面启动时通过 `--agent-tls-secret=agent-tls`、`--server-tls-domain` 等参数配置证书（详见 4.1.3 引用的部署清单附近，及后续 u2-l3「初始化与证书生成」讲义）。

#### 4.2.3 源码精读

**① 控制面入口：`gateway` 二进制的子命令**

控制面是一个 Go 程序，入口在 `cmd/gateway/main.go`。`main()` 组装了一个根命令，并挂上五个子命令：

> [cmd/gateway/main.go:L20-L35](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/main.go#L20-L35) —— 根命令下挂了 `controller`、`generate-certs`、`initialize`、`sleep`、`endpoint-picker` 五个子命令。其中 `controller` 才是「真正的控制面主进程」，其余是辅助命令（生成证书、init 容器初始化、推理扩展等）。

其中 `controller` 子命令就是控制面本体。在默认部署里，控制面容器正是用它启动的（见 4.1.3 引用的 [deploy/default/deploy.yaml:L335-L345](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/deploy/default/deploy.yaml#L335-L345)，`args` 第一项就是 `controller`）。

**② 控制面产品逻辑所在包**

> [internal/controller/doc.go:L1-L4](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/doc.go#L1-L4) —— `package controller` 注释说明：这个包包含 NGF 控制器实现相关的所有子包。后续讲义的「事件处理、图构建、配置生成、Agent 通信」都在 `internal/controller/` 之下。

**③ 关键依赖印证架构**

go.mod 里的依赖能从侧面印证这套架构：

> [go.mod:L1-L3](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/go.mod#L1-L3) —— 模块名 `github.com/nginx/nginx-gateway-fabric/v2`，Go 版本 `1.26.0`。
>
> [go.mod:L16](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/go.mod#L16) —— 依赖 `github.com/nginx/agent/v3 v3.11.2`：直接引用 NGINX Agent SDK（用于与数据面 Agent 的 gRPC 通信协议）。
>
> [go.mod:L27](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/go.mod#L27) —— `google.golang.org/grpc`：控制面与 Agent 走 gRPC。
>
> [go.mod:L36-L37](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/go.mod#L36-L37) —— `sigs.k8s.io/controller-runtime`（控制器框架）与 `sigs.k8s.io/gateway-api`（Gateway API 类型）。

#### 4.2.4 代码实践

**实践目标**：通过「读 flag」建立对控制面职责的直觉——它对外暴露哪些端口、和谁通信。

**操作步骤**：

1. 打开 [deploy/default/deploy.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/deploy/default/deploy.yaml)，定位 4.1.3 里引用的 `controller` 容器 args 段落（L335 附近）。
2. 列出其中所有以 `--` 开头的 flag，按用途分类：
   - 身份相关：`--gateway-ctlr-name`、`--gatewayclass`、`--config`
   - 与数据面通信相关：`--agent-tls-secret`、`--server-tls-domain`
   - 对外暴露端口：`--metrics-port`、`--health-port`
   - 一致性相关：`--leader-election-lock-name`
3. 对每个 flag 用一句话写出「它大概影响什么」。

**需要观察的现象**：你会发现控制面的启动参数几乎覆盖了「我是谁、我管谁、怎么和 Agent 安全通信、怎么被监控、怎么选举」这几件事。

**预期结果**：你能凭这份 flag 清单，把控制面的「对外接口」画成一张小图。这是后续 u2-l2「controller 命令与运行时配置」讲义的预习。

> 待本地验证：flag 的完整含义以 `cmd/gateway/commands.go` 里每个 flag 的 usage 文字为准；本实践只做粗略分类，不求精确。

#### 4.2.5 小练习与答案

**练习 1**：如果 NGINX Agent 进程挂了，但 NGINX 本身还在跑，现有流量会立刻断吗？新配置还能生效吗？

> **参考答案**：现有流量一般不会立刻断——NGINX 仍按内存里的当前配置转发。但新配置无法被下发应用（因为「快递员」没了），直到 Agent 恢复并重新连接控制面。这正是控制面/数据面解耦的好处。

**练习 2**：go.mod 里为什么需要 `github.com/nginx/agent/v3` 这个依赖？控制面又不去数据面跑 Agent。

> **参考答案**：控制面需要与数据面 Agent 用 gRPC「讲同一种语言」。引用 Agent SDK 通常是为了复用 gRPC 的 protobuf 消息定义与服务接口，使控制面能正确地实现/调用与 Agent 对接的那一端协议。

---

### 4.3 版本与兼容性矩阵

#### 4.3.1 概念说明

NGF 不是孤立的，它依赖一整套外部组件：Gateway API 规范、Kubernetes 集群、NGINX（OSS 或 Plus）、NGINX Agent，以及（可选的）F5 WAF。这些组件各自有版本，**并非任意组合都兼容**。因此 README 提供了一张「技术规格矩阵」，告诉你「某个 NGF 版本配套哪些版本」。

理解这张表，能帮你回答两个实际问题：

- 我的集群是 Kubernetes 1.31，能用哪个 NGF？
- 我想用 NGINX Plus R37，需要装哪个 NGF？

#### 4.3.2 核心流程

挑选版本组合的思路：

```text
1. 先确定你想要的「能力档位」：用开源 NGINX 还是 NGINX Plus？要不要 WAF？
2. 在 README 矩阵里找到提供这些能力的 NGF 版本行。
3. 核对：该行要求的 Kubernetes 版本 ≤ 你的集群版本；Gateway API 版本与你已装的 CRD 匹配。
4. 优先选「最新稳定版（latest release）」用于生产；「edge」仅用于尝鲜。
```

README 把发布分成两档：

- **Latest release（当前为 2.6.5）**：面向生产。
- **Edge**：主线最新提交构建，用于实验新功能。

> [README.md:L34-L44](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/README.md#L34-L44) —— 发布说明：最新稳定版是 2.6.5；edge 版本用于体验尚未发布的新特性。

NGF 采用**语义化版本（Semantic Versioning）**：`MAJOR.MINOR.PATCH`。在主版本号为 0 的阶段，公开 API 尚不稳定、随时可能变化。

#### 4.3.3 源码精读

**① 版本矩阵本体**

> [README.md:L69-L72](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/README.md#L69-L72) —— 技术规格表头与头两行数据（Edge 行与 2.6.5 行）。以 Edge 行为例：配套 Gateway API `1.5.1`、Kubernetes `1.31+`、NGINX OSS `1.31.2`、NGINX Plus `R37.0`、NGINX Agent `v3.11.2`、F5 WAF `5.13.2`。

> 提示：本讲义的 HEAD 对应仓库的 edge 主线，因此 go.mod 里 `github.com/nginx/agent/v3 v3.11.2`（[go.mod:L16](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/go.mod#L16)）与矩阵 Edge 行的 Agent 版本一致；稳定版 2.6.5 行则是 `v3.11.1`。

**② 版本号在代码里如何体现**

`gateway` 二进制的版本号是**构建期注入**的变量（不是写死在源码里）：

> [cmd/gateway/main.go:L9-L18](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/main.go#L9-L18) —— `version`、`telemetryReportPeriod`、`telemetryEndpoint` 等变量注释为 `Set during go build.`，在编译时通过 `-ldflags` 注入。所以你在源码里看不到 `version = "2.6.5"`，它是由构建流程（Makefile）在打包镜像时填进去的。

#### 4.3.4 代码实践

**实践目标**：学会「用矩阵反查依赖」，能为一组需求挑出合适的 NGF 版本。

**操作步骤**：

1. 打开 [README.md:L69-L83](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/README.md#L69-L83)（技术规格表）。
2. 假设你的场景是：**Kubernetes 1.30 集群 + 开源 NGINX + 不需要 WAF**。逐行核对「Kubernetes」列（要求 `1.31+` 还是 `1.25+`），找出所有可用的 NGF 版本。
3. 再假设场景改为：**Kubernetes 1.32 + NGINX Plus R37 + 需要 WAF**。找出可用的 NGF 版本行。

**需要观察的现象**：旧版本（如 1.x）支持的 Kubernetes/NGINX 版本明显更老，且大多没有 WAF 列（`---`）。

**预期结果**：你会发现「版本越新，支持的组件版本也越新、能力也越多（如 WAF）」，但旧集群可能只能用旧 NGF。这就是矩阵的价值——**先确定环境约束，再反查 NGF**。

#### 4.3.5 小练习与答案

**练习 1**：`edge` 版本和 `2.6.5` 版本，哪个更适合生产？为什么？

> **参考答案**：`2.6.5`（latest release）更适合生产，因为它是稳定发布版；`edge` 是主线最新提交构建的实验版本，可能包含未稳定的新特性，README 明确说它「用于实验」。

**练习 2**：为什么源码里搜不到 `version = "2.6.5"` 这样的赋值？

> **参考答案**：版本号是构建期注入的变量（[cmd/gateway/main.go:L9-L18](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/main.go#L9-L18)），由 Makefile 在 `go build` 时通过 `-ldflags` 写入，这样同一个源码树可以打出不同版本号的产物。

## 5. 综合实践

**综合任务**：结合本讲三个模块，写一份「一页纸 NGF 速览」。

请完成下面三件事，全部基于本讲引用的真实文件：

1. **定位**（对应 4.1）：用你自己的话写一段 3–5 句的话，回答实践任务的核心问题——
   - **NGF 相比一个普通 Ingress Controller 多了什么？**
   - **为什么需要 GatewayClass？**
   要求：至少引用 Gateway API 的一种 Route 资源名，并提到「控制器名 `gateway.nginx.org/nginx-gateway-controller`」这一身份机制。
2. **架构图**（对应 4.2）：画一张包含「控制面 / NGINX Agent / NGINX 数据面」三者的关系图，标注它们之间的通信协议（gRPC + mTLS）和数据流方向（谁生产配置、谁消费配置）。
3. **版本选型**（对应 4.3）：自选一个假想集群环境（写明 K8s 版本、是否用 Plus、是否要 WAF），从矩阵中选出合适的 NGF 版本，并写出你排除其他版本的理由。

**参考要点（不是标准答案，供你对照思路）**：

- NGF 多出来的核心是「实现了表达力更强的 Gateway API（多协议、角色分离、可移植），并把 NGINX 当作成熟数据面」；普通 Ingress Controller 通常只实现功能有限的 Ingress 规范。
- 需要 GatewayClass 是为了让「规范」与「实现」解耦，支持多控制器共存；NGF 只认领 `controllerName` 与自身 `--gateway-ctlr-name` 匹配的 GatewayClass。
- 架构图里：控制面生产配置 → 经 gRPC/mTLS → NGINX Agent 收配置并写盘 → NGINX reload 后消费配置处理流量。

## 6. 本讲小结

- NGF 是 **Gateway API 的开源实现**，目标是把 NGINX 配置成 K8s 的流量入口（负载均衡/反向代理/API 网关）。
- Gateway API 的核心资源分三层：**`GatewayClass`（实现归属）→ `Gateway`（监听实例）→ `HTTPRoute`/`GRPCRoute`/`TCPRoute`/`TLSRoute`/`UDPRoute`（路由规则）**。
- NGF 遵循**控制面 / 数据面分离**：Go 控制面负责翻译配置，NGINX 负责处理真实流量，**NGINX Agent** 负责通过 gRPC（mTLS）把配置下发到数据面并 reload。
- NGF 的「身份证」是控制器名 **`gateway.nginx.org/nginx-gateway-controller`**，由 `--gateway-ctlr-name` flag 传入，需与 GatewayClass 的 `spec.controllerName` 一致。
- 版本矩阵把 NGF 与 **Gateway API / Kubernetes / NGINX(OSS/Plus) / NGINX Agent / F5 WAF** 的兼容关系列清楚，选型时「先定环境、再反查 NGF」。
- 生产用 **latest release（2.6.5）**，尝鲜用 **edge**；版本号是构建期注入的，源码里搜不到硬编码。

## 7. 下一步学习建议

本讲只建立了「地图」。接下来建议：

1. **下一讲 u1-l2《目录结构与代码组织》**：动手把仓库目录树摸清楚，区分入口（`cmd`）、控制面产品逻辑（`internal/controller`）、可复用框架（`internal/framework`），为读源码打好基础。
2. **紧接着 u1-l3《构建与本地运行方式》**：用 Makefile 在本地 / kind 集群里真正把 NGF 跑起来，亲眼看到控制面与数据面工作。
3. 在进入进阶层（u2 起）之前，**务必先把项目跑通**——后面讲义会频繁出现「跟着代码走」，有一个能跑的环境会让你事半功倍。

> 延伸阅读（可选）：[Gateway API 官方文档](https://gateway-api.sigs.k8s.io/) 了解规范的完整设计；[docs.nginx.com 的架构页](https://docs.nginx.com/nginx-gateway-fabric/overview/gateway-architecture/) 看官方对控制面/数据面的图示。
