# TENT 架构与动态传输选择

本讲义深入讲解 TENT（Transfer Engine NEXT）的架构设计，重点关注其如何在异构集群中通过动态传输选择、细粒度调度与运行时故障处理来实现高效可靠的数据移动。

## 1. TENT 运行时架构

### 1.1 概念说明

TENT 运行时是 Mooncake 传输引擎的下一代核心，旨在解决异构 AI 集群中的点对点数据移动问题。在传统部署中，传输引擎假设进程绑定单一传输后端（如 RDMA 或 NVLink），这种模型在异构环境中面临两大挑战：

1. **静态后端选择的局限性**：无法适应动态变化的拓扑连接
2. **静态多路径 striping 的缺陷**：慢速链路会拖累整体传输延迟

TENT 通过将传输选择、调度和故障处理决策移入运行时来解决这些问题，使应用程序无需管理传输细节即可高效移动数据。

### 1.2 伪代码与流程

```
┌──────────────────────────────────────────────────────┐
│                   Application                         │
│         submitTransfer(request_list)                  │
└────────────────────┬─────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────────┐
│            TransferEngineImpl                         │
│  ┌────────────────────────────────────────────────┐  │
│  │  1. 分类请求：按传输类型分组                     │  │
│  │  2. 请求合并：识别可合并的相邻请求               │  │
│  │  3. 传输解析：resolveTransport() 选择传输        │  │
│  │  4. 任务提交：分发到各传输后端的 SubBatch        │  │
│  └────────────────────────────────────────────────┘  │
└────────────────────┬─────────────────────────────────┘
                     │
         ┌───────────┼───────────┐
         ▼           ▼           ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ RdmaTransport│ │ ShmTransport │ │ TcpTransport │
│   + workers  │ │   + workers  │ │   + workers  │
└──────────────┘ └──────────────┘ └──────────────┘
```

### 1.3 原理分析

TENT 运行时采用分层设计：

**声明式 API 层**：应用程序提交描述"传输什么数据"的请求，而非"如何传输"。请求包含源地址、目标 Segment ID、偏移量和长度。

**Segment 抽象层**：Segment 是数据位置的统一抽象，包含拓扑信息、设备列表和 Buffer 描述符。每个 Segment 可通过多种传输后端访问，运行时根据请求属性选择最优传输。

**传输选择层**：TransportSelector 根据配置策略和请求上下文（Segment 类型、内存类型、优先级、传输大小）选择传输后端和设备掩码。

**传输后端层**：各传输后端（RDMA、NVLink、TCP 等）实现统一 Transport 接口，负责实际数据移动。RDMA 后端进一步细分为：
- **Context**：保护域（PD）和完成队列（CQ）管理
- **Endpoint**：QP（队列对）管理和连接维护
- **Workers**：工作线程池处理发送和完成轮询
- **RailMonitor**：单条链路的故障监控与恢复
- **DeviceSelector**：基于 EWMA 的设备选择与负载均衡

**故障处理层**：在传输路径内部处理故障，对应用程序透明。包括跨传输故障转移（RDMA → TCP）和 RDMA 内部 rail 恢复。

### 1.4 代码实践

**TaskInfo 结构体**（存储每个传输任务的状态信息）：
https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/tent/include/tent/runtime/transfer_engine_impl.h#L51-L63

```cpp
struct TaskInfo {
    TransportType type{UNSPEC};          // 当前使用的传输类型
    int sub_task_id{-1};                  // 在 SubBatch 中的索引
    bool derived{false};                  // 是否由其他任务合并而来
    int xport_priority{0};                // 传输优先级（用于故障转移）
    int failover_count{0};                // 故障转移次数
    uint64_t device_mask{~0ULL};         // 设备分配掩码
    Request request;                      // 原始传输请求
    bool staging{false};                  // 是否使用分段传输
    TransferStatusEnum status{PENDING};  // 任务状态
    // ... 其他字段
};
```

**resolveTransport 方法**（解析传输类型和设备掩码）：
https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1207-L1216

```cpp
SelectionResult TransferEngineImpl::resolveTransport(const Request& req,
                                                     int transport_index,
                                                     bool invalidate_on_fail) {
    // 首次尝试解析传输类型
    auto result = getTransportType(req, transport_index);
    
    // 如果解析失败且启用了失效重试，则使远程 Segment 缓存失效后重试
    if (result.transport == UNSPEC && invalidate_on_fail) {
        metadata_->segmentManager().invalidateRemote(req.target_id);
        result = getTransportType(req, transport_index);
    }
    return result;
}
```

**submitTransfer 方法**（提交传输请求）：
https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1218-L1267

