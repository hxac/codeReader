# Recipes：可用的路由配方

## 1. 本讲目标

上一讲（u3-l1）我们拆解了单份 `config.yaml` v0.3 的七大顶层结构，知道了 `listeners / providers / routing / entrypoints / recipes / global` 各自装什么。但仓库里 `config/` 目录下真正“可交付”的，不是一堆零散字段，而是一个个**打包好的配方（recipe）**。

本讲的目标是让你学会“挑配方、读配方、验配方”：

- 知道 `config/recipes/` 下维护了哪几个配方，每个配方解决哪一类**用户结果场景**。
- 掌握每个配方必须遵守的**四文件交付契约**，并理解为什么仓库要用测试强制它。
- 学会用 `vllm-sr validate` 与 `go run ./cmd/dsl validate` 等命令**校验一个配方**，并看懂校验输出。
- 理解 `multi-objective` 这类“多入口配方”是如何用一条 `config.yaml` 同时对外暴露多个互不干扰的路由策略的。

学完本讲，你就能在仓库里找到“最接近自己业务”的那个配方，把它跑起来，或作为自己改写的起点。

## 2. 前置知识

- **配方（recipe）**：一份完整的、可直接运行的路由配置包。你可以把它理解成“路由器的出厂预设方案”。它和 `config.yaml` 的关系是：每个配方的核心就是一份 `config.yaml`，再配上其它辅助文件。
- **用户结果场景（user outcome）**：项目刻意用“业务结果”来命名配方目录（如 `balance`、`privacy`、`knowledge`），而不是用实现细节（如 `SAARS`、`MMLU`、`Router Flow`）来命名。这是因为配方目录是一个面向使用者的目录分类。
- **entrypoint / recipe**（来自 u3-l1）：客户端用虚拟模型名（如 `vllm-sr/mom-balanced-v1`）命中 `entrypoints`，`entrypoints` 再把它指到某个 `recipe`。本讲会看到如何用一个 `config.yaml` 同时声明多个 entrypoint 和多个 recipe。
- **信号 / 投影 / 决策**（来自 u2 系列）：每个配方内部都是“抽取信号 → 投影成路由带 → 决策选出 ROUTE → 分发 MODEL”这条流水线。本讲不深入内部机制，只把它们当成“配方内容”来看。
- **契约测试（contract test）**：用 Go 单元测试来检查“仓库里的资产文件是否符合约定”。本讲会读到一个专门检查配方目录是否“完整且对称”的测试。

如果你对 `config.yaml` 的顶层结构还不熟，建议先回看 u3-l1。

## 3. 本讲源码地图

本讲主要围绕“配方目录本身”和“校验入口”展开，涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `config/recipes/README.md` | 配方目录的“目录页”：列出所有配方、定义四文件交付契约、给出校验命令模板。 |
| `config/recipes/balance/README.md` | `balance` 配方的说明书：设计目标、模型货架、路由优先级表、本地运行方式。 |
| `config/recipes/privacy/README.md` | `privacy` 配方的说明书，本讲用于与 `balance` 做策略对比。 |
| `config/recipes/multi-objective/README.md` | 多入口配方的说明书：用一个 config 暴露多个互不干扰的目标。 |
| `src/semantic-router/pkg/config/maintained_asset_contract_test.go` | Go 契约测试：强制每个配方目录“四文件齐全且对称”，并校验 config 可解析、无遗留字段。 |
| `src/vllm-sr/cli/commands/validate.py` | `vllm-sr validate` 命令的实现：解析 config、跑语义校验、打印配置摘要。 |
| `config/recipes/multi-objective/config.yaml` | 多入口配方的 config，展示 `entrypoints` 与 `recipes[]` 的真实写法。 |
| `config/recipes/balance/probes.yaml` | 配方的探针清单：声明“期望命中哪个 decision / 哪个 alias”，供线上校准。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：**4.1 配方目录**、**4.2 四文件交付契约**、**4.3 配方校验命令**。

### 4.1 配方目录：七个用户结果场景

#### 4.1.1 概念说明

`config/recipes/` 不是“配置示例文件夹”，而是项目**正式维护、可直接交付**的一组完整用例。每个子目录是一个配方，目录名描述它要达成的**用户结果**，而不是它内部用了什么算法。

这一点在目录首页就明确写了：实现名（Router Flow、SAARS、MMLU 等）应当藏在配方自己的文档里，而不该出现在目录分类中。

