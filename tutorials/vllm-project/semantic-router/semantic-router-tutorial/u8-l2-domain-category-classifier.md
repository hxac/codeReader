# 域/类别分类器

## 1. 本讲目标

本讲是「分类信号系统」的第二篇，承接 u8-l1（分类编排与信号求值）中对 `Classifier` 编排器的整体认识，专门拆解其中的**域/类别（domain/category）信号**如何被算出来。

学完本讲你应该能够：

- 说清楚一次请求的文本如何被域分类模型转成「类别名 + 置信度 + 全概率分布」，以及 MMLU 标签到通用类别名的翻译过程。
- 说清楚 **Shannon 熵** 如何决定域信号是输出「单一最可能类别」还是「多个并立类别」，并能手算归一化熵。
- 说清楚基于示例（exemplar）的 **KB 增强分类** 如何用原型（prototype）簇打分，并把结果映射成 `kb` 信号。
- 解释为什么决策规则里的 `domain("health")` 即使底层模型吐出的是细粒度 MMLU 标签，也能正确命中 health 类别。

本讲只讲「域/类别信号怎么算」，**不讲**这条信号如何被决策引擎的布尔树消费（那是 u6-l1），也**不讲**信号族的并发编排（那是 u8-l1）。

## 2. 前置知识

在进入源码前，先用大白话把三个关键概念讲清楚。

### 2.1 什么是「域 / 类别」

