# 数据增删改与模式演化

## 1. 本讲目标

在前面几讲里，我们已经学会了「建表 → 写入初始数据 → 查询」。但现实中的表不是一次写完就不动了：你要修正错误数据、删除过期行、给表加一列、或者做「主键存在就更新、不存在就插入」的 upsert。

本讲聚焦 `table` 模块下与「数据修改和结构修改」相关的四个最小模块：

- **`update`**：按条件批量改写某些列的值。
- **`delete`**：按条件删除若干行。
- **`schema_evolution`**：对已存在的表增、改、删列（以及更新字段元数据）。
- **`merge`**：用 `merge_insert` 实现 upsert，把外部新数据与表内旧数据按主键合并。

学完本讲，你应当能够：

1. 用 `update()` 的 `only_if` / `column` 链式地做条件式批量更新，并理解「每次写都产生新版本」。
2. 用 `delete()` 配合 SQL 谓词或 DataFusion 表达式删行，并能区分「逻辑删除」与「物理回收」。
3. 用 `add_columns` / `alter_columns` / `drop_columns` 安全地演化表结构。
4. 用 `merge_insert` 的 `when_matched` / `when_not_matched` 语义完成一次 upsert，看懂 `MergeResult` 的各项统计。

## 2. 前置知识

在进入源码之前，先建立几个本讲反复用到的心智模型。

### 2.1 Lance 的「不可变文件 + 版本」模型

LanceDB 建立在 Lance 列式格式之上。Lance 的写策略是 **追加新文件，不就地修改旧文件**。每一次会改变数据的操作（写入、更新、删除、加列……）都会提交一个**新版本（version）**，写一组新文件并在 manifest 里记录「这次版本里哪些文件有效、哪些被淘汰」。这件事带来两个直接后果：

- **版本单调递增**：哪怕一次 `delete("false")` 一行都没删，也会提交一个新版本（这一点源码里有专门的测试保护，见后文）。
- **修改是逻辑的、间接的**：所谓「更新一行」并不是去旧文件里改字节，而是写一个「删除标记」+ 一个「新值文件」。真正的物理回收要靠 `optimize`（参见 u5-l1）。

理解这一点后，你会明白为什么本讲每个操作的实现都长着同一副骨架——见 4.0 节。

### 2.2 Builder 模式：先配置、再 `execute`

LanceDB 里凡涉及 I/O 的操作几乎都遵循一个风格：先返回一个**构建器（Builder）**，调用方在上面链式地配置参数，**最后才调用 `.execute().await` 真正落盘**。好处是「配置阶段不产生副作用」，你可以反复设置、复用、丢弃，只有 `execute` 才真正写数据。

本讲里：

- `update()` 返回 `UpdateBuilder`，是构建器风格。
- `merge_insert()` 返回 `MergeInsertBuilder`，也是构建器风格。
- `delete()` / `add_columns()` / `alter_columns()` / `drop_columns()` 则是「直接 async 方法」，调用即生效——它们不需要那么多配置项，所以没用构建器。注意这种区别。

### 2.3 SQL 谓词（Predicate）

更新和删除都需要「选中哪些行」，这个选择条件叫**谓词（predicate）**。LanceDB 接受两种形式的谓词：

- **SQL 字符串**：如 `"id > 5"`、`"age = 0"`。本地表把它原样交给底层 Lance，远程表把它通过 HTTP 发给服务端，最终都由 DataFusion 解析。
- **DataFusion 表达式 `Expr`**：如 `col("id").gt(lit(5))`，类型安全、可在 Rust 里拼接，但**仅本地表支持**（与 u3-l4 中 `only_if_expr` 的限制一致）。

源码里这两种形式被统一抽象成 `Predicate` 枚举。

### 2.4 upsert 与 source / target 语义

`merge_insert` 的核心思想来自 SQL 的 `MERGE INTO`：拿一份「新数据（source）」去和「表内旧数据（target）」按某个**连接键（on）」对齐，然后对三种情况分别决定怎么办：

- **匹配上（matched）**：source 和 target 在连接键上都有值 → 通常「更新」。
- **源有目标无（not matched）**：只在 source 里有的行 → 通常「插入」。
- **目标有源无（not matched by source）**：只在 target 里、source 这次没带的行 → 可选「删除」。

这套语义里，条件的写法用 `target.` 前缀指旧数据列、`source.` 前缀指新数据列，例如 `"target.last_update < source.last_update"`。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [rust/lancedb/src/table/update.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/update.rs) | `UpdateBuilder` 与 `execute_update`：条件式更新。 |
| [rust/lancedb/src/table/delete.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/delete.rs) | `execute_delete`：条件式删除，区分 SQL 与 Expr 两条路径。 |
| [rust/lancedb/src/table/schema_evolution.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/schema_evolution.rs) | 增/改/删列与字段元数据更新。 |
| [rust/lancedb/src/table/merge.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/merge.rs) | `MergeInsertBuilder` 与 `execute_merge_insert`：upsert。 |
| [rust/lancedb/src/table.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs) | `Table` 句柄方法、`BaseTable` trait 契约、`Predicate` 枚举、`NativeTable` 实现。 |
| [rust/lancedb/src/table/dataset.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/dataset.rs) | `DatasetConsistencyWrapper`：版本快照与 `ensure_mutable` 守卫。 |

