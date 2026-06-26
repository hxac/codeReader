# 优化：压缩与索引重建

> 单元 u5「表生命周期与优化」· 第 1 讲
> 最小模块：`table (optimize)`
> 依赖：u4-l1（索引总览与 Index 枚举）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 LanceDB 为什么需要 `optimize`：追加式写入会产生碎片文件，多版本会占用磁盘空间，新写入的数据默认不会进入已建索引。
- 掌握 `OptimizeAction` 这个统一入口如何把 **压缩（Compact）、清理（Prune）、索引增量优化（Index）** 三件事收拢成一个 API。
- 读懂 `rust/lancedb/src/table/optimize.rs` 中三类操作的实现，以及它们如何委托到底层 `lance` crate。
- 看懂 `OptimizeStats` 返回的 `compaction` 与 `prune` 统计字段含义，并据此判断优化是否真的发生了。
- 动手写一段「反复 `add` 小批次 → `optimize` → 对比文件数」的代码，亲眼看到压缩收益。

本讲承接 u4-l1（你已经知道 LanceDB 的索引分标量 / 向量 / 全文三类），把视角从「建索引」转向「表跑了一段时间后怎么维护」。`optimize` 是 LanceDB 里最接近数据库 `VACUUM` 的概念。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 Lance 的「追加式 + 不可变文件」存储

LanceDB 的本地后端是 Lance 列式格式（见 u1-l1、u1-l4）。Lance 用**只读、不可变（immutable）的数据文件**来保证并发安全和读性能。这意味着：

- 每次 `add` 一批数据，都会**新建一个或多个文件**，而不是去修改旧文件。
- 每次 `delete` / `update`，也并非原地改写，而是生成一个**新版本（version）**，在新版本里标记哪些行被删除。
- 所有改动都是 **additive（叠加式）** 的。

这样做的好处是读写不互锁、还能「时间旅行」（checkout 旧版本，见 u5-l3）。代价是：**频繁写入小批次会产生大量小文件（碎片）**，读时要扫描的文件数变多，性能下降。

### 2.2 多版本带来的空间膨胀

因为每次改动都新增一个版本而旧版本默认保留，磁盘空间会随版本数线性增长。旧版本是时间旅行的基础，不能随便删，但也不能永远留。

### 2.3 新数据默认「游离」在索引之外

这一点和 u4-l1 的索引知识直接相关：**新写入的数据不会自动进入已建的索引**。搜索时 Lance 会「并行扫描已索引部分 + 未索引部分」以保证结果正确，但未索引部分一旦变大，搜索就会越来越慢。

`optimize` 就是为了解决上述三个问题而存在的「磁盘维护」操作。源码里它被类比为 PostgreSQL 的 `VACUUM`：

> Similar to `VACUUM` in PostgreSQL, it offers different options to optimize different parts of the table on disk.

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `rust/lancedb/src/table/optimize.rs` | **本讲核心**。定义 `OptimizeAction`、`OptimizeStats`，以及 `execute_optimize` 主流程和三类操作的具体实现。 |
| `rust/lancedb/src/table.rs` | 在 `Table`（对外句柄）和 `BaseTable`（契约 trait）上暴露 `optimize` 方法；`NativeTable` 实现里一行委托给 `optimize::execute_optimize`。还提供 `num_small_files` / `count_fragments` 等诊断方法。 |
| `rust/lancedb/src/table/dataset.rs` | `DatasetConsistencyWrapper` 封装底层 Lance `Dataset`，提供 `ensure_mutable`（检查表是否可写）、`get` / `update`（读写底层句柄）等能力。三类优化操作都先经过它。 |
| `rust/lancedb/src/remote/table.rs` | `RemoteTable` 的 `optimize` 实现——注意它**返回 NotSupported**，优化只在本地后端可用。 |

底层 `lance` crate 提供 `compact_files` / `cleanup_old_versions` / `optimize_indices` 的真正算法，`lance_index` 提供 `OptimizeOptions`。本讲只讲到 LanceDB 核心如何「搬运」它们。

## 4. 核心概念与源码讲解

### 4.1 为什么需要 optimize：碎片、多版本与游离数据

#### 4.1.1 概念说明

把第 2 节的三个直觉落到 LanceDB 的语义上，`optimize` 对应解决三类「债」：

