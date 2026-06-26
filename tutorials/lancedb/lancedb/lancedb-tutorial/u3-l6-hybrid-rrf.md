# 混合搜索与 RRF 重排

## 1. 本讲目标

前两讲我们分别掌握了两种「找东西」的能力：

- u3-l2 的**向量搜索** `nearest_to`：按向量距离排序，结果带 `_distance` 列（越小越相似）。
- u3-l5 的**全文检索** `full_text_search`：按 BM25 相关性排序，结果带 `_score` 列（越大越相关）。

但这两种方法各有盲区：向量搜索擅长「语义相近」（「汽车」能匹配「轿车」），却可能漏掉字面完全命中的结果；全文检索擅长「字面命中」（关键词精确出现），却对同义、改写无能为力。**混合搜索（Hybrid Search）** 就是把这两路结果同时算出来，再融合成一个排序，取长补短。

本讲学完后，你应该能够：

1. 说清楚「混合搜索」在 LanceDB 里是怎样被触发和编排的（两路并发查询 → 归一化 → 重排）。
2. 掌握 **RRF（Reciprocal Rank Fusion，倒数排名融合）** 的公式直觉，理解它为什么能融合两个「分数尺度完全不同」的列表。
3. 看懂 `normalize_scores` 与 `NormalizeMethod`（`Score`/`Rank`）的作用，以及一个关键事实：**默认的 RRF 重排只看排名、不看分数本身**。
4. 用 `nearest_to + full_text_search` 写出一次混合搜索，并解释结果里 `_relevance_score` 列的含义。

本讲覆盖两个最小模块：**query (hybrid)**（混合搜索的编排）与 **rerankers (rrf)**（RRF 融合算法）。

## 2. 前置知识

- **向量搜索 `_distance`（u3-l2）**：向量搜索返回 `_distance` 列（Float32），默认 L2 距离，**越小越相似**，结果按升序。它和 FTS 的方向相反。
- **全文检索 `_score`（u3-l5）**：FTS 返回 `_score` 列（Float32），BM25 相关性，**越大越相关**，结果按降序。
- **`_rowid` 元列（u3-l1/u3-l2）**：Lance 给每一行分配的唯一行 id（`UInt64`）。混合搜索要用它来「对齐」两路结果里同一行——这正是融合的前提。
- **`QueryBase` 链式配置（u3-l1）**：`limit`/`select`/`full_text_search`/`with_row_id` 等都是只改 `QueryRequest`、不立即执行的方法；真正读盘在 `.execute().await`。
- **两路分数不可比**：L2 距离的数值范围可能是 `0.5 ~ 3.2`，BM25 分数可能是 `2.1 ~ 18.7`，两者量纲完全不同。直接相加毫无意义——这正是 RRF 这种「只看排名」的方法存在的理由。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `rust/lancedb/src/query.rs` | `VectorQuery::execute_hybrid` 编排两路查询与融合；`execute_with_options` 自动路由到 hybrid；`QueryBase::rerank`/`norm` 配置项；`QueryExecutionOptions`。 |
| `rust/lancedb/src/query/hybrid.rs` | 融合前的工具函数：`normalize_scores`（分数归一化到 [0,1]）、`rank`（分数转排名）、`query_schemas`（对齐两路 schema）。 |
| `rust/lancedb/src/rerankers.rs` | `Reranker` trait 与默认 `merge_results`（按 `_rowid` 去重合并）、`RELEVANCE_SCORE` 列名常量、`NormalizeMethod` 枚举。 |
| `rust/lancedb/src/rerankers/rrf.rs` | `RRFReranker` 实现 RRF 算法；含一份手算对照的单元测试。 |
| `rust/lancedb/examples/hybrid_search.rs` | 官方端到端示例：建表（带嵌入）→ 建 FTS 索引 → 混合搜索。 |

> 核心认知：混合搜索是 LanceDB **自己实现**的（不像 FTS/BM25 那样委托给底层 `lance_index`）。两路查询各自由底层算出分数，但「如何融合」这套逻辑（归一化 + RRF）完全在 `rust/lancedb` 这一层。

## 4. 核心概念与源码讲解

### 4.1 混合搜索的触发与整体编排

#### 4.1.1 概念说明

在 LanceDB 里，一次混合搜索就是「在同一个查询上同时挂上向量搜索和全文搜索」：

```rust
table.query()
    .full_text_search(FullTextSearchQuery::new("关键词".into()))  // 路径 A：FTS
    .nearest_to(query_vector)?                                      // 路径 B：向量
    .limit(5)
    .execute().await?
```

当你对同一个 `Query` 既调用了 `full_text_search` 又调用了 `nearest_to`，它就自动变成混合搜索。底层如何感知这一点？关键在 `execute_with_options` 的分流逻辑：

[rust/lancedb/src/query.rs:1334-1346](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1334-L1346) —— `VectorQuery::execute_with_options` 一上来就检查 `self.request.base.full_text_search.is_some()`：只要挂了 FTS 查询，就走 `execute_hybrid` 分支；否则走普通向量执行 `inner_execute_with_options`。这就是「挂上 FTS 就变混合搜索」的触发点。

