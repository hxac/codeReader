# ModelAdapter 调度策略

## 1. 本讲目标

本讲承接 u4-l1（ModelAdapter 与 LoRA 适配器管理）。在 u4-l1 中我们看到：当 `ModelAdapter` 的 `spec.replicas` 设为 `1` 时，控制器不会把适配器加载到所有匹配 Pod，而是要**挑选一个 Pod** 来加载。这个「挑哪个 Pod」的决策，就由本讲的 `scheduling` 包负责。

学完本讲，你应当能够：

1. 说清 `Scheduler` 接口的抽象方式，以及它为什么能让调度策略**可插拔**。
2. 逐个读懂 `random`、`leastAdapters`、`binPack`、`leastLatency`、`leastThroughput` 五种策略的打分目标函数，并区分它们各自适合的场景。
3. 说清策略是如何在控制器启动时被「一次性选定」的（`--model-adapter-scheduler-policy` 标志 → 工厂 → 注入），以及调度器在 `schedulePods` 里被循环调用的方式。

---

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **ModelAdapter 的两种加载模式**（u4-l1）：`spec.replicas` 省略（nil）= 加载到所有匹配 Pod，**不触发调度**；`spec.replicas: 1` = 只加载到一个 Pod，**触发调度**。本讲的调度逻辑只在第二种模式下生效。
- **`pkg/cache` 中央缓存**（u6-l1）：调度策略要查询「每个 Pod 上已加载了哪些模型」「每个 Pod 的实时延迟/吞吐指标」，这些数据全部来自中央缓存，调度器本身不直接访问 Kubernetes API 或推理引擎。
- **指标采集管线**（u9-l3）：基于指标的策略（leastLatency / leastThroughput）依赖运行时边车上报、经标准化后的指标已经写入缓存。
- **Go 接口与工厂模式**：本讲用 `interface` + `switch` 工厂实现策略可插拔，这是 Go 里最常见的扩展点写法。

> 名词速查
> - **适配器（adapter）**：LoRA 参数高效微调产物，体积小、可高密度挂载于同一基座模型。
> - **候选 Pod（candidate pods）**：通过 `spec.podSelector` 匹配且已就绪、可用于加载适配器的 Pod。
> - **bin-packing（装箱）**：一种把物品尽量塞满少数容器、减少碎片化的分配思想。

---

## 3. 本讲源码地图

本讲涉及的关键文件都集中在 `pkg/controller/modeladapter/scheduling/` 目录，外加控制器与配置入口：

| 文件 | 作用 |
| --- | --- |
| [scheduling/scheduler.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/scheduling/scheduler.go) | 定义 `Scheduler` 接口与 `NewScheduler` 工厂（策略选择的总入口）。 |
| [scheduling/random.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/scheduling/random.go) | 随机策略。 |
| [scheduling/least_adapters.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/scheduling/least_adapters.go) | 「最少适配器数」策略，按适配器计数扩散。 |
| [scheduling/bin_pack.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/scheduling/bin_pack.go) | 装箱策略，尽量塞满已较满的 Pod。 |
| [scheduling/least_latency.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/scheduling/least_latency.go) | 「最低延迟」策略，基于排队+推理耗时指标。 |
| [scheduling/least_throughput.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/scheduling/least_throughput.go) | 「最低吞吐」策略，基于 prompt/生成 token 速率。 |
| [modeladapter_controller.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go) | 调用方：在 `newReconciler` 注入调度器，在 `schedulePods` 循环调用 `SelectPod`。 |
| [pkg/config/config.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/config/config.go) | `ModelAdapterOpt.SchedulerPolicyName` 配置载体。 |
| [cmd/controllers/main.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/controllers/main.go) | `--model-adapter-scheduler-policy` 命令行标志定义。 |

---

## 4. 核心概念与源码讲解

### 4.1 Scheduler 接口与可插拔设计

#### 4.1.1 概念说明

调度策略要解决的问题是：**给定一个模型（适配器）和一组候选 Pod，选出「最合适」的一个 Pod**。

「最合适」的定义因场景而异——有时想尽量集中（省资源）、有时想尽量分散（降延迟）、有时无所谓（随机）。如果把这些判断直接写死在控制器里，每改一次策略就要改控制器主流程。AIBrix 的做法是定义一个极简的 `Scheduler` 接口，把「怎么选」完全交给具体实现，控制器只负责「调它」。

这是一个典型的**策略模式（Strategy Pattern）**：

- 控制器面向接口编程，不关心具体策略；
- 每种策略各写一个文件、各实现一个 `SelectPod`；
- 一个工厂函数 `NewScheduler` 按名字把字符串映射成具体实现。

#### 4.1.2 核心流程

调度的整体数据流如下（从配置到选出 Pod）：

