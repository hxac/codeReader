# 物理块分配器

## 1. 本讲目标

3FS 的 chunk engine 要把数据写到 SSD。但 SSD 不是一块「想写哪就写哪」的无边空间——引擎必须先回答一个问题：**这次写入，到底落在磁盘的哪一段字节上？** 负责回答这个问题的就是 **物理块分配器（physical allocator）**。本讲聚焦 `src/storage/chunk_engine/src/alloc/` 与 `src/storage/chunk_engine/src/file/` 这两个目录，让读者学完后能够：

- 说清 **11 种固定物理块大小**（64KiB → 64MiB）是如何组织成资源池的，以及一次写入如何按大小被归类到最匹配的那一档。
- 读懂 **位图分配**：一个「组（group）」如何用 256 位位图管理 256 个块，`ChunkAllocator` 如何用 4 个「活跃级别」做到近似 best-fit 的填充。
- 理解 **写时复制（Copy-On-Write, COW）** 与 **就地 append（append in place）** 两种写策略的判定条件，以及为什么纯追加写可以做到「不分配新块、只加引用计数」。

本讲承接 u6-l1（Chunk Engine 总览与 FFI）。u6-l1 讲的是「引擎骨架与跨语言边界」，本讲往下走一层，拆开 `Engine` 里的 `allocators` 字段，看物理空间是怎么管的。 RocksDB 元数据持久化留给 u6-l3。

## 2. 前置知识

- **chunk 与 position**：回顾 u6-l1，文件数据被切成固定大小的 **chunk** 落盘。每个 chunk 在磁盘上的位置由一个 `Position` 描述，它打包了「属于哪个 chunk_size 档、落在哪个 cluster 文件、第几个 group、group 内第几个槽位」四段信息到一个 `u64` 里。
- **位图（bitmap）**：用一个 bit 表示一个资源是否被占用，`1` 表示已用、`0` 表示空闲。256 个资源只需 256 bit = 32 字节。CPU 有 `trailing_zeros`、`count_ones` 等单指令快速操作位图。
- **fallocate**：Linux 系统调用，给文件**预留**一段连续空间（分配物理磁盘块），但不写数据。3FS 用它在 SSD 数据文件里给一组 chunk 预留整片空间，避免写时临时分配带来的碎片与延迟。
- **直接 I/O（O_DIRECT）与对齐**：直接 I/O 绕过页缓存直写 SSD，但要求缓冲区地址、读写长度、文件偏移都按块大小（通常 4096）对齐。分配器选「2 的幂」的块大小，正是为了让对齐计算退化成位运算。
- **CRAQ 双版本**：回顾 u5-l3，每个 chunk 有 `updateVer`（待确认）与 `commitVer`（已提交）。本讲的分配只关心「物理块在哪」，不直接处理版本号；但 COW 时会复制旧 chunk 的元数据。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [types/constants.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/types/constants.rs) | 定义 11 档 chunk size 常量与移位参数。 |
| [alloc/allocators.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/allocators.rs) | `Allocators`：把 11 档 `Allocator` 聚成一个数组，提供按大小选档的入口。 |
| [alloc/allocator.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/allocator.rs) | `Allocator`：单档分配器，加锁包装 `ChunkAllocator`，并驱动后台 group 分配/回收任务。 |
| [alloc/chunk_allocator.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/chunk_allocator.rs) | `ChunkAllocator`：核心分配逻辑，用位图 + 4 个活跃级别管理 group。 |
| [alloc/group_allocator.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/group_allocator.rs) | `GroupAllocator`：管理「已预留但未用」「未预留」的 group，决定何时触发 fallocate。 |
| [types/group_state.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/types/group_state.rs) | `GroupState`：256 位位图，一个 group 的占用状态。 |
| [alloc/chunk.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/chunk.rs) | `Chunk`：用户持有的块句柄，含 `copy_on_write` / `safe_write` 两种写策略。 |
| [file/clusters.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/file/clusters.rs) | `Clusters`：256 个 cluster 文件，按 group 做 fallocate 预留与读写。 |
| [file/cluster.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/file/cluster.rs) | `Cluster`：单个数据文件，封装 `fallocate` 与对齐 `pread/pwrite`。 |
| [alloc/allocator_counter.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/allocator_counter.rs) | `AllocatorCounter`：原子计数器，区分「已分配 allocated」与「已预留 reserved」。 |
| [core/engine.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs) | `Engine::update_chunk`：写路径里判定 COW 还是就地 append 的总入口。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**块大小分级**（4.1）、**位图分配**（4.2）、**写时复制与就地 append**（4.3）。

