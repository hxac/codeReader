# Prometheus 指标

## 1. 本讲目标

本讲讲解 NGINX Gateway Fabric（NGF）控制面如何采集、注册、暴露 Prometheus 指标。学完后你应该能够：

- 看清一条指标「从被观测到出现在 `/metrics` 端点」的完整链路：观测点 → Collector → 注册表 → metrics server → HTTP 端点。
- 读懂 `ControllerCollector` 中唯一一个控制器指标 `event_batch_processing_milliseconds` 直方图的定义（桶、单位、常量标签）。
- 区分**控制面指标端点**（由 `--metrics-port` 决定，默认 9113）与**数据面 NGINX 指标端点**（由 `NginxProxy.Metrics` 决定）两条相互独立的配置路径。
- 用 PromQL 基于直方图写出一条「事件批次处理过慢」的告警思路。

本讲依赖 u3-l1（StartManager 装配控制面），因为指标采集器的注册正发生在 StartManager 的装配阶段。

## 2. 前置知识

阅读本讲前，最好已经了解以下概念：

- **Prometheus 指标类型**：Prometheus 有四种核心指标类型——Counter（单调递增计数器）、Gauge（可增可减的瞬时值）、Histogram（分桶累计计数，适合耗时分布）、Summary（客户端算分位数）。本讲的「事件批次处理耗时」用 **Histogram**，因为耗时分布天然适合分桶。
- **直方图的桶（bucket）**：Histogram 把每次观测值落入预先划分好的若干个桶，每个桶记录「≤该上界的累计观测次数」，即 Prometheus 中的 `_bucket{le="..."}` 序列。Prometheus 端用 `histogram_quantile()` 函数从这些累计桶插值估算分位数（如 p95）。
- **`prometheus.Collector` 接口**：Go 客户端 `prometheus/client_golang` 中，任何想被采集的对象都要实现 `Describe(chan<- *Desc)` 与 `Collect(chan<- Metric)` 两个方法。NGF 的 `ControllerCollector` 就是一个自定义 Collector。
- **controller-runtime 的 metrics 包**：`sigs.k8s.io/controller-runtime/pkg/metrics` 暴露一个进程级全局 Prometheus 注册表 `metrics.Registry`，controller-runtime 自身（如 reconciliation 次数、工作队列深度）和用户自定义指标都注册到这里，最终由 metrics server 在 `/metrics` 暴露。
- **直方图分位数插值公式**：给定一组累计桶计数，第 \(q\) 分位数落在第 \(i\) 个桶内时，Prometheus 用线性插值估算：
  \[ q \approx b_i + (b_{i+1} - b_i) \cdot \frac{r - c_i}{c_{i+1} - c_i} \]
  其中 \(b_i, b_{i+1}\) 是相邻桶上界，\(c_i, c_{i+1}\) 是对应累计计数，\(r\) 是目标分位数对应的累计次数。这正是 `histogram_quantile()` 内部做的事。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [internal/controller/metrics/metrics.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/metrics/metrics.go) | 仅定义全部 NGF 自有指标共享的命名空间常量。 |
| [internal/controller/metrics/collectors/controller.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/metrics/collectors/controller.go) | `ControllerCollector`：控制面唯一自定义 Collector，定义 `event_batch_processing_milliseconds` 直方图及其 no-op 版本。 |
| [internal/controller/manager.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go) | `createMetricsCollector` 把 Collector 注册进全局注册表；`getMetricsOptions` 配置 metrics server 的监听地址与是否启用 TLS。 |
| [internal/controller/handler.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go) | 事件批次处理器在每批处理结束时把耗时 `Observe` 进 Collector——这是指标真正的「观测点」。 |
| [internal/controller/config/config.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go) | `MetricsConfig` 结构与默认端口常量 `DefaultNginxMetricsPort = 9113`。 |
| [apis/v1alpha2/nginxproxy_types.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha2/nginxproxy_types.go) | 数据面侧的 `Metrics{Port, Disable}` 类型，配置 NGINX 自身被 Prometheus 抓取的端口。 |

一条贯穿全讲的链路：`handler.HandleEventBatch` 计时 → `metricsCollector.Observe...` → `ControllerCollector.eventBatchProcessDuration`（Histogram）→ `createMetricsCollector` 在装配期把它 `MustRegister` 进 `metrics.Registry` → controller-runtime metrics server 在 `/metrics` 暴露。

