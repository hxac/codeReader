# 缓存树 cache-tree 与 split-index

## 1. 本讲目标

上一讲（u4-l1）我们建立了索引的「三层数据模型」——工作树／索引／对象数据库，并把 `struct index_state` 与 `cache_entry` 的字段、`.git/index` 的磁盘格式讲透了。本讲继续在索引这一层挖下去，回答三个性能问题：

1. **为什么 `git commit` 不会每次都把整个目录树重新哈希一遍？** —— 因为有 **cache-tree**，它把「目录 → tree 对象哈希」的映射缓存进索引。
2. **几十万文件的大仓库，为什么每次写索引不必重写几百 MB？** —— 因为有 **split-index**，把索引拆成「共享基 + 增量」两层。
3. **索引里按路径查找为什么够快？** —— 除了二分，还有 **name-hash** 哈希表，尤其支撑大小写不敏感查找。

学完本讲，你应该能：

- 说清 cache-tree 的「有效／无效」语义、惰性失效与命中短路机制；
- 说清 split-index 的「共享基 + link 扩展」两层结构与写入判定；
- 说清 name-hash 的懒加载、多线程构建与查找流程；
- 会用 `git write-tree`、`GIT_TRACE2_PERF` 等手段观察这些机制的实际行为。

## 2. 前置知识

在进入源码前，先澄清三个易混点：

- **tree 对象是递归的**：一个 tree 对象记录「某目录直属子项」的清单（子文件 blob + 子目录 tree），子目录再由它自己的 tree 对象记录。所以把一张扁平的索引「折叠」成一棵 tree 对象树，要自底向上逐目录序列化、哈希、写入。这是 `git write-tree` 的本质工作。
- **索引是扁平数组**：`istate->cache[]` 是按路径排序的一维 `cache_entry *` 数组，并不天然有目录层级。cache-tree 是「额外」挂在 `index_state` 上的目录树形缓存，不是索引本身的结构。
- **「扩展区」是索引文件的附加尾巴**：`.git/index` = 文件头 + 条目区 + 扩展区 + 校验尾。cache-tree、split-index、resolve-undo、untracked、fsmonitor 都以「4 字母签名 + 长度 + 内容」的 TLV 形式塞在扩展区。本讲的 cache-tree 对应 `TREE` 扩展，split-index 对应 `link` 扩展。

三个机制都是「用空间换时间」的缓存，且都遵循同一套路：**读时按需加载、写时按需持久化、改动时按需失效**。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [cache-tree.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.h) | cache-tree 的结构体与公共 API 声明 |
| [cache-tree.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c) | cache-tree 的失效、更新、读写、校验、预填（prime）全部实现 |
| [split-index.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/split-index.h) | `struct split_index` 与公共 API |
| [split-index.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/split-index.c) | 共享基/增量索引的读、合并、写、位图计算 |
| [name-hash.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/name-hash.c) | 路径/目录哈希表的懒加载、多线程构建与查找 |
| [read-cache.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c) | 索引读写主流程，把上述三者接入扩展区与 `write_locked_index` 调度 |
| [read-cache-ll.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache-ll.h) | `struct index_state` 字段与 `cache_changed` 位标志定义 |
| [builtin/write-tree.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/write-tree.c) | `git write-tree` 子命令，是观察 cache-tree 命中的最佳入口 |

## 4. 核心概念与源码讲解

### 4.1 cache-tree 目录哈希缓存

#### 4.1.1 概念说明

`git write-tree` 要把扁平索引折叠成一棵 tree 对象树。最朴素的办法是每次都自底向上重新序列化每个目录、重新哈希、重新写对象。但绝大多数 `git commit` 之间，目录内容并没变——重新算一遍纯属浪费。

**cache-tree** 就是为消除这种浪费而生的缓存。它在 `index_state` 上挂了一棵与目录结构同形的树，每个节点对应一个目录，缓存两样东西：

- `entry_count`：本节点覆盖的索引条目数；
- `oid`：这些条目「折叠」后对应的 tree 对象哈希。

核心不变式（**本讲最重要的一句话**）：

> `entry_count >= 0` 表示「有效」——缓存的 `oid` 对当前索引内容仍然正确；`entry_count < 0`（实际取 `-1`）表示「无效」——必须重建本节点（及其被波及的祖先）。

任何改动索引的操作（增/删/改一个文件）都会调用 `cache_tree_invalidate_path`，把受影响目录及其所有祖先的 `entry_count` 置 `-1`。这是一种**惰性失效**：失效极廉价（只是沿父链写几个 `-1`），重建被推迟到真正需要 tree 时（`write-tree` / `commit`）才做。

cache-tree 序列化后就是 `.git/index` 里的 `TREE` 扩展。

#### 4.1.2 核心流程

cache-tree 的生命周期可画成四阶段闭环：

```
        读索引                         改动索引
   ┌───────────────┐              ┌──────────────────┐
   │ cache_tree_read│              │cache_tree_       │
   │ 解析 TREE 扩展 │              │invalidate_path   │
   │ → 内存树       │              │ 沿父链置 -1      │
   └───────┬───────┘              └────────┬─────────┘
           │                                │
           ▼                                ▼
   ┌───────────────┐              ┌──────────────────┐
   │ write-tree 时  │  命中短路    │ cache_tree_update │
   │cache_tree_     │◀────────────│ update_one 递归   │
   │fully_valid?    │   否则重建   │ 有效则跳过/无效重建│
   └───────┬───────┘              └────────┬─────────┘
           │                                │
           └──────────────┬─────────────────┘
                          ▼
                 ┌──────────────────┐
                 │ cache_tree_write │
                 │ 序列化回 TREE 扩展│
                 └──────────────────┘
```

