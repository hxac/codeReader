# NGINX Ingress Controller 是什么：项目定位与整体架构

## 1. 本讲目标

本讲是整个学习手册的第一篇，目标是让你在**不写一行代码、不读任何实现细节**的前提下，先建立起对整个项目的心智模型。读完本讲，你应当能够：

- 用一句话说清 **Ingress** 和 **Ingress Controller** 各自是什么、它们如何分工。
- 说清 **本项目（nginxinc/kubernetes-ingress，下称 NIC）** 与社区版 `kubernetes/ingress-nginx` 的关键区别，避免在文档和镜像之间混淆。
- 列出 NIC 支持的几类路由资源（Ingress / VirtualServer / VirtualServerRoute / TransportServer），并知道 Policy、GlobalConfiguration 在整体模型中的角色。
- 看懂架构文档里描述的「五层架构」与「从 `kubectl apply` 到 NGINX reload」的数据流骨架，为后续逐层深入打下地图。

> 提示：本讲只讲「它是什么、整体怎么运作」，不讲具体实现。源码精读从第 3 单元（控制器）和第 4 单元（配置生成）才真正开始。

## 2. 前置知识

本讲面向初学者，但有几个概念最好先有点印象：

- **Kubernetes（K8s）**：一个容器编排系统，用它来部署和运行应用。应用在 K8s 里通常以 **Pod** 为最小单位运行，多个同类 Pod 会被抽象成一个 **Service**（带稳定 IP 和 DNS 名字的一组 Pod）。
- **Service 的局限**：Service 默认只能给集群内部或同集群访问使用，集群外面的用户访问不到。要把应用暴露到集群外，就需要一个「入口」。
- **NGINX**：一个高性能的 HTTP 服务器和反向代理。它通过读取一个文本配置文件（`nginx.conf`）来决定「收到请求后转发到哪里」。
- **反向代理 / 七层（L7）负载均衡**：在 HTTP 层面（看得见 URL、Host、Header）把请求分发到后端，相对的是「四层（L4）」负载均衡，它只看 IP 和端口。
- **控制器（Controller）模式**：K8s 里大量使用的一种编程范式——「让实际状态不断逼近用户声明的期望状态」。本讲会解释这一点，无需提前掌握。

如果你对 NGINX 配置或 K8s Service 完全陌生也没关系，本讲会用类比讲清楚。

## 3. 本讲源码地图

本讲引用的关键文件如下。你不需要现在就打开它们，本讲会在用到时给出永久链接：

| 文件 | 在本讲的作用 |
| --- | --- |
| [README.md](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/README.md) | 项目自述：定位、核心能力、与 ingress-nginx 的区别、上手步骤 |
| [docs/developer/architecture.md](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/developer/architecture.md) | 给开发者的架构指南：五层架构、数据流、校验与 Secret 管理约定 |
| [examples/ingress-resources/complete-example/cafe-ingress.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/ingress-resources/complete-example/cafe-ingress.yaml) | 一个真实的 Ingress 示例，用来直观感受 Ingress 长什么样 |
| [examples/custom-resources/basic-configuration/cafe-virtual-server.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/basic-configuration/cafe-virtual-server.yaml) | 同一个 cafe 应用用 VirtualServer 实现的版本，用来对比 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先讲 Ingress 与 Ingress Controller 的概念，再讲本项目的定位与区别，最后讲核心能力概览。

### 4.1 Ingress 与 Ingress Controller：集群内外的「翻译官」

#### 4.1.1 概念说明

先打一个比方。把 K8s 集群想象成一栋写字楼，每个 Pod 是楼里的一间办公室，Service 是某层的前台。楼里的人（集群内流量）可以自由走动，但**楼外的人（集群外的用户）进不来**——写字楼需要一个大堂总台，根据来访者要找的「楼层/公司」把他们引导到对应的前台。

在这个比喻里：

