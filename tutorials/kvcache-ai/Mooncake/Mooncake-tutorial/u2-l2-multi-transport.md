# 多传输协议支持与设备发现

## 前置知识

本讲义 assumes 你已经完成 [u2-l1](./u2-l1-transfer-engine.md)，对 Transfer Engine 的整体架构有基本理解。我们将深入探讨 Transfer Engine 如何支持多种传输协议以及设备自动发现机制。

---

## 最小模块 1：传输后端抽象

### 概念说明

在分布式存储系统中，不同硬件环境支持不同的传输协议：
- **RDMA**：高性能网络（InfiniBand、RoCE）
- **TCP**：通用网络协议
- **EFA**：AWS 专属高性能网络
- **NVLink**：NVIDIA GPU 间高速互连

传输后端抽象解决的核心问题是：**如何用统一的接口支持多种底层传输协议，使得上层业务代码无需关心底层传输细节**。

### 伪代码流程

```
// 统一的传输接口
class Transport {
    virtual submitTransfer(batch_id, requests) = 0
    virtual getTransferStatus(batch_id, task_id) = 0
    virtual registerLocalMemory(addr, length) = 0
}

// 各协议实现统一接口
class RdmaTransport extends Transport { ... }
class TcpTransport extends Transport { ... }
class EfaTransport extends Transport { ... }

// 使用时无需关心底层协议
engine.installTransport("rdma")  // 或 "tcp" 或 "efa"
engine.submitTransfer(batch_id, requests)  // 统一调用
```

### 原理分析

传输后端抽象基于面向对象的多态特性。`Transport` 基类定义了所有传输协议必须实现的核心接口：

1. **批量传输接口**：`submitTransfer()` 提交传输任务，`getTransferStatus()` 查询状态
2. **内存注册接口**：`registerLocalMemory()`/`unregisterLocalMemory()` 注册本地内存
3. **Segment 管理**：`allocateBatchID()`/`freeBatchID()` 管理传输批次

每个具体传输协议（RDMA、TCP、EFA）都继承 `Transport` 并实现这些接口。上层代码通过基类指针调用，实际执行的是子类的实现。

这种设计的数学表示：设传输协议集合为 \(P = \{p_1, p_2, ..., p_n\}\)，每个协议 \(p_i\) 实现统一接口 \(I\)，则对于任意传输请求 \(r\)，有：

\[
\forall p_i \in P, \quad p_i.\text{submit}(r) \rightarrow \text{result}_i
\]

其中 \(\text{result}_i\) 表示协议 \(p_i\) 的执行结果。上层代码只需关心接口 \(I\)，无需知道具体是哪个 \(p_i\) 在执行。

### 代码实践

Transport 基类定义了所有传输协议的统一接口：

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transport/transport.h#L44-421

class Transport {
public:
    // 核心传输接口 - 所有传输协议都必须实现
    virtual Status submitTransfer(
        BatchID batch_id, 
        const std::vector<TransferRequest> &entries) = 0;
    
    virtual Status getTransferStatus(BatchID batch_id, size_t task_id,
                                     TransferStatus &status) = 0;
    
    // 内存注册接口
    virtual int registerLocalMemory(void *addr, size_t length,
                                    const std::string &location,
                                    bool remote_accessible) = 0;
    
    virtual int unregisterLocalMemory(void *addr) = 0;
    
    // 批次管理
    virtual BatchID allocateBatchID(size_t batch_size);
    virtual Status freeBatchID(BatchID batch_id);
    
    // 获取传输协议名称
    virtual const char *getName() const = 0;
    
protected:
    // 安装传输协议的通用框架
    virtual int install(std::string &local_server_name,
                       std::shared_ptr<TransferMetadata> meta,
                       std::shared_ptr<Topology> topo);
    
    std::string local_server_name_;
    std::shared_ptr<TransferMetadata> metadata_;
};
```

每个具体传输协议都继承并实现这些接口。以 RDMA 和 TCP 为例：

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transport/rdma_transport/rdma_transport.h#L42-63

class RdmaTransport : public Transport {
public:
    RdmaTransport();
    ~RdmaTransport();
    
    // 实现 Transport 基类接口
    Status submitTransfer(BatchID batch_id,
                          const std::vector<TransferRequest> &entries) override;
    
    Status getTransferStatus(BatchID batch_id, size_t task_id,
                             TransferStatus &status) override;
    
    int registerLocalMemory(void *addr, size_t length,
                            const std::string &location, 
                            bool remote_accessible,
                            bool update_metadata) override;
    
    const char *getName() const override { return "rdma"; }
    
private:
    // RDMA 特有的资源
    std::vector<std::shared_ptr<RdmaContext>> context_list_;
    std::shared_ptr<Topology> local_topology_;
};
```

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transport/tcp_transport/tcp_transport.h#L61-109

class TcpTransport : public Transport {
public:
    TcpTransport();
    ~TcpTransport();
    
    // 实现 Transport 基类接口
    Status submitTransfer(BatchID batch_id,
                          const std::vector<TransferRequest> &entries) override;
    
    Status getTransferStatus(BatchID batch_id, size_t task_id,
                             TransferStatus &status) override;
    
    const char *getName() const override { return "tcp"; }
    
private:
    // TCP 特有的资源
    TcpContext *context_;
    std::atomic_bool running_;
    std::thread thread_;
    
