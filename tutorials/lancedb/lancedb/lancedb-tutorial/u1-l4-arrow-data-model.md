# Arrow 数据模型与向量列表示

## 1. 本讲目标

本讲是 LanceDB 数据建模的「地基」。学完后你应该能够：

- 说清楚 LanceDB 为什么把 **Apache Arrow** 当作自己的通用数据表示；
- 写出一个带「标量列 + 向量列」的 `RecordBatch`，并把它的 schema 当成一张表的 schema；
- 解释向量列为什么用 `FixedSizeList<Float32>`（或 `Float16`）表示，以及 LanceDB 内部如何自动识别这类列为「向量列」；
- 看懂 `rust/lancedb/src/arrow.rs` 里 `IntoArrow` / `RecordBatchReader` 这一组抽象如何让「任何能转成 Arrow 的数据」都能写入 LanceDB。

本讲只覆盖一个最小模块：**arrow**（数据模型与接入抽象）。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**什么是列式存储？** 传统行式数据库把一行数据连续存放（适合一次读一整行）。而分析和检索场景往往只关心某几列（比如只看向量列做相似度比较），列式存储把「同一列的所有值」连续存放，于是按列读取时能命中 CPU 缓存、可以按列压缩、还能只读需要的列。LanceDB 底层依赖的 **Lance** 就是一种面向 ML/检索的列式格式。

**什么是 Apache Arrow？** Arrow 是一套**内存中的列式数据标准**。它规定了一个二进制内存布局（每个数组在内存里长什么样），这样不同的语言、不同的库只要都遵守 Arrow 布局，就可以**零拷贝**地交换数据——不需要序列化/反序列化。Arrow 的 Rust 实现就是 `arrow-rs`（对应 crate `arrow-array` / `arrow-schema`）。

**为什么向量需要专门的列类型？** 一个向量是一组固定长度的浮点数（比如 128 维）。普通标量列是一维的（一个数对应一行）；向量列是「每行塞了一个固定长度的小数组」。Arrow 里专门有 `FixedSizeList<T>` 类型来表达「每行一个固定长度 N 的子列表」，恰好匹配向量的形状。LanceDB 就约定：凡是 `FixedSizeList<Float32/Float16>` 的列，都当作向量列来对待。

> 名词小词典：
> - **Schema（模式）**：描述一张表有哪些列、每列叫什么名字、是什么数据类型。
> - **Field（字段）**：一列的元信息，= 名字 + 数据类型 + 是否可空。
> - **RecordBatch（记录批）**：Arrow 里「一批行」的单位，= 一个 schema + 若干个并列的列数组。它是 LanceDB 写入和读取的最小数据单元。
> - **FixedSizeList**：固定长度的列表类型，`FixedSizeList(子字段, N)` 表示每行是一个长度恒为 N 的列表。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [rust/lancedb/src/arrow.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/arrow.rs) | LanceDB 对 Arrow 的接入抽象：定义自己的 `RecordBatchReader`/`RecordBatchStream` 流式接口，以及把任意数据源转成 Arrow 的 `IntoArrow` trait。 |
| [rust/lancedb/examples/simple.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs) | 官方最小示例，本讲的「样板代码」来源：如何构造含 `id` + `vector` 两列的 `RecordBatch`。 |
| [rust/lancedb/src/lib.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs) | crate 根文档，明确写了「LanceDB 用 arrow-rs 定义 schema，把 `FixedSizeList<Float16/Float32>` 当作向量列」。 |
| [rust/lancedb/src/data/inspect.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/inspect.rs) | `infer_vector_columns` 函数：扫描 schema 自动找出哪些列是向量列——本讲用来验证「向量列识别规则」。 |
| [rust/lancedb/src/table/create_index.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs) | `get_vector_dimension` 辅助函数：从 `FixedSizeList(_, N)` 里取出向量维度 N。 |

## 4. 核心概念与源码讲解

### 4.1 为什么 LanceDB 基于 Apache Arrow

#### 4.1.1 概念说明

LanceDB 不是「自己发明一套数据格式」，而是**全盘采用 Arrow 的数据模型**：schema 用 `arrow_schema::Schema`，列数组用 `arrow_array` 里的各种 `*Array`，一批行用 `arrow_array::RecordBatch`。这样做有三个直接收益：

