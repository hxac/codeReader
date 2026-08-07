# dsl 命令行工具

## 1. 本讲目标

本讲承接 u7-l2「DSL 编译与反编译」——你已经知道 DSL 文本与运行时配置 `RouterConfig` 之间如何双向翻译。但那些 `Compile`/`Decompile` 都是**库函数**，需要写 Go 代码才能调用。本讲回答一个更实际的问题：

> 作为一个写 recipe 的开发者，我在终端里用什么命令把 `.dsl` 文件变成可运行的 `config.yaml`？怎么在不启动整个路由器的前提下校验我写的策略？怎么让一条查询真正跑一遍，看它落到哪条 ROUTE？

读完本讲你将掌握：

- `sr-dsl` 命令行工具的五个子命令（compile / decompile / validate / fmt / generate）的用法与适用场景。
- `validate` 的两层校验：**静态校验**（纯 AST，无需模型）与 **运行时校验**（`--runtime-checks`，真跑分类器+决策引擎）。
- DSL 内嵌的 `TEST` 块如何声明「查询 → 期望路由」的断言，以及 `test_runner.go` 如何用原生分类器执行它。
- `generate` 子命令如何用一个 LLM 把自然语言描述直接生成 DSL，以及它的「schema-as-supervision + 修复循环」工作方式。

## 2. 前置知识

本讲默认你已经读过：

- **u7-l1 DSL 语法与 AST**：知道 `.dsl` 文件由 `SIGNAL`/`PROJECTION`/`MODEL`/`ROUTE`/`TEST` 等顶层块组成，知道 `Parse` 把字符串变成 `Program` AST。
- **u7-l2 DSL 编译与反编译**：知道 `Compile` 把 AST 变成 `RouterConfig`，`Decompile` 反过来。
- **u2-l4 决策与路由**：知道一条 `ROUTE` 的 `WHEN` 是布尔规则树，命中后产出决策名（decision name）。

还需要几个工程常识：

- **CLI 子命令分发**：一个二进制程序根据第一个参数（如 `compile`、`validate`）决定执行哪段逻辑，是 cobra/click/手写 `switch` 的通用模式（u1-l4 讲过 Python 端的 click，这里是 Go 端的手写 `switch`）。
- **flag 解析**：Go 标准库 `flag` 包负责把 `-o out.yaml`、`--runtime-checks` 这类选项解析成变量。
- **stdout 与 stderr**：正常产物（如编译出的 YAML）写到 stdout 便于管道传递；错误与警告写到 stderr，不污染产物。
- **静态 vs 运行时**：静态校验只看文本/AST（快、无需依赖）；运行时校验要真正执行（慢、可能需要加载模型），但能发现静态发现不了的问题。这是贯穿本讲的核心二分。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [src/semantic-router/cmd/dsl/main.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/dsl/main.go) | 程序入口，手写 `switch` 分发五个子命令；定义 `runCompile`/`runValidate`/`runFormat` 等。 |
| [src/semantic-router/cmd/dsl/generate.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/dsl/generate.go) | `generate` 子命令实现：解析 LLM 相关 flag、调用 `nlgen` 生成 DSL。 |
| [src/semantic-router/cmd/dsl/test_runner.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/dsl/test_runner.go) | 实现 `TestBlockRunner` 接口的「原生运行器」：用真实的 `classification.Classifier` 跑 TEST 块查询。 |
| [src/semantic-router/pkg/dsl/cli.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/cli.go) | 各子命令调用的库函数：`CLICompile`/`CLIDecompile`/`CLIValidateWithRunner`/`CLIFormat`。 |
| [src/semantic-router/pkg/dsl/test_blocks.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/test_blocks.go) | TEST 块的数据结构（`TestBlockResult`、`TestBlockRunner` 接口）与运行时比对逻辑 `ValidateTestBlocks`。 |
| [src/semantic-router/internal/nlgen/generate.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/internal/nlgen/generate.go) | 自然语言生成 DSL 的核心：`NLResult` 结构与修复循环。 |

> 约定：本讲「`sr-dsl`」指编译出的二进制名，开发期可用 `go run ./cmd/dsl ...` 等价运行（见第 5 节实践）。

## 4. 核心概念与源码讲解

### 4.1 validate / compile 子命令：命令分发与三种产物

#### 4.1.1 概念说明

