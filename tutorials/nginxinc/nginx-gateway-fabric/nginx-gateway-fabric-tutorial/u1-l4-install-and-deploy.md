# 安装与部署：Helm 与 Manifests

## 1. 本讲目标

学完本讲，你应当能够：

- 看懂 NGINX Gateway Fabric（以下简称 NGF）的 Helm chart 是怎么组织的：`Chart.yaml`、`values.yaml`、`templates/` 三者各负其责。
- 说出 `templates/` 下每个模板渲染出哪种 Kubernetes 资源，以及控制面 Deployment 启动时带了哪些关键 flag。
- 区分 `examples/helm/` 下的部署变体（default / nodeport / nginx-plus / snippets / azure / openshift / experimental / inference 等），并知道它们和 `deploy/` 单文件 manifests 的关系。
- 讲清楚安装 NGF 时「Gateway API CRDs → NGF CRDs → 控制面 Deployment」的先后顺序，以及 Helm pre-install hook 如何保证证书先于 Deployment 生成。
- 在 kind 集群里用 `helm install` 跑起 NGF，并用 `kubectl` 验证 GatewayClass 是否就绪。

## 2. 前置知识

本讲承接 [u1-l1 项目概览](u1-l1-project-overview.md)、[u1-l2 目录结构](u1-l2-repo-structure.md)、[u1-l3 构建与运行](u1-l3-build-and-run.md)，并补充三个工具概念：

- **Helm**：Kubernetes 的「包管理器」。一个 Helm **chart** = 一组 Go 模板（`templates/*.yaml`）+ 一份默认配置（`values.yaml`）。用 `helm install` 把 chart 装进集群，就得到一个 **release**。安装时可以用 `--set key=val` 或 `-f my-values.yaml` 覆盖默认值，模板会被渲染成真实的 Kubernetes YAML 再创建出来。
- **CRD（CustomResourceDefinition）**：给 Kubernetes「注册新资源类型」的机制。Gateway API 的 `GatewayClass`、`HTTPRoute`，以及 NGF 自定义的 `NginxProxy`、`ClientSettingsPolicy`，都是通过 CRD 引入的。**控制器只能 watch 已经注册的 CRD**——这就是为什么安装顺序很重要。
- **kustomize**：另一种配置管理工具，思路是「拿一份基础 YAML，再叠加小修改」。`kubectl` 已内置 kustomize，可以用 `kubectl kustomize <dir>` 渲染。

一句话回顾 NGF 的身份：它的控制器名是 `gateway.nginx.org/nginx-gateway-controller`（见 [u1-l1](u1-l1-project-overview.md)），只有 `GatewayClass.spec.controllerName` 等于这个名字时，NGF 才会认领这个 GatewayClass。本讲会看到这个值是怎么通过 Helm values 传进模板的。

## 3. 本讲源码地图

本讲围绕「把 NGF 装进集群」这条主线，涉及的关键文件如下：

| 路径 | 作用 |
| --- | --- |
| `charts/nginx-gateway-fabric/Chart.yaml` | Helm chart 元信息（名称、版本、兼容的 Kubernetes 版本）。 |
| `charts/nginx-gateway-fabric/values.yaml` | 全部可配置参数的默认值（控制面/数据面镜像、GatewayClass 名、service 类型、是否用 NGINX Plus 等）。 |
| `charts/nginx-gateway-fabric/templates/deployment.yaml` | 渲染控制面 Deployment（`nginx-gateway` 容器，运行 `controller` 子命令）。 |
| `charts/nginx-gateway-fabric/templates/gatewayclass.yaml` | 渲染 GatewayClass，并通过 `parametersRef` 指向 NginxProxy。 |
| `charts/nginx-gateway-fabric/templates/nginxproxy.yaml` / `nginxgateway.yaml` | 渲染 NGF 的两个自定义 CR：`NginxProxy`（数据面参数）与 `NginxGateway`（控制面动态配置）。 |
| `charts/nginx-gateway-fabric/templates/certs-job.yaml` | 用 Helm **pre-install hook** 跑 `generate-certs` Job，提前生成控制面↔数据面 mTLS 证书。 |
| `examples/helm/*/values.yaml` | 各部署变体的 values 覆盖文件（default、nodeport、nginx-plus、snippets、azure…）。 |
| `deploy/README.md`、`deploy/kustomization.yaml`、`deploy/crds.yaml` | 单文件 manifests 部署方式：一份 NGF CRDs + 每个变体一份 `deploy.yaml`。 |
| `docs/developer/quickstart.md` | 开发者快速上手文档，包含 CRD 安装与 `helm install` 的官方命令。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**Helm chart 结构**、**部署变体**、**CRD 与 Deployment 安装顺序**。

