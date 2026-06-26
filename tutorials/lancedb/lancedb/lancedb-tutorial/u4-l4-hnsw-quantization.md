# HNSW 与量化技术

## 1. 本讲目标

本讲承接 u4-l3（IVF 家族），继续深入向量索引（`index (vector)`）模块。学完本讲你应该能够：

- 理解 HNSW（Hierarchical Navigable Small World）图索引的工作原理，以及它为什么与 IVF「搭档」而不是「替代」。
- 区分 LanceDB 中三个图索引变体 `IvfHnswFlat` / `IvfHnswSq` / `IvfHnswPq`，知道各自存什么、精度与内存如何权衡。
- 掌握三种量化技术 PQ / SQ / RQ 的基本原理、压缩比与近似搜索的关系。
- 看懂 LanceDB 如何用一个「配方 Builder」加一个翻译函数 `make_index_params`，把用户配置翻译成底层 Lance 的 `IndexParams`。
- 在精度与速度之间做出合理取舍，并能动手对比图索引带来的召回率差异。

## 2. 前置知识

阅读本讲前，建议你已经掌握 u4-l1（索引总览与 `Index` 枚举）和 u4-l3（IVF 家族）。本讲会直接使用以下概念，这里先用一句话回顾：

- **向量索引（ANN）**：用近似最近邻算法以「少量精度损失」换取「巨大的速度提升」，区别于无索引的 flat search（暴力扫描）。
- **IVF（倒排文件）**：用 k-means 把向量空间划成 `num_partitions` 个分区，查询时只搜索离 query 最近的 `nprobes` 个分区。它只负责「分桶」，不负责桶内如何搜索。
- **量化（quantization）**：把高精度浮点向量压缩成更短的编码，以减少内存与磁盘占用，代价是距离计算变近似。
- **召回率（recall）**：近似搜索返回的 top-k 中，有多少个落在「暴力搜索的真正 top-k」里，是一个 0~1 的比值，用来衡量精度损失。
- **基座 Builder 模式**：LanceDB 中凡涉及 IO 的操作都先返回一个只配置、不执行的 Builder，调用 `.execute().await` 才真正生效（见 u4-l1）。

> 关键回顾：u4-l3 讲过 IVF 的「四兄弟」IvfFlat / IvfSq / IvfPq / IvfRq，它们的差别只在**桶内存什么**。本讲要回答的下一个问题是：**桶内的向量该怎么搜？** 答案就是 HNSW。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [rust/lancedb/src/index/vector.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs) | 所有向量索引的「配方 Builder」：`IvfFlatIndexBuilder`、`IvfPqIndexBuilder`、`IvfSqIndexBuilder`、`IvfRqIndexBuilder`，以及本讲主角三个 HNSW 变体 Builder。还定义了用宏批量生成的参数 setter。 |
| [rust/lancedb/src/index.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs) | 用户面对的 `Index` 枚举（含 `IvfHnswPq` / `IvfHnswSq` / `IvfHnswFlat` / `IvfRq` 等变体）与描述已存在索引的 `IndexType` 枚举。 |
| [rust/lancedb/src/table/create_index.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs) | 翻译核心 `make_index_params`：把 LanceDB 的 `Index` 配方翻译成底层 Lance 的 `IndexParams`（包含 `HnswBuildParams`、`PQBuildParams`、`SQBuildParams`、`RQBuildParams`）。 |
| [rust/lancedb/src/query.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs) | 查询侧调参：`nprobes`（IVF 用）、`ef`（HNSW 用）、`refine_factor`、`bypass_vector_index()`（暴力搜索做 ground truth）。 |

> 一句话定位：`index/vector.rs` 和 `index.rs` 是「用户配方」，`create_index.rs` 是「配方翻译器」，真正的 k-means、图构建、量化算法都不在 LanceDB 核心，而在它依赖的 `lance-index` / `lance-linalg` 里。LanceDB 核心只做**参数搬运与类型校验**。

## 4. 核心概念与源码讲解

