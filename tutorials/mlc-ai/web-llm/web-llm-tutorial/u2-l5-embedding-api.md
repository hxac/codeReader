# u2-l5 embedding 嵌入接口与 EmbeddingPipeline

## 1. 本讲目标

前几讲我们一直在和「生成文本」打交道：`chatCompletion` 产出对话、`completion` 产出补全。本讲转向另一类完全不同的任务——**embedding（嵌入）**：把一段文本变成一个固定长度的向量，用于语义搜索、聚类、RAG 检索等场景。

学完本讲，你应该能够：

1. 说清 `EmbeddingPipeline` 与 `LLMChatPipeline` 的分工差异——为什么嵌入模型不需要 KV cache、不需要解码循环、不需要采样。
2. 掌握 `EmbeddingCreateParams` 的 `input` 四种形态、`model` 的省略规则、`encoding_format` 的限制，以及 `dimensions`/`user` 为何被拒绝。
3. 理解 `engine.embedding` 从请求进来、经模型路由、到管线前向、最后组装 OpenAI 风格响应的完整分发链路。
4. 了解 embedding 场景下的一族专属错误（空输入、超上下文、不支持 base64、chat 模型不可用等）以及它们各自在源码中的触发位置。

## 2. 前置知识

### 2.1 什么是 embedding

embedding 模型把一段文本映射成一个向量（一个浮点数数组，例如 576 维）。它的核心用途是**度量语义相似度**：两段意思相近的文本，其向量的夹角也小。常用的度量是余弦相似度：

\[
\text{similarity}(A, B) = \frac{A \cdot B}{\|A\| \, \|B\|}
\]

值域为 \([-1, 1]\)，越接近 1 表示方向越一致、语义越相近。本讲综合实践会用到这个公式。

### 2.2 嵌入模型 vs 生成式 LLM

| 维度 | 生成式 LLM（如 Llama） | 嵌入模型（如 Snowflake Arctic Embed） |
| --- | --- | --- |
| 输出 | 逐 token 生成的文本 | 一个向量（整段文本的语义浓缩） |
| 推理方式 | prefill 一次 + decode 循环 N 次 | 只有一次前向（prefill），无 decode |
| KV cache | 需要，且跨轮可复用 | 不需要 |
| 采样参数 | temperature、top_p 等 | 无，不涉及采样 |
| 多输入并发 | 一次一条 | 支持 batch（批量前向） |

一句话：嵌入模型是「只做 prefill、只要第一个位置输出的 Transformer 编码器」。

### 2.3 batch、padding 与 attention mask

GPU 前向计算喜欢规整的矩形输入，但一批文本的 token 数各不相同。惯用做法是：

- **padding**：把短句子补 0，对齐到本批最长句的长度；
- **attention mask**：并行给一个同形状的 0/1 数组，1 表示真实 token，0 表示补位，让模型忽略补位部分。

这两个数组会在 `EmbeddingPipeline.embedStep` 里亲手构造，是本讲源码精读的重点之一。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/openai_api_protocols/embedding.ts` | OpenAI 风格的嵌入协议定义 | `Embeddings` 门面类、请求/响应类型、`postInitAndCheckFields` 校验 |
| `src/embedding.ts` | 嵌入推理管线（约 295 行） | 构造期检查、`embedStep` 批量前向、性能统计 |
| `src/engine.ts` | 引擎编排 | `reload` 时的管线分支、`embedding()` 方法、`getModelStates` 路由 |
| `src/config.ts` | 模型配置 | `ModelType` 枚举、4 条 embedding 模型记录 |
| `src/error.ts` | 错误定义 | embedding 专属错误族 |
| `src/support.ts` | 公共工具 | `getModelIdToUse` 的选模型规则 |
| `examples/embeddings/` | 官方示例 | 相似度计算、LangChain 集成、RAG 演示 |

## 4. 核心概念与源码讲解

### 4.1 Embeddings 协议类：OpenAI 风格的门面

#### 4.1.1 概念说明

与 `chat.completions`、`completions` 一样，WebLLM 把嵌入接口也做成了 OpenAI SDK 的形状：你调用 `engine.embeddings.create(request)`，它内部只是原样转发给 `engine.embedding(request)`。这个设计的意义在第 2 单元讲过——**让熟悉 OpenAI SDK 的开发者零成本迁移**，也让 LangChain 这类「面向 OpenAI 接口编程」的库能直接接入（官方示例正是这么做的）。

#### 4.1.2 核心流程

```text
用户代码
  └─ engine.embeddings.create(request)   // OpenAI 风格门面
       └─ engine.embedding(request)      // 真正的实现（见 4.3）
