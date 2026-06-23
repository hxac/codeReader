# MultiTransport：多传输协议聚合、选择与故障转移

## 1. 本讲目标

学完本讲后，你应当能够：

1. 说清楚 **`MultiTransport`** 是什么——为什么 Transfer Engine 不直接用一个 `Transport`，而要在外面套一层「聚合容器」，这层容器内部用 `transport_map_` 同时持有多少种传输。
2. 理解 **传输安装（install）** 机制：`installTransport()` 这个「工厂函数」如何根据一个协议字符串（`"rdma"` / `"tcp"` / `"cxl"` …）实例化出对应的 `Transport` 子类，以及 `TransferEngineImpl::init()` 的自动发现如何根据拓扑决定**装哪种**传输。
3. 掌握 **协议选择（select）**：单协议模式 `selectTransport()` 如何读目标 segment 的 `protocol` 字段定路由；多协议模式 `mp_selectTransport()` 又如何解析逗号分隔的协议列表、由调用方显式指定 `preferred_proto`。
4. 理解 **故障转移（failover）** 的真实边界：**`MultiTransport` 层并不做跨协议的自动切换**，真正的「替代路径选择」发生在更底层——RDMA 传输内部的 **rail 暂停 → 备选设备 → redispatch → context 失活**，以及在多协议模式下由调用方重新提交时切换协议。
5. 能在一台 RDMA 机器上动手安装 `tcp`+`rdma` 两种传输，提交一批任务并解释每条请求被路由到哪个 `Transport`。

---

## 2. 前置知识

本讲假设你已经读过：

- [u2-l2](u2-l2-segment-buffer-transport.md)：知道 **Segment / Buffer / TransferRequest / Batch / Transport** 这些基本概念，知道一次 `submitTransfer` 是「发起端把若干 `TransferRequest` 装进一个 batch，由某个 `Transport` 去搬运」。
- [u2-l3 Topology](u2-l3-topology-device.md)：知道 `Topology` 如何把 NIC 分成 `preferred_hca` / `avail_hca`，以及 `selectDevice(location, retry_count)` 的「首选优先、重试回退」语义——本讲的故障转移正是建立在它之上。

下面补充三个本讲必须的「衔接」概念。

### 2.1 为什么需要「多传输」聚合

Mooncake 支持的搬运方式远不止一种：跨机用 **RDMA（RoCE/IB）/ TCP**，机内用 **NVLink / CXL**，还有针对华为昇腾的 **HCCL / AscendDirect**、针对 EFA、Sunrise、UB 的各种传输。它们的硬件接口、内存注册方式、状态机完全不同，但上层的调用者（推理框架、KVCache Store）只想用一个统一的 `submitTransfer()`。

如果把「选哪种传输」直接写进调用方，代码会变成一团 `if (rdma) ... else if (tcp) ...`。`MultiTransport` 就是来解决这个问题的：

> 它是 `TransferEngineImpl` 持有的一个**传输聚合器**，内部用一张 `proto 字符串 → Transport 实例` 的表 `transport_map_`，把所有已安装的传输统一管理起来；对外只暴露一套与 `Transport` 几乎相同的 API，并在内部负责**为每条请求挑选正确的传输**。

注意层级关系：`TransferEngineImpl`（引擎实现）拥有 `MultiTransport`（聚合器），`MultiTransport` 又拥有若干个 `Transport` 子类实例。

### 2.2 Transport 抽象与 `proto` 字符串

每个具体的传输都继承自抽象基类 `Transport`（见 [transport.h](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transport/transport.h)），并实现 `install()` / `submitTransfer()` / `getTransferStatus()` / `registerLocalMemory()` 等纯虚函数。`MultiTransport` 给每个子类起一个**协议名字符串** `proto`，例如：

| `proto` 字符串 | 对应子类（编译宏） | 典型用途 |
| --- | --- | --- |
| `"rdma"` | `RdmaTransport`（`USE_*` 默认） | 跨机 RoCE/IB |
| `"tcp"` | `TcpTransport`（`USE_TCP`） | 无 RDMA 时的回退 |
| `"cxl"` | `CxlTransport`（`USE_CXL`） | 机内 CXL 内存共享 |
| `"nvlink"` / `"nvlink_intra"` | `NvlinkTransport` / `IntraNodeNvlinkTransport` | GPU 间 P2P |
| `"ascend"` | `HcclTransport` / `AscendDirectTransport` / `HeterogeneousRdmaTransport` | 昇腾 NPU |

这张表既是 `installTransport()` 的「工厂查表」依据，也是 `selectTransport()` 路由时的 key。

### 2.3 目标 Segment 的 `protocol` 字段是路由的「票根」

这是本讲最关键的一句话：**一条 `TransferRequest` 走哪种传输，不取决于发起端「想」用什么，而取决于它的目标 segment 在元数据里登记的 `protocol` 字段。**

当一个节点 `openSegment()` 并注册内存后，它会把「我这段内存用 `rdma`/`tcp`/`cxl` 访问」写进 `SegmentDesc::protocol`，并通过元数据服务（etcd 等）广播出去。发起端拿到 `target_id` 后，通过 `getSegmentDescByID(target_id)->protocol` 就知道该把请求交给哪个 `Transport`。

