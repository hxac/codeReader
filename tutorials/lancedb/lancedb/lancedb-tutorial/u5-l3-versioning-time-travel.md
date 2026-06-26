# 版本管理与时间旅行

## 1. 本讲目标

在前两讲（u5-l1、u5-l2）里，我们反复提到一句话：「每次写操作都会产生一个新版本」。但这个「版本」到底是什么？能不能回到昨天那份数据？能不能给某个版本起个人能记住的名字？两个进程同时读写同一张表，后写的能不能被先读到的看见？

本讲把 LanceDB 的「版本」从一句话承诺，拆成可操作的能力。覆盖两个最小模块：

- **`table`**：版本机制（`version` / `list_versions`）、时间旅行（`checkout` / `checkout_tag`）、回到最新（`checkout_latest`）、恢复（`restore`）、版本别名（`tags`），以及本地后端的「读一致性」三档模式。
- **`database (read_freshness)`**：远程命名空间后端独有的「读新鲜度」机制——用 HTTP 头告诉服务端「我至少要读到这个时间点之后的快照」。

学完本讲，你应当能够：

1. 解释 Lance 的「不可变文件 + 单调递增版本」模型，并能用 `version()` / `list_versions()` 观察。
2. 用 `checkout(version)` 或 `checkout_tag(tag)` 把表「钉」到历史快照上做只读查询，用 `checkout_latest()` 解除。
3. 理解本地后端三种读一致性模式（Lazy / Strong / Eventual）的取舍，并能用 `read_consistency_interval` 切换。
4. 看懂远程命名空间后端如何用 `x-lancedb-min-timestamp` 头避免读到陈旧快照。

## 2. 前置知识

### 2.1 Lance 的「不可变文件 + 版本」模型（回顾）

LanceDB 建立在 Lance 列式格式之上。Lance 的写策略是**追加新文件，不改旧文件**：每一次会改变数据或结构的操作（`add` / `update` / `delete` / 加列 / 建索引……）都会提交一个**新版本（version）**——写一组新文件，并在 manifest（清单）里记录「这一版里哪些文件有效、哪些被淘汰」。

两个直接后果（在 u5-l2 已建立，这里复用）：

- **版本号单调递增**：哪怕一次什么都没改（比如删了 0 行），也会提交一个新版本。
- **旧文件不会立刻消失**：旧版本的数据文件还留在对象存储里，只要那个版本没被清理，就能被重新「读」出来——这正是「时间旅行」的物理基础。

用一句话概括：**版本是只读快照的编号，时间旅行就是把句柄临时指向某个旧快照**。

### 2.2 句柄（Handle）与底层 Dataset 是两回事

在 u2-l2 我们讲过 `Table` 是一个轻量句柄，内部持有 `Arc<dyn BaseTable>`；本地实现 `NativeTable` 又持有一个 `DatasetConsistencyWrapper`，后者封装底层 Lance 的 `Dataset`。

关键认知：**「当前指向哪个版本」是句柄内部的一块可变状态，而不是表本身的一个属性**。同一个 LanceDB 表可以被多个 `Table` 句柄同时打开，它们各自维护「我看的是哪一版」。`checkout` 改的是「这个句柄看哪版」，不会动到别的句柄，也不会真的删数据。理解了这一点，本讲后面所有方法的行为都很自然。

### 2.3 读一致性（read consistency）问题

版本机制天然带出一个并发问题：

- 进程 A 持有一个 `Table` 句柄，打开时指向版本 1。
- 进程 B 往同一张表写入，提交了版本 2。
- 这时进程 A 再读，**读到的还是版本 1 还是版本 2？**

答案取决于「读一致性」配置。LanceDB 的设计是：**表自身永远内部一致**（自己写的自己立刻能看见），但**跨进程的可见性是可调的**——可以从「永远读最新」（强一致但慢）到「绝不自动刷新、要手动 `checkout_latest`」（最快但可能陈旧）之间选择。这是本讲第二部分的核心。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [rust/lancedb/src/table.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs) | 对外 `Table` 句柄方法（`version`/`checkout`/`restore`…）、`BaseTable` trait 契约、`Tags` trait、`NativeTable` 实现。 |
| [rust/lancedb/src/table/dataset.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/dataset.rs) | `DatasetConsistencyWrapper`：封装「当前版本指针」+「是否钉在旧版本」+ 三档读一致性模式。 |
| [rust/lancedb/src/connection.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs) | `ConnectBuilder::read_consistency_interval`：连接时设定读一致性档位。 |
| [rust/lancedb/src/database.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database.rs) | `ReadConsistency` 枚举：Manual / Eventual / Strong 三档语义。 |
| [rust/lancedb/src/database/read_freshness.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/read_freshness.rs) | 远程命名空间后端的读新鲜度机制：计算并注入 `x-lancedb-min-timestamp` 头。 |