#### 4.1.2 核心流程

`execute_hybrid` 的整体编排可以画成下面这张图：

```
           一个 VectorQuery（同时挂了 nearest_to 和 full_text_search）
                              │
        ┌─────────────────────┴─────────────────────┐
        │ execute_hybrid 拆成两份独立的内部查询       │
        ▼                                           ▼
  fts_query（只留 FTS）                      vector_query（只留向量）
  + with_row_id()                            + with_row_id()   ← 融合要对齐行 id
        │                                           │
        └────────── try_join! 并发执行 ──────────────┘
                              │
                  各自 try_collect 成 Vec<RecordBatch>
                              │
                  concat_batches 合并成单个 batch
                              │
              （可选）norm == Rank → rank() 把分数转成排名
                              │
              normalize_scores() 把 _distance / _score 归一化到 [0,1]
                              │
              reranker.rerank_hybrid(vec, fts)  ← 默认 RRFReranker
                              │
              check_reranker_result（必须带 _relevance_score 列）
                              │
              slice(0, limit)  +  按需 drop _rowid
                              │
                  最终结果流（带 _relevance_score 列）
```

这里有几个设计要点：

1. **拆成两份独立查询并发跑**：FTS 那份会把向量部分去掉，向量那份会把 FTS 去掉（见 4.1.3 源码），两路用 `try_join!` 并发，互不干扰。
2. **强制 `with_row_id`**：融合必须靠 `_rowid` 对齐两路结果里的同一行，所以两份内部查询都被加上 `with_row_id()`。
3. **归一化一定发生**：无论用哪个重排器，两路分数都会先被 `normalize_scores` 拉到 [0,1]。
4. **重排器可替换**：默认 `RRFReranker`，但你可以通过 `.rerank(Arc::new(自定义重排器))` 换成自己的实现。

#### 4.1.3 源码精读

先看拆分两份查询的那几行，这是理解「两路并发」的关键：

[rust/lancedb/src/query.rs:1226-1236](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1226-L1236) —— 第 1226 行新建一个 `fts_query`，把它的 `request` 设为当前查询的「基础部分」`self.request.base.clone()`（注意：**只取 base，不含 `query_vector`**，所以它退化成纯 FTS 查询）；第 1230 行 `vector_query = self.clone()`（保留向量），第 1232 行再显式把它的 `full_text_search` 置为 `None`（去掉 FTS）。两份查询各自补上 `.with_row_id()` 后，用 `try_join!` 并发执行。

两路结果各自是一个**流**，第 1238-1241 行再用 `try_join!` 把两个流分别 `try_collect` 成 `Vec<RecordBatch>`，把异步收齐的动作也并发掉。

合并与归一化在稍后几行：

[rust/lancedb/src/query.rs:1245-1257](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1245-L1257) —— 先用 `hybrid::query_schemas` 取得对齐用的 schema（处理某一路为空的边界情况，见 4.3.3），把两路各自的多个 batch `concat_batches` 成单个 batch；接着若用户设了 `norm == Rank`，就先调 `hybrid::rank` 把分数列转成排名；最后**无条件**对两路分别调 `normalize_scores`——向量那路的 `_distance`（`DIST_COL`）和 FTS 那路的 `_score`（`SCORE_COL`）都被归一化到 [0,1]。

> 这里的 `DIST_COL` / `SCORE_COL` 是从底层 `lance_index` 引入的列名常量（`_distance` / `_score`）：
> [rust/lancedb/src/query.rs:20-21](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L20-L21)。

最后看「拿到重排器 → 重排 → 截断」的收尾：

[rust/lancedb/src/query.rs:1259-1290](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1259-L1290) —— 第 1259-1264 行取出重排器，若用户没设（`reranker` 为 `None`）就用默认的 `RRFReranker::default()`；第 1266-1273 行从请求里把 FTS 查询文本取出来（若没有 FTS 查询就报 `Error::Runtime`，这其实是一道防御性校验）；第 1275-1277 行调用 `reranker.rerank_hybrid(...)` 得到融合后的单个 batch；第 1279 行 `check_reranker_result` 校验结果必须带 `_relevance_score` 列；最后第 1281-1284 行按 `limit` 截断，第 1286-1288 行在用户没要求 `_rowid` 时把它丢掉。

#### 4.1.4 代码实践（源码阅读型）

> **实践目标**：确认「挂上 FTS 就自动走 hybrid」这一触发机制。
>
> **操作步骤**：
> 1. 打开 [rust/lancedb/src/query.rs:1334-1346](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1334-L1346)。
> 2. 对比 [rust/lancedb/src/query.rs:1219-1232](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1219-L1232)，确认 `execute_hybrid` 一开始就把请求拆成「纯 FTS」和「纯向量」两份。
>
> **需要观察的现象**：触发 hybrid 的判据是 `full_text_search.is_some()`，而不是某个显式的 `hybrid()` 开关。也就是说「混合 vs 纯向量」是由「有没有挂 FTS」隐式决定的。
>
> **预期结果**：你会理解——官方示例里即使直接调用 `.execute()`（而非显式 `execute_hybrid`），只要查询同时挂了 `nearest_to` 和 `full_text_search`，也会自动走这条融合路径。

