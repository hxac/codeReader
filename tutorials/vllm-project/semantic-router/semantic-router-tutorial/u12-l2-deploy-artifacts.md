# Helm / Kubernetes / 本地部署

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `deploy/` 目录的**资产边界**：它只放「创建或配置部署目标」的产物，而路由配置、recipe、运行时后端示例分别属于 `config/`、`tools/`，二者刻意分离。
- 读懂官方 **Helm chart**（`deploy/helm/semantic-router`）的组织方式：它渲染出哪些 Kubernetes 资源、有哪些关键可配置项，以及它**只部署 ExtProc router、并不部署 Envoy** 这一关键事实。
- 区分两类「Envoy 与 router 的协作拓扑」：**sidecar 同 Pod 模型**（KServe / OpenShift 清单）与 **gateway 远程调用模型**（Istio / AI Gateway / agentgateway 等）。
- 对照 `deploy/kubernetes`、`deploy/kserve`、`deploy/openshift`、`deploy/local` 理解「部署变体」的存在意义。

> 本讲承接 [u4-l1 启动主流程](u4-l1-startup-sequence.md)：那篇讲 `main()` 在**进程内**怎么起来；本篇讲同一个容器镜像在**集群里**被哪些产物拉起来、配置和模型从哪挂进来、探活如何对应启动状态。

## 2. 前置知识

- **Kubernetes 基础资源**：Deployment（无状态工作负载）、Service（服务发现与负载均衡）、ConfigMap（配置）、PersistentVolumeClaim / PVC（持久存储）、HPA（水平自动扩缩容）。本讲会频繁引用这些。
- **Helm 基础**：Chart（一个可部署包）、`values.yaml`（默认值）、`templates/`（Go template 渲染的 K8s 清单）、`Chart.yaml`（含 `dependencies` 子 chart）。
- **Envoy External Processor（ExtProc）**：SR 以 gRPC ExtProc 服务的形式挂在 Envoy 侧。Envoy 把请求头/体发给 SR 的 `Process(stream)`，SR 回 `CONTINUE` 放行或 `ImmediateResponse` 直接回包。这一点是 [u4-l3](u4-l3-extproc-server.md) 的核心，本讲的部署产物本质上就是「把这一对 gRPC 端点在集群里布好并连起来」。
- **gRPC 与 Service**：gRPC 基于 HTTP/2 长连接，K8s 默认的 kube-proxy 负载均衡对长连接效果差，因此 SR 用了一个 **headless Service** 来支持客户端（Envoy）侧负载均衡——这是本讲一个关键设计点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `deploy/README.md` | `deploy/` 的资产边界声明：什么该放、什么不该放。 |
| `deploy/helm/README.md` | Helm chart 的总说明：安装方式、dev/prod profile、密钥处理、Make 目标。 |
| `deploy/helm/semantic-router/Chart.yaml` | chart 元数据与子 chart 依赖（redis/milvus/jaeger/prometheus/grafana）。 |
| `deploy/helm/semantic-router/values.yaml` | 全部默认值，是「关键可配置项」的权威清单。 |
| `deploy/helm/semantic-router/templates/deployment.yaml` | 渲染 router 的 Deployment——本讲的主角。 |
| `deploy/helm/semantic-router/templates/service.yaml` | 渲染 3 个 Service：常规、headless、metrics。 |
| `deploy/kubernetes/istio/envoyfilter.yaml` | gateway 模型样例：Istio 的 EnvoyFilter 把 ext_proc 指向 SR 服务。 |
| `deploy/kserve/deployment.yaml` | sidecar 模型样例：router 与 envoy-proxy 同 Pod。 |
| `deploy/kserve/configmap-envoy-config.yaml` | sidecar 模型里 Envoy 的配置：ext_proc 集群指向 `127.0.0.1:50051`。 |

> 说明：大纲里列出的 `deploy/kubernetes/README.md` 在当前 HEAD **并不存在**（`deploy/kubernetes/` 下没有顶层 README），取而代之的是各子目录自带的 README（如 `deploy/kubernetes/istio/README.md`）。本讲以真实存在的文件为准。

## 4. 核心概念与源码讲解

### 4.1 部署资产边界（Asset Boundary）

#### 4.1.1 概念说明

`deploy/` 是仓库里**唯一**的「部署产物」聚集地。但它有一条严格的边界：只放「创建或配置一个部署目标」的东西，绝不放路由配置、recipe、示例程序或纯文字教程。这条边界是刻意划的——它让「部署形态」与「路由策略」可以独立演进、独立版本化。

