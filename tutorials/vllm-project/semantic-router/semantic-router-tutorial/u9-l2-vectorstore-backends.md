# 向量存储与多后端

## 1. 本讲目标

本讲讲解 Semantic Router（以下简称 SR）中 `pkg/vectorstore` 这一层：它把「文档上传 → 切块 → 嵌入 → 写入向量库 → 相似度检索」做成一套**与 OpenAI Vector Stores API 对齐**的能力，并把底层向量数据库抽象成**可替换的后端**（内存 / Milvus / Qdrant / Valkey / LlamaStack）。

学完本讲你应该能够：

- 说清 `VectorStoreBackend` 接口的 7 个方法分别承担什么职责，以及 `Manager` 如何在「后端 + 元数据注册表」之上编排 CRUD。
- 画出一次文档摄入（ingestion）从字节到可检索的完整流水线，理解切块、嵌入、写入三步为何是异步、带生命周期取消的。
- 区分 5 种后端的实现差异：内存后端用暴力余弦检索、Milvus 用 HNSW + 内积、Qdrant/Valkey/LlamaStack 又各走什么协议。
- 说清 `pkg/milvus`（共享生命周期包）如何统一「连接、集合 ensure/load、带重试」这三件被多个运行时存储重复的琐碎逻辑。

本讲承接 [u9-l1 HNSW 向量索引](u9-l1-hnsw-index.md)：u9-l1 讲的是**进程内** HNSW 近邻图本身，本讲讲的是向量库这一**业务层**——以及它如何把检索能力下沉给不同的外部向量库。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，什么是向量库（Vector Store）。** 大模型本身没有「记忆」你私有的文档。要让它能检索私有知识，常见做法是：把文档切成小块（chunk），用嵌入模型把每块文字转成一个定长浮点向量（embedding），再把「向量 + 原文」存进一个能按向量相似度检索的库里。检索时，把用户查询也转成向量，在库里找最相似的若干块，拼进提示词。这套流程叫 RAG（Retrieval-Augmented Generation）。向量库就是那个「能按向量相似度检索」的库。

**第二，OpenAI Vector Stores API 的形态。** OpenAI 把这套能力建模成三层对象，SR 的领域模型刻意与之对齐：

- `VectorStore`（向量库）：一个命名的文档集合，记录文件计数与状态。
- `VectorStoreFile`（库文件）：一个文件「挂载」到某个库上的状态记录（`in_progress` / `completed` / `failed`）。
- `EmbeddedChunk`（嵌入块）：一块带向量的正文，是真正写进底层向量库的最小单位。

**第三，为什么需要「多后端抽象」。** 开发测试时你只想用内存；生产环境你可能用 Milvus、Qdrant 或 Valkey 这类专用向量库；某些场景下 LlamaStack 已经代你管理了嵌入与检索。SR 的做法是定义一个 `VectorStoreBackend` 接口，让上层（Manager、摄入流水线、检索服务）只面向接口编程，5 种后端各自实现，由工厂 `NewBackend` 按类型字符串切换。这样换库 = 改一个配置项，上层代码不动。

关键术语速查：collection（集合，一个向量库在底层对应一个集合）、chunk（块）、embedding（嵌入向量）、topK（取最相似的 K 条）、threshold（相似度阈值，低于阈值的不返回）、HNSW（分层可导航小世界图，见 u9-l1）、IP（Inner Product，内积度量）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pkg/vectorstore/types.go` | 领域模型：`VectorStore` / `VectorStoreFile` / `EmbeddedChunk` / `SearchResult` / 切块策略等类型，以及带前缀的 ID 生成器。 |
| `pkg/vectorstore/service.go` | **`VectorStoreBackend` 接口**——所有后端共同实现的契约，只有 7 个方法。 |
| `pkg/vectorstore/manager.go` | `Manager`：在后端与元数据注册表（`StoreRegistry`）之上做 CRUD，维护一份内存索引供快速查询。 |
| `pkg/vectorstore/factory.go` | `NewBackend` 工厂：按 `backendType` 字符串实例化 5 种后端之一。 |
| `pkg/vectorstore/pipeline.go` | `IngestionPipeline`：异步摄入流水线（读文件 → 解析 → 切块 → 嵌入 → 写入）。 |
| `pkg/vectorstore/chunking.go` | `ChunkText`：按 `auto`（段落/标题）或 `static`（定长+重叠）切分文本。 |
| `pkg/vectorstore/parser.go` | `ExtractText`：按文件扩展名把字节解析成纯文本（txt/md/json/csv/html）。 |
| `pkg/vectorstore/memory_backend.go` | 内存后端：暴力余弦检索，开发测试用，附带混合检索。 |
| `pkg/vectorstore/milvus_backend.go` | Milvus 后端：HNSW + 内积，集合 schema 与表达式注入防护。 |
| `pkg/vectorstore/hybrid.go` | 混合检索：向量 + BM25 + n-gram，加权或 RRF 融合。 |
| `pkg/vectorstore/candle_embedder.go` | `Embedder` 接口的实现：经 Candle FFI 本地嵌入（需 CGO）。 |
| `pkg/milvus/lifecycle.go` | **共享生命周期包**：连接、集合 ensure/load、带重试，供多个 Milvus 后端复用。 |

## 4. 核心概念与源码讲解

本讲拆为四个最小模块：① 向量库 API（领域模型 + 后端接口 + Manager 编排）；② 摄入流水线；③ 多后端抽象；④ Milvus 生命周期。

---

### 4.1 向量库 API：领域模型、后端接口与 Manager 编排

#### 4.1.1 概念说明

这一层回答两个问题：上层看到的「向量库」长什么样？底层后端必须实现哪些能力？

SR 的设计哲学是**薄接口 + 厚模型**：

- **厚模型**：`types.go` 里定义了大量 OpenAI 风格的领域结构体（`VectorStore`、`FileCounts`、`VectorStoreFile`、`EmbeddedChunk`、`SearchResult`、`ChunkingStrategy` 等）。这些类型上层（apiserver 的 `/v1/vector_stores` 路由、Dashboard）直接序列化成 JSON 暴露给用户，所以字段命名贴近 OpenAI 规范。
- **薄接口**：`VectorStoreBackend` 只有 7 个方法，刻意只保留「集合的增删存在 + 块的增删查 + 关闭」。切块、嵌入、文件读写、状态记账这些**与具体向量库无关**的逻辑，全部留在接口之外，由 `Manager` 与 `IngestionPipeline` 统一处理。

`Manager` 则是「编排者」：它持有**两样东西**——一个 `VectorStoreBackend`（真正存向量的地方）和一个 `StoreRegistry`（持久化向量库元数据的地方）。为什么不直接用后端？

> 因为并非所有后端都可靠地保存「向量库的元信息」（名字、创建时间、文件计数、过期策略）。Milvus/Qdrant 擅长存向量与检索，但不擅长存这种结构化的对象元数据。于是 SR 把「向量」交给后端、把「元数据」交给独立的注册表（`StoreRegistry` 接口，有内存与 PostgreSQL 两套实现），`Manager` 负责让两者保持一致，并额外维护一份进程内索引以加速查询。

#### 4.1.2 核心流程

一个向量库的「创建 → 使用 → 删除」生命周期由 `Manager` 编排，伪代码如下：

```
CreateStore(name)
  1. id = GenerateVectorStoreID()              # 生成 "vs_<uuid>"
  2. backend.CreateCollection(ctx, id, dim)     # 让后端建好物理集合
  3. 构造 VectorStore 对象（status=active）
  4. 写进内存索引 stores[id]
  5. registry.SaveStore(ctx, vs)                # 持久化元数据
  6. 返回 clone(vs)

