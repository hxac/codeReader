# 快照与恢复（Snapshot & Restore）

> 本讲是单元 7「高可用」的第 2 讲，依赖 [u7-l1 高可用：Leader 选举与热备](u7-l1-ha-leader-standby.md)。
> u7-l1 讲了「主是谁、主备之间怎么复制 OpLog」，本讲专门回答：「主的内存元数据如何落到磁盘/对象存储，崩了之后怎么选一个有效快照恢复，以及快照与 OpLog 之间的边界如何保证恢复后数据一致」。

## 1. 本讲目标

学完本讲后，你应当能够：

1. 说清楚 Master 内存里的元数据（`metadata_shards_`、段信息、任务管理器）是如何被序列化成一堆字节、又是如何被反序列化回来的。
2. 区分两类后端：**快照目录后端（Catalog，embedded / redis）**负责「有哪些快照、最新的是哪个」，**对象存储后端（Object Store，local / s3）**负责「真正存放大体积 payload 文件」，并能说出二者如何组合。
3. 复述一次周期性快照的完整流程：什么时候触发、为什么用 `fork()` 子进程、payload 写到哪里、`latest` 指针怎么推进。
4. 解释「OpLog 边界（`last_included_seq`）」的含义，以及它在两条恢复路径（Master 重启直接恢复、热备加载快照后回放 OpLog）中分别扮演什么角色。
5. 描述重启/热备时，系统如何「从多个候选快照里挑出一个有效快照」，以及挑不出来时会怎样。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 为什么 Master 需要快照

Mooncake Store 的 Master 把所有对象的元数据（key 属于哪个租户、副本在哪些 segment 上、引用计数、租约、任务……）都放在**内存**里。内存查询快，但一旦进程崩溃或重启，内存就全没了。客户端重新 mount segment、重新 Put 当然能慢慢重建，但对于存了大量对象的集群，重建过程既慢又会丢失「哪些对象还存在」这层信息。

因此 Master 提供了一条「把内存状态定期落盘 → 重启时读回来」的通路，这就是本讲的**快照与恢复**。

### 2.2 快照 + OpLog：经典的「全量 + 增量」组合

如果你读过 u7-l1，会知道主备之间用 **OpLog（操作日志）**做增量复制。但只靠 OpLog 有两个问题：

- 日志会无限增长，必须定期「截断」。
- 一个新上线/重启的节点，如果从第 0 条日志开始回放，代价巨大。

经典解法是 **全量快照 + 增量日志**：

```
[ ...... 全量快照 S，其内容覆盖到第 N 条操作 ...... ][ 第 N+1 条操作 ][ N+2 ]...
```

恢复时分两步：先把快照 S 读进来（得到「截至第 N 条」的状态），再回放 `seq > N` 的 OpLog 补齐增量。这里 **N 就是「快照的 OpLog 边界」**，本讲会反复提到它。代码里它叫 `last_included_seq`。

### 2.3 两个看似相似的「恢复」，其实消费者不同

本讲会看到两套「读快照」的代码，务必分清：

| 路径 | 触发场景 | 入口函数 | 是否回放 OpLog |
| --- | --- | --- | --- |
| **Master 重启恢复** | Master 自身进程重启（`enable_snapshot_restore`） | `MasterService::RestoreState()` | 否，直接把快照灌进自己的内存结构 |
| **热备一致性恢复** | Standby 成为新主 / 接管前对齐 | `HotStandbyService` 经由 `CatalogBackedSnapshotProvider::LoadLatestSnapshot()` | 是，以 `last_included_seq` 为起点回放 |

前者关心的是「我能不能把状态原样读回来」，后者额外关心「快照之后主上又写了几条日志，我得补上」。**OpLog 边界只在第二条路径里真正起作用**，但它的「取值」是在生成快照时就定好的。

### 2.4 关键术语速查

- **`SnapshotId`**：快照的唯一标识，由时间戳生成，形如 `20260620_143012_007`（`YYYYMMDD_HHMMSS_mmm`）。因为格式固定，**字典序就是创建顺序**，这一点在「挑最新快照」时被反复利用。
- **`OpLogSequenceId`**：OpLog 条目的单调递增序号，本质是 `uint64_t`，从 1 开始；`0` 是哨兵值，表示「没有持久化的 OpLog 边界」。
- **`ViewVersionId` / `producer_view_version`**：主所属的「任期版本」（见 u7-l1 的 Leader 选举），快照会记录它是被哪个任期产生的。

## 3. 本讲源码地图

本讲涉及的核心文件如下（均在 `mooncake-store/` 下）：

| 文件 | 作用 |
| --- | --- |
| `include/master_service.h` | `MasterService` 类。声明了快照/恢复的私有方法、内嵌的 `MetadataSerializer`，以及快照相关的成员变量。 |
| `src/master_service.cpp` | 快照/恢复的全部实现：`SnapshotThreadFunc`、`PersistState`、`RestoreState`、`MetadataSerializer::Serialize/Deserialize` 等。**本讲的主力阅读对象**。 |
| `include/ha/ha_types.h` | 定义 `SnapshotDescriptor`（快照描述符）、`OpLogSequenceId`、`SnapshotId` 等核心类型。 |
| `include/ha/snapshot/catalog/snapshot_catalog_store.h` | **快照目录后端**的抽象接口 `SnapshotCatalogStore`，以及路径拼接/描述符序列化的工具函数。 |
| `src/ha/snapshot/catalog/backends/embedded/embedded_snapshot_catalog_store.cpp` | 目录后端实现之一：**embedded**——把目录元数据也存进对象存储。 |
| `include/ha/snapshot/catalog/backends/redis/redis_snapshot_catalog_store.h` | 目录后端实现之二：**redis**——把目录索引/最新指针存进 Redis。 |
| `include/ha/snapshot/object/snapshot_object_store.h` | **对象存储后端**的抽象接口 `SnapshotObjectStore`，以及 `local`/`s3` 类型解析。 |
| `src/ha/snapshot/object/backends/local/local_file_snapshot_object_store.cpp` | 对象存储后端实现之一：**local**——落到本地文件系统。 |
| `include/ha/snapshot/snapshot_logger.h` | 子进程专用的「管道日志」宏（`SNAP_LOG_*`），避免 fork 后 glog 死锁。 |
| `src/ha/snapshot/catalog_backed_snapshot_provider.cpp` | 热备侧的快照消费者：`LoadLatestSnapshot()`，把快照读成 `LoadedSnapshot` 基线。 |
| `src/hot_standby_service.cpp` | 热备恢复：`LoadSnapshotBaselineLocked()` 展示「快照基线 + OpLog 回放」如何衔接。 |
| `include/master_config.h` / `include/types.h` | 快照相关配置项与默认值（间隔、超时、保留份数等）。 |

---

## 4. 核心概念与源码讲解

本讲拆成 5 个模块：4.1 元数据序列化器 → 4.2 多后端存储 → 4.3 周期性快照（子进程） → 4.4 重启恢复与有效快照选择 → 4.5 OpLog 边界与一致性。

