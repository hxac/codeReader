# DSL 语法与 AST

> 本讲对应大纲：`u7-l1`，单元 U7「路由 DSL」首篇。前置：u2-l1（信号-投影-决策心智模型）、u3-l1（config.yaml v0.3 整体结构）。

## 1. 本讲目标

学完本讲，你应当能够：

- 用 DSL 的 `SIGNAL` / `PROJECTION` / `MODEL` / `PLUGIN` / `ROUTE` / `RECIPE` / `ENTRYPOINT` / `ROUTING` / `TEST` 等块语法，读懂并手写一份路由策略。
- 说清楚 DSL 源码是如何被**词法分析 → raw 解析树 → 解析后的 Program AST** 三步处理的，以及为什么要分两层 AST。
- 对照 `ast.go` 说出一条 `WHEN` 布尔表达式（含 `AND`/`OR`/`NOT` 与括号）会被解析成哪些节点，并理解运算符优先级。

本讲只讲**语法与 AST 结构**，即「源码字符串如何变成内存里的树」。至于这棵树如何被编译成运行时 `RouterConfig`、又如何反编译回 YAML，留给 u7-l2；`dsl` 命令行工具留给 u7-l3。

## 2. 前置知识

在进入源码前，先用大白话建立两个直觉。

**第一，DSL 是 config.yaml 的「人友好版」。** u3-l1 讲过，运行时权威配置是 `config.yaml`（v0.3），但它又长又嵌套，不适合人读和评审。于是项目提供了一套路由 DSL（Domain-Specific Language，领域专用语言）：你用更像自然语言的关键字（`SIGNAL`、`ROUTE`、`WHEN`）写出策略，工具再把它编译成等价的 YAML。两者描述的是同一件事——信号 → 投影 → 决策 → 模型 + 插件这条流水线（u2-l1）。

**第二，什么是 AST。** AST 全称 Abstract Syntax Tree（抽象语法树）。解析器（parser）读源码字符串时，不是一行行处理文本，而是把它拆成一个个带类型、带位置的对象，再像搭积木一样组合成一棵树。比如 `keyword("simple") AND NOT projection("mini_hard")` 这句话，最终会变成一棵由 `BoolAnd`、`BoolNot`、`SignalRefExpr` 三种节点组成的树。后面的编译器、校验器、反编译器都只认这棵树，不再碰原始字符串。

本讲会反复出现「raw」和「resolved」两个词：

- **raw 解析树**：解析器按文法规则机械产出的、最贴近文法的中间节点，类型名都以 `raw` 开头（如 `rawRouteDecl`）。
- **resolved AST**：把 raw 节点清洗、去引号、归一化后得到的干净对象（如 `RouteDecl`），才是编译器/校验器真正消费的结构。

## 3. 本讲源码地图

本讲涉及三个核心源码文件：

| 文件 | 作用 |
| --- | --- |
| [parser.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/parser.go) | 词法规则（lexer）、participle 解析器、`Parse` 入口、raw→resolved 转换、错误恢复。本讲的「引擎」。 |
| [ast.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go) | 所有 AST 类型定义：raw 解析树、resolved AST、布尔表达式节点、值类型。本讲的「数据结构清单」。 |
| [config/recipes/balance/recipe.dsl](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl) | 真实可读的 DSL 范例，包含 SIGNAL/PROJECTION/MODEL/PLUGIN/ROUTE 全套写法，是本讲的「语料」。 |

辅助参考：[multi-objective/recipe.dsl](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/multi-objective/recipe.dsl) 是少数用到 `ROUTING` / `ENTRYPOINT` / `RECIPE` 块的配方；[dsl_test.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/dsl_test.go) 里有大量最小语法样例，包括 `TEST` 块。

## 4. 核心概念与源码讲解

### 4.1 模块一：DSL 块语法

#### 4.1.1 概念说明

DSL 文件由若干**顶层块（top-level block）**组成，每个块以一个大写关键字打头，形如 `KEYWORD ... { 字段... }`。解析器把这些块识别成 `rawTopLevel` 联合体里的一种，见 [ast.go:33-45](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L33-L45)：

```go
type rawTopLevel struct {
    Routing      *rawRoutingDecl      `parser:"  @@"`
    Entrypoint   *rawEntrypointDecl   `parser:"| @@"`
    Recipe       *rawRecipeDecl       `parser:"| @@"`
    Signal       *rawSignalDecl       `parser:"| @@"`
    Projection   *rawProjectionDecl   `parser:"| @@"`
    Route        *rawRouteDecl        `parser:"| @@"`
    DecisionTree *rawDecisionTreeDecl `parser:"| @@"`
    Model        *rawModelDecl        `parser:"| @@"`
    Plugin       *rawPluginDecl       `parser:"| @@"`
    TestBlock    *rawTestBlockDecl    `parser:"| @@"`
}
```