    // 连接池（可选启用）
    struct ConnectionKey {
        std::string host;
        uint16_t port;
    };
    std::unordered_map<ConnectionKey, 
                       std::deque<std::shared_ptr<PooledConnection>>,
                       ConnectionKeyHash> connection_pool_;
};
```

Transport 基类的 `install()` 方法为所有传输协议提供统一的安装框架：

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/src/transport/transport.cpp#L60-66

int Transport::install(std::string &local_server_name,
                       std::shared_ptr<TransferMetadata> meta,
                       std::shared_ptr<Topology> topo) {
    local_server_name_ = local_server_name;
    metadata_ = meta;
    return 0;
}
```

每个具体传输协议可以重写 `install()` 方法添加自己的初始化逻辑。例如 RDMA 需要初始化 RDMA 资源：

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp#L94-147

int RdmaTransport::install(std::string &local_server_name,
                           std::shared_ptr<TransferMetadata> meta,
                           std::shared_ptr<Topology> topo) {
    if (topo == nullptr) {
        LOG(ERROR) << "RdmaTransport: missing topology";
        return ERR_INVALID_ARGUMENT;
    }
    
    metadata_ = meta;
    local_server_name_ = local_server_name;
    local_topology_ = topo;
    
    // 处理双网卡环境：RDMA 网卡可能与 TCP 网卡不同
    const char *rdma_bind_addr = std::getenv("MC_RDMA_BIND_ADDRESS");
    if (rdma_bind_addr && rdma_bind_addr[0] != '\0') {
        auto [host_name, port] = parseHostNameWithPort(local_server_name);
        rdma_server_name_ = 
            std::string(rdma_bind_addr) + ":" + std::to_string(port);
        LOG(INFO) << "RdmaTransport: using RDMA bind address "
                  << rdma_server_name_;
    } else {
        rdma_server_name_ = local_server_name_;
    }
    
    // 初始化 RDMA 资源（保护域、完成队列等）
    auto ret = initializeRdmaResources();
    if (ret) {
        LOG(ERROR) << "RdmaTransport: cannot initialize RDMA resources";
        return ret;
    }
    
    // 分配本地 Segment ID
    ret = allocateLocalSegmentID();
    if (ret) return ret;
    
    // 启动握手守护进程
    ret = startHandshakeDaemon(local_server_name);
    if (ret) return ret;
    
    // 发布本地 Segment 描述符
    ret = metadata_->updateLocalSegmentDesc();
    if (ret) return ret;
    
    return 0;
}
```

### 练习题

1. **基础题**：为什么需要传输后端抽象？直接为每个协议写独立代码有什么问题？

2. **实现题**：假设你要为一个名为 "QUIC" 的新传输协议实现 Transport 接口，最少需要实现哪些方法？

3. **分析题**：`Transport::install()` 方法做了什么？为什么子类需要重写它？

4. **设计题**：如果要让一个传输同时支持 RDMA 和 TCP（RDMA 优先、TCP 降级），应该如何设计类结构？

### 答案

**答案 1**：传输后端抽象的好处：
- **代码复用**：上层业务逻辑只需写一次，适配所有传输协议
- **可扩展性**：新增传输协议只需实现 Transport 接口，无需修改上层代码
- **统一管理**：可以通过基类指针统一管理多个传输协议实例
- **易于测试**：可以轻松替换传输协议进行测试

直接为每个协议写独立代码的问题：
- 大量重复代码（内存管理、批次管理、状态查询等）
- 上层业务代码需要为每个协议写不同版本
- 新增协议需要修改大量现有代码

**答案 2**：实现 QUIC 传输协议最少需要实现的核心方法：
```cpp
class QuicTransport : public Transport {
public:
    // 必须实现的纯虚函数
    Status submitTransfer(BatchID batch_id,
                          const std::vector<TransferRequest> &entries) override;
    
    Status getTransferStatus(BatchID batch_id, size_t task_id,
                             TransferStatus &status) override;
    
    int registerLocalMemory(void *addr, size_t length,
                            const std::string &location,
                            bool remote_accessible) override;
    
    int unregisterLocalMemory(void *addr) override;
    
    const char *getName() const override { return "quic"; }
};
```

**答案 3**：`Transport::install()` 的作用：
- 保存本地服务器名称和元数据接口指针
- 为所有传输协议提供基础的安装逻辑

子类需要重写它的原因：
- 每个传输协议有不同的初始化需求（RDMA 需要创建保护域，TCP 需要启动 socket 服务）
- 需要协议特定的资源分配和验证
- 需要启动协议特定的后台线程（如 TCP 的 worker 线程、RDMA 的 CQ 轮询线程）

**答案 4**：同时支持 RDMA 和 TCP 的设计：
```cpp
class HybridTransport : public Transport {
public:
    Status submitTransfer(BatchID batch_id,
                          const std::vector<TransferRequest> &entries) override {
        // 尝试使用 RDMA
        if (rdma_transport_ && rdma_available_) {
            auto ret = rdma_transport_->submitTransfer(batch_id, entries);
            if (ret == Status::OK()) return Status::OK();
            
            // RDMA 失败，降级到 TCP
            LOG(WARNING) << "RDMA failed, fallback to TCP";
        }
        
        // 使用 TCP
        return tcp_transport_->submitTransfer(batch_id, entries);
    }
    
private:
    std::unique_ptr<RdmaTransport> rdma_transport_;
    std::unique_ptr<TcpTransport> tcp_transport_;
    bool rdma_available_ = false;
};
```

---

## 最小模块 2：设备发现

### 概念说明

设备发现解决的核心问题是：**如何自动检测系统可用的传输设备（RDMA 网卡、TCP 网卡、GPU 等），并根据硬件拓扑选择最优传输路径**。

在复杂的生产环境中，一个节点可能有：
- 多张 RDMA 网卡（mlx5_0, mlx5_1）
- 多张 GPU（GPU0, GPU1, GPU2, GPU3）
- 复杂的拓扑关系（某个 GPU 通过 PCIe 更接近某个网卡）

手动配置这些关系容易出错，因此需要自动发现机制。

### 伪代码流程

```
// 设备发现流程
function discover_devices():
    // 1. 扫描所有网络设备
    nics = scan_network_devices()
    
    // 2. 扫描所有内存设备（GPU、NVMe等）
    mems = scan_memory_devices()
    
    // 3. 构建拓扑矩阵：每个内存设备到每个网络设备的距离
    for mem in mems:
        for nic in nics:
            distance = get_distance(mem, nic)  // NUMA 距离、PCIe 距离等
            topology[mem][nic] = distance
    
    // 4. 根据距离排序，生成优先级列表
    for mem in mems:
        sorted_nics = sort_by_distance(topology[mem])
        topology.preferred_nics[mem] = sorted_nics
    
    return topology
