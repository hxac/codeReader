# 查询基础：QueryBase 与 Select

## 1. 本讲目标

本讲是「查询与搜索」单元的第一讲，承接 u2-l2（Table 三层抽象）。学完本讲，你应当能够：

- 说清楚 `table.query()` 返回的是什么，以及 LanceDB 里「查询」有哪些形态；
- 熟练使用 `QueryBase` trait 提供的 `limit` / `offset` / `only_if` / `only_if_expr` / `select` 等链式方法来配置一次查询；
- 区分 `Select` 枚举的四种取值（`All` / `Columns` / `Dynamic` / `Expr`），并能根据需要选择合适的返回列控制方式；
- 用 `ExecutableQuery::execute` 把配置好的查询真正跑起来，拿到一个 Arrow `RecordBatch` 流。

本讲只覆盖「不带向量相似度」的基础扫描（plain scan），向量搜索（`nearest_to`）会在下一讲 u3-l2 展开。本讲对应的最小模块是 `query`。

## 2. 前置知识

在开始前，请确保你已经理解以下概念（它们都在前几讲建立过）：

- **Apache Arrow 与 RecordBatch**（u1-l4）：LanceDB 的数据模型基于 Arrow，查询结果就是一个 `RecordBatch` 的流。每一列是一个 `Array`，多列组成一个 `RecordBatch`，多个 `RecordBatch` 组成一条流。
- **Table 是一个轻量句柄**（u2-l2）：`Table` 内部持有 `Arc<dyn BaseTable>`，几乎所有方法都「委托」到底层实现。本讲的 `table.query()` 就是其中之一。
- **Builder + execute 风格**（u1-l3、u2-l1）：LanceDB 凡是涉及 I/O 的操作都先返回一个「构建器」，链式配置后调用 `.execute().await` 才真正执行。查询也遵循这一风格——只不过查询的「构建器」就是查询对象本身。

还需要两个本讲会用到的术语：

- **投影（projection）**：在 SQL 里指 `SELECT a, b FROM ...` 决定「返回哪些列」。LanceDB 用 `Select` 枚举来表达同样的概念。
- **谓词过滤（predicate filter）**：类似 SQL 的 `WHERE` 子句，用一个表达式决定「返回哪些行」。LanceDB 的 Rust 核心里对应方法叫 `only_if`（Python/TypeScript 绑定中命名为 `where`，是同一回事，只是名字不同）。

## 3. 本讲源码地图

本讲主要围绕 Rust 核心的查询模块，涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [rust/lancedb/src/query.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs) | 查询模块的核心，定义 `Select`、`QueryBase`、`ExecutableQuery`、`Query`、`VectorQuery`、`TakeQuery` 等 |
| [rust/lancedb/src/table.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs) | `Table::query()` 与 `Table::vector_search()` 入口 |
| [rust/lancedb/src/table/query.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/query.rs) | `AnyQuery` 枚举，把普通查询和向量查询统一成一种内部表示 |
| [rust/lancedb/src/expr.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/expr.rs) | 表达式构建辅助函数 `col` / `lit`，用于类型安全的过滤与投影 |
| [rust/lancedb/examples/simple.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs) | 最小可运行示例，演示 `query().limit(...)` 链式调用 |

---

## 4. 核心概念与源码讲解

### 4.1 查询的起点：query() 与三种查询形态

#### 4.1.1 概念说明

一切查询都从 `Table` 上的一个方法开始：

```rust
let query = table.query();   // 拿到一个 Query，还没执行
```

`query()` 返回的是一个 **`Query` 对象**，它本身还不会做任何 I/O。你可以把它想象成一条「还没点火的 SQL」：先把要查什么（限制几行、过滤什么、返回哪些列）配置好，最后调用 `execute()` 才真正去读表。

LanceDB 实际上有三种「查询对象」，本讲主要讲第一种 `Query`：

| 查询对象 | 含义 | 本讲是否展开 |
| --- | --- | --- |
| `Query` | 基础查询：扫描 / 过滤 / 投影，**不带**向量相似度 | ✅ 本讲主角 |
| `VectorQuery` | 向量查询：在 `Query` 基础上加了 `nearest_to` 的相似度搜索 | 仅提及，u3-l2 展开 |
| `TakeQuery` | 按行偏移 `_rowoffset` 或行 id `_rowid` 精确取行 | 仅提及 |