---

## 4. 核心概念与源码讲解

### 4.1 指标注册：从 Collector 到全局注册表

#### 4.1.1 概念说明

「注册」要解决的问题是：一个 Go 进程里可能有很多指标，Prometheus 怎么知道它们的存在？答案是一个**注册表（Registry）**。Prometheus 客户端维护注册表，抓取时遍历其中所有 Collector，调用其 `Collect()` 方法拿到全部时间序列。

NGF 没有自建注册表，而是复用 controller-runtime 提供的进程级全局注册表 `sigs.k8s.io/controller-runtime/pkg/metrics.Registry`。这样做的好处是：controller-runtime 自己的指标（reconcile 次数、错误数、工作队列深度等）和 NGF 自定义指标会从同一个 `/metrics` 端点一起暴露，运维只需抓一个地址。

注册还必须决定「注册谁、什么时候注册、注册不注册」。NGF 给了一个开关：当 `--metrics-disable` 关闭指标时，连 Collector 也不创建，改用一个 no-op 占位对象，避免业务代码出现 nil 指针。

#### 4.1.2 核心流程

注册流程发生在控制面装配期（u3-l1 的 StartManager 阶段），分四步：

1. 装配事件处理器时调用 `createMetricsCollector(cfg)`。
2. 读 `cfg.MetricsConfig.Enabled`：关 → 返回 `ControllerNoopCollector`；开 → 继续。
3. 开时构造常量标签 `{"class": cfg.GatewayClassName}`（让多实例共存时按 GatewayClass 区分），创建 `ControllerCollector`。
4. 调 `metrics.Registry.MustRegister(handlerCollector)` 把它挂进全局注册表；返回的 Collector 注入到事件处理器配置中。

返回的 Collector 之后被注入 `eventHandlerConfig.metricsCollector` 字段，供每批事件结束时调用（见 4.2）。

#### 4.1.3 源码精读

先看命名空间常量——所有 NGF 自有指标的 Prometheus 指标名都带这个前缀：

