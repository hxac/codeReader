# 嵌入提供者（Embedding Provider）

## 1. 本讲目标

本讲聚焦 `pkg/embedding` 这个「小而关键」的包。它是把一段文本变成向量的统一入口。学完本讲，你应该能够：

1. 说出 `Provider` 接口的四个方法各自做什么、为什么向量类型是 `[]float32`。
2. 讲清楚 `NewProviderFromRouterConfig` 是如何根据配置在「本地推理后端（candle/openvino）」与「远程 HTTP 后端（openai_compatible）」之间分叉的，并能解释**为什么这条工厂只认远程后端**。
3. 列出至少 3 个消费 `Provider` 的子系统，理解「同一份嵌入能力被多处复用」的设计意图。

本讲是分类信号系统（u8）的收尾篇，承接 u8-l1 中「`Classifier` 持有各类信号族后端但不做推理」的结论：那些「学习型信号」（domain、complexity、preference、jailbreak、工具检索……）真正用来打分的向量，正是由这里的 `Provider` 提供的。

## 2. 前置知识

- **嵌入（embedding）**：把一段文本映射成一个固定长度的浮点向量，使得「语义相近的文本」在向量空间里距离也近。它是相似度检索、语义缓存、分类打分的基础设施。
- **余弦相似度**：衡量两个向量方向是否一致，取值范围 \([-1, 1]\)，越接近 1 越相似。本项目中几乎所有「找最像的」操作（找最像的工具、最像的记忆、最像的缓存查询）底层都是点积或余弦。
- **`float32` vs `float64`**：嵌入向量在内存里通常用 `float32` 存储（省一半内存、且 SIMD 指令对 `float32` 更友好），而 JSON 解码出来默认是 `float64`。你会看到 Provider 在边界处做了一次 `float64 → float32` 的转换。
- **CGO**：Go 调用 C/Rust 代码的机制。本项目的本地推理能力（candle、openvino）是通过 CGO 绑定暴露的，这一点会直接影响本讲对「工厂分叉」的理解。
- **OpenAI 兼容的嵌入 API**：一种事实标准——`POST /v1/embeddings`，请求体 `{"model": "...", "input": ["文本"], "dimensions": N}`，响应体 `{"data": [{"index": 0, "embedding": [...]}]}`，鉴权用 `Authorization: Bearer <key>`。任何兼容这套协议的服务（vLLM 自身的嵌入端点、TEI、各类网关）都能被本项目当作嵌入后端。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/semantic-router/pkg/embedding/provider.go` | 定义 `Provider` 接口、`FuncProvider` 适配器，以及工厂入口 `NewProvider` / `NewProviderFromRouterConfig`。本讲的主轴。 |
| `src/semantic-router/pkg/embedding/openai_provider.go` | 远程后端的唯一具体实现 `OpenAICompatibleProvider`：构造校验、HTTP 请求、重试退避、维度校验、float 转换。 |
| `src/semantic-router/pkg/config/embedding_config.go` | 后端常量（`candle`/`openvino`/`openai_compatible`）与 `EmbeddingBackend()` 的后端解析逻辑。 |
| `src/semantic-router/pkg/config/model_config_types.go` | `EmbeddingModels`（嵌入模型配置）与 `HNSWConfig.WithDefaults()`（默认维度 768、默认后端 candle）。 |
| `src/semantic-router/pkg/extproc/router_selection.go` | 本地后端的「组装现场」：用 `NewFuncProvider` 把 candle/openvino 的 CGO 函数包成 `Provider`。 |
| `src/semantic-router/pkg/extproc/router_components.go` | 工具库嵌入提供者的构造（只在远程后端时创建）。 |
| `src/semantic-router/pkg/classification/embedding_classifier.go`、`src/semantic-router/pkg/tools/tools.go` | 两个典型消费者：分类与工具检索。 |

## 4. 核心概念与源码讲解

### 4.1 Provider 接口：统一的嵌入抽象

#### 4.1.1 概念说明

「把文本变成向量」这件事，在 SR 里被很多子系统需要：分类器要给查询打域分、工具检索要找最像的工具、模型选择的 learning 算法要给候选打分……如果每个子系统各自直接调用嵌入后端，就会出现「换一个后端，全项目都要改」的灾难。

`Provider` 接口就是为了切断这种耦合而存在的：它定义了一个**最小、后端无关**的契约——「给我文本，我给你 `float32` 向量，外加告诉我向量维度和后端名字」。任何子系统只依赖这个接口，不关心向量到底是本地 Rust 模型算出来的，还是远程 HTTP 服务返回的。

这是一个典型的「依赖倒置」：高层策略（分类、检索）定义接口，底层细节（candle/openvino/远程 HTTP）提供实现，二者通过接口解耦。

#### 4.1.2 核心流程

`Provider` 的调用流程极其简单：

```
调用方                    Provider 接口               具体后端
  │  Embed(ctx, text) ───► Embed 内部转调 EmbedBatch ──► FuncProvider: 调 CGO 函数
  │                                                  └──► OpenAICompatibleProvider: 发 HTTP
  │  ◄── []float32, error ──◄
  │
  │  EmbedBatch(ctx, texts) ─► 一次性批量
  │  Dimension()           ─► 返回向量维度（建索引/对齐用）
  │  Backend()             ─► 返回后端名（日志/可观测用）
