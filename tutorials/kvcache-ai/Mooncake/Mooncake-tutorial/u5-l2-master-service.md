# MasterService：Store 的元数据大脑

## 1. 本讲目标

上一讲（u5-l1）我们从「地图级」认识了 Mooncake Store 的控制面与数据面分离，并指出 **Master Service 是控制面的核心**。本讲我们钻进这个「元数据大脑」内部，拆开它的机盖看齿轮。学完本讲你应该能够：

1. 说清 `MasterService` 这个类**内部由哪些数据结构组成**（段管理器、分片元数据、客户端租约表、后台线程），以及它如何用**严格的锁层级**避免死锁。
2. 说清一笔内存段是如何通过 `MountSegment` 被「登记」进全局资源池的，以及客户端停止心跳后段是如何被自动卸载的。
3. 描述**对象元数据模型**：一个 `(tenant, key)` 在内存里长什么样——`ObjectMetadata` 持有哪些字段、它和 `Replica` 的关系、副本状态机如何流转。
4. 掌握 **1024 路分片 + 多租户（tenant）命名空间**的设计：`getShardIndex` 如何由 tenant 与 key 共同决定一个 key 落在哪个分片，为什么这样设计。
5. 能准确说出 `MountSegment` / `PutStart` / `GetReplicaList` / `Remove` / `Ping` 这些核心 RPC 的**返回语义**和**可能的 `ErrorCode`**。

> 本讲聚焦控制面的「元数据与调度」机制本身。分配策略（random/free_ratio_first）、淘汰算法的数学细节、快照恢复等较大主题只点到为止，留给后续讲义。

## 2. 前置知识

本讲默认你已经学完：

- **u5-l1 Store 总体架构**：你需要知道 Master 是控制面、Client 是数据面，数据走 TE 在 Client↔Client 间直传，Master 只在 `PutStart`/`PutEnd`/`GetReplicaList` 这几步被「问一下」。本讲就是要把这几步在 Master 内部的真实实现讲透。

此外，几个通用概念先建立直觉：

### 什么是「元数据（metadata）」与「资源池」？

> 想象一个巨型仓库管理员。他手里有一本账本，记着：「1 号货架有 100 格、已被占 30 格，归属司机张三；2 号货架有 50 格……」以及「包裹 A 放在 1 号货架第 7 格，状态=已入库」。**管理员从不亲手搬货，但所有货的位置和状态都记在他这本账本里。**

- **资源池**：Master 维护的「集群里有哪些内存段、各段还剩多少空间」的账本（通过 `MountSegment` / `UnmountSegment` 增删）。
- **对象元数据**：对每个 `(tenant, key)`，记录「它的副本放在哪些段的哪些偏移、各副本状态如何、谁的租约还活着」。

`MasterService` 就是这个管理员 + 账本的总和。本讲的全部内容，都是在讲这本账本**长什么样、怎么加锁、怎么分卷（分片）、怎么按租户隔开**。

### `tl::expected<T, ErrorCode>` 是什么？

Mooncake 的 C++ 接口大量使用 `tl::expected<T, ErrorCode>` 作为返回类型。它是「要么返回正常的 `T`，要么返回一个 `ErrorCode`」的带类型错误通道（类似 Rust 的 `Result`）。

- 成功：返回 `T`（若是 `void` 则表示无返回值）。
- 失败：用 `tl::make_unexpected(ErrorCode::XXX)` 包装一个错误码返回。

本讲会反复看到这种签名，例如：

```cpp
auto MountSegment(const Segment& segment, const UUID& client_id)
    -> tl::expected<void, ErrorCode>;
```

读法：「调用成功返回空，失败返回某个 `ErrorCode`」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲怎么用 |
|---|---|---|
| [mooncake-store/include/master_service.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h) | `MasterService` 类声明，含 `ObjectMetadata`、`TenantState`、`MetadataShard`、各种 Accessor 内嵌类 | 本讲的「骨架」：看数据结构怎么组织、锁怎么分层、分片怎么算 |
| [mooncake-store/src/master_service.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp) | `MasterService` 各方法的实现 | 本讲的「血肉」：`MountSegment`/`PutStart`/`GetReplicaList`/`Remove`/`Ping`/`ClientMonitorFunc` 的真实逻辑 |
| [mooncake-store/include/master_config.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_config.h) | `MasterConfig` 及其 Builder/Wrapper 配置族 | 看 lease TTL、客户端存活 TTL、淘汰水位等关键参数从哪来 |
| [mooncake-store/include/types.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/types.h) | `ErrorCode` 枚举、`Segment`、`ClientStatus`、`NormalizeTenantId`、各种默认常量 | RPC 返回的 `ErrorCode` 全集、tenant 归一化规则、默认 TTL 值 |
| [mooncake-store/include/replica.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h) | `Replica`、`ReplicaStatus`、`ReplicateConfig` | 副本状态机（PROCESSING/COMPLETE…）与 `Replica::Descriptor` |
| [mooncake-store/include/allocator.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocator.h) | `ReplicaType` 枚举（MEMORY/DISK/LOCAL_DISK/NOF_SSD/ALL） | 区分不同介质的副本类型 |
| [mooncake-store/src/segment.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/segment.cpp) | `ScopedSegmentAccess::MountSegment` 等段管理实现 | `MountSegment` 在段管理器内部到底做了什么 |

> 小提示：`mooncake-store/src/master.cpp` 是 Master **可执行程序的入口**（解析命令行 flag、启动 RPC server），**不是** `MasterService` 方法的实现。方法的实现全部在 `master_service.cpp` 里——这是初学者常踩的坑。

## 4. 核心概念与源码讲解

### 4.1 MasterService 全景：元数据大脑的角色、数据与线程

#### 4.1.1 概念说明

`MasterService` 是一个单例式的核心类，它把「集群资源账本」和「调度逻辑」都装在一个对象里。可以这样概括它的四大职责：

1. **段管理（SegmentManager）**：登记/卸载各 Client 贡献的内存段，维护每段的分配器与剩余空间。
2. **对象元数据（分片的 `metadata_shards_`）**：记录每个 `(tenant, key)` 的副本位置与状态。
3. **客户端租约（`ok_client_` + `client_ping_queue_`）**：通过心跳判定哪些 Client 还活着，活着的才能持有有效段与副本。
4. **后台线程**：客户端监控、淘汰、快照、任务清理、NoF 心跳等多条后台循环。

它之所以能安全地服务高并发请求，靠的是**严格的锁层级**和**分片**——这是本模块的两个关键词。

#### 4.1.2 核心流程

`MasterService` 处理一笔请求时，锁的获取顺序是固定的。类注释直接写明了这套层级：

```
锁层级（从外到内）：
1. client_mutex_          （客户端存活表 ok_client_ 的读写锁）
2. metadata_shards_[i].mutex   （第 i 个元数据分片的读写锁）
3. segment_mutex_         （段管理器的锁）
```

此外，绝大多数变更类操作在最外层还会先拿一把 `snapshot_mutex_` 的**共享锁**（保证做快照时不会被改），并对**单个 key** 再加一把细粒度的 `object_operation_locks_` 条纹锁（4096 条纹）。一次 `PutStart` 的加锁顺序大致是：

```
object_operation_lock (按 key hash 选条纹, 串行化同一 key 的操作)
   └─ snapshot_mutex_ (shared, 阻挡快照)
        └─ metadata_shard[i].mutex (RW, 保护该分片的元数据)
```

后台线程方面，构造函数会拉起多条 `std::thread`：

- `ClientMonitorFunc`：每 1 秒扫一次，回收过期客户端、卸载其段。
- `EvictionThreadFunc`：内存/NoF 淘汰循环。
- `SnapshotThreadFunc` / `TaskCleanupThreadFunc` / `NofHeartbeatThreadFunc` / `JobDispatchThreadFunc`：快照、任务清理、NoF 心跳、Drain 任务派发。

#### 4.1.3 源码精读

**锁层级注释**——这是阅读 `MasterService` 的第一把钥匙：

```cpp
/*
 * @brief MasterService is the main class for the master server.
 * Lock order: To avoid deadlocks, the following lock order should be followed:
 * 1. client_mutex_
 * 2. metadata_shards_[shard_idx_].mutex
 * 3. segment_mutex_
 */
class MasterService {
```
—— [mooncake-store/include/master_service.h:L61-L68](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L61-L68)

