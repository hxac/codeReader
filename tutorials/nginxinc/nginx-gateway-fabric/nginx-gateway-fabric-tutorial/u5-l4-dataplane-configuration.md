# dataplane.Configuration：图的最终配置表示

## 1. 本讲目标

本讲是「图构建与数据面配置」单元（u5）的最后一讲。前面三讲我们一路追踪了：资源如何变成 `graph.Graph`（u5-l1）、路由如何绑定到监听器（u5-l2）、后端如何被解析（u5-l3）。本讲要回答「图的最后一公里」：

`graph.Graph` 描述的是 **Gateway API 的领域语义**（哪个 GatewayClass 被认领、哪个路由有效、哪个后端不可达、该给资源回写什么 Condition）。但 NGINX 不认识 Gateway API，它只认自己的配置语法（`server {}`、`upstream {}`、`ssl_certificate` 等指令）。在「领域模型」和「最终配置文本」之间，NGF 安插了一个中间层：`dataplane.Configuration`。

学完本讲，你应当能够：

- 说清 `dataplane.Configuration` 与 `graph.Graph` 的职责差异，以及为什么需要这层中间表示。
- 看懂 `BuildConfiguration` 如何把一个 `graph.Graph` 翻译成完整的 `Configuration`。
- 掌握 SSL 密钥对（`SSLKeyPairs`）、证书 Bundle（`CertBundles`）以及前端/后端 TLS 是如何被收集、去重、命名的。
- 理解 L4（TCP/UDP/TLS）服务是如何在 `stream` 上下文里被构建的。
- 顺着 `Configuration` 的数据流向，预判它下一步会被谁消费（配置生成器）。

## 2. 前置知识

阅读本讲前，请确保你已经掌握：

- **Gateway API 三层资源**：GatewayClass / Gateway / HTTPRoute 等，以及 Listener、parentRefs、backendRefs 的接线方式（见 u1-l1、u5-l1）。
- **`graph.Graph` 的结构**：它是 ChangeProcessor 在 `Process` 阶段产出的内部模型，节点带 `Valid` 布尔位和 `Conditions`，校验错误不抛异常而是沉淀进条件（见 u5-l1）。
- **配置生成的下游**：`Configuration` 之后会被 `generator.Generate(conf)` 渲染成一组 NGINX 配置文件（见 u6-l1）。本讲止于「渲染的输入」，不展开模板本身。
- **NGINX 的基本概念**：`http` 与 `stream` 两个上下文；`server`/`upstream`/`location` 块；`ssl_certificate`/`ssl_client_certificate` 等指令。这些是理解 Configuration 字段为何这样切分的背景。

一个关键直觉：**整条数据流是一个「逐层降维」的过程**。集群里上千个 K8s 资源 →（ChangeProcessor 捕获）→ `ClusterState` →（`BuildGraph`）→ `graph.Graph`（领域模型）→（`BuildConfiguration`）→ `dataplane.Configuration`（渲染模型）→（`Generate`）→ NGINX 文本。本讲讲解的就是倒数第二步。

## 3. 本讲源码地图

本讲涉及的文件全部位于 `internal/controller/state/dataplane/` 包，外加两处消费方：

