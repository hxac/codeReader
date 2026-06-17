# 第三单元 L1：Store 架构与 Master-Client 模型

## 最小模块 1：Master-Client 架构

### 1. 概念说明

Mooncake Store 采用**集中式元数据管理 + 分布式存储**的架构。整个集群由两类关键组件构成：

- **Master Service**：作为集群的"大脑"，负责统一管理整个集群的逻辑存储空间池，处理节点加入和离开事件，执行对象空间分配和元数据维护。Master Service 运行为独立进程，通过 RPC 向外部提供服务。
  
- **Client**：在 Mooncake Store 中具有**双重身份**：
  1. 作为**客户端**：被上层应用（如 vLLM）调用来发起 Put、Get 等请求
  2. 作为**存储服务器**：托管一段连续内存，贡献给分布式 KV 缓存，使其他 Client 能够从中读取数据

这种设计的关键在于：**数据传输实际上是从一个 Client 直接传输到另一个 Client，完全绕过 Master Service**，从而避免了 Master 成为数据路径的瓶颈。

### 2. 伪代码或流程

```
# 系统初始化流程
MasterService 初始化():
    启动 RPC 服务器
    初始化元数据分片 (metadata_shards_[1024])
    初始化 Segment 管理器
    初始化 Buffer Allocator
    启动客户端监控线程
    启动驱逐线程
    启动快照线程

# Client 启动流程
Client 初始化():
    连接到 Master Service
    初始化 TransferEngine
    如果 global_segment_size > 0:
        分配本地内存段
        向 Master 注册段 (MountSegment)
    启动心跳线程
    启动任务轮询线程

# 数据请求流程（以 Get 为例）
Client 发起 Get 请求(key):
    1. 向 Master 查询副本列表 (GetReplicaList)
    2. Master 返回副本位置信息（包含存储节点地址、内存偏移等）
    3. Client 直接通过 TransferEngine 从存储节点拉取数据
    4. 数据写入本地目标内存
```

### 3. 原理分析

#### 架构优势分析

Mooncake Store 的架构设计基于以下核心原理：

**控制流与数据流分离**：Master Service 仅负责元数据管理（控制流），不参与实际数据传输（数据流）。这种分离带来了两个关键优势：

1. **Master 可扩展性**：由于 Master 不处理数据流，其 CPU 和网络开销相对较小，可以专注于元数据管理，支持更大规模的集群
2. **数据路径优化**：数据直接在 Client 之间传输，减少了中间环节，降低了延迟并提高了带宽利用率

#### 元数据分片机制

为了支持高并发访问，Master Service 使用**1024 个元数据分片**（`metadata_shards_`）来分散锁竞争：

```
分片索引计算：
shard_idx = hash(tenant_id + key) % 1024

每个分片包含：
- tenant_states: map<string, TenantState>  # 租户状态
- mutex: SharedMutex  # 读写锁
```

这种设计使得多个 key 的元数据操作可以在不同分片上并发执行，大大提高了吞吐量。

#### Client 双重角色实现

Client 通过配置参数来控制其行为模式：

- `global_segment_size = 0`：纯客户端模式，只发起请求，不贡献内存
- `local_buffer_size = 0`：纯服务器模式，只提供内存，不发起请求
- 两者都大于 0：正常模式，同时具备两种能力

### 4. 代码实践

#### MasterService 核心结构

MasterService 类定义了 Master 的核心职责和数据结构：

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_service.h#L68-L73
class MasterService {
    // Lock order: To avoid deadlocks, the following lock order should be followed:
    // 1. client_mutex_
    // 2. metadata_shards_[shard_idx_].mutex
    // 3. segment_mutex_
};
```

这里定义了严格的加锁顺序来避免死锁：client_mutex_ → 元数据分片锁 → segment_mutex_。

#### 元数据分片定义

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_service.h#L1142-L1167
static constexpr size_t kNumShards = 1024;  // Number of metadata shards

struct TenantState {
    std::unordered_map<std::string, ObjectMetadata> metadata;
    std::unordered_set<std::string> processing_keys;
    std::unordered_map<std::string, const ReplicationTask> replication_tasks;
    std::unordered_map<std::string, PromotionTask> promotion_tasks;
    std::unordered_map<std::string, std::unordered_set<std::string>> group_members;
};

// Sharded metadata maps and their mutexes
struct MetadataShard {
    mutable SharedMutex mutex;
    std::unordered_map<std::string, TenantState> tenants GUARDED_BY(mutex);
};
std::array<MetadataShard, kNumShards> metadata_shards_;
```

每个分片都有独立的读写锁，支持高并发的元数据访问。`GUARDED_BY(mutex)` 注解表明该成员必须持有对应锁才能访问。

#### 分片索引计算

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_service.h#L1241-L1255
// Helper to get shard index from tenant-scoped object identity.
size_t getShardIndex(const std::string& tenant_id,
                     const std::string& user_key) const {
    const auto normalized_tenant = NormalizeTenantId(tenant_id);
    if (normalized_tenant == "default") {
        return std::hash<std::string>{}(user_key) % kNumShards;
    }
    size_t seed = std::hash<std::string>{}(normalized_tenant);
    boost::hash_combine(seed, user_key);
    return seed % kNumShards;
}
```

这段代码展示了如何根据租户 ID 和 key 计算分片索引。对于 default 租户，直接使用 key 的哈希；对于其他租户，将租户 ID 和 key 组合计算哈希。

#### Client 的双重角色

Client 类同时扮演客户端和存储服务器的角色：

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_service.h#L63-L91
class Client {
 public:
    /**
     * @brief Creates and initializes a new Client instance
     * @param local_hostname Local host address (IP:Port)
     * @param metadata_connstring Connection string for metadata service
     * @param protocol Transfer protocol ("rdma" or "tcp")
     * @param master_server_entry The entry of master server
     */
    static std::optional<std::shared_ptr<Client>> Create(
        const std::string& local_hostname,
        const std::string& metadata_connstring, 
        const std::string& protocol,
        const std::optional<std::string>& device_names = std::nullopt,
        const std::string& master_server_entry = kDefaultMasterAddress,
        const std::shared_ptr<TransferEngine>& transfer_engine = nullptr,
        std::map<std::string, std::string> labels = {},
        const std::string& tenant_id = "default");
    
    // 客户端角色：发起数据请求
    tl::expected<void, ErrorCode> Get(const std::string& object_key,
                                      std::vector<Slice>& slices);
    tl::expected<void, ErrorCode> Put(const ObjectKey& key,
                                      std::vector<Slice>& slices,
                                      const ReplicateConfig& config);
    
    // 存储服务器角色：贡献内存资源
    tl::expected<void, ErrorCode> MountSegment(
        const void* buffer, size_t size, 
        const std::string& protocol = "tcp",
        const std::string& location = kWildcardLocation);
};
```

