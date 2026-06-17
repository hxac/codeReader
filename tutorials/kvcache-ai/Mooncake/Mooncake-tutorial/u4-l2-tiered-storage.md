# U4-L2：分层存储与 SSD 卸载

本讲义讲解 Mooncake Store 的分层存储机制（DRAM + SSD/NVMe 两级缓存），包括 L1 内存缓存到 L2 磁盘的异步卸载流程、冷数据 L2→L1 主动提升机制、FileStorage 抽象接口、PromotionTaskItem 任务调度，以及如何通过分层存储在成本与性能之间取得平衡。

---

## 最小模块 1：分层存储架构

### 概念说明

Mooncake Store 采用**两级分层存储架构**，将分布式内存（L1，DRAM）与本地 SSD（L2，NVMe）结合，在保持热路径性能的同时突破 DRAM 容量限制。核心思想是：

- **热数据**留在 L1 分布式内存中，通过 RDMA 零拷贝访问，延迟最低
- **温/冷数据**异步卸载到 L2 本地 SSD，成本仅为 DRAM 的 \(1/10\) 至 \(1/20\)
- **卸载透明化**：应用无感知，`Put` 避免驱逐时自动持久化，`Get` 找不到内存副本时自动回退到 SSD

这种架构在多轮对话场景中表现尤为出色：早期对话的 KV cache 已冷却，卸载到 SSD 后不占用宝贵的 DRAM，而当前轮次的热数据仍在内存中，访问延迟与纯内存方案几乎相同。

### 伪代码或流程

```
架构层级（从上到下）：

Application (vLLM, etc.)
    │ MooncakeDistributedStore API
    ▼
Real Client Process
    │
    ├─ FileStorage（协调器）
    │  ├─ StorageBackendInterface（磁盘抽象）
    │  │  ├─ BucketStorageBackend（默认：桶文件）
    │  │  ├─ StorageBackendAdaptor（FilePerKey：一对象一文件）
    │  │  └─ OffsetAllocatorStorageBackend（预分配大文件 + 偏移量分配器）
    │  │
    │  ├─ ClientBuffer（O_DIRECT 对齐的暂存区）
    │  ├─ Heartbeat Thread（定时驱动卸载）
    │  └─ ClientBuffer GC Thread（回收暂存区）
    │
    └─ In-memory distributed KV cache（Transfer Engine / RDMA）
         │
         ▼
      Local SSD / NVMe
```

数据访问逻辑：

```
Get(key) 的分层查询顺序：
1. 查询 Master，获取副本列表
2. 如果有 MEMORY 副本 → 直接 RDMA 读（零拷贝，最快）
3. 如果只有 LOCAL_DISK 副本 → BatchGetOffloadObject → Transfer Engine 拉取
4. 如果都没有 → Key not found

Put(key, value) 的生命周期：
1. 写入本地 DRAM Segment（MEMORY 副本）
2. Heartbeat 驱动卸载决策（Master 根据容量/访问频率选择）
3. 如果选中 → OffloadObjects → 写入 SSD（LOCAL_DISK 副本）
4. Master 更新副本列表，移除 MEMORY 副本，添加 LOCAL_DISK 副本
```

### 原理分析

分层存储的**核心权衡**是成本与性能的平衡：

- **延迟**：DRAM 访问 ~\(100\) ns，NVMe SSD ~\(10\mu\)s，相差 100 倍
- **容量成本**：DRAM ~\(\$10\)/GB，SSD ~\(\$0.5\)/GB，相差 20 倍
- **带宽**：单通道 DDR4 ~\(25\) GB/s，PCIe 4.0 NVMe ~\(7\) GB/s

Mooncake 通过**访问频率感知**实现自动分层：

- 热数据（高频访问）→ 留在 L1
- 冷数据（低频/不再访问）→ 卸载到 L2
- Master 根据 LRU 模型或容量压力主动调度卸载

关键设计点：

1. **零拷贝路径**：SSD 读通过 `ClientBuffer`（预注册 RDMA 内存）→ Transfer Engine → 应用内存，全程无 CPU 拷贝
2. **异步卸载**：Heartbeat 线程后台执行，不阻塞 Put 操作
3. **副本一致性**：Master 维护副本列表，确保客户端始终读到最新数据

### 代码实践

分层存储的核心组件定义在 `file_storage.h` 中：

```cpp
// FileStorage：顶层协调器，拥有存储后端、暂存区和后台线程
class FileStorage {
    std::shared_ptr<StorageBackendInterface> storage_backend_;  // 磁盘抽象
    std::shared_ptr<ClientBufferAllocator> client_buffer_allocator_;  // 暂存区分配器
    std::thread heartbeat_thread_;      // 驱动 L1→L2 卸载
    std::thread client_buffer_gc_thread_;  // 回收暂存区
    std::atomic<bool> enable_offloading_;  // 是否允许卸载
};
```

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/file_storage.h#L12-L148

存储后端接口（三种实现共享同一抽象）：

```cpp
class StorageBackendInterface {
    // 批量卸载到磁盘（L1 → L2）
    virtual tl::expected<int64_t, ErrorCode> BatchOffload(
        const std::unordered_map<std::string, std::vector<Slice>>& batch_object,
        std::function<ErrorCode(...)> complete_handler,
        std::function<void(...)> eviction_handler = nullptr) = 0;

    // 批量从磁盘加载（L2 → 暂存区）
    virtual tl::expected<void, ErrorCode> BatchLoad(
        std::unordered_map<std::string, Slice>& batched_slices) = 0;

    // 启动时扫描磁盘元数据，重新注册到 Master
    virtual tl::expected<void, ErrorCode> ScanMeta(
        const std::function<ErrorCode(...)>& handler) = 0;
};
```

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/storage_backend.h#L249-L290

三种后端的配置与选择：

```cpp
struct FileStorageConfig {
    StorageBackendType storage_backend_type;  // kBucket（默认）, kFilePerKey, kOffsetAllocator
    std::string storage_filepath;             // 磁盘根目录
    int64_t local_buffer_size;                // ClientBuffer 大小（默认 1.28GB）
    int64_t total_size_limit;                 // SSD 总容量上限（默认 2TB）
    uint32_t heartbeat_interval_seconds;     // 心跳间隔（默认 10s）
    bool use_uring;                           // 是否使用 io_uring（默认 false）
};
```

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/storage_backend.h#L203-L247