一条用户提问往往带有一个或多个**学科领域**，比如「帮我解释这段 Python 代码」属于 *computer science*，「怀孕初期可以吃布洛芬吗」属于 *health*。Semantic Router（下称 SR）把这个判断交给一个**域分类模型**——它是一个在 [MMLU-Pro](https://arxiv.org/abs/2406.01515) 数据集上训练的多类别分类器（BERT / ModernBERT），能输出 14 个左右学科类别上的概率分布。

关键点：模型吐出的标签是 **MMLU 原生名**（如 `computer science`、`math`、`health`），而用户在配置/DSL 里写的决策规则可能用**更粗的通用名**（如把 `computer science` 和 `engineering` 合并成 `tech`）。所以中间需要一层翻译。

### 2.2 什么是「熵（Entropy）」

熵是信息论里衡量「一个概率分布有多不确定」的指标。直觉上：

- 如果模型把 99% 的概率压在某一个类别上（其余类别接近 0），分布很「尖」，**熵很低**，说明模型很笃定。
- 如果概率被均匀摊在好几个类别上，分布很「平」，**熵很高**，说明模型很犹豫。

SR 用熵来做一个聪明的决策：**模型笃定时只报它最可能的那一个类别；模型犹豫时把所有够格的类别都报出来**，让下游决策规则自己组合。这就避免了「硬选 top-1 却选错」的问题。

### 2.3 什么是「基于示例的 KB 分类」

域分类模型是**预先训练好、固定 14 类**的。但有时用户想要**自定义类别**（比如自己定义「合规风险」「客服工单」），又不想重新训练模型。SR 提供了一条「示例驱动」的旁路：

- 用户给每个自定义类别写一段 `description`（描述）和若干 `exemplars`（示例句）。
- 系统把这些示例句预先嵌入成向量，聚成几个**原型（prototype）**。
- 来一条新请求时，把请求也嵌入成向量，和每个类别的原型算余弦相似度，谁近就算谁。

这就是 **KB 增强分类（Knowledge-Base-augmented classification）**。它和域分类模型并列，输出独立的 `kb` 信号。

> 名词速查：MMLU、置信度（confidence）、全概率分布（full probability distribution）、余弦相似度（cosine similarity）、原型（prototype）。不熟悉的术语后文都会结合源码再讲一遍。

## 3. 本讲源码地图

本讲涉及的关键文件（都在 `src/semantic-router/pkg/` 下）：

| 文件 | 作用 |
|------|------|
| `classification/classifier_signal_rule_evaluators.go` | 域信号的求值入口 `evaluateDomainSignal`，调模型、翻译、写回信号。 |
| `classification/category_classifier.go` | 域类别的阈值筛选 `matchDomainCategories`、MMLU↔通用名双向映射、熵分支。 |
| `utils/entropy/entropy.go` | Shannon 熵的计算、归一化、不确定度分级。 |
| `classification/mapping.go` | `CategoryMapping`：模型类索引 ↔ MMLU 类别名的映射。 |
| `classification/classifier_category_entropy.go` | 把熵分析用于「是否启用推理」的更高层决策（辅助理解熵的复用）。 |
| `classification/category_kb_classifier.go` | KB 分类器主体：加载清单、嵌入、打分、产出 `KBClassifyResult`。 |
| `classification/category_kb_scoring.go` | KB 的逐标签打分、阈值筛选、分组、结果合成。 |
| `classification/category_kb_embeddings.go` | KB 示例向量的并发预嵌入与原型簇构建。 |
| `classification/prototype_bank.go` / `prototype_scoring.go` | 原型簇的构建与打分公式。 |
| `classification/classifier_signal_taxonomy.go` | 把 KB 结果映射成 `kb` 信号的 `evaluateKBSignals`。 |
| `decision/engine.go` | 决策引擎里 `domain` 信号的特殊匹配逻辑（解释实践任务）。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**类别匹配**、**熵分析**、**KB 增强分类**。最后用一节单独回答实践任务里的 `domain("health")` 匹配问题。

---

### 4.1 类别匹配：从模型输出到域信号

#### 4.1.1 概念说明

「类别匹配」回答的问题是：**域分类模型给出一个类索引和置信度后，怎么把它变成决策规则能引用的 `domain:<名字>` 信号？**

这个过程分三步：

1. **调用模型**：拿到类索引（`Class`）、置信度（`Confidence`）、全概率分布（`Probabilities`）。
2. **索引→名字**：用 `CategoryMapping` 把类索引翻成 MMLU 类别名，再用 `translateMMLUToGeneric` 翻成用户配置的通用名。
3. **阈值筛选**：用 `matchDomainCategories` 决定哪些类别「够格」被写进信号。

#### 4.1.2 核心流程

```text
evaluateDomainSignal(text)
  ├── categoryInference.ClassifyWithProbabilities(text)   # 调模型
  │       → domainResult{Class, Confidence, Probabilities}
  ├── CategoryMapping.GetCategoryFromIndex(domainResult.Class)
  │       → MMLU 名字（如 "computer science"）
  ├── translateMMLUToGeneric(MMLU 名字)
  │       → 通用名字（如 "tech"，若无映射则原样返回）
  └── matchDomainCategories(domainResult, 通用名字)        # 见 4.2
          → []CategoryProbability{Category, Probability}
```

最后把每个匹配类别写回 `SignalResults`：

```text
results.MatchedDomainRules = append(..., cat.Category)
results.SignalConfidences["domain:"+cat.Category] = cat.Probability
```

注意键名格式是 `"domain:<名字>"`——这正是 u2-l2 讲过的「`族:名`」键约定。

#### 4.1.3 源码精读

先看求值入口 `evaluateDomainSignal`。它先用 `ClassifyWithProbabilities` 拿全概率分布；如果该接口不可用，就退回只给 top-1 的 `Classify`：

[classifier_signal_rule_evaluators.go:58-106](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_rule_evaluators.go#L58-L106) — 调模型、把类索引翻成名字、记录延迟指标，最后调用 `matchDomainCategories` 把结果写回 `SignalResults`。

其中两行最关键：

```go
// 类索引 → MMLU 名字
if name, ok := c.CategoryMapping.GetCategoryFromIndex(domainResult.Class); ok {
    categoryName = c.translateMMLUToGeneric(name)   // MMLU → 通用名
}
```

`CategoryMapping` 是从磁盘上的 JSON 文件加载的「索引↔类别名」表，结构很简单：

[mapping.go:11-16](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/mapping.go#L11-L16) — `IdxToCategory` 把模型的整数类索引（以字符串形式）映射成 MMLU 类别名。

`GetCategoryFromIndex` 就是查这张表：

[mapping.go:96-99](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/mapping.go#L96-L99) — 把 `int` 类索引格式化成字符串键去查 `IdxToCategory`。

接下来是 MMLU→通用名的翻译。这层翻译由配置里的 `Categories` 段驱动。每个类别可以声明一组 `mmlu_categories`，表示「这些 MMLU 细标签都归到我这个通用名下」：

[signal_config.go:305-309](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/signal_config.go#L305-L309) — `CategoryMetadata` 的 `MMLUCategories` 字段。

构建期，`buildCategoryNameMappings` 把这份配置加工成两张反向查找表：

[category_classifier.go:64-90](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/category_classifier.go#L64-L90) — 遍历 `Config.Categories`，对每个声明了 `MMLUCategories` 的类别，建立 `MMLUToGeneric`（细→粗）和 `GenericToMMLU`（粗→细）两张表。

翻译函数本身很克制——查不到就原样返回，绝不报错：

[category_classifier.go:92-104](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/category_classifier.go#L92-L104) — `translateMMLUToGeneric`：有映射就翻成通用名，没映射就把原始 MMLU 名直接当类别名用。

> **一个重要推论**：如果一个 MMLU 标签没有被任何配置类别收录，它就会以「原始 MMLU 名」的身份进入域信号。比如模型吐出 `medicine`，但配置里没有哪个类别把 `medicine` 列进 `mmlu_categories`，那么信号键就是 `domain:medicine` 而非某个通用名。这一点在 4.4 节解释别名匹配时非常关键。

#### 4.1.4 代码实践

**实践目标**：理解一段示例文本经过域分类后，会产出哪些类别概率。

**操作步骤**：

1. 打开 [classifier_signal_rule_evaluators.go:97-104](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_rule_evaluators.go#L97-L104)，确认域信号最终被写进 `MatchedDomainRules` 与 `SignalConfidences["domain:"+cat.Category]`。
2. 假设输入文本是 `"What are the early signs of pregnancy and is ibuprofen safe?"`，设底层域分类模型在该句上的概率分布（节选）为：

   | 类别（MMLU） | 概率 |
   |------------|------|
   | health     | 0.62 |
   | biology    | 0.21 |
   | other      | 0.10 |
   | …其余类     | 剩余 |

3. 假设配置里没有为 `health`/`biology` 配 `mmlu_categories` 映射，则 `translateMMLUToGeneric` 原样返回 `health`、`biology`。

**需要观察的现象**：在熵较高时（见 4.2），`health` 与 `biology` 两个类别**都可能**被同时写进 `SignalConfidences`，键分别是 `domain:health`、`domain:biology`，值就是各自的概率。

**预期结果**：域信号不是「只保留 top-1」，而是一个**类别集合**，每个类别带自己的概率。这是域信号区别于规则型布尔信号（命中即 1.0）的地方——它天然携带多类别灰度。

> 上述概率数值是**示例数据**（非项目原有代码），用于说明机制；真实数值待本地用模型跑出后验证。

#### 4.1.5 小练习与答案

**练习 1**：如果模型类索引是 `5`，`IdxToCategory` 里 `"5" → "engineering"`，但配置里 `Categories` 把 `engineering` 映射到了通用名 `tech`，那么最终写进 `SignalConfidences` 的键是什么？

**答案**：键是 `domain:tech`。因为 `GetCategoryFromIndex(5)` 返回 `engineering`，再经 `translateMMLUToGeneric("engineering")` 翻成 `tech`。

**练习 2**：为什么 `translateMMLUToGeneric` 在查不到映射时要原样返回，而不是返回空字符串？

**答案**：因为域分类模型的标签空间（14 个 MMLU 类）远大于用户可能配置的通用类别数。绝大多数 MMLU 标签不会有显式映射，原样返回能保证这些类别仍以真实名字参与路由；若返回空串，这些类别会被静默丢弃，导致 `domain("math")` 这类直接引用 MMLU 名的规则永远无法命中。

---

### 4.2 熵分析：top-1 还是多类别？

#### 4.2.1 概念说明

「熵分析」回答的问题是：**模型给了一整条概率分布，域信号到底该报一个类别还是多个类别？**

SR 的策略很优雅：**用归一化 Shannon 熵衡量模型的不确定度，不确定度低就只信 top-1，不确定度高就把所有过阈值的类别都报出来**。这样既能精确路由「明确单一领域」的请求，又不丢失「跨领域」请求里的次要类别证据。

#### 4.2.2 核心流程与数学

**Shannon 熵**衡量一个概率分布 \(p_1, p_2, \dots, p_N\) 的不确定度：

\[
H = -\sum_{i=1}^{N} p_i \log_2 p_i
\]

（只累加 \(p_i > 0\) 的项。）\(H\) 的单位是 bit。分布越平均，\(H\) 越大；全部概率集中在一个类别时 \(H = 0\)。

但 \(H\) 的绝对值依赖类别数 \(N\)——14 个类的最大熵是 \(\log_2 14 \approx 3.81\)，3 个类的最大熵只有 \(\log_2 3 \approx 1.58\)。为了跨场景可比，SR 用**归一化熵**：

\[
H_{\text{norm}} = \frac{H}{\log_2 N} \in [0, 1]
\]

\(H_{\text{norm}} = 0\) 表示完全确定，\(H_{\text{norm}} = 1\) 表示完全平均（最不确定）。

随后按阈值把 \(H_{\text{norm}}\) 分成五档不确定度：

| 归一化熵范围 | 不确定度等级 |
|------------|------------|
| \([0.8, 1.0]\) | `very_high` |
| \([0.6, 0.8)\) | `high` |
| \([0.4, 0.6)\) | `medium` |
| \([0.2, 0.4)\) | `low` |
| \([0.0, 0.2)\) | `very_low` |

最后在 `matchDomainCategories` 里按等级分流：

```text
matchDomainCategories(domainResult, topCategoryName):
  threshold = Config.CategoryModel.Threshold
  若 Probabilities 为空 → 只能用 top-1 的 Confidence
  否则:
    entropyResult = AnalyzeEntropy(Probabilities)
    switch entropyResult.UncertaintyLevel:
      case "very_low", "low":   # 模型笃定 → 只报 top-1（若过阈值）
        return [ {topCategoryName, Confidence} ]
      default:                  # 模型犹豫 → 报所有过阈值的类别
        return 所有 prob >= threshold 的类别
```

#### 4.2.3 源码精读

熵的核心计算在 `entropy.go`：

[entropy.go:24-37](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/utils/entropy/entropy.go#L24-L37) — `CalculateEntropy` 实现 Shannon 熵公式 \(H = -\sum p_i \log_2 p_i\)，跳过 \(p_i = 0\) 的项。

[entropy.go:40-53](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/utils/entropy/entropy.go#L40-L53) — `CalculateNormalizedEntropy` 把熵除以 \(\log_2 N\) 归一化到 \([0,1]\)；类别数 \(\le 1\) 时直接返回 0。

[entropy.go:56-82](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/utils/entropy/entropy.go#L56-L82) — `AnalyzeEntropy` 一次性产出 `Entropy / NormalizedEntropy / Certainty (=1-归一化熵) / UncertaintyLevel`，五档分级就在这里的 `switch`。

分流逻辑在 `matchDomainCategories`：

[category_classifier.go:13-62](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/category_classifier.go#L13-L62) — 域类别的阈值筛选主体。注意三个细节：

1. **早退**（L20-27）：若模型没给全概率分布（`Probabilities` 为空），退化成「top-1 过阈值就报，否则返回 nil」。
2. **笃定分支**（L42-47）：`very_low`/`low` 时只报 `topCategoryName` 一个，置信度用 `domainResult.Confidence`。
3. **犹豫分支**（L48-57）：`medium`/`high`/`very_high` 时遍历整条分布，把每个 `prob >= threshold` 且名字非空的类别都收进来，**每个类别带它自己的原始概率**。

阈值来自配置：

[model_config_types.go:17-28](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/model_config_types.go#L17-L28) — `CategoryModel.Threshold` 是「一个类别要被算作命中」的最低概率。

> **熵的复用**：熵分析不只用在域信号的多类别决策上。`classifier_category_entropy.go` 里的 `classifyCategoryWithEntropyInTree` 还把它用于「这条请求是否需要启用 reasoning（推理）模型」的更高层判断——不确定度高时更倾向启用推理以保证安全。同一套 `AnalyzeEntropy` 服务于两处，可见它是一个被有意提取出来的通用工具。参见 [classifier_category_entropy.go:95-183](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_category_entropy.go#L95-L183)。

#### 4.2.4 代码实践

**实践目标**：手算一段概率分布的归一化熵与不确定度等级，验证它走哪个分支。

**操作步骤**：

1. 假设某请求的域概率分布（只列 4 类，便于手算）为 \(p = [0.7, 0.1, 0.1, 0.1]\)，类别数 \(N = 4\)。
2. 按 `CalculateEntropy` 的定义手算：

\[
H = -(0.7\log_2 0.7 + 3 \times 0.1\log_2 0.1)
\]

   - \(0.7\log_2 0.7 \approx 0.7 \times (-0.5146) \approx -0.3602\)
   - \(0.1\log_2 0.1 \approx 0.1 \times (-3.3219) \approx -0.3322\)，三项共 \(-0.9966\)
   - \(H \approx 0.3602 + 0.9966 = 1.3568\) bit
3. 归一化：\(\log_2 4 = 2\)，故 \(H_{\text{norm}} = 1.3568 / 2 \approx 0.678\)。
4. 查分档表：\(0.678 \in [0.6, 0.8)\) → `high`。

**需要观察的现象**：不确定度是 `high`（≥ `medium`），因此 `matchDomainCategories` 走**犹豫分支**，会遍历整条分布报出所有 `prob >= threshold` 的类别。

**预期结果**：若 `threshold = 0.3`，则只有 `0.7` 那一个类别过阈值，输出单一类别；若 `threshold = 0.05`，则四个类别（\(0.7, 0.1, 0.1, 0.1\)）全都过阈值，输出四个并立类别。可见**「报几个」同时由熵档位和阈值共同决定**——熵档位决定「要不要看多个」，阈值决定「哪些够格」。

> 上面的手算结果是**示例推演**，建议在本地写一个调用 `entropy.AnalyzeEntropy([]float32{0.7,0.1,0.1,0.1})` 的小测试核对 `NormalizedEntropy` 与 `UncertaintyLevel`。

#### 4.2.5 小练习与答案

**练习 1**：分布 \(p = [1, 0, 0, \dots, 0]\)（共 \(N\) 类）的归一化熵是多少？会走哪个分支？

**答案**：\(H = -(1\cdot\log_2 1 + 0 + \dots) = 0\)，\(H_{\text{norm}} = 0\)，等级 `very_low`，走 top-1 分支，只报最可能的那个类别。

**练习 2**：为什么用「归一化熵」而不是直接用原始熵做分档？

**答案**：原始熵的上界 \(\log_2 N\) 随类别数变化。固定阈值（如 0.6）在 14 类和 3 类下对应的「绝对不确定度」完全不同。归一化后映射到 \([0,1]\)，同一套阈值在任何类别数下都表示同一档「相对不确定度」，分档才稳定可比。

---

### 4.3 KB 增强分类：基于示例的原型分类

#### 4.3.1 概念说明

域分类模型只能输出**固定的 14 个 MMLU 类**。当用户需要**自定义类别**（且不想重新训练模型）时，SR 提供 `KnowledgeBaseClassifier`——一条「示例驱动」的分类旁路：

- 用户在一个 **KB 清单**（`labels.json`）里为每个自定义类别写描述和示例句。
- 启动时把所有示例句嵌入成向量，按相似度聚成少量**原型（prototype）**。
- 请求到来时，把请求嵌入，和每个类别的原型算余弦相似度，按公式打分；分数过阈值即命中。
- 命中的类别经 `evaluateKBSignals` 映射成普通的 `kb:<名字>` 信号，供决策规则引用。

它和域分类模型**并列**：域分类产 `domain:*` 信号，KB 分类产 `kb:*` 信号，互不干扰。

#### 4.3.2 核心流程

```text
启动期（一次性）：
  loadDefinition()           # 读 labels.json：每个 label 的 description + exemplars
  shouldDeferPreload()?      # candle/远程后端 → 延迟预嵌入；否则立即预嵌入
  preloadEmbeddings()        # 并发嵌入所有 exemplar → 向量
    └── rebuildLabelPrototypeBanks()  # 每个 label 的向量聚成 prototype 簇

请求期（每条请求）：
  Classify(text):
    embedText(text)                       # 请求嵌入
    computeLabelScores(queryEmb)          # 每个 label 用 prototype 簇打分
    buildResultFromLabelScores(...)       # 合成 KBClassifyResult（best/matched/groups/metrics）

信号映射（每条请求，在 evaluateKBSignals 里）：
  对每条 KBSignalRule：
    根据 target.kind(label/group) 与 match(best/threshold)
    → 命中则写 MatchedKBRules + SignalConfidences["kb:"+rule.Name]
```

#### 4.3.3 源码精读

**KB 分类器主体与生命周期**：

[category_kb_classifier.go:40-49](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/category_kb_classifier.go#L40-L49) — `KnowledgeBaseClassifier` 持有规则（`config.KnowledgeBaseConfig`）、标签数据（`labels`）、嵌入 provider 等。每个标签的运行时数据结构是 `kbLabelData`，含描述、示例、嵌入向量，以及聚好的 `Prototype` 簇。

[category_kb_classifier.go:55-78](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/category_kb_classifier.go#L55-L78) — 构造函数：先 `loadDefinition` 读清单，再决定是否延迟预嵌入。注意 `shouldDeferPreload`（[L87-90](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/category_kb_classifier.go#L87-L90)）：当后端是空/`candle` 或显式传了 provider 时延迟，否则立即预嵌入——这是为了避开 candle 后端启动早期的并发问题。

**清单配置长什么样**：

[kb_config.go:26-34](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/kb_config.go#L26-L34) — `KnowledgeBaseConfig`：名字、来源路径、全局阈值、**逐标签阈值**（`LabelThresholds`）、**分组**（`Groups`，把若干标签并成一组）、指标、原型打分参数。

**示例向量的预嵌入与原型构建**：

[category_kb_embeddings.go:113-141](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/category_kb_embeddings.go#L113-L141) — `preloadEmbeddings` 并发嵌入所有示例句（worker 数受 CPU 与后端约束），统计失败数，最后调 `rebuildLabelPrototypeBanks` 重建原型簇。

[category_kb_embeddings.go:143-160](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/category_kb_embeddings.go#L143-L160) — `rebuildLabelPrototypeBanks`：把每个标签的有效示例向量喂给 `newPrototypeBank`，产出该标签的原型簇。

原型簇本身会先把高度相似的示例**聚成一簇、选代表（medoid）**，以降噪并控制规模：

[prototype_bank.go:23-55](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/prototype_bank.go#L23-L55) — `prototypeBank` 与 `newPrototypeBank`：若原型打分未启用，直接把每个示例当一个原型；否则按 `ClusterSimilarityThreshold` 聚簇、选 medoid、按簇大小排序、截断到 `MaxPrototypes`。

**打分公式（请求期核心）**：

请求来时，`Classify` 把请求嵌入后，对每个标签调原型簇的 `score`：

[prototype_scoring.go:29-66](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/prototype_scoring.go#L29-L66) — `score` 先算查询向量到每个原型的余弦相似度，降序排列；`best` 是最大相似度，`support` 是 top-M 的均值；最终分数是两者的凸组合：

\[
\text{Score} = w_{\text{best}} \cdot \text{best} + (1 - w_{\text{best}}) \cdot \text{support}
\]

其中 \(w_{\text{best}} \in [0,1]\) 由配置 `BestWeight` 决定。`best` 偏向「只要有一个原型极近就算」，`support` 偏向「多个原型都比较近才算」。返回结构 `prototypeBankScore`（[prototype_scoring.go:9-14](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/prototype_scoring.go#L9-L14)）同时携带 `Score / Best / Support`，供可解释性使用。

**逐标签打分与结果合成**：

[category_kb_scoring.go:9-18](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/category_kb_scoring.go#L9-L18) — `computeLabelScores`：对每个标签的原型簇算一次分。

[category_kb_scoring.go:20-36](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/category_kb_scoring.go#L20-L36) — 阈值筛选：每个标签可有自己的 `LabelThresholds`，否则用全局 `Threshold`；分数 ≥ 有效阈值的标签进 `matched`。

[category_kb_scoring.go:108-163](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/category_kb_scoring.go#L108-L163) — `buildResultFromLabelScores`：合成 `KBClassifyResult`，包括全局最佳标签（`BestLabel`/`BestSimilarity`/`BestLabelMargin`）、**在已匹配标签中的最佳**（`BestMatchedLabel`）、分组得分（`computeGroupScores` 取组内标签最高分）、以及自定义指标（`computeMetricValues`，如两组之间的 margin）。`BestLabelMargin = 最佳分 - 次佳分`，反映「赢了多少」，是可解释性的关键数值。

最终结果结构：

[category_kb_classifier.go:22-37](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/category_kb_classifier.go#L22-L37) — `KBClassifyResult` 的全部字段。

**把 KB 结果映射成 `kb` 信号**：

[classifier_signal_taxonomy.go:14-66](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_taxonomy.go#L14-L66) — `evaluateKBSignals`：跑所有 KB，存结构化结果与指标，再按 `Config.KBRules` 把 KB 输出绑定成普通信号。绑定规则由 `KBSignalRule`（[kb_config.go:120-131](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/kb_config.go#L120-L131)）声明：`target.kind`（`label` 或 `group`）+ `target.value`+ `match`（`best` 或 `threshold`）。命中后写 `SignalConfidences["kb:"+rule.Name]`，与域信号写 `domain:` 完全对称。

[classifier_signal_taxonomy.go:68-119](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_taxonomy.go#L68-L119) — `kbSignalMatchConfidence` 等函数实现 label/group × best/threshold 四种组合的命中判定。

#### 4.3.4 代码实践

**实践目标**：理解 KB 分类如何用原型簇给一段文本打分并判定命中。

**操作步骤**：

1. 设想一个名为 `intent` 的 KB，有三个标签：`billing`（计费咨询）、`incident`（故障上报）、`howto`（使用咨询）。每个标签配了 5 句示例。
2. 启动时 `preloadEmbeddings` 把 15 句示例嵌入，`newPrototypeBank` 把每个标签的 5 个向量聚成（比如）2 个原型，于是每个标签有 2 个原型代表。
3. 来一条请求 `"我的账单多扣了 50 块"`，`embedText` 得到查询向量。
4. 对 `billing` 标签，`score` 算查询向量到它 2 个原型的余弦相似度，比如得 `[0.82, 0.71]`：`best=0.82`，`support=(0.82+0.71)/2=0.765`；设 `BestWeight=0.6`，则 `Score = 0.6×0.82 + 0.4×0.765 = 0.810`。
5. 同理算出 `incident` 的 `Score=0.55`、`howto` 的 `Score=0.40`。

**需要观察的现象**：

- 若全局 `threshold=0.5`：`billing`(0.810) 与 `incident`(0.55) 过阈值，`howto`(0.40) 不过 → `MatchedLabels=["billing","incident"]`（字母序）。
- `BestLabel=billing`，`BestLabelMargin = 0.810 - 0.55 = 0.26`。
- 若有 `KBSignalRule`：`{name: billing_intent, kb: intent, target:{kind:label, value:billing}, match:threshold}`，则因 `billing` 在 `MatchedLabels` 中 → 命中，写 `SignalConfidences["kb:billing_intent"] = 0.810`。

**预期结果**：KB 分类把一段自定义文本转成了带置信度的 `kb:*` 信号，可直接被 `kb("billing_intent")` 这样的决策规则引用，效果与 `domain("health")` 完全对称。

> 上述相似度与分数均为**示例数据**，真实数值待本地嵌入后验证。`BestWeight`、`threshold` 的实际默认值见 `PrototypeScoringConfig.WithDefaults()` 与 `KnowledgeBaseConfig.WithDefaults()`。

#### 4.3.5 小练习与答案

**练习 1**：`Score = w·best + (1-w)·support` 中，`w=1` 和 `w=0` 分别代表什么偏好？

**答案**：`w=1` 时 `Score = best`，只看「最相似的那个原型」，只要有一个示例极近就判定命中（偏激进，适合示例多样的标签）；`w=0` 时 `Score = support`，看 top-M 原型的平均相似度，需要多个原型都比较近才高分（偏保守，能抗个别离群示例的干扰）。

**练习 2**：`BestLabel` 和 `BestMatchedLabel` 有什么区别？

**答案**：`BestLabel` 是**所有标签**里分数最高的（不管是否过阈值）；`BestMatchedLabel` 是**仅在已过阈值的标签里**分数最高的。当最高分标签本身没过阈值时，二者会不同——后者更贴近「真正可用的命中」。

---

### 4.4 为什么 `domain("health")` 能匹配到 health 类别？（实践任务专题）

本节单独回答本讲指定的实践任务。它把 4.1 的「翻译」与决策引擎的「匹配」两边接起来。

#### 4.4.1 关键事实：`domain` 是决策引擎里唯一被特殊处理的信号类型

在决策引擎 `matchesSignalType` 里，绝大多数信号类型（keyword、context、embedding、kb……）都走同一条通用路径——在命中的规则名列表里做 `slices.Contains` 精确匹配。**唯独 `domain` 被特殊分支**：

[engine.go:297-316](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L297-L316) — `domain` 类型直接调 `matchesDomainCondition`，其余类型走 `resolveSignalRules` + `slices.Contains`。

#### 4.4.2 双重匹配机制

`matchesDomainCondition` 提供两条命中路径：

[engine.go:461-483](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L461-L483) — 注释明确写出两条规则：**(1) 检测到的域 == 类别名（直接命中）；(2) 检测到的域在该类别的 `mmlu_categories` 列表里（别名命中）**。

于是 `domain("health")` 能命中 health 类别有两种情形：

1. **直接命中**：分类器（经翻译后）输出 `domain:health`，`signals.DomainRules` 里含 `"health"` → `slices.Contains(detectedDomains, "health")` 为真。
2. **别名命中**：决策里的 `"health"` 是一个**配置类别**，它的 `mmlu_categories` 列出了若干细粒度 MMLU 标签；当分类器输出了其中某个细标签（比如 `medicine`）并出现在 `signals.DomainRules` 里时，第二层循环命中。

第二条路径正是为「**决策词汇粗、模型词汇细**」的粒度错配设计的。

#### 4.4.3 一个真实配置例子

项目测试里就有这样的配置（把多个 MMLU 细标签并到一个通用类别）：

[config_test.go:2495-2499](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/config_test.go#L2495-L2499) — 类别 `tech` 的 `mmlu_categories` 是 `["computer science", "engineering"]`；`finance` 对应 `["economics"]`。

在这个配置下，决策规则 `domain("tech")` 能命中，只要分类器检测到 `computer science` 或 `engineering` 中的**任意一个**——这就是别名路径的价值。同理，若用户把 health 类别配置成 `mmlu_categories: ["medicine", "biology"]`，那么 `domain("health")` 在模型吐出 `medicine` 时也能命中。

> **注意两层映射的方向**：4.1 的 `translateMMLUToGeneric` 是「分类器输出 → 通用名」的**前向翻译**；4.4 的 `matchesDomainCondition` 是「决策类别名 → 检测到的域」的**匹配期展开**。二者协作保证：无论配置用的是 MMLU 原名（如 balance 配方里直接用 `health`/`math`）还是自定义通用名（如 `tech`），`domain(...)` 规则都能正确命中。

#### 4.4.4 代码实践（本讲指定任务）

**实践目标**：用一段示例文本说明 `domain("health")` 如何在决策里匹配到 health 类别。

**操作步骤**：

1. 选定 balance 配方，它的域信号直接用 MMLU 名：`SIGNAL domain health { ... }`（见 [config/recipes/balance/recipe.dsl:29](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L29)），且决策里有 `ROUTE verified_health` 用 `domain("health")` 作条件（[recipe.dsl:583](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L583)）。
2. 假设请求文本 `"is ibuprofen safe in early pregnancy?"` 经域分类模型得到 `health` 概率最高（如 0.62）。
3. 由于 balance 未给 `health` 配 `mmlu_categories` 通用映射，`translateMMLUToGeneric("health")` 原样返回 `health`。
4. 经熵分支后，`health` 被写进 `SignalConfidences["domain:health"]` 与 `MatchedDomainRules`。
5. 决策引擎求值 `verified_health` 的 WHEN 时遇到 `domain("health")` → `matchesDomainCondition("health", detectedDomains)`。

**需要观察的现象**：第 5 步走的是**直接命中**路径（`slices.Contains(detectedDomains, "health")` 为真）。

**预期结果**：`domain("health")` 求值为命中。这正是 balance 配方能把医疗类问题路由到保守的 `verified_health` 路由（用更稳的模型 + 验证）的根因。

**延伸**：若把 `health` 配置成 `mmlu_categories: ["medicine", "biology"]`，则即使模型把同一句话判成 `medicine`（而非 `health`），`domain("health")` 也能经**别名路径**命中——这就是双重机制的意义。

## 5. 综合实践

把三个模块串起来：**给 balance 配方加一个自定义 KB 信号，并观察它与 domain 信号的协作**。

1. **阅读配置入口**：在 balance 的 `config.yaml` 里找到 `signals` 段（参考 u3-l1、u3-l3），确认 `domain` 信号已存在；再找到 `routing.signals.kb`（或等价位置）看 `KBSignalRule` 如何声明 `name/kb/target/match`。
2. **设计一个 KB**：构思一个名为 `safety` 的 KB，标签 `risky_health`（示例：用药、剂量、孕期相关问题）与 `general`。在 `labels.json` 里为每个标签写 description + 3~5 句 exemplar。
3. **声明信号绑定**：写一条 `KBSignalRule`，`target.kind=label, value=risky_health, match=threshold`，`name=risky_health_kb`。
4. **跟踪一次请求**：对 `"is ibuprofen safe in early pregnancy?"` 写出它**同时**触发的两类信号：
   - `domain:health`（来自域分类模型 + 熵分支）
   - `kb:risky_health_kb`（来自 KB 原型打分 + 阈值）
5. **回答**：如果在某条 ROUTE 的 WHEN 里写 `domain("health") AND kb("risky_health_kb")`，相比只写 `domain("health")`，路由会更激进还是更保守？为什么？

> 预期结论：同时要求两个独立证据（一个来自固定 14 类模型，一个来自自定义示例库）会让该路由**更难命中、更精确**——这正是 KB 增强分类的价值：用自定义示例为域分类提供「第二意见」。

## 6. 本讲小结

- 域信号由 MMLU-Pro 分类模型产出全概率分布，经 `CategoryMapping`（索引→MMLU 名）与 `translateMMLUToGeneric`（MMLU→通用名）两层翻译后写进 `SignalConfidences["domain:<名>"]`。
- **Shannon 熵**决定输出形态：归一化熵 `very_low/low` 时只报 top-1；`medium/high/very_high` 时报出所有过 `CategoryModel.Threshold` 的类别——域信号天然支持多类别并立。
- **KB 增强分类**是示例驱动的自定义分类旁路：预嵌入示例→聚原型簇→请求期按 `Score = w·best + (1-w)·support` 打分→过阈值命中→映射成与 domain 对称的 `kb:*` 信号。
- 决策引擎里 `domain` 是**唯一**有特殊匹配逻辑的信号类型：`matchesDomainCondition` 同时支持「直接命中」和「经 `mmlu_categories` 的别名命中」，解决决策词汇与模型词汇的粒度错配。
- 熵分析是一个被有意提取的通用工具，同时服务于域信号的多类别决策与「是否启用推理模型」的更高层判断。

## 7. 下一步学习建议

- **继续信号系统**：本讲的姊妹篇是 u8-l3（PII 与越狱检测），它讲另一类安全治理信号；u8-l4（嵌入提供者）讲 KB 分类所依赖的 `embedding.Provider` 抽象，建议紧接着读。
- **回到决策侧**：本讲反复提到的 `domain(...)` 匹配发生在决策引擎里，完整规则树求值（AND/OR/NOT 与置信度聚合）见 u6-l1。
- **深入源码**：若想看熵分析的更多用法，读 `classifier_category_entropy.go` 的 `MakeEntropyBasedReasoningDecision`；若想自定义 KB，参照 `config/recipes/` 下任意配方的 `signals.kb` 段与 `knowledge_bases/` 资产目录。
