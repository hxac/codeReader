# 投影追踪与可解释性

## 1. 本讲目标

在 u2-l3 里我们建立了投影层的心智模型：投影把杂乱、互相竞争的原始信号，用 partition/score/mapping 三类数学操作协调成少数干净的命名路由带（如 `balance_complex`），再交给决策层消费。但「为什么这次请求被分到了 `balance_complex`？」——这个问题在 u2-l3 里只给出了概念性回答。

本讲回答的是**可解释性（explainability）**问题：投影层每一步的数学运算，是如何被忠实地记录下来、变成一个可以被人审查、被面板展示、被重放系统持久化的 JSON 文档的。学完本讲你应当能够：

- 说出投影追踪 `Trace` 的 schema 版本约定与三大组成（分区、评分、映射）。
- 读懂 `PartitionResolution`（softmax 胜者与边际）、`ScoreBreakdown`（加权贡献分解）、`MappingDecision`（阈值带命中与边界距离）这三组字段的精确含义。
- 描述这份追踪从分类器产出，到被复制进请求上下文，再到写入重放记录、喂给面板的完整复用链路。
- 给定一段真实配置与信号命中，手工推演出对应的投影追踪 JSON。

本讲是「控制面与可观测性」单元里偏「白盒」的一篇：它不改变路由行为，只是把已经发生的投影计算完整曝光出来。

## 2. 前置知识

阅读本讲前，请确认你已建立以下认知（来自前置讲义）：

- **信号-投影-决策四层流水线**（u2-l1）：信号抽取 → 投影协调 → 决策求值 → 模型+插件。本讲聚焦其中的投影层。
- **三类投影操作**（u2-l3）：partition 用 `softmax_exclusive` 在互斥候选里选赢家；score 用 `weighted_sum` 把异类信号加权求和成连续分数；mapping 用 `threshold_bands` 把分数切成命名带，并用 `sigmoid_distance` 把「分数到最近边界的距离」翻译成置信度。
- **请求处理主链路**（u5-l2）：`performDecisionEvaluation` 会先 `evaluateSignalsForDecision` 把全部信号算进 `SignalResults`，再跑决策引擎。投影追踪正是在「算信号」这一步里产出的。
- **信号置信度双通道**（u2-l2）：每条信号有 `SignalConfidences`（置信度，0~1）与 `SignalValues`（原始值）两个数值通道，键统一为 `"族:名"`。

一个需要牢记的事实：投影追踪**只是记录，不参与路由决策**。路由决策读的是写回 `SignalConfidences` 的最终置信度（如 `projection:balance_complex`）；追踪只是把这些置信度是怎么算出来的「草稿纸」原样存档。

## 3. 本讲源码地图

本讲涉及的关键文件如下表。核心是 `projectiontrace/trace.go`——整个包只有这一个文件，是一个**纯类型定义包**，不含任何逻辑：

| 文件 | 作用 |
| --- | --- |
| [src/semantic-router/pkg/projectiontrace/trace.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/projectiontrace/trace.go) | 定义版本化 JSON 的全部 schema：`Trace` 及其三大组成、分区/评分/映射的细粒度结构体。 |
| [src/semantic-router/pkg/classification/classifier_signal_group_trace.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_signal_group_trace.go) | 产出**分区追踪**（`PartitionResolution`）：记录赢家、原始分、归一化分、边际、是否用了 default 兜底。 |
| [src/semantic-router/pkg/classification/classifier_signal_group_resolution.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_signal_group_resolution.go) | 分区规约的数学核心：softmax 归一化、赢家选择、default 兜底。 |
| [src/semantic-router/pkg/classification/classifier_projection_trace.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_projection_trace.go) | 产出**评分追踪**与**映射追踪**（`mergeProjectionTrace`），并把已有的分区追踪合并进同一个 `Trace`。 |
| [src/semantic-router/pkg/classification/classifier_projection_inputs.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_projection_inputs.go) | 评分输入取值（binary/confidence/raw/kb_metric 四种来源）。 |
| [src/semantic-router/pkg/classification/classifier_projection_outputs.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_projection_outputs.go) | 映射输出的阈值匹配、边界距离、sigmoid 置信度校准。 |
| [src/semantic-router/pkg/classification/classifier_projections.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_projections.go) | 投影编排入口 `applyProjections`：先算 score、再跑 mapping、最后合并追踪。 |
| [src/semantic-router/pkg/routerreplay/store/store.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/routerreplay/store/store.go) | 重放记录 `Record` 把 `Trace` 作为字段持久化（JSON / Postgres）。 |

> 提示：本讲引用的行号基于当前 HEAD `e7cbab82`。`projectiontrace` 包极小且稳定，是阅读全讲的锚点。

## 4. 核心概念与源码讲解

### 4.1 投影追踪 Trace：版本化 schema 与三大组成

#### 4.1.1 概念说明

「投影追踪」是一份**每请求一份、版本化、纯数据**的 JSON 文档，用来回答「这次请求的投影层到底发生了什么」。它由三个互相独立的数组组成，分别对应投影层的三类操作：

- `partitions`：分区（partition）是怎么从多个互斥候选里选出唯一赢家的。
- `scores`：评分（score）的连续分数是哪些输入、按什么权重加出来的。
- `mappings`：映射（mapping）把这个分数切进了哪一段命名带。

这份文档被三处消费：分类器自身（写回置信度前的草稿）、重放系统（持久化进 `Record`）、管理面板（展示给运维）。为了让多个消费者、多个版本的服务能安全共存，整个 schema 用一个 `SchemaVersion` 常量打头。