```cpp
Status TransferEngineImpl::submitTransfer(
    BatchID batch_id, const std::vector<Request>& request_list) {
    // ... 前置检查和初始化

    // 1. 按传输类型分类请求
    std::vector<Request> classified_request_list[kSupportedTransportTypes];
    std::vector<size_t> task_id_list[kSupportedTransportTypes];

    // 2. 请求合并（如果启用）
    auto merge_boundaries = merge_requests_ ?
        resolveRequestBoundaries(metadata_.get(), request_list) :
        std::vector<RequestBoundaryInfo>{};
    auto merged = mergeRequests(request_list, merge_boundaries, merge_requests_);

    // 3. 为每个请求解析传输类型
    for (auto& kv : merged.task_lookup) {
        size_t task_id = start_task_id + kv.first;
        auto& task = batch->task_list[task_id];
        
        // 初始化任务状态
        task.failover_count = 0;
        task.xport_priority = 0;
        task.status = PENDING;
        
        // 解析传输类型和设备掩码
        auto select_result = resolveTransport(merged_request, 0);
        task.type = select_result.transport;
        task.device_mask = select_result.device_mask;
        
        // ... 将任务提交到对应传输后端的 SubBatch
    }
    
    // 4. 分发到各传输后端
    for (size_t type = 0; type < kSupportedTransportTypes; ++type) {
        if (classified_request_list[type].empty()) continue;
        auto& transport = transport_list_[type];
        transport->submitTransferTasks(sub_batch, request_list);
    }
    
    return Status::OK();
}
```

### 1.5 练习题

1. **概念理解**：为什么 TENT 要将传输选择从应用层移到运行时？这种设计带来什么权衡？

2. **架构分析**：Segment 抽象在 TENT 架构中扮演什么角色？它如何支持多传输后端？

3. **状态追踪**：TaskInfo 结构体中 `xport_priority` 和 `failover_count` 字段的作用是什么？

4. **故障处理**：TENT 在哪个层级处理故障？为什么这样设计？

### 1.6 答案

**答 1**：将传输选择移到运行时的原因是：
- **适应性**：运行时可以根据当前网络条件动态选择最优传输路径
- **简化应用**：应用程序无需感知底层硬件差异和传输细节
- **集中优化**：调度和策略决策可以集中优化，而非分散在各应用中

权衡：
- **控制权**：应用程序放弃了对传输选择的细粒度控制
- **复杂性**：运行时内部逻辑更复杂，需要维护更多状态
- **开销**：动态选择会增加一定的运行时开销

**答 2**：Segment 抽象的角色：
- **位置统一描述**：封装了数据所在位置（内存/文件）、拓扑信息、设备列表
- **多传输支持**：每个 Segment 包含多种传输后端的访问信息（BufferDesc 包含 transports 列表）
- **透明访问**：应用程序通过 Segment ID 访问数据，无需关心底层使用哪个传输

**答 3**：
- `xport_priority`：传输优先级索引，指向传输候选列表中的位置。故障转移时递增，切换到下一个候选传输。
- `failover_count`：记录故障转移次数，用于限制重试次数，防止无限重试。

**答 4**：TENT 在数据路径内部（传输层）处理故障，而不是将故障暴露给应用层。原因：
- **透明性**：应用程序看到的是"传输最终成功"，无需处理故障
- **效率**：在数据路径内部处理可以更快恢复，无需跨层通信
- **统一策略**：所有应用共享同一套故障处理策略，避免重复实现

## 2. 动态传输选择机制

### 2.1 概念说明

动态传输选择是 TENT 的核心特性之一，它根据配置策略和请求上下文自动选择最优传输后端。与静态绑定单一后端不同，TENT 允许：

- **按需选择**：每个请求独立选择传输，可以根据优先级、数据大小、内存类型等因素差异化处理
- **多传输候选**：配置中指定多个候选传输（如 `[rdma, tcp]`），失败时自动故障转移到下一个
- **设备约束**：通过设备掩码限制可以使用哪些网卡，实现资源隔离和负载均衡

### 2.2 伪代码与流程

```
┌─────────────────────────────────────────────────────┐
│         TransportSelector::select()                  │
└──────────────────┬──────────────────────────────────┘
                   │
                   ▼
         ┌─────────────────────┐
         │  1. 遍历配置策略     │
         │  (JSON 顺序优先)     │
         └──────────┬──────────┘
                    │
                    ▼
         ┌─────────────────────┐
         │  2. 匹配策略规则：   │
         │   - segment_type     │
         │   - same_machine     │
         │   - memory_type     │
         │   - priority        │
         │   - size            │
         └──────────┬──────────┘
                    │
                    ▼
         ┌─────────────────────┐
         │  3. 转换设备列表     │
         │     为 device_mask   │
         └──────────┬──────────┘
                    │
                    ▼
         ┌─────────────────────┐
         │  4. 选择传输类型    │
         │  transports[index]  │
         └──────────┬──────────┘
                    │
                    ▼
         ┌─────────────────────┐
         │  5. 检查传输可用性  │
         │  (capabilities &    │
         │   context)          │
         └──────────┬──────────┘
                    │
                    ▼
         ┌─────────────────────┐
         │  返回 SelectionResult│
         │  {transport,         │
         │   device_mask}       │
         └─────────────────────┘
```

### 2.3 原理分析

**策略匹配机制**：TransportSelector 在配置中加载策略列表，每个策略包含匹配条件和传输候选。选择时按 JSON 配置顺序遍历，第一个匹配的策略胜出（first-match-wins）。

