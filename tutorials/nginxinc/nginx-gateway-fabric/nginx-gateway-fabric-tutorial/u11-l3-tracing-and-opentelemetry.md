# OpenTelemetry 链路追踪

## 1. 本讲目标

本讲讲解 NGINX Gateway Fabric（NGF）如何为 HTTP/gRPC 路由开启**分布式链路追踪（distributed tracing）**。学完后你应当能够：

1. 看懂用户侧 API `ObservabilityPolicy` 的 `Tracing` 字段（策略、采样比、上下文传播、span 属性），并理解它为何必须与 `NginxProxy` 全局遥测配置配合使用。
2. 说清一份 `ObservabilityPolicy` 是如何经「图构建 → `dataplane.Telemetry` → 模板渲染」变成 NGINX 的 `ngx_otel_module` 指令的，区分「全局 exporter 模板」与「每路由指令模板」两条渲染路径。
3. 理解 ratio 采样如何用 `split_clients` 实现、parent 采样如何用 `$otel_parent_sampled` 实现，以及上下文如何经 `extract/inject/propagate/ignore` 四种模式传播。

本讲承接 u8-l3《策略到 NGINX 指令的生成》（策略生成器的组合模式、`Generator` 接口按上下文分发），并属于「可观测性」单元（u11）。

## 2. 前置知识

### 2.1 先澄清一个易混点：NGF 里有「三套 telemetry」

`telemetry` 这个词在 NGF 里出现在三个互不相干的子系统，初学者极易混淆：

| 名称 | 是什么 | 谁配置 | 频率/去向 | 本手册讲义 |
| --- | --- | --- | --- | --- |
| **Prometheus 指标** | 控制面/数据面本地暴露的 metrics | 运维 | 高频、本地 `/metrics` | u11-l1 |
| **产品遥测（Product Telemetry）** | 上报给 NGF 项目方的集群画像 | 项目方构建期注入 | 低频（约 24h）、外发 | u11-l2 |
| **OpenTelemetry 链路追踪** | **每条请求的 trace/span** | 应用开发者 + 集群运维 | **每请求**、发往 OTLP collector | **本讲 u11-l3** |

本讲只讲第三种：请求级别的分布式追踪。它的代码常量、变量名（如 `$otel_*`、`dataplane.Telemetry`、`telemetry.go` 模板）与 u11-l2 的「产品遥测」（`internal/controller/telemetry/` 包、`data.avdl`）**完全没有关系**，只是恰好同名。

### 2.2 分布式追踪的最小概念