理解了这一点，你就能解释本讲实践任务里的核心问题——「为什么装了 `tcp`+`rdma` 两种传输后，不同请求会被分到不同传输」：因为它们的目标 segment 登记的协议不同。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [multi_transport.h](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/multi_transport.h) | `MultiTransport` 类声明：`transport_map_`、公开 API（`submitTransfer`/`installTransport`/`getTransport`/`isTcpOnly`…）和私有的 `selectTransport` / `mp_selectTransport`。 |
| [multi_transport.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp) | 全部实现：批次管理、`submitTransfer` 的「先选传输、再分组提交」、`installTransport` 的 if-else 工厂、两种 `selectTransport` 的路由逻辑。 |
| [transfer_engine_impl.h](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_engine_impl.h) | `TransferEngineImpl` 持有 `multi_transports_` 成员（行 414），并把 `submitTransfer`/`allocateBatchID` 等**直接委托**给它（行 119–134、178–195）。 |
| [transfer_engine_impl.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp) | `init()` 里的**自动发现安装决策树**（行 196–398）：根据拓扑/环境变量决定装 `rdma` 还是 `tcp`、是否额外装 `cxl`/`ascend`/`hip`。 |
| [worker_pool.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp) | RDMA 传输内部的**故障转移**实现：备选设备选择（行 150–173）、rail 暂停（行 578–604）、redispatch（行 245–256）、context 失活（行 200–203）。 |
| [worker_pool.h](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transport/rdma_transport/worker_pool.h) | 故障转移的数据结构与阈值：`RailState`、`context_failure_count_`、`kRailErrorThreshold=5`、`kRailPauseNs=1s`、`kContextFailureThreshold=32`。 |
| [mp_transport_test.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tests/mp_transport_test.cpp) | 多协议（`cxl,tcp`）的端到端测试：演示如何同时安装两种传输、`mp_registerLocalMemory`、`mp_submitTransfer`。 |

---

## 4. 核心概念与源码讲解

### 4.1 MultiTransport：传输聚合容器

#### 4.1.1 概念说明

`MultiTransport` 是「门面（Facade）+ 路由器（Router）」的结合体：

- **门面**：它对外的 API（`allocateBatchID` / `submitTransfer` / `getTransferStatus` / `freeBatchID`）和 `Transport` 抽象基类几乎一模一样，所以上层 `TransferEngineImpl` 完全感受不到「下面有多个传输」。
- **路由器**：每收到一条 `TransferRequest`，它要回答「这条请求该交给下面哪个 `Transport`」。这正是 `selectTransport()` 的职责。
- **容器**：它用 `transport_map_`（`map<string, shared_ptr<Transport>>`）把所有已安装传输统一持有，提供 `installTransport()` / `getTransport()` / `listTransports()` 来管理它们。

注意它**不负责**真正搬数据——搬数据是每个具体 `Transport` 的事；它只负责「分拣」和「批次管理」。

#### 4.1.2 核心流程

`MultiTransport` 把一次批量提交拆成三步：**选传输 → 按传输分组 → 逐组下发**。

```
submitTransfer(batch_id, entries[])
   │
   ├─ 1. 容量检查：task_list.size()+entries.size() 不能超过 batch_size
   ├─ 2. 为每条 entry 调 selectTransport(entry, &transport)
   │        └─ transport = transport_map_[目标 segment 的 protocol]
   │        └─ 选不到 → 返回 NotSupportedTransport
   ├─ 3. 为每条 entry 建一个 TransferTask，记录 task.transport_ = transport
   │        并按 transport 分桶：submit_tasks[transport].push_back(&task)
   └─ 4. for 每个 (transport, task 子集)：
            transport->submitTransferTask(task 子集)   # 真正下发
```

第 2 步是「路由」，第 3 步是「分组聚合」——把同一个 `Transport` 的任务攒到一起一次性下发，减少跨传输的重复调用。第 4 步里如果某个 `Transport` 下发失败，会用 `overall_status` 记下错误但仍继续下发其它传输的任务（不会一条失败拖垮整批）。

`MultiTransport` 自己还维护一张 batch 表 `batch_desc_set_`（受 `CONFIG_USE_BATCH_DESC_SET` 宏控制，默认关闭，用裸指针转换），以及 batch 生命周期：`allocateBatchID` 建 `BatchDesc`，`freeBatchID` 检查所有 task 都 `is_finished` 后才删除。

#### 4.1.3 源码精读

先看类声明，重点抓住三样东西：`transport_map_`、公开的委托 API、私有的 `selectTransport`。

