# 产品遥测（Telemetry）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「产品遥测（product telemetry）」与上一讲「Prometheus 指标（metrics）」的本质区别，并理解 NGF 为什么默认开启遥测、上报了什么。
- 读懂 `internal/controller/telemetry` 的三块拼图：**数据模型（Avro schema + Go `Data` 结构）**、**数据采集（`DataCollector`）**、**周期上报（`CronJob` + 导出器）**。
- 说出上报字段的大致分类（集群/产品信息、命令行 flag、资源计数、策略计数、snippets 指令统计等），并明白 flag 的**值是被脱敏上报**的。
- 知道整个产品遥测只有一个运行时 CLI flag（`--product-telemetry-disable`）能关掉它，而周期、端点、是否走 TLS 都是**构建期注入**、无法在运行时通过 `--` 调整。
- 理解「只在 leader、且首张图构建完成后」才上报这一设计背后的可靠性与隐私取舍。

## 2. 前置知识

本讲是 **advanced** 阶段内容，建议你已经学完：

- **u3-l1（StartManager 全景）**：知道控制面装配有 `create*`/`register*` 工厂，遥测正是其中一个在 `registerTelemetry` 阶段挂上去的 Runnable。
- **u3-l4（Leader Election 与 Runnables）**：理解 `runnables.Leader` 包装器与 `CronJob` 周期任务的语义——本讲的遥测 Job 就是「`Leader` 套 `CronJob`」的典型实例。
- **u2-l2（controller 命令与运行时配置）**：理解「flag → `config.Config` → `StartManager`」的流转，以及**构建期注入（ldflags `-X`）**与运行时 CLI flag 的区别——这是本讲「哪些能被 flag 关闭」一题的关键。
- **u5-l1（Graph）**：遥测的「资源计数」几乎全部来自 `graph.Graph` 的各种字段长度（`Gateways`、`Routes`、`NGFPolicies` …）。

几个术语先对齐：

- **产品遥测 vs. 指标**：上一讲的 Prometheus 指标是「给运维看的、高频、本地暴露、`curl /metrics` 抓取」的运行时信号；本讲的产品遥测是「给 NGF 项目方看的、低频（默认 24 小时一次）、主动外发、汇总集群画像」的产品反馈。两者目的、频率、流向都不同，不要混淆。
- **Avro**：Apache Avro 是一种数据序列化 + schema 描述格式。`.avdl`（Avro IDL）是它的 schema 定义文件。NGF 用它作为遥测数据的**契约**，并通过代码生成器产出 Go 结构与属性映射。
- **OTLP / gRPC**：OpenTelemetry Protocol。NGF 的遥测最终以 OTLP span 的形式经 gRPC 发往遥测端点（生产环境的 `telemetryEndpoint`）。
- **ldflags `-X`**：Go 的链接期注入，在 `go build -ldflags "-X main.version=..."` 时把字符串塞进包级变量。它**不是**命令行 flag，运行时不能用 `--` 修改。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [internal/controller/telemetry/data.avdl](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/data.avdl) | Avro IDL schema，定义 `NGFProductTelemetry` 协议与 `Data` 记录——遥测数据的**契约源头**。 |
| [internal/controller/telemetry/collector.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/collector.go) | Go 侧 `Data`/`NGFResourceCounts` 结构，`DataCollectorImpl.Collect` 把 Graph + 集群信息 + flag 聚合成一条 `Data`。 |
| [internal/controller/telemetry/data_attributes_generated.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/data_attributes_generated.go) | 由 `go:generate` 从 schema 生成的 `Attributes()` 方法，把 `Data` 映射成 OTLP 属性键值对（**不要手改**）。 |
| [internal/controller/telemetry/exporter.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/exporter.go) | `Exporter` 接口与 `LoggingExporter`（无端点时的占位实现，只打日志）。 |
| [internal/controller/telemetry/job_worker.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/job_worker.go) | `DataCollector` 接口与 `CreateTelemetryJobWorker`，把「采集 + 导出」串成一次周期任务的 Worker 函数。 |
| [internal/controller/telemetry/platform.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/platform.go) | 集群平台识别（gke/eks/aks/kind/k3s/openshift/rancher…），用于填充 `ClusterPlatform` 字段。 |
| [internal/controller/manager.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go) | `registerTelemetry`（装配）与 `createTelemetryJob`（构造 Leader+CronJob+导出器），是遥测的**挂载点**。 |
| [internal/framework/runnables/cronjob.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/cronjob.go) | `CronJob` 通用周期任务，含 `ReadyCh` 就绪门与 `sliding` 抖动。 |
| [cmd/gateway/commands.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go) | `--product-telemetry-disable` flag 注册、`ProductTelemetryConfig` 装配、`parseFlags`（flag 脱敏逻辑）。 |
| [internal/controller/config/config.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go) | `ProductTelemetryConfig`、`Flags`、`NginxOneConsoleTelemetryConfig` 三组配置结构。 |
| [cmd/gateway/main.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/main.go) | 构建期注入的 `telemetryReportPeriod`/`telemetryEndpoint`/`telemetryEndpointInsecure` 包级变量。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：**4.1 数据模型与 Avro schema**、**4.2 数据采集**、**4.3 周期上报与导出**，外加一节专门回答本讲实践任务里「哪些能被 flag 关闭」的问题（4.4）。

