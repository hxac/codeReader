# 数据检视与多模态 blob

## 1. 本讲目标

本讲覆盖两个最小模块：**data (inspect)** 与 **blob**。学完后你应当能够：

- 说清楚 `data::inspect` 模块到底「检视」了什么——它是**列形状/类型推断**（哪些列是向量列、维度是多少），而不是聚合统计；
- 理解 LanceDB 的 **blob v2 列**如何把图像/视频等大对象「带外存储」（out-of-line），从而让一张表同时承载向量、标量与多模态大对象；
- 会用 `lancedb::blob` 宏声明 blob 列，把 `Binary`/`LargeBinary` 原始字节写入并自动强转为 blob 结构；
- 会用 `Table::blob_columns()`、`Table::fetch_blobs()` 读取回字节，并理解「查询只返回小描述符（descriptor），不返回字节」的设计。

> 说明：本讲只读源码、不改源码。涉及的运行结果若未在本机复现，会明确标注「待本地验证」。

## 2. 前置知识

阅读本讲前，建议你已掌握（对应前置讲义）：

- **u1-l4 Arrow 数据模型**：LanceDB 用 Apache Arrow 的 `Schema`/`Field`/`RecordBatch` 建模，向量列用 `FixedSizeList<Float32>` 表示。
- **u2-l2 Table 三层抽象**：`Table`（对外句柄）持有 `Arc<dyn BaseTable>`，本地实现是 `NativeTable`，方法大多是「一行转发 + 委托」。
- **u2-l3 Scannable 抽象**：`create_table`/`add` 接收任意 `Scannable` 数据源，首批数据的 schema 即表 schema。

几个本讲会用到的术语：

| 术语 | 含义 |
|------|------|
| **blob（大对象）** | 单条记录里体积很大的二进制载荷，如一张图片、一段音频/视频。 |
| **带外存储（out-of-line）** | 大对象不与普通列数据混在同一个文件里，而是单独存放，主表只保留一个「指针/描述符」。 |
| **descriptor（描述符）** | blob 列在查询时返回的小结构体，告诉调用方「字节在哪、多大」，但不含字节本身。 |
| **stable row id** | Lance 的稳定行 id。blob 要求建表时开启，这样才能用行 id 定位大对象。 |
| **Lance 文件格式版本** | Lance 格式按版本演进；blob v2 要求 `>= V2_2`。 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [rust/lancedb/src/data.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data.rs) | `data` 模块入口，声明 `inspect`/`sanitize`/`scannable` 子模块。 |
| [rust/lancedb/src/data/inspect.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/inspect.rs) | **检视模块**：`infer_dimension` 推断变长列表维度，`infer_vector_columns` 推断向量列。 |
| [rust/lancedb/src/blob.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/blob.rs) | **blob 核心**：声明 blob 字段、检测 blob 列、校验、按行 id 物化字节/句柄。 |
| [rust/lancedb/src/lib.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs) | 导出 `blob` 模块、并在 crate 根再导出 `blob`/`is_blob` 两个便捷函数。 |
| [rust/lancedb/src/table.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs) | `Table` 上的 `blob_columns`/`fetch_blobs`/`fetch_blob_files` 三个对外方法及 `BaseTable` 契约。 |
| [rust/lancedb/src/database/listing.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs) | 建表时：检测到 blob 列就自动开启 stable row id 并抬高存储版本。 |
| [rust/lancedb/src/table/datafusion/blob_coerce.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/datafusion/blob_coerce.rs) | 写入路径：把原始 `Binary`/`LargeBinary` 强转成 blob v2 结构列（含可运行单元测试）。 |

---

## 4. 核心概念与源码讲解

### 4.1 数据检视：infer_vector_columns 与向量列推断

#### 4.1.1 概念说明

「检视（inspect）」在 LanceDB 里指的是：**在写入或读取之前，扫描数据源的结构，自动判定列的语义**——具体到这里，就是判定「哪些列是向量列、它们的维度是多少」。

这一点很关键，因为 LanceDB **不要求你显式声明**「这一列是向量列」。你在 u3-l2 用 `nearest_to(vector)` 做向量搜索时，LanceDB 必须先知道哪个列是向量列。判定规则就落在 `data::inspect` 模块里。

