# 可观测性：日志、指标、追踪

## 1. 本讲目标

本讲讲解 Semantic Router（SR）的「可观测性三件套」——结构化日志、Prometheus 指标、OpenTelemetry 追踪。学完后你应当能够：

- 说清楚 SR 用 **什么库、什么数据模型** 把「日志 / 指标 / 追踪」三件事分别落地；
- 在 [`main()`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/main.go#L18-L52) 启动序列中**准确定位**三者各自的初始化时机与端口，并解释「为什么有的早、有的晚」；
- 读懂一条 `startup_complete` 日志事件包含哪些字段，并知道这些字段在排障时的用途；
- 区分**全局包级指标**与**窗口化指标（windowed metrics）**两套体系，以及 `inflight` 为什么用「自定义 Collector」而不是 GaugeVec。

本讲是「控制面与可观测性」单元的一篇，依赖 u4-l1（启动主流程）建立的 `main()` 启动序列认知。

## 2. 前置知识

在进入源码前，先用一句话把三个概念区分开，它们解决的是不同的问题：

| 能力 | 回答的问题 | 数据形态 | SR 的实现 |
|------|-----------|----------|-----------|
| **日志（Logging）** | 「这件事发生了吗？当时状态是什么？」 | 离散事件（带时间戳的文本/JSON） | `zap` 结构化日志 |
| **指标（Metrics）** | 「系统整体表现如何？趋势怎样？」 | 聚合数值（计数器/直方图/仪表盘） | Prometheus client |
| **追踪（Tracing）** | 「这一次请求内部，时间花在了哪一步？」 | 单次请求的 span 树 | OpenTelemetry SDK |

三者关系可以这样记：**日志答「事件」，指标答「分布」，追踪答「单次因果链」**。三者常常共享同一个 `request_id` / `trace_id`，让你从一条慢的指标曲线，下钻到一次具体的 trace，再下钻到那条 trace 对应的日志行。

几个贯穿全讲的术语，先解释清楚：

- **结构化日志（structured logging）**：日志不再是「人读的一行字符串」，而是带字段的 JSON 对象（如 `{"event":"startup_complete","metrics_port":9190,...}`）。好处是日志聚合系统（Loki、Elasticsearch）可以按字段精确过滤，而不是用脆弱的正则匹配。
- **Prometheus 指标类型**：`Counter`（只增不减，如请求总数）、`Histogram`（分布，如延迟分布桶）、`Gauge`（可增可减的瞬时值，如当前在途请求数）。
- **span / tracer**：追踪的基本单位是一个 span（一段时间区间，带开始/结束时间、属性、父子关系）；多个 span 串成树就是一次 trace；创建 span 的工厂叫 tracer。
- **采样（sampling）**：追踪会产生海量 span，生产环境通常只记录其中一部分（按比例或按 ID 取样），这个取舍叫采样。
- **OTLP**：OpenTelemetry Protocol，把 span 发给收集器（如 Jaeger、Tempo）的标准传输协议，SR 走 gRPC。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`pkg/observability/logging/logging.go`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/logging/logging.go) | 结构化日志：基于 `zap` 的初始化、事件封装（`ComponentEvent` 等）、组件子 logger |
| [`pkg/observability/metrics/metrics.go`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/metrics/metrics.go) | 全局包级 Prometheus 指标定义与 `Record*` 写入函数 |
| [`pkg/observability/metrics/windowed_metrics.go`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/metrics/windowed_metrics.go) | 窗口化指标管理器：环形缓冲 + 后台 goroutine 周期重算 P50/P95/P99 等 |
| [`pkg/observability/metrics/inflight_collector.go`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/metrics/inflight_collector.go) | 把 `pkg/inflight` 在途计数暴露为 Prometheus 指标的自定义 Collector |
| [`pkg/observability/tracing/tracing.go`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/tracing/tracing.go) | OpenTelemetry 追踪：provider 初始化、采样器、信号/决策/插件三类 span 封装 |
| [`pkg/observability/tracing/propagation.go`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/tracing/propagation.go) | trace 上下文在 HTTP/gRPC 头里的注入与提取（W3C traceparent） |
| [`pkg/config/observability_config.go`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/config/observability_config.go) | 上述三者在 `config.yaml` 中的配置结构体 |
| [`cmd/main.go`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/main.go) | 启动序列：三者接入的调用点与 `startup_complete` 日志 |
| [`cmd/runtime_bootstrap.go`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/runtime_bootstrap.go) | `initializeTracing` / `startMetricsServerIfEnabled` 等封装函数的真实定义 |

> 提示：本讲的「源码精读」会同时给出 `main.go` 里的调用点和 `runtime_bootstrap.go` 里的定义，因为它们分处两个文件——调用在 `main.go`，实现细节在 `runtime_bootstrap.go`。

## 4. 核心概念与源码讲解

### 4.1 结构化日志

#### 4.1.1 概念说明