**匹配维度**：
- `segment_type`：Memory 或 File
- `same_machine`：本机或远程（可选过滤）
- `local_memory` / `remote_memory`：内存类型模式（cpu, cuda, npu, *）
- `priority`：请求优先级（high=0, medium=1, low=2）
- `min_size` / `max_size`：传输大小范围
- `devices`：允许的设备名称列表
- `transports`：传输候选列表

**设备掩码计算**：策略中的 `devices` 字段（如 `["mlx5_0", "mlx5_1"]`）会被转换为 64 位掩码：
- `devices: ["mlx5_0"]` → `device_mask = 0x0001`（第 0 位）
- `devices: ["mlx5_1", "mlx5_2"]` → `device_mask = 0x0006`（第 1、2 位）
- `devices: []` → `device_mask = ~0ULL`（所有设备）

**传输可用性检查**：即使策略匹配，还需要检查传输的实际可用性：
- 传输后端是否存在（`available_transports[type] != nullptr`）
- NVLink/SHM 仅限本机
- 传输能力是否匹配内存类型组合（如 `gpu_to_gpu`, `dram_to_dram`）

**transport_hint 机制**：`Request::transport_hint` 允许单次请求绕过策略选择：
- `UNSPEC`（默认）：按策略正常选择
- 指定传输：首次尝试使用该传输，故障转移时排除该传输

### 2.4 代码实践

**SelectionContext 结构体**（传输选择的上下文信息）：
https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/tent/include/tent/runtime/transport_selector.h#L74-L85

```cpp
struct SelectionContext {
    SegmentType segment_type;       // File 或 Memory
    bool same_machine;              // 本机或远程
    MemoryType local_memory_type;   // CPU, CUDA, ROCm 等
    MemoryType remote_memory_type;  // 远程内存类型
    const std::vector<TransportType>* buffer_transports;  // Buffer 注册的传输
    size_t transfer_size;           // 传输大小（字节）
    int priority_level;             // 优先级（0=high, 1=medium, 2=low）
    std::optional<std::string> policy_name;  // 可选：绑定特定策略
};
```

**SelectionPolicy 结构体**（传输选择策略规则）：
https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/tent/include/tent/runtime/transport_selector.h#L89-L120

```cpp
struct SelectionPolicy {
    std::string name;                      // 策略名称
    
    SegmentType segment_type;             // Memory 或 File
    std::optional<bool> same_machine;     // 本机过滤（可选）
    std::optional<std::string> local_memory_pattern;    // 本地内存模式
    std::optional<std::string> remote_memory_pattern;   // 远程内存模式
    
    std::optional<uint64_t> min_size;      // 最小传输大小
    std::optional<uint64_t> max_size;      // 最大传输大小
    
    std::optional<int> priority;          // 优先级过滤（精确匹配）
    
    std::vector<std::string> devices;     // 允许的设备名称列表
    std::vector<TransportType> transports; // 传输候选列表（按优先级）
};
```

**select 方法**（执行传输选择）：
https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp#L362-L442

```cpp
SelectionResult TransportSelector::select(
    const SelectionContext& context,
    const std::array<std::shared_ptr<Transport>, kSupportedTransportTypes>&
        available_transports,
    int transport_index, TransportType hint) {
    
    SelectionResult result;
    
    // 1. 查找匹配的策略（JSON 顺序优先）
    const SelectionPolicy* matching_policy = nullptr;
    for (const auto& policy : policies_) {
        if (matchesPolicy(policy, context)) {
            matching_policy = &policy;
            break;  // 首个匹配胜出
        }
    }
    
    if (!matching_policy) {
        LOG(WARNING) << "No matching transport policy";
        return result;  // 返回 UNSPEC
    }
    
    // 2. 转换设备列表为设备掩码
    result.device_mask = ~0ULL;  // 默认所有设备
    if (!matching_policy->devices.empty() && topology_) {
        result.device_mask = 0;
        for (const auto& name : matching_policy->devices) {
            int dev_id = topology_->getNicId(name);
            if (dev_id >= 0 && dev_id < 64) {
                result.device_mask |= (1ULL << dev_id);
            }
        }
    }
    
    // 3. 获取传输候选列表（策略 transports 或 buffer_transports）
    const auto& raw = !matching_policy->transports.empty() ?
                          matching_policy->transports :
                      (context.buffer_transports ? *context.buffer_transports :
                                                   std::vector<TransportType>{});
    
    // 4. 应用 transport_hint 重排序（hint 排第一）
    auto candidates = reorderWithHint(raw, hint);
    if (!candidates) return result;  // hint 不在候选列表中
    
    // 5. 按 transport_index 选择传输
    for (size_t i = 0; i < candidates->size(); ++i) {
        TransportType type = (*candidates)[i];
        
        // 检查传输可用性
        if (!isTransportAvailable(type, context, available_transports)) {
            // 如果 hint 不可用，拒绝请求
            if (hint != UNSPEC && i == 0) return result;
            continue;
        }
        
        if (transport_index == 0) {
            result.transport = type;
            break;
        }
        --transport_index;
    }
    
    return result;
}
```

