# 分类编排与信号求值

## 1. 本讲目标

本讲进入 vLLM Semantic Router（下称 SR）路由流水线的「信号抽取执行者」——`pkg/classification` 包。在前置讲义里，我们已经知道一次请求会经过「信号 → 投影 → 决策 → 模型 + 插件」四层，并且决策引擎（u6-l1）消费的是一个叫 `SignalMatches` 的容器。但**信号是怎么被一次性算出来的？谁来调度这十几个风格各异的分类后端？算完之后又怎么交给决策引擎？** 这三个问题就是本讲要回答的。

学完本讲，你应当能够：

- 说清 `Classifier` 这个「胖编排器」持有哪些分类后端、是怎么被组装出来的；
- 读懂 `EvaluateAllSignalsWithContext` 的并发调度流程，解释「只算决策用得上的信号」这条优化是怎么实现的；
- 描述 `SignalResults` 到 `decision.SignalMatches` 的桥接过程，并能跟踪 extproc 侧 `performDecisionEvaluation` 把信号求值与决策求值串成一条链的全过程。

## 2. 前置知识

在阅读本讲前，你需要先建立以下认知（均来自前置讲义）：

- **16 个信号族**（u2-l2）：SR 把请求里能抽取的事实分成 keyword、embedding、domain、complexity、context、jailbreak、pii 等约 20 类（项目官方对外称「16 个核心信号族」），每类信号带 0~1 的置信度。
- **投影层**（u2-l3）：把杂乱的信号协调成少数命名路由带（如 `balance_complex`），投影结果会以 `projection:名字` 的形式反写回信号置信度表。
- **决策引擎**（u6-l1）：以 `RuleNode` 规则树递归求值 ROUTE 的 WHEN，输入是按信号族分门别类装好的 `decision.SignalMatches`。
- **请求主链路**（u5-l2）：`performDecisionEvaluation` 是请求处理的内核，它把「对话历史 + 模型名」加工成 `(decisionName, confidence, selectedModel, …)` 五元组，本讲正是要拆开它的「信号求值」与「决策求值」两段。

如果这些概念你已经熟悉，可以把本讲理解为「**给 u5-l2 里那个被一笔带过的 `evaluateSignalsForDecision` / `runDecisionEngine` 补上完整实现细节**」。

一个贯穿全讲的关键术语是**后端（backend）**：这里指的不是模型后端，而是「实现某个信号族的分类器对象」，例如 `keywordClassifier`、`jailbreakInference`、`complexityClassifier`。`Classifier` 的职责就是把这些异构后端编排到一个统一入口后面。

## 3. 本讲源码地图

本讲涉及的关键源码文件，全部位于 `src/semantic-router/` 下：

| 文件 | 作用 |
| --- | --- |
| [pkg/classification/classifier.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier.go) | `Classifier` 结构体定义，持有全部分类后端；函数式选项（option）组装方式 |
| [pkg/classification/classifier_construction.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_construction.go) | `Classifier` 的构建器，用 `errgroup` 并行构造各分类后端选项 |
| [pkg/classification/classifier_signal_eval.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_eval.go) | `EvaluateAllSignals` 等公开入口（薄封装） |
| [pkg/classification/classifier_signal_context.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_context.go) | 信号求值核心 `evaluateAllSignalsWithContext`、`signalReadiness` |
| [pkg/classification/classifier_signal_dispatch.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_dispatch.go) | 「主信号」分发器构造与并发执行 |
| [pkg/classification/classifier_signal_dispatch_policy.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_dispatch_policy.go) | 「策略信号」（jailbreak/pii/kb/…）分发器构造 |
| [pkg/classification/classifier_signal_usage.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_usage.go) | 「只算用得上的信号」的使用分析逻辑 |
| [pkg/classification/classifier_signal_results.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_results.go) | `SignalResults` 容器结构体定义 |
| [pkg/classification/classifier_projections.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_projections.go) | 信号求值的最后一步：计算投影分数与命名带 |
| [pkg/classification/classifier_signal_decision.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_decision.go) | **信号 → 决策桥接**：把 `SignalResults` 翻译成 `decision.SignalMatches` 并调用决策引擎 |
| [pkg/classification/classifier_signal_authz.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_authz.go) | `EvaluateAllSignalsWithHeaders`（带鉴权头的请求期入口） |
| [pkg/classification/classifier_signal_rule_evaluators.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_rule_evaluators.go) | keyword/domain 等具体信号求值器示例 |
| [pkg/extproc/req_filter_classification.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification.go) | extproc 侧决策求值总入口 `performDecisionEvaluation` |
| [pkg/extproc/req_filter_classification_runtime.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_runtime.go) | `evaluateSignalsForDecision` / `runDecisionEngine` |
| [pkg/extproc/req_filter_classification_signal.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_signal.go) | `applySignalResultsToContext`（把信号快照写回请求上下文） |
| [pkg/config/config.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/config.go) | `SignalType*` 常量（信号族字符串标识） |

> 提示：本讲引用的代码行号基于 HEAD `7a77e1e1`，若你本地代码已更新，永久链接仍有效，但行号可能略有偏移。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **Classifier 编排**——谁在调度这些后端；
2. **信号求值**——一次请求的文本如何变成填好的 `SignalResults`；
3. **信号 → 决策桥接**——`SignalResults` 如何交给决策引擎。

### 4.1 Classifier 编排：一个胖编排器

#### 4.1.1 概念说明

`classification.Classifier` 是一个**胖编排器（fat orchestrator）**：它本身不做任何模型推理或规则匹配，而是**持有**所有信号族的分类后端，对外暴露统一的「把这段文本的信号全部算出来」入口。