这三种对象在内部最终都会被统一打包成一个 `AnyQuery` 枚举，交给底层后端执行——这样本地表和远程表就能共用同一套执行入口。

#### 4.1.2 核心流程

```text
table.query()                 ──►  构造一个 Query（持有 BaseTable 引用 + 一个默认 QueryRequest）
   │
   ├── .limit(n)              ──►  修改 QueryRequest.limit
   ├── .offset(n)             ──►  修改 QueryRequest.offset
   ├── .only_if("id > 10")    ──►  修改 QueryRequest.filter
   ├── .select(Select::...)   ──►  修改 QueryRequest.select
   │
   └── .execute().await       ──►  打包成 AnyQuery::Query，交给 BaseTable 执行
                                   返回 SendableRecordBatchStream（RecordBatch 流）
```

关键点：链式配置方法**不会立即执行**，它们只是往一个内部的 `QueryRequest` 结构里写字段；真正读数据发生在 `execute()`。

#### 4.1.3 源码精读

入口 `Table::query()` 非常薄，只是用内部的 `BaseTable` 句柄造一个 `Query`：

[query.rs 入口 Table::query（在 table.rs 中）:1374-1376](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1374-L1376) —— `Table::query()` 创建一个空的 `Query`，把内部 `BaseTable` 的 `Arc` 传进去。

`Query` 结构体本身只有两个字段：父表句柄和请求参数：

[query.rs Query 结构体:803-807](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L803-L807) —— `Query` 持有 `parent: Arc<dyn BaseTable>`（指向底层表）和 `request: QueryRequest`（本次查询的全部配置）。

[query.rs Query::new:810-815](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L810-L815) —— 构造时 `request` 取默认值（即「不限制、不过滤、返回所有列」）。

而 `QueryRequest` 就是「一条查询的全部配置」：

[query.rs QueryRequest 结构体:722-770](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L722-L770) —— 注意 `limit` / `offset` / `filter` / `select` / `prefilter` 等字段，本讲的链式方法最终都是在改这些字段。

它的默认值很值得记住：

[query.rs QueryRequest::default:772-789](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L772-L789) —— 默认 `select = Select::All`（返回所有非系统列）、`prefilter = true`（默认先过滤再搜索）、`limit = None`（普通扫描默认不限行数）。

> 注意一个重要区别：**普通扫描（`Query`）默认不限行数**（`limit = None`，会返回整张表）；而**向量搜索默认 `limit = 10`**（见下一讲）。这个差异由 `nearest_to` 在转换时补上，见 [query.rs nearest_to:858-868](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L858-L868)。

执行时，`Query` 把自己的 `request` 打包成 `AnyQuery::Query`，交给底层表：

[query.rs ExecutableQuery for Query::execute_with_options:891-899](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L891-L899) —— 调用 `self.parent.query(&AnyQuery::Query(...), options)`，由本地或远程后端真正执行。

而 `AnyQuery` 就是「普通查询 or 向量查询」的二选一：