> 全局结构提示：`Table`（对外轻量句柄）把方法委托给 `Arc<dyn BaseTable>`；本地后端是 `NativeTable`，它把每个操作翻译成对底层 Lance `Dataset` 的调用；远程后端 `RemoteTable` 把同样的调用转成 HTTP。本讲以本地 `NativeTable` 为主线讲解，因为核心逻辑都在本地实现里。

## 4. 核心概念与源码讲解

### 4.0 数据修改的统一骨架（先讲共性，再看个性）

本讲的四个操作，底层实现几乎共享同一套步骤。先记住这个「五步骨架」，后面每个模块都只是它的特化：

```
1. ensure_mutable()          —— 守卫：当前是否处于「时间旅行」只读状态？是则拒绝写。
2. dataset.get().await       —— 对当前版本做一次快照（snapshot），拿到 Lance Dataset。
3. 用 Lance Core 的 Builder  —— 把 filter / set / on 等配置翻译成 Lance 的更新计划。
4. plan.build()? + execute() —— 真正写出一组新文件，提交新版本。
5. dataset.update(new)       —— 把表内缓存的「当前版本指针」推进到新版本。
```

这五步背后的关键判断是 `ensure_mutable`：如果你 checkout 了一个历史版本在做「时间旅行」（见 u5-l3），这时是不允许写的。源码里它返回一个清晰的错误：

