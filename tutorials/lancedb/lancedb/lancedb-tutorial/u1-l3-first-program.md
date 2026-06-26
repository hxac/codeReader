# 第一个程序：连接、建表、搜索

## 1. 本讲目标

在 [u1-l1 项目总览](./u1-l1-project-overview.md) 画出全局地图、[u1-l2 仓库结构](./u1-l2-repo-build-run.md) 搞清构建运行之后，本讲终于要「跑起来」了。

学完本讲，你应该能够：

- 读懂并亲手运行 `rust/lancedb/examples/simple.rs` 这个官方最小示例；
- 串起一条完整的使用链路：`connect`（连接）→ `create_table`（建表）→ `create_index`（建索引）→ `query`/`nearest_to`（查询搜索）→ `delete`（删除）；
- 看懂 LanceDB 把「用户 API」分发到「核心模块」的整体结构，为后续深入每个模块打下基座。

本讲全程围绕 `simple.rs` 这一个文件展开，它不到 140 行，却被官方当作快速入门文档的代码来源（文件头注释里写着 "Snippets from this example are used in the quickstart documentation"）。

## 2. 前置知识

在开始前，请确认你已经具备（或暂时接受）以下几个概念。它们在 [u1-l1](./u1-l1-project-overview.md) 和 [u1-l2](./u1-l2-repo-build-run.md) 已经讲过，这里只做最小回顾：

- **LanceDB 的「一核多绑定」结构**：核心逻辑写在 Rust 的 `rust/lancedb`，Python/Node/Java 只是薄绑定。本讲只碰 Rust 核心。
- **基于 Arrow 的数据模型**：LanceDB 用 Apache Arrow 的 `RecordBatch` 表示一批数据。表里每一列都有明确类型；向量列约定写成 `FixedSizeList<Float32>`。
- **Builder（建造者）模式**：LanceDB 几乎所有「会真正执行 IO」的操作都不直接返回结果，而是先返回一个 Builder，你链式配置后再调 `.execute().await`。例如 `connect(uri).execute().await`、`create_table(...).execute().await`。这是本讲代码里反复出现的写法。
- **异步与 `#[tokio::main]`**：核心是异步的，所以示例的 `main` 用 `#[tokio::main] async fn main()` 标注，并在每个 `await` 点等待结果。

如果上面任何一点让你感到陌生，建议先回到对应讲义补一下，再继续往下读。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [rust/lancedb/examples/simple.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs) | 本讲主角。完整演示连接、建表、追加、建索引、搜索、删除、删表。 |
| [rust/lancedb/src/lib.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs) | crate 根。在这里 `pub use` 重新导出 `connect`、`Table`、`Result` 等顶层入口。 |
| [rust/lancedb/src/connection.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs) | 连接层。`connect`、`ConnectBuilder`、`Connection`，以及 `create_table`/`open_table`/`table_names`/`drop_table`。 |
| [rust/lancedb/src/table.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs) | 表抽象。`Table` 结构体，以及 `schema`/`count_rows`/`add`/`delete`/`create_index`/`query`。 |
| [rust/lancedb/src/query.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs) | 查询层。`Query`、`QueryBase`/`ExecutableQuery` 两个 trait、`nearest_to`。 |
| [rust/lancedb/src/index.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs) | 索引定义。`Index` 枚举（`Auto`、各种标量/向量索引）。 |
| [rust/lancedb/Cargo.toml](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/Cargo.toml) | 声明 `simple` 这个 example 不依赖任何可选 feature，可直接运行。 |

记忆口诀：`connection`（连接）管「数据库」，`table`（表）管「一张表」，`query`（查询）管「怎么读」，`index`（索引）管「怎么读得快」。本讲就是沿着这条线把它们串起来。

## 4. 核心概念与源码讲解

我们按 `simple.rs` 的执行顺序，把它拆成五个最小模块逐段精读。

### 4.1 连接数据库：connect 与 Connection

#### 4.1.1 概念说明

「连接（Connection）」是 LanceDB 里所有操作的入口。它本身**不存数据**，而是握着两个东西：

1. 一个 `Database` trait 对象（真正负责在存储上管理表集合的实现）；
2. 一个 `EmbeddingRegistry`（嵌入函数注册表，本讲先忽略，留到 u8 讲）。

对本地目录而言，连接就是「打开/创建一个目录作为数据库」，目录下每个子目录就是一张表。对云后端而言，连接则是「连到 LanceDB Cloud」。

