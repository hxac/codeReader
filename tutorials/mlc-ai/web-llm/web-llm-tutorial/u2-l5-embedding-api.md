# u2-l5 embedding 嵌入接口与 EmbeddingPipeline

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `EmbeddingPipeline` 与 `LLMChatPipeline` 的分工——为什么嵌入模型需要一条独立的推理管线，它们又共用什么。
2. 掌握 `EmbeddingCreateParams` 的四种 `input` 形态、`model` 字段的选模型规则、`encoding_format` 的限制，以及 `postInitAndCheckFields` 的三道校验。
3. 理解 `engine.embedding()` 如何在多模型引擎中路由到嵌入管线，`model_type` 在其中起的「身份验证」作用，以及 `EmbeddingUnsupportedModelError` 等错误分别在什么条件下触发。
4. 读懂返回的 `CreateEmbeddingResponse`：向量从哪个 token 位置取出、`usage.extra.prefill_tokens_per_s` 统计的是什么。

本讲是引擎接口层（单元二）的最后一讲。前三讲我们跟完了 chat 与 completion 的生成式链路，本讲换一个视角：模型不生成文字，而是把文字「压缩」成一个向量。

## 2. 前置知识

### 2.1 什么是 embedding（嵌入向量）

嵌入向量是把一段文本映射成一个定长浮点数数组，例如 768 维：

```
"The Data Cloud!"  →  [0.021, -0.113, 0.087, ..., 0.042]   // 长度 = hidden_size
```

它的关键性质是：**语义相近的文本，向量在空间中也相近**。于是「两段文本有多相关」就变成了「两个向量夹角有多小」。常用余弦相似度衡量：

\[ \cos(\theta) = \frac{A \cdot B}{\|A\| \, \|B\|} = \frac{\sum_i a_i b_i}{\sqrt{\sum_i a_i^2}\sqrt{\sum_i b_i^2}} \]

取值范围 \([-1, 1]\)，越接近 1 越相似。嵌入向量是检索（RAG）、聚类、去重、语义搜索的基础设施。

### 2.2 BERT 式「编码器」与生成式 LLM 的差别

- 生成式 LLM（前几讲的 chat 模型）是**自回归**的：吃进 prompt，输出下一个 token 的概率分布，循环往复。
- 嵌入模型（本讲的 Snowflake Arctic Embed 系列）是 BERT 式**编码器**：一次性读完整句，每个 token 位置都产出一个隐状态向量（hidden state）。通常取第一个位置（`[CLS]`）的隐状态作为整句的向量表示。

这个差别决定了嵌入管线不需要解码循环、不需要采样，只需要「一次前向 + 取出某位置的向量」。

### 2.3 批处理与 padding

GPU 前向要求同一批输入形状一致。若一批 3 句话长度分别是 5、3、8 个 token，就要把短句**补零**到 8，同时配一张「注意力掩码」（attention mask）标记哪些位置是真实 token（1）、哪些是补的（0），防止补零位置污染计算：

```
输入:  [t1 t2 t3 t4 t5 0  0  0]   掩码: [1  1  1  1  1 0  0  0]
```

### 2.4 与前面讲次的衔接

- u2-l1 讲过：`MLCEngine` 构造函数创建了三个 OpenAI 风格门面，其中就有 `embeddings`；`reload()` 按 `ModelRecord.model_type` 决定构造哪条管线。
- u2-l2/u2-l4 讲过 `Chat`/`Completions` 门面类只做转发；本讲的 `Embeddings` 门面是同一模式的第三份实现。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `src/openai_api_protocols/embedding.ts` | 协议层 | `Embeddings` 门面、`EmbeddingCreateParams` 请求类型、`postInitAndCheckFields` 校验、`CreateEmbeddingResponse` 响应类型 |
| `src/embedding.ts` | 管线层 | `EmbeddingPipeline`：构造校验、`embedStep` 批处理前向、性能统计 |
| `src/engine.ts` | 引擎层 | `embedding()` 分发、`getEmbeddingStates`/`getModelStates` 模型路由、`reload()` 中的管线分流 |
| `src/support.ts` | 公共工具 | `getModelIdToUse`：单模型/多模型下的模型选择规则 |
| `src/config.ts` | 配置 | `ModelType` 枚举、`ModelRecord.model_type` 字段、4 条预置嵌入模型记录 |
| `src/error.ts` | 错误 | 6 个 `Embedding*` 错误类 + `IncorrectPipelineLoadedError` |
| `src/message.ts` / `src/web_worker.ts` | Worker 层 | `"embedding"` 消息种类：嵌入接口同样可以在 Worker 中使用 |
| `examples/embeddings/` | 示例 | 三种用法：裸 API、LangChain 集成、单引擎双模型 RAG |
| `tests/openai_embeddings.test.ts` | 测试 | 用例反推请求校验的预期行为 |

## 4. 核心概念与源码讲解

### 4.1 Embeddings 协议类与请求校验

#### 4.1.1 概念说明

WebLLM 的公开接口刻意对齐 OpenAI 的 `embeddings.create()`，让熟悉 OpenAI SDK 的开发者零成本迁移。这一层只做三件事：

