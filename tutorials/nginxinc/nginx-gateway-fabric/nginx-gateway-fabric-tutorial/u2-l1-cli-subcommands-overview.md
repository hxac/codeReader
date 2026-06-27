# 命令行子命令总览

## 1. 本讲目标

本讲聚焦 NGF（NGINX Gateway Fabric）的命令行入口 `cmd/gateway`。学完后你应该能够：

- 说清 `cmd/gateway` 这个**唯一二进制**如何用 cobra 组装出根命令与五个子命令。
- 逐一讲清 `controller` / `generate-certs` / `initialize` / `sleep` / `endpoint-picker` 这五个子命令**各做什么**、它们的处理函数入口在源码的哪一行。
- 区分**控制面主命令**与**辅助命令**，并指出每个子命令在真实部署里运行在**哪类容器**（控制面容器、cert-generator Job、数据面 Pod 的 init 容器 / sidecar / lifecycle 钩子）。
- 理解 `version` 与三组 `telemetry*` 变量是**构建期注入**的，以及它们如何进入运行时。

承接 u1-l2：上一讲你已经知道 `cmd/gateway` 是全仓库唯一的二进制入口、业务逻辑都在 `internal/`。本讲往下钻一层，把这个二进制「拆开」，看清它的命令装配方式与跳转点——这是后续 u2-l2（controller 命令与配置）、u3（Manager 装配）等讲义的起点。

## 2. 前置知识

- **cobra**：Go 生态最流行的命令行框架。它用「命令树」组织 CLI：一个根命令（root command）下可以挂多个子命令（subcommand），每个命令自带 `Use`（命令名）、`Short`（一句话说明）、`RunE`（执行函数）等字段。`rootCmd.AddCommand(...)` 把子命令挂到根上；用户敲 `gateway controller` 时，cobra 负责解析、校验 flag，然后调用 `controller` 命令的 `RunE`。
- **pflag**：cobra 使用的 flag 库，兼容 POSIX 风格，支持 `--gateway-ctlr-name` 这样的长选项，也支持把 flag 绑定到自定义的 `pflag.Value` 类型（u2-l4 会专门讲这种校验型 Value）。
- **控制面 / 数据面**：控制面是 Go 写的「翻译官」（watch 资源、生成 NGINX 配置、下发），数据面是真正跑流量的 NGINX 进程。它们是**两个不同的 Pod/容器**，靠 NGINX Agent 经 gRPC+mTLS 通信。
- **构建期注入（ldflags -X）**：Go 的链接器可以在编译期用 `-ldflags -X main.version=xxx` 把某个包级变量的值「塞」进二进制。NGF 用这种方式把版本号、遥测参数在 `go build` 时写死，运行时只读不问。
- **init 容器 / lifecycle 钩子**：Kubernetes 里，init 容器在主容器启动前**跑一次**就退出；`lifecycle.preStop` 钩子在容器被终止**前**执行。NGF 用这两类机制分别承载 `initialize` 与 `sleep` 子命令。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `cmd/gateway/main.go` | 程序入口。声明构建期注入的变量，组装根命令并挂载五个子命令。 |
| `cmd/gateway/commands.go` | 本讲主战场。定义根命令与全部五个子命令的 flag、`RunE`，以及 flag 解析、构建信息读取等辅助函数。 |
| `cmd/gateway/initialize.go` | `initialize` 子命令的实际实现：拷贝文件、为 Plus 生成 deployment context。 |
| `cmd/gateway/certs.go` | `generate-certs` 子命令的实际实现：自签 CA/服务端/客户端证书并写回 Secret。 |
| `cmd/gateway/endpoint_picker.go` | `endpoint-picker` 子命令的实际实现：起一个本地 HTTP shim，把 NGINX 的请求转发给推理扩展的 EndpointPicker。 |
| `charts/nginx-gateway-fabric/templates/deployment.yaml` | 控制面 Deployment 模板，证明 `controller` 运行在 `nginx-gateway` 容器。 |
| `charts/nginx-gateway-fabric/templates/certs-job.yaml` | 证明 `generate-certs` 运行在一个 Helm pre-install/pre-upgrade **Job** 里。 |
| `internal/controller/provisioner/objects.go` | provisioner 构造数据面 Pod 的代码，证明 `initialize` 跑在 init 容器、`endpoint-picker` 跑在 sidecar。 |