```

请求字段的处理规则：

- `input`：必填，支持四种形态（见下）；
- `model`：单模型引擎可省略；多模型加载时必填，否则抛 `UnclearModelToUseError`；
- `encoding_format`：只支持 `"float"`，传 `"base64"` 报错；
- `dimensions`、`user`：属于不支持字段，出现在请求中直接抛 `UnsupportedFieldsError`。

#### 4.1.3 源码精读

**门面类只有 14 行，纯转发：**

[examples/../src/openai_api_protocols/embedding.ts:25-38](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/embedding.ts#L25-L38) —— `Embeddings` 类持有 `MLCEngineInterface`，`create()` 直接 `return this.engine.embedding(request)`。这和 `Chat`、`Completions` 门面如出一辙。

**`input` 的四种形态：**

[src/openai_api_protocols/embedding.ts:111-118](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/embedding.ts#L111-L118) —— 类型为 `string | Array<string> | Array<number> | Array<Array<number>>`，分别对应：单条文本、多条文本、单条 token 数组、多条 token 数组。也就是说你可以绕过 tokenizer，直接喂自己切好的 token id。

**`model` 字段的省略规则：**

[src/openai_api_protocols/embedding.ts:120-128](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/embedding.ts#L120-L128) —— `model` 等于 `ModelRecord.model_id`；只加载了一个模型时可省略，多个模型加载时必填。注意注释里的提醒：需要先 `CreateMLCEngine(model)` 或 `engine.reload(model)` 把模型加载好。

**不支持字段的清单与校验：**

[src/openai_api_protocols/embedding.ts:154-173](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/embedding.ts#L154-L173) —— `EmbeddingCreateParamsUnsupportedFields` 显式列出 `dimensions` 和 `user`；`postInitAndCheckFields` 用 `field in request` 逐个探测，命中即抛 `UnsupportedFieldsError`。这是「与其静默忽略，不如大声报错」的设计——避免用户以为 `dimensions` 生效了实际却被丢弃。

**base64 与空输入的检查：**

[src/openai_api_protocols/embedding.ts:176-197](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/embedding.ts#L176-L197) —— `encoding_format == "base64"` 抛 `EmbeddingUnsupportedEncodingFormatError`；空字符串、空数组、数组中的空字符串/空 token 数组都抛 `EmbeddingInputEmptyError`。注意第 182-196 行的分支结构：`typeof input === "string"` 只查空串，数组则逐项检查。

**响应结构：**

[src/openai_api_protocols/embedding.ts:40-59](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/embedding.ts#L40-L59) —— `CreateEmbeddingResponse` 含 `data`（`Embedding` 数组）、`model`、`object: "list"`、`usage`。其中 `usage.extra.prefill_tokens_per_s`（[第 77-87 行](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/openai_api_protocols/embedding.ts#L77-L87)）是 WebLLM 特有字段，OpenAI 原版没有——它直接来自管线的性能统计。

#### 4.1.4 代码实践

**实践：用类型系统摸清请求边界（源码阅读型，无需 GPU）**

1. 实践目标：不运行模型，仅通过阅读协议源码与类型定义，列出 `embeddings.create` 所有「能传」与「不能传」的字段。
2. 操作步骤：
   - 打开 `src/openai_api_protocols/embedding.ts`，通读 `EmbeddingCreateParams` 的每个字段注释；
   - 对照 `EmbeddingCreateParamsUnsupportedFields` 清单；
   - 在编辑器里新建一个临时 `.ts` 文件（放在仓库外即可），故意写下 `engine.embeddings.create({ input: "hi", dimensions: 64 } as any)`，观察不加 `as any` 时 TypeScript 是否已经从类型层面提示问题（`dimensions` 在类型里存在但运行时会抛错——体会「类型允许 ≠ 运行支持」）。
3. 需要观察的现象：TypeScript 编译器对该字段没有报错（因为类型定义里有它），但源码的 `postInitAndCheckFields` 会在运行期拦截。
4. 预期结果：整理出一张「字段 × 类型层面 × 运行层面」两列对照表；`dimensions`、`user` 在类型层面存在、运行层面抛 `UnsupportedFieldsError`；`encoding_format: "base64"` 运行层面抛 `EmbeddingUnsupportedEncodingFormatError`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Embeddings.create` 只有一层薄转发，而不在这里做校验？