[表不允许在被 checkout 的特定版本上修改 — rust/lancedb/src/table/dataset.rs:179-190](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/dataset.rs#L179-L190)

> 这段代码检查 `pinned_version`：只有「跟踪最新版本（Latest 模式）」时才允许写。被钉在某个历史版本上就报 `InvalidInput`。

每次操作成功后，`NativeTable` 还会调用 `self.bump_freshness()` 让「最终一致性」缓存失效（参见 u5-l3 的 `read_consistency`），这里先有个印象即可。

> 一句话总结共性：**LanceDB 核心在这四个操作里几乎只做「参数搬运 + 版本回写」**，真正的更新/删除/合并算法都在底层 Lance crate 里。LanceDB 的价值在于把这些底层能力包装成统一、类型安全、本地/远程一致的 API。

下面逐个展开。

---

### 4.1 模块一：条件更新 update

#### 4.1.1 概念说明

`update` 解决的问题是：「把满足某个条件的行，按一个表达式重新计算某些列的值」。它等价于 SQL 的 `UPDATE t SET col = expr WHERE predicate`。两件事要分别配置：

- **改哪些行**（WHERE）：由 `only_if` 指定，**可选**；不写就改全表。
- **改成什么**（SET）：由 `column` 指定，**至少要有一个**；`column` 可以反复调用以同时改多列。表达式里可以引用「这一行原来的列值」，例如 `column("i", "i + 1")` 表示把 `i` 列自增 1。

注意它与「逐行更新」的区别：`update` 是一次提交里**批量**处理所有命中行，内部并行写新文件，效率远高于「查一行、改一行、写一行」的循环。源码文档里也特别提醒：如果你的条件是按主键逐个改很多行，用一次 `merge_insert` 比反复 `update` 快得多。

#### 4.1.2 核心流程

`update` 的用户侧流程是构建器风格：

```
table.update()               // 得到 UpdateBuilder（还不碰 I/O）
      .only_if("id > 5")      // 可选：WHERE
      .column("name", "'foo'")// SET name = 'foo'（可多次）
      .execute().await        // 真正执行，返回 UpdateResult
```

`execute` 内部先校验「至少有一列要改」，然后委托给 `BaseTable::update`，最终落到 `execute_update`，走 4.0 节那套五步骨架：

1. `ensure_mutable()` 守卫。
2. 快照当前 dataset。
3. `LanceUpdateBuilder::new(dataset)`，先 `update_where(filter)`（若有 WHERE），再对每个 `column` 调 `set(col, expr)`。
4. `build()` + `execute()` 写新文件。
5. `dataset.update(new)` 升版本。

返回的 `UpdateResult` 带两个字段：命中并改写的行数 `rows_updated`、新提交的版本号 `version`。

#### 4.1.3 源码精读

先看用户构建的数据结构和返回类型：

[`UpdateResult`：更新结果，含改写行数与提交版本 — update.rs:14-21](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/update.rs#L14-L21)

[`UpdateBuilder`：持有父表、可选 filter、待改列列表 — update.rs:24-29](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/update.rs#L24-L29)

构建器的两个核心配置方法（注意它们消费 `self` 并返回 `Self`，所以是链式调用）：

[`only_if`：限定只改匹配 filter 的行 — update.rs:43-46](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/update.rs#L43-L46)

[`column`：声明「把某列设为某表达式」，可多次调用 — update.rs:55-62](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/update.rs#L55-L62)

[`execute`：校验「至少一列」后委托给底层表 — update.rs:65-73](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/update.rs#L65-L73)

> 注意第 66-69 行的校验：如果没有调用过 `column`，直接返回 `Error::InvalidInput`，避免一次「什么都没改」的空更新。

真正的五步骨架在 `execute_update`：

[`execute_update`：快照→where→set→build→升版本 — update.rs:77-110](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/update.rs#L77-L110)

> 对照 4.0 节：第 81 行 `ensure_mutable`、第 84 行快照、第 87-97 行把配置翻译给 `LanceUpdateBuilder`（这是 Lance 核心的构建器，注意它和 `UpdateBuilder` 同名但不同 crate）、第 100-101 行真正执行、第 104 行升版本。

对外入口在 `Table` 句柄上（注意它只返回构建器，不执行 I/O）：

[`Table::update` 返回 `UpdateBuilder` — rust/lancedb/src/table.rs:1051-1053](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1051-L1053)

`NativeTable` 的实现只是委托 + 失效缓存：

[`NativeTable::update`：委托 execute_update 后 bump_freshness — rust/lancedb/src/table.rs:2800-2805](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L2800-L2805)

#### 4.1.4 代码实践

**实践目标**：验证 `only_if` 的过滤范围与 `column` 表达式对「旧值」的引用。

**操作步骤**（这是「源码阅读型 + 可运行」实践，参考 `update.rs` 自带的测试 `test_update_with_predicate`）：

1. 用 `memory://` 连接建一张表，含列 `id: Int32`（0..10）、`name: Utf8`（"a".."j"）。
2. 执行 `table.update().only_if("id > 5").column("name", "'foo'").execute().await`。
3. 打印 `UpdateResult.rows_updated`，应等于 4（id 为 6,7,8,9）。
4. 用 `query().select(...)` 读回，断言 `id > 5` 的行 name 已变成 `"foo"`，其余行保持原字母。

**需要观察的现象**：
- `rows_updated` 恰好等于命中行数。
- 未命中行的值原封不动。

**预期结果**：见测试 [update.rs:344-410](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/update.rs#L344-L410)，它对 `id > 5` 的断言正是上面描述的行为。另一个测试 [update.rs:412-427](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/update.rs#L412-L427) 则演示了 `column("i", "i+1")` 这种「引用旧值」的表达式。

> 若无法在本地编译运行，可只阅读上述两个测试理解断言意图，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果不调用任何 `column` 就 `execute()`，会发生什么？
**答案**：在 `execute()` 里命中 [第 66-69 行的校验](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/update.rs#L65-L73)，返回 `Error::InvalidInput { message: "at least one column must be specified ..." }`，不会产生任何写操作或新版本。

**练习 2**：`column("i", "i + 1")` 里的 `i` 指的是哪一行的值？
**答案**：指**当前正在被更新的那一行的旧值**。正如 [文档 update.rs:53-54](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/update.rs#L52-L54) 所述，表达式「will be evaluated against the previous row's value」，逐行计算后写回。

**练习 3**：一次成功的 `update` 之后，表的 `version()` 会变化吗？
**答案**：会。`execute_update` 第 104 行调用 `dataset.update(...)` 推进版本，并在 `UpdateResult.version` 里返回新版本号（见 [update.rs:106-109](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/update.rs#L106-L109)）。

---

### 4.2 模块二：条件删除 delete

#### 4.2.1 概念说明

`delete` 解决「按条件删掉若干行」。它和 `update` 一样接受一个谓词，但**不需要构建器**——`Table::delete(predicate)` 是一个直接的 async 方法。谓词可以是 SQL 字符串，也可以是 DataFusion 表达式，由 `Predicate` 枚举统一。

理解删除的两层含义很重要：

- **逻辑删除**：被删的行不会从旧文件里物理抹除，而是记一个「删除标记」。后续查询自动过滤掉它们。这意味着磁盘空间不会立刻释放。
- **物理回收**：要等 `optimize`（u5-l1）做 compaction / prune 才真正清理。所以「删了但没回收」是正常状态。

一个反直觉但重要的点：**即使谓词匹配 0 行，`delete` 也会提交一个新版本**。源码里有专门测试保护这个行为（`test_delete_false_increments_version`）。这是因为「提交」本身是事实，Lance 用版本号记录每一次操作意图。

#### 4.2.2 核心流程

```
table.delete("id > 5").await            // SQL 字符串
// 或
table.delete(&expr).await                // DataFusion 表达式
```

`Predicate` 通过 `From` 实现自动从 `&str` / `&String` / `&Expr` 转换，所以你不用手动构造枚举。内部 `execute_delete` 仍是五步骨架的变体，但**按谓词类型分两条路径**：

- `Predicate::String`：直接用 Lance Dataset 的 `dataset.delete(sql)` 方法。
- `Predicate::Expr`：用 `DeleteBuilder::from_expr(dataset, expr).execute()`。

两条路都最终拿到「删除了多少行」和「新版本号」。

#### 4.2.3 源码精读

先看谓词抽象：

[`Predicate` 枚举与三个 From 实现 — rust/lancedb/src/table.rs:253-276](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L253-L276)

> 这就是为什么 `delete("id > 5")` 和 `delete(&col("id").gt(lit(5)))` 都能编译——`impl Into<Predicate>` 配合这三个 `From` 自动转换（见 [Table::delete 的签名 table.rs:1107-1109](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1107-L1109)）。

返回类型：

[`DeleteResult`：删除行数 + 版本号 — delete.rs:12-22](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/delete.rs#L12-L22)

> 注释里特意说明 `version = 0` 用于兼容「不返回版本号的旧服务端」——这是远程表场景的兜底。

核心实现，两条路径对比着看：

[`execute_delete`：先 ensure_mutable，再按 Predicate 分流 — delete.rs:27-60](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/delete.rs#L27-L60)

> 第 31 行 `ensure_mutable()` 是统一守卫；第 33-43 行是 SQL 路径（`dataset.delete(s)`）；第 44-58 行是 Expr 路径（`DeleteBuilder::from_expr`）。两条路径结构对称：都是「克隆 dataset → 执行删除 → 读 num_deleted_rows 和 version → dataset.update 升版本」。

`NativeTable::delete` 同样是委托 + 失效缓存：

[`NativeTable::delete — rust/lancedb/src/table.rs:2877-2879](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L2877-L2879)

#### 4.2.4 代码实践

**实践目标**：观察「0 行命中也会升版本」这一反直觉行为，并确认删除只影响行、不影响 schema。

**操作步骤**（参考测试 [delete.rs:170-203](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/delete.rs#L170-L203) 与 [delete.rs:119-142](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/delete.rs#L119-L142)）：

1. 建表 `id: [1,2,3,4,5]`，记录 `version()` 记为 `v0`。
2. 执行 `table.delete("false").await`（一行都不会删）。
3. 记录 `count_rows()`（仍是 5）和 `version()`（应大于 `v0`）。
4. 再执行 `table.delete("true").await`，观察 `DeleteResult.num_deleted_rows == 5`、`count_rows() == 0`。
5. 调用 `table.schema().await`，确认 schema 仍然存在（删空表 ≠ 删表）。

**需要观察的现象**：
- 步骤 3 里 `version` 上升但行数不变。
- 步骤 5 里 schema 与建表时完全一致。

**预期结果**：与上述两个测试的断言一致。`rows_removed_schema_same` 测试 [delete.rs:119-142](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/delete.rs#L119-L142) 明确断言 `current_schema == original_schema`。

#### 4.2.5 小练习与答案

**练习 1**：`delete("id > 100")` 在一张没有 id>100 的表上执行，返回的 `num_deleted_rows` 是多少？表版本会变吗？
**答案**：`num_deleted_rows == 0`，但版本号**会增加**。这正是 [delete.rs:145-168](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/delete.rs#L145-L168) 的 `test_delete_returns_num_deleted_rows` 测试验证的行为（其中第 159-162 行就是「删 0 行」的断言）。

**练习 2**：删完所有行后，再 `add` 写入新数据，会复用旧表结构吗？
**答案**：会。`delete("true")` 只清空行，不动 schema；后续 `add` 数据的 schema 只要兼容即可继续写入（schema 在建表时就固定了）。

**练习 3**：为什么删除后磁盘占用往往不会立刻下降？
**答案**：因为 Lance 采用逻辑删除——被删行只是被打上删除标记，物理文件还在，要等 `optimize`（u5-l1）做 compaction/prune 才回收。

---

### 4.3 模块三：模式演化 schema_evolution

#### 4.3.1 概念说明

表用着用着，往往会需要改结构：加一列派生指标、改一列的名字、放宽一列的可空性、删掉一列。这些就是**模式演化（schema evolution）**。LanceDB 把它拆成四个操作，都直接作用在已存在的表上，并且**每次都产生新版本**：

| 操作 | 等价 SQL | 说明 |
| --- | --- | --- |
| `add_columns(transforms)` | `ALTER TABLE ADD COLUMN` | 用 SQL 表达式新增列，可同时加多列，值由表达式算出。 |
| `alter_columns(alterations)` | `ALTER TABLE ALTER COLUMN` | 重命名、改可空性、做受支持的类型转换（cast）。 |
| `drop_columns(&[...])` | `ALTER TABLE DROP COLUMN` | 删除列。 |
| `update_field_metadata` | （无标准 SQL 对应） | 更新某个字段的元数据键值对（如标记 embedding 信息），默认合并。 |

注意一个**类型来源**：`add_columns` 接受的 `NewColumnTransform` 并非 LanceDB 自定义，而是直接 re-export 自 Lance 核心：

[NewColumnTransform 复用 Lance 的同名类型 — rust/lancedb/src/table.rs:17](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L17)

`alter_columns` 接受的 `ColumnAlteration` 同样来自 `lance::dataset`（见 schema_evolution.rs 顶部 import）。这是 LanceDB「薄壳透传」设计哲学的又一处体现。

#### 4.3.2 核心流程

四个操作的实现高度同构，都是 4.0 骨架的特化（`ensure_mutable` → 快照 → 调 Lance 方法 → 升版本），以 `add_columns` 为例：

```
table.add_columns(
    NewColumnTransform::SqlExpressions(vec![("doubled", "id * 2")]),
    None,                                  // 只读哪些列参与计算（可选优化）
).await
```

`SqlExpressions` 是一种「用 SQL 表达式定义新列」的变体（见测试 [schema_evolution.rs:181-238](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/schema_evolution.rs#L181-L238)）：`("doubled", "id * 2")` 表示新增列 `doubled`，其值 = `id * 2`。也可以传常量表达式如 `"42"` 给每行填同一个值。

模式演化的**返回值结构**很简单，只有 `version`：

[三个结果类型只有 version 字段 — schema_evolution.rs:18-46](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/schema_evolution.rs#L18-L46)

#### 4.3.3 源码精读

模块开头的文档注释概括了三类操作：

[模块文档：add/alter/drop columns — schema_evolution.rs:4-9](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/schema_evolution.rs#L4-L9)

三个核心实现，结构完全对称：

[`execute_add_columns`：add_columns(transforms, read_columns) — schema_evolution.rs:97-108](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/schema_evolution.rs#L97-L108)

[`execute_alter_columns`：rename / cast / set_nullable — schema_evolution.rs:113-123](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/schema_evolution.rs#L113-L123)

[`execute_drop_columns`：删除指定列 — schema_evolution.rs:128-138](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/schema_evolution.rs#L128-L138)

> 三段代码几乎逐行对应：`ensure_mutable()` → 克隆 dataset → 调 `dataset.add_columns/alter_columns/drop_columns` → 读版本 → `dataset.update` 升版本。差别只在调用 Lance 的哪个方法。

第四个操作 `update_field_metadata` 稍复杂，因为它支持「合并」或「整体替换」字段元数据。先看它操作的数据结构（一个构建器）：

[`FieldMetadataUpdate`：按点路径定位字段，set/remove/replace — schema_evolution.rs:48-85](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/schema_evolution.rs#L48-L85)

> `metadata: HashMap<String, Option<String>>`——值为 `Some` 表示设置该键，`None` 表示删除该键；`replace = true` 则整体替换该字段的元数据 map 而非合并。点路径（dot-path）如 `"address.zip"` 可定位嵌套字段。

它的实现据此分支：

[`execute_update_field_metadata`：合并或替换 — schema_evolution.rs:143-164](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/schema_evolution.rs#L143-L164)

对外四个方法（直接 async，非构建器）：

[`Table::add_columns` — table.rs:1456-1462](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1456-L1462) ｜ [`alter_columns` — table.rs:1465-1470](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1465-L1470) ｜ [`drop_columns` — table.rs:1481-1483](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1481-L1483)

`NativeTable` 实现（以 add_columns 为例，其余同构）：

[`NativeTable::add_columns`：委托 + bump_freshness — table.rs:2894-2902](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L2894-L2902)

#### 4.3.4 代码实践

**实践目标**：用 `add_columns` 加一列派生列，并验证版本递增。

**操作步骤**（参考 [schema_evolution.rs:180-238](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/schema_evolution.rs#L180-L238)）：

1. 建表 `id: [1,2,3,4,5]`，记录 `initial_version`。
2. 调 `table.add_columns(NewColumnTransform::SqlExpressions(vec![("doubled", "id * 2")]), None).await`，拿到 `result`。
3. 断言 `result.version > initial_version`。
4. `query().select(Select::columns(&["id","doubled"]))` 读回，断言每行 `doubled == id * 2`。

**需要观察的现象**：新列自动出现，且数值由表达式即时算出，无需预先声明列类型。

**预期结果**：与测试断言一致——`schema.fields().len()` 从 1 变 2，且 `doubled` 列值正确。

> 想再练 `alter_columns`，可参考 [schema_evolution.rs:318-345](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/schema_evolution.rs#L318-L345)（重命名）、[388-429](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/schema_evolution.rs#L388-L429)（类型 cast）；想练 `drop_columns` 可参考 [484-510](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/schema_evolution.rs#L484-L510)。注意 [431-453](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/schema_evolution.rs#L431-L453) 的测试表明 **Int32→Float64 这种 cast 不被支持**，会报错。

#### 4.3.5 小练习与答案

**练习 1**：`add_columns` 里表达式 `"42"`（一个常量整数）会新增一列什么类型的列？
**答案**：根据测试 [schema_evolution.rs:272-314](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/schema_evolution.rs#L272-L314)，结果列是 `Int64`，且每行都是 42——类型由底层 DataFusion 推断字面量得出。

**练习 2**：`drop_columns(&["nonexistent"])` 会怎样？
**答案**：报错，错误信息包含列名 `"nonexistent"`。见测试 [schema_evolution.rs:599-619](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/schema_evolution.rs#L599-L619)。

**练习 3**：一次 `add_columns` 后立刻 `alter_columns`，版本号会怎样变化？
**答案**：每次操作各自提交一个新版本，单调递增。测试 [schema_evolution.rs:647-686](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/schema_evolution.rs#L647-L686) 连续做 add→alter→drop 并断言每次 `result.version` 都严格大于上一次。

---

### 4.4 模块四：merge_insert 与 upsert

#### 4.4.1 概念说明

`merge_insert` 是本讲最复杂的操作，它把「按主键合并新数据」这件事一次性高效完成，避免「查一行改一行」。它对应 SQL 的 `MERGE INTO`，是 upsert（update or insert）的标准实现。

核心是「连接键 + 三种情况」：

- **`on`**：连接键，即判断「新旧是否同一行」的列（通常是主键）。
- **`when_matched`**：新旧在 `on` 上匹配 → 选项有「全部更新（UpdateAll）」「仅当满足条件才更新（update_if）」「什么都不做（DoNothing）」。
- **`when_not_matched`**：只在新数据里有的行 → 「全部插入（InsertAll）」或「不插入」。
- **`when_not_matched_by_source`**：旧表里有、但新数据这次没带的行 → 「删除（Delete）」「仅当满足条件才删（delete_if）」「保留（Keep）」。

默认情况下 `when_matched = DoNothing`、`when_not_matched = DoNothing`、`when_not_matched_by_source = Keep`——也就是说，**什么都不配的话，merge 不会改变任何东西**，必须显式打开你想要的行为。最常见的 upsert 组合是：`when_matched_update_all(None)` + `when_not_matched_insert_all()`。

条件的写法用 `target.` / `source.` 前缀：`"target.age = 0"` 表示「只更新旧表里 age 为 0 的匹配行」。

#### 4.4.2 核心流程

```
let mut b = table.merge_insert(&["id"]);   // on = ["id"]，得到构建器（先借用 &mut self）
b.when_matched_update_all(None);            // 匹配则全更新
b.when_not_matched_insert_all();            // 源有目标无则插入
let result = b.execute(new_data_reader).await;  // 真正执行，返回 MergeResult
```

注意 `MergeInsertBuilder` 的配置方法是 `&mut self`（返回 `&mut Self`），所以要先 `let mut b = ...` 再链式调用；最后 `execute` 消费 `self` 并接收一个 `Box<dyn RecordBatchReader + Send>` 作为新数据源。

`execute_merge_insert` 的内部流程比前三个模块多一步「路由」：

1. **LSM 路由判断**：`lsm::lsm_dispatch_decision` 决定走 MemWAL LSM 写路径还是标准路径（详见 4.4.6）。
2. 若走标准路径：快照 dataset → `LanceMergeInsertBuilder::try_new(dataset, on)`。
3. 根据 builder 上的标志位，配置 `when_matched` / `when_not_matched` / `when_not_matched_by_source`（含条件型）。
4. `use_index` 控制是否用连接键上的索引加速；构建 job；`execute_reader(new_data)` 真正执行。
5. 读 `stats`，升版本，组装 `MergeResult`。

返回的统计字段较多：

\[ \text{num\_rows} = \text{num\_inserted\_rows} + \text{num\_updated\_rows} \quad (\text{标准路径}) \]

#### 4.4.3 源码精读

先看返回统计和过滤类型：

[`MergeResult`：插入/更新/删除行数、尝试次数、总行数、版本 — merge.rs:21-54](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/merge.rs#L21-L54)

> 注意 `num_rows` 字段的注释（第 44-53 行）：标准路径下它等于「插入+更新」；但走 MemWAL LSM 路径时，插入/更新的细分要到 compaction 才知道，于是这些字段全是 0，只有 `num_rows` 有值。这是「同一返回类型服务两种写路径」的妥协。

[`MergeFilter`：条件可以是 SQL 或 Expr — merge.rs:56-60](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/merge.rs#L56-L60)

构建器结构（字段就是全部配置项）：

[`MergeInsertBuilder` 字段 — merge.rs:65-78](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/merge.rs#L65-L78)

> 注意几个默认值（构造函数 [merge.rs:80-95](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/merge.rs#L80-L95)）：`use_index = true`（默认用索引加速）、`validate_single_shard = true`、`use_lsm_write = None`（未指定时按表是否装了 LsmWriteSpec 决定）。

三个核心配置方法（注意 `&mut self` 风格）：

[`when_matched_update_all(Option<String>)` — merge.rs:97-121](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/merge.rs#L97-L121)

> 文档第 110-116 行详细解释了 `target.` / `source.` 前缀语义，是理解条件型更新的关键。

[`when_not_matched_insert_all()` — merge.rs:130-135](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/merge.rs#L130-L135)

[`when_not_matched_by_source_delete(Option<String>)` — merge.rs:137-150](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/merge.rs#L137-L150)

`execute` 消费 builder 并接收新数据 reader：

[`execute(new_data)` 委托给底层表 — merge.rs:222-224](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/merge.rs#L222-L224)

核心实现，重点看「路由 + 行为映射」：

[`execute_merge_insert`：LSM 路由 → 配置三种行为 → 执行 → 组装统计 — merge.rs:230-313](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/merge.rs#L230-L313)

> 分段理解：第 235-250 行是 LSM 路由（命中则提前返回 LSM 结果）；第 252-284 行把 LanceDB 的标志位翻译成 Lance 的 `WhenMatched` / `WhenNotMatched` / `WhenNotMatchedBySource` 枚举——**这是 LanceDB 核心最主要的「翻译」工作**；第 286-301 行处理超时（默认 30s，但只在重试时强制，首试成功不受限）；第 305-312 行组装 `MergeResult`，注意第 311 行 `num_rows = inserted + updated` 的公式。

对外入口：

[`Table::merge_insert(&["id"])` 返回构建器 — table.rs:1280-1285](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1280-L1285)

`NativeTable::merge_insert`：

[`NativeTable::merge_insert：委托 + bump_freshness — table.rs:2831-2837](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L2831-L2837)

#### 4.4.4 代码实践

**实践目标**：跑一次完整的 upsert，看懂 `MergeResult` 各字段。

**操作步骤**（参考 [merge.rs:339-388](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/merge.rs#L339-L388)）：

1. 建表：`i: [0..10]`、`age: [0;10]`（10 行，age 全 0）。
2. 准备新数据 reader：`i: [5..15]`、`age: [1;10]`。
3. 做「插入不存在」：`merge_insert(&["i"]).when_not_matched_insert_all()` 后 execute。
4. 断言 `count_rows() == 15`（原 10 + 新增 5 个 i=10..14），`result.num_inserted_rows == 5`，`num_updated_rows == 0`。
5. 再准备新数据 `i: [15..25]`，做「全部更新」`when_matched_update_all(None)`，但因为 i=15..24 在表里都没有，所以不会有任何更新——`count_rows()` 仍是 15。
6. 准备 `i: [5..15], age: 3`，做「条件更新」`when_matched_update_all(Some("target.age = 0"))`，只把 age 仍为 0 的匹配行更新；断言 `count_rows(Some("age = 3")) == 5`。

**需要观察的现象**：
- 步骤 4 里只插入了 5 行（5..9 是匹配，不插入；10..14 才插入）。
- 步骤 6 条件更新只命中了 5 行（age=0 的那些）。

**预期结果**：与测试断言完全一致。

#### 4.4.5 小练习与答案

**练习 1**：如果不调用任何 `when_matched_*` / `when_not_matched_*`，`execute` 会有什么效果？
**答案**：默认 `when_matched = DoNothing`、`when_not_matched = DoNothing`、`when_not_matched_by_source = Keep`（见 [execute_merge_insert 第 258/270/282 行](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/merge.rs#L252-L284)），所以表不会变化，但仍会提交一个新版本。

**练习 2**：`when_matched_update_all(Some("target.age = 0"))` 里，`target` 和 `source` 分别指什么？
**答案**：`target` 指表内**旧数据**的列，`source` 指**新数据**的列（见 [merge.rs:110-116](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/merge.rs#L110-L116)）。这个条件表示「只更新那些旧数据 age 为 0 的匹配行」。

**练习 3**：为什么文档建议「按主键逐行改很多行」时用 `merge_insert` 而不是反复 `update`？
**答案**：见 [Table::update 文档 table.rs:1047-1050](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1047-L1050)。`update` 每次都要扫描+写文件，而 `merge_insert` 把「找匹配 + 更新 + 插入」合并成一次批量操作，一次提交处理所有行，开销低得多。

#### 4.4.6 进阶补充：LSM（MemWAL）写路径

`merge_insert` 还有一条较新的**LSM 写路径**。当表通过 `set_lsm_write_spec` 安装了一个 `LsmWriteSpec`（如 `LsmWriteSpec::bucket("id", 1)`）后，符合 upsert 形态的 `merge_insert` 调用会被路由到 Lance 的 MemWAL 分片写器，走 LSM 式追加而非标准合并路径。

这段逻辑的入口：

[`execute_merge_insert 第 235-250 行：LSM 路由 — merge.rs:230-250](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/merge.rs#L235-L250)

LSM 路径有几个约束（见 [merge/lsm.rs 模块文档 第 4-17 行](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/merge/lsm.rs#L4-L17)）：

- 每次调用必须命中**单个分片**（所有行路由到同一个 shard），否则报 `InvalidInput`。
- 必须是 upsert 形态（同时开启 `when_matched_update_all` 和 `when_not_matched_insert_all`），insert-only 会被拒绝。
- 在 LSM 路径下，`MergeResult` 的 `num_inserted_rows`/`num_updated_rows`/`num_deleted_rows`/`version` 全是 0，只有 `num_rows` 有值（见 [merge.rs:550-576](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/merge.rs#L550-L576) 的 `lsm_merge_insert_bucket` 测试）——因为细分要等 compaction 才知道。

这条路径面向高吞吐 upsert 场景，初学者了解「存在这么一条可选路径」即可，日常 upsert 用标准路径（不装 LsmWriteSpec）就行。

## 5. 综合实践

把本讲四个模块串起来，完成一次「数据修正 → 结构升级 → 主键合并」的小任务。

**场景**：你有一张用户表 `users`，schema 为 `(id: Int32, name: Utf8, score: Int32)`，已有 3 行数据。现在要做三件事：

1. **批量修正**：用 `update` 把所有 `score < 60` 的行的 `score` 改成 60（及格线兜底）。
2. **结构升级**：用 `add_columns` 新增一列 `grade`，值为 `'pass'`（用一个常量 SQL 表达式占位，后续再用 update 按 score 计算）。
3. **主键合并**：用 `merge_insert` 做一次 upsert，传入若干新用户和若干已存在用户（`when_matched_update_all(None)` + `when_not_matched_insert_all()`），最后核验行数与字段值。

**参考实现骨架**（示例代码，非项目原有）：

```rust
// 1. 批量修正
table.update()
    .only_if("score < 60")
    .column("score", "60")
    .execute().await?;

// 2. 结构升级（新增常量列）
table.add_columns(
    NewColumnTransform::SqlExpressions(vec![("grade".into(), "'pass'".into())]),
    None,
).await?;

// 3. upsert
let mut b = table.merge_insert(&["id"]);
b.when_matched_update_all(None);
b.when_not_matched_insert_all();
let result = b.execute(Box::new(new_users_reader)).await?;
println!("inserted={}, updated={}", result.num_inserted_rows, result.num_updated_rows);
```

**核验要点**：
- 步骤 1 后：所有行 `score >= 60`。
- 步骤 2 后：`schema().fields()` 含 `grade`，每行值为 `"pass"`。
- 步骤 3 后：`count_rows()` 等于「原有 + 新增」；已存在的 id 被新数据覆盖。
- 每一步后 `version()` 单调递增。

**反思题**：如果把步骤 1 的 `only_if` 写错成 `only_if("score > 60")`，会发生什么？（答：只会改 `score > 60` 的行，反而把高分改成 60——条件写反是 update 最常见的事故，务必先 `query` 验证命中范围再 execute。）

## 6. 本讲小结

- 四个数据/结构修改操作（`update` / `delete` / `add_columns` 等 / `merge_insert`）共享同一套「`ensure_mutable` 守卫 → 快照 → 委托 Lance → 升版本」骨架；LanceDB 核心主要做**参数搬运与版本回写**，真正的算法在 Lance crate。
- `update` 是构建器风格：`only_if` 定 WHERE、`column` 定 SET（至少一个），`execute` 才落盘；返回 `UpdateResult{rows_updated, version}`。
- `delete` 直接接受 `Predicate`（SQL 字符串或 `Expr`），内部按谓词类型走两条对称路径；删除是**逻辑的**，物理回收靠 `optimize`；**0 行命中也会升版本**。
- `schema_evolution` 提供 add/alter/drop columns 及 `update_field_metadata`，均直接 async、每次升版本；类型 `NewColumnTransform`/`ColumnAlteration` 直接复用 Lance，cast 能力受 Lance 限制（如 Int32→Float64 不支持）。
- `merge_insert` 用 `when_matched` / `when_not_matched` / `when_not_matched_by_source` 三种情况配置 upsert，条件用 `target.`/`source.` 前缀；标准路径下 `num_rows = num_inserted + num_updated`；另有可选的 MemWAL LSM 高吞吐写路径。
- 所有操作都同时被本地 `NativeTable` 与远程 `RemoteTable` 支持（远程实现见 `remote/table.rs`），对外接口完全一致。

## 7. 下一步学习建议

- **回到 u5-l1（optimize）**：本讲多次提到「逻辑删除」与「版本膨胀」。理解 `optimize` 如何做 compaction/prune 回收空间，才能把本讲操作的「副作用」收尾干净。
- **进入 u5-l3（版本与时间旅行）**：本讲的 `version` 字段和 `ensure_mutable` 守卫都指向版本机制。下一讲讲 `checkout` 历史版本与 `read_consistency`，能让你彻底理解「为什么 checkout 旧版本后不能写」。
- **阅读源码延伸**：想深入了解 upsert 的并发控制，可读 `merge.rs` 中 `retry_timeout` / `num_attempts` 的重试逻辑，以及 Lance 核心的 `MergeInsertBuilder`；想了解远程后端如何把本讲操作转为 HTTP，可读 `rust/lancedb/src/remote/table.rs` 中对应方法（如 [remote update](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L1988)、[remote merge_insert](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L2170)）。
