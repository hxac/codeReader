# 对象生命周期管理

本讲义讲解对象在 Mooncake Store 中的完整生命周期，从 Put 两阶段写入保证原子性，到 Get 操作如何通过 replica_list 选择最优副本，再到 Remove/RemoveByRegex 的元数据删除流程，以及 zombie 对象清理、Upsert 原地更新、Copy/Move 异步任务等高级特性。

## 最小模块 1: Put 两阶段写入

### 概念说明

Put 两阶段写入机制是为了解决分布式存储中的**原子性和一致性**问题。在 Mooncake Store 中，对象的写入过程分为两个阶段：

1. **PutStart**：向 Master 请求存储空间，Master 根据副本配置（ReplicateConfig）在多个存储节点上分配内存空间
2. **PutEnd**：数据传输完成后，通知 Master 将副本状态标记为 COMPLETE，使对象对其他客户端可见

这种设计确保了其他客户端不会读取到**未完全写入的数据**（防止脏读），同时支持多种副本写入模式和容错机制。

### 伪代码流程

```
# 客户端 Put 操作流程
function Put(key, data, config):
    # 阶段1: PutStart - 请求存储空间
    replica_list = Master.PutStart(key, data_size, config)
    if replica_list is empty:
        return ERROR_NO_SPACE
    
    # 阶段2: 数据传输 - 并发写入所有副本
    transfer_summary = {success: 0, failed: 0}
    for replica in replica_list:
        if replica.type == MEMORY or replica.type == NOF_SSD:
            result = TransferEngine.transfer(data, replica)
            if result.success:
                transfer_summary.success++
            else:
                transfer_summary.failed++
    
    # 阶段3: 根据传输结果决定最终状态
    decision = DetermineFinalizeDecision(config, transfer_summary)
    
    # 阶段4: PutEnd/Revoke - 提交或回滚
    if decision.should_succeed:
        Master.PutEnd(key, decision.end_type)
    if decision.should_revoke:
        Master.PutRevoke(key, decision.revoke_type)
    
    return decision.success ? SUCCESS : ERROR
```

### 原理分析

**副本状态机转换**：Mooncake Store 通过副本状态机确保写入的原子性和一致性：

\[ \text{UNDEFINED} \xrightarrow{\text{PutStart}} \text{INITIALIZED} \xrightarrow{\text{Transfer}} \text{PROCESSING} \xrightarrow{\text{PutEnd}} \text{COMPLETE} \]

**三种副本写入模式**：

1. **SINGLE_REPLICA**（单一副本）：只有一个 MEMORY 副本，失败即整体失败
2. **FLEXIBLE_DUAL_REPLICA**（灵活双副本）：一个 MEMORY + 一个 NoF_SSD，任一成功即可
3. **RELIABLE_MULTI_REPLICA**（可靠多副本）：多个副本，全部成功才算成功

**最终化决策逻辑**：系统根据副本配置和传输结果决定最终状态：

- **可靠模式**：所有分配的副本都必须传输成功
- **灵活双副本模式**：MEMORY 或 NoF_SSD 任一类型成功即可
- **失败情况**：调用 PutRevoke 释放已分配的空间

### 代码实践

**PutStart 接口定义**（mooncake-store/src/master_service.cpp:1478-1509）：

```cpp
ErrorCode MasterService::PutStart(const std::string& key,
                                  uint64_t value_length,
                                  const std::vector<uint64_t>& slice_lengths,
                                  const ReplicateConfig& config,
                                  std::vector<ReplicaInfo>& replica_list) {
    // 分配存储空间
    auto allocated_replicas = AllocateReplicas(slice_lengths, config);
    // 创建对象元数据，状态设为 INITIALIZED
    object_metadata[key] = ObjectMetadata{
        status: ReplicaStatus::INITIALIZED,
        replicas: allocated_replicas
    };
    return ErrorCode::OK;
}
```

**客户端 Put 流程**（mooncake-store/src/client_service.cpp:1479-1587）：

```cpp
tl::expected<void, ErrorCode> Client::Put(const ObjectKey& key,
                                          std::vector<Slice>& slices,
                                          const ReplicateConfig& config) {
    // 准备切片长度信息
    std::vector<size_t> slice_lengths;
    for (size_t i = 0; i < slices.size(); ++i) {
        slice_lengths.emplace_back(slices[i].size);
    }
    
    // 调用 PutStart 获取存储空间分配
    auto start_result = master_client_.PutStart(key, slice_lengths, client_cfg);
    if (!start_result) {
        return tl::unexpected(start_result.error());
    }
    
    ReplicaTransferSummary transfer_summary;
    for (const auto& replica : start_result.value()) {
        transfer_summary.RecordAllocatedReplica(replica);
    }
    
    // 并发传输到所有副本
    for (const auto& replica : start_result.value()) {
        if (replica.is_memory_replica() || replica.is_nof_replica()) {
            ErrorCode transfer_err = TransferWrite(replica, slices);
            if (transfer_err != ErrorCode::OK) {
                transfer_summary.RecordFailure(replica_type, transfer_err);
            } else {
                transfer_summary.RecordSuccess(replica_type);
            }
        }
    }
    
    // 根据传输结果决定最终操作
    const auto finalize_decision = DetermineFinalizeDecision(config, transfer_summary);
    
    if (finalize_decision.end_type.has_value()) {
        master_client_.PutEnd(key, *finalize_decision.end_type);
    }
    if (finalize_decision.revoke_type.has_value()) {
        master_client_.PutRevoke(key, *finalize_decision.revoke_type);
    }
    
    return finalize_decision.success ? tl::expected<void, ErrorCode>{}
                                     : tl::unexpected(finalize_decision.error);
}
```