Client 类同时提供了 Get/Put（客户端操作）和 MountSegment（服务器端操作）接口，体现了其双重角色。

### 5. 练习题

#### 练习 1：基础理解
Mooncake Store 为什么要采用控制流与数据流分离的架构？这种设计有什么优势和潜在挑战？

#### 练习 2：分片机制
假设有一个集群，有 1000 个对象，分布在 10 个租户中。如果所有对象的 key 都是随机的 10 字符字符串，分析元数据分片如何影响并发性能？

#### 练习 3：Client 角色
在以下场景中，Client 应该如何配置？
- 场景 A：一个专用的存储节点，不运行推理任务，只提供内存
- 场景 B：一个推理实例，内存紧张，只能作为数据消费者
- 场景 C：一个推理实例，有充足内存，想同时贡献和消费内存

#### 练习 4：死锁预防
MasterService 定义了严格的锁顺序（client_mutex_ → metadata_shards_[shard_idx_].mutex → segment_mutex_）。分析为什么要这样设计，以及违反这个顺序会发生什么？

### 6. 答案

#### 答案 1
控制流与数据流分离的优势：
- **Master 可扩展性**：Master 不处理数据流，开销相对较小，可以支持更大规模集群
- **低延迟数据传输**：数据直接在 Client 之间传输，减少中间环节，降低延迟
- **高带宽利用率**：利用 TransferEngine 的 RDMA 等技术，充分利用网络带宽

潜在挑战：
- **实现复杂度高**：需要 TransferEngine 支持点对点数据传输
- **故障隔离复杂**：数据传输发生在 Client 之间，故障诊断和恢复更复杂
- **一致性保证困难**：Master 不参与数据流，需要额外机制保证数据一致性

#### 答案 2
分片机制对并发性能的影响：

**有利因素**：
- 1000 个对象分布在 1024 个分片中，平均每个分片不到 1 个对象
- 10 个租户的对象可能分布在不同分片，减少租户间干扰
- 热点 key 可能集中在少数分片，但整体锁竞争较小

**不利因素**：
- 如果某些 key 特别热门，可能导致分片热点
- 跨租户操作（如 RemoveAll）需要遍历所有分片
- 分片数量固定（1024），在某些极端情况下可能不够

#### 答案 3
场景配置建议：

- **场景 A（专用存储节点）**：
  - `global_segment_size`：大值（如 100GB）
  - `local_buffer_size`：0 或小值
  - 这样 Client 主要作为存储服务器，内存资源可供其他节点使用

- **场景 B（内存紧张的推理实例）**：
  - `global_segment_size`：0
  - `local_buffer_size`：必要值（用于临时缓冲）
  - 这样 Client 只作为消费者，不贡献内存

- **场景 C（内存充足的推理实例）**：
  - `global_segment_size`：适中值（如 50GB）
  - `local_buffer_size`：必要值
  - 这样 Client 同时作为消费者和存储服务器，充分利用内存资源

#### 答案 4
锁顺序设计的原因：

**为什么需要这个顺序**：
- **client_mutex_ 最外层**：客户端列表变化会影响元数据和段，需要最外层保护
- **metadata_shards_[shard_idx_].mutex 中间层**：元数据操作可能需要访问段信息
- **segment_mutex_ 最内层**：段操作最底层，只涉及具体段

**违反顺序的后果**：
假设两个线程分别执行：
- 线程 1：持有 segment_mutex_，尝试获取 client_mutex_
- 线程 2：持有 client_mutex_，尝试获取同一个 segment_mutex_

这会导致**循环等待**，形成死锁。严格的全局顺序可以避免这种情况。

---

## 最小模块 2：控制流与数据流分离

### 1. 概念说明

Mooncake Store 的核心设计原则是**控制流与数据流分离**：

- **控制流**：Master Service 负责元数据管理，包括对象位置、副本状态、空间分配等。Client 通过 RPC 与 Master 交互获取控制信息。
  
- **数据流**：实际的数据传输直接在 Client 之间进行，通过 TransferEngine 使用 RDMA 或 TCP 协议传输，完全绕过 Master Service。

这种分离设计确保了 Master 不会成为数据传输的瓶颈，使其能够专注于元数据管理，从而支持更大规模的集群和更高的吞吐量。

### 2. 伪代码或流程

```
# Put 操作的控制流与数据流
Client.Put(key, data, replica_config):
    # 控制流：与 Master 交互
    1. 向 Master 发送 PutStart 请求
    2. Master 分配存储空间，返回副本位置信息
    3. 接收 Master 返回的 replica_list（包含存储节点地址、内存偏移等）
    
    # 数据流：与存储节点直接交互
    4. 通过 TransferEngine 将数据直接传输到各个存储节点
    5. 等待所有传输完成
    
    # 控制流：通知 Master 完成
    6. 向 Master 发送 PutEnd 请求
    7. Master 更新元数据，标记对象为可读

# Get 操作的控制流与数据流
Client.Get(key, target_buffer):
    # 控制流：与 Master 交互
    1. 向 Master 发送 GetReplicaList 请求
    2. Master 查询元数据，返回副本位置信息
    3. 接收 Master 返回的 replica_list
    
    # 数据流：与存储节点直接交互
    4. 选择最近的或负载最轻的副本
    5. 通过 TransferEngine 直接从存储节点拉取数据
    6. 数据写入本地 target_buffer
```