| 问题 | 现象 | 对应操作 |
| --- | --- | --- |
| 大量小文件（碎片） | 文件多、读放大 | **Compaction 压缩** |
| 旧版本占用空间 | 磁盘膨胀 | **Prune 版本清理** |
| 新数据游离在索引外 | 搜索变慢 | **Index 索引增量优化** |

#### 4.1.2 核心流程

一条数据从写入到「需要 optimize」的生命周期：

```text
add(batch) ──► 新建数据文件（碎片 +1） ──► 新版本（旧版本保留）
                                              │
                                   索引未更新 ──► 未索引行 +1
                                              │
              optimize(Compact) ◄── 文件数变大、读变慢
              optimize(Prune)   ◄── 磁盘占用变大
              optimize(Index)   ◄── 未索引行变多、搜索变慢
```

#### 4.1.3 源码精读：诊断方法

LanceDB 提供了两个能直接「看到碎片」的诊断方法，它们都定义在 `NativeTable` 上，直接委托给底层 Lance `Dataset`：

- [rust/lancedb/src/table.rs:2400-2402](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L2400-L2402) —— `count_fragments()` 返回当前版本的数据分片（fragment）数量，分片数越多说明碎片越严重。
- [rust/lancedb/src/table.rs:2408-2415](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L2408-L2415) —— `num_small_files(max_rows_per_group)` 返回「小于目标行数的过小文件」数量，这是判断「该不该压缩」最直接的信号。

`fragment` 是 Lance 里的一个概念：一个数据文件内部按行切成的逻辑片段。压缩的本质就是把多个小 fragment 合并成更少、更大的 fragment。

#### 4.1.4 代码实践：观察碎片

1. **实践目标**：在不执行任何优化的前提下，看到「反复写入 → 碎片增长」。
2. **操作步骤**：复用 `optimize.rs` 测试里的写法（见 4.3.4），建一张表后循环 `add` 5 个小批次，每次 `add` 后调用 `table.count_fragments().await` 打印。
3. **需要观察的现象**：`count_fragments` 随 `add` 次数线性上升。
4. **预期结果**：6 次写入（1 次建表 + 5 次 add）后，fragment 数约为 6（每次 add 至少产生一个 fragment）。**待本地验证**（具体数字取决于 Lance 的写入分片估算，见 u2-l3）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 LanceDB 选择「不可变文件 + 追加版本」而不是原地更新？

**参考答案**：不可变文件让读写不互锁，天然支持并发与多进程访问；同时保留旧版本使「时间旅行」（checkout 旧版本）成为可能。代价是碎片与空间膨胀，这正是 `optimize` 要还的「债」。

**练习 2**：如果一张表只用来搜索、从不写入或修改，还需要定期 `optimize` 吗？

**参考答案**：基本不需要。压缩针对「频繁写入产生的小文件」，版本清理针对「频繁修改产生的旧版本」。只读表这两者都不会增长，所以 `optimize` 收益很小（源码注释也明确说明：仅搜索场景下 compaction 非必要）。

---

### 4.2 OptimizeAction：三类操作的统一入口

#### 4.2.1 概念说明

`optimize` 不接受一堆布尔参数（`do_compact=true, do_prune=false ...`），而是用一个枚举 `OptimizeAction` 表达「这次想干哪一类活」。这是一种典型的 Rust API 设计：用枚举把互斥的选项收拢，避免参数爆炸（这也是 u2 的连接、u3 的查询一贯的 Builder / 配置风格）。

`OptimizeAction` 有四个变体，默认是 `All`（一起做）：

```rust
#[derive(Default)]
pub enum OptimizeAction {
    #[default]
    All,                                  // 全做（默认）
    Compact { options, remap_options },   // 只压缩
    Prune { older_than, delete_unverified, error_if_tagged_old_versions }, // 只清理旧版本
    Index(OptimizeOptions),               // 只做索引增量优化
}
```

#### 4.2.2 核心流程

```text
Table::optimize(action)
      │  （一行委托）
      ▼
BaseTable::optimize  ──► NativeTable::optimize  ──► optimize::execute_optimize(table, action)
                                                            │
                                  ┌─────────────────────────┼─────────────────────────┐
                                  ▼                         ▼                         ▼
                          match action {  All  →  Compact + Prune + Index 三步顺序执行
                                         Compact → compact_files_impl
                                         Prune   → cleanup_old_versions
                                         Index   → optimize_indices      }
```

#### 4.2.3 源码精读

`OptimizeAction` 的完整定义和**详尽的字段级文档注释**是本讲最重要的阅读材料，每种操作都解释了「为什么需要它」：