**副本状态转换**（mooncake-store/include/replica.h:426-442）：

```cpp
void mark_processing() {
    if (status_ == ReplicaStatus::COMPLETE) {
        status_ = ReplicaStatus::PROCESSING;
    } else {
        LOG(ERROR) << "Cannot mark_processing from status: " << status_;
    }
}

void mark_complete() {
    if (status_ == ReplicaStatus::PROCESSING) {
        status_ = ReplicaStatus::COMPLETE;
    } else if (status_ == ReplicaStatus::COMPLETE) {
        LOG(WARNING) << "Replica already marked as complete";
    } else {
        LOG(ERROR) << "Invalid replica status: " << status_;
    }
}
```

**关键源码链接**：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/src/client_service.cpp#L1479-L1587

这段代码实现了完整的 Put 流程：调用 PutStart 获取存储空间分配、并发传输到所有副本、根据传输结果调用 PutEnd 或 PutRevoke，确保写入的原子性。

### 练习题

1. **基础题**：为什么需要 Put 两阶段写入，而不是直接写入并标记为完成？

2. **进阶题**：在 FLEXIBLE_DUAL_REPLICA 模式下，如果 MEMORY 副本传输成功但 NoF_SSD 传输失败，最终对象状态是什么？

3. **实现题**：假设你要实现一个 Put 操作的重试机制，在哪个阶段进行重试最合理？为什么？

4. **场景题**：如果客户端在 PutStart 之后、PutEnd 之前崩溃，系统如何处理这种情况？

### 答案

1. **答案**：Put 两阶段写入确保了原子性和一致性，防止其他客户端读取到未完全写入的数据（脏读）。第一阶段 PutStart 只分配空间不传输数据，副本状态为 INITIALIZED；第二阶段 PutEnd 将状态标记为 COMPLETE 后才对其他客户端可见。这样设计还支持多种副本写入模式和容错机制。

2. **答案**：在 FLEXIBLE_DUAL_REPLICA 模式下，如果 MEMORY 副本成功但 NoF_SSD 失败，系统会调用 PutEnd(key, ReplicaType::MEMORY) 提交 MEMORY 副本，同时调用 PutRevoke(key, ReplicaType::NOF_SSD) 释放 NoF_SSD 副本空间。最终对象状态为成功，只有一个 MEMORY 副本可用。

3. **答案**：在数据传输阶段（TransferWrite）进行重试最合理。因为 PutStart 阶段只是空间分配，失败通常是系统级错误（如空间不足），重试无意义；而 PutEnd/PutRevoke 是状态确认，失败需要回滚。只有数据传输阶段可能因网络瞬态问题失败，值得重试。

4. **答案**：这种情况会产生 "zombie 对象"。系统通过两个超时机制处理：put_start_discard_timeout（默认30秒）后新的 PutStart 可以抢占旧的；put_start_release_timeout（默认10分钟）后驱逐线程会回收这些僵尸对象的存储空间。这确保了崩溃客户端不会永久占用资源或阻塞该 key 的后续写入。

---

## 最小模块 2: Get 副本选择

### 概念说明

Get 操作的核心是**副本选择策略**，即从多个副本中选择最优的副本进行读取。Mooncake Store 支持多种副本类型（MEMORY、NoF_SSD、DISK、LOCAL_DISK），每种类型有不同的访问延迟和带宽特性。

Get 操作通过 GetReplicaList 接口获取对象的所有副本信息，然后根据副本状态、位置、本地缓存等因素选择最合适的副本进行数据传输。

### 伪代码流程

```
# 客户端 Get 操作流程
function Get(key, destination_buffers):
    # 阶段1: 查询副本列表
    query_result = Master.GetReplicaList(key)
    if query_result.replicas is empty:
        return ERROR_NOT_FOUND
    
    # 阶段2: 副本选择策略
    selected_replica = None
    
    # 优先级1: 检查本地热缓存
    if hot_cache.contains(key):
        cached_replica = hot_cache.get(key)
        if cached_replica.is_valid():
            selected_replica = cached_replica
    
    # 优先级2: 选择首个完整的副本
    if selected_replica is None:
        for replica in query_result.replicas:
            if replica.status == COMPLETE and replica.is_healthy():
                selected_replica = replica
                break
    
    # 优先级3: 检查租约是否过期
    if query_result.is_lease_expired():
        return ERROR_LEASE_EXPIRED
    
    # 阶段3: 数据传输
    if selected_replica.type == NOF_SSD:
        # NoF 副本需要连续内存
        if not is_contiguous(destination_buffers):
            return ERROR_INVALID_PARAMS
        TransferEngine.transfer(selected_replica, destination_buffers, contiguous=true)
    else:
        TransferEngine.transfer(selected_replica, destination_buffers)
    
    # 阶段4: 热缓存晋升
    if should_promote_to_hot_cache(key):
        hot_cache.promote(key, destination_buffers)
    
    return SUCCESS
```

### 原理分析

**副本优先级策略**：Mooncake Store 使用多级副本选择策略：

1. **本地热缓存优先**：如果数据在本地热缓存中，直接使用避免网络传输
2. **副本状态过滤**：只选择状态为 COMPLETE 的副本，避免读取未完成写入的数据
3. **副本类型偏好**：MEMORY > NoF_SSD > DISK > LOCAL_DISK（按访问延迟排序）
4. **网络拓扑感知**：优先选择同网段、同机架的副本以减少网络延迟