FileStorage 初始化流程（注册本地内存 + 启动后台线程 + 扫描磁盘元数据）：

```cpp
tl::expected<void, ErrorCode> FileStorage::Init() {
    // 1. 注册 ClientBuffer 到 Transfer Engine（用于 RDMA 零拷贝）
    auto register_memory_result = RegisterLocalMemory();

    // 2. 初始化存储后端（创建目录/扫描现有文件）
    auto init_storage_backend_result = storage_backend_->Init();

    // 3. 检查是否允许卸载（容量/配额检查）
    auto enable_offloading_result = IsEnableOffloading();
    enable_offloading_ = enable_offloading_result.value();

    // 4. 向 Master 挂载 LOCAL_DISK segment
    client_->MountLocalDiskSegment(enable_offloading_);

    // 5. 扫描磁盘元数据，重新注册所有对象（恢复重启前的状态）
    storage_backend_->ScanMeta([this](auto& keys, auto& metadatas) {
        for (auto& metadata : metadatas) {
            metadata.transport_endpoint = local_rpc_addr_;  // 标记为本机
        }
        client_->NotifyOffloadSuccess(tasks, metadatas);  // 通知 Master
    });

    // 6. 启动心跳线程（周期性驱动卸载）
    heartbeat_thread_ = std::thread([this]() {
        while (heartbeat_running_.load()) {
            Heartbeat();
            std::this_thread::sleep_for(std::chrono::seconds(config_.heartbeat_interval_seconds));
        }
    });

    // 7. 启动暂存区 GC 线程（回收超时的 batch）
    client_buffer_gc_thread_ = std::thread(&FileStorage::ClientBufferGCThreadFunc, this);
}
```

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/src/file_storage.cpp#L234-L321

### 练习题

1. **概念题**：为什么 Mooncake 需要分层存储，而不是直接用无限大的 DRAM？

2. **设计题**：BucketStorageBackend、StorageBackendAdaptor（FilePerKey）、OffsetAllocatorStorageBackend 三种后端各有什么优劣？分别适用于什么场景？

3. **推理题**：在多轮对话场景中，为什么 SSD 卸载能显著提升有效容量而不增加延迟？

4. **实践题**：假设你有一个 100GB 的 KV cache workload，其中 80% 的数据访问集中在 20% 的热数据上。如何配置 Mooncake 的分层存储参数（`local_buffer_size`、`total_size_limit`、`heartbeat_interval_seconds`）来优化成本与性能？

### 答案

1. **答案**：DRAM 成本高（~\(\$10\)/GB），而 SSD 成本低（~\(\$0.5\)/GB）。对于 TB 级别的 KV cache，纯 DRAM 方案成本过高。分层存储通过访问频率感知，将热数据留在 DRAM，冷数据卸载到 SSD，在保持性能的同时大幅降低成本。

2. **答案**：
   - **BucketStorageBackend**（默认）：对象聚合成桶（256MB 或 500 key），减少文件数量，适合大规模场景，但写放大较高。
   - **FilePerKey**：每个对象一个文件，简单易调试，但百万级对象会导致目录膨胀和 inode 耗尽，适合小规模测试。
   - **OffsetAllocatorStorageBackend**：预分配大文件（`kv_cache.data`），通过偏移量分配器管理空间，无文件数量问题，但重启后元数据丢失（V1 限制）。

3. **答案**：多轮对话中，早期轮次的 KV cache 在后续轮次中访问频率极低（冷却数据）。卸载到 SSD 后，这些数据不占用 DRAM，当前轮次的热数据仍在内存中，访问延迟与纯内存方案几乎相同。因此，有效容量（可同时容纳的历史轮次）显著提升，而热路径延迟不变。

4. **答案**：
   - `local_buffer_size`：设为 20GB（容纳 20% 热数据 + 余量），确保热数据常驻内存
   - `total_size_limit`：设为 1TB（容纳全部 100GB 数据 + 扩展空间）
   - `heartbeat_interval_seconds`：设为 5–10s（平衡卸载响应速度与心跳开销）
   - 可选：启用 `use_uring=true` 以提升 SSD I/O 并发度

---

## 最小模块 2：SSD 卸载（L1 → L2）

### 概念说明

SSD 卸载是分层存储的**核心机制**，将冷数据从 L1（DRAM）异步迁移到 L2（SSD），释放宝贵的内存空间。卸载由 **Heartbeat 线程**驱动，完全在后台执行，对应用透明。

卸载触发条件由 Master 决策，依据包括：
- **容量压力**：L1 Segment 使用率超过阈值（如 80%）
- **访问频率**：基于 LRU 模型，优先卸载最久未访问的对象
- **显式指令**：用户可通过 API 手动触发卸载

卸载流程包括：
1. **心跳交互**：客户端向 Master 发送心跳，Master 返回需要卸载的对象列表（`{key → size}`）
2. **内存读取**：从本地 Segment 读取对象数据（`Slice`）
3. **淘汰旧数据**（可选）：如果 SSD 容量受限，先删除最旧的桶/文件
4. **磁盘写入**：通过 `StorageBackend` 批量写入 SSD
5. **通知 Master**：卸载成功后，Master 更新副本列表（移除 MEMORY，添加 LOCAL_DISK）

### 伪代码或流程