这里用的是 Go 的 [participle](https://github.com/alecthomas/participle) 解析库：每个字段上的 `parser:"..."` 标签就是文法规则，`@@` 表示「填充本字段」，`|` 表示「或」（按顺序尝试匹配），`*` 表示「零次或多次」。所以这个联合体告诉解析器：顶层依次尝试 `ROUTING` / `ENTRYPOINT` / `RECIPE` / `SIGNAL` / `PROJECTION` / `ROUTE` / `DECISION_TREE` / `MODEL` / `PLUGIN` / `TEST` 这十种块。

一个术语提醒：**DecisionTree（`DECISION_TREE`）和 `ROUTE` 不能共存**——前者是 `IF/ELSE IF/ELSE` 条件树写法，后者是 `WHEN` 规则 + 优先级写法，二者只能选其一。这是后面 `rawToProgram` 里的一条硬校验。

#### 4.1.2 核心流程

一条 DSL 源码从字符串到 raw 解析树的流水线是：

1. **词法分析（lexing）**：`dslLexer` 用一组正则把字符串切成 token（关键字、标识符、字符串、数字、标点等）。
2. **解析（parsing）**：`rawParser` 按 `raw*` 结构体上的文法标签，把 token 流装配成 `rawProgram`。
3. **转换（resolving）**：`rawToProgram` 遍历 `rawProgram.Entries`，把每个 raw 块翻译成 resolved AST 节点，挂到 `Program` 上。

词法规则定义在 [parser.go:13-31](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/parser.go#L13-L31)：

```go
var dslLexer = lexer.MustSimple([]lexer.SimpleRule{
    {Name: "Comment", Pattern: `#[^\n]*`},
    {Name: "Whitespace", Pattern: `[\s]+`},
    {Name: "Float", Pattern: `[+-]?[0-9]+\.[0-9]+`},
    {Name: "Int", Pattern: `[+-]?[0-9]+`},
    {Name: "String", Pattern: `"(?:[^"\\]|\\.)*"`},
    {Name: "Arrow", Pattern: `->|→`},
    {Name: "Ident", Pattern: `[a-zA-Z_][a-zA-Z0-9_\-\.\/]*`},
    // ... LBrace/RBrace/LParen/RParen/LBracket/RBracket/Colon/Comma/GreaterThan/Equals
})
```

有几条**容易被初学者踩坑的词法细节**，值得记住：

- **`Float` 排在 `Int` 前面**：`0.75` 会被识别成 `Float`，`8080` 才是 `Int`。顺序很重要，因为正则按声明顺序匹配。
- **`Ident` 允许 `.`、`/`、`-`**：所以模型名 `anthropic/claude-opus-4.6` 是**一个**标识符 token，不是多个。这正是 DSL 里能直接写带斜杠和点号的模型名的底层原因。
- **`#` 开头是注释**，整行被词法器丢弃（`Elide`）。
- **箭头 `->` 或 `→`**：仅用于 `TEST` 块里 `"query" -> route_name`。