**matchesPolicy 方法**（检查策略是否匹配上下文）：
https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp#L247-L304

```cpp
bool TransportSelector::matchesPolicy(const SelectionPolicy& policy,
                                      const SelectionContext& context) const {
    // 如果指定了策略名称，精确匹配
    if (context.policy_name.has_value()) {
        return context.policy_name.value() == policy.name &&
               policy.segment_type == context.segment_type;
    }
    
    // 检查 segment_type
    if (policy.segment_type != context.segment_type) return false;
    
    // 检查 same_machine 约束
    if (policy.same_machine.has_value()) {
        if (policy.same_machine.value() != context.same_machine) return false;
    }
    
    // 检查内存类型模式
    if (policy.local_memory_pattern.has_value()) {
        if (!matchesMemoryPattern(policy.local_memory_pattern.value(),
                                  context.local_memory_type)) return false;
    }
    if (policy.remote_memory_pattern.has_value()) {
        if (!matchesMemoryPattern(policy.remote_memory_pattern.value(),
                                  context.remote_memory_type)) return false;
    }
    
    // 检查大小约束
    if (policy.min_size.has_value()) {
        if (context.transfer_size < policy.min_size.value()) return false;
    }
    if (policy.max_size.has_value()) {
        if (context.transfer_size > policy.max_size.value()) return false;
    }
    
    // 检查优先级（精确匹配）
    if (policy.priority.has_value()) {
        if (context.priority_level != policy.priority.value()) return false;
    }
    
    return true;
}
```

### 2.5 练习题

1. **策略设计**：设计一个传输选择策略，使高优先级请求优先使用 NVLink，低优先级请求使用 RDMA。

2. **设备掩码**：假设有 4 个 RDMA 设备（mlx5_0 到 mlx5_3），如何计算只使用 mlx5_1 和 mlx5_3 的 device_mask？

3. **transport_hint**：在什么场景下需要使用 `transport_hint`？它与策略选择的关系是什么？

4. **可用性检查**：为什么需要 `isTransportAvailable` 检查？策略匹配是否足够？

### 2.6 答案

**答 1**：配置示例：
```json
{
  "policy": [
    {
      "name": "high_priority_nvlink",
      "segment_type": "memory",
      "priority": "high",
      "same_machine": true,
      "transports": ["nvlink", "rdma"]
    },
    {
      "name": "low_priority_rdma",
      "segment_type": "memory",
      "priority": "low",
      "transports": ["rdma", "tcp"]
    }
  ]
}
```

**答 2**：
- mlx5_1 → device_id = 1 → `1ULL << 1 = 0x0002`
- mlx5_3 → device_id = 3 → `1ULL << 3 = 0x0008`
- device_mask = `0x0002 | 0x0008 = 0x000A`

**答 3**：`transport_hint` 用于需要临时绕过策略的场景，如：
- **测试**：强制使用特定传输测试故障转移逻辑
- **调试**：排除某个传输以验证其他传输的行为
- **特殊请求**：某些关键请求需要指定传输

关系：`transport_hint` 坐落在策略配置之上，首次尝试使用指定的传输，故障转移时排除该传输，按策略选择下一个候选。

**答 4**：`isTransportAvailable` 检查是必要的，因为策略匹配仅考虑配置规则，而不考虑实际运行时状态：
- 传输后端可能未初始化或加载失败
- 某些传输仅限特定场景（如 NVLink 仅限本机）
- 传输能力可能不支持当前的内存类型组合（如某些 RDMA 设备不支持 GPU-to-GPU）

## 3. 切片调度与负载均衡

### 3.1 概念说明

在多轨（multi-rail）RDMA 环境中，简单的轮询（round-robin）切片分配会导致性能次优，主要问题包括：

- **NUMA 效应**：跨 NUMA 访问增加延迟并降低有效带宽
- **负载不均**：静态分配无法适应动态负载变化
- **链路质量差异**：不同 rail 的有效带宽可能因拥塞或硬件特性而不同

TENT 通过智能切片调度解决这些问题：
- **NUMA 感知设备选择**：优先选择本地 NUMA 的设备，对远程设备施加惩罚
- **EWMA 带宽估计**：使用指数加权移动平均（EWMA）动态估计每个设备的有效带宽
- **动态多路径分配**：大传输时根据设备容量分配切片

### 3.2 伪代码与流程

```
┌─────────────────────────────────────────────────────┐
│           DeviceSelector::allocate()                 │
└──────────────────┬──────────────────────────────────┘
                   │
                   ▼
         ┌─────────────────────┐
         │  获取候选设备列表    │
         │  (按 NUMA 分层)      │
         └──────────┬──────────┘
                    │
                    ▼
         ┌─────────────────────┐
         │ enable_smart_sched?  │
         └──────┬──────────┬────┘
                │          │
        Yes     │          │    No
        ┌──────▼────┐      ▼
        │ Smart Mode│    Baseline Mode
        │ (EWMA)    │    (Round-Robin)
        └──────┬────┘      │
               │           │
               └────┬──────┘
                    │
                    ▼
         ┌─────────────────────┐
         │  为每个设备评分：    │
         │  score = predicted  │
         │    × penalty[tier]  │
         └──────────┬──────────┘
                    │
                    ▼
         ┌─────────────────────┐
         │  选择最优设备(s)     │
         │  - 单路径：最佳设备   │
         │  - 多路径：加权分配   │
         └──────────┬──────────┘
                    │
                    ▼
         ┌─────────────────────┐
         │  返回 slice_dev_ids  │
         └─────────────────────┘
```

