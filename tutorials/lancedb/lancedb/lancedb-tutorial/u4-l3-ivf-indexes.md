# 向量索引 IVF 家族

## 1. 本讲目标

本讲是「索引体系」单元的第三篇，承接 [u4-l1 索引总览与 Index 枚举] 与 [u1-l4 Arrow 数据模型与向量列表示]，专门拆解 LanceDB **向量索引的核心骨架——IVF 家族**。

读完本讲，你应该能够：

- 说清 **IVF（Inverted File，倒排文件）** 是如何用 k-means 把向量空间划分成分区、从而把暴力搜索 \(O(N)\) 降下来的。
- 区分 **IvfFlat / IvfPq / IvfSq / IvfRq** 四种变体在「精度」与「内存/磁盘占用」上的取舍，能为自己的数据挑一个合适的。
- 掌握 `num_partitions`、`sample_rate`、`num_sub_vectors`、`num_bits` 等关键参数的含义、默认值与影响。
- 读懂从用户层 `Index::IvfPq(...)` 到底层 Lance `VectorIndexParams` 的「翻译」过程，知道参数到底流到了哪里。
- 会用召回率（与暴力搜索对比）和查询延迟两个指标，亲自调参验证。

本讲只覆盖一个最小模块：**index (vector)**。图索引变体（IvfHnsw 系列）与量化原理深挖留到 [u4-l4 HNSW 与量化技术]。

## 2. 前置知识

本讲默认你已掌握下列概念（若陌生请先看对应讲义）：

- **向量列的 Arrow 表示**（[u1-l4]）：向量列约定为 `FixedSizeList<Float32>`，维度编码进类型里，例如 128 维就是 `FixedSizeList<Float32, 128>`。
- **Index 枚举与三类索引**（[u4-l1]）：`Index` 是用户构建索引的「配方」，分标量、向量、全文三类；向量索引是**近似最近邻（ANN）**索引，以精度换速度。本讲的四个变体都是 `Index` 的变体。
- **DistanceType**（[u3-l3]）：`L2` / `Cosine` / `Dot` / `Hamming`，默认 `L2`。一个关键约束——**训练索引用的度量必须和查询用的度量一致**，否则结果不准。
- **Builder + execute 模式**（[u4-l1]）：`create_index(...).execute().await` 才真正写盘，之前只是配置。

几个本讲会用到的术语，先用一句话建立直觉：

- **最近邻搜索（Nearest Neighbor）**：给定一个查询向量，找出库里和它最相似的 k 个向量。暴力做法是把库里所有向量都比一遍，叫 **flat search**。
- **召回率（Recall）**：ANN 搜索返回的 top-k 里，有多少个真正属于暴力搜索的 top-k。召回率越接近 1 越准。
- **量化（Quantization）**：用更少比特表示一个向量，牺牲一点精度换取更小的存储和更快的比较。PQ / SQ / RQ 是三种不同的量化策略。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `rust/lancedb/src/index/vector.rs` | IVF 家族所有 **Builder** 的定义（字段、默认值、参数 setter），是本讲的主战场。 |
| `rust/lancedb/src/index.rs` | `Index` 枚举（把 Builder 包成变体）、`IndexType` 枚举（描述已存在索引的精确类型）。 |
| `rust/lancedb/src/table/create_index.rs` | 把用户层 `Index` **翻译**成底层 Lance `VectorIndexParams` 的核心函数 `make_index_params` / `build_ivf_params`，以及验证与默认值逻辑。 |
| `rust/lancedb/examples/ivf_pq.rs` | 官方最小示例：建表 → 用 `Index::Auto` 与显式 `Index::IvfPq` 建索引 → 搜索，本讲代码实践以此为蓝本。 |
| `rust/lancedb/src/query.rs` | 查询侧的 IVF 相关参数 `nprobes` / `refine_factor`，调召回率时用得到。 |
| `rust/lancedb/src/utils/mod.rs` | `supported_vector_data_type`，决定哪些列能建向量索引。 |

一句话脉络：用户在 `index/vector.rs` 里用 Builder 配好参数 → `Index` 枚举（`index.rs`）把它包起来 → `create_index.rs` 的 `make_index_params` 把它翻译成 Lance 底层参数 → 真正的训练与搜索算法在依赖库 `lance` / `lance-index` 里。

## 4. 核心概念与源码讲解

