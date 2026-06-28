# 目录结构与代码组织

## 1. 本讲目标

在上一讲（u1-l1）里，我们已经知道了 NGINX Gateway Fabric（NGF）是「做什么的」——一个用 NGINX 当数据面的 Kubernetes Gateway API 实现。本讲要解决的是「代码放在哪儿」。

学完本讲，你应当能够：

1. 画出 NGF 仓库的顶层目录地图，并说出每个顶层目录大致负责什么。
2. 准确区分 `internal/controller`（NGF 专属的**控制面产品逻辑**）和 `internal/framework`（**可复用的通用框架**）这一条最重要的代码边界。
3. 知道自定义 CRD 类型（`apis`）、部署清单（`deploy`）、Helm chart（`charts`）、示例（`examples`）、文档（`docs`）、测试（`tests`）分别放在哪里，需要时能直接定位。

本讲是后续所有源码讲义的「坐标系」——后面每一讲都会落到这套目录结构里的某个具体位置。

## 2. 前置知识

- **Go 项目的常见布局**：Go 项目通常把可执行程序入口放在 `cmd/`，把不可对外复用的内部代码放在 `internal/`（`internal/` 是 Go 编译器强制的可见性边界，只有仓库内可以 import）。
- **Kubernetes 控制器的「控制面 / 数据面」概念**（上一讲已建立）：控制面 watch 资源、生成配置；数据面（NGINX）处理真实流量。本讲只看控制面这侧的代码是怎么分目录的。
- **CRD（CustomResourceDefinition）与 API 版本**：自定义资源类型通常按 `v1alpha1`、`v1beta1`、`v1` 这样分版本目录存放。NGF 自定义的策略、过滤器等类型就放在 `apis/` 下，并按版本分子目录。
- **Gateway API 的核心资源**（上一讲已建立）：GatewayClass / Gateway / HTTPRoute 等。这些是**上游标准库**提供的类型，不在本仓库；本仓库定义的是 NGF 自己额外加的「扩展类型」。

> 小提示：你可以把本仓库想成一个「翻译器工厂」——`cmd/` 是开关，`apis/` 是输入字段表，`internal/controller/` 是这台翻译器的核心电路，`internal/framework/` 是可以单独卖出去的通用零件，`deploy/charts/examples` 是它的出厂包装和说明书。

## 3. 本讲源码地图

本讲涉及的关键文件不多，但它们是理解整张地图的「路标」：