### 3. 原理分析

#### 分离设计的数学模型

假设：
- \( N \)：集群中的 Client 数量
- \( S \)：平均对象大小
- \( B \)：网络带宽
- \( L_M \)：Master 处理一个 RPC 请求的延迟
- \( L_D \)：Client 间直接传输数据的延迟

**传统集中式架构**（Master 参与数据流）：
- 数据路径延迟：\( 2 \times L_M + L_D \)（请求 → Master → 存储节点 → Master → 客户端）
- Master 网络负载：\( N \times S \times B \)（所有数据都经过 Master）

**分离式架构**（控制流与数据流分离）：
- 数据路径延迟：\( L_M + L_D \)（请求 → Master → 客户端直接与存储节点通信）
- Master 网络负载：仅元数据流量（通常远小于数据流量）

分离式架构的延迟降低为：
\[ \Delta L = \frac{2 \times L_M + L_D}{L_M + L_D} - 1 \approx \frac{L_M}{L_D} \]

当 \( L_D \gg L_M \) 时（大对象传输），延迟降低接近 1 倍。

#### Master 不处理数据流的关键机制

Master Service 通过以下机制确保不参与数据流：

1. **BufHandle 机制**：Master 分配空间时返回 BufHandle，包含存储节点的地址和内存偏移，Client 可以直接定位数据位置：

```cpp
struct BufHandle {
    required uint64 segment_name;  // 存储段名称（对应存储节点）
    required uint64 size;          // 分配空间大小
    required uint64 buffer;        // 分配空间的起始地址
    required BufStatus status;      // 空间状态
};
```

2. **TransferEngine 抽象**：Client 使用 TransferEngine 进行点对点数据传输，Master 完全不参与：

```cpp
ErrorCode TransferData(const Replica::Descriptor& replica_descriptor,
                       std::vector<Slice>& slices,
                       TransferRequest::OpCode op_code) {
    // 直接通过 TransferEngine 与目标节点通信
    return transfer_engine_->submitTransferRequest(req);
}
```

3. **元数据服务独立**：TransferEngine 需要的元数据服务（etcd、Redis、HTTP）与 Master Service 分离部署，Master 专注于对象元数据管理。

### 4. 代码实践

#### Master 的控制流 API：PutStart/PutEnd

Master Service 提供了 PutStart 和 PutEnd 接口来控制 Put 操作的生命周期：

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_service.h#L309-L312
auto PutStart(const UUID& client_id, const std::string& key,
              const std::string& tenant_id, const uint64_t slice_length,
              const ReplicateConfig& config)
    -> tl::expected<std::vector<Replica::Descriptor>, ErrorCode>;
```

PutStart 分配存储空间并返回副本描述符，但不传输任何数据。

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_service.h#L320-L322
auto PutEnd(const UUID& client_id, const std::string& key,
            const std::string& tenant_id, ReplicaType replica_type)
    -> tl::expected<void, ErrorCode>;
```

PutEnd 标记对象写入完成，使其对其他 Client 可见。

#### Client 的数据流实现：TransferData

Client 通过 TransferData 方法实现点对点数据传输：

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_service.h#L672-L674
ErrorCode TransferData(const Replica::Descriptor& replica_descriptor,
                       std::vector<Slice>& slices,
                       TransferRequest::OpCode op_code);
```

这个方法直接通过 TransferEngine 与目标存储节点通信，Master 完全不参与。

#### Get 操作的分离实现

Client 的 Get 操作展示了控制流与数据流的分离：

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_service.h#L99-L100
tl::expected<void, ErrorCode> Get(const std::string& object_key,
                                  std::vector<Slice>& slices);
```

Get 操作内部首先通过 Master 查询副本列表（控制流），然后直接从存储节点拉取数据（数据流）：

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_service.h#L128-L128
tl::expected<QueryResult, ErrorCode> Query(const std::string& object_key);
```

Query 返回的 QueryResult 包含副本位置和租约超时信息，Client 可以据此直接访问数据：

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_service.h#L39-L54
class QueryResult {
 public:
    const std::vector<Replica::Descriptor> replicas;
    const std::chrono::steady_clock::time_point lease_timeout;
    
    bool IsLeaseExpired() const {
        return std::chrono::steady_clock::now() >= lease_timeout;
    }
};
```

#### 设计文档的明确说明

设计文档明确强调了 Master 不参与数据流：

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/design/mooncake-store.md#L74-L76
The cluster's available resources are viewed as a large resource pool, 
managed centrally by a Master process for space allocation and guiding 
data replication