```
Heartbeat 线程的主循环（每 heartbeat_interval_seconds 秒执行一次）：

while (running) {
    // STEP 1: 向 Master 发送心跳，获取卸载决策
    offloading_objects = Master.OffloadObjectHeartbeat(enable_offloading)
    // 返回：[{tenant_id, key, size}, ...]

    if (offloading_objects is empty) continue

    // STEP 2: 从本地 Segment 读取对象数据
    for each task in offloading_objects {
        slices = BatchQuery(task.key, task.tenant_id)
        // 返回：Slice{ptr, size}，指向本地 DRAM 中的数据
    }

    // STEP 3: （可选）淘汰 SSD 上的旧数据
    if (SSD is full) {
        evicted_keys = PrepareEviction(required_size)
        Master.BatchEvictDiskReplica(evicted_keys)  // 通知 Master 删除副本
        FinalizeEviction()  // 物理删除文件
    }

    // STEP 4: 写入 SSD
    StorageBackend.BatchOffload(slices, complete_handler, eviction_handler)
    // BucketBackend: 聚合成桶，写入 .bucket + .meta 文件
    // FilePerKey: 写入独立文件
    // OffsetAllocator: 分配偏移量，写入 kv_cache.data

    // STEP 5: 通知 Master 卸载成功
    complete_handler(keys, metadatas) {
        Master.NotifyOffloadSuccess(keys, metadatas)
        // Master 更新副本列表：移除 MEMORY，添加 LOCAL_DISK（transport_endpoint=本机 RPC 地址）
    }

    sleep(heartbeat_interval_seconds)
}
```

### 原理分析

**分组策略（BucketStorageBackend）**：

为了减少文件数量和 I/O 次数，BucketStorageBackend 将对象聚合成**桶**（bucket）：

- 桶 ID 生成：`timestamp << 12 | sequence`，单调递增，确保按创建时间排序
- 分组条件：
  - 桶大小 ≥ `bucket_size_limit`（默认 256MB）
  - 桶内对象数 ≥ `bucket_keys_limit`（默认 500）
- 未填满一个桶的对象暂存在 `ungrouped_offloading_objects_`，下次心跳继续累积

**淘汰策略（BucketStorageBackend）**：

当 SSD 容量接近上限（`max_total_size`）时，触发两阶段淘汰：

- **FIFO**：淘汰 `buckets_.begin()`（最旧的桶）
- **LRU**：淘汰 `last_access_ns_` 最小的桶（最久未被读取）
- **两阶段协议**：
  - `PrepareEviction`：从元数据中移除桶，返回待淘汰的键和文件路径
  - `FinalizeEviction`：等待 `inflight_reads_` 归零后，物理删除 `.bucket` 和 `.meta` 文件

**GPU D2H 暂存**：

如果对象数据在 GPU 内存中（如 vLLM 的 KV cache），卸载前需要先拷贝到主机内存：

```cpp
// 检测指针是否在设备内存
if (IsDevicePointer(slice.ptr, &device_id)) {
    SetDevice(device_id);
    // 从固定内存池获取暂存缓冲区（避免频繁 malloc/free）
    auto buf = pinned_buffer_pool_->Acquire(slice.size);
    // 异步拷贝到主机
    CopyDeviceToHost(buf.data, slice.ptr, slice.size);
    // 替换 slice 为主机内存指针
    host_slices.emplace_back(Slice{buf.data, slice.size});
    staging_bufs.push_back(buf);  // 稍后释放回池
}
```

**io_uring 优化（可选）**：

当 `use_uring=true` 时，使用 Linux io_uring 替代 POSIX `pread`/`pwrite`：

- **线程本地环**：每个线程独立的 `io_uring` 实例，避免锁竞争
- **固定缓冲区**：`ClientBuffer` 注册为 `io_uring` fixed buffer，避免 per-I/O `mmap`/`munmap`
- **批量提交**：单次提交最多 32 个 SQE，充分利用 NVMe 队列深度

### 代码实践

Heartbeat 线程的主循环：

```cpp
tl::expected<void, ErrorCode> FileStorage::Heartbeat() {
    std::vector<OffloadTaskItem> offloading_objects;

    // STEP 1: 向 Master 发送心跳，获取卸载决策
    {
        MutexLocker locker(&offloading_mutex_);
        auto heartbeat_result = client_->OffloadObjectHeartbeat(
            enable_offloading_, offloading_objects);
        if (!heartbeat_result) {
            // 处理 Master 重启等异常情况
            if (heartbeat_result.error() == ErrorCode::SEGMENT_NOT_FOUND) {
                // Master 丢失了本机的 LOCAL_DISK segment，重新挂载
                client_->MountLocalDiskSegment(enable_offloading_);
                // 触发异步 ScanMeta，重新注册对象元数据
                rescan_future_ = std::async(std::launch::async, [this]() {
                    ReRegisterOffloadedObjects();
                });
                // 重试心跳
                heartbeat_result = client_->OffloadObjectHeartbeat(
                    enable_offloading_, offloading_objects);
            }
        }
    }

    if (offloading_objects.empty()) return {};

    // STEP 2: 执行卸载（包含内存读取、SSD 写入、通知 Master）
    auto offload_result = OffloadObjects(offloading_objects);

    // STEP 3: 处理 L2→L1 提升任务（见下一模块）
    (void)ProcessPromotionTasks();

    return offload_result;
}
```

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/src/file_storage.cpp#L582-L685

OffloadObjects 的核心逻辑：

```cpp
tl::expected<void, ErrorCode> FileStorage::OffloadObjects(
    const std::vector<OffloadTaskItem>& offloading_objects) {

    // STEP 1: 按桶分组（仅 BucketStorageBackend）
    std::vector<std::vector<std::string>> buckets_keys;
    if (auto bucket_backend =
            std::dynamic_pointer_cast<BucketStorageBackend>(storage_backend_)) {
        bucket_backend->AllocateOffloadingBuckets(storage_object_sizes, buckets_keys);
    } else {
        // FilePerKey / OffsetAllocator：不分桶，全部对象一批处理
        buckets_keys.emplace_back(all_keys);
    }

    // STEP 2: 对每个桶执行卸载
    for (const auto& keys : buckets_keys) {
        // STEP 2a: 从本地 Segment 读取对象数据
        std::unordered_map<std::string, std::vector<Slice>> batch_object;
        for (const auto& storage_key : keys) {
            auto query_result = BatchQuerySegmentSlices(
                user_keys, tenant_id, user_batch_object);
            // 返回：Slice{ptr, size}，指向本地 DRAM 中的数据
        }

        // STEP 2b: GPU D2H 暂存（如果数据在 GPU 内存）
        std::unordered_map<std::string, std::vector<Slice>> host_batch_object;
        std::vector<PinnedBufferPool::Buffer> staging_bufs;
        for (auto& [obj_key, slices] : batch_object) {
            for (const auto& slice : slices) {
                if (IsDevicePointer(slice.ptr, &device_id)) {
                    auto buf = pinned_buffer_pool_->Acquire(slice.size);
                    CopyDeviceToHost(buf.data, slice.ptr, slice.size);
                    host_slices.emplace_back(Slice{buf.data, slice.size});
                    staging_bufs.push_back(buf);
                }
            }
        }

        // STEP 2c: 定义完成回调（通知 Master）
        auto complete_handler = [this](auto& keys, auto& metadatas) -> ErrorCode {
            for (auto& metadata : metadatas) {
                metadata.transport_endpoint = local_rpc_addr_;  // 标记为本机
            }
            client_->NotifyOffloadSuccess(tasks, metadatas);
            // Master 更新副本列表：移除 MEMORY，添加 LOCAL_DISK
        };

        // STEP 2d: 定义淘汰回调（通知 Master 删除旧副本）
        auto eviction_handler = [this](const auto& evicted_keys) {
            client_->BatchEvictDiskReplica(evicted_keys, tenant_id, ReplicaType::LOCAL_DISK);
        };

        // STEP 2e: 调用存储后端写入 SSD
        auto offload_res = storage_backend_->BatchOffload(
            host_batch_object, complete_handler, eviction_handler);

        // STEP 2f: 释放暂存缓冲区
        for (auto& buf : staging_bufs) {
            pinned_buffer_pool_->Release(buf);
        }
    }

    return {};
}
```

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/src/file_storage.cpp#L364-L567