GetStore / ListStores
  → 只读内存索引（ListStores 还做排序 + 游标分页）

DeleteStore(id)
  1. 从内存索引删除
  2. registry.DeleteStore(ctx, id)             # 删元数据
  3. backend.DeleteCollection(ctx, id)          # 删底层集合与全部向量
```

注意第 5 步的一个细节：即使 `registry.SaveStore` 失败，`CreateStore` 也**先返回已创建的库**（带上错误），因为物理集合已经建好了——宁可让用户拿到一个可用对象再去追元数据落盘，也不要回滚已建的集合。

#### 4.1.3 源码精读

先看领域模型的「主角」`VectorStore`，字段命名刻意贴近 OpenAI：

[src/semantic-router/pkg/vectorstore/types.go:29-39](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/types.go#L29-L39) — `VectorStore` 结构体：`Object` 恒为 `"vector_store"`、`Status` 取 `active`/`expired`、`FileCounts` 记录文件处理计数、`BackendType` 标注 `"memory"`/`"milvus"`/`"llama_stack"` 等。

真正写入向量库的最小单位是 `EmbeddedChunk`，它把「块正文 + 嵌入向量 + 归属信息」打包在一起：

[src/semantic-router/pkg/vectorstore/types.go:99-107](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/types.go#L99-L107) — `EmbeddedChunk`：`Embedding []float32` 是嵌入向量，`ChunkIndex` 是块在文件内的序号，`VectorStoreID` 标明它属于哪个库。

> 注意向量用 `float32` 而非 `float64`：嵌入维度常达数百上千，`float32` 能省一半内存，也契合 SIMD 点积优化（见 u9-l1）。

接着是**本层最重要的契约**——`VectorStoreBackend` 接口，只有 7 个方法：

[src/semantic-router/pkg/vectorstore/service.go:22-44](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/service.go#L22-L44) — 接口定义：集合的 `CreateCollection` / `DeleteCollection` / `CollectionExists`，块的 `InsertChunks` / `DeleteByFileID`，检索 `Search`，以及 `Close`。

接口刻意收得很窄——注意它**没有**「切块」「嵌入」「文件上传」「状态机」等方法。这些跨后端共享的逻辑留在流水线里（见 4.2），保证换库不动业务代码。

ID 命名有统一约定，靠前缀区分对象类型：

[src/semantic-router/pkg/vectorstore/types.go:128-147](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/types.go#L128-L147) — `vs_` / `file_` / `vsf_` 三个前缀常量与对应的 `Generate*ID()`，用 UUID 拼前缀生成全局唯一 ID。

最后看 `Manager` 的编排。`CreateStore` 是「双写」（后端建集合 + 注册表存元数据）的典型：

[src/semantic-router/pkg/vectorstore/manager.go:89-116](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/manager.go#L89-L116) — `CreateStore`：先 `backend.CreateCollection` 建物理集合，再写内存索引，再 `registry.SaveStore` 持久化；第 112-114 行即使落盘失败也返回已建的库。

`DeleteStore` 是对称的「双删」，但顺序相反——先删元数据，再删底层集合：

[src/semantic-router/pkg/vectorstore/manager.go:227-244](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/manager.go#L227-L244) — `DeleteStore`：第 240 行调用 `backend.DeleteCollection` 删除底层集合及其全部向量。

启动时，`Manager` 用 `LoadFromRegistry` 从持久化注册表恢复库存清单（底层集合由后端自己管理，不在恢复范围）：

[src/semantic-router/pkg/vectorstore/manager.go:52-64](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/manager.go#L52-L64) — `LoadFromRegistry`：`registry.ListStores` 读全部元数据，写进内存索引，**启动时调用一次**。

`Manager` 的并发安全靠一把 `sync.RWMutex`：读（Get/List）走读锁，写（Create/Update/Delete/UpdateFileCounts）走写锁，且所有返回都 `clone*` 一份，避免调用方拿到内部指针后误改。`StoreRegistry` 接口本身只定义了 4 个元数据方法：

[src/semantic-router/pkg/vectorstore/metadata_registry.go:23-28](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/metadata_registry.go#L23-L28) — `StoreRegistry` 接口：`SaveStore` / `GetStore` / `ListStores` / `DeleteStore`，内存与 PostgreSQL 各有一套实现。

#### 4.1.4 代码实践

**实践目标**：确认「接口有多窄、模型有多厚」，并理解 Manager 双写顺序。

**操作步骤**：

1. 打开 `pkg/vectorstore/service.go`，数 `VectorStoreBackend` 接口的方法个数与签名。
2. 打开 `pkg/vectorstore/types.go`，统计领域结构体的数量（含 `VectorStore`、`FileCounts`、`VectorStoreFile`、`FileRecord`、`ChunkingStrategy`、`StaticChunkConfig`、`SearchResult`、`EmbeddedChunk`、`ExpirationPolicy`、`FileError`、`TextChunk`）。
3. 打开 `manager.go` 的 `CreateStore`（L89）与 `DeleteStore`（L227），对比两者的「后端调用」与「注册表调用」顺序。

**需要观察的现象**：接口只有 7 个方法；模型有 11 个结构体；`CreateStore` 是「先建集合后存元数据」，`DeleteStore` 是「先删元数据后删集合」。

**预期结果**：你能口头复述「薄接口 + 厚模型 + 双写」三句话，并指出 `CreateStore` 在落盘失败时为何不回滚（物理集合已建，回滚反而丢失资源）。

#### 4.1.5 小练习与答案

**练习 1**：如果 `registry.SaveStore` 在 `CreateStore` 里失败了，底层集合已经建好。SR 为什么选择「返回库 + 返回错误」而不是回滚？

**参考答案**：底层物理集合（如 Milvus collection）已经创建并占用资源，回滚需要再调用 `DeleteCollection`，而此时注册表里没有该库的任何记录，删除逻辑（依赖内存索引命中）反而难以触达。SR 选择保留已建对象，把元数据落盘失败作为可追查的错误返回，让上层重试或告警，避免「资源已建但上层看不见」的更糟状态。

**练习 2**：`VectorStoreBackend` 接口为什么不含「切块」「嵌入」方法？

**参考答案**：因为这两步与具体向量库无关——无论用 Milvus 还是内存，切块算法和嵌入模型都一样。把它们留在接口外、放进 `IngestionPipeline`，能保证换后端时业务逻辑零改动，也避免每个后端重复实现一遍。

---

### 4.2 摄入流水线：上传→解析→切块→嵌入→写入

#### 4.2.1 概念说明

用户上传一个文件后，它不会立即变得可检索——它要经历一条完整的**异步摄入流水线**（ingestion pipeline）。`IngestionPipeline` 就是这条流水线的实现：它由若干 worker goroutine 组成，从一个带缓冲的 job 队列里取任务，依次完成「读文件 → 解析文本 → 切块 → 嵌入 → 写入后端」五步，期间持续更新文件状态（`in_progress` → `completed`/`failed`）。

为什么是异步？因为嵌入（embedding）很慢——每块文本都要过一次嵌入模型。如果同步处理，上传一个大文档会让 HTTP 请求阻塞数十秒。所以 SR 让 `AttachFile` 立即返回一个 `status=in_progress` 的记录，把真正的工作丢进后台，用户靠轮询 `GetFileStatus` 得知进度。

这里有一个贯穿全流水线的关键设计：**生命周期取消（lifecycle cancellation）**。整个流水线持有一个根 context（`rootCtx`），每个 job 的工作都从它派生。当 `Stop` 被调用（比如进程关闭），`rootCancel()` 一触发，所有在途 job 会在「阶段检查点」处尽快中止，而不是把一个已经不需要的结果跑完。这正是 `Embedder.Embed` 与各 backend 调用都带 `ctx` 的原因。

#### 4.2.2 核心流程

`processJob` 是单文件处理的内核，伪代码（带阶段检查点）：

```
processJob(ctx, job):
  ① 读文件内容    fileStore.Read(job.FileID)        # 字节
  ② 取文件元信息  fileStore.Get(job.FileID)          # 拿 filename 给解析器
  ③ 解析文本      ExtractText(content, filename)     # 按扩展名 → 纯文本
     ✂ checkpoint: if ctx.Err() != nil → 标记 cancelled
  ④ 切块          ChunkText(text, strategy)          # auto 或 static
  ⑤ 嵌入          逐块 embedder.Embed(ctx, chunk)    # 每块都检查 ctx
     ✂ checkpoint: if ctx.Err() != nil → 标记 cancelled
  ⑥ 写入          backend.InsertChunks(ctx, vsID, embeddedChunks)
  → setFileStatus(completed)
