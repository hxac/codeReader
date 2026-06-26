# 过滤表达式与 SQL

## 1. 本讲目标

学完本讲，你应当能够：

- 用 `only_if` 写出 SQL 字符串过滤条件，限制查询只返回满足条件的行；
- 理解 SQL 字符串与 DataFusion 逻辑表达式（`Expr`）之间的关系，知道二者在本地表与远程表上的处理差异；
- 读懂 `expr/sql.rs` 里 `expr_to_sql_string` 这个「反向」转换函数的作用与它为何要自定义方言；
- 区分「预过滤（prefilter）」与「后过滤（postfilter）」，知道何时该用哪一种。

本讲承接 u3-l1（查询基础：`QueryBase` 与 `Select`），在「只配置、不执行」的查询模型基础上，专门讲「如何把行级过滤加到查询里」。

## 2. 前置知识

在进入源码前，先用大白话建立三个直觉。

**什么是谓词（predicate / filter）。** 谓词就是「这一行要不要保留」的判断条件，例如 `age > 18` 或 `category = 'fruit'`。数据库里它最常见的写法是 SQL 的 `WHERE` 子句。LanceDB 把这种行级条件称为 filter（过滤），它会在扫描数据时只保留满足条件的行。

**什么是 DataFusion 与 `Expr`。** DataFusion 是 Rust 生态里一个可嵌入的查询引擎（Apache 顶级项目）。它把一条表达式（比如 `age > 18 AND status = 'active'`）解析成一棵结构化的逻辑表达式树，类型就是 `datafusion_expr::Expr`。LanceDB 和底层 Lance 都构建在 DataFusion 之上，所以「SQL 字符串」和「类型安全表达式」在内部最终都会走到同一套 `Expr` 体系。

**标量过滤与向量搜索为什么可以叠加。** 向量搜索回答「和这个查询向量最相似的 k 条记录」，标量过滤回答「哪些行满足某个列条件」。两者叠加就得到「在某个范围内、和查询向量最相似的记录」——这正是 RAG、推荐等场景最常用的查询形态。叠加顺序（先过滤还是后过滤）会显著影响结果与延迟，这是本讲后半部分的重点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `rust/lancedb/src/query.rs` | 查询核心。定义 `QueryBase` trait（`only_if`/`only_if_expr`/`postfilter`）、`QueryFilter` 枚举、`QueryRequest`（含 `filter` 与 `prefilter` 字段）。 |
| `rust/lancedb/src/expr.rs` | 表达式构建器模块，对 DataFusion 表达式做薄封装，导出 `col`、`lit` 等辅助函数。 |
| `rust/lancedb/src/expr/sql.rs` | 「表达式 → SQL 字符串」的反向转换 `expr_to_sql_string`，含自定义的 `LanceSqlDialect`。 |
| `rust/lancedb/src/table/query.rs` | 把查询请求翻译成底层 Lance 扫描器调用的地方，能看到 `scanner.filter(sql)` 与 `scanner.prefilter(...)` 的实际接线。 |
| `rust/lancedb/src/table.rs` | 定义 `Filter`、`Predicate` 枚举，供 `count_rows`、`delete` 等非查询场景复用同样的「SQL 字符串 / 表达式」二选一约定。 |

## 4. 核心概念与源码讲解

### 4.1 过滤的两条入口：SQL 字符串 vs 类型安全表达式

#### 4.1.1 概念说明

LanceDB 给过滤条件提供了两个并列的入口，对应同一个抽象（`QueryFilter`）的两种写法：

- **SQL 字符串**：`only_if("age > 18")`。优点是灵活、与 SQL 习惯一致、本地和远程表都支持；缺点是字符串在编译期无法检查，拼错列名要等到执行才报错。
- **类型安全表达式**：`only_if_expr(col("age").gt(lit(18)))`。借助 `crate::expr` 提供的构建器，写成 Rust 方法链，IDE 有提示、列名拼写错也只是字符串里的错误，但表达式能复用 Rust 的运算符重载。注意：**表达式过滤不支持远程 / 服务端查询**，远程表必须用 SQL 字符串。