BucketStorageBackend 的分组逻辑：

```cpp
tl::expected<void, ErrorCode> BucketStorageBackend::GroupOffloadingKeysByBucket(
    const std::unordered_map<std::string, int64_t>& offloading_objects,
    std::vector<std::vector<std::string>>& buckets_keys) {

    std::vector<std::string> current_bucket;
    int64_t current_size = 0;

    for (const auto& [key, size] : offloading_objects) {
        current_bucket.push_back(key);
        current_size += size;

        // 检查是否达到桶大小或键数限制
        if (current_size >= bucket_backend_config_.bucket_size_limit ||
            current_bucket.size() >= bucket_backend_config_.bucket_keys_limit) {
            buckets_keys.emplace_back(std::move(current_bucket));
            current_bucket.clear();
            current_size = 0;
        }
    }

    // 未填满一个桶的对象暂存，下次心跳继续累积
    if (!current_bucket.empty()) {
        for (const auto& key : current_bucket) {
            ungrouped_offloading_objects_[key] = offloading_objects.at(key);
        }
    }

    return {};
}
```

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/src/storage_backend.cpp#L865-L888

两阶段淘汰协议：

```cpp
// Phase 1: PrepareEviction（在写入前执行，需持有互斥锁）
PendingEviction BucketStorageBackend::PrepareEviction(int64_t required_size) {
    PendingEviction pending;

    // 根据策略选择淘汰候选（FIFO 或 LRU）
    while (total_size_ + required_size > max_total_size_) {
        auto candidate_it = SelectEvictionCandidate();  // FIFO: buckets_.begin(); LRU: min last_access_ns_
        if (candidate_it == buckets_.end()) break;

        // 从元数据中移除
        int64_t bucket_id = candidate_it->first;
        auto& bucket_metadata = candidate_it->second;

        object_bucket_map_.erase(key);  // 移除键到桶的映射
        total_size_ -= bucket_metadata->data_size;

        // 记录待删除的桶和键
        pending.buckets.emplace_back(bucket_id, bucket_metadata);
        pending.keys.insert(pending.keys.end(),
                           bucket_metadata->keys.begin(),
                           bucket_metadata->keys.end());

        buckets_.erase(candidate_it);
    }

    return pending;  // 返回待淘汰的键和桶元数据（此时文件尚未删除）
}

// 调用者通知 Master 后，调用 FinalizeEviction
void BucketStorageBackend::FinalizeEviction(const PendingEviction& pending) {
    for (const auto& [bucket_id, bucket_metadata] : pending.buckets) {
        // 等待 in-flight reads 完成（最多 10 秒）
        auto start = std::chrono::steady_clock::now();
        while (bucket_metadata->inflight_reads_.load() > 0) {
            if (std::chrono::steady_clock::now() - start > 10s) {
                LOG(WARNING) << "Timeout waiting for in-flight reads";
                break;
            }
            std::this_thread::sleep_for(100ms);
        }

        // 删除物理文件
        std::filesystem::remove(GetBucketDataPath(bucket_id).value());
        std::filesystem::remove(GetBucketMetadataPath(bucket_id).value());
    }
}
```

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/src/storage_backend.cpp#L901-L918

### 练习题

1. **概念题**：为什么 BucketStorageBackend 需要分组策略，而不是每个对象立即写入一个文件？

2. **设计题**：两阶段淘汰协议（PrepareEviction + FinalizeEviction）的目的是什么？为什么不能在 PrepareEviction 中直接删除文件？

3. **推理题**：在 GPU 场景中，为什么需要 D2H 暂存，而不是直接从 GPU 内存写入 SSD？

4. **实践题**：假设你有一个 1TB 的 SSD，配置 `bucket_size_limit=256MB`、`bucket_keys_limit=500`、`max_total_size=1TB`。在 LRU 淘汰策略下，如何估算平均每个桶的生存时间？

### 答案

1. **答案**：分组策略减少文件数量和 I/O 次数。每个桶产生两个文件（`.bucket` + `.meta`），如果每个对象一个文件，百万级对象会导致文件系统 inode 耗尽和目录膨胀。分组后，256MB 或 500 个对象聚合为一个桶，文件数量减少 2–3 个数量级。

2. **答案**：两阶段协议确保：
   - Master 先被通知删除副本，避免客户端读取到已删除文件的 stale 地址
   - 等待 `inflight_reads_` 归零，确保正在进行的读取不会因文件删除而失败
   - 释放的磁盘空间立即可用于新的写入，避免 `WriteBucket` 因空间不足失败

3. **答案**：GPU 内存需要通过 PCIe 总线访问，不能直接从 SSD DMA。正确的流程是：GPU 内存 → 主机固定内存（pinned）→ SSD。固定内存避免额外的拷贝，且支持 DMA 操作。