```

四个方法可以分两类：

- **能力方法** `Embed` / `EmbedBatch`：真正算向量。注意 `Embed` 是「单条」、`EmbedBatch` 是「批量」，批量版本能让远程后端在单次 HTTP 往返里处理多条文本，省网络。
- **元数据方法** `Dimension` / `Backend`：`Dimension` 返回向量长度（建 HNSW 索引、做相似度时需要对齐维度）；`Backend` 返回后端名字（用于日志、探活上报，让运维知道现在用的是 candle 还是远程）。

#### 4.1.3 源码精读

接口本体只有四个方法，向量为 `[]float32`：

[provider.go:13-19](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/embedding/provider.go#L13-L19) 定义了 `Provider` 接口：`Embed` 单条、`EmbedBatch` 批量、`Dimension` 维度、`Backend` 后端名，所有向量都是 `[]float32`。

接口虽小，但要落地有一个常见痛点：很多已有的嵌入函数签名只是 `func(context.Context, string) ([]float32, error)`（特别是 candle/openvino 的 CGO 包装），并不天然实现「批量 + 两个元数据方法」。为此包里提供了 `FuncProvider` 适配器，把一个普通函数「升级」成完整接口：

[provider.go:53-62](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/embedding/provider.go#L53-L62) 是 `NewFuncProvider`：接收 `backend` 名、`dimension` 维度、一个 `embed` 函数，构造 `FuncProvider`，并对 `backend`/`embed` 做非空校验。

`FuncProvider` 的批量实现很朴素——逐条调用单条函数：

[provider.go:64-86](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/embedding/provider.go#L64-L86) 实现 `FuncProvider` 的四个方法。注意 `EmbedBatch` 是 `for` 循环逐条调用 `Embed`，**任意一条失败立即返回错误**——这对本地 CGO 后端够用，但批量效率不如远程后端的原生批量。

> 这就解释了为什么 `OpenAICompatibleProvider` 要单独实现 `EmbedBatch`（一次 HTTP 发多条），而 `FuncProvider` 只能循环：适配器无法假设被包装的函数支持批量。

此外还有一个便利函数 `NewEmbeddingFunc`，它把 `Provider` 反过来「降级」成一个 `func(string) ([]float32, error)`，喂给那些只接受函数、不接受接口的旧调用点：

[provider.go:88-96](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/embedding/provider.go#L88-L96) 是 `NewEmbeddingFunc`：内部 `context.Background()` 调 `provider.Embed`，返回一个无 `ctx` 的纯函数。它是一道「接口→函数」的桥。

#### 4.1.4 代码实践

**实践目标**：通过阅读测试，亲手验证 `EmbedBatch` 的批量契约与 `float64→float32` 转换。

**操作步骤**：

1. 打开 [openai_provider_test.go:14-37](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/embedding/openai_provider_test.go#L14-L37) 的 `TestOpenAICompatibleProviderEmbedBatch`。它用一个 `httptest.Server` 模拟一个 OpenAI 兼容端点，固定返回 `[[0.1,0.2],[0.3,0.4]]`。
2. 观察断言：`len(embeddings)==2`、每条长度 `==2`、且 `embeddings[1][0]==float32(0.3)`。注意断言写的是 `float32(0.3)`——服务端用 `[][]float64` 发送（见 `writeEmbeddingResponse`），客户端转成 `float32` 后比对。
3. 运行该测试：
   ```bash
   cd src/semantic-router
   go test ./pkg/embedding/ -run TestOpenAICompatibleProviderEmbedBatch -v
   ```

**需要观察的现象**：测试通过；`embeddings` 的形状是「条数×维度」二维切片；服务端 `float64` 的 `0.3` 经转换后仍能通过 `float32(0.3)` 比较。

**预期结果**：`PASS`，输出一条 `--- PASS: TestOpenAICompatibleProviderEmbedBatch`。如果维度与服务端不符，会触发 `convertEmbedding` 的维度不匹配错误——这正是 `ExpectedDimension` 校验的保护作用。

> 待本地验证：本实践需要可用的 Go 工具链。若环境无 Go，可只做第 1、2 步的源码阅读。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Provider` 接口的向量类型是 `[]float32` 而不是 `[]float64`？