**EWMA 带宽更新**（每次传输完成）：
```
observed_bandwidth = transfer_size / transfer_time
ewma_bandwidth = α × ewma_bandwidth + (1 - α) × observed_bandwidth
ewma_bandwidth = clamp(ewma_bandwidth, 0.1×theoretical, 10.0×theoretical)
```

其中 \(\alpha\) 是学习率（`bandwidth_learning_rate`）：
- **低 \(\alpha\)（接近 0）**：快速适应，新观测值权重高
- **高 \(\alpha\)（接近 1）**：慢速适应，旧值影响大

### 3.3 原理分析

**NUMA 分层**：设备按 NUMA 距离分为三层：
- Rank 0：本地 NUMA（惩罚 1.0）
- Rank 1：远程 NUMA tier 1（惩罚 5.0）
- Rank 2：远程 NUMA tier 2（惩罚 10.0）

惩罚作为预测完成时间的乘数，使远程设备在同等负载下吸引力降低。

**EWMA 带宽估计**：每个设备维护 EWMA 带宽估计，公式为：
\[ \text{ewma}_{t+1} = \alpha \times \text{ewma}_t + (1-\alpha) \times \text{observed}_t \]

特性：
- **记忆性**：近期观测比旧观测影响更大
- **稳定性**：平滑瞬时波动
- **适应性**：跟踪链路质量的渐变

注意：文献中的 EWMA 有两种惯例。TENT 使用 \(\alpha\) 作为**旧值系数**，因此：
- \(\alpha = 0\)：完全适应，`ewma = observed`
- \(\alpha = 1\)：不学习，`ewma` 永不变
- \(\alpha = 0.01\)：默认，逐渐适应

**设备评分**：选择设备时计算预测完成时间：
\[ \text{predicted} = \frac{\text{inflight} + \text{slice}}{\text{ewma_bandwidth}} \]
\[ \text{score} = \text{predicted} \times \text{penalty}_{\text{tier}} \]

选择评分最低的设备。

**多路径分配**：大传输（切片数 >= max_slice_count/2）时：
- **普通模式**（99%）：按设备容量比例分配切片
- **探测模式**（1%，每 100 次）：轮询分配，确保所有设备被采样以更新 EWMA

### 3.4 代码实践

**配置示例**（智能调度参数）：
https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/design/tent/slice-spraying.md#L171-L177

```json
{
  "transports": {
    "rdma": {
      "enable_smart_scheduling": true,
      "numa_penalties": [1.0, 5.0, 10.0],
      "bandwidth_learning_rate": 0.01,
      "ewma_min_bandwidth_multiplier": 0.1,
      "ewma_max_bandwidth_multiplier": 10.0
    }
  }
}
```

**设备选择逻辑**（概念代码，非实际源码位置）：
```cpp
// 伪代码：展示评分和选择逻辑
struct DeviceScore {
    double predicted_time;
    double score;
    int device_id;
};

DeviceScore scoreDevice(DeviceInfo& dev, size_t slice_bytes) {
    // 计算预测完成时间
    double inflight = dev.inflight_bytes;
    double bandwidth = dev.ewma_bandwidth;
    double predicted = (inflight + slice_bytes) / bandwidth;
    
    // 应用 NUMA 惩罚
    int tier = getNumaTier(dev.numa_node);
    double penalty = numa_penalties[tier];
    double score = predicted * penalty;
    
    return {predicted, score, dev.device_id};
}

// 选择最优设备
DeviceSelector::allocate(size_t slice_count, size_t slice_bytes) {
    if (!enable_smart_scheduling) {
        return allocateRoundRobin(slice_count);  // 基线模式
    }
    
    // 智能模式：评分每个设备
    std::vector<DeviceScore> scores;
    for (auto& dev : devices) {
        scores.push_back(scoreDevice(dev, slice_bytes));
    }
    
    // 按评分排序
    std::sort(scores.begin(), scores.end(),
              [](auto& a, auto& b) { return a.score < b.score; });
    
    // 单路径或多路径分配
    if (slice_count < max_slice_count / 2) {
        // 单路径：只使用最佳设备
        return {scores[0].device_id};
    } else {
        // 多路径：按容量加权分配
        return allocateWeighted(scores, slice_count);
    }
}
```

