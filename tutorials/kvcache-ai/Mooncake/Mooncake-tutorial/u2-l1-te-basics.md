# Unit 2 - Lesson 1: Transfer Engine 基础 API 与核心概念

> 本课介绍 Transfer Engine 的基础概念（Segment、Batch、TransferRequest）与核心 API（registerLocalMemory、submitTransfer、allocateBatchID），通过简单的点对点传输示例理解 TE 如何抽象底层传输细节、实现批量提交与零拷贝数据移动。

**前置知识**: 完成 Unit 1 - Lesson 2（Mooncake Store 编程模型与 Python API）

## 最小模块 1: Segment 抽象

### 概念说明

Segment（段）是 Mooncake Transfer Engine 中对**远程内存区域**的逻辑抽象。在分布式系统中，不同的节点（机器/设备）各自管理自己的本地内存，当需要跨节点传输数据时，发送方需要知道接收方内存的**地址信息**才能将数据写入正确位置。

Segment 抽象解决了以下问题：
- **寻址问题**：如何标识远程节点的内存区域？
- **拓扑发现**：如何获取远程内存的物理拓扑信息（如 RDMA 设备 LID/GID）？
- **多协议支持**：如何统一表示 RDMA、TCP、NVLink 等不同协议的内存区域？

在 Mooncake 中，每个 Segment 都有一个唯一的 **SegmentID**（64 位整数），以及一个人类可读的名称（如 `"node1:12345"`）。Segment 描述信息（`SegmentDesc`）包含该段的所有元数据：协议类型、设备列表、缓冲区列表、拓扑信息等。

### 伪代码或流程

```python
# 发送方视角
segment_id = engine.openSegment("receiver:12345")
segment_desc = engine.getMetadata().getSegmentDesc(segment_id)
# segment_desc 包含：
# - name: "receiver:12345"
# - protocol: "rdma"
# - devices: [{name: "rocep5s0f0", lid: 123, gid: "..."}]
# - buffers: [{addr: 0x7f8e4c000000, length: 1048576, rkey: [0x123]}]

# 接收方视角（元数据发布）
segment_name = "localhost:12345"  # 本地服务名
buffer = np.zeros(1024*1024, dtype=np.uint8)
ptr = buffer.ctypes.data
length = buffer.nbytes
engine.registerLocalMemory(ptr, length, location="localhost:12345")
# 元数据服务自动发布 SegmentDesc
```

### 原理分析

Segment 描述信息存储在**元数据服务**中（TransferMetadata），所有节点通过共享元数据服务来发现彼此的内存布局。当一个节点注册本地内存时，`TransferMetadata` 会创建一个 `SegmentDesc` 并将其发布到元数据服务（如 etcd、P2P Handshake 或 HTTP Server）。