### 4.1 MetadataSerializer：把内存元数据变成字节

#### 4.1.1 概念说明

`MetadataSerializer` 是 `MasterService` 的**内嵌类**（定义在 `master_service.h` 内），它的职责很纯粹：把 Master 内存里的元数据结构**序列化成一段字节**（用于写快照），以及把这段字节**反序列化回内存结构**（用于恢复）。

注意它只管「元数据」这一块。一次完整快照其实有 **三块 payload**：

- `metadata`：对象元数据（由 `MetadataSerializer` 负责，本模块的主角）。
- `segments`：段（segment）信息，由 `SegmentSerializer` 负责。
- `task_manager`：任务管理器状态，由 `TaskManagerSerializer` 负责。

本模块聚焦 `MetadataSerializer`，另外两块的套路几乎一样。

#### 4.1.2 核心流程

**序列化**把内存结构打包成一个顶层 MessagePack map，包含 3 个字段：

```text
顶层 map (3 个键):
├── "shards"          → map{ shard_idx(整数) → zstd 压缩后的 shard 二进制 }
│                         每个 shard 内部 = { tenant → [对象元数据...] }
├── "discarded_replicas" → 已丢弃但仍在 TTL 内的副本（用于恢复后还能正确释放）
└── "replica_next_id" → Replica::next_id_（全局副本 ID 计数器，恢复后要接续）
```

设计要点：

1. **按 shard 独立压缩**：1024 个 shard 各自序列化成独立小缓冲，再分别 zstd 压缩（level=3），最后以二进制挂进顶层 map。空 shard 直接跳过。这样既压缩了体积，也让单个 shard 的损坏不致于让整份快照不可读。
2. **排序保证一致性**：同一个 shard 内，按 `(tenant_id, key)` 排序后再写，保证「同样的内存状态 → 同样的字节」，便于比对/测试。
3. **`replica_next_id` 必须一起存**：否则恢复后新生成的副本 ID 会和已存在的撞车。

**反序列化**是逆过程：解包顶层 map，逐 shard 解压、重建，最后恢复 `replica_next_id` 并调用 `RebuildGroupRoutingIndex()` 重建组路由索引。

**`Reset()`** 用于「恢复失败回滚」：把所有 shard、组路由、丢弃副本清空，`replica_next_id` 复位为 1。

#### 4.1.3 源码精读

`MetadataSerializer` 的声明（构造时持有 `MasterService*`，三个核心方法 `Serialize/Deserialize/Reset`）：

[mooncake-store/include/master_service.h:1583-1622](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1583-L1622) — 内嵌序列化器类，把内存元数据与字节互转。

序列化的主体（顶层 3 字段 map、按 shard 压缩、记录 `replica_next_id`）：

[mooncake-store/src/master_service.cpp:6163-6238](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L6163-L6238) — `MetadataSerializer::Serialize()`：统计非空 shard、各自 zstd 压缩后挂进顶层 map。

其中单 shard 的序列化，注意它先收集再 **排序**：

[mooncake-store/src/master_service.cpp:6382-6434](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L6382-L6434) — `SerializeShard()`：把 `(tenant_id, key, metadata)` 三元组排序后逐条写出，保证可重复的序列化顺序。

反序列化：解包顶层 map、逐 shard 解压重建，并校验 shard 索引范围、字段是否齐全：

[mooncake-store/src/master_service.cpp:6240-6363](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L6240-L6363) — `MetadataSerializer::Deserialize()`：恢复 shard、`discarded_replicas`、`replica_next_id`，最后重建组路由。

`Reset()`（恢复失败时调用，清空一切并复位计数器）：

[mooncake-store/src/master_service.cpp:6365-6380](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L6365-L6380) — `MetadataSerializer::Reset()`：清空 shard/组路由/丢弃副本，`replica_next_id` 复位为 1。

> 小贴士：`SerializationError` 是贯穿快照流程的错误类型，携带 `ErrorCode` + `message`，用 `tl::expected<T, SerializationError>` 在「成功返回数据」与「失败返回原因」之间二选一，避免异常作为正常控制流。

#### 4.1.4 代码实践（源码阅读型）

**目标**：用一张图记住 MessagePack 的嵌套结构，并验证「排序」这一设计。

**步骤**：

1. 打开 `MetadataSerializer::Serialize()`（上面第二个链接），在纸上画出顶层 map 的 3 个键。
2. 进入 `SerializeShard()`，确认每个对象被写成 `[tenant_id, key, metadata_object]` 的三元组数组。
3. 找到 `std::sort(...)` 那段（按 `tenant_id` 再按 `key` 排序），思考：如果**不排序**，对「同一份内存」两次快照得到的字节会相同吗？

**需要观察的现象**：排序保证了「确定性序列化（deterministic serialization）」——只要内存内容相同，字节就相同。这在测试里被用来对比「快照→恢复→再快照」前后的字节是否一致。

**预期结果**：你应当能复述出「顶层 3 字段 / shard 级独立压缩 / 三元组排序」这三层结构。

> 待本地验证：若你能在本机构建并跑通 `mooncake-store/tests/ha/snapshot/` 下的测试，可在 `SerializeShard` 的 `std::sort` 前后各 dump 一次 buffer 大小，观察排序对体积无影响（只影响顺序）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `replica_next_id` 必须和元数据一起持久化？如果漏存会怎样？

> **参考答案**：`Replica::next_id_` 是全局单调递增的副本 ID 生成器。若恢复后从 1 重新开始，新分配的副本 ID 会和快照里已有的旧副本 ID 撞车，导致「按 ID 找副本」错乱。所以恢复时要把 `next_id_` 直接 `store()` 回去接续。

**练习 2**：`Reset()` 会在什么场景被调用？为什么恢复失败后必须 Reset？

> **参考答案**：在 `RestoreState` 逐个尝试候选快照、以及 `TryRestoreStateFromSnapshot` 内部失败时都会调 `Reset()`（见 4.4）。因为反序列化是「半途写内存」的，某个快照可能已经往 `metadata_shards_` 写了一部分才失败，不 Reset 就会让下一个候选快照建立在「脏」内存上，产生混合状态。

---

### 4.2 多后端存储：快照目录（Catalog）与对象存储（Object Store）

#### 4.2.1 概念说明

Mooncake 把「存快照」这件事拆成了**两个正交维度**，这是本讲最容易混淆、也最值得理解的设计：

- **对象存储后端（Object Store）**：存「大块 payload」——`metadata`、`segments`、`task_manager`、`manifest.txt` 这些动辄几 MB～几十 MB 的文件。实现有 **`local`（本地文件系统）** 和 **`s3`** 两种。接口是 `SnapshotObjectStore`。
- **快照目录后端（Catalog Store）**：存「有哪些快照、最新的是哪个、每个快照的描述符」。它很轻量，是个「目录/索引」。实现有 **`embedded`（把目录也写进对象存储）** 和 **`redis`（把目录写进 Redis）** 两种。接口是 `SnapshotCatalogStore`。

