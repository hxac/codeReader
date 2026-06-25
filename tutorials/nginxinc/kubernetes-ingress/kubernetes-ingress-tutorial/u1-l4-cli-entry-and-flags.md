# 命令行入口与启动 flags

## 1. 本讲目标

学完本讲后，你应当能够：

- 从 `cmd/nginx-ingress/main.go` 的 `main()` 开始，**按顺序**说出程序启动时依次做了哪些事（打印版本 → 解析 flags → 初始化日志 → 校验 → 建客户端 → 组装控制器 → 运行）。
- 理解版本号、git commit、遥测端点等「构建信息」是如何在编译期注入、在运行期读取的。
- 看懂 `flags.go` 里的三层校验链：`initValidate → mustValidateInitialChecks / mustValidateWatchedNamespaces / mustValidateFlags`，并能区分「警告并回退」与「直接 `Fatal` 退出」两类校验。
- 认识与 NGINX 配置路径、Ingress 类、Leader 选举相关的核心 flag（如 `-nginx-configmaps`、`-mgmt-configmap`、`-ingress-class`、`-enable-leader-election`、`-leader-election-lock-name`）。

本讲只聚焦「**程序如何启动、如何解析参数、如何初始化**」这一条链路，不展开控制器内部循环、配置生成与 NGINX 进程管理——它们有各自的后续讲义（u3、u4、u5）。

## 2. 前置知识

阅读本讲前，建议你已经具备以下认知（来自 u1-l1 ~ u1-l3）：

- **控制器范式**：NIC 是一个 watch → 发现差异 → 让实际逼近期望的控制器（reconcile），而不是一次性脚本。
- **五层架构**：数据模型 → 校验 → 控制器 → 配置生成 → 进程管理，职责单向流动。
- **目录布局**：`cmd/` 是入口、`internal/` 是私有实现（`internal/k8s`、`internal/configs`、`internal/nginx`）、`pkg/` 含对外 API 与生成代码。
- **构建方式**（u1-l2）：必须用 `make build` 而非裸 `go build`，因为版本号通过 `-ldflags -X` 在编译期注入。

下面补充几个本讲会用到的 Go 与 client-go 基础概念：

| 概念 | 通俗解释 |
| --- | --- |
| `flag` 包 | Go 标准库，用来解析命令行参数（`-foo bar` 或 `-foo=bar`）。每个 `flag.String/Bool/Int(...)` 会注册一个全局变量并自动生成 `-h` 帮助。 |
| `os.Exit(0)` | 立即退出进程，0 表示成功。`flag.Parse()` 后遇到 `-version` 就走这条路。 |
| `log/slog` | Go 1.21+ 的结构化日志库，支持 JSON/文本 handler 与自定义日志级别。 |
| `context.Context` | 贯穿请求/进程的上下文，这里被用来**携带 logger**（`LoggerFromContext`）。 |
| `InClusterConfig` | client-go 在「Pod 内部运行」时自动读取 ServiceAccount 凭据、连接集群 API 的方式。 |
| ldflags `-X` | 链接期把字符串注入到指定包变量，例如 `-X main.version=5.6.0`。 |

## 3. 本讲源码地图

本讲围绕 `cmd/nginx-ingress/` 下的三个文件展开：

| 文件 | 作用 |
| --- | --- |
| [cmd/nginx-ingress/main.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/main.go) | 程序入口。`main()` 编排整个启动序列；同时定义了被 ldflags 注入的 `version`、`telemetryEndpoint`，以及 `initLogger` 日志初始化。 |
| [cmd/nginx-ingress/flags.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/flags.go) | 所有命令行 flag 的定义、`parseFlags()` 解析入口，以及三层启动校验链与各类 `validate*` 辅助函数。 |
| [cmd/nginx-ingress/utils.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/utils.go) | `getBuildInfo()`，通过 `runtime/debug.ReadBuildInfo()` 读取 VCS 信息（commit、时间、是否 dirty）。 |

此外会引用 `Makefile`（ldflags 注入）、`.github/data/version.txt`（版本来源）与 `internal/logger/levels/levels.go`（日志级别常量）作为佐证。

---

## 4. 核心概念与源码讲解

### 4.1 入口 main 函数：启动序列全景

