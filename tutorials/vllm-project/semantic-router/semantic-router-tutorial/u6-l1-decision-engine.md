# 决策引擎：布尔规则求值与置信度

## 1. 本讲目标

学完本讲，你应该能够：

- 读懂 `pkg/decision/engine.go`，说清楚一条 `ROUTE` 的 `WHEN` 规则树是如何被**递归求值**的。
- 理解叶子节点如何按信号类型做**存在性匹配**，以及 `domain` 为何要做 `mmlu_categories` 别名匹配。
- 手工推演 `AND` / `OR` / `NOT` 三种布尔节点如何**聚合置信度**（AND 取平均、OR 取最大、NOT 翻转为 1.0）。
- 说清楚 `priority` / `confidence` / `tier` 三种选择策略在「多个决策同时命中」时如何挑出唯一胜者。

本讲是 u5-l2「决策求值管线」的纵深篇：u5-l2 讲的是「调用链如何走到决策引擎」，本讲钻进决策引擎**内部**，把布尔规则求值与置信度聚合讲透。信号族各自的实现细节留到 u8，选择算法的数学留到 u6-l2。

## 2. 前置知识

- **布尔表达式树**：把 `A AND (B OR NOT C)` 这种表达式写成树。组合节点（AND/OR/NOT）有若干子节点，叶子节点（A/B/C）是单个条件。求值时从根递归往下算。
- **置信度（confidence）**：一个 0 到 1 之间的数，表示「这条信号有多可信」。规则型信号（如 keyword 命中）置信度恒为 1.0；学习型信号（如 embedding 相似度）写入真实概率。这一点在 u2-l2 已建立。
- **`SignalMatches`**：决策引擎的输入容器，把一次请求里所有「命中的信号名」按族分门别类装好。详见 u5-l2。
- **`RuleNode`**：规则树的一个节点，既是叶子的载体，也是 AND/OR/NOT 组合的载体（同一结构体两种用法）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/semantic-router/pkg/decision/engine.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go) | 决策引擎本体：规则树递归求值、信号匹配、置信度聚合、最佳决策选择。本讲绝对主角。 |
| [src/semantic-router/pkg/config/decision_config.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/decision_config.go) | 定义 `Decision`、`RuleNode`（规则树节点）、`RuleCombination`/`RuleCondition` 类型别名。 |
| [src/semantic-router/pkg/config/config.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/config.go) | 定义全部 `SignalType*` 常量（keyword/domain/embedding 等），是叶子匹配的「类型字典」。 |
| [src/semantic-router/pkg/config/recipes.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/recipes.go) | 定义 `RoutingStrategyPriority` / `RoutingStrategyConfidence` 两种策略常量。 |
| [src/semantic-router/pkg/classification/classifier_signal_decision.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_decision.go) | 分类包调用决策引擎的桥接点：构造 `SignalMatches` 并调用 `EvaluateDecisionsWithSignals`。 |

## 4. 核心概念与源码讲解

### 4.1 规则树求值

#### 4.1.1 概念说明

一条 `ROUTE` 的 `WHEN` 条件，在配置里就是一棵**布尔表达式树**。引擎要做的事情很朴素：给定这棵树和一份「本次请求命中了哪些信号」，递归地求出两个结果——**是否匹配（matched）** 和 **匹配置信度（confidence）**。

这里的关键设计是：`RuleNode` 这个结构体**身兼两职**。

- 当它是**叶子**（`Type != ""`）时，`Type`+`Name` 指向一条具体信号（如 `keyword:urgent`）。
- 当它是**组合**节点时，`Operator` 取 `AND`/`OR`/`NOT`，`Conditions` 装子节点。

`IsLeaf()` 的判定就一行——只要 `Type` 非空就是叶子：

```go
func (n *RuleNode) IsLeaf() bool {
	return n.Type != ""
}
```