```
命令行 --model-adapter-scheduler-policy 标志（默认 "leastAdapters"）
        │
        ▼
config.NewRuntimeConfig(...)  →  ModelAdapterOpt.SchedulerPolicyName
        │
        ▼  （控制器启动时，仅一次）
scheduling.NewScheduler(policyName, cache)  →  switch policyName
        │                                       ├ random      → randomScheduler
        │                                       ├ leastAdapters → leastAdapters
        │                                       ├ binPack     → binPackScheduler
        │                                       ├ leastLatency → leastLatencyScheduler
        │                                       └ leastThroughput → leastThroughputScheduler
        ▼
reconciler.scheduler 字段（持有具体策略对象 + cache 引用）
        │
        ▼  （每次 reconcile，replicas=1 且需要扩容时）
schedulePods → 循环调用 scheduler.SelectPod(ctx, model, readyPods)
        │
        ▼
返回选中的 *v1.Pod，控制器随后经 loraClient 把适配器加载进去
```

注意几个关键点：

1. 策略对象在**控制器启动时创建一次**，之后所有 `ModelAdapter` CR 共用同一个策略实例。
2. 策略对象是**无状态**的——它只持有一个 `cache.Cache` 引用，所有判断所需的数据（Pod 上的模型列表、实时指标）都从缓存实时读取，不在策略对象里保存。这与 u3-l1/u3-l2 里伸缩算法「无状态 + 共享缓存」的设计如出一辙，因此单个策略实例可以被多个 reconcile 安全并发复用。
3. 策略名是**全局**的：整个控制器只有一个策略，没有「每个 CR 用不同策略」的能力（`spec.schedulerName` 字段虽然存在，但当前并不用于切换策略，详见 4.3）。

#### 4.1.3 源码精读