`connect` 不直接返回 `Connection`，而是返回一个 `ConnectBuilder`——你可以继续追加配置（凭证、读一致性等），最后用 `.execute().await` 真正建立连接。这就是上节说的 Builder 模式。

#### 4.1.2 核心流程

```
connect(uri)                         // 返回 ConnectBuilder
  ├── 可选: .api_key()/.region()      // 云后端凭证
  ├── 可选: .storage_options(...)     // 对象存储凭证/配置
  └── .execute().await
        ├── uri 以 "db" 开头  → 远程 RemoteDatabase  ─┐
        ├── manifest_enabled  → 命名空间后端           ├─ 包成 Connection
        └── 其它(本地路径等)  → ListingDatabase      ─┘
```

关键点：`execute` 会根据 URI 前缀决定走哪条后端分支，但无论哪条，最终都包成同一个 `Connection` 类型返回给用户。这就是「本地/远程两套后端、同一套 API」的体现。

#### 4.1.3 源码精读

示例里连接就两行（[rust/lancedb/examples/simple.rs:24-27](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs#L24-L27)）：

```rust
let uri = "data/sample-lancedb";
let db = connect(uri).execute().await?;
```

`connect` 在 crate 根被重新导出（[rust/lancedb/src/lib.rs:337-338](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L337-L338)），它本身只是构造一个 `ConnectBuilder`（[rust/lancedb/src/connection.rs:986-988](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L986-L988)）：

```rust
pub fn connect(uri: &str) -> ConnectBuilder {
    ConnectBuilder::new(uri)
}
```

`ConnectBuilder::execute` 才是「根据 URI 分流」的地方（[rust/lancedb/src/connection.rs:955-978](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L955-L978)）：当 `uri` 以 `"db"` 开头走远程，`manifest_enabled` 为真走命名空间后端，否则走本地 `ListingDatabase`。

URI 支持哪些写法？连接请求结构体的文档列得很清楚（[rust/lancedb/src/connection.rs:617-621](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L617-L621)）：

- `/path/to/database`：本地文件系统目录；
- `s3://bucket/path/...` 或 `gs://bucket/path/...`：云对象存储；
- `db://dbname`：LanceDB Cloud。

`simple.rs` 用的是第一种，即本地目录 `data/sample-lancedb`。

返回的 `Connection` 长这样（[rust/lancedb/src/connection.rs:374-385](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L374-L385)）：

```rust
pub struct Connection {
    internal: Arc<dyn Database>,           // 真正的后端实现
    embedding_registry: Arc<dyn EmbeddingRegistry>,
}
```

它实现了 `Clone`，意味着你可以廉价地复制一份连接句柄到处传递。建立连接后，示例立刻列出了所有表名（此时应为空）（[rust/lancedb/examples/simple.rs:29-31](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs#L29-L31)）：

```rust
println!("{:?}", db.table_names().execute().await?);
```

`table_names` 同样返回一个 Builder，支持分页（`start_after`/`limit`）（[rust/lancedb/src/connection.rs:408-415](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L408-L415)）。

#### 4.1.4 代码实践

**实践目标**：直观感受「连接 = 一个目录句柄」。

**操作步骤**：

1. 在 `rust/lancedb` 目录下，新建一个临时 example（或直接在 `simple.rs` 的连接两行后加一行）：

   ```rust
   println!("连接的 URI 是: {}", db.uri());
   ```

2. 运行示例（见本讲综合实践的运行方式）。

**需要观察的现象**：终端会打印出你传入的 URI（如 `data/sample-lancedb`），并且 `table_names()` 在建表前打印空数组 `[]`，建表后变成包含表名的数组。

**预期结果**：`db.uri()` 返回的就是 `connect` 时传入的字符串；本地后端会真的在你运行目录下创建 `data/sample-lancedb/` 目录。具体输出形式待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `connect` 不直接写成 `pub async fn connect(uri) -> Connection`，而要先返回 Builder 再 `.execute()`？

> **参考答案**：因为连接有大量可选配置（云凭证、对象存储参数、读一致性间隔、自定义 session 等）。Builder 模式让这些配置都变成可链式调用的可选方法，避免一个函数带十几个参数，也方便将来新增配置项而不破坏调用方（这正符合 AGENTS.md 里「Rust API 设计：优先 Builder/options struct」的原则）。

**练习 2**：如果把 `uri` 改成 `db://my-db` 会发生什么？

> **参考答案**：`execute` 会走 `execute_remote` 分支，要求提供 `region` 和 `api_key`，否则返回 `Error::InvalidInput`（参见 [connection.rs:914-944](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L914-L944)）。而且远程功能依赖 `remote` feature；若没开启该 feature，连接云端的调用会直接报错提示需要启用 `remote`（参见 [connection.rs:946-952](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L946-L952)）。

### 4.2 建表与追加数据：create_table、add、schema

#### 4.2.1 概念说明

「表（Table）」是 LanceDB 里真正存数据的对象，是一组强类型行的集合（每行的类型由 Arrow `Schema` 定义）。建表时要给两样东西：**表名**和**首批数据**。这批数据的 Schema 就成了这张表的 Schema——LanceDB 不需要你提前声明列，数据本身即声明。

示例在 `create_table` 之后紧接着调了一次 `add`，这是「追加」：往已有表里再写一批行，表的行数会增加。

#### 4.2.2 核心流程

```
构造 RecordBatch(含 schema + 数据)   // create_some_records()
  │
  ▼
db.create_table(name, data)          // 返回 CreateTableBuilder
  └── .execute().await ──▶ Table     // 表已落盘，返回句柄
        │
        ▼
tbl.add(new_data)                    // 返回 AddDataBuilder
  └── .execute().await               // 追加完成
```

注意 `create_table` 接收的数据类型是泛型 `T: Scannable`（[rust/lancedb/src/connection.rs:423-435](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L423-L435)）。`Scannable` 是 LanceDB 对「可被当作数据源扫描」的东西的统一抽象——一个 `RecordBatch`、一个 `RecordBatchReader` 都满足它。这意味着你能用流式 reader 写入海量数据，而不必一次把全部数据塞进内存（细节留到 u2-l3 讲）。

#### 4.2.3 源码精读

先看数据是怎么造出来的（[rust/lancedb/examples/simple.rs:60-89](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs#L60-L89)），关键是这两列的 Schema 定义：

```rust
const DIM: usize = 128;
let schema = Arc::new(Schema::new(vec![
    Field::new("id", DataType::Int32, false),
    Field::new(
        "vector",
        DataType::FixedSizeList(
            Arc::new(Field::new("item", DataType::Float32, true)),
            DIM as i32,                  // 128 维
        ),
        true,
    ),
]));
```

这就是 LanceDB 约定的向量列写法：`FixedSizeList<Float32>`，长度固定为 128。LanceDB 会把所有「固定长度、元素为 Float32/Float16」的列自动当作向量列。标量列 `id` 则是普通 `Int32`。

建表与追加（[rust/lancedb/examples/simple.rs:91-107](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs#L91-L107)）：

```rust
let tbl = db
    .create_table("my_table", initial_data)
    .execute()
    .await
    .unwrap();
// ...
let new_data = create_some_records()?;
tbl.add(new_data).execute().await.unwrap();
```

`add` 定义在 `Table` 上（[rust/lancedb/src/table.rs:1023-1035](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1023-L1035)），同样接收 `T: Scannable`，返回 `AddDataBuilder`。

`Table` 结构体本身（[rust/lancedb/src/table.rs:724-732](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L724-L732)）把真正的实现藏在一个 `BaseTable` trait 对象后面：

```rust
pub struct Table {
    inner: Arc<dyn BaseTable>,          // 本地 NativeTable 或远程 RemoteTable
    database: Option<Arc<dyn Database>>,
    embedding_registry: Arc<dyn EmbeddingRegistry>,
}
```

`schema()` 和 `count_rows()` 是最常用的两个只读方法（[rust/lancedb/src/table.rs:939-951](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L939-L951)）：

```rust
pub async fn schema(&self) -> Result<SchemaRef> { self.inner.schema().await }
pub async fn count_rows(&self, filter: Option<String>) -> Result<usize> { ... }
```

它们都是把调用转发给 `inner`（`BaseTable` 的具体实现）。这种「`Table` 是门面、`BaseTable` 是实现」的分层，正是 u2-l2 要深入讲的内容。

#### 4.2.4 代码实践

**实践目标**：验证「首批数据的 Schema = 表的 Schema」，以及「add 会累加行数」。

**操作步骤**：

1. 在 `create_table` 返回 `tbl` 之后、`add` 之前，打印一次 schema 和行数：

   ```rust
   println!("建表后 schema: {:?}", tbl.schema().await?);
   println!("建表后行数: {}", tbl.count_rows(None).await?);
   ```

2. 在 `tbl.add(...).execute().await.unwrap();` 之后再打印一次行数。

**需要观察的现象**：第一次行数应为 `1000`（`create_some_records` 里 `TOTAL = 1000`），`add` 一次后应变为 `2000`。schema 打印应包含 `id: Int32` 和 `vector: FixedSizeList<Field("item", Float32>, 128)`。

**预期结果**：行数从 1000 变 2000；schema 与你构造 RecordBatch 时给的完全一致。具体打印格式待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果不写 `vector` 这一列，只写 `id`，建表还能成功吗？之后还能做向量搜索吗？

> **参考答案**：能成功——表只要有合法 Schema 就行，不强制要求向量列。但没有向量列，就无处比较向量距离，后续 `nearest_to` 会因找不到向量列而报错。LanceDB 的「向量列」是靠 `FixedSizeList<Float>` 类型自动识别的。

**练习 2**：为什么 `create_table` 的第二个参数是泛型 `T: Scannable` 而不是固定写成 `RecordBatch`？

> **参考答案**：为了让同一套 API 同时支持「一次性给一整块数据」（`RecordBatch`）和「流式逐批给数据」（`RecordBatchReader`）。后者对写入上 GB 数据至关重要：不必全装进内存。`Scannable` 这个统一抽象让 `create_table` 和 `add` 的实现不必为每种数据源各写一遍。

### 4.3 创建索引：Index 与 create_index

#### 4.3.1 概念说明

建索引是为了「读得快」。LanceDB 有两大类索引：

- **标量索引**（BTree、Bitmap 等）：加速按列值过滤（如 `id > 10`）。
- **向量索引**（IVF 家族、HNSW 等）：加速向量相似度搜索（近似最近邻，ANN）。

`simple.rs` 用的是 `Index::Auto`，让 LanceDB 自己根据列的类型挑索引：向量列（`FixedSizeList<Float32>`）→ 默认 IVF-PQ 向量索引；其它列 → 默认 BTree。这一点 crate 根文档写得很明确（[rust/lancedb/src/lib.rs:119-126](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L119-L126)）。

关于「索引是什么、IVF/HNSW 怎么工作」的深入原理，留到 u4 整个单元细讲；本讲只把它当作「让搜索变快的一步」即可。

#### 4.3.2 核心流程

```
table.create_index(&["vector"], Index::Auto)
  │  // IndexBuilder 持有：列名 + Index 变体
  └── .execute().await
        ├── 列是向量列 → 训练并写入 IVF-PQ 索引
        └── 列是标量列 → 写入 BTree 索引
```

`create_index` 第一个参数是列名切片 `&["vector"]`（可一次给多列），第二个是要建的 `Index`。

#### 4.3.3 源码精读

示例里建索引就一行（[rust/lancedb/examples/simple.rs:119-123](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs#L119-L123)）：

```rust
table.create_index(&["vector"], Index::Auto).execute().await
```

`Index` 是一个枚举，列出了所有内置索引类型（[rust/lancedb/src/index.rs:28-83](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs#L28-L83)），摘录如下：

```rust
pub enum Index {
    Auto,
    BTree(BTreeIndexBuilder),
    Bitmap(BitmapIndexBuilder),
    LabelList(LabelListIndexBuilder),
    Fm(FmIndexBuilder),
    FTS(FtsIndexBuilder),       // 全文检索
    IvfFlat(IvfFlatIndexBuilder),
    IvfPq(IvfPqIndexBuilder),
    IvfSq(IvfSqIndexBuilder),
    IvfRq(IvfRqIndexBuilder),
    IvfHnswPq(IvfHnswPqIndexBuilder),
    IvfHnswSq(IvfHnswSqIndexBuilder),
    IvfHnswFlat(IvfHnswFlatIndexBuilder),
}
```

每个变体都带一个 Builder（如 `IvfPqIndexBuilder`），用来精细调节索引参数。`Auto` 是唯一「不带 Builder、让系统自选」的变体，最适合初学者。

`Table::create_index` 把这两个参数交给 `IndexBuilder`（[rust/lancedb/src/table.rs:1162-1181](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1162-L1181)）：

```rust
pub fn create_index(&self, columns: &[impl AsRef<str>], index: Index) -> IndexBuilder {
    IndexBuilder::new(
        self.inner.clone(),
        columns.iter().map(|val| val.as_ref().to_string()).collect(),
        index,
    )
}
```

注意 `columns` 用了 `&[impl AsRef<str>]`——既能传 `&["vector"]` 这样的 `&[&str]`，也能传 `&[String]`。这是 AGENTS.md 里强调的「优先 `AsRef<T>`」设计，让调用更灵活。

#### 4.3.4 代码实践

**实践目标**：体会 `Index::Auto` 会根据列类型自动选型。

**操作步骤**：

1. 先不动 `create_index(&["vector"], Index::Auto)`，运行示例，确认能成功。
2. 阅读 [rust/lancedb/src/lib.rs:119-126](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L119-L126) 这段文档，确认「向量列 → IVF-PQ，其它列 → BTree」的规则。
3. （可选探索）把 `Index::Auto` 换成显式的 `Index::IvfPq(IvfPqIndexBuilder::default())`，再编译运行，对比行为是否一致。

**需要观察的现象**：两种写法都应成功建立索引，对随后的搜索没有可观察的功能差异（`Auto` 只是替你选了默认参数）。

**预期结果**：`create_index` 正常返回。索引到底建成了什么类型，需要靠 u4-l5 讲的 `index_stats` 才能精确看到，本讲先不展开——所以这里标注「待本地验证」确切类型。

#### 4.3.5 小练习与答案

**练习 1**：`Index::Auto` 为什么不也要带一个 Builder？

> **参考答案**：因为 `Auto` 的语义就是「我不知道该选什么，请你（LanceDB）按列类型给我默认配置」。带 Builder 是为了让用户「明确指定并微调」，二者目的相反，所以 `Auto` 是枚举里唯一不带 Builder 的变体。

**练习 2**：建索引这一步能省略吗？省略后还能搜索吗？

> **参考答案**：能省略，也仍然能搜索。`nearest_to` 在没有向量索引时会退化为「flat search（暴力搜索）」——对库里每个向量都算一次距离再排序（参见 [query.rs:840-843](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L840-L843)）。只是数据量大时会很慢，所以大表才需要建索引。

### 4.4 向量搜索：query、nearest_to、execute

#### 4.4.1 概念说明

查询是检索的主线，LanceDB 用两个 trait 把它组织得很清楚：

- **`QueryBase`**：凡是查询都能做的通用配置，如 `limit`（取前 N 条）、`only_if`（SQL 过滤）、`select`（选哪些列）。
- **`ExecutableQuery`**：能被「执行」的查询，核心方法 `execute()` 返回一个 Arrow `RecordBatch` 的**异步流**（stream）。

「普通查询」和「向量查询」是两类对象：`table.query()` 拿到普通查询 `Query`；调一次 `nearest_to(向量)` 后，它就「升级」成向量查询 `VectorQuery`。向量查询的结果会比普通查询多一列 `_distance`，表示该结果与查询向量的距离（越小越相似）。

#### 4.4.2 核心流程

```
table.query()                       // 普通查询 Query
  ├── QueryBase::limit(2)           // 通用配置
  ├── (可选) .only_if("id > 10")    // SQL 过滤
  └── .nearest_to(&[1.0; 128])?     // 升级为 VectorQuery（这里定 limit 默认 10）
        └── ExecutableQuery::execute().await
              └── SendableRecordBatchStream  // 流式结果
                    └── .try_collect::<Vec<_>>().await  // 收成 Vec<RecordBatch>
```

两点值得记：

1. `limit` 在向量查询里如果不设，默认是 `DEFAULT_TOP_K = 10`（[rust/lancedb/src/query.rs:36](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L36)）。
2. 结果是**流**而不是一次性 `Vec`，方便处理超大结果集；示例最后用 `try_collect` 把流收成 `Vec<RecordBatch>`。

#### 4.4.3 源码精读

示例的搜索函数（[rust/lancedb/examples/simple.rs:125-136](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs#L125-L136)）：

```rust
table
    .query()
    .limit(2)
    .nearest_to(&[1.0; 128])?
    .execute()
    .await?
    .try_collect::<Vec<_>>()
    .await
```

逐个对上号：

- `query()` 在 `Table` 上定义，返回一个全新的 `Query`（[rust/lancedb/src/table.rs:1374-1376](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1374-L1376)）：

  ```rust
  pub fn query(&self) -> Query { Query::new(self.inner.clone()) }
  ```

- `limit(2)` 来自 `QueryBase` trait（[rust/lancedb/src/query.rs:376-384](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L376-L384)），返回 `Self`（仍是 `Query`），所以能继续链式调用。
- `nearest_to` 是 `Query` 上的方法（[rust/lancedb/src/query.rs:858-868](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L858-L868)）：它内部把 `Query` 转成 `VectorQuery`，并把查询向量塞进去；如果之前没设 `limit`，就补上默认的 10。因为可能转换失败，它返回 `Result<VectorQuery>`，所以示例里有 `?`。
- `execute()` 来自 `ExecutableQuery` trait（[rust/lancedb/src/query.rs:643-648](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L643-L648)），返回 `SendableRecordBatchStream`。

`QueryBase` 里那个文档反复强调的「最佳实践」值得记住——`select` 选列（[rust/lancedb/src/query.rs:449-471](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L449-L471)）：因为 LanceDB 是列式存储，只取需要的列能显著降低延迟。本讲的示例偷懒没选列，真实项目里建议加上。

关于结果里的 `_distance`：它是向量查询自动附加的「打分列」之一（源码里把 `_score`、`_distance` 称作 scoring columns，[rust/lancedb/src/query.rs:760](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L760)）。默认距离度量是 `L2`（欧氏距离），这在 crate 根的 `DistanceType` 枚举里有定义（[rust/lancedb/src/lib.rs:199-226](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L199-L226)），`L2` 是 `#[default]`。

#### 4.4.4 代码实践

**实践目标**：直观看到向量搜索「返回最近邻 + `_distance` 列」。

**操作步骤**：

1. 把示例的 `limit(2)` 改成 `limit(5)`。
2. 在 `.execute().await?` 之后、`try_collect` 之前（或收集完之后），把每个 batch 的 `_distance` 列打印出来：

   ```rust
   // 收集成 Vec<RecordBatch> 后
   for batch in &batches {
       if let Some(dist) = batch.column_by_name("_distance") {
           println!("distance 列: {}", dist);
       }
   }
   ```

**需要观察的现象**：示例里所有向量都是 `[1.0; 128]`，查询向量也是 `[1.0; 128]`，所以每条结果的 L2 距离都应是 `0.0`，结果按距离升序排列。

**预期结果**：打印出若干个 `0.0`，行数等于 `limit`。若你把数据改成不同向量，距离就会有差异，并按升序返回。具体数值待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`table.query().limit(2).nearest_to(...)` 中，`limit(2)` 和 `nearest_to` 的顺序能换吗？

> **参考答案**：能换。`limit` 是 `QueryBase` 提供的，`Query` 和 `VectorQuery` 都实现了 `QueryBase`，所以 `nearest_to(...).limit(2)` 也合法。但要注意：`nearest_to` 返回的是 `Result<VectorQuery>`，要先用 `?` 解开。无论哪种顺序，`limit` 最终都作用在向量查询上（若你两次都没设 limit，`nearest_to` 内部会补上默认 10）。

**练习 2**：为什么 `execute()` 返回的是「流」而不是直接 `Vec<RecordBatch>`？

> **参考答案**：因为一次向量搜索的结果可能极大（成千上万行），一次性收成 `Vec` 会占用大量内存。流式返回让调用方可以边读边处理、施加背压，把单次查询的内存用量控制在合理范围（这一点在 `ExecutableQuery::execute_with_options` 的文档注释里有说明，[query.rs:650-663](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L650-L663)）。示例因为数据小，才用 `try_collect` 收成 Vec。

### 4.5 删除与表的生命周期收尾：delete、drop_table

#### 4.5.1 概念说明

数据库的基本生命周期离不开「删」。LanceDB 区分两个粒度：

- **`delete`（删行）**：在**表内**按条件删除匹配的行。这是行级操作。
- **`drop_table`（删表）**：把**整张表**从数据库里移除。

`delete` 接收一个**谓词（predicate）**——一段 SQL 字符串（如 `"id > 24"`）或一个 DataFusion 表达式，所有满足谓词的行都会被删除。在底层 Lance 格式里，删除通常是「逻辑删除」（标记删除）而非立即物理擦除，物理回收发生在 `optimize` 压缩阶段（u5-l1 讲）。

#### 4.5.2 核心流程

```
tbl.delete("id > 24").await          // 行级删除：删 id>24 的行
db.drop_table("my_table", &[]).await // 删整张表（第 2 个参数是命名空间路径）
```

`drop_table` 的第二个参数是「命名空间路径」`&[String]`，空切片 `&[]` 表示根命名空间。本讲只碰本地根目录，所以一律传 `&[]`；命名空间机制留到 u6-l2 讲。

#### 4.5.3 源码精读

示例收尾（[rust/lancedb/examples/simple.rs:39-45](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs#L39-L45)）：

```rust
tbl.delete("id > 24").await.unwrap();
// ...
db.drop_table("my_table", &[]).await.unwrap();
```

`Table::delete` 把谓词转发给底层实现（[rust/lancedb/src/table.rs:1107-1109](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1107-L1109)）：

```rust
pub async fn delete(&self, predicate: impl Into<Predicate<'_>>) -> Result<DeleteResult> {
    self.inner.delete(predicate.into()).await
}
```

注意参数类型 `impl Into<Predicate<'_>>`——它既接受 `&str`（SQL 字符串），也接受 `&Expr`（DataFusion 表达式），灵活兼顾。表上的文档注释（[table.rs:1055-1106](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1055-L1106)）就同时给了 SQL 字符串和 DataFusion 表达式两种删除写法的例子。

`Connection::drop_table`（[rust/lancedb/src/connection.rs:516-525](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L516-L525)）：

```rust
pub async fn drop_table(&self, name: impl AsRef<str>, namespace_path: &[String]) -> Result<()> {
    self.internal.drop_table(name.as_ref(), namespace_path).await
}
```

它转发给后端 `Database` 的 `drop_table`。对本地后端，删一个不存在的表会返回 `Error::TableNotFound`（这一点在连接层测试 [connection.rs:1498-1525](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L1498-L1525) 有断言）。

最后提醒：`main` 开头有一段「如果 `data` 目录存在就先删掉」（[simple.rs:21-23](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs#L21-L23)），保证示例可重复运行、不残留旧数据。

#### 4.5.4 代码实践

**实践目标**：用 `count_rows` 量化验证 `delete` 的效果。

**操作步骤**：

1. 在 `tbl.delete("id > 24").await.unwrap();` 前后各打印一次行数：

   ```rust
   println!("删除前行数: {}", tbl.count_rows(None).await.unwrap());
   tbl.delete("id > 24").await.unwrap();
   println!("删除行数(id>24) 后剩余: {}", tbl.count_rows(None).await.unwrap());
   ```

**需要观察的现象**：示例数据是 `create_some_records` 调了两次（`create_table` 一次 + `add` 一次），每次 `id` 取值都是 `0..1000`。所以 `id > 24` 命中 `id ∈ [25, 999]`，共 975 个值、出现两次，删除 1950 行；剩余 `id ∈ [0, 24]`，共 25 个值、出现两次，剩余 50 行。

**预期结果**：行数从 2000 降到 50。如果你改了 `TOTAL` 或 `DIM`，请重新计算。具体数字待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：`delete` 之后磁盘空间会立刻释放吗？

> **参考答案**：通常不会立刻释放。Lance 格式多采用「逻辑删除」（在删除文件里记录被删行），原数据文件仍在。要真正回收空间、合并碎片，需要 `optimize`（压缩）操作，这部分留到 u5-l1 讲。这也是 LanceDB 支持时间旅行（读旧版本）的基础。

**练习 2**：`delete` 和 `drop_table` 报错语义有何不同？

> **参考答案**：对本地后端，对一个**不存在的表**调用 `drop_table` 会返回 `Error::TableNotFound`（参见 [connection.rs:1498-1525](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L1498-L1525) 的测试）；而 `delete` 作用在已打开的表上，若谓词语法错误则会返回 SQL/表达式解析相关的错误。二者粒度不同：一个是表级、一个是行级。

## 5. 综合实践

把本讲所有环节串成一个可运行的任务——这正是本讲规格里指定的实践任务。

**任务**：复制 `simple.rs`，在 `create_table` 之后新增一步调用 `table.schema()` 打印 schema，运行并对照观察输出。

**操作步骤**：

1. **准备**：进入 `rust/lancedb` 目录（示例与 Cargo.toml 的 `[[example]]` 声明在一起，[Cargo.toml:158-159](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/Cargo.toml#L158-L159)，该 example 无 `required-features`，无需额外 feature）。
2. **复制**：把 `rust/lancedb/examples/simple.rs` 复制一份，例如 `rust/lancedb/examples/simple_with_schema.rs`，并把函数 `main` 里的 example 名义改一改（仅在文件头注释或目录上区分即可，不必改 `[[example]]`，因为 examples 目录会被自动发现）。

   > 注意：本实践由你在自己的学习副本里完成，**不要真的改 `simple.rs`**（讲义禁止修改源码）。复制成新文件即可。

3. **加代码**：在 `create_table` 函数里 `create_table(...).execute().await` 之后、`add` 之前，插入打印 schema：

   ```rust
   let tbl = db
       .create_table("my_table", initial_data)
       .execute()
       .await
       .unwrap();

   // —— 新增：打印建表后的 schema ——
   println!("schema = {:?}", tbl.schema().await.unwrap());
   ```

4. **运行**：在仓库根目录执行：

   ```bash
   cargo run --quiet --manifest-path rust/lancedb/Cargo.toml --example simple_with_schema
   ```

   > 因为 `simple` 这类 example 不依赖可选 feature，默认 `default = []` 就能编译；若你的环境中首次编译较慢，属正常现象。

**需要观察的现象**：

- 终端先打印 `[]`（建表前的 `table_names()`）；
- 然后打印 `schema = Schema(...)`，里面应能看到 `id: Int32` 和 `vector: FixedSizeList<item: Float32>[128]`；
- 随后打印搜索结果 `batches`（含 `_distance`）；
- 最后 `data/sample-lancedb` 目录被新建并在运行结束时保留下来（删表只是删了表，目录还在）。

**预期结果**：schema 打印与你在 `create_some_records` 里构造的 `Schema` 完全一致，证明「首批数据的 Schema 就是表的 Schema」。搜索结果因所有向量相同，`_distance` 全为 0。一切跑通即说明你已经掌握了 connect → create_table → create_index → query → delete 的完整链路。

> 若运行失败，最常见原因是 `data` 目录残留——`simple.rs` 开头有清理逻辑，你的副本里若改名了表，注意 `drop_table` 的表名要对应。无法确定时标注「待本地验证」。

## 6. 本讲小结

- LanceDB 的 API 几乎都遵循 **Builder 模式**：`connect/create_table/add/create_index/query` 都先返回 Builder，`.execute().await` 才真正执行 IO。
- **连接（Connection）** 是入口，内部根据 URI（本地路径 / `s3://` / `db://`）分流到 `ListingDatabase` 或 `RemoteDatabase`，但对外统一为 `Connection`。
- **建表** 时「首批数据的 Schema 即表的 Schema」；向量列用 `FixedSizeList<Float32>` 表示；`create_table`/`add` 都接收 `Scannable` 泛型，既支持整块也支持流式数据。
- **索引（Index）** 分标量与向量两大类；`Index::Auto` 会按列类型自动选型（向量列 → IVF-PQ，其它 → BTree）。没有索引也能搜索，只是退化为暴力搜索。
- **查询** 用 `QueryBase`（通用配置）+ `ExecutableQuery`（执行）两个 trait 组织；`query()` 得到普通查询，`nearest_to()` 把它升级为向量查询，结果带 `_distance` 列、以流式返回。
- **删除** 区分 `delete`（行级，按 SQL 谓词，通常为逻辑删除）与 `drop_table`（表级）。

## 7. 下一步学习建议

本讲只是「跑通了最小链路」，每个环节都还有很多没展开。建议按以下顺序继续：

1. **u2-l1 连接 Connection 与 ConnectBuilder**：深入 `ConnectBuilder` 的 `storage_options`、`api_key`、`read_consistency_interval` 等配置方法，搞清本地与云端连接的差异。
2. **u2-l2 Table 抽象层**：本讲把 `Table` 当黑盒用了，下一讲打开它，看清 `Table`、`BaseTable`、`NativeTable` 的三层职责与委托关系。
3. **u2-l3 数据写入与 Scannable**：本讲只用了 `RecordBatch`，下一步学习如何用 `RecordBatchReader` 流式写入大数据，以及如何自定义 `Scannable`。
4. **u3 查询与搜索**：本讲只用了 `nearest_to`，整个 u3 会把 SQL 过滤、距离度量、全文检索、混合搜索（RRF）连成一条完整的检索链路。

在进入下一讲前，强烈建议你先把综合实践跑通——亲手看到那行 `schema = ...` 打印出来，比读十遍讲义都管用。
