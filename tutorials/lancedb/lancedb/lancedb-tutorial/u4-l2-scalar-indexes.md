# 标量索引：BTree / Bitmap / LabelList / Fm

## 1. 本讲目标

本讲承接 u4-l1（索引总览与 `Index` 枚举），把上一讲只「提了名字」的四种标量索引展开讲透。本讲只覆盖一个最小模块：**index (scalar)**。

学完本讲你应该能够：

- 说清楚 BTree、Bitmap、LabelList、Fm 四种标量索引各自的**核心机制**与**最佳适用场景**。
- 理解「基数（cardinality）」如何主导标量索引的选型——为什么高基数列适合 BTree、低基数列适合 Bitmap。
- 看懂 LanceDB 核心如何把 `Index::BTree/Bitmap/LabelList/Fm` 这四个变体，翻译到底层 Lance 的统一抽象 `ScalarIndexParams::for_builtin(...)`，并且本地表与远程表走的是同一套语义。
- 能为一张表里的不同列（数值列、字符串列、数组标签列、待做子串搜索的文本列）挑出合适的标量索引。
- 亲手在同一列上分别建 BTree 和 Bitmap，跑不同选择性的过滤，观察「标量索引是精确的（只提速不改结果）」这一关键性质。

## 2. 前置知识

### 2.1 标量索引回顾：精确、加速过滤、加速预过滤

u4-l1 已经建立了大局：标量索引是**精确（exact）**的辅助数据结构，它只改变查询速度、不改变结果；与之相对，向量索引是近似的。本讲聚焦在标量这一大类内部，到底有哪几种、各自擅长什么。

LanceDB 对标量索引有一段权威的总纲式说明，开宗明义地概括了它的用途：