#### 4.1.2 核心流程

`deploy/README.md` 用一句话划定了边界，并列举了内部三类产物与外部「不该放这里」的东西：

```
deploy/
├── helm/        + operator/   → 打包好的 K8s 安装方式（chart / Operator）
├── kubernetes/  + kserve/
│   + openshift/               → 各平台的「裸」清单（Kustomize / yaml）
└── local/                     → 本地 Envoy 部署边界（一个 envoy.yaml）

（不属于 deploy/ 的：）
config/recipes/                → 完整用例配方
config/{signal,decision,...}/  → 可复用配置片段
config/runtime/                → 运行时后端示例
tools/                         → 开发工具与辅助服务
website/                       → 公开文档
```

记住一条判定规则：**「它会不会创建或配置一个部署目标？」** 会→放 `deploy/`；不会（只是配置数据、文档、脚本工具）→放别处。

#### 4.1.3 源码精读

[deploy/README.md:1-7](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/README.md#L1-L7) 给出边界总纲：第 3 行声明「`deploy/` 只包含创建或配置部署目标的资产」，第 5–7 行点名 `helm/operator`、`kubernetes/kserve/openshift`、`local` 三组归属。

[deploy/README.md:9-18](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/README.md#L9-L18) 反向声明「路由配置刻意分离」：complete use cases 在 `config/recipes/`、可复用片段在 `config/{signal,decision,algorithm,plugin}/`，并明确「不要把纯文字指南、benchmark 输出、辅助程序或独立 router 示例放进这里」。

这条边界也解释了一个常见困惑：**为什么 Helm chart 的 `values.yaml` 里会内嵌一大段 `config:`？** 因为 chart 要能「开箱即用」地创建一个可运行的部署目标，所以它把一份默认 `config.yaml` 烤进了 ConfigMap；但这只是 chart 的**默认值**，真正的路由策略权威仍属于 `config/`（尤其是 `config/recipes/`）。

#### 4.1.4 代码实践

**实践目标**：亲手验证资产边界，理解「部署产物 vs 路由配置」的物理分离。

**操作步骤**：

1. 列出 `deploy/` 顶层子目录，确认就是 README 说的那几类。
2. 列出 `config/` 顶层，对比它放的是什么。
3. 找一个看起来「既是部署又像配置」的文件，用边界规则判定它归属。

```bash
# 步骤 1：deploy/ 顶层
ls deploy/
# 期望：helm  kserve  kubernetes  local  operator  openshift  OWNER

# 步骤 2：config/ 顶层
ls config/
# 期望看到 recipes/ signal/ decision/ algorithm/ plugin/ runtime/ 等「策略」目录

# 步骤 3：判定 deploy/kserve/configmap-router-config.yaml 归属
# 它在 deploy/ 下，但它承载的是 router config——
# 答：它属于「配置部署目标」的产物（把 config 烤进 ConfigMap 才能部署），所以放 deploy/kserve/。
```

**需要观察的现象**：`deploy/` 下没有任何 `recipes/` 目录；recipe 只存在于 `config/recipes/`。**预期结果**：你会清楚看到「部署形态」与「策略配方」物理隔离。若无法运行命令，可改为用 `Glob`/目录树阅读确认（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：有人想把一个「演示用的 curl 脚本」放进 `deploy/`，可以吗？
**答**：不可以。它不创建/配置部署目标，属于辅助工具，应放 `tools/`；若是对外文档则放 `website/`。`deploy/README.md` 第 16–18 行明确排除「helper programs」。

**练习 2**：`deploy/local/envoy.yaml` 为什么属于 `deploy/` 而 `config/runtime/*.yaml` 不属于？
**答**：前者定义「本地 Envoy 这个部署目标的监听/集群边界」，是部署产物；后者只是运行时后端示例数据，不创建部署目标。

---

### 4.2 Helm chart：router 的 Deployment 与 Service

#### 4.2.1 概念说明

`deploy/helm/semantic-router` 是官方推荐的 K8s 安装方式。它把「一个可运行的 SR 数据面」打包成 chart：渲染出 ServiceAccount、RBAC、ConfigMap、PVC、Deployment、若干 Service、可选的 Ingress/HPA/dashboard。

这里有一个**最容易踩的认知坑**（也是本讲要重点纠正的）：很多人以为「chart 会把 Envoy 和 router 一起部署成一个 Pod」。**实际并非如此**——chart 的 Deployment 里**只有一个容器**，就是 ExtProc router（镜像 `ghcr.io/vllm-project/semantic-router/extproc`）。Envoy 由**部署方自己选用的网关**（Istio / Envoy Gateway / AI Gateway 等）提供，通过 gRPC 调用 chart 部署出来的 router 服务。这与 [u12-l1](u12-l1-vllm-sr-orchestration.md) 讲的本地 Docker「split 拓扑」是同一思想：router 与 envoy 分处独立单元。

#### 4.2.2 核心流程

chart 渲染出的 router 工作负载，其生命周期与 [u4-l1](u4-l1-startup-sequence.md) 的 `main()` 启动序列一一对应：

```text
Deployment(containers: [router])
  ├─ volumeMounts:
  │    ├─ config-volume (ConfigMap)  → /app/config/config.yaml      ← main() 里的 config.Replace 来源
  │    └─ models-volume (PVC)        → /app/models                  ← 模型下载落地（复用，避免重复下载）
  ├─ ports: grpc 50051 / metrics 9190 / classify-api 8080
  ├─ startupProbe → TCP 50051         ← 对应 startup-status 从 503 翻 200 的「就绪」
  ├─ livenessProbe / readinessProbe → TCP 50051
  └─ envFrom: secretRef(vllm-sr-env-secrets)  ← HF_TOKEN 等敏感变量

Service(常规)    : grpc 50051 + api 8080          ← 给集群内客户端/网关用
Service(headless): clusterIP=None, grpc 50051     ← 给 Envoy 做 gRPC 客户端侧负载均衡
Service(metrics) : 9190                            ← 给 Prometheus 抓取
```

注意 chart 有一条**安全闸门**（safety guard）：当 router 是多副本、且配置开启了 Router Learning 的请求期本地状态时，模板会直接 `fail` 拒绝渲染。原因是本地学习状态是 Pod 私有的，多副本会导致各副本「学到不同的东西」而分叉。

#### 4.2.3 源码精读

**(a) 只有一个 router 容器，没有 Envoy sidecar。** 这是判断拓扑的关键证据。

[deploy/helm/semantic-router/templates/deployment.yaml:76-95](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/helm/semantic-router/templates/deployment.yaml#L76-L95) 定义了 `containers:` 下唯一一个容器 `semantic-router`，并暴露三个端口：`grpc`(50051)、`metrics`(9190)、`classify-api`(8080)。整个 Deployment 模板里**搜不到** `envoy` 容器。

**(b) 镜像与可选镜像仓库前缀。**

[deploy/helm/semantic-router/templates/deployment.yaml:64-78](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/helm/semantic-router/templates/deployment.yaml#L64-L78) 先算出可选的 `global.imageRegistry` 前缀（便于走镜像镜像站/私仓），再拼出 `image: <prefix>repository:tag|appVersion`。

**(c) 配置与模型卷挂载——对应 main() 的两个输入。**

[deploy/helm/semantic-router/templates/deployment.yaml:112-132](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/helm/semantic-router/templates/deployment.yaml#L112-L132) 把 ConfigMap 里的 `config.yaml`、`tools_db.json` 以 `subPath` 挂成只读文件；当 `persistence.enabled` 时再挂 models PVC 到 `/app/models`。这正好喂给 [u4-l1](u4-l1-startup-sequence.md) 的配置加载与模型下载两步。

**(d) 三个探活都打 TCP 50051（gRPC 端口）。**

[deploy/helm/semantic-router/templates/deployment.yaml:133-159](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/helm/semantic-router/templates/deployment.yaml#L133-L159) startup/liveness/readiness 都用 `tcpSocket.port=50051`。startupProbe 默认 `failureThreshold=360, periodSeconds=10`（合计 60 分钟），专治「模型下载慢」的冷启动（见 values.yaml 注释）。

**(e) 多副本 + 本地学习状态的安全闸门。**

[deploy/helm/semantic-router/templates/deployment.yaml:25-33](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/helm/semantic-router/templates/deployment.yaml#L25-L33) 计算出 `$localLearningStateEnabled` 后，第 31–33 行：若同时满足「开启安全闸门 + 多副本 + 本地学习状态」就 `{{ fail "..." }}`，Helm 直接报错终止。

**(f) 三个 Service，其中 headless 是为 gRPC 客户端侧负载均衡而设。**

[deploy/helm/semantic-router/templates/service.yaml:22-43](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/helm/semantic-router/templates/service.yaml#L22-L43) 第 23 行注释点明用途「issue #2417」，第 35 行 `clusterIP: None` 把它做成 headless——DNS 直接返回所有 Pod IP，让 Envoy 侧在 gRPC HTTP/2 长连接上自己做客户端负载均衡，绕开 kube-proxy 对长连接的单连接瓶颈。[deploy/helm/semantic-router/templates/service.yaml:1-21](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/helm/semantic-router/templates/service.yaml#L1-L21) 是常规 Service（grpc+api），[service.yaml:44-64](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/helm/semantic-router/templates/service.yaml#L44-L64) 是 metrics Service。

**(g) 关键可配置项**（节选自 [values.yaml](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/helm/semantic-router/values.yaml)）：

| 旋钮 | 行号 | 含义 |
| --- | --- | --- |
| `replicaCount` / `autoscaling` | [L124-L134](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/helm/semantic-router/values.yaml#L124-L134) | 副本数与 HPA（min/max/CPU 目标）。 |
| `safetyGuards.rejectMultiReplicaLocalLearningState` | [L136-L140](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/helm/semantic-router/values.yaml#L136-L140) | 上面那条安全闸门的开关。 |
| `startupProbe.*` | [L188-L199](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/helm/semantic-router/values.yaml#L188-L199) | 冷启动探活（默认最多等 60 分钟下载模型）。 |
| `persistence.*` | [L224-L237](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/helm/semantic-router/values.yaml#L224-L237) | 模型 PVC（storageClass/accessMode/size/existingClaim）。 |
| `service.grpc/api/metrics` | values.yaml 顶部 | 三个端口的暴露策略。 |
| `envFromSecrets` | deployment.yaml [L105-L111](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/helm/semantic-router/templates/deployment.yaml#L105-L111) | 把 `vllm-sr-env-secrets` 等 Secret 以 `envFrom` 注入（HF_TOKEN 等）。 |

子 chart 依赖见 [Chart.yaml](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/helm/semantic-router/Chart.yaml)：redis（语义缓存/Response API）、milvus（语义缓存）、jaeger、prometheus、grafana，均用 `condition:` 按需启用，对应 [values.yaml L634+](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/helm/semantic-router/values.yaml#L634) 的 `dependencies:` 段。

#### 4.2.4 代码实践

**实践目标**：在 `deploy/helm` 中找到 router 的 Deployment/Service 模板，亲自确认「Envoy 与 ExtProc router 在 chart 里到底如何编排」，并记录关键可配置项。

**操作步骤**：

1. 列出 chart 模板，定位 Deployment 与 Service。
2. 在 Deployment 模板里数容器个数与名称。
3. 在 Service 模板里找 headless Service 及其用途注释。
4. 用 `helm template` 本地渲染（无需集群），检查产物里有没有 Envoy。

```bash
cd deploy/helm/semantic-router

# 步骤 1：模板清单
ls templates/
# 期望含 deployment.yaml  service.yaml  configmap.yaml  pvc.yaml ...

# 步骤 2：数 Deployment 的容器
grep -n "name: semantic-router\|name: envoy" templates/deployment.yaml
# 期望：只有 - name: {{ .Chart.Name }}（即 semantic-router），没有 envoy。

# 步骤 3：headless Service
grep -n "clusterIP: None\|issue #2417\|headless" templates/service.yaml

# 步骤 4：本地渲染（无集群），确认渲染出的 Deployment 只有一个容器
helm template sr . --namespace vllm-semantic-router-system \
  | grep -A2 "containers:" | head
```

**需要观察的现象**：步骤 2 只命中一个容器名；步骤 4 渲染出的 `containers:` 列表里只有 `semantic-router`。
**预期结果**：你会得出结论——**chart 并未把 Envoy 与 router 编排进同一个 Deployment/Pod**。chart 只产出 ExtProc router（gRPC 50051）和暴露它的 Service；Envoy 由部署方另选网关、经 gRPC 远程调用 router。若环境无 `helm`，可纯靠阅读 `deployment.yaml`/`service.yaml` 得出同样结论（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：既然 chart 不部署 Envoy，那 chart 部署出来的 router 怎么被外部 HTTP 流量用到？
**答**：router 暴露 gRPC 50051 的 Service（含 headless）。部署方另起一个 Envoy 网关（如 Istio Gateway / Envoy Gateway），在网关侧配置 ext_proc 过滤器把 cluster 指向 `semantic-router.<ns>.svc.cluster.local:50051`。见 4.4 的 Istio 样例。

**练习 2**：为什么 router 的 gRPC 端口要专门配一个 headless Service？
**答**：gRPC 走 HTTP/2 长连接，kube-proxy 的默认负载均衡会在一条长连接上把流量固定到一个后端 Pod，多副本时无法均匀分散。headless Service 让 DNS 返回全部 Pod IP，由 Envoy 在客户端侧做负载均衡（issue #2417）。

---

### 4.3 K8s 清单：sidecar 模型与 gateway 模型

#### 4.3.1 概念说明

`deploy/kubernetes/`、`deploy/kserve/`、`deploy/openshift/` 是各平台的「裸」K8s 清单（Kustomize / yaml）。与 chart 不同，这些清单经常**把 Envoy 和 router 一起给出**，从而呈现两种「Envoy↔router 编排拓扑」：

- **Sidecar 同 Pod 模型**：Envoy 与 router 是同一个 Pod 里的两个容器，通过 `127.0.0.1` 互访。KServe、OpenShift 清单采用此模型。
- **Gateway 远程调用模型**：Envoy 是一个独立的网关（Istio Gateway 等），通过集群网络远程调用 router 的 Service。`deploy/kubernetes/istio/` 采用此模型。

理解这两种模型，就能看懂 `deploy/kubernetes/` 下五花八门的子目录（istio / agentgateway / ai-gateway / llm-d / dynamo / aibrix …）其实都是在为「不同 Envoy 网关实现」提供对接清单。

#### 4.3.2 核心流程

两种模型的数据面是一致的（都走 ExtProc），区别只在 Envoy 与 router 的**部署距离**与**寻址方式**：

```text
Sidecar 模型（KServe/OpenShift）：
  Pod
  ├─ container: semantic-router  (gRPC 50051, api 8080)
  └─ container: envoy-proxy      (HTTP 8801, admin 19000)
       └─ ext_proc cluster → 127.0.0.1:50051   # 同 Pod localhost

Gateway 模型（Istio 等）：
  Deployment: semantic-router  (chart 或 kustomize 部署)
       └─ Service: semantic-router:50051
  独立网关: Istio Gateway (Envoy)
       └─ EnvoyFilter.ext_proc.cluster → semantic-router.<ns>.svc:50051   # 跨 Pod/集群网络
```

#### 4.3.3 源码精读

**(a) Sidecar 模型：KServe 的一个 Pod 两个容器。**

[deploy/kserve/deployment.yaml:145-166](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/kserve/deployment.yaml#L145-L166) 定义 `semantic-router` 容器（镜像 `extproc:latest`，端口 50051/9190/8080）；[deploy/kserve/deployment.yaml:211-220](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/kserve/deployment.yaml#L211-L220) 紧接着定义第二个容器 `envoy-proxy`（镜像 `envoyproxy/envoy:v1.35.3`，端口 8801 http / 19000 admin）。两个容器共享网络命名空间，故互相是 `127.0.0.1`。

**(b) Sidecar 里 Envoy 怎么找到 router。**

[deploy/kserve/configmap-envoy-config.yaml:64-76](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/kserve/configmap-envoy-config.yaml#L64-L76) 在 `http_filters` 里插入 `envoy.filters.http.ext_proc`，其 `grpc_service.envoy_grpc.cluster_name` 指向 `extproc_service`；[configmap-envoy-config.yaml:108-116](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/kserve/configmap-envoy-config.yaml#L108-L116) 定义 `extproc_service` 集群地址为 `127.0.0.1:50051`——正是同 Pod 的 router。同一文件里 `semantic_router_cluster`（router 的 8080 API）也走 `127.0.0.1`，而真正承载流量的 `kserve_backend_cluster` 才指向外部 KServe 模型服务。

**(c) Gateway 模型：Istio 用 EnvoyFilter 把 ext_proc 指向 SR 服务。**

[deploy/kubernetes/istio/envoyfilter.yaml:17-32](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/kubernetes/istio/envoyfilter.yaml#L17-L32) 给 Istio 网关注入一个 `ext_proc` HTTP 过滤器，第 30–32 行的 `cluster_name` 是 `outbound|50051||semantic-router.vllm-semantic-router-system.svc.cluster.local`——即 chart（或 `kubectl apply -k deploy/kubernetes/istio/`）部署出来的 SR 服务。这里 router 与 Envoy 网关**分处不同 Pod**，靠集群 DNS 寻址。`deploy/kubernetes/istio/README.md` 第 1–3 行也点明「Istio Gateway 底层用 Envoy，因此能与 vSR 配合」。

> 旁证：OpenShift 清单同样用 sidecar 模型——[deploy/openshift/deployment.yaml:308-326](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/openshift/deployment.yaml#L308-L326) 是 `semantic-router` 容器，[deployment.yaml:374-381](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/openshift/deployment.yaml#L374-L381) 是 `envoy-proxy` 容器，两者同 Pod。本地开发用的 [deploy/local/envoy.yaml:134-140](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/local/envoy.yaml#L134-L140) 也把 `extproc_service` 指向 `127.0.0.1`，是同一 sidecar 思想在单机上的体现。

#### 4.3.4 代码实践

**实践目标**：用真实清单对比两种拓扑里 Envoy 寻址 router 的写法差异。

**操作步骤**：

1. 在 KServe（sidecar）里找 ext_proc 集群地址。
2. 在 Istio（gateway）里找 ext_proc 集群地址。
3. 对比「localhost vs 集群 DNS」。

```bash
# 步骤 1：sidecar 模型——localhost
grep -n "address: 127.0.0.1\|port_value: 50051" deploy/kserve/configmap-envoy-config.yaml

# 步骤 2：gateway 模型——集群内服务名
grep -n "cluster_name:" deploy/kubernetes/istio/envoyfilter.yaml

# 步骤 3：对比说明
# sidecar: extproc_service → 127.0.0.1:50051       (同 Pod)
# gateway: cluster_name    → outbound|50051||semantic-router.<ns>.svc.cluster.local (跨 Pod)
```

**需要观察的现象**：两处 `cluster_name`/地址的写法截然不同。**预期结果**：你能用一句话讲清「sidecar 靠 localhost、gateway 靠 Service DNS」。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：sidecar 模型里，为什么 router 与 envoy 能用 `127.0.0.1` 互通？
**答**：K8s 里同一 Pod 的所有容器共享网络命名空间（netns），彼此的端口都在同一回环地址上，故 `127.0.0.1:50051` 即同 Pod 的 router。

**练习 2**：gateway 模型相比 sidecar 模型，对 router 的可扩展性有什么影响？
**答**：gateway 模型下 router 是独立 Deployment，可独立扩缩容、可多副本（配合 headless Service 做客户端 LB）；sidecar 模型下 router 随 Pod 扩缩，envoy 与 router 强绑定，扩缩粒度耦合。

---

### 4.4 部署变体：local / openshift / kserve 与网关矩阵

#### 4.4.1 概念说明

`deploy/` 下之所以有多个平台目录，是因为 SR 的数据面（router）是协议无关的 gRPC ExtProc，可以接很多种 Envoy 网关/平台。每种「网关实现 + 平台」组合就构成一个**部署变体**。理解变体的分类，你就能在 `deploy/` 目录树里快速定位自己需要的那一份。

#### 4.4.2 核心流程

按「谁提供 Envoy / 跑在什么平台」两个维度，可以把 `deploy/` 下的变体分类：

| 目录 | 平台 / 网关 | 拓扑 | 典型用途 |
| --- | --- | --- | --- |
| `deploy/helm/semantic-router` | 通用 K8s（不含 Envoy） | router 独立 | 官方推荐起点，自选网关对接 |
| `deploy/operator` | K8s Operator | CRD 驱动 | 声明式 `SemanticRouter` CRD（见 [u12-l3](u12-l3-operator-crd.md)） |
| `deploy/kubernetes/istio` | Istio Gateway (Envoy) | gateway 远程 | Istio + 本地 vLLM 双模型 |
| `deploy/kubernetes/{ai-gateway,agentgateway,aibrix,dynamo,llm-d,streaming,...}` | 各 Envoy 网关/编排 | gateway 远程 | 各网关实现对接 + 各自 `semantic-router-values/` |
| `deploy/kubernetes/observability` | 通用 K8s | 配套 | Prometheus/Grafana/dashboard 栈 |
| `deploy/kserve` | OpenShift AI + KServe | sidecar 同 Pod | 给 KServe `LLMInferenceService` 当智能网关 |
| `deploy/openshift` | OpenShift（独立 vLLM） | sidecar 同 Pod | 不依赖 KServe 的 OpenShift 部署 |
| `deploy/local` | 单机 | sidecar（localhost） | 本地 Envoy 部署边界 |

注意一个规律：`deploy/kubernetes/<gateway>/` 下通常都带一个 `semantic-router-values/values.yaml`——那是「用对应网关时，传给 SR chart 的取值」，体现了「chart 部署 router + 平台清单配置网关」的组合用法。

#### 4.4.3 源码精读

**(a) Helm README 的安装路径与密钥处理——所有变体共用的基线。**

[deploy/helm/README.md:53-77](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/helm/README.md#L53-L77) 推荐「用 `vllm-sr serve --target k8s` 由 CLI 把 `config.yaml` 翻译成 Helm values 再 `helm upgrade --install`」；[README.md:79-116](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/helm/README.md#L79-L116) 说明敏感变量（`HF_TOKEN`/`OPENAI_API_KEY`/`ANTHROPIC_API_KEY`）**绝不**写进明文 values，而是建一个 `vllm-sr-env-secrets` Secret，再由 Deployment 的 `envFrom` 注入。

**(b) chart 的契约护栏。**

[deploy/helm/README.md:43-51](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/helm/README.md#L43-L51) 说明 `values.schema.json` 在渲染前就拒绝非法类型（副本数、HPA、dashboard 持久化、安全闸门），跨字段约束（如「多副本 + 本地学习状态」）则由模板 `fail` 兜住——这正是 4.2 看到的那条安全闸门。

**(c) KServe 变体的平台特性。**

[deploy/kserve/README.md:1-7](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/kserve/README.md#L1-L7) 开宗明义：这是「在 OpenShift AI 上、为 KServe `LLMInferenceService` 当智能网关」的部署；[README.md:88-92](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/deploy/kserve/README.md#L88-L92) 提示要把 Envoy 配置里的 `kserve_backend_cluster` 指向你的 predictor 服务——印证 sidecar 里 Envoy 的「真正后端」是外部 KServe 模型，而 SR 只做 ExtProc 决策。

#### 4.4.4 代码实践

**实践目标**：在 `deploy/kubernetes` 下选一个网关变体，看懂它的 `semantic-router-values` 如何复用 chart。

**操作步骤**：

1. 列出所有带 `semantic-router-values` 的网关变体。
2. 任选一个（如 istio），打开其 `values.yaml`，找出它给 chart 设了哪些值。
3. 思考：这些值如何与「chart 部署 router + 本目录配置网关」配合。

```bash
# 步骤 1：所有网关变体
ls -d deploy/kubernetes/*/semantic-router-values 2>/dev/null
# 期望：ai-gateway agentgateway aibrix dynamo istio llm-d ...

# 步骤 2：看 istio 给 chart 的取值
cat deploy/kubernetes/istio/semantic-router-values/values.yaml
```

**需要观察的现象**：这些 values 通常只覆盖少数键（如镜像、config 路径、service 设置），其余沿用 chart 默认。**预期结果**：你会理解「变体 = chart 取值覆盖 + 网关专属清单」的组合公式。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `deploy/kubernetes/` 下要有这么多子目录（istio/ai-gateway/agentgateway/...）？
**答**：因为 SR 的 router 对网关而言只是个 gRPC ExtProc 端点，而不同 Envoy 网关实现（Istio、Envoy Gateway/AI Gateway、agentgateway、aibrix、dynamo、llm-d）在「如何注入 ext_proc 过滤器、如何声明 HTTPRoute、如何发现后端」上各有差异，每个子目录给出该网关的对接清单与给 chart 的取值。

**练习 2**：一个团队已经在用 Istio，该选哪个变体部署 SR？
**答**：用 `deploy/helm/semantic-router` 部署 router，再用 `deploy/kubernetes/istio/`（`envoyfilter.yaml` + `destinationrule.yaml` + `httproute-*.yaml`）把 Istio 网关的 ext_proc 指向 SR 服务，参考其 `semantic-router-values/values.yaml`。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「部署产物阅读 + 拓扑判定」的综合任务。

**任务**：给定你要在一个标准 K8s 集群里用 Istio 网关跑 SR，请只靠阅读 `deploy/` 下的真实文件，回答并记录以下问题（产出一份简短笔记）：

1. **资产边界**：你会用到 `deploy/` 下哪些目录？哪些东西**绝不**该你改在 `deploy/` 里（而应改 `config/`）？
2. **chart 产物**：`deploy/helm/semantic-router/templates/deployment.yaml` 渲染出的 Deployment 有几个容器？分别是什么？模型与配置分别从哪个卷挂进来？写出对应永久链接与行号。
3. **关键可配置项**：列出你上线前必须确认的至少 5 个 values 旋钮（提示：镜像 tag、副本/HPA、startupProbe、persistence、envFromSecrets），并各给一行说明。
4. **Envoy 与 router 的编排**：诚实地说明「在 chart 里 Envoy 与 router 是否同 Pod」。然后说明在 Istio 变体里 Envoy 如何找到 router（引用 `envoyfilter.yaml` 的 `cluster_name`）。
5. **拓扑对比**：再读 `deploy/kserve/configmap-envoy-config.yaml`，说明 sidecar 模型里 Envoy 如何找到 router，与 Istio 模型的寻址差异。

**验证方式**：

```bash
# 用 helm template 把 chart 渲染出来，肉眼确认容器数（应有 1 个 semantic-router 容器）
helm template sr deploy/helm/semantic-router --namespace vllm-semantic-router-system \
  | grep -E "^kind:|name: (semantic-router|envoy)" | head -40

# 确认 Istio envoyfilter 的 cluster_name
grep -n "cluster_name" deploy/kubernetes/istio/envoyfilter.yaml

# 确认 KServe sidecar 的 localhost 寻址
grep -n "127.0.0.1\|50051" deploy/kserve/configmap-envoy-config.yaml
```

**预期结果**：你的笔记应明确写出——chart 只部署 ExtProc router（单容器），Envoy 不在 chart 内；Istio 靠 `outbound|50051||semantic-router.<ns>.svc.cluster.local` 跨 Pod 寻址，而 KServe 靠 `127.0.0.1:50051` 同 Pod 寻址。若本地无 helm/kubectl，全部结论可由阅读源码得出（待本地验证）。

## 6. 本讲小结

- `deploy/` 有严格资产边界：只放「创建或配置部署目标」的产物；路由策略、recipe、运行时后端示例属于 `config/`、`tools/`，二者物理隔离。
- 官方 Helm chart **只部署 ExtProc router**（单容器，gRPC 50051 / api 8080 / metrics 9190），**不部署 Envoy**；Envoy 由部署方另选网关经 gRPC 远程调用。
- chart 的 router Deployment 与 `main()` 启动序列一一对应：ConfigMap→config、PVC→模型、TCP 50051 探活→就绪；并有一条「多副本 + 本地学习状态」安全闸门会在渲染期 `fail`。
- chart 额外渲染一个 **headless Service**（`clusterIP: None`）专门服务 gRPC 客户端侧负载均衡（issue #2417），绕开 kube-proxy 对 HTTP/2 长连接的局限。
- 平台清单呈现两种拓扑：**sidecar 同 Pod**（KServe/OpenShift，Envoy 经 `127.0.0.1:50051` 访问 router）与 **gateway 远程**（Istio 等，Envoy 经集群 Service DNS 访问 router）。
- `deploy/kubernetes/` 下的众多子目录本质是「不同 Envoy 网关实现」的对接清单 + 给 chart 的 `semantic-router-values` 取值覆盖。

## 7. 下一步学习建议

- 想了解「声明式部署 SR」而非手写 values，请进入 [u12-l3 Operator 与 CRD](u12-l3-operator-crd.md)：看 `SemanticRouter` CRD 如何被 controller 翻译成 router config 与部署。
- 想深入「本地如何用 split 拓扑拉起 Envoy+router」，回顾 [u12-l1 vllm-sr 容器与运行时编排](u12-l1-vllm-sr-orchestration.md)。
- 想理解 router 容器**进程内**如何消费这里挂进去的 config 与模型，回到 [u4-l1 启动主流程](u4-l1-startup-sequence.md) 与 [u4-l3 ExtProc 服务](u4-l3-extproc-server.md)。
- 对可观测性栈（Prometheus/Grafana/Jaeger，对应 chart 的 `dependencies` 与 `deploy/kubernetes/observability`）感兴趣，可预习 [u11-l4 可观测性](u11-l4-observability.md)。