解析器本体只有几行，见 [parser.go:34-38](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/parser.go#L34-L38)：它装配 lexer、丢弃注释和空白、用 3 个 token 的向前看（`UseLookahead(3)`）来消歧。

#### 4.1.3 各类块的语法（对照真实 recipe）

下面用 `balance/recipe.dsl` 与 `multi-objective/recipe.dsl` 里的真实片段，逐类讲解。注意一个**全局语法约定**：块体内的**字段**用冒号 `:` 分隔键值（`key: value`），而路由头括号 `(description = "...")` 与模型选项 `(reasoning = true)` 用等号 `=`。这两种分隔符服务于不同的文法节点（`FieldEntry` 用 `:`，`RouteOpt`/`ModelOpt` 用 `=`）。

**SIGNAL 块**——声明一个信号（u2-l2 讲过 16 个信号族）。语法：`SIGNAL <族> <名> { 字段 }`，见 [ast.go:96-101](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L96-L101)。真实例子（规则型 keyword 信号，[recipe.dsl:61-64](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L61-L64)）：

```dsl
SIGNAL keyword correction_feedback_markers {
  operator: "OR"
  keywords: ["that's wrong", "this is wrong", "错了", "不对"]
}
```

学习型 embedding 信号则带 `threshold` / `candidates` / `aggregation_method`（[recipe.dsl:141-145](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L141-L145)）。族名决定字段含义，语法壳子一致。

**PROJECTION 块**——投影（u2-l3 详讲）。语法：`PROJECTION <partition|score|mapping> <名> { 字段 }`，`Kind` 由关键字决定，见 [ast.go:104-109](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L104-L109)。三种真实写法各一例：

```dsl
# partition：互斥分区（softmax 选赢家）
PROJECTION partition balance_domain_partition {
  semantics: "softmax_exclusive"
  temperature: 0.1
  members: ["biology", "business", "computer science", "math"]
  default: "other"
}

# score：加权求和（字段是对象数组）
PROJECTION score difficulty_score {
  method: "weighted_sum"
  inputs: [{ type: "keyword", weight: -0.26, name: "simple_request_markers" },
           { type: "complexity", weight: 0.2, name: "general_reasoning:hard" }]
}

# mapping：把 score 切成命名带
PROJECTION mapping difficulty_band {
  source: "difficulty_score"
  method: "threshold_bands"
  calibration: { method: "sigmoid_distance", slope: 10 }
  outputs: [{ name: "balance_simple", lt: 0.18 },
            { name: "balance_reasoning", gte: 0.82 }]
}
```

完整原文见 [recipe.dsl:363-407](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L363-L407)。注意 `score` 的 `inputs` 是**对象数组**、`mapping` 的 `outputs` 也是对象数组——这正好引出下一节「值类型」。

**MODEL 块**——模型货架条目。语法：`MODEL <名> { 字段 }`，见 [ast.go:164-168](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L164-L168)。注意 MODEL 必须是**顶层**声明（即便在 `RECIPE` 内也不行，因为运行时模型目录是全局共享的）。真实例子 [recipe.dsl:441-448](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L441-L448)：

```dsl
MODEL anthropic/claude-opus-4.6 {
  context_window_size: 262144
  capabilities: ["legal_analysis", "policy_review"]
  tags: ["tier:premium", "cost:highest"]
  quality_score: 0.94
  modality: "text"
}
```

**PLUGIN 块**——插件模板。语法：`PLUGIN <名> <类型> { 字段 }`（注意类型是第二个标识符），见 [ast.go:215-220](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L215-L220)。如 `PLUGIN router_replay router_replay {}`（[recipe.dsl:490](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L490)）。带连字符的类型名（如 `semantic-cache`）会被 `normalizePluginName` 归一化成下划线形式（`semantic_cache`），见 [parser.go:449-468](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/parser.go#L449-L468)。

**ROUTE 块**——决策路由（u2-l4、u6-l1 详讲）。这是 DSL 的主角，语法：`ROUTE <名> (可选头选项) { 体 }`，见 [ast.go:112-117](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L112-L117)。体里的元素由 `rawRouteItem` 联合体识别，见 [ast.go:178-189](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L178-L189)：

```go
type rawRouteItem struct {
    Priority     *int                 `parser:"  'PRIORITY' @Int"`
    Tier         *int                 `parser:"| 'TIER' @Int"`
    When         *BoolExprTop         `parser:"| 'WHEN' @@"`   // 布尔表达式，下一模块讲
    Model        *rawModelList        `parser:"| 'MODEL' @@"`
    Algorithm    *rawAlgoSpec         `parser:"| 'ALGORITHM' @@"`
    Plugin       *rawPluginRef        `parser:"| 'PLUGIN' @@"`
    Description  *string              `parser:"| 'DESCRIPTION' @String"`
    CandidateFor *rawCandidateForDecl `parser:"| @@"`
    Emit         *rawEmitDecl         `parser:"| @@"`
}
```

真实例子（[recipe.dsl:496-508](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L496-L508)）展示了头选项用 `=`、MODEL 引用带选项、PLUGIN 内联字段：

```dsl
ROUTE premium_legal (description = "Premium-only route for high-value legal analysis.") {
  PRIORITY 260
  TIER 1
  WHEN (domain("law") OR keyword("legal_risk_markers")) AND embedding("premium_legal_analysis")
  MODEL "anthropic/claude-opus-4.6" (reasoning = true, effort = "high")
  PLUGIN router_replay { enabled: true, max_records: 100000 }
}
```

而最末兜底路由 `casual_chat` 故意**没有 `WHEN`**（[recipe.dsl:678-689](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L678-L689)），保证永不落空——这正是 u2-l4 强调的「每个配方必须有一条无 WHEN 的最终兜底」。

**ROUTING / ENTRYPOINT / RECIPE / TEST 块**——这四类在单配方 `balance` 里用不到，要看 `multi-objective`：

- `ROUTING { strategy: priority }` 声明当前作用域的排序策略，见 [ast.go:50-53](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L50-L53)，实例见 [multi-objective/recipe.dsl:5-7](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/multi-objective/recipe.dsl#L5-L7)。
- `ENTRYPOINT { model_names: [...], recipe: "balanced" }` 把客户端虚拟模型名绑定到某配方，见 [ast.go:56-59](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L56-L59)，实例见 [multi-objective/recipe.dsl:70-93](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/multi-objective/recipe.dsl#L70-L93)。
- `RECIPE <名> (头选项) { ... }` 定义一个隔离的路由作用域，体里可再嵌 `ROUTING`/`SIGNAL`/`PROJECTION`/`ROUTE`/`PLUGIN`/`TEST`，但**不能嵌 MODEL**（目录全局共享），见 [ast.go:63-79](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L63-L79)，实例见 [multi-objective/recipe.dsl:99](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/multi-objective/recipe.dsl#L99)。
- `TEST <名> { "query" -> route_name }` 声明「这条 query 期望命中这条 route」的探针，箭头可用 `->` 或 Unicode `→`，见 [ast.go:82-93](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L82-L93)。recipe 目录里不放 TEST，最小写法来自测试 [dsl_test.go:5278-5282](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/dsl_test.go#L5278-L5282)：

```dsl
TEST routing_intent {
  "what is the derivative of sin(x)" -> math_route
}
```

#### 4.1.4 代码实践（值类型速查）

在动手写策略前，先做一个**最小词法/值类型验证**，搞清楚各种字面量会被识别成什么。值类型的 raw 联合体见 [ast.go:295-310](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L295-L310)，转换逻辑见 [parser.go:576-601](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/parser.go#L576-L601)。

1. **目标**：确认 `:` 字段里不同写法对应的 `Value` 类型。
2. **操作步骤**：阅读下表，对照 `valToValue` 的 switch 顺序（Str→Float→Int→Bool→Array→Object→BareStr）。
3. **观察现象**：

| DSL 写法 | 值类型（resolved） | 说明 |
| --- | --- | --- |
| `operator: "OR"` | `StringValue` | 带引号字符串 |
| `temperature: 0.1` | `FloatValue` | 含小数点，匹配 Float 正则 |
| `max_records: 100000` | `IntValue` | 整数 |
| `enabled: true` | `BoolValue` | `true`/`false` 关键字 |
| `keywords: ["a", "b"]` | `ArrayValue` | 方括号数组 |
| `calibration: { method: "sigmoid_distance" }` | `ObjectValue` | 花括号嵌套对象 |
| `operator: any` | `StringValue` | 裸标识符（BareStr）也当字符串 |

4. **预期结果**：能解释为什么 `0.1` 是 `FloatValue` 而 `100000` 是 `IntValue`（取决于 `.`），以及为什么裸 `any` 不会报错（BareStr 兜底）。
5. **待本地验证**：可用 `go test ./src/semantic-router/pkg/dsl/ -run TestLex -v` 观察 token 类型，确认你的判断。

### 4.2 模块二：AST 节点

#### 4.2.1 概念说明

本模块回答：解析完成后，内存里到底有哪些对象？关键设计是**两层 AST**。

- **raw 层**：`rawProgram` / `rawTopLevel` / `rawSignalDecl` / `rawRouteDecl` …… 这些结构体挂着 participle 文法标签，是文法的直接映射，保留了 lexer 的 `Position`、原始引号、字段数组等机械细节。
- **resolved 层**：`Program` / `SignalDecl` / `RouteDecl` …… 是清洗后的干净对象，去掉了引号、把字段数组转成了 `map[string]Value`、把布尔表达式从三层（`BoolExprTop`/`BoolAndTerm`/`BoolFactor`）折叠成了真正的递归树（`BoolAnd`/`BoolOr`/`BoolNot`）。

**为什么要分两层？** 因为 participle 这类「结构体标签即文法」的解析库，最擅长的是把 token 装进带标签的结构体，但它表达不了「字段名任意、值要归一化、表达式要递归」这类语义。于是项目让 participle 只管机械装配（raw 层），再用一段手写 Go 代码（`rawToProgram` 及一群 `rawToXxx` 函数）做语义清洗（resolved 层）。这样文法规则集中在 raw 结构体上，可读且好改；语义逻辑集中在转换函数里，职责清晰。

resolved 层的根是 `Program`，见 [ast.go:315-327](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L315-L327)：

```go
type Program struct {
    Strategy             string
    Entrypoints          []*EntrypointDecl
    Recipes              []*RecipeDecl
    Signals              []*SignalDecl
    ProjectionPartitions []*ProjectionPartitionDecl
    ProjectionScores     []*ProjectionScoreDecl
    ProjectionMappings   []*ProjectionMappingDecl
    Routes               []*RouteDecl
    Models               []*ModelDecl
    Plugins              []*PluginDecl
    TestBlocks           []*TestBlockDecl
}
```

注意投影被拆成了三个并列切片（`ProjectionPartitions`/`ProjectionScores`/`ProjectionMappings`），而不是一个混合切片——因为三种投影的 resolved 字段差异很大（partition 有 `Members`/`Default`，score 有 `Inputs`，mapping 有 `Outputs`/`Calibration`），分开存放更类型安全。

#### 4.2.2 核心流程

raw → resolved 的总调度在 [parser.go:148-196](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/parser.go#L148-L196) 的 `rawToProgram`。它遍历 `rawProgram.Entries`，用一个 `switch` 把每种 raw 块分发给对应的转换函数，并把结果追加到 `Program` 的相应切片：

```go
for _, entry := range raw.Entries {
    switch {
    case entry.Signal != nil:
        prog.Signals = append(prog.Signals, rawToSignal(entry.Signal))
    case entry.Projection != nil:
        switch entry.Projection.Kind {
        case "partition": prog.ProjectionPartitions = append(...)
        case "score":     prog.ProjectionScores = append(...)
        case "mapping":   prog.ProjectionMappings = append(...)
        }
    case entry.Route != nil:
        prog.Routes = append(prog.Routes, rawToRoute(entry.Route))
    // ... Model / Plugin / Entrypoint / Recipe / Routing / DecisionTree / TestBlock
    }
}
```

末尾还有一条硬校验：`hasDirectRoutes && treeCount > 0` 时报错「`DECISION_TREE` 与 `ROUTE` 不能共存」（[parser.go:192-194](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/parser.go#L192-L194)）。

`Parse` 入口（[parser.go:43-78](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/parser.go#L43-L78)）还有一层**错误恢复**：若整文件解析失败，它会用 `splitTopLevelBlocks` 把源码按顶层关键字切成多个块，逐块独立解析，能成功多少就返回多少，把失败块的错误收集起来。这样一处语法错不会让整个文件的所有诊断信息都丢失。

#### 4.2.3 源码精读：几个典型 resolved 节点

**`SignalDecl`**（[ast.go:420-425](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L420-L425)）：把 `rawSignalDecl` 的字段数组经 `entriesToMap` 转成 `map[string]Value`，调用 `unquoteIdent` 去掉名字上的引号。转换函数见 [parser.go:367-374](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/parser.go#L367-L374)。

**`RouteDecl`**（[ast.go:428-440](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L428-L440)）：这是 resolved 层信息量最大的节点，汇集了优先级、tier、`When BoolExpr`、`Models []*ModelRef`、`Algorithm`、`Plugins`、`CandidateIterations`、`Emits`。它的转换函数 `rawToRoute`（[parser.go:376-416](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/parser.go#L376-L416)）遍历 `rawRouteDecl.Body` 的每个 `rawRouteItem`，用 `switch` 分流到 `route.When`/`route.Models`/`route.Plugins` 等字段——注意它**先**处理头选项里的 `description`，**再**处理体内的 `DESCRIPTION`（体内优先级更高，会覆盖）。

**`ModelRef`**（[ast.go:559-567](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L559-L567)）：路由里 `MODEL "name" (reasoning = true, effort = "high")` 的 resolved 形式。`rawToModelRef`（[parser.go:478-515](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/parser.go#L478-L515)）解析 `reasoning`/`effort`/`lora`/`param_size`/`weight` 五个选项，其中 `reasoning` 是 `*bool`（指针，区分「未设」与「显式 false」）。

#### 4.2.4 代码实践（跟踪一个 SIGNAL 的解析产物）

1. **目标**：用一段最小 DSL，确认 `SIGNAL` 块解析后落入 `Program.Signals`，并验证字段被正确转成 `map[string]Value`。
2. **操作步骤**：在 `src/semantic-router/pkg/dsl/` 下写一个临时测试（或直接读现有 `TestParseTestBlock` 的风格），输入：

   ```dsl
   SIGNAL keyword demo { operator: "OR", keywords: ["hi", "你好"] }
   ```
3. **调用** `Parse(input)`，断言 `len(prog.Signals) == 1`、`prog.Signals[0].SignalType == "keyword"`、`prog.Signals[0].Name == "demo"`、`prog.Signals[0].Fields["operator"]` 是 `StringValue{"OR"}`、`prog.Signals[0].Fields["keywords"]` 是长度为 2 的 `ArrayValue`。
4. **预期结果**：所有断言通过；注意 `operator` 与 `keywords` 之间的逗号被词法器吞掉（`FieldEntry` 的文法允许 `,?`）。
5. **待本地验证**：若不想写测试，可直接 `go test ./src/semantic-router/pkg/dsl/ -run TestParseTestBlock -v` 观察现成用例的解析行为。

### 4.3 模块三：布尔表达式（WHEN）

#### 4.3.1 概念说明

`ROUTE` 的 `WHEN` 是一棵布尔表达式树，叶子是信号/投影引用，内部节点是 `AND`/`OR`/`NOT`。本模块是 u6-l1（决策引擎求值）的前置——决策引擎递归求值的 `RuleNode` 树，正是从这棵 DSL 布尔树编译来的。

participle 用**三层结构**来表达优先级（经典的「运算符优先级用嵌套层级实现」），见 [ast.go:258-283](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L258-L283)：

```go
// 顶层 = OR 序列：A OR B OR C
type BoolExprTop  struct { Terms []*BoolAndTerm   `parser:"@@ ( 'OR' @@ )*"` }
// 每个 term = AND 序列：X AND Y AND Z
type BoolAndTerm  struct { Factors []*BoolFactor  `parser:"@@ ( 'AND' @@ )*"` }
// 每个 factor = NOT / 括号 / 叶子
type BoolFactor   struct {
    Not       *BoolFactor       `parser:"  'NOT' @@"`
    Paren     *BoolExprTop      `parser:"| '(' @@ ')'"`
    SignalRef *rawSignalRefExpr `parser:"| @@"`
}
```

由此读出优先级（从低到高）：

\[
\text{OR} \;<\; \text{AND} \;<\; \text{NOT} \;<\; \text{括号 / 叶子}
\]

即 `NOT` 结合最紧，`AND` 次之，`OR` 最松。所以 `a OR b AND c` 解析为 `a OR (b AND c)`，与常见编程语言一致。

叶子节点 `rawSignalRefExpr`（[ast.go:278-283](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L278-L283)）长这样：

```go
type rawSignalRefExpr struct {
    SignalType string        `parser:"@Ident"`
    SignalName string        `parser:"'(' @(String | Ident)"`
    Fields     []*FieldEntry `parser:"@@* ')'"`
}
```

这正是 DSL 里 `keyword("simple")`、`projection("mini_hard")`、`complexity("legal_risk:hard")` 这类调用的文法：`族名("信号名", 可选字段)`。

#### 4.3.2 核心流程：三层 raw → 递归 resolved 树

三层 raw 结构是 participle 文法的产物，但用起来很别扭（要判断 `Terms`/`Factors` 长度）。于是 `toBoolExpr`/`toAndExpr`/`toFactorExpr` 三个函数（[parser.go:519-562](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/parser.go#L519-L562)）把它折叠成真正的递归接口树：

```go
type BoolExpr interface {
    boolExpr()
    GetPos() Position
}
type BoolAnd struct{ Left, Right BoolExpr; Pos Position }   // AND
type BoolOr  struct{ Left, Right BoolExpr; Pos Position }   // OR
type BoolNot struct{ Expr BoolExpr; Pos Position }          // NOT
type SignalRefExpr struct{ SignalType, SignalName string; Fields map[string]Value; Pos Position } // 叶子
```

接口定义与实现见 [ast.go:511-554](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L511-L554)。转换的关键逻辑：

```go
func toBoolExpr(top *BoolExprTop) BoolExpr {
    result := toAndExpr(top.Terms[0])              // 第一个 term
    for i := 1; i < len(top.Terms); i++ {          // 其余 term 用 OR 串起
        result = &BoolOr{Left: result, Right: toAndExpr(top.Terms[i]), ...}
    }
    return result
}
// toAndExpr 同理，用 AND 串起 Factors；toFactorExpr 处理 NOT/括号/叶子
```

注意它是**左结合**的：`a AND b AND c` 会被建成 `BoolAnd{BoolAnd{a, b}, c}`（左嵌套）。`BoolNot` 的转换有一点特别——`NOT` 的操作数仍是一个 `BoolFactor`（[parser.go:549-550](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/parser.go#L549-L550)），因此 `NOT NOT a`（双重否定）在文法上是合法的。

#### 4.3.3 源码精读：用真实 WHEN 走一遍

以 `formal_math_proof` 路由的 `WHEN` 为例（[recipe.dsl:513](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L513)，已简化）：

```dsl
WHEN domain("math") AND keyword("reasoning_request_markers")
     AND NOT (projection("verification_required") OR user_feedback("wrong_answer"))
```

解析与折叠过程：

1. 词法切成 `domain ( "math" ) AND keyword ( ... ) AND NOT ( projection ( ... ) OR user_feedback ( ... ) )`。
2. 顶层 `BoolExprTop` 只有 **1 个 term**（没有顶层 `OR`，因为那个 `OR` 在括号里）。
3. 这个 term（`BoolAndTerm`）有 **3 个 factor**：`domain("math")`、`keyword(...)`、`NOT (...)`。
4. `toAndExpr` 把前两个 factor 用 `BoolAnd` 串起，再与第三个 factor 串起，得到左嵌套 `BoolAnd{BoolAnd{domain, keyword}, NOT(...)}`。
5. 第三个 factor 命中 `BoolFactor.Not`，递归 `toFactorExpr` 处理其操作数；操作数是 `Paren`（括号），于是回到 `toBoolExpr` 处理括号内的 `projection(...) OR user_feedback(...)`，得到 `BoolOr{projection, user_feedback}`。
6. 最终 resolved 树：

```
BoolAnd
├─ Left: BoolAnd
│        ├─ Left:  SignalRefExpr{domain, math}
│        └─ Right: SignalRefExpr{keyword, reasoning_request_markers}
└─ Right: BoolNot
          └─ Expr: BoolOr
                   ├─ Left:  SignalRefExpr{projection, verification_required}
                   └─ Right: SignalRefExpr{user_feedback, wrong_answer}
```

对照 [parser.go:519-562](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/parser.go#L519-L562)，你能逐行对应上每个节点的诞生位置。

#### 4.3.4 代码实践（本讲主实践：手写最小策略并拆解 AST）

1. **目标**：手写一份包含 1 个 SIGNAL、1 组 PROJECTION、1 个含 `AND`/`NOT` 的 ROUTE 的最小策略，并对照 `ast.go` 说明它会解析成哪些节点。
2. **操作步骤**：新建文件 `/tmp/mini.dsl`（示例代码，非项目原有文件）：

   ```dsl
   # 示例代码：最小策略 mini
   SIGNAL keyword simple {
     keywords: ["tl;dr", "briefly explain"]
   }
   SIGNAL complexity general {
     threshold: 0.5
   }

   # 一个 score 投影 + 一个 mapping 投影（mapping 需要 source，故成对出现）
   PROJECTION score mini_difficulty {
     method: "weighted_sum"
     inputs: [
       { type: "keyword",    name: "simple",  weight: -0.6, value_source: "confidence" },
       { type: "complexity", name: "general", weight:  0.6, value_source: "confidence" }
     ]
   }
   PROJECTION mapping mini_band {
     source: "mini_difficulty"
     method: "threshold_bands"
     calibration: { method: "sigmoid_distance", slope: 10 }
     outputs: [{ name: "mini_easy", lt: 0.4 }, { name: "mini_hard", gte: 0.4 }]
   }

   ROUTE easy_lane {
     PRIORITY 100
     WHEN keyword("simple") AND NOT projection("mini_hard")
     MODEL "qwen/qwen3.5-rocm"
   }

   ROUTE fallback {
     PRIORITY 10
     MODEL "qwen/qwen3.5-rocm"
   }
   ```

3. **运行校验**（需要本地 Go 环境）：

   ```bash
   cd src/semantic-router
   go run ./cmd/dsl validate /tmp/mini.dsl
   ```

4. **AST 拆解**（这是本实践的核心，对照 4.3.3 的方法）：
   - `Parse` 返回的 `Program`：`Signals` 有 2 个（`simple`、`general`）；`ProjectionScores` 有 1 个（`mini_difficulty`）；`ProjectionMappings` 有 1 个（`mini_band`）；`Routes` 有 2 个（`easy_lane`、`fallback`）。
   - `easy_lane.When` 的 resolved 树为：

     ```
     BoolAnd
     ├─ Left:  SignalRefExpr{SignalType:"keyword",    SignalName:"simple"}
     └─ Right: BoolNot
               └─ Expr: SignalRefExpr{SignalType:"projection", SignalName:"mini_hard"}
     ```
     因为 `WHEN` 只有一个 term（无顶层 OR）、两个 factor 用 `AND` 连、第二个 factor 是 `NOT` 包住一个叶子。
   - `fallback` 没有 `WHEN`，故 `When` 为 `nil`——它是兜底路由。
5. **预期结果**：`validate` 通过（或只报「未引用信号」之类的轻微告警，取决于校验器版本）；你画出的 AST 树与上述结构一致。
6. **待本地验证**：若手头没有 `qwen/qwen3.5-rocm` 的模型目录，校验器可能在**语义校验**阶段抱怨模型未定义——这属于 u3-l3 的校验范畴，不影响你对**语法/AST**的理解。若只想验证语法层，可改用 `go run ./cmd/dsl fmt /tmp/mini.dsl`，它会重新格式化输出，能成功格式化即说明语法正确。

#### 4.3.5 小练习与答案

**练习 1**：把 `WHEN a OR b AND c` 写成带括号的形式，使优先级显式化。

> **答案**：`WHEN a OR (b AND c)`。因为 `AND` 优先级高于 `OR`，原式等价于先算 `b AND c` 再与 `a` 做 `OR`。

**练习 2**：表达式 `NOT a AND b` 在三层 raw 结构里，`BoolAndTerm` 有几个 factor？分别是什么？

> **答案**：2 个 factor。第一个是 `BoolFactor{Not: BoolFactor{SignalRef: a}}`（`NOT` 包住叶子 `a`），第二个是 `BoolFactor{SignalRef: b}`。`toAndExpr` 把它们建成 `BoolAnd{BoolNot{a}, b}`。

**练习 3**：为什么 resolved 层要把布尔表达式从三层（Top/AndTerm/Factor）折叠成递归接口（BoolAnd/BoolOr/BoolNot）？

> **答案**：三层结构是 participle 文法的副产物，遍历时要判长度、区分节点类型，使用繁琐且易错；折叠成递归 `BoolExpr` 接口后，编译器、校验器、反编译器可以用统一的「访问者」模式递归处理任意深度的表达式，也更容易在 u6-l1 的决策引擎里映射成 `RuleNode` 求值树。

## 5. 综合实践

把本讲三个模块串起来，做一个**「读懂 balance 最复杂的一条 WHEN」**的练习。

1. 打开 [recipe.dsl:524-527](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L524-L527)（`reasoning_deep` 路由的 `WHEN`）。
2. 用本讲学到的优先级规则（`NOT` > `AND` > `OR`，括号最高），手工为这条超长 `WHEN` 加上全套括号，使其逻辑显式化。
3. 画出它的 resolved 布尔树（顶层一定是 `BoolAnd`，右子树是一个长长的 `BoolNot`，其内部是一个由 `OR` 串起的列表）。
4. 用 `go run ./cmd/dsl fmt config/recipes/balance/recipe.dsl` 让工具自动重排格式，对比你手工加的括号与工具输出是否一致（工具可能省略它认为多余的括号，但语义应相同）。
5. 挑出树里的每个 `SignalRefExpr` 叶子，按 `族:名` 列一张表，并回头到 SIGNAL 段（[recipe.dsl:1-261](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L1-L261)）确认每个叶子引用的信号都确实被声明过——这其实就是校验器 `validateDecisionSignalReferences` 在做的事（u3-l3）。

完成这个练习，你就把「读 DSL → 建 AST → 关联信号声明」整条链路走通了。

## 6. 本讲小结

- DSL 是 config.yaml 的「人友好版」，由十类顶层块组成：`ROUTING` / `ENTRYPOINT` / `RECIPE` / `SIGNAL` / `PROJECTION` / `ROUTE` / `DECISION_TREE` / `MODEL` / `PLUGIN` / `TEST`。
- 词法细节决定写法：`Float` 先于 `Int` 匹配、`Ident` 允许 `.`/`/`/`-`（故 `anthropic/claude-opus-4.6` 是单个标识符）、字段用 `:` 而路由头/模型选项用 `=`。
- AST 分两层：raw 解析树（participle 文法的直接映射）与 resolved AST（`Program` 及各类 `*Decl`，供编译器/校验器消费），由 `rawToProgram` 及 `rawToXxx` 桥接。
- 布尔表达式优先级为 `NOT` > `AND` > `OR`，括号最高；participle 用三层 raw 结构表达优先级，再由 `toBoolExpr` 折叠成递归的 `BoolAnd`/`BoolOr`/`BoolNot`/`SignalRefExpr` 树，左结合。
- `Parse` 带错误恢复：整文件失败时按顶层关键字切块逐个解析，最大化保留诊断信息；`DECISION_TREE` 与 `ROUTE` 不可共存。

## 7. 下一步学习建议

- 继续本单元：**u7-l2（DSL 编译与反编译）** 讲这棵 `Program` AST 如何被 `compiler.go` 编译成运行时 `RouterConfig`，以及 `decompiler.go`/`emitter_yaml.go` 如何把配置反编译回 DSL/YAML，实现双向往返。
- 工具视角：**u7-l3（dsl 命令行工具）** 讲 `cmd/dsl` 的 `validate`/`compile`/`generate`/`test` 子命令，是本讲实践的命令行载体。
- 求值纵深：想看这棵布尔树在运行时如何被递归求值并产出匹配置信度，去 **u6-l1（决策引擎）**；想看信号如何被分类器填进 `SignalMatches`，去 **u8（分类信号系统）**。
- 建议继续阅读的源码：[compiler_routes.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler_routes.go)（看 `RouteDecl` 如何变成 `Decision`）与 [validator.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/validator.go)（看 AST 如何被静态校验）。