- **Trace**：一次请求穿越多个服务形成的完整调用链，由唯一 `trace-id` 标识。
- **Span**：调用链中的一个工作单元（如「NGINX 转发这次 /hello」），含起止时间、名字、属性（key/value）。
- **Trace Context（W3C traceparent/tracestate）**：请求头里携带的上下文，让下游服务知道「我属于哪条 trace、父 span 是谁、这条 trace 是否被采样」。
- **Sampling（采样）**：流量大时不可能每条请求都记录 span，需要按比例或按父 span 决定是否记录。
- **OpenTelemetry**：上述概念的工业标准。NGINX 通过第三方模块 [`ngx_otel_module`](https://nginx.org/en/docs/ngx_otel_module.html) 实现它，NGF 的工作就是把用户意图翻译成该模块的指令。

### 2.3 本讲要承接的 u8-l3 结论

u8-l3 讲过策略生成器的「组合模式三件套」：`Generator` 接口（按 main/http/server/location/internal-location 五个 NGINX 上下文分发）、`CompositeGenerator`（只转发拼接）、`UnimplementedGenerator`（嵌入后只重写关心的上下文）。本讲的 observability 生成器正是这套框架的一个具体实现，它只重写 `GenerateForLocation` 与 `GenerateForInternalLocation` 两个方法。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [apis/v1alpha2/observabilitypolicy_types.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha2/observabilitypolicy_types.go) | 用户侧 CRD：`ObservabilityPolicy`、`Tracing`、采样策略与上下文枚举 |
| [apis/v1alpha2/nginxproxy_types.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha2/nginxproxy_types.go) | `NginxProxy.Telemetry`：集群运维侧的全局 OTLP exporter 配置 |
| [apis/v1alpha1/shared_types.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/shared_types.go) | `SpanAttribute` 共享类型（key/value） |
| [internal/controller/state/dataplane/configuration.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go) | `buildTelemetry`：把 CRD 配置加工成渲染模型 `dataplane.Telemetry` |
| [internal/controller/state/dataplane/types.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/types.go) | 渲染模型 `Telemetry`、`Ratio`、`SpanAttribute` |
| [internal/controller/nginx/config/telemetry.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/telemetry.go) | `executeTelemetry`：渲染全局 `otel_exporter`/`split_clients` 到 `http.conf` |
| [internal/controller/nginx/config/telemetry_template.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/telemetry_template.go) | 全局 otel 模板文本 |
| [internal/controller/nginx/config/policies/observability/generator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/observability/generator.go) | 每路由 otel 指令模板 + 采样策略计算 `getStrategy` |
| [internal/controller/nginx/config/policies/observability/validator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/observability/validator.go) | 校验：策略/上下文枚举、spanName 防注入、依赖 NginxProxy、冲突判定 |
| [internal/controller/nginx/config/generator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go) | 总装：注册 observability 生成器、把 `executeTelemetry` 纳入模板执行链 |
| [internal/controller/nginx/config/servers.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go) | 在 location 生成处调用 `GenerateForLocation`/`GenerateForInternalLocation` |
| [tests/suite/tracing_test.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/suite/tracing_test.go) + [tests/suite/manifests/tracing/](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/suite/manifests/tracing/policy-single.yaml) | 端到端追踪测试与示例 manifest |

## 4. 核心概念与源码讲解

NGF 的追踪采用**两层职责分离**，这是理解全篇的钥匙：

- **集群运维**（Cluster Operator）在 `NginxProxy` 上配置**全局**的 OTLP collector 地址、服务名、全局 span 属性——决定「trace 发往哪里」。
- **应用开发者**（App Developer）用 `ObservabilityPolicy` 附着到具体 HTTPRoute/GRPCRoute——决定「哪些路由开追踪、采样多少、加什么属性」。

两层缺一不可：没有全局 endpoint，策略被判定为不可用；没有策略，则不会对任何路由生效。

### 4.1 ObservabilityPolicy：用户侧的追踪开关

#### 4.1.1 概念说明

`ObservabilityPolicy` 是一个 **Direct Attached Policy**（直接附着策略，不向下继承）。它通过 `targetRefs` 挂到一个或多个 HTTPRoute/GRPCRoute 上，只对这些路由的 location 生效。它的 `Spec.Tracing` 描述「怎么追踪」，但**不**描述「trace 发往哪里」——后者属于 `NginxProxy`。

关键设计动机：把「基础设施参数」（collector 地址，运维关心）与「业务参数」（采样率、自定义属性，开发者关心）拆开，分别交给不同角色，避免应用开发者误改集群级 collector 配置。

#### 4.1.2 核心流程

`ObservabilityPolicy` 从 YAML 到生效的链路（仅本模块涉及的「定义与校验」段）：

```
用户提交 ObservabilityPolicy YAML
        │
        ▼  (API server 准入)
CEL 校验：targetRef 只能是 HTTPRoute/GRPCRoute、group 固定、name+kind 唯一；
          ratio 仅在 strategy=ratio 时允许。
        │
        ▼  (控制面图层 BuildGraph 最后一步 processPolicies，见 u8-l2)
observability.Validator.Validate        → 校验策略/上下文枚举、spanName 防变量注入
observability.Validator.ValidateGlobalSettings → 校验 NginxProxy 存在且 telemetry 已启用
observability.Validator.Conflicts       → 两个策略同时带 Tracing 视为冲突
        │
        ▼  (policy.Source = *ObservabilityPolicy 进入 graph.NGFPolicies)
交由配置生成器消费（4.2、4.3）
```

#### 4.1.3 源码精读

**（1）类型定义与「Direct」标签。** 注意第 16 行的 kubebuilder 标签 `policy=direct`，它声明这是直接附着策略（见 u8-l2）：

[apis/v1alpha2/observabilitypolicy_types.go:16-30](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha2/observabilitypolicy_types.go#L16-L30) —— `policy=direct` 标签 + `ObservabilityPolicy` 结构，`Status` 复用 Gateway API 的 `gatewayv1.PolicyStatus`（用于回写 Accepted 等条件）。

**（2）`TargetRefs` 的 CEL 准入校验。** 四条 `XValidation` 在 API server 侧就挡住非法值，无需等控制面：

[apis/v1alpha2/observabilitypolicy_types.go:56-61](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha2/observabilitypolicy_types.go#L56-L61) —— 限定 `MinItems=1/MaxItems=16`、kind 只能 HTTPRoute/GRPCRoute、group 必须是 `gateway.networking.k8s.io`、且 (name,kind) 组合唯一。

**（3）`Tracing` 结构。** 这是本讲的「主角」字段：

[apis/v1alpha2/observabilitypolicy_types.go:64-106](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha2/observabilitypolicy_types.go#L64-L106) —— `Tracing` 含 5 个字段：

| 字段 | 含义 | 默认 |
| --- | --- | --- |
| `Strategy` | `ratio`（按比例）或 `parent`（跟父 span） | 必填 |
| `Ratio` | 0–100 的采样百分比，仅 `ratio` 策略可用 | 100 |
| `Context` | `extract`/`inject`/`propagate`/`ignore` | 模块默认 |
| `SpanName` | span 名字，正则禁 `$` 与未转义反斜杠 | location 名 |
| `SpanAttributes` | 自定义 key/value 属性，最多 64 项 | 无 |

特别注意第 66 行的跨字段 CEL 校验——「ratio 只能在 strategy=ratio 时出现」，这是把互斥规则焊进 schema：

[apis/v1alpha2/observabilitypolicy_types.go:66](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha2/observabilitypolicy_types.go#L66) —— `!(has(self.ratio) && self.strategy != 'ratio')`。

**（4）两个枚举。** 策略与上下文都是严格枚举：

[apis/v1alpha2/observabilitypolicy_types.go:108-139](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha2/observabilitypolicy_types.go#L108-L139) —— `TraceStrategy`（ratio/parent）与 `TraceContext`（extract/inject/propagate/ignore）的枚举与常量。四种上下文语义：`extract` 继承上游 trace、`inject` 覆盖写新上下文、`propagate` 二者结合、`ignore` 跳过。

**（5）校验器：三层守门。**

[internal/controller/nginx/config/policies/observability/validator.go:27-45](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/observability/validator.go#L27-L45) —— `Validate` 逐个 targetRef 校验 kind/group，再调 `validateSettings`。

[internal/controller/nginx/config/policies/observability/validator.go:48-65](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/observability/validator.go#L48-L65) —— **关键**：`ValidateGlobalSettings` 要求 `NginxProxy` 存在且 `TelemetryEnabled` 为真，否则策略被置为 NotAccepted。这正是「两层职责分离」的强制点——没有全局 endpoint，单个策略无法生效。

[internal/controller/nginx/config/policies/observability/validator.go:68-73](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/observability/validator.go#L68-L73) —— `Conflicts`：两个策略都带 `Tracing` 即判冲突（由 u8-l2 的 `markConflictedPolicies` 按优先级裁决）。

[internal/controller/nginx/config/policies/observability/validator.go:119-139](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/observability/validator.go#L119-L139) —— `spanName` 与每个 `SpanAttribute` 的 key/value 都过 `ValidateEscapedStringNoVarExpansion`，**禁止 NGINX 变量展开**（`$`），防止注入（这是把不可信用户输入拼进 NGINX 配置前的安全闸门，与 u8-l3 的 genericValidator 一脉相承）。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：用一个真实测试 manifest 印证上面字段，并手工走一遍校验。

**操作步骤**：

1. 阅读示例 [tests/suite/manifests/tracing/policy-single.yaml:1-14](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/suite/manifests/tracing/policy-single.yaml#L1-L14)：
   - `targetRefs` 指向 `HTTPRoute/hello`；
   - `tracing.strategy: ratio`（未设 `ratio`，故按默认 100% 采样）；
   - 一个 `spanAttributes: testkey2=testval2`。

2. 设想把它改成 `strategy: parent` 同时保留 `ratio: 50`，对照 [第 66 行 CEL](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha2/observabilitypolicy_types.go#L66) 判断：`has(ratio)=true` 且 `strategy!='ratio'` → 校验失败。

3. 设想把 `spanName` 设为 `some-$value`，对照 [validator.go:119-126](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/observability/validator.go#L119-L126) 判断：含 `$` → `ValidateEscapedStringNoVarExpansion` 报错。

**需要观察的现象**：两种非法写法分别在「API server 准入」与「控制面 Validate」阶段被拒，对应不同的错误返回点。

**预期结果**：parent+ratio 被 CEL 拒绝；含 `$` 的 spanName 被控制面 `Validate` 拒绝并回写 `PolicyInvalid` 条件。

> 说明：本实践为「源码阅读型」，无需运行集群即可完成推理。

#### 4.1.5 小练习与答案

**练习 1**：一个 `ObservabilityPolicy` 同时 `targetRefs` 了 `HTTPRoute/hello` 和 `HTTPRoute/world`，且都带 `tracing`。会生效吗？
**答案**：会。`MaxItems=16` 允许多 targetRef，只要 (name,kind) 组合唯一。这正是 [policy-multiple.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/suite/manifests/tracing/policy-multiple.yaml) 的场景，对应 [tracing_test.go:211-231](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/suite/tracing_test.go#L211-L231) 的测试用例。

**练习 2**：为什么 `ObservabilityPolicy` 里没有 OTLP endpoint 字段？
**答案**：职责分离。endpoint 属集群基础设施，由运维写在 `NginxProxy.Telemetry`；策略只管业务级参数。`ValidateGlobalSettings` 强制了这一约束。

### 4.2 otel 模板：从全局 exporter 到每路由指令

#### 4.2.1 概念说明

`ngx_otel_module` 的指令分布在两个 NGINX 上下文：

- **http 上下文（全局）**：`otel_exporter`（collector 地址、批处理参数）、`otel_service_name`、以及采样用的 `split_clients`。这些**全局唯一**，由集群运维的 `NginxProxy` 决定。
- **location 上下文（每路由）**：`otel_trace`（开关 + 采样）、`otel_trace_context`（传播模式）、`otel_span_name`、`otel_span_attr`。这些由 `ObservabilityPolicy` 决定，只出现在被附着路由的 location 里。

NGF 用**两套模板**分别渲染这两层，最终都落入同一份 `conf.d/http.conf`（全局部分）或挂到 location 的 include 文件（每路由部分）。

#### 4.2.2 核心流程

```
NginxProxy.Telemetry (CRD, 运维侧)
        │  (processNginxProxies/buildEffectiveNginxProxy, u8-l2)
        ▼
gateway.EffectiveNginxProxy.Telemetry
        │  (buildTelemetry, configuration.go)
        ▼
dataplane.Telemetry {Endpoint, ServiceName, Ratios, SpanAttributes, Batch*}
        │
        ├──► executeTelemetry ──► telemetry_template.go ──► http.conf（全局 otel_exporter/split_clients）
        │
        └──► observability.NewGenerator(conf.Telemetry) ──► 每路由 include 文件（otel_trace/...）
```

`buildTelemetry` 除了搬运 NginxProxy 字段，还做了一件跨职责的事：**遍历所有 ObservabilityPolicy，收集需要的采样比变量**，塞进 `Telemetry.Ratios`，供全局模板生成对应的 `split_clients`。这是 FIXME 注释里坦承的「policy-specific 逻辑泄漏进 buildTelemetry」的折中（见 4.2.3）。

#### 4.2.3 源码精读

**（1）渲染模型 `dataplane.Telemetry`。** 这是全局模板与每路由生成器共享的数据结构：

[internal/controller/state/dataplane/types.go:678-702](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/types.go#L678-L702) —— `Telemetry`（Endpoint/ServiceName/Interval/Ratios/SpanAttributes/BatchSize/BatchCount）与 `SpanAttribute`（Key/Value）。

[internal/controller/state/dataplane/types.go:808-815](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/types.go#L808-L815) —— `Ratio`：`Name` 用作 NGINX 变量名（如 `$otel_ratio_25`），`Value` 是百分比。

**（2）`buildTelemetry`：从 CRD 到渲染模型。**

[internal/controller/state/dataplane/configuration.go:2050-2070](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L2050-L2070) —— `telemetryEnabled` 是三重判断：`EffectiveNginxProxy` 非空、`Telemetry` 非空、未在 `DisabledFeatures` 里列 `DisableTracing`、且 `Exporter.Endpoint` 已设。任一不满足返回空 `Telemetry{}`（`Endpoint==""`，下游模板就不渲染）。

[internal/controller/state/dataplane/configuration.go:2072-2120](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L2072-L2120) —— `buildTelemetry`：
- 默认 `ServiceName = "ngf:<ns>:<gw-name>"`；若用户在 NginxProxy 设了 `serviceName`，则作为后缀拼接（`ngf:ns:gw:user-svc`），与 [tracing_test.go:162](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/suite/tracing_test.go#L162) 断言一致；
- 搬运 BatchSize/BatchCount/Interval；
- 第 2104–2112 行：遍历 `g.NGFPolicies`，把每个 ratio>0 的 ObservabilityPolicy 的采样比经 `CreateRatioVarName` 转成变量名，**以变量名为键写入 map 去重**——相同采样比的多个策略共享同一个 `split_clients`。

[internal/controller/state/dataplane/configuration.go:2135-2139](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L2135-L2139) —— `CreateRatioVarName(ratio) = "$otel_ratio_<ratio>"`。

**（3）全局模板 `executeTelemetry`。** 只有 `Endpoint != ""` 才渲染：

[internal/controller/nginx/config/telemetry.go:10-23](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/telemetry.go#L10-L23) —— 把 `conf.Telemetry` 喂给 `otelTemplate`，产物 `dest = httpConfigFile`（即 `conf.d/http.conf`），与 servers/upstreams/maps 等按 dest 分桶合并进同一份 http.conf（见 u6-l1）。

[internal/controller/nginx/config/telemetry_template.go:3-25](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/telemetry_template.go#L3-L25) —— 模板三段：`otel_exporter { endpoint; interval/batch_size/batch_count(可选) }`、`otel_service_name`、以及 `range .Ratios` 逐个生成 `split_clients`（采样机制详见 4.3）。

**（4）每路由模板（observability 生成器）。** 这是 u8-l3 框架的具体实现，嵌入 `UnimplementedGenerator` 只重写两个 location 方法：

[internal/controller/nginx/config/policies/observability/generator.go:20-61](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/observability/generator.go#L20-L61) —— **三套模板**：
- `observabilityTemplate`（外发到「直接代理」的 location，全量指令）；
- `internalTemplate`（内部 location，只放 `otel_span_name` + `otel_span_attr`，**不含** `otel_trace`）；
- `externalRedirectTemplate`（当 location 需重定向到内部 location 时，只放 `otel_trace` + `otel_trace_context`）。

这种拆分的原因：当一条路由带 match 规则需要内部 location 时（或推理后端），实际处理请求、应当打 span 属性的是**内部 location**；而采样开关 `otel_trace` 必须在**外部 location** 提前决定。所以指令被刻意分置两处。

[internal/controller/nginx/config/policies/observability/generator.go:63-114](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/observability/generator.go#L63-L114) —— `NewGenerator(telemetry)` 把全局 `dataplane.Telemetry` 注入生成器（用于合并全局 span 属性）；`GenerateForLocation` 按 `location.Type` 选模板：`ExternalLocationType` 用全量模板并带 `GlobalSpanAttributes`，否则用 `externalRedirectTemplate`（不带全局属性，因为属性放内部 location）。

[internal/controller/nginx/config/policies/observability/generator.go:116-140](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/observability/generator.go#L116-L140) —— `GenerateForInternalLocation` 用 `internalTemplate`，spanName 缺省时模板写 `$request_uri_path`，并合并全局 span 属性。

**（5）总装接线。**

[internal/controller/nginx/config/generator.go:132-139](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L132-L139) —— `observability.NewGenerator(conf.Telemetry)` 被注册进 `CompositeGenerator`（与 clientsettings/snippets/proxysettings/ratelimit/waf 并列，呼应 u8-l3）。

[internal/controller/nginx/config/generator.go:233](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L233) —— `executeTelemetry` 被列入 `getExecuteFuncs` 模板执行链（u6-l1 讲过的 12 个执行函数之一）。

[internal/controller/nginx/config/servers.go:557-559](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L557-L559) 与 [internal/controller/nginx/config/servers.go:692-694](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L692-L694) —— location 生成处分别调用 `GenerateForLocation`（外部）与 `GenerateForInternalLocation`（内部），产物经 `createIncludesFromPolicyGenerateResult`（u6-l1/u8-l3）转成 `include` 挂进 location。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：用单元测试印证「采样比变量名」与「全局服务名拼接」两条规则。

**操作步骤**：

1. 读 [internal/controller/state/dataplane/configuration_test.go:4859-4862](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration_test.go#L4859-L4862) —— `TestCreateRatioVarName` 断言 `CreateRatioVarName(25) == "$otel_ratio_25"`。
2. 读 [tracing_test.go:162](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/suite/tracing_test.go#L162) —— 断言 collector 收到的 span 含 `service.name: Str(ngf:<ns>:gateway:my-test-svc)`，印证「默认名 + 用户后缀」拼接。
3. 读 [observability/generator_test.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/observability/generator_test.go)，找出 ratio/parent 两种策略各自期望生成的指令文本。

**需要观察的现象**：`getStrategy` 的返回值如何直接决定模板里 `otel_trace` 后面跟什么。

**预期结果**：ratio(默认) → `otel_trace on;`；ratio=25 → `otel_trace $otel_ratio_25;`；parent → `otel_trace $otel_parent_sampled;`（详见 4.3）。

#### 4.2.5 小练习与答案

**练习 1**：两个 ObservabilityPolicy 分别用 ratio=25 和 ratio=25（相同），会生成几个 `split_clients`？
**答案**：1 个。`buildTelemetry` 用 `ratioMap` 以变量名 `$otel_ratio_25` 为键去重（[configuration.go:2104-2117](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L2104-L2117)）。

**练习 2**：`otel_exporter` 块渲染进哪个文件？为什么？
**答案**：`conf.d/http.conf`（`httpConfigFile`，[generator.go:58](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L58)）。因为它属于 http 上下文，与 servers/upstreams 等按 dest 分桶合并（u6-l1）。

### 4.3 采样与上下文传播

#### 4.3.1 概念说明

**采样（Sampling）** 决定「这条请求要不要真的生成并上报 span」。NGF 支持两种策略：

- **ratio（按比例）**：固定比例的请求被采样。NGF 用 NGINX 的 `split_clients` 把每条请求的 trace-id 哈希后，按比例映射成 `on/off`。
- **parent（跟随父 span）**：只有当入站请求已带「父 span 被采样」标记时才记录。靠 `$otel_parent_sampled` 变量实现。

**上下文传播（Context Propagation）** 决定 NGINX 如何处理入站请求头里的 `traceparent/tracestate`：

| 模式 | 行为 |
| --- | --- |
| `extract` | 继承上游的 trace-id 与父 span，把自己挂到已有调用链上 |
| `inject` | 新建上下文并覆盖已有头 |
| `propagate` | 既继承又下发（extract+inject） |
| `ignore` | 不处理上下文头 |

#### 4.3.2 核心流程

ratio 采样的数学本质：`split_clients` 对变量值做一致性哈希，把 `[0, 2^32)` 的哈希空间前 N% 映射到 `on`。设采样比为 \(r\)（0–100），则任一请求被采样的概率为：

\[
P(\text{sampled}) = \frac{r}{100}
\]

由于是**一致性哈希**（按 trace-id），同一条逻辑请求在不同节点会得到一致的采样结论，这对全链路追踪至关重要——能保证一条 trace 在所有跳转上要么全采、要么全不采。

生成逻辑（伪代码）：

```
# 全局（telemetry_template.go），每个出现的 ratio 值生成一份：
split_clients $otel_trace_id $otel_ratio_25 {
    25% on;
    *   off;
}

# 每路由（observability generator），由 getStrategy 决定：
ratio, ratio==nil  → otel_trace on;              # 100%
ratio, ratio>0     → otel_trace $otel_ratio_25;  # 查表
ratio, ratio==0    → otel_trace off;             # 关闭
parent             → otel_trace $otel_parent_sampled;
```

`$otel_parent_sampled` 是 `ngx_otel_module` 内置变量，取自入站 `traceparent` 头的 sampled 标志位。

#### 4.3.3 源码精读

**（1）`getStrategy`：采样策略 → `otel_trace` 取值。**

[internal/controller/nginx/config/policies/observability/generator.go:142-163](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/observability/generator.go#L142-L163) —— 四个分支：
- `TraceStrategyParent` → `"$otel_parent_sampled"`；
- `TraceStrategyRatio`：`Ratio==nil` → `"on"`（100%），`Ratio>0` → `CreateRatioVarName(*Ratio)`（即 `$otel_ratio_N`），`Ratio==0` → `"off"`；
- default → `"off"`。

**（2）采样表 `split_clients` 的生成。**

[internal/controller/nginx/config/telemetry_template.go:19-24](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/telemetry_template.go#L19-L24) —— `range .Ratios` 逐个产出 `split_clients $otel_trace_id $otel_ratio_N { N% on; * off; }`。`$otel_trace_id` 是每请求的 trace-id，被哈希后按 N% 命中 `on`。

**（3）上下文传播指令。**

[internal/controller/nginx/config/policies/observability/generator.go:23-25](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/observability/generator.go#L23-L25) —— `{{- if .Tracing.Context }} otel_trace_context {{ .Tracing.Context }};` 把 `extract/inject/propagate/ignore` 原样写入，对应 [ngx_otel_module#otel_trace_context](https://nginx.org/en/docs/ngx_otel_module.html#otel_trace_context)。

**（4）span 属性的「全局 + 策略」合并。**

[internal/controller/nginx/config/policies/observability/generator.go:29-34](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/observability/generator.go#L29-L34) —— 先渲染策略自带的 `SpanAttributes`，再追加 `GlobalSpanAttributes`（来自 NginxProxy）。全局属性在 [generator.go:96](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/observability/generator.go#L96) 由 `g.telemetryConf.SpanAttributes` 注入。这正是 [tracing_test.go:203-204](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/suite/tracing_test.go#L203-L204) 同时断言 `testkey1`（全局）与 `testkey2`（策略）都出现在 span 里的根因。

**（5）span 名字缺省。**

[internal/controller/nginx/config/policies/observability/generator.go:40-44](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/observability/generator.go#L40-L44) —— 内部 location 的 spanName 缺省为 `$request_uri_path`（按请求路径命名 span），与 CRD 注释 [observabilitypolicy_types.go:88](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha2/observabilitypolicy_types.go#L88) 的描述一致。

#### 4.3.4 代码实践（源码阅读 + 配置对照型）

**实践目标**：手工推演一份 `ObservabilityPolicy` 会生成哪些 otel 指令。

**操作步骤**：给定如下策略（示例代码）：

```yaml
# 示例代码
apiVersion: gateway.nginx.org/v1alpha2
kind: ObservabilityPolicy
metadata: { name: obs, namespace: default }
spec:
  targetRefs:
  - group: gateway.networking.k8s.io
    kind: HTTPRoute
    name: hello
  tracing:
    strategy: ratio
    ratio: 25
    context: propagate
    spanName: my-span
    spanAttributes:
    - { key: env, value: prod }
```

并假设 `NginxProxy.Telemetry` 设了 endpoint `collector:4317`、`serviceName: svc`、全局属性 `env=global`。

**推演**：

1. `getStrategy`：ratio=25 → 返回 `$otel_ratio_25`。
2. 全局模板（`buildTelemetry` 把 25 收进 `Ratios`）生成：
   ```
   split_clients $otel_trace_id $otel_ratio_25 { 25% on; * off; }
   ```
3. 该路由的外部 location include 生成：
   ```
   otel_trace $otel_ratio_25;
   otel_trace_context propagate;
   otel_span_name "my-span";
   otel_span_attr "env" "prod";
   otel_span_attr "env" "global";   # 全局合并
   ```
4. **注意冲突**：步骤 3 里 `env` 同时出现 `prod` 与 `global`，这是合并顺序导致的潜在覆盖——实际以 NGINX 处理重复 `otel_span_attr` 的行为为准（**待本地验证**最终生效值）。

**需要观察的现象**：`otel_trace` 的值随策略变化（on / `$otel_ratio_N` / `$otel_parent_sampled` / off）；全局与策略属性都被渲染。

**预期结果**：见上方推演；若要在真实 NGINX 验证最终 `env` 取值，需 `kubectl exec` 进数据面 Pod 查看生成的 include 文件并实测。

#### 4.3.5 小练习与答案

**练习 1**：把 `strategy` 从 `ratio` 改成 `parent`，生成的 `otel_trace` 会变成什么？为什么不需要 `split_clients`？
**答案**：变成 `otel_trace $otel_parent_sampled;`。parent 策略依据入站请求已有的采样标记，不需要按比例哈希，故 `buildTelemetry` 不会把 parent 策略的 ratio（也不允许设）收进 `Ratios`，也就不生成 `split_clients`。

**练习 2**：为什么采样用 `split_clients` 而不是简单随机？
**答案**：`split_clients` 基于 trace-id 的一致性哈希，保证同一 trace 在多跳上一致采样，避免「半截 trace」（前半段采到了、后半段没采到）破坏全链路可读性。

## 5. 综合实践

**任务**：在本地 kind 集群里为一个 HTTPRoute 启用 25% ratio 追踪，并端到端验证「策略 → 生成的 nginx 配置 → collector 收到 span」。

> 本实践需要：一个能运行的 kind 集群、一个 OTLP collector（如 otel-collector）、以及**按 tracing 测试要求构建的 NGF 镜像**。完整可运行版本见 [tests/suite/tracing_test.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/suite/tracing_test.go)，下面是手工步骤。

**步骤**：

1. **构建期注入 endpoint**。NGF 的 OTLP endpoint 是构建期变量，必须按 [tracing_test.go:31-32](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/suite/tracing_test.go#L31-L32) 的注释构建镜像：设 `TELEMETRY_ENDPOINT=otel-collector.opentelemetry-collector.svc:4317` 与 `TELEMETRY_ENDPOINT_INSECURE=true`（依赖 collector 是否启用 TLS，**待本地验证**你的 collector 配置）。

2. **部署 collector 与示例应用**：参考 [tracing_test.go:84-94](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/suite/tracing_test.go#L84-L94)（`framework.InstallCollector` + hello-world apps/gateway/routes）。

3. **配置全局 telemetry**（运维侧）：给 `NginxProxy` 写 `spec.telemetry.exporter.endpoint` 与可选 `serviceName`/`spanAttributes`，参照 [tracing_test.go:62-73](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/suite/tracing_test.go#L62-L73)。这一步是「打开 collector 通道」，否则策略会被 `ValidateGlobalSettings` 判 NotAccepted。

4. **创建 ObservabilityPolicy**（开发侧）：用 [policy-single.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/suite/manifests/tracing/policy-single.yaml)（可改成 `ratio: 25`），apply 到路由所在 namespace。

5. **追踪配置如何进入 nginx 配置**（本讲核心验证）：
   - `kubectl get observabilitypolicy -o yaml` 确认 `status.conditions` 含 `Accepted=True`（[tracing_test.go:263-295](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/suite/tracing_test.go#L263-L295) 的校验逻辑）。
   - `kubectl exec` 进数据面 NGINX Pod：
     - `cat /etc/nginx/conf.d/http.conf | grep -A3 otel_exporter` —— 应见全局 exporter；
     - `cat /etc/nginx/conf.d/http.conf | grep -A3 split_clients` —— 应见 `$otel_ratio_25 { 25% on; * off; }`；
     - `cat /etc/nginx/conf.d/includes/ObservabilityPolicy_*_ext.conf`（**待确认**实际 include 目录与文件名，由 [generator.go:101](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/observability/generator.go#L101) 的命名规则生成）—— 应见 `otel_trace $otel_ratio_25;` 等指令。

6. **发请求、看 span**：按 [tracing_test.go:155-163](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/suite/tracing_test.go#L155-L163) 发若干请求（注意 ratio 采样下需多发才能命中），查 collector 日志应含 `service.name: Str(ngf:<ns>:<gw>:...)`、`http.method`、自定义与全局 `spanAttributes`。

**预期结果**：步骤 5 在 nginx 配置里看到「全局 exporter + split_clients + 每路由 otel 指令」三件套；步骤 6 在 collector 看到 span。若某步缺失，对照 4.1–4.3 排查（最常见是漏了步骤 3 的全局 endpoint，导致策略 NotAccepted、`http.conf` 里没有 otel 块）。

> 若无法搭建完整环境，可退化为「源码阅读型」综合实践：用 4.3.4 的策略样例，结合 `getStrategy`/`telemetry_template.go`/observability 三模板，**在纸上推演**出步骤 5 应当看到的全部 nginx 指令，再与 `observability/generator_test.go` 的期望输出比对。

## 6. 本讲小结

- NGF 的请求级追踪由**两层职责分离**驱动：运维在 `NginxProxy.Telemetry` 配全局 OTLP endpoint，开发用 `ObservabilityPolicy` 控每路由采样与属性；`ValidateGlobalSettings` 强制前者必须就绪。
- `ObservabilityPolicy` 是 Direct 附着策略，`targetRefs` 仅支持 HTTPRoute/GRPCRoute，CEL 与控制面双层校验（含 spanName 防变量注入）。
- 渲染分两套模板：**全局** `executeTelemetry`/`telemetry_template.go` 产出 `otel_exporter`+`otel_service_name`+`split_clients`（落 `http.conf`）；**每路由** observability 生成器产出 `otel_trace`/`otel_trace_context`/`otel_span_name`/`otel_span_attr`（挂 location）。
- 采样：ratio 用 `split_clients`（一致性哈希，同 ratio 去重共享），parent 用 `$otel_parent_sampled`，由 `getStrategy` 决定 `otel_trace` 取值。
- 当路由需要内部 location 时，`otel_trace` 置外部 location、`otel_span_*` 置内部 location，靠 `GenerateForLocation`/`GenerateForInternalLocation` 两个方法分别渲染。
- span 属性按「策略自带 + NginxProxy 全局」合并；服务名默认 `ngf:<ns>:<gw>`，用户值作后缀。

## 7. 下一步学习建议

- **横向对比策略生成器**：回到 u8-l3，把 observability 生成器与 ratelimit 生成器对照——ratelimit 同样横跨 http 与 location 两个上下文（`limit_req_zone` vs `limit_req`），理解「跨上下文策略」的通用设计模式。
- **追踪 NginxProxy 的合并**：读 [nginxproxy_types.go:197-266](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha2/nginxproxy_types.go#L197-L266) 与 u8-l2 的 `buildEffectiveNginxProxy`，看清 GatewayClass 级与 Gateway 级 telemetry 如何合并出 `EffectiveNginxProxy.Telemetry`。
- **深入匹配与内部 location**：本讲提到「需要内部 location 时指令分置两处」，其判定逻辑在 u6-l3 的 `needsInternalLocationsForMatches`；若你要弄清哪些 HTTPRoute match 会触发内部 location，回到 u6-l3 复习。
- **运行完整 e2e**：以 [tests/suite/tracing_test.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/suite/tracing_test.go) 为脚手架跑通真实 collector，这是验证本讲所有结论的最有力方式。