4. **答案**：LRU 策略下，桶的生存时间取决于其 `last_access_ns_`。假设：
   - 总对象数 \(N\)，总容量 \(C\)，平均对象大小 \(S = C/N\)
   - 每个桶容纳 \(K\) 个对象（`bucket_keys_limit=500`）
   - 访问模式符合独立访问模型（IRM），热门对象集中在少数桶中
   - 平均生存时间 \(\approx\) 冷对象访问间隔 \(\times\) 淘汰速度
   - 更精确的估算需要访问频率分布（如 Zipf 参数）和具体的 LRU 更新频率

---

## 最小模块 3：数据提升（L2 → L1）

### 概念说明

数据提升（Promotion）是卸载的**逆过程**，将冷数据从 L2（SSD）**主动提升**回 L1（DRAM），以应对访问模式变化或容量重新分配。提升由 **Master 调度**，客户端执行，通常触发场景包括：

- **访问模式变化**：之前冷的对象突然变热（如对话回到早期轮次）
- **容量重新分配**：Master 主动提升某些对象以优化内存布局
- **预加载策略**：在已知即将访问前提前提升，减少延迟

提升流程包括：
1. **任务调度**：Master 将提升任务加入 `promotion_objects` 队列（`{key, size, source_replica}`）
2. **心跳拉取**：客户端 Heartbeat 时调用 `PromotionObjectHeartbeat`，获取待提升任务
3. **分配 MEMORY 副本**：客户端调用 `PromotionAllocStart`，Master 在 DRAM Segment 中分配空间
4. **从 SSD 加载**：客户端从本地 SSD 读取对象到 `ClientBuffer`（暂存区）
5. **Transfer Engine 写入**：通过 TE 将暂存区数据写入 MEMORY 副本（RDMA 零拷贝）
6. **通知 Master**：`NotifyPromotionSuccess`，Master 将副本标记为 COMPLETE（对读取可见）

### 伪代码或流程

```
Promotion 流程（Master 调度 + 客户端执行）：

Master 端：
1. 根据访问模式/容量策略，选择需要提升的冷数据
2. 创建 PromotionTaskItem{tenant_id, key, size, source_replica}
3. 加入 promotion_objects 队列（按 key 去重，避免重复提升）

Client 端（Heartbeat 线程）：
1. 调用 PromotionObjectHeartbeat()，拉取最多 1 个任务（Master 限流）
2. 对于每个任务：
   a. 调用 PromotionAllocStart(key, size, preferred_segments)
      → Master 返回 {alloc_id, memory_descriptor}（MEMORY 副本空间）
   b. 从本地 SSD 读取对象到 ClientBuffer：
      AllocateBatch(key, size) → BatchLoad(slices)
   c. 调用 Transfer Engine，将暂存区数据写入 MEMORY 副本：
      PromotionWrite(memory_descriptor, slices)
   d. 调用 NotifyPromotionSuccess(key, tenant_id)
      → Master 将副本从 PROCESSING 标记为 COMPLETE（可见）
3. 如果任何步骤失败，调用 NotifyPromotionFailure(key, tenant_id)
   → Master 释放分配的空间，清理任务条目

关键设计：
- 单心跳限流：每次 Heartbeat 最多返回 1 个任务，避免大对象阻塞心跳线程
- 幂等失败通知：NotifyPromotionFailure 可安全重入，Master 端 reaper 清理超时任务
- PROCESSING 副本：提升过程中，副本处于 PROCESSING 状态（对读取不可见），防止读取未完成数据
```

### 原理分析

**提升 vs. 卸载的对称性**：

| 维度 | 卸载（L1 → L2） | 提升（L2 → L1） |
|------|----------------|----------------|
| 触发方 | Master（容量压力/LRU） | Master（访问模式/容量重分配） |
| 执行方 | 客户端 Heartbeat 线程 | 客户端 Heartbeat 线程 |
| 数据源 | 本地 DRAM Segment | 本地 SSD |
| 数据目的地 | 本地 SSD | 本地 DRAM Segment（通过 Master 分配） |
| Master 交互 | OffloadObjectHeartbeat → NotifyOffloadSuccess | PromotionObjectHeartbeat → NotifyPromotionSuccess |
| 副本状态 | MEMORY → LOCAL_DISK | LOCAL_DISK → MEMORY |

**PROCESSING 副本的作用**：

提升过程中，Master 会创建一个 **PROCESSING** 状态的 MEMORY 副本，对读取不可见，防止读取未完成的数据。流程：

1. `PromotionAllocStart`：Master 创建 PROCESSING 副本，分配 DRAM 空间，返回 `memory_descriptor`
2. `PromotionWrite`：客户端将数据写入该空间（通过 Transfer Engine）
3. `NotifyPromotionSuccess`：Master 将副本从 PROCESSING 更新为 COMPLETE（对读取可见）

如果提升失败，PROCESSING 副本会被：
- `NotifyPromotionFailure` 立即释放（客户端主动通知）
- Master reaper 在 TTL 超时后清理（兜底机制）

**限流与退避**：

为避免大对象阻塞心跳线程或瞬时错误导致队列堆积：
- **Master 限流**：每次 `PromotionObjectHeartbeat` 最多返回 1 个任务
- **客户端快速失败**：任何步骤失败立即调用 `NotifyPromotionFailure`，释放 Master 资源
- **Reaper 兜底**：Master 端 reaper 线程定期清理超时的 PROCESSING 副本和任务条目（默认 TTL ~10 分钟）

### 代码实践

ProcessPromotionTasks 的核心逻辑：

