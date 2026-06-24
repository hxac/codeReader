# Chunk 元数据与 RocksDB

## 1. 本讲目标

在 u6-l1 我们看到 chunk engine 把「一个 target 在单机上的物理化身」分成 meta / alloc / file 三层；在 u6-l2 我们弄清了物理块「在哪个槽位」。本讲要回答的下一个问题是：

> 当一次写操作发生时，新 chunk 的元数据、它占用的新物理块、被它取代的旧物理块，这些状态是**如何被安全地、原子地**记录下来的？

读完本讲，你应当能够：

1. 看懂 chunk engine 把一个 target 的全部元数据塞进**同一个 RocksDB**时，靠 1 字节前缀区分的 8 类记录及其 key 编码方式，特别是 `chunk_meta_key` 的「按位取反」技巧。
2. 理解 `MetaStore` 如何封装 RocksDB，以及它用 **merge operator**（group 位图、used_size 计数）实现「无读改写」的并发安全累加。
3. 把 `update_chunk → commit_chunk` 的两阶段写流程讲清楚，并说出一次 `commit` 在**一个 `WriteBatch`** 里到底更新了哪几类 key、为什么必须原子，以及崩溃后如何靠 `writing_chunk` intent 日志恢复。

---

## 2. 前置知识

本讲默认你已经读过 u6-l1（engine 三层结构、CRAQ 双版本不变量 `commitVer ≤ updateVer ≤ commitVer+1`）和 u6-l2（物理块分级、`Position` 打包、写时复制 COW）。下面补充两个本讲会用到的 RocksDB 基础概念。

- **LSM-Tree 与字节序排序**：RocksDB（与 LevelDB 同源）是一个按 key 字节字典序排序的 KV 存储。`get(key)` 是点查，`iterator` 可以按 key 升序扫描一段区间。区间扫描和点查都依赖 key 的字节布局设计——本讲大量篇幅就是在讲「key 怎么排」。
- **WriteBatch（写批）**：RocksDB 允许把多个 `put`/`delete`/`merge` 操作打包成一个 `WriteBatch`，一次 `write()` 提交。**写批是原子的**——要么全部落盘生效，要么一个都不生效。这是本讲「一致性」的核心机制。
- **Merge operator（合并算子）**：RocksDB 原生支持一种叫 `merge` 的写操作。对一个 key 连续 `merge` 多个「增量（operand）」时，RocksDB 会在读取或 compaction 时调用你注册的 `full_merge` / `partial_merge`，把「旧值 + 一串增量」折叠成一个新值。它的意义是：多个写者可以各自 `merge` 自己的增量，而**不必先 `get` 再 `put`**（read-modify-write 有并发竞态）。

> 术语提示：本讲出现的 `chunk_id` 是逻辑块标识（字节串，由上层传入），`pos`（`Position`）是物理槽位（打包了块大小/cluster/group/槽号），二者通过元数据关联。

---

## 3. 本讲源码地图

本讲涉及的关键文件都在 `src/storage/chunk_engine/src/` 下：

| 文件 | 作用 |
| --- | --- |
| [meta/meta_key.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_key.rs) | 定义 RocksDB 中**所有 key 的编码方式**（8 类前缀）。 |
| [types/chunk_meta.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/types/chunk_meta.rs) | `ChunkMeta` 结构体定义——chunk 元数据的 value 内容。 |
| [meta/rocksdb.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/rocksdb.rs) | 对 RocksDB 的薄封装：`open`、`get`、`put`、`write`、`new_write_batch`、迭代器、`MergeOp` trait。 |
| [meta/meta_merge.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_merge.rs) | `MetaMergeOp`：group 位图与 used_size 两类 merge 的具体实现。 |
| [meta/meta_store.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_store.rs) | `MetaStore`：在 RocksDB 之上提供 `add_chunk / move_chunk / remove` 等业务接口，负责**把多类 key 的更新打包成原子写批**。 |
| [core/engine.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs) | `Engine`：把 `update_chunk`（落盘 intent）与 `commit_chunk`（原子写批）两阶段串起来，并维护内存缓存 `meta_cache`。 |
| [alloc/writing_chunk.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/writing_chunk.rs) | `WritingChunk`：在途写的句柄，及其 `Drop` 时根据是否提交成功来清理 `writing_list` 的逻辑。 |
| [types/group_state.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/types/group_state.rs) / [types/merge_state.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/types/merge_state.rs) | group 的 256 位位图，与 merge 增量 `{acquire, release}` 的定义。 |

---

## 4. 核心概念与源码讲解

### 4.1 元数据 key：用 1 字节前缀区分 8 类记录

#### 4.1.1 概念说明

一个 storage target 在单机上的**全部元数据**——每个 chunk 的内容、每个物理 group 里哪些槽被占用、每个前缀桶用了多少空间、每个 chunk 的写入时间戳、在途写日志、schema 版本——都被塞进**同一个 RocksDB 实例**里。怎么让它们互不干扰？答案是给每类记录的 key 冠以**不同的 1 字节前缀**。