> 诚实提示：`inspect` 模块做的是**列形状/类型推断**，并不是「算均值/方差/基数」那种聚合统计。表级别的数值统计在别处——例如行数看 `count_rows`（u2-l2）、索引统计看 `index_stats`（u4-l5）。本讲会把「检视」的语义讲准确，避免你误以为这里有现成的统计接口。

#### 4.1.2 核心流程

`infer_vector_columns` 的判定逻辑（伪代码）：

```text
对 schema 里的每个字段：
  若是 FixedSizeList<浮点>            → 直接认定为向量列（无论 strict）
  若是 List<浮点> / LargeList<浮点> 且非 strict → 进入「待定」，需读数据验证
  其它                                → 忽略

对每个「待定」列，逐批读取数据：
  计算该列每行的元素个数（维度）
  若所有行维度一致                     → 认定为向量列，记录维度
  若维度不一致（变长）                 → 剔除该列
最终返回所有认定的向量列名
```

这里有个 `strict`（严格模式）开关：

- `strict = true`：只认 `FixedSizeList<浮点>`，这是 LanceDB 推荐的标准向量列表示。
- `strict = false`：额外容忍「元素长度恰好一致」的 `List<浮点>` / `LargeList<浮点>`——但要读完数据才能确认长度真的恒定。

维度推断本身由 `infer_dimension` 完成：它用 Arrow 的 `length` 求每行长度，再用 `bool_and(eq(...))` 判断「是否所有行长度都等于第一行的长度」。

#### 4.1.3 源码精读

`infer_dimension`：求列表每行长度，若全相等返回该长度，否则返回 `None`：