**EWMA 更新**（传输完成时）：
```cpp
// 伪代码：EWMA 更新
void onTransferComplete(int device_id, size_t bytes,
                        std::chrono::microseconds duration) {
    auto& dev = devices[device_id];
    
    // 计算观测带宽
    double observed = bytes / (duration.count() / 1e6);  // bytes/sec
    
    // EWMA 更新
    double alpha = bandwidth_learning_rate;
    dev.ewma_bandwidth = alpha * dev.ewma_bandwidth + (1 - alpha) * observed;
    
    // 限制在合理范围
    double min_bw = theoretical_bandwidth * ewma_min_bandwidth_multiplier;
    double max_bw = theoretical_bandwidth * ewma_max_bandwidth_multiplier;
    dev.ewma_bandwidth = std::clamp(dev.ewma_bandwidth, min_bw, max_bw);
}
```

### 3.5 练习题

1. **EWMA 参数**：假设初始 ewma_bandwidth = 400 Gbps，observed = 200 Gbps，\(\alpha = 0.01\)，计算更新后的 ewma_bandwidth。

2. **NUMA 惩罚**：本地设备预测时间 10 μs，远程设备（惩罚 5.0）预测时间 8 μs，哪个设备会被选择？

3. **多路径分配**：有 3 个设备，容量分别为 [100, 50, 25] 单位，要分配 20 个切片，如何分配？

4. **学习率选择**：什么场景应该使用低学习率（\(\alpha = 0.001\)）？什么场景应该使用高学习率（\(\alpha = 0.1\)）？

### 3.6 答案

**答 1**：
\[
\text{ewma}_{\text{new}} = 0.01 \times 400 + 0.99 \times 200 = 4 + 198 = 202 \text{ Gbps}
\]

**答 2**：
- 本地设备：score = \(10 \times 1.0 = 10\)
- 远程设备：score = \(8 \times 5.0 = 40\)

选择本地设备（评分更低）。

**答 3**：
- 总容量 = \(100 + 50 + 25 = 175\)
- 设备 0：\(\frac{100}{175} \times 20 \approx 11.4\) → 11 切片
- 设备 1：\(\frac{50}{175} \times 20 \approx 5.7\) → 6 切片
- 设备 2：\(\frac{25}{175} \times 20 \approx 2.9\) → 3 切片
- 剩余：\(20 - (11 + 6 + 3) = 0\) 切片

分配：[11, 6, 3]

**答 4**：
- **低学习率（\(\alpha = 0.001\)）**：链路质量稳定、变化缓慢的场景。保持长期平均值，避免短期波动影响决策。
- **高学习率（\(\alpha = 0.1\)）**：链路质量波动剧烈、需要快速适应的场景。快速响应变化，但可能过度反应瞬时波动。

## 4. 故障处理与自动恢复

### 4.1 概念说明

TENT 在数据路径内部处理故障，对应用程序透明。故障处理分为两层：

1. **跨传输故障转移**（TransferEngineImpl 层）：当一个传输后端在完成阶段报告失败时，将任务重新提交到下一个候选传输（如 RDMA → TCP）。
2. **RDMA rail 恢复**（RailMonitor 层）：当特定（本地 NIC, 远程 NIC）链路持续失败时，使用指数退避暂停该链路，成功传输或冷却到期后恢复。

这种设计使应用程序看到的是"传输最终成功"，而非频繁的故障通知。

### 4.2 伪代码与流程

```
┌─────────────────────────────────────────────────────┐
│         跨传输故障转移流程                           │
└──────────────────┬──────────────────────────────────┘
                   │
                   ▼
┌───────────────────────────────────────────────────┐
│  getTransferStatus() 返回 FAILED                  │
└──────────────────┬────────────────────────────────┘
                   │
                   ▼
┌───────────────────────────────────────────────────┐
│  resubmitTransferTask(task_id)                     │
│  ┌───────────────────────────────────────────┐   │
│  │  failover_count++                          │   │
│  │  if failover_count > max_attempts:        │   │
│  │      return FAILED（预算耗尽）             │   │
│  └───────────────────────────────────────────┘   │
│  ┌───────────────────────────────────────────┐   │
│  │  xport_priority++                         │   │
│  │  type = resolveTransport(req, priority)   │   │
│  │  if type == UNSPEC:                       │   │
│  │      return FAILED（无传输可用）           │   │
│  └───────────────────────────────────────────┘   │
│  ┌───────────────────────────────────────────┐   │
│  │  提交到新传输                             │   │
│  │  task.status = PENDING（重试中）         │   │
│  └───────────────────────────────────────────┘   │
└───────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│         RDMA rail 恢复流程                          │
└──────────────────┬──────────────────────────────────┘
                   │
                   ▼
┌───────────────────────────────────────────────────┐
│  完成阶段发现 WC error（工作请求完成错误）         │
└──────────────────┬────────────────────────────────┘
                   │
                   ▼
┌───────────────────────────────────────────────────┐
│  RailMonitor::markFailed(local_nic, remote_nic)  │
│  ┌───────────────────────────────────────────┐   │
│  │  error_count++（在 error_window 内）       │   │
│  │  if error_count >= error_threshold:       │   │
│  │      cooldown *= 2（指数退避）             │   │
│  │      resume_time = now + cooldown          │   │
│  │      rail 暂停                             │   │
│  └───────────────────────────────────────────┘   │
└───────────────────────────────────────────────────┘
                   │
                   ▼
┌───────────────────────────────────────────────────┐
│  后续工作请求调用 available(local, remote)         │
│  ┌───────────────────────────────────────────┐   │
│  │  if paused && now < resume_time:         │   │
│  │      返回 false（调度器选择其他 rail）    │   │
│  │  else if cooldown expired:               │   │
│  │      清除退避状态                         │   │
│  │      返回 true（rail 恢复）               │   │
│  └───────────────────────────────────────────┘   │
└───────────────────────────────────────────────────┘
```

