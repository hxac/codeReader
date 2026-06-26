# 索引总览与 Index 枚举

## 1. 本讲目标

本讲是「索引体系」单元（u4）的起点，承接 u2-l2（Table 三层抽象）。

学完本讲你应该能够：

- 说清楚 LanceDB 里「索引」到底解决了什么问题，以及标量索引、向量索引、全文索引三大类的区别。
- 读懂 [`Index`](#) 枚举的所有变体，并能解释「构建用的 `Index`」「描述用的 `crate::index::IndexType`」「底层 `lance_index::IndexType`」三者为何不同。
- 完整复述 `create_index` 的 Builder 调用链：从 `Table::create_index` 一路到 `NativeTable` 把「配方」翻译成 Lance 引擎能执行的参数。
- 理解 `Index::Auto` 如何根据列的数据类型自动选型（向量列 → IvfPq，标量列 → BTree）。

本讲只覆盖最小模块 `index` 与 `table (create_index)`。具体的标量索引细节（BTree/Bitmap/LabelList/Fm）留给 u4-l2，向量索引（IVF/HNSW/量化）留给 u4-l3、u4-l4，索引统计与等待机制留给 u4-l5。

## 2. 前置知识

### 2.1 为什么需要索引

你已经在 u3 系列里学会了对一张表做扫描、过滤、向量搜索。但如果没有索引，每一次查询都要把相关数据从头扫到尾：

- 标量过滤 `WHERE category = 'A'` 要遍历每一行判断；
- 向量搜索 `nearest_to(q)` 要计算查询向量与每一行向量的距离（这种「暴力搜索」叫 **flat search**，见 u3-l2 提到的 `KNNFlatSearch`）。

当表里有上千万、上亿行时，全表扫描根本不可行。**索引（index）就是一种预先算好、持久化在磁盘上的辅助数据结构**，让查询只读必要的少量数据。

### 2.2 三大类索引

| 大类 | 加速的查询 | 是否精确 | 典型数据类型 |
| --- | --- | --- | --- |
| 标量索引（Scalar） | `=` / `>` / `<` / 范围 / `contains` 子串 | 精确（exact） | 数值、字符串、布尔、时间 |
| 向量索引（Vector） | 相似度搜索 `nearest_to` | 近似（approximate，ANN） | `FixedSizeList<Float>` |
| 全文索引（FTS） | 关键词检索 `full_text_search` | 排序打分（BM25） | 字符串 |

一个关键认知：**标量索引是「精确」的**——它只改变查询速度，不改变结果；**向量索引是「近似」的**——它用很小的精度损失换取巨大的速度提升，召回率（recall）往往不是 100%。这决定了后面整单元讨论的核心矛盾：速度 vs 召回。

### 2.3 两个需要先认识的术语

- **基数（cardinality）**：一列里不同取值的个数。性别列基数约为 2（低基数），用户 ID 列基数等于行数（高基数）。基数会直接影响标量索引类型的选择。
- **量化（quantization）**：把高维浮点向量压缩成更紧凑表示的技术（PQ/SQ/RQ），是向量索引省内存的关键。本讲只提名字，原理留到 u4-l4。

## 3. 本讲源码地图

本讲涉及的核心文件：

| 文件 | 作用 |
| --- | --- |
| [`rust/lancedb/src/index.rs`](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs) | 定义 `Index` 枚举（用户的索引「配方」）、`IndexBuilder`、描述用 `IndexType`、`IndexConfig`、`IndexStatistics`。 |
| [`rust/lancedb/src/table/create_index.rs`](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs) | `NativeTable` 的索引创建实现：把 `Index` 翻译成 Lance 参数、校验列类型、解析字段路径，并实现 `Index::Auto` 选型。 |
| [`rust/lancedb/src/index/scalar.rs`](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/scalar.rs) | 四种标量索引的 Builder（目前都是无参数结构体）。 |
| [`rust/lancedb/src/index/vector.rs`](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs) | 各类向量索引的 Builder（用宏统一注入 `distance_type`/IVF/PQ/HNSW 参数）。 |
| [`rust/lancedb/src/utils/mod.rs`](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/utils/mod.rs) | `supported_*_data_type` 一组判断函数，决定某数据类型能建哪种索引——这是 `Index::Auto` 选型的依据。 |
| [`rust/lancedb/src/table.rs`](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs) | `Table::create_index`（对外入口）、`BaseTable::create_index`（契约）、`NativeTable::create_index`（本地实现）。 |

## 4. 核心概念与源码讲解

### 4.1 索引的三大类与「构建 vs 描述」的区分

#### 4.1.1 概念说明

在 LanceDB 里，你会反复遇到三个名字相似、但含义完全不同的类型。先把它们分清楚，后面所有源码都会豁然开朗：

1. **`Index`（构建配方）**：**用户创建索引时传入**的枚举。它描述「我想建一个什么样的索引」，里面带着各种 Builder 参数。它有一个特殊变体 `Auto`，表示「让 LanceDB 自己选」。

2. **`crate::index::IndexType`（描述类型）**：**描述表中已存在索引**的枚举。它不带参数，变体是 `IvfFlat` / `BTree` / `Bitmap` …，用于 `IndexConfig`、`IndexStatistics` 这类「查询已有索引信息」的返回值，也支持 `Display`/`FromStr` 序列化。

3. **`lance_index::IndexType`（底层大类）**：底层 Lance 引擎用的「大类」标识，只有 `Vector` / `BTree` / `Bitmap` / `LabelList` / `Fm` / `Inverted` 等粗粒度变体。`create_index` 流程内部需要把它连同具体参数一起交给 Lance。

可以这样理解它们的关系：

```text
用户视角        : Index (配方，含 Auto) ──create_index──▶ LanceDB 翻译
翻译过程内部    : lance_index::IndexType (大类) + IndexParams (具体参数) ──▶ Lance 引擎
事后查询视角    : crate::index::IndexType (精确类型) ──list_indices/index_stats──▶ 给用户看
```

> 提示：本讲的源码引用里，`create_index.rs` 顶部 `use lance_index::IndexType;`（底层那个），而 `index.rs` 里定义的是 `crate::index::IndexType`（描述那个）。看代码时注意 `use` 了哪一个，才不会混淆。

#### 4.1.2 核心流程

创建一个索引的宏观流程（先建立直觉，4.3 再逐行精读）：

```text
Table::create_index(&["col"], Index::xxx)        // 1. 用户入口，返回 IndexBuilder（只配置）
        │
        ▼
IndexBuilder.execute().await                     // 2. 才真正触发 IO
        │  self.parent.create_index(self).await  （parent = Arc<dyn BaseTable>）
        ▼
BaseTable::create_index (trait 契约)             // 3. 统一契约，本地/远程都实现
        │
        ▼ (本地分支)
NativeTable::create_index                         // 4. 翻译为 Lance 调用
   ├─ resolve_index_field  : 列名 → (规范路径, Field)
   ├─ make_index_params    : Index  → lance IndexParams（含 Auto 选型）
   ├─ get_index_type_for_field : Index → lance_index::IndexType (大类)
   └─ dataset.create_index_builder(...).train().replace().await  // 5. 交给 Lance
```

这条链路里，LanceDB 核心「真正干活」的部分很薄：它主要是把面向用户的 `Index` 枚举，翻译成底层 Lance 引擎认识的参数对象，真正的索引构建算法（IVF 划分、PQ 训练、BTree 排序等）都在 Lance / lance-index 里实现。这与 u1-l1 说的「LanceDB 是 Lance 的薄壳」完全一致。

### 4.2 `Index` 枚举全景

#### 4.2.1 概念说明

`Index` 是用户最常打交道的类型。它把 LanceDB 支持的全部索引种类收拢在一个枚举里，分成三大类：`Auto`、标量、向量。每个非 `Auto` 变体都携带一个对应的 `Builder`，用来配置该索引的参数。

#### 4.2.2 变体分类

完整定义见源码：

[rust/lancedb/src/index.rs:28-83](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs#L28-L83) —— `Index` 枚举的完整定义，列出所有变体。

下表按三大类整理（标量类留待 u4-l2、向量类留待 u4-l3/u4-l4 详讲，本讲只建立全景）：

| 类别 | 变体 | 携带的 Builder | 一句话用途 |
| --- | --- | --- | --- |
| 自动 | `Auto` | 无 | 按列类型自动选型（见 4.4） |
| 标量 | `BTree` | `BTreeIndexBuilder` | 排序索引，高基数、高选择性查询；**标量列默认** |
| 标量 | `Bitmap` | `BitmapIndexBuilder` | 低基数列（几百个唯一值以内） |
| 标量 | `LabelList` | `LabelListIndexBuilder` | `List<T>` 数组列，支持 `array_contains_*` |
| 标量 | `Fm` | `FmIndexBuilder` | 字符串/二进制列的**子串**搜索 `contains` |
| 全文 | `FTS` | `FtsIndexBuilder` | 基于 BM25 的全文检索（见 u3-l5） |
| 向量 | `IvfFlat` | `IvfFlatIndexBuilder` | IVF 划分，存原始向量，召回最高 |
| 向量 | `IvfPq` | `IvfPqIndexBuilder` | IVF + 乘积量化，省内存 |
| 向量 | `IvfSq` | `IvfSqIndexBuilder` | IVF + 标量量化 |
| 向量 | `IvfRq` | `IvfRqIndexBuilder` | IVF + RabitQ 量化 |
| 向量 | `IvfHnswPq` | `IvfHnswPqIndexBuilder` | IVF + HNSW 图 + PQ |
| 向量 | `IvfHnswSq` | `IvfHnswSqIndexBuilder` | IVF + HNSW 图 + SQ |
| 向量 | `IvfHnswFlat` | `IvfHnswFlatIndexBuilder` | IVF + HNSW 图，存原始向量 |

几个值得注意的点：

- **`Auto` 不携带任何 Builder**：它是一个纯标记，真正的参数由 `make_index_params` 在运行时根据列类型补齐。
- **标量 Builder 目前都是无参数结构体**：例如 `BTreeIndexBuilder {}`、`BitmapIndexBuilder {}`（见 4.2.3）。它们存在主要是为了将来可扩展，以及让 `Index` 枚举形态统一。
- **`FTS` 的 Builder 是直接复用底层的**：`pub use lance_index::scalar::InvertedIndexParams as FtsIndexBuilder;`。这与 u3-l5 讲过的「LanceDB 核心不实现 BM25，只透传底层倒排索引」一脉相承。

#### 4.2.3 源码精读：标量 Builder 与向量 Builder 的形态差异

先看标量 Builder，它们极其简单：

[rust/lancedb/src/index/scalar.rs:30-61](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/scalar.rs#L30-L61) —— 四种标量 Builder 都是无字段的空结构体（`BTreeIndexBuilder {}` 等），`FtsIndexBuilder` 则是 `pub use` 自底层 `InvertedIndexParams`。

向量 Builder 则相反，字段丰富。以最简单的 `IvfFlatIndexBuilder` 为例：

[rust/lancedb/src/index/vector.rs:180-209](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index/vector.rs#L180-L209) —— `IvfFlatIndexBuilder` 持有 `distance_type`（默认 `L2`）、`num_partitions`、`sample_rate`（默认 256）、`max_iterations`（默认 50）、`target_partition_size`。

注意 `Default` 实现里 `distance_type: DistanceType::L2`——这呼应了 u3-l3 讲过的「默认度量是 L2」。向量 Builder 的链式配置方法（`distance_type`/`num_partitions`/`sample_rate`…）并非手写，而是用宏批量注入（`impl_distance_type_setter!` / `impl_ivf_params_setter!` 等，见 `vector.rs:42-167`），这样七个向量 Builder 就能共享同一套参数语义。

向量索引有一个贯穿全单元的关键默认值：分区数。源码注释说明默认取行数的平方根：

\[ \text{num\_partitions}_{\text{default}} \approx \sqrt{N} \quad (N \text{ 为表的行数}) \]

直觉是：分区太少，每个分区太大、搜索慢；分区太多，选分区这一步本身就慢。平方根是一个经验性的平衡点（见 `vector.rs:66-74` 的 `num_partitions` 文档注释）。

#### 4.2.4 源码精读：描述用的 `IndexType`

`Index` 是「我要建什么」，`IndexType` 是「这个已存在的索引是什么」。后者用在查询索引信息的返回值里：

[rust/lancedb/src/index.rs:291-320](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs#L291-L320) —— `crate::index::IndexType` 枚举，变体是 `IvfFlat`/`IvfPq`/`BTree`/`Bitmap`/`LabelList`/`Fm`/`FTS` 等，每个变体带 `#[serde(alias = "...")]` 支持大写别名的反序列化。

它还实现了 `Display` 与 `FromStr`：

[rust/lancedb/src/index.rs:341-363](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs#L341-L363) —— `FromStr` 实现：把字符串（大小写不敏感）解析成 `IndexType`，无法识别时返回 `Error::InvalidInput`。这正是 `list_indices` 用来把底层返回的字符串类型还原成枚举的依据（见 `table.rs:2933` 的 `idx_desc.index_type().parse()`）。

> 关键区分回顾：`crate::index::IndexType`（这里，描述用，变体精细到 `IvfPq`）≠ `lance_index::IndexType`（底层，大类，变体是 `Vector`/`BTree`…）。`list_indices` 返回前者；`create_index` 内部交给 Lance 的是后者。

#### 4.2.5 小练习与答案

**练习 1**：为什么标量索引的 Builder（如 `BTreeIndexBuilder`）几乎是空结构体，而向量索引的 Builder 字段很多？

> **参考答案**：标量索引（BTree/Bitmap）的算法固定、参数少（BTree 目前没有可调参数），所以 Builder 几乎为空，更多是为了将来扩展和保持枚举形态统一；向量索引涉及 IVF 分区数、量化参数（PQ 子向量数/比特数）、HNSW 图参数（m/ef_construction）、采样率/迭代次数等大量可调超参，这些超参直接决定召回率和速度，因此需要丰富的 Builder 字段。

**练习 2**：`Index::FTS(FtsIndexBuilder)` 里的 `FtsIndexBuilder` 是 LanceDB 自己实现的吗？

> **参考答案**：不是。`scalar.rs:64` 通过 `pub use lance_index::scalar::InvertedIndexParams as FtsIndexBuilder;` 直接复用底层 lance-index 的倒排索引参数类型。LanceDB 核心不实现 BM25，只做命名上的重新导出与透传（与 u3-l5 一致）。

### 4.3 `create_index` 的 Builder 流程

#### 4.3.1 概念说明

和 u2-l1 讲连接、u3 讲查询一样，创建索引也严格遵循 LanceDB 贯穿的「**Builder + execute**」风格：`create_index(...)` 只返回一个配置对象，真正写盘发生在 `.execute().await`。这样做的好处是配置过程零 IO、可链式拼接、错误延迟到执行时才暴露。

#### 4.3.2 核心流程

完整调用链分为五层：

```text
① Table::create_index(&["col"], Index::Auto)
      └─ IndexBuilder::new(inner, columns, index)        // 只存配置
② IndexBuilder.execute().await
      └─ parent.create_index(self).await                 // parent: Arc<dyn BaseTable>
③ BaseTable::create_index(&self, index: IndexBuilder)    // trait 契约
④ NativeTable::create_index(opts)                         // 本地实现
      ├─ 校验 columns.len() == 1（暂不支持复合索引）
      ├─ resolve_index_field(...)  → (canonical_path, Field)
      ├─ make_index_params(field, opts.index)             // Index → lance IndexParams
      ├─ get_index_type_for_field(field, &opts.index)     // Index → lance_index::IndexType
      └─ dataset.create_index_builder(...).train().replace().name().await
⑤ Lance 引擎真正构建索引并落盘
```

#### 4.3.3 源码精读

**① 对外入口 `Table::create_index`**：把任意 `AsRef<str>` 的列名统一收成 `Vec<String>`，再交给 `IndexBuilder::new`。

[rust/lancedb/src/table.rs:1172-1181](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1172-L1181) —— `Table::create_index` 仅做参数收集与 `IndexBuilder` 构造，不触发任何 IO。

注意它接收 `columns: &[impl AsRef<str>]`——这是 CLAUDE.md「Rust API 设计」里推荐的 `AsRef<T>` 用法，让 `&["col"]`、`&[String]`、`&[&str]` 都能直接传入。

**② `IndexBuilder` 的字段与 execute**：

[rust/lancedb/src/index.rs:168-176](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs#L168-L176) —— `IndexBuilder` 持有 `parent: Arc<dyn BaseTable>`、`index`、`columns`、`replace`、`wait_timeout`、`train`、`name`。

[rust/lancedb/src/index.rs:178-189](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs#L178-L189) —— `IndexBuilder::new` 的默认值：`replace = true`、`train = true`。也就是说，默认情况下会**覆盖同名旧索引**、并**用现有数据训练**索引。

它的链式方法都只改字段、返回 `Self`：`replace(bool)`（196-199）、`name(String)`（221-224）、`train(bool)`（272-275）、`wait_timeout(Duration)`（281-284）。真正执行的一行是：

[rust/lancedb/src/index.rs:286-288](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/index.rs#L286-L288) —— `execute` 把整个 builder 委托给 `parent.create_index(self).await`，即 `BaseTable` trait 对象。这与 u2-l2 讲的「Table 持有 `Arc<dyn BaseTable>`，方法几乎都是一行转发」完全吻合。

几个配置项的含义（来自源码文档注释）：

- `replace(true)`（默认）：若同列同名索引已存在则覆盖；设为 `false` 则已存在时报错。
- `train(true)`（默认）：用现有数据训练索引；设为 `false` 则只建一个空索引，稍后再填充。**注意：向量索引暂不支持 `train(false)`**（见 `index.rs:226-231` 注释）。
- `wait_timeout`：仅远程表有意义（远程索引异步构建），本地表索引是同步的。

**③ 契约层 `BaseTable::create_index`**：

[rust/lancedb/src/table.rs:527](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L527) —— trait 方法签名 `async fn create_index(&self, index: IndexBuilder) -> Result<()>;`。本地 `NativeTable` 与远程 `RemoteTable` 各自实现，对外接口一致。

**④ 本地实现 `NativeTable::create_index`**：

[rust/lancedb/src/table.rs:2755-2779](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L2755-L2779) —— 这是整个流程的「翻译核心」。逐行解读：

1. `opts.columns.len() != 1` → 报 `Error::Schema`：**目前只支持单列索引，复合（多列）索引尚未支持**。
2. `resolve_index_field(dataset.schema(), &opts.columns[0])` → 得到 `(canonical_path, field)`。它做大小写不敏感的列名解析，并支持嵌套字段路径（如 `metadata.user_id`，见 4.3.4 的测试）。
3. `make_index_params(&field, opts.index)` → 把用户的 `Index` 翻译成 Lance 的 `Box<dyn IndexParams>`（4.4 精读）。
4. `get_index_type_for_field(&field, &opts.index)` → 得到**底层大类** `lance_index::IndexType`。
5. `dataset.create_index_builder(&columns, index_type, params).train(...).replace(...)` → 组装 Lance 自己的 builder，`.await?` 真正落盘。
6. `self.dataset.update(dataset)` → 把变更后的 dataset 写回（与 u2-l2 讲的 `DatasetConsistencyWrapper` 一致）。

#### 4.3.4 校验与字段解析

`make_index_params` 对每个变体都先做类型校验，不匹配就报 `Error::Schema`：

[rust/lancedb/src/table/create_index.rs:45-61](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L45-L61) —— `validate_index_type` 用传入的 `supported_fn` 判断字段类型是否被支持，不支持时返回形如「A BTree index cannot be created on the field `x` which has data type Y」的清晰错误。

例如对 `List<T>` 数组列建 BTree 或 Bitmap 索引会被拒绝，只有 `LabelList` 允许——测试 `test_create_label_list_index` 正好印证这一点：

[rust/lancedb/src/table/create_index.rs:1230-1252](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L1230-L1252) —— 对 `tags: List<Utf8>` 列，`Index::BTree` 和 `Index::Bitmap` 都 `.is_err()`，只有 `Index::LabelList` 成功。

字段路径解析则支持嵌套结构与特殊列名（带连字符、带点号、大小写混合），测试 `test_create_index_nested_field_paths` 覆盖了 `metadata.user_id`、`` `row-id` ``、`MetaData.userId`、`image.embedding`、`literal.`` `a.b` `` 等路径：

[rust/lancedb/src/table/create_index.rs:887-952](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L887-L952) —— 对多种嵌套/特殊列名分别建索引，并用反引号转义含特殊字符的段。

#### 4.3.5 小练习与答案

**练习 1**：`create_index(&["a", "b"], ...)` 会发生什么？为什么？

> **参考答案**：会在 `NativeTable::create_index` 的第一步因 `opts.columns.len() != 1` 而返回 `Error::Schema { message: "Multi-column (composite) indices are not yet supported" }`（`table.rs:2756-2760`）。LanceDB 目前只支持单列索引。

**练习 2**：为什么 `IndexBuilder::execute` 要先 `self.parent.clone()` 再调用？

> **参考答案**：`parent` 是 `Arc<dyn BaseTable>`，`.clone()` 只增加一次 Arc 引用计数，代价极低（u2-l2 讲过 Table 克隆廉价正是这个原因）。这样做是为了把一个拥有所有权的 `IndexBuilder`（`self`）拆解：`parent` 克隆出来调用 trait 方法，`self`（含 index/columns 等字段）按值传入 `create_index(self)`。

### 4.4 `Index::Auto` 的自动选型逻辑

#### 4.4.1 概念说明

`Index::Auto` 是 `Index` 枚举里最「省心」的变体：你不用记住该用 IvfPq 还是 BTree，LanceDB 会根据**列的数据类型**自动替你选。它是 `simple.rs` 官方示例里建向量索引时用的写法：

[rust/lancedb/examples/simple.rs:119-122](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs#L119-L122) —— `table.create_index(&["vector"], Index::Auto).execute().await`。

自动选型的规则非常简单，核心是一个 if-else 链。

#### 4.4.2 核心流程

```text
make_index_params(field, Index::Auto):
   if supported_vector_data_type(field)   → IvfPq（L2，默认 IVF 参数，suggested_num_sub_vectors，8 bits）
   else if supported_btree_data_type(field) → BTree
   else                                   → Error::InvalidInput（无可用索引）
```

配套的 `get_index_type_for_field` 用同样判断给出底层大类：

```text
向量列  → lance_index::IndexType::Vector
btree 列 → lance_index::IndexType::BTree
（兜底）  → lance_index::IndexType::BTree  // 理论上不会走到，因为 make_index_params 会先报错
```

#### 4.4.3 源码精读

**选型的主体逻辑**：

[rust/lancedb/src/table/create_index.rs:142-170](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L142-L170) —— `Index::Auto` 分支：先判断是否为向量类型，是则用 IvfPq（默认 L2 度量、默认 IVF 参数、`suggested_num_sub_vectors` 算子向量数、8 bits）；否则判断是否支持 BTree，是则用 BTree；否则返回 `Error::InvalidInput`。

注意向量分支里有两处细节：

- 度量固定为 `MetricType::L2`（与 u3-l3「默认 L2」一致）。
- 子向量数用 `get_num_sub_vectors(None, dim, None)` 自动推算（`create_index.rs:83-98`），并在 4 bits 时保证为偶数。

**底层大类的判定**：

[rust/lancedb/src/table/create_index.rs:338-363](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L338-L363) —— `get_index_type_for_field`：`Auto` 在向量列返回 `IndexType::Vector`、在 btree 列返回 `IndexType::BTree`；其余显式变体直接映射（如 `Index::FTS(_)` → `IndexType::Inverted`，所有 `Ivf*` → `IndexType::Vector`）。

**判定依据 `supported_*_data_type`**：选型的「知识库」集中在 utils 模块。

[rust/lancedb/src/utils/mod.rs:291-299](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/utils/mod.rs#L291-L299) —— `supported_vector_data_type`：`FixedSizeList` 且子字段为浮点或 `UInt8` 时为真（也支持 `List` 嵌套递归判断）。这与 u1-l4 讲的「向量列约定为 `FixedSizeList<Float32>`」一致。

[rust/lancedb/src/utils/mod.rs:230-244](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/utils/mod.rs#L230-L244) —— `supported_btree_data_type`：整数、浮点、`Boolean`、`Utf8`、各种时间日期类型、`FixedSizeBinary` 都支持。所以 `Auto` 对绝大多数「普通」标量列都会落到 BTree。

#### 4.4.4 测试印证

官方测试清楚展示了 `Auto` 的两种走向：

[rust/lancedb/src/table/create_index.rs:441-451](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L441-L451) —— 向量列 `embeddings` 用 `Index::Auto`，建出的索引类型断言为 `IndexType::IvfPq`。

[rust/lancedb/src/table/create_index.rs:706-719](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L706-L719) —— 标量列 `i: Int32` 用 `Index::Auto`，建出的索引类型断言为 `IndexType::BTree`。

#### 4.4.5 代码实践：观察 `Auto` 选型

这是一个**源码阅读 + 本地验证型**实践。

1. **实践目标**：亲眼确认 `Index::Auto` 对向量列产出 IvfPq、对标量列产出 BTree。
2. **操作步骤**：
   - 阅读 `test_create_index`（`create_index.rs:411-474`）与 `test_create_scalar_index`（`create_index.rs:696-750`）两个测试。
   - 在本地按下方「示例代码」写一个小程序（可放进 `rust/lancedb/examples/` 下自建 example，或直接改一份本地拷贝运行）。
3. **需要观察的现象**：`list_indices()` 返回的 `IndexConfig` 里，向量列那条的 `index_type == IvfPq`、`distance_type == Some(L2)`；标量列那条的 `index_type == BTree`。
4. **预期结果**：与上面两个测试的断言一致。

示例代码（基于上述测试改写，标注为「示例代码」，非项目原有文件）：

```rust
// 示例代码：观察 Index::Auto 对向量列与标量列的不同选型
use std::sync::Arc;
use arrow_array::{Float32Array, Int32Array, RecordBatch};
use arrow_schema::{DataType, Field, Schema};
use lancedb::{connect, index::Index};
use futures::TryStreamExt;

#[tokio::main]
async fn main() -> lancedb::Result<()> {
    let conn = connect("memory://").execute().await?;

    // 构造一个含向量列 embeddings 和标量列 id 的批次
    let dim = 8usize;
    let schema = Arc::new(Schema::new(vec![
        Field::new("id", DataType::Int32, false),
        Field::new("embeddings", DataType::FixedSizeList(
            Arc::new(Field::new("item", DataType::Float32, true)), dim as i32), false),
    ]));
    let n = 256;
    let id = Arc::new(Int32Array::from_iter_values(0..n as i32));
    let vals = Float32Array::from_iter_values((0..n * dim).map(|v| v as f32));
    // （省略把 vals 包装成 FixedSizeListArray 的步骤，可参考测试里的 create_fixed_size_list）
    // let vectors = Arc::new(create_fixed_size_list(vals, dim as i32)?);
    // let batch = RecordBatch::try_new(schema, vec![id, vectors])?;
    // let table = conn.create_table("t", batch).execute().await?;

    // table.create_index(&["embeddings"], Index::Auto).execute().await?; // → IvfPq
    // table.create_index(&["id"], Index::Auto).execute().await?;          // → BTree
    // for cfg in table.list_indices().await? {
    //     println!("{:?} on {:?}", cfg.index_type, cfg.columns);
    // }
    Ok(())
}
```

> 说明：上面注释掉了构造 `FixedSizeListArray` 与执行的几行，完整写法请直接参照 `create_index.rs:393-462` 的 `create_fixed_size_list` 辅助函数与 `test_create_index` 主体。若暂时无法本地编译运行，可记为「待本地验证」，仅通过阅读测试断言理解行为即可。

#### 4.4.6 小练习与答案

**练习 1**：对一个 `tags: List<Utf8>` 列用 `Index::Auto` 会得到什么索引？

> **参考答案**：`supported_vector_data_type` 对 `List<Utf8>` 为假（递归判断要求最终是 FixedSizeList 浮点/UInt8），`supported_btree_data_type` 也为假（BTree 不支持 List 类型），因此会走到 `Error::InvalidInput`：「there are no indices supported for the field `tags` with the data type List(...)」。数组列必须显式用 `Index::LabelList`。

**练习 2**：`Auto` 对向量列默认用的度量是什么？如果我想用 Cosine，该怎么办？

> **参考答案**：默认是 L2（`create_index.rs:152` 写死 `MetricType::L2`）。`Auto` 不暴露度量参数，所以想用 Cosine 必须放弃 `Auto`，改用显式 Builder，例如 `Index::IvfPq(IvfPqIndexBuilder::default().distance_type(DistanceType::Cosine))`（向量 Builder 的 `distance_type` setter 见 `vector.rs:54-57`）。这也提醒我们：`Auto` 追求省心，但代价是失去对超参和度量的精细控制。

## 5. 综合实践

把本讲的三条主线（Index 枚举分类、create_index 流程、Auto 选型）串成一个完整任务。

**任务**：在一张表上同时建三个索引，并对照源码解释每一步。

1. 准备一张表，至少包含：一个向量列 `vector`（`FixedSizeList<Float32>`）、一个低基数字符串列 `category`（如只有 5 种取值）、一个高基数整数列 `id`。
2. 按下表分别建索引，**故意混用 `Auto` 和显式 Builder**：

   | 列 | 用法 | 预期 `index_type` |
   | --- | --- | --- |
   | `vector` | `Index::Auto` | `IvfPq` |
   | `category` | `Index::Bitmap(BitmapIndexBuilder::default())` | `Bitmap` |
   | `id` | `Index::BTree(BTreeIndexBuilder::default())` | `BTree` |

3. 调用 `table.list_indices().await?`，遍历打印每个 `IndexConfig` 的 `name`、`index_type`、`columns`，与你预期对照。
4. （可选进阶）挑其中一个索引名，调用 `table.index_stats(name).await?`，观察返回的 `IndexStatistics`：向量索引应有 `distance_type == Some(L2)`，标量索引则为 `None`（见 `index.rs:454-458` 的字段定义）。

**验证要点**：

- `vector` 用 `Auto` 落到 IvfPq（印证 4.4）。
- `category` 是低基数字符串，你**显式**选了 Bitmap 而非默认的 BTree——这正是「Auto 给标量列默认 BTree，但低基数列其实更适合 Bitmap」这一权衡的体现（Bitmap 适用场景见 `index.rs:41-45` 注释）。
- 整个过程没有多列索引、没有对不支持的列类型强行建索引，否则会触发 4.3.4 讲的 `Error::Schema`。

如果你暂时无法编译运行，可以改为**纯阅读型综合实践**：打开 `test_create_bitmap_index`（`create_index.rs:1089-1191`），逐行解释它如何对 5 个不同列建 Bitmap 索引，并断言 `list_indices` 返回的顺序与类型。

## 6. 本讲小结

- LanceDB 的索引分三大类：**标量索引（精确）**、**向量索引（近似 ANN）**、**全文索引（BM25 打分）**；标量索引只提速不改结果，向量索引用精度换速度。
- 必须区分三个同名近义的类型：**`Index`**（用户构建配方，含 `Auto`）、**`crate::index::IndexType`**（描述已存在索引，变体精细）、**`lance_index::IndexType`**（底层大类，仅 `Vector`/`BTree`/…）。
- `create_index` 严格遵循 Builder 模式：`Table::create_index` → `IndexBuilder`（只配置）→ `execute` → `BaseTable::create_index` → `NativeTable::create_index` 把 `Index` 翻译成 Lance 参数并落盘。
- `NativeTable::create_index` 的核心是 `make_index_params`（`Index` → Lance `IndexParams`）+ `get_index_type_for_field`（`Index` → 底层大类），并先做 `validate_index_type` 类型校验，目前只支持单列索引。
- `Index::Auto` 的选型规则极简：向量列（`FixedSizeList` 浮点/UInt8）→ **IvfPq（默认 L2）**，BTree 支持的标量列 → **BTree**，其余报错；依据是 `utils/mod.rs` 里的 `supported_*_data_type` 判断函数。
- 描述索引信息的两个结构体：`IndexConfig`（`list_indices` 返回，字段最全）与 `IndexStatistics`（`index_stats` 返回，含 `distance_type`）。

## 7. 下一步学习建议

本讲建立了索引的全景与创建流程。接下来按依赖关系建议：

- **u4-l2 标量索引**：深入 BTree / Bitmap / LabelList / Fm 的适用场景，理解「基数」如何决定选型，验证本讲里「低基数列更适合 Bitmap」的直觉。
- **u4-l3 向量索引 IVF 家族**：展开本讲只提名字的 IvfFlat/IvfPq/IvfSq/IvfRq，理解 IVF 空间划分与 `num_partitions` 等参数。
- **u4-l4 HNSW 与量化**：展开 IvfHnsw* 变体与 PQ/SQ/RQ 量化原理。
- **u4-l5 索引统计与等待机制**：深入本讲提到的 `IndexConfig`/`IndexStatistics` 字段含义，以及远程异步索引的 `Waiter` 轮询。

阅读源码时，建议带着本讲的「三层类型区分」去对照：每看到一个 `IndexType`，先确认它是 `crate::index::IndexType` 还是 `lance_index::IndexType`，能避免大量混淆。