```cpp
tl::expected<void, ErrorCode> FileStorage::ProcessPromotionTasks() {
    // STEP 1: 向 Master 拉取提升任务
    std::vector<PromotionTaskItem> promotion_objects;
    auto heartbeat_result = client_->PromotionObjectHeartbeat(promotion_objects);
    if (!heartbeat_result) {
        // SEGMENT_NOT_FOUND：Master 重启后丢失本机 segment，忽略（下次 Heartbeat 会重新挂载）
        if (heartbeat_result.error() == ErrorCode::SEGMENT_NOT_FOUND) return {};
        return tl::make_unexpected(heartbeat_result.error());
    }

    if (promotion_objects.empty()) return {};

    // STEP 2: 处理每个任务（Master 已限流，通常只有 1 个）
    for (const auto& task : promotion_objects) {
        const auto& key = task.key;
        const auto& tenant_id = task.tenant_id;
        const int64_t size = task.size;
        const auto storage_key = MakeTenantScopedStorageKey(tenant_id, key);

        // STEP 2a: 分配 MEMORY 副本空间
        auto alloc_result = client_->PromotionAllocStart(
            key, tenant_id, static_cast<uint64_t>(size), preferred_segments);
        if (!alloc_result) {
            // 分配失败（通常是 DRAM 不足），立即通知 Master 释放 slot
            VLOG(1) << "PromotionAllocStart failed for key=" << key
                    << ", error=" << alloc_result.error();
            client_->NotifyPromotionFailure(key, tenant_id);
            continue;
        }

        // 定义失败处理：释放 Master 端 PROCESSING 副本和 slot
        auto release_master_state = [this, &key, &tenant_id]() {
            client_->NotifyPromotionFailure(key, tenant_id);
        };

        // STEP 2b: 从 SSD 加载到暂存区
        std::vector<std::string> single_key{storage_key};
        std::vector<int64_t> single_size{size};
        auto allocate_res = AllocateBatch(single_key, single_size);
        if (!allocate_res) {
            LOG(WARNING) << "Promotion: AllocateBatch failed for key=" << key;
            release_master_state();
            continue;
        }
        auto staging = allocate_res.value();
        auto load_res = BatchLoad(staging->slices);
        if (!load_res) {
            LOG(WARNING) << "Promotion: BatchLoad failed for key=" << key;
            release_master_state();
            continue;
        }

        // STEP 2c: 通过 Transfer Engine 写入 MEMORY 副本
        auto slice_it = staging->slices.find(storage_key);
        if (slice_it == staging->slices.end()) {
            LOG(WARNING) << "Promotion: staging slice missing for key=" << key;
            release_master_state();
            continue;
        }
        std::vector<Slice> tx_slices{slice_it->second};
        ErrorCode write_err = client_->PromotionWrite(
            alloc_result.value().memory_descriptor, tx_slices);
        if (write_err != ErrorCode::OK) {
            LOG(WARNING) << "Promotion: TransferWrite failed for key=" << key;
            release_master_state();
            continue;
        }

        // STEP 2d: 通知 Master 提升成功
        auto notify_res = client_->NotifyPromotionSuccess(key, tenant_id);
        if (!notify_res) {
            LOG(WARNING) << "Promotion: NotifyPromotionSuccess failed for key=" << key;
            release_master_state();
            continue;
        }

        VLOG(1) << "Promotion completed for key=" << key << ", size=" << size;
    }

    return {};
}
```

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/src/file_storage.cpp#L687-L834

### 练习题

1. **概念题**：为什么提升过程需要创建 PROCESSING 副本，而不是直接写入 COMPLETE 副本？

2. **设计题**：Master 为什么要限制每次 `PromotionObjectHeartbeat` 只返回 1 个任务？如果返回多个任务会有什么问题？

3. **推理题**：在提升过程中，如果 `PromotionWrite` 成功但 `NotifyPromotionSuccess` 失败，会发生什么？如何保证数据一致性？

4. **实践题**：假设你有一个 100GB 的 SSD 数据集，需要将其中 10GB 的热数据提升到 DRAM。如何配置 `PromotionObjectHeartbeat` 的调用频率和 Master 端的 `promotion_queue_limit_` 来平衡提升速度与心跳稳定性？

### 答案

1. **答案**：PROCESSING 副本对读取不可见，防止读取未完成的数据。如果直接写入 COMPLETE 副本，并发读取可能会读到部分写入的数据（不一致）。PROCESSING 状态确保只有完全写入的数据才对读取可见。

2. **答案**：限制返回 1 个任务的原因：
   - **避免阻塞**：大对象的提升可能耗时数秒，如果返回多个任务，心跳线程可能被阻塞超过心跳间隔，导致 Master 认为本机超时
   - **流控**：Master 可以通过控制返回频率来平滑提升负载，避免瞬时大量提升冲击 SSD 或 DRAM
   - **公平性**：确保每个客户端都能有序地获取提升任务，避免单客户端垄断队列

3. **答案**：如果 `PromotionWrite` 成功但 `NotifyPromotionSuccess` 失败：
   - 数据已写入 MEMORY 副本，但副本仍处于 PROCESSING 状态（对读取不可见）
   - 客户端调用 `NotifyPromotionFailure` 释放 Master 资源（包括 PROCESSING 副本）
   - 已写入的数据成为"孤儿"数据，占用 DRAM 但不可访问
   - Master reaper 在 TTL 超时后清理 PROCESSING 副本，释放空间
   - **一致性保证**：错误是幂等的，重试 `NotifyPromotionSuccess` 不会导致重复写入（alloc_id 一次性）

4. **答案**：
   - **调用频率**：保持默认心跳间隔（10s），每次心跳最多提升 1 个任务，避免阻塞
   - **队列限制**：`promotion_queue_limit_` 设为 100–200（容纳 10GB/100MB≈100 个大对象），避免队列满后拒绝新任务
   - **监控指标**：观察 `promotion_latency_ms` 和 `promotion_queue_length`，如果延迟过高或队列堆积，考虑增加客户端数量或降低提升频率

---

## 最小模块 4：FileStorage 接口与数据路径

### 概念说明

FileStorage 是分层存储的**统一接口**，封装了存储后端、暂存区管理和后台线程，向上提供简单的 KV 操作，向下抽象磁盘 I/O。核心接口包括：

- **BatchGet**：从 SSD 读取对象到暂存区，返回缓冲区指针
- **BatchLoad**：存储后端从磁盘加载数据到提供的 Slice
- **BatchOffload**：存储后端将数据写入磁盘（L1 → L2）
- **ReleaseBuffer**：释放暂存区缓冲区（在 Transfer Engine 完成后）

**数据路径**：

