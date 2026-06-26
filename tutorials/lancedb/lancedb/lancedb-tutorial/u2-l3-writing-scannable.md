# 数据写入与 Scannable 抽象

## 1. 本讲目标

本讲承接 u2-l2（Table 三层抽象），专门拆解 LanceDB 的「数据写入」这一条链路。学完后你应当能够：

- 说清 `create_table` 与 `add` 这两个写入入口为什么能用同一种签名接收 `RecordBatch`、`Vec<RecordBatch>`、`RecordBatchReader`、异步流等形形色色的数据源。
- 理解 [`Scannable`] trait 的四个方法（`schema`、`scan_as_stream`、`num_rows`、`rescannable`）各自表达的能力，以及「可重扫（rescannable）」对写入重试与并行度的影响。
- 读懂 `add` 从用户数据到 Lance 列式写入之间的预处理流水线（嵌入计算、类型 cast、NaN 校验、并行分区）。
- 能够为 LanceDB 实现一个自定义的 `Scannable` 数据源，并把它写入表中。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（均在前置讲义中讲过）：

- **Apache Arrow 数据模型**（u1-l4）：LanceDB 的 schema 用 `arrow_schema::Schema`、数据用 `RecordBatch` 表示；向量列约定为 `FixedSizeList<Float32/Float16>`。
- **Table 三层抽象**（u2-l2）：对外轻量句柄 `Table` 持有 `Arc<dyn BaseTable>`，本地实现是 `NativeTable`，它把调用翻译为底层 Lance `Dataset` 的操作。
- **Builder + execute 风格**（u1-l3 / u2-l1）：所有执行 IO 的操作都先返回一个 Builder，调用 `.execute().await` 才真正生效。

此外需要一点 Rust 前置：泛型约束 `<T: Trait + 'static>`、trait 对象 `Box<dyn Trait>`、以及 `async_trait` 的基本概念。本讲会在用到时简要解释。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `rust/lancedb/src/data/scannable.rs` | 定义 `Scannable` trait，以及 `RecordBatch`、`Vec<RecordBatch>`、`RecordBatchReader`、异步流四种内置实现；还有 `WithEmbeddingsScannable`、`PeekedScannable` 等包装器 |
| `rust/lancedb/src/table/add_data.rs` | 定义 `AddDataBuilder`，把用户数据组装成可执行的写入计划（嵌入、cast、NaN 校验） |
| `rust/lancedb/src/table.rs` | 提供 `Table::add` 入口，以及 `NativeTable::add` 的真正写入实现（含并行度估算） |
| `rust/lancedb/src/connection.rs` / `rust/lancedb/src/connection/create_table.rs` | 提供 `create_table` 入口与 `CreateTableBuilder` |
| `rust/lancedb/examples/simple.rs` | 官方最小示例，演示 `create_table` + `add` 的实际调用顺序 |

## 4. 核心概念与源码讲解

### 4.1 写入入口：add 与 create_table 如何统一接收数据

#### 4.1.1 概念说明

写入数据库时，用户手里的数据形态千差万别：可能是一个整块的 `RecordBatch`，可能是一批 batch 的 `Vec`，可能是一个来自文件读取器的 `RecordBatchReader`，也可能是某个异步计算源源不断吐出的流。如果 LanceDB 为每种数据源都写一套不同的写入方法，API 会爆炸。

LanceDB 的做法是用一个 trait——`Scannable`——来表达「任何能被扫描成一批 `RecordBatch` 的东西」。两个写入入口 `create_table`（建表并写入首批数据）和 `add`（向已有表追加/覆盖数据）都用 `<T: Scannable>` 作为参数类型。这样无论你传入哪种数据源，都会先被装箱成 `Box<dyn Scannable>`，再走同一条统一的写入流水线。

> 术语：**装箱成 trait 对象**（`Box<dyn Scannable>`）就是把一个实现了 `Scannable` 的具体类型，通过 `Box::new(...)` 转成堆上、类型被擦除的指针。这样调用方不需要在编译期知道具体类型，可以用同一套代码处理所有数据源。

#### 4.1.2 核心流程

两个入口的写入流程几乎对称：

```text
用户调用                              装箱                  进入流水线
─────────                            ─────                 ─────────
Connection::create_table(name, data)  → Box<dyn Scannable>  → CreateTableBuilder → Database::create_table → Lance 写入首批数据
Table::add(data)                      → Box<dyn Scannable>  → AddDataBuilder     → BaseTable::add       → Lance append/overwrite
```

二者的关键设计：

1. 参数都是泛型 `<T: Scannable + 'static>`，函数体内 `Box::new(data)` 完成类型擦除。
2. 返回的都是一个 Builder，真正的写入发生在 `.execute().await`。
3. 装箱后的 `Box<dyn Scannable>` 沿着 `AddDataBuilder` / `CreateTableBuilder` 一路传递，最终交给底层 Lance。

#### 4.1.3 源码精读

先看 `add` 的入口签名——它就是一行装箱：

