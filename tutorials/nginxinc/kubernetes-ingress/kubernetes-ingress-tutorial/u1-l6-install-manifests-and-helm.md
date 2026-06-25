# 安装方式：Kubernetes 清单与 Helm Chart

## 1. 本讲目标

在前几讲里，我们已经认识了 NIC（NGINX Ingress Controller）的定位、目录结构和命令行入口。本讲回答一个更落地的问题：**这套程序到底以什么形态跑进 Kubernetes 集群？**

学完本讲，你应当能够：

- 看懂 `deployments/` 目录下手写 YAML 清单的组成，并能说出一份完整安装至少需要哪几类资源。
- 理解 `charts/nginx-ingress` 这个 Helm Chart 的标准结构（`Chart.yaml` / `values.yaml` / `templates/` / `crds/`），以及它与 `deployments/` 清单在表达方式上的差别。
- 用一个真实示例（Cafe）跑通「标准 Ingress」和「VirtualServer 自定义资源」两种路由写法，并理解二者的字段差异。
- 亲手用 `helm template` 把 Chart 渲染成 YAML，再与 `deployments/` 清单对照，分清 Deployment、ConfigMap、RBAC、Service 各自由哪个文件产生。

## 2. 前置知识

在进入源码前，先用大白话铺三个概念。

**一、安装 NIC，本质是往集群里放一组 Kubernetes 资源。** NIC 本身是一个在 Pod 里运行的 Go 程序（前几讲里的 `nginx-ingress` 二进制）。要让它跑起来，集群里至少要有：一个跑它的 Pod（由 Deployment/DaemonSet/StatefulSet 管理）、一个让流量进来的 Service、一个给它权限的 ServiceAccount + RBAC、一份给它的全局配置 ConfigMap，以及一个声明「我处理哪个 ingress class」的 IngressClass。这些资源可以用「一坨手写 YAML 清单」表达，也可以用「一个 Helm Chart」表达——两者是**同一件事的两种包装**。

**二、什么是 Helm。** Helm 是 Kubernetes 的「包管理器」，类比 yum/apt。一个 Helm Chart 就是一个带模板的安装包：`values.yaml` 给默认参数，`templates/` 下的 `.yaml` 文件用 Go template 语法写成可参数化模板，`helm install` 时把参数填进模板、渲染成最终清单、再交给 Kubernetes。所以 Chart 比 `deployments/` 清单多了「参数化」和「组合」的能力，但底层产物仍是普通 YAML。

**三、承接前几讲的两个关键约定。** 其一，前几讲（u1-l4）讲过 NIC 启动时依赖几个核心 flag：`-nginx-configmaps`（数据面全局配置来源）、`-ingress-class`（默认 `nginx`）、`-enable-leader-election`。本讲你会看到，这些 flag 的取值正是由安装清单/Chart 里的 `args` 段或对应 value 决定的。其二，前几讲（u1-l5）讲过镜像有 OSS/Plus 等变体，本讲你会看到安装清单也分 `nginx-ingress.yaml`（OSS）与 `nginx-plus-ingress.yaml`（Plus）两套。

> 名词速查：**清单（manifest）**指一份描述 Kubernetes 资源的 YAML 文件；**kind** 指资源类型（Deployment、Service 等）；**RBAC** 指 Role-Based Access Control，即给 ServiceAccount 授权的机制；**IngressClass** 是 Kubernetes 内置资源，用来声明「这个 Ingress Controller 处理哪一类 Ingress」。

## 3. 本讲源码地图

本讲涉及的关键文件如下。它们都是真实存在的仓库文件，本讲所有引用都指向它们。

| 文件 | 作用 |
| --- | --- |
| `deployments/deployment/nginx-ingress.yaml` | OSS 版的 Deployment 手写清单（NIC Pod 的「出生证明」） |
| `deployments/common/nginx-config.yaml` | 数据面全局 ConfigMap（NIC 默认全局配置的占位） |
| `deployments/common/ns-and-sa.yaml` | Namespace + ServiceAccount |
| `deployments/common/ingress-class.yaml` | 名为 `nginx` 的 IngressClass |
| `deployments/service/loadbalancer.yaml` | LoadBalancer 类型的 Service（把流量引进来） |
| `charts/nginx-ingress/Chart.yaml` | Helm Chart 的「身份证」 |
| `charts/nginx-ingress/values.yaml` | Helm Chart 的全部默认参数 |
| `charts/nginx-ingress/templates/controller-deployment.yaml` | 渲染 Deployment 的模板 |
| `charts/nginx-ingress/templates/controller-configmap.yaml` | 渲染 ConfigMap 的模板 |
| `charts/nginx-ingress/templates/_helpers.tpl` | 命名模板（给资源取统一名字） |
| `examples/ingress-resources/complete-example/cafe-ingress.yaml` | 标准 Ingress 路由示例 |
| `examples/custom-resources/basic-configuration/cafe-virtual-server.yaml` | VirtualServer 自定义资源示例 |
| `examples/custom-resources/basic-configuration/cafe.yaml` | 示例应用（coffee/tea 的 Deployment+Service） |

