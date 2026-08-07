# DSL 编译与反编译（YAML 往返）

> 本讲承接 u7-l1（DSL 语法与 AST）。u7-l1 解决的是「一段 DSL 字符串如何被解析成 AST 树」；本讲解决的是「这棵 AST 树如何被翻译成运行时配置 `RouterConfig`，以及反过来如何把配置再变回 DSL / YAML」。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `Compile` / `CompileAST` 这条「AST → `RouterConfig`」总线的入口、步骤顺序与错误模型；
- 在源码层面解释 ROUTE 如何变成 `Decision`、WHEN 布尔表达式如何变成 `RuleNode` 递归树（包括编译期的同级 AND/OR 「拍平」优化）；
- 说清楚 SIGNAL、PROJECTION、MODEL、PLUGIN 各自被哪段代码逐块下沉到配置；
- 理解「反编译」方向：`Decompile` / `DecompileRouting` 如何把配置写回 DSL 文本，`EmitYAML` / `EmitUserYAML` 如何把配置写回 YAML；
- 建立「YAML↔DSL 往返（round-trip）」的精确契约：哪些东西保证可逆、哪些是单向有损（lossy），并能在真实测试里验证它。

## 2. 前置知识

本讲默认你已经掌握 u7-l1 的内容，尤其是：

- DSL 的顶层块：`SIGNAL` / `PROJECTION`（partition / score / mapping）/ `MODEL` / `PLUGIN` / `ROUTE` / `RECIPE` / `ENTRYPOINT`；
- AST 分两层：participle 直接映射的 raw 层，和清洗后的 resolved 层（`Program` 及各种 `*Decl`），**编译器只消费 resolved 层**；
- WHEN 是一棵布尔表达式树：`BoolAnd` / `BoolOr` / `BoolNot` / `SignalRefExpr`，优先级 NOT > AND > OR。

此外，你还需要对运行时配置 `config.RouterConfig` 有一个最浅的印象：它是 Go 端反序列化 `config.yaml` 后的强类型结构，承载 signals / projections / decisions / strategy / providers 等。运行时只认 `RouterConfig`，不认 DSL 文本——所以 DSL 必须先「编译」成它。