### 4.1 IVF 空间划分原理

#### 4.1.1 概念说明

假设你的表里有 100 万个 128 维向量。用户搜一次，flat search 要算 100 万次距离，太慢。**IVF（Inverted File，倒排文件）** 的思路是：先把这 100 万个向量「分桶」，把空间切成若干个区域（partition），每个区域记一个**质心（centroid）**；查询时先看 query 离哪些质心最近，只在这几个区域里仔细搜，其它区域直接跳过。

这样就把「全库扫描」变成了「只扫几个桶」。代价是：万一真正的近邻被分到了你没搜的桶里，就会漏掉——这就是 ANN「近似」二字的来源，也是召回率会小于 1 的原因。

IVF 本身只负责「怎么分桶、怎么挑桶」，**不规定桶里向量怎么存**。桶里可以存原始向量（IvfFlat），也可以存量化的压缩向量（IvfPq / IvfSq / IvfRq）。这就是「IVF 家族」的由来：同一个 IVF 骨架 + 不同的量化策略 = 四兄弟。

#### 4.1.2 核心流程

IVF 索引的完整生命周期分三个阶段：

1. **训练阶段（Train）**：从全表随机采样若干向量，跑 **k-means** 聚类，得到 `num_partitions` 个质心。采样数 ≈ `sample_rate × num_partitions`；k-means 最多跑 `max_iterations` 轮。
2. **建索引阶段（Build）**：把每个向量分配到「离它最近的质心」所在的分区，形成倒排表。分区内存原始向量或量化码。
3. **查询阶段（Search）**：
   - 算 query 向量到**所有质心**的距离，挑出最近的 `nprobes` 个分区；
   - 在这 `nprobes` 个分区内搜索候选；
   - 可选 **refine**：用原始（未量化）向量对候选重排，修正顺序、提升召回。

伪代码描述：

```
训练: sample = 随机采样(sample_rate * num_partitions 个向量)
      centroids = kmeans(sample, k=num_partitions, max_iter=max_iterations)
建索引: for v in 全表: partitions[argmin dist(v, centroids)].append(v)
查询(query, k, nprobes):
      nearest_cells = argsort(dist(query, centroids))[:nprobes]
      candidates = ⋃ 在 nearest_cells 各分区内搜索
      (可选) refine: 用原始向量对 candidates 重排, 取 top-k
      return top-k
```

搜索的计算量从全库的 \(N\) 次距离，降到约 \(nprobes \times \frac{N}{num\_partitions}\) 次：

\[
\text{搜索成本} \;\approx\; \text{nprobes} \times \frac{N}{\text{num\_partitions}} \;\ll\; N
\]

这就解释了两个最直观的调参方向：

- `num_partitions` 太大 → 质心太多，「挑分区」这一步本身就慢；太小 → 每个分区太大，「分区内搜索」慢。
- `nprobes` 太小 → 漏掉真近邻，召回低；太大 → 接近全库扫，速度优势消失。

#### 4.1.3 源码精读

IVF 的训练参数集中在 `impl_ivf_params_setter!` 宏里，所有 IVF 家族成员都复用它。先看最关键的 `num_partitions`：

[rust/lancedb/src/index/vector.rs:61-74](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L61-L74) —— `num_partitions` setter。注意三点：① 类型是 `Option<u32>`（默认 `None`，交给底层决定）；② 注释明确说该值应随行数增长；③ 太大挑分区慢、太小分区内搜索慢（正是上面流程的翻译）。

[rust/lancedb/src/index/vector.rs:76-92](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L76-L92) —— `sample_rate`：控制 k-means 的采样规模，训练用的向量总数 = `sample_rate × num_partitions`，默认 **256**。一般用默认即可，调大可能略提质量但训练更慢。

[rust/lancedb/src/index/vector.rs:94-108](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L94-L108) —— `max_iterations`：k-means 最大迭代轮数，默认 **50**。多数情况 k-means 会提前收敛，这个值很少需要动。

[rust/lancedb/src/index/vector.rs:110-117](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L110-L117) —— `target_partition_size`：与 `num_partitions` 二选一的另一种指定方式——不直接说「分多少区」，而是说「每个分区大约多少行」，让底层自己算分区数。值越大搜索越快但越不准。

`distance_type` 由另一个宏 `impl_distance_type_setter!` 提供，默认 `L2`：