```

### 原理分析

设备发现基于以下原理：

1. **硬件枚举**：通过系统 API（如 `ibv_devices`、`cudaGetDeviceCount`）枚举所有设备
2. **拓扑感知**：读取系统的拓扑信息（NUMA 节点、PCIe 总线、设备距离）
3. **优先级排序**：根据拓扑距离为每个内存设备选择最优的网络设备

拓扑发现可以用图论表示：设系统中有 \(N\) 个网络设备 \(\text{NIC} = \{n_1, n_2, ..., n_N\}\) 和 \(M\) 个内存设备 \(\text{MEM} = \{m_1, m_2, ..., m_M\}\)，则拓扑矩阵 \(T\) 为：

\[
T_{ij} = \text{distance}(m_i, n_j)
\]

其中 \(\text{distance}(m_i, n_j)\) 表示内存设备 \(m_i\) 到网络设备 \(n_j\) 的拓扑距离（NUMA 距离、PCIe 跳数等）。

对于内存设备 \(m_i\)，最优网络设备选择为：

\[
n_{\text{optimal}} = \arg\min_{n_j \in \text{NIC}} T_{ij}
\]

Mooncake 的设备发现还支持**设备过滤**和**手动覆盖**：
```bash
# 只扫描特定设备
export MOONCAKE_DEVICE_FILTER="mlx5_0,mlx5_1"

# 手动指定拓扑
export MOONCAKE_TOPOLOGY='{"cuda:0": {"preferred": ["mlx5_0"], "avail": ["mlx5_0", "mlx5_1"]}}'
```

### 代码实践

Topology 类负责设备发现和拓扑管理：

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/topology.h#L37-124

struct TopologyEntry {
    std::string name;                           // 设备名称
    std::vector<std::string> preferred_hca;     // 首选 HCA 列表
    std::vector<std::string> avail_hca;         // 可用 HCA 列表
    
    Json::Value toJson() const;
};

class Topology {
public:
    Topology();
    ~Topology();
    
    // 自动发现设备
    int discover();
    int discover(const std::vector<std::string> &filter);
    
    // 从 JSON 解析拓扑
    int parse(const std::string &topology_json);
    
    // 禁用特定设备
    int disableDevice(const std::string &device_name);
    
    // 设备选择：根据存储类型选择最优设备
    int selectDevice(const std::string storage_type, int retry_count = 0);
    int selectDevice(const std::string storage_type, std::string_view hint,
                     int retry_count = 0);
    
    // 获取拓扑矩阵
    TopologyMatrix getMatrix() const { return matrix_; }
    
private:
    TopologyMatrix matrix_;                     // 拓扑矩阵
    std::vector<std::string> hca_list_;        // 所有 HCA 列表
    bool use_round_robin_;                     // 是否轮询选择
};
```

Topology 的 discover() 方法实现自动设备发现：

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/tent/src/runtime/topology.cpp#L101-107

Status Topology::discover(const std::vector<Platform*>& platforms) {
    clear();
    // 遍历所有平台（Linux、CUDA、ROCm等）
    for (auto& entry : platforms) {
        // 探测该平台的网络和内存设备
        CHECK_STATUS(entry->probe(nic_list_, mem_list_));
    }
    return Status::OK();
}
```

每个 Platform 负责探测特定类型的设备。例如 Linux 平台探测 RDMA 设备：

```cpp
// Linux 平台的 probe 实现（简化示意）
Status LinuxPlatform::probe(std::vector<NicEntry>& nic_list,
                           std::vector<MemEntry>& mem_list) {
    // 1. 探测 RDMA 设备
    int num_devices;
    struct ibv_device** dev_list = ibv_get_device_list(&num_devices);
    for (int i = 0; i < num_devices; ++i) {
        NicEntry nic;
        nic.name = ibv_get_device_name(dev_list[i]);
        nic.type = NIC_RDMA;
        
        // 获取 PCI 信息和 NUMA 节点
        readPciInfo(nic.name, nic.pci_bus_id, nic.numa_node);
        
        nic_list.push_back(nic);
    }
    
    // 2. 探测系统内存
    MemEntry mem;
    mem.name = " dram";
    mem.type = MEM_DRAM;
    mem_list.push_back(mem);
    
    return Status::OK();
}
```

拓扑发现完成后，可以通过 `selectDevice()` 选择最优设备：

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/topology.h#L86-88

// 根据存储类型选择最优设备（支持轮询）
int selectDevice(const std::string storage_type, int retry_count = 0);

// 根据 hint 选择特定设备
int selectDevice(const std::string storage_type, std::string_view hint,
                 int retry_count = 0);
```

使用示例：

