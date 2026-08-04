# 构建系统、依赖与 Makefile 任务

## 1. 本讲目标

本讲聚焦于 AIBrix 的「构建系统」。读完本讲，你应当能够：

1. 读懂仓库根目录的 `Makefile`，并按类别说出 `build`、`manifests`、`generate`、`docker-build-*`、`test`、`lint` 等常用目标各自做什么。
2. 理解 AIBrix 的两套代码生成机制——`controller-gen` 生成 CRD 与 deepcopy，`code-generator` 生成 clientset/lister/informer——以及它们落在哪些目录。
3. 看懂镜像构建流程：为什么控制器镜像 `CGO_ENABLED=0`，而网关镜像却必须开 CGO 并链接 ZeroMQ。
4. 独立追踪一个目标（例如 `make build`）背后触发的完整依赖链。

本讲承上一讲「仓库目录结构」已经建立的认知（monorepo、`api→pkg→cmd` 单向依赖、`cmd/controllers` 与 `cmd/plugins` 共用一个 `go.mod`），把视角从「代码放在哪」推进到「代码如何被编译、生成与打包」。

## 2. 前置知识

本讲会用到几个 kubebuilder / Go 生态的基础概念，先通俗解释：

- **Make / Makefile**：一个用「目标（target）+ 依赖 + 命令」描述构建步骤的工具。运行 `make <target>` 时，Make 会先把它依赖的目标也跑一遍。AIBrix 把所有重复的构建/生成/部署命令都封装成了 Make 目标。
- **go.mod / Go module**：Go 的依赖清单。上一讲已确认，AIBrix 整个 Go 代码（控制平面 + 网关）共用根目录这一个 [go.mod](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/go.mod)，入口不同但依赖统一管理。
- **CRD（CustomResourceDefinition）**：把「自定义资源」注册进 Kubernetes 的清单（YAML）。CRD 描述了某个自定义资源（如 `PodAutoscaler`）有哪些字段。
- **controller-gen**：kubebuilder 配套工具，能读取 Go 结构体上的 `+kubebuilder:...` 注解，反向「生成」CRD YAML，以及给每个类型生成 `DeepCopy` 方法。
- **code-generator**：Kubernetes 官方工具，能基于 API 类型生成一套 typed 客户端（clientset、lister、informer、apply configuration），免去手写。
- **CGO**：Go 调用 C 代码的机制。`CGO_ENABLED=1` 表示编译时需要链接 C 库（如 ZeroMQ），产物依赖动态库；`CGO_ENABLED=0` 生成纯静态二进制，更便于用 distroless 基础镜像分发。
- **distroless 镜像**：只包含运行程序所必需文件、不含 shell/包管理器的极简容器镜像，体积小、攻击面小。

> 提示：本讲大量引用「生成代码」。生成代码指的是机器自动产出、不应手改的文件，AIBrix 通过 `zz_generated.*` 命名约定与独立的 `pkg/client` 目录来标识它们。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| [Makefile](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile) | 构建系统的中枢，定义所有 make 目标、工具版本与镜像清单。 |
| [go.mod](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/go.mod) | Go 依赖清单，声明 module 路径、Go 版本与所有依赖。 |
| [.golangci.yml](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/.golangci.yml) | golangci-lint 配置，决定启用哪些静态检查规则。 |
| [hack/update-codegen.sh](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/hack/update-codegen.sh) | 调用 code-generator 生成 typed 客户端（clientset/lister/informer）。 |
| [hack/boilerplate.go.txt](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/hack/boilerplate.go.txt) | 生成代码时自动追加的版权头模板。 |
| [build/container/](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/build/container) | 各组件的 Dockerfile（控制器、网关、运行时等）。 |
| [api/autoscaling/v1alpha1/groupversion_info.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/groupversion_info.go) | API 分组定义，带 `+kubebuilder:object:generate=true` 等代码生成注解。 |

## 4. 核心概念与源码讲解

### 4.1 Makefile 目标分类

#### 4.1.1 概念说明