本讲在「index (vector)」这一个最小模块下，分成三个递进的子模块来学：先看 HNSW 图索引变体，再看 PQ/SQ/RQ 量化原理，最后把 Builder 到底层参数的翻译串起来。

### 4.1 HNSW 图索引变体：IvfHnswFlat / IvfHnswSq / IvfHnswPq

#### 4.1.1 概念说明

u4-l3 的 IVF 索引有一个隐藏假设：**选定 `nprobes` 个分区后，要把这几个分区里的向量逐一扫描（桶内 flat search）**。当每个分区很大时，桶内扫描本身就成为瓶颈。HNSW（分层可导航小世界图）就是为了解决「桶内怎么快速搜」。

HNSW 的核心思想是给向量建一张**多层导航图**：

- 每个向量是图中的一个节点，和它最近的若干邻居连边。
- 图分成多层，上层稀疏（节点少、跨度大），下层稠密（节点全、精细）。
- 搜索时从顶层任意节点出发，逐层「贪心」向 query 靠拢，最后在底层精细图里收集候选。

这样，桶内搜索的复杂度从「扫描整个分区」降到「沿图走几十跳」，是**桶内子线性搜索**。LanceDB 的做法是把 HNSW 嵌进 IVF：**先用 IVF 选分区，再在每个分区内建一张 HNSW 图**，所以这些索引叫 `IVF_HNSW_*`。

由于 HNSW 图本身不规定「向量怎么存」，桶内向量既可以存原始值（Flat），也可以存量化后的压缩值（SQ/PQ）。于是 LanceDB 提供三个变体：

| 变体 | 桶内存什么 | 精度 | 内存/磁盘 |
| --- | --- | --- | --- |
| `IvfHnswFlat` | 原始向量 | 最高 | 最大 |
| `IvfHnswSq` | 标量量化（约 4×） | 中高 | 中 |
| `IvfHnswPq` | 乘积量化（约 32×~64×） | 中 | 最小 |