#### 4.1.1 概念说明

任何 Go 程序都从 `package main` 里的 `func main()` 开始。NIC 的 `main()` 是一个**线性、自顶向下的启动编排函数**：它不包含控制器循环本身，而是负责把控制器运行所需的「一切依赖」按顺序准备好，最后才调用 `lbc.Run()` 把控制权交给控制器循环。

可以把 `main()` 理解成「装配车间」：它把客户端、事件记录器、NGINX 管理器、配置生成器、模板执行器、校验器、指标采集器等零件一一装好，组装成一台 `LoadBalancerController`，然后按下启动按钮。

#### 4.1.2 核心流程

`main()` 的启动序列大致分这几个阶段（后续小节会逐段精读前四阶段，其余留给后续讲义）：

```text
1. 构建信息   getBuildInfo() → 打印版本横幅（Version/Commit/Date/Arch/Go）
2. 解析 flags  parseFlags()           ← 见 4.2
3. 初始化日志  initLogger() → 从 ctx 取 logger ← 见 4.4
4. 清理/校验   cleanupSocketFiles() → initValidate() ← 见 4.3
5. 读环境变量  BUILD_OS / POD_NAMESPACE / POD_NAME
6. 建 K8s 客户端 mustCreateConfigAndKubeClient() → 校验 K8s 版本(≥1.22) → 取 Pod
7. 事件记录器  eventBroadcaster + eventRecorder（用于把事件写回 K8s）
8. 校验 IngressClass  mustValidateIngressClass() → checkNamespaces()
9. 建自定义客户端 createCustomClients()（dynamic / k8s.nginx.org typed client）
10. 指标采集器  createManagerAndControllerCollectors()
11. NGINX 管理器 createNginxManager() → 取 NGINX 版本
12. Plus/WAF/Agent 处理（条件分支，依赖 flag）
13. 模板执行器  createTemplateExecutors()（v1 + v2，OSS/Plus 分支）
14. Secret 处理  default-server / wildcard / license / trusted-cert / client-auth
15. 全局配置    mustProcessGlobalConfiguration() → processConfigMaps()
16. StaticConfigParams 构造 → 写初始 NGINX 配置 mustWriteInitialNginxConfig()
17. 启动子进程  startChildProcesses()（nginx / WAF plugin / DoS agent / agent）
18. Plus 客户端 + 指标采集器 createPlusClient() / createPlusAndLatencyCollectors()
19. 组装配置生成器 configs.NewConfigurator(...)
20. 组装校验器  transportServerValidator / virtualServerValidator
21. 组装控制器输入 lbcInput := k8s.NewLoadBalancerControllerInput{...}
22. NewLoadBalancerController(lbcInput)
23. （可选）ready 端点  /nginx-ready
24. handleTermination 协程（监听 SIGTERM）
25. lbc.Run()   ← 正式进入控制器循环
26. for{} 等待退出
```

注意：第 25 步 `lbc.Run()` 是**阻塞**的，控制器循环就在这里运行；只有收到终止信号时 `handleTermination` 才会调用 `lbc.Stop()` 让它返回。

#### 4.1.3 源码精读

入口与版本横幅——先取构建信息，再用 `fmt.Printf` 打印一行版本：