| 文件 | 作用 |
| --- | --- |
| [doc.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/doc.go#L1-L10) | 一句话定义包职责：把 Graph 翻译成「数据面配置的中间表示」。 |
| [types.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/types.go) | 中间表示的全部类型定义：`Configuration`、`VirtualServer`、`Upstream`、`SSL`、`SSLKeyPair` 等。 |
| [configuration.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go) | 翻译逻辑主体：`BuildConfiguration` 入口，以及构建各字段的 `build*` 函数。 |
| [convert.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/convert.go) | 纯转换函数：把上游 Gateway API / graph 的结构体逐字段映射成 dataplane 结构体。 |
| [sort.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/sort.go) | 排序辅助：保证生成顺序确定，避免无谓 reload。 |
| [handler.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L297-L302) | 消费方：EventHandler 调用 `BuildConfiguration` 得到 `cfg`，再交给生成器。 |
| [generator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L79-L86) | 下游消费方：`Generator.Generate(conf)` 把 `Configuration` 渲染成 NGINX 文件。 |

记住一条边界：`dataplane` 包**只产出数据结构，不写文件、不渲染文本**。写文件是生成器（u6）的活，下发是 Agent（u7）的活。

## 4. 核心概念与源码讲解

### 4.1 类型与转换：Configuration 的数据模型

#### 4.1.1 概念说明

`dataplane.Configuration` 是一个**面向 NGINX 配置生成而设计的中间数据结构**。它的字段切分方式不是按 Gateway API 资源类型（HTTPRoute/Service/…）来分，而是按 **NGINX 配置块**来分：`HTTPServers`、`SSLServers`、`Upstreams`、`SSLKeyPairs`、`CertBundles`……每个字段几乎都能在最终的 `nginx.conf` 里找到对应的落点。

为什么这样切？因为生成器的工作就是「遍历这些切片，逐个渲染成对应块」。把字段组织成「可直接喂给模板」的形状，能让生成器保持薄而 dumb。

`doc.go` 用一句话钉死了这个定位：

> Package dataplane translates Graph representation of the cluster state into an intermediate representation of data plane configuration. We can think of it as an intermediate state between the cluster resources and NGINX configuration files.

#### 4.1.2 核心流程

类型与转换层的核心是两类东西：

1. **容器类型**：`Configuration` 这个大结构，以及它引用的子结构（`VirtualServer`、`Upstream`、`SSL`、`SSLKeyPair`、`CertBundle` …）。
2. **ID 生成器**：`SSLKeyPairID` / `CertBundleID` / `AuthFileID` 等字符串 ID。注释反复强调「The ID is safe to use as a file name」——因为这些 ID 最终会变成数据面 Pod 上 `/etc/nginx/secrets/` 里的文件名。

转换函数（convert.go）则是把上游类型「逐字段搬」过来的纯函数，例如 `convertMatch`、`convertPathType`、`convertWAFBundles`。它们不查集群、不报错，只做结构体搬运，遇到不支持的枚举值用 `panic` 兜底（fail-fast）。

#### 4.1.3 源码精读

先看 `Configuration` 主结构的全貌。注意字段的「NGINX 视角」命名：

[internal/controller/state/dataplane/types.go:L29-L82](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/types.go#L29-L82)

这段定义里几个值得留意的点：

- `HTTPServers` / `SSLServers` 是 `[]VirtualServer`，对应 NGINX 里 80/443 端口的 `server {}`；`TLSServers` / `TCPServers` / `UDPServers` 是 `[]Layer4VirtualServer`，对应 `stream {}` 块。
- `SSLKeyPairs map[SSLKeyPairID]SSLKeyPair` 和 `CertBundles map[CertBundleID]CertBundle` 用 **map** 而非 slice 存储——这是去重用的，同一证书被多个监听器引用时只落一份。
- `SSLListenerHostnames map[int32][]string`：把每个 HTTPS 端口映射到它「监听过的主机名列表」，注释说明它是用来生成 NGINX `map` 指令、做「misdirected request 检测」（请求走错了 SNI）的。

再看 ID 类型与密钥对。注释里那句「safe to use as a file name」是理解整个证书落盘机制的关键：

[internal/controller/state/dataplane/types.go:L84-L116](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/types.go#L84-L116)

注意 `SSLKeyPair` 只有两块裸字节：`Cert` 和 `Key`。它**不带任何 K8s 元数据**——一旦从 Secret 里取出来，K8s 身份就被「剥」掉了，只剩 ID 和字节。这正是中间表示「去领域化」的体现。

转换函数的典型形态，以 `convertPathType` 为例（不支持的值直接 panic）：

[internal/controller/state/dataplane/convert.go:L132-L143](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/convert.go#L132-L143)

而 `convertWAFBundles` 则展示了「graph 类型 → dataplane 类型」的纯搬运（连类型别名转换都没有，只改了 map 的 value 类型）：

[internal/controller/state/dataplane/convert.go:L416-L431](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/convert.go#L416-L431)

#### 4.1.4 代码实践（源码阅读型）

**目标**：感受「NGINX 视角」的字段切分。

1. 打开 `types.go`，把 `Configuration` 结构体的每个字段，试着对应到一条 NGINX 指令或一个配置块。例如 `Logging.AccessLog` → `access_log` 指令；`Upstreams` → `upstream {}` 块。
2. 再打开 `generator.go`（u6 的主角），看它的 `Generate` 是不是按这些字段一块一块渲染的。

**需要观察的现象**：你会发现几乎每个字段都能在生成器里找到一一对应的渲染入口；如果有字段在生成器里找不到消费者，要么是尚未实现，要么是遗留字段。

**预期结果**：你能画出一张「`Configuration` 字段 → NGINX 配置落点」的对照表。例如：`SSLKeyPairs` → `/etc/nginx/secrets/` 下的证书文件；`CertBundles` → 同目录下的 CA bundle 文件。

> 待本地验证：生成的证书文件名是否真的等于 `SSLKeyPairID` 字符串，可用 `kubectl exec` 进入数据面 Pod 查看 `/etc/nginx/secrets/` 目录。

#### 4.1.5 小练习与答案

**练习 1**：`SSLKeyPairs` 为什么用 `map[SSLKeyPairID]SSLKeyPair` 而不是 `[]SSLKeyPair`？

**参考答案**：用 map 是为了天然去重——同一个 Secret 被多个监听器引用时，以 `SSLKeyPairID`（由 Secret 的 namespace/name 派生）为键，只会保留一份；同时 map 也充当「ID → 数据」的索引，方便生成器按 ID 取证书。

**练习 2**：`convertPathType` 遇到未知 `pathType` 时为什么是 `panic` 而不是返回错误？

**参考答案**：因为上游 `graph` 在校验阶段已经把无效值挡掉了（无效资源 `Valid=false`，不会进入配置生成）。能走到 `convertPathType` 的值都是合法枚举；若出现未知值，说明是程序 bug 而非用户输入问题，用 `panic` 做 fail-fast 比吞错更安全。

---

### 4.2 Configuration 构建：BuildConfiguration 主流程

#### 4.2.1 概念说明

`BuildConfiguration` 是本包的唯一入口函数，签名是：

```go
func BuildConfiguration(ctx, logger, g *graph.Graph, gateway *graph.Gateway,
    serviceResolver resolver.ServiceResolver, plus bool) Configuration
```

它接收一个**完整的图**和**当前要为之生成配置的那个 Gateway**（注意：图里可能含多个 Gateway，但每次只为一个 Gateway 生成一份 `Configuration`，见 u4-l3 的「逐 Gateway」处理）。它产出一个填满字段的 `Configuration`。

这是一个**纯函数式的翻译过程**：没有副作用（除了写日志、调 `serviceResolver` 查端点），输入相同则输出相同（端点解析结果除外）。

#### 4.2.2 核心流程

`BuildConfiguration` 的整体流程可以归纳为「一个早退 + 一组并行构建 + 一次装配」：

```text
1. 早退检查：
   若 GatewayClass 为空/无效 或 gateway 为 nil
   → 返回 GetDefaultConfiguration（最小骨架，几乎全空）
   （plus 时额外补一个 NginxPlus 字段）

2. 预处理：收集本 Gateway 引用到的 SnippetsFilter / RateLimitPolicy

3. 构建基础设施（base config）：
   buildBaseHTTPConfig  → http 上下文全局配置（http2、IP family、DNS resolver…）
   buildBaseStreamConfig → stream 上下文全局配置

4. 构建服务与证书（核心）：
   buildServers          → http/ssl VirtualServer + 监听主机名表 + 外部鉴权证书 ID
   buildOIDCProviderFromAuthenticationFilters → OIDC provider + CA/CRL bundle
   buildJWTRemoteTLSCABundles                  → JWT 远程 JWKS 的 CA bundle
   buildBackendGroups    → 从 servers 里收集去重的 BackendGroup
   buildTLSServers       → TLSRoute 对应的 stream server
   buildUpstreams        → http upstream（调 serviceResolver 解析端点）
   buildStreamUpstreams  → stream upstream
   buildRefCertificateBundles + buildCertBundles + buildFrontendTLSCertBundles → 各类证书 bundle

5. 一次性装配成 Configuration 字面量并返回
```

关键设计：**多个 `build*` 函数并行产出不同字段，彼此基本独立**。这让函数体读起来像一张「装配清单」，而不是一条曲折的流水线。依赖关系通过把前一步的产物（如 `sslServers`、`extAuthCertBundleIDs`）作为后一步的入参显式传递，而不是共享可变状态。

#### 4.2.3 源码精读

先看入口和**早退逻辑**——这是理解「无效 Gateway 会怎样」的关键：

[internal/controller/state/dataplane/configuration.go:L65-L82](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L65-L82)

当 GatewayClass 不存在、无效，或 gateway 为 nil 时，直接返回 `GetDefaultConfiguration`。注意它**不是返回一个空的 `Configuration{}`**，而是带日志、Worker 连接数等少量字段的骨架：

[internal/controller/state/dataplane/configuration.go:L2524-L2531](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L2524-L2531)

这保证即使集群里没有任何被认领的 Gateway，NGINX 也仍有一份合法的最小配置（能起来、能健康检查），而不是一张空白文件导致启动失败。

接着是核心的**装配阶段**——这是本讲最重要的一段代码。注意它如何把各 `build*` 的产物逐字段填进结构体字面量：

[internal/controller/state/dataplane/configuration.go:L139-L169](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L139-L169)

这段代码值得逐行读。几个要点：

- `TCPServers` 和 `UDPServers` 复用同一个 `buildL4Servers` 函数，只是传入的协议不同（`v1.TCPProtocolType` vs `v1.UDPProtocolType`）。
- `SSLKeyPairs`、`CertBundles` 这些 map 类型字段在这里被一次性算好。
- `DeploymentContext` 字段**没有**在这里填——它由调用方（handler）事后补上（见下方消费方代码）。

这正是「职责分层」的体现：`BuildConfiguration` 只负责「从图算配置」，deployment context（集群 ID 等）是环境信息，由 handler 注入。看消费方就一清二楚：

[internal/controller/handler.go:L297-L302](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L297-L302)

`BuildConfiguration` 的产出 `cfg` 随后被传给生成器渲染（u6-l1）：

[internal/controller/nginx/config/generator.go:L79-L86](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L79-L86)

`Generate(dataplane.Configuration) []agent.File`——生成器只认 `Configuration`，完全不碰 `graph.Graph`。这条接口边界就是「中间表示」存在的全部理由：**让生成器与 K8s 领域模型彻底解耦**。

#### 4.2.4 代码实践（源码阅读型）

**目标**：把 `BuildConfiguration` 的装配清单画成一张依赖图。

1. 在 `configuration.go` 的 `BuildConfiguration` 函数体（L65–L172）里，逐个标记每个 `build*` 调用。
2. 用箭头标出它们之间的数据依赖。例如 `buildCertBundles` 依赖 `refCertBundles`、`backendGroups`、`tlsServers`、`extAuthCertBundleIDs`、`authCertBundles` 五个输入——说明它是一个「汇聚点」。

**需要观察的现象**：你会发现绝大多数 `build*` 函数彼此独立，只有少数（如 cert bundles）是汇聚多个产物的。

**预期结果**：你能指出哪些字段可以理论上并行计算（独立），哪些必须最后算（依赖前面）。这有助于理解为什么这段代码能保持线性、易读。

> 本地可选：把 `BuildConfiguration` 末尾的 `config := Configuration{...}` 字面量注释掉几个字段（仅本地实验，勿提交），重新 `make build`，观察编译器报错如何精确指向缺失字段，验证「装配清单」的完整性。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `DeploymentContext` 不在 `BuildConfiguration` 里赋值？

**参考答案**：`DeploymentContext` 描述的是「NGF 部署本身 + 集群」的元信息（cluster id、installation id、节点数），与 Gateway API 资源图无关。`BuildConfiguration` 的职责是从图算配置，环境信息由 handler 通过 `getDeploymentContext` 单独获取后注入。两者关注点不同，分离后各自可测。

**练习 2**：`TCPServers` 和 `UDPServers` 为什么能共用 `buildL4Servers`？

**参考答案**：TCP 和 UDP 在 NGINX `stream` 上下文里的虚拟服务器结构完全一致（端口 + upstream 列表），区别仅在 `listen` 指令上的协议关键字。代码把协议作为参数传入，复用同一套遍历/去重/排序逻辑，避免重复代码。

---

### 4.3 SSL/证书处理：密钥对与证书 Bundle

#### 4.3.1 概念说明

NGINX 场景里，「证书」其实分两种用途，新手容易混淆，本讲必须分清：

1. **SSL 密钥对（`SSLKeyPair` = 证书 + 私钥）**：用于 NGINX **作为服务端**终止 TLS（前端 HTTPS 监听器）。对应 `ssl_certificate` / `ssl_certificate_key` 指令。
2. **证书 Bundle（`CertBundle` = 一堆 CA 证书，只有证书没有私钥）**：用于 NGINX **作为客户端**去验证对端。典型场景：后端 mTLS（BackendTLSPolicy）、外部鉴权后端、JWT 远程 JWKS、OIDC provider、客户端证书校验。对应 `ssl_client_certificate` / `proxy_ssl_trusted_certificate` / `ssl_trusted_certificate` 等指令。

`dataplane` 包对这两类做了严格区分：`SSLKeyPairs map[SSLKeyPairID]SSLKeyPair` 与 `CertBundles map[CertBundleID]CertBundle` 是两个独立字段、两套 ID、两套构建函数。

#### 4.3.2 核心流程

证书处理的整体流程是「**收集 → 去重 → 命名 → 挂载**」：

```text
SSLKeyPairs（前端终止用）：
  遍历 Gateway 下所有有效监听器的 ResolvedSecrets
    + Gateway 自身的 SecretRef（gateway 级证书）
  每个 Secret → SSLKeyPairID(ssl_keypair_<ns>_<name>) → {Cert, Key}

CertBundles（验证用），有四个来源，按「谁需要」汇聚：
  1. buildRefCertificateBundles：把 ReferencedSecrets + ReferencedCaCertConfigMaps 里
     的证书 bundle 先摊平成一张总表
  2. buildCertBundles：只保留「真正被引用」的 bundle：
     - 后端 TLS 验证（backendGroups 里带 VerifyTLS 的 backend）
     - TLSRoute 的 VerifyTLS
     - 外部鉴权（extAuthCertBundleIDs）
     - 认证相关（OIDC/JWT 的 CA 与 CRL）
  3. buildFrontendTLSCertBundles：前端客户端证书校验用的 CA bundle
     （listener.CACertificateRefs），并回填到 SSL server 的 ClientCertBundleID
```

命名的核心是几个 `generate*ID` 函数，它们都保证「ID 可作文件名」且「按 ns/name 唯一」。

#### 4.3.3 源码精读

先看 **SSL 密钥对**的构建。它遍历监听器的 `ResolvedSecrets`（解析过的、被授权引用的 Secret），取出证书和私钥字节：

[internal/controller/state/dataplane/configuration.go:L491-L524](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L491-L524)

注意两点：一是只在 `l.Valid` 且 `ResolvedSecrets` 非空时才纳入（无效监听器不产生证书，呼应 u5-l1 的 `Valid` 把门）；二是用 `generateSSLKeyPairID(secretNsName)` 做键天然去重。ID 生成器本身极简：

[internal/controller/state/dataplane/configuration.go:L1972-L1984](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L1972-L1984)

再看 **CertBundle** 的「按需保留」逻辑，这是节省数据面空间的关键——不是所有引用过的证书都要落盘，只保留真正会被某个验证场景用到的：

[internal/controller/state/dataplane/configuration.go:L739-L780](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L739-L780)

这里的 `referenced` set 先把「谁需要哪个 bundle」收齐（遍历 backendGroups/tlsServers/extAuth），再从总表里挑出被需要的——典型「需求驱动」过滤。若没有任何引用，直接返回空 map，不浪费存储。

**后端 TLS** 的转换体现了「图里的策略 → dataplane 的 VerifyTLS」的桥接。注意它处理「未显式给 CA」的情况——回退到 Alpine 的系统 CA 路径：

[internal/controller/state/dataplane/configuration.go:L1328-L1345](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L1328-L1345)

`SSL` 子结构本身（types.go）则把 NGINX 的 SSL 指令集中表达——`KeyPairIDs` 是个数组，注释点明多 ID 是为了 **SNI**（同一端口按域名选证书）：

[internal/controller/state/dataplane/types.go:L211-L230](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/types.go#L211-L230)

#### 4.3.4 代码实践（源码阅读型）

**目标**：跟踪一条「前端客户端证书校验」链路，理解密钥对与 bundle 如何挂到 SSL server 上。

1. 从 `buildFrontendTLSCertBundles`（configuration.go L568）读起。它为每个带 `CACertificateRefs` 的 HTTPS 监听器生成一个 `CertBundleID`，并存进 `clientSettingsMap[port]`。
2. 接着读 `addClientSettingsToSSLServers`（L692）：它把 `clientSettingsMap` 里的验证模式与 bundle ID **回填**到对应端口的 `sslServers[i].SSL` 字段上。
3. 注意 `AllowInsecureFallback` 分支：这个模式下不配 CA bundle，而是设 `VerifyClient = SSLVerifyClientOptionalNoCA`。

**需要观察的现象**：你会看到一个「先建 server、再补 SSL 细节」的两阶段模式——`buildServers` 先产出 server 骨架，`buildFrontendTLSCertBundles` 再按端口回填客户端校验配置。

**预期结果**：你能解释为什么 `addClientSettingsToSSLServers` 要按 `port` 索引而不是按 server 遍历——因为多个 SSL server 可能共享同一端口（不同 hostname），它们共用同一份监听器级校验配置。

> 待本地验证：构造一个带 `frontend.validation` 的 NginxProxy，部署后用 `openssl s_client` 连数据面，观察是否被要求出示客户端证书。

#### 4.3.5 小练习与答案

**练习 1**：`SSLKeyPairs` 和 `CertBundles` 在用途和结构上的核心区别是什么？

**参考答案**：`SSLKeyPairs` 含私钥（`Cert` + `Key`），用于 NGINX 作为 TLS 服务端终止前端连接；`CertBundles` 只含 CA 证书（无私钥），用于 NGINX 作为客户端去验证对端证书（后端 mTLS、外部鉴权、JWT/OIDC 等）。

**练习 2**：`buildCertBundles` 为什么要先构建 `referenced` set 再过滤？

**参考答案**：图里可能引用了很多 Secret/ConfigMap 形式的 CA，但并非每个都会被实际使用（例如某个 BackendTLSPolicy 可能 `Valid=false`）。先收集「真正被某个 backend/tlsServer/外部鉴权引用」的 bundle ID 集合，再从总表里挑出它们，可以避免把无用证书落盘到数据面，节省存储并减小配置体积。

---

### 4.4 L4 / Stream 服务的构建

#### 4.4.1 概念说明

Gateway API 不仅有 HTTP/HTTPS，还有 L4 协议：`TCPRoute`、`UDPRoute`、`TLSRoute`。它们在 NGINX 里落在 **`stream` 上下文**（而非 `http` 上下文）。`stream` 上下文的 server 不解析 HTTP，只做四层转发；对 TLSRoute 还支持 **passthrough**（不解密，按 SNI 转发）和 **terminate**（在 stream 块里终止 TLS 再转发明文/重新加密）两种模式。

dataplane 用 `Layer4VirtualServer` 统一表达这三类，通过 `buildTLSServers` / `buildL4Servers` 两个函数产出。

#### 4.4.2 核心流程

```text
TLSRoute（buildTLSServers）：
  遍历所有 TLS 协议监听器
    若是 Terminate 模式 → 调 buildSSL 生成 SSL 配置（在 stream 块里终止 TLS）
    遍历监听器上的 L4Routes：
      按路由接受的主机名（AcceptedHostnames）生成 Layer4VirtualServer
      （TLSRoute 的 backend 不支持 weight，故 Weight=0）
    若没有任何路由匹配监听器主机名 → 生成一个 default server
  最后按 port/hostname/default 排序，保证输出确定

TCP/UDP（buildL4Servers）：
  遍历指定协议的监听器 → 遍历 L4Routes → 收集有效 backend → 组 Layer4VirtualServer
  同样按 port/hostname/upstreams 排序
```

#### 4.4.3 源码精读

先看 `buildTLSServers` 的骨架，注意它对 Terminate/Passthrough 两种模式的分支处理：

[internal/controller/state/dataplane/configuration.go:L179-L210](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L179-L210)

其中 `isTLSTerminateListener` 是模式判定的全部依据——`TLS.Mode` 为 nil 或 `Terminate` 都算终止模式：

[internal/controller/state/dataplane/configuration.go:L174-L177](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L174-L177)

TCP/UDP 则更简单，没有 TLS 模式之分，核心是「收 backend + 排序」。注释点明排序的目的——避免 map 遍历的随机序引发无谓 reload：

[internal/controller/state/dataplane/configuration.go:L314-L384](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L314-L384)

`Layer4VirtualServer` 类型本身有个小而重要的方法 `NeedsWeightDistribution()`——当 upstream 多于 1 个时返回 true，提示生成器需要用 `split_clients` 做加权分流：

[internal/controller/state/dataplane/types.go:L142-L162](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/types.go#L142-L162)

#### 4.4.4 代码实践（源码阅读型）

**目标**：理解「确定序」如何避免无谓 reload。

1. 读 `buildTLSServers` 末尾的两段 `sort.Slice`（configuration.go L218、L232），以及 `buildL4Servers` 末尾的排序（L373）。
2. 读 `sort.go` 里的 `sortPathRules`——它把更长的路径排在前面，注释解释这是为了 NGINX 正则 location 的「first match wins」语义。

[internal/controller/state/dataplane/sort.go:L9-L24](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/sort.go#L9-L24)

**需要观察的现象**：所有「最终会变成配置文本」的切片（servers、upstreams、path rules）在 `BuildConfiguration` 里都会被排序。

**预期结果**：你能用自己的话解释——为什么 Go map 遍历顺序随机，而配置文件顺序若随机会导致即使内容没变、Agent 下发后 NGINX 也会因为文件 diff 而 reload。

> 待本地验证：高频改动一个 HTTPRoute，观察 NGF 数据面 reload 次数是否远少于改动次数（这还结合了 u4-l2 的事件批处理）。

#### 4.4.5 小练习与答案

**练习 1**：TLSRoute 的 Passthrough 和 Terminate 模式在 `buildTLSServers` 里的代码差异是什么？

**参考答案**：Terminate 模式下会调用 `buildSSL(l)` 生成一份 `*SSL`（含密钥对 ID 等），挂到 server 上，使 NGINX 在 `stream` 块里终止 TLS；Passthrough 模式下 `ssl` 为 nil，server 不配 SSL，NGINX 仅按 SNI 转发密文（不解密）。

**练习 2**：为什么 `buildL4Servers` 对结果要排序？

**参考答案**：L4Routes 存在 map 里（map 遍历顺序随机）。若直接把遍历结果写成配置，相同输入会产出顺序不同的文件，导致 Agent 对比文件时认为「变了」而触发不必要的 NGINX reload。排序保证「相同输入 → 相同输出」，把 reload 降到只在内容真正变化时才发生。

---

## 5. 综合实践

**任务**：用一段话回答本讲的核心问题——**`graph.Graph` 与 `dataplane.Configuration` 的区别是什么？为什么需要这层中间表示？** 要求结合本讲源码给出至少 3 条具体理由。

建议按以下步骤组织你的答案：

1. **先定位两者在数据流里的位置**：复习本讲 §2 的「逐层降维」链路，明确 Graph 是 ChangeProcessor `Process` 的产物，Configuration 是 `BuildConfiguration` 的产物。
2. **列举字段视角的差异**：打开 `types.go` 的 `Configuration` 结构体，对比 graph 包里 `Graph` 结构体的字段。你会发现 Graph 里满是 `Valid`、`Conditions`、`ParentRefs`、`Attachment` 这类 Gateway API 语义字段；而 Configuration 里全是 `HTTPServers`、`Upstreams`、`SSLKeyPairs` 这类 NGINX 视角字段。
3. **追踪消费方**：看 `Generator.Generate(conf)` 的接口定义，确认生成器只接受 `Configuration`、对 `Graph` 一无所知。
4. **给出 3 条理由**，参考方向：
   - **解耦**：生成器（u6）不必懂 K8s/Gateway API，只需把「NGINX 视角」的结构渲染成文本；K8s 资源语义变化不影响生成器。
   - **可测性**：`Configuration` 是纯数据结构，可在不启真实集群的情况下用单元测试构造它（参考 `configuration_test.go`，本包测试体量极大正因如此）。
   - **去领域化 + 去重 + 确定序**：把分散在多种资源里的同构信息（如多监听器引用同一证书）收敛、去重、排序，让数据面拿到的是「最小且确定」的配置，减少落盘文件数和无谓 reload。

**自检**：如果你的 3 条理由里没有出现「解耦」「可测」「去重/确定序」这类结构性收益，说明你还没抓住「中间表示」的价值，重读 §4.1.1 与 §4.2.3。

> 这是源码阅读型实践，无需运行命令；但鼓励你打开 `configuration_test.go`，看测试是如何直接构造 `graph.Graph` 输入、断言 `Configuration` 输出的——这正是中间表示带来的可测性的活样本。

## 6. 本讲小结

- `dataplane.Configuration` 是介于「领域模型 `graph.Graph`」和「NGINX 配置文本」之间的**渲染模型**；它的字段按 NGINX 配置块切分，几乎一对一对应到生成器（u6）的渲染入口。
- `BuildConfiguration` 是本包唯一入口，采用「早退 → 一组并行 `build*` → 一次性字面量装配」的结构；无效 Gateway 走 `GetDefaultConfiguration` 返回最小骨架而非空配置。
- 证书严格分两类：**`SSLKeyPairs`**（含私钥，前端 TLS 终止用）与 **`CertBundles`**（仅 CA，验证对端用），各有独立 ID 体系与「按需保留」的过滤逻辑。
- L4（TCP/UDP/TLS）服务落在 `stream` 上下文，由 `buildTLSServers`/`buildL4Servers` 产出 `Layer4VirtualServer`；TLS 区分 Passthrough 与 Terminate 两种模式。
- 所有会变成配置文本的切片在构建后**都会被排序**，目的是保证「相同输入 → 相同输出」，避免 map 随机序引发无谓 NGINX reload。
- `DeploymentContext` 等「环境信息」不在 `BuildConfiguration` 内填，由 handler 事后注入——这是关注点分离的体现。

## 7. 下一步学习建议

本讲产出的 `dataplane.Configuration` 紧接着就被 `generator.Generate(conf)` 消费，所以下一站应该是 **u6 单元「NGINX 配置生成与模板」**：

- **u6-l1 配置生成器总览**：看 `Generate` 如何把 `Configuration` 的每个字段渲染成 `[]File`，这是本讲的直接下游。
- **u6-l2 nginx.conf 骨架与模板体系**：理解生成的文件如何通过 `include` 组织进 `conf.d` / `stream-conf.d` 目录。
- **u6-l3 Servers/Upstreams/Locations 生成**：本讲讲的是「`VirtualServer`/`Upstream` 数据从哪来」，u6-l3 讲「这些数据怎么变成 NGINX 块」，两者拼起来才是完整链路。

如果对策略如何影响配置感兴趣，可在进入 u6 前先读 **u8-l2/u8-l3**（CRD 与策略生成），理解 `Configuration.Policies` 字段里的 `policies.Policy` 是怎么来的。此外，本讲多次提到的「确定序避免 reload」，其完整图景要与 **u4-l2（事件双缓冲）**、**u13-l2（并发与可靠性）** 串联阅读。
