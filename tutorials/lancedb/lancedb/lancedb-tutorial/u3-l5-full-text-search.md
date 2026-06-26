# 全文检索 FTS

## 1. 本讲目标

前面几讲我们学会了「按标量条件过滤」和「按向量相似度排序」两种查询。但现实中还有一类非常常见的检索需求：**给一段文字，找出内容最相关的文档**。这正是全文检索（Full-Text Search，简称 FTS）要解决的问题。

本讲学完后，你应该能够：

1. 说清楚「全文检索」和「标量过滤 `contains`」「向量搜索」三者的区别，理解 FTS 为什么需要专门的倒排索引。
2. 看懂 LanceDB 里 `Index::FTS` 这一索引变体的本质（它其实是底层 `lance_index` 的 `InvertedIndexParams`），并掌握创建 FTS 索引的完整步骤。
3. 理解 BM25 打分公式的直觉含义，知道结果为什么按 `_score` 列降序返回。
4. 用 `QueryBase::full_text_search` 写出一次关键词全文检索，并解释它的返回结果。

本讲只覆盖两个最小模块：**index (FTS)**（创建全文索引）与 **query**（执行全文检索查询）。BM25 是贯穿这两个模块的底层原理，所以我们先从它讲起。

## 2. 前置知识

- **向量搜索的回顾**：`Query::nearest_to` 按向量距离排序，结果带一个 `_distance` 列（见 u3-l2）。本讲的 FTS 与之平行，但结果带的是 `_score` 列。
- **索引的作用**：索引是一种「以空间换时间」的预计算结构。没有索引时，FTS 只能对每行文本做扫描比对；有了倒排索引，就能像查字典一样快速定位包含某个词的行。
- **分词（tokenize）**：把一段文本切成一个个「词项（term）」。例如 `"hello world"` 分成 `["hello", "world"]`。FTS 是建立在**词项**之上的，而不是原始字节，这是它和 FM 索引（子串匹配）的根本区别。
- **倒排索引（inverted index）**：普通索引是「行 → 内容」，倒排索引反过来是「词项 → 出现该词的所有行（的列表）」。这是几乎所有搜索引擎（Elasticsearch、Lucene）的核心数据结构。
- **Builder 模式**：LanceDB 贯穿「先配置、`.execute().await` 才生效」的风格，FTS 的建索引和查询都不例外（见 u3-l1、u3-l4）。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `rust/lancedb/src/index.rs` | 定义 `Index` 枚举（含 `FTS` 变体）和 `IndexType`（FTS 对应 `Inverted`）。 |
| `rust/lancedb/src/index/scalar.rs` | 把底层 `lance_index` 的 `InvertedIndexParams`/`FullTextSearchQuery` 重新导出为 `FtsIndexBuilder`/`FullTextSearchQuery`，是 FTS 的真正实现入口。 |
| `rust/lancedb/src/table/create_index.rs` | `create_index` 的实现，负责校验列类型并把 `Index::FTS` 翻译成底层参数。 |
| `rust/lancedb/src/query.rs` | `QueryBase::full_text_search` 方法、`QueryRequest.full_text_search` 字段、`DEFAULT_TOP_K` 与 `_score` 列引用。 |
| `rust/lancedb/examples/full_text_search.rs` | 官方最小可运行示例，串起 create_table → create_index → full_text_search。 |

> 一个重要认知：LanceDB 核心本身**不实现** BM25 与倒排索引，而是把工作委托给底层依赖 `lance_index`。本讲会反复看到这种「薄封装 + 重新导出」的模式。BM25 的真正打分逻辑在 `lance_index` 里（不在本仓库内），所以涉及打分细节时我们会明确标注。

## 4. 核心概念与源码讲解

### 4.1 全文检索与 BM25 打分原理

#### 4.1.1 概念说明

先区分三种「找文本」的方式：

