# 索引统计、配置与等待机制

## 1. 本讲目标

本讲是「索引体系」单元（u4）的收尾，承接 u4-l1（索引总览与 `Index` 枚举）。前面 u4-l2~u4-l4 讲的都是「**怎么建**」索引，本讲要回答两个同样重要的问题：建好之后「**怎么查它的信息**」，以及「**怎么知道它真的建好了**」。

学完本讲你应该能够：

- 区分 LanceDB 提供的两套索引信息接口——`list_indices()` 返回的 `IndexConfig`（「身份证」，列全表所有索引的概要）和 `index_stats(name)` 返回的 `IndexStatistics`（「体检报告」，单个索引的统计），并能说出它们各自有哪些字段、何时为 `None`。
- 读懂 `NativeTable` 与 `RemoteTable` 各自如何实现这两个接口，理解「本地直接读 Lance / 远程走 HTTP」的差异。
- 完整复述 `wait_for_index` 的轮询机制：它如何用 `list_indices` + `index_stats` 交替探询，直到 `num_unindexed_rows == 0`，以及在超时、索引未出现时的两种 `Error` 走向。
- 理解为什么「等待索引就绪」主要面向**远程异步索引**（`IndexBuilder::wait_timeout` 只对远程表有意义），而本地表索引是同步的。

本讲只覆盖两个最小模块：`index`（统计与配置）和 `index (waiter)`。

## 2. 前置知识

### 2.1 建索引是「同步」还是「异步」

在 u4-l1 你已经看过 `create_index` 的完整流程：调用 `IndexBuilder::execute()` 后，本地表（`NativeTable`）会**同步地**把索引构建好并落盘，`execute` 返回时索引就已经可用了——就像你在本地调用一个普通函数。

但在**远程后端**（LanceDB Cloud，见 u6-l3）上，索引构建是**异步的**：`create_index` 只是把「请帮我建这个索引」的请求通过 HTTP 发给服务器，服务器在后台慢慢构建，HTTP 调用很快返回。于是会出现一个时间窗口——**索引已被声明创建，但还没真正建完**。此时查询要么用不上索引（退化为暴力搜索），要么报错。

这就是本讲要解决的核心问题之一：**怎么在异步构建期间，可靠地等到「索引真的就绪」？** 答案是 LanceDB 提供的 `wait_for_index` 轮询函数。

### 2.2 两个需要先认识的术语

- **indexed rows（已索引行数）**：已经被纳入索引、查询时能被索引加速到的行数，即 `num_indexed_rows`。
- **unindexed rows（未索引行数）**：表里存在、但还没被该索引覆盖的行数，即 `num_unindexed_rows`。一个索引「完全就绪」的判据就是 `num_unindexed_rows == 0`。

一个直觉公式：

\[ \text{num\_unindexed\_rows} = \text{表总行数} - \text{num\_indexed\_rows} \]

（源码里用的是 `saturating_sub`，保证不为负，见 4.1.3。）

### 2.3 统计信息为什么不「一步到位」

你会奇怪：既然 `list_indices` 已经能列出索引，为什么还要单独一个 `index_stats`？因为两者职责不同：

- `list_indices` 一次返回**全表所有索引**的**概要配置**（名字、类型、列、段数等），适合「我这张表上一共有哪些索引」。
- `index_stats` 针对单个索引返回**统计细节**（已索引行数、未索引行数、距离度量等），适合「这个索引建得到底怎么样、覆盖了多少行」。

两者信息有部分重叠（都有 `index_type`、行数），但侧重不同，所以 LanceDB 同时提供。

## 3. 本讲源码地图

本讲涉及的核心文件：

| 文件 | 作用 |
| --- | --- |
| [`rust/lancedb/src/index.rs`](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs) | 定义 `IndexConfig`（索引配置）、`IndexStatistics`（索引统计）及其内部反序列化结构 `IndexStatisticsImpl`/`IndexMetadata`，以及 `IndexBuilder::wait_timeout`。 |
| [`rust/lancedb/src/index/waiter.rs`](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/waiter.rs) | `wait_for_index` 自由函数：轮询 `list_indices` + `index_stats` 直到索引完全就绪或超时。 |
| [`rust/lancedb/src/table.rs`](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs) | `BaseTable` trait 契约（`list_indices`/`index_stats`/`wait_for_index`）、对外 `Table` 转发、`NativeTable` 本地实现（从 Lance `describe_indices` / `index_statistics` 组装数据）。 |
| [`rust/lancedb/src/remote/table.rs`](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs) | `RemoteTable` 实现：`create_index` 末尾按 `wait_timeout` 触发 `wait_for_index`，`index_stats` 走 HTTP `/v1/table/{id}/index/{name}/stats/`，以及远程等待机制的 mock 测试。 |
| [`rust/lancedb/src/table/create_index.rs`](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs) | 多个 `create_index` 测试展示标准用法：建索引后紧跟 `wait_for_index` 等待就绪。 |
| [`rust/lancedb/src/error.rs`](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/error.rs) | 本讲用到的两个 `Error` 变体：`InvalidInput`（超时上限过大）与 `Timeout`（轮询超时）。 |

