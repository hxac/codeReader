# 工具检索与 MCP

## 1. 本讲目标

本讲承接 u10-l1（插件链架构与配置），深入 Semantic Router（以下简称 SR）的两条「工具能力」通路：进程内的**语义工具库**与对外的 **MCP（Model Context Protocol）客户端**。

学完后你应当能够：

- 说清 `pkg/tools` 的 `ToolsDatabase` 如何用「查询嵌入点积」从一批 OpenAI 格式工具里检索出 top-k，以及它与 `tool_selection` 插件的关系；
- 描述 `pkg/mcp` 的 `MCPClient` 接口、`ClientFactory` 如何按配置在 stdio / streamable-http 两种传输之间分派，以及 `BaseClient` 如何复用公共能力；
- 解释 `ToolFilter`（allow / block）在哪两层被强制、`FilterTools` 如何实现；
- 区分这两套机制的定位差异：工具库是「为请求**增补/裁剪**工具」，MCP 客户端在当前代码里主要作为**外部分类后端**接入。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，什么是「工具」。** 在 OpenAI 兼容的 Chat Completions 协议里，`tools` 字段是一组函数定义（名字、描述、参数 JSON Schema）。模型在生成时可以决定调用其中某个函数。当工具数量很多时（几十上百个），把全部工具塞给模型既浪费 token 又会干扰选择——所以需要**按当前查询语义检索出最相关的少数工具**。这正是 `ToolsDatabase` 要解决的问题。

**第二，点积即相似度。** SR 的嵌入向量都经过 L2 归一化（这个约定在 u9-l1 HNSW 与 u8-l4 嵌入提供者里已建立）。对两个归一化向量 \(a, b\)，点积等于余弦相似度：

\[
a \cdot b = \lVert a\rVert \lVert b\rVert \cos\theta = \cos\theta \quad (\lVert a\rVert=\lVert b\rVert=1)
\]

所以「点积打分」就是「余弦相似度打分」，值域 \([-1, 1]\)，越大越相似。本讲会反复看到 `dotProduct += queryEmbedding[i] * entry.Embedding[i]` 这种循环，它就是上式的逐维实现。

**第三，什么是 MCP。** Model Context Protocol 是一个基于 JSON-RPC 2.0 的开放协议，用于让「客户端」（这里是 SR）向「服务器」（外部进程或 HTTP 服务）枚举并调用**工具（tools）**、**资源（resources）**、**提示（prompts）**三类能力。MCP 的两种主流传输是：

- **stdio**：SR 作为父进程拉起一个 MCP 服务器子进程，通过它的标准输入/输出收发 JSON-RPC 报文；
- **streamable-http**：通过 HTTP POST 向一个远端端点发送请求。

SR 的 `pkg/mcp` 是一个**纯客户端**实现（只消费别人的工具，不对外暴露工具）。

> 重要定位说明：在当前 HEAD，`pkg/mcp` 客户端**没有**被用来把 MCP 工具注入到请求的 `tools` 字段里；它在代码里的实际接入点是把 MCP 服务器当作一个**类别分类器**后端（`classification.MCPCategoryClassifier`）。本讲会忠实呈现这一点：既讲 MCP 客户端的通用能力，也讲它真正被怎么用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/semantic-router/pkg/tools/tools.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/tools/tools.go) | `ToolsDatabase`：进程内工具库，负责加载、生成嵌入、按点积检索 top-k |
| [src/semantic-router/pkg/tools/retrieval_input.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/tools/retrieval_input.go) | `RetrievalInput`、`EffectivePoolSize()` 候选池启发式、`StrategyDefault` 常量 |
| [src/semantic-router/pkg/tools/retriever.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/tools/retriever.go) | `ToolRetriever` 接口与 `Registry` 策略注册表 |
| [src/semantic-router/pkg/tools/embedding_retriever.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/tools/embedding_retriever.go) | `EmbeddingRetriever`：默认策略，把 `RetrievalInput` 翻译成对 `ToolsDatabase` 的检索 |
| [src/semantic-router/pkg/tools/relevance.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/tools/relevance.go) | 检索后的「高级过滤与加权重排」（嵌入分 + 词法/标签/类别/名称） |
| [src/semantic-router/pkg/mcp/interface.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/interface.go) | `MCPClient` 接口、`BaseClient`、`ClientConfig`/`ToolFilter`/`TransportType` |
| [src/semantic-router/pkg/mcp/factory.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/factory.go) | `ClientFactory`：按传输类型创建 stdio/HTTP 客户端、配置校验 |
| [src/semantic-router/pkg/mcp/stdio_client.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/stdio_client.go) | `StdioClient`：拉起子进程、Initialize 握手、加载能力、调用工具 |
| [src/semantic-router/pkg/mcp/http_client.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/http_client.go) | `HTTPClient`：走 streamable-http 的同一套语义 |
| [src/semantic-router/pkg/mcp/types.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/types.go) | `FilterTools`、JSON-RPC/协议常量、`ConvertToolToOpenAI` 格式桥 |
| [src/semantic-router/pkg/classification/mcp_classifier_client.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/mcp_classifier_client.go) | MCP 客户端的真实接入点：把 MCP 服务器当类别分类器用 |

---

## 4. 核心概念与源码讲解

### 4.1 语义工具库