**租约机制**：GetReplicaList 返回时附带租约 TTL（默认5秒），在租约有效期内对象不会被删除或驱逐。如果数据传输时间超过租约时间，Get 操作会失败返回 LEASE_EXPIRED 错误。

**热缓存频率准入**：使用 Count-Min Sketch 算法跟踪访问频率，只有频繁访问的对象才会被晋升到热缓存，避免缓存污染。

### 代码实践

**副本选择函数**（mooncake-store/src/client_service.cpp:3832-3850）：

```cpp
ErrorCode Client::FindFirstCompleteReplica(
    const std::vector<Replica::Descriptor>& replicas,
    Replica::Descriptor& selected_replica) {
    // 优先选择 MEMORY 副本
    for (const auto& replica : replicas) {
        if (replica.is_memory_replica() && replica.status == ReplicaStatus::COMPLETE) {
            selected_replica = replica;
            return ErrorCode::OK;
        }
    }
    
    // 其次选择 NoF_SSD 副本
    for (const auto& replica : replicas) {
        if (replica.is_nof_replica() && replica.status == ReplicaStatus::COMPLETE) {
            selected_replica = replica;
            return ErrorCode::OK;
        }
    }
    
    // 最后选择其他类型副本
    for (const auto& replica : replicas) {
        if (replica.status == ReplicaStatus::COMPLETE) {
            selected_replica = replica;
            return ErrorCode::OK;
        }
    }
    
    return ErrorCode::INVALID_REPLICA;
}
```

**Get 操作主流程**（mooncake-store/src/client_service.cpp:1062-1120）：

```cpp
tl::expected<void, ErrorCode> Client::Get(const std::string& object_key,
                                          const QueryResult& query_result,
                                          std::vector<Slice>& slices) {
    // 查找第一个完整的副本
    Replica::Descriptor replica;
    ErrorCode err = FindFirstCompleteReplica(query_result.replicas, replica);
    if (err != ErrorCode::OK) {
        if (err == ErrorCode::INVALID_REPLICA) {
            LOG(ERROR) << "no_complete_replicas_found key=" << object_key;
        }
        return tl::unexpected(err);
    }
    
    // 检查本地热缓存并重定向副本描述符
    bool cache_used = false;
    if (hot_cache_ && replica.is_memory_replica()) {
        cache_used = RedirectToHotCache(object_key, replica);
    }
    
    // 执行数据传输
    auto t0_get = std::chrono::steady_clock::now();
    err = TransferRead(replica, slices);
    
    // 释放缓存块（传输完成后 memcpy 已完成）
    if (hot_cache_ && cache_used) {
        hot_cache_->ReleaseHotKey(object_key);
    }
    
    if (err != ErrorCode::OK) {
        LOG(ERROR) << "transfer_read_failed key=" << object_key;
        return tl::unexpected(err);
    }
    
    // 频率准入：只将频繁访问的键晋升到热缓存
    if (ShouldAdmitToHotCache(object_key, cache_used)) {
        ProcessSlicesAsync(object_key, slices, replica);
    }
    
    // 检查租约是否过期
    if (query_result.IsLeaseExpired()) {
        LOG(WARNING) << "lease_expired_before_data_transfer_completed key="
                     << object_key;
        return tl::unexpected(ErrorCode::LEASE_EXPIRED);
    }
    
    return {};
}
```

**热缓存重定向**（mooncake-store/src/client_service.cpp:1454-1477）：

```cpp
bool Client::RedirectToHotCache(const std::string& key,
                                Replica::Descriptor& replica) {
    if (!replica.is_memory_replica() || !hot_cache_) {
        return false;
    }
    
    auto& mem_desc = replica.get_memory_descriptor();
    HotMemBlock* blk = hot_cache_->GetHotKey(key);
    if (blk == nullptr) {
        return false;
    }
    
    // 验证缓存块大小
    if (mem_desc.buffer_descriptor.size_ != blk->size) {
        LOG(ERROR) << "Cache hit but size mismatch for key: " << key;
        return false;
    }
    
    // 重定向副本描述符到本地缓存地址
    mem_desc.buffer_descriptor.transport_endpoint_ =
        (metadata_connstring_ == P2PHANDSHAKE) ? GetTransportEndpoint()
                                               : local_hostname_;
    mem_desc.buffer_descriptor.buffer_address_ =
        reinterpret_cast<uintptr_t>(blk->addr);
    return true;
}
```

**关键源码链接**：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/src/client_service.cpp#L1062-L1120

这段代码实现了 Get 操作的核心流程：通过 FindFirstCompleteReplica 选择最优副本、检查本地热缓存避免网络传输、执行数据传输、验证租约有效性、管理热缓存晋升。

### 练习题

1. **基础题**：为什么 Get 操作需要租约机制，而不是直接读取数据？

2. **进阶题**：如果所有副本都不是 COMPLETE 状态，Get 操作会返回什么错误？这种情况如何避免？

3. **实现题**：假设你要实现一个智能副本选择策略，考虑网络延迟和副本负载，你会如何设计？

4. **场景题**：在多租户环境中，如何防止某个租户频繁访问同一对象污染热缓存？

### 答案

1. **答案**：租约机制防止数据在传输过程中被删除或驱逐。如果没有租约，可能出现 Get 操作正在传输数据时，对象被 Remove 或 Evict 删除，导致数据不一致。租约 TTL（默认5秒）确保在数据传输期间对象受到保护，传输完成后会检查租约是否过期，过期则返回 LEASE_EXPIRED 错误。

2. **答案**：如果所有副本都不是 COMPLETE 状态，Get 操作会返回 INVALID_REPLICA 错误。这种情况通常发生在客户端读取到正在写入中的对象（如 PutStart 后 PutEnd 前的查询）。避免方法：确保应用层使用正确的读写顺序，或在 Get 操作前检查对象是否存在；对于批量操作，可以使用 BatchQuery 并过滤掉状态不完整的对象。