```

每一步失败都会走 `failJob`，按失败原因写入不同的 `code`（`read_error` / `parse_error` / `empty_content` / `embedding_error` / `storage_error` / `cancelled`），并把 `FileCounts.InProgress--`、`Failed++`，保证计数与状态始终一致。

切块（chunking）有两种策略：

- `auto`（默认）：按段落（双换行）和 Markdown 标题切，每段再按定长兜底切。
- `static`：定长 + 重叠。默认 `maxChunkSize=800`、`overlap=400`（字符级，对 token 的近似）。

定长切块的重叠步长为：

\[
\text{step} = \text{maxSize} - \text{overlap}
\]

默认 step = 800 − 400 = 400，即相邻块共享一半正文，避免句子在边界被截断造成语义丢失。

#### 4.2.3 源码精读

先看流水线的结构与生命周期字段：

[src/semantic-router/pkg/vectorstore/pipeline.go:52-70](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/pipeline.go#L52-L70) — `IngestionPipeline`：`jobQueue` 是带缓冲的任务通道，`fileStatuses` 记录每个 vsf 的状态，`rootCtx`/`rootCancel` 是生命周期根，`running` 是启停标志。注释 L64-68 说明所有 job 工作都从 `rootCtx` 派生，取消它即通知全部在途 job。

`Embedder` 接口只有两个方法——这是流水线对嵌入能力的全部依赖：

[src/semantic-router/pkg/vectorstore/pipeline.go:31-34](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/pipeline.go#L31-L34) — `Embedder` 接口：`Embed(ctx, text)` 与 `Dimension()`。注释明确 `ctx` 用于在检查点中断嵌入工作。

`processJob` 是六步内核，注意每两步之间的 `ctx.Err()` 检查点：

[src/semantic-router/pkg/vectorstore/pipeline.go:349-411](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/pipeline.go#L349-L411) — `processJob`：步骤 1-6 注释清晰（L355/L362/L370/L381/L388/L400），L376 与 L394 是两个显式取消检查点，L406 成功后更新 `completed` 计数。

嵌入阶段对「取消」与「真正错误」做了区分——一个协作的 embedder 在生命周期取消时会返回 `context.Canceled`，这不该被误记为嵌入故障：

[src/semantic-router/pkg/vectorstore/pipeline.go:416-450](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/pipeline.go#L416-L450) — `embedChunks`：L431-433 用 `errors.Is(err, context.Canceled)` 把取消记为 `cancelled` 而非 `embedding_error`，避免污染故障指标。

`AttachFile` 是入口：校验文件与库存在后，建一条 `in_progress` 记录、更新计数、把 job 入队；队列满时立即标 `failed(queue_full)`：

[src/semantic-router/pkg/vectorstore/pipeline.go:184-255](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/pipeline.go#L184-L255) — `AttachFile`：L236-254 是「入队或队列满」的双分支；入队成功返回 `in_progress` 快照。

切块逻辑在 `chunking.go`，`auto` 策略按段落与标题切，每段再走定长兜底：

[src/semantic-router/pkg/vectorstore/chunking.go:31-42](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/chunking.go#L31-L42) — `ChunkText`：`nil` 或空策略走 `auto`，否则 `static`。

[src/semantic-router/pkg/vectorstore/chunking.go:136-181](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/chunking.go#L136-L181) — `chunkStatic`：按 rune 切（L147，正确处理多字节字符），步长 `step = maxSize - overlap`（L173），重叠 ≥ maxSize 时退化为一半（L143-145）。

文本解析按扩展名分发，支持 txt/md/json/csv/html 五类：

[src/semantic-router/pkg/vectorstore/parser.go:34-49](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/parser.go#L34-L49) — `ExtractText`：未知扩展名直接报错（L47），不在摄入后期才失败。

最后，`Embedder` 的默认实现是 `CandleEmbedder`，经 Rust 的 Candle FFI 做本地嵌入（需 CGO，构建标签 `!windows && cgo`）：

[src/semantic-router/pkg/vectorstore/candle_embedder.go:52-86](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/candle_embedder.go#L52-L86) — `Embed`：先检查 `ctx.Err()`（L53），再按 `modelType`（bert/qwen3/gemma/mmbert/multimodal）分派到对应 FFI；FFI 调用本身同步不可中断，注释（L49-51）说明中断在途调用是后续工作。

#### 4.2.4 代码实践

**实践目标**：跟踪一次摄入的全链路，理解阶段检查点与状态机。

**操作步骤**（源码阅读型）：

1. 从 `AttachFile`（pipeline.go L184）出发，画出从「HTTP 上传」到「`completed`」的状态迁移图，标注每一步对应的 `failJob` code。
2. 在 `processJob`（L349）里找出**全部** `ctx.Err()` 检查点的行号（至少 3 处）。
3. 阅读测试 `pkg/vectorstore/pipeline_lifecycle_test.go` 与 `pipeline_embedder_ctx_test.go`，找到验证「取消即在检查点中止」的用例。

**需要观察的现象**：检查点出现在「解析后、切块前」「嵌入后、写入前」「每块嵌入前」；取消时状态码是 `cancelled` 而非 `embedding_error`。

**预期结果**：你能画出含 6 个步骤、≥3 个检查点、6 种失败码的状态图。

> 可运行验证（**待本地验证**）：若本机已配好 CGO 与 candle 依赖，可运行
> `cd src/semantic-router && go test ./pkg/vectorstore/ -run TestIngestion -v`
> 观察流水线测试输出；切块逻辑本身是纯 Go，但 `pkg/vectorstore` 包含 CGO 文件（`candle_embedder.go`），未配 CGO 时该包无法编译，相关命令输出以本地环境为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `embedChunks` 要把 `context.Canceled` 单独记为 `cancelled`，而不是混进 `embedding_error`？

**参考答案**：取消是「有意的生命周期终止」（如 `Stop` 关闭流水线），不是嵌入后端的故障。若混进 `embedding_error`，会污染故障计数与告警，让人误以为嵌入模型坏了。区分二者让运维能准确判断「是模型挂了」还是「只是进程在关闭」。

**练习 2**：默认切块 `maxSize=800, overlap=400`。一个长度 1600 字符的纯文本会被切成几块？每块覆盖哪些字符区间？

**参考答案**：step = 800 − 400 = 400。块覆盖区间为 [0,800)、[400,1200)、[800,1600)，共 3 块，相邻块重叠 400 字符。注意这是按 rune 计数，多字节字符不会被截断。

---

### 4.3 多后端抽象：可替换的向量库后端

#### 4.3.1 概念说明

5 种后端实现同一个 `VectorStoreBackend` 接口，但内部天差地别。`factory.go` 的 `NewBackend` 是唯一的切换点：传入一个 `backendType` 字符串与一个聚合配置 `BackendConfigs`，返回对应后端实例。聚合配置里**只有与所选类型匹配的那一项会被用到**，其余忽略——这是为了让上层用一份配置结构同时承载多种后端的参数，由类型决定读哪一段。

五种后端的差异可以用一张表概括：

| 后端 | 协议 | 检索方式 | 持久化 | 典型场景 |
| --- | --- | --- | --- | --- |
| `memory` | 进程内 | **暴力余弦**（O(n)） | 否（重启丢） | 开发/测试 |
| `milvus` | gRPC | **HNSW + 内积（IP）** | 是 | 生产、大规模 |
| `qdrant` | gRPC | HNSW | 是 | 生产 |
| `valkey` | Redis 协议（Glide 客户端） | 服务端 HNSW 向量索引 | 是 | 已有 Redis/Valkey 栈 |
| `llama_stack` | HTTP | 由 Llama Stack 服务端代劳（连嵌入都代做） | 是 | Llama Stack 生态 |

一个值得强调的细节：**内存后端是唯一原生支持混合检索（HybridSearch）的**，因为它在进程内维护了 BM25 与 n-gram 索引。Milvus/Qdrant 等外部库不做文本侧重排，于是 SR 提供了一个通用的 `GenericHybridRerank` 回退路径——先做一次扩大 topK 的向量检索，再在返回的候选集上临时建 BM25/n-gram 索引重排，让任何后端都能获得「向量 + 关键词」的混合检索能力。

> 与 u9-l1 的关系：u9-l1 讲的 `pkg/hnsw`（进程内 HNSW 图）主要被**语义缓存**与**记忆**子系统使用；而本讲的 `MemoryBackend` 走的是更简单的暴力余弦（`cosineSimilarity`），因为向量库面向的是文档级、可重建的数据，开发期不需要 HNSW 的复杂度。真正的规模化检索交给 Milvus/Qdrant 的服务端 HNSW。

#### 4.3.2 核心流程

后端选择流程极简：

```
NewBackend(backendType, cfgs):
  switch backendType:
    "memory"     → NewMemoryBackend(cfgs.Memory)
    "milvus"     → NewMilvusBackend(cfgs.Milvus)
    "llama_stack"→ NewLlamaStackBackend(cfgs.LlamaStack)
    "valkey"     → NewValkeyBackend(cfgs.Valkey)
    "qdrant"     → NewQdrantBackend(cfgs.Qdrant)
    其它         → 报错（并列出支持的 5 种）