### 4.1 块大小分级：Allocators 与 11 种 chunk size

#### 4.1.1 概念说明

不同写入的数据量差异巨大：一次 KVCache 读可能只有几十 KB，一次 checkpoint 刷盘可能是好几 MB。如果所有 chunk 都用同一个固定大小，要么小块浪费空间、要么大块塞不下。

3FS 的做法是**预先定义 11 档 2 的幂大小的物理块**，从 64KiB 到 64MiB，每次写入**向上取整**到能装得下的最小那一档。这样：

- 小写入用小块，不浪费 SSD 空间与元数据。
- 大写入用大块，减少元数据条目数、提升顺序写吞吐。
- 所有块大小都是 2 的幂，对齐、寻址、取整全是位运算，极快。

#### 4.1.2 核心流程

11 档大小由 `CHUNK_SIZE_SMALL = 64KiB` 不断翻倍生成，最大 `CHUNK_SIZE_ULTRA = 64MiB`，共 11 档（\(2^{16}\) 到 \(2^{26}\)）：

\[
\text{chunk\_size}_i = 64\text{KiB} \times 2^i,\quad i \in [0, 10]
\]

| 档位 i | 大小 | 常量名 |
|--------|------|--------|
| 0 | 64 KiB | `CHUNK_SIZE_SMALL` |
| 1 | 128 KiB | — |
| 2 | 256 KiB | — |
| 3 | 512 KiB | `CHUNK_SIZE_NORMAL` |
| 4 | 1 MiB | — |
| 5 | 2 MiB | — |
| 6 | 4 MiB | `CHUNK_SIZE_LARGE` |
| 7 | 8 MiB | — |
| 8 | 16 MiB | — |
| 9 | 32 MiB | — |
| 10 | 64 MiB | `CHUNK_SIZE_ULTRA` |

`Allocators` 就是这 11 档 `Allocator` 组成的一个定长数组。一次 `allocate(size)` 调用经过两步：

1. **选档**：把请求大小向上取整到 2 的幂，换算成数组下标。
2. **委托**：交给对应档位的 `Allocator` 真正分配一个块。

#### 4.1.3 源码精读