3. **答案**：智能副本选择策略可以综合考虑：1) 副本健康状态和完整性；2) 网络拓扑距离（同机架 > 同数据中心 > 跨数据中心）；3) 存储介质类型（MEMORY > NoF_SSD > DISK）；4) 实时网络延迟（通过 ping 测量）；5) 副本负载（当前正在服务的请求数）。实现时可以为每个副本计算综合得分，选择得分最高的副本。

4. **答案**：Mooncake Store 使用 Count-Min Sketch 算法实现频率准入控制，只有访问频率超过阈值的对象才会晋升到热缓存。在多租户环境中，可以：1) 为每个租户维护独立的频率统计；2) 设置租户级别的热缓存大小限制；3) 使用更严格的准入阈值；4) 实现租户间的缓存隔离和公平调度。

---

## 最小模块 3: Remove 删除

### 概念说明

Remove 操作用于删除对象及其所有副本。Mooncake Store 提供了多种删除接口：

1. **Remove**：删除单个对象
2. **RemoveByRegex**：批量删除匹配正则表达式的对象
3. **RemoveAll**：删除所有对象（支持 force 参数）
4. **BatchRemove**：批量删除多个对象

删除操作是**元数据操作**，Master 只是将副本状态标记为 REMOVED，不涉及实际的数据传输。存储空间会在后续的驱逐线程或对象清理时被回收。

### 伪代码流程

```
# 客户端 Remove 操作流程
function Remove(key, force=false):
    # 阶段1: 检查对象状态
    object_metadata = Master.get_metadata(key)
    if object_metadata is None:
        return ERROR_NOT_FOUND
    
    # 阶段2: 检查租约和引用计数
    if not force and object_metadata.has_active_lease():
        return ERROR_LEASE_ACTIVE
    if not force and object_metadata.has_busy_replicas():
        return ERROR_REPLICAS_BUSY
    
    # 阶段3: 标记所有副本为删除状态
    for replica in object_metadata.replicas:
        replica.status = REMOVED
    
    # 阶段4: 清理相关元数据
    Master.remove_metadata(key)
    
    # 阶段5: 清理本地热缓存
    hot_cache.remove(key)
    
    return SUCCESS

# 批量删除（正则表达式）
function RemoveByRegex(pattern, force=false):
    # 阶段1: 查询匹配的对象
    matched_objects = Master.query_by_regex(pattern)
    removed_count = 0
    
    # 阶段2: 逐个删除匹配的对象
    for key in matched_objects:
        result = Remove(key, force)
        if result == SUCCESS:
            removed_count++
    
    return removed_count
```

### 原理分析

**删除安全性检查**：为了防止删除正在使用的对象，Remove 操作会进行多项检查：

1. **租约检查**：如果对象有活跃租约，非强制删除会失败
2. **引用计数检查**：如果有副本正在被读取（refcnt > 0），非强制删除会失败
3. **强制删除**：force 参数可以跳过安全检查，但可能导致数据不一致

**删除与驱逐的区别**：

- **Remove**：用户主动删除，立即标记所有副本为 REMOVED，元数据立即删除
- **Eviction**：系统自动驱逐，受驱逐策略控制，优先删除非 pin、无租约的对象

**热缓存清理**：删除对象时会同步清理本地热缓存，避免读取已删除的数据。对于批量删除，会清理整个缓存 epoch 以确保一致性。

### 代码实践

**客户端 Remove 实现**（mooncake-store/src/client_service.cpp:2557-2575）：

```cpp
tl::expected<void, ErrorCode> Client::Remove(const ObjectKey& key, bool force) {
    // 提升热缓存代数，使后续的缓存访问失效
    if (hot_cache_) {
        hot_cache_->BumpKeyGeneration(key);
    }
    
    // 调用 Master 删除对象
    auto result = master_client_.Remove(key, force);
    if (!result) {
        return tl::unexpected(result.error());
    }
    
    // 从热缓存中移除对象
    if (hot_cache_) {
        hot_cache_->RemoveHotKey(key);
    }
    
    return {};
}
```

**Master 端 Remove 实现**（mooncake-store/src/master_service.cpp 推测）：

```cpp
tl::expected<void, ErrorCode> MasterService::Remove(const std::string& key, bool force) {
    std::lock_guard<std::shared_mutex> lock(metadata_mutex_);
    
    // 查找对象元数据
    auto it = object_metadata_.find(key);
    if (it == object_metadata_.end()) {
        return tl::unexpected(ErrorCode::KEY_NOT_FOUND);
    }
    
    auto& metadata = it->second;
    
    // 安全检查
    if (!force) {
        // 检查租约
        if (metadata.has_active_lease()) {
            return tl::unexpected(ErrorCode::LEASE_ACTIVE);
        }
        
        // 检查副本引用计数
        for (const auto& replica : metadata.replicas) {
            if (replica.get_refcnt() > 0) {
                return tl::unexpected(ErrorCode::REPLICAS_BUSY);
            }
        }
    }
    
    // 标记所有副本为删除状态
    for (auto& replica : metadata.replicas) {
        replica.mark_removed();
    }
    
    // 删除元数据
    object_metadata_.erase(it);
    
    return {};
}
```

**批量删除实现**（mooncake-store/src/client_service.cpp:2577-2595）：

