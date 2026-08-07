# 模型运行时、库存与定价

## 1. 本讲目标

上一讲（u6-l2）我们看清了「决策命中之后，从候选模型里挑哪一个」的选择算法注册表。但要把选择真正跑起来，路由器在**启动时**必须先把本地的嵌入/分类小模型装进内存，在**运行时**必须知道有哪些外部大模型可用，在**计费时**必须能根据后端上报的 token 用量算出成本。本讲就补齐这三块「选择算法的脚手架」：

- 理解 `modelruntime` 如何在启动期并行初始化嵌入/BERT/多模态/模态分类等本地模型，并产出 `EmbeddingRuntimeState` 就绪状态；
- 理解 `WarmupRouter` 如何在「真就绪」之后对请求路径状态做一次热身加载；
- 理解「外部模型库存」的两层：`modelinventory` 包给出的模型信息 API 类型，以及 `config.ExternalModelConfig` 给出的外部（远端）模型目录；
- 理解 `modelpricing` 如何把 provider 上报的 token 用量归一化成互斥分桶，并按四档单价算出响应成本与输入成本乘数。

学完后，你应该能回答：一次请求路由到某模型后，路由器**为什么**能立刻知道「工具库能不能用」「这次花了多少钱」「下次该不该把候选排得更省」，以及这些能力的代码入口在哪里。

## 2. 前置知识

- **CGO 绑定**：项目用 Rust/C 实现本地推理，再通过 CGO 暴露给 Go。本讲会看到大量 `candle_binding.InitModel(...)` 调用，它们是「把一个本地小模型加载进进程」的入口。详细绑定机制见 u12-l4。
- **嵌入（embedding）**：把文本映射成定长向量，用于语义相似度检索。SR 里它同时服务于分类、语义缓存、记忆、工具检索等多个子系统，因此「嵌入运行时是否就绪」是一个被多方关心的全局状态。
- **provider 上报的 token 用量**：OpenAI 兼容响应里的 `usage` 字段会给出 `prompt_tokens`、`completion_tokens`，以及缓存相关的明细（命中缓存的输入 token、写缓存的 token）。这些是成本计算的原始数据。
- **ExtProc 请求阶段 / 响应阶段**：模型选择发生在请求阶段，成本计算发生在响应阶段（后端返回 usage 之后）。参见 u4-l3、u5-l3。
- **选择算法的多因子打分**：u6-l2 讲过 `Selector` 接口；本讲会看到 `MultiFactorSelector` 等算法如何把「每模型单价」当作一个打分信号。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/semantic-router/pkg/modelruntime/router_runtime.go` | 启动期「嵌入运行时初始化 + 模型热身」的核心：`PrepareRouterRuntime` / `WarmupRouter` 及一批 `*Task` 构造函数。 |
| `src/semantic-router/pkg/modelruntime/executor.go` | 一个带依赖图、并发上限、BestEffort 语义的任务执行器，被 `PrepareRouterRuntime` / `WarmupRouter` 共用。 |
| `src/semantic-router/pkg/modelinventory/types.go` | 模型信息 API 的响应类型（`ModelsInfoResponse` / `ModelInfo` 等），供控制面 `/info/models` 端点使用。 |
| `src/semantic-router/pkg/config/model_config_types.go` | 「外部模型目录」类型 `ExternalModelConfig`、模型定价类型 `ModelPricing`，以及按角色/名字查找外部模型的辅助方法。 |
| `src/semantic-router/pkg/modelpricing/modelpricing.go` | 纯函数的成本计算：`Normalize`（互斥分桶）、`Cost`（总成本）、`InputCostMultiplier`（输入成本乘数）。 |
| `src/semantic-router/pkg/extproc/model_pricing.go` | 适配层：把 ExtProc 内部用量类型与 `config.ModelPricing` 翻译成 `modelpricing` 的入参。 |
| `src/semantic-router/pkg/extproc/processor_res_usage.go` | 响应阶段调用 `modelpricing.Cost` 记录单次成本并打点。 |
| `src/semantic-router/pkg/selection/multi_factor.go` | 多因子选择器：把每模型单价当作打分信号，并按 SLO 上限 `MaxCostPer1M` 剔除昂贵候选。 |

> 说明：讲义规格里把「外部模型库存」的源码标为 `modelinventory/types.go`，但读真实代码会发现——`modelinventory` 包只承载「模型信息 API 的响应类型」，而真正描述「外部（远端）大模型如何接入」的目录类型 `ExternalModelConfig` 住在 `config` 包。本讲把两者都讲清楚，避免你照着规格找错文件。

---

## 4. 核心概念与源码讲解

### 4.1 嵌入运行时初始化（PrepareRouterRuntime）

#### 4.1.1 概念说明

「路由器要不要加载本地小模型」不是开关，而是一张**任务清单**。SR 在启动期可能要同时加载：统一嵌入工厂（qwen3/gemma/mmbert）、BERT（给语义缓存/向量库/记忆用）、多模态嵌入（给图像路由用）、模态分类器（判断请求是文本还是图）。

`PrepareRouterRuntime` 就是这张清单的编排者：它根据配置决定**该加载哪些模型**、把它们打包成一批可并发的 `Task`、交给执行器跑，最后把「加载结果」浓缩成一个轻量的 `EmbeddingRuntimeState` 返回给 `main.go`，供启动状态机（u4-l1）和 `/info/models` 端点消费。

关键设计：**嵌入运行时可能「故意不可用」**——比如配了远端嵌入后端、或本机没装模型。因此就绪状态必须是「显式」的（哪些 ready、哪些被跳过及原因），而不是一个布尔。

#### 4.1.2 核心流程

```text
PrepareRouterRuntime(ctx, cfg, options)
  │
  ├─ resolveEmbeddingPaths(cfg)            # 解析 5 个候选模型路径（qwen3/gemma/mmbert/multimodal/bert）
  │
  ├─ embeddingRuntimeTasks(cfg, paths)     # 决定「嵌入」这一类要跑哪些任务，并建一个 state tracker
  │     ├─ 远端后端  → remoteEmbeddingRuntimeTask（探测远端 provider）
  │     ├─ 无任何模型 → 返回空（嵌入不参与本进程）
  │     └─ 本地模型  → buildEmbeddingRuntimeTasks（unified / bert / multimodal 三组）
  │
  ├─ 追加子系统专属任务：
  │     ├─ semanticCacheBERTTask           # 语义缓存需要 BERT 时
  │     ├─ vectorStoreBERTTask             # 向量库需要 BERT 时
  │     ├─ memoryBERTTask                  # 记忆需要 BERT 时（且与上面去重）
  │     └─ modalityClassifierTask          # 模态分类器（classifier/hybrid 方法）
  │
  ├─ Execute(ctx, tasks, Options{...})     # 并发跑（带依赖图、BestEffort、panic 自愈）
  │
  └─ state = tracker.snapshot()            # 汇总就绪状态，返回 EmbeddingRuntimeState