> 一句话区分：**Catalog 回答「有哪些、最新的谁」，Object Store 回答「内容字节在哪」**。

为什么分开？因为这两个维度的访问模式完全不同：payload 是「整存整取、大块、低频」，适合对象存储；目录是「频繁读 latest 指针、需要原子推进」，适合一个支持 CAS/事务的索引（Redis）或一个约定俗成的「latest 文件」（embedded）。

#### 4.2.2 核心流程

**对象存储后端**：抽象接口 `SnapshotObjectStore` 提供统一方法，`local`/`s3` 各自实现：

```text
SnapshotObjectStore::Create(type)         // 工厂：按 "local"/"s3" 选实现
├── UploadBuffer(key, bytes) / DownloadBuffer(key, &bytes)
├── UploadString(key, str) / DownloadString(key, &str)
├── ListObjectsWithPrefix(prefix, &keys)
└── DeleteObjectsWithPrefix(prefix)
```

- `local`：把 key 当作相对路径，落在 `MOONCAKE_SNAPSHOT_LOCAL_PATH` 指定的目录下；带「路径不能逃出 base 目录」的安全校验。
- `s3`：包装 `S3Helper`，把 key 当作对象 key。仅在编译时开启 `HAVE_AWS_SDK` 才可用。

**快照目录后端**：抽象接口 `SnapshotCatalogStore` 只有四个方法：

```text
Publish(descriptor)            // 发布一个新快照，并把它标记为 latest
GetLatest()  → optional<desc>  // 读「最新」快照描述符
List(limit)  → vector<desc>    // 列出快照（按时间倒序）
Delete(snapshot_id)            // 删除一个快照（并在删的是 latest 时重选次新）
```

- `embedded`：`descriptor.txt`（每个快照一份，内容是 `last_included_seq|producer_view_version|created_at_ms`）+ `latest.txt`（内容是最新 `snapshot_id`），都落在**对象存储**里。
- `redis`：`latest` 指针和快照索引（`index_key`）放在 **Redis**；payload 仍走对象存储。

**目录布局**（embedded，以 `cluster_id = "mooncake_cluster"` 为例）：

```text
mooncake_master_snapshot/mooncake_cluster/
├── latest.txt                         ← 内容 = 最新 snapshot_id
├── 20260620_143012_007/
│   ├── descriptor.txt                 ← "last_included_seq|view_version|created_at_ms"
│   ├── manifest.txt                   ← "messagepack|1.0.0|20260620_143012_007"
│   ├── metadata                       ← MetadataSerializer 的输出（zstd 压缩）
│   ├── segments
│   └── task_manager
└── 20260620_144012_003/  ...          ← 上一份，受保留份数控制
```