```cpp
// 创建拓扑并发现设备
auto topology = std::make_shared<Topology>();
topology->discover();  // 自动发现所有设备

// 查询某个存储类型的最优设备
int device_id = topology->selectDevice("cuda:0");
if (device_id < 0) {
    LOG(ERROR) << "No available device for cuda:0";
    return -1;
}

// 查询某个存储类型并指定 hint
int device_id = topology->selectDevice("cuda:0", "mlx5_1");
```

### 练习题

1. **基础题**：设备发现的三个核心步骤是什么？

2. **计算题**：假设系统有 2 个 RDMA 网卡（mlx5_0, mlx5_1）和 4 个 GPU（GPU0-GPU3），拓扑矩阵如下：

|      | mlx5_0 | mlx5_1 |
|------|--------|--------|
| GPU0 | 0      | 2      |
| GPU1 | 0      | 2      |
| GPU2 | 2      | 0      |
| GPU3 | 2      | 0      |

请为每个 GPU 选择最优网卡。

3. **实现题**：实现一个简单的 `selectDevice()` 函数，支持轮询选择：

```cpp
int selectDevice(const std::string& storage_type, int retry_count) {
    // 每次调用返回不同的设备（轮询）
    // TODO: 实现轮询逻辑
}
```

4. **设计题**：如何设计一个支持动态拓扑更新的系统？当新增/移除设备时，如何自动更新拓扑矩阵？

### 答案

**答案 1**：设备发现的三个核心步骤：
1. **硬件枚举**：扫描系统中所有可用的网络和内存设备
2. **拓扑感知**：读取设备间的拓扑关系（NUMA 节点、PCIe 距离等）
3. **优先级排序**：根据拓扑距离为每个内存设备生成最优网络设备列表

**答案 2**：根据拓扑矩阵，每个 GPU 的最优网卡选择：
- GPU0: mlx5_0（距离 0）
- GPU1: mlx5_0（距离 0）
- GPU2: mlx5_1（距离 0）
- GPU3: mlx5_1（距离 0）

这个拓扑表明 GPU0/GPU1 在 NUMA 节点 0 上，靠近 mlx5_0；GPU2/GPU3 在 NUMA 节点 1 上，靠近 mlx5_1。

**答案 3**：轮询选择实现：

```cpp
int selectDevice(const std::string& storage_type, int retry_count) {
    auto it = matrix_.find(storage_type);
    if (it == matrix.end() || it->second.preferred_hca.empty()) {
        return -1;  // 未找到存储类型或无可用设备
    }
    
    const auto& devices = it->second.preferred_hca;
    
    // 轮询选择：根据 retry_count 选择设备
    size_t index = retry_count % devices.size();
    const std::string& device_name = devices[index];
    
    // 查找设备 ID
    for (size_t i = 0; i < hca_list_.size(); ++i) {
        if (hca_list_[i] == device_name) {
            return static_cast<int>(i);
        }
    }
    
    return -1;  // 设备不存在
}
```

**答案 4**：动态拓扑更新系统设计：

```cpp
class DynamicTopology : public Topology {
public:
    // 启动后台监控线程
    void startMonitoring() {
        monitor_thread_ = std::thread([this]() {
            while (monitoring_enabled_) {
                // 1. 检测设备变化
                auto current_devices = scanCurrentDevices();
                
                // 2. 对比上次快照
                auto changes = detectChanges(last_snapshot_, current_devices);
                
                // 3. 如果有变化，重新发现拓扑
                if (!changes.empty()) {
                    LOG(INFO) << "Device topology changed, re-discovering...";
                    this->discover();
                    
                    // 4. 通知所有传输协议更新拓扑
                    notifyTransports();
                }
                
                last_snapshot_ = current_devices;
                std::this_thread::sleep_for(std::chrono::seconds(10));
            }
        });
    }
    
private:
    std::thread monitor_thread_;
    bool monitoring_enabled_ = true;
    DeviceSnapshot last_snapshot_;
};
```

---

## 最小模块 3：拓扑感知路径选择

### 概念说明

拓扑感知路径选择解决的核心问题是：**在多设备环境中，如何为每个传输请求自动选择最优的传输路径，以最大化带宽和最小化延迟**。

考虑以下场景：
- 节点有 2 张 RDMA 网卡（mlx5_0, mlx5_1）
- 节点有 4 个 GPU（GPU0, GPU1, GPU2, GPU3）
- GPU0/GPU1 通过 PCIe 更接近 mlx5_0
- GPU2/GPU3 通过 PCIe 更接近 mlx5_1

如果所有 GPU 都通过 mlx5_0 传输，会导致：
- mlx5_0 带宽瓶颈
- mlx5_1 闲置浪费
- 跨 NUMA 访问增加延迟

拓扑感知路径选择会自动为每个 GPU 选择最优网卡。

### 伪代码流程

```
// 拓扑感知路径选择
function select_transport_path(source_memory, destination_memory, topology):
    // 1. 查询源内存和目标内存的最优网卡
    source_nics = topology.getPreferredNics(source_memory)
    dest_nics = topology.getPreferredNics(destination_memory)
    
    // 2. 选择最优源-目标网卡对
    best_pair = None
    best_score = -infinity
    for src_nic in source_nics:
        for dst_nic in dest_nics:
            // 评分：带宽利用率 + 路径距离
            score = calculate_score(src_nic, dst_nic)
            if score > best_score:
                best_score = score
                best_pair = (src_nic, dst_nic)
    
    // 3. 返回最优路径
    return best_pair

function calculate_score(src_nic, dst_nic):
    // 考虑因素：
    // - 当前带宽利用率（利用率越低分数越高）
    // - 路径距离（距离越近分数越高）
    // - 负载均衡（避免单点瓶颈）
    bandwidth_utilization = getBandwidthUtilization(src_nic)
    distance = getPathDistance(src_nic, dst_nic)
    
    score = (1 - bandwidth_utilization) * 0.6 + (1 / distance) * 0.4
    return score
```