```

任务执行期间的**状态写入**不是直接的——每个任务拿到一个共享的 `embeddingStateTracker`，成功后调用 `markAnyReady()` 或 `markToolsReady()` 改写状态，由互斥锁保护。这样并发任务可以安全地各自更新「自己负责的那格」就绪位。

#### 4.1.3 源码精读

返回类型 `EmbeddingRuntimeState` 只有三个字段，却是启动状态机的核心信号：

[router_runtime.go:17-21](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelruntime/router_runtime.go#L17-L21) — `AnyReady` 表示「至少有一个嵌入能力装好了」；`ToolsReady` 表示「工具库依赖的统一嵌入工厂装好了」；`EmbeddingProvider` 在用远端后端时记录探测结果。

入口函数 `PrepareRouterRuntime` 把「构造任务 → 执行 → 汇总」串起来：

[router_runtime.go:75-115](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelruntime/router_runtime.go#L75-L115) — 注意第 96 行 `if len(tasks) == 0 { return state, nil }`：若配置里既没本地模型也没远端后端，直接返回空状态，不报错（嵌入是可选的）。

任务分流的关键在 `embeddingRuntimeTasks`：

[router_runtime.go:173-193](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelruntime/router_runtime.go#L173-L193) — 远端后端走 `remoteEmbeddingRuntimeTask`；本地有模型时走 `buildEmbeddingRuntimeTasks`；都没有则返回 `(空state, nil, nil)`。

`AnyReady` 与 `ToolsReady` 的语义差异藏在两个 setter 里：

[router_runtime.go:546-557](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelruntime/router_runtime.go#L546-L557) — `markAnyReady` 只置 `AnyReady`；`markToolsReady` 同时置 `AnyReady` 和 `ToolsReady`（工具就绪蕴含至少有一个嵌入就绪）。

那么谁调谁？看三组本地任务：

[router_runtime.go:715-741](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelruntime/router_runtime.go#L715-L741) — 统一嵌入工厂初始化成功后：**若工具用多模态嵌入**则只 `markAnyReady`（因为工具链要的是多模态，统一工厂好了不够）；否则 `markToolsReady`（工具链能用统一工厂了）。

[router_runtime.go:743-764](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelruntime/router_runtime.go#L743-L764) — BERT 嵌入初始化成功只 `markAnyReady`（BERT 服务于缓存/记忆，**不**驱动工具库）。

[router_runtime.go:766-792](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelruntime/router_runtime.go#L766-L792) — 多模态嵌入初始化成功：**若工具用多模态嵌入**则 `markToolsReady`（工具链能用了）；否则只 `markAnyReady`。

远端后端探测成功则直接 `markToolsReady`：

[router_runtime.go:704-710](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelruntime/router_runtime.go#L704-L710) — 远端 provider 能 embed 即视作工具链可用。

> 一句话规律：**`ToolsReady` 专门表示「工具库 / ML 选择依赖的那条嵌入通路是否就绪」**；`AnyReady` 是更宽的「至少装好了一个嵌入模型」。出现 `AnyReady=true, ToolsReady=false`（典型场景：只装了 BERT，统一工厂失败）时，工具库会被降级禁用，对应日志 `embedding_runtime_degraded { tools_database_disabled: true }`（见 [router_runtime.go:364-368](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelruntime/router_runtime.go#L364-L368)）。

#### 4.1.4 代码实践

1. **实践目标**：把 `PrepareRouterRuntime` 的任务分流读成一张表，理解 `AnyReady` / `ToolsReady` 在不同配置下的取值。
2. **操作步骤**：
   - 打开 `router_runtime.go`，定位 `embeddingRuntimeTasks`（L173）与三个本地任务构造函数（unified L715、bert L743、multimodal L766）。
   - 对照下表填写「成功后调用的 setter」：

     | 配置场景 | unified 任务 | bert 任务 | multimodal 任务 | 最终 AnyReady | 最终 ToolsReady |
     | --- | --- | --- | --- | --- | --- |
     | 只配 qwen3，工具 model_type=text | markToolsReady | — | — | true | true |
     | 只配 bert（缓存用） | — | markAnyReady | — | true | false |
     | 配 qwen3 + multimodal，工具 model_type=multimodal | markAnyReady | — | markToolsReady | true | true |
     | 远端嵌入后端 | remote 探测→markToolsReady | — | — | true | true |

   - 用 `grep -n "markAnyReady\|markToolsReady" router_runtime.go` 验证你的判断与源码一致。
3. **需要观察的现象**：`markToolsReady` 一定会同时让 `AnyReady` 变 true（因为函数体里先置 `AnyReady=true`）；反向不成立。
4. **预期结果**：你能用一句话向同伴解释「为什么只装 BERT 时工具库不可用」——因为 BERT 任务只调 `markAnyReady`，从不调 `markToolsReady`。
5. 运行结果：待本地验证（本实践为源码阅读型，无需运行）。

#### 4.1.5 小练习与答案

**练习 1**：如果 `initializeUnifiedEmbeddingModels` 失败（比如模型文件损坏），`ToolsReady` 会是 true 吗？
**答**：不会。失败时该任务返回 error（[router_runtime.go:731](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelruntime/router_runtime.go#L731)），不会执行到 `markToolsReady`；又因任务标了 `BestEffort: true`（L728），失败不会让整个启动 fatal，而是留下 `ToolsReady=false` 的降级状态。

**练习 2**：为什么 `memoryBERTTask` 在语义缓存或向量库也需要 BERT 时返回 `nil`（不重复加载）？
**答**：见 [router_runtime.go:251-254](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelruntime/router_runtime.go#L251-L254)。同一个 BERT 模型只需加载一次，由 `semanticCacheBERTTask` / `vectorStoreBERTTask` 先注册的任务负责，记忆任务避免重复初始化。

---

### 4.2 模型热身（WarmupRouter）

#### 4.2.1 概念说明

`PrepareRouterRuntime` 只负责「把模型装进内存」。但有些**请求路径状态**——比如工具库要预嵌入一遍、知识库要建索引——必须在「模型装好之后、对外报就绪之前」再跑一次。`WarmupRouter` 就是这第二步。

它和 `PrepareRouterRuntime` 共用同一个执行器（`Execute`），但语义更克制：热身任务几乎都标 `BestEffort: true`，失败不影响启动，并且每个任务要求调用方**显式声明 `Ready` 与 `SkipReason`**——因为有些热身依赖一个「可能故意不可用」的嵌入运行时，跳过是预期内的，必须给出原因，而不是静默丢弃。

#### 4.2.2 核心流程

```text
WarmupRouter(ctx, []RouterWarmupTask, options)
  │
  ├─ for 每个 warmup：
  │     ├─ Ready=false → 打 "<name>_load_skipped { reason }" 日志，跳过
  │     └─ Load=nil    → 跳过（占位任务）
  │
  ├─ 把 Ready 且有 Load 的任务包成 Task{ Name:"router.warmup.<name>", BestEffort:true }
  │
  ├─ Execute(...)   # 并发跑（同样有 panic 自愈）
  │
  └─ 逐个核对结果：成功的打 "<name>_loaded" 日志