| 方式 | 建立依据 | 典型用法 | 适合场景 |
| --- | --- | --- | --- |
| 标量过滤 `contains(col, 'needle')` | 原始字节子串匹配 | `only_if("contains(doc, 'rust')")` | 精确知道子串、要找完全匹配 |
| FM 索引 | 原始字节子串 + FM-Index 加速 | `Index::Fm` | 加速任意子串（含乱码、无空格）|
| **FTS 全文检索** | **分词后的词项** + **BM25 相关性打分** | `full_text_search(...)` | 「这个查询词在哪些文档里最相关」 |

FTS 的核心价值有两个：

1. **它会排序**：不是「命中/不命中」，而是给每个命中文档算一个相关性分数 `_score`，越相关越靠前。
2. **它对语言友好**：基于词项，所以 `"the quick brown fox"` 搜 `quick` 能命中，搜 `quickly` 则不会（默认不词干还原时）。它还会降低高频无意义词（如 `the`）的权重。

BM25（Best Matching 25）是 FTS 用来打分的经典算法，几乎所有搜索引擎都支持它。直觉是：**一个词在某文档出现越多、且在全库出现越少，它对「该文档与查询相关」的证据就越强**；同时要惩罚过长的文档，避免「长文档天然词频高」的偏差。

#### 4.1.2 核心流程

给定查询 \(Q\)（一组词项）和文档 \(D\)，BM25 分数定义为：

\[
\text{BM25}(D, Q) = \sum_{t \in Q} \text{IDF}(t) \cdot \frac{f(t, D) \cdot (k_1 + 1)}{f(t, D) + k_1 \cdot \left(1 - b + b \cdot \frac{|D|}{\text{avgdl}}\right)}
\]

其中：

- \(f(t, D)\)：词项 \(t\) 在文档 \(D\) 中的出现次数（词频）。
- \(|D|\)：文档 \(D\) 的长度（词项个数）。
- \(\text{avgdl}\)：全库文档平均长度。
- \(k_1\)：词频饱和参数（典型值 \(1.2 \sim 2.0\)）。它让词频的贡献逐渐「饱和」，避免一个词重复出现 1000 次就把分数刷满。
- \(b\)：长度归一化参数（典型值 \(0.75\)），\(b=0\) 完全不看文档长度，\(b=1\) 强惩罚长文档。
- \(\text{IDF}(t)\)：词项的「逆文档频率」，衡量稀有程度：

\[
\text{IDF}(t) = \ln\left(1 + \frac{N - n(t) + 0.5}{n(t) + 0.5}\right)
\]

其中 \(N\) 是全库文档总数，\(n(t)\) 是包含词项 \(t\) 的文档数。\(n(t)\) 越大（词越常见），IDF 越小；极端常见的词几乎不贡献分数。

整个 FTS 检索流程可以概括为：

```
用户查询字符串
   │  (1) 分词 tokenizer
   ▼
查询词项集合 {t1, t2, ...}
   │  (2) 查倒排索引，取出每个词命中的文档及词频 f(t, D)
   ▼
   │  (3) 对每个候选文档，套用 BM25 公式累加分数
   ▼
候选文档 + _score
   │  (4) 按 _score 降序排序，截断到 limit
   ▼
最终结果（每行带一个 Float32 的 _score 列）
```

要点：FTS 不像向量搜索那样需要你提供「查询向量」，你直接给文本字符串即可——分词由底层引擎完成。

#### 4.1.3 源码精读

LanceDB 核心里，FTS 相关的类型几乎全是「从 `lance_index` 重新导出」的薄封装。这一点在 `index/scalar.rs` 文件末尾看得最清楚：