### 原理分析

拓扑感知路径选择基于以下原理：

1. **距离感知**：选择拓扑距离最近的设备（最小化 PCIe 跳数、NUMA 跨节点访问）
2. **负载均衡**：分散传输请求到多个网卡（避免单点瓶颈）
3. **带宽聚合**：充分利用多网卡带宽（并行传输）

路径选择的数学模型：设网卡集合为 \(N = \{n_1, n_2, ..., n_k\}\)，内存设备集合为 \(M = \{m_1, m_2, ..., m_p\}\)，定义：

- **距离函数**：\(d(m_i, n_j) \in \mathbb{R}_{\geq 0}\) 表示内存设备 \(m_i\) 到网卡 \(n_j\) 的拓扑距离
- **带宽函数**：\(b(n_j) \in [0, 1]\) 表示网卡 \(n_j\) 的当前带宽利用率
- **路径评分**：对于路径 \(P = (n_{src}, n_{dst})\)，评分为：

\[
S(P) = \alpha \cdot (1 - b(n_{src})) + \beta \cdot \frac{1}{d(m_{src}, n_{src}) + d(m_{dst}, n_{dst})}
\]

其中 \(\alpha + \beta = 1\)，\(\alpha\) 和 \(\beta\) 是权重参数（典型值：\(\alpha = 0.6\), \(\beta = 0.4\)）。

最优路径选择：

\[
P_{\text{optimal}} = \arg\max_{P \in \text{Paths}} S(P)
\]

Mooncake 还支持**多路径并行传输**：对于大块数据，可以同时使用多条路径传输，然后聚合带宽：

\[
\text{TotalBandwidth} = \sum_{i=1}^{k} b_i
\]

其中 \(b_i\) 是第 \(i\) 条路径的带宽。

### 代码实践

RdmaTransport 的 `selectDevice()` 方法实现了拓扑感知设备选择：

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transport/rdma_transport/rdma_transport.h#L128-132

public:
    // 根据存储描述符选择最优设备
    static int selectDevice(SegmentDesc *desc, uint64_t offset, size_t length,
                            int &buffer_id, int &device_id, int retry_cnt = 0);
    
    // 根据 hint 选择特定设备
    static int selectDevice(SegmentDesc *desc, uint64_t offset, size_t length,
                            std::string_view hint, int &buffer_id,
                            int &device_id, int retry_cnt = 0);
```

`selectDevice()` 的实现逻辑：

```cpp
// 伪代码示意（简化版）
int RdmaTransport::selectDevice(SegmentDesc *desc, uint64_t offset, 
                                size_t length, int &buffer_id, 
                                int &device_id, int retry_cnt) {
    // 1. 从 SegmentDesc 中获取存储位置（如 "cuda:0"）
    std::string location = desc->location;
    
    // 2. 查询拓扑：获取该存储位置的首选设备列表
    auto& topology = local_topology_;
    auto device_entry = topology->getMatrix()[location];
    
    if (device_entry.preferred_hca.empty()) {
        // 无首选设备，使用轮询
        device_id = retry_cnt % topology->getHcaList().size();
    } else {
        // 3. 根据重试次数选择设备（负载均衡）
        size_t index = retry_cnt % device_entry.preferred_hca.size();
        const std::string& device_name = device_entry.preferred_hca[index];
        
        // 4. 查找设备 ID
        for (size_t i = 0; i < topology->getHcaList().size(); ++i) {
            if (topology->getHcaList()[i] == device_name) {
                device_id = static_cast<int>(i);
                break;
            }
        }
    }
    
    // 5. 选择 buffer ID（该设备上的 buffer 索引）
    buffer_id = desc->getBufferId(device_id);
    
    return 0;
}
```

实际使用示例：

```cpp
// 假设要传输从 GPU0 到远程节点的数据
SegmentDesc* desc = ...;  // 包含源位置 "cuda:0"
uint64_t offset = 0;
size_t length = 1024 * 1024;  // 1MB

int buffer_id, device_id;

// 首次尝试：retry_cnt=0，选择首选设备
auto ret = RdmaTransport::selectDevice(desc, offset, length, 
                                       buffer_id, device_id, 0);
// device_id 应该指向最靠近 GPU0 的网卡（如 mlx5_0）

// 如果首次失败，重试时会选择下一个设备
ret = RdmaTransport::selectDevice(desc, offset, length, 
                                  buffer_id, device_id, 1);
// device_id 现在指向次选设备（如 mlx5_1）
```

EFA Transport 也支持类似的设备选择：

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transport/efa_transport/efa_transport.h#L137-141

public:
    static int selectDevice(SegmentDesc *desc, uint64_t offset, size_t length,
                            int &buffer_id, int &device_id, int retry_cnt = 0);
    
    static int selectDevice(SegmentDesc *desc, uint64_t offset, size_t length,
                            std::string_view hint, int &buffer_id,
                            int &device_id, int retry_cnt = 0);
```

### 练习题

1. **基础题**：拓扑感知路径选择的三个核心考虑因素是什么？

2. **计算题**：假设有 2 张网卡和 2 个 GPU，拓扑距离如下：

|      | mlx5_0 | mlx5_1 |
|------|--------|--------|
| GPU0 | 0      | 3      |
| GPU1 | 3      | 0      |

当前带宽利用率：mlx5_0 为 80%，mlx5_1 为 20%。评分公式：\(S = 0.6 \times (1 - b) + 0.4 \times \frac{1}{d}\)

请计算 GPU0 到远程节点的最优路径。

3. **设计题**：如何设计一个支持多路径并行传输的系统？

