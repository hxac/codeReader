# RDMA Transport 深入

## 1. 本讲目标

上一讲（`u3-l1`）我们学习了 `Transport` 基类与 `Slice / Task / Batch` 对象模型——那是所有协议共享的"骨架"。本讲我们要钻进 Mooncake 最重要、也最复杂的一种具体实现：**RDMA Transport**。

RDMA（Remote Direct Memory Access，远程直接内存访问）让一台机器的网卡可以**绕过远端 CPU 和操作系统**，直接读写另一台机器上的内存。它是 KV Cache 高速搬运的首选链路。本讲学完后，你应该能够：

1. 说清 `RdmaTransport → RdmaContext → RdmaEndPoint` 这三层的职责分工，理解"一块 NIC 对应一个 Context、一条逻辑连接对应一个 EndPoint"的组织方式；
2. 解释 QP（Queue Pair）从 `RESET → INIT → RTR → RTS` 的建连状态机，以及每块内存为什么要"注册"（Memory Region）并产生 `lkey / rkey`；
3. 掌握一条 `WRITE` 操作从 `submitTransfer` 到 `ibv_post_send` 的完整调用链，并指出**失败重试**发生在哪几处；
4. 了解 Worker 池（提交线程 + 轮询线程 + 监控线程）、端点（EndPoint）回收、rail 暂停与 PCI Relaxed Ordering 这些工程机制。

> 本讲只读不写源码，所有引用都来自当前 HEAD `945f3e61`。本讲假定你已读过 `u3-l1`（知道 `Slice`、`TransferTask`、`submitTransferTask` 是什么）。

## 2. 前置知识

在进入源码前，先建立四个直觉。

**直觉一：RDMA 是"网卡直接搬内存"，CPU 只负责下发命令。** 在传统 socket 传输里，数据要经过"用户态 → 内核 → 网卡 → 对端内核 → 对端用户态"多次拷贝和 CPU 介入。RDMA 把这些省掉了：本地 CPU 只是把一段"工作请求（Work Request, WR）"塞进发送队列，剩下的搬运由网卡硬件完成，对端 CPU 完全不参与。命令进队靠 **QP（Queue Pair）**，搬运完成靠 **CQ（Completion Queue）**。

**直觉二：要让网卡能访问某段内存，必须先"注册"它。** 操作系统默认不允许网卡随意读写进程内存。你必须显式调用 `ibv_reg_mr`，把一段地址"钉住（pin）"并登记给网卡，硬件会返回两个钥匙：

- `lkey`（local key）：**本地**访问这段内存用的钥匙（本机网卡发 WR 时填）；
- `rkey`（remote key）：**远端**访问这段内存用的钥匙（要交给对端，对端网卡发 RDMA READ/WRITE 时填）。

所以"远端能读写我的内存"的前提是：我把内存注册得到 `rkey`，并经元数据（metadata）告诉对端"我的这段地址 + 这个 rkey"。

**直觉三：建连不是 TCP 那样 connect 一下就完，而是要交换 QP 信息。** RDMA 的 RC（Reliable Connection）模式要求两端互相知道对方的 QP 号、GID、LID 等"地址"，才能把各自的 QP 修改到 `RTR`（Ready to Receive）/ `RTS`（Ready to Send）状态。Mooncake 用一个**带外的 RPC handshake** 来交换这些信息。

**直觉四：一块机器可能有多块 RDMA 网卡（RNIC），一张网卡又能连很多对端。** 所以需要一个层级：每块 NIC 一个 `RdmaContext`（持有 PD、CQ、MR、worker 线程），每条"本机 NIC ↔ 对端 NIC"的逻辑连接一个 `RdmaEndPoint`（持有一组 QP）。

如果你对 `Slice`、`submitTransferTask`、metadata（`SegmentDesc`/`BufferDesc`）还不熟，建议先读 `u3-l1` 和 `u2-l2`。本讲聚焦"RDMA 这些资源是怎么被组织、建连、下发和回收的"。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `mooncake-transfer-engine/include/transport/rdma_transport/rdma_transport.h` | `RdmaTransport` 类声明。它持有 `context_list_`（多块 NIC）和 `local_topology_`，对外实现 `install / submitTransfer / registerLocalMemory` 等。 |
| `mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp` | `RdmaTransport` 实现。本讲重点：`install`、`initializeRdmaResources`、`registerLocalMemoryInternal`（relaxed ordering + 并行注册）、`submitTransferTask`（切片 + 解析 lkey）、`selectDevice`（按拓扑选 NIC）。 |
| `mooncake-transfer-engine/include/transport/rdma_transport/rdma_context.h` | `RdmaContext` 类声明。代表"一块 NIC 的全部资源"：`ibv_context / pd_ / cq_list_ / memory_region_map_ / endpoint_store_ / worker_pool_`。 |
| `mooncake-transfer-engine/src/transport/rdma_transport/rdma_context.cpp` | `RdmaContext` 实现。重点：`construct`（打开设备、建 PD/CQ/worker）、`registerMemoryRegionInternal`（`ibv_reg_mr`，GPU 走 dmabuf）、`rkey/lkey`、`submitPostSend`（转交给 worker 池）。 |
| `mooncake-transfer-engine/include/transport/rdma_transport/rdma_endpoint.h` | `RdmaEndPoint` 类声明。代表"本机 NIC ↔ 某对端 NIC"的一组 QP，含建连状态机枚举和 `submitPostSend`。 |
| `mooncake-transfer-engine/src/transport/rdma_transport/rdma_endpoint.cpp` | `RdmaEndPoint` 实现。重点：`construct`（`ibv_create_qp`）、`doSetupConnection`（QP 状态机 RESET→INIT→RTR→RTS）、`submitPostSend`（构造 `ibv_send_wr` 并 `ibv_post_send`）。 |
| `mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp` | **Worker 池**。重点：`submitPostSend`（解析 rkey + 选路 + 分片入队）、`performPostSend`（建连 + post send）、`performPollCq`（收完成 + 失败处理）、`redispatch`（重试）、`monitorWorker`（异步事件 + 端点回收）。 |
| `mooncake-transfer-engine/include/transport/transport.h` | `Transport` 基类与 `Slice`（含 `rdma` union 分支）。本讲引用其中的 `Slice.rdma` 字段。 |
| `mooncake-transfer-engine/include/topology.h` | `Topology` 类，`selectDevice` 按 NUMA/位置在多块 NIC 里选一块。 |
| `mooncake-transfer-engine/include/transfer_metadata.h` | `BufferDesc`（含 `lkey` / `rkey` 向量）与 `HandShakeDesc`（建连交换信息）。 |
| `mooncake-transfer-engine/benchmark/main.cpp` | 传输基准工具 `tebench`，本讲综合实践的运行入口。 |

## 4. 核心概念与源码讲解

我们按"由外到内、由资源到流程"的顺序，拆成五个最小模块：

- 4.1 `RdmaTransport`：多块 NIC 的总入口与初始化
- 4.2 `RdmaContext`：一块 NIC 的资源容器（PD/CQ/MR/WorkerPool/EndpointStore）
- 4.3 内存注册与 `lkey` / `rkey`
- 4.4 QP 与 `RdmaEndPoint`：建连状态机与 post send
- 4.5 Worker 池：提交、轮询、重试、端点回收与 relaxed ordering

---

### 4.1 RdmaTransport：多块 NIC 的总入口与初始化

#### 4.1.1 概念说明

`RdmaTransport` 是 `Transport` 基类的 RDMA 具体实现（`getName()` 返回 `"rdma"`）。它对应"本机所有 RDMA 网卡"这个整体：