**Note: The Master Service does not take over any data flow, only 
providing corresponding metadata information.**
```

这段设计文档明确指出：Master Service 不接管任何数据流，只提供相应的元数据信息。

### 5. 练习题

#### 练习 1：性能分析
假设有一个集群，有 10 个 Client，每个对象大小为 100 MB，网络带宽为 10 Gbps，Master 处理 RPC 的延迟为 1 ms。计算分离式架构相比传统集中式架构的延迟降低比例。

#### 练习 2：故障场景
如果 Master Service 发生崩溃，正在进行的数据传输会受影响吗？为什么？

#### 练习 3：扩展性分析
假设集群规模从 10 个 Client 扩展到 1000 个 Client，分析分离式架构下 Master Service 的负载会如何变化？

#### 练习 4：设计权衡
控制流与数据流分离的设计有什么潜在缺点？在什么场景下这种缺点可能成为瓶颈？

### 6. 答案

#### 答案 1
延迟分析：

**传统架构**：
- 数据路径延迟：\( 2 \times L_M + L_D = 2 \times 1\,ms + \frac{100\,MB}{10\,Gbps} = 2\,ms + 80\,ms = 82\,ms \)
- 总延迟：82 ms

**分离式架构**：
- 数据路径延迟：\( L_M + L_D = 1\,ms + 80\,ms = 81\,ms \)
- 总延迟：81 ms

**延迟降低**：
\[ \frac{82\,ms - 81\,ms}{82\,ms} \approx 1.2\% \]

对于大对象传输，Master RPC 延迟在总延迟中占比较小，但分离式架构的优势在于：
- Master 不承担数据流量，可以支持更多并发请求
- Client 之间直连，可以利用更优的网络路径

#### 答案 2
Master 崩溃对数据传输的影响：

**不受影响的部分**：
- **正在进行的点对点数据传输**：一旦 Client 获得副本位置信息，数据传输直接在 Client 之间进行，与 Master 无关
- **本地缓存的数据**：已经传输到 Client 的数据不受影响

**受影响的部分**：
- **新的 Get/Put 请求**：无法获取元数据，无法发起新的操作
- **PutEnd 请求**：无法标记写入完成，对象可能处于不可见状态
- **租约刷新**：现有租约到期后无法刷新，对象可能被驱逐

**恢复机制**：
- 高可用模式下，新的 Master 会通过 etcd 选举产生
- 客户端会自动重连到新的 Master
- 快照恢复机制可以恢复元数据状态

#### 答案 3
Master 负载变化分析：

**Master 的主要负载**：
1. **元数据查询**：GetReplicaList、ExistKey 等
2. **空间分配**：PutStart、UpsertStart 等
3. **心跳处理**：Client 心跳、Segment 状态监控
4. **驱逐决策**：内存空间不足时触发驱逐

**扩展到 1000 个 Client 的负载变化**：

1. **元数据操作**：假设每个 Client 的请求率不变，总请求数增长 100 倍
   - 通过 1024 个分片可以分散锁竞争
   - 每个分片的负载增加约 10 倍（1000/1024 ≈ 1）

2. **心跳处理**：从 10 个心跳增加到 1000 个心跳
   - 心跳处理线程需要处理 100 倍的心跳消息
   - 需要优化心跳队列和处理逻辑

3. **驱逐决策**：对象数量增长 100 倍，驱逐扫描成本增加
   - 需要更智能的驱逐策略，避免全表扫描

**结论**：
- 分离式架构使 Master 的负载主要集中在元数据管理，可以支持更大的集群
- 但仍需要优化元数据存储和访问模式，以支持更大规模

#### 答案 4
潜在缺点和瓶颈场景：

**潜在缺点**：
1. **实现复杂度高**：
   - 需要实现 TransferEngine 支持点对点传输
   - 需要处理网络分区、节点故障等分布式问题
   - 一致性保证更复杂（Master 不参与数据流）

2. **故障诊断困难**：
   - 数据传输发生在 Client 之间，故障定位更困难
   - 需要额外的监控和调试机制

3. **元数据一致性**：
   - Master 和 Client 之间的元数据视图可能不一致
   - 需要租约、版本号等机制保证一致性

**瓶颈场景**：
1. **高频小对象**：
   - 控制流开销相对较大
   - RPC 延迟在总延迟中占比较高

2. **高并发元数据操作**：
   - 大量 Client 同时访问 Master
   - 元数据分片可能成为瓶颈

3. **网络不稳定环境**：
   - 点对点传输更易受网络影响
   - 需要复杂的重试和故障恢复机制

---

## 最小模块 3：RPC 协议

### 1. 概念说明

Mooncake Store 使用 gRPC/Protobuf 定义 Master-Client 之间的 RPC 协议。这些 RPC 接口分为以下几类：

- **段管理**：MountSegment、UnmountSegment、ReMountSegment
- **对象操作**：PutStart、PutEnd、UpsertStart、UpsertEnd、Remove、RemoveByRegex
- **元数据查询**：GetReplicaList、GetReplicaListByRegex、ExistKey、BatchQueryIp
- **任务管理**：CreateCopyTask、CreateMoveTask、QueryTask、FetchTasks

这些 RPC 接口构成了 Master-Client 交互的控制平面，实现了元数据的集中管理。

### 2. 伪代码或流程

```
# RPC 协议的核心接口
service MasterService {
    # 段管理
    rpc MountSegment(MountSegmentRequest) returns (MountSegmentResponse);
    rpc UnmountSegment(UnmountSegmentRequest) returns (UnmountSegmentResponse);
    
    # 对象写入
    rpc PutStart(PutStartRequest) returns (PutStartResponse);
    rpc PutEnd(PutEndRequest) returns (PutEndResponse);
    
    # 元数据查询
    rpc GetReplicaList(GetReplicaListRequest) returns (GetReplicaListResponse);
    
    # 对象删除
    rpc Remove(RemoveRequest) returns (RemoveResponse);
}

# 关键消息类型定义
message ReplicaInfo {
    repeated BufHandle handles = 1;  # 存储位置信息
    required ReplicaStatus status = 2;
}

message BufHandle {
    required uint64 segment_name = 1;  # 存储段名称
    required uint64 size = 2;
    required uint64 buffer = 3;       # 内存地址
    required BufStatus status = 4;
}
```

### 3. 原理分析

#### RPC 协议设计原则

Mooncake Store 的 RPC 协议遵循以下设计原则：

1. **两阶段提交**：Put 操作分为 PutStart 和 PutEnd 两个阶段
   - PutStart：分配空间，返回副本位置
   - PutEnd：标记写入完成，使对象可见
   
   这样避免了"脏读"，防止其他 Client 读取未完全写入的对象。

2. **批量操作**：提供 BatchPut、BatchGet、BatchRemove 等批量接口
   - 减少RPC 调用次数
   - 提高吞吐量
   - 降低网络开销

3. **正则表达式查询**：GetReplicaListByRegex、RemoveByRegex
   - 支持批量查询和管理
   - 方便运维和调试

4. **任务异步化**：CreateCopyTask、CreateMoveTask
   - 大对象复制/移动异步执行
   - 避免阻塞主线程
   - 支持进度查询

#### 两阶段 Put 协议的状态机

Put 操作的完整生命周期：

```
对象状态转换：
PROCESSING (PutStart) → COMPLETE (PutEnd) → REMOVED (Remove)