### 4.3 原理分析

**故障模型**：TENT 聚焦于三种瞬态故障：

| 故障类型 | 暴露点 | 恢复动作 |
|---------|--------|---------|
| WC error（完成错误） | RDMA worker 发现错误的完成条目 | rail 级 `markFailed` + 任务级重新提交 |
| QP/endpoint 失败 | `submitTransferTasks` 返回非 OK | *当前不重试*：任务标记为 FAILED |
| 对端断开 | `getTransferStatus` 返回 FAILED | 跨传输故障转移 |

永久性或应用可见的错误（参数无效、内存不足、Segment 不存在）不被重试，直接返回给调用者。

**跨传输故障转移**：
- **入口点**：`resubmitTransferTask` 是唯一入口，将失败任务提升到下一个候选传输
- **调用者**：两个可恢复故障表面调用它
  1. **完成阶段失败**：`getTransferStatus` 发现 FAILED 完成时调用
  2. **预算耗尽**：失败次数超过 `max_failover_attempts` 时设置状态为 InvalidEntry
- **提交阶段失败不重试**：当 `submitTransferTasks` 返回非 OK 时，任务标记为 UNSPEC 并返回 FAILED。原因：
  - **合并请求**：`merge_requests` 启用时，一个逻辑传输对应多个 task_id，重新提交会重复传输
  - **部分入队**：某些传输在错误前已启动部分请求，无法确定哪些成功

**RDMA rail 恢复**：
- **markFailed**：在 `error_window_` 窗口内累计错误计数，达到 `error_threshold_` 时暂停 rail，`cooldown_` 每次失败加倍（上限 300 秒）
- **markRecovered**：清除错误计数和退避状态，如果 rail 处于暂停状态则恢复（快速路径：健康 rail 直接返回）
- **available**：每次工作请求调用。如果冷却到期，自动恢复并清除退避状态

**两个独立的恢复信号**：
1. **冷却到期**：时间到期后自动恢复
2. **即时成功**：成功传输立即恢复，无需等待冷却

这确保故障 rail 不会永久停顿，同时恢复的 rail 在首次成功完成时立即服务。

### 4.4 代码实践

**resubmitTransferTask 方法**（跨传输故障转移）：
https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1386-L1424

```cpp
Status TransferEngineImpl::resubmitTransferTask(Batch* batch, size_t task_id) {
    auto& task = batch->task_list[task_id];
    auto prev_type = task.type;

    // 检查故障转移预算
    if (++task.failover_count > max_failover_attempts_) {
        LOG(WARNING) << "Task failover limit reached ("
                     << max_failover_attempts_
                     << "), last transport=" << transportTypeName(prev_type);
        return Status::InvalidEntry(
            "Failover limit exceeded, all transports exhausted");
    }

    // 清理分段传输状态
    if (task.staging)
        task.staging = false;
    else
        task.xport_priority = task.failover_count;  // 提升优先级索引

    // 解析下一个传输类型
    auto result = resolveTransport(task.request, task.xport_priority);
    auto type = result.transport;
    if (type == UNSPEC) {
        LOG(WARNING) << "No more transports available after "
                     << transportTypeName(prev_type) << " failed";
        return Status::InvalidEntry("All available transports are failed");
    }

    LOG(INFO) << "Transport failover: " << transportTypeName(prev_type)
              << " -> " << transportTypeName(type) << " (attempt "
              << task.failover_count << "/" << max_failover_attempts_ << ")";
    TENT_RECORD_TRANSPORT_FAILOVER();  // 记录指标

    // 提交到新传输
    auto& transport = transport_list_[type];
    if (!batch->sub_batch[type])
        CHECK_STATUS(transport->allocateSubBatch(batch->sub_batch[type],
                                                 batch->max_size));
    auto& sub_batch = batch->sub_batch[type];
    task.sub_task_id = sub_batch->size();
    task.type = type;
    return transport->submitTransferTasks(sub_batch, {task.request});
}
```

**RailMonitor::markFailed 方法**（标记 rail 失败）：
https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/tent/src/transport/rdma/rail_monitor.cpp#L60-L81

```cpp
void RailMonitor::markFailed(int local_nic, int remote_nic) {
    auto it = rail_states_.find(std::make_pair(local_nic, remote_nic));
    if (it == rail_states_.end()) return;
    auto& st = it->second;
    auto now = std::chrono::steady_clock::now();
    
    // 在时间窗口内累计错误计数
    if (st.error_count == 0 || now - st.last_error > error_window_) {
        st.error_count = 1;
    } else {
        st.error_count++;
    }
    st.last_error = now;
    
    // 指数退避：每次失败冷却时间加倍
    if (st.cooldown.count() == 0) {
        st.cooldown = cooldown_;
    } else {
        st.cooldown *= 2;
        if (st.cooldown > kMaxCooldown) st.cooldown = kMaxCooldown;
    }
    
    // 达到阈值，暂停 rail
    if (st.error_count >= error_threshold_) {
        st.resume_time = now + st.cooldown;
        updateBestMapping();
    }
}
```