#### 4.1.1 概念说明

`ToolsDatabase`（`pkg/tools`）是 SR 进程内的一个**工具注册表 + 语义检索引擎**。它解决的问题是：「我手上有一大批 OpenAI 格式工具定义，给定一次用户查询，如何挑出最相关的 top-k 个挂到请求里？」

它的工作模型非常直白：

1. **入库**：每条工具的 `description` 文本被嵌入成一个向量，连同工具定义一起存进切片；
2. **检索**：把用户查询也嵌入成向量，与库里每条工具向量做点积，按分排序，取 top-k；
3. **阈值**：低于相似度地板的工具被丢弃。

这套机制由 `tool_selection` 插件在请求阶段驱动，有两种工作模式：

- **add 模式**：库里**没有**工具定义、由请求自己带工具时，反过来对请求里已有的工具做语义打分、裁掉不相关的；
- **add 模式（库驱动）**：库里**有**工具定义时，按查询检索出 top-k **增补**进请求。

需要强调：`ToolsDatabase` 是**线性扫描**的暴力检索（遍历全部条目算点积），不是 HNSW。这在工具数量为几十到几百的量级下完全够用，且实现简单、无 CGO 依赖。

#### 4.1.2 核心流程

`ToolsDatabase` 的检索核心 `FindSimilarToolsWithScoresMinSimilarity` 可以用下面的伪代码描述：

```
输入: query, topK, minSimilarity(可选覆盖)
1. queryEmbedding = embedText(query)            # 查询向量化
2. 加读锁
3. results = []
4. for entry in db.entries:
       dot = Σ_{i} queryEmbedding[i] * entry.Embedding[i]   # 点积=余弦相似度
       floor = minSimilarity 若给定 否则 db.similarityThreshold
       if dot >= floor:
           results.append((entry, dot))
5. if results 为空: return []                    # 空切片，不是 nil
6. 按 dot 降序排序
7. limit = min(topK, len(results))              # topK 上限
8. return results[:limit]
```