```cpp
tl::expected<long, ErrorCode> Client::RemoveByRegex(const ObjectKey& str, bool force) {
    // 提升整个缓存代数，使批量删除期间的所有缓存访问失效
    if (hot_cache_) {
        hot_cache_->BumpCacheEpoch();
    }
    
    // 调用 Master 批量删除
    auto result = master_client_.RemoveByRegex(str, force);
    if (!result) {
        return tl::unexpected(result.error());
    }
    
    // 清理匹配的热缓存条目
    if (result.value() > 0 && hot_cache_) {
        hot_cache_->BumpCacheEpoch();
        hot_cache_->RemoveHotKeysByRegex(str);
    }
    
    return result.value();
}
```

**关键源码链接**：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/src/client_service.cpp#L2557-L2595

这段代码实现了 Remove 和 RemoveByRegex 操作的核心逻辑：更新热缓存代数使旧缓存失效、调用 Master 删除对象元数据、清理本地热缓存，确保删除操作的一致性。

### 练习题

1. **基础题**：为什么 Remove 操作要检查租约和引用计数，而不是直接删除？

2. **进阶题**：force 参数的使用场景是什么，过度使用会有什么问题？

3. **实现题**：假设你要实现一个延迟删除机制（软删除），如何设计？

4. **场景题**：在大规模批量删除时，如何避免对系统性能造成冲击？

### 答案

1. **答案**：租约和引用计数检查防止删除正在使用的对象，避免数据不一致。如果对象有活跃租约，说明可能有客户端正在读取；如果副本引用计数大于0，说明副本正在被传输。强制删除可能导致读取操作失败或传输到已删除的数据，因此需要安全检查。

2. **答案**：force 参数主要用于紧急清理或管理员维护场景，如清理损坏的对象、释放被占用的空间。过度使用 force 会导致：1) 数据不一致（正在读的客户端可能读到部分删除的数据）；2) 应用层错误处理复杂（需要处理突然的对象不存在错误）；3) 违反语义预期（有租约保护的对象被删除）。应谨慎使用，仅在必要时使用。

3. **答案**：延迟删除（软删除）机制设计：1) 添加中间状态 SOFT_DELETED，对象对查询不可见但元数据仍存在；2) 设置软删除 TTL（如24小时），超时后才真正删除；3) 提供 Undelete 接口恢复软删除的对象；4) 驱逐线程优先处理软删除对象；5) 统计信息区分软删除和硬删除。这样给用户反悔机会，同时减少立即删除的性能冲击。

4. **答案**：大规模批量删除的优化策略：1) 分批处理，每批删除数量限制（如100个对象）；2) 使用 RemoveByRegex 而不是多次 Remove，减少 RPC 开销；3) 非高峰期执行，避免影响正常业务；4) 监控系统负载，动态调整删除速率；5) 异步删除，主线程立即返回，后台线程处理实际删除；6) 优先删除无租约、非热对象，减少冲突。

---

## 最小模块 4: Upsert 更新

### 概念说明

Upsert（Update 或 Insert）操作用于插入或更新对象，与 Put 的区别在于：

1. **语义不同**：如果对象不存在则插入，如果存在则更新
2. **空间重用**：更新时优先重用现有分配，减少内存碎片
3. **三阶段流程**：UpsertStart → UpsertEnd 或 UpsertRevoke

Upsert 适用于需要频繁更新相同 key 的场景，如迭代训练中的模型参数更新、增量数据处理等。

### 伪代码流程

```
# 客户端 Upsert 操作流程
function Upsert(key, data, config):
    # 阶段1: UpsertStart - 请求空间分配
    result = Master.UpsertStart(key, data_size, config)
    
    if result.is_new_object:
        # 新对象：按正常 Put 流程处理
        replica_list = result.allocated_replicas
        for replica in replica_list:
            TransferEngine.transfer(data, replica)
        Master.UpsertEnd(key, replica_type)
        
    else:
        # 已存在对象：尝试原地更新
        if result.can_update_in_place:
            # 原地更新：重用现有分配
            TransferEngine.transfer(data, result.existing_replicas)
            Master.UpsertEnd(key, replica_type)
        else:
            # 需要重新分配：对象大小或配置变化
            # 1. 先分配新空间
            new_replicas = Master.allocate_new_space(key, data_size, config)
            # 2. 传输到新副本
            TransferEngine.transfer(data, new_replicas)
            # 3. 提交新副本
            Master.UpsertEnd(key, replica_type)
            # 4. 旧副本会在后台自动清理
    
    return SUCCESS

# 错误处理流程
function UpsertWithErrorHandling(key, data, config):
    try:
        result = UpsertStart(key, data_size, config)
        transfer_result = TransferEngine.transfer(data, result.replicas)
        
        if transfer_result.success:
            UpsertEnd(key, replica_type)
        else:
            UpsertRevoke(key, replica_type)
            return ERROR_TRANSFER_FAILED
            
    except Exception as e:
        UpsertRevoke(key, replica_type)
        return e.error_code
```

### 原理分析

**Upsert 决策树**：

\[ \text{Key Exists?} \rightarrow \begin{cases} 
\text{No} & \rightarrow \text{Insert: PutStart + Transfer + PutEnd} \\
\text{Yes} & \rightarrow \begin{cases}
\text{Size Match + Config Match} & \rightarrow \text{In-Place Update} \\
\text{Size Change or Config Change} & \rightarrow \text{Reallocate + Transfer}
\end{cases}
\end{cases} \]

**原地更新条件**：

1. 对象已存在且 key 匹配
2. 新旧数据大小相同（或新大小 <= 旧大小）
3. 副本配置兼容（如副本类型相同）
4. 现有副本健康且可用

**三阶段协议**：