```

#### 4.2.3 源码精读

热身任务的描述结构，注释点明了「显式就绪」的原因：

[router_runtime.go:48-56](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelruntime/router_runtime.go#L48-L56) — `Ready` / `SkipReason` / `Load` 三件套：只有 `Ready==true` 且 `Load!=nil` 才会真的执行。

`WarmupRouter` 主体：

[router_runtime.go:117-171](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelruntime/router_runtime.go#L117-L171) — 注意第 141 行每个任务都被设 `BestEffort: true`，第 164-169 行只在 `TaskSucceeded` 时打「已加载」日志。

两个函数共用的执行器是一个小型 DAG 调度器：

[executor.go:86-115](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelruntime/executor.go#L86-L115) — `Execute` 构建 `taskState`、找出无依赖的初始就绪任务、并发调度。并发度由 `DefaultParallelism` 给默认值（CPU 数与任务数的较小者，见 [executor.go:76-84](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelruntime/executor.go#L76-L84)）。

执行器有两个对「线上安全」至关重要的细节：

1. **panic 自愈**：初始化代码会调进 CGO 绑定，且每次配置热重载都会重跑。任务 goroutine 里 `defer recover()`（[executor.go:232-242](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelruntime/executor.go#L232-L242)）保证一个 BestEffort 任务的 panic 不会拖垮整个路由器进程，并且 `resultCh` 在 panic 路径上也会被写入，避免 `execute()` 永久阻塞。
2. **BestEffort 隔离**：非 BestEffort 任务失败会记 `firstErr` 并 `cancel()` 整批（[executor.go:257-277](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelruntime/executor.go#L257-L277)）；BestEffort 任务失败只记日志、不取消其他任务。这就是「嵌入可选」能在执行器层面落地的根本机制。

#### 4.2.4 代码实践

1. **实践目标**：用一条热身任务的视角，理解「显式就绪」如何避免静默错误。
2. **操作步骤**：
   - 在 `router_runtime.go` 阅读 `WarmupRouter`（L117）与 `RouterWarmupTask`（L48）。
   - 在 `main.go`（或 `runtime_bootstrap.go`）里 `grep -n "WarmupRouter"`，找到调用处，看它构造了哪些 `RouterWarmupTask`、各自依赖 `EmbeddingRuntimeState` 的哪个字段来决定 `Ready`。
3. **需要观察的现象**：热身任务的 `Ready` 字段通常派生自 `state.ToolsReady`（如工具库热身）或 `state.AnyReady`（如缓存热身）。
4. **预期结果**：你能说清「为什么热身要分两步」——第一步装模型（可能失败但 BestEffort），第二步依赖第一步的结果决定要不要跑、跑哪几个。
5. 运行结果：待本地验证（源码阅读型实践）。

#### 4.2.5 小练习与答案

**练习 1**：假如一个热身任务的 `Load` panic 了，`WarmupRouter` 会返回 error 吗？
**答**：不会。任务 goroutine 的 `defer recover()` 把 panic 转成 error（[executor.go:233-240](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelruntime/executor.go#L233-L240)），又因为热身任务一律 `BestEffort: true`，`failTask` 不会设置 `firstErr`，故 `WarmupRouter` 返回 `nil`（只是该任务在 `summary.Results` 里记为 failed）。

**练习 2**：为什么热身任务也复用 `Execute`，而不是简单 `for` 顺序跑？
**答**：因为热身项之间可能互相独立（工具库、知识库、记忆各管各的），并发跑能缩短「启动到就绪」的时间；且复用 `Execute` 能白拿依赖图、并发上限与 panic 自愈，不用重写。

---

### 4.3 外部模型库存

#### 4.3.1 概念说明

「模型库存」在本项目里有两层，初学者容易混：

1. **本地小模型的「装载清单」**——上一节的 `modelruntime` 任务清单，回答「进程里装了哪些嵌入/分类模型」。对外通过控制面 `/info/models` 端点暴露，响应类型住在 `modelinventory` 包。
2. **外部大模型的「接入目录」**——回答「路由器可以把请求转发给哪些远端 LLM、用什么端点、什么角色」。这个目录类型是 `config.ExternalModelConfig`，住在 `config` 包。

后者尤其重要：SR 的某些分类器（越狱、偏好、通用规则分类）可以**不在本地跑小模型，而是调用一个外部 LLM** 来判定。`ExternalModelConfig` 就是描述「这台外部 LLM 在哪、是什么角色、怎么调」的目录项。

#### 4.3.2 核心流程

```text
配置层：ExternalModels: []ExternalModelConfig{ {Role:"guardrail", Endpoint:{...}}, {Role:"preference",...} }
                                        │
           ┌────────────────────────────┴────────────────────────────┐
           ▼                                                          ▼
  FindExternalModelByRole("guardrail")                    FindExternalModelByName("xxx")
           │                                                          │
           ▼                                                          ▼
  分类器初始化时按角色取出（如越狱、偏好）               通用规则分类按名字取一条外部模型
           │
           ▼
  分类结果 → 信号（jailbreak / preference / ...）→ 决策引擎（u6-l1）