为什么需要这样一个编排器？因为 SR 的信号族异构性极强：

- **规则型 / 启发式后端**：keyword（关键词匹配）、structure（请求形状计数）、context（token 计数）、language（语言识别），命中即布尔；
- **学习型 / 模型驱动后端**：domain（BERT 多类别分类）、complexity（嵌入相似度）、jailbreak（BERT + 对比学习）、pii（token 分类），输出带概率灰度；
- **基于外部状态的后端**：kb（知识库检索）、authz（用户角色绑定）、conversation（会话形状）、event（事件抽取）。

这些后端初始化代价、运行代价、输入文本要求各不相同。`Classifier` 的价值在于把这种异构性藏在一个对象背后，让上层（extproc）只需调用一个方法，而不必关心「jailbreak 要带历史、context 要数 token、authz 要读请求头」这类细节。

#### 4.1.2 核心流程

`Classifier` 的生命周期分两阶段：

1. **构建期（启动时一次性）**：根据配置（`config.RouterConfig`）把需要的后端构造好，用**函数式选项（functional options）**模式注入到 `Classifier`。各后端的构造被组织成一个 `errgroup`，**并行初始化**以缩短启动时间。
2. **运行期（每请求）**：调用 `EvaluateAllSignals…` 系列方法，把请求文本分发到各后端并发求值。

构建期采用「option 列表」而非构造函数一大堆参数，好处是新加一个信号族只需新增一个 `withXxx` 选项，而不破坏既有调用方签名。

#### 4.1.3 源码精读

先看 `Classifier` 持有的后端字段，体会「胖」到什么程度：