PutStart 阶段：
1. Client 调用 PutStart
2. Master 分配空间，创建 ObjectMetadata，状态为 PROCESSING
3. Master 返回 replica_list
4. Client 将数据传输到存储节点

PutEnd 阶段：
1. Client 调用 PutEnd
2. Master 检查所有副本是否完成
3. Master 更新 ObjectMetadata 状态为 COMPLETE
4. 对象对其他 Client 可见

故障处理：
- 如果 PutStart 后未调用 PutEnd，对象会被超时回收（put_start_release_timeout）
- 如果 Client 崩溃，对象处于"僵尸"状态，会被驱逐或抢占
```

#### 副本描述符的结构

Replica::Descriptor 是 RPC 协议中返回的核心数据结构：

```cpp
struct Replica::Descriptor {
    ReplicaID id;              // 副本唯一标识
    std::string segment_name;  // 存储段名称
    uint64_t offset;           // 内存偏移
    uint64_t length;           // 副本长度
    ReplicaType type;          // 副本类型（MEMORY/DISK/LOCAL_DISK）
    ReplicaStatus status;      // 副本状态
};
```

Client 根据这些信息可以直接定位和访问副本，无需再次询问 Master。

### 4. 代码实践

#### Protobuf 定义的 RPC 服务

设计文档中定义了完整的 Protobuf 接口：

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/design/mooncake-store.md#L127-L158
service MasterService {
  // Get the list of replicas for an object
  rpc GetReplicaList(GetReplicaListRequest) returns (GetReplicaListResponse);

  // Start Put operation, allocate storage space
  rpc PutStart(PutStartRequest) returns (PutStartResponse);

  // End Put operation, mark object write completion
  rpc PutEnd(PutEndRequest) returns (PutEndResponse);

  // Delete all replicas of an object
  rpc Remove(RemoveRequest) returns (RemoveResponse);

  // Storage node (Client) registers a storage segment
  rpc MountSegment(MountSegmentRequest) returns (MountSegmentResponse);

  // Storage node (Client) unregisters a storage segment
  rpc UnmountSegment(UnmountSegmentRequest) returns (UnmountSegmentResponse);
}
```

这个定义展示了 Master-Client 协议的核心接口。

#### PutStart/PutEnd 的请求响应结构

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/design/mooncake-store.md#L240-L272
message PutStartRequest {
  required string key = 1;             // Object key
  required int64 value_length = 2;     // Total length of data to be written
  required ReplicateConfig config = 3; // Replica configuration information
  repeated uint64 slice_lengths = 4;   // Lengths of each data slice
};

message PutStartResponse {
  required int32 status_code = 1;
  repeated ReplicaInfo replica_list = 2;  // Replica information allocated by Master
};

message PutEndRequest {
  required string key = 1;
};

message PutEndResponse {
  required int32 status_code = 1;
};
```

PutStart 返回的 replica_list 包含了存储位置的完整信息，Client 可以据此直接传输数据。

#### GetReplicaList 接口

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/design/mooncake-store.md#L160-L176
message GetReplicaListRequest {
  required string key = 1;
};

message GetReplicaListResponse {
  required int32 status_code = 1;
  repeated ReplicaInfo replica_list = 2; // List of replica information
};
```

GetReplicaList 是 Master-Client 协议中最频繁调用的接口，Client 需要先调用它获取副本位置，才能执行 Get 操作。

#### MountSegment 接口

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/design/mooncake-store.md#L307-L319
message MountSegmentRequest {
  required uint64 buffer = 1;       // Starting address of the space
  required uint64 size = 2;         // Size of the space
  required string segment_name = 3; // Storage segment name
}

message MountSegmentResponse {
  required int32 status_code = 1;
};
```

MountSegment 是 Client 向 Master 注册存储资源的接口。Master 根据 segment_name 和 size 创建对应的 BufferAllocator，后续的空间分配都会从这个 Allocator 中进行。

#### MasterService 的 RPC 实现

MasterService 类实现了这些 RPC 接口：

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_service.h#L298-L299
auto GetReplicaList(const std::string& key, const std::string& tenant_id)
    -> tl::expected<GetReplicaListResponse, ErrorCode>;
```

这个方法返回的是 `GetReplicaListResponse`，包含副本列表和状态码。

### 5. 练习题

#### 练习 1：协议设计
为什么 Put 操作要分为 PutStart 和 PutEnd 两个阶段？如果只有一个 Put 接口会有什么问题？

#### 练习 2：故障恢复
假设 Client 在 PutStart 后崩溃，没有调用 PutEnd。Master 如何处理这种"僵尸对象"？

#### 练习 3：批量优化
假设需要写入 100 个小对象（每个 1 MB），分别使用单个 Put 和 BatchPut，分析 RPC 调用次数和网络开销的差异。

#### 练习 4：副本选择
GetReplicaList 返回多个副本后，Client 应该如何选择最优副本？考虑网络距离、负载均衡等因素。

### 6. 答案

#### 答案 1
两阶段 Put 的设计原因：

**避免脏读**：
- 如果只有一个 Put 接口，Client 调用后对象立即可见
- 其他 Client 可能读取到未完全写入的数据
- 两阶段设计确保只有 PutEnd 后对象才可见

**支持异步写入**：
- PutStart 后，Client 可以异步传输数据
- 不需要阻塞 RPC 连接
- 适合大对象写入

**故障隔离**：
- PutStart 失败，Client 知道空间分配失败
- PutEnd 失败，对象状态明确，可以重试或回滚
- 单一 Put 接口难以区分是空间分配失败还是数据传输失败

**并发控制**：
- Master 可以通过 PROCESSING 状态跟踪正在写入的对象
- 驱逐时跳过 PROCESSING 对象，避免驱逐正在写入的对象