- **UpsertStart**：分析现有对象状态，决定插入或更新策略
- **UpsertEnd**：提交更新，标记新副本为 COMPLETE
- **UpsertRevoke**：回滚更新，释放新分配的空间

### 代码实践

**客户端 Upsert 实现**（mooncake-store/src/client_service.cpp:1589-1678）：

```cpp
tl::expected<void, ErrorCode> Client::Upsert(const ObjectKey& key,
                                             std::vector<Slice>& slices,
                                             const ReplicateConfig& config) {
    // 准备切片长度信息
    std::vector<size_t> slice_lengths;
    for (size_t i = 0; i < slices.size(); ++i) {
        slice_lengths.emplace_back(slices[i].size);
    }
    
    // 清理热缓存（避免读到旧数据）
    if (hot_cache_) {
        hot_cache_->RemoveHotKey(key);
    }
    
    // 开始 Upsert 操作
    auto start_result = master_client_.UpsertStart(key, slice_lengths, client_cfg);
    if (!start_result) {
        ErrorCode err = start_result.error();
        if (err == ErrorCode::NO_AVAILABLE_HANDLE) {
            LOG(WARNING) << "Failed to start upsert operation for key=" << key
                         << PUT_NO_SPACE_HELPER_STR;
        } else {
            LOG(ERROR) << "Failed to start upsert operation for key=" << key
                       << ": " << toString(err);
        }
        return tl::unexpected(err);
    }
    
    // 处理磁盘副本
    if (storage_backend_) {
        for (auto it = start_result.value().rbegin();
             it != start_result.value().rend(); ++it) {
            const auto& replica = *it;
            if (replica.is_disk_replica()) {
                auto disk_descriptor = replica.get_disk_descriptor();
                PutToLocalFile(key, slices, disk_descriptor);
                break;
            }
        }
    }
    
    // 传输到内存副本
    for (const auto& replica : start_result.value()) {
        if (replica.is_memory_replica()) {
            ErrorCode transfer_err = TransferWrite(replica, slices);
            if (transfer_err != ErrorCode::OK) {
                // 传输失败，撤销操作
                auto revoke_result =
                    master_client_.UpsertRevoke(key, ReplicaType::MEMORY);
                if (!revoke_result) {
                    LOG(ERROR) << "Failed to revoke upsert operation";
                    return tl::unexpected(revoke_result.error());
                }
                return tl::unexpected(transfer_err);
            }
        }
    }
    
    // 结束 Upsert 操作
    auto end_result = master_client_.UpsertEnd(key, ReplicaType::MEMORY);
    if (!end_result) {
        ErrorCode err = end_result.error();
        LOG(ERROR) << "Failed to end upsert operation: " << err;
        return tl::unexpected(err);
    }
    
    // 成功侧失效：防止并发读取导致的缓存不一致
    if (hot_cache_) {
        hot_cache_->RemoveHotKey(key);
    }
    
    return {};
}
```

**批量 Upsert 实现**（mooncake-store/src/client_service.cpp:1680-1709）：

```cpp
std::vector<tl::expected<void, ErrorCode>> Client::BatchUpsert(
    const std::vector<ObjectKey>& keys,
    std::vector<std::vector<Slice>>& batched_slices,
    const ReplicateConfig& config) {
    ReplicateConfig client_cfg = config;
    if (protocol_ == "cxl") {
        client_cfg.preferred_segment = local_hostname_;
    }
    if (client_cfg.prefer_alloc_in_same_node) {
        LOG(ERROR) << "prefer_alloc_in_same_node is not supported for upsert";
        return std::vector<tl::expected<void, ErrorCode>>(
            keys.size(), tl::unexpected(ErrorCode::INVALID_PARAMS));
    }
    
    // 创建操作对象
    std::vector<PutOperation> ops = CreatePutOperations(keys, batched_slices);
    
    // 开始批量 Upsert
    StartBatchUpsert(ops, client_cfg);
    
    // 提交传输
    auto t0 = std::chrono::steady_clock::now();
    SubmitTransfers(ops);
    WaitForTransfers(ops);
    auto us = std::chrono::duration_cast<std::chrono::microseconds>(
                  std::chrono::steady_clock::now() - t0)
                  .count();
    if (metrics_) {
        metrics_->transfer_metric.batch_put_latency_us.observe(us);
    }
    
    // 完成批量 Upsert
    FinalizeBatchUpsert(ops);
    
    // 收集结果
    return CollectResults(ops);
}
```

**关键源码链接**：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/src/client_service.cpp#L1589-L1678

这段代码实现了 Upsert 操作的核心流程：清理热缓存避免读取旧数据、调用 UpsertStart 决定插入或更新策略、处理磁盘和内存副本的数据传输、在失败时调用 UpsertRevoke 回滚、成功后再次清理热缓存确保一致性。

### 练习题

1. **基础题**：Upsert 和 Put 的主要区别是什么，什么时候应该使用 Upsert？

2. **进阶题**：为什么 Upsert 需要"成功侧失效"（在成功后再次清理热缓存）？

3. **实现题**：假设你要实现一个条件 Upsert（只更新满足条件的对象），如何设计？

4. **场景题**：在分布式训练中，多个 worker 同时 Upsert 同一个 key，如何保证一致性？

### 答案

1. **答案**：Upsert 和 Put 的主要区别：1) 语义不同：Put 只能插入新对象（已存在返回错误），Upsert 可以插入或更新；2) 空间重用：Upsert 更新时优先重用现有分配，减少内存碎片和分配开销；3) 错误处理：Upsert 有专门的 UpsertRevoke 回滚机制。使用场景：需要频繁更新相同 key 时使用 Upsert，如模型参数更新、增量数据处理；确定是新对象时使用 Put 更简单明确。