[rust/lancedb/src/index/vector.rs:42-58](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L42-L58) —— 注释里有句最重要的约束：**训练用的度量必须等于查询用的度量**（"The metric type used to train an index MUST match the metric type used to search the index."）。

> ⚠️ **关于默认 `num_partitions` 的文档与现实差异**
>
> `num_partitions` 的文档注释（[vector.rs:65-66](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L65-L66)）写的是「默认取行数的平方根」，官方示例注释也据此举例（1000 行约 31 个分区，√1000 ≈ 31.6）。**但仓库内的测试验证的真实行为并非如此**：[test_ivf_pq_uses_default_partition_size_for_num_partitions](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L477-L538) 用默认 Builder 在 16384 行上建索引，断言分区数 == `num_rows / 8192` == 2，而不是 √16384 == 128。这说明当前底层 Lance 的默认是按「目标分区大小（约 8192 行/分区）」推导分区数，文档略滞后于实现。**结论：生产环境请显式设置 `num_partitions`，不要依赖默认值；任何关于默认值的结论都以本地验证为准。**

#### 4.1.4 代码实践（源码阅读型）

**目标**：在源码里把「IVF 参数 → 底层 k-means 训练」这条链路走通，确认你设的 `num_partitions` 到底传给了谁。

**步骤**：

1. 打开 [rust/lancedb/src/table/create_index.rs:64-80](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L64-L80) 的 `build_ivf_params`，观察三分支逻辑：
   - 传了 `num_partitions` → `IvfBuildParams::new(n)`；
   - 只传了 `target_partition_size` → `IvfBuildParams::with_target_partition_size(n)`；
   - 都没传 → `IvfBuildParams::default()`。
   - 然后统一写入 `sample_rate` 与 `max_iters`。
2. 搜索 `IvfBuildParams` 的来源（文件顶部 `use lance_index::vector::ivf::IvfBuildParams;`），确认它来自依赖库 `lance-index`——真正的 k-means 算法不在 LanceDB 核心。
3. 回答：如果你既不传 `num_partitions` 也不传 `target_partition_size`，分区数由谁决定？

**预期结果**：你会清楚地看到 LanceDB 核心**只做参数搬运**，k-means 与分区划分的真正逻辑在 `lance-index` 里。这正是「核心 Rust + 底层依赖」的分层。

#### 4.1.5 小练习与答案

**练习 1**：为什么把 `num_partitions` 从 10 调到 10000，查询未必变快？

**参考答案**：查询第一步要算 query 到**所有质心**的距离来挑 `nprobes` 个分区。`num_partitions` 越大，质心越多，这第一步「挑分区」越慢；同时每个分区更小，分区内搜索更快。两端都有成本，存在一个平衡点，所以不是越大越好。

**练习 2**：`sample_rate` 和 `num_partitions` 谁影响 k-means 的训练样本数？

**参考答案**：训练样本数 ≈ `sample_rate × num_partitions`（见 [vector.rs:83](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L83)）。两者共同决定。

---

### 4.2 IVF 四兄弟：Flat / PQ / SQ / RQ 的精度与内存权衡

#### 4.2.1 概念说明

IVF 只管「分桶」，桶里的向量怎么存决定了索引的精度和体积。LanceDB 提供四种存储策略，对应四个 Builder：

- **IvfFlat（不量化）**：桶里存**原始向量**。精度最高、召回最好，但磁盘和内存占用最大（和原始数据一样大）。适合数据量不大、对召回极敏感的场景。
- **IvfPq（乘积量化 Product Quantization）**：把一个 d 维向量切成 `num_sub_vectors` 段子向量，每段独立跑一个小 k-means（码本），向量被替换成「每段最近码字的编号」。压缩比极高，是 `Index::Auto` 对向量列的默认选择。
- **IvfSq（标量量化 Scalar Quantization）**：对**每一个标量维度**独立量化（典型到 8-bit）。压缩比中等（float32 → int8 约 4 倍），精度比 PQ 好。
- **IvfRq（残差量化 / RabitQ）**：迭代地量化「残差」，RabitQ 是一种有理论保证的方法。在相近压缩比下常比 PQ 更准；它也是唯一受 `ApproxMode`（[u3-l3]）影响的索引类型。

直觉上的一句话总结：**压缩越狠，体积越小、比较越快，但精度越低、召回越差。**