## 4. 核心概念与源码讲解

### 4.1 部署清单结构（deployments/）

#### 4.1.1 概念说明

`deployments/` 目录存放的是**手写、可直接 `kubectl apply` 的原始 YAML 清单**。它不依赖 Helm，是最「朴素」的安装方式，适合需要完全掌控每行 YAML、或所在环境没有 Helm 的场景。

回忆 u1-l3 讲过的目录地图：`deployments/` 下的子目录是按「部署拓扑」和「关注点」分的。本讲我们关心这四类子目录：

- `deployments/deployment/`（或 `daemon-set/`、`stateful-set/`）：决定 NIC Pod 以哪种工作负载形态运行。
- `deployments/common/`：所有安装形态都要用的公共资源（命名空间、ServiceAccount、IngressClass、ConfigMap）。
- `deployments/rbac/`：RBAC 授权（ClusterRole/ClusterRoleBinding）。
- `deployments/service/`：把 NIC 暴露出去的 Service（LoadBalancer / NodePort 等）。

每个 kind 通常是一个独立文件，方便按需挑选组合。

#### 4.1.2 核心流程

一份能跑起来的最小安装，需要把下面这几块拼起来（注意它们是**多个文件**，不是单文件）：

```text
1. ns-and-sa.yaml      → Namespace + ServiceAccount        （NIC 跑在哪、以谁的身份跑）
2. rbac/rbac.yaml      → ClusterRole + ClusterRoleBinding  （授权 NIC 监听/读写集群资源）
3. ingress-class.yaml  → IngressClass(name=nginx)         （声明处理哪类 Ingress）
4. nginx-config.yaml   → ConfigMap(name=nginx-config)     （NIC 全局配置，对应 -nginx-configmaps）
5. deployment/nginx-ingress.yaml → Deployment             （真正跑 NIC 二进制的 Pod）
6. service/loadbalancer.yaml     → Service                （把 80/443 流量引进 NIC）
```

执行时通常是：

```text
kubectl apply -f deployments/common/ns-and-sa.yaml
kubectl apply -f deployments/rbac/rbac.yaml
kubectl apply -f deployments/common/ingress-class.yaml
kubectl apply -f deployments/common/nginx-config.yaml
kubectl apply -f deployments/deployment/nginx-ingress.yaml
kubectl apply -f deployments/service/loadbalancer.yaml
```

这六步不是任意的，**前五步是第六步（真正接流量）的前提**：没有 RBAC，NIC 没权限读 Ingress；没有 ConfigMap，NIC 找不到全局配置；没有 Deployment，根本没有 NIC 进程在跑。

#### 4.1.3 源码精读