> 全局结构提示：`Table` 句柄把调用委托给 `Arc<dyn BaseTable>`；本地 `NativeTable` 把语义翻译成对底层 Lance `Dataset` 的调用；远程 `RemoteTable` 把同样的调用转成 HTTP。版本/时间旅行的公共 API 在本地与远程后端之间是一致的，但「读新鲜度」只在远程命名空间后端才有意义（本地没有缓存服务端可读陈旧）。

## 4. 核心概念与源码讲解

### 4.1 版本机制：`version` 与 `list_versions`

#### 4.1.1 概念说明

「版本」是一个单调递增的整数，从 1 开始。**任何会改变数据或表结构的操作都会让版本号 +1**——写入、更新、删除、加列、建索引、`restore`，无一例外。反过来，**纯读操作（`query`、`count_rows`、`schema`、`list_versions`）不增版本**。

为什么要这么设计？因为 Lance 用「不可变文件 + 追加版本」换来了两件事：（1）写不阻塞读（读旧版本永远安全）；（2）每个历史版本都是一个完整、自洽的快照，可以随时被重新打开。版本号就是这些快照的「门牌号」。

`Version` 这个类型本身并不在 LanceDB 里定义，而是直接从底层 Lance 重新导出：

[rust/lancedb/src/table.rs:19](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L19) —— `pub use lance::dataset::Version;`，把底层 Lance 的 `Version` 结构体原样暴露（内含版本号、时间戳、manifest 路径等元信息）。这种「核心只搬运、真相在 Lance」的模式，我们在 u4 索引、u3-l5 FTS 里都见过。

#### 4.1.2 核心流程

```
写操作 (add/update/delete/...)        读操作 (version / list_versions)
        │                                       │
        ▼                                       ▼
  写一组新文件 + 更新 manifest            不触发任何写
        │                                       │
        ▼                                       ▼
  提交：version = 旧 version + 1      返回「当前句柄指向的版本号」
```

注意一个微妙点：`version()` 返回的是**「这个句柄当前看的是哪一版」**，不一定等于「表最新的版本」。如果句柄被 `checkout` 钉在了旧版本上，`version()` 返回的就是那个旧版本号。

#### 4.1.3 源码精读

对外 `Table::version()` 只是一行转发：

[rust/lancedb/src/table.rs:1551-1559](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1551-L1559) —— 文档注释明确写「Every operation that modifies the table increases version」，方法体 `self.inner.version().await` 委托给底层后端。

`list_versions()` 列出表的全部历史版本：

[rust/lancedb/src/table.rs:1619-1622](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1619-L1622) —— 返回 `Vec<Version>`，是表的完整版本史，可用于审计或决定 `checkout` 哪一版。

本地实现 `NativeTable::version()` 从 `DatasetConsistencyWrapper` 取出当前 `Dataset`，再读它的版本号：

[rust/lancedb/src/table.rs:2528-2530](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L2528-L2530) —— `self.dataset.get().await?.version().version`，`.version().version` 中第一个是 Lance 的版本信息结构体、第二个才是其中的整数版本号。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「写操作让版本号 +1，读操作不变」。

**操作步骤**（源码阅读型，配合本地运行；若无 Rust 编译环境，标注「待本地验证」）：

1. 打开测试文件 [rust/lancedb/src/table.rs:3833-3852](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L3833-L3852) 中的 `test_time_travel_write`。
2. 重点看这两行：
   - `let version = table.version().await.unwrap();` 记录写之前的版本。
   - `table.add(some_sample_data()).execute().await.unwrap();` 之后**没有**再调用 `version()`，但你能推断它应当变成 `version + 1`。
3. 用 `cargo test --features remote -p lancedb --lib test_time_travel_write -- --nocapture` 跑这个测试（命令见仓库 `AGENTS.md`）。

**需要观察的现象**：测试通过，说明 `add` 之后的版本确实比写之前大。

**预期结果**：写一次，版本号 +1。

> 待本地验证：具体版本数值依赖运行环境，但「+1」这一规律是测试保证的。

#### 4.1.5 小练习与答案

**练习 1**：如果对一张表连续调用两次 `table.add(data).execute().await`，`version()` 会从 1 变成几？

**答案**：变成 3。初始建表是版本 1，第一次 `add` 提交版本 2，第二次 `add` 提交版本 3。

**练习 2**：`table.list_versions()` 返回的 `Vec<Version>` 长度，和「最新版本号」之间是什么关系？

**答案**：二者相等（假设没有版本被清理）。每提交一个版本就在历史里多一条记录，版本号从 1 连续递增，所以条目数 = 最新版本号。

---

### 4.2 时间旅行：`checkout` 与 `checkout_tag`

#### 4.2.1 概念说明

