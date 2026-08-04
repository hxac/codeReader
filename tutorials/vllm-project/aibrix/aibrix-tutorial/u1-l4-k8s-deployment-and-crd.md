# Kubernetes 部署与 CRD 安装流程

## 1. 本讲目标

学完本讲后，你应该能够：

1. 看懂 `config/` 目录下的 kustomize 分层，说清楚 `config/dependency`、`config/crd`、`config/default` 各自负责什么。
2. 复述 README Quick Start 中 **dependency → crd → default** 三步安装命令的执行顺序与作用，并解释为什么顺序不能乱。
3. 说明 AIBrix 为什么**故意把 CRD 与 operator 拆成两份独立清单**安装（这是本讲最关键的一个设计决策）。
4. 区分 **nightly（本地源码）** 与 **stable（发布预构建清单）** 两种安装方式。
5. 读懂 `samples/quickstart/model.yaml`，理解一个真实推理工作负载是如何被 AIBrix 发现的。
6. 说明 AIBrix 元数据存储层默认用内置 Redis，以及如何用 `config/metadata/valkey.yaml` 把它换成开源的 Valkey（并能解释为什么这种替换「不用改一行连接配置」）。

## 2. 前置知识

本讲假设你已经读过 [u1-l1 项目概览](u1-l1-project-overview.md)（知道 AIBrix 有控制平面、网关、运行时、KV Cache 四大子系统）和 [u1-l2 仓库结构](u1-l2-repository-structure.md)。下面补充几个本讲要用到的概念，初次接触 Kubernetes 的读者请先看这一节。

- **CRD（CustomResourceDefinition）**：让你在 Kubernetes 里定义一种新的资源类型。AIBrix 定义了 `PodAutoscaler`、`ModelAdapter`、`ModelClaim`、`StormService` 等多种 CRD。可以把它理解成「先在数据库里建表，再往表里写数据」——CRD 是建表语句，CR（Custom Resource，自定义资源实例）是表里的行。
- **operator / 控制器**：一个长期运行的程序，它盯着某些 CR 的变化，努力让集群的实际状态向 CR 声明的期望状态靠拢。AIBrix 的 operator 就是 `config/manager` 部署的那个 `controller-manager` Pod。
- **kustomize**：Kubernetes 官方的「配置拼接」工具。它的核心是一个个目录，每个目录里放一个 `kustomization.yaml`，声明「我要包含哪些 YAML、加什么前缀、放到哪个 namespace」。一个目录可以 `resources:` 引用另一个目录，层层叠加（overlay）。命令 `kubectl apply -k <目录>` 就是「按这个目录的 kustomization 渲染出最终 YAML，再提交给集群」。
- **`--server-side`（Server-Side Apply）**：让 Kubernetes 服务端而不是 kubectl 客户端来记录「哪些字段是谁写的」。对于 CRD 这种体积很大的对象，客户端 apply 会在对象上塞一个超大的 `last-applied-configuration` 注解，容易撑爆单对象 256KiB 的大小上限；server-side 把这件事移到服务端，从而避免该问题，也能更好地处理多方共同维护同一个对象的情况。这就是为什么 AIBrix 安装 CRD/依赖时统一带 `--server-side`。
- **级联删除（cascade delete）**：在 Kubernetes 里，**删除一个 CRD 会连带删除该类型下所有已存在的 CR 实例**。这条规则是本讲第 3 个模块要解释「CRD 为何单独安装」的根本原因。

## 3. 本讲源码地图

本讲主要围绕 `config/` 这个 kustomize 大包，以及一个示例工作负载：

| 路径 | 作用 |
| --- | --- |
| `config/dependency/kustomization.yaml` | 安装 AIBrix 依赖的外部组件（Envoy Gateway、KubeRay 的 CRD） |
| `config/dependency/envoy-gateway/kustomization.yaml` | 拉取 Envoy Gateway 官方安装清单并打补丁 |
| `config/crd/kustomization.yaml` | 聚合 AIBrix 自己的三大类 CRD（autoscaling / model / orchestration） |
| `config/crd/{autoscaling,model,orchestration}/` | 真正的 CRD 定义 YAML（由 controller-gen 生成，见 u1-l3） |
| `config/default/kustomization.yaml` | 顶层「全部组件接线完成」的清单：namespace、RBAC、manager、gateway、webhook 等 |
| `config/metadata/kustomization.yaml` | 元数据服务 + 元数据存储（内置 Redis，可切换 Valkey）的装配入口 |
| `config/metadata/metadata.yaml` | metadata-service 的 Deployment/Service，通过 `REDIS_HOST` 连接存储 |
| `config/metadata/redis.yaml` / `valkey.yaml` | 元数据存储后端：默认内置 Redis，可选开源 Valkey（BSD-3，drop-in 替换） |
| `config/manager/manager.yaml` | operator 的 Deployment 与 metrics Service |
| `config/overlays/release/kustomization.yaml` | 把 nightly 配置改写成 stable 发布版的 overlay |
| `samples/quickstart/model.yaml` | 一个用 vLLM 跑 DeepSeek-R1-Distill 的 Deployment 示例 |

## 4. 核心概念与源码讲解

### 4.1 kustomize 目录分层与三层定位

#### 4.1.1 概念说明

AIBrix 的 `config/` 不是一个扁平的 YAML 列表，而是一棵 kustomize 目录树。理解这棵树的关键是分清**三层**，它们正好对应 README 里的三步安装命令：