**分片元数据与段/分配器成员**——注意元数据是 `std::array<MetadataShard, 1024>`：

```cpp
static constexpr size_t kNumShards = 1024;  // Number of metadata shards
...
struct MetadataShard {
    mutable SharedMutex mutex;
    std::unordered_map<std::string, TenantState> tenants GUARDED_BY(mutex);
};
std::array<MetadataShard, kNumShards> metadata_shards_;
...
SegmentManager segment_manager_;
NoFSegmentManager nof_segment_manager_;
std::shared_ptr<AllocationStrategy> allocation_strategy_;
```
—— [mooncake-store/include/master_service.h:L1151-L1184](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1151-L1184)

**每 key 的细粒度条纹锁**——`object_operation_locks_` 把 4096 把 `std::mutex` 按 `(tenant,key)` 的 hash 分摊，用来串行化对同一个 key 的并发操作（避免同 key 的 `PutStart`/`PutEnd`/`Remove` 互相踩踏）：

```cpp
static constexpr size_t kObjectOperationLockStripes = 4096;
struct ObjectOperationLock {
    std::unique_lock<std::mutex> lock;
};
ObjectOperationLock AcquireObjectOperationLock(const std::string& tenant_id,
                                               const std::string& key);
std::array<std::mutex, kObjectOperationLockStripes> object_operation_locks_;
```
—— [mooncake-store/include/master_service.h:L1198-L1207](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1198-L1207)

**关键配置来自 `MasterConfig`**——lease TTL、客户端存活 TTL、淘汰水位等都从这里注入：

