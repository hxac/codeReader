# controller 命令与运行时配置

## 1. 本讲目标

承接 u2-l1：上一讲你已经看清 `cmd/gateway` 这棵命令树，并知道 **`controller` 是唯一的控制面主进程**，它的 `RunE` 把一堆 flag 汇聚成一个 `config.Config` 后调用 `controller.StartManager(conf)`。本讲就往里钻一层，把这一句话拆开讲透。学完后你应该能够：

- 说清 `controller` 命令**有哪些 flag**，它们按职责分成哪几组、各自被哪个 `config.Config` 字段消费。
- 看懂「flag 名常量 → flag 值变量 → `cmd.Flags().Var/BoolVar` 注册」这套三步声明模式，以及 `stringValidatingValue` 这种 pflag.Value 类型为何能在**赋值时**就做校验。
- 读懂 `internal/controller/config/config.go` 里的 **Config 结构族**：为什么不是一个超大扁平 struct，而要拆成 `MetricsConfig` / `HealthConfig` / `LeaderElectionConfig` 等若干子结构。
- 沿着 `controller` 的 `RunE` 走一遍完整数据流：**flag 解析 → 运行期校验 → 装配 `config.Config` → 调用 `StartManager`**，并亲手追踪 3 个 flag 最终被哪个子系统消费。

> 本讲不再重复 u2-l1 讲过的「五个子命令职责」和「构建期注入的 version/telemetry 变量」，只在数据流中需要时一笔带过。

## 2. 前置知识

- **cobra / pflag 回顾**（u2-l1 详讲）：cobra 用命令树组织 CLI，pflag 负责解析 `--xxx`。一个 flag 在 pflag 里就是一个 `*pflag.Flag`，它的值由实现了 `pflag.Value` 接口的对象持有。
- **pflag.Value 接口**：这是本讲反复出现的核心。任何类型只要实现下面三个方法，就能被当作一个 flag 值：

  ```text
  String() string   // 当前值的字符串表示（用于打印 --help、序列化）
  Set(s string) error // 用户传入命令行字符串时被调用，负责解析+校验+存储
  Type() string      // 值的类型名（如 "string"/"int"/"bool"），影响 --help 显示
  ```

  关键在 `Set`：它在**解析命令行的当下**就被调用，所以把校验逻辑放进去，就能实现「非法 flag 在进程真正干活之前就被拒绝」。NGF 的 `stringValidatingValue` / `intValidatingValue` / `stringSliceValidatingValue` 就是这套机制的具体实现（u2-l4 会专门讲，本讲只需理解「赋值即校验」这一点）。
- **依赖注入（Dependency Injection）**：把一堆配置参数打包成一个结构体传给函数，而不是给函数一长串参数。NGF 把所有运行参数装进**一个** `config.Config`，再用 `controller.StartManager(conf)` 一次性交给控制面 Manager——Manager 内部各子系统再各取所需。这是后续 u3 大量 `createXxx(cfg, ...)` 工厂函数得以简洁的前提。
- **资源名（DNS-1123 subdomain）**：Kubernetes 对大部分资源名的硬性要求——只能有小写字母、数字、`-`、`.`，且以字母数字开头结尾。NGF 校验 flag 指向的资源名时复用了同一个校验函数 `validation.IsDNS1123Subdomain`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `cmd/gateway/commands.go` | 本讲主战场。`createControllerCommand` 里定义了全部 flag、`RunE` 把它们装配成 `config.Config` 并调用 `StartManager`。 |
| `internal/controller/config/config.go` | **Config 结构族**的全部定义。控制面一切运行参数的类型归宿。 |
| `cmd/gateway/validating_types.go` | `stringValidatingValue` 等 pflag.Value 实现，解释「flag 为何能赋值即校验」。 |
| `cmd/gateway/validation.go` | 具体校验函数：`validateGatewayControllerName`、`validateResourceName`、`validatePort`、`ensureNoPortCollisions` 等。 |
| `internal/controller/manager.go` | `StartManager(cfg config.Config)` 的定义（L126），以及 Config 各字段被**消费**的地方——本讲实践需要追到这里。 |

## 4. 核心概念与源码讲解

### 4.1 controller 命令的 flag 集

#### 4.1.1 概念说明

`controller` 命令是整个 NGF 暴露给运维的最大「旋钮面板」：开不开 Plus、监听哪些命名空间、metrics 端口、leader 选举、是否启用实验特性……全都通过 flag 控制。flag 数量很多（四十多个），但它们并不杂乱，而是按职责天然分成几组：**身份与 Gateway API 核心**、**数据面寻址/TLS**、**metrics/健康**、**leader 选举**、**遥测**、**特性开关**、**Plus 与镜像**、**用量上报**、**WAF/PLM**、**命名空间范围**、**推理扩展 EPP**。

