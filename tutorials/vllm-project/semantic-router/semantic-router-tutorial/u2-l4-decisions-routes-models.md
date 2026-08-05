# Decisions/Routes/Models：决策路由与模型

## 1. 本讲目标

本讲是「信号-投影-决策」心智模型的最后一站。前两讲我们已经知道：**信号**从请求里抽取事实，**投影**把相互竞争的信号协调成少数干净的命名路由带。本讲要回答流水线的最后一问——**有了信号和投影，路由器到底选哪条路、用哪个模型？**

学完后你应该能够：

- 看懂一条 `ROUTE` 上的 `WHEN` 规则，理解它在「信号 + 投影」上用 `AND`/`OR`/`NOT` 是怎么组合求值的；
- 理解 `PRIORITY` 与 `TIER` 两个数字谁先起作用、谁做兜底，以及为什么必须有一条「没有 `WHEN`」的最终兜底路由；
- 理解 `MODEL` 声明里的 `capabilities`/`quality_score`/`tags` 等元数据的作用，以及 `reasoning`/`effort` 参数如何随路由下发。

> 本讲只讲「决策如何匹配与排序」的**规则语义**。规则树的具体递归求值算法、置信度聚合的数学细节，会在进阶层讲义 u6-l1（决策引擎）里深入展开；本讲给出足够的直觉，让你能读懂 `recipe.dsl`。

## 2. 前置知识

在进入本讲前，请确认你已经建立以下概念（来自 u2-l1 ~ u2-l3）：

- **信号（Signal）**：从一次请求里抽取的一类「事实」，每条信号带一个 0~1 的置信度，键形如 `"族:名"`，例如 `embedding:fast_qa_en`、`keyword:code_request_markers`。信号只抽取事实，**不做路由决定**。
- **投影（Projection）**：把杂乱的信号协调成少数命名路由带。本讲会频繁出现投影带，例如 `projection("balance_simple")`、`projection("balance_complex")`、`projection("verification_required")`，它们是 u2-l3 里 `mapping` 投影产出的结果，被反写进信号置信度。
- **命名带**：投影输出的字符串标签，如 `balance_simple`、`balance_medium`、`balance_complex`、`balance_reasoning`。

本讲用到的两个关键术语：

- **决策（Decision）**：路由器内部对一条 `ROUTE` 的称呼。`recipe.dsl` 里你写的是 `ROUTE`，编译后它在 Go 配置里叫 `Decision`。本讲会交替使用这两个词。
- **匹配（match）**：一条 `ROUTE` 的 `WHEN` 规则在该次请求的信号集合上求值为真，就说这条路由「匹配」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [config/recipes/balance/recipe.dsl](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl) | balance 配方的 DSL 源码，包含真实的 `SIGNAL`/`PROJECTION`/`MODEL`/`ROUTE` 块，是本讲的主要阅读素材。 |
| [src/semantic-router/pkg/config/decision_config.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/decision_config.go) | `Decision`、`RuleNode`、`ModelRef` 等结构体定义，是 `ROUTE`/`MODEL` 编译后的运行时表示。 |
| [src/semantic-router/pkg/dsl/ast.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go) | DSL 的解析后 AST：`RouteDecl`、`BoolAnd`/`BoolOr`/`BoolNot`、`ModelRef` 等。 |
| [src/semantic-router/pkg/dsl/compiler_routes.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler_routes.go) | 把 AST 里的 `ROUTE` 编译成 `Decision`、把 `WHEN` 布尔表达式编译成规则树、把 `MODEL` 编译成 `ModelRef`。 |
| [src/semantic-router/pkg/decision/engine.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go) | 决策引擎：求值规则树、聚合置信度、按 `PRIORITY`/`TIER`/`confidence` 选出最佳决策。 |
| [src/semantic-router/pkg/config/model_config_types.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/model_config_types.go) | `ModelParams` 结构体，承载 `capabilities`/`quality_score`/`tags`/`modality` 等模型元数据。 |
| [src/semantic-router/pkg/config/recipes.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/recipes.go) | `RoutingStrategy`（`priority` / `confidence`）的定义与校验。 |

---

## 4. 核心概念与源码讲解

### 4.1 ROUTE 规则树：WHEN 的 AND/OR/NOT

#### 4.1.1 概念说明

一条 `ROUTE` 描述「**在什么情况下，把请求送给哪个（些）模型**」。它的骨架是：

```
ROUTE <名字> (description = "...") {
  PRIORITY <整数>
  TIER     <整数>
  WHEN <布尔表达式>          # 可省略，省略即"永远匹配"
  MODEL "<模型名>" (reasoning = true, effort = "high")
  PLUGIN <插件名> { ... }
}
```

其中 `WHEN` 是这条路由的「守卫条件」。它是一个**布尔表达式**，操作数不是普通变量，而是**信号引用**和**投影引用**：