### 4.1 Helm Chart 结构：values + templates

#### 4.1.1 概念说明

NGF 的 Helm chart 位于 `charts/nginx-gateway-fabric/`。一个 chart 由三部分组成：

1. **`Chart.yaml`**：chart 自己的「身份证」——名称、版本、兼容的 Kubernetes 版本等元信息。Helm 用它判断能装到什么样的集群上。
2. **`values.yaml`**：所有可调参数的默认值。这是用户最常打交道的文件——改行为，基本就是改 values。
3. **`templates/`**：一组 Go 模板（`.yaml` 文件里夹着 `{{ ... }}`）。Helm 把 `values.yaml` 的值填进模板，渲染出真正的 Kubernetes 资源清单（Deployment、Service、GatewayClass…），再创建到集群里。

理解 chart 的关键是：**`values.yaml` 决定「装成什么样」，`templates/` 决定「渲染出哪些资源」**。同一个 chart，换一份 values 就能得到完全不同的部署形态（这正是部署变体的原理，见 4.2）。

#### 4.1.2 核心流程

`helm install my-release ./charts/nginx-gateway-fabric` 时，Helm 大致做这些事：

```
1. 读取 Chart.yaml，校验能否装到当前集群（kubeVersion 约束）。
2. 合并配置：values.yaml（默认） < 命令行 --set < -f 指定的覆盖文件。
3. 安装 crds/ 目录里的 CRD（Helm v3：仅首次安装，升级时不改）。
4. 执行所有 templates/*.yaml，用上一步的配置渲染成真实 YAML。
5. 遇到 helm.sh/hook 注解的资源（如证书 Job），按 hook 阶段优先执行。
6. 按依赖顺序创建普通资源（ServiceAccount→Deployment→Service…）。
```

渲染产物中最关键的几类资源：

- **控制面 Deployment**（`nginx-gateway` 容器）：NGF 的大脑，watch 资源、生成配置。
- **GatewayClass**：声明「本 NGF 实例认领哪个 class」。
- **NginxProxy / NginxGateway 两个 CR**：NGF 自己定义的资源，分别承载「数据面参数」和「控制面动态配置」。
- **证书 Job**（pre-install hook）：提前生成 mTLS 证书。

#### 4.1.3 源码精读

