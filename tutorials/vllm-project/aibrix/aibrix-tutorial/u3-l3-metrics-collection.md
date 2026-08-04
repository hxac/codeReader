# u3-l3 指标采集、聚合与监控管线

## 1. 本讲目标

上一讲（u3-l1）我们接通了「从 PodAutoscaler CR 到期望副本数」的伸缩管线骨架，并指出 `DefaultAutoScaler.executeScalingPipeline` 是一条「采集 → 配置窗口 → 聚合 → 算法计算」的四步流水线。本讲聚焦其中的**数据侧**——这条流水线里的指标从哪里来、怎么合并、谁在定时驱动它。

学完本讲，你应当能够：

1. 说清 **collector / fetcher / client 三层指标获取结构**各自的职责与边界。
2. 描述一个外部指标（如经 Prometheus Adapter 暴露的 QPS）从源头一路流到 `ComputeDesiredReplicas` 的完整方法调用链。
3. 解释聚合器（aggregator）如何用「双层平均 + stable/panic 双窗口」把原始抖动数据变成算法可消费的平滑信号。
4. 区分「resync 监控循环」与「monitor Prometheus 指标记录器」这两个容易混淆的概念。

## 2. 前置知识

- **指标（metric）与伸缩的关系**：自动伸缩的本质是「观察某个量化指标 → 判断是否偏离目标 → 调整副本数」。指标越准、越稳，伸缩决策越可靠。
- **Kubernetes 指标 API 体系**：K8s 原生提供三类指标接口——`metrics.k8s.io`（resource：cpu/memory）、`custom.metrics.k8s.io`（custom：任意自定义指标）、`external.metrics.k8s.io`（external：不与具体 Pod 绑定的全局指标）。Prometheus 通常通过 **prometheus-adapter** 桥接到后两类 API，而不是被业务直接抓取。
- **滑动窗口（sliding window）**：只保留「最近 N 秒」的数据点，老数据自动淘汰。窗口越长越平滑但反应慢，越短越灵敏但易抖动。
- **stable/panic 双窗口**：这是 KPA（KNative Pod Autoscaler）的核心思想——同时维护一个长窗口（stable，平时决策）和一个短窗口（panic，突发时快速扩容）。本讲只讲数据怎么进这两个窗口，算法侧的 panic 触发逻辑留到 u3-l2。
- 你需要已经读过 u3-l1，知道 `Reconcile → ComputeDesiredReplicas → executeScalingPipeline` 这条主线。

## 3. 本讲源码地图

本讲涉及的源码集中在 `pkg/controller/podautoscaler/` 下，分四个层次：

| 文件 | 层次 | 作用 |
|------|------|------|
| `metrics/collector.go` | 采集编排层 | `CollectMetrics` 入口，按指标来源分流到 Pod 级或 External 级采集，产出 `MetricSnapshot` |
| `metrics/fetcher.go` | 按源抓取层 | 定义统一 `MetricFetcher` 接口与四种实现（POD/RESOURCE/CUSTOM/EXTERNAL），以及 `MetricFetcherFactory` 工厂 |
| `aggregation/aggregator.go` | 聚合门面层 | 无状态聚合器，把快照写入存储并读出 stable/panic 值 |
| `metrics/client.go` | 存储层 | `MetricsClient` 持有 stable/panic 滑动窗口与历史记录，是真正的有状态存储 |
| `types/core.go` / `types/metrics.go` | 数据模型 | `MetricKey`、`CollectionSpec`、`MetricSnapshot`、`AggregatedMetrics`、`TimeWindow` |
| `autoscaler.go` | 装配与管线 | `executeScalingPipeline` 把上述四层串成四步流水线 |
| `podautoscaler_controller.go` | 驱动层 | `Run` 的 resync ticker 定时触发 Reconcile；`newReconciler` 装配工厂与 monitor |
| `monitor/monitor.go` / `monitor/metrics.go` | 可观测层 | 用 Prometheus GaugeVec 记录每次伸缩决策 |

一句话总览数据流方向：

```
Pod/外部源  →  Fetcher  →  Collector(MetricSnapshot)  →  Aggregator  →  MetricsClient(窗口)  →  AggregatedMetrics  →  算法
```

## 4. 核心概念与源码讲解

### 4.1 指标采集链路：Collector 编排层

#### 4.1.1 概念说明

`collector.go` 解决的问题是：**「一个 PodAutoscaler 可能配了多种来源的指标，怎么把『去哪里取、取几个』这件事统一编排好？」**

它故意把自己做成「薄编排层」——只负责按指标来源类型（`MetricSourceType`）决定「逐 Pod 取」还是「整体取一次」，然后把真正的网络/API 调用下放给 fetcher。这样上层 `executeScalingPipeline` 只需要调用一个 `CollectMetrics`，不必关心来源细节。