一个关键的工程细节是「候选池 vs 最终 top-k」的分离。决策命中后，`EmbeddingRetriever.Retrieve` 并不直接用插件配置的 `TopK` 去检索，而是先用 `EffectivePoolSize()` 算出一个**更大的候选池**，从库里捞出一个较大的候选集，再交给 `relevance.go` 的高级过滤做精排。`EffectivePoolSize` 的启发式（[retrieval_input.go:31-42](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/tools/retrieval_input.go#L31-L42)）：

\[
\text{pool} =
\begin{cases}
\text{PoolSize} & \text{若显式设置} \\
\max(\text{TopK}\times 5,\ 20) & \text{仅设了 TopK} \\
20 & \text{都没设}
\end{cases}
\]

例如 `TopK=10` → 池 `50`；`TopK=1` → 池 `20`（因为 \(1\times5=5 < 20\)，取地板 `20`）。先放大池子再精排，是为了让词法/标签/类别等次级信号有足够样本可挑。

#### 4.1.3 源码精读

**ToolEntry 与 ToolsDatabase 结构。** 工具条目把 OpenAI 工具定义、用于匹配的描述、嵌入向量、标签、类别打包在一起；嵌入向量用 `json:"-"` 标注，意味着序列化工具库 JSON 文件时不写嵌入（嵌入只在内存里、由描述现场生成）：

[src/semantic-router/pkg/tools/tools.go:20-43](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/tools/tools.go#L20-L43) —— `ToolEntry`、`ToolSimilarity`、`ToolsDatabase` 三个结构定义。其中 `ToolsDatabase` 持有 `similarityThreshold`（地板）、`enabled` 开关、`modelType`/`targetDim`（嵌入参数）和 `provider embedding.Provider`（嵌入来源）。

**点积检索本体。** 这是本模块最核心的一段，逐条目算点积、过阈值、排序、截 top-k：

[src/semantic-router/pkg/tools/tools.go:236-293](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/tools/tools.go#L236-L293) —— `FindSimilarToolsWithScoresMinSimilarity`。注意三个要点：① 禁用时（`!db.enabled`）直接返回空切片，不报错；② 点积循环用 `i < len(queryEmbedding) && i < len(entry.Embedding)` 做长度防御（维度不一致时取较短者，避免越界）；③ 无结果时返回 `[]ToolSimilarity{}` 而非 `nil`，这条约定让调用方不必判空指针。

阈值地板由 `similarityFloor` 决定（[tools.go:207-212](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/tools/tools.go#L207-L212)）：本次查询给了 `minSimilarity` 覆盖就用它，否则回退到建库时的 `similarityThreshold`。这支持「不同决策用不同严格度」。

**嵌入来源双通道。** `embedText` 优先用注入的 `embedding.Provider`，否则回退到 CGO 的 candle 绑定：

[src/semantic-router/pkg/tools/tools.go:295-304](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/tools/tools.go#L295-L304) —— 若 `db.provider != nil` 走 `provider.Embed`，否则走 `candle_binding.GetEmbeddingWithModelType`。这与 u8-l4 讲的「工厂只认远程、本地靠 extproc 就地拼装 FuncProvider」相呼应：当 `tool_db_cache.go` 构造库时会把一个 `Provider` 传进来（见下），使工具检索可以走远程嵌入后端。

**工厂里的库构造。** 请求阶段真正创建库的代码在 extproc 包，它注入了 provider：

[src/semantic-router/pkg/extproc/tool_db_cache.go:56-73](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/tool_db_cache.go#L56-L73) —— `getOrLoadToolDatabaseForSelection` 用 `toolsEmbeddingProvider(r.Config)` 取得 provider，构造 `ToolsDatabase`，再 `LoadToolsFromFile` 从 JSON 加载工具。它还做了**按路径缓存**（`toolSelectionDBByPath`）：同一个工具库文件只加载嵌入一次，后续命中缓存。

**默认检索策略。** `EmbeddingRetriever` 是注册名为 `"default"` 的策略，把 `RetrievalInput` 翻译成对库的检索，并把 top-1 相似度作为 `Confidence` 上报：

[src/semantic-router/pkg/tools/embedding_retriever.go:28-45](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/tools/embedding_retriever.go#L28-L45) —— `Retrieve` 用 `in.EffectivePoolSize()` 决定拉多少候选，`in.MinSimilarity` 覆盖阈值，返回 `RetrievalResult{Tools, Confidence, StrategyID}`。注释明确：`HistorySummary`、`Category`、`DecisionName` 等字段在此策略里**被忽略**（它们是给其它/未来策略用的）。

**接口与注册表。** 检索被抽象成可插拔的 `ToolRetriever` 接口，按策略名注册：

[src/semantic-router/pkg/tools/retriever.go:15-50](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/tools/retriever.go#L15-L50) —— `ToolRetriever.Retrieve(ctx, RetrievalInput) (RetrievalResult, error)`；`NewDefaultRegistry(db)` 把 `EmbeddingRetriever` 注册为 `StrategyDefault`（值为 `"default"`，见 [retrieval_input.go:9-10](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/tools/retrieval_input.go#L9-L10)）；`Register` 对 nil 实现会 panic（防御性）。`Retriever = ToolRetriever` 是个类型别名。

**高级过滤与重排。** 检索完之后，`relevance.go` 在候选池上做更精细的「加权综合分」排序（仅在 `advanced.enabled` 时启用，否则退化为纯相似度 top-k）：

[src/semantic-router/pkg/tools/relevance.go:84-127](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/tools/relevance.go#L84-L127) —— `weightedCandidateCombinedScore` 把嵌入分、词法重叠分、标签分、名称匹配分、类别匹配分按可配置权重加权：

\[
\text{combined} = \frac{w_e s_{\text{embed}} + w_l s_{\text{lex}} + w_t s_{\text{tag}} + w_n s_{\text{name}} + w_c s_{\text{category}}}{w_e + w_l + w_t + w_n + w_c}
\]

并设有 `MinLexicalOverlap`（最小词法重叠，低于则剔除）与 `MinCombinedScore`（综合分下限）两道闸门。这是「向量召回 + 符号精排」的混合检索思想（与 u9-l4 记忆的混合重排一脉相承）。

#### 4.1.4 代码实践

**实践目标**：用源码阅读 + 单测验证，确认 `FindSimilarToolsWithScores` 的检索行为。

**操作步骤**（源码阅读型，无需真实模型）：

1. 打开 [tools.go:236-293](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/tools/tools.go#L236-L293)，对照 4.1.2 的伪代码，把每一步在源码里标出来：查询单次嵌入（L241）、读锁（L246）、点积循环（L253-256）、阈值判断（L258-267）、降序排序（L276-278）、top-k 截断（L280-285）。
2. 打开 [retriever_test.go:24-39](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/tools/retriever_test.go#L24-L39)，阅读 `EffectivePoolSize` 的三个用例，手算验证：`TopK=10`→`50`、`TopK=1`→`20`、`PoolSize=42`→`42`。
3. 运行这条不依赖模型的检索/注册表测试：

   ```bash
   cd src/semantic-router
   go test ./pkg/tools/ -run 'TestTools|EffectivePoolSize|NewDefaultRegistry|EmbeddingRetriever' -v
   ```

**需要观察的现象**：`NewDefaultRegistry` 用例应当通过（注册了 `default` 策略）；`EmbeddingRetriever` 在库禁用时返回 `Confidence=0`、`StrategyID="default"`、`err=nil`。

**预期结果**：上述用例全部 PASS，证明「禁用库 → 空结果（非错误）」与「候选池启发式」两条契约成立。

> 若本地未下载嵌入模型，`BeforeSuite` 会打印 `Warning: No embedding models found`，需要真实嵌入的用例会跳过；但注册表/池大小/禁用库这几条用例不依赖模型，仍会跑。**待本地验证**具体跳过数量。

#### 4.1.5 小练习与答案

**练习 1**：假如两个工具描述的嵌入向量完全相同，查询向量也等于它们，`FindSimilarToolsWithScoresMinSimilarity` 会返回几个？排序如何？

**答案**：两个都会过阈值（点积为 1.0），都会返回。因为它们的 `Similarity` 相等，`sort.Slice` 的比较函数（`results[i].Similarity > results[j].Similarity`）判定为 false 两个方向都成立，二者相对顺序不保证稳定（未用稳定排序），但都会落在 top-k 内（只要 `topK >= 2`）。

**练习 2**：为什么 `EmbeddingRetriever.Retrieve` 要先用 `EffectivePoolSize()`（往往大于 `TopK`）检索，而不是直接检索 `TopK` 条？

**答案**：因为后续 `relevance.go` 还要用词法、标签、类别、名称等次级信号在候选池上做精排与多道闸门过滤（如 `MinLexicalOverlap`）。如果一开始就只捞 `TopK` 条，这些次级信号可能把候选筛到不足 `TopK`，没有余量补充。先放大池子再精排，保证最终能凑够 `TopK` 个高质量结果。

**练习 3**：`embedText` 在什么情况下走 `embedding.Provider`、什么情况下走 candle 绑定？

**答案**：当 `ToolsDatabase.provider != nil` 时走 `provider.Embed`；否则回退到 `candle_binding.GetEmbeddingWithModelType(text, db.modelType, db.targetDim)`。请求路径上，`tool_db_cache.go` 构造库时会把一个由配置解析出的 provider 传进来，因此优先走 provider（可能是远程 openai_compatible 后端）；provider 缺席时才依赖本地 CGO candle。

---

### 4.2 MCP 客户端

#### 4.2.1 概念说明

`pkg/mcp` 是 SR 对 **Model Context Protocol** 的**客户端**实现。它把「连接一个 MCP 服务器、枚举其 tools/resources/prompts、调用某个工具」这件事抽象成统一的 `MCPClient` 接口，并用工厂按配置选择 stdio 或 HTTP 两种传输的具体实现。

设计上有三个层次：

- **接口层**：`MCPClient` 定义连接管理、能力枚举、工具/资源/提示操作、日志回调等通用方法；
- **公共基类**：`BaseClient` 持有 name/config/tools/resources/prompts/logHandler/connected 等公共字段与默认实现，避免 stdio/http 两个实现重复；
- **传输实现**：`StdioClient` 和 `HTTPClient` 各自实现 `Connect`/`CallTool`/`Close` 等差异部分，并内嵌 `*BaseClient` 复用公共部分。

**它在 SR 里的真实角色**（重要）：`pkg/mcp` 当前并未用于把外部工具注入请求的 `tools` 字段；它被 `classification.MCPCategoryClassifier` 当作一个**类别分类后端**——也就是「类别（category）信号」的一种可选实现：把待分类文本作为参数调用 MCP 服务器上的某个分类工具，拿回类别与置信度。这样，任何遵守 MCP 协议、暴露 `classify_text` 之类工具的外部服务，都能成为 SR 的域/类别分类器。

#### 4.2.2 核心流程

一次 stdio MCP 客户端的「创建 → 连接 → 调用」流程：

```
1. NewClient(name, ClientConfig)
   └─ ClientFactory.CreateClient
        └─ determineTransportType:  transportType 字段 > command(→stdio) > url(→http) > 默认 stdio
        └─ switch: stdio → NewStdioClient ; http/streamable-http → NewHTTPClient
2. client.Connect()  (以 StdioClient 为例)
   ├─ 校验 config.Command 非空
   ├─ 合并环境变量 (os.Environ + config.Env)
   ├─ client.NewStdioMCPClient(command, env, args...)  # 拉起子进程
   ├─ 在 goroutine 里 mcpClient.Initialize(...)        # JSON-RPC initialize 握手
   ├─ select { initDone | ctx.Done(超时默认30s) }
   └─ 后台 goroutine: loadCapabilities → loadTools/loadResources/loadPrompts
3. client.GetTools()        # 返回 (已过滤后的) 工具列表
4. client.CallTool(ctx, name, arguments)
   ├─ 校验 name 在 c.tools 里 (双重过滤的第二层)
   └─ mcpClient.CallTool(...) → CallToolResult
5. client.Close()           # 关闭子进程连接
```

HTTP 客户端的流程结构相同，差别在第 2 步用 `sendRequest("initialize", ...)` 做 HTTP POST 探活、第 4 步用 `sendRequest("tools/call", ...)` 发请求。

#### 4.2.3 源码精读

**MCPClient 接口。** 所有客户端实现必须满足的契约，按连接、能力、工具、资源、提示、日志分组：

[src/semantic-router/pkg/mcp/interface.go:11-35](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/interface.go#L11-L35) —— `MCPClient`。注意 `CallTool` 的签名：`CallTool(ctx, name string, arguments map[string]interface{}) (*mcp.CallToolResult, error)`，参数用 `map[string]interface{}`，返回 mcp-go 库的标准结果类型。`RefreshCapabilities` 允许在连接后重新拉取服务器能力（应对 `tools/list_changed` 这类通知）。

**配置与传输类型。** `ClientConfig` 是创建客户端的全部输入，`TransportType` 用常量约束取值：

[src/semantic-router/pkg/mcp/interface.go:93-126](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/interface.go#L93-L126) —— `ClientConfig`（Command/Args/Env 用于 stdio，URL/Headers 用于 http，TransportType 显式指定，Timeout，Options）；`ToolFilter{Mode, List}`（mode 为 `"allow"` 或 `"block"`）；`TransportType` 两个常量 `TransportStdio="stdio"` 与 `TransportStreamableHTTP="streamable-http"`，注释点明 `"http"` 也被接受为后者的别名。

**工厂分派。** 这是本模块第二关键源码点——按配置选传输：

[src/semantic-router/pkg/mcp/factory.go:17-45](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/factory.go#L17-L45) —— `CreateClient` 先用 `determineTransportType` 决定传输，再 `switch`：`stdio` → `NewStdioClient`；`streamable-http` 或 `http` → `NewHTTPClient`；其余报错 `unsupported transport type`。`determineTransportType` 的优先级是：显式 `TransportType` > 有 `Command` 推 stdio > 有 `URL` 推 http > 默认 stdio。

**配置校验。** `NewClientFromConfig` 在创建前做语义校验，把错误配置变成显式失败：

[src/semantic-router/pkg/mcp/factory.go:66-106](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/factory.go#L66-L106) —— `NewClientFromConfig` 调 `validateConfig`：stdio 必须有 `command`，http 必须有 `URL`，两者都没给则报 `either command or URL must be specified`。这避免了「建出客户端、连到一半才发现缺参」的尴尬。

**StdioClient 连接。** 拉起子进程并完成 MCP 握手：

[src/semantic-router/pkg/mcp/stdio_client.go:28-118](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/stdio_client.go#L28-L118) —— `Connect`。几个要点：① 环境变量是「父进程环境 + config.Env」叠加（L40-43），让 MCP 服务器能继承必要密钥；② `Initialize` 在独立 goroutine 里跑并用 channel 回传结果，主流程用 `select` 在 `initDone` 与 `ctx.Done()`（默认 30s 超时）之间等待（L67-105），并用 `recover` 兜住握手期的 panic；③ 握手成功后**另起 goroutine** 异步加载能力（L108-115），连接立即返回，不阻塞在资源/提示列表上。

**工具加载与过滤。** stdio 与 http 两个实现在加载工具时都调用同一个 `FilterTools`：

[src/semantic-router/pkg/mcp/stdio_client.go:141-158](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/stdio_client.go#L141-L158) —— `loadTools`：`ListTools` 拿全量，`FilterTools(toolsResult.Tools, c.config.Options.ToolFilter)` 过滤后存入 `c.tools`，日志记 `Loaded N tools (filtered from M)`。这是**第一层过滤**（加载期）。

**工具调用前的二次校验。** `CallTool` 在真正调用前确认工具名在（已过滤的）`c.tools` 里：

[src/semantic-router/pkg/mcp/stdio_client.go:195-211](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/stdio_client.go#L195-L211) —— 这是**第二层过滤**（调用期）。即便有人绕过配置直接传工具名，不在 `c.tools` 集合里就会被拒为 `tool '%s' not found or not allowed`。HTTP 客户端有完全对称的实现（[http_client.go:203-219](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/http_client.go#L203-L219)）。

**真实接入点：作为分类器。** `MCPCategoryClassifier.Init` 是 MCP 客户端在 SR 里被实际创建和连接的地方：

[src/semantic-router/pkg/classification/mcp_classifier_client.go:20-63](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/mcp_classifier_client.go#L20-L63) —— 用 `MCPCategoryModel` 配置（`TransportType`/`Command`/`Args`/`Env`/`URL`/`TimeoutSeconds`）组装 `mcpclient.ClientConfig`，调 `mcpclient.NewClient("category_classifier", mcpConfig)` 创建、`client.Connect()` 连接，然后 `discoverClassificationTool()` 选定分类工具。配置类型见 [model_config_types.go:102-112](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/config/model_config_types.go#L102-L112) 的 `MCPCategoryModel`。

工具发现策略很有意思——先看是否显式配了 `tool_name`，否则按候选名（`classify_text`/`classify`/`categorize`/`categorize_text`）匹配，再退到「名字或描述含 classif」的模糊匹配：[mcp_classifier_client.go:66-119](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/mcp_classifier_client.go#L66-L119)。

#### 4.2.4 代码实践

**实践目标**：跟踪一个 stdio 类型 MCP 服务从 factory 创建到连接成功的完整路径。

**操作步骤**（源码阅读型）：

1. 从配置出发。打开 [model_config_types.go:102-112](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/config/model_config_types.go#L102-L112)，记下 `MCPCategoryModel` 的字段：`enabled`、`transport_type`、`command`/`args`/`env`、`url`、`tool_name`、`threshold`、`timeout_seconds`。
2. 跟踪创建链。读 [mcp_classifier_client.go:30-50](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/mcp_classifier_client.go#L30-L50)：配置 → `mcpclient.ClientConfig` → `mcpclient.NewClient`。
3. 进入 factory。读 [factory.go:17-45](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/factory.go#L17-L45)：当 `transport_type="stdio"`（或留空且给了 `command`）时，`determineTransportType` 返回 `stdio`，`CreateClient` 返回 `NewStdioClient(name, config)`。
4. 进入连接。读 [stdio_client.go:28-118](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/stdio_client.go#L28-L118)：拉子进程 → Initialize 握手 → 超时控制 → 异步加载能力。
5. 画出调用链：`MCPCategoryModel(配置) → ClientConfig → ClientFactory.CreateClient → determineTransportType → NewStdioClient → Connect → NewStdioMCPClient + Initialize → loadCapabilities`。

**需要观察的现象**：在调用链图里，标注出三个「失败即返回错误」的关键点：① `NewClient` 创建失败（[mcp_classifier_client.go:44-47](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/mcp_classifier_client.go#L44-L47)）；② `Connect` 失败（L48-50）；③ `discoverClassificationTool` 找不到分类工具会 `client.Close()` 后返回错误（L53-56）。

**预期结果**：得到一张完整的「stdio MCP 服务器从配置到可用」的调用链图，能指出每一步的输入输出与错误处理点。这一步**不执行真实子进程**，纯靠读源码完成。

> 若想真正跑通，需要本地有一个实现了 MCP 协议、暴露 `classify_text` 工具的可执行进程，并在配置里填好 `command`/`args`。这属于**待本地验证**的内容。

#### 4.2.5 小练习与答案

**练习 1**：`determineTransportType` 的判定优先级是什么？如果配置里同时给了 `command` 和 `url` 但没给 `transport_type`，会选哪个？

**答案**：优先级是「显式 `TransportType` > 有 `Command`（推 stdio）> 有 `URL`（推 streamable-http）> 默认 stdio」。同时给了 `command` 和 `url` 但没给 `transport_type` 时，因为先判 `Command != ""`，会选 **stdio**。所以想用 HTTP 必须显式写 `transport_type: streamable-http`（或 `http`），否则会被 command 抢走。

**练习 2**：`StdioClient.Connect` 为什么把 `Initialize` 放在 goroutine 里，再用 `select` 等待？

**答案**：为了同时实现「超时取消」。`Initialize` 是阻塞的 JSON-RPC 调用，如果服务器启动慢或卡住，直接调会永远挂住。放进 goroutine 后，主流程用 `select` 在 `initDone`（成功/失败）与 `ctx.Done()`（默认 30s 超时）之间竞争：要么握手完成，要么超时返回 `timeout connecting to MCP server`。同时 goroutine 内还套了 `recover`，把握手期的 panic 转成 error 而非崩进程。

**练习 3**：`MCPCategoryClassifier.discoverClassificationTool` 用了哪三种策略来确定用哪个工具做分类？

**答案**：① 配置显式指定 `tool_name`（`selection_mode: configured`）；② 按候选名列表 `classify_text`/`classify`/`categorize`/`categorize_text` 精确匹配（`auto_name_match`）；③ 退化到「工具名或描述包含 `classif` 子串」的模糊匹配（`auto_pattern_match`）。三者都未命中则报错并列出所有可用工具名。

---

### 4.3 传输与过滤

#### 4.3.1 概念说明

本模块把 4.2 里散见的两个横切关注点收拢：**传输选择**与**工具过滤**。

- **传输选择**：MCP 有 stdio 与 streamable-http 两种主流传输，SR 用工厂 + 配置自动分派，并对 `"http"` 做了 `"streamable-http"` 的别名兼容；HTTP 客户端还自行实现了 JSON-RPC over HTTP 的请求发送（没有复用 mcp-go 的 HTTP 客户端，而是手写 `sendRequest`）。
- **工具过滤**：`ToolFilter`（allow/block 两种模式）让客户端只暴露工具的一个子集。它在**两个层级**被强制：加载期（`loadTools` 调 `FilterTools` 决定 `c.tools`）与调用期（`CallTool` 校验工具名在 `c.tools` 里）。

理解这两点，就能解释「为什么配了 block 的工具既不会出现在 `GetTools()` 里、也无法被 `CallTool` 调到」。

#### 4.3.2 核心流程

**FilterTools 的逻辑**（`pkg/mcp/types.go`）：

```
输入: tools (服务器全量), filter{Mode, List}
若 Mode=="" 或 List 为空: 原样返回 (不过滤)
否则:
  for tool in tools:
    inList = tool.Name 是否在 filter.List 中
    if Mode=="allow":  保留 inList 为真者
    if Mode=="block":  保留 inList 为假者
  return filtered
```

注意两个边界：① 过滤配置为空时是**直通**（不过滤），不是「全拦」；② 名字匹配是**精确相等**（`toolInList`，[types.go:487-494](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/types.go#L487-L494)），不是子串、不分大小写处理（直接 `name == entry`）。

**HTTP 请求发送**（`HTTPClient.sendRequest`）：把任意 MCP 方法（`initialize`/`tools/list`/`tools/call`/`ping` 等）统一 POST 到 `{baseURL}/{endpoint}`，带上 `Content-Type`/`Accept` 与自定义 headers，检查 2xx 状态码，返回响应体字节。

#### 4.3.3 源码精读

**FilterTools 实现。** 简洁的模式分派：

[src/semantic-router/pkg/mcp/types.go:497-516](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/types.go#L497-L516) —— `FilterTools`。第一行就是「空配置直通」的早退（`filter.Mode == "" || len(filter.List) == 0` → 返回原 tools）。`allow` 模式只留名单内、`block` 模式只留名单外。这个函数被 stdio 与 http 的 `loadTools` 共用，保证两套传输的过滤行为一致。

**两层强制点。** 第一层（加载期）见 4.2.3 引用的 `loadTools`（[stdio_client.go:152](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/stdio_client.go#L152) 与 [http_client.go:154](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/http_client.go#L154)）；第二层（调用期）在 `CallTool` 开头遍历 `c.tools` 检查（[stdio_client.go:201-211](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/stdio_client.go#L201-L211)）。由于 `c.tools` 已经是过滤后的集合，第二层本质是「不在白名单/在黑名单 → 拒绝」。

**HTTP 传输的请求细节。** SR 的 HTTP 客户端没有用 mcp-go 的传输，而是自己拼 HTTP：

[src/semantic-router/pkg/mcp/http_client.go:319-368](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/http_client.go#L319-L368) —— `sendRequest`：URL 拼成 `baseURL/endpoint`（如 `.../tools/list`、`.../tools/call`）；`payload == nil` 时不发 body；带上 `Content-Type: application/json`、`Accept: application/json` 以及 `config.Headers` 里的自定义头（可用于鉴权）；非 2xx 状态码返回带状态码与响应体的错误。协议版本由常量 `MCPProtocolVersion = "2024-11-05"` 固定（[http_client.go:15-24](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/http_client.go#L15-L24)）。

**别名兼容。** `CreateClient` 与 `validateConfig` 都把 `"http"` 当 `"streamable-http"` 接受（[factory.go:23](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/factory.go#L23) 与 [factory.go:96-99](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/factory.go#L96-L99)），降低配置门槛。

**格式桥（连接两套工具体系）。** `ConvertToolToOpenAI` 把 MCP 的 `mcp.Tool` 转成 OpenAI 的 `ChatCompletionToolParam`，是 MCP 工具与请求 `tools` 字段之间的翻译器（虽然当前未在请求注入路径使用，但类型桥已就绪）：

[src/semantic-router/pkg/mcp/types.go:401-418](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/types.go#L401-L418) —— `ConvertToolToOpenAI`：把 `tool.Name`/`tool.Description` 映射到函数定义，把 `InputSchema.Properties`/`Required` 映射到 JSON Schema 参数。

#### 4.3.4 代码实践

**实践目标**：通过阅读 + 单测，验证 `FilterTools` 的 allow/block 行为与边界。

**操作步骤**：

1. 读 [types.go:497-516](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/types.go#L497-L516)，确认空配置直通、allow 留名单内、block 留名单外。
2. 读 [types_test.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/types_test.go) 中针对 `FilterTools` 的用例（若存在），看测试如何构造 `mcp.Tool` 切片与 `ToolFilter`。
3. 运行测试：

   ```bash
   cd src/semantic-router
   go test ./pkg/mcp/ -run 'FilterTools|TestTypes' -v
   ```

4. 手工推演一个例子：服务器返回 3 个工具 `["search", "delete", "list_categories"]`，配置 `ToolFilter{Mode: "block", List: ["delete"]}`。问 `loadTools` 后 `c.tools` 是什么？若再 `CallTool("delete", ...)` 结果如何？

**需要观察的现象**：`FilterTools` 在 block 模式下应保留 `["search", "list_categories"]`；`delete` 不在 `c.tools` 中，`CallTool` 会返回 `tool 'delete' not found or not allowed` 错误。

**预期结果**：测试通过；手工推演的两层过滤结论与源码一致。

> 测试是否覆盖 `FilterTools` 取决于 `types_test.go` 的实际内容，**待本地验证**用例命名与数量；若没有专门用例，可参考其断言风格自行补一个（仅作练习，不提交）。

#### 4.3.5 小练习与答案

**练习 1**：`ToolFilter{Mode: "allow", List: []}`（allow 但名单为空）会发生什么？

**答案**：由于 `FilterTools` 第一行的早退条件是 `filter.Mode == "" || len(filter.List) == 0`，`List` 为空时直接原样返回**全部工具**（不过滤）。也就是说「allow + 空名单」**不是**「全拦」，而是「直通」。这是一个容易踩的坑：想要「全部禁用」不能用空 allow 列表表达。

**练习 2**：为什么 `CallTool` 已经在 `loadTools` 过滤过工具后，还要在调用前再检查一次工具名是否存在？

**答案**：这是纵深防御（defense in depth）。`c.tools` 是加载期的快照，但调用方传进来的 `name` 是运行期入参，可能来自不可信数据。第二层检查确保「即便调用方传了被 block 的工具名，也会被拒」，把过滤从「加载期一次性」变成「加载期 + 调用期」双重保证。错误信息 `not found or not allowed` 同时覆盖了「不存在」和「被过滤」两种情况。

**练习 3**：HTTP 客户端的 `sendRequest` 如何把 MCP 方法映射到 URL？`tools/call` 会 POST 到哪里？

**答案**：URL 拼成 `{baseURL}/{endpoint}`，endpoint 就是 MCP 方法名。所以 `tools/call` 会 POST 到 `{baseURL}/tools/call`，`tools/list` 到 `{baseURL}/tools/list`，`initialize` 到 `{baseURL}/initialize`。这是一种把 JSON-RPC 方法名直接当 URL 路径段的扁平映射。

---

## 5. 综合实践

把 4.1 与 4.2/4.3 串起来，完成下面这个贯穿本讲的源码阅读任务：

**任务**：假设你要回答一个用户问题——「SR 是如何为一个带工具的请求挑出相关工具的，以及它如何调用一个外部 MCP 服务来做分类？」请完成两张图与一段说明。

1. **工具检索链路图**。从 `tool_selection` 插件出发，画出直到 top-k 工具被写回 `openAIRequest.Tools` 的完整调用链。提示需要串起的节点：
   - `runToolSelectionPluginAdd`（[req_tool_selection_plugin.go:59-113](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/req_tool_selection_plugin.go#L59-L113)）取库、算 topK/strategy/minSim；
   - `findToolsForQueryExt` → `EmbeddingRetriever.Retrieve`（[embedding_retriever.go:28-45](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/tools/embedding_retriever.go#L28-L45)）；
   - `EffectivePoolSize`（[retrieval_input.go:31-42](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/tools/retrieval_input.go#L31-L42)）放大候选池；
   - `FindSimilarToolsWithScoresMinSimilarity`（[tools.go:236-293](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/tools/tools.go#L236-L293)）点积检索；
   - `FilterAndRankToolsWithConversation`（[relevance.go:22-45](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/tools/relevance.go#L22-L45)）精排；
   - `applySelectedTools` 写回（[req_filter_tools.go:37-70](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/req_filter_tools.go#L37-L70)）。

2. **MCP 分类链路图**。画出从 `MCPCategoryModel` 配置到一次 `Classify` 返回类别+置信度的链路。提示节点：`Init`（[mcp_classifier_client.go:20-63](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/mcp_classifier_client.go#L20-L63)）→ `ClientFactory.CreateClient`（[factory.go:17-45](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/factory.go#L17-L45)）→ `StdioClient.Connect`（[stdio_client.go:28-118](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/stdio_client.go#L28-L118)）→ `discoverClassificationTool`（[mcp_classifier_client.go:66-119](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/mcp_classifier_client.go#L66-L119)）→ `Classify`（[mcp_classifier_client.go:130-147](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/mcp_classifier_client.go#L130-L147)）→ `CallTool`（[stdio_client.go:195-245](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/stdio_client.go#L195-L245)）。

3. **一段说明**：用自己的话写 200 字以内，对比两套机制的异同——都用了「嵌入/向量」吗？都改写请求的 `tools` 字段吗？谁是进程内计算、谁是跨进程 RPC？

**验收标准**：两张图能自洽对应到上述源码行号；说明里能准确指出「工具库用嵌入点积做进程内检索并改写请求 tools；MCP 客户端是跨进程 RPC、当前用作分类后端而非改写 tools」。

## 6. 本讲小结

- `ToolsDatabase`（`pkg/tools`）是进程内、线性扫描的语义工具库：入库时对 `description` 生成嵌入，检索时对查询嵌入与每条工具嵌入做点积（归一化下即余弦相似度），过相似度地板后取 top-k。
- 检索被抽象成 `ToolRetriever` 接口 + `Registry` 策略注册表；默认策略 `EmbeddingRetriever`（名 `"default"`）先用 `EffectivePoolSize()`（`TopK×5` 与地板 `20` 取大）放大候选池，再交 `relevance.go` 做嵌入+词法+标签+类别+名称的加权精排。
- 嵌入来源是双通道：优先用注入的 `embedding.Provider`（可远程 openai_compatible），缺席才回退 candle CGO 绑定；这与 u8-l4 的「工厂只认远程、本地靠 extproc 就地拼装」一致。
- `pkg/mcp` 是 MCP 客户端：`MCPClient` 接口 + `BaseClient` 公共基类 + `StdioClient`/`HTTPClient` 两套传输；`ClientFactory` 按显式 `transport_type` > 有 `command`（stdio）> 有 `url`（http）> 默认 stdio 分派，并把 `"http"` 当 `"streamable-http"` 的别名。
- `ToolFilter`（allow/block）在**两层**强制：`loadTools` 期调 `FilterTools` 决定暴露的 `c.tools`，`CallTool` 期再校验工具名在该集合内；空配置（空 Mode 或空 List）为直通，allow+空名单不是全拦。
- 真实接入点上，MCP 客户端当前由 `classification.MCPCategoryClassifier` 当作**类别分类后端**使用（配置类型 `MCPCategoryModel`），尚未用于把外部工具注入请求的 `tools` 字段——但 `ConvertToolToOpenAI` 这一格式桥已就绪。

## 7. 下一步学习建议

- **向「分类信号」深入**：本讲看到 MCP 被当作分类后端，建议接着读 u8-l1（分类编排与信号求值）与 u8-l2（域/类别分类器），看 `MCPCategoryClassifier` 如何与本地 MMLU-Pro 类别分类器并列、汇入 `SignalResults["domain:..."]`。
- **向「混合检索」深入**：`relevance.go` 的加权综合分与 `hybrid_history.go` 是工具检索的进阶策略，可与 u9-l4（智能体记忆的 BM25 重排）对照阅读，体会 SR 里「向量召回 + 符号重排」的统一模式。
- **向「插件装配」回看**：重读 u10-l1，确认 `tool_selection` 插件如何挂在某条 decision 的 `plugins[]` 上、按决策启停，把本讲的检索能力钉到请求链路里。
- **动手扩展**：若想让 SR 真正把 MCP 工具注入请求 `tools`，可参照 `ConvertToolToOpenAI`（[types.go:401-418](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/mcp/types.go#L401-L418)）写一个把 `client.GetTools()` 转成 `[]ChatCompletionToolParam` 并合并进请求的插件——这是理解本讲两套体系如何打通的最佳练习。