| 层 | 目录 | 装的是什么 | 谁的 |
| --- | --- | --- | --- |
| 依赖层 | `config/dependency` | Envoy Gateway、KubeRay 的 CRD/资源 | **别人的** |
| 类型层 | `config/crd` | PodAutoscaler、ModelAdapter、StormService 等 CRD | AIBrix 自己的「建表语句」 |
| 组件层 | `config/default` | operator、网关、metadata、RBAC、webhook 等真正运行的 Pod | AIBrix 自己的「程序」 |

一句话记忆：**先装别人给的零件（dependency），再建我们自己的表（crd），最后启动用这些表的程序（default）。**

#### 4.1.2 核心流程

每一层都是「带 `kustomization.yaml` 的目录」，渲染逻辑如下：

```
config/default/kustomization.yaml
   ├── resources: ../namespace        （创建 aibrix-system 命名空间）
   ├── resources: ../rbac             （ServiceAccount/Role/Binding）
   ├── resources: ../manager          （operator Deployment）
   ├── resources: ../gateway          （网关插件）
   ├── resources: ../metadata         （元数据服务）
   ├── resources: ../gpu-optimizer
   ├── resources: ../dependency/kuberay-operator  （把 KubeRay 也并进来）
   ├── resources: ../webhook
   ├── resources: ../internalcert
   ├── namespace: aibrix-system       （统一打命名空间）
   ├── namePrefix: aibrix-            （统一改名加前缀）
   └── images: ...                    （统一替换镜像 tag）
```

`config/default` 是「总装车间」，它本身不写资源，而是把其它子目录拉进来拼成完整清单，并做两件重要的全局变换：

1. `namespace: aibrix-system` —— 所有资源都丢进 `aibrix-system` 命名空间。
2. `namePrefix: aibrix-` —— 所有资源名字前面加 `aibrix-`，所以 `manager.yaml` 里写的 `controller-manager`，最终在集群里叫 `aibrix-controller-manager`。

#### 4.1.3 源码精读

顶层清单的全局变换与资源清单：