#### 答案 2
僵尸对象的处理机制：

**超时回收**：
```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_service.h#L1782-L1783
const std::chrono::seconds put_start_discard_timeout_sec_;
const std::chrono::seconds put_start_release_timeout_sec_;
```

Master 记录每个 PutStart 的开始时间，两个超时阈值：
- `put_start_discard_timeout`（默认 30 秒）：超时后允许新的 PutStart "抢占"旧的
- `put_start_release_timeout`（默认 10 分钟）：超时后释放分配的空间

**驱逐优先级**：
- 驱逐时优先释放这些僵尸对象的副本
- 因为它们永远不会被 PutEnd，可以安全回收

**定期清理**：
```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_service.h#L1297-L1299
void DiscardExpiredProcessingReplicas(
    MetadataShardAccessorRW& shard,
    const std::chrono::system_clock::time_point& now);
```

Master 定期扫描并清理超时的 PROCESSING 对象。

#### 答案 3
批量优化分析：

**单个 Put（100 次）**：
- RPC 调用次数：200 次（100 次 PutStart + 100 次 PutEnd）
- 网络开销：200 次 RPC 往返 + 数据传输
- 延迟：串行执行，总延迟为单次延迟的 100 倍

**BatchPut（1 次）**：
- RPC 调用次数：2 次（1 次 BatchPutStart + 1 次 BatchPutEnd）
- 网络开销：2 次 RPC 往返 + 数据传输
- 延迟：并行执行，总延迟接近单次延迟

**优化效果**：
- RPC 调用减少：200 次 → 2 次（减少 99%）
- 网络开销降低：RPC 往返大幅减少
- 延迟降低：并行执行，总延迟大幅减少

**注意事项**：
- BatchPut 需要所有对象的切片信息提前准备好
- 如果某个对象失败，可能影响整批操作
- 需要权衡批量大小和失败重试成本

#### 答案 4
副本选择策略：

**网络距离**：
- 优先选择同一网络节点的副本（低延迟）
- 避免跨网络传输（高延迟、低带宽）

**负载均衡**：
- 查询副本的引用计数，选择负载较轻的副本
- 避免所有请求集中到单个副本

**副本状态**：
- 优先选择 COMPLETE 状态的副本
- 避免 PROCESSING 或 FAILED 状态的副本

**实现示例**：
```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_service.h#L588-L589
tl::expected<Replica::Descriptor, ErrorCode> GetPreferredReplica(
    const std::vector<Replica::Descriptor>& replica_list);
```

Client 提供了 GetPreferredReplica 方法来选择最优副本：
- 首先过滤出 COMPLETE 状态的副本
- 然后根据本地性（是否在本地内存）、网络拓扑等信息排序
- 最后返回最优副本

---

## 最小模块 4：元数据管理

### 1. 概念说明

Mooncake Store 的元数据管理是 Master Service 的核心职责，包括：

- **对象元数据**：key → ObjectMetadata（包含副本列表、大小、租约、pin 状态等）
- **段元数据**：segment_name → Segment（包含地址、大小、状态、Allocator 等）
- **客户端元数据**：client_id → ClientInfo（包含地址、状态、挂载的段等）

这些元数据被分片存储（1024 个分片），使用读写锁保护，支持高并发访问。

### 2. 伪代码或流程

```
# 元数据查询流程
GetReplicaList(key, tenant_id):
    1. 计算分片索引：shard_idx = hash(tenant_id + key) % 1024
    2. 获取分片读锁
    3. 查询 tenants[tenant_id].metadata[key]
    4. 如果存在且有效：
        - 刷新租约超时时间
        - 返回副本列表
    5. 如果不存在或无效：
        - 返回 OBJECT_NOT_FOUND
    6. 释放读锁

# 元数据插入流程（PutStart）
PutStart(key, tenant_id, size, config):
    1. 计算分片索引：shard_idx = hash(tenant_id + key) % 1024
    2. 获取分片写锁
    3. 检查 key 是否已存在
    4. 如果不存在：
        - 通过 AllocationStrategy 分配空间
        - 创建 ObjectMetadata(size, replicas, client_id, ...)
        - 插入 tenants[tenant_id].metadata[key]
        - 添加到 processing_keys
    5. 释放写锁
    6. 返回副本描述符列表

# 租约管理流程
ExistKey(key, tenant_id):
    1. 计算分片索引并获取读锁
    2. 查询 tenants[tenant_id].metadata[key]
    3. 如果存在：
        - 更新 lease_timeout = now + ttl
        - 如果有 group_id，刷新整个组的租约
        - 返回 true
    4. 如果不存在：
        - 返回 false
    5. 释放读锁
```

### 3. 原理分析

#### ObjectMetadata 结构分析

ObjectMetadata 是元数据的核心结构，包含对象的所有管理信息：

```cpp
struct ObjectMetadata {
    UUID client_id;                            // 写入客户端 ID
    std::chrono::system_clock::time_point put_start_time;  // PutStart 时间
    const size_t size;                         // 对象大小
    const ObjectDataType data_type;            // 数据类型（UNKNOWN/MEMORY/DISK）
    const std::string group_id;                // 组 ID（可选）
    const std::string tenant_id;               // 租户 ID
    const std::string user_key;                // 用户 key
    
    mutable SpinLock lock;
    mutable std::chrono::system_clock::time_point lease_timeout;     // 硬租约超时
    mutable std::optional<std::chrono::system_clock::time_point> soft_pin_timeout;  // 软 pin 超时
    const bool hard_pinned;                    // 是否硬 pin
    
    std::vector<Replica> replicas_;            // 副本列表
};
```

关键设计：
- **租约机制**：`lease_timeout` 保护对象不被驱逐或删除
- **Pin 机制**：`soft_pin_timeout` 和 `hard_pinned` 提供不同级别的保护
- **组支持**：`group_id` 允许相关对象共享生命周期
- **不可变性**：大部分字段不可变，避免并发修改