[rust/lancedb/src/table.rs:1029-1035](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1029-L1035) —— `Table::add` 把任意 `Scannable` 数据装箱成 `Box<dyn Scannable>`，连同父表句柄和嵌入注册表一起交给 `AddDataBuilder`。

```rust
pub fn add<T: Scannable + 'static>(&self, data: T) -> AddDataBuilder {
    AddDataBuilder::new(
        self.inner.clone(),
        Box::new(data),
        Some(self.embedding_registry.clone()),
    )
}
```

`create_table` 的入口完全同构：

[rust/lancedb/src/connection.rs:423-435](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L423-L435) —— `Connection::create_table` 同样把首个数据集 `Box::new(initial_data)` 装箱后传入 `CreateTableBuilder`。

注意 `create_empty_table`（紧随其后，行 443-449）其实复用了 `create_table`：它先构造一个 `RecordBatch::new_empty(schema)` 作为「首批数据」，于是空表也走同一条链路。这就是为什么本讲的抽象对「有数据建表」和「按 schema 建空表」都适用。

`AddDataBuilder` 持有这个装箱数据，字段名叫 `data`：

[rust/lancedb/src/table/add_data.rs:54-63](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/add_data.rs#L54-L63) —— `AddDataBuilder` 把 `data: Box<dyn Scannable>` 与写入模式、写选项、NaN 处理策略、嵌入注册表、写入并行度等配置项并列存放。

官方示例 `simple.rs` 演示了这两个入口的实际调用顺序：

[rust/lancedb/examples/simple.rs:94-104](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs#L94-L104) —— 先用 `db.create_table("my_table", initial_data)` 建表写首批，再用 `tbl.add(new_data)` 追加第二批；两处传入的都是 `RecordBatch`。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `add` 对多种数据源的统一接收能力。

**操作步骤**：

1. 新建一个临时 Rust 项目或直接参考 `rust/lancedb/examples/simple.rs` 写一个小 `#[tokio::main]` 程序。
2. 用 `connect("memory://").execute().await?` 连接一个内存数据库（`memory://` 不落盘，最适合做练习）。
3. 先 `create_table("t", batch1)` 建表（`batch1` 是一个 3 行的 `RecordBatch`）。
4. 再 `table.add(batch2).execute().await?` 追加一个 2 行的 `RecordBatch`。
5. 再 `table.add(vec![batch3, batch4]).execute().await?` 追加一个 `Vec<RecordBatch>`（2 个 batch）。

**需要观察的现象**：每次 `add` 都返回 `Ok(AddResult)`，不报类型错误。

**预期结果**：最后 `table.count_rows(None).await?` 应当返回 `3 + 2 + (batch3 行数 + batch4 行数)`。如果你给 `batch3`、`batch4` 各放 2 行，最终就是 9 行。

**待本地验证**：具体行数取决于你构造的 batch 大小；如果你的 schema 与表 schema 不完全一致（例如列顺序不同、整型宽度不同），写入仍可能成功——因为流水线会做 cast（见 4.3）。这部分的边界行为建议本地实际跑一遍观察。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `add` 的签名是 `add<T: Scannable + 'static>` 而不是直接 `add(data: Box<dyn Scannable>)`？

**参考答案**：用泛型 `<T: Scannable>` 时，调用方可以直接传 `batch`（一个 `RecordBatch` 值），编译器在调用点推断 `T = RecordBatch` 并自动 `Box::new`，调用代码更简洁（`table.add(batch)` 而非 `table.add(Box::new(batch) as Box<dyn Scannable>)`）。`'static` 约束是为了让该类型能被安全地装箱并在异步任务间传递。

**练习 2**：`create_empty_table` 为什么也能复用 `create_table` 这条写入链路？

**参考答案**：因为空表的数据源被构造成一个 `RecordBatch::new_empty(schema)`——它本身也实现了 `Scannable`（schema 已知、`num_rows` 为 0、可重扫）。所以「建空表」本质上是「写入一个零行的批次」，走完全相同的流水线。

---

### 4.2 Scannable trait：数据源的统一契约

#### 4.2.1 概念说明

`Scannable` 是数据写入侧最核心的抽象。它的职责不是「存储数据」，而是**声明这个数据源具备哪些能力**，好让写入流水线据此决策。核心有三类能力：

- **能给出 schema**：`schema()` 返回 `SchemaRef`，即这批数据的列定义。建表时「首批数据的 schema 即表 schema」就依赖它。
- **能被扫描成流**：`scan_as_stream()` 把数据转成 `SendableRecordBatchStream`（一个可在异步任务间传递的 `RecordBatch` 流）。流水线最终就是消费这个流。
- **可选的「元信息」**：`num_rows()`（行数提示）和 `rescannable()`（能否从头重新读取），帮助流水线估算写入并行度、决定失败时能否重试。

> 术语：**`SendableRecordBatchStream`** 是 LanceDB 内部对异步 RecordBatch 流的别名（`Box<dyn Stream<Item = Result<RecordBatch>> + Send + Unpin>`）。把同步的、整块的数据「流式化」，是为了统一处理小批量与大批量数据。

#### 4.2.2 核心流程

一个 `Scannable` 数据源在被写入时，典型经历三步：

```text
1. schema()           → 流水线据此校验/推断表 schema（首批数据时即表 schema）
2. scan_as_stream()   → 数据被转成流，交给 DataFusion 执行计划逐批处理
3. num_rows()/rescannable() → 估算并行度、决定是否可重试
```

`scan_as_stream` 的语义分两类，这是理解本模块的关键：

- **可重扫源（rescannable = true）**：如内存中的 `RecordBatch` / `Vec<RecordBatch>`。`scan_as_stream` 可被多次调用，每次都返回克隆出来的新数据流。
- **不可重扫源（rescannable = false）**：如 `RecordBatchReader`、网络流。数据只能被消费一次，第二次调用 `scan_as_stream` 会返回「首项即错误」的流，明确报错而不是悄悄返回空数据。

#### 4.2.3 源码精读

先看 trait 定义本身，它只有 4 个方法，且后两个有默认实现：

[rust/lancedb/src/data/scannable.rs:28-58](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/scannable.rs#L28-L58) —— `Scannable` trait：`schema` 与 `scan_as_stream` 是必须实现的；`num_rows` 默认返回 `None`、`rescannable` 默认返回 `false`。

```rust
pub trait Scannable: Send {
    fn schema(&self) -> SchemaRef;
    fn scan_as_stream(&mut self) -> SendableRecordBatchStream;

    fn num_rows(&self) -> Option<usize> { None }
    fn rescannable(&self) -> bool { false }
}
```

trait 还要求 `Send`，因为写入在异步多任务环境下进行。注意 `scan_as_stream` 取 `&mut self`——这正是因为某些源（reader/stream）消费一次后状态会改变，需要可变借用。

文件顶部的模块文档精炼地说明了设计意图：

[rust/lancedb/src/data/scannable.rs:4-8](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/scannable.rs#L4-L8) —— 注释点明 `Scannable` 让数据源「声明能力（行数、可重扫性），使写入流水线能更好地决策写入并行度与重试策略」。

#### 4.2.4 代码实践

**实践目标**：直接观察一个 `Scannable` 的能力声明，不经过完整写入。

**操作步骤**：

1. 在测试或小程序里构造 `let batch = record_batch!(("id", Int64, [0,1,2])).unwrap();`
2. 调用 `batch.schema()`、`batch.num_rows()`、`batch.rescannable()`。
3. 调用 `let s = batch.scan_as_stream();`，用 `futures::TryStreamExt::try_collect` 收集成 `Vec<RecordBatch>`。

**需要观察的现象**：`num_rows()` 返回 `Some(3)`，`rescannable()` 返回 `true`，`scan_as_stream` 可以连续调用两次都拿到完整数据。

**预期结果**：这与仓库内置测试 `test_record_batch_rescannable` 的断言一致（见下方练习）。这是「待本地验证」类实践，跑通后你会直观感受到「可重扫」的含义。

#### 4.2.5 小练习与答案

**练习 1**：阅读源码文件底部的测试 `test_record_batch_rescannable`（[scannable.rs:495-509](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/scannable.rs#L495-L509)），它对同一个 `batch` 调用了几次 `scan_as_stream`？为什么允许这么做？

**参考答案**：调用了两次。因为 `RecordBatch` 是内存数据，`rescannable()` 返回 `true`；实现里每次 `scan_as_stream` 都先 `self.clone()` 再产出流，所以多次调用互不影响。测试断言两次收集到的 batch 完全相等。

**练习 2**：如果 `rescannable` 默认返回 `false`，而某个内置实现没有覆盖它，会发生什么？

**参考答案**：流水线会把它当作「不可重扫」处理——即只能消费一次、写入失败时无法通过重扫重试。这正是流式源的合理默认。要表达「可重扫」，实现必须显式覆盖 `rescannable()` 返回 `true`（如 `RecordBatch`、`Vec<RecordBatch>` 所做的那样）。

---

### 4.3 四种内置 Scannable 实现

#### 4.3.1 概念说明

光有 trait 不够，LanceDB 为最常见的数据形态都实现了 `Scannable`，于是用户「开箱即用」就能把多种数据源直接喂给 `add` / `create_table`：

| 实现类型 | 可重扫 | 行数提示 | 典型来源 |
| --- | --- | --- | --- |
| `RecordBatch` | 是 | `Some(num_rows)` | 内存里的一整块数据 |
| `Vec<RecordBatch>` | 是 | 各 batch 行数之和 | 分批构造好的内存数据 |
| `Box<dyn RecordBatchReader + Send>` | 否 | `None` | 文件读取器、Parquet reader |
| `SendableRecordBatchStream` | 否 | `None` | 异步计算、网络流、DataFusion 计划 |

#### 4.3.2 核心流程

四种实现的核心差异在于 `scan_as_stream` 如何「流式化」数据，以及如何处理「第二次调用」：

- **`RecordBatch`**：克隆自身，产出「只含一个 batch」的流；可重扫。
- **`Vec<RecordBatch>`**：克隆整个 Vec，产出多 batch 的流；可重扫。空 Vec 会立即报错（不能扫描空 Vec）。
- **`RecordBatchReader`**：用 `std::mem::replace` 把自身替换成一个「一迭代就报错」的占位 reader，真正的 reader 被搬走；再用 `spawn_blocking` + tokio channel 把同步 reader 桥接成异步流。第二次调用自然拿到占位 reader，首项即错误。
- **`SendableRecordBatchStream`**：同样用 `mem::replace` 把自身替换成错误流，原流被搬走返回。

> 术语：**`spawn_blocking`** 是 tokio 提供的，把「同步阻塞」的计算（如迭代一个 `RecordBatchReader`）丢到专门的阻塞线程池执行，避免阻塞异步运行时；结果通过 `mpsc::channel` 传回异步侧。

#### 4.3.3 源码精读

**`RecordBatch` 实现**——最简单的可重扫源：

[rust/lancedb/src/data/scannable.rs:70-91](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/scannable.rs#L70-L91) —— `scan_as_stream` 先 `self.clone()` 再包成 `SimpleRecordBatchStream`，`num_rows` 返回 `Some`，`rescannable` 返回 `true`。

```rust
impl Scannable for RecordBatch {
    fn scan_as_stream(&mut self) -> SendableRecordBatchStream {
        let batch = self.clone();
        let schema = batch.schema();
        Box::pin(SimpleRecordBatchStream {
            schema,
            stream: once(async move { Ok(batch) }),
        })
    }
    fn num_rows(&self) -> Option<usize> { Some(Self::num_rows(self)) }
    fn rescannable(&self) -> bool { true }
}
```

**`Box<dyn RecordBatchReader + Send>` 实现**——不可重扫源的代表，重点看它如何「桥接同步 reader 到异步流」并「防止第二次悄悄返回空数据」：

[rust/lancedb/src/data/scannable.rs:129-165](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/scannable.rs#L129-L165) —— 先用 `std::mem::replace` 把 `self` 换成一个会报错的占位 reader，真正的 reader 被 `spawn_blocking` 线程逐批读取，经 `mpsc::channel` 桥接为异步流。注意此实现**没有**覆盖 `rescannable()`，故默认 `false`。

关键两行（行 145 与 149）：

```rust
let reader = std::mem::replace(self, err_reader);   // self 被换成占位 reader
tokio::task::spawn_blocking(move || {               // 阻塞 reader 在独立线程跑
    for batch_result in reader { ... }
});
```

**`SendableRecordBatchStream` 实现**与 reader 思路相同：

[rust/lancedb/src/data/scannable.rs:167-186](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/scannable.rs#L167-L186) —— `scan_as_stream` 用 `mem::replace` 把自身换成「首项即错误」的流，原流被返回；同样默认不可重扫。

这条「第二次调用明确报错」的设计，由测试 `test_reader_not_rescannable` 与 `test_stream_not_rescannable` 固化（[scannable.rs:541-583](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/scannable.rs#L541-L583)）：断言第二次 `scan_as_stream` 的首项是 `Err`。

此外，`Box<dyn Scannable>` 还实现了 DataFusion 的 `StreamingWriteSource`：

[rust/lancedb/src/data/scannable.rs:188-197](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/scannable.rs#L188-L197) —— 这让任意 `Scannable` 都能被嵌入 DataFusion 执行计划（`into_stream` 内部就是调用 `scan_as_stream`），是写入流水线把 Scannable 当作数据源节点的桥梁。

#### 4.3.4 代码实践

**实践目标**：用同一个 `test_add_with_data` 通用测试函数验证四种数据源都能写入。

**操作步骤**：

1. 阅读 [add_data.rs:288-340](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/add_data.rs#L288-L340) 中的辅助测试 `test_add_with_data<T: Scannable + 'static>`，它先建一张 3 行的表，再 `table.add(data)`，最后断言行数为 5。
2. 注意它被四个测试复用：`test_add_with_batch`（传 `RecordBatch`）、`test_add_with_vec_batch`（传 `Vec<RecordBatch>`）、`test_add_with_record_batch_reader`（传 `Box<dyn RecordBatchReader>`）、`test_add_with_stream`（传 `SendableRecordBatchStream`）。
3. 运行这些测试：`cargo test --features remote -p lancedb --lib table::add_data::tests`。

**需要观察的现象**：四种数据源走同一个泛型函数，全部通过。

**预期结果**：四个测试均 PASS，`count_rows` 全部返回 5（3 初始 + 2 追加）。这直接证明了「`add` 对四种 Scannable 实现统一接收」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `RecordBatchReader` 的实现要用 `std::mem::replace` 把自身替换掉，而不是直接读取？

**参考答案**：因为 `scan_as_stream` 只拿到 `&mut self`，无法获取 reader 的所有权去搬进 `spawn_blocking` 线程。`mem::replace` 用一个「会报错的占位 reader」原地换出真正的 reader，从而拿到所有权；同时保证第二次调用时，`self` 已是占位 reader，首项即报错，避免悄悄返回空数据。

**练习 2**：四种内置实现里，哪两种会向流水线提供 `num_rows` 提示？这对写入有什么影响？

**参考答案**：`RecordBatch` 和 `Vec<RecordBatch>` 返回 `Some(...)`，reader 与 stream 返回 `None`。`num_rows` 提示会参与写入并行度估算（见 4.5）：有提示时能更准确地估算总数据量，从而选择更合理的写分区数；无提示时流水线只能用首批 batch 的行数作为下界估计。

---

### 4.4 写入预处理流水线：嵌入、cast、NaN 校验

#### 4.4.1 概念说明

用户数据装箱成 `Box<dyn Scannable>` 后，并不会直接落盘。`AddDataBuilder::into_plan` 会把它组装成一个 **DataFusion 执行计划**，并在「原始数据流」和「最终写盘」之间串上若干预处理算子：

- **嵌入计算**：若表定义了嵌入列（见 u8-l1），而用户数据没有提供向量列，则在写入时自动调用注册的 `EmbeddingFunction` 补算向量。
- **类型 cast**：把输入 schema「按列名」对齐到表 schema，并尝试把列类型转换成表要求的类型（如 `Int32 → Int64`、`LargeUtf8 → Utf8`、`List<Float64> → FixedSizeList<Float32>`）。
- **NaN 校验**：默认拒绝含 NaN 的向量列（NaN 无法被索引、不可搜索），可配置为保留但跳过索引。
- **schema 校验**：追加时检查「输入列名是否都存在于表中」，多出列直接报错。

> 术语：**执行计划（ExecutionPlan）** 是 DataFusion 对数据处理管线的抽象：一组算子节点连成 DAG，每个算子消费上游的流、产出新的流。LanceDB 借用它把「数据源 + cast + 嵌入 + NaN 过滤」串成一条统一流水线，最后整体交给 Lance 写入。

#### 4.4.2 核心流程

`AddDataBuilder::into_plan` 的组装顺序大致是：

```text
self.data (Box<dyn Scannable>)
   │  scannable_with_embeddings(...)    ← 若有嵌入列，包成 WithEmbeddingsScannable
   ▼
ScannableExec(封装后的 Scannable)        ← 执行计划的数据源节点
   │  cast_to_table_schema(...)          ← 按表 schema 逐列 cast（覆盖模式跳过）
   ▼
cast 后的计划
   │  reject_nan_vectors(...)            ← 默认拒绝 NaN 向量
   ▼
PreprocessingOutput { plan, rescannable, overwrite, ... }
```

注意：嵌入计算发生在 cast **之前**（先在数据源节点里算出向量列），而 NaN 校验发生在 cast **之后**。覆盖（Overwrite）模式会跳过 cast，因为此时输入 schema 直接取代表 schema。

#### 4.4.3 源码精读

`into_plan` 是整条流水线的总装入口：

[rust/lancedb/src/table/add_data.rs:159-203](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/add_data.rs#L159-L203) —— 关键步骤：(1) 非 overwrite 时先 `validate_schema` 校验列名；(2) `scannable_with_embeddings` 注入嵌入计算；(3) 记录 `rescannable`；(4) `ScannableExec::new` 把数据包成执行计划节点；(5) `cast_to_table_schema` 做类型对齐（overwrite 跳过）；(6) 按 `on_nan_vectors` 决定是否 `reject_nan_vectors`。

其中嵌入注入是一个独立的装饰器：

[rust/lancedb/src/data/scannable.rs:251-319](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/scannable.rs#L251-L319) —— `WithEmbeddingsScannable` 包装内层 Scannable，在 `scan_as_stream` 里对每个 batch 调用 `compute_embeddings_for_batch` 补算向量列，再**按列名**（而非位置）对齐到输出 schema。注释（行 274-280）解释了为何必须按列名匹配：模式演化后列顺序可能变化，按位置匹配会张冠李戴。

`scannable_with_embeddings` 是它的工厂函数：

[rust/lancedb/src/data/scannable.rs:321-360](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/scannable.rs#L321-L360) —— 遍历表定义的列，凡 `ColumnKind::Embedding` 的列就去注册表里找对应函数；找不到则返回 `Error::EmbeddingFunctionNotFound`；没有任何嵌入列则原样返回内层 Scannable（透传）。

schema 校验只校验「列名存在性」，类型差异留给 cast：

[rust/lancedb/src/table/add_data.rs:228-250](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/add_data.rs#L228-L250) —— `validate_fields` 按**列名**匹配（允许列顺序不同）；输入多出的列报错；表里有而输入没有的列允许（写入时填 null）；类型不一致在此阶段不报错，留给后续 cast；struct 列递归校验子字段。

这套行为被测试 `test_add_subschema`（[add_data.rs:884-987](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/add_data.rs#L884-L987)）和 `test_add_casts_to_table_schema`（[add_data.rs:745-792](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/add_data.rs#L745-L792)）固化：前者验证「子集列 + 多列报错」，后者验证「整型上转、字符串重编码、list→fixedsizelist 的 cast」。

NaN 行为由测试 `test_add_rejects_nan_vectors`（[add_data.rs:838-882](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/add_data.rs#L838-L882)）固化：默认拒绝并报错；设为 `NaNVectorBehavior::Keep` 后允许写入但该向量不参与索引。

#### 4.4.4 代码实践

**实践目标**：观察 cast 流水线的「宽容性」与边界。

**操作步骤**：

1. 用 `create_empty_table` 建一张表，schema 为 `[id:Int64, text:Utf8, embedding:FixedSizeList<Float32,4>]`。
2. 构造一个**类型不同但兼容**的输入 batch：`id` 用 `Int32`、`text` 用 `LargeUtf8`、`embedding` 用 `List<Float64>`（长度 4）。
3. `table.add(batch).execute().await`，观察是否成功。
4. 再构造一个含 NaN 的 `FixedSizeList<Float32,4>` 向量 batch，先默认 `add`（应报错），再用 `.on_nan_vectors(NaNVectorBehavior::Keep)` 重试。

**需要观察的现象**：第 3 步成功（流水线做了 cast）；第 4 步第一次报错信息含 "NaN"，第二次成功。

**预期结果**：与 `test_add_casts_to_table_schema` 和 `test_add_rejects_nan_vectors` 的断言一致。这是「待本地验证」类实践。

#### 4.4.5 小练习与答案

**练习 1**：如果输入 batch 多带了表里没有的一列，`add` 会怎样？依据是哪段代码？

**参考答案**：会报 `Error::InvalidInput { message: "field '...' does not exist in table schema" }`。依据是 `validate_fields`（[add_data.rs:232-249](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/add_data.rs#L232-L249)）中「输入有而 table 没有则返回错误」的分支。注意反过来（表有而输入没有）是允许的，会填 null。

**练习 2**：为什么 `WithEmbeddingsScannable` 要按列名而不是按位置匹配列？

**参考答案**：因为嵌入列是「算好后追加在 batch 末尾」的，而表 schema 里的列顺序可能因模式演化（`add_columns`）而不同——例如表是 `[text, embedding, score]`，而算完嵌入的 batch 是 `[text, score, embedding]`。按位置匹配会把 `score` 错当成 `embedding`，导致类型 cast 失败甚至数据错位（见回归测试 [scannable.rs:988-1045](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/scannable.rs#L988-L1045)，对应 issue #3136）。

---

### 4.5 PeekedScannable 与写入并行度估算

#### 4.5.1 概念说明

写入大表时，并行写入能显著提速。但并行度该开多少？开太多反而因调度开销变慢。LanceDB 的策略是：**窥探（peek）数据源的第一个 batch**，用它估算「每行多大、总共多少行」，从而算出一个合适的写分区数。难点在于——peek 会消费一个 batch，必须保证它不丢失，后续写入仍能拿到完整数据。

`PeekedScannable` 就是为此设计的包装器：它缓冲第一个 batch，并在真正 `scan_as_stream` 时把它「拼回」流的开头。配合 `estimate_write_partitions` 函数完成并行度估算。

#### 4.5.2 核心流程

`NativeTable::add` 的并行度决策流程：

```text
若用户显式设了 write_parallelism → 直接用
否则：
   1. 把 data 包成 PeekedScannable
   2. peek() 取第一个 batch（若无 → 1 个分区）
   3. estimate_write_partitions(首批字节数, 首批行数, num_rows提示, 最大分区数=CPU核数)
   4. 把 PeekedScannable 放回 add.data（保留首批数据）
得到 num_partitions → 后续 RepartitionExec 按该数做 RoundRobin 分区并行写入
```

`estimate_write_partitions` 的目标是「每分区约 100 万行或约 2GB，且不超过 CPU 核数」：

\[ \text{partitions} = \min\Big(\max\big(\lceil \tfrac{\text{total\_rows}}{10^6} \rceil,\ \lceil \tfrac{\text{total\_bytes}}{2 \times 2^{30}} \rceil,\ 1\big),\ \text{max\_partitions}\Big) \]

其中 `total_rows` 优先用 `num_rows` 提示，无提示时退化为首批行数（下界估计）。

#### 4.5.3 源码精读

`NativeTable::add` 中并行度估算的关键段落：

[rust/lancedb/src/table.rs:2657-2685](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L2657-L2685) —— 用 `PeekedScannable::new(add.data)` 包一层，`peek().await` 取首个 batch，调 `estimate_write_partitions`（`max_partitions` 取 `get_num_compute_intensive_cpus()`），最后把 `Box::new(peeked)` 写回 `add.data`——这一步保证 peek 拿走的首批数据不会丢。

```rust
let mut peeked = PeekedScannable::new(add.data);
let n = if let Some(first_batch) = peeked.peek().await {
    let max_partitions = lance_core::utils::tokio::get_num_compute_intensive_cpus();
    estimate_write_partitions(
        first_batch.get_array_memory_size(),
        first_batch.num_rows(),
        peeked.num_rows(),
        max_partitions,
    )
} else { 1 };
add.data = Box::new(peeked);   // 首批数据被「拼回」，不会丢
```

随后（行 2700-2708）若 `num_partitions > 1`，用 `RepartitionExec` + `Partitioning::RoundRobinBatch(num_partitions)` 真正并行写入。

`PeekedScannable` 如何保证「peek 后数据完整」：

[rust/lancedb/src/data/scannable.rs:363-414](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/scannable.rs#L363-L414) —— `peek()` 取内层流的第一个 `Some(Ok(batch))`，把 batch 克隆存进 `self.peeked`，剩余流存进 `self.stream`。对错误或空流则存进 `first_error`/`stream` 并返回 `None`。

[rust/lancedb/src/data/scannable.rs:429-460](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/scannable.rs#L429-L460) —— `scan_as_stream` 把缓冲的 `peeked` batch 用 `once(Ok(batch))` 拼到剩余流 `rest` 的开头（`prepend.chain(rest)`），从而还原完整数据；若 peek 遇到错误，则把错误重新放出，绝不静默丢弃。

并行度估算函数本体：

[rust/lancedb/src/data/scannable.rs:472-487](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/scannable.rs#L472-L487) —— 先算 `bytes_per_row`，再分别按行数阈值（100 万/分区）和字节阈值（2GB/分区）取上限，最后与 1 取 max、与 `max_partitions` 取 min。

这套逻辑被测试 `estimate_write_partitions_tests`（[scannable.rs:761-808](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/scannable.rs#L761-L808)）和 `peeked_scannable_tests`（[scannable.rs:585-758](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/scannable.rs#L585-L758)）固化：覆盖「小数据 1 分区」「按行数放大」「按字节放大」「封顶」「空首批」「peek 后扫描数据完整」「错误传播」等场景。

#### 4.5.4 代码实践

**实践目标**：亲手实现一个自定义 `Scannable` 数据源，并用它写入。

**操作步骤**（这是本讲核心实践）：

1. 定义一个结构体持有 `schema` 与若干 `RecordBatch`，并为它实现 `Scannable`：

   ```rust
   // 示例代码：一个最简自定义 Scannable，内部维护一个 batch 队列
   use lancedb::data::scannable::Scannable;
   use lancedb::arrow::{SendableRecordBatchStream, SimpleRecordBatchStream};
   use arrow_schema::SchemaRef;
   use arrow_array::RecordBatch;
   use futures::stream;

   struct MySource {
       schema: SchemaRef,
       batches: Vec<RecordBatch>, // 持有原始数据，可重扫
   }

   impl Scannable for MySource {
       fn schema(&self) -> SchemaRef { self.schema.clone() }
       fn scan_as_stream(&mut self) -> SendableRecordBatchStream {
           let batches = self.batches.clone();          // 克隆 → 可重扫
           let schema = self.schema.clone();
           Box::pin(SimpleRecordBatchStream::new(
               stream::iter(batches.into_iter().map(Ok)),
               schema,
           ))
       }
       fn num_rows(&self) -> Option<usize> {
           Some(self.batches.iter().map(|b| b.num_rows()).sum())
       }
       fn rescannable(&self) -> bool { true }
   }
   ```
   > 上面的 `SimpleRecordBatchStream::new` 接受任意 `Stream<Item = Result<RecordBatch>>`；`SendableRecordBatchStream`/`SimpleRecordBatchStream` 定义在 `rust/lancedb/src/arrow.rs`。该结构体为示例代码，非项目原有代码。

2. 连接 `connect("memory://").execute().await?`，建一张表 `create_table("t", batch1)`。
3. 第一批：`table.add(batch2).execute().await?`（直接传 `RecordBatch`）。
4. 第二批：`table.add(vec![batch3a, batch3b]).execute().await?`（传 `Vec<RecordBatch>`）。
5. 第三批：`table.add(MySource { schema, batches: vec![batch4] }).execute().await?`（传自定义源）。

**需要观察的现象**：三批 `add` 全部成功，自定义源也被流水线接受（会被 `Box::new` 成 `Box<dyn Scannable>`，并参与 peek 估算并行度）。

**预期结果**：`table.count_rows(None).await?` 等于所有批次行数之和。若你给 `batch1..batch4` 分别放 3、2、2、2 行，最终应为 9 行。

**待本地验证**：自定义源的 `SimpleRecordBatchStream::new` 签名请以本地 `arrow.rs` 实际定义为准（不同版本字段名可能略有差异）；若编译报错，可参照 `scannable.rs` 内置 `Vec<RecordBatch>` 实现（[scannable.rs:93-127](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/scannable.rs#L93-L127)）调整。

#### 4.5.5 小练习与答案

**练习 1**：`PeekedScannable` 的 `peek()` 如果遇到第一个 item 是错误（`Some(Err(e))`），会返回什么？数据会丢吗？

**参考答案**：`peek()` 返回 `None`（见 [scannable.rs:403-407](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/scannable.rs#L403-L407)），但错误被存进 `self.first_error`。后续 `scan_as_stream` 会把这个错误重新放出（[scannable.rs:433 与 447-454](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/scannable.rs#L433-L454)），所以数据/错误都不会丢。测试 `test_error_in_first_batch_propagates` 固化了这一点。

**练习 2**：假设一批数据有 250 万行、每行 24 字节，`max_partitions = 8`，`estimate_write_partitions` 会返回几？

**参考答案**：`bytes_per_row = 24`，`total_bytes = 2_500_000 × 24 = 60_000_000`（约 57MB）。按行数：`ceil(2_500_000 / 1_000_000) = 3`；按字节：`ceil(60_000_000 / 2_147_483_648) = 1`。`max(3, 1, 1) = 3`，再 `min(3, 8) = 3`，返回 3。这与测试 `test_scales_by_row_count`（[scannable.rs:770-775](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/data/scannable.rs#L770-L775)）的断言一致。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个端到端的小任务：

**任务**：写一个 `#[tokio::main]` 程序，用 `memory://` 数据库，演示完整的写入链路与能力声明。

要求：

1. **建表**：用 `create_table` 写入一个含 `id: Int32` 与 `vector: FixedSizeList<Float32, 4>` 两列的首批 `RecordBatch`（2 行）。
2. **追加批次一**：用 `table.add(batch)` 追加一个 `RecordBatch`（2 行），观察返回的 `AddResult`。
3. **追加批次二**：用 `table.add(vec![b1, b2])` 追加一个 `Vec<RecordBatch>`（共 2 行）。
4. **自定义源追加**：实现 4.5.4 中的 `MySource`（让它返回一个含 2 行的流），追加为第三批。
5. **观察 cast**：再追加一个「`id` 用 `Int64`、`vector` 用 `List<Float64>`（长度 4）」的兼容类型 batch（2 行），验证 cast 流水线让它成功写入。
6. **校验**：最后 `table.count_rows(None)` 应为 `2 + 2 + 2 + 2 + 2 = 10`；并用 `table.schema().await?` 确认 schema 仍是建表时的 `Int32 + FixedSizeList<Float32,4>`（说明 cast 把输入类型转回了表类型）。

**进阶思考**（不必写代码）：如果你把第 5 步的 `vector` 改成长度 3 的 list，会发生什么？（提示：参考 `test_add_rejects_bad_vector_dimensions` [add_data.rs:794-836](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/add_data.rs#L794-L836)，cast 到定长 4 的 list 会失败，写入报错。）

## 6. 本讲小结

- `create_table` 与 `add` 两个写入入口都用 `<T: Scannable + 'static>` 接收数据，函数体内 `Box::new` 装箱成 `Box<dyn Scannable>`，从而用同一套签名接纳 `RecordBatch`、`Vec`、reader、异步流等所有数据源。
- `Scannable` trait 用四个方法（`schema` / `scan_as_stream` / `num_rows` / `rescannable`）声明数据源的能力；后两者有默认实现，让流水线据此决策写入并行度与重试策略。
- 四种内置实现按「可重扫」分两类：内存数据（`RecordBatch`、`Vec<RecordBatch>`）可重扫、提供行数提示；reader 与 stream 不可重扫、用 `mem::replace` 让第二次 `scan_as_stream` 明确报错而非静默返回空。
- `AddDataBuilder::into_plan` 把数据组装成 DataFusion 执行计划，依次串上嵌入计算（`WithEmbeddingsScannable`，按列名匹配）、类型 cast、NaN 校验、schema 校验；overwrite 模式跳过 cast。
- `NativeTable::add` 用 `PeekedScannable` 窥探首批、配合 `estimate_write_partitions`（目标每分区约 100 万行或 2GB，封顶 CPU 核数）估算写分区数，并通过 `RepartitionExec` 并行写入；`PeekedScannable` 保证 peek 走的首批数据不丢失。

## 7. 下一步学习建议

本讲聚焦「写入」。接下来建议：

- **u5-l2 数据增删改与模式演化**：本讲提到的 `update`、`delete`、`add_columns`、`merge_insert`（upsert）都建立在同样的写入抽象之上，可对照学习数据如何被修改与合并。
- **u8-l1 嵌入函数与注册表**：本讲多次出现 `WithEmbeddingsScannable` 与 `scannable_with_embeddings`，若想彻底搞懂「写入时自动算向量」，需深入 `embeddings.rs`。
- **重读 u2-l2**：把本讲的 `NativeTable::add`（`table.rs:2657`）与 u2-l2 讲的「NativeTable 把契约翻译为 Lance 调用」对照，体会 BaseTable trait 的统一价值——远程 `RemoteTable::add`（`remote/table.rs:1816`）走的是 HTTP，但对用户暴露的 `Table::add` 完全一致。