SR 的日志全部基于 [Uber `zap`](https://pkg.go.dev/go.uber.org/zap)——Go 生态里性能最高、零内存分配的结构化日志库。所谓「结构化」，指的是每条日志最终是一组键值对（字段），而不是一个 `printf` 拼出来的字符串。SR 在 `zap` 之上做了一层薄封装，目的是：

1. **统一字段命名**：每条事件日志自动带 `event`、`component` 字段，便于按子系统过滤。
2. **全局可用**：通过 `zap.ReplaceGlobals` 让任何包都能用 `zap.L()` 取到同一个 logger，避免到处传 logger 参数。
3. **接管标准库**：`zap.RedirectStdLog` 把第三方库写的 `log.Printf` 也收进 zap，统一格式。

#### 4.1.2 核心流程

一次「组件事件」日志的产出流程：

1. 调用方调用 `logging.ComponentEvent("extproc", "request_received", map{...})`；
2. 封装把 `component` 与 `event` 写进字段 map（调用方已提供的同名字段优先，不被覆盖）；
3. 字段转成 `zap.Field` 切片，挂到全局 logger 上得到带字段的子 logger；
4. 按事件级别（info/warn/error）发出一行 JSON。

初始化流程：`main()` → `initializeRuntimeLogger()` → `logging.InitLoggerFromEnv()` → 读环境变量构造 `Config` → `InitLogger()` 构建 zap logger → `ReplaceGlobals` + `RedirectStdLog`。

#### 4.1.3 源码精读

日志器初始化的「配置入口」——完全由环境变量驱动，不依赖 config.yaml，因为日志必须在配置加载**之前**就可用：

[logging.go:L127-L142](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/logging/logging.go#L127-L142) —— 从 `SR_LOG_LEVEL` / `SR_LOG_ENCODING` / `SR_LOG_DEVELOPMENT` / `SR_LOG_ADD_CALLER` 四个环境变量构造 `Config`。注意默认级别是 `info`、默认编码是 `json`、默认开启调用者标注（`AddCaller`）。

构建 logger 后做两件关键的事——替换全局、接管标准库：

[logging.go:L28-L42](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/logging/logging.go#L28-L42) —— `ReplaceGlobals(logger)` 让 `zap.L()` / `zap.S()` 全局指向这个 logger；`RedirectStdLog` 把 Go 标准库的 `log` 包输出重定向到 zap，使第三方依赖的日志也被同一套格式和级别管理。`configureEncoder` 把时间格式定成带毫秒的 ISO 8601（`2006-01-02T15:04:05.000`），消息键定成 `msg`、级别键 `level`、调用者键 `caller`。

最常用的事件 API 是 `ComponentEvent`，它在普通事件上额外附上 `component` 字段：

[logging.go:L235-L237](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/logging/logging.go#L235-L237) 与 [logging.go:L259-L265](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/logging/logging.go#L259-L265) —— `ComponentEvent` → `logComponentEventAt`：先经 `prepareEventFields` 合并字段（`event` 字段若调用方未给则自动补成事件名），再把 `component` 写入字段（同样不覆盖调用方已提供的值），最后委托给通用的 `logEventAt`。这套「调用方优先」的字段合并规则，意味着同名键不会被框架静默改写。

时间编码与调用者格式的细节，藏在 `configureEncoder` 与 `filenameFromPath`：前者把时间键设为 `ts`、用毫秒精度时间戳；后者只保留文件名（去掉目录路径），让 `caller` 字段短而清晰。

> 补充：除了 `ComponentEvent`，包里还有 `WithComponent("extproc")` 返回一个带 `component` 字段的 `*zap.SugaredLogger`，适合在一个函数里反复打多条同组件日志；以及 `DebugEnabled()` 供调用方在打 debug 前判断是否值得构建昂贵负载（如克隆、脱敏 protobuf）。本讲这些不展开，知道入口即可。

#### 4.1.4 代码实践

**实践目标**：亲手验证环境变量如何改变日志输出格式与级别。

**操作步骤**：

1. 阅读上面的 `InitLoggerFromEnv`，确认四个环境变量名与默认值。
2. 用 Go 写一个最小程序（**示例代码，非项目代码**）调用 `logging.InitLoggerFromEnv()`，然后 `logging.ComponentEvent("demo", "hello", map[string]interface{}{"k": "v"})`。
3. 分别用两种环境变量组合跑：

   - `SR_LOG_ENCODING=json`（默认）
   - `SR_LOG_ENCODING=console SR_LOG_LEVEL=debug`

**需要观察的现象**：JSON 模式输出形如 `{"ts":"...","level":"info","caller":"main.go:...","msg":"hello","component":"demo","event":"hello","k":"v"}`；console 模式输出带颜色的人类友好格式。

**预期结果**：`console` 模式下 `caller` 仍只显示文件名而非完整路径（因为 `encodeCallerFilenameOnly`）；`debug` 级别下应能看到更细粒度的事件。

**待本地验证**：若你未配置 Go 运行环境，可改为「源码阅读型实践」——在 `logging.go` 中找到 `resolveLogLevel`，列出它接受的全部级别字符串，并说明输入一个未知值（如 `"trace"`）时回退到哪一级（答案见下面小练习）。

#### 4.1.5 小练习与答案

**练习 1**：为什么日志初始化用环境变量而不是 `config.yaml`？

> **答案**：因为日志必须在 `config.Parse`（配置加载）**之前**就可用——配置加载失败本身就要打日志。`main()` 的顺序是 `initializeRuntimeLogger()` 在 `loadRuntimeConfigOrFatal()` 之前，若日志依赖配置，会陷入「要打配置加载失败的日志，却还没加载配置」的死循环。

**练习 2**：在 `resolveLogLevel` 中，传入一个无法识别的级别字符串会发生什么？

> **答案**：落到 `default` 分支，返回 `zapcore.InfoLevel`（见 logging.go:60-79）。即「未知级别 = info」，保证永远有一个合理默认，不会因为拼错级别名而彻底关掉日志或炸掉启动。

**练习 3**：`ComponentEvent("extproc", "x", {"event":"y"})` 最终日志里的 `event` 字段是 `"x"` 还是 `"y"`？

> **答案**：是 `"y"`。`prepareEventFields` 在写入默认 `event` 前会先检查键是否已存在（`if _, ok := prepared["event"]; !ok`），调用方提供的值优先，框架不覆盖。

### 4.2 Prometheus 指标

#### 4.2.1 概念说明

SR 的指标体系分两套，初学者容易混淆，先讲清楚：

1. **全局包级指标**：在 `metrics.go` 里用 `promauto.NewCounterVec / NewHistogramVec` 声明的「进程级单例」。它们在包被 import 时就自动注册到 Prometheus 默认注册表，全程累加，永不重置。代表是 `llm_model_requests_total`、`llm_model_completion_latency_seconds`。
2. **窗口化指标（windowed metrics）**：在 `windowed_metrics.go` 里维护的「时间窗口派生指标」，如 `llm_model_latency_p95_windowed_seconds`。它们是 `Gauge`，由一个后台 goroutine 每 N 秒从环形缓冲里的原始请求数据**重算** P50/P95/P99、错误率、利用率，再 `Set` 到 Gauge 上。

两者的区别本质上是 **累积分布 vs. 滑动窗口**：前者回答「从启动到现在一共多少」，后者回答「最近 5 分钟表现如何」。负载均衡与调度需要后者，因为旧数据对「现在该把流量分给谁」没有参考价值。

此外还有一个特殊指标 `llm_model_inflight_requests`，它既不是包级 Counter 也不是窗口化 Gauge，而是用「自定义 Collector」在抓取时现查 `pkg/inflight`——这个设计很巧妙，下面专门讲。

#### 4.2.2 核心流程

**写入流程（以一次请求结束为例）**：请求处理代码调用 `metrics.RecordModelTokensDetailed(model, prompt, completion)` → 该函数同时累加三个 Counter（总 token、prompt token、completion token）并往两个直方图 `Observe` 每请求 token 数 → 若开启了窗口化指标，再调 `RecordModelWindowedRequest(...)` 把这条原始数据写进环形缓冲。

**窗口化重算流程**：

1. `InitializeWindowedMetrics` 创建 `WindowedMetricsManager` 并 `Start()`；
2. 后台 goroutine 用 `time.Ticker` 每 `update_interval`（默认 10s，config.yaml 里设 30s）触发一次；
3. `computeWindowedMetrics` 遍历「每个模型 × 每个时间窗口」，从环形缓冲取出窗口内的全部 `RequestData`，算均值/计数/错误率/分位数，`Set` 到对应的 Gauge；
4. 百分位数用线性插值：先把窗口内延迟排序，索引按 \( idx = p \cdot (n-1) \) 计算，再在相邻两点间插值：

\[ \text{value} = v_{\lfloor idx \rfloor}\cdot(1-w) + v_{\lceil idx \rceil}\cdot w, \quad w = idx - \lfloor idx \rfloor \]

#### 4.2.3 源码精读

先看全局包级指标的声明方式。以「每个模型的请求总数」为例，这是最典型的 CounterVec：

[metrics.go:L96-L104](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/metrics/metrics.go#L96-L104) —— `promauto.NewCounterVec` 在包初始化时自动注册，指标名 `llm_model_requests_total`，唯一标签 `model`。`promauto` 的好处是「声明即注册」，不会忘记 `MustRegister`。

写入函数 `RecordModelRequest` 做了一层「空值兜底」——model 为空时统一标成 `consts.UnknownLabel`，避免 Prometheus 因高基数空标签爆炸：

[metrics.go:L343-L349](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/metrics/metrics.go#L343-L349) —— `RecordModelRequest` 把空 model 归一化后再 `WithLabelValues(model).Inc()`。这个「空值→Unknown」的模式在几乎所有 `Record*` 函数里重复出现，是 SR 指标的统一约定。

批处理指标走「采样」节约开销——不是每条都记，按 `sample_rate` 概率丢弃：

[metrics.go:L79-L94](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/metrics/metrics.go#L79-L94) —— `shouldCollectMetric`：未启用直接 false；采样率 ≥ 1.0 全采；否则 `rand.Float64() < sampleRate`。`RecordBatchClassification*` 系列函数开头都先调它做闸门，未命中则直接 return，连 `WithLabelValues` 都不碰（避免创建无用的标签序列化）。

再看窗口化指标的核心——后台 goroutine 与重算：

[windowed_metrics.go:L274-L294](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/metrics/windowed_metrics.go#L274-L294) —— `Start()` 起一个 goroutine，`select` 在 ticker 与 `stopChan` 上：每 tick 调一次 `computeWindowedMetrics`，收到 stop 信号优雅退出。这是 SR 里典型的「长生命周期后台任务 + 退出通道」写法。

[windowed_metrics.go:L378-L458](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/metrics/windowed_metrics.go#L378-L458) —— `computeWindowedMetrics`：双重循环「模型 × 时间窗口」，对每个窗口用 `buffer.GetDataSince(now-window)` 取出窗口内数据；空窗口把请求数/延迟/错误率 `Set(0)`（而不是不设——保证抓到的值反映「最近确实没流量」而非「上次陈旧值」）；非空则算均值、错误率、P50/P95/P99 和利用率。利用率用了简化的 \( \text{util} = \frac{\text{req/s}}{100}\times100\% \)（假设理论上限 100 req/s），代码注释明确写了「可配置化」是 TODO。

最后看那个特殊的「自定义 Collector」：

[inflight_collector.go:L23-L50](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/metrics/inflight_collector.go#L23-L50) —— `inflightCollector` 实现了 Prometheus 的 `Describe`/`Collect` 接口，在 `init()` 里 `MustRegister` 自己。每次 Prometheus 抓取（`/metrics`）时，`Collect` 才调 `inflight.Snapshot()` **现拉**当前每个模型的在途请求数，转成 const metric。

为什么要这么麻烦，不直接用 GaugeVec 的 `Inc/Dec`？文件头注释给出了答案：**「keeps pkg/inflight the single source of truth and avoids drift」**——如果用 GaugeVec 镜像 in/out 调用，一旦某个上报点漏调 `Dec`（比如 panic 漏掉了），镜像值就会和真实在途数永久漂移。Collector 模式让抓取时**永远是真相**，等于把 inflight 当成唯一权威，Prometheus 只是它的只读视图。

#### 4.2.4 代码实践

**实践目标**：理解窗口化指标的「时间窗口」与「重算间隔」如何从 config 进入运行时。

**操作步骤**：

1. 打开 [`config/config.yaml`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/config/config.yaml#L1619-L1627) 的 `observability.metrics.windowed_metrics` 段（第 1619–1627 行），记下 `time_windows`、`update_interval`、`max_models` 的值。
2. 对照 [`observability_config.go`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/config/observability_config.go#L17-L24) 的 `WindowedMetricsConfig` 结构体，确认每个 YAML 键对应哪个字段。
3. 读 `NewWindowedMetricsManager`（windowed_metrics.go:229-272），看 `time_windows` 为空时回退到 `DefaultTimeWindows`（`["1m","5m","15m","1h","24h"]`），`update_interval` 为空时回退到 `DefaultUpdateInterval`（10s）。
4. 在配置里把 `update_interval` 改成 `5s`，`time_windows` 改成 `["1m","10m"]`，重启后抓 `/metrics`。

**需要观察的现象**：抓取到的指标会出现 `llm_model_latency_p95_windowed_seconds{model="...",time_window="1m"}` 和 `time_window="10m"` 两个序列；间隔 5s 后两次抓取的值会不同（因为窗口滑动、数据更新）。

**预期结果**：确认时间窗口标签完全由配置驱动，且窗口为空时该序列值为 0 而非消失。

**待本地验证**：若无法本地起服务，改为读 `computeWindowedMetrics`，手算「窗口内有 4 条延迟 [0.1, 0.2, 0.3, 0.4] 时 P95 应为多少」（提示：\( idx = 0.95 \times 3 = 2.85 \)，在 \(v_2=0.3\) 与 \(v_3=0.4\) 间插值，\( w=0.85 \)，结果 \(0.3\times0.15+0.4\times0.85=0.385\)）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `inflightCollector` 用「抓取时现拉」而不是 GaugeVec 的 `Inc/Dec`？

> **答案**：为了保证 `pkg/inflight` 是唯一真相。镜像模式一旦某个调用点漏掉 `Dec`（panic、提前 return），镜像值就和真实在途数永久漂移，且无法自愈。Collector 在每次抓取时直接读真相，永远不会陈旧。

**练习 2**：批处理指标的采样率 `sample_rate=0.1` 意味着什么？为什么不在 Counter 层面做？

> **答案**：约 10% 的批处理事件才会被记录进 `batch_classification_*` 指标，其余在 `shouldCollectMetric` 闸门处直接 return。这样做是为了在高吞吐批量场景下节约指标写入开销（尤其是带多个标签的直方图 `Observe`）。不能在 Counter 层面「乘 0.1」，因为 Counter 只能整数自增；采样是在「记不记这一条」层面做的概率丢弃。

**练习 3**：`computeWindowedMetrics` 对空窗口为什么 `Set(0)` 而不是跳过？

> **答案**：为了让指标值诚实反映「该窗口最近确实没有流量」。如果跳过，Gauge 会保留上一次的陈旧值，抓取方会误以为流量一直持续。`Set(0)` 是显式声明「窗口已空」。

### 4.3 OpenTelemetry 追踪

#### 4.3.1 概念说明

如果说指标告诉你「整体慢」，日志告诉你「某次出错」，那么追踪告诉你**「某一次具体请求里，时间到底花在了哪一步」**。SR 用 OpenTelemetry（OTel）SDK 实现，把一次请求拆成多层 span 树，遵循一条清晰的层级约定：

```
semantic_router.request.received           （根 span）
├── semantic_router.signal.evaluation      （信号层，第 1 层）
│   ├── semantic_router.signal.keyword
│   ├── semantic_router.signal.embedding
│   └── semantic_router.signal.domain
├── semantic_router.decision.evaluation    （决策层，第 2 层）
├── semantic_router.plugin.execution       （插件层，第 3 层）
└── semantic_router.upstream.request       （模型调用层，第 4 层）
```

这套命名（`signal → decision → plugin → model`）在 `tracing.go` 里用常量集中定义，所有 span 都从这里取名字，保证 trace 视图里层级一致、可被统一查询。

#### 4.3.2 核心流程

**初始化流程**：`main()` → `initializeTracing(cfg)` → 读 `cfg.Observability.Tracing` → `tracing.InitTracing`：

1. 用 `service_name`/`service_version`/`deployment_environment` 创建 `resource`（标识「这些 span 是谁产生的」）；
2. 按 `exporter.type`（`otlp` / `stdout`）创建导出器；
3. 按 `sampling.type` 创建采样器；
4. 组装 `TracerProvider`（带 resource + batcher + sampler），设为全局；
5. 设置全局传播器（W3C TraceContext + Baggage），让 trace 跨进程延续；
6. 创建名为 `"semantic-router"` 的 tracer。

**导出器连接的关键设计**：OTLP gRPC 导出器**不阻塞**启动——即使收集器暂时不可用，进程也能起来，span 会被批处理器暂存或丢弃，而不是卡死启动。这和 u4-l1 讲的「严格核心加宽容可选」一脉相承：tracing 是「可选」能力。

#### 4.3.3 源码精读

初始化主体：

[tracing.go:L43-L101](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/tracing/tracing.go#L43-L101) —— `InitTracing`：`!cfg.Enabled` 直接返回（关追踪时不做任何事）；创建 resource 时用 OTel 语义约定键（`ServiceNameKey` 等）；导出器二选一（`otlp` 走 gRPC、`stdout` 走本地打印，后者常用于调试）；最后用 `WithBatcher(exporter)` 装配——batcher 会异步批量发送 span，不阻塞业务路径。`otel.SetTextMapPropagator` 设置「TraceContext + Baggage」复合传播器，这是 W3C 标准，使 SR 能接入已有分布式追踪链。

采样器选择：

[tracing.go:L103-L120](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/tracing/tracing.go#L103-L120) —— `samplerFromConfig`：`always_on` 全采、`always_off` 全不采、`probabilistic`/`traceidratio`/`trace_id_ratio` 按 `SamplingRate` 比例采（底层 `TraceIDRatioBased`，按 trace ID 哈希确定性采样，保证同一条 trace 的所有 span 要么全采要么全不采）；空值默认全采；未知值回退全采并打 warn。

导出器「不阻塞」的实现：

[tracing.go:L123-L139](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/tracing/tracing.go#L123-L139) —— `createOTLPExporter`：注释明确写了**「We don't use WithBlock() to allow the exporter to connect asynchronously」**。用 5 秒超时上下文初始化，但不阻塞等待连接建立——收集器暂时不可用时，进程照常启动，span 在后台重试或最终丢弃。这是「tracing 可选、不拖累核心」的工程体现。

请求路径上的真实用法——以信号求值为例，这是把追踪接到业务代码的样板：

[req_filter_classification_runtime.go:L39](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/req_filter_classification_runtime.go#L39) 与 [L74](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/tracing/tracing.go#L74) —— 信号求值开始时 `tracing.StartSpan(ctx.TraceContext, tracing.SpanSignalEvaluation)` 开一个信号 span，把新 context 存回 `ctx.TraceContext`；求值结束后 `tracing.EndSignalSpan(signalSpan, matchedRules, confidence, latencyMs)` 把命中规则、置信度、延迟作为 span 属性写上再 `End()`。决策求值同理用 `StartDecisionSpan`/`EndDecisionSpan`（见同文件 L147、L188）。

这里有个关键设计：**span context 跟着请求走**。SR 在 `RequestContext` 上专门存了一个 `TraceContext` 字段，每开一层 span 都把返回的子 context 写回该字段，下一层就能基于它创建子 span，从而串成树。这就是「为什么需要 `InjectTraceContext`/`ExtractTraceContext`（propagation.go）」——当请求要跨进程（如调上游模型）时，得把当前 trace 上下文注入 HTTP 头，对端提取后才能延续同一条 trace。

span 名与属性键的集中定义，是这套体系「一致性」的来源：

[tracing.go:L366-L406](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/tracing/tracing.go#L366-L406) —— span 名常量分四层（信号/决策/插件/模型）外加 RAG、legacy 等，统一前缀 `semantic_router.`。属性键（`signal.type`、`decision.name`、`plugin.status`、`model.name` 等，见 L299-L364）同样集中定义。这套「信号→决策→插件→模型」的属性层级，与 u2 的路由心智模型完全对应——追踪视图其实就是一次请求在心智模型各层的耗时快照。

> 注意：tracing.go 末尾还保留了一批 `Legacy` span（`SpanClassification`、`SpanPIIDetection` 等，L399-L406 注释标 deprecated）。这是迁移期的向后兼容垫片，新代码应使用 `SpanSignalEvaluation` 等新名字。

#### 4.3.4 代码实践

**实践目标**：用 `stdout` 导出器，亲眼看到一次请求的 span 结构，不依赖任何外部收集器。

**操作步骤**：

1. 读 [`config/config.yaml`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/config/config.yaml#L1604-L1618) 的 `observability.tracing` 段，把 `exporter.type` 从 `otlp` 改成 `stdout`（临时改一份本地配置即可，**不要提交**）。
2. 确认 `enabled: true`、`sampling.type: always_on`（保证每条都采，调试时才这么设）。
3. 用这份配置启动 router，发一个 `/v1/chat/completions` 请求。
4. 在 router 的标准输出里找打印出来的 trace JSON。

**需要观察的现象**：每条 trace 是一组 span，每个 span 有 `name`（如 `semantic_router.signal.evaluation`）、`attributes`（含 `signal.type`、`signal.latency_ms` 等）、`start_time`/`end_time`、以及 `parent_span_id` 串成树。

**预期结果**：能看到根 span 下挂着信号 span、决策 span，它们的 `parent_span_id` 指向根；属性里能看到本次请求命中的信号类型与决策置信度。

**待本地验证**：若本地无模型后端无法端到端跑请求，改为「源码阅读型实践」——在 `tracing.go` 找到 `StartSignalSpan`（L189-218），说明它如何根据 `signalType` 选择不同的具体 span 名（`keyword`/`embedding`/`domain`...），以及未知类型回退到哪个通用 span（答案：`SpanSignalEvaluation`）。

#### 4.3.5 小练习与答案

**练习 1**：`TraceIDRatioBased(0.5)` 和「每个 span 各 50% 概率记录」有什么本质区别？

> **答案**：`TraceIDRatioBased` 是**按 trace ID 确定性采样**——同一条 trace 的 ID 决定了它整条采或不采，所以同一条 trace 的所有 span（跨进程、跨服务）要么全采要么全不采，trace 视图永远完整。而「每个 span 各 50%」是独立随机，会导致同一条 trace 的 span 一半采一半不采，trace 树断裂，失去追踪意义。config.yaml 里 `sampling.type: probabilistic`、`rate: 0.5` 用的就是前者。

**练习 2**：为什么 OTLP 导出器初始化不用 `WithBlock()`？

> **答案**：`WithBlock()` 会阻塞直到连接建立成功。若收集器暂时不可用，路由器就启动不了，这违背「tracing 是可选能力、不能拖累核心路径」的原则。不加 `WithBlock()` 让导出器异步连接，进程立即就绪，span 由 batcher 在后台重试发送或最终丢弃。

**练习 3**：为什么 SR 要在 `RequestContext` 上单独存一个 `TraceContext` 字段，而不直接用函数参数传递 context？

> **答案**：因为 ExtProc 的请求处理是**跨消息分阶段**的（请求头→请求体→响应体，见 u4-l3），一个逻辑请求对应多次 `Process` 调用和多个内部函数调用。span 上下文需要在这些阶段与函数之间持续传递以维持父子关系。把它存在请求级的 `RequestContext` 上，每个阶段的处理器都能取到「当前请求的 trace 进度」并继续往下开子 span，比在所有函数签名里都加 `ctx context.Context` 更省事，也保证不会断链。

### 4.4 三大能力在 main() 启动序列中的接入

#### 4.4.1 概念说明

可观测性的三件套不是「装好就完了」，而是要在启动序列的**正确时机**接入。本模块把 u4-l1 的启动序列和本讲前三模块缝起来，回答：日志、指标、追踪分别在 `main()` 的哪一步起？为什么是这个顺序？

核心原则只有一条：**日志最早（配置加载前就要用），指标和追踪居中（配置加载后、ExtProc 起服前），且三者都是「失败宽容」的——可观测性本身出问题绝不能拖垮核心路由。**

#### 4.4.2 核心流程

看 `main()` 里和可观测性相关的几行（顺序很重要）：

1. `initializeRuntimeLogger()` —— **最先**，日志器就位（环境变量驱动，不依赖配置）；
2. 中间若干步：加载配置、建 Registry、起 API Server、下模型；
3. `defer initializeTracing(cfg)()` —— 追踪初始化，用 `defer` 注册关闭钩子；
4. `initializeWindowedMetricsIfEnabled(cfg)` —— 窗口化指标管理器启动；
5. `registerSignalHandler(...)` + `startMetricsServerIfEnabled(cfg, opts.metricsPort)` —— 注册信号处理（退出时调关闭钩子，含 tracing shutdown）、起 `/metrics` HTTP 服务；
6. 后续：初始化运行时依赖、起 ExtProc；
7. 全部就绪后 `logStartupSummary(...)` 发一条 `startup_complete`。

#### 4.4.3 源码精读

`main()` 里可观测性的接入点（注意行号）：

[main.go:L37-L42](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/main.go#L37-L42) —— 三行：`defer initializeTracing(cfg)()`（追踪，defer 保证退出时 `shutdownTracing` 刷盘）、`initializeWindowedMetricsIfEnabled(cfg)`（窗口化指标）、`startMetricsServerIfEnabled(cfg, opts.metricsPort)`（`/metrics` HTTP 服务）。注意这三步都在模型下载（`ensureModelsDownloadedOrFatal`，L34）**之后**——因为它们都依赖配置，而配置加载在模型下载之前。

`initializeTracing` 的真实定义（在 runtime_bootstrap.go，不在 main.go）：

[runtime_bootstrap.go:L199-L225](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/runtime_bootstrap.go#L199-L225) —— 关键设计：`!cfg.Observability.Tracing.Enabled` 时返回一个**空函数** `func(){}`，配合 `main()` 里的 `defer initializeTracing(cfg)()`——即使关了追踪，defer 一个空函数也无害。初始化失败只 `ComponentWarnEvent` 不 fatal，再次印证「tracing 失败宽容」。返回的 `shutdownTracing` 被 defer 注册，进程退出时带 5 秒超时优雅 flush span。

`startMetricsServerIfEnabled` 的真实定义：

[runtime_bootstrap.go:L270-L298](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/runtime_bootstrap.go#L270-L298) —— 默认 `metricsEnabled=true`，但可被 `cfg.Observability.Metrics.Enabled`（`*bool`，三态）覆盖；`metricsPort<=0` 也强制关闭。启用时起一个 goroutine，把 `promhttp.Handler()` 挂到 `/metrics`，`ListenAndServe` 在 `:9190`（默认 `--metrics-port`）。这里有个细节：用的是 `http.Handle`（注册到 DefaultServeMux）+ `http.ListenAndServe(addr, nil)`，即指标服务是个**独立的 HTTP 监听**，和 API Server（8080）、ExtProc（gRPC 50051）是三个不同的监听点。

`initializeWindowedMetricsIfEnabled`：

[runtime_bootstrap.go:L237-L251](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/runtime_bootstrap.go#L237-L251) —— 仅当 `cfg.Observability.Metrics.WindowedMetrics.Enabled` 时调用 `metrics.InitializeWindowedMetrics`；失败只 warn 不 fatal。

启动完成的「里程碑日志」——这是排障时最该 grep 的一行：

[main.go:L180-L199](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/main.go#L180-L199) —— `logStartupSummary` 发的 `startup_complete` 事件。它的字段设计很讲究，几乎把「路由器此刻在监听什么、开了哪些能力」一次性交代清楚：

| 字段 | 含义 |
|------|------|
| `extproc_port` / `api_port` / `metrics_port` | 三个监听端口（数据面 / 控制面 / 指标） |
| `secure` | gRPC 是否启用 TLS |
| `config_source` | 配置来源（文件 / Kubernetes） |
| `decisions` | 当前生效的全部决策名（逗号分隔） |
| `embedding_ready` | 嵌入模型是否就绪 |
| `sem_cache_enabled` | 语义缓存是否启用 |
| `model_selection` | 模型选择（Elo/Hybrid 等）是否启用 |
| `authz_providers` / `ratelimit_providers` | 授权与限流 provider 数量 |

这条日志的设计意图写在函数注释里：**「making it trivial for agents and log aggregators to determine what the router is serving and on which ports」**——让运维/agent 一眼看出「这个实例在干什么」。当某台实例行为异常时，grep 它的 `startup_complete` 就能立刻知道它跑了哪些决策、开了哪些能力、监听哪些端口，是排障的第一手线索。

#### 4.4.4 代码实践

**实践目标**：在 `main.go` 中准确定位三个可观测性接入点，并解读 `startup_complete` 字段。

**操作步骤**：

1. 打开 [`cmd/main.go`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/main.go#L37-L42)，在第 37–42 行找到 `initializeTracing`（L37，注意是 `defer ...()`）、`initializeWindowedMetricsIfEnabled`（L38）、`startMetricsServerIfEnabled`（L42）。
2. 注意它们都在 `ensureModelsDownloadedOrFatal`（L34）之后、`initializeRuntimeDependencies`（L44）之前——**指标端口与追踪初始化都发生在模型下载完成后、运行时依赖初始化前**。
3. 跳到 [`cmd/runtime_bootstrap.go`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/runtime_bootstrap.go#L270-L298) 读 `startMetricsServerIfEnabled`，确认默认指标端口是 9190（见 `parseRuntimeOptions` 的 `--metrics-port` 默认值，runtime_bootstrap.go:50）。
4. 读 `logStartupSummary`（main.go:180-199），把 11 个字段抄成一张表。
5. 用一份本地配置启动 router，从日志里 grep `startup_complete`，对照表格逐字段解读你这次实例的状态。

**需要观察的现象**：`startup_complete` 出现在所有初始化完成、ExtProc 即将起服之际（它在 L49，`startExtProcServerOrFatal` 在 L51）；指标端口字段 `metrics_port` 应与你传的 `--metrics-port` 一致（默认 9190）。

**预期结果**：能说出「指标 HTTP 服务在 9190、由 `startMetricsServerIfEnabled` 在 goroutine 中起、追踪由 `initializeTracing` 在配置加载后初始化且失败仅 warn」。

**待本地验证**：若无法运行，改为读 `parseRuntimeOptions`（runtime_bootstrap.go:42-75）列出所有可观测性相关命令行 flag（`--metrics-port` 等），并说明 `--metrics-port 0` 会怎样（答案：`startMetricsServerIfEnabled` 里 `metricsPort<=0` 强制 `metricsEnabled=false`，发 `metrics_server_disabled` 事件，不起指标服务）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `initializeTracing(cfg)` 用 `defer ...()` 包起来，而 `startMetricsServerIfEnabled` 不用？

> **答案**：`initializeTracing` 返回的是一个**关闭函数**（`shutdownTracing`），必须 `defer` 调用以保证进程退出时把缓冲的 span flush 给收集器，否则会丢 span。而 `startMetricsServerIfEnabled` 起的是一个长生命周期 goroutine（`http.ListenAndServe` 阻塞监听），它本身不需要「关闭钩子」——进程退出时 goroutine 自然终止。两者职责不同，所以包装方式不同。

**练习 2**：如果配置里 `observability.tracing.enabled: false`，`main()` 里的 `defer initializeTracing(cfg)()` 会怎样？

> **答案**：`initializeTracing` 第一行检查 `!cfg.Enabled` 直接 `return func(){}`（空函数）。`main()` defer 这个空函数，进程退出时调用它什么都不做。全程不会创建 TracerProvider、不会连收集器，零开销。这是「可选能力关闭即无成本」的体现。

**练习 3**：`startup_complete` 里为什么把 `decisions`（决策名列表）也打进去？

> **答案**：让运维从一行日志就知道「这个实例当前在执行哪些路由决策」。在多实例、多配方（recipe）的部署里，不同实例可能加载不同配方，决策名列表是识别「这台实例究竟在按什么策略路由」的最直接证据，是排障和配置核对的第一手信息。

## 5. 综合实践

把三件套串起来，设计一个完整的可观测性演练：

**场景**：你想确认「某次慢请求，时间花在了信号求值还是上游模型调用」。

**任务**：

1. **配置就位**：在一份本地 `config.yaml` 里，确认 `observability.tracing.enabled: true`、`exporter.type: stdout`、`sampling.type: always_on`；确认 `observability.metrics.enabled: true`、`windowed_metrics.enabled: true`。参考 [`config/config.yaml`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/config/config.yaml#L1604-L1627) 的真实写法。
2. **启动并定位**：启动 router，grep `startup_complete`，确认 `metrics_port`（应为 9190）、`decisions`、`embedding_ready` 三个字段值符合预期。
3. **发请求看 trace**：发一个 `/v1/chat/completions` 请求，从 stdout 的 trace JSON 里找到 `semantic_router.signal.evaluation` 和 `semantic_router.upstream.request` 两个 span，对比它们的 `start_time`/`end_time`，判断主要耗时在哪一层。
4. **抓指标看分布**：`curl http://localhost:9190/metrics | grep llm_model_completion_latency_seconds`，看直方图分布；再 grep `llm_model_latency_p95_windowed_seconds`，对比「单次 trace 的延迟」与「窗口内 P95」的关系——前者是个例，后者是分布。
5. **关联三者**：在 trace 的 span 属性里找 `request.id`，用这个 ID 去 grep 日志（`ComponentEvent` 会带上 request_id 相关字段），验证你能从「慢的指标 → 慢的 trace → 慢的那条日志」一路下钻。

**预期结果**：你能说清楚「日志答事件、指标答分布、追踪答单次因果链」三者如何通过 `request_id` / `trace_id` 串成一条完整的排障链路。

**待本地验证**：若无完整模型后端，至少完成步骤 1、2、4 的配置与启动部分，并手工构造一段 trace JSON（参考 4.3 的 span 命名）画出 span 树。

## 6. 本讲小结

- SR 的可观测性是**三件套分离**：日志用 `zap`、指标用 Prometheus client、追踪用 OpenTelemetry SDK，三者各自独立、通过 `request_id`/`trace_id` 关联。
- **结构化日志**在配置加载前就由环境变量（`SR_LOG_*`）初始化；`ComponentEvent` 自动带 `component`/`event` 字段且「调用方优先」不覆盖；未知级别回退 info。
- **指标分两套**：全局包级指标（`promauto` 声明即注册、空值归一化、批处理走采样）+ 窗口化指标（环形缓冲 + 后台 goroutine 周期重算 P50/P95/P99）；`inflight` 用自定义 Collector 保证「pkg/inflight 是唯一真相」。
- **追踪**遵循「信号→决策→插件→模型」四层 span 层级，span 名与属性键集中定义；OTLP 导出器**不阻塞**启动、采样用 `TraceIDRatioBased` 保证整条 trace 完整、span context 存在 `RequestContext.TraceContext` 上跨阶段传递。
- 三者在 `main()` 的接入顺序是「日志最早、指标/追踪居中、全部失败宽容」；`startup_complete` 日志用 11 个字段一次性交代实例的全部监听端口与能力开关，是排障第一手线索。
- 贯穿全讲的设计哲学：**可观测性本身是「可选能力」，绝不能拖垮核心路由路径**——tracing/metrics 初始化失败只 warn 不 fatal。

## 7. 下一步学习建议

- **u11-l3（限流、在途、延迟与授权）**：本讲提到的 `pkg/inflight` 在途计数正是那里讲的「自愈老化」治理控制，可对照看 `inflightCollector` 如何把它暴露为指标。
- **u5（请求处理主链路）**：追踪 span 的真实打点（信号 span、决策 span）都在请求处理代码里，读 `req_filter_classification_runtime.go` 能看到追踪如何嵌入业务路径。
- **源码延伸**：阅读 `pkg/observability/metrics/` 下的 `cache_metrics.go`、`rag_metrics.go`、`reasoning_metrics.go` 等专题指标文件，它们展示了「按子系统组织指标」的扩展模式；再看 `tracing_test.go` 了解 span 的测试方式。
- **配置延伸**：把本讲的 [`observability_config.go`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/config/observability_config.go) 与 `config.yaml` 的 `observability` 段对照，尝试调整采样率和时间窗口，观察 trace 与指标的变化。