- **卸载路径（L1 → L2）**：`Heartbeat` → `OffloadObjects` → `BatchQuerySegmentSlices`（从内存读）→ `StorageBackend::BatchOffload`（写 SSD）
- **加载路径（L2 → App）**：请求客户端 → `BatchGetOffloadObject` → `FileStorage::BatchGet` → `AllocateBatch`（分配暂存区）→ `BatchLoad`（从 SSD 读）→ Transfer Engine（RDMA 拉取）→ `ReleaseBuffer`
- **提升路径（L2 → L1）**：`ProcessPromotionTasks` → `PromotionAllocStart`（分配 DRAM）→ `AllocateBatch` + `BatchLoad`（从 SSD 读暂存）→ `PromotionWrite`（TE 写 DRAM）

### 伪代码或流程

```
BatchGet 的完整流程（请求客户端视角）：

1. 查询 Master，发现副本在 LOCAL_DISK（transport_endpoint=target_client_addr）
2. 调用 target_client.BatchGetOffloadObject(keys, sizes)

   --- 跨入目标客户端 ---

3. FileStorage::BatchGet(keys, sizes):
   a. AllocateBatch(keys, sizes) → 分配 ClientBuffer 槽位
   b. BatchLoad(slices) → 从 SSD 读取到暂存区
   c. 返回 BatchGetResult{batch_id, pointers}

4. 目标客户端返回 BatchGetOffloadObjectResponse{
     batch_id,
     pointers,         // ClientBuffer 中的地址
     transfer_engine_addr,
     gc_ttl_ms        // 缓冲区租约 TTL
   }

5. 请求客户端调用 Transfer Engine:
   BatchGetOffloadObject(transfer_engine_addr, keys, pointers, target_slices)
   → RDMA 从目标客户端的 ClientBuffer 零拷贝拉取到请求客户端的 DRAM/VRAM

6. 传输完成后，请求客户端调用 target_client.ReleaseBuffer(batch_id)
   → 目标客户端回收 ClientBuffer 槽位

关键点：
- BatchGet 只分配缓冲区和加载数据，不参与数据传输（由 Transfer Engine 完成）
- batch_id 用于租约管理，防止缓冲区泄漏（超时后 GC 线程回收）
- pointers 地址直接用于 RDMA，无需中间拷贝
```

### 原理分析

**ClientBuffer 设计**：

ClientBuffer 是预注册的 RDMA 内存区域，用于 SSD → 应用的零拷贝传输：

- **大小**：`local_buffer_size`（默认 1.28GB）
- **对齐**：4KB 对齐（O_DIRECT 要求）
- **分配策略**：按需分配槽位（`AllocateBatch`），超时回收（`ClientBufferGCThreadFunc`）
- **租约机制**：每个批次有 `gc_ttl_ms`（默认 5000ms），超时后自动回收

**AllocateBatch 的对齐处理**：

为了支持 O_DIRECT 读取，缓冲区必须 4KB 对齐：

```cpp
// 分配 oversized 缓冲区（数据大小 + 对齐余量）
size_t alloc_size = align_up(data_size, 4096) + 2 * 4096;

// 分配原始缓冲区
auto alloc_result = client_buffer_allocator_->allocate(alloc_size);

// 对齐到 4096 边界
void* raw_ptr = alloc_result->ptr();
void* aligned_ptr = (void*)(((uintptr_t)raw_ptr + 4096 - 1) & ~(4096 - 1));

// Slice 记录数据大小（而非分配大小）
result->slices.emplace(keys[i], Slice{aligned_ptr, data_size});
```

**BatchLoad 的 offset 修正**：

O_DIRECT 读取可能返回修正后的指针（`offset_in_buffer`），需要更新 `Slice`：

```cpp
// BatchLoad 后，slice.ptr 可能被修正（offset_in_buffer）
for (size_t i = 0; i < keys.size(); ++i) {
    auto it = allocated_batch->slices.find(keys[i]);
    if (it != allocated_batch->slices.end()) {
        allocated_batch->pointers[i] = reinterpret_cast<uintptr_t>(it->second.ptr);
    }
}
```

### 代码实践

BatchGet 的实现：

```cpp
tl::expected<FileStorage::BatchGetResult, ErrorCode> FileStorage::BatchGet(
    const std::vector<std::string>& keys, const std::vector<int64_t>& sizes) {

    // STEP 1: 分配 ClientBuffer 槽位
    auto allocate_res = AllocateBatch(keys, sizes);
    if (!allocate_res) {
        return tl::make_unexpected(allocate_res.error());
    }
    auto allocated_batch = allocate_res.value();

    // STEP 2: 从 SSD 加载到暂存区
    auto result = BatchLoad(allocated_batch->slices);
    if (!result) {
        return tl::make_unexpected(result.error());
    }

    // STEP 3: 修正指针（O_DIRECT offset 修正）
    for (size_t i = 0; i < keys.size(); ++i) {
        auto it = allocated_batch->slices.find(keys[i]);
        if (it != allocated_batch->slices.end()) {
            allocated_batch->pointers[i] = reinterpret_cast<uintptr_t>(it->second.ptr);
        }
    }

    // STEP 4: 记录批次到 client_buffer_allocated_batches_（用于 ReleaseBuffer/GC）
    uint64_t batch_id = allocated_batch->batch_id;
    {
        MutexLocker locker(&client_buffer_mutex_);
        client_buffer_allocated_batches_.emplace(batch_id, std::move(allocated_batch));
    }

    return BatchGetResult{batch_id, pointers};
}
```

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/src/file_storage.cpp#L323-L362

AllocateBatch 的对齐处理：