`sr-dsl` 是一个**单一二进制、多子命令**的工具，和 `git`、`docker` 同构。它的入口 `main.go` 没有用 cobra 这类框架，而是最朴素的手写方式：取出 `os.Args[1]` 作为子命令名，再用 `switch` 分发。这种写法的好处是零依赖、易读，缺点是要自己处理帮助文本。

子命令共五个（外加 `help`）：

| 子命令 | 方向 | 作用 |
|--------|------|------|
| `compile` | DSL → YAML/CRD/Helm | 把 `.dsl` 编译成运行时配置 |
| `decompile` | YAML → DSL | 把配置反编译回人友好 DSL |
| `validate` | DSL → 诊断 | 校验 DSL，报告错误/警告/约束 |
| `fmt`（别名 `format`） | DSL → DSL | 格式化 DSL，规范排版 |
| `generate` | 自然语言 → DSL | 用 LLM 生成 DSL |

注意：u7-l2 讲的 `Compile`/`Decompile` 是 `pkg/dsl` 里的**库函数**（Go API），本讲的 `compile`/`decompile` 是**命令行壳**，它们只是读文件 → 调库函数 → 写文件。理解这层「壳」与「芯」的分离很关键：所有真正的逻辑都在 `pkg/dsl` 里，`cmd/dsl` 只负责参数解析与输入输出。

#### 4.1.2 核心流程

以 `compile` 为例的端到端流程：

```
终端命令 sr-dsl compile -o out.yaml --base providers.yaml input.dsl
   │
   ▼
main(): 取 cmd="compile"，重排 os.Args，调用 runCompile()
   │
   ▼
runCompile(): 用 flag 包解析 -o/--format/--base/--name/--namespace
   │
   ▼
dsl.CLICompile(inputPath, output, format, crdName, crdNamespace, base)
   │  读 input.dsl → 调 dsl.Compile() 得到 *config.RouterConfig
   │  （ Compile 即 u7-l2 讲的编译总线 ）
   ▼
emitFormat(cfg, format, ...): 按 format 选产物
   ├─ "yaml"  → EmitRoutingYAMLFromConfig(cfg)   （有 --base 则合并基础设施 ）
   ├─ "crd"   → EmitCRD(cfg, name, namespace)     （生成 K8s CRD ）
   └─ "helm"  → EmitHelm(cfg)                     （生成 Helm values ）
   │
   ▼
writeOutput(): outputPath 空/“-” 写 stdout，否则写文件
```

`validate` 的流程类似，但产物不是配置而是**诊断信息**，且多了一条「运行时校验」的支线（见 4.2）。

#### 4.1.3 源码精读

**命令分发**——这是整个程序的骨架。注意第 38 行那个 `os.Args = append(...)` 的「腾挪」：它把子命令名从参数列表里删掉，让后续每个 `runXxx()` 都能用标准 `flag.Parse` 处理「自己那一组」参数，互不干扰。