### 4.1 数据模型与 Avro schema

#### 4.1.1 概念说明

遥测系统的第一性问题不是「怎么发」，而是「发什么、发多少、字段会变怎么办」。NGF 的回答是：**用一份 Avro schema 当唯一契约**，所有上报字段都在 `data.avdl` 里白纸黑字定义，Go 代码里的 `Data` 结构由它驱动生成，下游的 OTLP 属性映射也由它自动生成。

这样做有三个好处：

1. **字段有据可查**：想知道 NGF 上报了什么，直接读 `data.avdl` 即可，不必反编译二进制、抓包。
2. **前向/后向兼容**：schema 里**每个字段都带 `= null` 默认值**（即 nullable）。这意味着将来新增字段，旧的解析端不会因缺字段报错；解析端读不到新字段也只是得到 `null`。这是 Avro schema 演进的核心约定。
3. **代码与契约不漂移**：Go 结构、属性映射、schema 三者由同一个 `go:generate` 指令同步产出，手改一处会被下一次生成覆盖。

#### 4.1.2 核心流程

数据从 schema 到上报的流水线：

```text
data.avdl (Avro IDL 契约)
   │  go:generate 调 telemetry-exporter/cmd/generator
   ▼
Go: Data 结构 (collector.go)  +  Attributes() 方法 (data_attributes_generated.go)
   │  Collect() 运行期填充
   ▼
Data{...} 实例
   │  Exporter.Export()
   ▼
OTLP span 属性  →  gRPC 上报到 telemetryEndpoint
```

`Data` 结构由三块拼成：

- **`tel.Data`（嵌入）**：来自外部库 `github.com/nginx/telemetry-exporter` 的公共字段（项目名/版本/架构、集群 ID/版本/平台、安装 ID、节点数等），是所有 NGINX 产品共用的「公共头」。
- **`NGFResourceCounts`（嵌入）**：NGF 特有的资源与策略计数，字段最多。
- **NGF 顶层自有字段**：`ImageSource`、`FlagNames/FlagValues`、snippets 指令统计、`NginxPodCount`、`ControlPlanePodCount`、`NginxOneConnectionEnabled`、`BuildOS` 等。

#### 4.1.3 源码精读

Avro schema 的协议与记录定义，每个字段都标注了用途且默认 `null`：