2. **答案**：Upsert 需要"成功侧失效"是因为在 UpsertStart 的热缓存清理和 UpsertEnd 之间，可能有并发的 Get 操作读取到旧数据并提交异步热缓存填充请求。如果旧数据的 token 仍然有效，这个异步填充可能在 Upsert 完成后发布，导致热缓存中存在已过期的旧数据。在 UpsertEnd 后再次失效热缓存，确保这些潜在的异步填充请求不会成功发布。

3. **答案**：条件 Upsert 设计方案：1) 扩展 Upsert 接口，添加条件谓词（如 version_check、value_compare）；2) Master 端在 UpsertStart 时检查条件，不满足则跳过更新；3) 支持多种条件类型：版本号检查、时间戳比较、哈希值匹配；4) 返回条件检查结果，让应用层知道是否实际执行了更新；5) 对于批量条件 Upsert，可以返回每个 key 的更新状态。这样实现 CAS（Compare-And-Swap）语义，支持乐观锁。

4. **答案**：多 worker 同时 Upsert 同一 key 的一致性保证：1) 使用 Master 的串行化控制，同一 key 的 UpsertStart 请求串行处理；2) 后续的 UpsertStart 会看到前面的 UpsertEnd 结果，基于最新值更新；3) 应用层使用版本号或时间戳检测冲突，实现乐观并发控制；4) 考虑使用分布式锁（如 etcd）确保关键更新的原子性；5) 设计幂等的更新逻辑，多次执行结果相同；6) 监控冲突率，冲突高时考虑分片或 batch 策略减少冲突。

---

## 最小模块 5: 异步任务

### 概念说明

异步任务机制用于执行跨节点的数据复制和移动操作，避免阻塞主流程。Mooncake Store 提供两种异步任务：

1. **CopyTask**：异步复制对象到目标段，源对象保留
2. **MoveTask**：异步移动对象到目标段，源对象删除

异步任务由 Master 调度，Client 执行实际的传输工作。任务状态可通过 QueryTask 查询，支持进度跟踪和错误处理。

### 伪代码流程

```
# 异步复制任务
function CreateCopyTask(key, target_segments):
    # 阶段1: 验证输入
    if not Master.object_exists(key):
        return ERROR_NOT_FOUND
    if not all(seg in Master.mounted_segments() for seg in target_segments):
        return ERROR_INVALID_SEGMENT
    
    # 阶段2: 创建任务记录
    task_id = generate_task_id()
    task_record = {
        id: task_id,
        type: COPY,
        key: key,
        source_replicas: Master.get_replicas(key),
        target_segments: target_segments,
        status: PENDING,
        created_at: current_time()
    }
    Master.add_task(task_record)
    
    # 阶段3: 通知任务调度线程
    Master.notify_task_dispatcher()
    return task_id

# 任务执行流程（后台线程）
function ExecuteTask(task_id):
    task = Master.get_task(task_id)
    
    if task.type == COPY:
        # 复制任务：保留源对象
        for target_seg in task.target_segments:
            # 分配目标空间
            new_replica = Master.allocate_replica(target_seg, task.key, task.size)
            
            # 选择最优源副本
            source_replica = select_best_source_replica(task.source_replicas, target_seg)
            
            # 执行传输
            TransferEngine.copy(source_replica, new_replica)
            
            # 提交新副本
            Master.add_replica(task.key, new_replica)
        
        task.status = COMPLETED
        
    elif task.type == MOVE:
        # 移动任务：删除源对象
        target_seg = task.target_segments[0]
        
        # 分配目标空间
        new_replica = Master.allocate_replica(target_seg, task.key, task.size)
        
        # 传输数据
        source_replica = task.source_replicas[0]
        TransferEngine.copy(source_replica, new_replica)
        
        # 提交新副本
        Master.replace_replica(task.key, source_replica, new_replica)
        
        task.status = COMPLETED

# 任务查询
function QueryTask(task_id):
    task = Master.get_task(task_id)
    if task is None:
        return ERROR_NOT_FOUND
    return {
        id: task.id,
        type: task.type,
        status: task.status,
        progress: task.progress,
        error: task.error
    }
```

### 原理分析

**任务状态机**：

\[ \text{PENDING} \rightarrow \text{SCHEDULED} \rightarrow \text{RUNNING} \rightarrow \begin{cases} 
\text{COMPLETED} \\
\text{FAILED} \\
\text{CANCELLED}
\end{cases} \]

**任务调度策略**：

1. **优先级调度**：紧急任务（如驱逐导致的移动）优先执行
2. **负载均衡**：选择负载最轻的 Client 执行任务
3. **本地性优化**：优先在目标段所在节点执行任务，减少网络传输
4. **并发控制**：限制同时执行的任务数，避免资源耗尽

**错误处理机制**：

1. **重试策略**：传输失败时自动重试（最多3次）
2. **部分成功**：多副本复制时，部分副本失败不影响其他副本
3. **清理机制**：失败任务的空间会被自动回收
4. **超时处理**：任务执行超时后自动取消，释放资源

### 代码实践

**创建复制任务**（mooncake-store/src/client_service.cpp:3090-3098）：

```cpp
tl::expected<UUID, ErrorCode> Client::CreateCopyTask(const std::string& key,
                                                      const std::vector<std::string>& targets) {
    // 调用 Master 创建复制任务
    return master_client_.CreateCopyTask(key, targets);
}

tl::expected<UUID, ErrorCode> Client::CreateCopyTask(const std::string& key,
                                                      const std::string& tenant_id,
                                                      const std::vector<std::string>& targets) {
    // 带租户 ID 的复制任务
    return master_client_.CreateCopyTask(key, tenant_id, targets);
}
```