关键设计：**所有指标最终都被归一化成一个 `MetricSnapshot`**，里面装着一个 `[]float64`（一组数值，通常是逐 Pod 的值）。这个统一形状让下游聚合器不必区分来源。

#### 4.1.2 核心流程

`CollectMetrics` 的执行过程可以用下面这段伪代码描述：

```
func CollectMetrics(spec, factory):
    fetcher = factory.For(spec.MetricSource)        # 按来源选 fetcher
    switch spec.MetricSource.MetricSourceType:
        case POD, RESOURCE, CUSTOM:
            return collectFromPods(spec, fetcher)   # 逐 Pod 取
        case EXTERNAL, DOMAIN:
            return collectFromExternal(spec, fetcher) # 整体取一次
        default:
            return collectFromPods(spec, fetcher)   # 默认走 Pod 路径
```

`collectFromPods` 遍历目标工作负载的每个 Pod，逐个调用 `fetcher.FetchPodMetrics`，收集成功的值，失败的记入错误列表。它有一个重要的**容错策略**：只要还有一个 Pod 取值成功，就返回带部分值的快照（错误记到 `MetricSnapshot.Error` 但不阻断流程）；只有全部失败才把错误上升为返回错误。

#### 4.1.3 源码精读

入口 `CollectMetrics` 用工厂拿到对应 fetcher，再按来源类型分流：