```cpp
tl::expected<std::shared_ptr<FileStorage::AllocatedBatch>, ErrorCode>
FileStorage::AllocateBatch(const std::vector<std::string>& keys,
                           const std::vector<int64_t>& sizes) {

    auto result = std::make_shared<AllocatedBatch>();
    result->batch_id = next_batch_id_.fetch_add(1, std::memory_order_relaxed);

    // 计算租约超时时间
    auto lease_timeout = std::chrono::steady_clock::now() +
                        std::chrono::milliseconds(config_.client_buffer_gc_ttl_ms);

    for (size_t i = 0; i < keys.size(); ++i) {
        size_t data_size = static_cast<size_t>(sizes[i]);

        // 分配 oversized 缓冲区（+4096 对齐 ptr，+4096 对齐 tail）
        size_t alloc_size = align_up(data_size, 4096) + 2 * 4096;

        auto alloc_result = client_buffer_allocator_->allocate(alloc_size);
        if (!alloc_result) {
            // 触发 GC 并重试
            {
                MutexLocker locker(&client_buffer_mutex_);
                auto now = std::chrono::steady_clock::now();
                for (auto it = client_buffer_allocated_batches_.begin();
                     it != client_buffer_allocated_batches_.end();) {
                    if (now >= it->second->lease_timeout) {
                        it = client_buffer_allocated_batches_.erase(it);
                    } else {
                        ++it;
                    }
                }
            }
            alloc_result = client_buffer_allocator_->allocate(alloc_size);
            if (!alloc_result) {
                return tl::make_unexpected(ErrorCode::BUFFER_OVERFLOW);
            }
        }

        // 对齐到 4096 边界
        void* raw_ptr = alloc_result->ptr();
        void* aligned_ptr = (void*)(((uintptr_t)raw_ptr + 4096 - 1) & ~(4096 - 1));

        // Slice 记录数据大小（而非分配大小）
        result->slices.emplace(keys[i], Slice{aligned_ptr, data_size});
        // pointers 稍后在 BatchGet 中修正
        result->pointers.emplace_back(reinterpret_cast<uintptr_t>(aligned_ptr));
        result->handles.emplace_back(std::move(alloc_result.value()));
    }

    result->lease_timeout = lease_timeout;
    return result;
}
```

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/src/file_storage.cpp#L910-L985

ClientBuffer GC 线程：

```cpp
void FileStorage::ClientBufferGCThreadFunc() {
    while (client_buffer_gc_running_) {
        {
            MutexLocker locker(&client_buffer_mutex_);
            auto now = std::chrono::steady_clock::now();
            for (auto it = client_buffer_allocated_batches_.begin();
                 it != client_buffer_allocated_batches_.end();) {
                // 租约超时，回收缓冲区
                if (now >= it->second->lease_timeout) {
                    VLOG(1) << "GC releasing batch_id: " << it->first
                            << " (lease expired)";
                    it = client_buffer_allocated_batches_.erase(it);
                } else {
                    ++it;
                }
            }
        }
        std::this_thread::sleep_for(
            std::chrono::seconds(config_.client_buffer_gc_interval_seconds));
    }
}
```

https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/src/file_storage.cpp#L987-L1010

### 练习题

1. **概念题**：为什么 ClientBuffer 需要 4KB 对齐？如果不对齐会发生什么？

2. **设计题**：AllocateBatch 中为什么要分配 `data_size + 2 * 4096` 的缓冲区，而不是 `data_size + 4096`？

3. **推理题**：在 BatchGet 流程中，为什么需要租约机制（`gc_ttl_ms`）？如果请求客户端在 Transfer Engine 完成后忘记调用 `ReleaseBuffer` 会发生什么？

4. **实践题**：假设你有一个 10GB 的对象需要通过 BatchGet 传输。如何配置 `local_buffer_size`、`client_buffer_gc_ttl_ms`、`client_buffer_gc_interval_seconds` 来确保传输成功且缓冲区不泄漏？

### 答案

1. **答案**：4KB 对齐是 O_DIRECT 的要求。如果缓冲区不对齐：
   - 使用 `O_DIRECT` 时，`pread`/`pwrite` 或 io_uring 会返回 `EINVAL` 错误
   - 即使不使用 `O_DIRECT`，不对齐的读写可能导致额外的内核拷贝（降低性能）
   - io_uring 的 `read_fixed` 要求缓冲区在注册区域内且对齐

2. **答案**：分配 `data_size + 2 * 4096` 的原因：
   - **头部对齐**：`+4096` 确保原始指针可以向前对齐到 4096 边界（最坏情况需要 4095 字节余量）
   - **尾部对齐**：O_DIRECT 读取可能读取完整的 4096 字节（即使 `data_size` 不是 4096 的倍数），尾部需要额外 4096 字节避免越界写入
   - 示例：`data_size=5000`，对齐到 8192，原始缓冲区需要至少 8192+4095=12287 字节（向上取整到 16384）

3. **答案**：租约机制的作用：
   - **防止泄漏**：如果请求客户端崩溃或忘记调用 `ReleaseBuffer`，GC 线程会在 `gc_ttl_ms` 后自动回收缓冲区
   - **避免饿死**：确保恶意或故障客户端不会永久占用缓冲区
   - **兜底机制**：即使 Transfer Engine 完成后 `ReleaseBuffer` 失败（如网络中断），GC 仍能清理
   - **权衡**：`gc_ttl_ms` 太短可能导致正常传输被回收（默认 5000ms，足够大多数 RDMA 传输）

4. **答案**：
   - `local_buffer_size`：至少 10GB + 余量（建议 20GB），确保单个对象能完整分配
   - `client_buffer_gc_ttl_ms`：根据网络延迟和对象大小调整，10GB 对象在 100Gbps 网络下约需 800ms，建议设为 5000–10000ms
   - `client_buffer_gc_interval_seconds`：保持默认 1s，及时回收过期缓冲区
   - **监控**：观察 `client_buffer_allocated_batches_` 的大小和 GC 频率，如果 GC 频繁触发，考虑增大 `gc_ttl_ms` 或 `local_buffer_size`

---

## 总结

本讲义覆盖了 Mooncake Store 分层存储的四个核心模块：

1. **分层存储架构**：L1（DRAM）+ L2（SSD）两级缓存，通过访问频率感知自动分层，在成本与性能间取得平衡
2. **SSD 卸载（L1 → L2）**：心跳线程驱动的异步卸载，通过分组策略、淘汰策略和 io_uring 优化，实现高效的冷数据迁移
3. **数据提升（L2 → L1）**：Master 调度的主动提升，通过 PROCESSING 副本、限流机制和幂等失败处理，应对访问模式变化
4. **FileStorage 接口**：统一的 KV 操作接口，通过 ClientBuffer、租约机制和 GC 线程，实现零拷贝的数据路径和资源管理

分层存储使 Mooncake 能够在 TB 级别的 KV cache 场景中，保持接近纯内存方案的热路径延迟，同时将硬件成本降低一个数量级。
