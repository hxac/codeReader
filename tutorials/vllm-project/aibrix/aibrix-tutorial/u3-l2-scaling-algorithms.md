# 伸缩算法 APA / HPA / KPA

## 1. 本讲目标

在上一讲（u3-l1）里，我们已经接通了「从 PodAutoscaler CR 到期望副本数」的整条管线：控制器 `Reconcile` → `DefaultAutoScaler.ComputeDesiredReplicas` → `executeScalingPipeline`（采集 → 配窗口 → 聚合 → 算法）。那条管线里真正决定「到底要几个副本」的一步，是调用一个 `ScalingAlgorithm`。

本讲就钻进这一个点，读完之后你应该能够：

- 说清 `ScalingAlgorithm` 接口长什么样、为什么被设计成**无状态**的。
- 区分 **APA、HPA、KPA** 三种策略各自的数学公式、适用场景与实现差异。
- 解释策略字符串（`HPA/KPA/APA`）是如何通过 `NewScalingAlgorithm` 工厂映射到具体算法，并被 `DefaultAutoScaler` 缓存复用的。
- 能够照着接口写出**一个最小的自定义算法**的伪代码，并指出它需要在工厂的哪里注册。

---

## 2. 前置知识

在进入源码前，先用大白话建立直觉。

### 2.1 什么是「伸缩策略」

自动伸缩的核心问题只有一句话：**给定当前观测到的指标，应该把副本数调成多少？** 不同策略只是用不同公式回答这一个问题。AIBrix 提供了三种内置策略：

| 策略 | 全称 | 灵感来源 | 一句话定位 |
|------|------|----------|------------|
| **KPA** | Knative Pod Autoscaling | Knative 的 KPA | 双窗口（stable/panic），能抗突发流量、支持缩到 0 |
| **APA** | AiBrix Pod Autoscaling | 一篇高并发 HPA 论文（见源码注释） | 单窗口、按当前利用率等比例放大缩小 |
| **HPA** | Horizontal Pod Autoscaling | Kubernetes 原生 HPA | 委托给 K8s 原生 HPA 控制器，本算法只是占位 |

> 注意大小写：CR 字段里的枚举值是**大写** `HPA/KPA/APA`（写在 CRD 里），而每个算法 `GetAlgorithmType()` 返回的标签是**小写** `"hpa"/"kpa"/"apa"`（写在日志和 `ScalingRecommendation` 里）。两者不要混淆。

### 2.2 为什么算法必须无状态

上一讲提到，`DefaultAutoScaler` 用一个 `algorithmCache` 缓存算法对象并跨 goroutine 复用。这要求算法对象本身**不能持有本次请求相关的可变状态**——否则并发调用会互相踩踏。AIBrix 的解法是：把所有「会变的数据」都塞进调用参数（`ScalingRequest`）和一张外部上下文（`ScalingContext`），算法结构体本身是空的 `struct{}`。这是后续理解每个算法实现的前提。

### 2.3 你需要记得的两个数据载体

- **`AggregatedMetrics`**：聚合后的指标，关键字段是 `StableValue`（长窗口均值，决策稳）和 `PanicValue`（短窗口均值，反应快）。
- **`ScalingContext`**：一张「配置大杂烩」，装着 target 目标值、容差、最大伸缩速率、panic 阈值、min/max 副本等所有参数。算法只读/写它，不自己存。

这两者的具体字段会在第 4 节结合源码展开。

---

## 3. 本讲源码地图

本讲涉及的关键文件都集中在 `pkg/controller/podautoscaler/` 下：

| 文件 | 作用 |
|------|------|
| `algorithm/algorithm.go` | 定义 `ScalingAlgorithm` 接口、入参 `ScalingRequest`、出参 `ScalingRecommendation`、工厂 `NewScalingAlgorithm`、公共的 `applyConstraints` 边界裁剪 |
| `algorithm/kpa.go` | KPA 策略：stable/panic 双窗口，最复杂的实现 |
| `algorithm/apa.go` | APA 策略：单窗口、按利用率等比例伸缩，带容差与速率限制 |
| `algorithm/hpa.go` | HPA 策略：占位实现，真正伸缩交给 K8s 原生 HPA |
| `context/context.go` | `ScalingContext` 接口与默认实现 `baseScalingContext`，算法的全部参数来源 |
| `autoscaler.go` | 算法的**消费者**：`getOrCreateAlgorithm` 缓存复用、`executeScalingPipeline` 组装 `ScalingRequest` 并调用算法 |
| `api/autoscaling/v1alpha1/podautoscaler_types.go` | `ScalingStrategyType` 枚举常量 `HPA/KPA/APA` |