[rust/lancedb/src/data/inspect.rs:18-36](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/inspect.rs#L18-L36) —— 用 `length()` 取每行长度，`bool_and(eq(...))` 判定是否恒定。

`infer_vector_columns` 的 schema 扫描分支：

[rust/lancedb/src/data/inspect.rs:45-65](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/inspect.rs#L45-L65) —— `FixedSizeList<浮点>` 直接入选；`List`/`LargeList<浮点>` 仅在非 strict 时进入待定集合。

读数据验证维度一致性的循环：

[rust/lancedb/src/data/inspect.rs:66-96](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/inspect.rs#L66-L96) —— 跨批维护「已见维度」，一旦发现不一致就把该列从候选里移除。

模块入口与可见性：

[rust/lancedb/src/data.rs:4-8](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data.rs#L4-L8) —— `data` 模块文档注释写明职责是「Data types, schema coercion, and data cleaning」，并 `pub mod inspect;`，因此可经 `lancedb::data::inspect::infer_vector_columns` 访问。

#### 4.1.4 代码实践

**实践目标**：亲手验证「向量列推断规则」，并直观感受 `strict` 的差别。

**操作步骤**：

1. 打开 [rust/lancedb/src/data/inspect.rs:111-169](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/inspect.rs#L111-L169) 的测试 `test_infer_vector_columns`。该测试构造了一个含 5 列的 batch：标量 `f`、字符串 `s`、定长浮点 list `l1`、变长浮点 list `l2`、`FixedSizeList<Float32,32>` 的 `fl`。
2. 运行该测试：

   ```bash
   cargo test -p lancedb --features remote infer_vector_columns -- --nocapture
   ```

**需要观察的现象**：测试在同一段数据上分别以 `strict=false` 与 `strict=true` 断言结果。

**预期结果**（已由源码断言确定）：

- `strict=false` → 认定 `["fl", "l1"]`（`fl` 是 FixedSizeList；`l1` 是定长 list，验证通过；`l2` 变长被剔除）。
- `strict=true` → 仅 `["fl"]`（只认 FixedSizeList）。

> 这一步可以本机复现；若环境未配置好 Rust 依赖，参考 u1-l2 的 `cargo check --features remote` 先确认工具链。

#### 4.1.5 小练习与答案

**练习 1**：一个外层是 `FixedSizeList` 但子字段是 `Int32` 的列，会被 `infer_vector_columns` 认作向量列吗？

> **答**：不会。匹配条件要求子字段是**浮点**类型（[inspect.rs:54](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/inspect.rs#L54) 的 `sub_field.data_type().is_floating()`）。整型定长列表不满足。

**练习 2**：为什么 `List<浮点>` 列在 `strict=false` 时还要「读数据」才能判定，而 `FixedSizeList<浮点>` 不用？

> **答**：`FixedSizeList` 的维度直接编码在类型里（`FixedSizeList(field, N)` 的 `N`），看 schema 即知；`List` 是变长类型，维度不在类型里，必须实际读取每行、确认所有行长度一致后才能认定（[inspect.rs:66-96](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/inspect.rs#L66-L96)）。

---

### 4.2 多模态大对象：blob 列的声明与存储格式

#### 4.2.1 概念说明

检索系统里，一行数据经常是「向量 + 标量元数据 + **原始大对象**」的组合——比如「图片向量 + 文件名 + **图片本身的字节**」。如果把几 MB 的图片字节和向量、标量塞进同一个列式文件，会导致：

- 读取任何一列都要跳过巨大的 blob，拖慢普通查询；
- 列式压缩对大块无结构字节几乎无效。

LanceDB 的 **blob v2 列**用「带外存储」解决：大对象单独存放在 blob 文件里，主表那列只保留一个**小描述符**。于是：

- 普通查询/向量搜索只读到小描述符，极轻量；
- 真正需要字节时，用行 id 去 `fetch_blobs` 把字节「物化」出来。

这就是 LanceDB 支持「多模态」的基础：一张表可以同时存向量、标量和任意大二进制。

声明一个 blob 列，只需用 `lancedb::blob` 宏（其实是函数）。它在底层生成一个带 `lance.blob.v2` 扩展标记的 `Struct` 字段。

#### 4.2.2 核心流程

blob 列的「形状」演变：

```text
声明阶段：lancedb::blob("image", true)
        → Field { name, Struct<data, uri>, metadata: { "ARROW:extension:name": "lance.blob.v2" } }

写入阶段：原始 Binary / LargeBinary
        → 经 coerce_blob_expr 强转为 Struct<data, uri> 的 blob 布局
        → 底层 Lance 把 data 写到带外 blob 文件，主表留下 descriptor

读取阶段：普通 query
        → 只回 Struct 里的 descriptor（小）
真正取字节：fetch_blobs("image", row_ids)
        → 按行 id 去 blob 文件读出 LargeBinary 字节
```

判定一个字段是不是 blob v2 列，看两点：类型是 `Struct`，且 metadata 里有 `lance.blob.v2` 扩展名。blob 列还可以**嵌套**在 struct/list 里，用「点分路径」寻址（如 `info.blob`）。

存储格式有一条硬约束：**blob 要求 Lance 文件格式 `>= V2_2`，且建表时开启 stable row id**。这条约束在 LanceDB 里是自动满足的——建表时一旦发现 schema 含 blob 列，就自动开启这两项。

#### 4.2.3 源码精读

模块文档把 blob 的设计一句话说透：

[rust/lancedb/src/blob.rs:4-10](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/blob.rs#L4-L10) —— 「blob v2 把大二进制载荷带外存储；写入时把 `Binary`/`LargeBinary` 强转为 blob 结构；查询只返回小描述符；要求格式 `>= V2_2` 且建表开启 stable row id」。

声明 blob 字段（核心入口）：

[rust/lancedb/src/blob.rs:42-44](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/blob.rs#L42-L44) —— `pub fn blob(name, nullable) -> Field`，转调底层 `lance::blob::blob_field`。文档示例展示了在 `Schema::new(...)` 里直接放 `lancedb::blob("image", true)`。

判断是否为 blob 列：

[rust/lancedb/src/blob.rs:52-54](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/blob.rs#L52-L54) —— `pub fn is_blob(field) -> bool` 即 `field.is_blob_v2()`。crate 根也再导出了它（[lib.rs:192](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L192) `pub use blob::{blob, is_blob};`），故可直接写 `lancedb::is_blob(&field)`。

检测 schema 是否含 blob 列（含嵌套）：

[rust/lancedb/src/blob.rs:57-97](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/blob.rs#L57-L97) —— `field_tree_has_blob_v2` 递归穿透 `Struct`/`List`/`LargeList`/`FixedSizeList` 子字段；`has_blob_columns` 在顶层调用它。

收集 blob 列的点分路径：

[rust/lancedb/src/blob.rs:71-107](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/blob.rs#L71-L107) —— `collect_blob_paths` 维护前缀，嵌套时拼出 `info.blob` 这样的路径；`blob_column_names` 按声明顺序返回。

存储版本自动抬高：

[rust/lancedb/src/blob.rs:110-122](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/blob.rs#L110-L122) —— `ensure_blob_storage_version`：若 schema 含 blob 列，把 `data_storage_version` 至少抬到 `V2_2`（用户显式指定了更高版本则保留）。

校验列是否为合法 blob v2 列（含旧版迁移提示）：

[rust/lancedb/src/blob.rs:127-146](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/blob.rs#L127-L146) —— `ensure_blob_v2_column` 三种错误：旧版 v1 列（带 `lance-encoding:blob` 标记）给迁移提示、非 blob 列、列不存在，均返回 `Error::InvalidInput`。

建表时自动满足两项前提：

[rust/lancedb/src/database/listing.rs:843-850](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L843-L850) —— `has_blob_columns(&data_schema).then_some(true)` 让 stable row id 默认开启；紧接着调用 `ensure_blob_storage_version`。namespace 后端也有同样处理（[namespace.rs:264-269](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/namespace.rs#L264-L269)）。

#### 4.2.4 代码实践

**实践目标**：用 `lancedb::blob` 构造字段，验证它带上了 `lance.blob.v2` 标记、类型是 `Struct`。

**操作步骤**：运行 blob 模块自带的单元测试（无需建库，纯内存）：

```bash
cargo test -p lancedb --features remote blob_field_carries_v2_extension_marker \
  storage_version_bumps_to_v2_2 -- --nocapture
```

**需要观察的现象**：`blob_field_carries_v2_extension_marker` 断言 `field.metadata()` 里 `ARROW:extension:name == "lance.blob.v2"`，且 `data_type` 是 `Struct(_)`（见 [blob.rs:338-345](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/blob.rs#L338-L345)）。

**预期结果**：测试通过，说明「blob 列 = 带 v2 标记的 Struct 字段」。具体字节数值「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果你建表时给某列显式指定了 `data_storage_version = V2_0`，但又用了 blob 列，最终版本是多少？

> **答**：会被抬高到 `V2_2`。`ensure_blob_storage_version` 只在「解析后版本 `< V2_2`」时覆盖（[blob.rs:119-121](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/blob.rs#L119-L121)），测试 `storage_version_overrides_lower_explicit_version` 正是断言这一点（[blob.rs:365-375](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/blob.rs#L365-L375)）。

**练习 2**：一个嵌套在 struct `info` 下的 blob 列 `blob`，其点分路径是什么？依据是哪段代码？

> **答**：`info.blob`。依据 [blob.rs:417-426](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/blob.rs#L417-L426) 的测试 `blob_column_names_includes_nested_path`，断言 `blob_column_names(&schema) == vec!["info.blob"]`。

---

### 4.3 写入与强制转换：Binary → blob 结构

#### 4.3.1 概念说明

blob 列在表 schema 里是 `Struct<data, uri>`，但用户手里的数据通常是原始字节——`Binary` / `LargeBinary` / `BinaryView`。LanceDB 在写入路径上做了一步**强转（coercion）**：只要你这一列的名字对得上、目标表声明了 blob 字段，原始字节就会自动变成 blob 结构。

这条强转由 DataFusion 物理表达式实现，是 `cast_to_table_schema` 的一环。强转还接受用户**预构造**的 struct（只要含 `data` 或 `uri` 子字段），并能把「窄」的两字段布局（data, uri）补齐成「宽」的四字段布局（data, uri, position, size）。

> 这解释了为什么练习里你可以直接写 `LargeBinary` 字节：写入管线会替你把它变成 blob。

#### 4.3.2 核心流程

```text
用户写入一列（列名 image）
  ├─ 该列是 Binary/LargeBinary/BinaryView
  │     → 强转：把字节塞进 blob struct 的 data 子字段，uri 等留空
  ├─ 该列是 Struct 且含 data 或 uri 子字段
  │     → 按声明补齐/对齐子字段，注入 lance.blob.v2 标记
  └─ 其它类型
        → 报错：cannot coerce ... into a blob v2 struct
```

合法的输入类型与错误分支都在 `coerce_blob_expr` 的前半段集中处理。

#### 4.3.3 源码精读

强转入口与输入类型判定：

[rust/lancedb/src/table/datafusion/blob_coerce.rs:22-64](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/datafusion/blob_coerce.rs#L22-L64) —— 接受 `Binary`/`LargeBinary`/`BinaryView`（[第 39 行](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/datafusion/blob_coerce.rs#L39)）；接受含 `data`/`uri` 子字段的 struct；其余类型在 [第 54-63 行](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/datafusion/blob_coerce.rs#L54-L63) 报错。

「宽」blob 布局（4 子字段）的样子：

[rust/lancedb/src/table/datafusion/blob_coerce.rs:147-165](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/datafusion/blob_coerce.rs#L147-L165) —— `wide_blob_field` 构造 `Struct<data: LargeBinary, uri: Utf8, position: UInt64, size: UInt64>` 并打上 `lance.blob.v2` 标记。

可直接运行的两个强转测试：

- [rust/lancedb/src/table/datafusion/blob_coerce.rs:218-231](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/datafusion/blob_coerce.rs#L218-L231) `large_binary_coerces_to_declared_blob_struct`：写入 `LargeBinary` 字节 `b"hello"`，强转后断言 `image` 字段 `is_blob_v2()` 且 `data.value(0) == b"hello"`。
- [rust/lancedb/src/table/datafusion/blob_coerce.rs:304-328](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/datafusion/blob_coerce.rs#L304-L328) `prebuilt_struct_gains_blob_field_metadata`：用 `blob("image", true)` 自带的子字段构造 `StructArray(data, uri)`，强转后断言拿到 `lance.blob.v2` 标记——这正是本讲综合实践里构造写入 batch 的依据。

#### 4.3.4 代码实践

**实践目标**：观察「原始 LargeBinary 字节 → blob 结构」的强转确实发生。

**操作步骤**：

```bash
cargo test -p lancedb --features remote blob_coerce -- --nocapture
```

**需要观察的现象**：`large_binary_coerces_to_declared_blob_struct`、`binary_coerces_to_declared_blob_struct`、`binary_view_coerces_to_declared_blob_struct` 等用例全部通过。

**预期结果**：三种二进制类型都能被强转为带 `lance.blob.v2` 标记的 struct 列，且 `data` 子字段保留了原始字节（[blob_coerce.rs:228-230](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/datafusion/blob_coerce.rs#L228-L230)）。具体终端输出「待本地验证」。

#### 4.3.5 小练习与答案

**练习**：如果你写入的列是个普通 `Utf8` 字符串列，却想强转成 blob 列，会发生什么？依据是？

> **答**：报错 `cannot coerce column '...' with type Utf8 into a blob v2 struct`。`coerce_blob_expr` 只接受二进制类型或含 `data`/`uri` 子字段的 struct，其它一律落到 [blob_coerce.rs:54-63](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/datafusion/blob_coerce.rs#L54-L63) 的错误分支。

---

### 4.4 读取大对象：descriptor、fetch_blobs 与 fetch_blob_files

#### 4.4.1 概念说明

前面强调过：**普通查询不会把 blob 字节读回来**，只会回一个小的 descriptor（描述符）。descriptor 是个 struct，记录大对象的「类型 kind、位置 position、大小 size」。当你真的需要字节时，有两种取法：

- `Table::fetch_blobs(column, row_ids)`：**立即物化**，返回 `LargeBinaryArray`，适合少量、马上要用的场景。
- `Table::fetch_blob_files(column, row_ids)`：**惰性句柄**，返回 `Vec<Option<BlobFile>>`，只有调用 `BlobFile::read()` 才真正读盘，适合大量、可能只挑一部分读的场景。

两种取法都遵循两条重要约定：

1. **长度与顺序对齐**：返回结果与传入 `row_ids` 等长、同序。
2. **空值保留**：null 行或零长行在结果里仍是 null（物化版本对应 null 元素，句柄版本对应 `None`）。

descriptor 的「空」判定用一个三元组：行被当作空，当且仅当

\[ \text{kind}=0 \;\land\; \text{position}=0 \;\land\; \text{size}=0 \]

（`kind == 0` 即 Lance 的 `BlobKind::Inline`）。Lance 底层读取会跳过这些空行，所以 LanceDB 用一轮额外的 null-mask 对齐把空行补回来。

> 跨后端一致性：`blob_columns`/`fetch_blobs`/`fetch_blob_files` 在 `BaseTable` trait 里有默认实现（返回 `NotSupported`），`NativeTable`（本地）真正实现；远程表默认不支持。这与 u2-l2 的「可选能力用默认 NotSupported 兜底」模式一致。

#### 4.4.2 核心流程

`fetch_blobs` 的内部流程（`take_blobs_aligned`）：

```text
1. ensure_blob_v2_column：校验列存在且是 blob v2（否则 InvalidInput）
2. blob_null_mask：先 take descriptor，按 (kind,position,size) 三元组算出哪些行是空
3. 过滤出 non-null 的 row_ids
4. dataset.read_blobs(column).with_row_ids(non_null).preserve_order(true).execute()
   → 得到非空行的字节载荷
5. 按 null_mask 把载荷散回原位置：空行 → null，非空行 → 对应字节
6. 返回 LargeBinaryArray（与原 row_ids 等长同序）
```

`fetch_blob_files` 流程几乎相同，只是第 4 步换成 `dataset.take_blobs(...)` 拿惰性句柄，第 5 步空行对应 `None`。

#### 4.4.3 源码精读

descriptor 的三元组空判定：

[rust/lancedb/src/blob.rs:198-224](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/blob.rs#L198-L224) —— 从 descriptor struct 取 `kind`(UInt8)/`position`(UInt64)/`size`(UInt64)，按 `kind==0 && position==0 && size==0`（[第 217-224 行](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/blob.rs#L217-L224) 注释对齐 Lance 的跳过条件）生成 null-mask。

按行 id 物化字节（含空值对齐）：

[rust/lancedb/src/blob.rs:236-281](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/blob.rs#L236-L281) —— `take_blobs_aligned`：先校验、算 null-mask、过滤非空 row_ids、`read_blobs(...).preserve_order(true)` 读字节、最后用 `LargeBinaryBuilder` 按 mask 把空行补 null。

惰性句柄版本：

[rust/lancedb/src/blob.rs:284-322](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/blob.rs#L284-L322) —— `take_blob_files_aligned`：结构与上面一致，空行映射为 `None`，非空行映射为 `Some(BlobFile)`。

对外方法（`Table` 句柄转发）：

- [rust/lancedb/src/table.rs:957-959](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L957-L959) `blob_columns()`：列出 blob v2 列名。
- [rust/lancedb/src/table.rs:991-997](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L991-L997) `fetch_blobs()`：物化字节，文档示例（[第 966-986 行](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L966-L986)）展示了 `query().with_row_id()` 取 `_rowid` 后再 `fetch_blobs` 的标准用法。
- [rust/lancedb/src/table.rs:1015-1021](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1015-L1021) `fetch_blob_files()`：惰性句柄。

`BaseTable` 契约与默认 `NotSupported`：

[rust/lancedb/src/table.rs:591-611](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L591-L611) —— trait 上三个 blob 方法都有「not supported on this table type」的默认实现。

`NativeTable` 实现（委托到 `crate::blob`）：

[rust/lancedb/src/table.rs:2857-2874](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L2857-L2874) —— `blob_columns` 调 `blob_column_names`、`fetch_blobs` 调 `take_blobs_aligned`、`fetch_blob_files` 调 `take_blob_files_aligned`。

#### 4.4.4 代码实践

**实践目标**：用 `fetch_blobs` 的官方文档示例理解「行 id → 字节」的取法。

**操作步骤**：阅读 [table.rs:966-986](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L966-L986) 的 doctest，它是一段「带 `_rowid` 查询 → 取 row_ids → `fetch_blobs`」的闭环代码（注意它被写成函数而非可执行测试，仅做类型检查，符合 AGENTS.md 的 doctest 约定）。

**需要观察的现象**：理解 `batch.column_by_name("_rowid")` 拿到的是 `UInt64Array`，其 `.values()` 即可直接喂给 `fetch_blobs`。

**预期结果**：`fetch_blobs("image", row_ids.values())` 返回与 `row_ids` 等长、同序、空值保留的 `LargeBinaryArray`。具体字节「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `fetch_blobs` 内部要先做一轮 `blob_null_mask`，再读字节？

> **答**：因为 Lance 的 `read_blobs`/`take_blobs` 会**跳过**空行（三元组全 0 的行，[blob.rs:171 注释](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/blob.rs#L171)），返回的非空载荷会比请求的 row_ids 短。先算 null-mask、只读非空行、再把结果按 mask 散回原位置，才能保证「等长、同序、空值保留」。

**练习 2**：在远程表上调用 `fetch_blobs` 会怎样？

> **答**：返回 `Error::NotSupported`。`BaseTable` 的 `fetch_blobs` 默认实现就是 NotSupported（[table.rs:598-602](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L598-L602)），只有 `NativeTable` 真正实现（[table.rs:2862-2865](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L2862-L2865)）。blob 目前是本地后端能力。

---

## 5. 综合实践

把本讲两个模块串起来，设计一个端到端任务：**用 `lancedb::blob` 标记一列大对象，写入若干行，再读取并核验**。

> 这是「源码阅读 + 最小可运行」型实践。完整程序需放到一个能引用 `lancedb` 的二进制/测试里运行；以下代码以 `rust/lancedb/src/table/datafusion/blob_coerce.rs` 的真实测试为依据，运行结果中的具体字节值「待本地验证」，但每一步的预期行为都由源码保证。

**示例代码**（标注为「示例代码」，依据 [blob_coerce.rs:304-328](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/datafusion/blob_coerce.rs#L304-L328) 与 [table.rs:966-986](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L966-L986) 编写）：

```rust
// 示例代码：声明 blob 列、写入、再读回字节
use std::sync::Arc;
use arrow_array::{
    ArrayRef, Int64Array, LargeBinaryArray, RecordBatch, StringArray, StructArray,
};
use arrow_schema::{DataType, Field, Schema};
use futures::TryStreamExt;
use lancedb::{
    connect,
    query::{ExecutableQuery, QueryBase},
};

# async fn run(uri: &str) -> Result<(), Box<dyn std::error::Error>> {
// 1) 用 blob 宏声明字段，取出它的 struct 子字段（data, uri）
let blob_field = lancedb::blob("image", true);
let DataType::Struct(children) = blob_field.data_type().clone() else { unreachable!() };

// 2) 构造 image 列：data 放字节，uri 留空（写入时 Lance 会补 descriptor）
let image = StructArray::new(
    children,
    vec![
        Arc::new(LargeBinaryArray::from_iter_values([
            b"\x89PNG fake-bytes-1".as_slice(),
            b"\x89PNG fake-bytes-2".as_slice(),
        ])) as ArrayRef,
        Arc::new(StringArray::from(vec![None::<&str>, None::<&str>])),
    ],
    None,
);

// 3) 构造 batch：首批数据 schema 即表 schema（含 blob 标记）
let batch = RecordBatch::try_new(
    Arc::new(Schema::new(vec![
        Field::new("id", DataType::Int64, false),
        blob_field,
    ])),
    vec![
        Arc::new(Int64Array::from_iter_values([1, 2])),
        Arc::new(image),
    ],
)?;

// 4) 建表并写入
let db = connect(uri).execute().await?;
let table = db.create_table("imgs", vec![batch]).execute().await?;

// 5) 检视：列出 blob 列、确认标记
let cols = table.blob_columns().await?;            // 预期: ["image"]
let field = table.schema()?.field_with_name("image")?;
assert!(lancedb::is_blob(field));                   // 预期: true

// 6) 读取：用 _rowid 取行 id，再 fetch_blobs 物化字节
let mut stream = table.query().with_row_id().limit(10).execute().await?;
while let Some(b) = stream.try_next().await? {
    let row_ids = b
        .column_by_name("_rowid")
        .unwrap()
        .as_any()
        .downcast_ref::<arrow_array::UInt64Array>()
        .unwrap();
    let images = table.fetch_blobs("image", row_ids.values()).await?;
    for i in 0..images.len() {
        if images.is_null(i) { continue; }
        println!("row {} -> {} bytes", row_ids.value(i), images.value(i).len());
    }
}
# Ok(())
# }
```

**操作步骤**：

1. 把上面片段放进一个 `#[tokio::test]` 或 `examples/blob_demo.rs`（注意需在 `rust/lancedb/Cargo.toml` 的 `[[example]]` 注册并 `required-features` 含相关 feature）。
2. 用本地路径连接，例如 `uri = "data/lancedb"`。
3. 运行后先看第 5 步的 `blob_columns()` 与 `is_blob` 断言。

**需要观察的现象与预期结果**：

- `blob_columns()` 返回 `["image"]`（依据 [table.rs:2857-2860](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L2857-L2860) + [blob.rs:101-107](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/blob.rs#L101-L107)）。
- `is_blob(field)` 为 `true`（依据 [blob.rs:52-54](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/blob.rs#L52-L54)）。
- `fetch_blobs` 回的字节长度与写入一致（依据 [blob_coerce.rs:228-230](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/datafusion/blob_coerce.rs#L228-L230) 的强转保证 data 不丢）。
- 具体打印的 byte 数「待本地验证」。

> 关于「用 inspect 查看该列」：本讲的 `data::inspect` 模块专做**向量列推断**（4.1 节），并不输出 blob 列的存储信息。查看 blob 列信息用的是 `Table::blob_columns()` + `Table::schema()` 的字段标记（属于 blob 模块本身）。如果你想顺带演示 inspect 模块，可在同一张表加一列 `FixedSizeList<Float32>` 向量，再用 `lancedb::data::inspect::infer_vector_columns(reader, true)` 观察——它会返回该向量列名，而**不会**把 blob 列报告为向量列（blob 是 `Struct`，不匹配向量列规则）。

---

## 6. 本讲小结

- **data (inspect)** 模块做的是**列形状/类型推断**：`infer_dimension` 判定变长列表维度，`infer_vector_columns` 按 `FixedSizeList<浮点>`（strict 时唯一）或定长 `List<浮点>`（非 strict）认定向量列；它不是聚合统计接口。
- **blob v2 列**用 `lancedb::blob(name, nullable)` 声明，本质是带 `lance.blob.v2` 扩展标记的 `Struct<data, uri>` 字段，把大对象**带外存储**，普通查询只回小 descriptor。
- blob 列可**嵌套**，用点分路径（如 `info.blob`）寻址；`has_blob_columns`/`blob_column_names` 递归检测与收集。
- 写入时原始 `Binary`/`LargeBinary`/`BinaryView` 会被 `coerce_blob_expr` **自动强转**为 blob 结构；建表时一旦发现 blob 列，**自动开启 stable row id 并把存储版本抬到 `V2_2`**。
- 读取字节有两条路：`fetch_blobs`（立即物化 `LargeBinaryArray`）与 `fetch_blob_files`（惰性 `BlobFile` 句柄）；两者都**等长、同序、空值保留**，内部靠 `(kind,position,size)` 三元组算 null-mask 来对齐 Lance 的跳空行为。
- blob 是**本地后端能力**：`BaseTable` 默认返回 `NotSupported`，仅 `NativeTable` 实现。

## 7. 下一步学习建议

- **多模态检索闭环**：本讲只讲了「存/取字节」。下一步可结合 u3-l2 向量搜索，做「图片向量检索 → `fetch_blobs` 取回原图」的完整链路。
- **数据写入细节**：若想深入「首批数据 schema 即表 schema」「强转管线」的更多细节，回看 u2-l3 的 Scannable 抽象与 `cast_to_table_schema`。
- **存储后端**：blob 字节最终落在对象存储上，u6-l1 的对象存储抽象会讲清本地/S3/GS 的统一读写。
- **扩展阅读源码**：`rust/lancedb/src/table/datafusion/blob_coerce.rs` 的完整测试集是理解 blob 写入边界（空值、宽窄布局、外部 uri 引用）的最佳材料；`rust/lancedb/src/blob.rs` 的 `#[cfg(test)]` 模块则覆盖了字段标记与版本抬高的全部边界。