## 4. 核心概念与源码讲解

### 4.1 模块 `index`：索引统计与配置

#### 4.1.1 概念说明

建好索引后，用户通常想知道三件事：**这张表上一共有哪些索引？某个索引是什么类型、覆盖了多少行？向量索引用的是哪种距离度量？** LanceDB 用两个公开类型分别回答：

1. **`IndexConfig`（索引配置）**——可以理解为索引的「**身份证**」。`list_indices()` 返回一个 `Vec<IndexConfig>`，每个元素描述一个已存在索引的静态属性：名字、类型、所在列、UUID、创建时间、段数、文件大小、索引版本等。字段最全，但很多字段在远程表上为 `None`。

2. **`IndexStatistics`（索引统计）**——可以理解为索引的「**体检报告**」。`index_stats(name)` 针对单个索引返回，重点是**动态统计量**：已索引行数、未索引行数、距离度量（仅向量索引有）、索引分片数。

> 为什么字段里有那么多 `Option`？源码注释反复提到一句「`None` if unavailable (e.g. for remote tables)」：本地表能从 Lance 直接读到几乎全部细节，而远程表目前只通过 HTTP 返回有限信息，很多字段（UUID、`type_url`、`created_at`、`size_bytes`、`num_segments` 等）暂不透出。写代码时**务必把这些字段当 `Option` 处理**，不能假定一定有值。

#### 4.1.2 核心流程

查询索引信息的调用链（本地分支）：

```text
Table::list_indices()                          // 1. 对外入口
   └─ inner.list_indices()  → BaseTable 契约   // 2. 转发到 trait 对象
        └─ NativeTable::list_indices()         // 3. 本地实现
             ├─ dataset.count_rows(None)       //    取表总行数（算 unindexed 用）
             ├─ dataset.describe_indices(None) //    从 Lance 取所有索引描述
             └─ 逐个 idx_desc → IndexConfig    //    翻译字段、计算 num_unindexed_rows

Table::index_stats(name)                       // 1. 对外入口
   └─ inner.index_stats(name) → BaseTable 契约
        └─ NativeTable::index_stats(name)      // 2. 本地实现
             ├─ dataset.index_statistics(name) //    从 Lance 取 JSON 字符串
             ├─ IndexNotFound → Ok(None)       //    索引不存在则返回 None
             └─ serde_json 反序列化 → IndexStatistics
```

两个接口都遵循 u2-l2 讲过的「**Table 持有 `Arc<dyn BaseTable>`，方法几乎都是一行转发**」模式，本地与远程后端对外接口完全一致——用户无需关心数据来自 Lance 引擎还是 HTTP。

#### 4.1.3 源码精读：`IndexConfig` 的字段

先看「身份证」的全部字段。这是 `list_indices` 的返回元素：