关键数据结构（[transfer_metadata.h:88-121](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transfer_metadata.h#L88-L121)）：

```cpp
struct SegmentDesc {
    std::string name;                    // Segment 名称，如 "node1:12345"
    std::string protocol;                // 传输协议：rdma/tcp/nvlink
    std::vector<DeviceDesc> devices;     // 设备列表（RDMA 需要 LID/GID）
    Topology topology;                   // 拓扑信息（NUMA 节点、PCIe 拓扑）
    std::vector<BufferDesc> buffers;     // 该 Segment 管理的缓冲区列表
    std::string rdma_server_name;        // 双网卡环境下 RDMA 可达地址
    // ...
};
```

SegmentID 到 SegmentDesc 的映射由 `TransferMetadata` 维护，通过 `openSegment()` 打开远程 Segment 时，TE 会从元数据服务拉取对应的 `SegmentDesc` 并缓存到本地。

### 代码实践

在 Python API 中，Segment 的打开和关闭操作通常由 `TransferEngine` 内部自动处理。以下代码展示如何显式操作 Segment（C++ API）：

```cpp
// 打开远程 Segment
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transfer_engine.h#L91
SegmentHandle handle = engine.openSegment("receiver:12345");

// 检查 Segment 状态
Status status = engine.CheckSegmentStatus(segment_id);

// 关闭 Segment
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transfer_engine.h#L95
engine.closeSegment(handle);

// 移除本地 Segment
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transfer_engine.h#L97
engine.removeLocalSegment("localhost:12345");
```

在 Python API 中，这些操作被封装在 `transfer_sync_write()` 等高级接口中，用户只需提供目标节点的 `session_id`（即 Segment 名称）：

```python
# Python API 中 session_id 就是 Segment 名称
# https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/quick-start.md#L156-L164
ret = client_engine.transfer_sync_write(
    server_session_id,  # 目标 Segment 名称
    client_ptr,         # 本地源地址
    server_ptr,        # 远程目标地址
    length             # 传输长度
)
```

### 练习题

1. **基础理解**：SegmentDesc 中为什么需要存储 `devices` 列表？TCP 协议的 SegmentDesc 中这个字段为空吗？

2. **设计思考**：在双网卡环境下，为什么 SegmentDesc 需要 `rdma_server_name` 字段？它与 `name` 字段有何区别？

3. **实践题**：查看 `openSegment()` 的实现（`transfer_engine_impl.cpp`），说明它如何从元数据服务获取 SegmentDesc？

4. **故障排查**：如果 `openSegment()` 返回 `INVALID_SEGMENT_ID`，可能的原因有哪些？

### 答案

1. **答案**：SegmentDesc 需要存储 `devices` 列表是因为 RDMA 等协议需要知道远程内存物理设备的 LID（Local Identifier）和 GID（Global Identifier），才能创建 Queue Pair（QP）并执行 RDMA 操作。TCP 协议的 SegmentDesc 中这个字段通常为空或仅包含一个占位符，因为 TCP 不需要这些硬件信息。

2. **答案**：双网卡环境下，一个节点可能有两个 IP 地址：一个用于 TCP 路由（如管理网络），另一个用于 RDMA 的高速网卡。`name` 字段存储 TCP 可达地址，`rdma_server_name` 存储 RDMA 可达地址。TE 在构建 RDMA NIC 路径时会优先使用 `rdma_server_name`（见 `SegmentDesc::nicPathServerName()`）。

3. **答案**：`openSegment()` 首先调用 `metadata_->getSegmentDesc(segment_name)` 从元数据服务（etcd/P2P/HTTP）拉取 SegmentDesc，然后调用 `transport->OpenChannel()` 建立传输通道（如 RDMA QP），最后返回 SegmentHandle（即 SegmentID）。

4. **答案**：可能原因包括：目标 Segment 未注册、元数据服务连接失败、Segment 名称拼写错误、协议不匹配（如 RDMA 客户端连接 TCP Segment）。

---

## 最小模块 2: 内存注册

### 概念说明

内存注册（Memory Registration）是 RDMA 和其他高性能传输协议中的**关键操作**。在传统的网络传输（如 TCP）中，操作系统内核会自动管理内存的页表映射；但在 RDMA 中，网卡需要直接访问用户空间内存，绕过内核。

为了实现这一点，RDMA 要求应用程序显式地**注册**内存区域，告诉网卡：
1. 这块内存的**虚拟地址**和**长度**是什么
2. 这块内存对应的**物理页**在哪里（操作系统会锁定这些页，防止被 swap）
3. 这块内存的**访问权限**（只读/读写）

注册后，RDMA 驱动会返回一个**本地密钥（lkey）**和**远程密钥（rkey）**：
- `lkey`：本地网卡用于发送数据时访问这块内存
- `rkey`：发送给远程节点，远程网卡通过 rkey 写入这块内存

### 伪代码或流程

```python
# 传统 TCP：无需注册，内核自动处理
socket.send(buf)  # 内核自动将 buf 映射到网卡

# RDMA：必须先注册
ptr = buffer.ctypes.data  # 虚拟地址
length = buffer.nbytes    # 长度

ret = engine.registerLocalMemory(ptr, length, location="localhost:12345")
# 返回：
# - lkey（本地密钥）存储在本地 BufferDesc
# - rkey（远程密钥）发布到元数据服务，供远程节点查询

# 远程节点通过元数据服务获取 rkey
segment_desc = engine.getMetadata().getSegmentDesc("localhost:12345")
rkey = segment_desc.buffers[0].rkey[0]  # 第一个缓冲区的 rkey

# 使用 rkey 执行 RDMA WRITE
engine.submitTransfer(batch_id, [
    TransferRequest(
        opcode=WRITE,
        source=local_ptr,
        target_id=segment_id,
        target_offset=0,
        length=1024,
        advise_retry_cnt=3
    )
])  # TE 自动查表获取 rkey 并填充 RDMA Work Request
```

### 原理分析

内存注册的本质是**构建虚拟地址到物理地址的映射表**，并将该表加载到网卡的地址转换与保护引擎（Translation and Protection Table, TPT）。RDMA 网卡在执行 RDMA 操作时，会根据虚拟地址和 lkey/rkey 查表获取物理地址，然后直接通过 DMA（Direct Memory Access）访问内存。

关键数据结构（[transfer_metadata.h:52-65](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transfer_metadata.h#L52-L65)）：

```cpp
struct BufferDesc {
    std::string name;                       // 缓冲区名称
    uint64_t addr;                          // 虚拟地址
    uint64_t length;                        // 长度
    std::vector<uint32_t> lkey;             // 本地密钥（可多个，对应不同设备）
    std::vector<uint32_t> rkey;             // 远程密钥（可多个，对应不同设备）
    std::string shm_name;                    // 共享内存名称（用于 NVLink）
    uint64_t offset;                        // 偏移量（用于 CXL）
    std::vector<std::string> tseg;          // 传输段（用于 UB/URMA）
    std::vector<uint32_t> l_seg_index;     // 本地段索引
};
```

为什么需要**多个密钥**？在多设备环境下（如一台机器有多张 RDMA 网卡），同一块内存可能在不同设备上有不同的 lkey/rkey。TE 会为每个设备生成独立的密钥对。

注册流程（RDMA）：
1. 应用调用 `ibv_reg_mr()`（libibverbs 库）
2. 驱动锁定内存页（`mlock`），防止被 swap
3. 驱动构建虚拟地址→物理地址的映射表
4. 驱动将映射表加载到网卡 TPT
5. 返回 `lkey` 和 `rkey`

### 代码实践

Python API 中，内存注册通过 `register_memory()` 完成：

```python
# https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/quick-start.md#L62-L67
if PROTOCOL == "rdma":
    ret_value = server_engine.register_memory(server_ptr, server_len)
    if ret_value != 0:
        print("Mooncake memory registration failed.")
        raise RuntimeError("Mooncake memory registration failed.")
```

C++ API 中，内存注册支持更细粒度的控制：

```cpp
// 注册本地内存（自动发布到元数据服务）
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transfer_engine.h#L99-L102
int ret = engine.registerLocalMemory(
    addr,                    // 虚拟地址
    length,                  // 长度
    "localhost:12345",       // Segment 名称（location）
    true,                    // remote_accessible：允许远程访问
    true                     // update_metadata：自动发布到元数据服务
);

// 批量注册（高效处理多个缓冲区）
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transfer_engine.h#L133-L134
std::vector<BufferEntry> buffer_list = {{addr1, len1}, {addr2, len2}};
engine.registerLocalMemoryBatch(buffer_list, "localhost:12345");

// 注销内存
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transfer_engine.h#L104
engine.unregisterLocalMemory(addr, true);
```

### 练习题

1. **基础理解**：为什么 RDMA 需要内存注册，而 TCP 不需要？这与数据拷贝次数有何关系？

2. **设计思考**：`registerLocalMemory()` 的 `remote_accessible` 参数为 `false` 时，这块内存仍然可以被本地传输使用吗？这种设计的用途是什么？

3. **实践题**：查看 `RdmaTransport::registerLocalMemoryInternal()` 的实现，说明它在调用 `ibv_reg_mr()` 前做了什么准备工作？

4. **故障排查**：如果 `registerLocalMemory()` 返回 `-1` 且 `errno` 为 `ENOMEM`，可能的原因是什么？如何缓解？

### 答案

1. **答案**：RDMA 需要内存注册是因为网卡需要直接访问用户空间内存，绕过内核。TCP 不需要注册是因为内核会自动管理内存映射。注册后，RDMA 可以实现**零拷贝**传输（网卡直接 DMA），而 TCP 需要经过**用户空间→内核→网卡**的两次拷贝。

2. **答案**：`remote_accessible=false` 时，这块内存仍然可以被本地传输使用（如本地 PCIe 设备间的传输），但不会发布 `rkey` 到元数据服务，远程节点无法直接访问。这种设计用于保护敏感内存（如私钥）不被远程节点访问。

3. **答案**：`RdmaTransport::registerLocalMemoryInternal()` 在调用 `ibv_reg_mr()` 前会：
   - 调用 `checkOverlap()` 检查内存是否与已注册区域重叠
   - 调用 `preTouchMemory()` 预先访问所有页（触发缺页中断，确保物理页已分配）
   - 调用 `ibv_reg_mr()` 注册内存
   - 将 `lkey/rkey` 存储到本地 `BufferDesc` 并发布到元数据服务

4. **答案**：`errno=ENOMEM` 表示内存页锁定失败，可能原因：
   - 超过 `ulimit -l` 锁定内存限制（解决：`ulimit -l unlimited`）
   - 系统物理内存不足（解决：释放内存或增加 swap）
   - 内存区域未实际分配（解决：`memset` 预先访问触发缺页）

---

## 最小模块 3: 批量传输

### 概念说明

批量传输（Batch Transfer）是 Mooncake Transfer Engine 的**核心优化机制**。在高性能网络中，单次网络请求的**固定开销**（如系统调用、上下文切换、网卡轮询）远高于数据传输本身。为了减少这种开销，TE 允许将多个传输请求打包成一个**批次（Batch）**，一次性提交到底层传输协议。

关键概念：
- **BatchID**：批次的唯一标识符（64 位整数），本质上是一个指向 `BatchDesc` 结构的指针
- **TransferRequest**：单个传输请求，描述源地址、目标 SegmentID、偏移量、长度、操作码（READ/WRITE）
- **原子性**：同一个 Batch 内的所有请求会一起提交，一起完成（或一起失败）

批量传输的优势：
- **减少系统调用**：多个请求合并为一次 `submitTransfer()` 调用
- **减少网卡轮询**：多个请求的完成状态在一次轮询中获取
- **提高流水线效率**：网卡可以并行处理同一 Batch 的多个请求

### 伪代码或流程

```python
# 传统方式：逐个提交（低效）
for i in range(100):
    engine.transfer_sync_write(target_id, src_ptrs[i], dst_offsets[i], lengths[i])

# 批量方式：打包提交（高效）
batch_id = engine.allocateBatchID(100)  # 预分配 100 个任务的 Batch
requests = []
for i in range(100):
    requests.append(TransferRequest(
        opcode=WRITE,
        source=src_ptrs[i],
        target_id=target_id,
        target_offset=dst_offsets[i],
        length=lengths[i]
    ))

# 一次性提交整个批次
status = engine.submitTransfer(batch_id, requests)

# 等待整个批次完成
while True:
    batch_status = engine.getBatchTransferStatus(batch_id)
    if batch_status.s == COMPLETED:
        break
    elif batch_status.s == FAILED:
        raise RuntimeError("Batch transfer failed")

# 释放 BatchID
engine.freeBatchID(batch_id)
```

### 原理分析

批次描述符（`BatchDesc`）存储了整个批次的所有任务和同步原语：

```cpp
struct BatchDesc {
    BatchID id;                           // 批次 ID（其实是 this 指针的整数值）
    size_t batch_size;                    // 任务数量
    std::vector<TransferTask> task_list; // 任务列表
    void *context;                        // 传输层私有上下文
    int64_t start_timestamp;             // 开始时间戳

    std::atomic<bool> has_failure;        // 是否有任务失败
    std::atomic<bool> is_finished;        // 是否完成
    std::atomic<uint64_t> finished_transfer_bytes;  // 已传输字节数
    std::atomic<uint64_t> finished_task_count;     // 已完成任务数（事件驱动模式）

    std::mutex completion_mutex;          // 完成通知的互斥锁
    std::condition_variable completion_cv;  // 完成通知的条件变量
};
```

**BatchID 的实现技巧**（[transport.h:91-104](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transport/transport.h#L91-L104)）：

```cpp
// BatchID 本质上是一个指向 BatchDesc 的指针， reinterpret_cast 为 64 位整数
static inline BatchDesc &toBatchDesc(BatchID id) {
    return *reinterpret_cast<BatchDesc *>(id);
}

// 分配 BatchID 就是 new BatchDesc()
virtual BatchID allocateBatchID(size_t batch_size) {
    BatchDesc *desc = new BatchDesc();
    desc->id = reinterpret_cast<BatchID>(desc);  // 自指
    desc->batch_size = batch_size;
    desc->task_list.resize(batch_size);
    return desc->id;
}
```

这种设计避免了 `std::unordered_map` 的查找开销，在热路径上实现 **O(1)** 批次定位。

**事件驱动的批量完成检测**（[transport.h:193-244](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transport/transport.h#L193-L244)）：

每个 `Slice`（传输切片）完成时会原子地增加 `completed_slice_count`，最后一个完成的线程负责检查整个 Batch 是否完成，并通过 `condition_variable` 唤醒等待线程。

### 代码实践

Python API 中，批量传输通过 `submitTransfer()` 实现：

```python
# 分配 BatchID（Python 中由 transfer_sync_write 自动处理）
batch_id = engine.allocateBatchID(10)

# 构建请求列表
requests = []
for i in range(10):
    req = TransferRequest()
    req.opcode = TransferRequest.WRITE
    req.source = client_buffers[i].ctypes.data
    req.target_id = server_segment_id
    req.target_offset = i * chunk_size
    req.length = chunk_size
    requests.append(req)

# 提交批量传输
status = engine.submitTransfer(batch_id, requests)

# 等待完成
while True:
    status = engine.get_batch_transfer_status(batch_id)
    if status.s == TransferStatus.COMPLETED:
        print(f"Transferred {status.transferred_bytes} bytes")
        break
    time.sleep(0.001)  # 避免忙等待

# 释放 BatchID
engine.free_batch_id(batch_id)
```

C++ API 提供了更灵活的控制：

```cpp
// 分配 BatchID
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transport/transport.h#L355
BatchID batch_id = engine.allocateBatchID(100);

// 构建请求列表
std::vector<TransferRequest> requests;
for (int i = 0; i < 100; ++i) {
    requests.push_back({
        .opcode = TransferRequest::WRITE,
        .source = src_ptrs[i],
        .target_id = target_segment_id,
        .target_offset = i * chunk_size,
        .length = chunk_size,
        .advise_retry_cnt = 3,
        .transport_hint = 0
    });
}

// 提交批量传输
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transfer_engine.h#L106-L107
Status status = engine.submitTransfer(batch_id, requests);

// 等待完成
TransferStatus batch_status;
while (true) {
    status = engine.getBatchTransferStatus(batch_id, batch_status);
    if (status.ok() && batch_status.s == COMPLETED) {
        std::cout << "Transferred " << batch_status.transferred_bytes << " bytes\n";
        break;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
}

// 释放 BatchID
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transport/transport.h#L358
engine.freeBatchID(batch_id);
```

### 练习题

1. **基础理解**：为什么 BatchID 设计为 `BatchDesc` 指针的整数值，而不是简单的整数递增 ID？这种设计的性能优势是什么？

2. **设计思考**：`allocateBatchID(batch_size)` 需要预先指定批次大小，为什么不能动态增长？这与 `BatchDesc::task_list` 的内存布局有何关系？

3. **实践题**：查看 `submitTransfer()` 的实现（`multi_transport.cpp`），说明它如何将 `TransferRequest` 转换为底层的 `TransferTask`？

4. **故障排查**：如果 `submitTransfer()` 返回的 `Status` 中 `success_count < entries.size()`，说明部分请求提交失败。这种情况下，未提交的请求会如何处理？

### 答案

1. **答案**：BatchID 设计为 `BatchDesc` 指针可以避免 `std::unordered_map` 的查找开销，实现 **O(1)** 定位。如果使用整数 ID，则需要 `O(log n)` 的 `std::map` 或 `O(1)` 但哈希计算开销大的 `std::unordered_map`。指针转换是**零开销**的，只需一次 `reinterpret_cast`。

2. **答案**：`allocateBatchID(batch_size)` 预先分配固定大小的 `task_list` 是为了避免动态扩容时的**内存重分配**和**元素拷贝**，这会导致 `TransferTask` 中的指针失效（因为 `submitTransfer()` 中会存储 `Slice*` 到 `task.slice_list`）。动态增长会破坏这些指针的稳定性。

3. **答案**：`submitTransfer()` 会：
   - 遍历每个 `TransferRequest`，创建对应的 `TransferTask`
   - 根据请求的 `target_id` 查询 `SegmentDesc`，获取远程拓扑信息
   - 选择最优的传输协议（RDMA/TCP/NVLink）
   - 将 `TransferTask` 分发给对应的 `Transport::submitTransferTask()`
   - 每个 Transport 将任务拆解为多个 `Slice`（根据 MTU、设备限制）
   - 提交 `Slice` 到底层硬件（RDMA POST SEND、TCP WRITE）

4. **答案**：`submitTransfer()` 返回的 `success_count` 表示成功提交的请求数量。未提交的请求会被**跳过**，不会影响已提交请求的执行。调用者需要检查返回值，对失败的请求进行**重试**或**报错**。失败的原因通常是：内存未注册、SegmentID 无效、传输协议不支持。

---

## 最小模块 4: 零拷贝机制

### 概念说明

零拷贝（Zero-Copy）是高性能网络传输的**核心目标**，指数据在传输过程中**避免不必要的内存拷贝**。在传统的网络栈中，数据从发送方到接收方需要经过多次拷贝：

```
发送方：应用程序 → 用户态缓冲区 → 内核态套接字缓冲区 → 网卡 DMA
接收方：网卡 DMA → 内核态套接字缓冲区 → 用户态缓冲区 → 应用程序
```

这至少涉及 **4 次内存拷贝** 和 **2 次上下文切换**（用户态↔内核态）。

Mooncake Transfer Engine 通过以下技术实现**真正的零拷贝**：
1. **RDMA**：网卡直接访问用户空间内存，无需内核参与
2. **SPDK/NVMe**：绕过内核块设备层，直接驱动 SSD
3. **GPUDirect**：网卡直接访问 GPU 显存，无需 CPU 中转

### 伪代码或流程

```python
# 传统 TCP：多次拷贝
# 1. 用户将数据写入 socket.send() → 内核拷贝到套接字缓冲区
# 2. 内核将套接字缓冲区数据拷贝到网卡 DMA 引擎
# 3. 接收方网卡 DMA → 内核套接字缓冲区
# 4. 应用程序 read() → 内核拷贝到用户缓冲区
socket.send(buf)  # 隐式 2 次拷贝（用户→内核→网卡）
data = socket.recv(1024)  # 隐式 2 次拷贝（网卡→内核→用户）

# Mooncake RDMA：零拷贝
# 1. 应用调用 registerLocalMemory() → 注册内存（一次性开销）
# 2. 应用调用 submitTransfer() → 直接告诉网卡"从地址 A 写到远程地址 B"
# 3. 网卡 DMA 直接搬运数据，无需 CPU 和内核参与
engine.registerLocalMemory(local_ptr, length)  # 注册（一次性）
engine.registerLocalMemory(remote_ptr, length)  # 远程也注册
engine.submitTransfer(batch_id, [
    TransferRequest(
        opcode=WRITE,
        source=local_ptr,        # 网卡直接从这里读
        target_id=remote_id,    # 网卡直接写到这里
        target_offset=0,
        length=length
    )
])  # 网卡 DMA 直接搬运，CPU 可以继续执行其他代码
```

### 原理分析

**零拷贝的数学模型**：

假设：
- \(t_{copy}\)：单次内存拷贝时间（约 10 ns/字节，受内存带宽限制）
- \(t_{dma}\)：网卡 DMA 传输时间（受网络带宽限制，RDMA 约 1 ns/字节）
- \(n\)：数据大小（字节）

传统 TCP 的总传输时间：
\[
T_{TCP} = 4 \times n \times t_{copy} + 2 \times t_{ctx\_switch} + n \times t_{dma}
\]

RDMA 零拷贝的总传输时间：
\[
T_{RDMA} = n \times t_{dma}
\]

加速比：
\[
\text{Speedup} = \frac{T_{TCP}}{T_{RDMA}} = \frac{4 \times t_{copy} + \frac{2 \times t_{ctx\_switch}}{n} + t_{dma}}{t_{dma}}
\]

当 \(n\) 较大时，\(\frac{2 \times t_{ctx\_switch}}{n} \approx 0\)，加速比约为：
\[
\text{Speedup} \approx \frac{4 \times t_{copy} + t_{dma}}{t_{dma}} = \frac{4 \times 10\ \text{ns} + 1\ \text{ns}}{1\ \text{ns}} = 41\times
\]

**硬件前提**：
- **IOMMU（Input-Output Memory Management Unit）**：将用户虚拟地址直接转换为物理地址，供网卡 DMA 使用
- **PINNED Memory（锁定内存）**：通过 `mlock()` 防止内存页被 swap，确保物理地址稳定
- **Remote Key（rkey）验证**：网卡检查 rkey，防止未授权访问（安全机制）

### 代码实践

在 Mooncake 中，零拷贝是**自动启用**的，只要使用 RDMA 协议并正确注册内存：

```python
# 1. 初始化 RDMA Engine
engine = TransferEngine()
engine.initialize(
    "localhost",
    "P2PHANDSHAKE",
    "rdma",  # 关键：使用 RDMA 协议
    ""       # 自动发现 RDMA 设备
)

# 2. 分配并注册内存（必须注册才能零拷贝）
local_buffer = np.ones(1024*1024, dtype=np.uint8)
local_ptr = local_buffer.ctypes.data
local_len = local_buffer.nbytes

ret = engine.register_memory(local_ptr, local_len)
assert ret == 0, "Memory registration failed"

# 3. 提交传输（零拷贝）
# https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/quick-start.md#L159-L164
ret = engine.transfer_sync_write(
    server_session_id,
    local_ptr,
    server_ptr,
    min(local_len, server_len)
)

# CPU 可以立即执行其他任务，网卡在后台 DMA 传输
print("Transfer submitted, CPU is free...")
time.sleep(0.1)  # 模拟其他计算

# 4. 等待完成（可选）
engine.sync_segment_cache()  # 同步缓存（某些架构需要）
```

C++ API 中，零拷贝机制更清晰：

```cpp
// 注册内存（启用 RDMA 零拷贝）
// https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-transfer-engine/include/transfer_engine.h#L99-L102
engine.registerLocalMemory(
    local_ptr,
    length,
    "localhost:12345",
    true,  // remote_accessible：允许远程 RDMA 访问（零拷贝）
    true   // update_metadata：发布 rkey 到元数据服务
);

// 提交 RDMA WRITE（零拷贝）
TransferRequest req = {
    .opcode = TransferRequest::WRITE,
    .source = local_ptr,      // 网卡直接从这里 DMA 读
    .target_id = remote_id,  // 远程 SegmentID（包含 rkey）
    .target_offset = 0,
    .length = length
};

Status status = engine.submitTransfer(batch_id, {req});
// 网卡在后台执行 DMA，CPU 立即返回
```

### 练习题

1. **基础理解**：为什么 RDMA 需要内存注册才能实现零拷贝？不注册内存可以执行 RDMA 操作吗？

2. **设计思考**：在多租户环境中（如云平台），如何通过 rkey 防止恶意节点访问未授权的内存？

3. **实践题**：查看 `RdmaTransport::submitTransfer()` 的实现，说明它如何填充 RDMA Work Request 的 `rkey` 和 `addr` 字段？

4. **故障排查**：如果 RDMA 传输性能反而低于 TCP，可能的原因是什么？如何诊断？

### 答案

1. **答案**：RDMA 需要内存注册是因为网卡需要知道用户虚拟地址对应的物理地址，以及访问权限（通过 lkey/rkey）。不注册内存，网卡无法解析虚拟地址，IOMMU 会拒绝访问。注册的本质是**建立虚拟地址→物理地址的映射表**，并加载到网卡的 TPT（Translation and Protection Table）。

2. **答案**：rkey（Remote Key）是一个 32 位的随机值，由 RDMA 驱动在注册内存时生成。只有持有正确 rkey 的远程节点才能访问这块内存。rkey 存储在 `BufferDesc` 中，通过元数据服务分发给授权节点。未授权节点无法获取 rkey，即使知道虚拟地址也无法访问。这类似于**能力令牌（Capability Token）**的安全模型。

3. **答案**：`RdmaTransport::submitTransfer()` 会：
   - 根据 `target_id` 查询 `SegmentDesc`，获取远程 `BufferDesc`
   - 调用 `selectDevice()` 选择最优 RDMA 设备（基于拓扑和负载均衡）
   - 从 `BufferDesc` 中提取 `rkey`（根据设备索引）
   - 填充 `ibv_send_wr` 结构：
     ```cpp
     wr.wr.rdma.remote_addr = buffer_desc.addr + request.target_offset;
     wr.wr.rdma.rkey = buffer_desc.rkey[device_id];
     ```
   - 调用 `ibv_post_send()` 提交到 RDMA 网卡

4. **答案**：RDMA 性能低于 TCP 的可能原因：
   - **内存未注册**：每次传输都需要临时注册（性能杀手）
   - **小包传输**：RDMA 固定开销（如 QP 建立）大于 TCP，小包不划算
   - **拓扑错误**：跨 NUMA 节点或跨 PCIe 根复杂度的传输
   - **网卡拥塞**：RDMA QP 深度不足，导致流水线停顿
   - **诊断方法**：使用 `perf stat` 监控 `ibv_post_send()` 延迟、检查 `/sys/class/infiniband/` 统计信息、使用 `rperf` 工具测试基准带宽

---

## 总结

本讲义介绍了 Mooncake Transfer Engine 的四个核心概念：

1. **Segment 抽象**：通过 `SegmentDesc` 统一表示远程内存区域，支持 RDMA、TCP、NVLink 等多协议
2. **内存注册**：通过 `registerLocalMemory()` 启用 RDMA 零拷贝，生成 lkey/rkey 供网卡直接访问
3. **批量传输**：通过 `allocateBatchID()` 和 `submitTransfer()` 打包多个请求，减少系统调用和网卡轮询开销
4. **零拷贝机制**：网卡直接 DMA 访问用户内存，绕过内核，实现数十倍性能提升

这些概念共同构成了 Mooncake 的高性能传输基础，为后续的 Mooncake Store 和 EP（Expert Parallelism）提供了底层支撑。

**下一步学习**：Unit 2 - Lesson 2（Transfer Engine 高级特性：通知机制、拓扑感知、多协议支持）