- 持有 `context_list_`——每块可用 RNIC 对应一个 `RdmaContext`；
- 持有 `local_topology_`——描述"哪种内存位置应该用哪块 NIC"，用于选路；
- 负责 `install`（初始化）、`registerLocalMemory`（向所有 Context 注册同一段内存）、`submitTransferTask`（切片 + 选设备 + 下发）。

理解它的关键是：**它本身不做硬件操作，而是把工作分派给 `context_list_` 里的各个 `RdmaContext`**。一段内存会在每块 NIC 上都注册一份（从而得到每块 NIC 各自的 `lkey/rkey`），传输时根据拓扑选一块 NIC 走。

#### 4.1.2 核心流程

`RdmaTransport` 的初始化（`install`）流程：

```
install(server_name, metadata, topology)
   ↓
解析 rdma_server_name_（双 NIC 环境下区分 TCP/RDMA 地址）
   ↓
initializeRdmaResources()   // 遍历拓扑里的每块 HCA，建一个 RdmaContext
   ↓
allocateLocalSegmentID()    // 在 metadata 里登记本机 segment + 设备描述
   ↓
startHandshakeDaemon()      // 启动建连 RPC 服务端，回调 onSetupRdmaConnections
   ↓
metadata_->updateLocalSegmentDesc()  // 把 segment 发布给其他节点
```

传输提交（`submitTransferTask`，上一讲 4.4 已讲过切片部分）的 RDMA 特化在于：切出每个 `Slice` 后，要为它**选定一块本地 NIC（device_id）**，并把对应 NIC 注册得到的 `lkey` 填进 `slice->rdma.source_lkey`，再交给该 `RdmaContext` 下发。

#### 4.1.3 源码精读

`install` 是总入口，按顺序完成五件事：