先看 chart 的「身份证」[Chart.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/charts/nginx-gateway-fabric/Chart.yaml#L1-L7)：

```yaml
apiVersion: v2
name: nginx-gateway-fabric
version: 2.6.5            # chart 自身版本
appVersion: "edge"        # 打包的 NGF 应用版本（默认 edge，release 会注入真实版本号）
kubeVersion: ">= 1.31.0-0" # 只能装到 Kubernetes ≥ 1.31 的集群
```

`appVersion` 默认是 `edge`，对应 [u1-l3](u1-l3-build-and-run.md) 讲过的「构建期注入版本号」机制。`kubeVersion` 字段是 Helm 的一道前置闸门——如果你的集群版本低于 1.31，`helm install` 会直接报错。

接着看用户最常改的 [values.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/charts/nginx-gateway-fabric/values.yaml#L34) 里的两个关键值：

```yaml
# values.yaml:34 —— GatewayClass 的名字
gatewayClassName: nginx
```

```yaml
# values.yaml:51 —— 控制制器名，必须形如 DOMAIN/PATH，且域名是 gateway.nginx.org
gatewayControllerName: gateway.nginx.org/nginx-gateway-controller
```

这两个值决定了 NGF 认领哪个 GatewayClass。数据面相关的重要开关有：

```yaml
# values.yaml:375 —— 数据面部署形态：deployment 或 daemonSet
  kind: deployment
# values.yaml:439 —— 是否使用商业版 NGINX Plus
  plus: false
# values.yaml:824 —— 数据面 Service 的暴露方式
    type: LoadBalancer
```

这些 value 是怎么变成真实资源的？看控制面 Deployment 模板 [deployment.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/charts/nginx-gateway-fabric/templates/deployment.yaml#L1) 的开头有一个守卫：

```yaml
{{- if eq .Values.nginxGateway.kind "deployment" }}
```

它说明：只有 `nginxGateway.kind=deployment` 时才渲染 Deployment（NGF 控制面目前只支持 deployment 形态）。模板把 `controller` 子命令和一堆 flag 拼进容器的 `args`（见 [u1-l3](u1-l3-build-and-run.md) 提到的五个子命令）：

```yaml
# deployment.yaml:42-49
      - args:
        - controller
        - --gateway-ctlr-name={{ .Values.nginxGateway.gatewayControllerName }}
        - --gatewayclass={{ .Values.nginxGateway.gatewayClassName }}
        - --config={{ include "nginx-gateway.config-name" . }}
        - --service={{ include "nginx-gateway.fullname" . }}
        - --agent-tls-secret={{ .Values.certGenerator.agentTLSSecretName }}
        - --server-tls-domain={{ .Values.serverTLSDomain }}
```

可以看到 `values.yaml` 里的 `gatewayControllerName`、`gatewayClassName` 直接被填进了 `--gateway-ctlr-name` 和 `--gatewayclass` 两个 flag——这正是 [u1-l1](u1-l1-project-overview.md) 讲的「控制器名必须与 GatewayClass 的 controllerName 一致」在部署层面的体现。

GatewayClass 模板 [gatewayclass.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/charts/nginx-gateway-fabric/templates/gatewayclass.yaml#L13-L19) 把同一个控制器名写进 `spec.controllerName`，并用 `parametersRef` 指向 NGF 自定义的 NginxProxy：

```yaml
spec:
  controllerName: {{ .Values.nginxGateway.gatewayControllerName }}
  parametersRef:
    group: gateway.nginx.org
    kind: NginxProxy
    name: {{ include "nginx-gateway.proxy-config-name" . }}
    namespace: {{ .Release.Namespace }}
```

`parametersRef` 是 Gateway API 的标准字段，让 GatewayClass 携带「实现特有参数」。NGF 用它指向 [nginxproxy.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/charts/nginx-gateway-fabric/templates/nginxproxy.yaml#L1-L8) 渲染出的 NginxProxy CR：

```yaml
apiVersion: gateway.nginx.org/v1alpha2
kind: NginxProxy
metadata:
  name: {{ include "nginx-gateway.proxy-config-name" . }}
  namespace: {{ .Release.Namespace }}
spec:
  ...
  kubernetes:        # 描述数据面如何部署（副本数、镜像、service 等）
    deployment: ...
```

类似地，[nginxgateway.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/charts/nginx-gateway-fabric/templates/nginxgateway.yaml#L1-L15) 渲染出 `NginxGateway` CR，承载控制面的动态配置（如日志级别）：

```yaml
apiVersion: gateway.nginx.org/v1alpha1
kind: NginxGateway
metadata:
  name: {{ include "nginx-gateway.config-name" . }}
  namespace: {{ .Release.Namespace }}
spec:
  {{- toYaml .Values.nginxGateway.config | nindent 2 }}   # 比如 logging.level
```

至此，一个 `helm install` 会创建出：控制面 Deployment、GatewayClass、NginxProxy、NginxGateway，以及配套的 Service / ServiceAccount / RBAC 等。模板目录里其余文件（`service.yaml`、`rbac.yaml`、`hpa.yaml`、`pdb.yaml`、`scc.yaml`、`gateway.yaml`）同理，都是「取 values → 渲染资源」。

#### 4.1.4 代码实践：用 helm template 离线渲染 chart

**目标**：不接触集群，把 chart 渲染成 YAML，看清 `values.yaml` 如何变成真实资源。

**操作步骤**：

1. 在仓库根目录执行（命令来自 [examples/helm/README.md](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/helm/README.md#L28)）：

   ```shell
   helm template nginx-gateway --namespace nginx-gateway \
     --values examples/helm/default/values.yaml \
     charts/nginx-gateway-fabric > rendered.yaml
   ```

2. 在 `rendered.yaml` 里分别搜索 `kind: Deployment`、`kind: GatewayClass`、`kind: NginxProxy`、`kind: NginxGateway`，确认这四类资源都被渲染出来了。
3. 在 Deployment 的 `args:` 里找到 `--gateway-ctlr-name=`，确认它的值是 `gateway.nginx.org/nginx-gateway-controller`。

**需要观察的现象**：

- `rendered.yaml` 是一份纯 YAML，不依赖集群就能查看。
- GatewayClass 的 `spec.controllerName` 与 Deployment 的 `--gateway-ctlr-name` 取值完全一致——这正是 NGF 认领 GatewayClass 的依据。

**预期结果**：以上四类 `kind` 都能搜到，且两个控制器名一致。

> 说明：本实践是「源码渲染型」，只读不写集群，可随时运行。`helm template` 不会创建任何资源。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `values.yaml` 里的 `nginxGateway.gatewayClassName` 从 `nginx` 改成 `my-nginx`，会发生什么？

**答案**：模板会把 GatewayClass 的 `metadata.name` 改成 `my-nginx`，同时 Deployment 的 `--gatewayclass=my-nginx` 也跟着变。控制器只会认领 `gatewayClassName == my-nginx` 的资源。注意：此后用户创建的 Gateway/HTTPRoute 也要写 `gatewayClassName: my-nginx` 才会被这个 NGF 处理。

**练习 2**：`Chart.yaml` 里的 `kubeVersion: ">= 1.31.0-0"` 在 `helm install` 时有什么实际作用？

**答案**：它是 Helm 的版本闸门。Helm 会读取目标集群的 Kubernetes 版本，低于 1.31 时直接拒绝安装并报错，避免把不兼容的版本装上去。

---

### 4.2 部署变体：从 examples/helm 到 deploy/

#### 4.2.1 概念说明

现实里部署 NGF 的场景很多：本地开发想用 NodePort、生产用 NGINX Plus、跑在 OpenShift 上需要 SCC、要开启 snippets……如果每种都维护一份完整的 chart，维护成本极高。NGF 的做法是：**只维护一份 chart + 一份默认 values，再用多个「极小的覆盖 values 文件」表达不同变体**。

这些覆盖文件就放在 `examples/helm/` 下，每个子目录一份 `values.yaml`，**只写与 default 不同的字段**。把某个变体的 values 用 `helm template` 渲染出来，就能得到该变体的完整 manifests——`deploy/` 目录下的 `deploy.yaml` 就是这么生成出来的。

#### 4.2.2 核心流程

```
charts/nginx-gateway-fabric/            ← 唯一的 chart（默认 values）
        │
        │  叠加 examples/helm/<变体>/values.yaml
        ▼
helm template ... > deploy/<变体>/deploy.yaml   ← 单文件 manifests（提交进仓库）
```

变体之间的关系可以列成一张表（来源见 [examples/helm/README.md](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/helm/README.md#L11-L18)）：

| 变体 | 与 default 的关键差异 | 适用场景 |
| --- | --- | --- |
| `default` | 无（OSS NGINX，LoadBalancer） | 标准 Kubernetes 集群 |
| `nodeport` | `nginx.service.type=NodePort` | 本地/裸机，没有云负载均衡器 |
| `nginx-plus` | `nginx.plus=true` + 私有镜像 + `imagePullSecret` | 使用商业版 NGINX Plus |
| `snippets` | `nginxGateway.snippets.enable=true` | 启用 Snippets（注入自定义 NGINX 指令） |
| `azure` | 加 `nodeSelector: kubernetes.io/os=linux` | Azure AKS |
| `experimental` / `experimental-nginx-plus` | 开启 Gateway API experimental channel | 尝鲜未稳定的 Gateway API 特性 |
| `inference` / `inference-nginx-plus` | 开启 Gateway API Inference Extension | AI 推理流量（见 [u12-l1](u12-l1-inference-extension.md)） |
| `openshift` | 开启 SecurityContextConstraints（SCC） | Red Hat OpenShift |

#### 4.2.3 源码精读

先看几个变体的覆盖文件有多「小」。`nodeport` 变体 [examples/helm/nodeport/values.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/helm/nodeport/values.yaml#L1-L5) 只改了 service 类型：

```yaml
nginxGateway:
  name: nginx-gateway
nginx:
  service:
    type: NodePort
```

`nginx-plus` 变体 [examples/helm/nginx-plus/values.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/helm/nginx-plus/values.yaml#L1-L8) 改了镜像源、开启 Plus、挂上拉取镜像用的 Secret：

```yaml
nginxGateway:
  name: nginx-gateway
nginx:
  plus: true
  image:
    repository: private-registry.nginx.com/nginx-gateway-fabric/nginx-plus
  imagePullSecret: nginx-plus-registry-secret
```

`snippets` 变体 [examples/helm/snippets/values.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/helm/snippets/values.yaml#L1-L4) 只开了一个开关：

```yaml
nginxGateway:
  name: nginx-gateway
  snippets:
    enable: true
```

回看 [deployment.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/charts/nginx-gateway-fabric/templates/deployment.yaml#L116-L121)，`snippets.enable=true` 会被翻译成 `--snippets` 这个 flag：

```yaml
        {{- if .Values.nginxGateway.snippets.enable }}
        - --snippets
        {{- end }}
```

这就是「values 里一个布尔值 → 模板里一段条件渲染 → 控制面一个 flag」的完整链条。`azure` 变体 [examples/helm/azure/values.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/helm/azure/values.yaml#L1-L8) 则是给控制面和数据面 Pod 都加了 `nodeSelector`，把 Pod 钉在 Linux 节点上：

```yaml
nginxGateway:
  name: nginx-gateway
  nodeSelector:
    kubernetes.io/os: linux
nginx:
  pod:
    nodeSelector:
      kubernetes.io/os: linux
```

这些覆盖文件经 `helm template` 渲染后，产物就是 `deploy/<变体>/deploy.yaml`。`deploy/` 目录提供「单文件 manifests」部署方式——不用 Helm 也能装。`deploy/kustomization.yaml` 默认引用的是 default 变体（[deploy/kustomization.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/deploy/kustomization.yaml#L33-L34)）：

```yaml
resources:
- default/deploy.yaml
```

如果想换镜像仓库或 tag，改这个 `kustomization.yaml` 里的 `images` 段，再 `kubectl kustomize deploy | kubectl apply -f -` 即可——这就是 [deploy/README.md](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/deploy/README.md#L1-L6) 推荐的 manifests 定制方式。

> 小结：**Helm values（可交互、可覆盖）是「源」，`deploy/` 下的单文件 manifests 是「产物」**。两条路通向同一个 NGF。

#### 4.2.4 代码实践：对比两个变体的渲染差异

**目标**：直观看到「一个变体值如何改变最终资源」。

**操作步骤**：

1. 渲染 default：

   ```shell
   helm template ng-default --namespace nginx-gateway \
     --values examples/helm/default/values.yaml charts/nginx-gateway-fabric > default.yaml
   ```

2. 渲染 nodeport：

   ```shell
   helm template ng-nodeport --namespace nginx-gateway \
     --values examples/helm/nodeport/values.yaml charts/nginx-gateway-fabric > nodeport.yaml
   ```

3. 用 `diff` 对比两份文件：

   ```shell
   diff default.yaml nodeport.yaml
   ```

**需要观察的现象**：差异应集中在数据面 Service 上——default 里 `type: LoadBalancer`，nodeport 里 `type: NodePort` 并多了 `nodePort`/端口分配相关字段。其他资源（Deployment、GatewayClass 等）基本一致。

**预期结果**：`diff` 输出的差异行数很少，且都和数据面 Service 的暴露方式相关。这印证了「变体 = 在默认 chart 上做最小覆盖」。

> 说明：本实践为离线渲染，不接触集群，结果可重复。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `examples/helm/nginx-plus/values.yaml` 要写 `imagePullSecret`，而 default 不用？

**答案**：default 用的是公开镜像 `ghcr.io/nginx/nginx-gateway-fabric/nginx`，无需鉴权；nginx-plus 变体从私有仓库 `private-registry.nginx.com` 拉取商业版镜像，必须用 `imagePullSecret` 提供凭据，否则 Pod 会因 `ImagePullBackOff` 起不来。

**练习 2**：如果你想在生产环境用 NGINX Plus 且用 NodePort 暴露，应该怎么做？

**答案**：把 `nginx-plus` 和 `nodeport` 两份 values 的关键字段合并成一份自定义 values（`nginx.plus=true` + 私有镜像 + `imagePullSecret` + `nginx.service.type=NodePort`），再用 `helm install -f my-values.yaml` 安装；或直接基于 nginx-plus 变体渲染后用 kustomize 叠加修改。

---

### 4.3 CRD 与 Deployment 安装顺序

#### 4.3.1 概念说明

装 NGF 之前必须想清楚三套「资源类型」的来源，它们各自独立、安装时机不同：

1. **Gateway API CRDs**（`gateway.networking.k8s.io` 域，如 `GatewayClass`、`Gateway`、`HTTPRoute`）：来自上游 Gateway API 项目，NGF 在 `config/crd/gateway-api/` 下提供了 `standard` 和 `experimental` 两个 channel。**这些 CRD 不在 NGF 的 chart 里**，需要单独安装。
2. **NGF 自定义 CRDs**（`gateway.nginx.org` 域，如 `NginxProxy`、`NginxGateway`、各种 `*Policy`）：在 `config/crd/bases/`，chart 里用 `crds` 符号链接引了进来，会随 chart 一起安装；也汇总在 `deploy/crds.yaml` 单文件里。
3. **（可选）Inference Extension CRDs**（如 `InferencePool`）：开启 inference 变体时才需要，在 `config/crd/inference-extension/`。

为什么顺序重要？因为**控制器只能 watch 已注册的 CRD**。如果 NGF 控制面启动时 `NginxProxy` 这个 CRD 还不存在，它就没法处理 GatewayClass 的 `parametersRef`；如果 `GatewayClass` CRD 不存在，NGF 连认领的对象都没有。此外，控制面↔数据面之间走 mTLS，证书 Secret 必须在控制面 Pod 拉起之前就绪——NGF 用 Helm 的 **pre-install hook** 解决这个问题。

#### 4.3.2 核心流程

以 quickstart 推荐的 Helm 路线为例（[docs/developer/quickstart.md](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/quickstart.md#L169-L178)），安装顺序是：

```
第 0 步（集群已就绪，如 kind）
   │
第 1 步：装 Gateway API CRDs
   kubectl kustomize config/crd/gateway-api/standard | kubectl apply -f -
   │   ← 注册 GatewayClass/Gateway/HTTPRoute… 这些类型
   ▼
第 2 步：helm install NGF
   ├─ Helm 先安装 chart 的 crds/（= config/crd/bases：NGF 自有 CRD）
   ├─ 执行 pre-install hook：证书 Job 跑 generate-certs，生成 server/agent TLS Secret
   └─ 创建 Deployment/GatewayClass/NginxProxy/NginxGateway/Service…
   │   ← 控制面 Pod 启动，认领 GatewayClass，进入「Accepted」状态
   ▼
第 3 步：kubectl get gatewayclass 验证就绪
```

如果走 manifests 路线（不用 Helm），顺序同样是「CRD 在前、控制面在后」，[deploy/README.md](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/deploy/README.md#L4-L6) 明确写了这一点：

> You should have the Gateway API CRDs and the NGINX Gateway Fabric CRDs deployed before applying these manifests. The NGINX Gateway Fabric CRDs can be found in this directory as a single file deployment manifest [crds.yaml](./crds.yaml).

#### 4.3.3 源码精读

先看第 1 步——Gateway API CRDs 的安装命令在 [quickstart.md](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/quickstart.md#L171-L178)：

```shell
kubectl kustomize config/crd/gateway-api/standard | kubectl apply -f -
# 若要实验性特性，改用 experimental channel：
kubectl kustomize config/crd/gateway-api/experimental | kubectl apply -f -
```

注意这两条命令在 `helm install` **之前**执行——因为 Gateway API CRDs 不在 chart 里。

再看第 2 步里证书 Job 的「抢先」机制。证书 Job 的所有相关资源都带 `helm.sh/hook` 注解，例如 [certs-job.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/charts/nginx-gateway-fabric/templates/certs-job.yaml#L10) 里的 ServiceAccount：

```yaml
metadata:
  name: {{ include "nginx-gateway.fullname" . }}-cert-generator
  annotations:
    "helm.sh/hook": pre-install    # 在普通资源创建之前先跑
```

Job 本身则在 [certs-job.yaml:116](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/charts/nginx-gateway-fabric/templates/certs-job.yaml#L116) 标注 `pre-install, pre-upgrade`，并调用 `generate-certs` 子命令（见 [u2-l3](u2-l3-initialize-and-generate-certs.md)）：

```yaml
# certs-job.yaml:127-133
      - args:
        - generate-certs
        - --service={{ include "nginx-gateway.fullname" . }}
        - --cluster-domain={{ .Values.clusterDomain }}
        - --server-tls-domain={{ .Values.serverTLSDomain }}
        - --server-tls-secret={{ .Values.certGenerator.serverTLSSecretName }}
        - --agent-tls-secret={{ .Values.certGenerator.agentTLSSecretName }}
```

`generate-certs` 会自签 CA 并生成 server / agent 两套 TLS 证书，写进两个 Secret。随后，控制面 Deployment 在 [deployment.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/charts/nginx-gateway-fabric/templates/deployment.yaml#L242-L245) 把 server 证书 Secret 挂进容器：

```yaml
      volumes:
      - name: nginx-agent-tls
        secret:
          secretName: {{ .Values.certGenerator.serverTLSSecretName }}
```

由于证书 Job 是 pre-install hook，它一定先于 Deployment 完成——这就保证了控制面 Pod 启动时，要挂的证书 Secret 已经存在，不会因 `SecretNotFound` 反复重启。证书 Job 默认开启（[values.yaml:879-881](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/charts/nginx-gateway-fabric/values.yaml#L879-L881)，`certGenerator.enable: true`），关掉它就必须改用 cert-manager 之类的方式自己造这两个 Secret。

> 顺序的精髓：**外部 CRD（Gateway API）手动装在前 → chart 自带 CRD 随 Helm 装 → pre-install hook 造证书 → 控制面 Deployment 启动**。每一步都为下一步扫清前置依赖。

#### 4.3.4 代码实践：在 kind 中部署 NGF 并验证 GatewayClass 就绪

**目标**：完整跑一遍「装 CRD → helm install → 验证就绪」，观察 GatewayClass 是否被 NGF 认领。

**前置条件**：已按 [u1-l3](u1-l3-build-and-run.md) 建好 kind 集群，并构建/加载好本地镜像（`make create-kind-cluster` + `make build-images` + `kind load docker-image ...`）。

**操作步骤**（命令取自 [quickstart.md](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/quickstart.md#L181-L187)）：

1. 装 Gateway API CRDs：

   ```shell
   kubectl kustomize config/crd/gateway-api/standard | kubectl apply -f -
   ```

2. 用本地镜像 Helm 安装 NGF，并用 NodePort 暴露数据面：

   ```shell
   helm install my-release ./charts/nginx-gateway-fabric \
     --create-namespace --wait \
     --set nginx.service.type=NodePort \
     --set nginxGateway.image.repository=nginx-gateway-fabric \
     --set nginxGateway.image.tag=$(whoami) \
     --set nginxGateway.image.pullPolicy=Never \
     --set nginx.image.repository=nginx-gateway-fabric/nginx \
     --set nginx.image.tag=$(whoami) \
     --set nginx.image.pullPolicy=Never \
     -n nginx-gateway
   ```

3. 验证 GatewayClass 是否就绪：

   ```shell
   kubectl get gatewayclass
   kubectl describe gatewayclass nginx
   ```

**需要观察的现象**：

- `helm install` 阶段会先看到一个 `*-cert-generator` Job（pre-install hook）跑完，然后 `nginx-gateway` Deployment 才起来。
- `kubectl get gatewayclass` 应显示名为 `nginx` 的 GatewayClass。
- `kubectl describe gatewayclass nginx` 的 Conditions 里应出现 `Accepted=True`（表示被 NGF 认领），`controllerName` 应为 `gateway.nginx.org/nginx-gateway-controller`。

**预期结果**：

```
NAME    CONTROLLER                                      ACCEPTED
nginx   gateway.nginx.org/nginx-gateway-controller     True
```

> 说明：`pullPolicy=Never` 是本地 kind 的关键——强制用本地已加载的镜像，而不是去远端拉取（见 [u1-l3](u1-l3-build-and-run.md)）。如果你尚未构建镜像，可改为直接用远端 `edge` 镜像（去掉几个 `--set ...image...` 参数，保留 `pullPolicy=Always`）。具体运行结果与本地镜像/集群状态相关，若 Pod 起不来，先查 `kubectl describe pod` 与镜像是否已 `kind load`。

#### 4.3.5 小练习与答案

**练习 1**：如果跳过第 1 步（不装 Gateway API CRDs），直接 `helm install`，会发生什么？

**答案**：Helm 能装上 NGF 的 Deployment 和 NginxProxy 等 NGF 自有资源，但 GatewayClass 这个 CRD 不存在，chart 里 `gatewayclass.yaml` 渲染出的 `GatewayClass` 资源会创建失败（报「unknown kind」）。即使强制创建成功，控制面也无处认领，GatewayClass 永远不会变 `Accepted`。所以 Gateway API CRDs 必须先装。

**练习 2**：把 `certGenerator.enable` 设为 `false` 会怎样？

**答案**：pre-install 的证书 Job 不会运行，server/agent TLS Secret 不会被创建。控制面 Deployment 仍会尝试挂载 `serverTLSDomain` 对应的 Secret，由于 Secret 不存在，Pod 会启动失败。此时必须改用 cert-manager 或手动创建这两个 Secret，才能让控制面正常拉起。

**练习 3**：为什么 `deploy/crds.yaml`（NGF 自有 CRD）和 Gateway API CRDs 要分开装？

**答案**：归属不同。Gateway API CRDs 属于上游 Gateway API 项目，版本和发布节奏由上游控制；NGF 自有 CRDs（`gateway.nginx.org` 域）由 NGF 自己定义、随 NGF 版本演进。把它们分开，既能让 NGF 跟随上游 Gateway API 的兼容版本（见 [u1-l1](u1-l1-project-overview.md) 版本矩阵），又能独立升级 NGF 自身。

## 5. 综合实践

把本讲三个模块串起来，完成一次「从零到就绪」的部署并解释每一步。

**任务**：在 kind 集群中部署 NGF 的 `nodeport` 变体，并对照源码解释整条链路。

1. **准备变体 values**：复制 `examples/helm/nodeport/values.yaml`，但把镜像改成你本地构建的版本（参考 4.3.4 的 `--set ...image...` 思路，或直接编辑这份 values）。
2. **安装 CRD**：`kubectl kustomize config/crd/gateway-api/standard | kubectl apply -f -`。
3. **Helm 安装**：`helm install ngf ./charts/nginx-gateway-fabric -n nginx-gateway --create-namespace -f your-nodeport-values.yaml`。
4. **验证**：
   - `kubectl get job -n nginx-gateway` 看到 cert-generator Job（确认 pre-install hook 生效）。
   - `kubectl get deploy,svc,gatewayclass -n nginx-gateway`，确认 Deployment、数据面 Service（`type: NodePort`）、GatewayClass 都在。
   - `kubectl get gatewayclass nginx -o jsonpath='{.status.conditions[*].type}{"="}{.status.conditions[*].status}{"\n"}'`，确认 `Accepted=True`。
5. **回溯源码**：针对 `nginx.service.type=NodePort` 这个设置，画出「你的 values → `nginxproxy.yaml` / `service.yaml` 模板 → 渲染出的 Service 资源」链路；再针对 `Accepted=True`，说明它依赖「GatewayClass CRD 已装 + 控制器名匹配 + 控制面已启动」三个前提。

**验收标准**：能口头解释清楚「为什么 CRD 必须先于 Deployment」「pre-install hook 解决了什么」「一个变体值如何穿透到最终资源」这三个问题。

> 若本地无 kind/Docker 环境，可降级为纯渲染实践：对自定义 values 跑 `helm template`，人工检查渲染产物是否正确，并写出对上述三个问题的文字解释。

## 6. 本讲小结

- NGF 的 Helm chart 由 `Chart.yaml`（元信息）、`values.yaml`（默认配置）和 `templates/`（渲染逻辑）三部分组成；用户改行为主要是改 values。
- `templates/deployment.yaml` 渲染控制面 Deployment，把 `gatewayControllerName` / `gatewayClassName` 等值翻译成 `controller` 子命令的 flag；`gatewayclass.yaml` 用 `parametersRef` 把 GatewayClass 指向 `NginxProxy` CR。
- `templates/certs-job.yaml` 通过 `helm.sh/hook: pre-install` 抢在 Deployment 之前运行 `generate-certs`，保证 mTLS 证书 Secret 先就绪（默认开启）。
- 部署变体本质是「在唯一 chart 之上做最小 values 覆盖」，`examples/helm/<变体>/values.yaml` 是源，`deploy/<变体>/deploy.yaml` 是渲染产物；两条路（Helm / manifests）殊途同归。
- 安装顺序铁律：**Gateway API CRDs（外部）→ NGF 自有 CRDs（随 chart / `deploy/crds.yaml`）→ 证书 hook → 控制面 Deployment**；顺序错了控制器无法 watch 所需资源。
- 部署后用 `kubectl get gatewayclass` 验证 `Accepted=True`，是判断 NGF 是否成功认领 GatewayClass 的关键信号。

## 7. 下一步学习建议

- 想深入「`helm install` 之后控制面到底做了什么」，进入 **[u2-l1 命令行子命令总览](u2-l1-cli-subcommands-overview.md)** 与 **[u2-l2 controller 命令与运行时配置](u2-l2-controller-command-and-config.md)**，本讲看到的 `controller` 子命令和它的 flag 会在那里被逐个拆开。
- 想理解证书生成细节（`generate-certs` / `initialize`），看 **[u2-l3 初始化命令与证书生成](u2-l3-initialize-and-generate-certs.md)**，本讲的 pre-install hook 会在那里展开。
- 想看「装好后如何路由第一个应用」，进入 **[u1-l5 运行第一个例子](u1-l5-first-example.md)**，用 examples 端到端体验 Gateway/HTTPRoute。
- 想从源码层面理解 NginxProxy / NginxGateway 这些 CR 的定义，后续可参考 `apis/v1alpha1`、`apis/v1alpha2` 目录（对应 **[u8-l2 自定义 CRD 与策略附着](u8-l2-crd-api-and-policy-attachment.md)**）。