#### 4.1.5 小练习与答案

**练习 1**：如果只调用 `nearest_to` 而不调用 `full_text_search`，会走 `execute_hybrid` 吗？为什么？

**参考答案**：不会。因为 [query.rs:1338](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1338) 的判据是 `self.request.base.full_text_search.is_some()`，没挂 FTS 时该字段是 `None`，条件为假，走普通向量执行 `inner_execute_with_options`。混合搜索的「混合」正来自于「两路都存在」。

**练习 2**：为什么 `execute_hybrid` 要给两份内部查询都强制加 `.with_row_id()`？

**参考答案**：因为后续的融合（`merge_results` 去重、RRF 按 row id 累加分数）都依赖 `_rowid` 来识别「这是不是同一行」。没有 `_rowid`，就无法把向量结果里的某行和 FTS 结果里的某行关联起来，也就无法融合。

---

### 4.2 RRF 倒数排名融合（rerankers/rrf 模块）

#### 4.2.1 概念说明

两路结果归一化之后，怎么融合成一个排序？最朴素的想法是「分数相加」，但这有个致命问题：L2 距离和 BM25 分数的尺度完全不同（哪怕各自归一化到 [0,1]，二者「1 的含义」也不一样），加权求和需要精心调权重，很脆弱。

**RRF（Reciprocal Rank Fusion，倒数排名融合）** 给了一个极其简单又稳健的替代方案：**不看分数，只看排名（名次）**。直觉是——「如果一个文档在两份榜单里都排得很靠前，那它大概率真的很相关」。因为只用排名，它天然绕开了「两路分数尺度不可比」的问题，也不需要调权重。

RRF 是 Cormack 等人 2009 年提出的经典方法，论文链接直接写在了源码注释里。

#### 4.2.2 核心流程

给定多个排序列表 \(L_1, L_2, \dots\)，文档 \(d\) 的 RRF 分数为：

\[
\text{RRF}(d) = \sum_{L} \frac{1}{k + \text{rank}_L(d)}
\]

其中 \(\text{rank}_L(d)\) 是文档 \(d\) 在列表 \(L\) 中的**排名**（从 1 开始），\(k\) 是一个平滑常数（默认 60）。\(k\) 的作用是**抑制排名特别靠后的项**——排名越靠后，\(1/(k+\text{rank})\) 越接近 \(1/k\)，差距越小，避免「第 100 名」和「第 1 名」的差距被放得过大。论文的实验结论是 \(k=60\) 近乎最优，但取值并不敏感。

> **实现细节（务必注意）**：LanceDB 源码里用的是 **0 起的下标** `i`（`enumerate` 得到），所以每项贡献实际是
>
> \[
> \frac{1}{k + i} \quad (i = 0, 1, 2, \dots)
> \]
>
> 也就是说，对应「1 起排名 \(r\)」时贡献为 \(1/(k + r - 1)\)，比标准公式多减了 1。当 \(k=60\) 时这点偏差可以忽略，且**不影响最终的相对排序**。我们会在 4.2.3 用源码测试的注释精确印证这一点。

文档 \(d\) 只要出现在**任意一份**榜单里就能拿到分数；若它同时出现在多份榜单里，分数**累加**。最后按 RRF 分数**降序**排列，得到融合后的最终排序。

用源码测试里的一组手算例子（见 4.2.3）说明：

| 文档（row id） | 向量榜排名 | FTS 榜排名 | RRF 分数（\(k=1\)） |
| --- | --- | --- | --- |
| foo (1) | 第 1（i=0） | 未出现 | \(1/(1+0) = 1.0\) |
| bar (4) | 第 2（i=1） | 第 1（i=0） | \(1/(1+1) + 1/(1+0) = 1.5\) |
| baz (2) | 第 3（i=2） | 未出现 | \(1/(1+2) \approx 0.333\) |
| bean (5) | 第 4（i=3） | 第 2（i=1） | \(1/(1+3) + 1/(1+1) = 0.75\) |
| dog (3) | 第 5（i=4） | 第 3（i=2） | \(1/(1+4) + 1/(1+2) \approx 0.533\) |

最终降序：`bar(1.5) > foo(1.0) > bean(0.75) > dog(0.533) > baz(0.333)`。注意 **bar** 虽然在两份榜单里都不是第一，但因为「两榜都靠前」累加后反超了单项第一的 foo——这正是 RRF「两榜共识」的力量。

#### 4.2.3 源码精读

先看 `RRFReranker` 的构造，\(k\) 默认值就在这里：