**先看 NIC Pod 的「出生证明」——Deployment 清单。** [deployments/deployment/nginx-ingress.yaml:1-10](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/deployments/deployment/nginx-ingress.yaml#L1-L10) 定义了一个名为 `nginx-ingress` 的 Deployment，`replicas: 1`，并通过 `selector.matchLabels.app: nginx-ingress` 与 Pod 模板的标签匹配。

容器镜像与端口在这里：[deployments/deployment/nginx-ingress.yaml:37-49](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/deployments/deployment/nginx-ingress.yaml#L37-L49)。可以看到镜像 `nginx/nginx-ingress:5.5.0`，并暴露了四个端口：`http`(80)、`https`(443)、`readiness-port`(8081)、`prometheus`(9113)。其中 8081 是就绪探针用的，9113 是 Prometheus 指标端口。

就绪探针走的就是 8081 上的 `/nginx-ready`：[deployments/deployment/nginx-ingress.yaml:50-54](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/deployments/deployment/nginx-ingress.yaml#L50-L54)。

**这里是最关键的承接点——容器启动参数 `args`。** [deployments/deployment/nginx-ingress.yaml:92-95](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/deployments/deployment/nginx-ingress.yaml#L92-L95) 写着：

```yaml
args:
  - -nginx-configmaps=$(POD_NAMESPACE)/nginx-config
  - -report-ingress-status
  - -external-service=nginx-ingress
```

这三个 flag 正是 u1-l4 里讲过的核心 flag：`-nginx-configmaps` 指向上面那份 ConfigMap，`-report-ingress-status -external-service=nginx-ingress` 让 NIC 把外部地址写回 Ingress 的 status 字段。注意 `$(POD_NAMESPACE)` 引用了下面这段注入的环境变量：[deployments/deployment/nginx-ingress.yaml:83-91](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/deployments/deployment/nginx-ingress.yaml#L83-L91)，它用 downward API 把 Pod 的命名空间和名字取出来。

容器的安全上下文也值得一看：[deployments/deployment/nginx-ingress.yaml:62-71](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/deployments/deployment/nginx-ingress.yaml#L62-L71)，`runAsUser: 101`（nginx 用户）、`runAsNonRoot: true`，capabilities 全 drop 后只加 `NET_BIND_SERVICE`（绑定 <1024 特权端口的权限，因为要监听 80/443）。

**再看 ConfigMap。** [deployments/common/nginx-config.yaml:1-7](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/deployments/common/nginx-config.yaml#L1-L7) 是一个 **`data:` 段为空** 的 ConfigMap。这很正常：它是一个「占位」骨架，默认不需要任何全局参数也能跑；当你需要改 NGINX 全局行为时（如 `worker-processes`、`error-log-level`），就在这里加 key。

**Namespace 与 ServiceAccount**：[deployments/common/ns-and-sa.yaml:1-11](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/deployments/common/ns-and-sa.yaml#L1-L11) 用一份文件（`---` 分隔）同时定义了 Namespace `nginx-ingress` 和 ServiceAccount `nginx-ingress`。Deployment 里 `serviceAccountName: nginx-ingress`（[deployments/deployment/nginx-ingress.yaml:21](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/deployments/deployment/nginx-ingress.yaml#L21)）正是引用它。

**IngressClass**：[deployments/common/ingress-class.yaml:1-9](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/deployments/common/ingress-class.yaml#L1-L9) 定义了名为 `nginx` 的 IngressClass，`spec.controller: nginx.org/ingress-controller`。这正是 NIC 默认 `-ingress-class=nginx`（u1-l4）要匹配的对象。

**Service**：[deployments/service/loadbalancer.yaml:1-20](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/deployments/service/loadbalancer.yaml#L1-L20) 是一个 `type: LoadBalancer` 的 Service，把 80/443 转发到 NIC Pod，`externalTrafficPolicy: Local` 保留客户端源 IP，`selector.app: nginx-ingress` 把它和 Deployment 的 Pod 绑在一起。

#### 4.1.4 代码实践

**实践目标**：在本地仓库里手动把这份「散装」清单在纸面上拼成一份完整安装。

**操作步骤**：

1. 打开 `deployments/deployment/nginx-ingress.yaml`，找到 `args` 段（L92 附近）。
2. 对照 `deployments/common/nginx-config.yaml`，确认 `-nginx-configmaps=$(POD_NAMESPACE)/nginx-config` 指向的 ConfigMap 名字、命名空间都对得上。
3. 对照 `deployments/common/ns-and-sa.yaml`，确认 Deployment 的 `serviceAccountName` 指向了这里定义的 ServiceAccount。
4. 对照 `deployments/service/loadbalancer.yaml` 的 `selector` 与 Deployment Pod 模板的 `labels`（`app: nginx-ingress`），确认 Service 能正确选中 Pod。

**需要观察的现象**：这是一份「源码阅读型」实践，不需要运行集群。你要观察的是**各文件之间的引用是否自洽**——Service 的 selector、Deployment 的 serviceAccountName、args 里的 ConfigMap 名字，构成一个闭环。

**预期结果**：你会得出结论——六份文件通过名字（labels / serviceAccountName / configmap 名）互相引用，缺任何一份，NIC 都无法正常工作。

#### 4.1.5 小练习与答案

**练习 1**：如果集群里已经有同名 ServiceAccount，但你想换一个 namespace 部署 NIC，至少需要改哪几个文件？

**参考答案**：至少改 `ns-and-sa.yaml`（新 Namespace）、`deployment/nginx-ingress.yaml`（`metadata.namespace` 与 `args` 里 `$(POD_NAMESPACE)` 解析后的引用对象仍正确，因为用了环境变量，主要改 namespace 字段）、`service/loadbalancer.yaml` 和 `nginx-config.yaml`（namespace 字段）、`rbac.yaml`（若使用了 namespace 级 RoleBinding）。因为它们都硬编码了 `namespace: nginx-ingress`。

**练习 2**：为什么 ConfigMap 的 `data:` 为空，NIC 也能跑？

**参考答案**：空 `data:` 只意味着「没有覆盖任何 NGINX 全局参数」，NIC 会在内部用代码里的默认值生成 `nginx.conf`。ConfigMap 的作用是「按需覆盖」，不是「必填」。

---

### 4.2 Helm Chart 结构（charts/nginx-ingress）

#### 4.2.1 概念说明

`charts/nginx-ingress` 是 NIC 的官方 Helm Chart。和 `deployments/` 手写清单相比，它的优势是**参数化**：你不用手动改 YAML，只要改 `values.yaml`（或 `--set`），就能切换 OSS/Plus、Deployment/DaemonSet、是否开启 Prometheus、是否开启选举等几十个开关。

回忆 u1-l3：Chart 顶层有 `Chart.yaml`、`values.yaml`、`values.schema.json`、`templates/`、`crds/`。本讲我们把前三个和 templates 串起来看。

#### 4.2.2 核心流程

Helm 安装的执行流程可以概括为：

```text
helm install <release> nginx-in/nginx-ingress -n nginx-ingress -f my-values.yaml
        │
        ▼
读 Chart.yaml（身份/appVersion）  ──┐
读 values.yaml + 用户覆盖          ──┼─► 合并出最终 .Values
读 values.schema.json（校验）      ──┘
        │
        ▼
渲染 templates/*.yaml（Go template，把 .Values 填进去）
        │
        ▼
把渲染好的清单 + crds/*.yaml 交给 Kubernetes 创建
        │
        ▼
打印 templates/NOTES.txt（安装后提示）
```

关键点：`templates/` 下每个 `.yaml` 文件通常对应一种资源，并且开头都有 `{{- if ... }}` 守卫——**根据 values 决定要不要渲染这个资源**。这就是为什么 Chart 能同时支持 Deployment、DaemonSet、StatefulSet 三种形态：它们是三个不同模板文件，由 `controller.kind` 这个 value 决定渲染哪一个。

#### 4.2.3 源码精读

**Chart 的身份证——`Chart.yaml`。** [charts/nginx-ingress/Chart.yaml:1-8](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/charts/nginx-ingress/Chart.yaml#L1-L8)：

```yaml
apiVersion: v2
name: nginx-ingress
version: 2.7.0
appVersion: 5.6.0
kubeVersion: ">= 1.25.0-0"
```

这里要分清两个版本号：`version: 2.7.0` 是 **Chart 自己的版本**，`appVersion: 5.6.0` 是 **NIC 程序的版本**（与 u1-l2 里 `.github/data/version.txt` 的 `IC_VERSION=5.6.0` 一致，也是镜像 tag 的默认值）。`kubeVersion: ">= 1.25.0-0"` 声明了支持的 Kubernetes 下限，`helm install` 时 Helm 会据此做兼容性检查。

**参数全集——`values.yaml`。** 这是 Chart 里最长、最重要的文件。几个承接前几讲的例子：

- 工作负载形态与副本数：[charts/nginx-ingress/values.yaml:5-6](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/charts/nginx-ingress/values.yaml#L5-L6) `kind: deployment`，决定渲染哪个工作负载模板。
- NGINX Plus 开关：[charts/nginx-ingress/values.yaml:15](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/charts/nginx-ingress/values.yaml#L15) `nginxplus: false`，对应 u1-l5 讲的镜像变体。
- 重载超时：[charts/nginx-ingress/values.yaml:52-53](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/charts/nginx-ingress/values.yaml#L52-L53) `nginxReloadTimeout: 60000`（毫秒）。
- ingress class：[charts/nginx-ingress/values.yaml:418](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/charts/nginx-ingress/values.yaml#L418) `name: nginx`，对应手写清单里的 IngressClass 和 u1-l4 的 `-ingress-class`。
- Service：[charts/nginx-ingress/values.yaml:492-497](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/charts/nginx-ingress/values.yaml#L492-L497) `create: true` + `type: LoadBalancer`，对应手写清单的 `loadbalancer.yaml`。
- 选举开关：[charts/nginx-ingress/values.yaml:611](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/charts/nginx-ingress/values.yaml#L611) `enableLeaderElection: true`，对应 u1-l4 的 `-enable-leader-election`。
- 指标：[charts/nginx-ingress/values.yaml:709-711](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/charts/nginx-ingress/values.yaml#L709-L711) `prometheus.create: true`，对应 Deployment 里 9113 端口。

**模板如何消费 values——以 Deployment 模板为例。** [charts/nginx-ingress/templates/controller-deployment.yaml:1-6](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/charts/nginx-ingress/templates/controller-deployment.yaml#L1-L6) 开头第一行 `{{- if eq .Values.controller.kind "deployment" }}` 就是守卫：只有当 `controller.kind` 等于 `deployment` 时才渲染这个文件。资源名通过命名模板 `include "nginx-ingress.controller.fullname"` 生成（见下）。

副本数的条件渲染也很典型：[charts/nginx-ingress/templates/controller-deployment.yaml:13-14](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/charts/nginx-ingress/templates/controller-deployment.yaml#L13-L14)，意思是「只有没开 HPA 时才写死 replicas，开了 HPA 就让 HPA 管」。

**ConfigMap 模板——对接 `-nginx-configmaps`。** [charts/nginx-ingress/templates/controller-configmap.yaml:1-14](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/charts/nginx-ingress/templates/controller-configmap.yaml#L1-L14) 把 `controller.config.entries`（默认 `{}`）原样渲染成 ConfigMap 的 `data:`。这正好对应手写清单里那份空 `nginx-config.yaml`——Chart 里默认也是空的，你想加全局参数就写 `controller.config.entries`。

**命名模板——`_helpers.tpl`。** [charts/nginx-ingress/templates/_helpers.tpl:6-8](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/charts/nginx-ingress/templates/_helpers.tpl#L6-L8) 定义了 `nginx-ingress.name`，[charts/nginx-ingress/templates/_helpers.tpl:32-34](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/charts/nginx-ingress/templates/_helpers.tpl#L32-L34) 定义了 `nginx-ingress.controller.fullname`（格式 `<release>-<chart>-controller`，并截断到 63 字符）。所有模板都通过这些 helper 取名字，保证 release 之间不冲突。

#### 4.2.4 代码实践

**实践目标**：不安装、只渲染，亲眼看到 Chart 产出的就是普通 YAML。

**操作步骤**：

1. 确保本机有 `helm`（没有可参考官方安装文档，或用 `make` 不涉及 helm，本步可跳过阅读型替代，见下方说明）。
2. 在仓库根目录执行：

   ```bash
   helm template my-release ./charts/nginx-ingress -n nginx-ingress > rendered.yaml
   ```

3. 打开 `rendered.yaml`，搜索 `kind:`，统计一共渲染出了哪些 kind。

**需要观察的现象**：你会看到一连串被 `---` 分隔的资源块，包括 Namespace/ServiceAccount、ClusterRole/ClusterRoleBinding、ConfigMap、Deployment、Service、IngressClass、Secret 等。

**预期结果**：渲染出的每个资源块，结构与 `deployments/` 里的手写清单高度相似，区别只是名字带了 release 前缀（`my-release-nginx-ingress-...`）、参数来自 values。**待本地验证**：实际渲染出的 kind 列表，因为不同 Chart 版本/默认 values 下渲染的资源集合会有差异。

> 若本机没有 helm：可改为阅读 `templates/NOTES.txt`（安装后提示文本，见 [charts/nginx-ingress/templates/NOTES.txt:1-3](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/charts/nginx-ingress/templates/NOTES.txt#L1-L3)），理解 Chart 安装后会打印什么；以及阅读 `templates/` 目录列表，数一下有多少个 `controller-*.yaml` 模板文件。

#### 4.2.5 小练习与答案

**练习 1**：`Chart.yaml` 里 `version` 和 `appVersion` 有什么区别？为什么要把它们分开？

**参考答案**：`version` 是 Chart 打包格式自身的版本，`appVersion` 是 Chart 里打包的应用（NIC）版本。分开是因为 Chart 可能不改应用、只改模板就发新版（version 升、appVersion 不升），也可能同时升级 NIC。

**练习 2**：`controller.kind` 分别取 `deployment` / `daemonset` / `statefulset` 时，最终集群里会多出几种工作负载？

**参考答案**：只多出**一种**。因为有三个独立模板文件 `controller-deployment.yaml` / `controller-daemonset.yaml` / `controller-statefulset.yaml`，每个开头都有 `{{- if eq .Values.controller.kind "..." }}` 守卫，三个值互斥，所以同一时间只渲染其中之一。

---

### 4.3 Ingress 示例（标准路由资源）

#### 4.3.1 概念说明

装好 NIC 后，要让它真正转发流量，还需要给它「路由规则」。最标准的方式是 Kubernetes 内置的 **Ingress** 资源（`networking.k8s.io/v1`）。本节用仓库自带的 Cafe 示例来讲。

承接 u1-l1：Ingress 是声明式路由规则，NIC 监听它、翻译成 NGINX 配置。本节我们看一份具体的 Ingress 长什么样。

#### 4.3.2 核心流程

Cafe Ingress 示例的完整运行链路（见 `examples/ingress-resources/complete-example/README.md`）：

```text
1. 先装好 NIC（上一节的安装清单 / Chart）
2. kubectl apply -f cafe.yaml          → 部署后端应用（coffee/tea 的 Deployment+Service）
3. kubectl apply -f cafe-secret.yaml   → 创建 TLS 证书 Secret（tls-secret）
4. kubectl apply -f cafe-ingress.yaml  → 创建 Ingress 路由规则
        │
        ▼
NIC 的 Informer 监测到 Ingress（ingressClassName=nginx）→
按 host/path 把 /tea 路由到 tea-svc、/coffee 路由到 coffee-svc →
reload NGINX → 对外可访问 https://cafe.example.com/{tea,coffee}
```

#### 4.3.3 源码精读

**Ingress 本体。** [examples/ingress-resources/complete-example/cafe-ingress.yaml:1-10](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/ingress-resources/complete-example/cafe-ingress.yaml#L1-L10)：

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: cafe-ingress
spec:
  ingressClassName: nginx
  tls:
  - hosts:
    - cafe.example.com
    secretName: tls-secret
```

两个要点：`ingressClassName: nginx` 让 NIC（默认 class 为 nginx）认领它；`tls` 段把 `cafe.example.com` 的证书指向名为 `tls-secret` 的 Secret。

**路由规则。** [examples/ingress-resources/complete-example/cafe-ingress.yaml:11-28](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/ingress-resources/complete-example/cafe-ingress.yaml#L11-L28) 定义了一个 host `cafe.example.com`，下面两条 path：`/tea`（Prefix）→ `tea-svc:80`，`/coffee`（Prefix）→ `coffee-svc:80`。NGINX 翻译后，相当于生成了两个 location 与两个 upstream。

**后端应用。** [examples/ingress-resources/complete-example/cafe.yaml:1-19](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/ingress-resources/complete-example/cafe.yaml#L1-L19) 是 coffee 的 Deployment+Service，[examples/ingress-resources/complete-example/cafe.yaml:34-52](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/ingress-resources/complete-example/cafe.yaml#L34-L52) 是 tea 的。它们用的是 `nginxdemos/nginx-hello:plain-text` 镜像，回显自己的 server address，方便验证流量切分。

> 注意：Ingress 引用的是 `tls-secret`，但仓库 `complete-example/` 目录里并没有 `cafe-secret.yaml`——它需要用 `make secrets`（见该目录 README 第 1 步）生成。这是真实仓库的现状，不要以为漏了文件。

#### 4.3.4 代码实践

**实践目标**：理解 Ingress 的 path → Service 映射。

**操作步骤**：

1. 打开 `cafe-ingress.yaml`，把每条 `path` 与其 `backend.service.name`、`port.number` 抄成一张表。
2. 打开 `cafe.yaml`，确认 `tea-svc` / `coffee-svc` 这两个 Service 的 `selector` 能选中对应的 Deployment，且 `targetPort` 对得上容器端口。

**需要观察的现象**：观察「Ingress path → Service name → Service selector → Deployment label → Pod」这条引用链是否闭合。

**预期结果**：你会看到 `/tea` 最终落到 `app: tea` 的 Pod 上（容器 8080），`/coffee` 落到 `app: coffee` 的 Pod 上。引用链完全自洽。

**待本地验证**：若你有集群，可按 `complete-example/README.md` 的 curl 命令实测 `https://cafe.example.com/tea` 与 `/coffee` 的回显差异。

#### 4.3.5 小练习与答案

**练习 1**：如果忘了写 `ingressClassName: nginx`，NIC 还会处理这个 Ingress 吗？

**参考答案**：取决于 NIC 版本与配置。较新的 NIC 只处理 `ingressClassName` 等于自身 class（默认 nginx）的 Ingress；漏写则不会被认领（除非配置了把无 class 的 Ingress 也纳入处理，或 setAsDefaultIngress）。所以写上 `ingressClassName: nginx` 是最稳妥的做法。

**练习 2**：`pathType: Prefix` 对 `/tea` 意味着什么？

**参考答案**：Prefix 表示前缀匹配，`/tea` 会匹配 `/tea`、`/tea/`、`/teaxxx` 等所有以 `/tea` 开头的路径。

---

### 4.4 VirtualServer 示例（自定义资源）

#### 4.4.1 概念说明

承接 u1-l1：除了标准 Ingress，NIC 还提供自定义资源 **VirtualServer**（`k8s.nginx.org/v1`），表达能力更强（流量切分、策略引用、更精细的匹配）。本节用同一个 Cafe 应用，对比 VirtualServer 的写法。

#### 4.4.2 核心流程

VirtualServer 示例运行链路：

```text
1. 装好 NIC 且 controller.enableCustomResources=true（Chart 默认就是 true）
2. CRD 已安装（charts/nginx-ingress/crds/k8s.nginx.org_virtualservers.yaml）
3. kubectl apply -f cafe.yaml                  → 后端应用
4. kubectl apply -f cafe-virtual-server.yaml   → VirtualServer
        │
        ▼
NIC 监测到 VirtualServer → 按 host/upstreams/routes 翻译成 NGINX 配置 → reload
```

与 Ingress 的关键区别：VirtualServer 把 **upstream** 显式声明为命名对象，再用 route 的 `action.pass` 按名字引用它；这比 Ingress 把 service 直接写在 path 里更解耦，也为后续的流量切分、策略引用留出了空间（u4-l5 会深入）。

#### 4.4.3 源码精读

**VirtualServer 本体。** [examples/custom-resources/basic-configuration/cafe-virtual-server.yaml:1-8](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/basic-configuration/cafe-virtual-server.yaml#L1-L8)：

```yaml
apiVersion: k8s.nginx.org/v1
kind: VirtualServer
metadata:
  name: cafe
spec:
  host: cafe.example.com
  tls:
    secret: tls-secret
```

注意三处与 Ingress 的差异：apiGroup 是 `k8s.nginx.org`（NIC 私有，不是 K8s 内置）；`host` 是顶层字段而非 `rules[].host`；TLS 直接写 `tls.secret`，不需要 `hosts` 列表。

**upstreams 与 routes。** [examples/custom-resources/basic-configuration/cafe-virtual-server.yaml:9-15](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/basic-configuration/cafe-virtual-server.yaml#L9-L15) 显式声明了两个 upstream：`tea`（→ `tea-svc:80`）、`coffee`（→ `coffee-svc:80`）。

[examples/custom-resources/basic-configuration/cafe-virtual-server.yaml:16-22](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/basic-configuration/cafe-virtual-server.yaml#L16-L22) 的 route 用 `action.pass: tea` / `coffee` 按名字引用 upstream。这种「命名 upstream + 名字引用」的结构，正是 VirtualServer 能支持流量切分（一个 route 指向多个 upstream + weight）的基础。

**后端应用。** [examples/custom-resources/basic-configuration/cafe.yaml:34-52](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/basic-configuration/cafe.yaml#L34-L52) 的 tea Deployment+Service，与 Ingress 版基本一致（只是副本数不同）。

#### 4.4.4 代码实践

**实践目标**：把 VirtualServer 与等价的 Ingress 并排对照，量化两者写法差异。

**操作步骤**：

1. 同时打开 `examples/ingress-resources/complete-example/cafe-ingress.yaml` 和 `examples/custom-resources/basic-configuration/cafe-virtual-server.yaml`。
2. 画一张对比表：host 字段位置、TLS 写法、后端引用方式（内联 service vs 命名 upstream）、class 声明方式（ingressClassName vs 无）。

**需要观察的现象**：观察 VirtualServer 把「后端」拆成了 upstream 对象，而 Ingress 把后端直接内联在 path 里。

**预期结果**：得到一张清晰的差异表，直观感受到 VirtualServer 更结构化、更适合复杂路由（后续 u2、u4 会展开）。

#### 4.4.5 小练习与答案

**练习 1**：VirtualServer 不写 `ingressClassName`，NIC 怎么知道要处理它？

**参考答案**：VirtualServer 是 NIC 私有 CRD，默认由部署它的 NIC 实例全部处理（在开启 `enableCustomResources` 的前提下），不像标准 Ingress 那样依赖 IngressClass 做认领。具体认领规则会在 u2、u3 详述。

**练习 2**：把 VirtualServer 的某条 `action.pass` 从 `tea` 改成 `coffee`，会发生什么？

**参考答案**：reload 后，`/tea` 这条路径的流量会被转发到 `coffee-svc`（即原本的 coffee 后端）。这体现了「route 按名字引用 upstream」的解耦优势——改引用不改后端定义。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这个对照任务（这是本讲的主实践任务）。

**任务**：用 `helm template` 把 Chart 渲染成 YAML，再与 `deployments/` 手写清单对照，填写下表，指出每类资源在「Chart」与「手写清单」两条路径下分别由哪个文件提供。

| 资源类型 | Chart 路径（templates/ 下） | 手写清单路径（deployments/ 下） |
| --- | --- | --- |
| Deployment（工作负载） | （你填写） | （你填写） |
| ConfigMap（全局配置） | （你填写） | （你填写） |
| RBAC（ClusterRole 等） | （你填写） | （你填写） |
| Service（LoadBalancer） | （你填写） | （你填写） |

**操作步骤**：

1. 执行渲染（本机有 helm 时）：

   ```bash
   helm template rel ./charts/nginx-ingress -n nginx-ingress > /tmp/rel.yaml
   ```

   本机没有 helm 时，改为直接阅读 `charts/nginx-ingress/templates/` 目录，按文件名推断每个模板渲染什么资源。

2. 在渲染结果里分别 `grep -n 'kind: Deployment'`、`'kind: ConfigMap'`、`'kind: ClusterRole'`、`'kind: Service'`，定位它们各自来自哪个模板文件（渲染输出会带 `# Source:` 注释，指明来源）。
3. 在 `deployments/` 下找到对应的手写文件。
4. 完成上表。

**参考答案（供自检）**：

| 资源类型 | Chart 路径 | 手写清单路径 |
| --- | --- | --- |
| Deployment | `templates/controller-deployment.yaml` | `deployments/deployment/nginx-ingress.yaml` |
| ConfigMap | `templates/controller-configmap.yaml` | `deployments/common/nginx-config.yaml` |
| RBAC | `templates/clusterrole.yaml` + `clusterrolebinding.yaml` | `deployments/rbac/rbac.yaml` |
| Service | `templates/controller-service.yaml` | `deployments/service/loadbalancer.yaml` |

**需要观察的现象**：你会清楚地看到——**Chart 和手写清单产出的是同一组资源，只是 Chart 多了一层参数化与命名模板**。Deployment/ConfigMap/RBAC/Service 这四类资源在两条路径下都能一一对应上。

**预期结果**：你建立起「安装 NIC 所需的最小资源集」与「两种安装方式（清单 vs Chart）的对应关系」的心智模型。这会直接帮助你在后续单元（尤其是 u3 控制器、u4 配置生成）里，把抽象的源码逻辑与具体的集群资源对上号。

> 本实践为「源码/清单阅读型」，不需要真实集群即可完成对照；若涉及 helm 命令的输出细节，标注为「待本地验证」。

## 6. 本讲小结

- `deployments/` 存放手写、可直接 `kubectl apply` 的原始清单，按部署拓扑和关注点分子目录；一份完整安装至少需要 Namespace/SA、RBAC、IngressClass、ConfigMap、工作负载、Service 六类资源，文件之间通过名字（label / serviceAccountName / configmap 名）互相引用。
- Deployment 清单的 `args` 段（`-nginx-configmaps`、`-report-ingress-status` 等）是承接 u1-l4 核心 flag 的落点；`nginx-config.yaml` 的 `data:` 默认为空，ConfigMap 是「按需覆盖全局参数」而非必填。
- `charts/nginx-ingress` 是参数化包装：`Chart.yaml` 是身份证（区分 chart version 与 appVersion），`values.yaml` 是参数全集，`templates/*.yaml` 用 `{{- if }}` 守卫按 values 渲染资源，`_helpers.tpl` 提供统一命名。
- 同一个开关在两条路径下都能找到对应：手写清单的 `args`/字段 ↔ Chart 的 value（如 `-enable-leader-election` ↔ `controller.reportIngressStatus.enableLeaderElection`，`nginx-config.yaml` ↔ `controller.config.entries`）。
- 标准 Ingress（`networking.k8s.io/v1`）把后端 service 内联在 path 里；自定义资源 VirtualServer（`k8s.nginx.org/v1`）把 upstream 显式声明为命名对象、route 用 `action.pass` 引用，更解耦、更适合复杂路由。
- 两种安装方式产出同一组资源；Cafe 示例（cafe.yaml + 路由资源）演示了 Ingress 与 VirtualServer 两种用法的等价路由。

## 7. 下一步学习建议

本讲把「安装」讲完了。接下来建议：

- **进入第 2 单元（资源模型）**：本讲你已见过 Ingress 与 VirtualServer 两种 YAML，u2-l1 会系统对比四类路由资源（Ingress/VirtualServer/VirtualServerRoute/TransportServer）的能力差异，u2-l2 会带你打开 `pkg/apis/configuration/v1/types.go` 看 VirtualServer 的 Go 结构体——把「用户 YAML」和「源码类型」对上。
- **想要更懂安装产物**：可先读 `charts/nginx-ingress/values.schema.json`，看 values 的类型约束是如何定义的；也可读 `charts/nginx-ingress/crds/` 下任意一份 CRD YAML，预热第 2 单元。
- **延伸阅读**：NIC 官方安装文档 <https://docs.nginx.com/nginx-ingress-controller/install/>（manifests 与 helm 两种方式的权威说明）。