> 阅读建议：先看 `algorithm.go` 的接口（抽象），再看三个 `*_algorithm.go`（实现），最后回到 `autoscaler.go`（消费者），就能形成闭环。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1** `ScalingAlgorithm` 接口与无状态数据契约
- **4.2** APA / HPA / KPA 三种策略实现对比
- **4.3** 策略工厂与选择逻辑（算法如何被选中并复用）

---

### 4.1 ScalingAlgorithm 接口与无状态数据契约

#### 4.1.1 概念说明

`ScalingAlgorithm` 是整个伸缩算法体系的**抽象基类**。它只有一个核心职责：吃进「当前状态 + 指标 + 配置」，吐出「建议副本数」。设计上有两个铁律：

1. **无状态**：接口注释明确写着「Implementations must be stateless and thread-safe for concurrent use」。结构体里不该有任何字段。
2. **配置随请求走**：所有配置都通过 `ScalingRequest` 传入，算法不自己 new 配置对象。这样同一个算法实例可以同时服务多个 PodAutoscaler 而不串台。

数据契约是单向的：`ScalingRequest`（入） → 算法 → `ScalingRecommendation`（出）。

#### 4.1.2 核心流程

```
┌─────────────────────────────────────────────────────────────┐
│  ScalingRequest                                             │
│   ├── Target            （伸缩谁：ns/name/kind）            │
│   ├── CurrentReplicas   （当前副本数）                      │
│   ├── AggregatedMetrics （聚合指标：StableValue/PanicValue）│
│   ├── ScalingContext    （配置：target/容差/速率/min/max）  │
│   └── Timestamp                                             │
└──────────────────────────────┬──────────────────────────────┘
                               │  algo.ComputeRecommendation()
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  ScalingRecommendation                                      │
│   ├── DesiredReplicas （期望副本数）                        │
│   ├── Confidence      （指标置信度）                        │
│   ├── Reason          （人类可读理由）                      │
│   ├── Algorithm       （算法标签，如 "kpa"）                │
│   ├── ScaleValid      （本次建议是否有效）                  │
│   └── Metadata        （调试用的额外键值）                  │
└─────────────────────────────────────────────────────────────┘
```

注意：算法算出来的 `DesiredReplicas` 还要经过一层 `applyConstraints` 用 `min/max` 副本做兜底裁剪，确保不会超出 PodAutoscaler 声明的边界。

#### 4.1.3 源码精读