[internal/controller/metrics/metrics.go:3-3](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/metrics/metrics.go#L3-L3) —— 定义 `Namespace = "nginx_gateway_fabric"`，它会被拼到每个指标的 `Name` 前面，形成完整的指标名（如 `nginx_gateway_fabric_event_batch_processing_milliseconds`）。

接下来是装配期的注册工厂：

[internal/controller/manager.go:289-301](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L289-L301) —— `createMetricsCollector` 是注册的核心：先判断 `Enabled`，关则返回 no-op；开则构造 `class` 常量标签并 `metrics.Registry.MustRegister(handlerCollector)`。注意 `MustRegister` 若注册失败会直接 panic，因此这里要求指标定义（同名、同标签集合）稳定且唯一。

注入点——装配事件处理器时把返回的 Collector 作为依赖塞进去：

[internal/controller/manager.go:226-226](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L226-L226) —— `metricsCollector: createMetricsCollector(cfg)` 出现在 `newEventHandlerImpl(eventHandlerConfig{...})` 的参数列表里，体现了「装配期创建一次、运行期反复调用」的依赖注入模式。

> 关键结论：注册只发生一次（装配期），但观测（Observe）会随每一批事件反复发生。注册表把「指标定义」和「指标数据采集」解耦。

#### 4.1.4 代码实践（源码阅读型）

实践目标：理解 no-op 占位模式为何能避免 nil 指针，并追踪注册调用的唯一性。

操作步骤：

1. 打开 `internal/controller/metrics/collectors/controller.go`，找到 `ControllerNoopCollector`（见 4.2）的实现——它对 `ObserveLastEventBatchProcessTime` 提供空方法体。
2. 打开 `internal/controller/handler.go`，确认 `eventHandlerConfig.metricsCollector` 字段类型是一个**接口**，而 no-op 和真实 Collector 都实现了该接口。
3. 在 `internal/controller/manager.go` 全局搜索 `MustRegister`，确认全仓库控制面侧只在 `createMetricsCollector` 里注册一次自定义 Collector。

需要观察的现象：no-op 与真实 Collector 共用同一个接口 `handlerMetricsCollector`，因此 handler 调用方代码完全不变，开关指标时只切换注入的实现。

预期结果：你会确认「关闭指标 = 注入 no-op，handler 代码零改动」，这就是依赖倒置带来的可测试、可开关收益。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `MustRegister` 换成两次注册同一个 Collector，会发生什么？
答案：Prometheus 注册表不允许重复注册同一指标描述（Desc），`MustRegister` 会在第二次调用时 panic，进程启动失败。这强制指标定义全局唯一。

**练习 2**：为什么用 controller-runtime 的全局 `metrics.Registry` 而不是 `prometheus.NewRegistry()` 自建一个？
答案：复用全局注册表可以让 controller-runtime 自带指标（如 controller、webhook、workqueue 相关指标）与 NGF 自定义指标从同一个 `/metrics` 端点一并暴露，运维只需配置一个抓取地址，且无需自己把 metrics server 与注册表对接。

---

### 4.2 ControllerCollector：唯一的控制器指标

#### 4.2.1 概念说明

`ControllerCollector` 是 NGF 控制面目前**唯一**自定义的 Collector，只采集一个指标：**事件批次处理耗时（event batch processing duration）**。

为什么选这个指标？回顾 u4-l2、u4-l3：控制面的核心工作是「收到一批资源变更事件 → 构建图 → 生成 NGINX 配置 → 下发给 Agent → reload」。一批事件从进入 `HandleEventBatch` 到返回的总时长，直接反映控制面「消化变更」的健康度：

- 耗时短且稳定 = 控制面健康，能快速把用户 YAML 落地成 NGINX 行为。
- 耗时长（例如逼近或超过 5s、10s）= 图构建/配置生成/下发出现瓶颈，集群规模变大或资源配置复杂时尤其需要关注。

用 **Histogram** 而非 Gauge 的原因：耗时是分布型的，单次值噪声大，分桶累计才能算出 p95、p99 这类「长尾」指标，而长尾往往才是要告警的。

> 注意一个容易误读的细节：观测方法是 `ObserveLastEventBatchProcessTime`，名字里有「Last」，但实现是把它 `Observe` 进直方图——也就是说**每一批事件都累积一次**，并不是只保留最近一次的 Gauge。直方图里能看到历史分布。

#### 4.2.2 核心流程

观测流程与 4.3.3（u4-l3）里的事件批次主流程绑定：

1. `HandleEventBatch` 进入时记录 `start := time.Now()`。
2. 处理整批事件（捕获 → Process 建图 → 下发 → 入队状态）。
3. `defer` 中计算 `duration := time.Since(start)`，调用 `metricsCollector.ObserveLastEventBatchProcessTime(duration)`。
4. Collector 把 `duration` 转成毫秒（`float64(duration / time.Millisecond)`）后 `Observe` 进直方图。
5. Prometheus 抓取时，注册表调用该 Collector 的 `Collect()`，把直方图的全部 `_bucket` / `_sum` / `_count` 序列吐到 `/metrics`。

直方图会把耗时（毫秒）归入下列桶（上界，含 +Inf）：

| 桶上界 le | 含义 |
| --- | --- |
| 500 | 处理 ≤ 0.5 秒的批次累计次数 |
| 1000 | 处理 ≤ 1 秒 |
| 5000 | 处理 ≤ 5 秒 |
| 10000 | 处理 ≤ 10 秒 |
| 30000 | 处理 ≤ 30 秒 |
| +Inf | 全部（= `_count`） |

#### 4.2.3 源码精读

`ControllerCollector` 结构体只持有一个直方图：

[internal/controller/metrics/collectors/controller.go:13-16](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/metrics/collectors/controller.go#L13-L16) —— 结构体字段 `eventBatchProcessDuration prometheus.Histogram`，目前唯一一个控制器指标。

直方图的完整定义（指标名、命名空间、帮助文本、常量标签、桶）在构造函数里：

[internal/controller/metrics/collectors/controller.go:19-32](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/metrics/collectors/controller.go#L19-L32) —— `NewControllerCollector` 用 `prometheus.HistogramOpts` 定义：`Name` 为 `event_batch_processing_milliseconds`，拼上 `Namespace` 得到完整名；`Buckets` 是 `[500,1000,5000,10000,30000]`（毫秒，Prometheus 客户端会自动追加 `+Inf`）；`ConstLabels` 接收注册时传入的 `class=<GatewayClassName>`。

观测方法把 Go 的 `time.Duration` 转成毫秒浮点数：

[internal/controller/metrics/collectors/controller.go:35-37](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/metrics/collectors/controller.go#L35-L37) —— `ObserveLastEventBatchProcessTime` 调 `c.eventBatchProcessDuration.Observe(float64(duration / time.Millisecond))`，整数除法截断掉不足 1 毫秒的部分。

实现 `prometheus.Collector` 接口的两方法，把内部直方图代理出去：

[internal/controller/metrics/collectors/controller.go:40-47](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/metrics/collectors/controller.go#L40-L47) —— `Describe` 与 `Collect` 直接委托给内部直方图，使本结构体成为合法 Collector。

no-op 占位（关闭指标时使用，见 4.1）：

[internal/controller/metrics/collectors/controller.go:49-58](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/metrics/collectors/controller.go#L49-L58) —— `ControllerNoopCollector` 的 `ObserveLastEventBatchProcessTime` 是空方法体，调用它什么都不做。

观测点在事件批次处理器的入口与出口：

[internal/controller/handler.go:189-200](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L189-L200) —— `HandleEventBatch` 用 `start := time.Now()` 与 `defer func(){... ObserveLastEventBatchProcessTime(duration) }()` 包裹整批处理，保证无论正常返回还是中途逻辑分支，耗时都会被记录。注意这是**每批**都观测一次，即便 `Process` 返回 nil（无变更、不重配），批次耗时任然计入直方图。

接口定义让 no-op 与真实实现可互换：

[internal/controller/handler.go:45-47](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L45-L47) —— `handlerMetricsCollector` 接口只声明 `ObserveLastEventBatchProcessTime(time.Duration)`，正是上面两个实现共同满足的最小契约。

#### 4.2.4 代码实践（核心实践任务）

实践目标：找到事件批次处理耗时指标的定义，并用 PromQL 写一条告警思路。

操作步骤：

1. 在 `internal/controller/metrics/collectors/controller.go` 第 19–32 行定位 `event_batch_processing_milliseconds` 直方图，确认桶为 `[500,1000,5000,10000,30000]`、常量标签为 `class`。
2. 确认完整指标名为 `nginx_gateway_fabric_event_batch_processing_milliseconds`，因此抓取后会出现三个序列族：`..._bucket{le="...",class="..."}`、`..._sum{class="..."}`、`..._count{class="..."}`。
3. 用 `histogram_quantile` 算 p95 耗时（毫秒），写一条「p95 处理耗时超过 5 秒持续 5 分钟即告警」的 PromQL。

需要观察的现象 / 预期结果：

```promql
# 估算过去 5 分钟内，事件批次处理耗时的 p95（毫秒），按 class 分组
histogram_quantile(
  0.95,
  sum by (le, class) (rate(nginx_gateway_fabric_event_batch_processing_milliseconds_bucket[5m]))
) > 5000
```

也可以用「超过 5 秒的批次占比」写告警（避免分位数插值噪声）：

```promql
# 过去 5 分钟内，处理超过 5 秒（5000ms）的批次占比超过 10%
(
  1 -
  (
    sum by (class) (rate(nginx_gateway_fabric_event_batch_processing_milliseconds_bucket{le="5000"}[5m]))
    /
    sum by (class) (rate(nginx_gateway_fabric_event_batch_processing_milliseconds_count[5m]))
  )
) > 0.10
```

> 说明：`le="5000"` 是「处理 ≤5s 的累计桶」，用总 `_count` 减去它、再除以 `_count`，即得「>5s 的占比」。两条 PromQL 都是「**待本地验证**」的思路——实际阈值（5 秒、10%）应结合你的集群规模与 SLO 调整。

#### 4.2.5 小练习与答案

**练习 1**：为什么 5000 正好是桶边界？把 p95 阈值定在 5000 有什么好处？
答案：5000 是预先设定的桶上界之一。`histogram_quantile` 在桶边界处插值结果最准（落在该桶内的样本不会跨桶插值），把告警阈值对齐桶边界能减少估算误差。

**练习 2**：若想知道「最近一次批次」的精确耗时而不是分布，现有指标够吗？
答案：不够。Histogram 只给分布（分位数、累计计数、总和），不给「最近一次」的瞬时值。要精确拿最近一次，需要额外加一个 Gauge（每次 `Set` 最新耗时），目前代码里没有。

**练习 3**：方法名带「Last」却用直方图 `Observe`，是否名不副实？
答案：是，命名具有误导性——它实际是「每次都累积进直方图」，而非只记录最近一次。阅读源码时应以实现（`Observe`）为准，而非方法名。

---

### 4.3 metrics 端口配置：控制面与数据面两条独立路径

#### 4.3.1 概念说明

「metrics 端口配置」在 NGF 里有**两套相互独立**的配置，初学者极易混淆：

1. **控制面指标端点**：控制面容器自己进程暴露的 `/metrics`（包含 `event_batch_processing_milliseconds`、controller-runtime 指标等），由 **flag** `--metrics-port` 控制，配置存在 `config.MetricsConfig`。
2. **数据面 NGINX 指标端点**：数据面 NGINX 进程暴露的 `/metrics`（NGINX 自身的连接数、状态等，供 Prometheus 抓取），由 **CRD 字段** `NginxProxy.Metrics` 控制。

两者默认端口都是 **9113**（`DefaultNginxMetricsPort`），但归属完全不同：一个是控制面 flag、一个是数据面 CRD 字段。**本讲聚焦控制面指标**，数据面端口配置仅作对照说明，帮你看清「9113」为何在多处出现。

另一个关键点：9113 是**受保护端口**。图层会把它（或你在 `NginxProxy.Metrics.Port` 指定的端口）加入 `ProtectedPorts`，禁止任何 Gateway Listener 绑定该端口，避免数据面监听器与指标端点抢端口。

#### 4.3.2 核心流程

控制面端口配置流程：

1. flag `--metrics-port`（默认 9113）与反向布尔 `--metrics-disable`、`--metrics-secure-serving` 经 u2-l2 的三步声明模式读入值变量。
2. controller 命令的 `RunE` 把它们装进 `config.MetricsConfig{Port, Enabled, Secure}`。
3. StartManager 里 `getMetricsOptions(cfg.MetricsConfig)` 把它翻译成 controller-runtime 的 `metricsserver.Options{BindAddress, SecureServing}`。
4. controller-runtime manager 用该 Options 起 metrics server：禁用 → `BindAddress: "0"`（不监听）；启用 → `:<port>`，可选 HTTPS。

数据面端口配置流程（对照）：

1. 用户在 `NginxProxy` CRD 写 `spec.metrics.port` / `spec.metrics.disable`。
2. 图层 `MetricsEnabledForNginxProxy` 解析出 `(port, enabled)`，默认 `enabled=true, port=nil`（nil 表示用默认 9113）。
3. `buildProtectedPorts` 把该端口加入受保护端口集合，禁止 Listener 占用。
4. provisioner 用它生成数据面 Deployment 里 NGINX 容器的容器端口与 Agent 配置。

#### 4.3.3 源码精读

控制面侧——默认端口常量与配置结构：

[internal/controller/config/config.go:10-10](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L10-L10) —— `DefaultNginxMetricsPort = int32(9113)`，控制面 flag 默认值与数据面默认端口共用这个常量。

[internal/controller/config/config.go:103-111](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L103-L111) —— `MetricsConfig{Port, Enabled, Secure}` 三字段：端口、是否启用、是否 HTTPS。

flag 到 Config 的装配（u2-l2 的「单一字面量装配」阶段）：

[cmd/gateway/commands.go:308-312](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L308-L312) —— controller 命令把 `!disableMetrics`、`metricsListenPort.value`、`metricsSecure` 装进 `MetricsConfig`，注意用的是「反向布尔」惯用法（`--metrics-disable` → `Enabled` 取反，见 u2-l2）。

Config 到 controller-runtime Options 的翻译：

[internal/controller/manager.go:1405-1416](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L1405-L1416) —— `getMetricsOptions`：禁用时返回 `BindAddress: "0"`（controller-runtime 约定 `0` 表示不监听）；启用时拼 `:<port>`，`Secure` 为真则开 `SecureServing`（HTTPS）。这一段是控制面指标端点的最终决定点。

Options 注入 manager：

[internal/controller/manager.go:513-513](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L513-L513) —— `Metrics: getMetricsOptions(cfg.MetricsConfig)` 作为 `manager.Options` 的一部分传入，由 controller-runtime 起 metrics server 并在 `/metrics` 暴露。

数据面侧（对照）——CRD 字段定义：

[apis/v1alpha2/nginxproxy_types.go:268-281](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha2/nginxproxy_types.go#L268-L281) —— `Metrics{Port *int32, Disable *bool}`：`Port` 端口范围 1–65535，`Disable` 关闭数据面指标。两者都是指针、可选，缺省即用默认。

图层解析（数据面是否启用、端口多少）：

[internal/controller/state/graph/nginxproxy.go:196-207](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/nginxproxy.go#L196-L207) —— `MetricsEnabledForNginxProxy`：未配置 `Metrics` 或未 `Disable` 时返回 `(nil, true)`（启用、用默认端口）；`Disable` 为真返回 `(nil, false)`；否则返回 `(port, true)`。

受保护端口（禁止 Listener 抢占 9113）：

[internal/controller/state/graph/gateway.go:461-472](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/gateway.go#L461-L472) —— `buildProtectedPorts` 在指标启用时把指标端口（默认 9113 或自定义）加入 `ProtectedPorts["MetricsPort"]`，后续 Listener 校验会拒绝绑定该端口。

数据面容器端口生成（provisioner 消费同一解析结果）：

[internal/controller/provisioner/objects.go:612-616](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/objects.go#L612-L616) —— provisioner 也调 `MetricsEnabledForNginxProxy`，把端口写进数据面 NGINX 容器配置（容器端口与 Agent 抓取配置）。

> 关键结论：控制面端口走 flag（`--metrics-port`，`config.MetricsConfig`，`getMetricsOptions`），数据面端口走 CRD（`NginxProxy.Metrics`，`MetricsEnabledForNginxProxy`）。两者都默认 9113、都由同一常量 `DefaultNginxMetricsPort` 提供默认值，但配置入口与消费链路完全分离。

#### 4.3.4 代码实践

实践目标：动手验证控制面指标端点的三种开关组合，并对照理解数据面端口。

操作步骤：

1. 阅读 `internal/controller/manager_test.go` 中 `getMetricsOptions` 的表驱动测试（约 505–540 行），它列出了「禁用 / 启用非安全 / 启用且 HTTPS」三种 `MetricsConfig` 与期望 `Options` 的对照。
2. 用 helm 在 kind 中部署 NGF（沿用 u1-l4），确认控制面容器默认监听 9113：`kubectl -n nginx-gateway port-forward <nginx-gateway-pod> 9113:9113`，再 `curl -s localhost:9113/metrics | grep event_batch_processing`，应能看到直方图序列。
3. 在 `NginxProxy` 里写 `spec.metrics.port: 8113`，重新部署后确认数据面 NGINX 容器端口随之变化（`kubectl describe` 数据面 Pod 的 containerPort），并理解此时 Gateway Listener 不能再用 8113。

需要观察的现象：

- 步骤 2 中，`curl /metrics` 输出应包含 `nginx_gateway_fabric_event_batch_processing_milliseconds_bucket{class="nginx",le="5000"}` 等行（`class` 取你的 GatewayClass 名）。
- 步骤 3 中，若强行让某 Listener 用 8113，Gateway 应出现端口被占用的错误条件。

预期结果：

- 控制面 `/metrics` 在 9113 暴露 `event_batch_processing_milliseconds` 直方图。
- 改 `NginxProxy.Metrics.Port` 只影响**数据面**端口，不影响控制面；控制面端口只能通过 `--metrics-port` flag 改。
- 上述端到端抓取与端口切换为**待本地验证**——具体输出取决于你的部署版本与 GatewayClass 名。

#### 4.3.5 小练习与答案

**练习 1**：把控制面 `--metrics-port` 改成 9114，数据面 `NginxProxy.Metrics.Port` 保持默认，会发生什么？
答案：控制面 `/metrics` 改在 9114 暴露；数据面 NGINX 指标仍在 9113。两者互不影响——这正说明它们是两条独立链路。Prometheus 需分别抓取两个端口。

**练习 2**：为什么要把 metrics 端口加入 `ProtectedPorts`？
答案：数据面 NGINX 同一进程里，Listener 的监听端口和指标端点端口共用端口空间。若允许某个 Gateway Listener 绑定 9113，就会和指标端点冲突，导致二者都无法正常工作。图层提前在受保护端口集合里拦截，给出明确错误条件。

**练习 3**：`getMetricsOptions` 在禁用时返回 `BindAddress: "0"` 而非空字符串，为什么？
答案：这是 controller-runtime 的约定——`"0"` 表示不监听任何地址（关闭 metrics server）。空字符串在某些版本会被当作默认监听，语义不安全；显式 `"0"` 是「关闭」的稳定信号。

---

## 5. 综合实践

设计一个把本讲三个模块串起来的小任务：**「端到端观测一次事件批次的处理耗时」**。

1. 用 helm 在 kind 部署 NGF（控制面默认在 9113 暴露指标）。
2. `kubectl -n nginx-gateway port-forward <pod> 9113:9113`，打开 `curl -s localhost:9113/metrics`，确认能看到 `nginx_gateway_fabric_event_batch_processing_milliseconds_count`。
3. 记下此刻的 `_count` 与 `_sum` 值（基线）。
4. 触发一次变更：`kubectl apply` 一个新的 HTTPRoute（或修改一个已有路由），这会让控制面处理一批事件。
5. 再次 `curl /metrics`，对比 `_count` 是否增加（说明新批次被处理并观测）、`_sum` 是否增加（说明有耗时被累积）。
6. 用本讲 4.2.4 的 PromQL 在本地 `promtool` 或 Prometheus 里算一次 p95，验证公式可跑通。

这个任务串起了：端口配置（9113 从哪来）→ 注册（指标为何出现在 `/metrics`）→ Collector（`_count`/`_sum` 为何随批次增长）。

> 端到端结果**待本地验证**：`_count` 的增量取决于变更触发了多少批事件（受 u4-l2 双缓冲批处理影响，可能合并成少于「你操作次数」的批次）。

## 6. 本讲小结

- NGF 控制面复用 controller-runtime 的全局 `metrics.Registry`，装配期 `createMetricsCollector` 用 `MustRegister` 把唯一自定义 Collector 注册进去，关闭指标时改注入 no-op 占位以避免 nil 指针。
- `ControllerCollector` 目前只有一个指标 `event_batch_processing_milliseconds`（直方图，桶 `[500,1000,5000,10000,30000]` ms，常量标签 `class`），在 `HandleEventBatch` 的 `defer` 中每批观测一次。
- 该指标名带「milliseconds」后缀但单位是毫秒、方法名带「Last」但实为直方图累积——读源码以实现为准。
- metrics 端口有两条独立链路：控制面走 flag `--metrics-port`（`config.MetricsConfig` + `getMetricsOptions`），数据面走 CRD `NginxProxy.Metrics`（`MetricsEnabledForNginxProxy`），二者都默认 9113。
- 9113 是受保护端口：图层 `buildProtectedPorts` 禁止 Gateway Listener 占用指标端口。
- 用 `histogram_quantile` 或「超阈值批次占比」可基于该直方图写控制面处理过慢的告警。

## 7. 下一步学习建议

- 接 **u11-l2 产品遥测（Telemetry）**：区别于本讲的 Prometheus 指标（被动抓取），telemetry 是控制面**主动周期上报**的产品数据（集群规模、版本等），二者机制不同，值得对照学习。
- 想深入「指标为何每批都观测、批处理如何合并变更」，回到 **u4-l2 事件循环与批处理（双缓冲）** 与 **u4-l3 EventHandler 编排**。
- 若关注数据面 NGINX 自身指标（NGINX Plus 的 API 暴露、连接数、上游健康等），可顺带阅读 provisioner 生成数据面容器端口的相关代码（`internal/controller/provisioner/objects.go`），本讲仅作入口介绍。
- 想为 NGF 新增一个自定义指标：参考 `ControllerCollector` 与 `createMetricsCollector` 的写法——实现 `prometheus.Collector`、在工厂里 `MustRegister`、在业务点 `Observe`。