[metrics/collector.go:32-43](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/metrics/collector.go#L32-L43) — `CollectMetrics`：根据 `MetricSourceType` 把采集分为「Pod 级」与「External 级」两条路径，默认走 Pod 级。

逐 Pod 采集与部分失败处理：

[metrics/collector.go:46-83](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/metrics/collector.go#L46-L83) — `collectFromPods`：循环调用 `fetcher.FetchPodMetrics`，成功值入 `values`，失败计入 `collectErrors`；`successCount == 0` 时才把错误上升，否则只在 `klog.V(4)` 记录部分失败，保证单个 Pod 异常不会拖垮整体伸缩。

External 采集用一个「假 Pod」适配统一接口：

[metrics/collector.go:86-110](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/metrics/collector.go#L86-L110) — `collectFromExternal`：外部指标与具体 Pod 无关，于是构造一个名为 `external-source` 的 dummy Pod，调用一次 `FetchPodMetrics`，把单个全局值包成单元素 `Values`，从而复用与 Pod 级完全相同的下游聚合逻辑。

`MetricSnapshot` 是这条链路的统一产物：

[types/core.go:86-94](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/types/core.go#L86-L94) — `MetricSnapshot`：携带命名空间、目标名、指标名、`Values []float64`、时间戳与可选 `Error`。注意源码注释里的 TODO 提到「Prefill/Decode 场景需要扩展」，说明当前模型假设一个 Pod 一个值。

#### 4.1.4 代码实践：阅读 collector 的单元测试

1. **实践目标**：用一个现成的测试验证「External 采集会正确使用 CollectionSpec 里的命名空间构造 dummy Pod」。
2. **操作步骤**：打开测试文件 [metrics/collector_test.go:32-67](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/metrics/collector_test.go#L32-L67)，阅读 `recordingMetricFetcher`（一个返回固定值 42、并偷偷记下 `pod.Namespace` 的假 fetcher）与 `TestCollectMetricsExternalUsesCollectionNamespace`。
3. **观察重点**：测试传入 `Namespace: "tenant-a"`、`MetricSourceType: EXTERNAL`，断言 `fetcher.namespace == "tenant-a"` 且 `snapshot.Values == []float64{42}`。这恰好印证了 `collectFromExternal` 用 `spec.Namespace` 构造 dummy Pod 的行为。
4. **预期结果**：如果你本地能跑 Go 测试（待本地验证，需 Go 工具链与依赖），执行 `go test ./pkg/controller/podautoscaler/metrics/ -run TestCollectMetricsExternalUsesCollectionNamespace` 应当通过。若无法运行，光读懂断言也能确认行为。

#### 4.1.5 小练习与答案

**练习 1**：假如目标工作负载有 3 个 Pod，其中第 2 个 Pod 抓取失败，`CollectMetrics` 返回的 `MetricSnapshot.Values` 长度是几？`Error` 字段是否为 `nil`？

**答案**：`Values` 长度为 2（成功的那两个）；`Error` 不为 `nil`——但因为 `successCount > 0`，这个错误只来自 `combineErrors` 聚合的失败信息，且 `CollectMetrics` 仍返回 `(snapshot, nil)`（第二个返回值是 `nil`）。注意区分：快照内部的 `snapshot.Error` 记录了部分失败，而函数的 error 返回值只有在「全部失败」时才非 nil。

**练习 2**：为什么 External 指标也要套一个 dummy Pod 去调 `FetchPodMetrics`，而不是给 fetcher 单独设计一个「全局取值」的方法？

**答案**：为了**让下游聚合逻辑保持单一形状**。所有来源都产出「一组 per-pod 值」，聚合器只需面对 `[]float64` 一种数据结构，不必为 external 写特殊分支。这是「接口归一化」换「上层简单」的典型取舍。

---

### 4.2 多来源 Fetcher：按 Pod 统一抽象

#### 4.2.1 概念说明

`fetcher.go` 是真正「伸手去拿数据」的一层。它的核心抽象是一个统一接口 `MetricFetcher`，**所有来源都被建模成「给一个 Pod、一个 MetricSource，返回一个 float64」**。源码注释把这条铁律写得很直白：

> All metrics are fetched per-pod to maintain uniform upper layer logic. External metrics are adapted to appear as per-pod values.

这是一个关键的归一化决策：不管指标来自 Pod 的 HTTP `/metrics`、K8s resource API、custom.metrics API 还是外部服务，对上层都是同一个 `FetchPodMetrics` 签名。四个实现各自只认一种来源类型，并在入口做类型校验（防御性编程）。

#### 4.2.2 核心流程

四种 fetcher 与来源的对应关系：

| Fetcher 实现 | 适用的 `MetricSourceType` | 数据来源 |
|--------------|---------------------------|----------|
| `RestMetricsFetcher` | `POD` | 直连 Pod HTTP：`http://pod_ip:port/path`，委托 `EngineMetricsFetcher.FetchTypedMetric` |
| `ResourceMetricsFetcher` | `RESOURCE` | K8s `metrics.k8s.io`：cpu（毫核）/memory |
| `CustomMetricsFetcher` | `CUSTOM` | K8s `custom.metrics.k8s.io` |
| `ExternalMetricsFetcher` | `EXTERNAL` / `DOMAIN` | 两条子路径：`Endpoint != ""` 走 AIBrix GPU-Optimizer REST；`Endpoint == ""` 走 K8s `external.metrics.k8s.io` |

选择哪个 fetcher 由工厂 `DefaultMetricFetcherFactory.For(source)` 的 `switch` 完成，未知类型兜底落到 external fetcher。

`MetricSource` CRD 字段决定了 fetcher 的行为参数：

[api/autoscaling/v1alpha1/podautoscaler_types.go:145-166](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/podautoscaler_types.go#L145-L166) — `MetricSource`：`MetricSourceType`（枚举 pod/resource/custom/external/domain）、`Endpoint`、`Path`、`Port`、`TargetMetric`、`TargetValue` 等字段，是 fetcher 取值的全部输入。

#### 4.2.3 源码精读

统一接口定义：

[metrics/fetcher.go:40-47](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/metrics/fetcher.go#L40-L47) — `MetricFetcher` 接口：唯一方法 `FetchPodMetrics(ctx, pod, source) (float64, error)`，注释列出了四种来源各自如何映射到这个签名。

工厂的分流逻辑：

[metrics/fetcher.go:348-363](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/metrics/fetcher.go#L348-L363) — `DefaultMetricFetcherFactory.For`：按 `MetricSourceType` 返回对应 fetcher，未知类型打警告并兜底到 external（注释认为 external fetcher 对多数场景能优雅降级）。

POD 类型如何直连 Pod：

[metrics/fetcher.go:79-107](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/metrics/fetcher.go#L79-L107) — `RestMetricsFetcher.fetchFromPod`：先校验 `pod.Status.PodIP` 非空，再用 `pod_ip:port` 端点、`GetEngineType(pod)` 推断的引擎类型调用 `engineFetcher.FetchTypedMetric`；**失败时返回 `0.0, nil`（记 warning 但不当错误）**，把「如何对待零值」的决策权交给上层。

RESOURCE 类型走 K8s metrics API：

[metrics/fetcher.go:129-165](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/metrics/fetcher.go#L129-L165) — `ResourceMetricsFetcher.fetchResourceMetric`：调用 `PodMetricses(namespace).Get(pod)`，按 `TargetMetric` 是 `cpu` 取 `MilliValue()`、是 `memory` 取 `Value()`，跨容器累加。

EXTERNAL 类型的两条子路径：

[metrics/fetcher.go:253-265](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/metrics/fetcher.go#L253-L265) — `fetchFromExternal`：`Endpoint != ""` 判定为 AIBrix GPU-Optimizer REST 调用；否则走 K8s `external.metrics` API。注释说明了这条二分判断。

K8s external.metrics 的读取与多值求和：

[metrics/fetcher.go:292-317](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/metrics/fetcher.go#L292-L317) — `fetchFromK8sExternalMetrics`：按命名空间+指标名 List，把返回的多个 item 求和。**这正是 Prometheus 经 prometheus-adapter 暴露为 external.metrics 时走的路径**（注释也指出当前无 selector，假设一个 namespace 内一个 external metric 只归一个 PodAutoscaler）。

#### 4.2.4 代码实践：手工推演一个 external 指标的取值

1. **实践目标**：理解 `ExternalMetricsFetcher` 的二分判断与「全局值当作 per-pod 值」的适配。
2. **操作步骤**：假设你的 PodAutoscaler 这样配置（示例 CR 片段，非项目自带文件）：

   ```yaml
   # 示例代码：一个使用 K8s external.metrics 的 PodAutoscaler 片段
   metricsSources:
     - metricSourceType: external
       targetMetric: http_requests_per_second
       targetValue: "100"
   ```

   注意这里**没有** `endpoint` 字段。沿着 `For(source)` → `ExternalMetricsFetcher` → `fetchFromExternal` 走，因为 `Endpoint == ""`，会进入 `fetchFromK8sExternalMetrics`。
3. **观察重点**：跟踪 [fetcher.go:303](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/metrics/fetcher.go#L303) 的 `NamespacedMetrics(pod.Namespace).List(...)`，它返回的多个 item 会被求和成一个 float64。
4. **预期结果**：这个单值回到 `collectFromExternal`，包成 `Values: [sum]`。由此你能解释「为什么一个全局 QPS 指标最终也能流进与 per-pod 指标相同的聚合管道」——因为它被适配成了单元素切片。

#### 4.2.5 小练习与答案

**练习 1**：`RestMetricsFetcher` 在抓取失败时为什么返回 `(0.0, nil)` 而不是 `(0, err)`？

**答案**：因为 collector 的 `collectFromPods` 一旦收到 `err` 就会把该 Pod 计入 `collectErrors` 并跳过该值；返回 `nil` 错误意味着这个 Pod 贡献一个 `0.0` 值参与平均。这是一个刻意选择：让「Pod 暂时不可达」表现为「零负载」而非「无数据」，由上层（部分失败兜底、窗口平均）来稀释噪声，避免单个 Pod 抖动直接中断整轮伸缩。

**练习 2**：如果你想新增一个直接查询 Prometheus 的 fetcher（不走 K8s API），需要改动哪几个地方？

**答案**：① 实现一个 `PrometheusMetricsFetcher`，满足 `MetricFetcher` 接口，并在入口校验自己的 `MetricSourceType`；② 在 `DefaultMetricFetcherFactory` 的字段、构造函数 `NewDefaultMetricFetcherFactory`、以及 `For` 的 switch 里各加一个分支；③ 若引入了新的 `MetricSourceType`，还需更新 CRD 的 enum 校验（`podautoscaler_types.go` 的 `MetricSourceType` 常量与 kubebuilder `Enum` 标记）并重新 `make manifests`。

---

### 4.3 聚合器逻辑：双层平均与 stable/panic 双窗口

#### 4.3.1 概念说明

原始指标是**抖动的**：某一秒 QPS 飙高、下一秒回落。直接拿原始值做伸缩决策会导致副本数频繁抖动（thrashing）。聚合器的任务是**把一系列原始采样平滑成算法可消费的稳态信号**。

AIBrix 的聚合层有两个角色，务必分清：

- **`aggregation.DefaultMetricAggregator`（门面，无状态）**：只做转发——把快照写给存储、从存储读出 stable/panic 值。它不持有任何窗口数据。
- **`metrics.MetricsClient`（存储，有状态）**：真正维护 stable/panic 滑动窗口与历史记录。它是被多个 PodAutoscaler 共享的，靠 `MetricKey` 做隔离。

这里有一个**双层平均**的关键语义，理解它就理解了整条聚合链：

1. **空间平均（跨 Pod）**：`UpdateMetrics` 收到 `MetricSnapshot.Values`（一组 per-pod 值），先求一次算术平均，得到「本周期这一组 Pod 的平均负载」。
2. **时间平均（跨周期）**：每个 resync 周期产生一个空间平均值，被 `Record` 进滑动窗口；`GetMetricValue` 再对窗口内所有周期点求平均。

最终送到算法的 `StableValue`，是「最近 stable 窗口内、各周期空间平均值的再平均」。

#### 4.3.2 核心流程

聚合在 `executeScalingPipeline` 的第 2 步发生，三步如下：

```
# 已由 collector 产出 snapshot
aggregator.ProcessSnapshot(metricKey, snapshot)
    └─ client.UpdateMetrics(snapshot.Timestamp, metricKey, snapshot.Values...)
         ├─ avg = sum(Values) / len(Values)     # 空间平均
         ├─ stableWindow.Record(timestamp, avg) # 进 stable 窗口
         └─ panicWindow.Record(timestamp, avg)  # 进 panic 窗口（KPA 用）

aggregated := aggregator.GetAggregatedMetrics(metricKey, now)
    └─ client.GetMetricValue(metricKey, now)
         ├─ stableValue = stableWindow.Avg()    # 时间平均（长窗口）
         └─ panicValue  = panicWindow.Avg()     # 时间平均（短窗口）
```

返回的 `AggregatedMetrics` 同时带 `StableValue` 与 `PanicValue`，**算法层决定用哪个**：APA 只用 `StableValue`；KPA 平时用 `StableValue`，当 `PanicValue/StableValue` 超过 panic 阈值时切到 `PanicValue`（算法细节见 u3-l2）。

默认窗口时长在存储层定义：

\[ \text{StableWindow} = 180\text{s}, \quad \text{PanicWindow} = 60\text{s} \]

可被 PodAutoscaler 的 `ObserveWindowSeconds` / `PanicWindowSeconds` 覆盖（见 4.4）。

#### 4.3.3 源码精读

聚合器接口与无状态实现：

[aggregation/aggregator.go:28-48](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/aggregation/aggregator.go#L28-L48) — `MetricAggregator` 接口与 `DefaultMetricAggregator`：注释明确说「同一实现服务所有策略，总是返回 stable 和 panic 两个值，由算法层决定用哪个」「这是一个无状态聚合器，委托给 MetricsClient」。

写入快照（空间平均 + 落窗口）：

[aggregation/aggregator.go:50-58](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/aggregation/aggregator.go#L50-L58) — `ProcessSnapshot`：若快照带错则直接返回错误，否则调用 `client.UpdateMetrics`，注释强调「窗口在首次使用时按内部默认值自动初始化」。

读出双窗口值：

[aggregation/aggregator.go:60-76](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/aggregation/aggregator.go#L60-L76) — `GetAggregatedMetrics`：调用 `client.GetMetricValue` 拿到 `(stableVal, panicVal)`，包进 `AggregatedMetrics` 返回。

真正的存储——`MetricsClient` 与多租户隔离：

[metrics/client.go:56-74](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/metrics/client.go#L56-L74) — `MetricsClient`：注释强调「它**不负责抓取**，抓取由 MetricFetcherFactory 完成」「**该 client 被多个 PodAutoscaler 共享，需要正确隔离**」。隔离键是 `metricKeyStr`，格式见下。

隔离键的来源：

[types/core.go:28-40](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/types/core.go#L28-L40) — `MetricKey.String()` 返回 `"PaNamespace/PaName/MetricName"`。注意它用的是 **PodAutoscaler 的** namespace/name，而非目标工作负载的——这样即便两个 PA 指向同一个工作负载的同一个指标，窗口也不会串数据。

空间平均与双窗口写入：

[metrics/client.go:152-199](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/metrics/client.go#L152-L199) — `UpdateMetrics`：先确保窗口存在（首次自动按默认时长创建），再算 `avg = sum/len`，然后同时写 stable 窗口与 panic 窗口（panic 窗口「仅 KPA 使用」，但数据始终写入，供 KPA 随时读取）。

时间平均读出：

[metrics/client.go:203-232](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/metrics/client.go#L203-L232) — `GetMetricValue`：分别取 `stableWindow.Avg()` 与 `panicWindow.Avg()`；任一窗口缺失都返回错误。

滑动窗口的分桶与淘汰机制：

[types/metrics.go:176-205](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/types/metrics.go#L176-L205) — `TimeWindow.Record`：按 `granularity` 把时间戳分桶（`bucket = ts.UnixNano() / granularity`），同一桶内新值覆盖旧值；再删除早于 `duration` 的老桶，最后按键序重建 `values` 切片以保证确定性。

窗口的平均计算：

[types/metrics.go:215-228](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/types/metrics.go#L215-L228) — `TimeWindow.Avg`：对窗口内当前所有桶值求算术平均；空窗口返回 0。

#### 4.3.4 代码实践：用单元测试验证双层平均

1. **实践目标**：用现成测试确认「空间平均」与「窗口写入」行为。
2. **操作步骤**：阅读 [metrics/client_test.go:28-64](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/metrics/client_test.go#L28-L64) 的 `TestUpdateMetrics`。
3. **观察重点**：测试用 `metricValues := []float64{30.0, 50.0}` 调用 `UpdateMetrics`，然后断言 stable 窗口里只有 1 个值且等于 `40.0`——这正是 `(30+50)/2` 的空间平均。同时断言 stable/panic 窗口与 stable/panic history 各被创建 1 项，key 为 `"default/test-llm-apa/gpu_cache_usage_perc"`（印证 `MetricKey.String()` 格式）。
4. **预期结果**：执行 `go test ./pkg/controller/podautoscaler/metrics/ -run TestUpdateMetrics` 应通过（待本地验证）。即便不运行，从断言 `expectedValue := 40.0` 也能直接看出双层平均中的「空间平均」这一层。

#### 4.3.5 小练习与答案

**练习 1**：若 resync 周期是 10s，stable 窗口 180s，那么 `StableValue` 大致是对多少个周期点做时间平均？

**答案**：约 180/10 = 18 个周期点（受分桶 granularity 影响，实际是窗口内有效桶数）。这也解释了 stable 值为何平滑——它是近 18 次空间平均的再平均。

**练习 2**：`DefaultMetricAggregator` 被注释为「无状态」，但它委托的 `MetricsClient` 明明有状态（持有窗口）。这个「无状态」说法矛盾吗？

**答案**：不矛盾。「无状态」指的是聚合器**自身**不持有可变数据，所有状态都在 `MetricsClient`。这种分离的好处是：聚合器可以安全地被并发调用（线程安全靠 client 的 `sync.RWMutex` 保证），且便于测试——测试时可以注入一个假的 `AggregatorMetricsClient` 而无需构造真实窗口。

---

### 4.4 监控循环：Resync 驱动与伸缩动作记录

#### 4.4.1 概念说明

初学者容易把本讲的「监控」理解错。这里有两件**不同**的事，必须分清：

1. **resync 监控循环（驱动者）**：PodAutoscaler 控制器用一个 10 秒的 ticker，周期性地把所有 PodAutoscaler 对象重新入队，从而**定时触发**一轮 Reconcile → `ComputeDesiredReplicas` → 采集+聚合。这是「时间驱动伸缩」的来源——没有事件时也保证每隔一段时间重新评估。
2. **monitor（记录者）**：`pkg/controller/podautoscaler/monitor/` 是一个 **Prometheus 指标记录器**，它**不参与决策**，只在每次伸缩决策完成后，把「namespace/name/算法/reason/期望副本数」写进一个 GaugeVec，供 Grafana 可观测。

换言之：resync 循环是「定时叫醒」数据管线，monitor 是「把决策结果上报给监控系统」。一个驱动数据流动，一个把流动结果可视化。

#### 4.4.2 核心流程

整条定时驱动链：

```
controller.Run()  (resyncInterval = 10s ticker)
   └─ ticker.C → enqueuePodAutoscalers()        # 把所有 PA 作为 GenericEvent 发到 eventCh
        └─ Reconcile 被触发
             └─ autoScaler.ComputeDesiredReplicas()      # (controller L1174)
                  └─ executeScalingPipeline()            # 采集 → 聚合 → 算法
             └─ 应用副本数后
                  └─ monitor.RecordScaleAction(...)      # (controller L806) 上报 Prometheus
```

窗口时长由 PodAutoscaler spec 决定：

```
stableWindow = ObserveWindowSeconds  ?? DefaultStableWindowDuration(180s)
panicWindow  = PanicWindowSeconds    ?? DefaultPanicWindowDuration(60s)
```

#### 4.4.3 源码精读

resync ticker 循环：

[podautoscaler_controller.go:603-621](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L603-L621) — `Run`：用 `time.NewTicker(r.resyncInterval)` 每 10s 调一次 `enqueuePodAutoscalers`，把所有 PodAutoscaler 作为 `GenericEvent` 发进 `eventCh`，从而触发 Reconcile。注释说明它由 controller-runtime manager 以 RunnableFunc 方式调用，ctx 绑定 manager 生命周期。

resync 间隔与装配：

[podautoscaler_controller.go:99-103](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L99-L103) — `DefaultResyncInterval = 10 * time.Second` 等默认常量。

[podautoscaler_controller.go:116-167](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L116-L167) — `newReconciler`：创建 resource/custom/external 三类 K8s metrics 客户端（任一失败只 warning 不中断），用 `NewDefaultMetricFetcherFactory` 组装工厂，用 `NewDefaultAutoScaler` 组装伸缩器（其内部会 `NewMetricsClient(time.Second)` 与 `NewMetricAggregator`），并 `monitor.New()` 装配监控记录器。granularity 传入 `time.Second`，即 4.3.3 里 `TimeWindow.Record` 的分桶粒度。

窗口时长从 spec 读取：

[autoscaler.go:327-339](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/autoscaler.go#L327-L339) — `metricWindowDurations`：`ObserveWindowSeconds`/`PanicWindowSeconds` 缺省时回落到 `DefaultStableWindowDuration`/`DefaultPanicWindowDuration`。

管线第 2 步先配置窗口再写入：

[autoscaler.go:266-280](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/autoscaler.go#L266-L280) — `executeScalingPipeline` 第 2 步：先 `ConfigureMetricWindows`（把 spec 的窗口时长落到 MetricsClient），再 `ProcessSnapshot`，再 `GetAggregatedMetrics`。

monitor 的 Prometheus 指标定义：

[monitor/metrics.go:27-40](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/monitor/metrics.go#L27-L40) — `autoscalerScaleAction` GaugeVec，标签为 `namespace/name/algorithm/reason`，在 `init()` 里注册到 controller-runtime 的 metrics registry。

monitor 的记录动作：

[monitor/monitor.go:31-38](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/monitor/monitor.go#L31-L38) — `RecordScaleAction`：用标签构造 Labels 并 `Set(float64(desiredReplicas))`，把本次期望副本数记进 Gauge。

#### 4.4.4 代码实践：推算一次 resync 的窗口点数与 monitor 上报

1. **实践目标**：把 resync 周期、窗口时长、monitor 上报三者串起来理解。
2. **操作步骤**：
   - 假设一个 PodAutoscaler 没有设置 `ObserveWindowSeconds`/`PanicWindowSeconds`，于是 stable=180s、panic=60s，resync=10s。
   - 在 [podautoscaler_controller.go:603](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L603) 的 `Run` 里确认每 10s 触发一次 `enqueuePodAutoscalers`。
3. **观察重点**：
   - 每个 tick 产生一次 `CollectMetrics → UpdateMetrics`，即向 stable/panic 窗口各 `Record` 一个空间平均值（granularity=1s 分桶）。
   - 稳态下 stable 窗口约累积 18 个有效桶、panic 窗口约 6 个。
   - 每次 Reconcile 完成伸缩后，[controller.go:806](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/podautoscaler_controller.go#L806) 调 `monitor.RecordScaleAction`，Prometheus 里 `aibrix_autoscaler_scale_action` 这个 Gauge 会被刷新为最新期望副本数。
4. **预期结果**：你能解释「为什么把 `ObserveWindowSeconds` 调小会让伸缩更灵敏但更抖动」——窗口变短，时间平均的点数变少，平滑作用减弱。

#### 4.4.5 小练习与答案

**练习 1**：如果 resync ticker 停了（比如 `Run` 的 ctx 被取消），伸缩会完全停止吗？

**答案**：时间驱动的 resync 会停，但**事件驱动**的伸缩仍可能发生——PodAutoscaler 的增删改、目标 Pod 的变化等仍会触发 Reconcile。resync 的作用是「兜底」：在没有显式事件时也定期重新评估，避免指标变化但无 K8s 事件时伸缩被卡住。

**练习 2**：`monitor.RecordScaleAction` 用的是 `GaugeVec.Set`，为什么不是 `CounterInc`？

**答案**：因为「期望副本数」是一个**可增可减的当前状态值**（今天 5、明天 3），Gauge 适合表示可升可降的瞬时值；Counter 只能单调递增，适合累计事件次数。这里记录的是「最新决策的副本数」而非「累计伸缩次数」，所以用 Gauge。

---

## 5. 综合实践：追踪一个外部指标到 ComputeDesiredReplicas 的完整路径

这是本讲的主实践任务（对应大纲的 `practice_task`）：**描述一个自定义指标从外部源（如经 Prometheus Adapter 暴露的 QPS）到 `ComputeDesiredReplicas` 的完整路径，标注经过哪些 collector / fetcher / aggregator 方法。**

### 场景设定

你有一个推理服务，外部用 Prometheus 收集 `http_requests_per_second`，通过 **prometheus-adapter** 把它暴露成 Kubernetes `external.metrics.k8s.io` API。你希望 AIBrix 据此伸缩。PodAutoscaler 配置（示例 CR 片段，非项目自带文件）：

```yaml
# 示例代码：PodAutoscaler 片段
spec:
  scaleTargetRef:
    name: my-llm
  scalingStrategy: APA
  metricsSources:
    - metricSourceType: external        # 走 external 路径
      targetMetric: http_requests_per_second
      targetValue: "100"
  maxReplicas: 10
```

### 任务步骤

请按顺序在下表的每一行填上**触发它的方法名、所在文件:行号**，以及**该步的输入→输出**。建议你打开源码边读边填。

| 步骤 | 触发者 / 方法 | 源码位置 | 输入 → 输出 |
|------|---------------|----------|-------------|
| 0. 定时唤醒 | `Run` ticker（10s） | podautoscaler_controller.go:603 | tick → enqueue 所有 PA |
| 1. 入口计算 | `ComputeDesiredReplicas` | autoscaler.go:128 | PA spec + Pods → 遍历 MetricsSources |
| 2. 单指标管线 | `executeScalingPipeline` | autoscaler.go:238 | metricKey + source → recommendation |
| 3. 采集编排 | `CollectMetrics`（→ `collectFromExternal`） | collector.go:32 / 86 | CollectionSpec → MetricSnapshot |
| 4. 选 fetcher | `DefaultMetricFetcherFactory.For` | fetcher.go:348 | source → ExternalMetricsFetcher |
| 5. 真正取值 | `fetchFromExternal` → `fetchFromK8sExternalMetrics` | fetcher.go:253 / 292 | external.metrics API → float64（多 item 求和） |
| 6. 配置窗口 | `ConfigureMetricWindows` | autoscaler.go:268 / client.go:125 | spec 时长 → stable/panic 窗口 |
| 7. 写入快照 | `ProcessSnapshot` → `UpdateMetrics` | aggregator.go:50 / client.go:152 | Values → 空间平均后 Record 进双窗口 |
| 8. 读出聚合值 | `GetAggregatedMetrics` → `GetMetricValue` | aggregator.go:60 / client.go:203 | now → (StableValue, PanicValue) |
| 9. 算法决策 | `ComputeRecommendation`（APA 用 `StableValue`） | autoscaler.go:315 / apa.go:39 | AggregatedMetrics → DesiredReplicas |
| 10. 多指标取最大 | 多 metric 取 max | autoscaler.go:179 | 各 metric 结果 → 最佳 DesiredReplicas |
| 11. 上报监控 | `monitor.RecordScaleAction` | podautoscaler_controller.go:806 / monitor.go:31 | 决策结果 → Prometheus Gauge |

### 需要你回答的检查点

1. **在第 5 步**，为什么走的是 `fetchFromK8sExternalMetrics` 而不是 `fetchFromGPUOptimizer`？（提示：看 `MetricSource.Endpoint` 是否为空。）
2. **在第 7 步**，若外部指标只返回一个全局值，`UpdateMetrics` 的「空间平均」实际等于什么？（提示：单元素的平均就是它本身。）
3. **在第 9 步**，APA 拿到的是 `StableValue` 还是 `PanicValue`？为什么 APA 不需要 panic 值？（提示：见 [apa.go:39](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/apa.go#L39)，APA 是按当前副本等比例伸缩，依赖平滑值而非突发值。）
4. **整体**：假如你把 `ObserveWindowSeconds` 从默认 180 改成 30，第 8 步的 `StableValue` 会变得更大、更小还是更抖？为什么？

### 参考答案要点

1. 因为示例 CR 没设 `endpoint`，`fetchFromExternal` 的 `source.Endpoint == ""` 分支命中 K8s external.metrics 路径。
2. 单值的空间平均就是该值本身；它随后被 `Record` 进窗口，参与后续的时间平均。
3. APA 只读 `StableValue`（[apa.go:39](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/apa.go#L39) `SetCurrentUsePerPod(metrics.StableValue)`），因为 APA 按「当前副本 × 利用率/target」等比例计算，需要的是稳态利用率而非突发峰值。
4. 更抖。窗口变短意味着时间平均的有效点数变少（约从 18 个降到 3 个），平滑作用下降，`StableValue` 对近期变化更敏感。

> 说明：以上为源码阅读型实践，结论基于当前 HEAD 的代码逻辑；若要观察真实运行数值需本地部署并接 Prometheus Adapter（待本地验证）。

## 6. 本讲小结

- **三层结构各司其职**：Collector 负责编排（按来源分流、逐 Pod/整体取值、部分失败兜底）；Fetcher 负责按源抓取（四种实现统一成 `FetchPodMetrics` 接口）；Client 负责有状态存储（双窗口+历史）。
- **接口归一化是关键设计**：所有来源（含 external 全局指标）都被适配成「一组 per-pod float64」，让下游聚合只需面对 `[]float64` 一种形状。
- **聚合是双层平均**：先跨 Pod 空间平均（`UpdateMetrics`），再跨周期时间平均（`TimeWindow.Avg`），最终 `StableValue` 是「窗口内各周期空间平均的再平均」。
- **stable/panic 双窗口同时维护**：聚合器总是返回两个值，APA 只用 stable，KPA 在 panic 阈值触发时切用 panic。
- **多租户隔离靠 MetricKey**：`MetricsClient` 被多个 PodAutoscaler 共享，用 `PaNamespace/PaName/MetricName` 作 key 隔离各自窗口。
- **「监控循环」≠「monitor」**：resync ticker（10s）是定时驱动数据管线的循环；`monitor` 包只是把决策结果写进 Prometheus 的 GaugeVec，供 Grafana 可观测，不参与决策。

## 7. 下一步学习建议

- **向上接算法**：本讲产出的 `AggregatedMetrics` 如何被消费？建议接着读 u3-l2，重点看 [algorithm/kpa.go:51-95](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/kpa.go#L51-L95) 如何用 `PanicValue/StableValue` 判定 panic 模式，以及 [algorithm/apa.go:39-55](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/apa.go#L39-L55) 如何用 `StableValue` 等比例伸缩。
- **向下接存储细节**：若对窗口统计感兴趣，可继续读 [types/metrics.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/types/metrics.go) 的 `MetricHistory.GetStats`（方差/标准差）与 `MetricsClient.GetUnifiedStats`/`GetEnhancedStats`，它们为更高级的趋势/置信度分析预留了接口（目前 `GetTrendAnalysis`、`CalculatePodAwareConfidence` 仍是 stub）。
- **接可观测**：想看 monitor 记录的指标长什么样，可阅读 `observability/grafana` 下的 autoscaler 面板配置（见 u11-l3）。
- **接 Python 指标侧**：POD 类型指标的 `/metrics` 数据其实由 Python runtime 边车标准化后暴露，建议在学完 u9-l3（指标采集标准化）后回看本讲 `RestMetricsFetcher` → `EngineMetricsFetcher` 这一环，形成「Python 产指标 → Go 消费指标」的闭环认知。