[table/query.rs AnyQuery 枚举:33-36](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/query.rs#L33-L36) —— `AnyQuery::Query(QueryRequest)` 或 `AnyQuery::VectorQuery(VectorQueryRequest)`。

#### 4.1.4 代码实践

**实践目标**：验证「`query()` 只构造、不执行」，并理解 `QueryRequest` 的默认值。

**操作步骤**（源码阅读型实践）：

1. 打开 [query.rs 第 772-789 行](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L772-L789)，记下 `QueryRequest::default()` 的每个字段默认值。
2. 打开 [simple.rs 的 `search` 函数:125-136](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs#L125-L136)，观察它如何 `table.query().limit(2).nearest_to(...).execute().await`。
3. 思考：如果在 simple.rs 里删掉 `.execute().await`，会发生什么？（答：只得到一个 `VectorQuery` 值，不会读任何数据。）

**预期结果**：能用自己的话说出「链式方法配置 `QueryRequest`、`execute()` 才触发 I/O」这一设计。

> 如果你本地已按 u1-l2 装好 Rust 环境，可以运行 `cargo run --example simple`（需在 `rust/lancedb` 目录）观察输出，但这不是必须的——本实践以阅读为主。

#### 4.1.5 小练习与答案

**练习 1**：`Query` 结构体里为什么只存 `Arc<dyn BaseTable>` 而不直接存 `NativeTable`？

**答案**：因为同一套查询代码要对**本地表**和**远程表**都生效。存 trait 对象 `Arc<dyn BaseTable>`，`Query` 就和具体后端解耦——执行时由后端自己决定是直接读本地 Lance 文件，还是发一个 HTTP 请求。这正是 u2-l2 讲过的「本地与远程共用同一套 Table API」。

**练习 2**：为什么普通扫描默认 `limit = None`，而向量扫描要默认 `limit = 10`？

**答案**：普通扫描是「把符合条件的行读出来」，不限数量是合理的默认；而向量搜索是「找最相似的 N 个」，没有上限在语义上没意义，还会对全表做距离计算非常昂贵，所以默认给一个 `DEFAULT_TOP_K = 10`（[query.rs:36](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L36)）。

---

### 4.2 QueryBase trait：链式通用配置方法

#### 4.2.1 概念说明

`QueryBase` 是一个 trait，集中定义了「**所有查询都通用的配置方法**」。无论你手里是 `Query`、`VectorQuery` 还是 `TakeQuery`，都能用同一套方法（`limit` / `offset` / `only_if` / `select` 等）来配置——因为这些配置对三种查询都成立。

本讲重点掌握下面四个方法（其余如 `full_text_search` / `postfilter` / `fast_search` 留到后续讲义）：

| 方法 | 作用 | SQL 类比 |
| --- | --- | --- |
| `limit(n)` | 最多返回 n 行 | `LIMIT n` |
| `offset(n)` | 跳过前 n 行 | `OFFSET n` |
| `only_if(sql)` | 只返回满足 SQL 谓词的行 | `WHERE <sql>` |
| `only_if_expr(expr)` | 同上，但用类型安全的 DataFusion 表达式 | `WHERE <expr>` |

注意命名映射：Rust 核心用 `only_if`，而 Python / TypeScript 绑定里同一个方法叫 `where`。它们是同一回事，跨语言阅读时不要混淆。

#### 4.2.2 核心流程

`QueryBase` 的实现采用了一个非常优雅的 **blanket impl（全局实现）** 模式：

```text
定义 trait QueryBase { fn limit(self, ...) -> Self; ... }
       │
       ├── 任何一个实现了 HasQuery 的类型 T，自动得到 QueryBase 的全部实现
       │       impl<T: HasQuery> QueryBase for T { ... }
       │
       └── HasQuery 只要求一个方法：mut_query() -> &mut QueryRequest
              Query / VectorQuery / TakeQuery 各自实现 HasQuery，
              把自己的「内部请求」暴露出来即可
```

也就是说：`limit` / `only_if` 这些方法的实现体是**完全一样**的——都是「拿到内部的 `&mut QueryRequest`，改对应字段，返回 `self`」。不同查询类型只需各自实现 `HasQuery::mut_query()`，告诉框架「我的请求字段在哪里」。

对 `Query`，`mut_query()` 直接返回 `&mut self.request`；对 `VectorQuery`，因为它的请求结构里嵌套了一个 `base: QueryRequest`，所以返回 `&mut self.request.base`（见 4.4.3）。

#### 4.2.3 源码精读

trait 定义本身只是方法签名：

[query.rs QueryBase trait（含 select/limit/only_if 等签名）:376-520](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L376-L520) —— 这是所有通用配置方法的「合同」。

其中 `limit` 的文档说明了普通扫描与向量扫描的差异：

[query.rs QueryBase::limit:377-384](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L377-L384) —— 「普通搜索默认无 limit；向量搜索默认 10」。

`only_if` 接收一个 SQL 字符串作为过滤谓词：

[query.rs QueryBase::only_if:392-404](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L392-L404) —— 文档给出 `x > 10`、`y > 0 AND y < 100` 等示例，并提示「在过滤列上建标量索引可加速」。

而 `select` 控制返回列（细节见 4.3）：

[query.rs QueryBase::select:449-471](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L449-L471) —— 文档强调「列式存储下，只选需要的列能显著降低延迟」。

接下来是关键的「桥接」trait 和全局实现：

[query.rs HasQuery trait:522-524](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L522-L524) —— `HasQuery` 只要求暴露一个可变的内部 `QueryRequest`。

[query.rs QueryBase 的全局实现 impl<T: HasQuery> QueryBase for T:526-589](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L526-L589) —— 这一段是「复用」的核心。看几个典型实现：

- `limit`：`self.mut_query().limit = Some(limit); self` —— 改字段、返回自己。
- `only_if`：把 SQL 字符串包成 `QueryFilter::Sql` 存进 `filter` 字段。
- `only_if_expr`：把 DataFusion 表达式包成 `QueryFilter::Datafusion` 存进 `filter` 字段。
- `select`：直接替换 `select` 字段。
- `full_text_search`：额外做了一件事——若没设 limit，自动补上默认的 `DEFAULT_TOP_K`。

`QueryFilter` 这个枚举说明「过滤条件」可以有三种来源：

[query.rs QueryFilter 枚举:708-717](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L708-L717) —— `Sql(String)` / `Substrait(...)` / `Datafusion(Expr)` 三选一。

类型安全的表达式由 `expr` 模块提供辅助函数 `col` / `lit`：

[expr.rs col 与 lit:30-42](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/expr.rs#L30-L42) —— `col("age")` 构造列引用（注意它不会把名字转小写，能正确匹配 `firstName` 这种列名），`lit(18)` 构造字面量，二者可用 `.gt` / `.and` / `.eq` 等组合成复杂表达式。

最后，`only_if_expr` 有一个**重要限制**：表达式过滤目前**不支持远程 / 服务端查询**，远程表必须用 `only_if` 的 SQL 字符串形式。这一点在 trait 文档里写明了：

[query.rs only_if_expr 文档说明远程不支持:406-426](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L406-L426) —— 「Expression filters are not supported for remote/server-side queries.」

#### 4.2.4 代码实践

**实践目标**：用 `only_if` 做一次带过滤的基础扫描，并验证非法过滤会报错。

**操作步骤**（基于真实测试 `test_execute_no_vector`）：

1. 打开 [query.rs test_execute_no_vector:1790-1820](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1790-L1820) 阅读测试逻辑。
2. 该测试先建一张含 `id`、`vector` 列的表，然后：

```rust
// 示例代码（摘自测试逻辑）：
let mut stream = table
    .query()
    .only_if("id % 2 == 0")   // 只保留偶数 id
    .execute()
    .await
    .expect("should have result");

while let Some(batch) = stream.next().await {
    let b = batch.unwrap();
    let arr: &Int32Array = b["id"].as_primitive();
    assert!(arr.iter().all(|x| x.unwrap() % 2 == 0));  // 全是偶数
}
```

3. 同一测试还故意用一个**非法过滤** `"id = 0 AND"` 来验证错误传播：

```rust
// 示例代码：
let result = table.query().only_if("id = 0 AND").execute().await;
assert!(result.is_err());   // 语法不完整，应当报错
```

**需要观察的现象**：合法谓词返回的 `id` 列全是偶数；不完整 SQL 谓词会让 `execute()` 返回 `Err`。

**预期结果**：理解 `only_if` 的语义是「保留满足谓词的行」，且错误谓词在执行期被解析器拒绝。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `QueryBase` 的方法签名都是 `fn xxx(self, ...) -> Self`（拿走所有权并返回），而不是 `&mut self`？

**答案**：因为这是典型的 **Builder 模式**。每次调用拿走 `self`、修改后返回新的 `Self`，于是可以链式写成 `query.limit(10).only_if("...").select(...)`。如果用 `&mut self` 就没法写链式调用，调用方也得先 `let mut q = ...` 再逐步改。

**练习 2**：`only_if("x > 10")` 里写入的 SQL 字符串，最终以什么形式存进 `QueryRequest`？

**答案**：被包成 `QueryFilter::Sql(String)` 存进 `request.filter` 字段（见 [query.rs:537-540](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L537-L540)）。注意此刻只是「存起来」，SQL 的真正解析发生在 `execute()` 时由 DataFusion 完成——这也是为什么非法 SQL 要到执行期才报错。

---

### 4.3 Select 枚举：控制返回哪些列

#### 4.3.1 概念说明

`Select` 枚举回答一个问题：**这次查询要返回哪些列？** 因为 LanceDB 是列式存储（u1-l4），「只读需要的列」能直接减少 I/O，对延迟影响极大。所以文档反复强调：**作为最佳实践，永远只 select 你真正需要的列**。

`Select` 有四种取值，能力从弱到强：

| 取值 | 含义 | 例子 |
| --- | --- | --- |
| `Select::All` | 返回所有非系统列（默认） | `SELECT *` |
| `Select::Columns(Vec<String>)` | 指定若干原始列 | `SELECT a, b` |
| `Select::Dynamic(Vec<(名字, SQL表达式)>)` | 用 SQL 表达式派生新列 | `SELECT a+b AS combined` |
| `Select::Expr(Vec<(名字, DataFusion Expr)>)` | 同 Dynamic，但用类型安全表达式 | 同上，类型安全 |

`Dynamic` 和 `Expr` 解决同一个问题（派生计算列），区别只是表达式用「SQL 字符串」还是「DataFusion `Expr` 对象」来表达。后者类型更安全、可由 IDE 检查；但**远程查询时 `Expr` 会被自动序列化成 SQL 字符串**（与 `Dynamic` 走同一条路）。

#### 4.3.2 核心流程

```text
选择列的方式：

① 不调 select()              ──►  QueryRequest.select 保持默认 = Select::All（全部列）

② .select(Select::columns(&["id","name"]))   ──►  只返回 id、name 两列

③ .select(Select::dynamic(&[("double", "id * 2"), ("id", "id")]))
                                ──►  返回派生列 double（= id*2）和原始列 id
                                     SQL 等价：SELECT id*2 AS double, id FROM ...

④ .select(Select::expr_projection(&[("double", col("id")*lit(2)), ("id", col("id"))]))
                                ──►  同 ③，但用类型安全表达式构造
```

派生列的两个要点：

- 每个元组 `(名字, 表达式)`：第一个是**输出列名**，第二个是**计算表达式**。
- **返回列的顺序 = 你给的顺序**，即使和写入时的列顺序不同。

#### 4.3.3 源码精读

枚举定义清晰列出四种取值：

[query.rs Select 枚举:39-73](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L39-L73) —— 注意 `Dynamic` 和 `Expr` 的文档都说明了「第一个元素是输出列名、第二个是表达式」。

三个便捷构造函数让你少写类型标注：

[query.rs Select::columns:80-82](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L80-L82) —— 从 `&[&str]` 或 `&[String]` 方便地造 `Select::Columns`。

[query.rs Select::dynamic:87-94](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L87-L94) —— 从 `&[(&str, &str)]` 造 `Select::Dynamic`。

[query.rs Select::expr_projection:110-117](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L110-L117) —— 从 `&[(name, Expr)]` 造 `Select::Expr`。

`Select` 最终通过 `QueryBase::select` 写进请求（4.2.3 已看过其实现 `self.mut_query().select = select`）。

真实测试演示了派生列的用法和返回顺序：

[query.rs test_select_with_transform:1686-1735](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1686-L1735) —— 用 `Select::dynamic(&[("id2", "id * 2"), ("id", "id")])`，断言输出 schema 顺序是先 `id2` 后 `id`，且每行 `id2 == id * 2`。

[query.rs test_select_with_expr_projection:1738-1787](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1738-L1787) —— 用 `Select::expr_projection(&[("id2", col("id") * lit(2i32)), ("id", col("id"))])` 走类型安全路线，行为与上一个测试完全一致。

#### 4.3.4 代码实践

**实践目标**：对比 `Select::All`、`Select::Columns`、`Select::Dynamic` 三种方式返回的列差异。

**操作步骤**（下面是一段示例代码，模仿 `test_select_with_transform`，你可在 `rust/lancedb` 下存为新 example 运行）：

```rust
// 示例代码：对比三种 Select 的返回列
use arrow_array::RecordBatch;
use futures::TryStreamExt;
use lancedb::query::{ExecutableQuery, QueryBase, Select};
use lancedb::{connect, Result};

#[tokio::main]
async fn main() -> Result<()> {
    // 假设已有一张含 id(Int32) 列的表（可参考 simple.rs 建表）
    let db = connect("data/sample-lancedb").execute().await?;
    let table = db.open_table("my_table").execute().await?;

    // 方式 ①：All —— 返回全部列
    let all: Vec<RecordBatch> = table.query().execute().await?
        .try_collect().await?;
    println!("All 列数: {}", all[0].num_columns());

    // 方式 ②：Columns —— 只返回 id
    let cols: Vec<RecordBatch> = table.query()
        .select(Select::columns(&["id"]))
        .execute().await?
        .try_collect().await?;
    println!("Columns 列: {:?}", cols[0].schema().fields());

    // 方式 ③：Dynamic —— 派生一个 id*2 列
    let dyn_: Vec<RecordBatch> = table.query()
        .limit(10)
        .select(Select::dynamic(&[("id2", "id * 2"), ("id", "id")]))
        .execute().await?
        .try_collect().await?;
    println!("Dynamic 列顺序: {:?}", dyn_[0].schema().fields());

    Ok(())
}
```

**需要观察的现象**：

- 方式 ① 返回的列数等于表的全部列数；
- 方式 ② 只剩 `id` 一列；
- 方式 ③ 列顺序是 `id2` 在前、`id` 在后（按你给的顺序），且 `id2` 的值确实是 `id` 的两倍。

**预期结果**：直观体会到「列式存储下，select 直接决定读多少数据」，并记住「Dynamic 返回顺序 = 给定顺序」。

> 如果本地无法运行，可直接阅读 [test_select_with_transform:1686-1735](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1686-L1735) 完成等价的源码阅读型实践。

#### 4.3.5 小练习与答案

**练习 1**：`Select::Dynamic` 与 `Select::Expr` 有什么异同？什么时候选哪个？

**答案**：两者都用于派生计算列，输出列名都是元组第一项、表达式是第二项，返回顺序都遵循给定顺序。区别在于表达式的载体：`Dynamic` 用 SQL 字符串（灵活但无类型检查、易拼错），`Expr` 用 DataFusion `Expr` 对象（类型安全、可被编译器和 IDE 检查）。本地查询两者皆可；远程查询时 `Expr` 会被自动序列化成 SQL 字符串，最终和 `Dynamic` 走同一条路。

**练习 2**：如果不调用 `.select(...)`，返回的列由什么决定？

**答案**：由 `QueryRequest::default()` 决定，默认是 `Select::All`（[query.rs:779](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L779)），即返回所有非系统列。文档特意警告：`All` 总是比只选需要的列慢，生产环境应避免。

---

### 4.4 ExecutableQuery：执行查询并拿到结果流

#### 4.4.1 概念说明

配置好 `Query` 之后，还要「点火」才能真正拿到数据。这由 `ExecutableQuery` trait 负责——它定义了**如何把一个查询对象变成结果**。它和 `QueryBase` 是分工的：

- `QueryBase`：「这次查什么」（配置，返回 `Self`，可链式）。
- `ExecutableQuery`：「把查出来的数据给我」（执行，返回 `Future`）。

执行的结果不是一次性的大数组，而是一个**异步流** `SendableRecordBatchStream`——即「一个会陆续产出多个 `RecordBatch` 的 Stream」。这种设计能处理远大于内存的结果集：一边产出、一边消费，配合反压（backpressure）控制内存。

`ExecutableQuery` 还提供几个有用的「非执行」方法：

| 方法 | 作用 |
| --- | --- |
| `execute()` | 用默认选项执行，返回 `RecordBatch` 流 |
| `execute_with_options(opts)` | 带选项执行（如控制每个 batch 最大行数、超时） |
| `create_plan(opts)` | 只生成执行计划，不执行（可用于优化或调试） |
| `explain_plan(verbose)` | 打印执行计划字符串（调试性能用，**不执行**） |
| `analyze_plan()` | 执行并打印带运行时指标的计划 |
| `output_schema()` | 不执行就拿到输出 schema（列名/类型） |

#### 4.4.2 核心流程

```text
配置好的 Query
   │
   ├── .execute().await                         ──► SendableRecordBatchStream
   │       （内部：execute_with_options(default)）
   │
   ├── .execute_with_options(opts).await        ──► SendableRecordBatchStream
   │       （可设 max_batch_length、timeout）
   │
   ├── .output_schema().await                   ──► SchemaRef（不读数据，只看列结构）
   │       （内部：create_plan(default).schema()）
   │
   └── .explain_plan(true).await                ──► String（只打印计划，不执行）
```

消费流的标准写法（用 `futures::TryStreamExt`）：

```rust
// 示例代码：
let mut stream = table.query().limit(10).execute().await?;
while let Some(batch) = stream.next().await {     // 逐个 batch 消费
    let batch: RecordBatch = batch?;
    println!("{} rows", batch.num_rows());
}
// 或一次性收集成 Vec<RecordBatch>：
let all: Vec<RecordBatch> = stream.try_collect().await?;
```

#### 4.4.3 源码精读

trait 定义把「执行」抽象成几个 future 方法：

[query.rs ExecutableQuery trait:633-706](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L633-L706) —— 注意 `execute()` 是带默认实现的，它转调 `execute_with_options(QueryExecutionOptions::default())`。

`execute_with_options` 的文档说明了「流式 + 反压」的设计：

[query.rs execute_with_options 文档:650-670](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L650-L670) —— 「结果以流返回；单个 batch 的行数和行顺序不保证；流消费慢时会施加反压以限制单次查询内存」。

执行选项 `QueryExecutionOptions` 控制两个维度：

[query.rs QueryExecutionOptions 结构体:591-619](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L591-L619) —— `max_batch_length`（每个 batch 最多多少行，默认 1024）和 `timeout`（最长等待时间）。

`Query` 对 `ExecutableQuery` 的具体实现，就是把请求打包成 `AnyQuery::Query` 交给后端：

[query.rs Query 的 execute_with_options 实现:891-899](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L891-L899) —— 调 `self.parent.clone().query(&AnyQuery::Query(self.request.clone()), options)`。

顺带验证一下 4.2.2 说的「`VectorQuery` 的 `HasQuery` 指向内嵌的 `base`」：

[query.rs HasQuery for VectorQuery:1359-1363](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1359-L1363) —— `mut_query()` 返回 `&mut self.request.base`，所以 `VectorQuery` 上的 `limit`/`only_if` 改的也是同一个 `QueryRequest`。

真实测试演示了「分页扫描 = limit + offset」：

[query.rs test_pagination_with_scan:2383-2393](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L2383-L2393) —— 用 `limit` + `offset` 把全表结果切片成分页，断言每一页等于全量结果对应切片。这说明 `offset` 的语义就是「跳过前 offset 行」。

#### 4.4.4 代码实践

**实践目标**：用 `limit` + `offset` 实现一次分页扫描，并体会 `output_schema()` 不执行就能拿到列结构。

**操作步骤**（源码阅读型 + 可选运行）：

1. 阅读 [test_pagination_with_scan:2383-2393](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L2383-L2393) 与它依赖的 [test_pagination:2354-2380](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L2354-L2380)，理解「全量结果 = 各页 limit/offset 切片的拼接」。
2. 写一小段示例代码（示例代码）：

```rust
// 示例代码：分页 + 预看 schema
use futures::TryStreamExt;
use lancedb::query::{ExecutableQuery, QueryBase};
use lancedb::{connect, Result};

#[tokio::main]
async fn main() -> Result<()> {
    let db = connect("data/sample-lancedb").execute().await?;
    let table = db.open_table("my_table").execute().await?;

    // 不执行，先看会返回什么列
    let schema = table.query().output_schema().await?;
    println!("输出列: {:?}", schema.fields());

    // 第 2 页，每页 10 行
    let page2 = table.query().limit(10).offset(10).execute().await?
        .try_collect::<Vec<_>>().await?;
    println!("第 2 页 batch 数: {}", page2.len());

    Ok(())
}
```

**需要观察的现象**：`output_schema()` 几乎瞬时返回（没有真正扫描全表）；改变 `offset` 能取到不同区间的行。

**预期结果**：理解「执行前可预知输出结构」「limit+offset 即分页」两点。

#### 4.4.5 小练习与答案

**练习 1**：`explain_plan()` 和 `analyze_plan()` 有什么区别？

**答案**：`explain_plan(verbose)` **只生成并打印**执行计划字符串，**不执行查询**，适合在调试查询性能时先看「会做哪些工作」；`analyze_plan()` 会**真正执行查询**，并打印带运行时指标（metrics）的计划，能看到每一步实际耗时和行数。测试 [test_analyze_plan:1998-2004](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1998-L2004) 就断言其输出包含 `metrics=`。

**练习 2**：为什么查询结果是「`RecordBatch` 流」而不是一个完整的 `Vec<RecordBatch>`？

**答案**：为了支持**大于内存的结果集**与**反压**。流式产出意味着消费者拿一批处理一批，生产者不会一次性把全部数据塞进内存；当消费者处理慢时，反压会限制预读，从而控制单次查询的峰值内存（见 [execute_with_options 文档:650-670](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L650-L670)）。当然，结果不大时也可以用 `try_collect` 一次性收成 `Vec`。

---

## 5. 综合实践

把本讲的四个模块串起来，完成下面这个任务：

> **任务**：打开一张含 `id`(Int32)、`vector`(向量) 列的表（没有就参考 [simple.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs) 建一张），写一个程序，对同一张表分别用三种方式扫描并打印返回的列名，最后做一次带过滤 + 投影 + 分页的查询。

参考实现（示例代码）：

```rust
use futures::TryStreamExt;
use lancedb::query::{ExecutableQuery, QueryBase, Select};
use lancedb::{connect, Result};

#[tokio::main]
async fn main() -> Result<()> {
    let db = connect("data/sample-lancedb").execute().await?;
    let table = db.open_table("my_table").execute().await?;

    // ① 默认 All
    let s = table.query().output_schema().await?;
    println!("[All]      列: {:?}", s.fields().iter().map(|f| f.name()).collect::<Vec<_>>());

    // ② 只选 id
    let s = table.query().select(Select::columns(&["id"])).output_schema().await?;
    println!("[Columns]  列: {:?}", s.fields().iter().map(|f| f.name()).collect::<Vec<_>>());

    // ③ 派生 id2 = id*2
    let s = table.query()
        .select(Select::dynamic(&[("id2", "id * 2"), ("id", "id")]))
        .output_schema().await?;
    println!("[Dynamic]  列: {:?}", s.fields().iter().map(|f| f.name()).collect::<Vec<_>>());

    // ④ 过滤 + 投影 + 分页
    let page = table.query()
        .only_if("id > 0")                 // 过滤：只要 id>0
        .select(Select::columns(&["id"]))  // 投影：只要 id
        .limit(10)                          // 限制：10 行
        .offset(5)                          // 分页：跳过前 5 行
        .execute().await?
        .try_collect::<Vec<_>>().await?;
    println!("[过滤+投影+分页] 行数: {}", page.iter().map(|b| b.num_rows()).sum::<usize>());

    Ok(())
}
```

**检查清单**：

- [ ] ① 打印出表的全部列名；
- [ ] ② 只剩 `id`；
- [ ] ③ 顺序是 `id2`、`id`；
- [ ] ④ 返回行数 ≤ 10，且每行 `id > 0`。

完成这个任务，你就把「`query()` 起步 → `QueryBase` 链式配置 → `Select` 控制列 → `ExecutableQuery` 执行」整条链路打通了。

## 6. 本讲小结

- 一切查询从 `table.query()` 开始，返回一个**只配置、不执行**的 `Query` 对象，内部持有一份 `QueryRequest` 配置；`execute()` 才真正触发 I/O。
- `QueryBase` trait 集中了所有查询通用的链式配置方法（`limit` / `offset` / `only_if` / `only_if_expr` / `select` …），通过 `HasQuery` + blanket impl 让 `Query` / `VectorQuery` / `TakeQuery` 共用同一套实现。
- `only_if` 用 SQL 字符串过滤行（等价 `WHERE`）；`only_if_expr` 用类型安全的 DataFusion 表达式，但**不支持远程查询**。
- `Select` 枚举用 `All` / `Columns` / `Dynamic` / `Expr` 四种方式控制返回列；因为列式存储，**只选需要的列能显著降延迟**；`Dynamic`/`Expr` 还能派生计算列，且返回顺序遵循给定顺序。
- `ExecutableQuery` 负责执行：`execute()` 返回一个 `RecordBatch` **流**（支持流式消费与反压），`output_schema()` / `explain_plan()` 可在不执行时预览结构或计划。
- 普通扫描默认不限行数、默认先过滤（`prefilter=true`）；分页可用 `limit` + `offset` 实现。

## 7. 下一步学习建议

本讲只讲了「不带相似度」的基础扫描。接下来建议：

- **u3-l2 向量相似度搜索：VectorQuery**：学习 `nearest_to` 如何把 `Query` 升级为 `VectorQuery`、`_distance` 列的含义，以及 `DEFAULT_TOP_K`。本讲的 `QueryBase` 在 `VectorQuery` 上完全适用（因为它也实现了 `HasQuery`）。
- **u3-l4 过滤表达式与 SQL**：深入 `only_if` 的 SQL 如何被解析成 DataFusion 表达式、向量搜索前的预过滤 vs 后过滤（`postfilter`）。
- **延伸阅读**：直接打开 [query.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs) 通读 `QueryBase`（376-520）与 `ExecutableQuery`（633-706）两个 trait，对照本讲的理解查漏补缺。