[config/default/kustomization.yaml:4-12](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/default/kustomization.yaml#L4-L12) —— 这里定义了 `namespace: aibrix-system` 和 `namePrefix: aibrix-`，是整个部署「统一换 namespace、统一加前缀」的源头。

[config/default/kustomization.yaml:25-38](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/default/kustomization.yaml#L25-L38) —— `config/default` 拉进来的全部子目录（namespace、rbac、manager、gateway、metadata、gpu-optimizer、kuberay-operator、webhook、internalcert）。注意它**没有**引用 `../crd`，原因见 4.2。

[config/crd/kustomization.yaml:4-8](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/crd/kustomization.yaml#L4-L8) —— CRD 层只聚合三个子目录 `autoscaling`、`model`、`orchestration`，对应自动伸缩、模型适配、分布式编排三类自定义资源。

[config/dependency/kustomization.yaml:3-7](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/dependency/kustomization.yaml#L3-L7) —— 依赖层装的是 Envoy Gateway 和 KubeRay 的三类 CRD（rayclusters / rayjobs / rayservices）。Envoy Gateway 的清单是直接从一个远程 GitHub release URL 拉下来的：

[config/dependency/envoy-gateway/kustomization.yaml:3-4](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/dependency/envoy-gateway/kustomization.yaml#L3-L4) —— `resources:` 里直接写 `https://github.com/envoyproxy/gateway/releases/download/v1.2.8/install.yaml`，kustomize 会联网把它拉进来再做 patch，这是「引用别人现成清单」的典型写法。

operator 本身的 Deployment 定义在：

[config/manager/manager.yaml:25-35](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/manager/manager.yaml#L25-L35) —— operator 容器跑 `/manager` 二进制，启动参数含 `--leader-elect`、`--health-probe-bind-address=:8081`、`--enable-runtime-sidecar` 等。注意 `namespace: system` 是占位符，会被 `config/default` 的 `namespace` 字段替换成真正的 `aibrix-system`。

#### 4.1.4 代码实践

**实践目标**：亲手看到 kustomize 是如何「拼接 + 改名 + 换 namespace」的，而不依赖集群。

**操作步骤**：

1. 在仓库根目录执行（不需要集群，只渲染不提交）：

   ```shell
   kubectl kustomize config/default
   ```

2. 在输出里搜索 controller-manager 的 Deployment，观察它的 `metadata.name` 和 `metadata.namespace`。

**需要观察的现象**：

- 原本 `manager.yaml` 里写的是 `name: controller-manager`、`namespace: system`。
- 渲染后变成 `name: aibrix-controller-manager`、`namespace: aibrix-system`。

**预期结果**：你会清楚看到 `namePrefix` 和 `namespace` 两项变换的效果，从而理解「源码里的占位名」与「集群里的真实名」之间的映射关系。如果你本地没装 kubectl，也可以用 `kustomize build config/default`（kustomize 独立二进制）。若环境不允许运行，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果我想把整个 AIBrix 装到 `myorg-aibrix` 命名空间而不是 `aibrix-system`，至少要改 `config/default/kustomization.yaml` 里的哪两个字段？改完会有什么隐患？

**参考答案**：要改 `namespace` 和 `namePrefix`。隐患是：AIBrix 很多组件（网关 EnvoyProxy、webhook service 名等）以及示例清单默认假设 `aibrix-system`，改了之后可能出现命名/引用对不上的问题，需要配套修改对应 patch。这正是社区更推荐用 overlay（见 4.3）而不是直接改 `config/default` 的原因。

**练习 2**：`config/default` 的 resources 里既有 `../dependency/kuberay-operator`，又有独立的 `config/dependency` 安装步骤，二者重复吗？

**参考答案**：不完全重复。`config/dependency/kuberay-operator` 子目录被并进 `config/default`（连同 operator 的 Deployment、RBAC 等），而 README 第一步 `kubectl apply -k config/dependency` 还会把 Envoy Gateway 与 KubeRay 的 CRD 先装上。把 CRD 单独提前装是为了在 operator 启动前依赖的类型就已就绪。

---

### 4.2 CRD 与组件的安装顺序

#### 4.2.1 概念说明

README 的 Quick Start 是固定的三步，顺序很重要：

```shell
kubectl apply -k config/dependency --server-side   # ① 依赖
kubectl apply -k config/crd --server-side          # ② CRD
kubectl apply -k config/default                     # ③ 组件
```

为什么是这个顺序？因为存在两条「必须先有」的依赖：

- **operator 启动前，它要 watch 的 CRD 必须已存在**。operator 的 informer 需要监听 PodAutoscaler、ModelAdapter 等类型；类型（CRD）不存在，监听就建立不起来。所以 ② 必须早于 ③。
- **AIBrix 的部分控制器依赖外部 CRD**。例如分布式推理控制器依赖 KubeRay 的 `rayclusters.ray.io` CRD（见 [u5-l1](u5-l1-distributed-inference-kuberay.md)）。所以 ① 必须早于 ②/③。

但本模块真正的重点是另一个、也是更显眼的设计：**AIBrix 故意不让 CRD 跟着 operator 一起安装/卸载。** 这不是随手为之，而是源码里有专门注释、并指向了具体 issue 的明确决策。

#### 4.2.2 核心流程

记住这条 Kubernetes 垃圾回收规则：

> 删除一个 CRD，会级联删除该类型下**所有** CR 实例。

于是问题来了：如果 CRD 写在 operator 的清单里（也就是放进 `config/default`），那么当你执行 `kubectl delete -k config/default` 卸载 AIBrix operator 时，CRD 会被删掉，进而把你集群里所有用户创建的 `StormService`、`RoleSet`、`PodAutoscaler`、`ModelAdapter` 实例**全部清空**——这通常不是用户想要的「我只是想升级/卸载 operator，结果我的模型部署配置全没了」。

AIBrix 的解法是把 CRD 放进**单独的** `config/crd` 清单（对应发布物 `aibrix-core-crds-*.yaml`），与 operator 清单 `config/default`（对应 `aibrix-core-*.yaml`）彻底分开。这样：

```
kubectl delete -k config/default   # 只删 operator/网关/RBAC……
                                   # CRD 仍在 → 用户的 CR 实例安然无恙 ✅
kubectl delete -k config/crd       # 这一步才会真正删表（需用户显式执行）
```

用伪代码描述安装/卸载的对称性：

```
安装： dependency  →  crd  →  default      （先建表后启动程序）
卸载： default     →  crd  →  dependency   （先停程序，表与数据可保留）
```

#### 4.2.3 源码精读

这段注释是整个决策的「白纸黑字」，非常重要：

[config/default/kustomization.yaml:20-24](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/default/kustomization.yaml#L20-L24) —— 注释明确写道：「CRDs are intentionally NOT included here. They are shipped as a separate manifest (aibrix-core-crds-${tag}.yaml) so that uninstalling the AIBrix operator does not cascade-delete user CRs (StormService, RoleSet, PodAutoscaler, ModelAdapter, etc.). See vllm-project/aibrix#2062.」并给出单独安装命令 `kubectl apply -k config/crd --server-side`。

CRD 的三个子目录里放的就是真实的 CRD 定义，例如 orchestration 下有六种：

[config/crd/orchestration/kustomization.yaml:1-7](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/crd/orchestration/kustomization.yaml#L1-L7) —— 这里能看到 `rayclusterreplicasets`、`rayclusterfleets`、`kvcaches`、`stormservices`、`rolesets`、`podsets` 六个 CRD 文件，它们正是上面注释里担心被级联删掉的用户 CR 类型。这些 YAML 是由 controller-gen 生成的，生成与同步流程见 [u1-l3 构建系统](u1-l3-build-and-makefile.md)（相关 Makefile 目标 `manifests` / `sync-crds`）。

`config/crd` 顶层的说明也提示了它的定位：

[config/crd/kustomization.yaml:1-3](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/crd/kustomization.yaml#L1-L3) —— 注释说「This kustomization.yaml is not intended to be run by itself ... It should be run by config/default」，但同时它又被当作**独立安装步骤**使用。这种「既可被引用、也可独立 apply」的双用途正是 kustomize 分层带来的灵活性。

#### 4.2.4 代码实践

**实践目标**：验证「CRD 不在 `config/default` 的渲染结果里」。

**操作步骤**：

1. 渲染组件清单并统计其中是否含 AIBrix 自己的 CRD：

   ```shell
   kubectl kustomize config/default | grep -E "^kind: CustomResourceDefinition" | sort | uniq -c
   ```

2. 再单独渲染 CRD 清单对照：

   ```shell
   kubectl kustomize config/crd | grep -E "^kind: CustomResourceDefinition" | sort | uniq -c
   ```

**需要观察的现象**：

- 第 1 条命令（default）里，应该**看不到** `podautoscalers`、`modeladapters`、`stormservices` 等 AIBrix CRD（顶多出现 KubeRay/Envoy 的依赖 CRD，因为 `dependency/kuberay-operator` 被并入了 default）。
- 第 2 条命令（crd）里，能看到 AIBrix 自己的全部 CRD。

**预期结果**：两组输出互斥（AIBrix 自有 CRD 只出现在 `config/crd`，不出现在 `config/default`），从渲染层面印证了源码注释里的设计。若无法运行命令，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么安装 CRD 命令都带 `--server-side`，而安装 `config/default` 不带？

**参考答案**：CRD 对象体积大，客户端 apply 会写入巨大的 `kubectl.kubernetes.io/last-applied-configuration` 注解，可能撞上单对象 256KiB 上限；server-side apply 把字段所有权记录在服务端，规避了这个问题。`config/default` 里多是 Deployment/Service 等常规对象，体积小，不强求 server-side（当然加上也无害）。

**练习 2**：如果用户误把 CRD 也放进 `config/default` 然后执行 `kubectl delete -k config/default`，会丢失什么？

**参考答案**：会删除 AIBrix 自有 CRD，进而级联删除集群中所有 `PodAutoscaler`、`ModelAdapter`、`StormService`、`RoleSet` 等 CR 实例——即用户的伸缩/适配/拓扑配置全部丢失。这正是 issue #2062 要避免的情况。

---

### 4.3 stable 与 nightly 两种安装方式

#### 4.3.1 概念说明

AIBrix 提供两种安装路径，面向不同人群：

| 方式 | 命令来源 | 镜像 tag | 适用场景 |
| --- | --- | --- | --- |
| **nightly** | 本地 clone 的 `config/` 目录（`kubectl apply -k config/...`） | `nightly` | 开发者、追新、改源码后自测 |
| **stable** | GitHub Releases 上的预构建 YAML（`kubectl apply -f https://...`） | 固定版本如 `v0.7.0` | 生产、可复现、只想要稳定版 |

两者**用的同一套 kustomize 源**，区别只在于镜像 tag 和是否经过 overlay 改写。stable 的 YAML 本质上是「把 `config/default` 走一遍 release overlay 渲染好、再发布到 GitHub」的产物。

#### 4.3.2 核心流程

```
config/default（nightly：镜像 tag=nightly）
        │
        ▼  叠加 release overlay
config/overlays/release
   ├── resources: ../../default + pdb.yaml
   ├── patches:  envoy_proxy_patch.yaml / gateway_plugins_patch.yaml
   └── images:   把各镜像 tag 改成 v0.7.0
        │
        ▼  渲染 + 发布
aibrix-core-v0.7.0.yaml          （对应 nightly 的 config/default）
aibrix-core-crds-v0.7.0.yaml     （对应 nightly 的 config/crd）
aibrix-dependency-v0.7.0.yaml    （对应 nightly 的 config/dependency）
```

注意 stable 把 nightly 的三个 `-k` 步骤，等价拆成了三个从 GitHub 下载的 `-f` 步骤，**顺序和「CRD 单独安装」的设计完全一致**。

#### 4.3.3 源码精读

release overlay 把 nightly 改写成 stable 的关键：

[config/overlays/release/kustomization.yaml:6-8](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/overlays/release/kustomization.yaml#L6-L8) —— release overlay 直接 `resources: ../../default`，即在 nightly 总装清单之上再叠加，并额外加入 `pdb.yaml`（PodDisruptionBudget，给生产用）。

[config/overlays/release/kustomization.yaml:14-29](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/overlays/release/kustomization.yaml#L14-L29) —— `images:` 段把 `aibrix/controller-manager`、`aibrix/gateway-plugins`、`aibrix/metadata-service`、`aibrix/runtime` 的 tag 全部从 `nightly` 覆盖成 `v0.7.0`（同时 KubeRay operator 也换成 `aibrix/kuberay-operator` 的固定 patch 版本）。这就是「同一份配置、不同版本」的实现。

对照 nightly 自身的镜像设置：

[config/default/kustomization.yaml:69-87](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/default/kustomization.yaml#L69-L87) —— nightly 里所有 AIBrix 镜像的 `newTag` 都是 `nightly`。release overlay 正是覆盖这里的 tag 来产出 stable。

#### 4.3.4 代码实践

**实践目标**：直观对比 nightly 与 stable 渲染结果的镜像差异。

**操作步骤**：

1. 渲染 nightly 并提取镜像：

   ```shell
   kubectl kustomize config/default | grep "image:" | sort -u
   ```

2. 渲染 stable 并提取镜像：

   ```shell
   kubectl kustomize config/overlays/release | grep "image:" | sort -u
   ```

**需要观察的现象**：第 1 组里 AIBrix 镜像是 `:nightly`，第 2 组里变成 `:v0.7.0`，KubeRay operator 也从社区镜像换成 `aibrix/kuberay-operator:...`。

**预期结果**：两组镜像 tag 不同，证明 stable = nightly + release overlay 的 tag 覆盖。若不能运行，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：我想在内部环境用一个自建镜像仓库（例如 `harbor.myorg.com/aibrix/controller-manager`），该改哪里？

**参考答案**：最干净的做法是写一个自己的 overlay（仿照 `config/overlays/release`），用 `images:` 段把 `newName` 指向自建仓库、`newTag` 指向你要的版本，然后 `kubectl apply -k` 你的 overlay 目录。这样不动 `config/default` 源码。

**练习 2**：stable 的三个发布物文件名里为什么也要把 `crds` 单独成一个文件？

**参考答案**：与 nightly 同理——保持「CRD 与 operator 分离」的设计，让 `aibrix-core-v0.7.0.yaml`（operator）可以独立升级/卸载而不会级联删掉用户的 CR 实例。

---

### 4.4 quickstart 示例 CR：从 CRD 到真实模型部署

#### 4.4.1 概念说明

装完 operator，下一步自然是「部署一个真模型试试」。`samples/quickstart/model.yaml` 就是这个入门示例。这里有一个容易让初学者困惑的点：**这个示例并不是一个 AIBrix 的 CR（不是 PodAutoscaler/ModelAdapter），而是一个普通的 Kubernetes `Deployment`**。

那 AIBrix 是怎么「认出」它的？答案在一个约定俗成的标签：只要你的工作负载打了 `model.aibrix.ai/name` 标签（且值与 Service 名一致），AIBrix 的发现机制（详见 [u6-l2 Pod 发现](u6-l2-discovery-and-profiling.md)）就能把它纳入管理，网关也才能把请求路由过去。

所以这个 quickstart 演示的链路是：

```
安装 CRD/operator → 部署一个打了 model.aibrix.ai/name 标签的 vLLM Deployment
                 → AIBrix 发现它 → 网关可路由 → 后续可挂 PodAutoscaler 做伸缩
```

#### 4.4.2 核心流程

`model.yaml` 做了这几件事：

1. 声明一个 `Deployment`，跑 `vllm/vllm-openai:v0.11.0` 镜像，用 `vllm serve` 启动 `deepseek-ai/DeepSeek-R1-Distill-Llama-8B` 模型。
2. 给 Deployment 与 Pod 模板都打上 `model.aibrix.ai/name` 与 `model.aibrix.ai/port` 标签。
3. 申请 1 张 GPU，配置 `/health` 的存活/就绪/启动探针。

其中最关键的一致性约束是：**`model.aibrix.ai/name` 的值、`--served-model-name` 的值、以及（配套的）Service 名，三者必须相同**，否则网关与发现机制对不上号。

#### 4.4.3 源码精读

发现标签与命名一致性提示：

[samples/quickstart/model.yaml:4-7](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/samples/quickstart/model.yaml#L4-L7) —— 注释直接点明：标签值 `model.aibrix.ai/name` 必须与 Service 名一致；端口标签 `model.aibrix.ai/port` 标成 `8000`。

vLLM 启动参数中的「服务名」约定：

[samples/quickstart/model.yaml:31-35](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/samples/quickstart/model.yaml#L31-L35) —— `--served-model-name` 被设为 `deepseek-r1-distill-llama-8b`，注释再次强调它必须与 Service 名、Deployment 标签三者匹配。

GPU 资源申请：

[samples/quickstart/model.yaml:44-48](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/samples/quickstart/model.yaml#L44-L48) —— 通过 `nvidia.com/gpu: "1"` 申请 1 张 GPU（requests 与 limits 都设）。这意味着要跑通本示例，节点上需要有 GPU 与对应 device plugin。

镜像与模型：

[samples/quickstart/model.yaml:38-40](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/samples/quickstart/model.yaml#L38-L40) —— 使用 `vllm/vllm-openai:v0.11.0`，容器名 `vllm-openai`，启动命令是 `vllm serve ... --model deepseek-ai/DeepSeek-R1-Distill-Llama-8B`。

#### 4.4.4 代码实践

**实践目标**：理解「标签一致性」对 AIBrix 发现工作负载的意义（无需 GPU 也能做阅读型验证）。

**操作步骤**：

1. 打开 `samples/quickstart/model.yaml`，找到三处 `deepseek-r1-distill-llama-8b`：Deployment 的 `model.aibrix.ai/name` 标签值、`--served-model-name` 参数值、Deployment 的 `metadata.name`。
2. 假设你把第一处的标签值改成 `my-model`，但其余两处不变，预测会发生什么。
3. （可选，需集群+GPU）部署并观察：

   ```shell
   kubectl apply -f samples/quickstart/model.yaml
   kubectl get pods -l model.aibrix.ai/name
   ```

**需要观察的现象**：

- 第 2 步的推理：AIBrix 会按 `model.aibrix.ai/name` 去发现工作负载并建立「模型 → Pod」映射；若该标签值与 `--served-model-name`/Service 名不一致，网关在转发 `/v1/chat/completions`（model 字段为 `deepseek-r1-distill-llama-8b`）时将找不到后端，请求 404 或路由失败。
- 第 3 步：Pod 起来后 `kubectl get pods -l model.aibrix.ai/name` 能列出该推理 Pod。

**预期结果**：你应能清晰说出「三个名字必须一致」背后的原因——它是 AIBrix 控制平面/网关与底层推理工作负载之间的「握手约定」。若第 3 步无 GPU 环境无法实跑，明确标注「待本地验证（需 GPU 节点）」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 quickstart 用一个普通 `Deployment` 而不是某个 AIBrix CR 来跑模型？

**参考答案**：AIBrix 的定位是「叠加在现有推理工作负载之上」的基础设施。它不强求你用某种特定 CR 部署模型，而是通过 `model.aibrix.ai/name` 标签这个轻量约定去发现任意工作负载（Deployment、RayCluster 等）。AIBrix 自己的 CR（如 PodAutoscaler）是「附加能力」，可后续挂上去。

**练习 2**：在这个 quickstart 基础上，要让 AIBrix 自动伸缩这个模型，下一步应该创建什么？

**参考答案**：创建一个 `PodAutoscaler` CR，其目标指向同一个模型（标签/Service 名一致），并配置 `MetricsSources`。这会在 [u3-l1 PodAutoscaler 总览](u3-l1-podautoscaler-overview.md) 详细讲解。

---

### 4.5 元数据存储层：内置 Redis 与可选的 Valkey

#### 4.5.1 概念说明

讲到这里你可能注意到 `config/default` 的 resources 里有一项 `../metadata`。它装的是两样东西：AIBrix 的「元数据服务（metadata-service）」以及该服务依赖的一个内存数据库。这个数据库是 AIBrix 数据平面里的公共依赖——网关多副本之间用它同步路由状态、做分布式限流，metadata-service 用它存模型与适配器元数据（详见 [u8-l4 Redis 状态同步](u8-l4-redis-statesync-configprofile.md) 与 [u9-l3 指标与元数据](u9-l3-metrics-and-metadata.md)）。

默认情况下，AIBrix 用 `config/metadata/redis.yaml` 在集群内置部署一个 Redis。但从许可证角度看，Redis 自 7.4 起改为双源许可（RSALv2 / SSPLv1），对一些只接受 OSI 认可开源许可证的企业不够友好。**Valkey** 是 Redis 的 BSD-3 开源分支，与 Redis「线路兼容（wire-compatible）」——说同样的 RESP 协议、用同样的 go-redis 客户端、读同样的环境变量，因此可以**直接替换（drop-in）**。本次更新（PR #2434）正是为 AIBrix 增加了用 Valkey 替代内置 Redis 的选项（同时还在 Go 侧 KVCache 控制器引入了外部托管 Valkey/Redis 端点，那部分见 [u5-l3 KVCache 控制器](u5-l3-kvcache-controller-backends.md)，本讲只看 K8s 元数据存储这一层）。

本模块要回答两个问题：默认的 Redis 是怎么被部署、被其它组件发现的？换成 Valkey 只改一处就够，背后靠的是什么约定？

#### 4.5.2 核心流程

关键在于一个贯穿全栈的「服务名约定」。整个机制可以拆成四步：

1. `config/metadata/redis.yaml` 部署一个 Deployment，外加一个名为 `redis-master`、监听 6379 的 Service。
2. `config/default` 的 `namePrefix: aibrix-` 会给所有资源名加前缀，于是集群里真实的 Service 名变成 `aibrix-redis-master`。
3. metadata-service 在 `metadata.yaml` 里通过环境变量 `REDIS_HOST=aibrix-redis-master`、`REDIS_PORT=6379` 去连接它。
4. 换 Valkey 时，Valkey 的 Deployment 改用 `valkey/valkey:8-alpine` 镜像，但 **Service 名仍叫 `redis-master`**（只是 label 从 `app=redis` 换成 `app=valkey`）。名字没变 → 前缀加工后仍是 `aibrix-redis-master` → metadata-service 的 `REDIS_HOST` 一行都不用改。这就是「drop-in 替换」的全部秘密。

切换操作只需改 `config/metadata/kustomization.yaml` 的 resources：

```yaml
resources:
- metadata.yaml
# - redis.yaml          # 注释掉默认的内置 Redis
- valkey.yaml            # 改用 Valkey
```

⚠️ 一条硬约束：**不要同时 apply redis.yaml 和 valkey.yaml**——两者都声明同名 Service `redis-master`，会冲突。Valkey 是「替代」而非「并存」，resources 里只能二选一。

用伪代码总结：

```
默认:     resources = [metadata.yaml, redis.yaml]    → 集群跑 Redis
切 Valkey: resources = [metadata.yaml, valkey.yaml]  → 集群跑 Valkey（同名 Service）
两种情况下: metadata-service 的 REDIS_HOST 都指向 aibrix-redis-master，无需改动
```

#### 4.5.3 源码精读

config/metadata 的 kustomization 默认只装 redis.yaml，并用注释给出切 Valkey 的方法：

[config/metadata/kustomization.yaml:1-5](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/metadata/kustomization.yaml#L1-L5) —— `resources` 默认列 `metadata.yaml` + `redis.yaml`；注释行 `# - valkey.yaml` 提示：要用 Valkey（BSD-3 开源替代）就把 redis.yaml 换成 valkey.yaml。

默认的内置 Redis 是一个最小 Deployment + Service：

[config/metadata/redis.yaml:23-31](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/metadata/redis.yaml#L23-L31) —— Redis 容器用官方 `image: redis`，暴露 6379 端口。

[config/metadata/redis.yaml:33-49](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/metadata/redis.yaml#L33-L49) —— Service 名定为 `redis-master`，selector 匹配 `app=redis`。记住这个名字——它是后面 drop-in 约定的关键。

metadata-service 通过环境变量连这个 Service，且有一个 initContainer 等它就绪：

[config/metadata/metadata.yaml:67-70](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/metadata/metadata.yaml#L67-L70) —— initContainer `init-redis` 用 busybox 反复 `nc aibrix-redis-master 6379` 探测，收到 PONG 才放行。这里的地址 `aibrix-redis-master` 正是「namePrefix `aibrix-` + 源文件里的 `redis-master`」加工后的真实名字，印证了前缀机制如何串起两个目录。

[config/metadata/metadata.yaml:98-102](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/metadata/metadata.yaml#L98-L102) —— 环境变量 `REDIS_HOST=aibrix-redis-master`、`REDIS_PORT=6379`。Valkey 与 Redis 共用这组变量名（valkey.yaml 头部注释也强调了 same env vars），所以连接方完全不感知后端换没换。

Valkey 版本最关键的设计——Service 名保持不变：

[config/metadata/valkey.yaml:1-16](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/metadata/valkey.yaml#L1-L16) —— 头部注释明确：Valkey 是 Redis 的 BSD-3 开源替代，wire-compatible（RESP 协议、同样的 `REDIS_HOST`/`REDIS_PORT`/`REDIS_PASSWORD`、同样的 go-redis 客户端），并分别给出 kustomize 与 standalone 两种用法，以及「不要与 redis.yaml 同时使用」的告警。

[config/metadata/valkey.yaml:38-46](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/metadata/valkey.yaml#L38-L46) —— Valkey 容器用 `image: valkey/valkey:8-alpine`，同样暴露 6379。

[config/metadata/valkey.yaml:48-64](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/metadata/valkey.yaml#L48-L64) —— **Service 名仍叫 `redis-master`**（只是 label 换成 `app=valkey`）。正因为名字没变，经 `aibrix-` 前缀加工后仍是 `aibrix-redis-master`，metadata-service 一行配置都不用改——这就是 drop-in 替换能在源码层面成立的依据。

#### 4.5.4 代码实践

**实践目标**：验证「Redis → Valkey 切换后 Service 名不变，因此 metadata-service 的连接地址无需改动」（无需集群，纯渲染验证）。

**操作步骤**：

1. 渲染默认（Redis）配置，提取 Deployment/Service 名与镜像（注意：单独 `kustomize config/metadata` 不会加 `aibrix-` 前缀，因为前缀是在 config/default 层施加的，所以这里看到的是原始名）：

   ```shell
   kubectl kustomize config/metadata | grep -E "name: (redis-master|valkey-master)|image: (redis|valkey/.*)"
   ```

2. 模拟切换：把 kustomization 里的 `redis.yaml` 换成 `valkey.yaml` 后再渲染（用临时副本，不改源码）：

   ```shell
   TMP=$(mktemp -d) && cp -r config/metadata/. "$TMP"/
   sed -i 's/^- redis.yaml$/# - redis.yaml\n- valkey.yaml/' "$TMP"/kustomization.yaml
   kubectl kustomize "$TMP" | grep -E "name: (redis-master|valkey-master)|image: (redis|valkey/.*)"
   ```

**需要观察的现象**：

- 步骤 1（默认）：Deployment 名 `redis-master`、image `redis`、Service 名 `redis-master`。
- 步骤 2（切换后）：Deployment 名变成 `valkey-master`、image 变成 `valkey/valkey:8-alpine`，但 **Service 名仍是 `redis-master`**。

**预期结果**：Service 名在两种配置下都是 `redis-master`，证明 Valkey 是 drop-in 替换——经 namePrefix 加工后两者都叫 `aibrix-redis-master`，metadata-service 的 `REDIS_HOST` 无需任何改动。若无法运行命令，标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `valkey.yaml` 故意把 Service 名仍写成 `redis-master`，而不是改成 `valkey-master`？

**参考答案**：为了让前缀加工后的地址 `aibrix-redis-master` 保持不变，metadata-service 等所有通过 `REDIS_HOST` 连接存储的组件一行都不用改，从而实现「换镜像、不换接线」的 drop-in 替换。如果改了 Service 名，就得同步修改 `metadata.yaml` 的 `REDIS_HOST` 以及所有引用方，破坏兼容性，Valkey 也就不成其为「替代品」了。

**练习 2**：能否同时 apply `redis.yaml` 和 `valkey.yaml`，让 Redis 和 Valkey 都跑起来给不同组件用？

**参考答案**：不能。两者都声明同名 Service `redis-master`（前缀加工后同为 `aibrix-redis-master`），apply 时会因 Service 名冲突而失败；即便绕过名字，metadata-service 的 `REDIS_HOST` 也只指向同一个地址，无法分流。Valkey 的设计定位是「替代」Redis，kustomization 的 resources 里只能二选一。

---

## 5. 综合实践

把本讲的三层结构、安装顺序、CRD 分离设计串起来，完成下面这个任务（即本讲规格指定的核心实践）：

**任务**：对照 README 的 Quick Start，用自己的话完成以下三件事。

**第 1 件：写出三步安装命令各自的作用**。

参照 [README.md:56-63](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/README.md#L56-L63) 的 nightly 三步，按下表填写：

| 步骤 | 命令 | 作用 | 为什么在这个顺序 |
| --- | --- | --- | --- |
| ① 依赖 | `kubectl apply -k config/dependency --server-side` | （你来写） | （你来写） |
| ② CRD | `kubectl apply -k config/crd --server-side` | （你来写） | （你来写） |
| ③ 组件 | `kubectl apply -k config/default` | （你来写） | （你来写） |

**参考要点**：

- ① 装入 Envoy Gateway 与 KubeRay 的 CRD（外部依赖），用 server-side 是因为 CRD 体积大；必须最先，因为后续 AIBrix 控制器会用到 KubeRay 的类型。
- ② 装入 AIBrix 自己的 CRD（PodAutoscaler/ModelAdapter/StormService 等）；必须在组件之前，因为 operator 启动时要 watch 这些类型。
- ③ 装入 operator、网关、metadata、RBAC、webhook 等真正运行的组件；最后装，因为它依赖前两步提供的类型与依赖。

**第 2 件：解释为什么 CRD 要与 operator 分开安装**。

要求结合 [config/default/kustomization.yaml:20-24](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/default/kustomization.yaml#L20-L24) 的注释，给出两层理由：

1. Kubernetes 的级联删除规则：删 CRD 会删掉该类型下所有 CR 实例。
2. 因此把 CRD 放进单独清单（`aibrix-core-crds-*.yaml`），使得 `kubectl delete -k config/default` 只卸载 operator 而保留用户的 CR 数据（对应 issue #2062）。

**第 3 件（可选，需集群）**：在一个测试集群里真正跑一遍 nightly 三步，然后：

```shell
kubectl get crd | grep aibrix.ai          # 确认 CRD 已建
kubectl -n aibrix-system get pods         # 确认组件已起
kubectl apply -f samples/quickstart/model.yaml   # 部署示例模型
```

观察每一步的前置依赖关系。

**第 4 件：说明如何用 `config/metadata/valkey.yaml` 替换默认 Redis 元数据存储**。

要求结合 4.5 的源码，完成两点说明：

1. **怎么换**：在 `config/metadata/kustomization.yaml` 里把 `resources` 中的 `- redis.yaml` 注释掉、改为 `- valkey.yaml`，然后照常 `kubectl apply -k config/default`。指出为什么不能两者并存（同名 Service `redis-master` 冲突）。
2. **为什么「不用改连接配置」**：valkey.yaml 把 Service 名仍设为 `redis-master`（见 [config/metadata/valkey.yaml:48-64](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/metadata/valkey.yaml#L48-L64)），经 namePrefix 加工后仍是 `aibrix-redis-master`，与 metadata-service 的 `REDIS_HOST`（[config/metadata/metadata.yaml:98-102](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/config/metadata/metadata.yaml#L98-L102)）一致，且 Valkey 与 Redis wire-compatible（同 RESP 协议、同环境变量），故 metadata-service 无需任何改动。

若无集群，至少完成第 1、2、4 件的书写与 `kubectl kustomize` 渲染验证，并标注「集群部分待本地验证」。

## 6. 本讲小结

- `config/` 是一棵 kustomize 目录树，分为**依赖层**（`config/dependency`，外部组件）、**类型层**（`config/crd`，AIBrix 自有 CRD）、**组件层**（`config/default`，operator/网关/RBAC/webhook 等运行体）三层。
- 安装顺序固定为 **dependency → crd → default**：先装外部依赖类型，再建自己的表，最后启动用这些表的程序。
- **CRD 与 operator 故意拆成两份清单**（源码注释指向 issue #2062），核心原因是避免卸载 operator 时级联删除用户的 CR 实例（StormService、RoleSet、PodAutoscaler、ModelAdapter 等）。
- 安装 CRD/依赖统一带 `--server-side`，以规避大对象的客户端注解体积上限问题。
- **nightly** 用本地 `config/` 目录、镜像 tag 为 `nightly`；**stable** 用 GitHub Releases 预构建 YAML、镜像 tag 为固定版本（如 `v0.7.0`），二者经 `config/overlays/release` overlay 衔接。
- 元数据存储层默认用 `config/metadata/redis.yaml` 在集群内置一个 Redis（经 namePrefix 后 Service 名为 `aibrix-redis-master`，与 metadata-service 的 `REDIS_HOST` 对应）；要换成开源的 Valkey，只需在 `config/metadata/kustomization.yaml` 把 `redis.yaml` 换成 `valkey.yaml`——因为 valkey.yaml 把 Service 名仍设为 `redis-master` 且 wire-compatible，metadata-service 无需任何改动（不能与 redis.yaml 并存）。
- `samples/quickstart/model.yaml` 是一个普通 Deployment，靠 `model.aibrix.ai/name` 标签与 Service/`--served-model-name` 三者一致被 AIBrix 发现，演示了「安装 operator → 部署工作负载 → 被发现」的最短路径。

## 7. 下一步学习建议

- 想看 operator 启动后内部到底注册了哪些控制器、怎么受 Feature Gate 开关控制，进入 **u2-l1 控制器管理器入口与启动流程**、**u2-l2 Feature Gates**。
- 想深入 CRD 的字段设计（Spec/Status、kubebuilder 标记），进入 **u2-l3 自定义资源 (CRD) 数据模型设计**。
- 想脱离 K8s 快速体验 AIBrix 全栈，进入 **u1-l5 本地 Standalone 部署快速体验**（docker-compose 方式，那里同样可以通过 `REDIS_IMAGE` 等环境变量把元数据存储换成 Valkey）。
- 想知道本讲这个元数据存储（Redis/Valkey）到底被谁用，进入 **u8-l4 Redis 状态同步与配置画像**（多网关副本经 Redis 同步路由状态）与 **u9-l3 指标采集标准化与元数据服务**（metadata-service 如何存模型元数据）。
- 若你想直接看模型部署后如何被自动伸缩，可跳读 **u3-l1 PodAutoscaler 控制器与伸缩总览**（依赖 u2-l3）。
