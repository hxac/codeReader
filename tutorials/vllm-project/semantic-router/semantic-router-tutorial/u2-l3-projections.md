# Projections：分区/评分/映射

## 1. 本讲目标

在上一讲（u2-l2）里，我们学会了 SR（vLLM Semantic Router）如何从一次请求中抽取 **16 个信号族** 的原始"事实"——它们各自带置信度与原始值，只负责"发现问题"，不做路由决定。

但原始信号常常是**互相竞争、彼此冗余**的：比如请求同时命中了 `domain("health")` 和 `domain("law")` 两个域；又比如一次请求既"长"又"含步骤词"又"含推理词"，到底算简单还是复杂？决策层（ROUTE 的 WHEN 规则）无法直接消化这么多零散证据。

**投影层（Projection）就是用来协调这些证据的"中间层"**。它把一堆原始信号，用三类数学方法，压缩成少数几个**命名路由带（named routing band）**——比如 `balance_simple` / `balance_medium` / `balance_complex` / `balance_reasoning`——供决策层干净地消费。

学完本讲，你应当能够：

1. 说清楚 **partition（分区）** 的 `softmax_exclusive` 语义：多个互斥候选里选出一个赢家、用 softmax 重写置信度，没人命中时用 `default` 兜底。
2. 说清楚 **score（评分）** 的 `weighted_sum` 方法：把多个信号按权重（可正可负）加权求和成一个连续分数。
3. 说清楚 **mapping（映射）** 的 `threshold_bands` 分带与 `sigmoid_distance` 校准：把分数切成几段命名带，并按"离边界多远"给一个 0~1 的置信度。
4. 读懂 **投影追踪（projection trace）** 这一版本化 JSON，看懂一次投影"为什么得出这个结论"。

---

## 2. 前置知识

阅读本讲前，你应当已经了解（来自 u2-l1、u2-l2）：

- **信号（Signal）**：从请求里抽取的一类事实，每条信号带两个数值通道——`SignalConfidences`（置信度）与 `SignalValues`（原始值），键统一为 `"族:名"`，如 `"embedding:fast_qa_en"`、`"domain:health"`。
- **规则型 vs 学习型信号**：规则型（keyword/context/structure）命中置信度恒为 1.0；学习型（embedding/domain/complexity）写入真实概率或相似度。
- **SignalResults 容器**：所有信号求值结果的聚合处，里面既有 `Matched*Rules`（命中名单），也有 `SignalConfidences`（置信度表）。

本讲会用到三个中学数学概念，先一句话铺垫：

- **softmax**：把一组任意大小的数"挤压"成一组加起来等于 1 的概率，最大的那个会分到最大的份额。
- **加权和（weighted sum）**：\(\sum w_i \cdot v_i\)，权重为正表示"推高总分"，为负表示"压低总分"。
- **sigmoid**：\(1/(1+e^{-x})\)，把任意实数压到 0~1，常用来把"距离"翻译成"置信度"。

> 一个贯穿全讲的关键直觉：投影层不做"是 / 否"的硬决定，它的产出是**带名字 + 带置信度**的"软标签"。决策层（下一讲 u2-l4）才在这些软标签上写 WHEN 规则做硬选择。

---

## 3. 本讲源码地图

本讲涉及的关键文件分两组：**配置侧（DSL）**声明"投影是什么"，**代码侧（Go）**执行"投影怎么算"。

| 文件 | 角色 |
| --- | --- |
| `config/recipes/balance/recipe.dsl` | balance 配方的投影声明：2 个 partition、5 个 score、5 个 mapping。是本讲最主要的阅读与实操素材。 |
| `src/semantic-router/pkg/config/projection_config.go` | 投影的 Go 配置结构体（`Projections`/`ProjectionPartition`/`ProjectionScore`/`ProjectionMapping`）。 |
| `src/semantic-router/pkg/classification/classifier_signal_context.go` | 信号求值主流程，在其中按固定顺序调用 partition → score → mapping（L215–L218）。 |
| `src/semantic-router/pkg/classification/classifier_signal_groups.go` | partition 的入口：对 domain、embedding 两类信号施加分区。 |
| `src/semantic-router/pkg/classification/classifier_signal_group_resolution.go` | partition 的核心算法：选赢家、softmax、default 兜底。 |
| `src/semantic-router/pkg/classification/classifier_projections.go` | score + mapping 的执行入口 `applyProjections`。 |
| `src/semantic-router/pkg/classification/classifier_projection_inputs.go` | score 的加权求和与输入取值（binary/confidence/raw）。 |
| `src/semantic-router/pkg/classification/classifier_projection_outputs.go` | mapping 的分带匹配与 sigmoid 校准。 |
| `src/semantic-router/pkg/classification/classifier_projection_order.go` | score 之间的拓扑排序（当 score 依赖另一个 score 的输出时）。 |
| `src/semantic-router/pkg/classification/classifier_projection_trace.go` | 生成 score/mapping 的可解释追踪 JSON。 |
| `src/semantic-router/pkg/classification/classifier_signal_group_trace.go` | 生成 partition 的可解释追踪 JSON。 |
| `src/semantic-router/pkg/projectiontrace/trace.go` | 追踪 JSON 的版本化 schema 定义（被分类、重放、面板共用）。 |