[multi_transport.h:79-85](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/multi_transport.h#L79-L85) —— 私有成员：`transport_map_`（`proto → Transport` 的表）、`local_server_name_`、以及可选的 batch 表 `batch_desc_set_`。这就是「聚合容器」的全部家当。

[multi_transport.h:53-64](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/multi_transport.h#L53-L64) —— 对外的传输管理接口：`installTransport(proto, topo)` 装一个传输、`getTransport(proto)` 取已装的传输、`listTransports()` 列出全部、`isTcpOnly()` 判断是否只装了 TCP（后面会讲它的用途）。

[multi_transport.h:71-77](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/multi_transport.h#L71-L77) —— 私有的两个路由器：`selectTransport`（单协议）和 `mp_selectTransport`（多协议，受 `ENABLE_MULTI_PROTOCOL` 宏保护）。

再看 `submitTransfer` 的「选→分→发」三步实现：

[multi_transport.cpp:110-149](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp#L110-L149) —— `submitTransfer`：第 121–122 行声明按传输分桶的 `submit_tasks`；第 123–138 行遍历每条请求，`selectTransport` 选出 `transport`，建 `task` 并把 `task.transport_` 设上，塞进对应桶；第 140–147 行逐桶调用 `transport->submitTransferTask()`，单个传输失败只记 `overall_status` 不中断。

注意第 130 行 `task.transport_ = transport;`——每个 `TransferTask` 都记住了它归哪个传输。这个指针后来在 `getTransferStatus()` 里被用来**回查状态**：

[multi_transport.cpp:228-239](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp#L228-L239) —— `getTransferStatus` 里：`task.transport_` 非空就委托给该传输的 `getTransferStatus()` 去轮询完成情况（例如 NVLink 异步传输在这里 `cudaStreamQuery`），并叠加超时检测。这正是「记住路由」带来的好处——状态查询也能精准回到正确的传输。

最后看上层 `TransferEngineImpl` 是怎么把整件事委托出去的——它几乎是「透传」：

[transfer_engine_impl.h:414](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_engine_impl.h#L414) —— `TransferEngineImpl` 持有 `std::shared_ptr<MultiTransport> multi_transports_;`。

[transfer_engine_impl.h:119-134](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_engine_impl.h#L119-L134) —— `submitTransfer` 直接 `multi_transports_->submitTransfer(...)`，只是在 `WITH_METRICS` 时顺带记一下 task 的起始时间。`allocateBatchID` / `freeBatchID`（行 235–241）同样是单行透传。

#### 4.1.4 代码实践：观察「聚合 + 分组」

**目标**：在不写新代码的前提下，通过阅读源码确认「同一批请求会被按传输分组下发」。

**操作步骤**（源码阅读型）：

1. 打开 [multi_transport.cpp:110-149](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp#L110-L149)。
2. 假设一批里有 4 条 `TransferRequest`，其中 3 条的目标 segment 登记为 `"rdma"`、1 条为 `"tcp"`。手动推演 `submit_tasks` 这个 `unordered_map<Transport*, vector<Task*>>` 最终会有几个桶、每桶几个 task。
3. 对应到第 140–147 行的循环，确认 `submitTransferTask` 会被调用几次（答案：2 次，每个传输一次）。

**预期结果**：`submit_tasks` 有 2 个桶（rdma 桶 3 个 task、tcp 桶 1 个 task），`submitTransferTask` 被调用 2 次。这验证了「聚合器只做分拣，不做搬运；同传输的任务被合并成一次下发」。

**待本地验证**：若想亲眼看到分桶，可在第 137 行后临时加一行 `LOG(INFO) << "routed to " << transport->getName();`（仅本地调试，勿提交），提交混合目标的一批请求即可在日志里看到两种传输名交替出现。

#### 4.1.5 小练习与答案

**练习 1**：`TransferEngineImpl` 已经有了 `MultiTransport`，为什么还要在 `MultiTransport` 之外再包一层 `TransferEngineImpl`？两者职责怎么分？

> **参考答案**：`MultiTransport` 只管「传输的聚合与路由 + batch 生命周期」。而 `TransferEngineImpl` 还要管：元数据（`metadata_`）、本地内存注册（`local_memory_regions_` + `registerLocalMemory`）、拓扑（`local_topology_`）、notify 通知（`notifies_to_send_`）、指标（metrics）、自动发现安装（`init()` 里的决策树）。分层是为了让 `MultiTransport` 保持「纯传输」关注点，不被元数据/内存管理等拖胖。

**练习 2**：第 140–147 行里，如果其中一个传输的 `submitTransferTask` 返回错误，整批请求会怎样？

> **参考答案**：不会整批回滚。代码用 `overall_status` 记下第一个失败的 status，但循环**继续**下发其它传输的 task。最终 `submitTransfer` 返回这个 `overall_status`（非 OK），但已成功下发的 task 仍会照常执行；失败的只是那一组。状态查询时该 task 会反映真实结果（成功/失败）。

---

### 4.2 传输安装：`installTransport` 工厂与自动发现

#### 4.2.1 概念说明

`MultiTransport` 出生时 `transport_map_` 是空的，必须有人往里「装」传输。装的动作叫 `installTransport(proto, topo)`，它是一个**简单工厂**：

1. 拿到协议字符串 `proto`；
2. 用一长串 `if (proto=="rdma") ... else if (proto=="tcp") ...` new 出对应的 `Transport` 子类；
3. 调子类的 `install(server_name, metadata, topo)` 完成真正的初始化（建 context、握手、注册设备）；
4. 成功就塞进 `transport_map_[proto]`，失败就 `delete` 掉返回 `nullptr`。

这层工厂受**编译宏**门控：只有编译时定义了 `USE_TCP`，`"tcp"` 分支才会存在；否则即使你传 `"tcp"`，也 new 不到任何对象，会打印 `Unsupported transport tcp, please rebuild Mooncake`。

「谁」来调这个工厂？有两条路径：

- **自动发现路径**（生产默认）：`TransferEngineImpl::init()` 在 `auto_discover_=true` 时，先 `Topology::discover()` 探测本机硬件，再按决策树决定装哪种传输。
- **手动路径**（测试/benchmark 用）：直接调 `TransferEngine::installTransport(proto, args)`。

#### 4.2.2 核心流程

自动发现的决策树（节选，见 [transfer_engine_impl.cpp:236-378](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp#L236-L378)）：

```
init():
  multi_transports_ = make_shared<MultiTransport>(metadata, local_server_name)   # 建空聚合器
  # —— 以下按硬件平台编译宏分流，互斥的几组里只走一支 ——
  若 USE_CXL 且 MC_CXL_DEV_PATH 非空  → install("cxl", topo)        # CXL 内存设备
  若 auto_discover_:
     local_topology_->discover(filter)                             # 探测 NIC/GPU/NUMA
     若 USE_MNNVL/USE_INTRA_NVLINK 且无 HCA → install("nvlink"/"nvlink_intra")
     否则（通用 GPU/CPU 平台）:
        有 HCA 且无 MC_FORCE_TCP   → install("rdma", topo)          # 有 RDMA 网卡就用 RDMA
        或设了 MC_FORCE_HCA        → install("rdma", topo)
        否则                       → install("tcp", nullptr)        # 兜底走 TCP
     若 USE_HIP → install("hip")  # 机内 GPU P2P，可与上面的跨机传输并存
```

两条关键规律：

1. **互斥 vs 并存**：`rdma` 和 `tcp` 是**二选一**（同一个 `#elif` 分支），自动发现不会同时装两者；而 `cxl` / `hip` 这类机内传输可以和跨机传输**并存**（所以才会出现「CXL + RDMA」这种多协议组合）。
2. **`MC_FORCE_TCP` / `MC_FORCE_HCA`**：环境变量强制覆盖默认判断。明明有 HCA 却想用 TCP 做对照测试时，设 `MC_FORCE_TCP` 即可。

#### 4.2.3 源码精读

**工厂本体**——一长串按 `proto` 名字 new 子类的 if-else，每个分支都被编译宏门控：

[multi_transport.cpp:309-340](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp#L309-L340) —— `installTransport` 前半段：`"rdma"`（无条件）→ `new RdmaTransport()`；`"ub"`/`"tcp"`/`"nvmeof"`/`"ascend"`/`"nvlink_intra"`/`"hip"`/`"nvlink"`/`"cxl"`… 各自被 `USE_*` 宏包住。注意 `"ascend"` 这个名字会按宏优先级映射到三种不同的昇腾子类（`AscendDirectTransport` > `HcclTransport` > `HeterogeneousRdmaTransport`）。

[multi_transport.cpp:393-440](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp#L393-L440) —— 工厂后半段：如果没匹配到任何分支，`transport` 仍为 `nullptr`，第 393–397 行打印 `Unsupported transport ... please rebuild Mooncake` 并返回空；否则第 433 行调 `transport->install(...)` 真正初始化，失败 `delete`，成功则第 438 行 `transport_map_[proto] = shared_ptr<Transport>(transport)` 入表。

**自动发现里的 RDMA/TCP 二选一**：

[transfer_engine_impl.cpp:341-378](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp#L341-L378) —— 通用平台的核心判断：`(HCA 数量>0 且 未设 MC_FORCE_TCP) 或 设了 MC_FORCE_HCA` → 装 `rdma`；否则装 `tcp`。第 379 行注释 `// TODO: install other transports automatically` 说明目前自动发现还不会主动装 TCP+RDMA 两套。

**CXL 可并存**：

[transfer_engine_impl.cpp:224-234](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp#L224-L234) —— `USE_CXL` 且设了 `MC_CXL_DEV_PATH` 就额外装 `cxl`，它与下面的 `rdma`/`tcp` 不互斥，于是 `transport_map_` 里可以同时有 `"cxl"` 和 `"rdma"`。这就是多协议（`ENABLE_MULTI_PROTOCOL`）能跑起来的前提。

**手动安装路径**（测试用）：

[transfer_engine_impl.cpp:409-441](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp#L409-L441) —— `TransferEngineImpl::installTransport(proto, args)`（注释明确写 `Only for testing`）：先查 `transport_map_` 是否已装（第 412–416 行，重复装会告警并返回已有实例），再用 `args[0]` 里的 NIC 优先级矩阵解析拓扑（第 418–425 行），最后委托 `multi_transports_->installTransport(proto, topo)`。这个手动入口是 benchmark 和测试同时安装多种传输的途径。

#### 4.2.4 代码实践：同时安装 `tcp` + `rdma`

**目标**：在一台有 RDMA 网卡的机器上，**手动**安装 `tcp` 和 `rdma` 两种传输，并确认 `transport_map_` 里同时存在两者。

**操作步骤**：

1. 阅读示例 [transfer_engine_bench.cpp:287-301](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/transfer_engine_bench.cpp#L287-L301)，确认 `FLAGS_auto_discovery=false` 时它用 `installTransport("rdma"/"tcp", args)` 手动装**一种**传输。
2. 仿照它写一个最小片段（**示例代码**，非项目原有）：

   ```cpp
   auto engine = std::make_unique<TransferEngine>(/*auto_discover=*/false);
   engine->init("P2PHANDSHAKE", "127.0.0.1:12345", "127.0.0.1", 12345);

   // (a) 手动装 RDMA：args[0] 是 NIC 优先级矩阵 JSON
   std::string matrix = R"({"cpu:0":[["mlx5_0"],[]]})";
   void* rdma_args[2] = {(void*)matrix.c_str(), nullptr};
   engine->installTransport("rdma", rdma_args);

   // (b) 手动装 TCP
   engine->installTransport("tcp", nullptr);

   // (c) 列出已装传输，确认两种都在
   for (auto* t : /*engine->impl_->multi_transports_->listTransports()*/)
       LOG(INFO) << "installed transport";
   ```

3. 想直接用现成二进制观察：用 `transfer_engine_bench`，target 侧 `--protocol=tcp`、initiator 侧 `--protocol=rdma`，分别在两个进程里各装一种；再各自 `openSegment` 对方。

**需要观察的现象**：步骤 (c) 应能看到两种传输都被装上（`listTransports()` 返回 2 个）；自动发现模式下则只能看到 1 种（rdma 或 tcp 二选一）。

**预期结果**：手动路径下 `transport_map_` 同时含 `"rdma"` 和 `"tcp"` 两个 key。

**待本地验证**：`listTransports()` 是 `MultiTransport` 的私有成员的外部映射，实际需经 `engine->getTransport("rdma")` / `getTransport("tcp")` 非空来间接确认；上例访问私有成员仅为示意。在无 RDMA 网卡的机器上 `install("rdma")` 会因找不到设备而返回 `nullptr`，只能装 TCP。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `rdma` 分支没有任何 `#ifdef` 包裹，而 `tcp` 分支要包在 `#ifdef USE_TCP` 里？

> **参考答案**：因为 RDMA（`RdmaTransport`）是 Mooncake 的**默认/核心**传输，几乎所有平台都编译进来，故 `proto=="rdma"` 无条件可 new。而 TCP 是「无 RDMA 时的回退」，是否编译由 `USE_TCP` 控制；不编 TCP 时 `"tcp"` 分支根本不存在，传 `"tcp"` 会落到第 393 行的 `Unsupported transport`。这种「核心传输无条件 + 可选传输按宏」的设计让二进制能按需裁剪。

**练习 2**：自动发现里，`MC_FORCE_TCP` 和「没有 HCA」两种情况都会走到装 TCP，它们有区别吗？

> **参考答案**：触发条件不同但结果相同（装 TCP）。`MC_FORCE_TCP` 是「明明有 HCA 仍强制 TCP」，用于对照测试或 RDMA 驱动有问题时临时降级；「无 HCA」是硬件上确实没有 RDMA 网卡，TCP 是唯一可行选项。两者最终都让 `isTcpOnly()` 返回 `true`，进而影响机内传输是否走本地 memcpy（见 4.3.4）。

---

### 4.3 协议选择：`selectTransport` 与多协议 `mp_selectTransport`

#### 4.3.1 概念说明

`MultiTransport` 的核心路由函数是 `selectTransport`。它解决的问题是：

> 给一条 `TransferRequest`（里面有 `target_id`），在 `transport_map_` 里挑出正确的 `Transport`。

答案藏在目标 segment 的元数据里——`getSegmentDescByID(target_id)->protocol`。单协议模式下这个字段就是一个名字（`"rdma"`），多协议模式下它可以是逗号分隔的列表（`"cxl,rdma"`）。

因此有两种路由 API：

| API | 宏 | 目标 `protocol` 字段 | 谁决定用哪个 |
| --- | --- | --- | --- |
| `selectTransport` | 默认 | 单个名字 | **目标** segment（字段写死） |
| `mp_selectTransport` | `ENABLE_MULTI_PROTOCOL` | 逗号分隔列表 | **发起端**（传 `preferred_proto`，但必须在目标列表内） |

一个常被误解的点（务必记住）：

> `selectTransport` 选不到时返回 `Status::NotSupportedTransport`，**它不会**自动「RDMA 失败就换 TCP」。跨协议的切换是**显式**的——多协议模式下由调用方重新提交并指定不同的 `preferred_proto`。`MultiTransport` 层没有「协议级自动故障转移」。

#### 4.3.2 核心流程

**单协议** `selectTransport`：

```
target = metadata_->getSegmentDescByID(entry.target_id)   # 拿目标 segment
若 target 为空 → InvalidArgument("Invalid target segment ID")
proto = target->protocol                                   # 例如 "rdma"
(昇腾异构特例：若 protocol=="rdma" 在 USE_ASCEND_HETEROGENEOUS 下改写成 "ascend")
若 transport_map_ 不含 proto → NotSupportedTransport
transport = transport_map_[proto]
```

**多协议** `mp_selectTransport`：多两步——解析逗号列表 + 校验发起端偏好是否被目标支持：

```
target = getSegmentDescByID(entry.target_id)
protos = split(target->protocol, ",")           # "cxl,rdma" → ["cxl","rdma"]
若 transport_map_ 不含 preferred_proto → NotSupportedTransport
若 preferred_proto 不在 protos 里 → NotSupportedTransport("... not supported by target segment")
transport = transport_map_[preferred_proto]
```

也就是说：目标 segment 用 `protocol="cxl,rdma"` 声明「我这两种都能访问」，但**最终走哪种由发起端在 `mp_submitTransfer` 时指定**（`preferred_proto`）。这给了上层「按内存类型选协议」的能力——比如同一台机器上，CXL 设备上的那段内存走 `cxl`，普通 DRAM 走 `rdma`。

#### 4.3.3 源码精读

**单协议路由**——直接读字段、查表：

[multi_transport.cpp:442-464](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp#L442-L464) —— `selectTransport`：第 444 行拿目标 segment desc；第 449 行读 `protocol`；第 450–457 行是昇腾异构的特例（把 `"rdma"` 改写成 `"ascend"`，因为目标侧复用 RDMA、发起侧用异构 RDMA）；第 458–461 行若 `transport_map_` 没有该协议，返回 `NotSupportedTransport`；第 462 行返回对应 `Transport*`。

**多协议路由**——解析列表 + 双重校验：

[multi_transport.cpp:466-505](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp#L466-L505) —— `mp_selectTransport`：第 476–481 行用 `stringstream` 按逗号切 `target->protocol` 成 `protos`；第 492–495 行校验 `preferred_proto` 已安装；第 496–501 行校验 `preferred_proto` 在目标的 `protos` 列表里（否则 `not supported by target segment`）；第 502 行返回传输。注意第 487–490 行同样有昇腾异构的 `preferred_proto` 改写。

**`isTcpOnly` 的用途**——它不是路由，但影响机内搬运选择：

[multi_transport.cpp:512-514](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp#L512-L514) —— `isTcpOnly()`：`transport_map_` 大小为 1 且那一个是 `"tcp"` 时返回 true。当**只有** TCP 时，同机传输优先走本地 `memcpy` 而非 TCP loopback（更快），这个判断由上层（Store 侧）使用。

**多协议的端到端示例**——看测试怎么用：

[mp_transport_test.cpp:112-124](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tests/mp_transport_test.cpp#L112-L124) —— `SetUp` 里分别 `installTransport("cxl", ...)` 和 `installTransport("tcp", ...)`，装两种传输。

[mp_transport_test.cpp:131-142](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tests/mp_transport_test.cpp#L131-L142) —— `mp_registerLocalMemory(buffer_map)`：`buffer_map` 以协议名为 key，给每种协议分别登记一段内存（CXL 段给 `base_addr+offset`，TCP 段给 `addr`）。这会让目标 segment 的 `protocol` 字段最终是 `"cxl,tcp"`。

[mp_transport_test.cpp:178](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tests/mp_transport_test.cpp#L178) —— `engine->mp_submitTransfer(batch_id, {entry}, proto)`：第三个参数 `proto`（来自 `FLAGS_protocol` 的第一项，如 `"cxl"`）就是 `preferred_proto`，决定这一批走 CXL。

#### 4.3.4 代码实践：观察「为每条请求选传输」

**目标**：亲手验证「路由由目标 segment 的 `protocol` 字段决定」，而不是由发起端随便选。

**操作步骤**（源码阅读 + 推演）：

1. 读 [multi_transport.cpp:442-464](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp#L442-L464)，确认 `proto` 完全来自 `target_segment_desc->protocol`，与 `entry.source` / `entry.opcode` 无关。
2. 假设你的 engine 手动装了 `tcp`+`rdma`（见 4.2.4），并用两个进程：
   - 进程 A（target）只装 `tcp`，`openSegment("A")` → 它的 segment 协议字段是 `"tcp"`；
   - 进程 B（target）只装 `rdma`，`openSegment("B")` → 协议字段是 `"rdma"`；
   - 进程 C（initiator）装了 `tcp`+`rdma`，分别对 A、B 发 `submitTransfer`。
3. 推演：C 对 A 的请求 → `selectTransport` 查到 `"tcp"` → `TcpTransport`；C 对 B 的请求 → 查到 `"rdma"` → `RdmaTransport`。即使 C 同时装了两种，路由完全跟随目标。

**需要观察的现象**：在 C 的日志里（若按 4.1.4 加了路由日志），对 A 和 B 的请求会分别打印 `tcp` 和 `rdma` 的传输名。

**预期结果**：同一个 initiator、同一批代码，仅仅因为 `target_id` 不同就被路由到不同 `Transport`——这正是「聚合器」的价值。

**待本地验证**：目标 segment 的 `protocol` 字段是否真如所述，可通过 `engine->getMetadata()->getSegmentDescByID(target_id)->protocol` 在发起端直接打印确认（注意 `SegmentDesc` 结构随版本可能调整字段名，以本地源码为准）。

#### 4.3.5 小练习与答案

**练习 1**：发起端只装了 `rdma`，却向一个 `protocol="tcp"` 的目标 segment 发请求，会发生什么？

> **参考答案**：`selectTransport` 第 458 行 `transport_map_.count("tcp")` 为 0，返回 `Status::NotSupportedTransport("Transport tcp not installed")`，`submitTransfer` 把它作为整体状态返回。请求**不会**被降级到 RDMA——协议不匹配就是直接失败。这再次说明 `MultiTransport` 没有跨协议自动回退。

**练习 2**：多协议模式下，目标 segment 声明 `protocol="cxl,rdma"`。发起端调用 `mp_submitTransfer(..., preferred_proto="tcp")`，会怎样？怎么改才能成功？

> **参考答案**：`mp_selectTransport` 第 492–501 行会先查 `transport_map_["tcp"]`（若没装 TCP 直接失败），即便装了 TCP，第 496–501 行也会发现 `"tcp"` 不在目标的 `["cxl","rdma"]` 列表里，返回 `not supported by target segment`。要成功，`preferred_proto` 必须从 `{"cxl","rdma"}` 里选一个，且发起端必须已安装该协议。

---

### 4.4 故障转移：rail 暂停、备选设备与 redispatch

#### 4.4.1 概念说明

先说清边界（这一点很重要，避免对系统行为产生错误预期）：

> **`MultiTransport` 自身不做「传输失败后自动换另一种协议」的故障转移。** 4.3 已说明：协议选择是确定性的，失败就返回错误状态。

那么「失败时的替代路径选择」发生在哪里？主要在 **RDMA 传输内部**，分三个层次：

1. **NIC 级（首选/备选回退）**：这是 u2-l3 讲过的 `Topology::selectDevice(location, retry_count)`——首次随机选 preferred NIC，失败重试时按 \((retry\_count - 1) \bmod |preferred+avail|\) 轮转，把备选 NIC 也纳入。属于「传输内、设备间」的回退。
2. **Rail 级（链路暂停 + 备选设备）**：当某条到对端 NIC 的「轨道（rail）」连续出错，`WorkerPool` 会把它**暂停**一段时间，并立即为该 slice 换一条可用 rail（遍历对端所有设备找可用的）。这是 `MultiTransport` 委托给 `RdmaTransport` 后，后者自带的快速故障转移。
3. **Context 级（硬件失活）**：如果某个本地 RNIC 的**所有** rail 都不可用（典型的本地网卡硬件故障），连续累计到阈值就把这个 context 标记为 inactive，后续直接快速失败。

此外还有**多协议显式切换**：上层应用可以捕获失败后，用 `mp_submitTransfer` 换一个 `preferred_proto` 重新提交——这是「应用层」的故障转移，不是引擎自动的。

本讲聚焦第 2、3 层（rail 与 context），因为它们是「传输失败时引擎自动选替代路径」的核心。

#### 4.4.2 核心流程

一次 RDMA slice 提交时的故障转移决策（见 [worker_pool.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp)）：

```
submitPostSend(slice_list):
  对每个 slice:
    selectDevice(...) 选 device_id                          # u2-l3 的首选 NIC
    若 isRailAvailable(peer_nic_path) == false:              # 这条 rail 被暂停了
        for 对端的每个其它设备 alt_dev_id:                    # —— 备选设备回退 ——
            若 isRailAvailable(alt_path): 换用它, found=true, break
        若都没找到 → slice->markFailed(); all_rails_failed_count++
  若 本批 submitted==0 且 all_rails_failed == slice 总数:     # 所有路全挂
        markContextFailure()                                 # 累计本地 RNIC 故障

(后台)某 slice 真正发送失败时:
    handlePathFailure(peer_nic_path, endpoint):
        markRailFailed(path)        # 该 rail 错误计数+1, 达 5 次暂停 1 秒
        redispatch_counter_++       # 通知所有 worker 把队列里的 slice 重新分发

performPostSend(worker):
    若 !contextHealthy(): 直接把队列里的 slice 全 markFailed   # 快速失败
    若检测到 redispatch_counter_ 变化: redispatch(本线程队列)  # 换路重投
```

关键阈值（[worker_pool.h:113-119](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transport/rdma_transport/worker_pool.h#L113-L119)）：

- `kRailErrorThreshold = 5`：某 rail 累计 5 次错误就暂停。
- `kRailPauseNs = 1_000_000_000`（1 秒）：暂停时长；暂停到期后 `error_count` 清零、自动恢复。
- `kContextFailureThreshold = 32`：连续 32 次「所有 rail 全挂」就把该 context 标记 inactive。

外加 `globalConfig()` 里的 `retry_cnt`（默认 9，见 [config.h:52](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/config.h#L52)）控制单 slice 最多重试次数，以及 `slice_timeout`（默认 -1 即关闭，见 [config.h:64](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/config.h#L64)）——后者在 `MultiTransport::getTransferStatus` 里把超时的 slice 标成 `TIMEOUT`。

#### 4.4.3 源码精读

**备选设备回退**——rail 不可用时立即换一条路：

[worker_pool.cpp:150-173](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp#L150-L173) —— `submitPostSend` 里：第 151 行若 `!isRailAvailable(peer_nic_path)`，第 153–167 行遍历对端 segment 的所有设备（`alt_dev_id`），找到第一条 `isRailAvailable` 的就换用它的 `rkey`/`peer_nic_path`；第 168–172 行若全不可用，`markFailed()` 并 `all_rails_failed_count++`。这是「毫秒级」的故障转移，不需要等重试。

**Context 失活判定**——所有路全挂视为本地硬件故障：

[worker_pool.cpp:197-203](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp#L197-L203) —— 一批里 `submitted_slice_count==0` 且 `all_rails_failed_count` 等于 slice 总数时，调 `markContextFailure()`。

**rail 暂停与恢复**：

[worker_pool.cpp:578-604](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp#L578-L604) —— `markRailFailed`：`error_count++`，到 5 就设 `pause_until_ns = now + 1s` 并打 `Rail paused`；`isRailAvailable`：未暂停返回 true，暂停未到期返回 false，**到期则自动清零恢复**（第 597–602 行）。注意它按 `peer_nic_path`（即「本机某 NIC → 对端某 NIC」这条轨道）粒度记录，不是整块网卡。

**redispatch**——把已在队列里的 slice 换路重投：

[worker_pool.cpp:245-256](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp#L245-L256) —— `performPostSend` 里用 `thread_local` 计数器对比全局 `redispatch_counter_`，一旦发现它被 `handlePathFailure` 自增过，就把本线程队列里的 slice 克隆出来 `redispatch()`（重新 `selectDevice`、重新选可用 rail 入队），从而让其它 NIC 接管。

[worker_pool.cpp:606-621](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp#L606-L621) —— `shouldRetrySlice`（单 slice 重试计数 < `max_retry_cnt` 才允许重试）和 `handlePathFailure`（标记 rail 失败、`redispatch_counter_++`、删除坏 endpoint）。

**context 健康状态**：

[worker_pool.h:65-80](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transport/rdma_transport/worker_pool.h#L65-L80) —— `contextHealthy()` 判断 `context_failure_count_ < 32`；`markContextFailure()` 自增并到阈值后 `context_.set_active(false)`；`markContextSuccess()` 任意一次成功就清零。一旦 inactive，[worker_pool.cpp:208-227](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp#L208-L227) 的 `performPostSend` 直接把队列里的 slice 全部 `markFailed` 快速返回，避免继续往坏网卡上发。

#### 4.4.4 代码实践：构造一次 RDMA 失败观察故障转移

**目标**：在多 NIC 的 RDMA 机器上，制造一条 rail 故障，观察「备选设备接管 → rail 暂停 → redispatch → 自动恢复」的完整过程。

**操作步骤**：

1. **准备多 rail 环境**。确认本机有 ≥2 块 RDMA NIC（`ibv_devices`），并用 NIC 优先级矩阵让首选/备选 NIC 都存在（见 u2-l3 的 `preferred`/`avail`）。可用 `transfer_engine_bench` 的 `--nic_priority_matrix` 指定一份含两块 NIC 的 JSON。

2. **打开观测点**。阅读 [worker_pool.cpp:585-586](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp#L585-L586)（`Rail paused` 日志）和 [worker_pool.h:75-78](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transport/rdma_transport/worker_pool.h#L75-L78)（`All rails failed ... marking inactive` 日志）——这两条 `LOG(WARNING)` 就是故障转移的可见信号，无需改代码。

3. **制造 rail 故障**。可选手段（任选其一，按你的环境而定）：
   - 用 `Topology::disableDevice(device_name)`（见 [topology.h:80](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/topology.h#L80)）在发起端禁用首选 NIC，迫使其走备选；
   - 或在对端把某 NIC 的端口 `ip link set <dev> down`，让发往它的请求超时；
   - 或设 `globalConfig().slice_timeout` 为一个较小正数（如 2 秒），让慢路径被判 `TIMEOUT` 触发重试。

4. **跑 benchmark**：发起端持续 `submitTransfer`，同时 `tail -f` 日志。

**需要观察的现象**（按时间顺序）：

- 正常时流量走首选 NIC；
- 故障注入后，对坏 rail 的请求先在 [worker_pool.cpp:151-167](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp#L151-L167) 立即换到备选设备，吞吐可能短期下降但不中断；
- 该 rail 错误累计到 5 次，日志出现 `Rail paused: peer=... error_count=5`，期间不再用这条 rail；
- `redispatch_counter_` 自增，其它 worker 把队列 slice 换路重投；
- 约 1 秒后暂停到期，`error_count` 清零，该 rail 自动恢复（若底层故障已解除）；
- 若**所有** rail 都坏（本地网卡故障），连续 32 次后出现 `All rails failed ... marking inactive`，该 context 进入快速失败。

**预期结果**：单条 rail 故障不会让传输整体失败——流量自动迁到备选 NIC，故障 rail 被冷却 1 秒后自愈。只有本地 RNIC 整体故障才会触发 context 失活的快速失败。

**待本地验证**：本实践依赖真实多 NIC RDMA 环境与可控的故障注入手段；在单 NIC / loopback 环境下 `all_rails_failed` 会很快触发 context 失活，无法观察到「备选设备接管」。若无法注入故障，可退化为「源码阅读型实践」：对照 4.4.2 的决策树，推演 `error_count` 从 0 涨到 5 再到期清零的全过程，并计算一次 `redispatch` 后 slice 会落在哪条 rail。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `markRailFailed` 按 `peer_nic_path`（轨道）粒度暂停，而不是直接禁用整块本地 NIC？

> **参考答案**：一块本地 NIC 可能与多个对端 NIC 通信，构成多条 rail。坏掉的往往只是「本机 NIC A → 对端 NIC B」这一条（比如对端 B 端口 down 了），本机 NIC A 到对端 NIC C 的 rail 仍正常。按 rail 粒度暂停，能精确隔离坏的那条，让 A 继续服务其它对端，最大化可用性。直接禁整块 NIC 会误伤所有经它的 rail。

**练习 2**：`MultiTransport` 层既然不做跨协议故障转移，那在「RDMA 网卡整块坏了」的极端情况下，上层应用如何继续工作？

> **参考答案**：两条路。（1）应用层显式故障转移：捕获 `FAILED`/`TIMEOUT` 后，在多协议模式下用 `mp_submitTransfer` 换一个 `preferred_proto`（如改走 TCP）重新提交——这要求目标 segment 的 `protocol` 列表里本就包含备选协议，且发起端装了对应传输。（2）引擎内的 context 失活会让该坏 NIC 快速失败，上层据此剔除该节点/重试到其它节点。两种都**不是** `MultiTransport` 自动完成的协议切换，而是显式的、由调用方或部署拓扑决定的行为。

---

## 5. 综合实践

把本讲的四个模块串起来，完成一个「安装多传输 → 路由 → 故障转移」的端到端推演与小实验。

**任务**：在一台有 RDMA 网卡的机器上，用 `transfer_engine_bench`（或自写最小程序）完成下列目标。

**步骤**：

1. **手动安装两种传输**。`FLAGS_auto_discovery=false`，仿照 [transfer_engine_bench.cpp:287-301](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/transfer_engine_bench.cpp#L287-L301)，先后调 `installTransport("rdma", args)` 与 `installTransport("tcp", nullptr)`。读 [transfer_engine_impl.cpp:412-416](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp#L412-L416) 确认第二次装同协议只会告警返回旧实例——所以要装**不同**协议才不会冲突。

2. **解释路由**。对照 [multi_transport.cpp:442-464](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp#L442-L464)，回答：若 target 进程只装了 `tcp`，initiator（装了 tcp+rdma）向它发请求会落到哪个 `Transport`？为什么不会落到 RDMA？（答：TcpTransport，因为目标 segment 的 `protocol=="tcp"`。）

3. **触发故障转移**。给 RDMA 配 ≥2 块 NIC 的优先级矩阵，按 4.4.4 注入一条 rail 故障，观察日志里 `Rail paused`（[worker_pool.cpp:585](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp#L585)）出现，并确认传输未中断（备选设备接管）。

4. **画一张分层故障转移图**。用文字画出三层的触发条件与动作：
   - NIC 级：`selectDevice` retry_count 轮转（u2-l3）；
   - Rail 级：5 次错误 → 暂停 1s → 备选设备 / redispatch（本讲）；
   - Context 级：32 次全 rail 失败 → inactive 快速失败（本讲）。

**参考答案要点**：

```
分层故障转移（自下而上自动触发，均不跨协议）：
  NIC 级  : selectDevice(retry) 把 preferred→avail 全试一遍     (Topology 层)
  Rail 级 : markRailFailed 达 5 次 → isRailAvailable=false →
            submitPostSend 换备选设备 / performPostSend redispatch → 1s 后自愈
  Context级: 连续 32 次 all_rails_failed → set_active(false) → 快速 markFailed
跨协议    : 仅应用层显式 mp_submitTransfer(preferred_proto=...) 重试，非自动
```

**待本地验证**：步骤 3 依赖真实多 NIC 与可控故障注入；步骤 1、2 可在任意能编译运行的机器上完成。若环境受限，把步骤 4 的图作为本实践的最终产出即可。

---

## 6. 本讲小结

- **`MultiTransport` 是传输聚合器**：`TransferEngineImpl` 持有它（[transfer_engine_impl.h:414](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_engine_impl.h#L414)），它用 `transport_map_`（`proto → Transport`）统一管理多种传输，对外 API 与 `Transport` 一致，`TransferEngineImpl` 几乎全权透传（行 119–134）。
- **`submitTransfer` 三步走**：选传输（`selectTransport`）→ 按传输分组（`submit_tasks`）→ 逐组 `submitTransferTask` 下发（[multi_transport.cpp:110-149](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp#L110-L149)）；每个 `TransferTask` 记下 `transport_`，供 `getTransferStatus` 回查。
- **安装是按 `proto` 名字的工厂**：`installTransport` 用一长串被编译宏门控的 if-else 实例化子类，成功入表、失败 `delete`（[multi_transport.cpp:309-440](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp#L309-L440)）；自动发现里 `rdma`/`tcp` 二选一（`MC_FORCE_TCP`/`MC_FORCE_HCA` 可覆盖），`cxl`/`hip` 可并存。
- **协议选择由目标 segment 决定**：单协议 `selectTransport` 读 `target->protocol` 直接查表；多协议 `mp_selectTransport` 解析逗号列表、由发起端指定 `preferred_proto` 且必须被目标支持（[multi_transport.cpp:442-505](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp#L442-L505)）。
- **`MultiTransport` 不做跨协议自动故障转移**：选不到协议就返回 `NotSupportedTransport`，不降级。真正的替代路径选择在 RDMA 内部的 rail/context 层——rail 5 次错误暂停 1 秒并换备选设备、redispatch 重投，context 连续 32 次全 rail 失败则失活快速失败（[worker_pool.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp)、[worker_pool.h:113-119](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transport/rdma_transport/worker_pool.h#L113-L119)）。
- 跨协议的容错是**显式**的：应用层在多协议模式下改 `preferred_proto` 重提交，或部署时让目标声明多个协议、发起端装多种传输来预留备用路径。

---

## 7. 下一步学习建议

1. **深入 `RdmaTransport`**：本讲的故障转移只是入口。读 [worker_pool.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp) 全文，理解 `submitPostSend`/`performPostSend`/`performPollCq` 的线程模型、slice 的 `max_retry_cnt` 与 `retry_cnt` 如何配合 `shouldRetrySlice`，以及 `redispatch` 如何重新 `selectDevice`。
2. **CXL 多协议内存模型**：读 [mp_transport_test.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tests/mp_transport_test.cpp) 和 `mp_registerLocalMemory` 在 [transfer_engine_impl.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine_impl.cpp) 中的实现，弄清一段内存如何同时为 `cxl` 和 `rdma` 两种协议注册、目标 segment 的 `protocol` 字段如何变成 `"cxl,rdma"`。
3. **TENT 新引擎的故障转移**：项目正在开发新一代传输引擎 `tent/`（由 `MC_USE_TENT` 启用）。它的 [transport_selector.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transport_selector.cpp) 与 [engine_failover_e2e_test.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/tests/engine_failover_e2e_test.cpp) 实现了更完整的跨传输故障转移，可作为对比阅读——理解新旧两套机制在「协议级 failover」上的设计差异。
4. **超时与状态机**：结合 [multi_transport.cpp:195-261](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp#L195-L261) 的 `getTransferStatus` 和 `globalConfig().slice_timeout`（[config.h:64](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/config.h#L64)），弄清 `WAITING/PENDING/COMPLETED/TIMEOUT/FAILED` 状态如何流转，这是上层决定是否重试/切换路径的依据。