**参考答案**：这是门面模式的一致选择——`Chat`、`Completions`、`Embeddings` 三个门面都只负责「像 OpenAI」，校验统一放在协议层的 `postInitAndCheckFields`、执行统一放在引擎的 `embedding()`。这样主线程引擎与 Worker 引擎（`WebWorkerMLCEngine`）共享同一套协议校验，行为不会分叉。

**练习 2**：请求里传 `input: []`（空数组）和 `input: [""]`（含空字符串的数组）分别会发生什么？

**参考答案**：两者都抛 `EmbeddingInputEmptyError`。前者命中 `postInitAndCheckFields` 第 186-189 行的 `input.length === 0` 分支；后者命中第 190-196 行循环里的 `curInput.length === 0` 分支。即使绕过了协议层，`embedStep` 里还有第二道防线（`src/embedding.ts:108-110`）。

### 4.2 EmbeddingPipeline：单次前向的嵌入管线

#### 4.2.1 概念说明

`EmbeddingPipeline` 是与 `LLMChatPipeline` 平行的另一条管线，二者的分工是本讲最核心的概念。对照着看：

| 能力 | LLMChatPipeline | EmbeddingPipeline |
| --- | --- | --- |
| 使用的 tvmjs 函数 | `prefill`、`decode`、`softmax...`、`sample...` 等一组 | 只有 `prefill` 一个 |
| KV cache | 有，逐轮追加 | 无 |
| 逐 token 循环 | decodeStep 循环直到终止条件 | 没有，一次前向即结束 |
| 会话状态 | `Conversation`，可跨轮复用 | 无状态，每次 `embedStep` 独立 |
| batch | 每次一条输入 | 支持批量，按 `max_batch_size` 分批 |
| 取哪个位置的输出 | 最后一个 token 的 logits（预测下一个词） | **第一个 token** 的输出向量（CLS 式池化） |

为什么嵌入只取第一个 token？这类 BERT 风格的编码器模型在训练时就是拿特殊的首 token（如 `[CLS]`）位置的隐状态作为整句表示，推理时沿用同一约定即可。

#### 4.2.2 核心流程

`embedStep` 的完整流程（对应源码第 95-250 行的编号注释）：

```text
输入: string | string[] | number[] | number[][]
  │
  ├─ 0. 重置本轮性能计数器
  ├─ 1. 输入归一化：全部转成 number[][]（必要时 tokenizer.encode）
  │      - 空输入 → EmbeddingInputEmptyError
  ├─ 2. 逐条检查 token 数 ≤ contextWindowSize
  │      - 超限 → EmbeddingExceedContextWindowSizeError
  ├─ 3. 按 maxBatchSize 分批，对每一批：
  │      3.1 切出当前批 curBatch
  │      3.2 求本批最长输入 maxInputSize
  │      3.3 构造 padding 后的输入数组与 attention mask（0/1）
  │      3.4 上传 GPU，view 成 (batchSize, maxInputSize) 的 int32 张量
  │      3.5 调 prefill 前向 → logits 形状 (batchSize, maxInputSize, hidden_size)
  │      3.6 拷回 CPU 展平成一维 Float32Array
  │      3.7 对每条输入取第 0 个 token 的 hidden_size 维向量，push 进结果
  │
  └─ 返回 number[][]，同时记录本轮耗时与 token 数
```

一个直观的例子：batch 为 2、`maxInputSize` 为 4、`hidden_size` 为 576 时，第 \(i\) 条输入的向量在一维数组中的起止下标为：

\[
b_i = i \times 4 \times 576, \quad e_i = b_i + 576
\]

即只取每条输入「自己的第 0 个 token」那一段。

#### 4.2.3 源码精读

**构造函数：创建 VM、取函数、读元数据、加载权重、五道检查：**

[src/embedding.ts:33-93](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L33-L93) —— 与 `LLMChatPipeline` 相比极简：创建 VirtualMachine 后只取出 `prefill` 一个 PackedFunc（第 45-47 行）；第 50-53 行从 wasm 内嵌的 `_metadata` 函数读出编译期元数据（参数名列表、`max_batch_size`、`prefill_chunk_size`）；第 56-62 行按名字从缓存取回模型权重。