[rust/lancedb/src/index.rs:366-422](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs#L366-L422) —— `IndexConfig` 完整定义。逐字段解读：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `name` | `String` | 索引名（建索引时 `.name()` 指定，否则用默认名 `{列名}_idx`） |
| `index_type` | `IndexType` | 描述用的精确类型（u4-l1 讲的 `crate::index::IndexType`，如 `IvfPq`/`BTree`） |
| `columns` | `Vec<String>` | 索引所在列，**目前恒为长度 1**（注释说明将来可能支持复合索引） |
| `index_uuid` | `Option<String>` | 首段的 UUID（一个索引可由多段组成） |
| `type_url` | `Option<String>` | protobuf 类型 URL，精确的类型标识 |
| `created_at` | `Option<DateTime<Utc>>` | 创建时间，取所有段的最小值 |
| `num_indexed_rows` | `Option<u64>` | 已索引行数，**近似值**，可能含已删除的行 |
| `num_unindexed_rows` | `Option<u64>` | 未索引行数 = 表总行数 − `num_indexed_rows` |
| `size_bytes` | `Option<u64>` | 所有索引文件总字节数 |
| `num_segments` | `Option<u32>` | 索引由多少段组成 |
| `index_version` | `Option<i32>` | 磁盘上的索引格式版本 |
| `index_details` | `Option<String>` | 索引类型专属细节，序列化为 JSON，**形状随索引类型变化** |

几个关键提醒（都写在源码注释里）：

- `num_indexed_rows` 是**近似**的，可能包含已被删除的行——所以不要用它精确判断「当前有效数据量」。
- `num_unindexed_rows` 的注释明确：「优化索引（`optimize`）会把这些未覆盖的行折叠进索引」——这与 u5-l1 讲的 compaction 优化直接相关：写入新数据后，老索引不再覆盖新行，需要 `optimize` 才能补上。
- `index_details` 是 JSON 字符串，不同索引类型的字段不同，需要按 `index_type` 分情况解析。

#### 4.1.4 源码精读：`IndexStatistics` 的字段与反序列化

再看「体检报告」。它字段更少，但带有 `IndexConfig` 没有的 `distance_type`：

[rust/lancedb/src/index.rs:447-461](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs#L447-L461) —— `IndexStatistics` 公开结构：`num_indexed_rows`、`num_unindexed_rows`、`index_type`、`distance_type: Option<DistanceType>`、`num_indices: Option<u32>`。

注意 `distance_type` 的注释：「**This is only present for vector indices.**」标量索引（BTree/Bitmap 等）没有距离度量，这里是 `None`；向量索引（Ivf*）才有，值是 u3-l3 讲过的 `DistanceType`（L2/Cosine/Dot/Hamming）。这是判断「这个索引是不是向量索引」的一个实用信号。

为什么 `IndexStatistics` 字段比 `IndexConfig` 少这么多？因为它的数据来自 Lance 的 `Dataset::index_statistics()`，返回的是一段 **JSON 字符串**，LanceDB 用一组**内部**反序列化结构解析：

[rust/lancedb/src/index.rs:424-443](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs#L424-L443) —— 内部结构 `IndexMetadata`（`metric_type` + `index_type`）和 `IndexStatisticsImpl`（含 `num_indexed_rows`/`num_unindexed_rows`/`indices: Vec<IndexMetadata>`/`index_type`/`num_indices`）。它们带 `#[skip_serializing_none]`，对底层返回的 JSON 容错（缺字段时为 `None`）。

这里有个值得注意的设计细节：**索引类型在 JSON 的两个层级都可能出现**——顶层 `index_type`，或 `indices` 数组里每个元素各自的 `index_type`。所以 `NativeTable::index_stats` 反序列化时要兜底取值：

[rust/lancedb/src/table.rs:3027-3044](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L3027-L3044) —— `stats.index_type.or(first_index.index_type)`：先取顶层，没有再取首个元素的；都没有就报 `Error::InvalidInput`。`distance_type` 则取 `first_index.metric_type`。

#### 4.1.5 源码精读：`NativeTable` 如何填充 `IndexConfig`

本地表组装「身份证」的过程最能体现「LanceDB 是 Lance 的薄壳」：大部分字段是直接从 Lance 的 `idx_desc`（索引描述符）搬运，只有 `num_unindexed_rows` 是 LanceDB 自己算出来的：

[rust/lancedb/src/table.rs:2925-2986](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L2925-L2986) —— `NativeTable::list_indices` 逐行解读：

1. `dataset.count_rows(None)` 取表总行数 `total_rows`（注意：这正是远程表没有的本地能力）。
2. `dataset.describe_indices(None)` 取所有索引描述，用 `filter_map` 逐个翻译（解析失败的就 `warn` 并跳过，不会让整个调用失败）。
3. `idx_desc.index_type().parse()` 把字符串类型解析成 `crate::index::IndexType`（用 u4-l1 讲的 `FromStr`，`table.rs:2933`）。
4. `idx_desc.field_ids()` + `dataset.schema().field_path(...)` 把字段 ID 翻译成列名字符串。
5. `idx_desc.segments()` 取所有段，从中取首段 UUID、最小创建时间、索引版本。
6. 关键计算：`num_unindexed_rows: Some(total_rows.saturating_sub(num_indexed_rows))`（`table.rs:2977`）——`saturating_sub` 保证结果不会因为 `total_rows < num_indexed_rows`（近似值含已删行）而变成负数溢出。

而 `index_stats` 的本地实现则把「索引不存在」翻译成 `Ok(None)`，对调用者更友好：

[rust/lancedb/src/table.rs:3009-3020](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L3009-L3020) —— `NativeTable::index_stats`：底层 Lance 返回 `IndexNotFound` 错误时，不向上抛错，而是返回 `Ok(None)`；其它错误用 `Error::from(e)` 转换（u2-l4 讲过的 `From` 自动转换）。这与对外 `Table::index_stats` 文档承诺的「Returns None if the index does not exist」一致。

#### 4.1.6 对外入口与远程差异

对外 `Table` 的三个方法都是一行转发（u2-l2 的委托模式）：

[rust/lancedb/src/table.rs:1624-1627](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1624-L1627) —— `Table::list_indices`。

[rust/lancedb/src/table.rs:1666-1673](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1666-L1673) —— `Table::index_stats`，接收 `impl AsRef<str>`，所以可以直接传 `&str` 或 `String`。

远程表的 `index_stats` 走 HTTP，把 404 也翻译成 `None`，与本地语义对齐：

[rust/lancedb/src/remote/table.rs:2488-2502](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L2488-L2502) —— `RemoteTable::index_stats`：POST 到 `/v1/table/{id}/index/{name}/stats/`，若服务器返回 `NOT_FOUND` 则 `Ok(None)`。这正是「本地与远程对用户完全透明」的体现——同样的 `index_stats` 调用，背后一个是函数调用直达 Lance，一个是 HTTP 往返，但「索引不存在返回 None」的语义两边一致。

#### 4.1.7 代码实践：读取一个向量索引的统计信息

1. **实践目标**：建一个向量索引，用 `list_indices` 与 `index_stats` 分别读取信息，观察两者的字段差异，特别验证 `distance_type` 仅向量索引存在。
2. **操作步骤**：
   - 阅读 `create_index.rs` 中的 `test_create_index`：它建索引后用 `wait_for_index` 等待，是标准用法。
   - 参考下面的「示例代码」写一个小程序（放进 `rust/lancedb/examples/` 自建 example，或直接复用测试里的数据构造方式）。
3. **需要观察的现象**：`list_indices()` 返回的 `IndexConfig` 里，`index_type == IvfPq`、`columns == ["vector"]`、`num_indexed_rows`/`num_unindexed_rows` 为 `Some(...)`；`index_stats("vector_idx")` 返回的 `IndexStatistics` 里 `distance_type == Some(L2)`。
4. **预期结果**：向量索引的 `distance_type` 有值；若再建一个标量索引（如 BTree），它的 `distance_type` 应为 `None`。

示例代码（基于官方测试改写，标注为「示例代码」，非项目原有文件）：

```rust
// 示例代码：读取索引配置与统计
use std::sync::Arc;
use arrow_array::{Float32Array, RecordBatch};
use arrow_schema::{DataType, Field, Schema};
use lancedb::{connect, index::Index};

#[tokio::main]
async fn main() -> lancedb::Result<()> {
    let conn = connect("memory://").execute().await?;
    let dim = 8usize;
    let schema = Arc::new(Schema::new(vec![Field::new(
        "vector",
        DataType::FixedSizeList(
            Arc::new(Field::new("item", DataType::Float32, true)),
            dim as i32,
        ),
        false,
    )]));
    let n = 512;
    let vals = Float32Array::from_iter_values((0..(n * dim)).map(|v| v as f32));
    // 省略：把 vals 包装成 FixedSizeListArray 再组成 RecordBatch
    // let vectors = Arc::new(create_fixed_size_list(vals, dim as i32)?);
    // let batch = RecordBatch::try_new(schema, vec![vectors])?;
    // let table = conn.create_table("t", batch).execute().await?;

    // table.create_index(&["vector"], Index::Auto).execute().await?; // → IvfPq, 默认名 vector_idx
    // table.wait_for_index(&["vector_idx"], std::time::Duration::from_secs(30)).await?;

    // // 身份证：list_indices
    // for cfg in table.list_indices().await? {
    //     println!("{:?} on {:?} indexed={:?} unindexed={:?}",
    //         cfg.index_type, cfg.columns, cfg.num_indexed_rows, cfg.num_unindexed_rows);
    // }
    // // 体检报告：index_stats
    // if let Some(s) = table.index_stats("vector_idx").await? {
    //     println!("type={:?} distance={:?} indexed={} unindexed={}",
    //         s.index_type, s.distance_type, s.num_indexed_rows, s.num_unindexed_rows);
    // }
    Ok(())
}
```

> 说明：上面注释掉了构造 `FixedSizeListArray` 与执行的几行。完整写法请直接参照 `create_index.rs:393-462` 的 `create_fixed_size_list` 辅助函数与 `test_create_index` 主体。若暂时无法本地编译运行，可记为「待本地验证」，仅通过阅读源码与测试断言理解字段含义即可。

#### 4.1.8 小练习与答案

**练习 1**：`IndexConfig` 的 `index_details` 字段是什么？为什么它的类型是 `Option<String>` 而不是一个强类型结构体？

> **参考答案**：`index_details` 是「索引类型专属细节」，序列化为 JSON 字符串（`index.rs:417-421`）。因为不同索引类型的细节字段完全不同（IvfPq 有子向量数/比特数，BTree 没有这类参数），用统一的强类型结构体无法涵盖。所以 LanceDB 选择返回原始 JSON，让调用方按 `index_type` 自行解析；当无可用细节（如无对应插件）或远程表未透出时为 `None`。

**练习 2**：为什么 `NativeTable::list_indices` 计算未索引行数时用 `saturating_sub` 而不是普通减法？

> **参考答案**：`num_indexed_rows` 是近似值，可能包含已被删除的行（`index.rs:394-397` 注释），在极端情况下可能出现 `num_indexed_rows > 表总行数`。普通减法会导致无符号整数下溢（panic 或回绕成巨大值），`saturating_sub` 在结果本应为负时钳制为 0（`table.rs:2977`），更安全。

**练习 3**：如何只通过 `index_stats` 的返回值判断一个索引是不是向量索引？

> **参考答案**：看 `distance_type` 字段是否为 `Some(_)`（`index.rs:454-458`）。向量索引才有距离度量（L2/Cosine/Dot/Hamming），标量索引该字段为 `None`。（也可以看 `index_type` 是否属于 `Ivf*` 家族，但 `distance_type` 更直接。）

### 4.2 模块 `index (waiter)`：轮询等待索引就绪

#### 4.2.1 概念说明

回到本讲开头的核心问题：远程索引是异步构建的，`create_index` 返回时索引可能还没建好。即使本地表索引是同步的，也有一类场景需要等待——**批量建多个索引后统一等它们全部就绪**。

LanceDB 的解法不是给索引加一个「构建完成」事件回调，而是采用最朴素也最通用的**轮询（polling）**策略：每隔一小段时间查一次「这个索引覆盖的未索引行数是不是 0」，是则认为建好了。这个逻辑封装在自由函数 `wait_for_index` 里（文件 `index/waiter.rs`），本地表和远程表共用同一份实现。

> 关键判据只有一个：`num_unindexed_rows == 0`（`waiter.rs:47`）。所以本模块其实高度依赖 4.1 的 `index_stats`——waiter 正是反复调用 `index_stats` 来读取这个数字。

#### 4.2.2 核心流程

`wait_for_index(table, index_names, timeout)` 的轮询逻辑：

```text
1. 校验 timeout <= MAX_WAIT（2 小时），否则 Error::InvalidInput
2. remaining = index_names（待等待列表）
3. 循环（只要 start.elapsed() < timeout）:
   a. list_indices() 取当前所有索引
   b. 对 remaining 里每个名字:
        - 若 list_indices 里还看不到该名字 → 还没创建，跳过本轮
        - 否则 index_stats(name):
            - 返回 None   → 统计还没准备好，跳过本轮
            - 返回 Some(s):
                - 若 s.num_unindexed_rows == 0 → 标记为「完成」
                - 否则                         → 继续等
   c. 从 remaining 移除所有「完成」的
   d. 若 remaining 为空 → 返回 Ok(())（全部就绪）
   e. 否则 sleep(DEFAULT_SLEEP_MS = 1s) 再进下一轮
4. 循环结束仍未就绪 → 记录 debug 诊断日志，返回 Error::Timeout
```

两个常量决定了轮询的「节奏」和「上限」：

[rust/lancedb/src/index/waiter.rs:11-12](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/waiter.rs#L11-L12) —— `DEFAULT_SLEEP_MS = 1000`（每轮间隔 1 秒）、`MAX_WAIT = 2 * 60 * 60`（最长等待 2 小时）。

#### 4.2.3 源码精读：轮询主体

逐段精读 `wait_for_index`。先是入口与超时上限校验：

[rust/lancedb/src/index/waiter.rs:16-27](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/waiter.rs#L16-L27) —— 函数签名 `pub async fn wait_for_index(table: &dyn BaseTable, index_names: &[&str], timeout: Duration) -> Result<()>`。开头先校验 `timeout > MAX_WAIT` 直接返回 `Error::InvalidInput`——这是防止用户误传一个超大超时把程序卡死两小时以上的护栏。`remaining` 用 `index_names.to_vec()` 初始化，作为「还没就绪的索引」工作列表。

然后是核心轮询循环：

[rust/lancedb/src/index/waiter.rs:30-69](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/waiter.rs#L30-L69) —— 循环条件 `start.elapsed() < timeout`。循环体要点：

- 先 `list_indices()` 拿到全表索引清单——这一步**不能省**，因为一个新建索引在 `index_stats` 能查到统计之前，必须先在 `list_indices` 里「出现」（`waiter.rs:34-38`：清单里没有就 `debug!` 并 `continue`，继续等创建完成）。
- 再对每个 remaining 索引调 `index_stats(idx)`：`None` 表示统计还没就绪（`waiter.rs:42-45`），继续等；`Some(s)` 且 `num_unindexed_rows == 0` 才算完成（`waiter.rs:46-54`）。
- 每轮用 `remaining.retain(|idx| !completed.contains(idx))` 移除已完成的（`waiter.rs:64`）——这是一种「逐步收敛」的写法：列表越来越短，全部清空就提前 `return Ok(())`。
- 没收敛就 `sleep(1s)` 再来（`waiter.rs:68`）。

源码里有一条很诚实的注释值得注意：

[rust/lancedb/src/index/waiter.rs:48-49](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/waiter.rs#L48-L49) ——「`this may never stabilize under constant writes. we should later replace this with a status/job model`」。意思是：如果一边建索引一边不停写入新数据，`num_unindexed_rows` 可能永远降不到 0（新写的行不断补进来），导致等待永远不收敛。作者坦承当前轮询模型的局限，并指出未来应换成「任务/状态模型」。

最后是超时出口：

[rust/lancedb/src/index/waiter.rs:71-89](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/waiter.rs#L71-L89) —— 循环正常结束（到点）说明还有 `remaining` 没就绪：先对每个 remaining 索引记录一条 `debug!` 诊断日志（含最后的统计快照或「未找到」），再返回 `Error::Timeout { message: ... }`。返回 `Err` 而非 `Ok`，让调用者明确知道「没等到」。

#### 4.2.4 源码精读：谁来调用 `wait_for_index`

`wait_for_index` 是个自由函数，有三条调用路径：

**路径一：对外 `Table::wait_for_index`（用户手动等待）**。这是用户在远程场景或批量建索引后显式调用的入口：

[rust/lancedb/src/table.rs:1726-1734](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1726-L1734) —— `Table::wait_for_index`，一行转发到 `BaseTable` 契约。

本地与远程实现都直接调用自由函数：

[rust/lancedb/src/table.rs:3049-3055](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L3049-L3055) —— `NativeTable::wait_for_index` 把自己（`self` 作为 `&dyn BaseTable`）传进去。

[rust/lancedb/src/remote/table.rs:2166-2168](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L2166-L2168) —— `RemoteTable::wait_for_index` 同样调用自由函数。两边共享同一份轮询逻辑，再次印证「核心逻辑写一遍」的设计。

**路径二：`create_index` 末尾自动等待（仅远程）**。这是远程异步索引的核心用法——通过 `IndexBuilder::wait_timeout` 配置：

[rust/lancedb/src/index.rs:277-284](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs#L277-L284) —— `IndexBuilder::wait_timeout(d)`，文档明确：「**This is not supported for `NativeTable` since indexing is synchronous.**」即本地表设了也无效（索引本来就同步建完）。

远程表在 `create_index` 执行末尾检查这个字段：

[rust/lancedb/src/remote/table.rs:2156-2159](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L2156-L2159) —— `if let Some(wait_timeout) = index.wait_timeout`：若有，则推算出索引名（用户未指定时用默认 `{column}_idx`），调用 `wait_for_index` 阻塞到就绪。这样远程用户只要一行 `.wait_timeout(Duration::from_secs(60))` 就能让 `create_index` 在索引真正建好后才返回，使用体验与本地同步建索引一致。

**路径三：测试中的标准用法**。官方测试反复示范「建索引 → 立刻 wait」的模式：

[rust/lancedb/src/table/create_index.rs:509-512](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L509-L512) —— `table.wait_for_index(&["embeddings_idx"], Duration::from_secs(30)).await`，紧跟在 `create_index(...).execute()` 之后。即便本地表索引同步，这个 `wait_for_index` 也能立即返回（首轮 `num_unindexed_rows` 就已是 0），所以测试里也常用极短超时（如 `create_index.rs:632` 的 `Duration::from_millis(10)`）。

#### 4.2.5 源码精读：远程等待的两个超时场景测试

远程表的 mock 测试清晰地展示了 `wait_for_index` 的两种「等不到」结果，值得对照阅读：

[rust/lancedb/src/remote/table.rs:5321-5327](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L5321-L5327) —— `test_wait_for_index`：mock 一个「0 个未索引行」的端点，`wait_for_index` 正常 `unwrap()` 成功（首轮即收敛）。

[rust/lancedb/src/remote/table.rs:5329-5353](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L5329-L5353) —— 两个超时测试：`test_wait_for_index_timeout`（mock「100 个未索引行」，永远收敛不了）和 `test_wait_for_index_timeout_never_created`（请求一个根本不存在的索引名 `doesnt_exist_idx`）。两者都断言错误信息精确等于 `"Timeout error: timed out waiting for indices: [...] after 1s"`——这正是 `waiter.rs:83-88` 构造的 `Error::Timeout` 文本。注意：索引「存在但没建完」和「压根没出现」在最终错误上**无法区分**，都表现为 `Timeout`，区别只在 debug 日志里（前者有统计快照，后者是「not found」）。

#### 4.2.6 代码实践：观察等待与超时

1. **实践目标**：理解 `wait_for_index` 的两种返回路径——成功收敛与超时报错。
2. **操作步骤**：
   - 阅读上述三个远程测试（`remote/table.rs:5321-5353`），理解 mock 端点如何用 `unindexed_rows` 参数控制 `wait_for_index` 的走向。
   - 阅读 `_make_table_with_indices`（`remote/table.rs:5355` 起）看 mock 如何对 `/index/list/` 与 `/index/{name}/stats/` 两条路径分别返回不同 JSON。
3. **需要观察的现象**：成功用例首轮就返回；超时用例即使 `timeout` 设为 1 秒，也会因 `num_unindexed_rows` 恒为 100 而在约 1 秒后报 `Timeout error`。
4. **预期结果**：三个测试的断言文本与 `Error::Timeout` 的 `display` 完全一致。
5. **若要本地运行**：执行 `cargo test --quiet --features remote -p lancedb --test` 无法直接跑到这些（它们是 `remote/table.rs` 内的 `#[tokio::test]`），应改用 `cd rust/lancedb && cargo test --features remote wait_for_index`。运行命令的精确结果**待本地验证**。

#### 4.2.7 小练习与答案

**练习 1**：为什么 `wait_for_index` 每轮要先 `list_indices()`，而不是直接 `index_stats()`？

> **参考答案**：因为远程索引是异步创建的——HTTP 建索引请求返回后，索引可能还没在服务器端「注册」到索引清单。此时直接 `index_stats(name)` 会得到 `None`（统计还没准备好）。先 `list_indices` 是为了区分两种状态：索引「还没创建」（清单里看不到）和「已创建但统计未就绪」（清单里有但 stats 是 None）。两种情况都要继续等，但 `list_indices` 这一步让等待逻辑覆盖了「索引尚未出现」的初始阶段（`waiter.rs:34-38`）。

**练习 2**：`IndexBuilder::wait_timeout(Duration::from_secs(60))` 在本地表（`NativeTable`）上有什么效果？

> **参考答案**：无效果。源码文档明确说该选项「not supported for `NativeTable` since indexing is synchronous」（`index.rs:280-281`）。本地表索引是同步构建的，`create_index().execute()` 返回时索引已就绪，无需等待。该选项仅对远程表有意义，会在 `RemoteTable::create_index` 末尾触发 `wait_for_index`（`remote/table.rs:2156-2159`）。

**练习 3**：如果一张表在持续高频写入，同时用 `wait_for_index` 等某个索引，可能会发生什么？源码哪一处说明了这个风险？

> **参考答案**：可能永远等不到就绪（一直 `Timeout`）。因为持续写入会不断产生新的未索引行，`num_unindexed_rows` 可能始终降不到 0。源码 `waiter.rs:48-49` 的注释明确写道「this may never stabilize under constant writes」，并建议未来改用「status/job 模型」替代当前轮询。实践中应先停止写入、建好索引、再恢复写入，或配合 `optimize`（见 u5-l1）把未索引行折叠进索引。

## 5. 综合实践

把本讲的两条主线（索引统计信息、等待机制）串成一个完整任务。

**任务**：在一张表上建一个向量索引，完整走一遍「建 → 等 → 查统计 → 解读」流程。

1. 准备一张含向量列 `vector`（`FixedSizeList<Float32>`，维度 16）的表，写入约 2000 行数据（数量要足以让 IVF 索引真正训练）。
2. 用 `Index::IvfPq(IvfPqIndexBuilder::default().distance_type(DistanceType::L2))` 显式建索引（不依赖 `Auto`，以便确认度量）。**不设** `wait_timeout`。
3. 紧接着调用 `table.wait_for_index(&["vector_idx"], Duration::from_secs(60)).await`，确认本地表首轮即返回。
4. 调用 `table.index_stats("vector_idx").await?.unwrap()`，打印 `index_type`、`distance_type`、`num_indexed_rows`、`num_unindexed_rows`，验证：
   - `index_type == IvfPq`
   - `distance_type == Some(L2)`
   - `num_unindexed_rows == 0`（等待过，应已就绪）
5. 再调用 `table.list_indices().await?`，对照 `IndexConfig` 与 `IndexStatistics` 的字段差异——确认 `IndexConfig` 字段更全（有 `size_bytes`/`num_segments`/`index_uuid` 等），而 `IndexStatistics` 多了 `distance_type`。

**验证要点**：

- `index_stats` 与 `list_indices` 都能给出 `index_type` 和行数，但前者带 `distance_type`、后者带更丰富的元数据——印证 4.1.1 的「身份证 vs 体检报告」区分。
- 本地表即使不调 `wait_for_index`，索引也已就绪；但加上 `wait_for_index` 让代码在本地/远程两种后端下行为一致，是更可移植的写法。

**进阶（源码阅读型）**：如果你暂时无法编译运行，改为阅读 `_make_table_with_indices`（`remote/table.rs:5355`）及其三个测试，画出 mock 服务器对 `/index/list/` 与 `/index/{name}/stats/` 两条路径的返回 JSON，解释 `unindexed_rows` 参数如何分别驱动「成功」「超时」「从未创建」三种结果。

## 6. 本讲小结

- LanceDB 用两套接口回答「索引信息」：`list_indices()` 返回 `Vec<IndexConfig>`（「身份证」，字段最全，多字段在远程表为 `None`），`index_stats(name)` 返回 `Option<IndexStatistics>`（「体检报告」，含独有的 `distance_type`）。
- `IndexStatistics.distance_type` 仅向量索引有值（L2/Cosine/Dot/Hamming），标量索引为 `None`——这是判断索引是否为向量索引的实用信号。
- `IndexConfig` 很多字段是 `Option`，因为本地表能从 Lance 读到全部细节，远程表目前只通过 HTTP 透出有限信息；写代码必须当 `Option` 处理。
- `NativeTable::list_indices` 大部分字段是从 Lance `describe_indices` 直接搬运，只有 `num_unindexed_rows` 是 LanceDB 用 `total_rows.saturating_sub(num_indexed_rows)` 自己算出来的（防下溢）。
- 「等待索引就绪」采用轮询策略：自由函数 `wait_for_index` 反复 `list_indices` + `index_stats`，判据是 `num_unindexed_rows == 0`；每轮间隔 1 秒，最长等待 2 小时；超时返回 `Error::Timeout`，超时上限过大返回 `Error::InvalidInput`。
- `IndexBuilder::wait_timeout` 只对**远程异步索引**有意义（本地表索引同步）；远程 `create_index` 末尾会据此自动调用 `wait_for_index`，让远程建索引的体验贴近本地。当前轮询模型在持续写入下可能永不收敛，源码已标注这一局限。

## 7. 下一步学习建议

本讲完结「索引体系」单元（u4）。后续建议：

- **u5-l1 优化：压缩与索引重建**：本讲多次提到「未索引行可通过 `optimize` 折叠进索引」。下一单元第一讲深入 `optimize` 的 compaction 与索引更新机制，正好解答「写入后如何让老索引重新覆盖全部数据」。
- **u5-l3 版本管理与时间旅行**：`IndexConfig.index_version`、`created_at` 等字段与 Lance 的版本机制相关，可在那里理解索引随表版本如何演化。
- **u6-l3 远程后端**：本讲的 `IndexBuilder::wait_timeout` 与远程 `index_stats` 的 HTTP 路径，都在那里有完整的远程后端上下文（`RemoteDatabase`/`RemoteTable`、`ServerVersion` 能力探测、retry 重试）。
- **阅读源码提示**：今后看到 `index_stats`/`wait_for_index`，记得它们都接受 `&dyn BaseTable`，本地与远程共用同一份实现——这是「核心逻辑写一遍」设计的典型范例。