[rust/lancedb/src/index/scalar.rs:63-66](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/scalar.rs#L63-L66) —— 这四行把底层 `lance_index::scalar` 的 `FullTextSearchQuery`、`InvertedIndexParams`（重命名为 `FtsIndexBuilder`）、以及倒排查询模块整体重新导出。也就是说，你写 `FtsIndexBuilder` 实际上就是在用 `lance_index` 的 `InvertedIndexParams`，BM25 的所有可调参数（如 \(k_1\)、\(b\)、分词器选择）都定义在那个类型上，而不是 lancedb 自己实现的。

结果里的打分列名也是从底层拿来的常量：

[rust/lancedb/src/query.rs:20](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L20) —— `use lance_index::scalar::inverted::SCORE_COL;` 引入了打分列常量 `_score`。FTS 结果会自动多出这一列（Float32），值越大越相关。注意它和向量搜索的 `_distance`（越小越相似）方向相反。

因此本讲涉及 BM25 打分细节时，真正的实现不在本仓库，而在 `lance_index` 这一依赖里；lancedb 的职责是「把 FTS 当作一种 `Index` 暴露出来，并在查询时把文本查询传下去」。

#### 4.1.4 代码实践（源码阅读型）

> **实践目标**：确认「FTS 是薄封装」这一认知。
>
> **操作步骤**：
> 1. 打开 [rust/lancedb/src/index/scalar.rs:63-66](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/scalar.rs#L63-L66)。
> 2. 在本仓库根目录用 `cargo doc --features remote -p lance-index --no-deps` 尝试生成文档（或在 [docs.rs/lance_index](https://docs.rs/lance_index) 上）查找 `InvertedIndexParams` 与 `FullTextSearchQuery` 的字段。
>
> **需要观察的现象**：`FtsIndexBuilder` 并非 lancedb 自定义结构体，而是一个 `pub use` 别名；`InvertedIndexParams` 里通常会有 tokenizer 配置、是否做词干还原等选项。
>
> **预期结果**：你会确认 LanceDB 核心不自己写 BM25，索引和查询的「真身」都在 `lance_index`。
>
> **待本地验证**：`cargo doc` 是否能拉到 `lance-index` 源码取决于本地缓存；若拉不到，直接看 `index/scalar.rs` 的 `pub use` 即可得出结论。

#### 4.1.5 小练习与答案

**练习 1**：FTS 检索 `"quick brown"` 时，假如全库里几乎所有文档都含 `the`，而你手滑把查询写成了 `"the quick brown"`，`the` 会怎样影响排序？

**参考答案**：几乎不影响。因为 `the` 的文档频率 \(n(t)\) 接近 \(N\)，它的 IDF \(\ln(1 + \frac{N-n+0.5}{n+0.5})\) 会趋近于 0，对每个文档的分数贡献近乎为零。这正是 BM25 用 IDF 抑制高频停用词的设计意图。

**练习 2**：把 \(b\) 调成 0、\(k_1\) 调成 0，BM25 分别会退化成什么？

**参考答案**：\(b=0\) 时长度归一化项消失，长文档不再被惩罚；\(k_1=0\) 时分子分母里的 \(f(t,D)\) 贡献被抹平，公式退化为「布尔命中」+ 纯 IDF，词频高低不再影响排序。

---

### 4.2 创建 FTS 索引（index 模块）

#### 4.2.1 概念说明

FTS 查询必须依赖一个已经建好的全文索引——`full_text_search` 的文档明确写了「This method is only valid on tables that have a full text search index」。没有索引就调用，会直接报错。

在 LanceDB 里，创建任何索引都遵循同一套 API：`table.create_index(&[列], Index::某变体).execute()`。FTS 对应的变体是 `Index::FTS(FtsIndexBuilder)`。它的特点是：

- **作用在字符串列**：FTS 索引建在文本/字符串列上（也支持字符串数组列，由底层 `supported_fts_data_type` 决定）。
- **参数可空**：`FtsIndexBuilder::default()` 即可用默认 BM25 参数建索引，绝大多数场景这样就够。
- **本质是倒排索引**：在底层 `IndexType` 里，FTS 映射为 `Inverted`（倒排）。

#### 4.2.2 核心流程

创建一个 FTS 索引的端到端流程：

```
table.create_index(&["doc"], Index::FTS(FtsIndexBuilder::default()))
   │
   ▼  IndexBuilder（默认 replace=true, train=true）
   │
.execute().await
   │
   ▼  BaseTable::create_index → NativeTable 实现
   │
   ▼  (1) validate_index_type: 校验 "doc" 列类型 ∈ supported_fts_data_type
   │
   ▼  (2) make_index_params: Index::FTS(opts) → 直接 Box::new(opts)
   │         （注意：不像向量索引那样转成 VectorIndexParams，
   │           FTS 的 InvertedIndexParams 直接透传给 Lance）
   │
   ▼  (3) Lance Dataset 创建倒排索引，记录 index_type = Inverted
   │
建索引完成
```

关键点：FTS 的参数**不经过 lancedb 的转换层**，`InvertedIndexParams` 被原样装箱交给底层 Lance，所以你在 `FtsIndexBuilder` 上设置的所有选项（分词器、`with_position` 等）都会一字不差地传下去。

#### 4.2.3 源码精读

先看 `Index` 枚举里的 FTS 变体定义：

[rust/lancedb/src/index.rs:57-58](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs#L57-L58) —— `Index::FTS(FtsIndexBuilder)`，注释写明 "Full text search index using bm25"，直接点明它基于 BM25。

再看官方示例里建索引的写法，最简练：

[rust/lancedb/examples/full_text_search.rs:77-83](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/full_text_search.rs#L77-L83) —— 对 `"doc"` 列创建 FTS 索引，用 `FtsIndexBuilder::default()`，链式 `.execute().await` 触发实际建索引。这正是「配置 → execute」Builder 模式的体现。

`create_index` 内部对 FTS 的处理：

[rust/lancedb/src/table/create_index.rs:195-198](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L195-L198) —— 先用 `validate_index_type(field, "FTS", supported_fts_data_type)` 校验目标列的数据类型是否被 FTS 支持，然后 `Ok(Box::new(fts_opts))` 把 `InvertedIndexParams` **原样返回**，不像 BTree/Bitmap 那样包成 `ScalarIndexParams::for_builtin(...)`。这就是上一节说的「FTS 参数直接透传」。

最后，FTS 在底层索引类型表里的身份：

[rust/lancedb/src/table/create_index.rs:354](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L354) —— `Index::FTS(_) => IndexType::Inverted`，说明对外统计/描述时，FTS 索引的类型是 `Inverted`（倒排）。相应地，`IndexType` 枚举里 FTS 这个变体本身也接受 `INVERTED`/`Inverted` 作为别名：

[rust/lancedb/src/index.rs:317-319](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs#L317-L319) —— `IndexType::FTS` 带 `#[serde(alias = "INVERTED", alias = "Inverted")]`，所以从 JSON 反序列化索引类型时，`FTS` 和 `Inverted` 都能识别成同一个变体。

#### 4.2.4 代码实践

> **实践目标**：亲手建一张含文本列的表并创建 FTS 索引。
>
> **操作步骤**（在仓库根目录，无需额外 feature）：
> 1. 直接运行官方示例，它就是「建表 → 建索引 → 查询」的完整链路：
>    ```bash
>    cargo run --example full_text_search
>    ```
>    该示例在 [Cargo.toml:161-163](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/Cargo.toml#L161-L163) 注册，**没有 required-features**，默认 feature 即可编译运行。
> 2. 观察它生成了 `data/sample-lancedb` 目录，里面是 Lance 数据集文件。
>
> **需要观察的现象**：程序先打印 `Searching for: <某个随机词>`，再打印若干 `RecordBatch`，每行是一个命中的 `doc` 文本片段。
>
> **预期结果**：建索引成功、查询返回结果。若你想看到「没有索引时报错」，可以注释掉 `create_index(&tbl).await?;` 那一行（[full_text_search.rs:28](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/full_text_search.rs#L28)）再跑，观察 FTS 查询会因缺索引而失败。
>
> **待本地验证**：`random_word` 词表是随机的，每次运行的查询词和结果不同；但「能跑通、有结果输出」这一结论是确定的。

#### 4.2.5 小练习与答案

**练习 1**：如果把 FTS 索引建在一个 `Int32` 列上会发生什么？

**参考答案**：会报错。`create_index` 会走到 [create_index.rs:196](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L196) 的 `validate_index_type(field, "FTS", supported_fts_data_type)`，`Int32` 不在 FTS 支持的类型集合里，校验失败返回 `Error`。

**练习 2**：为什么 FTS 的 `fts_opts` 是 `Box::new(fts_opts)` 直接返回，而 BTree 要包成 `ScalarIndexParams::for_builtin(BuiltinIndexType::BTree)`？

**参考答案**：因为 FTS 的参数类型 `InvertedIndexParams` 本身就已经是底层 Lance 能直接消费的索引参数对象（它实现了相应 trait）；而 BTree/Bitmap 是 lancedb 用 `BuiltinIndexType` 枚举统一切换的「内置标量索引」，需要经 `ScalarIndexParams::for_builtin` 转一道。这体现了 FTS 作为「相对独立、参数丰富」的索引子系统，被更直接地透传。

---

### 4.3 执行全文检索查询（query 模块）

#### 4.3.1 概念说明

建好 FTS 索引后，查询通过 `QueryBase::full_text_search(query)` 触发。它和 u3-l1 讲过的 `only_if`/`select`/`limit` 一样，是 `QueryBase` trait 上的链式方法——**只配置、不执行**，真正读盘发生在 `execute().await`。

`full_text_search` 接收一个 `FullTextSearchQuery`，里面装的就是用户输入的查询文本字符串。它的行为有几个要点：

- **结果按 BM25 `_score` 降序返回**（最相关的在最前）。
- **默认补 limit**：如果你没显式设 `limit`，会自动设为 `DEFAULT_TOP_K = 10`（和向量搜索一致）。
- **可与向量搜索叠加**：在同一个 `Query` 上既调用 `full_text_search` 又调用 `nearest_to`，就是 u3-l6 要讲的混合搜索（Hybrid Search）。本讲只关注「纯 FTS」。

#### 4.3.2 核心流程

一次纯 FTS 查询的内部流程：

```
table.query()                       // 返回 Query，QueryRequest 为默认空配置
   .full_text_search(FullTextSearchQuery::new("关键词".into()))
        │  (1) 若 limit 为 None，补默认 10
        │  (2) 把 query 存进 QueryRequest.full_text_search
        ▼
   .select(Select::Columns(["doc"]))  // 只投影需要的列，降低 I/O（见 u3-l1）
   .limit(10)
   .execute().await                  // ← 真正执行，本地表交给 Lance scanner
        │
        ▼  Lance 用倒排索引算 BM25，按 _score 降序，截断到 limit
        │
   SendableRecordBatchStream         // 一段段 RecordBatch 的流
        │  每行附带 _score(Float32)
        ▼
   try_collect / while let Some(batch) = ...
```

与向量搜索对比：

| 维度 | 向量搜索 `nearest_to` | 全文检索 `full_text_search` |
| --- | --- | --- |
| 输入 | 查询向量（数值） | 查询文本（字符串） |
| 打分列 | `_distance`（越小越相似，Float32） | `_score`（越大越相关，Float32） |
| 排序 | 升序 | 降序 |
| 依赖索引 | 向量索引（可选，无则 flat search） | **必须有 FTS 索引**，否则报错 |
| 默认 limit | `DEFAULT_TOP_K = 10` | `DEFAULT_TOP_K = 10` |

#### 4.3.3 源码精读

`full_text_search` 在 `QueryBase` trait 上的声明，文档把核心行为说得很清楚：

[rust/lancedb/src/query.rs:428-447](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L428-L447) —— 文档注释明确：「The results will be returned in order of BM25 scores」「This method is only valid on tables that have a full text search index」，并给出了一个最小用法示例。trait 方法签名是 `fn full_text_search(self, query: FullTextSearchQuery) -> Self`，返回 `Self` 以支持链式调用。

它的默认实现（通过 `HasQuery` + blanket impl，机制见 u3-l1）：

[rust/lancedb/src/query.rs:547-553](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L547-L553) —— 先检查 `limit` 是否为 `None`，是则补 `DEFAULT_TOP_K`；再把传入的 `query` 写入 `QueryRequest.full_text_search`。注意这里**没有任何 I/O**，纯字段赋值，印证「只配置不执行」。

默认 top-k 常量的定义：

[rust/lancedb/src/query.rs:36](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L36) —— `pub(crate) const DEFAULT_TOP_K: usize = 10;`，FTS 与向量搜索共用这个默认值。

`QueryRequest` 里存放 FTS 查询的字段：

[rust/lancedb/src/query.rs:733-734](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L733-L734) —— `pub full_text_search: Option<FullTextSearchQuery>`。它是 `Option`，意味着「不调用 `full_text_search` 就是普通扫描，调用了就是 FTS 查询」，这正是 u3-l1 提到的「一个 `QueryRequest` 描述多种查询」的设计。

最后看官方示例里完整的纯 FTS 查询写法：

[rust/lancedb/examples/full_text_search.rs:95-104](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/full_text_search.rs#L95-L104) —— `table.query().full_text_search(FullTextSearchQuery::new(words[0].to_owned())).select(Select::Columns(vec!["doc".to_owned()])).limit(10).execute().await?`，然后用 `while let Some(batch) = results.try_next().await?` 逐批消费流。这演示了：投影只取 `doc` 列、limit 控制返回行数、结果是流式 `RecordBatch`。

#### 4.3.4 代码实践

> **实践目标**：执行一次多词全文检索并观察打分排序。
>
> **操作步骤**：复制 [full_text_search.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/full_text_search.rs)，把 `search_index` 改成下面这样（**示例代码**，不是项目原有代码）：
>
> ```rust
> // 示例代码：手动构造确定性的文本，便于观察打分
> async fn search_index(table: &Table) -> Result<()> {
>     // 搜索两个词，观察 BM25 把同时含两词、且词频高的文档排前面
>     let query = FullTextSearchQuery::new("apple banana".to_owned());
>     let mut results = table
>         .query()
>         .full_text_search(query)
>         .select(lancedb::query::Select::Columns(vec!["doc".to_owned()]))
>         .with_row_id()   // 顺带取行 id，方便对照
>         .limit(10)
>         .execute()
>         .await?;
>     while let Some(batch) = results.try_next().await? {
>         println!("{:?}", batch);
>     }
>     Ok(())
> }
> ```
>
> 注意要相应把 `create_some_records` 里的文本换成你自己的确定性句子（比如让某些行重复出现 "apple banana"），这样打分才有可比性。
>
> **需要观察的现象**：返回的 `RecordBatch` 里，**同时**包含 `apple` 和 `banana` 的行应排在只含其一的行前面；含 `apple` 次数更多的行分数更高。
>
> **预期结果**：结果按相关性从高到低返回。如果你打印 `_score` 列（把 `Select::Columns` 换成 `Select::All`，或在 hybrid 之外查询默认会带 `_score`），会看到分数从大到小排列。
>
> **待本地验证**：默认 `select` 是否自动投影 `_score` 列取决于底层行为；若没看到 `_score`，改用 `Select::All` 或显式投影该列即可。确切行为建议本地跑一次确认。

#### 4.3.5 小练习与答案

**练习 1**：调用 `full_text_search` 后，为什么即使不写 `.limit(10)` 也不会返回全表？

**参考答案**：因为 [query.rs:548-550](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L548-L550) 的实现会检查 `limit` 是否为 `None`，是则补 `DEFAULT_TOP_K = 10`。这是 FTS 与向量搜索共享的默认 top-k 行为，避免「忘记 limit 就扫全表」。

**练习 2**：在没有 FTS 索引的表上调用 `full_text_search(...).execute()`，错误发生在配置阶段还是执行阶段？为什么？

**参考答案**：发生在**执行阶段**（`.execute().await` 时）。因为 `full_text_search` 的实现只做字段赋值，不触碰索引；真正去查倒排索引是在 `execute` 把 `QueryRequest` 交给底层 Lance scanner 时。这和 u3-l4 讲过的「非法 SQL 过滤要等 execute 才报错」是同一类「配置不校验、执行才触达底层」的模式。

---

## 5. 综合实践

把本讲的知识串起来，完成一个小任务：**亲手建一张文档表，建 FTS 索引，验证 BM25 排序，再体会它与标量过滤的区别。**

1. **建表**：仿照 [full_text_search.rs:33-69](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/full_text_search.rs#L33-L69) 的 `create_some_records`，构造一张含 `id: Int32` 和 `doc: Utf8` 两列的表，但把 `doc` 换成你设计的 5～10 行**确定性**文本，故意让某些行重复出现关键词（例如 3 行都含 "rust database"，1 行只含 "rust"，1 行含无关内容）。
2. **建索引**：对 `doc` 列调用 `Index::FTS(FtsIndexBuilder::default())` 建倒排索引（[full_text_search.rs:77-83](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/full_text_search.rs#L77-L83)）。
3. **FTS 查询**：用 `full_text_search(FullTextSearchQuery::new("rust database".into()))` 查询，`Select::All` 投影并带上 `_score`，观察排序。预期：同时含两词且词频高的行排在最前，无关行不返回。
4. **对比标量过滤**：用 `.only_if("contains(doc, 'rust')")`（见 u3-l4）做一次子串过滤扫描，对比它与 FTS 的差异——前者只返回「命中/不命中」且无相关性排序，后者有 `_score` 排序。
5. **思考题**：为什么查 `database`（在多行出现）比查 `rust`（也在多行出现）的排序影响可能不同？结合 IDF 公式解释。

> 这个综合实践覆盖了本讲全部要点：理解 FTS 索引的创建（index 模块）、理解 `full_text_search` 查询（query 模块）、理解 BM25 打分（原理）。完成后，你就具备了学习 u3-l6「混合搜索 + RRF 重排」的全部前置——因为混合搜索正是把 FTS 结果和向量搜索结果融合。

## 6. 本讲小结

- FTS 是建立在**分词后的词项**之上的相关性检索，结果带 `_score` 列（越大越相关），区别于标量过滤的「命中/不命中」和向量搜索的 `_distance`（越小越相似）。
- LanceDB 核心不自己实现 BM25，而是把底层 `lance_index` 的 `InvertedIndexParams` 重新导出为 `FtsIndexBuilder`、`FullTextSearchQuery` 直接透传——典型「薄封装」。
- 创建 FTS 索引：`table.create_index(&["doc"], Index::FTS(FtsIndexBuilder::default())).execute()`，底层 `IndexType` 为 `Inverted`；`full_text_search` 查询**必须**先有 FTS 索引，否则执行时报错。
- BM25 同时考虑词频 \(f(t,D)\)、文档长度归一化（\(b\)、\(\text{avgdl}\)）、词频饱和（\(k_1\)）和词稀有度 IDF，从而把「最相关」的文档排到最前。
- `full_text_search` 是 `QueryBase` 上的链式配置方法，只改 `QueryRequest.full_text_search` 字段；未设 limit 时自动补 `DEFAULT_TOP_K = 10`；真正执行在 `.execute().await`。
- FTS 可与 `nearest_to` 叠加成混合搜索，这是下一讲的主题。

## 7. 下一步学习建议

- **下一步学习 u3-l6「混合搜索与 RRF 重排」**：它会用到本讲的 FTS 结果，演示如何把 FTS 的 `_score` 排名和向量搜索的 `_distance` 排名用 RRF（倒数排名融合）合并。相关代码已在 [query.rs:1226-1276](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1226-L1276) 预告了 FTS 分支与 `SCORE_COL` 的归一化。
- **对比学习 u4-l2「标量索引」**：理解 FM 索引（子串匹配）与 FTS（词项匹配）的差异，体会「同样是字符串索引，为什么有两套」。
- **深入底层**：若想调 BM25 的 \(k_1\)、\(b\) 或换分词器，去看 `lance_index` 的 `InvertedIndexParams` 文档；lancedb 这层只是透传。
- **阅读测试**：[query.rs:1975-1994](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1975-L1994) 与 [query.rs:2209-2250](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L2209-L2250) 有 FTS 与向量/混合查询交互的测试断言，是理解「FTS 命中如何混入结果集」的好材料。
