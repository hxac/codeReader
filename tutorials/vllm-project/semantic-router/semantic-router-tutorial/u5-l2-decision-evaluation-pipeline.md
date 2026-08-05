# 决策求值管线：信号→决策

## 1. 本讲目标

本讲是「请求处理主链路」的第二篇，承接 u5-l1。u5-l1 解决了「请求体字节如何被解析为可路由状态、并选定配方」，本讲要回答这条链路最核心的一问：

> **当一个请求带着它的对话历史来到路由器，SR 是如何把它变成「走哪条路由（decision）」「匹配置信度多少」「最终选哪个后端模型」的？**

读完本讲，你应当能够：

- 说出 `performDecisionEvaluation` 这个内核函数的五个子步骤，以及它返回的五元组各自的含义；
- 解释「信号准备」如何把协议无关的对话历史打包成分类器能消费的输入，并理解其中的压缩裁剪；
- 解释「信号求值」如何抽取全部信号、把信号写回 `RequestContext`，并通过 `evaluateDecisionInternal` 桥接到决策引擎；
- 解释「决策引擎调用」中 `EvaluateDecisionsWithSignals` 的输入 `SignalMatches` 与输出 `DecisionResult`，以及 AND/OR/NOT 的置信度聚合规则；
- 解释「方法解析」如何把 YAML 里的 `algorithm.type` 字符串一步步解析成一个具体的选择器（Selector）实例。

本讲只覆盖到「选中决策与模型」为止；信号族本身的内部实现留待 u8（分类信号系统），选择算法（Elo/Hybrid 等）的数学留待 u6-l2。

## 2. 前置知识

在进入源码前，先统一三个在本讲会反复出现的术语：

- **决策（Decision）**：一条带名字的路由规则，对应 DSL 里的一个 `ROUTE` 块。它持有一棵布尔规则树（`Rules`）、一组候选模型（`ModelRefs`）、可选的优先级（`Priority`）/分层（`Tier`）以及选择算法配置（`Algorithm`）。详见 u2-l4。
- **信号（Signal）**：从一次请求里抽取出来的「事实」，例如「命中了关键词 `code`」「检测到域 `health`」「越狱置信度 0.92」。每条信号带一个 0~1 的置信度。详见 u2-l2。
- **信号匹配集（SignalMatches）**：决策引擎真正消费的输入容器——把所有信号族命中结果（`KeywordRules`、`DomainRules`……）连同置信度表 `SignalConfidences` 打包到一起。

再用一句话回顾 u2-l1 建立的心智模型：一次请求走的是 **信号 → 投影 → 决策 → 模型 + 插件** 的流水线。投影层（u2-l3）的输出也会作为一种「派生信号」（`ProjectionRules`）写回信号集合，因此从决策引擎的视角看，**信号与投影是同一类输入**，都装在 `SignalMatches` 里。本讲的「决策求值」就是这条流水线里「信号已经备好、现在要拿它们去匹配决策」的那一段。

最后提醒一个边界（u5-l1 已建立）：只有「面向路由的模型名」（`auto` / entrypoint 虚拟名）才会真正跑决策求值；具体后端模型名走 passthrough，**不继承**配方的信号、决策与插件。这个边界在本讲的多个早退分支里会再次出现。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/semantic-router/pkg/extproc/req_filter_classification.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification.go) | 内核编排者：`performDecisionEvaluation` 把五个子步骤串起来；同时承载方法解析的 `getSelectionMethod`、`selectorForDecisionMethod` |
| [src/semantic-router/pkg/extproc/req_filter_classification_signal.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_signal.go) | 信号准备：`prepareSignalEvaluationInput`、`applySignalResultsToContext`、可选的 prompt 压缩 |
| [src/semantic-router/pkg/extproc/req_filter_classification_runtime.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_runtime.go) | 信号求值与决策引擎调用：`evaluateSignalsForDecision`、`runDecisionEngine`、`finalizeDecisionEvaluation`，以及方法字符串→枚举的映射表 |
| [src/semantic-router/pkg/decision/engine.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go) | 决策引擎本体：`EvaluateDecisionsWithSignals`、规则树递归求值 `evalNode/evalAND/evalOR/evalNOT`、最佳决策选择 `selectBestDecision` |
| [src/semantic-router/pkg/classification/classifier_signal_decision.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_decision.go) | 信号→决策桥接：`evaluateDecisionInternal` 把 `SignalResults` 逐字段搬进 `SignalMatches`，并现场构造一个 `DecisionEngine` |
| [src/semantic-router/pkg/extproc/req_filter_entrypoint.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_entrypoint.go) | 候选决策来源：`decisionCandidatesForRequest` 从当前配方画像取出本请求要参与求值的决策集合 |

> 阅读建议：本讲按「先看编排者 `performDecisionEvaluation` 的骨架，再逐个钻进五个子步骤」的顺序最省力。决策引擎 `decision/engine.go` 是少数可以脱离请求上下文、当作纯函数库来读的代码，适合重点精读。

## 4. 核心概念与源码讲解

### 4.1 performDecisionEvaluation 全景：请求链路的内核

#### 4.1.1 概念说明

