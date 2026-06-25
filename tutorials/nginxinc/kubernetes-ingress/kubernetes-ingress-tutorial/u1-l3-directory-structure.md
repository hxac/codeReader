# 项目目录结构与模块布局

## 1. 本讲目标

本讲带你建立 NGINX Kubernetes Ingress Controller（以下简称 NIC）仓库的「空间地图」。学完后你应当能够：

- 一眼区分仓库里哪些目录是 **手写源码**、哪些是 **自动生成**、哪些是 **交付物**（清单 / 镜像 / 示例）。
- 准确说出 `internal/`（实现细节，禁止外部引用）与 `pkg/`（对外 API 与生成代码）的边界。
- 定位项目三大核心子目录：控制器（`internal/k8s`）、配置生成（`internal/configs`）、NGINX 进程管理（`internal/nginx`）。
- 看懂 `charts/`、`deployments/`、`examples/`、`tests/` 各自交付什么，并能在五层架构里给任意目录找到归属。

本讲只讲「目录在哪里、负责什么」，不深入任何模块的实现细节——那是后续讲义的任务。

## 2. 前置知识

阅读本讲前，你应当已经了解（来自 `u1-l1`、`u1-l2`）：

- **控制器范式**：`watch → queue → reconcile`，NIC 用原始 client-go（`SharedInformerFactory` + work queue）实现，而非 controller-runtime。
- **五层架构**：数据模型 → 校验 → 控制器 → 配置生成 → 进程管理，职责单向流动。
- **两类路由资源**：标准 Ingress（用 annotations/ConfigMap 扩展）与自定义资源 VirtualServer/VirtualServerRoute（L7）、TransportServer（L4）。

此外需要两个 Go 语言的常识：

- **`internal/` 是 Go 编译器的「可见性围栏」**：放在仓库根 `internal/` 下的包，只能被同一模块（`go.mod` 所定义的模块）内的代码 import，模块外引用会编译报错。所以一个项目把实现放进 `internal/`，等于宣告「这是私有实现，别从外部依赖我」。
- **手写代码 vs 生成代码**：Go 生态里常见以 `zz_generated.*.go`、`*_generated.go` 命名的文件，它们由 `controller-gen`、`client-gen` 等工具自动产出，**不应手改**。识别它们能帮你把注意力集中在真正需要读懂的源码上。

## 3. 本讲源码地图

本讲主要「读目录」而非「读函数」，核心参考两份文档与若干目录清单：