- **Ingress** 是贴在大堂总台的一张「路由规则表」——一张声明式的 YAML。它规定：访问 `cafe.example.com/tea` 的请求去 tea 公司，访问 `/coffee` 的请求去 coffee 公司。它本身**只是一份数据/规则**，不会自己干活。
- **Ingress Controller** 是站在总台后、真正读这张表并执行分流的那个人（一个运行中的程序）。不同的大堂总台（NGINX、HAProxy、云厂商 LB……）需要不同的「执行者」，所以不同负载均衡器有不同的 Ingress Controller 实现。

关键区分：**Ingress 是 K8s 内置的标准资源类型；Ingress Controller 是由具体厂商/社区提供的、用来「兑现」Ingress 规则的应用程序。** Kubernetes 本身不带 Ingress Controller，你需要自己装一个。

> 名词澄清：英文 *Ingress* 本意是「进入」。后面会反复出现，请始终把它理解为「一张描述入口路由规则的 K8s 资源」。

#### 4.1.2 核心流程

Ingress + Ingress Controller 的工作闭环可以用「控制器模式（reconcile loop）」概括：

```text
用户 kubectl apply 一份 Ingress YAML
        │  （声明期望状态）
        ▼
K8s API Server 把这份资源存进 etcd
        │
        ▼
Ingress Controller 持续「监听(watch)」API Server
   → 发现新增/变更的 Ingress
        │
        ▼
Ingress Controller 把规则翻译成「负载均衡器的本地配置」
   （本项目里就是 NGINX 的 nginx.conf）
        │
        ▼
让实际状态（NGINX 当前在跑的配置）逼近期望状态（用户写的 Ingress）
        │  循环往复，直到一致
```

这套「watch → 发现差异 → 让实际逼近期望」的循环，就是所有 K8s 控制器的通用范式。README 把 Ingress 能表达的规则归纳为两大类：**基于内容的路由**（按 Host 域名或 URL 路径分流）和 **TLS/SSL 终止**（在入口处解密 HTTPS）。

#### 4.1.3 源码精读

README 专门用一节讲这两个概念，这是理解整个项目的起点。

