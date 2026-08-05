# 选择算法注册表：Elo/Hybrid 等

## 1. 本讲目标

在 u6-l1 里，我们看完了「决策引擎如何对一条 ROUTE 的 WHEN 规则树求值，并选出命中的决策」。但决策命中之后，路由器还剩最后一个问题：**这条决策列出了多个候选模型，到底挑哪一个？**

本讲就回答这个问题。我们把镜头切到 `pkg/selection`，搞清楚：

- 所有选择算法共同遵守的接口是什么（`Selector`、`SelectionContext`、`SelectionResult`）。
- 算法是如何被注册、查找、并在找不到时安全降级的（`Registry` + 全局 `Select`）。
- Elo 算法如何用 **Bradley-Terry 模型** 给候选模型打分、用偏好反馈持续学习。
- Hybrid 算法如何把多个子算法（Elo + RouterDC + AutoMix）加权融合成一个更稳健的选择。
- 工厂 `Factory` 如何根据配置批量创建并注册算法，以及 `Tier`（supported / experimental）如何标记生产就绪度。

学完后，你应该能读懂任意一种选择算法的实现，并能解释「一次模型选择从输入到输出的完整数据流」。

## 2. 前置知识

本讲默认你已经读过：

- **u5-l2 决策求值管线**：知道请求链路在 `performDecisionEvaluation` 的第 5 步会做「方法解析」，把决策上的 `algorithm.type` 字符串翻译成一个具体的选择算法。
- **u6-l1 决策引擎**：知道决策引擎产出的是「命中的决策名 + 置信度」，而本讲处理的是「命中之后，从候选模型里选一个」。

几个本讲会用到的术语：

- **候选模型（candidate model）**：一条决策的 MODEL 声明里列出的、可以被选中的模型集合，类型是 `[]config.ModelRef`。
- **Bradley-Terry 模型**：一种用「评分」预测「两两对决胜负概率」的经典概率模型，是国际象棋 Elo 等级分的数学基础。
- **偏好反馈（preference feedback）**：用户告诉路由器「这次 A 比 B 好」（或打平），选择器据此更新内部状态。Elo、RouterDC、Hybrid 都会消费它。
- **生产就绪分级（Tier）**：项目把算法分成 `supported`（可上生产、无外部惊喜依赖）和 `experimental`（研究级、可能需要外部服务或预训练模型）两档。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [selector.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/selector.go) | 定义 `Selector` 接口、`SelectionContext`/`SelectionResult`、`SelectionMethod` 枚举、`Registry` 与全局 `Select` 调度。是整个包的「公共契约」。 |
| [elo.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/elo.go) | Elo 选择器实现：Bradley-Terry 打分、成对/单向反馈更新、分类（决策）级评分、可选持久化。 |
| [hybrid.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/hybrid.go) | Hybrid 选择器：把 Elo/RouterDC/AutoMix 三个子算法的分数加权融合，再叠加成本与缓存亲和度调整。 |
| [factory.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/factory.go) | 选择器工厂：根据配置创建单个选择器（`Create`）或批量创建并注册全部选择器（`CreateAll`），并对外暴露 `Initialize`。 |
| [tier_declarations.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/tier_declarations.go) | 集中实现每个算法的 `Tier()` 与 `ExternalDependencies()`，划分 supported / experimental。 |
| [selection_context.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/selection_context.go) / [selection_result.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/selection_result.go) | 输入/输出契约的校验函数。 |

> 说明：本讲引用的行号基于 HEAD `7a77e1e1`。下文永久链接均指向该 commit。

---

## 4. 核心概念与源码讲解

### 4.1 Selector 接口：SelectionContext 与 SelectionResult

#### 4.1.1 概念说明

「选择算法」本质上是一个函数：给我一批候选模型 + 一些上下文，我告诉你选哪个。项目把这件事抽象成一个 Go 接口 `Selector`，所有算法（Elo、Hybrid、静态……）都实现它。这样上层调用方完全不需要知道具体算法是谁，只跟接口打交道——这就是「策略模式」。

围绕这个接口有三个核心数据结构：

- **`SelectionContext`**：选择算法的**输入**。包含用户查询、查询向量、对话历史、命中的决策名（`DecisionName`）、所属配方（`RecipeName`）、候选模型列表，以及成本/质量权重等。
- **`SelectionResult`**：选择算法的**输出**。包含选中的模型名、分数、置信度、使用的方法、人类可读的理由，以及**所有候选的打分**（`AllScores`，用于可解释性）。
- **`Feedback`**：偏好反馈，用于「学习型」算法在事后更新自己（Elo 等）；静态算法会忽略它。

#### 4.1.2 核心流程

一次模型选择的数据流可以画成：