这正是 [index.rs 中 `Index::IvfHnswFlat` 变体文档](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs#L80-L82) 所说的「Stores raw vectors, providing the highest recall at the cost of more memory and disk space」。

#### 4.1.2 核心流程

构建一个 IVF-HNSW 索引分两步走（注意：这两步都在底层 `lance-index` 里完成，LanceDB 只配置参数）：

1. **IVF 分区（与 u4-l3 相同）**：用 k-means 把全部向量聚成 `num_partitions` 个簇，每簇一个质心。
2. **每个分区内建 HNSW 图**：对该分区里的向量建多层导航图，关键参数有两个：
   - `m`（`num_edges`，默认 20）：每个节点连几条边。越大图越密、精度越高、内存越大、建得越慢。
   - `ef_construction`（默认 300）：建图时每插入一个节点评估多少候选。越大建图越准但越慢。

查询时的流程是：

```text
query 向量
   │
   ├─ IVF 阶段：计算 query 到各质心距离 → 选 nprobes 个最近分区
   │
   └─ 桶内阶段：在选中的每个分区里，沿 HNSW 图贪心搜索
              ef（默认 1.5×limit）控制候选规模 → 返回桶内 top-k
   │
   └─ 合并各分区结果，按距离排序输出
```

> 直觉：IVF 负责「跳过 99% 的数据」，HNSW 负责「在剩下的 1% 里也别一个个扫」。`ef` 是查询时调召回的主力旋钮：调大 → 看更多候选 → 召回升高、延迟升高。

#### 4.1.3 源码精读

三个 HNSW 变体的 Builder 结构高度相似，都复用三段宏：`impl_ivf_params_setter!`（IVF 参数）、`impl_hnsw_params_setter!`（HNSW 参数）、可选 `impl_pq_params_setter!`（仅 Pq 变体）。HNSW 参数由这段宏定义：

[rust/lancedb/src/index/vector.rs:146-167](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L146-L167) —— 定义了 `num_edges(m)`（默认 20）与 `ef_construction`（默认 300）两个 setter，注释说明了「边数/精度/速度」的权衡。

以最简单的 `IvfHnswFlatIndexBuilder` 为例，它的字段就是 IVF 参数 + HNSW 参数，没有量化参数（因为存原始向量）：

```rust
// rust/lancedb/src/index/vector.rs:479-514（节选）
pub struct IvfHnswFlatIndexBuilder {
    pub(crate) distance_type: DistanceType,        // 默认 L2
    pub(crate) num_partitions: Option<u32>,        // None → 默认取行数的平方根
    pub(crate) sample_rate: u32,                    // 默认 256
    pub(crate) max_iterations: u32,                 // 默认 50
    pub(crate) target_partition_size: Option<u32>,
    pub(crate) m: u32,                              // 默认 20
    pub(crate) ef_construction: u32,                // 默认 300
}
```

[rust/lancedb/src/index/vector.rs:510-514](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L510-L514) 是它的 `impl` 块，三行宏展开后就能链式调用 `distance_type` / `num_partitions` / `num_edges` / `ef_construction` 等。

`IvfHnswPqIndexBuilder` 与 `IvfHnswSqIndexBuilder` 只是在此基础上分别多挂了 PQ 或 SQ 的字段（见 [4.2.3](#423-源码精读)）。

**查询侧**的 HNSW 旋钮 `ef` 在 query 模块里：

[rust/lancedb/src/query.rs:1137-1147](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1137-L1147) —— `VectorQuery::ef(ef)`，文档明确写着「only used when the vector column has an HNSW index」「Increasing this value will increase the recall … default value is 1.5*limit」。注意它和 IVF 的 `nprobes`（[query.rs:1037-1066](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1037-L1066)，默认 20）是**两个独立旋钮**，分别管「桶间选几个分区」和「桶内看多少候选」。

#### 4.1.4 代码实践

实践目标：在同一批随机向量上分别建 `IvfFlat` 与 `IvfHnswFlat` 索引，对比它们的 top-k 召回率，体会图索引的作用。

下面的示例代码改编自仓库里真实的测试 `test_create_index_ivf_hnsw_flat`（见 [create_index.rs:651-693](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L651-L693)），数据规模与构造方式与之完全一致（512 行 × 16 维）。**这是示例代码**，你需要自行创建为一个可运行的测试或 example：

```rust
// 示例代码：对比 IvfFlat 与 IvfHnswFlat 的召回率
use std::iter::repeat_with;
use lancedb::{connect, index::{Index, vector::{IvfFlatIndexBuilder, IvfHnswFlatIndexBuilder}}};
use arrow_array::{FixedsizeListArray, Float32Array, RecordBatch};
use arrow_schema::{DataType, Field, Schema};
use std::sync::Arc;
use futures::TryStreamExt;

# async fn run() -> lancedb::Result<()> {
let tmp = tempfile::tempdir().unwrap();
let conn = connect(tmp.path().to_str().unwrap()).execute().await?;

// 1) 构造 512 行 × 16 维的随机向量（与真实测试一致）
let dim = 16usize;
let n = 512usize;
let schema = Arc::new(Schema::new(vec![Field::new(
    "vector",
    DataType::FixedSizeList(Arc::new(Field::new("item", DataType::Float32, true)), dim as i32),
    false,
)]));
let vals = Float32Array::from(repeat_with(rand::random::<f32>).take(n * dim).collect::<Vec<_>>());
// （用 ArrayDataBuilder 把 vals 包成 FixedsizeListArray，参考测试里的 create_fixed_size_list 辅助函数）
let batch = RecordBatch::try_new(schema, vec![/* vectors */]).unwrap();

// 2) 同一份数据写两张表，分别建不同索引
let t_flat = conn.create_table("t_flat", batch.clone()).execute().await?;
let t_hnsw = conn.create_table("t_hnsw", batch.clone()).execute().await?;
t_flat.create_index(&["vector"], Index::IvfFlat(IvfFlatIndexBuilder::default()))
      .execute().await?;
t_hnsw.create_index(&["vector"], Index::IvfHnswFlat(IvfHnswFlatIndexBuilder::default()))
      .execute().await?;

// 3) 用 bypass_vector_index() 在 t_flat 上做暴力搜索，作为 ground truth
let query: &[f32; 16] = &[0.0; 16];
let truth: Vec<i64> = t_flat.query().nearest_to(query)?.bypass_vector_index()
    .limit(10).execute().await?
    .try_collect::<Vec<_>>().await?  // 取出 _rowid（具体取列方式见说明）
    .into_iter().flat_map(|b| /* 读 _rowid 列 */ vec![]).collect();
# Ok(())
# }
```

操作步骤与现象观察：

1. 复制 [create_index.rs:651-693](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L651-L693) 中的 `create_fixed_size_list` 辅助函数和数据构造方式，补全上面省略的数组构造。
2. 对 `t_flat` 和 `t_hnsw` 分别执行带索引的 `nearest_to(query).limit(10)`，各取回 10 条结果。
3. 以 `bypass_vector_index()`（[query.rs:1214-1216](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1214-L1216)）的结果为「真正的 top-10」，计算两个索引各自的召回率 \( \text{recall} = \frac{|\text{索引top10} \cap \text{真实top10}|}{10} \)。
4. 给 `t_hnsw` 的查询加上 `.ef(50)` 或 `.ef(100)`，重新计算召回率。

预期结果：

- 在 512 行这种小数据上，`IvfFlat` 桶内是精确扫描，召回率主要受 `nprobes` 影响。
- `IvfHnswFlat` 桶内是图搜索（近似），默认 `ef = 1.5×limit` 时召回率可能略低于 IvfFlat；**调大 `ef` 后召回率应回升并接近 IvfFlat**。这正是 `ef` 的作用——用更多候选换更高召回。
- 若数据量增大到数万行以上，你会观察到 HNSW 在**相近召回下延迟更低**，这才是它真正的价值。

> 说明：召回率的具体数值「待本地验证」，因为它取决于随机数据、`num_partitions`、`nprobes`、`ef` 的实际取值。本实践的要点是建立「调 `ef` → 看召回变化」的直觉，而不是追求某个固定数字。

#### 4.1.5 小练习与答案

**练习 1**：为什么 LanceDB 的图索引叫 `IVF_HNSW_*`，而不是单独的 `HNSW`？能否只用 HNSW 不要 IVF？

**参考答案**：因为 LanceDB 把 HNSW 嵌在 IVF 的每个分区里——先用 IVF 选 `nprobes` 个分区，再在每个分区里走 HNSW 图。IVF 负责「粗筛」跳过大部分数据，HNSW 负责「精搜」分区内部。两者分工不同，组合使用。源码注释 [vector.rs:373-378](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L373-L378) 明确说「For each IVF partition, this builds a HNSW graph」。

**练习 2**：`num_edges(m)` 和 `ef` 这两个参数，哪个是「建索引时」定的、哪个是「查询时」调的？它们各自调大的代价是什么？

**参考答案**：`num_edges(m)` 在建索引时固定（写进图结构），调大会让图更密、精度更高，但占用更多内存、建图更慢；`ef` 是查询时调的（每次查询都能改），调大看更多候选、召回更高，但单次查询延迟更高。两者分别在「建」和「查」两个阶段影响精度-速度权衡。

### 4.2 量化技术：PQ / SQ / RQ 的原理与取舍

#### 4.2.1 概念说明

量化（quantization）的本质是**用更少的比特表示一个向量**，从而压缩存储、加速距离计算，代价是引入近似误差。LanceDB 的向量索引在「桶内存什么」这一步用到三种量化器：

- **PQ（Product Quantization，乘积量化）**：把一个高维向量切成若干段（sub-vector），每段单独用一个小的码本（codebook）量化成一个短编码。它的压缩比最大（典型 32×~64×），是 `Index::Auto` 对向量列的默认选择（见 u4-l3）。
- **SQ（Scalar Quantization，标量量化）**：对每个标量维度独立做线性映射，把 `f32`（32 bit）压成 `u8`（8 bit），压缩比约 4×。精度损失比 PQ 小，压缩比也小。
- **RQ（Residual Quantization，残差量化）**：递归地量化「上一轮的残差」（真实值与当前近似之差），逐层逼近。它是唯一支持 `ApproxMode`（Fast/Normal/Accurate，见 u3-l3）精度档位的量化器。

> 一句话区分：PQ「分段各自量化」，SQ「逐维线性压缩」，RQ「层层逼近残差」。

#### 4.2.2 核心流程

**PQ 的压缩比**可以这样算。假设向量维度 \(d\)，切成 \(s\) 个子向量（`num_sub_vectors`），每个子向量量化到 \(b\) 位（`num_bits`），则：

- 原始存储：\(d \times 32\) 位（f32）
- 量化后存储：\(s \times b\) 位
- 压缩比：\(\frac{d \times 32}{s \times b}\)

LanceDB 默认取 \(s = d/16\)、\(b = 8\)，于是 \(\frac{d \times 32}{(d/16) \times 8} = 64\)，即约 64× 压缩，这与 u4-l3 给出的「约 32×~64×」一致。

**SQ** 固定把 f32 → u8，压缩比恒为 4×，源码注释 [vector.rs:432-433](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L432-L433) 写得很清楚：「each vector is mapped to a 8-bit integer vector, 4x compression ratio for float32 vector」。

近似搜索与量化的关系：量化后的距离是**近似距离**。索引阶段用近似距离快速圈出候选，必要时再用 `refine_factor` 取更多候选、用原始（或更高精度）向量重排，纠正排序。这就是为什么 IVF PQ 索引查询时 `refine_factor` 有效（[query.rs:1149-1179](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1149-L1179)）。

三种量化的选择思路：

| 量化 | 压缩比 | 精度 | 适用场景 |
| --- | --- | --- | --- |
| SQ | ~4× | 高 | 内存够、想兼顾精度与速度 |
| PQ | ~32×~64× | 中 | 数据量大、内存紧张、可接受调召回 |
| RQ | 可调（`num_bits`，默认 1） | 可调 | 需要 `ApproxMode` 动态权衡速度/精度 |

#### 4.2.3 源码精读

PQ 参数由宏 `impl_pq_params_setter!` 提供：

[rust/lancedb/src/index/vector.rs:121-144](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L121-L144) —— `num_sub_vectors` 与 `num_bits` 两个 setter。注释解释了为什么默认子向量数与 16/8 有关：每子向量含 8 或 16 个值时能用到高效的 SIMD 指令。

默认子向量数由这个函数决定：

[rust/lancedb/src/index/vector.rs:306-319](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L306-L319) —— `suggested_num_sub_vectors(dim)`：维度能被 16 整除取 `dim/16`，否则能被 8 整除取 `dim/8`，都不满足则退化为 1（并打 warning 说性能可能变差）。

RQ 的 Builder 结构最简单，只有一个 `num_bits`（默认 1）：

[rust/lancedb/src/index/vector.rs:333-369](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L333-L369) —— `IvfRqIndexBuilder`，字段只有 IVF 参数 + `num_bits`。这也呼应了 u3-l3 的结论：RQ 是唯一受 `ApproxMode` 影响的量化器（[query.rs:1198-1205](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1198-L1205)）。

`IvfHnswPqIndexBuilder` 则是「IVF + HNSW + PQ」三者字段叠加，是本讲三个 HNSW 变体里参数最多的：

[rust/lancedb/src/index/vector.rs:379-423](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L379-L423) —— 同时持有 IVF、HNSW（`m`/`ef_construction`）和 PQ（`num_sub_vectors`/`num_bits`）三组字段，`impl` 块挂载了三段宏。SQ 变体 [vector.rs:434-471](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L434-L471) 同理，只是没有 PQ 字段（SQ 当前固定 8 bit，源码 TODO 注释 [vector.rs:450](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L450) 说明 SQ 暂只支持 8 bit）。

#### 4.2.4 代码实践

实践目标：通过阅读源码与一次小实验，直观感受 PQ 的压缩与 `refine_factor` 的纠偏作用（源码阅读型 + 调参型实践）。

操作步骤：

1. 阅读仓库示例 `rust/lancedb/examples/ivf_pq.rs`，确认它用 `IvfPqIndexBuilder` 建索引后执行了一次 `nearest_to` 查询。
2. 复制 u4-l3 实践中的 IvfPq 索引表（或重新建一张 512×16 的表），分别以默认参数和 `num_bits(4)` 建 PQ 索引。
3. 对同一查询，分别执行不带 `refine_factor` 与带 `.refine_factor(5)` 的搜索，对比返回结果中 `_distance` 的排序变化。

现象观察：

- `num_bits` 越小，压缩越狠，索引文件越小，但 `_distance` 越粗糙。
- 带 `refine_factor` 时，引擎会先用 PQ 近似距离取 `k × refine_factor` 个候选，再用原始向量重算距离排序，结果更接近真实排序。

预期结果：`refine_factor` 版本的结果与 `bypass_vector_index()` 暴力搜索的结果更接近；具体召回数字「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：一个 128 维 f32 向量，用默认 PQ（`num_sub_vectors = 128/16 = 8`，`num_bits = 8`）量化后占多少字节？压缩比是多少？

**参考答案**：量化后编码 = `8 × 8 = 64` 位 = 8 字节。原始 = `128 × 4 = 512` 字节。压缩比 = 512 / 8 = 64×。

**练习 2**：SQ 的压缩比为什么是「约 4×」而不是像 PQ 那样随维度变？

**参考答案**：SQ 是对每个标量维度做 f32(32bit)→u8(8bit) 的线性映射，每维固定压缩 32/8 = 4×，与维度无关。PQ 的压缩比取决于 `num_sub_vectors` 和 `num_bits`，会随维度（进而随默认子向量数）变化。

### 4.3 Builder → IndexParams 的翻译与默认值

#### 4.3.1 概念说明

前面两个子模块看到的是「用户配方」（`IvfHnswFlatIndexBuilder` 等），它们只是配置数据的容器，本身不做任何索引工作。真正把这些配方变成底层 Lance 能识别的 `IndexParams` 的，是 `NativeTable` 上的翻译函数 `make_index_params`。理解这一层，你才能回答：「我设的 `m=20`、`num_bits=8` 到底传给了谁？」

核心结论：**LanceDB 核心只做参数搬运与类型校验，真正的算法在 `lance-index` 依赖库里**。`make_index_params` 的职责是：

1. 用 `validate_index_type` 校验列类型是否支持该索引（向量化索引要求 `FixedSizeList` 浮点列）。
2. 把 LanceDB 的 Builder 字段一一填进底层 Lance 的 `HnswBuildParams` / `PQBuildParams` / `SQBuildParams` / `RQBuildParams`。
3. 调用 `VectorIndexParams::with_ivf_*` 系列构造函数，组装成最终参数。

#### 4.3.2 核心流程

以 `IvfHnswFlat` 为例的翻译链路：

```text
用户: Index::IvfHnswFlat(IvfHnswFlatIndexBuilder { m, ef_construction, num_partitions, ... })
   │
   │  NativeTable::create_index -> make_index_params
   ▼
1) validate_index_type(field, "IVF HNSW FLAT", supported_vector_data_type)  // 类型校验
2) build_ivf_params(num_partitions, target_partition_size, sample_rate, max_iterations) -> IvfBuildParams
3) HnswBuildParams::default().num_edges(m).ef_construction(ef_construction) // 注意 num_edges = m
   │
   ▼
VectorIndexParams::ivf_hnsw(distance_type, ivf_params, hnsw_params)  // 底层 Lance 构造
   │
   ▼
底层 lance-index 执行真正的 k-means 分区 + 建图
```

一个容易踩坑的命名细节：Builder 里 HNSW 的边数字段叫 `m`，对应的 setter 叫 `num_edges`，而底层 `HnswBuildParams` 的方法也叫 `num_edges`。三者其实指同一个东西。

#### 4.3.3 源码精读

翻译主入口：

[rust/lancedb/src/table/create_index.rs:136-335](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L136-L335) —— `make_index_params`，对 `Index` 枚举逐分支 `match`。

三个 HNSW 变体的翻译分支（注意它们的高度对称）：

- [create_index.rs:316-333](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L316-L333) —— `IvfHnswFlat`：构造 `HnswBuildParams` 后调用 `VectorIndexParams::ivf_hnsw(...)`（注意是 `ivf_hnsw` 而不是 `with_ivf_hnsw_flat_params`）。
- [create_index.rs:293-315](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L293-L315) —— `IvfHnswSq`：HNSW 参数 + `SQBuildParams`，调 `with_ivf_hnsw_sq_params`。
- [create_index.rs:267-292](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L267-L292) —— `IvfHnswPq`：HNSW 参数 + `PQBuildParams`，调 `with_ivf_hnsw_pq_params`。

HNSW 参数的填装方式在三个分支里完全一致：

```rust
// create_index.rs:278-280（IvfHnswPq 分支内，IvfHnswSq/Flat 同理）
let hnsw_params = HnswBuildParams::default()
    .num_edges(index.m as usize)
    .ef_construction(index.ef_construction as usize);
```

默认值与校验相关的小函数：

- [create_index.rs:83-98](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L83-L98) —— `get_num_sub_vectors`：用户没给 `num_sub_vectors` 时按维度推算，且 `num_bits=4` 时强制让子向量数为偶数。
- [create_index.rs:64-80](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L64-L80) —— `build_ivf_params`：处理 `num_partitions` 与 `target_partition_size` 的互斥优先级（显式 `num_partitions` 优先，否则按目标分区大小，再否则用默认）。

最后，注意两个「IndexType」的区别（u4-l1 已强调）：`get_index_type_for_field` 里所有向量索引变体都映射到底层 `lance_index::IndexType::Vector`（[create_index.rs:355-362](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L355-L362)），但 LanceDB 自己的 `IndexType` 枚举（[index.rs:291-320](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs#L291-L320)）会精细区分 `IvfHnswFlat` / `IvfHnswPq` / `IvfHnswSq` / `IvfRq` 等，供 `list_indices` / `index_stats` 返回。

#### 4.3.4 代码实践

实践目标（源码阅读型）：跟踪一次 `Index::IvfHnswFlat` 从用户调用到底层构造函数的完整路径，确认每个参数的去向。

操作步骤：

1. 在 [create_index.rs:316-333](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L316-L333) 找到 `IvfHnswFlat` 分支，列出它读取了 Builder 的哪些字段。
2. 追踪 `build_ivf_params`（[L64-80](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L64-L80)）如何把 `num_partitions`、`sample_rate`、`max_iterations` 填进 `IvfBuildParams`。
3. 对照真实测试 [create_index.rs:651-693](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L651-L693)（`test_create_index_ivf_hnsw_flat`），确认它断言 `index.index_type == IvfHnswFlat` 且 `index.columns == ["embeddings"]`，说明翻译确实产出了正确类型的索引。

现象观察 / 预期结果：你应该能在脑中画出一条「Builder 字段 → make_index_params 局部变量 → 底层 BuildParams → VectorIndexParams 构造函数」的连线图，并确认 `m`、`ef_construction`、`num_partitions` 这三个用户最常调的参数都被原样搬运（无丢失、无静默改写）。

#### 4.3.5 小练习与答案

**练习 1**：如果用户建 `IvfHnswFlat` 索引时既没设 `num_partitions` 也没设 `target_partition_size`，分区数最终由谁决定？

**参考答案**：由底层 `IvfBuildParams::default()` 决定。LanceDB 的 [build_ivf_params](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L64-L80) 在两者都为 `None` 时走 `IvfBuildParams::default()` 分支，而 Builder 文档 [vector.rs:63-67](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L63-L67) 说明默认分区数约为「行数的平方根」。

**练习 2**：为什么训练索引时的 `distance_type` 必须和查询时的 `distance_type` 一致？

**参考答案**：因为 IVF 用该距离做 k-means 聚类、量化用它计算子向量编码，整个索引结构是基于某一种度量「形状」的。若查询换了一种度量，质心/编码与新度量不匹配，结果会失真。源码注释 [vector.rs:52-53](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L52-L53) 明确警告：「The metric type used to train an index MUST match the metric type used to search the index」。

## 5. 综合实践

把本讲三个子模块串起来，设计一个小任务：**为一个「中等规模」向量表选择并调优一个索引**。

任务设定：假设你有一张 5 万行、256 维 f32 向量的表（可以用 `rand` 生成），需要支持 top-10 检索，且对召回率有要求。请完成：

1. **选型分析**：对照本讲的三种量化（SQ 4× / PQ ~64× / RQ 可调）与三个 HNSW 变体，写一段话说明你会优先试哪种索引、为什么（提示：256 维能被 16 整除，PQ 默认子向量数 = 16，压缩比 = 64×）。
2. **建索引**：分别用 `IvfPq`（默认）和 `IvfHnswPq`（默认）建两个索引（注意：同一列默认 `replace=true` 会覆盖，应建在不同表上，或用 `.replace(false)` 配合不同 `name`）。
3. **建立 ground truth**：用 [query.rs:1214-1216](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1214-L1216) 的 `bypass_vector_index()` 得到真正的 top-10。
4. **调参对比**：固定查询，分别调 `nprobes`（IVF）和 `ef`（HNSW），记录召回率与延迟，画出「召回率 vs 延迟」的折线。
5. **结论**：用一句话总结，在你的数据规模下，图索引（HNSW）相比纯 IVF 是否值得——是召回更高、还是同召回下更快？

> 数据规模、硬件不同，结论会不同，这正是「调参」的意义。本实践的产出不是某个固定答案，而是一套**可复现的对比方法**：建索引 → 暴力搜索定 ground truth → 算召回 → 调参看曲线。

## 6. 本讲小结

- LanceDB 的图索引是 **IVF + HNSW** 的组合：IVF 选分区、HNSW 在分区内沿导航图搜索，桶内复杂度从线性降到子线性。
- 三个图索引变体 `IvfHnswFlat` / `IvfHnswSq` / `IvfHnswPq` 的差别只在「桶内向量怎么存」：原始 / 标量量化 / 乘积量化，精度依次降低、占用依次减小。
- HNSW 的关键参数：建索引侧 `num_edges(m)`（默认 20）与 `ef_construction`（默认 300）；查询侧 `ef`（默认 1.5×limit），后者是上线后调召回的主力。
- 三种量化各有定位：SQ 固定约 4× 压缩、精度高；PQ 约 32×~64×、压缩最大、是向量列默认；RQ 递归量化残差、是唯一支持 `ApproxMode` 的量化器。
- `make_index_params` 是「配方翻译器」：LanceDB 核心只做类型校验与参数搬运（`HnswBuildParams` / `PQBuildParams` 等），真正的 k-means、建图、量化算法都在 `lance-index` 依赖库里。
- 训练度量必须等于查询度量，否则索引结果失真；评估精度要靠 `bypass_vector_index()` 得到 ground truth 再算召回率。

## 7. 下一步学习建议

本讲完成了「索引体系」单元中向量索引的进阶部分。建议接下来：

- 学 **u4-l5（索引统计、配置与等待机制）**：了解 `IndexConfig` / `IndexStatistics` 如何读出本讲建的索引的类型（如 `IvfHnswFlat`）、`distance_type` 与覆盖行数，以及 `Waiter` 如何等待异步索引就绪。
- 回到 **u5-l1（优化：压缩与索引重建）**：建完索引后，随着数据 `add`，新写入的行会落在 `num_unindexed_rows` 里，需要 `optimize` 把它们折叠进索引——这正是索引生命周期的下一环。
- 想深入算法本身，可阅读 `lance-index` 依赖库（在 `Cargo.toml` 锁定的版本里）的 `vector::hnsw`、`vector::pq`、`vector::sq`、`vector::bq` 模块，那里才有真正的图构建与量化实现。