[rust/lancedb/src/index/scalar.rs:1-11](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/scalar.rs#L1-L11) —— 模块文档注释：标量索引用于快速满足针对标量列的各种过滤（如 `x > 10`、`x < 10`、`x = 10`），支持数值、字符串、布尔、时间列；**并且能加速向量搜索的预过滤（prefilter）**，一次带预过滤的向量搜索可以同时用到一个标量索引和一个向量索引。

最后这一点很重要：标量索引不只是给普通 `WHERE` 扫描提速，它还和 u3-l4 讲的「向量搜索预过滤」联动——当你在 `nearest_to(...)` 之前用 `only_if(...)` 加了标量条件，引擎会优先用标量索引把候选行筛出来，再做向量搜索，从而避免对全表算距离。

### 2.2 一个核心维度：基数（cardinality）

选标量索引时，最关键的一个考量是列的**基数**——即这列里有多少种**不同的取值**：

- **高基数（high cardinality）**：取值大多各不相同，极端情况是唯一 ID 列（基数 = 行数）。
- **低基数（low cardinality）**：取值种类很少，比如性别、状态码、类别标签（几百种以内）。

BTree 和 Bitmap 的分工，本质上就是按基数划分的。LabelList 和 Fm 则是针对特殊列类型（数组列、待子串搜索的文本/二进制列）的专门索引。

### 2.3 两个容易混淆的「文本索引」

本讲会出现两个都能用在字符串列上、但机制完全不同的索引，先区分清楚，避免和 u3-l5 的全文检索（FTS）混淆：

| 索引 | 匹配对象 | 是否分词 | 返回 | 典型查询 |
| --- | --- | --- | --- | --- |
| **FTS**（倒排，u3-l5） | **词（token）** | 是，先分词 | 按 BM25 **打分排序** | `full_text_search("关键词")` |
| **Fm**（本讲） | **原始字节的任意子串** | 否，直接对字节 | 命中/不命中（精确） | `contains(text, 'needle')` |

一句话：FTS 是「按词相关性排序」，Fm 是「字面子串匹配」。两者互不替代。

## 3. 本讲源码地图

本讲涉及的核心文件：

| 文件 | 作用 |
| --- | --- |
| [`rust/lancedb/src/index/scalar.rs`](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/scalar.rs) | 四种标量索引的 Builder（目前都是无字段结构体），以及各自的文档注释——**选型依据主要来自这里的注释**。 |
| [`rust/lancedb/src/index.rs`](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs) | `Index` 枚举里的标量变体（`BTree`/`Bitmap`/`LabelList`/`Fm`）、描述用 `IndexType` 的标量变体及其 `Display`/`FromStr`。 |
| [`rust/lancedb/src/table/create_index.rs`](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs) | `make_index_params` 把四个标量变体翻译成底层 `ScalarIndexParams::for_builtin(...)`；`validate_index_type` 做列类型校验；以及大量标量索引测试。 |
| [`rust/lancedb/src/utils/mod.rs`](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/utils/mod.rs) | `supported_btree/bitmap/label_list/fm_data_type` 四个判断函数——**决定某数据类型能建哪种标量索引**。 |
| [`rust/lancedb/src/remote/table.rs`](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs) | 远程表的 `create_index`，证明四种标量索引在云端后端同样支持、语义一致。 |

## 4. 核心概念与源码讲解

### 4.1 标量索引全景：四种索引与共同特征

#### 4.1.1 概念说明

标量索引这一大类下，LanceDB 当前对外暴露四种，全部收录在 `Index` 枚举里：

[rust/lancedb/src/index.rs:30-58](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs#L30-L58) —— `Index` 枚举中的标量与全文变体：`BTree`、`Bitmap`、`LabelList`、`Fm`、`FTS`。

它们的 Builder 都定义在 `scalar.rs`，而且形态出奇地一致——**四个标量 Builder 目前都是无字段的空结构体**：

[rust/lancedb/src/index/scalar.rs:30-61](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/scalar.rs#L30-L61) —— `BTreeIndexBuilder`、`BitmapIndexBuilder`、`LabelListIndexBuilder`、`FmIndexBuilder` 四个结构体，均 `#[derive(Default, Debug, Clone, serde::Serialize)]` 且无字段。

> 为什么是空结构体？因为标量索引的算法（排序、位图、倒排等）参数很少，目前没有需要用户调节的超参。这些 Builder 的存在，更多是为了让 `Index` 枚举形态统一、并为将来扩展留口子（详见 u4-l1 的 4.2.3）。真正的选型「知识」不在结构体字段里，而在**各自的文档注释**和 `supported_*_data_type` 判断函数里。

#### 4.1.2 共同特征

尽管四种索引机制各异，它们有四点共同特征，贯穿本讲：

1. **精确（exact）**：只提速、不改结果——有无索引，同一条过滤返回的行集合完全一致。
2. **薄壳透传**：LanceDB 核心不实现 BTree/Bitmap 等算法，只把 `Index` 变体翻译成底层 `lance_index::scalar::ScalarIndexParams` 交给 Lance 引擎构建（见 4.6）。
3. **支持本地与远程**：四种标量索引在 `NativeTable` 与 `RemoteTable` 上都能创建，语义一致（见 4.7）。
4. **可加速预过滤**：一次带标量条件的向量搜索，会自动用上对应列的标量索引（见 2.1 引用的模块文档）。

#### 4.1.3 四种索引速查表

| 索引 | 适用列类型（核心） | 核心机制 | 最佳场景 | 加速的查询 |
| --- | --- | --- | --- | --- |
| **BTree** | 整数 / 浮点 / 布尔 / 字符串 / 时间 / FixedSizeBinary | 存一份**排好序**的列副本，每 4096 行一块，块头放进可缓存的 btree | **高基数**列、**高选择性**查询（命中行少） | `=`、`>`、`>=`、`<`、`<=`、范围 |
| **Bitmap** | 整数 / 字符串(Utf8/LargeUtf8) / 二进制 / 布尔 | 每个**不同取值**存一张位图，记录该值出现在哪些行 | **低基数**列（几百种取值以内） | 等值 `=`、`IN` 类 |
| **LabelList** | `List<T>` / `LargeList<T>` / `FixedSizeList<T>`（`T` 需 bitmap 支持） | 底层用 Bitmap，对数组里**每个元素**当标签建索引 | **数组 / 标签**列 | `array_contains_all`、`array_contains_any` |
| **Fm** | 字符串 / 二进制 (Utf8/LargeUtf8/Binary/LargeBinary) | FM-Index（Ferragina–Manzini），对**原始字节**做子串匹配 | **任意子串**匹配 | `contains(col, 'needle')` |

下面四节（4.2–4.5）逐一展开，4.6 讲翻译机制，4.7 讲远程一致性，4.8 给出选型决策。

### 4.2 BTree 索引：高基数的排序索引

#### 4.2.1 概念说明

BTree 是**标量列的默认索引类型**（u4-l1 讲过 `Index::Auto` 对标量列就落到 BTree）。它的思路很经典：把列的值排好序存一份，查询时用「目录（header）」快速定位到要读的少数数据块，避免全表扫描。

它最适合**取值大多各不相同**的高基数列（如用户 ID、时间戳），并且在**查询高度选择性**（即命中行数很少，比如 `id = 500` 只命中 1 行）时表现最好。

#### 4.2.2 核心流程

BTree 的结构可以用三句话概括（来自源码文档）：

- 列值**按排序顺序**存一份副本；
- 每 **4096 行**划成一块，每块在 header 里有一个条目；
- header 本身组织成一棵**可缓存的 btree**；查询时先用 header 定位「该读哪些块」，再只读那些块。

header 的大小与表行数线性相关（以 4096 行为粒度）。设表有 \(N\) 行、块大小 \(b = 4096\)，则 header 条目数约为：

\[ \text{header 条目数} \approx \frac{N}{b} \quad\Rightarrow\quad \text{header 内存} \approx \text{sizeof}(\text{Scalar}) \times \frac{N}{b} \]

源码给了一个量化例子：10 亿（1Bi）行的表，header 约占 \(\text{sizeof}(\text{Scalar}) \times 256\text{Ki}\) 字节内存（因为 \(10^9 / 4096 \approx 2.44\times10^5 \approx 256\text{Ki}\)），而定位目标行通常只需再读 \(\text{sizeof}(\text{Scalar}) \times 4096\) 字节（一个块）。

#### 4.2.3 源码精读

[rust/lancedb/src/index/scalar.rs:13-33](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/scalar.rs#L13-L33) —— `BTreeIndexBuilder` 的文档注释与（空的）结构体定义。文档明确：存排序副本、每块 4096 行、header 放独立可缓存 btree、适合「mostly distinct values」且「highly selective」的查询。

BTree 支持的数据类型由 `supported_btree_data_type` 决定：

[rust/lancedb/src/utils/mod.rs:230-244](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/utils/mod.rs#L230-L244) —— 整数、浮点、`Boolean`、`Utf8`、`Time32`/`Time64`、`Date32`/`Date64`、`Timestamp`、`FixedSizeBinary` 都支持。覆盖面很广，所以 `Index::Auto` 对绝大多数「普通」标量列都会落到 BTree（u4-l1 的 4.4 已验证）。

测试 `test_create_scalar_index` 印证了 BTree 的两种创建方式（`Auto` 与显式 `BTree`）产出同一种类型：

[rust/lancedb/src/table/create_index.rs:706-732](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L706-L732) —— 对 `i: Int32` 列分别用 `Index::Auto` 和 `Index::BTree(...)` 建索引，`list_indices` 返回的 `index_type` 都断言为 `IndexType::BTree`。

#### 4.2.4 代码实践

**实践目标**：亲手验证 BTree 是高基数列的默认/合适选择，并观察「精确」性质。

1. 建一张含高基数列 `id: Int32`（0..1000，取值全不同）的表。
2. 用 `Index::Auto` 在 `id` 上建索引，再用 `list_indices()` 确认得到的是 `BTree`。
3. 跑一个高选择性过滤 `id = 500`，记录命中行数；再跑一个低选择性过滤 `id < 100`，记录命中行数。
4. **预期结果**：前者命中 1 行，后者命中 100 行——有无索引结果不变（精确）。

> 这是「源码阅读 + 本地验证型」实践。示例代码见 4.3.4（把 BTree 与 Bitmap 放在一起对比），此处可先单独验证 BTree 分支。若暂无法本地编译运行，阅读 `test_create_scalar_index`（4.2.3 引用）的断言即可理解行为。

#### 4.2.5 小练习与答案

**练习 1**：BTree 的 header 为什么要做成「可缓存的独立 btree」，而不是和列数据放一起？

> **参考答案**：header 体积远小于全列数据（按 4096 行粒度压缩），把它单独组织成 btree 并常驻缓存，查询时绝大多数定位工作都在内存里完成，只需按定位结果读少量（通常一个）磁盘块。若 header 与数据混放，每次定位都要落盘，失去了「先内存定位、再精准读盘」的核心收益。

**练习 2**：对一个只有 5 种取值的 `category` 字符串列建 BTree，会出错吗？合理吗？

> **参考答案**：不会出错（`Utf8` 被 `supported_btree_data_type` 支持，见 4.2.3）。但**不合理**：低基数列更适合 Bitmap。BTree 对低基数列的等值查询也能工作，只是不如 Bitmap 高效——这正是 4.3 要讲的选型权衡。

### 4.3 Bitmap 索引：低基数的位图索引

#### 4.3.1 概念说明

Bitmap 索引的思路是「**一个取值一张位图**」：对列里每一种不同的取值，维护一张位图（bitmap），每一位对应一行，置 1 表示该行的值正好是这个取值。查 `category = 'A'` 时，直接取出 `'A'` 那张位图，置 1 的位就是所有命中行——无需扫描数据。

它最适合**低基数**列，即取值种类很少的列（如状态码、类别标签）。源码两处注释对「多低」给出了略不同的经验值：`scalar.rs` 说「less than 1000 unique values」，`index.rs` 说「less than a few hundreds」——共同点是**取值种类要少**，因为取值种类越多，要维护的位图越多、空间和构建成本越高。

#### 4.3.2 核心流程

```text
列值           category
行0            A
行1            B
行2            A
行3            C
              ↓  为每种取值建一张位图
位图 'A' = [1, 0, 1, 0]   ← 查 category='A' 直接返回行0、行2
位图 'B' = [0, 1, 0, 0]
位图 'C' = [0, 0, 0, 1]
```

等值查询 `col = v` 变成「取 `v` 的位图」；`IN (...)` 变成「几张位图按位或」。位图还能高效做集合运算，所以低基数列上的多值过滤尤其受益。

#### 4.3.3 源码精读

[rust/lancedb/src/index/scalar.rs:35-43](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/scalar.rs#L35-L43) —— `BitmapIndexBuilder` 文档：为每个可能取值存一张位图，适合低基数列（注释给出「less than 1000 unique values」的经验值），位图记录值出现的行 id。

`index.rs` 的枚举变体注释给出更保守的经验值：

[rust/lancedb/src/index.rs:41-45](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs#L41-L45) —— `Bitmap` 变体文档：适合低基数列，取值种类很少（「less than a few hundreds」）。

Bitmap 支持的数据类型：

[rust/lancedb/src/utils/mod.rs:246-256](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/utils/mod.rs#L246-L256) —— 整数、`Utf8`、`LargeUtf8`、`Binary`、`LargeBinary`、`Boolean`。注意它**比 BTree 多支持 `LargeUtf8`/`Binary`/`LargeBinary`**，但**少支持时间日期类型和 `FixedSizeBinary`**——两种索引的支持集并不相同。

测试 `test_create_bitmap_index` 系统展示了 Bitmap 在多种低基数列上的用法：

[rust/lancedb/src/table/create_index.rs:1089-1191](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L1089-L1191) —— 对 `category(Utf8, 仅 5 种取值)`、`large_category(LargeUtf8)`、`is_active(Boolean)`、`data(Binary)`、`large_data(LargeBinary)` 五个低基数列分别建 Bitmap，全部成功，且 `list_indices` 返回的 `index_type` 均为 `Bitmap`。注意 `category` 列的数据是 `format!("category_{}", i % 5)`，刻意做成只有 5 种取值——这正是 Bitmap 的理想场景。

#### 4.3.4 代码实践（本讲主实践）：同一列分别建 BTree 与 Bitmap

**实践目标**：在同一个标量列上分别建 BTree 和 Bitmap，跑不同选择性的过滤，体会「精确」与「选型权衡」。

1. **实践目标**：确认两种索引都能在同一列上共存（用不同名字）、对同一过滤返回相同行数（精确），并理解高基数列更适合 BTree。
2. **操作步骤**：
   - 建一张含 `id: Int32`（0..1000，高基数）和 `category: Utf8`（`i % 5`，低基数）两列的表。
   - 在**同一个 `id` 列**上分别建 BTree（名字 `id_btree`）和 Bitmap（名字 `id_bitmap`）。
   - 跑两个过滤：高选择性 `id = 500`、低选择性 `id < 100`，记录命中行数。
   - 用 `explain_plan()` 查看执行计划里引擎选择了哪个索引。
3. **需要观察的现象**：两种索引下，`id = 500` 都命中 1 行、`id < 100` 都命中 100 行（精确，结果一致）；`list_indices()` 能看到两条 `BTree`/`Bitmap` 记录共存。
4. **预期结果**：行数完全一致；执行计划中高基数 `id` 列上 BTree 通常更优。**耗时比较待本地验证**——同一列上两个索引并存时，引擎会自行选择一个，若要干净地对比 BTree 与 Bitmap 的耗时，建议另建两张数据完全相同的表，分别只建一种索引后跑同一查询计时。
5. 关键认知：Bitmap 建在高基数 `id` 列上**技术上可行**（`Int32` 被 `supported_bitmap_data_type` 支持），但要为近 1000 种取值各维护一张位图，并不划算——这正是「基数决定选型」的活教材。

示例代码（基于 `test_create_scalar_index` 与 `test_create_bitmap_index` 改写，标注为「示例代码」，非项目原有文件）：

```rust
// 示例代码：在同一 id 列上分别建 BTree 与 Bitmap，对比过滤结果
use std::sync::Arc;
use arrow_array::{Int32Array, RecordBatch, StringArray};
use arrow_schema::{DataType, Field, Schema};
use futures::TryStreamExt;
use lancedb::{connect, index::Index};
use lancedb::index::scalar::{BTreeIndexBuilder, BitmapIndexBuilder};
use lancedb::query::{ExecutableQuery, QueryBase};

#[tokio::main]
async fn main() -> lancedb::Result<()> {
    let conn = connect("memory://").execute().await?;

    let n = 1000i32;
    let schema = Arc::new(Schema::new(vec![
        Field::new("id", DataType::Int32, false),
        Field::new("category", DataType::Utf8, true),
    ]));
    let batch = RecordBatch::try_new(
        schema,
        vec![
            Arc::new(Int32Array::from_iter_values(0..n)),
            Arc::new(StringArray::from_iter_values(
                (0..n).map(|i| format!("category_{}", i % 5)),
            )),
        ],
    )?;
    let table = conn.create_table("t", batch).execute().await?;

    // 在同一个 id 列上分别建 BTree 与 Bitmap，用不同名字让二者共存
    table
        .create_index(&["id"], Index::BTree(BTreeIndexBuilder::default()))
        .name("id_btree".to_string())
        .execute().await?;
    table
        .create_index(&["id"], Index::Bitmap(BitmapIndexBuilder::default()))
        .name("id_bitmap".to_string())
        .execute().await?;

    // 列出索引，应看到两条记录
    for cfg in table.list_indices().await? {
        println!("{:?} on {:?}", cfg.index_type, cfg.columns);
    }

    // 两种选择性的过滤（精确：行数与是否建 Bitmap 无关）
    for filter in ["id = 500", "id < 100"] {
        let cnt = table.query()
            .only_if(filter)
            .execute().await?
            .try_collect::<Vec<_>>().await?
            .iter().map(|b| b.num_rows()).sum::<usize>();
        println!("filter `{}` -> {} rows", filter, cnt);
    }
    Ok(())
}
```

> 说明：`create_index` 默认 `replace = true`，但替换条件是「**同列且同名**」（见 u4-l1 的 4.3.3 对 `index.rs:191-199` 的解读），因此给两个索引用不同名字即可让它们在同一列上共存。若暂时无法本地编译运行，可记为「待本地验证」，通过阅读 `test_create_bitmap_index`（4.3.3）的断言理解行为。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Bitmap 不适合高基数列？

> **参考答案**：Bitmap 要为**每一种不同取值**各维护一张位图。高基数列取值种类多（极端如唯一 ID 列，取值数 = 行数），会导致位图数量爆炸、空间与构建成本急剧上升，查询时也要处理大量位图，得不偿失。这正是它被定位为「低基数索引」的根本原因（见 `scalar.rs:38-41` 与 `index.rs:41-45` 注释）。

**练习 2**：Bitmap 和 BTree 各自支持、但对方不支持的数据类型有哪些？

> **参考答案**：对照 `supported_bitmap_data_type`（4.3.3）与 `supported_btree_data_type`（4.2.3）：Bitmap 额外支持 `LargeUtf8`、`Binary`、`LargeBinary`；BTree 额外支持各种时间日期类型（`Time32`/`Time64`/`Date32`/`Date64`/`Timestamp`）和 `FixedSizeBinary`。所以同一列并非「两种都能建」，要看类型是否在对应支持集里。

### 4.4 LabelList 索引：数组列的多值索引

#### 4.4.1 概念说明

很多业务场景里，一列存的是**数组/标签**，比如一篇文章的若干标签 `["rust", "db", "vector"]`、一张图的多个类别。这种列的数据类型是 Arrow 的 `List<T>`（或 `LargeList<T>`、`FixedSizeList<T>`）。

普通标量索引（BTree/Bitmap）无法直接作用于「数组列」——它要求每行一个标量值。LabelList 专门解决这个问题：它把数组里**每个元素当作一个标签**，底层用 Bitmap 为这些标签建索引，从而高效支持「这个数组里**包含**某标签」这类查询。

#### 4.4.2 核心流程

```text
列值（每行一个数组）          tags
行0                          ["rust", "db"]
行1                          ["db", "vector"]
行2                          ["rust"]
                           ↓  把每个元素当标签，底层建 Bitmap
标签 'rust'   → 命中行0、行2
标签 'db'     → 命中行0、行1
标签 'vector' → 命中行1
                           ↓  支持的查询
array_contains_any(tags, ['rust','vector'])  → 行0、行1、行2（任一命中）
array_contains_all(tags, ['rust','db'])      → 行0（全部命中）
```

#### 4.4.3 源码精读

[rust/lancedb/src/index/scalar.rs:45-52](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/scalar.rs#L45-L52) —— `LabelListIndexBuilder` 文档：可用于 `List<T>` 列，支持 `array_contains_all` 与 `array_contains_any` 查询，底层用 Bitmap 索引。

支持的数据类型有「双层」要求——外层是 List 类，内层元素类型必须被 Bitmap 支持：

[rust/lancedb/src/utils/mod.rs:258-266](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/utils/mod.rs#L258-L266) —— `List(field)`/`LargeList(field)`/`FixedSizeList(field, _)`，且内层 `field.data_type()` 必须满足 `supported_bitmap_data_type`。这正呼应了「底层用 Bitmap」的设计——内层元素类型得是 Bitmap 能处理的。

测试 `test_create_label_list_index` 同时展示了「拒绝」与「成功」两条路径：

[rust/lancedb/src/table/create_index.rs:1230-1252](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L1230-L1252) —— 对 `tags: List<Utf8>` 列，`Index::BTree` 与 `Index::Bitmap` 都 `.is_err()`（普通标量索引不能作用于数组列），只有 `Index::LabelList` 成功。这正是 u4-l1 4.3.4 提到「对 List 列建 BTree/Bitmap 会被 `validate_index_type` 拒绝」的具体印证。

`LargeList` 同样支持：

[rust/lancedb/src/table/create_index.rs:1262-1305](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L1262-L1305) —— `test_create_label_list_index_on_large_list`：对 `tags: LargeList<Utf8>` 建 `LabelList` 成功。

#### 4.4.4 代码实践

**实践目标**：亲手验证「数组列只能用 LabelList」。

1. 建一张含 `tags: List<Utf8>` 列的表（参考 `test_create_label_list_index` 的 `ListBuilder` 用法，4.4.3 引用）。
2. 尝试用 `Index::BTree`、`Index::Bitmap` 建，观察两者都报错。
3. 改用 `Index::LabelList`，观察成功，并用 `list_indices()` 确认 `index_type == LabelList`。
4. **预期结果**：前两步返回 `Error::Schema`（"A BTree/Bitmap index cannot be created on the field `tags` ..."），第三步成功。
5. 若想进一步验证查询效果：建好 LabelList 后跑 `only_if("array_contains_any(tags, ...)")`，观察命中行——**待本地验证**（项目测试仅验证了索引创建，未在公开用例里断言查询命中数）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 LabelList 的内层元素类型必须满足 `supported_bitmap_data_type`？

> **参考答案**：因为 LabelList 底层就是用 Bitmap 实现的（`scalar.rs:48-50` 文档明说「using an underlying bitmap index」）。它把数组里每个元素当成一个标签去建 Bitmap，所以内层元素类型必须落在 Bitmap 的支持集里（`utils/mod.rs:258-266` 的递归判断）。比如 `List<Utf8>` 可以（`Utf8` 被 Bitmap 支持），但若内层是 Bitmap 不支持的类型，LabelList 也会被拒绝。

**练习 2**：`array_contains_all` 和 `array_contains_any` 的语义区别是什么？

> **参考答案**：`array_contains_any(col, [a, b])` = 数组里**含有 a 或 b 中任意一个**即命中（集合「或」）；`array_contains_all(col, [a, b])` = 数组里**同时含有 a 和 b** 才命中（集合「与」）。LabelList 把每个元素当标签建 Bitmap 后，这两种查询都能用位图的按位或/按位与高效完成。

### 4.5 Fm 索引：任意子串搜索

#### 4.5.1 概念说明

Fm 索引（FM-Index，Ferragina–Manzini 算法）作用于**字符串/二进制**列，专门加速**子串（substring）搜索**，即 `contains(col, 'needle')`——判断某行的文本里是否**包含**给定子串。

它与 u3-l5 的全文检索（FTS）有本质区别（见 2.3 的对照表）：FTS 先**分词**再按词匹配、按 BM25 打分；而 Fm **不分词**，直接对**原始字节**做匹配，能命中任意子串（哪怕跨过词边界、哪怕是无意义的片段）。所以 Fm 是「字面子串匹配」的利器，FTS 是「语义/词相关性排序」的利器。

#### 4.5.2 核心流程

```text
查询  contains(text, 'world')
                       ↓
Fm 索引在原始字节序列上做子串定位
                       ↓
命中的行 = 文本字节里确实出现了 "world" 的那些行（精确，命中/不命中）
```

注意它返回的是「命中/不命中」（精确匹配），不是 FTS 那样的相关性分数排序。

#### 4.5.3 源码精读

[rust/lancedb/src/index/scalar.rs:54-61](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/scalar.rs#L54-L61) —— `FmIndexBuilder` 文档：FM-Index（Ferragina–Manzini）作用于字符串/二进制列，加速子串搜索 `contains(col, 'needle')`；匹配的是**原始字节的任意子串**，而非 FTS 那种分词后的词。

支持的数据类型：

[rust/lancedb/src/utils/mod.rs:282-289](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/utils/mod.rs#L282-L289) —— 注释说明 FM-Index 加速原始字节的 `contains` 子串搜索，故只支持 `Utf8`、`LargeUtf8`、`Binary`、`LargeBinary`。

测试 `test_create_fm_index` 完整展示了「建 Fm → 用 `contains` 过滤」的端到端用法：

[rust/lancedb/src/table/create_index.rs:753-798](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L753-L798) —— 对 `text: Utf8` 列（值为 `"hello world"`）建 `Index::Fm`，再用 `.only_if("contains(text, 'world')")` 查询，断言命中 1 行。这是本讲四种索引里唯一一个在公开测试里**实际跑了过滤查询并断言命中数**的用例。

#### 4.5.4 代码实践

**实践目标**：亲手验证 Fm 的「任意子串匹配」与 FTS 的区别。

1. 建一张含 `text: Utf8` 列的表，写入若干行（如 `"hello world"`、`"world peace"`、`"goodbye"`）。
2. 建 `Index::Fm`。
3. 分别跑 `contains(text, 'world')`、`contains(text, 'oo')`（跨/无意义子串）过滤，记录命中行。
4. **预期结果**：`'world'` 命中前两行；`'oo'` 命中 `"goodbye"` 那行（`goo`dbye 里含 `oo`）——这正是「任意子串、不分词」的体现，FTS 不一定能这样命中无意义片段。
5. 若暂无法本地运行，阅读 `test_create_fm_index`（4.5.3）的断言即可理解。

#### 4.5.5 小练习与答案

**练习 1**：什么场景下该选 Fm，什么场景下该选 FTS？

> **参考答案**：需要**字面子串匹配**（如日志里的错误码片段、文件路径片段、序列号前缀，或任何无自然语言语义的字节搜索）选 Fm，用 `contains(col, 'needle')`，结果是精确命中。需要**按自然语言关键词的相关性排序**（如文档检索、问答）选 FTS，用 `full_text_search(...)`，结果带 BM25 `_score`。二者底层不同（Fm 是字节子串索引，FTS 是分词倒排索引），不可互相替代。

**练习 2**：Fm 索引能建在 `Int32` 列上吗？

> **参考答案**：不能。`supported_fm_data_type`（4.5.3）只支持 `Utf8`/`LargeUtf8`/`Binary`/`LargeBinary`，`Int32` 不在其中，`validate_index_type` 会返回 `Error::Schema`。Fm 的子串匹配是面向字节序列的，数值类型没有「子串」语义。

### 4.6 翻译机制：从 `Index` 变体到底层 `ScalarIndexParams`

#### 4.6.1 概念说明

四种标量索引的 Builder 虽然都是空结构体，但 `Index` 枚举变体本身是有区分度的。`NativeTable::make_index_params` 负责把每个变体翻译成底层 Lance 引擎认识的参数对象，并在此之前做类型校验。

#### 4.6.2 核心流程

```text
Index::BTree(_)     ─┐
Index::Bitmap(_)    ─┤  validate_index_type(field, 名称, supported_*_data_type)
Index::LabelList(_) ─┤        ↓ 校验通过
Index::Fm(_)        ─┘  ScalarIndexParams::for_builtin(BuiltinIndexType::Xxx)
                            ↓ 交给 Lance 引擎构建
```

四个标量变体的翻译路径高度对称：都是「校验 → `for_builtin(对应大类)`」。

#### 4.6.3 源码精读

[rust/lancedb/src/table/create_index.rs:171-198](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L171-L198) —— `make_index_params` 的四个标量分支：`BTree` → 校验 `supported_btree_data_type` → `for_builtin(BuiltinIndexType::BTree)`；`Bitmap` → `Bitmap`；`LabelList` → `LabelList`；`Fm` → `Fm`。注意它们都走同一个 `ScalarIndexParams::for_builtin(...)`，差别只在传入的 `BuiltinIndexType` 大类和校验用的判断函数。

校验本身是公用的：

[rust/lancedb/src/table/create_index.rs:45-61](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L45-L61) —— `validate_index_type` 接收一个 `supported_fn: impl Fn(&DataType) -> bool`，不支持时返回形如「A {名称} index cannot be created on the field `{列}` which has data type {类型}」的清晰错误。这个泛型 `impl Fn` 的设计让四种索引共用同一段校验代码，只是传入的判断函数不同。

> 关键认知：LanceDB 核心对标量索引的「实现」非常薄——它不做排序、不画位图、不构造 FM 结构，这些全在底层 `lance_index`/Lance 里。核心的职责是「**选型校验 + 把变体翻译成大类标识**」，与 u1-l1 说的「LanceDB 是 Lance 的薄壳」、u4-l1 说的「真正干活的部分很薄」完全一致。

#### 4.6.4 小练习与答案

**练习**：为什么 `make_index_params` 对每个标量变体都要先调一次 `validate_index_type`，而不是统一在入口校验一次？

> **参考答案**：因为四种标量索引的支持集**各不相同**（见 4.3.5 练习 2）。每个变体需要用自己专属的 `supported_*_data_type` 判断，所以校验必须和具体变体绑定。`validate_index_type` 通过接收 `impl Fn(&DataType) -> bool` 参数，把「校验框架」与「具体判断函数」解耦——框架只写一遍，每个变体传入自己的判断函数即可（4.6.3）。

### 4.7 本地与远程的一致性

#### 4.7.1 概念说明

u4-l1 讲过 `create_index` 经 `BaseTable` trait 落到本地 `NativeTable` 或远程 `RemoteTable`。标量索引在两种后端上语义一致——远程表把索引请求序列化成 HTTP 报文发给 LanceDB Cloud，但支持的标量种类、`Auto` 选型规则与本地完全相同。

#### 4.7.2 源码精读

[rust/lancedb/src/remote/table.rs:2113-2116](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L2113-L2116) —— 远程 `create_index` 把 `Index::BTree/Bitmap/LabelList/Fm` 分别映射成线类型字符串 `"BTREE"`/`"BITMAP"`/`"LABEL_LIST"`/`"FM"`，并序列化各自（空）参数发往服务器。

[rust/lancedb/src/remote/table.rs:2123-2124](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L2123-L2124) —— 远程 `Index::Auto` 对标量列同样落到 `"BTREE"`，判断依据同样是 `supported_btree_data_type`，与本地 `make_index_params` 的 Auto 分支（`create_index.rs:157-160`）一致。

> 这说明：用户为标量列选索引时，不必关心表是本地还是云端——同一套 `Index::BTree/Bitmap/LabelList/Fm` 语义两边通用。这与 u2-l2「本地与远程共用同一套 Table API、对用户透明」一脉相承。

### 4.8 选型决策：基数 × 列类型

学完四种索引，把它们串成一张可操作的选型决策表。选型的两个维度是**列类型**和**基数**：

```text
是数组列 (List/LargeList/FixedSizeList)？
  └ 是 → 内层元素被 Bitmap 支持吗？
          └ 是 → LabelList
          └ 否 → （当前无合适标量索引）
  └ 否：
      要做子串搜索 contains(col, '...') 且列是字符串/二进制？
        └ 是 → Fm
        └ 否：
            基数低（几百种取值以内）且类型在 Bitmap 支持集 → Bitmap
            否则（高基数、范围/等值查询）→ BTree（也是 Auto 的默认）
```

几条经验法则：

- **不知道选什么，就 `Index::Auto`**：对标量列它会给你 BTree（本地见 `create_index.rs:157-160`，远程见 4.7.2），是安全的默认。
- **低基数列主动选 Bitmap**：状态、类别、布尔这类列，Bitmap 通常优于默认的 BTree。
- **数组/标签列必须 LabelList**：BTree/Bitmap 会被 `validate_index_type` 拒绝（4.4.3）。
- **子串搜索用 Fm，关键词检索用 FTS**：别把 Fm 和 u3-l5 的 FTS 混用（4.5.1）。

## 5. 综合实践

把本讲的四种标量索引串成一个完整任务：为一张模拟「内容库」的表，给每个列挑最合适的标量索引并验证。

**任务**：建一张含四列的表，分别建四种标量索引，并用对应的过滤验证。

1. 建表，列设计刻意覆盖四种索引的最佳场景：

   | 列 | 类型 | 数据特征 | 计划建的索引 |
   | --- | --- | --- | --- |
   | `id` | `Int32` | 0..N，高基数（唯一） | `BTree` |
   | `category` | `Utf8` | `i % 5`，低基数（5 种） | `Bitmap` |
   | `tags` | `List<Utf8>` | 每行若干标签 | `LabelList` |
   | `text` | `Utf8` | 任意文本 | `Fm` |

2. 对四列分别用对应 `Index` 变体建索引（`List<Utf8>` 的构造参考 `test_create_label_list_index` 的 `ListBuilder`，4.4.3）。
3. 跑四组验证查询，断言命中行数符合预期：

   | 验证查询 | 预期（示例） |
   | --- | --- |
   | `id = 500`（BTree 友好） | 命中 1 行 |
   | `category = 'category_3'`（Bitmap 友好） | 命中约 N/5 行 |
   | `array_contains_any(tags, ...)`（LabelList） | 命中含该标签的行 |
   | `contains(text, 'world')`（Fm） | 命中含该子串的行 |

4. 调用 `table.list_indices().await?`，确认返回四条 `IndexConfig`，`index_type` 分别为 `BTree`/`Bitmap`/`LabelList`/`Fm`。

**验证要点**：

- 四种索引都建成功、`list_indices` 类型正确——印证 4.1.3 速查表。
- 同样的过滤，建索引前后命中行数不变——印证「标量索引精确」（4.1.2）。
- 若把 `category` 误建成 `BTree` 也能成功（但非最优），把 `tags` 误建成 `BTree` 则会报 `Error::Schema`——印证 4.4.3 的「数组列只能 LabelList」。

如果你暂时无法编译运行，改为**纯阅读型综合实践**：依次打开 `test_create_scalar_index`（4.2.3）、`test_create_bitmap_index`（4.3.3）、`test_create_label_list_index`（4.4.3）、`test_create_fm_index`（4.5.3）四个测试，逐个解释它们建了哪种索引、断言了什么，并对照 4.8 的决策表说明为什么该列选该索引。

## 6. 本讲小结

- LanceDB 的标量索引有四种：**BTree**（高基数、高选择性，存排序副本+可缓存 btree header）、**Bitmap**（低基数，每取值一张位图）、**LabelList**（数组/标签列，底层 Bitmap，支持 `array_contains_*`）、**Fm**（字符串/二进制列的任意子串 `contains` 搜索）。
- 四种 Builder 目前都是**无字段空结构体**（`scalar.rs:30-61`），选型知识不在字段里，而在各自**文档注释**与 `supported_*_data_type` 判断函数里。
- **基数（cardinality）是 BTree vs Bitmap 选型的核心**：高基数/范围查询用 BTree（也是 `Index::Auto` 对标量列的默认），低基数的等值/IN 用 Bitmap；数组列必须 LabelList，子串搜索用 Fm。
- Fm 与 FTS 不可混淆：Fm 是**不分词的原始字节子串匹配**（精确命中），FTS 是**分词后按 BM25 打分排序**（u3-l5）。
- 翻译机制很薄：`make_index_params` 对每个标量变体先 `validate_index_type`（用专属 `supported_*_data_type`），再 `ScalarIndexParams::for_builtin(对应大类)` 交给 Lance（`create_index.rs:171-198`）；真正的算法在底层 `lance_index`/Lance。
- 本地与远程**语义一致**：远程 `create_index` 把四种标量变体映射成 `"BTREE"`/`"BITMAP"`/`"LABEL_LIST"`/`"FM"` 线类型（`remote/table.rs:2113-2116`），`Auto` 对标量列同样落到 BTree。

## 7. 下一步学习建议

本讲把标量索引讲透了。按依赖关系，接下来建议：

- **u4-l3 向量索引 IVF 家族**：从标量（精确）跨到向量（近似），展开 IvfFlat/IvfPq/IvfSq/IvfRq 的空间划分思想与 `num_partitions` 等参数，体会「速度 vs 召回」的另一面。
- **u4-l4 HNSW 与量化**：展开图索引变体与 PQ/SQ/RQ 量化原理。
- **u4-l5 索引统计与等待机制**：深入本讲反复用到的 `list_indices()`/`index_stats()` 返回结构（`IndexConfig`/`IndexStatistics`）字段含义，以及远程异步索引的 `Waiter` 轮询。

阅读源码时，建议带着本讲的「**基数 × 列类型**」决策表去对照：每看到一张业务表，先想每列的基数和类型，再判断该用哪种标量索引，最后用 `supported_*_data_type`（`utils/mod.rs:230-289`）核对你的判断是否被代码允许。