[`mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp:94-147`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp#L94-L147) — `RdmaTransport::install`，依次初始化资源、分配 segment、启动 handshake daemon、发布 metadata。

注意它对双 NIC 的处理：当 `MC_RDMA_BIND_ADDRESS` 被设置时，`local_server_name_`（用于 TCP/P2P）和 `rdma_server_name_`（用于构造 NIC path）会不同：

```cpp
const char *rdma_bind_addr = std::getenv("MC_RDMA_BIND_ADDRESS");
if (rdma_bind_addr && rdma_bind_addr[0] != '\0') {
    auto [host_name, port] = parseHostNameWithPort(local_server_name);
    rdma_server_name_ = std::string(rdma_bind_addr) + ":" + std::to_string(port);
} else {
    rdma_server_name_ = local_server_name_;
}
```

`initializeRdmaResources` 遍历拓扑里的每块 HCA（Host Channel Adapter，即 RNIC），逐个构造 `RdmaContext`，构造失败的就禁用：

[`mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp:708-729`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp#L708-L729) — `initializeRdmaResources`：对拓扑里的每块 HCA 建 `RdmaContext`，失败则 `disableDevice`。

```cpp
auto hca_list = local_topology_->getHcaList();
for (auto &device_name : hca_list) {
    auto context = std::make_shared<RdmaContext>(*this, device_name);
    auto &config = globalConfig();
    int ret = context->construct(config.num_cq_per_ctx,
                                 config.num_comp_channels_per_ctx,
                                 config.port, config.gid_index,
                                 config.max_cqe, config.max_ep_per_ctx);
    if (ret) {
        local_topology_->disableDevice(device_name);  // 这块 NIC 不可用
    } else {
        context_list_.push_back(context);
    }
}
```

`selectDevice` 是选路的核心：给定目标地址偏移和内存位置（NUMA），从拓扑里选一块合适的本地 NIC。它的参数 `retry_count` 会在重试时用来切换到**备选 NIC**（round-robin），这是 4.5 重试机制的关键：

[`mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp:741-777`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp#L741-L777) — `selectDevice`：在 `buffer_id`（哪段注册内存）确定后，调用 `topology.selectDevice(location, retry_count)` 选 `device_id`（哪块 NIC）。

```cpp
device_id = hint.empty()
                ? desc->topology.selectDevice(location, retry_count)
                : desc->topology.selectDevice(location, hint, retry_count);
if (device_id >= 0) return 0;
// preferred 选不到，退回通配位置再选一次
device_id = ... desc->topology.selectDevice(kWildcardLocation, retry_count) ...;
```

#### 4.1.4 代码实践

**实践目标**：用 grep 确认 `RdmaTransport` 把哪些活"外包"给了 `RdmaContext`，体会"Transport 是协调者、Context 是执行者"。

**操作步骤**：

1. 在 `rdma_transport.cpp` 里搜索 `context_list_` 的所有使用点。
2. 把它们分成三类：初始化（`initializeRdmaResources` 里 push）、内存注册（`registerLocalMemoryInternal` 里对每个 context 调 `registerMemoryRegion`）、传输（`submitTransferTask` 里按 `device_id` 取 context）。

**需要观察的现象**：`RdmaTransport` 几乎不直接碰 verbs API（`ibv_*`），所有 `ibv_*` 调用都在 `rdma_context.cpp` / `rdma_endpoint.cpp` 里。这正是分层的好处：上层只管"有几块 NIC、怎么选路"。

**预期结果**：你会看到 `context_list_` 是 `vector<shared_ptr<RdmaContext>>`，传输时通过 `context_list_[device_id]` 拿到对应 NIC 的 context 再下发。

> 待本地验证：可用 Grep 工具或 `rg "context_list_\[" mooncake-transfer-engine/src/transport/rdma_transport/` 查看索引访问点。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `install` 在 `initializeRdmaResources` 之后还要 `startHandshakeDaemon`？能不能反过来——先启动 daemon？

**参考答案**：不能。handshake daemon 被回调时会用到 `context_list_`（在 `onSetupRdmaConnections` 里按对端 NIC 名找到本地 context、再取 endpoint）。如果 daemon 先启动但 context 还没建好，对端发来的建连请求会因为找不到 context 而失败。顺序是"先把本地资源准备好，再对外提供服务"。

**练习 2**：`initializeRdmaResources` 里某块 NIC `construct` 失败时调用 `disableDevice`，而不是直接报错返回。这样设计有什么好处？

**参考答案**：容错降级。一台机器可能有多块 RNIC，其中一块出问题（端口未激活、驱动异常等）不应让整个 Transport 启动失败。把它从拓扑里禁用后，后续 `selectDevice` 自然不会再选它，剩下可用的 NIC 继续工作。只有当**所有** NIC 都不可用（`local_topology_->empty()`）时才返回 `ERR_DEVICE_NOT_FOUND`。

---

### 4.2 RdmaContext：一块 NIC 的资源容器

#### 4.2.1 概念说明

`RdmaContext` 代表"受一块本地 NIC 控制的全部资源"。源码头文件的注释一句话点题：

> RdmaContext represents the set of resources controlled by each local NIC, including Memory Region, CQ, EndPoint (QPs), etc.

它聚合了 verbs 编程里的核心对象：

| 字段 | verbs 对象 | 作用 |
| --- | --- | --- |
| `context_` | `ibv_context*` | 打开的设备上下文（`ibv_open_device`） |
| `pd_` | `ibv_pd*` | Protection Domain，MR/QP/AH 都要在同一个 PD 下 |
| `cq_list_` | `ibv_cq*`（多个） | Completion Queue，搬运完成事件队列 |
| `comp_channel_` | `ibv_comp_channel*` | 完成通道（事件通知） |
| `memory_region_map_` | `ibv_mr*`（map） | 已注册的内存区，按地址索引 |
| `endpoint_store_` | `EndpointStore` | 该 NIC 对各对端的 EndPoint 集合 |
| `worker_pool_` | `WorkerPool` | 后台 worker 线程（提交 + 轮询 + 监控） |

一句话：**`RdmaContext` = 一块网卡能干活需要的全部"上下文"**。`context_list_` 里每个 context 都独立持有这些，彼此隔离。

#### 4.2.2 核心流程

`RdmaContext::construct` 的初始化是一条典型的 verbs 资源搭建流水线：

```
openRdmaDevice()           // ibv_open_device + 选 GID + 查端口
   ↓
ibv_alloc_pd()             // 分配 Protection Domain
   ↓
ibv_create_comp_channel() // 每个完成通道（可多个）
   ↓
epoll_create1 + 注册 async_fd / comp_channel fd  // 统一事件监听
   ↓
ibv_create_cq() × num_cq   // 建若干 CQ（轮询/事件复用）
   ↓
WorkerPool(*this, socketId())  // 启动 worker 线程（绑到该 NIC 的 NUMA 节点）
```

析构（`deconstruct`）则严格**逆序释放**：先停 worker_pool → 销毁 QP → `ibv_dereg_mr` → `ibv_destroy_cq` → 关 epoll/comp_channel → `ibv_dealloc_pd` → `ibv_close_device`。这个顺序很重要：QP 必须先于 CQ 销毁（QP 依赖 CQ），CQ 必须先于 PD 销毁。

#### 4.2.3 源码精读

`construct` 里 CQ 的创建有个细节——每个 CQ 的 `cq_context`（verbs 里的用户自定义指针）被设成了该 CQ 的 `outstanding`（未决 WR 计数）地址：

[`mooncake-transfer-engine/src/transport/rdma_transport/rdma_context.cpp:244-257`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_context.cpp#L244-L257) — 建 CQ，把 `&cq_list_[i].outstanding` 作为 `cq_context` 传入。

```cpp
for (size_t i = 0; i < num_cq_list; ++i) {
    auto cq = ibv_create_cq(context_, max_cqe,
                            (void *)&cq_list_[i].outstanding /* CQ context */,
                            compChannel(), compVector());
    ...
    cq_list_[i].native = cq;
}
```

这个 `outstanding` 计数器随后被 EndPoint 通过 `cq->cq_context` 反查到（见 4.4.3），用来做"发送队列深度"的反压——这是 4.4 post send 流控的关键。

CQ、comp_channel 都支持多个，并通过轮询下标**均匀分配**，避免单个 CQ 成为瓶颈：

[`mooncake-transfer-engine/src/transport/rdma_transport/rdma_context.cpp:686-698`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_context.cpp#L686-L698) — `cq() / compChannel() / compVector()` 用自增原子下标做 round-robin。

```cpp
ibv_cq *RdmaContext::cq() {
    int index = (next_cq_list_index_++) % cq_list_.size();
    return cq_list_[index].native;
}
```

`WorkerPool` 在 construct 末尾创建，并传入 `socketId()`——后者从 sysfs 读这块 NIC 的 NUMA 节点，让 worker 线程绑到正确的 CPU socket，减少跨 NUMA 访问：

[`mooncake-transfer-engine/src/transport/rdma_transport/rdma_context.cpp:259`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_context.cpp#L259) — `worker_pool_ = std::make_shared<WorkerPool>(*this, socketId());`

`endpoint(peer_nic_path)` 是取/建 EndPoint 的入口，实际委托给 `endpoint_store_`：

[`mooncake-transfer-engine/src/transport/rdma_transport/rdma_context.cpp:617-637`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_context.cpp#L617-L637) — `RdmaContext::endpoint`：先查 store，没有就插入新建并触发回收。

```cpp
auto endpoint = endpoint_store_->getEndpoint(peer_nic_path);
if (endpoint) return endpoint;
endpoint = endpoint_store_->insertEndpoint(peer_nic_path, this);
endpoint_store_->reclaimEndpoint();   // 顺带回收不活跃的 endpoint
return endpoint;
```

最后，`submitPostSend` 几乎是个透传——把 slice 列表交给 worker 池：

[`mooncake-transfer-engine/src/transport/rdma_transport/rdma_context.cpp:1207-1210`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_context.cpp#L1207-L1210) — `RdmaContext::submitPostSend` 转发给 `worker_pool_`。

```cpp
int RdmaContext::submitPostSend(const std::vector<Transport::Slice *> &slice_list) {
    return worker_pool_->submitPostSend(slice_list);
}
```

这正是 4.5 调用链的关键一跳：Context 不亲自 post send，而是入队交给 worker 异步处理。

#### 4.2.4 代码实践

**实践目标**：观察 `RdmaContext` 的"多 CQ / 多 comp channel"配置如何被环境变量控制。

**操作步骤**：

1. 读 `config.h` 第 36–50 行附近的默认值：`num_cq_per_ctx`、`num_comp_channels_per_ctx`、`max_cqe`、`num_qp_per_ep`、`max_wr`、`workers_per_ctx`。
2. 在 `config.cpp` 里找到对应的环境变量名（`MC_NUM_CQ_PER_CTX`、`MC_NUM_QP_PER_EP`、`MC_MAX_WR`、`MC_WORKERS_PER_CTX` 等）。
3. 思考：把 `MC_WORKERS_PER_CTX` 从默认 2 调到 8，会让单个 NIC 的轮询能力提升还是下降？

**需要观察的现象**：默认每块 NIC 只有 1 个 CQ、2 个 QP/EndPoint、2 个 worker。worker 数 ≥ CQ 数时，多个 worker 会**分担轮询同一个 CQ**（见 4.5 `performPollCq` 的 `cq_index += kTransferWorkerCount` 步进）。

**预期结果**：你会理解"多 worker 共享少量 CQ"是 Mooncake 的默认并发模型——增加 worker 主要提升"切片解析 + post send"的并行度，而 CQ 数控制的是硬件完成队列的并行度。

> 待本地验证：默认值以本地 `config.h` / `config.cpp` 为准，不同版本可能有差异。

#### 4.2.5 小练习与答案

**练习 1**：`RdmaContext::construct` 里，每个 CQ 的 `cq_context` 为什么故意设成 `&cq_list_[i].outstanding` 而不是 `nullptr`？

**参考答案**：因为 verbs 在很多回调/对象里只回传 `cq_context` 这一个用户指针。EndPoint 构造时通过 `cq->cq_context` 把这个指针取出来存到 `cq_outstanding_`（见 4.4.3），从而在 post send / 销毁时能原子增减"该 CQ 上未决的 WR 数"，做反压和正确释放。如果设成 `nullptr`，就丢了这条"从 CQ 反查计数器"的链路。

**练习 2**：`RdmaContext::deconstruct` 为什么要先 `worker_pool_.reset()`，再去销毁 QP 和 MR？

**参考答案**：因为 worker 线程会持续 poll CQ、访问 endpoint 和 slice。如果不先停 worker，在销毁 QP/MR 的过程中 worker 可能正好在处理这些对象的完成事件，导致 use-after-free。先 `reset()` 让 worker 线程退出并 `join`，保证此后没有任何线程会碰这些 RDMA 资源，再安全销毁。

---

### 4.3 内存注册与 lkey / rkey

#### 4.3.1 概念说明

本节回答本讲学习目标里的核心问题：**rkey / lkey 在远端访问中到底起什么作用？**

- 一段本地内存要能被 RDMA 访问，必须先注册成 **Memory Region（MR）**。注册会"钉住"物理页（防止被 swap/迁移）并让网卡记录"这段虚拟地址 → 物理页"的映射。
- 注册成功后硬件返回两个 32 位钥匙：
  - **`lkey`**：本机自己访问该 MR 时用（例如本地发起 RDMA WRITE，源端的 `sge.lkey` 填它，告诉本地网卡"我有权读这段源内存"）。
  - **`rkey`**：交给对端用（对端发起 RDMA READ/WRITE 时，`wr.rdma.rkey` 填它，告诉对端网卡"我有权访问目的端的这段内存"）。

所以一次 RDMA WRITE 需要两把钥匙同时在场：**源端的 lkey（证明本地可读）+ 目的端的 rkey（证明远端可写）**。`lkey` 由本机 `registerLocalMemory` 产生，`rkey` 由对端 `registerLocalMemory` 产生、再通过 metadata 交换到本机。

Mooncake 的多 NIC 设计意味着：同一段内存会在每块 NIC 上各注册一次，得到**每块 NIC 各自的 `lkey`/`rkey`**。所以 `BufferDesc` 里 `lkey` 和 `rkey` 都是 `vector<uint32_t>`，按下标对应 `context_list_` 的 device：

[`mooncake-transfer-engine/include/transfer_metadata.h:52-65`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_metadata.h#L52-L65) — `BufferDesc`，`lkey`/`rkey` 是按 device 索引的向量。

```cpp
struct BufferDesc {
    std::string name;
    uint64_t addr;
    uint64_t length;
    std::vector<uint32_t> lkey;   // for rdma，每块 NIC 一个
    std::vector<uint32_t> rkey;   // for rdma，每块 NIC 一个
    ...
};
```

#### 4.3.2 核心流程

注册一段内存的流程（`RdmaTransport::registerLocalMemoryInternal`）：

```
确定 access_rights（LOCAL_WRITE | REMOTE_WRITE | REMOTE_READ [+ RELAXED_ORDERING]）
   ↓
（大块内存）preTouchMemory 并行预触页，加速注册
   ↓
对每个 context（每块 NIC）调 registerMemoryRegion(addr, length, access)
   ↓          ↳ ibv_reg_mr(pd, addr, length, access)  →  得到 mr->lkey / mr->rkey
收集每个 context 的 lkey(addr) / rkey(addr)，拼成 buffer_desc.lkey/rkey 向量
   ↓
metadata_->addLocalMemoryBuffer(buffer_desc)  // 发布，含 rkey，供对端使用
```

读取钥匙则用 `RdmaContext::lkey(addr)` / `rkey(addr)`：在 `memory_region_map_`（按起始地址排序的 map）里二分查找覆盖该地址的 MR，返回其 key。

#### 4.3.3 源码精读

`access_rights` 的组装体现了 **PCI Relaxed Ordering** 机制。本机基础权限是本地写 + 远端写 + 远端读；若启用了 relaxed ordering 再追加 `IBV_ACCESS_RELAXED_ORDERING`：

[`mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp:205-213`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp#L205-L213) — 组装 `access_rights`，按 relaxed ordering 开关追加标志。

```cpp
const int kBaseAccessRights = IBV_ACCESS_LOCAL_WRITE |
                              IBV_ACCESS_REMOTE_WRITE |
                              IBV_ACCESS_REMOTE_READ;
int access_rights = kBaseAccessRights;
if (MCIbRelaxedOrderingEnabled) {
    access_rights |= IBV_ACCESS_RELAXED_ORDERING;
}
```

> **什么是 Relaxed Ordering？** RDMA 读操作默认会让 CPU 侧的读操作严格排在它后面（强序），这对一些 PCIe 拓扑会损失吞吐。开启 `IBV_ACCESS_RELAXED_ORDERING` 允许硬件对这些读放宽排序约束，提升带宽——它需要 IBVERBS ≥ 1.8 的 `ibv_reg_mr_iova2` 符号支持。

开关本身在 `RdmaTransport` 构造函数里决定：读 `MC_IB_PCI_RELAXED_ORDERING` 配置（0 关 / 1 支持则开 / 2 自动），并用 `dlsym` 探测 `ibv_reg_mr_iova2` 是否存在：

[`mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp:57-82`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp#L57-L82) — `has_ibv_reg_mr_iova2` 用 `dlsym` 探测符号；构造函数据此置 `MCIbRelaxedOrderingEnabled`。

```cpp
bool has_ibv_reg_mr_iova2(void) {
    void *sym = dlsym(RTLD_DEFAULT, "ibv_reg_mr_iova2");
    return sym != NULL;
}
```

注册大内存时有个**并行预触页 + 并行注册**的优化。注册前的页缺失（page fault）很慢，所以先用多线程把页都摸一遍（`preTouchMemory`），再决定是否并行注册（多块 NIC 时并行更快）：

[`mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp:244-278`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp#L244-L278) — 并行注册分支：每块 NIC 一个线程并发 `registerMemoryRegion`。

```cpp
if (use_parallel_reg) {
    for (size_t i = 0; i < context_list_.size(); ++i) {
        reg_threads.emplace_back([this, &ret_codes, i, addr, length, ar]() {
            ret_codes[i] = context_list_[i]->registerMemoryRegion(addr, length, ar);
        });
    }
    ...
}
```

真正调用 verbs 的地方在 `RdmaContext::registerMemoryRegionInternal`。CPU 内存走 `ibv_reg_mr`；GPU 显存（CUDA/HIP）走 `ibv_reg_dmabuf_mr`，避免依赖 `nvidia-peermem` 内核模块：

[`mooncake-transfer-engine/src/transport/rdma_transport/rdma_context.cpp:531-539`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_context.cpp#L531-L539) — CPU 内存的最简注册路径：`ibv_reg_mr(pd_, addr, length, access)`，失败返回 `ERR_CONTEXT`。

```cpp
#else
    mrMeta.addr = addr;
    mrMeta.mr = ibv_reg_mr(pd_, addr, length, access);
#endif
    if (!mrMeta.mr) {
        PLOG(ERROR) << "Failed to register memory " << addr;
        return ERR_CONTEXT;
    }
```

读取钥匙的二分查找（`findMemoryRegionContaining`）：

[`mooncake-transfer-engine/src/transport/rdma_transport/rdma_context.cpp:577-593`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_context.cpp#L577-L593) — `rkey` / `lkey`：读锁下二分找覆盖该地址的 MR。

```cpp
uint32_t RdmaContext::rkey(void *addr) {
    RWSpinlock::ReadGuard guard(memory_regions_lock_);
    auto iter = findMemoryRegionContaining(reinterpret_cast<uintptr_t>(addr));
    if (iter != memory_region_map_.end()) return iter->second.mr->rkey;
    ...
}
```

#### 4.3.4 代码实践

**实践目标**：跟踪 `rkey` 从"产生"到"被远端使用"的完整链路，理解为什么它是 `vector`。

**操作步骤**：

1. 读 `rdma_transport.cpp:295-298`——注册后从每个 context 收集 `lkey(addr)` / `rkey(addr)`，`push_back` 进向量。
2. 读 `worker_pool.cpp:144-145`——传输时按选定的 `device_id` 取 `peer_segment_desc->buffers[buffer_id].rkey[device_id]` 填进 `slice->rdma.dest_rkey`。
3. 读 `rdma_endpoint.cpp:771-772`——`ibv_post_send` 时 `wr.wr.rdma.rkey = slice->rdma.dest_rkey`。
4. 思考：为什么 `lkey` 用"本机选定 NIC 的 device_id"下标，而 `rkey` 用"对端选定 NIC 的 device_id"下标？

**需要观察的现象**：`dest_rkey` 来自**对端** segment 的 `BufferDesc.rkey[device_id]`，而 `source_lkey` 来自**本机** segment 的 `BufferDesc.lkey[device_id]`。两个 device_id 是各自独立选路的结果，可能不同。

**预期结果**：你会画出"`rkey` 在对端 `ibv_reg_mr` 产生 → 经 metadata 发布 → 本机按对端 device_id 取出 → 填进 WR"的闭环，并理解"每块 NIC 一把钥匙"是支持多 NIC 选路的前提。

> 待本地验证：可在 `WorkerPool::submitPostSend` 里临时 `LOG(INFO) << "rkey=" << slice->rdma.dest_rkey;`，对照两台机器各自的注册日志确认钥匙一致（仅作观察，勿提交）。

#### 4.3.5 小练习与答案

**练习 1**：如果我注册了一段内存但忘记把它的 `rkey` 发布到 metadata，对端发起 RDMA WRITE 会怎样？

**参考答案**：对端根本拿不到合法的 `rkey`。在 `WorkerPool::submitPostSend` 里，`selectDevice` 找不到对端 segment 的 buffer 描述（或 `rkey` 向量为空），slice 会被 `markFailed`。即使侥幸填了一个错误 rkey 下发，网卡硬件也会因为钥匙校验失败返回一个带错误状态的 WC（Work Completion），在 `performPollCq` 里被当作路径失败处理（见 4.5）。

**练习 2**：`ibv_reg_mr` 为什么要"钉住"内存（pin）？relaxed ordering 会改变这一点吗？

**参考答案**：因为 RDMA 搬运时 CPU 不参与，网卡直接用注册时记录的虚拟→物理映射去 DMA。如果操作系统中途把页换出或迁移（改变物理地址），网卡就会 DMA 到错误的地方。钉住就是禁止这种移动。Relaxed Ordering 只放宽"读操作的排序约束"，**不改变**页被钉住的事实——内存依旧被 pin，钥匙依旧有效。

---

### 4.4 QP 与 RdmaEndPoint：建连状态机与 post send

#### 4.4.1 概念说明

`RdmaEndPoint` 代表"本机某块 NIC（由 `RdmaContext` 标识）到对端某块 NIC（由 `peer_nic_path` 标识）之间的全部 QP 连接"。源码注释点明了它的生命周期：

> 1. 构造后，资源已分配但还没指定对端；
> 2. 需要和对端的 EndPoint 交换握手信息（active 端 RPC、passive 端在 RPC 服务里处理），交换后状态变 `CONNECTED`；
> 3. 用户主动 `disconnect` 或内部检测到错误时，连接关闭、状态回 `UNCONNECTED`，可重新握手。

它持有一组 QP（`qp_list_`，默认 2 个，由 `num_qp_per_ep` 控制）和每 QP 的发送队列深度计数（`wr_depth_list_`）。QP 数量决定单条逻辑连接的并发度——多个 QP 可以让硬件并行处理更多在途 WR。

#### 4.4.2 核心流程

**建连状态机**（RC QP 的标准流程）由 `doSetupConnection` 驱动，对每个 QP 执行：

```
任意状态 ──RESET──▶ INIT   (设置端口、pkey、访问权限)
   INIT    ──▶ RTR         (Ready To Receive: 设 MTU、对端 GID/LID/QPN)
   RTR     ──▶ RTS         (Ready To Send:    设 timeout、retry_cnt、PSN)
```

- `RESET → INIT`：本地配置（端口、pkey_index、access_flags）。
- `INIT → RTR`：填入**对端**信息（对端 GID、LID、QP 号），决定路径 MTU、收端 PSN。这一步要"知道对方是谁"。
- `RTR → RTS`：填入重试参数（`timeout`、`retry_cnt`、`rnr_retry`），之后才能发。

握手信息（QP 号、GID、LID）通过 `HandShakeDesc` 在两端交换：active 端调 `setupConnectionsByActive`（发起 RPC），passive 端在 `onSetupRdmaConnections` 回调里调 `setupConnectionsByPassive`。

#### 4.4.3 源码精读

QP 在 `RdmaEndPoint::construct` 里创建，类型是可靠的 `IBV_QPT_RC`，并记录 `cq_outstanding_`（来自 4.2 讲的 CQ context 指针）：

[`mooncake-transfer-engine/src/transport/rdma_transport/rdma_endpoint.cpp:84-125`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_endpoint.cpp#L84-L125) — `construct`：为每个 QP 调 `ibv_create_qp`，记录深度计数与 CQ 未决计数指针。

```cpp
cq_outstanding_ = (volatile int *)cq->cq_context;   // 反查 CQ 未决计数
...
for (size_t i = 0; i < num_qp_list; ++i) {
    wr_depth_list_[i] = 0;
    ibv_qp_init_attr attr;
    attr.send_cq = cq; attr.recv_cq = cq;
    attr.qp_type = IBV_QPT_RC;                       // 可靠连接
    attr.cap.max_send_wr = attr.cap.max_recv_wr = max_wr_depth;
    attr.cap.max_send_sge = attr.cap.max_recv_sge = max_sge_per_wr;
    qp_list_[i] = ibv_create_qp(context_.pd(), &attr);
}
```

状态机的核心三段（`doSetupConnection` 单 QP 版本）。`RESET → INIT`：

[`mooncake-transfer-engine/src/transport/rdma_transport/rdma_endpoint.cpp:920-940`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_endpoint.cpp#L920-L940) — `INIT` 阶段，设置本地端口与访问权限。

`INIT → RTR`（填对端信息，注意 `dest_qp_num`/`dgid`/`dlid` 都来自握手）：

[`mooncake-transfer-engine/src/transport/rdma_transport/rdma_endpoint.cpp:942-981`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_endpoint.cpp#L942-L981) — `RTR` 阶段，写入对端 GID/LID/QP 号、路径 MTU、收端参数。

```cpp
attr.ah_attr.grh.dgid = peer_gid;          // 对端 GID
attr.ah_attr.dlid = peer_lid;              // 对端 LID
attr.dest_qp_num = peer_qp_num;            // 对端 QP 号
attr.path_mtu = context_.activeMTU();
```

`RTR → RTS`（写入重试参数）：

[`mooncake-transfer-engine/src/transport/rdma_transport/rdma_endpoint.cpp:983-1004`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_endpoint.cpp#L983-L1004) — `RTS` 阶段，设置硬件级重试（`timeout=14`、`retry_cnt=7`、`rnr_retry=7`）。

```cpp
attr.timeout = kTimeout;        // 14
attr.retry_cnt = kRetryCount;   // 7  —— 注意：这是 NIC 硬件在丢包时的自动重试
attr.rnr_retry = 7;             // RNR (Receiver Not Ready) 重试
```

> 区分两种"重试"：这里的 `kRetryCount=7` 是 **NIC 硬件层**对丢包/RNR 的自动重传，对软件透明；而 4.5 讲的 `shouldRetrySlice` / `redispatch` 是**软件层**在硬件彻底放弃（WC 报错）后，换一条路径（甚至换 NIC）重新发送。两者层级不同。

post send 的核心在 `RdmaEndPoint::submitPostSend`。它把 slice 列表均匀分摊到多个 QP，受三重容量约束（QP 深度、CQ 容量、slice 数），构造 `ibv_sge` + `ibv_send_wr` 后 `ibv_post_send`：

[`mooncake-transfer-engine/src/transport/rdma_transport/rdma_endpoint.cpp:719-802`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_endpoint.cpp#L719-L802) — `submitPostSend`：分摊到各 QP、构造 WR、`ibv_post_send`，失败 WR 计入 `failed_slice_list`。

关键片段（每个 slice 变成一条 WR）：

```cpp
sge.addr = (uint64_t)slice->source_addr;
sge.length = slice->length;
sge.lkey = slice->rdma.source_lkey;            // 源端 lkey

wr.wr_id = (uint64_t)slice;                    // 完成时靠它找回 slice
wr.opcode = (slice->opcode == READ) ? IBV_WR_RDMA_READ
                                    : IBV_WR_RDMA_WRITE;
wr.send_flags = IBV_SEND_SIGNALED;
wr.wr.rdma.remote_addr = slice->rdma.dest_addr;// 目的地址
wr.wr.rdma.rkey = slice->rdma.dest_rkey;       // 目的端 rkey
...
int rc = ibv_post_send(qp_list_[qp_index], wr_list.data(), &bad_wr);
```

注意三个容量约束的协同：

```cpp
int qp_avail = max_wr_depth_ - wr_depth_list_[qp_index];  // ① 该 QP 队列剩余深度
...
int cq_remaining = int(globalConfig().max_cqe) - *cq_outstanding_;  // ② 该 CQ 剩余容量
...
int wr_count = std::min(assigned_count, qp_avail);
wr_count = std::min(wr_count, cq_remaining);  // ③ 三者取最小
```

如果 `ibv_post_send` 返回非 0（队列满等），`bad_wr` 链表指向第一个失败的 WR，对应的 slice 会被回退深度计数并塞进 `failed_slice_list`（交给 worker 重试，见 4.5）。

#### 4.4.4 代码实践

**实践目标**：动手推演 `submitPostSend` 如何把一组 slice 分摊到多个 QP，并验证容量反压。

**操作步骤**：

1. 假设某 endpoint 有 2 个 QP（`num_qp_per_ep=2`），每个 QP `max_wr_depth=256`，CQ `max_cqe=4096`、当前 `*cq_outstanding_=0`。
2. 现在一次要发 600 个 slice（`requested=600`）。
3. 按 `rdma_endpoint.cpp:738-798` 的循环，手算每个 QP 分到多少、为什么发不完。

**需要观察的现象**：循环把 600 个 slice 在 2 个 QP 间均分，每 QP 分 300；但 `qp_avail = 256 - 0 = 256`，所以每 QP 只能发 256，两 QP 合计发 512，剩 88 个留在 `slice_list` 里（被 `erase` 掉前 512 个后保留）。`total_posted=512`。

**预期结果**：你会看到"QP 深度（256）"比"slice 数（300/QP）"先成为瓶颈，这正是 `wr_depth_list_` 反压的意义——下不进的部分留给 worker 下一轮再发。如果想一次发更多，需调大 `MC_MAX_WR`。

> 这是一个纯推理实践。若想验证，可在 `RdmaEndPoint::submitPostSend` 末尾临时打印 `total_posted` 与 `requested`（仅作观察，勿提交）。

#### 4.4.5 小练习与答案

**练习 1**：`doSetupConnection` 里 `RTR` 阶段为什么必须在对端信息到手之后才能做，而 `INIT` 阶段不需要？

**参考答案**：`INIT` 只配置本地参数（端口、pkey、访问权限），与对端无关，所以可以先做。`RTR`（Ready To Receive）要把"对端的 GID / LID / QP 号"写进 QP 的地址向量（`ah_attr`）和 `dest_qp_num`，告诉硬件"将来从哪个对端收包"——这些信息只能从握手获得，必须等握手完成。这就是为什么建连要走"先握手交换 QP 信息，再 modify QP 到 RTR/RTS"。

**练习 2**：`submitPostSend` 里 `ibv_post_send` 失败后，为什么要把失败 slice 放进 `failed_slice_list` 而不是直接 `markFailed`？

**参考答案**：`ibv_post_send` 失败通常是**暂时性**的（队列满、CQ 满），不是真正的数据错误。直接 `markFailed` 会让用户传输无谓失败。放进 `failed_slice_list` 交还给 `WorkerPool`，后者会走 `redispatch` 在下一轮重试（4.5），等队列腾出空间后成功发出。只有重试次数耗尽才真正 `markFailed`。

---

### 4.5 Worker 池：提交、轮询、重试、端点回收与 relaxed ordering

#### 4.5.1 概念说明

本节把前几节串起来，回答本讲规格指定的两个核心问题：**一次 WRITE 从 `submitTransfer` 到 RDMA post send 的调用链是什么？失败重试发生在何处？**

Mooncake 的 RDMA 传输是**异步**的：`submitTransferTask` 只是把切片"入队"，真正建连、post send、收完成都在**后台 worker 线程**里做。每个 `RdmaContext` 拥有一个 `WorkerPool`，里面有：

- **若干个 transfer worker**（`workers_per_ctx`，默认 2）：循环执行 `performPostSend`（建连 + post send）和 `performPollCq`（收完成）；
- **1 个 monitor worker**：用 epoll 监听 RDMA 异步事件（端口 down、设备致命错误等），并周期性回收不活跃 endpoint。

这套设计让"提交"与"下发"解耦：用户线程 `submitTransfer` 后立即返回，worker 线程异步消化。

#### 4.5.2 核心流程

**完整调用链（WRITE）**：

```
RdmaTransport::submitTransfer              [rdma_transport.cpp:495]
   └─ submitTransferTask                   [rdma_transport.cpp:513]
        ├─ 切片：按 slice_size 切，填 slice->rdma.source_lkey（本机 lkey）
        ├─ selectDevice 选本地 device_id
        └─ context->submitPostSend(slice)  [每批 watermark flush 一次]
             └─ RdmaContext::submitPostSend          [rdma_context.cpp:1207]
                  └─ worker_pool_->submitPostSend     [worker_pool.cpp:60]
                       ├─ 从对端 SegmentDesc 解析 dest_rkey
                       ├─ 选对端 NIC（rail），失败则换备选 rail
                       └─ 按 (target_id, device_id) 分片入队 slice_queue_[shard]

[transfer worker 线程异步消化]
transferWorker                            [worker_pool.cpp:443]
   ├─ performPostSend                     [worker_pool.cpp:208]
   │     ├─ context_.endpoint(peer_nic_path)        取/建 EndPoint
   │     ├─ 未连则 endpoint->setupConnectionsByActive()   建连（4.4 状态机）
   │     └─ endpoint->submitPostSend(...)           [rdma_endpoint.cpp:719]
   │          └─ ibv_post_send                       ★ 真正下发硬件
   └─ performPollCq                       [worker_pool.cpp:323]
        ├─ ibv_poll_cq                              收完成
        ├─ 成功 → slice->markSuccess()
        └─ 失败（非 WR_FLUSH_ERR）→ handlePathFailure + shouldRetrySlice
                                       └─ redispatch（换路径/换 NIC 重发）
```

**失败重试的发生点**有三处：

1. **post send 阶段**（`performPostSend` → `endpoint->submitPostSend`）：建连失败、endpoint 失效、或 `ibv_post_send` 返回失败的 slice，进入 `failed_slice_list`，由 `shouldRetrySlice` 判定后 `redispatch`。
2. **轮询阶段**（`performPollCq`）：WC 状态非 `IBV_WC_SUCCESS`。若是 `WR_FLUSH_ERR`（endpoint 销毁时硬件刷出的错误，不是真网络错）则直接 `markFailed` 不重试；其它错误触发 `handlePathFailure` + 重试。
3. **重试的执行**（`redispatch`）：若 `retry_cnt >= max_retry_cnt` 则 `markFailed`；否则用 `retry_cnt` 调 `selectDevice` **切到备选 NIC**（round-robin），重新解析 rkey 并入队。

#### 4.5.3 源码精读

`WorkerPool` 构造时启动若干 transfer worker + 1 个 monitor worker：

[`mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp:34-50`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp#L34-L50) — 构造函数启动 worker 线程。

```cpp
const static int kTransferWorkerCount = globalConfig().workers_per_ctx;
...
for (int i = 0; i < kTransferWorkerCount; ++i)
    worker_thread_.emplace_back(std::thread(std::bind(&WorkerPool::transferWorker, this, i)));
worker_thread_.emplace_back(std::thread(std::bind(&WorkerPool::monitorWorker, this)));
```

**入队阶段** `WorkerPool::submitPostSend` 做两件大事：解析对端 rkey、选 rail（对端 NIC）。当首选 rail 被暂停时，遍历备选 rail：

[`mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp:144-173`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp#L144-L173) — 取对端 `dest_rkey`，首选 rail 不可用则换备选 device，全部不可用则 `markFailed`。

```cpp
slice->rdma.dest_rkey = peer_segment_desc->buffers[buffer_id].rkey[device_id];
auto peer_nic_path = MakeNicPath(peer_segment_desc->nicPathServerName(),
                                 peer_segment_desc->devices[device_id].name);
if (!isRailAvailable(peer_nic_path)) {           // 首选 rail 被暂停
    for (size_t alt_dev_id = 0; ...) {           // 找一个可用的备选 rail
        if (isRailAvailable(alt_path)) { device_id = alt_dev_id; ...; break; }
    }
    if (!found) { slice->markFailed(); ... }     // 所有 rail 都不可用
}
```

入队按 `shard_id = (target_id * 10007 + device_id) % kShardCount` 分片到 8 个队列，降低锁竞争：

[`mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp:176-189`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp#L176-L189) — 分片入队，按 `peer_nic_path` 聚合。

**post send 阶段** `performPostSend`：worker 把分到自己 shard 的 slice 取出，按 `peer_nic_path` 取 endpoint，必要时建连，再下发：

[`mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp:283-304`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp#L283-L304) — 取 endpoint、未连则 `setupConnectionsByActive`、下发。

```cpp
auto endpoint = context_.endpoint(entry.first);
if (!endpoint->connected() && endpoint->setupConnectionsByActive()) {
    handlePathFailure(entry.first, endpoint.get());   // 建连失败 → 路径失败
    for (auto &slice : entry.second) failed_slice_list.push_back(slice);
    continue;
}
for (auto &slice : entry.second) slice->rdma.endpoint = endpoint.get();
endpoint->submitPostSend(entry.second, failed_slice_list);   // ★ 4.4 的 post send
```

post send 后，`failed_slice_list` 里的 slice 走重试：

[`mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp:307-320`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp#L307-L320) — 失败 slice：可重试则 `redispatch`，否则 `markFailed`。

```cpp
for (auto &slice : failed_slice_list) {
    if (shouldRetrySlice(slice)) retry_list.push_back(slice);
    else { slice->markFailed(); ... }
}
if (!retry_list.empty()) redispatch(retry_list, thread_id);
```

**轮询阶段** `performPollCq`：从 CQ 收完成。注意 worker 按 `cq_index += kTransferWorkerCount` 步进分担多个 CQ，且把 WC 错误分成两类：

[`mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp:328-401`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp#L328-L401) — 轮询 CQ，成功 `markSuccess`，错误按类型分流。

```cpp
if (wc[i].status == IBV_WC_WR_FLUSH_ERR) {
    slice->markFailed();                 // endpoint 销毁刷出的错误，不重试
    continue;
}
// 其它 WC 错误 = 真实路径/网络失败
handlePathFailure(slice->peer_nic_path, slice->rdma.endpoint);
if (shouldRetrySlice(slice)) failed_slice_list.push_back(slice);
else slice->markFailed();
```

`handlePathFailure` 是统一的路径失败处理：给该 rail 累计错误（达阈值则暂停 1 秒）、通知所有 worker 重新派发队列、删除失效 endpoint：

[`mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp:608-621`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp#L608-L621) — `shouldRetrySlice`（自增并判定）与 `handlePathFailure`（暂停 rail + 通知 + 删 endpoint）。

```cpp
bool WorkerPool::shouldRetrySlice(Transport::Slice *slice) {
    slice->rdma.retry_cnt++;
    return slice->rdma.retry_cnt < slice->rdma.max_retry_cnt;
}
void WorkerPool::handlePathFailure(const std::string &peer_nic_path,
                                   RdmaEndPoint *endpoint) {
    markRailFailed(peer_nic_path);
    redispatch_counter_++;          // 唤醒其它 worker 重新派发
    if (endpoint) context_.deleteEndpointByPtr(endpoint);
}
```

**重试执行** `redispatch`：超限则失败，否则用 `retry_cnt` 选备选 NIC 重新解析 rkey 并回到自己的本地队列：

[`mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp:404-441`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp#L404-L441) — `redispatch`：超限 `markFailed`，否则换设备重选 rkey 并重排队。

```cpp
if (slice->rdma.retry_cnt >= slice->rdma.max_retry_cnt) {
    slice->markFailed();
} else {
    RdmaTransport::selectDevice(..., slice->rdma.retry_cnt);  // 切备选 NIC
    slice->rdma.dest_rkey = ...->rkey[device_id];
    collective_slice_queue_[thread_id][peer_nic_path].push_back(slice);
}
```

**端点回收与监控**：`monitorWorker` 用 epoll 监听 RDMA 异步事件（端口 down 时 `set_active(false)` + 断开所有 endpoint），并每秒调 `reclaimEndpoints()` 回收 waiting_list 里的不活跃 endpoint（issue #1845 的修复）：

[`mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp:548-576`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp#L548-L576) — `monitorWorker`：周期回收 endpoint + epoll 处理异步事件。

endpoint 销毁本身是**两阶段**的（`beginDestroy` 把 QP 转到 ERR 让硬件刷出在途 WR，`finishDestroy` 等刷干净后再真正销毁），避免与并发 `submitPostSend` 的 use-after-free——这部分在 `rdma_endpoint.cpp:203-284`。

#### 4.5.4 代码实践

**实践目标**：完整跟踪一次 WRITE 的异步调用链，并标注三个失败重试点。这是本讲规格指定的核心实践（源码阅读型）。

**操作步骤**：

1. 准备一张纸，从 `RdmaTransport::submitTransfer`（`rdma_transport.cpp:495`）开始，逐层记下函数名、文件、行号，直到 `ibv_post_send`（`rdma_endpoint.cpp:781`）。
2. 在链条上用三种颜色标注三个失败重试点：
   - 🔴 post send 阶段（`worker_pool.cpp:307-320` 的 `failed_slice_list` 重试）
   - 🔵 轮询阶段（`worker_pool.cpp:345-380` 的 WC 错误重试）
   - 🟢 重试执行（`worker_pool.cpp:404-441` 的 `redispatch` 切 NIC）
3. 思考：为什么 `WR_FLUSH_ERR` 不重试，而其它 WC 错误要重试？

**需要观察的现象**：链条清晰呈现"同步切片 → 异步下发 → 异步完成"三段。三个重试点都汇聚到同一个 `redispatch`，区别只在"触发原因"。`WR_FLUSH_ERR` 是我们自己主动销毁 endpoint（`beginDestroy`）造成的，重试无意义（路径已注定要换）；其它错误是真实网络/路径问题，换一条路径重试有成功希望。

**预期结果**：你能口头复述完整链路，并回答"失败重试发生在 worker 线程的 `performPostSend` 和 `performPollCq` 两处，最终都汇聚到 `redispatch`，由 `shouldRetrySlice` 用 `retry_cnt` 计数，`selectDevice(retry_cnt)` 切换备选 NIC"。

> 待本地验证：若要动态观察，可在 `performPollCq` 的错误分支临时 `LOG(INFO)` 打印 `wc[i].status` 和 `slice->rdma.retry_cnt`，跑回环或真实传输制造一次错误（如中途拔掉一条链路），观察重试日志（仅作观察，勿提交）。

#### 4.5.5 小练习与答案

**练习 1**：用户调用 `submitTransfer` 后立即返回，数据可能还没真正发出。那用户怎么知道传完了？

**参考答案**：通过 `getTransferStatus` 轮询（或事件驱动完成）。`submitTransfer` 只负责切片入队（`slice_queue_`），真正下发和完成都在 worker 线程异步进行。slice 完成时 worker 调 `slice->markSuccess/markFailed` 原子累加 task 的 `success/failed_slice_count`（`u3-l1` 的 4.5）。用户调 `getTransferStatus` 时用 `success + failed == slice_count` 判定是否到达终态。

**练习 2**：`handlePathFailure` 里 `redispatch_counter_++` 之后，为什么 worker 在 `performPostSend` 开头要检查 `tl_redispatch_counter`？

**参考答案**：因为某条路径失败时，**其它 worker 的本地队列里可能还缓存着发往该失败路径的 slice**。`redispatch_counter_++` 是个"全局失效信号"；每个 worker 用线程局部副本 `tl_redispatch_counter` 与之比较，一旦发现自己落后，就把自己队列里的 slice 全部 `redispatch` 重新选路（`worker_pool.cpp:246-256`），避免继续往已知失效的路径上送。这是一种轻量的"失效广播 + 惰性重派"机制。

---

## 5. 综合实践

把本讲的知识串起来，完成本讲规格指定的综合任务：**在 RDMA 环境下运行传输基准，记录不同 message size 下的带宽；并对照源码解释一次 WRITE 从 `submitTransfer` 到 `ibv_post_send` 的调用链与失败重试点**。

### 任务 A：运行传输基准（需真实 RDMA 环境）

Mooncake 提供了基准工具 `tebench`（`mooncake-transfer-engine/benchmark/`）。它的主循环会遍历不同的 `block_size`（message size）和 `batch_size`，统计吞吐与延迟：

[`mooncake-transfer-engine/benchmark/main.cpp:136-154`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/benchmark/main.cpp#L136-L154) — `tebench` 主循环：对每个 `block_size`（×2 递增）和 `batch_size` 调 `processBatchSizes` 统计。

**操作步骤**（两台有 RDMA 网卡的机器）：

1. 在目标机器（target）启动：
   ```bash
   ./tebench --backend=classic --seg_type=rdma
   ```
   它会打印 initiator 应使用的 `--target_seg_name`。
2. 在发起机器（initiator）启动：
   ```bash
   ./tebench --backend=classic --seg_type=rdma \
             --target_seg_name=<上一步打印的名字> \
             --op_type=write \
             --start_block_size=4096 --max_block_size=16777216
   ```
3. 记录不同 `block_size`（4KB / 64KB / 1MB / 16MB）下的带宽，观察"小消息延迟受限、大消息带宽受限"的典型曲线。

**需要观察的现象**：开启 `MC_IB_PCI_RELAXED_ORDERING=2`（auto）重跑一次，对比读操作的带宽是否提升（取决于硬件是否支持，见 4.3.3 的 `has_ibv_reg_mr_iova2` 探测）。也可以调 `MC_NUM_QP_PER_EP`、`MC_MAX_WR`、`MC_WORKERS_PER_CTX` 观察对吞吐的影响，并对照 4.4.4 / 4.2.4 理解参数含义。

> **待本地验证**：本任务依赖真实 RDMA 硬件（两台带 RNIC 的机器 + metadata/etcd 服务）。若本地无 RDMA 环境，请改做任务 B（纯源码阅读型，无需硬件）。

### 任务 B：源码阅读型——画出 WRITE 调用链与重试点（无需硬件）

1. 依据 4.5.2 的调用链，在源码里逐跳确认每个函数的文件与行号。
2. 在 `rdma_endpoint.cpp:719-802` 的 `submitPostSend` 里，圈出 `sge.lkey`（源 lkey）和 `wr.rdma.rkey`（目的 rkey）分别来自哪里，回溯它们各自在 4.3 里是怎么产生并填进 slice 的。
3. 列出三个失败重试点，并解释为什么 `WR_FLUSH_ERR` 不重试。
4. 把 relaxed ordering 的开关链路也画出来：`RdmaTransport 构造函数 → has_ibv_reg_mr_iova2 → registerLocalMemoryInternal 的 access_rights`。

**预期结果**：你得到一张覆盖"切片 → 选路 → 建连 → post send → 完成回收 → 失败重试"的完整时序图，并能解释 lkey/rkey、QP 状态机、worker 池三者如何协同完成一次 RDMA WRITE。

## 6. 本讲小结

- `RdmaTransport` 是 RDMA 协议的 `Transport` 实现，持有 `context_list_`（每块 NIC 一个 `RdmaContext`）和 `local_topology_`；它本身不碰 verbs API，只做"选路 + 分派"。
- `RdmaContext` 是一块 NIC 的资源容器，聚合 `ibv_context / pd / cq_list / memory_region_map / endpoint_store / worker_pool`；`construct` 按 `open device → alloc pd → comp channel → CQ → WorkerPool` 搭建，析构严格逆序释放。
- 内存注册（`ibv_reg_mr`）产生 `lkey`（本地用）/`rkey`（远端用）；多 NIC 下每块 NIC 各注册一份，所以 `BufferDesc.lkey/rkey` 是 `vector`，按 `device_id` 索引。relaxed ordering 通过 `MC_IB_PCI_RELAXED_ORDERING` + `IBV_ACCESS_RELAXED_ORDERING` 开启。
- `RdmaEndPoint` 持有一组 QP（`IBV_QPT_RC`），建连走 `RESET → INIT → RTR → RTS` 状态机（`RTR` 填对端 GID/LID/QPN，`RTS` 填硬件重试参数）；`submitPostSend` 受 QP 深度、CQ 容量、slice 数三重约束，构造 `ibv_send_wr` 后 `ibv_post_send`。
- 调用链：`submitTransfer → submitTransferTask（切片+填 lkey）→ RdmaContext::submitPostSend → WorkerPool::submitPostSend（解析 rkey+选 rail+入队）`，然后在 transfer worker 里 `performPostSend（建连+ibv_post_send）` 与 `performPollCq（ibv_poll_cq+markSuccess/markFailed）`。
- 失败重试有三处汇聚到 `redispatch`：post send 失败、CQ 轮询到非 `WR_FLUSH_ERR` 的 WC 错误、以及 `redispatch` 内按 `retry_cnt` 切换备选 NIC；`shouldRetrySlice` 用 `retry_cnt < max_retry_cnt` 控制，超限才真正 `markFailed`。`monitorWorker` 负责异步事件处理与不活跃 endpoint 的回收。

## 7. 下一步学习建议

1. **精读 endpoint_store 与两阶段销毁**：本讲多次提到 endpoint 的回收与 `beginDestroy/finishDestroy`。建议读 `endpoint_store.cpp` 和 `rdma_endpoint.cpp:203-284`，理解 SIEVE/FIFO 两种存储策略、waiting_list 回收、以及两阶段销毁如何避免与并发 post send 的竞态（issue #1845）。
2. **深入 handshake 与 GID 自动选择**：`setupConnectionsByActive/Passive` 里有"同时打开（simultaneous open）"、auto-GID reprobe 等复杂分支（`rdma_endpoint.cpp:295-647`）。如果你对网络初始化的鲁棒性感兴趣，这是下一站。
3. **对比其它 Transport**：现在你已经理解了 RDMA 这套"Context/EndPoint/Worker"模型，可以对比 `tcp_transport.cpp`、`nvlink_transport.cpp`，看它们如何用相同 `Slice/Task/Batch` 骨架实现完全不同的下发与完成机制，巩固 `u3-l1` 建立的抽象。