- 信号引用：`domain("health")`、`keyword("code_request_markers")`、`embedding("fast_qa_en")`、`user_feedback("wrong_answer")`、`context("short_context")`、`reask("likely_dissatisfied")`、`fact_check("needs_fact_check")`、`language("en")`、`complexity("math_task:hard")`……
- 投影引用：`projection("balance_complex")`、`projection("verification_required")`。

> 直觉：`WHEN` 就是在问一连串「这次请求像不像 X？」的判断题，再用 `AND`（并且）、`OR`（或者）、`NOT`（不是）把它们拼起来。信号/投影命中就是「真」，没命中就是「假」。

三种布尔算子的语义和日常逻辑一致：

| 算子 | 含义 | 命中条件 |
| --- | --- | --- |
| `AND` | 并且 | **所有**子条件都命中 |
| `OR` | 或者 | **至少一个**子条件命中 |
| `NOT` | 非 | 子条件**没有**命中时为真 |

#### 4.1.2 核心流程

WHEN 表达式的求值是一棵**递归布尔树**。优先级上，`OR` 比 `AND` 结合得更松（和数学里一样：先算 AND，再算 OR），`NOT` 紧贴在它右边的一项上。所以：

```
A OR B AND C        等价于   A OR (B AND C)
NOT A OR B          等价于   (NOT A) OR B
```

求值过程（伪代码）：

```
函数 evalNode(节点, 信号集合):
    若节点是叶子（单个信号/投影引用）:
        在信号集合里查这个 "族:名" 是否命中
        若命中 → 返回 (真, 该信号的置信度)
        若未命中 → 返回 (假, 0)
    否则按节点算子:
        AND → 对每个子节点求值；任一为假则整体为假；否则置信度 = 子置信度的平均
        OR  → 对每个子节点求值；取所有命中子节点里置信度最高的那个
        NOT → 只有一个子节点；子节点为假时 NOT 为真（置信度 1.0），子节点为真时 NOT 为假
```

注意置信度的聚合方式（这一点会在 u6-l1 详细讲）：

- `AND` 取子置信度的**算术平均**：\(\text{conf}_{AND} = \dfrac{1}{n}\sum_{i=1}^{n} c_i\)；
- `OR` 取命中子项里的**最大值**：\(\text{conf}_{OR} = \max_{i \in \text{命中}} c_i\)；
- `NOT` 在「子条件为假」时返回置信度 \(1.0\)（「肯定不是它」这件事很笃定）。

这套设计的好处是：一条路由不仅告诉你「匹配 / 不匹配」，还附带一个 0~1 的**匹配置信度**，供后面「多条路由都匹配时谁优先」做参考。

#### 4.1.3 源码精读

**① DSL 里真实的 WHEN 长什么样**

先看一条结构清晰的 `premium_legal` 路由，它的 WHEN 是「一个 OR 组」与「另一个 OR 组」用 `AND` 连接：

[config/recipes/balance/recipe.dsl:L496-L508](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L496-L508) —— 这条路由把「法律领域相关」(`domain("law") OR keyword("legal_risk_markers") OR embedding("premium_legal_analysis")`) 与「需要高规格分析」(`embedding("premium_legal_analysis") OR projection("verification_required") OR complexity("legal_risk:medium") OR complexity("legal_risk:hard")`) 用 `AND` 组合，命中即送给最强模型 `anthropic/claude-opus-4.6`。

再看一个带 `NOT` 的例子 `formal_math_proof`：

[config/recipes/balance/recipe.dsl:L510-L513](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L510-L513) —— 它要求「是数学领域 `AND` 有推理请求词」，并且 `NOT`（一大串「需要核验/纠错/写代码」的信号）。`NOT` 在这里的作用是**排除**：哪怕这是个数学题，只要用户在要求核验、纠错或写代码，就**不要**走这条狭窄的形式化证明专用道。

> 观察规律：balance 配方里几乎所有专用路由都用 `... AND NOT (...)` 的模式，`NOT` 后面挂一串「该请求其实属于别的车道」的排除条件。这是避免路由相互「抢客」的常用写法。

**② WHEN 解析成哪种 AST**

DSL 解析器把布尔表达式建成接口 `BoolExpr` 的四种节点：

[src/semantic-router/pkg/dsl/ast.go:L516-L554](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L516-L554) —— `BoolAnd`/`BoolOr` 各有 `Left`/`Right` 两个孩子，`BoolNot` 有一个 `Expr` 孩子，`SignalRefExpr` 是叶子，记录 `SignalType`（如 `domain`）与 `SignalName`（如 `"health"`）。优先级由文法层保证：`OR` 在最外层、`AND` 在中层、`NOT`/括号在最内层。

**③ WHEN 编译成运行时规则树**

编译时，`compileBoolExpr` 把上面的 AST 转成 `config.RuleCombination`（其实就是 `RuleNode`），并把**同层连续的同算子「拍平」**：`(a AND b) AND c` 会被压成 `AND(a, b, c)` 一个节点带三个孩子，而不是两层嵌套。