（[decision_config.go:172-174](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/decision_config.go#L172-L174)）

还有一个 `IsEmpty()`，用来识别「规则被整个省略」的情况——这是兜底路由（无 `WHEN`）的规范写法。

#### 4.1.2 核心流程

引擎入口是 `EvaluateDecisionsWithSignals`，它对每条决策调用 `evaluateDecisionWithSignals`，收集所有命中的，最后交给 `selectBestDecision` 挑一个。其主干如下：

```go
for i := range e.decisions {
	decision := &e.decisions[i]
	matched, confidence, matchedRules := e.evaluateDecisionWithSignals(decision, signals)
	if matched {
		results = append(results, DecisionResult{Decision: decision, Confidence: confidence, MatchedRules: matchedRules})
	}
}
return e.selectBestDecision(results), nil
```

（[engine.go:143-157](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L143-L157) 简化呈现）

`evaluateDecisionWithSignals` 在根部做一次特殊处理：**规则被整个省略 ⇒ 这是一条无条件兜底决策**，直接返回 `(matched=true, confidence=0, nil)`：

```go
if decision.Rules.IsEmpty() {
	return true, 0, nil
}
return e.evalNode(decision.Rules, signals)
```

（[engine.go:176-180](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L176-L180)）

注意源码注释强调：这个「省略即兜底」的语义**只在决策根部成立**，嵌套节点不能这么推断。引擎显式处理它，而不是依赖零值滑进 OR 语义。

真正递归的地方是 `evalNode`——它是一个分发器：叶子走 `evalLeaf`，组合节点按 `Operator` 分发到 `evalAND` / `evalNOT` / `evalOR`：

```go
func (e *DecisionEngine) evalNode(node config.RuleNode, signals *SignalMatches) (matched bool, confidence float64, matchedRules []string) {
	if node.IsLeaf() {
		return e.evalLeaf(node, signals)
	}
	switch strings.ToUpper(node.Operator) {
	case "AND":
		return e.evalAND(node.Conditions, signals)
	case "NOT":
		return e.evalNOT(node.Conditions, signals)
	default: // OR
		return e.evalOR(node.Conditions, signals)
	}
}
```

（[engine.go:185-201](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L185-L201)）

> 一个细节：`default` 分支是 `OR`。也就是说**任何不是 AND/NOT 的 Operator（包括空字符串）都按 OR 处理**。这就是为什么根部必须用 `IsEmpty()` 显式拦截兜底——否则一个空根会被当成「OR 空集」而非「无条件命中」。

#### 4.1.3 源码精读

- **`DecisionEngine` 结构体**（[engine.go:33-40](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L33-L40)）：持有 keyword/embedding 规则、categories、decisions、strategy、routingScope。注意它**不持有任何信号数据**——信号是每次请求现传进来的，引擎本身是无状态求值器。
- **`RuleNode` 结构体**（[decision_config.go:162-170](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/decision_config.go#L162-L170)）：`Type/Name/Label` 描述叶子信号，`Predicate/OnError` 用于数值断言（见 4.2），`Operator/Conditions` 描述组合节点。
- **入口 `EvaluateDecisionsWithSignals`**（[engine.go:128-166](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L128-L166)）：开头用 `defer` 记录求值耗时指标；若没有任何决策配置，直接返回 `no decisions configured` 错误；若全部不命中，返回 `(nil, nil)`（注意是 nil 结果 + nil 错误，由调用方决定走兜底）。

#### 4.1.4 代码实践

**实践目标**：用源码阅读的方式，确认「省略规则 ⇒ 兜底」与「显式空 AND ⇒ 兜底」两条路径在引擎里是等价的。

**操作步骤**：

1. 打开 [engine_empty_and_test.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine_empty_and_test.go)。
2. 阅读 `TestDecisionEngine_OmittedRulesActsAsCatchAll`（第 42-61 行）：构造的决策**只有 Name 和 Priority，没有 Rules**，断言它能命中且置信度为 0。
3. 阅读 `TestDecisionEngine_EmptyANDActsAsCatchAll`（第 9-40 行）：构造的决策显式写了 `Operator:"AND"` 但 `Conditions` 为空切片，断言同样命中且置信度为 0。
4. 运行这两个测试验证：

   ```bash
   cd src/semantic-router
   go test ./pkg/decision/ -run 'TestDecisionEngine_(EmptyAND|OmittedRules)ActsAsCatchAll' -v
   ```

**需要观察的现象**：两条路径都通过，且 `result.Confidence` 都是 `0.0`。

**预期结果**：两个测试均 `PASS`。这说明兜底路由无论用「省略 rules」还是「空 AND」写法，在引擎里都会得到 `confidence=0`——这正是它在 `confidence` 策略下不会抢占信号支撑路由的关键（见 4.4）。

#### 4.1.5 小练习与答案

**练习 1**：如果把一条决策的根 `Operator` 写成空字符串、`Conditions` 也为空切片（不是省略），引擎会怎么处理？

**参考答案**：`IsEmpty()` 会因为 `Operator==""` 且 `Conditions` 长度为 0 而返回 true（参见 [decision_config.go:179-181](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/decision_config.go#L179-L181)），根部仍按兜底处理，返回 `(true, 0, nil)`。

**练习 2**：`evalNode` 为什么把 `default` 设为 OR 而不是 AND？

**参考答案**：因为 YAML/DSL 里最常见的是「多个条件任一命中即可」，OR 是更宽容的默认；同时根部兜底已由 `IsEmpty()` 单独拦截，不会与 OR 默认冲突。

---

### 4.2 信号匹配

#### 4.2.1 概念说明

叶子节点的工作是回答一个问题：**「本次请求里，`族:名` 这条信号命中了吗？」** 这是一种**存在性匹配**——只看 `SignalMatches` 对应族的切片里有没有这个名字，不看具体数值（数值断言由 `Predicate` 单独处理，见下）。

`SignalMatches` 是一个把 16+ 族信号分门别类装好的容器，每个族一个字符串切片，外加三个 map：

```go
type SignalMatches struct {
	KeywordRules    []string
	EmbeddingRules  []string
	DomainRules     []string
	// ... 其余各族 ...
	ProjectionRules []string

	SignalConfidences map[string]float64 // "signalType:ruleName" → 0.0-1.0，缺失默认 1.0
	SignalValues      map[string]float64 // 信号暴露的原始数值
	SignalErrors      map[string]string  // "type:name" → 评估错误
}
```

（[engine.go:71-98](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L71-L98) 简化呈现）

#### 4.2.2 核心流程

叶子求值入口 `evalLeaf` 先做**类型归一化**，再问 `matchesSignalType`「支不支持、匹不匹配」。匹配上之后，置信度从 `SignalConfidences` 取（缺失默认 1.0）：

```go
func (e *DecisionEngine) evalLeaf(node config.RuleNode, signals *SignalMatches) (matched bool, confidence float64, matchedRules []string) {
	normalizedType := strings.ToLower(strings.TrimSpace(node.Type))
	matched, supported := e.matchesSignalType(normalizedType, node.Name, signals)
	if !supported {
		return false, 0, nil          // 未知信号类型 → 直接不匹配
	}
	if node.Predicate != nil {
		return evaluatePredicateLeaf(node, normalizedType, signals) // 数值断言走单独路径
	}
	if !matched {
		return false, 0, nil
	}
	confidence = signalConfidence(signals.SignalConfidences, normalizedType, node.Name)
	return true, confidence, []string{formatMatchedRule(node)}
}
```

（[engine.go:204-223](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L204-L223)）

`matchesSignalType` 内部有三条分支：

```go
func (e *DecisionEngine) matchesSignalType(normalizedType string, name string, signals *SignalMatches) (matched bool, supported bool) {
	if normalizedType == "domain" {
		return e.matchesDomainCondition(name, signals.DomainRules), true   // ① domain 别名匹配
	}
	if normalizedType == config.SignalTypeClassifier {
		return false, true                                                // ② classifier 仅支持断言
	}
	rules, ok := resolveSignalRules(normalizedType, signals)             // ③ 其余族：存在性匹配
	if !ok {
		return false, false
	}
	return slices.Contains(rules, name), true
}
```

（[engine.go:297-316](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L297-L316)）

- **① domain 别名匹配**：`domain` 是特殊的——它不止看检测到的域是否等于类别名，还要看检测域是否落在该类别的 `mmlu_categories` 列表里。这正是 u2-l2 提到的「`domain('health')` 能匹配到 health 类别」的实现机制。
- **② classifier 仅支持断言**：`classifier` 类型的叶子**只允许搭配 `Predicate`（数值断言）使用**，存在性匹配恒返回 `matched=false`。配置校验会保证命名的分类器存在。
- **③ 其余族存在性匹配**：`resolveSignalRules` 把类型字符串（如 `"keyword"`、`"embedding"`、`"complexity"`、`"projection"`）映射到 `SignalMatches` 对应切片，再用 `slices.Contains` 判断名字在不在里面。

`resolveSignalRules` 分两步查（[engine.go:318-326](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L318-L326)）：先查 `resolvePrimarySignalRules`（keyword/embedding/fact_check/user_feedback/reask/preference/language/context/structure/complexity），再查 `resolvePolicySignalRules`（modality/authz/jailbreak/pii/kb/conversation/event/metadata/projection）。这两个 switch 的 case 字符串与 [config.go:24-43](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/config.go#L24-L43) 的 `SignalType*` 常量一一对应。

#### 4.2.3 源码精读：domain 别名匹配

`matchesDomainCondition` 是 domain 分支的核心，它实现了「直接相等 **或** 落在 mmlu 别名表里」两种命中方式：

```go
func (e *DecisionEngine) matchesDomainCondition(categoryName string, detectedDomains []string) bool {
	if slices.Contains(detectedDomains, categoryName) {            // 直接相等
		return true
	}
	for _, cat := range e.categories {                             // 查类别的 mmlu_categories 别名表
		if cat.Name == categoryName {
			for _, detectedDomain := range detectedDomains {
				if slices.Contains(cat.MMLUCategories, detectedDomain) {
					return true
				}
			}
			break
		}
	}
	return false
}
```

（[engine.go:465-483](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L465-L483)）

这就是为什么模型分类器可能输出一个细粒度域（如 `clinical_knowledge`），而规则写的是大类（如 `health`），只要 `health` 类别把 `clinical_knowledge` 列进了 `mmlu_categories`，照样能命中。这层别名映射让规则作者用大类写规则、分类器用细类出结果，两边解耦。

#### 4.2.4 源码精读：数值断言（Predicate）

当叶子带 `Predicate` 时，匹配不再是「存在性」，而是「该信号的数值是否满足 `GT/GTE/LT/LTE` 阈值」。数值优先取 `SignalValues`（原始值），缺失再取 `SignalConfidences`：

```go
func signalPredicateValue(signals *SignalMatches, signalType string, name string, label string) (float64, bool) {
	key := fmt.Sprintf("%s:%s", signalType, name)
	if label != "" {
		key += ":" + label
	}
	if signals.SignalValues != nil {
		if value, ok := signals.SignalValues[key]; ok {
			return value, true
		}
	}
	if signals.SignalConfidences != nil {
		value, ok := signals.SignalConfidences[key]
		return value, ok
	}
	return 0, false
}
```

（[engine.go:253-273](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L253-L273)）

`numericPredicateMatches`（[engine.go:275-295](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L275-L295)）随后按四个阈值字段逐条校验，命中即返回 `(true, 1.0, ...)`——**断言命中固定置信度 1.0**。还有一个 `on_error: match` 兜底：若该信号评估出错（出现在 `SignalErrors` 里）且配置了 `on_error=match`，则当作命中（[engine.go:237-242](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L237-L242)）。

#### 4.2.5 代码实践

**实践目标**：验证 `SignalConfidences` 缺失键时默认为 1.0，并理解它对规则型信号的影响。

**操作步骤**：

1. 阅读 `signalConfidence`（[engine.go:386-396](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L386-L396)）：map 为 nil 返回 1.0，键不存在也返回 1.0。
2. 在 [engine_test.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine_test.go) 的 `TestDecisionEngine_EvaluateDecisions` 中，看 `all AND conditions match` 用例：它只传了 `matchedKeywordRules`/`matchedDomainRules`，**没传任何 `SignalConfidences`**，却照样命中。

**需要观察的现象**：没有置信度 map 时，keyword/domain 这类规则信号默认按 1.0 参与聚合。

**预期结果**：测试通过，说明规则型信号即使不带显式置信度也等价于满权重。

#### 4.2.6 小练习与答案

**练习 1**：为什么 `classifier` 类型的叶子在 `matchesSignalType` 里直接返回 `matched=false`？

**参考答案**：因为 classifier 信号设计为**只能搭配 `Predicate` 数值断言使用**（如某个分类器的得分 `>= 0.8`），不存在「存在性」语义。`evalLeaf` 在 `matchesSignalType` 返回后还会检查 `node.Predicate != nil`，有断言才走 `evaluatePredicateLeaf`，所以 `matched=false` 不影响断言路径。

**练习 2**：如果一个信号类型拼写错误（如写成 `keywrod`），引擎会怎样？

**参考答案**：`resolveSignalRules` 两个 switch 都 miss，返回 `(nil, false)`；`matchesSignalType` 因此返回 `(false, false)`；`evalLeaf` 见 `!supported` 直接返回 `(false, 0, nil)`——该叶子永不命中。注意这不会报错，配置层的语义校验才会拦截这类错误（见 u3-l3）。

---

### 4.3 置信度聚合

#### 4.3.1 概念说明

光知道「匹不匹配」还不够——决策引擎还要算出**这条决策整体有多可信**，供选择策略排序用。聚合规则是本模块的核心，也是最容易记错的点：

| 算子 | 匹配条件 | 置信度聚合 |
| --- | --- | --- |
| **AND** | 所有子节点都匹配 | 全部子节点置信度的**算术平均** |
| **OR** | 至少一个子节点匹配 | 命中子节点里置信度**最大**的那个 |
| **NOT** | 唯一子节点**不**匹配 | 固定 **1.0**（子不匹配 ⇒ NOT 完全确信） |
| 空 AND / 省略规则 | 无条件 | 固定 **0**（兜底，不抢占） |

#### 4.3.2 核心流程与数学

**AND 取平均**：若 AND 的 n 个子节点都匹配，置信度为

\[
\text{conf}_{\text{AND}} = \frac{1}{n}\sum_{i=1}^{n} c_i
\]

任一子节点不匹配则整条 AND 不匹配（短路返回 `(false, 0, nil)`）。

```go
func (e *DecisionEngine) evalAND(children []config.RuleNode, signals *SignalMatches) (matched bool, confidence float64, matchedRules []string) {
	if len(children) == 0 {
		return true, 0, nil                 // 空 AND = 兜底，置信度 0
	}
	totalConf := 0.0
	for _, child := range children {
		m, c, r := e.evalNode(child, signals)
		if !m {
			return false, 0, nil            // 短路：任一不匹配 ⇒ 整体不匹配
		}
		totalConf += c
		matchedRules = append(matchedRules, r...)
	}
	return true, totalConf / float64(len(children)), matchedRules
}
```

（[engine.go:402-419](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L402-L419)）

**OR 取最大**：OR 遍历所有子节点，记录命中者中置信度最高的。注意它**不短路**——要把所有子节点都看一遍才能确定最大值：

```go
func (e *DecisionEngine) evalOR(children []config.RuleNode, signals *SignalMatches) (matched bool, confidence float64, matchedRules []string) {
	bestConf := 0.0
	var bestRules []string
	for _, child := range children {
		m, c, r := e.evalNode(child, signals)
		if m && (!matched || c > bestConf) {
			matched = true
			bestConf = c
			bestRules = r
		}
	}
	if matched {
		return true, bestConf, bestRules
	}
	return false, 0, nil
}
```

（[engine.go:422-440](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L422-L440)）

**NOT 严格一元**：NOT 要求**恰好 1 个**子节点，否则按不匹配处理（防御性）。语义是逻辑翻转：

```go
func (e *DecisionEngine) evalNOT(children []config.RuleNode, signals *SignalMatches) (matched bool, confidence float64, matchedRules []string) {
	if len(children) != 1 {
		logging.Warnf("NOT operator requires exactly 1 child, got %d — treating as non-match", len(children))
		return false, 0, nil
	}
	m, c, r := e.evalNode(children[0], signals)
	if !m {
		return true, 1.0, r                 // 子不匹配 ⇒ NOT 命中，置信度 1.0
	}
	return false, c, r                     // 子匹配 ⇒ NOT 不命中
}
```

（[engine.go:444-459](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L444-L459)）

> **为什么 NOT 命中时给 1.0？** 直觉是：「我确信这条信号**没有**出现」。比如 `NOT keyword:safe_topic` 在没有命中 `safe_topic` 时，等价于「我很肯定这不是个安全闲聊话题」，因此给满置信度。这与 AND 取平均、OR 取最大形成一套自洽的传播规则。

#### 4.3.3 一个完整推演示例

假设有决策：

```
ROUTE escalate = AND(
  OR(domain:math, domain:science),
  NOT(keyword:trivial),
  embedding:reasoning
)
```

给定信号：`DomainRules=["math"]`、`KeywordRules=[]`（无 trivial）、`EmbeddingRules=["reasoning"]`，`SignalConfidences={"domain:math":0.8, "embedding:reasoning":0.9}`。逐层求值：

1. `OR(domain:math, domain:science)`：math 命中（conf 0.8）、science 不命中 ⇒ OR 命中，置信度取最大 = **0.8**。
2. `NOT(keyword:trivial)`：trivial 不命中 ⇒ NOT 命中，置信度 = **1.0**。
3. `embedding:reasoning`：命中，置信度 = **0.9**。
4. 外层 `AND`：三者全命中 ⇒ 置信度 = (0.8 + 1.0 + 0.9) / 3 = **0.9**。

最终 `escalate` 命中、置信度 0.9。若 keyword 里**有** trivial，第 2 步 NOT 不命中 ⇒ 整条 AND 不命中。

#### 4.3.4 代码实践

**实践目标**：手工推演一条含 AND/OR/NOT 的规则，再用 Go 测试验证。

**操作步骤**：

1. 仿照 [engine_test.go:43-56](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine_test.go#L43-L56) 的 `notCodingDecision()`，自己构造一条 `NOT( OR(keyword:programming, domain:coding) )` 决策。
2. 手工推演两种输入：
   - 输入 A：`KeywordRules=[]`、`DomainRules=[]` ⇒ OR 不命中 ⇒ NOT 命中，置信度 1.0。
   - 输入 B：`KeywordRules=["programming"]` ⇒ OR 命中（conf 1.0）⇒ NOT 不命中。
3. 把推演写成一个小测试（示例代码，**非项目原有代码**）：

   ```go
   // 示例代码：用于验证手工推演，可放进 engine_test.go 同包临时运行
   func TestMyNotTrace(t *testing.T) {
       engine := NewDecisionEngine(nil, nil, nil, []config.Decision{
           ruleDecision("exclude-coding", 10, "NOT",
               config.RuleCondition{Operator: "OR", Conditions: []config.RuleCondition{
                   {Type: config.SignalTypeKeyword, Name: "programming"},
                   {Type: config.SignalTypeDomain, Name: "coding"},
               }}),
       }, config.RoutingStrategyPriority)

       rA, _ := engine.EvaluateDecisionsWithSignals(&SignalMatches{})
       if rA == nil || rA.Decision.Name != "exclude-coding" || rA.Confidence != 1.0 {
           t.Fatalf("输入A期望 exclude-coding/conf=1.0, got %#v", rA)
       }

       rB, _ := engine.EvaluateDecisionsWithSignals(&SignalMatches{KeywordRules: []string{"programming"}})
       if rB != nil {
           t.Fatalf("输入B期望不命中(nil), got %s", rB.Decision.Name)
       }
   }
   ```

4. 运行：`cd src/semantic-router && go test ./pkg/decision/ -run TestMyNotTrace -v`

**需要观察的现象**：输入 A 命中且置信度恰为 1.0；输入 B 不命中（返回 nil）。

**预期结果**：测试通过。若不通过，对照 4.3.3 的推演步骤排查哪一步算错。

#### 4.3.5 小练习与答案

**练习 1**：AND 有 3 个子节点，置信度分别是 0.6、0.9、0.3，整体置信度是多少？若把外层换成 OR 呢？

**参考答案**：AND 取平均 = (0.6+0.9+0.3)/3 = 0.6；OR 取最大 = 0.9。

**练习 2**：`NOT` 写了 0 个或 2 个子节点会发生什么？

**参考答案**：`evalNOT` 检查 `len(children) != 1`，打印告警并返回 `(false, 0, nil)`，即按不匹配处理（[engine.go:448-451](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L448-L451)）。这是防御性设计，把配置错误变成「安全的不匹配」。

---

### 4.4 最佳决策选择

#### 4.4.1 概念说明

一次请求可能**同时命中多条决策**（比如既匹配 `math-route` 又匹配兜底 `default-route`）。引擎必须从所有命中的结果里挑出**唯一胜者**。`selectBestDecision` 用一个稳定排序实现：把所有命中结果按一套比较规则升序排，取 `results[0]`。

比较规则有三套，由两个维度决定：

1. **是否进入分层模式（tiered）**：只要**任意一条**命中决策的 `Tier > 0`，就进入分层模式。这是为了把「越狱拦截」这类高优先级安全决策（通常 `Tier=1`）牢牢顶在最前。
2. **`strategy` 取值**：`priority`（默认）或 `confidence`。

#### 4.4.2 核心流程：三种比较规则

`decisionResultLess` 是唯一的比较函数（升序，越「小」越优先）。三套规则如下，**逐条比较、首条不同者决定顺序，全部相同则按 Name 升序做确定性兜底**：

**① 分层模式（任一命中决策 Tier>0）**：

| 比较键 | 方向 | 含义 |
| --- | --- | --- |
| `Tier` | **升序**（小者优先） | Tier=1 最优先，安全拦截压过普通路由 |
| `Confidence` | 降序（大者优先） | 同 Tier 内更可信的优先 |
| `Priority` | 降序 | 再相同则按声明优先级 |
| `Name` | 升序 | 最终确定性 tie-break |

**② `confidence` 策略（无 Tier）**：`Confidence` 降序 → `Priority` 降序 → `Name` 升序。

**③ `priority` 策略（无 Tier，默认）**：`Priority` 降序 → `Confidence` 降序 → `Name` 升序。

```go
func (e *DecisionEngine) decisionResultLess(left, right DecisionResult, useTieredSelection bool) bool {
	if useTieredSelection {
		if left.Decision.Tier != right.Decision.Tier {
			return left.Decision.Tier < right.Decision.Tier      // Tier 升序
		}
		if left.Confidence != right.Confidence {
			return left.Confidence > right.Confidence            // 置信度降序
		}
		if left.Decision.Priority != right.Decision.Priority {
			return left.Decision.Priority > right.Decision.Priority
		}
		return left.Decision.Name < right.Decision.Name
	}
	if e.strategy == config.RoutingStrategyConfidence {
		// confidence 策略：置信度降序 → Priority 降序 → Name 升序
		...
	}
	// priority 策略（默认）：Priority 降序 → 置信度降序 → Name 升序
	...
}
```

（[engine.go:512-547](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L512-L547) 完整三套规则）

是否进入分层模式由 `useTieredSelection` 决定，它扫描所有命中结果，任一 `Tier>0` 即为 true（[engine.go:503-510](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L503-L510)）。

> **关键洞察**：分层模式一旦触发，`strategy` 字段就被「架空」了——Tier 成为第一主键。这就是 u2-l4 所说的「一旦任意匹配路由标了 TIER 便进入分层选择，TIER 升序为主键，PRIORITY 仅在同 TIER 内做微调」。

#### 4.4.3 源码精读：为什么兜底不会抢占

回到 4.1 的兜底决策——它的置信度恒为 0。现在结合选择规则就能看清它的妙处：

- 在 **`confidence` 策略**下：信号支撑的路由置信度通常 > 0，而兜底是 0，`Confidence` 降序会把信号路由排在前面、兜底排最后。这正是 [engine_empty_and_test.go:91-107](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine_empty_and_test.go#L91-L107) 的 `signal-backed route outranks catch-all` 用例验证的：有信号时 `specific-route` 胜，无信号时兜底 `default-route` 兜底胜。
- 在 **`priority` 策略**下：兜底能否胜出取决于它的 `Priority` 设置。通常把兜底的 `Priority` 设得很低，让信号路由优先。

测试 [engine_tier_test.go:9-35](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine_tier_test.go#L9-L35) 演示了分层模式的威力：`jailbreak-block`（Tier=1）与 `math-route`（Tier=2）同时命中，尽管 `math-route` 的置信度（0.99）远高于 `jailbreak`（0.55），但 **Tier 升序优先**，`jailbreak-block` 胜出——安全拦截永远压过普通路由。

#### 4.4.4 代码实践

**实践目标**：通过运行现成测试，看清三种选择策略在同一组信号下的不同胜者。

**操作步骤**：

1. 打开 [engine_tier_test.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine_tier_test.go)。
2. 阅读三个用例，分别回答：
   - `SelectBestDecisionPrefersLowerTier`：Tier 1 vs Tier 2，谁赢？（答案：Tier 1 的 `jailbreak-block`）
   - `SelectBestDecisionUsesConfidenceWithinTier`：同 Tier 2，置信度 0.52 vs 0.89，谁赢？（答案：高置信度的 `science-route`）
   - `SelectBestDecisionFallsBackToPriorityOnTierConfidenceTie`：同 Tier 2、同置信度 0.89，Priority 200 vs 100，谁赢？（答案：高优先级的 `math-route`）
3. 运行验证：

   ```bash
   cd src/semantic-router
   go test ./pkg/decision/ -run 'TestDecisionEngine_SelectBestDecision' -v
   ```

**需要观察的现象**：三个用例分别覆盖「Tier 主键 → 置信度次键 → 优先级第三键」这条比较链的每一级。

**预期结果**：全部 `PASS`，胜者与上述答案一致。

#### 4.4.5 小练习与答案

**练习 1**：两条决策都命中、都无 Tier、`strategy=priority`，A 的 Priority=100 置信度=0.9，B 的 Priority=100 置信度=0.5，谁赢？

**参考答案**：Priority 相同（都是 100），进入置信度降序比较，A 的 0.9 > 0.5，**A 赢**。

**练习 2**：若把上题的 `strategy` 改成 `confidence`，结果会变吗？

**参考答案**：不变。`confidence` 策略的主键是置信度降序，A 的 0.9 仍高于 B，A 仍赢。两种策略的差异只在「Priority 和 Confidence 谁是第一主键」上体现——只有当高置信度决策的 Priority 反而更低时，两种策略才会选出不同胜者。

---

## 5. 综合实践

把本讲四个模块串起来，设计一个小型多决策场景并完整推演。

**场景**：一个配方有三条决策，`strategy=confidence`：

```
ROUTE jailbreak_block   (Tier=1)  WHEN  jailbreak:detector
ROUTE math_route        (Tier=2)  WHEN  OR(domain:math, complexity:hard)
ROUTE fallback          (无 WHEN, 兜底)
```

**给定信号**：`DomainRules=["math"]`、`ComplexityRules=["hard"]`、`JailbreakRules=["detector"]`，`SignalConfidences={"domain:math":0.7, "complexity:hard":0.9, "jailbreak:detector":0.6}`（假设 complexity 走存在性匹配、置信度取 map 值）。

**任务**：

1. **规则树求值**：分别判断三条决策是否命中。
   - `jailbreak_block`：叶子 `jailbreak:detector` 命中，置信度 0.6。
   - `math_route`：`OR(domain:math=0.7, complexity:hard=0.9)` 命中，取最大 ⇒ 置信度 0.9。
   - `fallback`：省略规则 ⇒ 命中，置信度 0。
2. **选择**：三条全命中。任一 Tier>0（有 Tier=1 和 Tier=2）⇒ 进入分层模式。Tier 升序：`jailbreak_block`(1) < `math_route`(2) < `fallback`(0? 不，fallback 没标 Tier，Tier=0)。

   ⚠️ **陷阱**：`fallback` 的 Tier=0，在分层模式下 Tier 升序会把它排到**最前**！这提醒我们：**进入分层模式后，未标 Tier 的兜底（Tier=0）反而会胜出**——这与直觉相反。正确的做法是：要么给兜底也标一个较大的 Tier，要么依赖配置校验保证「只要有一条决策标了 Tier，所有决策都应显式标 Tier」。

3. **反思**：基于上述陷阱，写出两条配置建议：
   - 兜底路由若参与分层配方，应显式标 Tier（且通常是最大值）。
   - 或者在非分层配方里彻底不用 Tier，让 `confidence`/`priority` 策略接管。

**验证方式**：把上述三条决策和信号照 [engine_tier_test.go:127-139](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine_tier_test.go#L127-L139) 的 `testDecision` 辅助函数写成一个临时测试，断言胜者是谁，运行验证你的推演（待本地验证）。

## 6. 本讲小结

- 决策引擎是**无状态求值器**：每次请求传入 `SignalMatches`，对每条决策的规则树递归求值，产出 `(matched, confidence, matchedRules)`。
- `RuleNode` 身兼两职：`Type` 非空是叶子，`Operator`+`Conditions` 是组合节点；`evalNode` 据此分发到 `evalLeaf` / `evalAND` / `evalOR` / `evalNOT`。
- 叶子匹配以**存在性**为主（`slices.Contains`），`domain` 走 `mmlu_categories` 别名扩展，`classifier` 仅支持 `Predicate` 数值断言；置信度从 `SignalConfidences` 取，**缺失默认 1.0**。
- 置信度聚合三规则：**AND 取平均、OR 取最大、NOT 命中固定 1.0**；空 AND / 省略规则是置信度 0 的兜底。
- 选择胜者由 `decisionResultLess` 单一排序实现：**任一 Tier>0 ⇒ 分层模式（Tier 升序优先）**；否则按 `strategy` 取 `priority`（Priority 降序优先）或 `confidence`（置信度降序优先）；全部 tie 时按 Name 升序做确定性兜底。
- 桥接点在 [classifier_signal_decision.go:56-98](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/classifier_signal_decision.go#L56-L98)：分类包构造 `SignalMatches` 并调用 `EvaluateDecisionsWithSignals`，决策包不反向依赖分类包。

## 7. 下一步学习建议

- **下一步学选择算法**：本讲只讲到「选出命中的决策」，决策里的 `MODEL` 如何被进一步筛选成唯一后端模型，由 u6-l2「选择算法注册表：Elo/Hybrid 等」接棒——那里讲 `Selector` 接口与 Bradley-Terry 等。
- **回看信号实现**：本讲把 `SignalMatches` 当黑盒输入。想知道 `DomainRules`/`ComplexityRules` 这些切片是怎么填出来的，去 u8「分类信号系统」，尤其是 u8-l2 的 `category_classifier` 与 `mmlu_categories` 映射。
- **续读源码**：若对可解释性感兴趣，阅读 [pkg/decision/trace.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/trace.go)（`EvaluateDecisionsWithTrace`），它把本讲的求值过程录成结构化追踪，与 u11-l2 的投影追踪配套，构成完整的可解释性链路。