#### 4.1.2 核心流程

挑选一个配方的心智流程是：

1. 读 `config/recipes/README.md` 的**目录表（Catalog）**，按“我想优化什么”找匹配的用途。
2. 进入对应子目录，读它的 `README.md`，确认它的设计目标、模型货架、路由优先级是否符合预期。
3. 用校验命令（见 4.3）确认它能在你的环境里通过。
4. 用 `vllm-sr serve --config config/recipes/<用例>/config.yaml` 启动它。

仓库目前维护 **7 个配方**。下表把它们和“它解决的结果”对应起来：

| 配方 | 解决的用户结果 |
| --- | --- |
| `accuracy` | 只在“确实有质量收益”的地方才做多模型编排；长上下文保持单模型。 |
| `agent` | 在本地与前沿两条车道之间，调度 agent / 编码 / 专业领域 / 隐私 / 安全这几类工作。 |
| `balance` | 单一默认路由画像下的通用“质量-延迟-成本”平衡。 |
| `feedback` | 把“用户不满意、重复提问、代码失败、要求核查”这类恢复场景变成一等路由策略。 |
| `knowledge` | 用一份带版本的知识库（KB）得分来决定“该不该把问题升级到更大模型”，而不是写死厂商策略。 |
| `multi-objective` | 用一个 config 同时对外暴露平衡、速度、成本、精度、隐私五个**互相隔离**的目标配方。 |
| `privacy` | 把敏感、可疑、隐私上下文的流量留在策略兼容（通常是本地）的模型上。 |

#### 4.1.3 源码精读

配方目录首页用一张表概括了这 7 个配方的用途：

