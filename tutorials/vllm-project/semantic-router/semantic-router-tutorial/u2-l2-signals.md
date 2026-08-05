# Signals：16 个信号族

## 1. 本讲目标

在上一讲（u2-l1）里，我们建立了一条端到端心智模型：一次请求先被**抽取成信号**，再被**投影**协调，最后由**决策**选出路由。本讲就钻进这条流水线的最前端——**信号层（Signals）**。

学完本讲，你应当能够：

1. 认出 vLLM Semantic Router（下文简称 SR）**维护的 16 个核心信号族**，并说出每一族负责从请求里抽取哪一类“事实”。
2. 读懂 `recipe.dsl` 与 `config/signal/` 里 **keyword / embedding / domain / complexity / context** 五类典型信号的写法，能解释它们的判定条件。
3. 理解信号如何同时携带 **confidence（置信度）** 与 **value（原始值）**，以及规则型信号与学习型信号在置信度上的本质差别。

> 本讲只讲“信号怎么被声明、怎么被抽取”，**不**讲它们如何被组合成分数或路由带——那是投影层（u2-l3）和决策层（u2-l4）的任务。

## 2. 前置知识

- **信号（Signal）**：从一次请求里抽取出来的一条**带名字的事实**。它只负责“我观察到了什么”，不负责“因此该走哪条路由”。例如 `domain("health")` 表示“我判断这条请求属于健康领域”，至于健康领域该用哪个模型，由后面的决策决定。
- **置信度（confidence）**：一个 0 到 1 之间的数，表示这条信号“有多可信”。`1.0` 表示确定命中，`0.0` 表示没命中或不可用。
- **规则型 vs 学习型**：有的信号靠人写的规则（关键词、token 数）判定，命中就是命中，是“非黑即白”的；有的信号靠模型（嵌入、分类器）判定，输出的是一个概率或相似度，带有灰度。这是本讲最重要的一组对比。
- **配方（recipe）**：一组完整的路由策略，包含信号、投影、决策、模型。本讲的代码素材主要来自 `balance` 配方。

如果你还没读过 u2-l1，建议先建立“信号→投影→决策”的整体画面，再回到本讲看信号层的细节。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [config/recipes/balance/recipe.dsl](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl) | balance 配方的 DSL 源文件，本讲信号写法的主要素材，里面声明了 domain/keyword/embedding/context/structure/complexity 等所有信号块。 |
| [src/semantic-router/pkg/config/signal_config.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/signal_config.go) | 信号配置的 Go 结构体定义：`KeywordRule`、`EmbeddingRule`、`ContextRule`、`StructureRule`、`ComplexityRule` 等，决定了 YAML/DSL 能写哪些字段。 |
| [src/semantic-router/pkg/config/config.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/config.go) | 定义全部信号类型常量（`SignalTypeKeyword ="keyword"` 等），是“信号族”在代码里的权威清单。 |
| [src/semantic-router/pkg/classification/classifier_signal_results.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_results.go) | `SignalResults` 结构体：所有信号抽取结果的容器，其中 `SignalConfidences` 与 `SignalValues` 两个 map 是本讲“置信度”模块的核心。 |
| [src/semantic-router/pkg/classification/classifier_signal_rule_evaluators.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_rule_evaluators.go) | 各信号族的真实求值函数：`evaluateKeywordSignal`、`evaluateDomainSignal`、`evaluateReaskSignal`、`evaluateContextSignal` 等，能看出置信度到底从哪来。 |
| [config/signal/](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/signal) | 可复用的 `routing.signals` 配置片段目录，每个信号族一个子目录，是写配置时的“样板间”。 |

## 4. 核心概念与源码讲解

### 4.1 信号族总览

#### 4.1.1 概念说明

SR 把“从请求里抽取事实”这件事，固化成了若干个**信号族（signal family）**。每一族对应一种抽取能力：有的看关键词，有的看话题领域，有的看请求长度，有的看是否疑似越狱。本讲聚焦项目**维护的 16 个核心信号族**，它们覆盖了绝大多数路由场景。