接口本体只有两个方法。[algorithm.go:33-40](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/algorithm.go#L33-L40) 定义了 `ComputeRecommendation`（算副本）和 `GetAlgorithmType`（报身份）：

```go
type ScalingAlgorithm interface {
    ComputeRecommendation(ctx context.Context, request ScalingRequest) (*ScalingRecommendation, error)
    GetAlgorithmType() string
}
```

入参 [algorithm.go:43-49](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/algorithm.go#L43-L49) 把所有「变的东西」打包：

```go
type ScalingRequest struct {
    Target            types.ScaleTarget
    CurrentReplicas   int32
    AggregatedMetrics *types.AggregatedMetrics
    ScalingContext    scalingctx.ScalingContext
    Timestamp         time.Time
}
```

其中 `AggregatedMetrics` 定义在 [types/metrics.go:27-35](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/types/metrics.go#L27-L35)，关键字段是 `StableValue`、`PanicValue`、`Trend`、`Confidence`。

出参 [algorithm.go:52-59](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/algorithm.go#L52-L59)：

```go
type ScalingRecommendation struct {
    DesiredReplicas int32
    Confidence      float64
    Reason          string
    Algorithm       string
    ScaleValid      bool
    Metadata        map[string]interface{}
}
```

公共的边界裁剪函数 [algorithm.go:77-85](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/algorithm.go#L77-L85)，三种策略算完都会调它，用 `ScalingContext` 里的 min/max 把副本数夹住：

```go
func applyConstraints(replicas int32, context scalingctx.ScalingContext) int32 {
    if replicas < context.GetMinReplicas() { return context.GetMinReplicas() }
    if replicas > context.GetMaxReplicas() { return context.GetMaxReplicas() }
    return replicas
}
```

> 关键观察：算法**只负责算**，边界保护由 `applyConstraints` 统一兜底。这是一种典型的职责分离——算法可以放心写纯数学，不用每个都重复写 min/max 检查。

`ScalingContext` 是算法读写的「配置面板」，[context.go:31-56](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/context/context.go#L31-L56) 列出了它全部的 getter/setter。默认值在 [context.go:106-118](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/context/context.go#L106-L118)：最大扩容速率 2、最大缩容速率 2、上下容差各 10%、panic 阈值 2.0、缩容冷却 5 分钟。这些都可以被 PodAutoscaler 的注解覆盖。

#### 4.1.4 代码实践：验证无状态契约

1. **实践目标**：确认三个算法结构体确实是空的，没有字段。
2. **操作步骤**：在仓库根目录用 `Grep` 搜索三个算法类型的定义。
3. **需要观察的现象**：三处定义都应是 `type XxxAlgorithm struct{}`，花括号里什么都没有。
4. **预期结果**：你会看到
   - `type APAAlgorithm struct{}`（[apa.go:30](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/apa.go#L30)）
   - `type HPAAlgorithm struct{}`（[hpa.go:25](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/hpa.go#L25)）
   - `type KPAAlgorithm struct{}`（[kpa.go:32](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/kpa.go#L32)）

   此外每个文件都有一行编译期断言 `var _ ScalingAlgorithm = (*XxxAlgorithm)(nil)`（如 [kpa.go:34](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/kpa.go#L34)），用来保证结构体实现了接口。这是 Go 里检查接口实现的惯用法。

5. 这一步只是阅读与搜索，不修改源码，**待本地验证**搜索结果。

#### 4.1.5 小练习与答案

**练习 1**：如果有人把 `KPAAlgorithm` 改成 `type KPAAlgorithm struct{ lastReplicas int32 }` 并在 `ComputeRecommendation` 里读写它，会出现什么问题？

> **答案**：因为 `DefaultAutoScaler` 用 `algorithmCache` 把同一个 KPA 实例缓存复用、且会被多个 PodAutoscaler 的 reconcile 并发调用（不同 goroutine），带状态的字段会产生数据竞争（data race），还会让不同 PA 的决策互相串台。这正是接口注释要求「stateless and thread-safe」的原因。

**练习 2**：`applyConstraints` 是接口的一部分吗？为什么把它放在包级函数而不是接口方法里？

> **答案**：不是。它是包级私有辅助函数。放成接口方法会让所有实现被迫重复实现 min/max 逻辑；放成包级函数则让「边界兜底」成为所有策略共享的公共行为，算法实现只管算数学，符合职责分离。

---

### 4.2 APA / HPA / KPA 三种策略实现对比

#### 4.2.1 概念说明

三种策略虽然实现同一个接口，但「怎么算副本」差别很大：

- **KPA**（最复杂）：维护 stable（长窗口，决策稳）和 panic（短窗口，反应快）两套指标。平时用 stable 决策；一旦短窗口指标暴涨超过 panic 阈值，切到 panic 模式快速扩容，且 panic 模式下**只增不减**，避免抖动。支持 activation scale（从 0 唤醒时直接给到指定副本）。
- **APA**：只用 stable 窗口的当前值，按「当前每 Pod 利用率 / 目标利用率」的比例等比例伸缩。带上下容差，超出容差才动。没有 panic 概念。
- **HPA**：**不做任何计算**，直接返回当前副本数。真正的伸缩交给 Kubernetes 原生 HPA 资源去完成（那部分逻辑在 `hpa_resources.go` / `workload_scale.go`，不在本算法包里）。

#### 4.2.2 核心流程（三种算法的数学对比）

先约定记号：`currentPodCount` = 当前副本数，`target` = 目标指标值，`upTol`/`downTol` = 上下容差，`maxUp`/`maxDown` = 最大扩/缩容速率。

**APA 的公式**（单窗口，按比例伸缩）：

APA 先把当前利用率设进 context：`currentUsePerPod = StableValue`。令比例

\[
r = \frac{\text{currentUsePerPod}}{\text{target}}
\]

\[
\text{expectedPods} = \lceil \text{currentPodCount} \times r \rceil
\]

判定：

- 若 \( r > 1 + \text{upTol} \)：扩容。`maxScaleUp = ceil(maxUp × currentPodCount)`，`expectedPods = min(expectedPods, maxScaleUp)`。
- 若 \( r < 1 - \text{downTol} \)：缩容。`maxScaleDown = floor(currentPodCount / maxDown)`，`expectedPods = max(expectedPods, maxScaleDown)`。
- 否则：容差范围内，**保持不变**。

注意 APA 的 `expectedPods` 是 `currentPodCount × r`，**与当前副本数成正比**——这是「利用率型指标」（如 GPU cache 利用率百分比）的典型算法。

**KPA 的公式**（双窗口）：

KPA 同时算 stable 和 panic 两个候选：

\[
\text{dspc} = \lceil \frac{\text{observedStableValue}}{\text{target}} \rceil,\quad
\text{dppc} = \lceil \frac{\text{observedPanicValue}}{\text{target}} \rceil
\]

（仅当指标超出容差带 `target×(1±tol)` 时才重算，否则维持当前副本。）再用速率限幅：

\[
\text{maxScaleUp} = \max(1, \lceil \text{maxUp} \times \text{readyPods} \rceil),\quad
\text{maxScaleDown} = \lfloor \frac{\text{readyPods}}{\text{maxDown}} \rfloor
\]

\[
\text{desiredStablePodCount} = \text{clamp}(\text{dspc},\ \text{maxScaleDown},\ \text{maxScaleUp})
\]

然后：

1. 若 `activationScale > 1` 且算出的副本数大于 0 但小于 activationScale，则抬到 activationScale（从 0 唤醒时一把起够）。
2. 是否进入 panic 模式由 `shouldEnterPanicMode` 判断：`panicValue / stableValue > panicThreshold`（默认 2.0）即进入。
3. **panic 模式下取 `max(stable, panic)`，且不缩容**——用一个高水位 `maxPanicPods` 记住峰值，只会涨不会跌，直到退出 panic 模式。

> **APA 与 KPA 的本质区别**：APA 的期望副本 = `currentPods × (利用率/目标)`（与当前副本成正比）；KPA 的期望副本 = `ceil(指标值/目标)`（直接由指标值算出绝对值），再用速率限幅相对当前副本收敛。APA 单窗口、不看趋势；KPA 双窗口、靠 panic 机制抗突发。

**HPA 的公式**：无。直接 `DesiredReplicas = CurrentReplicas`。

#### 4.2.3 源码精读

**APA** — [apa.go:35-59](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/apa.go#L35-L59) 的 `ComputeRecommendation` 把 `StableValue` 当作当前每 Pod 利用率塞进 context，然后调内部 `computeTargetReplicas`，最后 `applyConstraints` 兜底：

```go
// APA uses current value directly
request.ScalingContext.SetCurrentUsePerPod(metrics.StableValue)
desiredReplicas := a.computeTargetReplicas(float64(request.CurrentReplicas), request.ScalingContext,
    request.AggregatedMetrics.MetricKey.MetricName)
desiredReplicas = applyConstraints(desiredReplicas, request.ScalingContext)
```

核心公式在 [apa.go:70-110](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/apa.go#L70-L110)。注释 [apa.go:66-69](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/apa.go#L66-L69) 标明了它参考的论文（Huo 等人 2023 年关于高并发 HPA 的研究）。容差判定与扩容分支 [apa.go:91-98](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/apa.go#L91-L98)：

```go
if currentUsePerPod/expectedUse > (1 + upTolerance) {
    maxScaleUp := int32(math.Ceil(context.GetMaxScaleUpRate() * currentPodCount))
    expectedPods := int32(math.Ceil(currentPodCount * (currentUsePerPod / expectedUse)))
    if expectedPods > maxScaleUp { expectedPods = maxScaleUp }
    return expectedPods
}
```

缩容分支 [apa.go:99-107](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/apa.go#L99-L107) 结构对称，只是用 `floor` 算下限。

**HPA** — [hpa.go:30-41](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/hpa.go#L30-L41) 整个实现就是占位：

```go
func (a *HPAAlgorithm) ComputeRecommendation(...) (*ScalingRecommendation, error) {
    // HPA scaling is handled by Kubernetes HPA controller
    return &ScalingRecommendation{
        DesiredReplicas: request.CurrentReplicas,   // 不变
        Reason:          "HPA managed by Kubernetes",
        Algorithm:       "hpa",
        ScaleValid:      true,
        ...
    }, nil
}
```

> 为什么 HPA 要留一个空实现？因为 `DefaultAutoScaler` 对所有策略走同一条 `executeScalingPipeline`，需要一个对象占位以满足接口；真正的副本控制由 `hpa_resources.go` 创建/同步原生 HPA 资源来完成（那是 u3-l4 的内容）。这种「接口齐全、实现委托」的做法让上层管线不用 if/else 区分策略。

**KPA** — [kpa.go:42-83](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/kpa.go#L42-L83) 的 `ComputeRecommendation` 先决定走 stable 还是 panic：

```go
if a.shouldEnterPanicMode(metrics, request.ScalingContext.GetPanicThreshold()) {
    currentValue = metrics.PanicValue
    mode = "panic"
    request.ScalingContext.SetInPanicMode(true)
} else {
    currentValue = metrics.StableValue
    mode = "stable"
    request.ScalingContext.SetInPanicMode(false)
}
```

`shouldEnterPanicMode` [kpa.go:91-96](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/kpa.go#L91-L96)：短窗口值是长窗口值的 `panicThreshold` 倍以上就进入 panic（`stableValue <= 0` 时也进 panic，便于从 0 唤醒）：

```go
func (a *KPAAlgorithm) shouldEnterPanicMode(metrics *types.AggregatedMetrics, panicThreshold float64) bool {
    if metrics.StableValue <= 0 { return true }
    return metrics.PanicValue/metrics.StableValue > panicThreshold
}
```

核心的 `computeTargetReplicas` 在 [kpa.go:100-191](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/kpa.go#L100-L191)。stable/panic 两个候选的计算与限幅 [kpa.go:125-142](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/kpa.go#L125-L142)：

```go
if observedStableValue > scaleUpThreshold || observedStableValue < scaleDownThreshold {
    dspc = math.Ceil(observedStableValue / targetValue)
} else {
    dspc = currentPodCount            // 容差内，维持
}
// ... dppc 同理用 observedPanicValue
desiredStablePodCount := int32(math.Min(math.Max(dspc, maxScaleDown), maxScaleUp))
desiredPanicPodCount  := int32(math.Min(math.Max(dppc, maxScaleDown), maxScaleUp))
```

activation scale 抬升 [kpa.go:145-154](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/kpa.go#L145-L154)：从 0 唤醒时如果算出的副本小于 activationScale，就抬到它。

最关键的是 panic 模式下「只增不减」的高水位逻辑 [kpa.go:168-188](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/kpa.go#L168-L188)：

```go
desiredPodCount := desiredStablePodCount
if inPanicMode {
    if desiredPodCount < desiredPanicPodCount { desiredPodCount = desiredPanicPodCount }
    // 不在 panic 模式缩容，只涨不跌，用 maxPanicPods 记峰值
    if desiredPodCount > maxPanicPods {
        maxPanicPods = desiredPodCount
        context.SetMaxPanicPods(maxPanicPods)
    }
    desiredPodCount = maxPanicPods
}
```

注意 `maxPanicPods` 是写到 `ScalingContext` 里的（不是算法结构体里），所以它随 PA 维度存续，仍满足算法结构体无状态的要求。

#### 4.2.4 代码实践：用表驱动测试推演 KPA

1. **实践目标**：通过阅读现有测试，亲手验证 KPA 的 stable/panic/缩到 0/速率限制等行为。
2. **操作步骤**：打开 [algorithm/kpa_test.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/kpa_test.go)，它用表驱动测试覆盖了 7 个场景。重点看这几个用例的输入与期望：
   - `stable_mode_basic_scaling`：`StableValue=20, target=10`，期望 2（`ceil(20/10)`）。
   - `panic_mode_scaling_up`：`StableValue=30, PanicValue=40, target=10, InPanicMode=true`，期望 4（取 panic 的 `ceil(40/10)`）。
   - `max_scale_up_rate_limit`：`currentPodCount=2, MaxScaleUpRate=1.5, StableValue=100, target=10`，本来要 10 个，但被 `ceil(2×1.5)=3` 限到 3。
3. **需要观察的现象**：测试用 `mockScalingContext`（[algorithm/mock_context_test.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/mock_context_test.go)）构造一个假的 `ScalingContext`，从而把算法与真实指标采集完全解耦——这正是无状态设计带来的可测性红利。
4. **预期结果**：直接运行 `go test ./pkg/controller/podautoscaler/algorithm/ -run TestKPAAlgorithm -v` 应全部通过。**待本地验证**运行结果（本环境不替你执行命令）。

#### 4.2.5 小练习与答案

**练习 1**：同样 `currentPodCount=4`、当前利用率 80、target 50、`maxUp=2`、`upTol=0.1`，APA 会扩到几个？

> **答案**：`r = 80/50 = 1.6 > 1.1`，触发扩容；`expectedPods = ceil(4 × 1.6) = ceil(6.4) = 7`；`maxScaleUp = ceil(2 × 4) = 8`；`7 < 8`，不触发限幅，最终 7（之后还要经 `applyConstraints` 用 min/max 兜底）。

**练习 2**：为什么 HPA 的 `ComputeRecommendation` 直接返回当前副本数，而不是返回 0 或报错？

> **答案**：因为 HPA 的真正伸缩由 Kubernetes 原生 HPA 控制器完成，AIBrix 这条 `executeScalingPipeline` 管线对 HPA 来说只是「占位走流程」。返回当前副本数意味着「本轮我建议不变」，避免 AIBrix 的伸缩器与原生 HPA 抢着改副本数。返回 0 会把工作负载删空，报错会让整条管线失败，都不合理。

---

### 4.3 策略工厂与选择逻辑

#### 4.3.1 概念说明

策略字符串（`HPA/KPA/APA`）是写在 PodAutoscaler CR 的 `spec.scalingStrategy` 字段里的。运行时需要把它映射成一个具体的算法对象——这就是**工厂** `NewScalingAlgorithm` 的职责。它是一个简单的 `switch`，但配合 `DefaultAutoScaler` 的缓存，形成了「按策略取算法、按策略缓存复用」的机制。

#### 4.3.2 核心流程

```
PodAutoscaler.Spec.ScalingStrategy  (例如 "KPA")
        │
        ▼
DefaultAutoScaler.getOrCreateAlgorithm(strategy)
        │  ① 先读缓存（RLock）—— 命中直接返回
        │  ② 未命中再加写锁（WLock），double-check 后创建
        ▼
algorithm.NewScalingAlgorithm(strategy)   ← 工厂 switch
        │   KPA  -> &KPAAlgorithm{}
        │   APA  -> &APAAlgorithm{}
        │   HPA  -> &HPAAlgorithm{}
        │   其它 -> &KPAAlgorithm{} （默认 KPA）
        ▼
存入 algorithmCache[strategy]，返回
        │
        ▼
executeScalingPipeline 组装 ScalingRequest → algo.ComputeRecommendation()
```

#### 4.3.3 源码精读

枚举常量定义在 [podautoscaler_types.go:107-119](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/podautoscaler_types.go#L107-L119)，CRD 还用 `+kubebuilder:validation:Enum={HPA,KPA,APA}`（[podautoscaler_types.go:96](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/podautoscaler_types.go#L96)）把它限制成这三个值：

```go
type ScalingStrategyType string
const (
    HPA ScalingStrategyType = "HPA"
    KPA ScalingStrategyType = "KPA"
    APA ScalingStrategyType = "APA"
)
```

工厂本体 [algorithm.go:63-74](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/algorithm.go#L63-L74)，一个 `switch`，未知值兜底成 KPA：

```go
func NewScalingAlgorithm(strategy autoscalingv1alpha1.ScalingStrategyType) ScalingAlgorithm {
    switch strategy {
    case autoscalingv1alpha1.KPA:  return &KPAAlgorithm{}
    case autoscalingv1alpha1.APA:  return &APAAlgorithm{}
    case autoscalingv1alpha1.HPA:  return &HPAAlgorithm{}
    default:                       return &KPAAlgorithm{} // Default to KPA
    }
}
```

> 注意它**不接收任何参数**——因为算法无状态，创建时无需配置。这也意味着工厂返回的对象是「裸」的，所有配置都靠每次调用时传入的 `ScalingRequest`/`ScalingContext`。

消费者侧的缓存复用在 [autoscaler.go:102-124](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/autoscaler.go#L102-L124) 的 `getOrCreateAlgorithm`，用「读锁优先 + 写锁 double-check」的经典模式（上一讲 u3-l1 已分析过此处为何线程安全）：

```go
algo = algorithm.NewScalingAlgorithm(strategy)   // L121：仅在缓存未命中时创建
a.algorithmCache[strategy] = algo
```

缓存字段声明在 [autoscaler.go:74-76](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/autoscaler.go#L74-L76)，是 `map[ScalingStrategyType]ScalingAlgorithm`，最多只会有 3 个键。

算法被调用的真正入口在 [autoscaler.go:299-322](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/autoscaler.go#L299-L322)：`executeScalingPipeline` 在第 3 步采到 `aggregatedMetrics` 后，组装 `ScalingRequest`，然后调 `algo.ComputeRecommendation(ctx, scalingRequest)`。这一段把第 4.1 节的数据契约和第 4.2 节的算法实现缝在了一起。

#### 4.3.4 代码实践：实现一个最小的自定义 ScalingAlgorithm

这是本讲规格要求的实践任务。**只写伪代码，不修改源码**。

1. **实践目标**：理解新增一个策略需要改哪几处，掌握接口与工厂的注册方式。
2. **操作步骤**：

   **第一步**：实现接口。一个新算法至少要实现 `ComputeRecommendation` 和 `GetAlgorithmType` 两个方法。以下为示例代码（非项目原有代码）：

   ```go
   // 示例代码：一个只会「保底 N 个副本」的占位算法
   type FloorAlgorithm struct {
       // 必须是空 struct，满足无状态约束
   }

   var _ ScalingAlgorithm = (*FloorAlgorithm)(nil) // 编译期接口检查

   func (a *FloorAlgorithm) ComputeRecommendation(ctx context.Context, request ScalingRequest) (*ScalingRecommendation, error) {
       // 所有配置从 request.ScalingContext 取，绝不存到结构体
       floor := request.ScalingContext.GetMinReplicas()
       desired := request.CurrentReplicas
       if desired < floor {
           desired = floor
       }
       desired = applyConstraints(desired, request.ScalingContext) // 复用公共边界兜底
       return &ScalingRecommendation{
           DesiredReplicas: desired,
           Confidence:      1.0,
           Reason:          "floor algorithm: keep at least minReplicas",
           Algorithm:       "floor",
           ScaleValid:      true,
           Metadata:        map[string]interface{}{},
       }, nil
   }

   func (a *FloorAlgorithm) GetAlgorithmType() string { return "floor" }
   ```

   **第二步**：注册到工厂。在 [algorithm.go:63-74](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/podautoscaler/algorithm/algorithm.go#L63-L74) 的 `switch` 里加一个 case：

   ```go
   // 示例代码：新增分支
   case autoscalingv1alpha1.FLOOR: // 需要同时在 podautoscaler_types.go 加常量
       return &FloorAlgorithm{}
   ```

   **第三步**：要让用户能在 CR 里写 `scalingStrategy: FLOOR`，还需在 [podautoscaler_types.go:110-119](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/podautoscaler_types.go#L110-L119) 加常量 `FLOOR ScalingStrategyType = "FLOOR"`，并把 [podautoscaler_types.go:96](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/autoscaling/v1alpha1/podautoscaler_types.go#L96) 的 Enum 校验扩成 `{HPA,KPA,APA,FLOOR}`，最后跑 `make manifests` 重新生成 CRD。

3. **需要观察的现象**：以上三处缺一不可——少加常量则 CR 校验不通过；少加 switch 分支则工厂 `default` 兜底成 KPA，你的算法永远不会被选中；忘了 `applyConstraints` 则可能突破 min/max 边界。
4. **预期结果**：照此伪代码你能说清「一个新策略 = 加常量 + 改 Enum + 加 switch case + 实现接口」四步。本实践为源码阅读型，**待本地验证**编译行为。

#### 4.3.5 小练习与答案

**练习 1**：如果用户在 CR 里写了 `scalingStrategy: KPA`，但工厂缓存里已有该 key，`NewScalingAlgorithm` 会被调用吗？

> **答案**：不会。`getOrCreateAlgorithm` 先用读锁查 `algorithmCache`，命中（L105-108）直接返回缓存对象，根本走不到 L121 的 `NewScalingAlgorithm(strategy)`。只有第一次或缓存未命中时才创建。因为算法无状态，复用是安全的。

**练习 2**：工厂的 `default` 分支兜底成 KPA，这个设计是好是坏？有什么风险？

> **答案**：好处是健壮——即使传进来的策略拼写错误或来自旧版本，也能用一个合理默认继续工作而不是 panic。风险是「静默兜底」可能掩盖问题：如果用户本想用 HPA 却写错枚举，系统会悄悄按 KPA 行为伸缩，与预期不符。不过由于 CRD 的 Enum 校验会在 API 层挡住非法值，正常路径下不会触发 default 分支。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「纸上推演」任务（无需运行，纯分析）：

> 场景：一个推理服务当前 **4 个副本**，PodAutoscaler 配置 `scalingStrategy: APA`，目标指标 `gpu_cache_usage_perc` 的 target 为 **50**，`maxScaleUpRate=2`，`upTolerance=0.1`，`maxReplicas=20`。某轮 reconcile 采到的 `StableValue = 85`。

请按顺序回答：

1. **算法选择**：`scalingStrategy: APA` 经 `NewScalingAlgorithm` 会返回哪个对象？是否命中缓存？
2. **公式推演**：代入 APA 公式，`r` 是多少？是否超过扩容容差带？不限速时 `expectedPods` 是多少？限速后是多少？`applyConstraints` 后最终 `DesiredReplicas` 是多少？
3. **对比**：若把策略改成 `KPA`，在 `StableValue=85, PanicValue=85, panicThreshold=2.0, target=50` 且当前不在 panic 模式下，`dspc` 会是多少？你会观察到 APA 与 KPA 在「同样的指标、同样的 target」下给出**不同副本数**的根因是什么？

参考答案（请先自己算再对照）：

1. 返回 `&APAAlgorithm{}`；首轮未命中缓存会创建并存入，之后命中。
2. `r = 85/50 = 1.7 > 1.1` 触发扩容；不限速 `expectedPods = ceil(4 × 1.7) = ceil(6.8) = 7`；限速 `maxScaleUp = ceil(2 × 4) = 8`，`7 < 8` 不限速；`applyConstraints` 后 `min(7, 20) = 7`，最终 **7**。
3. KPA 的 `dspc = ceil(85/50) = ceil(1.7) = 2`（限幅后落在 `[floor(4/2), ceil(2×4)] = [2,8]` 内，仍为 2）。根因：**APA 的期望副本与当前副本数成正比**（`currentPods × r`），适合「每 Pod 利用率」类指标；**KPA 的期望副本直接由 `指标值/target` 算出绝对值**，适合「总量/并发」类指标。同值同 target 下两者语义不同，所以副本数不同。这也提醒你：**选策略前要先搞清楚指标的语义是「每 Pod 利用率」还是「系统总量」**。

---

## 6. 本讲小结

- `ScalingAlgorithm` 接口只有 `ComputeRecommendation` + `GetAlgorithmType` 两个方法，**必须无状态**（空 `struct{}`），所有可变数据通过 `ScalingRequest`/`ScalingContext` 传入。
- 数据契约是单向的：`ScalingRequest`（Target/当前副本/聚合指标/上下文）→ 算法 → `ScalingRecommendation`（期望副本/置信度/理由/标签）。`applyConstraints` 在最后统一用 min/max 兜底。
- **APA**：单窗口、按 `currentPods × (利用率/target)` 等比例伸缩，带上下容差与速率限制，适合每 Pod 利用率型指标；参考了一篇 2023 年的高并发 HPA 论文。
- **KPA**：双窗口（stable/panic），平时用 stable、突发时切 panic 且**只增不减**（高水位 `maxPanicPods`），支持 activation scale 从 0 唤醒，实现最复杂。
- **HPA**：空实现，直接返回当前副本，真正伸缩委托给 Kubernetes 原生 HPA 控制器（在 `hpa_resources.go`）。
- 策略字符串经 `NewScalingAlgorithm` 工厂的 `switch` 映射成算法对象（未知值兜底 KPA），`DefaultAutoScaler` 用「读锁优先 + 写锁 double-check」按策略缓存复用，因算法无状态故复用是安全的。

---

## 7. 下一步学习建议

本讲只讲了「算法怎么算副本」，但还有两块拼图没补：

1. **指标从哪来**：算法吃的 `StableValue/PanicValue` 是 `executeScalingPipeline` 第 1~3 步采集并聚合出来的。下一讲 **u3-l3（指标采集、聚合与监控管线）** 会讲清 `collector → aggregator → AggregatedMetrics` 这条链路，搞懂 stable/panic 窗口是怎么滑动的。
2. **副本怎么落地**：算法算出的 `DesiredReplicas` 最终要写回 Deployment。讲义 **u3-l4（工作负载伸缩与 HPA 资源映射）** 讲 `workload_scale.go` 如何把副本数应用到工作负载、以及 HPA 策略下如何与原生 HPA 资源协作。

读完这两篇，自动伸缩单元（单元 3）就形成完整闭环。之后可以横向跳到单元 4（模型适配与激活）或单元 6（中央缓存与 Pod 发现），看指标与状态的另一面。