「时间旅行」就是把一个 `Table` 句柄**临时钉到某个旧版本上**，之后对这个句柄的所有读操作都看到那份数据。它解决的问题是：**审计、复现、回滚前的对比**——比如「上周三导出的那批数据到底是什么样子」。

两个入口：

- **`checkout(version: u64)`**：按版本号钉。
- **`checkout_tag(tag: &str)`**：按「人类可读的别名」钉。版本号是一串递增整数，难记；**tag** 是给某个版本起的名字（如 `"v1.0-baseline"`），用 `table.tags()` 创建/管理。这样代码里可以写 `checkout_tag("v1.0-baseline")` 而不用记它是版本几。

时间旅行有三个关键性质，源码文档讲得很清楚：

1. **只读**：钉住旧版本后，任何写操作都会失败（你不能在一个历史快照上「改历史」，要改用 `restore`）。
2. **只影响当前句柄**：别的句柄不受影响。
3. **关闭读一致性**：一旦钉到具体版本，「读一致性」档位就被旁路了——因为你已经明确指定「我就要这版」，不需要再去刷新「最新」。

#### 4.2.2 核心流程

```
checkout(v) ──► 把句柄内部状态置为「钉在版本 v」(pinned_version = Some(v))
                       │
                       ▼
              之后每次读 (get) ──► 检测到 pinned_version，直接返回该快照，
                                  完全跳过「检查最新版本」的逻辑
                       │
                       ▼
              写操作 ──► ensure_mutable() 守卫发现已钉版本 ──► 返回 InvalidInput 错误
```

底层 Lance 用 `dataset.checkout_version(Ref)` 真正去读对应版本的 manifest 文件，重建那一份 `Dataset`。

#### 4.2.3 源码精读

对外 `Table::checkout()` 的文档把「只读 / 只影响本句柄 / 关闭读一致性」三点说得很直白：

[rust/lancedb/src/table.rs:1561-1577](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1561-L1577) —— 注意它把 `checkout` 形容成「a sort of 'view' or 'detached head'」，并提示「To return the table to a normal state use `checkout_latest`」。

`checkout_tag()` 同款语义，只是入参从版本号换成 tag 名：

[rust/lancedb/src/table.rs:1579-1595](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1579-L1595) —— 「Checks out a specific version of the Table by tag」。

本地实现委托给 `DatasetConsistencyWrapper::as_time_travel`：

[rust/lancedb/src/table.rs:2532-2538](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L2532-L2538) —— `checkout(version)` 调 `as_time_travel(version)`，`checkout_tag(tag)` 调 `as_time_travel(tag)`，二者最终走同一条「把 Ref 解析成具体快照」的路径（Ref 既能是版本号也能是 tag 名）。

`as_time_travel` 的核心是把「是否需要重新 checkout」判断清楚后，调用底层 Lance 的 `checkout_version`，并把 `pinned_version` 置为 `Some(...)`：