---

## 4. 核心概念与源码讲解

### 4.1 投影层在请求流水线中的位置

#### 4.1.1 概念说明

回忆 u2-l1 的四层流水线：**信号 → 投影 → 决策 → 模型+插件**。投影层夹在中间，输入是杂乱的原始信号，输出是少量干净的命名带。

投影内部又分三类**正交**的操作，它们解决的问题完全不同：

- **partition（分区）**：解决"多个同类信号**互相竞争**，该留谁"。比如 14 个 domain 候选里只能有一个胜出。
- **score（评分）**：解决"**多个不同类**信号各执一词，综合下来算几分"。比如把长度、结构、关键词、嵌入相似度揉成一个 0~1 的"难度分"。
- **mapping（映射）**：解决"这个连续分数**落到哪一档**"。比如难度分 0.6 落到 `balance_complex` 这条带。

三类投影不是平行的，而是**串行接力**：partition 先"瘦身"竞争信号，score 再把（瘦身后的）信号汇总成分数，mapping 最后把分数切带。

#### 4.1.2 核心流程

这个串行顺序在代码里是硬编码的固定调用链：

```go
results = c.applySignalGroups(results)      // partition（分区）
results = c.applySignalComposers(results)
results = c.applySignalOutputPolicies(results)
results = c.applyProjections(results)       // score（评分）→ mapping（映射）
```

#### 4.1.3 源码精读

固定调用顺序写死在信号求值主流程的末尾：