> 路径根 `mooncake_master_snapshot/{cluster_id}/` 由 [BuildSnapshotRoot](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/snapshot/catalog/snapshot_catalog_store.h#L29-L37) 生成，`snapshot_id` 格式由 [IsValidSnapshotId](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/snapshot/catalog/snapshot_catalog_store.h#L43-L62) 校验（必须 19 位 `YYYYMMDD_HHMMSS_mmm`）。

#### 4.2.3 源码精读

**对象存储接口与类型解析**：

[mooncake-store/include/ha/snapshot/object/snapshot_object_store.h:62-141](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/snapshot/object/snapshot_object_store.h#L62-L141) — `SnapshotObjectStore` 抽象接口；上方 [L19-L40](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/snapshot/object/snapshot_object_store.h#L19-L40) 的 `ParseSnapshotObjectStoreType` 把 `"local"`/`"s3"` 解析成枚举，`s3` 在未编译 AWS SDK 时直接抛异常。

**local 实现的初始化（必须设置环境变量）**：

[mooncake-store/src/ha/snapshot/object/backends/local/local_file_snapshot_object_store.cpp:19-49](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/snapshot/object/backends/local/local_file_snapshot_object_store.cpp#L19-L49) — 构造时读 `MOONCAKE_SNAPSHOT_LOCAL_PATH`，建目录并 canonical 化作为 base。

**local 实现的上传（带路径越界防护）**：

[mooncake-store/src/ha/snapshot/object/backends/local/local_file_snapshot_object_store.cpp:119-154](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/snapshot/object/backends/local/local_file_snapshot_object_store.cpp#L119-L154) — `UploadBuffer`：校验路径在 base 之内 → 建父目录 → 二进制写文件。

**目录后端接口**：

[mooncake-store/include/ha/snapshot/catalog/snapshot_catalog_store.h:145-161](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/snapshot/catalog/snapshot_catalog_store.h#L145-L161) — `SnapshotCatalogStore` 纯虚接口：`Publish/GetLatest/List/Delete/GetSnapshotRoot`。描述符的「文本序列化」见上方 [L113-L141](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/snapshot/catalog/snapshot_catalog_store.h#L113-L141)（`last_included_seq|view_version|created_at_ms`）。

**embedded 实现：Publish（写 descriptor + 推进 latest）**：

[mooncake-store/src/ha/snapshot/catalog/backends/embedded/embedded_snapshot_catalog_store.cpp:50-77](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/snapshot/catalog/backends/embedded/embedded_snapshot_catalog_store.cpp#L50-L77) — 先 `UploadString(descriptor.txt)`，再 `UploadString(latest.txt = snapshot_id)`，两步都成功才算发布。

**embedded 实现：List（靠 `snapshot_id` 字典序倒序）**：

[mooncake-store/src/ha/snapshot/catalog/backends/embedded/embedded_snapshot_catalog_store.cpp:124-184](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/snapshot/catalog/backends/embedded/embedded_snapshot_catalog_store.cpp#L124-L184) — 用 `std::set<SnapshotId, std::greater<>>` 把 `snapshot_id` 排成倒序，再逐个读 descriptor。

**embedded 实现：Delete（删的是 latest 时要重选次新）**：

[mooncake-store/src/ha/snapshot/catalog/backends/embedded/embedded_snapshot_catalog_store.cpp:186-246](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/snapshot/catalog/backends/embedded/embedded_snapshot_catalog_store.cpp#L186-L246) — 删除某快照的前缀；若删的恰是 latest，则从列表里挑次新重写 `latest.txt`，否则清空 latest。

**redis 实现（索引与 latest 指针放 Redis，payload 仍走对象存储）**：

[mooncake-store/include/ha/snapshot/catalog/backends/redis/redis_snapshot_catalog_store.h:13-46](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/snapshot/catalog/backends/redis/redis_snapshot_catalog_store.h#L13-L46) — `RedisSnapshotCatalogStore`：成员里有 `latest_key_`、`index_key_`（都在 Redis），但仍持有 `object_store_` 用于 payload。

**两个后端如何被组装**：`MasterService` 构造时根据配置创建它们：

[mooncake-store/src/master_service.cpp:183-202](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L183-L202) — `enable_snapshot || enable_snapshot_restore` 时，先建 `snapshot_object_store_`，再建 `snapshot_catalog_store_`；`enable_snapshot_restore` 时立即调用 `RestoreState()`。

[mooncake-store/src/master_service.cpp:358-392](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L358-L392) — `CreateSnapshotCatalogStore()`：按 `snapshot_catalog_store_type_` 选 embedded/redis（类型解析在 [L69-L80](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L69-L80) `ParseSnapshotCatalogKind`，空串/`"embedded"`/`"payload"` 都算 embedded）。

#### 4.2.4 代码实践（源码阅读型）

**目标**：体会「两个维度正交」，并搞清楚 embedded 与 redis 各把什么数据放哪。

**步骤**：

1. 对照接口：`SnapshotObjectStore`（7 个方法，都是「按 key 存取字节」）vs `SnapshotCatalogStore`（4 个方法，都是「快照级别的元数据」）。注意**前者完全不知道「快照」是什么**，它只是个 KV 字节存储；「快照语义」是后者叠加出来的。
2. 分别打开 `EmbeddedSnapshotCatalogStore::Publish` 和 `RedisSnapshotCatalogStore` 的声明，回答：embedded 把 `latest.txt` 写在哪？redis 把等价信息写在哪？
3. 思考：为什么 `RedisSnapshotCatalogStore` 仍要持有 `SnapshotObjectStore*`？

**需要观察的现象**：embedded 的目录元数据（`descriptor.txt`/`latest.txt`）和你写快照 payload 用的是**同一个对象存储**；redis 的目录元数据走 Redis，但 payload 仍走对象存储——所以 redis 后端必须持有对象存储指针来读写 payload。

**预期结果**：你能填出下表。

| 后端维度 | 选项 | 存放 `metadata/segments/...` payload | 存放 `latest` 指针 / 描述符 |
| --- | --- | --- | --- |
| Object Store | local | 本地文件系统 | （embedded 模式下）也在此 |
| Object Store | s3 | S3 | （embedded 模式下）也在此 |
| Catalog | embedded | — | 写进 Object Store（latest.txt/descriptor.txt） |
| Catalog | redis | — | 写进 Redis（latest_key/index_key） |

#### 4.2.5 小练习与答案

**练习 1**：为什么 `embedded` 后端「列出快照」要靠 `snapshot_id` 的字典序，而不是存一个单独的列表文件？

> **参考答案**：`snapshot_id` 是 `YYYYMMDD_HHMMSS_mmm` 格式，定长且字典序 == 时间序。这让 `ListObjectsWithPrefix` 出来的 key 天然带顺序，`std::set<..., greater<>>` 一排就得到倒序。省去维护一个容易和实际文件不一致的「列表文件」，符合对象存储「最终一致、无原子目录」的特性。

**练习 2**：删除当前 latest 快照后，`embedded::Delete` 做了什么额外动作？为什么？

> **参考答案**：它会从剩余快照里挑出次新的，重写 `latest.txt` 指向它；如果一个不剩就清空 `latest.txt`。否则 `latest` 会指向一个已删除的快照，导致 `GetLatest` 读到失效描述符。

---

### 4.3 周期性快照：SnapshotThreadFunc 子进程 fork 全流程

#### 4.3.1 概念说明

「周期性快照」由一个后台线程 `SnapshotThreadFunc` 驱动。它的核心技巧是 **`fork()` 一个子进程来做序列化**，理由是：

- 序列化 + zstd 压缩 + 上传对象存储是**慢操作**（可能数秒到数十秒）。
- 如果让主线程边持锁边做，会长时间阻塞所有写请求。
- `fork()` 利用操作系统的 **写时复制（COW）**，瞬间「冻结」当前内存作为一致性快照点；子进程慢慢序列化它的那份 COW 副本，主进程继续对外服务，互不阻塞。

注意：fork 时只复制**调用线程**。其他线程持有的锁（比如 glog 内部锁）在子进程里状态未定义，所以子进程**不能用 glog**，必须用专门的「管道日志」（见 4.3.3）。

#### 4.3.2 核心流程

一次周期循环（默认每 `600s` 一次，见 [types.h:99-105](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/types.h#L99-L105)）：

```text
SnapshotThreadFunc (主进程后台线程, 每 snapshot_interval_seconds_ 唤醒)
│
├─ 0. 若 !enable_snapshot_ → 跳过本轮
│
├─ 1. snapshot_id = FormatTimestamp(now)         # 生成 "YYYYMMDD_HHMMSS_mmm"
├─ 2. 建 log_pipe[2]                              # 给子进程传日志
├─ 3. descriptor = BuildSnapshotDescriptor(...)   # ★ 在 fork 前就定好 OpLog 边界
│       └─ ResolveSnapshotSequenceId() → last_included_seq
│       └─ producer_view_version = view_version_
│
├─ 4. lock(snapshot_mutex_) → fork() → unlock     # ★ fork 发生在持锁期间
│
├─【父进程】close 写端 → WaitForSnapshotChild(pid, ...)
│       └─ 轮询 waitpid(WNOHANG) + 读管道日志 → HandleChildExit / HandleChildTimeout
│
└─【子进程】close 读端，g_snapshot_log_pipe_fd = 写端
        └─ PersistState(descriptor)
              ├─ MetadataSerializer(this).Serialize()  → metadata 字节
              ├─ SegmentSerializer(...).Serialize()     → segments 字节
              ├─ TaskManagerSerializer(...).Serialize() → task_manager 字节
              ├─ UploadSnapshotPayloadFile(metadata/segments/task_manager/manifest)
              ├─ snapshot_catalog_store->Publish(descriptor)   # 推进 latest
              └─ CleanupOldSnapshot(retention_count)           # 删旧快照
        └─ _exit(0 / 1)
```

关键点：

1. **何时触发**：后台线程定时睡眠 `snapshot_interval_seconds_`（默认 600s）；且仅当 `enable_snapshot_` 为真、且内存分配器是 `OFFSET` 类型时才启动该线程（见 [L343-L349](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L343-L349)）。
2. **OpLog 边界在 fork 前确定**：`BuildSnapshotDescriptor` 先于 fork 调用，里面 `ResolveSnapshotSequenceId()` 拿到「当前最新 OpLog 序号」。这个顺序很重要，4.5 会展开。
3. **fork 在持锁期间**：`snapshot_mutex_` 是个读写锁，fork 时持**写锁**，目的是和「创建 copy/move/drain 任务」这类持**读锁**的操作互斥（如 `CreateCopyTask` 入口取共享锁），保证 fork 瞬间没有这类任务在改结构。fork 返回后父进程立刻释放锁，**不阻塞**正常读写。
4. **父子协作**：父进程通过 `waitpid` 非阻塞轮询、读管道日志转发成 `[Snapshot:Child]`；超时（默认 300s）先 `SIGTERM` 再 `SIGKILL`。
5. **payload 写哪个后端**：`UploadSnapshotPayloadFile` 调用 `snapshot_object_store_->UploadBuffer`（即 4.2 的对象存储后端）；`Publish` 调用目录后端。

#### 4.3.3 源码精读

**线程主循环 + fork**：

[mooncake-store/src/master_service.cpp:4181-4273](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L4181-L4273) — `SnapshotThreadFunc()`：睡眠间隔 → 生成 `snapshot_id` → 建 pipe → `BuildSnapshotDescriptor` → 持 `snapshot_mutex_` fork → 子进程调 `PersistState`，父进程 `WaitForSnapshotChild`。

**时间戳生成 `snapshot_id`**（毫秒保证唯一）：

[mooncake-store/src/master_service.cpp:6713-6728](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L6713-L6728) — `FormatTimestamp`：`%Y%m%d_%H%M%S` + `_mmm` 毫秒。

**父进程等待 + 超时处理**：

[mooncake-store/src/master_service.cpp:4275-4373](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L4275-L4373) — `WaitForSnapshotChild`：管道设非阻塞、轮询 `waitpid(WNOHANG)`、把子进程日志按行转发。

[mooncake-store/src/master_service.cpp:4375-4435](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L4375-L4435) — `HandleChildTimeout`（SIGTERM→等 5s→SIGKILL）与 `HandleChildExit`（按退出码/信号上报成功/失败指标）。

**子进程专用的管道日志（避免 fork 后 glog 死锁）**：

[mooncake-store/include/ha/snapshot/snapshot_logger.h:12-30](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/snapshot/snapshot_logger.h#L12-L30) — `g_snapshot_log_pipe_fd` + `SNAP_LOG_*` 宏：用 async-signal-safe 的 `write()` 写管道，绕开 glog 锁。

**PersistState 的核心实现（序列化三块 + 上传 + Publish + 清理）**：

[mooncake-store/src/master_service.cpp:4543-4756](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L4543-L4756) — `PersistState(descriptor)`：三个 serializer 各自 `Serialize()` → 逐个 `UploadSnapshotPayloadFile` → 写 `manifest.txt`（`messagepack|1.0.0|snapshot_id`）→ `Publish` 推进 latest → `CleanupOldSnapshot`。

**单文件上传（含本地备份兜底）**：

[mooncake-store/src/master_service.cpp:4758-4797](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L4758-L4797) — `UploadSnapshotPayloadFile`：上传失败且开启 `backup_dir` 时，把 payload 落到本地备份目录兜底。

**保留份数清理**：

[mooncake-store/src/master_service.cpp:4799-4849](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L4799-L4849) — `CleanupOldSnapshot(keep_count)`：`List` 全部，超出 `retention_count_`（默认 2）的逐个 `Delete`，跳过当前 `snapshot_id`。

#### 4.3.4 代码实践（源码阅读型）

**目标**：把 4.3.2 的流程图和真实代码一一对应，确认「fork 瞬间一致性」是怎么达成的。

**步骤**：

1. 在 `SnapshotThreadFunc` 里定位三件事的**先后顺序**：① 生成 `snapshot_id`；② `BuildSnapshotDescriptor`（确定 OpLog 边界）；③ `fork()`。确认 ② 在 ③ 之前。
2. 找到持锁 fork 的那几行（`std::unique_lock<std::shared_mutex> lock(snapshot_mutex_); pid = fork();`），思考：为什么 fork 要在持写锁期间？fork 之后锁立刻释放（作用域结束），父进程会不会被自己的序列化阻塞？
3. 全局搜索 `shared_lock(snapshot_mutex_)`（如 `CreateCopyTask`），验证「读锁持有者会和 fork 互斥」。

**需要观察的现象**：父进程在 fork 后**立即**释放 `snapshot_mutex_` 并进入 `WaitForSnapshotChild` 的轮询；真正耗时的序列化/上传发生在**子进程**里。所以「慢」不会传导到主服务路径——这正是 fork 方案的价值。

**预期结果**：你能解释「为什么 Master 在做快照时几乎不影响在线读写」：fork 是瞬时的 COW，慢活在子进程，父子靠管道传日志、靠 `waitpid` 同步生死。

> 待本地验证：可在测试里把 `snapshot_interval_seconds` 调到很小，观察日志中 `[Snapshot] Locking snapshot mutex` 与 `[Snapshot:Child] ...` 的交替时序。

#### 4.3.5 小练习与答案

**练习 1**：为什么子进程用 `SNAP_LOG_*` 而不是 `LOG(INFO)`？

> **参考答案**：`fork()` 只复制调用线程，其他线程（可能正持有 glog 内部互斥锁）不会进入子进程，那些锁在子进程里处于「已锁但无 owner」状态。子进程一旦调 glog 就可能死锁。`SNAP_LOG_*` 改用 async-signal-safe 的 `write()` 写管道，绕开所有锁；父进程再从管道读出来转成 glog。

**练习 2**：`snapshot_retention_count_` 默认是 2，如果设成 0 会怎样？

> **参考答案**：构造时会校验（见 [L203-L206](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L203-L206)），`enable_snapshot_ && retention_count == 0` 直接抛 `invalid_argument` 拒绝启动，避免「每拍快照就把上一拍删光、永远只剩 0~1 份」的危险配置。

---

### 4.4 重启恢复：RestoreState 与有效快照选择

#### 4.4.1 概念说明

这是「Master 重启直接恢复」路径（不涉及 OpLog 回放）。Master 进程重启时，如果 `enable_snapshot_restore` 为真，构造函数会立刻调用 `RestoreState()`，把最近一次成功快照读回内存。

难点在于：**怎么从一堆快照里挑出一个「能用」的？** 因为：

- `latest.txt` 指向的那个快照，其 payload 可能上传到一半、或被并发删除，并不保证完整。
- 历史快照里，越新的越可能是 latest，但「最新指针」可能损坏或超前于实际存在的快照。

所以 `RestoreState` 的策略是：**先信 latest 指针，再列出所有候选按时间倒序逐个试**，哪个能完整下载+反序列化就用哪个；全不行就「从零开始（starting fresh）」。

#### 4.4.2 核心流程

```text
RestoreState()
│
├─ 1. GetLatest() → 拿 latest 指针指向的快照，加入候选（去重）
├─ 2. List(不限) → 把所有 snapshot_id ≤ latest 的快照也加入候选（去重）
│       （snapshot_id 字典序 == 时间序，所以 ">" latest 的视为「比指针还新但未发布完整」，跳过）
├─ 3. 候选为空 → LOG(ERROR) "starting fresh" 返回
└─ 4. 对每个候选（按加入顺序，latest 在前）：
        ├─ ResetStateAfterFailedRestoreAttempt()   # 先清场，保证干净起点
        └─ TryRestoreStateFromSnapshot(candidate, now)
              ├─ 下载 manifest.txt → 校验 protocol=="messagepack" 且 version=="1.0.0"
              ├─ 下载 metadata / segments / task_manager 三块
              ├─ 反序列化三块（任一失败 → fail_restore，回滚）
              ├─ 清理过期/非 COMPLETE 的元数据、重建容量计量
              └─ 成功 → return true
     全部失败 → ResetStateAfterFailedRestoreAttempt() + "starting fresh"
```

`TryRestoreStateFromSnapshot` 内部还有一个「带版本校验的准入」：`manifest.txt` 里写的是 `messagepack|1.0.0|snapshot_id`，protocol 或 version 不匹配直接判这个快照不可用。这让未来升级序列化格式时，旧 Master 不会去读读不懂的新快照。

恢复成功后还有两件「善后」：① 清理掉租约已过期、副本状态非 COMPLETE 的脏元数据（避免恢复出一堆半成品对象）；② 重置并重建内存容量计量、对每个段 `Ping` 对应客户端以恢复心跳。

#### 4.4.3 源码精读

**RestoreState：选候选 + 逐个尝试**：

[mooncake-store/src/master_service.cpp:4851-4924](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L4851-L4924) — `RestoreState()`：`GetLatest` + `List` 汇总候选（`snapshot_id > latest` 的跳过，去重），逐个 `TryRestoreStateFromSnapshot`。

**TryRestoreStateFromSnapshot：下载 + 校验 + 反序列化 + 善后**：

[mooncake-store/src/master_service.cpp:4926-5216](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L4926-L5216) — 关键段落：`fail_restore` lambda（失败即 `Reset` 回滚）[L4941-L4946](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L4941-L4946)；manifest 协议/版本校验 [L4981-L4990](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L4981-L4990)；三块反序列化 [L5061-L5088](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L5061-L5088)；清理脏元数据 [L5097-L5126](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L5097-L5126)。

**失败回滚**：

[mooncake-store/src/master_service.cpp:5218-5238](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L5218-L5238) — `ResetStateAfterFailedRestoreAttempt()`：三个 serializer 各 `Reset()`，清 `ok_client_`、排空 ping 队列、重置容量计量。

**触发点（构造函数里直接调）**：

[mooncake-store/src/master_service.cpp:200-202](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L200-L202) — `enable_snapshot_restore_` 时构造期即 `RestoreState()`。

#### 4.4.4 代码实践（源码阅读型）

**目标**：理解「latest 指针不可全信」这件事，以及候选排序逻辑。

**步骤**：

1. 读 `RestoreState`，找到「`snapshot.snapshot_id > latest_snapshot_id` 就 `continue`」那行（[L4897-L4900](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L4897-L4900)）。思考：为什么 `List` 出来的快照里，比 latest 还新的要丢弃？
2. 跟着 `TryRestoreStateFromSnapshot` 走一遍失败路径：任意一块下载或反序列化失败，都走 `fail_restore` → `ResetStateAfterFailedRestoreAttempt`。确认「一个快照失败不会污染下一个候选」。
3. 找到 manifest 版本校验（`SNAPSHOT_SERIALIZER_VERSION == "1.0.0"`），思考升级格式时的兼容策略。

**需要观察的现象**：恢复是**容错的**——latest 坏了试次新，次新坏了一路往下试，全坏了才「从零开始」。这正是 `retention_count >= 2` 的意义：多留一份历史快照作为恢复的「冗余」。

**预期结果**：你能回答「Master 重启时怎么选有效快照」：以 latest 指针优先、其余按时间倒序逐个尝试，每个候选都从干净状态开始、失败即回滚，直到某个能完整反序列化为止。

> 待本地验证：参考 `mooncake-store/tests/ha/snapshot/master_service_test_for_snapshot_base.h` 与 `snapshot_child_process_test.cpp`，它们用 `MOONCAKE_SNAPSHOT_LOCAL_PATH` 指向临时目录、`enable_snapshot_restore=true` 来跑「写快照→重建服务→断言状态一致」的循环。

#### 4.4.5 小练习与答案

**练习 1**：`RestoreState` 为什么在尝试每个候选前都要先 `ResetStateAfterFailedRestoreAttempt()`？

> **参考答案**：反序列化是「边读边写内存」的。上一个候选可能已经把部分 shard 灌进去了才失败，内存处于半恢复状态。不 Reset 就试下一个，会把两份不同快照的内容混在一起。Reset 保证每个候选都从「干净空状态」开始。

**练习 2**：恢复成功后，代码为什么要遍历元数据清理「`HasDiffRepStatus(COMPLETE)` 或租约过期且非 soft-pin」的对象？

> **参考答案**：快照冻结的是 fork 瞬间的状态，其中可能包含「正在 Put（PROCESSING）」「租约已过期」的对象。这些在恢复后的新世界里没有意义（客户端可能早已不活跃），清理它们避免恢复出大量无效/过期对象，同时也让容量计量准确。

---

### 4.5 OpLog 边界：快照序号如何保证一致性恢复

#### 4.5.1 概念说明

回到 2.2 的「全量 + 增量」模型：快照必须携带一个**边界序号** `last_included_seq`，声明「我的内容覆盖到第 N 条 OpLog」。恢复方据此知道：**只需回放 `seq > N` 的 OpLog 即可补齐**。这个 N 就是 OpLog 边界。

在 Mooncake 里：

- 边界在**生成快照时**写入 `SnapshotDescriptor.last_included_seq`（见 [ha_types.h:194-201](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/ha_types.h#L194-L201)）。
- 它只在 **HA + etcd** 模式下才有真实值；否则是哨兵 `0`，表示「没有持久化边界」。

边界串联起两条恢复路径：

- **Master 重启路径**（4.4）：**不读** `last_included_seq`，直接把快照灌回内存——因为这是同一个 Master 的自我恢复，没有「主备之间的日志差」要补。
- **热备一致性路径**：**依赖** `last_included_seq` 作为 OpLog 回放起点——因为 Standby 接管前必须先对齐到「快照时刻」，再补齐主上后续的日志。

#### 4.5.2 核心流程

**确定边界（生成侧）**：

```text
BuildSnapshotDescriptor(snapshot_id, manifest_path, object_prefix)
├─ sequence_id = ResolveSnapshotSequenceId()
│     ├─ if (!enable_ha_ || ha_backend_type_ != "etcd")  → 返回 0（哨兵）
│     └─ else: EtcdOpLogStore::GetLatestSequenceId() → 当前 etcd 最新序号
│           （OPLOG_ENTRY_NOT_FOUND 也视作 0）
├─ descriptor.last_included_seq    = sequence_id
├─ descriptor.producer_view_version = view_version_      # 记录是哪个任期产生的
└─ descriptor.created_at_ms        = CurrentTimeMs()
```

注意 `ResolveSnapshotSequenceId` 在 **`BuildSnapshotDescriptor` 里、也就是 fork 之前**就被调用（见 4.3.2 的顺序）。所以边界序号反映的是「即将 fork 那一刻」etcd 里已落地的最新 OpLog 序号。

**使用边界（热备侧）**：

```text
HotStandbyService::LoadSnapshotBaselineLocked(baseline_seq_id)
├─ metadata_store_->Clear(); oplog_applier_->Recover(0)
├─ snapshot = snapshot_provider_->LoadLatestSnapshot(cluster_id_)
│       （CatalogBackedSnapshotProvider：读 latest → 校验 manifest → 下载 segments/metadata
│         → 把 metadata 解析成 StandbyObjectMetadata，每条带上 last_sequence_id = last_included_seq）
├─ 把 snapshot.metadata 逐条 PutMetadata 进本地元数据存储
├─ oplog_applier_->Recover(snapshot.snapshot_sequence_id)   # ★ 以边界为起点
└─ baseline_seq_id = snapshot.snapshot_sequence_id          # 供后续 OpLog 跟随从此处之后开始
```

随后 `StartOplogFollowingLocked(baseline_seq_id)` 开始拉取并回放 `seq > baseline_seq_id` 的 OpLog，把快照之后的增量补上。

> 边界的语义一句话：**快照 = 截至第 N 条的完整状态；恢复 = 加载快照（到第 N 条）+ 回放第 N+1 条起的 OpLog**。这是数据库/存储系统里 WAL + checkpoint 的标准范式。

#### 4.5.3 源码精读

**生成侧：解析边界序号**：

[mooncake-store/src/master_service.cpp:4437-4470](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L4437-L4470) — `ResolveSnapshotSequenceId()`：非 etcd 返回 0；etcd 模式经 `GetSnapshotBoundaryOpLogStore`（[L4472-L4506](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L4472-L4506)）连 etcd，读 `GetLatestSequenceId`。`0` 的哨兵语义见注释（[L4440-L4444](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L4440-L4444)）：表示「无持久化边界」，调用方 `Recover(0)` 会从第 1 条开始回放。

**生成侧：把边界写进描述符**：

[mooncake-store/src/master_service.cpp:4508-4527](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L4508-L4527) — `BuildSnapshotDescriptor()`：`last_included_seq` 与 `producer_view_version` 被填入描述符。

**描述符类型定义**：

[mooncake-store/include/ha/ha_types.h:194-201](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/ha_types.h#L194-L201) — `SnapshotDescriptor`：`last_included_seq`、`producer_view_version`、`manifest_key`、`object_prefix`、`created_at_ms`。`OpLogSequenceId` 是 `uint64_t`（[ha_types.h:18](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/ha_types.h#L18)）。

**描述符被序列化进 catalog（embedded 写进 descriptor.txt）**：

[mooncake-store/include/ha/snapshot/catalog/snapshot_catalog_store.h:113-118](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/snapshot/catalog/snapshot_catalog_store.h#L113-L118) — `SerializeSnapshotDescriptor`：`"last_included_seq|producer_view_version|created_at_ms"` 文本格式。

**热备侧：以边界为起点回放**：

[mooncake-store/src/hot_standby_service.cpp:249-295](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/hot_standby_service.cpp#L249-L295) — `LoadSnapshotBaselineLocked`：`LoadLatestSnapshot` → 逐条 `PutMetadata` → `oplog_applier_->Recover(snapshot.snapshot_sequence_id)` → `baseline_seq_id = snapshot_sequence_id`。

**热备侧：快照消费者如何解析出边界**：

[mooncake-store/src/ha/snapshot/catalog_backed_snapshot_provider.cpp:364-462](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/snapshot/catalog_backed_snapshot_provider.cpp#L364-L462) — `LoadLatestSnapshot`：`descriptor.last_included_seq` 被赋给 `snapshot.snapshot_sequence_id`（[L459](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/snapshot/catalog_backed_snapshot_provider.cpp#L459)），并随每条 `StandbyObjectMetadata.last_sequence_id` 一起返回（[L218](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha/snapshot/catalog_backed_snapshot_provider.cpp#L218)）。

**SnapshotProvider 抽象**（热备依赖的窄接口）：

[mooncake-store/include/ha/snapshot/snapshot_provider.h:34-54](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha/snapshot/snapshot_provider.h#L34-L54) — 注释明确写出语义：「load snapshot → recover applier to `snapshot_sequence_id` → replay OpLog with `seq > snapshot_sequence_id`」。

#### 4.5.4 代码实践（源码阅读型）

**目标**：追踪 `last_included_seq` 从「被确定」到「被用作回放起点」的完整生命周期。

**步骤**：

1. **生成侧**：`SnapshotThreadFunc` → `BuildSnapshotDescriptor` → `ResolveSnapshotSequenceId`。确认：非 etcd 时返回 0；etcd 时返回 etcd 当前最新序号。记录这个值是怎么流进 `descriptor.last_included_seq` 的。
2. **持久化侧**：`Publish` 把 descriptor 写进 catalog（embedded 写成 `descriptor.txt`，内容含 `last_included_seq`）。
3. **消费侧（热备）**：`CatalogBackedSnapshotProvider::LoadLatestSnapshot` 读回 `descriptor.last_included_seq` → `snapshot.snapshot_sequence_id` → `HotStandbyService::LoadSnapshotBaselineLocked` 里 `oplog_applier_->Recover(snapshot_sequence_id)`。

**需要观察的现象**：边界序号在生成端、持久化端、消费端用的是**同一个字段**（`last_included_seq` ↔ `snapshot_sequence_id`），全程没有重新计算。这就是「快照与 OpLog 的契约」。

**预期结果**：你能讲清下面这张端到端映射：

| 阶段 | 函数 | 字段 | 值的含义 |
| --- | --- | --- | --- |
| 生成 | `ResolveSnapshotSequenceId` | 返回值 | fork 前 etcd 最新 OpLog 序号（或 0 哨兵） |
| 装填 | `BuildSnapshotDescriptor` | `last_included_seq` | 写进描述符 |
| 持久化 | `Publish` | `descriptor.txt` | 落到 catalog |
| 消费 | `LoadLatestSnapshot` | `snapshot_sequence_id` | 读回边界 |
| 回放 | `LoadSnapshotBaselineLocked` | `Recover(...)` 入参 | OpLog 回放起点 |

> 关于一致性的边界讨论（待本地验证）：`last_included_seq` 在 fork 之前解析。理论上 fork 瞬间的内存状态可能与该序号之间存在微小窗口（序号解析后、fork 前，主上可能又追加了新 OpLog 并改了内存）。本讲不就「重放是否幂等」下定论——这取决于 `OpLogApplier` 对各类操作（Put/Remove/Copy…）的幂等性保证，建议结合 `mooncake-store/src/ha/oplog/oplog_applier.cpp` 自行核验。从工程范式看，「快照取一个不晚于实际状态的序号、再回放其后日志」是 WAL+checkpoint 的常见且安全的取法。

#### 4.5.5 小练习与答案

**练习 1**：在「非 HA / 非 etcd」的纯本地快照模式下，`last_included_seq` 是多少？为什么这样设计？

> **参考答案**：是 `0`。因为这种模式下没有跨进程的 OpLog 需要回放（Master 重启走 4.4 的直接灌回路径，不用 OpLog）。`0` 作为哨兵表示「无持久化边界」，注释里明确：调用方若 `Recover(0)` 且开启了 oplog following，会从第 1 条开始回放。

**练习 2**：`producer_view_version`（即 `view_version_`）为什么也要写进快照描述符？

> **参考答案**：它记录「这份快照是由哪个任期（view）的主产生的」。热备在加载快照、决定从哪个 OpLog 点回放时，需要知道这份基线属于哪一代主，以正确处理「旧主的过期日志」与「新主的日志」之间的关系（详见 u7-l1 的 Leader 任期与 OpLog 复制）。它是边界序号的「命名空间」补充。

---

## 5. 综合实践

**任务**：用一段「时序叙述」把本讲所有最小模块串起来——这正是规格里要求描述的「一次周期性快照」全貌。请对照源码填空并回答四个问题。

**场景设定**：一个开启了 `enable_snapshot=true`、`enable_snapshot_restore=true`、对象存储 `local`（`MOONCAKE_SNAPSHOT_LOCAL_PATH=/data/snap`）、目录后端 `embedded`、且 HA 后端为 `etcd` 的 Master。

**请按顺序描述并回答**：

1. **何时触发**：后台线程 `SnapshotThreadFunc` 每隔多久醒来？唤醒后第一步检查什么？（对应 [L4181-L4191](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L4181-L4191)）
2. **如何确定 OpLog 边界**：fork 之前调用了哪个函数？它在 etcd 模式下返回什么？这个值被放进描述符的哪个字段？（对应 [L4508-L4527](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L4508-L4527) 与 [L4437-L4470](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L4437-L4470)）
3. **快照写入哪个后端**：fork 之后子进程 `PersistState` 把 `metadata/segments/task_manager/manifest.txt` 写到哪？`latest` 指针又是通过哪个后端推进的？payload 与目录元数据在本场景下分别落在什么物理位置？（对应 [L4543-L4756](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L4543-L4756)）
4. **重启时如何选择有效快照恢复**：Master 重启 → 构造函数调 `RestoreState` → 它如何汇总候选？为什么 latest 不可全信、要逐个尝试？热备路径又如何用边界序号衔接 OpLog 回放？（对应 [L4851-L4924](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L4851-L4924) 与 [hot_standby_service.cpp:249-295](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/hot_standby_service.cpp#L249-L295)）

**参考作答要点**：

1. 默认每 `snapshot_interval_seconds_`（600s）醒来；唤醒后先看 `enable_snapshot_`，为假则跳过本轮。
2. fork 前调用 `BuildSnapshotDescriptor`，其内部 `ResolveSnapshotSequenceId` 在 etcd 模式下返回 etcd 当前最新 OpLog 序号，填入 `descriptor.last_included_seq`；非 etcd 返回 0。
3. 子进程经 `UploadSnapshotPayloadFile` → `snapshot_object_store_->UploadBuffer`（local 实现）把四份 payload 写到 `/data/snap/mooncake_master_snapshot/{cluster_id}/{snapshot_id}/`；再经 `EmbeddedSnapshotCatalogStore::Publish` 把 `descriptor.txt` 与 `latest.txt` 也写进**同一个** local 对象存储目录。payload 与目录元数据在本场景物理上都在 `/data/snap` 下。
4. `RestoreState` 先 `GetLatest` 拿指针指向的快照，再 `List` 全部并把 `snapshot_id ≤ latest` 的也加入候选（去重）；因为 latest 指针可能指向不完整/已删的快照，故对候选逐个 `TryRestoreStateFromSnapshot`（失败即 `Reset` 回滚）。热备路径则由 `LoadSnapshotBaselineLocked` 加载快照后，以 `snapshot_sequence_id = last_included_seq` 调 `oplog_applier_->Recover(...)`，再回放其后日志。

> 这是「源码阅读型」综合实践，不要求运行；若要落地验证，可参照 `mooncake-store/tests/ha/snapshot/` 下的测试（设 `MOONCAKE_SNAPSHOT_LOCAL_PATH` 到临时目录、`enable_snapshot_restore=true`），跑一次「写快照→销毁服务对象→重建服务对象→断言 key 还在」。

## 6. 本讲小结

- **MetadataSerializer** 把 Master 内存里的对象元数据（按 shard 独立 zstd 压缩、`(tenant,key)` 排序）序列化成 MessagePack 字节，并一并持久化 `discarded_replicas` 与全局 `replica_next_id`；恢复后重建组路由。
- **两个正交后端**：对象存储（`local`/`s3`）存大块 payload，快照目录（`embedded`/`redis`）存「有哪些快照、最新的是谁」。embedded 把目录元数据也写进对象存储，redis 把 `latest`/索引放 Redis。
- **周期性快照靠 fork**：后台线程在持 `snapshot_mutex_` 写锁期间 fork，瞬间用 COW 冻结一致性状态；耗时的序列化/上传在子进程进行，不阻塞主服务；父子靠管道传日志、`waitpid` 同步生死。
- **重启恢复容错选快照**：`RestoreState` 以 latest 指针优先、历史快照按时间倒序逐个尝试，每个候选从干净状态起、失败即回滚，直至找到能完整反序列化的一份；全失败则「从零开始」。
- **OpLog 边界 `last_included_seq`** 在生成端（`ResolveSnapshotSequenceId`）确定并写入描述符，在热备端（`Recover`）作为回放起点，实现「全量快照 + 增量 OpLog」的一致性恢复；非 etcd 模式为 0 哨兵。
- **manifest 版本/协议校验**（`messagepack|1.0.0`）为未来的序列化格式演进预留了兼容性闸门。

## 7. 下一步学习建议

- **接 u7-l3**：本讲的 `embedded` 目录后端和 `redis` 目录后端都依赖一个外部存储。下一讲「元数据服务器后端：etcd / HTTP / Redis」会系统讲解 Store 与 TE 共用的元数据后端选型与部署权衡，能帮你把本讲里出现的 etcd/redis 连接串、`cluster_id` 命名空间放在更大的部署图景里理解。
- **深入 OpLog 一致性**：本讲对 `last_included_seq` 的「重放幂等性」留了待验证项。建议接着读 `mooncake-store/src/ha/oplog/oplog_applier.cpp`、`oplog_replicator.cpp`，搞清楚各类操作在「快照之后被重复回放」时是否安全，把 4.5 的开放问题闭合。
- **跑一遍快照测试**：阅读并尝试构建 `mooncake-store/tests/ha/snapshot/`（`snapshot_child_process_test.cpp`、`master_service_test_for_snapshot_base.h`），它们以最小依赖复现了「写快照→子进程→恢复」闭环，是验证本讲理解的最佳抓手。
- **回看 u7-l1**：如果你对 `producer_view_version`、OpLog 复制、主备状态机的联系还不够清晰，建议复习 u7-l1 再回到本讲 4.5，两边对照阅读收益最大。