**RailMonitor::available 方法**（检查 rail 可用性）：
https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/tent/src/transport/rdma/rail_monitor.cpp#L43-L58

```cpp
bool RailMonitor::available(int local_nic, int remote_nic) {
    auto it = rail_states_.find(std::make_pair(local_nic, remote_nic));
    if (it == rail_states_.end()) return false;
    auto& st = it->second;
    
    // 未暂停，直接可用
    if (!st.paused()) return true;
    
    // 冷却未到期，不可用
    if (std::chrono::steady_clock::now() < st.resume_time) return false;
    
    // 冷却到期：清除所有指数退避状态
    st.resume_time = {};
    st.error_count = 0;
    st.cooldown = std::chrono::seconds(0);
    updateBestMapping();
    LOG(INFO) << "Rail recovered: local_nic=" << local_nic
              << " remote_nic=" << remote_nic << " (cooldown expired)";
    return true;
}
```

**updateTaskStatusAfterPoll 方法**（轮询后更新状态并尝试故障转移）：
https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1448-L1460

```cpp
void TransferEngineImpl::updateTaskStatusAfterPoll(Batch* batch, size_t task_id,
                                                   TransferStatus& task_status,
                                                   bool allow_failover) {
    auto& task = batch->task_list[task_id];
    task.status = task_status.s;
    
    // 不需要故障转移，或任务未失败，或传输类型未指定
    if (!allow_failover || task_status.s != FAILED || task.type == UNSPEC)
        return;

    // 尝试故障转移
    if (resubmitTransferTask(batch, task_id).ok()) {
        task_status.s = PENDING;
        task.status = PENDING;  // 标记为待处理，不报告失败
    }
}
```

### 4.5 练习题

1. **故障转移预算**：`max_failover_attempts = 3`，配置了 `[rdma, tcp]` 两个传输，RDMA 失败后会发生什么？如果只有 RDMA 一个传输呢？

2. **指数退避**：初始 cooldown = 30 秒，error_threshold = 3，连续失败 6 次后的 cooldown 是多少？

3. **恢复信号**：rail 的哪两种恢复信号？为什么需要两种？

4. **提交阶段不重试**：为什么 `submitTransferTasks` 失败时不重试？这与完成阶段重试有什么区别？

### 4.6 答案

**答 1**：
- 配置 `[rdma, tcp]`：RDMA 失败 → 切换到 TCP（`xport_priority = 1`）。如果 TCP 也失败 → 返回 FAILED（`failover_count = 2 <= 3` 但无更多传输）。
- 只有 RDMA：RDMA 失败 → 尝试解析下一个传输（`resolveTransport(..., 1)`）→ 返回 UNSPEC → 任务标记 FAILED。

**答 2**：
- 第 1-2 次失败：累计错误，未达到阈值
- 第 3 次失败：`cooldown = 30` 秒，暂停 rail
- 第 4 次失败：`cooldown = 60` 秒
- 第 5 次失败：`cooldown = 120` 秒
- 第 6 次失败：`cooldown = 240` 秒

最终 cooldown = 240 秒（未超过上限 300 秒）。

**答 3**：
1. **冷却到期**：时间到期后自动恢复，防止故障 rail 永久停顿
2. **即时成功**：成功传输立即恢复，使 rail 在首次成功时立即服务（无需等待冷却）

两种信号确保：
- 即使没有新工作请求，rail 也能在冷却到期后恢复
- 恢复的 rail 在有成功传输时立即服务，不浪费冷却时间

**答 4**：不重试的原因：
- **合并请求**：`merge_requests` 启用时，一个逻辑传输对应多个 `task_id`，重新提交会重复传输
- **部分入队**：某些传输在错误前已启动部分请求，`submitTransferTasks` 的返回状态无法区分哪些成功

区别：
- **完成阶段重试**：每个任务独立完成，明确知道哪个失败，可以安全重试
- **提交阶段不重试**：批量提交，无法确定部分成功状态，重试可能重复传输

## 总结

本讲义覆盖了 TENT 架构的四个核心模块：

1. **TENT 运行时**：分层架构设计，将传输选择、调度和故障处理移入运行时，通过 Segment 抽象统一数据位置描述
2. **动态传输选择**：基于配置策略的多维度匹配（segment_type、内存类型、优先级、大小）和设备掩码机制
3. **切片调度**：NUMA 感知的 EWMA 带宽估计、动态多路径分配和智能设备选择
4. **故障处理**：跨传输故障转移和 RDMA rail 恢复的两层故障处理，对应用程序透明

这些机制共同使 TENT 能够在异构集群中高效可靠地移动数据，无需应用程序感知底层硬件差异和传输细节。