```

检索时（以向量检索为例），后端的差异落在 `Search` 实现上：

- **内存**：遍历全部 chunk，逐个算余弦相似度，过滤阈值，排序，取 topK。
- **Milvus**：把查询向量化成 Milvus `Search` 调用，用 HNSW 检索 + IP 度量，由服务端返回近邻。

余弦相似度的定义为：

\[
\cos(a, b) = \frac{a \cdot b}{\lVert a \rVert \cdot \lVert b \rVert}
\]

当向量已 L2 归一化时，\(\lVert a \rVert = \lVert b \rVert = 1\)，余弦相似度即等于内积 \(a \cdot b\)。这正是 Milvus 用 `entity.IP`（内积）度量、而嵌入向量已归一化时，两者检索结果等价的原因。

#### 4.3.3 源码精读

工厂与 5 个后端类型常量：

[src/semantic-router/pkg/vectorstore/factory.go:22-58](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/factory.go#L22-L58) — 后端类型常量（L22-28）、聚合配置 `BackendConfigs`（L32-38）、`NewBackend` 的 switch（L43-57）。注意 L55-56 的报错信息会列出全部 5 种支持的类型，方便排错。

**内存后端**——`Search` 是最直白的暴力余弦，便于理解「检索」到底在做什么：

[src/semantic-router/pkg/vectorstore/memory_backend.go:148-209](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/memory_backend.go#L148-L209) — `Search`：L174-193 遍历全部 chunk，逐个 `cosineSimilarity`，过阈值（L181）进候选，L196-198 按分数降序排序，L200-202 截断 topK。

[src/semantic-router/pkg/vectorstore/memory_backend.go:334-351](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/memory_backend.go#L334-L351) — `cosineSimilarity`：上面公式的直接实现，向量长度不等或为空返回 0。

内存后端还在每次块变更后重建文本索引（BM25/n-gram），从而原生支持混合检索：

[src/semantic-router/pkg/vectorstore/memory_backend.go:103-122](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/memory_backend.go#L103-L122) — `InsertChunks`：插入后 L120 调 `rebuildTextIndexes`，让后续 `HybridSearch` 能用上最新的 BM25/n-gram 索引。

**Milvus 后端**——与内存后端形成鲜明对比，检索交给服务端 HNSW：

[src/semantic-router/pkg/vectorstore/milvus_backend.go:262-334](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/milvus_backend.go#L262-L334) — `Search`：L279 用 `NewIndexHNSWSearchParam(searchEf)` 建 HNSW 搜索参数，L284-287 调 `client.Search`，度量 `entity.IP`（内积），L296 按阈值过滤。客户端不做任何遍历——全部近邻查找在 Milvus 服务端完成。

写入也走 Milvus 的列式 Upsert + Flush：

[src/semantic-router/pkg/vectorstore/milvus_backend.go:196-243](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/milvus_backend.go#L196-L243) — `InsertChunks`：把 `[]EmbeddedChunk` 拆成 7 个并列列（id/file_id/filename/content/embedding/chunk_index/created_at），L225 一次 `Upsert`，L238 `Flush` 落盘。

Milvus 后端还有一处安全细节：过滤表达式由用户传入的 `file_id` 拼接，必须防注入。后端用正则白名单限制只允许字母数字与 `_-`：

[src/semantic-router/pkg/vectorstore/milvus_backend.go:246-257](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/milvus_backend.go#L246-L257) — `DeleteByFileID`：L247 先用 `safeIdentifierPattern` 校验 `fileID`，不合法直接拒绝，避免 Milvus 表达式注入。

混合检索的通用回退路径，让任何后端都能获得「向量 + 关键词」融合：

[src/semantic-router/pkg/vectorstore/hybrid.go:459-550](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/hybrid.go#L459-L550) — `GenericHybridRerank`：L476-479 把 topK 放大 4 倍（至少 50）取候选，L505-510 在候选上临时建 BM25/n-gram 索引，L513 三路分数融合，再按调用方阈值与 topK 截断。这是「后端只管向量、重排由 SR 统一做」的体现。

融合有两种模式：加权（默认，权重 0.7/0.2/0.1）与 RRF。RRF 的得分公式为：

\[
\text{score}(d) = \sum_{r} \frac{1}{k + \text{rank}_r(d)}
\]

其中 \(r\) 遍历各检索器（向量/BM25/n-gram），\(\text{rank}_r(d)\) 是文档 \(d\) 在检索器 \(r\) 结果中的名次，\(k\) 默认 60。

#### 4.3.4 代码实践

**实践目标**：对比内存与 Milvus 两后端的 `Search`/`InsertChunks`/`DeleteByFileID` 实现，理解「同一接口、不同实现」。

**操作步骤**：

1. 打开 `memory_backend.go` 的 `Search`（L148）与 `milvus_backend.go` 的 `Search`（L262），逐行对比：内存做了什么循环？Milvus 把工作交给了谁？
2. 对比两者的 `InsertChunks`：内存后端是 `map[id]=chunk` 一次赋值；Milvus 后端要做哪两步（Upsert + Flush）？
3. 找出 Milvus 后端独有的安全机制（`safeIdentifierPattern`，L33），说明内存后端为什么不需要它。

**需要观察的现象**：内存后端的所有计算在客户端进程内（遍历、余弦、排序）；Milvus 后端的近邻检索完全在 Milvus 服务端，客户端只发一次 `Search` RPC。内存后端不拼接任何外部表达式，故无需注入防护。

**预期结果**：你能用一句话概括差异——「内存 = 客户端暴力算；Milvus = 服务端 HNSW 算」。

#### 4.3.5 小练习与答案

**练习 1**：`NewBackend` 收到一个不认识的 `backendType`（比如 `"redis"`）会怎样？

**参考答案**：走到 `default` 分支返回错误，错误信息形如 `unsupported backend type: redis (supported: memory, milvus, llama_stack, valkey, qdrant)`（factory.go L55-56），并列出全部 5 种受支持类型，便于使用者自查拼写。

**练习 2**：为什么 `GenericHybridRerank` 要先把 topK 放大 4 倍再做重排？

**参考答案**：向量检索只取 topK 个候选时，可能漏掉「向量相似度一般、但关键词命中度很高」的文档。先把候选池放大（4×topK，至少 50），给 BM25/n-gram 重排留出更丰富的候选，再用融合分数筛回 topK，能显著提升混合检索的召回质量。

---

### 4.4 Milvus 生命周期：连接、集合 ensure/load 与重试

#### 4.4.1 概念说明

`pkg/milvus` 是一个**共享生命周期包**，不属于 vectorstore 专有。它的存在回答一个问题：「多个用 Milvus 的子系统（向量库、语义缓存、记忆、router replay 等）都要做『连接 Milvus、确保集合存在并加载、失败时重试』这三件几乎一模一样的琐事，能不能抽到一起？」

答案是能。`pkg/milvus` 把这三件事抽成一组纯函数：

- **连接**：`ConnectGRPC` / `Connect`——建立 gRPC 客户端，带超时。
- **ensure/load**：`EnsureCollectionLoaded`（及带钩子、带重试的变体）——「集合不存在就建、存在就（可选）校验、最后加载进内存供查询」。
- **重试**：`RetryIf`——有界指数退避，只对「瞬时错误」重试。

为什么集合要先 `Load`？这是 Milvus 的设计：集合创建后数据驻留在磁盘/对象存储，必须显式 `LoadCollection` 把它加载到查询节点的内存里，才能做向量检索。加载是异步收敛的，尤其在 standalone Milvus 重启后，gRPC 端口虽已开放，但内部 proxy/data node 元数据还在对齐——此时 `LoadCollection` 可能报「server is not ready」「node not match」等**瞬时错误**。`IsTransientLifecycleError` 就是用来识别这类「可重试」错误的。

#### 4.4.2 核心流程

集合 ensure/load 的核心逻辑（`EnsureCollectionLoadedWithHooks`）：

```
EnsureCollectionLoadedWithHooks(ctx, client, name, createIfMissing, onExists):
  1. exists = client.HasCollection(ctx, name)
  2. if exists && onExists != nil:  onExists(ctx)        # 校验已有集合（如维度匹配）
  3. if !exists:                    createIfMissing(ctx)  # 建集合 + 建索引
  4. client.LoadCollection(ctx, name, async=false)        # 加载到内存供查询