## 4. 核心概念与源码讲解

### 4.1 cobra 根命令组装

#### 4.1.1 概念说明

NGF 的命令行程序只编译出**一个二进制**（名为 `gateway`），却要承担五种截然不同的职责（跑控制面、生成证书、初始化数据面、睡眠、推理 shim）。cobra 的「根命令 + 子命令」模式正是为此而生：根命令 `gateway` 本身不干活（只打印帮助），真正的逻辑分散在它的五个子命令里。这样可以用同一份镜像、同一个二进制，在不同容器里以不同子命令启动，承担不同角色。

#### 4.1.2 核心流程

根命令的装配流程可以概括为三步：

1. `createRootCommand()` 构造一个名为 `gateway` 的根命令：`SilenceUsage` 和 `SilenceErrors` 都置为 `true`（出错时不重复打印用法），其 `RunE` 直接返回 `cmd.Help()`——也就是「敲 `gateway` 不带子命令时，只显示帮助」。
2. `main()` 调用 `rootCmd.AddCommand(...)`，把五个子命令的构造函数返回值一次性挂到根命令下。
3. `rootCmd.Execute()` 让 cobra 解析命令行、路由到对应子命令并执行；若返回错误，`main` 把它打到 stderr 并以退出码 1 退出。

```text
gateway (root, 只显示 help)
 ├── controller        ← 控制面主进程
 ├── generate-certs    ← 自签证书
 ├── initialize        ← 数据面 init
 ├── sleep             ← lifecycle 睡眠
 └── endpoint-picker   ← 推理扩展 shim
```

#### 4.1.3 源码精读

入口 `main` 非常薄，只做「装配 + 执行」：