#### 4.2.2 核心流程

**PQ 压缩**（以 128 维 float32、`num_sub_vectors = 16`、`num_bits = 8` 为例）：

1. 把 128 维向量切成 16 段，每段 8 维。
2. 对每段 8 维子空间，用 k-means 训练一个含 \(2^{\text{num\_bits}} = 256\) 个码字的码本。
3. 每个向量的每一段，替换成「最近码字的编号」——一个 8-bit（1 字节）的码。
4. 最终一个向量从 \(128 \times 4 = 512\) 字节，压成 \(16 \times 1 = 16\) 字节。

PQ 的压缩比：

\[
\text{压缩比} \;=\; \frac{\text{dim} \times 4\,\text{字节}}{\text{num\_sub\_vectors} \times \text{num\_bits} / 8}
\]

查询时用**非对称距离计算（ADC）**：query 的每段子向量与对应码本算距离，预先建表再查表求和，避免解压。

**SQ 压缩**：每个维度独立映射到 `num_bits`（默认 8）个比特。128 维 float32 → 128 字节（int8），约 **4 倍**压缩。比较时直接在量化后的整型上算距离。

**RQ 压缩**：先量化一次得到近似，再用残差（原向量 − 近似）再量化，迭代若干级。LanceDB 的 RQ 基于 RabitQ，默认 `num_bits = 1`。

四兄弟的权衡对照表（128 维 float32 为例）：

| 变体 | 桶内存储 | 单向量占用 | 压缩比 | 精度/召回 | 典型场景 |
| --- | --- | --- | --- | --- | --- |
| IvfFlat | 原始向量 | 512 B | 1×（不压缩） | 最高 | 数据小、召回至上 |
| IvfSq | 标量量化 | 128 B | ≈4× | 较高 | 想省内存又不愿牺牲太多精度 |
| IvfPq | 乘积量化 | 16 B（m=16）/ 8 B（m=8） | ≈32× / 64× | 中等 | 大规模、默认选择 |
| IvfRq | 残差量化 | 依 num_bits | 可调 | 中高 | 想用 ApproxMode 精调 |

> 说明：PQ 的 m（`num_sub_vectors`）越大，每段维度越小、压缩越少、精度越高；`num_bits` 越大，码本越大、码字越精细、精度越高。两者都是「精度换体积」的旋钮。

#### 4.2.3 源码精读

**哪些列能建向量索引？** 先看门槛：