理解这一组组 flag 的意义在于：日后看 Helm `values.yaml` 或 `deployment.yaml` 里那一长串 `args` 时，你能立刻对号入座「这一行对应源码里哪个 flag、影响哪个子系统」，而不是面对一片黑盒。

#### 4.1.2 核心流程

NGF 声明一个 flag 走的是**三步模式**，这是全文件最值得记住的套路：

1. **flag 名常量**：在 `const (...)` 块里把 flag 名定义成字符串常量（如 `metricsPortFlag = "metrics-port"`），避免「名字」在声明处和使用处（如 `MarkFlagRequired`）写成魔法字符串。
2. **flag 值变量**：在 `var (...)` 块里声明承载该 flag 值的变量。普通布尔用 `bool`；字符串/整型/字符串切片用自定义的校验型 Value（`stringValidatingValue` 等），并在声明时**挂上校验函数**和默认值。
3. **注册**：在 `cmd.Flags().Var(...)` / `BoolVar(...)` 里把「flag 名」与「值变量」绑定，并给出 `--help` 里的说明文字。

这套模式的好处是：校验逻辑跟着值变量走（声明时挂上），注册处只负责「接线」，职责分明。`Set` 在解析命令行时被调用，非法值当场报错、进程直接退出，根本走不到后续装配。

flag 分组与「值类型 → 消费字段」的总览见 4.1.3 的表格。

#### 4.1.3 源码精读

**第一步：flag 名常量**。`createControllerCommand` 内有一个函数级 `const` 块，集中定义本命令用到的 flag 名：

[cmd/gateway/commands.go:87-118](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L87-L118) —— 每一行就是一个 flag 名常量，注释即用途。另有少量跨命令共享的 flag 名（`gatewayclass`、`gateway-ctlr-name`、`nginx-plus` 等）定义在**包级** const 块：

[cmd/gateway/commands.go:29-50](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L29-L50) —— 注意 `domain = "gateway.nginx.org"`，它是控制器名与 GatewayClass 认领的关键（u1-l1 讲过）。

**第二步：flag 值变量**。紧接着的 `var (...)` 块声明所有承载值的变量，并在校验型 Value 上挂校验函数与默认值：

[cmd/gateway/commands.go:121-197](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L121-L197) —— 例如 `gatewayCtlrName` 挂 `validateGatewayControllerName`、`metricsListenPort` 默认 `9113` 挂 `validatePort`、`healthListenPort` 默认 `8081`。

`stringValidatingValue` 为何能「赋值即校验」？看它的 `Set`：