这 16 族可以按“判定方式”分成两大类，这也是项目自身文档采用的分类法（见 `website/docs/tutorials/signal/` 下的 `heuristic/` 与 `learned/` 两个子目录）：

- **启发式 / 规则型（heuristic）**：靠人写的规则判定，命中即命中，本质是布尔 membership。包括 `keyword`、`context`、`structure`、`language`、`authz`。
- **学习型 / 模型驱动（learned）**：靠嵌入或分类器判定，输出带概率或相似度。包括 `domain`、`embedding`、`complexity`、`modality`、`fact_check`、`jailbreak`、`pii`、`kb`、`preference`、`reask`、`user_feedback`。

> **准确性提示**：除了这 16 个核心族，代码里还有 4 个较新的族——`conversation`、`event`、`metadata`、`classifier`（外加一个派生类型 `projection`）。本讲聚焦 16 核心族，新族的思想与对应核心族一致，遇到时按“它属于规则型还是学习型”去理解即可。

#### 4.1.2 核心流程

一次请求进来后，分类器（`Classifier`）会**并发**跑各信号族的求值函数，把结果汇总进一个 `SignalResults` 结构体，流程如下：

```text
请求文本 + 元数据
   │
   ▼
┌──────────── 分类器 Classifier ────────────┐
│ evaluateKeywordSignal   → MatchedKeywordRules      │
│ evaluateDomainSignal    → MatchedDomainRules (+概率) │
│ evaluateContextSignal   → MatchedContextRules       │
│ evaluateReaskSignal     → MatchedReaskRules (+相似度)│
│ ...（其余各族并发求值）...                          │
└──────────────────┬────────────────────────┘
                   ▼
            SignalResults（一个装满命名事实的盒子）
                   │
                   ▼
        交给投影层 / 决策层消费
```

一个关键优化：SR 并不是无脑把所有信号都算一遍。它会先扫描决策规则树，算出**真正被用到的信号集合**，跳过没人引用的信号以节省算力（尤其省嵌入计算）。

#### 4.1.3 源码精读

**信号族的权威清单——类型常量。** 16 个核心族在代码里各有一个字符串常量，这些常量就是信号在 `SignalConfidences` 等 map 里的“姓”：