[src/semantic-router/pkg/dsl/compiler_routes.go:L155-L196](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler_routes.go#L155-L196) —— 注意三个分支：`BoolAnd` 产出 `Operator:"AND"`、`BoolOr` 产出 `Operator:"OR"`、`BoolNot` 产出 `Operator:"NOT"` 且只有 1 个孩子；叶子 `SignalRefExpr` 产出 `{Type, Name}`。拍平由 `flattenBoolExpr` 完成。

**④ 运行时怎么存这条规则树**

`RuleNode` 是个递归结构：要么是「叶子」（带 `Type`/`Name`，引用一个信号/投影），要么是「组合」（带 `Operator` 和 `Conditions` 子树）：

[src/semantic-router/pkg/config/decision_config.go:L161-L186](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/decision_config.go#L161-L186) —— `IsLeaf()` 看 `Type` 是否非空；`IsEmpty()` 用于识别「完全没有规则」的节点（见 4.2 节的兜底路由）。注意 `RuleCombination` 和 `RuleCondition` 都只是 `RuleNode` 的别名（第 183-186 行），它们是同一个递归类型。

**⑤ 决策引擎如何求值这棵树**

求值的入口在 `evaluateDecisionWithSignals`，它先判断「有没有规则」，再调用 `evalNode` 递归求值；`evalNode` 根据算子分发到 `evalAND`/`evalOR`/`evalNOT`：

[src/semantic-router/pkg/decision/engine.go:L185-L201](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L185-L201) —— `evalNode` 对叶子调 `evalLeaf`（在信号集合里查命中、取置信度），对组合节点按 `Operator` 分发：`AND`/`NOT` 各走一条，**其它（包括 `OR`）走默认的 `evalOR`**。

三种聚合方式在源码里一目了然：

- [src/semantic-router/pkg/decision/engine.go:L402-L419](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L402-L419) —— `evalAND`：任一子节点不命中即整体失败；全部命中则置信度 = `totalConf / 孩子数`（平均）。特别注意**空孩子列表返回 `(true, 0)`**，这正是「没有 WHEN」的兜底路由能无条件匹配的原因之一。
- [src/semantic-router/pkg/decision/engine.go:L422-L440](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L422-L440) —— `evalOR`：遍历所有子节点，记录命中者里置信度最高的那一个。
- [src/semantic-router/pkg/decision/engine.go:L444-L459](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L444-L459) —— `evalNOT` 是**严格一元**算子：孩子数必须正好是 1，否则视为不匹配；孩子不命中时 NOT 命中且置信度 1.0，孩子命中时 NOT 不命中。

#### 4.1.4 代码实践

**实践目标**：手工推演一条含 `AND`/`OR`/`NOT` 的 WHEN，验证你能算出它的求值结果与聚合置信度。

**操作步骤**：

1. 取 `premium_legal` 的 WHEN（[recipe.dsl:L499](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L499)）。
2. 假设某次请求命中如下信号/投影（其余均未命中）：
   - `domain("law")` 命中，置信度 `1.0`；
   - `embedding("premium_legal_analysis")` 命中，置信度 `0.83`；
   - `complexity("legal_risk:hard")` 命中，置信度 `0.9`；
   - `keyword("legal_risk_markers")`、`projection("verification_required")`、`complexity("legal_risk:medium")` 均**未**命中。
3. 按优先级分组求值：
   - 左边 `OR` 组 = `domain("law") OR keyword(...) OR embedding(...)`：三个里前两个和第三个命中，`evalOR` 取最大置信度 \(\max(1.0, 0.83) = 1.0\)。
   - 右边 `OR` 组 = `embedding(...) OR projection(...) OR complexity(:medium) OR complexity(:hard)`：命中者为 `embedding`(0.83) 和 `complexity:hard`(0.9)，取最大 \(0.9\)。
   - 整体是 `AND`：两边都为真，置信度 \(= (1.0 + 0.9)/2 = 0.95\)。

**需要观察的现象**：`OR` 取最大、`AND` 取平均；只要有一个 `OR` 子项命中该组就为真。

**预期结果**：该请求匹配 `premium_legal`，匹配置信度约 `0.95`。

> 待本地验证：上述置信度数值取决于真实分类器输出，这里用于演示聚合规则；若你想看真实数值，可在 u11 讲义的可观测性部分学会打印投影追踪与决策置信度。

#### 4.1.5 小练习与答案

**练习 1**：把 `A AND B OR C` 加上括号，写成等价形式。

> **答案**：`(A AND B) OR C`。因为 `AND` 比 `OR` 结合更紧。

**练习 2**：若 `NOT` 后面误写了两个子条件（例如 `NOT (A) (B)`），按 [engine.go:L444-L459](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L444-L459) 的实现，结果会怎样？

> **答案**：`evalNOT` 要求孩子数恰为 1，否则打印告警并视为**不匹配**（返回假）。所以 `NOT` 只能是一元算子，要排除多个条件应写成 `NOT (A OR B OR C)`。

---

### 4.2 优先级与兜底：PRIORITY、TIER 与命中顺序

#### 4.2.1 概念说明

一次请求可能**同时匹配多条路由**。例如一个「简短的代码问题」，可能既满足 `fast_qa` 又满足 `medium_code_general`。路由器必须有一个**确定性**的规则决定胜者，否则同一条请求每次结果不同，系统就不可靠。

vLLM SR 给每条路由两个数字来排序：

- **`PRIORITY`**：优先级，**数字越大越优先**。
- **`TIER`**：层级，**数字越小越优先**（像「梯队」，第 1 梯队最先）。

此外还有一个全局的 **`routing.strategy`**（路由策略），目前只有两个合法值：`priority`（默认）和 `confidence`。

> 直觉：`TIER` 像「先把路由分到大组（梯队），同组内再细排」；`PRIORITY` 像同一层里的「插队优先级」。两个数字配合，能既保证「高价值专用道」永远优先，又允许在同级里按需要微调顺序。

还有一个**关键约定**：每个配方必须有一条**没有 `WHEN` 的最终兜底路由**，保证「任何请求都至少匹配一条路由」，永不落空。

#### 4.2.2 核心流程

决策引擎对一次请求的处理分两步（见 u6-l1 详述）：

1. **遍历所有决策**，对每条都用 `evalNode` 求值它的规则树，收集所有「匹配」的决策及其置信度。
2. **`selectBestDecision`**：把所有匹配者排序，取第一名。

排序的「比较键」由两个因素共同决定：

- **是否有任何匹配决策设置了 `TIER > 0`**：如果有，进入「分层选择」模式（`useTieredSelection = true`）；
- **否则**按全局 `strategy` 决定。

三种排序方式的比较键（从前到后，前面能分出胜负就看前面）：

| 模式 | 第 1 键 | 第 2 键 | 第 3 键 | 第 4 键 |
| --- | --- | --- | --- | --- |
| 分层选择（有 TIER） | `TIER` **升序**（小者优先） | `confidence` **降序** | `PRIORITY` **降序** | `Name` 升序（兜底字典序） |
| `confidence` 策略 | `confidence` **降序** | `PRIORITY` **降序** | `Name` 升序 | — |
| `priority` 策略（默认） | `PRIORITY` **降序** | `confidence` **降序** | `Name` 升序 | — |

> 重要结论：**只要配方里给路由标了 `TIER`，`TIER` 就会成为主排序键，`PRIORITY` 退化为「同 TIER 内的 tiebreaker」。** balance 配方给每条路由都标了独一无二的 `TIER`（1~14），所以最终顺序完全由 `TIER` 决定，`PRIORITY` 在这里其实不起决定作用。

#### 4.2.3 源码精读

**① Decision 结构里的两个数字**

[src/semantic-router/pkg/config/decision_config.go:L3-L9](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/decision_config.go#L3-L9) —— `Decision` 顶部的 `Priority`（第 7 行）和 `Tier`（第 8 行）就是 DSL 里 `PRIORITY`/`TIER` 编译后的归宿。`Rules` 字段（第 11 行）即规则树，`ModelRefs`（第 12 行）即选中的模型。

**② 路由策略只有两个合法值**

[src/semantic-router/pkg/config/recipes.go:L17-L35](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/recipes.go#L17-L35) —— `RoutingStrategyPriority = "priority"`、`RoutingStrategyConfidence = "confidence"`，`Validate()` 会拒绝其它值。空字符串在引擎里被当作 `priority`（见 engine.go 的 `NewDecisionEngine`）。

**③ 选最佳决策的主干**

[src/semantic-router/pkg/decision/engine.go:L486-L501](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L486-L501) —— `selectBestDecision` 先调 `useTieredSelection` 判断模式，再用 `sort.Slice` + `decisionResultLess` 排序，**取排序后的第 0 个**作为胜者。

**④ 是否进入分层选择**

[src/semantic-router/pkg/decision/engine.go:L503-L510](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L503-L510) —— 只要**任意一个**匹配决策的 `Tier > 0`，就启用分层选择。这就是「给路由标了 TIER，TIER 就接管排序」的实现原因。

**⑤ 三种比较方式**

[src/semantic-router/pkg/decision/engine.go:L512-L547](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L512-L547) —— 逐段对照上表：

- 第 517-528 行是分层选择：先 `Tier` 升序（`left.Tier < right.Tier`），再 `confidence` 降序，再 `Priority` 降序，最后 `Name` 升序。
- 第 530-538 行是 `confidence` 策略：先 `confidence` 降序，再 `Priority` 降序，再 `Name`。
- 第 540-546 行是 `priority` 策略：先 `Priority` 降序，再 `confidence` 降序，再 `Name`。

注意每段最后都用 `Name` 字典序做最终 tiebreaker——这保证了「即便所有数字都相同，排序仍然确定」，不会出现随机结果。

**⑥ 为什么必须有无 WHEN 的兜底**

来看 balance 的最终兜底路由 `casual_chat`：

[config/recipes/balance/recipe.dsl:L678-L689](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L678-L689) —— 它**没有 `WHEN`**，`PRIORITY 10`、`TIER 14` 都是全表最低/最高，意味着「只有在前面 13 条专用路由都不匹配时才轮到它」。没有 `WHEN` 等价于「永远匹配」，因此无论什么请求都至少会命中这一条，路由器永远不会「无路可走」。

「没有 WHEN 永远匹配」在代码里有两条等价路径：

- **YAML 表示**：直接省略 `rules` 字段 → `RuleNode.IsEmpty()` 为真。

  [src/semantic-router/pkg/decision/engine.go:L168-L180](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L168-L180) —— `evaluateDecisionWithSignals` 里：若 `decision.Rules.IsEmpty()` 直接返回 `(true, 0, nil)`，注释明确说明「省略规则是 DSL 路由不写 WHEN 的 YAML 等价物，且只对决策根节点成立」。

- **DSL 编译表示**：DSL 不写 `WHEN` 时，`compileRouteRules` 产出一个**空条件的 AND 节点** `{Operator:"AND", Conditions:[]}`，求值时 `evalAND` 遇到空孩子列表返回 `(true, 0)`（见 4.1.3 ⑤ 引用的 `evalAND`）。

  [src/semantic-router/pkg/dsl/compiler_routes.go:L52-L64](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler_routes.go#L52-L64) —— `r.When == nil` 时返回空 AND 组合，正是 `casual_chat` 这种无 WHEN 路由的来源。

#### 4.2.4 代码实践（本讲指定实践任务）

**实践目标**：对比 balance 里的 `fast_qa` 与 `casual_chat` 两条路由，理解它们的 WHEN 条件、优先级差异，以及为什么 `casual_chat` 作为最终兜底没有 WHEN。

**操作步骤**：

1. 打开 [recipe.dsl:L650-L662](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L650-L662)（`fast_qa`）与 [recipe.dsl:L678-L689](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L678-L689)（`casual_chat`）。
2. 写一段话回答下面三个问题（参考答案见后）：
   - 两条路由各自的 `WHEN` 条件是什么？
   - 两条路由的 `PRIORITY`/`TIER` 差异如何？
   - 为什么 `casual_chat` 没有 `WHEN`？

**参考答案**：

- **`fast_qa` 的 WHEN**：要求请求是「简短的中英文事实问答」且落在简单/中等难度带——它是一长串 `AND`/`OR` 的精细条件，核心是 `(embedding("fast_qa_en") AND language("en")) OR (embedding("fast_qa_zh") AND language("zh")) OR keyword("simple_request_markers")`，再叠加 `context("short_context")`、难度带 `balance_simple`/`balance_medium`、以及一串 `NOT`（排除健康、法律、代码、紧急、纠错等该走别的车道的情形）。它**有明确的守卫**，只在「便宜就能答对的简单题」时匹配。`PRIORITY 184`、`TIER 12`。
- **`casual_chat` 的 WHEN**：**没有 WHEN**，因此无条件匹配。`PRIORITY 10`、`TIER 14`——都是全表最末。
- **优先级差异**：因为 balance 给所有路由都标了 `TIER`，按 [engine.go:L503-L510](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L503-L510) 进入分层选择，`TIER` 升序为主键。`fast_qa`（TIER 12）远在 `casual_chat`（TIER 14）之前；只有当 TIER 1~13 的所有专用路由都不匹配时，请求才会落到 TIER 14 的 `casual_chat`。这里的 `PRIORITY`（184 vs 10）由于 TIER 互不相同，**并不实际影响**两者的先后，只在「同 TIER 内」才有用。
- **为什么 `casual_chat` 没有 WHEN**：它是**绝对兜底**。没有 WHEN = 永远匹配（见 4.2.3 ⑥），保证「任何请求都至少命中一条路由」。把它放在最低 TIER，意味着它只在「没有任何更贴切的专用路由」时才接管，把请求送到最便宜的默认模型 `qwen/qwen3.5-rocm`，既不丢请求、又不多花钱。

**需要观察的现象**：把 `casual_chat` 的 `TIER` 改成 1（最高优先），所有请求都会被它「截胡」，前面的专用路由形同虚设——这验证了 TIER 是主排序键。

**预期结果**：能用自己的话讲清「TIER 主排序、PRIORITY 仅在同 TIER 内 tiebreak、无 WHEN = 兜底」三点。

> 待本地验证：若你已按 u1/u12 讲义搭起本地栈，可构造一条「明显不属于任何专用道」的请求（例如一句普通的闲聊），用 `vllm-sr` 的路由追踪或 apiserver 的 classify 端点观察它是否最终落到 `casual_chat`。

#### 4.2.5 小练习与答案

**练习 1**：假设 balance 没有给任何路由标 `TIER`（全部 TIER=0），那么 `fast_qa`(PRIORITY 184) 和 `casual_chat`(PRIORITY 10) 同时匹配时谁赢？

> **答案**：因为没有任何匹配决策的 `Tier > 0`，`useTieredSelection` 为假，走默认 `priority` 策略。`PRIORITY` 降序为主键，`fast_qa`(184) > `casual_chat`(10)，`fast_qa` 赢。

**练习 2**：为什么不把 `casual_chat` 的 WHEN 写成「匹配一切」的 `projection("balance_simple") OR NOT projection("balance_simple")`，而是干脆不写 WHEN？

> **答案**：不写 WHEN 时置信度为 0 且语义清晰（无条件的终端决策）；写成恒真表达式既啰嗦又会被求值成带置信度的普通匹配，还可能被校验器告警。`IsEmpty()` 这条专门路径（[engine.go:L176-L178](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go#L176-L178)）才是「兜底」的规范表达，且注释强调该语义**只在决策根节点成立**，不会被嵌套节点误用。

---

### 4.3 MODEL 元数据：capabilities / quality_score / tags / reasoning

#### 4.3.1 概念说明

`ROUTE` 命中后，要把请求送给模型。模型在 DSL 里分两层声明：

- **`MODEL` 块**（顶层声明）：描述一个模型的**目录元数据**——它是谁、擅长什么、有多好、什么模态、什么成本档位。这是全配方共享的「模型货架」。
- **`ROUTE` 里的 `MODEL` 引用**：从货架上挑一个（或多个）模型，并附上**本次路由专属的推理控制参数** `reasoning`/`effort`。

模型元数据字段（来自 `MODEL` 块）的含义：

| 字段 | 含义 | 例 |
| --- | --- | --- |
| `context_window_size` | 上下文窗口大小（token） | `262144` |
| `description` | 人类可读说明 | `"PREMIUM tier alias ..."` |
| `capabilities` | 能力标签数组 | `["legal_analysis", "policy_review"]` |
| `tags` | 自由标签数组，常含 tier/cost/specialty | `["tier:premium", "cost:highest", "specialty:legal"]` |
| `quality_score` | 质量分（0~1） | `0.94` |
| `modality` | 模态 | `"text"` |

路由引用模型时可下发的推理参数：

| 参数 | 含义 |
| --- | --- |
| `reasoning = true/false` | 是否开启该模型的「思考/推理」模式 |
| `effort = "low"/"medium"/"high"` | 推理强度（思考多少） |

> 直觉：`MODEL` 块是「简历」，`capabilities`/`quality_score`/`tags` 是简历上的技能、评分、标签；`ROUTE` 里的 `reasoning`/`effort` 是「这次任务要不要让模型多想想」。元数据本身**不直接决定路由是否命中**（那是 WHEN 的职责），但它会被选择算法（u6-l2）、成本感知（u6-l3）和面板（u13）消费，用来在多个候选模型里挑最合适的。

#### 4.3.2 核心流程

模型从声明到被使用的链路：

```
DSL 顶层 MODEL 块          ┐
                           │  编译 → config.ModelParams（元数据：capabilities/quality_score/tags/...）
                           │         存进 RouterConfig.ModelConfig[name]
ROUTE 里的 MODEL "xxx"     ┘
  (reasoning=, effort=)       编译 → config.ModelRef（推理控制：UseReasoning/ReasoningEffort）
                                     存进 Decision.ModelRefs[]
        │
        ▼
决策命中 → 取 Decision.ModelRefs → 选择算法在候选模型里挑（可参考 quality_score/cost）→ 下发到后端
```

注意区分两个容易混淆的 `ModelRef`：

- **DSL 层的 `ModelRef`**（[ast.go:L559-L567](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L559-L567)）：解析后的中间表示，字段是 `Model`/`Reasoning`/`Effort`/`LoRA`/`Weight`。
- **config 层的 `ModelRef`**（[decision_config.go:L154-L159](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/decision_config.go#L154-L159)）：编译后的运行时表示，内嵌 `ModelReasoningControl`（`UseReasoning`/`ReasoningEffort`）。

#### 4.3.3 源码精读

**① balance 的模型货架**

[config/recipes/balance/recipe.dsl:L441-L484](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L441-L484) —— 五个模型，覆盖从「免费自托管」到「最贵 premium」的全档位：

| 模型 | quality_score | tags（节选） | 角色 |
| --- | --- | --- | --- |
| `anthropic/claude-opus-4.6` | 0.94 | `tier:premium`, `cost:highest` | 高风险法律专用 |
| `openai/gpt5.4` | 0.90 | `tier:reasoning`, `cost:high` | 形式化数学证明 |
| `google/gemini-3.1-pro` | 0.82 | `tier:complex`, `cost:upper_mid` | 系统设计/硬 STEM/深度推理 |
| `google/gemini-2.5-flash-lite` | 0.68 | `tier:medium`, `cost:low` | 低成本核验/纠错 |
| `qwen/qwen3.5-rocm` | 0.58 | `tier:simple`, `cost:free` | 免费自托管默认，承接大多数流量 |

可以看到 `quality_score` 与成本档位正相关：越强的模型越贵。路由的意义正在于「**简单题用便宜模型、难题才升级**」。

**② 元数据在 Go 里的结构**

[src/semantic-router/pkg/config/model_config_types.go:L323-L339](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/model_config_types.go#L323-L339) —— `ModelParams` 承载 `Capabilities`、`Tags`、`QualityScore`、`Modality`、`ContextWindowSize`、`Pricing`（成本）、`Description` 等。`MODEL` 块编译后即生成此结构的实例，按模型名存进 `RouterConfig.ModelConfig`。

**③ 路由如何引用模型并下发推理参数**

[config/recipes/balance/recipe.dsl:L500](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L500) —— `MODEL "anthropic/claude-opus-4.6" (reasoning = true, effort = "high")`：这条 premium 路由要求模型开满推理强度。对比 [recipe.dsl:L556](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L556) 的 `feedback_wrong_answer_verified` 用 `MODEL "google/gemini-2.5-flash-lite" (reasoning = false)`——简单核验任务直接关推理，省钱省时。

**④ 编译如何把 reasoning/effort 落到运行时**

[src/semantic-router/pkg/dsl/compiler_routes.go:L84-L110](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler_routes.go#L84-L110) —— `appendModelRef` 把 DSL 的 `ModelRef` 翻译成 config 的 `ModelRef`：`m.Reasoning`（`*bool`）→ `ref.UseReasoning`，`m.Effort` → `ref.ReasoningEffort`。这两个字段最终内嵌在 `Decision.ModelRefs[i]` 里，随决策命中一起生效。

**⑤ 推理控制的结构定义**

[src/semantic-router/pkg/config/decision_config.go:L148-L159](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/decision_config.go#L148-L159) —— `ModelReasoningControl` 含 `UseReasoning *bool`（指针，可区分「未设」与「显式 false」）、`ReasoningEffort string`；`ModelRef` 通过 `yaml:",inline"` 把它内联进来。

#### 4.3.4 代码实践

**实践目标**：把 balance 的五条「专用路由 → 模型 → 推理参数」对应关系梳理成一张表，验证「难度/风险越高，模型越强、推理越开」。

**操作步骤**：

1. 遍历 [recipe.dsl:L496-L689](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L496-L689) 的 14 条 `ROUTE`。
2. 对每条记录：路由名、`MODEL` 引用、`reasoning`/`effort`。
3. 与 4.3.3 ① 的模型质量分对照。

**需要观察的现象**：高 TIER（更优先、更专用）的路由倾向用高 `quality_score` 模型 + `effort="high"`；低 TIER（兜底类）的路由用 `qwen/qwen3.5-rocm` + `reasoning=false`。

**预期结果**（节选）：

| 路由 | TIER | 模型 | quality_score | reasoning / effort |
| --- | --- | --- | --- | --- |
| `premium_legal` | 1 | claude-opus-4.6 | 0.94 | true / high |
| `formal_math_proof` | 2 | gpt5.4 | 0.90 | true / high |
| `complex_specialist` | 4 | gemini-3.1-pro | 0.82 | true / high |
| `feedback_wrong_answer_verified` | 5 | gemini-2.5-flash-lite | 0.68 | false |
| `fast_qa` | 12 | qwen3.5-rocm | 0.58 | false |
| `casual_chat` | 14 | qwen3.5-rocm | 0.58 | false |

可见路由层把「模型能力」与「任务难度」做了精细对齐：贵模型只在真正需要时才被调用。

> 待本地验证：`quality_score`/`capabilities` 是否参与「同一决策的多个候选模型」之间的挑选，取决于该路由是否声明了 `ALGORITHM`（选择算法）。本讲的 balance 路由大多是单模型引用，选择算法的细节在 u6-l2 展开。

#### 4.3.5 小练习与答案

**练习 1**：`quality_score = 0.58` 的 `qwen3.5-rocm` 为什么会被大量路由（fast_qa/simple_general/casual_chat/…）引用？

> **答案**：因为它是 `cost:free` 的自托管默认模型，承接「简单题」足够好又几乎不花钱。路由的核心价值之一就是「**能省则省**」——简单题用便宜模型，把贵的算力留给真正需要的请求。

**练习 2**：`reasoning` 字段在 config 里是 `*bool`（指针）而不是 `bool`，为什么？

> **答案**：见 [decision_config.go:L148-L152](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/decision_config.go#L148-L152)。指针能区分三种状态：「未设置」（用模型默认行为）、「显式 true」、「显式 false」。普通 `bool` 的零值 `false` 会和「显式关推理」混淆，无法表达「作者没写、交给模型决定」。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个小设计任务：

**任务**：在 balance 配方之外，设想你要新增一条路由 `math_quick_hint`，专门承接「数学领域的简单提问」（用便宜模型快速给提示，不开推理）。请按下列要求写出它的 `ROUTE` 片段，并解释每一行的依据。

要求：

1. 用 `domain("math")` 与某个简单/快速信号（如 `embedding("fast_qa_en")` 或 `keyword("simple_request_markers")`）用 `AND` 组合作为主条件；
2. 用 `NOT` 排除「需要形式化证明」的情形（参考 `keyword("reasoning_request_markers")`）；
3. 给一个比 `formal_math_proof`(TIER 2) 更大、但比 `fast_qa`(TIER 12) 更小的 `TIER`，并说明为什么；
4. 选一个低成本模型并设 `reasoning = false`。

**参考片段（示例代码，非项目原有内容）**：

```
ROUTE math_quick_hint (description = "Cheap quick hints for simple math questions.") {
  PRIORITY 200
  TIER 8
  WHEN domain("math") AND (embedding("fast_qa_en") OR keyword("simple_request_markers")) AND NOT keyword("reasoning_request_markers")
  MODEL "qwen/qwen3.5-rocm" (reasoning = false)
  PLUGIN router_replay {
    enabled: true
    max_records: 100000
    capture_request_body: true
    capture_response_body: true
    max_body_bytes: 4096
  }
}
```

**自检要点**：

- **TIER 取 8 的理由**：它必须比 `formal_math_proof`(TIER 2)「更靠后」，因为简单的数学提问不该抢走形式化证明专用道；但又该比通用兜底 `fast_qa`(TIER 12)/`casual_chat`(TIER 14) 更靠前，因为它是「数学简单题」的更贴切专用道。由于 balance 启用了分层选择，TIER 就是主排序键，这个取值直接决定了它的命中顺位。
- **NOT 的作用**：确保「请严格证明…」这类高难请求被排除，留给 TIER 2 的 `formal_math_proof`。
- **模型与 reasoning**：简单题用免费模型且关推理，符合「能省则省」。

> 待本地验证：把上述片段放入一个实验性 recipe，用 `go run ./cmd/dsl validate` 校验语法（见 u7-l3），再用 `go run ./cmd/dsl test` 配合 `TEST` 块验证几条样例查询的路由结果是否符合预期。

## 6. 本讲小结

- **ROUTE 的 WHEN 是一棵布尔规则树**：叶子是信号/投影引用（如 `domain("health")`、`projection("balance_complex")`），组合节点是 `AND`/`OR`/`NOT`。DSL 解析成 `BoolAnd`/`BoolOr`/`BoolNot`/`SignalRefExpr`，编译成 `RuleNode`，运行时由决策引擎递归求值。
- **置信度聚合有定规**：`AND` 取子项平均，`OR` 取命中子项最大值，`NOT` 在子项为假时返回 1.0。匹配置信度会在「多条路由都命中」时参与排序。
- **排序由 TIER 与 strategy 共同决定**：只要任何匹配路由设了 `TIER > 0`，就进入分层选择，`TIER` 升序为主键；否则按 `strategy`（默认 `priority`，或 `confidence`）排序。`Name` 字典序是最终 tiebreaker，保证结果确定。
- **PRIORITY 与 TIER 的分工**：在标了 TIER 的配方里，TIER 是主排序键，PRIORITY 仅作同 TIER 内的微调；两者数字方向相反（PRIORITY 大者优先，TIER 小者优先）。
- **必须有无 WHEN 的最终兜底**：没有 WHEN = 永远匹配（`Rules.IsEmpty()` 或空 AND 两条等价路径），配合最低 TIER，保证任何请求都不会无路可走。
- **MODEL 分元数据与引用两层**：`MODEL` 块是模型货架（`capabilities`/`quality_score`/`tags`/`modality`，存为 `ModelParams`）；`ROUTE` 里的 `MODEL` 引用挑模型并下发 `reasoning`/`effort`（编译为 `ModelRef.ModelReasoningControl`）。元数据不直接决定命中，但供选择算法与成本感知消费。

## 7. 下一步学习建议

- **进入请求主链路**：本讲只讲了「决策的规则语义」，真实的求值发生在请求处理管线里。建议接着学 **u5（请求处理主链路）**，特别是 u5-l2「决策求值管线」，看 `performDecisionEvaluation` 如何在真实请求里调用决策引擎。
- **深入决策引擎算法**：若你想彻底搞懂 `evalNode` 的递归、置信度聚合的数学、`selectBestDecision` 的排序细节，去读 **u6-l1（决策引擎：布尔规则求值与置信度）**，它会逐行拆解 [engine.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/decision/engine.go)。
- **模型如何被挑选**：当一个 `ROUTE` 引用**多个**模型时，`quality_score`/`cost` 才真正参与挑选，这是 **u6-l2（选择算法注册表：Elo/Hybrid 等）** 与 **u6-l3（模型运行时、库存与定价）** 的主题。
- **自己写一条 DSL**：学完 u7（路由 DSL）后，你可以用 `cmd/dsl` 工具校验、编译、测试自己写的 `ROUTE`，把本讲综合实践里的 `math_quick_hint` 真正跑起来。