这样做有两个直接好处：

- 不同类记录的 key 落在**互不相交的字节区间**，天然隔离；扫描某一类只需 `seek` 到它的前缀区间。
- 所有记录共享同一个 LSM-Tree、同一份 compaction 与缓存，省去了维护多个 DB 的开销。

`ChunkMeta` 是「chunk 的内容」，它是本类记录的主角：

[src/storage/chunk_engine/src/types/chunk_meta.rs:7-19](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/types/chunk_meta.rs#L7-L19) 定义了 `ChunkMeta` 的全部字段：`pos`（物理位置）、`chain_ver`/`chunk_ver`（CRAQ 双版本）、`len`、`checksum`、`timestamp`、`last_request_id`/`last_client_*`（幂等去重用）、`etag`、以及 `uncommitted` 标记。其中 `pos` 把「逻辑 chunk」和「物理块」绑定起来，是后续一切迁移/回收的依据。

#### 4.1.2 核心流程：8 类前缀一览

前缀常量集中定义在 [meta/meta_key.rs:7-16](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_key.rs#L7-L16)：

| 前缀值 | 常量 | key 组成 | value 含义 |
| --- | --- | --- | --- |
| 1 | `CHUNK_META_KEY_PREFIX` | `1 ‖ ~chunk_id` | 序列化的 `ChunkMeta` |
| 2 | `GROUP_BITS_KEY_PREFIX` | `2 ‖ group_id(8B, 大端)` | 256 位 group 占用位图（32B） |
| 3 | `POS_TO_CHUNK_KEY_PREFIX` | `3 ‖ pos(8B, 大端)` | 占据该物理位置的 `chunk_id` |
| 4 | `USED_SIZE_KEY_PREFIX` | `4 ‖ chunk_id 前缀` | 该前缀桶已用字节数（i64, 小端） |
| 5 | `USED_SIZE_PREFIX_LEN_KEY` | `5`（单条） | 当前 used_size 桶的前缀长度（u32, 小端） |
| 6 | `TIMESTAMP_KEY_PREFIX` | `6 ‖ chunk_id[..prefix_len] ‖ ts(8B,大端) ‖ chunk_id[prefix_len..]` | 该 chunk_id（时间戳索引） |
| 8 | `VERSION_KEY` | `8`（单条） | schema 版本号 |
| 9 | `WRITING_CHUNK_KEY_PREFIX` | `9 ‖ chunk_id` | 在途写的 `ChunkMeta`（intent 日志） |

可以看到一个贯穿全表的设计原则：**key 存「身份/索引」，value 存「内容/计数」**。比如 `CHUNK_META` 的 key 是 chunk 的身份（`chunk_id`），value 是它的全部内容；`POS_TO_CHUNK` 的 key 是物理位置，value 是占据它的 chunk 身份——这是为「物理位置 → chunk」的反查建索引。

#### 4.1.3 源码精读

**① `chunk_meta_key` 的「按位取反」技巧**

这是全文件最反直觉、也最值得讲的一处。[meta/meta_key.rs:28-34](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_key.rs#L28-L34) 在拼 key 时，对 `chunk_id` 的每个字节做了**按位取反 `!num`**：

```rust
pub fn chunk_meta_key(chunk_id: &[u8]) -> Self {
    let mut out = Self::chunk_meta_key_prefix();
    for num in chunk_id {
        out.0.push(!num)        // 每个字节取反
    }
    out
}
```

`parse_chunk_meta_key`（[meta/meta_key.rs:36-42](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_key.rs#L36-L42)）再做一次取反还原。取反的效果是：**把字节序整个翻转**——`chunk_id` 越大，取反后的 key 字节越小，因而在 RocksDB 里**排得越靠前**。

为什么要翻转？看 `query_chunks` 的扫描方式（[meta/meta_store.rs:59-97](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_store.rs#L59-L97)）：它先 `seek` 到区间**上界** `end_key`，跳过它，再**向前 `next()`** 扫描，命中 `chunk_id >= begin` 就收集。由于取反让「物理升序 = 逻辑降序」，这个「从上界向前扫」的过程产出的 `chunk_id` 是**降序**的。这一点被单测钉死：

```rust
// meta_store.rs 测试 test_meta_get_set
let vec = meta_store.query_chunks(10u32.to_be_bytes(), 20u32.to_be_bytes(), 30).unwrap();
assert_eq!(vec.len(), 10);
assert_eq!(vec.first().unwrap().0.as_ref(), &19u32.to_be_bytes());  // 19 在最前
assert_eq!(vec.last().unwrap().0.as_ref(),  &10u32.to_be_bytes());  // 10 在最后
```

即 `[10, 20)` 返回 `19, 18, …, 10`。配合迭代器设置的 4 MiB readahead（见 4.2），用「取反 + 前向扫描」实现了「按 `chunk_id` 降序、前向预读」的区间列举——这是一种把逻辑降序映射到物理前向扫描的实现选择。

**② 物理位置与 group 的 key**

- `group_bits_key`（[meta/meta_key.rs:54-58](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_key.rs#L54-L58)）：`GroupId` 是一个 `u64`（[types/group_id.rs:14-19](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/types/group_id.rs#L14-L19)），把「32 位 chunk_size ‖ 24 位 group ‖ 8 位 cluster」打包，固定 256 个槽（`COUNT = 1<<8`）。大端序保证 group 按 (chunk_size, group, cluster) 升序排列。
- `pos_to_chunk_key`（[meta/meta_key.rs:84-88](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_key.rs#L84-L88)）：`Position` 同样是 `u64`（[types/position.rs:12-16](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/types/position.rs#L12-L16)），打包「24 位 chunk_size ‖ 8 位 cluster ‖ 24 位 group ‖ 8 位 槽号」。这张表用于反查「某物理位置现在被哪个 chunk 占着」。

**③ 时间戳索引的复合 key**

`timestamp_key`（[meta/meta_key.rs:126-130](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_key.rs#L126-L130)）把 `chunk_id` 拆成前后两段，中间塞入大端 `timestamp`：

```
6 ‖ chunk_id[..prefix_len] ‖ timestamp(8B 大端) ‖ chunk_id[prefix_len..]
```

这样排序后，同一个前缀桶内的 chunk 按**写入时间升序**排列——`query_chunks_by_timestamp`（[meta/meta_store.rs:99-136](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_store.rs#L99-L136)）就能高效地按时间区间扫描，用于「找出某段时间内写过的 chunk」这类（GC、dump-chunkmeta 对比等）操作。前缀分桶（`prefix_len`）让时间索引可以按 `chunk_id` 前缀并行分片。

#### 4.1.4 代码实践：手工推算一个 key

1. **实践目标**：把 `chunk_meta_key` 的字节布局算出来，并验证它确实把顺序翻转了。
2. **操作步骤**：
   - 打开本讲引用的 [meta/meta_key.rs:186-192](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_key.rs#L186-L192) 处的单测 `test_meta_key_create`。
   - 手工计算 `chunk_meta_key(&[1,2,3,4])`：前缀 `1`，随后是 `!1, !2, !3, !4`，即 `0xFE, 0xFD, 0xFC, 0xFB`，故 key = `[0x01, 0xFE, 0xFD, 0xFC, 0xFB]`。
   - 再算 `chunk_meta_key(&[1,2,3,5])` = `[0x01, 0xFE, 0xFD, 0xFC, 0xFA]`。
3. **需要观察的现象**：`[1,2,3,4] < [1,2,3,5]`（chunk_id 升序），但取反后 `…FB > …FA`，即 `chunk_meta_key([1,2,3,4]) > chunk_meta_key([1,2,3,5])`（key 降序）。
4. **预期结果**：在 RocksDB 中 `[1,2,3,5]` 这条记录**排在** `[1,2,3,4]` **前面**；因此从上界向前扫描会先遇到较大的 `chunk_id`，印证 4.1.3 的降序结论。
5. 若想直接验证，可在 `src/storage/chunk_engine/` 下运行该单测：`待本地验证`（需要 Rust 工具链，命令形如 `cargo test -p chunk_engine --lib meta_key::tests`）。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `chunk_meta_key` 要对 `chunk_id` 取反，而 `group_bits_key`、`pos_to_chunk_key` 用大端不取反？
  - **答案**：取反是为了把「逻辑 `chunk_id` 降序」映射成「物理 key 升序」，从而配合前向扫描 + readahead 实现「新/大 chunk 在前」的列举（见 `query_chunks`）。而 group 位图、物理位置这些索引本就需要**升序**遍历（如「找到第一个空闲 group」），所以直接用大端序即可。
- **练习 2**：`timestamp_key` 为什么把 `chunk_id` 拆成 `prefix_len` 前后两段、把 `timestamp` 夹在中间？
  - **答案**：前段 `chunk_id[..prefix_len]` 作为「桶」，让时间索引按前缀分片（可并行扫描、可统计每桶大小）；夹在中间的大端 `timestamp` 让**同一桶内**按写入时间升序排列，于是 `query_chunks_by_timestamp(prefix, begin, end)` 只需一次前向区间扫描。

---

### 4.2 RocksDB 存储：merge operator 实现「无读改写」累加

#### 4.2.1 概念说明

`MetaStore`（[meta/meta_store.rs:13-16](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_store.rs#L13-L16)）内部就一个 RocksDB 实例（路径在 `config.path.join("meta")`，见 [engine.rs:31-39](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L31-L39)）。

这里有两类状态会被**高频、并发地修改**：

1. **group 占用位图**（`GROUP_BITS`）：一个 group 有 256 个槽，每次写/迁移/删除都要置位/清位。如果用 `get → 改 → put`，两个并发写者会互相覆盖（丢失更新）。
2. **used_size 计数**（`USED_SIZE`）：每写一个 chunk 加、每删一个 chunk 减。同样是「读改写」竞态重灾区。

3FS 的解法是**不读不改写**：对这两类 key 只用 `merge` 提交一个**增量描述**（「我占了第 3 个槽」「我增加了 1 MiB」），由 RocksDB 在读/compaction 时调用注册的 `MetaMergeOp` 把一串增量折叠成最终值。多个写者各 merge 各的增量，天然无竞态。

#### 4.2.2 核心流程

`RocksDB::open` 在打开时做三件关键事（[meta/rocksdb.rs:27-54](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/rocksdb.rs#L27-L54)）：

1. **注册 merge operator**（[meta/rocksdb.rs:30-34](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/rocksdb.rs#L30-L34)）：把 `MetaMergeOp::full_merge` / `partial_merge` 挂到名字为 `"merge"` 的算子上。
2. **开 Bloom filter**（[meta/rocksdb.rs:36-38](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/rocksdb.rs#L36-L38)）：`set_bloom_filter(10.0, true)`。`ChunkMeta` 查询绝大多数是**点查** `get(chunk_meta_key)`，Bloom filter 能在 SST 里快速排除不存在的 key。
3. **预备两套 `WriteOptions`**（[meta/rocksdb.rs:47-53](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/rocksdb.rs#L47-L53)）：索引 0 = 普通（不 `fsync`），索引 1 = `sync`（`fsync`）。后面所有写都通过 `write_options[sync as usize]` 二选一——这是**持久性开关**：已提交的 chunk 用 `sync=true`，内部整理（如 compaction）可用 `sync=false` 换吞吐。

merge 的折叠规则由 key 的首字节分派（[meta/meta_merge.rs:9-59](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_merge.rs#L9-L59)）：

- `GROUP_BITS` 分支（[meta/meta_merge.rs:15-28](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_merge.rs#L15-L28)）：每个 operand 是序列化的 `MergeState{ acquire, release }`（[types/merge_state.rs:7-43](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/types/merge_state.rs#L7-L43)）。先把所有 operand 折叠成一个 `MergeState`（`acquire`/`release` 两个集合做并集，且 `acquire` 会抵消同槽的 `release`、反之亦然），再一次性 `GroupState::update` 应用到位图上。`GroupState` 是一张 256 位位图（4 个 `u64`，共 32 字节，[types/group_state.rs:8-11](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/types/group_state.rs#L8-L11)），`update`（[types/group_state.rs:100-112](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/types/group_state.rs#L100-L112)）对 `acquire` 集合置位、对 `release` 集合清位。
- `USED_SIZE` 分支（[meta/meta_merge.rs:29-46](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_merge.rs#L29-L46)）：每个 operand 是小端 `i64` 增量。`full_merge` 把「旧值 + 所有增量」求和；`partial_merge` 只把若干增量求和。

`partial_merge`（[meta/meta_merge.rs:61-89](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_merge.rs#L61-L89)）让 RocksDB 在 **compaction** 时就能把一串增量预先折叠成一个增量，避免增量无限堆积；两类 key 都支持。

#### 4.2.3 源码精读

`MergeOp` 是 3FS 自定义的合并 trait（[meta/rocksdb.rs:16-24](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/rocksdb.rs#L16-L24)），签名与 RocksDB 原生 merge 回调对应：`full_merge(key, 现值, 一串增量)` 和 `partial_merge(key, 一串增量)`。返回 `Option<Vec<u8>>`，**返回 `None` 即表示合并失败**。

合并失败的后果很严重：RocksDB 会把该 key 标记为「merge 出错」，此后对该 key 的 `get` 和迭代都会返回错误。这正是单测 `test_rocksdb_invalid_merge`（[meta/rocksdb.rs:265-313](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/rocksdb.rs#L265-L313)）验证的——对一个**没有注册过**的 key 前缀（如 `TEST_KEY_PREFIX` 之外的任意 key）`merge`，`full_merge` 落入 `_ => None` 分支，`get` 立即报错。所以 `MetaMergeOp` 必须对它能产生的所有 merge key 都给出确定结果。

写批与迭代器接口：

- `new_write_batch()` / `write(batch, sync)`（[meta/rocksdb.rs:80-89](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/rocksdb.rs#L80-L89)）：写批入口，`write` 按 `sync` 选 `WriteOptions`。
- `new_iterator()`（[meta/rocksdb.rs:91-95](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/rocksdb.rs#L91-L95)）：用 `raw_iterator_opt` 并设置 `readahead_size = 4 MiB`，为大范围扫描预读。

#### 4.2.4 代码实践：跟踪一次 used_size 的 merge

1. **实践目标**：理解 `USED_SIZE` 的增量累加如何在并发下保持正确。
2. **操作步骤**：阅读 [meta/meta_merge.rs:107-151](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_merge.rs#L107-L151) 的单测 `test_used_size_merge`。
   - 它构造 10 个增量 `0..10`（每个是小端 `i64`），先 `partial_merge` 得到 `sum(0..10)=45`；再 `full_merge(现值=10, 增量=0..10)` 得到 `55`。
   - 还验证了「非法增量」（长度不是 8 字节）返回 `None`。
3. **需要观察的现象**：无论多少个写者各自 `merge(+N)`，RocksDB 最终折叠出的值都等于「初值 + 所有增量之和」——**顺序无关、无丢失**。
4. **预期结果**：据此回答一个并发场景——若写者 A 并发 `merge(+1MiB)`、写者 B 并发 `merge(+2MiB)`，最终 used_size 一定增加 3 MiB，绝不会因为「A、B 同时读到旧值再各自覆盖」而只增加 1 或 2 MiB。这就是 merge 相对 read-modify-write 的核心价值。
5. 该断言由 `test_rocksdb_parallel_write`（[meta/rocksdb.rs:224-262](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/rocksdb.rs#L224-L262)，16 线程并发写）间接保证；可在本地运行确认：`待本地验证`。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `USED_SIZE` 的增量用 `i64`（有符号），而 `query_used_size` 读取时用 `u64`（无符号）？
  - **答案**：merge 阶段需要做**带符号求和**（删除是负增量），所以增量按 `i64` 小端编码；稳态下 used_size 永不为负，读取时按 `u64` 解释同一组字节即可。两者字节序一致（都是小端），只是解释不同。
- **练习 2**：如果一个 key 被 `merge` 了，但 `MetaMergeOp` 对它的首字节返回 `None`，会发生什么？
  - **答案**：该 key 进入「merge 出错」状态，之后对它的 `get` / 迭代都会返回错误（见 `test_rocksdb_invalid_merge`）。这就是为什么 `MetaMergeOp` 必须覆盖所有会被 merge 的前缀（`GROUP_BITS`、`USED_SIZE`，以及测试用的 `TEST_KEY_PREFIX`）。

---

### 4.3 update/commit 的原子写批：一个 fsync 落定一切

#### 4.3.1 概念说明

一次写操作的元数据更新，涉及**至少 4～5 类 key** 同时变化：

- `CHUNK_META`（chunk 的内容，含新 `pos`）
- `POS_TO_CHUNK`（新物理位置 → chunk；旧位置要删）
- `GROUP_BITS`（占新槽、释放旧槽）
- `USED_SIZE`（增减已用空间）
- `TIMESTAMP`（更新时间索引）
- 以及清理 `WRITING_CHUNK`（在途写日志）

如果这些更新**分多次** `write`，一旦中间崩溃就会出现「半应用」状态——比如 `CHUNK_META` 已写、但 group 位图还没置位，重启后分配器会把这个槽**再次分配给别人**，导致两个 chunk 共用同一物理位置，数据损坏。

3FS 的解法是：把这「一组相关 key 的更新」**全部塞进一个 `WriteBatch`**，用**一次 `write(batch, sync=true)`**（一次 `fsync`）原子提交。要么全落盘，要么一个都不落——这是本讲一致性的总根。

为此，写路径被显式拆成**两阶段**：

- **`update_chunk`**：在内存里准备好新 chunk（分配/COW/safe_write），写入**在途写日志** `WRITING_CHUNK`（这是它自己的一次 sync 写，作为 intent），但**还不写** `CHUNK_META`。返回一个 `WritingChunk` 句柄。
- **`commit_chunk`**：拿到句柄，把「新元数据 + 新旧物理块 + used_size + 时间戳 + 清理 intent」组装进**一个** `WriteBatch` 一次提交，再更新内存缓存 `meta_cache`。

#### 4.3.2 核心流程

下图是一次「COW 写」（`pos` 发生变化）的完整流程（对应 [engine.rs:290-518](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L290-L518)）：

```
update(chunk_id, req)
 ├─ update_chunk                                  [engine.rs:295]
 │   ├─ 校验、版本检查、分配/COW 新 chunk
 │   ├─ 插入内存 writing_list
 │   └─ persist_writing_chunk ── put WRITING_CHUNK (sync)   ← intent 日志
 │      返回 WritingChunk（此时 CHUNK_META 尚未落盘）
 └─ commit_chunk(WritingChunk)                    [engine.rs:481]
     ├─ 取 meta_cache 写锁（per-chunk）
     ├─ 旧 chunk 存在？ → move_chunk_mut(old, new) 装填写批
     │   旧 chunk 不存在？→ add_chunk_mut(new)     装填写批
     ├─ write(WriteBatch, sync=true)               ← 一次 fsync 落定一切
     ├─ 更新 meta_cache 条目
     └─ 标记 commit_succ（Drop 时从 writing_list 移除）
```

**`add_chunk_mut` 的 6 步**（[meta/meta_store.rs:149-191](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_store.rs#L149-L191)，全部进同一个 `WriteBatch`）：

| 步骤 | 操作 | key 前缀 | 语义 |
| --- | --- | --- | --- |
| 1 | `put` chunk_meta_key → ChunkMeta | 1 | 写入 chunk 内容（新 `pos`） |
| 2 | `put` pos_to_chunk_key(pos) → chunk_id | 3 | 登记新物理位置归属 |
| 3 | `merge` group_bits_key(group) ← `acquire(index)` | 2 | 占用新槽 |
| 4 | `merge` used_size_key(prefix) ← `+chunk_size` | 4 | 增加已用空间 |
| 5 | `put` timestamp_key(ts, id) → id | 6 | 建时间索引 |
| 6 | `delete` writing_chunk_key(id) | 9 | 清理 intent |

`move_chunk_mut`（[meta/meta_store.rs:205-271](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_store.rs#L205-L271)）在此基础上**多了「释放旧块」**：当 `old.pos != new.pos` 时，额外 `delete` 旧 `pos_to_chunk`、`merge release(旧 index)`、并把 used_size 调整为 `新块大小 − 旧块大小`；时间索引则 `put` 新时间戳 + `delete` 旧时间戳。`remove_mut`（[meta/meta_store.rs:279-319](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_store.rs#L279-L319)）是「全删」版本。

**批量提交 `commit_chunks`**（[engine.rs:520-581](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L520-L581)）更进一步：先按 `chunk_id` 排序获取全部 `meta_cache` 锁（避免死锁），再把**所有 chunk 的更新混进同一个 `WriteBatch`**，最后**只调用一次 `write(batch, sync)`**。这意味着无论一批提交多少个 chunk，都只付出**一次 fsync** 的代价——这是高吞吐的关键。

**崩溃恢复**：若进程在 `update_chunk` 之后、`commit` 之前崩溃，磁盘上只留下 `WRITING_CHUNK` 记录、`CHUNK_META` 仍是旧值。重启时 `Engine::open` 调用 `occupy_uncommitted_positions`（[meta/meta_store.rs:367-405](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_store.rs#L367-L405)）：扫描所有 `WRITING_CHUNK`，对其中 `pos` 与已提交 meta 不一致的（即在途的）chunk，**临时把它的物理位置在 group 位图里重新占用**（防止分配器再次下发），并把它们重新加载进内存 `writing_list`；之后 `vacate_uncommitted_positions`（[meta/meta_store.rs:407-434](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_store.rs#L407-L434)）回滚这次临时占用。等拿到正确的 `chain_ver` 后，`handle_uncommitted_chunks` 会把这些在途写正式提交。于是 `WRITING_CHUNK` 充当了**在途写的 WAL**。

#### 4.3.3 源码精读

**① Engine 结构与内存缓存**

[engine.rs:18-28](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L18-L28) 里 `meta_store: Arc<MetaStore>` 是状态边界，`meta_cache: Arc<LockMap<Bytes, ChunkArc>>` 是内存缓存兼 per-key 锁（256 分片）。读路径 `get`（[engine.rs:184-209](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L184-L209)）先查缓存命中即返回，未命中才 `meta_store.get_chunk_meta` 从 RocksDB 加载并填缓存。**commit 在 `meta_cache` 的写锁保护下替换条目**（[engine.rs:502-514](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L502-L514)），保证读到的 `ChunkArc` 永远是一个完整的、已提交的版本——这是内存层面的一致性。

**② `update_chunk` 的两处关键落点**

- 写入 intent 日志（[engine.rs:468-469](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L468-L469)）：`self.meta_store.persist_writing_chunk(chunk_id, new_chunk.meta())?`，这是 `update_chunk` 里**唯一**一次 RocksDB 写，且是独立 `put` + `sync`（见 [meta/meta_store.rs:348-356](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_store.rs#L348-L356)）。
- CRAQ 版本不变量在这里校验（[engine.rs:351-376](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L351-L376)）：拒绝 `update_ver ≤ commit_ver`（已提交）或 `update_ver > commit_ver+1`（缺更新），强制满足 `commitVer ≤ updateVer ≤ commitVer+1`。

**③ `commit_chunks` 的写批装填循环**

[engine.rs:537-567](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L537-L567) 是批量提交的核心：遍历每个 `WritingChunk`，按「remove / 已存在 / 不存在」分别调 `remove_mut` / `move_chunk_mut` / `add_chunk_mut` 把操作**追加进同一个 `write_batch`**，循环结束后一次 `self.meta_store.write(write_batch, sync)?`。注意 `*_mut` 后缀的方法（如 `add_chunk_mut`）就是「只装填写批、不立即写」的版本，与「立即写」的 `add_chunk`（无后缀）成对出现——这是为了让多个 chunk 共享一个写批而做的拆分。

**④ `WritingChunk` 的 Drop 语义**

[alloc/writing_chunk.rs:35-50](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/writing_chunk.rs#L35-L50) 的 `Drop`：若 `commit_succ` 则从 `writing_list` 移除该 chunk；否则把对应 `WritingHolder.abort` 置 `true`（允许后续同 id 的 `update_chunk` 覆盖它，见 [engine.rs:444-451](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L444-L451)）。这保证一个 `chunk_id` 在同一时刻至多有一个在途写。

#### 4.3.4 代码实践（本讲主任务）：拆解一次 COW 写的写批

> 任务：描述一次「`update` + `commit`」中，新 chunk 元数据与新旧物理块状态如何在 RocksDB 写批中原子更新。

1. **实践目标**：能逐条列出一次 COW 写（`pos` 变化）在 `commit` 阶段产生的 `WriteBatch` 内容，并说明哪几条对应「新块」、哪几条对应「旧块」、为什么必须原子。
2. **操作步骤**：
   - 假设 chunk `C` 原本在 `pos = P_old`（块大小 S_old），一次覆盖写触发 COW，分配到新位置 `pos = P_new`（块大小 S_new），`chunk_ver` 由 1 升到 2。
   - 打开 [meta/meta_store.rs:205-271](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_store.rs#L205-L271) `move_chunk_mut`，对照下表逐条推导写批：
     | 写批操作 | 对应 | 物理块语义 |
     | --- | --- | --- |
     | `put` chunk_meta_key(C) → 新 ChunkMeta（pos=P_new, ver=2） | 新块 | chunk 内容指向新位置 |
     | `delete` pos_to_chunk_key(P_old) | 旧块 | 解除旧位置归属 |
     | `merge` group_bits_key(group_old) ← `release(index_old)` | 旧块 | 释放旧槽 |
     | `put` pos_to_chunk_key(P_new) → C | 新块 | 登记新位置归属 |
     | `merge` group_bits_key(group_new) ← `acquire(index_new)` | 新块 | 占用新槽 |
     | `merge` used_size_key(prefix) ← `(S_new − S_old)` | 新旧块 | 调整已用空间 |
     | `put` timestamp_key(ts_new, C) → [] ；`delete` timestamp_key(ts_old, C) | — | 刷新时间索引 |
     | `delete` writing_chunk_key(C) | — | 清理 intent 日志 |
   - 注意 `meta_store.rs:222` 的 `if old_meta.pos != new_meta.pos` 守卫：若本次是**就地 append**（COW 优化路径，`pos` 不变），则**跳过**上表第 2～6 行——既不释放旧槽也不占用新槽、used_size 不变，只更新 `CHUNK_META`、时间戳和清理 intent。
3. **需要观察的现象**：上表「新块」与「旧块」的操作出现在**同一个** `WriteBatch` 里，由 [engine.rs:567](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L567) 的一次 `write(batch, sync=true)` 提交。
4. **预期结果**：因为写批原子，崩溃后磁盘上绝不会出现「`CHUNK_META` 已指向 P_new，但 group 位图仍把 P_old 标记为占用、P_new 标记为空闲」的撕裂态——重启后分配器永远不会再下发 P_new，也不会误以为 P_old 还被 C 占用。请用一句话写下：「新块占用与旧块释放要么同时生效、要么同时不生效，从而物理块状态与 chunk 元数据永远一致。」
5. 验证手段：阅读 [engine.rs:854-903](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L854-L903) `test_engine_normal` 中 `chunk2`（COW，`pos.index()` 由 0→1，`ver` 1→2）与 `chunk3`（再次 COW，`index()` 1→0，`ver` 2→3）的断言，确认 COW 前后 `used_size().reserved_size` 的变化与上表 used_size 一行一致；运行命令为 `待本地验证`（形如 `cargo test -p chunk_engine --lib core::engine::tests::test_engine_normal`）。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `update_chunk` 要先 `persist_writing_chunk` 写 intent，再在 `commit` 里才写 `CHUNK_META`？能不能合并成一次写？
  - **答案**：合并成一次写就无法区分「在途」与「已提交」。拆成两阶段后，`WRITING_CHUNK` 是唯一在 commit 前落盘的记录，崩溃恢复时它就是「这个 chunk 有未提交的写、它占了某个物理位置」的唯一线索（`occupy_uncommitted_positions` 据此重新占用位置、防止被重复分配）。`CHUNK_META` 只在 commit 成功后才更新，保证「`CHUNK_META` 存在 ⇔ 该 chunk 完整可读」这一不变量。
- **练习 2**：`commit_chunks` 相比逐个 `commit_chunk` 提升吞吐的关键在哪？有没有代价？
  - **答案**：关键在 [engine.rs:537-567](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L537-L567) 把多个 chunk 的更新**合并进一个 `WriteBatch`、只 `fsync` 一次**，把昂贵的同步开销摊薄到整批。代价是必须**先一次性获取所有相关 `chunk_id` 的锁**（按排序获取以防死锁），批量越大持锁面越广；另外若批中任一 chunk 装填失败，整批都不会提交。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「写生命周期 + 崩溃恢复」的纸面推演任务：

**场景**：target T 上原本没有 chunk `X`。现在连续发生两次写：

1. 第一次写 12 字节（触发**新建**路径，落到块大小 `CHUNK_SIZE_SMALL` 的某 group 的 0 号槽，`ver=1`）。
2. 第二次写覆盖了原数据范围之外的位置、超过当前块容量（触发 **COW**，迁到 1 号槽，`ver=2`）。
3. 假设在第二次写的 `commit` **之前**进程崩溃。

**要求**：

1. 分别画出第一次写的 `add_chunk_mut` 写批、第二次写的 `move_chunk_mut` 写批，标注每条操作的前缀与「新/旧块」归属（参考 4.3.4 的表格）。
2. 解释崩溃那一刻磁盘上 `X` 的状态：`CHUNK_META` 是什么？`WRITING_CHUNK` 是什么？group 位图里 0 号、1 号槽分别是什么状态？
3. 重启后 `occupy_uncommitted_positions` 会对 1 号槽做什么？为什么这一步对「防止 1 号槽被重复分配」至关重要？
4. 如果第二次写其实是**就地 append**（`pos` 不变，仍是 0 号槽），写批会比 COW 情形少哪几条？为什么这些可以省？

**参考思路**：

1. 第一次写批见 4.3.2 的 6 步表（新块 = 0 号槽）。第二次写批见 4.3.4 的表（旧块 = 0 号槽 `release`、新块 = 1 号槽 `acquire`），并因 `pos` 变化触发全部行。
2. 崩溃时 `CHUNK_META(X)` 仍是第一次提交的值（`pos`=0 号槽、`ver=1`）；`WRITING_CHUNK(X)` 存着第二次的在途 meta（`pos`=1 号槽、`ver=2`）；由于第二次的写批**尚未提交**，group 位图里只有 0 号槽被占用、1 号槽仍空闲。
3. `occupy_uncommitted_positions` 发现 `WRITING_CHUNK(X).pos`(1 号槽) 与 `CHUNK_META(X).pos`(0 号槽) 不一致，判定为在途写，**临时 `acquire` 1 号槽**（[meta/meta_store.rs:386-396](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_store.rs#L386-L396)），避免分配器在恢复完成前把 1 号槽分给别的 chunk；待 `handle_uncommitted_chunks` 正式提交后再恢复正确占用。
4. 就地 append 时 `old.pos == new.pos`（[meta/meta_store.rs:222](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/meta/meta_store.rs#L222)），跳过旧位置 `delete`+`release`、新位置 `put`+`acquire` 以及 used_size 调整——因为物理块没换，占用状态与已用空间都不变，只需更新内容与时间戳。这正是 u6-l2 讲过的「装得下就 append、不分配新块」在元数据层的体现。

---

## 6. 本讲小结

- chunk engine 把一个 target 的**全部元数据**塞进**同一个 RocksDB**，用 **1 字节前缀**区分 8 类记录；`chunk_meta_key` 对 `chunk_id` **按位取反**，把「逻辑降序」映射成「物理前向扫描」，配合 4 MiB readahead 高效列举。
- 两类高频并发可变状态——**group 占用位图**与 **used_size 计数**——用 **merge operator** 提交增量，由 `MetaMergeOp` 折叠，**规避 read-modify-write 竞态**；merge 必须对所有产生 merge 的前缀给出确定结果，否则该 key 永久报错。
- 写路径拆成 **`update_chunk`（写 intent `WRITING_CHUNK`）+ `commit_chunk`（原子写批）**两阶段；commit 把**新 chunk 元数据 + 新块占用 + 旧块释放 + used_size + 时间戳 + 清理 intent** 全部塞进**一个 `WriteBatch`**，一次 `fsync` 原子落盘，杜绝「半应用」撕裂态。
- **批量提交** `commit_chunks` 把多个 chunk 的更新合并成一个写批、只 `fsync` 一次，是高吞吐关键；代价是需先按序获取全部相关锁。
- 崩溃后靠 `WRITING_CHUNK`（在途写 WAL）恢复：`occupy_uncommitted_positions` 临时重新占用在途写的物理位置，防止重复分配，待拿到 `chain_ver` 后再正式提交。
- 内存层面，`meta_cache`（`LockMap`）既是读缓存也是 per-chunk 写锁，commit 在锁保护下替换条目，保证读到的永远是完整已提交版本。

---

## 7. 下一步学习建议

- **回收的另一半在 allocator**：本讲只讲了 group 位图如何记录「占用/释放」。物理块真正回收到 `allocated_groups` 池、空闲 group 的 `fallocate` 回收，发生在 alloc 层——回看 u6-l2 的 `ChunkAllocator` / `GroupAllocator`，把「元数据位图」与「分配器 free-list」两条线对上。
- **FFI 边界**：阅读 [cxx.rs:143-196](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/cxx.rs#L143-L196) 的 `update_raw_chunk` / `commit_raw_chunk` / `commit_raw_chunks`，看 C++ 侧如何用裸指针 + `out_error_code` 调用本讲的 `update_chunk` / `commit_chunk` / `commit_chunks`，以及错误码到 `StorageCode` 的映射。
- **大批量删除**：阅读 [engine.rs:680-721](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L680-L721) `batch_remove`，它是「先 `query_chunks` 拿 id、再按 4096 一批组装写批、按序加锁」的又一处原子写批实践，可作为本讲内容的延伸练习。
- **配合数据恢复**：本讲的 `query_chunks_by_timestamp` 与「在途写恢复」是 u5-l5（ResyncWorker、dump-chunkmeta 对比）在单机侧的支撑点，学完 u5-l5 后回看本讲，会更清楚「chunk 元数据为什么要这么存」。