**接口定义**——整个抽象只有一个方法 [scheduling/scheduler.go:28-32](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/scheduling/scheduler.go#L28-L32)：

```go
type Scheduler interface {
    // SelectPod selects a suitable Pod to schedule the model adapter based on the given model and available pods.
    // The input pods is guaranteed to be non-empty and contain only routable pods.
    SelectPod(ctx context.Context, model string, readyPods []v1.Pod) (*v1.Pod, error)
}
```

接口注释里有一句很重要的**契约**：调用方保证 `readyPods` 非空、且只含可路由（routable）的 Pod。这意味着各个策略实现**不需要**处理空切片的边界情况——这是「把不变量上移到调用方」的简化技巧。

**工厂函数** [scheduling/scheduler.go:35-50](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/scheduling/scheduler.go#L35-L50) 用一个 `switch` 把策略名字符串映射成构造函数：

```go
func NewScheduler(policyName string, c cache.Cache) (Scheduler, error) {
    switch policyName {
    case "random":           return NewRandomScheduler(c), nil
    case "leastAdapters":    return NewLeastAdapters(c), nil
    case "binPack":          return NewBinPackScheduler(c), nil
    case "leastLatency":     return NewLeastLatencyScheduler(c), nil
    case "leastThroughput":  return NewLeastThroughputScheduler(c), nil
    default:                 return nil, errors.New("unknown scheduler policy")
    }
}
```

未知策略名会返回错误——由于这个调用发生在控制器启动阶段（见下文），返回错误会让控制器**启动失败**，属于 fail-fast 设计，避免带病运行。

**调用方注入点**在 `newReconciler` 中 [modeladapter_controller.go:162-170](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L162-L170)：

```go
c, err := cache.Get()           // 拿到全局中央缓存单例
if err != nil { klog.Fatal(err) }

scheduler, err := scheduling.NewScheduler(runtimeConfig.ModelAdapterOpt.SchedulerPolicyName, c)
if err != nil { return nil, err }
```

注意三个策略（leastAdapters/binPack/leastLatency/leastThroughput/random）都需要 `cache.Cache`，因此缓存是它们的公共依赖。`cache.Get()` 返回的就是 u6-l1 讲的中央缓存单例。

#### 4.1.4 代码实践

**实践目标**：用 `go doc` 阅读接口与工厂，确认「新增一个策略需要动哪些地方」。

**操作步骤**：

1. 在仓库根目录运行（仅阅读，不修改任何源码）：
   ```bash
   go doc ./pkg/controller/modeladapter/scheduling Scheduler
   go doc ./pkg/controller/modeladapter/scheduling NewScheduler
   ```
2. 观察 `Scheduler` 接口只有一个方法，`NewScheduler` 的返回值是接口类型 `Scheduler`（而不是某个具体 struct 指针）。

**需要观察的现象**：

- `go doc` 输出里 `NewScheduler` 返回 `(Scheduler, error)`——这是面向接口返回，正是可插拔的关键。
- 若传入一个不存在的策略名（你可以只读工厂的 `default` 分支脑补），会得到 `unknown scheduler policy` 错误。

**预期结果**：你会清楚看到，新增策略的最小改动点是「写一个实现 `SelectPod` 的 struct + 在 `NewScheduler` 的 switch 里加一个 `case`」。这一步不需要真的运行控制器，属于源码阅读型实践。

> 待本地验证：`go doc` 命令的具体输出格式依赖本地 Go 版本，但接口方法与返回类型不会有歧义。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Scheduler` 接口的注释要强调「input pods is guaranteed to be non-empty」？如果删掉这个保证，每个策略实现要多写什么代码？

**参考答案**：这是把「候选 Pod 非空」这一不变量上移给调用方。若没有该保证，每个策略在循环前都要先判 `len(readyPods) == 0` 并返回错误或 nil，否则 `randomScheduler` 里 `rand.Intn(0)` 会直接 panic。把不变量集中到调用方，能让策略实现保持极简。

**练习 2**：策略对象为什么只持有 `cache.Cache`、不持有任何可变字段？这带来什么好处？

**参考答案**：因为「选哪个 Pod」所需的输入（模型列表、指标）都能从缓存实时读，策略本身不需要记住历史决策。无状态意味着同一个策略实例可以被多个 `ModelAdapter` 的 reconcile 并发安全地复用，无需加锁、无需为每个 CR 新建对象——这与伸缩算法（u3-l2）的 `algorithmCache` 复用是同一套设计哲学。

---

### 4.2 各策略的打分逻辑

#### 4.2.1 概念说明

五种策略本质都是同一个模板：「遍历候选 Pod → 给每个 Pod 算一个分数 → 选分数最优（最小）的那个」。它们的区别**只在「分数怎么算」**和「分数从哪来」：

| 策略 | 数据来源 | 分数定义 | 选优方向 | 直觉 |
| --- | --- | --- | --- | --- |
| random | 无 | 不打分，直接随机抽 | — | 无偏好，均匀打散 |
| leastAdapters | `ListModelsByPod` | 该 Pod 已加载模型数 | 最小 | 把新适配器放到「最闲」（适配器最少）的 Pod |
| binPack | `ListModelsByPod` | `podCap − 已加载数`（剩余容量） | 最小 | 塞进「最满但还能装」的 Pod，集中装箱 |
| leastLatency | 两个延迟直方图指标 | 排队均值 + 推理均值 | 最小 | 放到端到端延迟最低的 Pod |
| leastThroughput | 两个吞吐指标 | `2·prompt吞吐 + 生成吞吐` | 最小 | 放到当前最不繁忙（吞吐最低）的 Pod |

可以看出一个有趣的对照：`leastAdapters` 和 `binPack` 都用「已加载模型数」这一数据，但一个取最小、一个取「剩余容量最小」——也就是说一个**扩散**、一个**集中**，方向正好相反（见 4.2.3 的精读）。

> 易混点：`leastAdapters` 选的是「已加载模型数最少」的 Pod（最空的）；`binPack` 选的是「剩余容量最小」的 Pod（最满的）。两者都基于适配器计数，但目标函数相反。

#### 4.2.2 核心流程

所有策略的 `SelectPod` 都遵循同一个骨架（伪代码）：

```
best = 空
bestScore = +∞            # random 除外
for pod in readyPods:
    score = computeScore(pod)        # 各策略各算各的
    if score < bestScore:
        best, bestScore = pod, score
return best
```

其中：

- **random** 跳过打分，直接 `rand.Intn(len)` 抽一个。
- **leastAdapters / binPack** 调 `cache.ListModelsByPod(pod.Name, pod.Namespace)` 得到该 Pod 上已加载的模型名列表，用 `len(models)` 当计数。
- **leastLatency** 调两次 `cache.GetMetricValueByPodModel(...)` 取排队时间与推理时间的直方图，用 `.GetHistogramValue().GetMean()` 求均值相加。
- **leastThroughput** 同样调两次 `GetMetricValueByPodModel(...)` 取 prompt 与生成吞吐，用 `.GetSimpleValue()` 取标量，按 `2:1` 加权相加。

对 leastLatency 而言，单 Pod 的延迟分数是：

\[
\text{latency}(pod) = \mathbb{E}[\text{排队时间}] + \mathbb{E}[\text{推理时间}]
\]

对 leastThroughput 而言，单 Pod 的吞吐分数是：

\[
\text{throughput}(pod) = 2 \cdot \text{prompt 吞吐} + \text{生成吞吐}
\]

二者都取**最小值**所在的 Pod。leastThroughput 给 prompt 吞吐 2 倍权重，是因为加载新适配器会主要影响 prefill（prompt 处理）阶段的算力占用，所以更看重 prompt 侧的繁忙程度。

#### 4.2.3 源码精读

**random** ——最简单，直接抽签 [scheduling/random.go:38-44](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/scheduling/random.go#L38-L44)：

```go
func (r randomScheduler) SelectPod(...) (*v1.Pod, error) {
    idx := rand.Intn(len(readyPods))
    selectedPod := readyPods[idx]
    ...
    return &selectedPod, nil
}
```

**leastAdapters** ——选「已加载模型数最少」的 Pod，倾向于把适配器**扩散**到较空的 Pod [scheduling/least_adapters.go:38-55](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/scheduling/least_adapters.go#L38-L55)：

```go
modelAdapterCountMin := math.MaxInt
for _, pod := range readyPods {
    models, err := r.cache.ListModelsByPod(pod.Name, pod.Namespace)
    if err != nil { return nil, err }
    if len(models) < modelAdapterCountMin {
        selectedPod = pod
        modelAdapterCountMin = len(models)
    }
}
```

**binPack** ——选「剩余容量最小」的 Pod，倾向于把适配器**集中**到已较满的 Pod [scheduling/bin_pack.go:38-62](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/scheduling/bin_pack.go#L38-L62)：

```go
podRemainCapMin := math.MaxInt
for _, pod := range readyPods {
    models, err := r.cache.ListModelsByPod(pod.Name, pod.Namespace)
    if err != nil { return nil, err }
    podCap := 10 // todo: replace mock data
    if len(models) >= podCap { continue }     // 装不下的跳过
    if podCap-len(models) < podRemainCapMin {  // 剩余容量最小 = 最满
        selectedPod = pod
        podRemainCapMin = podCap - len(models)
    }
}
```

这里有两个**教学点**，读源码时务必注意：

1. **注释与代码一致，日志不一致**。文件头注释 [bin_pack.go:39](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/scheduling/bin_pack.go#L39) 写的是「choose the pod ... with the least remaining space」（剩余空间最小，即最满），与代码逻辑一致；但随后打印的日志却是 `"pod selected with first fit"`（[bin_pack.go:60](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/scheduling/bin_pack.go#L60)），「first fit」是另一种装箱策略（按顺序选第一个能装下的）。这是日志文案遗留的不准确，**以代码为准**——实际实现是 best-fit（最满优先），不是 first-fit。
2. **容量是写死的 mock 值**。`podCap := 10` 带 `// todo: replace mock data` 注释（[bin_pack.go:49](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/scheduling/bin_pack.go#L49)），目前每个 Pod 的适配器容量上限硬编码为 10，尚未按引擎实际可承载量动态化。

**leastLatency** ——基于两个直方图指标的均值之和 [scheduling/least_latency.go:39-61](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/scheduling/least_latency.go#L39-L61)：

```go
queueTime, err := r.cache.GetMetricValueByPodModel(pod.Name, pod.Namespace, model,
    metrics.RequestQueueTimeSeconds)
...
inferenceTime, err := r.cache.GetMetricValueByPodModel(pod.Name, pod.Namespace, model,
    metrics.RequestInferenceTimeSeconds)
...
podLatency := queueTime.GetHistogramValue().GetMean() + inferenceTime.GetHistogramValue().GetMean()
if podLatency < podLatencyMin { ... }
```

两个指标名是常量 [metrics/metrics.go:30-31](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/metrics/metrics.go#L30-L31)：`request_queue_time_seconds`（排队秒数）与 `request_inference_time_seconds`（推理秒数），都是直方图类型，所以用 `GetHistogramValue().GetMean()` 取均值。注意它取的是 `(pod, model)` 维度的指标——即「这个 Pod 上、这个具体模型」的延迟，而非 Pod 整体。

**leastThroughput** ——基于 prompt 与生成吞吐的加权之和 [scheduling/least_throughput.go:39-61](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/scheduling/least_throughput.go#L39-L61)：

```go
promptThroughput, err := r.cache.GetMetricValueByPodModel(..., metrics.AvgPromptThroughputToksPerMinPod)
...
generationThroughput, err := r.cache.GetMetricValueByPodModel(..., metrics.AvgGenerationThroughputToksPerMinPod)
...
podThroughput := promptThroughput.GetSimpleValue()*2 + generationThroughput.GetSimpleValue()
if podThroughput < podThroughputMin { ... }
```

这两个指标是标量（Gauge），所以用 `GetSimpleValue()` 而非直方图取值（指标常量见 [metrics/metrics.go:81-82](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/metrics/metrics.go#L81-L82)）。`GetSimpleValue()` / `GetHistogramValue()` 都来自 `MetricValue` 接口 [metrics/types.go:91-94](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/metrics/types.go#L91-L94)，直方图均值的实现在 [metrics/types.go:166](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/metrics/types.go#L166)。

> 小坑：leastThroughput 的成功日志也写成了 `"pod selected with least latency"`（[least_throughput.go:59](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/scheduling/least_throughput.go#L59)），是复制粘贴遗留，实际选的是最低吞吐，读日志排查时别被误导。

**数据从哪来**：计数型策略依赖的 `ListModelsByPod` 实现见 [cache/cache_impl.go:114-122](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_impl.go#L114-L122)，它从缓存里的 `metaPod.Models` 取该 Pod 已加载模型名；指标型策略依赖的 `GetMetricValueByPodModel` 见 [cache/cache_impl.go:157-165](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_impl.go#L157-L165)。如果某个 Pod 在缓存里不存在（key does not exist），二者都会返回 error，导致 `SelectPod` 把 error 向上抛——这是为什么基于指标的策略要求缓存已经被运行时指标管线（u9-l3）充分填充。

#### 4.2.4 代码实践

**实践目标**：给定一组候选 Pod 的状态，**手算** binPack 与 leastLatency 两种策略分别会选哪个 Pod，并解释二者在负载分布上的差异。

**场景设定**（这是示例数据，用于对照源码逻辑推算，非项目内置数据）：

3 个候选 Pod，基座模型相同，缓存中观测到的状态为：

| Pod | 已加载模型数（`len(models)`） | 平均排队时间(s) | 平均推理时间(s) | prompt 吞吐 | 生成吞吐 |
| --- | --- | --- | --- | --- | --- |
| pod-a | 8 | 0.4 | 1.0 | 800 | 300 |
| pod-b | 3 | 0.1 | 0.6 | 200 | 100 |
| pod-c | 5 | 0.2 | 0.8 | 500 | 200 |

**操作步骤（推算）**：

1. **binPack**：`podCap = 10`。各 Pod 剩余容量 = `10 − 已加载数`：
   - pod-a: 10−8 = **2**
   - pod-b: 10−3 = **7**
   - pod-c: 10−5 = **5**
   - 取剩余容量最小者 → **pod-a（2）**。
2. **leastLatency**：`latency = 排队均值 + 推理均值`：
   - pod-a: 0.4+1.0 = **1.4**
   - pod-b: 0.1+0.6 = **0.7**
   - pod-c: 0.2+0.8 = **1.0**
   - 取最小者 → **pod-b（0.7）**。
3. （对照）**leastAdapters**：取已加载数最小 → pod-b（3）。**leastThroughput**：`2·prompt + 生成` → pod-a=1900、pod-b=500、pod-c=1200，取最小 → pod-b。

**需要观察的现象与差异解释**：

- 同一组 Pod，**binPack 选 pod-a（最满的）**，**leastLatency 选 pod-b（最闲的）**——结果完全相反。
- **负载分布差异**：binPack 是「集中」策略，会持续往已经较满的 pod-a 塞，导致少数 Pod 越来越热、其余 Pod 保持空闲（利于装箱密度、便于把空闲 Pod 缩容到 0）；leastLatency 是「扩散」策略，会把新适配器推向延迟最低（即最不繁忙）的 pod-b，倾向于让各 Pod 的延迟趋于均衡（利于尾延迟 SLO，但适配器分布更散）。
- 一句话：**binPack 优化「密度/资源利用率」，leastLatency 优化「延迟均衡」**，选哪个取决于你更在意省机器还是压低延迟。

**预期结果**：binPack → pod-a；leastLatency → pod-b。这是从源码逻辑直接推得的确定性结果（只要场景数据给定），无需运行集群即可验证。

> 待本地验证：若要在真实集群复现，需要先让运行时边车把上述指标写入 `pkg/cache`，再创建一个 `replicas: 1` 的 ModelAdapter 观察控制器日志里 `pod selected with ...` 的目标 Pod。

#### 4.2.5 小练习与答案

**练习 1**：leastLatency 和 leastThroughput 都调 `GetMetricValueByPodModel`，为什么前者用 `GetHistogramValue().GetMean()`，后者用 `GetSimpleValue()`？

**参考答案**：因为两者读取的指标**类型不同**。排队/推理时间是直方图（histogram）指标，记录的是一段时间内多次请求的分布，要取均值才能代表「典型延迟」；而 prompt/生成吞吐是 Gauge 标量指标，本身就是单个数值，直接 `GetSimpleValue()` 即可。`MetricValue` 接口同时提供这两种取值方法，正是为了适配不同指标类型（见 [metrics/types.go:91-94](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/metrics/types.go#L91-L94)）。

**练习 2**：leastThroughput 给 prompt 吞吐乘了 2，给生成吞吐乘了 1。如果改成「生成吞吐权重更高」，会对调度倾向产生什么影响？

**参考答案**：加载一个新 LoRA 适配器主要增加 prefill（处理 prompt）阶段的算力压力，所以原实现更看重 prompt 吞吐（2 倍权重），倾向于避开 prompt 阶段繁忙的 Pod。若改成生成吞吐权重更高，调度器会更在意 decode 阶段的繁忙程度，倾向把适配器放到「生成阶段较闲」的 Pod——在长输出（decode 重）的场景下可能更合理，但在短问答（prefill 重）场景下可能选偏。

**练习 3**：binPack 里 `podCap := 10` 是硬编码。如果某个引擎实际只能挂 6 个适配器，当前实现会出什么问题？

**参考答案**：调度器仍会以为该 Pod 还能装到 10 个，可能持续往已挂满（实际已达 6）的 Pod 上调度，导致后续 `loraClient` 调引擎加载时失败、触发 reconcile 重试与退避。这正是 `// todo: replace mock data` 注释要解决的隐患——理想情况应按引擎实际容量（可来自模型画像或配置）动态确定 `podCap`。

---

### 4.3 策略选择与切换

#### 4.3.1 概念说明

知道了五种策略各怎么打分，下一个问题是：**控制器运行时到底用哪一个？能不能动态切换？**

AIBrix 的设计是「**启动时一次性选定，全局生效**」：

- 策略由控制器的命令行标志 `--model-adapter-scheduler-policy` 指定，默认值是 `leastAdapters`。
- 该值在控制器进程启动时被读取，经 `config.RuntimeConfig` 传给 `scheduling.NewScheduler`，构造出一个策略对象，注入到 reconciler 的 `scheduler` 字段。
- 此后整个控制器生命周期、所有 `ModelAdapter` CR 都用这同一个策略，**没有 per-CR 切换策略的机制**。

这意味着切换策略需要**重启控制器进程**（改启动参数），而不能像改 CR 字段那样即时生效。这是一种有意的简化：调度策略属于「运维级」配置，而非「应用级」配置。

> 关于 `spec.schedulerName`：`ModelAdapterSpec` 里确实有一个 `SchedulerName` 字段（[modeladapter_types.go:37-40](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/model/v1alpha1/modeladapter_types.go#L37-L40)，默认 `"default"`），但它**当前并不参与**上面的策略选择——策略完全由启动标志决定。阅读时不要误以为每个 CR 能指定不同策略。

另一个要点：**调度只在 `replicas: 1` 模式下发生**。当 `spec.replicas` 省略（nil）时，控制器走 `reconcileLoadOnAllPods` 分支，把适配器加载到所有匹配 Pod，**根本不调用调度器**（[modeladapter_controller.go:606-617](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L606-L617)）。所以调度策略只对「单副本」适配器有意义。

#### 4.3.2 核心流程

策略从配置到生效的链路：

```
cmd/controllers/main.go
   flag "model-adapter-scheduler-policy" (默认 modeladapter.DefaultModelAdapterSchedulerPolicy)
        │  DefaultModelAdapterSchedulerPolicy = "leastAdapters"   ← 默认策略常量
        ▼
config.NewRuntimeConfig(..., modeladapterSchedulerPolicy)
        │  → RuntimeConfig.ModelAdapterOpt.SchedulerPolicyName
        ▼
modeladapter.newReconciler(mgr, runtimeConfig)
        │  scheduling.NewScheduler(runtimeConfig.ModelAdapterOpt.SchedulerPolicyName, c)
        ▼
reconciler.scheduler  （具体策略对象，整个生命周期不变）
```

调度器在 reconcile 中的调用点是 `schedulePods`。它不是「调一次就完」，而是**循环调用** `SelectPod`——因为 `desiredReplicas` 可能大于 1（虽然目前枚举只允许 `1`，但代码预留了多副本循环选 Pod 的能力）：

```
schedulePods(availablePods, count):
    selected = []
    remaining = availablePods 的拷贝
    for i in 0..count:
        pod = scheduler.SelectPod(ctx, model, remaining)   # 每次从「剩余」里选
        selected.append(pod)
        remaining.remove(pod)                               # 关键：选中后从候选里剔除
    return selected
```

**剔除已选 Pod** 是关键设计：每次 `SelectPod` 后都把选中的 Pod 从 `remainingPods` 里删掉（[modeladapter_controller.go:716-719](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L716-L719)），避免同一轮调度里重复选同一个 Pod。对 binPack 而言，这意味着第二轮会选「剩余候选里次满」的 Pod，从而把多个适配器集中到少数 Pod 上；对 leastLatency 则会依次填到延迟由低到高的几个 Pod。

#### 4.3.3 源码精读

**默认策略常量** [modeladapter_controller.go:110-111](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L110-L111)：

```go
// DefaultModelAdapterSchedulerPolicy is the default scheduler policy for ModelAdapter Controller.
DefaultModelAdapterSchedulerPolicy = "leastAdapters"
```

**命令行标志**定义在控制器入口，默认值就是上面的常量 [cmd/controllers/main.go:153](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/controllers/main.go#L153)：

```go
flag.StringVar(&modeladapterSchedulerPolicy, "model-adapter-scheduler-policy",
    modeladapter.DefaultModelAdapterSchedulerPolicy,
    "model-adapter-scheduler-policy is the name of the scheduler policy to use for model adapter controller.")
```

标志值随后被装入 `RuntimeConfig` [cmd/controllers/main.go:219](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/controllers/main.go#L219)：

```go
runtimeConfig := cfg.NewRuntimeConfig(enableRuntimeSidecar, debugMode, modeladapterSchedulerPolicy)
```

`RuntimeConfig` 只是一个普通结构体，`SchedulerPolicyName` 是其中字符串字段 [config/config.go:25-30](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/config/config.go#L25-L30)：

```go
type ModelAdapterOpt struct {
    // SchedulerPolicyName is the name of the scheduler policy
    // to use for model adapter controller.
    SchedulerPolicyName string
}
```

**循环调用调度器**的 `schedulePods` [modeladapter_controller.go:699-719](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L699-L719)：

```go
func (r *ModelAdapterReconciler) schedulePods(ctx ..., availablePods []corev1.Pod, count int) ([]corev1.Pod, error) {
    if count <= 0 || len(availablePods) == 0 { return nil, nil }
    selectedPods := []corev1.Pod{}
    remainingPods := append([]corev1.Pod{}, availablePods...)
    for i := 0; i < count && len(remainingPods) > 0; i++ {
        pod, err := r.scheduler.SelectPod(ctx, instance.Name, remainingPods)
        if err != nil { return nil, err }
        selectedPods = append(selectedPods, *pod)
        // Remove selected pod from remaining pods to avoid selecting it again
        for j, p := range remainingPods {
            if p.Name == pod.Name {
                remainingPods = append(remainingPods[:j], remainingPods[j+1:]...)
                ...
```

注意 `SelectPod` 的第二个参数传的是 `instance.Name`（即 ModelAdapter CR 的名字），它对应策略里 `GetMetricValueByPodModel` 的 `model` 形参——也就是说 leastLatency/leastThroughput 查的是「这个适配器名」维度下的指标。`reconciler.scheduler` 字段的声明见 [modeladapter_controller.go:286](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L286)。

**测试中的接口替身**：控制器的测试用一个 `mockScheduler` 实现 `Scheduler` 接口，按预设名字返回指定 Pod [modeladapter_controller_test.go:478-504](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller_test.go#L478-L504)。这正好印证了「面向接口」的好处——测试时可以注入任意替身，不必真的起缓存和指标管线。

#### 4.3.4 代码实践

**实践目标**：把「标志 → 配置 → 工厂 → 注入」这条链完整跟一遍，确认策略切换的正确方式。

**操作步骤（源码跟踪，不修改源码）**：

1. 打开 [cmd/controllers/main.go:153](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/controllers/main.go#L153)，记下标志名 `model-adapter-scheduler-policy` 与默认值来源。
2. 跳到 [main.go:219](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/controllers/main.go#L219)，看标志值如何进入 `RuntimeConfig`。
3. 跳到 [modeladapter_controller.go:167](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/modeladapter_controller.go#L167)，看 `NewScheduler` 如何把它变成策略对象。
4. 跳到 [scheduler.go:36-49](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/scheduling/scheduler.go#L36-L49)，确认每个合法策略名对应的 `case`。

**需要观察的现象**：

- 整条链路上**没有任何地方**读取 `ModelAdapter` CR 的 `spec.schedulerName` 字段来做策略选择——从而验证「策略是全局的、由启动标志决定」。
- 链路上策略对象只被构造一次（在 `newReconciler` 里），之后只被读、不被替换——从而验证「运行期不可热切换」。

**预期结果**：你会得出结论——要切换策略，正确做法是修改控制器启动参数 `--model-adapter-scheduler-policy=<策略名>` 然后重启控制器 Pod；改 CR 字段无效。

> 待本地验证：可在本地 `make build` 后用 `./bin/manager --model-adapter-scheduler-policy=binPack ...` 启动（需配合测试集群/缓存），观察日志中 `pod selected with ...` 的变化。纯源码跟踪则无需运行。

#### 4.3.5 小练习与答案

**练习 1**：同事想在「不同的 ModelAdapter CR 上用不同调度策略」，直接改 `spec.schedulerName` 行不行？如果要做这个功能，至少要改哪几处？

**参考答案**：不行。当前 `spec.schedulerName` 不参与策略选择，策略只由启动标志 `--model-adapter-scheduler-policy` 全局决定。要支持 per-CR 策略，至少要：① 在 `reconcileLoadOnSinglePod` 里读取 `instance.Spec.SchedulerName`；② 把策略对象的构造从「启动时一次」改成「每次 reconcile 按需 `NewScheduler`」（或缓存多策略实例）；③ 给 `SchedulerName` 加合法值校验（webhook），避免传未知策略名。注意这会与「无状态策略实例复用」的设计产生张力，需权衡。

**练习 2**：`schedulePods` 为什么要把选中的 Pod 从 `remainingPods` 里删掉，而不是每次都对完整列表调用 `SelectPod`？

**参考答案**：因为大多数策略（除 random 外）是**确定性**的——对同一份候选列表，`SelectPod` 每次会返回同一个最优 Pod。若不剔除，循环 `count` 次会重复选中同一个 Pod，无法选出 `count` 个不同的 Pod。剔除已选项后，下一轮 `SelectPod` 才会落到「次优」的 Pod 上，从而选出互不相同的多个目标（这也让 binPack 天然具有「集中到少数 Pod」的效果）。

**练习 3**：为什么策略选择放在控制器**启动**时（fail-fast），而不是每次 reconcile 时延迟构造？

**参考答案**：启动时构造可以让 `NewScheduler` 的「未知策略名」错误立即暴露、直接让控制器启动失败，避免带病运行后才发现策略无效；同时也让策略对象只构造一次、被所有 reconcile 复用，性能更好。若延迟到每次 reconcile 构造，既会反复付出构造开销，又会让「非法策略名」错误推迟到首个 CR 处理时才暴露，排查更难。

---

## 5. 综合实践

**任务**：为一个「Prefill 较重、关心端到端延迟」的在线问答场景，挑选合适的 ModelAdapter 调度策略，并验证你的选择。

1. **阅读选型**：回顾 4.2 的策略对照表。在适配器多副本集中部署（想省 GPU、可接受单 Pod 偏热）时，应选 `binPack`；在想让延迟均衡、避免热点 Pod 时，应选 `leastLatency`。请为「关心尾延迟」的场景写下你的选择与理由。
2. **配置切换**：写出把控制器切换到你所选策略的完整做法（提示：修改哪个启动标志、是否需要重启）。
3. **手算验证**：沿用 4.2.4 的场景表，假设现在要**连续调度 2 个**不同的 `replicas:1` 适配器，用你选的策略推算这两个适配器分别会落到哪个 Pod（注意第二次调度时，第一个适配器已加载、`ListModelsByPod` 计数 +1）。例如选 `binPack`：第一次选 pod-a（剩余容量 2 最小），加载后 pod-a 计数变为 9、剩余容量 1；第二次再选时各 Pod 剩余容量为 a=1、b=7、c=5，仍选 pod-a——两个适配器都集中到 pod-a，体现「集中」特性。换用 `leastLatency` 重算一遍，对比分布差异。
4. **源码定位**：在 [scheduler.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modeladapter/scheduling/scheduler.go) 里找到你要新增一个「`least_gpu_memory`（选 GPU 显存最空闲的 Pod）」策略需要改动的最小位置（提示：新建一个 `.go` 文件实现 `SelectPod`、用 `GetMetricValueByPodModel` 或类似缓存 API 取显存指标、在 `NewScheduler` 加一个 `case`），写出改动清单。

> 这个任务把「选型 → 配置 → 推算 → 扩展」四步串起来，覆盖了本讲的全部三个最小模块。

---

## 6. 本讲小结

- `scheduling` 包用**策略模式**解耦了「选哪个 Pod」：`Scheduler` 接口只有一个 `SelectPod` 方法，五种策略各实现一个，`NewScheduler` 工厂按名字装配。
- 五种策略本质都是「遍历候选 → 算分 → 取最小」，区别只在分数定义与数据来源：`random`（无）、`leastAdapters`/`binPack`（用 `ListModelsByPod` 的适配器计数，但前者扩散、后者集中）、`leastLatency`（排队+推理延迟直方图均值）、`leastThroughput`（2·prompt + 生成吞吐标量）。
- 策略对象**无状态**，只持有 `cache.Cache` 引用，启动时构造一次、全局复用、并发安全——与伸缩算法（u3）共享同一设计哲学。
- 策略由启动标志 `--model-adapter-scheduler-policy` **一次性选定**（默认 `leastAdapters`），运行期不可热切换；`spec.schedulerName` 字段当前不参与选择。
- 调度只在 `spec.replicas: 1` 时触发；`schedulePods` 通过**循环调用 + 剔除已选 Pod** 选出互不相同的目标，使 binPack 天然集中、leastLatency 天然扩散。
- 读源码要注意两处「日志/字段误导」：binPack 实际是 best-fit（最满优先）而非日志宣称的 first-fit；leastThroughput 的成功日志误写成「least latency」；`podCap` 仍是 `// todo` 的硬编码 mock 值。

---

## 7. 下一步学习建议

- **走向调用方**：回看 u4-l1 的 `reconcileLoading`，把「`schedulePods` 选出 Pod → `loraClient` 加载适配器 → 更新 `Status.Instances`」整条链补全，理解调度决策如何落地为引擎动作。
- **深入数据来源**：调度策略高度依赖 `pkg/cache`。建议接着学 u6-l1（中央缓存架构）与 u6-l2（Pod 发现与模型画像），看清 `ListModelsByPod` 背后的 `metaPod.Models` 是如何被维护的。
- **理解指标管线**：leastLatency/leastThroughput 依赖的 `request_queue_time_seconds`、吞吐等指标来自运行时边车的标准化流程，详见 u9-l3（指标采集标准化），那是把这些指标写进缓存的「上游」。
- **对比另一套调度**：u4-l3 的 ModelClaim 也有放置决策（`placement.go`/`pool_policy.go`），可对比二者在「选 Pod」问题上的不同抽象与目标，加深对 AIBrix 多种编排方式的理解。