> **参考答案**：嵌入向量通常维度很高（如 768、1024），用 `float32` 存储相比 `float64` 节省一半内存；且本项目底层 HNSW 索引的 SIMD 点积优化（AVX2/AVX-512）针对 `float32` 设计。精度损失对「相似度排序」这类任务几乎无影响。

**练习 2**：`FuncProvider.EmbedBatch` 在第 i 条失败时会怎样？

> **参考答案**：立即返回 `(nil, err)`，不再处理后续文本。前 i-1 条已算出的结果会被丢弃（因为整体返回 nil），保证「要么全成功，要么失败」的语义。

### 4.2 提供者工厂：本地与远程的分叉

#### 4.2.1 概念说明

有了接口，下一个问题是「谁来构造具体的 `Provider`」。直觉上应该有一个统一的工厂：传配置进去，吐出一个 `Provider`。SR 确实提供了这个工厂（`NewProviderFromRouterConfig` → `NewProvider`），但**它只认远程后端**。本地后端（candle/openvino）的构造被刻意留在了工厂之外，由调用现场（`extproc/router_selection.go`）用 `NewFuncProvider` 手工拼装。

为什么这样设计？因为本地后端依赖 CGO 绑定（`candle-binding`、`openvino-binding`），而 `pkg/embedding` 这个底层包**不想引入 CGO 依赖**——一旦引入，所有 import 它的包都会被迫卷入 CGO 编译。把本地后端的拼装放到已经 import 了 CGO 绑定的 `extproc` 包里，既保持了 `pkg/embedding` 的纯净（纯 Go、只含 HTTP 逻辑），又让本地后端的初始化与模型加载就近完成。这是一次「以调用现场复杂度换核心包纯净度」的有意识取舍。

#### 4.2.2 核心流程

后端解析有一套明确的**优先级**，理解它就理解了「本地还是远程」的全部判定：

```
EMBEDDING_BACKEND_OVERRIDE 环境变量（最高优先级，运行期覆盖）
        │（为空则）
        ▼
config.EmbeddingModels.EmbeddingBackend()
        │
        ├─ 配置里显式写了 backend ──► 用它
        ├─ model_type == "remote" ──► openai_compatible
        └─ 兜底 ─────────────────► candle
```

拿到 `backend` 后：

- 若是 `openai_compatible` → 工厂走 `NewOpenAICompatibleProvider`（纯 HTTP，无 CGO）。
- 若是 `candle` / `openvino` → **不进工厂**，由 `router_selection.go` 用 `NewFuncProvider` 把 CGO 函数包起来。