这两个方法都只是「把过滤条件存进查询请求」，本身不触发任何 I/O——这是 LanceDB 贯穿的「构建器只配置、`execute()` 才执行」风格（见 u3-l1）。

#### 4.1.2 核心流程

```text
only_if("age > 18")
   └─> QueryRequest.filter = Some(QueryFilter::Sql("age > 18".to_string()))

only_if_expr(col("age").gt(lit(18)))
   └─> QueryRequest.filter = Some(QueryFilter::Datafusion(<Expr>))

两者最终都进入 QueryRequest.filter，是同一个 Option<QueryFilter>
```

无论是 `Query`（普通扫描）、`VectorQuery`（向量搜索）还是 `TakeQuery`（按 rowid 取行），都通过 `HasQuery` + blanket impl（见 u3-l1）共享同一套 `only_if` / `only_if_expr`，所以「加过滤」这件事对三种查询对象完全一致。

#### 4.1.3 源码精读

`only_if` 与 `only_if_expr` 的 trait 定义（注意各自的文档注释强调的适用场景）：

[rust/lancedb/src/query.rs:392-426](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L392-L426) —— `only_if` 的文档给出 `x > 10`、`y > 0 AND y < 100` 等示例，并提示「在过滤列上建标量索引常能提升性能」；`only_if_expr` 的文档明确写着「表达式过滤不支持远程 / 服务端查询，远程表请用 `only_if` 的 SQL 字符串」。

它们的默认实现（由 blanket impl 提供，把值塞进 `QueryRequest.filter`）：

[rust/lancedb/src/query.rs:537-544](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L537-L544) —— `only_if` 构造 `QueryFilter::Sql`，`only_if_expr` 构造 `QueryFilter::Datafusion`，二者都只是赋值，没有 I/O。

`QueryFilter` 枚举本身就是「过滤条件的三种表示」：

[rust/lancedb/src/query.rs:708-717](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L708-L717) —— `Sql(String)`、`Substrait(Arc<[u8]>)`、`Datafusion(Expr)` 三个变体。`Substrait` 是一种跨引擎的序列化格式，普通用户很少直接接触；本讲聚焦 `Sql` 与 `Datafusion`。

构建表达式用的 `col` / `lit` 来自 `crate::expr` 模块，它是对 DataFusion 表达式的薄封装：