4. **分析题**：为什么需要 `retry_cnt` 参数？它如何影响设备选择？

### 答案

**答案 1**：拓扑感知路径选择的三个核心因素：
1. **距离感知**：选择拓扑距离最近的设备（减少 PCIe 跳数、NUMA 跨节点）
2. **负载均衡**：分散请求到多个网卡（避免单点瓶颈）
3. **带宽聚合**：利用多网卡并行传输（最大化总带宽）

**答案 2**：计算 GPU0 的最优路径

路径 1（mlx5_0）：
- \(b = 0.8\)（80% 利用率）
- \(d = 0\)
- \(S_1 = 0.6 \times (1 - 0.8) + 0.4 \times \frac{1}{0} = \infty\)（除零）

路径 2（mlx5_1）：
- \(b = 0.2\)（20% 利用率）
- \(d = 3\)
- \(S_2 = 0.6 \times (1 - 0.2) + 0.4 \times \frac{1}{3} = 0.48 + 0.133 = 0.613\)

修正评分公式（避免除零）：\(S = 0.6 \times (1 - b) + 0.4 \times \frac{1}{d + 1}\)

路径 1（mlx5_0）：
- \(S_1 = 0.6 \times 0.2 + 0.4 \times \frac{1}{1} = 0.12 + 0.4 = 0.52\)

路径 2（mlx5_1）：
- \(S_2 = 0.6 \times 0.8 + 0.4 \times \frac{1}{4} = 0.48 + 0.1 = 0.58\)

结论：选择 mlx5_1（评分 0.58 > 0.52）

**答案 3**：多路径并行传输系统设计：

```cpp
class MultiPathTransport : public Transport {
public:
    Status submitTransfer(BatchID batch_id,
                          const std::vector<TransferRequest> &entries) override {
        // 1. 对每个请求，计算应该使用多少条路径
        for (auto& req : entries) {
            // 根据数据大小决定路径数
            int num_paths = calculateNumPaths(req.length);
            
            // 2. 选择多条最优路径
            auto paths = selectTopKPaths(req.source, req.target_id, 
                                         req.target_offset, num_paths);
            
            // 3. 将数据分片到多条路径
            size_t chunk_size = req.length / num_paths;
            for (int i = 0; i < num_paths; ++i) {
                TransferRequest sub_req = req;
                sub_req.length = chunk_size;
                sub_req.source = (char*)req.source + i * chunk_size;
                sub_req.target_offset += i * chunk_size;
                sub_req.transport_hint = paths[i];  // 指定使用的路径
                
                // 4. 提交子请求
                paths[i]->submitTransfer(batch_id, {sub_req});
            }
        }
        
        return Status::OK();
    }
    
private:
    std::vector<Transport*> selectTopKPaths(void* source, SegmentID target,
                                           uint64_t offset, int k) {
        // 根据评分选择 Top-K 路径
        std::vector<std::pair<Transport*, double>> scores;
        
        for (auto* transport : available_transports_) {
            double score = calculatePathScore(transport, source, target);
            scores.push_back({transport, score});
        }
        
        // 排序并取 Top-K
        std::sort(scores.begin(), scores.end(),
                 [](auto& a, auto& b) { return a.second > b.second; });
        
        std::vector<Transport*> result;
        for (int i = 0; i < k && i < scores.size(); ++i) {
            result.push_back(scores[i].first);
        }
        
        return result;
    }
};
```

**答案 4**：`retry_cnt` 参数的作用：

1. **负载均衡**：通过轮询选择不同设备，避免所有请求都使用同一设备
2. **故障转移**：当首选设备失败时，自动切换到次选设备
3. **避免热点**：分散请求到多个设备，提高整体吞吐量

工作原理：
```cpp
// 首次尝试：retry_cnt=0，选择 preferred_hca[0]
int device_id = retry_cnt % preferred_hca.size();  // 0

// 重试：retry_cnt=1，选择 preferred_hca[1]
int device_id = retry_cnt % preferred_hca.size();  // 1

// 再次重试：retry_cnt=2，回到 preferred_hca[0]
int device_id = retry_cnt % preferred_hca.size();  // 0
```

这种设计实现了自动的轮询负载均衡，无需手动配置。

---

## 最小模块 4：多传输管理

### 概念说明

多传输管理解决的核心问题是：**如何在一个 Transfer Engine 实例中动态管理多个传输协议，支持运行时安装、卸载和切换传输协议**。

考虑以下场景：
1. **混合传输**：同时使用 RDMA 和 TCP（RDMA 用于 GPU 内存，TCP 用于 DRAM）
2. **动态切换**：RDMA 网卡故障时自动切换到 TCP
3. **A/B 测试**：对比不同传输协议的性能

多传输管理允许用户通过 `installTransport()`/`uninstallTransport()` 动态管理传输后端。

### 伪代码流程

```
// 多传输管理
class MultiTransportManager {
    Map<String, Transport*> transports;  // 协议名 -> 传输实例
    
    // 安装传输协议
    function installTransport(protocol, args):
        if transports.contains(protocol):
            return Error("Already installed")
        
        // 根据协议名创建传输实例
        transport = createTransport(protocol)
        
        // 初始化传输协议
        ret = transport.install(local_server_name, metadata, topology)
        if ret != OK:
            return Error("Install failed")
        
        transports[protocol] = transport
        return OK
    
    // 卸载传输协议
    function uninstallTransport(protocol):
        if not transports.contains(protocol):
            return Error("Not installed")
        
        transport = transports[protocol]
        
        // 检查是否有活跃的传输任务
        if transport.hasActiveTransfers():
            return Error("Has active transfers")
        
        // 清理传输协议
        transport.cleanup()
        
        transports.remove(protocol)
        return OK
    
    // 提交传输请求（自动选择传输协议）
    function submitTransfer(request):
        // 根据请求的特征选择传输协议
        protocol = selectProtocol(request)
        
        transport = transports[protocol]
        return transport.submitTransfer(request)
}
```