[config.go:L25-L43](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/config.go#L25-L43) —— 定义了 `SignalTypeKeyword = "keyword"`、`SignalTypeDomain = "domain"`、`SignalTypeEmbedding = "embedding"` 等全部类型常量。信号在结果 map 里的键固定是 `"类型:名字"`，例如 `"domain:health"`、`"keyword:urgent_keywords"`。

**结果容器——`SignalResults`。** 所有信号族的抽取结果都汇聚到这里：

[classifier_signal_results.go:L12-L56](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_results.go#L12-L56) —— 可以看到每一族都有自己的 `Matched*Rules` 字段，比如 `MatchedKeywordRules`、`MatchedDomainRules`、`MatchedContextRules`。这正对应“每一族抽取一类事实”。

**只算用到的信号。** `getAllSignalTypes` 列出全部已配置信号，而 `getUsedSignals` 则只保留决策真正引用的那些：

[classifier_signal_usage.go:L39-L70](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_usage.go#L39-L70) —— 用 `collectSignalKeys` 逐族收集 `"type:name"` 键，说明每一族都被当作一类独立的、可被决策引用的信号。

#### 4.1.4 代码实践

1. **实践目标**：在真实源码里数清 16 个核心信号族，并按“规则型 / 学习型”归类。
2. **操作步骤**：
   - 打开 [config.go:L25-L43](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/config.go#L25-L43)，列出全部 `SignalType*` 常量。
   - 对照本讲 4.1.1 的两大类划分，把每一个常量归入“规则型”或“学习型”。
   - 再打开 `website/docs/tutorials/signal/heuristic/` 与 `website/docs/tutorials/signal/learned/` 两个目录，核对项目官方文档的归类是否与你一致。
3. **需要观察的现象**：官方 `heuristic/` 目录下应有 `keyword.md`、`context.md`、`structure.md`、`language.md`、`authz.md`；`learned/` 目录下应有 `domain.md`、`embedding.md`、`complexity.md` 等。
4. **预期结果**：16 核心族中，规则型 5 个、学习型 11 个（与官方教程目录吻合）。
5. 若你本地仓库的 `website/docs/tutorials/signal/` 下目录数与本讲描述不符，以仓库实际内容为准，并标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`SignalTypeProjection = "projection"` 也是信号常量，它属于 16 核心族吗？为什么？

> **答案**：不属于。`projection` 是**派生信号**——它的值不是从请求里直接抽取的，而是由投影层（score/mapping）计算出来的命名路由带。它消费其它信号，所以本质上是“信号的信号”，不在 16 个原始抽取族之列。

**练习 2**：如果一个信号族在配置里声明了，但没有任何决策引用它，运行时会被求值吗？

> **答案**：默认不会。`getUsedSignals` 只收集决策规则树里引用到的信号键，未引用的信号会被跳过以省算力。这一点对昂贵的 `embedding`、`domain` 等学习型信号尤其重要。

---

### 4.2 典型信号写法

#### 4.2.1 概念说明

知道了有哪些信号族，接下来看**每一族具体怎么写**。SR 提供两种等价的写法：

- **DSL 写法**（`recipe.dsl`）：人读起来顺，是配方作者的主力语言，形如 `SIGNAL keyword <名字> { ... }`。
- **YAML 写法**（`config.yaml` / `config/signal/*.yaml`）：机器读写友好，是运行时实际加载的格式。DSL 在加载前会被编译成 YAML 结构。

两种写法字段一一对应，背后是同一组 Go 结构体（`signal_config.go`）。本小节挑实践任务要求的五类——**keyword、embedding、domain、complexity、context**——逐一拆解。

#### 4.2.2 核心流程

声明一个信号的通用骨架是：

```text
SIGNAL <族名> <这条信号的名字> {
   <族专属的判定字段>
}
```

- `<族名>` 决定走哪个求值器（keyword 走关键词匹配，embedding 走余弦相似度……）。
- `<名字>` 是这条信号在决策里被引用的代号，比如 `keyword("code_request_markers")`。
- 族专属字段决定“怎么算命中”：keyword 看关键词列表，embedding 看候选句与阈值，domain 看分类标签，complexity 看 hard/easy 候选集，context 看 token 区间。

#### 4.2.3 源码精读

**(a) keyword —— 关键词匹配（规则型）。** 一条 keyword 信号声明一个关键词列表和一个聚合算子：

[recipe.dsl:L61-L64](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L61-L64) —— `correction_feedback_markers` 信号，`operator: "OR"` 表示只要命中列表里任意一个词（如 “that's wrong”、“错了”、“重新回答”）就算这条信号命中。背后结构体 `KeywordRule`：

[signal_config.go:L44-L55](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/signal_config.go#L44-L55) —— 可见 keyword 除了纯字符串匹配，还支持 `method: bm25` / `method: ngram` 等模糊匹配方式（对应 `config/signal/keyword/nlp.yaml`），以及大小写开关 `case_sensitive`。它本质是规则匹配，所以命中即 1、不命中即 0。

**(b) embedding —— 语义意图匹配（学习型）。** embedding 信号靠“请求向量 vs 候选锚点句向量”的余弦相似度判定：

[recipe.dsl:L141-L145](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L141-L145) —— `fast_qa_en` 信号：候选句是一批典型“快问快答”问题（如 “What is 2 + 2?”），`threshold: 0.72` 表示请求与任意候选句的相似度超过 0.72 才算命中，`aggregation_method: "max"` 表示取所有候选里的最大相似度。结构体：

[signal_config.go:L83-L92](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/signal_config.go#L83-L92) —— `EmbeddingRule` 的 `Candidates` 是锚点句列表，`SimilarilarityThreshold` 是门槛，`AggregationMethodConfiged` 支持 `mean`/`max`/`any` 三种聚合。相似度的数学定义是归一化向量的点积：

\[
\text{sim}(q, c) = \cos(\theta) = \frac{q \cdot c}{\lVert q\rVert \,\lVert c\rVert}
\]

**(c) domain —— 话题领域分类（学习型）。** domain 信号声明一个领域标签，靠分类器给出该领域的概率：

[recipe.dsl:L5-L31](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L5-L31) —— 一连串 `SIGNAL domain <名字>`，如 `health`、`law`、`math`。每个领域还能映射到 MMLU 标准类目，便于复用公开评测的分类器（见 `config/signal/domain/mmlu.yaml`）。分类器对所有领域做一次 softmax，每个领域拿到一个概率。

**(d) complexity —— 难度评估（学习型）。** complexity 信号比较请求更像“难题”还是“易题”：

[recipe.dsl:L317-L322](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L317-L322) —— `general_reasoning`：`hard.candidates` 是一批难题样句（如 “build a rigorous step-by-step argument”），`easy.candidates` 是易题样句（如 “brief definition”），`threshold: 0.14` 是难度分界。结构体：

[signal_config.go:L278-L285](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/signal_config.go#L278-L285) —— `ComplexityRule` 还有一个可选的 `Composer`（`RuleCombination`），允许把难度判定**限定在某个领域内**，例如 `code_task` 只在 `domain("computer science")` 为真时才评估（见 `recipe.dsl` L324-L330）。求值后输出的是带级别的结果，如 `general_reasoning:hard`、`code_task:medium`。

**(e) context —— token 区间（规则型）。** context 信号按请求的 token 数落在哪个区间：

[recipe.dsl:L263-L276](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L263-L276) —— `short_context`（0–999）、`medium_context`（1K–7999）、`long_context`（8K–256K）三条信号。注意 token 数写成字符串 `"1K"`、`"8K"`，会被解析成数字。结构体：

[signal_config.go:L197-L202](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/signal_config.go#L197-L202) —— `ContextRule` 用 `MinTokens` / `MaxTokens` 两个 `TokenCount` 字段界定区间。`TokenCount` 支持人类友好的 `K`/`M` 后缀（见同文件 L173-L195 的 `Value()` 方法，`"1K"` → `1000`）。命中即属于该区间，是典型的规则型布尔判定。

> 补充一类没在实践任务里但很常见——**structure**：检测请求的**结构特征**，如是否含编号列表（正则 `^\s*\d+\.\s+`）、是否含“first…then…”顺序词、约束词密度、问号密度、感叹号数量。它的特别之处在于同时输出**原始值**（密度比值 / 计数）和置信度，见 [recipe.dsl:L278-L315](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L278-L315) 与结构体 [signal_config.go:L204-L222](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/signal_config.go#L204-L222)。

#### 4.2.4 代码实践

1. **实践目标**：在 `balance` 配方里挑出 keyword、embedding、domain、complexity、context 各一条，解释其判定条件，并找到 `config/signal/` 下对应族的 YAML 片段。
2. **操作步骤**：
   - 在 [recipe.dsl](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl) 中分别定位：
     - keyword：`code_request_markers`（L116-L119）
     - embedding：`fast_qa_en`（L141-L145）
     - domain：`health`（L29-L31）
     - complexity：`code_task`（L324-L330）
     - context：`long_context`（L273-L276）
   - 逐条写下：它匹配什么、阈值/算子是什么、命中后会被决策怎样引用（如 `keyword("code_request_markers")`）。
   - 打开 `config/signal/` 对应子目录，对照 YAML 写法：
     - [config/signal/keyword/regex.yaml](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/signal/keyword/regex.yaml)
     - [config/signal/embedding/support.yaml](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/signal/embedding/support.yaml)
     - [config/signal/domain/mmlu.yaml](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/signal/domain/mmlu.yaml)
     - [config/signal/complexity/escalation.yaml](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/signal/complexity/escalation.yaml)
     - [config/signal/context/long-context.yaml](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/signal/context/long-context.yaml)
3. **需要观察的现象**：YAML 片段里字段名（`keywords`、`threshold`、`candidates`、`min_tokens`）与 DSL 完全对应，只是多了 `routing.signals.<族复数>` 的外层包裹；`config/signal/` 是“可复用片段”，`recipe.dsl` 是“完整配方里的一次具体声明”。
4. **预期结果**：你能用一句话说清每条信号的判定条件，例如：“`code_request_markers` 是 OR 关键词信号，请求里出现 code/function/debug/sql 等任一词即命中。”
5. 这些都是源码阅读型步骤，无需运行；如要运行校验，可用 `go run ./cmd/dsl validate config/recipes/balance/recipe.dsl`（命令用法见 u7-l3），结果标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`fast_qa_en` 的 `aggregation_method: "max"` 改成 `"mean"` 会怎样影响命中？

> **答案**：`max` 取所有候选句里的最高相似度，只要有一个候选句很像就过阈值，偏“宽容”；`mean` 取平均，会被大量不相似的候选句拉低，更“严格”。对于“快问快答”这种只要沾边就该走便宜通道的场景，`max` 更合适。

**练习 2**：`complexity` 的 `composer: { operator: "AND", conditions: [{ type: "domain", name: "computer science" }] }` 起什么作用？

> **答案**：它把这条 complexity 信号的评估**限定在计算机科学领域内**。只有当 `domain("computer science")` 同时为真，`code_task` 的难度判定才会生效。这样避免了“在法律问题里误用代码难度阈值”。

---

### 4.3 信号置信度

#### 4.3.1 概念说明

一条信号被抽取后，不只是“命中 / 没命中”这么简单。SR 给每条信号配了**两个数值通道**：

- **confidence（置信度）**：这条匹配有多可信，范围 \([0, 1]\)。这是投影层算分数时最常引用的数值（DSL 里写作 `value_source: "confidence"`）。
- **value（原始值）**：这条信号背后的原始测量值，比如 structure 的“约束词密度 0.12”、reask 的“重复了 2 轮”。并非每条信号都有。

最关键的区别在于**置信度的来源**：

- **规则型信号**（keyword、context）：命中是布尔的，所以置信度恒为 **1.0**。
- **学习型信号**（domain、embedding、reask、kb 等）：置信度是模型输出的**真实概率或相似度**，有灰度。

这一节我们用源码把这个区别钉死。

#### 4.3.2 核心流程

信号抽取时，求值函数会往两个 map 里写数值：

```text
求值函数 evaluateXxxSignal
   │
   ├──► results.SignalConfidences["族:名"] = 置信度   # 投影 score 用它做加权
   └──► results.SignalValues["族:名"]     = 原始值    # 结构特征/重复轮数等
```

- 键的格式统一是 `"族:名"`，族用 [config.go:L25-L43](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/config.go#L25-L43) 的常量，如 `"domain:health"`、`"reask:likely_dissatisfied"`。
- 投影层读置信度时有个兜底规则：**如果某条信号命中了但没有写置信度，就当 1.0 处理**。这正好解释了规则型信号为何等价于 1.0。

#### 4.3.3 源码精读

**两个数值通道的定义。**

[classifier_signal_results.go:L50-L51](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_results.go#L50-L51) —— `SignalConfidences map[string]float64`（注释举例 `"embedding:ai" → 0.88`）与 `SignalValues map[string]float64`（举例 `"structure:many_questions" → 4`）。

**规则型：置信度恒为 1.0。** 看 keyword 与 context：

[classifier_signal_rule_evaluators.go:L26-L28](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_rule_evaluators.go#L26-L28) —— keyword 求值后直接写 `results.Metrics.Keyword.Confidence = 1.0`，注释明说 “Rule-based, always 1.0”。

[classifier_signal_rule_evaluators.go:L231-L232](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_rule_evaluators.go#L231-L232) —— context 同样写 `Confidence = 1.0`，因为它只是判断 token 数落在哪个区间。

**学习型：写入真实概率/相似度。** 看 domain 与 reask：

[classifier_signal_rule_evaluators.go:L99-L103](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_rule_evaluators.go#L99-L103) —— domain 把分类器的概率写进 `SignalConfidences["domain:"+Category]`，值是该领域的 softmax 概率。

[classifier_signal_rule_evaluators.go:L217-L220](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_rule_evaluators.go#L217-L220) —— reask 同时写两个通道：`SignalConfidences["reask:"+rule] = MinSimilarity`（与上一轮的余弦相似度），`SignalValues["reask:"+rule] = MatchedTurns`（重复了几个历史轮次）。这是“置信度 + 原始值”双通道的最佳样例。

**结构特征也走双通道。**

[classifier_signal_structure.go:L33-L34](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_structure.go#L33-L34) —— structure 求值时 `SignalValues[key] = match.Value`（密度比值或计数）与 `SignalConfidences[key] = match.Confidence` 同时写入。

**投影层读置信度的兜底（关键）。** 当一条信号命中却没有显式置信度时怎么办？

[classifier_projection_inputs.go:L70-L76](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_projection_inputs.go#L70-L76) —— 找不到对应键时 `return 1.0`。也就是说：**命中即默认置信度 1.0**。这正是 keyword/context 这类规则型信号在 `difficulty_score` 投影里以 `value_source: "confidence"` 参与加权、却等价于满权重的根本原因；而 embedding/domain 用的是它们真实的概率/相似度。

把两个例子放在一起对比：

| 信号 | 类型 | confidence 来源 | 投影里读到的值 |
| --- | --- | --- | --- |
| `keyword("code_request_markers")` 命中 | 规则型 | 恒 1.0 | 默认 1.0（满权重） |
| `embedding("fast_qa_en")` 命中，相似度 0.81 | 学习型 | 余弦相似度 0.81 | 真实 0.81（灰度权重） |

#### 4.3.4 代码实践

1. **实践目标**：用源码确认“规则型 1.0、学习型真实值”这条规律，并理解它对加权打分的影响。
2. **操作步骤**：
   - 打开 [recipe.dsl:L377-L380](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L377-L380) 的 `difficulty_score` 投影，找到这两条输入：
     - `{ type: "keyword", weight: -0.26, name: "simple_request_markers" }`
     - `{ type: "embedding", weight: 0.18, name: "fast_qa_en", value_source: "confidence" }`
   - 假设两条都命中，且 `fast_qa_en` 相似度为 0.80。
   - 手算它们对 `difficulty_score` 的贡献：
     - keyword：\(-0.26 \times 1.0 = -0.26\)
     - embedding：\(0.18 \times 0.80 = 0.144\)
   - 再对照 [classifier_projection_inputs.go:L70-L76](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_projection_inputs.go#L70-L76) 验证：keyword 没有显式置信度 → 取默认 1.0；embedding 取真实 0.80。
3. **需要观察的现象**：同样是 `value_source: "confidence"`，规则型永远贡献满权重，学习型贡献按相似度打折。这意味着学习型信号的“强弱”会真实影响最终分数，而规则型只有“在/不在”之分。
4. **预期结果**：你能解释为什么 SR 更倾向用 embedding/domain 这类带灰度的信号来打“难度分”，而把 keyword 多用于硬性的条件门控（WHEN 规则）。
5. 上述为源码阅读 + 手算型实践，结果可自行推演；若想看真实分数，需启动服务并触发投影追踪（见 u11-l2），此处标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `evaluateKeywordSignal` 没有往 `SignalConfidences` 里写 `"keyword:..."`，但 keyword 信号仍能在 `difficulty_score` 里以 `value_source: "confidence"` 正常参与加权？

> **答案**：因为投影层读取时有兜底——`classifier_projection_inputs.go` 的 `projectionInputConfidenceValue` 在找不到键时返回 `1.0`。keyword 是规则型，命中即等价于置信度 1.0，所以不写也无所谓。

**练习 2**：`reask` 信号为什么同时往 `SignalConfidences` 和 `SignalValues` 两个 map 里写？分别表达什么？

> **答案**：`SignalConfidences["reask:..."]` 存的是当前轮与上一轮的**余弦相似度**（越像越像在重复提问，置信度越高）；`SignalValues["reask:..."]` 存的是**重复命中的历史轮数** `MatchedTurns`（一个计数原始值）。前者用于加权，后者可被需要“次数”的规则或可观测性消费。

**练习 3**：如果想让一条 keyword 信号的命中也能有“灰度”（比如部分匹配给 0.5），应该怎么做？

> **答案**：改用 keyword 支持的模糊匹配方法——`method: bm25`（设 `bm25_threshold`）或 `method: ngram`（设 `ngram_threshold`、`ngram_arity`），见 [config/signal/keyword/nlp.yaml](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/signal/keyword/nlp.yaml)。这类方法的匹配度会被写进置信度，从而带出灰度。

## 5. 综合实践

把本讲三个模块串起来，完成一次“信号体检”小任务：

**任务**：为 `balance` 配方里的 `fast_qa` 路由（[recipe.dsl:L650-L662](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L650-L662)）画一张“信号依赖图”，并回答三个问题。

1. **收集信号**：通读 `fast_qa` 路由的 `WHEN` 规则，列出它直接引用的全部信号，按族分组。预期会涉及 `embedding`（fast_qa_en/zh）、`language`（en/zh）、`keyword`（simple_request_markers、verification_markers 等）、`structure`（low_question_density）、`context`（short_context）、`projection`（balance_simple 等）、`domain`、`user_feedback`、`reask`。
2. **标注类型与置信度**：对每个信号标注它是规则型还是学习型，以及它的置信度是 1.0 还是真实概率/相似度。
3. **解释设计意图**：用你标注的结果回答——为什么 `fast_qa` 这条“便宜通道”要用 `embedding + language` 双重确认（如 `embedding("fast_qa_en") AND language("en")`），却用 `context("short_context")` 做硬门控？提示：结合 4.3 学到的“学习型带灰度、规则型做硬门控”。

**交付物**：一张依赖图（手绘或文字列表皆可）+ 一段 3～5 句的设计意图说明。

**预期收获**：你会真切看到，一条路由其实是“用学习型信号判断语义意图、用规则型信号画硬边界”的组合体——这正是 SR 信号层的核心用法。

## 6. 本讲小结

- SR 维护 **16 个核心信号族**（外加 conversation/event/metadata/classifier 等较新族），每一族负责从请求里抽取一类事实，结果汇聚进 `SignalResults`。
- 信号分两大类：**规则型**（keyword/context/structure/language/authz，命中即 1.0）与**学习型**（domain/embedding/complexity 等，输出真实概率或相似度）。项目教程用 `heuristic/` 与 `learned/` 目录区分。
- 典型写法遵循 `SIGNAL <族> <名> { 族专属字段 }`：keyword 看关键词列表与算子，embedding 看候选句 + 阈值 + 余弦相似度，domain 看分类标签 + softmax 概率，complexity 看 hard/easy 候选集（可按领域限定），context 看 token 区间（支持 `"1K"` 写法）。
- 每条信号有两个数值通道：`SignalConfidences`（置信度，投影加权用）与 `SignalValues`（原始值，如密度、轮数）。键统一为 `"族:名"`。
- 投影层读置信度有兜底：命中但无显式置信度时取 **1.0**，这正是规则型信号等价于满权重的根因，也解释了为何打“难度分”更爱用带灰度的学习型信号。
- SR 只求值**决策真正引用**的信号，避免无用的嵌入计算。

## 7. 下一步学习建议

- **下一讲 u2-l3（Projections）**：本讲反复提到的 `difficulty_score`、`value_source: "confidence"`、`threshold_bands` 都属于投影层。建议接着学投影，看信号如何被加权求和、切成命名路由带。
- **u2-l4（Decisions/Routes/Models）**：看 `WHEN` 规则如何用 AND/OR/NOT 组合本讲这些信号，以及 `keyword("...")`、`domain("...")` 这类引用在决策引擎里如何求值。
- **深入阅读**：`website/docs/tutorials/signal/overview.md` 与各信号族教程页，是项目官方对信号层的系统说明，可作为本讲的权威补读材料。