控制面层：GET /info/models
           ▼
  apiserver.buildModelsInfoResponse()
           ▼
  组装 modelinventory.ModelsInfoResponse { Models[], Summary, System } → JSON
```

#### 4.3.3 源码精读

外部模型目录类型，注意它面向的是「外部 LLM」而非本地嵌入：

[model_config_types.go:193-205](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/model_config_types.go#L193-L205) — `Provider`（llm_provider）、`ModelRole`（角色，如 guardrail/preference）、`ModelEndpoint`（vLLM 兼容端点）、`Threshold`、`AccessKey`（标了 `json:"-"` 不外泄）等。

按角色查找是分类器初始化的标准动作：

[model_config_types.go:377-384](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/model_config_types.go#L377-L384) — `FindExternalModelByRole` 线性扫描 `ExternalModels`。真实消费者例如 `classifier_jailbreak_init.go` 用 `ModelRoleGuardrail`、`classifier_preference_lifecycle.go` 用 `ModelRolePreference`、`generic_classifier.go` 用 `FindExternalModelByName` 按规则指定的名字取模型。

本地模型的「装载清单」对外形态——`modelinventory` 包只定义 API 响应类型：

[modelinventory/types.go:6-10](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelinventory/types.go#L6-L10) — `ModelsInfoResponse` 含 `Models` 列表、`Summary` 汇总、`System` 系统信息。

[modelinventory/types.go:13-22](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelinventory/types.go#L13-L22) — `ModelsInfoSummary` 里的 `Ready` / `Phase` / `DownloadingModel` / `PendingModels` / `LoadedModels` / `TotalModels` 正是给启动状态机（u4-l1）「模型下载进度」用的字段。

[modelinventory/types.go:25-37](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelinventory/types.go#L25-L37) — `ModelInfo` 描述单个已加载模型：`Name`、`Type`、`Loaded`、`State`、`Categories`、`Registry`（指向 `config.ModelRegistryInfo`）等。

控制面把这些类型组装成响应：

[route_model_info.go:9-12](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/apiserver/route_model_info.go#L9-L12) — `handleModelsInfo` 处理 `GET /info/models`，直接返回 `buildModelsInfoResponse()`。

[route_model_info.go:53-70](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/apiserver/route_model_info.go#L53-L70) — 把「分类器模型信息 + 嵌入模型信息」拼进 `Models`，再带上 `Summary` 和 `System`。其中 `runtimeState` 正是来自 4.1 节 `PrepareRouterRuntime` 产出的就绪状态——库存端点因此能如实反映「现在到底装好了几个模型」。

#### 4.3.4 代码实践

1. **实践目标**：区分两类「模型库存」，并找到外部模型如何驱动一个分类器。
2. **操作步骤**：
   - 在仓库根执行 `grep -rn "FindExternalModelByRole" src/semantic-router/pkg/classification/`，列出每个调用点用的是什么角色（`ModelRoleGuardrail` / `ModelRolePreference`）。
   - 打开 `config/config.yaml`，搜索 `external_models:`，看一条 `ExternalModelConfig` 实际长什么样（注意 `llm_provider`、`model_role`、`llm_endpoint`）。
   - 对照 `modelinventory/types.go` 的 `ModelInfo`，理解 `/info/models` 返回的是「本地已装载模型」而非「外部 LLM 目录」。
3. **需要观察的现象**：外部模型目录项的 `model_role` 与分类器代码里 `FindExternalModelByRole(config.ModelRoleXxx)` 的常量一一对应。
4. **预期结果**：你能说清「想给越狱检测换一个外部 LLM 后端，该改配置里的哪条 `external_models`」。
5. 运行结果：待本地验证（源码阅读 + 配置查看型实践）。

#### 4.3.5 小练习与答案

**练习 1**：`ExternalModelConfig` 的 `AccessKey` 字段为什么带 `json:"-"` tag？
**答**：防止它被序列化进 API 响应或日志（[model_config_types.go:202](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/model_config_types.go#L202)）。这是密钥不外泄的护栏，和 apiserver 的 secret 脱敏策略一致。

**练习 2**：`/info/models` 的 `Summary.DownloadingModel` 字段由谁填充？
**答**：由启动状态机在「下载模型」阶段写入，再经 `buildModelsInfoSummary` 透传到响应（见 [modelinventory/types.go:17](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelinventory/types.go#L17)）。它对应 u4-l1 讲过的「模型下载进度上报」。

---

### 4.4 成本定价（modelpricing）

#### 4.4.1 概念说明

`modelpricing` 是一个**纯函数包**——无状态、无 IO，只做两件事：

1. 把 provider 上报的、可能重叠或越界的 token 明细，**归一化成互斥分桶**；
2. 按各桶的「每百万 token 单价」算出**总成本**，或算出一个 0~1 的**输入成本乘数**。

它不读配置、不发请求。所有「每模型单价」由调用方（ExtProc 的适配层）从 `config.ModelPricing` 取来再传入。这种「纯计算 + 外部喂参数」的设计让它极易单测（包里就有一份 `modelpricing_test.go`）。

为什么要把 token 分桶？因为现代 provider 的 `prompt_tokens` 实际上**包含了**缓存命中和写缓存两部分，而这三部分单价不同（缓存命中通常更便宜、写缓存可能更贵）。如果直接 `prompt_tokens × prompt单价`，会把便宜的部分按贵的算，成本严重高估。

#### 4.4.2 核心流程与数学

归一化把重叠的明细切成三个互斥的输入桶（缓存命中优先，写缓存吃掉剩余输入）：

\[
\begin{aligned}
P &= \max(\text{PromptTokens}, 0) \\
C_d &= \mathrm{clamp}(\text{CachedInputTokens},\ 0,\ P) \\
W &= \mathrm{clamp}(\text{CacheWriteTokens},\ 0,\ P - C_d) \\
S &= P - C_d - W \quad\text{(标准输入)}
\end{aligned}
\]

于是桶满足 \(S + C_d + W = P\)，互斥不重叠。

总成本（货币单位由 `Rates.Currency` 决定）：

\[
\text{Cost} = \frac{S\cdot p + C_d\cdot c + W\cdot w + O\cdot o}{10^6}
\]

其中 \(p,c,w,o\) 分别是标准输入、缓存命中、写缓存、输出的每百万 token 单价；\(O\) 是 completion tokens。

**输入成本乘数** `InputCostMultiplier` 把「本次实际输入成本」对照「最贵输入单价」归一化到 \([0,1]\)：

\[
M = \frac{S\cdot p + C_d\cdot c + W\cdot w}{P \cdot \max(p,c,w)}
\]

它衡量「这次请求的输入相对该模型最贵输入档打了多少折」——全是缓存命中时趋近 \(c/\max\)（很便宜），全是写缓存且写缓存最贵时为 1。这个乘数后面会被路由器学习（router learning）用来在线感知「某个模型在我这儿的真实输入开销」。

#### 4.4.3 源码精读

四档单价与「是否配置过」的判断：

[modelpricing.go:7-22](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelpricing/modelpricing.go#L7-L22) — `Rates` 含 `PromptPer1M` / `CachedInputPer1M` / `CompletionPer1M`，以及可空的 `CacheWritePer1M`（指针，区分「没配」与「配成 0」）。

写缓存单价的兜底——没单独配时退回普通输入单价（兼容老配置）：

[modelpricing.go:26-31](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelpricing/modelpricing.go#L26-L31) — 这就是为什么 `CacheWritePer1M` 用指针：`nil` 走兜底，显式 `0.0` 表示「写缓存免费」。

归一化（上面公式的代码实现）：

[modelpricing.go:54-66](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelpricing/modelpricing.go#L54-L66) — `clampInt` 保证缓存明细不越界、不超 prompt 总数；`StandardInputTokens` 是相减得到的剩余。

总成本：

[modelpricing.go:69-75](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelpricing/modelpricing.go#L69-L75) — 四桶各自乘单价、相加、除以一百万。

输入成本乘数：

[modelpricing.go:80-98](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelpricing/modelpricing.go#L80-L98) — 第 90 行 `maxInputRate==0` 时返回 0（没配单价的模型不参与成本感知）。

调用方适配层（把 ExtProc 内部类型翻译成 `modelpricing` 入参）：

[model_pricing.go:36-42](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/model_pricing.go#L36-L42) — `costForResponseUsage` 与 `effectiveCacheWriteRate` 是 ExtProc 调 `modelpricing` 的两个薄封装。

响应阶段真正算成本并打点：

[processor_res_usage.go:178-218](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_usage.go#L178-L218) — `recordResponseCost` 用 `r.Config.GetFullModelPricing(ctx.RequestModel)` 取该模型的四档单价，调 `costForResponseUsage` 得到 `costAmount`，写入 Prometheus 指标 `RecordModelCost` 与 `llm_usage` 日志；没配价时记 `cost:0, pricing:"not_configured"`。

> 成本计算发生在**响应阶段**（要等后端回 usage），不在选择阶段。它对候选排序的影响是**间接但持续**的，见 4.4.4。

#### 4.4.4 代码实践

1. **实践目标**：用测试用例验证成本公式，并追踪 `InputCostMultiplier` 如何回流影响排序。
2. **操作步骤**：
   - 读 `modelpricing_test.go` 的 `TestCostUsesDistinctInputRates`（[L39-55](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelpricing/modelpricing_test.go#L39-L55)）：手算 `(500×5 + 200×0.5 + 300×6.25 + 100×30)/1e6`，与 `want` 对照。
   - 读 `TestCostPreservesExplicitFreeCacheWrites`（[L67-76](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelpricing/modelpricing_test.go#L67-L76)）：理解为什么 `CacheWritePer1M=&0.0` 时全写缓存的成本是 0，而 `nil` 时会退回 prompt 单价。
   - 在 `pkg/extproc` 执行 `grep -n "InputCostMultiplier" router_learning_*.go`，跟踪乘数如何进入 `routerLearningTelemetryObservation`（[router_learning_telemetry.go:45-58](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router_learning_telemetry.go#L45-L58)）。
3. **需要观察的现象**：`learningInputCostMultiplier`（[router_learning_telemetry.go:91-100](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router_learning_telemetry.go#L91-L100)）当 `PromptPer1M<=0` 时直接返回 0——没配单价的模型对成本感知「透明」。
4. **预期结果**：你能讲清「成本从响应阶段产生 → 经 InputCostMultiplier 折成 0~1 → 进入 EWMA → 影响下一轮排序」这条回流路径。
5. 运行结果：可直接 `go test ./pkg/modelpricing/` 验证公式（待本地确认 Go 环境）。

#### 4.4.5 小练习与答案

**练习 1**：provider 上报 `prompt_tokens=100`，但 `cached_input_tokens=80`、`cache_write_tokens=70`（明细加起来超过 prompt）。`Normalize` 会怎么处理？
**答**：缓存命中 clamp 到 80，写缓存只能吃掉剩余 `100-80=20`（不是 70），标准输入为 0。见 `TestNormalizeClampsOverreportedDetails`（[modelpricing_test.go:27-37](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelpricing/modelpricing_test.go#L27-L37)）。缓存命中优先于写缓存。

**练习 2**：为什么用「最贵输入单价」做分母来算 `InputCostMultiplier`，而不是用 prompt 单价？
**答**：因为写缓存可能比普通输入更贵（`cacheWriteRate` 可能最大）。用三者最大值做分母，才能保证结果落在 \([0,1]\)，即使「全是写缓存」也只是 =1 而不溢出。测试 `TestInputCostMultiplierAccountsForPremiumCacheWrites` 验证了这一点（[modelpricing_test.go:78-88](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelpricing/modelpricing_test.go#L78-L88)）。

---

## 5. 综合实践

> 对应规格里的实践任务：描述 `EmbeddingRuntimeState` 中 `AnyReady` / `ToolsReady` 的含义，并说明 `modelpricing` 在一次选择中可能如何影响候选排序。

### 任务：画出「启动→请求→计费→再选择」的成本闭环

请按以下步骤，把本讲四个模块串成一条闭环，产出一份一页笔记：

1. **启动侧（modelruntime）**：写下一台 SR 进程在「只配了 BERT 做语义缓存、没配统一嵌入、没配远端」时的 `EmbeddingRuntimeState`——`AnyReady` 与 `ToolsReady` 各是多少？为什么工具库会被禁用？引用 [router_runtime.go:743-764](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelruntime/router_runtime.go#L743-L764) 作证据。
2. **库存侧（inventory/config）**：到 `config/config.yaml` 里找一条带 `pricing:` 的 provider 模型，记下它的四档单价；再到 `external_models:` 找一条外部模型，记下它的 `model_role`。说明前者会被 `modelpricing` 消费、后者会被某个分类器按角色消费。
3. **请求侧（选择）**：读 `selection/multi_factor.go` 的 `gatherSignals`（[L218-240](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/multi_factor.go#L218-L240)）与 `exceedsSLO`（[L266-290](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/multi_factor.go#L266-L290)）。回答：当一条决策配了 `slo.max_cost_per_1m` 时，单价超过该上限的候选会怎样？（提示：被剔除；若全被剔除，默认策略是选最便宜的，见 `cheapestCandidate` [L305-319](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/selection/multi_factor.go#L305-L319)）。
4. **计费侧（modelpricing）**：这次请求命中的模型回了 usage，`recordResponseCost`（[processor_res_usage.go:178-218](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_usage.go#L178-L218)）算出 `costAmount`，同时 `observeRouterLearningUsageTelemetry` 算出 `InputCostMultiplier`（[router_learning_telemetry.go:45-58](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router_learning_telemetry.go#L45-L58)）。
5. **回流侧**：`InputCostMultiplier` 进入 router learning 的 EWMA（指数移动平均），在后续自适应打分里以一个小权重（如 `0.03*clamp01(InputCostMultiplierEWMA)`）影响候选——于是「历史上输入偏贵的模型会被慢慢往下压」。

**交付物**：一张包含 5 个箭头的闭环图，以及一段话回答「为什么 SR 不在选模型时实时算成本，而要用 EWMA 慢慢学？」（提示：选择在请求阶段没有本次 usage，只有历史观测；且 EWMA 抗单次抖动。）

> 本实践为源码阅读 + 配置观察型，无需真实推理后端即可完成。若本地有 Go 环境，可额外 `go test ./pkg/modelpricing/ ./pkg/modelruntime/` 验证两个包的行为。

## 6. 本讲小结

- `PrepareRouterRuntime` 是一张「本地模型装载任务清单」的编排者，产出三字段的 `EmbeddingRuntimeState`；`AnyReady` 表示「至少装好一个嵌入模型」，`ToolsReady` 专指「工具库/ML 选择依赖的统一嵌入通路就绪」，后者蕴含前者。
- `WarmupRouter` 在模型装好后做请求路径状态热身，任务一律 `BestEffort`，且要求显式声明 `Ready` / `SkipReason`，避免「嵌入故意不可用」时静默丢任务；它与 `PrepareRouterRuntime` 共用带依赖图、并发上限与 panic 自愈的执行器 `Execute`。
- 「模型库存」分两层：`modelinventory` 包是 `/info/models` 的响应类型（反映本地已装载模型与下载进度），`config.ExternalModelConfig` 是外部 LLM 接入目录，分类器按 `model_role` / 名字取出使用。
- `modelpricing` 是纯函数包：`Normalize` 把重叠的 token 明细切成互斥三输入桶（缓存命中优先），`Cost` 按四档单价算总成本，`InputCostMultiplier` 把输入成本归一化到 \([0,1]\)；写缓存单价用指针区分「未配置（退回 prompt 单价）」与「免费（0）」。
- 成本计算发生在响应阶段，对候选排序的影响有两条路：**配置侧**的 `MaxCostPer1M` / `CostWeight` 直接在多因子选择里剔除/压低昂贵模型；**学习侧**的 `InputCostMultiplier` 经 EWMA 慢慢调整自适应打分。

## 7. 下一步学习建议

- **深入 CGO 绑定**：本讲反复出现的 `candle_binding.InitModel` / `InitEmbeddingModels` 背后的 Rust/C 实现见 u12-l4（推理绑定），那是「本地推理能力」的真正源头。
- **可观测性闭环**：`recordResponseCost` 写入的 `RecordModelCost` 指标与 `llm_usage` 日志如何在面板/指标端被消费，见 u11-l4（可观测性）与 u13（管理面板）。
- **router learning 全貌**：`InputCostMultiplierEWMA` 如何参与自适应决策、`0.03` 这类权重从哪来，在 u6-l2 提到的选择算法之上，建议接着读 `pkg/extproc/router_learning_adaptation.go` 与 `pkg/extproc/router_learning_runtime.go`，把「成本→学习→再选择」的回流彻底打通。
- **配置侧成本旋钮**：`CostWeight` / `CostAwareRouting` / `MaxCostPer1M` 的 DSL 写法见 u7（路由 DSL）与 `pkg/config/selection_config.go`，可在 `balance`/`multi-objective` 配方里找到真实例子对照。