**创建移动任务**（mooncake-store/src/client_service.cpp:3101-3110）：

```cpp
tl::expected<UUID, ErrorCode> Client::CreateMoveTask(const std::string& key,
                                                      const std::string& source,
                                                      const std::string& target) {
    // 调用 Master 创建移动任务
    return master_client_.CreateMoveTask(key, source, target);
}

tl::expected<UUID, ErrorCode> Client::CreateMoveTask(const std::string& key,
                                                      const std::string& tenant_id,
                                                      const std::string& source,
                                                      const std::string& target) {
    // 带租户 ID 的移动任务
    return master_client_.CreateMoveTask(key, tenant_id, source, target);
}
```

**Master 端任务创建**（mooncake-store/src/master_service.cpp:6530-6586 推测）：

```cpp
tl::expected<UUID, ErrorCode> MasterService::CreateCopyTask(
    const std::string& key,
    const std::string& tenant_id,
    const std::vector<std::string>& targets) {
    
    std::lock_guard<std::shared_mutex> lock(metadata_mutex_);
    
    // 查找对象元数据
    auto it = object_metadata_.find(key);
    if (it == object_metadata_.end()) {
        return tl::unexpected(ErrorCode::KEY_NOT_FOUND);
    }
    
    auto& metadata = it->second;
    
    // 验证目标段
    for (const auto& target : targets) {
        if (!segment_manager_.is_segment_mounted(target)) {
            return tl::unexpected(ErrorCode::INVALID_SEGMENT);
        }
    }
    
    // 创建任务记录
    UUID task_id = generate_uuid();
    TaskRecord task{
        id: task_id,
        type: TaskType::COPY,
        key: key,
        tenant_id: tenant_id,
        source_replicas: metadata.replicas,
        target_segments: targets,
        status: TaskStatus::PENDING,
        created_at: CurrentTimeMs()
    };
    
    // 添加到任务管理器
    task_manager_.add_task(task);
    
    // 通知任务调度线程
    task_dispatch_cv_.notify_one();
    
    return task_id;
}
```

**任务查询接口**（mooncake-store/src/client_service.cpp 推测）：

```cpp
tl::expected<TaskInfo, ErrorCode> Client::QueryTask(const UUID& task_id) {
    auto result = master_client_.QueryTask(task_id);
    if (!result) {
        return tl::unexpected(result.error());
    }
    
    return result.value();
}

std::vector<tl::expected<TaskInfo, ErrorCode>> Client::FetchTasks(
    TaskStatus filter_status,
    int64_t limit) {
    auto result = master_client_.FetchTasks(filter_status, limit);
    return result;
}
```

**关键源码链接**：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/src/client_service.cpp#L3090-L3110

这段代码实现了异步任务的创建接口：CreateCopyTask 和 CreateMoveTask，支持单租户和多租户场景。任务创建后由 Master 的任务管理器调度执行，Client 可以通过 QueryTask 查询任务状态和进度。

### 练习题

1. **基础题**：CopyTask 和 MoveTask 的主要区别是什么，使用场景有何不同？

2. **进阶题**：异步任务机制如何保证任务执行的可靠性，避免任务丢失？

3. **实现题**：假设你要实现任务优先级调度，如何设计优先级策略和调度算法？

4. **场景题**：在集群扩容时，如何使用异步任务实现数据的重新平衡？

### 答案

1. **答案**：CopyTask 和 MoveTask 的主要区别：1) 源对象处理：CopyTask 保留源对象，MoveTask 删除源对象；2) 副本数量：CopyTask 增加副本数，MoveTask 保持副本数不变；3) 使用场景：CopyTask 用于增加副本提高读取性能或数据备份，MoveTask 用于数据迁移或负载均衡；4) 执行复杂度：MoveTask 需要额外的元数据更新和源副本清理，更复杂。

2. **答案**：异步任务可靠性保证机制：1) 任务持久化：Master 将任务记录写入持久化存储（如 etcd），崩溃恢复后重新执行；2) 状态跟踪：任务状态机清晰定义每个状态转换，确保不会遗漏；3) 心跳检测：定期检查任务执行者的存活状态，失败时重新调度；4) 重试机制：传输失败自动重试（最多3次），超过次数后标记为失败；5) 清理机制：失败任务的空间会被自动回收，避免资源泄漏。

3. **答案**：任务优先级调度设计：1) 优先级分类：紧急（如驱逐导致的移动）、高（如用户指定的迁移）、中（如副本数不足的复制）、低（如后台数据整理）；2) 优先级字段：在 TaskRecord 中添加 priority 字段和 created_at 时间戳；3) 调度算法：使用多级优先队列，相同优先级按 FIFO 排序；4) 动态调整：允许用户在运行时修改任务优先级，系统可以根据负载自动调整；5) 饥饿避免：低优先级任务等待时间过长时自动提升优先级。

4. **答案**：使用异步任务实现数据重新平衡的步骤：1) 监控集群负载，识别热点节点和空闲节点；2) 生成迁移计划，确定需要移动的对象和目标位置；3) 为每个需要移动的对象创建 MoveTask，指定源段和目标段；4) 使用 FetchTasks 定期查询任务进度，监控重新平衡的执行情况；5) 处理失败任务，重试或选择替代目标；6) 验证重新平衡后的数据分布和性能改善；7) 分批执行避免影响正常业务，优先移动热点数据。这种异步方式确保重新平衡过程对业务透明，可以在运行时动态调整。