[internal/controller/telemetry/data.avdl:1-9](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/data.avdl#L1-L9) —— `@namespace` 命名空间 `gateway.nginx.org`，协议名 `NGFProductTelemetry`；`@df_datatype("ngf-product-telemetry")` 给记录打上数据类型标签，这个字符串会原样出现在上报属性里（见下文 `dataType`）。前三个固定字段 `dataType`/`eventTime`/`ingestTime` 是公共元数据，不带默认值。

[internal/controller/telemetry/data.avdl:12-13](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/data.avdl#L12-L13) —— `string? ImageSource = null;`：注意 `?` 表示 nullable、`= null` 是默认值。这是 schema 演进的护身符——**所有业务字段无一例外**都这么写。

[internal/controller/telemetry/data.avdl:40-46](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/data.avdl#L40-L46) —— `FlagNames` 与 `FlagValues` 用的是 Avro 的 `union {null, array<string>}`，表示「可能为 null，否则是字符串数组」。注意注释明确：每个值要么是 `'true'/'false'`（布尔 flag），要么是 `'default'/'user-defined'`（非布尔 flag）——**绝不上报真实 flag 值**。这一点 4.2 会再展开。

Go 侧 `Data` 结构由代码生成器驱动，结构本身手工维护但受 schema 约束：

[internal/controller/telemetry/collector.go:41-81](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/collector.go#L41-L81) —— 顶部 `//go:generate` 指令调用 `telemetry-exporter/cmd/generator -type=Data -scheme -scheme-protocol=NGFProductTelemetry -scheme-df-datatype=ngf-product-telemetry`：它读这个 `Data` 结构，**反向生成** `data.avdl` schema 与 `data_attributes_generated.go`。所以 schema 和 Go 结构谁是「源」需要留意——Go 结构里的字段注释会被搬运进 schema 的注释，二者强绑定。

[internal/controller/telemetry/collector.go:83-158](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/collector.go#L83-L158) —— `NGFResourceCounts` 嵌入结构，集中了所有「计数」字段（Gateway/Route/Secret/Service/Endpoint/各类 Policy/Filter 计数）。它也带自己的 `//go:generate`（`-type=NGFResourceCounts`），单独生成其 `Attributes()` 片段。

生成出来的属性映射（**不要手改**）：

[internal/controller/telemetry/data_attributes_generated.go:13-33](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/data_attributes_generated.go#L13-L33) —— `Attributes()` 把每个字段转成一个 `attribute.KeyValue`（OTLP 属性）。第一行固定写入 `dataType = "ngf-product-telemetry"`（对应 schema 的 `@df_datatype`），随后是各字段。结尾 `var _ ngxTelemetry.Exportable = (*Data)(nil)` 是编译期断言：保证 `Data` 实现了导出器要求的 `Exportable` 接口。

#### 4.1.4 代码实践

1. **实践目标**：亲手确认「schema 字段、Go 结构字段、上报属性」三者一一对应，建立「契约驱动」的直觉。
2. **操作步骤**：
   - 打开 [data.avdl](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/data.avdl)，数一下 `record Data { ... }` 里的字段总数。
   - 打开 [collector.go 的 `Data` 结构](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/collector.go#L44-L81)，对比字段名与注释。
   - 打开 [data_attributes_generated.go 的 `Attributes()`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/data_attributes_generated.go#L13-L33)，确认每个字段都对应一行 `attribute.*`。
3. **需要观察的现象**：三处的字段名（如 `SnippetsPoliciesDirectivesCount`）严格一致；schema 里凡是 `long?` 对应 Go `int64`、OTLP `Int64`；凡是 `boolean?` 对应 Go `bool`、OTLP `Bool`。
4. **预期结果**：你能画出一张「schema 字段 → Go 字段 → OTLP 属性 key」的三列对照表。如果你在 `Data` 里加一个字段却忘了更新 schema 生成，`Attributes()` 不会自动包含它——这就是为什么必须走 `go:generate`。
5. 本实践为源码阅读型，**待本地验证**：可运行 `go generate ./internal/controller/telemetry/...` 观察生成器是否改动文件（注意：本仓库该生成依赖 `-tags generator`，详见 `//go:generate` 指令）。

#### 4.1.5 小练习与答案

**练习 1**：schema 里所有业务字段都写成 `类型? 字段名 = null;`，目的是什么？

**答案**：保证 Avro schema 的前向/后向兼容。新增字段时，旧解析端遇到不认识的字段会取默认值 `null` 而不报错；发送端读不到某字段时也填 `null`。这样 NGF 升级后端点解析端无需同步升级。

**练习 2**：`dataType` 字段的值 `"ngf-product-telemetry"` 来自哪里？

**答案**：来自 schema 顶部的 `@df_datatype("ngf-product-telemetry")` 注解，被代码生成器读出，在 `Attributes()` 第一行硬编码写入 `attribute.String("dataType", "ngf-product-telemetry")`，用于让下游端点区分这是哪种产品的遥测记录。

---

### 4.2 数据采集：DataCollector

#### 4.2.1 概念说明

有了数据模型，下一步是「运行时把数据填进去」。这是 `DataCollector` 的职责。它的输入是**当前集群的真实状态**，输出是一条 `Data`。

关键设计：采集器**只读不写**，且读取的是「最新图（`GetLatestGraph`）」和「最新配置（`GetLatestConfiguration`）」这两个快照——也就是说，它不重复做图构建的工作，而是复用控制面主链路已经算好的结果。这呼应了 u4-l3/u5 的结论：图是单一数据源，遥测只是它的一个「只读消费者」。

#### 4.2.2 核心流程

`Collect(ctx)` 一次采集走这几步：

```text
1. GetLatestGraph()            ← 拿最新 Graph（nil 则直接报错返回）
2. CollectClusterInformation() ← 查 API server：NodeList/Namespaces/kube-system UID
3. collectGraphResourceCount() ← 数 Graph 里的资源/策略/路由/端点
4. getPodReplicaSet()          ← 通过 Pod → ReplicaSet → Deployment 拿副本数与安装 ID
5. collectSnippetsFilter/PoliciesDirectives() ← 统计 snippets 指令
6. getNginxPodCount()          ← 按 Gateway 的 EffectiveNginxProxy 算数据面 Pod 数
7. 组装 Data{...} 字面量返回
```

任何一步出错都返回 `error`、**整条采集失败**（不会上报半条数据）。周期 Worker 拿到 error 只打日志、本次跳过（见 4.3）。

#### 4.2.3 源码精读

采集入口 `Collect`，体现「拿图 → 拿集群信息 → 数资源 → 拿副本 → 组装」的顺序：

[internal/controller/telemetry/collector.go:264-329](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/collector.go#L264-L329) —— 开头 `GetLatestGraph()` 为 nil 立即 `errors.New(...)` 返回（L266-268）；随后每个子步骤都用 `fmt.Errorf("...: %w", err)` 包裹原始错误返回（采集失败要带上下文）。最后 L303-326 是一个**单一 `Data{...}` 字面量装配**：`tel.Data{...}` 填公共头、`NGFResourceCounts: graphResourceCount` 填计数、其余字段逐个填入。注意 `BuildOS` 来自环境变量 `BUILD_OS`，缺省回退 `"alpine"`（L298-301）。

集群信息采集——这里能看出哪些字段是「集群画像」：

[internal/controller/telemetry/collector.go:522-561](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/collector.go#L522-L561) —— `CollectClusterInformation` 做四件事：① `List` 全部 Node 算 `NodeCount`（L525-534，节点数为 0 直接报错）；② 取第一个 Node 的 `KubeletVersion` 解析成 `ClusterVersion`（L538-544，解析失败回退 `"unknown"`）；③ `List` 全部 Namespace 配合 Node 算 `Platform`（L546-551）；④ 取 `kube-system` Namespace 的 UID 当 `ClusterID`（L553-558）。`ClusterID` 用 kube-system 的 UID 是业界惯用法——它稳定、唯一、不暴露用户业务。

资源/策略计数——遥测里字段最多的一块，几乎全部来自 Graph：

[internal/controller/telemetry/collector.go:331-391](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/collector.go#L331-L391) —— `collectGraphResourceCount`：`GatewayClassCount = len(IgnoredGatewayClasses)` 再 +1（若有 Winner，L338-341）；各类 Route 计数委托给 `computeRouteCount`（L345）；`SecretCount`/`ServiceCount` 直接取 `ReferencedSecrets`/`ReferencedServices` 长度（L352-353）；`EndpointCount` 遍历 Configuration 的 Upstreams 累加（L355-361，跳过有 `ErrorMsg` 的无效 upstream）；策略与过滤器计数委托 `CountPolicies`/`CountFilters`（L363-364）；最后算 `GatewayAttachedNpCount`、`ListenerSetCount`、`InferencePoolCount`、`WAFEnabledGatewayCount`。

策略计数的分发逻辑，体现「按 Kind 分流」：

[internal/controller/telemetry/collector.go:160-201](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/collector.go#L160-L201) —— `CountPolicies` 遍历 `g.NGFPolicies`，按 `policyKey.GVK.Kind` 用 `switch` 分发：`ClientSettingsPolicy`/`RateLimitPolicy`/`ProxySettingsPolicy`/`WAFPolicy` 调 `countPolicyTargetRefs` 分别累计「挂 Gateway」与「挂 Route」两个计数；`WAFPolicy` 还额外按 `Spec.Type`（HTTP/NIM/N1C/PLM）累计来源计数（L183-194，这正是 u10-l1 讲的四种 bundle 来源在遥测里的投影）。注意这些 `Kind` 常量来自 `internal/framework/kinds`，与 u3-l3 的 CRD 注册同源。

**flag 脱敏**——本讲最重要的隐私点。采集器只拿到 `cfg.Flags.Names/Values`，而这两个切片在进入 `Config` 前已被 `parseFlags` 脱敏：

[cmd/gateway/commands.go:989-1010](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L989-L1010) —— `parseFlags` 遍历所有 flag：布尔 flag 直接记录 `true/false`（L996-997）；**非布尔 flag 永远不记录真实值**——只比较「当前值是否等于默认值」，相等记 `"default"`、不等记 `"user-defined"`（L998-1004）。所以像 `--gateway-ctlr-name`、`--usage-report-secret` 这类可能含敏感信息的 flag，上报的只是「你改没改过它」，不是「你填了什么」。这正是 schema 注释里 `'default'/'user-defined'` 的来源。

#### 4.2.4 代码实践

1. **实践目标**：验证 flag 脱敏——确认无论你给非布尔 flag 填什么值，遥测里都只剩 `default`/`user-defined`。
2. **操作步骤**：
   - 阅读 [parseFlags](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L989-L1010)，确认非布尔分支只产出两个字符串字面量。
   - 在 [collector.go 的 `Data` 装配处](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/collector.go#L317-L318) 看到 `FlagNames: c.cfg.Flags.Names` / `FlagValues: c.cfg.Flags.Values`，确认采集器原样搬运、不做二次加工。
3. **需要观察的现象**：从 `parseFlags` 的输出到 `Data.FlagValues`，链路上没有任何地方读取 `flag.Value.String()` 的真实内容用于上报（只在比较 `== flag.DefValue` 时用了一次）。
4. **预期结果**：你能向团队说明「NGF 上报了用户改了哪些 flag，但不知道改成了什么值」——这对合规审查是个关键结论。
5. 本实践为源码阅读型，**待本地验证**：如想眼见为实，可在测试里构造一个 `pflag.FlagSet`、塞入一个非默认值的字符串 flag，调用 `parseFlags` 断言返回 `"user-defined"`。

#### 4.2.5 小练习与答案

**练习 1**：`ClusterID` 为什么用 `kube-system` Namespace 的 UID，而不是随机生成一个 UUID 存下来？

**答案**：① 稳定——只要集群不重建，kube-system 的 UID 不变，多次上报可关联到同一集群；② 无需额外存储——不必维护一个 UUID 的 ConfigMap/Secret；③ 不暴露用户业务——它只是一个 K8s 内部标识。代价是：若有人删了 kube-system 又重建（极端情况），ClusterID 会变。

**练习 2**：`EndpointCount` 为什么遍历的是 Configuration 的 Upstreams，而不是 Graph 里的 Endpoints？

**答案**：见 [collector.go:355-361](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/collector.go#L355-L361)：它累加的是**最终下发到数据面**的有效端点，并跳过 `ErrorMsg != ""` 的无效 upstream。这比 Graph 里的原始引用更贴近「真实承载流量的后端规模」，且复用了 u5-l3 经 `ServiceResolver` 解析、去重后的就绪端点结果。

---

### 4.3 周期上报与导出

#### 4.3.1 概念说明

采集到 `Data` 后，要决定「多久采一次、谁来采、发去哪、发不出去怎么办」。NGF 的方案是：

- **多久采一次**：默认 24 小时，带约 10 分钟抖动（避免所有集群同时打爆端点）。
- **谁来采**：只有 **leader** 副本采（多副本不重复上报）。
- **何时开始采**：首张图构建完成之前**不采**（`ReadyCh` 就绪门）——避免在控制面还没就绪、Graph 为 nil 时上报噪声。
- **发去哪**：配置了 `telemetryEndpoint` 就走 OTLP/gRPC 外发；没配置就用 `LoggingExporter` 只打日志（开发/本地场景）。
- **发不出去怎么办**：周期 Worker 吞掉错误只打日志，下一周期重试——不阻塞控制面主链路。

#### 4.3.2 核心流程

```text
StartManager
  └─ registerTelemetry(cfg, ...)
       │  if !ProductTelemetryConfig.Enabled → 直接 return（遥测整体关闭）
       ├─ NewDataCollectorImpl(DataCollectorConfig{...})   ← 注入 GraphGetter/ConfigurationGetter/K8sClientReader/Flags...
       └─ createTelemetryJob(cfg, collector, healthChecker.getReadyCh())
            ├─ 选 Exporter：
            │    Endpoint != "" → tel.NewExporter (OTLP/gRPC, 走/不走 TLS 看 EndpointInsecure)
            │    else           → LoggingExporter (只打日志)
            └─ 返回 runnables.Leader{ CronJob{ Worker, Period, JitterFactor, ReadyCh } }
                 └─ mgr.Add(job)  ← 注册进 controller-runtime Manager
```

`CronJob.Start` 的运行期行为：先 `select` 等 `ReadyCh` 关闭（图就绪）或 ctx 取消；就绪后用 `wait.JitterUntilWithContext` 以 `Period`+抖动周期性调用 `Worker`，`sliding=true` 表示每次 Worker 跑完才重新计算下一次间隔。

#### 4.3.3 源码精读

装配入口，体现「未启用即早退」：

[internal/controller/manager.go:429-465](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L429-L465) —— `registerTelemetry` 第一行 `if !cfg.ProductTelemetryConfig.Enabled { return nil }`（L437-439）：只要 `--product-telemetry-disable` 为真，整个遥测（采集 + 导出 + 周期任务）都不装配，连 goroutine 都不启。随后构造 `DataCollectorImpl`，注意它注入的依赖：`GraphGetter: processor`（变更处理器提供最新图）、`ConfigurationGetter: eventHandler`（事件处理器提供最新配置）、`K8sClientReader: mgr.GetAPIReader()`（**用 API Reader 直读 API server，不走缓存**，保证集群信息是实时的）、`NginxOneConsoleConnection` 由「是否配了 DataplaneKeySecretName」推导（L452）。最后 `createTelemetryJob` + `mgr.Add`。

构造 Leader+CronJob+导出器：

[internal/controller/manager.go:1229-1274](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L1229-L1274) —— `createTelemetryJob`：若 `Endpoint != ""`（L1238）走真实导出器，用 `otlptracegrpc.WithEndpoint(...)`，并按 `EndpointInsecure` 决定是否 `WithInsecure()`（L1244-1246），再 `tel.NewExporter(...)` 包成 span 导出（L1249-1258）；否则用 `LoggingExporter`（L1259-1261）。返回值是 `&runnables.Leader{ Runnable: runnables.NewCronJob(...) }`（L1263-1273）——这正是 u3-l4 讲的「`Leader` 套 `CronJob`」：只有 leader 才会真正跑这个 CronJob。

抖动因子常量：

[internal/controller/manager.go:1225-1227](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L1225-L1227) —— `telemetryJitterFactor = 10.0 / (24 * 60)`：注释说明「10 分钟抖动足够」。对默认 24h 周期，抖动上限 = `jitterFactor * period ≈ 10min`，即实际上报间隔在 24h ± 10min 内随机分布，避免全球所有 NGF 实例在同一秒冲击端点。

周期 Worker——把「采集 + 导出」串成一次执行：

[internal/controller/telemetry/job_worker.go:17-40](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/job_worker.go#L17-40) —— `CreateTelemetryJobWorker` 返回一个 `func(ctx)`：先 `dataCollector.Collect(ctx)`（L27），失败只 `logger.Error` 后 `return`（L28-31，本次跳过）；成功后 `exporter.Export(ctx, &data)`（L36），失败同样只记日志（L37-38）。这个 `func(ctx)` 就是 CronJob 的 `Worker`。

周期任务的就绪门与抖动调度：

[internal/framework/runnables/cronjob.go:41-57](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/cronjob.go#L41-L57) —— `Start` 先 `select { case <-ReadyCh: case <-ctx.Done(): }`（L42-47）：`ReadyCh` 关闭前 Worker 一次都不跑——对应 `registerTelemetry` 传入的 `healthChecker.getReadyCh()`（u4-l3 里首图构建完成后才关闭的通道）。就绪后 `wait.JitterUntilWithContext(ctx, Worker, Period, JitterFactor, sliding=true)`（L51-53）。`sliding=true` 意味着若某次 Worker 卡很久，下一次间隔从 Worker 返回后算起，不会出现间隔被吞掉后的密集补偿。

导出器接口与日志占位实现：

[internal/controller/telemetry/exporter.go:14-34](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/exporter.go#L14-L34) —— `Exporter` 接口只有一个方法 `Export(ctx, tel.Exportable) error`（L14-16），参数类型是库里的 `Exportable`（任何实现了 `Attributes()` 的对象，`Data` 正是）。`LoggingExporter.Export` 只是 `logger.Info("Exporting telemetry", "data", data)`（L32）——本地开发没配端点时，你能直接在日志里看到整条 `Data`，是调试字段清单最快的方式。

#### 4.3.4 代码实践

1. **实践目标**：在本地（不连任何外部端点）看到一条完整的遥测 `Data`，核对字段清单。
2. **操作步骤**：
   - 在本地或 kind 集群里跑控制面，**不要**注入 `telemetryEndpoint`（即留空）。此时 `createTelemetryJob` 会选 `LoggingExporter`（[manager.go:1259-1261](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L1259-L1261)）。
   - 把 `--product-telemetry-disable` 设为 `false`（即默认开启），并确保进程是 leader 且首图已构建。
   - 等待一个上报周期（开发时可临时把构建期 `telemetryReportPeriod` 调短后重新 `make build`，因为它不是运行时 flag）。
3. **需要观察的现象**：控制面日志出现一行 `telemetryExporter ... "Exporting telemetry" "data"={...}`，展开 `data` 即可看到 `ProjectVersion`、`ClusterID`、`FlagNames`/`FlagValues`、`NGFResourceCounts` 全部字段。
4. **预期结果**：你拿到一张真实环境的字段快照，可与 `data.avdl` 逐一比对。
5. 本实践依赖运行中的集群与 leader 身份，**待本地验证**。若只想看结构而不跑集群，可直接读 [data_test.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/data_test.go) 里 `TestDataAttributes` 构造的样例 `Data`。

#### 4.3.5 小练习与答案

**练习 1**：为什么遥测 Job 用 `runnables.Leader` 包装，而不是 `LeaderOrNonLeader`？

**答案**：因为遥测上报的是**集群维度的汇总画像**（集群 ID、资源计数、flag 使用情况）。若每个副本都上报，同一份数据会被上报 N 次，既浪费带宽又污染统计。用 `Leader` 保证整个集群同一时间只有一个副本上报（呼应 u3-l4 的「集群维度写/统计用 `Leader`」判定法则）。

**练习 2**：`ReadyCh`（首图就绪门）对遥测有什么具体好处？

**答案**：避免在控制面启动早期、Graph 还是 nil 时上报。`Collect` 第一步就要求 `GetLatestGraph()` 非 nil（[collector.go:265-268](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/collector.go#L265-L268)），否则直接报错。`ReadyCh` 让 CronJob 在首图构建完成前根本不启动，从源头消除了这类必然失败的采集，也让「安装后多久第一次上报」可预期。

---

### 4.4 配置入口：哪些能被 flag 关闭

> 这一节直接回答本讲的实践任务。

#### 4.4.1 概念说明

NGF 的产品遥测有三类「配置项」，必须严格区分它们的来源，才能回答「哪些能用 flag 关」：

| 配置项 | 来源 | 运行时能否用 `--` 改 | 默认值 |
|---|---|---|---|
| **是否启用遥测** | 运行时 CLI flag `--product-telemetry-disable` | ✅ 能 | `false`（即默认**开启**） |
| **上报周期** `ReportPeriod` | 构建期注入 `main.telemetryReportPeriod`（ldflags `-X`） | ❌ 不能 | 见 Makefile/镜像构建注入 |
| **上报端点** `Endpoint` | 构建期注入 `main.telemetryEndpoint`（ldflags `-X`） | ❌ 不能 | 空字符串（空→走 `LoggingExporter`） |
| **端点是否走 TLS** `EndpointInsecure` | 构建期注入 `main.telemetryEndpointInsecure`（ldflags `-X`） | ❌ 不能 | 见构建注入 |

核心结论：**运行时唯一能关掉产品遥测的开关是 `--product-telemetry-disable`**，它是一个**总闸**——一旦置真，`registerTelemetry` 第一行就 `return`，采集器、导出器、CronJob 一个都不装配。周期、端点、TLS 都是**构建期注入**的包级变量，不能在运行时通过命令行调整（这点与 u1-l3 讲的「版本号、遥测配置在链接期注入」一致）。

另外注意：**字段层面没有「逐字段开关」**。你不能选择「只上报集群信息、不上报 flag」。要么整体上报（按 schema 全字段），要么整体关闭。

#### 4.4.2 核心流程

```text
构建期（make build / 镜像构建）
  go build -ldflags "-X main.version=... -X main.telemetryReportPeriod=... -X main.telemetryEndpoint=... -X main.telemetryEndpointInsecure=..."
      ↓ 注入
  main.go 包级变量: version / telemetryReportPeriod / telemetryEndpoint / telemetryEndpointInsecure

运行期（controller 命令 RunE）
  productTelemetryDisable  ← 来自 --product-telemetry-disable（唯一运行时开关）
      ↓ 装配
  config.ProductTelemetryConfig{
     Enabled:          !productTelemetryDisable,
     ReportPeriod:     period,                 // ← time.ParseDuration(telemetryReportPeriod)
     Endpoint:         telemetryEndpoint,
     EndpointInsecure: telemetryEndpointInsecure,
  }
```

#### 4.4.3 源码精读

构建期注入的包级变量（**不是** flag）：

[cmd/gateway/main.go:9-18](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/main.go#L9-L18) —— `version`、`telemetryReportPeriod`、`telemetryEndpoint`、`telemetryEndpointInsecure` 四个包级 `string`，注释写明 `// Set during go build`。它们由 Makefile 的 ldflags `-X` 填充，运行时没有对应的 `--` flag。

运行期解析构建期变量并装配 `ProductTelemetryConfig`：

[cmd/gateway/commands.go:264-278](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L264-L278) —— `time.ParseDuration(telemetryReportPeriod)` 把构建期字符串解析成 `time.Duration`（L264-267，解析失败直接让 controller 启动失败——所以构建期注入的值必须合法）；`telemetryEndpoint` 非空时调 `validateEndpoint` 校验（L269-273）；`strconv.ParseBool(telemetryEndpointInsecure)`（L275-278）。

[cmd/gateway/commands.go:319-324](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L319-L324) —— `config.ProductTelemetryConfig{ ReportPeriod: period, Enabled: !disableProductTelemetry, Endpoint: telemetryEndpoint, EndpointInsecure: telemetryEndpointInsecure }`。注意 `Enabled` 取反自 `disableProductTelemetry`，这就是「反向布尔」惯用法（u2-l2 讲过）。

唯一的运行时 flag 注册：

[cmd/gateway/commands.go:472-477](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L472-L477) —— `cmd.Flags().BoolVar(&disableProductTelemetry, productTelemetryDisableFlag, false, "Disable the collection of product telemetry.")`。默认 `false` 意味着**遥测默认开启**——这是 NGF 的产品决策（默认收集匿名使用统计以改进产品），用户若介意可显式 `--product-telemetry-disable`。

对应的配置结构：

[internal/controller/config/config.go:131-141](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L131-L141) —— `ProductTelemetryConfig` 四字段 `Endpoint/ReportPeriod/EndpointInsecure/Enabled`，与上文装配一一对应。注意 `Enabled` 注释明确写「flag for toggling the collection of product telemetry」。

flag 结构（脱敏上报用）：

[internal/controller/config/config.go:161-169](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L161-L169) —— `Flags{ Names, Values }`，注释重申「Values 为 true/false 或 default/user-defined」。

#### 4.4.4 代码实践（本讲主任务）

**实践目标**：列出 telemetry 上报的字段清单，并明确指出哪些能通过 flag 关闭。

**操作步骤**：

1. 打开 [data.avdl](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/data.avdl)，把 `record Data` 的字段按下表分类抄出（示例分类，请你自行补全计数）：

   | 分类 | 代表字段（举例） |
   |---|---|
   | 公共元数据 | `dataType`、`eventTime`、`ingestTime` |
   | 产品/构建信息 | `ProjectName`、`ProjectVersion`、`ProjectArchitecture`、`ImageSource`、`BuildOS` |
   | 集群画像 | `ClusterID`、`ClusterVersion`、`ClusterPlatform`、`ClusterNodeCount`、`InstallationID` |
   | 命令行 flag（脱敏） | `FlagNames`、`FlagValues` |
   | 资源计数 | `GatewayCount`、`HTTPRouteCount`、`SecretCount`、`ServiceCount`、`EndpointCount` …（共 30+ 项，见 `NGFResourceCounts`） |
   | snippets 指令统计 | `SnippetsFiltersDirectives`(+Count)、`SnippetsPoliciesDirectives`(+Count) |
   | 规模/连接 | `NginxPodCount`、`ControlPlanePodCount`、`NginxOneConnectionEnabled` |

2. 回答「哪些可以通过 flag 关闭」。请在源码里找到证据并写下结论：
   - **能**：`--product-telemetry-disable`（[commands.go:472-477](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L472-L477)）——总闸，置真后 `registerTelemetry` 早退（[manager.go:437-439](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L437-L439)），所有字段都不再采集上报。
   - **不能（运行时）**：周期、端点、是否 TLS 是构建期注入（[main.go:9-18](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/main.go#L9-L18)），只能改镜像/构建。
   - **不能（字段级）**：没有「只关某个字段」的开关，是 schema 全字段整体上报。

**需要观察的现象**：通读 `cmd/gateway/commands.go` 全部 `cmd.Flags()` 注册，确认与 telemetry 相关的运行时 flag **只有** `--product-telemetry-disable` 一个；`telemetryReportPeriod` 等从未出现在 `cmd.Flags()` 调用里。

**预期结果**：你能写出一句准确结论——「NGF 产品遥测在运行时只能整体开关（`--product-telemetry-disable`），无法逐字段或逐项关闭；周期/端点/TLS 属于构建期注入，需改镜像才能调整。」

> 说明：本实践为源码阅读型，结论可仅凭读码得出；如要在集群里验证「关闭后不再上报」，可在本地用 `LoggingExporter` 观察：加 `--product-telemetry-disable` 后日志里不再出现 `Exporting telemetry` 行（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：用户在 `values.yaml` 里把 `--product-telemetry-disable=true` 传给控制面，遥测链路上哪些对象不会被创建？

**答案**：`ProductTelemetryConfig.Enabled` 为 `false`，`registerTelemetry` 在 [manager.go:437-439](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L437-L439) 第一行 `return nil`，于是 `DataCollectorImpl`、`Exporter`（无论 Logging 还是 OTLP）、`CronJob`、`runnables.Leader` 都不会被创建，`mgr.Add` 也不会被调用——零 goroutine、零外发。

**练习 2**：为什么把 `telemetryReportPeriod`/`telemetryEndpoint` 设计成构建期注入，而不是运行时 flag？

**答案**：这两个值与「上报到哪里、多久上报」相关，属于产品/构建决策而非运维调参。把它们焊在镜像里可以：① 避免误改端点把遥测发去错误地址；② 让官方发布的镜像自带正确的上报目标；③ 减少运行时 flag 数量。代价是改它们必须重新构建镜像——这对 NGF 这类「端点固定、由项目方维护」的场景是合理的取舍。

## 5. 综合实践

把本讲三块知识串起来，完成一次「**为 NGF 的产品遥测做一次合规审查**」：

**背景**：有安全团队问你——「NGF 默认开启的遥测，到底把我们的集群信息发去了哪、发了什么、怎么关？」

**任务**：

1. **发了什么**：基于 [data.avdl](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/data.avdl) 与 [collector.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/collector.go) 产出一份字段清单，逐项标注「是否含敏感信息」。重点核实：
   - `FlagValues` 是否含真实 flag 值（结论：不含，见 [parseFlags](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L989-L1010)）。
   - `ClusterID` 是什么（结论：kube-system 的 UID，见 [collectClusterID](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/telemetry/collector.go#L503-L512)）。
   - 是否包含任何用户业务数据 / 路由内容 / 请求体（结论：否，只有计数与脱敏 flag）。
2. **发去哪**：基于 [createTelemetryJob](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L1229-L1274) 说明端点来自构建期注入的 `telemetryEndpoint`，未注入时只打本地日志、不外发。
3. **怎么关**：基于 [commands.go:472-477](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L472-L477) 与 [manager.go:437-439](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L437-L439) 给出关闭方法：`--product-telemetry-disable=true`，并说明这是总闸、关闭后零外发。
4. **频率与身份**：补充说明默认 24h 一次、仅 leader 上报、首图就绪后才启动（[cronjob.go:41-57](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/runnables/cronjob.go#L41-L57)、[manager.go:1225-1227](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L1225-L1227)）。

**交付物**：一份一页纸的审查结论，能用源码行号支撑每一条断言。如果你能用 `LoggingExporter` 在本地跑出一条真实 `Data` 截图附上，说服力更强（**待本地验证**）。

## 6. 本讲小结

- **产品遥测 ≠ 指标**：遥测是低频（默认 24h）、主动外发、给项目方看集群画像；指标是高频、本地暴露、给运维看运行时信号。
- **契约驱动**：`data.avdl` 是唯一契约，所有字段 nullable 以保证 schema 演进；Go `Data` 结构、`Attributes()` 由 `go:generate` 同步生成，三者不可手工拆离。
- **采集只读复用图**：`DataCollector.Collect` 不重算，直接读 `GetLatestGraph`/`GetLatestConfiguration` 与 API server，产出一条 `Data`；任一步失败整条采集丢弃、不报半条。
- **flag 脱敏**：`parseFlags` 对非布尔 flag 只上报 `default`/`user-defined`，绝不外泄真实值；布尔 flag 只报 true/false。
- **总闸式开关**：运行时唯一能关掉产品遥测的 CLI flag 是 `--product-telemetry-disable`（默认开启）；周期/端点/TLS 是构建期注入（ldflags `-X`），非运行时 flag；字段层面无逐项开关。
- **可靠性三件套**：仅 leader 上报（`runnables.Leader`）、首图就绪后才启动（`ReadyCh`）、10 分钟抖动避免雷鸣（`telemetryJitterFactor`），失败只打日志不阻塞主链路。

## 7. 下一步学习建议

- **横向对比**：回到 **u11-l1（Prometheus 指标）**，并排对比「指标 vs 遥测」在采集频率、暴露方式、消费者、可关闭性上的差异，建立 NGF 可观测性的完整心智。
- **顺着数据流往下**：遥测的「资源计数」字段全部来自 Graph，建议重读 **u5-l1（Graph 构建）** 与 **u8-l2（CRD 与策略附着）**，理解为何 `NGFPolicies` 一个 map 就能覆盖几乎所有策略计数。
- **Leader/CronJob 机制**：若想彻底弄懂「仅 leader + 周期 + 就绪门」的通用模式，重读 **u3-l4（Leader Election 与 Runnables）**，本讲的遥测 Job 是其最典型的产品化实例。
- **NGINX One Console**：本讲提到 `NginxOneConnectionEnabled` 由 `NginxOneConsoleTelemetryConfig.DataplaneKeySecretName` 推导（[config.go:171-181](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L171-L181)）。这是另一条「连上 NGINX One 控制台」的上报链路，建议后续结合部署文档深入。
- **链路追踪**：本单元下一讲 **u11-l3（OpenTelemetry 链路追踪）** 会讲数据面的请求级追踪（`ObservabilityPolicy` + otel 模板），与本讲的「控制面产品遥测」是不同层面的可观测性，注意区分。