### 原理分析

多传输管理基于以下原理：

1. **协议工厂模式**：根据协议名称动态创建传输实例
   \[
   \text{create}(\text{protocol}) \rightarrow \text{Transport}*
   \]
   
2. **生命周期管理**：管理传输协议的安装、使用和卸载
   \[
   \text{install} \rightarrow \text{active} \rightarrow \text{uninstall}
   \]
   
3. **协议选择**：根据请求特征（内存位置、大小等）自动选择最优协议
   \[
   \text{selectProtocol}(request) \rightarrow \text{protocol}
   \]

协议选择的数学模型：设协议集合为 \(P = \{p_1, p_2, ..., p_n\}\)，请求特征向量为 \(F\)（包括内存类型、数据大小、延迟要求等），则协议选择函数为：

\[
s(F) = \arg\max_{p \in P} \text{score}(p, F)
\]

其中 \(\text{score}(p, F)\) 表示协议 \(p\) 对特征 \(F\) 的适配度评分。

例如，对于 GPU 内存传输：
- RDMA 评分：\(\text{score}(\text{rdma}, \text{GPU}) = 0.95\)（支持 GPUDirect）
- TCP 评分：\(\text{score}(\text{tcp}, \text{GPU}) = 0.60\)（需要 CPU 中转）
- 选择：\(s(\text{GPU}) = \text{rdma}\)

### 代码实践

TransferEngine 提供了 `installTransport()` 和 `uninstallTransport()` 方法：

```cpp
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transfer_engine.h#L83-85

// 安装传输协议
Transport* installTransport(const std::string& proto, void** args);

// 卸载传输协议
int uninstallTransport(const std::string& proto);
```

使用示例：

```cpp
// 创建 TransferEngine
TransferEngine engine;
engine.init("etcd://localhost:2379", "node1", "192.168.1.10", 12345);

// 安装 RDMA 传输协议
void* rdma_args[] = {
    (void*)"device_name", (void*)"mlx5_0",
    nullptr
};
Transport* rdma_transport = engine.installTransport("rdma", rdma_args);
if (rdma_transport == nullptr) {
    LOG(ERROR) << "Failed to install RDMA transport";
    return -1;
}

// 安装 TCP 传输协议
Transport* tcp_transport = engine.installTransport("tcp", nullptr);
if (tcp_transport == nullptr) {
    LOG(ERROR) << "Failed to install TCP transport";
    return -1;
}

// 使用 RDMA 传输 GPU 内存
SegmentHandle gpu_segment = engine.openSegment("gpu_cache");
engine.registerLocalMemory(gpu_buffer, buffer_size, "cuda:0");

// 使用 TCP 传输 DRAM
SegmentHandle dram_segment = engine.openSegment("dram_cache");
engine.registerLocalMemory(dram_buffer, buffer_size, "dram");

// 提交传输请求（系统自动选择协议）
auto batch_id = engine.allocateBatchID(2);
engine.submitTransfer(batch_id, {
    {gpu_buffer, target_id1, 0, buffer_size, Transport::OpCode::WRITE},  // RDMA
    {dram_buffer, target_id2, 0, buffer_size, Transport::OpCode::WRITE}  // TCP
});

// 卸载传输协议（使用完毕后）
engine.uninstallTransport("rdma");
engine.uninstallTransport("tcp");
```

`installTransport()` 的实现逻辑（示意）：

```cpp
// 伪代码（简化版）
Transport* TransferEngine::installTransport(const std::string& proto, 
                                           void** args) {
    // 1. 检查协议是否已安装
    if (transports_.find(proto) != transports_.end()) {
        LOG(ERROR) << "Transport " << proto << " already installed";
        return nullptr;
    }
    
    // 2. 创建传输实例
    Transport* transport = nullptr;
    if (proto == "rdma") {
        transport = new RdmaTransport();
    } else if (proto == "tcp") {
        transport = new TcpTransport();
    } else if (proto == "efa") {
        transport = new EfaTransport();
    } else {
        LOG(ERROR) << "Unknown protocol: " << proto;
        return nullptr;
    }
    
    // 3. 安装传输协议
    std::string local_server_name = getLocalServerName();
    auto ret = transport->install(local_server_name, metadata_, topology_);
    if (ret != 0) {
        LOG(ERROR) << "Failed to install transport " << proto;
        delete transport;
        return nullptr;
    }
    
    // 4. 注册到管理器
    transports_[proto] = transport;
    
    return transport;
}
```

`uninstallTransport()` 的实现逻辑（示意）：

```cpp
int TransferEngine::uninstallTransport(const std::string& proto) {
    // 1. 检查协议是否已安装
    auto it = transports_.find(proto);
    if (it == transports_.end()) {
        LOG(ERROR) << "Transport " << proto << " not installed";
        return -1;
    }
    
    Transport* transport = it->second;
    
    // 2. 检查是否有活跃的传输任务
    if (transport->hasActiveTransfers()) {
        LOG(ERROR) << "Transport " << proto << " has active transfers";
        return -1;
    }
    
    // 3. 清理传输协议
    delete transport;
    
    // 4. 从管理器中移除
    transports_.erase(it);
    
    return 0;
}
```

### 练习题

1. **基础题**：多传输管理的三个核心功能是什么？

2. **实现题**：实现一个简单的 `installTransport()` 函数：