一个贯穿全讲的关键事实：`config.RuleCombination` 其实就是 `config.RuleNode` 的**类型别名**（`type RuleCombination = RuleNode`），定义在 [decision_config.go:184](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/decision_config.go#L184)。也就是说，一条决策的 `Rules` 字段就是**一棵 `RuleNode` 递归树**：叶子节点填 `Type`+`Name`（指向某个 `族:名` 信号），分支节点填 `Operator`+`Conditions`（`AND`/`OR`/`NOT`）。记住这一点，编译与反编译的对称性就一目了然了。

## 3. 本讲源码地图

本讲全部聚焦于 `src/semantic-router/pkg/dsl/` 与一个命令行入口：

| 文件 | 作用 |
| --- | --- |
| [compiler.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler.go) | 编译器主体：`Compile`/`CompileAST` 总线、`compile()` 步骤编排、投影编译入口。 |
| [compiler_routes.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler_routes.go) | ROUTE → `Decision` 的逐字段编译、布尔表达式 → `RuleNode` 的递归翻译、同级 AND/OR 拍平。 |
| [compiler_signals.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler_signals.go) | 按信号族分派的 SIGNAL 编译（keyword/embedding/domain/complexity/jailbreak/pii…）。 |
| [compiler_scopes.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler_scopes.go) | RECIPE / ENTRYPOINT 作用域编译：每条配方拿到一个全新 Compiler 实例，从结构上杜绝跨配方符号串用。 |
| [decompiler.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/decompiler.go) | 反编译主体：`Decompile` 入口、`Format`（解析→编译→反编译的三段式）、插件模板抽取。 |
| [decompiler_scopes.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/decompiler_scopes.go) | `DecompileConfig`：先输出共享路由目录，再输出 entrypoint 与各 recipe 隔离体。 |
| [routing_contract.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/routing_contract.go) | `DecompileRouting`（配置→DSL 文本）、`EmitRoutingYAMLFromConfig`（配置→路由片段 YAML）。 |
| [decompiler_decisions.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/decompiler_decisions.go) | `Decision` → ROUTE 的反编译：`RuleNode` 重新拍平回 DSL 表达式、MODEL 省略优化。 |
| [emitter_yaml.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/emitter_yaml.go) | 配置→YAML 的多种出口：`EmitYAML`（扁平）、`EmitUserYAML`（用户友好嵌套）、`EmitCRD`、`EmitHelm`。 |
| [cli.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/cli.go) | `sr-dsl` 各子命令的实现：`CLICompile` / `CLIDecompile` / `CLIValidate` / `CLIFormat`。 |
| [cmd/dsl/main.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/dsl/main.go) | `sr-dsl` 命令分发入口。 |

可对照阅读的真实产物：`config/recipes/balance/recipe.dsl`（DSL 源）与 `config/recipes/balance/config.yaml`（等价配置）。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：**① AST → 配置的编译总线**、**② 逐块编译（信号/投影/ROUTE→决策/作用域）**、**③ 反编译与 YAML 往返**。

### 4.1 AST→Config 编译：总线、默认值种子与错误模型

#### 4.1.1 概念说明

「编译」在这里是一个**纯函数式**的翻译过程：输入是 resolved AST `*Program`，输出是 `*config.RouterConfig` 加一组错误。它不做 I/O、不读磁盘、不连网络，只把内存里的树翻成另一种内存里的结构。

之所以需要这层翻译，是因为运行时（分类、决策引擎、选择算法）只认强类型的 `RouterConfig`，而人写的是更紧凑的 DSL。编译器就是这个「人友好 → 机器友好」的单向阀门。

两个设计要点先记住：

1. **默认值种子**：编译器不从零开始构建配置，而是先种一份 `config.DefaultGlobalConfig()` 作为底，再在它上面叠加 DSL 声明的值。这保证了「DSL 没写的东西」仍有一套安全默认。
2. **错误是累积而非抛出**：编译器把所有诊断收进 `c.errors []error`，跑完一遍再一次性返回，这样一次能报多个错，而不是在第一个错处停下。

#### 4.1.2 核心流程

`Compile` 是给字符串用的语法糖，真正干活的是 `CompileAST`：

```
Compile(input string)
  └─ Parse(input)              // u7-l1 讲过：字符串 → resolved *Program
  └─ CompileAST(prog)
        ├─ 种默认值 DefaultGlobalConfig()
        ├─ 设置 Strategy
        ├─ compile()            // 编译「默认配方」的主体（信号/投影/模型/路由）
        ├─ compileScopes()      // 编译 RECIPE / ENTRYPOINT 作用域
        └─ 若 c.errors 非空 → 返回错误；否则返回 config
```

其中 `compile()` 把默认配方的内容按固定顺序逐块下沉，顺序很关键（见 4.2）。

#### 4.1.3 源码精读

入口与总线在 [compiler.go:19-45](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler.go#L19-L45)：`Compile` 先解析再委托 `CompileAST`；`CompileAST` 用 `DefaultGlobalConfig()` 种底、置 `Strategy`、依次调用 `compile()` 与 `compileScopes()`，最后按 `c.errors` 是否为空决定返回配置还是错误。

`compile()` 的步骤编排见 [compiler.go:47-68](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler.go#L47-L68)，顺序是固定的六步：

1. 注册 PLUGIN 模板（供 ROUTE 里的 `PLUGIN <name>` 引用按名查表）；
2. `compileSignals()`；
3. `compileProjectionPartitions()`；
4. `compileProjectionScores()` + `compileProjectionMappings()`；
5. `compileModels()`（顶层模型目录）；
6. `compileRoutes()`（ROUTE → decisions）。

`Compiler` 结构本身见 [compiler.go:10-15](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler.go#L10-L15)：它持有 `prog`、正在构建的 `config`、`pluginTemplates` 与累积的 `errors`。错误统一经 `addError` 落盘，见 [compiler.go:133-136](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler.go#L133-L136)，每条错误都带上 `Position`（行/列），方便定位。

#### 4.1.4 代码实践

**实践目标**：用最少代码亲手跑一次「字符串 → 配置」，观察默认值种子与错误累积。

**操作步骤**（在 `src/semantic-router/` 目录下，因为 `go.mod` 在此）：

1. 新建一个临时测试文件 `pkg/dsl/zz_compile_demo_test.go`（示例代码，做完可删）：

   ```go
   package dsl

   import (
       "fmt"
       "testing"
   )

   func TestCompileDemo(t *testing.T) {
       src := `SIGNAL keyword hi { operator: "OR", keywords: ["hello"] }
   ROUTE lane { PRIORITY 5
     WHEN keyword("hi")
     MODEL "qwen2.5:3b"
   }
   ROUTE fallback { PRIORITY 1
     MODEL "qwen2.5:3b"
   }`
       cfg, errs := Compile(src)
       fmt.Println("errors:", len(errs))
       for _, d := range cfg.Decisions {
           fmt.Printf("decision %q priority=%d rules-operator=%q\n", d.Name, d.Priority, d.Rules.Operator)
       }
   }
   ```

2. 运行：`go test ./pkg/dsl/ -run TestCompileDemo -v`。

**需要观察的现象**：即便源码里完全没写 strategy、providers、global，输出仍是一个合法的 `RouterConfig`（因为种了默认值）；两条 ROUTE 各自变成一条 `Decision`，`lane` 的 `Rules.Operator` 是 `AND`，`fallback` 的 `Rules` 为空结构（无 WHEN 的兜底）。

**预期结果**：`errors: 0`，打印出 `decision "lane"` 与 `decision "fallback"` 两行。若你故意把 `keyword("hi")` 改成 `bogus("hi")`，会看到错误数 > 0，但程序不会 panic——错误被累积返回。

> 待本地验证：精确的打印格式以你本地实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CompileAST` 要先调用 `compile()` 再调用 `compileScopes()`，而不能反过来？

<details><summary>参考答案</summary>

因为配方作用域（`compileScopes`）会把「默认配方的 signals/projections/decisions」打包成第一条名为 `DefaultRecipeName` 的 `RoutingRecipe`（见 4.2.4），它依赖 `compile()` 已经填好的 `c.config.Signals/Projections/Decisions`。反过来调用时这些字段还是空的，默认配方就会是空壳。
</details>

**练习 2**：编译期错误为什么采用「累积」而不是遇到第一个就 return？

<details><summary>参考答案</summary>

为了让作者一次看到全部问题、少跑几轮「改一处再编译」的循环。代价是编译器要保证「报错后继续往下走」是安全的——所以各 `compileXxx` 都对 nil/空输入做了防御，不会因为前面某个声明非法而空指针崩溃。
</details>

---

### 4.2 逐块编译：信号、投影、ROUTE→决策、作用域

#### 4.2.1 概念说明

总线确定后，真正「搬砖」的是各 `compileXxx` 方法。它们的特点是**一一对应、彼此独立**：

- 每种 DSL 块对应一个（或一组）编译函数；
- 每个函数只把自己的 AST 节点翻成对应的 config 字段，几乎不做跨块推断；
- 唯一的「跨块」语义集中在两处：PLUGIN 模板的「按名查表」、以及 ROUTE → Decision 里 WHEN 树的递归翻译。

这意味着你可以按任意顺序阅读这些函数，彼此耦合很低。

#### 4.2.2 核心流程

**信号编译**走「按族分派表」：`signalCompilerByType[族名]` 是一张 `map[string]func`，`compileSignals` 遍历每条 SIGNAL，按 `SignalType` 查表调用对应编译器（keyword 走 `compileKeywordSignal`、domain 走 `compileDomainSignal`……），未知族名直接报错。各编译器把 DSL 的 `Fields map[string]Value` 一个个读出来，塞进对应的 `config.XxxRule`。

**投影编译**分三段：partition / score / mapping，分别填 `config.Projections` 的三个切片，几乎是字段对字段的搬运（见 4.1.3 的链接）。

**ROUTE → Decision** 是本模块重点，流程如下：

```
compileRoute(r *RouteDecl) config.Decision
  ├─ 装 Name / Description / Priority / Tier
  ├─ compileRouteRules(r)          // WHEN → RuleCombination(RuleNode 树)
  ├─ 遍历 r.Models → appendModelRef → Decision.ModelRefs
  ├─ 遍历 r.CandidateIterations → 编译 + 可能补 ModelRef
  ├─ 若有 r.Algorithm → compileAlgorithm
  ├─ 遍历 r.Plugins → compilePluginRef（按名查模板）→ Decision.Plugins
  └─ compileRouteEmits(r)          // EMIT retention → Decision.Emits
```

WHEN 的翻译由 `compileBoolExpr` 递归完成，关键点是**同级 AND/OR 的拍平（flatten）**。

**作用域编译**：每条 RECIPE 用 `newScopedCompiler` 建一个全新 `Compiler` 实例编译其 body，再把结果作为一条独立 `RoutingRecipe` 挂到配置上；ENTRYPOINT 只做引用校验。

#### 4.2.3 源码精读

**ROUTE → Decision 的主函数** [compiler_routes.go:15-50](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler_routes.go#L15-L50) 严格按上图顺序装填。其中模型引用的搬运在 `appendModelRef` [compiler_routes.go:84-110](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler_routes.go#L84-L110)：注意 `reasoning`/`effort` 是模型级选项，而 `param_size` 这种元数据被额外写进 `c.config.ModelConfig[model]`——这是 ROUTE 内联模型元数据的落点。

**WHEN → 规则树**是本讲最值得精读的部分。入口 `compileRouteRules` [compiler_routes.go:52-64](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler_routes.go#L52-L64) 做两件小事：

- 若 ROUTE 没有 WHEN（兜底路由），返回一个 `Operator:"AND"` 且 `Conditions` 为空的 `RuleCombination`——这是「无条件命中」的规范表示；
- 若 WHEN 只是一个裸叶子（如单独一条 `keyword("hi")`），把它包成一个单元素 AND。

递归核心是 `compileBoolExpr` [compiler_routes.go:155-196](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler_routes.go#L155-L196)，按 AST 节点类型分发：

- `*BoolAnd` → `RuleCombination{Operator:"AND", Conditions:[...]}`；
- `*BoolOr` → `RuleCombination{Operator:"OR", Conditions:[...]}`；
- `*BoolNot` → `RuleCombination{Operator:"NOT", Conditions:[单个子树]}`；
- `*SignalRefExpr` → 叶子 `RuleCombination{Type, Name, Label, Predicate, OnError}`。

**拍平优化**：u7-l1 讲过，解析器把 `a AND b AND c` 产生成左结合的二叉 `BoolAnd` 树 `(a AND b) AND c`。如果原样翻译会得到层层嵌套的单子 AND，既难看又让决策引擎多递归。所以编译器对**同级**的 AND/OR 做拍平：

\[
(a \wedge b) \wedge c \;\Longleftrightarrow\; \text{AND}(a, b, c)
\]

实现是 `flattenBoolExpr` [compiler_routes.go:227-240](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler_routes.go#L227-L240)：遇到同类型节点就递归把左右子树拆开、摊平进一个切片，遇到异类型节点则停止、作为一个独立子树保留。因此 `a AND (b OR c)` 会变成 `AND(a, OR(b,c))`——OR 仍嵌套在 AND 内，因为运算优先级要求如此。

**叶子翻译** `compileSignalRefExpr` [compiler_routes.go:198-225](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler_routes.go#L198-L225)：把 DSL 里的 `classifier("x", predicate: {op:">", value:0.7})` 这种带数值断言的叶子，经 `yaml.Marshal`/`Unmarshal` 往返成强类型 `config.NumericPredicate`。这是 DSL 动态字段 → 强类型结构的小技巧：先转成 `map`，再让 yaml 库做类型转换。

**信号分派** [compiler_signals.go:11-20](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler_signals.go#L11-L20)：`signalCompilerByType` 是注册表，未知族名直接 `addError`。以 keyword 为例，`compileKeywordSignal` [compiler_signals.go:22-52](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler_signals.go#L22-L52) 把 `operator`/`keywords`/`case_sensitive`/`method`/模糊匹配参数等逐个读出，append 到 `c.config.KeywordRules`。结构类信号（structure/conversation/classifier）则用「先 `fieldsToMap` 再 `yaml.Marshal/Unmarshal`」的通用模式（见 `compileStructureSignal`），因为这些规则字段较多、且契约由 `config.ValidateXxxRuleContract` 把关。

**作用域编译** [compiler_scopes.go:13-65](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler_scopes.go#L13-L65)：`compileRecipes` 先把默认配方的 signals/projections/decisions 打包成第一条 `RoutingRecipe{Name: DefaultRecipeName}`；随后对 DSL 里每条 RECIPE，用 `newScopedCompiler(recipe.Program)`（[compiler_scopes.go:93-106](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler_scopes.go#L93-L106)）建一个全新编译器实例编译其 body。**关键设计**：因为是全新实例，配方 A 里声明的信号名绝不可能被配方 B 的决策引用——跨配方符号串用在结构层面就被杜绝了（其运行期对应语义见 u3-l1/u3-l2 的「recipe 隔离」）。ENTRYPOINT 的编译只做引用校验：指向的 recipe 必须存在、`model_names` 不能重复（[compiler_scopes.go:67-91](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/compiler_scopes.go#L67-L91)）。

#### 4.2.4 代码实践

**实践目标**：用 balance 配方里一条真实 ROUTE，手工追一遍「WHEN 字符串 → RuleNode 树」。

**操作步骤**：

1. 打开 [config/recipes/balance/recipe.dsl:650-662](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L650-L662)（`ROUTE fast_qa`）。它的 WHEN 极长，结构可抽象为：

   ```
   WHEN (A AND B OR C AND D OR E) AND (F OR G OR NOT H) AND I AND (J OR K)
   ```

2. 只挑最外层的骨架（两个顶层 `AND`），画出编译后的形状：根是 `Operator:"AND"`，`Conditions` 是 4 个子树（对应 4 个被 `AND` 连接的顶层因子）。

3. 再看 [config/recipes/balance/recipe.dsl:678-689](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L678-L689)（`ROUTE casual_chat`）：它**没有 WHEN**，是最终兜底。据 `compileRouteRules`，它的 `Decision.Rules` 应为 `{Operator:"AND", Conditions:[]}`。

**需要观察的现象**：

- `fast_qa` 的 WHEN 顶层有 4 个 AND 因子 → `Decision.Rules.Conditions` 长度 ≈ 4；
- 同级 OR（如 `domain("health") OR domain("law") OR ...`）被拍平成单个 `Operator:"OR"` 节点，其 `Conditions` 是一串叶子；
- `casual_chat` 的 `Rules.Conditions` 为空。

**预期结果**：你画出的树根节点 Operator 为 `AND`，分支里穿插若干 `OR`/`NOT` 节点；叶子节点形如 `{Type:"projection", Name:"balance_simple"}`。这与运行时决策引擎（u6-l1）消费的 `SignalMatches`/`RuleNode` 完全对齐。

> 待本地验证：用 4.1.4 的临时测试打印 `cfg.Decisions[i].Rules` 的 JSON 即可看到真实结构。

#### 4.2.5 小练习与答案

**练习 1**：DSL 写 `a AND b AND c`，编译后 `Decision.Rules` 是嵌套三层 AND，还是一层 AND 三个叶子？为什么？

<details><summary>参考答案</summary>

一层 AND 三个叶子。因为 `flattenBoolExpr` 对同级 AND 做了拍平：\((a \wedge b) \wedge c \Rightarrow \text{AND}(a,b,c)\)。这样决策引擎（u6-l1 的 `evalNode`）只需递归一层即可聚合三个子项的置信度（AND 取平均）。
</details>

**练习 2**：`compileSignalRefExpr` 里为什么对 `predicate` 字段要先 `yaml.Marshal` 再 `yaml.Unmarshal`？

<details><summary>参考答案</summary>

因为 DSL 的字段值是弱类型的 `Value`（`ObjectValue`/`ArrayValue`…），而 config 需要强类型的 `*config.NumericPredicate`。先用 `fieldsToMap`+`Marshal` 把 `Value` 转成 YAML 字节流，再 `Unmarshal` 进强类型结构，借 yaml 库完成类型转换与校验。这是一个「动态字段 → 强类型」的通用桥接手法。
</details>

**练习 3**：为什么每条 RECIPE 要用 `newScopedCompiler` 建全新实例，而不是复用主编译器？

<details><summary>参考答案</summary>

为了让「跨配方符号误引用」从结构上不可能。若复用同一实例，配方 A 声明的信号会留在 `c.config.Signals` 里，配方 B 的决策就能引用到它，违背 u3-l1 讲过的「信号/投影/决策名字局部于配方」契约。全新实例 = 全新的 `config.Signals/Projections/Decisions`，天然隔离。
</details>

---

### 4.3 反编译与 YAML 往返：配置 → DSL / YAML

#### 4.3.1 概念说明

「反编译」是编译的逆方向：输入 `*config.RouterConfig`，输出 DSL 文本（`Decompile`）或 YAML 字节（`EmitYAML` 等）。它存在的理由有三：

1. **可评审性**：把 YAML 翻回 DSL 让人审阅；
2. **格式化**：`Format` = 解析 → 编译 → 反编译，用规范 DSL 格式重排源文件；
3. **多形态产出**：同一份路由逻辑可输出成完整 `config.yaml`、路由片段 YAML、CRD、Helm values 等不同部署形态。

但**反编译不是编译的严格逆函数**，它是有损的（lossy）。理解「哪些可逆、哪些不可逆」是本模块的核心。

#### 4.3.2 核心流程

```
Decompile(cfg)                       // 配置 → DSL 文本
  └─ DecompileConfig(cfg)
        ├─ DecompileRouting(cfg)     // 共享路由目录（strategy/signals/models/plugins/routes）
        │     ├─ decompileRoutingStrategy()
        │     ├─ decompileSignals()
        │     ├─ decompileRoutingModels()
        │     ├─ decompilePluginTemplates()   // 仅在 2+ 处复用的插件才抽模板
        │     └─ decompileDecisions()         // Decision → ROUTE
        ├─ writeEntrypoints(cfg.Entrypoints)
        └─ 对每条非默认 recipe：cfg.ConfigForRecipe(recipe) → 再跑一遍上面的 routing 反编译，缩进塞进 RECIPE 块
```

Decision → ROUTE 的关键仍是 `RuleNode` 树 → DSL 表达式：`decompileRuleNode` 做的是编译期 `compileBoolExpr` 的「逆拍平」。

YAML 方向有三个出口，产物的「形状」不同：

| 函数 | 产物形状 |
| --- | --- |
| `EmitYAML` / `EmitYAMLFromConfig` | **扁平**结构，直接 marshal `RouterConfig` |
| `EmitRoutingYAMLFromConfig` | 仅 **routing 片段**（v0.3 canonical 的 `routing`/`entrypoints`/`recipes`） |
| `EmitUserYAML` | **用户友好嵌套**（把扁平的 `keyword_rules` 等归拢回 `signals:` 段、把 `vllm_endpoints` 重组为 `providers.models`） |
| `EmitCRD` / `EmitHelm` | 部署形态：Operator CRD、Helm values |

#### 4.3.3 源码精读

**反编译入口** `Decompile` 只是 `DecompileConfig` 的别名，见 [decompiler.go:13-15](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/decompiler.go#L13-L15)。`DecompileConfig` 的编排见 [decompiler_scopes.go:13-39](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/decompiler_scopes.go#L13-L39)：先写共享路由目录，再写 entrypoint，最后遍历 `cfg.Recipes`，对每条非默认配方用 `ConfigForRecipe` 取**隔离视图**后递归反编译、缩进嵌入 `RECIPE` 块（[decompiler_scopes.go:70-87](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/decompiler_scopes.go#L70-L87)）。

`DecompileRouting` 见 [routing_contract.go:43-67](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/routing_contract.go#L43-L67)，按「strategy → SIGNALS → MODELS → PLUGINS → ROUTES」分段输出，每段用 `writeSection` 加横线注释分隔（[decompiler.go:80-84](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/decompiler.go#L80-L84)）。

**规则树的逆翻译** `decompileRuleNode` 见 [decompiler_decisions.go:87-117](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/decompiler_decisions.go#L87-L117)：叶子（`Type != ""`）输出 `族("名")` 形式；AND 用 `flattenRuleNode` 拍平成 `a AND b AND c`；OR 拍平并**加括号** `(a OR b OR c)`（因为 OR 优先级低于 AND，必须括起来保语义）；NOT 输出 `NOT <inner>`。这与编译期的拍平对称：

\[
\text{AND}(a,b,c) \;\Longleftrightarrow\; a \wedge b \wedge c, \qquad
\text{OR}(a,b,c) \;\Longleftrightarrow\; (a \vee b \vee c)
\]

叶子细节 `decompileRuleLeaf` [decompiler_decisions.go:119-136](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/decompiler_decisions.go#L119-L136)：把 `Label`/`Predicate`/`OnError` 还原回 DSL 参数。`flattenRuleNode` [decompiler_decisions.go:149-158](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/decompiler_decisions.go#L149-L158) 是 `flattenBoolExpr` 的镜像。

**Decision → ROUTE** 的拼装 `decompileDecision` 见 [decompiler_decisions.go:200-219](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/decompiler_decisions.go#L200-L219)：注意它**总是输出 `PRIORITY`**（`d.write("  PRIORITY %d\n", dec.Priority)`），但 `TIER` 仅在非零时输出——这是反编译与原 DSL 可能产生「表面差异」的一个来源（原 DSL 可能省略了 PRIORITY）。

**MODEL 省略优化**：`writeDecisionModels` [decompiler_decisions.go:229-244](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/decompiler_decisions.go#L229-L244) 在 `candidateIterationsCoverModelRefs(dec)` 为真时**不输出 MODEL 行**——当一条决策的模型列表完全由某个 `FOR ... IN [models]` 候选迭代覆盖时，MODEL 是冗余的，省略可读性更好。判定见 [decompiler_decisions.go:64-85](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/decompiler_decisions.go#L64-L85)。

**YAML 出口**：

- `EmitYAML` / `EmitYAMLFromConfig` 见 [emitter_yaml.go:15-34](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/emitter_yaml.go#L15-L34)。注意一个分叉：当配置含 KB / 多 entrypoint / 多 recipe 时，走 `CanonicalConfigFromRouterConfig` 输出 canonical 五段式；否则直接 marshal `RouterConfig`。
- `EmitUserYAML` 见 [emitter_yaml.go:39-59](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/emitter_yaml.go#L39-L59)——这是「逆规范化」：先 marshal 成扁平 map，再用 `denormalizeSignals`/`denormalizeProviders`/`pruneZeroValueInfra` 在 map 层面重组。
  - `denormalizeSignals` [emitter_yaml.go:62-96](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/emitter_yaml.go#L62-L96)：把扁平的 `keyword_rules`/`embedding_rules`/`categories` 等键归拢到嵌套的 `signals:` 段下。
  - `denormalizeProviders` [emitter_yaml.go:116-143](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/emitter_yaml.go#L116-L143)：把扁平的 `vllm_endpoints`+`model_config` 重组回用户友好的 `providers.models[].endpoints[]` 结构（端点名按 `{modelName}_{epName}` 拆分还原）。
  - `pruneZeroValueInfra` [emitter_yaml.go:277-307](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/emitter_yaml.go#L277-L307)：**砍掉 DSL 表达不了的纯基础设施段**（`embedding_models`/`classifier`/`semantic_cache`/`memory`/`tools`/`observability`… 若为零值）。这正是「DSL↔YAML 不完全等价」的根因——DSL 只描述路由逻辑面，基础设施面在 YAML 里独立存在。

**往返契约的权威定义**：测试 `TestMaintainedBalanceRoutingAssetsStayInSync` [maintained_asset_roundtrip_test.go:72-82](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/maintained_asset_roundtrip_test.go#L72-L82) 断言：把维护的 `recipe.dsl` 编译成 routing YAML 再解析成 `CanonicalRouting`，必须与直接解析 `config.yaml` 得到的 `CanonicalRouting` **深相等**（`reflect.DeepEqual`）。即契约是定义在 **canonical routing 层**的等价，不是 DSL 文本逐字符相等。`mustCompileMaintainedRoutingDSL` [maintained_asset_roundtrip_test.go:322-338](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/maintained_asset_roundtrip_test.go#L322-L338) 展示了完整链路：`CompileAST` → `EmitRoutingYAMLFromConfig` → `ParseRoutingYAMLBytes` → `CanonicalRoutingFromRouterConfig`。

**格式化** `Format` [decompiler.go:102-121](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/decompiler.go#L102-L121) 是「解析 → 编译 → 反编译」三段式，把任意合法 DSL 规范化成标准排版（并单独保留 TEST 块）。

#### 4.3.4 代码实践

**实践目标**：亲手走一遍「YAML ↔ DSL」往返，识别差异来源。

**操作步骤**（在 `src/semantic-router/` 目录下）：

1. 把维护的 config.yaml 反编译成 DSL：

   ```bash
   go run ./cmd/dsl decompile -o /tmp/balance.from-yaml.dsl \
       ../../../config/recipes/balance/config.yaml
   ```

   （路径以你 cwd 为准；`decompile` 的第一个位置参数是输入 YAML，`-o` 是输出 DSL。）

2. 把维护的 recipe.dsl 编译成路由片段 YAML：

   ```bash
   go run ./cmd/dsl compile -o /tmp/balance.from-dsl.yaml \
       ../../../config/recipes/balance/recipe.dsl
   ```

3. 文本对比 DSL 方向：

   ```bash
   diff ../../../config/recipes/balance/recipe.dsl /tmp/balance.from-yaml.dsl
   ```

4. 跑结构往返测试（最权威的等价性检查）：

   ```bash
   go test ./pkg/dsl/ -run TestMaintainedBalanceRoutingAssetsStayInSync -v
   ```

**需要观察的现象**：

- 第 3 步的 `diff` **几乎一定有输出**（非空），但都是「无害的格式差异」：
  - 反编译输出带 `# ===` 分段注释横线（`writeSection`）；
  - `PRIORITY` 总是被显式写出；
  - 在多处复用的插件可能被抽成 `PLUGIN` 模板（`extractPluginTemplates` 只对使用 ≥2 次的插件抽模板，见 [decompiler.go:59-64](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/decompiler.go#L59-L64)）；
  - 原 DSL 里的注释、空行、作者手写的排版会被规范化掉；
  - 若某决策的 MODEL 完全由候选迭代覆盖，MODEL 行会被省略。
- 第 4 步的测试**应当通过（PASS）**——证明这些文本差异在 canonical routing 层面是等价的。

**预期结果**：`diff` 有文本差异，但 `TestMaintainedBalanceRoutingAssetsStayInSync` 通过。这就是「DSL 文本不逐字可逆，但 routing 语义可逆」的活教材。

**差异来源清单**（解释用）：

| 差异 | 来源函数 | 是否影响语义 |
| --- | --- | --- |
| 分段横线注释 | `writeSection` | 否 |
| `PRIORITY` 总是写出 | `decompileDecision` | 否 |
| 插件模板化 | `extractPluginTemplates`（≥2 次复用） | 否 |
| MODEL 行省略 | `candidateIterationsCoverModelRefs` | 否（候选迭代已覆盖） |
| 注释/空行丢失 | 反编译不保留原文注释 | 否 |
| 基础设施段缺失 | `pruneZeroValueInfra`（仅 EmitUserYAML） | 是，但仅影响 YAML 全量形态，不影响路由面 |

> 待本地验证：第 1、2 步的实际命令路径与 `diff` 输出内容以本地为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `decompileRuleNode` 对 OR 要加括号，对 AND 不用？

<details><summary>参考答案</summary>

因为 DSL 的优先级是 NOT > AND > OR（u7-l1）。反编译时若把 OR 输出成裸的 `a OR b OR c`，再被嵌进一个 AND 上下文（如 `x AND a OR b`）会被重新解析成 `(x AND a) OR b`，语义改变。加括号 `(a OR b OR c)` 可保证任意上下文下语义不变。AND 优先级高，裸输出 `a AND b AND c` 已无歧义，故不需括号。
</details>

**练习 2**：`EmitUserYAML` 为什么不能直接 marshal `RouterConfig`，而要先 marshal 成 map 再做 `denormalize*`？

<details><summary>参考答案</summary>

因为 `RouterConfig` 的 Go 结构是「扁平」的（信号规则散落在 `KeywordRules`/`EmbeddingRules`/`Categories`… 各字段），而用户友好的 `config.yaml` 是「嵌套」的（统一收在 `signals:` 下）。Go 的 yaml tag 没法表达「把多个字段归拢到一个父键」，所以在 map 层面手工重组最直接。这本质上是 `normalizeYAML` 的逆操作（见 `EmitUserYAML` 注释「This is the inverse of normalizeYAML」）。
</details>

**练习 3**：往返契约（`TestMaintainedBalanceRoutingAssetsStayInSync`）为什么定义在 `CanonicalRouting` 层，而不是 DSL 文本层？

<details><summary>参考答案</summary>

因为 DSL 文本层有大量无损但无关语义的差异（注释、排版、PRIORITY 是否省略、插件是否模板化）。真正的契约是「路由逻辑等价」，而 `CanonicalRouting` 恰好是路由逻辑的规范投影——它剥掉了所有非路由噪声。把契约放在这一层，既允许作者自由排版 DSL，又能用 `DeepEqual` 做严格机器校验。
</details>

---

## 5. 综合实践

**任务**：为 balance 配方建立一条「DSL → YAML → DSL」的本地往返流水线，并产出一份差异分析。

1. **编译**：`go run ./cmd/dsl compile config/recipes/balance/recipe.dsl -o /tmp/balance.routing.yaml`（在 `src/semantic-router/` 下，注意相对路径），打开 `/tmp/balance.routing.yaml`，确认它只含 `routing`/`entrypoints`/`recipes` 三段（路由片段，无 providers/listeners）。

2. **反编译**：`go run ./cmd/dsl decompile config/recipes/balance/config.yaml -o /tmp/balance.roundtrip.dsl`。

3. **二次编译验证**：把上一步生成的 `/tmp/balance.roundtrip.dsl` 再编译一次，`go run ./cmd/dsl compile /tmp/balance.roundtrip.dsl -o /tmp/balance.routing2.yaml`，然后 `diff /tmp/balance.routing.yaml /tmp/balance.routing2.yaml`。

4. **结构等价校验**：`go test ./pkg/dsl/ -run 'TestMaintainedBalance' -v`，记录通过/失败。

5. **产出**：写一段 200 字以内的结论，覆盖三点——(a) 文本层 `diff` 出现了哪几类差异；(b) 二次编译后的 routing YAML 与第一次是否一致（说明往返是否「定点」收敛）；(c) 你认为哪一类差异最可能误导新手、为什么。

**预期**：第 3 步两次 routing YAML 应基本一致（往返收敛到规范形态）；第 4 步测试通过。结论里应点出「PRIORITY 总是写出」「插件模板化」「注释丢失」等差异，并指出注释丢失最易误导（因为原 DSL 的中文意图注释在反编译后消失）。

> 待本地验证：各步命令的相对路径与 diff 具体内容以本地实际为准。

## 6. 本讲小结

- **编译是单向阀门**：`Compile`/`CompileAST` 把 resolved AST 翻译成强类型 `RouterConfig`，先种 `DefaultGlobalConfig()` 默认值，再按「插件模板→信号→投影→模型→路由」固定顺序逐块下沉，错误累积而非抛出。
- **ROUTE → Decision 是重点**：WHEN 布尔树由 `compileBoolExpr` 递归翻译成 `RuleNode`（= `RuleCombination` 别名）树，编译期对同级 AND/OR 做「拍平」优化，叶子节点承载 `Type`+`Name`+可选 `Predicate`。
- **作用域编译用全新实例**：每条 RECIPE 走 `newScopedCompiler`，从结构上杜绝跨配方符号串用，把 u3-l1 的「recipe 隔离」契约在编译期兑现。
- **反编译是逆方向但有损**：`Decompile`/`DecompileRouting` 把配置写回 DSL 文本，`decompileRuleNode` 做逆拍平（OR 加括号保语义）；`EmitUserYAML` 做逆规范化把扁平结构重组回嵌套 `signals`/`providers`。
- **往返契约定义在 canonical routing 层**：`TestMaintainedBalanceRoutingAssetsStayInSync` 用 `reflect.DeepEqual` 校验 `CanonicalRouting` 深相等，允许 DSL 文本自由排版——DSL 文本不逐字可逆，但路由语义严格可逆。
- **有损点可枚举**：分段注释、`PRIORITY` 总是写出、插件 ≥2 次复用才模板化、MODEL 在候选迭代覆盖时省略、基础设施段被 `pruneZeroValueInfra` 砍除（仅影响 YAML 全量形态）。

## 7. 下一步学习建议

- **运行时如何消费这棵 `RuleNode`**：进入 u6-l1（决策引擎：布尔规则求值与置信度），看 `evalNode` 如何递归求值编译产物。
- **DSL 的命令行与 TEST 块**：进入 u7-l3（dsl 命令行工具），看 `validate`/`compile`/`generate`/`test_runner` 如何把本讲的编译能力暴露成开发者工具，以及 TEST 块如何做运行期校验。
- **配置如何被加载与校验**：回顾 u3-l3（配置加载与校验），理解本讲产出的 `RouterConfig` 在真实启动链路里如何被 `config.Parse`/`Load`/`Replace` 与语义校验器接管。
- **多形态部署产出**：若对 Operator/Helm 部署感兴趣，可追读 `EmitCRD`/`EmitHelm`/`MergeRoutingIntoBase`，它们与本讲的 `EmitRoutingYAMLFromConfig` 共享同一 canonical 投影。