[README.md:76-94](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/README.md#L76-L94) 定义了「什么是 Ingress」：它是配置 HTTP 负载均衡器的 K8s 资源，承载的内容就是 **基于 Host/路径的内容路由** 和 **TLS/SSL 终止** 两类能力。

[README.md:96-103](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/README.md#L96-L103) 定义了「什么是 Ingress Controller」：它是运行在集群里、依据 Ingress 资源去配置负载均衡器的应用。这里有一句至关重要的话——**不同的负载均衡器需要不同的 Ingress Controller 实现**；而本项目里，「NGINX 的 Ingress Controller 与 NGINX 负载均衡器被部署在同一个 Pod 中」。

控制器模式的 watch→reconcile 循环，在架构文档里有一段简明 primer：

[docs/developer/architecture.md:8-23](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/developer/architecture.md#L8-L23) 把控制器工作方式概括为三步：Informer 维护本地缓存 → 资源变更触发事件、入队一个 task → sync 循环逐个出队并调谐期望状态与实际状态。

为了让你对「Ingress 长什么样」有直观印象，这是项目自带的 cafe 示例（节选）：

[examples/ingress-resources/complete-example/cafe-ingress.yaml:1-29](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/ingress-resources/complete-example/cafe-ingress.yaml#L1-L29) 这份 YAML 声明：域名 `cafe.example.com` 下，`/tea` 前缀的请求转发到 `tea-svc`，`/coffee` 前缀的请求转发到 `coffee-svc`，并用 `tls-secret` 做 HTTPS。注意 `apiVersion: networking.k8s.io/v1`——它是 K8s **标准内置**的 Ingress 资源，任何符合规范的 Ingress Controller 都应能识别它。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：通过真实 YAML 建立对「Ingress = 一张路由规则表」的直觉。
2. **操作步骤**：
   - 打开 [cafe-ingress.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/ingress-resources/complete-example/cafe-ingress.yaml)。
   - 找到 `spec.rules` 下的两条 `paths`，分别在脑中模拟一次请求：`GET https://cafe.example.com/tea/123` 会命中哪一条？理由是什么（提示：`pathType: Prefix`）。
   - 再找到 `spec.tls`，确认证书来自哪个 Secret。
3. **需要观察的现象**：你会注意到 Ingress **完全没写 NGINX 相关的字段**——它只描述「去哪」，不描述「NGINX 怎么实现」。这正是 Ingress 与 Ingress Controller 分工的体现。
4. **预期结果**：能口头复述「`/tea` 前缀路由到 tea-svc、HTTPS 证书来自 tls-secret」，并理解「实现细节由 Ingress Controller 负责」。
5. 本步骤为源码阅读型，无需运行集群，也无需本地验证命令输出。

#### 4.1.5 小练习与答案

**练习 1**：如果把 Ingress Controller 关掉，只保留 Ingress 资源，集群外部访问 `cafe.example.com` 会怎样？

> **答案**：没有任何东西会「兑现」这份规则。Ingress 只是一份存放在 API Server 里的声明，没有 Ingress Controller 去读取并配置负载均衡器，请求根本无法被路由——相当于写字楼大堂总台没人值班，规则表形同虚设。

**练习 2**：Ingress 资源里的 `spec.rules[].http.paths[].backend.service` 指向的是 Service 还是 Pod？

> **答案**：指向 **Service**（一组同类 Pod 的稳定抽象）。Ingress 不直接路由到具体 Pod，而是路由到 Service，再由 Service 把流量负载均衡到它背后的 Pod。

### 4.2 本项目的定位：与社区 ingress-nginx 的区别

#### 4.2.1 概念说明

K8s 生态里有**两个名字几乎一样**、但完全独立的 Ingress Controller 项目，初学者极易混淆：

| 项目 | 仓库 | 维护方 |
| --- | --- | --- |
| **本项目（NIC）** | `nginxinc/kubernetes-ingress` | NGINX / F5 官方，由「NGINX 背后的人」维护 |
| 社区版 | `kubernetes/ingress-nginx` | Kubernetes 社区 |

它们**不是同一个项目**，代码库、镜像、安装方式、功能集都不互通。README 专门用一条 Note 提醒这一点。

为什么官方要另起炉灶？因为本项目不仅支持开源 NGINX，还支持商业版 **NGINX Plus**（提供动态 API、健康检查等增强能力），并提供了一套远超标准 Ingress 的 **自定义资源（CRD）** 体系。这些在社区 ingress-nginx 里是没有的。

还有一个对学习者很重要的技术选型区别（后续讲源码会反复用到）：**本项目使用「原始」的 client-go（SharedInformerFactory + work queue），不使用 controller-runtime / Operator SDK。** 这决定了后续控制器源码的写法风格。

#### 4.2.2 核心流程

理解本项目定位，可以从「它扩展了什么」入手。标准 Ingress 只能表达基础路由；本项目通过两条路扩展能力：

```text
标准 Ingress 能力（内容路由 + TLS）
        │
        ├── 扩展方式 A：annotations（注解）+ ConfigMap
        │     给 Ingress 打注解，开启 NGINX/NGINX Plus 的额外能力
        │
        └── 扩展方式 B：自定义资源（CRD）
              VirtualServer / VirtualServerRoute  → 高级 HTTP 路由（流量切分等）
              TransportServer                    → TCP/UDP/TLS 透传（L4）
              Policy                             → 可复用策略（限流/认证等）
              GlobalConfiguration                 → 全局 listener 配置
```

- 扩展方式 A 是「在标准 Ingress 上贴标签」，兼容性好但表达力有限；
- 扩展方式 B 是「用专门的资源类型替代 Ingress」，表达力强、类型安全，是本项目的主打路线。

#### 4.2.3 源码精读

README 的核心能力段落和那条关键 Note：

[README.md:50-69](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/README.md#L50-L69) 说明本项目同时支持 NGINX 与 NGINX Plus，支持标准 Ingress（内容路由 + TLS 终止），并通过 annotations / ConfigMap 扩展能力；除 HTTP 外还支持 Websocket、gRPC、TCP、UDP。并用 VirtualServer/VirtualServerRoute、TransportServer 作为 Ingress 的「进阶替代」。

[README.md:71-74](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/README.md#L71-L74) 是那条关键 Note：**本项目不同于 `kubernetes/ingress-nginx` 仓库里的那个 NGINX Ingress Controller**。这是避免初学者用错文档/镜像的最重要的提醒。

技术选型上的区别记录在架构文档里：

[docs/developer/architecture.md:20-22](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/developer/architecture.md#L20-L22) 明确指出：NIC 使用原始 client-go（`SharedInformerFactory` + 单一 work queue），**不使用 controller-runtime 或 Operator SDK**。后续阅读控制器源码时，你会看到大量直接操作 informer 和 workqueue 的代码，而非 controller-runtime 的 `Reconciler` 抽象。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：在源码中亲自确认「两个项目的区别」并避免混淆。
2. **操作步骤**：
   - 在本仓库根目录确认它的 `go.mod` module 名（这是区分两个项目的硬证据）。可用 `head -1 go.mod` 查看。
   - 阅读上面引用的 README Note，再去社区项目 `kubernetes/ingress-nginx` 的仓库页对比，确认两者是不同仓库。
3. **需要观察的现象**：本仓库 module 名为 `github.com/nginx/kubernetes-ingress`；社区版是 `k8s.io/ingress-nginx`，二者完全不同。
4. **预期结果**：能说出至少两点区别（维护方不同、本项目支持 NGINX Plus 和 CRD、控制器实现用原始 client-go）。
5. 上述 module 名结论基于仓库实际 `go.mod`，可本地核对；其余为源码阅读，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：同事让你「装一下 ingress-nginx」，你该装本仓库的镜像吗？

> **答案**：不该。`ingress-nginx` 通常指社区版 `kubernetes/ingress-nginx`，与本项目（`nginxinc/kubernetes-ingress`）镜像和安装清单不通用。要先和同事确认到底要哪一个。

**练习 2**：本项目用原始 client-go 而非 controller-runtime，这对学习者意味着什么？

> **答案**：意味着你会在控制器源码里直接看到 informer、workqueue、event handler 的「原始」用法，而不是被 controller-runtime 的 `Reconciler` 抽象封装起来。第 3 单元会专门讲这套 list-watch + workqueue 模式。

### 4.3 核心能力概览：五层架构与可用资源

#### 4.3.1 概念说明

要在一个几万行的项目里不迷路，最重要的一件事是**知道每层各管什么**。架构文档把 NIC 划分成五个层次，每层有严格的职责边界。理解这五层，你就有了整个项目的「索引」。

从上到下，五层分别是：

| 层 | 包路径 | 一句话职责 |
| --- | --- | --- |
| 数据模型 | `pkg/apis/configuration/v1/` | CRD 的 Go 结构体定义，是 API 的唯一真相源 |
| 校验 | `pkg/apis/configuration/validation/`、`internal/k8s/validation.go` | 校验 CRD 字段和 Ingress 注解是否合法 |
| 控制器 | `internal/k8s/` | 监听资源、事件分发、内存状态、Secret 解析、状态回写 |
| 配置生成 | `internal/configs/` | 把资源翻译成 NGINX 配置结构体并渲染 `.tmpl` 模板 |
| 进程管理 | `internal/nginx/` | 启动、重载、退出 NGINX 进程，配置失败时回滚 |

> 一条贯穿全局的规则：**配置生成层不许直接访问 K8s API 或读 Secret；控制器层不许直接拼 NGINX 配置文本。** 数据只能沿着「控制器解析好 → 配置层翻译 → 进程层落地」的方向单向流动。

#### 4.3.2 核心流程

把五层串起来，就是文档里那张著名的「从 `kubectl apply` 到 NGINX reload」数据流图。精简成伪代码：

```text
kubectl apply virtualserver.yaml
  → API Server 持久化资源
  → Informer 监听到事件（controller 层）
  → 事件 handler 入队一个 task（controller 层）
  → sync 循环出队，更新内存状态（controller 层）
  → 校验 CRD 字段（validation 层）
  → 解析 Secret 引用、组装「扩展资源」VirtualServerEx（controller 层）
  → Configurator 翻译成 NGINX 配置结构体（config 层）
  → 模板执行器渲染 .tmpl 成配置文本（config 层）
  → NginxManager 写文件 + reload（process 层）
  → 失败则回滚到上一份可用配置（process 层）
  → 回写资源 status（controller 层）
```

这张图会在后续每一个单元里被反复细化，现在只需记住它的大形状。

关于资源类型，本项目能处理的路由/配置资源主要分四类：

```text
1. Ingress                    标准内置资源，networking.k8s.io/v1，基础七层路由
2. VirtualServer              自定义资源，k8s.nginx.org/v1，高级七层路由
3. VirtualServerRoute (VSR)   与 VirtualServer 配合，实现跨命名空间/分片路由
4. TransportServer            自定义资源，k8s.nginx.org/v1，L4（TCP/UDP/TLS 透传）

辅助资源：
- Policy                      可复用策略（限流/认证/访问控制…），被上面资源引用
- GlobalConfiguration         定义全局 listener，供 VirtualServer/TransportServer 引用
```

#### 4.3.3 源码精读

[docs/developer/architecture.md:26-52](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/developer/architecture.md#L26-L52) 给出五层架构的示意图，标注了每层对应的包和关键文件（如数据模型的 `types.go`、进程管理的 `manager.go`）。这是你日后定位代码的「地图」。

[docs/developer/architecture.md:54-62](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/developer/architecture.md#L54-L62) 用表格说明每层各自「拥有」的职责。注意每行对应的包路径，后面每一讲都绕着它们展开。

[docs/developer/architecture.md:64-77](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/developer/architecture.md#L64-L77) 列出「层间穿越规则」这几条不变量——例如「配置生成层不许调用 K8s API、不许直接访问 Secret store」「数据模型不许 import 内部包」。这些规则比任何具体函数都重要，是判断「这段改动该放在哪一层」的依据。

[docs/developer/architecture.md:80-144](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/developer/architecture.md#L80-L144) 是那张完整的数据流图，标注了每一步对应的源码文件和函数（如 `handlers.go` → `controller.go` 的 `sync()` → `configuration.go` → `configurator.go` → `nginx/`）。本讲只需通读它的脉络，具体函数会在后续单元逐一拆解。

为了让你对「同一应用用两种资源实现」的差异建立直觉，对比 cafe 的两份示例：

[examples/custom-resources/basic-configuration/cafe-virtual-server.yaml:1-24](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/basic-configuration/cafe-virtual-server.yaml#L1-L24) 是同一个 cafe 应用用 VirtualServer 写的版本。注意它用 `apiVersion: k8s.nginx.org/v1`（**自定义资源**），把 `upstreams`（后端）和 `routes`（路由动作）分开声明，路由用 `action.pass` 指向 upstream 名称——结构比 Ingress 更清晰、更类型化，也更容易扩展（比如加流量切分）。

#### 4.3.4 代码实践（对比阅读型）

1. **实践目标**：通过对比 Ingress 与 VirtualServer 两个示例，建立「标准资源 vs 自定义资源」的直觉。
2. **操作步骤**：
   - 同时打开 [cafe-ingress.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/ingress-resources/complete-example/cafe-ingress.yaml) 与 [cafe-virtual-server.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/basic-configuration/cafe-virtual-server.yaml)。
   - 列一张三列对照表：`apiVersion/kind`、`后端定义方式`、`路由定义方式`。
3. **需要观察的现象**：Ingress 把后端写在每条 path 里；VirtualServer 把后端集中声明为 `upstreams`，路由用名字引用。
4. **预期结果**：你能解释为什么 VirtualServer 更适合做流量切分（因为后端被命名、可复用，路由可指向同一个 upstream 的不同权重）。这正是架构文档说的「VirtualServer 支持标准 Ingress 不支持的场景」。
5. 本步骤为源码阅读型，无需运行集群。

#### 4.3.5 小练习与答案

**练习 1**：如果一个新功能「既可能影响 Ingress、又可能影响 VirtualServer」，根据层间规则，改动最可能落在哪一层？

> **答案**：落在**配置生成层**（`internal/configs/`）。因为两个模板管线（version1 的 Ingress、version2 的 VirtualServer）都在这层，且二者共享 `policy.go` 等公共生成逻辑。架构文档的「两个模板系统」一节专门提醒：改动可能涉及任一路径时，两条管线都要更新。

**练习 2**：TransportServer 和 VirtualServer 的核心区别是什么？

> **答案**：工作层级不同。VirtualServer 处理 **L7（HTTP）** 流量，能看懂 URL/Header；TransportServer 处理 **L4（TCP/UDP/TLS 透传）** 流量，只看连接和端口，不解析应用层协议。

## 5. 综合实践

**任务**：综合本讲三个模块，用自己的话完成一份「项目职责速写」。

1. **实践目标**：把「Ingress/Ingress Controller 概念 + 本项目定位 + 核心能力」融会贯通，输出一段可被他人读懂的说明。
2. **操作步骤**：
   - 重读 [README.md:50-103](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/README.md#L50-L103) 与 [docs/developer/architecture.md:26-77](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/developer/architecture.md#L26-L77)。
   - 用**不超过 200 字**写一段中文，说明 NIC 是什么、解决什么问题。
   - 在段落末尾，**列出 NIC 支持的全部路由资源类型**（含辅助资源），并对每类用一句话标注它属于 L7 还是 L4。
3. **需要观察的现象**：写的过程中你会反复用到本讲的三个概念——「Ingress Controller 兑现规则」「与 ingress-nginx 是两回事」「标准 Ingress + CRD 两套资源」。
4. **预期结果（参考要点）**：你的速写里应至少包含——它是 NGINX/NGINX Plus 官方 Ingress Controller；把 K8s 资源（Ingress/VS/VSR/TS）翻译成 NGINX 配置并重载；用原始 client-go 而非 controller-runtime。路由资源应列出 Ingress、VirtualServer、VirtualServerRoute（均 L7）与 TransportServer（L4），辅助资源 Policy、GlobalConfiguration。
5. 本任务为阅读理解型，无固定运行结果，可对照上述要点自检。

## 6. 本讲小结

- **Ingress 是规则表、Ingress Controller 是执行者**：前者是 K8s 标准资源（声明期望），后者是把它翻译成负载均衡器配置并持续调谐的应用程序。
- **NIC 由 NGINX/F5 官方维护，与社区 `kubernetes/ingress-nginx` 是两个独立项目**，不互通镜像与安装方式。
- **核心技术选型**：使用原始 client-go（SharedInformerFactory + work queue），不用 controller-runtime，这决定了后续控制器源码的风格。
- **五层架构**（数据模型 → 校验 → 控制器 → 配置生成 → 进程管理）每层职责严格隔离，是整个项目的索引地图。
- **两类资源路线**：标准 Ingress（+ annotations/ConfigMap 扩展）与自定义资源 CRD（VirtualServer/VSR/TransportServer，外加 Policy/GlobalConfiguration）。
- **数据流骨架**：`kubectl apply` → Informer 监听 → 入队 → sync 调谐 → 解析 Secret/组装扩展资源 → 生成配置 → 渲染模板 → 写文件 reload → 回写 status。

## 7. 下一步学习建议

本讲建立了「整体是什么」的地图，接下来建议按依赖顺序推进：

- **想先动手跑起来**：学本单元 [u1-l2 构建与运行](u1-l2-build-and-run.md) 和 [u1-l6 安装方式](u1-l6-install-manifests-and-helm.md)，把 NIC 真正部署起来。
- **想深入资源模型**：进入第 2 单元 [u2-l1 路由资源全景](u2-l1-routing-resource-concepts.md)，系统对比 Ingress/VS/VSR/TS 的能力边界。
- **想读控制器源码**：在读完 u1-l4（命令行入口）后，进入第 3 单元 [u3-l1 控制器生命周期](u3-l1-controller-lifecycle.md)，那里会真正拆解本讲提到的 watch→queue→reconcile 循环。
- **继续阅读的源码**：把 `docs/developer/architecture.md` 整篇通读一遍是性价比最高的下一步——它是后续所有讲义的「目录」。