[rust/lancedb/src/expr.rs:30-42](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/expr.rs#L30-L42) —— 直接 `pub use datafusion_expr::lit;` 复用 DataFusion 的 `lit`，并自定义了 `col`：与 DataFusion 原生 `col` 不同，LanceDB 的 `col` **不会**把标识符转成小写，因此 `col("firstName")` 能正确引用大小写敏感的字段名。

#### 4.1.4 代码实践

**实践目标**：直观对比 SQL 字符串过滤与类型安全表达式过滤两种写法。

**操作步骤**：

1. 在 u1-l3 跑通过的 `examples/simple.rs` 基础上，复制一份示例程序。
2. 对同一张表分别写两段查询：
   - 用 `only_if("id % 2 == 0")`；
   - 用 `only_if_expr(col("id").modulus(lit(2)).eq(lit(0)))`（注：`%` 在表达式里用 `modulus`）。
3. 各自 `execute()` 并打印命中行数。

**需要观察的现象**：两段查询返回的行集合应当一致。

**预期结果**：两段返回相同的行（按列 `id` 值看都满足偶数条件）。如果对 `col` 的方法链不确定（如 `modulus`），可在 `datafusion_expr::Expr` 的文档里查到。

> 说明：本实践为「源码阅读 + 改造示例」型，若本地尚未配好 Rust 编译环境，可先只阅读 `query.rs:1790-1820` 的测试（见 4.1.5），通过断言理解行为，待编译环境就绪再实际运行。「待本地验证」具体命中行数。

#### 4.1.5 小练习与答案

**练习 1**：为什么远程表不能用 `only_if_expr`，而本地表可以？

**参考答案**：远程（服务端）查询通过 HTTP 把请求发到 LanceDB Cloud，协议里携带的是 SQL 字符串；表达式（`Expr`）是 Rust 进程内的内存对象，无法跨网络直接传递，所以 `only_if_expr` 的文档明确标注它不支持远程 / 服务端查询。本地表则在进程内直接持有 `Expr`，可以原样交给底层 Lance（见 4.2）。

**练习 2**：`only_if("a > 1")` 调用后，会不会立即扫描数据？

**参考答案**：不会。`only_if` 只是把 `QueryFilter::Sql("a > 1".to_string())` 存进 `QueryRequest.filter`，没有任何 I/O；真正的扫描发生在后续的 `execute().await`。

### 4.2 SQL 字符串如何被「解析」成过滤表达式

#### 4.2.1 概念说明

这一节回答本讲的核心问题：**用户写的 SQL 字符串，到底在哪里、被谁解析成 DataFusion 表达式？**

答案可能有点反直觉：**LanceDB 自己的 Rust 核心并不负责把 SQL 字符串解析成 `Expr`。** 对于本地表，LanceDB 把 SQL 字符串**原样**透传给底层 Lance 的扫描器 `scanner.filter(sql)`，由 Lance（内部使用 DataFusion 的 SQL 解析器 `datafusion_sql`）解析并求值。对于远程表，SQL 字符串通过 HTTP 发给服务器，由服务器解析。

这样设计的好处是：无论本地还是远程，过滤语法的「单一真相源」都是 DataFusion 的 SQL 方言，LanceDB 不必自己维护一套解析器；同时也解释了为什么 SQL 字符串过滤对两种后端都通用。

#### 4.2.2 核心流程

```text
本地表路径：
  only_if("a > 1")
   → QueryFilter::Sql("a > 1")
   → table/query.rs: match QueryFilter::Sql(sql) => scanner.filter(sql)?
   → Lance Scanner::filter(sql)            ← 字符串原样交给 Lance
   → Lance 用 datafusion_sql 把 "a > 1" 解析成 Expr
   → DataFusion 物理执行计划里挂上这个过滤

远程表路径：
  only_if("a > 1")
   → QueryFilter::Sql("a > 1")
   → table/query.rs::filter_to_sql: 直接返回 sql.clone()
   → HTTP 请求体里带上 "a > 1"
   → 服务器端解析
```

注意一个关键细节：对于远程表，如果用户用的是 `only_if_expr`（类型安全表达式），就需要**反向**把 `Expr` 转成 SQL 字符串才能发出去——这正是 4.3 节 `expr_to_sql_string` 的主要用途之一。

#### 4.2.3 源码精读

本地表把过滤条件翻译成 Lance 扫描器调用的关键 `match`：

[rust/lancedb/src/table/query.rs:240-252](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/query.rs#L240-L252) —— 三种 `QueryFilter` 变体分别走不同入口：`Sql(sql) => scanner.filter(sql)?`（字符串原样透传给 Lance）、`Substrait(...) => scanner.filter_substrait(...)`、`Datafusion(expr) => scanner.filter_expr(expr.clone())`（直接传 `Expr`）。注意这里**没有**调用任何 SQL 解析函数——解析发生在 Lance 内部。

远程表把过滤条件转成字符串发出去的辅助函数：

[rust/lancedb/src/table/query.rs:520-528](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/query.rs#L520-L528) —— `filter_to_sql`：`Sql` 变体直接克隆字符串；`Substrait` 不支持服务端查询（报 `NotSupported`）；`Datafusion` 变体调用 `expr_to_sql_string(expr)` 反向转成 SQL（见 4.3）。

「SQL 字符串过滤是否真的只配置不执行」的最直接证据——一条单元测试先验证合法过滤生效、再验证非法过滤报错：

[rust/lancedb/src/query.rs:1807-1819](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1807-L1819) —— `only_if("id % 2 == 0")` 执行后断言返回的 `id` 全是偶数；随后 `only_if("id = 0 AND")`（一个不完整的 SQL 表达式）执行时断言 `result.is_err()`。后者正是「解析发生在 Lance/DataFusion」的体现——字符串直到 `execute()` 才被解析，所以语法错误在那里才暴露。

同样的「SQL 字符串 / 表达式」二选一约定也出现在非查询场景，LanceDB 用一个独立的 `Filter` 枚举复用它：

[rust/lancedb/src/table.rs:240-246](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L240-L246) —— `Filter` 枚举有 `Sql(String)` 和 `Datafusion(Expr)` 两个变体，用于 `Table::count_rows` 等需要行级过滤的接口（例如 `count_rows(Some(Filter::Sql("i >= 5")))`）。

#### 4.2.4 代码实践

**实践目标**：通过「触发一次非法过滤」亲眼看到 SQL 解析发生在执行阶段而非调用阶段。

**操作步骤**：

1. 打开一张含 `id` 列的表。
2. 先调用 `let q = table.query().only_if("id = 0 AND");`，**不**调用 `execute()`。
3. 观察：这一步不会报错。
4. 再调用 `q.execute().await`。

**需要观察的现象**：第 3 步成功构造查询对象；第 4 步返回 `Err`。

**预期结果**：因为 `"id = 0 AND"` 是不合法的 SQL 表达式，DataFusion 解析失败，错误在 `execute()` 时返回。这证明了 LanceDB 核心并不在 `only_if` 时解析，而是把字符串延迟到 Lance 扫描器里才解析。错误信息会包含类似 "sql parser" / "syntax" 的字样。

> 「待本地验证」具体错误消息文案。

#### 4.2.5 小练习与答案

**练习 1**：如果 LanceDB 核心不解析 SQL，那么过滤条件的 SQL 语法遵循谁的方言？

**参考答案**：遵循底层 Lance / DataFusion 的 SQL 方言。它是表达式级的语法（类似 SQL `WHERE` 子句里的布尔表达式），支持 `>`、`<`、`=`、`AND`、`OR`、`IN`、函数调用等。注意：Lance 的标识符引号用反引号 `` ` `` 而不是双引号 `"`，这一点会在 4.3 节的方言处理里体现。

**练习 2**：为什么 `only_if_expr` 对远程表不行，但远程表依然能做复杂过滤？

**参考答案**：远程表用 `only_if("...")` 传 SQL 字符串即可，过滤在服务器端用 DataFusion 解析执行，能力与本地一致；`only_if_expr` 不行只是因为 `Expr` 无法跨 HTTP 传递，并非远程表功能受限。

### 4.3 反向转换：`expr_to_sql_string` 与自定义方言

#### 4.3.1 概念说明

`rust/lancedb/src/expr/sql.rs` 里的 `expr_to_sql_string` 做的是**与解析相反**的事：把一棵 DataFusion `Expr` 树序列化回 SQL 字符串。它主要服务两个场景：

1. **远程表用 `only_if_expr`**：必须把 `Expr` 转成 SQL 字符串才能放进 HTTP 请求（见 4.2.3 的 `filter_to_sql`）。
2. **`Select::Expr` 投影**：当用户用类型安全表达式定义「派生列」（如 `a + b`）时，最终也要转成 SQL 字符串交给底层 `project_with_transform`。

这个文件还解决了一个看似不起眼但很关键的细节——**标识符引号**。Lance 的 SQL 解析器用反引号 `` ` `` 作为定界标识符引号，而 DataFusion 默认 unparser 用双引号 `"`。如果不处理，包含大写字母或特殊字符的列名（比如 `firstName`）会被错误引用。

#### 4.3.2 核心流程

```text
expr_to_sql_string(expr):
  if expr 子树里没有 Binary/LargeBinary 字面量:
     → run_unparser(expr)  # 用 LanceSqlDialect 直接序列化
  else:  # 慢路径：DataFusion unparser 无法序列化二进制字面量
     → 把每个二进制字面量替换成唯一占位字符串
     → run_unparser(改写后的 expr)
     → 把占位符逐个替换回 SQL 的 X'...' 十六进制字节串字面量
```

`LanceSqlDialect::identifier_quote_style` 的判定规则：标识符「含大写字母」或「含 `[a-zA-Z0-9_]` 以外的字符」或「以数字开头」时，加反引号。

#### 4.3.3 源码精读

自定义方言及其判定逻辑：

[rust/lancedb/src/expr/sql.rs:9-31](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/expr/sql.rs#L9-L31) —— 注释说明 Lance 用反引号作为唯一标识符引号，所以必须产出 `` `firstName` `` 而不是 `"firstName"`；`identifier_quote_style` 在「含大写字母 / 非法字符 / 数字开头」时返回 `Some('`')`，否则返回 `None`（不加引号）。这正是为什么大小写敏感的 schema 能被正确还原。

主函数与快慢两条路径：

[rust/lancedb/src/expr/sql.rs:71-115](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/expr/sql.rs#L71-L115) —— 快路径直接 `run_unparser`；慢路径处理 `Binary` / `LargeBinary` 字面量：DataFusion 的 unparser 不能序列化这两种 `ScalarValue`，所以先用占位符字符串 `__lancedb_binary_placeholder_<i>__` 替换，等 unparser 把其余结构序列化好，再把占位符替换成 SQL 的 `X'....'` 十六进制字节串。

二进制字面量的判定（决定走哪条路径）：

[rust/lancedb/src/expr/sql.rs:46-60](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/expr/sql.rs#L46-L60) —— `has_binary_literal` 遍历表达式子树，一旦发现 `Binary` / `LargeBinary` 标量就返回 `true`，从而触发慢路径。

该函数在模块根部被导出：

[rust/lancedb/src/expr.rs:22](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/expr.rs#L22) —— `pub use sql::expr_to_sql_string;`，因此它对外是 `lancedb::expr::expr_to_sql_string`，并被 `table/query.rs` 的 `filter_to_sql` 与 `Select::Expr` 分支复用。

#### 4.3.4 代码实践

**实践目标**：用 `expr_to_sql_string` 把一个表达式序列化成 SQL 字符串，观察大小写敏感列名的引号处理。

**操作步骤**：

1. 写一段小程序（示例代码，非项目原有代码）：
   ```rust
   use lancedb::expr::{col, lit, expr_to_sql_string};
   // 注意：expr_to_sql_string 接受 &datafusion_expr::Expr
   let e = col("firstName").gt(lit(18));
   println!("{}", expr_to_sql_string(&e).unwrap());
   ```
2. 把列名换成全小写的 `age`，再跑一次。

**需要观察的现象**：`firstName` 输出形如 `` `firstName` > 18 ``（带反引号）；`age` 输出形如 `age > 18`（无引号）。

**预期结果**：含大写字母的列名被反引号包裹，纯小写列名不加引号，与 `LanceSqlDialect` 的判定一致。

> 这是「示例代码」，用于理解方言行为；实际编译需在 `rust/lancedb` 的 example 或测试里调用，「待本地验证」精确输出。

#### 4.3.5 小练习与答案

**练习 1**：如果不自定义 `LanceSqlDialect`，直接用 DataFusion 默认 unparser，对 `firstName` 列会产生什么 SQL？会有什么问题？

**参考答案**：默认 unparser 用双引号包裹，产出 `"firstName" > 18`。而 Lance 的 SQL 解析器只认反引号作为定界标识符引号，双引号会被当成普通字符或字符串字面量，导致列名解析失败或引用错误列。自定义方言保证 LanceDB 序列化出的 SQL 能被 Lance 正确回解析。

**练习 2**：为什么要为二进制字面量单独走一条「占位符替换」的慢路径？

**参考答案**：DataFusion 的 unparser 没有实现 `Binary` / `LargeBinary` 这两种 `ScalarValue` 的序列化，直接调用会失败。LanceDB 的做法是先用唯一字符串占位符顶替，让 unparser 处理好其余结构（运算符、函数、嵌套），最后再把占位符替换成 SQL 标准的 `X'....'` 十六进制字节串字面量，从而把序列化逻辑集中在 DataFusion，又绕过了它的限制。

### 4.4 预过滤 vs 后过滤：过滤与向量搜索的顺序

#### 4.4.1 概念说明

把过滤条件加到**向量搜索**上时，「过滤」和「搜索」的先后顺序会带来截然不同的行为：

- **预过滤（prefilter，默认）**：先用标量过滤缩小候选集，再在这个子集上做向量搜索。结果总是准确且能凑满 `limit` 条，但因为要先扫描过滤条件，会**增加一些延迟**。在过滤列上建标量索引（见 u4-l2）常能把这部分延迟降下来。
- **后过滤（postfilter）**：先做完整的向量搜索取 top-k，再对这批结果套过滤。延迟低（过滤只作用在很少的行上），但**可能返回少于 `limit` 条甚至 0 条**——如果最近的 k 条都不满足过滤条件就会被剔掉。可以配合更大的 `refine_factor` 把更多候选拉回来，部分弥补这个损失。

二者由 `QueryRequest.prefilter` 这一个布尔字段切换，默认 `true`（预过滤）；调用 `postfilter()` 把它置为 `false`。

#### 4.4.2 核心流程

```text
QueryRequest.prefilter = true   (默认 / 预过滤)
   → 过滤 → 在过滤后的候选集上做向量搜索 → 准确、可凑满 limit，延迟略高

QueryRequest.prefilter = false  (调用 postfilter() / 后过滤)
   → 向量搜索取 top-k → 再过滤结果 → 延迟低，但结果数可能 < limit

二者最终都把 prefilter 开关交给 Lance 扫描器：
   scanner.prefilter(query.base.prefilter)
```

从工程取舍看：

\[

\text{预过滤}:\quad \text{召回完整度} \uparrow,\ \text{延迟} \uparrow
\qquad
\text{后过滤}:\quad \text{召回完整度} \downarrow,\ \text{延迟} \downarrow

\]

#### 4.4.3 源码精读

`prefilter` 字段与默认值：

[rust/lancedb/src/query.rs:750-751](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L750-L751) —— `QueryRequest.prefilter: bool` 注释「If set to false, the filter will be applied after the vector search.」

[rust/lancedb/src/query.rs:782](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L782) —— `QueryRequest::default()` 里 `prefilter: true`，即默认预过滤。

`postfilter()` 的定义与文档（讲清两种模式的取舍）：

[rust/lancedb/src/query.rs:483-501](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L483-L501) —— 文档说明默认预过滤，但会额外增加延迟、标量索引常能改善；当过滤太复杂或列上无标量索引时，可用后过滤降低延迟；后过滤作用于搜索结果上，「可能返回少于 `limit` 条甚至 0 条」，且发生在「refine stage」，调大 `refine_factor` 能恢复部分结果。

[rust/lancedb/src/query.rs:565-567](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L565-L567) —— `postfilter()` 实现只有一行：`self.mut_query().prefilter = false;`。

这个开关最终如何传给底层 Lance：

[rust/lancedb/src/table/query.rs:209](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/query.rs#L209) —— `scanner.prefilter(query.base.prefilter);`，把开关交给 Lance 扫描器，由它决定在执行计划里把过滤放在向量搜索之前还是之后。

#### 4.4.4 代码实践

**实践目标**：在向量搜索上加标量过滤，对比预过滤与后过滤返回的命中数量。

**操作步骤**：

1. 建一张表，列含 `id`(Int32)、`category`(Utf8)、`vector`(FixedSizeList\<Float32\>)；写入若干行，让 `category` 取值不均匀（例如大部分是 `"a"`，少量是 `"b"`）。
2. 对查询向量做一次 `nearest_to(q).limit(10)`，不加过滤，记录返回行数（应为 10）。
3. 加过滤 `.only_if("category = 'b'")`（默认预过滤），记录返回行数。
4. 改为后过滤 `.only_if("category = 'b'").postfilter()`，记录返回行数。

**需要观察的现象**：

- 无过滤：返回 10 条。
- 预过滤：仍能返回接近 10 条 `category='b'` 的记录（如果库里 `b` 足够多）。
- 后过滤：返回的 `category='b'` 行数**可能明显少于 10**，因为先取了 top-10 再剔除非 `b` 的。

**预期结果**：后过滤在 `b` 稀疏时会返回少于 `limit` 条，体现「延迟换召回完整度」的取舍。

> 「待本地验证」具体行数取决于数据分布；若库里 `b` 很少，差异最明显。

#### 4.4.5 小练习与答案

**练习 1**：业务要求「向量搜索结果必须凑满 top-10，少一条都不行」，应该用预过滤还是后过滤？

**参考答案**：预过滤。它在搜索前缩小候选集，只要满足过滤的记录够多，就能稳定凑满 `limit`。后过滤是先取 top-k 再剔，可能不足 `limit` 条。

**练习 2**：调用 `.postfilter()` 之后，能做点什么来「尽量」恢复丢失的结果？

**参考答案**：调大 `refine_factor`。后过滤发生在「refine stage」，更大的 refine factor 会让向量搜索先拉回更多候选，过滤后留下来的结果更多，部分弥补后过滤造成的数量损失（参见 `query.rs:498-500` 的文档）。

## 5. 综合实践

把本讲四个模块串起来，设计一个「带标量预过滤的向量搜索」端到端小任务。

**任务**：模拟一个商品检索场景——库里有商品名、类别、价格、向量；用户想「在『电子』类别、价格低于 1000 的商品里，找和查询向量最相似的 5 件」。

**步骤**：

1. 建表，schema 为 `name: Utf8, category: Utf8, price: Float32, vector: FixedSizeList<Float32, 128>`，写入一批数据，确保 `category='电子'` 且 `price<1000` 的记录有不少于 5 条。
2. 用 SQL 字符串过滤做查询：
   ```rust
   let rs = table.query()
       .nearest_to(&query_vec)
       .only_if("category = '电子' AND price < 1000")
       .limit(5)
       .execute().await?;
   ```
3. 把它换成等价的类型安全表达式版本（注意：仅本地表可行）：
   ```rust
   use lancedb::expr::{col, lit};
   .only_if_expr(
       col("category").eq(lit("电子")).and(col("price").lt(lit(1000.0_f32)))
   )
   ```
   验证两版结果一致。
4. 再单独跑一次**不带过滤**的 `nearest_to(...).limit(5)`，对比两次结果的 `category` / `price` 列，体会「过滤把不符合条件的近邻挡在门外」。
5. （可选）在该查询上调用 `.postfilter()`，对比返回行数，验证 4.4 的取舍。

**验收要点**：能说出 SQL 字符串过滤与表达式过滤在本地/远程表上的差异；能解释为什么非法 SQL 要到 `execute()` 才报错；能区分预过滤与后过滤对结果数量的影响。

## 6. 本讲小结

- 过滤有两条入口：`only_if("...")`（SQL 字符串，本地 / 远程都支持）与 `only_if_expr(Expr)`（类型安全表达式，**仅本地**），二者最终都存进 `QueryRequest.filter` 的 `QueryFilter` 枚举。
- LanceDB 核心本身**不解析** SQL 字符串；本地表把字符串原样交给 Lance 的 `scanner.filter(sql)`，由 Lance / DataFusion 解析；远程表则通过 HTTP 发给服务器解析。
- `expr/sql.rs` 的 `expr_to_sql_string` 做的是**反向**转换（`Expr` → SQL 字符串），服务于远程表的 `only_if_expr` 与 `Select::Expr` 投影；它用自定义 `LanceSqlDialect`（反引号引号）和二进制字面量占位符路径来保证序列化结果可被 Lance 正确回解析。
- 过滤与向量搜索的顺序由 `QueryRequest.prefilter` 切换：默认预过滤（准确、可凑满 `limit`，延迟略高），`postfilter()` 切到后过滤（延迟低，但结果数可能少于 `limit`，可用 `refine_factor` 补救）。
- 「构建器只配置、`execute()` 才执行」的风格在过滤上同样成立——非法 SQL 字符串要等到 `execute()` 才会报错。
- 在过滤列上建标量索引（u4-l2）是降低预过滤延迟的常用手段。

## 7. 下一步学习建议

- **u3-l5 全文检索 FTS**：过滤是「列上的布尔条件」，全文检索是「文本上的相关性打分」，两者可以叠加；学完后你能区分 `only_if` 与 `full_text_search` 的关系。
- **u3-l6 混合搜索与 RRF 重排**：当向量搜索与全文搜索同时进行时，本讲的过滤会与两路搜索如何组合，将在混合搜索里进一步展开。
- **u4-l2 标量索引**：本讲多次提到「在过滤列上建标量索引能加速预过滤」，下一单元会讲清 BTree / Bitmap 等标量索引如何具体加速 `only_if` 的过滤。
- 继续精读源码：建议顺着 `rust/lancedb/src/table/query.rs` 的 `create_plan` 流程，跟踪 `scanner.filter(sql)` 之后 Lance 是如何把 SQL 表达式挂到执行计划上的（这部分在 Lance 依赖里）。