AIBrix 的 `Makefile` 是整个构建系统的入口。它遵循 kubebuilder 脚手架的约定：用注释 `##@ 分类名` 划分大类别，用 `目标名: ## 说明` 给每个目标写一行帮助文本。因此运行 `make help` 就能得到一份自动生成的目标清单——这是阅读任何 kubebuilder 项目 Makefile 的第一招。

理解 Makefile 的关键不是逐行背命令，而是抓住三件事：

1. **目标分类**：哪些目标属于「开发」「构建」「部署」「依赖管理」。
2. **依赖链**：一个目标 `:` 后面跟的是它的前置依赖，Make 会先执行它们。例如 `build` 依赖 `manifests generate fmt vet`。
3. **变量与版本固定**：所有外部工具（kustomize、controller-gen 等）都固定了版本并按需 `go install` 到本地 `bin/`，保证每个人构建环境一致。

#### 4.1.2 核心流程

AIBrix Makefile 的目标大致可归为六类：

```text
General        help                        # 打印目标清单
Development    manifests / generate / test / lint / fmt / vet   # 生成代码与本地校验
Build          build / build-*             # 编译出本地二进制
Deployment     install / deploy / deploy-release  # 把清单应用到集群
Dependencies   kustomize / controller-gen / envtest   # 按需安装工具到 bin/
Docker         docker-build-* / docker-push-*        # 构建并推送容器镜像
```

一个典型的「开发→构建」链路如下：

```text
make build
  └─ make manifests   (生成 CRD/RBAC/webhook YAML)
  └─ make generate    (生成 deepcopy + typed 客户端)
  └─ make fmt         (go fmt)
  └─ make vet         (go vet)
  └─ go build -o bin/manager cmd/controllers/main.go   (真正编译)
```

也就是说，AIBrix 的构建目标会「先确保生成代码是最新的，再编译」。这一点非常关键：如果你只改了 `api/` 下的类型而没重新生成，编译或测试就会用到旧的 deepcopy/CRD。

#### 4.1.3 源码精读

**help 目标：自动解析注释生成清单**——这是 kubebuilder Makefile 的标志性写法，用 awk 扫描所有 `##@` 与 `##` 注释：

[Makefile:54-56](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L54-L56) 用 awk 把 `目标: ## 说明` 解析成对齐的彩色帮助文本。

类别注释长这样（举几例）：

- [Makefile:58](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L58) `##@ Development`
- [Makefile:213](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L213) `##@ Build`
- [Makefile:353](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L353) `##@ Deployment`
- [Makefile:488](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L488) `##@ Dependencies`

**build 目标：依赖前置生成 + 编译控制器**——注意它依赖了 `manifests generate fmt vet` 四件套：

[Makefile:215-217](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L215-L217) `build` 先跑生成与检查，再 `go build -o bin/manager cmd/controllers/main.go` 产出控制器二进制。

**工具版本固定**：所有构建工具都钉死版本，避免「我这能编、你那不能编」：

[Makefile:504-508](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L504-L508) 定义了 `KUSTOMIZE_VERSION`、`CONTROLLER_TOOLS_VERSION`（controller-gen）、`ENVTEST_VERSION`、`GOLANGCI_LINT_VERSION` 等版本号。

**go-install-tool 宏**：统一用 `go install` 把工具装到本地 `bin/`，并按版本改名以支持多版本共存：

[Makefile:539-547](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L539-L547) 若目标二进制不存在，就 `GOBIN=$(LOCALBIN) go install <pkg>@<version>`，再把产物重命名带上版本后缀（如 `controller-gen-v0.16.1`）。

**test / lint 目标**：测试与静态检查同样依赖生成代码：

[Makefile:139-142](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L139-L142) `test` 依赖 `manifests generate fmt vet envtest`，然后用 envtest 提供的本地 apiserver 跑单测，并排除 e2e/integration 包。

[Makefile:177-179](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L177-L179) `lint` 依赖 `golangci-lint` 工具目标，再运行 `$(GOLANGCI_LINT) run`。