#### 元数据分片与并发控制

元数据分片的设计考虑：

1. **分片数量**：1024 个分片
   - 足够分散锁竞争
   - 内存开销可控（每个分片一个 hashmap）
   - 分片索引计算快速（哈希取模）

2. **锁粒度**：每个分片一个读写锁
   - 读操作（GetReplicaList）可以并发
   - 写操作（PutStart、Remove）互斥
   - 不同分片的操作不冲突

3. **租户隔离**：每个租户独立的命名空间
   - `tenants[tenant_id].metadata[key]`
   - 不同租户的元数据物理分离

#### 元数据访问器模式

MasterService 使用 RAII 风格的访问器来自动管理锁和清理：

```cpp
class MetadataAccessorRW {
    // 构造时自动获取写锁
    MetadataAccessorRW(MasterService* service, const ObjectIdentity& object_id);
    
    // 析构时自动释放锁
    ~MetadataAccessorRW();
    
    // 提供便捷的元数据操作
    bool Exists();
    void Erase();
    void Create(...);
};
```

这种模式确保：
- 异常安全：异常发生时锁会自动释放
- 自动清理：访问器析构时清理无效副本
- 代码简洁：不需要手动管理锁生命周期

### 4. 代码实践

#### ObjectMetadata 完整定义

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_service.h#L796-L1094
struct ObjectMetadata {
    // RAII-style metric management
    ~ObjectMetadata() {
        MasterMetricManager::instance().dec_key_count(1);
        if (soft_pin_timeout) {
            MasterMetricManager::instance().dec_soft_pin_key_count(1);
        }
    }

    ObjectMetadata(
        const UUID& client_id_,
        const std::chrono::system_clock::time_point put_start_time_,
        size_t value_length, std::vector<Replica>&& reps,
        bool enable_soft_pin, bool enable_hard_pin = false,
        ObjectDataType data_type_ = ObjectDataType::UNKNOWN,
        std::string group_id_ = "", std::string tenant_id_ = "default",
        std::string user_key_ = {})
        : client_id(client_id_),
          put_start_time(put_start_time_),
          size(value_length),
          data_type(data_type_),
          group_id(std::move(group_id_)),
          tenant_id(std::move(tenant_id_)),
          user_key(std::move(user_key_)),
          lease_timeout(),
          soft_pin_timeout(std::nullopt),
          hard_pinned(enable_hard_pin),
          replicas_(std::move(reps)) {
        MasterMetricManager::instance().inc_key_count(1);
        if (enable_soft_pin) {
            soft_pin_timeout.emplace();
            MasterMetricManager::instance().inc_soft_pin_key_count(1);
        }
        MasterMetricManager::instance().observe_value_size(value_length);
    }
    
    // 租约管理
    void GrantLease(const uint64_t ttl, const uint64_t soft_ttl) const;
    bool IsLeaseExpired() const;
    bool IsSoftPinned() const;
    bool IsHardPinned() const;
    
    // 副本管理
    void AddReplicas(std::vector<Replica>&& replicas);
    std::vector<Replica> PopReplicas();
    bool HasMemReplica() const;
    size_t GetMemReplicaCount() const;
    
    // 验证
    bool IsValid() const;
};
```

这个结构展示了对象元数据的完整生命周期管理，包括指标统计、租约管理、副本管理等。

#### 元数据分片定义

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_service.h#L1163-L1167
struct MetadataShard {
    mutable SharedMutex mutex;
    std::unordered_map<std::string, TenantState> tenants GUARDED_BY(mutex);
};
std::array<MetadataShard, kNumShards> metadata_shards_;
```

每个分片包含一个租户状态映射，使用 `GUARDED_BY(mutex)` 注解表明访问需要持有锁。

#### 租约授予机制

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_service.h#L1014-L1025
void GrantLease(const uint64_t ttl, const uint64_t soft_ttl) const {
    SpinLocker locker(&lock);
    std::chrono::system_clock::time_point now =
        std::chrono::system_clock::now();
    lease_timeout =
        std::max(lease_timeout, now + std::chrono::milliseconds(ttl));
    if (soft_pin_timeout) {
        soft_pin_timeout =
            std::max(*soft_pin_timeout,
                     now + std::chrono::milliseconds(soft_ttl));
    }
}
```

租约授予只增加超时时间，不减少现有租约。这样可以避免频繁刷新导致的租约抖动。

#### 元数据访问器的 RAII 设计

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_service.h#L1393-L1456
class MetadataAccessorRW {
   public:
    MetadataAccessorRW(MasterService* service,
                       const ObjectIdentity& object_id)
        : service_(service),
          object_id_(object_id),
          shard_idx_(service_->getMetadataShardIndex(object_id_.tenant_id,
                                                     object_id_.user_key)),
          shard_guard_(service_, shard_idx_),
          tenant_it_(shard_guard_->tenants.find(object_id_.tenant_id)),
          tenant_state_(tenant_it_ == shard_guard_->tenants.end()
                            ? nullptr
                            : &tenant_it_->second),
          it_(tenant_state_ == nullptr
                  ? ObjectMetadataIterator{}
                  : tenant_state_->metadata.find(object_id_.user_key)) {
        // Automatically clean up invalid handles
        if (tenant_state_ != nullptr &&
            it_ != tenant_state_->metadata.end()) {
            it_->second.EraseReplicas([](const Replica& replica) {
                return replica.has_invalid_mem_handle();
            });
            if (!it_->second.IsValid()) {
                this->Erase();
            }
        }
    }

    // Check if metadata exists
    bool Exists() const NO_THREAD_SAFETY_ANALYSIS {
        return tenant_state_ != nullptr &&
               it_ != tenant_state_->metadata.end() &&
               it_->second.IsValid();
    }

    // Get metadata (only call when Exists() is true)
    ObjectMetadata& Get() NO_THREAD_SAFETY_ANALYSIS { return it_->second; }

    // Delete current metadata (for PutRevoke or Remove operations)
    void Erase() NO_THREAD_SAFETY_ANALYSIS;
};
```