```cpp
Transport* installTransport(const std::string& proto) {
    // TODO: 创建并安装传输协议
    // 支持的协议：rdma, tcp, efa
}
```

3. **设计题**：如何设计一个支持协议自动选择的系统？

4. **分析题**：为什么需要在卸载传输协议前检查是否有活跃的传输任务？

### 答案

**答案 1**：多传输管理的三个核心功能：
1. **动态安装**：运行时安装新的传输协议
2. **动态卸载**：运行时卸载不需要的传输协议
3. **协议选择**：根据请求特征自动选择最优传输协议

**答案 2**：`installTransport()` 实现：

```cpp
Transport* installTransport(const std::string& proto) {
    // 工厂函数：根据协议名创建传输实例
    auto createTransport = [](const std::string& proto) -> Transport* {
        if (proto == "rdma") return new RdmaTransport();
        if (proto == "tcp") return new TcpTransport();
        if (proto == "efa") return new EfaTransport();
        return nullptr;
    };
    
    // 1. 创建传输实例
    Transport* transport = createTransport(proto);
    if (transport == nullptr) {
        LOG(ERROR) << "Unknown protocol: " << proto;
        return nullptr;
    }
    
    // 2. 安装传输协议
    std::string local_server_name = getLocalServerName();
    auto ret = transport->install(local_server_name, metadata_, topology_);
    if (ret != 0) {
        LOG(ERROR) << "Failed to install transport: " << proto;
        delete transport;
        return nullptr;
    }
    
    // 3. 注册到管理器
    transports_[proto] = transport;
    
    LOG(INFO) << "Transport " << proto << " installed successfully";
    return transport;
}
```

**答案 3**：协议自动选择系统设计：

```cpp
class ProtocolSelector {
public:
    // 根据请求特征选择最优协议
    std::string selectProtocol(const TransferRequest& request) {
        // 特征提取
        auto features = extractFeatures(request);
        
        // 评分并选择最高分协议
        std::string best_protocol = "tcp";  // 默认
        double best_score = 0.0;
        
        for (const auto& [protocol, transport] : installed_transports_) {
            double score = calculateScore(protocol, features);
            if (score > best_score) {
                best_score = score;
                best_protocol = protocol;
            }
        }
        
        return best_protocol;
    }
    
private:
    // 提取请求特征
    FeatureVector extractFeatures(const TransferRequest& request) {
        FeatureVector f;
        f.memory_type = getMemoryType(request.source);
        f.data_size = request.length;
        f.latency_requirement = request.latency_hint;
        return f;
    }
    
    // 计算协议评分
    double calculateScore(const std::string& protocol, 
                          const FeatureVector& features) {
        double score = 0.0;
        
        // GPU 内存：RDMA 优先（支持 GPUDirect）
        if (features.memory_type == "cuda") {
            if (protocol == "rdma") score += 0.9;
            else if (protocol == "tcp") score += 0.5;
        }
        
        // 大数据传输：高带宽协议优先
        if (features.data_size > 1024 * 1024) {  // > 1MB
            if (protocol == "rdma" || protocol == "efa") score += 0.8;
            else if (protocol == "tcp") score += 0.6;
        }
        
        // 低延迟要求：低延迟协议优先
        if (features.latency_requirement < 10) {  // < 10us
            if (protocol == "rdma" || protocol == "nvlink") score += 0.9;
            else if (protocol == "tcp") score += 0.4;
        }
        
        return score;
    }
    
    std::unordered_map<std::string, Transport*> installed_transports_;
};
```

**答案 4**：卸载前检查活跃传输任务的原因：

1. **避免数据损坏**：如果传输正在进行中，强制卸载会导致数据丢失或损坏
2. **资源泄漏**：传输任务可能持有内存注册、句柄等资源，卸载会导致这些资源泄漏
3. **未定义行为**：传输协议可能被异步访问，卸载后访问野指针导致崩溃
4. **状态不一致**：元数据、拓扑等状态可能处于中间状态，卸载后无法恢复

正确的做法：
```cpp
// 1. 停止接受新任务
transport->stopAcceptingNewTasks();

// 2. 等待所有活跃任务完成
transport->waitForAllTasksComplete();

// 3. 清理资源
transport->cleanup();

// 4. 卸载协议
delete transport;
```

---

## 总结

本讲义介绍了 Mooncake Transfer Engine 的多传输协议支持与设备发现机制，涵盖以下四个最小模块：

1. **传输后端抽象**：通过 `Transport` 基类定义统一接口，支持 RDMA、TCP、EFA、NVLink 等多种传输协议

2. **设备发现**：`Topology` 类自动发现系统中的网络和内存设备，构建拓扑矩阵

3. **拓扑感知路径选择**：根据设备拓扑自动选择最优传输路径，支持负载均衡和多路径并行传输

4. **多传输管理**：通过 `installTransport()`/`uninstallTransport()` 动态管理传输协议

这些机制使得 Mooncake 能够：
- 自动适配不同硬件环境（GPU 集群、CPU 集群、混合环境）
- 最大化利用硬件资源（多网卡带宽聚合、拓扑感知选择）
- 提供灵活的配置方式（自动发现、手动覆盖、运行时切换）

### 关键代码链接

- Transport 基类：[transport.h#L44-421](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transport/transport.h#L44-421)
- RdmaTransport：[rdma_transport.h#L42-63](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transport/rdma_transport/rdma_transport.h#L42-63)
- TcpTransport：[tcp_transport.h#L61-109](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transport/tcp_transport/tcp_transport.h#L61-109)
- Topology：[topology.h#L37-124](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/topology.h#L37-124)
- TransferEngine：[transfer_engine.h#L83-85](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transfer_engine.h#L83-85)