[cmd/nginx-ingress/main.go#L84-L91](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/main.go#L84-L91) 启动序列的头四步：取构建信息 → 打印版本 → 解析 flags → 初始化日志。

关键点：版本信息在**解析 flags 之前**就打印了，这意味着即使 flags 写错，你也至少能看到一行版本号，便于排查「为什么这个 Pod 跑的是旧版本」。

构造控制器的最后阶段——组装 `lbcInput` 并启动：

[cmd/nginx-ingress/main.go#L348-L366](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/main.go#L348-L366) 创建控制器、起 ready 端点、注册终止处理、调用 `lbc.Run()` 后进入等待循环。

这里的 `parsedFlags := os.Args[1:]`（[main.go#L94](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/main.go#L94)）会把原始命令行参数原样塞进 `lbcInput.InstallationFlags`，供控制器后续做遥测/诊断时上报「这个实例是用哪些 flag 启动的」。

> 说明：本讲聚焦启动链路本身。第 6~20 步里的「建客户端、写初始配置、组装 Configurator」属于控制器（u3）与配置生成（u4）的内容，这里只确认它们在 `main()` 里的**顺序与位置**。

#### 4.1.4 代码实践

**实践目标**：在源码层面建立 `main()` 的「启动时序地图」。

**操作步骤**：

1. 打开 [cmd/nginx-ingress/main.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/main.go)，定位到 `func main()`（L84）。
2. 从 L84 一直读到 L367，把每个「顶层语句」按上面的 1~26 步编号标注。
3. 特别留意哪些步骤是**条件分支**（如 `if *nginxPlus { ... }`、`if *appProtect { ... }`），这些是「变体行为」的来源。

**需要观察的现象**：

- `main()` 里几乎每个失败路径都调用 `nl.Fatalf` / `logEventAndExit`（即 `os.Exit`），说明启动期奉行「**fail fast**」：任何一个前置依赖失败就直接退出，绝不在半残状态下进入控制器循环。
- 第 12、14 步大量依赖 flag（`*nginxPlus`、`*appProtect`、`*defaultServerSecret`…），这些 flag 的值在第 2 步 `parseFlags()` 之后才可用。

**预期结果**：你能画出一张从 L84 到 L361 的线性流程图，并指出 `lbc.Run()` 是控制权交接点。

#### 4.1.5 小练习与答案

**练习 1**：如果 `parseFlags()` 之后、`lbc.Run()` 之前抛出 panic，控制器循环会启动吗？

> **参考答案**：不会。`lbc.Run()`（L361）之前的任何 panic 都会让 `main` 直接崩溃，控制器循环根本没机会运行。这就是为什么启动期的 `Fatal`/`logEventAndExit` 都是 `os.Exit`——它们要在进入循环前把问题挡住。

**练习 2**：版本横幅为什么要在 `parseFlags()` **之前**打印？

> **参考答案**：为了让即使 flags 配置错误导致进程立刻退出，运维也能从日志第一行确认「跑的是哪个版本、哪个 commit」。这是一种可观测性兜底。

---

### 4.2 flag 定义与解析

#### 4.2.1 概念说明

NIC 用 Go 标准库的 `flag` 包来定义和解析命令行参数。`flags.go` 顶部的 `var(...)` 块里，每一行 `flag.String/Bool/Int("name", default, "usage")` 都做了三件事：

1. **注册**一个命令行 flag（名字、默认值、帮助文本）。
2. 返回一个**指向值的指针**（如 `*nginxPlus`），全程序通过解引用来读取该 flag 的当前值。
3. 该 flag 会自动出现在 `-h` 的帮助输出里。

> 小贴士：`flag` 包里 `-h` / `-help` 是内置的；只要调用了 `flag.Parse()`，遇到 `-h` 就会打印所有已注册 flag 的 usage 并退出。

NIC 有上百个 flag，但它们可以归为几大家族：

| 家族 | 代表 flag | 作用 |
| --- | --- | --- |
| NGINX 配置来源 | `-nginx-configmaps`、`-mgmt-configmap` | 指定承载全局 NGINX 配置的 ConfigMap |
| 路由/类 | `-ingress-class`、`-enable-custom-resources`、`-enable-tls-passthrough` | 决定控制器处理哪些资源 |
| 状态回写/选举 | `-report-ingress-status`、`-enable-leader-election`、`-leader-election-lock-name`、`-external-service` | 多副本下谁来写 status、用哪把锁 |
| TLS/Secret | `-default-server-tls-secret`、`-wildcard-tls-secret` | 默认/wildcard 证书来源 |
| 变体能力 | `-nginx-plus`、`-enable-app-protect`、`-enable-app-protect-dos`、`-agent` | 启用商业版/附加能力 |
| 可观测性 | `-enable-prometheus-metrics`、`-prometheus-metrics-listen-port`、`-nginx-status`、`-nginx-status-port` | 指标/状态暴露 |
| 日志 | `-log-format`、`-log-level` | 日志格式与级别 |
| 健康/就绪 | `-health-status`、`-health-status-uri`、`-ready-status`、`-ready-status-port` | 探针端点 |

#### 4.2.2 核心流程

```text
flag.String/Bool/Int(...)   # 启动期：注册所有 flag（包级 var 块，import 时执行）
        │
        ▼
parseFlags()                # main() 第 2 步调用
   ├── flag.Parse()         # 真正解析 os.Args，填充各指针
   └── if *versionFlag { os.Exit(0) }   # -version 只打印横幅后退出（横幅在 main 里已打印）
```

`flag.Parse()` 之后，所有 `*xxx` 指针才指向「命令行给的值或默认值」。在此之前读取它们会得到零值——这也是为什么版本横幅里只读 `version`（编译期注入的包变量），而不读任何 flag。

#### 4.2.3 源码精读

解析入口极其简短：

[cmd/nginx-ingress/flags.go#L245-L252](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/flags.go#L245-L252) `parseFlags()` 仅做 `flag.Parse()`，并在 `-version` 时直接退出。

注意 `//gocyclo:ignore` 注释——它告诉圈复杂度检查工具「下一个函数不要算复杂度」，因为紧随其后的 `initValidate` 分支极多。

与「NGINX 配置路径」直接相关的两个 flag：

[cmd/nginx-ingress/flags.go#L54-L62](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/flags.go#L54-L62) `-nginx-configmaps` 与 `-mgmt-configmap` 都要求 `<namespace>/<name>` 格式；取不到会让控制器启动失败。

> 区别：`-nginx-configmaps` 是数据面 NGINX 的全局配置（worker、http 块指令等）；`-mgmt-configmap` 是 NGINX **Plus** 的管理面配置（license、trusted cert），仅 Plus 可用（见 4.3 的校验）。

与「选举/状态」相关的 flag：

[cmd/nginx-ingress/flags.go#L130-L134](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/flags.go#L130-L134) `-enable-leader-election` 默认 `true`；`-leader-election-lock-name` 默认用 ConfigMap `nginx-ingress-leader-election` 作为锁。

> 重要细节：这里的选举锁是一个 **ConfigMap**（不是 Lease），用于「多副本中只有一个副本回写 status」，与 u3-l7 讲的 status 回写配套。`-ingress-class` 默认 `nginx`：

[cmd/nginx-ingress/flags.go#L87-L93](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/flags.go#L87-L93) 类名默认 `nginx`；必须存在同名 IngressClass，否则启动失败。

#### 4.2.4 代码实践

**实践目标**：用 `-h` 自助探索 flag 清单，并锁定「NGINX 配置路径 + 选举」相关 flag。

**操作步骤**：

1. 按 u1-l2 构建二进制：`make build`（产出 `nginx-ingress-<ARCH>`，例如 `nginx-ingress-amd64`）。
2. 运行帮助：

   ```bash
   ./nginx-ingress-amd64 -h 2>&1 | head -80
   ```

3. 在输出里找出下列三个 flag，并阅读其 usage 文本：
   - `-nginx-configmaps`
   - `-mgmt-configmap`
   - `-leader-election-lock-name`

**需要观察的现象**：

- `-h` 输出由 `flag` 包自动生成，**顺序按注册顺序**排列，每个 flag 都带默认值（如 `-enable-leader-election`（默认 true））。
- `-version` 会触发 `parseFlags()` 里的 `os.Exit(0)`，结合 `main` 里提前打印的横幅，效果是「打印版本后立刻退出」。

**预期结果**：你能用一句话说明这三个 flag 分别控制什么（见下方小练习答案）。

> 待本地验证：`-h` 的确切排版取决于本地 Go 版本与终端宽度；以上描述基于源码 flag 定义，实际渲染请以本地运行为准。本讲未在本环境实际执行构建与运行。

#### 4.2.5 小练习与答案

**练习 1**：用一句话分别说明 `-nginx-configmaps`、`-mgmt-configmap`、`-leader-election-lock-name` 控制什么。

> **参考答案**：
> - `-nginx-configmaps`：指向一个 `<ns>/<name>` 的 ConfigMap，里面写数据面 NGINX 的全局配置指令。
> - `-mgmt-configmap`：指向 NGINX **Plus** 的管理面 ConfigMap（license、trusted cert 等），仅 Plus 生效。
> - `-leader-election-lock-name`：多副本选举时用作锁的 ConfigMap 名称（默认 `nginx-ingress-leader-election`）。

**练习 2**：为什么 `parseFlags()` 里对 `-version` 要 `os.Exit(0)` 而不是返回？

> **参考答案**：因为版本横幅已经在 `main()` 里、`parseFlags()` 调用前打印过了；`-version` 的语义就是「打印版本即退出」，不进入后续任何启动步骤，所以必须立即退出进程。

---

### 4.3 启动校验链：分层校验与 fail-fast

#### 4.3.1 概念说明

NIC 的 flag 多、且彼此存在**依赖与互斥**关系（例如 App Protect 必须 Plus、TLS Passthrough 必须开自定义资源、`watch-namespace` 与 `watch-namespace-label` 不能同时给）。如果不在启动期校验，这些错误会潜伏到运行时才暴露，非常难排查。

因此 `flags.go` 设计了一条**三层校验链**，统一由 `initValidate(ctx)` 在 `main()` 第 4 步触发。校验结果分两种处理方式：

| 处理方式 | 含义 | 例子 |
| --- | --- | --- |
| `nl.Warnf(...)` + 回退 | 参数不合法但可降级：打个警告，把该 flag 改回安全默认值，继续启动 | `-log-level` 给错 → 回退 `info` |
| `nl.Fatal(...)` / `nl.Fatalf(...)` | 参数严重非法或依赖缺失：记录日志后 `os.Exit(1)`，拒绝启动 | `-enable-app-protect` 但没开 `-nginx-plus` |

> `Fatal` 本质是「记一条日志 + `os.Exit(1)`」。它在 `logger` 包里实现，是 fail-fast 的标准手段。

#### 4.3.2 核心流程

```text
initValidate(ctx)                              # 入口
   ├── validateLogFormat/validateLogLevel       # 软校验：非法 → Warn + 回退默认
   ├── 一组「依赖型」软校验                        # 例：latency→prometheus、service-insight→plus
   │     └── 不满足依赖 → Warn + 把 flag 置回 false/空
   ├── mustValidateInitialChecks(ctx)           # 启动检查(AWS)、打印 flags、忽略多余参数
   ├── mustValidateWatchedNamespaces(ctx)        # 命名空间互斥/格式校验
   └── mustValidateFlags(ctx)                    # 硬校验：端口、CIDR、location、跨 flag 依赖
                                                  #   任何一项非法 → Fatal 退出
```

「软校验」与「硬校验」的分界很重要：软校验优先发生，会**修正** flag 值；硬校验在后，只做**裁决**。

#### 4.3.3 源码精读

入口与软校验段：

[cmd/nginx-ingress/flags.go#L254-L289](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/flags.go#L254-L289) `initValidate`：先校验日志格式/级别并回退，再处理四组依赖型软校验，最后调三个 `mustValidate*`。

典型软校验（「延迟指标依赖 Prometheus」）：

[cmd/nginx-ingress/flags.go#L266-L269](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/flags.go#L266-L269) 开了 `-enable-latency-metrics` 却没开 `-enable-prometheus-metrics` → 警告并把延迟指标关掉。

初始检查（打印实际启动 flags，便于诊断）：

[cmd/nginx-ingress/flags.go#L291-L308](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/flags.go#L291-L308) `mustValidateInitialChecks` 会 `l.Info("Starting with flags: ...")`，把 `os.Args[1:]` 原样记进日志。

命名空间互斥硬校验：

[cmd/nginx-ingress/flags.go#L311-L346](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/flags.go#L311-L346) `-watch-namespace` 与 `-watch-namespace-label` 互斥；同时解析并校验命名空间名格式。

硬校验集中营——`mustValidateFlags`：

[cmd/nginx-ingress/flags.go#L351-L447](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/flags.go#L351-L447) 校验 URI/资源名/端口/CIDR，以及一组「跨 flag 依赖必须满足」的硬规则（如 App Protect/Dos 必须 Plus、TLS Passthrough/外部 DNS/cert-manager 必须开自定义资源、Plus 必须配 mgmt ConfigMap）。

其中一条与「NGINX 配置路径」强相关的硬规则：

[cmd/nginx-ingress/flags.go#L444-L446](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/flags.go#L444-L446) 开了 `-nginx-plus` 却没给 `-mgmt-configmap` → 直接 `Fatal`。这说明 `-mgmt-configmap` 对 Plus 是**强制**的。

辅助校验函数示例（端口、CIDR）：

[cmd/nginx-ingress/flags.go#L504-L532](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/flags.go#L504-L532) `parseNginxStatusAllowCIDRs` 把逗号分隔串拆成 CIDR/IP 数组，每个都过 `validateCIDRorIP`。

#### 4.3.4 代码实践

**实践目标**：通过「故意写错 flag」观察软校验与硬校验的不同表现（源码阅读型实践）。

**操作步骤**：

1. 在 [flags.go#L254-L289](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/flags.go#L254-L289) 找到「日志级别软校验」分支。
2. 假设启动命令里带了 `-log-level=xyz`（非法值）。预测 `validateLogLevel("xyz")` 会返回什么、`initValidate` 接下来会做什么。
3. 对照 [flags.go#L400-L402](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/flags.go#L400-L402) 的 App Protect 硬规则：假设 `-enable-app-protect=true` 但 `-nginx-plus=false`，预测进程命运。

**需要观察的现象**：

| 场景 | 校验类型 | 预期行为 |
| --- | --- | --- |
| `-log-level=xyz` | 软校验 | 打印 `Invalid log level...` 警告，回退为 `info`，**继续启动** |
| `-enable-app-protect` 未配 `-nginx-plus` | 硬校验 | 打印 `NGINX App Protect support is for NGINX Plus only` 后 `os.Exit(1)`，**拒绝启动** |

**预期结果**：你能清晰区分「软校验修正后继续」与「硬校验直接退出」两类行为，并能各举一个真实 flag 组合。

> 待本地验证：上表的「继续启动/拒绝启动」结论来自源码静态分析；如需亲见日志输出，可在本地用对应 flag 组合启动二进制验证。

#### 4.3.5 小练习与答案

**练习 1**：`initValidate` 里为什么把日志格式/级别做成「软校验」，而把 App Protect 必须 Plus 做成「硬校验」？

> **参考答案**：日志格式/级别错了不影响功能正确性，回退默认值即可继续安全运行；而 App Protect 依赖 Plus 二进制，没装 Plus 却开了它会导致运行期找不到组件、行为不可预期，属于根本性配置矛盾，必须在启动期拒绝。

**练习 2**：`mustValidateInitialChecks` 里 `l.Info("Starting with flags: ...")` 有什么运维价值？

> **参考答案**：它把实例**实际生效**的命令行参数（`os.Args[1:]`）记进启动日志，运维据此可判断「这个 Pod 到底是用什么参数起的」，对排查「行为与预期不符」非常关键——尤其是 flag 被软校验回退后，日志里仍是最初给的原始值。

---

### 4.4 构建信息注入与日志初始化

#### 4.4.1 概念说明

本模块回答两个问题：

1. **版本号、git commit、遥测端点从哪来？** —— 一部分（`version`、`telemetryEndpoint`）由 Makefile 在**编译期**通过 ldflags `-X` 注入；另一部分（commit、时间、是否 dirty）由 Go 工具链在构建时写入二进制，运行期用 `debug.ReadBuildInfo()` 读出。
2. **日志怎么初始化？** —— `initLogger()` 基于 `log/slog`，按 `-log-format` 选择 handler（glog/json/text），按 `-log-level` 设置级别，并把 logger 塞进 `context.Context` 供全程序取用。

#### 4.4.2 核心流程

构建期注入（Makefile）：

```text
.github/data/version.txt   IC_VERSION=5.6.0
        │  Makefile: VER = $(shell grep IC_VERSION ... )
        ▼
go build -trimpath -ldflags "-s -w -X main.version=5.6.0 -X main.telemetryEndpoint=oss.edge.df.f5.com:443"
        │
        ▼
二进制内的 main.version / main.telemetryEndpoint 被改写
```

运行期读取 VCS 信息（utils.go）：

```text
getBuildInfo()
   └── debug.ReadBuildInfo()   # 读出构建信息
        └── 遍历 info.Settings，取 vcs.revision / vcs.time / vcs.modified
```

日志初始化（main.go `initLogger`）：

```text
initLogger(logFormat, level, os.Stdout)
   ├── 构造 slog.HandlerOptions（AddSource=true，ReplaceAttr 改写 source/时间）
   ├── 按 logFormat 选 handler：glog | json* | text*
   ├── slog.New(h) → SetDefault(l)
   ├── programLevel.Set(level)   # 应用 -log-level
   └── return ContextWithLogger(ctx, l)
```

#### 4.4.3 源码精读

被编译期注入的包变量：

[cmd/nginx-ingress/main.go#L55-L67](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/main.go#L55-L67) `version`、`telemetryEndpoint` 注释为 `// Injected during build`；`logLevels` 把字符串名映射到 `slog.Level`。

Makefile 的注入实现（佐证）：

[Makefile#L48-L51](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L48-L51) `GO_LINKER_FLAGS_VARS = -X main.version=... -X main.telemetryEndpoint=...`，最终拼进 `GO_LINKER_FLAGS`。

版本来源与构建命令（佐证）：

[.github/data/version.txt#L1](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/.github/data/version.txt#L1) `IC_VERSION=5.6.0`。

[Makefile#L149](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L149) `go build -trimpath -ldflags "$(GO_LINKER_FLAGS)" ...`，`-trimpath` 让 VCS 路径可复现。

> 为什么必须 `make build`？因为裸 `go build` 不会带 `-X main.version=...`，得到的 `version` 是空串，版本横幅会显示空值。

运行期读取 VCS 信息：

[cmd/nginx-ingress/utils.go#L7-L27](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/utils.go#L7-L27) `getBuildInfo` 用 `debug.ReadBuildInfo()` 取 `vcs.revision`/`vcs.time`/`vcs.modified`，取不到则回退 `"unknown"`。

> 这要求构建时启用了 VCS 嵌入（Go 默认在 git 仓库内构建即启用），`-trimpath` 不影响 VCS 元信息。

日志级别常量（佐证）：

[internal/logger/levels/levels.go#L3-L21](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/logger/levels/levels.go#L3-L21) 定义 `LevelTrace(-8)`…`LevelFatal(12)`，被 `main.go` 的 `logLevels` map 引用。

日志初始化函数：

[cmd/nginx-ingress/main.go#L1239-L1297](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/main.go#L1239-L1297) `initLogger`：构造 options（含 `ReplaceAttr` 改写时间戳为 unix 秒/毫秒、把 source 文件名裁成 basename），按格式选 handler，设置默认 logger 并返回带 logger 的 context。

关键设计：用的是 `slog.LevelVar`（**可变**级别变量）。这意味着级别可在运行期被改写——配合后续配置热更新，可以不改代码、不重启地动态调整日志级别。

#### 4.4.4 代码实践

**实践目标**：验证「版本号来自编译期注入」这一结论（对照实验型实践）。

**操作步骤**：

1. 用 `make build` 构建二进制（正确注入版本）。
2. 运行 `./nginx-ingress-amd64 -version`，观察第一行横幅的 `Version=` 字段。
3. （对照）进入 `cmd/nginx-ingress` 目录，用裸 `go build -o /tmp/nic-raw .` 构建，再运行 `/tmp/nic-raw -version`。

**需要观察的现象**：

| 构建方式 | `Version=` 取值 | 原因 |
| --- | --- | --- |
| `make build` | `5.6.0` | ldflags `-X main.version=5.6.0` 注入 |
| 裸 `go build` | 空（或空串） | 未带 `-X`，`version` 保持零值 |

**预期结果**：对照实验证明版本号确实由 Makefile 的 ldflags 注入，而非写死在源码里。

> 待本地验证：本环境未实际执行构建；以上对照结论基于 [Makefile#L48-L51](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L48-L51) 与 [main.go#L55-L57](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/main.go#L55-L57) 的静态分析，请在本地实跑确认。

#### 4.4.5 小练习与答案

**练习 1**：`initLogger` 为什么用 `slog.LevelVar` 而不是固定 `slog.Level`？

> **参考答案**：`LevelVar` 是可变的级别变量，handler 持有它的指针。这样后续可以在不重建 handler、不重启进程的情况下动态调整日志级别（例如根据 ConfigMap 热更新把级别从 `info` 调到 `debug`），固定 `slog.Level` 做不到。

**练习 2**：横幅里的 `Commit=...` 与 `Version=...` 信息来源有何不同？

> **参考答案**：`Version` 来自 Makefile 通过 ldflags 注入的 `main.version`（语义版本）；`Commit` 来自 `getBuildInfo()` 运行期读取的 `vcs.revision`（git commit hash）。前者是「发布版本」，后者是「确切代码快照」，二者互补。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「**追踪一次启动**」的任务：

**任务**：假设你拿到一条部署命令（如下），请完整描述 NIC 从敲下回车到进入 `lbc.Run()` 之间发生了什么，并指出命令里每个 flag 分别在第几步、被哪段校验检查。

```bash
./nginx-ingress \
  -nginx-plus \
  -mgmt-configmap=default/nginx-plus-mgmt \
  -nginx-configmaps=default/nginx-config \
  -ingress-class=nginx \
  -enable-leader-election \
  -leader-election-lock-name=nginx-ingress-leader-election \
  -default-server-tls-secret=default/default-server-secret \
  -log-level=info
```

**要求**：

1. 按 4.1.2 的启动序列，列出这条命令会触发的关键步骤（版本横幅 → parseFlags → initLogger → initValidate → 建客户端 → … → lbc.Run）。
2. 在 4.3 的校验链里定位：
   - `-nginx-plus` 为真、且提供了 `-mgmt-configmap`，为什么能通过 [flags.go#L444-L446](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/flags.go#L444-L446) 的硬校验？
   - 如果**漏掉** `-mgmt-configmap`，会在哪一行 `Fatal`？
3. 指出 `-nginx-configmaps` 在第 15 步（`processConfigMaps`）被消费，而 `-leader-election-lock-name` 被组装进 `lbcInput` 交给控制器（u3-l7 会用到）。

**预期成果**：你能画出「命令行 → 启动步骤 → 校验点 → 最终进入控制器循环」的完整链路图，并为每个 flag 标注它在启动期的落点。这就是本讲的学习闭环。

## 6. 本讲小结

- `main()` 是一条**线性、fail-fast** 的启动编排链：打印版本 → 解析 flags → 初始化日志 → 清理/校验 → 建客户端与各类依赖 → 组装 `LoadBalancerController` → `lbc.Run()`。控制权在 `lbc.Run()` 处交接给控制器循环。
- 所有命令行参数在 `flags.go` 用标准库 `flag` 注册，`parseFlags()` 只做 `flag.Parse()` 并处理 `-version`；flag 值在解析后才可用。
- 启动校验分**三层**（`mustValidateInitialChecks` / `mustValidateWatchedNamespaces` / `mustValidateFlags`），统一由 `initValidate` 触发；区分「软校验（Warn + 回退）」与「硬校验（Fatal 退出）」。
- 版本号与遥测端点由 Makefile 在编译期经 `-ldflags -X` 注入（必须 `make build`）；commit/时间/dirty 由 `debug.ReadBuildInfo()` 运行期读取。
- 日志基于 `log/slog`，`initLogger` 按格式选 handler、用 `LevelVar` 支持动态级别，并把 logger 放进 `context` 全程序共享。
- 关键 flag 速记：`-nginx-configmaps`/`-mgmt-configmap`（配置来源）、`-ingress-class`（默认 nginx）、`-enable-leader-election`/`-leader-election-lock-name`（多副本 status 选举锁）。

## 7. 下一步学习建议

- 想看 `lbc.Run()` 之后控制器如何 list-watch、入队、调谐，进入 **u3-l1「LoadBalancerController 生命周期与 Run/Stop」**——本讲的终点正是它的起点。
- 想了解 `-nginx-configmaps` 指向的 ConfigMap 如何被解析成 NGINX 全局配置，进入 **u4-l2「全局配置：ConfigMap 解析」**。
- 想了解选举锁（`-leader-election-lock-name` 指向的 ConfigMap）如何驱动 leader 选举与 status 回写，进入 **u3-l7「Leader 选举与 Status 回写」**。
- 建议同时浏览官方文档的 [Command-line arguments](https://docs.nginx.com/nginx-ingress-controller/configuration/global-configuration/command-line-arguments/)（若可访问），把本讲的 flag 清单与官方说明对照印证。