```cpp
struct MasterConfig {
    ...
    uint64_t default_kv_lease_ttl;        // 对象硬租约 TTL（毫秒）
    uint64_t default_kv_soft_pin_ttl;     // 软 pin TTL（毫秒）
    bool allow_evict_soft_pinned_objects;
    double eviction_ratio;
    double eviction_high_watermark_ratio;
    ...
    int64_t client_live_ttl_sec;          // 客户端存活 TTL（秒）
    ...
};
```
—— [mooncake-store/include/master_config.h:L37-L47](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_config.h#L37-L47)

这些字段的默认值定义在 `types.h` 里：硬租约 `DEFAULT_DEFAULT_KV_LEASE_TTL = 5000`（5 秒）、软 pin `DEFAULT_KV_SOFT_PIN_TTL_MS = 30*60*1000`（30 分钟）、客户端存活 `DEFAULT_CLIENT_LIVE_TTL_SEC = 10`（10 秒）。
—— [mooncake-store/include/types.h:L85-L95](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/types.h#L85-L95)

#### 4.1.4 代码实践

**实践目标**：用源码确认 `MasterService` 的「多线程 + 多锁」结构，建立全局心智模型。

**操作步骤**：

1. 打开 [master_service.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h)，定位第 61–68 行的锁层级注释。
2. 在文件中分别搜索 `std::thread`、`std::atomic<bool>`、`std::shared_mutex`、`client_mutex_`、`snapshot_mutex_`，数一数 `MasterService` 内部有几条后台线程、几把不同用途的锁。
3. 打开 [master_config.h:L27-L118](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_config.h#L27-L118)，圈出与本讲相关的 4 个参数：`default_kv_lease_ttl`、`default_kv_soft_pin_ttl`、`client_live_ttl_sec`、`eviction_high_watermark_ratio`。

**需要观察的现象**：你会发现 `MasterService` 几乎所有变更方法的第一行都是 `std::shared_lock<std::shared_mutex> shared_lock(snapshot_mutex_);`——这是一把「全局快照屏障」，它的存在是为了让快照线程能拿独占锁冻结状态。

**预期结果**：能画出一张「`MasterService` 成员速览图」：段管理器（`segment_manager_`）、分片元数据（`metadata_shards_[1024]`）、客户端表（`ok_client_` + `client_ping_queue_`）、N 条后台线程，并用箭头标出锁层级。

#### 4.1.5 小练习与答案

**练习 1**：为什么锁层级里 `client_mutex_` 必须在 `metadata_shards_[i].mutex` 之外（先拿）？

> **参考答案**：因为清理无效句柄（`ClearInvalidHandles`）等操作需要先确定「哪些客户端还活着」（读 `ok_client_`），再据此遍历元数据分片删除悬挂的副本句柄。如果反过来先锁分片、再锁客户端表，就可能与「先锁客户端表、再碰分片」的另一条路径（如 `ClientMonitorFunc`）形成相反的获取顺序，从而死锁。固定层级（client → shard → segment）让所有路径以相同顺序加锁，打破死锁环。

**练习 2**：`object_operation_locks_` 用 4096 条纹而不是「每个 key 一把锁」，主要权衡是什么？

> **参考答案**：每个 key 一把锁需要随对象创建/销毁动态管理锁对象（生命周期复杂、且有开销）。用固定数量的条纹锁（`hash(key) % 4096`）复用锁对象，省去动态分配；代价是 hash 到同一条纹的不同 key 会互相串行化（伪共享）。4096 足够大，使同条纹碰撞概率很低，是「简单 + 低冲突」的常见折中。

---

### 4.2 段挂载：MountSegment 与客户端存活

#### 4.2.1 概念说明

每个想贡献内存的 Client，启动时要把本地一段连续内存「挂载（mount）」给 Master。挂载的本质是：**Client 把段的元数据（名字、基址、大小、传输端点）告诉 Master，Master 为它创建一个内存分配器并登记到全局资源池**。此后这个段就可以被分配策略选中、用来承载别人的副本。

挂载不是一次性的——它和「客户端心跳」绑在一起：

- 挂载时，Master 把该 client 推入 `client_ping_queue_`，监控线程开始给它计时。
- Client 要定期 `Ping`；若超过 `client_live_ttl_sec`（默认 10 秒）没 ping，监控线程认为它「死了」，自动卸载它的段、清理它的副本。

这就形成「软注册」模型：**段的有效性由心跳维持，心跳断了就回收**。这避免了某个 Client 进程崩溃后，它登记的段永远占着元数据。

#### 4.2.2 核心流程

一次 `MountSegment` 的流程：

```
Client 启动 → 调用 MountSegment(segment, client_id)
   ├─ ① snapshot_mutex_ 共享锁
   ├─ ② 把 client_id 推入 client_ping_queue_（让监控线程开始计时）
   │      失败(队列满) → INTERNAL_ERROR
   ├─ ③ segment_access.MountSegment(segment, client_id)  ← 段管理器内部
   │      ├─ 参数校验（base/size 非零、对齐）→ 否则 INVALID_PARAMS
   │      ├─ 已存在且状态 OK → SEGMENT_ALREADY_EXISTS（被外层当幂等成功）
   │      ├─ 已存在但非 OK → UNAVAILABLE_IN_CURRENT_STATUS
   │      └─ 创建分配器、登记 mounted_segments_、记录 client→segments 映射
   └─ 返回 OK
```

监控线程 `ClientMonitorFunc` 的回收流程（每秒一轮）：

```
每 1 秒：
  ├─ 排空 client_ping_queue_，把每个 client 的 TTL 刷新为 now + client_live_ttl_sec
  ├─ 找出 TTL 已过期的 client
  ├─ 对每个过期 client：从 ok_client_ 移除、PrepareUnmount 其所有段
  ├─ ClearInvalidHandles()   ← 清理指向已卸载段或已死 client 的副本句柄
  └─ CommitUnmount 各段 + UnmountLocalDiskSegment
```

而 `Ping` 则是 Client 主动续命：返回当前 `view_version_` 和客户端状态（`OK` 或 `NEED_REMOUNT`）。

#### 4.2.3 源码精读

**`MountSegment` 实现**——注意它「先推队列再挂载」的顺序，以及把 `SEGMENT_ALREADY_EXISTS` 当幂等成功：

```cpp
auto MasterService::MountSegment(const Segment& segment, const UUID& client_id)
    -> tl::expected<void, ErrorCode> {
    std::shared_lock<std::shared_mutex> shared_lock(snapshot_mutex_);
    ScopedSegmentAccess segment_access = segment_manager_.getSegmentAccess();
    {
        PodUUID pod_client_id{client_id.first, client_id.second};
        if (!client_ping_queue_.push(pod_client_id)) {        // 队列满 → 失败
            return tl::make_unexpected(ErrorCode::INTERNAL_ERROR);
        }
    }
    auto err = segment_access.MountSegment(segment, client_id);
    if (err == ErrorCode::SEGMENT_ALREADY_EXISTS) {
        return {};        // 幂等：重复挂载视为成功
    } else if (err != ErrorCode::OK) {
        return tl::make_unexpected(err);
    }
    return {};
}
```
—— [mooncake-store/src/master_service.cpp:L475-L513](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L475-L513)

**段管理器内部的真正挂载逻辑**——参数校验、重复检测、按配置创建 `CachelibBufferAllocator` 或 `OffsetBufferAllocator`：

```cpp
ErrorCode ScopedSegmentAccess::MountSegment(const Segment& segment,
                                            const UUID& client_id) {
    const uintptr_t buffer = segment.base;
    const size_t size = segment.size;
    ...
    if (buffer == 0 || size == 0) {
        return ErrorCode::INVALID_PARAMS;
    }
    // 已存在
    auto exist_segment_it = segment_manager_->mounted_segments_.find(segment.id);
    if (exist_segment_it != segment_manager_->mounted_segments_.end()) {
        if (exist_segment->status == SegmentStatus::OK)
            return ErrorCode::SEGMENT_ALREADY_EXISTS;
        else
            return ErrorCode::UNAVAILABLE_IN_CURRENT_STATUS;
    }
    // 创建分配器并登记
    switch (segment_manager_->memory_allocator_) {
        case BufferAllocatorType::CACHELIB: ... break;
        case BufferAllocatorType::OFFSET:   ... break;
    }
    ...
}
```
—— [mooncake-store/src/segment.cpp:L30-L118](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/segment.cpp#L30-L118)

**`Ping` 实现**——续命 + 返回客户端状态；队列满也返回 `INTERNAL_ERROR`：

```cpp
auto MasterService::Ping(const UUID& client_id)
    -> tl::expected<PingResponse, ErrorCode> {
    std::shared_lock<std::shared_mutex> lock(client_mutex_);
    ClientStatus client_status;
    client_status = ok_client_.contains(client_id)
                        ? ClientStatus::OK
                        : ClientStatus::NEED_REMOUNT;
    PodUUID pod_client_id = {client_id.first, client_id.second};
    if (!client_ping_queue_.push(pod_client_id)) {
        return tl::make_unexpected(ErrorCode::INTERNAL_ERROR);   // 队列满
    }
    return PingResponse(view_version_, client_status);
}
```
—— [mooncake-store/src/master_service.cpp:L3278-L3296](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3278-L3296)

`PingResponse` 就两个字段：`view_version_id`（集群视图版本，客户端据此判断是否需要重新拉取段视图）和 `client_status`（`OK` / `NEED_REMOUNT`）。
—— [mooncake-store/include/rpc_types.h:L12-L19](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/rpc_types.h#L12-L19)

**`ClientMonitorFunc` 的过期回收**——过期客户端的段被卸载、副本句柄被清理：

```cpp
void MasterService::ClientMonitorFunc() {
    ...
    while (client_monitor_running_) {
        auto now = std::chrono::steady_clock::now();
        // ① 刷新 TTL
        while (client_ping_queue_.pop(pod_client_id)) {
            client_ttl[client_id] = now + std::chrono::seconds(client_live_ttl_sec_);
        }
        // ② 找出过期 client
        for (auto it = client_ttl.begin(); it != client_ttl.end();) {
            if (it->second < now) { expired_clients.push_back(it->first); it = client_ttl.erase(it); }
            else ++it;
        }
        if (!expired_clients.empty()) {
            // ③ 从 ok_client_ 移除、PrepareUnmount 各段
            // ④ ClearInvalidHandles() 清理悬挂句柄
            // ⑤ CommitUnmount + UnmountLocalDiskSegment
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(kClientMonitorSleepMs));  // 1 秒
    }
}
```
—— [mooncake-store/src/master_service.cpp:L5813-L5923](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L5813-L5923)

> 一个精妙的设计细节：注释解释了为什么「先推 ping 队列、再挂载」——如果挂载完成后再推队列，此时队列可能已满，但又必须推（否则该 client 永远不被监控）；如果挂载前就先让 client 过期并触发卸载，又会和正在进行的挂载竞争。因此推队列的时机被精确安排在「拿到锁之后、挂载完成之前」。见 [master_service.cpp:L480-L500](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L480-L500)。

#### 4.2.4 代码实践

**实践目标**：跟随「挂载 → 心跳 → 过期回收」这条生命周期，验证你对段有效性的理解。

**操作步骤**：

1. 在 [master_service.cpp:L475](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L475)（`MountSegment`）、[L3278](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L3278)（`Ping`）、[L5813](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L5813)（`ClientMonitorFunc`）三处设「心理断点」。
2. 思考：如果某 Client `MountSegment` 之后**再也没调用过 `Ping`**，多久之后它的段会被卸载？依据哪个常量？
3. 进阶：看 `UnmountSegment`（[L907](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L907)）的三步结构（Prepare → ClearInvalidHandles → Commit），对比 `ClientMonitorFunc` 的回收路径，你会发现它们高度相似——主动卸载和被动过期走的是同一套清理逻辑。

**需要观察的现象**：`ClearInvalidHandles` 在卸载段后被调用（[L930](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L930)），它遍历**所有 1024 个分片**，删除指向已卸载段的副本句柄——这正是「段没了，依赖它的副本元数据也要清掉」的体现。

**预期结果**：你能回答「一个 Client 进程被 `kill -9` 后，它的段和副本元数据多久、由谁、怎么被清理」。**待本地验证**：若本地可跑 Master，启动一个 Client mount 一段后直接 kill 该 Client，观察 Master 日志中约 10 秒后出现的 `client_expired` / `unmount_expired_mem_segment` 日志。

#### 4.2.5 小练习与答案

**练习 1**：`MountSegment` 对同一个 `(segment, client_id)` 调用两次，返回什么？为什么这样设计？

> **参考答案**：第二次返回 `ErrorCode::OK`（成功）。因为 `segment_access.MountSegment` 检测到段已存在且状态为 OK 时返回 `SEGMENT_ALREADY_EXISTS`，而外层 `MountSegment` 把它转换成空（成功）。注释里明确写「This function is idempotent」。幂等设计是为了应对网络重试：Client 不确定上一次挂载是否到达 Master，重试是安全的。

**练习 2**：`client_ping_queue_` 是 `boost::lockfree::queue`，为什么用心跳队列而不是直接在 `Ping` 里加锁改 TTL？

> **参考答案**：`Ping` 是高频调用（每个 Client 每秒级一次），若在 `Ping` 里直接加锁修改共享的 TTL 表，会引入锁竞争并拖慢这条热路径。无锁队列把「续命事件」异步投递给 `ClientMonitorFunc` 线程，由它独占处理 TTL 表，做到「热路径无锁、冷路径集中处理」。队列容量 `kClientPingQueueSize = 128*1024`，满时才退化成 `INTERNAL_ERROR`。

---

### 4.3 对象元数据模型：ObjectMetadata 与 Replica

#### 4.3.1 概念说明

挂载段解决了「集群有哪些空间」的问题；对象元数据解决「某个 key 的数据放在哪、状态如何」的问题。这两层关系是：

```
ObjectMetadata（一个 (tenant,key) 对应一个）
   └─ 持有 std::vector<Replica>   ← 一个对象可以有多个副本（多副本冗余/多介质）
```

- **`ObjectMetadata`**：记录对象的「身份 + 生命周期」——谁的 client_id、何时开始 put、value 多大、数据类型、租约/软硬 pin 状态，以及一组副本。它自带一把 `SpinLock`，保护 `lease_timeout` / `soft_pin_timeout` 这类可变字段。
- **`Replica`**：一个副本，描述「数据落在哪个段、段内偏移、大小、状态」。副本有状态机：`INITIALIZED → PROCESSING → COMPLETE`（正常），或 `FAILED`/`REMOVED`。
- **`Replica::Descriptor`**：副本的「地址描述符」，是 Master 交给数据面的东西（上一讲强调过：控制面的输出就是数据面的输入）。

一个关键规则：**只有 `COMPLETE` 的副本对 `Get` 可见**。这是两阶段 Put（`PutStart` 分配→写→`PutEnd` 标记 COMPLETE）防脏读的基础。

#### 4.3.2 核心流程

副本状态机的流转：

```
PutStart:  分配空间 → 副本 = PROCESSING（不可读，对 Get 不可见）
   │
   ├─ PutEnd:    副本.mark_complete() → COMPLETE（对 Get 可见）
   └─ PutRevoke: 副本被删除（释放空间）→ 对象若无有效副本则整体删除
```

`ObjectMetadata` 的有效性判定（`IsValid`）：**至少有一个有效副本且 size>0**。访问器（Accessor）在打开对象时会顺手清理「指向已卸载段的无效内存句柄」，若清理后对象不再有效，整个对象会被删除——这就是上一讲提到的「段卸载会连带清理依赖它的副本」。

#### 4.3.3 源码精读

**`ObjectMetadata` 结构**——注意它的 RAII 指标管理（构造时 `inc_key_count`，析构时 `dec_key_count`）、不可拷贝/移动、以及一把保护租约字段的 `SpinLock`：

```cpp
struct ObjectMetadata {
    ~ObjectMetadata() {                                  // 析构即减少指标
        MasterMetricManager::instance().dec_key_count(1);
        if (soft_pin_timeout) dec_soft_pin_key_count(1);
    }
    ObjectMetadata(const UUID& client_id_, ..., size_t value_length,
                   std::vector<Replica>&& reps, bool enable_soft_pin,
                   ..., std::string tenant_id_ = "default", std::string user_key_ = {})
        : client_id(client_id_), size(value_length), ..., tenant_id(...), user_key(...),
          replicas_(std::move(reps)) {
        MasterMetricManager::instance().inc_key_count(1);
        if (enable_soft_pin) { soft_pin_timeout.emplace(); inc_soft_pin_key_count(1); }
    }

    UUID client_id;                       // 谁发起的 PutStart（PutEnd/PutRevoke 要校验）
    const size_t size;
    const std::string tenant_id;
    const std::string user_key;
    mutable SpinLock lock;
    mutable time_point lease_timeout GUARDED_BY(lock);       // 硬租约
    mutable std::optional<time_point> soft_pin_timeout;      // 软 pin（VIP 对象）
    const bool hard_pinned{false};
   private:
    std::vector<Replica> replicas_;       // 用访问器方法操作，不直接碰
};
```
—— [mooncake-store/include/master_service.h:L803-L839](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L803-L839)（结构体全文见 [:L803-L1103](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L803-L1103)）

**有效性判定**——`size>0` 且至少存在一个「非内存副本 或 句柄未失效」的副本：

```cpp
bool IsValid() const {
    return size > 0 && HasReplica([](const Replica& replica) {
               return !replica.is_memory_replica() ||
                      !replica.has_invalid_mem_handle();
           });
}
```
—— [mooncake-store/include/master_service.h:L1080-L1085](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1080-L1085)

**副本状态机与类型**——`ReplicaStatus` 和 `ReplicaType`：

```cpp
enum class ReplicaStatus { UNDEFINED, INITIALIZED, PROCESSING, COMPLETE, REMOVED, FAILED };
enum class ReplicaType { MEMORY, DISK, LOCAL_DISK, NOF_SSD, ALL };
```
—— [mooncake-store/include/replica.h:L51-L58](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L51-L58)、[mooncake-store/include/allocator.h:L21-L27](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocator.h#L21-L27)

**`PutEnd` 如何把副本标 COMPLETE**——`VisitReplicas` 用谓词选出目标副本，对每个执行 `mark_complete()`；还校验调用者必须是当初 `PutStart` 的那个 client（否则 `ILLEGAL_CLIENT`）：

```cpp
auto MasterService::PutEnd(const UUID& client_id, const std::string& key,
                           const std::string& tenant_id, ReplicaType replica_type)
    -> tl::expected<void, ErrorCode> {
    ...
    if (!accessor.Exists()) return tl::make_unexpected(ErrorCode::OBJECT_NOT_FOUND);
    auto& metadata = accessor.Get();
    if (client_id != metadata.client_id)
        return tl::make_unexpected(ErrorCode::ILLEGAL_CLIENT);
    metadata.VisitReplicas(
        [replica_type](const Replica& replica) { /* 选出目标副本 */ },
        [](Replica& replica) { replica.mark_complete(); });   // ← 标记 COMPLETE
    ...
    metadata.GrantLease(0, default_kv_soft_pin_ttl_);   // 初始无硬租约，给软 pin
}
```
—— [mooncake-store/src/master_service.cpp:L1911-L1981](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1911-L1981)

**`GetReplicaList` 只返回 COMPLETE 副本**——用 `fn_is_completed` 谓词过滤，没有 COMPLETE 副本则 `REPLICA_IS_NOT_READY`：

```cpp
metadata.VisitReplicas(
    &Replica::fn_is_completed, [&replica_list](const Replica& replica) {
        replica_list.emplace_back(replica.get_descriptor());
    });
if (replica_list.empty()) {
    return tl::make_unexpected(ErrorCode::REPLICA_IS_NOT_READY);
}
```
—— [mooncake-store/src/master_service.cpp:L1463-L1472](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1463-L1472)

#### 4.3.4 代码实践

**实践目标**：通过 `ReplicateConfig` 理解「一个对象可以有多少种、多少个副本」。

**操作步骤**：

1. 阅读 [replica.h:L81-L106](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L81-L106) 的 `ReplicateConfig`，圈出 `replica_num`（内存副本数）、`nof_replica_num`（NoF SSD 副本数）、`with_soft_pin`、`with_hard_pin`、`preferred_segments`。
2. 对照 `AllocateAndInsertMetadata`（[master_service.cpp:L1629-L1785](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1629-L1785)）：它先按 `replica_num` 分配内存副本，再按 `nof_replica_num` 分配 SSD 副本，最后把所有副本塞进同一个 `ObjectMetadata`。
3. 思考：若 `replica_num=3` 但集群只能分到 2 个内存段，会发生什么？（提示：看 `HasExpectedReplicaAllocation` 与 `NO_AVAILABLE_HANDLE` 的返回条件，[L1717-L1739](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1717-L1739)）。

**需要观察的现象**：`AllocateAndInsertMetadata` 在分配失败时会设置 `need_mem_eviction_ = true`（[L1674](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1674)），这是「分配失败触发淘汰」的信号——淘汰线程看到它会启动一轮回收腾空间。

**预期结果**：你能说清「`ObjectMetadata` 是一个容器，里面装着若干 `Replica`；副本分内存/SSD/磁盘等类型；只有 COMPLETE 的才对读可见；PutEnd/PutRevoke 是副本状态机翻转的唯一入口」。

#### 4.3.5 小练习与答案

**练习 1**：`PutEnd` 为什么要校验 `client_id != metadata.client_id` 就返回 `ILLEGAL_CLIENT`？

> **参考答案**：`metadata.client_id` 记录的是发起 `PutStart` 的那个客户端（它在 Master 预留了空间）。只有这个客户端才知道把数据写到了哪些副本偏移，也只有它该来「收尾」标 COMPLETE。如果允许别的 client 调 `PutEnd`，就可能把一个还没真正写完数据的副本标成可读，造成脏读。这是一道权限与一致性双重防线。

**练习 2**：一个对象有 3 个内存副本，其中 1 个所在段被卸载了。`GetReplicaList` 会返回什么？

> **参考答案**：访问器打开对象时会清理「句柄失效」的内存副本（`EraseReplicasWithCacheTotalAccounting`，见 [master_service.cpp:L1450-L1453](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1450-L1453)），剩下 2 个有效副本。只要还有至少 1 个 COMPLETE 副本，`GetReplicaList` 就正常返回这 2 个。只有当所有副本都失效/未完成时，对象才被判定无效并整体删除（返回 `OBJECT_NOT_FOUND`）。

---

### 4.4 租约、软硬 Pin：对象的存活与淘汰优先级

#### 4.4.1 概念说明

「租约（lease）」是 Master 用来回答「这个对象现在能不能被删/淘汰」的机制。Mooncake 有三层保护，强度递减：

| 机制 | 字段 | 含义 | 谁设置/影响 |
|---|---|---|---|
| **硬 pin（hard_pinned）** | `hard_pinned`（创建时不可变） | 永不淘汰 | `ReplicateConfig.with_hard_pin` |
| **硬租约（lease）** | `lease_timeout` | 租约未过期期间，`Remove` 非强制会被拒、淘汰跳过 | `GetReplicaList` 授予、`PutEnd` 置零 |
| **软 pin（soft pin）** | `soft_pin_timeout`（VIP 对象） | 淘汰时优先保留；二轮淘汰才可能动 | `PutEnd`/`Get` 续期，按 `soft_ttl` |

直觉上：

- **硬租约**保护「正在被读」的对象——`Get` 时授予 lease，读数据期间（即使读得慢）对象不会被删。
- **软 pin**保护「VIP/热」对象——即使内存吃紧，第一轮淘汰也会尽量绕开它们。
- **硬 pin**则是「钉死」——配置层的强保证。

#### 4.4.2 核心流程

租约的授予是**单调递增**的——只在新 TTL 更大时才更新（避免一个续期反而把租约缩短）：

\[
\text{lease\_timeout} \leftarrow \max(\text{lease\_timeout},\ \text{now} + \text{ttl})
\]

关键生命周期事件：

```
PutEnd:     GrantLease(ttl=0, soft_ttl)        → 硬租约置为 now（无硬保护），软 pin 生效
GetReplicaList: GrantLease(default_kv_lease_ttl=5s, soft_ttl)  → 续硬租约 5 秒
Remove:     若 !force 且 !IsLeaseExpired()      → 拒绝，返回 OBJECT_HAS_LEASE
NeedsLeaseRefresh: 当 lease_timeout <= now + ttl/2  → 客户端需续租
```

淘汰（`BatchEvict`）则利用这些字段排序：第一轮只淘汰「无软 pin」的对象；若淘汰比例不达标，第二轮才考虑软 pin 对象（仅当 `allow_evict_soft_pinned_objects_` 为真）。硬 pin 和硬租约未过期的对象始终绕过。

#### 4.4.3 源码精读

**`GrantLease`——单调续租**：

```cpp
void GrantLease(const uint64_t ttl, const uint64_t soft_ttl) const {
    SpinLocker locker(&lock);
    auto now = std::chrono::system_clock::now();
    lease_timeout = std::max(lease_timeout, now + std::chrono::milliseconds(ttl));
    if (soft_pin_timeout) {
        soft_pin_timeout = std::max(*soft_pin_timeout,
                                    now + std::chrono::milliseconds(soft_ttl));
    }
}
```
—— [mooncake-store/include/master_service.h:L1023-L1034](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1023-L1034)

**`NeedsLeaseRefresh`——半周期续租规则**（剩余寿命不足一半时需要续）：

```cpp
bool NeedsLeaseRefresh(const uint64_t ttl, const uint64_t soft_ttl) const {
    SpinLocker locker(&lock);
    const auto now = std::chrono::system_clock::now();
    if (lease_timeout <= now + std::chrono::milliseconds(ttl / 2)) return true;
    return soft_pin_timeout &&
           *soft_pin_timeout <= now + std::chrono::milliseconds(soft_ttl / 2);
}
```
—— [mooncake-store/include/master_service.h:L1036-L1046](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1036-L1046)

**`Remove` 的租约门禁**——非强制删除时，硬租约未过期直接拒绝：

```cpp
auto MasterService::Remove(const std::string& key, const std::string& tenant_id,
                           bool force) -> tl::expected<void, ErrorCode> {
    ...
    if (!accessor.Exists()) return tl::make_unexpected(ErrorCode::OBJECT_NOT_FOUND);
    auto& metadata = accessor.Get();
    if (!force && !metadata.IsLeaseExpired()) {
        return tl::make_unexpected(ErrorCode::OBJECT_HAS_LEASE);   // ← 租约未过，拒绝
    }
    if (!metadata.AllReplicas(&Replica::fn_is_completed)) {
        return tl::make_unexpected(ErrorCode::REPLICA_IS_NOT_READY);
    }
    if (accessor.HasReplicationTask()) {
        return tl::make_unexpected(ErrorCode::OBJECT_HAS_REPLICATION_TASK);
    }
    accessor.Erase();
    return {};
}
```
—— [mooncake-store/src/master_service.cpp:L2950-L2987](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L2950-L2987)

**两轮淘汰的注释说明**——第一轮绕开软 pin，第二轮在允许时才动软 pin 对象：

```cpp
// BatchEvict evicts objects in a near-LRU way ... It has two passes.
// The first pass only evicts objects without soft pin. The second pass
// prioritizes objects without soft pin, but also allows to evict soft
// pinned objects if allow_evict_soft_pinned_objects_ is true.
void BatchEvict(double evict_ratio_target, double evict_ratio_lowerbound);
```
—— [mooncake-store/include/master_service.h:L769-L777](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L769-L777)

#### 4.4.4 代码实践

**实践目标**：用 `GetReplicaList` 与 `Remove` 的源码，验证「读授予租约、删受租约阻挡」的协同。

**操作步骤**：

1. 在 [master_service.cpp:L1481-L1489](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1481-L1489)（`GetReplicaList` 授予租约）和 [L2962-L2965](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L2962-L2965)（`Remove` 检查租约）之间建立对照。
2. 思考：Client A 刚 `GetReplicaList` 拿到副本正要读，Client B 同时调 `Remove(key)`（非 force）。会发生什么？B 何时才能删成功？
3. 进阶：阅读 `DEFAULT_DEFAULT_KV_LEASE_TTL = 5000`（5 秒）与 `DEFAULT_CLIENT_LIVE_TTL_SEC = 10`（10 秒），比较「对象租约」与「客户端心跳」两个时间尺度的差别。

**需要观察的现象**：`Remove` 除了租约检查，还有两道关卡：`AllReplicas(completed)`（副本须全部 COMPLETE）和 `!HasReplicationTask()`（不能有进行中的复制/迁移任务）。这三道关卡共同保证「不会删掉正在被读写或迁移的对象」。

**预期结果**：你能解释「为什么 `Get` 拿到副本后，数据面慢慢读也不会读到一半被删」——因为 `Get` 授予的硬租约让非强制 `Remove` 被拒，要等租约过期（默认 5 秒）后才可删。**待本地验证**：若本地可跑，对一个对象先 `Get`（不读完），立刻 `Remove`，应观察到 `OBJECT_HAS_LEASE`；等几秒后再 `Remove` 成功。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `GrantLease` 用 `std::max` 而不是直接赋值？

> **参考答案**：并发场景下可能有多个 `Get` 同时续租，或一个 `Get` 续租时网络延迟导致到达 Master 较晚。若直接赋值，一个「携带较旧 now 的迟到的续租请求」会把租约缩短（甚至缩短到已过期），导致对象在读途中被误删。用 `max` 保证租约只能「往后推」，符合「续租只会延长保护期」的语义。

**练习 2**：软 pin 和硬 pin 都叫「pin」，区别是什么？

> **参考答案**：硬 pin（`hard_pinned`，创建时不可变）是**绝对**保护，对象永远不会被淘汰，适合配置层钉死的关键数据。软 pin（`soft_pin_timeout`）是**优先级**保护——第一轮淘汰绕开它，但内存严重不足、且 `allow_evict_soft_pinned_objects_` 为真时仍可能被淘汰。软 pin 有 TTL（默认 30 分钟），是 VIP 对象的「临时优惠」；硬 pin 没有 TTL，是永久豁免。

---

### 4.5 分片与多租户：1024 shard 与 tenant 命名空间

#### 4.5.1 概念说明

LLM 推理每秒会产生海量 `Put`/`Get` 元数据请求，若全挤在一把锁上，Master 就成了瓶颈。Mooncake 用两级结构化解这个问题：

1. **横向分片（sharding）**：把元数据按 `hash(tenant, key)` 分到 **1024 个 shard**，每个 shard 有自己的读写锁（`SharedMutex`）。不同 key 的操作落在不同 shard，互不竞争——锁冲突被摊薄到约 1/1024。
2. **纵向租户（multi-tenancy）**：每个 shard 内部是一个 `unordered_map<tenant_id, TenantState>`。不同租户的相同 key 是**不同的对象**，靠 `tenant_id` 隔开命名空间。

为什么要在 shard 里再套一层 tenant map，而不是直接用 `(tenant, key)` 拼成一个 key 放进一个 map？因为这样能：方便按租户批量操作（如 `GetAllKeys(tenant_id)`、`RemoveAll(tenant_id)`）、让租户成为一等公民的隔离单元、并在 tenant 完全为空时把整个 `TenantState` 从 shard 里删掉回收内存。

> **「默认租户」规则**：空的 `tenant_id` 会被 `NormalizeTenantId` 归一化成字符串 `"default"`。所以不指定 tenant 的请求，全部进 `"default"` 命名空间。
> —— [mooncake-store/include/types.h:L226-L228](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/types.h#L226-L228)

#### 4.5.2 核心流程

定位一个 `(tenant, key)` 的过程是**两级查找**：

```
① 算分片号：shard_idx = getShardIndex(tenant, key)        ← 0..1023
② 打开分片：MetadataShardAccessor{shard_idx}              ← 拿该分片的读写锁
③ 找租户：  shard.tenants[tenant_id] → TenantState
④ 找对象：  tenant_state.metadata[user_key] → ObjectMetadata
```

`getShardIndex` 的 hash 规则是本模块的重点（也是本讲实践任务之一）：

- 若 tenant 归一化后是 `"default"`：`shard = hash(user_key) % 1024`。
- 否则：先 `seed = hash(tenant)`，再用 `boost::hash_combine(seed, user_key)` 把 key 混进去，最后 `seed % 1024`。

即**默认租户只按 key 分；非默认租户按 (tenant, key) 联合分**。两种路径都能把同一 `(tenant, key)` 稳定映射到同一 shard。

#### 4.5.3 源码精读

**分片元数据布局**——每个 shard 一把读写锁 + 一个 tenant→TenantState 的 map：

```cpp
static constexpr size_t kNumShards = 1024;
struct TenantState {
    std::unordered_map<std::string, ObjectMetadata> metadata;
    std::unordered_set<std::string> processing_keys;          // PutStart 中、未 PutEnd 的 key
    std::unordered_map<std::string, const ReplicationTask> replication_tasks;
    std::unordered_map<std::string, const OffloadingTask> offloading_tasks;
    std::unordered_map<std::string, PromotionTask> promotion_tasks;
    std::unordered_map<std::string, std::unordered_set<std::string>> group_members;
    bool Empty() const { ... }
};
struct MetadataShard {
    mutable SharedMutex mutex;
    std::unordered_map<std::string, TenantState> tenants GUARDED_BY(mutex);
};
std::array<MetadataShard, kNumShards> metadata_shards_;
```
—— [mooncake-store/include/master_service.h:L1151-L1176](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1151-L1176)

**`getShardIndex`——本讲的核心 hash 函数**——默认租户只按 key，其他租户联合 (tenant, key)：

```cpp
size_t getShardIndex(const std::string& tenant_id,
                     const std::string& user_key) const {
    const auto normalized_tenant = NormalizeTenantId(tenant_id);
    if (normalized_tenant == "default") {
        return std::hash<std::string>{}(user_key) % kNumShards;       // 只按 key
    }
    size_t seed = std::hash<std::string>{}(normalized_tenant);
    boost::hash_combine(seed, user_key);                              // 联合 (tenant, key)
    return seed % kNumShards;
}
// Legacy helper routes plain keys to the default tenant.
size_t getShardIndex(const std::string& key) const {
    return std::hash<std::string>{}(key) % kNumShards;
}
```
—— [mooncake-store/include/master_service.h:L1264-L1278](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1264-L1278)

**为什么默认租户走「只按 key」分支？** 这是一个**向后兼容**的关键设计。早期 Mooncake 没有 tenant 概念，所有 key 直接 `hash(key) % 1024` 分片。引入多租户后，为了让「不指定 tenant 的老请求」继续落在和以前完全相同的 shard（保证升级前后元数据位置不变），默认租户特意保留原始的 `hash(key)` 公式；只有显式指定了非默认 tenant 时，才用联合 hash。这就是那个「legacy helper routes plain keys to the default tenant」注释的含义。

**`getMetadataShardIndex`——带分组路由的分片查找**——实际定位时还多一步「分组路由」检查：若该 key 被加入了某个 group，则按 `group_id` 算分片（让同组对象聚到同一 shard，便于批量刷新租约与协同淘汰）：

```cpp
size_t MasterService::getMetadataShardIndex(const std::string& tenant_id,
                                            const std::string& key) const {
    const auto normalized_tenant = NormalizeTenantId(tenant_id);
    std::shared_lock<std::shared_mutex> lock(group_routing_mutex_);
    auto it = object_group_ids_.find(MakeTenantScopedKey(normalized_tenant, key));
    if (it == object_group_ids_.end()) {
        return getShardIndex(normalized_tenant, key);     // 普通对象
    }
    return getShardIndex(it->second);                     // 分组成员：按 group_id 分片
}
```
—— [mooncake-store/src/master_service.cpp:L612-L622](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L612-L622)

**租户作用域键**——把 tenant 与 key 拼成无歧义的内部键（中间用 `\0` 分隔，避免 `tenant="a"` `key="b"` 与 `tenant="ab"` `key=""` 这类碰撞）：

```cpp
static std::string MakeTenantScopedKey(const std::string& tenant_id,
                                       const std::string& key) {
    const auto normalized_tenant = NormalizeTenantId(tenant_id);
    std::string scoped_key;
    scoped_key.reserve(normalized_tenant.size() + key.size() + 1);
    scoped_key.append(normalized_tenant);
    scoped_key.push_back('\0');                  // 分隔符
    scoped_key.append(key);
    return scoped_key;
}
```
—— [mooncake-store/include/master_service.h:L1252-L1261](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1252-L1261)

#### 4.5.4 代码实践（本讲核心实践之一）

**实践目标**：亲手验证 `getShardIndex` 如何由 tenant 与 key 共同决定分片号，并理解多租户隔离。

**操作步骤**：

1. 打开 [master_service.h:L1264-L1278](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1264-L1278)，确认两个分支。
2. 用下表（示例，非项目原有代码）手工推演 `shard_idx`（取 `std::hash<std::string>` 是平台相关的，这里只看**决定因子**）：

   | 调用 | tenant 归一化 | 走哪条分支 | shard 由谁决定 |
   |---|---|---|---|
   | `getShardIndex("", "keyA")` | `"default"` | 默认分支 | `hash("keyA") % 1024` |
   | `getShardIndex("default", "keyA")` | `"default"` | 默认分支 | `hash("keyA") % 1024` |
   | `getShardIndex("acme", "keyA")` | `"acme"` | 联合分支 | `combine(hash("acme"),"keyA") % 1024` |
   | `getShardIndex("acme", "keyB")` | `"acme"` | 联合分支 | `combine(hash("acme"),"keyB") % 1024` |

3. 推论验证：`("", "keyA")` 和 `("default", "keyA")` 是否落在**同一个** shard？（应相同，因为都归一化成 `"default"` 且公式一致。）
4. 推论验证：`("acme", "keyA")` 和 `("beta", "keyA")` 这两个**不同租户的同一个 key**，是否被隔成两个不同对象？（是——它们大概率落在不同 shard；即便恰好同 shard，shard 内的 `tenants["acme"]` 与 `tenants["beta"]` 也会把它们分开。）

**需要观察的现象**：多租户隔离是**双层**的——外层 shard 号由 `(tenant,key)` 联合决定（把不同租户尽量分散到不同 shard），内层 shard 的 `tenants` map 用 `tenant_id` 做键二次隔离。两层都到位，才能保证「同 key 不同 tenant 是完全独立的对象」。

**预期结果**：你能用自己的话解释——「默认租户为了兼容老逻辑只按 key 分片；非默认租户按 tenant 与 key 联合 hash 分片；分片内再用 tenant 命名空间二次隔离；分组对象额外按 group_id 聚拢。」**待本地验证**：若你想看真实 hash 值，可在本地写一个最小 C++ 片段调用 `std::hash<std::string>{}("keyA") % 1024` 对照（属于示例代码，非项目原有）。

#### 4.5.5 小练习与答案

**练习 1**：如果把 `kNumShards` 从 1024 改成 1，系统还能正确工作吗？会有什么影响？

> **参考答案**：功能上仍然正确——所有 key 都落到唯一那个 shard，`getShardIndex` 仍返回合法的 `0`。但性能会急剧下降：所有元数据操作竞争同一把 `SharedMutex`，并发吞吐退化为串行。1024 是「锁冲突摊薄」与「固定内存开销（1024 个 MetadataShard 对象 + 锁）」之间的工程折中。

**练习 2**：`RemoveAll(const std::string& tenant_id)`（[master_service.h:L529](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L529)）要删某租户的全部对象，它必须遍历几个 shard？为什么不能用 `getShardIndex` 直接定位？

> **参考答案**：必须遍历**全部 1024 个 shard**。因为同一租户的不同 key 会（按联合 hash）散布在所有 shard 上——`getShardIndex(tenant, key1)` 与 `getShardIndex(tenant, key2)` 可能指向不同 shard。租户不是「独占某几个 shard」的，而是「在所有 shard 里都可能有属于自己的 `TenantState` 条目」。所以按租户的批量操作本质上是「扫所有 shard、挑出该 tenant 的 `TenantState`」。

---

### 4.6 核心 RPC 语义：PutStart / PutEnd / GetReplicaList / Remove / Ping

#### 4.6.1 概念说明

前面几节拆开了各个机制，本节把它们收束到「Client 调用 Master 时，每个 RPC 到底返回什么、可能报什么错」。这是本讲实践任务的核心：**列出 `MountSegment` / `PutStart` / `GetReplicaList` / `Ping` 的返回语义与可能的 `ErrorCode`**。

`ErrorCode` 是一个覆盖全系统的枚举（[types.h:L304-L410](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/types.h#L304-L410)），本讲涉及的主要是「对象/副本」段（`-703`～`-714`）和「参数/段」段。

#### 4.6.2 核心流程

下表汇总四个核心 RPC（连同 `Remove` 作为补充）的返回值与错误码。这是本讲的「速查表」，建议记牢：

| RPC | 成功返回 | 可能的 `ErrorCode`（典型） |
|---|---|---|
| **MountSegment** | `void`（空） | `INVALID_PARAMS`（base/size 非法）、`UNAVAILABLE_IN_CURRENT_STATUS`（段已存在但状态非 OK）、`INTERNAL_ERROR`（ping 队列满）；`SEGMENT_ALREADY_EXISTS` 被当作幂等成功 |
| **PutStart** | `vector<Replica::Descriptor>`（分配好的副本地址） | `INVALID_PARAMS`（key 空/size 0/replica_num 全 0 等）、`OBJECT_ALREADY_EXISTS`（key 已存在且未过期）、`NO_AVAILABLE_HANDLE`（空间不足/分配失败） |
| **GetReplicaList** | `GetReplicaListResponse{replicas, lease_ttl_ms}` | `OBJECT_NOT_FOUND`（key 不存在）、`REPLICA_IS_NOT_READY`（无 COMPLETE 副本） |
| **Remove** | `void`（空） | `OBJECT_NOT_FOUND`、`OBJECT_HAS_LEASE`（非 force 且租约未过）、`REPLICA_IS_NOT_READY`（有未完成副本）、`OBJECT_HAS_REPLICATION_TASK`（有迁移任务） |
| **Ping** | `PingResponse{view_version_id, client_status}` | `INTERNAL_ERROR`（ping 队列满） |

#### 4.6.3 源码精读

**`PutStart` 的参数校验与错误返回**——多种 `INVALID_PARAMS` 场景：

```cpp
auto MasterService::PutStart(const UUID& client_id, const std::string& key,
                             const std::string& tenant_id,
                             const uint64_t slice_length,
                             const ReplicateConfig& config)
    -> tl::expected<std::vector<Replica::Descriptor>, ErrorCode> {
    const auto object_id = MakeObjectIdentity(key, tenant_id);
    if ((config.replica_num == 0 && config.nof_replica_num == 0) ||
        key.empty() || slice_length == 0) {
        return tl::make_unexpected(ErrorCode::INVALID_PARAMS);
    }
    ...
    [[maybe_unused]] auto object_operation_lock =
        AcquireObjectOperationLock(object_id.tenant_id, object_id.user_key);
    ...
    // 命中已存在对象 → OBJECT_ALREADY_EXISTS；分配失败 → NO_AVAILABLE_HANDLE（见 AllocateAndInsertMetadata）
}
```
—— [mooncake-store/src/master_service.cpp:L1787-L1800](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1787-L1800)（`OBJECT_ALREADY_EXISTS` 见 [:L1861-L1864](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1861-L1864)；`NO_AVAILABLE_HANDLE` 见 AllocateAndInsertMetadata [:L1666-L1676](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1666-L1676)）

> 注意 `PutStart` 里有一个「分组重定位」细节：它先用 `getMetadataShardIndex` 查到一个 shard，但如果该 key 属于某 group，真正应落的 shard（按 `group_id` 算）可能不同。代码在 [:L1881-L1896](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1881-L1896) 检测到不一致时会释放当前 shard、换到正确 shard 重试（`retry_shard_idx`）。

**`AllocateAndInsertMetadata` 的空间不足错误**——分配失败设淘汰标志并返回 `NO_AVAILABLE_HANDLE`：

```cpp
auto allocation_result = allocation_strategy_->Allocate(...);
if (!allocation_result.has_value()) {
    ...
    if (write_mode != ReplicaWriteMode::FLEXIBLE_DUAL_REPLICA) {
        need_mem_eviction_ = true;                              // 触发淘汰
        return tl::make_unexpected(ErrorCode::NO_AVAILABLE_HANDLE);
    }
}
```
—— [mooncake-store/src/master_service.cpp:L1662-L1680](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1662-L1680)

**`GetReplicaList` 的两种失败**——不存在 / 无可用副本：

```cpp
if (!accessor.Exists()) {
    return tl::make_unexpected(ErrorCode::OBJECT_NOT_FOUND);
}
...
if (replica_list.empty()) {        // 没有 COMPLETE 副本
    return tl::make_unexpected(ErrorCode::REPLICA_IS_NOT_READY);
}
```
—— [mooncake-store/src/master_service.cpp:L1457-L1472](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1457-L1472)

**`ErrorCode` 全集**——本讲涉及的错误码都能在这里找到定义：

```cpp
enum class ErrorCode : int32_t {
    OK = 0,
    INTERNAL_ERROR = -1,
    ...
    NO_AVAILABLE_HANDLE = -200,
    INVALID_PARAMS = -600,
    ILLEGAL_CLIENT = -601,
    INVALID_WRITE = -700,
    REPLICA_IS_NOT_READY = -703,
    OBJECT_NOT_FOUND = -704,
    OBJECT_ALREADY_EXISTS = -705,
    OBJECT_HAS_LEASE = -706,
    OBJECT_HAS_REPLICATION_TASK = -708,
    OBJECT_REPLICA_BUSY = -714,
    ...
    UNAVAILABLE_IN_CURRENT_STATUS = -1010,
    ...
};
```
—— [mooncake-store/include/types.h:L304-L410](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/types.h#L304-L410)

#### 4.6.4 代码实践（本讲核心实践之二）

**实践目标**：完成规格要求的实践任务——「阅读 `master_service.h`，列出 `MountSegment`/`PutStart`/`GetReplicaList`/`Ping` 各自的返回语义与可能的 `ErrorCode`，并解释分片索引是如何由 tenant 与 key 共同决定的」。

**操作步骤**：

1. **列返回语义**：在 [master_service.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h) 中找到这四个方法的声明与文档注释（`MountSegment` 在 [:L88-L98](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L88-L98)、`PutStart` 在 [:L308-L319](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L308-L319)、`GetReplicaList` 在 [:L292-L299](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L292-L299)、`Ping` 在 [:L547-L554](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L547-L554)）。每个方法的 `@return` 注释列出了它能返回的 `ErrorCode`。
2. **交叉验证**：对照本节 4.6.2 的速查表，确认注释里写的错误码与实现里 `tl::make_unexpected(...)` 实际返回的一致。例如 `PutStart` 注释写「`OBJECT_NOT_FOUND` if exists」——这是个历史笔误，实际实现里「key 已存在」返回的是 `OBJECT_ALREADY_EXISTS`（见 [:L1861-L1864](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1861-L1864)）。**以实现为准**。
3. **解释分片索引**：复述 4.5.4 的结论——`getShardIndex(tenant, key)`：默认租户（含空 tenant）只按 `hash(key)`；非默认租户用 `boost::hash_combine(hash(tenant), key)` 联合 hash；都 `% 1024`。

**需要观察的现象**：源码注释（`@return`）描述的是「设计意图」，而 `.cpp` 里的 `tl::make_unexpected` 是「真实行为」。两者偶尔会有出入（如上文的 `PutStart` 注释笔误），调试时**一定要以实现为准**。

**预期结果**：你能不看任何资料，口述出本节 4.6.2 那张表，并解释 `getShardIndex` 的双分支逻辑与默认租户的兼容性考量。

#### 4.6.5 小练习与答案

**练习 1**：Client 调 `PutStart` 收到 `NO_AVAILABLE_HANDLE`，它该怎么处理？

> **参考答案**：这个错误表示「当前没有足够空间分配副本」。Master 内部已经设置了 `need_mem_eviction_ = true` 触发一轮淘汰。Client 通常会**短暂等待后重试** `PutStart`——等淘汰线程腾出空间后，重试大概率成功。这正是上一讲提到的「分配失败 → 触发淘汰 → 重试」旁路。若持续失败，说明集群容量真的不足，需要 mount 更多段。

**练习 2**：`Ping` 返回的 `client_status = NEED_REMOUNT` 是什么意思？Client 收到后该做什么？

> **参考答案**：意味着 Master 的 `ok_client_` 里没有这个 client——通常是因为它之前心跳超时被 `ClientMonitorFunc` 清掉了（其段已被卸载、副本已清理）。Client 收到 `NEED_REMOUNT` 后应当调用 `ReMountSegment` 重新挂载自己的所有内存段，把资源池视图重建回来，之后才能继续正常 `Put`/`Get`。`ReMountSegment` 把 client 重新加入 `ok_client_` 并恢复状态（见 [master_service.cpp:L541-L585](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L541-L585)）。

---

## 5. 综合实践

**任务**：以一个具体的 key `"kvlayer_42"`、tenant `"acme"` 为线索，把本讲的「挂载 → 分片定位 → 元数据模型 → 租约 → RPC」串成一条完整的「对象一生」叙事，并完成一张标注图。

**步骤**：

1. **分片定位**：写出 `getShardIndex("acme", "kvlayer_42")` 走哪条分支、由哪些量决定（答：非默认租户，走联合分支，`seed = hash("acme")`，`hash_combine(seed, "kvlayer_42")`，再 `% 1024`）。
2. **画对象结构图**：画出一个 `MetadataShard` → `TenantState("acme")` → `ObjectMetadata("kvlayer_42")` → `vector<Replica>` 的嵌套，标注 `client_id`、`size`、`lease_timeout`、`soft_pin_timeout`、`hard_pinned` 等字段，以及副本的 `PROCESSING/COMPLETE` 状态。
3. **标注对象一生**：在这张图上标出五个时刻及其触发的 RPC 与字段变化：
   - (a) 某 Client `MountSegment` 登记内存段（资源池建立）；
   - (b) `PutStart("acme","kvlayer_42")` 分配副本、副本=PROCESSING、对象进 `processing_keys`；
   - (c) `PutEnd` 标记 COMPLETE、`GrantLease(0, soft_ttl)`（软 pin 生效）；
   - (d) 别人 `GetReplicaList` 授予硬租约 5 秒、期间 `Remove` 被 `OBJECT_HAS_LEASE` 拒；
   - (e) 租约过期后 `Remove` 成功，`ObjectMetadata` 析构、`dec_key_count`。
4. **诊断题**（用源码验证）：
   - (a) 步骤 中如果该 Client 在 (b) 之后、(c) 之前崩溃（停止 Ping），这个 PROCESSING 对象会怎样？（提示：`ClientMonitorFunc` 过期回收 + `put_start_release_timeout` 兜底，[master_service.h:L1806-L1807](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_service.h#L1806-L1807)）
   - (b) 如果 tenant 传成空串 `""`，对象会落在哪个命名空间、哪个分片公式？（提示：`NormalizeTenantId` → `"default"`，走「只按 key」分支。）

**预期产出**：一张自洽的「对象一生」标注图 + 两道诊断题的源码级答案。不确定处标注「待本地验证」并写出推断依据。

## 6. 本讲小结

- `MasterService` 是 Store 的「元数据大脑」，内部由**段管理器**（`segment_manager_`）、**分片元数据**（`metadata_shards_[1024]`）、**客户端租约表**（`ok_client_` + `client_ping_queue_`）和**多条后台线程**（监控/淘汰/快照/任务清理）组成，靠**严格锁层级**（client → shard → segment）和**快照共享锁**保证一致与不死锁。
- **段挂载**是「Client 把内存元数据登记给 Master、Master 创建分配器」的过程，与**心跳**绑定——`client_live_ttl_sec`（默认 10 秒）没 ping 就由 `ClientMonitorFunc` 自动卸载段并清理副本。
- **对象元数据模型**：`ObjectMetadata` 持有 `vector<Replica>`，副本状态机 `PROCESSING → COMPLETE`，**只有 COMPLETE 对 Get 可见**；`PutEnd`/`PutRevoke` 是状态翻转的唯一入口，且校验调用者必须是 `PutStart` 的那个 client。
- **租约三层保护**：硬 pin（永不淘汰）> 硬租约（`Get` 授予、`Remove` 非强制时被拒）> 软 pin（VIP 对象，淘汰优先保留）。`GrantLease` 用 `max` 单调续租。
- **分片 + 多租户**：1024 shard 按 `getShardIndex(tenant,key)` 分——默认租户只按 `hash(key)`（向后兼容），非默认租户用 `boost::hash_combine(hash(tenant),key)` 联合 hash；shard 内再用 `tenants[tenant_id]` 命名空间二次隔离。
- **核心 RPC 语义**：`MountSegment`（幂等挂载）、`PutStart`（分配副本，可能 `INVALID_PARAMS`/`OBJECT_ALREADY_EXISTS`/`NO_AVAILABLE_HANDLE`）、`GetReplicaList`（返回 COMPLETE 副本+授予 lease，可能 `OBJECT_NOT_FOUND`/`REPLICA_IS_NOT_READY`）、`Remove`（受租约/副本态/迁移任务三重门禁）、`Ping`（续命+返回 `view_version` 与 `client_status`）。

## 7. 下一步学习建议

- **分配策略**：精读 `allocation_strategy.h` 与 `AllocateAndInsertMetadata` 的分配段（[master_service.cpp:L1629-L1785](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1629-L1785)），理解 `random` / `free_ratio_first` / `cxl` 三种策略如何选段、`preferred_segments` 如何影响局部性。对应「分配策略」主题讲义。
- **淘汰算法**：阅读 `BatchEvict` / `EvictionThreadFunc` 与 `count_min_sketch.h`，理解近 LRU 淘汰、两轮淘汰（软 pin 兜底）、以及 `need_mem_eviction_` 触发链。
- **多级存储与 promotion**：本讲多次提到 `LOCAL_DISK`/`NOF_SSD` 副本与 promotion-on-hit（`TryPushPromotionQueue`）。后续可阅读 `file_storage.h` 与 [master_service.cpp:L1360-L1380](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1360-L1380) 的 promotion 任务流，理解内存未命中如何回源 SSD 并异步提升回内存。
- **快照与高可用**：`MasterService` 持有 `MetadataSerializer`、`SnapshotCatalogStore`、`ha` 相关成员。若关心 Master 宕机后的元数据恢复，可顺着 `SnapshotThreadFunc` / `RestoreState` 阅读。
- **推荐源码入口**：以本讲的 `PutStart`（[:L1787](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1787)）、`GetReplicaList`（[:L1444](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L1444)）、`ClientMonitorFunc`（[:L5813](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L5813)）三个函数为锚点向外辐射，结合 `master_service.h` 的内嵌结构体（`ObjectMetadata`/`TenantState`/`MetadataShard`/各 Accessor），就能把控制面的元数据机制读透。