[cmd/gateway/main.go:20-35](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/main.go#L20-L35) —— `main` 先建根命令，再用 `AddCommand` 把五个子命令挂上去，最后 `Execute`；失败时把错误写到 stderr 并 `os.Exit(1)`。

挂载五个子命令的关键四行：

[cmd/gateway/main.go:23-29](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/main.go#L23-L29) —— 这就是「命令树」的根节点。注意顺序只是书写顺序，与路由无关。

根命令本身的定义：

[cmd/gateway/commands.go:72-83](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L72-L83) —— `Use: "gateway"`、`SilenceUsage/SilenceErrors: true`，`RunE` 返回 `cmd.Help()`，所以裸敲 `gateway` 等价于 `gateway --help`。

> 设计要点：把根命令做得「只会打印帮助」，强制用户必须显式选一个子命令。这避免了「无参数运行就意外启动控制面」这类误操作。

#### 4.1.4 代码实践

**实践目标**：亲眼看到这棵命令树，确认五个子命令都被正确注册。

**操作步骤**：

1. 按 u1-l3 的方法编译出二进制：`make build`（产物在 `build/out/gateway`）。
2. 不带任何参数运行：`./build/out/gateway`。
3. 再运行：`./build/out/gateway --help`。

**需要观察的现象**：第 2、3 步的输出应当**完全一致**——都是一段帮助文本，其中 `Available Commands:` 一栏列出 `controller`、`generate-certs`、`initialize`、`sleep`、`endpoint-picker` 五项，每项后面带一句 `Short` 说明。

**预期结果**：你看到与下面类似的列表（节选）：

```text
Available Commands:
  controller      Run the NGINX Gateway Fabric control plane
  endpoint-picker Shim server for communication between NGINX and the Gateway API Inference Extension Endpoint Picker
  generate-certs  Generate self-signed certificates for securing control plane to data plane communication
  initialize      Write initial configuration files
  sleep           Sleep for specified duration and exit
```

> 提示：若 `make build` 在 macOS 上报平台错误，参考 u1-l3 覆盖 `GOOS`/`GOARCH`；若暂时无法本地编译，可直接对照 `commands.go` 中每个 `createXxxCommand` 的 `Short` 字段阅读，效果等同。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `main` 里把 `SilenceUsage` 设成 `true`？如果设成 `false` 会怎样？

**参考答案**：`SilenceUsage: true` 让 cobra 在命令返回错误时**不要**再打印一大段用法说明（usage 只在「用法本身错了」时才有价值）。设成 `false` 时，任意运行期错误（例如连不上集群）后面都会跟一长串 flag 帮助，淹没真正的错误信息，不利于在容器日志里排障。

**练习 2**：根命令的 `RunE` 返回 `cmd.Help()`，这个返回值会如何被 cobra 处理？

**参考答案**：`Help()` 内部把帮助文本写到 stdout 并返回 `nil`，所以 `RunE` 返回 `nil`，cobra 视为「成功」，进程以退出码 0 结束——这正是「不带子命令 = 看帮助」的预期行为，而非报错退出。

---

### 4.2 五个子命令的职责

#### 4.2.1 概念说明

五个子命令里，**只有 `controller` 是「真正的控制面主进程」**，它长驻运行、watch 资源、下发配置。其余四个都是「一次性辅助命令」：在特定时机被调用、干完即退。理解它们各自的责任，才能在看部署清单时明白「这一行 args 对应源码里哪段逻辑」。

下表给出全貌（运行位置的来源在 4.2.3 详解）：

| 子命令 | 处理函数入口 | 一句话职责 | 运行位置 |
| --- | --- | --- | --- |
| `controller` | `createControllerCommand` 的 `RunE`（调用 `controller.StartManager`） | 装配并运行控制面 | 控制面 Deployment 的 `nginx-gateway` 容器 |
| `generate-certs` | `createGenerateCertsCommand` 的 `RunE` | 自签 CA/服务端/客户端证书，写回两个 Secret | Helm pre-install/pre-upgrade 的 cert-generator **Job** |
| `initialize` | `createInitializeCommand` 的 `RunE` → `initialize()` | 拷贝初始配置文件；Plus 下生成 deployment context | 数据面 Pod 的 **init 容器** |
| `sleep` | `createSleepCommand` 的 `Run` | 睡眠指定时长后退出 | 数据面容器的 lifecycle `preStop` 钩子 |
| `endpoint-picker` | `createEndpointPickerCommand` 的 `RunE` | 本地 HTTP shim，转发请求给推理扩展 EPP | 数据面 Pod 的 **sidecar 容器**（仅启用推理扩展时） |

#### 4.2.2 核心流程

**`controller`（主命令）** 的 `RunE` 是五个里最重的，流程如下：

1. 初始化 zap 日志、`klog.SetLogger`，打印启动横幅（含 `version`、commit、date）。
2. 一系列运行期校验：端口冲突检查（`ensureNoPortCollisions`）、解析 `telemetryReportPeriod` 为 `time.Duration`、校验遥测 endpoint、按需构建 usage report 配置。
3. 把所有 flag 的值汇聚成一个 `config.Config` 结构（metrics/health/leader election/telemetry/Plus/flags 等都在这里）。
4. 调用 `controller.StartManager(conf)`，把配置交给控制面 Manager 启动事件循环（这一步是 u3 的主题）。

**`generate-certs`（辅助）**：从 `POD_NAMESPACE` 取命名空间 → `generateCertificates(...)` 生成 CA + 服务端 + 客户端证书 → 建 k8s client → `createSecrets(...)` 把证书写入 `server-tls` 与 `agent-tls` 两个 Secret（支持 `--overwrite`）。

**`initialize`（辅助）**：校验 `--source/--destination` 参数对 → 从环境变量取 `POD_UID`、`CLUSTER_UID` → 把若干源文件拷贝到目标目录；若 `--nginx-plus`，再生成一份 deployment context 文件（用于 Plus 授权/用量上报）。

**`sleep`（辅助）**：极其简单——`time.Sleep(duration)` 然后退出。源码里有一段 FIXME 注释说明：一旦 Kubernetes 原生支持 lifecycle 的 `sleep` action，这个子命令就会被移除。

**`endpoint-picker`（辅助）**：构造一个 HTTP handler（`createEndpointPickerHandler`），用 `endpointPickerServer` 在本地 `127.0.0.1:<GoShimPort>` 起服务，把收到的请求通过 gRPC ext_proc 协议转发给真正的 EndpointPicker（EPP），再把 EPP 选出的后端 endpoint 回写给 NGINX。

#### 4.2.3 源码精读

**`controller` 命令的定义与跳转点**

[cmd/gateway/commands.go:233-237](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L233-L237) —— 子命令名 `controller`，`Short` 明确写着 "Run the NGINX Gateway Fabric control plane"。

[cmd/gateway/commands.go:239-356](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L239-L356) —— `controller` 的 `RunE` 主体：日志初始化、端口与遥测校验、构建 `config.Config`。

[cmd/gateway/commands.go:290-349](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L290-L349) —— 把全部 flag 值塞进 `config.Config{ ... }`，这是「flag → Config」的汇聚点。

[cmd/gateway/commands.go:351-353](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L351-L353) —— **最关键的跳转**：`controller.StartManager(conf)`。命令行入口到此把控制权交给控制面 Manager，后续 u3 全在这里展开。

[cmd/gateway/commands.go:364-371](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L364-L371) —— `gateway-ctlr-name` 与 `gatewayclass` 被标为**必填**（`MarkFlagRequired`），印证 u1-l1 讲的「控制器名必须与 GatewayClass 的 controllerName 一致才会被认领」。

**`generate-certs` 命令**

[cmd/gateway/commands.go:719-787](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L719-L787) —— 命令定义与 `RunE`。

[cmd/gateway/commands.go:763-783](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L763-L783) —— 调用 `generateCertificates(...)` 造证书，再 `createSecrets(...)` 写回 Secret。

[cmd/gateway/certs.go:48-76](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/certs.go#L48-L76) —— `generateCertificates` 的真正实现：先生成 CA，再用 CA 分别签服务端证书（带服务 DNS 名）和客户端证书（带 `*.cluster.local`）。

**`initialize` 命令**

[cmd/gateway/commands.go:834-891](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L834-L891) —— 命令定义与 `RunE`，最终调用 `initialize(initializeConfig{...})`。

[cmd/gateway/initialize.go:34-64](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/initialize.go#L34-L64) —— `initialize` 函数：先循环拷贝每个 `--source` 到对应 `--destination`；若不是 Plus 直接结束，若是 Plus 则再生成 deployment context 文件。

**`sleep` 命令（含 FIXME）**

[cmd/gateway/commands.go:919-948](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L919-L948) —— 顶部 FIXME 注释说明它服务于 lifecycle hook、待 K8s 原生 sleep action 后移除；`Run`（注意不是 `RunE`）就是一句 `time.Sleep(duration)`。

**`endpoint-picker` 命令**

[cmd/gateway/commands.go:950-969](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L950-L969) —— 命令定义：构造 handler 并启动 shim 服务。

[cmd/gateway/endpoint_picker.go:28-35](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/endpoint_picker.go#L28-L35) —— `endpointPickerServer` 监听 `127.0.0.1:<GoShimPort>`，只对本机可见（NGINX 与它在同一 Pod 内）。

**「运行在哪个容器」的硬证据（部署清单）**

`controller` 运行在控制面容器——Helm 模板里 `nginx-gateway` 容器的第一条 args 就是 `controller`：

[charts/nginx-gateway-fabric/templates/deployment.yaml:42-48](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/charts/nginx-gateway-fabric/templates/deployment.yaml#L42-L48) —— `args:` 第一行 `controller`，后面跟着一堆 `--gateway-ctlr-name`、`--gatewayclass` 等 flag。

`generate-certs` 运行在一个 **Job**（注意不是 init 容器），靠 Helm hook 在安装前触发：

[charts/nginx-gateway-fabric/templates/certs-job.yaml:105-144](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/charts/nginx-gateway-fabric/templates/certs-job.yaml#L105-L144) —— `kind: Job`，`helm.sh/hook: pre-install, pre-upgrade`，容器 args 第一行是 `generate-certs`。

`initialize` 运行在**数据面 Pod 的 init 容器**（数据面 Pod 由 provisioner 动态创建，u9 详讲）：

[internal/controller/provisioner/objects.go:1274-1288](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/objects.go#L1274-L1288) —— init 容器名为 `"init"`，命令是 `/usr/bin/gateway initialize --source ... --destination ...`（多对源/目标）。

`endpoint-picker` 运行在数据面 Pod 的 **sidecar 容器**：

[internal/controller/provisioner/objects.go:1532-1537](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/objects.go#L1532-L1537) —— sidecar 命令是 `/usr/bin/gateway endpoint-picker`，按需追加 TLS 相关 flag。

`sleep` 运行在数据面容器的 lifecycle `preStop` 钩子。源码 FIXME 已说明用途；chart 的 `values.yaml` 也把它作为 lifecycle 钩子的典型示例（数据面容器一段）：

[charts/nginx-gateway-fabric/values.yaml:752-764](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/charts/nginx-gateway-fabric/values.yaml#L752-L764) —— 注释里给出 `preStop` + `sleep` 的用法示例。

> 重要区分：`generate-certs` 是 **Job**，`initialize` 才是 **init 容器**，两者都「跑一次就退出」，但生命周期完全不同——Job 是独立的 Kubernetes 资源、在控制面 Deployment 创建**之前**就把 Secret 准备好；init 容器则在数据面 Pod **内部**、主容器启动前运行。别把它们混为一谈。

#### 4.2.4 代码实践

**实践目标**：本讲的核心实践——在源码里标注每个子命令的处理函数入口，并判定各自运行的容器。

**操作步骤（源码阅读型实践，无需运行集群）**：

1. 打开 `cmd/gateway/commands.go`，定位下表左侧的 `createXxxCommand` 函数，在每个命令的 `RunE`/`Run` 处各加一条「脑内标注」（不要改源码，只是阅读）：

   | 子命令 | 处理函数入口（commands.go） | 进一步实现 |
   | --- | --- | --- |
   | `controller` | `RunE` 内调用 `controller.StartManager(conf)`（约 L351） | `internal/controller/manager.go`（u3） |
   | `generate-certs` | `RunE` 内调用 `generateCertificates` + `createSecrets`（约 L763、L773） | `cmd/gateway/certs.go` |
   | `initialize` | `RunE` 内调用 `initialize(...)`（约 L881） | `cmd/gateway/initialize.go` |
   | `sleep` | `Run` 内 `time.Sleep(duration)`（约 L936） | 就地，无外部实现 |
   | `endpoint-picker` | `RunE` 内调用 `endpointPickerServer(handler)`（约 L962） | `cmd/gateway/endpoint_picker.go` |

2. 打开 `charts/nginx-gateway-fabric/templates/deployment.yaml`，找到 `args: - controller`，确认控制面容器只跑 `controller`。
3. 打开 `charts/nginx-gateway-fabric/templates/certs-job.yaml`，确认 `generate-certs` 出现在一个带 `helm.sh/hook: pre-install, pre-upgrade` 的 **Job** 里。
4. 打开 `internal/controller/provisioner/objects.go`，确认 init 容器跑 `initialize`、sidecar 跑 `endpoint-picker`。

**需要观察的现象 / 预期结果**：你应该能填出下面这张「子命令 → 运行位置」映射（这是本实践的产出）：

| 子命令 | 运行位置 | 触发时机 |
| --- | --- | --- |
| `controller` | 控制面 Deployment 的 `nginx-gateway` 容器 | 控制面常驻 |
| `generate-certs` | 独立的 cert-generator **Job** | Helm install/upgrade **之前** |
| `initialize` | 数据面 Pod 的 **init 容器** | 数据面主容器启动**前**，跑一次 |
| `sleep` | 数据面容器的 lifecycle **preStop 钩子** | 数据面容器被终止**前** |
| `endpoint-picker` | 数据面 Pod 的 **sidecar 容器** | 启用推理扩展时常驻 |

> 说明：`initialize` 与 `endpoint-picker` 所在的数据面 Pod 是由 **provisioner 动态创建**的（每个 Gateway 对应一组数据面资源），其完整生命周期是 u9 的主题；本讲只需知道「它们不在控制面 Pod 里」即可。

#### 4.2.5 小练习与答案

**练习 1**：`generate-certs` 写出的两个 Secret 分别叫什么？各自被谁使用？

**参考答案**：默认名为 `server-tls` 与 `agent-tls`（见 `commands.go` 顶部常量 `serverTLSSecret`、`agentTLSSecret`）。`server-tls` 装的是控制面 gRPC 服务端的 CA/证书/私钥，供控制面对数据面 Agent 暴露 mTLS 服务端；`agent-tls` 装的是 Agent 作为客户端的 CA/证书/私钥，供 NGINX Agent 反向连控制面时做客户端认证。两者共享同一份 CA。

**练习 2**：为什么 `sleep` 是用 `Run` 而不是 `RunE`？

**参考答案**：`sleep` 永远「成功」（睡眠本身不会产生需要上报的错误），所以用不需要返回 error 的 `Run` 字段；用 `RunE` 则必须返回一个 error，这里没有意义。这也呼应了它在源码注释里「只是 lifecycle 钩子的一个临时占位」的定位。

**练习 3**：`endpoint-picker` 的 shim 服务监听地址是 `127.0.0.1`，为什么不用 `0.0.0.0`？

**参考答案**：因为它和 NGINX 跑在**同一个 Pod**里，只需对本机 NGINX 可见；监听 `127.0.0.1` 可以避免把这个内部 shim 暴露成 Pod 外可达的端口，缩小攻击面。

---

### 4.3 构建期注入的变量（version / telemetry）

#### 4.3.1 概念说明

`cmd/gateway/main.go` 顶部声明了四个「空」的包级变量：`version`、`telemetryReportPeriod`、`telemetryEndpoint`、`telemetryEndpointInsecure`。它们在 Go 源码里没有初始值，真正的值是在 `go build` 阶段由 `-ldflags -X main.<变量名>=<值>` 注入的（u1-l3 讲过 Makefile 用 `-ldflags -X main.version=...`）。这种做法把「与构建环境相关、每次发版才变」的信息（版本号、遥测上报周期与地址）从运行时配置中剥离，让同一份配置在不同构建产物里自动带上正确的版本/遥测行为。

为什么要这样？因为：

- `version` 要随 release/edge 不同而变，却需要被控制面日志、遥测数据携带，不宜写成运行时 flag。
- 遥测上报周期与地址属于「产品发布策略」，官方镜像统一即可，不该让每个部署随意改。

#### 4.3.2 核心流程

注入与消费的链路是：

```text
Makefile (-ldflags -X main.version=... -X main.telemetryReportPeriod=...)
        │  编译期写入
        ▼
main.go 顶部的四个包级变量 (version, telemetryReportPeriod, telemetryEndpoint, telemetryEndpointInsecure)
        │  运行时读取
        ▼
controller.RunE：
  - version  → 启动日志横幅 + config.Config（再传给 telemetry/usage）
  - telemetryReportPeriod → time.ParseDuration → config.ProductTelemetryConfig.ReportPeriod
  - telemetryEndpoint(_Insecure) → 校验/ParseBool → config.ProductTelemetryConfig.Endpoint(_Insecure)
```

注意 `version` 还会与 Go 的 `runtime/debug` 构建信息配合：`getBuildInfo()` 从 `debug.ReadBuildInfo()` 读取 `vcs.revision`、`vcs.time`、`vcs.modified`，连同注入的 `version` 一起打印成启动横幅，方便从日志反查是哪个 commit 构建的。

#### 4.3.3 源码精读

[cmd/gateway/main.go:8-18](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/main.go#L8-L18) —— 四个变量的声明，注释 `// Set during go build.` 点明了它们的赋值时机。

`controller` 的 `RunE` 如何消费它们：

[cmd/gateway/commands.go:245-253](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L245-L253) —— 启动横幅：把注入的 `version` 与从构建信息取出的 commit/date/dirty 一起打印。

[cmd/gateway/commands.go:264-278](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L264-L278) —— 把字符串形式的 `telemetryReportPeriod` 解析成 `time.Duration`，把 `telemetryEndpoint` 做校验，把 `telemetryEndpointInsecure` 用 `strconv.ParseBool` 解析。

[cmd/gateway/commands.go:319-324](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L319-L324) —— 三组遥测值最终汇入 `config.ProductTelemetryConfig`，`version` 则经由 `config.Config` 透传（见 L292 的 `createGatewayPodConfig(version, ...)` 与 L1067 的 `Version: version`）。

构建信息读取（与 `version` 互补，用于日志可追溯）：

[cmd/gateway/commands.go:1012-1033](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L1012-L1033) —— `getBuildInfo` 从 `debug.ReadBuildInfo()` 的 `Settings` 里挑出 `vcs.revision`、`vcs.time`、`vcs.modified`，读不到就回退为 `"unknown"`。

> 边界情况：本地直接 `go build ./cmd/gateway`（不经 Makefile 的 ldflags）时，`version` 会是空字符串、三组遥测变量也为空，于是 `time.ParseDuration("")` 会报错——这就是为什么 u1-l3 强调要走 `make build` 而不是裸 `go build`。

#### 4.3.4 代码实践

**实践目标**：验证「构建期注入」确实发生，并观察不注入时的行为差异。

**操作步骤**：

1. 走 `make build`（带 ldflags 注入）编译，运行 `./build/out/gateway controller --help` 不需要——直接看启动日志需要集群，故改用如下静态对照。
2. **对照编译**（不注入变量）：`go build -o /tmp/gateway-bare ./cmd/gateway`。
3. 准备最小环境后运行裸二进制：`POD_UID=x POD_NAMESPACE=x POD_NAME=x INSTANCE_NAME=x IMAGE_NAME=x /tmp/gateway-bare controller --gateway-ctlr-name=gateway.nginx.org/nginx-gateway-controller --gatewayclass=nginx`（**待本地验证**：缺少集群连接会在更后步骤失败，但我们要观察的是更早的遥测参数解析）。

**需要观察的现象**：

- 正常 `make build` 产物：启动横幅里 `version` 是一个真实值（本地为 `edge`）。
- 裸 `go build` 产物：由于 `telemetryReportPeriod` 为空，`time.ParseDuration("")` 返回错误，`RunE` 立即返回 `error parsing telemetry report period: ...`，进程直接退出，**根本走不到连集群那一步**。

**预期结果**：你将直观看到「这四个变量不是可选的运行时配置，而是构建期必须注入的依赖」——裸编译的二进制连控制面都启动不了。这也解释了为什么 Makefile 要在 ldflags 里同时设置 `version` 与三组遥测变量。

> 提示：若不想真跑二进制，纯阅读型做法是——在 `commands.go` 的 L264-L278 处确认：`telemetryReportPeriod` 来自包级变量、且无任何默认值兜底；由此推断「不注入即失败」。结论一致。

#### 4.3.5 小练习与答案

**练习 1**：`version` 变量除了写进启动日志，还会被带到哪里？

**参考答案**：`version` 经 `createGatewayPodConfig(version, ...)` 进入 `config.GatewayPodConfig.Version`（commands.go 约 L292、L1067），随后随 `config.Config` 传入 `StartManager`，最终被产品遥测（telemetry）与用量上报（usage report）携带，用于上报「这个控制面是什么版本」。

**练习 2**：为什么不直接把 `telemetryReportPeriod` 做成一个普通运行时 flag？

**参考答案**：这是产品发布策略层面的决定——遥测上报周期与地址希望由官方镜像统一控制（不同构建：release vs edge 可能不同），不希望被每个部署随意修改。用 ldflags 在构建期注入，既能让官方产物自带正确值，又避免了「用户改坏遥测参数」的风险。它和 `version` 一样属于「构建产物属性」，而非「部署配置」。

**练习 3**：`getBuildInfo()` 与 `-ldflags -X main.version` 都能给出版本信息，二者关系是什么？

**参考答案**：`-X main.version` 注入的是「产品版本号」（如 `1.4.6` 或 `edge`），由 Makefile 显式设置；`getBuildInfo()` 从 Go 内置的 `debug.ReadBuildInfo()` 读取的是**源码版本信息**（commit hash、提交时间、是否 dirty），由 git 自动嵌入。两者互补：版本号便于人读，commit/time/dirty 便于精确追溯。读不到构建信息时 `getBuildInfo` 回退为 `"unknown"`，但 `version` 若未被 ldflags 注入则会是空串，进而导致遥测解析失败。

---

## 5. 综合实践

把本讲三个模块串起来，完成一张「NGF 命令行全景图」。

**任务**：制作一张可供日后查阅的「子命令速查表 + 调用链图」，要求：

1. **命令树**：画出 `gateway` 根命令到五个子命令的树形结构（参照 4.1）。
2. **入口标注**：为每个子命令写明「定义于 commands.go 的哪个函数」「RunE/Run 里调用的关键函数」「真正的实现文件」。例如 `controller` → `createControllerCommand` → `controller.StartManager(conf)` → `internal/controller/manager.go`。
3. **运行位置**：用三类标记区分运行位置——控制面容器、cert-generator Job、数据面 Pod（再细分 init 容器 / sidecar / lifecycle 钩子）。
4. **构建期依赖**：在 `controller` 一栏旁注「依赖 `version` / 三组 `telemetry*`，由 Makefile ldflags 注入；裸 go build 会启动失败」。

**建议产出形式**：一张 Markdown 表格 + 一段不超过 10 行的文字小结。完成后，你应当能不看源码回答：「NGF 部署后，`initialize` 命令第一次是在哪里、什么时候被执行的？」（答案：在 provisioner 为某 Gateway 创建的数据面 Pod 的 init 容器里，于 NGINX 主容器启动前执行一次。）

## 6. 本讲小结

- NGF 只有**一个二进制** `gateway`，用 cobra 组装成「根命令 + 五个子命令」的命令树；根命令本身只打印帮助。
- **`controller` 是唯一的控制面主进程**，其 `RunE` 把全部 flag 汇聚成 `config.Config`，再调用 `controller.StartManager(conf)` 交棒给 Manager（u3 主题）。
- 其余四个是**一次性辅助命令**：`generate-certs`（自签证书）、`initialize`（数据面初始化）、`sleep`（preStop 钩子占位）、`endpoint-picker`（推理扩展 shim）。
- 「跑在哪」各不相同：`controller` 在控制面 `nginx-gateway` 容器；`generate-certs` 在 Helm pre-install 的 **Job**；`initialize` 在数据面 Pod 的 **init 容器**；`endpoint-picker` 在数据面 **sidecar**；`sleep` 在数据面容器的 **lifecycle 钩子**。
- `version` 与 `telemetryReportPeriod/Endpoint/EndpointInsecure` 是**构建期注入**的包级变量（ldflags `-X`），不经 Makefile 注入会导致 `controller` 启动失败。
- `getBuildInfo()` 用 `debug.ReadBuildInfo()` 补充 commit/date/dirty，与注入的 `version` 一起构成启动横幅，便于从日志追溯构建来源。

## 7. 下一步学习建议

- **下一讲 u2-l2（controller 命令与运行时配置）**：本讲只是点到了 `controller` 的 flag 汇聚成 `config.Config`；下一讲会逐组拆解这些 flag，看清「flag → Config → StartManager」的完整数据流。
- **u2-l3（初始化命令与证书生成）**：想深入 `generate-certs` 的证书矩阵与 `initialize` 的 Plus deployment context，直接进这一讲。
- **u2-l4（参数校验与准入式校验机制）**：本讲多次出现 `stringValidatingValue` 等 pflag Value 类型，其设计在 u2-l4 系统讲解。
- **延伸阅读源码**：想提前感受控制面装配，可跳读 `internal/controller/manager.go` 的 `StartManager`——那是 `controller` 命令 `RunE` 的终点，也是 u3 全部内容的起点。