[rust/lancedb/src/rerankers/rrf.rs:22-43](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/rerankers/rrf.rs#L22-L43) —— 结构体只持有一个 `k: f32` 字段；`new(k)` 可自定义；`Default` 实现给出 `k = 60.0`，注释引用了 RRF 原论文并说明 \(k=60\) 近乎最优、取值不敏感。

RRF 的核心算法在这段——遍历两份榜单的 row id，按位置累加倒数排名分数：

[rust/lancedb/src/rerankers/rrf.rs:82-102](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/rerankers/rrf.rs#L82-L102) —— 第 82-83 行把两路的 `ROW_ID` 列下转成 `UInt64Array`；第 85 行建一个 `BTreeMap<row_id, score>`；第 86-92 行定义闭包：对「第 `i` 个（0 起）结果」，算 `score = 1.0 / (i as f32 + self.k)`，如果该 row id 已在 map 里就**累加**（`and_modify(|e| *e += score)`），否则插入。第 93-102 行用这个闭包分别遍历 `vector_ids` 和 `fts_ids` 的每个值。**注意：这里只读了 `ROW_ID`，完全没读 `_distance`/`_score` 的数值——印证「RRF 只看排名、不看分数」。**

拿到累加好的 `rrf_score_map` 后，把它附加到合并后的结果上并排序：

[rust/lancedb/src/rerankers/rrf.rs:104-148](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/rerankers/rrf.rs#L104-L148) —— 第 104 行调用 `self.merge_results(...)`（默认实现见 4.4）把两路 batch 合并去重；第 106-114 行按合并后的 row id 顺序，从 map 里查出每个 row id 的 RRF 分数，组成 `relevance_scores` 数组；第 117-125 行用 `sort_to_indices` 按分数**降序**（`descending: true`）得到排序下标；第 128-135 行把所有列按这个下标 `take` 重排；第 138-146 行给 schema 追加一列 `_relevance_score`（`RELEVANCE_SCORE` 常量，Float32），重建 batch 返回。

最值得精读的是那份手算注释，它把 4.2.2 的公式和代码精确对应起来：

[rust/lancedb/src/rerankers/rrf.rs:182-188](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/rerankers/rrf.rs#L182-L188) —— 测试用 `RRFReranker::new(1.0)`（即 \(k=1\)），注释逐项列出每个文档的分数计算：`foo = 1/1`、`bar = 1/2 + 1/1`、`baz = 1/3`、`bean = 1/4 + 1/2`、`dog = 1/5 + 1/3`。注意这里的 `1/1`、`1/2` 写法——分母就是 `i + k`（\(k=1\) 时即 `i+1`），正好印证 4.2.2「实现用 0 起下标 \(i\)」的结论。紧随其后的断言 [rrf.rs:205-221](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/rerankers/rrf.rs#L205-L221) 验证了排序为 `["bar","foo","bean","dog","baz"]`、对应分数 `[1.5, 1.0, 0.75, 0.533..., 0.333...]`，与上表完全一致。

#### 4.2.4 代码实践

> **实践目标**：亲手验证 RRF 的「两榜共识」效应，体会它对「只在一路靠前」的文档的取舍。
>
> **操作步骤**：直接运行该模块的单元测试，它就是一份精确的手算用例（**无需任何 feature flag**）：
> ```bash
> cargo test --quiet -p lancedb --lib rerankers::rrf::test::test_rrf_reranker
> ```
> 然后读 [rust/lancedb/src/rerankers/rrf.rs:157-222](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/rerankers/rrf.rs#L157-L222)，对照测试里构造的 `vec_results`（5 行，row id `[1,4,2,5,3]`）和 `fts_results`（3 行，row id `[4,5,3]`），自己用 4.2.2 的公式手算一遍每个 row id 的分数。
>
> **需要观察的现象**：row id `4`（bar）在向量榜排第 2、FTS 榜排第 1，累加后分数最高（1.5），超过只在向量榜排第 1 的 row id `1`（foo，1.0）。FTS 榜根本没出现的 row id `2`（baz）分数最低。
>
> **预期结果**：测试通过；你手算的顺序与分数应与注释 [rrf.rs:182-188](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/rerankers/rrf.rs#L182-L188) 完全吻合。
>
> **待本地验证**：若本地未编译过核心库，`cargo test` 会先编译 `lancedb`，耗时较长，但结论确定。

#### 4.2.5 小练习与答案

**练习 1**：把 `RRFReranker` 的 \(k\) 从 60 调到 1，再调到 1000，分别会让排序向哪个方向偏？

**参考答案**：\(k\) 越小（如 1），排名靠前项的优势被放大（\(1/(1+0)\) vs \(1/(1+1)\) 差距大），结果更「尖」，更看重谁排第一；\(k\) 越大（如 1000），所有项的贡献都被压平到接近 \(1/1000\)，差距缩小，排序对「是否同时出现在两榜」更敏感、对「单榜内的具体名次」更不敏感。极端大 \(k\) 时排序趋近于「按出现榜单数计数」。

**练习 2**：RRF 融合时，如果一个文档在向量榜和 FTS 榜都排第 1，它的分数和「只在向量榜排第 1」的文档相比如何？这说明 RRF 偏好什么样的文档？

**参考答案**：前者分数是 \(2 \times 1/(k+0)\)（两榜各贡献一次），后者只有 \(1/(k+0)\)，前者翻倍。这说明 RRF 偏好**两路都认可（共识）的文档**，而不是「单路冠军」。这正是混合搜索取长补短的核心收益。

---

### 4.3 分数归一化 normalize_scores 与 NormalizeMethod

#### 4.3.1 概念说明

`execute_hybrid` 在交给重排器之前，一定会调一次 `normalize_scores`。要理解它的作用，得先抓住一个容易混淆的事实：

> **默认的 RRF 重排只看排名、不看分数数值。所以 `normalize_scores` 的输出，对默认 RRF 的排序结果没有直接影响。**

那为什么还要归一化？两个原因：

1. **为「分数型」重排器准备**：如果你想写一个自定义重排器，用「归一化后的向量分 + 归一化后的 FTS 分」加权求和来融合（而非 RRF 的排名融合），就需要先把两路分数拉到同一尺度 [0,1]。`normalize_scores` 就是为此提供的通用工具。
2. **让输出结果可解释**：融合后返回的 batch 里仍带着归一化后的 `_distance`/`_score` 列，归一化后它们的含义更统一，便于下游消费或调试。

此外，`NormalizeMethod` 提供两种「先把分数转成排名再归一化」的方式（`Rank`），适用于「分数本身没有可比意义、只有排名有意义」的场景——其实这正契合 RRF 的思想。

#### 4.3.2 核心流程

`normalize_scores` 做的是经典的 **min-max 归一化**，把一列 Float32 压缩到 [0,1]：

\[
\text{norm}(x) = \frac{x - \min}{\max - \min}
\]

并有两个边界处理：

- 若 \(\max - \min < 10^{-5}\)（接近相等，等价于 numpy 的 `isclose`），分母改用 \(\max\)，避免除以极小值放大浮点误差。
- 若 \(\max - \min = 0\)（所有值相同），分数原样保留（不除零）。

还有一个 `invert`（反转）选项：归一化后再做 \(1 - \text{norm}(x)\)。它用来「翻转方向」——比如 `_distance` 越小越好，反转后变成「越大越好」，便于和「越大越好」的 `_score` 用同一套逻辑比较。不过 `execute_hybrid` 调用时 `invert` 传的是 `None`（即不反转），因为默认走的是排名融合，方向翻转与否不影响 RRF。

`NormalizeMethod` 两种模式的区别：

| 模式 | 含义 | 何时用 |
| --- | --- | --- |
| `Score`（默认） | 直接对原始分数做 min-max 归一化 | 分数本身有意义、分布可比时 |
| `Rank` | 先把分数转成排名（1,2,3,…），再对排名归一化 | 分数尺度不可比、只有排名有意义时（更接近 RRF 思想） |

#### 4.3.3 源码精读

`normalize_scores` 的实现，min-max 公式与边界处理一目了然：

[rust/lancedb/src/query/hybrid.rs:123-174](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query/hybrid.rs#L123-L174) —— 第 146-147 行算 `max`/`min`；第 150 行用 `if max - min < 10e-5` 做 `isclose` 判断（注释明确说这等价于 `np.isclose`，为与 Python 端行为一致）；第 153-159 行做 `(score - min)/(max - min)`（用 arrow 的 `sub`/`div` kernel 向量化计算）；第 161-164 行按 `invert` 决定是否 `1 - score`。注意第 141-143 行：空 batch 直接原样返回。

`rank` 函数——把分数列转成排名，用的是 arrow 自带的 `rank` kernel：

[rust/lancedb/src/query/hybrid.rs:19-58](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query/hybrid.rs#L19-L58) —— 第 39-48 行调用 `arrow::compute::kernels::rank::rank`，传入 `SortOptions { descending: !ascending.unwrap_or(true), .. }`。这里有个细节：`ascending` 默认 `true`（分数升序排第 1），传进 arrow 的 `descending` 取反，所以「ascending=true → descending=false」。转换后把原分数列**原地替换**为排名值（Float32），其余列不动。它的测试 [hybrid.rs:182-265](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query/hybrid.rs#L182-L265) 用 `[0.2,0.4,0.1,0.6,0.45]` 验证：`ascending=false` 时得到排名 `[4,3,5,1,2]`（最大值 0.6 排第 1），可直接对照。

`query_schemas`——对齐两路 schema 的边界处理，处理「某一路结果为空」的情况：

[rust/lancedb/src/query/hybrid.rs:65-86](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query/hybrid.rs#L65-L86) —— 当 FTS 结果为空（`None`）但向量结果存在时，第 74-77 行拿向量的 schema，把它的 `_distance`（`DIST_COL`）字段名替换成 `_score`（`SCORE_COL`）作为 FTS 的 schema（用 `with_field_name_replaced`，[hybrid.rs:102-118](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query/hybrid.rs#L102-L118)），保证后续 `concat_batches` 时列名对齐；两路都空时第 82 行给出最小占位 schema（[hybrid.rs:88-100](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query/hybrid.rs#L88-L100)）。

`NormalizeMethod` 枚举与字符串解析（可从 Python/配置传 `"score"`/`"rank"`）：

[rust/lancedb/src/rerankers.rs:21-48](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/rerankers.rs#L21-L48) —— `NormalizeMethod { Score, Rank }`，`FromStr` 把小写的 `"score"`/`"rank"` 解析成对应变体，非法值返回 `Error::InvalidInput`。`execute_hybrid` 在 [query.rs:1251-1254](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1251-L1254) 据此决定是否先调 `rank`。

#### 4.3.4 代码实践（源码阅读型）

> **实践目标**：理解 `normalize_scores` 与 `Rank` 模式的数值行为。
>
> **操作步骤**：
> 1. 运行该模块自带测试（无需 feature）：
>    ```bash
>    cargo test --quiet -p lancedb --lib query::hybrid::test::test_normalize_scores
>    cargo test --quiet -p lancedb --lib query::hybrid::test::test_rank
>    ```
> 2. 读 [hybrid.rs:267-348](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query/hybrid.rs#L267-L348)：测试用 `[-4, 2, 0, 3, 6]` 验证，min=-4、max=6、range=10，归一化得 `[0.0, 0.6, 0.4, 0.7, 1.0]`；`invert=true` 得 `[1.0, 0.4, 0.6, 0.3, 0.0]`；全相同值 `[2.1,...]` 归一化全为 0。
>
> **需要观察的现象**：归一化结果严格落在 [0,1]；最大值恒为 1（除非全相同）、最小值恒为 0；`invert` 会把「方向」翻转。
>
> **预期结果**：测试通过，你对 min-max + invert 的行为有了具象认识。
>
> **待本地验证**：浮点比较用的是精确相等（测试里值都是可精确表示的），本地跑应稳定通过。

#### 4.3.5 小练习与答案

**练习 1**：既然默认 RRF 只看排名，那 `normalize_scores`（`Score` 模式）跑出来的归一化分数，对默认混合搜索的最终排序有影响吗？

**参考答案**：**没有直接影响**。因为默认 `RRFReranker` 只读 `ROW_ID` 列、按位置算 \(1/(k+i)\)，从不读 `_distance`/`_score` 的数值（见 4.2.3）。`normalize_scores` 的产物只在改用「分数型」自定义重排器时才真正起作用，或用于让输出列更可解释。这是本讲最容易被误解的一点。

**练习 2**：什么场景下你会想把 `norm` 设成 `NormalizeMethod::Rank` 而非 `Score`？

**参考答案**：当两路原始分数的尺度/分布差异极大、直接 min-max 归一化仍不可比时（比如向量距离集中在 0.3~0.5 而 BM25 分数从 2 跨到 18）。`Rank` 先转成「1,2,3,…」的排名再做归一化，抹掉了绝对量纲，只保留相对次序——这更稳健，也更接近 RRF 的精神。

---

### 4.4 Reranker trait 与结果合并

#### 4.4.1 概念说明

RRF 只是「融合策略」的一种。LanceDB 把「如何融合两路结果」抽象成了一个 trait：`Reranker`。你可以实现自己的 `Reranker`（比如用归一化分数加权、或接一个交叉编码器模型重排），通过 `.rerank(Arc::new(你的重排器))` 挂上去。

`Reranker` trait 只要求实现一个方法 `rerank_hybrid`，但它还提供了一个**默认方法** `merge_results`：把两路 batch 拼起来、按 `_rowid` 去重（保留首次出现）。`RRFReranker` 就复用了这个默认 `merge_results`。

#### 4.4.2 核心流程

`merge_results`（默认实现）做两件事：

```
vector_results（带 _rowid）  +  fts_results（带 _rowid）
              │
   concat_batches 拼成一个 batch（行数 = 两路之和）
              │
   逐行看 _rowid，用 BTreeSet 记录已见过的 id
              │
   mask = 「这一行的 id 之前没出现过」
              │
   filter_record_batch 只保留 mask 为 true 的行（去重，保留首次出现）
              │
   合并去重后的 batch（交给 rerank_hybrid 进一步打分排序）
```

要点：去重是**按 `_rowid`**，且**保留首次出现的位置**——所以同一文档如果同时出现在两路，会保留它在「向量结果」里的那一份（因为 `concat` 时向量在前）。

另外，trait 还约定了一个硬性契约：任何 `rerank_hybrid` 的返回结果**必须**带一列名为 `_relevance_score`（`RELEVANCE_SCORE` 常量）。`execute_hybrid` 在拿到结果后会用 `check_reranker_result` 校验这一点，缺失就报 `Error::Schema`。

#### 4.4.3 源码精读

`Reranker` trait 的定义与默认 `merge_results`：

[rust/lancedb/src/rerankers.rs:50-97](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/rerankers.rs#L50-L97) —— 第 54 行 trait 边界 `Debug + Sync + Send`（要能跨异步线程传递）；第 60-65 行声明 `rerank_hybrid(&self, query, vector_results, fts_results) -> Result<RecordBatch>` 是唯一必须实现的方法；第 67-96 行是默认 `merge_results`：第 72 行 `concat_batches` 拼接，第 74-75 行建一个 mask builder 和 `BTreeSet`，第 89-91 行逐个 row id 判断 `unique_ids.insert(id)`（insert 返回是否首次插入）并 append 到 mask，最后第 93 行 `filter_record_batch` 过滤。

`RELEVANCE_SCORE` 常量与结果校验：

[rust/lancedb/src/rerankers.rs:18-19](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/rerankers.rs#L18-L19) —— `RELEVANCE_SCORE = "_relevance_score"`，这是混合搜索结果里的相关性分数列名。

[rust/lancedb/src/rerankers.rs:99-110](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/rerankers.rs#L99-L110) —— `check_reranker_result` 校验返回的 schema 里有 `_relevance_score` 列，没有就返回 `Error::Schema`。这保证无论用哪个重排器，输出契约统一。

如何在查询里挂上自定义重排器和设置归一化模式（`QueryBase` 的两个链式方法）：

[rust/lancedb/src/query.rs:506-514](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L506-L514) —— `rerank(self, reranker: Arc<dyn Reranker>)` 和 `norm(self, norm: NormalizeMethod)`，文档注明「currently only supported for Hybrid Search」。它们的实现 [query.rs:575-583](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L575-L583) 只是把值写进 `QueryRequest.reranker` / `QueryRequest.norm`（[query.rs:755-758](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L755-L758)）——仍是「只配置不执行」。

#### 4.4.4 代码实践（源码阅读型）

> **实践目标**：理解「自定义重排器」的扩展点长什么样。
>
> **操作步骤**：
> 1. 打开 [rust/lancedb/src/rerankers.rs:50-65](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/rerankers.rs#L50-L65)，看清 `Reranker` trait 只需实现一个 `rerank_hybrid`。
> 2. 参考 `RRFReranker` 的实现 [rrf.rs:45-150](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/rerankers/rrf.rs#L45-L150)，思考：若你想写一个「按归一化分数加权求和」的重排器，会从 `vector_results`/`fts_results` 的哪两列取值？（答：归一化后的 `_distance`/`_score` 列。）
>
> **需要观察的现象**：任何重排器都复用默认 `merge_results` 做去重，然后自由决定打分逻辑；唯一硬性要求是返回列里要有 `_relevance_score`。
>
> **预期结果**：你会确认「换融合策略」只需实现一个 trait 方法并 `.rerank(...)` 挂上，`execute_hybrid` 的编排逻辑（两路并发、归一化、截断）完全复用。
>
> **待本地验证**：本实践为阅读型，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：`merge_results` 去重时保留「首次出现」，而 `concat_batches` 先放 vector 后放 fts。如果一个文档同时在两路出现，合并后保留的是哪一份？为什么这个选择对 RRF 无影响？

**参考答案**：保留向量结果里的那一份（因为它在 concat 时排在前面、首次出现）。对 RRF 无影响，因为 RRF 的最终分数来自 `rrf_score_map`（按 row id 累加的两榜排名分），与合并后保留哪一份的「列内容」无关——合并 batch 只是用来确定「最终有哪些唯一 row id」以及承载用户要看的业务列。

**练习 2**：如果你实现的自定义 `Reranker` 忘了在返回 batch 里加 `_relevance_score` 列，会发生什么？在哪一行报错？

**参考答案**：会在 `execute_hybrid` 调完 `rerank_hybrid` 后的 [rerankers.rs:99-110](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/rerankers.rs#L99-L110)（由 [query.rs:1279](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1279) 调用）报 `Error::Schema`，提示「rerank_hybrid must return a RecordBatch with a column named _relevance_score」。这是 trait 的输出契约校验。

---

## 5. 综合实践

把本讲知识串起来：**对同一查询分别跑纯向量搜索、纯全文检索，再用混合搜索（RRF）融合，对比三份结果的排序差异。**

推荐直接基于官方端到端示例 [rust/lancedb/examples/hybrid_search.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/hybrid_search.rs)，它已经把「建表（带 sentence-transformers 嵌入）→ 建 FTS 索引 → 混合搜索」串好了：

1. **跑通官方示例**（需 `sentence-transformers` feature）：
   ```bash
   cargo run --features sentence-transformers --example hybrid_search
   ```
   该示例在 [Cargo.toml:168-169](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/Cargo.toml#L168-L169) 注册，**必须带 `sentence-transformers` feature**（因为它要用嵌入模型把文本自动转向量）。

2. **读懂混合查询的构造**：[hybrid_search.rs:48-54](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/hybrid_search.rs#L48-L54) 是核心——`.full_text_search(FullTextSearchQuery::new("world records".into())).nearest_to(query_vector)?.limit(5).execute_hybrid(QueryExecutionOptions::default())`。注意它先算查询向量（第 47 行 `embedding.compute_query_embeddings`），再同时挂上 FTS 和向量。`QueryExecutionOptions::default()` 的含义见 [query.rs:612-619](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L612-L619)（`max_batch_length=1024`、`timeout=None`）。

3. **三路对比**（**示例代码**，非项目原有）：在示例基础上，对同一个 `query_str = "world records"` 分别跑：
   ```rust
   // 示例代码：三路对比
   // (a) 纯向量
   let vec_only = table.query().nearest_to(query_vector.clone())?.limit(5).execute().await?;
   // (b) 纯 FTS
   let fts_only = table.query()
       .full_text_search(FullTextSearchQuery::new(query_str.to_owned()))
       .limit(5).execute().await?;
   // (c) 混合（RRF）——即官方示例那条
   let hybrid = table.query()
       .full_text_search(FullTextSearchQuery::new(query_str.to_owned()))
       .nearest_to(query_vector.clone())?
       .limit(5).execute_hybrid(QueryExecutionOptions::default()).await?;
   ```
   分别收集前 5 条 `facts` 文本，对比三份排序。

4. **需要观察的现象**：
   - 纯向量那路会返回「语义相近但字面不含 "world records"」的句子（比如讲登山、地理的）。
   - 纯 FTS 那路只返回字面含 `world` 或 `records` 的句子。
   - 混合那路应综合两者：既字面命中、又语义相近的句子排最前（两榜共识，RRF 分最高）。
   - 混合结果的 batch 会多出 `_relevance_score` 列（若 `Select::All` 或不投影）。

5. **思考题**：找一个「只字面命中但语义无关」的句子，和一个「语义相近但字面没命中」的句子，观察它们在纯向量、纯 FTS、混合三路里分别排第几。结合 4.2 的 RRF 公式解释为什么混合结果更均衡。

> **待本地验证**：`sentence-transformers` 首次运行需下载模型权重（依赖网络与较大磁盘/内存），可能较慢。若环境受限，可跳过运行，仅做源码阅读型对比——重点理解 `execute_hybrid` 内部三步（两路并发 → 归一化 → RRF）即可。`execute_hybrid` 的编排逻辑不依赖嵌入模型，仅示例的「自动生成向量」那一步需要。

## 6. 本讲小结

- **混合搜索 = 向量搜索 + 全文检索 + 融合**。在同一个 `Query` 上同时挂 `nearest_to` 和 `full_text_search` 即可触发；`execute_with_options` 检测到 `full_text_search.is_some()` 就自动路由到 `execute_hybrid`。
- **`execute_hybrid` 的编排**：把请求拆成「纯 FTS」和「纯向量」两份（各自 `with_row_id`），`try_join!` 并发执行并收齐 → `concat_batches` 合并 →（可选）`rank` 转排名 → `normalize_scores` 归一化 → `reranker.rerank_hybrid` 融合 → 校验 `_relevance_score` → 按 `limit` 截断。
- **RRF 只看排名、不看分数**：公式 \(\text{RRF}(d)=\sum_L 1/(k+\text{rank}_L(d))\)（实现用 0 起下标 \(i\)，即 \(1/(k+i)\)，默认 \(k=60\)）。它天然绕开了「L2 距离与 BM25 分数尺度不可比」的难题，偏好「两榜都靠前」的共识文档。
- **`normalize_scores` 对默认 RRF 无直接影响**：min-max 归一化把 `_distance`/`_score` 压到 [0,1]，是为「分数型」自定义重排器和可解释输出准备的；`NormalizeMethod::Rank` 先转排名再归一化，更贴近 RRF 思想。
- **`Reranker` 是可扩展点**：trait 只要求实现 `rerank_hybrid` 并保证返回 `_relevance_score` 列，默认 `merge_results` 按 `_rowid` 去重；通过 `.rerank(...)` / `.norm(...)` 配置。
- 至此，「查询与搜索」单元完结：从基础扫描（u3-l1）→ 向量搜索（u3-l2/l3）→ 过滤（u3-l4）→ 全文检索（u3-l5）→ 混合搜索（本讲），构成一条完整的检索链路。

## 7. 下一步学习建议

- **进入 u4「索引体系」**：混合搜索的两路都依赖索引提速——向量搜索靠 IVF/HNSW（u4-l3/l4），FTS 靠倒排索引（u3-l5）。理解索引如何加速这两路，才能调好混合搜索的延迟与召回。
- **深读混合搜索测试**：[query.rs:2199-2310](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L2199-L2310) 一带有多组 FTS/向量/混合交互的测试断言，是理解「两路结果如何汇合」的好材料（结合 RRF 手算）。
- **实践自定义重排器**：参考 `RRFReranker` 实现一个「归一化分数加权」重排器（用 `normalize_scores` 产出的列），挂到 `.rerank(...)` 上，对比它与 RRF 在你数据上的排序差异。
- **跨语言对照**：本讲的 `execute_hybrid`/RRF 是 Rust 核心实现；Python/Node 绑定会把它原样暴露（如 Python 的 `query.execute_hybrid()`）。学完 u7 多语言绑定后可回来对照各语言 API。