1. **零拷贝跨语言**：Python / Node / Java 的绑定拿到的是 Arrow 缓冲区，不需要在 Rust 和宿主语言之间反复序列化，向量这种大块数据尤其受益。
2. **复用生态**：Arrow 生态里有大量现成工具（DataFusion 做表达式、Polars 做数据处理、各种 `*Array` 构造器），LanceDB 直接拿来用。
3. **与底层 Lance 对齐**：Lance 列式格式本身设计为 Arrow 兼容的内存表示，磁盘格式 ↔ 内存格式之间转换开销很小。

#### 4.1.2 核心流程

数据从「用户手里的某种结构」到「LanceDB 写入」的流程：

```text
用户的任意数据源
      │  （Vec、DataFrame、自定义流……）
      ▼
IntoArrow::into_arrow()        ← 转成 arrow 的 RecordBatchReader
      │
      ▼
arrow_array::RecordBatch       ← 标准列式批：schema + 若干列数组
      │
      ▼
LanceDB create_table / add     ← schema 即表 schema，按列写入 Lance
```

关键点：**schema 来自 RecordBatch，表的 schema = 第一批数据的 schema**。你给什么样的列定义，就得到什么样的表。

#### 4.1.3 源码精读

crate 根文档里一句话点明整体设计：

[rust/lancedb/src/lib.rs:68-70](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L68-L70)：说明 LanceDB 用 arrow-rs 来定义 schema、数据类型和数组本身。

紧跟着的文档块 [rust/lancedb/src/lib.rs:76-79](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L76-L79) 进一步强调：

> To create a Table, you need to provide an `arrow_array::RecordBatch`. The schema of the `RecordBatch` determines the schema of the table.

也就是说，**建表不需要单独声明 schema，RecordBatch 的 schema 就是表的 schema**。

把「任意数据」统一到 Arrow 的入口是 `IntoArrow` trait：