**构造期的约束检查（嵌入模型的三条硬规矩）：**

[src/embedding.ts:74-91](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L74-L91) —— 依次检查：不支持滑动窗口（`EmbeddingSlidingWindowError`）、`maxBatchSize`/`contextWindowSize`/`prefillChunkSize` 必须为正（`MinValueError`），以及最关键的一条——`prefillChunkSize` 必须等于 `contextWindowSize`（`EmbeddingChunkingUnsupportedError`）。原因：生成式管线可以长 prompt 分块预填充、块间靠 KV cache 衔接，而嵌入管线没有 KV cache，分块后无法拼回一个完整的句向量，所以干脆要求编译期就不分块。

**输入归一化：四种形态统一成 `number[][]`：**

[src/embedding.ts:105-131](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L105-L131) —— 字符串直接 `tokenizer.encode`；数组则逐项判断：子数组视为已切好的 token、字符串则编码、裸数字收集进 `tempInputs` 最后合并成一条。注意第 106-107 行的注释：不用 `input.every` 是为了绕开 TypeScript 的一个类型推断缺陷（microsoft/TypeScript#33591）。

**上下文窗口检查：**

[src/embedding.ts:136-145](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L136-L145) —— 逐条累计 token 数并检查是否超过 `contextWindowSize`。预置的 snowflake 模型上下文是 512（从 wasm 文件名 `ctx512` 也能看出来）。第 134-135 行的 TODO 注释值得留意：`tokenizer.encode` 可能隐式截断超长输入，行为尚未完全确认。

**padding 与 attention mask 的手工构造：**

[src/embedding.ts:167-189](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L167-L189) —— 对每条输入：真实 token 原样 push、对应 mask push 一串 1；然后补 `maxInputSize - len` 个 0、mask 同样补 0。最后断言两个数组长度都等于 `curBatchSize * maxInputSize`，不等则抛内部错误。这是 2.3 节概念在源码里的逐行落地。

**上传 GPU 并前向：**

[src/embedding.ts:190-212](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L190-L212) —— 先 `tvm.empty` 建一维 int32 缓冲、`copyFrom` 灌数据、再 `view` 成 `(curBatchSize, maxInputSize)` 二维形状（第 197、204 行）；随后一次 `this.prefill(inputNDArray, maskNDArray, this.params)` 完成前向，返回张量形状为 `(curBatchSize, maxInputSize, hidden_size)`，`await this.device.sync()` 等待 GPU 完成。

**取第一个 token 的向量（CLS 池化）：**

[src/embedding.ts:214-238](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L214-L238) —— 把 GPU 张量拷回 CPU、`view` 成一维 `Float32Array`（第 222-224 行），然后第 234-238 行按本节开头的下标公式 `slice(b, e)` 切出每条输入的向量。第 232-233 行的 TODO 提醒：目前假设所有模型都用 `[0,:]` 池化，若这是 snowflake 特有的，将来需要写进模型配置。

**性能统计：**

[src/embedding.ts:245-247](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L245-L247) 与 [src/embedding.ts:277-293](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L277-L293) —— `curRoundEmbedTotalTime` 记录 `embedStep` 全程秒数，`curRoundEmbedTotalTokens` 只计真实 token（不含 padding），`getCurRoundEmbedTokensPerSec()` 用两者相除。引擎层正是用这三个 getter 填充响应里的 `usage`。

#### 4.2.4 代码实践

**实践：用 Node 模拟 embedStep 的 padding 逻辑（可本地运行，无需 GPU）**

