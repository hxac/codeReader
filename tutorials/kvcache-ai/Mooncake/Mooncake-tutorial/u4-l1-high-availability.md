# 高可用架构与容错机制

## 最小模块导航

本文档通过以下最小模块深入剖析 Mooncake Store 的高可用架构：

1. [高可用架构概览](#高可用架构概览) — 整体架构与设计目标
2. [基于 etcd 的领导选举](#基于-etcd-的领导选举) — Master 主备切换机制
3. [故障检测与恢复](#故障检测与恢复) — Client 心跳与 Master 故障检测
4. [OpLog 复制与恢复](#oplog-复制与恢复) — 元数据变更的同步机制
5. [Snapshot 持久化](#snapshot-持久化) — 元数据快照与恢复

---

## 高可用架构概览

### 概念说明

Mooncake Store 作为分布式 KV 缓存系统，其可用性至关重要。系统提供两种部署模式：

- **默认模式**（单 Master）：简化部署，但 Master 是单点故障
- **高可用模式**（HA）：多个 Master 节点组成集群，通过 etcd 进行协调，实现主备自动切换

在 HA 模式下，Master 节点分为两种角色：
- **Primary（主节点）**：处理客户端请求，负责元数据管理、存储空间分配
- **Standby（备节点）**：通过 OpLog 实时复制 Primary 的元数据变更，保持热备状态

**设计目标**：任意数量 Master 和 Client 节点故障，只要至少有一个 Master 和一个 Client 存活，系统仍能正确服务。

### 伪代码

```
HA 架构启动流程:
1. 多个 Master 节点启动，连接 etcd 集群
2. 通过 etcd 进行领导选举，选出 Primary
3. Primary 开始处理客户端请求
4. Standby 启动 OpLog 复制，同步 Primary 的元数据变更
5. Client 定期发送心跳，Master 监控 Client 健康状态

故障场景:
if Primary 故障:
    etcd 检测到 lease 失效
    剩余 Master 节点重新选举
    新 Primary 上线，Standby 重新复制
    Client 自动重连到新 Primary

if Standby 故障:
    Standby 下线，停止 OpLog 复制
    不影响 Primary 服务

if Client 故障:
    Master 检测到心跳超时
    Master 清理该 Client 的 segment 元数据
    其他 Client 不受影响
```

### 原理分析

Mooncake 的 HA 架构基于 **主备复制**（Primary-Standby Replication）模式，核心思想是：

1. **控制面高可用**：多个 Master 节点通过 etcd 选主，确保总有一个 Primary 在线
2. **数据面容错**：数据实际存储在 Client 节点的内存中，Client 故障只影响部分数据
3. **元数据同步**：Standby 通过 OpLog 实时复制 Primary 的元数据变更

**系统可用性保证**：

假设单个 Master 节点可用性为 \(p_{master}\)，单个 Client 节点可用性为 \(p_{client}\)，集群有 \(M\) 个 Master 和 \(C\) 个 Client。

- 系统可用需要：至少 1 个 Master 且 至少 1 Client 存活
- 系统可用性 \(P_{system} = (1 - (1 - p_{master})^M) \times (1 - (1 - p_{client})^C)\)

当 \(M \geq 3, C \geq 3\) 且 \(p_{master} = p_{client} = 0.99\) 时：
\[ P_{system} = (1 - 0.01^3) \times (1 - 0.01^3) \approx 0.999999 \]

### 代码实践

Mooncake Store 的 HA 架构在设计文档中有详细说明：

> [设计文档 - 高可用模式](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/design/mooncake-store.md#L45-L50)
>
> HA 模式通过运行多个 Master 节点（由 etcd 集群协调）增强容错能力。Master 节点使用 etcd 选举领导者，由领导者处理客户端请求。如果当前领导者故障或与网络隔离，剩余 Master 节点自动重新选举，确保持续可用。

Master 服务的主管负责协调 Standby 的生命周期：

> [MasterServiceSupervisor](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/ha/leadership/master_service_supervisor.h) — 监控领导权状态，控制 Standby 启动/停止/提升

### 练习题

1. **基础题**：在 HA 模式下，如果 Primary 节点故障，大约需要多长时间才能选出新的 Primary？（假设 etcd election timeout 为 3s）

2. **进阶题**：Mooncake 为什么选择主备复制而不是多主复制？分析两种架构的优劣。

3. **设计题**：假设集群有 3 个 Master 和 10 个 Client，任意 1 个 Master 和任意 2 个 Client 可能同时故障，计算系统的理论可用性（假设单节点可用性 0.99）。

4. **思考题**：在 HA 模式下，为什么 Standby 节点不处理客户端读请求？这样设计的权衡是什么？

### 答案

1. **答案**：通常需要 2-3 倍的 election timeout，即约 6-9 秒。etcd 需要等待当前 lease 过期才能开始新一轮选举。

2. **答案**：主备复制的优势是实现简单、一致性容易保证；劣势是 Standby 资源闲置、主备切换有延迟。多主复制的优势是资源利用率高、读写可并行；劣势是实现复杂、冲突解决困难。对于 KV 缓存场景，写入相对较少，主备复制是更实用的选择。

3. **答案**：
   - Master 部分可用性：\(1 - (1 - 0.99)^3 = 1 - 0.000001 = 0.999999\)
   - Client 部分可用性：\(1 - C(3,2) \times 0.99^8 \times 0.01^2 - C(3,3) \times 0.99^7 \times 0.01^3 \approx 0.9997\)
   - 系统可用性：\(0.999999 \times 0.9997 \approx 0.9997\)

4. **答案**：Standby 不处理读请求的原因：① 简化一致性保证，避免读写不同步问题；② Standby 的元数据可能滞后于 Primary，读到的数据可能不完整。权衡是牺牲了部分读吞吐量，换取了更强的一致性保证和更简单的系统设计。

---

## 基于 etcd 的领导选举

### 概念说明

在 HA 模式下，多个 Master 节点需要协调选出唯一的 Primary 来处理请求。Mooncake 使用 etcd 的 **分布式选举**机制实现这一目标。

**核心概念**：
- **Leader Lease（领导租约）**：Primary 持有 etcd 的一个 lease，需要定期续约
- **Leader Key（领导键）**：etcd 中存储当前 Primary 信息的键
- **Election（选举）**：通过 etcd 的竞争原语选出新的 Primary
- **View Version（视图版本）**：每次选举产生新的版本号，用于标识任期

当 Primary 故障时，其 lease 过期，Standby 节点通过 etcd 重新选举选出新的 Primary。

### 伪代码

```
// 选举流程（简化版）
function TryAcquireLeadership():
    // 1. 尝试在 etcd 中创建一个带 lease 的 key
    lease = etcd.GrantLease(ttl=10s)
    success = etcd.CompareAndSet(key="/mooncake/leader",
                                 value="master-1:8080",
                                 lease=lease,
                                 old_value="")

    if success:
        return Success(lease_id="12345")  // 成为 Primary
    else:
        return Failed              // 已有其他 Primary

// 续约流程（Primary 定期执行）
function RenewLeadership(lease_id):
    success = etcd.KeepAlive(lease_id)
    if not success:
        return Failed  // lease 已过期，不再是 Primary

// Standby 监听领导权变更
function WaitForViewChange(known_version):
    watcher = etcd.Watch("/mooncake/leader")
    for event in watcher:
        new_version = event.version
        if new_version > known_version:
            return NewView(version=new_version,
                          leader=event.value)
```

### 原理分析

etcd 的分布式选举基于 **Multi-Paxos** 和 **Raft** 共识算法，提供强一致性保证。

**选举正确性保证**：

假设有 \(N\) 个 Master 节点，etcd 集群有 \(M\) 个节点（通常 \(M = 3\) 或 \(5\)）。

1. **安全性**：任意时刻最多有一个 Primary
   - etcd 的 CompareAndSet 是原子操作，保证只有一个节点能成功
   - lease 机制防止脑裂：即使网络分区，旧 Primary 的 lease 会过期

2. **活性**：当 Primary 故障时，最终会选出新 Primary
   - lease TTL 通常为 10 秒，故障后 10 秒内触发选举
   - etcd Raft 保证只要多数节点存活，选举就能成功

** lease 续约**：

Primary 需要在 lease 到期前续约，通常使用 **心跳**机制：

```
续约周期 T_renew < TTL_lease
通常设置：T_renew = TTL_lease / 3
例如：TTL_lease = 10s, T_renew = 3s
```

这样即使有一次心跳失败，仍有两次重试机会。

### 代码实践

Mooncake 定义了 `LeaderCoordinator` 接口抽象领导选举逻辑：

> [LeaderCoordinator 接口](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/ha/leadership/leader_coordinator.h#L21-L43) — 定义了 TryAcquireLeadership、RenewLeadership、WaitForViewChange 等核心方法

etcd 实现通过 `EtcdLeaderCoordinator`：

> [EtcdLeaderCoordinator](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/ha/leadership/backends/etcd/etcd_leader_coordinator.h#L16-L72) — 使用 etcd 的 lease 和 campaign API 实现选举

关键实现细节：

```cpp
// TryAcquireLeadership 返回结果包含 owner_token
// etcd 实现中，owner_token 就是 etcd 的 lease_id
struct AcquireLeadershipResult {
    bool success;
    OwnerToken owner_token;  // 用于后续续约
    ViewVersionId view_version;  // 当前视图版本
};

// 续约时使用 owner_token 标识自己
tl::expected<bool, ErrorCode> RenewLeadership(
    const LeadershipSession& session) {
    // session.owner_token 包含 lease_id
    // 调用 etcd 的 KeepAliveOnce 续约
}
```

领导权丢失的监控：

> [StartLeadershipMonitor](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/ha/leadership/leader_coordinator.h#L38-L40) — 启动一个监控线程，当领导权丢失时触发回调

### 练习题

1. **基础题**：为什么需要 lease 机制？直接使用一个定期更新的 key 不行吗？

2. **进阶题**：在 etcd 选举中，如果网络分区导致旧 Primary 无法访问 etcd，但仍然处理客户端请求，会发生什么？Mooncake 如何防止这种情况？

3. **设计题**：假设 etcd 集群的选举超时配置为 3s，lease TTL 为 10s。计算从 Primary 故障到新 Primary 上线的最坏情况延迟。

4. **思考题**：为什么 Mooncake 选择 etcd 而不是 ZooKeeper 或 Consul 进行选举？对比三种方案的优劣。

### 答案

1. **答案**：lease 机制提供自动过期能力。如果只使用定期更新的 key，当 Primary 崩溃（不是网络故障）时，key 永远不会过期，导致无法选出新 Primary。lease 能在进程崩溃时自动过期，触发重新选举。

2. **答案**：这种情况称为"脑裂"。旧 Primary 可能处理写入请求，但新 Primary 也处理写入，导致数据不一致。Mooncake 通过 lease 机制防止：旧 Primary 的 lease 过期后，其续约会失败，此时应该主动降级为 Standby 或停止服务。

3. **答案**：
   - Primary 故障后，etcd 需要 10s（lease TTL）确认 lease 过期
   - 然后触发选举，需要约 2-3 × election timeout = 6-9s
   - 最坏情况：10 + 9 = 19s
   - 平均情况：10/2（平均过期时间）+ 7.5（平均选举时间）≈ 12.5s

4. **答案**：
   - **etcd**：优势是 Go 实现、简单易用、性能好；劣势是功能相对单一
   - **ZooKeeper**：优势是功能丰富、成熟稳定；劣势是复杂、Java 实现、性能较差
   - **Consul**：优势是功能全面（服务发现+KV+健康检查）；劣势是复杂度高

   Mooncake 选择 etcd 的原因：① 简单够用，只需要 KV 和选举；② 性能好；③ Go 生态与现代云原生栈兼容。

---

## 故障检测与恢复

### 概念说明

分布式系统中需要快速检测节点故障并触发恢复。Mooncake 实现了 **双层故障检测**：

1. **Master ← Client 心跳**：Client 定期向 Master 发送心跳，Master 检测 Client 故障
2. **etcd ← Master 心跳**：Master 通过 etcd lease 续约，etcd 检测 Master 故障

**检测参数**：
- 心跳间隔：Client 每 5s 发送一次心跳
- 超时阈值：Master 连续 15s（3 次心跳）未收到心跳则判定 Client 故障
- lease TTL：Master 的 etcd lease 为 10s，过期后触发重新选举

### 伪代码

```
// Client 端：定期发送心跳
function ClientHeartbeatLoop():
    while running:
        master_client.SendHeartbeat(client_id, mounted_segments)
        sleep(5s)

// Master 端：监控 Client 健康状态
function MonitorClients():
    for each client_id, last_heartbeat_time in clients:
        if now() - last_heartbeat_time > 15s:
            // Client 故障
            MarkClientDead(client_id)
            // 清理该 Client 的所有 segment
            for segment in GetSegmentsByClient(client_id):
                DeallocateSegment(segment)
                DeleteObjectMetadataOnSegment(segment)

// Master 端：处理心跳 RPC
service MasterService {
    rpc Heartbeat(HeartbeatRequest) returns (HeartbeatResponse):
        client_id = request.client_id
        UpdateLastHeartbeat(client_id, now())
        return OK
}

// etcd 端：Master lease 续约
function MasterKeepAlive():
    while running:
        success = etcd.KeepAlive(lease_id)
        if not success:
            // lease 已过期，可能已有新 Primary
            StopServing()
            return LEASE_LOST
        sleep(3s)
```

### 原理分析

故障检测的核心挑战是 **区分节点故障和网络分区**。

**心跳机制**：

假设心跳间隔为 \(T\)，超时阈值为 \(N \times T\)（通常 \(N=3\)。

- **误判率**：网络延迟或抖动导致误判为故障的概率 \(P_{false\_positive}\)
- **检测延迟**：节点实际故障到检测到的期望延迟 \(E[detection\_delay]\)

这两个指标是矛盾的：
\[ E[detection\_delay] \approx T \times N / 2 \]
\[ P_{false\_positive} \propto e^{-N} \]

增加 \(N\) 可以降低误判率，但增加检测延迟。Mooncake 选择 \(N=3\) 是经验值，平衡了两者。

**Client 故障后的恢复流程**：

1. Master 检测到 Client 故障（15s 无心跳）
2. Master 标记该 Client 的所有 segment 为 **不可用**
3. 对于这些 segment 上的对象副本：
   - 如果还有其他副本，对象仍然可用
   - 如果是唯一副本，对象变为不可用（但不会返回错误数据）
4. 当 Client 重新上线时，重新挂载 segment，恢复数据服务

### 代码实践

Master 服务通过心跳线程监控 Client：

> [Master 心跳线程](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/src/master_service.cpp) — 启动后台线程定期检查 Client 健康状态

Client 端也有心跳线程：

> [Client 心跳线程](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_service.h#L837) — `storage_heartbeat_thread_` 定期向 Master 发送心跳

关键逻辑：

```cpp
// Master 处理 Client 故障
void MasterService::MarkClientDead(ClientID client_id) {
    // 1. 更新 Client 状态
    client_states_[client_id] = ClientState::DEAD;

    // 2. 清理该 Client 的所有 segment
    for (auto& segment_name : GetSegmentsByClient(client_id)) {
        allocator_manager_->DeallocateSegment(segment_name);
    }

    // 3. 元数据会在下次查询时自动过滤（GetReplicaList 检查 segment 可用性）
}
```

设计文档说明了故障检测策略：

> [Client 故障处理](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/design/mooncake-store.md#L50) — 领导者通过定期心跳监控所有 Client 节点的健康状况。如果 Client 崩溃或不可达，领导者快速检测故障并采取适当行动。

### 练习题

1. **基础题**：为什么选择 5s 心跳间隔和 15s 超时？这个设置有什么优缺点？

2. **进阶题**：在跨地域部署中，网络延迟可能达到 100-200ms，心跳参数应该如何调整？

3. **设计题**：设计一个自适应心跳机制，根据网络状况动态调整心跳间隔和超时阈值。

4. **思考题**：如果 Client 网络分区（能发送心跳但无法传输数据），会发生什么？Mooncake 如何应对这种情况？

### 答案

1. **答案**：
   - 5s 间隔：平衡了检测速度和网络开销，更频繁会增加带宽和 CPU 消耗
   - 15s 超时（3倍间隔）：允许偶尔的网络抖动，避免误判
   - 缺点：故障检测需要 7.5s（平均），对延迟敏感的场景可能太慢

2. **答案**：跨地域部署应该调整：
   - 心跳间隔：增加到 10s（考虑往返延迟 200ms × 2 = 400ms，留足够余量）
   - 超时阈值：增加到 30s（3倍间隔）
   - 或者使用更智能的检测机制，如基于 RTT 的动态超时

3. **答案**：
   ```
   自适应心跳算法：
   1. 测量最近 N 次心跳的 RTT，计算平均值 μ 和标准差 σ
   2. 设置心跳间隔 T = max(5s, 3 × μ + 2 × σ)
   3. 设置超时阈值 = 3 × T
   4. 如果连续检测到网络状况稳定，逐渐降低 T 到最小值 5s
   ```

4. **答案**：这种情况称为"灰度故障"。Client 能发送心跳，Master 认为其存活，但实际数据传输失败。
   - Mooncake 的应对：数据传输层面有超时和重试机制
   - 客户端 Get 操作会失败，但不会返回脏数据
   - 改进方案：可以在心跳中增加"探测包"，检验数据通路是否正常

---

## OpLog 复制与恢复

### 概念说明

**OpLog（Operation Log）**是 Primary 到 Standby 的元数据变更日志，用于主备同步。每次 Primary 修改元数据（Put、Remove、Upsert 等），都会生成一条 OpLog 记录并持久化到 etcd。Standby 通过监听 etcd 获取这些变更，应用到本地元数据存储。

**核心概念**：
- **Sequence ID**：全局单调递增的操作序号，保证 OpLog 顺序
- **OpLog Entry**：单条操作记录，包含操作类型、对象 key、元数据 payload 等
- **OpLog Store**：OpLog 持久化存储后端，目前支持 etcd 和本地文件系统
- **OpLog Replicator**：监听 OpLog 变更的组件
- **OpLog Applier**：将 OpLog 应用到本地元数据存储的组件

### 伪代码

```
// Primary 端：生成 OpLog
function OnPutEnd(key, metadata):
    entry = OpLogEntry(
        sequence_id = AllocateSequenceId(),
        timestamp = now(),
        op_type = PUT_END,
        object_key = key,
        payload = Serialize(metadata)
    )
    AppendOpLog(entry)  // 异步写入 etcd，不等待响应

// Standby 端：OpLog 复制流程
function OpLogReplicationLoop():
    // 1. 加载 snapshot，获得 baseline
    snapshot = LoadLatestSnapshot()
    Recover(snapshot.sequence_id)

    // 2. 监听 etcd，获取增量 OpLog
    watcher = etcd.Watch("/mooncake/oplog",
                         start_revision = snapshot.sequence_id + 1)

    for event in watcher:
        if event.type == PUT:
            entry = DeserializeOpLogEntry(event.value)
            ApplyOpLogEntry(entry)

// 应用单条 OpLog
function ApplyOpLogEntry(entry):
    // 检查顺序性
    if entry.sequence_id != expected_sequence_id:
        // 缺失中间的 OpLog，加入 pending 队列
        AddToPending(entry)
        return

    // 根据操作类型应用
    switch entry.op_type:
        case PUT_END:
            metadata_store.Put(entry.object_key,
                              DeserializePayload(entry.payload))
        case REMOVE:
            metadata_store.Remove(entry.object_key)
        case PUT_REVOKE:
            metadata_store.Remove(entry.object_key)

    expected_sequence_id += 1
    ProcessPendingEntries()  // 检查是否有 pending 的 OpLog 可以应用
```

### 原理分析

OpLog 复制基于 **State Machine Replication**（状态机复制）原理：

\[ State_{standby} = Apply(OpLog_N, Apply(OpLog_{N-1}, ..., Apply(OpLog_1, State_{empty}))) \]

只要所有节点按相同顺序应用相同的 OpLog，最终状态会一致。

**顺序性保证**：

使用全局 sequence_id 保证顺序：
\[ sequence_id: \mathbb{N} \to \mathbb{N}, \quad sequence_id(i+1) > sequence_id(i) \]

OpLog 应用必须满足：
\[ \forall i, \forall j < i: Apply(OpLog_j) \text{ 必须在 } Apply(OpLog_i) \text{ 之前完成} \]

**缺失处理**：

当 Standby 检测到 sequence_id 不连续时（例如期望 10，但收到 12）：
1. 将 OpLog 12 放入 pending 队列
2. 启动一个请求任务，从 etcd 获取 OpLog 10 和 11
3. 如果 1 秒内收到，按顺序应用
4. 如果 3 秒后仍未收到，跳过 10 和 11，直接应用 12

**复制延迟**：

定义复制延迟 \(L\) 为 Primary 生成 OpLog 到 Standby 应用的延迟：
\[ L = t_{persist} + t_{propagate} + t_{apply} \]

- \(t_{persist}\)：写入 etcd 的延迟（通常 5-10ms）
- \(t_{propagate}\)：etcd 通知 Standby 的延迟（通常 1-5ms）
- \(t_{apply}\)：Standby 应用元数据的延迟（通常 <1ms）

典型总延迟：10-20ms。

### 代码实践

OpLogManager 管理 OpLog 的生成和存储：

> [OpLogManager](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/ha/oplog/oplog_manager.h#L55-L152) — 提供 Append、AllocateEntry、PersistEntry 等方法

OpLog 复制流程由 Replicator 和 Applier 协作完成：

> [OpLogReplicator](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/ha/oplog/oplog_replicator.h#L29-L85) — 监听 OpLog 变更，调用 Applier 应用

> [OpLogApplier](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/ha/oplog/oplog_applier.h#L26-L163) — 应用 OpLog 到本地元数据，保证顺序性

关键实现细节：

```cpp
// Applier 的顺序性检查
bool OpLogApplier::CheckSequenceOrder(const OpLogEntry& entry) {
    if (entry.sequence_id < expected_sequence_id_) {
        // 重复或乱序，拒绝
        return false;
    }
    if (entry.sequence_id == expected_sequence_id_) {
        // 正常顺序，可以应用
        return true;
    }
    // sequence_id > expected，存在 gap
    // 加入 pending 队列，等待前面的 OpLog
    AddToPending(entry);
    return false;
}

// PUT_END 使用异步持久化（不等待 etcd 响应）
uint64_t OpLogManager::Append(OpType type, const std::string& key,
                               const std::string& payload) {
    OpLogEntry entry = AllocateEntry(type, key, payload);
    // 异步写入 etcd，不阻塞主流程
    oplog_store_->AsyncPersist(entry);
    return entry.sequence_id;
}

// REMOVE 使用同步持久化（必须等待 etcd 响应）
tl::expected<uint64_t, ErrorCode> OpLogManager::AppendAndPersist(
    OpType type, const std::string& key, const std::string& payload) {
    OpLogEntry entry = AllocateEntry(type, key, payload);
    auto result = oplog_store_->Persist(entry);
    if (!result) {
        return tl::make_unexpected(result.error());
    }
    return entry.sequence_id;
}
```

为什么 REMOVE 需要同步持久化？因为如果 REMOVE 没有被 Standby 收到，Standby 提升为 Primary 后可能返回已被删除的脏数据，破坏一致性。

### 练习题

1. **基础题**：为什么 PUT_END 使用异步持久化，而 REMOVE 使用同步持久化？

2. **进阶题**：如果 etcd watch 断连重连，Standby 如何保证不丢失 OpLog？

3. **设计题**：设计一个 OpLog 压缩机制，定期清理旧的 OpLog 以节省 etcd 存储空间。

4. **思考题**：在主备切换过程中，如何确保新 Primary 的元数据与旧 Primary 完全一致？

### 答案

1. **答案**：
   - **PUT_END 异步**：如果 OpLog 丢失，Standby 上缺少这个对象。当 Standby 提升为 Primary 时，客户端会 Get 失败（对象不存在），但不会返回脏数据。这是安全的。
   - **REMOVE 同步**：如果 OpLog 丢失，Standby 上仍然有这个对象。当 Standby 提升为 Primary 时，客户端会成功 Get 到已被删除的数据，这是脏数据，不可接受。

   简单原则：**删除操作必须持久化，创建操作可以异步**。

2. **答案**：etcd watch 支持从指定 revision 开始监听。Standby 重连时：
   - 记录上次收到的 sequence_id 对应的 etcd revision
   - 重连时使用 `WatchFromRevision(last_revision + 1)`
   - 这样可以收到重连期间的所有变更，不会丢失

3. **答案**：
   ```
   OpLog 压缩策略：
   1. 定期（如每小时）创建 snapshot，包含当时的 sequence_id
   2. 对于所有 Standby，记录它们已应用的 sequence_id
   3. 计算 safe_cleanup_seq = min(all_standby_applied_seq)
   4. 删除 sequence_id < safe_cleanup_seq 的所有 OpLog

   实现：
   function CleanupOldOpLogs():
       snapshot_seq = GetLatestSnapshot().sequence_id
       min_applied_seq = GetMinAppliedSequenceIdAmongAllStandbys()
       safe_cleanup_seq = min(snapshot_seq, min_applied_seq)
       DeleteOpLogsBefore(safe_cleanup_seq)
   ```

4. **答案**：主备切换时需要确保新 Primary 的元数据是最新的：
   - 方案 1：**严格等待**。Standby 提升前等待 OpLog 完全同步（延迟高）
   - 方案 2：**OpLog 补偿**。新 Primary 上线后，先从 etcd 读取最新的 snapshot，再重放增量 OpLog（推荐）
   - 方案 3：**租约机制**。旧 Primary 的 lease 过期后，再允许新 Primary 上线，确保足够的时间窗口让 OpLog 传播

   Mooncake 使用方案 2，在 [StandbyController](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/ha/standby_controller.h#L25) 中实现 PromoteStandby()。

---

## Snapshot 持久化

### 概念说明

虽然 OpLog 提供了增量同步机制，但如果 Standby 长时间离线后重新上线，重放大量 OpLog 会很慢。**Snapshot（快照）**是元数据的完整备份，可以快速恢复到某个时间点。

**核心概念**：
- **Snapshot ID**：快照的唯一标识，格式为 `YYYYMMDD_HHMMSS`（如 `20250115_083000`）
- **Sequence ID**：快照对应的 OpLog sequence_id，表示快照包含此 ID 之前的所有变更
- **Snapshot Catalog**：快照的索引目录，存储快照元数据（ID、sequence_id、创建时间等）
- **Snapshot Object Store**：快照数据存储后端，支持本地文件系统和 S3

**快照的作用**：
1. **快速恢复**：新 Standby 或长时间离线的 Standby 可以从快照快速恢复
2. **灾难恢复**：Master 崩溃重启后可以从快照恢复元数据
3. **长期归档**：保存元数据历史版本，用于审计或回滚

### 伪代码

```
// Primary 端：定期创建快照
function SnapshotLoop():
    while running:
        sleep(interval=1hour)

        // 1. Fork 进程，创建一致性快照
        pid = fork()
        if pid == 0:  // 子进程
            // 2. 序列化元数据到二进制格式
            snapshot_data = SerializeMetadata(metadata_store)

            // 3. 上传到 Object Store
            snapshot_id = GenerateSnapshotId()  // 当前时间戳
            object_store.Upload(
                key = f"{snapshot_root}/{snapshot_id}/metadata.bin",
                data = snapshot_data
            )

            // 4. 更新 Catalog
            descriptor = SnapshotDescriptor(
                snapshot_id = snapshot_id,
                sequence_id = GetCurrentSequenceId(),
                timestamp = now()
            )
            catalog_store.Publish(descriptor)

            exit(0)

// Standby 端：加载快照
function LoadSnapshotAndRecover():
    // 1. 从 Catalog 获取最新快照
    latest = catalog_store.GetLatest()
    if latest is None:
        return NoSnapshot

    // 2. 从 Object Store 下载快照数据
    snapshot_data = object_store.Download(
        key = f"{snapshot_root}/{latest.snapshot_id}/metadata.bin"
    )

    // 3. 反序列化元数据
    metadata = DeserializeMetadata(snapshot_data)

    // 4. 恢复到本地 MetadataStore
    metadata_store.Restore(metadata)

    // 5. 设置 OpLogManager 的初始 sequence_id
    oplog_manager.SetInitialSequenceId(latest.sequence_id)

    // 6. 重放增量 OpLog
    StartOpLogReplicationFrom(latest.sequence_id + 1)
```

### 原理分析

**一致性快照**：

使用 **fork + copy-on-write** 机制创建一致性快照，不阻塞 Master 服务：

1. **Fork 时刻**：子进程获得父进程内存的完整副本（通过操作系统的 COW 机制）
2. **序列化**：子进程序列化元数据，父进程继续服务
3. **上传**：子进程上传快照，不影响父进程

假设元数据大小为 \(S\)，序列化带宽为 \(B\)，上传带宽为 \(U\)：

快照创建时间 \(T_{create} = T_{fork} + \frac{S}{B} + \frac{S}{U}\)

- \(T_{fork}\)：fork 系统调用时间（通常 < 100ms）
- \(S/B\)：序列化时间（假设 1GB 数据，100MB/s 序列化 → 10s）
- \(S/U\)：上传时间（假设 1GB 数据，50MB/s 上传 → 20s）

总计约 30s，期间 Master 不受影响。

**快照恢复时间**：

恢复时间 \(T_{restore} = \frac{S}{D} + \frac{S}{B}\)，其中 \(D\) 是下载带宽。

假设 1GB 快照，100MB/s 下载 → 10s 下载 + 10s 反序列化 = 20s。

对比 OpLog 重放：假设每小时产生 10000 条 OpLog，Standby 离线 24 小时，需要重放 240000 条 OpLog，每条 1ms → 240s。

**快照频率权衡**：

- 频繁快照（如每 5 分钟）：恢复快，但存储和 CPU 开销大
- 稀疏快照（如每天一次）：恢复慢，需要重放大量 OpLog

Mooncake 默认每小时一次快照，平衡了恢复速度和资源开销。

### 代码实践

SnapshotProvider 定义了快照加载接口：

> [SnapshotProvider](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/ha/snapshot/snapshot_provider.h#L34-L44) — LoadLatestSnapshot 方法加载最新快照

SnapshotCatalogStore 管理快照索引：

> [SnapshotCatalogStore](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/ha/snapshot/catalog/snapshot_catalog_store.h#L145-L161) — 提供 Publish、GetLatest、List、Delete 方法

SnapshotObjectStore 存储快照数据：

> [SnapshotObjectStore](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/ha/snapshot/object/snapshot_object_store.h#L62-L141) — 支持 UploadBuffer、DownloadBuffer、DeleteObjectsWithPrefix

设计文档说明了快照机制：

> [Snapshot & Restore](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/design/mooncake-store.md#L78-L92) — 使用 fork 创建一致性快照，子进程序列化并上传，父进程不阻塞

关键实现细节：

```cpp
// 快照描述符，包含关键元信息
struct SnapshotDescriptor {
    SnapshotId snapshot_id;              // 快照 ID
    std::string manifest_key;           // manifest 文件路径
    std::string object_prefix;           // 数据文件前缀
    uint64_t last_included_seq;         // 包含的最后 sequence_id
    uint64_t producer_view_version;      // 创建快照时的视图版本
    uint64_t created_at_ms;             // 创建时间戳
};

// 快照根路径生成（支持多集群）
inline std::string BuildSnapshotRoot(const std::string& cluster_id) {
    std::string root("mooncake_master_snapshot/");
    if (!cluster_id.empty()) {
        root += cluster_id + "/";
    }
    return root;
}
```

### 练习题

1. **基础题**：为什么使用 fork 而不是直接在主进程中序列化快照？

2. **进阶题**：如果快照上传过程中 Master 崩溃，会导致快照不完整吗？如何处理？

3. **设计题**：设计一个快照保留策略，自动清理旧快照但保留最近 N 个和每天一个、每周一个、每月一个。

4. **思考题**：在多云部署中，快照应该存储在哪个区域？如何设计跨区域快照复制策略？

### 答案

1. **答案**：使用 fork 的原因：
   - **不阻塞服务**：父进程继续处理客户端请求，子进程在后台序列化
   - **一致性**：fork 时刻获得父进程内存的完整快照，序列化期间父进程的修改通过 COW 机制隔离，不影响快照一致性
   - **简单**：不需要实现复杂的拷贝逻辑，操作系统提供 COW 支持

   直接在主进程序列化会阻塞请求，降低服务可用性。

2. **答案**：不会导致不一致，因为：
   - 快照使用 **原子 publish** 机制：只有在上传完成后才更新 Catalog
   - 如果上传中断，Catalog 中不会包含这个快照，加载时会被忽略
   - Object Store 可能留下部分数据，但会被后续的清理任务删除

   改进方案：使用 **临时路径 + 重命名**，上传到临时路径，完成后再重命名为正式路径。

3. **答案**：
   ```
   快照保留策略：
   function RetentionPolicy():
       snapshots = ListAllSnapshots()
       now = GetCurrentTime()

       // 必须保留
       must_keep = []

       // 1. 保留最近 N 个（如最近 7 个）
       must_keep += snapshots[:7]

       // 2. 保留最近 7 天每天一个
       for i in range(7):
           date = now - i days
           snapshot = FindSnapshotByDate(date)
           if snapshot:
               must_keep += [snapshot]

       // 3. 保留最近 4 周每周一个
       for i in range(4):
           week = now - i weeks
           snapshot = FindSnapshotByWeek(week)
           if snapshot:
               must_keep += [snapshot]

       // 4. 保留最近 12 个月每月一个
       for i in range(12):
               month = now - i months
               snapshot = FindSnapshotByMonth(month)
               if snapshot:
                   must_keep += [snapshot]

       // 删除不在 must_keep 中的快照
       for snapshot in snapshots:
           if snapshot not in must_keep:
               DeleteSnapshot(snapshot)
   ```

4. **答案**：多云快照策略：
   - **本地快照**：每个区域至少保留一个快照，用于本地快速恢复
   - **跨区域复制**：定期（如每天）将快照复制到备份区域，防止区域级灾难
   - **分层存储**：
     - 热数据（最近 7 天）：存储在所有区域
     - 温数据（最近 30 天）：存储在主区域 + 一个备份区域
     - 冷数据（更早）：只在归档区域（如 S3 Glacier）

   成本权衡：跨区域复制增加存储和传输成本，但提供更好的灾难恢复能力。

---

## 总结

本文档深入剖析了 Mooncake Store 的高可用架构与容错机制，涵盖以下五个核心模块：

1. **高可用架构**：基于 etcd 的主备复制模式，任意节点故障系统仍可正确服务
2. **领导选举**：利用 etcd 的 lease 和选举机制实现 Master 主备自动切换
3. **故障检测**：双层心跳机制（Master-Client、Master-etcd）快速检测节点故障
4. **OpLog 复制**：通过全局 sequence_id 保证元数据变更的有序同步
5. **Snapshot 持久化**：定期创建一致性快照，支持快速恢复和灾难恢复

这些机制共同确保了 Mooncake Store 在分布式环境下的高可用性和数据一致性，是构建可靠 LLM 推理缓存系统的基础。