访问器在构造时自动获取锁和清理无效副本，析构时自动释放锁，确保异常安全和资源管理。

#### 组元数据管理

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_service.h#L1144-L1159
struct TenantState {
    std::unordered_map<std::string, ObjectMetadata> metadata;
    std::unordered_set<std::string> processing_keys;
    std::unordered_map<std::string, const ReplicationTask> replication_tasks;
    std::unordered_map<std::string, PromotionTask> promotion_tasks;
    
    std::unordered_map<std::string, std::unordered_set<std::string>>
        group_members;  // group_id → set of keys
};
```

组元数据维护 `group_id → keys` 的映射，支持相关对象的统一生命周期管理。

### 5. 练习题

#### 练习 1：并发控制
假设两个线程同时对同一个 key 调用 PutStart，元数据分片的读写锁如何防止竞争条件？

#### 练习 2：租约刷新
ExistKey 操作会刷新租约。如果一个对象被频繁访问，它的租约会一直刷新，这可能导致什么问题？如何解决？

#### 练习 3：内存开销
假设有 100 万个对象，每个 ObjectMetadata 约 1 KB，计算总内存开销。如果每个对象平均有 3 个副本，开销会增加多少？

#### 练习 4：组管理
假设一个 KV cache entry 被拆分为 K tensor 和 V tensor 两个对象，使用相同的 group_id。分析在驱逐、租约刷新等操作时，组机制如何保证一致性？

### 6. 答案

#### 答案 1
并发控制机制：

**读写锁保护**：
- 第一个线程获取分片写锁
- 第二个线程尝试获取写锁时被阻塞
- 第一个线程完成 PutStart 后释放锁
- 第二个线程获取锁，发现 key 已存在，返回 OBJECT_EXISTS

**元数据访问器保护**：
```cpp
MetadataAccessorRW accessor(this, object_id);
if (accessor.Exists()) {
    return ErrorCode::OBJECT_EXISTS;
}
```
访问器在构造时获取锁，整个操作期间持有锁，确保原子性。

**状态检查**：
- processing_keys 包含正在写入的 key
- PutStart 成功后加入 processing_keys
- PutEnd 后从 processing_keys 移除
- 防止重复 PutStart

#### 答案 2
频繁刷新租约的问题和解决方案：

**问题分析**：
- 对象永远不会被驱逐（租约一直有效）
- 内存泄漏风险（无用对象长期占用内存）
- 违反了 LRU 驱逐策略的假设

**解决方案**：

1. **软 pin 自动过期**：
   ```cpp
   bool IsSoftPinned() const {
       return soft_pin_timeout &&
              std::chrono::system_clock::now() < *soft_pin_timeout;
   }
   ```
   软 pin 有 TTL，超过一定时间未访问自动失效

2. **驱逐策略优化**：
   - 优先驱逐非软 pin 对象
   - 软 pin 对象在内存紧张时也可以被驱逐

3. **硬 pin 限制**：
   - 硬 pin 对象数量有限制（如系统提示词）
   - 只对重要对象使用硬 pin

4. **访问频率跟踪**：
   - 使用 CountMinSketch 估算访问频率
   - 低频热点对象可以降级处理

#### 答案 3
内存开销分析：

**基础元数据开销**：
- 100 万个对象 × 1 KB = 1 GB

**副本开销**：
- 每个 Replica 约 200 bytes（segment_name、offset、status 等）
- 3 个副本 × 200 bytes = 600 bytes
- 100 万个对象 × 600 bytes = 600 MB

**总开销**：约 1.6 GB

**额外开销**：
- 元数据分片开销（1024 个 hashmap 开销）
- 锁开销（每个分片一个读写锁）
- 租户状态映射（如果有多租户）

**优化建议**：
- 使用更紧凑的数据结构（如 flat_map）
- 延迟分配副本列表（按需分配）
- 定期压缩元数据（清理碎片）

#### 答案 4
组机制的一致性保证：

**组路由机制**：
```cpp
size_t getShardIndex(const std::string& tenant_id,
                     const std::string& user_key) const {
    const auto normalized_tenant = NormalizeTenantId(tenant_id);
    if (normalized_tenant == "default") {
        return std::hash<std::string>{}(user_key) % kNumShards;
    }
    size_t seed = std::hash<std::string>{}(normalized_tenant);
    boost::hash_combine(seed, user_key);
    return seed % kNumShards;
}
```

- 有 group_id 的对象：`shard_idx = hash(group_id) % 1024`
- 同一组的对象路由到同一分片，方便批量操作

**租约刷新一致性**：
```cpp
void GrantLeaseForGroup(const TenantState& tenant_state,
                        const std::string& key,
                        const ObjectMetadata& metadata) const;
```
- 刷新任一成员时，刷新整个组的租约
- 避免组内部分成员被驱逐

**驱逐一致性**：
- 驱逐候选对象时，扩展到整个组
- 检查组的所有成员是否可驱逐
- 只有所有成员都可驱逐时才驱逐组

**限制**：
- 组机制是"尽力而为"，不保证强一致性
- Remove 操作仍然针对单个对象
- 高并发下可能出现组内对象状态短暂不一致

---

## 总结

本讲义涵盖了 Mooncake Store 的四个核心最小模块：

1. **Master-Client 架构**：集中式元数据管理 + 分布式存储，Client 同时扮演客户端和存储服务器的双重角色
2. **控制流与数据流分离**：Master 仅负责元数据管理，数据直接在 Client 之间传输
3. **RPC 协议**：定义了完整的 Master-Client 交互协议，包括两阶段 Put、批量操作、正则查询等
4. **元数据管理**：1024 个分片的高并发元数据存储，支持租约、Pin、组等高级特性

这些设计共同构成了 Mooncake Store 的高性能分布式 KV 缓存架构，为 LLM 推理场景提供了低延迟、高带宽的对象存储能力。