1. 实践目标：脱离 GPU，亲手复现 `embedStep` 第 3.2-3.7 步的纯计算部分，确认自己对下标公式的理解。
2. 操作步骤：
   - 新建一个独立脚本（示例代码，非项目源码）：

   ```ts
   // 示例代码：模拟 EmbeddingPipeline 的 batch padding 与取向量逻辑
   const batch = [[1, 2, 3], [4, 5]];        // 两条输入，maxInputSize = 3
   const maxInputSize = Math.max(...batch.map(x => x.length));
   const hidden = 2;                          // 假设 hidden_size = 2
   const flat: number[] = [], mask: number[] = [];
   for (const seq of batch) {
     flat.push(...seq); mask.push(...Array(seq.length).fill(1));
     const pad = Array(maxInputSize - seq.length).fill(0);
     flat.push(...pad); mask.push(...pad);
   }
   console.log("padded:", flat, "mask:", mask);
   // 假想 logits 是 flat 中每个位置展开成 hidden 维，这里直接用位置验证切片
   for (let i = 0; i < batch.length; i++) {
     const b = i * maxInputSize * hidden;
     console.log(`第 ${i} 条向量的下标区间: [${b}, ${b + hidden})`);
   }
   ```
   - 用 `npx tsx 上述文件.ts` 或 `node` 运行。
3. 需要观察的现象：`padded` 应为 `[1,2,3,4,5,0]`，`mask` 应为 `[1,1,1,1,1,0]`；两条向量的下标区间分别是 `[0,2)` 和 `[6,8)`。
4. 预期结果：第二组区间从 6 开始而非 4——因为第 0 条输入占了 `3 × 2 = 6` 个位置。理解了这一点，就理解了 [src/embedding.ts:234-238](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L234-L238) 为什么这么切。

#### 4.2.5 小练习与答案

**练习 1**：嵌入管线为什么强制 `prefillChunkSize === contextWindowSize`？

**参考答案**：分块预填充依赖 KV cache 把前一块的计算结果带给下一块（LLMChatPipeline 的做法），而 EmbeddingPipeline 没有 KV cache；若允许分块，各块的中间表示无处存放，最终句向量无法等价于整句一次前向的结果，所以源码在 [src/embedding.ts:86-91](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L86-L91) 直接抛 `EmbeddingChunkingUnsupportedError` 拒绝。

**练习 2**：一次请求传入 10 条文本，但模型是 `b4` 版本，会发生什么？