后端解析优先级见 [embedding_config.go:24-33](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/embedding_config.go#L24-L33)：`EmbeddingBackend()` 先取显式 backend，否则 `model_type=="remote"` 转 `openai_compatible`，否则兜底 `candle`；常量定义在同文件 [embedding_config.go:5-12](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/embedding_config.go#L5-L12)。

`NewProvider` 工厂的 `switch` 只有一个分支，这是本节最关键的事实：

[provider.go:43-51](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/embedding/provider.go#L43-L51) 的 `NewProvider`：`resolveBackend` 决定后端，`switch` 仅匹配 `openai_compatible` 走 `NewOpenAICompatibleProvider`，**其它一切后端（含 candle）直接返回 `unsupported embedding backend` 错误**。

后端覆盖与解析：

[provider.go:32-34](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/embedding/provider.go#L32-L34) 的 `BackendOverrideFromEnv` 读取并归一化 `EMBEDDING_BACKEND_OVERRIDE` 环境变量（小写、去空白）。

[provider.go:98-103](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/embedding/provider.go#L98-L103) 的 `resolveBackend`：环境变量 override 非空则优先，否则退回 `models.EmbeddingBackend()`。

#### 4.2.3 源码精读

**入口工厂**（从 `RouterConfig` 出发）：

[provider.go:36-41](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/embedding/provider.go#L36-L41) 的 `NewProviderFromRouterConfig`：校验 `cfg` 非空，把 `cfg.EmbeddingModels` 连同 options 交给 `NewProvider`。它是一道「配置对象 → 嵌入模型段」的转发阀门。

`options` 里除了 `BackendOverride`，还有一个 `HTTPClient *http.Client`（[provider.go:21-24](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/embedding/provider.go#L21-L24)），用于注入自定义 HTTP 客户端（测试里用来挂 `httptest.Server` 的传输层）。

**配置→远程 Provider 的字段映射**：

[provider.go:105-120](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/embedding/provider.go#L105-L120) 的 `openAICompatibleConfigFromModels`：把 `EmbeddingModels.Endpoint`（base_url/model/api_key_env/timeout/retries/dimensions）映射成 `OpenAICompatibleConfig`。注意 `expectedDimension` 的取值——`Endpoint.Dimensions` 优先于 `EmbeddingConfig.TargetDimension`，二者并存且不一致时会在构造期报错。

`EmbeddingModels` 的字段结构见 [model_config_types.go:41-50](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/model_config_types.go#L41-L50)，其中 `EmbeddingConfig` 是 `HNSWConfig`，承载 backend/model_type/target_dimension 等；它的默认值在 [model_config_types.go:69-90](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/model_config_types.go#L69-L90) 的 `WithDefaults`：backend 默认 `candle`、model_type 默认 `qwen3`、`target_dimension` 默认 `768`。

**远程后端具体实现 OpenAICompatibleProvider**：

构造期做严格校验，把错误配置挡在启动阶段：

[openai_provider.go:71-111](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/embedding/openai_provider.go#L71-L111) 的 `NewOpenAICompatibleProvider`：校验 base_url/model 非空、`dimensions` 与 `expectedDimension` 一致、`timeout_seconds` 非负；timeout 为 0 时取默认 10 秒；`HTTPClient` 为空则自建带 timeout 的客户端，已有客户端且无 timeout 时复制一份补上 timeout（不污染传入的客户端）。

请求发往规范化的 `/embeddings` 端点：

[openai_provider.go:296-315](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/embedding/openai_provider.go#L296-L315) 的 `embeddingsEndpoint`：把 `base_url` 解析后保证 path 以 `/embeddings` 结尾，并清掉 query。所以配置写 `http://host:port` 或 `http://host:port/v1` 都能拼出正确的 `/v1/embeddings`。

重试与退避：远程嵌入是网络调用，必须容错。`EmbedBatch` 用「尝试次数 = `maxRetries + 1`」的循环，仅对可重试错误（429/5xx/超时/临时网络错误）退避重试：

[openai_provider.go:121-150](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/embedding/openai_provider.go#L121-L150) 的 `EmbedBatch` 主体；退避算法在 [openai_provider.go:152-165](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/embedding/openai_provider.go#L152-L165) 的 `waitBeforeRetry`。

退避是指数级的，以位移实现（`baseRetryDelay << (attempt-1)`），上限 `maxRetryDelay`：

\[
\text{delay}(\text{attempt}) = \min\left(50\text{ms} \cdot 2^{\,\text{attempt}-1},\ 500\text{ms}\right)
\]

即第 1、2、3、4 次重试前分别等待约 50ms、100ms、200ms、400ms，第 5 次起封顶 500ms。这种「指数退避 + 上限」是避免在远程服务过载时雪崩的标准手法。

维度校验与 float 转换发生在解析阶段：

[openai_provider.go:271-283](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/embedding/openai_provider.go#L271-L283) 的 `convertEmbedding`：JSON 里 `embedding` 字段是 `[]float64`，这里逐元素转成 `float32`；若配置了 `expectedDimension`，长度不符即报「dimension mismatch」——这能及早发现「远程模型换了，维度对不上本地索引」的运维事故。

`Dimension()` 与 `Backend()` 的实现：

[openai_provider.go:167-176](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/embedding/openai_provider.go#L167-L176)：`Dimension()` 优先返回 `expectedDimension`，退而取 `dimensions`；`Backend()` 恒返回常量 `openai_compatible`。

**本地后端的组装现场**（这才是 candle/openvino 真正变成 `Provider` 的地方）：

[router_selection.go:81-113](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router_selection.go#L81-L113) 的 `resolveSelectionEmbeddingProvider`：

1. 先取后端（env override 优先，否则 `EmbeddingBackend()`）。
2. 若是 `openai_compatible` → 走工厂 `embedding.NewProvider`（与远程分类器共用同一条路径）。
3. 否则按 `candle` / `openvino` 分支，用 `NewFuncProvider` 把 CGO 函数包起来：
   - `openvino` → 包 `openvinoEmbeddingFunc(modelType)`；
   - `candle`（default）→ 包一段闭包，`model_type==qwen3` 时调 `candle_binding.GetEmbeddingBatched`，否则调 `candle_binding.GetEmbeddingWithModelType`。

注意 `candle` 这条路**完全绕开了 `pkg/embedding` 的工厂**，直接把 `candle_binding` 的输出喂给 `NewFuncProvider`——这正是 4.2.1 所说的「工厂只认远程」的体现。

#### 4.2.4 代码实践

**实践目标**：手工追踪「配置 → 后端判定 → 工厂分叉」，验证 candle 不会进工厂。

**操作步骤**：

1. 在 [model_config_types.go:69-90](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/model_config_types.go#L69-L90) 确认：一份**没有**显式写 `backend`、`model_type` 也不是 `remote` 的 `EmbeddingModels`，经 `WithDefaults()` 后 backend 为 `candle`、model_type 为 `qwen3`、维度为 768。
2. 沿调用链推断：把这样一份配置喂给 `NewProviderFromRouterConfig` → `NewProvider` → `resolveBackend` 返回 `candle` → `switch` 落入 `default` 分支。
3. 预测结果：返回错误 `unsupported embedding backend "candle"`（见 [provider.go:49](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/embedding/provider.go#L49)）。
4. 对比：同样的配置在 [router_selection.go:81-113](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router_selection.go#L81-L113) 里却**能**正常产出 `Provider`——因为它走的是 `NewFuncProvider` + `candle_binding`，不进工厂 switch。

**需要观察的现象**：同一份 candle 配置，工厂入口报错、而 `resolveSelectionEmbeddingProvider` 入口成功。这种「双入口」的差异正是本节的核心。

**预期结果**：你能用自己的话解释——「工厂只负责无 CGO 依赖的远程后端；本地后端由已 import CGO 的 extproc 包就地拼装」。

> 待本地验证：若想实测第 3 步，可写一个最小 Go 测试，构造一份默认 `EmbeddingModels` 调 `embedding.NewProviderFromRouterConfig`，断言返回的错误信息包含 `unsupported embedding backend`。

#### 4.2.5 小练习与答案

**练习 1**：设置环境变量 `EMBEDDING_BACKEND_OVERRIDE=openai_compatible`，但配置文件里写的是 candle 模型路径，会发生什么？

> **参考答案**：`resolveBackend` 让 env override 优先，后端被判为 `openai_compatible`，工厂会尝试构造 `OpenAICompatibleProvider`。但因为配置里 `Endpoint.BaseURL`/`Model` 大概率为空，构造期会在 `embeddingsEndpoint`（base_url 必填）或 `NewOpenAICompatibleProvider`（model 必填）报错。这是「运行期强制切远程」的逃生通道，但要求配置里补齐远程端点字段。

**练习 2**：为什么把 candle 的拼装放在 `extproc` 而不是 `pkg/embedding`？

> **参考答案**：`pkg/embedding` 若 import `candle-binding` 就会被迫成为 CGO 包，所有传递依赖它的包（含纯测试）都要走 CGO 编译，拖慢构建、限制交叉编译。把 CGO 拼装放在已经依赖 CGO 的 `extproc`，保持 `pkg/embedding` 纯 Go、可被任意包安全引用。

**练习 3**：远程嵌入返回的向量维度和配置的 `target_dimension` 不一致时，哪一步会拦住它？

> **参考答案**：`convertEmbedding`（[openai_provider.go:271-283](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/embedding/openai_provider.go#L271-L283)）在 `expectedDimension>0` 时检查返回向量长度，不符即报 `dimension mismatch`。这避免把错误维度的向量塞进 HNSW 索引导致后续相似度计算全错。

### 4.3 跨子系统复用

#### 4.3.1 概念说明

`Provider` 的价值不在「定义了一个接口」，而在「被很多子系统共用同一个实现」。SR 里凡是需要「把文本变向量再做相似度」的地方，都尽量指向同一个 `Provider`，这样：

- **换后端只改一处**：从本地 candle 切到远程 openai_compatible，只动配置（或一个环境变量），所有下游子系统自动跟着切。
- **维度一致**：所有子系统拿到的向量维度统一来自 `Provider.Dimension()`，不会出现「分类用 768 维、工具检索用 1024 维」对不上的问题。
- **可观测一致**：探活、日志里的后端名都来自 `Provider.Backend()`，运维看到的是单一事实源。

#### 4.3.2 核心流程

一次请求里，`Provider` 可能被这些路径消费：

```
请求进入
  ├─ 分类（classification）: embedding/complexity/preference/jailbreak/category_kb 等学习型信号
  │     └─ 用 Provider 把查询/证据文本变向量，做相似度或对比打分
  ├─ 工具检索（tools）: 把查询向量与工具库向量做点积，找 top-k 工具
  ├─ 模型选择 learning（extproc/router_selection）: RouterDC/AutoMix 等用嵌入给候选模型打分
  └─ 启动期探活（modelruntime）: 用一句探针文本试跑远程 Provider，确认健康
```

需要澄清一个边界：**语义缓存（`pkg/cache`）与智能体记忆（`pkg/memory`）目前走的是各自独立的、更早期的嵌入路径**（基于 BERT/candle 的 `GenerateEmbedding`），并未统一到 `embedding.Provider`。所以「跨子系统复用」主要指分类、工具检索、模型选择这三类**新链路**，而非缓存/记忆。讲义不夸大复用范围。

#### 4.3.3 源码精读

**消费者 1：分类器直接持有 `Provider`**

[embedding_classifier.go:33-43](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/embedding_classifier.go#L33-L43) 表明 `EmbeddingClassifier` 把 `embedding.Provider` 作为字段持有，并通过 `NewEmbeddingClassifierWithProvider` 注入。同样的模式还出现在 complexity、preference（含对比学习版）、contrastive jailbreak、category_kb、reask 等学习型分类器中——它们都是 `Provider` 的消费者。这正是 u8-l1 所说「`Classifier` 持有各类信号族后端」里「学习型信号」那部分后端的统一来源。

**消费者 2：工具检索库把 `Provider` 作为构造选项**

[tools.go:42-63](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/tools/tools.go#L42-L63)：`ToolsDatabase` 结构体持有 `provider embedding.Provider`，并通过 `ToolsDatabaseOptions.Provider` 在 `NewToolsDatabase` 时注入。工具检索（u10-l3 会详讲）正是用这个 provider 把查询变向量、再与工具库向量做点积打分。

**消费者 3：模型选择的嵌入提供者**

[router_selection.go:81-113](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router_selection.go#L81-L113) 不仅构造本地 Provider，其产出的 `resolveSelectionEmbeddingFunc`（[router_selection.go:69-79](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router_selection.go#L69-L79)）会被选择算法（如 RouterDC/AutoMix，见 u6-l2）用来给候选模型打分。这是「嵌入驱动模型选择」的入口。

**消费者 4：工具库的远程嵌入提供者（带门控）**

[router_components.go:153-162](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router_components.go#L153-L162) 的 `toolsEmbeddingProvider`：**只有当 `UsesRemoteEmbeddingBackend()` 为真时才创建** `Provider`，否则返回 `(nil, nil)`。这是一个重要细节——工具库在远程后端下用统一的 `Provider`，在本地后端下回退到各自的原生嵌入路径（返回 nil 让调用方走老路）。

**消费者 5：启动期远程后端探活**

[router_runtime.go:674-704](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelruntime/router_runtime.go#L674-L704) 的 `remoteEmbeddingRuntimeTask`：启动时用一句探针文本 `"semantic router embedding probe"` 实跑 `provider.Embed`，成功则记录维度与健康、失败则上报 `remote_embedding_init_failed`。这是 u6-l3 讲过的「`EmbeddingRuntimeState` 健康」的具体来源——远程嵌入是否就绪，由 Provider 的真实调用决定。

#### 4.3.4 代码实践

**实践目标**：用搜索工具列出全部消费者，亲手验证「同一接口、多处复用」。

**操作步骤**：

1. 在仓库根目录运行（只读检索，不改代码）：
   ```bash
   cd src/semantic-router
   grep -rn "embedding.Provider" pkg/classification pkg/tools pkg/extproc pkg/modelruntime
   ```
2. 把命中结果按子系统归类（classification / tools / extproc / modelruntime）。
3. 对每条命中，判断它是「持有 Provider 字段」「构造 Provider」还是「接收 Provider 参数」。

**需要观察的现象**：`embedding.Provider` 这个类型名出现在多个包里；`NewProvider` / `NewFuncProvider` 的构造点集中在 `extproc` 与 `modelruntime`，而 `classification`、`tools` 只做「接收/持有」。

**预期结果**：你能列出至少 3 个消费子系统（如 classification 的 `EmbeddingClassifier`、tools 的 `ToolsDatabase`、extproc 的模型选择 / 工具库 / modelruntime 探活），并指出构造与消费的分工。

> 待本地验证：grep 的具体行数会随代码演进变化，以你本地仓库的实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `toolsEmbeddingProvider` 在非远程后端时返回 `(nil, nil)` 而不是报错？

> **参考答案**：工具库在本地后端下有自己原生的嵌入路径（直接用 candle），不需要统一的 `Provider`。返回 `(nil, nil)` 是一种「软门控」——调用方拿到 nil 就走老路，既不强制统一，也不误报错误。这是渐进式迁移的常见手法。

**练习 2**：如果要把语义缓存（`pkg/cache`）也迁移到 `embedding.Provider`，需要改动哪一层？

> **参考答案**：需要让缓存的构造接收一个 `embedding.Provider`（或用 `NewEmbeddingFunc` 降级成函数），替换现有的 `GenerateEmbedding`/BERT 直调路径，并保证向量维度与缓存索引一致。由于缓存有自己的用户作用域隔离（HMAC 命名空间），迁移时要确保维度对齐，否则旧缓存失效。

## 5. 综合实践

**任务**：画出「一次配置 → 多个子系统拿到同一个 Provider」的装配图，并用源码佐证每个箭头。

要求：

1. 选一份远程后端配置（`model_type: remote` 或 `backend: openai_compatible`，配好 `endpoint.base_url`/`model`/`api_key_env`）。
2. 画出三条装配路径并标注源码位置：
   - **A 远程分类器**：`NewProviderFromRouterConfig` → `NewProvider` → `NewOpenAICompatibleProvider` → 注入 `EmbeddingClassifier`（[embedding_classifier.go:43](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/classification/embedding_classifier.go#L43)）。
   - **B 工具检索**：`toolsEmbeddingProvider` → `NewProvider` → 注入 `ToolsDatabase`（[router_components.go:153](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router_components.go#L153)、[tools.go:55](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/tools/tools.go#L55)）。
   - **C 启动探活**：`remoteEmbeddingRuntimeTask` → `NewProvider` → 探针调用（[router_runtime.go:687](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/modelruntime/router_runtime.go#L687)）。
3. 再画一份本地后端配置（默认 candle），标注它**不走工厂**，而是经 [router_selection.go:98](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router_selection.go#L98) 的 `NewFuncProvider` + `candle_binding` 就地拼装。
4. 在图上用一句话总结：为什么「换后端只改配置」能成立（答案：所有消费者依赖的是 `Provider` 接口，后端切换发生在构造期，消费者无感知）。

> 待本地验证：图中行号以当前 HEAD 为准；若代码演进，请用 `grep -rn "embedding.Provider"` 重新核对。

## 6. 本讲小结

- `Provider` 是 SR 把「文本→`[]float32` 向量」抽象成的小接口：`Embed`/`EmbedBatch`（能力）+ `Dimension`/`Backend`（元数据），用 `float32` 是为省内存与配合 SIMD。
- `FuncProvider` 是把普通函数 `func(ctx, string) ([]float32, error)` 升级成完整接口的适配器，`EmbedBatch` 退化为逐条循环——本地后端靠它接入。
- 工厂 `NewProviderFromRouterConfig` → `NewProvider` **只认远程 `openai_compatible` 后端**；本地 candle/openvino 因依赖 CGO，刻意留在工厂之外，由 `extproc/router_selection.go` 用 `NewFuncProvider` 就地拼装，以保持 `pkg/embedding` 纯 Go。
- 后端解析有明确优先级：`EMBEDDING_BACKEND_OVERRIDE` 环境变量 > 配置显式 backend > `model_type=="remote"` > 兜底 candle。
- 远程 `OpenAICompatibleProvider` 自带指数退避重试（50ms·2ⁿ，上限 500ms）、维度校验（`expectedDimension`）、`float64→float32` 转换，把网络与协议的脏活全包了。
- `Provider` 被分类、工具检索、模型选择、启动探活等多个子系统复用；语义缓存与记忆目前仍是各自独立的早期嵌入路径，尚未统一。

## 7. 下一步学习建议

- **横向看消费侧**：阅读 [u9-l1 HNSW 向量索引](u9-l1-hnsw-index.md) 与 [u10-l3 工具检索与 MCP](u10-l3-tools-mcp.md)，看 `Provider` 产出的向量如何进入 HNSW 图、如何驱动工具的 top-k 检索。
- **纵向看本地推理**：阅读 [u12-l4 推理绑定](u12-l4-inference-bindings.md)，理解 `candle_binding.GetEmbeddingBatched` 这类 CGO 函数背后的 Rust/C 推理实现，与本讲的「拼装现场」对上号。
- **回到选择算法**：结合 [u6-l2 选择算法注册表](u6-l2-selection-algorithms.md)，理解 `resolveSelectionEmbeddingFunc` 产出的嵌入如何被 RouterDC/AutoMix 等算法用来给候选模型打分。
- **可选拓展**：尝试给 `pkg/cache` 或 `pkg/memory` 接入 `embedding.Provider`，作为「统一嵌入抽象」的一次真实重构练习。