[逐次施加 partition、composer、policy、projection（含 score 与 mapping）](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_context.go#L215-L218)

```go
results = c.applySignalGroups(results)    // L215：partition 分区
results = c.applySignalComposers(results) // L216：信号组合器（本讲不讲）
results = c.applySignalOutputPolicies(results) // L217：输出策略（本讲不讲）
results = c.applyProjections(results)     // L218：score 评分 + mapping 映射
```

> 注意 partition 在 score **之前**执行。这意味着 score 里引用的 domain/embedding 信号，已经是 partition 选出的唯一赢家了——这是 partition "协调竞争"价值的体现。

---

### 4.2 partition 分区：softmax_exclusive 与 default 兜底

#### 4.2.1 概念说明

partition 解决的核心痛点是**互斥竞争**。以 balance 配方的域分类为例：SR 维护了 14 个 domain 信号（computer science、math、physics、health、law……）。一段文本经过嵌入相似度计算后，可能同时让 `health` 和 `biology` 两个域的置信度都很高。如果决策层直接面对两个同时命中的 domain，规则会变得很啰嗦。

partition 的做法是：**把 14 个 domain 声明为一个互斥分区，只保留置信度最高的一个，其余全部丢弃**。这样下游永远只看到唯一一个"赢的域"。

它有两个关键语义：

1. **softmax_exclusive**：不是简单取 max，而是用 softmax（带温度）把所有候选的置信度变成一组概率，赢家得到它对应的概率作为**新置信度**。这等于顺带告诉下游"赢得有多彻底"——两个域旗鼓相当时，赢家置信度接近 0.5；一边倒时接近 1.0。
2. **default 兜底**：如果一个候选都没命中（比如用户输入了一串乱码，没有任何 domain 相似度达标），就**合成**一个默认成员（balance 里 domain 的 default 是 `other`，intent 的 default 是 `general_chat_fallback`），保证分类永不落空。

#### 4.2.2 核心流程

partition 对 **domain** 和 **embedding** 两类信号施加（其他信号族不走 partition），伪代码如下：

```
对每个 partition 分组 group：
    members = group.Members 中真正"存在"的候选（domain 看 Categories，embedding 看 EmbeddingRules）
    contenders = members 中"确实命中"且有置信度的候选
    if len(contenders) == 0:
        合成 group.Default（若配置了）作为命中   # default 兜底
    elif len(contenders) == 1:
        不做处理（唯一命中，无需竞争）           # 早返回优化
    else:
        winner = 原始置信度最高的候选
        if semantics == "softmax_exclusive":
            winnerScore = softmax(confidences, temperature)[winner]   # 重写为概率
        else:
            winnerScore = winner 的原始置信度
        从命中名单与置信度表里删除所有非赢家       # 互斥：只留赢家
        把赢家的置信度写成 winnerScore
```

softmax 的数学定义（带温度 \(T\)、用最大值减法做数值稳定）：

\[
p_i = \frac{\exp\big((s_i - s_{\max}) / T\big)}{\sum_j \exp\big((s_j - s_{\max}) / T\big)}
\]

温度 \(T\) 控制"锐化"程度：\(T\) 越小，分布越尖锐（赢家通吃）；\(T\) 越大，越趋近均匀。balance 里 domain 用 \(T=0.1\)（很锐），intent 用 \(T=0.18\)（稍缓）。

#### 4.2.3 源码精读

partition 的声明（DSL），balance 有两个分区，一个管域、一个管意图嵌入：

[balance 的两个 softmax_exclusive 分区，分别带 temperature 与 default 兜底](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L363-L375)

```dsl
PROJECTION partition balance_domain_partition {
  semantics: "softmax_exclusive"
  temperature: 0.1
  members: ["biology", "business", "chemistry", "computer science", ... ]  # 14 个域
  default: "other"
}
PROJECTION partition balance_intent_partition {
  semantics: "softmax_exclusive"
  temperature: 0.18
  members: ["agentic_workflows", "architecture_design", ... ]              # 16 个意图嵌入
  default: "general_chat_fallback"
}
```

对应的 Go 配置结构体，`Members` 即候选名单、`Default` 即兜底成员：

[ProjectionPartition 结构体：Name/Semantics/Temperature/Members/Default](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/projection_config.go#L11-L19)

partition 的入口，可见它只对 `SignalTypeDomain` 和 `SignalTypeEmbedding` 两类施加：

[applySignalGroups：对 domain 与 embedding 分别施加分区](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_groups.go#L5-L24)

核心算法在 `applySignalGroupToMatches`：先收集 contenders，0 个走 default 兜底、1 个早返回、≥2 个选赢家：

[applySignalGroupToMatches：收集候选 → 0 走兜底 / 1 早返回 / ≥2 选赢家并删除败者](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_group_resolution.go#L11-L66)

softmax 与赢家重写在 `selectSignalGroupWinner` + `softmaxScores`。注意赢家是按**原始置信度**最高选的，但写回的置信度在 softmax_exclusive 下是**归一化后的概率**：

[selectSignalGroupWinner + softmaxScores：选最高原始分赢家，softmax_exclusive 下写回归一化概率](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_group_resolution.go#L97-L156)

```go
func softmaxScores(scores []float64, temperature float64) []float64 {
    if temperature <= 0 { temperature = 1.0 }
    maxScore := scores[0]
    for _, score := range scores[1:] { if score > maxScore { maxScore = score } }
    expScores := make([]float64, len(scores)); sum := 0.0
    for i, score := range scores {
        expScore := math.Exp((score - maxScore) / temperature) // 数值稳定的 softmax
        expScores[i] = expScore; sum += expScore
    }
    for i := range expScores { expScores[i] /= sum }
    return expScores
}
```

default 兜底的实现：仅当无任何成员命中、且 default 在成员表里、且 default 尚未被命中时，才合成它：

[applySignalGroupDefaultFallback：无成员命中时合成 default 成员](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_group_resolution.go#L68-L95)

#### 4.2.4 代码实践

1. **实践目标**：亲手验证 softmax + default 的行为，建立对"互斥"和"兜底"的直觉。
2. **操作步骤**：
   - 打开 `config/recipes/balance/recipe.dsl`，找到 L363–L375 的两个 partition。
   - 假设一次请求让 `health` 和 `biology` 两个域同时命中，原始置信度分别为 `0.82` 和 `0.71`，temperature = 0.1。
   - 用上面的 softmax 公式手算 `health`（赢家）的新置信度。
3. **需要观察的现象**：赢家 `health` 的新置信度应当**高于** 0.5 但**低于**它原来的 0.82；而 `biology` 会从命中名单和置信度表里彻底消失。
4. **预期结果**（待本地用计算器验证）：
   - \( \exp(0) = 1 \)（health，做基准），\( \exp((0.71-0.82)/0.1) = \exp(-1.1) \approx 0.333 \)。
   - 归一化：health ≈ \( 1 / (1 + 0.333) \approx 0.75 \)。
   - 即赢家置信度从 0.82 被重写为约 0.75，告诉下游"赢了，但赢得不算特别彻底"。
   - 再换一组 `health=0.95, biology=0.30`：health 新置信度 ≈ \( 1/(1+\exp(-6.5)) \approx 0.9985 \)，几乎一边倒。
5. **default 场景**：假设输入是 `asdfgh jkl` 这种乱码，所有 domain 都不达标 → contenders 为空 → 合成 `domain("other")`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 partition 只对 domain 和 embedding 两类信号施加，而不对 keyword 施加？

> 参考答案：keyword 是人写的规则，命中即布尔，不存在"几个 keyword 互斥竞争同一个语义槽"的问题（一条请求同时含多个 keyword 是正常的、有信息量的）。而 domain/embedding 是学习型信号，它们的候选天然属于"同一个语义空间里互斥的多个选项"（14 个域、16 种意图），必须选一个代表，否则下游规则会被冗余命中淹没。

**练习 2**：把 `balance_domain_partition` 的 temperature 从 0.1 调到 1.0，赢家的 softmax 置信度会变大还是变小？

> 参考答案：变小。温度越大，softmax 分布越平缓，赢家分到的概率份额下降，新置信度会更靠近"均匀分配"的值（两个候选时趋近 0.5）。温度越小，越接近"赢家通吃"，赢家置信度越趋近 1.0。

---

### 4.3 score 评分：weighted_sum 加权组合

#### 4.3.1 概念说明

partition 解决了"同类竞争"，score 解决"异类综合"。一次请求的证据来自许多不同信号族——长度（context）、结构（structure）、关键词（keyword）、嵌入相似度（embedding）、复杂度（complexity）……决策层很难直接在这么多维度上写规则。

score 的做法很朴素：**给每个证据一个权重（可正可负），加权求和成一个连续分数**。以 balance 最核心的 `difficulty_score` 为例，它把约 40 个输入信号揉成一个"难度分"，权重的设计直接编码了"什么让请求更难、什么让它更简单"。

关键三点：

1. **权重可负**：负权重的输入（如 `simple_request_markers`、`fast_qa_en`、`short_context`）表示"这是简单请求的证据，把难度分**往下压**"；正权重表示"这是复杂请求的证据，把分**往上推**"。
2. **三种取值方式**（`value_source`）：
   - `binary`（默认）：命中取 `Match`（缺省 1.0），未命中取 `Miss`（缺省 0）。规则型信号多用这种。
   - `confidence`：取该信号的置信度（0~1 的灰度）。学习型信号多用这种，让"有多像"参与打分。
   - `raw`：取 `SignalValues` 里的原始值。
3. **总分一般不强制归一化到 [0,1]**：它的实际范围由权重大小和命中模式决定，靠下游 mapping 的阈值来切分。

#### 4.3.2 核心流程

score 的数学定义：

\[
\text{score} = \sum_{i} w_i \cdot v_i
\]

其中 \(w_i\) 是权重，\(v_i\) 是输入值（按 `value_source` 解析）。执行伪代码：

```
对每个 score（按拓扑序，见 4.3 补充）：
    total = 0
    for input in score.Inputs:
        v = 按 input.Type 与 input.ValueSource 取值
            - Type=="kb_metric":  取 KB 指标值
            - Type=="projection": 取另一个投影的输出（score 或置信度）
            - ValueSource=="raw":        取 SignalValues
            - ValueSource=="confidence": 命中则取置信度（缺省 1.0），未命中 0
            - 其他（默认 binary）:        命中取 Match(=1)，未命中取 Miss(=0)
        total += input.Weight * v
    ProjectionScores[score.Name] = total
    紧接着对依赖该 score 的 mapping 做映射
```

**补充：score 之间的依赖与拓扑排序。** 一个 score 的输入可以是 `type: "projection"`，即引用另一个 score 的结果或某个 mapping 输出的置信度。这时必须先算被依赖的 score，再算依赖者。`topologicalScoreOrder` 用 DFS 把 score 排成满足依赖的顺序（无依赖时直接原序返回）。balance 的 5 个 score 互不依赖，所以保持声明顺序。

#### 4.3.3 源码精读

score 的 DSL 声明（节选 `difficulty_score` 的少量输入，完整约 40 项）：

[difficulty_score 的 weighted_sum 声明，输入带正负权重与 value_source](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L377-L380)

score 的执行入口 `applyProjections`：先拓扑排序，再逐个算分，算完一个分立刻对依赖它的 mapping 做映射（保证 score→mapping 的接力顺序）：

[applyProjections：拓扑排序 scores → 逐个算分 → 立即施加依赖该分的 mapping](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_projections.go#L20-L37)

加权求和的核心就是一行循环：

[projectionScoreValue：weighted_sum 的实现](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_projection_inputs.go#L34-L40)

```go
func projectionScoreValue(score config.ProjectionScore, results *SignalResults) float64 {
    total := 0.0
    for _, input := range score.Inputs {
        total += input.Weight * projectionInputValue(input, results)
    }
    return total
}
```

输入取值的三分支（kb_metric / projection / 其余按 value_source 分 binary/confidence/raw）：

[projectionInputValue：按 Type 与 ValueSource 解析输入值](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_projection_inputs.go#L42-L64)

confidence 取值的一个关键细节：**命中但无显式置信度时兜底为 1.0**（这正是 u2-l2 讲过的"规则型等价满权重"的根源）：

[projectionInputConfidenceValue：命中取置信度，缺失兜底 1.0，未命中 0](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_projection_inputs.go#L66-L77)

拓扑排序（仅当存在 projection 类型输入时才启用，否则原样返回）：

[topologicalScoreOrder：当 score 依赖其他投影时按 DFS 拓扑排序](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_projection_order.go#L40-L84)

#### 4.3.4 代码实践

> 这是本讲的主实践任务，详见第 5 节综合实践。此处先做一个小热身：在 `difficulty_score`（L377–L380）里找出**负权重**的输入，并解释它们为什么是负的。提示：`simple_request_markers`(-0.26)、`fast_qa_en`/`fast_qa_zh`(-0.18)、`short_context`(-0.1)、`low_question_density`(-0.05) 全是负的——因为它们都是"简单/简短请求"的标志，应当把难度分往下拉。

#### 4.3.5 小练习与答案

**练习 1**：`difficulty_score` 里 `keyword simple_request_markers` 没写 `value_source`，而 `keyword reasoning_request_markers` 写了 `value_source: "confidence"`。两者取值有何不同？

> 参考答案：`simple_request_markers` 走默认 binary——命中贡献 `1.0 * (-0.26) = -0.26`，不命中贡献 0。`reasoning_request_markers` 走 confidence——命中贡献 `0.20 * (该关键词规则的置信度)`，置信度缺省时也是 1.0。区别在于前者只看"有没有"，后者还看"有多像"。

**练习 2**：假如某个 score 的输入里同时有 `type:"projection"` 引用另一个尚未计算的 score，会发生什么？

> 参考答案：`applyProjections` 会先用 `topologicalScoreOrder` 把所有 score 排成依赖优先的顺序，保证被引用的 score 先算、其结果写入 `ProjectionScores` 后，引用者才取值。所以不会取到 0 或旧值。

---

### 4.4 mapping 分带：threshold_bands 与 sigmoid_distance 校准

#### 4.4.1 概念说明

score 产出的是一个连续分数（如 `difficulty_score = 0.6`），但决策层喜欢的是**离散的命名带**（"这条请求属于 complex 档"）。mapping 就是把连续分数**切片**成若干命名带。

balance 的 `difficulty_band` 把难度分切成 4 档：

| 命名带 | 条件 | 含义 |
| --- | --- | --- |
| `balance_simple` | 分数 < 0.18 | 简单快答 |
| `balance_medium` | 0.18 ≤ 分数 < 0.48 | 中等 |
| `balance_complex` | 0.48 ≤ 分数 < 0.82 | 复杂 |
| `balance_reasoning` | 分数 ≥ 0.82 | 需要深度推理 |

两个关键设计：

1. **threshold_bands（首带命中，first-hit）**：按声明顺序逐条检查每个 output 的边界条件（`lt`/`lte`/`gt`/`gte`），**第一条匹配的就胜出**，后面的不再看。因为这几段是首尾相接的，恰好只有一条会命中。（另有 `multi_emit` 方法会发射**所有**匹配带，用于正交策略标签同时传播，balance 没用到。）
2. **sigmoid_distance 校准**：命中的带还要带一个置信度。这个置信度由"分数离最近的边界有多远"决定——用 sigmoid 把"距离"翻译成 0~1：

\[
\text{confidence} = \frac{1}{1 + \exp(-k \cdot d)}, \quad d = \min_{\text{边界}} |\text{分数} - \text{边界}|
\]

其中 \(k\) 是 `slope`（balance 难度带用 \(k=10\)）。直觉非常优雅：

- 分数正好落在边界上（\(d \to 0\)）→ confidence = 0.5（"模棱两可"）。
- 分数稳稳落在带中央（\(d\) 大）→ confidence → 1.0（"很确定"）。

即**离边界越远，越笃定；越贴近边界，越犹豫**。

#### 4.4.2 核心流程

```
对每个 mapping（source = 某个 score）：
    scoreValue = ProjectionScores[mapping.Source]
    switch mapping.Method:
    case "multi_emit":  对每个 output，凡匹配就记录（可多条）
    default(threshold_bands): 按顺序找第一条匹配的 output，记录它（仅一条）
    对记录的 output：
        confidence = sigmoid(slope * boundaryDistance(scoreValue, output))
        把 output.Name 追加进 MatchedProjectionRules
        SignalConfidences["projection:" + output.Name] = confidence
```

注意：mapping 的产出会**反写**进 `SignalConfidences`，键前缀是 `"projection:"`。这正是决策层 ROUTE 里能写 `projection("balance_complex")` 的原因——它在查这张置信度表。

#### 4.4.3 源码精读

mapping 的 DSL 声明，`difficulty_band` 切 4 档、用 sigmoid_distance（slope=10）校准：

[difficulty_band：threshold_bands 切 4 档 + sigmoid_distance 校准](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L402-L407)

mapping 的方法分发：`multi_emit` 发所有匹配带，`threshold_bands`/默认只发第一条：

[applyProjectionMapping：multi_emit 发全部 / threshold_bands 发首条](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_projection_outputs.go#L13-L30)

边界条件的判定，四个比较符逐一检查：

[projectionOutputMatches：gt/gte/lt/lte 四条件与判定](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_projection_outputs.go#L54-L68)

sigmoid 校准，slope 缺省 12、可被配置覆盖：

[projectionOutputConfidence：confidence = 1/(1+exp(-slope*distance))](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_projection_outputs.go#L70-L82)

边界距离 = 到所有已声明边界的最小绝对距离：

[projectionBoundaryDistance：取到各边界的最小绝对距离](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_projection_outputs.go#L84-L109)

#### 4.4.4 代码实践

1. **实践目标**：用 sigmoid_distance 校准建立"贴近边界=不确定"的直觉。
2. **操作步骤**：对 `difficulty_band`（slope=10）手算三个分数的置信度：`0.48`（medium/complex 边界）、`0.65`（complex 带正中央）、`0.82`（complex/reasoning 边界）。
3. **预期结果**（待本地验证）：
   - `0.48`：到边界 0.48 距离 \(d=0\) → confidence = \(1/(1+e^0)=0.5\)（最犹豫）。
   - `0.65`：到 0.48 距离 0.17、到 0.82 距离 0.17 → \(d=0.17\) → confidence = \(1/(1+e^{-1.7}) \approx 0.846\)（较笃定）。
   - `0.82`：\(d=0\) → confidence = 0.5（又犹豫了）。
4. **观察结论**：同样的"complex 档"，分数越靠近带中央置信度越高，越贴近边界越接近 0.5。下游决策可以把这个置信度用于"是否需要更谨慎地路由"。

#### 4.4.5 小练习与答案

**练习 1**：`threshold_bands` 和 `multi_emit` 的本质区别是什么？balance 为什么只用前者？

> 参考答案：`threshold_bands` 是首带命中（first-hit），只输出一条带；`multi_emit` 输出所有匹配带，适合"多个正交标签同时成立"的场景（如一条请求同时打上 `urgency_elevated` 和 `verification_required` 两个互不冲突的标签）。balance 的难度带是**互斥分档**（一条请求只能属于 simple/medium/complex/reasoning 之一），所以必须用 first-hit 的 `threshold_bands`。

**练习 2**：为什么 sigmoid 校准用"到边界的距离"而不是"到带中央的距离"？

> 参考答案：因为"不确定性"只发生在边界附近。带中央无论如何都属于这一档，是确定的；只有当分数贴近两个带的分界线时，才存在"到底算哪一档"的犹豫。用"到边界的最小距离"能把这种犹豫精确地表达为 confidence≈0.5。

---

### 4.5 投影追踪（projection trace）：让投影可解释

#### 4.5.1 概念说明

投影层做了不少"数学运算"（softmax、加权、sigmoid），一旦路由结果出乎意料，运维人员需要知道**为什么**。SR 把每次投影的中间过程记录成一份**版本化 JSON**（schema_version="1"），由分类、重放持久化（router_replay）、面板/API 共同消费。

这份 Trace（`projectiontrace.Trace`）有三个数组，正好对应三类投影：

- `Partitions[]`：每个分区的候选、原始分、softmax 归一化分、赢家、赢家分、top-2 的 margin（"赢得多彻底"）、是否用了 default。
- `Scores[]`：每个 score 的逐项分解——每个输入的权重、取到的值、贡献（权重×值），以及总分。
- `Mappings[]`：每个 mapping 的源分数、每个 output 是否匹配、到边界距离、最终选中的 output 及其置信度。

#### 4.5.2 核心流程

Trace 在 `applyProjections` 末尾由 `mergeProjectionTrace` 一次性合成；partition 的追踪条目则在 partition 执行时（`appendPartitionWinnerTrace` / `appendPartitionDefaultTrace`）逐条追加，最后被合并保留。

#### 4.5.3 源码精读

Trace 的 schema 定义，三数组对应三类投影：

[Trace 结构：Partitions / Scores / Mappings 三数组 + SchemaVersion](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/projectiontrace/trace.go#L10-L15)

partition 追踪条目，记录候选、原始分、归一化分、赢家、margin：

[PartitionResolution：候选/原始分/归一化分/赢家/margin/default_used](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/projectiontrace/trace.go#L24-L36)

score 追踪条目，逐项分解贡献：

[ScoreBreakdown + ScoreInputPart：每个输入的权重/值/贡献与总分](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/projectiontrace/trace.go#L38-L54)

mapping 追踪条目，记录每个 output 的匹配与否、到边界距离、选中 output 及置信度：

[MappingDecision + OutputEvalStep：源分/各带匹配/边界距离/选中带与置信度](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/projectiontrace/trace.go#L56-L72)

score/mapping 追踪的合成逻辑（对每个 score 逐项累加贡献、对每个 mapping 逐 output 判定匹配）：

[mergeProjectionTrace：合成 score 逐项分解与 mapping 各带判定](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_projection_trace.go#L8-L63)

partition 追踪的写入（含 softmax 归一化分与 margin 的计算）：

[appendPartitionWinnerTrace：写候选/原始分/归一化分/margin](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_group_trace.go#L32-L73)

#### 4.5.4 代码实践

1. **实践目标**：学会读一份投影追踪 JSON，定位"这次难度分为什么落到了 complex 档"。
2. **操作步骤**：下面是一份 `difficulty_score` → `difficulty_band` 的追踪**示意片段**（非项目原有数据，为讲解构造的示例）：

   ```json
   {
     "schema_version": "1",
     "scores": [{
       "name": "difficulty_score",
       "total": 0.55,
       "inputs": [
         {"type":"embedding","name":"agentic_workflows","weight":0.20,"value":0.71,"contribution":0.142},
         {"type":"context","name":"long_context","weight":0.18,"value":1.0,"contribution":0.18},
         {"type":"keyword","name":"simple_request_markers","weight":-0.26,"value":0.0,"contribution":0.0},
         {"type":"complexity","name":"agentic_delivery:hard","weight":0.22,"value":1.0,"contribution":0.22}
       ]
     }],
     "mappings": [{
       "mapping_name": "difficulty_band",
       "source_score": "difficulty_score",
       "score_value": 0.55,
       "selected_output": "balance_complex",
       "confidence": 0.832,
       "boundary_distance": 0.07,
       "outputs": [
         {"name":"balance_simple","matched":false,"boundary_distance":0.37},
         {"name":"balance_medium","matched":false,"boundary_distance":0.07},
         {"name":"balance_complex","matched":true,"boundary_distance":0.07},
         {"name":"balance_reasoning","matched":false,"boundary_distance":0.27}
       ]
     }]
   }
   ```
3. **需要观察的现象**：
   - `inputs` 里 `contribution = weight × value`，把每条证据对总分的贡献量化了——这里 `agentic_delivery:hard`(+0.22) 和 `long_context`(+0.18)、`agentic_workflows`(+0.142) 是推高分的主力；`simple_request_markers` 未命中、贡献 0。
   - 总分 0.55 落在 `[0.48, 0.82)` → `balance_complex` 命中。
   - 它到最近边界（0.48）距离 0.07，confidence ≈ \(1/(1+e^{-10×0.07})\approx 0.832\)。
4. **预期结果**：你能指着这份 JSON 说清"为什么是 complex、有多确定"。真实环境下，这份 Trace 会随 `router_replay` 插件落盘，也可在管理面板的投影追踪视图里看到（见 u11-l2）。

#### 4.5.5 小练习与答案

**练习**：上面示例里 `balance_medium` 的 `boundary_distance` 也是 0.07（因为它和 `balance_complex` 共享 0.48 这条边界），但 `matched` 是 false。为什么？

> 参考答案：因为 0.55 不满足 `balance_medium` 的 `gte: 0.18, lt: 0.48`——它 ≥0.48，超出了 medium 的上界。`boundary_distance` 只度量"几何上离边界多近"，不代表"是否落在带内"；是否命中由 `projectionOutputMatches` 的四条件严格判定。两个相邻带共享一条边界，所以它们到该边界的距离相同，但只有一个带能包含这个分数。

---

## 5. 综合实践

现在把三类投影串起来，完成本讲的主实践任务（对应规格里的代码实践任务）。

### 任务一：解析 `difficulty_score` 的全部输入与权重含义

1. 打开 `config/recipes/balance/recipe.dsl` 的 L377–L380（`difficulty_score`）。
2. 把它的约 40 个输入按**权重正负**和**信号族**整理成下表（示例骨架，请补全）：

   | 方向 | 信号族 | 输入名 | 权重 | value_source | 含义 |
   | --- | --- | --- | --- | --- | --- |
   | 压低难度（负） | keyword | simple_request_markers | -0.26 | binary | 用户明说要简短回答 |
   | 压低难度（负） | embedding | fast_qa_en / fast_qa_zh | -0.18 | confidence | 像快问快答 |
   | 压低难度（负） | context | short_context | -0.10 | binary | 上下文很短 |
   | 推高难度（正） | keyword | reasoning_request_markers | +0.20 | confidence | 含推理词 |
   | 推高难度（正） | embedding | agentic_workflows | +0.20 | confidence | 像智能体工作流 |
   | 推高难度（正） | complexity | math_task:hard | +0.22 | binary | 数学难题 |
   | … | … | … | … | … | … |

3. **要点解释**：
   - **负权重** = "简单 / 简短 / 快答"的证据，把难度分往下拉。最大负权重是 `simple_request_markers`(-0.26)，其次是两个 `fast_qa`(-0.18) 和 `short_context`(-0.10)。
   - **正权重** = "复杂 / 长上下文 / 多步骤 / 推理 / 智能体"的证据，把分往上推。最大的几个是 `math_task:hard`(+0.22)、`agentic_delivery:hard`(+0.22)、`reasoning_request_markers`(+0.20)、`agentic_workflows`(+0.20)。
   - **complexity 信号的 `:medium` / `:hard` 后缀**：complexity 信号本身输出多个难度档，这里用 `name: "general_reasoning:hard"` 这样的写法只取"hard"这一档是否命中（binary）。同一信号族按档位分别给不同权重（hard 的权重普遍大于 medium），实现对"多难"的精细加权。

### 任务二：解释 `difficulty_band` 把分数切成哪几段

1. 看 L402–L407 的 `difficulty_band`，把任务一算出的连续分数映射到命名带：
   - 分数 < 0.18 → `balance_simple`（最便宜的模型，如 qwen3.5-rocm）
   - 0.18 ≤ 分数 < 0.48 → `balance_medium`
   - 0.48 ≤ 分数 < 0.82 → `balance_complex`（升级到 gemini-3.1-pro 这类）
   - 分数 ≥ 0.82 → `balance_reasoning`（开 reasoning + high effort）
2. 回到 ROUTE（如 L524 的 `reasoning_deep`、L538 的 `complex_specialist`），注意它们的 WHEN 里大量出现 `projection("balance_reasoning")`、`projection("balance_complex")` —— 这正是决策层消费 mapping 产出的地方。投影层把"几十个零散证据"压缩成了 4 个干净标签，决策层才写得动规则。

### 任务三（源码阅读型）：跟踪一次投影的完整调用链

把本讲学到的调用点串起来，画一张调用链：

```
EvaluateAllSignalsWithContext (classifier_signal_context.go:218)
  └─ applyProjections (classifier_projections.go:5)
       ├─ topologicalScoreOrder            (投影排序，无依赖时原序)
       ├─ projectionScoreValue             (weighted_sum 加权求和)
       │    └─ projectionInputValue        (binary/confidence/raw 取值)
       ├─ applyProjectionMapping           (threshold_bands 首带命中)
       │    ├─ projectionOutputMatches     (gt/gte/lt/lte 判定)
       │    └─ projectionOutputConfidence  (sigmoid_distance 校准)
       └─ mergeProjectionTrace            (生成可解释 JSON)
```

> 说明：partition 的调用链在更早的 `applySignalGroups`（L215），结构与上面类似。本任务不需要运行任何命令，重在理解数据如何从"原始信号"经"分数"变成"带置信度的命名带"，再被决策层读取。

---

## 6. 本讲小结

- **投影层是协调层**：把杂乱、互相竞争的原始信号，用 partition/score/mapping 三类数学方法，压缩成少量干净的**命名路由带**，供决策层消费。
- **partition（softmax_exclusive）**：对 domain/embedding 两类信号施加——多个互斥候选里选原始置信度最高的为赢家，用 softmax（带温度）把赢家置信度重写为概率，败者全部删除；无人命中时合成 `default` 兜底成员，保证分类不落空。
- **score（weighted_sum）**：把多个异类信号按权重（可正可负）加权求和成连续分数；负权重压低分数（简单证据），正权重推高分数（复杂证据）；输入取值有 binary/confidence/raw 三种，规则型信号缺省置信度兜底为 1.0。
- **mapping（threshold_bands + sigmoid_distance）**：按声明顺序找第一条匹配的边界带（首带命中），用"分数到最近边界的距离"经 sigmoid 翻译成 0~1 置信度——离边界越远越笃定，贴边界越犹豫（≈0.5）。
- **产出反写 SignalConfidences**：mapping 的命名带以 `"projection:名字"` 为键写回复信度表，这正是 ROUTE 的 WHEN 能写 `projection("balance_complex")` 的原因。
- **投影追踪（projection trace）**：版本化 JSON，记录 partition 候选/赢家/margin、score 逐项贡献、mapping 各带匹配/边界距离，被分类、router_replay 重放、管理面板共用，让投影完全可解释。

---

## 7. 下一步学习建议

本讲把投影层的"计算"讲透了，但**投影的产出如何被消费**还没展开。建议按以下顺序继续：

1. **u2-l4（Decisions/Routes/Models）**：直接承接本讲——看 ROUTE 的 WHEN 规则如何在本讲的 `projection("balance_complex")` 这类软标签上写 AND/OR/NOT 布尔逻辑，以及 PRIORITY/TIER 如何决定命中顺序与兜底。这是把"投影"接上"决策"的关键一讲。
2. **u5-l2（决策求值管线）**：进入请求主链路，看 `performDecisionEvaluation` 如何在真实请求里把"信号求值 → 投影 → 决策引擎"串起来执行。
3. **u11-l2（投影追踪与可解释性）**：进阶讲，看本讲的 projection trace JSON 如何在管理面板与 router_replay 重放里被消费、可视化。
4. **源码延伸阅读**：想了解投影声明的**校验规则**（比如 members 必须真实存在、mapping 的 source 必须指向已声明的 score、阈值带不能重叠），可读 `src/semantic-router/pkg/config/validator_projection.go` 与 `src/semantic-router/pkg/dsl/validator_projection_partition_test.go`。