> golangci-lint 启用哪些检查由 [.golangci.yml:23-43](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/.golangci.yml#L23-L43) 决定，例如 `errcheck`、`govet`、`gocyclo`、`lll`（行长限制）等；并对 `api/*`、`pkg/*` 做了部分豁免。

#### 4.1.4 代码实践

**实践目标**：用 `make help` 读懂目标全貌，并确认各类别下都有哪些目标。

**操作步骤**：

1. 在仓库根目录运行 `make help`，观察输出。它会按 `##@` 分组列出所有带 `##` 说明的目标。
2. 在输出中分别找到 `Build`、`Development`、`Deployment`、`Dependencies` 四个分组。
3. 在 `Makefile` 里用搜索定位 `##@ Build` 这一行，往下数有几个 `build-*` 目标，记下它们各自编译哪个入口。

**需要观察的现象**：

- `make help` 输出的目标与 Makefile 中带 `##` 的目标一一对应，且按 `##@` 分组对齐。
- `Build` 组里除了 `build`，还有 `build-controller-manager`、`build-gateway-plugins`、`build-gateway-plugins-nozmq`、`build-console` 等针对不同入口的目标。

**预期结果**：你能不查文档，仅凭 `make help` 说清「要单独编译网关该用哪个目标」。

> 待本地验证：`make help` 与 `make build` 的实际输出需要在你本地环境（需安装 Go 1.22 与 make）运行后确认。本讲不假定已运行成功。

#### 4.1.5 小练习与答案

**练习 1**：`make build` 触发后，`bin/manager` 是由哪条具体命令产生的？

> **答案**：由 [Makefile:217](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L217) 的 `go build -o bin/manager cmd/controllers/main.go` 产生；在它之前，Make 已经先跑完了 `manifests`、`generate`、`fmt`、`vet` 四个依赖。

**练习 2**：为什么 AIBrix 要把 `controller-gen` 的版本钉死成 `v0.16.1` 而不是用 `latest`？

> **答案**：不同版本的 controller-gen 生成的 CRD 与 deepcopy 代码会有差异（字段、格式、schema 细节）。钉死版本是为了保证所有贡献者与 CI 生成的代码完全一致，否则会出现「一人重新生成、提交大面积 diff」的噪声。`verify` 目标就是用来在 CI 里卡这道关。

---

### 4.2 代码生成机制（manifests / generate）

#### 4.2.1 概念说明

Kubernetes 控制器项目几乎都靠「声明式注解 + 代码生成」来减少样板代码。AIBrix 用到**两套**生成器，职责不同，极易混淆，必须分清：

| 生成器 | 工具 | 读取 | 产出 | 落点 |
| --- | --- | --- | --- | --- |
| CRD / deepcopy | `controller-gen`（controller-tools） | `+kubebuilder:...` 注解 | CRD YAML、RBAC、Webhook 配置、`zz_generated.deepcopy.go` | `config/crd/bases/`、`api/*/v1alpha1/` |
| typed 客户端 | `code-generator`（k8s 官方） | API 类型结构体 | clientset、lister、informer、apply configuration | `pkg/client/` |

一句话记忆：**`controller-gen` 管「类型怎么变成 CRD 和怎么深拷贝」，`code-generator` 管「怎么生成一套类型安全的 K8s 客户端」**。

为什么需要这两套？因为控制器代码里既要拿到「CRD 清单」去安装到集群，又要用 typed 客户端（而不是手写 REST 调用）去读写自定义资源。手写既枯燥又易错，所以交给工具生成。

#### 4.2.2 核心流程

**manifests 目标（controller-gen 出 CRD）** 的流程：

```text
make manifests
  └─ 先确保 controller-gen 已安装到 bin/
  └─ controller-gen rbac crd webhook paths="./..."
       读取 api/**/*_types.go 上的 +kubebuilder 注解
       产出 config/crd/bases/*.yaml（CRD）
       产出 RBAC ClusterRole、Webhook 配置
```

**sync-crds（把 bases 分发到各模块与 Helm）**：controller-gen 只把 CRD 写到 `config/crd/bases/`，但 kustomize 各 overlay 与 Helm chart 需要引用它们，于是再 copy 到 `config/crd/{orchestration,autoscaling,model}/` 和 `dist/chart/crds/`。

**generate 目标（controller-gen deepcopy + code-generator 客户端）** 的流程：

```text
make generate
  └─ controller-gen object:headerFile="hack/boilerplate.go.txt" paths="./..."
       给每个类型生成 DeepCopy/DeepCopyInto/DeepCopyObject
       产出 api/*/v1alpha1/zz_generated.deepcopy.go
  └─ ./hack/update-codegen.sh
       调 code-generator 的 kube_codegen.sh
       产出 pkg/client/{clientset,informers,listers,applyconfiguration}
```

#### 4.2.3 源码精读

**manifests 目标**——这是产出 CRD YAML 的唯一入口，依赖 `controller-gen`：

[Makefile:72-74](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L72-L74) 调用 `controller-gen rbac:... crd:maxDescLen=0,generateEmbeddedObjectMeta=true webhook paths="./..."`，把 CRD 输出到 `config/crd/bases`。

关键参数含义：

- `paths="./..."`：扫描整个模块的 `+kubebuilder` 注解。
- `crd:maxDescLen=0`：CRD 描述（description）字段不裁剪长度。
- `output:crd:artifacts:config=config/crd/bases`：CRD YAML 落到 `config/crd/bases/`。

**sync-crds 目标**——把 bases 按域名分发到模块目录：

[Makefile:80-89](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L80-L89) 依赖 `manifests`，新建三个模块目录后，按文件名匹配（如 `*autoscaling.aibrix.ai_*`）把 CRD 拷贝过去。

**sync-crds-to-helm 目标**——把 bases 同步到 Helm chart：

[Makefile:99-109](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L99-L109) 把 `config/crd/bases/*.yaml` 全量覆盖到 `dist/chart/crds/`，注释里写明「覆盖 helmify 生成的版本」——即以 `config/crd/` 为权威来源。

**一键全量生成**——把生成与所有同步串起来：

[Makefile:93-95](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L93-L95) `manifests-all: manifests sync-crds sync-crds-to-helm`，一条命令把 CRD 生成 + 模块分发 + Helm 同步全部完成。

**generate 目标**——两步：先 deepcopy，再 typed 客户端：

[Makefile:111-114](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L111-L114) 先用 `controller-gen object:` 生成 `zz_generated.deepcopy.go`（`headerFile` 指定版权头），再调用 `hack/update-codegen.sh` 生成客户端。

**update-codegen.sh 的核心两行**——这是 code-generator 真正干活的地方：

[hack/update-codegen.sh:32-40](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/hack/update-codegen.sh#L32-L40) 分别调用 `kube::codegen::gen_helpers`（生成 DeepCopy 等辅助方法）与 `kube::codegen::gen_client`（带 `--with-watch --with-applyconfig`，输出到 `pkg/client`，输出包路径为 `github.com/vllm-project/aibrix/pkg/client`）。

> 该脚本顶部注释明确写道：deepcopy 由 controller-tools 覆盖，本脚本只负责 clientset/lister/informer 等——这正是区分两套生成器的直接证据。

**生成结果在仓库里的样子**：你可以亲眼确认这些文件确实存在：

- deepcopy 落在三个 API 组各自目录：`api/{autoscaling,model,orchestration}/v1alpha1/zz_generated.deepcopy.go`
- typed 客户端落在：`pkg/client/{applyconfiguration,clientset,informers,listers}/`

**代码生成的「源头」——groupversion_info.go 的注解**：controller-gen 之所以知道要为某个包生成，靠的是包级注解：

[api/autoscaling/v1alpha1/groupversion_info.go:18-20](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/groupversion_info.go#L18-L20) `// +kubebuilder:object:generate=true` 与 `// +groupName=autoscaling.aibrix.ai` 告诉 controller-gen：这个包要生成对象，属于 `autoscaling.aibrix.ai` API 组。

**verify 目标——CI 守门员**：

[Makefile:120-129](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L120-L129) `verify: verify-codegen verify-crd`，前者检查 `pkg/client` 是否与重新生成的一致（[hack/verify-codegen.sh](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/hack/verify-codegen.sh) 用临时目录 diff），后者用 `hack/verify-crd-sync.sh` 检查 CRD 是否同步。任何「改了类型却忘了重新生成」都会被这里拦下。

#### 4.2.4 代码实践

**实践目标**：追踪「CRD YAML 与 deepcopy 各自由哪个工具产出」，并亲手验证生成器与产物的对应关系。

**操作步骤（源码阅读型）**：

1. 打开 [Makefile:72-74](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L72-L74)，确认 `manifests` 只用 `controller-gen`，工具二进制由 [Makefile:515-518](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L515-L518) 的 `controller-gen` 目标安装。
2. 打开 [Makefile:111-114](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L111-L114)，确认 `generate` 调了 `controller-gen object:`（产 deepcopy）**和** `hack/update-codegen.sh`（产客户端）。
3. 列出真实产物：在仓库里执行 `ls api/autoscaling/v1alpha1/zz_generated.deepcopy.go` 与 `ls pkg/client/clientset/`，确认二者都存在。

**需要观察的现象**：

- `manifests` 的命令行里只有 `controller-gen`，没有 `code-generator`。
- `generate` 同时包含两个生成动作。
- `pkg/client/` 下确实有 `clientset`、`informers`、`listers`、`applyconfiguration` 四个子目录。

**预期结果**：你能用一句话回答——「CRD YAML 由 controller-gen 经 `manifests` 产出；deepcopy 由 controller-gen 经 `generate` 产出；typed 客户端由 code-generator 经 `hack/update-codegen.sh` 产出」。

> 待本地验证：若你本地已装好 Go 与 controller-gen，可运行 `make manifests` 后用 `git status` 查看 `config/crd/bases/` 是否有变更，以此验证生成确实落在该目录。

#### 4.2.5 小练习与答案

**练习 1**：某贡献者修改了 `api/model/v1alpha1/modeladapter_types.go` 加了一个字段，只跑了 `make build` 就提交，CI 会在哪个 `make` 目标上报错？

> **答案**：会在 `make verify`（即 `verify-codegen` / `verify-crd`）上报错。因为新字段意味着 deepcopy 代码、CRD YAML、typed 客户端都需要重新生成；`make build` 虽然内部依赖了 `manifests generate` 会重新生成本地文件，但如果贡献者没有把生成结果一起提交，CI 重新生成后与仓库版本 diff 不一致，`verify` 就会失败。正确做法是改动 API 类型后运行 `make manifests-all generate` 并提交生成结果。

**练习 2**：为什么 `hack/update-codegen.sh` 要先把 CRD bases 分发到 `config/crd/orchestration` 等模块目录？直接用 `bases/` 不行吗？

> **答案**：因为不同的 kustomize overlay（以及 Helm chart）需要按各自关心的 API 域引用 CRD。把 CRD 按域名拆分到模块目录，可以让每个 overlay 只 kustomize 它需要的部分，避免把所有 CRD 一次性塞进每个部署场景。`bases/` 是 controller-gen 的权威产出，模块目录与 `dist/chart/crds/` 是面向不同消费方的分发副本。

---

### 4.3 Docker 镜像构建

#### 4.3.1 概念说明

AIBrix 在 Kubernetes 上以容器镜像形式分发各组件。镜像构建同样封装在 Makefile 里，但比本地 `go build` 多一层考虑：**不同组件对 C 库的依赖不同**。

最典型的对比是控制平面 vs 网关：

- **controller-manager（控制器）**：纯 Go，`CGO_ENABLED=0`，编译出完全静态的二进制，用极简的 `distroless/static` 镜像即可。
- **gateway-plugins（网关）**：依赖 ZeroMQ（一种高性能消息库，C 实现），必须 `CGO_ENABLED=1` 链接 `libzmq`、`libsodium` 等，产物依赖动态库，要用带 glibc 的 `distroless/base-debian12` 并把 `.so` 一起打包进去。

这就是为什么 Makefile 里有 `build-gateway-plugins` 和 `build-gateway-plugins-nozmq` 两个版本——后者去掉 ZeroMQ 用于 standalone 等不需要它的场景。

#### 4.3.2 核心流程

镜像构建的总体流程：

```text
make docker-build-controller-manager
  └─ build_and_tag(controller-manager, Dockerfile)   # 见 Makefile define
       └─ docker build -t aibrix/controller-manager:<tag> -f build/container/Dockerfile .
       └─ 若 IS_MAIN_BRANCH=true，再 docker tag ... :nightly
```

镜像清单与命名规则：

- [Makefile:9-10](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L9-L10) 定义了 `IMAGES := controller-manager gateway-plugins runtime metadata-service`，并据此拼出带 `:nightly` 的镜像列表 `AIBRIX_IMAGES`。
- 镜像 tag 默认取 git commit hash：[Makefile:2-3](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L2-L3) `IMAGE_TAG ?= ${GIT_COMMIT_HASH}`，即每次构建的 tag 对应当前提交，便于追溯。

构建用宏 `build_and_tag`：

[Makefile:258-261](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L258-L261) 接收「镜像名」与「Dockerfile 文件名」两个参数，先 `docker build`，再在主分支上额外打一个 `:nightly` tag。

`docker-build-all` 并行构建全部镜像：

[Makefile:268-270](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L268-L270) 用 `make -j $(nproc)` 并行跑 controller-manager、gateway-plugins、runtime、metadata-service、kvcache-watcher 五个镜像构建。

各组件对应的 Dockerfile（都在 `build/container/`）：

| make 目标 | Dockerfile | 说明 |
| --- | --- | --- |
| `docker-build-controller-manager` | `Dockerfile` | 多阶段，distroless/static |
| `docker-build-gateway-plugins` | `Dockerfile.gateway` | CGO + ZeroMQ，distroless/base-debian12 |
| `docker-build-runtime` / `metadata-service` | `Dockerfile.python` | Python 运行时边车 |
| `docker-build-kvcache-watcher` | `Dockerfile.kvcache` | KV Cache watcher |

#### 4.3.3 源码精读

**控制器 Dockerfile——纯静态、distroless**：

[build/container/Dockerfile:24-33](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/build/container/Dockerfile#L24-L33) 用 `CGO_ENABLED=0` 编译出静态 `manager`，第二阶段基于 `gcr.io/distroless/static:nonroot` 只拷贝二进制，并以非 root 用户（65532）运行。

注意它的分层缓存技巧：先 `COPY go.mod go.sum` 再 `go mod download`，最后才 `COPY cmd/ api/ pkg/`——这样改源码不会让依赖下载层失效，加速重复构建。

**网关 Dockerfile——CGO + ZeroMQ + 动态库打包**：

[build/container/Dockerfile.gateway:13-25](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/build/container/Dockerfile.gateway#L13-L25) 在 builder 阶段用 apt 安装 `libzmq3-dev`、`libsodium-dev`、`build-essential` 等 C 编译依赖。

[build/container/Dockerfile.gateway:41-49](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/build/container/Dockerfile.gateway#L41-L49) 设置 `CGO_ENABLED=1` 与 `CGO_LDFLAGS="-lzmq -lsodium ..."`，用 `-tags=zmq` 与 `-linkmode=external` 编译，产物 `gateway-plugins` 依赖动态库。

[build/container/Dockerfile.gateway:52-58](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/build/container/Dockerfile.gateway#L52-L58) 用 `ldd` 列出二进制依赖的所有 `.so`，逐一拷到 `deps/`；运行阶段基于 `distroless/base-debian12:nonroot`，把这些库放进 `/gateway-plugins-lib` 并设 `LD_LIBRARY_PATH`。

这套 `ldd` 抽库的技巧，正是为了让「依赖动态库的 CGO 程序」也能跑在精简的 distroless 镜像里——distroless 没有 apt/包管理，无法现装库，只能把库拷进去。

**Makefile 里的对应编译目标**（本地构建，与 Dockerfile 思路一致）：

[Makefile:219-221](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L219-L221) `build-controller-manager` 用 `CGO_ENABLED=0 -tags="nozmq"`。

[Makefile:223-229](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L223-L229) `build-gateway-plugins` 用 `CGO_ENABLED=1` + 一长串 `CGO_LDFLAGS` + `-tags="zmq"`。

> 这解释了一个常见困惑：为什么本地 `make build` 能编出 `bin/manager`，但编网关却可能报 ZeroMQ 找不到——因为后者需要你本机已安装 `libzmq` 开发库。Dockerfile 通过 apt 安装解决了这一点。

#### 4.3.4 代码实践

**实践目标**：对比控制器与网关两份 Dockerfile，理解 CGO 开关如何影响镜像选型。

**操作步骤（源码阅读型）**：

1. 打开 [build/container/Dockerfile](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/build/container/Dockerfile)，找到 `CGO_ENABLED` 的值（应为 0）与第二阶段的 `FROM` 基础镜像（`distroless/static`）。
2. 打开 [build/container/Dockerfile.gateway](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/build/container/Dockerfile.gateway)，找到 `CGO_ENABLED`（应为 1）、`CGO_LDFLAGS` 里的 `-lzmq`，以及第二阶段基础镜像（`distroless/base-debian12`）。
3. 在两份文件里各找到「拷贝依赖库」相关行：控制器没有 `ldd` 拷库步骤，网关有。

**需要观察的现象**：

- 控制器：静态二进制 + 最小镜像，无需拷动态库。
- 网关：动态二进制 + 带库镜像，必须把 `libzmq` 等 `.so` 拷进去并设 `LD_LIBRARY_PATH`。

**预期结果**：你能解释「为什么网关镜像基础镜像必须是 `base-debian12` 而不是更小的 `static`」——因为 CGO 程序依赖 glibc 与若干动态库，`static` 镜像里没有这些。

> 待本地验证：本地若有 docker，可运行 `make docker-build-controller-manager`（无需 ZeroMQ）观察多阶段构建日志；网关镜像构建需要能 apt 安装 libzmq，网络受限环境下可能失败。

#### 4.3.5 小练习与答案

**练习 1**：`docker-build-controller-manager` 与 `docker-build-gateway-plugins` 分别会产出名为 `:nightly` 的额外 tag 吗？条件是什么？

> **答案**：会，但仅当 `IS_MAIN_BRANCH=true`。见 [Makefile:258-261](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L258-L261) 的 `build_and_tag` 宏：构建后用 `if [ "${IS_MAIN_BRANCH}" = "true" ]` 判断，只有主分支才额外 `docker tag ... :nightly`。默认 `IS_MAIN_BRANCH ?= true`（[Makefile:256](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L256)）。

**练习 2**：如果有人想新增一个「不带 ZeroMQ 的网关」镜像目标，应该参考哪两个现有目标？为什么？

> **答案**：参考 `build-gateway-plugins-nozmq`（[Makefile:231-233](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L231-L233)，`CGO_ENABLED=0 -tags="nozmq"`）和 `docker-build-gateway-plugins`（调用 `build_and_tag`）。新镜像可以复用 `build_and_tag` 宏，传入一个「不装 libzmq」的 Dockerfile；它不需要 `ldd` 抽库步骤，基础镜像也可以回到更小的 `distroless/static`。

---

## 5. 综合实践

**任务**：以 `make build` 为线索，把本讲三个模块串起来，画出一张「从源码到控制器二进制」的完整产出表。

**要求完成**：

1. 追踪 `make build` 的依赖链，列出它按顺序触发的全部目标（提示：见 [Makefile:215-217](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile#L215-L217)，并展开 `manifests`、`generate` 各自的依赖）。
2. 对链路上每个目标，填出下表：

| 目标 | 依赖的工具 | 产出物 | 产出落在 |
| --- | --- | --- | --- |
| `manifests` | controller-gen | CRD/RBAC/webhook YAML | `config/crd/bases/` |
| `generate`（第 1 步） | controller-gen | `zz_generated.deepcopy.go` | `api/*/v1alpha1/` |
| `generate`（第 2 步） | code-generator（经 `hack/update-codegen.sh`） | clientset/lister/informer | `pkg/client/` |
| `fmt` / `vet` | go 自带 | — | — |
| `build`（末步） | go build | `bin/manager` | `bin/` |

3. 回答两个串联问题：
   - 如果只运行 `make docker-build-controller-manager` 而不先跑 `make build`，`config/crd/bases/` 里的 CRD 会是最新吗？为什么？（提示：看 `docker-build-*` 目标**没有**依赖 `manifests`，而 Dockerfile 直接 `COPY api/ pkg/` 并在容器内 `go build`。）
   - 控制器镜像最终跑在什么基础镜像上、用什么用户？这与网关镜像的根本差异是什么？

**预期成果**：你能凭这张表向别人讲清「AIBrix 的控制器二进制与 CRD 分别由哪条命令链产生、各自依赖哪个工具」，并且理解镜像构建与本地 `make build` 是两条相对独立的路径（镜像构建在容器内自行编译，不依赖本地的 `bin/manager`）。

> 待本地验证：第 3 题第一个问题的确切行为，建议在你本地分别运行 `make docker-build-controller-manager` 前后用 `git status` 观察 `config/crd/bases/` 是否变化来验证。

## 6. 本讲小结

- AIBrix 用一个根 [Makefile](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/Makefile) 统管 Go 代码的生成、编译、测试、镜像构建与部署，目标用 `##@`/`##` 注释分组，`make help` 可自动打印清单。
- `make build` 不是孤立编译，它依赖 `manifests generate fmt vet`，体现了「先保证生成代码最新、再编译」的设计。
- 代码生成有**两套**：`controller-gen`（经 `manifests`/`generate`）产 CRD 与 deepcopy；`code-generator`（经 `hack/update-codegen.sh`）产 typed 客户端到 `pkg/client/`。
- CRD 的权威产出在 `config/crd/bases/`，再由 `sync-crds` / `sync-crds-to-helm` 分发到模块目录与 Helm chart；`make verify` 在 CI 里卡住「忘重新生成」的提交。
- 镜像构建区分 CGO 需求：控制器 `CGO_ENABLED=0` 走 distroless/static，网关 `CGO_ENABLED=1` 链接 ZeroMQ 走 distroless/base-debian12 并用 `ldd` 抽动态库。
- 所有构建工具（controller-gen、kustomize、envtest、golangci-lint）版本都钉死在 Makefile 里，按需 `go install` 到本地 `bin/`，保证环境一致。

## 7. 下一步学习建议

掌握了「怎么编译、怎么生成代码」之后，下一步应该回到「代码本身」：

- 想了解控制器的启动入口（`bin/manager` 来自的 `cmd/controllers/main.go`），请阅读 **u2-l1 控制器管理器入口与启动流程**。
- 想知道那些被生成 deepcopy 与 CRD 的「API 类型」长什么样，请阅读 **u2-l3 自定义资源 (CRD) 数据模型设计**，它会带你打开 `api/` 下的 `*_types.go`。
- 想看这些 CRD 与组件如何被装进集群（`make install` / `make deploy` 背后的 kustomize 分层），请阅读 **u1-l4 Kubernetes 部署与 CRD 安装流程**。

建议同时用 `git log --oneline -- Makefile hack/` 看一眼构建系统近期的演进，体会一个云原生项目是如何逐步把构建脚本沉淀进 Makefile 的。