- **读**：`read_index_extension` 遇到 `CACHE_EXT_TREE`，调 `cache_tree_read` 把字节流还原成内存树。
- **失效**：索引一改动，`cache_tree_invalidate_path` 把对应路径的节点及祖先 `entry_count = -1`，并置 `CACHE_TREE_CHANGED` 位。
- **更新（贵，按需）**：`cache_tree_update → update_one` 递归遍历索引区域，对每个目录：
  - 若 `entry_count >= 0` 且 tree 对象确实存在于对象库 → **命中，直接返回**，跳过重建；
  - 否则 → 重建：拼 `<mode> <name>\0<oid>` 字节流，哈希并写成 tree 对象，回填 `entry_count`。
- **写**：`cache_tree_write → write_one` 把内存树序列化回 `TREE` 扩展。
- **预填（prime）**：`checkout`/`read-tree`/`reset` 后，与其等下次 `write-tree` 重建，不如直接从刚检出的 tree 对象把已知正确的 oid 灌进 cache-tree——这就是 `prime_cache_tree`，又快又准。

#### 4.1.3 源码精读

先看结构体。cache-tree 节点本身和它的子节点句柄分开定义：

[cache-tree.h:8-22](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.h#L8-L22) 定义了 `struct cache_tree_sub`（子节点句柄，持名字和指向子 `cache_tree` 的指针）与 `struct cache_tree`（节点本体：`entry_count`、`oid`、`down[]` 子节点数组）。注意头注释一针见血：`entry_count` 负数即「无效」。

构造函数把新节点直接置为无效：

[cache-tree.c:25-30](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c#L25-L30) 中 `cache_tree()` 把 `entry_count = -1`。这是「默认不信」——新建的节点没有可信 oid，必须经一次 `update_one` 才能转为有效。

**失效**逻辑是递归沿路径下钻、沿父链上溯置 `-1`：

[cache-tree.c:113-157](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c#L113-L157) 的 `do_invalidate_path`：先把自己的 `entry_count = -1`；若路径还有下一层（含 `/`），则找到对应子树递归失效；若已是末层且子树存在，直接摘除并释放。公开入口 [cache-tree.c:159-163](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c#L159-L163) 在失效成功后置 `CACHE_TREE_CHANGED`，让索引知道「下次写盘要带上新的 TREE 扩展」。索引侧的调用点很多，例如删除条目时：

[read-cache.c:610-613](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L610-L613) 在 `remove_marked_cache_entries` 里对每个被删条目调 `cache_tree_invalidate_path`。增删改路径都会走这里——这就是「惰性失效」的源头。

**整体有效性检查**是命中判定的前置：

[cache-tree.c:278-292](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c#L278-L292) 的 `cache_tree_fully_valid`：`entry_count < 0` 固然无效；但即使 `>= 0`，还要用 `odb_has_object` 确认 tree 对象**真的在对象库里**（防止缓存指向一个已被清理的对象），并**递归**检查所有子树都有效。整棵树全有效才算「fully valid」。

**更新主循环** `update_one` 是本模块的心脏，分三段看。

第一段——命中短路（本讲最关键的两行）：

[cache-tree.c:336-339](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c#L336-L339)：若本节点 `entry_count >= 0` 且 tree 对象存在于对象库，**直接 `return it->entry_count`**，跳过本目录的全部重建。连续两次 `git write-tree` 第二次飞快，就因为这里短路了。

第二段——递归处理子树：

[cache-tree.c:346-394](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c#L346-L394) 先把现有子树全标 `used=0`，再扫描索引区域，遇到含 `/` 的路径就切出子目录名、`find_subtree` 创建/定位子节点、递归 `update_one`，并标 `used=1`；扫完用 `discard_unused_subtrees` 丢弃没被用到的子树（索引里已不存在的目录）。

第三段——重建本层 tree 对象：

[cache-tree.c:399-508](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c#L399-L508) 遍历本层直属条目，按 tree 对象格式拼 `strbuf`：子目录写其子树 `oid` + `S_IFDIR`，文件写 `ce->oid` + `ce->ce_mode`；其间处理 `CE_REMOVE`（跳过）、`CE_INTENT_TO_ADD`（不进 tree、置无效）等特殊情况；最后 [cache-tree.c:490-505](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c#L490-L505) 依 `dryrun`/`repair`/正常三种模式，或只算哈希、或写对象；最终 [cache-tree.c:508](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c#L508) 设 `entry_count = to_invalidate ? -1 : i - *skip_count`——只要本层出现过需要失效的情况（如 i-t-a），就把自己也标无效。

公开入口 `cache_tree_update` 包了一层事务与 trace：

[cache-tree.c:517-545](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c#L517-L545) 先 `verify_cache`（检查无未合并条目、无 D/F 冲突），再用 `odb_transaction` 批量写 tree 对象以减少 I/O，最后置 `CACHE_TREE_CHANGED`。

**序列化与反序列化**是一对自描述的文本+二进制格式：

[cache-tree.c:547-585](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c#L547-L585) 的 `write_one` 注释写明了每条记录的格式：`路径(NUL) + entry_count + 空格 + subtree_nr + 换行 + [oid（仅当有效）] + 各子树记录`。读取端 [cache-tree.c:625-701](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c#L625-L701) 的 `read_one` 是其逆过程，逐字段解析、`parse_int` 读两个整数、有效则读 `rawsz` 字节作 oid，再递归读子树。

**`git write-tree` 如何用上缓存**：

[builtin/write-tree.c:54-56](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/write-tree.c#L54-L56) 调 `write_index_as_tree`，其内部 [cache-tree.c:794-828](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c#L794-L828) 先算 `was_valid = cache_tree_fully_valid(...)`：若整棵树已有效，**完全跳过 `cache_tree_update`**，直接返回根 oid；并且若此前无效、本次刚建好，会 `write_locked_index` 把新的 cache-tree 持久化回 `.git/index`——这正是「第一次慢、第二次快」的根因。

**预填（prime）**——checkout 后的最优路径：

[cache-tree.c:838-892](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c#L838-L892) 的 `prime_cache_tree_rec` 直接遍历刚检出的 tree 对象，把每个目录的 oid 灌进对应 cache-tree 节点。因为这些 tree 对象是 checkout 刚写下的、必然存在，所以灌进去的缓存天然 `fully_valid`，省去了 `update_one` 的重建。调用点见 `reset.c`、`builtin/read-tree.c`、`sequencer.c` 等。

最后看扩展区的接入：

[read-cache.c:1737-1739](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L1737-L1739) 读到 `CACHE_EXT_TREE` 时调 `cache_tree_read`；[read-cache.c:2998-3009](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L2998-L3009) 写盘时若 `WRITE_CACHE_TREE_EXTENSION` 且未 `drop_cache_tree`，调 `cache_tree_write`。`cache_changed` 位标志定义在 [read-cache-ll.h:128-136](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache-ll.h#L128-L136)，其中 `CACHE_TREE_CHANGED = (1<<5)`。

#### 4.1.4 代码实践

**实践目标**：亲手验证「cache-tree 命中短路」，并对照源码说清命中发生在哪一行。

**操作步骤**：

1. 在 git 源码仓库（本仓库即可，文件多、目录深，效果明显）里准备一个干净的工作树。
2. 先确保索引里有内容：`git read-tree HEAD`，再 `git checkout-index -a -f`（或直接 `git status` 触发索引读取）。
3. 第一次写树并计时：
   ```
   /usr/bin/time -v git write-tree
   ```
4. 紧接着第二次写树并计时：
   ```
   /usr/bin/time -v git write-tree
   ```
5. 用隐藏选项强制忽略 cache-tree 再计时（对照重建开销）：
   ```
   /usr/bin/time -v git write-tree --ignore-cache-tree
   ```

**需要观察的现象**：

- 第 3 步会输出一个 tree oid，并耗时较高（要重建所有 tree 对象并写对象库）。
- 第 4 步输出**同一个** oid，但耗时应明显更低——因为第一次结束时 `write_index_as_tree` 已把有效 cache-tree 写回 `.git/index`，第二次 `cache_tree_fully_valid` 返回真，整体跳过 `cache_tree_update`。
- 第 5 步 `--ignore-cache-tree` 会 `cache_tree_free` 后强制重建（见 [builtin/write-tree.c:36-43](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/write-tree.c#L36-L43) 与 [cache-tree.c:748-754](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c#L748-L754)），耗时应与第 3 步接近。

**预期结果**：第 4 步最快、第 3 与第 5 步接近。命中发生在 [cache-tree.c:336-339](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c#L336-L339) 的短路返回，以及更外层 [cache-tree.c:809-814](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c#L809-L814) 的 `was_valid` 判定。

> 精确耗时数字「待本地验证」（取决于仓库大小与磁盘）。若想看更细的区域耗时，可改用 `GIT_TRACE2_PERF=1 git write-tree`，在输出里搜 `cache_tree` 相关 region 事件。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cache_tree()` 构造函数要把 `entry_count` 初始化为 `-1`，而不是 `0`？

**参考答案**：`-1` 表示「无效、oid 不可信」。若初始化为 `0`，会被误解为「有效且覆盖 0 个条目」（即空树），可能让命中短路返回一个并不正确的空 tree oid。`-1` 强制节点必须经过一次 `update_one` 重建才能转为有效，杜绝使用未初始化的 oid。

**练习 2**：修改深层文件 `a/b/c.txt` 后，cache-tree 中哪些节点会被置为无效？

**参考答案**：`do_invalidate_path` 沿路径递归下钻到 `a/b/`，把 `a/b/` 节点的 `entry_count = -1`；但失效并非只影响叶子——调用链上每一层都会把自己置 `-1`，所以 `a/` 与根节点也会被置为无效。因为父目录的 tree oid 依赖子目录的 tree oid，子目录变了，父目录缓存必然作废。

**练习 3**：`cache_tree_fully_valid` 除了检查 `entry_count >= 0`，为什么还要调 `odb_has_object` 并递归检查子树？

**参考答案**：缓存的 oid 理论上可能指向一个已被 `gc` 清理或尚未拉取的对象（promisor 场景）；不检查就返回「有效」会让后续直接拿来用而崩。递归检查子树则是因为「整棵树有效」要求每个子目录的缓存都有效——任何一个子节点无效，根节点的 oid 即便存在也不代表当前索引内容。

---

### 4.2 split-index 基础/共享索引

#### 4.2.1 概念说明

cache-tree 解决的是「别重复算 tree」，split-index 解决的是另一个维度的问题：**别每次都重写整个索引文件**。

设想一个 50 万文件的单体仓库，`.git/index` 可能上百 MB。每一次 `git status`/`git add`/`git commit` 都会把整个索引重写一遍并 fsync——而两次写之间，99% 的条目根本没变。这在大型仓库上是实打实的性能瓶颈。

split-index 的解法是**把索引拆成两层**：

- **共享索引（shared index）**：只读的「基」，文件名 `.git/sharedindex.<oid>`，存放绝大多数不变条目。很少重写。
- **拆分索引（split index）**：就是 `.git/index` 本身，但变小了——只存「相对基的增量」：哪些基条目要删、哪些要替换、以及全新增加的条目。

增量以两个 **EWAH 压缩位图**表达，记录在 `.git/index` 的 `link` 扩展里：

- `delete_bitmap`：基中要删除的条目位置；
- `replace_bitmap`：基中要替换（内容变了）的条目位置，替换后的新内容跟在 split 索引的条目区里。

读索引时，先把共享基读进内存，再叠加上述增量，还原出完整的内存索引。写索引时，只重写小小的 split 索引；共享基只「续命」（刷新 mtime）不重写。多个工作树、多次操作可共用同一个基。

#### 4.2.2 核心流程

split-index 的读写可画成「基 + 增量」的合成与分解：

```
        读索引（合成）                          写索引（分解）
   ┌─────────────────────┐               ┌──────────────────────┐
   │ read_index_from     │               │ write_locked_index   │
   │  1. do_read_index   │               │  判定：非共享条目占比 │
   │     (.git/index)    │               │  > 20% ?             │
   │  2. 读 link 扩展    │               │   是→写新共享基       │
   │     得 base_oid     │               │   否→只写 split 增量 │
   │  3. do_read_index   │               └──────────┬───────────┘
   │     (sharedindex.X) │                          │
   │  4. merge_base_index│            ┌─────────────┴─────────────┐
   │     叠加 delete/    │            ▼                           ▼
   │     replace 增量    │   ┌─────────────────┐       ┌─────────────────┐
   └─────────────────────┘   │write_shared_index│       │write_split_index │
                             │move_cache_to_base│       │prepare_to_write  │
                             │ 写 sharedindex.X │       │ 计算 del/rep 位图 │
                             └─────────────────┘       └─────────────────┘
```

- **读**：`read_index_from` 读 `.git/index`；若 `link` 扩展存在，按 `base_oid` 找到 `.git/sharedindex.<base_oid>` 读出基，再 `merge_base_index` 应用 delete/replace 增量，得到完整内存索引。
- **条目归属标记**：每个 `cache_entry` 有个 `index` 字段，**1-based**，记录它来自基的第几个槽位；`0` 表示「新条目，不在基里」。这是写时计算增量的关键依据。`mark_base_index_entries` 在基读入后给每个基条目打上 `index = i+1`。
- **写判定**：`write_locked_index` 先决定走哪条路：
  - 若 `cache_changed` 超出 `EXTMASK`（非扩展类改动）、或没开 split、或 `alternate_index_output` → 走普通整索引写。
  - 否则计算「非共享条目占比」，超过阈值（默认 20%，`splitIndex.maxPercentChange`）就置 `SPLIT_INDEX_ORDERED`，意味着「该换新基了」。
- **写新基**：`write_shared_index` 调 `move_cache_to_base_index` 把当前内存索引整体提升为新基，写成 `.git/sharedindex.<新oid>`，并清理过期旧基。
- **写增量**：`write_split_index` 调 `prepare_to_write_split_index` 比对当前索引与基，生成 `delete_bitmap`（基里有、当前没的）和 `replace_bitmap`（内容变了的），把非共享 + 替换条目写进 `.git/index`，再附上 `link` 扩展。

#### 4.2.3 源码精读

结构体很紧凑：

[split-index.h:10-20](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/split-index.h#L10-L20) 定义 `struct split_index`：`base_oid`（指向共享基）、`base`（基的 `index_state` 指针）、`delete_bitmap`/`replace_bitmap`（两个 EWAH 位图）、`saved_cache`/`saved_cache_nr`（写时暂存原索引数组）、`refcount`（多工作树共享时引用计数）。

初始化与约束：

[split-index.c:13-23](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/split-index.c#L13-L23) 的 `init_split_index` 懒分配 `split_index` 并设 `refcount=1`。注意第 16-17 行：**sparse index 与 split index 互斥**，混用直接 `die`。

**读 link 扩展**：

[split-index.c:25-54](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/split-index.c#L25-L54) 的 `read_link_extension`：先读 `rawsz` 字节作 `base_oid`；若还有数据，依次读 `delete_bitmap` 与 `replace_bitmap` 两个 EWAH 位图。写端 [split-index.c:56-66](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/split-index.c#L56-L66) 是其逆过程。

**条目归属标记**——理解 split-index 的钥匙：

[split-index.c:68-80](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/split-index.c#L68-L80) 的 `mark_base_index_entries` 注释明说：基条目位置从 **1** 起算，**0 保留为「这是新条目」**。这个 `index` 字段贯穿读写两端：读时据此区分基条目与新增条目；写时据此判断「这个条目相对基是删除、替换还是新增」。

**读时合成**：

[split-index.c:164-208](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/split-index.c#L164-L208) 的 `merge_base_index`：先把基的 `cache[]` 拷成当前索引骨架，再用 `ewah_each_bit(replace_bitmap, replace_entry, ...)` 把替换条目覆写到对应槽位，用 `ewah_each_bit(delete_bitmap, mark_entry_for_delete, ...)` 标记删除并 `remove_marked_cache_entries`，最后把 `saved_cache` 里剩余的非基新条目 `add_index_entry` 追加进去。`replace_entry`（[split-index.c:136-162](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/split-index.c#L136-L162)）会校验「被替换槽位不能再同时被删除」等一致性约束。

**写时分解**是 split-index 最复杂的函数：

[split-index.c:235-394](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/split-index.c#L235-L394) 的 `prepare_to_write_split_index`。它遍历当前 `istate->cache[]`，对每个条目：

- `ce->index == 0` → 新条目（不在基里），直接进 split 条目区（[split-index.c:255-275](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/split-index.c#L255-L275) 注释详述了「也可能是 unpack_trees 重构出的同名不同内容条目」的边界情况）。
- `ce->index > 0` 且 `ce == base->cache[index-1]` → 与基完全一致，标 `CE_MATCHED`，**不进 split**（[split-index.c:282-314](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/split-index.c#L282-L314)），除非 racy 需要重写 stat。
- 内容变化（`compare_ce_content` 不等）→ 标 `CE_UPDATE_IN_BASE`，进 `replace_bitmap`。

随后扫基数组：没被 `CE_MATCHED` 也没被 `CE_UPDATE_IN_BASE` 的基条目进 `delete_bitmap`；`CE_UPDATE_IN_BASE` 的进 `replace_bitmap` 并标 `CE_STRIP_NAME`（替换条目在 split 索引里不存名字，靠基的位置定位，省空间）。最后把要写进 split 索引的条目（新增 + 替换）临时换到 `istate->cache` 供 `do_write_index` 落盘，写完由 `finish_writing_split_index`（[split-index.c:396-407](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/split-index.c#L396-L407)）换回原数组。

**提升为新基**：

[split-index.c:82-124](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/split-index.c#L82-L124) 的 `move_cache_to_base_index` 把当前 `cache[]` 整体搬到 `si->base->cache[]`，并 `mark_base_index_entries` 打 1-based 序号、清掉 `CE_UPDATE_IN_BASE`。注意内存池（`ce_mem_pool`）随条目一起搬家，避免悬空指针。

**引用计数与共享**：

[split-index.c:409-423](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/split-index.c#L409-L423) 的 `discard_split_index` 用 `refcount` 决定是否真正释放基——多个 `index_state` 共享同一基时，最后一个释放者才回收。

**read-cache.c 侧的接入**：

读时 [read-cache.c:2369-2413](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L2369-L2413)：读出 `.git/index` 后若有 `base_oid`，定位 `.git/sharedindex.<base_oid>` 读基，校验基的 oid 与 `base_oid` 一致（[read-cache.c:2403-2406](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L2403-L2406)），再 `merge_base_index`。扩展区分派见 [read-cache.c:1743-1746](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L1743-L1746)（`CACHE_EXT_LINK`）。

写时核心调度在 [read-cache.c:3310-3389](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L3310-L3389) 的 `write_locked_index`：

- [read-cache.c:3331-3337](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L3331-L3337) 判定走普通写还是 split 写（`cache_changed & ~EXTMASK` 表示有非扩展类改动，必须整写）。
- [read-cache.c:3349-3352](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L3349-L3352) 用 `too_many_not_shared_entries` 决定是否换新基。
- 新基写入 [read-cache.c:3242-3278](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L3242-L3278) 的 `write_shared_index`，落盘成 `sharedindex.<基oid>` 并 `clean_shared_index_files` 清理过期基。
- 最后 `write_split_index` 写增量。`EXTMASK`（[read-cache.c:79-81](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L79-L81)）圈定了「可只写扩展、不整写索引」的改动类型集合，split-index 正是其中之一。

**换基阈值**：

[read-cache.c:3282-3308](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L3282-L3308) 的 `too_many_not_shared_entries`：默认 `default_max_percent_split_change = 20`，即非共享条目占比超 20% 就换新基；`0%` 表示每次都换新基，`100%` 表示永不换。

#### 4.2.4 代码实践

**实践目标**：观察 split-index 的「共享基 + 增量」磁盘布局，理解增量如何叠加。

**操作步骤**：

1. 在一个有较多文件的仓库（git 源码仓库即可）启用 split-index：
   ```
   git config splitIndex true
   ```
2. 触发一次索引写以生成共享基（任一改动即可）：
   ```
   echo x >> README.md && git add README.md && git commit -m "touch"
   ```
   或更直接：`git update-index --split-index`。
3. 查看共享基文件：
   ```
   ls -la .git/sharedindex.*
   ```
4. 比较基与主索引的大小：
   ```
   stat -c '%n %s bytes' .git/index .git/sharedindex.*
   ```
5. 再做一个小改动并 `git add`，然后看 `.git/index` 大小是否仍很小（只含增量）：
   ```
   echo y >> COPYING && git add COPYING
   stat -c '%n %s bytes' .git/index
   ```

**需要观察的现象**：

- 第 3 步能看到 `.git/sharedindex.<40 或 64 位 hex>` 文件，名为基索引的 oid。
- 第 4 步：`.git/sharedindex.*` 较大（承载绝大多数条目），`.git/index` 较小（只含增量与 link 扩展）。
- 第 5 步：改动一个文件后，`.git/index` 仍是小文件——新增/替换条目进 split，基不动。

**预期结果**：split-index 把「大且少变」的部分隔离到共享基，主索引保持小而高频的增量。对照 [split-index.c:235-394](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/split-index.c#L235-L394) 理解：`ce->index == 0` 的新条目和 `CE_UPDATE_IN_BASE` 的替换条目进 split，其余基条目不重写。

> splitIndex 在不同 git 版本上的默认行为与推荐度有变化，本实践的精确文件大小「待本地验证」。若想强制测试该路径，可用环境变量 `GIT_TEST_SPLIT_INDEX=1` 跑任意命令（见 [read-cache.c:3329-3348](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L3329-L3348)）。

#### 4.2.5 小练习与答案

**练习 1**：`cache_entry` 的 `index` 字段为 `0` 表示什么？为什么从 1 起算？

**参考答案**：`0` 表示「该条目不在共享基中，是 split 索引新增的」。从 1 起算是为了把 0 留作「新条目」哨兵值（见 [split-index.c:71-79](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/split-index.c#L71-L79) 注释）。写时 `prepare_to_write_split_index` 正是用 `ce->index == 0` 判定新条目、`ce->index > 0` 判定基条目。

**练习 2**：什么条件下会触发写一个**新的**共享基？

**参考答案**：当 `cache_changed` 含 `SPLIT_INDEX_ORDERED` 时。触发它的主因是 `too_many_not_shared_entries` 返回真——非共享条目占比超过 `splitIndex.maxPercentChange`（默认 20%）。配置为 `0%` 时每次都换新基，`100%` 时永不换（见 [read-cache.c:3287-3298](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L3287-L3298)）。

**练习 3**：为什么 split-index 与 sparse-index 互斥？

**参考答案**：[split-index.c:16-17](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/split-index.c#L16-L17) 直接 `die`。二者都是对索引的复杂压缩策略：split-index 靠「基 + 增量位图」减少写入量，sparse-index 靠「稀疏目录条目」减少条目数。叠加会让稀疏目录条目跨基/拆分的语义难以定义（一个稀疏目录条目该算共享还是非共享？折叠/展开时如何同步基？），工程上难以正确维护，故显式禁止。

---

### 4.3 name-hash 路径快速查找

#### 4.3.1 概念说明

索引 `cache[]` 是按路径排序的数组，`index_name_pos` 用二分查找做精确路径定位，已是 O(log n)。那为什么还要 name-hash？

因为有两个需求二分满足不了：

1. **大小写不敏感查找**：在 macOS/Windows 等大小写不敏感文件系统上（`core.ignorecase=true`），用户输入 `README` 要能命中索引里的 `Readme`。但排序是大小写敏感的，二分找不到大小写变体。
2. **目录存在性快速判定**：`git status`、pathspec 匹配需要频繁问「这个目录在索引里有没有条目」，且要支持大小写不敏感。

name-hash 在 `index_state` 上维护**两个 hashmap**：

- `name_hash`：路径 → `cache_entry`，供 `index_file_exists` 查找；
- `dir_hash`：目录路径 → `dir_entry`（带引用计数 `nr`），供 `index_dir_exists` 查找。

关键是**懒加载**：hashmap 在索引读入后并不立即构建，而是首次查找时才由 `lazy_init_name_hash` 构建；并且对大索引会用多线程加速构建。哈希用 `memihash`（大小写不敏感哈希），所以同一路径的不同大小写写法会落到同一桶。

#### 4.3.2 核心流程

name-hash 的构建与查找流程：

```
   首次 index_file_exists / index_dir_find
              │
              ▼
   ┌─────────────────────────┐
   │ lazy_init_name_hash     │
   │  已初始化? 是→直接返回   │
   │  否→lookup_lazy_params  │
   └────────┬────────────────┘
            │
   ┌────────┴─────────┐
   │满足: ignore_case │否
   │  且 cache_nr≥4000│   ┌──────────────────┐
   │  且 CPU≥2        │   │ 单线程逐条        │
   └────────┬─────────┘   │ hash_index_entry  │
            ▼              └──────────────────┘
   ┌─────────────────────┐
   │ threaded_lazy_init  │
   │  Phase1: N 个 dir   │
   │   线程建 dir_hash   │
   │   (32 桶互斥锁)     │
   │  Phase2: 1 个 name  │
   │   线程建 name_hash  │
   └─────────────────────┘
```

- **懒触发**：`index_file_exists`/`index_dir_find`/`adjust_dirname_case` 入口都先调 `lazy_init_name_hash`。
- **线程决策**：`lookup_lazy_params` 仅在 `ignore_case` 且 `cache_nr >= 2*LAZY_THREAD_COST`（即 4000）且 CPU≥2 时启用多线程。
- **两阶段构建**：Phase 1 用 N 个「dir 线程」分片扫描索引建 `dir_hash`（用 32 个按桶分组的互斥锁降低争用，中间结果存入无锁的 `lazy_entries[]`）；Phase 2 用 1 个「name 线程」从 `lazy_entries` 填 `name_hash`，主线程同时补齐目录引用计数。
- **查找**：`index_file_exists` 用 `memihash` 算查询哈希，`hashmap_get_entry_from_hash` 定位桶，再 `same_name` 逐个比较——先精确 `memcmp`，不中再 `slow_same_name` 做大小写不敏感比较。

#### 4.3.3 源码精读

目录条目结构：

[name-hash.c:23-29](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/name-hash.c#L23-L29) 的 `struct dir_entry`：含 `parent`（父目录指针，形成目录树）、`nr`（本目录下条目引用计数，归零则可摘除）、`namelen`、`name[]`。

**单条目入哈希**：

[name-hash.c:118-131](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/name-hash.c#L118-L131) 的 `hash_index_entry`：先标 `CE_HASHED` 防重入；非稀疏目录条目（`!S_ISSPARSEDIR`）按 `memihash(name)` 入 `name_hash`；若 `ignore_case`，再 `add_dir_entry` 把它的各级父目录登记进 `dir_hash`。注意：**`dir_hash` 仅在 `ignore_case` 时才构建**——这是后面「线程化只为 dir_hash」的根因。

**懒初始化入口**：

[name-hash.c:591-620](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/name-hash.c#L591-L620) 的 `lazy_init_name_hash`：`name_hash_initialized` 为真则直接返回；否则初始化两个 hashmap，按 `lookup_lazy_params` 结果走多线程或单线程路径，最后置 `name_hash_initialized=1`。整个过程包在 `trace2_region_enter/leave("index", "name-hash-init", ...)` 里，便于用 trace2 观察。

**线程化参数决策**：

[name-hash.c:196-224](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/name-hash.c#L196-L224) 的 `lookup_lazy_params`：`LAZY_THREAD_COST=2000`（[name-hash.c:162](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/name-hash.c#L162)），门槛是 `cache_nr < 2*LAZY_THREAD_COST`（即 4000）就不线程化；线程数取 `min(online_cpus, cache_nr/LAZY_THREAD_COST)`。

**两阶段多线程构建**：

[name-hash.c:516-589](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/name-hash.c#L516-L589) 的 `threaded_lazy_init_name_hash`：

- Phase 1（[name-hash.c:543-560](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/name-hash.c#L543-L560)）：每个 dir 线程调 `handle_range_1` 处理自己的索引切片，结果写进各自独占的 `lazy_entries[k]` 区间（无锁），需插入 `dir_hash` 时按 `bucket(hash) % 32` 选互斥锁。
- Phase 2（[name-hash.c:572-582](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/name-hash.c#L572-L582)）：1 个 name 线程从 `lazy_entries` 读预算好的哈希填 `name_hash`，主线程同时 `lazy_update_dir_ref_counts` 补目录引用计数。注释指出测试表明 name_hash 阶段不值得开多线程。

**按桶分组锁**：

[name-hash.c:238-273](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/name-hash.c#L238-L273)：`init_dir_mutex` 建固定 32 个递归互斥锁（`LAZY_MAX_MUTEX=32`），`compute_dir_lock_nr` 用 `hashmap_bucket(map, hash) % 32` 选锁。注释（[name-hash.c:164-175](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/name-hash.c#L164-L175)）解释：用 n 个锁按桶分组，比单锁守整表冲突率低约 n 倍；为此必须**禁用 hashmap 的自动 rehash**（[name-hash.c:608](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/name-hash.c#L608) `hashmap_disable_item_counting`），否则表扩容会重排桶、破坏锁与桶的对应。

**查找**：

[name-hash.c:735-750](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/name-hash.c#L735-L750) 的 `index_file_exists`：`memihash(name, namelen)` 算哈希 → `hashmap_get_entry_from_hash` 取桶链 → `hashmap_for_each_entry_from` 遍历同桶条目 → `same_name` 判定。`same_name`（[name-hash.c:677-692](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/name-hash.c#L677-L692)）先做快的精确 `memcmp`（命中即返回，因为大小写完全一致是常见情况），不中且 `icase` 时才退化到 `slow_same_name`（[name-hash.c:658-675](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/name-hash.c#L658-L675)）逐字符 `toupper` 比较。

目录查找 [name-hash.c:694-709](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/name-hash.c#L694-L709) 的 `index_dir_find` 类似，返回 `dir && dir->nr`（引用计数 > 0 才算该目录有条目）。

`name_hash`、`dir_hash`、`name_hash_initialized` 都挂在 `struct index_state`（[read-cache-ll.h:174-182](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache-ll.h#L174-L182)）。

#### 4.3.4 代码实践

**实践目标**：用 trace2 观察 name-hash 的懒加载与多线程构建，验证线程化门槛。

**操作步骤**：

1. 在 git 源码仓库跑一次 status 并采集 trace2 性能事件：
   ```
   GIT_TRACE2_PERF=1 git status >/dev/null 2>trace.txt
   grep -i name-hash trace.txt
   ```
2. 在输出里找 `name-hash-init` region 的 enter/leave 事件，确认它只出现一次（懒加载）。
3. 观察 region 是否报告使用了多线程（取决于 `core.ignorecase` 与索引规模）。若在大小写敏感文件系统上默认 `ignorecase=false`，则不会走多线程路径——这本身印证了 `lookup_lazy_params` 的 `ignore_case` 前置条件。
4. 对照 [name-hash.c:196-224](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/name-hash.c#L196-L224) 确认门槛：`ignore_case` 且 `cache_nr >= 4000` 且 CPU≥2。
5. 想直接测试线程化逻辑，可用 `t/helper/test-lazy-init-name-hash`：
   ```
   make -C t/helper test-tool
   t/helper/test-tool lazy-init-name-hash --analyze
   ```
   该工具的 `--analyze` 会输出不同线程数下的耗时，正是源码注释里提到的调参依据（[name-hash.c:159-161](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/name-hash.c#L159-L161)）。

**需要观察的现象**：

- `name-hash-init` region 出现一次，且发生在首次路径查找时（不是读索引时）。
- 大小写敏感系统上不出现多线程 dir 线程；强制 `core.ignorecase true` 且索引足够大时才出现。

**预期结果**：name-hash 是按需构建的；多线程构建仅在「大小写不敏感 + 大索引 + 多核」三者同时满足时启用。

> 精确线程数与耗时「待本地验证」。`test-tool lazy-init-name-hash` 的子命令选项以本地 `t/helper/test-lazy-init-name-hash.c` 实际提供为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 name-hash 的多线程构建只在 `ignore_case` 开启时才有意义？

**参考答案**：大小写敏感时，`index_name_pos` 的二分查找已能 O(log n) 精确定位，`name_hash` 仅作辅助；而 `dir_hash` 只在 `ignore_case` 时由 `add_dir_entry` 构建（见 [name-hash.c:129-130](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/name-hash.c#L129-L130)）。多线程化的目标是加速 `dir_hash` 构建，`dir_hash` 都不建，自然无需线程化（`lookup_lazy_params` 第 210-211 行直接返回 0）。

**练习 2**：线程化构建 `dir_hash` 时为什么用 32 个互斥锁，而不是 1 个全局锁？

**参考答案**：1 个全局锁会让所有 dir 线程在插入 `dir_hash` 时串行等待。用 `bucket(hash) % 32` 把 hashmap 的桶分成 32 组、每组一把锁，不同桶的插入可真正并行，冲突率约降到 1/32（见 [name-hash.c:164-175](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/name-hash.c#L164-L175) 注释）。代价是必须禁用自动 rehash，否则扩容重排桶会破坏「锁↔桶」对应关系。

**练习 3**：`index_file_exists` 在 hashmap 里定位到桶后，为什么还要逐个 `same_name` 比较，不能直接返回？

**参考答案**：哈希冲突会让不同路径落入同一桶；`memihash` 又是大小写不敏感哈希，大小写不同的同名条目也会同桶。必须逐个比较确认：先精确 `memcmp`（覆盖最常见的大小写完全一致情况，最快），不中再 `slow_same_name` 做大小写不敏感比较（见 [name-hash.c:677-692](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/name-hash.c#L677-L692)）。哈希只负责「缩小范围」，相等性最终仍靠逐字符比较判定。

---

## 5. 综合实践

把三个机制串起来观察一次完整的「改动 → 暂存 → 提交」链路：

**任务**：在 git 源码仓库里，开启 trace2 与（可选）split-index，做一次小改动并提交，从性能事件里同时读出三个机制的活动。

**步骤**：

1. 准备：
   ```
   git config core.ignorecase false        # 大小写敏感，便于看清 name-hash 行为
   # 可选：git config splitIndex true
   ```
2. 采集一次基线 status：
   ```
   GIT_TRACE2_PERF=1 git status >/dev/null 2>trace-baseline.txt
   ```
   在 `trace-baseline.txt` 里定位：`name-hash-init` region（name-hash 首次构建）、`cache_tree` 相关 region、若开了 split-index 还有 `shared/do_read_index` region。
3. 改动一个深层文件并暂存：
   ```
   echo "// touch" >> builtin/write-tree.c
   git add builtin/write-tree.c
   ```
   对照 [read-cache.c:612-613](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L612-L613) 与 [cache-tree.c:113-157](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c#L113-L157) 想清楚：这一步会失效 `builtin/` 目录及其祖先的 cache-tree 节点，并在 `cache_changed` 里置 `CACHE_TREE_CHANGED` 与 `CE_ENTRY_CHANGED`。
4. 提交并采集：
   ```
   GIT_TRACE2_PERF=1 git commit -m "touch write-tree" >/dev/null 2>trace-commit.txt
   ```
5. 在 `trace-commit.txt` 里找：
   - `cache_tree` 的 `update` region——本次提交触发了 `cache_tree_update` 重建被失效的目录树（因为提交要 `write-tree`）；
   - 若开了 split-index，看 `index` 的 `shared/do_write_index` 与主索引写——`builtin/` 那个条目进了 split 增量（`replace_bitmap`），其余基条目不重写；
   - `name-hash-init` 应**不再出现**（已初始化过），印证懒加载只发生一次。
6. 再做一次「无改动」提交对照（用 `--allow-empty`）：
   ```
   GIT_TRACE2_PERF=1 git commit --allow-empty -m "empty" >/dev/null 2>trace-empty.txt
   ```
   `cache_tree` 的 `update` region 应极短或命中短路——因为上一次提交已把 cache-tree 重建并持久化，本次 `cache_tree_fully_valid` 返回真。

**需要观察的现象**：

- 改动暂存后，仅受影响目录的 cache-tree 失效，而非整棵树重建。
- 提交时 `cache_tree_update` 只重建被失效的子树，未失效子树命中短路。
- split-index 下，主索引只写增量，共享基不重写（除非非共享占比超 20%）。
- name-hash 仅在首次查找时构建一次。

**预期结果**：三个机制各司其职——cache-tree 避免重算 tree、split-index 避免重写整索引、name-hash 提供快速路径查找，共同把「一次小改动」的代价压到与改动规模成正比，而非与仓库规模成正比。

> 精确耗时与 region 名称以本地 trace2 输出为准，「待本地验证」。

## 6. 本讲小结

- **cache-tree** 是挂在 `index_state` 上的目录树形缓存，每个节点存「覆盖条目数 `entry_count` + 对应 tree 对象 `oid`」；`entry_count >= 0` 为有效、`< 0`（`-1`）为无效。索引改动走 `cache_tree_invalidate_path` 惰性失效，`write-tree` 时 `update_one` 命中即短路（[cache-tree.c:336-339](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c#L336-L339)），无效才重建。序列化为 `.git/index` 的 `TREE` 扩展。
- **split-index** 把索引拆成只读「共享基 `sharedindex.<oid>`」+ 增量 `.git/index`（`link` 扩展，含 `delete_bitmap`/`replace_bitmap` 两个 EWAH 位图）。`cache_entry.index` 字段（1-based，0=新条目）是区分基/增量的依据。非共享占比超 20% 时换新基。
- **name-hash** 维护 `name_hash`（路径→条目）与 `dir_hash`（目录→引用计数）两个 hashmap，懒加载；仅 `ignore_case` 且索引≥4000 条且多核时用两阶段多线程构建 `dir_hash`，靠 32 个按桶分组互斥锁降冲突。
- 三者都遵循「读时按需加载、写时按需持久化、改动时按需失效」的缓存套路，并通过 `cache_changed` 位标志（[read-cache-ll.h:128-136](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache-ll.h#L128-L136)）与 `EXTMASK`（[read-cache.c:79-81](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L79-L81)）协调「是否只需写扩展、不必整写索引」。
- cache-tree 与 sparse-index 有协同（`update_one` 识别 `S_ISSPARSEDIR` 叶子，[cache-tree.c:324-334](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c#L324-L334)）；split-index 与 sparse-index 互斥。

## 7. 下一步学习建议

- **下一讲 u4-l3「稀疏索引 sparse-index」**：继续索引主题，看 `sparse-index.c` 如何把整目录折叠成单个稀疏目录条目以压缩巨型单仓库索引。重点关注它与 cache-tree 的协同（`prime_cache_tree_rec` 对稀疏目录的特殊处理）以及与 split-index 的互斥约束。
- **横向联系 u9-l2「checkout/switch 与 unpack-trees」**：`prime_cache_tree` 的主要调用点就在 checkout/reset/read-tree 之后，读完那讲你会更理解「为什么切换分支后第一次 write-tree 也很快」。
- **性能向 u13-l1「commit-graph 与 multi-pack-index」**：cache-tree 是「索引侧」的目录树缓存，commit-graph 则是「历史侧」的提交图缓存，二者思路相通（用额外文件缓存派生数据、按需失效重建），可对比学习。
- **源码阅读**：想深入 cache-tree 的正确性保证，可读 `verify_one`/`cache_tree_verify`（[cache-tree.c:961-1100](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/cache-tree.c#L961-L1100)），并配合环境变量 `GIT_TEST_CHECK_CACHE_TREE=1`（[read-cache.c:3316-3318](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L3316-L3318)）在测试中强制校验。