[cmd/gateway/validating_types.go:11-32](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validating_types.go#L11-L32) —— `Set` 先调用挂上的 `validator(param)`，通过才赋值；不通过直接返回 error，pflag/cobra 收到后令 `RunE` 根本不被调用。整型同理：

[cmd/gateway/validating_types.go:79-104](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validating_types.go#L79-L104) —— `intValidatingValue.Set` 先 `ParseInt` 再 `validator`。

对应的校验函数：控制器名必须形如 `DOMAIN/PATH` 且 domain 固定为 `gateway.nginx.org`：

[cmd/gateway/validation.go:17-41](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation.go#L17-L41) —— 先检查非空、再按 `/` 切分校验 domain、最后用正则 `controllerNameRegex` 兜底。资源名则复用 K8s 的 DNS-1123 校验：

[cmd/gateway/validation.go:43-56](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation.go#L43-L56) —— `validation.IsDNS1123Subdomain(value)`。端口范围校验：

[cmd/gateway/validation.go:235-248](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation.go#L235-L248) —— `validatePort` 要求 [1024-65535]（避开特权端口），`validateAnyPort` 放宽到 [1-65535]。

**第三步：注册**。`cmd.Flags().Var/BoolVar` 把名字与值变量绑定。两个**必填** flag 用 `MarkFlagRequired` 标记：

[cmd/gateway/commands.go:359-371](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L359-L371) —— `gateway-ctlr-name` 与 `gatewayclass` 必填，呼应 u1-l1「控制器名须与 GatewayClass 的 controllerName 一致才被认领」。其余注册逐块展开于 L373-L632。

下面把全部 flag 按职责分组归纳（「消费字段」指 4.3 装配时写入的 `config.Config` 字段）：

| 分组 | flag（注册处行号） | 值类型 | 消费字段 |
| --- | --- | --- | --- |
| 身份/Gateway API | `--gateway-ctlr-name`(L359, 必填) `--gatewayclass`(L366, 必填) `-c/--config`(L373) | 校验型 string | `GatewayCtlrName` `GatewayClassName` `ConfigName` |
| 数据面寻址/TLS | `--service`(L381) `--agent-tls-secret`(L388) `--server-tls-domain`(L593) | 校验型 string | `GatewayPodConfig.ServiceName` `AgentTLSSecretName` `ServerTLSDomain` |
| metrics/健康 | `--metrics-disable`(L422) `--metrics-port`(L429, 默认 9113) `--metrics-secure-serving`(L435) `--health-disable`(L443) `--health-port`(L450, 默认 8081) | bool / 校验型 int | `MetricsConfig` `HealthConfig` |
| leader 选举 | `--leader-election-disable`(L456) `--leader-election-lock-name`(L465) | bool / string | `LeaderElection` |
| 遥测 | `--product-telemetry-disable`(L472) `--nginx-one-*`(L396-L420, 共 4 个) | bool / string / int | `ProductTelemetryConfig` `NginxOneConsoleTelemetryConfig` |
| 特性开关 | `--gateway-api-experimental-features`(L486) `--gateway-api-inference-extension`(L494) `--snippets-filters`(L560, 已废弃) `--snippets`(L570) | bool | `ExperimentalFeatures` `InferenceExtension` `SnippetsFilters` `Snippets` |
| Plus/镜像 | `--nginx-plus`(L479) `--nginx-docker-secret`(L504) `--nginx-scc`(L579, OpenShift) | bool / 切片 / string | `Plus` `NginxDockerSecretNames` `NGINXSCCName` |
| 用量上报(仅 Plus) | `--usage-report-*`(L511-L558, 共 7 个) | 校验型 string / bool | `UsageReportConfig`（经 `buildUsageReportConfig`） |
| WAF/PLM | `--plm-storage-*`(L599-L631, 共 5 个) | 校验型 string / bool | `PLMStorageConfig`（经 `buildPLMStorageConfig`） |
| 命名空间范围 | `--watch-namespaces`(L586) | 切片 | `WatchNamespaces` |
| 推理扩展 EPP | `--endpoint-picker-disable-tls` `--endpoint-picker-tls-skip-verify`(L502→L971) | bool | `EndpointPickerDisableTLS` `EndpointPickerTLSSkipVerify` |

> 两个值得注意的惯用法：
> （1）**反向布尔**：`--metrics-disable` 存进 `disableMetrics bool`，装配时写 `Enabled: !disableMetrics`。面向用户的 flag 是「关闭」，内部字段却是「启用」，因为默认值要落在「启用」上。`health-disable` / `leader-election-disable` / `product-telemetry-disable` 同理。
> （2）**已废弃 flag**：`--snippets-filters` 用 `MarkDeprecated` 标记，提示改用 `--snippets`（后者同时启用 SnippetsFilter 与 SnippetsPolicy 两个 API）：

[cmd/gateway/commands.go:567-568](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L567-L568) —— 废弃提示文本。

#### 4.1.4 代码实践

**实践目标**：动手验证「三步声明模式」与「赋值即校验」，建立 flag 到校验函数的直觉。

**操作步骤（源码阅读 + 命令行对照）**：

1. 在 `commands.go` 的 L87-L118 中任选 3 个 flag 名常量，沿「常量 → L121-L197 的值变量 → L359 起的 `cmd.Flags().Var/BoolVar`」走一遍，确认三者一一对应。
2. 对 `--gatewayclass` 追到它挂的校验函数 `validateResourceName`，再追到 K8s 的 `validation.IsDNS1123Subdomain`。
3. 若本地已按 u1-l3 `make build` 出了二进制，故意传一个非法值观察行为：

   ```bash
   # 资源名不允许大写
   ./build/out/gateway controller --gateway-ctlr-name=gateway.nginx.org/nginx-gateway-controller --gatewayclass=Bad_Name
   ```

**需要观察的现象**：第 3 步应**立刻报错退出**（在 `RunE` 之前），错误信息形如 `invalid format: ...`，且**不会**打印 `Starting the NGINX Gateway Fabric control plane` 横幅——证明校验发生在装配之前。

**预期结果**：你确认了「校验型 Value 的 `Set` 在命令行解析阶段就拦截非法值」，进程根本走不到 4.3 的装配逻辑。

> 若无法本地编译，纯阅读亦可得出同样结论：`stringValidatingValue.Set`（validating_types.go:22-28）在赋值前调用 `validator`，返回 error 后 pflag 会让命令直接失败。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `--metrics-port` 默认值 `9113` 写在值变量声明处（L157-L160），而不是写在 `cmd.Flags().Var(...)` 里？

**参考答案**：因为 `metricsListenPort` 是 `intValidatingValue`，它的 `value` 字段就是「当前值」兼「默认值」——cobra 取默认值时会调它的 `String()`（返回 `strconv.Itoa(v.value)`）。把默认值放在值变量里，校验函数、默认值、当前值三者集中在一处，符合「校验逻辑跟着值走」的设计；若放进 `Var` 还得额外维护一份默认值，容易不一致。

**练习 2**：`--gatewayclass` 与 `--gateway-ctlr-name` 为什么被标为必填，而 `--service` 不是？

**参考答案**：前两者是控制面的**身份标识**——没有控制器名就无法认领 GatewayClass，没有 GatewayClass 名就不知道认领哪一个（u1-l1）。`--service` 指向「前置本 Pod 的 Service」，虽然重要，但在没有它时控制面仍可启动（只是部分依赖 Service 名的能力，如证书 SAN、provisioner 寻址会受影响），故设为可选而非必填。

---

### 4.2 Config 结构族

#### 4.2.1 概念说明

`controller` 的 `RunE` 最终要产出一个 `config.Config` 交给 `StartManager`。你可能会问：为什么不直接把四十多个变量当参数传给 `StartManager(conf, a, b, c, ...)`？因为参数会爆炸、可读性极差。NGF 的做法是把它们**按子系统聚类**成若干小 struct，再组合进顶层 `Config`。这就是「结构族」：一个根 `Config`，下面挂着 `MetricsConfig`、`HealthConfig`、`LeaderElectionConfig`、`ProductTelemetryConfig`、`UsageReportConfig`、`NginxOneConsoleTelemetryConfig`、`PLMStorageConfig`、`GatewayPodConfig` 等子结构。

这种聚类的好处有三：（1）**就近消费**——`createManager` 只关心 `MetricsConfig`/`HealthConfig`/`LeaderElection`，整块传进去即可；（2）**可选性清晰**——`PLMStorageConfig` 是指针，`nil` 即「未配置」，比一堆空字符串语义明确；（3）**新增参数不破坏调用方**——往某个子 struct 加字段，所有传递该 struct 的函数签名都不变。

#### 4.2.2 核心流程

顶层 `Config` 由三类字段拼成：

```text
config.Config
├── 标量配置（身份/开关）
│    GatewayCtlrName, GatewayClassName, ConfigName, Plus,
│    ExperimentalFeatures, InferenceExtension, Snippets, SnippetsFilters,
│    AgentTLSSecretName, ServerTLSDomain, ImageSource, NGINXSCCName,
│    EndpointPickerDisableTLS, EndpointPickerTLSSkipVerify
├── 子结构配置（按子系统聚类）
│    GatewayPodConfig, MetricsConfig, HealthConfig, LeaderElectionConfig,
│    ProductTelemetryConfig, UsageReportConfig, NginxOneConsoleTelemetryConfig
├── 集合/指针配置
│    WatchNamespaces []string, NginxDockerSecretNames []string,
│    PLMStorageConfig *PLMStorageConfig(指针,nil=未配置)
└── 运行时对象（非 flag 来源）
     Logger logr.Logger, AtomicLevel zap.AtomicLevel, Flags(原始 flag 名/值,供遥测)
```

其中 `Logger` / `AtomicLevel` / `GatewayPodConfig` / `Flags` **不是来自 flag**：`Logger` 与 `AtomicLevel` 在 `RunE` 里现场创建（日志器 + 可热改的日志级别）；`GatewayPodConfig` 由环境变量组装（见 4.3）；`Flags` 由 `parseFlags` 扫描全部 flag 生成，用于把「用户改了哪些旋钮」随遥测上报。

#### 4.2.3 源码精读

顶层 `Config` 的全貌（字段注释即用途）：

[internal/controller/config/config.go:12-68](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L12-L68) —— 注意 `PLMStorageConfig *PLMStorageConfig` 是指针（注释 `Nil when PLM is not configured`），其余子结构是值类型。

各子结构的定义（同一文件内依次排列）：

- `MetricsConfig`：[config.go:103-111](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L103-L111) —— `Port` / `Enabled` / `Secure`，对应 metrics 端口的绑定地址与是否 HTTPS。
- `HealthConfig`：[config.go:113-119](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L113-L119) —— 健康探针服务端口与开关。
- `LeaderElectionConfig`：[config.go:121-129](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L121-L129) —— `LockName` / `Identity` / `Enabled`。
- `ProductTelemetryConfig`：[config.go:131-141](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L131-L141) —— `Endpoint` / `ReportPeriod time.Duration` / `EndpointInsecure` / `Enabled`。
- `UsageReportConfig`：[config.go:143-159](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L143-L159) —— NGINX Plus 用量上报的 Secret/CA/客户端证书/endpoint/resolver/skip-verify/是否强制初始上报。
- `GatewayPodConfig`：[config.go:84-101](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L84-L101) —— 本 Pod 的 Service 名、命名空间、名字、UID、实例名(Helm release 名)、版本、镜像。
- `PLMStorageConfig`：[config.go:70-82](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L70-L82) —— WAF bundle 的 S3 兼容存储连接配置。
- `NginxOneConsoleTelemetryConfig`：[config.go:171-181](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L171-L181) —— NGINX One Console 遥测。
- `Flags`：[config.go:161-169](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L161-L169) —— `Names` / `Values` 两个切片**按下标配对**，记录每个 flag 的名与值（值只区分 `default` / `user-defined` / 布尔的 `true/false`，**不记录真实敏感值**，安全地随遥测上报）。

> 设计要点：`Flags` 故意不存敏感值——`parseFlags` 对非布尔 flag 只记 `default` 或 `user-defined`，对布尔 flag 记 `true/false`。这样遥测能统计「多少部署改了某个旋钮」而不泄露 Secret 名等敏感信息。

#### 4.2.4 代码实践

**实践目标**：体会「子结构聚类」如何让消费方代码简洁。

**操作步骤（源码阅读型）**：

1. 打开 `internal/controller/manager.go`，找到 `createManager`（L509）与 `getMetricsOptions`（L1405）。
2. 观察 `getMetricsOptions(cfg config.MetricsConfig)` 的签名——它**只接收 `MetricsConfig` 这一个子结构**，而不是整个 `Config`。
3. 对照 `getMetricsOptions` 的实现，看它如何用 `cfg.Enabled` / `cfg.Secure` / `cfg.Port` 决定 `metricsserver.Options`。

[internal/controller/manager.go:1405-1416](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L1405-L1416) —— `Enabled` 时按 `:%v` 绑定 `cfg.Port`，`Secure` 时开 HTTPS；未启用则 `BindAddress: "0"`（不监听）。

**需要观察的现象 / 预期结果**：你会看到 `createManager` 调用 `Metrics: getMetricsOptions(cfg.MetricsConfig)`（L513）、`LeaderElection: cfg.LeaderElection.Enabled`（L517）、`LeaderElectionID: cfg.LeaderElection.LockName`（L519）——每个子系统只取自己那块子结构，互不干扰。这就是聚类的回报：消费方代码按子系统自然分块，新增 metrics 字段不会动到 leader 选举逻辑。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `PLMStorageConfig` 用指针而 `MetricsConfig` 用值？

**参考答案**：`PLMStorageConfig` 有「未配置」这一合法状态（没启用 WAF PLM 时根本不需要），用指针后 `nil` 即天然表达「未配置」，消费方 `if cfg.PLMStorageConfig != nil` 一眼可读。`MetricsConfig` 永远存在（至多 `Enabled=false`），用值类型即可，省去指针判空与堆分配。

**练习 2**：`GatewayPodConfig` 的字段（ServiceName/Namespace/Name/UID/InstanceName/Version/Image）里，哪些来自 flag、哪些来自环境变量？

**参考答案**：只有 `ServiceName` 来自 `--service` flag；其余（Namespace/Name/UID/InstanceName/Version/Image）来自 `createGatewayPodConfig` 读取的环境变量 `POD_NAMESPACE`/`POD_NAME`/`POD_UID`/`INSTANCE_NAME`/`IMAGE_NAME`（`Version` 来自构建期注入的 `version`）。详见 4.3 的 `createGatewayPodConfig`。

---

### 4.3 flag → Config → StartManager 流转

#### 4.3.1 概念说明

前两模块分别讲了「输入（flag）」和「容器（Config）」。本模块把它们串成一条完整的数据流：`controller` 的 `RunE` 是这条流水线的总调度，它做四件事——**初始化日志 → 运行期校验 → 装配 `config.Config` → 调用 `StartManager`**。其中「装配」是核心：把分散在各 flag 值变量里的值，连同现场创建的日志器、从环境变量组装的 Pod 信息，一齐填进一个 `config.Config{...}` 字面量，然后整个交给 `StartManager`。从此刻起，命令行入口的使命结束，控制面 Manager 接管（u3 主题）。

这条流转有一个重要性质：**装配点单一**。无论 flag 多少个，最终都收口在 `RunE` 里那一个 `conf := config.Config{...}` 字面量。这意味着任何人想搞清楚「这个运行参数从哪来、到哪去」，只需要读这一段字面量 + 顺着字段名去 `StartManager` 里 grep。

#### 4.3.2 核心流程

`controller` 的 `RunE`（commands.go L239-L356）可划为四个阶段：

```text
阶段 1：日志与构建信息
  atom := zap.NewAtomicLevel()           // 可热改的日志级别
  logger := ctlrZap.New(...)             // zap 日志器
  klog.SetLogger(logger); log.SetLogger(logger)  // 让 K8s 库也用这个 logger
  getBuildInfo() → commit/date/dirty      // 启动横幅（version 为构建期注入，u2-l1）

阶段 2：运行期校验与解析
  ensureNoPortCollisions(metricsPort, healthPort)  // 两端口不能撞车
  imageSource := BUILD_AGENT 环境变量归一化(gHA/local/unknown)
  period := time.ParseDuration(telemetryReportPeriod)   // 构建期注入字符串→Duration
  validateEndpoint(telemetryEndpoint)（非空时）
  telemetryEndpointInsecure := strconv.ParseBool(...)   // 构建期注入字符串→bool
  if plus { usageReportConfig = buildUsageReportConfig(...) }  // 仅 Plus
  plmStorageConfig := buildPLMStorageConfig(plmParams)        // URL 为空则返回 nil
  flagKeys, flagValues := parseFlags(cmd.Flags())             // 扫描全部 flag 供遥测
  podConfig := createGatewayPodConfig(version, serviceName.value)  // 从环境变量组装

阶段 3：装配
  conf := config.Config{ ... }   // 把上面所有结果填进一个字面量（L297-L349）

阶段 4：交棒
  controller.StartManager(conf)   // 命令行入口结束，控制面接管（L351）
```

注意几个细节：`buildUsageReportConfig` 只在 `plus=true` 时调用，否则 `usageReportConfig` 保持零值；`buildPLMStorageConfig` 内部判断 URL 为空就返回 `nil`（对应 `PLMStorageConfig` 的指针语义）；`parseFlags` 把全部 flag 名/值收进 `config.Flags`，供后续遥测上报「用户改了哪些旋钮」。

#### 4.3.3 源码精读

**阶段 1：日志与构建信息**

[cmd/gateway/commands.go:240-253](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L240-L253) —— 创建 `atom`（`zap.AtomicLevel`，支持运行时动态调日志级别）与 `logger`，设置给 klog 与 controller-runtime 的全局 logger，打印含 version/commit/date/dirty 的启动横幅。`atom` 与 `logger` 后面会作为 `Config.AtomicLevel` / `Config.Logger` 装进去。

**阶段 2：运行期校验与解析**

端口冲突检查——metrics 与 health 不能监听同一端口：

[cmd/gateway/commands.go:255-257](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L255-L257) —— 调用 `ensureNoPortCollisions`。

[cmd/gateway/validation.go:252-263](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation.go#L252-L263) —— 用一个 `seen` map 检测重复端口。这是「单个 flag 各自校验」做不到的**跨 flag 一致性校验**，所以放在 `RunE` 里手工调用。

构建期注入变量的解析（u2-l1 详讲）：

[cmd/gateway/commands.go:264-278](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L264-L278) —— `telemetryReportPeriod`→`time.Duration`、`telemetryEndpoint` 校验、`telemetryEndpointInsecure`→`bool`。

按需构建 Plus 用量上报配置：

[cmd/gateway/commands.go:280-286](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L280-L286) —— 仅当 `plus` 为真才调用。

[cmd/gateway/commands.go:636-650](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L636-L650) —— `buildUsageReportConfig` 在 `SecretName` 为空时报错（Plus 必须提供 usage-report-secret）。

PLM 存储配置（WAF）：

[cmd/gateway/commands.go:288-296](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L288-L296) —— `buildPLMStorageConfig` + `createGatewayPodConfig`。

[cmd/gateway/commands.go:652-664](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L652-L664) —— URL 为空返回 `nil`，否则组装 `*config.PLMStorageConfig`。

扫描全部 flag 供遥测：

[cmd/gateway/commands.go:290](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L290) 与 [cmd/gateway/commands.go:989-1010](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L989-L1010) —— `parseFlags` 遍历所有 flag：布尔记 `true/false`，其余记 `default`（等于 `DefValue`）或 `user-defined`。

Pod 信息组装（来自环境变量）：

[cmd/gateway/commands.go:1035-1072](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L1035-L1072) —— `createGatewayPodConfig` 逐个读取 `POD_UID`/`POD_NAMESPACE`/`POD_NAME`/`INSTANCE_NAME`/`IMAGE_NAME`，任一缺失即返回错误（`getValueFromEnv` 在 L1074-L1081）。

**阶段 3：装配**

核心字面量——本讲的总收口：

[cmd/gateway/commands.go:297-349](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L297-L349) —— 一个 `config.Config{...}` 把所有 flag 值变量、现场创建的 `logger`/`atom`、`podConfig`、`usageReportConfig`、`plmStorageConfig`、`flagKeys/flagValues` 全部填入。注意反向布尔在此处翻转（如 `Enabled: !disableMetrics`、`Enabled: !disableLeaderElection`），以及 `Identity: podConfig.Name`（leader 选举身份用 Pod 名）。

**阶段 4：交棒**

[cmd/gateway/commands.go:351-353](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L351-L353) —— `controller.StartManager(conf)`；失败时包成 `failed to start control loop`。

`StartManager` 的入口签名（消费方）：

[internal/controller/manager.go:126-131](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L126-L131) —— 接收 `config.Config`，内部 `createManager(cfg, ...)` 装配 controller-runtime manager。从这里起进入 u3。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：挑选 3 个 flag，追踪它们「从命令行字符串 → flag 值变量 → `config.Config` 字段 → `StartManager` 内被哪个子系统消费」的完整链路。这是把本讲三个模块串起来的练习。

**操作步骤（源码追踪型，全程不改源码）**：

按下面三条链路，分别在源码里跳转并标注行号。

**链路 A：`--gatewayclass nginx`**

1. 解析：flag 名常量 `gatewayClassFlag`(L32) → 值变量 `gatewayClassName`(L126-L128，挂 `validateResourceName`) → 注册 `cmd.Flags().Var`(L366-L370) + 必填 `MarkFlagRequired`(L371)。
2. 装配：写入 `Config.GatewayClassName`(L302)。
3. 消费（manager.go）：`recorderName := fmt.Sprintf("nginx-gateway-fabric-%s", cfg.GatewayClassName)`(L133，事件记录器名)；`constLabels := map[string]string{"class": cfg.GatewayClassName}`(L295，metrics 固定标签)；`gatewayClassName: cfg.GatewayClassName`(L245，控制器注册配置，决定认领哪个 GatewayClass)。
   - [internal/controller/manager.go:133](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L133)
   - [internal/controller/manager.go:295](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L295)
   - [internal/controller/manager.go:245](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L245)

**链路 B：`--nginx-plus`**

1. 解析：flag 名常量 `plusFlag`(L37) → 值变量 `plus bool`(L181) → 注册 `cmd.Flags().BoolVar`(L479-L484)。
2. 装配：写入 `Config.Plus`(L325)；并触发 `buildUsageReportConfig`(L280-L286) 生成 `UsageReportConfig`(L318)。
3. 消费（manager.go）：配置生成器 `cfg.Plus`(L231、L315，决定走 OSS 还是 Plus 的 upstream/指令)；`Plus: cfg.Plus`(L370，provisioner 配置)；`PlusUsageConfig: &cfg.UsageReportConfig`(L372)；`if !cfg.Plus { ... }`(L401，分支)；`createPlusSecretMetadata` 内 `if cfg.Plus { ... cfg.UsageReportConfig.SecretName ... }`(L1060)。
   - [internal/controller/manager.go:231-232](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L231-L232)
   - [internal/controller/manager.go:370-372](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L370-L372)
   - [internal/controller/manager.go:1060-1063](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L1060-L1063)

**链路 C：`--metrics-port 9113`**

1. 解析：flag 名常量 `metricsPortFlag`(L97) → 值变量 `metricsListenPort`(L157-L160，默认 9113，挂 `validatePort`) → 注册 `cmd.Flags().Var`(L429-L433)。
2. 运行期校验：`ensureNoPortCollisions(metricsListenPort.value, healthListenPort.value)`(L255)。
3. 装配：写入 `MetricsConfig.Port`(L310)。
4. 消费（manager.go）：`createManager` 调 `Metrics: getMetricsOptions(cfg.MetricsConfig)`(L513)；`getMetricsOptions` 用 `cfg.Port` 拼 `:%v` 绑定地址(L1412)。
   - [internal/controller/manager.go:513](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L513)
   - [internal/controller/manager.go:1405-1416](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L1405-L1416)

**需要观察的现象 / 预期结果**：你应当得到一张「flag → Config 字段 → 消费子系统」三列表，例如：

| flag | Config 字段 | 消费子系统（manager.go） |
| --- | --- | --- |
| `--gatewayclass` | `GatewayClassName` | 事件记录器名(L133)、metrics 标签(L295)、控制器注册/认领(L245)、watch GatewayClass(L1281) |
| `--nginx-plus` | `Plus` + `UsageReportConfig` | 配置生成(L231/315)、provisioner(L370)、Plus Secret 元数据(L1060) |
| `--metrics-port` | `MetricsConfig.Port` | metrics server 绑定地址(L513→1412) |

> 说明：本实践只追到 `StartManager` 内部的**第一跳消费点**即达成目标；这些子系统（控制器注册、配置生成、provisioner）的内部细节分别属于 u3/u5/u6/u9，本讲不展开。

#### 4.3.5 小练习与答案

**练习 1**：`ensureNoPortCollisions` 为什么不能像 `validatePort` 那样挂在校验型 Value 的 `Set` 里？

**参考答案**：因为它校验的是**两个 flag 之间的关系**（metrics 端口与 health 端口不能相同），而 `Set` 只在单个 flag 被赋值时触发，拿不到另一个 flag 的值。任何「跨 flag 一致性」校验都必须推迟到所有 flag 都解析完之后——也就是 `RunE` 里手工调用。

**练习 2**：如果用户既没传 `--usage-report-secret` 又开了 `--nginx-plus`，会在哪一步、以什么方式失败？

**参考答案**：在阶段 2 装配前，`buildUsageReportConfig`(commands.go:636-650) 检查到 `SecretName.value == ""`，返回 `errors.New("usage-report-secret is required when using NGINX Plus")`，`RunE` 直接返回该错误、进程退出，**不会**调用 `StartManager`。这是一个「条件必填」的典型：仅当 `--nginx-plus` 时 `--usage-report-secret` 才必填。

**练习 3**：`config.Flags`（`parseFlags` 的产物）最终被带到哪里、起什么作用？

**参考答案**：它随 `config.Config` 传入 `StartManager`，最终被产品遥测（telemetry，u11-l2）携带上报。由于 `parseFlags` 对非布尔 flag 只记 `default`/`user-defined`（不记真实值），它能让官方统计「多少部署启用了某特性 / 改了某旋钮」而不泄露 Secret 名等敏感信息。

---

## 5. 综合实践

把本讲三个模块串起来，制作一张**「controller 命令旋钮全景表」**，作为日后排障与调参的速查卡。要求：

1. **flag 分组**：参照 4.1.3 的分组表，把全部 flag 按职责归入 9~11 组。
2. **三列表**：每行写「flag 名 → 默认值 → 消费的 `config.Config` 字段（标明是否子结构、是否指针）」。
3. **数据流标注**：在表头注明这条数据流——`flag 值变量(赋值即校验) → RunE 阶段 2 运行期校验 → 阶段 3 装配 config.Config{...}(L297-L349) → 阶段 4 StartManager(conf)(L351)`。
4. **必填/条件必填标记**：标出两个必填 flag（`--gateway-ctlr-name`、`--gatewayclass`）与一个条件必填组合（`--nginx-plus` 时 `--usage-report-secret` 必填）。
5. **反向布尔专区**：单独列出所有 `--xxx-disable` flag，并写出它们到 `Enabled` 字段的翻转关系。

**建议产出**：一张 Markdown 表格 + 一段不超过 8 行的小结。完成后，你应当能不看源码回答：「我想把 metrics 端口从 9113 改成 9090，需要改哪个 flag、它会被写到哪个 Config 字段、最终影响哪个子系统？」（答案：`--metrics-port 9090` → `MetricsConfig.Port` → `getMetricsOptions` 的 `BindAddress`。）

## 6. 本讲小结

- `controller` 命令的 flag 走**三步声明模式**：flag 名常量(L87-L118) → 值变量(挂校验函数+默认值，L121-L197) → `cmd.Flags().Var/BoolVar` 注册(L359-L632)；校验型 pflag.Value（如 `stringValidatingValue`）在 `Set` 阶段就拦截非法值。
- 全部四十多个 flag 按**职责分组**：身份/Gateway API、数据面寻址/TLS、metrics/健康、leader 选举、遥测、特性开关、Plus/镜像、用量上报、WAF/PLM、命名空间范围、EPP。注意「反向布尔」（`--xxx-disable` → `Enabled: !disableX`）与「已废弃 flag」（`--snippets-filters`→`--snippets`）两类惯用法。
- 配置被打包成 **Config 结构族**：顶层 `config.Config`(config.go:12-68) 下挂 `MetricsConfig`/`HealthConfig`/`LeaderElectionConfig`/`ProductTelemetryConfig`/`UsageReportConfig`/`GatewayPodConfig`/`PLMStorageConfig`(指针)/`NginxOneConsoleTelemetryConfig` 等子结构，按子系统聚类，消费方各取所需。
- `controller.RunE` 是一条**四阶段流水线**：日志初始化 → 运行期校验与解析（含跨 flag 的 `ensureNoPortCollisions`、条件构建 `UsageReportConfig`/`PLMStorageConfig`、`parseFlags` 收集旋钮、`createGatewayPodConfig` 读环境变量）→ 装配单一 `config.Config{...}`(L297-L349) → 调用 `controller.StartManager(conf)`(L351)。
- 装配点**单一收口**：所有运行参数最终都汇聚进那一个 `config.Config` 字面量，再整体交给 `StartManager`；从此命令行入口结束、控制面 Manager 接管（u3）。
- 非 flag 来源的 Config 字段有三类：现场创建的 `Logger`/`AtomicLevel`、来自环境变量的 `GatewayPodConfig`、记录原始旋钮的 `Flags`（只区分 default/user-defined，安全随遥测上报）。

## 7. 下一步学习建议

- **u3-l1（StartManager 全景）**：本讲止步于 `StartManager(conf)` 的调用；下一讲进入 `StartManager` 内部，看清各 `createXxx(cfg, ...)` 工厂函数如何用这份 `config.Config` 装配出处理器、生成器、Agent 服务、provisioner、telemetry、健康检查。
- **u2-l3（初始化命令与证书生成）**：本讲提到 `--agent-tls-secret`/`--server-tls-domain` 等数据面 TLS 相关 flag，其证书由 `generate-certs` 命令生成，详见 u2-l3。
- **u2-l4（参数校验与准入式校验机制）**：本讲多次出现 `stringValidatingValue`/`intValidatingValue` 与各 `validateXxx` 函数，其完整设计与校验规则集合在 u2-l4 系统讲解。
- **延伸阅读源码**：想提前感受 Config 的消费全景，可在 `internal/controller/manager.go` 中通览 `StartManager`(L126) 与 `createManager`(L509)，对照本讲 4.3.4 的三条追踪链路自行扩展更多 flag 的消费点。