```

带重试的版本在外面包一层 `RetryIf`，对瞬时错误做有界指数退避。退避间隔为：

\[
\text{delay}_i = \text{baseDelay} \times 2^i
\]

默认 `baseDelay = 2s`、`attempts = 3`，故间隔序列约 2s、4s（第 3 次为最后一次不再退避）。重试只在 `IsTransientLifecycleError(err)` 为真时进行；非瞬时错误立即返回，不浪费时间。

#### 4.4.3 源码精读

`ConnectGRPC` 封装带超时的 Milvus gRPC 拨号，地址为空立即报错：

[src/semantic-router/pkg/milvus/lifecycle.go:41-59](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/milvus/lifecycle.go#L41-L59) — `ConnectGRPC`：`timeout>0` 时用 `context.WithTimeout` 包一层拨号上下文（L48-50），失败时附上地址便于排错（L55）。

ensure/load 的核心，步骤 1-4 清晰：

[src/semantic-router/pkg/milvus/lifecycle.go:94-125](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/milvus/lifecycle.go#L94-L125) — `EnsureCollectionLoadedWithHooks`：L105 查存在性，L110-114 已存在则跑 `onExists` 钩子，L115-119 不存在则 `createIfMissing`，L121 最后 `LoadCollection(async=false)`。

瞬时错误识别——靠错误信息子串匹配：

[src/semantic-router/pkg/milvus/lifecycle.go:165-186](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/milvus/lifecycle.go#L165-L186) — `IsTransientLifecycleError`：把错误信息小写化，匹配 `connection refused` / `server is not ready` / `node not match` / `unavailable` 等 8 个标记串。注释（L162-164）解释这些是 standalone Milvus 重启后、端口已开但元数据仍在收敛时出现的瞬时错误。

有界指数退避重试：

[src/semantic-router/pkg/milvus/lifecycle.go:201-247](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/milvus/lifecycle.go#L201-L247) — `RetryIf`：L238 `delay := baseDelay * (1<<i)` 即 baseDelay × 2ⁱ；L229-231 若 `shouldRetry` 判定不可重试则立即返回；L239-243 退避期间响应 `ctx.Done()` 以支持中途取消。

vectorstore 的 Milvus 后端正是 `EnsureCollectionLoadedWithRetry` 的使用者——`CreateCollection` 把「建 schema + 建 HNSW 索引」作为 `createIfMissing` 回调传进去：

[src/semantic-router/pkg/vectorstore/milvus_backend.go:104-175](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/milvus_backend.go#L104-L175) — `CreateCollection`：L117 调 `EnsureCollectionLoadedWithRetry`，回调内 L118-159 定义 7 字段 schema（id/file_id/filename/content/embedding/chunk_index/created_at），L166 用 `entity.NewIndexHNSW(entity.IP, indexM, indexEf)` 在 embedding 字段建 HNSW + 内积索引。注意它传入的是零值 `CollectionRetryOptions{}`（L174），经 `normalizedCollectionRetryOptions` 归一为 attempts=3、baseDelay=2s（见 lifecycle.go L30-38）。

这条「调用方只管 schema、生命周期琐事交给共享包」的边界，正是 `pkg/milvus` 注释里强调的「领域包保留 schema 与查询语义，本包只管重复的生命周期机制」（lifecycle.go L1-5）。

#### 4.4.4 代码实践

**实践目标**：理解共享生命周期包如何被具体后端复用，以及瞬时错误重试的触发条件。

**操作步骤**：

1. 打开 `lifecycle.go` 的 `EnsureCollectionLoadedWithHooksRetry`（L141），跟踪它如何把 `EnsureCollectionLoadedWithHooks` 包进 `RetryIf`。
2. 打开 `milvus_backend.go` 的 `CreateCollection`（L104），找出它传给共享包的两个回调（`createIfMissing` 与隐含的 `onExists=nil`）。
3. 阅读 `milvus_backend_test.go`，找到验证「瞬时错误触发重试」的用例（通常会构造返回 `server is not ready` 的假 client）。

**需要观察的现象**：调用方无需自己写「查存在→建→加载→重试」的样板，只需提供「怎么建集合」这一个回调；重试只对 `IsTransientLifecycleError` 列出的瞬时错误生效，其它错误一次就返回。

**预期结果**：你能复述 ensure/load 的 4 步、退避公式 baseDelay×2ⁱ，以及为何只重试瞬时错误。

> 可运行验证（**待本地验证**）：Milvus 后端的集成测试通常需要真实或 mock 的 Milvus 服务，输出以本地环境为准；`IsTransientLifecycleError` 是纯函数，可在本地用 `go test ./pkg/milvus/ -run IsTransient -v`（若该包无 CGO 依赖）验证其匹配规则。

#### 4.4.5 小练习与答案

**练习 1**：为什么集合创建后还要 `LoadCollection`？

**参考答案**：Milvus 把数据持久化在磁盘/对象存储，`CreateCollection` 只是定义 schema 与索引。要执行向量检索，必须把数据加载到查询节点的内存中——这就是 `LoadCollection` 的职责（lifecycle.go L121）。未加载的集合无法 `Search`。

**练习 2**：`RetryIf` 为什么不重试所有错误，而要靠 `IsTransientLifecycleError` 筛选？

**参考答案**：非瞬时错误（如 schema 字段非法、维度不匹配、权限不足）重试再多次结果也一样，盲目重试只会延长失败时间、占满重试预算。只对「服务正在启动/收敛」这类瞬时错误重试，才能在 standalone Milvus 重启的窗口期自动恢复，又不在确定性错误上浪费时间。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，描述「一个用户上传 PDF 等价文本文件，到能被检索命中」的完整旅程，并说明在内存与 Milvus 两种后端下的实现差异。

**请完成一份图文回答，覆盖以下要点**：

1. **领域对象流转**：从 `FileRecord`（上传）→ `VectorStoreFile`（挂载、`in_progress`）→ `EmbeddedChunk[]`（切块嵌入后）→ `SearchResult[]`（检索命中），每个对象由谁创建、由谁持久化。引用 `types.go` 的对应结构体。
2. **Manager 的双写**：在创建库与删除库时，`Manager` 如何同时操作「后端」与「StoreRegistry」。引用 `manager.go` 的 `CreateStore`（L89）与 `DeleteStore`（L227）。
3. **摄入六步与检查点**：画出 `processJob` 的步骤序列，标出 `ctx.Err()` 检查点的位置与对应取消码。引用 `pipeline.go` L349-411。
4. **两后端 `Search` 对比**：用一段话说明内存后端（暴力余弦，memory_backend.go L148）与 Milvus 后端（服务端 HNSW + IP，milvus_backend.go L262）的实现差异，并指出哪种适合生产、哪种适合开发。
5. **Milvus 生命周期的复用**：说明 Milvus 后端的 `CreateCollection` 如何把「建集合 + 建索引」作为回调交给 `pkg/milvus` 的 `EnsureCollectionLoadedWithRetry`，而连接、加载、重试由共享包统一处理。引用 lifecycle.go L94-125 与 L201-247。

**自检清单**（对照检查你的回答）：

- [ ] 是否区分了「向量存后端」与「元数据存 StoreRegistry」？
- [ ] 是否说明了摄入是异步、`AttachFile` 立即返回 `in_progress`？
- [ ] 是否解释了 Milvus 为何用内积（IP）度量而内存用余弦，且二者在归一化向量下等价？
- [ ] 是否指出了混合检索（HybridSearch）的两种路径：内存原生 vs `GenericHybridRerank` 回退？
- [ ] 是否说明了 `IsTransientLifecycleError` 决定重试与否？

> 本实践以源码阅读与图解为主，不要求真实运行 Milvus。若需实操，可先用内存后端（`BackendTypeMemory`）在本地走通「建库 → 挂文件 → 轮询状态 → 检索」全流程（**待本地验证**：依赖 CGO/candle 编译环境）。

## 6. 本讲小结

- `pkg/vectorstore` 是 OpenAI Vector Stores API 的对齐实现：**厚模型**（11 个领域结构体）+ **薄接口**（`VectorStoreBackend` 仅 7 个方法）。
- `Manager` 是编排者，做「后端（存向量）+ StoreRegistry（存元数据）」的双写/双删，并维护进程内索引加速查询；启动时用 `LoadFromRegistry` 恢复库存清单。
- `IngestionPipeline` 是异步六步流水线（读→解析→切块→嵌入→写入），用根 context 贯穿生命周期取消，在每个阶段检查点响应 `Stop`，并把取消与真实故障区分记账。
- 切块分 `auto`（段落/标题）与 `static`（定长 + 重叠，默认 800/400，步长 = maxSize − overlap）。
- 5 种后端实现同一接口：内存走暴力余弦（开发用，原生支持混合检索），Milvus/Qdrant/Valkey 走服务端 HNSW，LlamaStack 连嵌入都由服务端代劳；`GenericHybridRerank` 为不支持文本重排的后端提供通用混合检索回退。
- `pkg/milvus` 是共享生命周期包，把「连接、集合 ensure/load、瞬时错误重试」抽成纯函数，供 vectorstore/cache/memory/routerreplay 等多个子系统复用，调用方只管提供 schema。

## 7. 下一步学习建议

- **语义缓存**：本讲的向量库是「文档检索」用途；下一篇 [u9-l3 语义缓存](u9-l3-semantic-cache.md) 讲「请求-响应」级别的缓存，它会复用 HNSW 与多后端思想，但加了用户作用域隔离（HMAC 命名空间）与 hybrid 两级缓存，可对照阅读。
- **智能体记忆**：[u9-l4 Agentic Memory](u9-l4-agentic-memory.md) 把「向量检索 + BM25 重排」用于记忆系统，与本讲的混合检索同源，可看到 `pkg/memory` 如何借鉴而非照搬 `pkg/vectorstore`。
- **深入 Milvus 子系统**：阅读 `pkg/cache/hybrid_cache.go`、`pkg/memory/milvus_store.go`，观察它们如何各自调用 `pkg/milvus` 的 `EnsureCollectionLoadedWithRetry`，体会「共享生命周期包」的复用价值。
- **混合检索数学**：若对 BM25 的 IDF 公式与 RRF 融合感兴趣，精读 `pkg/vectorstore/hybrid.go` 的 `BM25Index.Score`（L148）与 `fuseRRF`（L406），对照本讲的公式理解每一步。