```
请求链路 (u5-l2 第 5 步：方法解析)
        │  得到 SelectionMethod（如 elo / hybrid / static）
        ▼
SelectionContext  ──►  Selector.Select(ctx, selCtx)  ──►  SelectionResult
(查询/候选/决策名/权重)        (具体算法求值)            (选中模型/分数/置信度/理由)
        │
        │  事后（响应阶段或反馈端点）
        ▼
Selector.UpdateFeedback(ctx, feedback)   ──►  更新内部状态（如 Elo 评分）
```

`Selector` 接口除了核心的 `Select`，还要求实现：

- `Method()`：返回自己是哪种算法（用于日志、指标）。
- `UpdateFeedback()`：接收偏好反馈（不学习的算法返回 nil 即可）。
- `Tier()`：声明自己的生产就绪分级。
- `ExternalDependencies()`：声明自己依赖的外部服务或预训练模型（用于启动时健康检查与告警）。

#### 4.1.3 源码精读

`Selector` 接口定义在 [src/semantic-router/pkg/selection/selector.go:269-285](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/selector.go#L269-L285)，这是全包的公共契约：

```go
type Selector interface {
    Select(ctx context.Context, selCtx *SelectionContext) (*SelectionResult, error)
    Method() SelectionMethod
    UpdateFeedback(ctx context.Context, feedback *Feedback) error
    Tier() AlgorithmTier
    ExternalDependencies() []Dependency
}
```

`SelectionContext` 的关键字段在 [selector.go:150-210](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/selector.go#L150-L210)：`Query`、`QueryEmbedding`、`CandidateModels`（候选模型）、`DecisionName`（命中的决策名，Elo 用它做「分类级」评分）、`CostWeight`/`QualityWeight`（成本/质量权重）。

`SelectionResult` 在 [selector.go:230-266](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/selector.go#L230-L266)：注意它除了 `SelectedModel`，还带 `AllScores map[string]float64`——把每个候选的分数都返回，方便面板与日志解释「为什么选了它、其他模型差多少」。

输入输出都有校验。输入校验 `ValidateSelectionContext` 在 [selection_context.go:32-45](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/selection_context.go#L32-L45)，保证「必须有候选模型、每个模型名非空」；输出校验 `ValidateSelectionResult` 在 [selection_result.go:32-49](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/selection_result.go#L32-L49)，强制「选中的模型必须是候选之一」——这避免了算法「幻觉」出一个未声明的模型。

#### 4.1.4 代码实践

**实践目标**：用源码确认「选择算法的输入输出契约与校验边界」。

**操作步骤**：

1. 打开 `selector.go`，找到 `Selector` 接口（约 269 行），数一下它有几个方法。
2. 打开 `selection_result.go`，阅读 `ValidateSelectionResult`，确认它如何判断「选中的模型是否合法」。
3. 思考：如果一个选择器返回的 `SelectedModel` 不在 `CandidateModels` 里，会发生什么？

**需要观察的现象**：校验函数会遍历候选列表，匹配 `Model` 或 `LoRAName`；都匹配不上就返回 `ErrSelectedModelNotCandidate` 错误。

**预期结果**：调用方（全局 `Select`）会把这个错误向上抛，触发请求链路的「选择降级」（见 4.2）。这个契约把「算法输出非法」变成了一个**可观测的失败**，而不是静默选错。

#### 4.1.5 小练习与答案

**练习 1**：`SelectionResult` 里为什么除了 `SelectedModel` 还要保留 `AllScores`？
**参考答案**：为了可解释性。面板、日志和投影追踪可以展示「每个候选得了多少分、差距多大」，让路由决策可审计；同时也便于在测试里断言算法行为。

**练习 2**：静态选择器（`StaticSelector`）的 `UpdateFeedback` 会做什么？
**参考答案**：什么都不做，直接返回 nil（它不学习）。参见 [static.go:160-164](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/static.go#L160-L164)。

---

### 4.2 算法 Registry 与全局 Select 调度

#### 4.2.1 概念说明

有了接口，还需要一个地方「把所有算法集中起来，按名字取用」。这就是 `Registry`——一个线程安全的 `map[SelectionMethod]Selector`。项目还提供了一个**全局默认 Registry**（`GlobalRegistry`）和一个**全局入口函数 `Select`**，让上层不用自己持有 Registry 引用就能做选择。

关键设计是 **`Select` 的三级降级链**：请求的算法没注册 → 退到 static → 再退到「第一个候选」。这保证模型选择**永远不会因为算法缺失而整体失败**，最坏也只是「选了第一个候选」。

#### 4.2.2 核心流程

全局 `Select(ctx, method, selCtx)` 的执行过程：

```
1. ValidateSelectionContext(selCtx)          // 输入不合法直接报错
2. GlobalRegistry.Get(method)                 // 查请求的算法
   ├─ 命中  → 用它
   └─ 未命中 → Get(MethodStatic)              // 第一级降级：退到 static
3. selector.Select(ctx, selCtx)               // 真正求值
4. ValidateSelectionResult(selCtx, result)    // 输出校验
5. result.Tier = selector.Tier()              // 回填生产就绪分级
```

如果到第 2 步连 static 都没有（极端情况），`Select` 会直接构造一个「选第一个候选」的结果返回（[selector.go:372-383](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/selector.go#L372-L383)）。

#### 4.2.3 源码精读

`SelectionMethod` 是一个字符串枚举，定义了全部算法类型，见 [selector.go:38-104](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/selector.go#L38-L104)。每一种算法都关联了一篇参考论文（注释里给了 arXiv 编号），例如：

```go
MethodElo       SelectionMethod = "elo"        // RouteLLM, Bradley-Terry
MethodRouterDC  SelectionMethod = "router_dc"  // 双对比学习
MethodAutoMix   SelectionMethod = "automix"    // POMDP 级联 + 自验证
MethodHybrid    SelectionMethod = "hybrid"     // 多算法加权融合
MethodStatic    SelectionMethod = "static"     // 配置静态分（默认）
```

`Registry` 本身在 [selector.go:330-356](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/selector.go#L330-L356)，用一把 `sync.RWMutex` 保护 map，`Register` 加写锁、`Get` 加读锁。

全局入口 `Select` 的降级逻辑在 [selector.go:362-393](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/selector.go#L362-L393)：

```go
selector, ok := GlobalRegistry.Get(method)
if !ok {
    selector, _ = GlobalRegistry.Get(MethodStatic)   // 降级到 static
}
if selector == nil {
    return &SelectionResult{SelectedModel: selCtx.CandidateModels[0].Model, ...}  // 最后兜底
}
```

> 与请求链路的衔接：u5-l2 第 5 步的「方法解析」会把决策上的 `algorithm.type`（字符串）经 `selectionMethodByAlgorithmType` 映射成 `SelectionMethod`，再交给本讲的全局 `Select`。这张映射表在 [req_filter_classification_runtime.go:17-29](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_runtime.go#L17-L29)，`getSelectionMethod` 的回退逻辑在 [req_filter_classification.go:403-410](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification.go#L403-L410)：映射表里没有的类型一律回退 `MethodStatic`。

#### 4.2.4 代码实践

**实践目标**：验证「全局 Select 的三级降级」与「请求链路的方法映射」是两套独立的回退。

**操作步骤**：

1. 阅读 [selector.go:362-393](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/selector.go#L362-L393) 的 `Select`，确认它的降级顺序是「请求方法 → static → 第一个候选」。
2. 阅读 [req_filter_classification_runtime.go:17-29](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_runtime.go#L17-L29)，列出映射表里**显式支持**的 `algorithm.type`。
3. 对比：映射表里有 `elo` 吗？有 `prompt` 吗？

**需要观察的现象**：请求链路的映射表里**没有 `elo`**（但有 `prompt`）；而全局 Registry（见 4.5 的 `CreateAll`）**注册了 `elo`**。也就是说「Registry 里有的算法」和「请求路径 per-decision 能直接选到的算法」是两个不同的集合。

**预期结果**：若一条决策写 `algorithm.type: elo`，`getSelectionMethod` 在映射表里找不到，会回退到 `static`。这说明 Registry 是「能力全集」，而映射表是「per-decision 对外暴露的子集」。两者集合不同是有意为之还是遗漏，**待本地验证**（可在测试里构造 `algorithm.type: elo` 的决策并观察实际命中的 selector 日志）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Select` 要设计三级降级，而不是「找不到算法就报错」？
**参考答案**：模型选择位于请求关键路径，宁可「选得不够好」（选第一个候选），也不能让整个请求失败。降级链把算法缺失变成可观测的「次优选择」而非硬故障。

**练习 2**：`Registry` 用 `sync.RWMutex` 而不是 `sync.Mutex`，有什么好处？
**参考答案**：选择是读多写少的场景（请求时大量 `Get`，只有启动时 `Register`）。`RWMutex` 允许多个 `Get` 并发读，提升吞吐。

---

### 4.3 Elo 实现：Bradley-Terry 打分与反馈学习

#### 4.3.1 概念说明

Elo 是本包最重要的「学习型」算法，思路来自 RouteLLM 论文（arXiv:2406.18665）。它借用了国际象棋的 **Elo 等级分**：每个模型有一个评分（rating），用户反馈「A 比 B 好」就调整两者的评分。选择时，用 **Bradley-Terry 模型** 把评分换算成「胜出概率」，概率最高的候选胜出。

它的核心价值是**在线学习**：路由器跑得越久、收到的偏好反馈越多，评分就越准，选择质量就越高——而且这一切对上层透明（上层只管调 `Select`）。

Elo 还支持**分类级（per-decision）评分**：同一个模型在不同决策（如「数学题」vs「闲聊」）下可以有不同评分，更精细。

#### 4.3.2 核心流程

**选择阶段**（`Select`）用 Bradley-Terry 把评分转成概率：

对每个候选模型 \(i\)，其评分 \(R_i\)，先计算「实力项」 \(Q_i = 10^{R_i/400}\)，再归一化得到胜出概率：

\[
P_i = \frac{Q_i}{\sum_j Q_j} = \frac{10^{R_i/400}}{\sum_j 10^{R_j/400}}
\]

这正是 softmax（以 \(10^{R/400}\) 为底）。概率 \(P_i\) 同时就是该模型的「选择分数」，分数最高者胜。注意：评分越高概率越高，所以 Elo 选择等价于「选评分最高的候选」。

**学习阶段**（`UpdateFeedback`）用标准 Elo 更新公式调整评分。对于一次 A 胜 B 的成对反馈：

期望得分（A 视角）：

\[
E_A = \frac{1}{1 + 10^{(R_B - R_A)/400}}
\]

实际得分 \(S_A\)：胜=1、负=0、平=0.5。更新：

\[
R_A' = R_A + K \cdot (S_A - E_A), \qquad R_B' = R_B + K \cdot (S_B - E_B)
\]

其中 \(K\) 是 K 因子（项目默认 32），\(K\) 越大评分变化越快、越 volatile。胜者若本就被看好（\(E_A\) 高），\(S_A - E_A\) 小，涨分少；爆冷则大涨——这就是 Elo 的「自我校正」。

Elo 还支持**单向反馈**（只知道一个模型好坏、没有对手）：正反馈给该模型加 \(0.1K\)，负反馈扣 \(0.1K\)（见 [elo.go:551-575](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/elo.go#L551-L575)）。

**置信度**由评分的「样本量稳定性」决定：比较次数越多越自信，用一个 sigmoid 把比较次数映射到 \((0,1)\)（见 4.3.3）。

#### 4.3.3 源码精读

常量与默认配置在 [elo.go:30-83](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/elo.go#L30-L83)：默认初始评分 `DefaultEloRating = 1500`，K 因子 `EloKFactor = 32`。

**选择打分**——Bradley-Terry 概率计算在 [elo.go:315-332](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/elo.go#L315-L332)：

```go
totalRating := 0.0
for _, r := range ratings {
    totalRating += math.Pow(10, r.Rating/400.0)   // 实力项 Q_i
}
for _, r := range ratings {
    prob := math.Pow(10, r.Rating/400.0) / totalRating  // P_i
    allScores[r.Model] = prob
}
```

**候选评分查询**遵循「分类级优先、全局兜底」的顺序，在 [elo.go:620-648](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/elo.go#L620-L648)：先查 `categoryRatings[decisionName][model]`，没有就查全局 `globalRatings[model]`，再没有就用默认初始评分。

**成对反馈更新**——期望得分与评分更新在 [elo.go:577-617](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/elo.go#L577-L617)：

```go
expectedWinner := 1.0 / (1.0 + math.Pow(10, (loserRating.Rating-winnerRating.Rating)/400.0))
expectedLoser  := 1.0 - expectedWinner
actualWinner, actualLoser := 1.0, 0.0
if feedback.Tie { actualWinner = 0.5; actualLoser = 0.5 }
winnerRating.Rating += e.config.KFactor * (actualWinner - expectedWinner)
loserRating.Rating  += e.config.KFactor * (actualLoser  - expectedLoser)
```

**置信度**——基于比较次数的 sigmoid，在 [elo.go:723-735](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/elo.go#L723-L735)：

\[
\text{confidence} = \frac{1}{1 + e^{-k \cdot (\text{comparisons} - \text{threshold})}}, \quad k=0.2
\]

比较次数达到 `MinComparisons`（默认 5）附近时，置信度从 ~0.5 快速爬升到接近 1。

此外，`InitializeFromConfig` 会把配置里的静态分（0~1）线性映射到 Elo 区间（1000~2000）作为初始评分，见 [elo.go:278-294](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/elo.go#L278-L294)：`rating = 1000 + score*1000`。这让 Elo 能「站在静态分的肩膀上」起步，而不是从一无所知开始。

#### 4.3.4 代码实践

**实践目标**：手工推演一次 Elo 选择与一次反馈更新，验证 Bradley-Terry 打分与「爆冷涨分多」的性质；并对照测试理解反馈如何塑造排行榜。

**操作步骤（手算）**：

1. **初始选择**：三个候选 A、B、C 全是默认评分 1500，\(Q_A=Q_B=Q_C=10^{1500/400}\)。求各自的 \(P\)。
   - 因为三者评分相同，\(P_A=P_B=P_C=\frac{1}{3}\)。三者分数相等，`Select` 里 `score > bestScore` 对后两者为 false，所以**第一个候选 A 胜出**。
2. **一次反馈**：用户反馈「C 胜 A」。两者都 1500，\(E_C = 1/(1+10^0)=0.5\)，\(S_C=1\)，\(K=32\)。
   - \(R_C' = 1500 + 32(1-0.5) = 1516\)
   - \(R_A' = 1500 + 32(0-0.5) = 1484\)
3. **再次选择**：现在 \(Q_C = 10^{1516/400}\) 高于 \(Q_A = 10^{1484/400}\)，\(P_C\) 变大，**C 胜出**。

**需要观察的现象**：评分相同时按候选声明顺序取第一个；一次反馈后评分分化，胜者 C 反超 A。

**预期结果**：手算结论应与 `Select` 的「取最高分、同分取先」语义一致。

**可选运行验证**（**待本地验证**）：项目里有 Elo 反馈模拟测试辅助函数 `simulateEloRankingFeedback`/`assertEloRankingEvolution`（见 [selector_elo_test.go:8-66](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/selector_elo_test.go#L8-L66)），它给 strong/medium/weak 三个模型喂 10 轮成对反馈，断言排行榜为 strong > medium > weak，且 strong 有 20 胜、weak 有 20 负。可在 `src/semantic-router/` 下运行：

```bash
go test ./pkg/selection/ -run Elo -v
```

观察测试是否通过、日志里评分如何演化。

#### 4.3.5 小练习与答案

**练习 1**：为什么「爆冷」（弱胜强）时胜者涨分比「意料之中」（强胜弱）多？
**参考答案**：因为 \(S_A - E_A\) 在爆冷时更大。强胜弱时 \(E_A\) 接近 1，\(1-E_A\) 很小；弱胜强时 \(E_A\) 接近 0，\(1-E_A\) 接近 1，乘以 \(K\) 后涨分大。这正是 Elo 自我校正的核心。

**练习 2**：Elo 的「分类级评分」如何与「全局评分」协作？
**参考答案**：查询时优先用当前决策名对应的分类评分；若该分类下没有该模型的评分，退回全局评分；都没有则用默认初始评分 1500。这让模型在擅长领域有专门评分，同时新模型不会因「无评分」而无法选择。

**练习 3**：把置信度设计成「比较次数的 sigmoid」而不是固定值，意图是什么？
**参考答案**：区分「评分可信」与「评分不可信」。一个只比过 1 次的模型即便评分高，置信度也低（~0.5）；比过几十次的模型置信度接近 1。下游可以用置信度决定是否触发人工复核或降级。

---

### 4.4 Hybrid 组合算法

#### 4.4.1 概念说明

每种算法都有盲区：Elo 靠历史反馈，冷启动时谁都没评分；RouterDC 靠查询与模型描述的向量相似度，擅长语义匹配但不懂历史表现；AutoMix 靠级联自验证，质量高但依赖外部验证服务。

**Hybrid**（来自 Hybrid LLM 论文 arXiv:2404.14618）的思路是「不押注单一算法，把几个加权融合」：让 Elo、RouterDC、AutoMix 各自打分，再按可配置权重加权求和。这样任何一个子算法的短板都能被其他算法弥补，整体更稳健。

Hybrid 本身就是 `Selector`，但它的 `Select` 内部会调用三个**子选择器**的 `Select`，收集它们的 `AllScores` 后融合。

#### 4.4.2 核心流程

Hybrid 的 `Select` 流程（[hybrid.go:170-266](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/hybrid.go#L170-L266)）：

```
1. ValidateSelectionContext
2. 依次调用三个子选择器（权重>0 才调）：
   elo.Select      → experience 分数集
   routerDC.Select → router_dc 分数集
   autoMix.Select  → automix 分数集
3. （可选）对每个子算法的分数做 min-max 归一化到 [0,1]
4. combineScores：按权重加权求和（权重再归一化）
5. （可选）applyCostAdjustment：便宜的模型加分
6. applyCacheAffinity：缓存亲和度调整
7. selectBestModel：取最高分
8. calculateConfidence：用「子算法一致性」估置信度
9. recordComponentAgreement：记录一致性指标
```

融合公式（权重归一化后）：

\[
\text{score}(m) = \sum_{c \in \{\text{elo, dc, automix}\}} \frac{w_c}{W} \cdot \text{score}_c(m), \quad W = \sum_c w_c
\]

其中 \(w_c\) 只对「真的产出了分数」的子算法累加——若某个子算法报错或没数据，它的权重会被剔除，剩余权重重新归一化（[hybrid.go:385-407](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/hybrid.go#L385-L407)）。这让 Hybrid 对子算法失败有韧性。

**置信度**来自两个信号的平均：子算法间「一致性比例」（几个子算法选了同一个模型）+ 子算法置信度的均值（[hybrid.go:481-505](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/hybrid.go#L481-L505)）。子算法都选同一个模型 → 高置信；分歧大 → 低置信。

#### 4.4.3 源码精读

`HybridConfig` 与默认权重在 [hybrid.go:33-67](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/hybrid.go#L33-L67)：默认 `ExperienceWeight=0.3`、`RouterDCWeight=0.3`、`AutoMixWeight=0.2`、`CostWeight=0.2`。

`HybridSelector` 结构体持有三个子选择器与模型成本表，见 [hybrid.go:72-86](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/hybrid.go#L72-L86)。

加权融合的核心 `combineScores` 在 [hybrid.go:370-410](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/hybrid.go#L370-L410)，关键片段：

```go
for component, scores := range componentScores {
    weight := weights[component]
    for model, score := range scores {
        result[model] += (weight / totalWeight) * score   // 权重归一化后加权
    }
}
```

成本调整 `applyCostAdjustment` 在 [hybrid.go:448-478](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/hybrid.go#L448-L478)：把成本归一化到 \([0,1]\)（0=最便宜），给便宜模型一个 `score *= (1 + costBonus)` 的加成。

反馈传播 `UpdateFeedback` 在 [hybrid.go:315-353](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/hybrid.go#L315-L353)：把同一条反馈转发给三个子选择器，任一失败都收集错误，最终用 `errors.Join` 合并返回——不会因为一个子算法失败而丢失其他子算法的学习。

#### 4.4.4 代码实践

**实践目标**：理解 Hybrid 的「加权融合 + 容错」与「一致性置信度」。

**操作步骤**：

1. 阅读 [hybrid.go:170-266](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/hybrid.go#L170-L266) 的 `Select`，确认三个子算法是「权重>0 才调用」。
2. 阅读 `combineScores`（[hybrid.go:370-410](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/hybrid.go#L370-L410)）：假设 experience 和 router_dc 都产出了分数，但 automix 报错（没产出）。回答：automix 的权重 0.2 会被计入 `totalWeight` 吗？experience+router_dc 的权重会如何重新分配？

**需要观察的现象**：`totalWeight` 只对「`len(scores) > 0`」的子算法累加权重（[hybrid.go:386-391](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/hybrid.go#L386-L391)）。

**预期结果**：automix 失败时其权重不进分母，experience(0.3) 与 router_dc(0.3) 各占 50%，融合照常进行——这就是 Hybrid 对子算法失败的容错。

#### 4.4.5 小练习与答案

**练习 1**：Hybrid 的置信度为什么用「子算法一致性」来估？
**参考答案**：因为 Hybrid 没有自己独立的「正确性」信号。多个独立算法（Elo 看历史、RouterDC 看语义、AutoMix 看验证）都选同一个模型，说明这个选择从不同角度都站得住，可信度高；反之分歧大说明证据冲突，应低置信。

**练习 2**：Hybrid 为什么在融合前要对每个子算法的分数做 min-max 归一化？
**参考答案**：不同子算法的分数尺度不同（Elo 给的是概率，RouterDC 给的是相似度），直接相加会让尺度大的子算法主导结果。归一化到 \([0,1]\) 后，权重才能真正决定各算法的话语权。

---

### 4.5 算法工厂 Factory 与生产就绪分级

#### 4.5.1 概念说明

现在我们有了十几种算法，谁来「根据配置把它们造出来、装进 Registry」？这就是 **`Factory`**。它做两件事：

- `Create()`：根据配置的 `method` **只造一个**选择器（用于「我就要用这个算法」的场景）。
- `CreateAll()`：**把所有算法都造出来并注册**进一个新 Registry（用于启动时一次性装配全局 Registry）。

工厂还引入了 **Tier（生产就绪分级）** 机制：每个算法声明自己是 `supported` 还是 `experimental`，以及依赖哪些外部服务/预训练模型。启动时 `WarnExperimentalAlgorithms` 会给实验性算法打醒目告警，`CheckDependencyHealth` 会探测外部服务是否可达——但都**只记录不阻断**，避免拖垮启动。

#### 4.5.2 核心流程

启动期装配全局 Registry 的流程（由 `Initialize` 触发，[factory.go:434-446](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/factory.go#L434-L446)）：

```
Initialize(cfg, modelConfig, categories, embeddingFunc)
   └─ NewFactory(cfg).WithModelConfig(...).WithCategories(...).WithEmbeddingFunc(...)
      └─ factory.CreateAll()
         ├─ 初始化指标
         ├─ NewRegistry()
         ├─ 逐个创建并 Register：static, elo, router_dc, automix, hybrid, knn, kmeans, svm, mlp, rl_driven, gmtrouter, latency_aware, multi_factor
         ├─ LogRegisteredAlgorithms（打印每个算法的 tier 与依赖）
         └─ 返回 Registry → 赋给 GlobalRegistry
```

`Create()` 的单选逻辑则是一个 `switch method`，default 分支回退到 static（[factory.go:116-200](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/factory.go#L116-L200)）。

#### 4.5.3 源码精读

`ModelSelectionConfig` 把「方法名 + 每种方法的专属配置」打包，见 [factory.go:32-62](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/factory.go#L32-L62)，`Method` 默认是 `static`（[factory.go:65-69](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/factory.go#L65-L69)）。

`Factory` 用**链式 With 方法**收集依赖（模型配置、分类、嵌入函数、lookup table），见 [factory.go:72-113](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/factory.go#L72-L113)。

`CreateAll` 注册全部算法的核心段落是 [factory.go:203-348](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/factory.go#L203-L348)。注意几个细节：

- **static 永远注册**（[factory.go:210-214](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/factory.go#L210-L214)），保证降级链有底。
- **Hybrid 复用已创建的子选择器实例**（`NewHybridSelectorWithComponents`，[factory.go:260](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/factory.go#L260)），这样 Hybrid 的 elo 子算法和单独注册的 elo 是**同一个对象**——给 elo 喂反馈，Hybrid 里的 elo 也同步更新。
- **ML 系算法（knn/kmeans/svm/mlp）创建失败只告警不致命**（[factory.go:276-305](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/factory.go#L276-L305)），因为它们依赖预训练模型，缺失时跳过注册即可。

**Tier 分级**集中在 [tier_declarations.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/tier_declarations.go)：

| Tier | 算法 | 说明 |
| --- | --- | --- |
| supported | static, elo, router_dc, latency_aware, multi_factor, hybrid | 无外部惊喜依赖，可上生产 |
| experimental | automix | 需 AutoMix 验证服务（自验证） |
| experimental | rl_driven | 需 Router-R1 服务 |
| experimental | gmtrouter | 需预训练图模型权重 |
| experimental | knn/kmeans/svm/mlp（MLSelectorAdapter） | 需预训练 ML 模型 |

`WarnExperimentalAlgorithms` 与 `CheckDependencyHealth` 在 [factory.go:372-431](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/factory.go#L372-L431)：前者对配置里实际用到的实验性算法打 WARN，后者对 `DependencyExternalService` 类依赖发 HTTP 健康探测（5 秒超时），探测失败只记日志、不阻断启动。

#### 4.5.4 代码实践（本讲核心实践）

**实践目标**：列出 `CreateAll` 注册的全部选择方法，并对照 Tier 表分类；再选 Elo 算法，用自己的话解释它如何用 Bradley-Terry 给候选打分并选出胜者。

**操作步骤**：

1. 打开 [factory.go:203-348](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/factory.go#L203-L348)，逐行找出所有 `registry.Register(...)` 调用，记下它们注册的 `SelectionMethod`。
2. 打开 [tier_declarations.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/tier_declarations.go)，把第 1 步列出的方法分成 supported / experimental 两类。
3. 针对 Elo：用自己的话写一段「Elo 如何用 Bradley-Terry 打分并选出胜者」，要求包含 4.3 的概率公式与「同分取先」的边界行为。

**需要观察的现象 / 预期结果**：

第 1 步应列出 **13 个**方法（按注册顺序）：

| # | 方法 | 常量 |
| --- | --- | --- |
| 1 | static | `MethodStatic` |
| 2 | elo | `MethodElo` |
| 3 | router_dc | `MethodRouterDC` |
| 4 | automix | `MethodAutoMix` |
| 5 | hybrid | `MethodHybrid` |
| 6 | knn | `MethodKNN` |
| 7 | kmeans | `MethodKMeans` |
| 8 | svm | `MethodSVM` |
| 9 | mlp | `MethodMLP` |
| 10 | rl_driven | `MethodRLDriven` |
| 11 | gmtrouter | `MethodGMTRouter` |
| 12 | latency_aware | `MethodLatencyAware` |
| 13 | multi_factor | `MethodMultiFactor` |

第 2 步的 Tier 分类见 4.5.3 的表格。注意：`prompt` 与 `session_aware` 两种方法虽然在 `SelectionMethod` 枚举里有定义（[selector.go:96-104](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/selector.go#L96-L104)），但**不在 `CreateAll` 注册列表里**——它们由别处装配（`session_aware` 是包装器，`prompt` 走运行时结构化输出契约），这是 `SelectionMethod` 枚举（能力全集）与 `CreateAll`（默认装配集）的又一处差异，**待本地验证**其装配位置。

第 3 步的 Elo 解释应包含：每个候选算 \(Q_i=10^{R_i/400}\)，概率 \(P_i=Q_i/\sum Q_j\) 作为分数，取最高分胜出；评分相同时因 `score > bestScore` 严格大于不成立，**声明顺序靠前的候选胜出**；评分由偏好反馈按 \(R'=R+K(S-E)\) 在线更新。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `CreateAll` 里 Hybrid 要复用已创建的 elo/router_dc/automix 实例，而不是 `NewHybridSelector` 新建一组？
**参考答案**：为了让「单独注册的 elo」和「Hybrid 内部的 elo」共享同一份评分状态。这样无论请求走 elo 还是走 hybrid，给 elo 喂的反馈都会同步生效，避免状态分裂。

**练习 2**：`CheckDependencyHealth` 探测外部服务失败时，会阻止路由器启动吗？
**参考答案**：不会。它只记录 WARN/UNREACHABLE 日志，函数不返回错误、不阻断启动（[factory.go:396-431](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/factory.go#L396-L431)）。依赖缺失的影响是「运行时降级」，由各算法自己处理（如 AutoMix 找不到验证服务就无法做自验证升级）。

**练习 3**：ML 系算法（knn 等）创建失败时为什么只告警而不让 `CreateAll` 失败？
**参考答案**：因为它们依赖磁盘上的预训练模型，开发/测试环境通常没有。若让创建失败连锁导致 `CreateAll` 报错，会拖垮整个路由器启动。跳过注册只是让这些方法在 Registry 里「不可用」，请求路径会经降级链退到 static。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「**从配置到选择**」的端到端追踪任务：

1. **接口视角**：写一段话描述 `SelectionContext`（输入）如何变成 `SelectionResult`（输出），指出 `AllScores` 和 `Tier` 分别由谁填充。
2. **调度视角**：假设一条决策配置了 `algorithm.type: hybrid` 但 `GlobalRegistry` 里没有 hybrid（假设 `CreateAll` 没装配它），追踪 `Select` 会走怎样的降级路径，最终选中的模型是谁、`Method` 字段是什么。
3. **算法视角**：给定两个候选模型，初始评分分别为 1500 和 1600，**手算**各自的 Bradley-Terry 选择概率，判断谁胜出；再给定一次「1500 的模型胜过 1600 的模型」的反馈（爆冷），手算两者更新后的评分。
4. **工程视角**：打开 `factory.go` 的 `CreateAll`，确认 hybrid 复用了哪些子选择器实例，并解释这对「反馈学习」的意义。

**预期产出**：一段能把「输入→调度降级→算法打分→反馈学习→工程装配」讲清楚的解释。其中第 2 题的降级路径应为：hybrid 未注册 → 退到 static → static 选最高静态分（无配置分则取第一个候选）；第 3 题概率应反映 1600 的模型概率更高、但爆冷后两者评分差距缩小。

> 手算部分可对照 4.3 的公式；运行部分（如想用真实代码验证）**待本地验证**：可在 `src/semantic-router/` 下写一个最小 Go 测试，构造两个 `ModelRef` 候选、一个 `EloSelector`，调用 `Select` 与 `UpdateFeedback`，打印评分与概率演化。

## 6. 本讲小结

- `Selector` 是所有选择算法的公共接口，输入 `SelectionContext`、输出 `SelectionResult`；输出必须通过「选中模型属于候选」的校验，把算法幻觉变成可观测失败。
- `Registry` 是线程安全的算法注册表；全局 `Select` 有「请求方法 → static → 第一个候选」三级降级链，保证模型选择永不整体失败。
- 请求链路的 `selectionMethodByAlgorithmType` 是 per-decision 的算法暴露子集，与 Registry 的能力全集**不完全相同**（如 Registry 有 elo、映射表无 elo），未命中会回退 static。
- Elo 用 Bradley-Terry 模型把评分转成选择概率 \(P_i=10^{R_i/400}/\sum 10^{R_j/400}\)，取最高分胜；偏好反馈按 \(R'=R+K(S-E)\) 在线更新，爆冷涨分多、意料之中涨分少；支持分类级评分与持久化。
- Hybrid 把 Elo/RouterDC/AutoMix 三个子算法加权融合，权重归一化且对子算法失败容错，置信度来自子算法一致性。
- `Factory.CreateAll` 注册 13 种算法，按 supported/experimental 分级；实验性算法的外部依赖在启动时只探测告警、不阻断。

## 7. 下一步学习建议

- **继续向下看算法实现**：本讲只精读了 Elo 与 Hybrid。建议接着读 `router_dc.go`（双对比学习的查询-模型相似度）和 `automix.go`（POMDP 级联 + 自验证），对照 `pomdp_solver.go` 理解 AutoMix 的信念状态更新。
- **看模型运行时与定价**：选择算法需要「候选模型清单」和「成本数据」作为输入。下一步可读 u6-l3（modelruntime/modelinventory/modelpricing），看 `ModelParams.Pricing` 如何被 Elo/Hybrid 的 `applyCostAdjustment` 消费。
- **看反馈如何回流**：Elo 的 `UpdateFeedback` 在生产中由谁调用？建议追踪 `pkg/selection` 的反馈入口与响应阶段的 `RouterOutcome` 学习运行时（u4-l2 提到的 `LearningRuntime`），把「选择 → 反馈 → 再选择」的闭环补全。
- **看 ML 系与实验性算法**：若对研究前沿感兴趣，可读 `ml_adapter.go`（knn/kmeans/svm/mlp 的统一适配器）和 `rl_driven.go`（Router-R1 奖励结构），理解它们的预训练模型依赖与 Tier 划分依据。