[src/semantic-router/cmd/dsl/main.go:L31-L58](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/dsl/main.go#L31-L58) —— `main()` 取第一个参数做 `switch` 分发，未知命令打印 usage 并退出。

`compile` 子命令的参数定义与委托：

[src/semantic-router/cmd/dsl/main.go:L60-L83](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/dsl/main.go#L60-L83) —— `runCompile` 定义了 `-o`（输出路径）、`--format`（yaml/crd）、`--base`（基础设施基线）、`--name`/`--namespace`（CRD 专用），最后把活全交给 `dsl.CLICompile`。

真正的「芯」`CLICompile` 在 `pkg/dsl/cli.go`：读文件 → `Compile` → `emitFormat` → `writeOutput`。

[src/semantic-router/pkg/dsl/cli.go:L16-L35](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/cli.go#L16-L35) —— 编译错误会逐条打到 stderr，最后返回 `"%d compilation error(s)"` 让命令行以非零码退出。

`--format` 的三选一在这里：

[src/semantic-router/pkg/dsl/cli.go:L37-L54](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/cli.go#L37-L54) —— `emitFormat` 按 `format` 分派到 `EmitRoutingYAMLFromConfig` / `EmitCRD` / `EmitHelm`。注意 `--base` 仅对 `yaml` 生效：它把编译出的 routing 叠加到一个含 `version/listeners/providers` 的基线 YAML 上，产出完整可运行配置（`emitMergedConfig` → `MergeRoutingIntoBase`）。

> 提示：`compile --format crd` 产出的就是 u12-l3 将讲的 `SemanticRouter` CRD，可作为 Operator 的输入，是「DSL → K8s 声明式资源」的直达通路。

#### 4.1.4 代码实践

**实践目标**：亲手把一个真实 recipe 的 DSL 编译成 YAML，并观察 `--format crd` 的差异。

**操作步骤**：

1. 在仓库根目录执行静态校验，确认 DSL 合法：

   ```bash
   go run ./cmd/dsl validate config/recipes/balance/recipe.dsl
   ```

2. 编译成 YAML 并写到临时文件：

   ```bash
   go run ./cmd/dsl compile -o /tmp/balance-from-dsl.yaml config/recipes/balance/recipe.dsl
   ```

3. 再用 CRD 格式生成一份，对比结构差异：

   ```bash
   go run ./cmd/dsl compile --format crd -o /tmp/balance-crd.yaml config/recipes/balance/recipe.dsl
   ```

**需要观察的现象**：

- 步骤 1 应输出 `No issues found.` 或仅含少量 `🟡 Warning`/`🟠 Constraint`（balance 故意保留了若干非互斥 domain 路由，会产生冲突警告，但不应有 `🔴 Error`）。
- 步骤 2 产出的 `/tmp/balance-from-dsl.yaml` 顶层是 `routing:` 段（routing YAML 形态），把它与仓库自带的 `config/recipes/balance/config.yaml` 对照，routing 部分应语义一致（u7-l2 已说明：DSL↔YAML 在 `CanonicalRouting` 层 `reflect.DeepEqual` 可逆，但文本不逐字相同）。
- 步骤 3 产出的 CRD 顶层是 `apiVersion: aigateway.vllm.ai/v1alpha1` 之类的 Kubernetes 资源包裹，`spec` 内才是路由配置。

**预期结果**：三条命令均以退出码 0 结束；步骤 2、3 各生成一份文件。若 `validate` 报 `🔴 Error`，说明本地仓库 DSL 有改动或环境异常，应先排查。

> 说明：`validate` 默认是纯静态校验，不需要任何模型，因此在任何机器上都能跑通。`compile` 同理。

#### 4.1.5 小练习与答案

**练习 1**：`compile` 的 `--base providers.yaml` 是做什么用的？如果不加会怎样？

<details><summary>参考答案</summary>

`--base` 指向一个含基础设施段（`version`/`listeners`/`providers`）的 YAML。DSL 本身只描述 routing，不含模型后端接入；`--base` 让 `emitMergedConfig` 把编译出的 routing 叠加进基线，产出一份**完整可运行**的 config（含 providers）。不加 `--base` 时，`compile` 只产出 routing YAML，需要再与 providers 配置合并才能实际运行。
</details>

**练习 2**：为什么 `runCompile` 里要先 `os.Args = append(os.Args[:1], os.Args[2:]...)` 再 `fs.Parse`？

<details><summary>参考答案</summary>

`main()` 已经把子命令名（`os.Args[1]`）取走了，但 Go 的 `flag.Parse` 默认从 `os.Args[1:]` 开始解析。如果不删掉子命令名，`flag` 会把 `compile` 当成第一个非 flag 参数，导致后续真正的位置参数（输入文件路径）错位。这行代码把参数列表重排成 `[程序名, 剩余参数...]`，让每个 `runXxx` 都能像独立程序一样干净地 `Parse`。
</details>

### 4.2 TEST 块运行：从静态断言到运行时求值

#### 4.2.1 概念说明

`TEST` 块是 DSL 里一种**可执行的文档**：你声明「这条查询应该落到这条 ROUTE」，工具就帮你验证。它的语法是：

```
TEST routing_intent {
  "urgent help" -> urgent_route
}
```

每个条目是 `"查询文本" -> 期望路由名`。这和单元测试里的 `assertEqual(路由(query), route)` 是一回事，只不过断言写在策略文件里、紧挨着被测的策略。

关键在于：`TEST` 块有**两层校验**，对应两种成本：

| 层级 | 触发条件 | 做什么 | 成本 |
|------|----------|--------|------|
| **静态校验** | 总是运行 | 检查 `->` 右边的路由名是否真实存在、查询串是否非空 | 极低，纯 AST |
| **运行时校验** | 加 `--runtime-checks` | 真把查询喂给分类器+决策引擎，比对实际命中路由是否等于期望 | 高，需加载模型 |

为什么要分两层？因为静态校验能抓「笔误」（路由名写错），但抓不了「逻辑错」（路由名写对了，可这条查询实际根本不会命中它——比如 WHEN 条件写反了）。运行时校验才能抓后者，但它要拉起整套信号管线，所以默认关闭。

#### 4.2.2 核心流程

运行时校验的流程（在 `validate --runtime-checks` 时触发）：

```
cliValidate(): 先跑静态 Validate() 得到 diags
   │
   ▼ 有 🔴/🟠 阻断性诊断？ 是 → 直接返回（不跑运行时，因为程序都不合法）
   │ 否
   ▼
appendRuntimeValidationDiagnostics():
   │  Parse 一遍拿到 *Program
   │  programNeedsRuntimeValidation(prog)?  （含 TEST 块 或 softmax_exclusive 投影分区）
   │    否 → 跳过
   │    是
   ▼
factory(prog)  → buildNativeTestBlockRunner(prog):
   │  CompileAST(prog) 得到 cfg
   │  loadClassifierMappings(cfg)  （加载 domain/PII/jailbreak 映射表）
   │  classification.NewClassifier(cfg, ...)  构建真实分类器
   ▼
collectRuntimeValidationDiagnostics(prog, runner):
   ├─ ValidateTestBlocks(prog, runner)        逐条跑 TEST 查询，比对路由
   └─ runner.ValidateProjectionPartitions(prog) 投影分区质心相似度检查
   │
   ▼
所有诊断汇入 writeValidationDiagnostics 打印汇总
```

注意两个「护栏」：

1. **静态不通过不跑运行时**（`hasBlockingDiagnostics`）：有语法/约束错误时直接停，避免浪费模型加载。
2. **运行时失败不阻断构建**：运行时诊断里 TEST 不匹配也是 `🔴 Error`，但它只影响 `validate` 的退出码，不影响 `compile`——`compile` 不跑 TEST。

#### 4.2.3 源码精读

**TEST 块的 AST 形态**——先看它在解析树里长什么样：

[src/semantic-router/pkg/dsl/ast.go:L403-L417](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/ast.go#L403-L417) —— `TestBlockDecl`（一个 TEST 块，含 `Name` 与若干 `Entries`）和 `TestEntry`（一条 `"query" -> route`）。

**静态校验**——每次 `validate` 都跑，无需任何运行时依赖：

[src/semantic-router/pkg/dsl/validator_conflicts.go:L720-L743](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/validator_conflicts.go#L720-L743) —— `checkTestBlocks` 只做两件事：路由名不存在 → `🟡 Warning`；查询串为空 → `🟠 Constraint`。它不执行查询。

**运行时校验的总闸**——`--runtime-checks` 开关如何变成工厂：

[src/semantic-router/cmd/dsl/main.go:L107-L138](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/dsl/main.go#L107-L138) —— `runValidate` 解析 `--runtime-checks` 布尔 flag，经 `validateRunnerFactory` 转成「工厂或 nil」：不加开关时返回 `nil`（纯静态），加上才返回 `buildNativeTestBlockRunner`。`errCount > 0` 时以退出码 1 退出。

**运行时校验的编排**（库侧）——这是「是否真跑」的决策核心：

[src/semantic-router/pkg/dsl/cli.go:L124-L154](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/cli.go#L124-L154) —— `appendRuntimeValidationDiagnostics` 守住三道门：`factory == nil`（没开开关）、`hasBlockingDiagnostics`（静态已有错）、`!programNeedsRuntimeValidation`（没 TEST 块也没 softmax 分区）任一成立即跳过；否则用工厂造运行器，再由 `collectRuntimeValidationDiagnostics` 收集 TEST 比对与投影分区两类诊断。

**判定是否需要运行时**：

[src/semantic-router/pkg/dsl/test_blocks.go:L145-L158](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/test_blocks.go#L145-L158) —— `programNeedsRuntimeValidation`：有 TEST 块，或有 `softmax_exclusive` 投影分区时返回 true。

**TEST 块的运行时接口与比对**——这是「跑查询、比路由」的核心：

[src/semantic-router/pkg/dsl/test_blocks.go:L8-L51](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/test_blocks.go#L8-L51) —— `TestBlockRunner` 只有一个方法 `EvaluateTestBlockQuery(query)`，返回命中的 `DecisionName/Confidence/MatchedRules`；`ValidateTestBlocks` 逐条调用，实际命中 ≠ 期望即产出不匹配诊断。注意 `shouldEvaluateTestEntry` 只评估「查询非空 且 路由名存在」的条目——后者正好与静态校验互补。

`validateTestEntry` 的三分支比对很值得读：运行报错、没命中任何路由、命中但路由不符，分别给出不同诊断。

[src/semantic-router/pkg/dsl/test_blocks.go:L57-L69](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/dsl/test_blocks.go#L57-L69) —— 比对逻辑：结果为空报「无匹配路由」，结果路由等于期望则通过，否则报错并附上实际置信度与命中规则。

**原生运行器如何造、如何跑**——这是把 DSL 工具与真实分类管线缝合起来的地方：

[src/semantic-router/cmd/dsl/test_runner.go:L17-L37](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/dsl/test_runner.go#L17-L37) —— `buildNativeTestBlockRunner` 先 `CompileAST(prog)` 拿到 cfg，再加载三类映射表，最后 `classification.NewClassifier(...)` 造出一个真实分类器。也就是说：运行时校验复用的正是 u8 将讲的「分类编排 + 决策引擎」整条管线，没有 mock。

[src/semantic-router/cmd/dsl/test_runner.go:L78-L101](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/dsl/test_runner.go#L78-L101) —— `EvaluateTestBlockQuery` 把查询文本填进 `SignalEvaluationInput`（Text/ContextText/CurrentUserText 都置为同一查询），调 `EvaluateAllSignalsWithHeaders` 抽全部信号，再 `EvaluateDecisionWithEngine` 求决策，最后把 `(Decision.Name, Confidence, MatchedRules)` 装进 `TestBlockResult` 返回。

**附带的投影分区质心检查**——运行时校验还有第二条腿：检查 softmax 分区的成员候选「质心」是否过于相似：

[src/semantic-router/cmd/dsl/test_runner.go:L103-L139](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/dsl/test_runner.go#L103-L139) —— `ValidateProjectionPartitions` 调分类器的 `AnalyzeSoftmaxSignalGroupCentroids`，若两个分区成员的候选质心余弦相似度超过阈值（第 15 行常量 `projectionPartitionCentroidWarningThreshold = 0.7`），就发 `🟡 Warning`：提示「在模糊查询上 softmax 得分可能近乎均匀」。这正是 u2-l3 投影层的运行时健康检查。

#### 4.2.4 代码实践

**实践目标**：为一个 ROUTE 写一个 TEST 块，并分别用静态校验与运行时校验跑它，体会两者差异。

> 选择策略：balance recipe 依赖大量 embedding/domain 信号，运行时校验需要可用的嵌入模型，未必每台机器都具备。因此本实践用一个**只含 keyword 信号的最小 DSL**（keyword 是规则型信号，不依赖嵌入模型，最易复现），它和仓库测试 `TestBuildNativeTestBlockRunnerEvaluatesKeywordRoute` 同构。

**操作步骤**：

1. 新建文件 `/tmp/mini.dsl`，内容如下（这是「示例代码」，仿照仓库测试编写）：

   ```dsl
   SIGNAL keyword urgent {
     operator: "OR"
     keywords: ["urgent"]
   }

   ROUTE urgent_route {
     PRIORITY 100
     WHEN keyword("urgent")
     MODEL "m:1b"
   }

   ROUTE fallback {
     MODEL "m:1b"
   }

   TEST routing_intent {
     "urgent help" -> urgent_route
   }
   ```

2. 先跑**静态校验**（不加 `--runtime-checks`）：

   ```bash
   go run ./cmd/dsl validate /tmp/mini.dsl
   ```

3. 再跑**运行时校验**：

   ```bash
   go run ./cmd/dsl validate --runtime-checks /tmp/mini.dsl
   ```

4. 故意把 TEST 的期望路由改错，验证运行时校验能抓到逻辑错误。把 `/tmp/mini.dsl` 最后一行改成：

   ```dsl
     "urgent help" -> fallback
   ```

   再跑步骤 3 的命令。

**需要观察的现象**：

- 步骤 2（静态）：应输出 `No issues found.`——因为 `urgent_route` 与 `fallback` 都存在、查询非空，静态校验全过。
- 步骤 3（运行时，期望正确）：应仍通过；背后它真的把 `"urgent help"` 跑了一遍分类器，确认命中 `urgent_route`。
- 步骤 4（期望改错）：应报形如 `🔴 Error: TEST routing_intent: query "urgent help" expected route "fallback", got "urgent_route" (confidence 0.xxx; matched rules: keyword:urgent)` 的诊断，且退出码非 0。

**预期结果**：步骤 4 能清楚看到「实际命中 vs 期望」的差异信息——这正是运行时校验相对静态校验的增量价值。

> 待本地验证：步骤 3、4 依赖 `classification.NewClassifier` 能成功构建（可能需要 domain/PII/jailbreak 映射文件或最小模型）。若本机缺少这些依赖，命令会在「native runtime validation initialization failed」处报错并退回静态结果；这种情况下步骤 2 仍可复现，步骤 3/4 需在配置好模型的环境（如 u1-l3 的 nvidia/amd 本地环境）中验证。

#### 4.2.5 小练习与答案

**练习 1**：如果一个 TEST 条目写成 `"some query" -> nonexistent_route`，静态校验和运行时校验分别会怎么报？

<details><summary>参考答案</summary>

静态校验（`checkTestBlocks`）会发一条 `🟡 Warning: TEST ...: route "nonexistent_route" is not defined`。运行时校验里，`shouldEvaluateTestEntry` 会因为 `routeNames[entry.RouteName]` 为 false 而**跳过该条**（不评估），所以不会额外报运行时错误。即：静态层负责抓「不存在的路由名」，运行时层只评估合法条目，二者职责互补、不重复。
</details>

**练习 2**：为什么 `appendRuntimeValidationDiagnostics` 在 `hasBlockingDiagnostics(diags)` 为真时直接返回原诊断，不启动运行时？

<details><summary>参考答案</summary>

阻断性诊断（`🔴 Error` 或 `🟠 Constraint`）意味着 DSL 本身有语法或硬约束错误，程序可能根本无法编译成有效配置。此时去 `CompileAST`、`NewClassifier` 多半会失败或无意义，还白白消耗加载模型的时间。先保证静态合法、再谈运行时，是「快失败、省成本」的常规护栏。
</details>

### 4.3 代码生成：用 LLM 从自然语言生成 DSL

#### 4.3.1 概念说明

`generate` 子命令是一个**反向入口**：你给它一句自然语言（「把数学题路由到 qwen-math，其余走 qwen2.5:3b」），它调一个 LLM 帮你写出对应的 DSL。这降低了写 DSL 的门槛——不必记住全部语法也能起步。

它的设计哲学叫 **schema-as-supervision（schema 即监督）**：系统提示词里塞进完整的 DSL 语法参考和 few-shot 示例，让 LLM 在「语法框架」约束下生成；生成后再用真实的 `Parse` 校验，**如果解析失败就带着错误反馈让 LLM 修**，循环若干次。这比单纯让 LLM「自由发挥」靠谱得多——DSL 的形式语法本身就成了校验器。

与 `compile`/`validate` 不同，`generate` **必须连一个 LLM 服务**：通过 `--api-url`/`--model`/`--api-key`（或对应环境变量 `VLLM_API_URL`/`VLLM_MODEL`/`VLLM_API_KEY`）指定一个 OpenAI 兼容的推理端点。

#### 4.3.2 核心流程

```
sr-dsl generate --api-url URL --model M "自然语言指令"
   │
   ▼
runGenerate() → doGenerate():
   │  parseGenerateFlags(): 解析 api-url/model/api-key/temperature/max-retries/timeout/-o
   │  校验 api-url、model 必填
   │  readInstruction(): 位置参数拼接，否则读 stdin
   ▼
nlgen.NewOpenAIClient(apiURL, model, apiKey)        构建 OpenAI 兼容客户端
   │
   ▼
nlgen.GenerateFromNL(ctx, client, instruction,
        WithTemperature(...), WithMaxRetries(...))   ← 核心生成 + 修复循环
   │
   ▼  返回 *NLResult{ DSL, RawOutput, Attempts, ParseError, Warnings }
printWarnings(result)                                解析失败/警告/重试次数 → stderr
writeOutput(output, result.DSL)                      DSL → stdout 或 -o 文件
```

修复循环的含义：`Attempts` 记录 LLM 被调用了几次。`Attempts == 1` 表示一次成功；若首次产物解析失败，会带错误反馈再试，`Attempts` 递增，直到成功或耗尽 `--max-retries`。若最终仍解析失败，`ParseError` 非空，工具会发警告但仍把（有缺陷的）DSL 输出，交由人工修。

#### 4.3.3 源码精读

**flag 定义与必填校验**：

[src/semantic-router/cmd/dsl/generate.go:L29-L44](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/dsl/generate.go#L29-L44) —— 每个 flag 都支持「命令行 → 环境变量」回退（`envOrDefault`），方便不在命令行里暴露 api-key。

[src/semantic-router/cmd/dsl/generate.go:L60-L98](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/dsl/generate.go#L60-L98) —— `doGenerate` 是主流程：缺 `--api-url` 或 `--model` 直接返回错误码 1；指令既可来自位置参数，也可来自 stdin（`readInstruction`，便于管道 `echo "..." | sr-dsl generate ...`）。

**生成结果的形状**——`NLResult` 是生成与修复循环的产物契约：

[src/semantic-router/internal/nlgen/generate.go:L112-L118](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/internal/nlgen/generate.go#L112-L118) —— `DSL` 是最终（已格式化的）DSL 文本；`Attempts` 是 LLM 调用次数；`ParseError` 非空表示最终仍解析失败；`Warnings` 是非致命校验警告。

**警告的呈现**：

[src/semantic-router/cmd/dsl/generate.go:L100-L123](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/dsl/generate.go#L100-L123) —— `printWarnings` 把解析失败、各类 warning、重试次数都打到 stderr（不污染 stdout 上的 DSL 产物）；`writeOutput` 与 `compile` 共用同一套「空或 `-` 写 stdout，否则写文件」约定。

> 调用链说明：`generate.go` 只与 `pkg/nlgen`（一个薄转发层）打交道，真正的 prompt 构造、LLM 调用、修复循环在 `internal/nlgen` 包里。本讲只到 CLI 这一层的入口，prompt 工程细节不在本讲范围。

#### 4.3.4 代码实践

**实践目标**：理解 `generate` 的输入输出契约（无需真的有 LLM 端点也能演练前半段）。

**操作步骤**：

1. 查看帮助与必填校验行为（不需要 LLM）：

   ```bash
   go run ./cmd/dsl generate "把数学路由到 qwen-math"
   # 预期：报 "Error: --api-url or VLLM_API_URL is required" 并退出
   ```

2. 如果你有一个 OpenAI 兼容端点（例如本地 vLLM 服务），完整跑一次（**待本地验证**）：

   ```bash
   export VLLM_API_URL=http://localhost:8090/v1
   export VLLM_MODEL=Qwen2.5-72B
   go run ./cmd/dsl generate "Route math to qwen-math, default to qwen2.5:3b" -o /tmp/gen.dsl
   ```

3. 对生成产物立即做闭环校验（这是推荐工作流）：

   ```bash
   go run ./cmd/dsl validate /tmp/gen.dsl
   ```

**需要观察的现象**：

- 步骤 1：确认 `--api-url` 与 `--model` 是必填，缺一即失败。
- 步骤 2：stderr 可能出现 `Note: required N attempts (repair loop)`，表示首次产物解析失败、经过修复才成功；stdout/`-o` 文件得到一份合法 DSL。
- 步骤 3：生成物应能通过静态校验（若仍有 `ParseError`，`validate` 会指出具体错误）。

**预期结果**：生成 → 校验形成闭环。即便没有 LLM 端点，步骤 1 + 步骤 3（用手工 DSL）也能让你掌握这条工作流。

> 待本地验证：步骤 2 依赖可用的 LLM 端点；无端点时可跳过，重点掌握「generate 产出 DSL → validate 校验」的衔接。

#### 4.3.5 小练习与答案

**练习 1**：`generate` 与 `compile` 在「产物是否可信」上的最大区别是什么？如何弥补？

<details><summary>参考答案</summary>

`compile` 的产物来自确定性的编译器，输入合法则输出必然合法。`generate` 的产物来自 LLM，**可能解析失败或语义错误**。弥补方式正是项目采用的修复循环（`--max-retries` 带反馈重试）+ 生成后立即 `validate`/`--runtime-checks` 闭环校验。换言之：把 DSL 的形式语法当作 LLM 输出的校验器。
</details>

**练习 2**：为什么 `generate` 把 `api-key` 设计成既可 `--api-key` 又可 `VLLM_API_KEY` 环境变量？

<details><summary>参考答案</summary>

避免在命令行（会被 shell history、`ps`、日志记录）中暴露密钥。环境变量是传递凭证的更安全通道，`envOrDefault` 的回退设计让本地试验（用 `--api-key`）与 CI/生产（用环境变量）都能方便地工作。
</details>

## 5. 综合实践

把本讲三个模块串成一个完整的「DSL 开发循环」。请完成下面这个贯穿性小任务：

**场景**：你要新增一条「紧急请求」专用路由，并用 `sr-dsl` 工具走完「编写 → 校验 → 编译 → 测试 → （可选）生成」全流程。

**任务步骤**：

1. **手写一份最小策略** `/tmp/urgent.dsl`，要求包含：
   - 一个 `keyword` 信号（关键词自选，如 `["urgent","asap"]`）。
   - 一条 `urgent_route`，`WHEN` 引用该信号，并带 `PRIORITY`。
   - 一条无 `WHEN` 的 `fallback` 兜底路由（回顾 u2-l4：每个配方必须有无条件兜底）。
   - 一个 `TEST` 块，含两条断言：一条应命中 `urgent_route`，一条应命中 `fallback`。

2. **静态校验**：`go run ./cmd/dsl validate /tmp/urgent.dsl`，确保无 `🔴 Error`。

3. **格式化**：`go run ./cmd/dsl fmt -o /tmp/urgent.fmt.dsl /tmp/urgent.dsl`，对比格式化前后差异（u7-l1 讲过 fmt 的规范化效果）。

4. **编译**：`go run ./cmd/dsl compile -o /tmp/urgent.yaml /tmp/urgent.dsl`，打开 `/tmp/urgent.yaml`，确认你的 ROUTE 被编译成了 `decisions[]` 的一条、信号编译成了规则（回顾 u7-l2 的 ROUTE→Decision 映射）。

5. **运行时测试**（需模型环境，否则记为待本地验证）：`go run ./cmd/dsl validate --runtime-checks /tmp/urgent.dsl`，确认两条 TEST 断言都被实际管线满足。

6. **反编译往返**：`go run ./cmd/dsl decompile -o /tmp/urgent.back.dsl /tmp/urgent.yaml`，对照 `/tmp/urgent.fmt.dsl` 与反编译结果，体会 u7-l2 所说的「语义可逆、文本不逐字相同」。

**验收标准**：

- 步骤 2 退出码 0。
- 步骤 4 产出的 YAML 中能找到与你两条 ROUTE 对应的 decision。
- 步骤 6 反编译回的 DSL 经 `validate` 仍合法。

## 6. 本讲小结

- `sr-dsl` 是单一二进制多子命令工具，入口 `main.go` 用手写 `switch` 分发 `compile/decompile/validate/fmt/generate`；命令行壳只做参数解析与 IO，真正逻辑都在 `pkg/dsl` 库函数（`CLICompile`/`CLIValidateWithRunner` 等）里。
- `compile` 通过 `--format yaml|crd|helm` 切换三种产物，`--base` 可把编译出的 routing 叠加到含 providers 的基线 YAML 上产出完整配置；`compile --format crd` 直达 u12-l3 的 Operator CRD。
- `validate` 有两层：**静态校验**（`checkTestBlocks`，查路由名存在/查询非空）总开；**运行时校验**（`--runtime-checks`）用 `buildNativeTestBlockRunner` 造真实分类器，真跑查询比对路由，并附投影分区质心健康检查。静态不通过即跳过运行时。
- `TEST` 块是「`"查询" -> 期望路由`」的可执行文档，由 `TestBlockRunner.EvaluateTestBlockQuery` 执行，复用 u8 的分类+决策整条管线；运行时层只评估「查询非空且路由名存在」的条目，与静态层互补。
- `generate` 子命令走 schema-as-supervision：用 OpenAI 兼容端点把自然语言生成 DSL，带解析失败的修复循环（`Attempts`/`ParseError`/`Warnings`），生成后应立即 `validate` 闭环。
- 推荐工作流：`fmt`（规范）→ `validate`（静态）→ `compile`（出 YAML/CRD）→ `validate --runtime-checks`（跑 TEST）；`decompile` 提供 YAML→DSL 往返。

## 7. 下一步学习建议

- **向下游（运行时）走**：本讲的 `buildNativeTestBlockRunner` 复用了 `classification.Classifier`。要理解一条 TEST 查询具体怎么被抽成信号、怎么进入决策引擎，请进入 **u8 分类信号系统**（尤其 u8-l1 分类编排）。
- **向部署走**：`compile --format crd` 产出的 CRD 如何被 Operator 调谐成实际部署，见 **u12-l3 Operator 与 CRD**。
- **向工具链走**：`decompile`/`fmt` 与面板的可视化编辑器（`BuilderPage`/`DslEditorPage`）共享同一套编译/反编译能力，见 **u13-l3 可视化配置与 DSL 编辑器**。
- **如果想深入 LLM 生成**：`generate` 背后的 prompt 构造与修复循环在 `internal/nlgen` 包，可自行阅读 `GenerateFromNL` 与 `BuildNLPrompt`，作为「用形式语法监督 LLM 生成」的工程范例。