- [rust/lancedb/src/table/optimize.rs:29-90](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/optimize.rs#L29-L90) —— `OptimizeAction` 枚举定义。重点读三段文档注释：
  - `Compact`（约 35-48 行）解释「只读文件系统 → 每次写入产生新文件 → 小文件伤读写 → 压缩合并」。
  - `Prune`（约 49-73 行）解释「改动是叠加式的 → 旧版本保留以支持一致性与时间旅行 → 时间久了占空间 → 清理旧版本」。注意字段 `delete_unverified` 带 **WARNING**：只有在确认没有其它进程在写时才能置 `true`，否则可能损坏数据集。
  - `Index`（约 74-89 行）解释「新数据不进索引 → 搜索会并行扫未索引部分 → 未索引部分变大变慢 → 索引增量优化把新数据并入现有索引」。并指出：增量优化比重建快，但**不移动索引底层模型**（以 IVF 为例，只把新数据归入已有簇，不新增簇），所以偶尔仍需重建索引。

对外暴露链路（自上而下）：

- [rust/lancedb/src/table.rs:616](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L616) —— `BaseTable` trait 上的 `optimize(&self, action) -> Result<OptimizeStats>`，本地 / 远程表共用契约。
- [rust/lancedb/src/table.rs:1435-1453](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1435-L1453) —— `Table::optimize` 公开方法，文档给出调用频率的经验法则：**新增 / 修改 10 万行以上，或超过 20 次修改操作后**建议跑一次。
- [rust/lancedb/src/table.rs:2889-2892](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L2889-L2892) —— `NativeTable::optimize` 一行委托：`optimize::execute_optimize(self, action).await`。

而远程表**不支持**优化，云端由服务端托管：

- [rust/lancedb/src/remote/table.rs:2306-2311](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L2306-L2311) —— `RemoteTable::optimize` 先 `check_mutable()`，再返回 `Error::NotSupported { "optimize is not supported on LanceDB cloud." }`。

类型在 `table.rs` 顶部被重新导出，方便用户以 `lancedb::table::OptimizeAction` 引用：

- [rust/lancedb/src/table.rs:82-83](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L82-L83) —— `pub use lance_index::optimize::OptimizeOptions;` 与 `pub use optimize::{CompactionOptions, OptimizeAction, OptimizeStats};`。

#### 4.2.4 代码实践：阅读 enum 文档

1. **实践目标**：建立「读源码文档注释 = 读官方说明书」的习惯。
2. **操作步骤**：打开 [rust/lancedb/src/table/optimize.rs:29-90](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/optimize.rs#L29-L90)，对照本节表格，用自己的话复述 `Compact` / `Prune` / `Index` 各自要还的「债」。
3. **需要观察的现象**：注意 `Prune` 字段里有两处 **WARNING**（`delete_unverified` 与 `error_if_tagged_old_versions`），体会它们为什么危险。
4. **预期结果**：能口头说明「为什么 `Index` 增量优化后，偶尔还要重建索引」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `OptimizeAction` 用枚举而不是 `fn optimize(compact: bool, prune: bool, index: bool)`？

**参考答案**：枚举表达的是「这次做哪一类完整操作」，每类还携带各自专属的配置（`Compact` 带 `CompactionOptions`，`Prune` 带时间阈值等）。用一堆布尔参数既无法携带这些配置，也无法表达「`All` = 三件一起做」这种聚合语义，还会出现 `compact=false & prune=false & index=false` 这种无意义组合。枚举把这些互斥选项收拢，编译期就排除了无效组合。

**练习 2**：在 LanceDB Cloud（远程后端）上调用 `table.optimize()` 会发生什么？

**参考答案**：`RemoteTable::optimize` 会先做可写性检查，然后返回 `Error::NotSupported`，提示 optimize 在云端不支持。云端的压缩 / 清理由服务端托管，客户端无需（也无法）手动触发。

---

### 4.3 Compaction 压缩：合并小文件

#### 4.3.1 概念说明

Compaction 是 `optimize` 里最常用的一类。它的目标是：**把多个小文件（小 fragment）合并成更少、更大的文件**，减少读时要打开的文件数。它对应 `OptimizeAction::Compact`，配置类型是 `CompactionOptions`（来自 `lance::dataset::optimize`，被重新导出）。

从测试可确认 `CompactionOptions` 至少有两个字段：

- `target_rows_per_fragment: usize` —— 目标每个 fragment 的行数，压缩会尽量往这个值靠。
- `defer_index_remap: bool` —— 是否延迟「索引重映射」。压缩会改变行的物理位置，索引里记录的位置需要更新；`true` 表示把这部分更新推迟（通常由后续的 `Index` 优化统一处理），可加快压缩本身。

#### 4.3.2 核心流程

```text
compact_files_impl(table, options, remap_options):
  1. table.dataset.ensure_mutable()        // 表必须处于「可写 / 跟踪最新版」状态
  2. dataset = (*table.dataset.get()).clone()  // 取出底层 Dataset 并克隆（拿到所有权）
  3. metrics = lance::compact_files(&mut dataset, options, remap_options)  // 真正合并
  4. table.dataset.update(dataset)         // 把新版本写回包装器
  5. return metrics                        // 返回 CompactionMetrics
```

第 2、4 步是「取出来改、再塞回去」的写模式，所有写操作都遵循它（见 4.5.3 对 `DatasetConsistencyWrapper` 的讲解）。

#### 4.3.3 源码精读

压缩的具体实现函数：

- [rust/lancedb/src/table/optimize.rs:148-158](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/optimize.rs#L148-L158) —— `compact_files_impl`：注意它把 `compact_files`（来自 `lance::dataset::optimize`，见 [第 12 行](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/optimize.rs#L12) 的 import）作为底层算法调用，LanceDB 核心本身**不实现合并算法**，只做参数搬运与版本回写。返回值 `CompactionMetrics` 含 `fragments_removed` / `fragments_added` / `files_removed` / `files_added` 等字段（字段名可从 Python / Node 绑定交叉确认，见下文 4.5）。

`execute_optimize` 里的 `Compact` 分支：

- [rust/lancedb/src/table/optimize.rs:187-192](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/optimize.rs#L187-L192) —— 把 `metrics` 塞进 `stats.compaction`。

最贴近真实使用的测试（读懂它 = 会用压缩）：

- [rust/lancedb/src/table/optimize.rs:228-305](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/optimize.rs#L228-L305) —— `test_optimize_compact_simple`：建表（100 行）→ `add` 5 次（每次 100 行，共 600 行）→ 用 `OptimizeAction::Compact { target_rows_per_fragment: 1000 }` 压缩 → 断言 `fragments_removed > 0`、行数仍为 600、内容完整有序。这个测试就是本讲代码实践的蓝本。

#### 4.3.4 代码实践（本讲主实践）：反复 add → optimize → 看压缩收益

这是本讲规格要求的核心实践。

1. **实践目标**：亲眼看到「碎片随写入增长、压缩后碎片减少、数据零丢失」。
2. **操作步骤**：新建一个二进制 example 或临时测试，照搬下面这段**示例代码**（改编自 `test_optimize_compact_simple`，非项目原有文件）：

   ```rust
   // 示例代码：放在 rust/lancedb/examples/optimize_demo.rs，不要提交
   use arrow_array::{Int32Array, RecordBatch};
   use arrow_schema::{DataType, Field, Schema};
   use lancedb::{connect, table::{OptimizeAction, CompactionOptions}};
   use std::sync::Arc;

   #[tokio::main]
   async fn main() -> anyhow::Result<()> {
       let conn = connect("memory://").execute().await?;
       let schema = Arc::new(Schema::new(vec![Field::new("i", DataType::Int32, false)]));
       let mk = |rng: std::ops::Range<i32>| RecordBatch::try_new(
           schema.clone(), vec![Arc::new(Int32Array::from_iter_values(rng))]).unwrap();

       let table = conn.create_table("t", mk(0..100)).execute().await?;
       for i in 0..5 {
           table.add(mk(i*100+100..(i+1)*100+100)).execute().await?;
           println!("after add#{}: fragments = {}", i,
               table.count_fragments().await?);          // 观察 4.1 的碎片增长
       }
       println!("small files (target 1000): {}",
           table.num_small_files(1000).await?);          // 诊断：有多少过小文件

       let stats = table.optimize(OptimizeAction::Compact {
           options: CompactionOptions { target_rows_per_fragment: 1000, ..Default::default() },
           remap_options: None,
       }).await?;
       println!("fragments_removed = {:?}", stats.compaction.as_ref()
           .map(|m| m.fragments_removed));
       println!("fragments after   = {}", table.count_fragments().await?);
       println!("rows still        = {}", table.count_rows(None).await?);
       Ok(())
   }
   ```

   运行：`cargo run --example optimize_demo`（从仓库根目录）。注意 `count_fragments` / `num_small_files` 是 `NativeTable` 的方法，通过 `NativeTableExt` 的 `as_native()` 拿到原生表后调用；若 `Table` 未直接暴露，可改为在测试模块内调用，或仅依赖 `stats.compaction` 的指标。

3. **需要观察的现象**：
   - `count_fragments` 随 `add` 单调上升。
   - `num_small_files(1000)` 在压缩前 > 0。
   - 压缩后 `stats.compaction.fragments_removed > 0`，且 `count_fragments` 明显下降。
   - `count_rows` 全程稳定为 600。
4. **预期结果**：压缩前后行数不变、内容不变，但 fragment 数显著减少。具体 fragment 数值**待本地验证**（受 Lance 写入分片估算影响）。

#### 4.3.5 小练习与答案

**练习 1**：把 `target_rows_per_fragment` 从 `1000` 改成 `50`（小于单批 100 行），再跑压缩，`fragments_removed` 会怎样变化？

**参考答案**：当目标行数小于现有 fragment 行数时，压缩几乎无事可做——现有 fragment 已经「够大」，不需要合并。预期 `fragments_removed` 接近 0。这说明压缩只合并「过小」的 fragment，不会为了凑目标行数而拆分大 fragment。**待本地验证。**

**练习 2**：`defer_index_remap: true` 解决了什么问题？代价是什么？

**参考答案**：压缩改变了行的物理位置，索引里记录的位置（如 IVF 桶内偏移）需要重新映射（remap）。`defer_index_remap = true` 把这个相对昂贵的重映射推迟，让压缩更快完成；代价是压缩刚结束时索引还指向旧位置，需要随后跑一次 `OptimizeAction::Index`（或重建）来修正，否则该索引在修正前可能命中率下降。它本质上是把成本从「压缩」转移到「索引优化」。

---

### 4.4 Prune 清理与 Index 增量优化

压缩之外，`optimize` 还有两类操作。本节把它们放在一起讲，因为实现结构高度对称（都遵循 4.3.2 的「取出来改、塞回去」模式）。

#### 4.4.1 概念说明

- **Prune（版本清理）**：删除早于某个时间阈值的旧版本，释放磁盘空间。一旦某个版本被清理，就再也无法 `checkout` 到它（时间旅行到该版本失效）。对应 `OptimizeAction::Prune`。
- **Index（索引增量优化）**：把游离在索引之外的新数据并入现有索引，比重建快，但不调整索引底层模型（IVF 不新增簇）。对应 `OptimizeAction::Index(OptimizeOptions)`，其中 `OptimizeOptions` 来自 `lance_index::optimize`。

#### 4.4.2 核心流程

两者都先 `ensure_mutable()` 再操作底层 Dataset：

```text
cleanup_old_versions(table, older_than, delete_unverified, error_if_tagged):
  ensure_mutable()  →  dataset.get()  →  dataset.cleanup_old_versions(...)  →  RemovalStats

optimize_indices(table, options):
  ensure_mutable()  →  dataset = get().clone()  →  dataset.optimize_indices(options)  →  dataset.update(dataset)
```

注意一个小差异：`cleanup_old_versions` **不 clone**（只读地清理物理文件，不改 Dataset 版本号），而 `optimize_indices` **要 clone + update**（索引更新会产生新版本）。这从源码签名与实现可以区分。

#### 4.4.3 源码精读

**Prune 实现**：

- [rust/lancedb/src/table/optimize.rs:129-140](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/optimize.rs#L129-L140) —— `cleanup_old_versions`：调用底层 `Dataset::cleanup_old_versions`，返回 `RemovalStats`（字段 `bytes_removed`、`old_versions`，含义见测试）。
- [rust/lancedb/src/table/optimize.rs:193-207](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/optimize.rs#L193-L207) —— `execute_optimize` 的 `Prune` 分支：若用户没传 `older_than`，默认保留 7 天。
- [rust/lancedb/src/table/optimize.rs:307-381](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/optimize.rs#L307-L381) —— `test_optimize_prune_versions`：5 次 add 产生旧版本 → `older_than = 0 天` + `delete_unverified = true` 清理 → 断言 `old_versions == 5`、`bytes_removed > 0`、当前数据仍为 60 行。

**Index 实现**：

- [rust/lancedb/src/table/optimize.rs:105-112](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/optimize.rs#L105-L112) —— `optimize_indices`：clone 出 Dataset，调 `dataset.optimize_indices(options)`，再 `update` 回去。
- [rust/lancedb/src/table/optimize.rs:208-210](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/optimize.rs#L208-L210) —— `execute_optimize` 的 `Index` 分支。
- [rust/lancedb/src/table/optimize.rs:383-443](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/optimize.rs#L383-L443) —— `test_optimize_index`：建表 100 行 → 建 BTree 索引（此时 100 行已索引）→ 再 add 100 行（这 100 行 `num_unindexed_rows == 100`）→ `OptimizeAction::Index` 优化 → 断言 `num_indexed_rows == 200`、`num_unindexed_rows == 0`。这个测试把「游离数据被并入索引」演示得最清楚。

> 提示：`num_indexed_rows` / `num_unindexed_rows` 来自 `IndexStatistics`，是 u4-l5 讲过的 `index_stats()` 返回值。

#### 4.4.4 代码实践：验证索引增量优化

1. **实践目标**：确认「新数据最初游离于索引外，Index 优化后被并入」。
2. **操作步骤**：仿照 `test_optimize_index`（[第 383-443 行](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/optimize.rs#L383-L443)）写一个 example：建表 + 建标量索引 + add 新数据，分别用 `index_stats(name)` 打印优化前后的 `num_indexed_rows` / `num_unindexed_rows`。
3. **需要观察的现象**：优化前 `num_unindexed_rows > 0`；优化后变为 0，`num_indexed_rows` 等于总行数。
4. **预期结果**：与测试断言一致（200 / 0）。这是**源码阅读型 + 可运行型**结合的实践。

#### 4.4.5 小练习与答案

**练习 1**：`Prune` 的 `older_than` 默认值是多少？为什么不是 0？

**参考答案**：默认 7 天（见 [execute_optimize 第 199-201 行](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/optimize.rs#L199-L201) 的 `unwrap_or(Duration::try_days(7)...)`）。不用 0 是因为 7 天内的文件可能是某个**正在进行的写入事务**的一部分，贸然删除会破坏数据集一致性。源码注释明确警告：把 `delete_unverified` 置 `true` 才会无视这 7 天保护，且仅在你确认没有其它进程在写时才能这么做。

**练习 2**：为什么 `Index` 增量优化后，文档仍建议「偶尔重建索引」？

**参考答案**：增量优化只把新数据**归入现有索引结构**，不重建索引的底层模型。以 IVF 为例，它把新向量分配给已有的质心簇，但不会根据新数据重新跑 k-means、不新增簇。当新数据的分布与建索引时差异较大时，现有簇已不是最优划分，召回率会下降。因此数据累积到一定程度后，仍需 `drop_index` + `create_index` 全量重建。

---

### 4.5 主流程 execute_optimize、OptimizeStats 与一致性保护

#### 4.5.1 概念说明

把前三节拼起来的是 `execute_optimize` 这个总调度函数。它做两件事：按 `action` 分派到对应实现，并把结果汇总成 `OptimizeStats`。同时，所有写操作都被 `ensure_mutable()` 这道「闸门」保护——它确保表不在「时间旅行（checkout 旧版本）」状态下被改写。

`OptimizeStats` 只有两个字段：

```rust
pub struct OptimizeStats {
    pub compaction: Option<CompactionMetrics>,  // 来自 lance，含 fragments/files 的增删计数
    pub prune: Option<RemovalStats>,            // 来自 lance，含 bytes_removed / old_versions
}
```

哪个字段是 `Some` 取决于这次跑了什么：`Compact` 只填 `compaction`，`Prune` 只填 `prune`，`Index` 两者都是 `None`（索引优化不返回结构化统计），`All` 两者都填。测试 [test_optimize_stats_default（第 513-519 行）](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/optimize.rs#L513-L519) 正是验证默认值两者皆 `None`。

#### 4.5.2 核心流程

`OptimizeAction::All`（默认）会**顺序**执行三步：先 Compact，再 Prune，最后 Index：

```text
execute_optimize(table, All):
  1. stats.compaction = compact_files_impl(table, CompactionOptions::default(), None)
  2. stats.prune      = cleanup_old_versions(table, older_than = 7 天, None, None)
  3. optimize_indices(table, OptimizeOptions::default())     // 注意：不进 stats
  return stats
```

注意 `All` 里 `Index` 步骤的返回值被丢弃（`optimize_indices` 返回 `Result<()>`），所以 `OptimizeStats` 里**没有索引优化的统计**——这也是为什么 `index_stats()` 要单独提供（见 u4-l5）。

#### 4.5.3 源码精读

**主调度**：

- [rust/lancedb/src/table/optimize.rs:163-213](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/optimize.rs#L163-L213) —— `execute_optimize`：`match action` 四分支。注释（第 173 行）解释为何用独立 helper 函数而非递归调用——「avoid async recursion issues」。
- [rust/lancedb/src/table/optimize.rs:172-186](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/optimize.rs#L172-L186) —— `All` 分支三步走。
- [rust/lancedb/src/table/optimize.rs:92-100](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/optimize.rs#L92-L100) —— `OptimizeStats` 定义。

**一致性闸门 `ensure_mutable`**（贯穿三类操作的第一步）：

- [rust/lancedb/src/table/dataset.rs:180-190](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/dataset.rs#L180-L190) —— `ensure_mutable`：若 `pinned_version.is_some()`（表被 checkout 到某个旧版本做时间旅行），返回 `Error::InvalidInput { "table cannot be modified when a specific version is checked out" }`。
- [rust/lancedb/src/table/dataset.rs:18-26](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/dataset.rs#L18-L26) —— `DatasetConsistencyWrapper` 持有 `state: Arc<Mutex<DatasetState>>` 与一致性模式。
- [rust/lancedb/src/table/dataset.rs:111-136](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/dataset.rs#L111-L136) —— `get()`：按一致性模式（Lazy/Strong/Eventual）返回当前 Dataset 句柄。
- [rust/lancedb/src/table/dataset.rs:146-161](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/dataset.rs#L146-L161) —— `update()`：写回新版本，且只在新版本号 ≥ 当前时才接受（防止并发写回退）。

`ensure_mutable` 的保护有一个专门测试：

- [rust/lancedb/src/table/optimize.rs:688-731](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/optimize.rs#L688-L731) —— `test_optimize_fails_on_checked_out_table`：用 `rstest` 把四种 `OptimizeAction` 全部跑一遍，先 `table.checkout(1)`，再 `optimize`，断言全部报错且错误信息含「cannot be modified when a specific version is checked out」。

**多语言绑定的统计映射**（佐证 Lance 类型字段名）：

- [python/src/table.rs:944-955](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/python/src/table.rs#L944-L955) —— Python 绑定把 `CompactionMetrics`（`fragments_added/removed`、`files_added/removed`）与 `RemovalStats`（`bytes_removed`、`old_versions`）映射成 Python 的 `OptimizeStats`。注意 Python 端把 `old_versions` 重命名为 `old_versions_removed`。
- [python/src/table.rs:900-957](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/python/src/table.rs#L900-L957) —— Python 的 `optimize` 把 Rust 的「一次 `OptimizeAction::All`」拆成了「Compact → Prune → Index」三次单独调用再拼装结果，但对用户透明。

#### 4.5.4 代码实践：触发 ensure_mutable 报错

1. **实践目标**：理解「时间旅行状态下不能 optimize」这条硬约束。
2. **操作步骤**：照搬 `test_optimize_fails_on_checked_out_table` 的前半段——建表、add、`table.checkout(1).await`、然后 `table.optimize(OptimizeAction::All).await`，打印 `Err`。
3. **需要观察的现象**：`optimize` 返回 `Err`，错误信息含「cannot be modified when a specific version is checked out」。
4. **预期结果**：与测试断言一致。这说明 optimize 属于「写」操作，被 `DatasetConsistencyWrapper::ensure_mutable` 拦截（参见 u5-l3 时间旅行）。

#### 4.5.5 小练习与答案

**练习 1**：`OptimizeAction::All` 执行后，`OptimizeStats.index` 有没有字段告诉你索引优化了多少行？

**参考答案**：没有。`OptimizeStats` 只有 `compaction` 和 `prune` 两个字段，索引优化步骤的返回值是 `Result<()>`，被丢弃。要查看索引优化效果，需另外调用 `index_stats(name)` 看 `num_indexed_rows` / `num_unindexed_rows`（u4-l5）。

**练习 2**：为什么 `execute_optimize` 不直接在 `All` 分支里递归调用 `execute_optimize(table, Compact{..})`，而是调独立的 `compact_files_impl`？

**参考答案**：源码第 173 行注释点明——为了避免 async 递归（async fn 直接递归自身在 Rust 里难以表达且会带来装箱 / 栈问题）。改调同步签名的独立 helper 函数既绕开了递归，又能复用同一份实现（`Compact` 分支也调 `compact_files_impl`）。

---

## 5. 综合实践

把本讲三件事串成一个完整维护脚本。**目标**：模拟一张表「写一批 → 写一批 → 建索引 → 再写 → 全面优化」的真实生命周期，并量化每一步的效果。

**操作步骤**（示例代码，非项目原有文件）：

```rust
// 示例代码：综合实践
use lancedb::{connect, index::Index, table::OptimizeAction};
// schema / batch 构造参考 4.3.4

// 1. 建表 + 两次小批量 add（产生碎片 + 多版本）
// 2. 对标量列建一个 BTree 索引
// 3. 再 add 一批（产生游离于索引之外的数据）
// 4. 记录「优化前」基线：count_fragments()、index_stats(name) 的 num_unindexed_rows
// 5. table.optimize(OptimizeAction::All).await   // 一次做齐三件事
// 6. 记录「优化后」：count_fragments() 应下降；index_stats 的 num_unindexed_rows 应为 0
// 7. 打印返回的 OptimizeStats：compaction.fragments_removed、prune.old_versions、prune.bytes_removed
```

**需要观察并解释的现象**：

| 指标 | 优化前 | 优化后 | 解释 |
| --- | --- | --- | --- |
| `count_fragments` | 较大 | 明显下降 | Compact 合并了小文件 |
| `num_unindexed_rows` | > 0 | 0 | Index 增量优化并入了游离数据 |
| `stats.prune.old_versions` | — | > 0 | Prune 清理了 add 产生的旧版本 |
| `count_rows` | N | N | 数据零丢失（核心正确性保证） |

**预期结果**：四项指标的变化方向符合上表。具体数值**待本地验证**。完成后，你应该能回答：什么时候该用 `All`，什么时候只该 `Compact`，以及为什么 `optimize` 之后行数绝不能变。

> 进阶思考：如果第 5 步改成 `OptimizeAction::Index(Default::default())`，`OptimizeStats` 的两个字段会是什么？为什么？（答案见 4.5.1：两者皆 `None`。）

## 6. 本讲小结

- Lance 用「不可变文件 + 追加版本」换取并发安全与时间旅行，代价是**碎片文件、版本膨胀、索引游离**三类「债」，`optimize` 就是还债工具，类比 PostgreSQL 的 `VACUUM`。
- `OptimizeAction` 用一个枚举把 **Compact（合并小文件）/ Prune（清理旧版本）/ Index（索引增量优化）/ All（全做）** 四种互斥操作收拢，避免布尔参数爆炸；默认 `All`。
- 三类操作的实现结构高度对称：都先 `ensure_mutable()` 把关，再委托底层 `lance` crate（`compact_files` / `cleanup_old_versions` / `optimize_indices`），LanceDB 核心**只做参数搬运与版本回写**，真正的算法在 `lance` / `lance-index`。
- 写模式统一为「`get().clone()` 取出 → 改 → `update()` 塞回」；`DatasetConsistencyWrapper::ensure_mutable` 在表被 checkout 到旧版本时拦截一切写操作（含 optimize）。
- `OptimizeStats` 只有 `compaction` 与 `prune` 两个 `Option` 字段；跑了哪步就填哪个，`Index` 步骤不返回结构化统计（要看索引效果用 `index_stats`）。
- `optimize` **仅本地后端支持**：`RemoteTable::optimize` 直接返回 `NotSupported`，云端由服务端托管。
- 经验法则（来自 `Table::optimize` 文档）：新增 / 修改 10 万行以上，或超过 20 次修改操作后，建议跑一次 `optimize`。

## 7. 下一步学习建议

- **u5-l2 数据增删改与模式演化**：`update` / `delete` 也是「产生新版本」的写操作，学完会更清楚为什么频繁 CRUD 后必须 `optimize`。
- **u5-l3 版本管理与时间旅行**：深入 `checkout` 与 `read_consistency`，理解 `ensure_mutable` 为什么在 checkout 旧版本时拦截 optimize。
- **u4-l5 索引统计、配置与等待机制**：`index_stats` 与 `wait_for_index`，配合本讲的 Index 优化观察效果。
- **延伸阅读**：直接打开 [rust/lancedb/src/table/optimize.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/optimize.rs) 通读全部 8 个单元测试，它们覆盖了空表、schema 保留、checkout 拦截等边界场景，是理解 `optimize` 行为最可靠的「行为说明书」。