1. 提供一个 `Embeddings` 门面类，让 `engine.embeddings.create(...)` 这种 OpenAI 风格写法生效（真正的实现是 `engine.embedding(...)`）。
2. 用 TypeScript 类型描述请求与响应的形状。
3. 在请求进入引擎前做字段校验，把非法请求挡在 GPU 之前。

#### 4.1.2 核心流程

```
engine.embeddings.create(request)
        │  （门面转发）
        ▼
engine.embedding(request)          ← 引擎层，4.3 节精读
        │
        ▼
API.postInitAndCheckFieldsEmbedding(request, modelId)   ← 本节的三道校验：
        │  1. 含不支持字段（dimensions / user）→ UnsupportedFieldsError
        │  2. encoding_format === "base64"     → EmbeddingUnsupportedEncodingFormatError
        │  3. input 为空串/空数组/含空元素     → EmbeddingInputEmptyError
        ▼
EmbeddingPipeline.embedStep(input)  ← 管线层，4.2 节精读
```

#### 4.1.3 源码精读

**门面类**。与 `Chat`、`Completions` 完全同构：构造时持有引擎，`create()` 一行转发：

- [src/openai_api_protocols/embedding.ts:25-38](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/embedding.ts#L25-L38)：`Embeddings` 类持有 `MLCEngineInterface`，`create()` 直接 `return this.engine.embedding(request)`——门面模式，无任何自身逻辑。

**请求类型 `EmbeddingCreateParams`**。最关键的是 `input` 的四种形态：

- [src/openai_api_protocols/embedding.ts:111-118](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/embedding.ts#L111-L118)：`input` 的联合类型 `string | Array<string> | Array<number> | Array<Array<number>>`，四种形态含义分别是：

| 写法 | 语义 | 产出几条向量 |
| --- | --- | --- |
| `"你好"` | 一句话 | 1 |
| `["你好", "世界"]` | 两句话（批量） | 2 |
| `[101, 872, 1962]` | **一句话**的 token id 序列 | 1 |
| `[[101, 872], [101, 1962]]` | 两句话的 token 序列（批量） | 2 |

  注意第三行的陷阱：`Array<number>` 是「一条已分词的输入」，不是「多个数字输入」——数字元素永远被归入同一条序列（4.2.3 节源码会印证这一点）。

- [src/openai_api_protocols/embedding.ts:120-128](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/embedding.ts#L120-L128)：`model` 字段的规则——只加载了一个模型时可省略；加载了多个模型时必填（引擎据此路由，见 4.3 节）。
- [src/openai_api_protocols/embedding.ts:130-135](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/embedding.ts#L130-L135)：`encoding_format` 类型上允许 `"float" | "base64"`，但注释明确「目前只支持 float」。
- [src/openai_api_protocols/embedding.ts:137-152](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/embedding.ts#L137-L152)：`dimensions`（截断维度，未来支持 matryoshka 模型时开放）和 `user` 均标注「Not supported」。

**不支持字段清单**与**校验函数**：

- [src/openai_api_protocols/embedding.ts:154-157](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/embedding.ts#L154-L157)：`EmbeddingCreateParamsUnsupportedFields = ["dimensions", "user"]`——声明了却没实现的字段以显式清单管理，`in` 运算符检测用户是否真的传了。
- [src/openai_api_protocols/embedding.ts:159-173](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/embedding.ts#L159-L173)：校验第 1 道——遍历不支持字段清单，凡用户传了就收集进 `unsupported`，抛 `UnsupportedFieldsError`。
- [src/openai_api_protocols/embedding.ts:175-178](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/embedding.ts#L175-L178)：第 2 道——`encoding_format === "base64"` 抛 `EmbeddingUnsupportedEncodingFormatError`。
- [src/openai_api_protocols/embedding.ts:180-197](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/embedding.ts#L180-L197)：第 3 道——空输入检查，分四种情况：空字符串、空数组、数组中含空字符串、数组中含空 token 数组，统一抛 `EmbeddingInputEmptyError`。注意循环里用 `typeof curInput !== "number"` 区分元素是数字（整体是一条 token 序列）还是字符串/数组（逐条检查）。

**响应类型**：

- [src/openai_api_protocols/embedding.ts:93-109](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/embedding.ts#L93-L109)：`Embedding` 接口——`embedding`（浮点数组，长度由模型的 `hidden_size` 决定）、`index`（在本次批量中的序号）、`object: "embedding"`。
- [src/openai_api_protocols/embedding.ts:40-60](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/embedding.ts#L40-L60)：`CreateEmbeddingResponse`——`data` 向量列表、`model`、`object: "list"`、`usage`。
- [src/openai_api_protocols/embedding.ts:62-88](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/embedding.ts#L62-L88)：`usage` 中 `prompt_tokens`/`total_tokens` 与 OpenAI 对齐；`extra.prefill_tokens_per_s` 是 WebLLM 特有扩展，报告本轮嵌入的吞吐（见 4.2.3 节性能 API）。

#### 4.1.4 代码实践：用单测反推校验行为

**实践目标**：不启动浏览器，仅靠一个 mock 级单测验证三道校验的边界。

**操作步骤**：

1. 本讲不需要 WebGPU，`tests/openai_embeddings.test.ts` 是纯协议层测试，直接调用 `postInitAndCheckFields`。在仓库根目录执行：

   ```bash
   npx jest tests/openai_embeddings.test.ts
   ```

2. 打开 [tests/openai_embeddings.test.ts:52-88](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/openai_embeddings.test.ts#L52-L88)，对照「Invalid embedding input」的四个用例：空字符串、数组含空字符串、空数组、数组含空 token 数组——全部断言抛 `EmbeddingInputEmptyError`。
3. 再对照 [tests/openai_embeddings.test.ts:90-122](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/openai_embeddings.test.ts#L90-L122)：`base64` 抛 `EmbeddingUnsupportedEncodingFormatError`；`user: "Bob"` 和 `dimensions: 2048` 的断言只匹配错误消息前缀 `"The following fields in"`（即 `UnsupportedFieldsError`）。

**需要观察的现象**：测试输出中每个用例的通过情况；注意「Supported embedding request」组（[tests/openai_embeddings.test.ts:11-50](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/openai_embeddings.test.ts#L11-L50)）覆盖了 `input` 的全部四种合法形态。

**预期结果**：全部用例通过（该测试属 mock 层，不依赖 GPU；具体输出以本地运行为准，若失败请检查依赖是否安装完整）。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：以下哪个 `input` 会产出 2 条向量？A. `"hello"`；B. `["hello", "world"]`；C. `[1, 2, 3]`；D. `[[1, 2], [3]]`。

答案：B 和 D。A 是单字符串产出 1 条；C 是**一条** token 序列（`Array<number>` 整体视为一句话），产出 1 条；D 是两条 token 序列的批量，产出 2 条。

**练习 2**：为什么 `dimensions` 和 `user` 在类型里声明了，却要在运行时抛错，而不是直接从类型中删掉？

答案：为了与 OpenAI SDK 的类型保持一致，让用户可以直接把 OpenAI 的请求对象传进来（协议兼容优先）。声明但未实现的字段集中登记在 `EmbeddingCreateParamsUnsupportedFields`（[src/openai_api_protocols/embedding.ts:154-157](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/embedding.ts#L154-L157)），运行时检测到「用户真的传了」才报 `UnsupportedFieldsError`，从而给出精确的错误提示而不是静默忽略。

### 4.2 EmbeddingPipeline：嵌入模型的推理管线

#### 4.2.1 概念说明

`EmbeddingPipeline` 是 `LLMChatPipeline` 的姊妹管线，两者的分工：

| 维度 | LLMChatPipeline（u3 单元精读） | EmbeddingPipeline |
| --- | --- | --- |
| 任务 | 自回归生成文本 | 把整句编码成向量 |
| 调用的核心 PackedFunc | `prefill` + `decode` + 采样 | **只有 `prefill`** |
| 取的是什么 | 最后一个位置的 logits → 采样下一个 token | **第一个 token 位置的隐状态** `logits[:, 0, :]` |
| 会话状态 | 维护 KV cache，多轮可复用 | 无状态，每次 `embedStep` 独立 |
| 支持批量 | 一次一条 | 支持，按 `max_batch_size` 分批 |

注意一个容易忽略的事实：嵌入管线复用的函数名也叫 `prefill`——模型 wasm 库的入口是一致的，差别在于 WebLLM 拿到输出后怎么用。生成式管线拿最后一个位置的分布去采样；嵌入管线拿第一个位置的隐状态当向量。

#### 4.2.2 核心流程

`embedStep(input)` 的完整流程（伪代码）：

```
输入归一化: 把 4 种 input 形态统一成 Array<Array<number>>（token 序列数组）
    │  字符串 → tokenizer.encode；数字元素 → 累积为同一条序列
    ▼
逐条检查: 每条序列长度 ≤ contextWindowSize，否则抛 EmbeddingExceedContextWindowSizeError
    ▼
按 maxBatchSize 分批，对每一批:
    │  1. 求本批最长序列 maxInputSize
    │  2. 短序列补零到 maxInputSize，同时生成 0/1 注意力掩码
    │  3. 展平的 id 数组与掩码上传 GPU，view 成 (batchSize, maxInputSize)
    │  4. prefill(input, mask, params) 前向 → (batchSize, maxInputSize, hidden_size)
    │  5. 拷回 CPU
    │  6. 对每条序列取 [0, :]（第一个 token 的隐状态）作为该句向量
    ▼
返回 Array<Array<number>>，并记录本轮 token 数与耗时
```

其中 padding 的几何关系（设本批 3 条序列，最长 5）：

\[ \text{输入矩阵} \in \mathbb{Z}^{3 \times 5}, \quad \text{掩码矩阵} \in \{0,1\}^{3 \times 5}, \quad \text{输出} \in \mathbb{R}^{3 \times 5 \times h} \]

每条句子只取输出的第 0 个位置：`输出[i, 0, :]`（长度 \(h\) = hidden_size）。

#### 4.2.3 源码精读

**构造函数：加载函数与四道配置校验**。

- [src/embedding.ts:33-47](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L33-L47)：保存 `tvm`/`tokenizer`/`config`，取 WebGPU 设备，创建 `VirtualMachine` 并从中取出 `prefill` PackedFunc——注意它**没有**取 `decode`，嵌入不需要解码循环。`detachFromCurrentScope` 把对象从 tvm 作用域中「摘出」，避免离开 `beginScope/endScope` 区间时被自动回收。
- [src/embedding.ts:49-62](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L49-L62)：调用 VM 的 `_metadata` 函数拿到编译期元数据 JSON，从中读出参数名列表，再 `getParamsFromCacheByName` 把模型权重按名取出挂到管线。
- [src/embedding.ts:64-72](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L64-L72)：从元数据读 `max_batch_size` 与 `prefill_chunk_size`，从 ChatConfig 读 `context_window_size`。注释说明了一个约定：嵌入模型编译时 `prefillChunkSize` 与 `contextWindowSize` 相同。
- [src/embedding.ts:74-91](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L74-L91)：四道校验，全部不满足即抛错（错误类都定义在 [src/error.ts:499-553](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L499-L553)）：

| 校验 | 抛出的错误 | 含义 |
| --- | --- | --- |
| `sliding_window_size !== -1` | `EmbeddingSlidingWindowError` | 嵌入模型不支持滑动窗口注意力 |
| `maxBatchSize <= 0` | `MinValueError` | 元数据非法 |
| `contextWindowSize <= 0` | `MinValueError` | 配置非法 |
| `prefillChunkSize !== contextWindowSize` | `EmbeddingChunkingUnsupportedError` | 嵌入不支持分块预填充（生成式管线靠分块省显存，嵌入要求整句一次进来） |

**`embedStep` 第 1 步：输入归一化**。

- [src/embedding.ts:105-131](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L105-L131)：所有形态归一到 `tokenizedInputs: Array<Array<number>>`。三处关键：
  - L108-110：`input.length === 0` 直接抛 `EmbeddingInputEmptyError`（管线层的兜底，与协议层校验双保险）；
  - L111-113：单个字符串编码为一条序列；
  - L115-127：遍历数组元素——元素是数组（`Array<Array<number>>`）原样收集；是字符串就编码；**是数字则 push 进 `tempInputs`**，循环结束后 `tempInputs` 整体作为一条序列追加（L129-131）。这正是 4.1.3 节说 `Array<number>` 表示「一句话」的代码依据。源码注释还提到不能用 `input.every` 判型（TypeScript 的已知类型收窄缺陷，issue #33591）。

**第 2 步：上下文窗口检查**。

- [src/embedding.ts:133-148](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L133-L148)：逐条累计 token 数到 `totalNumTokens`（后面 usage 用），任何一条超过 `contextWindowSize` 就抛 `EmbeddingExceedContextWindowSizeError`。注意与生成式管线不同：**嵌入没有截断策略，超长直接报错**。源码 TODO 提到 `tokenizer.encode` 似乎会隐式截断到窗口大小，该行为尚待确认——所以这条防线可能很少真正触发。

**第 3 步：分批与 padding**。

- [src/embedding.ts:150-158](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L150-L158)：外层循环 `begin += this.maxBatchSize` 切批，一批最多 `maxBatchSize` 条——**超过编译期批上限的输入不会被拒绝，而是自动多次前向**（示例注释里 b4/b32 的区别正在于此）。
- [src/embedding.ts:159-189](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L159-L189)：求本批最长 `maxInputSize`；对每条序列「真实 token + 1 掩码」与「补零 + 0 掩码」交错 push 进两个展平数组；最后断言两个数组长度都等于 `curBatchSize * maxInputSize`（防内部错误的 invariant）。

**第 4 步：上传 GPU 并前向**。

- [src/embedding.ts:190-204](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L190-L204)：`tvm.empty` 在 GPU 上开一块 `int32` 一维缓冲，`copyFrom` 拷入展平数据，再 `view([curBatchSize, maxInputSize])` 重塑成二维——输入 id 与掩码各一份。
- [src/embedding.ts:206-212](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L206-L212)：`this.prefill(inputNDArray, maskNDArray, this.params)` 完成真正的前向，返回形状 `(curBatchSize, maxInputSize, hidden_size)` 的张量，`await this.device.sync()` 等待 GPU 完成。变量名叫 `logits` 是历史习惯，这里实际是各位置的隐状态。

**第 5 步：取第一个 token 的隐状态**。

- [src/embedding.ts:214-228](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L214-L228)：在 CPU 上开同形状缓冲，把 GPU 结果拷回，`view` 成一维长度 `curBatchSize * maxInputSize * hidden_size` 的数组。
- [src/embedding.ts:230-238](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L230-L238)：对第 `i` 条句子，偏移 `b = i * maxInputSize * hidden_size`，切出 `[b, b + hidden_size)`——**恰好是第 0 个 token 位置的隐状态**，即注释所说的 `logits[:, 0, :]`。源码 TODO 也在追问：是否所有嵌入模型都取 `[0, :]`，如果这是 Snowflake（BERT/[CLS] 风格）特有的约定，应该写进 `mlc-chat-config.json`。这也解释了官方示例为什么要手工给输入包上 `[CLS] ... [SEP]`（见 4.3.4 节实践）。

**收尾：校验、性能与释放**。

- [src/embedding.ts:241-249](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L241-L249)：断言返回条数等于批条数；`curRoundEmbedTotalTokens` 记录真实 token 数（**不含 padding**），`curRoundEmbedTotalTime` 记录秒级耗时。
- [src/embedding.ts:272-293](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L272-L293)：三个性能 API——`getCurRoundEmbedTotalTokens/TotalTime/TokensPerSec`，最后一个就是 usage 里 `prefill_tokens_per_s` 的数据来源。
- [src/embedding.ts:252-258](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L252-L258)：`dispose()` 自内向外释放 params、prefill、vm、tvm、tokenizer——`unload()` 时由引擎统一调用（u2-l1 讲过的生命周期收尾）。
- [src/embedding.ts:268-270](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L268-L270)：`asyncLoadWebGPUPipelines` 异步编译 WebGPU shader，与 `LLMChatPipeline` 同名方法在 `reload()` 中被统一调用。

#### 4.2.4 代码实践：观察批处理与 padding

**实践目标**：通过源码数据推演，理解 `max_batch_size` 与 padding 的关系（不需要运行浏览器）。

**操作步骤**：

1. 阅读 [src/config.ts:2560-2601](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L2560-L2601) 中 4 条 Snowflake Arctic Embed 预置记录，注意注释「-b means max_batch_size this model allows. The smaller it is, the less memory the model consumes」，对比 `b32`（vram 1407.51MB / 1022.82MB）与 `b4`（539.4MB / 238.71MB）的显存差距。
2. 手工推演：假设加载 `snowflake-arctic-embed-m-q0f32-MLC-b4`（`maxBatchSize = 4`），一次性传入 6 条长度分别为 3、5、2、8、4、4 的 token 序列。按 [src/embedding.ts:150-158](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L150-L158) 的分批规则写出两批的划分。
3. 再按 [src/embedding.ts:159-189](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L159-L189) 推演每批的 `maxInputSize` 与 padding 后的展平数组长度。

**需要观察的现象**：你在纸面上得到的分批与矩阵形状。

**预期结果**：第一批为前 4 条（长 3、5、2、8），`maxInputSize = 8`，展平长度 `4 × 8 = 32`；第二批为后 2 条（长 4、4），`maxInputSize = 4`，展平长度 `2 × 4 = 8`；共两次 GPU 前向，`totalNumTokens = 3+5+2+8+4+4 = 26`（不含 padding）。可自行对照源码验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `EmbeddingPipeline` 要强制 `prefillChunkSize === contextWindowSize`，而生成式 `LLMChatPipeline` 反而要支持小于上下文窗口的分块？

答案：生成式的 prompt 可能长达数千 token，分块预填充（u3-l3 的 `prefill_chunk_size`）可以限制单次前向的显存峰值，还能边填边上报进度。嵌入模型追求的是整句一次性编码（且预置模型上下文只有 512），分块会把一句话拆开、破坏每个位置隐状态的语义，因此直接不支持，配置不符即在构造期抛 `EmbeddingChunkingUnsupportedError`。

**练习 2**：如果把 100 条句子传给 `b4` 模型，会发生什么？返回的向量和 usage 分别是什么样？

答案：不会报错。`embedStep` 的外层循环会切成 25 批、做 25 次 GPU 前向，最后按顺序拼成 100 条向量返回；`usage.prompt_tokens` 是 100 句的真实 token 总数（不含 padding），`extra.prefill_tokens_per_s` 为总 token 数除以整轮耗时。

**练习 3**：嵌入管线复用的函数也叫 `prefill`，它和生成式管线里的 `prefill` 输出用法有何不同？

答案：两者都把 token 序列送入模型做一次前向。生成式管线取**最后一个位置**的 logits 采样下一个 token，并维护 KV cache 供后续 decode 复用；嵌入管线取**第一个位置**（`[CLS]`）的隐状态 `logits[:, 0, :]` 作为整句向量，且不维护任何跨调用的会话状态。

### 4.3 engine.embedding 分发与模型类型限制

#### 4.3.1 概念说明

引擎层要回答三个问题：

1. **该构造哪条管线？**——`reload()` 依据 `ModelRecord.model_type` 决定，嵌入模型构造 `EmbeddingPipeline`。
2. **这次请求该找哪个模型？**——单模型可省略 `model` 字段，多模型必填，由 `getModelIdToUse` 裁决。
3. **这个模型真的能做嵌入吗？**——双重检查：管线类型检查 + 模型记录的 `model_type` 检查，不匹配就抛错。

第三个问题引出本讲最容易踩坑的一组错误：`IncorrectPipelineLoadedError` 与 `EmbeddingUnsupportedModelError`，它们触发的条件不同（见 4.3.4 实践）。

#### 4.3.2 核心流程

```
reload(modelId)
    │  查 ModelRecord
    ├── model_type === ModelType.embedding ──→ new EmbeddingPipeline(tvm, tokenizer, config)
    └── 其他（LLM/VLM）──────────────────────→ new LLMChatPipeline(...)

engine.embedding(request)
    │ 0. getEmbeddingStates → getModelIdToUse 选模型 → 双重类型检查
    │ 0.5 获取该模型的互斥锁（防并发请求打架）
    │ 1. pipeline.embedStep(request.input)
    │ 2. 组装 CreateEmbeddingResponse（data / usage）
    └─ finally 释放锁
```

#### 4.3.3 源码精读

**reload 中的管线分流**。

- [src/engine.ts:399-413](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L399-L413)：`reload()` 在下载完 tokenizer 与权重后构造管线——`modelRecord.model_type === ModelType.embedding` 则 `new EmbeddingPipeline(...)`，否则 `new LLMChatPipeline(...)`；随后统一 `asyncLoadWebGPUPipelines()` 编译 shader，并把管线与专属锁注册进以 modelId 为键的 Map（u2-l1 讲过的四张状态表）。注意 L400-401 的 TODO：如果用户给嵌入模型配了没写 `model_type` 的自定义记录，引擎无法在加载期发现误用。
- [src/config.ts:251-255](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L251-L255)：`ModelType` 枚举只有三个值 `LLM`/`embedding`/`VLM`；[src/config.ts:284](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L284) 显示 `ModelRecord.model_type` 是可选字段——**不写就默认按 LLM 处理**，这是自定义模型记录时的常见坑。

**`engine.embedding()` 主体**。

- [src/engine.ts:1104-1112](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1104-L1112)：第 0 步先 `getEmbeddingStates` 选出模型与管线，再做 4.1 节的协议校验——**选模型在加锁之前**，非法请求不必占用锁。
- [src/engine.ts:1114-1116](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1114-L1116)：第 0.5 步获取该模型专属的 `CustomLock`，与 chat/completion 一致——同一模型的请求串行执行，防止 GPU 上的前向交叉（u2-l3 讲过同样的锁在流式路径贯穿整个生成周期）。
- [src/engine.ts:1119-1121](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1119-L1121)：第 1 步调用 `embedStep(request.input)` 拿到向量数组。
- [src/engine.ts:1123-1146](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1123-L1146)：第 2 步组装响应——每条向量包成 `{embedding, index, object: "embedding"}`；`model` 填选中模型的 modelId；`usage` 的两个字段都取 `getCurRoundEmbedTotalTokens()`，`extra.prefill_tokens_per_s` 取 `getCurRoundEmbedTokensPerSec()`。
- [src/engine.ts:1147-1149](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1147-L1149)：`finally` 释放锁，异常路径也不会把锁泄漏。

**模型路由与双重类型检查**。

- [src/engine.ts:1210-1219](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1210-L1219)：`getEmbeddingStates` 是 `getModelStates` 指定 `ModelType.embedding` 的薄封装；与之对称的 `getLLMStates`（L1199-1208）服务 chat/completion。
- [src/engine.ts:1229-1242](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1229-L1242)：`getModelStates` 第 0 步用 `getModelIdToUse` 在已加载模型中裁决。
- [src/support.ts:227-256](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L227-L256)：裁决规则——没加载任何模型抛 `ModelNotLoadedError`；指定了 `model` 但未加载抛 `SpecifiedModelNotFoundError`；未指定且加载了多个抛 `UnclearModelToUseError`；否则唯一模型胜出。这解释了协议层注释「多模型时 model 必填」。
- [src/engine.ts:1246-1269](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1246-L1269)：嵌入分支的**双重检查**，顺序很关键：
  1. L1256-1262：已加载的管线不是 `EmbeddingPipeline`（比如是 `LLMChatPipeline`）→ 抛 `IncorrectPipelineLoadedError`（[src/error.ts:590-602](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L590-L602)）；
  2. L1263-1268：再查模型记录的 `model_type` 是否为 `embedding`，不是则抛 `EmbeddingUnsupportedModelError`（[src/error.ts:506-515](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L506-L515)），错误消息明确提示「要么加载嵌入模型，要么在 ModelRecord 里声明 model type」。

  由于 `reload()` 严格按 `model_type` 构造管线，正常流程下两个条件总是同时成立；第 2 道检查是防御性的——典型触发场景是自定义模型记录漏写 `model_type` 却手工构造了嵌入管线。**用普通 chat 模型调用 `embeddings.create()`，实际先撞上的是第 1 道检查 `IncorrectPipelineLoadedError`**（因为 chat 模型加载的是 `LLMChatPipeline`）。这一点与直觉相反，务必以 4.3.4 的实践验证为准。

- [src/engine.ts:1271-1286](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1271-L1286)：最后取出该模型的 ChatConfig 并确认锁已初始化，返回三元组。

**门面注册与 Worker 支持**。

- [src/engine.ts:121-122](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L121-L122) 与 [src/engine.ts:163-165](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L163-L165)：构造函数里 `this.embeddings = new API.Embeddings(this)`——第三张 OpenAI 风格门面在此挂载。
- [src/message.ts:29](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L29) 与 [src/web_worker.ts:253-262](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L253-L262)：消息协议中有 `"embedding"` 种类（参数封装为 [src/message.ts:95-97](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L95-L97) 的 `EmbeddingParams`），Worker 侧 handler 直接转调 `this.engine.embedding(params.request)`——意味着嵌入接口在 `WebWorkerMLCEngine` 下同样可用（Worker 架构详见 u5 单元）。

#### 4.3.4 代码实践：文本相似度小工具 + 错误观察（本讲主实践）

**实践目标**：

1. 用官方示例的思路写一个三段文本的两两余弦相似度工具；
2. 实测「用 chat 模型调用 embedding 接口」抛出的到底是什么错误。

**操作步骤**：

1. 进入示例目录并启动（与 u1-l2 相同的 Parcel 流程）：

   ```bash
   cd examples/embeddings
   npm install
   npm start
   ```

2. 阅读官方示例的输入预处理：[examples/embeddings/src/embeddings.ts:55-69](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/embeddings/src/embeddings.ts#L55-L69) 给每条输入手工包上 `[CLS] ... [SEP]`——配合 4.2.3 节「取第 0 个 token 隐状态」的实现，这是 BERT 风格模型的用法约定；查询侧还额外加了 `query_prefix` 前缀（Snowflake 模型的推荐用法）。[examples/embeddings/src/embeddings.ts:72-92](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/embeddings/src/embeddings.ts#L72-L92) 是最简调用路径：`CreateMLCEngine` 加载 `snowflake-arctic-embed-m-q0f32-MLC-b4`，`engine.embeddings.create({ input: documents })` 一次拿回全部向量。

3. 参照它写自己的页面（示例代码，放到你的示例工程里）：

   ```ts
   // similarity.ts —— 示例代码，非仓库原有文件
   import * as webllm from "@mlc-ai/web-llm";

   const engine = await webllm.CreateMLCEngine("snowflake-arctic-embed-s-q0f32-MLC-b4");

   const texts = [
     "[CLS] The cat sat on the mat. [SEP]",
     "[CLS] A feline rested on the rug. [SEP]",
     "[CLS] Quarterly revenue grew by 12%. [SEP]",
   ];
   const reply = await engine.embeddings.create({ input: texts });

   function cosine(a: number[], b: number[]): number {
     let dot = 0, na = 0, nb = 0;
     for (let i = 0; i < a.length; i++) {
       dot += a[i] * b[i];
       na += a[i] * a[i];
       nb += b[i] * b[i];
     }
     return dot / (Math.sqrt(na) * Math.sqrt(nb));
   }

   const v = reply.data.map((d) => d.embedding);
   console.log("向量维度:", v[0].length);
   console.log("句1-句2:", cosine(v[0], v[1])); // 语义相近
   console.log("句1-句3:", cosine(v[0], v[2])); // 语义无关
   console.log("usage:", reply.usage);
   ```

4. 错误观察：把第一步的模型换成任一 chat 模型（如 `gemma-2-2b-it-q4f32_1-MLC-1k`，示例 RAG 部分用到过它），加载后再调用 `engine.embeddings.create({ input: texts })`，用 `try/catch` 捕获并打印 `e.name` 与 `e.message`。

**需要观察的现象**：

- 第 3 步：控制台输出向量维度（`snowflake-arctic-embed-s` 的 hidden_size，具体数值待本地验证）、两个相似度数值、以及 `usage` 中 `prompt_tokens === total_tokens`、`extra.prefill_tokens_per_s` 的吞吐。
- 第 4 步：抛出的错误名。**预期是 `IncorrectPipelineLoadedError`**（消息形如 `EmbeddingCreateParams expects model to be loaded with EmbeddingPipeline. However, <modelId> is not loaded with this pipeline.`），而不是 `EmbeddingUnsupportedModelError`——因为 chat 模型加载的管线是 `LLMChatPipeline`，先命中 [src/engine.ts:1256-1262](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1256-L1262) 的第一道检查。`EmbeddingUnsupportedModelError` 只在「管线确实是 EmbeddingPipeline、但模型记录漏写了 `model_type: embedding`」的错配场景才会触发。

**预期结果**：句 1-句 2（猫坐垫子 vs 猫科动物趴地毯）的相似度明显高于句 1-句 3（财务话题）；错误实验稳定抛 `IncorrectPipelineLoadedError`。相似度具体数值与向量维度待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：一个引擎同时加载了嵌入模型和 chat 模型（示例 `simpleRAG` 的做法），调用 `embeddings.create()` 时必须做什么？

答案：必须传 `model` 字段指定嵌入模型的 modelId。`getModelIdToUse`（[src/support.ts:247-253](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L247-L253)）在未指定且已加载多个模型时抛 `UnclearModelToUseError`。官方示例 [examples/embeddings/src/embeddings.ts:160-176](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/embeddings/src/embeddings.ts#L160-L176) 正是向 `CreateMLCEngine` 传了模型数组，之后每次请求都带 `model` 字段。

**练习 2**：用户自定义了一条 ModelRecord，指向的 wasm 其实是嵌入模型，但忘了写 `model_type`。会发生什么？

答案：`reload()` 走 `ModelType` 默认分支，用 `LLMChatPipeline` 去加载嵌入模型（[src/engine.ts:403-412](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L403-L412)），多半在取 `decode` 等函数时失败或行为异常；即使手工让它加载了 `EmbeddingPipeline`，`embeddings.create()` 也会在 [src/engine.ts:1263-1268](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1263-L1268) 处抛 `EmbeddingUnsupportedModelError`，错误消息会提示补写 `ModelRecord.model_type`。引擎在 [src/engine.ts:400-401](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L400-L401) 留有 TODO 希望在加载期就给出更友好的提示。

**练习 3**：`engine.embedding()` 为什么先选模型、做协议校验，然后才获取互斥锁？

答案：选模型和校验都是纯 CPU 上的同步判断，不触碰 GPU 状态；把它们放在锁外，非法请求（空输入、不支持字段、模型未加载）可以立刻失败，不必排队等锁，也不会延长其他合法请求的等待时间。锁只保护真正独占 GPU 的 `embedStep` 阶段（[src/engine.ts:1114-1121](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1114-L1121)）。

## 5. 综合实践

**任务：给上一讲的对话应用加一个「语义去重」开关。**

把本讲三层知识串起来用一遍：

1. **协议层**：定义请求时只用受支持字段——`input` 用 `Array<string>` 批量传入，`encoding_format` 省略（默认 float），不碰 `dimensions`/`user`。
2. **管线层**：用一个引擎同时加载一个 chat 模型与 `snowflake-arctic-embed-s-q0f32-MLC-b4`（参考 `simpleRAG` 的双模型写法）。用户每次提问前，把新问题与历史问题批量送 `embeddings.create()`（注意给每条包 `[CLS] ... [SEP]`），计算新问题与每个历史问题的余弦相似度；若最大值超过阈值（如 0.92，具体阈值待本地调参验证），提示「与第 N 轮问题重复，直接复用该轮回答」而不再调用 chat 模型。
3. **引擎层**：所有 `embeddings.create` 与 `chat.completions.create` 都显式带 `model` 字段（多模型必填）；观察两类请求各自 `usage` 的差异——嵌入的 `usage` 只有 token 数与 `prefill_tokens_per_s`，没有生成的 `completion_tokens`。

**验收要点**：

- 换两种说法问同一个问题（如「怎么退货」vs「退款流程是什么」），开关能正确识别为重复；
- 故意停用嵌入模型（`engine.unload()` 后仅重载 chat 模型）再触发开关，页面应捕获 `ModelNotLoadedError` 或 `SpecifiedModelNotFoundError` 并降级为直接提问，而不是白屏；
- 记录一次批量嵌入的 `prefill_tokens_per_s`，与自己手算的 `totalTokens / 耗时` 对照。

## 6. 本讲小结

- WebLLM 的嵌入接口完全对齐 OpenAI `embeddings.create()`：`Embeddings` 门面只做转发，真正实现在 `engine.embedding()`；请求校验由 `postInitAndCheckFields` 的三道检查（不支持字段、base64、空输入）在进入 GPU 前完成。
- `input` 有四种形态，`Array<number>` 是「一句话的 token 序列」而非多条输入；`model` 字段在多模型引擎中必填。
- `EmbeddingPipeline` 与 `LLMChatPipeline` 分工明确：嵌入只用 `prefill` 一次前向，按 `max_batch_size` 自动分批，padding 补零配 0/1 掩码，最终取**第一个 token 位置的隐状态** `logits[:, 0, :]` 作为整句向量；它强制 `prefillChunkSize === contextWindowSize`（不支持分块）、禁用滑动窗口、超长输入直接抛错。
- 嵌入管线无会话状态，`usage.prompt_tokens` 统计真实 token（不含 padding），`extra.prefill_tokens_per_s` 来自管线的性能计数器。
- 模型路由靠 `model_type`：`reload()` 据此构造管线，`getModelStates` 再做双重检查——用 chat 模型调嵌入抛的是 `IncorrectPipelineLoadedError`；`EmbeddingUnsupportedModelError` 是「管线与记录错配」的防御性检查，常源于自定义记录漏写 `model_type: embedding`。
- 预置嵌入模型共 4 条（Snowflake Arctic Embed m/s × b4/b32），批上限越大显存占用越高；`"embedding"` 也在 Worker 消息协议中，嵌入可在 Worker 中使用。

## 7. 下一步学习建议

至此单元二（引擎接口层）完结，你已经掌握 MLCEngine 的全部公开请求入口：chat、流式 chat、completion、embedding。接下来两条路：

1. **进入单元三（推荐主线）**：u3-l1 开始精读 `LLMChatPipeline` 的构造与 tvmjs 运行时——本讲你已经见过 `EmbeddingPipeline` 如何创建 VM、取 PackedFunc、上传 NDArray，这些机制在 `LLMChatPipeline` 中完全同源，是很好的铺垫。
2. **横向扩展**：若你想先看分发侧，可跳到 u4-l1 的缓存机制（`webllm/model` 三个缓存作用域同样服务嵌入模型权重）；若对多模型引擎感兴趣，u7-l2 会展开 `reload`/`unload` 的资源管理细节。

建议顺手阅读 [examples/embeddings/src/embeddings.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/embeddings/src/embeddings.ts) 中的 `simpleRAG()` 函数——单引擎同时驱动嵌入模型与 chat 模型完成检索增强生成，是本讲内容的最佳综合示范。