[config/recipes/README.md:L22-L31](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/README.md#L22-L31) — 这就是上面的 Catalog 表的源头：每行一个 `Use case` 链接到该配方的 README，并用一句话说明 `Purpose`。

首页还特别强调了一条边界：`bounded-candidate-iteration.dsl` 这类“只演示 DSL 语法”的文件**不是**可部署配方，它们的行为由 DSL 单元测试覆盖，不混进这个目录。这说明目录里只放“真正可交付”的东西：

[config/recipes/README.md:L33-L35](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/README.md#L33-L35) — 语法演示文件不进入 catalog。

其中 `balance` 被定位为“推荐默认的通用路由画像”。它的 README 开头说明了它在五个模型别名之间如何升级：

[config/recipes/balance/README.md:L14-L24](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/README.md#L14-L24) — `balance` 的 Design Goals：普通流量留在免费本地车道，再按需升级到 flash-lite / pro / gpt5.4 / opus，全部由信号-投影-决策驱动，并在每条路由上用 `router_replay` 记录审计。

> **多入口配方（multi-objective）的设计**：大多数配方是“一个 config = 一个默认路由画像”。`multi-objective` 不一样：它把五条互相隔离的目标（balanced / speed / cost / accuracy / privacy）塞进同一个 config，靠 `entrypoints` 让客户端用不同的虚拟模型名选择不同目标。它的 README 用一张表说明了五个入口：

[config/recipes/multi-objective/README.md:L3-L12](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/multi-objective/README.md#L3-L12) — 五个 `vllm-sr/mom-*-v1` 入口各自映射到一个目标配方。

它的隔离语义也写得很清楚：信号、投影、决策、算法、插件**归某一个 recipe 私有**，不能跨 recipe 匹配；而 provider 绑定、模型卡片、运行时服务是共享基础设施：

[config/recipes/multi-objective/README.md:L14-L16](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/multi-objective/README.md#L14-L16) — 配方隔离边界。

#### 4.1.4 代码实践

**实践：用目录表选配方并阅读它的路由优先级表。**

1. **实践目标**：建立“按用途找配方”的直觉，并读懂一个配方的“路由控制面”。
2. **操作步骤**：
   - 打开 `config/recipes/README.md`，对照上面的 7 项 Catalog 表，假设你的业务是“一个内部企业助手，既要处理代码，又绝不能把含 PII 的内容发到云端”，你会选哪个配方？（提示：可在 `agent` 与 `privacy` 之间权衡。）
   - 进入 `config/recipes/balance/README.md`，找到 **Route Order** 表。
3. **需要观察的现象**：Route Order 表按 `Priority` 从高到低排列，每行给出 `Decision`（决策名）、`Target model`（目标模型别名）、`Reasoning`（推理强度）、`Purpose`（用途）。
4. **预期结果**：你会看到优先级最高的 `premium_legal`（260）胜过 `formal_math_proof`（252），优先级最低的 `casual_chat`（10）是“最终兜底”，且它没有 WHEN 条件。这与 u2-l4 讲过的“末尾保留无 WHEN 的兜底路由”一致。
5. 对应源码：[config/recipes/balance/README.md:L43-L58](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/README.md#L43-L58)。

> 说明：本实践是“源码阅读型实践”，不依赖运行环境。

#### 4.1.5 小练习与答案

**练习 1**：为什么目录用 `balance / privacy / knowledge` 这种名字，而不是 `SAARS / MMLU / RouterFlow`？

> **参考答案**：因为目录分类面向“用户结果场景”，实现细节（如 SAARS 学习、MMLU 数据集、Router Flow 编排）属于配方内部，应当写在配方自己的 README 里，而不是出现在 catalog 分类法中。这让使用者能按业务目标挑配方。

**练习 2**：`multi-objective` 与 `balance` 在“对外暴露的策略数量”上有什么本质区别？

> **参考答案**：`balance` 一个 config 只有一个默认路由画像；`multi-objective` 一个 config 同时声明了 5 个 entrypoint，每个 entrypoint 映射到一个互相隔离的 recipe（balanced / speed / cost / accuracy / privacy），客户端靠请求里的虚拟模型名选择不同目标。

---

### 4.2 四文件交付契约

#### 4.2.1 概念说明

一个配方要“可交付”，光有一份 config 是不够的。项目规定：`config/recipes/` 下的**每个子目录必须恰好包含四个文件**，缺一不可、多一个也不行。这就是“四文件交付契约”：

| 文件 | 角色 |
| --- | --- |
| `config.yaml` | 权威的 v0.3 运行时配置，路由器真正消费的就是它。 |
| `recipe.dsl` | 可读、可评审的路由策略，能编译回同一套动态路由面（与 config 等价）。 |
| `probes.yaml` | 与后端无关的 `/api/v1/eval` 正确性探针（声明“期望命中哪个 decision/alias”）。 |
| `README.md` | 说明用途、路由策略、权衡取舍与校验步骤。 |

为什么要这么严格？因为配方是“交付物”，而不是“草稿”。四件套保证：机器能跑（config）、人能读（README）、策略可评审且可双向转换（dsl）、正确性可校准（probes）。任何一件缺失或漂移，都会让配方失去“可维护”的属性。

#### 4.2.2 核心流程

仓库用两个层面来强制契约：

1. **目录对称性**：枚举 `config/recipes/` 下所有子目录，要求目录集合**恰好**等于白名单；再对每个目录要求文件集合**恰好**等于 `{README.md, config.yaml, probes.yaml, recipe.dsl}`，连嵌套子目录都不允许。
2. **内容有效性**：对每个 `config.yaml`，要求它能被 v0.3 解析器成功解析、且不含任何遗留（legacy）顶层字段；对每个 `probes.yaml`，要求它声明了 `routing_assets` 指向同一目录的 yaml 与 dsl，并且 `decisions` 列表非空。

伪代码描述：

```text
for 目录 d in config/recipes/*:
    assert d 的文件集合 == {README.md, config.yaml, probes.yaml, recipe.dsl}
    assert ParseYAMLBytes(d/config.yaml) 成功
    assert d/config.yaml 不含任何 legacy 顶层键
    assert d/probes.yaml.routing_assets == {yaml: d/config.yaml, dsl: d/recipe.dsl}
    assert d/probes.yaml.decisions 非空
```

#### 4.2.3 源码精读

目录首页以一段文字定义了这个契约，并强调“仓库契约测试会拒绝不完整的目录、无效的 YAML/DSL，以及 YAML 与 DSL 之间的漂移”：

[config/recipes/README.md:L8-L19](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/README.md#L8-L19) — Delivery Contract 的文字定义：每个子目录都有同样的四个文件。

契约的“机器执行版”在 Go 测试里。先看白名单：测试维护了**配方目录名**和**配方文件名**两个列表，作为唯一合法答案：

[src/semantic-router/pkg/config/maintained_asset_contract_test.go:L48-L63](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/maintained_asset_contract_test.go#L48-L63) — `maintainedRecipeNames`（7 个目录）与 `maintainedRecipeFiles`（4 个文件）就是契约的“标准答案”。

测试 `TestMaintainedRecipeDirectoriesAreCompleteAndSymmetric` 先读 `config/recipes/` 目录，要求**目录集合恰好等于白名单**（连根目录下出现 README.md 以外的文件都会报错），再对每个目录调用 `assertRecipeDirectoryContract`：

[src/semantic-router/pkg/config/maintained_asset_contract_test.go:L144-L171](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/maintained_asset_contract_test.go#L144-L171) — 强制目录完整且对称的入口测试。

`assertRecipeDirectoryContract` 则是把上面的伪代码落到 Go：读目录、禁止嵌套子目录、比较文件集合、再校验 config 可解析并检查探针清单：

[src/semantic-router/pkg/config/maintained_asset_contract_test.go:L173-L195](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/maintained_asset_contract_test.go#L173-L195) — 逐目录检查：文件集合必须**精确等于**四个文件，且 config 通过 v0.3 校验。

它还顺带检查探针清单的“资产指针”指向正确：`probes.yaml` 里的 `routing_assets.yaml` 必须等于本目录的 `config.yaml`、`routing_assets.dsl` 必须等于本目录的 `recipe.dsl`，且 `decisions` 列表非空：

[src/semantic-router/pkg/config/maintained_asset_contract_test.go:L197-L211](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/maintained_asset_contract_test.go#L197-L211) — 探针清单的资产指针校验与 decisions 非空校验。

为什么 config 还要额外检查“无遗留字段”？因为项目从旧 schema 迁移到了 v0.3，老配置会把 `signals / decisions / default_model / strategy` 等放在顶层。测试维护了一张遗留键黑名单，凡是顶层出现这些键就判定为“还没迁移到 v0.3 规范”：

[src/semantic-router/pkg/config/maintained_asset_contract_test.go:L262-L294](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/maintained_asset_contract_test.go#L262-L294) — 遗留顶层键黑名单：出现任何一个就 fatal。

`probes.yaml` 的真实样子（以 `balance` 为例）也印证了契约：开头是 `name / description / routing_assets / router_eval_endpoint`，其中 `routing_assets` 精确指向本目录的 yaml 与 dsl，随后是带 `acceptance`（最低通过率）的 `decisions` 列表：

[config/recipes/balance/probes.yaml:L1-L20](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/probes.yaml#L1-L20) — 探针清单头部：`routing_assets` 指向同目录的 config 与 dsl，`acceptance` 要求 100% 通过。

#### 4.2.4 代码实践

**实践：让契约测试替你“读”一个配方目录。**

1. **实践目标**：用仓库自带的契约测试，验证 7 个配方目录都满足四文件契约。
2. **操作步骤**：在仓库根目录执行：
   ```bash
   REPO_ROOT="$(git rev-parse --show-toplevel)"
   cd "$REPO_ROOT/src/semantic-router"
   go test ./pkg/config -run TestMaintainedRecipeDirectoriesAreCompleteAndSymmetric -count=1 -v
   ```
3. **需要观察的现象**：输出里会出现 7 个 `PASS` 子测试（每个配方一个），外加目录枚举本身通过。
4. **预期结果**：测试通过；你会看到 `accuracy / agent / balance / feedback / knowledge / multi-objective / privacy` 七个名字各对应一个通过的子测试。
5. **延伸观察**：如果你临时在某个配方目录里多放一个 `note.txt`，再跑这个测试，它会因为文件集合不再等于四件套而 fatal（**做完记得删除该文件，不要改动仓库资产**）。这一步是“待本地验证”的破坏性观察，建议只读不写。

> 说明：本实践需要本地装有 Go 工具链。若环境无 Go，则改为阅读型实践——逐条比对 `maintained_asset_contract_test.go` 的白名单与 `config/recipes/` 实际目录，确认两者一致。

#### 4.2.5 小练习与答案

**练习 1**：如果你给 `balance` 目录加了一个 `notes.md`，哪个断言会失败？

> **参考答案**：`assertRecipeDirectoryContract` 里比较 `actual`（实际文件名集合）与 `maintainedRecipeFiles`（四件套）的 `reflect.DeepEqual` 会失败，因为实际集合变成了 5 个文件，不再精确等于 `{README.md, config.yaml, probes.yaml, recipe.dsl}`。

**练习 2**：`probes.yaml` 里的 `routing_assets` 字段为什么必须指向“本目录”的 config 与 dsl？

> **参考答案**：它把探针清单和它要校验的路由资产**绑死在同一目录**，防止校准时误用别的配方的 config/dsl，从而保证“这份探针测的就是这个配方”。测试 `assertRecipeProbeManifest` 专门检查这个指针。

---

### 4.3 配方校验命令

#### 4.3.1 概念说明

光有契约测试还不够：契约测试只保证“文件齐全、能解析、无遗留字段”，但不保证“配置语义正确”（比如某个 decision 引用了一个不存在的信号）。语义校验由两条命令分担：

- **`vllm-sr validate --config <config.yaml>`**：Python 侧命令，解析整份 config、跑一组语义校验器、打印配置摘要。这是**最常用**的配方校验入口。
- **`go run ./cmd/dsl validate <recipe.dsl>`** 与 **`go run ./cmd/dsl compile ...`**：Go 侧命令，校验 DSL 合法性，并能把 DSL 编译回 config（验证往返一致性）。

此外还有一条**探针校准**命令 `router_calibration_loop.py eval`，它对一个**在线路由器**发探针，验证端到端“命中了期望的 decision”。本讲聚焦前两条静态校验命令。

#### 4.3.2 核心流程

`vllm-sr validate` 的执行流程可以概括为：

```text
1. parse_user_config(config_path)   # 把 YAML 解析成结构化 UserConfig；解析失败直接退出码 1
2. validate_user_config(user_config) # 跑一组语义校验器，收集 errors
3. 若有 errors：打印错误并退出码 1
4. 否则：打印 "Configuration is valid!" 与配置摘要
        摘要包括 version、listeners、按 profile 聚合的信号/投影计数、
        entrypoints/recipes 数、decisions 总数（默认 vs recipe 私有）、
        插件统计、models 数、默认模型
```

关键点是第 4 步：校验通过后会打印一份**配置摘要**，让你一眼看出这份配方有几个 entrypoint、几个 recipe、多少条决策、用了哪些插件。这对“快速读懂一个陌生配方”非常有用。

#### 4.3.3 源码精读

配方目录首页把三条命令写成模板（把 `<use-case>` 换成具体配方名即可）：

[config/recipes/README.md:L39-L52](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/README.md#L39-L52) — 校验模板：`vllm-sr validate`、`go run ./cmd/dsl validate`、`go run ./cmd/dsl compile`。

`vllm-sr validate` 的实现核心在 `validate_command`：先解析，再校验，最后打印摘要。注意它对“解析失败”和“校验失败”都走 `sys.exit(1)`，让命令可被脚本/CI 判断成败：

[src/vllm-sr/cli/commands/validate.py:L124-L150](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/commands/validate.py#L124-L150) — `validate_command`：解析失败或校验失败都退出码 1，全部通过才继续打印摘要。

通过校验后，它用 `iter_routing_profiles` 遍历所有路由画像（默认画像 + 每个 recipe），聚合统计信号、投影、决策、插件，并区分“默认决策”与“recipe 私有决策”：

[src/vllm-sr/cli/commands/validate.py:L159-L193](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/vllm-sr/cli/commands/validate.py#L159-L193) — 配置摘要：按 profile 聚合信号/投影，统计 entrypoints、recipes、decisions（区分 default 与 recipe-owned）、插件、models。

> **小贴士**：摘要里 `Decisions: N total (X default, Y recipe-owned)` 这一行特别有用——它直接告诉你这份 config 是“单画像”（Y=0，像 `balance`）还是“多画像”（Y>0，像 `multi-objective`）。

多入口配方的 config 真实写法，可以直接在 `multi-objective/config.yaml` 看到：`entrypoints` 用 `model_names` 声明虚拟模型名，`recipe` 指向某个命名 recipe；紧接着 `recipes[]` 用 `name / description / routing` 定义每个目标配方的私有路由面：

[config/recipes/multi-objective/config.yaml:L165-L184](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/multi-objective/config.yaml#L165-L184) — 5 个 entrypoint 映射到 5 个 recipe，每个 recipe 拥有独立的 `routing` 块。对这份 config 跑 `vllm-sr validate`，摘要里 `Recipes: 5`、`recipe-owned` 决策数会明显大于 0。

#### 4.3.4 代码实践

**实践：校验 `balance` 配方，并对比 `balance` 与 `privacy` 的策略差异。**

这是本讲的核心实践，分两步。

**步骤一：运行校验命令**

1. **实践目标**：用 `vllm-sr validate` 校验 `balance`，读懂它的配置摘要。
2. **操作步骤**：在仓库根目录执行（假设你已按 u1-l3/u1-l4 装好 `vllm-sr`）：
   ```bash
   vllm-sr validate --config config/recipes/balance/config.yaml
   ```
3. **需要观察的现象**：先看到 `Validating: config/recipes/balance/config.yaml`；若通过，会看到 `Configuration is valid!` 与一段配置摘要。
4. **预期结果**：摘要应显示 `Version: v0.3`，`Recipes: 0`（balance 只有默认画像，没有命名 recipe），`Decisions: 13 total (13 default, 0 recipe-owned)`（与 README 的“13 个非兜底车道”一致），并能看到若干插件统计（每条路由都开了 `router_replay`）。
5. 如果本地未安装 `vllm-sr`，可改用 Go 侧命令做等价校验（**待本地验证**）：
   ```bash
   (cd src/semantic-router && go run ./cmd/dsl validate ../../config/recipes/balance/recipe.dsl)
   ```

> **说明**：不要假装命令已经跑过。如果你的环境没有 `vllm-sr`，请如实记录“待本地验证”，并改为阅读 `validate.py` 源码，预测摘要里 `Decisions` 一行会显示什么。

**步骤二：对比 balance 与 privacy 的路由策略**

1. **实践目标**：用两个真实配方，体会“同一套信号-投影-决策框架，可以表达截然不同的路由目标”。
2. **操作步骤**：分别阅读 `config/recipes/balance/README.md` 与 `config/recipes/privacy/README.md` 的 **Design Goals / Route Order** 两段。
3. **需要观察的现象**：
   - `balance` 的路由优先级表里，最高优先级是 `premium_legal`（高价值专业分析），目标是“质量-成本-延迟”平衡，普通流量留便宜车道、按需升级。
   - `privacy` 的路由优先级表里，最高优先级是 `local_security_containment`（安全隔离），其次是 `local_privacy_policy`（隐私留在本地），云前沿模型（`cloud_frontier_reasoning`）只服务“既不敏感又确实需要深度推理”的流量。
4. **预期结果**：你能总结出两者的**策略差异**：
   - **主轴不同**：`balance` 以“能力/成本”为主轴升级模型；`privacy` 以“敏感度/安全”为主轴决定是否留在本地。
   - **最贵模型的用途不同**：`balance` 把最贵模型（opus）用于高风险法律分析；`privacy` 把云前沿模型严格限制在“非敏感的深度推理”上，敏感流量优先留在免费本地车道。
   - **共同点**：两者都用 `router_replay` 在每条路由记录审计；两者都强调“策略驱动而非偏好驱动”（不允许调用方凭喜好挑模型）。
5. 对应源码：`balance` 路由表 [config/recipes/balance/README.md:L43-L58](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/README.md#L43-L58)；`privacy` 路由表 [config/recipes/privacy/README.md:L36-L43](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/privacy/README.md#L36-L43)。

#### 4.3.5 小练习与答案

**练习 1**：`vllm-sr validate` 通过后打印的摘要里，`Decisions: 13 total (13 default, 0 recipe-owned)` 说明这份 config 是单画像还是多画像？如果换成 `multi-objective`，这一行会变成什么样？

> **参考答案**：`0 recipe-owned` 说明 `balance` 是单画像（只有顶层默认 routing，没有命名 recipe）。换成 `multi-objective` 后，`Recipes` 会变成 5，`recipe-owned` 决策数会明显大于 0（因为 5 个目标配方各自拥有私有决策）。

**练习 2**：如果你改坏了 `config.yaml` 里某个 decision 引用的信号名（引用了一个不存在的信号），`TestMaintainedRecipeDirectoriesAreCompleteAndSymmetric` 和 `vllm-sr validate` 分别会报错吗？

> **参考答案**：契约测试**大概率不会**报这种语义错误——它只检查“文件齐全、能被 v0.3 解析、无遗留顶层键、探针资产指针正确”，引用一致性不在它的职责内（除非解析阶段就失败）。而 `vllm-sr validate` 里的 `validate_user_config` 会跑语义校验器（如信号引用校验），**会**捕获“decision 引用了不存在的信号”这类错误并退出码 1。所以两者是互补的：契约测试管“形态”，validate 管“语义”。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个“选型 + 校验 + 解读”的小任务：

> **场景**：你所在团队要给一个内部助手做路由。需求是——绝大多数日常问答要省钱（用本地小模型），但当代码出错、用户反复追问、或需要引用权威来源时，要能自动升级到更强的模型；同时所有路由决策都要留审计。

1. **选型**：对照 4.1 的 Catalog 表，在 `balance` 与 `feedback` 之间选一个最贴合的配方作为起点，并用一句话说明理由。
2. **契约自检**：运行 4.2.4 的契约测试，确认你选的配方目录四件套齐全。
3. **语义校验**：运行 `vllm-sr validate --config config/recipes/<你选的配方>/config.yaml`，记录摘要里 `Decisions` 与 `Plugins` 两行。
4. **解读**：打开该配方的 `README.md`，找到它的 Route Order 表，指出“哪一条路由负责‘用户反复追问’”，并说明它升级到了哪个模型、推理强度（reasoning）设成了什么。
5. **产出**：写一段 200 字以内的结论——这个配方在“省钱”和“必要时升级”之间是怎么平衡的。

参考思路：

- 选型上，`feedback` 把“不满意恢复”做成一等公民（含 `lookback_turns` 的持续追问升级），但若“日常问答省钱 + 必要时升级”是主诉求，`balance` 更通用；`feedback` 更适合“以纠错/恢复为核心”的场景。结论里应体现你对优先级表的理解。
- 解读时可参考 `feedback` README 里 `feedback_persistent_recovery`（`lookback_turns: 2`，升级到 `openai/gpt5.4`，reasoning high）这条路由，它正是“同一问题跨两轮仍失败 → 升级到最强模型”的体现。

> 这是一个综合的“源码阅读 + 命令实践”任务；若本地无运行环境，步骤 2/3 可改为“阅读测试与 validate.py，预测输出”，并如实标注“待本地验证”。

## 6. 本讲小结

- `config/recipes/` 维护 **7 个可交付配方**，目录名按“用户结果场景”分类（balance/privacy/knowledge/agent/accuracy/feedback/multi-objective），实现细节藏在各自 README 里。
- 每个配方必须满足**四文件交付契约**：`config.yaml`（运行时权威）、`recipe.dsl`（可评审且可往返）、`probes.yaml`（正确性探针）、`README.md`（说明文档），多一个少一个都不行。
- 契约由 Go 测试 `TestMaintainedRecipeDirectoriesAreCompleteAndSymmetric` 强制：目录集合与文件集合都必须精确等于白名单，且 config 必须能被 v0.3 解析、不含遗留顶层键。
- `probes.yaml` 通过 `routing_assets` 把探针与同目录的 config/dsl 绑死，并声明 `acceptance`（最低通过率）与期望命中的 decision/alias。
- 校验分两层：`vllm-sr validate` 管“语义正确”并打印配置摘要；`go run ./cmd/dsl validate/compile` 管“DSL 合法与往返一致”；线上正确性再由探针校准命令对在线路由器验证。
- `multi-objective` 是特殊的“多入口配方”：一个 config 用 `entrypoints` + `recipes[]` 同时暴露 5 个互相隔离的目标，recipe 私有信号/投影/决策不可跨 recipe 匹配。

## 7. 下一步学习建议

- 想知道 `vllm-sr validate` 背后的“解析 + 加载 + 语义校验”在 Go 内核里是怎么实现的，请进入 **u3-l3 配置加载与校验**，它讲解 `config.Parse/Load`、`validator` 与热替换 `config.Replace`。
- 想读懂某个配方内部的信号、投影、决策具体怎么写，可回看 **u2-l2 / u2-l3 / u2-l4** 三讲，再用 `balance/recipe.dsl` 做对照练习。
- 想了解 DSL 与 config 的双向编译（即 `go run ./cmd/dsl compile` 背后的机制），请进入 **u7（路由 DSL）** 单元，尤其是 u7-l2。
- 想亲手改一个配方，建议从复制 `balance` 目录开始，按四文件契约改写后，依次跑契约测试与 `vllm-sr validate`，把“改写 → 校验”的闭环跑通。