[rust/lancedb/src/arrow.rs:113-131](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/arrow.rs#L113-L131)：定义 `IntoArrow` trait（把数据转成 Arrow 的 `RecordBatchReader`），并对所有「已经是 `arrow_array::RecordBatchReader + Send`」的类型提供了一个 blanket 实现——这意味着只要你手里是个标准的 Arrow reader，就能直接喂给 `create_table` / `add`，无需任何适配代码。

> 小贴士：正是因为这个 blanket 实现，`simple.rs` 里直接传一个 `RecordBatch`（它实现了 Arrow 的 `RecordBatchReader`）就能建表，不需要包一层。

#### 4.1.4 代码实践

**实践目标**：亲手感受「schema 来自数据」。

**操作步骤**：

1. 打开 `rust/lancedb/examples/simple.rs`，定位到 `create_empty_table`（[第 109-117 行](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs#L109-L117)），它演示了**只给 schema、不给数据**也能建表。
2. 在 `main` 里 `create_empty_table(&db).await` 之后，补一行打印空表 schema（示例代码）：

```rust
// 示例代码（非项目原有）
let empty = db.open_table("empty_table").execute().await?;
println!("{:?}", empty.schema().await?);
```

3. 运行示例：

```bash
cargo run --example simple -p lancedb
```

> 该 example 在 `Cargo.toml` 中没有 `required-features`（见 [rust/lancedb/Cargo.toml:158-159](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/Cargo.toml#L158-L159)），因此用默认 feature 即可编译运行。

**需要观察的现象**：打印出的 schema 正是你传给 `create_empty_table` 的两个字段（`id: Int32`、`item: Utf8`），证明「schema 即表 schema」。

**预期结果**：表里 0 行，但 schema 已存在，字段名和类型与传入一致。

**待本地验证**：具体打印格式（`Schema` 的 Debug 输出）以你本地运行结果为准。

#### 4.1.5 小练习与答案

**练习 1**：如果不传任何数据、也不传 schema，能不能建表？为什么？

**参考答案**：不能。建表要么提供数据（其 RecordBatch 的 schema 即表 schema），要么显式提供一个 schema（`create_empty_table`）。LanceDB 没有办法凭空知道列结构。

**练习 2**：为什么 LanceDB 选择 Arrow 而不是自己定义一套内存结构？

**参考答案**：复用 Arrow 生态、获得跨语言零拷贝能力、并与底层 Lance 列式格式对齐，避免重复造轮子和反复序列化开销。

---

### 4.2 向量列的 FixedSizeList 表示约定

#### 4.2.1 概念说明

向量列的本质是「每一行放一个固定长度 N 的浮点数组」。Arrow 的 `FixedSizeList(子字段, N)` 正好表达这个形状：

- 外层是「每行一个列表」，长度固定为 N；
- 内层「子字段」说明列表里每个元素的类型，向量列用 `Float32`（或 `Float16`）。

LanceDB 的约定（来自 crate 文档）非常明确：**`FixedSizeList<Float16/Float32>` 的列被视为向量列**。当你对这样的列建索引或做 `nearest_to` 搜索时，LanceDB 就知道要按「向量」来处理，而不是普通列表。

#### 4.2.2 核心流程

一个 `FixedSizeList<Float32, N>` 列在内存里的物理布局：

```text
表共 R 行，向量维度 N
values 子数组长度 = R × N   （所有向量的分量平铺成一个一维数组）
第 i 行的向量 = values[i*N .. (i+1)*N]
```

用数学语言描述第 i 行向量 \(\vec{v}_i\) 与子数组 a 的关系：

\[
\vec{v}_i = \big(\,a_{iN},\; a_{iN+1},\; \dots,\; a_{iN+N-1}\,\big), \quad i = 0,1,\dots,R-1
\]

这种「平铺 + 固定步长」的布局正是列式 + 向量化计算友好的形式，也是它零拷贝高效的来源。

构造一个向量列的标准三步：

1. 定义子字段 `Field::new("item", Float32, true)`（习惯上叫 `item`，可空以允许分量缺失）；
2. 用 `DataType::FixedSizeList(子字段, N)` 定义列字段；
3. 用 `FixedSizeListArray::from_iter_primitive::<Float32Type, _, _>(行迭代器, N)` 把数据填进去。

#### 4.2.3 源码精读

**样板代码：构造 id + vector 两列的 RecordBatch**

[rust/lancedb/examples/simple.rs:60-89](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs#L60-L89) 是本讲最重要的代码段。它做了三件事，逐段拆开看：

schema 部分（[第 64-74 行](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs#L64-L74)）：

```rust
let schema = Arc::new(Schema::new(vec![
    Field::new("id", DataType::Int32, false),
    Field::new(
        "vector",
        DataType::FixedSizeList(
            Arc::new(Field::new("item", DataType::Float32, true)),
            DIM as i32,          // DIM = 128
        ),
        true,
    ),
]));
```

- `id` 列：标量 `Int32`，不可空。
- `vector` 列：`FixedSizeList<Float32, 128>`，第二个参数是**维度**。这正是「向量列」的规范写法。

数据填充（[第 77-88 行](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs#L77-L88)）：

```rust
RecordBatch::try_new(
    schema.clone(),
    vec![
        Arc::new(Int32Array::from_iter_values(0..TOTAL as i32)),
        Arc::new(
            FixedSizeListArray::from_iter_primitive::<Float32Type, _, _>(
                (0..TOTAL).map(|_| Some(vec![Some(1.0); DIM])),
                DIM as i32,
            ),
        ),
    ],
)
```

- 第 0 列用 `Int32Array::from_iter_values` 生成 `0..1000` 的 id；
- 第 1 列用 `FixedSizeListArray::from_iter_primitive::<Float32Type, _, _>` 生成 1000 个全 1.0 的 128 维向量。泛型参数 `Float32Type` 指定分量类型，第二个参数 `DIM` 指定维度。

crate 文档对这一约定的官方表述：[rust/lancedb/src/lib.rs:79](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L79)「Vector columns should be represented as `FixedSizeList<Float16/Float32>` data type.」

**LanceDB 如何自动识别向量列**

LanceDB 不需要你显式声明「这一列是向量列」，而是扫描 schema 自动推断。推断逻辑在 `infer_vector_columns`：

[rust/lancedb/src/data/inspect.rs:45-63](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/inspect.rs#L45-L63)：核心是下面这段匹配（[第 52-56 行](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/inspect.rs#L52-L56)）：

```rust
DataType::FixedSizeList(sub_field, _) if sub_field.data_type().is_floating() => {
    columns.push(field.name().clone());
}
```

判据是：**外层是 `FixedSizeList`，且子字段类型是浮点（`is_floating()`）**。注意它用的是 `is_floating()`（涵盖 Float16/Float32/Float64），比文档里说的「Float16/Float32」稍宽松；非严格模式下还会把定长的 `List<float>` 也当作候选（见 [第 57-62 行](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/inspect.rs#L57-L62)）。

**维度如何被取出**

建向量索引时需要知道维度 N。`get_vector_dimension` 直接从 `FixedSizeList(_, N)` 解出 N：

[rust/lancedb/src/table/create_index.rs:100-106](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L100-L106)：`DataType::FixedSizeList(_, n) => Ok(*n as u32)`，把第二个参数直接当维度返回。

#### 4.2.4 代码实践

**实践目标**：构造一个含 `id(Int32)` 与 `vector(FixedSizeList<Float32>)` 两列的 RecordBatch，写入一张新表并读取验证。

**操作步骤**：

1. 复制 `simple.rs` 为本地新文件 `my_arrow.rs`（放在 `rust/lancedb/examples/`，**注意：示例代码，仅用于本地练习，不要提交**）。
2. 修改 `create_some_records`，把维度从 128 改成 **8**、行数改成 **4**，并让每行向量各不相同（便于读取后核对）。示例代码：

```rust
// 示例代码（基于 simple.rs 改写，非项目原有）
fn create_some_records() -> Result<RecordBatch> {
    const TOTAL: usize = 4;
    const DIM: usize = 8;
    let schema = Arc::new(Schema::new(vec![
        Field::new("id", DataType::Int32, false),
        Field::new(
            "vector",
            DataType::FixedSizeList(
                Arc::new(Field::new("item", DataType::Float32, true)),
                DIM as i32,
            ),
            true,
        ),
    ]));
    // 第 i 行向量 = [i, i, ..., i]（8 个 i），方便肉眼核对
    let rows = (0..TOTAL).map(|i| Some(vec![Some(i as f32); DIM]));
    Ok(RecordBatch::try_new(
        schema.clone(),
        vec![
            Arc::new(Int32Array::from_iter_values(0..TOTAL as i32)),
            Arc::new(FixedSizeListArray::from_iter_primitive::<Float32Type, _, _>(
                rows, DIM as i32,
            )),
        ],
    )?)
}
```

3. 建表后读取并打印（示例代码）：

```rust
// 示例代码
let batches: Vec<RecordBatch> = tbl.query()
    .limit(4)
    .execute().await?
    .try_collect::<Vec<_>>().await?;
println!("{:?}", batches);
```

4. 在 `rust/lancedb/` 下运行（example 无 required-features）：

```bash
cargo run --example my_arrow -p lancedb
```

**需要观察的现象**：读回的 4 行里，`id` 为 0/1/2/3，对应 `vector` 全是 0.0 / 1.0 / 2.0 / 3.0，证明写入的列式数据被正确落盘又读回。

**预期结果**：schema 显示 `vector: FixedSizeList<Float32, 8>`，行数 4，向量分量与写入一致。

**待本地验证**：`query().execute()` 在本讲（u1-l4）尚未正式讲解，这里只用于读取验证；若编译报错，可先只调用 `tbl.count_rows()` 验证行数为 4。`query` 的完整用法见 u3-l1。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `vector` 列定义成 `DataType::Float32`（而不是 `FixedSizeList<Float32>`），LanceDB 还会把它当向量列吗？

**参考答案**：不会。`infer_vector_columns` 只匹配 `FixedSizeList` 外层（[inspect.rs:54](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/inspect.rs#L54)），裸的 `Float32` 是普通标量列，无法做 `nearest_to` 向量搜索。

**练习 2**：两张表的向量列维度不同（一张 128 维、一张 8 维），会冲突吗？

**参考答案**：不会。维度是「列字段」的一部分，存在 schema 里（`FixedSizeList(_, N)` 的 N），不同表各自独立。`get_vector_dimension`（[create_index.rs:101-106](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L101-L106)）会按列各自的 N 取值。

**练习 3**：为什么子字段习惯写成可空（`Field::new("item", Float32, true)` 里的 `true`）？

**参考答案**：允许个别向量的个别分量为 null，提供更大兼容性；LanceDB 的检测规则只看「子字段是否浮点」，并不要求分量非空。

---

### 4.3 通用数据接入：IntoArrow 与 RecordBatch 抽象

#### 4.3.1 概念说明

`arrow.rs` 这个最小模块要解决的另一类问题是：**用户手里的数据形态千差万别**——可能是一个 `Vec`、一个 Polars DataFrame、一个数据库游标、或一个异步流。LanceDB 不想为每种数据源写一份 `create_table`，于是定义了两条窄抽象：

- **同步**：`RecordBatchReader`（迭代器 + schema）+ `IntoArrow`；
- **异步**：`RecordBatchStream`（异步流 + schema）+ `IntoArrowStream`。

只要数据源能转成其中之一，就能写入。这就是 `create_table` / `add` 能接收「任意 `Scannable` 输入」的底层原因（`Scannable` 在 u2-l3 详讲，本讲只看它的 Arrow 地基）。

#### 4.3.2 核心流程

```text
同步数据源                        异步数据源
   │                                 │
   ▼                                 ▼
IntoArrow::into_arrow()         IntoArrowStream::into_arrow()
   │  → Box<dyn RecordBatchReader>   │  → SendableRecordBatchStream
   └─────────────┬───────────────────┘
                 ▼
      统一交给 LanceDB 写入管线（按 schema、按列落盘）
```

两个 trait 都用了「blanket 实现」技巧：已经是 Arrow 标准类型的东西，自动满足 trait，无需手写适配。

#### 4.3.3 源码精读

**同步侧**

[rust/lancedb/src/arrow.rs:17-24](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/arrow.rs#L17-L24)：LanceDB 自己的 `RecordBatchReader` trait——一个「能产出 `RecordBatch` 的迭代器，且自带 schema」。注意它要求每个 batch 的 schema 都和 `schema()` 返回的一致。

[rust/lancedb/src/arrow.rs:120-131](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/arrow.rs#L120-L131)：`IntoArrow` trait 及其 blanket 实现：凡是实现了标准 `arrow_array::RecordBatchReader + Send + 'static` 的类型，自动 `IntoArrow`，`into_arrow()` 只是把它 `Box` 起来。这就是「直接传 RecordBatch 也能建表」的原因。

需要一个简单的「迭代器 + schema」组合时，用现成的 `SimpleRecordBatchReader`：[rust/lancedb/src/arrow.rs:26-46](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/arrow.rs#L26-L46)。

**异步侧**

[rust/lancedb/src/arrow.rs:48-58](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/arrow.rs#L48-L58)：`RecordBatchStream` trait（异步版）与类型别名 `SendableRecordBatchStream = Pin<Box<dyn RecordBatchStream + Send>>`。`Pin` 是因为异步流需要「自引用」安全，`Send` 是为了能跨线程（多线程写入/查询必需）。

[rust/lancedb/src/arrow.rs:133-161](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/arrow.rs#L133-L161)：`IntoArrowStream` 及其两个 blanket 实现——一个把 `SendableRecordBatchStream` 直通，另一个把 DataFusion 的流（`datafusion_physical_plan::SendableRecordBatchStream`）转过来。后者让 LanceDB 能直接消费 DataFusion 查询计划的输出。

> 延伸：[rust/lancedb/src/arrow.rs:163-181](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/arrow.rs#L163-L181) 还为 `lance_datagen` 的批量生成器提供了 `into_ldb_stream` 扩展，测试里常用它造大量随机数据。本讲了解即可。

#### 4.3.4 代码实践

**实践目标**：源码阅读型实践——跟踪「一个 RecordBatch 是怎么满足 `IntoArrow` 的」。

**操作步骤**：

1. 在 [arrow.rs:127-131](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/arrow.rs#L127-L131) 的 blanket 实现处，确认 `T: arrow_array::RecordBatchReader + Send + 'static` 自动得到 `IntoArrow`。
2. 查阅 `arrow` crate 文档：`arrow_array::RecordBatch` 是否实现了 `arrow_array::RecordBatchReader`（它通过 `RecordBatchIterator`/自身可以充当 reader）。
3. 回到 `simple.rs` 的 `create_table`（[第 91-99 行](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs#L91-L99)），看到 `db.create_table("my_table", initial_data)` 直接传 `RecordBatch`。

**需要观察的现象**：整条链路「RecordBatch →（满足）RecordBatchReader →（blanket）IntoArrow → into_arrow() 返回 Box<dyn RecordBatchReader> → LanceDB 写入」是通的，无需任何用户侧转换代码。

**预期结果**：能画出这条转换链，并解释为什么 `create_table` 第二个参数的类型签名（接受 `impl IntoArrow`）能兼容如此多输入。

**待本地验证**：若想亲手验证，可在本地给一个 `RecordBatch` 调用 `IntoArrow::into_arrow()`，确认它编译通过并返回 reader。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SendableRecordBatchStream` 要带 `Send`？

**参考答案**：LanceDB 的写入和查询是多线程/异步的，流需要在不同的 task、不同的线程之间传递，`Send` 是跨线程安全的前提。

**练习 2**：`IntoArrow`（同步）和 `IntoArrowStream`（异步）的区别是什么？什么时候用哪个？

**参考答案**：前者返回同步迭代器 `RecordBatchReader`，适合内存里已有的整块数据；后者返回异步流 `SendableRecordBatchStream`，适合数据需要异步生产（如远程拉取、文件流式读取）的场景。LanceDB 的写入 API 对两种都有对应入口。

## 5. 综合实践

把本讲三个要点（Arrow 数据模型、向量列 FixedSizeList 约定、通用接入抽象）串成一个任务：

**任务**：写一个最小的 Rust 程序，完成「构造数据 → 建表 → 读回验证」全流程。

1. 基于 `simple.rs` 的 `create_some_records`，构造一张表：
   - `id`: `Int32`，3 行（0、1、2）；
   - `vector`: `FixedSizeList<Float32, 4>`，3 个向量：`[1,1,1,1]`、`[2,2,2,2]`、`[3,3,3,3]`；
   - 额外加一个标量列 `tag`: `Utf8`，值为 `"a"`、`"b"`、`"c"`（练习混合标量 + 向量列）。
2. 用 `connect("data/my_arrow").execute().await?` 连接本地库，`create_table` 写入。
3. 打印 `table.schema().await?`，确认 `vector` 列类型为 `FixedSizeList<Float32, 4>`，且 `tag` 为 `Utf8`。
4. 用 `table.count_rows().await?` 确认 3 行（`count_rows` 是表格抽象的方法，u2-l2 会详讲；这里只用它做行数核对）。

**自检清单**：

- [ ] schema 中 `vector` 列的维度与你设置的一致；
- [ ] 表行数 = 3；
- [ ] 能说清楚「为什么 LanceDB 知道 `vector` 是向量列」（答：`infer_vector_columns` 匹配 `FixedSizeList<浮点>`，见 [inspect.rs:54](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/inspect.rs#L54)）；
- [ ] 能说清楚「为什么直接传 RecordBatch 就能建表」（答：`IntoArrow` 的 blanket 实现，见 [arrow.rs:127-131](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/arrow.rs#L127-L131)）。

## 6. 本讲小结

- LanceDB 全盘采用 **Apache Arrow** 作为数据模型：schema 用 `arrow_schema::Schema`，数据用 `arrow_array::RecordBatch`，**首批数据的 schema 即表的 schema**（[lib.rs:76-79](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L76-L79)）。
- **向量列约定**为 `FixedSizeList<Float16/Float32>`，维度写在类型里；`simple.rs` 的 `create_some_records`（[第 60-89 行](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs#L60-L89)）是构造它的标准样板。
- LanceDB 通过扫描 schema **自动识别向量列**：外层 `FixedSizeList` + 子字段浮点（[inspect.rs:52-56](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/inspect.rs#L52-L56)），维度从类型直接取出（[create_index.rs:101-106](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/create_index.rs#L101-L106)）。
- 通用数据接入靠两条窄抽象：同步的 `RecordBatchReader`/`IntoArrow` 与异步的 `RecordBatchStream`/`IntoArrowStream`，二者都用 blanket 实现让标准 Arrow 类型免适配（[arrow.rs:120-161](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/arrow.rs#L120-L161)）。
- 选择 Arrow 的根本收益是**零拷贝跨语言**与**生态复用**（DataFusion / Polars / Lance 列式格式对齐）。

## 7. 下一步学习建议

本讲只解决了「数据长什么样、怎么塞进去」。接下来建议：

- **u2-l2（Table 抽象层）**：数据落盘后，`Table` / `BaseTable` / `NativeTable` 三层抽象如何让你读 schema、数行数、打开表。
- **u2-l3（数据写入与 Scannable）**：本讲提到的 `Scannable` 如何在 `IntoArrow` 之上进一步统一多种数据源，以及流式写入。
- **u3-l2（向量相似度搜索）**：本讲构造的 `FixedSizeList` 向量列，将被 `nearest_to` 消费，返回带 `_distance` 列的结果。
- 若想直接看 LanceDB 官方对向量列的完整说明，可继续阅读 crate 文档 [rust/lancedb/src/lib.rs:68-126](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L68-L126)。