**参考答案**：`embedStep` 第 153 行的 `for (let begin = 0; begin < batchSize; begin += this.maxBatchSize)` 会把 10 条切成 3 批（4+4+2），每批各做一次 GPU 前向，结果拼起来仍是 10 个向量。示例代码 [examples/embeddings/src/embeddings.ts:73-77](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/embeddings/src/embeddings.ts#L73-L77) 的注释也点明了：b4 的含义就是编译期最大批为 4，批越大越占显存。

**练习 3**：`getCurRoundEmbedTotalTokens` 统计的 token 数包含 padding 吗？

**参考答案**：不包含。`curRoundEmbedTotalTokens` 在 [src/embedding.ts:136-138](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/embedding.ts#L136-L138) 于 padding 发生之前、按每条输入的真实长度累加；字段声明处的注释（第 30 行）也写明 "excludes padded tokens for batching"。

### 4.3 engine.embedding 分发：从请求到响应

#### 4.3.1 概念说明

引擎层要回答三个问题：**用哪个模型**（多模型引擎下必须路由）、**这个模型配的是哪种管线**（嵌入请求不能落到 LLMChatPipeline 上）、**如何串起校验、加锁、前向、组装**。这三件事分别由 `getModelStates`、构造期分支和 `embedding()` 方法完成。

还有一个前置事实：`reload` 加载模型时就按 `ModelRecord.model_type` 决定了实例化哪条管线，这是整个分发的源头。

#### 4.3.2 核心流程

```text
CreateMLCEngine("snowflake-arctic-embed-m-q0f32-MLC-b4")
  └─ reload()
       └─ modelRecord.model_type === ModelType.embedding ?
            ├─ 是 → new EmbeddingPipeline(tvm, tokenizer, config)   // src/engine.ts:403-404
            └─ 否 → new LLMChatPipeline(...)                          // src/engine.ts:405-412

engine.embedding(request)
  ├─ 0. getEmbeddingStates → getModelStates(ModelType.embedding, request.model)
  │      a. getModelIdToUse 选模型（support.ts:227-256）
  │      b. 管线必须是 EmbeddingPipeline，否则 IncorrectPipelineLoadedError
  │      c. 模型记录的 model_type 必须是 embedding，否则 EmbeddingUnsupportedModelError
  ├─ 0.5 postInitAndCheckFieldsEmbedding 校验请求字段
  ├─ 0.8 获取该模型的互斥锁（同一模型一次只处理一个请求）
  ├─ 1. pipeline.embedStep(request.input) → number[][]
  ├─ 2. 组装 data / model / object / usage（含 extra.prefill_tokens_per_s）
  └─ finally 释放锁
```

#### 4.3.3 源码精读

**管线分叉点在 reload：**

[src/engine.ts:399-413](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L399-L413) —— `modelRecord.model_type === ModelType.embedding` 时走 `EmbeddingPipeline`，否则走 `LLMChatPipeline`；两条管线随后都调 `asyncLoadWebGPUPipelines()` 编译 WebGPU shader，再注册进 `loadedModelIdToPipeline`。第 400-401 行的 TODO 注释坦承：目前如果用户把嵌入模型的 `model_type` 写错成 LLM，会一路走到 LLMChatPipeline 里才在别处失败，缺少前置提醒。

**`model_type` 从哪来：**

[src/config.ts:251-255](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L251-L255) —— `ModelType` 枚举只有三个值：`LLM`、`embedding`、`VLM`。预置配置里带 `model_type: ModelType.embedding` 的记录共 4 条，全部是 snowflake-arctic-embed 家族（`s`/`m` 两档模型 × `b4`/`b32` 两种批大小），见 [src/config.ts:2562-2601](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L2562-L2601)。其中 `snowflake-arctic-embed-s-q0f32-MLC-b4` 的 `vram_required_MB` 仅 238.71，是全家最轻的。

**embedding() 主流程：**

[src/engine.ts:1104-1150](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1104-L1150) —— 与 `chatCompletion` 的「锁在流式生成器里释放」不同，这里是最朴素的同步结构：第 1115-1116 行先 `await lock.acquire()` 拿到该模型的互斥锁（保证同一模型不并发前向），第 1120-1121 行一次 `embedStep` 拿到全部向量，第 1126-1133 行把它们包装成 `Embedding` 对象数组（`index` 即输入序号），第 1138-1145 行用管线的三个性能 getter 填 `usage`——注意 `prompt_tokens` 与 `total_tokens` 相等（没有生成的 token），`prefill_tokens_per_s` 是 WebLLM 扩展字段。第 1147-1149 行的 `finally` 保证异常时也释放锁。

**模型路由的三重检查：**

[src/engine.ts:1229-1269](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1229-L1269) —— `getModelStates` 先用 `getModelIdToUse`（实现在 [src/support.ts:227-256](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L227-L256)，规则：未加载模型抛 `ModelNotLoadedError`；指定了未加载的模型抛 `SpecifiedModelNotFoundError`；未指定且加载了多个模型抛 `UnclearModelToUseError`）确定 `selectedModelId`；然后对 embedding 请求做两道检查——第 1256-1262 行要求管线 `instanceof EmbeddingPipeline`，否则抛 `IncorrectPipelineLoadedError`；第 1263-1268 行再查模型记录的 `model_type`，不匹配抛 `EmbeddingUnsupportedModelError`。

**这里有一个容易被误解的细节**：如果引擎里只加载了一个 chat 模型就去调 `embeddings.create`，第 1256 行的 `instanceof` 检查会**先**命中，抛出的是 `IncorrectPipelineLoadedError`（定义在 [src/error.ts:590-602](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L590-L602)），而不是 `EmbeddingUnsupportedModelError`（[src/error.ts:506-515](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L506-L515)，后者是「管线确实是 EmbeddingPipeline、但模型记录 `model_type` 未标 embedding」的防御性双保险，例如自定义 ModelRecord 标错类型又恰好加载了嵌入 wasm 的边缘场景）。

**错误族一览（本讲涉及部分）：**

| 错误类 | 触发条件 | 源码位置 |
| --- | --- | --- |
| `EmbeddingInputEmptyError` | 空字符串/空数组/数组含空项 | `src/openai_api_protocols/embedding.ts:181-197`、`src/embedding.ts:108-110` |
| `EmbeddingUnsupportedEncodingFormatError` | `encoding_format: "base64"` | `src/openai_api_protocols/embedding.ts:176-178` |
| `UnsupportedFieldsError` | 请求含 `dimensions`/`user` | `src/openai_api_protocols/embedding.ts:165-173` |
| `EmbeddingExceedContextWindowSizeError` | 单条输入 token 数超上下文 | `src/embedding.ts:139-144` |
| `EmbeddingSlidingWindowError` | 配置了滑动窗口 | `src/embedding.ts:74-76` |
| `EmbeddingChunkingUnsupportedError` | `prefillChunkSize ≠ contextWindowSize` | `src/embedding.ts:86-91` |
| `MinValueError` | `maxBatchSize` 等值非正 | `src/embedding.ts:77-85` |
| `IncorrectPipelineLoadedError` | 选中的模型不是 EmbeddingPipeline（如 chat 模型） | `src/engine.ts:1256-1262` |
| `EmbeddingUnsupportedModelError` | 管线是嵌入管线但记录 `model_type` 不符 | `src/engine.ts:1263-1268` |

**Worker 环境同样可用：**

[src/message.ts:29](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/message.ts#L29) —— Worker 消息协议里有独立的 `"embedding"` kind，意味着把嵌入推理放进 Web Worker（第 5 单元主题）时接口完全一致。

#### 4.3.4 代码实践

**实践：跟踪一次 embeddings.create 的调用链（源码阅读型）**

1. 实践目标：不看任何文档，仅凭跳转，写出一次 `engine.embeddings.create({ input: ["a", "b"] })` 调用所经过的全部函数。
2. 操作步骤：
   - 从 [src/engine.ts:165](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L165)（门面实例化）进入 `API.Embeddings`，再回到 `engine.embedding`；
   - 依次记录：`getEmbeddingStates` → `getModelStates` → `getModelIdToUse` → `postInitAndCheckFieldsEmbedding` → `lock.acquire` → `embedStep` → 响应组装 → `lock.release`。
3. 需要观察的现象：调用链里没有任何采样函数、没有 `Conversation`、没有 KV cache 相关调用。
4. 预期结果：得到一条约 8 站的调用链清单；与第 2 单元 chatCompletion 的调用链并排画出来，差异处即两条管线的本质区别。

#### 4.3.5 小练习与答案

**练习 1**：同一个引擎里同时加载了 `gemma-2-2b-it`（chat）和 `snowflake-arctic-embed-m-b4`（embedding），调用 `embeddings.create` 时不传 `model` 会怎样？

**参考答案**：`getModelIdToUse`（src/support.ts:249-250）发现加载了多个模型且请求未指定，抛 `UnclearModelToUseError`。这是 u2-l1 讲过的多模型规则的直接应用：单模型可省 `model`，多模型必填。

**练习 2**：`usage.prompt_tokens` 和 `usage.total_tokens` 为什么相等？

**参考答案**：在 [src/engine.ts:1139-1140](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1139-L1140) 两处都填的是 `getCurRoundEmbedTotalTokens()`。嵌入任务只有输入、没有输出 token（OpenAI 语义里 `total_tokens = prompt + completion`，这里 completion 恒为 0），所以两者相等。

**练习 3**：为什么 `EmbeddingPipeline` 的构造函数里没有 `logitProcessor` 参数（对比 `LLMChatPipeline`）？

**参考答案**：见 [src/engine.ts:402-412](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L402-L412)：`LLMChatPipeline` 接收 `logitProcessor` 是因为生成式推理每一步解码都要在 logits 上做用户自定义修改（u3-l5 主题）；嵌入任务不采样、不生成，logits 对用户没有意义，自然没有这个扩展点。

## 5. 综合实践

**任务：三段文本的语义相似度小工具 + 一次「故意用错模型」的实验**

参照 [examples/embeddings/src/embeddings.ts:72-109](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/embeddings/src/embeddings.ts#L72-L109) 的 `webllmAPI` 写法，完成一个独立页面（示例代码框架如下）：

```ts
// 示例代码：三段文本两两余弦相似度
import * as webllm from "@mlc-ai/web-llm";

const engine = await webllm.CreateMLCEngine(
  "snowflake-arctic-embed-s-q0f32-MLC-b4",  // 最轻量的预置嵌入模型，238.71 MB
  { initProgressCallback: (r) => console.log(r.text) },
);

// snowflake 系模型推荐的输入格式：[CLS] ... [SEP]（见示例 59-67 行）
const texts = [
  "[CLS] The cat sits on the mat. [SEP]",
  "[CLS] A feline is resting on a rug. [SEP]",
  "[CLS] Today's stock market closed early. [SEP]",
];
const reply = await engine.embeddings.create({ input: texts });

const cos = (a: number[], b: number[]) => {
  let dot = 0, na = 0, nb = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i]; na += a[i] ** 2; nb += b[i] ** 2;
  }
  return dot / (Math.sqrt(na) * Math.sqrt(nb));  // 2.1 节公式的代码形态
};

const v = reply.data.map((d) => d.embedding);
console.log("猫垫 vs 猫毯:", cos(v[0], v[1]));
console.log("猫垫 vs 股市:", cos(v[0], v[2]));
console.log("维度:", v[0].length, "tokens:", reply.usage.prompt_tokens,
            "tok/s:", reply.usage.extra.prefill_tokens_per_s);
```

**步骤**：

1. 在 `examples/embeddings` 目录 `npm install && npm start`，先跑通官方示例确认环境（Parcel 起在 8885 端口，见 [examples/embeddings/package.json:6](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/embeddings/package.json#L6)）；再仿照它建自己的入口页面。
2. 记录两两相似度：预期「两句猫」的相似度显著高于「猫 vs 股市」。首次运行需浏览器支持 WebGPU。
3. 记录 `v[0].length`（该模型的 hidden_size）、`usage` 各字段，并与 `getCurRoundEmbedTokensPerSec` 的口径对照。
4. **错误实验**：另起一个引擎加载任意 chat 模型（如 `Llama-3.2-1B-Instruct-q4f32_1-MLC`），调用 `engine.embeddings.create({ input: "hello" })`，捕获并打印错误。

**预期结果与待本地验证**：第 4 步依据 4.3.3 节的源码分析，抛出的应是 `IncorrectPipelineLoadedError`（instanceof 检查在前）；而讲义规格中提到的 `EmbeddingUnsupportedModelError` 是同一函数内更靠后的防御性检查，正常公开 API 下难以触发。请在本地记录实际错误名与错误信息，验证上述顺序判断——若与预期不符，回到 [src/engine.ts:1254-1268](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1254-L1268) 核对当前版本的检查顺序（此结论基于 HEAD `90f6709`，待本地验证）。

## 6. 本讲小结

- `Embeddings` 是纯转发的 OpenAI 风格门面；协议层的 `postInitAndCheckFields` 拦截不支持字段（`dimensions`/`user`）、base64 格式与空输入。
- `EmbeddingPipeline` 与 `LLMChatPipeline` 是平行管线：只有 `prefill` 一个 PackedFunc、无 KV cache、无解码循环、无采样，一次前向后取**第一个 token** 的隐状态作为句向量（CLS 式池化）。
- 批量输入按编译期 `max_batch_size` 分批（b4/b32 即由此得名），批内用零 padding 对齐到最长输入，并配 0/1 attention mask；性能统计不含 padding token。
- 嵌入管线的三条硬约束在构造期检查：不支持滑动窗口、`prefillChunkSize` 必须等于 `contextWindowSize`（无 KV cache 无法分块）、输入不得超过上下文窗口。
- 引擎分发先由 `reload` 按 `ModelRecord.model_type` 选管线，请求时再经 `getModelStates` 双重校验；chat 模型上调用嵌入接口先触发 `IncorrectPipelineLoadedError`，`EmbeddingUnsupportedModelError` 是其后的防御性检查。
- 预置嵌入模型共 4 条（snowflake-arctic-embed s/m × b4/b32），上下文均为 512；Worker 消息协议中有独立的 `"embedding"` kind，嵌入推理同样可搬进 Worker。

## 7. 下一步学习建议

本讲是第 2 单元（引擎接口层）的收官：至此你已覆盖 chat、completion、embedding 三类 OpenAI 风格接口。接下来建议：

1. **进入第 3 单元**，从 `u3-l1 LLMChatPipeline 初始化与 tvmjs 运行时` 开始，深入生成式管线内部——本讲多次对比的两条管线，其差异的根源（PackedFunc 的数量与用途）将在那里展开。
2. 若你对「模型如何被下载与缓存」更好奇，可先跳读 `u4-l1 模型缓存机制`，再回第 3 单元。
3. 想立刻用嵌入模型做点东西的读者，推荐通读 [examples/embeddings/src/embeddings.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/embeddings/src/embeddings.ts) 的 `simpleRAG`（第 160-206 行）：它把嵌入模型与 chat 模型装进**同一个引擎**完成了最小 RAG，是本讲内容与 u2-l2 chatCompletion 的完美合流点。