#### 4.1.2 核心流程

整份数据的产出一侧串成一条单向流水线：

```text
分类器算信号
   │
   ├─ applySignalGroups（分区）──── appendPartition*Trace  ──▶ results.ProjectionTrace.Partitions
   │
   └─ applyProjections（评分+映射）
          ├─ 逐 score 算总分 ──▶ ProjectionScores[name]
          ├─ 逐 mapping 切带  ──▶ SignalConfidences["projection:带名"]
          └─ mergeProjectionTrace(results, 配置) ──▶ 覆盖 results.ProjectionTrace
                                  （保留上面已有的 Partitions，再补上 Scores + Mappings）
```

关键点是**分两阶段、合并写入**：分区追踪在「分区规约」时逐条 append 进 `results.ProjectionTrace.Partitions`；评分与映射追踪则在 `applyProjections` 收尾时由 `mergeProjectionTrace` 一次性补齐，同时**原样保留**已经写好的分区条目。

#### 4.1.3 源码精读

整个 schema 的根与版本常量只有一个文件定义。schema 主版本被硬编码为字符串 `"1"`：

[trace.go:7-15](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/projectiontrace/trace.go#L7-L15) 定义 `SchemaVersion` 与 `Trace` 根结构。`Trace` 三个切片都用 `omitempty`，意味着没有分区操作时 `partitions` 字段不会出现在 JSON 里——这也是为什么一个只有 score+mapping 的配方，其追踪 JSON 里看不到 partitions。

注意 `SchemaVersion` 是包级常量而非配置项，所有生产者与消费者都引用同一个符号（如 `projectiontrace.SchemaVersion`），因此不会出现「这条记录是哪个版本」的歧义。三类消费者只要在反序列化后检查 `schema_version`，就能决定如何解读字段。

#### 4.1.4 代码实践

**实践目标**：确认 `Trace` 的 JSON 形态与 `omitempty` 行为。

**操作步骤**：

1. 阅读 [trace.go:10-15](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/projectiontrace/trace.go#L10-L15)，记下三个切片字段的 json tag。
2. 打开重放记录的往返测试 [record_projection_trace_test.go:10-43](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/routerreplay/store/record_projection_trace_test.go#L10-L43)，里面手工构造了一个同时含 partitions/scores/mappings 三段的 `Trace` 并做 JSON 往返。

**需要观察的现象**：测试里 `PartitionResolution`、`ScoreBreakdown`、`MappingDecision` 三段各放了一条；`Trace` 被塞进 `Record.ProjectionTrace` 字段后，`json.Marshal` → `json.Unmarshal` 能完整还原（`assertProjectionTraceRoundTrip` 校验 winner、total、selected_output 都没丢）。

**预期结果**：你能口述出「一份完整的追踪 JSON 顶层有 `schema_version`、`partitions`、`scores`、`mappings` 四个键，后三者皆可省略」。

#### 4.1.5 小练习与答案

**练习 1**：如果一个配方只配置了 score 和 mapping、没有任何 partition，它的追踪 JSON 里会有 `partitions` 键吗？

**答案**：不会。`Trace.Partitions` 带 `omitempty`，空切片不会序列化。但只要发生过一次分区规约，`appendPartitionTraceEntry` 就会懒初始化 `results.ProjectionTrace` 并 append，于是该键才会出现。

**练习 2**：为什么 `SchemaVersion` 用包级常量而不是放进配置文件？

**答案**：因为它是「线缆契约（wire contract）」而非「用户可调参数」。生产者（分类器）和所有消费者（重放存储、面板）必须引用同一个值，写死成常量可杜绝不同代码路径写出版本号不一致的记录。

---

### 4.2 分区解析：PartitionResolution（softmax 胜者与边际）

#### 4.2.1 概念说明

`PartitionResolution` 记录的是 partition 操作的「庭审记录」：在一组互斥候选（如 balance 配方里 `balance_domain_partition` 的 biology/health/economics… 等 MMLU 域）里，谁被命中了、谁是赢家、赢了多少、是不是靠兜底兜上来的。

最关键的字段是「赢家（winner）」和「边际（margin）」：

- **赢家**：原始置信度最高的那个候选。对 `softmax_exclusive` 语义，赢家分数会被 softmax 重新归一化成概率后写回 `SignalConfidences`；非 softmax 语义则直接用原始分。
- **边际**：第一名与第二名的分差。边际大说明分类很笃定，边际小说明这次分区是在「险胜」，是值得被运维关注的灰度请求。
- **DefaultUsed**：当没有任何候选命中时，分区会合成一个 `default` 兜底成员（如 `other`），这个布尔标记就是用来曝光这种「无人认领、走了兜底」的情况。

#### 4.2.2 核心流程

分区规约发生在一个信号族（如 domain）内部，当一个 partition group 的多个成员同时命中时触发：

```text
matched 命中列表
   │  筛出「属于本组 且 有置信度」的成员 → contenders
   │
   ├─ contenders 为空 ──▶ 合成 default 兜底（appendPartitionDefaultTrace，DefaultUsed=true）
   ├─ contenders 只有 1 个 ──▶ 无需竞争，直接返回，不写追踪
   └─ contenders ≥ 2 ──▶ selectSignalGroupWinner（softmax 或 argmax）→ appendPartitionWinnerTrace
```

softmax 归一化（温度 \(\tau\)）的公式为：

\[
p_i = \frac{\exp((s_i - s_{\max})/\tau)}{\sum_j \exp((s_j - s_{\max})/\tau)}
\]

减去 \(s_{\max}\) 是数值稳定技巧（让指数最大值为 0，避免上溢）。温度 \(\tau\) 越小，赢家概率越尖锐（越「赢家通吃」）。注意 softmax 是**保序变换**：原始分最高的候选，归一化后概率仍最高，因此「赢家」永远是原始分的 argmax，softmax 只改变赢家分数的绝对值（写回置信度用）与边际。

#### 4.2.3 源码精读

分区的实际规约逻辑在 `applySignalGroupToMatches` 里。当竞争者 ≥ 2 时调用 `selectSignalGroupWinner` 选赢家，紧接着调用 `appendPartitionWinnerTrace` 写追踪：

[classifier_signal_group_resolution.go:41-53](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_signal_group_resolution.go#L41-L53)：选赢家后追加分区追踪，然后把非赢家的竞争者从置信度表里删掉、把赢家的置信度改写成归一化后的 `winnerScore`。这一段揭示了一个重要事实——**追踪记录发生在置信度被改写之前/之中**，所以追踪里的 `RawWinnerScore`（softmax 前的原始分）与 `WinnerScore`（写回用的归一化分）能并排保存，让审查者一眼看出「这个 0.95 的置信度是 softmax 放大出来的，原始分其实只有 0.6」。

赢家选择与 softmax 的实现在 [classifier_signal_group_resolution.go:97-122](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_signal_group_resolution.go#L97-L122) 与 [classifier_signal_group_resolution.go:124-156](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_signal_group_resolution.go#L124-L156)：先用一次线性扫描找原始分 argmax，再按 `semantics` 决定是否做 softmax 归一化。

追踪条目的具体拼装在 [classifier_signal_group_trace.go:32-73](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_signal_group_trace.go#L32-L73)（`appendPartitionWinnerTrace`）和兜底场景的 [classifier_signal_group_trace.go:21-30](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_signal_group_trace.go#L21-L30)（`appendPartitionDefaultTrace`）。每个竞争者被存成一条 `PartitionContender`，对 softmax 语义还会额外填 `NormalizedScore`（指针类型，`nil` 即「非 softmax、无归一化分」）。

边际由 [classifier_signal_group_trace.go:84-91](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_signal_group_trace.go#L84-L91) 的 `topTwoMargin` 计算：拷贝一份分数、降序排序、取 `sorted[0] - sorted[1]`。softmax 语义用归一化分算边际，非 softmax 用原始分。

对应的 schema 字段在 [trace.go:25-36](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/projectiontrace/trace.go#L25-L36)：`Winner`、`WinnerScore`（写回置信度的值）、`RawWinnerScore`（softmax 前的原始分）、`Margin`、`DefaultUsed`、以及 `Contenders[]`（每个含 `RawScore` 与可选 `NormalizedScore`）。

#### 4.2.4 代码实践

**实践目标**：手工推演一次 softmax 分区的追踪字段。

**场景**：balance 配方的 `balance_domain_partition`（[config.yaml:1121-1123](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/config/recipes/balance/config.yaml#L1121-L1123) 声明 `semantics: softmax_exclusive`、`temperature: 0.1`）。假设一次请求同时命中 `health`（原始置信度 0.6）与 `biology`（0.3），两者都是组成员。

**操作步骤**：

1. 用上面的 softmax 公式，取 \(\tau=0.1\)，算 health 与 biology 的归一化概率。
2. 写出对应的 `PartitionResolution` 的 `Winner`、`WinnerScore`、`RawWinnerScore`、`Margin`。

**需要观察的现象 / 预期结果**：

- \(s_{\max}=0.6\)。\(p_{\text{health}}=\exp(0)/(\exp(0)+\exp(-3))\approx 1/1.0498\approx 0.953\)；\(p_{\text{biology}}\approx 0.047\)。
- `Winner=health`、`WinnerScore≈0.953`（写回 `SignalConfidences["domain:health"]` 的值）、`RawWinnerScore=0.6`、`Margin≈0.953−0.047=0.906`。
- 两个竞争者各成一条 `PartitionContender`，health 的 `NormalizedScore≈0.953`、biology 的 `NormalizedScore≈0.047`。
- 温度 0.1 极小，所以即使原始分差只有 0.3，归一化后几乎「赢家通吃」——这正是运维看边际时要警惕的：高 `WinnerScore` 不等于高原始置信度。

**待本地验证**：上述数值可用 [classifier_signal_groups_test.go:394-399](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_signal_groups_test.go#L394-L399) 的断言（`winner == "economics"`、只产出 1 条分区追踪）作为运行时佐证；默认兜底场景见 [classifier_signal_groups_test.go:432-437](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_signal_groups_test.go#L432-L437)（`DefaultUsed=true`、`Winner="other"`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `PartitionContender.NormalizedScore` 是指针 `*float64` 而不是 `float64`？

**答案**：为了区分「非 softmax 语义（没有归一化分）」与「softmax 语义下归一化分恰好为 0」。指针 + `omitempty`：非 softmax 语义时填 `nil`、字段不出现在 JSON 里；softmax 时填具体指针值。普通 `float64` 做不到这种三态区分。

**练习 2**：一次请求只命中了 partition 组里的 1 个成员，追踪里会有这条 partition 记录吗？

**答案**：不会。`applySignalGroupToMatches` 在 `len(contenders)==1` 时直接返回、不写追踪（见 [classifier_signal_group_resolution.go:37-39](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_signal_group_resolution.go#L37-L39)）。追踪只在「真正发生了竞争（≥2）」或「走了 default 兜底」时才记录。

---

### 4.3 评分分解：ScoreBreakdown（加权贡献）

#### 4.3.1 概念说明

`ScoreBreakdown` 回答的是「这个连续分数是哪些输入、按什么权重、各贡献了多少加出来的」。一个 score（如 balance 的 `difficulty_score`）的 `weighted_sum` 方法本质是：

\[
\text{total} = \sum_i w_i \cdot v_i
\]

其中 \(w_i\) 是配置里的 `weight`（可正可负），\(v_i\) 是该输入的**取值**——取值有四种来源，由 `value_source` 或输入类型决定。追踪把每个输入的 \(w_i\)、\(v_i\)、以及贡献 \(w_i \cdot v_i\) 都单独存下来，于是审查者能看到「这个 0.74 的难度分，主要是 reasoning 标记贡献了 +0.54、长上下文贡献了 +0.20，而简单请求标记贡献了 −0.26」。

#### 4.3.2 核心流程

评分输入取值是关键。`projectionInputValue` 按下面的优先级解析一个输入应该取多少：

```text
input.Type == "kb_metric"      ──▶ results.KBMetricValues[kb:metric]
input.Type == "projection"     ──▶ 喂另一个 score/mapping 的产出（score 用 ProjectionScores，confidence 用 SignalConfidences）
否则按 input.ValueSource：
   "raw"        ──▶ results.SignalValues["族:名"]（缺失=0）
   "confidence" ──▶ 命中则 SignalConfidences["族:名"]（缺失兜底 1.0）；未命中=0
   其他/缺省    ──▶ binary：命中=Match(默认1.0)，未命中=Miss(默认0)
```

这印证了 u2-l3 的结论：规则型信号命中但没显式置信度时取 1.0（即 `confidence` 来源缺省 1.0），所以规则信号等价满权重；而 `raw` 来源对缺失值直接返回 0、且允许负值（如 `structure:many_questions=-2.5` 也能原样读出）。负权重 + 正取值，或正权重 + 负取值，都能压低总分——这就是「简单证据拉低难度分」的实现机制。

#### 4.3.3 源码精读

评分追踪在 `mergeProjectionTrace` 里生成。它对配置里的每个 score 重建一遍求和过程，并记录每个输入的贡献：

[classifier_projection_trace.go:15-34](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_projection_trace.go#L15-L34)：遍历 `score.Inputs`，用 `projectionInputValue` 取每个输入的值 \(v\)，`contrib = weight * v` 累加成 `sum`，并把 `(Type, Name, KB, Metric, Weight, Value, Contribution)` 存成一条 `ScoreInputPart`，最后 `sb.Total = sum`。注意这里**重新取值**而非读取已经算好的 `ProjectionScores`——这保证了追踪里的每个 `Value` 都和「真正参与求值时的取值」一致，即使该值是临时算出来的。

取值函数本体在 [classifier_projection_inputs.go:42-64](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_projection_inputs.go#L42-L64)，四种分支的细节在 [classifier_projection_inputs.go:66-88](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_projection_inputs.go#L66-L88)（confidence / binary）。`confidence` 来源的「缺失兜底 1.0」逻辑在 [classifier_projection_inputs.go:70-77](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_projection_inputs.go#L70-L77)：命中但置信度表里没有/为 0，就当 1.0。

对应的 schema 在 [trace.go:39-54](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/projectiontrace/trace.go#L39-L54)：`ScoreBreakdown{Name, Total, Inputs[]}`，`ScoreInputPart{Type, Name, KB, Metric, Weight, Value, Contribution}`。`KB`/`Metric`/`Name` 都是 `omitempty`，只为在该输入是 kb_metric 或命名信号时才出现，避免噪声。

#### 4.3.4 代码实践

**实践目标**：读懂一个真实评分分解，并验证贡献之和等于总分。

**素材**：balance 的 `difficulty_score`（[config.yaml:1145-1287](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/config/recipes/balance/config.yaml#L1145-L1287)）是一个有十几路输入的 `weighted_sum`。它的第一个输入是 `simple_request_markers`（keyword，`weight: -0.26`，负权重），后面还有若干 `complexity:*:hard`（正权重 0.18~0.22）。

**操作步骤**：

1. 在 `difficulty_score` 的 inputs 里挑出全部**负权重**输入，说明它们各自压低难度分的语义（如 `simple_request_markers` 表示「这是简单请求」、`fast_qa_en` 表示「像快问快答」）。
2. 假设一次请求命中了 `math_task:hard`（complexity，weight 0.22，binary 取值 1.0）和 `simple_request_markers`（keyword，weight −0.26，binary 取值 1.0），其余未命中按 0 计。手工算 total，并写出这两条 `ScoreInputPart`。

**需要观察的现象 / 预期结果**：

- `math_task:hard` 贡献 \(0.22 \times 1.0 = +0.22\)；`simple_request_markers` 贡献 \(-0.26 \times 1.0 = -0.26\)；其余未命中输入（binary 缺省 Miss=0）贡献 0；total \(= 0.22 - 0.26 = -0.04\)。
- 负 total 是合法的：`difficulty_score` 允许为负，随后 mapping 的 `balance_simple` 带（`lt 0.18`）会接住它。
- 单元测试 [classifier_projections_test.go:138-194](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_projections_test.go#L138-L194) 用 `raw` 来源演示了正负权重混合（`0.5*4.0 + 0.3*7.0 = 4.1`），可与本例对照。

#### 4.3.5 小练习与答案

**练习 1**：`confidence` 来源与默认（binary）来源对一个「命中、但置信度表里没有该键」的规则型信号，取值有何不同？

**答案**：`confidence` 来源返回 1.0（缺失兜底）；binary 来源返回 `Match`（默认也是 1.0，但可被配置改成别的值）。二者对默认配置等价，但 binary 允许把「命中」映射成任意分值、把「未命中」映射成 `Miss`（非 0），灵活度更高。

**练习 2**：为什么追踪要「重新取值」而不是直接读 `ProjectionScores[name]`？

**答案**：因为 `ProjectionScores` 只存了最终总分，丢了「每个输入贡献多少」的中间信息。追踪的价值恰恰在于分解，所以必须重放求和过程、逐项记录 `Value` 与 `Contribution`。这也顺带保证了追踪与实际求值用的是同一套取值函数（同一个 `projectionInputValue`），不会出现「追踪算的」和「路由用的」不一致。

---

### 4.4 映射决策：MappingDecision（阈值带与边界距离）

#### 4.4.1 概念说明

`MappingDecision` 回答「这个分数最终被切进了哪一段命名带、有多笃定」。mapping 把一个连续分数（如 `difficulty_score`）按声明顺序的阈值带（`balance_simple`/`balance_medium`/`balance_complex`/`balance_reasoning`）切成互斥段，命中的那一段就是 `SelectedOutput`。

「多笃定」由两个量刻画：

- **BoundaryDistance（边界距离）**：分数到该带**任一活跃边界**的最近距离。离边界越远说明分数稳稳落在带中央、越笃定；贴边界说明险险落进这段、随时可能掉到邻段。
- **Confidence（置信度）**：用 `sigmoid_distance` 校准把边界距离翻译成 0~1 的概率。

这两个量会同时记录在 mapping 层（`SelectedOutput` 的总体置信度）和每个 output 的逐带评估里（`OutputEvalStep`），让审查者既看到「选了谁」，也看到「每段带各自离这个分数多远、为什么没被选中」。

#### 4.4.2 核心流程

映射分两种 method，决定「命中多条带时怎么办」：

```text
mapping.Method == "multi_emit"  ──▶ 每条命中的带都发射（允许正交策略标签同时生效）
default / "threshold_bands"     ──▶ 只发射第一条命中的带（matchProjectionOutput 按声明顺序首个命中）
```

阈值匹配本身（`projectionOutputMatches`）要求该带声明的**所有**边界约束同时满足：

\[
\text{matched} = (s > \text{GT}) \wedge (s \ge \text{GTE}) \wedge (s < \text{LT}) \wedge (s \le \text{LTE})
\]

未声明的边界自动跳过（视为真）。边界距离取该带所有活跃边界距离的最小值：

\[
d = \min_{\text{活跃边界 } b} |s - b|
\]

sigmoid 置信度校准（斜率 \(k\)，默认 12）：

\[
c = \frac{1}{1 + \exp(-k \cdot d)}
\]

\(d\) 越大、\(c\) 越接近 1（笃定）；\(d \to 0\)（贴边界）时 \(c \to 0.5\)（犹豫）；无任何边界（单边无界带）时距离定义为 1.0。

#### 4.4.3 源码精读

映射追踪同样在 `mergeProjectionTrace` 里生成。对每个 mapping，先从 `ProjectionScores` 取分数，再逐 output 评估：

[classifier_projection_trace.go:35-61](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_projection_trace.go#L35-L61)：对每个 output 算 `matched` 与 `boundary_distance`，存成一条 `OutputEvalStep`；**第一条**命中的 output 成为 `SelectedOutput`，并把它的置信度（`projectionOutputConfidence`）与边界距离填到 mapping 层。注意「第一条命中」与运行时 `applyProjectionMapping` 的 `threshold_bands` 行为（首带命中即止）保持一致，追踪如实反映这一规则。

阈值匹配与边界距离的实现在 [classifier_projection_outputs.go:54-68](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_projection_outputs.go#L54-L68)（`projectionOutputMatches`：四个边界逐个 AND）和 [classifier_projection_outputs.go:84-109](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_projection_outputs.go#L84-L109)（`projectionBoundaryDistance`：取所有活跃边界距离的最小值，无边界返回 1.0）。sigmoid 校准在 [classifier_projection_outputs.go:70-82](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_projection_outputs.go#L70-L82)：斜率优先取 `mapping.Calibration.Slope`，否则默认 12.0，再用 `1/(1+exp(-slope*d))`。

对应的 schema 在 [trace.go:57-72](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/projectiontrace/trace.go#L57-L72)：`MappingDecision{MappingName, SourceScore, ScoreValue, SelectedOutput, Confidence, BoundaryDistance, Outputs[]}`，`OutputEvalStep{Name, Matched, BoundaryDistance}`。`SourceScore` 是字符串（指向某个 score 的名字），`ScoreValue` 是该 score 的实际数值。

#### 4.4.4 代码实践

**实践目标**：给定分数，推演映射追踪的全部字段（本讲的招牌实践，也是规格里指定的任务）。

**场景**：沿用 4.3 里 balance 的 `difficulty_score`。假设一次请求算得 `difficulty_score = 0.74`。`difficulty_band` 映射配置（[config.yaml:1042-1059](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/config/recipes/balance/config.yaml#L1042-L1059)，`calibration.slope: 10`）有四段带：

| 带名 | 约束 | 0.74 是否命中 |
| --- | --- | --- |
| `balance_simple` | `lt 0.18` | 否 |
| `balance_medium` | `gte 0.18 lt 0.48` | 否 |
| `balance_complex` | `gte 0.48 lt 0.82` | **是** |
| `balance_reasoning` | `gte 0.82` | 否 |

**操作步骤**：

1. 对每段带算 `boundary_distance = min(|0.74 − 活跃边界|)`。
2. 找出第一条命中的带，作为 `SelectedOutput`；用 slope=10 算它的 sigmoid 置信度。
3. 把结果写成下面的 JSON。

**需要观察的现象 / 预期结果**：

- 边界距离：simple = |0.74−0.18| = 0.56；medium = min(0.56, 0.26) = 0.26；complex = min(0.26, 0.08) = 0.08；reasoning = |0.82−0.74| = 0.08。
- 命中的是 `balance_complex`（首条命中且唯一命中）。它的 `BoundaryDistance=0.08`，`Confidence = 1/(1+exp(−10×0.08)) = 1/(1+exp(−0.8)) ≈ 1/1.449 ≈ 0.69`。
- `difficulty_band` 这条分数贴在 complex/reasoning 的分界（0.82）附近，所以置信度只有 0.69、犹豫不决——这正是 sigmoid_distance 想要表达的「险险落进 complex 段」。

对应追踪 JSON（示例代码，字段取自真实 schema）：

```json
{
  "schema_version": "1",
  "scores": [
    {
      "name": "difficulty_score",
      "total": 0.74,
      "inputs": [
        {"type": "keyword", "name": "reasoning_request_markers", "weight": 0.6, "value": 0.9, "contribution": 0.54},
        {"type": "context", "name": "long_context", "weight": 0.2, "value": 1.0, "contribution": 0.20}
      ]
    }
  ],
  "mappings": [
    {
      "mapping_name": "difficulty_band",
      "source_score": "difficulty_score",
      "score_value": 0.74,
      "selected_output": "balance_complex",
      "confidence": 0.69,
      "boundary_distance": 0.08,
      "outputs": [
        {"name": "balance_simple",   "matched": false, "boundary_distance": 0.56},
        {"name": "balance_medium",   "matched": false, "boundary_distance": 0.26},
        {"name": "balance_complex",  "matched": true,  "boundary_distance": 0.08},
        {"name": "balance_reasoning","matched": false, "boundary_distance": 0.08}
      ]
    }
  ]
}
```

> 上面 `scores[0].total = 0.74` 的两个输入（0.6×0.9 + 0.2×1.0）取自单元测试 [classifier_projections_test.go:9-77](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_projections_test.go#L9-L77) 的同款设置（该测试断言 `difficulty_score ≈ 0.74`），可作运行时佐证。注意该测试用的是自己的简化 mapping（0.7 切带、slope 默认 12），所以它那里选中的是 `balance_reasoning`；本实践用的是真实 balance 配置（0.82 切带、slope 10），故选中 `balance_complex`。两套数字都正确，差别只在配置。

#### 4.4.5 小练习与答案

**练习 1**：若把 `calibration.slope` 从 10 调到 50，同一条 `boundary_distance=0.08` 的请求，置信度会变高还是变低？

**答案**：变高。\(c = 1/(1+\exp(-k d))\)，\(k\) 越大、同样的 \(d\) 会被推得越接近 1。slope 是「离边界后多快变得笃定」的陡峭度旋钮：slope 大意味着只要稍微远离边界就接近满置信度，slope 小则置信度曲线平缓、始终偏犹豫。

**练习 2**：`multi_emit` 与 `threshold_bands` 在追踪里的差别体现在哪？

**答案**：在**运行时** `applyProjectionMapping`（[classifier_projection_outputs.go:13-30](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_projection_outputs.go#L13-L30)）`multi_emit` 会发射所有命中带、`threshold_bands` 只发射首条命中带。但**追踪层**（`mergeProjectionTrace`）始终评估全部带、并只把第一条命中的带标为 `SelectedOutput`——也就是说追踪记录的是「评估全景」，而 `MatchedProjectionRules`（实际写回、供决策消费的命中列表）才随 method 变化。测试 [classifier_projections_test.go:750-789](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_projections_test.go#L750-L789) 对照了这两者在 `MatchedProjectionRules` 上的差异。

---

### 4.5 可解释复用：从分类到重放与面板

#### 4.5.1 概念说明

`projectiontrace` 被刻意设计成**纯类型包**（只有 `trace.go` 一个文件、零逻辑），目的是让它能被多个包安全导入而不引入循环依赖。于是同一份 `Trace` 数据沿着三个阶段被复用：

1. **产出**：分类器在算信号时把 `Trace` 写进 `SignalResults.ProjectionTrace`。
2. **搬运**：请求处理链路把它从 `SignalResults` 深拷贝进请求上下文 `RequestContext.VSRProjectionTrace`。
3. **持久化与展示**：重放记录把它作为字段存进 `Record.ProjectionTrace`，落盘为 JSON/Postgres，再被面板读取；列表摘要视图则会**主动清空**它以减小体积。

这条链路让一次「为什么路由到 X」的疑问，可以一路回溯到分区边际、评分各路贡献、映射边界距离——而无需重新跑一次推理。

#### 4.5.2 核心流程

```text
classification.SignalResults.ProjectionTrace
        │  (req_filter_classification_signal.go: 深拷贝)
        ▼
extproc.RequestContext.VSRProjectionTrace
        │  (recorder.go: 再次深拷贝进重放记录)
        ▼
routerreplay.store.Record.ProjectionTrace
   ├─ Postgres：marshal 成 JSON 列（postgres_record_row.go）
   ├─ JSON 文件：随 Record 序列化
   └─ 列表摘要：list_summary_record.go 主动置 nil（只保留 names）
        │
        ▼
   面板 / 重放回放 / 运维排查
```

两处深拷贝都采用「marshal → unmarshal」的笨办法，目的是**彻底断开指针引用**：请求上下文与重放记录的生命周期不同，若共享底层切片，后续对 `SignalResults` 的改写会污染已落盘的记录。

#### 4.5.3 源码精读

`Trace` 作为信号结果的字段挂在 [classifier_signal_results.go:39](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_signal_results.go#L39)，注释明确写着「Explainability payload for projections (replay / dashboard)」——它的消费对象在字段注释里就写定了。

搬运到请求上下文：[req_filter_classification_signal.go:126](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/req_filter_classification_signal.go#L126) 把 `signals.ProjectionTrace` 经 `cloneProjectionTraceForReplay` 深拷贝后赋给 `ctx.VSRProjectionTrace`；上下文字段定义在 [request_context.go:208](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/request_context.go#L208)。深拷贝实现 [req_filter_classification_signal.go:142-157](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/req_filter_classification_signal.go#L142-L157) 就是 `json.Marshal` → `json.Unmarshal`，失败时记 warn 并返回 nil（宁可丢追踪也不污染数据）。

写入重放记录：[recorder.go:215](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/recorder.go#L215) 把 `ctx.VSRProjectionTrace` 再次拷进 `Record`；`Record.ProjectionTrace` 字段在 [store.go:178](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/routerreplay/store/store.go#L178)（json tag `projection_trace,omitempty`）。落 Postgres 时，[postgres_record_row.go:104](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/routerreplay/store/postgres_record_row.go#L104) 把它 marshal 进 `projection trace` 列、[postgres_record_row.go:339](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/routerreplay/store/postgres_record_row.go#L339) 读回时反序列化。`Record` 自身的克隆（[store.go:359](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/routerreplay/store/store.go#L359) 与 [store.go:424](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/routerreplay/store/store.go#L424)）也会深拷贝追踪，保证重放回放不会串改原始记录。

体积控制：列表摘要 [list_summary_record.go:14](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/routerreplay/list_summary_record.go#L14) 主动 `out.ProjectionTrace = nil`。因为面板的「记录列表」只需要知道命中了哪条路由，不需要每条的完整投影草稿；只有点进单条详情时才从完整 `Record` 读追踪。这一取舍被测试 [list_summary_record_test.go:29-31](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/routerreplay/list_summary_record_test.go#L29-L31) 固化（断言「expected projection trace cleared」）。

#### 4.5.4 代码实践

**实践目标**：追踪一次投影追踪从产出到落盘的完整调用链。

**操作步骤**：

1. 在 [classifier_projections.go:36](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_projections.go#L36)（`applyProjections` 收尾的 `mergeProjectionTrace`）处，确认评分与映射追踪被写进 `results.ProjectionTrace`，且之前分区阶段写入的 `Partitions` 被 `mergeProjectionTrace` 开头的 [classifier_projection_trace.go:9-14](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_projection_trace.go#L9-L14) 原样保留。
2. 沿 `req_filter_classification_signal.go:126` → `recorder.go:215` → `store.go:178` → `postgres_record_row.go:104` 顺读，画出数据在四个包之间的流转。
3. 运行 `go test ./pkg/routerreplay/store/ -run TestRecordJSONRoundTripProjectionTrace -v`（在 `src/semantic-router/` 下）。

**需要观察的现象**：测试构造了一个含 partition/score/mapping 三段的 `Trace`，塞进 `Record` 做 JSON 往返，再断言三段都完整还原（[record_projection_trace_test.go:60-82](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/routerreplay/store/record_projection_trace_test.go#L60-L82)）。

**预期结果**：测试通过，证明 `Trace` 经 JSON 序列化后字段零丢失——这是它能在 Postgres 列与文件存储之间安全往返的保证。若你无法运行测试，则标注「待本地验证」，但可依据该测试的断言描述往返正确性。

**待本地验证**：`go test` 的实际输出（环境是否就绪、是否需要先 `make vllm-sr-dev` 拉起依赖）请在你本机确认。

#### 4.5.5 小练习与答案

**练习 1**：为什么搬运追踪要用 `json.Marshal` → `json.Unmarshal` 这种「笨办法」深拷贝，而不是手写一个逐字段复制？

**答案**：因为 `Trace` 是嵌套结构（切片里套结构体、结构体里又有 `*float64` 指针），手写深拷贝容易漏掉一层（尤其 `NormalizedScore` 这种指针字段），一旦漏了就会出现「请求上下文和重放记录共享底层数组」的隐式别名 bug。Marshal/Unmarshal 虽然慢，但保证彻底断开引用，且随 schema 演进自动覆盖新字段。失败时返回 nil 而非半成品，是 fail-safe 取舍。

**练习 2**：列表摘要为什么要把 `ProjectionTrace` 清空，却不清空 `Decision`？

**答案**：列表视图的核心信息是「这条请求最终去了哪」（由 `Decision`/`SelectedModel` 给出），投影草稿是排查细节时才需要的「大块头」附帽数据。清空追踪是为了把列表接口的体积压下来（一条追踪可能含十几路 score inputs × 若干 mappings），而保留 `Decision` 是因为它小且是列表的主信息列。这是「按视图裁剪字段」的典型可观测性工程取舍。

---

## 5. 综合实践

把本讲三块数学串起来，完成一次端到端的投影追踪推演。**目标**：给定信号命中，手算出一份完整的 `Trace` JSON，并解释它如何驱动最终路由。

**输入条件**（基于 balance 配方）：

- 信号命中：`keyword:reasoning_request_markers`（置信度 0.9）、`context:long_context`（命中、无显式置信度）。
- 相关投影：
  - score `difficulty_score`，inputs：`reasoning_request_markers`（keyword，confidence，0.6）、`long_context`（context，binary，0.2）。
  - mapping `difficulty_band`，slope=10，带：simple(`lt 0.18`) / medium(`gte 0.18 lt 0.48`) / complex(`gte 0.48 lt 0.82`) / reasoning(`gte 0.82`)。

**任务**：

1. 算 `difficulty_score` 总分与每路贡献，写出 `ScoreBreakdown`。
2. 判定命中的带，算各带 boundary_distance 与选中带的 sigmoid 置信度，写出 `MappingDecision`。
3. 用一句话回答：这条追踪被写出后，决策层的某条 ROUTE 若声明了 `WHEN projection("balance_complex")`，它的匹配置信度会取自哪个键、约等于多少？

**参考答案**：

1. 总分 \(= 0.6 \times 0.9 + 0.2 \times 1.0 = 0.74\)。`reasoning_request_markers` 贡献 +0.54、`long_context` 贡献 +0.20。`long_context` 走 binary、命中取默认 `Match=1.0`（也可视为 confidence 缺省 1.0，结果相同）。
2. 0.74 落在 `[0.48, 0.82)` → `balance_complex` 命中；boundary_distance = min(0.26, 0.08) = 0.08；confidence \(\approx 0.69`。各带 boundary_distance：simple 0.56 / medium 0.26 / complex 0.08 / reasoning 0.08。
3. 决策引擎读 `SignalConfidences["projection:balance_complex"]`（由 [classifier_projection_outputs.go:32-40](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_projection_outputs.go#L32-L40) 的 `recordProjectionMatch` 写回），约等于 0.69。这也呼应了 u2-l3 的结论：mapping 产出的命名带以 `"projection:名字"` 反写进置信度表，决策层的 WHEN 正据此写规则。

> 进阶：把 `long_context` 的 `value_source` 改成 `raw` 并假设其 `SignalValues` 缺该键，重算总分（应变成 0.54，落到 `balance_medium` 段），体会 binary 与 raw 在「缺失」语义上的差异（前者命中即满、后者缺失即 0）。

## 6. 本讲小结

- **版本化纯数据 schema**：`projectiontrace` 是只有一个 `trace.go` 的纯类型包，`Trace` 由 `SchemaVersion`（常量 `"1"`）打头，下挂 `partitions`/`scores`/`mappings` 三个 `omitempty` 数组，被分类、重放、面板三处复用而不引入循环依赖。
- **分区追踪（PartitionResolution）**：记录 softmax/argmax 选出的赢家、原始分 vs 归一化分（`RawWinnerScore`/`WinnerScore`）、首两名边际（`Margin`）、以及是否走了 default 兜底（`DefaultUsed`）；只在「真正竞争（≥2 候选）」或「兜底」时才记录。
- **评分追踪（ScoreBreakdown）**：把 `weighted_sum` 的每路输入的权重、取值、贡献逐一存档；取值有 binary/confidence/raw/kb_metric 四种来源，`confidence` 缺省兜底 1.0、`raw` 缺省为 0 且允许负值；追踪**重新取值**而非读现成总分，以保证分解与实际求值同源。
- **映射追踪（MappingDecision）**：逐带记录阈值匹配（GT/GTE/LT/LTE 全满足）与边界距离（到活跃边界最近距离），选中首条命中带，并用 `sigmoid_distance`（`1/(1+exp(-slope·d))`）把边界距离翻译成置信度；贴边界 ≈ 0.5（犹豫）、远离边界 ≈ 1（笃定）。
- **三段合并写入**：分区追踪在分区规约时逐条 append，评分与映射追踪在 `applyProjections` 收尾时由 `mergeProjectionTrace` 一次性补齐并保留已有分区条目。
- **可解释复用链路**：`SignalResults.ProjectionTrace` →（深拷贝）→ `RequestContext.VSRProjectionTrace` →（深拷贝）→ `Record.ProjectionTrace` → Postgres/JSON 落盘 → 面板；列表摘要主动清空追踪以控体积。两处深拷贝用 Marshal/Unmarshal 彻底断开指针引用。

## 7. 下一步学习建议

- **横向对照另一份可解释文档**：阅读 u11-l1（API Server 管理 API）里的 eval/classify 能力端点，它们会把 `EvaluateAllSignals=true` 的信号结果（含 `ProjectionTrace`）序列化出来，是这份追踪除了重放之外的另一条对外出口。
- **追溯写入侧的调用时机**：回到 u5-l2（决策求值管线）与 u8-l1（分类编排），确认 `applyProjections` 在 `evaluateAllSignalsWithContext` 里的位置——投影追踪是在所有信号算完、写回置信度的同一个临界区内产出的。
- **看一份真实落盘记录**：结合 u10-l1（插件链）里的 `router_replay` 插件，理解 `recorder.go` 在请求/响应两阶段如何闭合一条含 `ProjectionTrace` 的完整 `RoutingRecord`，把本讲的「字段」变成「一条可在面板点开的审计记录」。
- **如果要做面板可视化**：`projectiontrace.Trace` 的三段结构天然对应三种图——分区的「赢家 vs 边际」条形图、评分的「贡献瀑布图」、映射的「分数轴 + 命中带 + 边界」刻度图。可参照 u13（管理面板）单元尝试用这三段数据各渲染一种视图。