[rust/lancedb/src/table/dataset.rs:236-264](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/dataset.rs#L236-L264) —— 注意它先比较「目标版本」和「当前已钉版本」，相同就短路返回（省一次 manifest 读）；不同才真正 `dataset.checkout_version(target_ref).await?`，然后 `state.pinned_version = Some(version_value)`。

`Tags` trait 定义了 tag 的增删查改（CRUD），是 tag 机制的契约：

[rust/lancedb/src/table.rs:278-294](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L278-L294) —— `list` / `get_version` / `create` / `delete` / `update` 五个方法。`create(tag, version)` 给版本起名，`checkout_tag(tag)` 内部等价于「先 `get_version(tag)` 查出它指向哪个版本，再 `checkout` 那个版本」。

#### 4.2.4 代码实践

**实践目标**：多次写入后，`checkout` 一个旧版本，验证读到的是旧数据，且写入被拒绝。

**操作步骤**：

1. 阅读测试 [rust/lancedb/src/table.rs:3833-3852](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L3833-L3852)（`test_time_travel_write`），它的关键三步是：

   ```rust
   let version = table.version().await.unwrap();        // 记录 v1
   table.add(some_sample_data()).execute().await.unwrap(); // 现在 v2
   table.checkout(version).await.unwrap();              // 钉回 v1
   assert!(table.add(some_sample_data()).execute().await.is_err()) // 写入必失败
   ```

2. 若想亲手验证「读到旧数据」，可仿照它在 `checkout(version)` 之后加一行 `table.count_rows(None).await`，预期它返回 v1 时的行数（而非 v2 的）。
3. 用 tag 版本：参考 [rust/lancedb/src/table.rs:3374-3381](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L3374-L3381)，先 `tags().create(tag1, version)`，写新数据后 `checkout_tag(tag1)`，再 `checkout_latest()` 回到最新。

**需要观察的现象**：`checkout` 之后写入返回错误（`InvalidInput`：table cannot be modified when a specific version is checked out）；`count_rows` 显示旧版本的行数。

**预期结果**：时间旅行期间只读，写被守卫拦截。

> 待本地验证：行数具体值取决于你写入的样本数据。

#### 4.2.5 小练习与答案

**练习 1**：进程 A `checkout(3)` 后，进程 B（用另一个句柄打开同一张表）读到的数据是版本 3 吗？

**答案**：不是。`checkout` 只影响「调用它的那个句柄」的内部状态，进程 B 的句柄不受影响，仍按它自己的读一致性档位读。

**练习 2**：`checkout_tag("release-1")` 和 `checkout(version)` 相比，最大好处是什么？

**答案**：解耦。代码里写的是语义化名字 `"release-1"`，至于它指向版本几，由 tag 管理；后续可以把同一个 tag 指向更新的版本，调用方代码不用改。

---

### 4.3 回到最新与恢复：`checkout_latest` 与 `restore`

#### 4.3.1 概念说明

「钉在旧版本」是个临时状态，需要两条出路：

- **`checkout_latest()`**：解除钉定，让句柄重新指向最新版本（即「回到正常状态」）。它也是**手动刷新**的手段——当读一致性设成「不自动检查」时，调用它就能主动拉一次最新。
- **`restore()`**：把表**真正回滚**到当前钉住的版本。`checkout` 只是「看」历史，`restore` 是「把历史变成现在」——它会用被 checkout 的旧版本覆盖「最新版本」，之后该版本之后的所有改动都不再可见。

二者的区别是「读 vs 写」：

| 操作 | 性质 | 是否改表 | 之后状态 |
| --- | --- | --- | --- |
| `checkout_latest()` | 读 | 否 | 句柄回到「跟踪最新」 |
| `restore()` | 写 | 是（提交新版本） | 句柄回到「跟踪最新」+ 历史被「回退」 |

注意 `restore` 虽然是写，但**它必须在「已 checkout」状态下才能调**——先把句柄钉到目标版本，再 `restore`。源码里它被特别标注为「the only write operation allowed in time travel mode」。

#### 4.3.2 核心流程

```
checkout(v) ──► restore() ──► checkout_latest()（restore 内部其实已完成这步）
     │              │
     │              ▼
     │      底层 dataset.restore()：用旧版本覆盖最新
     │      （注意：这是写！提交新版本号，而非删除中间版本）
     ▼
 pinned ──► (restore 后) 解除 pinned，重新跟踪最新
```

一个反直觉但重要的点：`restore` **不删除**中间那些版本，而是**提交一个新版本**，其内容指向旧快照。所以版本号依然是递增的，旧的「错误版本」仍可通过版本号被 checkout 看到（除非被 `optimize` 的 prune 清理）。

#### 4.3.3 源码精读

对外两个方法，文档把语义讲清：

[rust/lancedb/src/table.rs:1597-1603](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1597-L1603) —— `checkout_latest`：「manually update a table when the read_consistency_interval is None」「undo a `checkout` operation」。

[rust/lancedb/src/table.rs:1605-1617](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1605-L1617) —— `restore`：「overwrites the latest version with a previous version」「will fail if checkout has not been called previously」。

本地实现 `NativeTable::checkout_latest()`：先 `bump_freshness()`（给远程新鲜度基线续命，见 4.5），再 `as_latest()` 解除钉定并 `reload()`：

[rust/lancedb/src/table.rs:2540-2545](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L2540-L2545)。

`as_latest()` 的实现——先读出最新版本号，checkout 到它，再把 `pinned_version` 置回 `None`：

[rust/lancedb/src/table/dataset.rs:212-234](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/dataset.rs#L212-L234) —— 关键是末尾 `state.pinned_version = None`，这表示「不再钉版本，恢复跟踪最新」。

`restore()` 实现——取当前钉住的版本号，调底层 Lance 的 `restore`，最后同样 `as_latest()` 收尾：

[rust/lancedb/src/table.rs:2617-2634](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L2617-L2634) —— 注意开头 `self.dataset.time_travel_version().ok_or_else(...)`：没 `checkout` 过就报「you must run checkout before running restore」；中间注释明确「restore is the only 'write' operation allowed in time travel mode」；末尾 `self.bump_freshness()` 同样为远程新鲜度续命。

`time_travel_version()` 就是读 `pinned_version`：

[rust/lancedb/src/table/dataset.rs:203-209](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/dataset.rs#L203-L209)。

#### 4.3.4 代码实践

**实践目标**：用一个 `restore` 把表回滚到某旧版本，并验证「中间版本仍在版本史里」。

**操作步骤**：

1. 阅读源码，确认 `restore` 会先取 `time_travel_version()`、要求你必须先 `checkout`。
2. 自己设计一段最小调用（示例代码，非项目原有）：

   ```rust
   // 示例代码：演示 checkout + restore 的典型时序
   let v1 = table.version().await?;
   table.add(data).execute().await?;          // 现在是 v2
   table.checkout(v1).await?;                  // 钉回 v1
   table.restore().await?;                     // 用 v1 覆盖"最新"，产生 v3
   let all = table.list_versions().await?;     // 预期仍能看到 v1/v2/v3
   ```

3. 跑 `cargo test --features remote -p lancedb --lib` 下的相关测试确认行为。

**需要观察的现象**：`restore` 后 `count_rows` 回到 v1 的行数；`list_versions` 仍包含 v2（说明 restore 不是删版本，而是追加一个指向旧数据的版本）。

**预期结果**：表内容回退，但版本号继续递增、历史不丢。

> 待本地验证：版本号具体数值依赖运行序列。

#### 4.3.5 小练习与答案

**练习 1**：不先调 `checkout`，直接调 `restore()`，会发生什么？

**答案**：返回错误，变体 `Error::InvalidInput`，消息「you must run checkout before running restore」。因为 `time_travel_version()` 此时为 `None`。

**练习 2**：`restore()` 之后，`table.version()` 返回的版本号，是「被恢复到的旧版本号」还是「一个更大的新版本号」？

**答案**：是一个更大的新版本号。restore 通过提交新版本来表达「回退」，它不会复用旧版本号，也不会删除中间版本。

---

### 4.4 读一致性：三档模式与 `read_consistency_interval`

#### 4.4.1 概念说明

这是 4.1–4.3 之外的另一条线：**当句柄「跟踪最新」（没有 checkout 旧版本）时，它读到的是不是真的最新？**

LanceDB 把答案设计成可调档位，对应 `database.rs` 里的 `ReadConsistency` 枚举三档：

| 档位 | `read_consistency_interval` | 含义 | 代价 |
| --- | --- | --- | --- |
| **Manual（手动）** | `None`（默认） | 永不自动检查，句柄一直用打开时缓存的那版；要最新需手动 `checkout_latest()` | 读延迟最低 |
| **Eventual（最终一致）** | `Some(d)`，`d > 0` | 在 TTL=`d` 内可能读到旧版，TTL 一到后台刷新；空闲久了下次读会同步刷新 | 折中 |
| **Strong（强一致）** | `Some(0)` | 每次读都先检查是否有新版本 | 读延迟最高 |

一个统一的保证（来自枚举文档）：**表永远内部一致——在同一个句柄上写入的数据，立刻能在同一个句柄上读到**。读一致性档位只影响「**跨句柄 / 跨进程**」的可见性。

#### 4.4.2 核心流程

档位由连接时（或打开表时）传入的 `read_consistency_interval: Option<Duration>` 决定，在 `DatasetConsistencyWrapper::new_latest` 里翻译成内部 `ConsistencyMode`：

```
read_consistency_interval
        │
        ├── None                 ──► ConsistencyMode::Lazy        (Manual)
        ├── Some(0)              ──► ConsistencyMode::Strong      (Strong)
        └── Some(d), d > 0       ──► ConsistencyMode::Eventual(d) (Eventual)
```

读路径（`get()`）根据模式分流：

- **Lazy**：直接返回缓存的 dataset，绝不主动检查。
- **Strong**：每次读都同步 `refresh_latest`，即问对象存储「现在最新版是几」，必要时 checkout。
- **Eventual**：用一个 `BackgroundCache` 做 TTL 缓存——TTL 没到直接返回缓存（快）；TTL 到了，先返回缓存同时**后台**刷新；空闲太久则下次读**同步**刷新。刷新窗口 `refresh_window = min(3s, TTL/4)`。

Eventual 模式的状态机（来自源码注释）：

\[ \begin{cases} t < \text{TTL} - \text{refresh\_window} & \Rightarrow \text{直接返回缓存} \\ \text{TTL} - \text{refresh\_window} \le t < \text{TTL} & \Rightarrow \text{后台刷新，同时返回缓存} \\ t \ge \text{TTL} & \Rightarrow \text{同步刷新后再返回} \end{cases} \]

其中 \( t \) 是距上次刷新已过去的时间。

#### 4.4.3 源码精读

`ReadConsistency` 枚举的三档语义定义：

[rust/lancedb/src/database.rs:189-201](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database.rs#L189-L201) —— 注释明确「Tables are always internally consistent」，这是贯穿本节的承诺。

连接时设定档位的 builder 方法及其权衡说明：

[rust/lancedb/src/connection.rs:860-883](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L860-L883) —— 文档给出三条建议：默认（不设）= 最快但可能陈旧；设为 0 = 强一致；设为非零 = 最终一致。并强调「This only affects read operations. Write operations are always consistent.」

`DatasetConsistencyWrapper` 把 `Option<Duration>` 翻译成 `ConsistencyMode`：

[rust/lancedb/src/table/dataset.rs:57-77](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/dataset.rs#L57-L77) —— 注意 `Some(d) if d == Duration::ZERO => Strong`、`Some(d) => Eventual(...)`（且 `refresh_window = min(3s, d/4)`）、`None => Lazy`，与上表一一对应。

`ConsistencyMode` 枚举本身，注释里画了上面那个状态机表：

[rust/lancedb/src/table/dataset.rs:37-53](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/dataset.rs#L37-L53)。

读路径 `get()` 的分流——先看是否钉版本（钉了就跳过所有一致性逻辑），否则按模式分流：

[rust/lancedb/src/table/dataset.rs:111-136](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/dataset.rs#L111-L136) —— 顶部 `if state.pinned_version.is_some()` 直接返回旧快照，印证 4.2 说的「checkout 关闭读一致性」。

后台刷新的实际动作 `refresh_latest`：克隆当前 dataset，调 Lance 的 `checkout_latest()`，再在版本不回退的前提下更新缓存指针：

[rust/lancedb/src/table/dataset.rs:297-314](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/dataset.rs#L297-L314) —— 注意 `new_arc.manifest().version >= state.dataset.manifest().version` 这一守卫，保证「版本只前进、不后退」。

#### 4.4.4 代码实践

**实践目标**：用同一张表、两个句柄，对比三档读一致性下「读到对方写入」的时机。

**操作步骤**：阅读测试 [rust/lancedb/src/table.rs:3781-3831](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L3781-L3831)（`test_read_consistency_interval`），它正是这个实验的现成实现。核心断言分三档：

1. **`None`（Manual）**：`table1` 写入后，`table2` 仍读到 0 行；直到 `table2.checkout_latest().await` 之后才读到 1 行。
2. **`Some(0)`（Strong）**：`table1` 写入后，`table2` **立刻**读到 1 行。
3. **`Some(100ms)`（Eventual）**：写入后 `table2` 先读到 0 行；`sleep(100ms)` 等 TTL 过去后，再读到 1 行。

**需要观察的现象**：三档行为差异——Manual 需手动刷新、Strong 立即可见、Eventual 延迟一个 TTL 后可见。

**预期结果**：测试通过，三档断言全部成立。

> 待本地验证：可自行把 `100ms` 调大（如 `500ms`）再观察，能更直观看到 Eventual 的「先旧后新」。

#### 4.4.5 小练习与答案

**练习 1**：默认配置（不设 `read_consistency_interval`）下，进程 B 写入后，进程 A 的句柄永远读不到新数据吗？

**答案**：不是「永远」，而是「不会自动读到」。调用 `table_a.checkout_latest().await` 即可手动拉一次最新。默认档是 Manual，不是「锁死」。

**练习 2**：为什么 Strong 模式读延迟最高？

**答案**：因为每次读之前都要先向对象存储确认「现在最新版是几」（一次额外的元数据往返），确认后才返回数据。Lazy 模式则完全跳过这步，直接用缓存。

---

### 4.5 远程读新鲜度：`database (read_freshness)` 模块

#### 4.5.1 概念说明

前面 4.4 讲的是**本地后端**：句柄直接读对象存储，自己决定何时刷新。但**远程命名空间后端**（通过 `lance-namespace` 的 REST 客户端访问 LanceDB Cloud / 托管服务）多了一层麻烦——**服务端会缓存表的元数据**。

后果是：你的句柄刚写完一版（或刚 `checkout_latest` 过），下一次读请求**仍可能拿到服务端缓存的旧快照**，因为服务端的缓存还没过期。这破坏了「我刚写的我自己应该立刻能读到」这条基本预期（虽然对象存储层面是最新的，但服务端给你的是旧的 manifest）。

解决办法是**读新鲜度信令（read-freshness signaling）**：在读请求上附一个 HTTP 头 `x-lancedb-min-timestamp`，告诉服务端「我至少要读到这个时间点之后的快照」。服务端看到这个头，就必须返回不早于该时间的快照，从而绕过它的陈旧缓存。

> 这个机制**只对远程命名空间后端有意义**——本地后端没有「可被读陈旧的中间服务端」，所以 4.4 的三档模式已足够；`read_freshness` 是为补上「远程多了一层缓存」这个缺口而存在的。

#### 4.5.2 核心流程

每张表维护一个**新鲜度基线（baseline）**时间戳（进程内、按表共享）：

```
写操作 / checkout_latest()
        │
        ▼
  bump()：把本表 baseline 推进到"现在"（只前进不后退）
        │
        ▼
后续读操作（describe_table / list_table_versions / query_table / list_tables）
        │
        ▼
  compute_min_timestamp = max(baseline, now - read_consistency_interval)
        │
        ▼
  在请求头注入 x-lancedb-min-timestamp = compute_min_timestamp
        │
        ▼
  服务端据此返回不早于该时间的快照（跳过陈旧缓存）
```

这里把「写时 bump 的基线」和「连接时的 `read_consistency_interval`」两条信息**取更紧（更新）的那一个**作为下限：

- 你刚写过 → baseline 很新 → 下限很新 → 保证读到自己的写。
- 你设了 Eventual 一致性 → `now - interval` 作为下限 → 允许一定陈旧。

二者取 `max`，体现「在不破坏你声明的最终一致性的前提下，绝不让你读不到自己的写」。

#### 4.5.3 源码精读

模块顶部注释把整个机制讲得很清楚：

[rust/lancedb/src/database/read_freshness.rs:4-15](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/read_freshness.rs#L4-L15) —— 「a per-table baseline is bumped to 'now' on every write and on `checkout_latest()`, and reads send `max(baseline, now - read_consistency_interval)`」。

`compute_min_timestamp` 实现取 max 的逻辑：

[rust/lancedb/src/database/read_freshness.rs:32-47](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/read_freshness.rs#L32-L47) —— 注意三个分支：`None`→不发头；`Some(0)`→下限是 `now`（等价强一致）；`Some(d)`→下限是 `now - d`；最后与 baseline 取 `a.max(b)`。

`bump` 让基线只前进不后退，防止并发句柄互相拉低地板：

[rust/lancedb/src/database/read_freshness.rs:49-56](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/read_freshness.rs#L49-L56) 与 [rust/lancedb/src/database/read_freshness.rs:71-77](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/read_freshness.rs#L71-L77)（`TableFreshness::bump`）。

哪些操作算「可被服务陈旧的读」、需要带这个头：

[rust/lancedb/src/database/read_freshness.rs:82-87](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/read_freshness.rs#L82-L87) —— 只有 `describe_table` / `list_table_versions` / `query_table` / `list_tables` 这四个读操作带头。注释点明 `list_table_versions` 是「managed-versioning 表解析 latest」的关键，正是它让 `checkout_latest()` 能观察到先前写入。

真正把头注入请求的是 `ReadFreshnessContextProvider`，它实现 `lance-namespace` 的 `DynamicContextProvider` trait，按操作的 `object_id` 查本表 baseline：

[rust/lancedb/src/database/read_freshness.rs:90-119](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/read_freshness.rs#L90-L119) —— 关键在「`headers.` 前缀的 context key 会变成 HTTP 头」，所以 `headers.x-lancedb-min-timestamp` 这个 key 最终成为请求头 `x-lancedb-min-timestamp`（见 [read_freshness.rs:23-25](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/read_freshness.rs#L23-L25)）。

本地 `NativeTable` 侧的触发点——写操作和 `checkout_latest` / `restore` 都会调 `bump_freshness()`：

[rust/lancedb/src/table.rs:2056-2061](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L2056-L2061) —— `bump_freshness` 对「非命名空间表是 no-op」（`freshness` 字段为 `None` 时什么都不做，见 [table.rs:1861](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1861) 的 `freshness: Option<TableFreshness>` 字段），只有用命名空间客户端打开的表才会真正 bump 基线。这把「本地无脑 bump」和「远程真正生效」统一到了同一个调用点。

#### 4.5.4 代码实践

**实践目标**：理解 `compute_min_timestamp` 在不同 baseline / interval 组合下的取值。

**操作步骤**（源码阅读 + 单元测试验证）：

1. 打开 [rust/lancedb/src/database/read_freshness.rs:140-179](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/read_freshness.rs#L140-L179) 的 `test_compute_min_timestamp_combines_baseline_and_interval`，它枚举了六种组合。
2. 重点对照这几条断言：
   - 无 baseline、无 interval → `None`（不发头）。
   - 只有 `Some(0)` interval → 下限 = `now`。
   - baseline = `now-60`、interval = 10s → 下限 = `now-10`（取更新的那个）。
   - baseline = `now-5`、interval = 60s → 下限 = `now-5`（baseline 更新，取它）。
3. 运行 `cargo test --features remote -p lancedb --lib read_freshness`。

**需要观察的现象**：所有断言成立，确认「取 max」逻辑正确。

**预期结果**：`max(baseline, now - interval)` 在六种边界下都如预期。

> 待本地验证：`read_freshness` 模块是独立单测，不依赖真实远程服务，可直接跑通。

#### 4.5.5 小练习与答案

**练习 1**：为什么写操作（`create_table` / `drop_table` 等）**不**发送 `x-lancedb-min-timestamp` 头？

**答案**：写操作是「建立」而非「消费」基线——它们负责 bump 基线，本身就是要落到最新版本，不存在「读到陈旧快照」的问题，所以不需要这个头（见 [read_freshness.rs:276-294](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/read_freshness.rs#L276-L294) 的 `test_provider_non_read_ops_emit_nothing`）。

**练习 2**：两个并发句柄对同一张表写，`next_freshness_baseline` 为什么不允许基线「后退」？

**答案**：基线是「地板」（读请求至少要满足的新鲜度下限）。如果句柄 A 刚把地板抬到 `now`，句柄 B 的写入又把地板降到更早的时间，就会让 B 之后某个读请求拿到低于 A 已设地板的下限，可能读到陈旧数据。取 `max` 保证地板只会越来越高，不会因并发而松动。

## 5. 综合实践

**任务**：用一张表，把本讲的「版本 → 时间旅行 → 恢复 → 读一致性」串起来，复刻一次「写错了、回滚、再验证」的完整故事。

**背景**：你有一张向量表，第一版数据是「正确基线」。随后一次 `add` 误写入了一批脏数据（变成 v2）。你要（1）确认脏数据存在；（2）时间旅行回到干净的 v1 看一眼；（3）用 `restore` 把表回滚到 v1；（4）另开一个句柄，对比 Manual / Strong 两档读一致性下它能否看到回滚结果。

**建议步骤**：

1. 建表写入「正确基线」，记录 `v1 = table.version().await?`。
2. `add` 脏数据，记录 `v2 = table.version().await?`，确认 `count_rows` 变大。
3. `table.checkout(v1).await?`，验证 `count_rows` 回到基线值（时间旅行看到旧数据）。
4. `table.restore().await?`，确认 `version()` 变成一个比 v2 更大的新版本号（回滚是追加版本）。
5. 用 `ConnectBuilder` 新开一个 `read_consistency_interval(Duration::ZERO)`（Strong）的句柄打开同一张表，确认它能立刻读到回滚后的行数；再换默认（Manual）句柄，确认它读不到、直到 `checkout_latest()`。

**验证清单**：

- [ ] v2 > v1，且 restore 后的版本号 > v2。
- [ ] checkout(v1) 期间写入被拒绝（报 `InvalidInput`）。
- [ ] restore 后 `list_versions` 仍含 v2（历史未删）。
- [ ] Strong 句柄立即看到回滚结果，Manual 句柄需 `checkout_latest`。

> 提示：本地表用 `cargo test` 现有测试（`test_time_travel_write`、`test_read_consistency_interval`、`test_branches`）可逐段对照；远程命名空间表的 `read_freshness` 行为靠 `read_freshness.rs` 的单测覆盖，端到端需真实远程服务，标注「待本地验证」。

## 6. 本讲小结

- **版本是只读快照的编号**：每次写都 +1，纯读不变；`version()` 返回「当前句柄看的那版」，`list_versions()` 是完整版本史。
- **时间旅行 = 把句柄钉到旧版本**：`checkout(v)` / `checkout_tag(tag)` 让句柄只读历史快照，写被 `ensure_mutable` 守卫拦截；只影响当前句柄、并旁路读一致性。
- **`checkout_latest` 是读、`restore` 是写**：前者解除钉定回到最新，后者用旧版本覆盖「最新」（追加新版本、不删历史），且 restore 必须先 checkout。
- **本地三档读一致性**：`read_consistency_interval` 为 `None`/`Some(0)`/`Some(d>0)` 分别对应 Manual / Strong / Eventual，由 `DatasetConsistencyWrapper` 翻译成 `ConsistencyMode`，在 `get()` 里分流；表永远内部一致，档位只影响跨句柄可见性。
- **远程读新鲜度补上「服务端缓存」缺口**：`read_freshness` 在写时 bump 基线、在读时注入 `x-lancedb-min-timestamp = max(baseline, now - interval)` 头，确保你读得到自己的写；仅远程命名空间后端生效，本地是 no-op。
- **核心只搬运、真相在 Lance**：`Version` 类型、`checkout_version`、`restore`、`checkout_latest` 等真正实现在底层 Lance；LanceDB 负责包装成统一 API（本地函数调用 / 远程 HTTP 两套后端一致）。

## 7. 下一步学习建议

本讲把「表的读侧」（版本、时间旅行、一致性）讲完了，建议接着看：

- **u5-l4 数据检视与多模态 blob**：从版本史的另一面——实际的数据文件与统计——去检视一张表，配合本讲的 `list_versions` 能更立体地理解「版本背后到底存了什么」。
- **u6-l2 Database trait 与命名空间模型**：本讲的 `read_freshness` 依赖「命名空间客户端」，那里会讲清 listing 模式与 namespace 模式的区别，以及为什么只有 namespace 模式才有服务端缓存问题。
- **u6-l3 远程后端：HTTP 客户端与重试**：想深入 `x-lancedb-min-timestamp` 头如何真正随 HTTP 请求发出、以及远程 `RemoteTable` 如何把 `checkout`/`version` 翻译成 REST 调用，看这一讲。
- **源码延伸阅读**：`rust/lancedb/src/table/dataset.rs` 的 `BackgroundCache`（Eventual 模式的 TTL 缓存实现）、`rust/lancedb/src/remote/table.rs`（远程表如何复刻这套版本语义）。