`performDecisionEvaluation` 是请求处理主链路的内核。它的职责只有一句话：**接收 u5-l1 交来的「原始模型名 + 协议无关的对话历史 + 请求上下文」，产出「决策名、匹配置信度、推理决策、选中的模型、错误」**。它本身不做信号抽取、也不做规则求值，而是像一个「五幕剧的舞台监督」，把每个子步骤按固定顺序调度起来，并在每一幕之间做边界检查与早退。

它的返回签名就浓缩了整条管线的产物：

```go
func (r *OpenAIRouter) performDecisionEvaluation(
    originalModel string, history signalConversationHistory, ctx *RequestContext,
) (string, float64, entropy.ReasoningDecision, string, error)
//     ^decisionName   ^confidence   ^reasoningDecision        ^selectedModel  ^err
```

四个返回值对应「走哪条路由」「多确定」「要不要开推理模式」「最终模型是谁」。注意 `selectedModel` 可以为空——当客户端指定了具体后端模型（passthrough）时，决策求值只定决策名，不替换模型。

#### 4.1.2 核心流程

整个函数是一段线性的「检查 → 准备 → 求值 → 调引擎 → 收尾」流程，伪代码如下：

```
performDecisionEvaluation(originalModel, history, ctx):
  ① 内容检查：history 既无文本也无 envelope 路由事实 → 直接返回空（不路由）
  ② 入口解析：若 ctx.Routing 未解析 → resolveEntrypointForRequest；若没有选中配方 → 返回空
  ③ 配置检查：若全局没有任何 decisions → 走默认模型 / passthrough 早退
  ④ 信号准备：prepareSignalEvaluationInput(history) → signalInput
  ⑤ 候选决策：decisionCandidatesForRequest(...) → candidates（本请求参与求值的决策子集）
  ⑥ 信号求值：evaluateSignalsForDecision(...) → signals（SignalResults），并写回 ctx
  ⑦ 决策引擎：runDecisionEngine(...) → result（DecisionResult）
  ⑧ 收尾选模型：finalizeDecisionEvaluation(result, ...) → (decisionName, confidence, reasoning, selectedModel)
```

前 ③ 步是「早退护栏」，保证后续步骤拿到的输入永远非空、且确实需要路由；④~⑧ 是真正的五幕管线。下面四个小节（4.2~4.5）分别展开 ④⑤、⑥、⑦、⑧ 中的方法解析部分。

#### 4.1.3 源码精读

先看编排者本身的骨架与三道早退护栏：