| 文件 / 目录 | 作用 |
| --- | --- |
| [README.md](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/README.md#L15-L15) | 项目门面与定位，L15 是项目标题入口 |
| [docs/developer/architecture.md](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/developer/architecture.md#L26-L52) | 五层架构图，是本讲给目录分类的「标尺」 |
| `cmd/`、`internal/`、`pkg/`、`config/`、`build/`、`charts/`、`deployments/`、`examples/`、`tests/` | 仓库各顶层目录，本讲逐一讲解 |

## 4. 核心概念与源码讲解

### 4.1 顶层目录速览

#### 4.1.1 概念说明

一个生产级项目（NIC 是几万行 Go 代码的大型项目）的仓库，通常按「**它是什么**」来切分顶层目录，而不是按功能。理解 NIC 仓库的第一步，是把十几个顶层目录归类成几个大组：

| 分组 | 包含的顶层目录 | 一句话职责 |
| --- | --- | --- |
| **代码** | `cmd/`、`internal/`、`pkg/` | 可编译的 Go 源码：入口、私有实现、对外 API |
| **生成产物** | `config/` | 由代码生成的 CRD YAML |
| **构建与交付** | `build/`、`charts/`、`deployments/` | Dockerfile、Helm chart、原始 K8s 清单 |
| **示例与文档** | `examples/`、`docs/` | 用户可直接 `kubectl apply` 的示例、开发者文档 |
| **测试** | `tests/`、`perf-tests/` | Python pytest 集成测试、性能测试 |
| **辅助** | `hack/`、`grafana/`、`tools.go`、`Makefile`、`go.mod` | 脚本、监控面板、工具版本锁、构建入口、依赖声明 |

> 说明：`hack/` 在很多 Kubernetes 生态项目里都表示「辅助脚本」（非贬义），用来跑代码生成、CI 前置检查等杂活；`grafana/` 存放开箱即用的 Grafana 仪表盘 JSON；`tools.go` 用 `//go:build tools` 锁定开发工具（如 `controller-gen`）的版本（详见 `u1-l2`）。

#### 4.1.2 核心流程

拿到仓库后，建议按下面的顺序建立空间感：

1. **先定位入口**：`cmd/nginx-ingress/main.go` 是程序唯一入口（`u1-l4` 详讲）。
2. **再定位三大核心子系统**：`internal/k8s`（控制器）、`internal/configs`（配置生成）、`internal/nginx`（进程管理）。这三个目录串起了从「监听 K8s 事件」到「reload NGINX」的主链路。
3. **然后看对外 API**：`pkg/apis/configuration/v1/types.go` 是 CRD 的数据真相源；`pkg/client/` 与 `config/crd/bases/` 是由它生成的。
4. **最后看交付与示例**：`charts/`、`deployments/`、`examples/` 决定项目如何被安装和使用。

这个顺序与五层架构图完全对应——从「进程管理」往上回溯到「数据模型」，再延伸到「交付物」。

#### 4.1.3 源码精读

- [README.md:15-15](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/README.md#L15-L15) —— 项目标题 `# NGINX Ingress Controller`，整个仓库的门面，从这里进入「Getting Started」。
- [docs/developer/architecture.md:26-52](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/developer/architecture.md#L26-L52) —— **「Architectural Layers」五层架构 ASCII 图**，是本讲给所有目录归类的标尺。每一层对应一个或几个目录：数据模型 → `pkg/apis`，校验 → `pkg/apis/configuration/validation` + `internal/k8s/validation.go`，控制器 → `internal/k8s`，配置生成 → `internal/configs`，进程管理 → `internal/nginx`。

#### 4.1.4 代码实践

1. **实践目标**：把仓库顶层目录亲手归类，建立空间感。
2. **操作步骤**：在仓库根目录执行 `ls -1F`，把输出抄下来，对照本节表格给每个目录贴一个「分组 + 一句话职责」标签。
3. **需要观察的现象**：哪些目录是代码、哪些是产物、哪些是交付物，三者数量大致相当。
4. **预期结果**：你能不看本讲，指着任何一个顶层目录说出它属于哪个分组。
5. **待本地验证**：本实践的「结果」是主观分类，不需要运行命令验证正确性。

#### 4.1.5 小练习与答案

**练习 1**：仓库里同时存在 `deployments/`（原始 YAML）和 `charts/`（Helm），它们是否重复？为什么两个都要保留？

> **答案**：不重复。`deployments/` 面向「直接 `kubectl apply`」的简单安装与排查场景，文件平铺、易读；`charts/` 面向「用 Helm 管理」的生产场景，支持参数化、版本化、值校验（`values.schema.json`）。两者是同一套部署能力的两种交付形态。

**练习 2**：`hack/` 目录为什么不被算作「代码」分组？

> **答案**：`hack/` 放的是辅助脚本（通常是 shell 或一次性 Go 脚本），用来跑代码生成、检查等杂活，不参与编译进 `nginx-ingress` 二进制，所以归入「辅助」而非「代码」。

---

### 4.2 internal 核心子系统

#### 4.2.1 概念说明

`internal/` 是 Go 的可见性围栏——这里面的所有包只能在模块内部被引用。NIC 把全部**实现细节**放进 `internal/`，这是项目最重要的「实现区」。`internal/` 下有十几个子目录，但真正构成主链路的是三大核心子系统：

| 子目录 | 对应架构层 | 核心职责 |
| --- | --- | --- |
| `internal/k8s/` | 控制器 | Informer 监听、事件入队、sync 调谐、内存模型、Secret 解析、status 回写 |
| `internal/configs/` | 配置生成 | 把扩展资源翻译成 NGINX 配置结构体，渲染 `.tmpl` 模板 |
| `internal/nginx/` | 进程管理 | 启动 / reload / 退出 NGINX，配置校验，失败回滚 |

其余子目录是「辅助能力」：`certmanager/`（cert-manager 集成）、`externaldns/`（外部 DNS）、`healthcheck/`（健康检查端点）、`metrics/`（Prometheus 指标）、`telemetry/`（OTLP 遥测）、`logger/`（日志）、`metadata/`（集群元数据）、`validation/`、`license_reporting/`、`common_cluster_info/`、`nsutils/`。

#### 4.2.2 核心流程

三大子系统构成一条单向数据流（与 `u1-l1` 的骨架一致）：

```text
K8s 事件 ──► internal/k8s（控制器）
                    │  组装扩展资源 VirtualServerEx
                    ▼
            internal/configs（配置生成）
                    │  渲染 .tmpl → 配置文本
                    ▼
            internal/nginx（进程管理）
                    │  写文件 + reload + 校验
                    ▼
              NGINX 实际生效
```

**关键约束**：架构文档明确规定了层间不可越界的规则，这是给目录「定边界」的依据——配置生成层不得调用 K8s API，控制器层不得渲染模板。

#### 4.2.3 源码精读

- [docs/developer/architecture.md:54-62](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/developer/architecture.md#L54-L62) —— **「What each layer owns」表格**，逐层列出了控制器 / 配置生成 / 进程管理各自负责什么、对应哪些包，是给三大子目录「对号入座」的权威清单。
- [docs/developer/architecture.md:64-77](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/developer/architecture.md#L64-L77) —— **「Layer crossing rules」层间规则**，规定三大子系统之间禁止互相越界（例如配置生成不能访问 SecretStore），理解它就理解了为什么这三个目录要严格分离。
- [docs/developer/architecture.md:305-319](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/developer/architecture.md#L305-L319) —— **「Key Source Files」表**，列出了三大子系统各自的「入口文件」（如 `internal/k8s/controller.go`、`internal/configs/configurator.go`、`internal/nginx/manager.go`），后续每篇讲义都从这些文件切入。

三大子目录内部的关键文件（先建立印象，不必现在读懂）：

| 子目录 | 关键文件（节选） | 用途 |
| --- | --- | --- |
| `internal/k8s/` | `controller.go`、`handlers.go`、`configuration.go`、`task_queue.go`、`status.go`、`leader.go`、`secrets/`、`policies/` | 控制器生命周期、事件处理、内存模型、任务队列、状态回写、选举、Secret 缓存 |
| `internal/configs/` | `configurator.go`、`virtualserver.go`、`ingress.go`、`policy.go`、`transportserver.go`、`version1/`、`version2/` | 配置中枢、各类资源翻译、两套模板体系 |
| `internal/nginx/` | `manager.go`、`rollback_manager.go`、`verify.go`、`version.go` | 进程管理、回滚保护、版本验证 |

> 注意 `internal/configs/` 下有 `version1/`（Ingress 模板）和 `version2/`（VirtualServer/TransportServer 模板）两个子目录，每个都含 OSS 与 Plus 两套 `.tmpl` 文件以及 `__snapshots__/`（快照黄金测试，见 `u8-l4`）。

#### 4.2.4 代码实践

1. **实践目标**：把三大子目录里的文件与五层架构的职责对应起来。
2. **操作步骤**：
   - 执行 `ls -1 internal/k8s/ | grep -v _test`，挑出 `controller.go`、`handlers.go`、`configuration.go`。
   - 执行 `ls -1 internal/configs/ | grep -v _test`，挑出 `configurator.go`、`virtualserver.go`、`version1/`、`version2/`。
   - 执行 `ls -1 internal/nginx/ | grep -v _test`，挑出 `manager.go`、`rollback_manager.go`、`verify.go`。
3. **需要观察的现象**：每个子目录里 `_test.go` 文件与生产文件成对出现（如 `controller.go` 配 `controller_test.go`），说明项目测试紧贴源码。
4. **预期结果**：你能用一句话说出上述每个文件属于架构哪一层、解决什么问题（对照 4.2.3 的表格）。
5. **待本地验证**：分类结果可与架构文档的 Key Source Files 表互相校对。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `internal/configs/version1/` 和 `version2/` 要分成两套模板？

> **答案**：因为 Ingress（v1）和 VirtualServer/TransportServer（v2）是两种数据模型：v1 一个配置含多个 `Server` 块，v2 一个配置只有一个 `Server` 块。两套模板各自适配自己的结构体，互不干扰；加新功能时常需要两套都改（见 CLAUDE.md「Templates」不变量）。

**练习 2**：`internal/nginx/verify.go` 为什么独立于 `manager.go` 存在，而不是合并进 manager？

> **答案**：reload 之后需要通过 unix socket 上的 configVersion 端点**轮询确认**新 worker 真正生效，这和「发 reload 命令」是两种不同职责（发命令是动作，等版本是验证）。拆成独立文件让验证逻辑更聚焦、更易测试（`verify_test.go`）。

**练习 3**：如果一个新同事问「我该把新的 K8s 事件处理逻辑放在哪里」，你会指向哪个目录？为什么不能放 `pkg/`？

> **答案**：放 `internal/k8s/`（控制器层）。不能放 `pkg/` 是因为事件处理是私有实现细节，属于 `internal/` 围栏内；`pkg/` 留给对外 API 与生成代码。

---

### 4.3 pkg 与生成代码

#### 4.3.1 概念说明

`pkg/` 是 NIC 的「对外门面」：这里放可以被外部引用的 API 定义。但要注意，`pkg/` 里**既有手写的「真相源」，也有大量自动生成的代码**，必须分清：

- **手写（真相源，需读懂）**：`pkg/apis/configuration/v1/types.go` 等 CRD 结构体定义，带 kubebuilder 标注，是整个 API 的源头。
- **生成（不要手改）**：`pkg/apis/.../zz_generated.deepcopy.go`（DeepCopy 方法）、整个 `pkg/client/`（typed clientset、informers、listers、applyconfiguration）、以及仓库根的 `config/crd/bases/*.yaml`（CRD 安装清单）。

这三类生成物都由 `make update-codegen` 和 `make update-crds` 从 `types.go` 派生（见 `u2-l3`）。

#### 4.3.2 核心流程

生成链路是一条单向派生：

```text
pkg/apis/configuration/v1/types.go  （手写，真相源）
        │
        ├── make update-codegen ──► pkg/apis/.../zz_generated.deepcopy.go
        │                        └► pkg/client/{clientset,informers,listers,applyconfiguration}
        └── make update-crds   ──► config/crd/bases/k8s.nginx.org_*.yaml
```

也就是说：**改 `types.go` 是「上游」，其余全是「下游」产物**。这就是 CLAUDE.md 反复强调「改 types.go 后必须依次跑 `make update-codegen`、`make update-crds`」的原因。

`pkg/apis` 还按 API Group 拆成三组：

| 子路径 | API Group | 内容 |
| --- | --- | --- |
| `pkg/apis/configuration/v1/` | `k8s.nginx.org` | VirtualServer/VSR/TransportServer/Policy/GlobalConfiguration |
| `pkg/apis/dos/v1beta1/` | `appprotectdos.f5.com` | DosProtectedResource |
| `pkg/apis/externaldns/v1/` | `externaldns.nginx.org` | DNSEndpoint |

每组下都有 `validation/` 子包，做字段级 CRD 校验。

#### 4.3.3 源码精读

- [docs/developer/architecture.md:33-36](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/developer/architecture.md#L33-L36) —— 架构图最顶层「Data Model」层，明确指出 `pkg/apis/configuration/v1/` 下的 `types.go` 与 `zz_generated.deepcopy.go` 是 API 真相源。
- [docs/developer/architecture.md:309-309](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/developer/architecture.md#L309-L309) —— Key Source Files 表第一行，标注 `pkg/apis/configuration/v1/types.go` 为「CRD struct definitions — the source of truth for the API」。
- [pkg/apis/configuration/v1/](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go) —— 该目录同时含手写的 `types.go` 和生成的 `zz_generated.deepcopy.go`，是观察「手写 + 生成」共存的最好样本。

生成产物的真实分布（已在仓库确认）：

| 路径 | 由什么生成 | 是否手改 |
| --- | --- | --- |
| `pkg/client/clientset/versioned/` | `client-gen` | 否 |
| `pkg/client/informers/externalversions/` | `informer-gen` | 否 |
| `pkg/client/listers/` | `lister-gen` | 否 |
| `pkg/client/applyconfiguration/` | `applyconfiguration-gen` | 否 |
| `config/crd/bases/k8s.nginx.org_virtualservers.yaml` 等 12 个文件 | `controller-gen`（从 kubebuilder 标注） | 否 |

#### 4.3.4 代码实践

1. **实践目标**：在 `pkg/` 里区分手写与生成代码。
2. **操作步骤**：
   - 执行 `find pkg -name 'zz_generated*'`，列出所有生成文件，确认它们都不该手改。
   - 执行 `ls -1 config/crd/bases/`，数出共有 12 个 CRD YAML，对应 12 种自定义资源（含 WAF/DoS/DNSEndpoint/NIC 自有资源）。
   - 打开 `pkg/apis/configuration/v1/`，对比 `types.go`（手写、含 kubebuilder 标注）与 `zz_generated.deepcopy.go`（纯生成、顶部有 `// Code generated by ... DO NOT EDIT.`）。
3. **需要观察的现象**：生成文件顶部都有 `DO NOT EDIT` 注释；手写文件没有。
4. **预期结果**：你能在 5 秒内判断 `pkg/` 下任意文件是手写还是生成。
5. **待本地验证**：可直接 `head` 文件查看首行注释。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `pkg/client/` 几乎整个目录都是生成的，却不放在 `internal/` 里？

> **答案**：因为 typed clientset/informers/listers 是**对外提供**的——其他项目可能想用编程方式访问 NIC 的 CRD（如写自己的控制器监听 VirtualServer），所以必须放 `pkg/` 而非 `internal/`。生成是为了减少手写样板代码，对外是设计取舍。

**练习 2**：`config/crd/bases/` 下的 YAML 和 `pkg/apis/.../types.go` 谁是源头？

> **答案**：`types.go` 是源头。YAML 由 `controller-gen` 读取 `types.go` 上的 kubebuilder 标注（`+kubebuilder:object`、`+kubebuilder:validation:...` 等）生成。所以改字段必须先改 `types.go`，再 `make update-crds`。

---

### 4.4 部署与示例目录

#### 4.4.1 概念说明

这一组目录回答「**NIC 如何被安装、如何被使用**」：

- **`deployments/`**：原始 Kubernetes 清单（YAML），按安装拓扑分子目录，适合直接 `kubectl apply`。
- **`charts/nginx-ingress/`**：官方 Helm chart，支持参数化安装，是生产推荐方式。
- **`examples/`**：可直接 apply 的示例资源（Ingress / VirtualServer / Policy 等），也是最好的「活文档」。
- **`tests/`**：Python pytest 集成测试套件，针对真实集群验证端到端行为。

#### 4.4.2 核心流程

`deployments/` 按部署拓扑拆分，每种拓扑对应一种运行形态：

| 子目录 | 安装形态 |
| --- | --- |
| `deployments/common/` | 公共资源：Namespace/SA、RBAC、IngressClass、全局 ConfigMap |
| `deployments/deployment/` | 以 **Deployment** 运行 NIC（常用） |
| `deployments/daemon-set/` | 以 **DaemonSet** 运行（每节点一个） |
| `deployments/stateful-set/` | 以 **StatefulSet** 运行 |
| `deployments/service/` | 暴露 NIC 的 Service（LoadBalancer / NodePort 等） |
| `deployments/rbac/` | 角色与绑定 |

`charts/nginx-ingress/` 是标准 Helm 结构：`Chart.yaml`（chart 元数据）、`values.yaml`（默认值）、`values.schema.json`（值的 JSON Schema 校验）、`templates/`（资源模板）、`crds/`（chart 自带的 CRD）。还提供 `values-plus.yaml`、`values-nsm.yaml`、`values-icp.yaml` 等场景化预设值文件。

`examples/` 分三大块：`ingress-resources/`（标准 Ingress 用法）、`custom-resources/`（VirtualServer/TransportServer/Policy 用法，含 50+ 场景如 `rate-limit`、`jwt`、`oidc`、`traffic-splitting`）、`shared-examples/`（跨示例复用资源）。

#### 4.4.3 源码精读

- [deployments/deployment/nginx-ingress.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/deployments/deployment/nginx-ingress.yaml) —— OSS 版以 Deployment 形态运行 NIC 的主清单，是看「NIC 怎么跑起来」的起点。
- [deployments/common/nginx-config.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/deployments/common/nginx-config.yaml) —— 全局 ConfigMap，它的 key 会被 `internal/configs/configmaps.go` 解析成 `MainConfig`（见 `u4-l2`）。
- [charts/nginx-ingress/Chart.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/charts/nginx-ingress/Chart.yaml) —— Helm chart 元数据（名称、版本、appVersion）。
- [charts/nginx-ingress/values.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/charts/nginx-ingress/values.yaml) —— chart 默认值，与 `values.schema.json` 一一对应（见 `u8-l5`）。
- [examples/custom-resources/basic-configuration/cafe-virtual-server.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/basic-configuration/cafe-virtual-server.yaml) —— 最基础的 VirtualServer 示例（cafe 应用），是学习 CRD 写法的入门样本。

#### 4.4.4 代码实践

1. **实践目标**：理解 Helm chart 与原始清单如何对应同一套部署。
2. **操作步骤**：
   - 读 `deployments/deployment/nginx-ingress.yaml`，识别它定义了哪几种 K8s 资源（Deployment / ConfigMap / Service 等）。
   - 读 `charts/nginx-ingress/templates/`，找出与上述资源对应的模板文件。
   - 读 `examples/custom-resources/basic-configuration/cafe-virtual-server.yaml`，对照 `u1-l1` 介绍的 VirtualServer 概念。
3. **需要观察的现象**：原始清单是「写死」的资源定义，Helm 模板则是「参数化」的同类资源（用 `{{ .Values.* }}` 占位）。
4. **预期结果**：你能说出「同一个 Deployment，原始清单给一份固定值，Helm 给一族可变值」。
5. **待本地验证**：可执行 `helm template charts/nginx-ingress` 渲染 chart 并与 `deployments/` 对照（需本地装 Helm，见 `u1-l6`）。

#### 4.4.5 小练习与答案

**练习 1**：`deployments/common/nginx-config.yaml` 里的 ConfigMap，最终被哪段代码消费？

> **答案**：被 `internal/configs/configmaps.go` 的 ConfigMap 解析逻辑消费，转成 `MainConfig` / `ConfigParams`，用于生成全局 `nginx.conf`（详见 `u4-l2`）。这说明 `deployments/`（交付物）与 `internal/configs/`（实现）之间有直接的数据通路。

**练习 2**：`examples/custom-resources/` 下有 50 多个子目录（如 `rate-limit`、`jwt`、`oidc`），它们除了给用户参考，对开发者还有什么价值？

> **答案**：它们也是集成测试与文档的素材——很多 `tests/suite/` 下的 pytest 用例会引用 `examples/` 里的资源作为测试数据，开发者改了某功能后能直接拿对应示例跑通验证。示例即「活文档」。

---

## 5. 综合实践

把本讲所有模块串起来，画一张完整的「NIC 仓库空间地图」：

1. **目标**：产出一份两级的目录树（顶层 + 各核心目录的一级子目录），每个目录附一句话职责，并标注它在五层架构中的归属。
2. **步骤**：
   - 执行 `ls -1F` 拿到顶层目录。
   - 对 `internal/`、`pkg/`、`deployments/`、`examples/` 各展开一层（`ls -1F internal/` 等）。
   - 对照本讲的表格，给每个目录写一句话职责，并在末尾用括号标注架构层（数据模型 / 校验 / 控制器 / 配置生成 / 进程管理 / 交付物 / 示例 / 测试 / 辅助）。
   - 最后用箭头画出一条「数据流」：从 `pkg/apis/configuration/v1/types.go`（数据模型）→ `internal/k8s`（控制器）→ `internal/configs`（配置生成）→ `internal/nginx`（进程管理），并旁注 `deployments/common/nginx-config.yaml` 如何喂给 `internal/configs`。
3. **预期结果**：一张既能当目录索引、又能体现五层架构与数据流的地图。完成它意味着你已建立本讲全部空间认知。

## 6. 本讲小结

- 仓库顶层目录按「**它是什么**」分组：代码（`cmd/internal/pkg`）、生成产物（`config`）、构建交付（`build/charts/deployments`）、示例文档（`examples/docs`）、测试（`tests/perf-tests`）、辅助（`hack/grafana/tools.go`）。
- `internal/` 是 Go 可见性围栏，放全部私有实现；三大核心子系统是 `internal/k8s`（控制器）、`internal/configs`（配置生成）、`internal/nginx`（进程管理），三者构成单向数据流且层间不可越界。
- `pkg/` 放对外 API：`pkg/apis/.../types.go` 是手写真相源，`pkg/client/` 与 `config/crd/bases/` 全是从它派生的生成代码（带 `DO NOT EDIT`，不要手改）。
- `deployments/`（原始清单，按拓扑分目录）、`charts/nginx-ingress`（Helm）、`examples/`（可直接 apply 的活示例）、`tests/`（pytest 集成测试）共同回答「NIC 如何被安装与使用」。
- 五层架构图（`architecture.md` 的 ASCII 图与 Key Source Files 表）是给所有目录归类、对号入座的权威标尺。

## 7. 下一步学习建议

建立空间地图后，建议按数据流方向深入：

- 先读 `u1-l4`（命令行入口与启动 flags），从 `cmd/nginx-ingress/main.go` 看程序如何启动并装配三大子系统。
- 再进入 `u2-l2`（CRD 类型定义源码），精读 `pkg/apis/configuration/v1/types.go` 这个数据真相源。
- 随后按 `u3 → u4 → u5` 的顺序，分别深入控制器（`internal/k8s`）、配置生成（`internal/configs`）、进程管理（`internal/nginx`）。
- 想动手安装体验，可先读 `u1-l6`（安装方式：清单与 Helm）。