11 档常量集中定义在 [constants.rs:3-8](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/types/constants.rs#L3-L8)，`CHUNK_SIZE_SHIFT = 16` 正是 64KiB 对应的幂次（\(2^{16}\)），用作「档位下标 = 幂次 − 16」的偏移：

```rust
pub const CHUNK_SIZE_SMALL:  Size = Size::kibibyte(64);
pub const CHUNK_SIZE_NORMAL: Size = Size::kibibyte(512);
pub const CHUNK_SIZE_LARGE:  Size = Size::mebibyte(4);
pub const CHUNK_SIZE_ULTRA:  Size = Size::mebibyte(64);
pub const CHUNK_SIZE_SHIFT:  usize = 16;   // 64KiB is 2^16
pub const CHUNK_SIZE_NUMBER: usize = 11;   // from 64KiB to 64MiB
```

`Allocators` 持有一个长度固定为 11 的数组，每档一个 `Arc<Allocator>`，构造时按 `SMALL * 2^i` 依次建出，见 [allocators.rs:6-24](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/allocators.rs#L6-L24)：

```rust
pub struct Allocators {
    pub vec: [Arc<Allocator>; CHUNK_SIZE_NUMBER],
    meta_store: Arc<MetaStore>,
}
// 构造：第 i 档的 chunk_size = SMALL * (1 << i)
for i in 0..CHUNK_SIZE_NUMBER {
    let chunk_size = CHUNK_SIZE_SMALL * (1 << i);
    ...
}
```

选档逻辑在 [allocators.rs:57-67](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/allocators.rs#L57-L67) 的 `select_by_size`，它把「向上取整到 2 的幂」一步到位地算成数组下标：

```rust
pub fn select_by_size(&self, size: Size) -> Result<&Arc<Allocator>> {
    if size <= CHUNK_SIZE_SMALL {
        Ok(&self.vec[0])
    } else if size <= CHUNK_SIZE_ULTRA {
        Ok(&self.vec[size.next_power_of_two().trailing_zeros() as usize - CHUNK_SIZE_SHIFT])
    } else {
        Err(Error::InvalidArg(...))
    }
}
```

关键技巧是 `next_power_of_two().trailing_zeros() - CHUNK_SIZE_SHIFT`：例如 `512KiB = 2^19`，`trailing_zeros = 19`，下标 = `19 − 16 = 3`，正好指向 `CHUNK_SIZE_NORMAL` 档；`65KiB` 向上取整到 `128KiB = 2^17`，下标 = `17 − 16 = 1`。一个不大于 64KiB 的请求一律落到第 0 档（`size <= CHUNK_SIZE_SMALL` 分支），超 64MiB 直接报错。

#### 4.1.4 代码实践

**实践目标**：验证 `select_by_size` 的「向上取整」选档行为。

**操作步骤**：阅读 [allocators.rs:122-199](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/allocators.rs#L122-L199) 的单元测试 `test_allocators`。

**需要观察的现象**：测试里有几条关键断言——

```rust
// 请求刚好等于 SMALL(64KiB) → 第 0 档
select_by_size(CHUNK_SIZE_SMALL)        → chunk_size == CHUNK_SIZE_SMALL
// 请求 64KiB + 1 → 向上取整到 128KiB → 第 1 档（SMALL*2）
select_by_size(CHUNK_SIZE_SMALL + 1)    → chunk_size == CHUNK_SIZE_SMALL * 2
// 请求 512KiB+1 → 向上取整到 1MiB → 第 4 档（NORMAL*2）
select_by_size(CHUNK_SIZE_NORMAL + 1)   → chunk_size == CHUNK_SIZE_NORMAL * 2
// 1GiB 超过 ULTRA(64MiB) → 报错
select_by_size(Size::gibibyte(1))       → is_err()
```

**预期结果**：每档只接受「上一档大小 + 1」到「本档大小」区间的请求，区间是左开右闭。如果本地装好 Rust 工具链，可在 `src/storage/chunk_engine` 目录下执行 `cargo test --lib test_allocators` 验证。

#### 4.1.5 小练习与答案

**练习 1**：一次写入 `3 MiB` 数据，会落到第几档？块大小是多少？

**答案**：`3 MiB` 向上取整到 2 的幂是 `4 MiB = 2^22`，下标 `= 22 − 16 = 6`，即第 6 档 `CHUNK_SIZE_LARGE`，块大小 4 MiB。

**练习 2**：为什么 `select_by_size` 对 `size <= CHUNK_SIZE_SMALL` 单独走一个分支，而不是统一用公式？

**答案**：因为 `0` 或极小值的 `next_power_of_two` 行为特殊（`0u64.next_power_of_two()` 是 1，`trailing_zeros` 会算出负下标越界）。单独把 `[0, 64KiB]` 全归到第 0 档，既避开边界，又保证「再小的写也至少拿到一个 64KiB 块」。

---

### 4.2 位图分配：GroupState / ChunkAllocator / GroupAllocator

#### 4.2.1 概念说明

选定档位后，接下来要在那一档的资源池里挑出**一个具体的空闲块**。3FS 用两层结构管理：

- **group（组）**：一档资源池的最小预留单位。一个 group = **256 个连续 chunk 槽位**，用一张 256 位位图（`GroupState`）记录每个槽是否被占用。group 是 `fallocate` 的粒度——引擎一次给一整个 group 预留磁盘空间，而不是一个 chunk 一个 chunk 地预留。
- **cluster 文件**：每档有 **256 个数据文件**（`00` ~ `FF`），chunk 实际落在这些文件里。一个 group 的 256 个槽位落在其中一个 cluster 文件内。

这样设计的好处：以 group 为单位批量预留空间，大幅减少 `fallocate` 调用次数；位图让「找一个空槽」退化成几条位运算；256 这个数既是 group 内槽位数，又是 cluster 文件数，配合 2 的幂块大小，寻址全部是位运算。

#### 4.2.2 核心流程

一次 `ChunkAllocator::allocate` 的决策顺序（**优先复用、最后才扩容**）：

```text
allocate()
  │
  ├─ 1. 有「活跃 group」吗？(active_groups 非空)
  │     从最满的级别开始倒序扫描 active_levels[3..0]
  │     ├─ 找到一个 group → 在其位图里分配一个空槽(index)
  │     │   · 若该 group 满了  → 移到 full_groups
  │     │   · 若跨过级别边界 → 移到更高一级 level 集合
  │     └─ 返回 Position(group_id, index)
  │
  └─ 2. 没有空闲槽的活跃 group？
        向 GroupAllocator 要一个 group：
        ├─ 优先复用 allocated_groups（之前预留但用空、回收回来的）
        └─ 否则触发 fallocate 新预留一个 group（慢路径）
        建一个空 GroupState，分配 index 0，返回 Position
```

**4 个活跃级别**是「近似 best-fit」的关键：`LEVELS = 4`，每个 group 按已用 chunk 数分成 4 档（每档 64 个）。分配时**从最满的级别开始扫**（`for level in (0..LEVELS).rev()`），先把快满的 group 填满，再动半空的，最后才开新 group。这样能尽量**填满已有 group**，减少「每个 group 都只占一点点」的碎片。

**回收与复用**：当 group 里最后一个 chunk 被释放（`is_empty`），整个 group 不会立即还给磁盘，而是进 `GroupAllocator::allocated_groups` 这个「空 group 池」。下次分配优先从这个池里捞，避免再做 `fallocate`。

#### 4.2.3 源码精读

**256 位位图**：`GroupState` 用 `[u64; 4]`（32 字节 = 256 bit）存占用情况，见 [group_state.rs:7-18](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/types/group_state.rs#L7-L18)：

```rust
type Bits = [u64; 4];
pub struct GroupState { bits: Bits, count: u32 }
impl GroupState {
    pub const TOTAL_BITS: usize = 256;   // 8 * 32 字节
    pub const LEVELS: usize = 4;
    ...
}
```

分配一个空槽只需找第一个 `0` bit，用 `trailing_zeros` 单指令定位，见 [group_state.rs:56-68](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/types/group_state.rs#L56-L68)：

```rust
pub fn allocate(&mut self) -> Option<u8> {
    for (i, v) in self.bits.iter_mut().enumerate() {
        if let Some(mark) = NonZeroU64::new(!*v) {  // 取反后非零 = 还有空位
            let idx = mark.trailing_zeros();         // 第一个空位的 bit 号
            *v |= 1 << idx;                          // 置 1 占用
            self.count += 1;
            return Some(i as u8 * Self::ITEM_BITS + idx as u8);
        }
    }
    None
}
```

级别由已用计数换算：`level = count / (256/4) = count / 64`，见 [group_state.rs:74-76](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/types/group_state.rs#L74-L76)。

**核心分配**：`ChunkAllocator` 持有 `full_groups`、`active_groups`、`active_levels[4]`、`frozen_groups` 几个集合与一个 `GroupAllocator`，见 [chunk_allocator.rs:7-15](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/chunk_allocator.rs#L7-L15)。`allocate` 的两级查找在 [chunk_allocator.rs:97-131](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/chunk_allocator.rs#L97-L131)，先倒序扫活跃级别（填满优先），找不到再向 `group_allocator` 要新 group：

```rust
pub fn allocate(&mut self, clusters: &Clusters, allow_to_allocate: bool) -> Result<Position> {
    if !self.active_groups.is_empty() {
        for level in (0..GroupState::LEVELS).rev() {       // 从最满级别开始
            let set = &mut self.active_levels[level];
            if let Some(&group_id) = set.iter().next() {
                let state = self.active_groups.get_mut(&group_id).unwrap();
                let index = state.allocate().unwrap();     // 位图里找个空槽
                if state.is_full() { ... self.full_groups.insert(group_id); }
                else if state.level() != level as u32 { ... 移到更高 level }
                let pos = Position::new(group_id, index);
                self.reference(pos, true);                 // 引用计数 +1
                return Ok(pos);
            }
        }
    }
    // 慢路径：要一个新 group（优先复用，否则 fallocate）
    let group_id = self.group_allocator.allocate(clusters, allow_to_allocate)?;
    let state = ... GroupState::empty();
    let index = state.allocate().unwrap();
    ...
}
```

**group 的复用与扩容**：`GroupAllocator::allocate` 严格遵循「先复用空 group，再 fallocate 新 group」的顺序，见 [group_allocator.rs:29-46](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/group_allocator.rs#L29-L46)：

```rust
pub fn allocate(&mut self, clusters: &Clusters, allow_to_allocate: bool) -> Result<GroupId> {
    if let Some(&group_id) = self.allocated_groups.iter().next() {  // 1. 复用空 group
        self.allocated_groups.remove(&group_id);
        Ok(group_id)
    } else if allow_to_allocate {                                   // 2. fallocate 新 group（慢）
        let group_id = self.get_unallocated_group_id();
        let result = clusters.allocate(group_id);                   // ← 触发 fallocate
        if let Err(err) = result {
            self.unallocated_groups.insert(group_id);
            return Err(err);
        }
        self.counter.allocate_group();
        Ok(group_id)
    } else {
        Err(Error::NoSpace)                                         // 不允许扩容 → 没空间
    }
}
```

回收发生在 `ChunkAllocator::deallocate`：当 group 被清空，它被交还给 `GroupAllocator::deallocate`，塞回 `allocated_groups` 等待复用，见 [chunk_allocator.rs:173-205](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/chunk_allocator.rs#L173-L205) 与 [group_allocator.rs:48-50](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/group_allocator.rs#L48-L50)。

**fallocate 预留**：`Clusters` 持有 256 个 `Cluster` 文件，按 group 调 `fallocate`，见 [clusters.rs:17-47](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/file/clusters.rs#L17-L47)。底层 `Cluster::fallocate` 调 Linux 的 `fallocate`，`punch_hole=false` 时是预留空间、`=true` 时是打洞回收，见 [cluster.rs:43-61](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/file/cluster.rs#L43-L61)。一个 group 预留的空间大小是 `group_id.size() = chunk_size × 256`（`GroupId::COUNT = 256`，见 [group_id.rs:15](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/types/group_id.rs#L15) 与 [group_id.rs:38-40](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/types/group_id.rs#L38-L40)）。

**allocated vs reserved**：分配器区分两类计数（[allocator_counter.rs:51-87](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/allocator_counter.rs#L51-L87)）——`allocated`（已 fallocate 预留的全部块）与 `reserved`（预留了但还没被业务占用的块）。fallocate 一个 group 让两者都 +256；分出一个 chunk 给业务让 `reserved −1`（`allocate_chunk`）；回收让 `reserved +1`。这给监控提供了「物理占用量」与「可用预留量」两个口径。

#### 4.2.4 代码实践

**实践目标**：观察 group 的「填充 → 满 → 新开 group」与「回收复用」全过程。

**操作步骤**：阅读 [allocator.rs:111-202](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/allocator.rs#L111-L202) 的测试 `test_allocator`。它先连续分配 1000 个 chunk，再检查内部状态：

```rust
const N: usize = 1000;
for _ in 0..N { chunks.push(Arc::new(allocator.allocate(true).unwrap())); }

{
    let allocator = allocator.allocator.lock().unwrap();
    assert_eq!(allocator.full_groups.len(), N / 256);           // 3 个满 group
    assert_eq!(allocator.active_groups.len(), 1);               // 1 个活跃(半满)group
    assert_eq!(allocator.active_groups.iter().next().unwrap().1.count() as usize, N % 256); // 232 个槽已用
}
// ... 读写校验后 chunks.clear() 释放全部 ...
{
    let allocator = allocator.allocator.lock().unwrap();
    assert!(allocator.full_groups.is_empty());                  // 满 group 全没了
    assert!(allocator.active_groups.is_empty());               // 活跃 group 也清空（回收进 allocated_groups）
}
```

**需要观察的现象**：1000 个 chunk = 3 个满 group（3×256）+ 1 个用了 232 个槽的活跃 group。全部释放后，这些 group 都回到空 group 池（`allocated_groups`），等待复用——**下一次分配不会再触发 fallocate**。

**预期结果**：本实践为源码阅读型，断言即预期。若本地运行 `cargo test --lib test_allocator`，可在释放前后各打印一次 `allocator.group_allocator.allocated_groups.len()`，确认释放后空 group 数增加。如果暂无 Rust 环境则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`ChunkAllocator::allocate` 为什么要从 `active_levels` 的**高 level 向低 level**倒序扫描，而不是从低到高？

**答案**：高 level 表示 group 已经比较满。先填满快满的 group，能让每个 group 尽快到达 256 满、整组下线，从而把活跃的半空 group 数量压到最少。这降低了元数据分散、提升了局部性（同一文件的多个 chunk 更可能落在同一 group/cluster），是一种近似 best-fit 的碎片控制策略。

**练习 2**：一个 chunk 被释放后，它所在的 group 并不会立刻 `fallocate(PUNCH_HOLE)` 还给磁盘。这个设计带来什么好处与代价？

**答案**：好处是把回收的空 group 留在 `allocated_groups` 池里，下次分配直接复用，**避免反复 fallocate**（系统调用开销大、且易产生碎片）。代价是 SSD 空间不会立即还给操作系统，表现为 `allocated_size` 不降、只有 `reserved_size` 变化；真正归还磁盘需要走 `compact_groups` 把低利用率 group 的数据搬走、再 deallocate 整组（见 [engine.rs:117-138](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L117-L138)）。

---

### 4.3 写时复制与就地 append：Chunk::copy_on_write / safe_write

#### 4.3.1 概念说明

到这里我们只讲了「分配一个全新的空块」。但真实的写入往往是对**已有 chunk** 的修改：追加一段数据、覆盖中间一段、或截断。3FS 的 chunk 是**不可变（immutable）**的语义单位——一旦提交，数据就固定。要改它，引擎有两条路：

- **就地 append（in-place append）**：如果新数据是**纯追加**（写在已有数据末尾之后）且**装得下当前块**，就直接在原物理块上往后写。旧数据原封不动，新数据续在后面，**不需要分配新块**，只把 chunk 的引用计数加一。
- **写时复制（Copy-On-Write, COW）**：如果新数据会**覆盖已有内容**（offset 落在已有长度内）或**超出当前块容量**，就必须分配一个新块，把旧数据搬过去、再覆盖写入新数据。旧块保留给还引用它的读者，等引用归零再回收。

这个判定的意义在于：AI 负载里**追加写（append）** 极其常见（checkpoint 顺序写、日志追加），用 in-place append 能省掉一次「分配 + 整块拷贝」，把追加写的代价压到最低；只有真正的随机覆盖写才付出 COW 的代价。

#### 4.3.2 核心流程

`Engine::update_chunk` 是写路径总入口，它在拿到旧 chunk 后用一个 `match` 决定走哪条路（见下方源码）。判定条件可以归纳成一句话：

> **只要新数据与旧数据有重叠（offset < 旧长度），或装不下当前块（offset+length > 容量），或处于同步恢复期，就必须 COW；否则就地 append。**

```text
update_chunk(chunk_id, req)
  │
  ├─ get(chunk_id) 取旧 chunk（可能不存在）
  ├─ 校验链版本、算新版本号
  │
  └─ 决定写策略：
     ┌─ 旧 chunk 不存在        → allocate 全新块 + safe_write    （首次写）
     ├─ 纯追加且装得下         → clone(同块, 引用+1) + safe_write （就地 append，不分配）
     └─ 覆盖/超容量/syncing    → copy_on_write（分配新块 + 拷贝 + 写入）
```

注意「就地 append」里的 `clone` **不是复制数据**：`Chunk::clone` 只是把同一个 `Position` 的引用计数加一（`allocator.reference`），新旧 chunk 共享同一物理块，见 [chunk.rs:296-300](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/chunk.rs#L296-L300)。真正省下来的是「分配新块 + 读旧块 + 写新块」这一整套 I/O。

而 COW 分配新块时，调用的是 `allocators.allocate(Size(new_len), ...)`，会按新总长度**重新选档**——如果追加让 chunk 长到了更大的 2 的幂区间，COW 就会把它「升级」到更大的物理块（比如从 512KiB 块升级到 1MiB 块）。

#### 4.3.3 源码精读

写策略的总判定在 [engine.rs:386-434](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L386-L434)，三个分支一目了然：

```rust
let mut new_chunk = match old_chunk {
    Some(old_chunk) if req.is_remove => old_chunk.as_ref().clone(),          // 删除：仅引用
    Some(old_chunk)
        if req.is_syncing
            || (req.length > 0 && req.offset < old_chunk.meta().len)         // 覆盖已有数据
            || req.offset + req.length > old_chunk.capacity() =>             // 超出当前块容量
    {
        old_chunk.copy_on_write(...)                                         // ← COW
    }
    Some(old_chunk) => {
        let mut new_chunk = old_chunk.as_ref().clone();                      // 共享同块，引用+1
        new_chunk.safe_write(...);                                           // ← 就地 append
        new_chunk
    }
    None => {
        let mut new_chunk = self.allocators.allocate(                        // ← 全新块
            Size::from(req.offset + req.length), ...)?;
        new_chunk.safe_write(...);
        new_chunk
    }
};
```

`capacity()` 就是物理块大小，见 [chunk.rs:37-39](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/chunk.rs#L37-L39)，所以「装不下」直接对比 `offset+length` 与所在档位的 chunk_size。

**COW 实现**：`copy_on_write` 先按新长度 `allocators.allocate(Size(new_len))` 选档分配新块（可能升级到更大档），再把旧数据搬过来、写入新数据，见 [chunk.rs:89-174](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/chunk.rs#L89-L174)。它有两个关键优化：

- `skip_read`：如果新写覆盖了整个旧块（`offset==0 && data.len() >= 旧len`），就**跳过读旧块**，直接写新块、复用请求自带的 checksum，省一次读 I/O（见 [chunk.rs:112](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/chunk.rs#L112)）。
- 数据搬运用线程本地的对齐缓冲 `BUFFER`（`CHUNK_SIZE_ULTRA` 大小），保证 `pread/pwrite` 满足 `O_DIRECT` 对齐。

**就地 append 实现**：`safe_write` 进一步区分「对齐 append」与「非对齐 append」，见 [chunk.rs:176-281](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/chunk.rs#L176-L281)。当长度、偏移、缓冲都按 `ALIGN_SIZE` 对齐时，走 `safe_write_direct_append`——直接 `pwrite` 追加，并用 `crc32c_combine` **增量合并 checksum**（不必重算整块）；否则借线程本地缓冲对齐后再写（`safe_write_indirect_append`）：

```rust
if is_aligned_len(self.meta.len) && is_aligned_len(offset)
    && (data.is_empty() || is_aligned_buf(data)) {
    // 对齐快路径：直接 pwrite 追加 + crc32c_combine 增量更新 checksum
    ...
    self.pwrite(data, offset)?;
    self.meta.checksum = crc32c::crc32c_combine(self.meta.checksum, checksum, data.len());
} else if self.meta.len < offset + data.len() as u32 {
    // 非对齐：借 BUFFER 对齐后写（可能要先读 tail 凑齐）
    ...
}
```

`crc32c_combine` 让追加写的 checksum 更新成本与「新增字节数」成正比，而不是与「整块大小」成正比——这对高频小追加是决定性的性能优势。

#### 4.3.4 代码实践

**实践目标**：给定三种写入场景，预测 `update_chunk` 走哪个分支、是否分配新块。

**操作步骤**：假设一个已提交的 chunk，`meta().len = 100000`，`capacity() = 131072`（128KiB 档）。针对下列三次 `UpdateReq`，判断 `engine.rs:386` 的 `match` 走哪个分支：

| 场景 | offset | length | is_syncing | 走哪条路？是否分配新块？ |
|------|--------|--------|-----------|------------------------|
| A | 100000 | 4096 | false | ? |
| B | 50000 | 4096 | false | ? |
| C | 0 | 131073 | false | ? |

**需要观察的现象**：对照判定条件 `is_syncing || (length>0 && offset < len) || (offset+length > capacity)` 逐一代入。

**预期结果**：

- **A**：`offset(100000) < len(100000)`? 否（相等不是小于）；`offset+length = 104096 > 131072`? 否 → 不满足 COW 条件 → 走 `Some(old_chunk)` 的第三分支，**就地 append**（clone 同块 + safe_write），**不分配新块**。
- **B**：`offset(50000) < len(100000)`? **是** → 满足 COW 条件 → **COW**，分配新块并拷贝。
- **C**：`offset+length = 131073 > capacity(131072)`? **是** → **COW**，且 `allocate(Size(131073))` 会向上取整到 256KiB 档，**升级到更大的物理块**。

若本地有环境，可参考 [engine.rs:1181](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L1181) 附近的测试 `test_safe_write` / `test_copy_on_write` 编写最小用例验证；否则标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Chunk::clone` 不会真的复制磁盘数据，却能让多个持有者安全并发读？

**答案**：`clone` 调 `allocator.reference`，只把该 `Position` 的引用计数 `+1`（记录在 `position_rc`）。所有克隆共享同一个不可变的物理块。只要引用计数 > 0，分配器就不会把该槽位重新分给别人（`deallocate` 只在 `rc == 0` 时才回收）。这是「读拿引用、写产新块」的无冲突并发模型——读者持有的旧块永不被原地修改，写者总是产出新块。

**练习 2**：一次「覆盖整个旧块」的写入（`offset=0, length=旧len`）会触发完整 COW（读旧块再写）吗？

**答案**：不会读旧块。它满足 COW 的触发条件（`offset < len` 为假，但通常 `length >= len` 走 `copy_on_write`），但 `copy_on_write` 内部会算出 `skip_read = (offset==0 && data.len() >= self.meta.len)` 为真，于是**跳过读旧块**，直接把新数据写进新块，并复用请求携带的 checksum（`checksum_reuse` 计数 +1）。只有部分覆盖才需要先读旧块凑齐（`copy_on_write_read_times` 计数）。

---

## 5. 综合实践

**任务**：完整描述一次 chunk 写入从「选块大小」到「落盘」的全过程，重点回答三个问题——选档、复用、扩容。

**背景数据**：假设某 target 的 64KiB 档（第 0 档）资源池当前状态为：有 2 个 group 已经 fallocate 预留但全空（在 `allocated_groups` 池里），没有任何活跃 group；此时业务发起一次 `length = 70000` 字节、`offset = 0` 的全新写入（旧 chunk 不存在），且 `allow_to_allocate = true`。

**要求按顺序回答**：

1. **选档**：`Allocators::select_by_size(70000)` 会落到第几档？块大小是多少？（提示：`70000` 向上取整到 2 的幂。）
2. **复用**：`ChunkAllocator::allocate` 发现没有活跃 group，转向 `GroupAllocator::allocate`。它会先从哪里取 group？是否会立即触发 `fallocate`？
3. **扩容**：假设接下来业务**不断追加**该 chunk，直到总长度超过当前块容量。`Engine::update_chunk` 的哪个条件会被触发？`copy_on_write` 里的 `allocators.allocate(Size(new_len))` 会让块「升级」到哪一档？

**参考答案**：

1. `70000` 向上取整到 2 的幂 = `128KiB (2^17)`，下标 = `17 − 16 = 1`，即**第 1 档（128KiB）**。注意：第 0 档的 2 个空 group 用不上，因为它们属于 64KiB 档，**各档资源池互相隔离**。
2. 第 1 档此时 `allocated_groups` 为空（题目说的 2 个空 group 属于第 0 档），`allow_to_allocate = true`，所以 `GroupAllocator::allocate` 走慢路径：`get_unallocated_group_id()` 取一个新 group id，调 `clusters.allocate(group_id)` **触发 `fallocate`** 预留 128KiB × 256 = 32MiB 空间，然后建空 `GroupState`、分配 index 0。若第 1 档恰好有空 group 在池里，则**直接复用、不 fallocate**。
3. 追加超过 128KiB 容量时，`req.offset + req.length > old_chunk.capacity()`（128KiB）为真 → 触发 **COW**。`copy_on_write` 调 `allocators.allocate(Size(new_len))`，按新长度重新选档——例如新长度 200KiB 会取整到 **256KiB（第 2 档）**，块从 128KiB **升级**到 256KiB，旧 128KiB 块等引用归零后回收进第 1 档的空 group 池。

**延伸观察（可选）**：在 [engine.rs:140-158](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L140-L158) 的 `start_allocate_workers` 可以看到，引擎会起后台 worker 周期性调用 `allocate_groups(1, 2, 2, false)`，**提前**把空 group 预留好（保持 `allocated_groups` 里至少有 1~2 个空 group）。这样业务的正常分配几乎总走「复用」快路径，`fallocate` 的延迟被后台 worker 吸收掉了——这就是为什么热路径上几乎看不到 `allocate group slow path` 日志。

## 6. 本讲小结

- **11 档分级**：物理块有 64KiB → 64MiB 共 11 个 2 的幂大小，`Allocators` 用一个长度 11 的数组管理；`select_by_size` 把请求大小向上取整成数组下标，一步选档。
- **group = 256 个槽**：一档资源池以 group 为 fallocate 单位，每个 group 用 `GroupState` 这张 256 位位图管理 256 个 chunk 槽位，分配/回收都是位运算。
- **4 级填充 + 优先复用**：`ChunkAllocator` 用 4 个活跃级别，从最满的 group 倒序填充（近似 best-fit）；空 group 回收到 `allocated_groups` 池，下次分配先复用、最后才 fallocate。
- **双口径计数**：`allocated`（已预留）与 `reserved`（预留未用）分开统计，fallocate 一个 group 两者各 +256。
- **就地 append vs COW**：纯追加且装得下 → clone 同块（引用+1）+ `safe_write` 原块追加，**不分配新块**；覆盖或超容量 → `copy_on_write` 分配新块（可能升级到更大档）并拷贝。
- **checksum 增量**：对齐 append 用 `crc32c_combine` 增量合并、COW 全覆盖用 `skip_read` 复用请求 checksum，把校验开销压到最低。

## 7. 下一步学习建议

- 本讲只讲了「块在哪、怎么分」，**没讲元数据怎么持久化**。分配结果（`Position`、`GroupState` 位图）靠崩溃后从 RocksDB 重建。建议下一讲学习 **u6-l3（Chunk 元数据与 RocksDB）**，看 `ChunkAllocator::load`（[chunk_allocator.rs:31-95](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/alloc/chunk_allocator.rs#L31-L95)）是怎么从 `group_bits` 前缀的 KV 还原出 `full_groups / active_groups / allocated_groups` 的。
- 若想了解 COW 产出的新块如何与旧块在 **CRAQ 版本号** 上对齐，可回顾 u5-l3（写路径与 CRAQ）的 `commitVer / updateVer` 双版本不变量。
- 想看真实负载下分配器的行为，可阅读 [chunk_engine/benches/bench_allocator.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/benches/bench_allocator.rs) 与 [examples/chunk_viewer.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/examples/chunk_viewer.rs)，前者做分配性能基准、后者可视化 group 占用。