[src/semantic-router/pkg/classification/classifier.go:L30-L100](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier.go#L30-L100) —— `Classifier` 结构体。注意它几乎每个信号族都成对持有「初始化器 + 推理器」或一个具体分类器对象，例如 `keywordClassifier *KeywordClassifier`、`categoryInference CategoryInference`、`jailbreakInference JailbreakInference`、`complexityClassifier *ComplexityClassifier`、`kbClassifiers map[string]*KnowledgeBaseClassifier`。字段被分为「In-tree classifiers」「MCP-based classifiers」「Hallucination mitigation classifiers」等几组，对应不同来源与职责的后端。

> 关键观察：`Classifier` 只持有**指针/接口**，不持有模型权重本身。模型权重在各自的 binding（candle/onnx，见 u12-l4）里，`Classifier` 通过 `categoryInference` 这类接口调用它们。

再看它怎么被组装出来。`newClassifierWithOptions` 接收一个 option 列表逐个应用：

[src/semantic-router/pkg/classification/classifier.go:L184-L204](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier.go#L184-L204) —— `newClassifierWithOptions`：先建空壳 `&Classifier{Config: cfg}`，从 `cfg.Authz` 解析身份头名与 fail-open 策略，再循环应用 options，最后 `buildCategoryNameMappings()` 建立 MMLU 类别名与通用类别名的双向映射（供 domain 信号做别名扩展，详见 u6-l1）。

而真正决定「装哪些后端」的是构建器 `classifierOptionBuilder.build`：

[src/semantic-router/pkg/classification/classifier_construction.go:L29-L51](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_construction.go#L29-L51) —— 它把 11 个 `buildXxxClassifierOption` 步骤放进一个切片，交给 `buildParallelOptions` 用 `errgroup.Group`（带并发上限 `SetLimit`）**并行构造**，任何一个失败则整组失败。这条设计直接决定了启动时间——十几个后端是并行而非串行就绪的。

一个典型的 option 长这样（以 keyword 为例）：

[src/semantic-router/pkg/classification/classifier.go:L128-L139](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier.go#L128-L139) —— `withKeywordClassifier` / `withKeywordEmbeddingClassifier`：返回一个闭包，把后端对象塞进 `Classifier` 对应字段。这就是函数式选项的标准写法。

> 小结：`Classifier` = 配置 + 一堆后端指针。它存在的全部意义，是让运行期那一个 `EvaluateAllSignals…` 调用能拿到所有后端。

#### 4.1.4 代码实践

**实践目标**：建立一个「信号族 → Classifier 后端字段」的映射直觉。

**操作步骤**：

1. 打开 [classifier.go:L30-L100](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier.go#L30-L100)，数一下 `Classifier` 持有多少个分类后端字段。
2. 对照 [config.go:L25-L43](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/config.go#L25-L43) 的 `SignalType*` 常量列表（keyword/embedding/domain/fact_check/…/event/projection），逐个找：哪个信号族对应 `Classifier` 里哪个字段？哪些信号族没有专属字段（而是从 `c.Config.*Rules` 配置直接读）？
3. 在 [classifier_construction.go:L30-L42](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_construction.go#L30-L42) 的 `steps` 切片里，确认哪些后端是并行构造的，哪些（`addCategoryClassifier`、`addMCPCategoryClassifier`）是串行追加的。

**需要观察的现象**：你会发现 `authz`、`fact_check`、`user_feedback`、`preference`、`language` 等信号族在 `Classifier` 里**没有独立的「初始化器」字段**——它们要么依赖一个通用后端（如 `feedbackDetector`），要么直接读 `c.Config` 里的规则表。这说明「信号族数量」与「后端字段数量」并不一一对应，编排器做了一定归并。

**预期结果**：你能画出一张表，左列是 20 个 `SignalType*`，右列是对应的 `Classifier` 字段或「无（读 Config）」。

#### 4.1.5 小练习与答案

**练习 1**：`Classifier` 为什么用「函数式选项 + errgroup 并行」而不是「一个超长 `New` 函数顺序构造」？

**参考答案**：选项模式让新增信号族成为「加一个 `withXxx` + 在 steps 列表登记」的局部改动，不破坏 `newClassifierWithOptions` 的签名；errgroup 并行则把十几个独立后端的初始化延迟从「求和」降到「取最大」，显著缩短冷启动。两者结合换来的是「易扩展」与「启动快」。

**练习 2**：`Classifier` 持有 `Config *config.RouterConfig`，这意味着什么？热重载配置时会发生什么？

**参考答案**：`Classifier` 直接持有配置指针，运行期求值时（如 `evaluateJailbreakSignal` 遍历 `c.Config.JailbreakRules`）读的就是这块配置。热重载时（见 u4-l2、u4-l3）控制面会**重建整个 Classifier 并原子替换**，而不是原地改 `Config` 字段，从而保证一次请求内看到的配置是一致的快照。

---

### 4.2 信号求值：EvaluateAllSignals 的并发调度

#### 4.2.1 概念说明

`EvaluateAllSignals*` 系列方法是 `Classifier` 对外的运行期入口。它的合同很简洁：**给我这段（可能的多段）文本和请求上下文，我还你一个填好的 `SignalResults`**。`SignalResults` 是一个大容器，按信号族分门别类地装着「命中的规则名」「置信度」「原始值」三类信息。

这个看似简单的合同背后有三条关键设计：

1. **并发扇出**：十几个信号后端互相独立，应该并发跑，用一把 `sync.Mutex` 保护对 `SignalResults` 的写入。
2. **懒求值（只算用得上的）**：如果当前配方没有任何决策引用 `pii` 信号，就根本不调 PII 后端——这是 u2-l2 提到的「SR 只求值决策真正引用的信号以省算力」的具体实现。
3. **就绪门控**：即使某信号被引用，但对应后端没初始化好（如嵌入模型未就绪），也跳过并记日志，不让一个未就绪的后端拖垮整条请求。
4. **投影收尾**：所有原始信号算完后，统一计算投影分数与命名带，把结果反写回 `SignalConfidences`。

#### 4.2.2 核心流程

`evaluateAllSignalsWithContext` 的执行过程（这是真正的实现，公开方法都是它的薄封装）：

```
1. 进入负载闸门（load gate，限流保护）
2. 决定 usedSignals（要算哪些 type:name）：
     - forceEvaluateAll → 全部已配置信号
     - signalScopeSet   → 仅给定决策子集引用的信号
     - 默认              → 当前配方所有决策引用的信号
3. 解析每个信号类型应使用的文本（压缩 vs 原文）
4. 计算 ready（每个信号族的后端是否就绪）
5. 新建空 SignalResults（含 SignalConfidences/SignalValues/SignalErrors 三个 map）
6. buildSignalDispatchers：为每个信号族构造一个闭包（捕获 results、mu、文本）
7. runSignalDispatchers：对每个 (used && ready) 的信号起一个 goroutine 并发求值
8. wg.Wait() 等全部完成
9. 后处理（串行）：
     applySignalGroups      → 信号分组（partition 别名归并）
     applySignalComposers   → 信号组合
     applySignalOutputPolicies → 输出策略
     applyProjections       → 计算投影分数与命名带，写回 SignalConfidences
10. 返回填好的 SignalResults
```

并发调度的核心判定可以写成一个逻辑条件——一个信号类型 `t` 会被实际求值，当且仅当：

\[
\mathrm{dispatch}(t) \;=\; \mathrm{used}(t) \;\land\; \mathrm{ready}(t)
\]

其中 \(\mathrm{used}(t)\) 表示「至少有一个被决策引用的信号键以 `t:` 为前缀」，\(\mathrm{ready}(t)\) 表示「该族后端已初始化且配置非空」。这条「与」门是 SR 控制算力的核心旋钮。

#### 4.2.3 源码精读

先看公开入口的层次——它们都收敛到同一个私有实现：

[src/semantic-router/pkg/classification/classifier_signal_eval.go:L5-L13](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_eval.go#L5-L13) —— `EvaluateAllSignals(text)` 只有一行，把所有参数填默认值后委托给 `EvaluateAllSignalsWithContext`。这是给测试和简单场景用的便捷入口。

[src/semantic-router/pkg/classification/classifier_signal_context.go:L48-L106](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_context.go#L48-L106) —— 三层公开方法 `EvaluateAllSignalsWithContext` / `EvaluateAllSignalsWithRequestFacts` / `EvaluateAllSignalsWithRequestFactsForDecisions`，参数越来越多（文本、上下文文本、历史消息、压缩信息、请求事实、决策作用域），但都只是把参数透传给私有 `evaluateAllSignalsWithContext`，区别仅在最后两个参数 `signalScope []config.Decision` 与 `signalScopeSet bool`——后者用于把信号作用域**限制到某个决策子集**（如「直接 looper」算法视图）。

接下来是真正的实现：

[src/semantic-router/pkg/classification/classifier_signal_context.go:L143-L220](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_context.go#L143-L220) —— `evaluateAllSignalsWithContext` 主体。重点看几处：

- **L159** `defer c.enterSignalEvaluationLoadGate()()`：进入一个负载闸门，防止信号求值并发过高拖垮进程；
- **L162-L170** 三分支决定 `usedSignals`：force / scope / 默认；
- **L172** `textForSignal`：为每个信号类型解析正确文本（部分信号如 jailbreak 必须用未压缩原文，见 `skipCompressionSignals`）；
- **L173** `ready := c.signalReadiness()`：算就绪表；
- **L182-L214** 并发扇出核心：建 `sync.WaitGroup` 与 `sync.Mutex`，`buildSignalDispatchers` 造闭包，`runSignalDispatchers` 起 goroutine，最后 `wg.Wait()`；
- **L215-L218** 串行后处理四连：groups → composers → output policies → projections。

「就绪表」是怎么算的：

[src/semantic-router/pkg/classification/classifier_signal_context.go:L12-L34](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_context.go#L12-L34) —— `signalReadiness` 返回一个 `map[string]bool`，每个信号族的就绪条件不同：keyword 看 `c.keywordClassifier != nil`，domain 还要求 `IsCategoryEnabled() && categoryInference != nil && CategoryMapping != nil`，jailbreak/pii/modality 还要求配置里规则非空且功能开启（`IsJailbreakEnabled()` 等）。注释说明它被单独抽出来是为了「把圈复杂度压在 linter 阈值之下」——这是一个值得学习的工程习惯。

「懒求值」是怎么决定 usedSignals 的：

[src/semantic-router/pkg/classification/classifier_signal_usage.go:L13-L28](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_usage.go#L13-L28) —— `getUsedSignals` 遍历当前配方的全部决策（`c.Config.AllRoutingDecisions()`），对每条决策的规则树递归收集被引用的信号键（`type:name`），再 `expandProjectionDependencies` 把投影引用展开成它依赖的底层信号。这样一张 `map[string]bool` 就是「真正要算的信号清单」。

[src/semantic-router/pkg/classification/classifier_signal_usage.go:L72-L113](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_usage.go#L72-L113) —— `analyzeRuleCombination` 递归遍历 `RuleNode`：叶子节点（`IsLeaf()`）记下 `type:name`，组合节点递归子条件。`expandProjectionDependencies` 处理一种关键依赖：当决策引用了 `projection:difficulty_complex`，而该投影带来自某个 score，score 又依赖若干输入信号——这些输入信号也必须被求值，否则投影算不出来。

[src/semantic-router/pkg/classification/classifier_signal_usage.go:L151-L161](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_usage.go#L151-L161) —— `isSignalTypeUsed`：判断某个类型前缀下是否有任何信号被引用，是 `runSignalDispatchers` 决定是否起 goroutine 的关键。

现在看分发器的构造与执行：

[src/semantic-router/pkg/classification/classifier_signal_dispatch.go:L16-L112](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_dispatch.go#L16-L112) —— `buildSignalDispatchers` 构造「主信号」分发列表（keyword/embedding/domain/fact_check/user_feedback/reask/preference/language/context/structure/complexity/modality 共 12 个），每个元素是 `{signalType, name, evaluate func()}` 三元组，闭包捕获了 `results`、`mu` 与对应文本。注意不同信号拿到的文本不同：context 用 `contextText`，reask 用 `currentUserText + priorUserMessages`，其余用 `textForSignal(type)`。最后追加 `buildPolicySignalDispatchers` 的策略信号。

[src/semantic-router/pkg/classification/classifier_signal_dispatch_policy.go:L9-L91](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_dispatch_policy.go#L9-L91) —— 策略信号分发器（jailbreak/pii/kb/conversation/event/metadata/classifier）。这里有个巧妙设计：`historyForSignals` 用 `sync.Once` **惰性且只算一次**地拼出历史消息——因为 jailbreak 与 pii 都需要 `include_history`，但历史拼接有成本，用 `Once` 保证只拼一次共享。

[src/semantic-router/pkg/classification/classifier_signal_dispatch.go:L136-L151](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_dispatch.go#L136-L151) —— `runSignalDispatchers`：对每个分发器，若 `isSignalTypeUsed && ready` 则 `wg.Add(1)` 起一个 goroutine 跑 `dispatch.evaluate()`；否则记一行 debug 日志说明跳过原因。**这就是并发扇出与懒求值的交汇点**。

并发写入的安全性靠每个求值器内部加锁保证。看一个具体例子：

[src/semantic-router/pkg/classification/classifier_signal_rule_evaluators.go:L40-L55](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_rule_evaluators.go#L40-L55) —— keyword 求值器在 `mu.Lock()` 保护下把命中的 `match.RuleName` 追加进 `results.MatchedKeywordRules`，把实际关键词追加进 `results.MatchedKeywords`。所有信号求值器都遵循「算自己的、加锁写 results」这个模式。

最后看 `SignalResults` 容器长什么样：

[src/semantic-router/pkg/classification/classifier_signal_results.go:L12-L56](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_results.go#L12-L56) —— `SignalResults` 结构体。三类字段值得记住：

- **`MatchedXxxRules []string`**（约 20 个）：每族命中的规则名列表，如 `MatchedKeywordRules`、`MatchedDomainRules`、`MatchedComplexityRules`——这是决策引擎**存在性匹配**的主要输入；
- **`SignalConfidences map[string]float64`**：键为 `"族:名"`（如 `"embedding:ai"` → 0.88），存真实置信度——决策引擎据此做置信度聚合（AND 平均、OR 取最大）；
- **`SignalValues map[string]float64`**：原始数值（如 `"structure:many_questions"` → 4），供 `classifier` 类型的 `Predicate` 数值断言用；
- 还有 `MatchedKeywords`（实际关键词，非规则名）、`ProjectionScores`、`ProjectionTrace`（可解释性负载）等。

投影收尾在最后一步：

[src/semantic-router/pkg/classification/classifier_projections.go:L5-L38](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_projections.go#L5-L38) —— `applyProjections`：把 score 按拓扑序（`topologicalScoreOrder`，因为 score 可依赖另一个 score）逐个算出 `ProjectionScores[name]`，再对每个 mapping 应用阈值带（`applyProjectionMapping`），把命名带以 `projection:名字` 反写进 `SignalConfidences`，最后 `mergeProjectionTrace` 生成可解释性 trace。投影的数学细节见 u2-l3，这里只需知道**投影是信号求值的最后一步、产出会反写回置信度表**。

> 关键结论：`EvaluateAllSignals*` 的输出是一个**自洽的 `SignalResults`**——它既包含原始信号，也包含由原始信号派生的投影结果，决策引擎可以无差别地用 `MatchedXxxRules` + `SignalConfidences` 消费两者。

#### 4.2.4 代码实践

**实践目标**：搞清「对一个具体配方，`EvaluateAllSignals` 到底会触发哪些分类器」。

**操作步骤**：

1. 选定 `balance` 配方：打开 [config/recipes/balance/recipe.dsl](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl)（或编译产物 `config/recipes/balance/config.yaml`）。
2. 找出它定义了哪些 SIGNAL、PROJECTION、ROUTE。重点看 ROUTE 的 WHEN 引用了哪些信号与投影。
3. 对照 `analyzeRuleCombination`（[classifier_signal_usage.go:L73-L83](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_usage.go#L73-L83)）的逻辑，手工推演：balance 的所有 ROUTE WHEN 展开后，`usedSignals` 这个 map 里会有哪些 `type:name` 键？
4. 再对照 `signalReadiness`（[classifier_signal_context.go:L12-L34](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_context.go#L12-L34)），判断在「嵌入模型已就绪、但 PII 模型未下载」的场景下，哪些信号会被求值、哪些会因 `ready=false` 被跳过。

**需要观察的现象**：你会看到一个配方**不会**触发全部 20 个信号族。例如 balance 主要用到 keyword/embedding/domain/complexity/context 等，而不一定用 pii/jailbreak/event——后者属于 privacy/agent 等配方。这印证了懒求值的价值。

**预期结果**：列出 balance 配方下 `runSignalDispatchers` 实际会为其起 goroutine 的信号类型清单（即 `used && ready` 的那些），以及明确被跳过的信号类型及原因。

**待本地验证**：若想看到真实运行结果，可在 `runSignalDispatchers` 的 debug 日志（`[Signal Computation] %s signal not used in any decision, skipping evaluation`）处开启 debug 级别日志，发一次 `vllm-sr chat` 请求观察输出。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `evaluateAllSignalsWithContext` 要把 `usedSignals` 和 `ready` 分成两张 map，而不是合并成一个？

**参考答案**：它们语义不同且都需要独立可观察。`usedSignals` 回答「**该不该**算」（由配方逻辑决定，与运行环境无关），`ready` 回答「**能不能**算」（由后端初始化状态决定，与配置无关）。分开后，一个未就绪的后端会被记录为「跳过」而非「未引用」，便于运维定位「为什么某信号没生效」——是配方没引用，还是模型没下载好。合并会丢失这个区分。

**练习 2**：`historyForSignals` 用 `sync.Once` 拼历史消息，为什么不用一个普通局部变量提前算好？

**参考答案**：因为只有 jailbreak/pii 中带 `include_history` 的规则才需要历史，且这两个信号可能因 `used/ready` 门控被跳过。提前算好会在「不需要历史」时白做一次拼接；`sync.Once` 让历史拼接**惰性触发且仅一次**，在 jailbreak 与 pi 都需要时还能共享同一份结果，避免重复拼。

**练习 3**：投影（`applyProjections`）为什么放在所有信号求值**之后**、而不是和信号并发算？

**参考答案**：投影的输入是原始信号（score 的 weighted_sum 依赖若干信号值），必须等原始信号写完才能算；且 score 之间还有依赖（一个 score 可引用另一个 score），所以 `applyProjections` 用拓扑序串行计算。它无法与信号并发，只能作为 `wg.Wait()` 之后的收尾步骤。

---

### 4.3 信号 → 决策桥接：从 SignalResults 到 SignalMatches

#### 4.3.1 概念说明

到这一步，`EvaluateAllSignals*` 已经把请求文本变成了一个填好的 `SignalResults`。但决策引擎（`pkg/decision`，见 u6-l1）**不认识 `SignalResults`**——它认识的是 `decision.SignalMatches`。这是一个刻意的解耦：

- `SignalResults` 是 classification 包的「**协议无关的事实容器**」，字段名贴近信号族（`MatchedKeywordRules`…）；
- `decision.SignalMatches` 是 decision 包的「**规则求值输入**」，字段名贴近规则树语义。

桥接（bridge）就是把前者**逐字段搬**到后者。这样决策包就不必反向 import classification 包，两个包可以独立演进、独立测试。这是 SR 代码库里「窄接口缝」哲学（见 u4-l2）的又一次体现。

桥接完成后，调用决策引擎得到 `DecisionResult`（命中决策名 + 置信度 + 匹配规则），再由 extproc 侧把它落成具体模型选择。

#### 4.3.2 核心流程

桥接发生在 classification 包内部，被 extproc 侧的总链路串起来：

```
extproc 侧（req_filter_classification*.go）:
  performDecisionEvaluation            ← 总入口（u5-l2 已介绍）
    ├─ prepareSignalEvaluationInput    ← 把对话历史搬成 signalEvaluationInput
    ├─ decisionCandidatesForRequest    ← 取当前配方参与求值的决策子集
    ├─ evaluateSignalsForDecision      ← 调 classifier.EvaluateAllSignalsWithHeaders → SignalResults
    │     └─ applySignalResultsToContext ← 把 SignalResults 快照写回 RequestContext
    ├─ runDecisionEngine               ← 调 classifier.EvaluateDecisionWithEngineForDecisions
    │     └─ classification 包内：
    │          evaluateDecisionInternal
    │            ├─ 构造 decision.SignalMatches（逐字段搬 SignalResults）
    │            ├─ decision.NewDecisionEngine(...).WithRoutingScope(...)
    │            └─ engine.EvaluateDecisionsWithSignals(sm) → DecisionResult
    └─ finalizeDecisionEvaluation      ← 把 DecisionResult 落成 selectedModel + reasoning
```

关键点：**信号只算一次**。`evaluateSignalsForDecision` 产出 `SignalResults` 后，`runDecisionEngine` 接收**同一个 `SignalResults` 指针**去求值决策，不会重复跑分类器。当只评估部分决策候选（`candidates != nil`）时，走 `EvaluateDecisionWithEngineForDecisions`，它把候选决策传给 `evaluateDecisionInternal` 而非用全量 `c.Config.Decisions`。

#### 4.3.3 源码精读

先看 extproc 侧总入口，把整条链路串起来：

[src/semantic-router/pkg/extproc/req_filter_classification.go:L22-L79](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification.go#L22-L79) —— `performDecisionEvaluation`。注意它的防线：

- **L29-L32** 内容为空且无信封路由事实时直接返回空（不浪费算力）；
- **L37-L42** 若路由未解析则幂等地调用 `resolveEntrypointForRequest`，且选中 recipe 为空就早退；
- **L46-L52** 若整个配置没有任何决策，auto 模型走 default model，否则返回空；
- **L61-L65** 算 `candidates`、调 `evaluateSignalsForDecision` 抽信号（鉴权错则直接返回错误）；
- **L67-L70** 用同一份 `signals` 调 `runDecisionEngine`，结果为 nil 走 default model；
- **L72-L78** `finalizeDecisionEvaluation` 落成模型与 reasoning。

注释里强调了一条重要边界：**具体后端模型 ID 的 passthrough 请求不进入决策求值**——它们不继承 default recipe 的信号/策略/决策/插件。

接着看信号求值这一段（extproc 侧）：

[src/semantic-router/pkg/extproc/req_filter_classification_runtime.go:L31-L77](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_runtime.go#L31-L77) —— `evaluateSignalsForDecision`。它把协议无关的 `signalEvaluationInput` 装进 classification 包的 `SignalEvaluationInput` 结构体（注意 `Text` 用压缩文本 `compressedText`、`UncompressedText` 用原文 `evaluationText`），调 `classifier.EvaluateAllSignalsWithHeaders`。鉴权错误（`authzErr`）会被当成**硬错误**返回——「缺失身份不能静默绕过策略」。得到 `signals` 后立刻 `applySignalResultsToContext` 把信号快照写回 `RequestContext`（供后续插件、重放、响应阶段使用）。

`EvaluateAllSignalsWithHeaders` 的特别之处在于它在普通信号求值后**额外处理 authz**：

[src/semantic-router/pkg/classification/classifier_signal_authz.go:L14-L54](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_authz.go#L14-L54) —— 它先调 `evaluateAllSignalsWithContext` 算普通信号，再调 `appendAuthzFromHeaders` 单独处理鉴权。`SignalEvaluationInput` 是一个「全部具名字段」的结构体，注释解释了为什么不用位置参数：避免新增信号上下文时改变调用顺序。

[src/semantic-router/pkg/classification/classifier_signal_authz.go:L56-L94](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_authz.go#L56-L94) —— `appendAuthzFromHeaders`：从请求头读 userID/userGroups，调 `authzClassifier.Classify` 得到匹配角色规则。注意 `applyAuthzFailOpenOnClassifyError` 这一步——它根据 `cfg.Authz.FailOpen` 决定分类失败时是「放行」还是「拒绝」。authz 之所以被单独处理而非走通用 dispatch，是因为它**不依赖文本、依赖请求头**，且失败语义必须是硬错误（可被 fail-open 策略改写）。

现在进入本模块的核心——桥接与决策引擎调用：

[src/semantic-router/pkg/classification/classifier_signal_decision.go:L13-L37](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_decision.go#L13-L37) —— 四个公开方法 `EvaluateDecisionWithEngine` / `…ForDecisions` / `…AndTrace` / `…AndTraceForDecisions`，都委托给 `evaluateDecisionInternal(signals, trace, candidates)`，区别仅在于：是否带 trace（求值轨迹树，供可解释性）、是否限定候选决策子集。

[src/semantic-router/pkg/classification/classifier_signal_decision.go:L39-L114](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_decision.go#L39-L114) —— `evaluateDecisionInternal`，**桥接的发生地**。三件事：

1. **L40-L46** 决定决策集合：`candidates != nil` 用候选子集，否则用 `c.Config.Decisions`（默认配方的决策）；空则报错 `no decisions configured`。
2. **L56-L62** 构造决策引擎 `decision.NewDecisionEngine(...)`，传入 keyword/embedding 规则、categories、决策、strategy，并 `.WithRoutingScope(c.Config.RoutingScope)` 限定路由作用域。
3. **L64-L89** **逐字段搬运**：把 `signals.MatchedKeywordRules` → `sm.KeywordRules`，`signals.MatchedDomainRules` → `sm.DomainRules`，`signals.SignalConfidences` → `sm.SignalConfidences`……共约 20 个字段一一对应。这一段就是「桥接」的物理实现——没有任何转换逻辑，纯粹是字段重命名式搬运，目的是让 decision 包不依赖 classification 包。
4. **L94-L102** 调引擎：带 trace 走 `EvaluateDecisionsWithTrace`，否则走 `EvaluateDecisionsWithSignals`。
5. **L108** 把 `signals.MatchedKeywords`（实际关键词）补进结果，供后续日志/重放使用。

debug 日志（L48-L54）会打印每一族信号的命中情况，是排查「为什么没命中预期路由」的第一手信息。

最后看 extproc 侧的决策引擎调用与收尾：

[src/semantic-router/pkg/extproc/req_filter_classification_runtime.go:L138-L191](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_runtime.go#L138-L191) —— `runDecisionEngine`。注意几个鲁棒性设计：

- classifier 为 nil → 记错误并返回 default model（不让缺分类器拖垮请求）；
- `candidates` 非空但长度 0 → 直接返回 default model；
- 引擎报错或结果为 nil → 返回 default model；
- 注释明确：`llm_decision_evaluation_latency_seconds` 与 `llm_decision_match_total` 两个指标由决策引擎内部发出，**这里不能再发一次**，否则会双计——这是一个容易踩的坑，代码用注释固化了约定。

[src/semantic-router/pkg/extproc/req_filter_classification_runtime.go:L200-L238](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_runtime.go#L200-L238) —— `finalizeDecisionEvaluation`：把 `DecisionResult` 落到 `RequestContext`（`applyDecisionResultToContext` 记录选中决策、router_replay 插件配置、保留策略、类别名），若是 auto 模型再调 `selectDecisionRuntimeModel` 选具体模型。这一步把「决策」翻译成「模型」，是通往 u6-l2（选择算法）的衔接点。

还有一处容易被忽略但很重要的桥——把信号写回请求上下文：

[src/semantic-router/pkg/extproc/req_filter_classification_signal.go:L99-L140](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_signal.go#L99-L140) —— `applySignalResultsToContext`：把 `signals.MatchedKeywordRules` → `ctx.VSRMatchedKeywords`、`signals.SignalConfidences` → `ctx.VSRSignalConfidences`、`signals.ProjectionTrace` → `ctx.VSRProjectionTrace`……这是为了让**后续阶段**（插件链、响应处理、重放记录、面板展示）都能读到这次请求的信号快照，而不必重新求值。注意 map 类字段都经过 `cloneReplay*Map` 深拷贝——因为 `SignalResults` 会被复用，而 `RequestContext` 的快照要随请求独立存活（尤其重放场景）。

> 关键结论：信号 → 决策的桥接是**两段式**的——classification 包内 `evaluateDecisionInternal` 把 `SignalResults` 搬成 `decision.SignalMatches` 并跑引擎；extproc 包用 `performDecisionEvaluation` 把「抽信号 → 跑引擎 → 选模型」串成一条线，并对所有失败路径提供 default model 兜底。

#### 4.3.4 代码实践

**实践目标**：跟踪 `MatchedKeywordRules` / `MatchedDomainRules` / `SignalConfidences` 从被写入到被决策引擎消费的完整路径（本讲规格指定的实践任务）。

**操作步骤**：

1. **写入侧**：在 [classifier_signal_rule_evaluators.go:L40-L55](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_rule_evaluators.go#L40-L55) 看 keyword 求值器如何把 `match.RuleName` 写进 `results.MatchedKeywordRules`；在 [classifier_signal_rule_evaluators.go:L58-L95](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_rule_evaluators.go#L58-L95)（及之后）看 domain 求值器如何写 `MatchedDomainRules` 与 `SignalConfidences`（domain 是学习型，会写真实置信度）。
2. **搬运侧**：在 [classifier_signal_decision.go:L64-L89](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_decision.go#L64-L89) 找到 `sm.KeywordRules = signals.MatchedKeywordRules`、`sm.DomainRules = signals.MatchedDomainRules`、`sm.SignalConfidences = signals.SignalConfidences` 三行，确认它们是一一对应搬运。
3. **消费侧**：进入决策引擎 `pkg/decision/engine.go`（u6-l1 已精读），找到求值叶子节点时如何用 `SignalConfidences["keyword:xxx"]` 取置信度、如何用 `DomainRules` 做存在性匹配、domain 又如何经 `mmlu_categories` 做别名扩展。
4. **上下文快照侧**：在 [req_filter_classification_signal.go:L99-L125](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_classification_signal.go#L99-L125) 看这三类信息如何被复制进 `RequestContext`（`VSRMatchedKeywords` / `VSRMatchedDomains` / `VSRSignalConfidences`），供重放与面板使用。

**需要观察的现象**：你会看到同一条信息（如 keyword 命中 `math_keywords`）在四个地方以四个不同字段名出现：`results.MatchedKeywordRules`（classification 写）→ `sm.KeywordRules`（桥接搬）→ 引擎内存在性匹配（decision 消费）→ `ctx.VSRMatchedKeywords`（extproc 快照）。这正是解耦带来的「字段重命名链」。

**预期结果**：你能画一张表，列名是「写入点 / 桥接点 / 消费点 / 快照点」，行是 `MatchedKeywordRules`、`MatchedDomainRules`、`SignalConfidences` 三个字段，填出它们在四处的字段名。

**待本地验证**：可临时在 `evaluateDecisionInternal` 的 debug 日志（[classifier_signal_decision.go:L48-L54](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_decision.go#L48-L54)）处确认运行时这三类字段的实际内容。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `evaluateDecisionInternal` 要把 `SignalResults` 逐字段搬成 `decision.SignalMatches`，而不是直接让 decision 包 import classification 包、直接读 `SignalResults`？

**参考答案**：为了解耦与可测试性。`SignalResults` 字段名贴近信号族、未来会随信号族扩张而变；`SignalMatches` 字段名贴近规则求值、稳定。让 decision 反向依赖 classification 会形成胖依赖，且 decision 引擎的单元测试就得先构造一个完整的 `Classifier`。逐字段搬运换来的是 decision 包零 classification 依赖、可独立用纯数据 `SignalMatches` 喂入测试。代价是新增信号族时要同步改两边的字段——但这是一次性的机械改动。

**练习 2**：`runDecisionEngine` 在 classifier 为 nil、候选为空、引擎报错三种情况下都返回 default model。这种「一律兜底」的设计有什么利弊？

**参考答案**：利是**可用性优先**——路由层永远返回一个可用模型，不会因为分类器未就绪或某条决策配置错误而让请求整体失败（符合「路由作为基础设施层」的定位）。弊是**故障可能被静默吞掉**——运维需要靠指标（`llm_decision_evaluation_latency_seconds`、`recipe_classifier_unavailable` 等错误事件日志）才能发现「其实一直在走 default」。SR 用详尽的 debug/error 日志与 tracing span 来弥补这个透明度问题。

**练习 3**：`applySignalResultsToContext` 里 map 类字段为什么要 `cloneReplay*Map` 深拷贝，而切片类字段（如 `MatchedKeywordRules`）没有显式拷贝？

**参考答案**：切片赋值在 Go 里只是复制 slice header（底层数组共享），但信号求值完成后 `SignalResults` 不会再追加这些切片（本次请求的求值已结束），所以共享底层数组是安全的。而 map 是引用类型且可能在后续被投影/分组步骤继续写入，若不深拷贝，`RequestContext` 的快照会随 `SignalResults` 被复用而被动改变——尤其重放（replay）场景要求快照独立存活，所以 map 必须深拷贝。

---

## 5. 综合实践

把三个模块串起来，完成一次「**端到端跟踪一次 balance 配方请求的信号→决策全流程**」：

1. **准备**：确认本地已能跑 `vllm-sr`（见 u1-l3、u1-l4），balance 是默认配方。
2. **读配方**（模块 4.1）：打开 [config/recipes/balance/config.yaml](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/config.yaml)，统计它配置了哪些 signals / projections / decisions。
3. **推 usedSignals**（模块 4.2）：根据它的 decisions 的 WHEN 规则，手工算出 `usedSignals` 应包含哪些 `type:name` 键；再根据 `signalReadiness`，判断在「嵌入就绪、jailbreak 未就绪」时实际会跑哪些信号。
4. **跟踪搬运**（模块 4.3）：假设这次请求命中了 keyword 信号 `math_keywords`（置信度 1.0）和 domain 信号 `math`（置信度 0.92），写出这两个事实在 `results.MatchedKeywordRules` / `results.MatchedDomainRules` / `results.SignalConfidences` 中的样子，再写出它们被搬进 `decision.SignalMatches` 后的样子。
5. **预测决策**：根据 balance 的 ROUTE WHEN 与 u6-l1 的求值规则（AND 取平均、OR 取最大），预测这条请求会命中哪条 ROUTE、聚合置信度是多少。
6. **（可选）验证**：在 `evaluateAllSignalsWithContext` 与 `evaluateDecisionInternal` 的 debug 日志处开启 debug 级别，发一次含数学题的 `vllm-sr chat` 请求，对比你的预测与实际日志输出。

> 这个练习覆盖了本讲全部三个最小模块，并把它们与 u5-l2（请求主链路）、u6-l1（决策引擎）衔接起来。

## 6. 本讲小结

- **`Classifier` 是胖编排器**：它持有全部信号族的后端指针，本身不推理；构建期用函数式选项 + errgroup 并行组装，运行期对外只暴露 `EvaluateAllSignals*` 统一入口。
- **信号求值是「并发扇出 + 懒求值 + 就绪门控」**：`evaluateAllSignalsWithContext` 用 `usedSignals && ready` 这条「与」门决定每个信号是否起 goroutine，用一把 `sync.Mutex` 保护 `SignalResults` 写入，最后串行跑投影收尾。
- **懒求值由 `getUsedSignals` 实现**：它递归遍历决策规则树收集被引用信号，并 `expandProjectionDependencies` 把投影依赖展开，确保不算决策用不到的信号。
- **`SignalResults` 是协议无关事实容器**：三类核心字段 `MatchedXxxRules`（命中规则名）、`SignalConfidences`（置信度）、`SignalValues`（原始值），外加投影分数与 trace。
- **信号 → 决策靠逐字段搬运桥接**：`evaluateDecisionInternal` 把 `SignalResults` 一一对应搬进 `decision.SignalMatches`，让 decision 包零依赖 classification 包，然后用同一份信号跑决策引擎（信号只算一次）。
- **extproc 用 `performDecisionEvaluation` 串起全链路**：`evaluateSignalsForDecision`（抽信号 + 写回上下文）→ `runDecisionEngine`（跑引擎，所有失败兜底 default model）→ `finalizeDecisionEvaluation`（落模型），并对 authz 失败做硬错误处理。

## 7. 下一步学习建议

本讲讲清了「信号怎么算、怎么交给决策」，接下来建议：

- **往下深入信号族实现**：u8-l2（域/类别分类器）会拆开 `evaluateDomainSignal` 背后的 `category_classifier`，看嵌入与熵分析如何产出多类别概率；u8-l3（PII 与越狱检测）拆开 `evaluatePIISignal` / `evaluateJailbreakSignal` 的安全治理细节；u8-l4（嵌入提供者）看 `embedding.Provider` 这个被多个信号族共用的抽象。
- **往旁看投影与可解释性**：u11-l2（投影追踪与可解释性）会展开本讲里一笔带过的 `ProjectionTrace`，看 partition/score/mapping 的版本化 JSON 如何供重放与面板消费。
- **复习决策侧**：若对 `decision.SignalMatches` 被消费的细节仍不熟，回头读 u6-l1（决策引擎：布尔规则求值与置信度），重点关注 `evalNode` 如何读 `SignalConfidences` 与做 AND/OR/NOT 聚合。