| 文件 / 目录 | 作用 |
| --- | --- |
| [README.md](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/README.md) | 项目入口说明，包含版本矩阵、示例与文档链接 |
| [cmd/gateway/main.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/main.go) | 整个程序**唯一的入口** `main()`，装配 cobra 子命令 |
| [internal/controller/doc.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/doc.go#L1-L4) | 用一句话定义了 `controller` 包的职责：**NGF 控制器实现** |
| [internal/framework/doc.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/doc.go#L1-L4) | 用一句话定义了 `framework` 包的职责：**构建控制器的通用包** |
| [apis/doc.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/doc.go) | 定义了 `apis` 包：**存放 NGF 的 API 定义** |

## 4. 核心概念与源码讲解

### 4.1 顶层目录全貌：一张地图

#### 4.1.1 概念说明

拿到一个陌生的大型 Go 项目，最快的入门方式不是读代码，而是先看目录。目录名往往已经透露了作者的设计意图。NGF 仓库的顶层目录可以分为四类：

1. **程序入口**：`cmd/`。
2. **核心代码**：`internal/`（强制只在仓库内可见）。
3. **API 类型定义**：`apis/`（可被外部 import 的 CRD 类型）。
4. **部署 / 示例 / 文档 / 测试 / 构建**：`deploy/`、`charts/`、`examples/`、`docs/`、`tests/`、`build/`、`scripts/`、`config/`、`operators/`、`design/`、`dev/`、`debug/`。

另外还有一些仓库治理文件：`Makefile`（构建脚本）、`go.mod` / `go.sum`（Go 依赖）、`README.md`、`CONTRIBUTING.md`、`CHANGELOG.md`、`SECURITY.md`、`LICENSE` 等。

#### 4.1.2 核心流程

一个典型的「从源码到运行」的查找顺序是：

```text
1. cmd/gateway/main.go        ← 程序从哪里启动？
2. internal/controller/...    ← 启动后装配了哪些产品级子系统？
3. apis/...                   ← 它 watch / 写回的资源类型定义在哪？
4. deploy/ 或 charts/         ← 怎么把它装到集群里？
5. examples/                  ← 装好后拿什么例子验证？
```

这条顺序也是本学习手册后面讲义的大致展开顺序（u2 CLI → u3 Manager → u8 状态/策略 → …）。

#### 4.1.3 源码精读

程序的唯一入口在 `cmd/gateway/main.go`，它极其简短，只做一件事：创建根命令，挂上五个子命令，然后执行。

[cmd/gateway/main.go:L20-L35](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/main.go#L20-L35) —— 这是 `main()` 函数，调用 `createRootCommand()` 创建根命令，再用 `rootCmd.AddCommand(...)` 把五个子命令（`controller` / `generate-certs` / `initialize` / `sleep` / `endpoint-picker`）挂上去。也就是说，整个二进制的所有功能都从这五个子命令进入。这些子命令的具体实现在同目录下的 `commands.go`、`certs.go`、`initialize.go`、`endpoint_picker.go` 等文件里（详见 u2-l1）。

注意同一个文件顶部还有几个「构建期注入」的变量：

[cmd/gateway/main.go:L8-L18](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/main.go#L8-L18) —— `version`、`telemetryReportPeriod` 等变量的注释写着 `// Set during go build`，说明它们不是写死的，而是在 `go build` 时通过 `-ldflags` 注入。这也解释了上一讲提到的「版本号是构建期注入」。

> 顶层目录速查表（按职责分组）：

| 类别 | 目录 | 一句话 |
| --- | --- | --- |
| 入口 | `cmd/` | 可执行程序入口（只有 `cmd/gateway`） |
| 核心代码 | `internal/` | 控制面代码，仓库外不可 import |
| API 类型 | `apis/` | NGF 自定义 CRD 类型，可被外部 import |
| 部署 | `deploy/` | 多种部署变体的 Kubernetes 清单 + `crds.yaml` |
| 部署 | `charts/` | Helm chart |
| 示例 | `examples/` | 端到端用法示例 |
| 文档 | `docs/` | 开发者文档（含 `developer/quickstart.md`） |
| 测试 | `tests/` | conformance / 集成测试 |
| 构建/脚本 | `build/` `scripts/` `config/` | 镜像、代码生成、kustomize 基础 |
| 其它 | `operators/` `design/` `dev/` `debug/` | Operator bundle、设计文档、开发工具 |

#### 4.1.4 代码实践

**实践目标**：用 `git ls-files` 看一眼顶层都有哪些目录，并对照上面的速查表给每个目录归类。

**操作步骤**：

1. 在仓库根目录执行 `git ls-files | sed 's#/.*##' | sort | uniq -c | sort -rn`（如果 `sed` 被禁用，可改用 `git ls-files | cut -d/ -f1 | sort | uniq -c`）。
2. 观察输出，每个顶层目录后面跟着一个数字，表示该目录下的文件数量。

**需要观察的现象**：

- `internal/` 下的文件数应当明显最多（这是代码主体）。
- `deploy/`、`charts/`、`examples/` 也各有一批文件。
- `apis/` 相对较小，因为类型定义本身不长，配套的代码生成文件（`zz_generated.deepcopy.go`）会拉高一点数量。

**预期结果**：你会得到一张「目录 → 文件数」的直方图，与本讲的速查表一一对应。这能帮你直观感受「代码主体在 `internal/`，部署/示例是围绕它的配套」。

> 说明：本实践是只读的源码探查，不修改任何文件，可在本地直接运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 NGF 把可执行程序入口放在 `cmd/gateway/` 而不是仓库根目录的 `main.go`？

**参考答案**：这是 Go 社区推荐的布局。`cmd/<名字>/main.go` 的好处是：当将来需要第二个二进制（例如一个迁移工具）时，可以在 `cmd/` 下新增并列目录而互不干扰；同时把「入口」和「业务逻辑」物理分开，`main.go` 保持极薄，只负责装配，不写业务。NGF 的 `main.go` 确实只有十几行，全部业务在 `internal/` 里。

**练习 2**：`internal/` 这个目录名有什么特殊含义？

**参考答案**：`internal` 是 Go 工具链的**硬性约定**：`internal/` 下的包只能被同模块（或其父目录链）内的代码 import，模块外引用会直接编译报错。NGF 把所有控制面实现放进 `internal/`，意味着这些代码是私有的、不被当作对外 API，可以随时重构。只有想对外暴露的类型（CRD 类型）才放到 `internal/` 之外的 `apis/`。

**练习 3**：仓库根目录有一堆 `.md` 文件（README、CONTRIBUTING、CHANGELOG、SECURITY），它们和源码学习有什么关系？

**参考答案**：它们是「元信息」而不是源码。`README.md` 给出版本矩阵和入门链接；`CONTRIBUTING.md` 说明如何贡献；`docs/developer/` 下的 `quickstart.md`、`implementing-a-feature.md`、`design-principles.md` 等是后续讲义反复引用的开发指南。读源码前先扫一遍 `docs/developer/` 能少走很多弯路。

---

### 4.2 internal/controller 与 internal/framework：最重要的那条边界

#### 4.2.1 概念说明

`internal/` 下只有两个顶层包：`controller` 和 `framework`。理解它们的边界，就理解了整个代码库的最高层切分。

- **`internal/controller`（控制面产品逻辑）**：写的是「NGF 这个产品」。它知道 Gateway API 的语义、知道 NGINX 要怎么配、知道 NGF 自己的 CRD（策略、过滤器）该怎么翻译成 NGINX 指令。换一个产品，这部分代码就没用了。
- **`internal/framework`（可复用框架）**：写的是「如何搭一个控制器」。它提供事件循环、控制器抽象、leader 选举、WAF 拉取等**通用能力**，理论上不绑定 NGF 的具体业务。这部分代码换一个数据面（比如换成另一个反代）也能复用。

这条边界不是随便画的——两个包各自的 `doc.go` 用一句话把它「钉死」了。

#### 4.2.2 核心流程

把这两个包想象成两层：

```text
┌──────────────────────────────────────────────┐
│  internal/controller   （产品逻辑层）          │
│  - manager.go        装配控制面                 │
│  - handler.go        事件批次总编排             │
│  - state/            图构建、变更捕获           │
│  - nginx/            配置生成 + Agent 下发      │
│  - status/ telemetry/ provisioner/ ...         │
└─────────────────────┬────────────────────────┘
                      │ 调用通用能力
┌─────────────────────┴────────────────────────┐
│  internal/framework    （可复用框架层）         │
│  - controller/   Register/Reconciler 抽象      │
│  - events/       事件循环 + 双缓冲批处理        │
│  - runnables/    Leader 选举、CronJob          │
│  - waf/          bundle 拉取/轮询              │
│  - kinds/ kubernetes/ file/ helpers/ types/    │
└──────────────────────────────────────────────┘
```

依赖方向是**单向**的：`controller` 依赖 `framework`，反之不行。这样 `framework` 可以独立演进，而产品逻辑随时可以替换。

#### 4.2.3 源码精读

`internal/controller/doc.go` 全文只有 4 行，但它定义了整个包的边界：

[internal/controller/doc.go:L1-L4](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/doc.go#L1-L4) —— 注释写道 *「Package controller contains all the packages that relate to the controller implementation of NGF.」*（controller 包包含所有与 **NGF 控制器实现**相关的包）。关键词是 **of NGF**——它是「NGF 专属」的。

对照看 `internal/framework/doc.go`：

[internal/framework/doc.go:L1-L4](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/doc.go#L1-L4) —— 注释写道 *「Package framework contains general packages for building the controller.」*（framework 包包含**用于构建控制器**的通用包）。关键词是 **general / building**——它是「通用工具」，不带 NGF 业务。

`internal/controller` 下按子系统再分目录，每个目录对应学习手册里的一个单元：

| 子目录 / 文件 | 职责 | 对应单元 |
| --- | --- | --- |
| `manager.go` | 装配整个控制面（`StartManager`） | u3 |
| `handler.go` | 事件批次的总编排 | u4 |
| `state/` | 变更捕获、图（graph）构建、dataplane 配置 | u4、u5 |
| `nginx/config/` | 把配置渲染成 NGINX 配置文件 | u6 |
| `nginx/agent/` | 通过 NGINX Agent 下发配置 | u7 |
| `status/` | 把 Conditions 写回 K8s 资源 | u8 |
| `provisioner/` | 按 Gateway 配置数据面资源 | u9 |
| `telemetry/`、`metrics/` | 遥测与指标 | u11 |
| `licensing/`、`crd/`、`ngfsort/` | licensing、CRD 发现、冲突排序 | 各高级单元 |

而 `internal/framework` 提供的则是更底层的能力，例如自研的控制器抽象和事件循环：

| 子目录 | 职责 | 典型文件 |
| --- | --- | --- |
| `controller/` | 控制器抽象（`Register`/`Reconciler`） | `register.go`、`reconciler.go` |
| `events/` | 事件循环、双缓冲批处理 | `loop.go`、`first_eventbatch_preparer.go` |
| `runnables/` | Leader 选举、CronJob | `runnables.go`、`cronjob.go` |
| `waf/` | WAF bundle 拉取 / 轮询 | `fetch/`、`poller/` |
| `kinds/` `kubernetes/` `file/` `helpers/` `types/` | 类型工具、k8s 辅助、文件、辅助函数 | — |

> 一句话记忆：**`controller` 写「NGF 是什么」，`framework` 写「怎么搭一个控制器」**。

#### 4.2.4 代码实践

**实践目标**：用静态依赖关系验证「`controller` 依赖 `framework`、反过来不成立」这条边界。

**操作步骤**：

1. 在仓库根目录执行：`grep -rn "nginx-gateway-fabric/internal/framework" internal/controller | wc -l`（统计 controller 包里引用 framework 的次数）。
2. 再反向执行：`grep -rn "nginx-gateway-fabric/internal/controller" internal/framework | wc -l`。

**需要观察的现象**：

- 第 1 条命令应当输出一个正数（controller 大量复用 framework 的能力）。
- 第 2 条命令**理想情况输出 0**——framework 不应该反过来依赖产品逻辑。

**预期结果**：你会看到「单向依赖」被代码本身证实。如果第 2 条不是 0，那就是一个值得讨论的边界泄漏（待本地验证具体引用点）。这条实践能让你直观体会「分层」不是口号，而是编译期就被 import 路径约束住的。

> 说明：`grep` 是只读操作，不修改源码。

#### 4.2.5 小练习与答案

**练习 1**：如果 NGF 要支持一种新的「虚拟服务器」CRD，新代码应该放进 `internal/controller` 还是 `internal/framework`？为什么？

**参考答案**：放进 `internal/controller`（具体说，类型定义放 `apis/`，图处理逻辑放 `internal/controller/state/graph/`，配置生成放 `internal/controller/nginx/config/`）。因为这是「NGF 这个产品」的业务能力，不属于「如何搭控制器」的通用范畴。`framework` 不应感知任何具体业务资源。

**练习 2**：`internal/controller/state/loop.go`（如果存在）和 `internal/framework/events/loop.go` 是什么关系？

**参考答案**：事件循环的「机制」（双缓冲、批处理、首批事件准备）属于通用能力，因此实现在 `internal/framework/events/loop.go`；而「这个事件批次里要处理哪些 NGF 资源、产出什么配置」属于产品逻辑，由 `internal/controller/handler.go` 在 `framework` 提供的事件循环之上编排。机制和策略分离，正是这条边界存在的意义。（具体文件位置以 u4-l2 的源码精读为准。）

**练习 3**：为什么把 CRD 类型放在 `apis/` 而不是 `internal/controller/`？

**参考答案**：CRD 类型需要被「仓库之外」的工具和用户引用——比如代码生成器、 Helm chart、用户自己的 controller。`internal/` 的可见性限制会阻止这些外部引用，所以对外暴露的类型必须放在 `apis/`。这也是 Kubernetes 生态的惯例：`apis/` 放可对外暴露的 API 类型，`internal/` 放实现。

---

### 4.3 apis、deploy、charts、examples、docs、tests 概览

#### 4.3.1 概念说明

除了 `internal/` 这块代码主体，还有几个目录是你部署、使用、理解 NGF 时高频接触的。它们各自承担「定义类型 / 部署 / 示例 / 文档 / 测试」的角色：

- **`apis/`**：NGF 自定义的 CRD 类型定义。按版本分目录：`v1alpha1`、`v1alpha2`，以及专门的 `waf/v1`。
- **`deploy/`**：纯 Kubernetes 清单（YAML），用 kustomize 组织成多种部署变体。
- **`charts/`**：Helm chart（`charts/nginx-gateway-fabric`），用 values 灵活配置安装。
- **`examples/`**：22 个端到端示例目录，每个演示一类用法（路由、TLS、限流、WAF……）。
- **`docs/`**：开发者文档，其中 `docs/developer/` 是本手册反复引用的「开发指南」。
- **`tests/`**：conformance 测试、集成测试框架等（独立 `go.mod`）。

#### 4.3.2 核心流程

当你要「装一个 NGF 并验证」时，这些目录的协作顺序是：

```text
charts/nginx-gateway-fabric   ← helm install 用它（内部会引用 deploy/ 的清单和 crds.yaml）
        │
        ▼
deploy/crds.yaml              ← 安装 apis/ 里定义的所有 CRD
        │
        ▼
examples/<某示例>             ← 创建 Gateway / HTTPRoute / Policy 验证功能
        │
        ▼
tests/conformance             ← 自动化回归上游 Gateway API 一致性
```

`apis/` 是「源」——它定义类型；`deploy/crds.yaml` 和 chart 里的 CRD 是「产物」，通常由代码生成器从 `apis/` 生成（详见 `docs/developer/implementing-a-feature.md`）。

#### 4.3.3 源码精读

`apis/doc.go` 同样用一句话定义了包的职责：

[apis/doc.go:L1-L2](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/doc.go) —— *「Package apis stores the API definitions for NGINX Gateway Fabric configuration.」*（apis 包存放 NGF 配置的 **API 定义**）。

`apis/` 按版本与主题组织（这些文件都真实存在）：

| 目录 | 代表文件 | 说明 |
| --- | --- | --- |
| `apis/v1alpha1/` | `clientsettingspolicy_types.go`、`ratelimitpolicy_types.go`、`snippetsfilter_types.go`、`authenticationfilter_types.go`、`wafpolicy_types.go`、`nginxgateway_types.go` 等 | 一批策略 / 过滤器 CRD 类型 |
| `apis/v1alpha2/` | `nginxproxy_types.go`、`observabilitypolicy_types.go` | NginxProxy、ObservabilityPolicy |
| `apis/waf/v1/` | （WAF 相关内部类型） | WAF bundle / PLM 状态类型 |
| 各目录共有 | `register.go`、`policy_methods.go`、`zz_generated.deepcopy.go` | 注册 GVK、策略方法、自动生成的深拷贝 |

注意每个版本目录里都有 `zz_generated.deepcopy.go`——这是 controller-runtime 代码生成器的产物（文件名 `zz_generated` 是惯例），**不要手改**。这也是为什么新增 CRD 要走 `make generate-all`（详见 u13-l3）。

`deploy/` 提供多种部署变体（每个子目录是一个 kustomize overlay / 独立清单集合）：

| 子目录 | 适用场景 |
| --- | --- |
| `default` | 标准 OSS 部署 |
| `nginx-plus` | NGINX Plus 部署 |
| `nodeport` | 用 NodePort 暴露 |
| `snippets` / `snippets-nginx-plus` | 启用 SnippetsFilter 能力 |
| `openshift` | OpenShift 环境 |
| `inference` / `inference-nginx-plus` | 启用推理扩展（InferencePool） |
| `experimental` / `experimental-nginx-plus` | 实验特性 |
| `azure` | Azure 环境相关 |
| `crds.yaml` | 所有 CRD 的聚合清单 |
| `kustomization.yaml` | kustomize 入口 |

`charts/` 下只有一个 chart：`charts/nginx-gateway-fabric`，它的 `values.yaml` 和 `Chart.yaml` 是 Helm 安装时的核心（详见 u1-l4）。

`examples/` 有 22 个目录，几乎覆盖了 NGF 的全部用例，例如 `cafe-example`（经典路由）、`https-termination`、`tcp-routing`、`udp-routing`、`grpc-routing`、`basic-authentication`、`cors-filter`、`rate-limit-policy`、`client-settings-policy`、`snippets`、`waf-policy`、`cross-namespace-routing` 等（详见 u1-l5）。

`docs/developer/` 下的关键文档（真实存在，后续讲义会引用）：`quickstart.md`、`implementing-a-feature.md`、`design-principles.md`、`crd-versioning.md`、`debugging.md`、`go-style-guide.md`、`pull-request.md`、`mapping.md`。

`tests/` 自带独立的 `go.mod`，包含 `conformance/`（上游 Gateway API 一致性测试）、`framework/`（测试辅助）、`cel/`、`ipv6/` 等子目录（详见 u13-l1）。

#### 4.3.4 代码实践

**实践目标**：在 `examples/` 里找一个能跑通的最小示例，确认它的资源结构符合上一讲建立的三层模型（GatewayClass / Gateway / HTTPRoute）。

**操作步骤**：

1. 进入 `examples/cafe-example/`（或任选一个路由类示例目录）。
2. 用 `ls` 列出里面的 YAML 文件。
3. 打开其中的 `httproute` 相关 YAML，找到 `kind: HTTPRoute` 的资源。
4. 在该资源里找 `parentRefs`（它指向哪个 Gateway）和 `backendRefs`（它指向哪个 Service）。

**需要观察的现象**：

- 一个示例通常由多个 YAML 组成：后端应用 Deployment + Service、Gateway、HTTPRoute。
- `HTTPRoute` 通过 `parentRefs` 挂到某个 `Gateway`，再通过 `backendRefs` 指向 `Service`，正好对应上一讲讲的「三层资源」。

**预期结果**：你会看到上一讲抽象的模型在真实 YAML 里是怎么落地的，并且确认 `examples/` 是「带验证功能的活文档」。如果你想跑起来，可以用 `kubectl apply -f examples/cafe-example/`（需要先装好 NGF，详见 u1-l4、u1-l5）。

> 说明：本实践只读 YAML，不修改任何文件；若要实际 apply，需要本地有可用的集群，结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`apis/v1alpha1/zz_generated.deepcopy.go` 是谁生成的？为什么放在 `apis/` 而不是 `internal/`？

**参考答案**：由 controller-runtime 的 `controller-gen` 工具生成（命令通常是 `make generate-all` 的一部分）。深拷贝方法必须和类型定义放在一起，且 CRD 类型对外暴露（在 `apis/`），所以生成文件也跟着放在 `apis/<version>/`。文件名 `zz_generated` 是社区惯例，表示「自动生成、勿手改」。

**练习 2**：`deploy/` 和 `charts/` 都能装 NGF，它们的差别是什么？

**参考答案**：`deploy/` 是「静态的 kustomize 清单」，适合直接 `kubectl apply -k` 或被 CI 直接消费，变体通过不同子目录区分；`charts/` 是「参数化的 Helm chart」，通过 `values.yaml` 在同一套模板上覆盖配置，适合需要灵活定制参数的场景。两者底层都引用相同的 CRD 定义和镜像。生产里二选一即可。

**练习 3**：`tests/conformance/` 测的是什么？为什么它有独立的 `go.mod`？

**参考答案**：它跑的是**上游 Gateway API 的一致性测试**（conformance），验证 NGF 是否正确实现了 Gateway API 标准。独立 `go.mod` 是因为 conformance 测试要引入上游 `sigs.k8s.io/gateway-api` 的测试套件及较重的依赖，与主模块解耦可以避免污染控制面的依赖图，也能让测试以自己的节奏升级版本。

---

## 5. 综合实践

本讲的贯穿任务是：**画一张 NGF 仓库目录树，并用三种颜色给目录分类**。这正是本讲规格里要求的实践，它把 4.1～4.3 的知识点串起来。

**实践目标**：产出一棵带颜色标注的目录树，作为你后续读源码时的「导航图」。

**三种颜色的语义**：

- 🟥 **入口（红色）**：程序从这里启动。
- 🟦 **控制面产品逻辑（蓝色）**：NGF 专属的业务实现。
- 🟩 **可复用框架（绿色）**：不绑定 NGF 业务的通用能力。

**操作步骤**：

1. 在仓库根目录执行 `ls -1` 得到顶层目录列表。
2. 按下面的颜色规则给每个目录上色。
3. 对 🟥 和 🟦 两类，进一步展开一层子目录（这是后续讲义的主战场）。

**参考标注（你可以照此画在自己的笔记里）**：

```text
nginx-gateway-fabric/
│
├── 🟥 cmd/                          ← 入口：唯一的二进制 gateway
│   └── gateway/                     ← main.go + 五个子命令实现
│
├── 🟦 internal/controller/          ← 控制面产品逻辑（NGF 专属）
│   ├── manager.go                   ← 装配控制面（StartManager）
│   ├── handler.go                   ← 事件批次总编排
│   ├── state/                       ← 图构建、变更捕获、dataplane 配置
│   ├── nginx/                       ← 配置生成(config/) + Agent 下发(agent/)
│   ├── status/  telemetry/  metrics/  provisioner/
│   ├── licensing/  crd/  ngfsort/
│   └── config/config.go             ← 运行时配置结构
│
├── 🟩 internal/framework/           ← 可复用框架（不绑定 NGF 业务）
│   ├── controller/                  ← Register/Reconciler 抽象
│   ├── events/                      ← 事件循环 + 双缓冲批处理
│   ├── runnables/                   ← Leader 选举、CronJob
│   ├── waf/                         ← bundle 拉取 / 轮询
│   └── kinds/ kubernetes/ file/ helpers/ types/
│
├── apis/                            ← 自定义 CRD 类型（v1alpha1 / v1alpha2 / waf）
├── deploy/                          ← kustomize 部署变体 + crds.yaml
├── charts/nginx-gateway-fabric/     ← Helm chart
├── examples/                        ← 22 个端到端示例
├── docs/developer/                  ← 开发指南（quickstart / implementing-a-feature…）
├── tests/                           ← conformance / 集成测试（独立 go.mod）
├── build/ scripts/ config/          ← 镜像、代码生成、kustomize 基础
├── operators/ design/ dev/ debug/   ← Operator bundle、设计文档、开发工具
└── Makefile  go.mod  go.sum  README.md …  ← 构建、依赖、治理文档
```

**需要观察的现象**：

- 🟥 只有一个点（`cmd/gateway`），非常薄。
- 🟦 的子目录最多，正好对应后续 u3～u9 的各个单元。
- 🟩 是 🟦 的「地基」，体积更小但更通用。
- 其余目录（apis/deploy/charts/examples/docs/tests）都是围绕这三类核心代码的「配套」。

**预期结果**：你得到一张可以长期贴在墙上的导航图。后续每一讲提到 `internal/controller/state/graph` 或 `internal/framework/events/loop.go` 时，你都能立刻在图上定位它在哪一层、负责什么。

> 说明：本实践是纯整理型的源码阅读实践，不运行任何会改状态的命令，产出物是你自己的笔记。

## 6. 本讲小结

- NGF 顶层目录可分为四类：**入口（`cmd/`）**、**核心代码（`internal/`）**、**API 类型（`apis/`）**、**部署/示例/文档/测试（`deploy` `charts` `examples` `docs` `tests` 等）**。
- 程序唯一入口是 `cmd/gateway/main.go`，它只装配五个 cobra 子命令，业务全部在 `internal/` 里。
- 全仓库最重要的边界是 **`internal/controller`（NGF 产品逻辑）↔ `internal/framework`（可复用框架）**，两个包各自的 `doc.go` 用一句话钉死了这条边界，依赖方向单向：`controller` 依赖 `framework`。
- `internal/controller` 按子系统分目录（`state/`、`nginx/`、`status/`、`provisioner/`、`telemetry/`…），正好对应学习手册 u3～u9 的单元。
- `apis/` 存放可对外暴露的 CRD 类型，按 `v1alpha1` / `v1alpha2` / `waf/v1` 分版本，含自动生成的 `zz_generated.deepcopy.go`。
- `deploy/`、`charts/`、`examples/`、`docs/`、`tests/` 分别承担「部署变体 / Helm 安装 / 用法示例 / 开发文档 / 一致性测试」的配套角色。

## 7. 下一步学习建议

有了这张地图，接下来建议沿「入口 → 装配」的方向往里走：

1. **下一讲 u1-l3（构建与本地运行）**：先用 `Makefile` 把这个仓库编译出二进制、跑起来，亲手验证 `cmd/gateway` 这个入口。
2. **进入 u2（CLI 入口）**：精读 `cmd/gateway/main.go` 挂载的五个子命令，看清入口是怎么跳转到业务逻辑的。
3. **再进入 u3（Manager 组装）**：精读 `internal/controller/manager.go` 的 `StartManager`，看 🟦 这一层是怎么把自己装配起来的。
4. **随手对照 `docs/developer/quickstart.md` 和 `implementing-a-feature.md`**：这两份官方开发文档和本讲义互补，能帮你把目录结构和实际开发流程对应起来。

> 阅读建议：在进入 u2 之前，把本讲的「三色目录树」画一遍——这是后面所有源码讲义的坐标系。