[req_filter_classification.go:L22-L52](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification.go#L22-L52) —— 函数签名与前三道护栏：内容检查、入口解析（幂等 guard）、`HasRoutingDecisions()` 配置检查。注意第 37~39 行的注释强调：哪怕某些「聚焦调用者」绕过了正常的预路由阶段，这里也会幂等地补做入口解析，让隔离边界**不依赖调用顺序**。

接着是五幕管线的核心调度，一气呵成、顺序不可调换：

[req_filter_classification.go:L54-L79](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification.go#L54-L79) —— 依次调用 `prepareSignalEvaluationInput`（L54）、`decisionCandidatesForRequest`（L61）、`evaluateSignalsForDecision`（L62）、`runDecisionEngine`（L67）、`finalizeDecisionEvaluation`（L72）。注意 L62 的信号求值可能返回 `authzErr`（授权失败），它会被原样上抛——调用方据此返回 HTTP 403。

那么这个内核被谁调用？入口在请求体预路由阶段：

[processor_req_body_prepare.go:L101-L121](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_req_body_prepare.go#L101-L121) —— `runRequestPreRoutingStages` 先做入口解析，再把 u5-l1 产出的 `history` 交给 `performDecisionEvaluation`（L109）。返回的错误被分成两类：请求取消（499）与授权失败（403）。

#### 4.1.4 代码实践

**实践目标**：在源码里定位 `performDecisionEvaluation` 的主体，把它的关键子步骤摘出来，建立「读骨架」的肌肉记忆。

**操作步骤**：

1. 打开 [req_filter_classification.go:L22-L79](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification.go#L22-L79)。
2. 在函数体内找到第 54、61、62、67、72 行那五个方法调用，分别记下它们的名字。
3. 用 `git grep -n "func (r \*OpenAIRouter) <方法名>"` 找到每个子函数定义所在的文件（你会看到它们分布在 `req_filter_classification_signal.go` 与 `req_filter_classification_runtime.go` 两个文件里）。

**需要观察的现象**：五个子步骤分别落在不同文件，但都挂在同一个 `*OpenAIRouter` 接收者上——这是 SR 在 extproc 包内常用的「按职责拆文件、共享接收者」组织方式。

**预期结果**：你能画出一张「调用名 → 文件 → 职责」的小表，例如 `prepareSignalEvaluationInput → _signal.go → 信号准备`。

> 说明：本实践是「源码阅读型实践」，不需要运行服务；如果你想在本地真正跑一次决策求值，可改读本讲 4.4.4 给出的单元测试。

#### 4.1.5 小练习与答案

**练习 1**：函数返回的五个值里，哪两个在「passthrough（客户端指定具体后端模型）」场景下会是空值？
**答案**：`selectedModel` 为空（不替换模型），且若该配方无 decisions 配置，`decisionName` 也为空。决策名即便非空，模型也由客户端原值决定。

**练习 2**：为什么第 37~39 行要在函数内部再做一次 `resolveEntrypointForRequest`？u5-l1 不是已经解析过了吗？
**答案**：因为这是一个**幂等 guard**。该函数也可能被「聚焦调用者」（focused callers，例如某些 envelope 路由路径）在没有经过正常预路由阶段时直接调用；内部补做一次解析，保证「配方隔离边界不依赖调用顺序」，避免出现 nil 歧义。

---

### 4.2 信号准备：prepareSignalEvaluationInput

#### 4.2.1 概念说明

「信号准备」是一道**搬运 + 可选压缩**的解耦层。u5-l1 产出的 `signalConversationHistory` 是协议无关的对话历史（当前用户消息、历史非用户消息、各类计数、元数据等），但分类器 `classification` 包想要的输入是另一个形状——`signalEvaluationInput`。`prepareSignalEvaluationInput` 的职责就是把这份数据搬运并整理成分类器期望的形状，顺带做一次「文本太长就先压缩」的裁剪。

为什么要单独一道工序？因为信号抽取（下一步）是整条管线里**最贵的一步**（要跑嵌入、分类器），如果用户消息很长，先做无 LLM 的 prompt 压缩（u10-l2）能显著降低后续算力开销；而压缩与否、压成什么样子，属于「输入整形」，不应混进分类器内部。

#### 4.2.2 核心流程

```
prepareSignalEvaluationInput(history):
  ① 把 history 的字段逐一搬进 signalEvaluationInput：
     - evaluationText / currentUserText ← history.currentUserMessage
     - allMessagesText ← history.nonUserMessages（必要时拼接当前消息）
     - conversationFacts ← 各类消息计数（user/assistant/system/tool/图片…）
     - requestFacts.Metadata ← history.metadata（克隆，避免共享引用）
  ② 若 currentUserMessage 为空但有 nonUserMessages，用后者兜底当 evaluationText
  ③ 若 evaluationText 仍为空 → 直接返回（后面什么都不做）
  ④ compressSignalEvaluationText(evaluationText)：
     - 若未启用压缩 / 文本短于阈值 → 原样返回
     - 否则跑 promptcompression.Compress，得到 compressedText 和 skipCompressionSignals
```

关键产出有两个文本：`evaluationText`（原始、未压缩，给那些「不能压」的信号用）和 `compressedText`（压缩后，给大多数信号用）。`skipCompressionSignals` 记录哪些信号族必须用未压缩文本（例如越狱检测通常需要原文）。

#### 4.2.3 源码精读

先看搬运目标的结构体——它就是分类器输入的「容器」：

[req_filter_classification_signal.go:L14-L24](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_signal.go#L14-L24) —— `signalEvaluationInput`。注意它把「会话形状事实」（`conversationFacts`）和「请求事实」（`requestFacts`）分开携带，前者描述对话结构，后者携带请求级元数据。

再看搬运与压缩主体：

[req_filter_classification_signal.go:L26-L73](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_signal.go#L26-L73) —— `prepareSignalEvaluationInput`。重点看 L34~L51 的字段搬运（`conversationFacts` 把 `history` 里的 `hasDeveloperMessage`、`userMessageCount`、`assistantToolCallCount` 等逐个搬过去）和 L71 的压缩调用 `compressSignalEvaluationText`。

压缩决策的细节在这里：

[req_filter_classification_signal.go:L75-L97](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_signal.go#L75-L97) —— `compressSignalEvaluationText`。三个跳过条件（未启用、短于 `min_length`、token 数本来就低于 `max_tokens`）任一满足就原样返回；否则才真正调用 `promptcompression.Compress`。

#### 4.2.4 代码实践

**实践目标**：理清「两个文本」的来源，验证压缩是可选且保守的。

**操作步骤**：

1. 阅读 [req_filter_classification_signal.go:L75-L97](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_signal.go#L75-L97)。
2. 回答：在「`PromptCompression.Enabled == false`」和「文本 token 数 ≤ `MaxTokens`」两种情况下，`compressedText` 与 `evaluationText` 是否相等？
3. 进一步追踪 `skipCompressionSignals` 的去向：它在 [req_filter_classification_runtime.go:L57](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_runtime.go#L57) 被塞进 `SignalEvaluationInput.SkipCompressionSignals`，最终告诉分类器「这些信号请用 `UncompressedText`（L55）而不是压缩文本」。

**预期结果**：两种跳过条件下 `compressedText == evaluationText`（函数直接返回原文本，`skipCompressionSignals` 为 nil）。`evaluationText` 永远是原文，是给「不能压」的信号的保底输入。

> 待本地验证：若你想看压缩实际生效，需要在 config 里启用 `prompt_compression` 并喂一段超过 `max_tokens` 的长文本。

#### 4.2.5 小练习与答案

**练习 1**：`conversationFacts` 与 `requestFacts` 各自携带什么？为什么要分两个结构体？
**答案**：`conversationFacts` 携带对话**结构**事实（各类消息计数、是否有 developer message、是否有 tool result），`requestFacts` 携带请求**级**事实（目前主要是 `Metadata`，即请求附带的非信任元数据）。分开是因为前者驱动「会话形状」类信号（如 `conversation`、`structure`），后者驱动 `metadata` 信号，职责清晰、便于独立演进。

**练习 2**：为什么 `requestFacts.Metadata` 要用 `cloneRoutingMetadata` 克隆，而不是直接赋值？
**答案**：为了避免下游分类器与上游共享同一个 map 引用、互相意外改写。这是 SR 在跨包传递可变集合时常见的防御性克隆约定。

---

### 4.3 信号求值：evaluateSignalsForDecision 与信号→决策桥接

#### 4.3.1 概念说明

「信号求值」是整条管线里**最重**的一步：它真的去跑嵌入、跑分类器，把一次请求变成一兜子信号命中结果 `SignalResults`。这一步由分类器 `classification.Classifier` 主导（其内部实现是 u8 的主题），本讲只关注 extproc 这一层如何**调用**它、如何**接收**结果、以及如何把结果**桥接**给决策引擎。

桥接是一个关键设计：分类器产出的 `classification.SignalResults` 和决策引擎消费的 `decision.SignalMatches` 是**两个不同的类型**（分属两个包）。之所以不共用一个类型，是为了让 `decision` 包不反向依赖 `classification` 包——决策引擎只认 `SignalMatches` 这个纯数据容器，至于这些信号是谁算出来的，它不关心。于是中间就需要一道「逐字段搬运」的桥接函数。

#### 4.3.2 核心流程

```
evaluateSignalsForDecision(originalModel, signalInput, nonUserMessages, ctx, candidates):
  ① classifierForRequest(ctx) → 取当前配方对应的分类器（若为空 → 返回 error）
  ② classifier.EvaluateAllSignalsWithHeaders(SignalEvaluationInput{...}) → signals (SignalResults)
       └─ 若返回 authzErr → 上抛（最终导致 HTTP 403）
  ③ applySignalResultsToContext(ctx, signals) → 把 22+ 个信号族命中结果写回 RequestContext
  ④ 记录 token 计数、打日志、结束 trace span
  ⑤ 返回 signals

—— 随后 runDecisionEngine 内部 ——
  ⑥ classifier.EvaluateDecisionWithEngineForDecisions(signals, candidates)
       └─ evaluateDecisionInternal：把 SignalResults 逐字段搬进 SignalMatches，构造 DecisionEngine 并求值
```

第 ③ 步「写回 ctx」很重要：信号结果不止喂给决策引擎，还会被**响应阶段、插件链、重放（replay）、投影追踪**等后续环节复用，所以必须落到 `RequestContext` 这个请求级状态载体上（u4-l3 已介绍 `RequestContext`）。

#### 4.3.3 源码精读

先看 extproc 这一层如何调用分类器并接收结果：

[req_filter_classification_runtime.go:L31-L59](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_runtime.go#L31-L59) —— `evaluateSignalsForDecision`。L41 取分类器；L46 调 `EvaluateAllSignalsWithHeaders`，把上一步准备的 `compressedText`/`allMessagesText`/`conversationFacts`/`headers` 等全部塞进 `SignalEvaluationInput`；L60~L68 处理授权错误。

再看「写回 ctx」这道关键工序——它把 22 个信号族的命中结果一一映射到 `ctx.VSR*` 字段：

[req_filter_classification_signal.go:L99-L140](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_signal.go#L99-L140) —— `applySignalResultsToContext`。注意 L122~L126 对 `ProjectionScores`、`SignalConfidences`、`SignalValues`、`SignalErrors`、`ProjectionTrace` 都做了**克隆**（`cloneReplayFloat64Map` 等），同样是为了重放安全、避免共享引用。

最后看「信号→决策桥接」本体——这是理解整条管线最重要的一段代码，因为它把两个包的类型接到了一起：

[classifier_signal_decision.go:L39-L89](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_decision.go#L39-L89) —— `evaluateDecisionInternal`。L40~L43 决定求值「全量决策」还是「候选子集」（即上一步传进来的 `candidates`）；L56~L62 **现场 new 一个 `DecisionEngine`**（每次请求都新建，无状态）；L64~L89 把 `SignalResults` 逐字段搬进 `decision.SignalMatches`——你会看到 `signals.MatchedKeywordRules → sm.KeywordRules`、`signals.SignalConfidences → sm.SignalConfidences` 等一一对应的映射。

> 这段桥接也解释了「投影为何能参与决策」：投影层产出的命名路由带会被写进 `signals.MatchedProjectionRules`，再经此处映射成 `sm.ProjectionRules`，于是决策规则里的 `projection("balance_complex")` 就能命中。投影与信号在决策引擎眼里合流为一。

#### 4.3.4 代码实践

**实践目标**：验证「写回 ctx」覆盖了多少个信号族，并理清 `SignalResults → SignalMatches` 的字段对应关系。

**操作步骤**：

1. 在 [req_filter_classification_signal.go:L99-L140](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_signal.go#L99-L140) 里数一数有多少行形如 `ctx.VSRMatched* = signals.Matched*Rules`。
2. 在 [classifier_signal_decision.go:L64-L89](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_decision.go#L64-L89) 里，把左侧 `sm.*` 字段名和右侧 `signals.*` 来源列成一张对照表。
3. 检查对照表里有没有「两侧都出现、但名字略不同」的字段（提示：投影、置信度、原始值）。

**预期结果**：写回 ctx 的信号族在 20 个以上；桥接表里 `sm.ProjectionRules ← signals.MatchedProjectionRules`、`sm.SignalConfidences ← signals.SignalConfidences`、`sm.SignalValues ← signals.SignalValues` 都是一一对应，证明投影与信号统一进入决策引擎。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `decision` 包不直接接收 `classification.SignalResults`，而要中间搬一次？
**答案**：为了**解除包依赖**。`decision` 是一个纯规则求值库，若它依赖 `classification`，就会把一整套分类器实现拖进自己的依赖图。引入 `SignalMatches` 这个纯数据容器后，`decision` 只依赖 `config`，可以被独立测试与复用。

**练习 2**：`evaluateSignalsForDecision` 返回的 `authzErr` 最终会变成什么 HTTP 状态码？依据是哪段代码？
**答案**：403。依据是 [processor_req_body_prepare.go:L119-L121](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_req_body_prepare.go#L119-L121)：调用方收到非取消类错误时 `createErrorResponse(403, decisionErr.Error())`。

---

### 4.4 决策引擎调用：runDecisionEngine → EvaluateDecisionsWithSignals

#### 4.4.1 概念说明

决策引擎（`decision.DecisionEngine`）是整条管线里**最像纯函数**的一环：输入 `SignalMatches`（信号命中集）+ 一组决策定义，输出零个或一个 `DecisionResult`（最佳匹配决策）。它不碰网络、不碰文件、不持有可变状态——`evaluateDecisionInternal` 每次请求都 `new` 一个新引擎。

`runDecisionEngine` 是 extproc 包对引擎的**薄封装**：它负责取分类器、取策略、选「全量求值」还是「候选子集求值」、处理错误与 trace，然后把引擎的纯结果返回上去。真正的规则求值逻辑全部在 `decision/engine.go` 里。

#### 4.4.2 核心流程

决策引擎内部对一个决策的求值，是**对一棵布尔规则树的递归求值**：

```
EvaluateDecisionsWithSignals(signals):
  对每一条 decision：
    evaluateDecisionWithSignals(decision, signals):
      若 decision.Rules 为空（IsEmpty）→ 无条件命中（这是 DSL 里「没有 WHEN 的兜底路由」）
      否则 → evalNode(decision.Rules, signals)  ← 递归求值规则树
    若命中 → 收集 (decision, confidence, matchedRules)
  若一个都没命中 → 返回 (nil, nil)
  否则 → selectBestDecision(results)  ← 按策略排序取第一名
```

`evalNode` 的递归规则：

- **叶子节点**（`IsLeaf()`，即 `Type != ""`）：查「这个信号族里有没有这个名字的命中」，命中则置信度取 `SignalConfidences["族:名"]`，缺失则兜底 1.0。
- **AND**：所有子节点都命中才命中；置信度 = 子节点置信度的**算术平均**。
- **OR**：任一子节点命中即命中；置信度 = 命中子节点里的**最大值**。
- **NOT**：严格一元；子节点不命中时 NOT 命中且置信度 1.0，子节点命中时 NOT 不命中。

最佳决策的选择（`selectBestDecision`）有三种排序策略，下一小节用公式说明。

#### 4.4.3 源码精读

先看 extproc 的封装层——注意它对「候选为空」与「引擎返回 nil」都做了兜底：

[req_filter_classification_runtime.go:L138-L191](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_runtime.go#L138-L191) —— `runDecisionEngine`。L162~L171 是分叉点：传了 `candidates` 就调 `EvaluateDecisionWithEngineForDecisions`（只求值候选子集），否则调 `EvaluateDecisionWithEngine`（求值配方全量决策）。L182~L186 处理「没有任何决策命中」——返回默认模型。

进入引擎本体。先看输入输出类型：

[engine.go:L71-L106](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L71-L106) —— `SignalMatches`（输入，22 个信号族命中切片 + 三张 map）与 `DecisionResult`（输出，指向命中的 `Decision`、置信度、匹配规则）。

主入口与遍历：

[engine.go:L128-L166](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L128-L166) —— `EvaluateDecisionsWithSignals`。L143~L157 遍历每条决策求值，命中则记录并打指标 `RecordDecisionMatch`；L159~L162 没有命中则返回 `(nil, nil)`；L165 调 `selectBestDecision`。

规则树递归分发：

[engine.go:L185-L201](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L185-L201) —— `evalNode`。叶子走 `evalLeaf`，组合节点按 `Operator` 分发到 AND / NOT / 默认 OR。

三个布尔算子：

[engine.go:L402-L419](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L402-L419) —— `evalAND`：任一子不匹配即整体不匹配；置信度取平均。

[engine.go:L422-L440](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L422-L440) —— `evalOR`：遍历所有子节点，保留置信度最高的那个命中。

[engine.go:L444-L459](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L444-L459) —— `evalNOT`：严格要求恰好 1 个子节点（多了少了都视为不匹配并告警）。

叶子求值与置信度兜底：

[engine.go:L204-L223](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L204-L223) —— `evalLeaf`。L221 取置信度；兜底逻辑在 [engine.go:L386-L396](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L386-L396) 的 `signalConfidence`：表里没有就返回 1.0。

最佳决策选择与排序：

[engine.go:L486-L547](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L486-L547) —— `selectBestDecision` 与 `decisionResultLess`。是否启用「分层选择」由 [engine.go:L503-L510](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L503-L510) 的 `useTieredSelection` 决定：只要任意命中决策标了 `Tier > 0`，就进入分层模式。

**置信度聚合的数学表达**。设一条决策的规则树根节点有 \(n\) 个子树，第 \(i\) 个子树命中且置信度为 \(c_i\)：

- AND 命中时（所有子都命中）：

\[
\text{confidence}_{\text{AND}} = \frac{1}{n}\sum_{i=1}^{n} c_i
\]

- OR 命中时（至少一个子命中）：

\[
\text{confidence}_{\text{OR}} = \max_{i\,:\,\text{matched}} c_i
\]

- NOT（单子 \(c\)）：子不命中 → 置信度 1.0；子命中 → 整体不命中。

**最佳决策的排序键**（`decisionResultLess`，每行是 tie-break，越靠前优先级越高）：

| 模式 | 主键 | 次键 | 再次键 | 末键 |
| --- | --- | --- | --- | --- |
| 分层（任意命中含 `Tier>0`） | `Tier` 升序 | `Confidence` 降序 | `Priority` 降序 | `Name` 升序 |
| `strategy=confidence` | `Confidence` 降序 | `Priority` 降序 | `Name` 升序 | — |
| `strategy=priority`（默认） | `Priority` 降序 | `Confidence` 降序 | `Name` 升序 | — |

> 这与 u2-l4 讲的「TIER 为主、PRIORITY 微调」完全对应：一旦出现分层，TIER 升序压过一切；否则按配方的 `strategy` 在「置信度优先」与「优先级优先」之间二选一。`Name` 升序作为最终 tie-break，保证排序是**确定性**的（同样的输入永远选出同一个决策）。

#### 4.4.4 代码实践

**实践目标**：用 `decision/engine.go` 说清楚 `EvaluateDecisionsWithSignals` 的输入输出，并用单元测试验证你的理解。

**操作步骤**：

1. 复述输入输出：输入是 `*SignalMatches`（一个纯数据容器，见 [engine.go:L71-L98](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L71-L98)），输出是 `(*DecisionResult, error)`（见 [engine.go:L100-L106](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L100-L106)）。两个 nil 语义不同：返回 `(nil, nil)` 表示「没有决策命中」（正常兜底场景）；返回 `(nil, err)` 表示「配置错误」（如 `no decisions configured`）。
2. 打开测试 [req_filter_entrypoint_test.go:L244-L261](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_entrypoint_test.go#L244-L261)，看它如何构造 `signalConversationHistory{currentUserMessage: ...}` 喂给 `performDecisionEvaluation`，并断言 `decisionName` / `selectedModel` / `ctx.VSRSelectedDecision.Name`。
3. 运行这个测试：`go test ./src/semantic-router/pkg/extproc/ -run TestPerformDecisionEvaluation -v`（测试名以文件内实际函数名为准，可先 `grep -n "^func Test" req_filter_entrypoint_test.go` 找到准确名字）。

**需要观察的现象**：测试里名为 `explicit model preserves the client selection` 的用例（[L228-L233](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_entrypoint_test.go#L228-L233)）期望 `wantDecision: ""`、`wantModel: ""`——这正是 passthrough 边界：指定具体模型时既不产决策也不换模型。

**预期结果**：测试通过；你能用自己的话讲清「`(nil,nil)` 与 `(nil,err)` 的区别」以及「passthrough 用例为何两个返回值都为空」。

> 待本地验证：测试的确切名字以你 grep 出的结果为准；若环境未配置 Go，至少完成步骤 1~2 的阅读部分。

#### 4.4.5 小练习与答案

**练习 1**：一条决策的规则是 `AND(keyword:code, NOT(domain:health))`。给定信号 `KeywordRules=["code"]`、`DomainRules=["health"]`，求值结果与置信度是什么？
**答案**：`keyword:code` 命中，置信度兜底 1.0；`domain:health` 命中，故 `NOT(domain:health)` 不命中；AND 要求所有子命中，所以整体**不命中**，置信度 0。

**练习 2**：若两条决策都命中，且配方 `strategy=priority`，它们的 `Priority` 也相同，最终由什么决定胜者？
**答案**：依次比较 `Priority`（降序，相同）→ `Confidence`（降序）；若仍相同，则 `Name` 字典序升序小的胜出（确定性 tie-break）。

**练习 3**：为什么 `evaluateDecisionInternal` 每次请求都 `NewDecisionEngine` 而不复用一个全局引擎？
**答案**：因为引擎持有的 `decisions`/`strategy`/`routingScope` 是**按请求的配方画像**变化的（不同 recipe 有不同决策集，`candidates` 还可能是子集）。引擎本身无状态、构造极廉价，每次新建既避免并发共享，又天然支持配方隔离。

---

### 4.5 方法解析：从 algorithm.type 到 Selector

#### 4.5.1 概念说明

决策引擎选出「走哪条路由」之后，`finalizeDecisionEvaluation` 还要做最后一件事：**在这条路由的候选模型里，用它的选择算法挑出一个具体模型**。这就引出本讲最后一个最小模块——「方法解析」。

方法解析要跨过**三道表示鸿沟**，每一层的类型都不同：

1. **YAML 字符串**：`decision.algorithm.type`，例如 `"elo"`、`"hybrid"`、`"automix"`、`"static"`。这是配置层的人类可读名字。
2. **枚举常量**：`selection.SelectionMethod`（如 `MethodElo`、`MethodHybrid`）。这是 selection 包内部的强类型标识。
3. **具体实例**：`selection.Selector` 接口的某个实现（如 `*EloSelector`、`*HybridSelector`）。这是真正干活的算法对象。

`getSelectionMethod` 负责第 1→2 步（字符串查表），`selectorForDecisionMethod` 负责第 2→3 步（按方法解析实例）。理解这两步，你就能回答「我在 YAML 里写 `algorithm.type: hybrid`，最终是哪个对象在算」。

#### 4.5.2 核心流程

```
finalizeDecisionEvaluation → selectDecisionRuntimeModel → selectModelFromCandidates
  ① getSelectionMethod(algorithm):
       algorithm.Type (字符串) → selectionMethodByAlgorithmType[type] → SelectionMethod
       查不到 → MethodStatic（默认静态选择）
  ② selectorForDecisionMethod(method, algorithm, ctx):
       MethodHybrid  → newDecisionHybridSelector（需要 HybridConfig）
       MethodMultiFactor → newDecisionMultiFactorSelector
       MethodPrompt  → newDecisionPromptSelector
       其他 → modelSelectorForRequest(ctx) 拿到 Registry，再 registry.Get(method)
  ③ selector.Select(...) → SelectionResult.SelectedModel
```

注意三个「需要额外配置」的方法（Hybrid / MultiFactor / Prompt）走了**特例分支**：它们不能简单地从 Registry 取，因为它们需要本决策 `algorithm` 里携带的子配置（如 `Hybrid.CostWeight`）。其余方法（Elo、RouterDC、AutoMix、Static、KNN……）一律从 Registry 查。

#### 4.5.3 源码精读

字符串→枚举的映射表（注意它定义在 runtime 文件里）：

[req_filter_classification_runtime.go:L17-L29](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_runtime.go#L17-L29) —— `selectionMethodByAlgorithmType`。11 种算法类型的字符串到枚举的映射。

第 1→2 步：

[req_filter_classification.go:L403-L410](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification.go#L403-L410) —— `getSelectionMethod`。配置为空或查不到时回落 `MethodStatic`。

第 2→3 步：

[req_filter_classification.go:L81-L98](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification.go#L81-L98) —— `selectorForDecisionMethod`。三个特例分支 + 末尾的 Registry 兜底（L92~L97）。Registry 的选取也按配方隔离：[req_filter_classification.go:L100-L111](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification.go#L100-L111) 的 `modelSelectorForRequest` 在有命名配方时优先用 `RecipeModelSelectors[recipe]`。

最后看调用方如何把这一切串起来：

[req_filter_classification_selector.go:L38-L77](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_selector.go#L38-L77) —— `selectModelFromCandidates`。L38 先解析方法；L52 单候选快速路径；L56 解析 selector 实例；L57~L69 selector 为空时走 fallback；L70 真正调用 `selectWithSelector` → `selector.Select(...)`。

候选决策的来源（顺带补全 4.1 里 `decisionCandidatesForRequest` 的实现）：

[req_filter_entrypoint.go:L62-L77](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_entrypoint.go#L62-L77) —— `decisionCandidatesForRequest`。正常情况返回当前配方画像的 `recipe.Profile.Decisions`；特殊模型名（ReMoM/Fusion/Flow）走另一套候选；值得注意的是 L68~L73：配方无 decisions 时返回**空非 nil 切片**，注释解释这是为了让 `runDecisionEngine` 不回落到默认配方的决策——配方隔离的边界在这里再次被严格守住。

#### 4.5.4 代码实践

**实践目标**：追踪一个 YAML 字符串到具体 Selector 实例的完整解析路径。

**操作步骤**：

1. 假设某决策配置了 `algorithm.type: hybrid`。从 [req_filter_classification_runtime.go:L17-L29](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_runtime.go#L17-L29) 确认 `"hybrid" → MethodHybrid`。
2. 在 [req_filter_classification.go:L81-L98](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification.go#L81-L98) 确认 `MethodHybrid` 命中第一个特例分支，调用 `newDecisionHybridSelector`。
3. 跟到 [req_filter_classification.go:L121-L137](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification.go#L121-L137) 的 `newDecisionHybridSelector`：它从 `r.Config` 构造 `HybridConfig`，再从 Registry 取出 elo/routerDC/autoMix 三个子选择器组装成 `HybridSelector`。
4. 对比：若 `algorithm.type: elo`，路径是什么？（提示：elo 不在特例分支，走 L92~L97 的 Registry 查表。）

**预期结果**：你能讲清「hybrid 需要 new 一个带子组件的复合选择器，而 elo 直接从 Registry 取现成实例」的差异，并指出这是「复合算法 vs 原子算法」的区别。

> 待本地验证：选择算法本身的数学（Hybrid 如何组合 elo/routerDC/automix）是 u6-l2 的主题，本讲只追到「拿到 Selector 实例」为止。

#### 4.5.5 小练习与答案

**练习 1**：如果 YAML 里写了一个映射表里不存在的 `algorithm.type: foobar`，会发生什么？
**答案**：`getSelectionMethod` 查表失败，回落到 `MethodStatic`（[L409](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification.go#L409)）。也就是说，未知算法名不会报错，而是退化为静态选择——这是一个「容错降级」而非「硬失败」的设计。

**练习 2**：为什么 Hybrid / MultiFactor / Prompt 三个方法要单独走特例分支，而不像 elo 那样直接 `registry.Get`？
**答案**：因为它们需要**本决策 `algorithm` 里携带的子配置**才能实例化（Hybrid 需要 `HybridSelectionConfig`、MultiFactor 需要 `MultiFactorSelectionConfig`、Prompt 需要 prompt 字符串）。Registry 里存的是「不带决策级配置的原子选择器」，无法满足这些复合/参数化算法的需要，所以必须在拿到 `algorithm` 的调用点现场构造。

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「端到端调用链追踪」。

**任务**：给定一个 `model: "vllm-sr/auto"`、消息为 `"write a quicksort in python"` 的请求，画出它从进入 `performDecisionEvaluation` 到产出 `selectedModel` 的完整调用链，并标注每一步的输入输出类型。

**建议步骤**：

1. **起点**：[processor_req_body_prepare.go:L109](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_req_body_prepare.go#L109) 调用 `performDecisionEvaluation("vllm-sr/auto", history, ctx)`。
2. **信号准备**：`prepareSignalEvaluationInput(history)`（[L54](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification.go#L54)）→ `signalEvaluationInput`。
3. **候选决策**：`decisionCandidatesForRequest`（[L61](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification.go#L61)）→ `[]config.Decision`。
4. **信号求值**：`evaluateSignalsForDecision` → `classifier.EvaluateAllSignalsWithHeaders` → `SignalResults`；经 `evaluateDecisionInternal` 桥接为 `decision.SignalMatches`。
5. **决策引擎**：`runDecisionEngine` → `EvaluateDecisionsWithSignals(SignalMatches)` → `DecisionResult`（你预期这条消息会命中类似 `fast_qa`/`code` 类的决策，而非兜底 `casual_chat`）。
6. **方法解析与选模型**：`finalizeDecisionEvaluation` → `selectDecisionRuntimeModel` → `getSelectionMethod(algorithm.type)` → `selectorForDecisionMethod` → `selector.Select` → `selectedModel`。

**交付物**：一张包含「步骤号 → 函数名 → 所在文件:行 → 输入类型 → 输出类型」五列的表格，并在最后写一行：这条请求最终命中了哪个决策（你可以通过运行 [req_filter_entrypoint_test.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_entrypoint_test.go) 里的相关用例，或查阅 `balance` 配方的 `recipe.dsl` 来验证你的猜测）。

> 待本地验证：具体命中哪条决策取决于所启用配方的实际配置；若本地未起服务，可改为「读 balance 的 recipe.dsl，找出最可能匹配编程类问题的 ROUTE」作为替代。

## 6. 本讲小结

- `performDecisionEvaluation` 是请求链路的内核，线性串联 **信号准备 → 候选决策 → 信号求值 → 决策引擎 → 收尾选模型** 五步，返回 `(decisionName, confidence, reasoning, selectedModel, error)` 五元组；前三道护栏处理「无内容 / 无配方 / 无 decisions」的早退。
- **信号准备**（`prepareSignalEvaluationInput`）是一道搬运+可选压缩的解耦层，产出 `evaluationText`（原文）与 `compressedText`（压缩）两个文本，并标记哪些信号必须用原文。
- **信号求值**（`evaluateSignalsForDecision`）调用分类器抽取全部信号得到 `SignalResults`，写回 `RequestContext` 供后续阶段复用；再经 `evaluateDecisionInternal` 把 `SignalResults` **逐字段桥接**成 `decision.SignalMatches`，让 `decision` 包不反向依赖 `classification`。
- **决策引擎**（`EvaluateDecisionsWithSignals`）是纯函数式规则求值器：对布尔规则树递归求值，AND 取平均置信度、OR 取最大、NOT 翻转；最后按「分层 / confidence / priority」三策略选最佳决策，`Name` 升序作确定性 tie-break。
- **方法解析**跨三道类型鸿沟：`algorithm.type`（字符串）→ `SelectionMethod`（枚举，查 `selectionMethodByAlgorithmType` 表）→ `Selector`（实例，Hybrid/MultiFactor/Prompt 走特例现场构造，其余从 Registry 取）。
- 两条贯穿全讲的边界：**passthrough 不参与决策/换模型**；**配方隔离**靠「候选决策取自当前 recipe、空决策返回空非 nil 切片、Registry 按配方选取」三处共同守住。

## 7. 下一步学习建议

- 想深入「选择算法的数学」（Elo 的 Bradley-Terry、Hybrid 如何组合子选择器、AutoMix 的成本-质量权衡）：继续读 **u6-l2 选择算法注册表**，那里展开 `selection` 包内部。
- 想知道「信号族内部到底怎么算」：进入 **u8 分类信号系统**，从 `Classifier.EvaluateAllSignalsWithHeaders` 往里读，本讲的第 4.3 节正是它的调用侧。
- 想看决策选出后「响应阶段如何处理、插件链如何回调」：读 **u5-l3 响应体处理与插件回调**，那是请求链路的收尾。
- 想从配置侧理解「决策、规则树、algorithm 是怎么用 DSL 写出来的」：读 **u7-l1 DSL 语法与 AST**，你会看到 DSL 的 `WHEN`/`ROUTE`/`algorithm` 如何编译成本讲引用的 `config.Decision`、`RuleNode`、`AlgorithmConfig`。