[rust/lancedb/src/utils/mod.rs:291-299](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/utils/mod.rs#L291-L299) —— `supported_vector_data_type`：必须是 `FixedSizeList`，且子字段是浮点类型（`is_floating()`，含 Float16/Float32/Float64）或 `UInt8`（二值/字节向量）。这也是 `Index::Auto` 判断「这是不是向量列」的依据。

**四个 Builder 的字段对比**——这是本节最该精读的地方：

[rust/lancedb/src/index/vector.rs:181-209](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L181-L209) —— `IvfFlatIndexBuilder`：字段只有 IVF 那一组（`distance_type`、`num_partitions`、`sample_rate`、`max_iterations`、`target_partition_size`），**没有任何量化参数**，因为不压缩。文档明确说「stores raw vectors」。

[rust/lancedb/src/index/vector.rs:216-244](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L216-L244) —— `IvfSqIndexBuilder`：字段也**只有 IVF 那一组**，Builder 没有暴露 `num_bits`（SQ 固定走底层 8-bit 默认）。

[rust/lancedb/src/index/vector.rs:266-304](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L266-L304) —— `IvfPqIndexBuilder`：在 IVF 字段之外，**多了 PQ 专属的 `num_sub_vectors` 与 `num_bits`**（都是 `Option`）。宏 `impl_pq_params_setter!` 提供这两个 setter。

[rust/lancedb/src/index/vector.rs:333-369](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L333-L369) —— `IvfRqIndexBuilder`：只多了 `num_bits` 一个量化参数（RQ 不需要子向量分段）。

把这四段并排看，结论很清晰：**Builder 字段的差异 = 量化策略的差异**。Flat 没有量化字段；SQ 不暴露 num_bits；PQ 有子向量+比特数；RQ 只有比特数。

**这些 Builder 如何被翻译到底层？** 看 `make_index_params` 里四个变体的 match 分支：

[rust/lancedb/src/table/create_index.rs:199-210](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L199-L210) —— `Index::IvfFlat`：先 `validate_index_type` 校验列类型，再用 `build_ivf_params` 组装 IVF 参数，最后 `VectorIndexParams::with_ivf_flat_params(distance_type, ivf_params)`。注意 `index.distance_type.into()`——这是 [u3-l3] 讲过的 LanceDB `DistanceType` 到底层 `MetricType` 的 `Into` 转换。

[rust/lancedb/src/table/create_index.rs:230-250](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L230-L250) —— `Index::IvfPq`：关键默认值在这里。`num_bits` 用户不传时 `unwrap_or(8)`，即 **PQ 默认 8 bit**；`num_sub_vectors` 用户不传时走 `get_num_sub_vectors`。然后 `PQBuildParams::new(num_sub_vectors, num_bits)` + `VectorIndexParams::with_ivf_pq_params(...)`。

[rust/lancedb/src/table/create_index.rs:251-266](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L251-L266) —— `Index::IvfRq`：`num_bits` 用户不传时 `unwrap_or(1)`，即 **RQ 默认 1 bit**，`RQBuildParams::new(num_bits)`。

[rust/lancedb/src/table/create_index.rs:211-229](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L211-L229) —— `Index::IvfSq`：用 `SQBuildParams { sample_rate, ..Default::default() }`，num_bits 走底层默认（8-bit）。

**`Index::Auto` 对向量列默认选谁？** IvfPq：

[rust/lancedb/src/table/create_index.rs:142-156](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L142-L156) —— 当列是向量类型时，`Index::Auto` 用 `IvfBuildParams::default()` + `PQBuildParams::new(num_sub_vectors, 8)` + 度量 `L2`，组装成 IVF_PQ。这就是「Auto 对向量列默认建 IvfPq（L2）」的来源（呼应 [u4-l1]）。

最后，建好的索引在 `list_indices` / `index_stats` 里如何被描述？看精确类型枚举：

[rust/lancedb/src/index.rs:291-307](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs#L291-L307) —— `IndexType` 枚举的向量变体：`IvfFlat` / `IvfSq` / `IvfPq` / `IvfRq` / `IvfHnswPq` / `IvfHnswSq` / `IvfHnswFlat`。注意它是「描述已存在索引」的精细类型（呼应 [u4-l1] 三个同名 IndexType 的区分）。

#### 4.2.4 代码实践（运行型）

**目标**：亲手建一张表，分别用 `IvfFlat`、`IvfPq`、`IvfSq` 建索引，用 `index_stats` 看到它们被识别为不同 `IndexType`，直观体会三者的差异。

**步骤**（以官方示例 [ivf_pq.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/ivf_pq.rs) 为蓝本改写，**以下为示例代码**）：

```rust
// 示例代码：基于 examples/ivf_pq.rs 改写
use lancedb::index::vector::{IvfFlatIndexBuilder, IvfPqIndexBuilder, IvfSqIndexBuilder};
use lancedb::index::Index;

// 假设 tbl 是一张含 vector 列(dim=128)的表
// 1) Flat：不压缩
tbl.create_index(&["vector"], Index::IvfFlat(IvfFlatIndexBuilder::default()))
    .execute().await?;
// 2) PQ：默认 8 bit，子向量数按维度推算
tbl.create_index(&["vector"], Index::IvfPq(IvfPqIndexBuilder::default()))
    .execute().await?;
// 3) SQ：标量量化
tbl.create_index(&["vector"], Index::IvfSq(IvfSqIndexBuilder::default()))
    .execute().await?;

// 注意：IndexBuilder 默认 replace=true，后建的会覆盖前一个。
// 想同时保留多个索引，请分别 .name("...") 命名、replace(false)。
for cfg in tbl.list_indices().await? {
    println!("{:?} on {:?}", cfg.index_type, cfg.columns);
}
```

**需要观察的现象**：

- 每次建索引后，`list_indices()` 返回的 `index_type` 分别是 `IvfFlat` / `IvfPq` / `IvfSq`。
- 由于默认 `replace = true`（见 [index.rs:184](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs#L184)），连续建会覆盖，最后只剩一个；想对比请在同一列用不同 `.name(...)` 并 `.replace(false)`。

**预期结果**：你能用 `index_stats` 看到不同类型的索引。索引体积（`size_bytes`，见 [IndexConfig](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs#L365-L422)）应当呈现 Flat > Sq > Pq 的趋势。具体字节数**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `IvfSqIndexBuilder` 没有 `num_sub_vectors` 参数，而 `IvfPqIndexBuilder` 有？

**参考答案**：PQ 把向量**切段**再做子空间量化，所以需要 `num_sub_vectors` 控制切多少段；SQ 是对**每个标量维度**独立量化，没有「切段」概念，自然不需要这个参数（见 [vector.rs:216-244](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L216-L244) vs [vector.rs:266-284](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L266-L284)）。

**练习 2**：PQ 默认 `num_bits` 是多少？RQ 呢？分别在源码哪里？

**参考答案**：PQ 默认 8（[create_index.rs:241](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L241) `unwrap_or(8)`）；RQ 默认 1（[create_index.rs:259](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L259) `unwrap_or(1)`）。

---

### 4.3 关键参数：num_partitions、PQ 子向量与 num_bits、查询侧 nprobes/refine

#### 4.3.1 概念说明

IVF 家族的调参空间可以分成「**建索引时**」和「**查询时**」两组，理解它们的分工是调出好召回率的关键：

- **建索引参数**（写进 Builder）：`num_partitions`（分多少区）、`sample_rate` / `max_iterations`（k-means 训练质量）、PQ 的 `num_sub_vectors` / `num_bits`（压缩程度）。这些一旦 `execute()` 就固化进索引文件，改它们要重建索引。
- **查询参数**（写在 `VectorQuery` 上）：`nprobes`（搜几个分区）、`refine_factor`（是否用原始向量重排）。这些**每次查询都能改**，是上线后微调召回率的主要手段，无需重建索引。

一个好的工作流是：先在建索引时定下合理的 `num_partitions` 与压缩策略，再在查询侧用 `nprobes` / `refine_factor` 把召回率调到位。

#### 4.3.2 核心流程

PQ 子向量数的「建议值」由维度决定，规则在 `suggested_num_sub_vectors` 里：

- 维度能被 16 整除 → `dim / 16`（首选，便于 SIMD）；
- 否则能被 8 整除 → `dim / 8`；
- 否则 → 1（性能不佳，会打 warning）。

以 dim=128 为例：\(128 / 16 = 8\)，所以默认 `num_sub_vectors = 8`（这正是官方示例注释里「dimension 128 would have been 8 by default」的来源）。

查询侧两个参数的作用：

- `nprobes`：默认 **20**。值越大召回越高、延迟越长。它同时设置 `minimum_nprobes` 和 `maximum_nprobes`。
- `refine_factor`：默认不启用（`None`）。启用后，先按量化距离取 `limit × refine_factor` 个候选，再取这些候选的**原始向量**按真实距离重排，保留 top-k。能同时提升召回和排序准确性，代价是多一次原始向量读取。

#### 4.3.3 源码精读

**`num_sub_vectors` 默认推算**：

[rust/lancedb/src/index/vector.rs:306-319](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L306-L319) —— `suggested_num_sub_vectors`：dim/16 → dim/8 → 1 的三级回退，注释强调 8 或 16 个值/子向量才能用高效 SIMD。

[rust/lancedb/src/table/create_index.rs:83-98](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L83-L98) —— `get_num_sub_vectors`：用户传了就用用户的；否则用 `suggested_num_sub_vectors`；特殊处理——当 `num_bits == 4` 时子向量数必须为偶数，否则 +1 补齐。

**官方示例里的典型调参**：

[rust/lancedb/examples/ivf_pq.rs:100-116](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/ivf_pq.rs#L100-L116) —— 显式 `Index::IvfPq`：`.distance_type(Cosine).num_partitions(50).num_sub_vectors(16)`。注释点明：1000 行默认约 31 分区（此处显式给 50），dim=128 默认 8 子向量（此处显式给 16，压缩更少、精度更高）。

[rust/lancedb/examples/ivf_pq.rs:139-149](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/ivf_pq.rs#L139-L149) —— 查询侧调参：`.distance_type(Cosine).limit(15).nprobes(30).refine_factor(1)`。注意 `.distance_type(Cosine)` 必须和建索引时的 Cosine 一致。

**查询侧参数定义**：

[rust/lancedb/src/query.rs:1038-1066](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1038-L1066) —— `nprobes`：文档明确「only used when the vector column has an IVF PQ index. If there is no index then this value is ignored.」，默认 20，调大增召回但增延迟。实现上同时写 `minimum_nprobes` 与 `maximum_nprobes`。

[rust/lancedb/src/query.rs:1149-1179](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1149-L1179) —— `refine_factor`：先取 `limit × refine_factor` 个候选，再用**未压缩**原始向量重排取 top-k。注释还提醒：传 1 和完全不传不一样——传任意值都会触发原始向量读取，有延迟成本。

#### 4.3.4 代码实践（运行型·调参对比）

**目标**：用 `IvfPq` 建索引，扫一组不同的 `num_partitions`，对同一个 query 比较**召回率**（以无索引暴力搜索为基准）与**查询延迟**，体会 `num_partitions` 与查询侧 `nprobes` 的关系。

**步骤**（**以下为示例代码**，基于 [ivf_pq.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/ivf_pq.rs) 改造，把「全 1 向量」换成随机向量，否则召回没意义）：

```rust
// 示例代码：召回率对比骨架（需自行补全随机数据生成与计时）
use std::time::Instant;
use lancedb::index::vector::IvfPqIndexBuilder;
use lancedb::index::Index;
use lancedb::query::{ExecutableQuery, QueryBase};
use lancedb::{DistanceType, connect};

const K: usize = 10;
let db = connect("data/recall-bench").execute().await?;
// 1) 建一张 N 行、dim 维的随机向量表（自行用 rand 填充，避免全 1）
// 2) 基准：无索引暴力搜索，得到 ground-truth top-K 的 id 集合
let gt: std::collections::HashSet<i32> = /* table.vector_search(q)?.limit(K).execute() 收集 id */;

// 3) 对不同 num_partitions 建索引、搜索、算召回
for &npart in &[10usize, 50, 100, 500] {
    let tbl = db.open_table("vectors").execute().await?;
    tbl.create_index(
        &["vector"],
        Index::IvfPq(IvfPqIndexBuilder::default().num_partitions(npart as u32)),
    ).execute().await?;

    let start = Instant::now();
    let mut got = std::collections::HashSet::new();
    let mut s = tbl.vector_search(&query_vec)?
        .distance_type(DistanceType::L2)
        .limit(K)
        .nprobes(20)          // 查询侧分区数，可再扫一组 [5,20,50]
        .execute().await?;
    while let Some(batch) = s.try_next().await? { /* 收集 id 进 got */ }
    let latency = start.elapsed();

    let recall = gt.intersection(&got).count() as f64 / K as f64;
    println!("num_partitions={npart} nprobes=20 recall={recall:.2} latency={latency:?}");
}
```

**需要观察的现象**：

- 固定 `nprobes=20` 时，`num_partitions` 很小（如 10）→ 每个分区大、搜索近似度高、召回偏高但延迟偏高；`num_partitions` 很大（如 500）且 `nprobes` 不变 → 只搜 20/500 的分区，可能漏掉真近邻，召回下降但单分区搜索变快。
- 把 `nprobes` 调大（如 50），大 `num_partitions` 的召回会回升，但延迟也回升。
- 加 `.refine_factor(1)` 通常能小幅提升召回并修正排序，代价是延迟略增。

**预期结果**：你会得到一张「num_partitions × nprobes → 召回/延迟」的表格，据此为你的数据选「最小够用的 nprobes」。**具体数值待本地验证**，取决于数据分布、维度、行数。

> ⚠️ 注意：示例里的 `vector_search(q)?.limit(K)` 在**无索引**时退化为 flat search（暴力），正是「基准答案」的来源（呼应 [u4-l1] 的 flat search）。比较时务必让基准搜索与索引搜索用**相同**的 `distance_type`，否则对比无效。

#### 4.3.5 小练习与答案

**练习 1**：上线后发现召回率不够，但又不想重建索引，应该调哪个参数？

**参考答案**：优先调查询侧的 `nprobes`（增大陆_partition 数）和 `refine_factor`（启用原始向量重排）。它们无需重建索引，每次查询都能改（见 [query.rs:1062](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1062) 与 [query.rs:1176](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1176)）。

**练习 2**：dim=128 时，PQ 默认 `num_sub_vectors` 是多少？为什么推荐 8 或 16 的倍数？

**参考答案**：默认 8（\(128/16\)，见 [vector.rs:307-309](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L307-L309)）。推荐让每个子向量含 8 或 16 个值，是为了用上底层高效的 SIMD 指令（见 [vector.rs:130-132](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L130-L132)）。

**练习 3**：建索引用 `Cosine`，查询用 `L2`，会发生什么？

**参考答案**：结果不准。训练度量和查询度量必须一致（见 [vector.rs:52-53](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L52-L53) 的明确警告）。

---

## 5. 综合实践

设计一个贯穿本讲的小任务：**为你的数据选出「召回达标、延迟最低」的 IVF_PQ 配置**。

1. **准备数据**：构造一张 5000～10000 行、64 或 128 维的随机向量表（务必随机，避免全 1），写入 `data/ivf-tuning`。
2. **建立基准**：在无索引状态下对 10 个查询做 `vector_search` 取 top-10，作为 ground-truth。
3. **建索引扫描**：用 `IvfPq`，扫一组 `num_partitions`（如行数的 1%、5%、10%），每组都重建索引。
4. **查询扫描**：对每个索引，扫一组 `nprobes`（如 5、20、50）和是否开 `refine_factor(1)`。
5. **记录指标**：每个组合记录平均召回率（与 ground-truth 的交集占比）和平均延迟。
6. **决策**：找出「召回率 ≥ 0.9 的组合里延迟最低」的那组，作为你的生产配置。

完成后再回答一个开放问题：在你的数据上，是「多分区 + 大 nprobes」好，还是「少分区 + 小 nprobes」好？为什么？这能帮你把本讲的 IVF 原理、量化权衡、查询调参真正串起来。

（本任务的运行结果取决于具体数据与机器，所有数值请以本地验证为准。）

## 6. 本讲小结

- **IVF 是骨架**：用 k-means 把向量空间划成 `num_partitions` 个分区，查询只搜 `nprobes` 个最近分区，把搜索成本从 \(O(N)\) 降到 \(O(\text{nprobes}\cdot N/\text{num\_partitions})\)。
- **四兄弟的差别在「桶里存什么」**：IvfFlat 存原始向量（最准最大），IvfSq 标量量化（≈4×，较准），IvfPq 乘积量化（≈32×～64×，中等），IvfRq 残差量化（可配 num_bits、受 ApproxMode 影响）。Builder 字段的差异直接对应量化策略的差异。
- **参数分两组**：建索引侧（`num_partitions`/`sample_rate`/`max_iterations`/`num_sub_vectors`/`num_bits`，固化进索引）与查询侧（`nprobes`/`refine_factor`，每次可调、是上线后调召回的主力）。
- **默认值要记牢**：`distance_type` 默认 L2；`sample_rate` 默认 256、`max_iterations` 默认 50；PQ `num_bits` 默认 8、子向量数默认 `dim/16`；RQ `num_bits` 默认 1；`nprobes` 默认 20。`Index::Auto` 对向量列默认建 IvfPq(L2)。
- **LanceDB 核心只搬参数**：`make_index_params` 把 Builder 翻译成底层 `lance`/`lance-index` 的 `VectorIndexParams`，真正的 k-means 与量化算法在依赖库里。
- **重要约束与陷阱**：训练度量必须等于查询度量；文档里「默认分区数=√行数」与仓库测试的真实行为（按约 8192 行/分区推导）不一致，生产请显式设 `num_partitions`。

## 7. 下一步学习建议

- 进入 **[u4-l4 HNSW 与量化技术]**：本讲提到了 `IvfHnswPq` / `IvfHnswSq` / `IvfHnswFlat` 三个图索引变体，下一讲会讲 HNSW 图相对 IVF 的收益，并深挖 PQ/SQ/RQ 的量化原理。
- 想理解索引建好后的统计与等待机制，看 **[u4-l5 索引统计、配置与等待机制]**：`IndexStatistics`、`index_stats`、`Waiter`。
- 想从查询侧系统理解 `nprobes`/`refine_factor` 所在的 `VectorQuery`，回顾 **[u3-l2 向量相似度搜索]**。
- 进阶阅读：直接打开依赖库 `lance-index` 中 `IvfBuildParams`、`PQBuildParams`、`SQBuildParams`、`RQBuildParams` 的源码，看 k-means 训练与量化码本是如何真正实现的。
